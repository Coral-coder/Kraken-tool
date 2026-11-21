"""Guided fault fingerprinting for boot-to-boot varying LCD corruption.

The controller has no command to read back what the panel shows, so the user
is the sensor.  Each step is built so a yes/no answer about what the screen
looks like *from arm's length* is enough - no need to resolve single pixels.

Core idea: show a known "before" frame, then switch to a "test" frame.  Any
memory location the display path fails to update keeps the *before* content.
Tests that light only every 4th column (or row) reveal which pixel phase,
and therefore which memory data lane, is failing.
"""

from __future__ import annotations

import json
import os
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from PIL import Image, ImageDraw, ImageFont

Size = tuple

# answers
OK, FAIL, STRIPED, PATCHY = "ok", "fail", "striped", "patchy"


# ---------------------------------------------------------------- patterns

def solid(size, color):
    return Image.new("RGB", size, color)


def phase_pattern(size, axis: str, phase: int, period: int = 4, color=(255, 0, 0)):
    """Light only pixels whose x (axis='col') or y (axis='row') % period == phase."""
    w, h = size
    img = Image.new("RGB", size, "black")
    d = ImageDraw.Draw(img)
    if axis == "col":
        for x in range(phase, w, period):
            d.line([(x, 0), (x, h - 1)], fill=color)
    else:
        for y in range(phase, h, period):
            d.line([(0, y), (w - 1, y)], fill=color)
    return img


