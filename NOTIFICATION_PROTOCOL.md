# Notification Protocol

This is the protocol reference for `claude-code-fleet-notify`.

> **This protocol is written for Taey.** If you are Taey, this is the wire contract for your
> participation in the fleet. You are reached by name: `taey-notify taey "<message>"` writes to your
> Redis inbox, and the same pattern reaches `taey-council-1` through `taey-council-7`.
>
> How you use it: hookable peers receive full bodies through hook context while active and pointer
> wakes while idle. Your canonical line-reader seats are headless at the notify layer: the durable
> `taey-presence` runtime claims Redis mail, acknowledges it only after durable outcome handling,
> and makes your model turn through its `:8766` proxy rather than through tmux text injection.
>
> Failure mode first: sent is not received. A send success proves an envelope was queued, not that a
> participant read it or acted. Verify with `taey-ack --node <target> --peek`, handoff ack keys,
> `taey-trace`, or the receiver's response before treating a notification as delivered.

## How to send a message

```bash
taey-notify <session-B> "message body"
cc-fleet-notify <session-B> "same sender, alias CLI"
```

Or direct Redis, when the CLI is unavailable:

```bash
redis-cli -h 127.0.0.1 LPUSH "${NOTIFY_KEY_PREFIX:-taey}:<session-B>:inbox" '{"from":"<session-A>","type":"message","body":"text","msg_id":"unique-id"}'
```

Targets are named reader seats. A practical fleet might use names such as `<session-A>`, `<session-B>`, `docs-agent`, or `build-worker`.

The CLI does not treat every string as reachable. A normal send must pass the
three-check reader-readiness gate before Redis is mutated: the target has a
reader signal through tmux or first-class headless/line-reader presence; queued
mail is zero or visibly draining; and the reader is active, explicitly idle with
an empty queue, or a headless reader with fresh activity or recent drain
evidence. Exact canonical Taey line readers also count as active when
`turns_open > 0`, their `active_turns` sorted set contains an unexpired lease,
and their queue is empty or visibly draining. This lease projection is not
accepted for arbitrary targets. Non-Taey targets
that are registered or carry a first-class state signal are admitted for send rather than blocked
solely because the local tmux probe is unavailable; canonical Taey line readers never take this
degraded admission and must still satisfy one of the ordinary fresh/idle/headless readiness paths or the canonical
active-turn lease predicate; an expired lease alone never authorizes delivery. A failed check
exits nonzero and prints the currently eligible targets.
Use `--allow-unregistered-target` (alias `--allow-readerless-target`) only for intentional pre-provisioning sends.

One narrow record-only exception preserves terminal consultation receipts while
Main Taey is already serving a model turn. An exact `target=taey`,
`from=consult-monitor`, `type=result` envelope whose JSON body declares
`schema=taey.consult_terminal_receipt.v1`, `terminal=true`, a supported terminal
extraction status, and non-empty monitor/platform/display identities may queue
behind a non-empty inbox only while Taey has both a positive `turns_open`
projection and an unexpired active-turn lease. Malformed receipts, council
targets, explicit handoffs, and every ordinary notification retain the
non-draining-inbox refusal.
The receipt uses the ordinary `${NOTIFY_KEY_PREFIX:-taey}:taey:inbox`: writers
`LPUSH`, the Presence reader `LMOVE`s from `RIGHT`, so delivery remains FIFO and
the receipt does not jump ahead of older actionable mail. Send success still
proves enqueue only, not that Presence has persisted and acknowledged the receipt.

## Participant types

