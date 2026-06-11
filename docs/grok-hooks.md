# Grok Hooks

Grok should use a dedicated global hook file at `~/.grok/hooks/cf-notify.json`.
This avoids boot-time dependence on Claude-compat project hook loading and closes
the cold-start idle gap.

## Install

1. Create the global hooks directory:

```bash
mkdir -p ~/.grok/hooks
```

2. Copy the template:

```bash
cp templates/grok/cf-notify.json ~/.grok/hooks/cf-notify.json
```

3. Either use the template default or set `CF_NOTIFY_HOOK_ROOT` before launching
Grok. It must point at the stable runtime hook copies written by
`scripts/install-hooks.sh --apply` (default
`~/.local/share/claude-code-fleet-notify/hooks-runtime/hooks/`), not at a
checkout — checkout paths can move; the runtime copies cannot.

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
