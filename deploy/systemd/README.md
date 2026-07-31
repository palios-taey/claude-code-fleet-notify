# systemd units

User unit files for the notification router, committed so the running daemon
definition is reproducible rather than local-only.

The unit intentionally requires an environment file at:

```bash
~/.config/claude-code-fleet-notify/notify-router.env
```

Copy the template and edit the placeholders for the target machine:

```bash
install -d -m 700 ~/.config/claude-code-fleet-notify
install -m 600 deploy/systemd/notify-router.env.example ~/.config/claude-code-fleet-notify/notify-router.env
cp deploy/systemd/conductor-notify-router.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now conductor-notify-router.service
systemctl --user status conductor-notify-router.service
```

The committed service keeps the live restart policy, timeout, and `MemorySwapMax`
property. It makes the checkout path, Python path, Redis endpoint, and key
prefix explicit env values so a bad install fails before the daemon starts.

Use `systemctl --user cat conductor-notify-router.service` after installation to
verify the loaded unit matches the committed file plus the local env file.
