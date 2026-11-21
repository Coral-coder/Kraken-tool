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


def _cycle_env(monkeypatch, answers):
    import kraken_tool.transport as T
    from kraken_tool import cycle as C

    fake = FakeKraken(brightness=70, orientation=1)
    _patch(monkeypatch, fake)
    entry = T.DeviceEntry(0x300C, "Fake", (640, 640), "SER1", b"p")
    monkeypatch.setattr(T, "enumerate_devices", lambda: [entry])
    resets = []
    monkeypatch.setattr(C, "usb_reset", lambda pid, ser: resets.append((pid, ser)) or "USB port reset")
    monkeypatch.setattr(C, "wait_for_device", lambda *a, **k: entry)
    monkeypatch.setattr(C, "_is_windows_admin", lambda: True)
    monkeypatch.setattr("time.sleep", lambda s: None)
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda *_: next(it))
    return fake, resets


def test_cycle_runs_rounds_then_asks(monkeypatch, capsys):
    fake, resets = _cycle_env(monkeypatch, ["y"])
    assert cli.main(["cycle"]) == 0
    assert len(resets) == 10
    out = capsys.readouterr().out
    assert "round 10/10" in out and "Fixed after 10 rounds" in out
    # brightness and orientation restored, built-in screen shown
    assert (fake.brightness, fake.orientation) == (70, 1)
    assert fake.mode[0] == P.LCD_MODE_LIQUID
    # each round rotated away and back with the backlight off
    sets = [r for r in fake.log if r[:2] == b"\x30\x02"]
    assert len(sets) == 30 and sets[0][3] == 0 and sets[0][7] == 2


def test_cycle_gives_up_after_max_batches(monkeypatch, capsys):
    fake, resets = _cycle_env(monkeypatch, ["n", "n"])
    assert cli.main(["cycle", "--rounds", "3", "--max-batches", "2"]) == 3
    assert len(resets) == 6
    assert "Still corrupted after 6 rounds" in capsys.readouterr().out


def test_cycle_without_usb_reset(monkeypatch):
    fake, resets = _cycle_env(monkeypatch, ["y"])
    assert cli.main(["cycle", "--no-usb-reset", "--rounds", "2"]) == 0
    assert resets == []


def _fix_env(monkeypatch, admin=True, cam=("C:/Program Files/NZXT CAM/NZXT CAM.exe",)):
    from kraken_tool import autofix as A

    fake, resets = _cycle_env(monkeypatch, [])
    events = []
    monkeypatch.setattr(A, "is_admin", lambda: admin)
    monkeypatch.setattr(A, "find_cam", lambda: list(cam))
    monkeypatch.setattr(A, "close_cam", lambda: events.append("close"))
    monkeypatch.setattr(A, "reopen_cam", lambda p: events.append(("reopen", p)))
    monkeypatch.setattr(A, "relaunch_elevated", lambda a: events.append(("elevate", a)) or True)
    monkeypatch.setattr(A, "timed_pause", lambda *a: events.append("pause"))
    monkeypatch.setattr("builtins.input", lambda *_: (_ for _ in ()).throw(AssertionError("asked")))
    return fake, resets, events


def test_bare_launch_runs_fix_without_questions(monkeypatch, capsys):
    fake, resets, events = _fix_env(monkeypatch)
    assert cli.main([]) == 0
    assert len(resets) == 10
    assert events[0] == "close" and events[1][0] == "reopen" and events[-1] == "pause"
    assert (fake.brightness, fake.orientation) == (70, 1)
    assert "round 10/10" in capsys.readouterr().out


def test_fix_elevates_on_windows_when_not_admin(monkeypatch):
    fake, resets, events = _fix_env(monkeypatch, admin=False)
    monkeypatch.setattr(cli.sys, "platform", "win32")
    assert cli.main(["fix", "--pause"]) == 0
    assert events == [("elevate", ["fix", "--rounds", "10", "--pause"])]
    assert resets == []  # work happens in the elevated copy


def test_fix_reopens_cam_even_on_error(monkeypatch, capsys):
    from kraken_tool import cycle as C

    fake, resets, events = _fix_env(monkeypatch)
    monkeypatch.setattr(C, "run_rounds", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert cli.main(["fix"]) == 1
    assert ("reopen", ["C:/Program Files/NZXT CAM/NZXT CAM.exe"]) in events
    assert "boom" in capsys.readouterr().out


def test_fix_without_cam_running(monkeypatch):
    fake, resets, events = _fix_env(monkeypatch, cam=())
    assert cli.main(["fix", "--rounds", "2"]) == 0
    assert events == [] and len(resets) == 2
