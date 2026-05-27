# Changelog

## v1.0.2 - 2026-05-27

- Fixed Stop-hook PEER_IDLE noise for intentionally waiting sessions: `hooks/_shared.py:action_stop()` now suppresses supervisor notifications when the stopping task has a non-empty `blocked_on` in the live OrchTask graph.
- The suppression check uses the real `current_task.task_id` and production tasks API lookup, and logs `suppressed PEER_IDLE for <session>: blocked_on=<reason>` to stderr as the audit trail.

## v1.0.1 - 2026-05-26

- Adopter-validation findings from treasurer + x-claude full cycles.
