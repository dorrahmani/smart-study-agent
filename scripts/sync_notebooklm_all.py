"""
scripts/sync_notebooklm_all.py
==============================
מעלה את כל הסיכומים+SRTs הקיימים לכל המחברות של NotebookLM.
- חוצה כל קורס × כל scope (all / lectures_only / tutorials_only)
- sync_sources() אוטומטית מדלג על מה שכבר הועלה (לפי sources_synced)
- אפס סיכוי לכפילויות
"""
import sys, os, traceback
sys.path.insert(0, '.')
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass
sys.stdout = open(sys.stdout.fileno(), 'w', encoding='utf-8', errors='replace', buffering=1)
sys.stderr = open(sys.stderr.fileno(), 'w', encoding='utf-8', errors='replace', buffering=1)

from utils.env import _load_env_dotfile
_load_env_dotfile()

from generators import notebooklm_agent as nlm

def main():
    print('🔐 בודק קובץ session...', flush=True)
    agent = nlm.NotebookLMAgent()
    # is_authenticated() שבור ב-SDK 0.3.4 (קורא ל-list_notebooks הישן). מדלגים.
    age = agent.auth_age_hours()
    if age == float('inf'):
        print('❌ לא מצאתי storage_state.json — הרץ: python -m notebooklm login')
        return
    print(f'✅ session נמצא (גיל: {age:.1f}h)\n')

    # כל הקורסים מה-config
    idx = nlm._load_notebooks_index()
    course_keys = list(idx.keys())
    print(f'📚 קורסים: {course_keys}\n')

    total_added = 0
    total_skipped = 0
    for ck in course_keys:
        scopes = nlm.all_scopes_for_course(ck)
        print(f'\n══════ {ck} ({len(scopes)} scopes) ══════', flush=True)
        for scope in scopes:
            # הכן רשימות לפי scope
            if scope == 'lectures_only':
                summary_pdfs = nlm.list_summary_pdfs(ck, 'lectures')
                srts = nlm.list_srt_files(ck, 'lectures')
                moodle = []  # לא כוללים moodle ב-lectures_only
            elif scope == 'tutorials_only':
                summary_pdfs = nlm.list_summary_pdfs(ck, 'tutorials')
                srts = nlm.list_srt_files(ck, 'tutorials')
                moodle = []
            else:  # 'all'
                summary_pdfs = (
                    nlm.list_summary_pdfs(ck, 'lectures')
                    + nlm.list_summary_pdfs(ck, 'tutorials')
                )
                srts = nlm.list_srt_files(ck, 'all')
                moodle = nlm.list_moodle_pdfs(ck)

            print(f'  📁 [{scope}] PDFs={len(summary_pdfs)} Moodle={len(moodle)} SRTs={len(srts)}', flush=True)
            try:
                result = agent.sync_sources(
                    course_key=ck, scope=scope,
                    summary_pdfs=summary_pdfs,
                    moodle_pdfs=moodle,
                    srt_files=srts,
                    text_files=nlm.list_moodle_texts(ck) if scope == 'all' else None,
                )
                added = result.get('added', [])
                skipped = result.get('skipped', [])
                total_added += len(added)
                total_skipped += len(skipped)
                if added:
                    print(f'    ✅ הוסיף {len(added)}:')
                    for a in added:
                        print(f'      + {a}')
                if skipped:
                    print(f'    ⏭️  דילג {len(skipped)} (כבר קיימים או שגיאות)')
                    # הראה רק שגיאות
                    errs = [s for s in skipped if 'ERROR' in s or 'EMPTY' in s]
                    for e in errs[:5]:
                        print(f'      ⚠️  {e}')
            except Exception as e:
                print(f'    ❌ נכשל: {e}')
                traceback.print_exc()

    print(f'\n══════ סיכום ══════')
    print(f'✅ סה"כ הועלה: {total_added}')
    print(f'⏭️  סה"כ דולג: {total_skipped}')

if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f'💥 קריסה: {e}')
        traceback.print_exc()
