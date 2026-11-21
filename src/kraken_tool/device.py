"""High level operations on a Kraken Elite LCD controller."""

from __future__ import annotations

import logging
import time
from typing import Optional

from . import protocol as P
from .transport import Transport, TransportError, drain, transact

log = logging.getLogger(__name__)


class KrakenLcd:
    def __init__(self, transport: Transport, resolution=(640, 640), name: str = "Kraken"):
        self.t = transport
        self.resolution = tuple(resolution)
        self.name = name

    # ------------------------------------------------------------- queries
    def firmware(self) -> P.FirmwareInfo:
        drain(self.t)
        return P.parse_firmware_info(transact(self.t, P.req_firmware_info(), sub=0x01))

    def lcd_info(self) -> P.LcdInfo:
        return P.parse_lcd_info(transact(self.t, P.req_lcd_info(), sub=0x01))

    def buckets(self) -> list[P.Bucket]:
        return [
            P.parse_bucket(i, transact(self.t, P.req_query_bucket(i), sub=0x04))
            for i in range(P.BUCKET_COUNT)
        ]

    def liquid_temperature(self) -> Optional[float]:
        try:
            msg = transact(self.t, P.packet(0x74, 0x01), sub=0x01)
        except TransportError:
            return None
        if msg[15:17] == b"\xff\xff":
            return None
        return msg[15] + msg[16] / 10

    # ------------------------------------------------------------ controls
    def set_lcd(self, brightness: Optional[int] = None, orientation: Optional[int] = None) -> None:
        cur = self.lcd_info()
        b = cur.brightness if brightness is None else brightness
        o = cur.orientation_deg if orientation is None else orientation
        if o not in P.ORIENTATIONS:
            o = 0
        transact(self.t, P.req_set_lcd(b, o))

    def show_liquid(self) -> bool:
        return P.reply_ok(transact(self.t, P.req_set_mode(P.LCD_MODE_LIQUID, 0)))

    def show_bucket(self, index: int) -> bool:
        return P.reply_ok(transact(self.t, P.req_set_mode(P.LCD_MODE_BUCKET, index)))

    def cancel_transfers(self) -> None:
        try:
            transact(self.t, P.req_cancel_transfers(), timeout_s=1.0)
        except TransportError:
            log.debug("no reply to cancel-transfers (harmless)")

    def delete_bucket(self, index: int) -> bool:
        return P.reply_ok(transact(self.t, P.req_delete_bucket(index), sub=0x02))

    # -------------------------------------------------------------- repair
    def wipe(self, passes: int = 2) -> list[P.Bucket]:
        """Switch to liquid mode, abort transfers and delete every bucket.

        Returns the bucket table read back afterwards.
        """
        self.cancel_transfers()
        self.show_liquid()
        for _ in range(passes):
            for i in range(P.BUCKET_COUNT):
                try:
                    self.delete_bucket(i)
                except TransportError as e:
                    log.warning("delete bucket %d: %s", i, e)
            time.sleep(0.2)
        return self.buckets()

    def upload(self, data_type: int, payload: bytes, *, bucket: int = 0, wipe_first: bool = True,
               start_kb: int = 0, show: bool = True, progress=None) -> None:
        """Write one asset into a clean, non-overlapping bucket and display it.

        Unlike incremental allocators, this always starts from an empty table
        and places the asset at offset 0, so overlap is impossible.  With
        ``wipe_first=False`` only the target bucket is replaced; the caller is
        responsible for choosing a ``start_kb`` that does not overlap others.
        """
        size_kb = P.bucket_size_kb(len(payload))
        if size_kb > P.LCD_TOTAL_MEMORY_KB:
            raise ValueError(
                f"asset is {size_kb} KiB, LCD memory is {P.LCD_TOTAL_MEMORY_KB} KiB; "
                "use a shorter/smaller GIF"
            )
        if wipe_first:
            left = [b for b in self.wipe() if b.occupied]
            if left:
                raise TransportError(
                    f"buckets {[b.index for b in left]} could not be deleted; power-cycle the "
                    "pump (unplug SATA/USB for 30s) and retry"
                )
        else:
            self.cancel_transfers()
            if not self.delete_bucket(bucket):
                # the controller refuses to delete the bucket it is displaying
                self.show_liquid()
                if not self.delete_bucket(bucket) and self.buckets()[bucket].occupied:
                    raise TransportError(f"controller refused to delete bucket {bucket}")

        if not P.reply_ok(transact(self.t, P.req_create_bucket(bucket, start_kb, size_kb))):
            raise TransportError("controller refused to create bucket")

        if not P.reply_ok(transact(self.t, P.req_start_transfer(bucket), sub=0x01)):
            log.warning("start-transfer not acknowledged; continuing")
        self.t.bulk_write(P.bulk_header(data_type, len(payload)))
        step = 1024 * 1024
        for i in range(0, len(payload), step):
            self.t.bulk_write(payload[i : i + step])
            if progress:
                progress(min(i + step, len(payload)), len(payload))
        try:
            transact(self.t, P.req_end_transfer(bucket), sub=0x02, timeout_s=5.0)
        except TransportError:
            log.warning("end-transfer not acknowledged; verifying via bucket table")

        b = self.buckets()[bucket]
        if not b.occupied:
            raise TransportError(f"post-upload verification failed: bucket {bucket} is empty")
        if b.start_kb != start_kb or b.size_kb < size_kb:
            log.warning("controller relocated asset: %s (requested %d+%d KiB)", b, start_kb, size_kb)

        if show and not self.show_bucket(bucket):
            raise TransportError("controller refused to display the uploaded asset")
