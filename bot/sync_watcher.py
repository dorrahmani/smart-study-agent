"""
bot/sync_watcher.py — push-notification daemon.

Tails data/sync_log.json. When a new entry has n_new>0, posts a one-liner
to the user's Telegram chat. Designed to run alongside (or replaced by)
the main bot:

    python -m bot.sync_watcher

Stops cleanly with Ctrl-C.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

from config.paths import DATA_DIR

# Reuse the bot's env-loader.
from utils.env import _load_env_dotfile  # noqa: E402
_load_env_dotfile()

SYNC_LOG = DATA_DIR / 'sync_log.json'
STATE    = DATA_DIR / '.sync_watcher_state.json'

TOKEN    = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
USER_IDS = [
    int(x) for x in os.environ.get('TELEGRAM_USER_ID', '').split(',')
    if x.strip().lstrip('-').isdigit()
]


def _send(chat_id: int, text: str) -> bool:
    from notifier.telegram import send
    return send(text, markdown=True, chat_id=chat_id) is not None


def _read_state() -> str:
    if not STATE.exists():
        return ''
    try:
        return json.loads(STATE.read_text(encoding='utf-8')).get('last_seen', '')
    except Exception:
        return ''


def _write_state(last_seen: str) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(
        json.dumps({'last_seen': last_seen}, ensure_ascii=False),
        encoding='utf-8',
    )


def _format(entry: dict) -> str:
    lines = [
        f'🆕 *{entry.get("n_new", 0)} פריטים חדשים* '
        f'(${entry.get("total_cost_usd", 0):.3f})',
    ]
    for s in entry.get('steps', []):
        if (s.get('items') or 0) > 0:
            lines.append(f'• {s["name"]}: {s["items"]}')
    return '\n'.join(lines) or '🆕 sync done.'


def main() -> int:
    if not TOKEN:
        print('❌ TELEGRAM_BOT_TOKEN missing — aborting.')
        return 1
    if not USER_IDS:
        print('❌ TELEGRAM_USER_ID missing — aborting.')
        return 1
    if not SYNC_LOG.exists():
        print(f'⏳ waiting for {SYNC_LOG} to appear...')

    last_seen = _read_state()
    print(f'sync_watcher started. last_seen={last_seen!r}')

    while True:
        try:
            if SYNC_LOG.exists():
                log = json.loads(SYNC_LOG.read_text(encoding='utf-8'))
                if log:
                    latest = log[-1]
                    finished = latest.get('finished_at', '')
                    if finished and finished != last_seen and (latest.get('n_new') or 0) > 0:
                        msg = _format(latest)
                        for uid in USER_IDS:
                            _send(uid, msg)
                        last_seen = finished
                        _write_state(last_seen)
        except Exception as e:
            print(f'tick error: {e}')
        time.sleep(30)


if __name__ == '__main__':
    sys.exit(main())
