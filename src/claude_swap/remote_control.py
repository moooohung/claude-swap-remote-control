"""cswap integrated Remote Control auto-rebind module.

Automatically rebinds active Claude Code CLI sessions when accounts are swapped,
ensuring uninterrupted Remote Control access from browser/mobile.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
from ctypes import wintypes as w
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

COMMAND = "/remote-control"
CLAUDE_DIR = Path.home() / ".claude"
SESSIONS_DIR = CLAUDE_DIR / "sessions"
SETTINGS_PATH = CLAUDE_DIR / "settings.json"

_REBIND_SERIAL_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Win32 Console Low-Level Structures & Helper Classes
# ---------------------------------------------------------------------------

class Coord(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class Rect(ctypes.Structure):
    _fields_ = [
        ("Left", ctypes.c_short),
        ("Top", ctypes.c_short),
        ("Right", ctypes.c_short),
        ("Bottom", ctypes.c_short),
    ]


class ScreenInfo(ctypes.Structure):
    _fields_ = [
        ("size", Coord),
        ("cursor", Coord),
        ("attrs", w.WORD),
        ("window", Rect),
        ("maximum", Coord),
    ]


class KeyEvent(ctypes.Structure):
    _fields_ = [
        ("down", w.BOOL),
        ("repeat", w.WORD),
        ("vk", w.WORD),
        ("scan", w.WORD),
        ("char", w.WCHAR),
        ("control", w.DWORD),
    ]


class Event(ctypes.Union):
    _fields_ = [("key", KeyEvent), ("padding", ctypes.c_byte * 16)]


class InputRecord(ctypes.Structure):
    _fields_ = [("type", w.WORD), ("event", Event)]


class Console:
    """Interacts with the Win32 console buffer of a running Claude CLI process."""

    def __init__(self, pid: int):
        self.pid = pid
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k.GetConsoleProcessList.argtypes = [ctypes.POINTER(w.DWORD), w.DWORD]
        self.k.GetConsoleProcessList.restype = w.DWORD
        self.k.AttachConsole.argtypes = [w.DWORD]
        self.k.AttachConsole.restype = w.BOOL
        self.k.CreateFileW.argtypes = [
            w.LPCWSTR,
            w.DWORD,
            w.DWORD,
            w.LPVOID,
            w.DWORD,
            w.DWORD,
            w.HANDLE,
        ]
        self.k.CreateFileW.restype = w.HANDLE
        self.k.GetConsoleScreenBufferInfo.argtypes = [
            w.HANDLE,
            ctypes.POINTER(ScreenInfo),
        ]
        self.k.ReadConsoleOutputCharacterW.argtypes = [
            w.HANDLE,
            w.LPWSTR,
            w.DWORD,
            Coord,
            ctypes.POINTER(w.DWORD),
        ]
        self.k.WriteConsoleInputW.argtypes = [
            w.HANDLE,
            ctypes.POINTER(InputRecord),
            w.DWORD,
            ctypes.POINTER(w.DWORD),
        ]
        self.k.GetNumberOfConsoleInputEvents.argtypes = [
            w.HANDLE,
            ctypes.POINTER(w.DWORD),
        ]
        self.k.CloseHandle.argtypes = [w.HANDLE]
        self.handles: list[Any] = []

    def __enter__(self):
        processes = (w.DWORD * 64)()
        if self.k.GetConsoleProcessList(processes, 64):
            self.k.FreeConsole()
        if not self.k.AttachConsole(self.pid):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            n = self.k.GetConsoleProcessList(processes, 64)
            if self.pid not in list(processes)[: min(n, 64)]:
                raise RuntimeError("Target PID is not on the attached console")
            self.output = self._open("CONOUT$", 0x80000000)
            self.input = self._open("CONIN$", 0xC0000000)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def _open(self, name: str, access: int):
        h = self.k.CreateFileW(name, access, 3, None, 3, 0, None)
        if not h or h == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        self.handles.append(h)
        return h

    def __exit__(self, *_):
        for handle in self.handles:
            self.k.CloseHandle(handle)
        self.handles.clear()
        self.k.FreeConsole()

    def screen(self) -> dict:
        info = ScreenInfo()
        if not self.k.GetConsoleScreenBufferInfo(self.output, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        rows = []
        for y in range(info.window.Top, info.window.Bottom + 1):
            buf = ctypes.create_unicode_buffer(info.size.X + 1)
            nr = w.DWORD()
            if not self.k.ReadConsoleOutputCharacterW(
                self.output, buf, info.size.X, Coord(0, y), ctypes.byref(nr)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            rows.append({"y": y, "text": buf.value.rstrip()})
        pending = w.DWORD()
        if not self.k.GetNumberOfConsoleInputEvents(self.input, ctypes.byref(pending)):
            raise ctypes.WinError(ctypes.get_last_error())
        return {
            "cursor": [info.cursor.X, info.cursor.Y],
            "size": [info.size.X, info.size.Y],
            "pending": pending.value,
            "rows": rows,
        }

    def key(self, char: str, vk: int = 0, control: int = 0):
        records = (InputRecord * 2)()
        for i, down in enumerate((True, False)):
            records[i].type = 1
            records[i].event.key = KeyEvent(down, 1, vk, 0, char, control)
        nr = w.DWORD()
        if not self.k.WriteConsoleInputW(self.input, records, 2, ctypes.byref(nr)):
            raise ctypes.WinError(ctypes.get_last_error())
        if nr.value != 2:
            raise RuntimeError("Incomplete console key write")

    def text(self, text: str):
        for char in text:
            self.key(char)


# ---------------------------------------------------------------------------
# Test Helpers / Mocks
# ---------------------------------------------------------------------------

def screen(text: str = "", cursor: int = 2, pending: int = 0) -> dict:
    return {
        "cursor": [cursor, 21],
        "size": [80, 24],
        "pending": pending,
        "rows": [
            {"y": 21, "text": "> " + text},
            {"y": 22, "text": "─" * 80},
            {"y": 23, "text": "bypass permissions on"},
        ],
    }


class ConsoleMock:
    def __init__(self, snapshots: list[dict]):
        self.snapshots = iter(snapshots)
        self.last: Optional[dict] = None
        self.keys: list[tuple] = []
        self.typed: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def screen(self) -> dict:
        self.last = next(self.snapshots, self.last)
        return self.last  # type: ignore

    def text(self, text: str):
        self.typed.append(text)

    def key(self, *key):
        self.keys.append(key)


# ---------------------------------------------------------------------------
# Utility Functions
# ---------------------------------------------------------------------------

def screen_text(screen_dict: dict) -> str:
    return "\n".join(row["text"] for row in screen_dict.get("rows", []))


def prompt(screen_dict: dict) -> Optional[str]:
    """Recognize the bottom input region in standard Claude Code CLI."""
    cursor_y = screen_dict["cursor"][1]
    row = next((r["text"] for r in screen_dict.get("rows", []) if r["y"] == cursor_y), "")
    if not row.startswith(">"):
        return None
    following = [r["text"] for r in screen_dict.get("rows", []) if r["y"] > cursor_y]
    if not following or not following[0].startswith("─"):
        return None
    return row[1:].replace("\u00a0", " ").strip()


def account_fingerprint() -> Optional[str]:
    """Return SHA256 of active OAuth account from ~/.claude.json."""
    try:
        path = Path.home() / ".claude.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        account = data.get("oauthAccount", {})
        uuid = account.get("accountUuid")
        if not isinstance(uuid, str) or not uuid:
            return None
        identity = uuid + "|" + (account.get("organizationUuid") or "")
        return hashlib.sha256(identity.encode()).hexdigest()
    except Exception:
        return None


def ensure_remote_control_at_startup(
    logger: Optional[logging.Logger] = None,
    settings_path: Optional[Path] = None,
) -> bool:
    """Ensure Claude Code starts Remote Control for future CLI sessions.

    Returns True when the setting is enabled after this call. Malformed or
    unreadable settings are left untouched instead of risking unrelated user
    configuration.
    """
    path = settings_path or SETTINGS_PATH
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError("settings root is not an object")
        else:
            data = {}

        if data.get("remoteControlAtStartup") is True:
            return True

        data["remoteControlAtStartup"] = True
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=path.name + "-", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, path)
        finally:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        if logger:
            logger.info("Enabled Claude Code remoteControlAtStartup after account change.")
        return True
    except Exception as exc:
        if logger:
            logger.warning(f"Could not enable remoteControlAtStartup: {exc}")
        return False


def is_process_alive(pid: int, proc_start: Optional[str] = None) -> bool:
    """Check if process with given PID exists and matches creation timestamp."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    k.OpenProcess.restype = w.HANDLE
    k.CloseHandle.argtypes = [w.HANDLE]
    k.GetProcessTimes.argtypes = [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4

    handle = k.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        times = [w.FILETIME() for _ in range(4)]
        if not k.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
            return False
        if proc_start is not None:
            born = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            if str(born) != str(proc_start):
                return False
        return True
    finally:
        k.CloseHandle(handle)


def get_active_cli_sessions(sessions_dir: Optional[Path] = None) -> list[dict]:
    """Find all running interactive Claude CLI sessions."""
    sdir = sessions_dir or SESSIONS_DIR
    if not sdir.exists():
        return []

    active = []
    for p in sdir.glob("*.json"):
        try:
            meta = json.loads(p.read_text(encoding="utf-8-sig"))
            if meta.get("kind") != "interactive" or meta.get("entrypoint") != "cli":
                continue
            pid = meta.get("pid")
            if not pid or not is_process_alive(pid, proc_start=meta.get("procStart")):
                continue
            meta["_session_file"] = str(p)
            active.append(meta)
        except Exception:
            continue

    # Sort by recent activity or creation
    active.sort(
        key=lambda m: (m.get("lastFocusedAt") or m.get("lastActivityAt") or 0),
        reverse=True,
    )
    return active


def decision(
    owner: Optional[str],
    current: Optional[str],
    old_bridge: Optional[str],
    bridge: Optional[str],
    awaiting: bool = False,
) -> str:
    if not current:
        return "wait_identity"
    if owner == current and not awaiting:
        return "adopt" if bridge else "manual_disconnect_preserved"
    if bridge:
        if owner is not None and owner != current and bridge == old_bridge:
            return "disconnect_old_account"
        return "adopt"
    return "connect"


# ---------------------------------------------------------------------------
# Console Command Execution & Safety Checks
# ---------------------------------------------------------------------------

def undo_prefix(console: Any, screen_dict: dict, original_y: int) -> str:
    val = prompt(screen_dict) or ""
    if (
        val.startswith(COMMAND)
        and screen_dict["cursor"] == [2 + len(COMMAND), original_y]
        and not screen_dict.get("pending", 0)
    ):
        for _ in COMMAND:
            console.key("\b", 8)
        return "draft_preserved"
    return "input_changed_no_enter"


def local_command(
    console: Any,
    unchanged: Callable[[], bool],
    sleep_fn: Callable[[float], None] = time.sleep,
) -> str:
    """Type /remote-control with two-phase verification.

    Safe against human typing collisions: if the user begins typing,
    the inserted command is cleanly backspaced out without sending Enter.
    """
    before = console.screen()
    val = prompt(before)
    if (
        val is None
        or before.get("pending", 0)
        or before["cursor"][0] != 2
        or len(val) > 30
        or "NORMAL" in screen_text(before)
    ):
        return "input_deferred"

    sleep_fn(0.1)
    if console.screen() != before or not unchanged():
        return "input_deferred"

    console.text(COMMAND)
    sleep_fn(0.25)
    after = console.screen()
    valid = (
        prompt(after) == COMMAND
        and not after.get("pending", 0)
        and after["cursor"] == [2 + len(COMMAND), before["cursor"][1]]
    )
    if not valid or not unchanged():
        return undo_prefix(console, after, before["cursor"][1])

    final = console.screen()
    if final != after or not unchanged():
        return undo_prefix(console, final, before["cursor"][1])

    console.key("\r", 13)
    return "command_submitted"


def disconnect_panel(
    console: Any,
    bridge_id: str,
    unchanged: Callable[[], bool],
    sleep_fn: Callable[[float], None] = time.sleep,
) -> str:
    """Select Disconnect in the Remote Control status menu if present."""
    for _ in range(25):
        s = console.screen()
        txt = screen_text(s)
        if (
            "Remote Control" in txt
            and bridge_id in txt
            and "Disconnect this session" in txt
            and "Esc to continue" in txt
        ):
            break
        sleep_fn(0.1)
    else:
        return "panel_unconfirmed"

    for _ in range(3):
        s = console.screen()
        txt = screen_text(s)
        if (
            s.get("pending", 0)
            or not unchanged()
            or "Remote Control" not in txt
            or bridge_id not in txt
            or "Disconnect this session" not in txt
            or "Esc to continue" not in txt
        ):
            return "panel_deferred"

        if re.search(r"(?m)^\s*>\s*Disconnect this session\s*$", txt):
            console.key("\r", 13)
            return "disconnect_submitted"

        if not re.search(r"(?m)^\s*>\s*(Continue|Show QR code)", txt):
            return "panel_deferred"

        console.key("\x00", 38)  # Win32 VK_UP
        sleep_fn(0.15)

    return "panel_deferred"


# ---------------------------------------------------------------------------
# High-Level Rebind Logic
# ---------------------------------------------------------------------------

def rebind_session(
    meta: dict,
    new_fp: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> dict:
    """Disconnect old remote session (if any) and connect to new account."""
    pid = meta.get("pid")
    sid = meta.get("sessionId")
    bridge = meta.get("bridgeSessionId")
    file_path = meta.get("_session_file")

    result = {
        "sessionId": sid,
        "pid": pid,
        "action": "none",
        "status": "failed",
        "url": None,
    }

    if not pid or not file_path:
        return result

    def unchanged() -> bool:
        try:
            fresh = json.loads(Path(file_path).read_text(encoding="utf-8-sig"))
            return (
                fresh.get("pid") == pid
                and fresh.get("procStart") == meta.get("procStart")
                and is_process_alive(pid, meta.get("procStart"))
            )
        except Exception:
            return False

    try:
        with Console(pid) as console:
            # Step 1: If an old bridge session exists, disconnect it first
            if bridge:
                if logger:
                    logger.info(f"Disconnecting old Remote Control bridge {bridge} in session {sid} (PID {pid})...")
                sub = local_command(console, unchanged)
                if sub == "command_submitted":
                    disc = disconnect_panel(console, bridge, unchanged)
                    if logger:
                        logger.info(f"Disconnect result for {sid}: {disc}")
                    time.sleep(1.2)  # Wait for menu to close and prompt to reset
                    if disc == "disconnect_submitted":
                        # Claude Code's Disconnect action can flip this user
                        # preference off. cswap initiated the disconnect only
                        # to move the bridge to the newly active account, so
                        # restore the requested auto-start behavior immediately.
                        ensure_remote_control_at_startup(logger=logger)

            # Step 2: Request new Remote Control connection
            if logger:
                logger.info(f"Enabling Remote Control for new account in session {sid} (PID {pid})...")
            cmd_res = local_command(console, unchanged)
            result["status"] = cmd_res
            if cmd_res == "command_submitted":
                result["action"] = "reconnected"
                # Wait briefly for bridge session ID to be populated in session JSON
                for _ in range(20):
                    time.sleep(0.2)
                    try:
                        fresh = json.loads(Path(file_path).read_text(encoding="utf-8-sig"))
                        new_bridge = fresh.get("bridgeSessionId")
                        if new_bridge and new_bridge != bridge:
                            result["url"] = f"https://claude.ai/code/{new_bridge}"
                            result["bridgeSessionId"] = new_bridge
                            break
                    except Exception:
                        pass
            else:
                result["action"] = "deferred"
    except Exception as exc:
        result["status"] = f"error: {exc}"
        if logger:
            logger.warning(f"Error rebinding remote control for PID {pid}: {exc}")

    return result


def rebind_all_sessions(logger: Optional[logging.Logger] = None) -> list[dict]:
    """Find all active interactive Claude CLI sessions and rebind them."""
    # Account switching is the user's signal that Remote Control should follow
    # the active cswap account, including future CLI sessions.
    ensure_remote_control_at_startup(logger=logger)

    new_fp = account_fingerprint()
    if not new_fp:
        if logger:
            logger.info(
                "Remote Control auto-rebind skipped because the active account "
                "has no OAuth identity."
            )
        return []

    sessions = get_active_cli_sessions()
    if not sessions:
        if logger:
            logger.info("No active Claude Code CLI sessions found.")
        return []

    results = []
    for s in sessions:
        res = rebind_session(s, new_fp=new_fp, logger=logger)
        results.append(res)
        if logger and res.get("url"):
            logger.info(f"Session {res['sessionId']} Remote Control URL: {res['url']}")
    # A Disconnect performed above can toggle the setting after the first
    # preflight write. Re-assert it once the whole rebind batch is complete.
    ensure_remote_control_at_startup(logger=logger)
    return results


def _scheduled_rebind_worker(logger: Optional[logging.Logger]) -> None:
    """Run one rebind batch without holding any cswap/autoswitch caller lock."""
    with _REBIND_SERIAL_LOCK:
        try:
            rebind_all_sessions(logger=logger)
        except Exception as exc:
            if logger:
                logger.warning(f"Remote control auto-rebind failed: {exc}")


def schedule_rebind(
    logger: Optional[logging.Logger] = None,
) -> threading.Thread:
    """Schedule a serialized Remote Control rebind after a successful switch.

    The worker is intentionally non-daemon. Long-running surfaces such as
    cswap auto return from the switch immediately and release their own locks,
    while one-shot CLI invocations still let the short-lived worker finish
    before the Python process exits.
    """
    thread = threading.Thread(
        target=_scheduled_rebind_worker,
        args=(logger,),
        name="cswap-remote-control-rebind",
        daemon=False,
    )
    thread.start()
    return thread
