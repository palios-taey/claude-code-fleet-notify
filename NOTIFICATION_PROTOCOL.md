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

- **tmux-session participants** are Claude Code sessions with the four hooks installed. The daemon injects a pointer prompt through tmux only when the session is stopped and marked idle.
- **headless participants** read Redis directly. They do not use hooks, tmux injection, `idle`, or `tool_running` state. They poll `${NOTIFY_KEY_PREFIX:-taey}:<name>:inbox` and reply with `taey-notify <target> --from <name>`.

## How messages are delivered

Two paths:

1. **Active session, hook path**: `PostToolUse` drains queues after a tool call and shows full message bodies through `hookSpecificOutput.additionalContext`. Full content is allowed here because it is structured hook output, not shell text sent through tmux.

2. **Idle session, daemon path**: when the `Stop` hook has set `${NOTIFY_KEY_PREFIX:-taey}:SESSION:idle=1` and messages exist, the daemon injects only a pointer summary:

   ```text
   [NOTIFY] You have 3 messages (from <session-A>, docs-agent; first=ESCALATION). Read with: redis-cli -h 127.0.0.1 LRANGE ${NOTIFY_KEY_PREFIX:-taey}:SESSION:inbox 0 -1
   ```

The daemon does not pop messages. Full bodies stay in Redis. The recipient reads them on its next tool call via `PostToolUse`, on the next submitted prompt via `UserPromptSubmit`, or directly with `taey-ack`.

Tmux is used only when the session is stopped and `idle=1`, and it carries only a pointer. Active sessions never receive notification bodies through tmux.

## Redis keys

`NOTIFY_KEY_PREFIX` defaults to `taey` for backward compatibility.

| Key | Purpose |
|---|---|
| `${NOTIFY_KEY_PREFIX:-taey}:SESSION:inbox` | inter-session queue, writers `LPUSH`, readers `RPOP` |
| `${NOTIFY_KEY_PREFIX:-taey}:SESSION:notifications` | monitor / worker queue, writers `RPUSH`, readers `LPOP` |
| `${NOTIFY_KEY_PREFIX:-taey}:notify:SESSION:orch` | auxiliary queue, writers `RPUSH`, readers `LPOP` |
| `${NOTIFY_KEY_PREFIX:-taey}:SESSION:idle` | durable idle flag set only by `Stop` |
| `${NOTIFY_KEY_PREFIX:-taey}:SESSION:tool_running` | transient tool-running flag with 60s TTL |
| `${NOTIFY_KEY_PREFIX:-taey}:SESSION:last_activity` | last hook activity timestamp |

## State rules

| Flag | Who sets it | Who clears it |
|---|---|---|
| `${NOTIFY_KEY_PREFIX:-taey}:SESSION:idle` | **Only** the `Stop` hook | `UserPromptSubmit` hook |
| `${NOTIFY_KEY_PREFIX:-taey}:SESSION:tool_running` | `PreToolUse` hook | `PostToolUse` hook, `Stop` hook, or TTL |
| `${NOTIFY_KEY_PREFIX:-taey}:SESSION:last_activity` | hook activity | overwritten by later hook activity |

Nothing else sets `idle=1`. The daemon never clears idle because tmux injection is not proof that the prompt was submitted.

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

Use the installer to review the exact settings change:

```bash
bash scripts/install-hooks.sh
```

To apply it:

```bash
bash scripts/install-hooks.sh --apply
```

`--apply` writes a timestamped backup of `~/.claude/settings.json` before changing it. Without `--apply`, the script prints a unified diff and exits without writing.

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
