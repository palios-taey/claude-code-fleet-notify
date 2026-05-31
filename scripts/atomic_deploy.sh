#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SOURCE_REPO="${SOURCE_REPO:-$SCRIPT_DIR/..}"
LIVE_PATH="${LIVE_PATH:-/path/to/repo}"
VERSION_ROOT="${VERSION_ROOT:-/path/to/repo}"
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

if [ ! -d "$TARGET_DIR/.git" ]; then
    rm -rf "$TARGET_DIR"
    git -C "$SOURCE_REPO" worktree add --detach "$TARGET_DIR" "$TARGET_SHA" >/dev/null
fi

PREVIOUS_TARGET="$CURRENT_REPO"
if [ -L "$LIVE_PATH" ]; then
    PREVIOUS_TARGET="$(readlink -f "$LIVE_PATH")"
elif [ -d "$LIVE_PATH" ]; then
    BASELINE_DIR="$VERSION_ROOT/baseline-pre-stage-b"
    if [ ! -e "$BASELINE_DIR" ]; then
        mv "$LIVE_PATH" "$BASELINE_DIR"
    fi
    ln -sfn "$BASELINE_DIR" "$LIVE_PATH"
    PREVIOUS_TARGET="$BASELINE_DIR"
fi

rollback() {
    ln -sfn "$PREVIOUS_TARGET" "$LIVE_PATH"
    bash "${DAEMON_CONTROL:-$LIVE_PATH/scripts/start_notify_daemons.sh}" start >/dev/null
    printf '{"ts":"%s","target":"%s","previous":"%s","outcome":"rollback"}\n' \
        "$(date -u +%FT%TZ)" "$TARGET_SHA" "$PREVIOUS_TARGET" >> "$DEPLOY_LOG"
}

trap 'rollback' ERR

bash "${DAEMON_CONTROL:-$LIVE_PATH/scripts/start_notify_daemons.sh}" stop >/dev/null
ln -sfn "$TARGET_DIR" "$LIVE_PATH"
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
