# Issue 59 lifecycle diagnosis: peer idle notification did not fire

Date: 2026-06-12
Scope: MEASURE/ANALYZE only. No code fix in this branch.
Issue context: `palios-taey/claude-code-fleet-orchestrator#59`

## Root cause

The peer Stop/AfterAgent hooks did fire in the observed Codex and Gemini idle incidents, and they did reach the shared stop-notification path. The missing notification was caused by an internal suppression branch in `hooks/_shared.py:_notify_supervisor_of_stop`: when the orchestrator stop-decision API returns `wake_type=ALLOW_STOP`, the function logs `suppressed PEER_IDLE ... allow_stop` and returns before `LPUSH`ing any message to the supervisor inbox.

That suppression contradicts the documented lifecycle contract in `README.md` and `NOTIFICATION_PROTOCOL.md`, which state that worker Stop emits a `peer_idle` message to the supervisor. In practice, the hook path currently treats `ALLOW_STOP` as "do not notify supervisor," even when the session has just idled after holding peer work.

## Findings

### 1. Does the CLI actually fire Stop/AfterAgent on idle-at-prompt?

Observed:

- `~/.codex/hooks.json` contains `Stop` wired to `/home/mira/.local/share/claude-code-fleet-notify/hooks-runtime/hooks/codex_stop.py`.
- `~/.gemini/settings.json` contains `AfterAgent` wired to `/home/mira/.local/share/claude-code-fleet-notify/hooks-runtime/hooks/gemini_after_agent.py`.
- `/tmp/taey-ed-codex-hooks.log` shows the Codex idle incidents did invoke Stop:
  - `2026-06-12T18:56:23.311859 STOP: idle=1`
  - `2026-06-12T19:06:32.498432 STOP: idle=1`
  - `2026-06-12T19:20:44.866491 STOP: idle=1`
- `/tmp/taey-ed-gemini-hooks.log` shows Gemini AfterAgent/Stop-equivalent idle events:
  - `2026-06-12T17:47:34.646871 STOP: idle=1`
  - `2026-06-12T17:48:49.798737 STOP: idle=1`
- The same logs contain many `PRE-TOOL` / `POST-TOOL` entries, proving the CLIs can honor hooks in the same installed runtime.

Inferred:

- For the issue #59 idle-at-prompt cases represented by these logs, the primary break was not "Codex/Gemini cannot run hooks." The Stop/AfterAgent boundary fired.
- The Gemini quota menu may still represent a separate class of interactive stall if it occurs without AfterAgent. The available log evidence for the captured taey-ed Gemini incidents shows AfterAgent did run at least for the observed idle windows above.

Unknown:

- Whether every possible Gemini quota menu path emits AfterAgent. No controlled quota-exhaustion reproduction was run in this diagnose-only pass.
- Whether Codex emits Stop on every idle-at-prompt class. The observed taey-ed Codex idle-after-report cases did emit Stop.

### 2. Does the hook reach `_notify_supervisor_of_stop`?

Observed:

- `hooks/codex_stop.py` and `hooks/gemini_after_agent.py` both call `action_stop(r, node_id)` after caching any stop decision.
- `hooks/_shared.py:action_stop` resolves the supervisor and calls `_notify_supervisor_of_stop(r, node_id, supervisor)` when a supervisor exists.
- The incident logs contain lines written only inside `_notify_supervisor_of_stop`:
  - `/tmp/taey-ed-codex-hooks.log`: `suppressed PEER_IDLE for taey-ed-codex: allow_stop`
  - `/tmp/taey-ed-gemini-hooks.log`: `suppressed PEER_IDLE for taey-ed-gemini: allow_stop`
- A synthetic probe with a fake node and real Redis reproduced the same internal path:
  - input state: `taey:probe-lifecycle-codex2:parent=probe-lifecycle-supervisor2`
  - input state: JSON `current_task` plus `last_outcome`
  - `fetch_stop_decision(node)` returned `{'wake_type': 'ALLOW_STOP', 'block': False, ...}`
  - after `action_stop(r, node)`, `taey:probe-lifecycle-supervisor2:inbox` was empty.

Inferred:

- The function is reached, but its ALLOW_STOP branch suppresses the lifecycle notification before enqueue.
- The stop-decision API is being used as a notification gate, not only as a stop-blocking decision. That is the contract mismatch.

Unknown:

- Whether `ALLOW_STOP` was semantically correct for the underlying orchestrator task graph in each production incident. This pass only diagnosed notify-side behavior.

### 3. Does the notification deliver to the dispatcher's inbox?

Observed:

- In the real incident logs, no delivery happened because the ALLOW_STOP branch returned before `r.lpush(inbox_key(supervisor), msg)`.
- A positive synthetic probe with `ORCH_API_BASE=http://127.0.0.1:9` forced stop-decision fail-open and used:
  - `TAEY_NODE_ID=probe-lifecycle-deliver-codex`
  - `taey:probe-lifecycle-deliver-codex:parent=probe-lifecycle-deliver-supervisor`
  - JSON `current_task` and JSON `last_outcome`
- That probe produced a supervisor inbox message:

```json
{"from":"probe-lifecycle-deliver-codex","type":"peer_idle","task_id":"probe-task","outcome":"unknown"}
```

Inferred:

- Redis delivery itself works when `_notify_supervisor_of_stop` chooses the enqueue path.
- The missing production messages were suppressed before delivery, not lost after `LPUSH`.

Unknown:

- Whether the notification daemon would also pointer-inject every delivered supervisor inbox message in all idle states. That was outside the requested root-cause question, and the positive probe stopped at Redis delivery.

## Reconciliation with "Codex/Gemini CLIs can honor hooks"

Observed:

- The installed configs for both CLIs include the expected hook entries.
- The logs show tool hooks and stop hooks firing for both `taey-ed-codex` and `taey-ed-gemini`.
- This session's own PostToolUse path surfaced notifications, which is consistent with the same installed Codex hook runtime working.

Conclusion:

- The verified "CLIs can honor hooks" finding is true.
- The incident claim "the documented peer_idle notification did not fire" is also true.
- They reconcile because the hook did fire, but notify-side code suppressed `peer_idle` when stop-decision returned `ALLOW_STOP`.

## Contract gap

Documented contract:

- `README.md`: "when a worker stops ... pushes a single `peer_idle` message"
- `NOTIFICATION_PROTOCOL.md`: "Pushes a single `peer_idle` message to the supervisor's inbox"

Runtime behavior:

- `ALLOW_STOP` suppresses `peer_idle`.
- That means a worker can Stop/idle after a report, with current dispatch context still relevant to the supervisor, and the dispatcher receives no lifecycle signal.

## Fix direction, not implemented here

The follow-on fix should separate two concepts that are currently conflated:

- Stop permission: whether the CLI should be blocked from idling.
- Supervisor lifecycle notification: whether the dispatcher must be told the worker stopped.

`ALLOW_STOP` may mean "do not block the worker," but it must not automatically mean "do not notify the dispatcher" when a supervised peer had dispatch context. The planned follow-on tasks named by conductor, `stop-notify-all` backstop and `dispatch-ack`, should enforce that separation.