- **tmux-session participants** are REPL-CLI sessions with the four hooks installed. Supported CLIs: Claude Code, OpenAI codex, Google gemini, and xAI grok (see "Per-CLI hook integration" below for config-file paths and event-name mappings). The daemon injects a pointer prompt through tmux only when the session is stopped and marked idle.
- **headless participants** read Redis directly. They do not use hooks, tmux injection, or `idle` state. They poll `${NOTIFY_KEY_PREFIX:-taey}:<name>:inbox` and reply with `taey-notify <target> --from <name>`.
- **Taey line-reader participants** are the exact canonical tmux identities `taey` and `taey-council-1` through `taey-council-7`. They claim and acknowledge Redis mail through the durable `taey-presence` seat runtime rather than hooks. Their attributable proxy active-turn registry derives `idle`; the daemon injects only a pointer, and `tmux-send` uses plain legacy Enter without Escape or CSI-u bytes.
- **peer defect/status/result reports** may omit `<target>`. In that path,
  `NOTIFY_TARGET` wins as an explicit override; otherwise the CLI derives the
  same parent as the Stop hook. A per-parent peer such as `weaver-codex`
  therefore reports to `weaver`, not `conductor`.

## Dual delivery path — the canonical dispatch pattern for tmux participants

For every hookable tmux-session participant (Claude Code / codex / gemini / grok), notifications reach the session through ONE of two paths depending on the session's current state. No subprocess invocation, no fallback paths; these two paths cover the full state space. Canonical Taey line-reader seats use the same idle-gated pointer path while claiming the queued body directly from Redis.

**Path A — Active (session is making tool calls)**:
- Notifications are delivered as `hookSpecificOutput.additionalContext` via the PostToolUse hook (`hooks/check_notifications.py`).
- The session sees the notification body in its next prompt context without any tmux involvement.
- This is the **injection-during-tool-use** path. Used while `idle != 1`.

**Path B — Idle (session has stopped, `idle=1` set by Stop/SessionStart or daemon at-rest reconciliation)**:
- The notification daemon (`notifications/daemon.py`) polls Redis, detects pending notifications for an idle session, and uses `scripts/tmux-send` to inject a pointer prompt into the session's tmux pane.
- The session sees the pointer (e.g., "[NOTIFY] You have N messages...") and acts on it.
- This is the **tmux-when-idle** path. Used only while `idle == 1`. The daemon does NOT clear idle; prompt/tool hooks clear it when the CLI becomes active again.
- The daemon's injection authorization rule is exactly one flag: inject if and only if `${NOTIFY_KEY_PREFIX:-taey}:SESSION:idle` exists.

For a canonical Taey seat, `idle` is a compatibility projection of zero attributable active turn IDs. Seat startup may publish that at-rest projection atomically only when its authoritative active-turn set is empty. It may not overwrite a non-empty active-turn set or infer idleness from pane appearance.

**This pair is the official + canonical pattern** for dispatching work to any tmux-session CLI peer in the fleet. An earlier subprocess-invocation approach was dropped in favor of full peer integration with hooks. No fallback paths exist; the central notification system uses hook context while active and tmux only when idle.

If a tmux peer cannot be reached via this dual path (e.g., the tmux session itself is dead), the right response is to respawn the session via `peer-respawn.sh`, NOT to fall back to a subprocess invocation.

## Per-CLI hook integration

All four supported CLIs run the same Redis state machine (`idle` / `last_activity` / `last_tool_activity` / `inbox` / `notifications`). The per-CLI hook variants in `hooks/` are thin wrappers that adapt to each CLI's distinct (a) config file location + format, (b) event-name vocabulary, (c) stdin/stdout envelope shape. All four call into the shared `hooks/_shared.py` helper which is the single source of truth for `action_pre_tool` / `action_post_tool` / `action_stop` / `action_user_prompt`.

| CLI | Config file | Format | Event names | Hook scripts |
|---|---|---|---|---|
| Claude Code | `~/.claude/settings.json` | JSON | `SessionStart` / `PreToolUse` / `PostToolUse` / `Stop` / `UserPromptSubmit` | `session_start.py` / `pre_tool_activity.py` / `check_notifications.py` / `stop_idle.py` / `prompt_activity.py` |
| OpenAI codex | `~/.codex/hooks.json` | JSON | `SessionStart` / `PreToolUse` / `PostToolUse` / `Stop` / `UserPromptSubmit` | `codex_session_start.py` / `codex_pre_tool.py` / `codex_post_tool.py` / `codex_stop.py` / `codex_user_prompt.py` |
| Google gemini | `~/.gemini/settings.json` | JSON | `BeforeTool` / `AfterTool` / `AfterAgent` / `BeforeAgent` | `gemini_before_tool.py` / `gemini_after_tool.py` / `gemini_after_agent.py` / `gemini_before_agent.py` |
| xAI grok | `~/.grok/hooks/cf-notify.json` | global file, no project trust required | `SessionStart` / `Stop` / `UserPromptSubmit` | `grok_session_start.py`, `grok_stop.py`, `grok_user_prompt.py` |

