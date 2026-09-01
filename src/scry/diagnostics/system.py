"""Platform probes for the machine Scry is running on.

Deliberately stdlib-only. ``psutil`` would answer all of this in one line, but
adding a dependency so that a diagnostic command can print a number is a poor
trade for a tool whose install story is meant to be trivial on every platform.

Anything that cannot be determined returns ``None`` rather than a guess, so the
caller can report "unknown" honestly instead of asserting something false.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

BYTES_PER_GB = 1024**3


def human_bytes(count: int | None) -> str:
    if count is None:
        return "unknown"
    if count >= BYTES_PER_GB:
        return f"{count / BYTES_PER_GB:.1f} GB"
    return f"{count / (1024**2):.0f} MB"


def cpu_count() -> int:
    """Logical processors. Section 1.11 sizes its worker pool from this."""
    return os.cpu_count() or 1


def total_memory_bytes() -> int | None:
    """Physical RAM, or None where we cannot determine it.

    Spec section 14.3 sets a 4 GB floor, and that floor is the foundation of the
    whole lite-mode argument — so a user with 2 GB deserves to be told rather
    than left wondering why analysis is thrashing.
    """
    if sys.platform == "win32":
        return _windows_memory()
    if sys.platform == "darwin":
        return _darwin_memory()
    if sys.platform.startswith("linux"):
        return _linux_memory()
    return None


def _windows_memory() -> int | None:
    try:
        import ctypes
        from ctypes import wintypes

        class MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", wintypes.DWORD),
                ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatusEx()
        status.dwLength = ctypes.sizeof(MemoryStatusEx)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return int(status.ullTotalPhys)
    except Exception:
        return None


def _linux_memory() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024  # reported in kB
    except Exception:
        return None
    return None


def _darwin_memory() -> int | None:
    try:
        result = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return int(result.stdout.strip()) if result.returncode == 0 else None
    except Exception:
        return None


def free_disk_bytes(path: Path) -> int | None:
    """Free space on the volume holding ``path``, walking up if it does not exist."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


def check_writable(directory: Path) -> tuple[bool, str]:
    """Whether a directory can genuinely be written to.

    Tested by writing a file and removing it, not with ``os.access``. On Windows
    ``os.access(path, W_OK)`` reports the read-only *attribute* and ignores ACLs
    entirely, so it cheerfully returns True for a directory you cannot write to.
    For a diagnostic whose whole job is to be trustworthy, the only honest check
    is to try.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"cannot create directory: {exc}"

    probe = directory / f".scry-write-test-{uuid.uuid4().hex[:8]}"
    try:
        probe.write_text("probe", encoding="utf-8")
    except OSError as exc:
        return False, f"not writable: {exc}"
    finally:
        # A probe we cannot clean up is untidy, not a failure of the check.
        with contextlib.suppress(OSError):
            probe.unlink(missing_ok=True)
    return True, "writable"


def git_version() -> tuple[bool, str]:
    """Whether git is usable, and what it reports.

    A deliberately minimal invocation. Section 2.1 owns the real git layer;
    building it early just to ask for a version string would be the wrong trade.
    Explicit argument list, never ``shell=True``, always a timeout.
    """
    try:
        result = subprocess.run(
            ["git", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except FileNotFoundError:
        return False, "not found on PATH"
    except subprocess.TimeoutExpired:
        return False, "timed out"
    except OSError as exc:
        return False, f"could not be run: {exc}"

    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip() or "exited non-zero"
    return True, result.stdout.strip()
