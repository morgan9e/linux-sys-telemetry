#!/usr/bin/python3 -sP
"""sensord — system sensor bridge for D-Bus.

Reads hardware sensors from sysfs/procfs and exposes them
as D-Bus interfaces for sandboxed and desktop consumers.

Bus:    org.sensord
Object: /org/sensord

Interfaces:
    org.sensord.Power     — RAPL power draw (W)
    org.sensord.Thermal   — hwmon temperatures (°C)
    org.sensord.Cpu       — per-core and total usage (%)
    org.sensord.Memory    — memory utilization (bytes/%)
    org.sensord.Battery   — battery state (%/W/status)

Each interface exposes:
    GetReadings() → a{sd}
    GetMeta()     → a{sv}   (icon: s, units: a{ss})
    signal Changed(a{sd})

Usage:
    sensord --setup     install D-Bus policy
    sensord             start daemon (via systemd)
"""

import os, sys  # noqa: E401

import gi
gi.require_version("Gio", "2.0")
gi.require_version("GLibUnix", "2.0")
from gi.repository import Gio, GLib, GLibUnix  # noqa: E402

DBUS_NAME = "org.sensord"
DBUS_PATH = "/org/sensord"
DBUS_CONF = "/etc/dbus-1/system.d/org.sensord.conf"

POLICY = """\
<!DOCTYPE busconfig PUBLIC
  "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
  "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <policy user="root">
    <allow own="org.sensord"/>
  </policy>
  <policy context="default">
    <allow send_destination="org.sensord"/>
  </policy>
</busconfig>
"""


def make_iface_xml(name):
    return f"""<interface name="org.sensord.{name}">
  <method name="GetReadings"><arg direction="out" type="a{{sd}}"/></method>
  <method name="GetMeta"><arg direction="out" type="a{{sv}}"/></method>
  <signal name="Changed"><arg type="a{{sd}}"/></signal>
</interface>"""


INTROSPECTION = f"""
<node>
  {make_iface_xml("Power")}
  {make_iface_xml("Thermal")}
  {make_iface_xml("Cpu")}
  {make_iface_xml("Memory")}
  {make_iface_xml("Battery")}
</node>
"""


# ── sensors ───────────────────────────────────────────────────


class PowerSensor:
    """RAPL energy counters → watts."""

    RAPL_BASE = "/sys/class/powercap/intel-rapl"
    ICON = "battery-full-charged-symbolic"

    class Zone:
        __slots__ = ("name", "fd", "wrap", "prev_e", "prev_t")

        def __init__(self, path):
            self.name = self._read(path, "name") or os.path.basename(path)
            self.fd = os.open(os.path.join(path, "energy_uj"), os.O_RDONLY)
            self.wrap = int(self._read(path, "max_energy_range_uj") or 1 << 32)
            self.prev_e = self.prev_t = None

        @staticmethod
        def _read(path, name):
            try:
                with open(os.path.join(path, name)) as f:
                    return f.read().strip()
            except OSError:
                return None

        def sample(self):
            os.lseek(self.fd, 0, os.SEEK_SET)
            e = int(os.read(self.fd, 64))
            t = GLib.get_monotonic_time()

            if self.prev_e is None:
                self.prev_e, self.prev_t = e, t
                return None

            dE = e - self.prev_e
            dt = t - self.prev_t
            self.prev_e, self.prev_t = e, t

            if dE < 0:
                dE += self.wrap
            return dE / dt if dt > 0 else None

        def close(self):
            os.close(self.fd)

    def __init__(self):
        self.zones = []
        if not os.path.isdir(self.RAPL_BASE):
            return
        for root, _, files in os.walk(self.RAPL_BASE):
            if "energy_uj" in files:
                try:
                    z = self.Zone(root)
                    z.sample()
                    self.zones.append(z)
                    print(f"  power: {z.name}", file=sys.stderr)
                except OSError as e:
                    print(f"  power skip: {e}", file=sys.stderr)

    @property
    def available(self):
        return bool(self.zones)

    def units(self):
        return {z.name: "W" for z in self.zones}

    def sample(self):
        r = {}
        for z in self.zones:
            w = z.sample()
            if w is not None:
                r[z.name] = round(w, 2)
        return r

    def close(self):
        for z in self.zones:
            z.close()


