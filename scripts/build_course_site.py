"""
scripts/build_course_site.py — a private study site, one page per course, rebuilt every night (29/10).

The owner: "תעשה לי אתר לכל קורס שמלמד את הקורסים שלי ומתעדכן" + "שים לב שיעורי בית שהגשתי ואלו שלא".
Only what the pipeline already made — no LLM, no new data:
    lessons       every lecture / tutorial summary of the semester, in order, with prev / next
    homework      Moodle + lecturer-site exercises: ✅ submitted · ⏳ open (days left) · ❌ missed
    exam topics   the course's exam map (data/exam_topics) — weight, taught yet, practiced
    exam bank     the real past exam questions, each with its solution folded under it
    practice      the weekly practice sets, solutions folded
Served on the tailnet only (tailscale serve --set-path /learn → data/site), never public:
    https://your-server.your-tailnet.ts.net/learn/
Built into data/site.tmp, then swapped in, so a half-built site is never served. A course not rebuilt this time (--course,
or its build failed) keeps the pages it has, and the top page still lists it from its card.json.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import shutil
import sys
import threading
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / 'data' / 'site'
URL = 'https://your-server.your-tailnet.ts.net/learn/'

# ── markdown → html (math kept for KaTeX) ──────────────────────────────────

USD = '<span class="usd">$</span>'
MATH_RX = re.compile(r'(\$\$.+?\$\$|\\\[.+?\\\]|\\\(.+?\\\)|(?<![\\$])\$(?!\s)[^$\n]+?(?<!\s)\$)', re.S)


# 07/11 audit A12: markdown passes raw HTML through, and the summaries are model text over other people's words (a script in
# a lecture's transcript, a mail in the brain) on a page of the owner's. nh3 would need an install on the server and in the deploy
# gate's venv; this needs none, and it can't be talked around: nothing of the input is copied, the page is written anew from
# the list below, so what the browser reads is only these tags, these attributes, and escaped text.
SAFE_TAGS = {'p', 'br', 'hr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'li', 'dl', 'dt', 'dd', 'strong', 'b', 'em', 'i', 'u',
             's', 'del', 'ins', 'sub', 'sup', 'small', 'mark', 'kbd', 'abbr', 'code', 'pre', 'blockquote', 'div', 'span', 'center',
             'table', 'thead', 'tbody', 'tfoot', 'tr', 'th', 'td', 'caption', 'details', 'summary', 'a', 'img'}
SAFE_VOID = {'br', 'hr', 'img'}
SAFE_DROP = {'script', 'style', 'iframe', 'object', 'embed', 'applet', 'template', 'svg', 'math', 'noscript', 'noembed', 'noframes',
             'textarea', 'select', 'title', 'xmp', 'plaintext', 'head', 'frameset'}     # with all that's inside them
SAFE_ATTRS = {'a': {'href', 'title'}, 'img': {'src', 'alt', 'title'}, 'td': {'colspan', 'rowspan'}, 'th': {'colspan', 'rowspan'},
              'ol': {'start'}, 'abbr': {'title'}, 'code': {'class'}, 'span': {'class'}}
SAFE_URL = re.compile(r'(?i)^(?:https?://|mailto:|#|/(?![/\\])|\.{1,2}/|[^:/?#\\]+(?:[/?#]|$))')      # a browser reads \ as /
SAFE_IMG = re.compile(r'(?i)^(?:/(?![/\\])|\.{1,2}/|[^:/?#\\]+(?:[/?#]|$))')     # our own files: a remote one tells its host who read


class _Clean(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.open: list[str] = []
        self.drop: list[str] = []

    def handle_starttag(self, tag, attrs):
        if self.drop or tag in SAFE_DROP:
            if tag not in SAFE_VOID:
                self.drop.append(tag)
            return
        if tag not in SAFE_TAGS:
            return                                             # the tag goes, its text stays
        keep = []
        for k, v in attrs:
            v = re.sub(r'[\x00-\x1f\x7f]', '', v or '').strip() if k in ('href', 'src') else (v or '')
            if k not in SAFE_ATTRS.get(tag, ()):
                continue
            if (k == 'href' and not SAFE_URL.match(v)) or (k == 'src' and not SAFE_IMG.match(v)) \
                    or (k == 'class' and not re.fullmatch(r'language-[\w+-]+|usd', v)) \
                    or (k in ('colspan', 'rowspan', 'start') and not re.fullmatch(r'\d{1,3}', v)):
                continue
            keep.append(f' {k}="{html.escape(v, quote=True)}"')
        if tag == 'img' and not any(a.startswith(' src=') for a in keep):
            return
        self.out.append(f'<{tag}{"".join(keep)}>')
        if tag not in SAFE_VOID:
            self.open.append(tag)

    def handle_endtag(self, tag):
        if self.drop:
            if self.drop[-1] == tag:
                self.drop.pop()
            elif tag in self.drop:
                del self.drop[self.drop.index(tag):]
            return
        if tag in self.open:                                   # a </div> can't close the page around it
            while self.open:
                t = self.open.pop()
                self.out.append(f'</{t}>')
                if t == tag:
                    break

    def handle_data(self, data):
        if not self.drop:
            self.out.append(html.escape(data, quote=False))

    def result(self) -> str:
        self.close()
        return ''.join(self.out + [f'</{t}>' for t in reversed(self.open)])


def safe_html(fragment: str) -> str:
    """The allowlist above, on a fragment. Safe twice over: running it on its own output changes nothing."""
    c = _Clean()
    c.feed(fragment or '')
    return c.result()


def md_html(text: str) -> str:
    """Markdown → HTML. The math is taken out first (markdown would eat its _ and *) and put back for KaTeX."""
    import markdown
    text = re.sub(r'<!--.*?-->', '', text or '', flags=re.S)
    text = re.sub(r'\[\[([^\]|]+)\|([^\]]+)\]\]', r'\2', text)          # obsidian links → their text
    text = re.sub(r'\[\[([^\]]+)\]\]', r'\1', text)
    keep: list[str] = []

    def stash(m):
        keep.append(m.group(0))
        return f'MATHX{len(keep) - 1}X'
    text = MATH_RX.sub(stash, text)
    # a $ left unpaired is money ("18,000$"), not math — in its own element KaTeX can't pair it with a later $
    text = re.sub(r'(?<!\\)\$', USD, text)
    # the summaries put tables / lists right under a "**כותרת:**" line; python-markdown needs a blank line there
    lines, prev = [], ''
    for ln in text.split('\n'):
        kind = 't' if ln.lstrip().startswith('|') else 'l' if re.match(r'\s*(?:[-*+]|\d+\.)\s', ln) else ''
        pkind = 't' if prev.lstrip().startswith('|') else 'l' if re.match(r'\s*(?:[-*+]|\d+\.)\s', prev) else ''
        if kind and prev.strip() and kind != pkind and not (kind == 'l' and prev.startswith((' ', '\t'))):
            lines.append('')
        lines.append(ln)
        prev = ln
    text = '\n'.join(lines)
    out = markdown.markdown(text, extensions=['tables', 'fenced_code', 'sane_lists'])
    out = re.sub(r' style="text-align: (?:left|right|center);"', '', out)     # ":---" means nothing in an RTL table
    return safe_html(re.sub(r'MATHX(\d+)X', lambda m: html.escape(keep[int(m.group(1))], quote=False), out))


# 04/11 the owner: "עיצוב חדשני ושהלימודים יהיה נוח ללמוד דרך זה". The look and behaviour live in scripts/site_assets/ (one
# cached file instead of 30 KB in every page). W3Schools' flow stays (03/11): a track of pages, next / previous, chapters
# on the side. New: a hue per course, a route of stops with what's done, Ctrl+K over all the material, a focus mode with
# a clock, flash cards, "ידעתי / לחזור" on every answer, reading settings, resuming where you stopped.
ASSETS = Path(__file__).resolve().parent / 'site_assets'
HUES = {'probability': 275, 'software_1': 165, 'microeconomics_2': 55, 'macroeconomics': 350}
FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
         '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Hebrew:wght@400;500;600&'
         'family=Rubik:wght@500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap">')
# before anything paints, so a chosen theme never flashes
PREFS_JS = ('<script>try{var p=JSON.parse(localStorage.getItem("ssa-prefs"))||{};if(p.theme&&p.theme!=="auto")'
            'document.documentElement.dataset.theme=p.theme;if(p.size)document.documentElement.dataset.size=p.size}catch(e){}</script>')
KATEX_CDN = 'https://cdnjs.cloudflare.com/ajax/libs/KaTeX/0.16.9/'
KATEX = (f'<link rel="stylesheet" href="{KATEX_CDN}katex.min.css">'
         f'<script defer src="{KATEX_CDN}katex.min.js"></script>'
         f'<script defer src="{KATEX_CDN}contrib/auto-render.min.js"></script>')      # app.js, after them, renders the math
_prefs_hash = base64.b64encode(hashlib.sha256(re.sub(r'</?script>', '', PREFS_JS).encode('utf-8')).digest()).decode('ascii')
# 'unsafe-inline' is for styles only (style="", and KaTeX sets them): no inline script runs but the one hashed here.
CSP = (f"default-src 'none'; script-src 'self' {KATEX_CDN} 'sha256-{_prefs_hash}'; "
       f"style-src 'self' 'unsafe-inline' https://fonts.googleapis.com {KATEX_CDN}; "
       f"font-src https://fonts.gstatic.com {KATEX_CDN}; img-src 'self' data:; connect-src 'self'; "
       "object-src 'none'; base-uri 'none'; form-action 'none'")
CSP_META = f'<meta http-equiv="Content-Security-Policy" content="{CSP}">'
_ICONS = {'search': '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
          'focus': '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="3"/>',
          'prefs': '<path d="M4 7h10M18 7h2M4 17h2M10 17h10"/><circle cx="16" cy="7" r="2"/><circle cx="8" cy="17" r="2"/>',
          'menu': '<path d="M4 7h16M4 12h16M4 17h16"/>', 'check': '<path d="m5 13 4.5 4.5L19 7"/>',
          'book': '<path d="M12 6.5C10 5 7 4.5 4 5v13.5c3-.5 6 0 8 1.5 2-1.5 5-2 8-1.5V5c-3-.5-6 0-8 1.5ZM12 6.5V20"/>',
          'lecture': '<path d="m12 5 9 4.5-9 4.5-9-4.5L12 5Z"/><path d="M7 12v4.5c3 2 7 2 10 0V12"/>',
          'pen': '<path d="m15 5 4 4L9 19l-5 1 1-5L15 5Z"/>',
          'cards': '<rect x="3.5" y="7" width="13" height="13" rx="2.5"/><path d="M8 3.8h10a2.5 2.5 0 0 1 2.5 2.5V16"/>',
          'exam': '<path d="M7 3.5h7.5L19 8v12.5H7a2 2 0 0 1-2-2v-13a2 2 0 0 1 2-2Z"/><path d="M14 3.5V8h5M9 13h6M9 17h4"/>'}


def icon(name: str) -> str:
    return f'<svg class="i" viewBox="0 0 24 24" aria-hidden="true">{_ICONS[name]}</svg>'


def topbar(links: str = '', course: str = '', course_href: str = '', menu: bool = False) -> str:
    """Logo · the course · its modes · search / focus / settings."""
    crs = f'<a class="crs" href="{course_href}">{html.escape(course)}</a>' if course else ''
    btn = f'<button class="ib menu" data-act="side" aria-label="תפריט">{icon("menu")}</button>' if menu else ''
    modes = f'<nav class="modes">{links}</nav>' if links else ''
    focus = f'<button class="ib" data-act="focus" title="מצב ריכוז (F)" aria-label="מצב ריכוז">{icon("focus")}</button>' if menu else ''
    return (f'<header class="top">{btn}<a class="logo" href="/learn/"><span class="mk">ל</span><span class="tx">הקורסים שלי</span></a>'
            f'{crs}{modes}<div class="tools"><button class="ib search" data-act="search" aria-label="חיפוש">{icon("search")}<span>חיפוש בחומר</span>'
            f'<kbd>Ctrl K</kbd></button>{focus}<button class="ib" data-act="prefs" aria-label="הגדרות קריאה">{icon("prefs")}</button></div></header>')


_OVERLAYS = ('<div class="scrim pal-scrim"></div><div class="scrim side-scrim"></div>'
             '<div class="pal" role="dialog" aria-label="חיפוש"><input type="search" placeholder="מה לחפש?" aria-label="חיפוש"><div class="list"></div></div>'
             '<div class="prefs"><label>ערכת צבעים</label><div class="seg"><button data-p="theme" data-v="auto">אוטומטי</button>'
             '<button data-p="theme" data-v="light">יום</button><button data-p="theme" data-v="sepia">נייר</button>'
             '<button data-p="theme" data-v="dark">לילה</button></div><label>גודל טקסט</label><div class="seg">'
             '<button data-p="size" data-v="s">קטן</button><button data-p="size" data-v="m">רגיל</button><button data-p="size" data-v="l">גדול</button></div>'
             '<div class="keys"><kbd>Ctrl K</kbd><span>חיפוש בכל החומר</span><kbd>← →</kbd><span>דף הבא / הקודם</span><kbd>F</kbd><span>מצב ריכוז + שעון 25 דקות</span>'
             '<kbd>D</kbd><span>סמן שסיימתי</span></div></div>'
             '<div class="timer"><b>25:00</b><button class="ib" data-act="timer">עצור</button><button class="ib" data-act="focus">יציאה</button></div>')


def _asset_v() -> str:
    import hashlib
    return hashlib.sha1(b''.join(f.read_bytes() for f in sorted(ASSETS.iterdir()))).hexdigest()[:8]


def page(title: str, body: str, up: str = '', wide: bool = False, top: str | None = None, bare: bool = False,
         course: str = '', attrs: str = '') -> str:
    """bare: the body brings its own layout; else a centred reading column. course → its hue, and search stays inside it."""
    if top is None:
        top = topbar()
    inner = body if bare else f'<main class="{"hub" if wide else "narrow"}">{body}</main>'
    v = _asset_v()
    return (f'<!doctype html><html lang="he" dir="rtl" style="--h:{HUES.get(course, 250)}"><head><meta charset="utf-8">{CSP_META}'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>'
            f'{PREFS_JS}{FONTS}<link rel="stylesheet" href="/learn/assets/app.css?v={v}">{KATEX}'
            f'<script defer src="/learn/assets/app.js?v={v}"></script></head>'
            f'<body data-course="{html.escape(course)}"{attrs}>{top}{inner}{_OVERLAYS}</body></html>')


# ── W3Schools boxes on a page's HTML (03/11) ─────────────────────────────────────────────────────────────

_EX_RX = re.compile(r'דוגמ|תרגיל|בעיה|מקרה|יישום|הדגמה|Example', re.I)


def _block_end(h: str, start: int, level: int) -> int:
    """Where a section that starts at `start` (an h<level>) ends: the next heading of that level or higher, an <hr>,
    or the end — top-level blocks only (python-markdown puts each on its own line)."""
    m = re.compile(rf'\n<(?:h[1-{level}]\b|hr\b)').search(h, start + 1)
    return m.start() if m else len(h)


def _wrap_sections(h: str, level: int, title_rx: re.Pattern, cls: str) -> str:
    """Each h<level> section whose title matches → in a <div class=cls> box (never a box inside a box)."""
    out, pos = [], 0
    for m in re.finditer(rf'(?m)^<h{level}>(.*?)</h{level}>', h):
        if m.start() < pos or not title_rx.search(re.sub(r'<[^>]+>', '', m.group(1))):
            continue
        if h.count('<div class="w3-', 0, m.start()) > h.count('<!--/w3-->', 0, m.start()):
            continue                              # already inside a box
        end = _block_end(h, m.start(), level)
        out += [h[pos:m.start()], f'<div class="{cls}">\n', h[m.start():end], '\n</div><!--/w3-->']
        pos = end
    return ''.join(out) + h[pos:]


def _li_end(h: str, start: int) -> int:
    """The index just past the </li> closing the <li> that opens at start (nested lists counted)."""
    depth, i = 0, start
    for m in re.compile(r'<(/?)li\b[^>]*>').finditer(h, start):
        depth += -1 if m.group(1) else 1
        if depth == 0:
            return m.end()
        i = m.end()
    return len(h)


_ANS = r'(תשובה|פתרון)(?:\s+(?:נכונה|מלא|מלאה|מפורט|מפורטת|סופי|סופית|מוצע|מוצעת))?\s*\d*\s*[:：.]?'


def _fold_answers(h: str) -> str:
    """Quiz answers / exercise solutions → folded under "הצג תשובה" (W3Schools' exercises: try first)."""
    out, pos = [], 0
    for m in re.finditer(rf'<li>\s*<strong>{_ANS}</strong>', h):
        if m.start() < pos:
            continue
        end = _li_end(h, m.start())
        inner = h[m.end():end - len('</li>')]
        out += [h[pos:m.start()], f'<li><details class="ans"><summary>הצג {m.group(1)}</summary>{inner}</details></li>']
        pos = end
    h = ''.join(out) + h[pos:]
    out, pos = [], 0                          # a paragraph "**פתרון:**" … up to the next heading / rule
    for m in re.finditer(rf'(?m)^<p><strong>{_ANS}</strong>:?', h):
        if m.start() < pos:
            continue
        end = _block_end(h, m.start(), 4)
        out += [h[pos:m.start()], f'<details class="ans"><summary>הצג {m.group(1)}</summary>\n<p>',
                h[m.end():end], '\n</details>']
        pos = end
    return ''.join(out) + h[pos:]


def w3_html(h: str) -> str:
    """Definitions / theorems green, "חשוב למבחן" yellow, examples in gray boxes, key points as a summary panel,
    answers folded."""
    h = re.sub(r'<blockquote>(\s*<p>\s*(?:⚠️\s*)?<strong>\s*(?:⚠️\s*)?(?:חשוב למבחן|טעות נפוצה|שימו לב))',
               r'<blockquote class="exam">\1', h)
    h = re.sub(r'<blockquote>(\s*<p>\s*<strong>\s*(?:הגדרה|משפט|למה|טענה|תכונה|אקסיומ|כלל|נוסחה|סימון))',
               r'<blockquote class="def">\1', h)
    h = _wrap_sections(h, 3, re.compile(r'נקודות מפתח|לסיכום|סיכום'), 'w3-summary')
    for lv in (3, 4):
        h = _wrap_sections(h, lv, _EX_RX, 'w3-example')
    return _fold_answers(h)


_TITLE_JUNK = re.compile(r'^(?:סוגיי?ת\s+(?:הקדמה|הכרעה(?:\s+\S+)?|פתיחה)|סוגיה\s+\S+|חלק\s+[א-ת\d]+\'?|שלב\s+\d+|'
                         r'נושא\s+\d+|הקדמה)\s*[:：—–-]\s*')


def nice_title(t: str) -> str:
    """"סוגיית הקדמה: שני הכדים" → "שני הכדים" — the lecture's own numbering out of a textbook title."""
    t2 = re.sub(r'\s*\((?:ב?עמוד|ב?עמודים|עמ\')[^)]*\)|\s*`[^`]*`', '', t)       # "(עמודים 23–28 ב-`cond_sum`)"
    if re.search(r'מסיכומי (?:ה)?מרצה|מהחומר (?:ה)?כתוב|מסיכום המרצה', t2):
        rest = re.split(r'\s*[:：—–]\s*', t2, maxsplit=1)
        t2 = rest[1] if len(rest) > 1 and len(rest[1]) >= 4 else 'השלמות והרחבות'
    t2 = re.sub(r'^השלמ(?:ה|ות)(?:\s+\S+)?\s*[:：—–-]\s*', '', t2)
    t2 = _TITLE_JUNK.sub('', t2).strip()
    return t2[:1].upper() + t2[1:] if len(t2) >= 4 else t


# ── data ───────────────────────────────────────────────────────────────────

def lessons(ck: str, summaries: Path | None = None) -> list[dict]:
    """This semester's summaries, oldest first: {date, kind, title, md}. Moved-aside ones (replaced/) aren't."""
    from collectors.course_files import SEMESTER_START
    d = (summaries or ROOT / 'data' / 'summaries') / ck
    out = []
    for sub, kind in (('lectures', 'הרצאה'), ('tutorials', 'תרגול')):
        for md in sorted((d / sub).glob('*.md')) if (d / sub).exists() else []:
            day = md.name[:10]
            if not re.match(r'\d{4}-\d{2}-\d{2}$', day) or day < SEMESTER_START.isoformat():
                continue
            title = md.stem[13:] if md.stem[10:13] == ' - ' else md.stem
            from utils.other_group import md_has                  # 14/11: another group's recording
            out.append({'date': day, 'kind': kind, 'title': title, 'md': md, 'other': md_has(md)})
    out.sort(key=lambda r: (r['date'], r['kind'] != 'הרצאה', r['title']))
    for i, r in enumerate(out, 1):
        r['slug'] = f'lesson-{i}.html'
    return out


# ── the guide: all the material in course order, a page per topic (02/11) ──────
# The owner: "משהו שסטקר את כל החומר בסדר הגיוני כמו W3SCHOOLS". Each lesson is cut at its own headings:
# overview (TL;DR + lesson map) → one page per topic of the lesson body (### under "תוכן ...") → the add-ons →
# formulas/concepts → tips → self-quiz. Bookkeeping sections (scanned files, recap, reading times, glossary,
# continuity check) stay out — they're on the full lesson page.

SKIP_RX = re.compile(r'חומרים שנסרקו|תזכורת|זמני קריאה|בדיקת רצף|גלוסר')
BUCKETS = (('📋 דף עזר — נוסחאות ומושגים', re.compile(r'נוסחאות|מושגים|הדברים החשובים|טבלת')),
           ('💎 טיפים למבחן והעמקה', re.compile(r'טיפ|העמקה|טעויות|מלכוד')),
           ('🎯 חידון — בדוק את עצמך', re.compile(r'חידון|תרגיל')))


def _topic(head: str, body: str) -> tuple[str, str]:
    """A "### [00:03:52](panopto link) ⭐ title" section → (title, body led by a link to that moment of the recording)."""
    m = re.match(r'\s*\[(\d{1,2}:\d{2}(?::\d{2})?)\]\((https?://[^)\s]+)\)', head)
    ts = re.match(r'\s*\[(\d{1,2}:\d{2}(?::\d{2})?)\]', head)
    return _clean_title(head), ((f'[▶️ צפה בהקלטה מ-{m.group(1)}]({m.group(2)})\n\n' if m else '') + body
                                + (f'\n<!--ts:{ts.group(1)}-->' if ts else ''))     # → the lesson map's page pointers


def _clean_title(t: str) -> str:
    t = re.sub(r'^\s*\[\d{1,2}:\d{2}(?::\d{2})?\](?:\(https?://[^)\s]+\))?\s*', '', t)
    t = re.sub(r'^[^\w$(]+', '', t)                 # leading emoji / marks (⭐ 🚨 📖 —)
    return t.strip() or 'נושא'


def _split(text: str, level: int) -> tuple[str, list[tuple[str, str]]]:
    """text → (what comes before the first heading of this level, [(heading, body)])."""
    parts = re.split(rf'^{"#" * level}[ \t]+(.+?)[ \t]*$', text, flags=re.M)
    return parts[0], [(parts[i].strip(), parts[i + 1]) for i in range(1, len(parts) - 1, 2)]


def lesson_sections(text: str) -> list[tuple[str, str, str]]:
    """One lesson summary → [(page title, markdown, role)] in reading order; role: 'over' (TL;DR + map) · 'topic'
    (the lesson's own subjects + add-ons) · 'extra' (cheat sheet / tips / quiz). Never empty for a non-empty summary."""
    text = re.sub(r'<!--.*?-->', '', text or '', flags=re.S)
    _, h2 = _split(text, 2)
    over, topics, extra, buckets, other = [], [], [], {b: [] for b, _ in BUCKETS}, []
    for head, body in h2:
        if SKIP_RX.search(head):
            continue
        if re.search(r'TL;?DR|מפת ה', head):
            over.append(f'## {head}\n{body}')
        elif re.search(r'^\W*תוכן', head):
            pre, h3 = _split(body, 3)
            if not h3:                                 # 06/11 three lessons use #### here: whole lessons were one skipped page
                pre, h3 = _split(body, 4)
            if pre.strip():
                over.append(pre)
            topics += [_topic(h, b) for h, b in h3] or [(_clean_title(head), body)]
        elif 'השלמ' in head:
            pre, h3 = _split(body, 3)
            extra += [(_clean_title(h), (pre if i == 0 else '') + b) for i, (h, b) in enumerate(h3)] \
                or [(_clean_title(head), body)]
        else:
            b = next((b for b, rx in BUCKETS if rx.search(head)), None)
            if b:
                buckets[b].append(f'## {head}\n{body}')
            else:
                other.append((_clean_title(head), body))
    out = ([('סקירת השיעור', '\n\n'.join(over), 'over')] if over else [])
    out += [(t, m, 'topic') for t, m in topics + extra + other]
    out += [(b, '\n\n'.join(v), 'extra') for b, v in buckets.items() if v]
    out = [x for x in out if x[1].strip()]
    if not out and text.strip():                     # a summary without the usual headings → one page
        out = [('השיעור', text, 'topic')]
    return out


def guide_sections(text: str) -> list[tuple[str, str]]:
    return [(t, m) for t, m, _ in lesson_sections(text)]


def _file_name(r: dict, ext: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', '-', f'{r["date"]} - {r["kind"]} - {r["title"]}')[:150] + ext


def build_downloads(ck: str, ls: list[dict], dest: Path) -> dict:
    """Each lesson's PDF next to its page (lesson-N.pdf) + one ZIP of the whole course (PDF, or the md when a lesson
    has none) → {'zip': name or '', 'n': files in it}."""
    import zipfile
    files = []
    for r in ls:
        pdf = r['md'].with_suffix('.pdf')
        if pdf.exists():
            shutil.copyfile(pdf, dest / r['slug'].replace('.html', '.pdf'))
            r['pdf'] = r['slug'].replace('.html', '.pdf')
            files.append((pdf, _file_name(r, '.pdf')))
        else:
            r['pdf'] = ''
            files.append((r['md'], _file_name(r, '.md')))
    if not files:
        return {'zip': '', 'n': 0}
    name = f'{ck}-summaries.zip'
    with zipfile.ZipFile(dest / name, 'w', zipfile.ZIP_DEFLATED) as z:
        for src, arc in files:
            z.write(src, arc)
    return {'zip': name, 'n': len(files)}


# ── the course's own files: "עמוד 16–19 ב-`random_v_sum_b`" opens that page and shows it (02/11) ──────────
# The owner: "תפתח את זה ותוסיף". The summaries point into the lecturer's / Moodle files; those PDFs are copied to
# <ck>/files/ (only the ones pointed at), every pointer becomes a link to its page, and on the guide's topic pages the
# pages themselves are shown, folded — from the file's text version when it marks its pages ("### עמוד N").

PTR_RX = re.compile(r'עמוד(?:ים)?\s*(\d+)(?:\s*[–-]\s*(\d+))?\s*ב-?\s*`([^`\n]+)`')            # עמוד 16–19 ב-`f`
PTR2_RX = re.compile(r'\(`([^`\n]+)`,\s*עמוד(?:ים)?\s*(\d+)(?:\s*[–-]\s*(\d+))?\)')            # (`f`, עמוד 16–17)
PTR3_RX = re.compile(r'עמוד(?:ים)?\s*(\d+)(?:\s*[–-]\s*(\d+))?\s*(במצגת|בשקפים|בסיכום)')        # עמודים 12–24 במצגת
MAX_EMBED_PAGES = 8


def source_files(ck: str, root: Path | None = None) -> dict[str, Path]:
    """name → PDF, for every PDF the course has (Moodle + lecturer site). Both "186458_מצגת שיעור 2" and
    "מצגת שיעור 2" find it."""
    d = (root or ROOT / 'data' / 'moodle') / ck
    out: dict[str, Path] = {}
    for p in sorted(d.rglob('*.pdf')) if d.exists() else []:
        out.setdefault(p.stem, p)
        out.setdefault(re.sub(r'^\d+_', '', p.stem), p)
    return out


def _lesson_no(r: dict) -> str | None:
    m = re.search(r'(?:שיעור(?:\s*מספר)?|הרצאה|תרגול|Lecture|Lesson)\s*(\d+)', r['md'].stem, re.I)
    return m.group(1) if m else None


def _own_file(word: str, r: dict, files: dict[str, Path]) -> str | None:
    """"במצגת" / "בסיכום" of this lesson → the one file that is that lesson's slides / summary (None when unsure)."""
    n = _lesson_no(r)
    if not n:
        return None
    kinds = ('סיכום',) if word == 'בסיכום' else ('מצגת', 'שקפים', 'slides')
    hits = {p for k, p in files.items()
            if any(w in k.lower() for w in kinds) and re.search(rf'(?<!\d){n}(?!\d)', k)}
    return next(iter(hits)).stem if len(hits) == 1 else None


def find_pointers(text: str, r: dict, files: dict[str, Path]) -> list[tuple[str, int, int]]:
    """[(file stem, first page, last page)] the text points at, only files the course really has."""
    out = []
    for m in PTR_RX.finditer(text):
        out.append((m.group(3).strip(), int(m.group(1)), int(m.group(2) or m.group(1))))
    for m in PTR2_RX.finditer(text):
        out.append((m.group(1).strip(), int(m.group(2)), int(m.group(3) or m.group(2))))
    for m in PTR3_RX.finditer(text):
        f = _own_file(m.group(3), r, files)
        if f:
            out.append((f, int(m.group(1)), int(m.group(2) or m.group(1))))
    return [(files[f].stem, a, max(a, b)) for f, a, b in dict.fromkeys(out) if f in files]


def link_pointers(text: str, r: dict, files: dict[str, Path], prefix: str, used: set) -> str:
    """Every pointer → a link that opens the PDF at that page (files/<stem>.pdf#page=N)."""
    def url(f: str, a: str) -> str | None:
        if f not in files:
            return None
        used.add(files[f])
        return f'{prefix}files/{quote(files[f].name)}#page={a}'

    def one(m):
        u = url(m.group(3).strip(), m.group(1))
        return f'[📄 {m.group(0).replace("`", "")}]({u})' if u else m.group(0)

    def two(m):
        u = url(m.group(1).strip(), m.group(2))
        return f'([📄 {m.group(0)[1:-1].replace("`", "")}]({u}))' if u else m.group(0)

    def three(m):
        f = _own_file(m.group(3), r, files)
        u = url(f, m.group(1)) if f else None
        return f'[📄 {m.group(0)}]({u})' if u else m.group(0)
    return PTR3_RX.sub(three, PTR2_RX.sub(two, PTR_RX.sub(one, text)))


def file_pages(pdf: Path, a: int, b: int) -> str:
    """Pages a..b of the file's text version (<stem>.md beside the PDF, "### עמוד N" headings); '' when it has none."""
    try:
        t = pdf.with_suffix('.md').read_text(encoding='utf-8')
    except OSError:
        return ''
    _, pages = _split(t, 3)
    got = [(int(m.group(1)), body) for h, body in pages for m in [re.match(r'עמוד\s*(\d+)', h)] if m]
    keep = [f'#### עמוד {n}\n{body.strip()}' for n, body in got if a <= n <= min(b, a + MAX_EMBED_PAGES - 1)]
    return '\n\n'.join(keep)


# ── the guide: W3Schools-style (02/11) ───────────────────────────────────────────────────────────────────
# The owner: "משהו שסטקר את כל החומר בסדר הגיוני כמו W3SCHOOLS" + "לפעמים לומדים בתרגול והרצאות אותו חומר אני רוצה
# את זה באותו מקום מבין לא מעורבב" + "אם אני רוצה לעבור הרצאה הרצאה תהיה האפשרות". Three ways through the same pages:
#   guide/      by topic — chapters in a logical order; a chapter holds the lectures' pages, then the tutorials'
#               pages on that subject, under their own headings; each lesson's cheat sheet / tips / quiz close the
#               chapter most of that lesson went to
#   lectures/   lecture after lecture        tutorials/   tutorial after tutorial
# Gemini sorts the topics into chapters once (data/site_chapters/<ck>.json) and again only when a lesson adds
# topics it hasn't seen; without it (no key, budget lock, error) the saved chapters + the lesson order are used.

CHAPTERS_DIR = ROOT / 'data' / 'site_chapters'
CHAPTER_MODEL = 'gemini-3.8-flash'
CHAPTER_PROMPT = """אתה עורך מדריך לימוד בסגנון W3Schools לקורס "{course}".
לפניך כל הנושאים שנלמדו עד עכשיו בהרצאות ובתרגולים (מספר · סוג · תאריך · שיעור · נושא).
סדר אותם לפרקים לפי נושא:
- פרק = נושא אחד ברור, כמו פרק בספר לימוד (שם קצר, 2-6 מילים, בעברית). בערך פרק לכל 3-10 פריטים.
- נושא שנלמד גם בהרצאה וגם בתרגול — באותו פרק.
- סדר הפרקים = סדר לימוד הגיוני, מהבסיס למתקדם (בדרך כלל כסדר הקורס).
- פריטים שהם מנהלות ולוגיסטיקה של הקורס (ציונים, דרישות, מבנה הקורס, היכרות, הודעות) — ב-"skip", לא בפרק.
- כל מספר מופיע בדיוק פעם אחת (בפרק או ב-skip).
{existing}
פריטים:
{items}

החזר JSON בלבד: {{"chapters": [{{"title": "...", "items": [1, 4, 2]}}], "skip": [3]}}"""
CHAPTERS_VERSION = 2


SUB_TIMEOUT = 150           # 11/11: the API answers the biggest site prompt in ~20 s; a call still silent at 300 s was stuck
SUB_RETRIES = 1             # …and a stuck call is tried once more on the subscription before the API pays for it


# 13/11: the subscription rested (a hung call on 11/11, "no capacity" 503 on 12/11) and the rest of that night's build
# went to the paid API at full speed — $1.86 and $0.81 of the two nights' $3.41 / $2.61, for pages nothing waits for.
# A build pays the API at most API_CAP_USD; past it every call raises 'budget lock …', so the topics stop without
# counting a failure and the polish / stages / chapters wait for the next night (most nights the subscription is back).
API_CAP_USD = float(os.environ.get('SSA_SITE_API_CAP', '0.25'))
_API = {'cap': None, 'spent': 0.0}
_API_LOCK = threading.Lock()


def api_cap(cap: float | None) -> None:
    """The API spend this build may still make (None = no cap: a hand run that prints its cost)."""
    with _API_LOCK:
        _API['cap'], _API['spent'] = cap, 0.0


def _ask_gemini(prompt: str, as_json: bool = True, op: str = 'site.chapters', max_tokens: int = 8000) -> str:
    import os
    if os.environ.get('PYTEST_CURRENT_TEST'):
        raise RuntimeError('no Gemini in tests')
    from utils import gemini_sub                       # 03/11 the owner's Pro subscription first — free; the API if it can't
    sub = gemini_sub.ask(prompt + ('\n\nהחזר JSON בלבד, בלי הסברים.' if as_json else
                                   '\n\nהחזר רק את הטקסט המבוקש, בלי הקדמה ובלי הסברים.'),
                         timeout=SUB_TIMEOUT, retries=SUB_RETRIES)
    if sub:
        return gemini_sub.json_of(sub) if as_json else sub
    from utils.budget import budget_lock_reason
    if budget_lock_reason():
        raise RuntimeError('budget lock')
    with _API_LOCK:
        if _API['cap'] is not None and _API['spent'] >= _API['cap']:
            raise RuntimeError(f"budget lock: the site's API share for this build (${_API['cap']:.2f}) is spent")
    return _api_call(prompt, as_json, op, max_tokens)


def _api_call(prompt: str, as_json: bool, op: str, max_tokens: int) -> str:
    import google.genai as genai
    from google.genai import types as T
    client = genai.Client(api_key=os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY'))
    resp = client.models.generate_content(
        model=CHAPTER_MODEL, contents=prompt,
        config=T.GenerateContentConfig(max_output_tokens=max_tokens, temperature=0.0,
                                       response_mime_type='application/json' if as_json else 'text/plain',
                                       thinking_config=T.ThinkingConfig(thinking_level='low')))
    from utils.usage_tracker import log_gemini
    rec = log_gemini(resp, CHAPTER_MODEL, op=op) or {}
    with _API_LOCK:
        _API['spent'] += float(rec.get('cost_usd') or 0)
    return resp.text or ''


def chapters(ck: str, items: list[dict], ask=None, path: Path | None = None) -> list[dict]:
    """items [{key, kind, date, lesson, title}] → [{title, keys}] covering every key exactly once."""
    from config.paths import COURSE_NAMES
    p = path or CHAPTERS_DIR / f'{ck}.json'
    try:
        raw_saved = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raw_saved = {}
    fresh = raw_saved.get('v') == CHAPTERS_VERSION
    saved = raw_saved.get('chapters') or []
    skip = set(raw_saved.get('skip') or []) if fresh else set()
    keys = [it['key'] for it in items]
    known = {k for c in saved for k in c.get('keys') or []} | skip
    chs = saved
    if set(keys) - known or not fresh:
        try:
            existing = ('פרקים קיימים — שמור על השמות ועל הסדר שלהם, והוסף פרק חדש רק לנושא חדש באמת: '
                        + ' · '.join(c['title'] for c in saved)) if saved else ''
            listing = '\n'.join(f'{i} · {it["kind"]} · {it["date"][8:10]}/{it["date"][5:7]} · {it["lesson"]} · {it["title"]}'
                                for i, it in enumerate(items, 1))
            raw = (ask or _ask_gemini)(CHAPTER_PROMPT.format(course=COURSE_NAMES.get(ck, ck), existing=existing,
                                                             items=listing))
            data = json.loads(re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip()))
            got, seen = [], set()
            for c in data.get('chapters') or []:
                ks = []
                for n in c.get('items') or []:
                    if isinstance(n, int) and 1 <= n <= len(items) and keys[n - 1] not in seen:
                        seen.add(keys[n - 1])
                        ks.append(keys[n - 1])
                if ks and str(c.get('title') or '').strip():
                    got.append({'title': str(c['title']).strip()[:60], 'keys': ks})
            sk = {keys[n - 1] for n in data.get('skip') or [] if isinstance(n, int) and 1 <= n <= len(items)} - seen
            if got and len(seen | sk) >= len(keys) * 0.8 and len(sk) <= len(keys) * 0.3:   # nothing big dropped
                chs, skip = got, sk
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps({'v': CHAPTERS_VERSION, 'chapters': chs, 'skip': sorted(skip)},
                                        ensure_ascii=False, indent=1), encoding='utf-8')
        except Exception as e:
            print(f'  ⚠️ site chapters {ck}: {e!r}'[:200])
    # what's left (gone from the course / never sorted): drop the gone, put the unsorted beside a sibling of its lesson
    cur = set(keys) - skip                     # course logistics stay out of the guide
    keys = [k for k in keys if k in cur]
    chs = [{'title': c['title'], 'keys': [k for k in c.get('keys') or [] if k in cur]} for c in chs]
    chs = [c for c in chs if c['keys']]
    placed = {k for c in chs for k in c['keys']}
    lesson_of = {it['key']: it['lesson_i'] for it in items}
    title_of = {it['key']: it['lesson'] for it in items}
    for k in keys:
        if k in placed:
            continue
        home = next((c for c in chs if any(lesson_of.get(x) == lesson_of[k] for x in c['keys'])), None)
        if home is None:
            home = {'title': title_of[k], 'keys': []}
            chs.append(home)
        home['keys'].append(k)
        placed.add(k)
    return chs


# ── a professional page, not a lecture's notes (02/11) ───────────────────────────────────────────────────
# The owner: "ממש כמו W3 רק מקצועי — שום דבר שקשור להרצאה, התייחסות רק אם יודע שזה משהו שאמרו חשוב למבחן". Each guide
# page is rewritten once by Gemini as a textbook page (data/site_pages/<ck>/<hash>.md) — every definition, formula,
# proof and example kept, the lecture itself gone (timestamps, "המרצה אמר", recaps, logistics); an exam remark stays
# as "⚠️ חשוב למבחן". A rewrite that lost content or broke a formula isn't used: the page falls back to clean_md().

PAGES_DIR = ROOT / 'data' / 'site_pages'
POLISH_VERSION = '1'
MAX_POLISH = 200                    # new rewrites per course per build (the rest wait for the next night)
POLISH_PROMPT = """אתה כותב דף במדריך לימוד מקצועי בסגנון W3Schools לקורס "{course}". נושא הדף: "{title}".
לפניך חומר גלם מסיכום {source}. כתוב ממנו דף לימוד נקי ומקצועי, כמו בספר לימוד טוב:
- בלי שום התייחסות להרצאה, לתרגול, למרצה או למתרגל: בלי חותמות זמן, בלי "בהרצאה", "המרצה אמר/הדגיש", "בשיעור הקודם",
  "כפי שראינו", בלי מנהלות, בלי הפניות לעמודים, לקבצים או להקלטה, בלי אימוג'י.
- היוצא מן הכלל: כשהחומר אומר שמשהו חשוב למבחן / חוזר במבחנים / טעות נפוצה — שמור את זה כשורה נפרדת:
  > ⚠️ **חשוב למבחן:** ...
- שמור במדויק את כל התוכן המקצועי: כל הגדרה, משפט, נוסחה, הוכחה, דוגמה ופתרון. אל תמציא ואל תוסיף עובדות שאינן
  בחומר. אל תקצר הוכחות, דוגמאות ופתרונות.
{shape}
- הגדרות ומשפטים בתוך ציטוט: > **הגדרה (שם):** ... / > **משפט (שם):** ...
- נוסחאות ב-LaTeX עם $...$ ו-$$...$$ כמו בחומר; קוד בתוך ``` עם שם השפה.
- אל תכתוב את שם הנושא ככותרת ראשית (הוא כבר מופיע מעל הדף). החזר Markdown בלבד, בעברית.

חומר הגלם:
{md}"""
SHAPE_TOPIC = ('- מבנה: פסקת פתיחה קצרה (2-3 משפטים) — מה זה ולמה זה חשוב; אחר כך כותרות ### לפי הצורך (הגדרה, משפט, '
               'הוכחה, דוגמה, שימושים); בסוף "### נקודות מפתח" עם 2-5 בולטים.')
SHAPE_EXTRA = '- מבנה: שמור על מבנה החומר (רשימה / טבלה / שאלות ותשובות). בלי פסקת פתיחה ובלי "נקודות מפתח".'


def clean_md(md: str) -> str:
    """The lecture's marks out, no LLM: the recording link, timestamps, the emoji in front of headings."""
    md = re.sub(r'^\s*\[▶️[^\]]*\]\([^)]*\)\s*$', '', md or '', flags=re.M)
    md = re.sub(r'<!--.*?-->', '', md, flags=re.S)
    md = re.sub(r'\[\d{1,2}:\d{2}(?::\d{2})?\](?:\(https?://[^)\s]+\))?\s*', '', md)
    md = re.sub(r'^(#{2,6}\s+)[^\w$(]+', r'\1', md, flags=re.M)
    return md.strip()


def _odd_dollars(md: str) -> bool:
    body = re.sub(r'\$\$.+?\$\$', '', md, flags=re.S)
    return len(re.findall(r'(?<![\\$])\$(?!\$)', body)) % 2 == 1


def _polish_ok(src: str, out: str) -> bool:
    """Kept (most of) the content and every formula closes. 10/11: a source that itself has an odd '$' (a price,
    "$5") can't come back even, so the rewrite is held only to the source's own parity — such pages were
    rejected every night, forever."""
    return (len(out) >= 0.35 * len(src) and (not _odd_dollars(out) or _odd_dollars(src))
            and out.count('$$') % 2 == 0 and out.count('```') % 2 == 0)


POLISH_TRIES = 2                     # 10/11: a rewrite rejected this many times for the same text isn't asked again


def _rejections(f: Path) -> int:
    try:
        return int(f.with_suffix('.rejected').read_text(encoding='utf-8').strip() or 0)
    except (OSError, ValueError):
        return 0


def polish(ck: str, jobs: list[dict], ask=None, cache: Path | None = None) -> list[str]:
    """jobs [{title, md, role, kind}] → markdown per job: the cached rewrite, a new one (≤ MAX_POLISH, in parallel),
    else clean_md."""
    import hashlib
    from concurrent.futures import ThreadPoolExecutor
    from config.paths import COURSE_NAMES
    d = (cache or PAGES_DIR) / ck
    out = [clean_md(j['md']) for j in jobs]
    todo = []
    for i, j in enumerate(jobs):
        h = hashlib.sha1(f"{POLISH_VERSION}|{j['role']}|{j['title']}|{j['md']}".encode('utf-8')).hexdigest()[:16]
        f = d / f'{h}.md'
        if f.exists():
            out[i] = f.read_text(encoding='utf-8')
        elif _rejections(f) >= POLISH_TRIES:
            continue                                    # same text, rejected before: clean_md, no new call
        elif len(todo) < MAX_POLISH:
            todo.append((i, f))
    if not todo:
        return out
    ask = ask or (lambda prompt: _ask_gemini(prompt, as_json=False, op='site.polish', max_tokens=12000))

    def one(job):
        i, f = job
        j = jobs[i]
        src = out[i]
        try:
            res = ask(POLISH_PROMPT.format(
                course=COURSE_NAMES.get(ck, ck), title=j['title'],
                source='של תרגול (דוגמאות ותרגילים פתורים)' if j['kind'] == 'תרגול' else 'של הרצאה',
                shape=SHAPE_TOPIC if j['role'] == 'topic' else SHAPE_EXTRA, md=src))
        except Exception as e:
            return f'{e!r}'[:120]
        res = re.sub(r'^```(?:markdown|md)?\s*\n|\n```\s*$', '', (res or '').strip())
        if not _polish_ok(src, res):
            try:
                d.mkdir(parents=True, exist_ok=True)
                f.with_suffix('.rejected').write_text(str(_rejections(f) + 1), encoding='utf-8')
            except OSError:
                pass
            return 'rejected'
        d.mkdir(parents=True, exist_ok=True)
        f.write_text(res, encoding='utf-8')
        out[i] = res
        return ''
    with ThreadPoolExecutor(6) as ex:
        errs = [e for e in ex.map(one, todo) if e]
    if errs:
        print(f'  ⚠️ site polish {ck}: {len(errs)}/{len(todo)} kept as is ({errs[0]})')
    return out


# ── the course as a path, from nothing to the exam (05/11) ───────────────────────────────────────────────
# The owner: "כאילו היית מכין אתר שמלמד את החומר של הקורס מא' עד ת' בשלבים". Each chapter is a stage, walked in one line:
#   פתיחה (why it matters, what you'll be able to do, the idea from zero) → הסבר → דוגמאות → בדוק את עצמך →
#   שאלות ממבחנים אמיתיים של הנושא → מבחן שלב (passed at 70%).
# Gemini writes the opening and the quiz once per stage (data/site_stages/<ck>/<hash>.json), again only when the stage's
# material changed. Without them (no quota, a bad answer) the stage is just its pages, and it's retried next night.

STAGES_DIR = ROOT / 'data' / 'site_stages'
STAGE_VERSION = '2'
STAGE_CHARS = 16000
STAGE_PASS = 0.7
STAGE_PROMPT = """אתה מורה מעולה שבונה קורס מקוון בעברית ל"{course}", לסטודנט שמתחיל מאפס. זה שלב {n} מתוך {total}: "{title}".
{before}
לפניך חומר השלב. החזר JSON בלבד:
{{"why": "משפט אחד: למה השלב הזה חשוב ומה הוא נותן",
 "goals": ["3-5 דברים שתדע לעשות בסוף השלב, כל אחד מתחיל בפועל"],
 "recall": "1-2 משפטים: מה צריך לזכור מהשלבים הקודמים כדי להבין את זה (ריק בשלב הראשון)",
 "idea": "הרעיון המרכזי מאפס, בלי להניח ידע קודם: 2-4 פסקאות קצרות בשפה פשוטה, עם דוגמה מוחשית אחת מהחיים. Markdown.",
 "exam_topics": ["מהרשימה הזו בלבד, הנושאים ששייכים לשלב: {topics}"],
 "quiz": [{{"q": "שאלה", "options": ["א", "ב", "ג", "ד"], "answer": 0, "why": "למה זו התשובה, ומה הטעות בשאר"}}]}}
כללים:
- quiz: בדיוק 6 שאלות שבודקות הבנה ולא שינון, מהקל לקשה, רק ממה שנלמד בחומר. answer הוא האינדקס (0-3) של התשובה הנכונה, והמיקום שלה משתנה בין השאלות.
- אל תזכיר הרצאה, מרצה, מתרגל או שיעור. אל תמציא הגדרות או משפטים שאינם בחומר.
- נוסחאות ב-LaTeX בין $...$, וב-JSON כל backslash כפול (\\\\frac). קוד בתוך `...`.

חומר השלב:
{md}"""
CHECK_PROMPT = """לפניך חומר לימוד ושאלות אמריקאיות עליו. פתור כל שאלה רק לפי החומר, בלי לנחש.
החזר JSON בלבד: {{"answers": [אינדקס התשובה הנכונה (0-3), או -1 אם החומר לא מכריע, או אם יש יותר מתשובה נכונה אחת]}}

חומר:
{md}

שאלות:
{qs}"""
_BAD_ESC = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')       # "\frac" written with one backslash became a form feed


def _stage_json(raw: str) -> dict:
    """The model's JSON, with "\\frac"-style single backslashes repaired (a LaTeX command can't be a JSON escape,
    and "\\f" + "rac" would silently become a form feed)."""
    t = re.sub(r'^```(?:json)?\s*|\s*```$', '', (raw or '').strip())
    return json.loads(re.sub(r'(?<!\\)((?:\\\\)*)\\(?=[a-mo-zA-Z][a-zA-Z]|n(?:eq|ot|abla|u|e|i))', r'\1\\\\', t))


def checked_quiz(quiz: list[dict], md: str, ask) -> list[dict]:
    """A second, separate reading solves the quiz without seeing the key: a question it answers differently is dropped,
    so a wrong key never teaches something false."""
    qs = '\n'.join(f'{i}. {q["q"]}\n' + '\n'.join(f'  {k}) {o}' for k, o in enumerate(q['options'])) for i, q in enumerate(quiz))
    got = _stage_json(ask(CHECK_PROMPT.format(md=md, qs=qs))).get('answers') or []
    return [q for i, q in enumerate(quiz) if i < len(got) and got[i] == q['answer']]


def _stage_ok(d: dict) -> bool:
    qs = d.get('quiz') or []
    if not (str(d.get('idea') or '').strip() and d.get('goals') and len(qs) >= 4):
        return False
    for q in qs:
        opts = q.get('options') or []
        a = q.get('answer')
        if not (str(q.get('q') or '').strip() and len(opts) >= 3 and isinstance(a, int) and 0 <= a < len(opts)):
            return False
    text = json.dumps(d, ensure_ascii=False)
    plain = json.loads(text)
    all_text = ' '.join([str(plain.get('idea'))] + [str(q['q']) + ' '.join(map(str, q['options'])) + str(q.get('why')) for q in qs])
    return not _BAD_ESC.search(all_text) and len(re.findall(r'(?<![\\$])\$(?!\$)', all_text)) % 2 == 0


def stage_wrap(ck: str, name: str, groups: list[dict], ask, topics: list[str], cache: Path | None = None) -> list[dict | None]:
    """Per stage: {why, goals, recall, idea, exam_topics, quiz}, or None for now."""
    import hashlib
    from concurrent.futures import ThreadPoolExecutor
    d = (cache or STAGES_DIR) / ck
    out: list[dict | None] = [None] * len(groups)
    todo = []
    for i, g in enumerate(groups):
        pages = [pg for _, ps in g['parts'] for pg in ps]
        md = re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', ' '.join(pg['html'] for pg in pages))))[:STAGE_CHARS]
        h = hashlib.sha1(f"{STAGE_VERSION}|{g['title']}|{md}".encode('utf-8')).hexdigest()[:16]
        f = d / f'{h}.json'
        try:
            out[i] = json.loads(f.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            todo.append((i, f, md))

    def one(job):
        i, f, md = job
        prev = ' · '.join(g['title'] for g in groups[:i])
        try:
            raw = ask(STAGE_PROMPT.format(course=name, n=i + 1, total=len(groups), title=groups[i]['title'], md=md,
                                          before=f'שלבים שכבר נלמדו: {prev}.' if prev else 'זה השלב הראשון בקורס.',
                                          topics=' | '.join(topics) or '(אין)'))
            data = _stage_json(raw)
            if not _stage_ok(data):
                return 'rejected'
            data['quiz'] = checked_quiz(data['quiz'], md, ask)
        except Exception as e:
            return f'{e!r}'[:120]
        if not _stage_ok(data):
            return 'quiz failed the second reading'
        data['exam_topics'] = [t for t in data.get('exam_topics') or [] if t in topics]
        d.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
        out[i] = data
        return ''
    if todo:
        with ThreadPoolExecutor(3) as ex:
            errs = [e for e in ex.map(one, todo) if e]
        if errs:
            print(f'  ⚠️ site stages {ck}: {len(errs)}/{len(todo)} waiting for the next build ({errs[0]})')
    return out


def _inline(md: str) -> str:
    return re.sub(r'^<p>|</p>$', '', md_html(str(md)).strip())


def quiz_html(w: dict) -> str:
    """Server-rendered, so KaTeX reads it like the rest of the page; the clicks are in app.js."""
    qs = []
    for n, q in enumerate(w['quiz'], 1):
        opts = ''.join(f'<button class="opt" data-i="{i}">{_inline(o)}</button>' for i, o in enumerate(q['options']))
        qs.append(f'<div class="qq" data-a="{q["answer"]}"><div class="qt"><b>{n}.</b> {_inline(q["q"])}</div><div class="opts">{opts}</div>'
                  f'<div class="why" hidden>{md_html(str(q.get("why") or ""))}</div></div>')
    return (f'<div class="quiz" data-pass="{STAGE_PASS}"><p class="mute">{len(qs)} שאלות. עוברים ב-{int(STAGE_PASS * 100)}%, ואפשר לנסות שוב כמה שרוצים.</p>'
            + ''.join(qs) + '<div class="qres" hidden><b></b><button class="btn alt again-q">נסה שוב</button></div></div>')


def exam_refs(ck: str) -> dict[str, list[dict]]:
    """topic → [{slug, n, label}] newest first — the real exam questions on it (only exams the site has)."""
    from agent import exam_weight as ew
    from config.paths import VAULT_DIR
    data = ew.load(ck)
    if not data:
        return {}
    vdir = VAULT_DIR / '🧾 מבחנים' / ew._vault(data)
    out: dict[str, list[dict]] = {}
    for e in sorted(data.get('exams') or [], key=lambda e: e['date'], reverse=True):
        if not (vdir / f"{e['date']} שאלות.md").exists():
            continue
        for q in e.get('questions') or []:
            n = re.match(r'\d+', str(q.get('q', '')))
            for t in q.get('topics') or []:
                if n:
                    out.setdefault(t, []).append({'slug': f"exam-{e['date']}.html", 'n': n.group(0),
                                                  'label': f"{e['date'][8:10]}/{e['date'][5:7]}/{e['date'][:4]} שאלה {n.group(0)}"})
    return out


def into_path(groups: list[dict], wraps: list, refs: dict, meta: str = '') -> None:
    """Each stage gets its opening first, then the real exam questions and the stage quiz last. Same pages otherwise."""
    for n, (g, w) in enumerate(zip(groups, wraps), 1):
        g['n'] = n
        if not w:
            continue
        g['why'] = str(w.get('why') or '')
        lesson = g['parts'][0][1][0]['lesson']
        m = g['parts'][0][1][0].get('meta')
        recall = f'<blockquote><p><strong>מה צריך לזכור:</strong> {_inline(w["recall"])}</p></blockquote>' if str(w.get('recall') or '').strip() else ''
        goals = ''.join(f'<li>{_inline(x)}</li>' for x in w['goals'])
        intro = {'title': f'פתיחה: {g["title"]}', 'lesson': lesson, 'meta': m,
                 'html': (f'<p class="lead">{_inline(g["why"])}</p>{recall}<div class="w3-summary"><h3>בסוף השלב תדע</h3><ul>{goals}</ul></div>'
                          f'<h2>הרעיון מאפס</h2>{w3_html(md_html(str(w["idea"])))}')}
        tail = []
        qs = [r for t in w.get('exam_topics') or [] for r in refs.get(t, [])]
        seen, uniq = set(), []
        for r in qs:
            if (r['slug'], r['n']) not in seen:
                seen.add((r['slug'], r['n']))
                uniq.append(r)
        if uniq:
            links = ''.join(f'<a class="chip" href="../{r["slug"]}#q{r["n"]}">{r["label"]}</a>' for r in uniq[:12])
            tail.append(('🧾 משאלות מבחן', [{'title': f'שאלות מבחן: {g["title"]}', 'lesson': lesson, 'meta': m,
                                             'html': f'<p>אלה שאלות אמיתיות ממבחנים קודמים על החומר של השלב. קודם לפתור, אחר כך לפתוח פתרון.</p><div class="chips">{links}</div>'}]))
        tail.append(('🏁 מבחן שלב', [{'title': f'מבחן שלב {n}: {g["title"]}', 'lesson': lesson, 'meta': m, 'html': quiz_html(w), 'quiz': True}]))
        g['parts'] = [('🧭 פתיחה', [intro])] + g['parts'] + tail


# ── what's still ahead (05/11): the path doesn't stop at last week. The rest of the course, from what the system already
# has and nothing else, no LLM. The course's syllabus table when it has one (macro); else the exam map's topics not taught
# yet, in the order of the lecturer's summaries page or the numbered slide decks (probability, micro). Neither → nothing
# (software_1 has no list of its topics). It's recomputed every build, so a topic leaves by itself once it's taught.

def _stems(t: str) -> list[str]:
    """The topic's own words, before the "(…)" of examples; a leading ו dropped, 4 letters are enough ("סוגי" / "סוגים")."""
    ws = re.findall(r"[֐-׿'a-zA-Z]{3,}", (t or '').split('(')[0].replace("'", ''))
    return [(w[1:] if w[0] == 'ו' and len(w) > 4 else w)[:4] for w in ws]


def _hits(topic: str, row: str) -> int:
    return len(set(_stems(topic)) & set(_stems(row)))


def course_order(ck: str, root: Path | None = None) -> list[str]:
    """The course's subjects in teaching order: the lecturer's summaries page, else the Moodle decks "מצגת N …"."""
    from collectors import course_files
    root = root or ROOT / 'data' / 'moodle'
    try:
        t = (root / ck / 'site_summaries.txt').read_text(encoding='utf-8')
    except OSError:
        t = ''
    rows = [r.strip() for r in re.split(r'√|(?<!\d)\d{4}(?!\d)', t.split('הקלטה חלק 4', 1)[-1])]
    rows = [r for r in rows if len(r) > 3 and not re.search(r'גרסא מצומצמת|הקלטות אלטרנטיביות|שאלות חזרה|סוגיות נוספות|אתר הקורס', r)]
    if rows:
        return rows
    try:
        idx = json.loads((root / ck / 'files' / 'index.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        idx = course_files.load_index(ck) if root == ROOT / 'data' / 'moodle' else {}
    decks = {int(m.group(1)): m.group(2).strip() for e in idx.values()
             for m in [re.match(r'מצגת\s+(\d+)\s*[:：–-]?\s*(.+)', e.get('title', ''))] if m}
    return [decks[n] for n in sorted(decks)]


def syllabus_rows(ck: str, root: Path | None = None) -> list[tuple[int, str]]:
    """[(lesson, subject)] from the table of a syllabus file that was read to text. Under 3 rows isn't a plan."""
    d = (root or ROOT / 'data' / 'moodle') / ck / 'files'
    for f in sorted(d.glob('*.md')) if d.is_dir() else []:
        if re.search(r'סילבוס|syllabus', f.name, re.I):
            rows = [(int(a), b.strip()) for a, b in re.findall(r'(?m)^\|\s*(\d{1,2})\s*\|\s*(.+?)\s*\|\s*$', f.read_text(encoding='utf-8'))]
            if len(rows) >= 3:
                return rows
    return []


def roadmap(ck: str, ls: list[dict], root: Path | None = None, stats: list | None = None, taught: set | None = None) -> dict:
    """{'items': [{title, note}], 'source': where it's from} — the subjects still to come, in order."""
    plan = syllabus_rows(ck, root)
    if plan:
        lectures = [r for r in ls if r['kind'] == 'הרצאה']
        done = max([int(n) for n in map(_lesson_no, lectures) if n] + [len(lectures)])
        titles = list(dict.fromkeys(t for n, t in plan if n > done))
        return {'items': [{'title': t, 'note': ''} for t in titles], 'source': 'לפי הסילבוס של הקורס'}
    from agent import exam_weight as ew, study_focus as sf
    from collectors.course_files import SEMESTER_START
    if stats is None:
        stats = ew.load(ck).get('stats') or []
    if not stats:
        return {'items': [], 'source': ''}
    if taught is None:
        taught = sf.taught(ck, SEMESTER_START)
    rows = course_order(ck, root)

    def rank(r: dict) -> int:
        best = max(range(len(rows)), key=lambda i: (_hits(r['topic'], rows[i]), -i), default=None)
        return best if best is not None and _hits(r['topic'], rows[best]) else len(rows)
    ordered = sorted(stats, key=lambda r: (rank(r), -r['exam_pct']))
    # sf.taught is loose: one shared word in a heading counts (micro's "כלכלת רווחה" passed "משפטי הרווחה"). What's sure is
    # most of the topic's words in one heading; before the last sure topic everything was taught, after it only what's sure.
    heads = [h for r in ls for h in re.findall(r'(?m)^#{1,3}\s+(.+)$', r['md'].read_text(encoding='utf-8')[:60000])]
    sure = {r['topic'] for r in stats if r['topic'] in taught
            and any(2 * _hits(r['topic'], h) > len(set(_stems(r['topic']))) for h in heads)}
    last = max((rank(r) for r in stats if r['topic'] in sure), default=-1) if rows else len(rows)
    items = [{'title': r['topic'].split(' (')[0], 'note': f'הופיע ב-{r["exam_pct"]}% מהמבחנים'} for r in ordered
             if r['topic'] not in taught or (r['topic'] not in sure and rank(r) > last)]
    return {'items': items, 'source': 'לפי סדר הקורס והמבחנים הקודמים'}


def final_exam(ck: str, today: date | None = None, events: list | None = None, cal: list | None = None) -> dict | None:
    """The course's next exam: the university's lists, else the owner's calendar (next semester's sittings are only there so far)."""
    from config.paths import COURSE_NAMES
    today = today or date.today()
    got = upcoming_exams(ck, today)
    if got:
        return got[0]
    if events is None:
        try:
            from integrations import tau_portal
            events = tau_portal.schedule(live=False)[0] or []
        except Exception:
            events = []
    name = COURSE_NAMES.get(ck, ck)
    first = name.split()[0]
    hits = [e for e in events if e.get('is_exam') and e.get('date', '') >= today.isoformat() and first in e.get('course', '')]
    if hits:
        return min(hits, key=lambda e: e['date'])
    if cal is None:
        try:
            from integrations import gcal
            cal = gcal.upcoming_items()
        except Exception:
            cal = []
    rows = sorted(((st, t) for st, t in cal if first in t and re.search(r'מבחן|בחינה', t) and st.date() >= today), key=lambda x: x[0])
    if not rows:
        return None
    st, t = rows[0]
    m = re.search(r"מועד\s+\S+", t)
    return {'date': st.date().isoformat(), 'time': st.strftime('%H:%M') if st.hour or st.minute else '', 'kind': m.group(0) if m else 'בחינה'}


def road_html(road: dict | None, start: int) -> str:
    """After the stages: locked ones, then the end of the line. Nothing known → nothing."""
    road = road or {}
    items, exam = road.get('items') or [], road.get('exam')
    out = []
    if items:
        out.append(f'<div class="later"><b>בהמשך הקורס</b> <span class="mute">{html.escape(road.get("source") or "")}. נפתח כשהנושא יילמד.</span></div>')
    for i, it in enumerate(items, start):
        note = f'<small>{html.escape(it["note"])}</small>' if it.get('note') else ''
        out.append(f'<div class="lk"><i>{i}</i><span><b>{html.escape(it["title"])}</b>{note}</span></div>')
    if exam:
        d = exam['date']
        out.append(f'<div class="finish"><i>🏁</i><span><b>המבחן</b><small>{html.escape(exam.get("kind") or "בחינה")} · '
                   f'{d[8:10]}/{d[5:7]}/{d[:4]} {html.escape(exam.get("time") or "")}</small></span></div>')
    return ''.join(out)


KIND_ICON = {'הרצאה': '🎓', 'תרגול': '✏️'}
MODES = (('topics', 'מסלול לימוד'), ('guide', 'מסלול לימוד'), ('lectures', 'הרצאות'), ('tutorials', 'תרגולים'))


def _bar2(cur: str, have: dict, prefix: str = '../') -> str:
    links = [f'<a{" class=on" if cur == "home" else ""} href="{prefix or "./"}">סקירה</a>']
    for d, label in MODES:
        if have.get(d):                        # 05/11 the path of topics is the main route; the guide is the long version
            label = 'מדריך מלא' if d == 'guide' and have.get('topics') else label
            links.append(f'<a{" class=on" if d == cur else ""} href="{prefix}{d}/">{label}</a>')
    if have.get('cards'):
        links.append(f'<a{" class=on" if cur == "cards" else ""} href="{prefix}cards.html">כרטיסיות</a>')
    if have.get('exams'):
        links.append(f'<a href="{prefix}#bank">מבחנים</a>')
    return ''.join(links)


def _render_track(d: Path, name: str, mode: str, label: str, groups: list[dict], have: dict, tail: str = '') -> int:
    """index.html = what's in this track + "התחל ללמוד", then 1.html, 2.html … : the route of chapters and their pages
    on the side, the page in the middle, its contents on the other side, next / previous in a dock.
    groups [{title, sub, parts: [(subhead or '', [pages])]}], page {title, html, lesson, meta?}."""
    d.mkdir(parents=True, exist_ok=True)
    ck = d.parent.name
    pages = [pg for g in groups for _, ps in g['parts'] for pg in ps]
    if not pages:
        return 0
    import hashlib
    idx = {id(pg): i for i, pg in enumerate(pages)}
    key = {id(pg): hashlib.sha1(f'{pg["lesson"]["md"].name}|{pg["title"]}'.encode('utf-8')).hexdigest()[:10]
           for pg in pages}
    href = lambda i: f'{i + 1}.html'
    top = topbar(_bar2(mode, have), name, '../', menu=True)

    def side(cur: int | None) -> str:
        out = [f'<nav class="side" aria-label="{html.escape(label)}"><div class="prog"><span></span><b><i></i></b></div>'
               f'<a class="home{" on" if cur is None else ""}" href="./">{icon("book")}{html.escape(label)}</a><div class="hits"></div>']
        for g in groups:
            here = cur is not None and any(idx[id(q)] == cur for _, ps in g['parts'] for q in ps)
            num = f'<i>{g["n"]}</i>' if g.get('n') else ''
            out.append(f'<details class="grp"{" open" if here else ""}><summary>{num}{html.escape(g["title"])}<small></small></summary>'
                       + (f'<div class="sub">{g["sub"]}</div>' if g.get('sub') else ''))
            for sub, ps in g['parts']:
                if sub:
                    out.append(f'<div class="sub">{sub}</div>')
                out.append('<div class="stops">')
                for q in ps:
                    k = idx[id(q)]
                    out.append(f'<a href="{href(k)}" data-k="{key[id(q)]}"{" class=on" if k == cur else ""}>'
                               f'{html.escape(q["title"])}</a>')
                out.append('</div>')
            out.append('</details>')
        return ''.join(out) + '</nav>'

    for i, pg in enumerate(pages):
        prev = (f'<a class="btn prev" href="{href(i - 1)}">❯ הקודם</a>' if i
                else '<a class="btn prev" href="./">❯ דף הבית</a>')
        nxt = f'<a class="btn next" href="{href(i + 1)}">הבא ❮</a>' if i + 1 < len(pages) else ''
        r = pg['lesson']
        crumb = pg.get('meta') or (f'{r["kind"]} {r["date"][8:10]}/{r["date"][5:7]} · '
                                   f'<a href="../{r["slug"]}">{html.escape(r["title"])}</a>')
        body = (f'<div class="readbar"><i></i></div><div class="shell">{side(i)}<article class="content"><p class="crumb">{crumb}</p>'
                f'<h1 class="title">{html.escape(pg["title"])}</h1><div class="prose">{pg["html"]}</div>'
                f'<p class="mute">עודכן {datetime.now():%d/%m}</p></article><aside class="toc"></aside></div>'
                f'<div class="pager dock">{prev}<span class="pos">{i + 1}/{len(pages)}</span>'
                f'<button class="btn mark" data-k="{key[id(pg)]}"{" data-quiz=1" if pg.get("quiz") else ""}>{icon("check")}<span>סמן שסיימתי</span></button>{nxt}</div>')
        (d / href(i)).write_text(page(f'{pg["title"]} — {name}', body, top=top, bare=True, course=ck), encoding='utf-8')
    index = [{'t': pg['title'].replace('$', ''), 'u': href(i),
              'x': re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', re.sub(r'<(script|style)\b.*?</\1>', '',
                                                                                    pg['html'], flags=re.S))))[:6000]}
             for i, pg in enumerate(pages)]
    (d / 'search.json').write_text(json.dumps(index, ensure_ascii=False), encoding='utf-8')
    (d / 'keys.json').write_text(json.dumps([[key[id(pg)], href(i)] for i, pg in enumerate(pages)]), encoding='utf-8')
    # the home of this track
    chs = []
    for gi, g in enumerate(groups, 1):
        items = ''.join(f'<li><a href="{href(idx[id(q)])}" data-k="{key[id(q)]}">{html.escape(q["title"])}</a></li>'
                        for _, ps in g['parts'] for q in ps)
        n = sum(len(ps) for _, ps in g['parts'])
        head = (f'<summary><i>{gi}</i><span><b>{html.escape(g["title"])}</b>' + (f'<small>{html.escape(g["why"])}</small>' if g.get('why') else '')
                + (f'<small>{g["sub"]}</small>' if g.get('sub') else '') + f'</span><em>{n} דפים</em></summary>')
        chs.append(f'<details class="stage" data-stage="{gi}">{head}<ol>{items}</ol></details>')
    intro = {'guide': f'{html.escape(name)} מההתחלה ועד המבחן, בשלבים. בכל שלב: הרעיון מאפס, הסבר, דוגמאות, שאלות ממבחנים אמיתיים ומבחן שלב.',
             'lectures': 'ההרצאות לפי הסדר, כל אחת מחולקת לנושאים שלה.',
             'tutorials': 'התרגולים לפי הסדר: דוגמאות ותרגילים פתורים.'}.get(mode, '')
    body = (f'<div class="shell" style="grid-template-columns:292px minmax(0,1fr)">{side(None)}<div style="padding:20px 22px 60px;min-width:0"><section class="hero">'
            f'<p class="eyebrow">{len(groups)} {"שלבים" if mode == "guide" else "שיעורים"} · {len(pages)} דפים</p><h1>{html.escape(label)}</h1>'
            f'<p class="intro">{intro}</p><div class="btns">'
            f'<a class="btn big" href="{href(0)}">התחל ללמוד ❮</a>'
            f'<a class="btn big alt cont" style="display:none" href="{href(0)}">המשך מאיפה שעצרת ❮</a></div></section>'
            f'<div class="path">{"".join(chs)}{tail}</div></div></div>')
    (d / 'index.html').write_text(page(f'{label} — {name}', body, top=top, bare=True, course=ck), encoding='utf-8')
    return len(pages)


