# Costs: staying around $15–30 a month

<div dir="rtl">

> **בקצרה:** עוזר אישי בלבד (בלי לימודים) עולה בערך $3–10 לחודש. עם שכבת הלימודים (סיכומי שיעורים, אתר קורס) בערך $15–30. השרת חינמי (Oracle Always Free). יש תקרה חודשית קשיחה: מעליה כל קריאה בתשלום נעצרת, והמערכת עוברת למצב חירום שעונה מהנתונים בלבד.

</div>

Every price below is from the usage ledger of a real install (`data/usage_ledger.json`, the dashboard's cost page) in the autumn 2026 semester, on Gemini 2.5 Flash / Flash-Lite. The Google invoice was higher than the ledger until every call was logged with its thinking tokens, so **trust the invoice and set a Google Cloud budget alert** as well.

## What one month looks like

| | Assistant only (`SSA_STUDY=0`) | With the study layer (`SSA_STUDY=1`) |
|---|---|---|
| Chat with the mind (≈30 messages a day) | $2–8 | $2–8 |
| Fast path, reminders, calendar, catch-up, approvals, undo | $0 | $0 |
| Web questions (Python first) | ≈$0–1 | ≈$0–1 |
| Lecture summaries (≈4 courses, ≈10 lectures a week) | — | $3–20 (captions vs. paid transcription) |
| Course site, primer, NotebookLM routing | — | $1–3 |
| Server (Oracle Always Free) | $0 | $0 |
| **Total** | **≈$3–10** | **≈$15–30** |

## The levers, biggest first

### 1. Panopto captions first, then the server's own CPU, then the paid API
Transcription is the most expensive single item: a lecture without captions cost ≈$0.40–0.50 with the paid transcriber, against ≈$0.08 for a summary made from captions.
- **Captions first** (`collectors/panopto_collector.py`). A recording without captions waits up to 48 hours for them and is re-checked every run. The exception is an exam or quiz of that course within 2 days.
- **Local transcription, $0** (`collectors/local_asr.py`, `scripts/local_asr_worker.py`, `SSA_LOCAL_ASR=1`, the default). This uses faster-whisper with a Hebrew fine-tune on the server's CPU, under `nice 19` / `ionice idle`, one lecture at a time, with a time budget. A 2-hour lecture takes about an hour on 4 ARM cores. It needs its own venv (`SSA_ASR_PYTHON`) and model folder (`SSA_ASR_MODEL`). If that setup is missing, or a job fails, it falls back to the next step.
- **Paid API last** (`SSA_TRANSCRIBE_MODEL`, default gpt-4o-transcribe, then whisper-1). It still obeys the budget lock. `SMART_STUDY_DISABLE_WHISPER=1` turns paid transcription off completely.

### 2. Answer in code before asking a model
- **Fast path** (`agent/fastpath.py`). "What do I have tomorrow", "when is the quiz", "what's waiting for me" are answered from the stores for $0 in under a second, instead of a ≈$0.015 mind turn.
- **Deterministic handlers.** Buttons, "ביטלתי", "מה פספסתי", "תבטל את מה שעשית", and reminders with a clear time (`agent/hetime.py`) never reach a model.
- **Web in Python first** (`integrations/freeweb.py`). A plain search plus reading the page costs $0, or ≈$0.0005 with a Flash-Lite pass. The grounded search fallback (≈$0.04) runs only when that fails.
- **Notebook first for course questions** (`agent/nlm_ask.py`). A course question goes to the course's NotebookLM notebook ($0) before any paid retrieval.

### 3. Prompt caching order
The mind's system prompt is built with the **static blocks first**: rules, tools, domain lines. Only then come the parts that change every turn: the clock, open questions, the conversation (`agent/mind.system_prompt`). Gemini caches the shared prefix implicitly, and cached input tokens are billed at a fraction. The ledger records them separately (`cached_tokens`), so you can see the saving on the cost page. Keep this order if you add a block.

### 4. Hard caps
| Setting | Default | What it does |
|---|---|---|
| `SSA_MONTH_CAP_USD` | 50 | The month's hard cap. Above it `data/budget_lock.json` stops every paid call until the 1st. |
| `config/budget_caps.json` | — | A different cap per month (for example lower in the exam month). It never sets a cap below what the month already spent. |
| `SSA_BRAIN_MAX_COST` / `SSA_BRAIN_DAY_CAP` | $0.10 / $1 | One message / one day of mind turns. |
| `SSA_SITE_API_CAP` | $0.10 | The course site's nightly build. |
| Proactive and question caps | ≤3 nudges, ≤5 questions a day | Fewer messages also means fewer model calls. |

When a cap is hit, **emergency mode** (`agent/emergency.py`) keeps the bot useful: data answers from the calendar, tasks and timetable, plus one honest line about what it can't do now.

### 5. The subscription option (optional, off by default)
`utils/gemini_sub.py` can route some text work through a personal Gemini subscription instead of the paid API. The work it can take is plain chat (`agent/chat_sub.py`), lecture summaries and formula checks. **It is off unless you set `SSA_SUBSCRIPTION=1`**, and it is worth knowing why:
- It uses a CLI login to *your* personal subscription, so the subscription's own terms and limits apply. You are responsible for whether that use is allowed.
- It is slower (seconds to minutes) and can be unavailable. Every path falls back to the API.
- **Third-party text never goes there.** Mail, groups, web pages, files and anything a contact wrote are `api_only`, because the subscription runs an agent-like tool. Its replies must pass a check before they are used.

Settings: `SSA_SUBSCRIPTION`, `SSA_BOT_VIA_GEMINI_SUB`, `SSA_SUMMARY_VIA_SUB`, `SSA_FORMULA_VIA_SUB` (see `.env.example`, section 4c).

## Where to see it
- The dashboard's cost and system pages show the ledger by feature and model, with cached tokens, the breaker and the lock.
- `python scripts/budget_check.py` prints the month so far.
- The weekly bot report (`deploy/crontab.assistant.txt`) includes the week's spend.
- The invoice: set a **budget alert in Google Cloud Billing** at about 1.5× what you expect.