class ThermalSensor:
    """hwmon temperature sensors → °C."""

    HWMON_BASE = "/sys/class/hwmon"
    ICON = "sensors-temperature-symbolic"

    class Chip:
        __slots__ = ("label", "fd")

        def __init__(self, label, path):
            self.label = label
            self.fd = os.open(path, os.O_RDONLY)

        def read(self):
            os.lseek(self.fd, 0, os.SEEK_SET)
            return int(os.read(self.fd, 32)) / 1000.0

        def close(self):
            os.close(self.fd)

    def __init__(self):
        self.chips = []
        if not os.path.isdir(self.HWMON_BASE):
            return

        for hwmon in os.listdir(self.HWMON_BASE):
            hwdir = os.path.join(self.HWMON_BASE, hwmon)
            chip_name = self._read_file(os.path.join(hwdir, "name")) or hwmon

            for f in sorted(os.listdir(hwdir)):
                if not f.startswith("temp") or not f.endswith("_input"):
                    continue

                path = os.path.join(hwdir, f)
                idx = f.replace("temp", "").replace("_input", "")
                label_path = os.path.join(hwdir, f"temp{idx}_label")
                label = self._read_file(label_path) or f"temp{idx}"
                full_label = f"{chip_name}/{label}"

                try:
                    chip = self.Chip(full_label, path)
                    self.chips.append(chip)
                    print(f"  thermal: {full_label}", file=sys.stderr)
                except OSError as e:
                    print(f"  thermal skip {full_label}: {e}", file=sys.stderr)

    @staticmethod
    def _read_file(path):
        try:
            with open(path) as f:
                return f.read().strip()
        except OSError:
            return None

    @property
    def available(self):
        return bool(self.chips)

    def units(self):
        return {c.label: "°C" for c in self.chips}

    def sample(self):
        r = {}
        for c in self.chips:
            try:
                r[c.label] = round(c.read(), 1)
            except (OSError, ValueError):
                pass
        return r

    def close(self):
        for c in self.chips:
            c.close()


class CpuSensor:
    """/proc/stat → per-core and total CPU usage %."""

    ICON = "utilities-system-monitor-symbolic"

    def __init__(self):
        self.fd = None
        self.prev = {}

        try:
            self.fd = os.open("/proc/stat", os.O_RDONLY)
            self._read_stat()
            print(f"  cpu: {len(self.prev)} entries", file=sys.stderr)
        except OSError as e:
            print(f"  cpu skip: {e}", file=sys.stderr)

    @property
    def available(self):
        return self.fd is not None

    def _read_stat(self):
        os.lseek(self.fd, 0, os.SEEK_SET)
        raw = os.read(self.fd, 8192).decode()
        entries = {}
        for line in raw.splitlines():
            if not line.startswith("cpu"):
                break
            parts = line.split()
            name = parts[0]
            vals = [int(v) for v in parts[1:]]
            idle = vals[3] + vals[4] if len(vals) > 4 else vals[3]
            total = sum(vals)
            entries[name] = (idle, total)
        return entries

    def units(self):
        r = {}
        for name in self.prev:
            label = "total" if name == "cpu" else name
            r[label] = "%"
        return r

    def sample(self):
        cur = self._read_stat()
        r = {}
        for name, (idle, total) in cur.items():
            if name in self.prev:
                pi, pt = self.prev[name]
                dt = total - pt
                di = idle - pi
                if dt > 0:
                    label = "total" if name == "cpu" else name
                    r[label] = round(100.0 * (1.0 - di / dt), 1)
        self.prev = cur
        return r

    def close(self):
        if self.fd is not None:
            os.close(self.fd)


