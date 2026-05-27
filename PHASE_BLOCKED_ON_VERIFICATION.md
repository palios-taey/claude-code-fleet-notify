# Blocked-On Stop-Hook Verification

Date: 2026-05-27

## Scope

- [Observed] Verification exercised the live `hooks/codex_stop.py` entrypoint for the active tmux session `conductor-codex`.
- [Observed] The hook path resolved supervisor `conductor`.
- [Observed] Two real OrchTasks were created in the production tasks API and bound to the live session through the orchestrator dispatch wire:
  - blocked task: `task-8af4e649`
  - unblocked task: `task-96cdbaa5`

## Blocked-On Suppression

- [Observed] `task-8af4e649` was marked `in_progress` with `blocked_on=family-round4-response`.
- [Observed] Invoking the live Stop hook entrypoint produced stdout `{}` and stderr:

```text
suppressed PEER_IDLE for conductor-codex: blocked_on=family-round4-response
```

- [Observed] `taey:conductor:inbox` had 0 messages before the blocked hook run and 0 messages after it.
- [Observed] No `peer_idle` message containing `task-8af4e649` appeared in the supervisor inbox.

## Inverse Check

- [Observed] `task-96cdbaa5` was marked `in_progress` with `blocked_on=""`.
- [Observed] Invoking the same live Stop hook entrypoint produced stdout `{}` and empty stderr.
- [Observed] `taey:conductor:inbox` had 0 messages before the unblocked hook run and 1 message after it.
- [Observed] The emitted message contained:
  - `type=peer_idle`
  - `task_id=task-96cdbaa5`
  - `task_description="unblocked stop-hook notification verification"`

## Cleanup

- [Observed] The temporary `current_task` bindings for `conductor-codex` were cleared after verification.
- [Observed] The temporary supervisor inbox messages used for verification were removed after capture.

## Unknowns

- [Unknown] This verification used the live Stop hook entrypoint directly rather than waiting for a human-driven CLI stop event. The code path, session identity detection, Redis state, and supervisor inbox were all production surfaces.
