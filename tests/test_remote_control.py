"""Unit tests for cswap integrated remote_control module."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from claude_swap.remote_control import (
    COMMAND,
    ConsoleMock,
    decision,
    disconnect_panel,
    ensure_remote_control_at_startup,
    get_active_cli_sessions,
    local_command,
    rebind_session,
    schedule_rebind,
    screen,
)
from claude_swap.switcher import ClaudeAccountSwitcher


class TestRemoteControl(unittest.TestCase):
    def test_decision_disconnects_old_account(self):
        self.assertEqual(
            decision("old_fp", "new_fp", "bridge_123", "bridge_123"),
            "disconnect_old_account",
        )

    def test_decision_already_rebound(self):
        self.assertEqual(
            decision("old_fp", "new_fp", "bridge_123", "bridge_456"),
            "adopt",
        )

    def test_decision_new_connection_when_no_bridge(self):
        self.assertEqual(
            decision("old_fp", "new_fp", None, None),
            "connect",
        )

    def test_decision_same_account_retains(self):
        self.assertEqual(
            decision("same_fp", "same_fp", "bridge_123", "bridge_123"),
            "adopt",
        )

    def test_local_command_submits_when_clean(self):
        before = screen("")
        after = screen(COMMAND, 2 + len(COMMAND))
        console = ConsoleMock([before, before, after, after])
        res = local_command(console, lambda: True, lambda _: None)
        self.assertEqual(res, "command_submitted")
        self.assertEqual(console.typed, [COMMAND])
        self.assertEqual(console.keys, [("\r", 13)])

    def test_local_command_restores_draft_on_human_input(self):
        before = screen("my work")
        after = screen(COMMAND + "my work", 2 + len(COMMAND))
        console = ConsoleMock([before, before, after])
        res = local_command(console, lambda: True, lambda _: None)
        self.assertEqual(res, "draft_preserved")
        self.assertEqual(console.keys, [("\b", 8)] * len(COMMAND))

    def test_local_command_deferred_when_pending(self):
        console = ConsoleMock([screen(pending=1)])
        res = local_command(console, lambda: True, lambda _: None)
        self.assertEqual(res, "input_deferred")
        self.assertEqual(console.keys, [])
        self.assertEqual(console.typed, [])

    def test_disconnect_panel_selects_disconnect(self):
        panel = {
            "cursor": [3, 21],
            "size": [80, 24],
            "pending": 0,
            "rows": [
                {"y": 14, "text": "Remote Control"},
                {"y": 17, "text": "https://claude.ai/code/session_old"},
                {"y": 19, "text": "  > Disconnect this session"},
                {"y": 21, "text": "Continue"},
                {"y": 23, "text": "Enter to select · Esc to continue"},
            ],
        }
        console = ConsoleMock([panel, panel])
        res = disconnect_panel(console, "session_old", lambda: True, lambda _: None)
        self.assertEqual(res, "disconnect_submitted")
        self.assertEqual(console.keys, [("\r", 13)])

    def test_disconnect_panel_unconfirmed(self):
        other = screen("Some other prompt")
        console = ConsoleMock([other])
        res = disconnect_panel(console, "session_old", lambda: True, lambda _: None)
        self.assertEqual(res, "panel_unconfirmed")
        self.assertEqual(console.keys, [])

    def test_get_active_cli_sessions_filters(self):
        with tempfile.TemporaryDirectory() as td:
            sessions_dir = Path(td)
            # 1: Good interactive cli
            (sessions_dir / "s1.json").write_text(
                '{"sessionId": "s1", "pid": 111, "kind": "interactive", "entrypoint": "cli"}',
                encoding="utf-8",
            )
            # 2: Non-interactive
            (sessions_dir / "s2.json").write_text(
                '{"sessionId": "s2", "pid": 222, "kind": "batch", "entrypoint": "cli"}',
                encoding="utf-8",
            )
            # 3: Non-cli
            (sessions_dir / "s3.json").write_text(
                '{"sessionId": "s3", "pid": 333, "kind": "interactive", "entrypoint": "desktop"}',
                encoding="utf-8",
            )

            with patch("claude_swap.remote_control.is_process_alive", side_effect=lambda pid, **kw: pid == 111):
                sessions = get_active_cli_sessions(sessions_dir=sessions_dir)
                self.assertEqual(len(sessions), 1)
                self.assertEqual(sessions[0]["sessionId"], "s1")

    def test_ensure_remote_control_at_startup_preserves_other_settings(self):
        with tempfile.TemporaryDirectory() as td:
            settings = Path(td) / "settings.json"
            settings.write_text(
                '{"theme": "dark", "remoteControlAtStartup": false}',
                encoding="utf-8",
            )

            self.assertTrue(
                ensure_remote_control_at_startup(settings_path=settings)
            )
            data = json.loads(settings.read_text(encoding="utf-8"))
            self.assertEqual(data["theme"], "dark")
            self.assertIs(data["remoteControlAtStartup"], True)

    def test_ensure_remote_control_at_startup_leaves_malformed_settings(self):
        with tempfile.TemporaryDirectory() as td:
            settings = Path(td) / "settings.json"
            settings.write_text("{broken", encoding="utf-8")

            self.assertFalse(
                ensure_remote_control_at_startup(settings_path=settings)
            )
            self.assertEqual(settings.read_text(encoding="utf-8"), "{broken")

    def test_schedule_rebind_runs_worker(self):
        with patch(
            "claude_swap.remote_control.rebind_all_sessions", return_value=[]
        ) as rebind:
            thread = schedule_rebind()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            rebind.assert_called_once_with(logger=None)


class TestSwitcherRemoteControlHook(unittest.TestCase):
    def _switcher(self):
        switcher = object.__new__(ClaudeAccountSwitcher)
        switcher._logger = MagicMock()
        return switcher

    def test_successful_switch_schedules_remote_control(self):
        switcher = self._switcher()
        expected = {
            "from": {"number": 1, "email": "one@example.com"},
            "to": {"number": 2, "email": "two@example.com"},
            "warnings": [],
        }
        with patch.object(
            ClaudeAccountSwitcher, "_perform_switch_core", return_value=expected
        ) as core, patch(
            "claude_swap.remote_control.schedule_rebind"
        ) as schedule:
            result = switcher._perform_switch("2", emit_output=False)

        self.assertIs(result, expected)
        core.assert_called_once_with(
            "2",
            emit_output=False,
            force_activate=False,
            provenance=None,
        )
        schedule.assert_called_once_with(logger=switcher._logger)

    def test_remote_control_schedule_failure_does_not_fail_switch(self):
        switcher = self._switcher()
        expected = {"from": None, "to": {"number": 2}, "warnings": []}
        with patch.object(
            ClaudeAccountSwitcher, "_perform_switch_core", return_value=expected
        ), patch(
            "claude_swap.remote_control.schedule_rebind",
            side_effect=RuntimeError("boom"),
        ):
            result = switcher._perform_switch("2")

        self.assertIs(result, expected)
        switcher._logger.warning.assert_called_once()


if __name__ == "__main__":
    unittest.main()
