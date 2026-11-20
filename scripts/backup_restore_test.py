"""
scripts/backup_restore_test.py — weekly proof that every backup can actually be restored (17/11/2026). No LLM, $0.

A backup nobody ever restored is a hope, not a backup. Once a week this takes the newest Drive backup and
  1. restores it into a fresh ~/tmp/backup_restore_XXXX (never over a live file; the only thing it ever writes to
     is that temp dir, plus data/backup_test.json + history), compares file counts / bytes with the live source and
     parses the JSON/YAML files it got back;
  2. checks the GPG-encrypted secrets bundle: it is there, not shrunk, and encrypted to OUR key with an integrity
     packet. It can NOT be decrypted here — the private key lives only on the owner's PC (docs/RUNBOOK.md) — and the
     report says so. Nothing from inside it is ever read or printed, and no passphrase is needed;
  3. runs an archive integrity check (tar -tzf) on the local backups/*.tgz;
  4. checks the crontab copy (backups/crontab.*, deploy/crontab.txt vs the live crontab);
  5. checks the vault copy on Drive (gdrive:smart-study-agent/vault) against the live vault;
then removes the temp restore (guarded: only a direct child of ~/tmp named backup_restore_*, never a symlink,
never $HOME) and writes data/backup_test.json — the dashboard's "בריאות" page reads it.

Quiet when all is well: it only logs. A FAIL (a backup is broken, or older than expected) is ONE WhatsApp message via
utils.notify (its dedup + quiet hours; the key is the set of failing checks + the ISO week). WARNs are shown on the
dashboard but never sent. rclone is used with COPY only (never sync). Exit code 0 unless the script itself crashes,
so run_with_alert.sh doesn't double the alert.

    venv/bin/python3 scripts/backup_restore_test.py            # run + write + alert on FAIL
    venv/bin/python3 scripts/backup_restore_test.py --dry      # run, print, no file, no alert
Cron (UTC, Sunday 05:30 = 07:30/08:30 Israel, after the 04:00 backup):
    30 5 * * 0 TZ=Asia/Jerusalem /home/ubuntu/smart_study_agent/scripts/run_with_alert.sh "Backup Restore Test" bash -c "cd /home/ubuntu/smart_study_agent && PYTHONIOENCODING=utf-8 venv/bin/python3 scripts/backup_restore_test.py" >> /home/ubuntu/smart_study_agent/logs/backup_restore_test.log 2>&1
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
ROOT = Path(os.environ.get('SSA_ROOT') or (HERE if (HERE / 'utils').is_dir() else Path.home() / 'smart_study_agent'))
HOME = Path(os.environ.get('SSA_HOME') or Path.home())
sys.path.insert(0, str(ROOT))

STATUS = ROOT / 'data' / 'backup_test.json'
HISTORY = ROOT / 'data' / 'backup_test_history.jsonl'
REMOTE = os.environ.get('BACKUP_REMOTE', 'gdrive:smart-study-agent-backup')
VAULT_REMOTE = 'gdrive:smart-study-agent/vault'
TMP_BASE = HOME / 'tmp'
TMP_PREFIX = 'backup_restore_'
GPG_RECIPIENT = os.environ.get('BACKUP_GPG_RECIPIENT', '').strip()     # your key's fingerprint (SECURITY.md); empty = skip the GPG check

OK, WARN, FAIL = 'OK', 'WARN', 'FAIL'

# what backup_to_drive.sh writes (restored/<path> mirrors the live path), and where the live copy is
DIR_BACKUPS = ['data/summaries', 'data/homework', 'data/assignments']
FILE_BACKUPS = ['data/chat_history.json', 'data/vault_index.tfidf.json', 'config/sent_log.json', 'config/courses.yaml',
                'config/professors.yaml', 'config/notebooklm_notebooks.json']
EXPECTED_TOP = ['config', 'data', 'obsidian-vault', 'secrets.tar.gz.gpg']
MIN_SECRETS_BYTES = 2000                 # 6 repo items + rclone.conf + the NotebookLM session: ~200 KB in practice


# ── small helpers ──────────────────────────────────────────────────────────────────────────────────────────────────

def chk(cid: str, label: str, status: str, reason: str, **extra) -> dict:
    return {'id': cid, 'label': label, 'status': status, 'reason': reason, **extra}


def run(cmd: list[str], timeout: int = 600, retries: int = 0, wait: int = 20) -> subprocess.CompletedProcess | None:
    """subprocess without a shell; retries only for the network (rclone). None = could not run at all."""
    for i in range(retries + 1):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors='replace')
        except (OSError, subprocess.TimeoutExpired):
            r = None
        if r is not None and r.returncode == 0:
            return r
        if i < retries:
            time.sleep(wait)
    return r


def safe_remove_tree(path, base: Path = TMP_BASE, prefix: str = TMP_PREFIX) -> bool:
    """The only delete this script does. Refuses anything that is not a real directory directly under `base` whose name
    starts with `prefix` (so never $HOME, never the repo, never a symlink, never a path with '..')."""
    p = Path(path)
    try:
        if p.is_symlink() or not p.is_dir():
            return False
        rp, rb = p.resolve(), Path(base).resolve()
        if rp.parent != rb or not rp.name.startswith(prefix) or rp in (Path.home().resolve(), HOME.resolve(), ROOT.resolve()):
            return False
        shutil.rmtree(rp)
        return True
    except OSError:
        return False


def tree_stats(d: Path, skip=lambda rel: False) -> tuple[int, int]:
    n = b = 0
    for dp, _dn, fn in os.walk(d):
        for f in fn:
            fp = Path(dp) / f
            if skip(fp.relative_to(d).as_posix()) or fp.is_symlink():
                continue
            try:
                b += fp.stat().st_size
                n += 1
            except OSError:
                pass
    return n, b


def _vault_skip(rel: str) -> bool:                  # backup_to_drive.sh: --exclude ".obsidian/workspace*"
    return rel.startswith('.obsidian/workspace')


def ratio_status(got: int, want: int, what: str) -> tuple[str, str]:
    """A backup may lag the live data by a few files (changed since 04:00) but must not be missing a chunk of it."""
    if want <= 0:
        return OK, f'{what}: אין מקור להשוות'
    r = got / want
    txt = f'{what}: {got}/{want} ({r * 100:.0f}%)'
    if r >= 0.95:
        return OK, txt
    return (WARN if r >= 0.85 else FAIL), txt


def gpg_packet_check(listing: str, recipient: str) -> tuple[str, str]:
    """Judge `gpg --list-packets` output (structure only — nothing is decrypted). → (status, reason)."""
    keyids = re.findall(r'pubkey enc packet:.*?keyid ([0-9A-Fa-f]{16})', listing)
    if not keyids:
        return FAIL, 'אין חבילת מפתח ציבורי בקובץ — לא מוצפן למפתח שלנו'
    if recipient[-16:].upper() not in {k.upper() for k in keyids}:
        return FAIL, f'מוצפן למפתח אחר ({keyids[0][-8:]}) ולא למפתח הגיבוי שלנו — אי אפשר יהיה לפתוח'
    if not re.search(r'encrypted data packet|aead encrypted packet', listing):
        return FAIL, 'אין חבילת נתונים מוצפנת בקובץ'
    if not re.search(r'mdc_method: 2|aead', listing):
        return WARN, 'אין חבילת שלמות (MDC) — הקובץ נראה תקין אבל אי אפשר לאמת שלמות'
    return OK, 'מוצפן למפתח הגיבוי שלנו, עם חבילת שלמות'


# ── the checks ─────────────────────────────────────────────────────────────────────────────────────────────────────

def check_drive_latest() -> tuple[list[dict], str | None]:
    r = run(['rclone', 'lsjson', REMOTE, '--dirs-only', '--no-modtime'], timeout=120, retries=2)
    if r is None or r.returncode != 0:
        return [chk('drive.latest', 'גיבוי יומי ב-Drive', FAIL, 'לא הצלחתי לקרוא את Drive (rclone נכשל 3 פעמים)')], None
    try:
        names = sorted(d['Name'] for d in json.loads(r.stdout) if re.fullmatch(r'\d{4}-\d{2}-\d{2}', d.get('Name', '')))
    except (ValueError, KeyError):
        names = []
    if not names:
        return [chk('drive.latest', 'גיבוי יומי ב-Drive', FAIL, f'אין תיקיות גיבוי תחת {REMOTE}')], None
    latest = names[-1]
    age = (datetime.now(timezone.utc).date() - datetime.strptime(latest, '%Y-%m-%d').date()).days
    out = []
    if age >= 2:
        out.append(chk('drive.latest', 'גיבוי יומי ב-Drive', FAIL, f'הגיבוי האחרון {latest} — ישן מ-{age} ימים (מצופה: מדי לילה)'))
    elif age == 1:
        out.append(chk('drive.latest', 'גיבוי יומי ב-Drive', WARN, f'הגיבוי האחרון {latest} — של אתמול, של הלילה לא הגיע'))
    else:
        out.append(chk('drive.latest', 'גיבוי יומי ב-Drive', OK, f'הגיבוי האחרון {latest} (היום)'))
    out.append(chk('drive.retention', 'שמירת 14 ימים', OK if len(names) >= 7 else WARN, f'{len(names)} ימי גיבוי ב-Drive'))
    return out, latest


def check_drive_complete(latest: str) -> dict:
    r = run(['rclone', 'lsf', f'{REMOTE}/{latest}', '--max-depth', '1'], timeout=120, retries=2)
    if r is None or r.returncode != 0:
        return chk('drive.complete', 'תוכן הגיבוי', FAIL, f'לא הצלחתי לרשום את {latest}')
    have = {x.strip().rstrip('/') for x in r.stdout.splitlines() if x.strip()}
    missing = [t for t in EXPECTED_TOP if t not in have]
    if missing:
        return chk('drive.complete', 'תוכן הגיבוי', FAIL, f'חסר בגיבוי {latest}: ' + ', '.join(missing))
    return chk('drive.complete', 'תוכן הגיבוי', OK, 'כל הרכיבים נמצאים (config, data, כספת, secrets מוצפן)')


def restore_latest(latest: str, dest: Path) -> dict | None:
    """rclone COPY of the newest backup into `dest`. → a FAIL check, or None on success."""
    r = run(['rclone', 'copy', f'{REMOTE}/{latest}', str(dest), '--transfers', '4', '--quiet'], timeout=1500, retries=1)
    if r is None or r.returncode != 0:
        return chk('restore.copy', 'שחזור לתיקיית ניסוי', FAIL, f'rclone copy מ-{latest} נכשל')
    return None


def _parse_ok(p: Path) -> bool:
    try:
        if p.suffix == '.json':
            json.loads(p.read_text(encoding='utf-8'))
        elif p.suffix in ('.yaml', '.yml'):
            import yaml
            yaml.safe_load(p.read_text(encoding='utf-8'))
        return p.stat().st_size > 0
    except Exception:
        return False


def check_restored_paths(restored: Path) -> list[dict]:
    out = []
    for rel in DIR_BACKUPS + ['obsidian-vault']:
        live = (ROOT / rel) if rel != 'obsidian-vault' else _vault_dir()
        got_dir = restored / rel
        label = f'שחזור {rel}'
        if live is None or not live.is_dir():
            out.append(chk(f'restore.{rel}', label, OK, 'אין מקור חי להשוות'))
            continue
        skip = _vault_skip if rel == 'obsidian-vault' else (lambda _r: False)
        ln, lb = tree_stats(live, skip)
        gn, gb = tree_stats(got_dir, skip) if got_dir.is_dir() else (0, 0)
        if ln and not gn:
            out.append(chk(f'restore.{rel}', label, FAIL, f'לא שוחזר דבר ({ln} קבצים חיים)'))
            continue
        s1, t1 = ratio_status(gn, ln, 'קבצים')
        s2, t2 = ratio_status(gb, lb, 'בתים')
        bad = [p.name for p in list(got_dir.rglob('*'))[:4000] if p.suffix in ('.json', '.yaml', '.yml') and p.is_file()
               and not _parse_ok(p)][:3]
        status = max((s1, s2), key=(OK, WARN, FAIL).index)
        reason = f'{t1} · {t2}'
        if bad:
            status, reason = FAIL, f'{reason} · קבצים שלא נפתחים: {", ".join(bad)}'
        out.append(chk(f'restore.{rel}', label, status, reason, files=gn, bytes=gb))
    bad_files, missing = [], []
    for rel in FILE_BACKUPS:
        live, got = ROOT / rel, restored / rel
        if not live.exists():
            continue
        if not got.is_file():
            missing.append(rel)
        elif not _parse_ok(got):
            bad_files.append(rel)
    if missing or bad_files:
        out.append(chk('restore.files', 'שחזור קבצים בודדים', FAIL,
                       'חסרים: ' + ', '.join(missing or ['-']) + (' · לא נפתחים: ' + ', '.join(bad_files) if bad_files else '')))
    else:
        out.append(chk('restore.files', 'שחזור קבצים בודדים', OK, f'{len(FILE_BACKUPS)} קבצי JSON/YAML שוחזרו ונפתחו'))
    return out


def check_secrets(restored: Path, prev_bytes: int | None) -> dict:
    f = restored / 'secrets.tar.gz.gpg'
    note = 'פענוח לא נבדק בשרת (המפתח הפרטי רק במחשב של המשתמש); נבדקו מבנה, נמען ושלמות'
    if not f.is_file() or f.stat().st_size == 0:
        return chk('secrets.gpg', 'כספת סודות מוצפנת', FAIL, 'secrets.tar.gz.gpg חסר או ריק', decrypt_tested=False)
    size = f.stat().st_size
    if size < MIN_SECRETS_BYTES:
        return chk('secrets.gpg', 'כספת סודות מוצפנת', FAIL, f'הקובץ קטן מדי ({size} בתים)', decrypt_tested=False)
    if prev_bytes and size < prev_bytes * 0.5:
        return chk('secrets.gpg', 'כספת סודות מוצפנת', FAIL, f'הקובץ התכווץ מ-{prev_bytes} ל-{size} בתים', decrypt_tested=False)
    r = run(['gpg', '--batch', '--no-tty', '--list-packets', str(f)], timeout=300)
    if r is None:
        return chk('secrets.gpg', 'כספת סודות מוצפנת', WARN, f'gpg לא זמין — לא נבדק. {note}', decrypt_tested=False, bytes=size)
    listing = (r.stdout or '') + '\n' + (r.stderr or '')
    if re.search(r'invalid packet|premature|unexpected EOF|truncated|bad packet', listing, re.I):
        return chk('secrets.gpg', 'כספת סודות מוצפנת', FAIL, 'מבנה הקובץ פגום (gpg מדווח על חבילה לא תקינה)',
                   decrypt_tested=False, bytes=size)
    if not GPG_RECIPIENT:
        return chk('secrets.gpg', 'כספת סודות מוצפנת', WARN, f'BACKUP_GPG_RECIPIENT לא מוגדר — הנמען לא נבדק. {note}',
                   decrypt_tested=False, bytes=size)
    status, reason = gpg_packet_check(listing, GPG_RECIPIENT)
    return chk('secrets.gpg', 'כספת סודות מוצפנת', status, f'{reason}. {note}', decrypt_tested=False, bytes=size)


def check_archives() -> dict:
    files = sorted(p for p in (ROOT / 'backups').glob('*') if p.is_file() and p.suffix in ('.tgz', '.gz'))
    if not files:
        return chk('archives', 'ארכיוני tar מקומיים', OK, 'אין ארכיונים')
    bad, entries = [], 0
    for p in files:
        r = run(['tar', '-tzf', str(p)], timeout=600)
        if r is None or r.returncode != 0:
            bad.append(p.name)
        else:
            entries += r.stdout.count('\n')
    if bad:
        return chk('archives', 'ארכיוני tar מקומיים', FAIL, 'ארכיון פגום: ' + ', '.join(bad))
    return chk('archives', 'ארכיוני tar מקומיים', OK, f'{len(files)} ארכיונים נקראו עד הסוף ({entries} רשומות), ללא שגיאה')


def _cron_lines(text: str) -> set[str]:
    return {re.sub(r'\s+', ' ', l.strip()) for l in text.splitlines() if l.strip() and not l.lstrip().startswith('#')}


def check_crontab() -> dict:
    saved = sorted((ROOT / 'backups').glob('crontab.*'))
    deploy = ROOT / 'deploy' / 'crontab.txt'
    notes = []
    status = OK
    if not saved or len(_cron_lines(saved[-1].read_text(encoding='utf-8', errors='replace'))) < 5:
        notes.append('אין גיבוי crontab תקין ב-backups/')
        status = WARN
    live_r = run(['crontab', '-l'], timeout=20)
    if live_r is not None and live_r.returncode == 0 and deploy.is_file():
        live, saved_set = _cron_lines(live_r.stdout), _cron_lines(deploy.read_text(encoding='utf-8', errors='replace'))
        only_live, only_git = len(live - saved_set), len(saved_set - live)
        if only_live or only_git:
            status = WARN
            notes.append(f'deploy/crontab.txt לא תואם ל-crontab החי ({only_live} שורות רק חיות, {only_git} רק בקובץ) — העותק לשחזור מיושן')
        else:
            notes.append(f'deploy/crontab.txt תואם ל-crontab החי ({len(live)} משימות)')
    else:
        notes.append('crontab חי לא נקרא — לא הושווה')
    return chk('crontab', 'עותק ה-crontab', status, ' · '.join(notes))


def _vault_dir() -> Path | None:
    try:
        from config.paths import VAULT_DIR
        return Path(VAULT_DIR)
    except Exception:
        d = HOME / 'Documents' / 'Obsidian Vault' / 'Smart Study Agent'
        return d if d.is_dir() else None


def check_vault_drive() -> dict:
    vd = _vault_dir()
    r = run(['rclone', 'lsjson', VAULT_REMOTE, '--recursive', '--files-only'], timeout=300, retries=2)
    if r is None or r.returncode != 0:
        return chk('vault.drive', 'הכספת ב-Drive', FAIL, f'לא הצלחתי לקרוא {VAULT_REMOTE}')
    try:
        items = json.loads(r.stdout)
    except ValueError:
        return chk('vault.drive', 'הכספת ב-Drive', FAIL, 'תשובת rclone לא תקינה')
    rn = sum(1 for i in items if i.get('Path', '').endswith('.md'))
    if vd is None or not vd.is_dir():
        return chk('vault.drive', 'הכספת ב-Drive', OK, f'{rn} הערות ב-Drive (אין כספת חיה להשוות)')
    ln = sum(1 for _ in vd.rglob('*.md'))
    status, txt = ratio_status(rn, ln, 'הערות ב-Drive מול חיות')
    newest = max((i.get('ModTime', '') for i in items), default='')
    age = ''
    try:
        d = (datetime.now(timezone.utc) - datetime.fromisoformat(newest.replace('Z', '+00:00'))).total_seconds() / 86400
        age = f' · עדכון אחרון לפני {d:.1f} ימים'
        if d > 7 and status == OK:
            status = WARN
    except ValueError:
        pass
    return chk('vault.drive', 'הכספת ב-Drive', status, txt + age, files=rn)


# ── orchestration ──────────────────────────────────────────────────────────────────────────────────────────────────

def _prev_secrets_bytes() -> int | None:
    try:
        for c in json.loads(STATUS.read_text(encoding='utf-8')).get('checks', []):
            if c.get('id') == 'secrets.gpg' and c.get('bytes'):
                return int(c['bytes'])
    except (OSError, ValueError):
        pass
    return None


def run_all() -> list[dict]:
    checks: list[dict] = []
    latest_checks, latest = check_drive_latest()
    checks += latest_checks
    work = None
    if latest:
        checks.append(check_drive_complete(latest))
        TMP_BASE.mkdir(mode=0o700, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix=TMP_PREFIX, dir=TMP_BASE))
        try:
            restored = work / 'restore'
            bad = restore_latest(latest, restored)
            if bad:
                checks.append(bad)
            else:
                checks += check_restored_paths(restored)
                checks.append(check_secrets(restored, _prev_secrets_bytes()))
        finally:
            if not safe_remove_tree(work):
                checks.append(chk('restore.cleanup', 'ניקוי תיקיית הניסוי', WARN, f'לא נמחקה (הגנת נתיב): {work}'))
    checks.append(check_archives())
    checks.append(check_crontab())
    checks.append(check_vault_drive())
    return checks


def overall(checks: list[dict]) -> str:
    return FAIL if any(c['status'] == FAIL for c in checks) else (WARN if any(c['status'] == WARN for c in checks) else OK)


def alert_text(checks: list[dict]) -> str:
    bad = [c for c in checks if c['status'] == FAIL]
    lines = [f'🛑 בדיקת שחזור הגיבויים: {len(bad)} בעיות'] + [f'• {c["label"]}: {c["reason"]}' for c in bad]
    return '\n'.join(lines) + '\nפירוט: data/backup_test.json · docs/RUNBOOK.md (בדיקת שחזור גיבויים)'


def alert_key(checks: list[dict], today=None) -> str:
    from datetime import date
    y, w, _ = (today or date.today()).isocalendar()
    ids = '+'.join(sorted(c['id'] for c in checks if c['status'] == FAIL))
    return f'backup-restore-test:{ids}:{y}-W{w:02d}'


def main(argv: list[str]) -> int:
    dry = '--dry' in argv
    t0 = time.time()
    checks = run_all()
    res = overall(checks)
    ts = datetime.now(timezone.utc).isoformat(timespec='seconds')
    for c in checks:
        print(f'{ {OK: "✅", WARN: "⚠️", FAIL: "⛔"}[c["status"]] } {c["status"]:<4} {c["id"]:<22} {c["reason"]}')
    print(f'\n{res} · {len(checks)} בדיקות · {time.time() - t0:.0f}s')
    if dry:
        return 0
    rec = {'ts': ts, 'overall': res, 'seconds': round(time.time() - t0), 'checks': checks}
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS.with_suffix('.tmp')
    tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding='utf-8')
    tmp.replace(STATUS)
    with HISTORY.open('a', encoding='utf-8') as fh:
        fh.write(json.dumps({'ts': ts, 'overall': res, 'fail': [c['id'] for c in checks if c['status'] == FAIL],
                             'warn': [c['id'] for c in checks if c['status'] == WARN]}, ensure_ascii=False) + '\n')
    if res == FAIL:
        try:
            from utils.notify import notify
            print('alert:', notify(alert_text(checks), key=alert_key(checks), markdown=False))
        except Exception as e:                          # the status file is already written; the dashboard shows it
            print(f'alert failed: {e}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
