"""scripts/local_vm_recovery.py — Run on user's PC. Detects VM lockup and auto-recovers via OCI API.

This is the *outside-the-VM* watchdog. When the VM freezes (no SSH, no ping),
this script:
  1. Tries SSH (10s timeout)
  2. If 3 consecutive failures (~3 minutes) → calls Oracle OCI API
  3. Performs "soft reboot" via API; if that fails, "force reboot"
  4. Notifies before and after (Windows toast + WhatsApp via utils.notify; 10/11 Telegram deleted)

Setup (one-time on user's PC):
  1. Install OCI CLI: pip install oci
  2. Configure OCI CLI: oci setup config — generate API key, add to OCI Console
  3. Run this script via Windows Task Scheduler every 2 min, OR
     run as a long-running background process.

Configuration via .env (in same dir):
  OCI_INSTANCE_ID=ocid1.instance.oc1.eu-jerusalem-1.xxxxxxx
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env', override=True)
except ImportError:
    pass

VM_IP        = os.environ.get('VM_IP', 'your-server')   # tailnet — public SSH closed 03/10/2026
VM_USER      = os.environ.get('VM_USER', 'ubuntu')
SSH_KEY      = os.environ.get('SSH_KEY', str(Path.home() / '.ssh' / 'smart_study.key'))
INSTANCE_ID  = os.environ.get('OCI_INSTANCE_ID', '')
STATE_FILE   = Path.home() / '.smart_study_vm_recovery_state.json'

FAIL_THRESHOLD     = 3       # 3 consecutive failures → recover
COOLDOWN_MINUTES   = 15      # don't reboot more than once per 15min


def _notify(text: str) -> None:
    """A toast on this PC (works while the VM is down) + WhatsApp when it can take it (else the undelivered file)."""
    try:
        from notifier.toast import notify as toast
        toast('VM recovery', text)
    except Exception:
        pass
    try:
        from utils.notify import deliver
        deliver(text, markdown=False)
    except Exception as e:
        print(f'  ⚠️ notify failed: {e}')


def _load_state() -> dict:
    if not STATE_FILE.exists(): return {'fail_count': 0, 'last_reboot': 0}
    try: return json.loads(STATE_FILE.read_text())
    except Exception: return {'fail_count': 0, 'last_reboot': 0}


def _save_state(s: dict) -> None:
    STATE_FILE.write_text(json.dumps(s))


def _check_ssh() -> bool:
    try:
        result = subprocess.run(
            ['ssh', '-o', 'ConnectTimeout=10',
             '-o', 'StrictHostKeyChecking=no',
             '-o', 'BatchMode=yes',
             '-i', SSH_KEY, f'{VM_USER}@{VM_IP}', 'echo UP'],
            capture_output=True, text=True, timeout=15,
        )
        return result.returncode == 0 and 'UP' in result.stdout
    except Exception:
        return False


def _reboot_via_oci(action: str = 'SOFTRESET') -> bool:
    """Reboot via OCI CLI. action: SOFTRESET (graceful) or RESET (force)."""
    if not INSTANCE_ID:
        print('No OCI_INSTANCE_ID set — cannot auto-recover')
        return False
    try:
        cmd = ['oci', 'compute', 'instance', 'action',
               '--instance-id', INSTANCE_ID,
               '--action', action,
               '--wait-for-state', 'RUNNING',
               '--max-wait-seconds', '300']
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=400)
        if result.returncode == 0:
            print(f'OCI {action} succeeded')
            return True
        print(f'OCI {action} failed: {result.stderr[:500]}')
        return False
    except FileNotFoundError:
        print('OCI CLI not installed. Run: pip install oci-cli')
        return False
    except Exception as e:
        print(f'OCI exception: {e}')
        return False


def main():
    s = _load_state()
    now = time.time()

    if _check_ssh():
        if s.get('fail_count', 0) > 0:
            print(f'VM back online (was failing {s["fail_count"]}x)')
        s['fail_count'] = 0
        _save_state(s)
        return

    s['fail_count'] = s.get('fail_count', 0) + 1
    print(f'SSH check failed ({s["fail_count"]}/{FAIL_THRESHOLD})')

    if s['fail_count'] < FAIL_THRESHOLD:
        _save_state(s)
        return

    # Threshold reached — but respect cooldown
    last_reboot = s.get('last_reboot', 0)
    if now - last_reboot < COOLDOWN_MINUTES * 60:
        remaining = int(COOLDOWN_MINUTES * 60 - (now - last_reboot))
        print(f'In cooldown ({remaining}s) — skipping reboot')
        _save_state(s)
        return

    _notify(f'⚠️ ה-VM לא נגיש כבר {FAIL_THRESHOLD * 2} דק׳. מבצע אתחול אוטומטי דרך OCI API...')
    ok = _reboot_via_oci('SOFTRESET')
    if not ok:
        print('Soft reset failed — trying force reset')
        _notify('⚠️ Soft reboot נכשל. מנסה Force reboot...')
        ok = _reboot_via_oci('RESET')

    if ok:
        _notify('✅ ה-VM אותחל אוטומטית. בדיקה תוך 2 דקות.')
        s['last_reboot'] = now
        s['fail_count'] = 0
    else:
        _notify('⛔ Auto-recovery נכשלה. נדרשת התערבות ידנית בקונסולה.')
    _save_state(s)


if __name__ == '__main__':
    main()
