# Changelog

## v1.0.3 - 2026-05-31

- Fixed `scripts/tmux-send` grok-cli multi-line dispatch bug: prior versions sent message bodies via `tmux send-keys -- "$MSG"`, which interprets embedded newlines as Enter keys. In grok-cli's modal TUI Enter = SUBMIT, so a multi-line dispatched packet fragmented into multiple partial submits and left a trailing fragment stuck in the input box. Fix uses `tmux load-buffer` + `tmux paste-buffer -p -d` (bracketed paste) for `*-grok` targets so the TUI inserts the whole message as text without interpreting embedded `\n` as Enter; a single explicit Enter then submits the complete prompt. Verified live 2026-05-31 against `conductor-grok` with a 4-line test message that submitted as one prompt + got a correct response. Both local and SSH-remote branches patched.
- No behavior change for Claude Code / codex / gemini targets (they continue to use direct `send-keys` since their Ink-TUI input boxes treat embedded newlines as input-newlines, not submit).

## v1.0.2 - 2026-05-27

- Fixed Stop-hook PEER_IDLE noise for intentionally waiting sessions: `hooks/_shared.py:action_stop()` now suppresses supervisor notifications when the stopping task has a non-empty `blocked_on` in the live OrchTask graph.
- The suppression check uses the real `current_task.task_id` and production tasks API lookup, and logs `suppressed PEER_IDLE for <session>: blocked_on=<reason>` to stderr as the audit trail.

## v1.0.1 - 2026-05-26

- Adopter-validation findings from treasurer + x-claude full cycles.
