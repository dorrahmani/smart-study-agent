"""
scripts/release_scan.py — pattern-based leak scanner. Run it before every push:

    python scripts/release_scan.py            # scans what `git add -A` would commit (honours .gitignore)
    python scripts/release_scan.py --all      # scans EVERY file, gitignored ones too (data/, logs/, .env …)

It looks for SHAPES of secrets and personal data (API keys, tokens, phone numbers,
e-mails, IPs, ID numbers, cookies, private keys, tailnet hostnames, home paths …).
It contains no personal values. For extra safety keep your OWN list of literal
values (your phone, e-mail, ID, server IP) in a file OUTSIDE the repo and pass it:

    python scripts/release_scan.py --values ~/my_private_values.txt   # one value per line

Exit code 1 if anything is found. Matches are printed masked (first/last chars only).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ── files that must never be in the repo at all ─────────────────────────────
FORBIDDEN_FILES = [
    r'(^|/)\.env$', r'(^|/)\.env\.(?!example$)[^/]+$', r'\.pickle$', r'\.pkl$', r'storage_state[^/]*\.json$',
    r'(^|/)[^/]*token[^/]*\.json$', r'(^|/)[^/]*credentials[^/]*\.json$', r'\.key$', r'\.pem$', r'\.p12$',
    r'(^|/)id_(rsa|ed25519)(\.pub)?$', r'(^|/)cookies?[^/]*\.(json|txt|sqlite)$', r'(^|/)config/secrets/',
    r'(^|/)config/(courses|professors)\.yaml$', r'(^|/)config/recipients[^/]*$', r'(^|/)config/notebooklm_notebooks\.json$',
    r'(^|/)config/(sent_log|approval_log|reviewer_sent_log|api_budget)\.json$', r'(^|/)config/[^/]*\.env$',
    r'(^|/)data/', r'(^|/)logs/', r'(^|/)backups/', r'\.bak$', r'\.sqlite3?$', r'\.db$',
    r'\.(mp3|mp4|wav|m4a|ogg|srt|vtt)$', r'master_token', r'(^|/)rclone\.conf$', r'client_secret[^/]*\.json$',
    r'service_account[^/]*\.json$',
]

# ── content patterns ────────────────────────────────────────────────────────
PATTERNS = [
    ('google-api-key',      r'AIza[0-9A-Za-z_\-]{30,}'),
    ('openai-key',          r'\bsk-(?:proj-|ant-)?[A-Za-z0-9_\-]{20,}'),
    ('telegram-bot-token',  r'\b\d{8,10}:[A-Za-z0-9_\-]{30,}'),
    ('google-oauth-client', r'\b\d{6,}-[a-z0-9]{20,}\.apps\.googleusercontent\.com'),
    ('google-oauth-secret', r'GOCSPX-[A-Za-z0-9_\-]{10,}'),
    ('google-refresh-token', r'\b1//0[A-Za-z0-9_\-]{20,}'),
    ('google-access-token', r'\bya29\.[A-Za-z0-9_\-]{20,}'),
    ('github-token',        r'\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}'),
    ('slack-token',         r'\bxox[abposr]-[A-Za-z0-9\-]{10,}'),
    ('aws-key',             r'\bAKIA[0-9A-Z]{16}\b'),
    ('private-key',         r'-----BEGIN [A-Z ]*PRIVATE KEY-----'),
    ('bearer-token',        r'Bearer\s+[A-Za-z0-9_\-\.=]{24,}'),
    ('cookie-value',        r'(?i)\b(?:SID|HSID|SSID|APISID|SAPISID|__Secure-[0-9A-Za-z_-]+|\.ASPXAUTH|MoodleSession)\s*[=:]\s*["\']?[A-Za-z0-9_\-\.%/+]{16,}'),
    ('password-assignment', r'(?i)\b(?:password|passwd|pwd|secret|api_key|apikey|token)\s*[=:]\s*["\'][^"\'\s{}<>$]{8,}["\']'),
    ('il-mobile',           r'(?<![\d.])(?:\+?972[\s-]?|0)5\d(?:[\s-]?\d){7}(?![\d])'),
    ('wa-jid',              r'\b\d{10,15}@(?:c\.us|s\.whatsapp\.net|g\.us|lid)\b'),
    ('email',               r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b'),
    ('ipv4',                r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])'),
    ('tailnet-host',        r'\b[a-z0-9\-]+\.(?:tail[0-9a-f]+|[a-z0-9\-]+)\.ts\.net\b'),
    ('home-path',           r'(?i)(?:[A-Z]:[\\/]+Users[\\/]+|(?<![\w.])/home/|(?<![\w.])/Users/)([A-Za-z0-9_\-][A-Za-z0-9._\-]*)'),
    ('israeli-id-near-word', r'(?i)(?:ת["״]?ז|תעודת זהות|tau_id|id[_ ]?number|\bID\b)[^\n\d]{0,25}(?<!\d)\d{9}(?!\d)'),
    ('uuid',                r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b'),
    ('drive-folder-id',     r'(?:folders/|[?&]id=|drive\.google\.com/[^\s]*?/d/)[A-Za-z0-9_\-]{25,}'),
    ('notebooklm-url',      r'notebooklm\.google\.com/notebook/[A-Za-z0-9\-]{8,}'),
    ('google-calendar-id',  r'\b[a-z0-9]{20,}@group\.calendar\.google\.com\b'),
    ('long-hex',            r'(?<![0-9A-Za-z])(?:[0-9a-f]{32,}|[0-9A-F]{32,})(?![0-9A-Za-z])'),
    ('gpg-fingerprint',     r'(?<![0-9A-Za-z])[0-9A-F]{4}(?:\s?[0-9A-F]{4}){9}(?![0-9A-Za-z])'),
    ('ssh-fingerprint',     r'SHA256:[A-Za-z0-9+/]{43}'),
    ('long-base64',         r'(?<![A-Za-z0-9+/_\-])[A-Za-z0-9+/_\-]{60,}={0,2}(?![A-Za-z0-9+/_\-])'),
]

# ── values that look sensitive but are documented placeholders ──────────────
ALLOW = {
    'email': re.compile(r'(?i)@(?:example\.(?:com|org|net)|[a-z0-9.\-]*\.example|test|localhost|x\.com|users\.noreply\.github\.com)$'
                        r'|^(?:student\d*@mail\.tau\.ac\.il|prof@tau\.ac\.il|noreply@anthropic\.com|git@github\.com)$'
                        r'|^[^@]+@(?:2x|3x)\.|\.(?:png|jpg|svg|webp)$'
                        # placeholders used by the tests (short local parts, evil.com, fake JIDs/groups):
                        r'|^[a-z]{1,2}(?:\.[a-z])?@(?:[a-z]+\.)*(?:tau\.ac\.il|b\.co|evil\.com|nottau\.ac\.il|tau\.ac\.il\.evil\.com|s\.whatsapp\.net|g\.us)$'
                        r'|^(?:attacker@evil\.com|(?:library|secretary|news|lect\d*|office)@(?:tauex\.)?tau\.ac\.il|news@example-library\.org'
                        r'|studies@group\.calendar\.google\.com|claude-agent@smart-study\.local|your_email@gmail\.com|-?X@(?:gmail|Y)\.com'
                        r'|-a\.b@gmail\.com|(?:other|gid|123-456|120363-1)@g\.us|(?:9725\d0000\d{3}|972501234567|12345\d+)@s\.whatsapp\.net)$'),
    'ipv4': re.compile(r'^(?:127\.0\.0\.1|0\.0\.0\.0|255\.255\.255\.\d+|192\.0\.2\.\d+|198\.51\.100\.\d+|203\.0\.113\.\d+'
                       r'|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+|100\.64\.0\.0|100\.100\.100\.100'
                       r'|127\.0\.0\.53|1\.1\.1\.1|8\.8\.8\.8|169\.254\.169\.254|169\.254\.0\.0|\d+\.\d+\.\d+\.0)$'),
    'il-mobile': re.compile(r'^\D*(?:972|0)[\s-]?5\d[\s-]?(?:0[\s-]?0[\s-]?0|1[\s-]?2[\s-]?3[\s-]?4[\s-]?5[\s-]?6[\s-]?7|X)'),
    'wa-jid': re.compile(r'^(?:9725\d0000\d{3}|1000000000\d\d|12345\d+|9725\d1234567)@'),
    'home-path': re.compile(r'(?i)(?:Users[\\/]+|/home/|/Users/)(?:you|your[-_ ]?user(?:name)?|<[^>]*>|ubuntu|runner|root|Public|USERNAME|\$\w+|%\w+%|me|user|claudeagent|geminiagent|smart|smartstudy|friend\w*|tenant\w*|ssa-gate|m|alice|bob|mallory)$'),
    'uuid': re.compile(r'^(?:00000000-|11111111-1111-1111-1111-111111111111|22222222-2222-2222-2222-222222222222)'),
    'long-hex': re.compile(r'^0+$|^f+$'),
    'gpg-fingerprint': re.compile(r'^[0\s]+$'),
    'ssh-fingerprint': re.compile(r'^SHA256:0+$'),
    'israeli-id-near-word': re.compile(r'(?:012345678|123456789|000000000)$'),
    'tailnet-host': re.compile(r'^your-server\.your-tailnet\.ts\.net$'),
}

TEXT_SKIP_EXT = {'.png', '.jpg', '.jpeg', '.gif', '.ico', '.pdf', '.woff', '.woff2', '.ttf', '.zip'}
SKIP_DIRS = {'.git', '__pycache__', '.pytest_cache', 'venv', '.venv', 'node_modules'}
IGNORED_BY_DEFAULT = {'data', 'logs', 'backups'}
SELF = Path(__file__).resolve()


def mask(s: str) -> str:
    s = s.strip()
    return s if len(s) <= 6 else f'{s[:3]}…{s[-2:]} (len {len(s)})'


def scan(root: Path, include_ignored: bool, extra_values: list[str]) -> list[tuple[str, int, str, str]]:
    hits = []
    compiled = [(n, re.compile(p)) for n, p in PATTERNS]
    vals = [(v, re.compile(re.escape(v), re.I)) for v in extra_values if len(v) >= 3]
    only = None
    if not include_ignored and (root / '.git').exists():
        import subprocess
        out = subprocess.run(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
                             cwd=root, capture_output=True).stdout.decode('utf-8', 'replace')
        only = {x for x in out.split(chr(0)) if x}      # what `git add -A` would commit
    for p in sorted(root.rglob('*')):
        rel_parts = p.relative_to(root).parts
        if any(x in SKIP_DIRS for x in rel_parts):
            continue
        if not include_ignored and rel_parts and rel_parts[0] in IGNORED_BY_DEFAULT:
            continue
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if only is not None and rel not in only:
            continue
        for fp in FORBIDDEN_FILES:
            if re.search(fp, rel):
                hits.append((rel, 0, 'forbidden-file', rel))
                break
        if p.suffix.lower() in TEXT_SKIP_EXT:
            continue
        try:
            text = p.read_bytes().decode('utf-8')
        except UnicodeDecodeError:
            hits.append((rel, 0, 'binary-unknown', rel))
            continue
        if p.resolve() == SELF:
            continue
        is_req = p.name.startswith('requirements')
        for ln, line in enumerate(text.splitlines(), 1):
            for name, rx in compiled:
                for m in rx.finditer(line):
                    val = m.group(1) if name == 'home-path' else m.group(0)
                    full = m.group(0)
                    if is_req and name == 'ipv4':
                        continue    # package versions like 1.2.3.4
                    allow = ALLOW.get(name)
                    if allow and (allow.search(full if name in ('home-path',) else val)):
                        continue
                    if name == 'email' and allow and allow.search('@' + val.split('@')[-1]):
                        continue
                    if name == 'long-base64' and '/' in val and re.search(r'[a-z]{3,}/', val):
                        continue        # a URL path
                    if name in ('long-base64',) and (re.fullmatch(r'[A-Za-z_\-/]+', val) or re.fullmatch(r'[a-z0-9_/\-]+', val)):
                        continue        # identifiers / paths, not blobs
                    hits.append((rel, ln, name, val))
            for v, rx in vals:
                if rx.search(line):
                    hits.append((rel, ln, 'private-value', v))
    return hits


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding='utf-8')   # Windows consoles default to a legacy code page
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--all', action='store_true', help='also scan gitignored data/, logs/, backups/')
    ap.add_argument('--values', help='file with your own literal private values (kept OUTSIDE the repo)')
    ap.add_argument('--root', default=str(ROOT))
    a = ap.parse_args()
    extra = []
    if a.values:
        extra = [x.strip() for x in Path(a.values).expanduser().read_text(encoding='utf-8').splitlines()
                 if x.strip() and not x.startswith('#')]
    hits = scan(Path(a.root).resolve(), a.all, extra)
    for rel, ln, name, val in hits:
        print(f'{rel}:{ln}  [{name}]  {mask(val)}')
    by = {}
    for h in hits:
        by[h[2]] = by.get(h[2], 0) + 1
    print(f'\n{len(hits)} finding(s)' + (': ' + ', '.join(f'{k}={v}' for k, v in sorted(by.items())) if by else ' — clean.'))
    return 1 if hits else 0


if __name__ == '__main__':
    sys.exit(main())
