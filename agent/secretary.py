"""
agent/secretary.py — the owner sends a link, a file or a photo; the agent reads it like a personal secretary.

  • a link (web page / PDF) or a file (PDF, Word, Excel, PowerPoint, text) or a photo of a document
  • with a question ("מה הדדליין פה?") → answers it; without → a short summary + what needs doing
  • follow-up questions about the same thing for 30 minutes ("ומה צריך להגיש שם?"); after that agent/my_files finds it
  • dates in it (deadline, meeting, event) → offered for the calendar, through the usual ✅ flow

Answers come ONLY from the content (The owner 06/10: "אם לא יודע אל תמציא") — if it isn't there, it says so.
Links are fetched by the server with an SSRF guard (no localhost / private / tailnet addresses), size and time
limits. Gemini only (gemini-3.8-flash, low thinking) — one call per question.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import socket
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

from config.paths import DATA_DIR
from utils.gemini_sub import api_only

MODEL = 'gemini-3.8-flash'
DOCS = DATA_DIR / 'agent_docs'
MAX_BYTES = 15 * 1024 * 1024
FOLLOWUP_SECONDS = 30 * 60
URL_RX = re.compile(r'https?://[^\s<>"\'״]+', re.I)
TEXT_EXT = {'.txt', '.csv', '.md', '.json', '.html', '.htm', '.xml', '.py', '.java', '.c', '.cpp', '.js', '.ts', '.yaml', '.yml'}
FILE_EXT = TEXT_EXT | {'.pdf', '.docx', '.xlsx', '.pptx', '.jpg', '.jpeg', '.png', '.webp'}
FOLLOWUP_RX = re.compile(r'בקובץ|במסמך|בלינק|בקישור|בכתבה|במאמר|בדף|באתר הזה|בעמוד|בהודעה ששלחתי|בתמונה|'
                         r'שם\?|^\s*ו?מה (?:עוד )?(?:כתוב|רשום|צריך|הדדליין|התאריך|המחיר|השעה)|לפי זה|מתוך זה|'
                         r'תסכם (?:את )?(?:זה|אותו|אותה)')


# ── fetching links safely ──────────────────────────────────────────────────

class Blocked(ValueError):
    """Never retried another way (internal address, not http)."""


class Unreadable(ValueError):
    """The server couldn't read it (403, login, JavaScript page) — Gemini may still read it from Google's side."""


UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
      'Chrome/130.0 Safari/537.36')

def _safe_host(host: str) -> bool:
    """Public internet only — the bot must never be a way into localhost, the dashboard or the tailnet."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split('%')[0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
                or ip.is_unspecified or ip in ipaddress.ip_network('100.64.0.0/10')):
            return False
    return bool(infos)


def fetch(url: str, timeout: int = 15) -> tuple[bytes, str, str]:
    """(body, content-type, final url). Raises ValueError with a Hebrew reason."""
    import requests
    for _ in range(5):
        u = urlparse(url)
        if u.scheme not in ('http', 'https') or not u.hostname:
            raise Blocked('זה לא קישור רגיל (http/https).')
        if not _safe_host(u.hostname):
            raise Blocked('אני לא ניגש לכתובות פנימיות או לא קיימות.')
        r = requests.get(url, timeout=timeout, stream=True, allow_redirects=False,
                         headers={'User-Agent': UA, 'Accept-Language': 'he,en'})
        if r.is_redirect or r.status_code in (301, 302, 303, 307, 308):
            url = urljoin(url, r.headers.get('Location', ''))
            continue
        if r.status_code >= 400:
            raise Unreadable(f'האתר החזיר שגיאה ({r.status_code}) — אולי הדף דורש התחברות.')
        body = b''
        for chunk in r.iter_content(65536):
            body += chunk
            if len(body) > MAX_BYTES:
                raise ValueError('הקובץ בקישור גדול מדי (מעל 15MB).')
        return body, (r.headers.get('Content-Type') or '').split(';')[0].strip().lower(), url
    raise ValueError('יותר מדי הפניות בקישור.')


def _html_text(body: bytes) -> tuple[str, str]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(body, 'html.parser')
    for t in soup(['script', 'style', 'noscript', 'svg', 'nav', 'footer', 'form']):
        t.decompose()
    title = (soup.title.string or '').strip() if soup.title else ''
    text = re.sub(r'\n\s*\n+', '\n\n', soup.get_text('\n'))
    return title, re.sub(r'[ \t]+', ' ', text).strip()


# ── files → something Gemini can read ──────────────────────────────────────

def _zip_xml_text(path: Path, prefix: str) -> str:
    """Plain text of docx/pptx/xlsx without extra libraries (their XML, tags stripped, in order)."""
    out = []
    with zipfile.ZipFile(path) as z:
        names = sorted(n for n in z.namelist() if n.startswith(prefix) and n.endswith('.xml'))
        names.sort(key=lambda n: [int(x) if x.isdigit() else x for x in re.split(r'(\d+)', n)])
        for n in names:
            xml = z.read(n).decode('utf-8', 'ignore')
            xml = re.sub(r'</w:p>|</a:p>|</row>', '\n', xml)
            xml = re.sub(r'</c>|</si>', '\t', xml)
            out.append(re.sub(r'<[^>]+>', '', xml))
    return re.sub(r'\n\s*\n+', '\n', '\n'.join(out)).strip()


