#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

git init -q "$TMP/live"
git -C "$TMP/live" config user.email "repro@example.invalid"
git -C "$TMP/live" config user.name "atomic deploy repro"
mkdir -p "$TMP/live/scripts"
printf '#!/usr/bin/env bash\ncase "${1:-}" in\n  status) echo "[UP] repro-daemon" ;;\n  *) exit 0 ;;\nesac\n' > "$TMP/live/scripts/start_notify_daemons.sh"
chmod +x "$TMP/live/scripts/start_notify_daemons.sh"
printf 'one\n' > "$TMP/live/payload.txt"
git -C "$TMP/live" add .
git -C "$TMP/live" commit -q -m "first"
REF_ONE="$(git -C "$TMP/live" rev-parse HEAD)"

printf 'two\n' > "$TMP/live/payload.txt"
git -C "$TMP/live" add payload.txt
git -C "$TMP/live" commit -q -m "second"
REF_TWO="$(git -C "$TMP/live" rev-parse HEAD)"

mkdir -p "$TMP/bin"
cat > "$TMP/bin/python3" <<'PY'
#!/usr/bin/env bash
cat >/dev/null
echo redis_ok
PY
chmod +x "$TMP/bin/python3"

export PATH="$TMP/bin:$PATH"
export LIVE_PATH="$TMP/live"
export VERSION_ROOT="$TMP/versions"
export DEPLOY_LOG="$TMP/deploy-log.jsonl"
export DAEMON_CONTROL="$TMP/live/scripts/start_notify_daemons.sh"
export SOURCE_REPO="$TMP/live"

bash "$ROOT/scripts/atomic_deploy.sh" "$REF_ONE" >/tmp/atomic-deploy-first.out

BASELINE="$VERSION_ROOT/baseline-pre-stage-b"
export SOURCE_REPO="$BASELINE"
export DAEMON_CONTROL="$BASELINE/scripts/start_notify_daemons.sh"

bash "$ROOT/scripts/atomic_deploy.sh" "$REF_TWO" >/tmp/atomic-deploy-second.out

test "$(git -C "$(readlink -f "$LIVE_PATH")" rev-parse HEAD)" = "$REF_TWO"
test "$(git -C "$(readlink -f "$VERSION_ROOT/${REF_ONE}")" rev-parse HEAD)" = "$REF_ONE"
test "$(git -C "$(readlink -f "$VERSION_ROOT/${REF_TWO}")" rev-parse HEAD)" = "$REF_TWO"
test -d "$VERSION_ROOT/${REF_ONE}/.git"
test -d "$VERSION_ROOT/${REF_TWO}/.git"
grep -q '"outcome":"ok"' "$DEPLOY_LOG"

echo "atomic_deploy_redeploy_ok"
