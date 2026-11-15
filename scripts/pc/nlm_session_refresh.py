"""
scripts/pc/nlm_session_refresh.py — the PC's safety net for the server's NotebookLM login (14/11).

The server watches its own login (scripts/nlm_keepalive.py, every 20 min) but its copy dies after hours to days
(15/11: Google drops the server's copy of the session this PC's Chrome owns). When it does, the fix has
always been on this PC: `python -m notebooklm login --browser chrome` (the persistent Chrome profile is still signed
in, so nothing is typed) and scp storage_state.json to the server. This does that on its own.

Each run (Windows task SmartStudy_NLM_Session: every 4 h, at logon, on wake from sleep):
  1. offline (no Google or no server) → log one line, exit 0
  2. ask the server, read-only: `notebooklm auth check --test --passive` + its file's mtime
  3. server ok → done. The PC does NOT touch the session then: two copies of one Google session each rotating
     cookies on their own is exactly what we don't want. (15/11: "this PC's file is newer" used to push too — but
     anything on the PC that uses NotebookLM rewrites that file, so it pushed a second chain over a healthy one.)
  4. otherwise: refresh here — headless Chrome on the persistent profile (layer-3 re-auth of notebooklm-py); if that
     can't run, `notebooklm login --browser chrome` with a short timeout (it only says "Already logged in" and
     saves; a dead profile just times out — nothing is ever typed).
  5. verify here (list notebooks), copy to the server (temp file → chmod 600 → atomic mv), verify there.
Never prompts, never types credentials, never prints a cookie. Always exits 0; the log is
~\\.smart_study\\nlm_session.log.

Install (once):  powershell -ExecutionPolicy Bypass -File scripts\\pc\\install_nlm_session_task.ps1
By hand:         python scripts\\pc\\nlm_session_refresh.py [--force]
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

VM_HOST = os.environ.get('VM_IP', 'your-server')            # the server's (tailnet) address: set VM_IP in .env
VM = f"{os.environ.get('VM_USER', 'ubuntu')}@{VM_HOST}"
KEY = Path(os.environ.get('SSH_KEY', str(Path.home() / '.ssh' / 'smart_study.key')))
PROFILE = Path.home() / '.notebooklm' / 'profiles' / 'default'
STORAGE = PROFILE / 'storage_state.json'
VM_STORAGE = '.notebooklm/profiles/default/storage_state.json'
# not under AppData: Claude Desktop (MSIX) virtualizes AppData writes, so a task wouldn't see files put there
LOG = Path.home() / '.smart_study' / 'nlm_session.log'
SSH_OPTS = ['-i', str(KEY), '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', '-o', 'StrictHostKeyChecking=accept-new']
NOWIN = getattr(subprocess, 'CREATE_NO_WINDOW', 0)      # pythonw from the task: no console flashes
VM_CHECK = ('cd ~/smart_study_agent && venv/bin/python3 -m notebooklm --quiet auth check --test --passive --json; '
            f'echo; echo MTIME=$(stat -c %Y ~/{VM_STORAGE} 2>/dev/null || echo 0)')


def log(msg: str) -> None:
    line = f'[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}'
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open('a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        pass
    if sys.stdout:                              # None under pythonw
        print(line)


def online(host: str, port: int, timeout: float = 6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def parse_vm_check(out: str) -> tuple[bool | None, float]:
    """(server session ok? — None when the check itself couldn't run, server file mtime)."""
    mtime = 0.0
    body = []
    for ln in (out or '').splitlines():
        if ln.startswith('MTIME='):
            try:
                mtime = float(ln[6:].strip() or 0)
            except ValueError:
                pass
        else:
            body.append(ln)
    try:
        d = json.loads('\n'.join(body).strip() or '{}')
    except ValueError:
        return None, mtime
    if not d:
        return None, mtime
    checks = d.get('checks') or {}
    if d.get('status') == 'ok' and checks.get('token_fetch') is True:
        return True, mtime
    err = json.dumps(d.get('details') or {}, ensure_ascii=False).lower()
    if any(w in err for w in ('timed out', 'timeout', 'connecterror', 'network', 'temporary failure')):
        return None, mtime                                  # the server's own network, not the login
    return False, mtime


def need_push(vm_ok: bool | None, force: bool) -> str | None:
    """Why to refresh/push, or None: only a dead server session (or --force). Pure."""
    if force:
        return 'forced'
    if vm_ok is False:
        return 'server session dead'
    return None


def ssh(cmd: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(['ssh', *SSH_OPTS, VM, cmd], capture_output=True, text=True, timeout=timeout,
                          encoding='utf-8', errors='replace', creationflags=NOWIN)


def vm_check() -> tuple[bool | None, float]:
    try:
        return parse_vm_check(ssh(VM_CHECK).stdout)
    except (subprocess.SubprocessError, OSError):
        return None, 0.0


def headless_refresh() -> bool:
    try:
        from notebooklm._browser.headless_reauth import attempt_headless_reauth
        r = attempt_headless_reauth(storage_path=STORAGE, allow_headless=True, browser='chrome')
        log(f'  headless re-auth: {r.status.value} ({r.reason})')
        return r.status.value == 'success'
    except Exception as e:                      # library moved/changed → the login fallback
        log(f'  headless re-auth unavailable: {type(e).__name__}')
        return False


def login_refresh() -> bool:
    try:
        p = subprocess.run([sys.executable, '-m', 'notebooklm', 'login', '--browser', 'chrome', '--browser-timeout', '90'],
                           capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL,
                           encoding='utf-8', errors='replace', creationflags=NOWIN)
        log(f'  login --browser chrome: exit {p.returncode}')
        return p.returncode == 0
    except (subprocess.SubprocessError, OSError) as e:
        log(f'  login --browser chrome failed: {type(e).__name__}')
        return False


def count_notebooks() -> int | None:
    async def go():
        from notebooklm import NotebookLMClient
        async with NotebookLMClient.from_storage(path=str(STORAGE)) as c:
            return len(await c.notebooks.list())
    try:
        return asyncio.run(asyncio.wait_for(go(), timeout=90))
    except Exception as e:
        log(f'  verify here failed: {type(e).__name__}: {str(e)[:120]}')
        return None


def push() -> bool:
    tmp = VM_STORAGE + '.incoming'
    try:
        p = subprocess.run(['scp', '-q', *SSH_OPTS, str(STORAGE), f'{VM}:{tmp}'], capture_output=True, text=True, timeout=120,
                           creationflags=NOWIN)
        if p.returncode != 0:
            log(f'  scp failed (exit {p.returncode})')
            return False
        r = ssh(f'chmod 600 ~/{tmp} && mv -f ~/{tmp} ~/{VM_STORAGE}')
        return r.returncode == 0
    except (subprocess.SubprocessError, OSError) as e:
        log(f'  copy failed: {type(e).__name__}')
        return False


def main(argv: list[str]) -> int:
    force = '--force' in argv
    if not online('accounts.google.com', 443) or not online(VM_HOST, 22):
        log('offline (Google or the server unreachable) — nothing to do')
        return 0
    vm_ok, _ = vm_check()
    why = need_push(vm_ok, force)
    if not why:
        log(f'server session {"ok" if vm_ok else "unknown (check could not run)"} — nothing to do')
        return 0
    log(f'refreshing: {why}')
    if not headless_refresh() and not login_refresh():
        log('  the PC profile could not refresh either — the owner has to log in (python -m notebooklm login --browser chrome)')
        return 0
    n = count_notebooks()
    if not n:
        log('  the PC session does not work — not copying it')
        return 0
    log(f'  PC session ok ({n} notebooks)')
    if not push():
        return 0
    time.sleep(2)
    ok, _ = vm_check()
    log(f'  copied to the server; server check: {"ok" if ok else "FAILED" if ok is False else "unknown"}')
    return 0


if __name__ == '__main__':
    try:
        main(sys.argv[1:])
    except Exception as e:                      # a scheduled task never crashes loudly
        log(f'error: {type(e).__name__}: {str(e)[:200]}')
    sys.exit(0)
