"""
integrations/websearch.py — up-to-date answers from the web (Gemini + Google Search grounding).

For questions outside the courses and the calendar: weather, news, opening hours, prices, "when does
course registration open". Cheap (a regular Gemini call; grounding has a large free daily quota),
answers cite up to 3 sources. Prices are estimates — the bot never books or pays.
"""
from __future__ import annotations

import os
import re
from datetime import datetime

MODEL = 'gemini-3.8-flash'


SHOP_RX = re.compile(r'לקנות|לקנייה|קנייה|הכי משתלם|הכי זול|מחיר|כמה עולה|איפה כדאי לקנות|מבצע|דיל')
FALLBACK = 'gemini-2.5-flash'      # 3.8-flash sometimes skips the search and answers from "seasonal averages"


def _grounded(resp) -> list:
    try:
        return [ch for ch in (resp.candidates[0].grounding_metadata.grounding_chunks or []) if ch.web and ch.web.uri]
    except (AttributeError, IndexError, TypeError):
        return []


def ask_web(question: str, who: str = 'המשתמש') -> str:
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
    resp = None
    for model in (MODEL, FALLBACK):
        try:
            resp = client.models.generate_content(model=model, contents=prompt, config=cfg)
        except Exception:
            continue
        log_gemini(resp, model, op='agent.web_search')
        if _grounded(resp):
            break
    if resp is None:
        return 'החיפוש ברשת לא זמין כרגע — נסה שוב עוד מעט.'
    text = (resp.text or '').strip() or 'לא מצאתי תשובה עדכנית.'
    # the URIs are long Google redirect links — on WhatsApp the site names are enough
    sites = list(dict.fromkeys(ch.web.title or 'מקור' for ch in _grounded(resp)))[:3]
    return text + (f"\n\n🔗 מקורות: {', '.join(sites)}" if sites else '')