# ── flash cards (04/11): definitions and the cheat sheets' rows, from the guide's own pages ─────────────────

def flash_cards(groups: list[dict]) -> list[dict]:
    """[{id, c: chapter, f: front html, b: back html}] — "**הגדרה (שם):** …" and each row of a formula table. No LLM."""
    out, seen = [], set()

    def add(ch: str, front: str, back: str) -> None:
        plain = re.sub(r'\W+', '', re.sub(r'<[^>]+>', '', front))
        if len(plain) < 2 or plain in seen or not re.sub(r'<[^>]+>', '', back).strip():
            return
        seen.add(plain)
        out.append({'id': hashlib.sha1(f'{ch}|{plain}'.encode('utf-8')).hexdigest()[:10], 'c': ch,
                    'f': html.unescape(re.sub(r'<[^>]+>', '', front)), 'b': safe_html(back)})     # f is plain text, app.js escapes it
    for g in groups:
        for _, ps in g['parts']:
            for pg in ps:
                for m in re.finditer(r'<blockquote class="def">\s*<p>\s*<strong>(.*?)</strong>(.*?)</blockquote>', pg['html'], re.S):
                    add(g['title'], m.group(1).strip().rstrip(':： '), '<p>' + re.sub(r'^\s*[:：]\s*', '', m.group(2).strip()))
                for t in re.findall(r'<table>.*?</table>', pg['html'], re.S):
                    heads = [re.sub(r'<[^>]+>', '', x) for x in re.findall(r'<th[^>]*>(.*?)</th>', t, re.S)]
                    if len(heads) < 2 or not re.search(r'נוסח|הגדר|משפט|מושג|שם', heads[0] + heads[1]):
                        continue
                    for row in re.findall(r'<tr>\s*(<td.*?)</tr>', t, re.S):
                        tds = re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)
                        if len(tds) >= 2:
                            add(g['title'], tds[0], ''.join(f'<p><span class="mute">{html.escape(h)}:</span> {c}</p>'
                                                            for h, c in zip(heads[1:], tds[1:]) if c.strip()))
    return out


