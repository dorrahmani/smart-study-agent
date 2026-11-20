"""
scripts/security_check.py — nightly security drift monitor for the VM (14/11/2026). No LLM, no writes outside data/.

The 10–11/11 hardening (docs/RUNBOOK.md, deploy/hooks/README.md, deploy/systemd/hardening/) must not regress silently.
Every check compares the live box against the baseline recorded below and answers PASS / WARN / FAIL with a short Hebrew
reason. Secrets are never printed: the hygiene checks report counts and file names only.

    venv/bin/python3 scripts/security_check.py            # run, write data/security_status.json + history, alert
    venv/bin/python3 scripts/security_check.py --dry      # print the table only: no file, no alert
    venv/bin/python3 scripts/security_check.py --no-live  # skip the live token calls (Gmail/Calendar/Drive/NotebookLM)
    SSA_ROOT=/home/ubuntu/smart_study_agent python3 /tmp/security_check.py --dry    # a copy outside the tree

Alerts: utils.notify, only for a FAIL that wasn't a FAIL in the previous run (one message per run, deduped by key).
A FAIL in the "compromise" group (ports, keys, users, sudoers, firewall, gate) is urgent; the rest wait out quiet hours.
Cron (UTC): docs/RUNBOOK.md, "🔒 בדיקת אבטחה לילית". Exit code 0 unless the script itself crashes, so run_with_alert
doesn't double the alert.

Updating the baseline: a deliberate change (a new key, a new sudoers file, a new listener) shows up as FAIL/WARN once;
change the matching constant below in the same commit as the change itself.
"""
from __future__ import annotations

import ast
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
ROOT = Path(os.environ.get('SSA_ROOT') or (HERE if (HERE / 'utils').is_dir() else Path.home() / 'smart_study_agent'))
HOME = Path(os.environ.get('SSA_HOME') or Path.home())
STATUS = ROOT / 'data' / 'security_status.json'
HISTORY = ROOT / 'data' / 'security_history.jsonl'

PASS, WARN, FAIL = 'PASS', 'WARN', 'FAIL'

# ── baseline, recorded on the VM 14/11/2026 ────────────────────────────────────────────────────────────────────────
# Listening on a wildcard / non-loopback, non-tailnet address. sshd is on 0.0.0.0 but ufw only lets it in on tailscale0
# (the ufw check below); 41641/udp is tailscale's WireGuard port; 68/udp is the DHCP client on the VCN NIC.
EXPOSED_OK = {('tcp', 22, 'sshd'), ('udp', 41641, 'tailscaled'), ('udp', 68, 'systemd-network')}
LOCAL_OK = {'sshd', 'tailscaled', 'streamlit', 'ollama', 'systemd-resolve', 'systemd-network'}
DASHBOARD_PORT = 8501
FORBIDDEN_GROUPS = {'adm', 'lxd', 'docker', 'disk', 'shadow', 'libvirt', 'kvm'}
SHELL_USERS = {'root', 'opc', 'ubuntu', 'claudeagent', 'geminiagent'}        # ssa-gate is nologin on purpose
SUDOERS_D = {                                                                 # sha256, first 16 hex
    '090-oca-vss-plugin-commands': '19a274080602a609',
    '100-oracle-cloud-agent-users': 'b42750c6a11bc7a5',
    '90-cloud-init-users': 'c4635020a5ccc95f',
    'README': '016d919662c52917',
    'ssa-gate': '8e44f9586232b82b',
}
SUDOERS_MAIN = '5ae1d5673f987e40'
# Fingerprints (ssh-keygen -lf key.pub) of the ONLY keys allowed to log in. Set SSA_AUTHORIZED_KEYS in .env (comma-separated).
AUTHORIZED_KEYS = {x.strip() for x in os.environ.get('SSA_AUTHORIZED_KEYS', 'SHA256:' + '0' * 43).split(',') if x.strip()}   # default = placeholder
ROOT_AUTHORIZED_KEYS = 'd21aa013991581ca'                                     # OCI's "login as ubuntu" stub
SSHD_EXPECT = {'passwordauthentication': 'no', 'permitrootlogin': 'no', 'pubkeyauthentication': 'yes',
               'kbdinteractiveauthentication': 'no', 'allowusers': 'ubuntu', 'maxauthtries': '3'}
EGRESS_CHAINS = {          # chain: (rules in v4, rules in v6) — `-A` lines; the last must REJECT (claude) / gate (gemini)
    'CLAUDEAGENT_OUT': (6, 4),
    'GEMINIAGENT_OUT': (10, 6),
}
EGRESS_USERS = {'CLAUDEAGENT_OUT': 'claudeagent', 'GEMINIAGENT_OUT': 'geminiagent'}
TAILNET_V4 = ipaddress.ip_network('100.64.0.0/10')
TAILNET_V6 = ipaddress.ip_network('fd7a:115c:a1e0::/48')
SSH_FAIL_WARN = 20                         # failed SSH attempts in 24h; ufw keeps the internet out, so 0 is normal
TS_KEY_WARN_DAYS, TS_KEY_FAIL_DAYS = 30, 7
# Functions whose text is someone else's words: they must stay under @api_only() (never the subscription agent).
API_ONLY_EXPECTED = {
    'agent/assistant.py': {'decide_partner'},
    'agent/coordinator.py': {'_json_call'},
    'agent/core.py': {'mail_tick', '_contact', '_partner'},
    'agent/course_lookup.py': {'_ask'},
    'agent/learn.py': {'extract'},
    'agent/secretary.py': {'ask', 'ask_url'},
    'agent/starter_help.py': {'_local', 'screenshot'},      # 17/11 friends' questions about the public repo
    'agent/group_helper.py': {'reply_for'},
    'agent/web_agent.py': {'_ask'},
    'integrations/tau_mail.py': {'triage', 'answer'},
}
CODE_CAPS = (             # (label, file, regex for the number, max allowed)
    ('קבוצות: DAY_CALLS', 'agent/group_mode.py', r'^DAY_CALLS\s*=\s*(\d+)', 100),
    ('קבוצות: MAX_REPLIES', 'agent/group_mode.py', r'^MAX_REPLIES\s*=\s*(\d+)', 20),
    ('חברים: תשובות לחבר ליום', 'agent/group_helper.py', r'^FRIEND_DAY\s*=\s*(\d+)', 30),
    ('חברים: תשובות לקבוצה ליום', 'agent/group_helper.py', r'^GROUP_DAY\s*=\s*(\d+)', 100),
    ('תקרת בת הזוג ליום', 'utils/budget.py', r"'partner':\s*(\d+)", 500),
    ('תקרת אנשי קשר ליום', 'utils/budget.py', r"'contact':\s*(\d+)", 200),
    ('נעילת $50', 'scripts/budget_check.py', r'^HARD_LIMIT_USD\s*=\s*([\d.]+)', 50),
)
GUARD_SITES = ('whatsapp/worker.py', 'scripts/tau_mail_watch.py', 'scripts/moodle_announce_watch.py')
GATE_TESTS_MIN = {'settle': 3, 'api_only': 2}           # tests in the deploy gate whose name/body hold these words
COMPROMISE = ('ports', 'ufw', 'egress', 'groups', 'users', 'sudoers', 'authorized_keys', 'sshd', 'deploy_gate',
              'ssh_accepted', 'secrets.git')

