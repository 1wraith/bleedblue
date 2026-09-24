"""
Enrichment layer — turns raw identifiers into meaning. Pure functions, no
hardware, fully testable on a laptop.

Three jobs:
  1. Vendor lookup   — first 3 MAC octets (OUI) -> manufacturer name.
  2. Address type    — tell a real public MAC from a BLE random/private one.
  3. BLE AD decode   — parse advertising payloads into names, services, tx
                       power, manufacturer data, and iBeacon / Eddystone beacons.

The OUI and company-ID tables below are a curated STARTER set of common
prefixes — enough to be useful out of the box and to demonstrate the pipeline.
For exhaustive coverage, drop the IEEE `oui.txt` or a Wireshark `manuf` file
next to the app and point OUI_FILE at it (see load_oui_file); this stays fully
offline. Unknown prefixes simply return None, never a wrong guess.
"""
from __future__ import annotations

import struct
from typing import Dict, List, Optional

# --- curated OUI table (lowercase "xx:xx:xx" -> vendor) ---------------------
_OUI: Dict[str, str] = {}


def _seed(vendor: str, *prefixes: str) -> None:
    for p in prefixes:
        _OUI[p.lower()] = vendor


_seed("Apple", "00:03:93", "00:0a:95", "3c:5a:b4", "a4:83:e7", "f0:18:98",
      "dc:a9:04", "ac:bc:32", "f4:0f:24", "90:b0:ed", "6c:40:08")
_seed("Samsung", "00:00:f0", "34:23:87", "5c:0a:5b", "e8:50:8b", "a0:21:95")
_seed("Google", "94:eb:2c", "f4:f5:e8", "d4:f5:47", "00:1a:11", "3c:8d:20")
_seed("Espressif", "24:0a:c4", "30:ae:a4", "3c:71:bf", "a4:cf:12", "24:6f:28",
      "cc:50:e3", "84:cc:a8", "dc:4f:22", "7c:9e:bd", "b4:e6:2d")
_seed("Raspberry Pi", "b8:27:eb", "dc:a6:32", "e4:5f:01", "28:cd:c1", "d8:3a:dd", "2c:cf:67")
_seed("Texas Instruments", "00:12:4b", "00:17:e9", "54:6c:0e", "98:07:2d", "a0:e6:f8")
_seed("Intel", "00:1b:21", "3c:a9:f4", "5c:e0:c5", "a4:34:d9", "94:65:9c")
_seed("Microsoft", "00:12:5a", "28:18:78", "7c:1e:52", "98:5f:d3")
_seed("Amazon", "44:65:0d", "74:75:48", "fc:65:de", "a0:02:dc", "68:37:e9")
_seed("Xiaomi", "00:9e:c8", "28:6c:07", "64:09:80", "f8:a4:5f", "fc:64:ba")
_seed("Sonos", "00:0e:58", "5c:aa:fd", "94:9f:3e", "b8:e9:37")
_seed("Bose", "04:52:c7", "2c:41:a1", "60:ab:d2")
_seed("Garmin", "10:c6:fc", "98:83:89")
_seed("TP-Link", "50:c7:bf", "a4:2b:b0", "f4:f2:6d", "c0:06:c3")
_seed("Netgear", "00:09:5b", "20:e5:2a", "a0:40:a0")
_seed("Ubiquiti", "00:15:6d", "24:a4:3c", "fc:ec:da", "78:8a:20")
_seed("Cisco", "00:1a:2f", "00:25:45", "f4:cf:e2")
_seed("HP", "00:1b:78", "3c:d9:2b", "a0:d3:c1")
_seed("Dell", "00:14:22", "18:03:73", "f8:bc:12")

# --- Bluetooth SIG company IDs (manufacturer data) --------------------------
_COMPANY: Dict[int, str] = {
    0x004C: "Apple", 0x0006: "Microsoft", 0x00E0: "Google",
    0x0075: "Samsung", 0x0059: "Nordic Semiconductor", 0x000D: "Texas Instruments",
    0x0087: "Garmin", 0x02E5: "Espressif", 0x009E: "Bose", 0x0157: "Huami (Amazfit)",
    0x0499: "Ruuvi", 0x000F: "Broadcom", 0x0001: "Nokia",
}

def load_oui_file(path: str) -> int:
    """Merge an IEEE oui.txt or Wireshark manuf file. Returns entries added."""
    added = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # manuf:  "XX:XX:XX<tab>Vendor"   oui.txt: "XX-XX-XX   (hex)<tab>Vendor"
                head = line.split()[0].replace("-", ":")
                if len(head) >= 8 and head[2] == ":" and head[5] == ":":
                    parts = line.split("\t")
                    vendor = parts[-1].strip() if len(parts) > 1 else line.split(None, 1)[-1].strip()
                    _OUI[head[:8].lower()] = vendor
                    added += 1
    except OSError:
        pass
    return added