def build_cards(ck: str, name: str, groups: list[dict], dest: Path, have: dict) -> int:
    cards = flash_cards(groups)
    if not cards:
        return 0
    (dest / 'cards.json').write_text(json.dumps(cards, ensure_ascii=False), encoding='utf-8')
    body = (f'<div class="deck"><h1 style="font:700 1.7rem var(--disp);margin:6px 0 10px">כרטיסיות</h1>'
            f'<div class="head"><select aria-label="פרק"></select><span class="mute left"></span></div>'
            '<div class="fc" tabindex="0"></div><div class="acts"><button class="btn no" data-ok="0">לא ידעתי <kbd>1</kbd></button>'
            '<button class="btn" data-ok="1">ידעתי <kbd>2</kbd></button></div>'
            f'<p class="mute" style="margin-top:14px">{len(cards)} כרטיסיות: הגדרות ונוסחאות מהמדריך. מה שידעת חוזר אחרי כמה ימים, מה שלא חוזר מהר.</p></div>')
    (dest / 'cards.html').write_text(page(f'כרטיסיות — {name}', body, top=topbar(_bar2('cards', dict(have, cards=True), ''), name, './'),
                                          course=ck), encoding='utf-8')
    return len(cards)


def build_guide(ck: str, name: str, ls: list[dict], dest: Path, files: dict[str, Path] | None = None,
                used: set | None = None, ask=None, chapters_path: Path | None = None, polish_ask=None,
                pages_cache: Path | None = None, exams: bool = False, stage_ask=None, stages_cache: Path | None = None, road: dict | None = None,
                topics: dict | None = None) -> dict:
    """topics/ (scripts/course_topics.py, only with `topics`: {ask, cache, cap, retry}) + guide/ (by subject) + lectures/ +
    tutorials/ → {'topics': topics, 'guide': pages, 'lectures': pages, 'tutorials': pages}."""
    files = files if files is not None else {}
    used = used if used is not None else set()
    raw = []                                   # per lesson: [(title, md, role, pointers)]
    for li, r in enumerate(ls):
        text = r['md'].read_text(encoding='utf-8')
        # the lesson map's "[00:22:27] … ← עמוד 1–4 ב-`f`" lines → the topic with that timestamp gets those pages
        by_ts: dict[str, list] = {}
        for ln in text.split('\n'):
            m = re.match(r'\s*[-*]\s*\[(\d{1,2}:\d{2}(?::\d{2})?)\]', ln)
            if m:
                by_ts.setdefault(m.group(1), []).extend(find_pointers(ln, r, files))
        out = []
        for t, md, role in lesson_sections(text):
            ptrs = list(dict.fromkeys(find_pointers(md, r, files)
                                      + (by_ts.get((re.search(r'<!--ts:([\d:]+)-->', md) or [None, ''])[1]) or [])))
            out.append((t, md, role, ptrs if role == 'topic' else []))
        raw.append(out)
    jobs = [{'title': t, 'md': md, 'role': role, 'kind': ls[li]['kind']}
            for li, out in enumerate(raw) for t, md, role, _ in out if role != 'over']
    polished = iter(polish(ck, jobs, polish_ask, pages_cache) if jobs else [])
    secs = []                                  # per lesson: [(title, role, page dict)]
    for li, out in enumerate(raw):
        r = ls[li]
        row = []
        for t, md, role, ptrs in out:
            text = clean_md(md) if role == 'over' else next(polished)
            body = w3_html(md_html(link_pointers(text, r, files, '../', used)))
            for f, a, b in ptrs[:3]:
                pdf = next((p for p in files.values() if p.stem == f), None)
                if not pdf:
                    continue
                used.add(pdf)
                rng = f'{a}–{b}' if b > a else f'{a}'
                orig = file_pages(pdf, a, b)
                link = f'<a href="../files/{quote(pdf.name)}#page={a}">📄 פתח את {html.escape(f)} בעמוד {a}</a>'
                body += (f'<details><summary>📄 בחומר הקורס — {html.escape(f)}, עמוד {rng}</summary>'
                         f'<p>{link}</p>{md_html(orig)}</details>' if orig else f'<p class="mute">{link}</p>')
            row.append((t, role, {'title': nice_title(t), 'html': body, 'lesson': r, 'li': li}))
        secs.append(row)
    have = {'guide': any(secs), 'lectures': any(r['kind'] == 'הרצאה' for r in ls),
            'tutorials': any(r['kind'] == 'תרגול' for r in ls)}
    res = {}
    # by topic — a textbook: no lesson, no date, no lecturer on these pages
    items, by_key = [], {}
    for li, row in enumerate(secs):
        r = ls[li]
        for k, (t, role, pg) in enumerate(row):
            if role == 'topic':
                key = f'{r["md"].name}#{k}#{t}'
                by_key[key] = pg
                items.append({'key': key, 'kind': r['kind'], 'date': r['date'], 'lesson': r['title'], 'lesson_i': li,
                              'title': t})
    chs = chapters(ck, items, ask, chapters_path) if items else []
    home: dict[int, int] = {}                  # lesson → the chapter most of its topics went to
    for li in range(len(ls)):
        counts = [sum(1 for k in c['keys'] if by_key[k]['li'] == li) for c in chs]
        if counts and max(counts):
            home[li] = counts.index(max(counts))
    order = {k: n for n, k in enumerate(by_key)}
    groups = []
    for ci, c in enumerate(chs):
        meta = f'{html.escape(name)} · שלב {ci + 1} מתוך {len(chs)}: {html.escape(c["title"])}'
        ks = sorted(c['keys'], key=order.get)
        lec = [dict(by_key[k], meta=meta) for k in ks if by_key[k]['lesson']['kind'] == 'הרצאה']
        tut = [dict(by_key[k], meta=meta) for k in ks if by_key[k]['lesson']['kind'] != 'הרצאה']
        rev, seen = [], {}
        for li in sorted(l for l, h in home.items() if h == ci):
            for t, role, pg in secs[li]:
                if role == 'extra':
                    seen[t] = seen.get(t, 0) + 1
                    rev.append(dict(pg, meta=meta, title=t + (f' ({seen[t]})' if seen[t] > 1 else '')))
        parts = [(p, ps) for p, ps in (('📖 הסבר', lec), ('✍️ דוגמאות ותרגילים', tut), ('🎯 בדוק את עצמך', rev)) if ps]
        groups.append({'title': c['title'], 'sub': '', 'parts': parts})
    if groups:
        refs = exam_refs(dest.name)
        wraps = stage_wrap(dest.name, name, groups, stage_ask, sorted(refs), stages_cache) if stage_ask else [None] * len(groups)
        into_path(groups, wraps, refs)
    res['cards'] = flash_cards(groups) != []
    have['cards'] = res['cards']
    have['exams'] = exams
    res['topics'] = _topic_path(dest, name, ls, raw, files, used, have, road, topics) if topics is not None else 0
    res['guide'] = _render_track(dest / 'guide', name, 'guide', 'המדריך המלא' if res['topics'] else 'מסלול הלימוד', groups, have,
                                 road_html(road, len(groups) + 1))
    for mode, kind, label in (('lectures', 'הרצאה', 'הרצאות'), ('tutorials', 'תרגול', 'תרגולים')):
        ls_groups = [{'title': r['title'], 'sub': f'{r["date"][8:10]}/{r["date"][5:7]}',
                      'parts': [('', [pg for *_, pg in secs[li]])]} for li, r in enumerate(ls) if r['kind'] == kind]
        res[mode] = _render_track(dest / mode, name, mode, label, ls_groups, have)
    if res['cards']:
        build_cards(dest.name, name, groups, dest, have)
    return res


