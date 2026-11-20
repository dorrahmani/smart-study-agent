"""
Gmail Sender - sends summaries by email
"""

import os
import sys
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except AttributeError:
    pass
import io
import json
import base64

from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from typing import Optional
from datetime import datetime
import pickle


class GmailSender:
    SCOPES = ['https://www.googleapis.com/auth/gmail.send']
    
    def __init__(self):
        self.creds = None
        self.service = None
        self._authenticate()
    
    def _authenticate(self):
        """Authenticates with the Gmail API"""
        
        # Check whether a saved token exists
        token_path = Path('config/gmail_token.pickle')
        
        if token_path.exists():
            with open(token_path, 'rb') as token:
                self.creds = pickle.load(token)
        
        # If there are no valid credentials, log in again
        if not self.creds or not self.creds.valid:
            if self.creds and self.creds.expired and self.creds.refresh_token:
                self.creds.refresh(Request())
            else:
                # Read credentials from an environment variable
                credentials_json = os.getenv('GMAIL_CREDENTIALS_JSON')
                
                if not credentials_json:
                    raise ValueError("GMAIL_CREDENTIALS_JSON לא מוגדר!")
                
                # Save temporarily to a file
                temp_creds_path = Path('config/credentials.json')
                temp_creds_path.parent.mkdir(exist_ok=True)
                
                with open(temp_creds_path, 'w') as f:
                    f.write(credentials_json)
                
                # Run the OAuth flow
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(temp_creds_path),
                    self.SCOPES
                )
                
                self.creds = flow.run_local_server(port=0)
                
                # Delete the temporary file
                temp_creds_path.unlink()
            
            # Save the token for future use
            with open(token_path, 'wb') as token:
                pickle.dump(self.creds, token)
        
        # Create the service
        self.service = build('gmail', 'v1', credentials=self.creds)
        print("✅ התחברות ל-Gmail הצליחה")
    
    def create_message_with_attachment(
        self,
        to: str,
        subject: str,
        body_html: str,
        attachment_path: Optional[str] = None
    ) -> dict:
        """Creates an email message with an attachment"""
        
        message = MIMEMultipart()
        message['to'] = to
        message['subject'] = subject
        
        # Add the message body
        msg_body = MIMEText(body_html, 'html', 'utf-8')
        message.attach(msg_body)
        
        # Add an attachment if there is one
        if attachment_path and Path(attachment_path).exists():
            with open(attachment_path, 'rb') as f:
                attachment = MIMEApplication(f.read(), _subtype='pdf')
                attachment.add_header(
                    'Content-Disposition',
                    'attachment',
                    filename=Path(attachment_path).name
                )
                message.attach(attachment)
        
        # base64 encoding
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')
        
        return {'raw': raw_message}
    
    def send_email(
        self,
        to: str,
        subject: str,
        body_html: str,
        attachment_path: Optional[str] = None
    ) -> bool:
        """Sends an email"""
        
        print(f"📧 שולח מייל אל: {to}")
        print(f"📝 נושא: {subject}")
        
        try:
            message = self.create_message_with_attachment(
                to,
                subject,
                body_html,
                attachment_path
            )
            
            sent_message = self.service.users().messages().send(
                userId='me',
                body=message
            ).execute()
            
            print(f"✅ נשלח בהצלחה! Message ID: {sent_message['id']}")
            return True
            
        except Exception as e:
            print(f"❌ שגיאה בשליחה: {e}")
            return False
    
    def _strip_metadata(self, md_text: str) -> str:
        """Strips the metadata header (materials, API cost) from the markdown before sending.
        The metadata appears before the first '---' line, after which the summary itself begins."""
        import re
        # Looks for the first boundary: a standalone --- line followed by a # heading line or content
        # Everything before it is metadata
        parts = re.split(r'\n---\n', md_text, maxsplit=1)
        if len(parts) == 2:
            return parts[1].lstrip('\n')
        return md_text

    def send_summary(
        self,
        course_name: str,
        lecture_title: str,
        summary_md_path: str,
        summary_pdf_path: Optional[str] = None,
        recipient: Optional[str] = None,
        recipients: Optional[list] = None
    ) -> bool:
        """Sends a summary by email to one or more recipients"""

        # Build the recipient list
        all_recipients = []
        if recipients:
            all_recipients = list(recipients)
        elif recipient:
            all_recipients = [recipient]
        else:
            env_r = os.getenv('RECIPIENT_EMAIL')
            if env_r:
                all_recipients = [env_r]

        if not all_recipients:
            print("❌ לא מוגדרים נמענים!")
            return False

        date_str = datetime.now().strftime("%d/%m/%Y")
        subject = f"סיכום {course_name} - {lecture_title} ({date_str})"

        with open(summary_md_path, 'r', encoding='utf-8') as f:
            summary_content = f.read()
        clean_md = self._strip_metadata(summary_content)

        if summary_pdf_path and Path(summary_pdf_path).exists():
            # Create a clean PDF (without metadata) to send
            from generators.pdf_generator import markdown_to_pdf
            import tempfile, os as _os
            tmp_pdf = summary_pdf_path.replace('.pdf', '_send.pdf')
            markdown_to_pdf(clean_md, tmp_pdf)
            html_body = self._short_html(course_name, lecture_title, date_str)
            attachment = tmp_pdf
        else:
            html_body = self._markdown_to_html(clean_md)
            attachment = None
            tmp_pdf = None

        success = True
        for to in all_recipients:
            ok = self.send_email(
                to=to,
                subject=subject,
                body_html=html_body,
                attachment_path=attachment
            )
            if not ok:
                success = False

        # Clean up the temporary PDF
        if tmp_pdf:
            try:
                import os as _os
                _os.unlink(tmp_pdf)
            except Exception:
                pass

        return success
    
    def _short_html(self, course_name: str, lecture_title: str, date_str: str) -> str:
        """Short RTL email body when a PDF is attached"""
        S = ('direction:rtl;text-align:right;font-family:Arial,sans-serif;'
             'max-width:600px;margin:0 auto;padding:24px;color:#1a1a1a')
        return (
            f'<div dir="rtl" style="{S}">'
            f'<h2 style="direction:rtl;text-align:right;color:#1a3c6e;border-bottom:2px solid #3498db;padding-bottom:8px">'
            f'סיכום {course_name}</h2>'
            f'<p style="direction:rtl;text-align:right;font-size:1.1em"><strong>{lecture_title}</strong></p>'
            f'<p style="direction:rtl;text-align:right;color:#555">{date_str}</p>'
            f'<p style="direction:rtl;text-align:right">הסיכום המלא מצורף כ-PDF.</p>'
            f'</div>'
        )

    def _inline(self, text: str) -> str:
        """Converts inline markdown (bold, code, timestamps) within a line"""
        import re
        # Bold: **text**
        text = re.sub(r'\*\*(.+?)\*\*', r'<strong style="color:#c0392b;font-weight:bold">\1</strong>', text)
        # Inline code: `code`
        text = re.sub(r'`([^`]+)`',
                      r'<code style="background:#f0f4f8;padding:2px 6px;border-radius:3px;'
                      r'font-family:monospace;font-size:0.88em;direction:ltr;display:inline-block">\1</code>',
                      text)
        # Timestamps like [00:15:30]
        text = re.sub(r'\[(\d{2}:\d{2}:\d{2})\]',
                      r'<span style="color:#718096;font-size:0.82em;font-family:monospace;direction:ltr;display:inline-block">[\1]</span>',
                      text)
        return text

    def _markdown_to_html(self, markdown_text: str) -> str:
        """Converts Markdown to HTML with inline styles (Gmail strips <style> blocks)"""

        # Styles as constants
        S_BODY  = ('direction:rtl;text-align:right;font-family:Arial,sans-serif;'
                   'line-height:1.8;max-width:860px;margin:0 auto;padding:24px 16px;'
                   'color:#1a1a1a;background:#ffffff')
        S_H1    = ('direction:rtl;text-align:right;color:#1a3c6e;font-size:1.7em;'
                   'border-bottom:3px solid #3498db;padding-bottom:8px;margin-top:0')
        S_H2    = ('direction:rtl;text-align:right;color:#2c5282;font-size:1.3em;'
                   'margin-top:28px;border-right:4px solid #3498db;padding-right:10px')
        S_H3    = 'direction:rtl;text-align:right;color:#4a5568;font-size:1.05em;margin-top:16px'
        S_P     = 'direction:rtl;text-align:right;margin:6px 0'
        S_LI    = 'direction:rtl;text-align:right;margin:4px 0'
        S_UL    = 'direction:rtl;text-align:right;padding-right:24px;padding-left:0;margin:8px 0'
        S_PRE   = ('direction:ltr;text-align:left;background:#f0f4f8;padding:14px;'
                   'border-radius:6px;overflow-x:auto;border-right:3px solid #3498db;'
                   'font-family:monospace;font-size:0.88em;margin:10px 0')
        S_HR    = 'border:none;border-top:1px solid #e2e8f0;margin:20px 0'
        S_QUOTE = ('direction:rtl;text-align:right;border-right:4px solid #3498db;'
                   'padding:8px 14px;margin:8px 0;background:#f8faff;color:#555;border-radius:0 4px 4px 0')

        lines = markdown_text.split('\n')
        parts = []
        in_code_block = False
        in_list = False

        def close_list():
            nonlocal in_list
            if in_list:
                parts.append(f'</ul>')
                in_list = False

        for line in lines:
            # Code block fence
            if line.startswith('```'):
                close_list()
                if in_code_block:
                    parts.append('</pre>')
                else:
                    parts.append(f'<pre style="{S_PRE}">')
                in_code_block = not in_code_block
                continue

            if in_code_block:
                # Escape HTML inside code blocks
                parts.append(line.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))
                continue

            # HR
            if line.strip() in ('---', '***', '___'):
                close_list()
                parts.append(f'<hr style="{S_HR}">')
                continue

            # Headers
            if line.startswith('# '):
                close_list()
                parts.append(f'<h1 style="{S_H1}">{self._inline(line[2:])}</h1>')
            elif line.startswith('## '):
                close_list()
                parts.append(f'<h2 style="{S_H2}">{self._inline(line[3:])}</h2>')
            elif line.startswith('### '):
                close_list()
                parts.append(f'<h3 style="{S_H3}">{self._inline(line[4:])}</h3>')
            elif line.startswith('#### '):
                close_list()
                parts.append(f'<h3 style="{S_H3}">{self._inline(line[5:])}</h3>')

            # Blockquote
            elif line.startswith('> '):
                close_list()
                parts.append(f'<div style="{S_QUOTE}">{self._inline(line[2:])}</div>')

            # List items
            elif line.startswith('- ') or line.startswith('* ') or (len(line) > 2 and line[0].isdigit() and line[1] in '.):'):
                if not in_list:
                    parts.append(f'<ul style="{S_UL}">')
                    in_list = True
                content = line[2:] if line[1] in ' ' else line[line.index(' ')+1:]
                parts.append(f'<li style="{S_LI}">{self._inline(content)}</li>')

            # Empty line
            elif not line.strip():
                close_list()
                parts.append('<br>')

            # Regular paragraph
            else:
                close_list()
                parts.append(f'<p style="{S_P}">{self._inline(line)}</p>')

        close_list()

        body_content = '\n'.join(parts)
        return (f'<div dir="rtl" style="{S_BODY}">'
                f'{body_content}'
                f'</div>')
        
        return html


if __name__ == "__main__":
    # Test
    sender = GmailSender()
    
    # Example
    sender.send_summary(
        course_name="חדו\"א 2ב",
        lecture_title="אינטגרלים - הרצאה 5",
        summary_md_path="data/summaries/calculus_2_lecture5.md",
        recipient="your_email@gmail.com"
    )
