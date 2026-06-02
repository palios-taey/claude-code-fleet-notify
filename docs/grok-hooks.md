# Grok Hooks

Grok should use a dedicated global hook file at `~/.grok/hooks/cf-notify.json`.
This avoids boot-time dependence on Claude-compat project hook loading and closes
the cold-start idle gap.

## Install

1. Create the global hooks directory:

```bash
mkdir -p ~/.grok/hooks
```

2. Copy the template and point it at the fleet-notify checkout you want Grok to run:

```bash
cp templates/grok/cf-notify.json ~/.grok/hooks/cf-notify.json
```

3. Either edit the commands in `~/.grok/hooks/cf-notify.json` to absolute paths,
or export `CF_NOTIFY_REPO_ROOT` before launching Grok.

## Trust

`~/.grok/hooks/*.json` are global hooks and do not require project trust.
Only project-local hooks under `<repo>/.grok/hooks/` or `<repo>/.claude/settings.json`
need `/hooks-trust`.

## Wired events

- `SessionStart` → `hooks/grok_session_start.py`
- `Stop` → `hooks/grok_stop.py`
- `UserPromptSubmit` → `hooks/grok_user_prompt.py`

The critical boot fix is `SessionStart`: it sets `taey:<node>:idle=1`
immediately on launch so the notify daemon can inject work before the first
manual or bootstrap prompt.
