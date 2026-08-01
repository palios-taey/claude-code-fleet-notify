.PHONY: install uninstall syntax hooks-diff hooks-install hooks-safe-edit daemon-start daemon-stop daemon-status

PREFIX ?= /usr/local
# Live hooks-loading path the fleet daemon + hook subprocesses read from.
# Override per-adopter to the deployed hooks checkout you want to protect.
LIVE_HOOKS_PATH ?=

install:
	@echo "Installing claude-code-fleet-notify CLIs to $(PREFIX)/bin..."
	@install -d "$(PREFIX)/bin"
	@install -m 755 scripts/taey-notify "$(PREFIX)/bin/taey-notify"
	@ln -sf "$(PREFIX)/bin/taey-notify" "$(PREFIX)/bin/cc-fleet-notify"
	@install -m 755 scripts/taey-ack "$(PREFIX)/bin/taey-ack"
	@install -m 755 scripts/tmux-send "$(PREFIX)/bin/tmux-send"
	@install -m 755 scripts/start_notify_daemons.sh "$(PREFIX)/bin/start_notify_daemons.sh"
	@echo "Done. Run 'taey-notify --help' and 'taey-ack --help' to verify."

uninstall:
	@echo "Removing claude-code-fleet-notify CLIs from $(PREFIX)/bin..."
	@rm -f "$(PREFIX)/bin/taey-notify" "$(PREFIX)/bin/cc-fleet-notify" \
	       "$(PREFIX)/bin/taey-ack" "$(PREFIX)/bin/tmux-send" \
	       "$(PREFIX)/bin/start_notify_daemons.sh"
	@echo "Done."

hooks-diff:
	@bash scripts/install-hooks.sh

hooks-install:
	@bash scripts/install-hooks.sh --apply

# Hot-deploy class guard (Logos Stage B suggestion 2): refuse to operate on the
# live hooks-loading path. Hooks reload from disk on every Stop event, so
# editing in-place activates code without restart. Use a worktree instead:
#   git worktree add ~/.dev-worktrees/<name> <branch>
# Resolves symlinks on both sides so a flipped live symlink is still detected.
# Set LIVE_HOOKS_PATH to match your deploy when you want this guard enabled.
hooks-safe-edit:
	@PWD_RESOLVED=$$(pwd -P); \
	if [ -z "$(LIVE_HOOKS_PATH)" ]; then \
		echo "REFUSED: LIVE_HOOKS_PATH is unset." >&2; \
		echo "Set LIVE_HOOKS_PATH to the deployed hooks checkout you want to protect." >&2; \
		exit 1; \
	fi; \
	if [ -e "$(LIVE_HOOKS_PATH)" ]; then \
		LIVE_RESOLVED=$$(cd "$(LIVE_HOOKS_PATH)" && pwd -P); \
	else \
		LIVE_RESOLVED=""; \
	fi; \
	if [ -n "$$LIVE_RESOLVED" ] && [ "$$PWD_RESOLVED" = "$$LIVE_RESOLVED" ]; then \
		echo "REFUSED: cwd ($$PWD_RESOLVED) is the live hooks-loading path." >&2; \
		echo "Hooks reload from disk every Stop event — in-place edits activate without restart." >&2; \
		echo "Edit in a worktree instead: git worktree add ~/.dev-worktrees/<name> <branch>" >&2; \
		exit 1; \
	fi; \
	echo "Safe to edit: cwd=$$PWD_RESOLVED is NOT the live hooks path ($(LIVE_HOOKS_PATH))"

daemon-start:
	@bash scripts/start_notify_daemons.sh start

daemon-stop:
	@bash scripts/start_notify_daemons.sh stop

daemon-status:
	@bash scripts/start_notify_daemons.sh status

syntax:
	@echo "Syntax check..."
	@python3 -m py_compile identity.py notifications/*.py hooks/*.py scripts/_stage_b_api.py scripts/taey-*
	@bash -n scripts/start_notify_daemons.sh scripts/tmux-send scripts/install-hooks.sh scripts/atomic_deploy.sh
