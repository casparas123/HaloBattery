"""AULA F75 on its 2.4 GHz receiver (3554:FA09).

The F75's receiver publishes a keyboard collection and the Compx control
collection ``0xFF02:0x0002`` on interface 1.  The newer 20-byte firmware speaks
its own report id ``0x13`` exchange, recovered from the F75 and documented in
ushineko/sanshoku#36 (MIT-licensed captures):

    request  13 4A 00 ... 00 <sum of bytes 0..18>
    reply    13 4A 01 00 02 <level> <state> ... <sum of bytes 0..18>

The level is byte 5 (1..100).  Byte 6 is the state: 0x01 on battery, 0x10 with
the cable in.  In the cable state the level byte always reads 100 even when the
keyboard is at 97 %, so the last level read on battery is kept and the charger
is reported from the state byte instead of inventing a new percentage.

Some receivers answer the older 17-byte Compx report-0x08 frame instead.  That
path is used only when Windows reports a 17-byte output report, or when the
20-byte query gets no answer and the length is unknown; the old frame and its
offsets are in providers/compx.py.

Only the FF02:0002 collection is written to.  The keyboard's wired identity is
not claimed: the F75 has no battery command on its cable.
"""
from __future__ import annotations

import time
from typing import List, Optional, Tuple

try:
    import hid
except ImportError:              # pragma: no cover
    hid = None

from . import compx, hidlist
from .base import DeviceStatus, Provider, hexdump, log

VID = 0x3554
PID = 0xFA09
PAGE, USAGE = 0xFF02, 0x0002

REPORT_ID = 0x13
COMMAND = 0x4A
REPORT_LEN = 20
LEVEL_AT = 5
STATE_AT = 6
STATE_BATTERY = 0x01
STATE_CABLE = 0x10

ATTEMPTS = 3
READ_TIMEOUT_MS = 250
ATTEMPT_GAP = 0.2

NAME = "AULA F75"
KEY = "aula:f75"


def make_request() -> bytes:
    r = [REPORT_ID, COMMAND] + [0x00] * (REPORT_LEN - 3)
    r.append(sum(r) & 0xFF)
    return bytes(r)


def parse_reply(r) -> Optional[Tuple[int, int]]:
    """(level, state) from the 20-byte F75 reply, or None.

    The checksum is the sum of every byte before the last one.  A reply with a
    different command is a status push, not an answer to the battery query.
    """
    if not r or len(r) < REPORT_LEN:
        return None
    b = list(r[:REPORT_LEN])
    if b[0] != REPORT_ID or (b[1] & 0x7F) != COMMAND:
        return None
    if (sum(b[:-1]) & 0xFF) != b[-1]:
        return None
    level = b[LEVEL_AT]
    if not 1 <= level <= 100:
        return None
    return level, b[STATE_AT]


class AulaF75Provider(Provider):
    name = "aula_f75"

    def __init__(self):
        self._diag: List[str] = []
        self._last_level: Optional[int] = None

    def poll(self) -> List[DeviceStatus]:
        self._diag = []
        if hid is None:
            return []
        try:
            infos = hidlist.enumerate(VID)
        except Exception as e:  # pragma: no cover
            log.warning("hid.enumerate(aula_f75): %s", e)
            return []

        candidates = [d for d in infos
                      if d.get("product_id") == PID
                      and (d.get("usage_page"), d.get("usage")) == (PAGE, USAGE)]
        if not candidates:
            return []
        d = candidates[0]
        length = compx.output_length(d["path"])
        self._diag.append(f"[AULA F75] pid=3554:{PID:04x} "
                          f"iface={d.get('interface_number')} "
                          f"{PAGE:04x}:{USAGE:04x} output={length}")

        got = None
        if length == 17:
            got = self._read_compx(d["path"])
        else:
            got = self._read_f75(d["path"])
            if got is None and length is None:
                # An old receiver may not expose its output length; try the
                # 17-byte Compx frame before giving up.
                got = self._read_compx(d["path"])

        if got is None:
            self._diag.append("  no answer (keyboard off, asleep, or asleep on its cable)")
            return []

        level, charging, charging_known = got
        self._diag.append(f"  -> {level}%{' (charging)' if charging else ''}"
                          f"{'' if charging_known else ' (charger unknown)'}")
        if level is not None:
            self._last_level = level
        return [DeviceStatus(KEY, NAME, level, charging, True, "aula_f75",
                             kind="keyboard", charging_known=charging_known)]

    def _read_f75(self, path) -> Optional[Tuple[Optional[int], bool, bool]]:
        dev = hid.device()  # type: ignore[union-attr]
        try:
            dev.open_path(path)
        except (OSError, IOError) as e:
            self._diag.append(f"  open: {e}")
            return None
        try:
            for _ in range(ATTEMPTS):
                dev.write(make_request())
                deadline = time.time() + ATTEMPT_GAP
                while time.time() < deadline:
                    r = dev.read(REPORT_LEN, READ_TIMEOUT_MS)
                    if not r:
                        continue
                    self._diag.append(f"  reply: {hexdump(r, 20)}")
                    parsed = parse_reply(r)
                    if parsed is None:
                        continue
                    level, state = parsed
                    if state == STATE_CABLE:
                        # The level byte is a cable-placeholder 100, not the
                        # charge level; keep the last real reading.
                        return (self._last_level, True, True)
                    if state == STATE_BATTERY:
                        return (level, False, True)
                    self._diag.append(f"  unknown state 0x{state:02x}; level kept")
                    return (level, False, False)
                time.sleep(0.05)
            return None
        except (OSError, IOError, ValueError) as e:
            self._diag.append(f"  query error: {e}")
            return None
        finally:
            try:
                dev.close()
            except Exception:
                pass

    def _read_compx(self, path) -> Optional[Tuple[Optional[int], bool, bool]]:
        dev = hid.device()  # type: ignore[union-attr]
        try:
            dev.open_path(path)
        except (OSError, IOError) as e:
            self._diag.append(f"  open: {e}")
            return None
        try:
            for _ in range(compx.READ_ATTEMPTS):
                dev.write(compx.make_request())
                r = dev.read(compx.PAYLOAD_LEN, compx.READ_TIMEOUT_MS)
                if not r:
                    continue
                self._diag.append(f"  reply (17-byte): {hexdump(r, 17)}")
                parsed = compx.parse_power(r)
                if parsed is not None:
                    return parsed[0], parsed[1], True
            return None
        except (OSError, IOError, ValueError) as e:
            self._diag.append(f"  query error: {e}")
            return None
        finally:
            try:
                dev.close()
            except Exception:
                pass

    def diagnostics(self) -> List[str]:
        return list(self._diag)
