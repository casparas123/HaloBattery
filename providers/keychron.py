"""Keychron wireless devices over their receiver (Ultra-Link 8K) or cable, without
Keychron's own software.

Two protocol families are covered here.

**Ultra-Link 8K / M5 (3434:d028, 3434:d048)** - protocol from
csutcliff/keychron-battery-dkms:

  * vendor collection on interface 4 (a Link-KM also exposes a plain keyboard
    collection there, so collection choice matters).
  * request: a 64-byte *feature* report `b3 06 00 ...` (report id 0xB3, command
    0x06 status).
  * reply: a 64-byte input report `b4 06 ... <level> ...`, battery in byte 20,
    accepted only when it is 100 or less.
  * up to three attempts, 100 ms apart, with a 500 ms wait for each reply.
  * the reply carries no charging flag, so none is reported.

**Keychron Link-KM (3434:d026, Nape Pro / M6)** - protocol from the Keychron
Launcher (WebHID), documented by the Nape Pro reverse-engineering projects:

  * collection: Usage Page 0xFF60 / Usage 0x61 (QMK raw HID), report id 0.
  * request: a 32-byte *output* report `a7 31 00 ...`; byte 0 is the KC_MISC
    command group (0xA7), byte 1 the "get battery report" sub-command (0x31).
  * reply: an input report `a7 31 <level> <state> ...`; level in byte 2.
  * the reply arrives after ~0.8 s, so the read timeout is 1.2 s.
  * the state byte is not used here; this exchange does not give a reliable
    charging flag.

The earlier `b3 06` attempt on d026 is kept out of the way: this receiver does
not answer it on any exposed collection (#270).
"""
from __future__ import annotations

import time
from typing import List, Optional

import hid

from . import hidlist
from .base import DeviceStatus, Provider, hexdump, log

KEYCHRON_VID = 0x3434

VENDOR_INTERFACE = 4            # Ultra-Link / M5 vendor interface

REPORT_ID_CMD = 0xB3
REPORT_ID_RESP = 0xB4
CMD_STATUS = 0x06
BATTERY_OFFSET = 20
REPORT_SIZE = 64

ATTEMPTS = 3
RETRY_DELAY = 0.1               # 100 ms between attempts
READ_TIMEOUT_MS = 500           # the module waits 500 ms for the reply
FLUSH_TIMEOUT_MS = 30

# Keychron Link-KM (Nape Pro, M6)
NAPE_PID = 0xD026
NAPE_USAGE_PAGE = 0xFF60
NAPE_USAGE = 0x0061
NAPE_REPORT_SIZE = 32
NAPE_CMD_GROUP = 0xA7
NAPE_CMD_BATTERY = 0x31
NAPE_TIMEOUT_MS = 1200          # the Launcher's answer takes ~0.8 s

PIDS = {
    0xD028: "Keychron Ultra-Link 8K",
    0xD048: "Keychron M5",
    0xD026: "Keychron Nape Pro",
}


def make_request() -> List[int]:
    return [REPORT_ID_CMD, CMD_STATUS] + [0x00] * (REPORT_SIZE - 2)


def make_nape_request() -> List[int]:
    return [NAPE_CMD_GROUP, NAPE_CMD_BATTERY] + [0x00] * (NAPE_REPORT_SIZE - 2)


def parse_level(r) -> Optional[int]:
    """Percent from an Ultra-Link status reply, or None when this is not one."""
    if not r or len(r) <= BATTERY_OFFSET:
        return None
    if r[0] != REPORT_ID_RESP or r[1] != CMD_STATUS:
        return None
    level = r[BATTERY_OFFSET]
    return level if 0 <= level <= 100 else None


def parse_nape(r) -> Optional[int]:
    """Percent from a Link-KM battery reply, or None when this is not one.

    hidapi may or may not include the report id (0) in what it returns, so the
    reply is accepted both as `a7 31 <level>` and `00 a7 31 <level>`.
    """
    if not r or len(r) < 4:
        return None
    if r[0] == NAPE_CMD_GROUP and r[1] == NAPE_CMD_BATTERY:
        offset = 2
    elif len(r) >= 5 and r[1] == NAPE_CMD_GROUP and r[2] == NAPE_CMD_BATTERY:
        offset = 3
    else:
        return None
    level = r[offset]
    return level if 0 <= level <= 100 else None


