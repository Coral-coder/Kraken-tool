"""kraken-tool command line interface."""

from __future__ import annotations

import argparse
import logging
import sys
from contextlib import contextmanager
from pathlib import Path

from . import __version__
from . import protocol as P
from .images import PATTERNS, encode_rgba, gif_frame_report, make_pattern, sanitize_gif

SYMBOL = {"ok": "[ OK ]", "warn": "[WARN]", "error": "[FAIL]"}


def _print(msg=""):
    print(msg, flush=True)


@contextmanager
def open_device(args):
    from .device import KrakenLcd
    from .transport import UsbTransport, enumerate_devices

    devs = enumerate_devices()
    if args.serial:
        devs = [d for d in devs if d.serial == args.serial]
    if not devs:
        raise SystemExit(
            "No Kraken Elite found. Check the internal USB cable, quit NZXT CAM completely "
            "(tray icon too), and on Linux install packaging/71-kraken-tool.rules."
        )
    if len(devs) > 1:
        raise SystemExit("Several Krakens found; pick one with --serial (see `kraken-tool list`).")
    d = devs[0]
    t = UsbTransport(d)
    try:
        yield KrakenLcd(t, d.resolution, d.name)
    finally:
        t.close()


def _progress(done, total):
    pct = done * 100 // total
    sys.stdout.write(f"\r  uploading... {pct:3d}%")
    sys.stdout.flush()
    if done >= total:
        sys.stdout.write("\n")


# ------------------------------------------------------------------ commands

def cmd_list(args):
    from .transport import enumerate_devices

    devs = enumerate_devices()
    if not devs:
        _print("No supported NZXT Kraken found.")
        return 1
    for d in devs:
        _print(f"{d.name}  pid={d.product_id:#06x}  lcd={d.resolution[0]}x{d.resolution[1]}  serial={d.serial}")
    return 0


def _report(dev) -> list[P.Finding]:
    fw = dev.firmware()
    lcd = dev.lcd_info()
    buckets = dev.buckets()
    _print(f"Device      : {dev.name}")
    _print(f"Firmware    : {fw}")
    _print(f"LCD         : {dev.resolution[0]}x{dev.resolution[1]}, brightness {lcd.brightness}%, "
           f"orientation {lcd.orientation_deg}°")
    _print("Asset memory:")
    used = [b for b in buckets if b.occupied]
    if not used:
        _print("  (empty)")
    for b in used:
        _print(f"  bucket {b.index:2d}: asset {b.asset:3d}  {b.start_kb:6d}-{b.end_kb:<6d} KiB "
               f"({b.size_kb} KiB)")
    _print(f"  capacity {P.LCD_TOTAL_MEMORY_KB} KiB")
    findings = P.analyse_buckets(buckets)
    if lcd.orientation_deg == -1:
        findings.append(P.Finding("error", "LCD orientation register holds an invalid value"))
    if lcd.brightness > 100:
        findings.append(P.Finding("error", f"LCD brightness register holds invalid value {lcd.brightness}"))
    _print("\nDiagnosis:")
    for f in findings:
        _print(f"  {SYMBOL[f.severity]} {f.message}")
    return findings


def cmd_doctor(args):
    with open_device(args) as dev:
        findings = _report(dev)
    bad = any(f.severity == "error" for f in findings)
    _print()
    if bad:
        _print("The stored image table is damaged. Run `kraken-tool repair` to rebuild it.")
    else:
        _print("Image memory looks consistent. Next step to isolate the fault:")
        _print("  kraken-tool patterns      # cycle hardware test patterns on the panel")
        _print("See README 'Reading the results' to interpret what you see.")
    return 2 if bad else 0


def cmd_wipe(args):
    with open_device(args) as dev:
        left = [b for b in dev.wipe() if b.occupied]
    if left:
        _print(f"Could not clear buckets {[b.index for b in left]}. Power-cycle the pump and retry.")
        return 2
    _print("All stored LCD images deleted; display switched to liquid temperature.")
    return 0


def _upload_pattern(dev, name, orientation):
    img = make_pattern(name, dev.resolution)
    dev.upload(P.DATA_TYPE_RGBA, encode_rgba(img, dev.resolution, 0))


