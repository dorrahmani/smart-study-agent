"""
agent/brain.py — one brain: the Obsidian vault holds what the bot knows; NotebookLM answers from it (no API cost).

The owner 15/10: "ה-ONE BRAIN זה האובסידיאן שלנו" + "שיהיה על ה-NotebookLM ו-Gemini ונחסוך כסף".
The vault (VM = source of truth, pulled nightly to the PC) gets a folder "🧠 המוח":
    עליי.md                    — what the bot remembers about the owner (agent/life facts)
    📎 מסמכים/<date> <name>.md — every link / file he sent, with its NotebookLM summary
    📧 מיילים/<YYYY-MM>.md     — the university mail and Moodle items the triage kept (not the 'low' ones)
Course material is already in the vault (summaries, transcripts, exams) and in the course notebooks.

NotebookLM side: the WhatsApp documents notebook (agent/nlm_docs) IS the brain notebook — documents are sources
there already; sync() (nightly, scripts/brain_sync.py) adds עליי.md and the last months of mail as text sources,
replacing a source only when its note changed. "מה היה ב…" / "מה אתה יודע על…" questions are asked there — free.
No LLM calls here. Every write is best-effort: the brain must never break a reply.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / 'data' / 'brain_state.json'
MAIL_MONTHS = 4                                            # how many months of mail live in NotebookLM


def brain_dir() -> Path:
    from config.paths import VAULT_DIR
    return VAULT_DIR / '🧠 המוח'


def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|#\[\]]+', ' ', name or '').strip()[:80] or 'מסמך'


def _front(meta: dict) -> str:
    return '---\n' + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + '---\n\n'


# ── writing into the vault ──────────────────────────────────────────────────

def note_doc(name: str, kind: str, ref: str, summary: str, when: datetime | None = None, root: Path | None = None) -> Path:
    """A link / file the owner sent → its own note with the summary (the source itself stays in NotebookLM)."""
    when = when or datetime.now()
    d = (root or brain_dir()) / '📎 מסמכים'
    d.mkdir(parents=True, exist_ok=True)
    p = d / f'{when:%Y-%m-%d} {_safe(name)}.md'
    src = ref if kind == 'url' else Path(ref).name
    p.write_text(_front({'type': 'document', 'title': name, 'source': src, 'sent': when.isoformat(timespec='minutes'),
                         'tags': ['מוח', 'מסמך']})
                 + f'# 📎 {name}\n\n' + (f'מקור: {src}\n\n' if src else '') + (summary or '').strip() + '\n',
                 encoding='utf-8')
    return p


def note_mail(rows: list[dict], when: datetime | None = None, root: Path | None = None) -> int:
    """Triaged university mail / Moodle items (not 'low') → appended to this month's note. → how many written."""
    from integrations import tau_mail
    when = when or datetime.now()
    keep = [r for r in rows if r.get('level') != 'low' and (r.get('summary') or r.get('subject'))]
    if not keep:
        return 0
    d = (root or brain_dir()) / '📧 מיילים'
    d.mkdir(parents=True, exist_ok=True)
    p = d / f'{when:%Y-%m}.md'
    head = '' if p.exists() else (_front({'type': 'mail', 'month': f'{when:%Y-%m}', 'tags': ['מוח', 'מייל']})
                                  + f'# 📧 מיילים ומודל — {when:%m/%Y}\n')
    out = []
    for r in keep:
        who = r.get('from', '') if r.get('source') == 'moodle' else tau_mail.sender_name(r.get('from', ''))
        block = f"\n## {when:%d/%m} · {who} — {r.get('subject', '')}\n{(r.get('summary') or '').strip()}\n"
        if r.get('action'):
            block += f"👉 {r['action']}\n"
        if r.get('lesson_change'):
            block += f"🏫 שינוי בשיעור: {r['lesson_change']}\n"
        if r.get('attachments'):
            block += f"📎 {', '.join(r['attachments'][:5])}\n"
        out.append(block)
    with open(p, 'a', encoding='utf-8') as f:
        f.write(head + ''.join(out))
    return len(out)


_DAYS = ['שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת', 'ראשון']


