# Notification Protocol

This is the protocol reference for `claude-code-fleet-notify`.

## How to send a message

```bash
taey-notify <session-B> "message body"
cc-fleet-notify <session-B> "same sender, alias CLI"
```

Or direct Redis, when the CLI is unavailable:

```bash
redis-cli -h 127.0.0.1 LPUSH "${NOTIFY_KEY_PREFIX:-taey}:<session-B>:inbox" '{"from":"<session-A>","type":"message","body":"text","msg_id":"unique-id"}'
```

Targets are arbitrary strings. There is no allowlist. A practical fleet might use names such as `<session-A>`, `<session-B>`, `docs-agent`, or `build-worker`.

## Participant types

- **tmux-session participants** are REPL-CLI sessions with the four hooks installed. Supported CLIs: Claude Code, OpenAI codex, Google gemini, and xAI grok (see "Per-CLI hook integration" below for config-file paths and event-name mappings). The daemon injects a pointer prompt through tmux only when the session is stopped and marked idle.
- **headless participants** read Redis directly. They do not use hooks, tmux injection, or `idle` state. They poll `${NOTIFY_KEY_PREFIX:-taey}:<name>:inbox` and reply with `taey-notify <target> --from <name>`.
- **peer defect/status/result reports** may omit `<target>`. In that path,
  `NOTIFY_TARGET` wins as an explicit override; otherwise the CLI derives the
  same parent as the Stop hook. A per-parent peer such as `weaver-codex`
  therefore reports to `weaver`, not `conductor`.

## Dual delivery path — the canonical dispatch pattern for tmux participants

For every tmux-session participant (Claude Code / codex / gemini / grok), notifications reach the session through ONE of two paths depending on the session's current state. No subprocess invocation, no fallback paths; these two paths cover the full state space.

**Path A — Active (session is making tool calls)**:
- Notifications are delivered as `hookSpecificOutput.additionalContext` via the PostToolUse hook (`hooks/check_notifications.py`).
- The session sees the notification body in its next prompt context without any tmux involvement.
- This is the **injection-during-tool-use** path. Used while `idle != 1`.

**Path B — Idle (session has stopped, `idle=1` set by Stop/SessionStart or usage-limit reconciliation)**:
- The notification daemon (`notifications/daemon.py`) polls Redis, detects pending notifications for an idle session, and uses `scripts/tmux-send` to inject a pointer prompt into the session's tmux pane.
- The session sees the pointer (e.g., "[NOTIFY] You have N messages...") and acts on it.
- This is the **tmux-when-idle** path. Used only while `idle == 1`. The daemon does NOT clear idle; prompt/tool hooks clear it when the CLI becomes active again.
- The daemon's injection authorization rule is exactly one flag: inject if and only if `${NOTIFY_KEY_PREFIX:-taey}:SESSION:idle` exists.

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
2. Resolves the supervisor via two-mechanism rule: explicit `${NOTIFY_KEY_PREFIX:-taey}:<node>:parent` Redis key wins, else suffix-strip (`<name>-codex` / `<name>-gemini` / `<name>-grok` → `<name>`). Top-level sessions resolve to `None` and skip the parent-notify.
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
| `${NOTIFY_KEY_PREFIX:-taey}:SESSION:idle` | durable idle flag set by lifecycle hooks, or by daemon repair for a parked Claude Code usage-limit banner |
| `${NOTIFY_KEY_PREFIX:-taey}:SESSION:last_activity` | last hook activity timestamp, used for handoff activation observation |
| `${NOTIFY_KEY_PREFIX:-taey}:SESSION:last_tool_activity` | last tool-hook activity timestamp, used for handoff activation observation |

## State rules

| Flag | Who sets it | Who clears it |
|---|---|---|
| `${NOTIFY_KEY_PREFIX:-taey}:SESSION:idle` | `Stop`, `SessionStart`, and daemon usage-limit reconciliation | `UserPromptSubmit`/`BeforeAgent` and tool-activity hooks |
| `${NOTIFY_KEY_PREFIX:-taey}:SESSION:last_activity` | hook activity | overwritten by later hook activity |
| `${NOTIFY_KEY_PREFIX:-taey}:SESSION:last_tool_activity` | tool-hook activity | overwritten by later tool-hook activity |

The daemon's usage-limit repair is intentionally narrow: it only restores `idle=1` for a local tmux pane that visibly shows a Claude Code session/weekly/usage-limit banner while no tool is marked running. It is not a stale-activity or pane-active injection heuristic; after repair, the same one-flag `idle=1` authorization rule still decides pointer injection. The daemon never clears idle because tmux injection is not proof that the prompt was submitted.

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

- Never set `idle=1` outside the `Stop` hook.
- Never send full message bodies through tmux injection; use Redis plus hook `additionalContext`.
- Never clear queues with destructive `DELETE` during normal acknowledgement; drain with pops so messages arriving concurrently are not dropped.
- Do not relay messages through a central human or agent when the sender can target the recipient directly.
