"""Node identity helpers.

Detection order:
1. ``TAEY_NODE_ID`` environment override
2. current tmux session name
3. machine hostname

The returned node id is the canonical identifier used for Redis keys such as
``${NOTIFY_KEY_PREFIX:-taey}:{node_id}:inbox``.
"""
import os
import socket
import subprocess

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv():
        return None


def _find_ancestor_tty() -> str:
    """Walk up the process tree to find the nearest ancestor with a real TTY."""
    pid = os.getpid()
    for _ in range(10):
        try:
            with open(f"/proc/{pid}/stat") as f:
                stat = f.read()
            after_comm = stat[stat.rfind(")") + 2 :]
            pid = int(after_comm.split()[1])
            if pid <= 1:
                break
            fd0 = os.readlink(f"/proc/{pid}/fd/0")
            if fd0.startswith("/dev/pts/") or fd0.startswith("/dev/tty"):
                return fd0
        except Exception:
            break
    return ""


def detect_tmux_session() -> str:
    """Detect which tmux session this process is running in."""
    try:
        ancestor_tty = _find_ancestor_tty()
        if ancestor_tty:
            result = subprocess.run(
                ["tmux", "list-panes", "-a", "-F", "#{pane_tty} #{session_name}"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode == 0:
                for line in result.stdout.strip().splitlines():
                    parts = line.split(" ", 1)
                    if len(parts) == 2 and parts[0] == ancestor_tty:
                        return parts[1]
    except Exception:
        pass
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "#S"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def detect_machine() -> str:
    """Detect machine hostname."""
    return socket.gethostname()


def detect_node_id() -> str:
    """Return the canonical node id for the current process."""
    explicit = os.environ.get("TAEY_NODE_ID")
    if explicit:
        return explicit
    session = detect_tmux_session()
    if session:
        return session
    return detect_machine()


def node_key(suffix: str) -> str:
    """Redis key scoped to this node."""
    prefix = os.environ.get("NOTIFY_KEY_PREFIX", "taey")
    return f"{prefix}:{detect_node_id()}:{suffix}"


def redis_connect(host=None, port=None):
    """Get a Redis connection using ``REDIS_HOST`` / ``REDIS_PORT`` defaults."""
    import redis

    load_dotenv()

    h = host or os.environ.get("REDIS_HOST", "127.0.0.1")
    p = port or int(os.environ.get("REDIS_PORT", "6379"))
    return redis.Redis(host=h, port=p, decode_responses=True, socket_timeout=2)
