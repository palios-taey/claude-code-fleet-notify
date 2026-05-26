# claude-code-fleet-notify

Autonomous wake notifications for unattended Claude Code fleets.

Current version: v0.1.0

`claude-code-fleet-notify` gives each Claude Code tmux session a Redis inbox, four lifecycle hooks, and one local daemon. Active sessions receive full messages through hook `additionalContext`; stopped sessions are woken by a tmux-injected pointer prompt, while full message bodies remain in Redis.

## claudemesh

This is complementary to claudemesh, not a replacement. If you want interactive multi-session coordination, see claudemesh. If you want autonomous wake for unattended Claude Code fleets, use this.

The architectural split is the wake invariant: `Stop` sets durable idle, `UserPromptSubmit` is the only idle clearer, and the daemon only injects a pointer when `idle=1`. A v0.1.x/v0.2 backlog item is to propose that autonomous-wake invariant upstream to claudemesh.

## Install

```bash
git clone https://github.com/palios-taey/claude-code-fleet-notify.git
cd claude-code-fleet-notify
sudo make install
```

This installs:

- `taey-notify`
- `cc-fleet-notify`, an alias for `taey-notify`
- `taey-ack`

Redis and tmux are runtime requirements.

## Configure

```bash
export REDIS_HOST=127.0.0.1
export REDIS_PORT=6379
export NOTIFY_KEY_PREFIX=taey
```

`NOTIFY_KEY_PREFIX` defaults to `taey`. Use a different value when several fleets share one Redis instance.

## Install Hooks

Review the exact Claude Code settings diff:

```bash
bash scripts/install-hooks.sh
```

Apply after review:

```bash
bash scripts/install-hooks.sh --apply
```

The installer reads `~/.claude/settings.json`, prints a unified diff that adds `PreToolUse`, `PostToolUse`, `Stop`, and `UserPromptSubmit`, and writes a timestamped backup before applying. Without `--apply`, it writes nothing.

## Run The Daemon

Run one daemon per machine that hosts Claude Code tmux sessions:

```bash
bash scripts/start_notify_daemons.sh start
bash scripts/start_notify_daemons.sh status
bash scripts/start_notify_daemons.sh stop
```

## Usage

```bash
taey-notify session-b "build is ready"
cc-fleet-notify session-b "same command through the alias"
taey-notify session-b "production deploy failed" --type escalation
taey-notify session-b "cycle done" --type heartbeat --priority low
```

Read your own inbox:

```bash
taey-ack --peek
taey-ack
```

## Protocol

See [NOTIFICATION_PROTOCOL.md](NOTIFICATION_PROTOCOL.md).

The Redis key layout is:

```text
${NOTIFY_KEY_PREFIX:-taey}:SESSION:inbox
${NOTIFY_KEY_PREFIX:-taey}:SESSION:notifications
${NOTIFY_KEY_PREFIX:-taey}:notify:SESSION:orch
${NOTIFY_KEY_PREFIX:-taey}:SESSION:idle
${NOTIFY_KEY_PREFIX:-taey}:SESSION:tool_running
${NOTIFY_KEY_PREFIX:-taey}:SESSION:last_activity
```

## Testing

```bash
python3 -m py_compile identity.py notifications/*.py hooks/*.py tests/*.py
python3 -m unittest discover -s tests
```

or:

```bash
make test
```

## License

[Apache-2.0](LICENSE)
