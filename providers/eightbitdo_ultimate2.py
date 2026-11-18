"""8BitDo Ultimate 2 Wireless in XInput mode (2dc8:310b), #461.

The controller appears as an Xbox pad, so providers/xinput.py owns the icon;
this module only supplies the battery reading.  SDL's 8BitDo driver reads the
controller's enhanced input report on the vendor collection:

    usage page 0xFF7A, usage 0x0001
    byte 14: bit 7 = charging, bits 0-6 = level in %

The same layout is used by the Pro 2/Pro 3 and is documented in
providers/eightbitdo.py.  The Ultimate 2's report is 34 bytes on firmware
v1.03 (12 bytes on v1.02, whose report has no battery field); the v1.03 report
can arrive about 250 ms after the controller wakes, so the reader listens for
a short window instead of sending anything.

Only the vendor collection is opened, and only input reports are read.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List, Optional

try:
    import hid
except ImportError:              # pragma: no cover
    hid = None

from . import hidlist
from .base import hexdump, log

VID = 0x2DC8
PID = 0x310B
PAGE, USAGE = 0xFF7A, 0x0001
BATTERY_BYTE = 14
WINDOW = 0.8
MAX_REPORTS = 32


@dataclass
class Reading:
    name: str
    level: int
    charging: bool
    approx: str = ""


def parse_report(data) -> Optional[Reading]:
    """A battery reading from an enhanced report, or None.

    Byte 14 is the same field SDL's 8BitDo driver reads.  An ordinary report
    padded with zeros has 0 there and is ignored; the controller is running,
    so a 0 must not become a low-battery alert.
    """
    if not data or len(data) <= BATTERY_BYTE:
        return None
    level = data[BATTERY_BYTE] & 0x7F
    charging = bool(data[BATTERY_BYTE] & 0x80)
    if level == 0 or level > 100:
        return None
    return Reading("8BitDo Ultimate 2", level, charging)


def read_connected(diag: List[str]) -> Optional[Reading]:
    """Listen once for the controller's battery report; None if it is silent."""
    if hid is None:
        return None
    try:
        infos = hidlist.enumerate(VID)
    except Exception as e:  # pragma: no cover
        log.warning("hid.enumerate(8bitdo ultimate2): %s", e)
        return None
    candidates = [d for d in infos
                  if d.get("product_id") == PID
                  and (d.get("usage_page"), d.get("usage")) == (PAGE, USAGE)]
    for d in candidates:
        dev = hid.device()
        try:
            dev.open_path(d["path"])
        except (OSError, IOError) as e:
            diag.append(f"    [8BitDo Ultimate 2] open: {e}")
            continue
        try:
            try:
                dev.set_nonblocking(True)
            except Exception:
                pass
            deadline = time.time() + WINDOW
            seen = 0
            while time.time() < deadline and seen < MAX_REPORTS:
                try:
                    data = dev.read(64)
                except (OSError, ValueError) as e:
                    diag.append(f"    [8BitDo Ultimate 2] read: {e}")
                    break
                if not data:
                    time.sleep(0.01)
                    continue
                seen += 1
                got = parse_report(data)
                if got is not None:
                    diag.append(f"    [8BitDo Ultimate 2] report {data[0]:#04x} "
                                f"len={len(data)}: {hexdump(data, 32)} -> {got.level}%"
                                f"{' charging' if got.charging else ''}")
                    return got
            if not seen:
                diag.append("    [8BitDo Ultimate 2] nothing received: asleep, or "
                            "the receiver does not forward the vendor report")
            else:
                diag.append(f"    [8BitDo Ultimate 2] {seen} report(s), no level in "
                            f"byte {BATTERY_BYTE}")
            return None
        finally:
            try:
                dev.close()
            except Exception:
                pass
    diag.append("    [8BitDo Ultimate 2] no 0xFF7A:0x0001 vendor collection")
    return None
