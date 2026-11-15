"""Test NotebookLM with full browser_profile + headless mode + Windows user-agent."""
import sys
from pathlib import Path

PROFILE = Path.home() / '.notebooklm' / 'browser_profile'
if not PROFILE.exists():
    print(f'❌ Missing: {PROFILE}'); sys.exit(1)

from playwright.sync_api import sync_playwright
print('🌐 Launching headless Chromium with persistent profile...')

# Mimic the Windows user-agent that originally saved these cookies — Google
# binds auth to user-agent fingerprint.
WIN_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
          'AppleWebKit/537.36 (KHTML, like Gecko) '
          'Chrome/142.0.0.0 Safari/537.36')

with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE),
        headless=True,
        user_agent=WIN_UA,
        args=[
            '--no-sandbox',
            '--disable-blink-features=AutomationControlled',
            '--password-store=basic',
        ],
        ignore_default_args=['--enable-automation'],
    )
    page = context.pages[0] if context.pages else context.new_page()
    print('   ⏳ Navigating to NotebookLM...')
    page.goto('https://notebooklm.google.com', timeout=30000)
    page.wait_for_load_state('networkidle', timeout=20000)
    title = page.title()
    url = page.url
    body = page.evaluate('document.body.innerText')[:600]
    print(f'\n📄 Title: {title}')
    print(f'🔗 URL:   {url}')
    if 'accounts.google.com' in url or 'Sign in' in body:
        print('\n❌ NOT authenticated — Google redirects to sign-in')
        print('   Body preview:', body[:200])
    elif 'notebooklm.google.com' in url:
        print('\n✅ AUTHENTICATED — NotebookLM loaded!')
        # Try to find notebooks list
        notebooks_count = page.evaluate(
            "document.querySelectorAll('[data-test-id=\"project-card\"], [aria-label*=\"notebook\"]').length"
        )
        print(f'   Notebooks visible: {notebooks_count}')
    context.close()
