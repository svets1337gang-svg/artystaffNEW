import os
import logging
import re
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import gspread

from config import (
    FT_GOOGLE_SHEETS_ID, FT_SHEET_NAME, FT_GOOGLE_FORM_ID,
    RW_GOOGLE_SHEETS_ID, RW_SHEET_NAME, RW_GOOGLE_FORM_ID
)

logger = logging.getLogger('StaffBot.GoogleManager')

class GoogleManager:
    def __init__(self):
        self.base_dir = os.path.dirname(os.path.abspath(__file__))
        
        creds_json = os.getenv('GOOGLE_CREDS_JSON')
        if creds_json and creds_json.strip():
            self.creds_path = os.path.join(self.base_dir, 'credentials_tmp.json')
            try:
                with open(self.creds_path, 'w', encoding='utf-8') as f:
                    f.write(creds_json)
                logger.info("Credentials created from environment variable.")
            except Exception as e:
                logger.error(f"Failed to create credentials file from env: {e}")
        else:
            env_path = os.getenv('GOOGLE_CREDS_PATH')
            if env_path and env_path.strip():
                self.creds_path = env_path
            else:
                self.creds_path = os.path.join(self.base_dir, 'credentials.json')

        self._drive_service = None
        self._gc = None

    def get_drive_service(self):
        if self._drive_service is None:
            try:
                creds = Credentials.from_service_account_file(self.creds_path)
                scoped_creds = creds.with_scopes(['https://www.googleapis.com/auth/drive'])
                self._drive_service = build('drive', 'v3', credentials=scoped_creds)
            except Exception as e:
                logger.error(f"Google Drive API Error: {e}")
        return self._drive_service

    def get_gc(self):
        if self._gc is None:
            try:
                self._gc = gspread.service_account(filename=self.creds_path)
            except Exception as e:
                logger.error(f"Gspread Error: {e}")
        return self._gc

    def get_sheet(self, sheet_id, sheet_name):
        try:
            gc = self.get_gc()
            return gc.open_by_key(sheet_id).worksheet(sheet_name)
        except Exception as e:
            logger.error(f"Error getting sheet {sheet_name}: {e}")
            return None

def normalize_nick(nick: str) -> str:
    if not nick: return ""
    return re.sub(r'\[[^\]]*\]|\([^)]*\)|\{[^}]*\}', '', nick).strip()

def parse_points(value) -> int:
    if not value: return 0
    s = str(value).strip().replace(' ', '').replace(',', '').replace('\xa0', '')
    try:
        return int(float(s))
    except:
        return 0

def find_user_by_nick(nick, sheet_id, sheet_name, google_manager):
    sheet = google_manager.get_sheet(sheet_id, sheet_name)
    if not sheet: return None
    try:
        clean_nick = normalize_nick(nick)
        values = sheet.col_values(1)
        for row_num, value in enumerate(values, start=1):
            if normalize_nick(value) == clean_nick:
                row = sheet.row_values(row_num)
                return {
                    'row': row_num,
                    'nick': row[0] if len(row) > 0 else '',
                    'position': row[1] if len(row) > 1 else '',
                    'points': row[2] if len(row) > 2 else '0',
                    'warns': row[3] if len(row) > 3 else '0/3',
                    'warnings': row[4] if len(row) > 4 else '0/3',
                    'email': row[5] if len(row) > 5 else '',
                }
    except Exception as e:
        logger.error(f"Search error: {e}")
    return None

# Singleton instance for the app
google_manager = GoogleManager()
