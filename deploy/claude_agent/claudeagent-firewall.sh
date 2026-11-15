#!/bin/bash
# Red team 03/11: the isolated `claudeagent` user (Claude Code jobs from WhatsApp) may talk only to Anthropic.
#   • no localhost (the dashboard runs as ubuntu, Ollama) — only DNS on 127.0.0.53
#   • no internet except Anthropic's API range (api.anthropic.com / claude.ai = 160.79.104.0/23, 2607:6bc0::/48)
# so a prompt-injected job can't reach the dashboard or send the subscription token / code anywhere else.
# Idempotent; run at boot by claudeagent-firewall.service (after ufw). Installed to /usr/local/sbin/.
set -eu
U=claudeagent
for T in iptables ip6tables; do
    $T -D OUTPUT -m owner --uid-owner $U -j CLAUDEAGENT_OUT 2>/dev/null || true
    $T -F CLAUDEAGENT_OUT 2>/dev/null || $T -N CLAUDEAGENT_OUT
    $T -A CLAUDEAGENT_OUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
done
iptables -A CLAUDEAGENT_OUT -d 127.0.0.53 -p udp --dport 53 -j ACCEPT
iptables -A CLAUDEAGENT_OUT -d 127.0.0.53 -p tcp --dport 53 -j ACCEPT
iptables -A CLAUDEAGENT_OUT -d 160.79.104.0/23 -p tcp --dport 443 -j ACCEPT
ip6tables -A CLAUDEAGENT_OUT -d 2607:6bc0::/48 -p tcp --dport 443 -j ACCEPT
for T in iptables ip6tables; do
    $T -A CLAUDEAGENT_OUT -m limit --limit 6/min -j LOG --log-prefix "claudeagent-blocked: "
    $T -A CLAUDEAGENT_OUT -j REJECT
    $T -I OUTPUT 1 -m owner --uid-owner $U -j CLAUDEAGENT_OUT
done
echo "claudeagent egress: Anthropic only"

# 03/11 `geminiagent` (Gemini CLI on the owner's Google AI Pro subscription, background jobs): Google's ranges are too wide to
# pin, so the internet is open — but never this machine (dashboard, Ollama) or the tailnet / private networks.
G=geminiagent
if id $G >/dev/null 2>&1; then
    for T in iptables ip6tables; do
        $T -D OUTPUT -m owner --uid-owner $G -j GEMINIAGENT_OUT 2>/dev/null || true
        $T -F GEMINIAGENT_OUT 2>/dev/null || $T -N GEMINIAGENT_OUT
        $T -A GEMINIAGENT_OUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    done
    iptables -A GEMINIAGENT_OUT -d 127.0.0.53 -p udp --dport 53 -j ACCEPT
    iptables -A GEMINIAGENT_OUT -d 127.0.0.53 -p tcp --dport 53 -j ACCEPT
    for net in 127.0.0.0/8 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 169.254.0.0/16; do
        iptables -A GEMINIAGENT_OUT -d $net -j REJECT
    done
    for net in ::1/128 fc00::/7 fe80::/10 fd7a:115c:a1e0::/48; do
        ip6tables -A GEMINIAGENT_OUT -d $net -j REJECT
    done
    for T in iptables ip6tables; do
        $T -A GEMINIAGENT_OUT -j ACCEPT
        $T -I OUTPUT 1 -m owner --uid-owner $G -j GEMINIAGENT_OUT
    done
    echo "geminiagent egress: internet, never localhost / tailnet / private"
fi
