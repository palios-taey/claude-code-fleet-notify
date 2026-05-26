#!/bin/bash
# claude-code-fleet-notify v0.1.0 demo run.
# Pre-req: demo-alpha + demo-beta tmux sessions exist with claude running,
# attached via xterm on Xvfb :20. ffmpeg captures :20 while this runs.

set -e

# Ensure both panes are at fresh state (no stray text in input box)
tmux send-keys -t demo-alpha Escape 2>/dev/null || true
tmux send-keys -t demo-beta Escape 2>/dev/null || true
sleep 1

# Step 1: prompt demo-alpha with something quick. Claude responds, Stop hook fires, idle=1.
tmux send-keys -t demo-alpha -l "Reply with the single word: online"
sleep 0.3
tmux send-keys -t demo-alpha Enter
sleep 0.1
tmux send-keys -t demo-alpha -H 1b 5b 31 33 75
sleep 8

# Step 2: same for demo-beta.
tmux send-keys -t demo-beta -l "Reply with the single word: online"
sleep 0.3
tmux send-keys -t demo-beta Enter
sleep 0.1
tmux send-keys -t demo-beta -H 1b 5b 31 33 75
sleep 8

# Both now idle. Step 3: from THIS shell (a third party), send a message
# from demo-beta -> demo-alpha via the released taey-notify CLI.
# The notification daemon (already running on the fleet) sees idle=1 + pending
# inbox, injects a pointer prompt into demo-alpha's tmux pane.
sleep 2
/usr/local/bin/taey-notify demo-alpha \
    "Hi from demo-beta. Please acknowledge by running this command in your bash tool: /usr/local/bin/taey-notify demo-beta 'ack from demo-alpha'" \
    --from demo-beta
sleep 8  # daemon poll interval is up to 30s default; force visibility by waiting

# The pointer should be in demo-alpha's pane now. Simulate the user pressing
# Enter on an empty prompt to trigger UserPromptSubmit, which drains the inbox
# via the hook + surfaces the full message as additionalContext to the model.
# Claude then sees the instruction + runs the ack taey-notify via bash tool.
tmux send-keys -t demo-alpha Enter
sleep 0.1
tmux send-keys -t demo-alpha -H 1b 5b 31 33 75
sleep 20  # let claude read the notification + run the bash ack

# demo-beta is also idle. The ack should be in its inbox now. Simulate user
# pressing Enter to drain.
tmux send-keys -t demo-beta Enter
sleep 0.1
tmux send-keys -t demo-beta -H 1b 5b 31 33 75
sleep 12  # let demo-beta drain + acknowledge

# Done. Final pause for the video.
sleep 3