def cmd_pattern(args):
    with open_device(args) as dev:
        _upload_pattern(dev, args.name, 0)
    _print(f"Showing '{args.name}': {PATTERNS[args.name][1]}")
    return 0


def cmd_patterns(args):
    names = args.only or list(PATTERNS)
    with open_device(args) as dev:
        for n in names:
            _upload_pattern(dev, n, 0)
            _print(f"\n[{n}] {PATTERNS[n][1]}")
            try:
                input("  Look at the pump screen, then press Enter for the next pattern (Ctrl+C to stop) ")
            except (KeyboardInterrupt, EOFError):
                _print()
                break
        dev.wipe()
    _print("Done; display returned to liquid temperature.")
    return 0


def _prepare_file(path: Path, resolution, orientation, raw: bool):
    with open(path, "rb") as fh:
        head = fh.read(6)
    if head[:3] == b"GIF":
        data = path.read_bytes() if raw else sanitize_gif(path, resolution, orientation)
        return P.DATA_TYPE_GIF, data
    from PIL import Image

    return P.DATA_TYPE_RGBA, encode_rgba(Image.open(path), resolution, orientation)


def cmd_upload(args):
    path = Path(args.file)
    with open_device(args) as dev:
        orient = dev.lcd_info().orientation_deg
        orient = orient if orient in P.ORIENTATIONS else 0
        kind, data = _prepare_file(path, dev.resolution, orient, args.raw)
        _print(f"Uploading {'GIF' if kind == P.DATA_TYPE_GIF else 'image'} "
               f"({len(data) / 1024:.0f} KiB) to a clean bucket...")
        dev.upload(kind, data, progress=_progress)
    _print("Uploaded and verified.")
    return 0


def cmd_repair(args):
    with open_device(args) as dev:
        _print("== 1/4 current state ==")
        _report(dev)
        _print("\n== 2/4 resetting controller state ==")
        dev.cancel_transfers()
        lcd = dev.lcd_info()
        dev.set_lcd(
            brightness=lcd.brightness if 0 <= lcd.brightness <= 100 else 80,
            orientation=lcd.orientation_deg if lcd.orientation_deg in P.ORIENTATIONS else 0,
        )
        _print("\n== 3/4 wiping LCD asset memory ==")
        left = [b for b in dev.wipe(passes=3) if b.occupied]
        if left:
            _print(f"  {SYMBOL['error']} buckets {[b.index for b in left]} survive deletion.")
            _print("  Power-cycle: shut down, switch the PSU off for 30 s, boot, run repair again.")
            return 2
        _print(f"  {SYMBOL['ok']} asset memory empty")
        _print("\n== 4/4 restoring display ==")
        if args.file:
            orient = dev.lcd_info().orientation_deg
            kind, data = _prepare_file(Path(args.file), dev.resolution,
                                       orient if orient in P.ORIENTATIONS else 0, args.raw)
            dev.upload(kind, data, wipe_first=False, progress=_progress)
            _print(f"  {SYMBOL['ok']} re-uploaded {args.file} (sanitised, full-frame)")
        else:
            dev.show_liquid()
            _print(f"  {SYMBOL['ok']} showing liquid temperature (no file given)")
    _print("\nRepair complete. If artifacts remain on the *liquid temperature* screen or on")
    _print("`kraken-tool patterns`, the fault is below the image memory: see README.")
    return 0