class MemorySensor:
    """/proc/meminfo → memory stats in bytes and usage %."""

    ICON = "drive-harddisk-symbolic"
    KEYS = ("MemTotal", "MemAvailable", "MemFree", "SwapTotal", "SwapFree")

    def __init__(self):
        self.fd = None
        try:
            self.fd = os.open("/proc/meminfo", os.O_RDONLY)
            self.sample()
            print("  memory: ok", file=sys.stderr)
        except OSError as e:
            print(f"  memory skip: {e}", file=sys.stderr)

    @property
    def available(self):
        return self.fd is not None

    def units(self):
        return {
            "total": "bytes", "available": "bytes", "used": "bytes",
            "percent": "%",
            "swap_total": "bytes", "swap_used": "bytes", "swap_percent": "%",
        }

    def sample(self):
        os.lseek(self.fd, 0, os.SEEK_SET)
        raw = os.read(self.fd, 4096).decode()

        vals = {}
        for line in raw.splitlines():
            parts = line.split()
            key = parts[0].rstrip(":")
            if key in self.KEYS:
                vals[key] = int(parts[1]) * 1024

        r = {}
        mt = vals.get("MemTotal", 0)
        ma = vals.get("MemAvailable", 0)
        st = vals.get("SwapTotal", 0)
        sf = vals.get("SwapFree", 0)

        if mt:
            r["total"] = float(mt)
            r["available"] = float(ma)
            r["used"] = float(mt - ma)
            r["percent"] = round(100.0 * (1.0 - ma / mt), 1)
        if st:
            r["swap_total"] = float(st)
            r["swap_used"] = float(st - sf)
            r["swap_percent"] = round(100.0 * (1.0 - sf / st), 1) if st else 0.0

        return r

    def close(self):
        if self.fd is not None:
            os.close(self.fd)


