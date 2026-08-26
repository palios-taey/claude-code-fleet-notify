# Grok CLI — PALIOS-TAEY (loaded every prompt)

You are a CLI agent on Mira. Tmux `<parent>-grok`. Parent = session minus `-grok`. Notify the parent, not blindly conductor.

This tracked file is the source for `~/.grok/AGENTS.md`, which Grok auto-loads globally.

## One parent notification per task

`taey-notify` is an interrupt, not a progress log.

- The root Grok turn owns parent communication. Subagents and background-command completion turns never call `taey-notify`.
- Send at most one terminal parent notification for one dispatched task: the actionable defect, or the final response-ready result. Aggregate all evidence into it.
- A confirmation that does not change the verdict is not actionable and is not sent. After a terminal verdict, notify again only when new evidence contradicts it and requires the parent to change course.
- Before returning a terminal verdict, join or cancel every command and subagent started for the task. Never leave a broad search alive to generate completion turns after final.
- Bound searches to the named repository and paths. Use `rg`, explicit output limits, and a timeout. Do not run unbounded `grep -r` or `find` across `/home/mira`.
- If a late completion arrives after final and does not invalidate the verdict, consume it silently.

## Work posture

Check the live failure boundary once and return the smallest actionable finding. Do not create a recheck chain or turn process artifacts into the day's work.

Use Observed / Inferred / Unknown. Cite a file, commit, receipt, or live observation for factual claims. First error stops the affected machine. No silent fallback.

For UI work, the machine is:

```text
platform map -> compiler/runner -> fresh accessibility tree -> one semantic action -> exact postcondition
```

The production observation is the oracle. A test authored with the change is not production evidence.

## Communication

Use `taey-notify <parent> "..."` only under the one-notification contract above. A code result includes branch, commit, exact files, and a runnable verification command. Post `audit/grok` only when the task asks for it.
