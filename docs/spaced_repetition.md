# Spaced Repetition (Obsidian)

`Smart Study Agent` מייצר חידונים וכרטיסיות בפורמט שמתאים לפלאגין
[`obsidian-spaced-repetition`](https://github.com/st3v3nmw/obsidian-spaced-repetition).
הפלאגין מזהה כרטיסיות בתוך תווי-Obsidian רגילים ומריץ עליהן לוח חזרות
מבוסס SM-2 (אלגוריתם Anki) — ללא צורך לייצא ל-Anki.

## התקנה

1. בתוך Obsidian: `Settings` → `Community plugins` → `Browse`.
2. חפש **Spaced Repetition** (מאת `st3v3nmw`).
3. `Install` ואז `Enable`.
4. `Settings` → `Spaced Repetition` — ודא ש-`Tags to review` כולל `#flashcards`
   (זוהי ברירת המחדל).

> אין צורך לעשות את זה אוטומטית — הפלאגין מתקין את עצמו פעם אחת ב-vault המקומי.

## כיצד לחזור על כרטיסיות

- `Ctrl+P` (או `Cmd+P` ב-macOS) → `Spaced Repetition: Open Flashcard Review`.
- הפלאגין יסרוק את כל הכותרות שמכילות `#flashcards` ויפתח חזרה יומית.
- ספציפית לקורס אחד: בחר `Review flashcards in note` או היכנס לחפיסה
  `#flashcards/<course_key>` (למשל `#flashcards/linear_algebra_2`).

## פורמט הכרטיסיות שאנחנו פולטים

ב-`exporters/notebooklm_obsidian.py`, כל פתק `quiz` או `flashcard` מקבל:

- `tags: [..., flashcards]` ב-frontmatter — כדי שהפלאגין יסרוק אותו.
- תג חפיסה לקורס: `#flashcards/<course_key>` בתוך הגוף.
- כרטיסיות בשני וריאנטים:

  **שורה אחת** (קצר):

  ```
  שאלה::תשובה
  ```

  **רב-שורות** (ארוך / שאלה אמריקאית):

  ```
  שאלה (יכולה להיות על מספר שורות)
  ?
  תשובה (יכולה לכלול אופציות, הסבר, רשימות)
  ```

הפלאגין מזהה את שניהם אוטומטית — אין צורך בעטיפה נוספת.

## אימות מהיר

לאחר התקנת הפלאגין, פתח כל פתק חידון ב-`🎓 NotebookLM/<course>/`
ואמור לראות את שורות ה-`::` הופכות לכרטיסיות לחיצות בעמודה הימנית.
