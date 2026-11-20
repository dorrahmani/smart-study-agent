# Spaced Repetition (Obsidian)

`Smart Study Agent` produces quizzes and flashcards in a format that suits the
[`obsidian-spaced-repetition`](https://github.com/st3v3nmw/obsidian-spaced-repetition) plugin.
The plugin recognizes flashcards inside ordinary Obsidian text and runs a review schedule on them
based on SM-2 (the Anki algorithm) — with no need to export to Anki.

## Installation

1. Inside Obsidian: `Settings` → `Community plugins` → `Browse`.
2. Search for **Spaced Repetition** (by `st3v3nmw`).
3. `Install` and then `Enable`.
4. `Settings` → `Spaced Repetition` — make sure `Tags to review` includes `#flashcards`
   (this is the default).

> There is no need to do this automatically — the plugin installs itself once in the local vault.

## How to review flashcards

- `Ctrl+P` (or `Cmd+P` on macOS) → `Spaced Repetition: Open Flashcard Review`.
- The plugin scans all the headings that contain `#flashcards` and opens a daily review.
- For one course specifically: choose `Review flashcards in note` or enter the deck
  `#flashcards/<course_key>` (for example `#flashcards/linear_algebra_2`).

## The flashcard format we emit

In `exporters/notebooklm_obsidian.py`, every `quiz` or `flashcard` note gets:

- `tags: [..., flashcards]` in the frontmatter — so that the plugin scans it.
- A deck tag for the course: `#flashcards/<course_key>` inside the body.
- Flashcards in two variants:

  **A single line** (short):

  ```
  question::answer
  ```

  **Multi-line** (long / a multiple-choice question):

  ```
  question (may span several lines)
  ?
  answer (may include options, an explanation, lists)
  ```

The plugin recognizes both automatically — no additional wrapper is needed.

## Quick verification

After installing the plugin, open any quiz note in `🎓 NotebookLM/<course>/`
and you should see the `::` lines turn into clickable flashcards in the right column.
