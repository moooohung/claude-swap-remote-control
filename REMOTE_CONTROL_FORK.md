# claude-swap Remote Control fork

Private fork based on upstream `claude-swap v0.26.0`.

## Local changes

- Integrates Remote Control handling into the common account-switch completion path.
- Covers rotation, direct switch, force activation, and autoswitch through the same post-commit hook.
- Runs Remote Control rebinding in a serialized worker so autoswitch does not hold its state lock while controlling CLI sessions.
- Ensures `~/.claude/settings.json` keeps `remoteControlAtStartup=true` after an account change.
- Restores that setting when Claude Code's Remote Control disconnect flow turns it off.
- Preserves an active prompt draft and defers rebinding rather than submitting over user input.

## Verification

The fork carries `tests/test_remote_control.py` in addition to the upstream switcher/autoswitch suites.

Run:

```powershell
python -m pytest -q -o addopts= tests/test_switcher.py tests/test_autoswitch.py tests/test_remote_control.py
```