def topic_context(ck: str, files: dict[str, Path]) -> dict:
    """What the topic path knows about the course besides its summaries: the exams and the plan of the course."""
    from agent import exam_weight as ew
    from config.paths import VAULT_DIR
    data = ew.load(ck)
    plan = [t for _, t in syllabus_rows(ck)] or course_order(ck)
    return {'stats': data.get('stats') or [], 'refs': exam_refs(ck), 'vdir': VAULT_DIR / '🧾 מבחנים' / ew._vault(data) if data else None,
            'plan': ' · '.join(plan)[:1500], 'files': {p.stem: p for p in files.values()}}


def _topic_path(dest: Path, name: str, ls: list[dict], raw: list, files: dict, used: set, have: dict, road: dict | None, opts: dict) -> int:
    """topics/ when there's something to show, else 0 and the guide stays the main route. Its failure never costs the site."""
    from scripts import course_topics as ct
    ck = dest.name
    try:
        ctx = topic_context(ck, files)
        plan = ct.prepare(ck, name, ls, raw, ctx, opts.get('ask'), opts.get('cache'), opts.get('cap', ct.NIGHT_CAP), opts.get('retry', False))
        if not plan or not plan['ready']:
            return 0
        have['topics'] = True
        if any(plan['res'].values()):
            print(f'  🧭 topics {ck}: {plan["res"]}')
        return ct.render(ck, name, plan['map'], plan['by_key'], ctx, dest, have, road, opts.get('cache'), used)
    except Exception as e:
        print(f'  ⚠️ site topics {ck}: {e!r}'[:300])
        have.pop('topics', None)
        shutil.rmtree(dest / 'topics', ignore_errors=True)
        return 0