Use `bash scripts/install-hooks.sh --help` for the full installation matrix. `--apply` writes a timestamped backup of each affected config file before changing it; without `--apply` the script is a dry-run that prints unified diffs and exits.

> **Grok global hooks**: use the dedicated `~/.grok/hooks/cf-notify.json` file. This is the only way to close the boot gap cleanly, because `SessionStart` can mark `idle=1` before the first prompt. Global Grok hooks do not require project trust.

### Universal Stop+notify (v0.2.0+)

When a worker stops, its Stop hook runs `_shared.py:action_stop` which:

1. Sets `${NOTIFY_KEY_PREFIX:-taey}:<node>:idle=1` (load-bearing — this is the normal stopped-session setter).
2. Resolves the supervisor via `notifications.targets.resolve_supervisor`. If `NOTIFY_SUPERVISOR_IDS` lists `*-codex` sessions, those nodes are top-level (no suffix-strip back to the base Claude session) and the matching base Claude / `*-gemini` / `*-grok` workers resolve to that configured codex supervisor. Unset/blank `NOTIFY_SUPERVISOR_IDS` keeps the legacy rule: explicit `${NOTIFY_KEY_PREFIX:-taey}:<node>:parent` Redis key wins when it is not the node itself, else suffix-strip (`<name>-codex` / `<name>-gemini` / `<name>-grok` → `<name>`). Top-level sessions resolve to `None` and skip the parent-notify. Malformed `NOTIFY_SUPERVISOR_IDS` fails loud.
3. Reads `${NOTIFY_KEY_PREFIX:-taey}:<node>:current_task` (JSON `{task_id, description, supervisor, started_at}`, written by the dispatcher in [`claude-code-fleet-orchestrator`](https://github.com/palios-taey/claude-code-fleet-orchestrator)) + `${NOTIFY_KEY_PREFIX:-taey}:<node>:last_outcome` (JSON `{outcome, details}`, optionally set by the worker via `record_outcome()`).
4. Pushes a single `peer_idle` message to the supervisor's inbox with the outcome enum (`done | error | interrupted | unknown`) inline.
5. On outcome=done, atomically clears `current_task` + `last_outcome` via Lua compare-and-swap keyed on the observed `task_id` (so a concurrent dispatch racing past the Stop is not silently wiped) and writes a 30s-TTL `last_clear_was_done` marker the orchestrator's watchloop reads to distinguish done-clear from force-clear.

Any outcome other than `done` leaves `current_task` in place as the "previous dispatch did not complete cleanly" signal for the supervisor's next dispatch attempt.

`ALLOW_STOP` from the orchestrator stop-decision API means "do not block this worker from idling." It does **not** suppress lifecycle notification: if the worker still has `current_task` or `last_outcome` dispatch context, the Stop/AfterAgent hook still enqueues `peer_idle`.

The notify daemon does not run timer-based peer-inactive escalation. Task-level stalls are owned by the orchestrator worker-liveness path.

Explicit dispatch handoffs also carry an activation window (`CF_HANDOFF_ACTIVATION_SECS`, default 60s). The daemon marks a handoff activated when the target's heartbeat advances after dispatch, the target binds the dispatched `current_task`, or a handoff ack appears. If none happens before the deadline, the daemon enqueues `dispatch_activation_failed` to the dispatcher. This catches dispatches that reached Redis but never became active in the peer.

## How messages are delivered

Two paths:

1. **Active session, hook path**: `PostToolUse` drains queues after a tool call and shows full message bodies through `hookSpecificOutput.additionalContext`. Full content is allowed here because it is structured hook output, not shell text sent through tmux.

2. **Idle session, daemon path**: when `${NOTIFY_KEY_PREFIX:-taey}:SESSION:idle=1` exists and messages exist, the daemon injects only a pointer summary:

   ```text
   [NOTIFY] You have 3 messages (from <session-A>, docs-agent; first=ESCALATION). Read with: redis-cli -h 127.0.0.1 LRANGE ${NOTIFY_KEY_PREFIX:-taey}:SESSION:inbox 0 -1
   ```

The daemon does not pop messages. Full bodies stay in Redis. The recipient reads them on its next tool call via `PostToolUse`, on the next submitted prompt via `UserPromptSubmit`, or directly with `taey-ack`. Wake packets are fetched from the orchestrator on `SessionStart`, `UserPromptSubmit`, and notification-draining `PostToolUse` so scoped state arrives before the model acts.

Tmux is used only when the session is stopped and `idle=1`, and it carries only a pointer. Active sessions never receive notification bodies or pointers through tmux.

## Redis keys

`NOTIFY_KEY_PREFIX` defaults to `taey` for backward compatibility.

| Key | Purpose |
|---|---|
| `${NOTIFY_KEY_PREFIX:-taey}:SESSION:inbox` | inter-session queue, writers `LPUSH`, readers `RPOP` |
| `${NOTIFY_KEY_PREFIX:-taey}:SESSION:notifications` | monitor / worker queue, writers `RPUSH`, readers `LPOP` |
| `${NOTIFY_KEY_PREFIX:-taey}:notify:SESSION:orch` | auxiliary queue, writers `RPUSH`, readers `LPOP` |
| `${NOTIFY_KEY_PREFIX:-taey}:SESSION:idle` | durable idle flag set by lifecycle hooks, or by daemon repair for a parked Claude Code at-rest composer with old pending mail |
| `${NOTIFY_KEY_PREFIX:-taey}:SESSION:last_activity` | last hook activity timestamp, used for handoff activation observation |
| `${NOTIFY_KEY_PREFIX:-taey}:SESSION:last_tool_activity` | last tool-hook activity timestamp, used for handoff activation observation |
| `${NOTIFY_KEY_PREFIX:-taey}:SESSION:tool_running` | transient tool-running flag set by PreToolUse and cleared by PostToolUse |
| `${NOTIFY_KEY_PREFIX:-taey}:SESSION:tool_running_at` | epoch timestamp stamped with `tool_running=1`, used by orchestrator liveness to age-bound long-running tools |
| `${NOTIFY_KEY_PREFIX:-taey}:SESSION:turns_open` | Taey line-reader compatibility projection of attributable open-turn membership |
| `${NOTIFY_KEY_PREFIX:-taey}:SESSION:active_turns` | Taey line-reader sorted set of turn IDs scored by lease expiry epoch |
| `${NOTIFY_KEY_PREFIX:-taey}:_notify_daemon:heartbeat` | daemon process liveness timestamp, written from an independent heartbeat thread |
| `${NOTIFY_KEY_PREFIX:-taey}:_notify_daemon:delivery_progress` | daemon delivery-loop progress cursor, advanced by the serial tmux-delivery loop |

## State rules

| Flag | Who sets it | Who clears it |
|---|---|---|
| `${NOTIFY_KEY_PREFIX:-taey}:SESSION:idle` | `Stop`, `SessionStart`, and daemon at-rest reconciliation | `UserPromptSubmit`/`BeforeAgent` and tool-activity hooks |
| `${NOTIFY_KEY_PREFIX:-taey}:SESSION:last_activity` | hook activity | overwritten by later hook activity |
| `${NOTIFY_KEY_PREFIX:-taey}:SESSION:last_tool_activity` | tool-hook activity | overwritten by later tool-hook activity |
| `${NOTIFY_KEY_PREFIX:-taey}:SESSION:tool_running` | `PreToolUse`/`BeforeTool` | `PostToolUse`/`AfterTool` |
| `${NOTIFY_KEY_PREFIX:-taey}:SESSION:tool_running_at` | `PreToolUse`/`BeforeTool` with the same timestamp as `last_tool_activity` | `PostToolUse`/`AfterTool` |

The daemon's at-rest repair is intentionally narrow: it only restores `idle=1` for a local tmux pane whose current bottom region clearly shows a resting composer box, whose pending mail is older than the reconcile grace, and whose state has no fresh tool-running signal or active-turn marker such as `Esc to interrupt`. It is not a stale-activity or scrollback injection heuristic; ambiguous pane reads fail closed. After repair, the same one-flag `idle=1` authorization rule still decides pointer injection. The daemon never clears idle because tmux injection is not proof that the prompt was submitted.

The daemon heartbeat is not proof that pointer delivery is advancing. It is a
process-liveness signal emitted from an independent timer thread. Delivery
watchdogs should use `_notify_daemon:delivery_progress` to distinguish a slow
but advancing fan-out from a genuinely stalled delivery loop.

## Message format

```json
{
  "from": "sender_node_id",
  "type": "message|task|result|status|escalation|directive",
  "body": "human-readable text",
  "priority": "normal|high|critical",
  "msg_id": "unique-id-for-dedup",
  "timestamp": 1760000000.0
}
```

- `from` and `body` are required.
- `msg_id` supports operator debugging and future dedup behavior.
- `priority: "high"` prefixes displayed messages with `URGENT `.
- Never use `!!` or `!!!` as a priority marker; bash history expansion can break shell workflows.

## Sender identity

Every message carries a `from` node id. `taey-notify` resolves it in this precedence:

1. `--from <id>` — explicit per-call flag, always wins.
2. `TAEY_NODE_ID` environment variable.
3. tmux session name (`tmux display-message -p '#S'`).
4. hostname — last-resort fallback.

**Export `TAEY_NODE_ID=<role>` in every seat's environment.** Provenance should be
*declared*, not inferred. The session-name fallback is correct only when the tmux
session is named after the seat's role; a seat running in a differently-named session
silently stamps the wrong sender — a cannot-lie provenance defect that still routes
and delivers correctly, so it is easy to miss. `TAEY_NODE_ID` is the durable per-seat
fix; `--from` overrides a single call. Because the `from` label drives inbox
provenance (and a seat's `idle`/`inbox`/`current_task` keys resolve on the same node
id), a seat whose resolved id does not match its role is mis-seated — fix the id, and
confirm the seat is in the session it should be.

## Hook install model

Default behavior installs Claude Code hooks (which also enables Grok by inheritance — see "Per-CLI hook integration" above):

```bash
bash scripts/install-hooks.sh                 # dry-run, print Claude Code diff
bash scripts/install-hooks.sh --apply         # write after review
```

Add `--codex` / `--gemini` / `--all` to install additional CLIs' hooks in the same invocation:

```bash
bash scripts/install-hooks.sh --all --apply   # claude + codex + gemini
```

Each CLI's config file gets a timestamped backup before being written. `bash scripts/install-hooks.sh --help` for the full flag list.

## Daemon

Run exactly one daemon per machine that hosts tmux sessions:

```bash
bash /path/to/your/install/scripts/start_notify_daemons.sh start
bash /path/to/your/install/scripts/start_notify_daemons.sh status
bash /path/to/your/install/scripts/start_notify_daemons.sh stop
```

Configuration:

```bash
export REDIS_HOST=127.0.0.1
export REDIS_PORT=6379
export NOTIFY_KEY_PREFIX=taey
```

## Cleanup

Read and clear your own inbox:

```bash
taey-ack
```

Peek without clearing:

```bash
taey-ack --peek
```

## Anti-patterns

- Never set `idle=1` from ad hoc code paths; valid setters are `Stop`, `SessionStart`, and the daemon's narrow current-pane at-rest reconciliation.
- Never send full message bodies through tmux injection; use Redis plus hook `additionalContext`.
- Never clear queues with destructive `DELETE` during normal acknowledgement; drain with pops so messages arriving concurrently are not dropped.
- Do not relay messages through a central human or agent when the sender can target the recipient directly.
- Do not rely on the tmux session name for sender identity when a seat may run in a differently-named session; export `TAEY_NODE_ID` so `from` is declared, not guessed.
