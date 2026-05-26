# Demo — claude-code-fleet-notify v0.1.0

`demo.gif` (and the source `demo.mp4`) show the full cross-instance notification + ack flow between two real Claude Code sessions:

1. **demo-alpha** is prompted with a quick task, responds with "online", then idles (Stop hook fires, sets `idle=1` in Redis)
2. **demo-beta** is prompted similarly, responds, and idles
3. A third-party shell runs `taey-notify demo-alpha "<message>" --from demo-beta` — the message is appended to demo-alpha's Redis inbox
4. The notification daemon (polling every 30s) sees `demo-alpha` is idle + has pending messages, injects a pointer prompt into demo-alpha's tmux pane via `tmux send-keys`
5. The next user prompt to demo-alpha triggers the UserPromptSubmit hook, which drains the inbox and surfaces the full message body via `additionalContext`
6. Claude in demo-alpha reads the instruction ("acknowledge by running …") and uses its bash tool to call `taey-notify demo-beta "ack from demo-alpha"`
7. demo-beta (also idle) receives the ack via the same mechanism; its next prompt drains and Claude confirms "Acknowledged — message from demo-alpha received"

The whole flow is autonomous — no human keystrokes between the initial prompts and the final ack. That's the differentiator vs interactive multi-session tools (see README on `claudemesh`).

## How this was recorded

Real Xvfb display + real xterm windows + real Claude Code sessions. The recording itself uses no simulation:

```bash
# Setup (creates the two tmux sessions with claude in each, attached via xterm on Xvfb :20)
bash demo/run-setup.sh

# Capture the demo flow into mp4 while ffmpeg records :20
ffmpeg -f x11grab -framerate 12 -video_size 1280x720 -i :20 \
    -c:v libx264 -preset ultrafast -pix_fmt yuv420p demo.mp4 &
FFMPEG_PID=$!
bash demo/run-demo.sh
kill -INT $FFMPEG_PID

# Convert to GIF for README embedding
ffmpeg -i demo.mp4 -vf "fps=8,scale=900:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse" demo.gif
```

Requires: Xvfb, xterm, ffmpeg, tmux, Claude Code installed.

The orchestration of "type into tmux pane" steps uses `tmux send-keys` with the same Claude Code Ink TUI submit chain documented in `scripts/tmux-send` — legacy Enter + Kitty-protocol CSI-u Enter (bytes `1b 5b 31 33 75`).
