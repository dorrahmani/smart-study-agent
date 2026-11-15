#!/bin/bash
# vnc_notebooklm_login.sh — a temporary desktop on the server, to sign in to NotebookLM ON the server (14/10/26).
#
# Why: a NotebookLM session copied from the PC (storage_state.json) was dropped by Google within ~1 day — one
# session used from two IPs. Signing in here gives the server its own session, in its own profile.
# Safety: VNC listens on localhost only (reach it through an SSH tunnel), no extra port is opened; everything
# stops by itself after 30 minutes.
#
#   On the VM:   bash scripts/vnc_notebooklm_login.sh
#   On the PC:   ssh -i ~/.ssh/smart_study.key -N -L 5901:localhost:5901 ubuntu@<your-server>
#                "C:\Program Files\TightVNC\tvnviewer.exe" localhost::5901
#   After signing in:  venv/bin/python3 scripts/export_storage_from_profile.py   (then it's used by the bot)
set -e
DISPLAY_NUM=1
export DISPLAY=":$DISPLAY_NUM"
PROFILE="$HOME/.notebooklm/profiles/default/browser_profile"
CHROME=$(ls -d "$HOME"/.cache/ms-playwright/chromium-*/chrome-linux/chrome 2>/dev/null | tail -1)
[ -x "$CHROME" ] || { echo "no Playwright chromium found"; exit 1; }

pkill -f "Xvfb :$DISPLAY_NUM" 2>/dev/null || true
pkill -f "x11vnc -display :$DISPLAY_NUM" 2>/dev/null || true
pkill -x fluxbox 2>/dev/null || true
rm -f /tmp/.X${DISPLAY_NUM}-lock /tmp/.X11-unix/X${DISPLAY_NUM} 2>/dev/null || true
mkdir -p "$PROFILE"

Xvfb :$DISPLAY_NUM -screen 0 1280x800x24 >/dev/null 2>&1 &
sleep 1
fluxbox >/dev/null 2>&1 &
sleep 1
x11vnc -display :$DISPLAY_NUM -rfbport 5901 -localhost -nopw -forever -shared -bg -o /tmp/x11vnc.log -noxdamage
"$CHROME" --user-data-dir="$PROFILE" --no-sandbox --no-first-run --disable-features=Translate --start-maximized \
    https://notebooklm.google.com >/tmp/chromium.log 2>&1 &

# auto-stop after 30 minutes, whatever happens
( sleep 1800; pkill -f "user-data-dir=$PROFILE"; pkill -f "x11vnc -display :$DISPLAY_NUM"; pkill -x fluxbox; \
  pkill -f "Xvfb :$DISPLAY_NUM" ) >/dev/null 2>&1 &
disown -a
echo "✅ ready — VNC on localhost:5901 of the VM (SSH tunnel only). Stops by itself in 30 minutes."