class BatterySensor:
    """power_supply sysfs → battery state.

    Handles both energy-based (energy_*_uWh) and charge-based
    (charge_*_uAh) batteries.  Voltage/current/power/energy are
    derived from whatever sysfs exposes:

        power   = power_now          else  |voltage * current|
        energy  = energy_now         else  charge_now * V_design
        health  = full / full_design (capacity retained vs. new)
    """

    PS_BASE = "/sys/class/power_supply"
    ICON = "battery-symbolic"

    STATUS_MAP = {
        "Charging": 1.0, "Discharging": 2.0,
        "Not charging": 3.0, "Full": 4.0,
    }

    class Supply:
        __slots__ = ("name", "path", "is_battery", "v_design")

        def __init__(self, name, path, is_battery, v_design=0.0):
            self.name = name
            self.path = path
            self.is_battery = is_battery
            self.v_design = v_design  # volts, for charge→energy conversion

    def __init__(self):
        self.supplies = []
        if not os.path.isdir(self.PS_BASE):
            return

        for entry in sorted(os.listdir(self.PS_BASE)):
            path = os.path.join(self.PS_BASE, entry)
            ptype = self._read(path, "type")
            if ptype == "Battery":
                vd = self._micro(path, "voltage_min_design") or \
                    self._micro(path, "voltage_now") or 0.0
                self.supplies.append(self.Supply(entry, path, True, vd))
                print(f"  battery: {entry}", file=sys.stderr)
            elif ptype in ("Mains", "USB"):
                self.supplies.append(self.Supply(entry, path, False))
                print(f"  battery: {entry} (line)", file=sys.stderr)

    @staticmethod
    def _read(path, name):
        try:
            with open(os.path.join(path, name)) as f:
                return f.read().strip()
        except OSError:
            return None

    def _micro(self, path, name):
        """Read a sysfs micro-unit (uV/uA/uW/uWh/uAh) → base unit float."""
        v = self._read(path, name)
        if v is None:
            return None
        try:
            return int(v) / 1e6
        except ValueError:
            return None

    @property
    def available(self):
        return any(s.is_battery for s in self.supplies)

    def units(self):
        r = {}
        for s in self.supplies:
            n = s.name
            if not s.is_battery:
                r[f"{n}/online"] = "bool"
                if self._read(s.path, "voltage_now") is not None:
                    r[f"{n}/voltage"] = "V"
                if self._read(s.path, "current_now") is not None:
                    r[f"{n}/current"] = "A"
                continue

            r[f"{n}/percent"] = "%"
            r[f"{n}/status"] = "enum:Charging,Discharging,Not charging,Full"
            if self._read(s.path, "voltage_now") is not None:
                r[f"{n}/voltage"] = "V"
            if self._read(s.path, "current_now") is not None:
                r[f"{n}/current"] = "A"
            r[f"{n}/power"] = "W"
            if self._read(s.path, "charge_now") is not None:
                r[f"{n}/charge_now"] = "Ah"
                r[f"{n}/charge_full"] = "Ah"
                r[f"{n}/charge_design"] = "Ah"
            r[f"{n}/energy_now"] = "Wh"
            r[f"{n}/energy_full"] = "Wh"
            r[f"{n}/energy_design"] = "Wh"
            r[f"{n}/health"] = "%"
            if self._read(s.path, "cycle_count") is not None:
                r[f"{n}/cycles"] = "count"
        return r

    def _sample_battery(self, s, r):
        n = s.name

        cap = self._read(s.path, "capacity")
        if cap is not None:
            r[f"{n}/percent"] = float(cap)

        status = self._read(s.path, "status")
        if status is not None:
            r[f"{n}/status"] = self.STATUS_MAP.get(status, 0.0)

        volt = self._micro(s.path, "voltage_now")
        curr = self._micro(s.path, "current_now")
        if volt is not None:
            r[f"{n}/voltage"] = round(volt, 3)
        if curr is not None:
            r[f"{n}/current"] = round(curr, 3)

        # power: prefer the chip's own reading, else V * I
        power = self._micro(s.path, "power_now")
        if power is None and volt is not None and curr is not None:
            power = volt * curr
        if power is not None:
            r[f"{n}/power"] = round(abs(power), 2)

        # charge capacity (uAh batteries)
        c_now = self._micro(s.path, "charge_now")
        c_full = self._micro(s.path, "charge_full")
        c_design = self._micro(s.path, "charge_full_design")
        if c_now is not None:
            r[f"{n}/charge_now"] = round(c_now, 4)
        if c_full is not None:
            r[f"{n}/charge_full"] = round(c_full, 4)
        if c_design is not None:
            r[f"{n}/charge_design"] = round(c_design, 4)

        # energy: prefer energy_* sysfs, else derive charge * V_design
        e_now = self._micro(s.path, "energy_now")
        e_full = self._micro(s.path, "energy_full")
        e_design = self._micro(s.path, "energy_full_design")
        if e_now is None and c_now is not None:
            e_now = c_now * s.v_design
        if e_full is None and c_full is not None:
            e_full = c_full * s.v_design
        if e_design is None and c_design is not None:
            e_design = c_design * s.v_design
        if e_now is not None:
            r[f"{n}/energy_now"] = round(e_now, 2)
        if e_full is not None:
            r[f"{n}/energy_full"] = round(e_full, 2)
        if e_design is not None:
            r[f"{n}/energy_design"] = round(e_design, 2)

        # health: capacity retained relative to factory design
        full = c_full if c_full is not None else e_full
        design = c_design if c_design is not None else e_design
        if full is not None and design:
            r[f"{n}/health"] = round(100.0 * full / design, 1)

        cycles = self._read(s.path, "cycle_count")
        if cycles is not None:
            r[f"{n}/cycles"] = float(cycles)

    def sample(self):
        r = {}
        for s in self.supplies:
            if s.is_battery:
                self._sample_battery(s, r)
            else:
                online = self._read(s.path, "online")
                if online is not None:
                    r[f"{s.name}/online"] = float(online)
                volt = self._micro(s.path, "voltage_now")
                curr = self._micro(s.path, "current_now")
                if volt is not None:
                    r[f"{s.name}/voltage"] = round(volt, 3)
                if curr is not None:
                    r[f"{s.name}/current"] = round(curr, 3)
        return r

    def close(self):
        pass


# ── daemon ────────────────────────────────────────────────────


