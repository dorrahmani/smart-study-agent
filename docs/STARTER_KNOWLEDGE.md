# Experience from the user's system — what broke and how it was fixed (to help with setting up tau-study-starter)

> This file was written for an earlier, simpler template (tau-study-starter, which ran in GitHub Actions). The lessons about Panopto, Moodle, Gemini and NotebookLM are true here as well, but setting up this project is `SETUP.md` and `docs/FAQ.md`, not GitHub Actions.

The user's system ran a whole semester (2026) on the same components: university login, Panopto, Moodle, Gemini, NotebookLM, WhatsApp. These are the problems that actually happened and the solutions. There is no personal information here.

## University login (SSO)
- The TAU form (NIDP): the fields #Ecom_User_ID (username), #Ecom_User_Pid (ID number), #Ecom_Password, and the button #loginButton. After the click you need to wait about 8 seconds for the redirect chain.
- The same login opens Panopto, Moodle and my.tau. A wrong username or ID number, or an additional verification that the university requires, returns "login failed". What to check: the secrets TAU_USERNAME, TAU_ID and TAU_PASSWORD (with no extra spaces).
- The university may block logins from GitHub's servers, which are abroad. That is why the first step is setup → feasibility. If it fails, run from your computer (README → "running on a computer") with Task Scheduler.

## Panopto
- The folder name of each group contains "(<an 8-digit course number><a 2-digit group>-<year>-<semester>)", for example "(0368200201-2026-1)". Semester A starts in October and belongs to the year in which it starts.
- A folder appears only after the first recording has been uploaded to it. Before the semester starts it is normal that no folder was found ("no recordings yet").
- Do not filter recordings by a word in the title. For the user the titles changed to "שיעור N" ("lesson N") and the filter threw out all the lectures. Match by folder, date and time (±90 minutes).
- Panopto captions are sometimes Cyrillic gibberish instead of Hebrew. Check that at least 20% of the letters are Hebrew, and if not, transcribe.
- Empty recordings of 0-5 minutes exist. Transcribing one returned 60 thousand tokens of "אוקיי" ("okay") (it cost $0.15), so they are skipped.
- Times: Panopto returns UTC. Israel's clock changes on 25/10, so convert with zoneinfo and not with a fixed offset.

## Gemini
- Transcription: gemini-2.5-flash without thinking returned empty (RECITATION), and with thinking invented a transcript. gemini-3.8-flash with low thinking transcribed Hebrew correctly. This is the default.
- "Thinking" tokens are billed and count inside max_output_tokens. For the user the real invoice came out 3-5 times what was logged, until they were counted. For JSON answers you need a max_output_tokens of 2000 or more, otherwise they are cut off.
- "Cannot send a request, as the client has been closed": the genai client was created inside an expression and closed in the middle of the call. You need to keep it in a variable.
- Actual costs: a lecture with captions (25 minutes) cost about $0.08. Without captions, 90 minutes, about $0.40.
- Key: aistudio.google.com/apikey. You must set a budget in Google Cloud Billing, in addition to the cap in settings.yaml.

## Moodle
- The /my/ page loads the course list in JavaScript, so in a plain request you do not see courses. Use the AJAX call core_course_get_enrolled_courses_by_timeline_classification (with the sesskey from the page).
- The course id in Moodle is the course number without the leading zeros plus 2 group digits, for example 368200201.
- Before the semester starts most courses show a "notice" page (not yet open to students). This is normal, and they will open by themselves.
- Assignments: the due date is in Hebrew ("יום ראשון, 17 מאי 2026, 23:59", i.e. "Sunday, 17 May 2026, 23:59"). "הוגש להערכה" ("submitted for grading") means the assignment was already submitted.

## my.tau (the timetable)
- You enter through Moodle, then to the personal page, click "ליומן המלא" ("to the full calendar"), and read the JSON that the page itself loads (screenservices).
- Every class has a course number, group, type (lecture/tutorial), date, time, building and room. There are also "events" with course number 00000000, for example the opening of the year, and those are filtered out.
- Sometimes the site does not show all the groups. For the user the probability tutorial did not appear at all.

## NotebookLM
- There is no official API. The notebooklm-py library connects with a regular Google login (notebooklm login on the computer).
- A login copied from the computer to the server was logged out by Google within a day, probably because it saw the same login from two places. On the server only a login done on the server itself (through VNC) worked.
- That is why a disconnect is expected in GitHub Actions (servers that change). The stable way is `python -m src.notebooklm` from the computer (git pull first).
- A "CSRF token not found" error means the login expired, and you need notebooklm login again.

## GitHub
- The summaries and materials are saved in the repo, so the repo must be private (Use this template → Private).
- A push is rejected with "email privacy restrictions": you need to commit with GitHub's noreply address (Settings → Emails).
- The Actions cron is in UTC. 19:30 UTC is about 21:30-22:30 in Israel.
- A workflow that did not run: check that Actions is enabled (in a copy from a template you sometimes need to click "Enable"), and that the permissions allow write (Settings → Actions → Workflow permissions).

## Rules the user learned the hard way
- Backup sync: rclone copy and not rclone sync, because sync deleted files that had not yet been uploaded.
- YAML with Hebrew: only yaml.safe_dump. Quotation marks like in חדו"א (calculus) break manual writing.
- Word in Hebrew: alignment=LEFT is displayed in practice on the right. Check by coordinates, not by eye.
- Every bug that is discovered gets an automated test, so that it does not come back.
- A hard monthly cost cap that stops everything, and not only an alert.
