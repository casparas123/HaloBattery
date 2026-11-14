"""Lamzu Tachi over its 8K receiver (#262).

The receiver (37b0:000c) publishes `ffa0:0001` plus `ffff:0001` on interface 1 and
`ffff:0000` on interface 2 - the same collection layout as the Glorious Model O
Wireless, which is a Sinowealth design read with the framed feature query implemented in
providers/glorious.py:

    request  feature report, report id 0: 00 | 00 00 02 02 00 83 | 00 ...
    wait     ~100 ms (USB -> 2.4 GHz -> mouse -> back)
    reply    [1] status A1 awake / A2 waking / A4 asleep
             [6] 0x83, [7] charging (0 = no), [8] level %

Community Lamzu tools group the 37b0 receivers with the Maya X's Aurora family, and
the Tachi publishes the same `ffff:0000` collection on interface 2. The provider
therefore tries the Aurora 64-byte feature query (`00 00 02 02 00 83`) on that
collection first, then falls back to the Sinowealth layout on the other vendor
collections. Both are read-only.

Registers are only ever read. Unverified on hardware; test build 2 asks the
reporter to run it with diagnostics if neither query answers.
"""
from __future__ import annotations

import time
from typing import List, Optional, Tuple

from . import hidlist
from .base import DeviceStatus, Provider, hexdump, log
from .glorious import feature_length, make_request, parse_reply, pick
from .wlmouse import QUERY as AURORA_QUERY, parse_feature

try:
    import hid  # type: ignore
except Exception:  # pragma: no cover - only when the package is missing
    hid = None

VID = 0x37B0
PID = 0x000C
NAME = "Lamzu Tachi"
KEY = f"lamzu:{PID:04x}"
# #262/fork #34: collection layout alone does not establish this query.
# Confirmed Glorious and other Lamzu providers remain independent.
ENABLED_PIDS = frozenset({PID})   # test build: the Tachi is the target

RF_DELAY_S = 0.10
READ_ATTEMPTS = 2
AURORA_USAGE = (0xFFFF, 0x0000)
AURORA_LENGTH = 65
ASLEEP_KEEP = 300


class LamzuSinowealthProvider(Provider):
    name = "lamzu_sw"

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
            log.warning("hid.enumerate(lamzu 8k): %s", e)
            return []
        mine = [d for d in infos if d.get("product_id") == PID]
        if not mine:
            return []
        if PID not in ENABLED_PIDS:
            self._diag.append(f"[Lamzu] pid={PID:04x}: release hold; not opening")
            return []
        self._diag.append(f"[Lamzu] pid={PID:04x} '{NAME}'")
        parsed = None
        aurora = next((d for d in mine
                       if (d.get("usage_page"), d.get("usage")) == AURORA_USAGE
                       and d.get("interface_number") == 2), None)
        if aurora is not None:
            self._diag.append(f"  aurora iface={aurora.get('interface_number')} "
                              f"ffff:0000")
            parsed = self._ask_aurora(aurora["path"])
        sinowealth = [d for d in mine
                      if (d.get("usage_page"), d.get("usage")) != AURORA_USAGE]
        for d in ([] if parsed is not None else pick(sinowealth)):
            length = feature_length(d["path"]) or 65
            self._diag.append(f"  trying iface={d.get('interface_number')} "
                              f"{d.get('usage_page', 0):04x}:{d.get('usage', 0):04x} "
                              f"feature={length}")
            parsed = self._ask(d["path"], length)
            if parsed is not None:
                break
        if parsed is None:
            self._diag.append("  no battery answer (asleep, off, or not this collection)")
            if self._last and time.time() - self._last[1] < ASLEEP_KEEP:
                return [DeviceStatus(KEY, NAME, self._last[0], False, False, "lamzu_sw",
                                     kind="mouse")]
            return [DeviceStatus(KEY, NAME, None, False, False, "lamzu_sw", kind="mouse")]
        level, charging = parsed
        self._last = (level, time.time())
        self._diag.append(f"  -> {level}%{' charging' if charging else ''}")
        return [DeviceStatus(KEY, NAME, level, charging, True, "lamzu_sw", kind="mouse")]

    def _ask_aurora(self, path) -> Optional[Tuple[int, bool]]:
        """The Maya X / Aurora 64-byte feature query, used by the 37b0 family.

        Community Lamzu tools treat 37b0 receivers as Aurora devices; the Tachi
        publishes the same ffff:0000 collection on interface 2 as the Maya X.
        Read-only, and tried before the Sinowealth layout because the layout
        alone did not answer on the test build.
        """
        dev = hid.device()  # type: ignore[union-attr]
        try:
            dev.open_path(path)
        except (OSError, IOError) as e:
            self._diag.append(f"    aurora open: {e}")
            return None
        try:
            report = [0x00] + AURORA_QUERY + [0x00] * (64 - len(AURORA_QUERY))
            sent = dev.send_feature_report(report)
            if sent is not None and sent < 0:
                self._diag.append(f"    aurora feature write refused ({sent})")
                return None
            time.sleep(0.05)
            for length in (AURORA_LENGTH, 64):
                try:
                    r = dev.get_feature_report(0, length)
                except (OSError, IOError, ValueError):
                    r = None
                if r:
                    self._diag.append(f"    aurora reply: {hexdump(r, 12)}")
                level, charging = parse_feature(r)
                if level is not None:
                    return level, bool(charging)
            self._diag.append("    aurora: no 0x83 reply")
            return None
        except (OSError, IOError, ValueError) as e:
            self._diag.append(f"    aurora error: {e}")
            return None
        finally:
            try:
                dev.close()
            except Exception:
                pass

    def _ask(self, path, length: int) -> Optional[Tuple[int, bool]]:
        dev = hid.device()  # type: ignore[union-attr]
        try:
            dev.open_path(path)
        except (OSError, IOError) as e:
            self._diag.append(f"    open: {e}")
            return None
        try:
            for _ in range(READ_ATTEMPTS):
                sent = dev.send_feature_report(bytes(make_request(length)))
                if sent is not None and sent < 0:
                    self._diag.append(f"    feature write refused ({sent})")
                    return None
                time.sleep(RF_DELAY_S)
                r = dev.get_feature_report(0x00, length)
                if r:
                    self._diag.append(f"    reply: {hexdump(r, 12)}")
                parsed = parse_reply(r)
                if parsed is not None:
                    return parsed
        except (OSError, IOError, ValueError) as e:
            self._diag.append(f"    error: {e}")
        finally:
            try:
                dev.close()
            except Exception:
                pass
        return None

    def diagnostics(self) -> List[str]:
        return list(self._diag)
