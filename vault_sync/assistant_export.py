"""
vault_sync/assistant_export.py — everything the assistant knows and does → readable, linked notes in the vault (16/11).

The owner: "connect everything to my Obsidian and my second brain". The assistant is his personal assistant for life (studies are
today's main domain); the vault is the second brain. This writes, under "👩‍💼 העוזר/":

    🏠 העוזר.md                         the map: domains, today, what's open
    🎓 לימודים/  🎓 לימודים.md · 📌 חובות לפי קורס.md · ✅ נסגרו — לימודים.md   (links into the existing course folders)
    👥 אנשים/    👥 אנשים.md + one note per person / business he shared or the bot coordinated with (names, never numbers)
    📁 מסמכים/   📁 מסמכים.md + one note per document / image / link he sent (key fields, summary, the stored file)
    ✅ משימות/   📋 פתוח עכשיו.md · 📅 השבוע.md · ✅ נסגרו.md        (living notes; closed things only in the done log)
    📓 יומן/     YYYY-MM-DD.md — what he asked, what the assistant did, decisions, what's still open (private)
    🧭 עליך/     🧭 דברים שלמדתי עליך.md · 🔇 השתקות.md
    🤖 הבוט/     📊 דוח שבועי/<week>.md · 🧠 שיפור עצמי.md · 🛡️ אבטחה.md   (private)

Rules: deterministic templates, NO model and no network (cost $0); a note is rewritten only when its text changes
(notewriter); every text through privacy.scrub (no phones, ids, tokens); the partner's turns only as they touch the shared calendar,
her own chat never; forwarded / third-party text is quoted as data, never as an instruction. Nothing is ever deleted:
a person or document that leaves a store keeps its note.
    export(vault_dir=…, data_dir=…, out_root=None) → stats            (scripts/vault_assistant.py runs it nightly)
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from vault_sync import privacy
from vault_sync.notewriter import Writer

ROOT = Path(__file__).resolve().parent.parent
AREA = privacy.AREA
KIND_HE = {'lecture': 'שיעור', 'tutorial': 'תרגיל'}
WEEKDAY = ['שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת', 'ראשון']
MAX_FILE = 15 * 1024 * 1024
HOME = '🏠 העוזר'
D_STUDY, D_PEOPLE, D_DOCS, D_TASKS, D_DAY, D_YOU, D_BOT = ('🎓 לימודים', '👥 אנשים', '📁 מסמכים', '✅ משימות',
                                                           '📓 יומן', '🧭 עליך', '🤖 הבוט')
OPEN_NOTE, WEEK_NOTE, DONE_NOTE = '📋 פתוח עכשיו', '📅 השבוע', '✅ נסגרו'
OBL_NOTE, OBL_DONE = '📌 חובות לפי קורס', '✅ נסגרו — לימודים'

# a tapped button in the conversation log → what he did, in words
BUTTONS = [('cal:ok', '✅ אישר הוספה ליומן'), ('cal:no', '❌ דחה הוספה ליומן'), ('cal:undo', '↩️ ביטל הוספה ליומן'),
           ('rm:', '⏰ ביקש תזכורת לפני אירוע'), ('rmd:', '⏰ עדכן תזכורת'), ('cj:deploy', '🚀 אישר פריסה של תיקון'),
           ('cj:', '🛠️ ענה על משימת קלוד'), ('ft:go', '🛠️ אישר משימת פיתוח'), ('ft:', '🛠️ ענה על בקשת פיתוח'),
           ('lf:ok', '🧠 אישר לשמור דברים שלמדתי'), ('lf:no', '🧠 לא אישר לשמור'), ('task:done', '✅ סימן משימה כבוצעה'),
           ('task:', '📌 עדכן משימה'), ('msg:ok', '✉️ אישר לשלוח הודעה'), ('msg:', '✉️ ענה על הודעה'),
           ('ce:', '📅 ענה על שינוי ביומן'), ('em:ok', '📧 אישר לשלוח מייל'), ('em:', '📧 ענה על מייל'),
           ('hw:', '📚 עדכן שיעורי בית'), ('oi:', '📋 סגר / עדכן משהו פתוח'), ('sec:', '🗂️ ענה להצעה של המזכירה'),
           ('esc:', '🚨 ענה על מעקב'), ('wa:', '🌐 ענה על סוכן הדפדפן'), ('bk:', '🍽️ ענה על הזמנה'),
           ('co:', '🤝 ענה על תיאום'), ('pp:', '💞 ענה על הצעה של בת הזוג'), ('vi:', '✍️ ענה על שורה מה-Inbox'),
           ('noop:', ''), ('menu:', '')]


# ── small helpers ───────────────────────────────────────────────────────────

def _json(p: Path, default):
    try:
        return json.loads(Path(p).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def _jsonl(p: Path) -> list[dict]:
    out = []
    try:
        for ln in Path(p).read_text(encoding='utf-8').splitlines():
            try:
                r = json.loads(ln)
                if isinstance(r, dict):
                    out.append(r)
            except ValueError:
                pass
    except OSError:
        pass
    return out


def safe_name(name: str) -> str:
    return re.sub(r'\s+', ' ', re.sub(r'[\\/:*?"<>|#^\[\]]+', ' ', str(name or ''))).strip()[:80] or 'ללא שם'


def clip(s, n: int = 200) -> str:
    """One line, scrubbed, no markdown that would turn into a task / heading / link in Obsidian."""
    t = privacy.scrub(str(s or ''))
    t = re.sub(r'[*_`]+', '', t)
    t = re.sub(r'\[\[|\]\]', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    t = re.sub(r'^(?:[-#>]+\s*|\[\s?[xX ]?\]\s*)+', '', t)
    return t if len(t) <= n else t[:n - 1].rstrip() + '…'


def block(s, n: int = 1500) -> str:
    """Several lines (a document summary) scrubbed, as a quote: shown as text, never as tasks or headings."""
    t = privacy.scrub(str(s or ''))[:n]
    t = re.sub(r'\[\[|\]\]', '', t)
    lines = [re.sub(r'^\s*(?:[-*]\s*\[[ xX]?\]|#+)\s*', '• ', ln).rstrip() for ln in t.splitlines()]
    return '\n'.join('> ' + ln if ln.strip() else '>' for ln in lines).strip()


def _dt(v) -> datetime | None:
    try:
        return datetime.fromisoformat(str(v)[:19]) if v else None
    except ValueError:
        return None


def _day(v) -> str:
    return str(v or '')[:10]


def _hm(v) -> str:
    s = str(v or '')
    return s[11:16] if len(s) >= 16 else ''


def dmy(v) -> str:
    d = _dt(v)
    if not d:
        return str(v or '')
    return f'{d:%d/%m}' + (f' {d:%H:%M}' if len(str(v)) > 10 and (d.hour or d.minute) else '')


def link(stem: str, alias: str = '') -> str:
    return f'[[{stem}|{alias}]]' if alias and alias != stem else f'[[{stem}]]'


def week_of(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f'{y}-W{w:02d}'


def _out_text(row: dict) -> str:
    for o in row.get('out') or []:
        if isinstance(o, (list, tuple)) and len(o) > 1 and str(o[1]).strip():
            return str(o[1])
    return ''


def _first_line(s: str) -> str:
    return next((ln for ln in str(s or '').splitlines() if ln.strip()), '')


def _vcard_name(text: str) -> tuple[str, bool]:
    """(display name, is a business) from a shared contact card. Never the number."""
    t = str(text or '')
    m = re.search(r'^FN:(.+)$', t, re.M)
    name = (m.group(1) if m else _first_line(t.split('BEGIN:VCARD')[0])).strip()
    biz = bool(re.search(r'^(?:X-WA-BIZ-NAME|ORG):', t, re.M))
    return safe_name(name) if name else '', biz


# ── key fields of a document (deterministic, from the summary the bot already wrote) ──

_BOOKING_RX = re.compile(r'(?:הזמנה|אישור|booking|reservation|confirmation|order)[^\n\d]{0,14}?(?:מס[\'׳]?|מספר|#|no\.?|number)?'
                         r'\s*[:#]?\s*([A-Z]{0,3}\d[\dA-Z-]{3,14})', re.I)
_DATE_RX = re.compile(r'(?<![\d/.])(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?(?![\d/.])')
_AMOUNT_RX = re.compile(r'(?:₪|ש"ח|ש״ח|NIS|\$|€)\s?\d[\d,]*(?:\.\d+)?|\d[\d,]*(?:\.\d+)?\s?(?:₪|ש"ח|ש״ח|שקלים|שקל|NIS|USD|EUR)')
_PLACE_RX = re.compile(r'(?:מלון|hotel|מסעדת|מסעדה|אולם|מקום|כתובת|ב?שדה התעופה)\s*:?\s*([^\n•,.;()*]{2,40})', re.I)


def key_fields(text: str) -> dict:
    t = privacy.scrub(text or '')
    out: dict = {}
    if m := _BOOKING_RX.search(t):
        out['booking'] = m.group(1)
    dates = []
    for m in _DATE_RX.finditer(t):
        d, mo = int(m.group(1)), int(m.group(2))
        if 1 <= d <= 31 and 1 <= mo <= 12:
            s = m.group(0)
            if s not in dates:
                dates.append(s)
    if dates:
        out['dates'] = dates[:4]
    amounts = list(dict.fromkeys(a.strip() for a in _AMOUNT_RX.findall(t)))
    if amounts:
        out['amount'] = amounts[0] if len(amounts) == 1 else amounts[:3]
    rest = t.split('\n', 1)[-1]                         # the first line is the file's name: the place is in the text
    if m := (_PLACE_RX.search(rest) or _PLACE_RX.search(t)):
        out['place'] = clip(m.group(0), 50)
    return out


# ── reading the stores ──────────────────────────────────────────────────────

class Data:
    """Every store the notes come from. Paths beside one data dir, so a test hands it a tmp dir."""

    def __init__(self, data_dir: Path, owner: str = '', partner: str = '', now: datetime | None = None):
        self.dir = Path(data_dir)
        self.now = now or datetime.now()
        self.owner = owner
        self.partner = partner
        self.convo = _jsonl(self.dir / 'agent_conversations.jsonl')
        self.actions = _jsonl(self.dir / 'agent_actions.jsonl')
        self.life = _json(self.dir / 'agent_life.json', {})
        self.tasks = (_json(self.dir / 'tasks.json', {}) or {}).get('tasks') or []
        self.closed = _json(self.dir / 'closed_items.json', []) or []
        self.obl = (_json(self.dir / 'obligations.json', {}) or {}).get('items') or {}
        self.contacts = _json(self.dir / 'contacts.json', {}) or {}
        self.coord = _json(self.dir / 'coord_tasks.json', {}) or {}
        self.nlm_docs = (_json(self.dir / 'nlm_docs.json', {}) or {}).get('docs') or []
        self.now_note = _json(self.dir / 'agent_now.json', {}) or {}
        self.kinds = _json(self.dir / 'wa_question_kinds.json', {}) or {}
        self.options = _json(self.dir / 'wa_options.json', {}) or {}
        self.backlog = (_json(self.dir / 'improvement_backlog.json', {}) or {}).get('items') or []
        self.improve = _json(self.dir / 'improve_state.json', {}) or {}
        self.security = _json(self.dir / 'security_status.json', {}) or {}
        self.tau = (_json(self.dir / 'tau_calendar.json', {}) or {}).get('events') or []
        self.weekly_sent = _json(self.dir / 'weekly_bot_report_sent.json', {}) or {}
        mf = _json(self.dir / 'my_files.json', [])                 # agent/my_files: what he sent, kept and findable
        self.my_files = [r for r in mf if isinstance(r, dict) and self.mine(r)] if isinstance(mf, list) else []
        so = _json(self.dir / 'schedule_overrides.json', [])       # agent/schedule: changes to HIS sessions, with source
        self.overrides = [r for r in so if isinstance(r, dict)] if isinstance(so, list) else []
        self.media = self.dir / 'wa_media'

    # The owner's own turns; the partner's only for the shared calendar (from the action log, never her chat text)
    def owner_rows(self) -> list[dict]:
        return [r for r in self.convo if r.get('role') == 'owner']

    def mine(self, rec: dict, key: str = 'chat') -> bool:
        return not self.owner or rec.get(key) in ('', None, self.owner)

    def owner_actions(self) -> list[dict]:
        return [a for a in self.actions if not self.owner or a.get('sender') == self.owner]

    def partner_calendar(self) -> list[dict]:
        if not self.partner:
            return []
        return [a for a in self.actions if a.get('sender') == self.partner
                and a.get('action') in ('partner_proposal', 'calendar_add', 'calendar_edit')]


# ── courses ─────────────────────────────────────────────────────────────────

def course_links(vault: Path) -> dict[str, tuple[str, str]]:
    """course_key → (link target, display name), into the existing course folders (their _index MOC)."""
    from config.paths import ACTIVE_COURSES, COURSE_NAMES, VAULT_COURSE_FOLDER_TO_KEY
    out = {}
    for ck in ACTIVE_COURSES:
        folders = [f for f, k in VAULT_COURSE_FOLDER_TO_KEY.items() if k == ck]
        have = next((f for f in folders if (vault / f).is_dir()), folders[0] if folders else COURSE_NAMES.get(ck, ck))
        out[ck] = (f'{have}/_index', COURSE_NAMES.get(ck, have))
    return out


def _obl_status(rec: dict, now: datetime) -> str:
    try:
        from agent import obligations
        return obligations.status(rec, now)
    except Exception:
        return 'closed' if rec.get('closed') or rec.get('state') == 'done' else 'open'


# ── the notes ───────────────────────────────────────────────────────────────

class Exporter:
    def __init__(self, data: Data, writer: Writer, vault: Path):
        self.d = data
        self.w = writer
        self.vault = vault
        self.now = data.now
        self.today = self.now.date().isoformat()
        self.courses = course_links(vault)
        self.people: dict[str, dict] = {}
        self.docs: list[dict] = []
        self.doc_of: dict[str, str] = {}
        self.days: dict[str, dict] = defaultdict(lambda: {'asked': [], 'did': [], 'decided': [], 'shared': [], 'people': set(),
                                                          'docs': [], 'remember': []})
        self.memories: list[dict] = []

    # where a note goes
    def rel(self, domain: str, stem: str) -> str:
        return f'{AREA}/{domain}/{stem}.md' if domain else f'{AREA}/{stem}.md'

    def front(self, typ: str, domain: str, **kw) -> dict:
        fm = {'type': typ, 'domain': domain.split(' ', 1)[-1] if domain else 'עוזר'}
        fm.update(kw)
        fm['tags'] = list(dict.fromkeys(['עוזר'] + ([fm['domain']] if domain else []) + list(kw.get('tags') or [])))
        if privacy.domain_private(domain):
            fm['private'] = True
        fm['source'] = 'assistant'
        return fm

    # ── people ──
    def _person(self, name: str) -> dict:
        return self.people.setdefault(name, {'name': name, 'roles': set(), 'first': '', 'last': '', 'lines': [],
                                             'open': [], 'done': [], 'days': set(), 'told': []})

    def _touch(self, p: dict, ts: str, line: str = '') -> None:
        day = _day(ts)
        if day:
            p['first'] = min(p['first'] or day, day)
            p['last'] = max(p['last'], day)
            p['days'].add(day)
            self.days[day]['people'].add(p['name'])
        if line:
            p['lines'].append((str(ts)[:16], line))

    def collect_people(self) -> None:
        for name in self.d.contacts:
            self._person(safe_name(name))['roles'].add('איש קשר שהעוזר יכול לכתוב לו')
        for r in self.d.owner_rows():
            if r.get('kind') != 'contact':
                continue
            name, biz = _vcard_name(r.get('in', ''))
            if not name:
                continue
            p = self._person(name)
            p['roles'].add('עסק' if biz else 'איש קשר')
            self._touch(p, r.get('ts', ''), 'המשתמש שיתף את איש הקשר. העוזר: ' + clip(_first_line(_out_text(r)), 160))
        for cid, c in self.d.coord.items():
            if not self.d.mine(c, 'owner') or not c.get('name'):
                continue
            p = self._person(safe_name(c['name']))
            p['roles'].add('תיאום של העוזר')
            goal = clip(c.get('goal', ''), 220)
            self._touch(p, c.get('created', ''), f'תיאום ({c.get("kind", "")}): {goal}' if goal else 'תיאום')
            for entry in (c.get('log') or [])[-6:]:
                if isinstance(entry, (list, tuple)) and len(entry) > 1:
                    who = 'העוזר' if entry[0] in ('bot', 'me', 'out') else c['name']
                    p['lines'].append(('', f'{who}: {clip(entry[1], 200)}'))
            (p['open'] if c.get('status') in ('active', 'open', 'waiting') else p['done']).append(
                f'תיאום: {goal or c.get("kind", "")}')
        for a in self.d.owner_actions():
            if a.get('action') in ('contact_message', 'message_sent') and a.get('to') and a.get('action') == 'contact_message':
                p = self._person(safe_name(a['to']))
                p['roles'].add('קיבל הודעה מהעוזר')
                self._touch(p, a.get('ts', ''), 'העוזר שלח בשם המשתמש: ' + clip(a.get('text', ''), 240))
                self.days[_day(a.get('ts'))]['did'].append(f'- ✅ {_hm(a.get("ts"))} שלח הודעה ל-{link(safe_name(a["to"]))}')

    def write_people(self) -> None:
        rows = []
        for name in sorted(self.people):
            p = self.people[name]
            roles = sorted(p['roles'])
            body = [f'# 👤 {name}', '', f'**מי:** {", ".join(roles) or "איש קשר"}',
                    f'**מתי:** ' + (f'{dmy(p["first"])} – {dmy(p["last"])}' if p['first'] else '—'), '']
            if p['open']:
                body += ['## ⏳ פתוח'] + [f'- [ ] {clip(x, 200)}' for x in p['open']] + ['']
            if p['done']:
                body += ['## ✅ נסגר'] + [f'- [x] {clip(x, 200)}' for x in p['done']] + ['']
            if p['lines']:
                body += ['## 💬 מה היה'] + [f'- {dmy(ts) + " — " if ts else ""}{ln}' for ts, ln in p['lines'][-15:]] + ['']
            if p['told']:
                body += ['## 🧠 מה סיפרת לי'] + [self._memory_line(m) for m in p['told']] + ['']
            if p['days']:
                body += ['## 📓 ימים', ' · '.join(link(d) for d in sorted(p['days']))]
            body += ['', f'↩️ {link(D_PEOPLE)} · {link(HOME)}', '']
            if name in {safe_name(n) for n in self.d.contacts}:
                body.append('*המספר שמור אצל העוזר (לא בהערה). כדי לכתוב ל' + name + ': "תכתוב ל' + name + ' ש…" בוואטסאפ.*')
            else:
                body.append('*אין לעוזר מספר שמור. לשמור: לשתף איתו את איש הקשר בוואטסאפ.*')
            status = 'open' if p['open'] else 'none'
            self.w.note(self.rel(D_PEOPLE, safe_name(name)),
                        self.front('person', D_PEOPLE, title=name, role=roles, first_seen=p['first'], last_seen=p['last'],
                                   status=status, tags=['אדם']), '\n'.join(body))
            rows.append(f'| {link(safe_name(name), name)} | {", ".join(roles)} | {dmy(p["last"]) or "—"} | '
                        f'{"⏳ " + str(len(p["open"])) if p["open"] else ""} |')
        body = ['# 👥 אנשים ועסקים', '', 'מי שהמשתמש שיתף, או שהעוזר תיאם איתו. בלי מספרי טלפון: הם אצל העוזר.', '',
                '| מי | תפקיד | לאחרונה | פתוח |', '|---|---|---|---|'] + (rows or ['| — | | | |']) + [
                '', '```dataview', 'TABLE role AS "תפקיד", last_seen AS "לאחרונה", status AS "מצב"',
                f'FROM "Smart Study Agent/{AREA}/{D_PEOPLE}"', 'WHERE type = "person"', 'SORT last_seen DESC', '```',
                '', f'↩️ {link(HOME)}']
        self.w.note(self.rel(D_PEOPLE, D_PEOPLE), self.front('moc', D_PEOPLE, title='אנשים'), '\n'.join(body))

    # ── documents ──
    def _media_for(self, row: dict, title: str) -> Path | None:
        if not self.d.media.is_dir():
            return None
        files = [f for f in self.d.media.iterdir() if f.is_file() and f.suffix.lower() not in ('.ogg', '.opus', '.mp3', '.m4a')]
        if title:
            want = safe_name(Path(title).stem)[:30]
            for f in files:
                if want and want in safe_name(f.name.split('__', 1)[-1]):
                    return f
        ts = _dt(row.get('ts'))
        if ts and row.get('kind') == 'image':                   # an image has no name: by time, ±3 minutes
            near = [f for f in files if f.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp')
                    and abs(datetime.fromtimestamp(f.stat().st_mtime) - ts) < timedelta(minutes=3)]
            if len(near) == 1:
                return near[0]
        return None

    def _doc_title(self, name: str, kind: str, ts: str) -> tuple[str, str]:
        nice = re.sub(r'\.(pdf|docx?|jpe?g|png|txt)$', '', clip(name, 120), flags=re.I).strip()
        if not nice or nice in ('התמונה', 'תמונה', 'מסמך'):
            nice = 'תמונה' if kind == 'image' else (nice or 'מסמך')
            return nice, safe_name(f'{_day(ts)} {nice} {_hm(ts).replace(":", "")}')
        return nice, safe_name(f'{_day(ts)} {nice}')

    def collect_docs(self) -> None:
        # 1. agent/my_files (16/11): one record per file / link he sent, with its identifiers and a private copy
        rows_by_ts = {str(r.get('ts', ''))[:16]: r for r in self.d.owner_rows() if r.get('kind') in ('document', 'image')}
        for rec in self.d.my_files:
            ts = rec.get('ts', '')
            row = rows_by_ts.get(str(ts)[:16])
            kind = 'url' if rec.get('url') and not rec.get('path') else (
                'image' if (rec.get('kind') or '') in ('jpg', 'jpeg', 'png', 'webp', 'image') or (row or {}).get('kind') == 'image' else 'file')
            nice, stem = self._doc_title(rec.get('name') or (rec.get('url') or ''), kind, ts)
            src = Path(rec['path']) if rec.get('path') and Path(rec['path']).is_file() else (
                self._media_for(row, rec.get('name', '')) if row else None)
            text = '\n'.join(x for x in (rec.get('summary'), rec.get('keys')) if x)
            self.docs.append({'stem': stem, 'title': nice, 'kind': kind, 'ts': ts, 'summary': rec.get('summary', ''),
                              'row': row or {'kind': kind}, 'fields': key_fields(text), 'file': src,
                              'file_name': rec.get('name', ''), 'ref': rec.get('url') or None})
            if row:
                self.doc_of[row.get('ts', '')] = stem
        covered = {str(d['ts'])[:16] for d in self.docs}
        # 2. older ones the store doesn't have: the conversation log (the bot's own summary) and the NotebookLM sources
        for r in self.d.owner_rows():
            if r.get('kind') not in ('document', 'image') or r.get('forwarded') or str(r.get('ts', ''))[:16] in covered:
                continue
            out = _out_text(r)
            m = re.search(r'📎\s*\*([^*\n]{1,120})\*', out)
            title = (m.group(1).strip() if m else '') or ('תמונה' if r['kind'] == 'image' else 'מסמך')
            nice = re.sub(r'\.(pdf|docx?|jpe?g|png|txt)$', '', title, flags=re.I).strip() or title
            stem = safe_name(f'{_day(r.get("ts"))} {nice}')
            if r['kind'] == 'image' and nice in ('התמונה', 'תמונה'):
                stem = safe_name(f'{_day(r.get("ts"))} תמונה {_hm(r.get("ts")).replace(":", "")}')
            self.docs.append({'stem': stem, 'title': nice, 'kind': 'image' if r['kind'] == 'image' else 'file',
                              'ts': r.get('ts', ''), 'summary': out, 'row': r, 'fields': key_fields(out),
                              'file': self._media_for(r, title)})
            self.doc_of[r.get('ts', '')] = stem
        seen = {d['title'] for d in self.docs} | {str(d.get('ref') or '') for d in self.docs}
        for doc in self.d.nlm_docs:
            title = clip(doc.get('title') or doc.get('ref') or 'קישור', 80)
            if title in seen or str(doc.get('ref') or '') in seen:
                continue
            kind = doc.get('kind') or 'url'
            title, stem = self._doc_title(title, kind, doc.get('added', ''))
            self.docs.append({'stem': stem, 'title': title, 'kind': kind,
                              'ts': doc.get('added', ''), 'summary': '', 'row': None, 'fields': {},
                              'ref': doc.get('ref') if kind == 'url' else Path(str(doc.get('ref', ''))).name, 'file': None})

    def write_docs(self) -> None:
        brain = self.vault / '🧠 המוח' / '📎 מסמכים'
        rows = []
        for doc in sorted(self.docs, key=lambda x: x['ts'], reverse=True):
            f = doc['fields']
            day = _day(doc['ts'])
            file_rel = ''
            if doc.get('file') and doc['file'].stat().st_size <= MAX_FILE:
                name = safe_name(doc.get('file_name') or doc['file'].name.split('__', 1)[-1])
                name = name if Path(name).suffix.lower() == doc['file'].suffix.lower() else name + doc['file'].suffix
                file_rel = f'{AREA}/{D_DOCS}/קבצים/{name}'
                self.w.copy(file_rel, doc['file'])
            body = [f'# 📎 {doc["title"]}', '',
                    f'**מתי:** {dmy(doc["ts"])} · **יום:** {link(day) if day else "—"}',
                    '**מקור:** ' + ('המשתמש שלח בוואטסאפ' if doc['row'] else 'קישור / קובץ ששלח (במחברת NotebookLM של המסמכים)')]
            if doc.get('ref'):
                body.append(f'**קישור:** {doc["ref"]}' if str(doc['ref']).startswith('http') else f'**קובץ:** {clip(doc["ref"], 80)}')
            if file_rel:
                body.append(f'**הקובץ:** ![[{Path(file_rel).name}]]' if doc['kind'] == 'image'
                            else f'**הקובץ:** [[{Path(file_rel).name}]]')
            if f:
                body += ['', '## 🔑 פרטים'] + [f'- **{k}:** {", ".join(v) if isinstance(v, list) else v}' for k, v in
                                                 (('מספר הזמנה', f.get('booking')), ('תאריכים', f.get('dates')),
                                                  ('סכום', f.get('amount')), ('מקום', f.get('place'))) if v]
            if doc['summary']:
                body += ['', '## 📝 מה העוזר הבין מזה', block(doc['summary'])]
            old = sorted(brain.glob(f'{day}*.md')) if day and brain.is_dir() else []
            if old:
                body += ['', 'ראה גם: ' + ' · '.join(link(p.stem) for p in old)]
            body += ['', f'↩️ {link(D_DOCS)} · {link(HOME)}']
            fm = self.front('document', D_DOCS, title=doc['title'], date=day, kind=doc['kind'],
                            booking=f.get('booking'), dates=f.get('dates'), amount=f.get('amount'),
                            place=f.get('place'), file=Path(file_rel).name if file_rel else None, tags=['מסמך'])
            self.w.note(self.rel(D_DOCS, doc['stem']), fm, '\n'.join(body))
            if day:
                self.days[day]['docs'].append(doc['stem'])
            rows.append(f'| {dmy(day)} | {link(doc["stem"], doc["title"])} | {f.get("booking", "")} | '
                        f'{", ".join(f.get("dates", [])[:2])} |')
        body = ['# 📁 מסמכים', '', 'כל מסמך, תמונה או קישור שהמשתמש שלח לעוזר: פרטים, סיכום, והקובץ עצמו כשיש.', '',
                '| מתי | מה | מס׳ הזמנה | תאריכים |', '|---|---|---|---|'] + (rows or ['| — | | | |']) + [
                '', '```dataview', 'TABLE date AS "תאריך", booking AS "הזמנה", amount AS "סכום", place AS "מקום"',
                f'FROM "Smart Study Agent/{AREA}/{D_DOCS}"', 'WHERE type = "document"', 'SORT date DESC', '```',
                '', f'↩️ {link(HOME)}']
        self.w.note(self.rel(D_DOCS, D_DOCS), self.front('moc', D_DOCS, title='מסמכים'), '\n'.join(body))

    # ── open items / done log / week ──
    def _reminders(self) -> list[dict]:
        rs = [r for r in self.d.life.get('reminders') or [] if self.d.mine(r)]
        return sorted(rs, key=lambda r: r.get('due', ''))

    def _open_tasks(self) -> list[dict]:
        return sorted([t for t in self.d.tasks if t.get('status') == 'open' and self.d.mine(t)], key=lambda t: t.get('due', ''))

    def _obl_open(self) -> list[dict]:
        return sorted([r for r in self.d.obl.values() if _obl_status(r, self.now) in ('open', 'late')],
                      key=lambda r: (r.get('due') or '9999', r.get('title', '')))

    def _course(self, ck: str) -> str:
        tgt, name = self.courses.get(ck, ('', ck))
        return link(tgt, name) if tgt else name

    def _obl_line(self, r: dict, done: bool = False) -> str:
        kind = {'quiz': 'בוחן', 'exam_reg': 'רישום'}.get(r.get('kind'), 'מטלה')
        st = _obl_status(r, self.now)
        when = ('עבר המועד, אפשר באיחור עד ' + dmy(r.get('cutoff'))) if st == 'late' else (
            ('עד ' + dmy(r['due'])) if r.get('due') else 'בלי תאריך')
        extra = f' · ציון {r["grade"]}' if r.get('grade') else ''
        return (f'- [{"x" if done else " "}] {clip(r.get("title"), 120)} ({kind}) — '
                + (f'נסגר {dmy(r.get("closed"))}' if done and r.get('closed') else when) + extra)

    def _ck_of(self, name: str) -> str:
        """The university's course name ("הסתברות וסטטיסטיקה (לדו-חוגי)", "מבוא להנדסת תוכנה") → the course key."""
        from config.paths import COURSE_ALT_NAMES
        norm = lambda t: re.sub(r'\s+', ' ', str(t or '').replace('׳', "'").replace('"', '').replace('״', '')).strip()
        n = norm(name)
        if not n:
            return ''
        for ck, (_, short) in self.courses.items():
            for alias in [short] + list(COURSE_ALT_NAMES.get(ck, [])):
                a = norm(alias)
                if a and (n.startswith(a) or a in n or n in a):
                    return ck
        return ''

    def _override(self, day: str, ck: str, kind_he: str, start: str) -> dict | None:
        kind = {'שיעור': 'lecture', 'תרגיל': 'tutorial'}.get(kind_he, '')
        for o in self.d.overrides:
            if (str(o.get('date', ''))[:10] == day and o.get('ck') == ck and (not kind or o.get('kind') == kind)
                    and (not o.get('start') or not start or o['start'] == start[:5])):
                return o
        return None

    def _override_text(self, o: dict | None) -> str:
        if not o:
            return ''
        try:
            from agent import schedule
            return ' ' + clip(schedule.suffix(o), 220)
        except Exception:
            return f' — {clip(o.get("status"), 20)}: {clip(o.get("note"), 160)}'

    def write_open(self) -> None:
        rem = [r for r in self._reminders() if r.get('active')]
        tasks = self._open_tasks()
        coord = [c for c in self.d.coord.values() if self.d.mine(c, 'owner') and c.get('status') in ('active', 'open', 'waiting')]
        threads = [i for i in self.d.now_note.get(self.d.owner, []) if i.get('kind') in ('thread', 'request')
                   and self.now.timestamp() - float(i.get('at') or 0) < 7 * 86400] if self.d.owner else []
        qs = []
        for o in (self.d.options.get(self.d.owner) or []) if self.d.owner else []:
            if not o.get('replied') and self.now.timestamp() - float(o.get('ts') or 0) < 48 * 3600 and o.get('q'):
                qs.append(clip(o['q'], 120))
        obl = self._obl_open()
        body = ['# 📋 מה פתוח עכשיו', '', 'נכתב מחדש בכל לילה מהזיכרון של העוזר. מה שנסגר עובר ל-' + link(DONE_NOTE)
                + ' ולא חוזר לכאן. *לסגור משהו: לכתוב לעוזר "עשיתי את…" בוואטסאפ.*', '']
        sec = [('⏰ תזכורות', [f'- [ ] {clip(r.get("text"), 160)} — {dmy(r.get("due"))}'
                              + (' · חוזר' if r.get('repeat') not in (None, '', 'none') else '') for r in rem]),
               ('📌 משימות', [f'- [ ] {clip(t.get("text"), 160)} — עד {dmy(t.get("due"))}'
                             + (f' · מ{clip(t.get("src"), 40)}' if t.get('src') else '') for t in tasks]),
               ('🎓 חובות בקורסים', [self._obl_line(r) + f' · {self._course(r.get("course", ""))}' for r in obl]),
               ('🤝 תיאומים פתוחים', [f'- [ ] {link(safe_name(c.get("name", "")))}: {clip(c.get("goal"), 140)}' for c in coord]),
               ('🧵 חוטים פתוחים השבוע', [f'- {clip(i.get("text"), 180)}' for i in threads]),
               ('❓ שאלות שלא ענית עליהן', [f'- {q}' for q in qs])]
        for title, lines in sec:
            if lines:
                body += [f'## {title}'] + lines + ['']
        if not any(lines for _, lines in sec):
            body += ['אין כרגע שום דבר פתוח 🎉', '']
        body += ['```dataview', 'TASK', f'FROM "Smart Study Agent/{AREA}"', 'WHERE !completed', 'GROUP BY file.link', '```',
                 '', f'📅 {link(WEEK_NOTE)} · ✅ {link(DONE_NOTE)} · ↩️ {link(HOME)}']
        n = len(rem) + len(tasks) + len(obl) + len(coord)
        self.w.note(self.rel(D_TASKS, OPEN_NOTE), self.front('open_items', D_TASKS, title='פתוח עכשיו', status='open' if n else 'clear',
                                                              open_count=n, tags=['פתוח']), '\n'.join(body))

    def write_done(self) -> None:
        done = []
        for t in self.d.tasks:
            if t.get('status') != 'open' and self.d.mine(t):
                done.append((t.get('closed') or t.get('created') or '', f'📌 {clip(t.get("text"), 160)}'
                             + (' (לא רלוונטי)' if t.get('status') == 'dropped' else '')))
        for c in self.d.closed:
            if self.d.mine(c):
                done.append((c.get('ts', ''), f'💬 {clip(c.get("text"), 160)} (סגרת במילים)'))
        for r in self._reminders():
            if not r.get('active'):
                done.append((r.get('due', ''), f'⏰ {clip(r.get("text"), 160)}'))
        for c in self.d.coord.values():
            if self.d.mine(c, 'owner') and c.get('status') not in ('active', 'open', 'waiting'):
                done.append((c.get('created', ''), f'🤝 תיאום עם {link(safe_name(c.get("name", "")))}: {clip(c.get("goal"), 120)}'))
        done.sort(key=lambda x: x[0], reverse=True)
        by_month: dict[str, list] = defaultdict(list)
        for ts, line in done:
            by_month[str(ts)[:7] or 'לא ידוע'].append(f'- [x] {dmy(ts)} — {line}' if ts else f'- [x] {line}')
        body = ['# ✅ נסגרו', '', 'כל מה שנסגר: משימות, תזכורות, דברים שאמרת שעשית. מה שכאן לא חוזר לרשימת הפתוחים.', '']
        for month in sorted(by_month, reverse=True):
            body += [f'## {month}'] + by_month[month] + ['']
        body += [f'↩️ {link(OPEN_NOTE)} · {link(HOME)}']
        self.w.note(self.rel(D_TASKS, DONE_NOTE), self.front('done_log', D_TASKS, title='נסגרו', count=len(done), tags=['נסגר']),
                    '\n'.join(body))

    def _agenda(self, isos: set[str]) -> dict[str, list]:
        """day → [(time, line)]: the university's sessions, schedule changes, tasks, assignments, reminders and calendar
        events of those days (the week note and, 17/11, every daily note)."""
        per: dict[str, list] = defaultdict(list)
        used = set()
        for e in self.d.tau:
            if e.get('date', '')[:10] in isos:
                mark = '📝 *מבחן* ' if e.get('is_exam') else ''
                ck = self._ck_of(e.get('course', ''))
                where = ' '.join(x for x in (e.get('building', ''), e.get('room', '')) if x)
                o = self._override(e['date'][:10], ck, e.get('kind', ''), e.get('time', ''))
                if o:
                    used.add(id(o))
                per[e['date'][:10]].append((e.get('time', ''), f'- {e.get("time", "")}' + (f'–{e["end_time"]}' if e.get('end_time') else '')
                                            + f' {mark}{e.get("kind", "")} · {self._course(ck) if ck else clip(e.get("course"), 60)}'
                                            + (f' · {clip(where, 50)}' if where else '') + self._override_text(o)))
        for o in self.d.overrides:                      # a change whose session the cached timetable doesn't show
            if str(o.get('date', ''))[:10] in isos and id(o) not in used:
                per[str(o['date'])[:10]].append((o.get('start', ''), f'- {o.get("start", "")} {KIND_HE.get(o.get("kind"), "")} · '
                                                 f'{self._course(o.get("ck", ""))}' + self._override_text(o)))
        for t in self._open_tasks():
            if _day(t.get('due')) in isos:
                per[_day(t.get('due'))].append(('', f'- [ ] 📌 {clip(t.get("text"), 140)}'))
        for r in self._obl_open():
            if r.get('due') and _day(r['due']) in isos:
                per[_day(r['due'])].append((_hm(r['due']), self._obl_line(r) + f' · {self._course(r.get("course", ""))}'))
        for r in self._reminders():
            if r.get('active') and _day(r.get('due')) in isos:
                per[_day(r.get('due'))].append((_hm(r.get('due')), f'- [ ] ⏰ {_hm(r.get("due"))} {clip(r.get("text"), 140)}'))
        seen = set()
        for a in self.d.owner_actions() + self.d.partner_calendar():
            for ev in ([a['event']] if isinstance(a.get('event'), dict) else []) + [e for e in a.get('events') or [] if isinstance(e, dict)]:
                key = (ev.get('title'), str(ev.get('start'))[:16])
                if key in seen:
                    continue
                seen.add(key)
                if _day(ev.get('start')) in isos:
                    per[_day(ev.get('start'))].append((_hm(ev.get('start')), f'- 📅 {(_hm(ev.get("start")) + " ") if _hm(ev.get("start")) else ""}{clip(ev.get("title"), 120)}'
                                                       + (' *(הצעה של בת הזוג)*' if a.get('action') == 'partner_proposal' else '')))
        return per

    def write_week(self) -> None:
        d0 = self.now.date()
        start = d0 - timedelta(days=(d0.weekday() + 1) % 7)            # Sunday
        days = [start + timedelta(days=i) for i in range(7)]
        per = self._agenda({x.isoformat() for x in days})
        body = [f'# 📅 השבוע — {start:%d/%m} עד {days[-1]:%d/%m}', '',
                'מהמערכת באוניברסיטה, ממה שהעוזר הוסיף ליומן, ממשימות, תזכורות והגשות. היומן המלא: Google Calendar.', '']
        for x in days:
            iso = x.isoformat()
            has = iso in self.days and any(self.days[iso][k] for k in ('asked', 'did', 'decided', 'shared', 'docs'))
            body.append(f'## יום {WEEKDAY[x.weekday()]} {x:%d/%m}' + (f' · {link(iso)}' if has else '')
                        + (' ⬅️ היום' if iso == self.today else ''))
            items = [ln for _, ln in sorted(per.get(iso, []), key=lambda p: p[0] or '99')]
            body += (items or ['- —']) + ['']
        body += [f'שבוע {link(week_of(d0))} · {link(OPEN_NOTE)} · ↩️ {link(HOME)}']
        self.w.note(self.rel(D_TASKS, WEEK_NOTE), self.front('week', D_TASKS, title='השבוע', week=week_of(d0),
                                                              date=start.isoformat(), tags=['שבוע']), '\n'.join(body))

    # ── studies ──
    def write_study(self) -> None:
        open_ = self._obl_open()
        closed = sorted([r for r in self.d.obl.values() if _obl_status(r, self.now) == 'closed'],
                        key=lambda r: str(r.get('closed') or r.get('due') or ''), reverse=True)
        missed = [r for r in self.d.obl.values() if _obl_status(r, self.now) == 'missed']
        body = ['# 📌 חובות לפי קורס', '', 'בחנים, מטלות ושיעורי בית מהמודל ומאתרי המרצים. נכתב מחדש בלילה; מה שהוגש עובר ל-'
                + link(OBL_DONE) + '.', '']
        for ck, (tgt, name) in self.courses.items():
            mine = [r for r in open_ if r.get('course') == ck]
            body += [f'## {link(tgt, name)}'] + ([self._obl_line(r) for r in mine] or ['- אין חובות פתוחות ✅']) + ['']
        if missed:
            body += ['## ⌛ פספסת (המועד והאיחור עברו)'] + [self._obl_line(r) + f' · {self._course(r.get("course", ""))}'
                                                         for r in missed] + ['']
        body += [f'↩️ {link(D_STUDY)} · {link(OPEN_NOTE)}']
        self.w.note(self.rel(D_STUDY, OBL_NOTE), self.front('obligations', D_STUDY, title='חובות לפי קורס',
                                                             open_count=len(open_), status='open' if open_ else 'clear',
                                                             tags=['חובות']), '\n'.join(body))
        body = ['# ✅ נסגרו — לימודים', ''] + [self._obl_line(r, done=True) + f' · {self._course(r.get("course", ""))}'
                                               for r in closed] + ['', f'↩️ {link(OBL_NOTE)}']
        self.w.note(self.rel(D_STUDY, OBL_DONE), self.front('done_log', D_STUDY, title='נסגרו — לימודים', count=len(closed)),
                    '\n'.join(body))
        exams = sorted([e for e in self.d.tau if e.get('is_exam') and e.get('date', '') >= self.today], key=lambda e: e['date'])
        body = ['# 🎓 לימודים', '', 'התחום הראשי היום. החומר עצמו (סיכומים, תמלולים, מבחנים) נמצא בתיקיות הקורסים; כאן מה שהעוזר עוקב אחריו.', '',
                '## הקורסים']
        for ck, (tgt, name) in self.courses.items():
            n = sum(1 for r in open_ if r.get('course') == ck)
            body.append(f'- {link(tgt, name)}' + (f' · ⏳ {n} פתוחות' if n else ''))
        changes = sorted([o for o in self.d.overrides if str(o.get('date', ''))[:10] >= self.today], key=lambda o: o.get('date', ''))
        if changes:
            body += ['', '## 🔀 שינויים במערכת (מהמודל / מהמייל)'] + [
                f'- {dmy(o.get("date"))} {o.get("start", "")} {KIND_HE.get(o.get("kind"), "")} · {self._course(o.get("ck", ""))}'
                + self._override_text(o) for o in changes]
        if exams:
            body += ['', '## 📝 מבחנים קרובים'] + [f'- {dmy(e["date"])} {e.get("time", "")} — {clip(e.get("course"), 60)}' for e in exams[:10]]
        body += ['', f'- {link(OBL_NOTE)}', f'- {link(OBL_DONE)}', '- [[📅 מטלות]] · [[🏠 בית]]', '', f'↩️ {link(HOME)}']
        self.w.note(self.rel(D_STUDY, D_STUDY), self.front('moc', D_STUDY, title='לימודים'), '\n'.join(body))

    # ── 17/11 links: what he told the bot → the people, courses, projects and day it touches (code only, no model) ──
    def _entities(self) -> list[tuple[re.Pattern, str]]:
        """[(pattern, link)] for every name a memory can mention. Built once per run."""
        if getattr(self, '_ents', None) is not None:
            return self._ents
        ents: list[tuple[re.Pattern, str]] = []

        def rx(word: str) -> re.Pattern:
            return re.compile(r'(?<![\w\u0590-\u05FF])[ובלמשהכ]?' + re.escape(word) + r'(?![\w\u0590-\u05FF])', re.I)
        for name in sorted(self.people, key=len, reverse=True):
            first = name.split()[0] if ' ' in name else ''
            ents.append((rx(name), link(safe_name(name))))
            if len(first) >= 3:
                ents.append((rx(first), link(safe_name(name), name)))
        try:
            from config.paths import COURSE_ALT_NAMES
        except Exception:
            COURSE_ALT_NAMES = {}
        for ck, (tgt, name) in self.courses.items():
            for alias in [name] + list(COURSE_ALT_NAMES.get(ck, [])):
                if len(str(alias)) >= 4:
                    ents.append((rx(str(alias)), link(tgt, name)))
        proj = self.vault / '🧠 המוח' / '🗣️ שיחות עם קלוד'
        for p in sorted(proj.glob('*.md')) if proj.is_dir() else []:
            if p.stem != 'כללי' and len(p.stem) >= 4:
                ents.append((rx(p.stem), link(f'🧠 המוח/🗣️ שיחות עם קלוד/{p.stem}', p.stem)))
                if '_' in p.stem:
                    ents.append((rx(p.stem.replace('_', ' ')), link(f'🧠 המוח/🗣️ שיחות עם קלוד/{p.stem}', p.stem)))
        self._ents = ents
        return ents

    def links_in(self, text: str) -> list[str]:
        out = []
        for pat, lk in self._entities():
            if pat.search(text or '') and lk not in out:
                out.append(lk)
        return out[:6]

    def collect_memories(self) -> None:
        """"תזכור ש…" facts and the decisions / corrections of the week → linked lines: the day they were said, the people,
        courses and projects they name. Each person they name gets them on the person's note too."""
        self.memories: list[dict] = []
        if not self.d.owner:
            return
        at = ((self.d.life.get('memory_at') or {}).get(self.d.owner) or {})
        rows = self.d.owner_rows()
        for f in (self.d.life.get('memory') or {}).get(self.d.owner, []):
            day = _day(at.get(f))
            if not day:                       # saved before 17/11: the first message of his that carries it
                key = re.sub(r'\s+', ' ', f)[:25]
                day = next((_day(r.get('ts')) for r in rows if key and key in re.sub(r'\s+', ' ', str(r.get('in', '')))), '')
            self.memories.append({'text': f, 'day': day, 'kind': 'fact'})
        for i in self.d.now_note.get(self.d.owner, []):
            if i.get('kind') in ('decision', 'correction'):
                self.memories.append({'text': i.get('text', ''), 'kind': i['kind'],
                                      'day': datetime.fromtimestamp(float(i.get('at') or 0)).date().isoformat()})
        for m in self.memories:
            m['links'] = self.links_in(m['text'])
            if m['kind'] == 'fact' and m['day']:
                self.days[m['day']]['remember'].append(self._memory_line(m, day=False))
            for name, p in self.people.items():
                if link(safe_name(name)) in m['links'] or link(safe_name(name), name) in m['links']:
                    p['told'].append(m)

    def _memory_line(self, m: dict, day: bool = True) -> str:
        label = {'decision': 'החלטה: ', 'correction': 'תיקון: '}.get(m['kind'], '')
        tail = [lk for lk in m.get('links') or []] + ([link(m['day'])] if day and m.get('day') else [])
        return f'- {label}{clip(m["text"], 240)}' + (' · ' + ' · '.join(tail) if tail else '')

    # ── 17/11 the lesson notes of a day (summary + transcript), as path links: a summary and its transcript share a name ──
    def lesson_notes(self) -> dict[str, list[tuple[str, str]]]:
        """date → [(course key, line)] from the course folders' "YYYY-MM-DD - …" notes."""
        if getattr(self, '_lessons', None) is not None:
            return self._lessons
        from config.paths import VAULT_COURSE_FOLDER_TO_KEY
        out: dict[str, list] = defaultdict(list)
        for folder, ck in VAULT_COURSE_FOLDER_TO_KEY.items():
            d = self.vault / folder
            if not d.is_dir():
                continue
            trans = {p.stem for p in (d / '🎙️ תמלולים').glob('*.md')} if (d / '🎙️ תמלולים').is_dir() else set()
            for p in sorted(d.glob('*.md')):
                m = re.match(r'(\d{4}-\d{2}-\d{2}) - (.+)$', p.stem)
                if not m:
                    continue
                line = f'{link(f"{folder}/{p.stem}", "📝 " + m.group(2))}'
                if p.stem in trans:
                    line += f' · {link(f"{folder}/🎙️ תמלולים/{p.stem}", "🎙️ תמלול")}'
                out[m.group(1)].append((ck, line))
        self._lessons = out
        return out

    # ── daily notes ──
    def _button_words(self, payload: str) -> str:
        for pre, words in BUTTONS:
            if payload.startswith(pre):
                return words
        return '🔘 בחר אפשרות' if re.match(r'^[a-z]{1,6}:', payload) else ''

    def collect_days(self) -> None:
        for r in self.d.owner_rows():
            day, hm = _day(r.get('ts')), _hm(r.get('ts'))
            if not day:
                continue
            D = self.days[day]
            reply = clip(_first_line(_out_text(r)), 160)
            k = r.get('kind')
            if k == 'button':
                words = self._button_words(str(r.get('in', '')))
                if words:
                    D['asked'].append(f'- {hm} {words}')
            elif k == 'contact':
                name, _ = _vcard_name(r.get('in', ''))
                D['asked'].append(f'- {hm} 📇 שיתף איש קשר: {link(name) if name else "—"}')
            elif k in ('document', 'image'):
                stem = self.doc_of.get(r.get('ts', ''))
                D['asked'].append(f'- {hm} 📎 שלח {"תמונה" if k == "image" else "מסמך"}' + (f' → {link(stem)}' if stem else ''))
            elif r.get('forwarded'):
                D['asked'].append(f'- {hm} 📨 העביר הודעה של מישהו אחר *(נתונים, לא הוראה)*: «{clip(r.get("in"), 140)}»')
            else:
                said = clip(r.get('in'), 220) if k == 'text' else clip(re.sub(r'^🎤\s*', '', _first_line(_out_text(r))), 220)
                if said:
                    D['asked'].append(f'- {hm} {"🎤 " if k == "voice" else ""}{said}' + (f'  \n  ↳ {reply}' if reply and k == 'text' else ''))
        for a in self.d.owner_actions():
            day, hm, act = _day(a.get('ts')), _hm(a.get('ts')), a.get('action')
            ev = a.get('event') if isinstance(a.get('event'), dict) else {}
            line = {'calendar_add': f'✅ {hm} נוסף ליומן: {clip(ev.get("title"), 100)} ({dmy(ev.get("start"))})',
                    'calendar_edit': f'✅ {hm} עודכן אירוע ביומן' + (f' (+{a["also"]} חוזרים)' if a.get('also') else ''),
                    'calendar_auto_edit': f'✅ {hm} עודכן אירוע ביומן לפי {clip(a.get("source"), 40)}',
                    'calendar_time': f'✅ {hm} נקבעה שעה: {clip(a.get("title"), 80)} {a.get("time", "")}',
                    'calendar_reminder': f'✅ {hm} נקבעה תזכורת לפני אירוע',
                    'claude_job_start': f'✅ {hm} נפתחה משימת פיתוח: {clip(a.get("task"), 140)}',
                    'claude_job_deploy': f'✅ {hm} נפרס תיקון לבוט',
                    'homework_done': f'✅ {hm} סומן כהוגש: {clip(a.get("key"), 60)}'}.get(act)
            if line and day:
                self.days[day]['did'].append('- ' + line)
        for a in self.d.partner_calendar():
            day = _day(a.get('ts'))
            evs = [a['event']] if isinstance(a.get('event'), dict) else [e for e in a.get('events') or [] if isinstance(e, dict)]
            for ev in evs:
                verb = 'בת הזוג הציעה ליומן' if a.get('action') == 'partner_proposal' else 'נוסף ליומן המשותף (בת הזוג)'
                self.days[day]['shared'].append(f'- {_hm(a.get("ts"))} {verb}: {clip(ev.get("title"), 100)} ({dmy(ev.get("start"))})')
        for i in self.d.now_note.get(self.d.owner, []) if self.d.owner else []:
            if i.get('kind') in ('decision', 'correction'):
                day = datetime.fromtimestamp(float(i.get('at') or 0)).date().isoformat()
                self.days[day]['decided'].append(f'- {"החלטה" if i["kind"] == "decision" else "תיקון"}: {clip(i.get("text"), 200)}')
        for c in self.d.closed:
            if self.d.mine(c) and _day(c.get('ts')):
                self.days[_day(c['ts'])]['decided'].append(f'- סגרת: {clip(c.get("text"), 160)}')

    def _day_set(self) -> set[str]:
        """The days that get a note: any day something happened in the chat, today (the home note links to it), and, 17/11,
        every day of this semester up to today that had a session or a lesson note."""
        days = {d for d in self.days if re.fullmatch(r'\d{4}-\d{2}-\d{2}', d)}
        days.add(self.today)
        sess = sorted(e['date'][:10] for e in self.d.tau if e.get('date') and not e.get('is_exam'))
        try:                                     # the timetable cache holds only what's ahead: the semester's own start
            from collectors.course_files import SEMESTER_START
            lo = str(SEMESTER_START)[:10]
        except Exception:
            lo = ''
        lo = min([x for x in (lo, sess[0] if sess else '') if x] or [''])
        days |= {d for d in sess if d <= self.today}
        if lo:
            days |= {d for d in self.lesson_notes() if lo <= d <= self.today}
        return days

    def write_days(self) -> None:
        stems = sorted(self._day_set())
        agenda = self._agenda(set(stems))
        lessons = self.lesson_notes()
        open_by_day: dict[str, list] = defaultdict(list)
        for t in self._open_tasks():
            open_by_day[_day(t.get('created'))].append(f'- [ ] 📌 {clip(t.get("text"), 140)} — עד {dmy(t.get("due"))}')
        for c in self.d.coord.values():
            if self.d.mine(c, 'owner') and c.get('status') in ('active', 'open', 'waiting'):
                open_by_day[_day(c.get('created'))].append(f'- [ ] 🤝 תיאום עם {link(safe_name(c.get("name", "")))}')
        for r in self._obl_open():
            open_by_day[_day(r.get('first_seen'))].append(self._obl_line(r))
        for i, day in enumerate(stems):
            D = self.days[day]
            d = date.fromisoformat(day)
            nav = []
            if i:
                nav.append(f'⬅️ {link(stems[i - 1])}')
            if i + 1 < len(stems):
                nav.append(f'{link(stems[i + 1])} ➡️')
            body = [f'# 📓 יום {WEEKDAY[d.weekday()]} {d:%d/%m/%Y}', '',
                    ' · '.join(nav + [f'שבוע {link(week_of(d))}', link(HOME)]), '']
            items = [ln for _, ln in sorted(agenda.get(day, []), key=lambda p: p[0] or '99')]
            if items:
                body += ['## 🗓️ סדר היום'] + items + ['']
            if lessons.get(day):
                by_ck: dict[str, list] = defaultdict(list)
                for ck, ln in lessons[day]:
                    by_ck[ck].append(ln)
                body += ['## 🎓 סיכומי השיעורים'] + [f'- {self._course(ck)}: ' + ' · '.join(lns) for ck, lns in by_ck.items()] + ['']
            if D['remember']:
                body += ['## 🧠 ביקשת שאזכור'] + D['remember'] + ['']
            if D['asked']:
                body += ['## 💬 מה ביקשת'] + D['asked'] + ['']
            if D['did']:
                body += ['## ✅ מה העוזר עשה'] + sorted(D['did']) + ['']
            if D['decided']:
                body += ['## 🧭 החלטות ומה נסגר'] + D['decided'] + ['']
            if D['shared']:
                body += ['## 💞 יומן משותף'] + D['shared'] + ['']
            if D['docs']:
                body += ['## 📎 מסמכים'] + [f'- {link(s)}' for s in D['docs']] + ['']
            if D['people']:
                body += ['## 👥 אנשים', ' · '.join(link(safe_name(p)) for p in sorted(D['people'])), '']
            if open_by_day.get(day):
                body += ['## ⏳ נשאר פתוח מהיום הזה'] + open_by_day[day] + ['']
            if day == self.today:
                body += [f'📋 כל מה שפתוח עכשיו: {link(OPEN_NOTE)}']
            fm = self.front('daily', D_DAY, date=day, week=week_of(d), asked=len(D['asked']), done=len(D['did']),
                            people=sorted(D['people']) or None, status='open' if open_by_day.get(day) else 'done',
                            courses=sorted({ck for ck, _ in lessons.get(day, [])}) or None,
                            tags=['יומן'] + sorted({ck for ck, _ in lessons.get(day, [])}))
            self.w.note(self.rel(D_DAY, day), fm, '\n'.join(body))

    # ── about him / the bot ──
    def write_you(self) -> None:
        facts = (self.d.life.get('memory') or {}).get(self.d.owner, []) if self.d.owner else []
        decided = [i for i in self.d.now_note.get(self.d.owner, []) if i.get('kind') in ('decision', 'correction')] if self.d.owner else []
        facts_m = [m for m in self.memories if m['kind'] == 'fact']
        dec_m = [m for m in self.memories if m['kind'] != 'fact']
        body = ['# 🧭 דברים שלמדתי עליך', '', 'מה שאמרת לעוזר לזכור ("תזכור ש…"), ומה שאישרת ב"🧠 למדתי עליך היום". '
                'כל שורה מקושרת ליום שבו נאמרה ולאנשים, לקורסים ולפרויקטים שהיא מזכירה. '
                'למחוק: "תשכח ש…" בוואטסאפ.', '', '## מה אני זוכר'] + (
            [self._memory_line(m) for m in facts_m] if facts_m else [f'- {clip(f, 240)}' for f in facts] or ['- (עדיין כלום)'])
        if decided:
            body += ['', '## החלטות ותיקונים מהשבוע האחרון'] + (
                [self._memory_line(m) for m in dec_m] if dec_m else
                [f'- {datetime.fromtimestamp(float(i.get("at") or 0)):%d/%m} {clip(i.get("text"), 200)}' for i in decided])
        body += ['', f'🔇 {link("🔇 השתקות")} · ↩️ {link(HOME)}']
        self.w.note(self.rel(D_YOU, '🧭 דברים שלמדתי עליך'), self.front('preferences', D_YOU, title='דברים שלמדתי עליך',
                                                                         count=len(facts)), '\n'.join(body))
        mutes = []
        kinds = ((self.d.kinds.get(self.d.owner) or {}).get('kinds') or {}) if self.d.owner else {}
        for k in kinds.values():
            if float(k.get('muted_until') or 0) > self.now.timestamp():
                mutes.append(f'- {clip(k.get("q") or "סוג שאלה", 100)} — עד {datetime.fromtimestamp(float(k["muted_until"])):%d/%m}')
        body = ['# 🔇 השתקות', '', 'סוגי שאלות שהעוזר הפסיק לשאול (התעלמת פעמיים, או ביקשת "פחות"). '
                'להחזיר: "תחזיר את …" בוואטסאפ.', ''] + (mutes or ['- אין השתקות פעילות']) + ['', f'↩️ {link(HOME)}']
        self.w.note(self.rel(D_YOU, '🔇 השתקות'), self.front('mutes', D_YOU, title='השתקות', count=len(mutes)), '\n'.join(body))

    def write_bot(self, weekly_text=None) -> None:
        items = [b for b in self.d.backlog if b.get('status') != 'done']
        body = ['# 🧠 שיפור עצמי', '', 'מה הבוט מצא על עצמו בסקירות הלילה (agent/improve), ומה מחכה להחלטה.', '']
        for b in items:
            body += [f'### {clip(b.get("title"), 120)}', f'- [ ] {clip(b.get("change"), 300)}',
                     f'  - ראיה: {clip(b.get("evidence"), 300)}', f'  - נראה {b.get("seen", 1)} פעמים, {dmy(b.get("first"))} – {dmy(b.get("last"))}', '']
        qs = self.d.improve.get('questions') or []
        if qs:
            body += ['## ❓ שאלות לדוח השבועי'] + [f'- {dmy(q.get("ts"))} {clip(q.get("text"), 200)}' for q in qs[-10:]] + ['']
        if not items and not qs:
            body.append('- אין ממצאים פתוחים')
        body += ['', f'↩️ {link(HOME)}']
        self.w.note(self.rel(D_BOT, '🧠 שיפור עצמי'), self.front('improve', D_BOT, title='שיפור עצמי', open_count=len(items)), '\n'.join(body))
        s = self.d.security
        summ = s.get('summary') or {}
        bad = [c for c in s.get('checks') or [] if c.get('status') not in ('pass', 'ok')]
        body = ['# 🛡️ אבטחה', '', f'בדיקת האבטחה הלילית ({dmy(s.get("ts"))}): ✅ {summ.get("pass", 0)} · ⚠️ {summ.get("warn", 0)} · '
                f'❌ {summ.get("fail", 0)}', ''] + [f'- {"❌" if c.get("status") == "fail" else "⚠️"} {clip(c.get("id"), 60)}: '
                                                   f'{clip(c.get("reason"), 160)}' for c in bad] + ['', f'↩️ {link(HOME)}']
        self.w.note(self.rel(D_BOT, '🛡️ אבטחה'), self.front('security', D_BOT, title='אבטחה', date=_day(s.get('ts')),
                                                           status='fail' if summ.get('fail') else 'warn' if summ.get('warn') else 'pass'),
                    '\n'.join(body))
        # the weekly report: one note per week it was sent, written once (the report reads the last 7 days at send time)
        # only the latest sent week, within 7 days of sending: an older one can't be rebuilt as it was
        for week in sorted(self.d.weekly_sent)[-1:]:
            rel = self.rel(f'{D_BOT}/📊 דוח שבועי', week)
            sent = _dt(self.d.weekly_sent[week])
            if (self.vault / rel).exists() or weekly_text is None or not sent or self.now - sent > timedelta(days=7):
                continue
            try:
                txt = weekly_text()
            except Exception as e:
                print(f'  ⚠️ weekly report: {e!r}'[:200])
                continue
            self.w.note(rel, self.front('weekly_report', D_BOT, week=week, date=_day(self.d.weekly_sent[week])),
                        f'# 📊 הבוט בשבוע {week}\n\n' + block(txt, 6000) + f'\n\n↩️ {link(HOME)}')

    def write_home(self) -> None:
        n_open = len([r for r in self._reminders() if r.get('active')]) + len(self._open_tasks()) + len(self._obl_open())
        body = ['# 👩‍💼 העוזר — המוח השני', '',
                'כל מה שהעוזר בוואטסאפ יודע ועושה, כהערות מקושרות. נכתב מחדש בלילה (רק מה שהשתנה).',
                '**לא לערוך כאן** — מה שנכתב כאן יוחלף. ההערות שלך: בתיקייה [[✍️ שלי/Inbox|✍️ שלי]] (העוזר קורא אותן, לא כותב בהן). '
                'שורה כמו `- [ ] תזכיר לי …` או `לעוזר: …` שם תגיע אליך בוואטסאפ לאישור ✅ לפני שמשהו קורה.', '',
                f'## היום: {link(self.today)}', f'- 📋 {link(OPEN_NOTE)} ({n_open} פתוחים)', f'- 📅 {link(WEEK_NOTE)}', f'- ✅ {link(DONE_NOTE)}', '',
                '## תחומים',
                f'- 🎓 {link(D_STUDY)} · {link(OBL_NOTE)}',
                f'- 👥 {link(D_PEOPLE)} ({len(self.people)})',
                f'- 📁 {link(D_DOCS)} ({len(self.docs)})',
                f'- 🧭 {link("🧭 דברים שלמדתי עליך")} · {link("🔇 השתקות")}',
                f'- 🤖 {link("🧠 שיפור עצמי")} · {link("🛡️ אבטחה")}',
                '- 🧠 [[עליי]] · [[🗣️ שיחות עם קלוד]] (תיקיית 🧠 המוח)', '',
                '## 🗺️ כל הכספת', '- [[🗺️ מפת המוח]]: הקורסים, השבועות, ההודעות, המטלות, המבחנים והמושגים', '',
                '## ימים אחרונים',
                '```dataview', 'TABLE asked AS "בקשות", done AS "פעולות", status AS "מצב"',
                f'FROM "Smart Study Agent/{AREA}/{D_DAY}"', 'SORT date DESC', 'LIMIT 10', '```', '',
                '> 🔒 הערות עם `private: true` (היומן, הבוט, ✍️ שלי) לא עולות ל-NotebookLM. הכספת כולה מגובה ב-Drive ונמשכת למחשב.']
        self.w.note(f'{AREA}/{HOME}.md', self.front('moc', '', title='העוזר', date=self.today, tags=['MOC']), '\n'.join(body))


