import io

import pytest
from PIL import Image

from fake_kraken import FakeKraken
from kraken_tool import protocol as P
from kraken_tool.device import KrakenLcd
from kraken_tool.images import PATTERNS, encode_rgba, gif_frame_report, make_pattern, sanitize_gif
from kraken_tool.transport import TransportError


def B(i, start, size, asset=None):
    return P.Bucket(index=i, occupied=True, asset=asset if asset is not None else i + 1,
                    start_kb=start, size_kb=size)


# ------------------------------------------------------------------ protocol

def test_packets_are_64_bytes_and_encode_little_endian():
    p = P.req_create_bucket(3, 0x1234, 0x0201)
    assert len(p) == 64
    assert list(p[:9]) == [0x32, 0x01, 3, 4, 0x34, 0x12, 0x01, 0x02, 0x01]
    assert list(P.req_set_lcd(55, 270)[:8]) == [0x30, 0x02, 0x01, 55, 0, 0, 1, 3]


def test_packet_validation():
    with pytest.raises(ValueError):
        P.req_query_bucket(16)
    with pytest.raises(ValueError):
        P.req_create_bucket(0, P.LCD_TOTAL_MEMORY_KB - 1, 2)
    with pytest.raises(ValueError):
        P.req_set_lcd(101, 0)
    with pytest.raises(ValueError):
        P.req_set_lcd(50, 45)


def test_bulk_header():
    h = P.bulk_header(P.DATA_TYPE_GIF, 0x01020304)
    assert h[:12] == P.BULK_MAGIC
    assert list(h[12:]) == [1, 0, 0, 0, 4, 3, 2, 1]


def test_analyse_detects_overlap_and_overflow():
    f = P.analyse_buckets([B(0, 0, 100), B(1, 50, 100), B(2, 24300, 100)])
    msgs = " ".join(x.message for x in f if x.severity == "error")
    assert "buckets 0 and 1 overlap at 50-100" in msgs
    assert "bucket 2" in msgs and "past the end" in msgs


def test_analyse_clean_and_empty():
    assert P.analyse_buckets([]) [0].severity == "ok"
    f = P.analyse_buckets([B(0, 0, 100), B(1, 100, 50)])
    assert [x.severity for x in f] == ["ok"]


def test_bucket_size_counts_header():
    assert P.bucket_size_kb(1024 - 20) == 1
    assert P.bucket_size_kb(1024 - 19) == 2


# -------------------------------------------------------------------- device

def test_info_skips_unsolicited_status_reports():
    fake = FakeKraken(fw=(2, 1, 0), brightness=40, orientation=2)
    dev = KrakenLcd(fake)
    assert str(dev.firmware()) == "2.1.0"
    lcd = dev.lcd_info()
    assert (lcd.brightness, lcd.orientation_deg) == (40, 180)


def test_wipe_clears_overlapping_table():
    fake = FakeKraken()
    fake.buckets = {0: (1, 0, 9000), 1: (2, 4000, 9000), 5: (6, 20000, 8000)}
    dev = KrakenLcd(fake)
    assert any(f.severity == "error" for f in P.analyse_buckets(dev.buckets()))
    after = dev.wipe()
    assert not any(b.occupied for b in after)
    assert fake.mode[0] == P.LCD_MODE_LIQUID


def test_upload_places_asset_at_zero_and_displays_it():
    fake = FakeKraken()
    fake.buckets = {0: (1, 0, 9000), 1: (2, 4000, 9000)}
    dev = KrakenLcd(fake)
    payload = encode_rgba(make_pattern("grid", (640, 640)), (640, 640))
    dev.upload(P.DATA_TYPE_RGBA, payload)
    assert fake.buckets == {0: (1, 0, P.bucket_size_kb(len(payload)))}
    assert fake.mode == (P.LCD_MODE_BUCKET, 0)
    blob = fake.written[0]
    assert blob[:20] == P.bulk_header(P.DATA_TYPE_RGBA, len(payload))
    assert blob[20:] == payload
    assert P.analyse_buckets(dev.buckets())[0].severity == "ok"


def test_upload_refuses_when_bucket_cannot_be_deleted():
    fake = FakeKraken()
    fake.buckets = {3: (4, 0, 10)}
    fake.stuck = {3}
    with pytest.raises(TransportError, match="could not be deleted"):
        KrakenLcd(fake).upload(P.DATA_TYPE_RGBA, b"\0" * 4096)


def test_upload_rejects_oversized_asset():
    with pytest.raises(ValueError):
        KrakenLcd(FakeKraken()).upload(P.DATA_TYPE_GIF, b"\0" * (P.LCD_TOTAL_MEMORY_KB * 1024))


def test_missing_reply_raises():
    fake = FakeKraken()
    fake.write = lambda r: None  # device goes silent
    with pytest.raises(TransportError):
        KrakenLcd(fake).lcd_info()


# -------------------------------------------------------------------- images

def test_encode_rgba_layout():
    img = Image.new("RGB", (640, 640), (10, 20, 30))
    data = encode_rgba(img, (640, 640))
    assert len(data) == 640 * 640 * 4
    assert data[:8] == bytes([10, 20, 30, 0, 10, 20, 30, 0])


@pytest.mark.parametrize("name", list(PATTERNS))
def test_patterns_render(name):
    assert make_pattern(name, (64, 64)).size == (64, 64)


def _delta_gif():
    """A GIF that uses small, transparent delta frames (what breaks LCD decoders)."""
    base = Image.new("RGB", (100, 100), "white")
    frames = []
    for i in range(4):
        f = base.copy()
        f.paste((255, 0, 0), (i * 20, 40, i * 20 + 20, 60))
        frames.append(f)
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], optimize=True,
                   duration=80, loop=0, disposal=1)
    return buf.getvalue()


def test_sanitize_gif_produces_full_opaque_frames():
    raw = _delta_gif()
    assert not all(r["full_canvas"] for r in gif_frame_report(raw))
    clean = sanitize_gif(io.BytesIO(raw), (64, 64))
    rows = gif_frame_report(clean)
    assert len(rows) == 4
    for r in rows:
        assert r["full_canvas"], r
        assert not r["transparency"], r
    # content preserved: red block moves across frames
    img = Image.open(io.BytesIO(clean))
    img.seek(3)
    assert img.convert("RGB").getpixel((56, 32))[0] > 200
