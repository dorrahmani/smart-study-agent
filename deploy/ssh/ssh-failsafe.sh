#!/bin/bash
# SSH is reachable only through Tailscale (ufw: 22 on tailscale0; legacy iptables accepts removed 03/10/2026).
# Failsafe: 5 minutes after boot, if Tailscale isn't up, reopen SSH publicly (key-only, fail2ban) so the
# server is never locked out. Logged to the journal; a WhatsApp alert follows from the health checks.
sleep 300
if tailscale status >/dev/null 2>&1 && ip -4 addr show tailscale0 2>/dev/null | grep -q 'inet 100\.'; then
    echo "ssh-failsafe: tailscale up — SSH stays tailnet-only"
else
    echo "ssh-failsafe: TAILSCALE DOWN — reopening 22/tcp publicly"
    /usr/sbin/ufw allow 22/tcp comment "ssh-failsafe: tailscale was down at boot"
fi