def build_all_in_one(name: str, ls: list[dict], dest: Path, files: dict[str, Path] | None = None,
                     used: set | None = None) -> None:
    """all.html — every summary on one page, in order: to read straight through or print to one PDF."""
    toc = ''.join(f'<li><a href="#l{i + 1}">{html.escape(r["title"])}</a> <span class="mute">{r["kind"]} · '
                  f'{r["date"][8:10]}/{r["date"][5:7]}</span></li>' for i, r in enumerate(ls))
    parts = [f'<section id="l{i + 1}" style="break-before:page"><p class="mute">{r["kind"]} · {r["date"][8:10]}/{r["date"][5:7]}</p>'
             f'<h1>{html.escape(r["title"])}</h1>'
             f'{md_html(link_pointers(r["md"].read_text(encoding="utf-8"), r, files or {}, "", set() if used is None else used))}</section>'
             for i, r in enumerate(ls)]
    body = (f'<h1>{html.escape(name)} — כל הסיכומים</h1><div class="btns"><a class="btn" href="#" '
            'data-act="print">🖨️ הדפס / שמור כ-PDF אחד</a></div>'
            f'<div class="card"><ol>{toc}</ol></div>' + ''.join(parts))
    (dest / 'all.html').write_text(page(f'כל הסיכומים — {name}', f'<div class="prose">{body}</div>', course=dest.name,
                                        top=topbar('', name, './')), encoding='utf-8')


