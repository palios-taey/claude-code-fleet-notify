#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SOURCE_REPO="${SOURCE_REPO:-$SCRIPT_DIR/..}"
LIVE_PATH="${LIVE_PATH:-$HOME/.local/share/claude-code-fleet-notify/live}"
VERSION_ROOT="${VERSION_ROOT:-$HOME/.local/share/claude-code-fleet-notify/versions}"
DEPLOY_LOG="${DEPLOY_LOG:-$HOME/.taey/deploy-log.jsonl}"
TARGET_REF="${1:-}"

if [ -z "$TARGET_REF" ]; then
    echo "Usage: $0 <git-ref>" >&2
    exit 1
fi

mkdir -p "$VERSION_ROOT" "$(dirname "$DEPLOY_LOG")"
git -C "$SOURCE_REPO" fetch --tags origin >/dev/null 2>&1 || true
TARGET_SHA="$(git -C "$SOURCE_REPO" rev-parse --verify "${TARGET_REF}^{commit}")"
TARGET_NAME="${TARGET_REF//\//-}"
TARGET_DIR="$VERSION_ROOT/$TARGET_NAME"
CURRENT_REPO="$LIVE_PATH"

if [ -L "$LIVE_PATH" ]; then
    CURRENT_REPO="$(readlink -f "$LIVE_PATH")"
fi

if ! git -C "$CURRENT_REPO" diff --quiet || ! git -C "$CURRENT_REPO" diff --cached --quiet; then
    echo "Refusing deploy: uncommitted changes in $CURRENT_REPO" >&2
    exit 1
fi

# Idempotent worktree check: a git worktree's .git is a FILE (gitdir pointer), not a directory.
# Use `git rev-parse --git-dir` to validate worktree existence regardless of .git file/dir shape.
# If the worktree already exists and HEAD already matches $TARGET_SHA, skip recreation (true idempotence).
# Refuse to overwrite if $LIVE_PATH currently symlinks to this target.
CURRENT_TARGET_SHA=""
if git -C "$TARGET_DIR" rev-parse --git-dir >/dev/null 2>&1; then
    CURRENT_TARGET_SHA="$(git -C "$TARGET_DIR" rev-parse HEAD 2>/dev/null || true)"
fi
if [ "$CURRENT_TARGET_SHA" != "$TARGET_SHA" ]; then
    if [ -L "$LIVE_PATH" ] && [ "$(readlink -f "$LIVE_PATH")" = "$(readlink -f "$TARGET_DIR")" ]; then
        echo "Refusing: $TARGET_DIR is currently live but HEAD ($CURRENT_TARGET_SHA) != target ($TARGET_SHA)" >&2
        exit 1
    fi
    git -C "$SOURCE_REPO" worktree remove --force "$TARGET_DIR" >/dev/null 2>&1 || rm -rf "$TARGET_DIR"
    git -C "$SOURCE_REPO" worktree add --detach "$TARGET_DIR" "$TARGET_SHA" >/dev/null
fi

PREVIOUS_TARGET="$CURRENT_REPO"
if [ -L "$LIVE_PATH" ]; then
    PREVIOUS_TARGET="$(readlink -f "$LIVE_PATH")"
elif [ -d "$LIVE_PATH" ]; then
    BASELINE_DIR="$VERSION_ROOT/baseline-pre-stage-b"
    if [ -e "$BASELINE_DIR" ]; then
        # Conflict state: baseline already taken AND live is still real-dir.
        # If we ln -sfn over a real dir, the symlink lands INSIDE it (silent false-success).
        # Refuse loudly per cannot-lie + no-fallback discipline; operator must resolve.
        echo "Refusing bootstrap: $BASELINE_DIR exists but $LIVE_PATH is still a real directory (partial prior bootstrap)" >&2
        echo "Resolve manually: inspect both paths, decide which is canonical, then re-run deploy" >&2
        exit 1
    fi
    mv "$LIVE_PATH" "$BASELINE_DIR"
    ln -sfn "$BASELINE_DIR" "$LIVE_PATH"
    PREVIOUS_TARGET="$BASELINE_DIR"
fi

rollback() {
    # Audit-resilience: disable set -e inside rollback so a failed daemon
    # start cannot prevent the JSONL log line from being written. Cosmos
    # v1.3.0 full audit (2026-05-31) AMENDMENT 1: "a system that silently
    # drops its audit log during a failure state violates our foundational
    # transparency." The rollback path is best-effort — we want to record
    # what was attempted regardless of whether each step succeeded.
    set +e
    # Horizon v1.3.0 full audit AMENDMENT 5: explicit stop of any daemon
    # currently running against the failed target before reverting the
    # symlink. start_notify_daemons.sh's start is idempotent (refuses if
    # pidfile shows alive), so without an explicit stop a partially-started
    # daemon on the failed code path may persist while we flip the symlink
    # back, leaving fleet in a mixed state. Stop first → flip → start clean.
    bash "${DAEMON_CONTROL:-$LIVE_PATH/scripts/start_notify_daemons.sh}" stop >/dev/null
    DAEMON_STOP_RC=$?
    ln -sfn "$PREVIOUS_TARGET" "$LIVE_PATH"
    bash "${DAEMON_CONTROL:-$LIVE_PATH/scripts/start_notify_daemons.sh}" start >/dev/null
    DAEMON_RC=$?
    printf '{"ts":"%s","target":"%s","previous":"%s","outcome":"rollback","daemon_stop_rc":%d,"daemon_restart_rc":%d}\n' \
        "$(date -u +%FT%TZ)" "$TARGET_SHA" "$PREVIOUS_TARGET" "$DAEMON_STOP_RC" "$DAEMON_RC" >> "$DEPLOY_LOG"
}

trap 'rollback' ERR

bash "${DAEMON_CONTROL:-$LIVE_PATH/scripts/start_notify_daemons.sh}" stop >/dev/null
ln -sfn "$TARGET_DIR" "$LIVE_PATH"
# Post-flip assertion: LIVE_PATH must be a symlink (catches silent-success bugs where
# ln landed inside an existing real-dir instead of replacing it).
if [ ! -L "$LIVE_PATH" ] || [ "$(readlink -f "$LIVE_PATH")" != "$(readlink -f "$TARGET_DIR")" ]; then
    echo "Swap did not take: $LIVE_PATH is not a symlink to $TARGET_DIR" >&2
    exit 1
fi
bash "${DAEMON_CONTROL:-$LIVE_PATH/scripts/start_notify_daemons.sh}" start >/dev/null
sleep 2

STATUS="$(bash "${DAEMON_CONTROL:-$LIVE_PATH/scripts/start_notify_daemons.sh}" status)"
echo "$STATUS"
echo "$STATUS" | grep -q "\[UP\]"
python3 - <<'PY'
import os
import redis
redis.Redis(
    host=os.environ.get("REDIS_HOST", "127.0.0.1"),
    port=int(os.environ.get("REDIS_PORT", "6379")),
    decode_responses=True,
).ping()
print("redis_ok")
PY

trap - ERR
printf '{"ts":"%s","target":"%s","previous":"%s","outcome":"ok"}\n' \
    "$(date -u +%FT%TZ)" "$TARGET_SHA" "$PREVIOUS_TARGET" >> "$DEPLOY_LOG"
