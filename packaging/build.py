"""Build a single-file kraken-tool executable with PyInstaller (run from repo root)."""
import platform
import sys

import PyInstaller.__main__

name = f"kraken-tool-{sys.platform}-{platform.machine().lower()}"
args = [
    "src/kraken_tool/__main__.py",
    "--onefile",
    "--console",
    "--name", name,
    "--paths", "src",
    "--collect-submodules", "kraken_tool",
    "--hidden-import", "hid",
    "--collect-submodules", "usb",
]
if sys.platform == "win32":
    args += ["--collect-all", "libusb_package"]
PyInstaller.__main__.run(args)