def cmd_cycle(args):
    from . import cycle as C
    from .transport import enumerate_devices

    devs = [d for d in enumerate_devices() if not args.serial or d.serial == args.serial]
    if len(devs) != 1:
        raise SystemExit("Need exactly one Kraken connected (use --serial). Is NZXT CAM closed?")
    entry = devs[0]

    reset = None
    if not args.no_usb_reset:
        if sys.platform == "win32" and not C._is_windows_admin():
            _print("Note: not running as Administrator, so the USB reset (the part closest to a")
            _print("reboot) will be skipped. For the full cycle: right-click PowerShell >")
            _print("'Run as administrator' and run this command again.\n")
        else:
            def reset():
                what = C.usb_reset(entry.product_id, entry.serial)
                C.wait_for_device(enumerate_devices, entry.serial)
                return what

    total = 0
    for batch in range(1, args.max_batches + 1):
        _print(f"Running {args.rounds} reset rounds (batch {batch}/{args.max_batches})...")
        C.run_rounds(args.rounds, lambda: open_device(args), reset,
                     lambda i, msg: _print(f"  round {i:2d}/{args.rounds}: {msg}"))
        total += args.rounds
        _print("\nThe pump is now showing its built-in temperature screen.")
        try:
            ans = input("Does it look clean (no stripes, no ghosting)? [y/n] > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            _print()
            return 130
        if ans.startswith("y"):
            _print(f"\nFixed after {total} rounds. Reopen NZXT CAM to restore your image/GIF and")
            _print("cooling profile. If it comes back after a later boot, just run this again.")
            return 0
        if batch < args.max_batches:
            _print("OK, trying another batch.\n")

    _print(f"\nStill corrupted after {total} rounds. The software resets aren't enough this time:")
    _print("  - do a warm reboot (that fixed it before), or a cold boot with the PSU off for 60 s;")
    _print("  - if it keeps happening, run `fingerprint` on a bad boot and send the --report.")
    return 3


def cmd_fix(args):
    from . import autofix as A
    from . import cycle as C
    from .transport import enumerate_devices

    _print(f"kraken-tool {__version__}: automatic display fix\n")
    admin = A.is_admin()
    if sys.platform == "win32" and not admin and not args.no_elevate:
        fwd = ["fix", "--rounds", str(args.rounds)] + (["--keep-cam"] if args.keep_cam else []) \
            + (["--pause"] if args.pause else [])
        _print("Asking Windows for administrator rights (needed for the USB reset)...")
        if A.relaunch_elevated(fwd):
            return 0  # the elevated copy carries on in its own window
        _print("Administrator rights declined; continuing without the USB reset.\n")

    rc = 0
    cam = [] if args.keep_cam else A.find_cam()
    if cam:
        _print("Closing NZXT CAM (it will be reopened afterwards)...")
        A.close_cam()
    try:
        entry = A.wait_for_kraken(enumerate_devices)
        if entry is None:
            _print("No Kraken found. Check the pump's internal USB cable.")
            rc = 1
        else:
            args.serial = entry.serial
            reset = None
            if admin:
                def reset():
                    what = C.usb_reset(entry.product_id, entry.serial)
                    C.wait_for_device(enumerate_devices, entry.serial)
                    return what
            _print(f"Found {entry.name}. Running {args.rounds} reset rounds...")
            C.run_rounds(args.rounds, lambda: open_device(args), reset,
                         lambda i, msg: _print(f"  round {i:2d}/{args.rounds}: {msg}"))
            _print("\nDone. The display path has been re-initialised "
                   f"{args.rounds} times.")
            _print("If the screen is still corrupted, run this again or do a warm reboot.")
    except Exception as e:  # noqa: BLE001 - always reopen CAM and report
        _print(f"\nerror: {e}")
        rc = 1
    finally:
        if cam:
            _print("Reopening NZXT CAM...")
            A.reopen_cam(cam)
    if args.pause:
        A.timed_pause(20, _print)
    return rc


def cmd_liquid(args):
    with open_device(args) as dev:
        dev.show_liquid()
    return 0


def cmd_brightness(args):
    with open_device(args) as dev:
        dev.set_lcd(brightness=args.value)
    return 0


def cmd_orientation(args):
    with open_device(args) as dev:
        dev.set_lcd(orientation=args.degrees)
    return 0


def cmd_firmware(args):
    with open_device(args) as dev:
        fw = dev.firmware()
    _print(f"Installed controller firmware: {fw}")
    _print(
        "\nkraken-tool does not write the controller's program flash. NZXT's bootloader\n"
        "protocol and signed firmware images are not public, and a bad write bricks the\n"
        "pump (it is also the pump's speed controller). To reflash official firmware:\n"
        "  1. Run `kraken-tool wipe` first (clears the image memory CAM keeps re-using).\n"
        "  2. Open NZXT CAM > your Kraken > Settings (gear) > Firmware > Update/Reinstall.\n"
        "  3. Do not let the PC sleep or unplug USB during the update.\n"
        "  4. Close CAM fully, then run `kraken-tool doctor` to confirm a clean state."
    )
    return 0


def cmd_inspect_gif(args):
    rows = gif_frame_report(args.file)
    risky = 0
    for r in rows:
        flags = []
        if not r["full_canvas"]:
            flags.append("partial-frame")
        if r["transparency"]:
            flags.append("transparency")
        if r["disposal"] not in (0, 1, 2):
            flags.append(f"disposal={r['disposal']}")
        risky += bool(flags)
        _print(f"frame {r['frame']:3d} box={r['box']} disposal={r['disposal']} "
               f"{'  <- ' + ', '.join(flags) if flags else ''}")
    _print(f"\n{risky}/{len(rows)} frames use delta/transparency tricks that the Kraken's decoder"
           " can render as ghosting." if risky else "\nAll frames are full-canvas and opaque.")
    return 0


def cmd_sanitize_gif(args):
    size = (args.size, args.size)
    data = sanitize_gif(args.input, size)
    Path(args.output).write_bytes(data)
    _print(f"Wrote {args.output} ({len(data) / 1024:.0f} KiB, {size[0]}x{size[1]}, full-frame)")
    return 0


def _ask(prompt, parser, help_text=""):
    while True:
        if help_text:
            _print(help_text)
        try:
            ans = input("  > ")
        except EOFError:
            raise KeyboardInterrupt from None
        val = parser(ans)
        if val is not None:
            return val
        _print("  (didn't understand, try again)")


def cmd_fingerprint(args):
    import time

    from . import fingerprint as F

    log_path = Path(args.log) if args.log else F.default_log_path()
    if args.report:
        for line in F.cross_boot_analysis(F.load_log(log_path)):
            _print(line)
        _print()
        _print(F.NEXT_STEPS)
        _print(f"(log: {log_path})")
        return 0

    with open_device(args) as dev:
        fw = str(dev.firmware())
        temp = dev.liquid_temperature()
        orient = dev.lcd_info().orientation_deg
        res = dev.resolution
        _print(f"{dev.name}, firmware {fw}, liquid {temp if temp is not None else '?'} °C")
        _print("Look at the pump screen from about arm's length for each step.\n")

        # reference frames live in buckets 0 and 1; tests alternate between buckets 2 and 3
        ref_kb = P.bucket_size_kb(res[0] * res[1] * 4)
        left = [b for b in dev.wipe() if b.occupied]
        if left:
            raise SystemExit(f"Could not clear buckets {[b.index for b in left]}; power-cycle and retry.")
        dev.upload(P.DATA_TYPE_RGBA, encode_rgba(F.solid(res, "black"), res), bucket=0,
                   wipe_first=False, start_kb=0, show=False)
        dev.upload(P.DATA_TYPE_RGBA, encode_rgba(F.solid(res, "white"), res), bucket=1,
                   wipe_first=False, start_kb=ref_kb, show=False)

        answers = {}
        try:
            for i, step in enumerate(F.build_plan(), 1):
                tb = 2 + i % 2  # alternate test buckets: never overwrite the one on screen
                if step.make:
                    dev.upload(P.DATA_TYPE_RGBA, encode_rgba(step.make(res), res), bucket=tb,
                               wipe_first=False, start_kb=tb * ref_kb, show=False)
                if step.before == "liquid":
                    dev.show_liquid()
                else:
                    dev.show_bucket(0 if step.before == "black" else 1)
                    time.sleep(1.5)
                    dev.show_bucket(tb)
                _print(f"[{i}] {step.prompt}")
                if step.kind == "tiles":
                    answers[step.key] = _ask(
                        "", F.parse_tiles,
                        "  Type the numbers of tiles that look striped/wrong (e.g. 1,2,5), or Enter if none.")
                else:
                    answers[step.key] = _ask("", F.parse_look, F.LOOK_HELP)
        except KeyboardInterrupt:
            _print("\nAborted; nothing logged.")
            dev.wipe()
            return 130
        dev.wipe()

    rec = F.make_record(answers, fw, temp, orient, args.note or "")
    F.append_log(log_path, rec)
    v = F.classify(answers)
    _print("\n== Result for this boot ==")
    _print(f"  {v.summary}")
    for d in v.details:
        _print(f"  - {d}")
    _print(f"\nLogged to {log_path}. After a few reboots run: kraken-tool fingerprint --report")
    return 0 if v.category == "clean" else 3


# ---------------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kraken-tool",
        description="Diagnose and repair the LCD controller of NZXT Kraken Elite coolers.",
    )
    p.add_argument("--version", action="version", version=f"kraken-tool {__version__}")
    p.add_argument("-v", "--verbose", action="store_true", help="log USB traffic")
    p.add_argument("--serial", help="select device by serial number")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="list connected Krakens").set_defaults(fn=cmd_list)
    sub.add_parser("doctor", help="read firmware, LCD state and image memory; diagnose").set_defaults(fn=cmd_doctor)
    sub.add_parser("wipe", help="delete every stored LCD image").set_defaults(fn=cmd_wipe)

    s = sub.add_parser("repair", help="full repair: reset LCD state, wipe image memory, restore display")
    s.add_argument("file", nargs="?", help="image or GIF to put back after the repair")
    s.add_argument("--raw", action="store_true", help="upload GIF as-is (skip sanitising)")
    s.set_defaults(fn=cmd_repair)

    s = sub.add_parser("pattern", help="show one hardware test pattern")
    s.add_argument("name", choices=list(PATTERNS))
    s.set_defaults(fn=cmd_pattern)

    s = sub.add_parser("patterns", help="step through all test patterns interactively")
    s.add_argument("--only", nargs="+", choices=list(PATTERNS))
    s.set_defaults(fn=cmd_patterns)

    s = sub.add_parser("upload", help="upload an image or GIF (GIFs are sanitised)")
    s.add_argument("file")
    s.add_argument("--raw", action="store_true", help="upload GIF as-is (skip sanitising)")
    s.set_defaults(fn=cmd_upload)

    s = sub.add_parser("fingerprint",
                       help="guided test that pinpoints boot-to-boot varying corruption")
    s.add_argument("--note", help='what you did before this run, e.g. "cold boot, PSU off 60s"')
    s.add_argument("--report", action="store_true", help="analyse all logged runs across reboots")
    s.add_argument("--log", help="log file (default: per-user app data)")
    s.set_defaults(fn=cmd_fingerprint)

    s = sub.add_parser("fix", help="automatic fix (default when launched without arguments)")
    s.add_argument("--rounds", type=int, default=10, help="reset rounds (default 10)")
    s.add_argument("--keep-cam", action="store_true", help="don't close/reopen NZXT CAM")
    s.add_argument("--no-elevate", action="store_true", help="don't ask for admin rights")
    s.add_argument("--pause", action="store_true", help="keep the window open briefly at the end")
    s.set_defaults(fn=cmd_fix)

    s = sub.add_parser("cycle",
                       help="re-run the Kraken side of a reboot N times, then ask if the screen is fixed")
    s.add_argument("--rounds", type=int, default=10, help="resets per batch (default 10)")
    s.add_argument("--max-batches", type=int, default=3, help="batches before giving up (default 3)")
    s.add_argument("--no-usb-reset", action="store_true", help="skip the USB device reset")
    s.set_defaults(fn=cmd_cycle)

    sub.add_parser("liquid", help="show liquid temperature").set_defaults(fn=cmd_liquid)
    s = sub.add_parser("brightness", help="set LCD brightness")
    s.add_argument("value", type=int, choices=range(0, 101), metavar="0-100")
    s.set_defaults(fn=cmd_brightness)
    s = sub.add_parser("orientation", help="set LCD orientation")
    s.add_argument("degrees", type=int, choices=P.ORIENTATIONS)
    s.set_defaults(fn=cmd_orientation)
    sub.add_parser("firmware", help="show firmware version and how to reflash it").set_defaults(fn=cmd_firmware)

    s = sub.add_parser("inspect-gif", help="(offline) check a GIF for decoder-hostile frames")
    s.add_argument("file")
    s.set_defaults(fn=cmd_inspect_gif)
    s = sub.add_parser("sanitize-gif", help="(offline) re-encode a GIF as full opaque frames")
    s.add_argument("input")
    s.add_argument("output")
    s.add_argument("--size", type=int, default=640)
    s.set_defaults(fn=cmd_sanitize_gif)
    return p


def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:  # double-clicked / launched bare: just fix it
        argv = ["fix", "--pause"]
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return args.fn(args) or 0
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # noqa: BLE001
        from .transport import TransportError

        if args.verbose or not isinstance(e, (TransportError, ValueError, OSError)):
            raise
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
