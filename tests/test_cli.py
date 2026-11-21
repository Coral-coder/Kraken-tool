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


def test_fingerprint_interactive_run_and_report(monkeypatch, capsys, tmp_path):
    from kraken_tool import fingerprint as F

    monkeypatch.setattr("time.sleep", lambda s: None)
    log = tmp_path / "fp.jsonl"
    # boot 1: odd columns fail; boot 2: even columns fail; boot 3 clean
    boots = [
        {"col1": "s", "col3": "s"},
        {"col0": "n", "col2": "n", "liquid": "s"},
        {},
    ]
    for i, bad in enumerate(boots):
        fake = FakeKraken()
        _patch(monkeypatch, fake)
        answers = iter(bad.get(st.key, "" if st.kind == "tiles" else "y") for st in F.build_plan())
        monkeypatch.setattr("builtins.input", lambda *_: next(answers))
        rc = cli.main(["fingerprint", "--log", str(log), "--note", f"boot {i}"])
        assert rc == (0 if not bad else 3)
        # test assets used 3 non-overlapping buckets, then wiped
        assert fake.buckets == {}
        assert fake.mode[0] == P.LCD_MODE_LIQUID

    out = capsys.readouterr().out
    assert "half-word lane" in out
    cli.main(["fingerprint", "--report", "--log", str(log)])
    rep = capsys.readouterr().out
    assert "3 run(s), 2 with a reproduced fault" in rep
    assert "CHANGES between boots" in rep
    assert "came up clean" in rep
