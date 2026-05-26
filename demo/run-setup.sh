#!/bin/bash
# claude-code-fleet-notify v0.1.0 demo setup — spawn two side-by-side xterms
# on Xvfb :20, each running claude in its own tmux session (demo-alpha,
# demo-beta). The live notification daemon picks them up + delivers messages.
# ffmpeg captures :20 to mp4 while the demo flow runs.

set -e

# Ensure Xvfb :20 is up (started earlier)
if ! pgrep -f "Xvfb :20" > /dev/null; then
    echo "ERROR: Xvfb :20 not running" >&2
    exit 1
fi

# Clean prior demo state (safely — no pkill -f with names that match our own bash)
for s in demo-alpha demo-beta; do
    tmux kill-session -t "$s" 2>/dev/null || true
done
# Kill any prior xterm on :20
for pid in $(pgrep xterm 2>/dev/null); do
    if grep -q ":20" "/proc/$pid/environ" 2>/dev/null; then
        kill "$pid" 2>/dev/null || true
    fi
done
sleep 1

# Pre-create both tmux sessions detached
tmux new-session -d -s demo-alpha -x 80 -y 40 -c /home/mira/the-conductor
tmux send-keys -t demo-alpha "claude --dangerously-skip-permissions" Enter

tmux new-session -d -s demo-beta -x 80 -y 40 -c /home/mira/the-conductor
tmux send-keys -t demo-beta "claude --dangerously-skip-permissions" Enter

echo "demo sessions created; waiting 16s for claude startups..."
sleep 16

# Launch two xterms on :20, side-by-side, each attached to its tmux session
DISPLAY=:20 xterm -geometry 80x40+0+0 -fa "DejaVu Sans Mono" -fs 11 \
    -title "demo-alpha" -e tmux attach -t demo-alpha &
XTERM_A=$!
sleep 1
DISPLAY=:20 xterm -geometry 80x40+640+0 -fa "DejaVu Sans Mono" -fs 11 \
    -title "demo-beta" -e tmux attach -t demo-beta &
XTERM_B=$!
sleep 2

echo "xterm A pid: $XTERM_A, xterm B pid: $XTERM_B"
echo "demo environment ready. Demo flow follows in run_demo.sh."
