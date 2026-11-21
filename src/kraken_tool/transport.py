"""USB transport: HID for commands, a vendor bulk OUT endpoint for image data.

* HID  -> ``hidapi`` (``pip install hidapi``; imports as ``hid``)
* bulk -> ``pyusb`` with libusb.  On Windows the ``libusb-package`` wheel
  ships the libusb DLL; the Kraken's bulk interface already uses the WinUSB
  driver, which libusb can drive without Zadig.
"""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass
from typing import Optional, Protocol

from . import protocol as P

log = logging.getLogger(__name__)


class TransportError(RuntimeError):
    pass


class Transport(Protocol):
    def write(self, report: bytes) -> None: ...
    def read(self, timeout_ms: int) -> Optional[bytes]: ...
    def bulk_write(self, data: bytes) -> None: ...
    def close(self) -> None: ...


@dataclass
class DeviceEntry:
    product_id: int
    name: str
    resolution: tuple
    serial: str
    hid_path: bytes


def enumerate_devices() -> list[DeviceEntry]:
    hid = _import_hid()
    found: dict[str, DeviceEntry] = {}
    for d in hid.enumerate(P.NZXT_VID, 0):
        pid = d["product_id"]
        if pid not in P.SUPPORTED_DEVICES:
            continue
        serial = d.get("serial_number") or ""
        if serial in found:
            continue
        name, res = P.SUPPORTED_DEVICES[pid]
        found[serial] = DeviceEntry(pid, name, res, serial, d["path"])
    return list(found.values())


def _import_hid():
    try:
        import hid  # type: ignore
    except ImportError as e:  # pragma: no cover - environment dependent
        raise TransportError("hidapi is not installed: pip install hidapi") from e
    return hid


def _usb_backend():
    import usb.backend.libusb1  # type: ignore

    backend = None
    if sys.platform in ("win32", "cygwin"):
        try:
            import libusb_package  # type: ignore

            backend = libusb_package.get_libusb1_backend()
        except ImportError:
            pass
    return backend or usb.backend.libusb1.get_backend()


class UsbTransport:
    def __init__(self, entry: DeviceEntry):
        self.entry = entry
        hid = _import_hid()
        self._hid = hid.device()
        try:
            self._hid.open_path(entry.hid_path)
        except (IOError, OSError) as e:
            raise TransportError(
                f"could not open HID interface ({e}). Close NZXT CAM (including the tray icon) "
                "and, on Linux, install the udev rule from packaging/ or run as root."
            ) from e
        self._hid.set_nonblocking(False)
        self._usb = None
        self._intf = None
        self._detached = False

    # ---------------------------------------------------------------- HID
    def write(self, report: bytes) -> None:
        log.debug("HID > %s", report[:16].hex(" "))
        n = self._hid.write(list(report))
        if n < 0:
            raise TransportError("HID write failed")

    def read(self, timeout_ms: int) -> Optional[bytes]:
        data = self._hid.read(P.HID_REPORT_LENGTH, timeout_ms)
        if not data:
            return None
        data = bytes(data)
        log.debug("HID < %s", data[:24].hex(" "))
        return data

    # --------------------------------------------------------------- bulk
    def _open_bulk(self) -> None:
        if self._usb is not None:
            return
        try:
            import usb.core  # type: ignore
            import usb.util  # type: ignore
        except ImportError as e:  # pragma: no cover
            raise TransportError("pyusb is not installed: pip install pyusb") from e

        backend = _usb_backend()
        if backend is None:
            raise TransportError("libusb not found (Windows: pip install libusb-package)")
        devs = list(
            usb.core.find(
                find_all=True,
                idVendor=P.NZXT_VID,
                idProduct=self.entry.product_id,
                backend=backend,
            )
        )
        if not devs:
            raise TransportError("bulk USB device not found via libusb")
        dev = devs[0]
        if len(devs) > 1 and self.entry.serial:
            for d in devs:
                try:
                    if usb.util.get_string(d, d.iSerialNumber) == self.entry.serial:
                        dev = d
                        break
                except Exception:  # noqa: BLE001 - serial read may be denied
                    pass

        try:
            cfg = dev.get_active_configuration()
        except usb.core.USBError:
            dev.set_configuration()
            cfg = dev.get_active_configuration()

        for intf in cfg:
            for ep in intf:
                if ep.bEndpointAddress == P.BULK_OUT_ENDPOINT and (ep.bmAttributes & 0x3) == 0x2:
                    self._intf = intf.bInterfaceNumber
                    break
            if self._intf is not None:
                break
        if self._intf is None:
            raise TransportError("no bulk OUT endpoint 0x02 found on device")

        if sys.platform.startswith("linux"):
            try:
                if dev.is_kernel_driver_active(self._intf):
                    dev.detach_kernel_driver(self._intf)
                    self._detached = True
            except (NotImplementedError, usb.core.USBError):
                pass
        usb.util.claim_interface(dev, self._intf)
        self._usb = dev
        log.debug("bulk interface %d claimed", self._intf)

    def bulk_write(self, data: bytes) -> None:
        self._open_bulk()
        chunk = 1024 * 1024
        for i in range(0, len(data), chunk):
            part = data[i : i + chunk]
            written = self._usb.write(P.BULK_OUT_ENDPOINT, part, timeout=10_000)
            if written != len(part):
                raise TransportError(f"short bulk write ({written}/{len(part)})")

    def close(self) -> None:
        if self._usb is not None:
            import usb.util  # type: ignore

            try:
                usb.util.release_interface(self._usb, self._intf)
                if self._detached:
                    self._usb.attach_kernel_driver(self._intf)
            except Exception:  # noqa: BLE001
                pass
            usb.util.dispose_resources(self._usb)
            self._usb = None
        try:
            self._hid.close()
        except Exception:  # noqa: BLE001
            pass


def drain(t: Transport) -> int:
    n = 0
    while t.read(5) is not None:
        n += 1
        if n > 256:
            break
    return n


def transact(t: Transport, request: bytes, *, timeout_s: float = 2.0, sub: Optional[int] = None) -> bytes:
    """Send ``request`` and wait for its reply, skipping unsolicited status reports."""
    t.write(request)
    want = P.reply_prefix(request)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        msg = t.read(100)
        if msg is None:
            continue
        if msg[0] == want and (sub is None or msg[1] == sub):
            return msg
    raise TransportError(f"no reply to command {request[0]:#04x} {request[1]:#04x}")