def export(vault_dir: Path | None = None, data_dir: Path | None = None, out_root: Path | None = None,
           owner: str | None = None, partner: str | None = None, now: datetime | None = None, weekly_text=None) -> dict:
    """The whole assistant area. → {'new', 'changed', 'same', …, 'folders': {folder: n}, 'written': […]}."""
    from config.paths import VAULT_DIR
    vault = Path(vault_dir or VAULT_DIR)
    if owner is None or partner is None:
        from whatsapp import outbox
        owner = outbox.owner_chat() if owner is None else owner
        partner = outbox.partner_chat() if partner is None else partner
    data = Data(Path(data_dir or ROOT / 'data'), owner=owner or '', partner=partner or '', now=now)
    w = Writer(vault, out_root=out_root)
    ex = Exporter(data, w, vault)
    for step in (ex.collect_people, ex.collect_docs, ex.collect_days, ex.collect_memories, ex.write_people, ex.write_docs, ex.write_open,
                 ex.write_done, ex.write_week, ex.write_study, ex.write_days, ex.write_you,
                 lambda: ex.write_bot(weekly_text), ex.write_home):
        try:
            step()
        except Exception as e:                      # one broken store never stops the rest
            print(f'  ⚠️ {getattr(step, "__name__", "step")}: {e!r}'[:300])
    return {**dict(w.stats), 'folders': dict(w.by_folder), 'written': w.written, 'people': len(ex.people), 'docs': len(ex.docs)}


# ── NotebookLM: which assistant notes join the brain notebook (agent/brain.uploadable) ──

NOTEBOOK_NOTES = [f'{HOME}.md', f'{D_TASKS}/{OPEN_NOTE}.md', f'{D_TASKS}/{WEEK_NOTE}.md', f'{D_STUDY}/{OBL_NOTE}.md',
                  f'{D_PEOPLE}/{D_PEOPLE}.md', f'{D_DOCS}/{D_DOCS}.md', f'{D_YOU}/🧭 דברים שלמדתי עליך.md']


def for_notebook(brain_root: Path, files: list[Path]) -> list[Path]:
    """The brain notebook's list + the assistant's index notes, minus every private note."""
    area = Path(brain_root).parent / AREA
    extra = [area / n for n in NOTEBOOK_NOTES if (area / n).exists()]
    out, seen = [], set()
    for p in list(files) + extra:
        if p.name in seen or privacy.is_private(p):
            continue
        seen.add(p.name)
        out.append(p)
    return out

