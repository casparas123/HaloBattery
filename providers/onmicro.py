"""OnMicro HS6209 2.4 GHz dongle (32c2:0012), #24.

The RAPIQUE mouse's receiver enumerates as ``32c2:0012`` "HS6209 2.4G Wireless
Receiver" and exposes the OnMicro vendor page on interface 1:

    32c2:0012 if=1  ffb5:0001   vendor status
    32c2:0012 if=1  ffbc:0088   second vendor page (not used here)

``ffb5:0001`` carries an 8-byte status command. The frame, report id and field
layout are mirrored from ksanjeev284/acer-popgo-companion (MIT), which
reverse-engineered the same vendor page on the sibling dongle ``32c2:0066``:

    request  B5 | 01 00 00 00 00 00 00     report id 0xB5, command 0x01
    reply    B5 | 01 flags pct a b c d     pct = MCU percentage in byte 3

Bytes 5-6 often carry a LiPo voltage in mV (little endian). The reference app
prefers a percentage derived from that voltage, because the MCU percentage has
been seen frozen for long periods; this provider follows it. If no plausible
voltage is present the MCU percentage is used.

No charging bit exists on this channel: the status byte stays 0x01 with the
cable in or out and the charge LED is local to the mouse, so the charger is
reported as unknown rather than guessed.

The product firmware may differ from the reference (PID 0012 vs 0066), so the
diagnostics always log the raw reply - the first report from a real RAPIQUE
settles whether the layout transfers.
"""
from __future__ import annotations

import time
from typing import List, Optional, Tuple

from . import hidlist
from .base import DeviceStatus, Provider, hexdump, log

try:
    import hid  # type: ignore
except Exception:  # pragma: no cover - only when the package is missing
    hid = None

VID = 0x32C2
PID = 0x0012
NAME = "2.4G Wireless Mouse (HS6209)"
KEY = f"onmicro:{PID:04x}"

USAGE_PAGE, USAGE = 0xFFB5, 0x0001          # the vendor status collection, interface 1
REPORT_ID = 0xB5
COMMAND_STATUS = 0x01
REPORT_SIZE = 8

READ_ATTEMPTS = 6
READ_TIMEOUT_MS = 250
ASLEEP_KEEP = 300

VOLTAGE_EMPTY_MV = 3300
VOLTAGE_FULL_MV = 4200


def make_request() -> List[int]:
    """``B5 | 01 00 00 00 00 00 00`` - report id 0xB5, status command, zero padding."""
    return [REPORT_ID, COMMAND_STATUS] + [0x00] * (REPORT_SIZE - 2)


def lipo_mv_to_percent(mv: int) -> int:
    """Single-cell LiPo state of charge, linear between 3.30 V and 4.20 V."""
    if mv <= VOLTAGE_EMPTY_MV:
        return 0
    if mv >= VOLTAGE_FULL_MV:
        return 100
    return int(round((mv - VOLTAGE_EMPTY_MV) * 100 / (VOLTAGE_FULL_MV - VOLTAGE_EMPTY_MV)))


def _payload(r) -> Optional[List[int]]:
    """The reply's payload, with or without the report id that hidapi may keep."""
    if not r:
        return None
    if len(r) >= 4 and r[0] == REPORT_ID and r[1] == COMMAND_STATUS:
        return list(r[2:])
    if len(r) >= 3 and r[0] == COMMAND_STATUS:
        return list(r[1:])
    return None


def parse_reply(r) -> Optional[Tuple[int, Optional[int]]]:
    """(level, voltage_mv) from ``B5 | 01 flags pct a b c d``, or None.

    The voltage is only a hint: a candidate outside 3.0-4.3 V is ignored and the
    MCU percentage is shown instead.
    """
    p = _payload(r)
    if p is None or len(p) < 2:
        return None
    pct = int(p[1])
    voltage: Optional[int] = None
    if len(p) >= 6:
        a, b, c, d = p[2], p[3], p[4], p[5]
        for candidate in (b | (c << 8), (c << 8) | d, c | (d << 8), a | (b << 8)):
            if 3000 <= candidate <= 4300:
                voltage = candidate
                break
    if voltage is not None:
        return lipo_mv_to_percent(voltage), voltage
    if 1 <= pct <= 100:
        return pct, None
    return None


def pick(infos: List[dict]) -> Optional[dict]:
    for d in infos:
        if d.get("usage_page") == USAGE_PAGE and d.get("usage") == USAGE:
            return d
    return None


class OnMicroProvider(Provider):
    name = "onmicro"

    def __init__(self):
        self._diag: List[str] = []
        self._last: Optional[Tuple[int, float]] = None

    def poll(self) -> List[DeviceStatus]:
        self._diag = []
        if hid is None:
            return []
        try:
            infos = hidlist.enumerate(VID)
        except Exception as e:  # pragma: no cover
            log.warning("hid.enumerate(onmicro): %s", e)
            return []
        mine = [d for d in infos if d.get("product_id") == PID]
        if not mine:
            return []
        d = pick(mine)
        if d is None:
            self._diag.append(f"[OnMicro] pid={PID:04x} '{NAME}': no 0xFFB5:0x01 channel")
            return [DeviceStatus(KEY, NAME, None, False, False, "onmicro", kind="mouse",
                                 charging_known=False, wake_on_input=True)]
        self._diag.append(f"[OnMicro] pid={PID:04x} '{NAME}' iface={d.get('interface_number')} "
                          f"{USAGE_PAGE:04x}:{USAGE:04x}")
        parsed = self._ask(d)
        if parsed is not None:
            level, voltage = parsed
            self._last = (level, time.time())
            self._diag.append(f"  -> {level}%" + (f" ({voltage} mV)" if voltage else ""))
            return [DeviceStatus(KEY, NAME, level, False, True, "onmicro", kind="mouse",
                                 charging_known=False)]
        self._diag.append("  no answer (mouse asleep or off)")
        if self._last and time.time() - self._last[1] < ASLEEP_KEEP:
            return [DeviceStatus(KEY, NAME, self._last[0], False, False, "onmicro",
                                 kind="mouse", charging_known=False, wake_on_input=True)]
        return [DeviceStatus(KEY, NAME, None, False, False, "onmicro", kind="mouse",
                             charging_known=False, wake_on_input=True)]

    def _ask(self, d: dict) -> Optional[Tuple[int, Optional[int]]]:
        dev = hid.device()  # type: ignore[union-attr]
        try:
            dev.open_path(d["path"])
        except (OSError, IOError) as e:
            self._diag.append(f"  open: {e}")
            return None
        try:
            sent = dev.write(bytes(make_request()))
            if sent is not None and sent < 0:
                self._diag.append(f"  write refused ({sent})")
                return None
            for _ in range(READ_ATTEMPTS):
                r = dev.read(REPORT_SIZE, READ_TIMEOUT_MS)
                if not r:
                    continue
                self._diag.append(f"  reply: {hexdump(r, 8)}")
                parsed = parse_reply(r)
                if parsed is not None:
                    return parsed
        except (OSError, IOError, ValueError) as e:
            self._diag.append(f"  error: {e}")
        finally:
            try:
                dev.close()
            except Exception:
                pass
        return None

    def diagnostics(self) -> List[str]:
        return list(self._diag)