def _profile() -> str:
    """What the system already knows without being told: this semester's courses + weekly slots (the university site),
    and the people the bot can message (names only — never numbers)."""
    out = []
    try:
        from config.paths import ACTIVE_COURSES, COURSE_NAMES
        out.append('## הסמסטר\n' + '\n'.join(f'- {COURSE_NAMES.get(k, k)}' for k in ACTIVE_COURSES))
    except Exception:
        pass
    try:
        from datetime import date as _d
        from integrations import tau_portal
        events = tau_portal.schedule(live=False)[0] or []
        slots = sorted({(_d.fromisoformat(e['date']).weekday(), e['time'], e['kind'], e['course'],
                         ' '.join(x for x in (e.get('building', ''), e.get('room', '')) if x))
                        for e in events if not e.get('is_exam') and e.get('time') and e.get('kind') in ('שיעור', 'תרגיל')},
                       key=lambda s: ((s[0] + 1) % 7, s[1]))
        if slots:
            out.append('## מערכת שעות שבועית (מאתר האוניברסיטה)\n' + '\n'.join(
                f'- יום {_DAYS[w]} {t} — {k} ב{c}' + (f' · {r}' if r else '') for w, t, k, c, r in slots))
        today = _d.today().isoformat()
        exams = [e for e in events if e.get('is_exam') and e.get('date', '') >= today]
        if exams:
            out.append('## מבחנים\n' + '\n'.join(f"- {e['date']} {e['time']} — {e['course']}" for e in exams[:12]))
    except Exception:
        pass
    try:
        from agent import contacts
        names = sorted(contacts._load())
        if names:
            out.append('## אנשים שהבוט מכיר\n' + '\n'.join(f'- {n}' for n in names[:40]))
    except Exception:
        pass
    return '\n\n'.join(out) + ('\n\n' if out else '')


def write_facts(chat: str | None = None, root: Path | None = None) -> Path | None:
    """עליי.md — the owner's facts (his own memory only; the partner's stays out of the shared vault)."""
    from agent import life
    if chat is None:
        from whatsapp import outbox
        chat = outbox.owner_chat()
    if not chat:
        return None
    facts = life.facts(chat)
    d = root or brain_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / 'עליי.md'
    body = (_front({'type': 'facts', 'updated': datetime.now().strftime('%Y-%m-%d'), 'tags': ['מוח']})
            + '# 🧠 מה העוזר יודע על המשתמש\n\n'
            + 'המשתמש — סטודנט באוניברסיטת תל אביב. בת הזוג: בת הזוג.\n\n'
            + _profile()
            + '## מה המשתמש סיפר לבוט\n'
            + 'נשמר כשאומרים לבוט "תזכור ש…" (או כשזה עולה בשיחה) ונמחק ב"תשכח ש…".\n\n'
            + ('\n'.join(f'- {f}' for f in facts) or '- (עדיין כלום)') + '\n')
    old = p.read_text(encoding='utf-8') if p.exists() else ''
    if re.sub(r'updated: .*', '', old) != re.sub(r'updated: .*', '', body):   # don't touch it just for the date
        p.write_text(body, encoding='utf-8')
    return p


# ── NotebookLM: the same notes as sources of the brain notebook ─────────────

def _load_state(path: Path | None = None) -> dict:
    try:
        return json.loads((path or STATE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'sources': {}}


def _save_state(st: dict, path: Path | None = None) -> None:
    p = path or STATE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding='utf-8')


def uploadable(root: Path | None = None, now: datetime | None = None) -> list[Path]:
    """עליי.md + the last MAIL_MONTHS months of mail (documents are NotebookLM sources already)."""
    r = root or brain_dir()
    now = now or datetime.now()
    months = set()
    y, m = now.year, now.month
    for _ in range(MAIL_MONTHS):
        months.add(f'{y:04d}-{m:02d}')
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    out = [r / 'עליי.md'] if (r / 'עליי.md').exists() else []
    out += [r / '🗂️ הפרויקטים שלי.md'] if (r / '🗂️ הפרויקטים שלי.md').exists() else []   # from the PC (17/10)
    if (r / '🗣️ שיחות עם קלוד').exists():                 # 03/11 his Claude Code sessions, a note per project
        out += [r / '🗣️ שיחות עם קלוד.md'] if (r / '🗣️ שיחות עם קלוד.md').exists() else []
        out += sorted((r / '🗣️ שיחות עם קלוד').glob('*.md'))
    out += sorted(p for p in (r / '📧 מיילים').glob('*.md') if p.stem in months) if (r / '📧 מיילים').exists() else []
    try:                                    # 16/11 + the assistant's index notes (👩‍💼 העוזר), minus every `private` note
        from vault_sync import assistant_export
        out = assistant_export.for_notebook(r, out)
    except Exception as e:
        print(f'  ⚠️ assistant notes: {e!r}'[:200])
    return out


