"""Re-run the device-side parts of a warm reboot without rebooting the PC.

A warm reboot of the PC does three things to the Kraken that we can repeat:

1. resets the USB device, so the controller re-enumerates;
2. sends the start-up commands NZXT CAM issues at boot;
3. reconfigures the LCD (brightness/orientation), which forces the display
   path to be set up again from scratch.

Every round does all three. On hardware whose display interface comes up
marginally, each re-initialisation is another chance for it to come up clean.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from typing import Callable, Optional

from . import protocol as P
from .device import KrakenLcd
from .transport import TransportError, transact

log = logging.getLogger(__name__)


def reinit_and_reconfigure(dev: KrakenLcd) -> None:
    """Steps 2 and 3: start-up commands, then a full LCD reconfiguration."""
    # status-report setup and init, as sent by CAM/liquidctl at start-up
    dev.t.write(P.packet(0x70, 0x02, 0x01, 0xB8, 0x01))
    dev.t.write(P.packet(0x70, 0x01))
    dev.firmware()
    lcd = dev.lcd_info()
    bright = lcd.brightness if 0 <= lcd.brightness <= 100 else 80
    orient = lcd.orientation_deg if lcd.orientation_deg in P.ORIENTATIONS else 0
    # rotate through a different orientation with the backlight off, then restore
    transact(dev.t, P.req_set_lcd(0, (orient + 90) % 360))
    time.sleep(0.4)
    transact(dev.t, P.req_set_lcd(0, orient))
    time.sleep(0.2)
    dev.show_liquid()
    transact(dev.t, P.req_set_lcd(bright, orient))


# ------------------------------------------------------------- USB reset

def _is_windows_admin() -> bool:
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def _ps(cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
        capture_output=True, text=True, timeout=60,
    )


def usb_reset(product_id: int, serial: str) -> str:
    """Reset the Kraken's USB device. Returns a short description of what was done."""
    if sys.platform == "win32":
        if not _is_windows_admin():
            raise PermissionError("USB reset needs an Administrator PowerShell")
        pat = f"USB\\VID_{P.NZXT_VID:04X}&PID_{product_id:04X}\\*"
        r = _ps(f"Get-PnpDevice -PresentOnly | Where-Object {{ $_.InstanceId -like '{pat}' }} "
                "| Select-Object -ExpandProperty InstanceId")
        ids = [l.strip() for l in r.stdout.splitlines() if l.strip()]
        if serial:
            ids = [i for i in ids if i.upper().endswith(serial.upper())] or ids
        if not ids:
            raise TransportError("could not find the Kraken's USB device node")
        inst = ids[0]
        r = subprocess.run(["pnputil", "/restart-device", inst], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:  # older Windows: no /restart-device
            r = _ps(f"Disable-PnpDevice -InstanceId '{inst}' -Confirm:$false; Start-Sleep 2; "
                    f"Enable-PnpDevice -InstanceId '{inst}' -Confirm:$false")
            if r.returncode != 0:
                raise TransportError(f"device restart failed: {r.stderr.strip()[:200]}")
        return "USB device restarted"

    import usb.core  # type: ignore

    from .transport import _usb_backend

    dev = usb.core.find(idVendor=P.NZXT_VID, idProduct=product_id, backend=_usb_backend())
    if dev is None:
        raise TransportError("USB device not found for reset")
    dev.reset()
    return "USB port reset"


def wait_for_device(find: Callable[[], list], serial: str, timeout_s: float = 25.0):
    deadline = time.monotonic() + timeout_s
    time.sleep(1.5)
    while time.monotonic() < deadline:
        for d in find():
            if not serial or d.serial == serial:
                return d
        time.sleep(0.5)
    raise TransportError("the Kraken did not come back after the USB reset; reboot the PC")


def run_rounds(rounds: int, open_dev: Callable[[], "object"], reset: Optional[Callable[[], str]],
               on_round: Callable[[int, str], None]) -> list[str]:
    """Run ``rounds`` cycles. ``open_dev`` is a context-manager factory yielding a KrakenLcd."""
    notes = []
    for i in range(1, rounds + 1):
        what = []
        if reset is not None:
            try:
                what.append(reset())
            except PermissionError as e:
                what.append(f"USB reset skipped ({e})")
                reset = None  # don't retry every round
            except (TransportError, OSError, subprocess.SubprocessError) as e:
                what.append(f"USB reset failed ({e})")
        with open_dev() as dev:
            reinit_and_reconfigure(dev)
        what.append("re-initialised + LCD reconfigured")
        msg = "; ".join(what)
        notes.append(msg)
        on_round(i, msg)
    return notes