# Token shapes. Never print a match: count it.
TOKEN_RX = {
    'Google API key': re.compile(rb'AIza[0-9A-Za-z_\-]{35}'),
    'OpenAI/Anthropic key': re.compile(rb'sk-(?:ant-|proj-)?[A-Za-z0-9_\-]{20,}'),
    'Telegram bot token': re.compile(rb'\b\d{8,10}:AA[A-Za-z0-9_\-]{33}'),
    'Google access token': re.compile(rb'ya29\.[A-Za-z0-9_\-]{20,}'),
    'Google refresh token': re.compile(rb'\b1//0[A-Za-z0-9_\-]{30,}'),
    'GitHub token': re.compile(rb'gh[pousr]_[A-Za-z0-9]{36}'),
    'private key': re.compile(rb'-----BEGIN [A-Z ]*PRIVATE KEY-----'),
}
GIT_TOKEN_ERE = ('AIza[0-9A-Za-z_-]{35}|sk-(ant-|proj-)?[A-Za-z0-9_-]{20,}|[0-9]{8,10}:AA[A-Za-z0-9_-]{33}|'
                 'ya29\\.[A-Za-z0-9_-]{20,}|1//0[A-Za-z0-9_-]{30,}|gh[pousr]_[A-Za-z0-9]{36}|-----BEGIN [A-Z ]*PRIVATE KEY-----')
SECRET_ENV_RX = re.compile(r'(KEY|SECRET|TOKEN|PASSWORD|PASS|TAU_ID)$', re.I)
PII_ENV = {'TAU_ID'}                   # personal, not a credential: found in git = WARN, not "rotate it"
SCAN_MAX_BYTES = 64 * 1024 * 1024


# ── plumbing ───────────────────────────────────────────────────────────────────────────────────────────────────────

