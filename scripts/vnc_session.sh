#!/bin/bash
# vnc_session.sh — start xvfb + fluxbox + x11vnc, then launch Chromium with
# the NotebookLM profile so the user can log in via VNC.
set -e

DISPLAY_NUM=1
export DISPLAY=":$DISPLAY_NUM"
PROFILE=/home/ubuntu/.notebooklm/browser_profile
CHROMIUM_BIN=/home/ubuntu/.cache/ms-playwright/chromium-1217/chrome-linux/chrome

# Clean stale lock files from previous runs
rm -f /tmp/.X${DISPLAY_NUM}-lock /tmp/.X11-unix/X${DISPLAY_NUM} 2>/dev/null || true

# 1. Start Xvfb (virtual display)
Xvfb :$DISPLAY_NUM -screen 0 1280x800x24 &
XVFB_PID=$!
sleep 1

# 2. Start window manager
fluxbox &
FLUX_PID=$!
sleep 1

# 3. Start VNC server, bound to all interfaces, with password
x11vnc -display :$DISPLAY_NUM -rfbport 5901 -rfbauth /home/ubuntu/.vnc/passwd \
       -forever -shared -bg -o /tmp/x11vnc.log -noxdamage
sleep 1

# 4. Launch Chromium with the NotebookLM persistent profile
"$CHROMIUM_BIN" \
    --user-data-dir="$PROFILE" \
    --no-sandbox \
    --no-first-run \
    --disable-features=Translate \
    --start-maximized \
    https://notebooklm.google.com \
    > /tmp/chromium.log 2>&1 &

echo "✅ VNC ready on :5901"
echo "PIDs: Xvfb=$XVFB_PID Fluxbox=$FLUX_PID"
echo "Connect a VNC client to <VM-IP>:5901"
echo "Logs: /tmp/x11vnc.log /tmp/chromium.log"

# Keep script alive so systemd or shell session sees it running
wait $XVFB_PID
