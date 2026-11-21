"""One-launch automatic fix: elevate, close CAM, run reset rounds, reopen CAM."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Callable, Optional

CAM_PROCESS_PATTERN = "NZXT CAM*"


# ------------------------------------------------------------- elevation

def relaunch_elevated(args: list[str]) -> bool:
    """Re-run this program as Administrator (UAC prompt). True if the relaunch started."""
    import ctypes

    if getattr(sys, "frozen", False):
        exe, params = sys.executable, subprocess.list2cmdline(args)
    else:
        exe, params = sys.executable, subprocess.list2cmdline(["-m", "kraken_tool", *args])
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
    return rc > 32  # ShellExecute: >32 means success; user declining UAC gives 5


# -------------------------------------------------------------- NZXT CAM

def _ps(cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                          capture_output=True, text=True, timeout=60)


def find_cam() -> list[str]:
    """Executable paths of running NZXT CAM processes (Windows)."""
    if sys.platform != "win32":
        return []
    r = _ps(f"Get-Process -Name '{CAM_PROCESS_PATTERN}' -ErrorAction SilentlyContinue "
            "| Select-Object -ExpandProperty Path")
    return sorted({p.strip() for p in r.stdout.splitlines() if p.strip()})


def close_cam() -> None:
    _ps(f"Get-Process -Name '{CAM_PROCESS_PATTERN}' -ErrorAction SilentlyContinue | Stop-Process -Force")
    for _ in range(20):  # wait until it has released the device
        if not find_cam():
            break
        time.sleep(0.5)
    time.sleep(2)


def reopen_cam(paths: list[str]) -> None:
    """Start CAM again as the normal (non-admin) user, via Explorer."""
    main = [p for p in paths if os.path.basename(p).lower() == "nzxt cam.exe"] or paths[:1]
    for p in main:
        subprocess.Popen(["explorer.exe", p])


# ------------------------------------------------------------------ pause

def timed_pause(seconds: int, out: Callable[[str], None]) -> None:
    """Keep a double-clicked console window readable, without needing input."""
    if sys.platform != "win32":
        return
    import msvcrt

    out(f"\nThis window closes in {seconds} s (press any key to close now).")
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if msvcrt.kbhit():
            msvcrt.getch()
            return
        time.sleep(0.1)


def is_admin() -> bool:
    if sys.platform != "win32":
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    from .cycle import _is_windows_admin

    return _is_windows_admin()


def wait_for_kraken(find: Callable[[], list], timeout_s: float = 30.0) -> Optional[object]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        devs = find()
        if devs:
            return devs[0]
        time.sleep(1)
    return None
