"""
Capability detection.

Efficiency module #3 — a declarative registry that probes, once at startup,
what hardware and tools are actually present. Each data source simply declares
the requirement it needs; this file decides whether that requirement is met and
supplies a human-readable reason when it isn't. That is what lets the app run on
a bare laptop and clearly FLAG everything the Ubertooth (or Kismet/GPS/Wi-Fi)
would normally provide.
"""
from __future__ import annotations

import asyncio
import shutil
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Dict, Tuple


class Req(str, Enum):
    UBERTOOTH = "ubertooth"
    KISMET = "kismet"
    GPSD = "gpsd"
    WIFI_MONITOR = "wifi_monitor"
    NONE = "none"


@dataclass
class Capability:
    key: str
    title: str
    requires: str
    available: bool = False
    detail: str = ""

    def dict(self) -> Dict:
        return asdict(self)


async def _run(argv, timeout: float = 4.0) -> Tuple[int, str]:
    try:
        p = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        return 127, ""
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        p.kill()
        return 124, ""
    return p.returncode or 0, out.decode("utf-8", "replace")


async def _port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        w.close()
        return True
    except Exception:
        return False


# --- individual probes -----------------------------------------------------

async def probe_ubertooth() -> Tuple[bool, str]:
    if not shutil.which("ubertooth-util"):
        return False, "ubertooth-util not installed — no Ubertooth toolchain on this host"
    rc, out = await _run(["ubertooth-util", "-v"])
    if rc != 0:
        return False, "toolchain present but no Ubertooth device is responding on USB"
    first = next((l for l in out.splitlines() if l.strip()), "device present")
    return True, first.strip()


async def probe_kismet() -> Tuple[bool, str]:
    if await _port_open("127.0.0.1", 2501):
        return True, "Kismet REST API reachable on :2501"
    if shutil.which("kismet"):
        return False, "kismet installed but not running (start it to enable this source)"
    return False, "kismet not installed"


async def probe_gpsd() -> Tuple[bool, str]:
    if await _port_open("127.0.0.1", 2947):
        return True, "gpsd reachable on :2947"
    if shutil.which("gpsd"):
        return False, "gpsd installed but not running"
    return False, "gpsd not installed"


async def probe_wifi_monitor() -> Tuple[bool, str]:
    if not shutil.which("iw"):
        return False, "iw not available (Linux Wi-Fi tooling absent — expected on macOS/Windows)"
    rc, out = await _run(["iw", "list"])
    if rc == 0 and "monitor" in out.lower():
        return True, "monitor-mode-capable Wi-Fi interface detected"
    return False, "no monitor-mode-capable Wi-Fi interface detected"


_PROBES = {
    Req.UBERTOOTH: ("Ubertooth radio", probe_ubertooth),
    Req.KISMET: ("Kismet engine", probe_kismet),
    Req.GPSD: ("GPS (gpsd)", probe_gpsd),
    Req.WIFI_MONITOR: ("Wi-Fi monitor mode", probe_wifi_monitor),
}


async def probe_all() -> Dict[str, Capability]:
    caps: Dict[str, Capability] = {}
    results = await asyncio.gather(*(fn() for _, fn in _PROBES.values()))
    for (req, (title, _)), (ok, detail) in zip(_PROBES.items(), results):
        caps[req.value] = Capability(req.value, title, req.value, ok, detail)
    caps[Req.NONE.value] = Capability("none", "No hardware required", "none", True, "always available")
    return caps
