"""Wire protocol for the NZXT Kraken Elite (2023 / 2024 RGB) LCD controller.

Everything in this module is pure (no I/O) so it can be unit tested.

Protocol references: liquidctl's ``kraken3`` driver and its
``docs/developer/protocol/nzxt_x3-z3-2023.md`` (GPL-3.0), cross-checked
against captured CAM traffic documented there.  Only commands that are
publicly documented are used here; nothing in this tool writes to the
controller's *program* flash.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

NZXT_VID = 0x1E71

# product id -> (name, lcd resolution)
SUPPORTED_DEVICES = {
    0x300C: ("NZXT Kraken Elite (2023)", (640, 640)),
    0x3012: ("NZXT Kraken Elite RGB (2024)", (640, 640)),
    # 240x240 siblings share the protocol; supported on a best-effort basis
    0x300E: ("NZXT Kraken (2023)", (240, 240)),
    0x3014: ("NZXT Kraken Plus (2024)", (240, 240)),
}

HID_REPORT_LENGTH = 64
BULK_OUT_ENDPOINT = 0x02

# LCD asset memory, in KiB (1 "unit" of bucket address/size == 1024 bytes)
LCD_TOTAL_MEMORY_KB = 24320
BUCKET_COUNT = 16

BULK_MAGIC = bytes([0x12, 0xFA, 0x01, 0xE8, 0xAB, 0xCD, 0xEF, 0x98, 0x76, 0x54, 0x32, 0x10])

DATA_TYPE_GIF = 0x01
DATA_TYPE_RGBA = 0x02

LCD_MODE_LIQUID = 0x02
LCD_MODE_BUCKET = 0x04

ORIENTATIONS = (0, 90, 180, 270)


def packet(*payload: int) -> bytes:
    """Build a 64 byte HID output report.  Byte 0 is the command/report id."""
    data = bytes(payload)
    if len(data) > HID_REPORT_LENGTH:
        raise ValueError("payload too long")
    return data + bytes(HID_REPORT_LENGTH - len(data))


# ----------------------------------------------------------------- requests

def req_firmware_info() -> bytes:
    return packet(0x10, 0x01)


def req_lcd_info() -> bytes:
    return packet(0x30, 0x01)


def req_set_lcd(brightness: int, orientation_deg: int) -> bytes:
    if not 0 <= brightness <= 100:
        raise ValueError("brightness must be 0-100")
    if orientation_deg not in ORIENTATIONS:
        raise ValueError(f"orientation must be one of {ORIENTATIONS}")
    return packet(0x30, 0x02, 0x01, brightness, 0x00, 0x00, 0x01, orientation_deg // 90)


def req_query_bucket(index: int) -> bytes:
    _check_bucket(index)
    return packet(0x30, 0x04, index)


def req_create_bucket(index: int, start_kb: int, size_kb: int) -> bytes:
    _check_bucket(index)
    if start_kb < 0 or size_kb <= 0 or start_kb + size_kb > LCD_TOTAL_MEMORY_KB:
        raise ValueError(
            f"bucket range {start_kb}+{size_kb} KiB outside LCD memory ({LCD_TOTAL_MEMORY_KB} KiB)"
        )
    s = start_kb.to_bytes(2, "little")
    z = size_kb.to_bytes(2, "little")
    return packet(0x32, 0x01, index, index + 1, s[0], s[1], z[0], z[1], 0x01)


def req_delete_bucket(index: int) -> bytes:
    _check_bucket(index)
    return packet(0x32, 0x02, index)


def req_start_transfer(index: int) -> bytes:
    _check_bucket(index)
    return packet(0x36, 0x01, index)


def req_end_transfer(index: int) -> bytes:
    _check_bucket(index)
    return packet(0x36, 0x02, index)


def req_cancel_transfers() -> bytes:
    return packet(0x36, 0x03)


def req_set_mode(mode: int, bucket: int = 0) -> bytes:
    return packet(0x38, 0x01, mode, bucket)


def bulk_header(data_type: int, length: int) -> bytes:
    return BULK_MAGIC + bytes([data_type, 0, 0, 0]) + length.to_bytes(4, "little")


def _check_bucket(index: int) -> None:
    if not 0 <= index < BUCKET_COUNT:
        raise ValueError(f"bucket index must be 0-{BUCKET_COUNT - 1}")


# ---------------------------------------------------------------- responses

def reply_prefix(request: bytes) -> int:
    """Replies echo the request command with the low bit set (0x30 -> 0x31)."""
    return request[0] + 1


def reply_ok(msg: bytes) -> bool:
    return len(msg) > 14 and msg[14] == 0x01


@dataclass(frozen=True)
class FirmwareInfo:
    major: int
    minor: int
    patch: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


def parse_firmware_info(msg: bytes) -> FirmwareInfo:
    if msg[:2] != b"\x11\x01":
        raise ValueError("not a firmware info reply")
    return FirmwareInfo(msg[0x11], msg[0x12], msg[0x13])


@dataclass(frozen=True)
class LcdInfo:
    brightness: int
    orientation_deg: int


def parse_lcd_info(msg: bytes) -> LcdInfo:
    if msg[:2] != b"\x31\x01":
        raise ValueError("not an LCD info reply")
    orient = msg[0x1A]
    return LcdInfo(brightness=msg[0x18], orientation_deg=ORIENTATIONS[orient % 4] if orient < 4 else -1)


@dataclass(frozen=True)
class Bucket:
    index: int
    occupied: bool
    asset: int = 0
    start_kb: int = 0
    size_kb: int = 0
    raw: bytes = b""

    @property
    def end_kb(self) -> int:
        return self.start_kb + self.size_kb


def parse_bucket(index: int, msg: bytes) -> Bucket:
    if msg[:2] != b"\x31\x04":
        raise ValueError("not a bucket info reply")
    occupied = any(msg[15:])
    if not occupied:
        return Bucket(index=index, occupied=False, raw=bytes(msg))
    return Bucket(
        index=index,
        occupied=True,
        asset=msg[15],
        start_kb=int.from_bytes(msg[17:19], "little"),
        size_kb=int.from_bytes(msg[19:21], "little"),
        raw=bytes(msg),
    )


# ------------------------------------------------------------- diagnostics

@dataclass(frozen=True)
class Finding:
    severity: str  # "ok" | "warn" | "error"
    message: str


def analyse_buckets(buckets: Iterable[Bucket]) -> list[Finding]:
    """Look for the bucket-table damage that produces corrupted/ghosted images."""
    used = [b for b in buckets if b.occupied]
    findings: list[Finding] = []
    if not used:
        return [Finding("ok", "LCD asset memory is empty (no stored images).")]

    for b in used:
        if b.size_kb == 0:
            findings.append(Finding("error", f"bucket {b.index}: occupied but size is 0 KiB"))
        if b.end_kb > LCD_TOTAL_MEMORY_KB:
            findings.append(
                Finding(
                    "error",
                    f"bucket {b.index}: range {b.start_kb}-{b.end_kb} KiB runs past the end of "
                    f"LCD memory ({LCD_TOTAL_MEMORY_KB} KiB)",
                )
            )
        if b.asset != b.index + 1:
            findings.append(
                Finding("warn", f"bucket {b.index}: unusual asset id {b.asset} (expected {b.index + 1})")
            )

    for i, a in enumerate(used):
        for b in used[i + 1 :]:
            if a.size_kb and b.size_kb and a.start_kb < b.end_kb and b.start_kb < a.end_kb:
                lo, hi = max(a.start_kb, b.start_kb), min(a.end_kb, b.end_kb)
                findings.append(
                    Finding(
                        "error",
                        f"buckets {a.index} and {b.index} overlap at {lo}-{hi} KiB: writing one "
                        f"silently corrupts the other",
                    )
                )

    if not any(f.severity == "error" for f in findings):
        total = sum(b.size_kb for b in used)
        findings.append(
            Finding("ok", f"{len(used)} stored asset(s), {total} KiB used, no overlaps detected.")
        )
    return findings


def bucket_size_kb(payload_len: int) -> int:
    """KiB needed for header + payload, as the firmware counts it."""
    return -(-(len(BULK_MAGIC) + 8 + payload_len) // 1024)


def find_reply(messages: Iterable[bytes], prefix: int, sub: Optional[int] = None) -> Optional[bytes]:
    for m in messages:
        if m and m[0] == prefix and (sub is None or m[1] == sub):
            return m
    return None