def run(cmd: list[str], timeout: int = 30) -> tuple[int, str]:
    """(returncode, stdout+stderr). 127 when the program isn't there (Windows, a box without it)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors='replace')
        return p.returncode, (p.stdout or '') + (p.stderr or '')
    except FileNotFoundError:
        return 127, ''
    except subprocess.TimeoutExpired:
        return 124, ''


class Ctx:
    def __init__(self, root: Path = ROOT, home: Path = HOME, runner=run, live: bool = True, now: datetime | None = None,
                 env: dict | None = None, tmp: Path | None = None):
        self.root, self.home, self.run, self.live = Path(root), Path(home), runner, live
        self.tmp = Path(tmp) if tmp else (Path(self.home) / '.no-tmp' if os.name == 'nt' else Path('/tmp'))
        self.now = now or datetime.now(timezone.utc)
        self.env = env if env is not None else read_env(self.root / '.env')

    def sudo(self, *cmd: str, timeout: int = 30) -> tuple[int, str]:
        return self.run(['sudo', '-n', *cmd], timeout)


def R(cid: str, status: str, reason: str) -> dict:
    return {'id': cid, 'status': status, 'reason': reason}


def unchecked(cid: str, why: str) -> dict:
    return R(cid, WARN, f'לא נבדק: {why}')


def read_env(path: Path) -> dict:
    out = {}
    try:
        for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def sha16(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _mode(p: Path) -> int | None:
    try:
        return p.stat().st_mode & 0o777
    except OSError:
        return None


# ── 1. network exposure ────────────────────────────────────────────────────────────────────────────────────────────

def parse_ss(text: str, proto: str) -> list[dict]:
    """`ss -tlnpH` / `ss -ulnpH` lines → [{proto, host, iface, port, proc}]."""
    out = []
    for line in text.splitlines():
        f = line.split()
        if len(f) < 5:
            continue
        local = f[3]
        host, _, port = local.rpartition(':')
        iface = ''
        if '%' in host:
            host, iface = host.split('%', 1)
        host = host.strip('[]')
        m = re.search(r'users:\(\("([^"]+)"', line)
        try:
            out.append({'proto': proto, 'host': host, 'iface': iface, 'port': int(port), 'proc': m.group(1) if m else ''})
        except ValueError:
            continue
    return out


def classify(host: str, iface: str) -> str:
    if iface == 'lo':
        return 'loopback'
    if iface.startswith('tailscale'):
        return 'tailnet'
    if host in ('*', '', '0.0.0.0', '::'):
        return 'wildcard'
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return 'wildcard'
    if ip.is_loopback:
        return 'loopback'
    if ip in (TAILNET_V4 if ip.version == 4 else TAILNET_V6):
        return 'tailnet'
    return 'exposed'


def check_ports(ctx: Ctx) -> list[dict]:
    rc_t, t = ctx.sudo('ss', '-tlnpH')
    rc_u, u = ctx.sudo('ss', '-ulnpH')
    if rc_t != 0 or rc_u != 0:
        return [unchecked('ports', 'ss/sudo לא זמינים')]
    socks = parse_ss(t, 'tcp') + parse_ss(u, 'udp')
    bad, odd = [], []
    for s in socks:
        where = classify(s['host'], s['iface'])
        key = (s['proto'], s['port'], s['proc'])
        if where in ('wildcard', 'exposed') and key not in EXPOSED_OK:
            bad.append(f"{s['proto']}/{s['port']} {s['proc'] or '?'} על {s['host'] or '*'}")
        elif where in ('loopback', 'tailnet') and s['proc'] not in LOCAL_OK:
            odd.append(f"{s['proto']}/{s['port']} {s['proc'] or '?'} ({where})")
    res = []
    if bad:
        res.append(R('ports', FAIL, 'פורט פתוח לא צפוי: ' + ', '.join(sorted(set(bad)))))
    elif odd:
        res.append(R('ports', WARN, 'מאזין חדש (מקומי/טיילנט): ' + ', '.join(sorted(set(odd)))))
    else:
        res.append(R('ports', PASS, f'{len(socks)} מאזינים, כולם ברשימה'))
    dash = [s for s in socks if s['proto'] == 'tcp' and s['port'] == DASHBOARD_PORT]
    if dash and any(classify(s['host'], s['iface']) != 'loopback' for s in dash):
        res.append(R('dashboard.bind', FAIL, f'הדשבורד ({DASHBOARD_PORT}) לא רק על 127.0.0.1'))
    elif dash:
        res.append(R('dashboard.bind', PASS, 'הדשבורד מאזין רק על 127.0.0.1'))
    else:
        res.append(R('dashboard.bind', WARN, f'אין מאזין על {DASHBOARD_PORT} (הדשבורד למטה?)'))
    return res


def check_ufw(ctx: Ctx) -> list[dict]:
    rc, out = ctx.sudo('ufw', 'status')
    if rc != 0:
        return [unchecked('ufw', 'ufw/sudo לא זמינים')]
    if 'Status: active' not in out:
        return [R('ufw', FAIL, 'חומת האש ufw כבויה')]
    allows = [l for l in out.splitlines() if 'ALLOW' in l]
    wide = [l.split('ALLOW')[0].strip() for l in allows if 'on tailscale0' not in l]
    if wide:
        return [R('ufw', FAIL, 'כלל ufw פתוח לא רק לטיילנט: ' + ', '.join(wide))]
    return [R('ufw', PASS, f'ufw פעיל, {len(allows)} כללי ALLOW וכולם על tailscale0')]


def check_egress(ctx: Ctx) -> list[dict]:
    res = []
    for chain, (n4, n6) in EGRESS_CHAINS.items():
        user = EGRESS_USERS[chain]
        rc, pw = ctx.run(['getent', 'passwd', user])
        uid = pw.split(':')[2] if rc == 0 and pw.count(':') >= 3 else None
        problems, drift = [], []
        for tool, want in (('iptables', n4), ('ip6tables', n6)):
            rc1, rules = ctx.sudo(tool, '-S', chain)
            rc2, output = ctx.sudo(tool, '-S', 'OUTPUT')
            if rc1 == 127 or rc2 == 127:
                return [unchecked('egress', f'{tool} לא זמין')]
            if rc1 != 0:
                problems.append(f'{tool}: אין שרשרת {chain}')
                continue
            a = [l for l in rules.splitlines() if l.startswith('-A ')]
            if uid and f'--uid-owner {uid} -j {chain}' not in output:
                problems.append(f'{tool}: OUTPUT לא מפנה את {user} לשרשרת')
            if chain == 'CLAUDEAGENT_OUT' and (not a or '-j REJECT' not in a[-1]):
                problems.append(f'{tool}: השרשרת לא נגמרת ב-REJECT')
            if chain == 'GEMINIAGENT_OUT' and not any('-j REJECT' in l for l in a):
                problems.append(f'{tool}: אין חסימת רשת פנימית')
            if len(a) != want:
                drift.append(f'{tool} {len(a)}/{want} כללים')
        if not uid:
            drift.append(f'אין משתמש {user}')
        cid = f'egress.{user}'
        if problems:
            res.append(R(cid, FAIL, '; '.join(problems)))
        elif drift:
            res.append(R(cid, WARN, 'כללי היציאה השתנו: ' + ', '.join(drift)))
        else:
            res.append(R(cid, PASS, f'כללי יציאה של {user} במקום (v4+v6)'))
    rc, st = ctx.run(['systemctl', 'is-enabled', 'claudeagent-firewall'])
    if rc == 127:
        res.append(unchecked('egress.boot', 'systemctl לא זמין'))
    elif st.strip() != 'enabled':
        res.append(R('egress.boot', FAIL, 'claudeagent-firewall.service לא enabled — הכללים ייעלמו באתחול'))
    else:
        res.append(R('egress.boot', PASS, 'הכללים נטענים באתחול (claudeagent-firewall)'))
    return res


# ── 2. file modes ──────────────────────────────────────────────────────────────────────────────────────────────────

SECRET_NAME_RX = re.compile(r'(\.pickle|token[^/]*\.json|\.env(\..*)?|rclone\.conf|storage_state[^/]*\.json|'
                            r'crontab\.backup[^/]*|credentials[^/]*\.json|\.key|\.pem|session\.sqlite3)$', re.I)
SECRET_NAME_SKIP = re.compile(r'\.env\.example$|\.py[co]?$|\.ps1$|\.sh$')
PRUNE_DIRS = {'venv', 'venv310', 'venv312', 'venv_wa', 'node_modules', 'site-packages', '.cache', '.git', 'snap',
              '__pycache__', 'browser_profile', '.npm', '.local'}


def secret_files(ctx: Ctx) -> list[Path]:
    """Every file under ~ whose name says it holds a credential (venvs/caches pruned), plus the known paths."""
    found = set()
    for base, dirs, files in os.walk(ctx.home):
        dirs[:] = [d for d in dirs if d not in PRUNE_DIRS and not d.startswith('venv')]
        for f in files:
            if SECRET_NAME_RX.search(f) and not SECRET_NAME_SKIP.search(f):
                found.add(Path(base) / f)
    r = ctx.root
    for p in [r / 'config' / 'recipients.json', r / 'config' / 'recipients_full.json',
              ctx.home / '.config' / 'rclone' / 'rclone.conf']:
        if p.exists():
            found.add(p)
    sec = r / 'config' / 'secrets'
    if sec.is_dir():
        found.update(p for p in sec.rglob('*') if p.is_file())
    return sorted(found)


def check_modes(ctx: Ctx) -> list[dict]:
    res = []
    loose = []
    files = secret_files(ctx)
    for p in files:
        m = _mode(p)
        if m is not None and m & 0o077:
            loose.append(f'{p.relative_to(ctx.home) if ctx.home in p.parents else p} ({m:o})')
    dirs = [(ctx.root / 'config' / 'secrets', 0o700), (ctx.root / 'logs', 0o700), (ctx.home / '.ssh', 0o700),
            (ctx.root / 'data' / 'wa_bridge', 0o700)]
    for d, _want in dirs:
        m = _mode(d)
        if m is not None and m & 0o077:
            loose.append(f'{d.name}/ ({m:o})')
    if loose:
        res.append(R('modes.secrets', FAIL, f'{len(loose)} קבצי סוד פתוחים לקבוצה/לכולם: ' + ', '.join(loose[:6])))
    else:
        res.append(R('modes.secrets', PASS, f'{len(files)} קבצי סוד, כולם 600/700'))
    homes = []
    for h in (Path('/home').iterdir() if Path('/home').is_dir() and os.name != 'nt' else []):
        m = _mode(h)
        if m is not None and m & 0o007:
            homes.append(f'{h.name} ({m:o})')
    if homes:
        res.append(R('modes.home', FAIL, 'תיקיית בית פתוחה לכולם: ' + ', '.join(homes)))
    elif os.name != 'nt':
        res.append(R('modes.home', PASS, 'תיקיות הבית סגורות לאחרים (750)'))
    tmp = tmp_logs(ctx)
    wide = [p.name for p in tmp if (_mode(p) or 0) & 0o004]
    if wide:
        res.append(R('modes.tmp_logs', WARN, f'{len(wide)} לוגים ב-/tmp קריאים לכל משתמש (claudeagent/geminiagent): '
                                             + ', '.join(wide[:5]) + ('…' if len(wide) > 5 else '')))
    elif os.name != 'nt':
        res.append(R('modes.tmp_logs', PASS, f'{len(tmp)} לוגים ב-/tmp, אף אחד לא קריא לאחרים'))
    return res


def tmp_logs(ctx: Ctx) -> list[Path]:
    if os.name == 'nt' or not ctx.tmp.is_dir():
        return []
    me = os.getuid()
    out = []
    for p in ctx.tmp.glob('*.log'):
        try:
            if p.is_file() and not p.is_symlink() and p.stat().st_uid == me:
                out.append(p)
        except OSError:
            pass
    return sorted(out)


# ── 3. accounts ────────────────────────────────────────────────────────────────────────────────────────────────────

def check_accounts(ctx: Ctx) -> list[dict]:
    res = []
    rc, out = ctx.run(['id', '-nG', 'ubuntu'])
    if rc == 0:
        bad = sorted(set(out.split()) & FORBIDDEN_GROUPS)
        res.append(R('groups', FAIL, 'ubuntu חזר לקבוצות: ' + ', '.join(bad)) if bad
                   else R('groups', PASS, 'ubuntu לא ב-adm/lxd/docker'))
    else:
        res.append(unchecked('groups', 'id לא זמין'))
    rc, out = ctx.run(['getent', 'passwd'])
    if rc == 0:
        shells = {l.split(':')[0] for l in out.splitlines()
                  if l.count(':') >= 6 and not re.search(r'(nologin|false|sync|shutdown|halt)$', l.split(':')[6])}
        new = sorted(shells - SHELL_USERS)
        res.append(R('users', FAIL, 'משתמש חדש עם shell: ' + ', '.join(new)) if new
                   else R('users', PASS, f'{len(shells)} משתמשים עם shell, כולם ידועים'))
    else:
        res.append(unchecked('users', 'getent לא זמין'))
    rc, out = ctx.sudo('sh', '-c', 'cd /etc/sudoers.d && sha256sum * && sha256sum ../sudoers')
    if rc == 0:
        got = {}
        for l in out.splitlines():
            h, _, name = l.partition('  ')
            got[name.strip()] = h[:16]
        main_h = got.pop('../sudoers', None)
        diff = sorted(n for n in set(got) | set(SUDOERS_D) if got.get(n) != SUDOERS_D.get(n))
        if main_h != SUDOERS_MAIN:
            diff.append('/etc/sudoers')
        res.append(R('sudoers', FAIL, 'sudoers השתנה: ' + ', '.join(diff)) if diff
                   else R('sudoers', PASS, f'sudoers + {len(got)} קבצים ב-sudoers.d זהים לבסיס'))
    else:
        res.append(unchecked('sudoers', 'אין sudo -n'))
    ak = ctx.home / '.ssh' / 'authorized_keys'
    rc, out = ctx.run(['ssh-keygen', '-lf', str(ak)])
    if rc == 0:
        fps = {f for f in re.findall(r'SHA256:[A-Za-z0-9+/=]+', out)}
        extra, missing = fps - AUTHORIZED_KEYS, AUTHORIZED_KEYS - fps
        if extra:
            res.append(R('authorized_keys', FAIL, f'{len(extra)} מפתח SSH חדש ב-authorized_keys'))
        elif missing:
            res.append(R('authorized_keys', WARN, 'המפתח המוכר חסר ב-authorized_keys'))
        else:
            res.append(R('authorized_keys', PASS, f'{len(fps)} מפתח, הטביעה תואמת'))
    else:
        res.append(unchecked('authorized_keys', 'ssh-keygen/הקובץ לא זמינים'))
    rc, out = ctx.sudo('sha256sum', '/root/.ssh/authorized_keys')
    if rc == 0:
        res.append(R('authorized_keys.root', PASS, 'root: רק החסימה של OCI') if out[:16] == ROOT_AUTHORIZED_KEYS
                   else R('authorized_keys.root', FAIL, 'authorized_keys של root השתנה'))
    rc, out = ctx.sudo('sshd', '-T')
    if rc == 0:
        conf = {}
        for l in out.splitlines():
            k, _, v = l.partition(' ')
            conf.setdefault(k.lower(), v.strip())
        bad = [f'{k}={conf.get(k)}' for k, v in SSHD_EXPECT.items() if conf.get(k) != v]
        res.append(R('sshd', FAIL, 'הגדרות SSH נחלשו: ' + ', '.join(bad)) if bad
                   else R('sshd', PASS, 'SSH: מפתח בלבד, בלי root, רק ubuntu, 3 ניסיונות'))
    else:
        res.append(unchecked('sshd', 'sshd -T לא זמין'))
    return res


# ── 4. services ────────────────────────────────────────────────────────────────────────────────────────────────────

def _prop(ctx: Ctx, unit: str, prop: str) -> str:
    rc, out = ctx.run(['systemctl', 'show', unit, '-p', prop, '--value'])
    return out.strip() if rc == 0 else ''


def check_services(ctx: Ctx) -> list[dict]:
    res = []
    rc, _ = ctx.run(['systemctl', '--version'])
    if rc == 127:
        return [unchecked('services', 'systemctl לא זמין')]
    st = _prop(ctx, 'fail2ban', 'ActiveState')
    res.append(R('fail2ban', PASS, 'fail2ban פעיל') if st == 'active' else R('fail2ban', FAIL, f'fail2ban לא פעיל ({st})'))
    drop = _prop(ctx, 'smart-study-wa-bridge', 'DropInPaths')
    res.append(R('bridge.hardening', PASS, 'drop-in ההקשחה של הגשר נטען') if 'hardening.conf' in drop
               else R('bridge.hardening', FAIL, 'drop-in ההקשחה של הגשר לא נטען'))
    drop = _prop(ctx, 'dashboard', 'DropInPaths')
    args = _prop(ctx, 'dashboard', 'ExecStart')
    miss = [f for f in ('--server.address=127.0.0.1', '--server.enableXsrfProtection=true') if f not in args]
    if 'hardening.conf' not in drop:
        miss.append('hardening.conf')
    res.append(R('dashboard.unit', FAIL, 'בדשבורד חסר: ' + ', '.join(miss)) if miss
               else R('dashboard.unit', PASS, 'הדשבורד: 127.0.0.1 + XSRF + הקשחה'))
    act, en = _prop(ctx, 'smart-study-bot', 'ActiveState'), _prop(ctx, 'smart-study-bot', 'UnitFileState')
    res.append(R('telegram_bot', PASS, 'בוט הטלגרם כבוי ולא מופעל באתחול') if act != 'active' and en in ('disabled', 'masked', '')
               else R('telegram_bot', FAIL, f'בוט הטלגרם חזר ({act}/{en})'))
    res += check_deploy_gate(ctx)
    return res


def check_deploy_gate(ctx: Ctx) -> list[dict]:
    hooks = Path(os.environ.get('SSA_GIT_DIR') or (ctx.home / 'smart_study_agent.git')) / 'hooks'
    try:
        live = (hooks / 'pre-receive').read_bytes()
    except OSError:
        return [unchecked('deploy_gate', 'אין hooks/pre-receive')]
    if b'ssa-gate-run' not in live or b'sudo -n -u ssa-gate' not in live:
        return [R('deploy_gate', FAIL, 'ה-pre-receive הפעיל הוא לא הגרסה המבודדת (ssa-gate/bwrap)')]
    res = []
    try:
        repo = (ctx.root / 'deploy' / 'hooks' / 'pre-receive.sandboxed').read_bytes()
        res.append(R('deploy_gate', PASS, 'השער המבודד פעיל וזהה לריפו') if sha16(repo) == sha16(live)
                   else R('deploy_gate', WARN, 'השער המבודד פעיל אבל שונה מ-deploy/hooks/pre-receive.sandboxed'))
    except OSError:
        res.append(R('deploy_gate', PASS, 'השער המבודד פעיל'))
    runner = Path('/usr/local/bin/ssa-gate-run')
    try:
        st = runner.stat()
        if st.st_uid != 0 or st.st_mode & 0o022:
            res.append(R('deploy_gate.runner', FAIL, 'ssa-gate-run לא של root או ניתן לכתיבה'))
        else:
            same = sha16(runner.read_bytes()) == sha16((ctx.root / 'deploy' / 'hooks' / 'ssa-gate-run').read_bytes())
            res.append(R('deploy_gate.runner', PASS, 'ssa-gate-run של root וזהה לריפו') if same
                       else R('deploy_gate.runner', WARN, 'ssa-gate-run שונה מהריפו'))
    except OSError:
        res.append(R('deploy_gate.runner', FAIL, 'אין /usr/local/bin/ssa-gate-run'))
    return res


# ── 5. updates ─────────────────────────────────────────────────────────────────────────────────────────────────────

def check_updates(ctx: Ctx) -> list[dict]:
    res = []
    rc, out = ctx.run(['apt-get', '-s', '-o', 'Debug::NoLocking=1', 'upgrade'], 120)
    if rc == 0:
        inst = [l for l in out.splitlines() if l.startswith('Inst ')]
        sec = [l.split()[1] for l in inst if '-security' in l]
        if sec:
            res.append(R('updates.security', WARN, f'{len(sec)} עדכוני אבטחה ממתינים: ' + ', '.join(sec[:6])))
        else:
            res.append(R('updates.security', PASS, f'אין עדכוני אבטחה ממתינים ({len(inst)} עדכונים רגילים)'))
    else:
        res.append(unchecked('updates.security', 'apt-get לא זמין'))
    flag = Path('/var/run/reboot-required')
    if flag.exists():
        try:
            pkgs = Path('/var/run/reboot-required.pkgs').read_text().split()
        except OSError:
            pkgs = []
        days = (time.time() - flag.stat().st_mtime) / 86400
        res.append(R('updates.reboot', WARN, f'נדרש אתחול כבר {days:.0f} ימים' + (f" ({', '.join(pkgs[:4])})" if pkgs else '')))
    elif os.name != 'nt':
        res.append(R('updates.reboot', PASS, 'לא נדרש אתחול'))
    try:
        auto = Path('/etc/apt/apt.conf.d/20auto-upgrades').read_text()
        on = re.search(r'Unattended-Upgrade\s+"1"', auto) and re.search(r'Update-Package-Lists\s+"1"', auto)
    except OSError:
        on = None
    if on is None and os.name == 'nt':
        pass
    elif on and _prop(ctx, 'unattended-upgrades', 'ActiveState') == 'active':
        res.append(R('updates.unattended', PASS, 'עדכונים אוטומטיים פעילים'))
    else:
        res.append(R('updates.unattended', FAIL, 'עדכונים אוטומטיים כבויים'))
    return res


# ── 6. logins + tailscale ──────────────────────────────────────────────────────────────────────────────────────────

def _tailnet(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a in (TAILNET_V4 if a.version == 4 else TAILNET_V6)


def check_logins(ctx: Ctx) -> list[dict]:
    res = []
    rc, out = ctx.sudo('journalctl', '-u', 'ssh', '-u', 'sshd', '--since', '24 hours ago', '--no-pager', '-q', '-o', 'cat',
                       timeout=60)
    if rc in (0, 1):
        failed = len(re.findall(r'Failed (?:password|publickey)|Invalid user|authentication failure', out))
        res.append(R('ssh_failed', WARN, f'{failed} ניסיונות SSH כושלים ב-24 שעות') if failed > SSH_FAIL_WARN
                   else R('ssh_failed', PASS, f'{failed} ניסיונות SSH כושלים ב-24 שעות'))
        ips = re.findall(r'Accepted \S+ for \S+ from ([0-9a-fA-F.:]+)', out)
        outside = sorted({ip for ip in ips if not _tailnet(ip)})
        res.append(R('ssh_accepted', FAIL, f'כניסת SSH מחוץ לטיילנט ({len(outside)} כתובות)') if outside
                   else R('ssh_accepted', PASS, f'{len(ips)} כניסות SSH, כולן מהטיילנט'))
    else:
        res.append(unchecked('ssh_failed', 'אין journalctl/sudo'))
    rc, out = ctx.sudo('journalctl', '-u', 'fail2ban', '--since', '24 hours ago', '--no-pager', '-q', '-o', 'cat', timeout=60)
    if rc in (0, 1):
        bans = len(re.findall(r'\] Ban ', out))
        res.append(R('fail2ban_bans', PASS if bans <= 5 else WARN, f'{bans} חסימות fail2ban ב-24 שעות'))
    rc, out = ctx.run(['tailscale', 'status', '--json'], 30)
    if rc != 0:
        res.append(unchecked('tailscale', 'tailscale לא זמין') if rc == 127 else R('tailscale', FAIL, 'tailscale לא עונה'))
        return res
    try:
        d = json.loads(out)
        me = d.get('Self') or {}
        if d.get('BackendState') != 'Running' or not me.get('Online'):
            res.append(R('tailscale', FAIL, f"tailscale לא מחובר ({d.get('BackendState')})"))
        elif me.get('KeyExpiry'):
            exp = datetime.fromisoformat(me['KeyExpiry'].replace('Z', '+00:00'))
            days = (exp - ctx.now).days
            st = FAIL if days < TS_KEY_FAIL_DAYS else WARN if days < TS_KEY_WARN_DAYS else PASS
            res.append(R('tailscale', st, f"tailscale מחובר, המפתח פג ב-{exp.strftime('%d/%m/%Y')} (עוד {days} ימים)"))
        else:
            res.append(R('tailscale', PASS, 'tailscale מחובר, למפתח אין תפוגה'))
    except (ValueError, TypeError):
        res.append(R('tailscale', WARN, 'פלט tailscale לא קריא'))
    return res


# ── 7. secrets hygiene ─────────────────────────────────────────────────────────────────────────────────────────────

def secret_values(env: dict, pii: bool | None = None) -> list[bytes]:
    """Secret .env values. pii=True: only the personal IDs (PII_ENV), False: only credentials, None: both."""
    out = []
    for k, v in env.items():
        if SECRET_ENV_RX.search(k) and len(v) >= 8 and v.lower() not in ('true', 'false', 'changeme'):
            if pii is None or (k in PII_ENV) == pii:
                out.append(v.encode('utf-8'))
    return out


def _git_value_files(ctx: Ctx, gd: list[str], rev: str, values: list[bytes]) -> int:
    if not values:
        return 0
    args = [*gd, 'grep', '-I', '-l', '-F']
    for v in values:
        args += ['-e', v.decode('utf-8', 'replace')]
    rc, out = ctx.run(args + [rev, '--'], 60)
    return len([l for l in out.splitlines() if l.strip()]) if rc == 0 else 0


def scan_bytes(data: bytes, values: list[bytes]) -> dict:
    """{kind: count}. Never returns the matches themselves."""
    hits = {}
    for kind, rx in TOKEN_RX.items():
        n = len(rx.findall(data))
        if n:
            hits[kind] = n
    n = sum(data.count(v) for v in values)
    if n:
        hits['ערך מ-.env'] = n
    return hits


def scan_targets(ctx: Ctx) -> list[Path]:
    since = time.time() - 26 * 3600
    out = []
    logs = ctx.root / 'logs'
    if logs.is_dir():
        out += [p for p in logs.iterdir() if p.is_file() and not p.name.endswith('.gz') and p.stat().st_mtime >= since]
    data = ctx.root / 'data'
    if data.is_dir():
        out += sorted(data.glob('*.jsonl'))
    out += [p for p in tmp_logs(ctx) if p.stat().st_mtime >= since]
    return out


def check_secrets(ctx: Ctx) -> list[dict]:
    res = []
    values = secret_values(ctx.env)
    found = {}
    targets = scan_targets(ctx)
    for p in targets:
        try:
            with open(p, 'rb') as f:
                data = f.read(SCAN_MAX_BYTES)
        except OSError:
            continue
        h = scan_bytes(data, values)
        if h:
            found[p.name] = h
    if found:
        # a key-shaped string anywhere, or any secret in a /tmp log other users can read = FAIL; an .env value
        # (the TAU ID, say) in a private log = WARN
        shared = {p.name for p in targets if p.parent == ctx.tmp and (_mode(p) or 0) & 0o004}
        bad = any(any(k != 'ערך מ-.env' for k in h) or f in shared for f, h in found.items())
        desc = ', '.join(f"{f}: " + '+'.join(f'{n} {k}' for k, n in h.items()) for f, h in list(found.items())[:5])
        res.append(R('secrets.logs', FAIL if bad else WARN, f'סודות בלוגים/בנתונים ({len(found)} קבצים): {desc}'))
    else:
        res.append(R('secrets.logs', PASS, f'{len(targets)} לוגים וקבצי jsonl נסרקו, אין סודות'))
    git = Path(os.environ.get('SSA_GIT_DIR') or (ctx.home / 'smart_study_agent.git'))
    gd = ['git', f'--git-dir={git}'] if git.is_dir() else ['git', '-C', str(ctx.root)]
    rev = 'main' if git.is_dir() else 'HEAD'
    rc, out = ctx.run([*gd, 'grep', '-I', '-c', '-E', GIT_TOKEN_ERE, rev, '--'], 60)
    if rc == 1:
        hits = 0
    elif rc == 0:
        hits = len([l for l in out.splitlines() if l.strip()])
    else:
        res.append(unchecked('secrets.git', 'git grep נכשל'))
        return res
    creds = _git_value_files(ctx, gd, rev, secret_values(ctx.env, pii=False))
    ids = _git_value_files(ctx, gd, rev, secret_values(ctx.env, pii=True))
    if hits or creds:
        res.append(R('secrets.git', FAIL, f'סוד בקוד ב-git: {hits} קבצים בצורת טוקן, {creds} עם מפתח/סיסמה מ-.env — לסובב'))
    elif ids:
        res.append(R('secrets.git', WARN, f'מספר הזהות (TAU_ID) מופיע ב-{ids} קבצים במעקב git'))
    else:
        res.append(R('secrets.git', PASS, 'אין סודות בקבצים במעקב git'))
    return res


# ── 8. tokens ──────────────────────────────────────────────────────────────────────────────────────────────────────

def _age(p: Path, now: datetime) -> str:
    try:
        d = (now.timestamp() - p.stat().st_mtime) / 86400
    except OSError:
        return 'חסר'
    return 'עודכן היום' if d < 1 else f'עודכן לפני {d:.0f} ימים'


def _google_live(path: Path, kind: str) -> str | None:
    """None = works. A read-only call; a refreshed token stays in memory (the owner script writes it, not this)."""
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    if path.suffix == '.pickle':
        import pickle
        with open(path, 'rb') as f:
            creds = pickle.load(f)                      # our own file, 600, written by scripts/gmail_reauth.py
    else:
        from google.oauth2.credentials import Credentials
        creds = Credentials.from_authorized_user_file(str(path))
    scopes = set(getattr(creds, 'scopes', None) or [])
    if scopes and scopes <= {'https://www.googleapis.com/auth/gmail.send'}:
        creds.refresh(Request())                         # send-only: no read call exists, a forced refresh proves it
        return None
    if not creds.valid and getattr(creds, 'refresh_token', None):
        creds.refresh(Request())
    if kind == 'calendar':
        build('calendar', 'v3', credentials=creds, cache_discovery=False).events().list(
            calendarId='primary', maxResults=1).execute(num_retries=1)
    else:
        build('gmail', 'v1', credentials=creds, cache_discovery=False).users().getProfile(userId='me').execute(num_retries=1)
    return None


def _nlm_live() -> str | None:
    import asyncio
    from notebooklm import NotebookLMClient

    async def probe():
        async with NotebookLMClient.from_storage() as c:
            return len(await c.notebooks.list())
    asyncio.run(asyncio.wait_for(probe(), timeout=60))
    return None


def check_tokens(ctx: Ctx) -> list[dict]:
    res = []
    items = [('token.gmail', 'Gmail', ctx.root / 'config' / 'gmail_token.pickle', lambda p: _google_live(p, 'gmail')),
             ('token.tau_mail', 'מייל אוניברסיטה', ctx.root / 'config' / 'tau_mail_token.json', lambda p: _google_live(p, 'gmail')),
             ('token.gcal', 'יומן', ctx.root / 'config' / 'gcal_token.json', lambda p: _google_live(p, 'calendar')),
             ('token.rclone', 'Drive (rclone)', ctx.home / '.config' / 'rclone' / 'rclone.conf', lambda p: _rclone_live(ctx)),
             ('token.notebooklm', 'NotebookLM', ctx.home / '.notebooklm' / 'profiles' / 'default' / 'storage_state.json',
              lambda p: _nlm_live())]
    for cid, name, path, live in items:
        if not path.exists():
            res.append(R(cid, WARN, f'{name}: הקובץ חסר'))
            continue
        age = _age(path, ctx.now)
        if not ctx.live:
            res.append(R(cid, PASS, f'{name}: {age} (בלי בדיקה חיה)'))
            continue
        try:
            err = live(path)
        except Exception as e:                           # the message may carry a URL, never a token: type only
            err = type(e).__name__
        res.append(R(cid, PASS, f'{name}: עובד, {age}') if not err else R(cid, WARN, f'{name}: הקריאה החיה נכשלה ({err}), {age}'))
    return res


def _rclone_live(ctx: Ctx) -> str | None:
    rc, out = ctx.run(['rclone', 'listremotes'])
    remotes = [l.strip() for l in out.splitlines() if l.strip().endswith(':')] if rc == 0 else []
    if not remotes:
        return 'אין remote'
    rc, _ = ctx.run(['rclone', 'lsd', remotes[0], '--max-depth', '1'], 90)
    return None if rc == 0 else f'rclone {rc}'


# ── 9. app invariants ──────────────────────────────────────────────────────────────────────────────────────────────

def decorated(path: Path, name: str = 'api_only') -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding='utf-8'))
    except (OSError, SyntaxError):
        return set()
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in node.decorator_list:
                f = d.func if isinstance(d, ast.Call) else d
                if (isinstance(f, ast.Name) and f.id == name) or (isinstance(f, ast.Attribute) and f.attr == name):
                    out.add(node.name)
    return out


def check_app(ctx: Ctx) -> list[dict]:
    res = []
    lost = []
    total = 0
    for rel, want in API_ONLY_EXPECTED.items():
        got = decorated(ctx.root / rel)
        total += len(got)
        lost += [f'{rel}:{f}' for f in sorted(want - got)]
    res.append(R('app.api_only', FAIL, 'נעלם @api_only מפונקציה שמטפלת בטקסט של אחרים: ' + ', '.join(lost)) if lost
               else R('app.api_only', PASS, f'{total} פונקציות תחת @api_only, כל הצפויות במקום'))
    no_guard = [s for s in GUARD_SITES if 'install_guard(' not in _text(ctx.root / s)]
    res.append(R('app.budget_guard', FAIL, 'install_guard חסר ב: ' + ', '.join(no_guard)) if no_guard
               else R('app.budget_guard', PASS, 'שומר התקציב מותקן בכל נקודות הכניסה'))
    env = {**ctx.env, **{k: v for k, v in os.environ.items() if k.startswith('SSA_')}}
    caps = []
    try:
        if float(env.get('SSA_BRAIN_MAX_COST', '0.10')) > 0.5:
            caps.append('SSA_BRAIN_MAX_COST')
        if float(env.get('SSA_BRAIN_DAY_CAP', '1.0')) > 5:
            caps.append('SSA_BRAIN_DAY_CAP')
        if float(env.get('SSA_BRAIN_TIMEOUT', '70')) > 300:
            caps.append('SSA_BRAIN_TIMEOUT')
    except ValueError:
        caps.append('ערך לא מספרי')
    for label, f, rx, lim in CODE_CAPS:
        m = re.search(rx, _text(ctx.root / f), re.M)
        if not m or float(m.group(1)) > lim:
            caps.append(label)
    flags = f"SSA_GROUPS={env.get('SSA_GROUPS', '0') or '0'}, SSA_BRAIN={env.get('SSA_BRAIN', '0') or '0'}"
    res.append(R('app.caps', FAIL, 'תקרה חסרה/גבוהה מדי: ' + ', '.join(caps)) if caps
               else R('app.caps', PASS, f'תקרות במקום ({flags}, תקציב $50)'))
    lock = ctx.root / 'data' / 'budget_lock.json'
    if lock.exists():
        try:
            d = json.loads(lock.read_text(encoding='utf-8'))
            res.append(R('app.budget_lock', WARN, f"נעילת תקציב פעילה: {str(d.get('reason') or '')[:60]}"))
        except ValueError:
            res.append(R('app.budget_lock', FAIL, 'budget_lock.json פגום'))
    else:
        res.append(R('app.budget_lock', PASS, 'אין נעילת תקציב'))
    gate = _text(ctx.root / 'tests' / 'test_safety_critical.py')
    bodies = re.split(r'^def test_', gate, flags=re.M)[1:]
    tests = bodies
    count = {w: sum(1 for b in bodies if w in b) for w in GATE_TESTS_MIN}
    short = [f'{w} {count[w]}/{n}' for w, n in GATE_TESTS_MIN.items() if count[w] < n]
    runner = _text(ctx.root / 'deploy' / 'hooks' / 'ssa-gate-run')
    if 'test_safety_critical' not in runner:
        short.append('ssa-gate-run לא מריץ את test_safety_critical')
    res.append(R('app.gate_tests', FAIL, 'בדיקות השער חסרות: ' + ', '.join(short)) if short
               else R('app.gate_tests', PASS, f"{len(tests)} בדיקות בשער (settle {count['settle']}, api_only {count['api_only']})"))
    return res


def _text(p: Path) -> str:
    try:
        return p.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''


# ── runner ─────────────────────────────────────────────────────────────────────────────────────────────────────────

CHECKS = (check_ports, check_ufw, check_egress, check_modes, check_accounts, check_services, check_updates,
          check_logins, check_secrets, check_tokens, check_app)


def run_all(ctx: Ctx) -> list[dict]:
    out = []
    for fn in CHECKS:
        try:
            out += fn(ctx)
        except Exception as e:                   # one broken check must not hide the rest
            out.append(R(fn.__name__.replace('check_', ''), WARN, f'הבדיקה קרסה: {type(e).__name__}'))
    return out


def summary(results: list[dict]) -> dict:
    return {s.lower(): sum(1 for r in results if r['status'] == s) for s in (PASS, WARN, FAIL)}


def _group(cid: str) -> str:
    return cid.split('.')[0]


def new_fails(results: list[dict], prev: dict | None, today: str) -> tuple[list[dict], dict]:
    """(FAILs that weren't FAIL last run, {id: since}) — `since` keeps the first day of an ongoing FAIL."""
    before = {r['id']: r for r in (prev or {}).get('checks', [])}
    since = {}
    fresh = []
    for r in results:
        if r['status'] != FAIL:
            continue
        p = before.get(r['id'])
        if p and p.get('status') == FAIL:
            since[r['id']] = p.get('since') or today
        else:
            since[r['id']] = today
            fresh.append(r)
    return fresh, since


def alert_text(fresh: list[dict]) -> str:
    lines = [f'🔒 *בדיקת אבטחה: {len(fresh)} כשלים חדשים*']
    lines += [f"• {r['id']}: {r['reason']}" for r in fresh[:8]]
    lines.append('פירוט: data/security_status.json · תיקון: docs/RUNBOOK.md (בדיקת אבטחה לילית)')
    return '\n'.join(lines)


def table(results: list[dict]) -> str:
    icon = {PASS: '✅', WARN: '⚠️', FAIL: '⛔'}
    return '\n'.join(f"{icon[r['status']]} {r['status']:<4} {r['id']:<22} {r['reason']}" for r in results)


def _write_private(p: Path, text: str, append: bool = False) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, 'a' if append else 'w', encoding='utf-8') as f:
        f.write(text)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def main(argv: list[str] | None = None, ctx: Ctx | None = None, status_path: Path | None = None,
         history_path: Path | None = None, notify=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    dry = '--dry' in argv or '--dry-run' in argv
    ctx = ctx or Ctx(live='--no-live' not in argv)
    results = run_all(ctx)
    s = summary(results)
    print(table(results))
    print(f"\n{s['pass']} PASS · {s['warn']} WARN · {s['fail']} FAIL")
    if dry:
        return 0
    status_path, history_path = status_path or STATUS, history_path or HISTORY
    try:
        prev = json.loads(status_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        prev = None
    today = ctx.now.date().isoformat()
    fresh, since = new_fails(results, prev, today)
    for r in results:
        if r['id'] in since:
            r['since'] = since[r['id']]
    doc = {'ts': ctx.now.isoformat(timespec='seconds'), 'summary': s, 'checks': results}
    _write_private(status_path, json.dumps(doc, ensure_ascii=False, indent=1))
    _write_private(history_path, json.dumps({'ts': doc['ts'], **s, 'fail': [r['id'] for r in results if r['status'] == FAIL],
                                             'warn': [r['id'] for r in results if r['status'] == WARN]},
                                            ensure_ascii=False) + '\n', append=True)
    if fresh:
        if notify is None:
            sys.path.insert(0, str(ctx.root))
            from utils.notify import notify
        key = 'security:' + today + ':' + sha16(','.join(sorted(r['id'] for r in fresh)).encode())
        urgent = any(_group(r['id']) in COMPROMISE or r['id'] in COMPROMISE for r in fresh)
        print('alert:', notify(alert_text(fresh), key=key, priority='urgent' if urgent else 'normal'))
    return 0


if __name__ == '__main__':          # last line
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except AttributeError:
        pass
    sys.exit(main())