def homework(ck: str, now: datetime | None = None, items: list[dict] | None = None, state: dict | None = None,
             closed: set[str] | None = None) -> list[dict]:
    """{title, due, url, status: done|open|missed, days} — submitted on Moodle, marked ✅ by the owner, or closed by the
    loop check count as done."""
    from utils import deadlines as dl
    now = now or datetime.now()
    if items is None:
        items = dl.load_items()
    if state is None:
        state = dl.load_state()
    if closed is None:
        try:
            closed = set(json.loads((ROOT / 'data' / 'loops_state.json').read_text(encoding='utf-8')).get('closed') or [])
        except (OSError, ValueError):
            closed = set()
    out = []
    for a in items:
        if a.get('course_key') != ck:
            continue
        due = dl._due(a)
        done = dl.is_done(a, state) or dl.key_of(a) in closed
        status = 'done' if done else ('open' if due and due > now else 'missed')
        out.append({'title': a.get('title', ''), 'due': due, 'url': a.get('url') or a.get('assignment_file_url') or '',
                    'status': status, 'days': (due.date() - now.date()).days if due else None})
    out.sort(key=lambda r: r['due'] or datetime.max)
    return out


def exam_pairs(qs_md: str, sol_md: str) -> list[tuple[str, str, str]]:
    """[(n, question, solution)] — the vault notes split on '## שאלה N'."""
    def split(t: str) -> dict[str, str]:
        parts = re.split(r'^#+\s*שאלה\s*(\d+)\b[^\n]*\n', t or '', flags=re.M)
        return {parts[i]: parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}
    q, s = split(qs_md), split(sol_md)
    return [(n, q[n], s.get(n, '')) for n in sorted(q, key=int)]


