# Live Capability Ledger

This page is a claims ledger for the current `claude-code-fleet-notify` main
branch. It distinguishes capabilities implemented here from orchestration
features implemented in `claude-code-fleet-orchestrator`.

| Capability | Current status | How to observe it |
|---|---|---|
| Tasks | Delegated. This repo transports task-state notifications but does not own the task graph. | Orchestrator `taey-task`; notify Redis payload fields `task_id`, `task_description`, `current_task`. |
| Plans | Delegated. Plan ingest and dependency readiness live in the orchestrator. | Orchestrator `taey-plan`; notify only carries messages generated from plan/task state. |
| Stop engine | Integrated, not owned. Stop hooks call the orchestrator `/stop-decision` endpoint when configured, then set Redis idle state and notify the supervisor. SessionStart also marks the initial resting state idle; the daemon can repair a Claude Code at-rest composer back to idle when Stop is bypassed, pending mail is older than the reconcile grace, no fresh tool-running signal exists, and the pane has no active-turn marker. | `hooks/_shared.py:fetch_stop_decision`; `hooks/stop_idle.py`; `hooks/session_start.py`; `notifications/daemon.py:reconcile_idle_at_rest`; Redis key `${NOTIFY_KEY_PREFIX:-taey}:<node>:idle`. |
| Dispatch | Integrated, not owned. Dispatch writes `current_task` in Redis from the orchestrator; notify hooks read it when a worker stops. | Redis key `${NOTIFY_KEY_PREFIX:-taey}:<worker>:current_task`; `hooks/_shared.py:_current_task_summary`. |
| Chat | Transport only. Dashboard chat is orchestrator-owned; this repo delivers chat-like messages through the same inbox queues. | `taey-notify <target> <message>`; `taey-ack --node <target> --peek`. |
| Refs | Delegated. Ref storage/rendering lives in the orchestrator. Notify can carry wake packets containing refs when the orchestrator endpoint returns them. | `hooks/_shared.py:_fetch_wake_packet`; orchestrator `/api/sessions/{session}/wake-packet`. |
| Wake packet | Integrated, not owned. SessionStart and UserPromptSubmit hooks fetch the orchestrator wake packet as primary scoped context; PostToolUse also appends it to drained notification deliveries. | `hooks/_shared.py:_fetch_wake_packet`; set `ORCH_API_BASE`; the orchestrator wake-packet endpoint must be enabled. |
| Receipts | Handoff receipts live here; decision receipts live in the orchestrator. Explicit handoff messages create receipt/ack Redis keys. | `taey-handoff`; `notifications/handoff.py`; keys `${NOTIFY_KEY_PREFIX:-taey}:handoff:*`. |
| Template | Delegated. Gate-template plan expansion is orchestrator-owned. | Orchestrator `ORCH_GATE_TEMPLATE_ENABLED`; no template expansion code exists in this repo. |
| Loops | Delegated. Loop declaration/advance is orchestrator-owned. Notify only carries loop wake messages if the orchestrator sends them. | Orchestrator `/api/loops/*`; notify `taey-notify` inbox delivery. |
| Notify | Live by default. `taey-notify` writes JSON messages to Redis inboxes; `taey-ack` peeks/drains them. | `taey-notify session-b "hello"` then `taey-ack --node session-b --peek`. |
| Daemon | Live by default when started. One local daemon watches Redis inboxes and tmux idle state, repairs clear current-pane at-rest composer state to `idle=1` only after the reconcile guards pass, then pointer-injects pending messages into idle sessions. Process heartbeat is emitted by an independent timer thread; delivery movement is exposed as a separate progress cursor. | `bash scripts/start_notify_daemons.sh start`; `bash scripts/start_notify_daemons.sh status`; `/tmp/notify-daemon.log`; Redis keys `${NOTIFY_KEY_PREFIX:-taey}:_notify_daemon:heartbeat` and `${NOTIFY_KEY_PREFIX:-taey}:_notify_daemon:delivery_progress`. |
| Handoff | Live. Explicit handoff records and passive receipt flushing use Redis records plus inbox messages. | `taey-handoff --help`; `taey-notify --handoff`; `notifications/handoff.py`. |
| Trace | Live best-effort. Notify enqueue/drain/idle/inject events append to Redis stream `taey:notify_trace`; failures never break delivery. | `taey-trace`; `notifications/trace.py`; `redis-cli XREVRANGE taey:notify_trace + - COUNT 20`. |
| Stable runtime root | Live. `install-hooks.sh --apply` copies hooks, `notifications`, and `identity.py` to `~/.local/share/claude-code-fleet-notify/hooks-runtime` or `CF_INSTALL_DIR`; settings reference only runtime copies. | `bash scripts/install-hooks.sh --apply`; inspect `~/.claude/settings.json` or sandbox settings for `hooks-runtime`. |
| Hook boot gate | Live. In apply mode the installer imports every runtime hook from a neutral cwd before writing settings; failures leave settings untouched. | `bash scripts/install-hooks.sh --apply`; installer output `[boot-gate] ... runtime hooks import cleanly.` |
| Live-path guard | Live when a registry path is configured (`CF_LIVE_PATH_REGISTRY` or `ORCH_LIVE_PATH_REGISTRY` — no built-in default) and the file exists. The second PreToolUse/BeforeTool hook denies destructive shell operations targeting registered live checkouts; registered worktree roots are allowed. Unset path, missing/unreadable registry, or unparseable commands fail open with a warning naming the fix. | `hooks/pre_tool_live_guard.py`; `hooks/_shared.py:live_guard_decision`; registry example `config/live_path_registry.example.json`. |

Documentation rule: if a row cannot be observed by the command/file named in
the right column, treat it as a bug in either the docs or the implementation.
