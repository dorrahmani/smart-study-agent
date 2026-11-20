"""
integrations/websearch.py — up-to-date answers from the web.

19/11: Python first (integrations/freeweb.py). Weather, exchange rates, Shabbat times / holidays / the Hebrew date and
news come from free keyless APIs with no LLM at all; general questions are searched without a key, the top pages are
read in Python and ONE flash-lite call answers from their passages with [n] citations; prices come from zap.co.il's
search page. The grounded call below (Gemini + Google Search, ~$0.04) is now only the fallback, when the Python path
found nothing. Prices are estimates — the bot never books or pays.
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime

MODEL = 'gemini-3.8-flash'
GROUNDING_FEE = 0.035                 # USD per grounded prompt past the free quota: the ledger's tokens don't include it


SHOP_RX = re.compile(r'לקנות|לקנייה|קנייה|הכי משתלם|הכי זול|מחיר|כמה עולה|איפה כדאי לקנות|מבצע|דיל')
FALLBACK = 'gemini-2.5-flash'      # 3.8-flash sometimes skips the search and answers from "seasonal averages"


def _grounded(resp) -> list:
    try:
        return [ch for ch in (resp.candidates[0].grounding_metadata.grounding_chunks or []) if ch.web and ch.web.uri]
    except (AttributeError, IndexError, TypeError):
        return []


def ask_grounded(question: str, who: str = 'המשתמש') -> tuple[str, float]:
    """The old way, kept as the fallback: one Gemini call with Google Search grounding. → (text, estimated cost)."""
    import google.genai as genai
    from google.genai import types as T
    from utils.usage_tracker import log_gemini
    client = genai.Client(api_key=os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY'))
    now = datetime.now().strftime('%d/%m/%Y %H:%M')
    shop = bool(SHOP_RX.search(question))
    extra = ('זו בקשת קנייה: חפש מחירים עדכניים בישראל (zap.co.il, אתרי החנויות) והשווה עד 3 אפשרויות — לכל אחת: '
             'מחיר, חנות, ומה כלול (אחריות יבואן רשמי/מקביל). אם מדובר באילת — באילת אין מע"מ, אז המחיר נמוך בערך '
             'ב-15% מהמחיר עם מע"מ; ציין את זה ואם יש חנויות באילת. בסוף: המלצה אחת קצרה. ' if shop else '')
    prompt = (f'עכשיו {now}, ישראל. חובה לחפש בגוגל לפני שאתה עונה — אל תענה מהזיכרון או מממוצעים עונתיים; '
              f'תן את הנתונים העדכניים שמצאת (מזג אוויר: לפי העיר ששאלו, ואם לא צוינה — תל אביב). {extra}'
              f'ענה ל{who} בעברית, קצר ומעשי (עד {1100 if shop else 700} תווים), לוואטסאפ: בלי LaTeX, *מודגש* לכותרות. '
              f'מחיר או זמינות — כתוב שזו הערכה ומאיפה לבדוק. השאלה: {question}')
    cfg = T.GenerateContentConfig(tools=[T.Tool(google_search=T.GoogleSearch())], temperature=0.3,
                                  max_output_tokens=2000)
    resp, cost = None, 0.0
    for model in (MODEL, FALLBACK):
        try:
            resp = client.models.generate_content(model=model, contents=prompt, config=cfg)
        except Exception:
            continue
        cost += float((log_gemini(resp, model, op='agent.web_search') or {}).get('cost_usd') or 0) + GROUNDING_FEE
        if _grounded(resp):
            break
    if resp is None:
        return 'החיפוש ברשת לא זמין כרגע — נסה שוב עוד מעט.', cost
    text = (resp.text or '').strip() or 'לא מצאתי תשובה עדכנית.'
    # the URIs are long Google redirect links — on WhatsApp the site names are enough
    sites = list(dict.fromkeys(ch.web.title or 'מקור' for ch in _grounded(resp)))[:3]
    return text + (f"\n\n🔗 מקורות: {', '.join(sites)}" if sites else ''), cost


def ask_web(question: str, who: str = 'המשתמש') -> str:
    """Python first; the grounded call only when it found nothing. One row per question in data/websearch_log.jsonl."""
    from integrations import freeweb
    t0 = time.monotonic()
    try:
        r = freeweb.search(question, who)
    except Exception as e:
        print(f'  ⚠️ freeweb: {e!r}'[:200])
        r = {'route': '?', 'path': 'python', 'cost': 0.0, 'sources': [], 'text': None}
    row = {'q': question.split('\n')[0][:120], 'route': r.get('route'), 'path': r.get('path'), 'sources': r.get('sources'),
           'cost': round(float(r.get('cost') or 0), 5)}
    if r.get('text'):
        freeweb.log(row | {'sec': round(time.monotonic() - t0, 1)})
        return r['text']
    text, cost = ask_grounded(question, who)
    freeweb.log(row | {'path': 'grounded', 'python_cost': row['cost'], 'cost': round(row['cost'] + cost, 5),
                       'sec': round(time.monotonic() - t0, 1)})
    return text