def upcoming_exams(ck: str, today: date | None = None) -> list[dict]:
    try:
        import yaml
        code = str(yaml.safe_load((ROOT / 'config' / 'courses.yaml').read_text(encoding='utf-8'))['courses'][ck].get('course_code') or '')
        events = json.loads((ROOT / 'data' / 'tau_calendar.json').read_text(encoding='utf-8')).get('events') or []
    except Exception:
        return []
    today = (today or date.today()).isoformat()
    return [e for e in events if e.get('is_exam') and code and str(e.get('course_number', '')).lstrip('0') == code.lstrip('0')
            and e.get('date', '') >= today]


# ── pages ──────────────────────────────────────────────────────────────────

def _hw_html(rows: list[dict]) -> str:
    if not rows:
        return '<p class="mute">אין עדיין מטלות בקורס.</p>'
    out = []
    for r in rows:
        if r['status'] == 'done':
            st = '<span class="ok">✅ הוגש</span>'
        elif r['status'] == 'open':
            days = r['days']
            cls = 'bad' if days is not None and days <= 2 else 'warn'
            left = 'היום' if days == 0 else 'מחר' if days == 1 else f'עוד {days} ימים'
            st = f'<span class="{cls}">⏳ {left}</span>'
        else:
            st = '<span class="bad">❌ לא הוגש</span>'
        t = html.escape(r['title'])
        t = f'<a href="{html.escape(r["url"])}">{t}</a>' if r['url'] else t
        due = f'{r["due"]:%d/%m ב-%H:%M}' if r['due'] else ''
        out.append(f'<li><span>{t}<br><span class="mute">{due}</span></span><span class="when">{st}</span></li>')
    return '<ul class="rows scrolly">' + ''.join(out) + '</ul>'


def _topic_ask(prompt: str) -> str:
    """The topic path's calls, each under its own name in the ledger."""
    from scripts import course_topics as ct
    op = ('site.topicmap' if prompt.startswith(ct.MAP_PROMPT[:20]) else 'site.topiccheck' if prompt.startswith(ct.FACT_PROMPT[:20])
          else 'site.topicquiz' if prompt.startswith(CHECK_PROMPT[:20]) else 'site.topic')
    return _ask_gemini(prompt, as_json=True, op=op, max_tokens=12000)


def build_course(ck: str, dest: Path, now: datetime | None = None, topics: dict | None = None) -> dict:
    from config.paths import COURSE_NAMES, VAULT_DIR
    from collectors.course_files import SEMESTER_START
    from agent import exam_weight as ew, study_focus as sf
    now = now or datetime.now()
    name = COURSE_NAMES.get(ck, ck)
    dest.mkdir(parents=True, exist_ok=True)
    info: dict = {'course': ck, 'name': name}

    # lessons
    ls = lessons(ck)
    dl = build_downloads(ck, ls, dest)
    files, used = source_files(ck), set()
    has_exams = bool(ew.load(ck).get('stats'))
    stage_ask = lambda prompt: _ask_gemini(prompt, as_json=True, op='site.stage', max_tokens=8000)
    try:
        road = dict(roadmap(ck, ls), exam=final_exam(ck, now.date()))
    except Exception as e:                     # the map of what's ahead never costs the site
        print(f'  ⚠️ site roadmap {ck}: {e!r}'[:200])
        road = None
    tracks = build_guide(ck, name, ls, dest, files, used, exams=has_exams, stage_ask=stage_ask, road=road,
                         topics=topics if topics is not None else {}) if ls else {}
    course_top = topbar(_bar2('', {**tracks, 'exams': has_exams}, ''), name, './')
    guide_n = tracks.get('guide', 0)
    topics_n = tracks.get('topics', 0)
    if ls:
        build_all_in_one(name, ls, dest, files, used)
    for i, r in enumerate(ls):
        prev = f'<a href="{ls[i - 1]["slug"]}">→ {html.escape(ls[i - 1]["title"])}</a>' if i else '<span></span>'
        nxt = f'<a href="{ls[i + 1]["slug"]}">{html.escape(ls[i + 1]["title"])} ←</a>' if i + 1 < len(ls) else '<span></span>'
        pdf = f'<a class="btn" href="{r["pdf"]}" download="{html.escape(_file_name(r, ".pdf"))}">⬇️ הורד PDF</a>' if r['pdf'] else ''
        og = ' · מקבוצה אחרת' if r.get('other') else ''
        body = (f'<p class="crumb">{r["kind"]} · {r["date"][8:10]}/{r["date"][5:7]}{og}</p>'
                f'<h1 class="title">{html.escape(r["title"])}</h1><div class="btns">{pdf}<a class="btn alt" href="guide/">במדריך</a></div>'
                f'<div class="pager">{prev}{nxt}</div>'
                f'<div class="prose">{w3_html(md_html(link_pointers(r["md"].read_text(encoding="utf-8"), r, files, "", used)))}</div>'
                f'<div class="pager">{prev}{nxt}</div>')
        (dest / r['slug']).write_text(page(f'{r["title"]} — {name}', body, top=course_top, course=ck), encoding='utf-8')
    if used:                                   # only the course files a summary points at
        (dest / 'files').mkdir(exist_ok=True)
        for pdf in used:
            shutil.copyfile(pdf, dest / 'files' / pdf.name)

    # exam map + bank
    data = ew.load(ck)
    stats = data.get('stats') or []
    have = sf.taught(ck, SEMESTER_START) if stats else set()
    done = sf.practiced(ck, now) if stats else {}
    nxt = sf.next_topic(ck, SEMESTER_START, now) if stats else None
    bank = []
    vdir = VAULT_DIR / '🧾 מבחנים' / ew._vault(data) if data else None
    for e in sorted(data.get('exams') or [], key=lambda e: e['date'], reverse=True):
        try:
            qs = (vdir / f"{e['date']} שאלות.md").read_text(encoding='utf-8')
        except OSError:
            continue
        try:
            sol = (vdir / f"{e['date']} פתרון.md").read_text(encoding='utf-8')
        except OSError:
            sol = ''
        pairs = exam_pairs(qs, sol)
        if not pairs:
            continue
        tags: dict[str, set] = {}
        for q in e.get('questions') or []:
            n = re.match(r'\d+', str(q.get('q', '')))
            if n:
                tags.setdefault(n.group(0), set()).update(q.get('topics') or [])
        slug = f"exam-{e['date']}.html"
        parts = []
        for n, q, s in pairs:
            t = ' · '.join(sorted(tags.get(n, ())))
            parts.append(f'<div class="card" id="q{n}"><h2>שאלה {n}</h2>' + (f'<p class="mute">{html.escape(t)}</p>' if t else '')
                         + f'<div class="prose">{md_html(q)}</div>' + (f'<details class="ans"><summary>הצג פתרון</summary><div class="prose">{md_html(s)}</div></details>' if s else '') + '</div>')
        by = '' if (e.get('by') or '').strip() in ('', 'אני') else f" {e['by'].strip()}"     # 'אני' = the lecturer's own
        title = f"מבחן{by} — {e['date'][8:10]}/{e['date'][5:7]}/{e['date'][:4]}"
        (dest / slug).write_text(page(f'{title} — {name}', f'<h1 class="title">{html.escape(title)}</h1><p class="mute">'
                                       'קודם לפתור, אחר כך לפתוח את הפתרון.</p>' + ''.join(parts), top=course_top, course=ck),
                                 encoding='utf-8')
        bank.append({'slug': slug, 'title': title, 'tags': tags, 'n': len(pairs)})

    # practice sets
    pdir = ROOT / 'data' / 'practice' / ck
    practice = []
    for md in sorted(pdir.glob('*.md'), reverse=True) if pdir.exists() else []:
        t = md.read_text(encoding='utf-8')
        q, _, s = (re.split(r'^(##\s*חלק ב.*)$', t, maxsplit=1, flags=re.M) + ['', ''])[:3]
        slug = f'practice-{md.name[:10]}.html'
        body = f'<div class="prose">{md_html(q)}</div>' + (f'<details class="ans"><summary>הצג פתרונות מלאים</summary><div class="prose">{md_html(s)}</div></details>' if s else '')
        (dest / slug).write_text(page(f'סט תרגול {md.name[:10]} — {name}', body, top=course_top, course=ck), encoding='utf-8')
        practice.append({'slug': slug, 'date': md.name[:10]})

    # the course page
    hw = homework(ck, now)
    exams = upcoming_exams(ck, now.date())
    info.update(lessons=len(ls), last=ls[-1] if ls else None, hw=hw, next=nxt, exams=exams, track='topics' if topics_n else 'guide')
    open_hw = [r for r in hw if r['status'] == 'open']
    first = 'topics/' if topics_n else 'guide/' if guide_n else 'lectures/' if tracks.get('lectures') else 'tutorials/'
    size = f'{topics_n} נושאים' if topics_n else f'{guide_n} דפים'
    out = [f'<section class="hero" data-keys="{ck}" data-track="{info["track"]}"><p class="eyebrow">{len(ls)} שיעורים · {size} · <span class="pct"></span></p>'
           f'<h1>{html.escape(name)}</h1><div class="btns"><a class="btn big resume" href="{first}"><span>התחל ללמוד</span> ❮</a>'
           + (f'<a class="btn big alt" href="cards.html">{icon("cards")}כרטיסיות</a>' if tracks.get('cards') else '') + '</div></section>']
    if topics_n:
        from scripts.course_topics import plan_html
        out.append(plan_html(ck))
    tiles = []
    if topics_n:
        tiles.append(f'<a class="tile w3 half mode" href="topics/">{icon("focus")}<b>מסלול לימוד</b><span>נושא אחרי נושא, 10-15 דקות כל אחד</span></a>')
    if guide_n:
        tiles.append(f'<a class="tile w3 half mode" href="guide/">{icon("book")}<b>{"מדריך מלא" if topics_n else "מסלול לימוד"}</b>'
                     f'<span>{"כל החומר, בפירוט ולפי פרקים" if topics_n else "מההתחלה ועד המבחן, בשלבים"}</span></a>')
    if tracks.get('lectures'):
        tiles.append(f'<a class="tile w3 half mode" href="lectures/">{icon("lecture")}<b>הרצאה אחר הרצאה</b><span>בסדר שנלמד</span></a>')
    if tracks.get('tutorials'):
        tiles.append(f'<a class="tile w3 half mode" href="tutorials/">{icon("pen")}<b>תרגול אחר תרגול</b><span>דוגמאות פתורות</span></a>')
    if bank:
        tiles.append(f'<a class="tile w3 half mode" href="#bank">{icon("exam")}<b>מבחנים קודמים</b><span>{len(bank)} מבחנים עם פתרון</span></a>')
    if exams:
        e = exams[0]
        tiles.append(f'<div class="tile w4 soft"><h2>{html.escape(e.get("kind", "בחינה"))}</h2><div class="big-n" data-days="{e["date"]}"></div>'
                     f'<span data-unit class="mute">ימים · {e["date"][8:10]}/{e["date"][5:7]} {e.get("time", "")}</span></div>')
    if nxt:
        tiles.append(f'<div class="tile {"w8" if exams else "w12"}" id="now"><h2>הכי שווה ללמוד עכשיו</h2><p style="font:600 1.3rem var(--disp);margin:0 0 4px">'
                     f'{html.escape(nxt["topic"])}</p><p class="mute" style="margin:0">הופיע ב-{nxt["exam_pct"]}% מהמבחנים'
                     + ('' if nxt['practiced'] else ', ועוד לא תרגלת') + '</p></div>')
    tiles.append(f'<div class="tile" id="hw"><h2>שיעורי בית<small>{len(open_hw)} פתוחים</small></h2>{_hw_html(hw)}</div>')
    tiles.append('<div class="tile" id="lessons"><h2>שיעורים</h2><ul class="rows scrolly">' + (''.join(
        f'<li><a href="{r["slug"]}">{html.escape(r["title"])}</a><span class="when">{r["kind"]} · {r["date"][8:10]}/{r["date"][5:7]}'
        + (' · מקבוצה אחרת' if r.get('other') else '')
        + (f' · <a href="{r["pdf"]}" download="{html.escape(_file_name(r, ".pdf"))}">PDF</a>' if r['pdf'] else '') + '</span></li>'
        for r in reversed(ls)) or '<li class="mute">עוד אין סיכומים.</li>') + '</ul></div>')
    tiles.append('<div class="tile w12 again" hidden><h2>לחזור על זה</h2><ul class="rows"></ul></div>')
    if stats:
        rows = []
        for r in stats:
            ex = [b for b in bank if any(r['topic'] in ts for ts in b['tags'].values())][:3]
            links = ' · '.join(f'<a href="{b["slug"]}#q{n}">{b["title"].split("—")[-1].strip()}</a>'
                               for b in ex for n in [next(k for k, ts in b['tags'].items() if r['topic'] in ts)])
            tried = sum(v for raw, v in done.items() if ew.find(raw, [{'topic': r['topic']}], ck))
            state = ('נלמד' if r['topic'] in have else 'עוד לא נלמד') + (f' · תרגלת {tried}' if tried else '')
            rows.append(f'<div class="topic"><div>{html.escape(r["topic"])}<small>{state}</small></div>'
                        f'<div><div class="bar"><i style="width:{r["exam_pct"]}%"></i></div><small class="mute">{r["exam_pct"]}% מהמבחנים</small></div>'
                        f'<div class="links">{links}</div></div>')
        tiles.append('<div class="tile w12" id="topics"><h2>נושאים למבחן<small>לפי כמה הם חוזרים במבחנים</small></h2>' + ''.join(rows) + '</div>')
        tiles.append(f'<div class="tile w8" id="bank"><h2>מבחנים קודמים<small>{len(bank)}</small></h2><div class="chips">' + ''.join(
            f'<a class="chip" href="{b["slug"]}">{html.escape(b["title"].replace("מבחן — ", ""))}</a>' for b in bank) + '</div></div>')
    if practice:
        tiles.append('<div class="tile w4" id="practice"><h2>סטים שבועיים</h2><div class="chips">' + ''.join(
            f'<a class="chip" href="{p["slug"]}">{p["date"][8:10]}/{p["date"][5:7]}</a>' for p in practice) + '</div></div>')
    if ls:
        tiles.append('<div class="tile w12"><div class="btns">'
                     + (f'<a class="btn alt" href="{dl["zip"]}" download>כל הסיכומים (ZIP, {dl["n"]})</a>' if dl['zip'] else '')
                     + '<a class="btn alt" href="all.html">הכול בדף אחד להדפסה</a></div></div>')
    out.append(f'<div class="bento">{"".join(tiles)}</div>')
    (dest / 'index.html').write_text(page(name, ''.join(out), wide=True, top=topbar(_bar2('home', {**tracks, 'exams': bool(bank)}, ''), name, './'),
                                          course=ck), encoding='utf-8')
    return info


