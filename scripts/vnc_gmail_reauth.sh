#!/bin/bash
# vnc_gmail_reauth.sh — start xvfb + x11vnc + run Gmail reauth flow.
# User connects via VNC, sees Google OAuth in Chromium, logs in, token saved.
set -e

DISPLAY_NUM=1
export DISPLAY=":$DISPLAY_NUM"

# Clean stale locks
rm -f /tmp/.X${DISPLAY_NUM}-lock /tmp/.X11-unix/X${DISPLAY_NUM} 2>/dev/null || true

# Xvfb
Xvfb :$DISPLAY_NUM -screen 0 1280x800x24 &
sleep 1

# Window manager
fluxbox &
sleep 1

# VNC server
x11vnc -display :$DISPLAY_NUM -rfbport 5901 -rfbauth /home/ubuntu/.vnc/passwd \
       -forever -shared -bg -o /tmp/x11vnc.log -noxdamage

echo "✅ VNC ready on :5901"
echo "Connect via SSH tunnel: ssh -L 5901:127.0.0.1:5901 ubuntu@<your-server>"
echo "Then VNC to localhost:5901, password from config/secrets/vnc_password.txt"
echo ""
echo "Running Gmail reauth — Chrome will pop up with Google OAuth..."
echo "Log in to user6@example.com, grant access, then close Chrome."
echo ""

cd /home/ubuntu/smart_study_agent
/home/ubuntu/smart_study_agent/venv/bin/python3 scripts/reauth_gmail.py 2>&1

echo ""
echo "✅ Done! Cleaning up VNC..."
pkill -f 'Xvfb|x11vnc|fluxbox' 2>/dev/null || true
echo "VNC stopped. Gmail token refreshed at config/gmail_token.pickle"
