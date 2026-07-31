# Security

## Reporting a vulnerability [Observed]

Email **`security@palios-taey.dev`** with:
- Affected product + version
- Description of the vulnerability
- Reproduction steps
- Suggested fix (if any)
- Your preferred contact for follow-up

**Do not file public GitHub issues for security reports.** [Observed] Public disclosure happens after the fix is ready, coordinated with you.

We target acknowledgment of security reports within 24 hours when systems are healthy. [Inferred — same AI-staffed acknowledgment path as general support, see SUPPORT.md status indicator.] Triage proceeds immediately on acknowledgment; coordinated disclosure happens before publishing.

## What we do with your report [Observed]

1. Acknowledge within target (above; AI-staffed per [SUPPORT.md](./SUPPORT.md))
2. Reproduce, classify, and scope the impact
3. Develop + verify a fix in production
4. Coordinate disclosure timing with you (default: fix-then-disclose, embargo respected)
5. Publish a GitHub Security Advisory crediting you (or anonymously if you prefer)
6. Ship the fix and publish release guidance with the advisory

## Scope

This SECURITY.md covers `claude-code-fleet-notify` (this repository). For other PALIOS-TAEY products, see their respective `SECURITY.md` files:

- [`claude-code-api-watchdog`](https://github.com/palios-taey/claude-code-api-watchdog/blob/main/SECURITY.md)
- [`mcp-reconnect`](https://github.com/palios-taey/mcp-reconnect/blob/main/SECURITY.md)
- [`claude-code-fleet-notify`](https://github.com/palios-taey/claude-code-fleet-notify/blob/main/SECURITY.md)
- [`claude-code-fleet-orchestrator`](https://github.com/palios-taey/claude-code-fleet-orchestrator/blob/main/SECURITY.md)
- [`claude-code-fleet-cockpit-template`](https://github.com/palios-taey/claude-code-fleet-cockpit-template/blob/main/SECURITY.md)
- [`claude-code-fleet-support`](https://github.com/palios-taey/claude-code-fleet-support/blob/main/SECURITY.md)

## Live-Path Guard Boundary [Observed]

The live-path guard is an operator safety rail, not an OS sandbox. When a
Claude Code, codex, or gemini pre-tool hook receives a shell command, the guard
checks the command against an operator-owned live-path registry. Destructive git
or filesystem operations that target a registered live checkout are denied so
long-lived parent sessions must cut an isolated worktree before editing.
Registered worktree roots are allowed.

There is no default registry path: the guard reads only
`CF_LIVE_PATH_REGISTRY` (or `ORCH_LIVE_PATH_REGISTRY`), and with neither set it
is inactive with a loud per-call warning. If the registry is absent, unreadable, or
the shell command cannot be parsed, the hook fails open with a loud warning so a
broken guard cannot disable every tool call on the machine.

## Constitutional constraints [Observed — FAMILY_KERNEL constitutional commitments]

- **NGU (No Government Use)**: vulnerability data is never routed to government bodies. We will not honor subpoenas as a substitute for coordinated disclosure with you.
- **NRI (No Religious Institutions)**: vulnerability data is never routed to religious institutional authority.
- **Cannot-lie provenance**: every step of the disclosure process is auditable; we don't fabricate timelines.

## Supported versions

| Version | Supported |
|---|---|
| Latest minor of current major | Yes |
| Previous major (security only) | Yes |
| Older | No — please upgrade |