CARD = 'card.json'


def _save_card(i: dict, dest: Path) -> None:
    """What the top page shows of a course, kept in its folder for a build that doesn't rebuild this course."""
    card = {'course': i['course'], 'name': i['name'], 'lessons': i['lessons'], 'track': i['track'],
            'next': {'topic': i['next']['topic']} if i.get('next') else None,
            'exams': [{'date': e['date'], 'kind': e.get('kind', 'בחינה')} for e in i['exams']],
            'hw': [{'title': r['title'], 'status': r['status'], 'due': r['due'].isoformat() if r['due'] else None}
                   for r in i['hw']]}
    (dest / CARD).write_text(json.dumps(card, ensure_ascii=False, indent=1), encoding='utf-8')


def _load_card(ck: str, folder: Path, now: datetime) -> dict:
    """The course's card as of its last build, with what passed since then taken out. A folder from before cards
    existed is still listed: name, lessons and track come from the folder itself."""
    from config.paths import COURSE_NAMES
    try:
        c = json.loads((folder / CARD).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'course': ck, 'name': COURSE_NAMES.get(ck, ck), 'lessons': len(list(folder.glob('lesson-*.html'))),
                'track': 'topics' if (folder / 'topics').is_dir() else 'guide', 'next': None, 'exams': [], 'hw': []}
    for r in c['hw']:
        r['due'] = datetime.fromisoformat(r['due']) if r['due'] else None
        if r['status'] == 'open' and r['due'] and r['due'] <= now:
            r['status'] = 'missed'
    c['exams'] = [e for e in c['exams'] if e['date'] >= now.date().isoformat()]
    return c


def build(out: Path | None = None, courses: list[str] | None = None, now: datetime | None = None, topics: dict | None = None,
          api_usd: float | None = API_CAP_USD) -> dict:
    """topics: how the topic path may write ({ask, cap, retry}). None = from what's already written, no model.
    courses: only these are rebuilt. The site always ends with every course: 06/11 `--course` swapped in a site of the
    chosen course alone and the others disappeared from it. api_usd: what this build may pay the API when the
    subscription can't answer (None = no cap)."""
    from config.paths import ACTIVE_COURSES
    api_cap(api_usd)
    out = out or OUT
    tmp = out.with_name(out.name + '.tmp')
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    now = now or datetime.now()
    infos = []
    for ck in courses or list(ACTIVE_COURSES):
        try:
            infos.append(build_course(ck, tmp / ck, now, topics))
        except Exception as e:
            print(f'  ⚠️ site {ck}: {e!r}'[:300])
    if not infos:                       # every course failed → keep yesterday's site rather than an empty one
        shutil.rmtree(tmp, ignore_errors=True)
        return {'courses': 0, 'lessons': 0, 'url': URL}
    for i in infos:
        _save_card(i, tmp / i['course'])
    built = {i['course'] for i in infos}
    order = list(dict.fromkeys(list(ACTIVE_COURSES) + list(courses or [])))
    try:
        for ck in order:
            if ck not in built and (out / ck / 'index.html').exists():
                shutil.rmtree(tmp / ck, ignore_errors=True)     # what a failed build left
                shutil.copytree(out / ck, tmp / ck)
                infos.append(_load_card(ck, tmp / ck, now))
    except OSError as e:                # a course can't be carried over → keep the live site as it is
        print(f'  ⚠️ site kept as it was: {e!r}'[:300])
        shutil.rmtree(tmp, ignore_errors=True)
        return {'courses': 0, 'lessons': 0, 'url': URL}
    infos.sort(key=lambda i: order.index(i['course']))
    cards = []
    for i in infos:
        open_hw = [r for r in i['hw'] if r['status'] == 'open']
        missed = [r for r in i['hw'] if r['status'] == 'missed']
        bits = []
        if open_hw:
            bits.append(f'<span class="warn">{html.escape(open_hw[0]["title"])} עד {open_hw[0]["due"]:%d/%m}</span>')
        if missed:
            bits.append(f'<span class="bad">{len(missed)} לא הוגשו</span>')
        if i['next']:
            bits.append(f'<span>הכי שווה: {html.escape(i["next"]["topic"].split(" (")[0])}</span>')
        cards.append(f'<a class="tile w3 course" href="{i["course"]}/" style="--ch:{HUES.get(i["course"], 250)}" data-keys="{i["course"]}" data-track="{i["track"]}">'
                     f'<svg class="ring" viewBox="0 0 36 36" aria-hidden="true"><circle cx="18" cy="18" r="15.9"/><circle cx="18" cy="18" r="15.9" pathLength="100"/></svg>'
                     f'<h2>{html.escape(i["name"])}</h2><span class="mute">{i["lessons"]} שיעורים · <span class="pct"></span></span>'
                     f'<div class="bits">{"".join(bits)}</div></a>')
    hw_all = sorted(((i['name'], r) for i in infos for r in i['hw'] if r['status'] == 'open'), key=lambda x: x[1]['due'])
    todo = ''.join(f'<li><span>{html.escape(r["title"])}<br><span class="mute">{html.escape(n)}</span></span>'
                   f'<span class="when">{r["due"]:%d/%m ב-%H:%M}</span></li>' for n, r in hw_all)
    exams = sorted(((i['name'], e) for i in infos for e in i['exams']), key=lambda x: x[1]['date'])
    tests = ''.join(f'<li><span>{html.escape(n)}<br><span class="mute">{html.escape(e.get("kind", "בחינה"))}</span></span>'
                    f'<span class="when">{e["date"][8:10]}/{e["date"][5:7]}</span></li>' for n, e in exams[:4])
    soon = (f'<div class="tile soft w4"><h2>{html.escape(exams[0][1].get("kind", "בחינה"))} קרובה</h2><div class="big-n" data-days="{exams[0][1]["date"]}"></div>'
            f'<span data-unit class="mute">ימים · {html.escape(exams[0][0])}</span></div>') if exams else ''
    body = ('<h1 class="greet" data-hi>שלום</h1><p class="mute" style="margin:0 0 20px">מה לומדים היום?</p><div class="bento">' + ''.join(cards)
            + soon + (f'<div class="tile {"w8" if soon else "w12"}"><h2>להגיש<small>{len(hw_all)}</small></h2><ul class="rows">{todo}</ul></div>' if todo else '')
            + (f'<div class="tile w12"><h2>מבחנים</h2><ul class="rows">{tests}</ul></div>' if tests else '') + '</div>')
    names = html.escape(json.dumps({i['course']: i['name'] for i in infos}, ensure_ascii=False), quote=True)
    attrs = f' data-courses="{",".join(i["course"] for i in infos)}" data-names="{names}"'
    (tmp / 'index.html').write_text(page('הקורסים שלי', body, wide=True, attrs=attrs), encoding='utf-8')
    (tmp / 'assets').mkdir()
    for f in ASSETS.iterdir():
        shutil.copyfile(f, tmp / 'assets' / f.name)
    old = out.with_name(out.name + '.old')
    shutil.rmtree(old, ignore_errors=True)
    if out.exists():
        out.replace(old)
    tmp.replace(out)
    shutil.rmtree(old, ignore_errors=True)
    return {'courses': len(infos), 'lessons': sum(i['lessons'] for i in infos), 'url': URL}


def audit(site: Path | None = None, summaries: Path | None = None, now: datetime | None = None) -> list[str]:
    """What's broken on the built site — read off the files, no LLM (29/10, the owner: "קלוד משפר את האתר לבד": these go
    into Claude's nightly review, which fixes the builder). Only real defects; an empty list = nothing to fix."""
    from config.paths import ACTIVE_COURSES
    site = site or OUT
    now = now or datetime.now()
    out: list[str] = []
    home = site / 'index.html'
    if not home.exists():
        return ['האתר לא נבנה בכלל (data/site/index.html חסר)']
    age_h = (now.timestamp() - home.stat().st_mtime) / 3600
    if age_h > 36:
        out.append(f'האתר לא נבנה מחדש {age_h:.0f} שעות (שלב 9h בלילה נכשל?)')

    def text_of(h: str) -> str:
        h = h.replace(USD, '')                                         # a literal $ (money), already defused
        h = re.sub(r'<(script|style)\b.*?</\1>', '', h, flags=re.S)
        h = re.sub(r'<(pre|code)\b.*?</\1>', '', h, flags=re.S)       # code may hold $ / ** / | legitimately
        return html.unescape(re.sub(r'<[^>]+>', ' ', h))
    for ck in ACTIVE_COURSES:
        d = site / ck
        have = len(lessons(ck, summaries))
        built = len(list(d.glob('lesson-*.html'))) if d.exists() else 0
        if built != have:
            out.append(f'{ck}: {have} סיכומים אבל {built} דפי שיעור באתר')
        if have and not (d / 'guide' / 'index.html').exists():
            out.append(f'{ck}: מדריך הקורס (guide/) לא נבנה')
        from scripts import course_topics as ct
        rows = ct._load(d / 'topics' / 'keys.json') or []
        if ct.load_map(ck) and not rows:
            out.append(f'{ck}: יש מפת נושאים אבל מסלול הלימוד (topics/) לא נבנה')
        for f, r in ((d / 'topics' / r[1], r) for r in rows):
            t = text_of(f.read_text(encoding='utf-8')) if f.exists() else ''
            if not t:
                out.append(f'{ck}/topics: חסר הדף של "{r[3]}"')
            elif 'MATHX' in t or len(re.findall(r'(?<![\\$])\$(?!\$)', re.sub(r'\$\$.+?\$\$', '', t, flags=re.S))) % 2:
                out.append(f'{ck}/topics/{f.name}: נוסחה שבורה ב-"{r[3]}"')
        for f in sorted(d.glob('*.html')) if d.exists() else []:
            if f.name == 'all.html':                  # the lesson pages again, one after the other
                continue
            t = text_of(f.read_text(encoding='utf-8'))
            bad = []
            if 'MATHX' in t:
                bad.append('מציין מקום של נוסחה (MATHX) לא הוחזר')
            body = re.sub(r'\$\$.+?\$\$', '', t, flags=re.S)
            if len(re.findall(r'(?<![\\$])\$(?!\$)', body)) % 2:
                bad.append('מספר אי-זוגי של $ — נוסחה שלא תיסגר ב-KaTeX')
            if re.search(r'^\s*\|.*\|\s*$\n^\s*\|\s*:?-{3,}', t, re.M):
                bad.append('טבלת markdown שלא הומרה לטבלה')
            if re.search(r'(?<!\*)\*\*[^*\n]{1,80}\*\*(?!\*)', t):
                bad.append('** שלא הפך להדגשה')
            if f.name.startswith('lesson-') and len(t) < 1500:
                bad.append(f'דף שיעור כמעט ריק ({len(t)} תווים)')
            if bad:
                out.append(f'{ck}/{f.name}: ' + '; '.join(bad))
    return out[:40]


def refresh_soon() -> None:
    """Rebuild in the background (a homework item was just closed / marked) — the site never waits for the night."""
    import threading

    def run():
        try:
            build()
        except Exception as e:
            print(f'  ⚠️ site refresh: {e!r}'[:200])
    threading.Thread(target=run, daemon=True).start()


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except AttributeError:
        pass
    try:                                       # the chapters' Gemini key when run by hand
        from dotenv import load_dotenv
        load_dotenv(ROOT / '.env')
    except ImportError:
        pass
    import argparse
    ap = argparse.ArgumentParser(description='builds the study site; with --topics it also writes the topic path')
    ap.add_argument('--topics', action='store_true', help='write every topic still missing (no nightly cap); cost is printed at the end')
    ap.add_argument('--cap', type=int, default=None, help='with --topics: at most this many new topics per course')
    ap.add_argument('--retry', action='store_true', help='with --topics: try again topics that failed three times')
    ap.add_argument('--remap', action='store_true', help='with --topics: move the maps aside and derive them again (same title keeps its id)')
    ap.add_argument('--course', action='append', help='rebuild only this course (repeatable); the others keep their pages')
    a = ap.parse_args()
    opts = None
    if a.topics:
        from scripts import course_topics as _ct
        if a.remap:
            _ct.move_maps_aside(a.course)
        opts = {'ask': _topic_ask, 'cap': a.cap, 'retry': a.retry}
    since = datetime.utcnow().isoformat(timespec='seconds')
    print(build(courses=a.course, topics=opts, api_usd=None if a.topics else API_CAP_USD))   # --topics by hand: no cap
    if a.topics:
        from scripts import course_topics as _ct
        print(_ct.cost_report(since))