class KeychronProvider(Provider):
    name = "keychron"

    def __init__(self):
        self._diag: List[str] = []

    def _candidates(self, infos: List[dict]) -> List[dict]:
        # The Ultra-Link receiver's battery channel is the vendor collection on
        # interface 4. A Link-KM also exposes a plain keyboard collection there,
        # so interface alone is not a safe first choice (#270).
        ordered: List[dict] = []
        seen = set()

        def add(d):
            if d is not None and d.get("path") not in seen:
                seen.add(d.get("path"))
                ordered.append(d)

        vendor = [d for d in infos if (d.get("usage_page") or 0) >= 0xFF00]
        for d in vendor:
            if d.get("interface_number") == VENDOR_INTERFACE:
                add(d)
        for d in vendor:
            add(d)
        for d in infos:
            if d.get("interface_number") == VENDOR_INTERFACE:
                add(d)
        for d in infos:
            add(d)
        return ordered

    @staticmethod
    def _nape_candidates(infos: List[dict]) -> List[dict]:
        """The M6/Nape battery collection, vendor page first.

        The Launcher's battery action writes report id 0 on the mouse's 1K
        vendor page (ffc1:0001); the VIA page (ff60:0061) is the fallback. A
        previous build only tried the VIA page, which stayed silent (#270).
        """
        ordered = []
        for page, usage in ((0xFFC1, 0x0001), (NAPE_USAGE_PAGE, NAPE_USAGE)):
            for d in infos:
                if (d.get("usage_page"), d.get("usage")) == (page, usage):
                    ordered.append(d)
                    break
        return ordered

    def _open(self, path: bytes):
        dev = hid.device()
        try:
            dev.open_path(path)
        except (OSError, IOError) as e:
            self._diag.append(f"  open: {e}")
            return None
        return dev

    def _query(self, path: bytes) -> Optional[List[int]]:
        dev = self._open(path)
        if dev is None:
            return None
        try:
            for attempt in range(ATTEMPTS):
                if attempt:
                    time.sleep(RETRY_DELAY)
                try:
                    dev.send_feature_report(make_request())
                except (OSError, IOError, ValueError) as e:
                    self._diag.append(f"  attempt {attempt + 1} send: {e}")
                    continue
                try:
                    r = dev.read(REPORT_SIZE, READ_TIMEOUT_MS)
                except (OSError, IOError) as e:
                    # a wrong collection can fail the read outright; log it and let
                    # poll() move on to the next candidate instead of aborting (#270)
                    self._diag.append(f"  attempt {attempt + 1} read: {e}")
                    continue
                if not r:
                    self._diag.append(f"  attempt {attempt + 1}: no reply")
                    continue
                self._diag.append(f"  attempt {attempt + 1} reply: {hexdump(r)}")
                if parse_level(r) is not None:
                    return list(r)
            self._diag.append("  no usable level in the replies")
            return None
        finally:
            try:
                dev.close()
            except Exception:
                pass

    def _query_nape(self, path: bytes) -> Optional[List[int]]:
        dev = self._open(path)
        if dev is None:
            return None
        payload = bytes(make_nape_request())
        # WebHID sendReport(0, 32 bytes) and hidapi's "report id is the first byte"
        # disagree on whether that id is on the wire. The Launcher's packet is 32
        # bytes; try both forms before giving up (#270).
        variants = (("output+id", b"\x00" + payload), ("output", payload))
        try:
            for label, buf in variants:
                for attempt in range(2):
                    if attempt:
                        time.sleep(RETRY_DELAY)
                    try:
                        written = dev.write(buf)
                    except (OSError, IOError, ValueError) as e:
                        self._diag.append(f"  {label} write attempt {attempt + 1}: {e}")
                        continue
                    self._diag.append(f"  {label} write attempt {attempt + 1} -> {written}")
                    if isinstance(written, int) and written < 0:
                        continue
                    try:
                        r = dev.read(NAPE_REPORT_SIZE, NAPE_TIMEOUT_MS)
                    except (OSError, IOError) as e:
                        self._diag.append(f"  {label} read attempt {attempt + 1}: {e}")
                        continue
                    if not r:
                        self._diag.append(f"  {label} attempt {attempt + 1}: no reply")
                        continue
                    self._diag.append(f"  {label} attempt {attempt + 1} reply: {hexdump(r)}")
                    if parse_nape(r) is not None:
                        return list(r)
            # last resort: the same frame as a feature report
            try:
                written = dev.send_feature_report(b"\x00" + payload)
                self._diag.append(f"  feature write -> {written}")
                r = dev.get_feature_report(0, NAPE_REPORT_SIZE)
                self._diag.append(f"  feature reply: {hexdump(r)}")
                if parse_nape(r) is not None:
                    return list(r)
            except (OSError, IOError, ValueError) as e:
                self._diag.append(f"  feature: {e}")
            self._diag.append("  no usable level in the replies")
            return None
        finally:
            try:
                dev.close()
            except Exception:
                pass

    def poll(self) -> List[DeviceStatus]:
        self._diag = []
        try:
            infos = hidlist.enumerate(KEYCHRON_VID)
        except Exception as e:  # pragma: no cover
            log.warning("hid.enumerate(keychron): %s", e)
            return []
        out = []
        seen = set()
        for pid in PIDS:
            mine = [d for d in infos if d["product_id"] == pid and d["path"] not in seen]
            if not mine:
                continue
            name = PIDS[pid]
            if pid == NAPE_PID:
                candidates = self._nape_candidates(mine)
                if not candidates:
                    self._diag.append(f"[Keychron] pid={pid:04x} '{name}': "
                                      f"no ffc1:0001 or {NAPE_USAGE_PAGE:04x}:{NAPE_USAGE:04x} collection")
                    continue
                for d in candidates:
                    seen.add(d["path"])
                    self._diag.append(f"[Keychron] pid={pid:04x} '{name}' "
                                      f"iface={d.get('interface_number')} "
                                      f"{d.get('usage_page', 0):04x}:{d.get('usage', 0):04x} "
                                      f"(a7 31)")
                    reply = self._query_nape(d["path"])
                    level = parse_nape(reply)
                    if level is not None:
                        out.append(DeviceStatus(f"keychron:{pid:04x}", name, level, False, True,
                                                "keychron", charging_known=False))
                        break
                continue
            for d in self._candidates(mine):
                seen.add(d["path"])
                self._diag.append(f"[Keychron] pid={pid:04x} '{name}' "
                                  f"iface={d.get('interface_number')} "
                                  f"{d.get('usage_page', 0):04x}:{d.get('usage', 0):04x}")
                reply = self._query(d["path"])
                level = parse_level(reply)
                if level is None:
                    continue
                out.append(DeviceStatus(f"keychron:{pid:04x}", name, level, False, True,
                                        "keychron"))
                break
        return out

    def diagnostics(self) -> List[str]:
        return list(self._diag)
