#!/usr/bin/env python3
"""
Dry Run - בודק מה המערכת תוריד בלי להוריד באמת
שימושי לבדיקת הסינון!
"""

import os
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright
import yaml
from typing import List, Dict
from dotenv import load_dotenv

load_dotenv()


class DryRunChecker:
    def __init__(self, config_path: str = "config/courses.yaml"):
        with open(config_path, 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)
        
        self.base_url = self.config['settings']['panopto']['base_url']
        self.username = os.getenv('TAU_USERNAME')
        self.password = os.getenv('TAU_PASSWORD')
    
    def login(self, page):
        """התחברות ל-Panopto"""
        print("🔐 מתחבר ל-Panopto...")
        
        page.goto(f"{self.base_url}/Panopto/Pages/Auth/Login.aspx")
        page.click("text=Sign in with your organization")
        page.wait_for_load_state("networkidle")
        
        page.fill('input[name="username"]', self.username)
        page.fill('input[name="password"]', self.password)
        page.click('button[type="submit"]')
        page.wait_for_url("**/Home**", timeout=15000)
        
        print("✅ התחברות הצליחה!")
    
    def check_course(self, course_key: str):
        """בודק מה יורד עבור קורס"""
        course = self.config['courses'][course_key]
        
        if not course['sources']['panopto']['enabled']:
            print(f"⏭️  Panopto לא מופעל עבור {course['name']}")
            return
        
        folder_id = course['sources']['panopto']['folder_id']
        keywords = course['sources']['panopto'].get('keywords', [])
        
        if folder_id == "TO_BE_FILLED":
            print(f"❌ יש למלא folder_id!")
            return
        
        print(f"\n{'='*70}")
        print(f"📚 קורס: {course['name']}")
        print(f"🔑 Keywords לסינון: {keywords if keywords else '❌ לא מוגדרים - יורד הכל!'}")
        print(f"{'='*70}\n")
        
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)  # headless=False כדי לראות
            context = browser.new_context()
            page = context.new_page()
            
            try:
                self.login(page)
                
                # לך לתיקייה
                folder_url = f"{self.base_url}/Panopto/Pages/Sessions/List.aspx#folderID=\"{folder_id}\""
                page.goto(folder_url)
                page.wait_for_load_state("networkidle")
                
                import time
                time.sleep(3)
                
                # חלץ כל הסרטונים
                video_elements = page.query_selector_all('.session-row')
                
                print(f"📊 נמצאו {len(video_elements)} סרטונים בתיקייה\n")
                
                included = []
                excluded = []
                
                for i, element in enumerate(video_elements, 1):
                    try:
                        title = element.query_selector('.detail-title').inner_text()
                        date = element.query_selector('.detail-date').inner_text()
                        
                        # בדוק התאמה
                        matches = []
                        if keywords:
                            for keyword in keywords:
                                if keyword.lower() in title.lower():
                                    matches.append(keyword)
                        
                        if matches or not keywords:
                            included.append({
                                'title': title,
                                'date': date,
                                'matches': matches
                            })
                            status = "✅ יורד"
                            reason = f"(מתאים ל: {', '.join(matches)})" if matches else "(אין סינון)"
                        else:
                            excluded.append({
                                'title': title,
                                'date': date
                            })
                            status = "❌ לא יורד"
                            reason = "(לא מתאים לאף keyword)"
                        
                        print(f"{i}. {status} {title}")
                        print(f"   📅 {date}")
                        print(f"   {reason}")
                        print()
                        
                    except Exception as e:
                        print(f"⚠️  שגיאה בסרטון {i}: {e}\n")
                
                # סיכום
                print("\n" + "="*70)
                print("📊 סיכום:")
                print("="*70)
                print(f"✅ יורדו: {len(included)} סרטונים")
                print(f"❌ לא יורדו: {len(excluded)} סרטונים")
                
                if not keywords:
                    print("\n⚠️  אזהרה: אין keywords מוגדרים!")
                    print("💡 הוסף keywords ב-config/courses.yaml כדי לסנן:")
                    print(f"""
    panopto:
      keywords: ["קבוצה {course['teaching_staff']['lecture']['group']}", "{course['teaching_staff']['lecture']['instructors'][0] if isinstance(course['teaching_staff']['lecture'].get('instructors'), list) else course['teaching_staff']['lecture'].get('instructor', '')}"]
                    """)
                
            finally:
                input("\n🔍 לחץ Enter כדי לסגור את הדפדפן...")
                browser.close()


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='Dry Run - בודק מה יורד בלי להוריד')
    parser.add_argument('--course', type=str, required=True, help='מפתח הקורס')
    
    args = parser.parse_args()
    
    checker = DryRunChecker()
    checker.check_course(args.course)


if __name__ == "__main__":
    main()
