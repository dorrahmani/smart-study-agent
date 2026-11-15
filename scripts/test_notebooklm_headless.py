"""Verify NotebookLM auth works on VM via headless Playwright + storage_state."""
import json, sys
from pathlib import Path

STORAGE = Path.home() / '.notebooklm' / 'storage_state.json'
if not STORAGE.exists():
    print(f'❌ Missing: {STORAGE}'); sys.exit(1)

# Show what's in the storage state (count cookies, no values)
data = json.loads(STORAGE.read_text(encoding='utf-8'))
cookies = data.get('cookies', [])
origins = data.get('origins', [])
google_cookies = [c for c in cookies if 'google' in c.get('domain', '').lower()]
print(f'📦 storage_state: {len(cookies)} cookies, {len(origins)} origins')
print(f'   Google cookies: {len(google_cookies)}')

from playwright.sync_api import sync_playwright
print('\n🌐 Launching headless Chromium...')
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
    context = browser.new_context(storage_state=str(STORAGE))
    page = context.new_page()
    print('   ⏳ Navigating to NotebookLM...')
    page.goto('https://notebooklm.google.com', timeout=30000)
    page.wait_for_load_state('networkidle', timeout=20000)
    title = page.title()
    url = page.url
    print(f'\n📄 Title: {title}')
    print(f'🔗 URL:   {url}')
    # Check for sign-in indicators
    body_text = page.evaluate('document.body.innerText')[:500]
    if 'Sign in' in body_text or 'התחברות' in body_text or 'accounts.google.com' in url:
        print('\n❌ NOT authenticated — page shows sign-in')
    elif 'notebooklm.google.com' in url and 'NotebookLM' in title:
        print('\n✅ Authenticated — NotebookLM loaded successfully!')
    else:
        print(f'\n⚠️  Unknown state. First 500 chars of body:\n{body_text}')
    browser.close()