def classify_addr(mac: str) -> str:
    """Best-effort BLE address type from the bytes alone.

    Note: public-vs-random is truly signalled by the BLE TxAdd bit, not the
    address, so this uses the locally-administered bit as a heuristic and is
    marked as such in output.
    """
    try:
        msb = int(mac[0:2], 16)
    except ValueError:
        return "unknown"
    if not (msb & 0x02):          # universally administered -> real OUI
        return "public"
    top = msb >> 6                # random sub-type from top two bits
    return {0b11: "random-static", 0b01: "resolvable-private",
            0b00: "nonresolvable-private"}.get(top, "random")


def lookup_vendor(mac: str) -> Optional[str]:
    return _OUI.get(mac[:8].lower()) if len(mac) >= 8 else None


def enrich_mac(mac: str) -> Dict:
    atype = classify_addr(mac)
    vendor = lookup_vendor(mac) if atype == "public" else None
    return {"vendor": vendor, "addr_type": atype}

# --- BLE advertising-data parser -------------------------------------------
def _s8(b: int) -> int:
    return b - 256 if b > 127 else b

def _le_uuid(b: bytes) -> str:
    return "0x" + b[::-1].hex()

def parse_ad(data: bytes) -> Dict:
    out: Dict = {"name": None, "services": [], "tx_power": None, "flags": None,
                 "company_id": None, "company": None, "beacon": None}
    i, n = 0, len(data)
    while i < n:
        ln = data[i]
        if ln == 0 or i + 1 + ln > n:
            break
        typ = data[i + 1]
        val = data[i + 2:i + 1 + ln]
        if typ == 0x01 and val:
            out["flags"] = val[0]
        elif typ in (0x02, 0x03):
            out["services"] += [_le_uuid(val[j:j + 2]) for j in range(0, len(val) - 1, 2)]
        elif typ in (0x08, 0x09):
            out["name"] = val.decode("utf-8", "replace")
        elif typ == 0x0A and val:
            out["tx_power"] = _s8(val[0])
        elif typ == 0x16 and len(val) >= 2:          # service data (16-bit UUID)
            uuid = int.from_bytes(val[0:2], "little")
            if uuid == 0xFEAA:                        # Eddystone
                frame = {0x00: "UID", 0x10: "URL", 0x20: "TLM", 0x30: "EID"}.get(
                    val[2] if len(val) > 2 else -1, "?")
                out["beacon"] = {"type": "Eddystone", "frame": frame}
        elif typ == 0xFF and len(val) >= 2:          # manufacturer specific
            cid = int.from_bytes(val[0:2], "little")
            out["company_id"] = cid
            out["company"] = _COMPANY.get(cid)
            body = val[2:]
            if cid == 0x004C and len(body) >= 23 and body[0] == 0x02 and body[1] == 0x15:
                out["beacon"] = {
                    "type": "iBeacon",
                    "uuid": body[2:18].hex(),
                    "major": int.from_bytes(body[18:20], "big"),
                    "minor": int.from_bytes(body[20:22], "big"),
                    "tx": _s8(body[22]),
                }
        i += 1 + ln
    return out


HEX = set("0123456789abcdefABCDEF")


def extract_hex(line: str) -> Optional[bytes]:
    """Pull the longest hex-byte run out of a text line (real-HW path).

    Handles both one long hex string ("0201060509...") and space-separated
    bytes ("02 01 06 05 09 ..."), which is how ubertooth-btle dumps payloads.
    """
    # strip a leading label like "Data:" so its letters don't break the run
    if ":" in line:
        line = line.split(":", 1)[1]
    toks = line.replace("-", " ").split()
    best: List[str] = []
    run: List[str] = []
    for t in toks:
        if len(t) == 2 and all(c in HEX for c in t):
            run.append(t)
        else:
            if len(run) > len(best):
                best = run
            run = []
            if len(t) >= 4 and len(t) % 2 == 0 and all(c in HEX for c in t):
                best = max(best, [t[j:j + 2] for j in range(0, len(t), 2)], key=len)
    if len(run) > len(best):
        best = run
    if len(best) < 2:
        return None
    try:
        return bytes.fromhex("".join(best))
    except ValueError:
        return None


def enrich_ble(mac: str, ad: Optional[bytes]) -> Dict:
    info = enrich_mac(mac)
    if ad:
        info.update({k: v for k, v in parse_ad(ad).items() if v not in (None, [], {})})
    return info