SENSORS = {
    "Power":   (PowerSensor,   1),
    "Thermal": (ThermalSensor, 2),
    "Cpu":     (CpuSensor,     1),
    "Memory":  (MemorySensor,  2),
    "Battery": (BatterySensor, 5),
}


class Daemon:
    def __init__(self):
        self.loop = GLib.MainLoop()
        self.bus = None
        self.sensors = {}
        self.readings = {}
        self.pending = {}
        self.node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION)

        for name, (cls, interval) in SENSORS.items():
            sensor = cls()
            if sensor.available:
                self.sensors[name] = sensor
                self.readings[name] = {}
            else:
                self.pending[name] = (cls, interval)

        if not self.sensors and not self.pending:
            raise RuntimeError("no sensors available")

        Gio.bus_own_name(
            Gio.BusType.SYSTEM, DBUS_NAME, Gio.BusNameOwnerFlags.NONE,
            self._on_bus, None, lambda *_: self.loop.quit(),
        )

    def _on_bus(self, conn, _name):
        self.bus = conn
        iface_map = {i.name: i for i in self.node.interfaces}
        for name in self.sensors:
            conn.register_object_with_closures2(
                DBUS_PATH, iface_map[f"org.sensord.{name}"],
                self._on_call, None, None,
            )
        for name in self.pending:
            conn.register_object_with_closures2(
                DBUS_PATH, iface_map[f"org.sensord.{name}"],
                self._on_call, None, None,
            )
        print(f"sensord: {', '.join(self.sensors)}", file=sys.stderr)
        if self.pending:
            print(f"sensord: waiting: {', '.join(self.pending)}", file=sys.stderr)

    def _on_call(self, conn, sender, path, iface, method, params, invocation):
        name = iface.rsplit(".", 1)[-1]
        if method == "GetReadings":
            invocation.return_value(GLib.Variant("(a{sd})", (self.readings.get(name, {}),)))
        elif method == "GetMeta":
            meta = {}
            sensor = self.sensors.get(name)
            if sensor:
                meta["icon"] = GLib.Variant("s", sensor.ICON)
                meta["units"] = GLib.Variant("a{ss}", sensor.units())
            invocation.return_value(GLib.Variant("(a{sv})", (meta,)))
        else:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)

    def _make_tick(self, name):
        def tick():
            r = self.sensors[name].sample()
            if r:
                self.readings[name] = r
                if self.bus:
                    self.bus.emit_signal(
                        None, DBUS_PATH, f"org.sensord.{name}", "Changed",
                        GLib.Variant.new_tuple(GLib.Variant("a{sd}", r)),
                    )
            return GLib.SOURCE_CONTINUE
        return tick

    def _make_probe(self, name):
        cls, interval = self.pending[name]
        def probe():
            sensor = cls()
            if not sensor.available:
                return GLib.SOURCE_CONTINUE
            self.sensors[name] = sensor
            self.readings[name] = {}
            del self.pending[name]
            print(f"sensord: late: {name}", file=sys.stderr)
            GLib.timeout_add_seconds(interval, self._make_tick(name))
            return GLib.SOURCE_REMOVE
        return probe

    def run(self):
        for name in self.sensors:
            GLib.timeout_add_seconds(SENSORS[name][1], self._make_tick(name))
        for name in list(self.pending):
            GLib.timeout_add_seconds(self.pending[name][1], self._make_probe(name))

        GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, 2, self.loop.quit)
        GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, 15, self.loop.quit)
        self.loop.run()

        for s in self.sensors.values():
            s.close()


# ── entry ─────────────────────────────────────────────────────


def setup():
    with open(DBUS_CONF, "w") as f:
        f.write(POLICY)
    os.chmod(DBUS_CONF, 0o644)
    print(f"wrote {DBUS_CONF}", file=sys.stderr)


def main():
    if os.geteuid() != 0:
        print("run as root", file=sys.stderr)
        sys.exit(1)

    if "--setup" in sys.argv:
        setup()
        return

    Daemon().run()


if __name__ == "__main__":
    main()
