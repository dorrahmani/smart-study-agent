#!/bin/bash
# Runs ONE WhatsApp "קלוד:" ("Claude:") task as the isolated `claudeagent` user (no sudo, can't read /home/ubuntu,
# .env or any key). Installed to /home/claudeagent/agent_job.sh; started by agent/claude_job.py.
#   agent_job.sh <job_id> [start|resume|restart] [session uuid]
#     start   (default) fresh copy of production main + the task (~/jobs/<id>/task.txt, ~/inbox/main.bundle)
#     resume  same session (--resume), prompt = ~/jobs/<id>/continue.txt; the working copy is left as it is
#     restart new session, task + continue.txt; the working copy is left as it is (the session could not be resumed)
# Produces ~/jobs/<id>/result.json. Changes stay on branch job-<id> in ~/ws — nothing reaches
# production unless the owner answers "1" (agent/claude_job.py then pushes through the normal test gate).
# AGENT_JOB_VERSION=2  (agent/claude_job.py reads this line: version 1 can't resume)
set -u
ID="$1"
MODE="${2:-start}"
SID="${3:-}"
J="$HOME/jobs/$ID"
cd "$HOME/ws" || exit 1
set -a; . "$HOME/.claude_token.env"; set +a
unset ANTHROPIC_API_KEY
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1   # red team 03/11: egress is Anthropic-only anyway

finish() {  # status, summary file
    python3 - "$J" "$1" <<'PY'
import json, subprocess, sys, pathlib
j, status = pathlib.Path(sys.argv[1]), sys.argv[2]
def sh(*a):
    return subprocess.run(a, capture_output=True, text=True).stdout.strip()
summary = (j / 'claude.txt').read_text(encoding='utf-8', errors='replace')[-3500:] if (j / 'claude.txt').exists() else ''
res = {'status': status, 'summary': summary,
       'diffstat': sh('git', 'diff', '--stat', 'base..HEAD')[-1500:],
       'commits': sh('git', 'log', '--oneline', 'base..HEAD')[-1500:],
       'tests': (j / 'tests.txt').read_text(encoding='utf-8', errors='replace')[-300:] if (j / 'tests.txt').exists() else ''}
(j / 'result.json').write_text(json.dumps(res, ensure_ascii=False), encoding='utf-8')
PY
}

fail() {  # why
    printf '%s\n' "$1" > "$J/claude.txt"; finish error; exit 1
}

rm -f .git/index.lock      # left by a git that was killed with the job before; one job at a time, so it's not anyone's
if [ "$MODE" = start ]; then
    # fresh, clean copy of production main. 05/11: with HEAD on `base` (a manual checkout on 03/11) git refuses to fetch
    # into the checked-out branch, and every job died in seconds with a bare "fetch failed". Detached, it can't.
    git checkout -q --detach 2>/dev/null
    if ! ERR="$(git fetch -q "$HOME/inbox/main.bundle" main:refs/heads/base --force 2>&1)"; then
        fail "git could not take the fresh code: $ERR"
    fi
    if ! ERR="$(git checkout -q -f -B "job-$ID" base 2>&1 && git clean -q -fdx -e venv 2>&1)"; then
        fail "git could not prepare the job's branch: $ERR"
    fi
elif [ "$(git rev-parse --abbrev-ref HEAD)" != "job-$ID" ]; then      # the parked work must be what's in front of Claude
    if ! ERR="$(git checkout -q "job-$ID" 2>&1)"; then
        fail "the working copy of this job is gone: $ERR"
    fi
fi

RULES="---
You are working on the owner's smart_study_agent repo (this directory), asked from WhatsApp. Rules:
- Follow the repo conventions (CLAUDE.md, existing style). LLM code = Gemini only, never Anthropic APIs.
- Make the change, then run the safety tests: ../venv/bin/python -m tests.test_safety_critical  (all must pass).
- Commit on the current branch with a clear message (no push — the owner approves deploys).
- Only a short list of paths can deploy from WhatsApp (WHATSAPP_DEPLOY_RX in agent/claude_job.py). Anything else, tests/
  included, needs the owner's computer: you may still prepare it, but say so in the summary.
- You cannot and must not touch secrets, .env, the server config or anything outside this directory.
- Finish with a SHORT summary in Hebrew for WhatsApp (max 8 lines): what changed, test result, anything the owner must know."

SESSION=()
case "$MODE" in
    resume)  SESSION=(--resume "$SID")
             PROMPT="$(cat "$J/continue.txt")" ;;
    restart) SESSION=(--session-id "$SID")
             PROMPT="$(cat "$J/task.txt")

$(cat "$J/continue.txt")

$RULES" ;;
    *)       [ -n "$SID" ] && SESSION=(--session-id "$SID")
             PROMPT="$(cat "$J/task.txt")

$RULES" ;;
esac

# stream-json is what says when a usage limit resets (rate_limit_event.resetsAt); agent/claude_job.py reads it.
# Only stderr lands in claude.txt, so a run that never started still explains itself.
timeout 1800 "$HOME/.local/bin/claude" -p "$PROMPT" --output-format stream-json --verbose --max-turns 60 \
    "${SESSION[@]}" --permission-mode acceptEdits \
    --allowedTools "Read" "Edit" "Write" "Glob" "Grep" "Bash(git *)" "Bash(../venv/bin/python *)" "Bash(python3 *)" "Bash(ls *)" "Bash(cat *)" "Bash(grep *)" \
    > "$J/stream.jsonl" 2> "$J/claude.txt"
RC=$?
PYTHONIOENCODING=utf-8 ../venv/bin/python -m tests.test_safety_critical 2>&1 | tail -1 > "$J/tests.txt"
if [ $RC -eq 124 ]; then finish timeout; elif [ $RC -ne 0 ]; then finish error; else finish done; fi
