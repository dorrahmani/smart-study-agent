"""Push newly-recovered summaries into NotebookLM — mirrors daily_full_update
step 7 (_nlm_sync). Free (uses the NotebookLM web session, no LLM cost)."""
import sys, os
sys.path.insert(0, '/home/ubuntu/smart_study_agent')
os.chdir('/home/ubuntu/smart_study_agent')
from utils.env import _load_env_dotfile
_load_env_dotfile()

from generators import notebooklm_agent as nlm

agent = nlm.NotebookLMAgent()
if not nlm.NotebookLMAgent.is_authenticated():
    print('NOT AUTHENTICATED'); print('DONE'); sys.exit(0)

idx = nlm._load_notebooks_index()
total_added = 0
for ck in idx.keys():
    for scope in nlm.all_scopes_for_course(ck):
        if scope == 'lectures_only':
            pdfs = nlm.list_summary_pdfs(ck, 'lectures'); srts = nlm.list_srt_files(ck, 'lectures'); moodle = []
        elif scope == 'tutorials_only':
            pdfs = nlm.list_summary_pdfs(ck, 'tutorials'); srts = nlm.list_srt_files(ck, 'tutorials'); moodle = []
        else:
            pdfs = nlm.list_summary_pdfs(ck, 'lectures') + nlm.list_summary_pdfs(ck, 'tutorials')
            srts = nlm.list_srt_files(ck, 'all'); moodle = nlm.list_moodle_pdfs(ck)
        try:
            r = agent.sync_sources(course_key=ck, scope=scope,
                                   summary_pdfs=pdfs, moodle_pdfs=moodle, srt_files=srts)
            added = len(r.get('added', []))
            total_added += added
            if added:
                print(f'  + {ck}/{scope}: +{added}', flush=True)
        except Exception as e:
            print(f'  WARN {ck}/{scope}: {e}', flush=True)
print(f'TOTAL ADDED: {total_added}')
print('DONE')
