.PHONY: install uninstall test hooks-diff hooks-install daemon-start daemon-stop daemon-status

PREFIX ?= /usr/local

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

daemon-start:
	@bash scripts/start_notify_daemons.sh start

daemon-stop:
	@bash scripts/start_notify_daemons.sh stop

daemon-status:
	@bash scripts/start_notify_daemons.sh status

test:
	@echo "Syntax check..."
	@python3 -m py_compile identity.py notifications/*.py hooks/*.py tests/*.py
	@bash -n scripts/start_notify_daemons.sh scripts/tmux-send scripts/install-hooks.sh
	@echo "Unit tests..."
	@python3 -m unittest discover -s tests
