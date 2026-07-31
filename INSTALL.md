# Install Notes

The standard hook and daemon setup path remains in [README.md](README.md).

## User systemd service

Persistent notification-router service installation is documented in
[deploy/systemd/README.md](deploy/systemd/README.md). The committed unit replaces
the local-only `conductor-notify-router.service` definition with an
env-configured user service and keeps runtime-specific paths out of the unit
file.
