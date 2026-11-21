from contextlib import contextmanager

from fake_kraken import FakeKraken
from kraken_tool import cli
from kraken_tool import protocol as P
from kraken_tool.device import KrakenLcd


def _patch(monkeypatch, fake):
    @contextmanager
    def fake_open(args):
        yield KrakenLcd(fake, (640, 640), "Fake Kraken Elite")

    monkeypatch.setattr(cli, "open_device", fake_open)


def test_doctor_then_repair_fixes_corrupt_table(monkeypatch, capsys, tmp_path):
    fake = FakeKraken()
    fake.buckets = {0: (1, 0, 9000), 1: (2, 4000, 9000)}
    _patch(monkeypatch, fake)

    assert cli.main(["doctor"]) == 2
    assert "overlap" in capsys.readouterr().out

    from PIL import Image
    img = tmp_path / "pic.png"
    Image.new("RGB", (800, 600), "teal").save(img)
    assert cli.main(["repair", str(img)]) == 0
    assert fake.mode == (P.LCD_MODE_BUCKET, 0)

    assert cli.main(["doctor"]) == 0
    assert "no overlaps" in capsys.readouterr().out


def test_repair_reports_stuck_bucket(monkeypatch, capsys):
    fake = FakeKraken()
    fake.buckets = {7: (8, 0, 10)}
    fake.stuck = {7}
    _patch(monkeypatch, fake)
    assert cli.main(["repair"]) == 2
    assert "survive deletion" in capsys.readouterr().out


def test_offline_gif_commands(tmp_path, capsys):
    from test_kraken import _delta_gif
    src = tmp_path / "a.gif"
    src.write_bytes(_delta_gif())
    assert cli.main(["inspect-gif", str(src)]) == 0
    assert "ghosting" in capsys.readouterr().out
    out = tmp_path / "b.gif"
    assert cli.main(["sanitize-gif", str(src), str(out), "--size", "64"]) == 0
    capsys.readouterr()
    cli.main(["inspect-gif", str(out)])
    assert "All frames are full-canvas" in capsys.readouterr().out
