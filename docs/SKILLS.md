# Skills registry: tasks already codified in Python

These are deterministic tools: no LLM, or one small call on data the code already chose. Before you write a new
tool, find the closest one here and extend it. When you add or extend a tool, update its line in the same commit.
The rules for this are in `docs/SELF_IMPROVE.md`.

| Task | Tool (module.function) | LLM | Called from | Tests |
|---|---|---|---|---|
| Weather (any city, today to +6 days) | `integrations/freeweb.weather` (Open-Meteo + its geocoding) | none | `websearch.ask_web` | `test_freeweb_structured_answers_need_no_llm_19_11` |
| Exchange rates and conversion | `integrations/freeweb.fx` (Bank of Israel representative rate; open.er-api fallback) | none | `websearch.ask_web` | same |
| Shabbat times, holidays, Hebrew date | `integrations/freeweb.hebcal` (hebcal.com, Israel schedule) | none | `websearch.ask_web` | same |
| News headlines (top or a topic) | `integrations/freeweb.news` (Google News RSS he-IL) | none | `websearch.ask_web` | — |
| Opening hours of a place in Israel | `integrations/freeweb.osm_hours` (OpenStreetMap Nominatim) | none | `freeweb.search` | — |
| General web question | `integrations/freeweb.search`: DuckDuckGo, known TAU pages, Wikipedia, Google News → top pages → `rank` passages → `answer` | 1 flash-lite call, cited | `websearch.ask_web` (grounded call only as fallback) | `test_freeweb_answer_treats_pages_as_data_19_11`, `test_ask_web_python_first_grounded_only_as_fallback_19_11` |
| Shopping prices | `integrations/freeweb.zap` (zap.co.il search page) | 1 flash-lite call | `freeweb.search` | — |
| Fetching a page safely (public addresses only, size cap) | `integrations/freeweb.get`, `agent/secretary.fetch` | none | — | `test_freeweb_fetch_never_reaches_inside_the_house_19_11` |
| Schedule: day / week / next deadline / where is the lesson / what's next | `agent/fastpath.answer` (`intent` → `day_answer`, `week_answer`, `deadline_answer`, `waiting_answer`, `where_answer`, `next_answer`) | none | `core` before the mind | fastpath tests |
| A day's overview (calendar + lessons + homework + reminders) | `agent/daytools.day_overview`, `parse_day` | none | fastpath, mind tools | daytools tests |
| Real timetable with university changes (cancellations) | `agent/schedule.plan`, `cancelled`, `suffix` | none | fastpath, factcheck | schedule tests |
| Checking an answer's dates, times and rooms against the data | `agent/factcheck.check` / `apply` | none | every answer | factcheck tests |
| Hebrew time words → Israel datetime | `agent/hetime.parse`, `say` | none | reminders (`life.quick_reminder`) | `test_hetime_israel_time_and_dst_17_11` |
| Quick reminders read in code | `agent/life.quick_reminder` | none | core | `test_quick_reminder_only_when_fully_understood_17_11` |
| Lessons from my.tau → calendar | `agent/timetable` | none | cron | improve_cases |
| Office hours → calendar + reminder when free | `agent/office_hours.parse`, `sync`, `tick` | none | cron | improve_cases |
| Is this topic worth it in the exam | `agent/exam_weight` | none | core / mind | improve_cases |
| Exam plan from past exams | `agent/exam_plan` | none | core / mind | exam_plan tests |
| What to study today | `agent/study_focus` | none | weekly / on request | improve_cases |
| What the owner still owes per course | `agent/obligations` | none | mind tool, nudges | obligations tests |
| Open items, and closing them in his own words | `agent/open_items`, `agent/closure` | none | core | closure tests |
| Weekly message (week ahead, health, cost) | `agent/weekly` | none | cron | improve_cases |
| What keeps costing several model rounds | `agent/improve.codify_candidates` | none | daily precheck | `test_improve_codify_candidates_19_11` |
