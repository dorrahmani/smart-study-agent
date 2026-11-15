"""Export a fresh storage_state.json from the authenticated browser_profile."""
from pathlib import Path
from playwright.sync_api import sync_playwright

# 14/10/26: the paths notebooklm-py ≥0.8 uses (profiles/default/…); the server signs in in its own profile
# (scripts/vnc_notebooklm_login.sh) instead of a session copied from the PC, which Google dropped within a day.
PROFILE = Path.home() / '.notebooklm' / 'profiles' / 'default' / 'browser_profile'
STORAGE = Path.home() / '.notebooklm' / 'profiles' / 'default' / 'storage_state.json'

WIN_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
          'AppleWebKit/537.36 (KHTML, like Gecko) '
          'Chrome/142.0.0.0 Safari/537.36')

with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE),
        headless=True,
        user_agent=WIN_UA,
        args=['--no-sandbox', '--disable-blink-features=AutomationControlled'],
        ignore_default_args=['--enable-automation'],
    )
    page = context.pages[0] if context.pages else context.new_page()
    page.goto('https://notebooklm.google.com', timeout=30000)
    page.wait_for_load_state('networkidle', timeout=20000)
    # Save state
    context.storage_state(path=str(STORAGE))
    print(f'✅ Exported to: {STORAGE}')
    print(f'   Size: {STORAGE.stat().st_size} bytes')
    context.close()
