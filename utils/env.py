"""utils/env.py — the project's .env loader (no external dependency).

10/11: scripts used to `from bot.telegram_bot import _load_env_dotfile`, which imported the Telegram bot module at
runtime only for this. Telegram is gone; the loader and that import's side effects (Gemini over GOOGLE_API_KEY, INFO
logging without httpx's per-URL lines) live here, so nothing imports bot/telegram_bot.py any more.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env_dotfile(path: Path | None = None) -> None:
    """Best-effort .env loader: fills variables that are unset or empty in the process env."""
    env = path or ROOT / '.env'
    if not env.exists():
        return
    for line in env.read_text(encoding='utf-8', errors='replace').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        # Override empty values from shell env (e.g. ANTHROPIC_API_KEY="" set globally)
        if not os.environ.get(k):
            os.environ[k] = v


_load_env_dotfile = load_env_dotfile          # the old name the scripts call


load_env_dotfile()
# Force Gemini (don't use the GOOGLE_API_KEY of a project that has no Gemini API enabled)
if os.environ.get('GEMINI_API_KEY'):
    os.environ.pop('GOOGLE_API_KEY', None)

logging.basicConfig(format='%(asctime)s [%(levelname)s] %(name)s: %(message)s', level=logging.INFO)
for _noisy in ('httpx', 'httpcore'):          # at INFO they log every request URL (URLs can carry keys)
    logging.getLogger(_noisy).setLevel(logging.WARNING)
