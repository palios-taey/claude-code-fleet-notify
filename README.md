# claude-code-fleet-notify

![demo](demo/demo.gif)

> *Real cross-instance notification + ack flow between two live Claude Code sessions. See `demo/README.md` for the recording setup.*


Turn scattered AI terminals into a supervised tmux fleet: dispatch work to Claude Code, Codex, Gemini, Grok, or any **hookable** REPL CLI, then get `done`/`error`/`interrupted` outcomes back inline so the supervisor can update the plan instead of babysitting panes.

Current version: v0.2.3

`claude-code-fleet-notify` gives each terminal-native, hookable CLI session a Redis inbox, four lifecycle hooks, and one local daemon. Active sessions receive full messages through hook `additionalContext`; stopped sessions are woken by a tmux-injected pointer prompt, while full message bodies remain in Redis.

## Scope (what this is and what this isn't)

In scope:
- **Terminal-native REPL CLIs that expose hook events**: Claude Code, OpenAI codex, Google gemini (field-verified), xAI grok (via Claude Code config inheritance, field-verified).
- Single-machine tmux fleets where the daemon, hooks, and supervisor sessions share one Redis.

Out of scope (will fail silently or partially — adopters: don't):
- **IDE-embedded agents** (Cursor, Continue, GitHub Copilot Workspace, etc.) — extension-host IPC sits below the tmux/process boundary our hooks observe, so critical task state transitions are missed.
- **Many-to-many distributed graph topologies** — the supervisor↔worker abstraction here is point-to-point with optional multi-level via explicit `parent` override. Fanout/aggregate workflows need a different layer.
- **Non-hookable REPLs** — if a CLI has no equivalent of Stop / UserPromptSubmit / Pre+PostToolUse (or comparable lifecycle events), the universal Stop+notify primitive has nothing to attach to and the daemon's pointer injection won't have a UserPromptSubmit hook on the receiving side to drain the inbox.

> **Integration verification status**: Claude Code (field-verified, the original target). xAI grok (field-verified, `grok inspect` shows the 4 inherited hooks). OpenAI codex (integration-tested via the per-CLI hook variants; field-verified on the Mira fleet via per-parent peers). Google gemini (integration-tested via the per-CLI hook variants; field-verified via the same per-parent peers, with the known BeforeTool/AfterTool event-name mapping).

## Supported CLIs

| CLI | Config file | Event names | Install command |
|---|---|---|---|
| Claude Code | `~/.claude/settings.json` | `PreToolUse` / `PostToolUse` / `Stop` / `UserPromptSubmit` | `install-hooks.sh --apply` |
| OpenAI codex | `~/.codex/hooks.json` | same as Claude Code | `install-hooks.sh --codex --apply` |
| Google gemini | `~/.gemini/settings.json` | `BeforeTool` / `AfterTool` / `BeforeAgent` / `AfterAgent` | `install-hooks.sh --gemini --apply` |
| xAI grok | inherits `~/.claude/settings.json` automatically | same as Claude Code | *(no separate install; covered by Claude Code install)* |

All four CLIs share the same Redis state machine via per-CLI hook variants that route through one shared `hooks/_shared.py` helper. The supervisor-worker primitive (v0.2.0 universal Stop+notify) works identically across them: when a worker stops, its Stop hook resolves the supervisor (via `taey:<worker>:parent` override or `<name>-codex` / `<name>-gemini` / `<name>-grok` suffix-strip), reads `taey:<worker>:current_task` (set by the dispatcher) + `taey:<worker>:last_outcome` (optionally set by the worker), and pushes a single `peer_idle` message with the outcome inline.

> The supervisor-worker dispatch + plan/task tracking + recurring-runner pieces ship in the companion product [`claude-code-fleet-orchestrator`](https://github.com/palios-taey/claude-code-fleet-orchestrator), which depends on this package.

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

Default behavior is Claude Code only (which also enables Grok by inheritance, see CLI table above):

```bash
bash scripts/install-hooks.sh                # dry-run, print diff
bash scripts/install-hooks.sh --apply        # write changes after review
```

For codex and/or gemini, pass the corresponding flags:

```bash
bash scripts/install-hooks.sh --codex --apply             # + codex
bash scripts/install-hooks.sh --gemini --apply            # + gemini
bash scripts/install-hooks.sh --all --apply               # claude + codex + gemini (+ grok by inheritance)
```

Each CLI's settings file gets a timestamped backup before being written. Without `--apply`, the installer is dry-run only — it prints the unified diff and writes nothing. `bash scripts/install-hooks.sh --help` for the full flag list.

> **Grok** (xAI `grok-cli`) requires no separate install. It reads its hook configuration from `~/.claude/settings.json` automatically — verified via `grok inspect` which shows `Hooks (4)` sourced from the Claude Code path. Installing Claude Code hooks therefore enables Grok as well.

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