def _parts(path: Path):
    """→ (list of genai parts, short description) for a saved file."""
    from google.genai import types as T
    ext = path.suffix.lower()
    data = path.read_bytes()
    if ext == '.pdf':
        return [T.Part.from_bytes(data=data, mime_type='application/pdf')], 'PDF'
    if ext in ('.jpg', '.jpeg', '.png', '.webp'):
        mime = {'.png': 'image/png', '.webp': 'image/webp'}.get(ext, 'image/jpeg')
        return [T.Part.from_bytes(data=data, mime_type=mime)], 'תמונה'
    if ext == '.docx':
        return [_zip_xml_text(path, 'word/document')[:150000]], 'מסמך Word'
    if ext == '.pptx':
        return [_zip_xml_text(path, 'ppt/slides/slide')[:150000]], 'מצגת'
    if ext == '.xlsx':
        return [_zip_xml_text(path, 'xl/sharedStrings')[:60000] + '\n' + _zip_xml_text(path, 'xl/worksheets/')[:90000]], 'גיליון'
    if ext in ('.html', '.htm'):
        title, text = _html_text(data)
        return [f'{title}\n\n{text}'[:150000]], 'דף'
    return [data.decode('utf-8', 'ignore')[:150000]], 'קובץ טקסט'


def save_link(url: str) -> tuple[Path, str]:
    """Fetch a link into data/agent_docs (so follow-ups don't refetch) → (path, title)."""
    body, ctype, final = fetch(url)
    DOCS.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha1(final.encode()).hexdigest()[:12]
    if 'pdf' in ctype or final.lower().endswith('.pdf') or body[:5] == b'%PDF-':
        p, title = DOCS / f'{h}.pdf', Path(urlparse(final).path).name or 'PDF'
        p.write_bytes(body)
    elif ctype.startswith('image/'):
        ext = {'image/png': '.png', 'image/webp': '.webp'}.get(ctype, '.jpg')
        p, title = DOCS / f'{h}{ext}', 'תמונה'
        p.write_bytes(body)
    else:
        title, text = _html_text(body) if ('html' in ctype or b'<html' in body[:2000].lower()) else ('', body.decode('utf-8', 'ignore'))
        if len(text) < 200:
            raise Unreadable('הדף כמעט ריק בשבילי — כנראה הוא נבנה ב-JavaScript או דורש התחברות. '
                             'אפשר לשלוח לי צילום מסך או PDF שלו.')
        p = DOCS / f'{h}.txt'
        p.write_text(f'{title}\n{final}\n\n{text}', encoding='utf-8')
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p, title or urlparse(final).hostname or 'הקישור'


# ── asking ─────────────────────────────────────────────────────────────────

PROMPT = """אתה המזכיר/ה האישי/ת של {who}. היום {now}.
המשתמש שלח {what}. {task}

כללים:
- ענה רק ממה שכתוב בתוכן. אם התשובה לא מופיעה בו — כתוב במפורש "זה לא כתוב שם" ואל תנחש.
- עברית, קצר ומעשי, לוואטסאפ: *מודגש* לכותרות, • לרשימות, בלי LaTeX ובלי טבלאות.
- אם זה תרגיל — הסבר איך ניגשים אליו צעד-צעד (מותר להסביר, זו לא "המצאה").

החזר JSON בלבד:
{{"answer": "התשובה למשתמש",
  "dated": "כל דבר בתוכן עם תאריך שרלוונטי למשתמש לשים ביומן (דדליין, פגישה, אירוע, תור) — שורה לכל אחד, עם כותרת, תאריך מלא ושעה אם יש; ריק אם אין",
  "keys": "מה ששווה לשמור כדי למצוא את זה אחר כך: מספרי הזמנה/אסמכתא/לקוח, שמות של עסקים, מקומות ואנשים, טלפונים, כתובות, סכומים, תאריכים ומועדי ביטול — שורה לכל אחד בצורה 'מה: ערך', רק מה שכתוב בתוכן; ריק אם אין. בלי מספרי כרטיס אשראי, תעודת זהות או סיסמאות"}}"""


