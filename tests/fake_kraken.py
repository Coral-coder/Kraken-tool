"""A behavioural simulator of the Kraken Elite LCD controller's HID/bulk protocol."""

from __future__ import annotations

from collections import deque

from kraken_tool import protocol as P


class FakeKraken:
    def __init__(self, fw=(1, 2, 1), brightness=80, orientation=0, stuck=()):
        self.fw = fw
        self.brightness = brightness
        self.orientation = orientation
        self.buckets: dict[int, tuple[int, int, int]] = {}  # idx -> (asset, start, size)
        self.mode = (P.LCD_MODE_LIQUID, 0)
        self.stuck = set(stuck)  # buckets that refuse deletion
        self.out: deque[bytes] = deque()
        self.bulk: list[bytes] = []
        self.transfer_bucket = None
        self.written: dict[int, bytes] = {}
        self.log: list[bytes] = []
        self.spam_status = True

    def _reply(self, a, b, ok=None, body=None):
        r = bytearray(64)
        r[0], r[1] = a, b
        if ok is not None:
            r[14] = 1 if ok else 0
        if body:
            for k, v in body.items():
                r[k] = v
        self.out.append(bytes(r))

    # Transport interface
    def write(self, report: bytes) -> None:
        assert len(report) == 64
        self.log.append(report)
        if self.spam_status:  # unsolicited status report, like the real pump
            self._reply(0x75, 0x01)
        c, s = report[0], report[1]
        if (c, s) == (0x10, 0x01):
            self._reply(0x11, 0x01, body={0x11: self.fw[0], 0x12: self.fw[1], 0x13: self.fw[2]})
        elif (c, s) == (0x30, 0x01):
            self._reply(0x31, 0x01, body={0x18: self.brightness, 0x1A: self.orientation})
        elif (c, s) == (0x30, 0x02):
            self.brightness, self.orientation = report[3], report[7]
            self._reply(0x31, 0x02)
        elif (c, s) == (0x30, 0x04):
            i = report[2]
            body = {14: i}
            if i in self.buckets:
                asset, start, size = self.buckets[i]
                body.update({15: asset, 16: 2, 17: start & 0xFF, 18: start >> 8,
                             19: size & 0xFF, 20: size >> 8, 21: 1, 22: 1})
            self._reply(0x31, 0x04, body=body)
        elif (c, s) == (0x32, 0x01):
            i = report[2]
            start = int.from_bytes(report[4:6], "little")
            size = int.from_bytes(report[6:8], "little")
            ok = i not in self.buckets
            if ok:
                self.buckets[i] = (report[3], start, size)
            self._reply(0x33, 0x01, ok=ok)
        elif (c, s) == (0x32, 0x02):
            i = report[2]
            ok = i not in self.stuck
            if ok:
                self.buckets.pop(i, None)
            self._reply(0x33, 0x02, ok=ok)
        elif (c, s) == (0x36, 0x01):
            self.transfer_bucket = report[2]
            self.bulk = []
            self._reply(0x37, 0x01, ok=report[2] in self.buckets)
        elif (c, s) == (0x36, 0x02):
            self.written[self.transfer_bucket] = b"".join(self.bulk)
            self.transfer_bucket = None
            self._reply(0x37, 0x02, ok=True)
        elif (c, s) == (0x36, 0x03):
            self.transfer_bucket = None
            self._reply(0x37, 0x03)
        elif (c, s) == (0x38, 0x01):
            mode, i = report[2], report[3]
            ok = mode == P.LCD_MODE_LIQUID or i in self.written
            if ok:
                self.mode = (mode, i)
            self._reply(0x39, 0x01, ok=ok)

    def read(self, timeout_ms):
        return self.out.popleft() if self.out else None

    def bulk_write(self, data: bytes) -> None:
        assert self.transfer_bucket is not None, "bulk write outside a transfer"
        self.bulk.append(bytes(data))

    def close(self):
        pass