def plan_sync(files: list[Path], st: dict) -> tuple[list[Path], list[str]]:
    """→ (notes to (re)upload, stale source ids to delete). Pure — the tests use it."""
    want = {p.name: hashlib.sha1(p.read_bytes()).hexdigest() for p in files}
    upload = [p for p in files if (st['sources'].get(p.name) or {}).get('hash') != want[p.name]]
    drop = [v['sid'] for k, v in st['sources'].items()
            if v.get('sid') and (k not in want or v.get('hash') != want[k])]
    return upload, drop


def sync(root: Path | None = None, state_path: Path | None = None) -> dict:
    """Nightly: vault notes → the brain notebook. Only changed notes cost a call. → {'uploaded', 'deleted'}."""
    from agent import nlm_docs
    write_facts(root=root)
    st = _load_state(state_path)
    files = uploadable(root)
    upload, drop = plan_sync(files, st)
    if not upload and not drop:
        return {'uploaded': 0, 'deleted': 0}
    nb = nlm_docs._notebook()

    async def go():
        async with await nlm_docs._client() as c:
            if not st.get('renamed'):
                try:
                    await c.notebooks.rename(nb, '🧠 המוח של המשתמש — מסמכים, מיילים, עליי')
                    st['renamed'] = True
                except Exception as e:
                    print(f'  ⚠️ rename failed: {e!r}'[:200])
            for sid in drop:
                try:
                    await c.sources.delete(nb, sid)
                except Exception as e:
                    print(f'  ⚠️ delete {sid} failed: {e!r}'[:200])
            done = {}
            for p in upload:
                s = await c.sources.add_text(nb, f'🧠 {p.stem}', p.read_text(encoding='utf-8'), wait=True, wait_timeout=150)
                done[p.name] = {'sid': getattr(s, 'source_id', None) or getattr(s, 'id', None),
                                'hash': hashlib.sha1(p.read_bytes()).hexdigest()}
            return done
    done = nlm_docs._run(go())
    keep = {p.name for p in files}
    st['sources'] = {k: v for k, v in st['sources'].items() if k in keep and v.get('sid') not in drop}
    st['sources'].update(done)
    st['synced'] = datetime.now().isoformat(timespec='seconds')
    _save_state(st, state_path)
    return {'uploaded': len(done), 'deleted': len(drop)}


# "מה היה עם המלגה?" (not "מה אתה יודע על X" — that's usually course material), "תזכיר לי מה אמרו על…" → the brain notebook
ASK_RX = re.compile(r'(?:מה (?:היה|קרה) עם ה|תזכיר(?:י)? לי מה (?:אמר|כתב|היה|סיכמנו)|'
                    r'מה (?:סיכמנו|אמרתי לך) על|מה (?:כתבו|אמרו) (?:לי )?(?:במייל|במודל|על)|'
                    r'(?:במייל|במודל) (?:ש|מ)(?:לפני|לפנ|שבוע|חודש)|'
                    r'(?:ה)?פרויקט(?:ים)? (?:שלי|ה?אחרון)|באיזה פרויקט|מה (?:המצב|עשיתי|עבדתי) (?:ב|על )(?:ה)?פרויקט|'
                    r'\b(?:my-shop|my-tool)\b|'
                    # 03/11 his Claude Code sessions: "מה עשינו בקלוד על האתר?", "מה סיכמתי עם קלוד", "מה נשאר פתוח"
                    r'(?:עשינו|עשיתי|סיכמנו|סיכמתי|דיברנו|דיברתי|בנינו|בניתי|תיקנו|נשאר)[^?]{0,25}(?:ב|עם |של )?קלוד|'
                    r'(?:ב|עם |של )קלוד(?:\s*קוד)?\s+(?:על|לגבי|ב)|מה נשאר (?:פתוח|לעשות) (?:ב|על )?(?:ה)?פרויקט)', re.I)


def has_sources() -> bool:
    from agent import nlm_docs
    return bool(nlm_docs._load().get('docs')) or bool(_load_state()['sources'])