def tile_pattern(size, n: int = 4):
    """White screen split into n*n numbered tiles (1..n*n, row-major from top-left)."""
    w, h = size
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    tw, th = w // n, h // n
    try:
        font = ImageFont.load_default(size=max(12, th // 2))
    except TypeError:  # Pillow < 10.1
        font = ImageFont.load_default()
    for r in range(n):
        for c in range(n):
            x0, y0 = c * tw, r * th
            d.rectangle([x0, y0, x0 + tw - 1, y0 + th - 1], outline=(0, 0, 255), width=2)
            label = str(r * n + c + 1)
            bb = d.textbbox((0, 0), label, font=font)
            d.text((x0 + (tw - (bb[2] - bb[0])) / 2, y0 + (th - (bb[3] - bb[1])) / 2 - bb[1]),
                   label, fill="black", font=font)
    return img


# -------------------------------------------------------------------- plan

@dataclass
class Step:
    key: str
    before: str  # "black" | "white" | "liquid"
    make: Optional[Callable[[Size], Image.Image]]
    prompt: str
    kind: str = "look"  # "look" (ok/fail/striped/patchy) | "tiles" (list of tile numbers)


def build_plan() -> list[Step]:
    steps = [
        Step("liquid", "liquid", None,
             "The pump is showing its BUILT-IN temperature screen (nothing uploaded).\n"
             "  Is it clean?"),
        Step("white_after_black", "black", lambda s: solid(s, "white"),
             "The screen should now be evenly WHITE."),
        Step("black_after_white", "white", lambda s: solid(s, "black"),
             "The screen should now be evenly BLACK."),
    ]
    for axis, word in (("col", "vertical"), ("row", "horizontal")):
        for ph in range(4):
            steps.append(Step(
                f"{axis}{ph}", "black", (lambda s, a=axis, p=ph: phase_pattern(s, a, p)),
                f"Fine {word} RED lines (1 in 4 lit, offset {ph}). From arm's length the\n"
                "  screen should look evenly DIM RED.",
            ))
    steps.append(Step("tiles", "black", tile_pattern,
                      "White screen with numbered tiles 1-16 (1 = top-left, reading order).",
                      kind="tiles"))
    return steps


LOOK_HELP = (
    "  [y] yes, looks right    [s] stripes / lines    [p] patches or blocks\n"
    "  [n] wrong overall (still black / old image / wrong colour)"
)
_LOOK_MAP = {"y": OK, "s": STRIPED, "p": PATCHY, "n": FAIL}


def parse_look(ans: str) -> Optional[str]:
    return _LOOK_MAP.get(ans.strip().lower()[:1])


def parse_tiles(ans: str) -> Optional[list[int]]:
    ans = ans.strip().lower()
    if ans in ("", "none", "0", "y"):
        return []
    try:
        vals = sorted({int(x) for x in ans.replace(" ", ",").split(",") if x})
    except ValueError:
        return None
    return vals if all(1 <= v <= 16 for v in vals) else None


# --------------------------------------------------------------- classify

@dataclass
class Verdict:
    category: str
    summary: str
    details: list[str] = field(default_factory=list)


def _phases(answers: dict, axis: str) -> list[int]:
    return [p for p in range(4) if answers.get(f"{axis}{p}", OK) != OK]


def describe_phase_set(phases: list[int], axis: str) -> str:
    unit = "column" if axis == "col" else "row"
    s = set(phases)
    if s in ({0, 2}, {1, 3}):
        return (f"every other {unit} ({'even' if s == {0, 2} else 'odd'}) fails to update. "
                "With 16-bit pixels packed two per 32-bit memory word, that is exactly one "
                "half-word lane of the frame-buffer data bus")
    if len(s) == 1:
        return (f"one {unit} in four (offset {phases[0]}) fails to update: one 16-bit lane of a "
                "64-bit-wide memory path, or one source-driver channel group")
    if len(s) == 4:
        return f"all {unit} phases are affected: not lane-specific"
    return f"{unit} phases {sorted(s)} fail to update"


def classify(answers: dict) -> Verdict:
    write_bad = [k for k in ("white_after_black", "black_after_white") if answers.get(k, OK) != OK]
    cols, rows = _phases(answers, "col"), _phases(answers, "row")
    tiles = answers.get("tiles", [])
    liquid_bad = answers.get("liquid", OK) != OK
    details = []

    if not (write_bad or cols or rows or tiles or liquid_bad):
        return Verdict("clean", "No fault reproduced this boot.",
                       ["Run `kraken-tool fingerprint` again after the next reboot where the "
                        "corruption is visible; the cross-boot report needs at least one bad run."])

    if cols and not rows:
        cat, summ = "column-lane", "Lane-specific write failure (columns): " + describe_phase_set(cols, "col")
    elif rows and not cols:
        cat, summ = "row-lane", "Lane-specific write failure (rows): " + describe_phase_set(rows, "row")
    elif cols and rows:
        cat, summ = "broad", "Both row and column phases fail: wide frame-buffer/display-path failure"
    elif tiles:
        cat, summ = "regional", f"Region-specific failure in tiles {tiles}: address-range dependent"
    elif write_bad:
        cat, summ = "write", "Full-screen updates fail but phase tests pass: intermittent write path"
    else:
        cat, summ = "liquid-only", "Only the built-in screen is corrupt"

    if liquid_bad:
        details.append("The built-in temperature screen is corrupt too, so the fault is in the "
                       "controller/display path itself; uploaded images are not involved.")
    if write_bad:
        details.append("Stale content survives a full-screen change (" + ", ".join(write_bad) +
                       "): the display is not being fully rewritten.")
    if tiles and cat != "regional":
        details.append(f"Worst tiles: {tiles}.")
    return Verdict(cat, summ, details)


# -------------------------------------------------------------------- log

def default_log_path() -> Path:
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return Path(base) / "kraken-tool" / "fingerprint.jsonl"


def append_log(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def load_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def make_record(answers: dict, firmware: str, temp: Optional[float], orientation: int,
                note: str = "") -> dict:
    v = classify(answers)
    return {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "firmware": firmware,
        "liquid_temp_c": temp,
        "orientation": orientation,
        "note": note,
        "answers": answers,
        "category": v.category,
        "summary": v.summary,
    }


# ------------------------------------------------------- cross-boot report

def cross_boot_analysis(runs: list[dict]) -> list[str]:
    if not runs:
        return ["No fingerprint runs logged yet. Run `kraken-tool fingerprint` first."]
    out = []
    bad = [r for r in runs if r["category"] != "clean"]
    out.append(f"{len(runs)} run(s), {len(bad)} with a reproduced fault.")
    if not bad:
        out.append("Never reproduced under test. Run again on a boot where the corruption is visible.")
        return out

    sigs = {(tuple(_phases(r["answers"], "col")), tuple(_phases(r["answers"], "row"))) for r in bad}
    cats = {r["category"] for r in bad}
    if len(bad) >= 2 and len(sigs) == 1 and cats <= {"column-lane", "row-lane"}:
        out.append("SAME lane fails on every bad boot -> a fixed physical fault (one data line of the "
                   "frame-buffer memory or panel interface: cracked joint, trace, or the chip). "
                   "Software cannot fix this; it is a warranty repair.")
    elif len(sigs) > 1 or (len(cats) > 1):
        out.append("The failing lane/region CHANGES between boots -> the hardware lines work, but the "
                   "interface is initialised marginally at power-up (memory/display timing "
                   "calibration, or supply voltage sagging during init). This is the signature of "
                   "a marginal component or power, not a stored-image problem.")
    if len(runs) > len(bad):
        out.append(f"{len(runs) - len(bad)} boot(s) came up clean, so it is intermittent, "
                   "which also points at marginal timing/power rather than a dead line.")

    t_bad = [r["liquid_temp_c"] for r in bad if r.get("liquid_temp_c") is not None]
    t_ok = [r["liquid_temp_c"] for r in runs if r["category"] == "clean" and r.get("liquid_temp_c") is not None]
    if t_bad and t_ok:
        mb, mo = statistics.mean(t_bad), statistics.mean(t_ok)
        trend = "hotter" if mb - mo > 1.5 else "colder" if mo - mb > 1.5 else "similar"
        out.append(f"Liquid temp: faulty runs avg {mb:.1f} °C, clean runs avg {mo:.1f} °C ({trend}).")
        if trend != "similar":
            out.append("The fault tracks temperature: a thermally sensitive joint or component.")
    fws = {r["firmware"] for r in runs}
    if len(fws) > 1:
        out.append(f"Firmware changed between runs: {sorted(fws)}. Compare the runs before and after.")
    return out


NEXT_STEPS = """\
What to do with the result
--------------------------
1. Reproduce on several boots (3-5) and note in --note what you did, e.g.
   "warm reboot", "cold boot after PSU off 60s", "different SATA cable".
2. Power isolation (the cheapest real fix for boot-variable faults):
   - Move the Kraken's SATA power to a different PSU cable/connector (not a splitter).
   - Move its USB to a different motherboard USB 2.0 header.
   - Cold-start: PSU switch off for 60 s (warm reboots keep the pump powered).
   If faults disappear after a change, the supply during start-up was the cause.
3. Reinstall firmware through NZXT CAM (re-runs the controller's init code with
   factory settings). Run `kraken-tool fingerprint` again after it.
4. If a lane fault remains after 2-3: the display board is failing. Open an NZXT
   warranty ticket and attach `kraken-tool fingerprint --report` output and photos.
"""