@api_only()                                             # a document or a page is someone else's words
def ask(path: Path, question: str = '', what: str = '', client=None) -> dict:
    """→ {'answer': str, 'dated': str}. One Gemini call."""
    import google.genai as genai
    from google.genai import types as T
    client = client or genai.Client(api_key=os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY'))
    parts, desc = _parts(path)
    task = (f'השאלה שלו: "{question}"' if question.strip() else
            'אין שאלה — תן סיכום קצר (עד 6 נקודות) ואחריו *מה צריך לעשות* (אם יש: הגשות, תשלומים, תאריכים, אישורים).')
    from agent import domains                          # 16/11 who he is comes from his life domains (student today)
    prompt = PROMPT.format(now=datetime.now().strftime('%d/%m/%Y %H:%M'), what=what or desc, task=task,
                           who=domains.identity('המשתמש'))
    resp = client.models.generate_content(
        model=MODEL, contents=parts + [prompt],
        config=T.GenerateContentConfig(temperature=0.2, max_output_tokens=4000, response_mime_type='application/json',
                                       thinking_config=T.ThinkingConfig(thinking_level='low')))
    from utils.usage_tracker import log_gemini
    log_gemini(resp, MODEL, op='agent.secretary')
    try:
        j = json.loads(resp.text or '{}')
    except ValueError:
        j = {'answer': (resp.text or '').strip(), 'dated': ''}
    keys = j.get('keys') or ''
    if isinstance(keys, list):
        keys = '\n'.join(str(k) for k in keys)
    return {'answer': (j.get('answer') or '').strip() or 'לא הצלחתי לקרוא את זה.', 'dated': (j.get('dated') or '').strip(),
            'keys': _no_secrets(str(keys).strip())}


_CARD = re.compile(r'(?<!\d)(?:\d[ -]?){13,19}(?!\d)')                 # a card number, never kept
_SECRET_LINE = re.compile(r'אשראי|כרטיס|תעודת זהות|ת\.ז|סיסמ|cvv|password', re.I)


def _no_secrets(keys: str) -> str:
    """What gets kept for later search: no card numbers, IDs or passwords even if the model wrote them."""
    return '\n'.join(ln for ln in keys.splitlines() if ln.strip() and not _SECRET_LINE.search(ln) and not _CARD.search(ln))


@api_only()
def ask_url(url: str, question: str = '', client=None) -> str:
    """Fallback when the server can't read a page: Gemini fetches it itself (url_context — works on
    gemini-2.5-flash; 3.8-flash returns 500 for it). Raises Unreadable if Google couldn't retrieve it either."""
    import google.genai as genai
    from google.genai import types as T
    client = client or genai.Client(api_key=os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY'))
    task = (f'השאלה של המשתמש: "{question}"' if question.strip() else
            'אין שאלה — תן סיכום קצר (עד 6 נקודות) ואחריו *מה צריך לעשות* אם יש.')
    prompt = (f'היום {datetime.now():%d/%m/%Y}. קרא את הדף {url} . {task}\n'
              'ענה רק ממה שכתוב בדף; אם התשובה לא שם — כתוב "זה לא כתוב שם" ואל תנחש. '
              'עברית, קצר, לוואטסאפ (*מודגש*, •), בלי LaTeX.')
    resp = client.models.generate_content(model='gemini-2.5-flash', contents=prompt,
                                          config=T.GenerateContentConfig(tools=[T.Tool(url_context=T.UrlContext())],
                                                                         temperature=0.2, max_output_tokens=3000))
    from utils.usage_tracker import log_gemini
    log_gemini(resp, 'gemini-2.5-flash', op='agent.secretary_url')
    try:
        meta = resp.candidates[0].url_context_metadata.url_metadata or []
    except (AttributeError, IndexError, TypeError):
        meta = []
    if not any('SUCCESS' in str(m.url_retrieval_status) for m in meta):
        raise Unreadable('גם דרך גוגל לא הצלחתי לפתוח את הדף — כנראה צריך התחברות. אפשר לשלוח צילום מסך או PDF.')
    return (resp.text or '').strip() or 'לא הצלחתי לקרוא את הדף.'


def remember(u: dict, path: Path | None, name: str, url: str = '', answer: str = '', keys: str = '') -> None:
    """The last thing read (follow-ups for 30 minutes) — and, with the answer, what agent/my_files keeps for good."""
    u['doc'] = {'path': str(path) if path else '', 'url': url, 'name': name,
                'ts': datetime.now().isoformat(timespec='seconds'), 'answer': (answer or '')[:4000], 'keys': keys or ''}


def current(u: dict) -> dict | None:
    d = u.get('doc')
    if not d:
        return None
    if (datetime.now() - datetime.fromisoformat(d['ts'])).total_seconds() > FOLLOWUP_SECONDS:
        return None
    if not d.get('url') and not (d.get('path') and Path(d['path']).exists()):
        return None
    return d


def is_followup(u: dict, text: str, reply_to: str | None = None) -> bool:
    """An explicit reference ('בקובץ', 'בלינק', 'שם?') or a WhatsApp reply, within 30 min of the last doc —
    a bare 'מתי…?' is NOT pulled in (it may well be about the calendar)."""
    return bool(current(u) and (FOLLOWUP_RX.search(text) or reply_to))


def file_ok(path: Path) -> str | None:
    """None if readable, else a Hebrew reason."""
    if path.suffix.lower() not in FILE_EXT:
        return f'את סוג הקובץ הזה ({path.suffix or "בלי סיומת"}) אני עוד לא יודע לקרוא. PDF / Word / Excel / PowerPoint / טקסט / תמונה — כן.'
    if path.stat().st_size == 0:
        return 'הקובץ ריק או גדול מדי (מעל 15MB) — לא הורדתי אותו.'
    if path.stat().st_size > MAX_BYTES:
        return 'הקובץ גדול מדי בשבילי (מעל 15MB).'
    return None
