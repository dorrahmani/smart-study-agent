#!/bin/bash
# Gemini on the owner's Google AI Pro subscription (03/11) through the Antigravity CLI, as the isolated `geminiagent` user.
# The prompt = one stream-json message on stdin (no argument-size limit). Plan mode in an empty folder, slash commands
# off. Plan mode is agy's own promise and it was never tested against a prompt that tries to act, so don't rely on it:
# what contains agy is this user (no sudo, no project, no .env) and its firewall.
# Installed as /usr/local/bin/ssa-run-agy (root, 755).
#   ssa-run-agy <model> <timeout seconds>   → agy's stream-json on stdout (utils/gemini_sub.py reads the result event)
set -u
cd "$HOME/empty" || exit 1
unset GEMINI_API_KEY GOOGLE_API_KEY GOOGLE_GENAI_USE_VERTEXAI
exec timeout $(( ${2:-300} + 10 )) "$HOME/bin/agy" --input-format stream-json --output-format stream-json \
    --model "${1:-gemini-3.8-flash-low}" --mode plan --disable-slash-commands --print-timeout "${2:-300}s" -p=
