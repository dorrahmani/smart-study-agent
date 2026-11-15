"""scripts/brain_sync.py — nightly: the vault's "🧠 המוח" notes → the brain notebook in NotebookLM (agent/brain.py).

Cron (VM): 40 1 * * *  (04:40 Israel) via run_with_alert.sh. Only changed notes are uploaded; no LLM, no API cost.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env', override=True)
except ImportError:
    pass


def main() -> int:
    from agent import brain, nlm_ask
    if nlm_ask._broken_recently():
        print('skip: NotebookLM failed in the last hours (circuit breaker)')
        return 0
    try:
        res = brain.sync()
    except Exception as e:
        nlm_ask._mark(True)
        print(f'brain sync failed: {e!r}'[:300])
        return 1
    nlm_ask._mark(False)
    print(f"brain sync: {res['uploaded']} uploaded, {res['deleted']} replaced")
    try:                                    # 18/10: the probability course notebook follows the lecturer's site
        from collectors.lecturer_site import sync_notebook
        print(f'probability site notebook: {sync_notebook("probability")}')
    except Exception as e:
        print(f'probability site notebook failed: {e!r}'[:300])
    try:                                    # 17/10: the friends' project notebook follows its GitHub repo
        from agent import starter_help
        print(f'starter help: {starter_help.sync()}')
    except Exception as e:
        print(f'starter help sync failed: {e!r}'[:300])
    return 0


if __name__ == '__main__':
    sys.exit(main())
