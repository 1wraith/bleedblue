"""
Data sources — the actual capabilities, now enriched.

Each Source declares the hardware Requirement it needs and provides two runners:
  * run_real() — drives the genuine radio tool
  * run_sim()  — emits clearly-tagged synthetic data so the frontend can be
                 built on a laptop with no Ubertooth attached

Depth added over the first version:
  * BLE      — decodes advertising payloads (name, services, tx power,
               manufacturer/company, iBeacon + Eddystone) and vendor/address-type
  * Kismet   — real device enumeration (type, SSID, signal, associations) with an
               API key; vendor-enriched
  * Spectrum — noise-floor estimate, peak, and non-hopping-carrier (interference)
               detection — the Ubertooth as a jamming/anomaly DETECTOR
  * GPS      — fix quality (satellites, HDOP) and feeds hub.set_fix so every
               event is geotagged at emit time

REAL-HARDWARE PARSERS remain marked VERIFY: exact ubertooth/kismet output shifts
by version, so confirm against your device. The simulated paths are exact and
exercise all of the enrichment above.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import struct
import time
import urllib.request
from collections import deque
from typing import Dict, List, Optional

import enrich
from capabilities import Req
from supervisor import Process, State


class SourceUnavailable(Exception):
    """Raised when a source is asked to start but its hardware is absent and sim is off."""


class Source:
    key: str = ""
    title: str = ""
    requires: Req = Req.NONE
    event_type: str = ""

    def __init__(self, hub, caps: Dict) -> None:
        self.hub = hub
        self.caps = caps
        self.running = False
        self.sim = False
        self.error = ""
        self._task: Optional[asyncio.Task] = None
        self._proc: Optional[Process] = None

    @property
    def available(self) -> bool:
        return self.caps[self.requires.value].available

    def emit(self, **payload) -> None:
        self.hub.publish({"type": self.event_type, "source": self.key, "sim": self.sim, **payload})

    def _log(self, level: str, msg: str) -> None:
        self.hub.publish({"type": "log", "source": self.key, "level": level, "msg": msg})

    async def start(self, allow_sim: bool) -> None:
        if self.running:
            return
        self.error = ""
        if self.available:
            self.sim, runner = False, self.run_real
        elif allow_sim:
            self.sim, runner = True, self.run_sim
        else:
            raise SourceUnavailable(self.caps[self.requires.value].detail or f"{self.requires.value} unavailable")
        self.running = True
        self.hub.publish({"type": "state", "source": self.key, "running": True, "sim": self.sim})
        self._task = asyncio.create_task(self._guard(runner()))

    async def _guard(self, coro) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
            self._log("error", str(e))
        finally:
            self.running = False
            self.hub.publish({"type": "state", "source": self.key, "running": False, "sim": self.sim})

    async def stop(self) -> None:
        if self._proc:
            await self._proc.stop()
            self._proc = None
        if self._task:
            self._task.cancel()
        self.running = False

    async def _run_proc_until_stopped(self, argv, on_line) -> None:
        self._proc = Process(argv, on_line)
        await self._proc.start()
        while self._proc and self._proc.state is State.RUNNING:
            await asyncio.sleep(0.4)
        if self._proc and self._proc.state is State.ERROR:
            raise RuntimeError(self._proc.error)

    def status(self) -> Dict:
        return {
            "key": self.key, "title": self.title, "requires": self.requires.value,
            "available": self.available, "running": self.running, "sim": self.sim,
            "error": self.error, "event_type": self.event_type,
            "detail": self.caps[self.requires.value].detail,
        }

    async def run_real(self) -> None:
        raise NotImplementedError

    async def run_sim(self) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# 2.4 GHz spectrum sweep + analyzer  (ubertooth-specan)
# ---------------------------------------------------------------------------
class SpectrumSource(Source):
    key = "spectrum"
    title = "2.4 GHz spectrum analyser"
    requires = Req.UBERTOOTH
    event_type = "spectrum"
    ARGV = ["ubertooth-specan"]   # VERIFY output format against your firmware

    WINDOW = 40           # sweeps kept for interference detection
    CARRIER_HITS = 30     # of WINDOW: same peak freq -> non-hopping carrier
    ALERT_COOLDOWN = 8.0  # seconds between interference alerts

    def __init__(self, hub, caps):
        super().__init__(hub, caps)
        self._sweep: List[List[int]] = []
        self._last_freq = 0
        self._peaks: deque = deque(maxlen=self.WINDOW)
        self._last_alert = 0.0

    # ---- shared analysis + emit (both real and sim call this) -------------
    def _emit_sweep(self, points: List[List[int]]) -> None:
        if not points:
            return
        rssis = sorted(p[1] for p in points)
        noise = rssis[len(rssis) // 5]                 # ~20th percentile = baseline
        peak_f, peak_r = max(points, key=lambda p: p[1])
        self._peaks.append(peak_f)
        self._detect_interference()
        self.emit(kind="sweep", points=points, noise=noise, peak=[peak_f, peak_r])

    def _detect_interference(self) -> None:
        if len(self._peaks) < self.WINDOW:
            return
        common, hits = _mode(self._peaks)
        if hits >= self.CARRIER_HITS:
            now = time.time()
            if now - self._last_alert > self.ALERT_COOLDOWN:
                self._last_alert = now
                self._log("alert", f"possible non-hopping carrier near {common} MHz "
                                    f"({hits}/{self.WINDOW} sweeps) - interference/jammer signature")

    # ---- real -------------------------------------------------------------
    def _on_line(self, line: str) -> None:
        nums = []
        for t in line.replace(",", " ").split():
            try:
                nums.append(int(float(t)))
            except ValueError:
                pass
        freq = next((n for n in nums if 2400 <= n <= 2485), None)
        rssi = next((n for n in nums if -120 <= n <= 0), None)
        if freq is None or rssi is None:
            return
        if freq < self._last_freq and self._sweep:
            self._emit_sweep(self._sweep)
            self._sweep = []
        self._sweep.append([freq, rssi])
        self._last_freq = freq

    async def run_real(self) -> None:
        await self._run_proc_until_stopped(self.ARGV, self._on_line)

    # ---- sim (also injects a periodic carrier to exercise detection) ------
    async def run_sim(self) -> None:
        freqs = list(range(2402, 2482))
        humps = {2412: 18, 2437: 22, 2462: 16}
        carrier_until = 0
        tick = 0
        while self.running:
            tick += 1
            if tick % 260 == 0:                        # occasionally start a jammer
                carrier_until = tick + 90
                self._log("warn", "sim: injecting a persistent carrier at 2455 MHz")
            # fluctuate the Wi-Fi humps each sweep so their peak wanders between
            # channels (like real bursty traffic) — only a real non-hopping
            # carrier then produces a persistent peak the detector can catch.
            live = {cf: amp + random.randint(-8, 12) for cf, amp in humps.items()}
            pts = []
            for f in freqs:
                base = -95 + random.randint(-3, 3)
                for cf, amp in live.items():
                    if abs(f - cf) < 8:
                        base += int(max(0, amp) * (1 - abs(f - cf) / 8))
                if tick < carrier_until and f == 2455:      # single persistent bin
                    base = -38 + random.randint(-2, 2)
                pts.append([f, base])
            self._emit_sweep(pts)
            await asyncio.sleep(0.12)


# ---------------------------------------------------------------------------
# Bluetooth Low Energy discovery + payload decode  (ubertooth-btle)
# ---------------------------------------------------------------------------
class BleSource(Source):
    key = "ble"
    title = "BLE sniffer / discovery"
    requires = Req.UBERTOOTH
    event_type = "ble"
    ARGV = ["ubertooth-btle", "-f"]   # VERIFY flags/output for your firmware

    def __init__(self, hub, caps):
        super().__init__(hub, caps)
        self._cur_addr: Optional[str] = None

    # ---- real: pull address + AD hex from the text dump, then enrich ------
    def _on_line(self, line: str) -> None:
        low = line.lower()
        for tok in line.replace("=", " ").split():
            parts = tok.split(":")
            if len(parts) == 6 and all(len(p) == 2 for p in parts):
                self._cur_addr = tok.lower()
        rssi = next((int(t) for t in line.split() if t.lstrip("-").isdigit() and -120 <= int(t) <= 0), None)
        ad = enrich.extract_hex(line) if ("data" in low or "adv" in low) else None
        if self._cur_addr and (ad or rssi is not None):
            info = enrich.enrich_ble(self._cur_addr, ad)
            self.emit(kind="adv", addr=self._cur_addr, rssi=rssi, **info)

    async def run_real(self) -> None:
        await self._run_proc_until_stopped(self.ARGV, self._on_line)

    # ---- sim: build genuine AD payloads, then run them through enrich ------
    def _templates(self) -> List:
        def name_ad(mac, nm, svc=None, tx=None):
            b = bytes([2, 0x01, 0x06]) + bytes([len(nm) + 1, 0x09]) + nm.encode()
            if svc is not None:
                b += bytes([3, 0x03, svc & 0xFF, svc >> 8])
            if tx is not None:
                b += bytes([2, 0x0A, tx & 0xFF])
            return mac, b
        ibeacon_body = bytes([0x4C, 0x00, 0x02, 0x15]) + bytes(range(16)) + struct.pack(">HH", 1, 42) + bytes([0xC5])
        return [
            name_ad("24:0a:c4:11:22:33", "ESP-Sensor", svc=0x180D, tx=-12),    # Espressif, heart-rate
            name_ad("b8:27:eb:44:55:66", "pi-beacon", svc=0x180F),             # Raspberry Pi, battery
            ("ac:bc:32:aa:bb:cc", bytes([len(ibeacon_body) + 1, 0xFF]) + ibeacon_body),  # Apple iBeacon
            ("c0:06:c3:00:00:01", bytes([2, 0x01, 0x06, 5, 0x16, 0xAA, 0xFE, 0x10, 0x00])),  # Eddystone URL
            name_ad("d3:ab:cd:ef:11:22", "Tile"),                              # random-static addr
            name_ad("94:eb:2c:07:08:09", "Nest-Cam"),                          # Google
        ]

    async def run_sim(self) -> None:
        templates = self._templates()
        while self.running:
            mac, ad = random.choice(templates)
            info = enrich.enrich_ble(mac, ad)
            self.emit(kind="adv", addr=mac, rssi=random.randint(-95, -45), **info)
            await asyncio.sleep(random.uniform(0.25, 0.8))


# ---------------------------------------------------------------------------
# GPS position + fix quality  (gpsd JSON stream)
# ---------------------------------------------------------------------------
class GpsSource(Source):
    key = "gps"
    title = "GPS geotagging (gpsd)"
    requires = Req.GPSD
    event_type = "gps"

    async def run_real(self) -> None:
        reader, writer = await asyncio.open_connection("127.0.0.1", 2947)
        writer.write(b'?WATCH={"enable":true,"json":true}\n')
        await writer.drain()
        try:
            while self.running:
                raw = await reader.readline()
                if not raw:
                    break
                try:
                    obj = json.loads(raw.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    continue
                cls = obj.get("class")
                if cls == "TPV" and "lat" in obj and "lon" in obj:
                    self.hub.set_fix(obj["lat"], obj["lon"])
                    self.emit(kind="fix", lat=obj["lat"], lon=obj["lon"],
                              alt=obj.get("alt"), speed=obj.get("speed"))
                elif cls == "SKY":
                    sats = obj.get("satellites", [])
                    used = sum(1 for s in sats if s.get("used"))
                    self.emit(kind="quality", sats_used=used, sats_seen=len(sats),
                              hdop=obj.get("hdop"))
        finally:
            writer.close()

    async def run_sim(self) -> None:
        lat, lon = 51.283, -0.234
        tick = 0
        while self.running:
            tick += 1
            lat += random.uniform(-1, 1) * 1e-4
            lon += random.uniform(-1, 1) * 1e-4
            self.hub.set_fix(round(lat, 6), round(lon, 6))
            self.emit(kind="fix", lat=round(lat, 6), lon=round(lon, 6),
                      alt=round(random.uniform(40, 60), 1), speed=round(random.uniform(0, 1.5), 2))
            if tick % 5 == 0:
                self.emit(kind="quality", sats_used=random.randint(6, 11),
                          sats_seen=random.randint(11, 16), hdop=round(random.uniform(0.7, 1.8), 1))
            await asyncio.sleep(1.0)


# ---------------------------------------------------------------------------
# Kismet Wi-Fi / device engine  (REST enumeration)
# ---------------------------------------------------------------------------
class KismetSource(Source):
    key = "kismet"
    title = "Kismet Wi-Fi/device engine"
    requires = Req.KISMET
    event_type = "device"
    BASE = "http://127.0.0.1:2501"

    def _fetch(self, path: str) -> str:
        key = os.getenv("KISMET_APIKEY", "")
        sep = "&" if "?" in path else "?"
        url = self.BASE + path + (f"{sep}KISMET={key}" if key else "")
        req = urllib.request.Request(url)
        if key:                                   # VERIFY: Kismet accepts key via param or cookie
            req.add_header("Cookie", f"KISMET={key}")
        with urllib.request.urlopen(req, timeout=4) as r:
            return r.read().decode("utf-8", "replace")

    async def run_real(self) -> None:
        # Enumerate devices seen in the last few seconds. VERIFY the endpoint and
        # field names against your Kismet version; the schema is broad, so we read
        # defensively and enrich the MAC ourselves.
        loop = asyncio.get_event_loop()
        if not os.getenv("KISMET_APIKEY"):
            self._log("warn", "KISMET_APIKEY not set - device enumeration will likely 401")
        while self.running:
            try:
                data = await loop.run_in_executor(
                    None, self._fetch, "/devices/views/all/last-time/-5/devices.json")
                for dev in json.loads(data):
                    self._emit_device(dev)
            except Exception as e:  # noqa: BLE001
                self._log("warn", f"Kismet poll failed: {e}")
                await asyncio.sleep(3)
            await asyncio.sleep(2.0)

    def _emit_device(self, dev: Dict) -> None:
        mac = dev.get("kismet.device.base.macaddr", "")
        if not mac:
            return
        dtype = (dev.get("kismet.device.base.type") or "device").lower().replace(" ", "_")
        sig = dev.get("kismet.device.base.signal") or {}
        rssi = sig.get("kismet.common.signal.last_signal") if isinstance(sig, dict) else None
        ssid = dev.get("kismet.device.base.name") or dev.get("kismet.device.base.commonname") or ""
        vendor = enrich.lookup_vendor(mac) or dev.get("kismet.device.base.manuf")
        self.emit(kind="device", devtype=dtype, mac=mac.lower(), rssi=rssi, ssid=ssid, vendor=vendor)

    # ---- sim: APs with associated clients, vendor-enriched ----------------
    async def run_sim(self) -> None:
        aps = [
            {"bssid": "50:c7:bf:aa:00:01", "ssid": "HOME-WIFI", "vendor": "TP-Link"},
            {"bssid": "fc:ec:da:bb:00:02", "ssid": "coffeeshop", "vendor": "Ubiquiti"},
            {"bssid": "00:09:5b:cc:00:03", "ssid": "eduroam", "vendor": "Netgear"},
        ]
        clients = [dict(enrich.enrich_mac(m), mac=m) for m in
                   ["ac:bc:32:10:20:30", "94:eb:2c:40:50:60", "24:0a:c4:70:80:90", "f8:a4:5f:a0:b0:c0"]]
        while self.running:
            if random.random() < 0.4:
                ap = random.choice(aps)
                self.emit(kind="device", devtype="wifi_ap", mac=ap["bssid"],
                          ssid=ap["ssid"], vendor=ap["vendor"], rssi=random.randint(-75, -40))
            else:
                c = random.choice(clients); ap = random.choice(aps)
                self.emit(kind="device", devtype="wifi_client", mac=c["mac"],
                          vendor=c.get("vendor"), rssi=random.randint(-90, -50),
                          assoc=ap["bssid"], ssid=ap["ssid"])
            await asyncio.sleep(random.uniform(0.3, 0.9))


def _mode(seq):
    """Most common value and its count."""
    counts: Dict = {}
    for v in seq:
        counts[v] = counts.get(v, 0) + 1
    best = max(counts.items(), key=lambda kv: kv[1])
    return best[0], best[1]


# Registry consumed by main.py
SOURCES = [SpectrumSource, BleSource, GpsSource, KismetSource]