import os
import re
import json
import time
import asyncio
import logging
import threading
from datetime import datetime, timedelta, time as dtime
from collections import Counter, defaultdict
from io import BytesIO
from difflib import SequenceMatcher

import pytz
from google import genai
from google.genai import types
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from telegram import (
 Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
)
from telegram.ext import (
 Application, CommandHandler, MessageHandler,
 CallbackQueryHandler, filters, ContextTypes,
)
from flask import Flask, Response

# WeasyPrint សម្រាប់ PDF
from weasyprint import HTML

# ══════════════════════════════════════
# Environment Variables
# ══════════════════════════════════════

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS")
GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")
CHAT_ID = os.environ.get("CHAT_ID")
CALENDAR_SECRET = os.environ.get("CALENDAR_SECRET", "changeme")
PORT = int(os.environ.get("PORT", 10000))

TZ = pytz.timezone("Asia/Phnom_Penh")

# ══════════════════════════════════════
# Logging
# ══════════════════════════════════════

logging.basicConfig(
 format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
 level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════
# Gemini Setup
# ══════════════════════════════════════

gemini_client = genai.Client(api_key=GEMINI_API_KEY)
logger.info("✅ Gemini configured!")

# ══════════════════════════════════════
# Google Services Setup
# ══════════════════════════════════════

SCOPES = [
 "https://www.googleapis.com/auth/spreadsheets",
 "https://www.googleapis.com/auth/drive",
 "https://www.googleapis.com/auth/calendar",
]

credentials_info = json.loads(GOOGLE_CREDENTIALS)
creds = Credentials.from_service_account_info(credentials_info, scopes=SCOPES)

# Sheets
gc = gspread.authorize(creds)
spreadsheet = gc.open_by_key(GOOGLE_SHEET_ID)
worksheet = spreadsheet.sheet1

# Calendar
calendar_service = build('calendar', 'v3', credentials=creds)
logger.info(f"✅ Google Calendar: {GOOGLE_CALENDAR_ID}")

# ══════════════════════════════════════
# Sheet Headers (8 columns)
# ══════════════════════════════════════

HEADERS = [
 "#", "កាលបរិច្ឆេទ", "ម៉ោងព្រឹត្តិការណ៍",
 "ព្រឹត្តិការណ៍", "ប្រភេទ", "ស្ថានភាព",
 "ម៉ោងបញ្ចូល", "GCal_ID"
]

try:
 header = worksheet.row_values(1)
 if not header or header != HEADERS:
 worksheet.update("A1:H1", [HEADERS])
 logger.info("✅ Headers updated to v3.2")
except Exception as e:
 logger.error(f"Header error: {e}")

logger.info("✅ Sheet connected!")

# ══════════════════════════════════════
# Constants
# ══════════════════════════════════════

KHMER_MONTHS = {
 "មករា": 1, "កុម្ភៈ": 2, "មីនា": 3, "មេសា": 4,
 "ឧសភា": 5, "មិថុនា": 6, "កក្កដា": 7, "សីហា": 8,
 "កញ្ញា": 9, "តុលា": 10, "វិច្ឆិកា": 11, "ធ្នូ": 12,
}
KHMER_MONTHS_NAMES = {v: k for k, v in KHMER_MONTHS.items()}

WEEKDAY_NAMES = {
 0: "ច័ន្ទ", 1: "អង្គារ", 2: "ពុធ",
 3: "ព្រហស្បតិ៍", 4: "សុក្រ", 5: "សៅរ៍", 6: "អាទិត្យ",
}
WEEKDAY_SHORT = {
 0: "MON", 1: "TUE", 2: "WED",
 3: "THU", 4: "FRI", 5: "SAT", 6: "SUN",
}

CATEGORIES = {
 "work": "🏢 ការងារ",
 "family": "👨‍👩‍👧 គ្រួសារ",
 "health": "💊 សុខភាព",
 "event": "🎉 ព្រឹត្តិការណ៍",
 "study": "📚 សិក្សា",
 "other": "📌 ផ្សេងៗ",
}

STATUS_PENDING = "⏳ រង់ចាំ"
STATUS_DONE = "✅ រួចរាល់"
STATUS_CANCEL = "❌ បោះបង់"

pending_events = {}


# ══════════════════════════════════════
# Utilities
# ══════════════════════════════════════

def similarity(a, b):
 return SequenceMatcher(None, a, b).ratio()


def detect_category(text):
 text_lower = text.lower()
 if any(w in text_lower for w in ["meeting", "ប្រជុំ", "work", "office", "ការងារ", "deadline"]):
 return "work"
 if any(w in text_lower for w in ["family", "គ្រួសារ", "កូន", "ម្តាយ", "ឪពុក", "បងប្អូន"]):
 return "family"
 if any(w in text_lower for w in ["doctor", "hospital", "មន្ទីរពេទ្យ", "គ្រូពេទ្យ", "ថ្នាំ", "សុខភាព"]):
 return "health"
 if any(w in text_lower for w in ["party", "birthday", "ខួប", "ពិធី", "រៀបការ", "បុណ្យ"]):
 return "event"
 if any(w in text_lower for w in ["class", "study", "រៀន", "សិក្សា", "ថ្នាក់", "ប្រឡង"]):
 return "study"
 return "other"


def html_escape(text):
 """Escape HTML special chars"""
 if not text:
 return ""
 return (str(text)
 .replace("&", "&amp;")
 .replace("<", "&lt;")
 .replace(">", "&gt;")
 .replace('"', "&quot;")
 .replace("'", "&#39;"))


# ══════════════════════════════════════
# AI Parser
# ══════════════════════════════════════

def parse_with_ai(text):
 try:
 today = datetime.now(TZ).strftime("%Y-%m-%d")
 weekday_today = WEEKDAY_NAMES[datetime.now(TZ).weekday()]

 prompt = f"""អ្នកគឺជាកម្មវិធីវិភាគអត្ថបទខ្មែរដ៏ឆ្លាតវៃ។

ថ្ងៃនេះ: {today} (ថ្ងៃ{weekday_today})

សូមបំបែកអត្ថបទជា JSON:
{{
 "date": "YYYY-MM-DD",
 "time": "HH:MM" ឬ "",
 "event": "ព្រឹត្តិការណ៍តែប៉ុណ្ណោះ",
 "category": "work|family|health|event|study|other",
 "is_recurring": true/false,
 "recurring_day": 0-6 ឬ null
}}

ច្បាប់:
1. Date: បើគ្មាន → {today}
2. Time: "៩ ព្រឹក"→"09:00", "២ រសៀល"→"14:00", "៦ ល្ងាច"→"18:00"
3. Category:
 - work: ប្រជុំ, ការងារ, meeting
 - family: គ្រួសារ, កូន, ម្តាយ, ឪពុក
 - health: មន្ទីរពេទ្យ, គ្រូពេទ្យ, ថ្នាំ
 - event: ខួប, ពិធី, បុណ្យ, រៀបការ
 - study: រៀន, សិក្សា, ថ្នាក់, ប្រឡង
 - other: ផ្សេងទៀត
4. Recurring: "រៀងរាល់ថ្ងៃ..." → is_recurring=true
5. Event ត្រូវកាត់ date/time words ចេញ

ឆ្លើយតែ JSON។

អត្ថបទ: "{text}"

JSON:"""

 response = gemini_client.models.generate_content(
 model="gemini-flash-latest",
 contents=[prompt],
 )
 result = response.text.strip()
 result = re.sub(r"^```json\s*|\s*```$", "", result).strip()
 result = re.sub(r"^```\s*|\s*```$", "", result).strip()

 data = json.loads(result)
 date_str = data.get("date", today).strip()
 try:
 datetime.strptime(date_str, "%Y-%m-%d")
 except ValueError:
 date_str = today

 return {
 "date": date_str,
 "time": data.get("time", "").strip() or "",
 "event": data.get("event", text).strip() or text,
 "category": data.get("category", "other").strip() or "other",
 "is_recurring": bool(data.get("is_recurring", False)),
 "recurring_day": data.get("recurring_day"),
 }
 except Exception as e:
 logger.warning(f"AI parse failed: {e}")
 return {
 "date": datetime.now(TZ).strftime("%Y-%m-%d"),
 "time": "",
 "event": text,
 "category": detect_category(text),
 "is_recurring": False,
 "recurring_day": None,
 }


# ══════════════════════════════════════
# Sheet Operations
# ══════════════════════════════════════

def save_to_sheet(date_str, time_str, event, category_key, gcal_id=""):
 now = datetime.now(TZ)
 created = now.strftime("%Y-%m-%d %H:%M:%S")
 all_values = worksheet.get_all_values()
 row_num = len(all_values)
 category = CATEGORIES.get(category_key, CATEGORIES["other"])
 new_row = [
 row_num, date_str, time_str, event,
 category, STATUS_PENDING, created, gcal_id
 ]
 worksheet.append_row(new_row, value_input_option="USER_ENTERED")
 return row_num


def delete_row(row_num):
 all_values = worksheet.get_all_values()
 for idx, row in enumerate(all_values[1:], start=2):
 if row and str(row[0]) == str(row_num):
 gcal_id = row[7] if len(row) > 7 else ""
 if gcal_id:
 try:
 calendar_service.events().delete(
 calendarId=GOOGLE_CALENDAR_ID,
 eventId=gcal_id
 ).execute()
 logger.info(f"Deleted gcal: {gcal_id}")
 except Exception as e:
 logger.warning(f"Gcal delete failed: {e}")

 worksheet.delete_rows(idx)
 renumber_rows()
 return True
 return False


def edit_row(row_num, field, new_value):
 field_map = {"date": 2, "time": 3, "event": 4, "category": 5, "status": 6}
 col = field_map.get(field)
 if not col:
 return False
 all_values = worksheet.get_all_values()
 for idx, row in enumerate(all_values[1:], start=2):
 if row and str(row[0]) == str(row_num):
 worksheet.update_cell(idx, col, new_value)
 return True
 return False


def renumber_rows():
 all_values = worksheet.get_all_values()
 for idx in range(1, len(all_values)):
 worksheet.update_cell(idx + 1, 1, idx)


def update_gcal_id(row_num, gcal_id):
 all_values = worksheet.get_all_values()
 for idx, row in enumerate(all_values[1:], start=2):
 if row and str(row[0]) == str(row_num):
 worksheet.update_cell(idx, 8, gcal_id)
 return True
 return False


def get_all_events():
 all_values = worksheet.get_all_values()
 events = []
 for row in all_values[1:]:
 if len(row) >= 4 and row[0]:
 events.append({
 "id": row[0],
 "date": row[1] if len(row) > 1 else "",
 "time": row[2] if len(row) > 2 else "",
 "event": row[3] if len(row) > 3 else "",
 "category": row[4] if len(row) > 4 else CATEGORIES["other"],
 "status": row[5] if len(row) > 5 else STATUS_PENDING,
 "created": row[6] if len(row) > 6 else "",
 "gcal_id": row[7] if len(row) > 7 else "",
 })
 return events


def sort_sheet_by_date():
 try:
 all_values = worksheet.get_all_values()
 if len(all_values) <= 1:
 return False
 header = all_values[0]
 data = all_values[1:]
 data.sort(key=lambda x: (
 x[1] if len(x) > 1 else "",
 x[2] if len(x) > 2 else ""
 ))
 for idx, row in enumerate(data, start=1):
 row[0] = str(idx)
 worksheet.clear()
 worksheet.update("A1", [header] + data)
 logger.info("✅ Sheet sorted")
 return True
 except Exception as e:
 logger.error(f"Sort error: {e}")
 return False


def find_duplicates(date_str, event, threshold=0.75):
 events = get_all_events()
 return [
 e for e in events
 if e['date'] == date_str
 and similarity(e['event'].lower(), event.lower()) >= threshold
 ]


def search_events(keyword):
 events = get_all_events()
 keyword_lower = keyword.lower()
 return [e for e in events if keyword_lower in e['event'].lower()]


# ══════════════════════════════════════
# Google Calendar Sync
# ══════════════════════════════════════

def get_calendar_events(days_back=7, days_forward=90):
 try:
 now = datetime.now(TZ)
 time_min = (now - timedelta(days=days_back)).isoformat()
 time_max = (now + timedelta(days=days_forward)).isoformat()

 result = calendar_service.events().list(
 calendarId=GOOGLE_CALENDAR_ID,
 timeMin=time_min,
 timeMax=time_max,
 singleEvents=True,
 orderBy='startTime',
 maxResults=250,
 ).execute()

 return result.get('items', [])
 except HttpError as e:
 logger.error(f"Calendar API error: {e}")
 return []
 except Exception as e:
 logger.error(f"Get calendar events error: {e}")
 return []


def parse_gcal_event(gcal_event):
 try:
 gcal_id = gcal_event.get('id', '')
 summary = gcal_event.get('summary', '(គ្មានចំណងជើង)')
 description = gcal_event.get('description', '')

 start = gcal_event.get('start', {})
 if 'dateTime' in start:
 dt = datetime.fromisoformat(start['dateTime'].replace('Z', '+00:00'))
 dt_local = dt.astimezone(TZ)
 date_str = dt_local.strftime("%Y-%m-%d")
 time_str = dt_local.strftime("%H:%M")
 elif 'date' in start:
 date_str = start['date']
 time_str = ""
 else:
 return None

 category = detect_category(summary + " " + description)

 return {
 "gcal_id": gcal_id,
 "date": date_str,
 "time": time_str,
 "event": summary,
 "category": category,
 }
 except Exception as e:
 logger.warning(f"Parse gcal event error: {e}")
 return None


def get_synced_gcal_ids():
 try:
 all_values = worksheet.get_all_values()
 return {row[7] for row in all_values[1:] if len(row) > 7 and row[7]}
 except Exception:
 return set()


def save_gcal_to_sheet(gcal_data):
 return save_to_sheet(
 gcal_data['date'],
 gcal_data['time'],
 gcal_data['event'],
 gcal_data['category'],
 gcal_id=gcal_data['gcal_id']
 )


def update_sheet_from_gcal(gcal_id, gcal_data):
 try:
 all_values = worksheet.get_all_values()
 for idx, row in enumerate(all_values[1:], start=2):
 if len(row) > 7 and row[7] == gcal_id:
 worksheet.update(f"B{idx}:E{idx}", [[
 gcal_data['date'],
 gcal_data['time'],
 gcal_data['event'],
 CATEGORIES.get(gcal_data['category'], CATEGORIES["other"]),
 ]])
 return True
 return False
 except Exception as e:
 logger.error(f"Update from gcal error: {e}")
 return False


def sync_from_google_calendar():
 try:
 logger.info("🔄 Syncing from Google Calendar...")
 gcal_events = get_calendar_events()
 if not gcal_events:
 return 0, 0

 synced_ids = get_synced_gcal_ids()
 added = 0
 updated = 0

 for gcal_event in gcal_events:
 data = parse_gcal_event(gcal_event)
 if not data:
 continue

 if data['gcal_id'] in synced_ids:
 if update_sheet_from_gcal(data['gcal_id'], data):
 updated += 1
 else:
 save_gcal_to_sheet(data)
 added += 1
 logger.info(f"➕ Added from gcal: {data['event']}")

 logger.info(f"✅ Sync: +{added} new, ~{updated} updated")
 return added, updated
 except Exception as e:
 logger.error(f"Sync error: {e}")
 return 0, 0


def push_to_google_calendar(date_str, time_str, event, category):
 try:
 if time_str:
 start_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
 end_dt = start_dt + timedelta(hours=1)
 event_body = {
 'summary': event,
 'description': f"🏷 {category}\n\n📱 From Voice Tracker Bot",
 'start': {
 'dateTime': start_dt.strftime("%Y-%m-%dT%H:%M:00"),
 'timeZone': 'Asia/Phnom_Penh',
 },
 'end': {
 'dateTime': end_dt.strftime("%Y-%m-%dT%H:%M:00"),
 'timeZone': 'Asia/Phnom_Penh',
 },
 'reminders': {
 'useDefault': False,
 'overrides': [
 {'method': 'popup', 'minutes': 60},
 {'method': 'popup', 'minutes': 1440},
 ],
 },
 }
 else:
 event_body = {
 'summary': event,
 'description': f"🏷 {category}",
 'start': {'date': date_str},
 'end': {'date': date_str},
 }

 result = calendar_service.events().insert(
 calendarId=GOOGLE_CALENDAR_ID,
 body=event_body
 ).execute()
 logger.info(f"➡️ Pushed to gcal: {event}")
 return result.get('id')
 except Exception as e:
 logger.error(f"Push to gcal error: {e}")
 return None


# ══════════════════════════════════════
# Speech / Vision (Gemini)
# ══════════════════════════════════════

def transcribe_audio(file_path):
 models = ["gemini-flash-latest", "gemini-2.0-flash",
 "gemini-flash-lite-latest", "gemini-2.5-flash-lite"]
 prompt = "សូមស្តាប់សំឡេងនេះ ហើយបំលែងទៅជាអក្សរខ្មែរ។ Return ONLY the Khmer text."
 try:
 with open(file_path, "rb") as f:
 audio_bytes = f.read()
 except Exception:
 return None

 for model in models:
 for attempt in range(3):
 try:
 response = gemini_client.models.generate_content(
 model=model,
 contents=[prompt, types.Part.from_bytes(
 data=audio_bytes, mime_type="audio/ogg")],
 )
 return response.text.strip()
 except Exception as e:
 if "503" in str(e) or "UNAVAILABLE" in str(e):
 time.sleep(2)
 elif "429" in str(e):
 time.sleep(5)
 else:
 break
 return None


def extract_from_image(file_path):
 models = ["gemini-flash-latest", "gemini-2.0-flash", "gemini-flash-lite-latest"]
 prompt = (
 "សូមមើលរូបភាពនេះ ហើយស្រង់យក JSON:\n"
 '{"date": "YYYY-MM-DD", "time": "HH:MM" ឬ "", "event": "ការពិពណ៌នា"}\n'
 "បើគ្មានកាលបរិច្ឆេទ ប្រើថ្ងៃនេះ។ Return ONLY JSON."
 )
 try:
 with open(file_path, "rb") as f:
 image_bytes = f.read()
 except Exception:
 return None

 for model in models:
 for attempt in range(2):
 try:
 response = gemini_client.models.generate_content(
 model=model,
 contents=[prompt, types.Part.from_bytes(
 data=image_bytes, mime_type="image/jpeg")],
 )
 text = response.text.strip()
 text = re.sub(r"^```json\s*|\s*```$", "", text).strip()
 text = re.sub(r"^```\s*|\s*```$", "", text).strip()
 try:
 data = json.loads(text)
 return {
 "date": data.get("date", datetime.now(TZ).strftime("%Y-%m-%d")),
 "time": data.get("time", ""),
 "event": data.get("event", "រូបភាព"),
 }
 except json.JSONDecodeError:
 return {
 "date": datetime.now(TZ).strftime("%Y-%m-%d"),
 "time": "",
 "event": text[:200],
 }
 except Exception as e:
 if "503" in str(e) or "429" in str(e):
 time.sleep(2)
 else:
 break
 return None


# ══════════════════════════════════════
# ICS Export
# ══════════════════════════════════════

def generate_ics(events):
 lines = [
 "BEGIN:VCALENDAR",
 "VERSION:2.0",
 "PRODID:-//Voice Tracker Bot//KH//EN",
 "CALSCALE:GREGORIAN",
 ]
 for e in events:
 try:
 date = datetime.strptime(e['date'], "%Y-%m-%d")
 if e['time']:
 try:
 t = datetime.strptime(e['time'], "%H:%M").time()
 start = datetime.combine(date.date(), t)
 end = start + timedelta(hours=1)
 dtstart = start.strftime("%Y%m%dT%H%M%S")
 dtend = end.strftime("%Y%m%dT%H%M%S")
 except Exception:
 dtstart = date.strftime("%Y%m%d")
 dtend = (date + timedelta(days=1)).strftime("%Y%m%d")
 else:
 dtstart = date.strftime("%Y%m%d")
 dtend = (date + timedelta(days=1)).strftime("%Y%m%d")

 summary = e['event'].replace("\n", " ")
 desc = f"Category: {e.get('category', '')} | Status: {e.get('status', '')}"

 lines.extend([
 "BEGIN:VEVENT",
 f"UID:{e['id']}@voice-tracker-bot",
 f"DTSTAMP:{datetime.now(TZ).strftime('%Y%m%dT%H%M%SZ')}",
 f"DTSTART:{dtstart}",
 f"DTEND:{dtend}",
 f"SUMMARY:{summary}",
 f"DESCRIPTION:{desc}",
 "END:VEVENT",
 ])
 except Exception as ex:
 logger.warning(f"ICS skip #{e.get('id')}: {ex}")

 lines.append("END:VCALENDAR")
 return "\n".join(lines).encode("utf-8")


# ══════════════════════════════════════
# 📄 PDF Calendar Generator (WeasyPrint) v3.3
# ══════════════════════════════════════

from weasyprint import HTML

CATEGORY_COLOR_MAP = {
 "🏢 ការងារ": "#4285F4",
 "👨‍👩‍👧 គ្រួសារ": "#EA4335",
 "💊 សុខភាព": "#34A853",
 "🎉 ព្រឹត្តិការណ៍": "#FBBC04",
 "📚 សិក្សា": "#9C27B0",
 "📌 ផ្សេងៗ": "#00ACC1",
}

FONT_IMPORT = """
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+Khmer:wght@400;500;600;700&display=swap');
"""


def build_week_html(start_date, week_dates, events_by_date, today):
 """Build HTML for Week Calendar"""
 end_date = start_date + timedelta(days=6)
 total = sum(len(events_by_date[d]) for d in week_dates)
 subtitle = (f"{start_date.day} {KHMER_MONTHS_NAMES[start_date.month]} - "
 f"{end_date.day} {KHMER_MONTHS_NAMES[end_date.month]} {end_date.year}")

 columns_html = ""
 for d in week_dates:
 is_weekend = d.weekday() >= 5
 is_today = d == today

 col_classes = ["day-col"]
 if is_today:
 col_classes.append("today")
 elif is_weekend:
 col_classes.append("weekend")

 day_short = WEEKDAY_SHORT[d.weekday()]
 day_khmer = WEEKDAY_NAMES[d.weekday()]
 day_num = str(d.day)

 wd_class = ""
 if is_weekend:
 wd_class = "weekend-text"
 elif is_today:
 wd_class = "today-text"

 day_num_html = (f'<div class="day-num today-circle">{day_num}</div>'
 if is_today
 else f'<div class="day-num">{day_num}</div>')

 events_html = ""
 for e in events_by_date[d]:
 category = e.get('category', '📌 ផ្សេងៗ')
 color = CATEGORY_COLOR_MAP.get(category, "#00ACC1")

 status = e.get('status', '')
 extra_style = ""
 if STATUS_DONE in status:
 color = "#34A853"
 extra_style = "opacity: 0.75;"
 elif STATUS_CANCEL in status:
 color = "#9E9E9E"
 extra_style = "opacity: 0.5;"

 time_html = (f'<span class="event-time">{html_escape(e["time"])}</span>'
 if e['time'] else "")

 events_html += f"""
 <div class="event" style="background-color: {color}; {extra_style}">
 <div class="event-header">
 <span class="event-id">#{html_escape(e['id'])}</span>
 {time_html}
 </div>
 <div class="event-text">{html_escape(e['event'])}</div>
 </div>
 """

 columns_html += f"""
 <div class="{' '.join(col_classes)}">
 <div class="day-header">
 <div class="weekday-en {wd_class}">{day_short}</div>
 {day_num_html}
 <div class="weekday-kh">{day_khmer}</div>
 </div>
 <div class="events">
 {events_html}
 </div>
 </div>
 """

 footer_time = datetime.now(TZ).strftime('%Y-%m-%d %H:%M')

 html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
{FONT_IMPORT}

@page {{
 size: A4 landscape;
 margin: 8mm;
}}

* {{
 box-sizing: border-box;
 margin: 0;
 padding: 0;
 letter-spacing: normal;
 word-spacing: normal;
}}

html, body {{
 font-family: 'Noto Sans Khmer', 'Khmer OS', sans-serif;
 color: #202124;
 font-size: 10pt;
 letter-spacing: 0;
}}

/* Numbers & English text - use Arial for tight spacing */
.day-num, .cell-num, .weekday-en, .wd-en,
.event-id, .event-time, .header-count-num {{
 font-family: 'Arial', 'Helvetica', sans-serif !important;
 letter-spacing: 0 !important;
}}

/* Header */
.header {{
 background-color: #4285F4;
 color: white;
 padding: 20px 25px;
 border-radius: 8px 8px 0 0;
}}

.header-flex {{
 display: table;
 width: 100%;
}}

.header-left {{
 display: table-cell;
 vertical-align: middle;
 width: 70%;
}}

.header-right {{
 display: table-cell;
 vertical-align: middle;
 text-align: right;
 font-size: 12pt;
 font-weight: 500;
}}

.header-title {{
 font-size: 24pt;
 font-weight: 700;
 margin-bottom: 6px;
 line-height: 1.2;
}}

.header-subtitle {{
 font-size: 13pt;
 font-weight: 400;
 opacity: 0.95;
}}

/* Grid */
.grid {{
 display: table;
 width: 100%;
 border-collapse: separate;
 border-spacing: 4px;
 margin-top: 4px;
 table-layout: fixed;
}}

.grid-row {{
 display: table-row;
}}

.day-col {{
 display: table-cell;
 background-color: white;
 border: 1px solid #DADCE0;
 border-radius: 6px;
 vertical-align: top;
 padding: 10px 8px;
 width: 14.28%;
 min-height: 460px;
}}

.day-col.weekend {{
 background-color: #FFF3E0;
}}

.day-col.today {{
 background-color: #E3F2FD;
}}

/* Day header */
.day-header {{
 text-align: center;
 padding-bottom: 10px;
 border-bottom: 1px solid #DADCE0;
 margin-bottom: 10px;
}}

.weekday-en {{
 font-size: 10pt;
 font-weight: 700;
 color: #5F6368;
}}

.weekday-en.weekend-text {{
 color: #EA4335;
}}

.weekday-en.today-text {{
 color: #4285F4;
}}

.day-num {{
 font-size: 24pt;
 font-weight: 700;
 color: #202124;
 margin: 6px 0;
 line-height: 1;
}}

.day-num.today-circle {{
 display: inline-block;
 background-color: #4285F4;
 color: white;
 width: 44px;
 height: 44px;
 line-height: 44px;
 border-radius: 50%;
 font-size: 18pt;
 text-align: center;
 padding: 0;
 margin: 4px auto;
}}

.weekday-kh {{
 font-size: 9pt;
 color: #5F6368;
 margin-top: 4px;
}}

/* Events */
.events {{
 padding-top: 4px;
}}

.event {{
 color: white;
 border-radius: 5px;
 padding: 6px 8px;
 font-size: 8pt;
 line-height: 1.4;
 margin-bottom: 5px;
 page-break-inside: avoid;
}}

.event-header {{
 width: 100%;
 display: table;
 table-layout: fixed;
 margin-bottom: 5px;
 padding-bottom: 3px;
 font-size: 7.5pt;
 font-weight: 700;
 border-bottom: 1px solid rgba(255,255,255,0.25);
}}

.event-id {{
 display: table-cell;
 text-align: left;
 vertical-align: middle;
 width: 50%;
}}

.event-time {{
 display: table-cell;
 text-align: right;
 vertical-align: middle;
 width: 50%;
}}

.event-text {{
 font-size: 9pt;
 line-height: 1.4;
 word-wrap: break-word;
 overflow-wrap: break-word;
}}

/* Footer */
.footer {{
 margin-top: 10px;
 display: table;
 width: 100%;
 font-size: 8pt;
 color: #5F6368;
 padding: 5px;
}}

.footer-left {{
 display: table-cell;
 vertical-align: middle;
}}

.footer-right {{
 display: table-cell;
 vertical-align: middle;
 text-align: right;
}}

.legend-item {{
 display: inline-block;
 margin-right: 15px;
}}

.legend-color {{
 display: inline-block;
 width: 10px;
 height: 10px;
 border-radius: 2px;
 vertical-align: middle;
 margin-right: 4px;
}}
</style>
</head>
<body>
 <div class="header">
 <div class="header-flex">
 <div class="header-left">
 <div class="header-title">កាលវិភាគសប្តាហ៍</div>
 <div class="header-subtitle">{subtitle}</div>
 </div>
 <div class="header-right">
 សរុប៖ <span class="header-count-num">{total}</span> ព្រឹត្តិការណ៍
 </div>
 </div>
 </div>

 <div class="grid">
 <div class="grid-row">
 {columns_html}
 </div>
 </div>

 <div class="footer">
 <div class="footer-left">
 <span class="legend-item"><span class="legend-color" style="background:#4285F4"></span>ការងារ</span>
 <span class="legend-item"><span class="legend-color" style="background:#EA4335"></span>គ្រួសារ</span>
 <span class="legend-item"><span class="legend-color" style="background:#34A853"></span>សុខភាព</span>
 <span class="legend-item"><span class="legend-color" style="background:#FBBC04"></span>ព្រឹត្តិការណ៍</span>
 <span class="legend-item"><span class="legend-color" style="background:#9C27B0"></span>សិក្សា</span>
 </div>
 <div class="footer-right">
 Voice Tracker Bot • {footer_time}
 </div>
 </div>
</body>
</html>
"""
 return html


def generate_week_calendar_pdf(start_date=None):
 """Generate Week PDF using WeasyPrint"""
 if start_date is None:
 today = datetime.now(TZ).date()
 start_date = today - timedelta(days=today.weekday())

 week_dates = [start_date + timedelta(days=i) for i in range(7)]

 all_events = get_all_events()
 events_by_date = defaultdict(list)
 for e in all_events:
 try:
 d = datetime.strptime(e['date'], "%Y-%m-%d").date()
 if start_date <= d <= start_date + timedelta(days=6):
 events_by_date[d].append(e)
 except Exception:
 pass

 for d in events_by_date:
 events_by_date[d].sort(
 key=lambda x: (x['time'] or "99:99",
 int(x['id']) if str(x['id']).isdigit() else 0)
 )

 today = datetime.now(TZ).date()
 html_content = build_week_html(start_date, week_dates, events_by_date, today)

 output = BytesIO()
 HTML(string=html_content).write_pdf(output)
 output.seek(0)
 return output


def build_month_html(year, month, events_by_date, today):
 """Build HTML for Month Calendar"""
 first_day = datetime(year, month, 1).date()
 if month == 12:
 last_day = datetime(year + 1, 1, 1).date() - timedelta(days=1)
 else:
 last_day = datetime(year, month + 1, 1).date() - timedelta(days=1)

 total = sum(len(events_by_date[d]) for d in events_by_date)

 weekdays_html = ""
 weekdays_kh = ["ច័ន្ទ", "អង្គារ", "ពុធ", "ព្រហស្បតិ៍", "សុក្រ", "សៅរ៍", "អាទិត្យ"]
 weekdays_en = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
 for en, kh in zip(weekdays_en, weekdays_kh):
 weekdays_html += (f'<div class="wd-cell">'
 f'<div class="wd-en">{en}</div>'
 f'<div class="wd-kh">{kh}</div>'
 f'</div>')

 first_weekday = first_day.weekday()
 total_days = (last_day - first_day).days + 1
 total_cells = first_weekday + total_days
 rows = (total_cells + 6) // 7

 rows_html = ""
 for row_idx in range(rows):
 row_cells = ""
 for col_idx in range(7):
 cell_idx = row_idx * 7 + col_idx
 day_offset = cell_idx - first_weekday
 if 0 <= day_offset < total_days:
 cell_date = first_day + timedelta(days=day_offset)
 is_this_month = True
 else:
 if day_offset < 0:
 cell_date = first_day + timedelta(days=day_offset)
 else:
 cell_date = last_day + timedelta(days=day_offset - total_days + 1)
 is_this_month = False

 is_weekend = cell_date.weekday() >= 5
 is_today = cell_date == today

 cell_classes = ["cell"]
 if not is_this_month:
 cell_classes.append("other-month")
 elif is_today:
 cell_classes.append("today")
 elif is_weekend:
 cell_classes.append("weekend")

 day_num = str(cell_date.day)
 num_classes = ["cell-num"]
 if is_weekend and is_this_month:
 num_classes.append("weekend-num")
 if is_today:
 num_classes.append("today-num")

 events_html = ""
 if is_this_month and cell_date in events_by_date:
 events = events_by_date[cell_date]
 for e in events[:4]:
 category = e.get('category', '📌 ផ្សេងៗ')
 color = CATEGORY_COLOR_MAP.get(category, "#00ACC1")
 time_prefix = f"{e['time']} " if e['time'] else ""
 event_text = e['event'][:22] + ("…" if len(e['event']) > 22 else "")
 events_html += (f'<div class="mini-event" style="background:{color}">'
 f'{html_escape(time_prefix + event_text)}</div>')
 if len(events) > 4:
 events_html += f'<div class="more">+ {len(events)-4} ទៀត</div>'

 row_cells += f"""
 <div class="{' '.join(cell_classes)}">
 <div class="{' '.join(num_classes)}">{day_num}</div>
 <div class="cell-events">{events_html}</div>
 </div>
 """

 rows_html += f'<div class="grid-row">{row_cells}</div>'

 footer_time = datetime.now(TZ).strftime('%Y-%m-%d %H:%M')

 html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
{FONT_IMPORT}

@page {{
 size: A4 landscape;
 margin: 8mm;
}}

* {{
 box-sizing: border-box;
 margin: 0;
 padding: 0;
 letter-spacing: normal;
 word-spacing: normal;
}}

html, body {{
 font-family: 'Noto Sans Khmer', 'Khmer OS', sans-serif;
 color: #202124;
}}

.cell-num, .wd-en, .header-count-num {{
 font-family: 'Arial', 'Helvetica', sans-serif !important;
 letter-spacing: 0 !important;
}}

/* Header */
.header {{
 background-color: #4285F4;
 color: white;
 padding: 20px;
 text-align: center;
 border-radius: 8px 8px 0 0;
}}

.header-title {{
 font-size: 26pt;
 font-weight: 700;
 line-height: 1.2;
}}

.header-subtitle {{
 font-size: 12pt;
 margin-top: 6px;
}}

/* Weekdays */
.weekdays {{
 display: table;
 width: 100%;
 border-collapse: separate;
 border-spacing: 2px;
 margin-top: 4px;
 table-layout: fixed;
}}

.wd-cell {{
 display: table-cell;
 background-color: #4285F4;
 color: white;
 padding: 10px 4px;
 text-align: center;
 width: 14.28%;
}}

.wd-en {{
 font-size: 11pt;
 font-weight: 700;
 line-height: 1.2;
}}

.wd-kh {{
 font-size: 9pt;
 margin-top: 3px;
}}

/* Grid */
.grid-row {{
 display: table;
 width: 100%;
 border-collapse: separate;
 border-spacing: 2px;
 table-layout: fixed;
}}

.cell {{
 display: table-cell;
 background-color: white;
 border: 1px solid #DADCE0;
 height: 90px;
 padding: 6px;
 vertical-align: top;
 width: 14.28%;
}}

.cell.weekend {{
 background-color: #FFF3E0;
}}

.cell.today {{
 background-color: #E3F2FD;
}}

.cell.other-month {{
 background-color: #F5F5F5;
}}

.cell-num {{
 font-size: 13pt;
 font-weight: 700;
 color: #202124;
 margin-bottom: 4px;
 display: inline-block;
 line-height: 1;
}}

.cell-num.weekend-num {{
 color: #EA4335;
}}

.cell-num.today-num {{
 background-color: #4285F4;
 color: white;
 width: 26px;
 height: 26px;
 line-height: 26px;
 text-align: center;
 border-radius: 50%;
 font-size: 11pt;
}}

.other-month .cell-num {{
 color: #BDBDBD;
}}

.cell-events {{
 margin-top: 2px;
}}

.mini-event {{
 color: white;
 font-size: 7.5pt;
 padding: 2px 5px;
 border-radius: 2px;
 margin-bottom: 2px;
 overflow: hidden;
 text-overflow: ellipsis;
 white-space: nowrap;
 line-height: 1.3;
}}

.more {{
 font-size: 7pt;
 color: #5F6368;
 font-style: italic;
 margin-top: 2px;
}}

.footer {{
 margin-top: 10px;
 text-align: center;
 font-size: 8pt;
 color: #5F6368;
}}
</style>
</head>
<body>
 <div class="header">
 <div class="header-title">{KHMER_MONTHS_NAMES[month]} {year}</div>
 <div class="header-subtitle">សរុប៖ <span class="header-count-num">{total}</span> ព្រឹត្តិការណ៍</div>
 </div>

 <div class="weekdays">
 {weekdays_html}
 </div>

 {rows_html}

 <div class="footer">
 Voice Tracker Bot • {footer_time}
 </div>
</body>
</html>
"""
 return html


def generate_month_calendar_pdf(year=None, month=None):
 """Generate Month PDF using WeasyPrint"""
 now = datetime.now(TZ)
 if year is None:
 year = now.year
 if month is None:
 month = now.month

 first_day = datetime(year, month, 1).date()
 if month == 12:
 last_day = datetime(year + 1, 1, 1).date() - timedelta(days=1)
 else:
 last_day = datetime(year, month + 1, 1).date() - timedelta(days=1)

 all_events = get_all_events()
 events_by_date = defaultdict(list)
 for e in all_events:
 try:
 d = datetime.strptime(e['date'], "%Y-%m-%d").date()
 if first_day <= d <= last_day:
 events_by_date[d].append(e)
 except Exception:
 pass

 for d in events_by_date:
 events_by_date[d].sort(key=lambda x: x['time'] or "99:99")

 today = datetime.now(TZ).date()
 html_content = build_month_html(year, month, events_by_date, today)

 output = BytesIO()
 HTML(string=html_content).write_pdf(output)
 output.seek(0)
 return output

# ══════════════════════════════════════
# 🤖 AI Summary & Insights
# ══════════════════════════════════════

def generate_weekly_summary_ai(week_start, week_end):
 """
 ប្រើ Gemini វិភាគ events សប្តាហ៍មួយ
 Returns: dict {summary, insights, suggestions}
 """
 try:
 events = get_all_events()
 
 # Events in the target week
 week_events = []
 for e in events:
 try:
 d = datetime.strptime(e['date'], "%Y-%m-%d").date()
 if week_start <= d <= week_end:
 week_events.append(e)
 except Exception:
 pass
 
 # Events for next week (for suggestions)
 next_week_start = week_end + timedelta(days=1)
 next_week_end = next_week_start + timedelta(days=6)
 next_week_events = []
 for e in events:
 try:
 d = datetime.strptime(e['date'], "%Y-%m-%d").date()
 if next_week_start <= d <= next_week_end:
 next_week_events.append(e)
 except Exception:
 pass
 
 # Previous week (for comparison)
 prev_week_start = week_start - timedelta(days=7)
 prev_week_end = week_start - timedelta(days=1)
 prev_week_count = sum(1 for e in events
 if prev_week_start <= 
 datetime.strptime(e['date'], "%Y-%m-%d").date() 
 <= prev_week_end)
 
 # Prepare data
 current_week_data = [
 {
 "date": e['date'],
 "time": e['time'],
 "event": e['event'],
 "category": e['category'],
 "status": e['status']
 }
 for e in week_events
 ]
 
 next_week_data = [
 {
 "date": e['date'],
 "time": e['time'],
 "event": e['event'],
 "category": e['category']
 }
 for e in next_week_events
 ]
 
 # Statistics
 cat_counter = Counter(e['category'] for e in week_events)
 status_counter = Counter(e['status'] for e in week_events)
 day_counter = Counter()
 for e in week_events:
 try:
 d = datetime.strptime(e['date'], "%Y-%m-%d")
 day_counter[WEEKDAY_NAMES[d.weekday()]] += 1
 except Exception:
 pass
 
 done_count = status_counter.get(STATUS_DONE, 0)
 pending_count = status_counter.get(STATUS_PENDING, 0)
 total = len(week_events)
 completion_rate = (done_count / total * 100) if total > 0 else 0
 
 # Growth vs last week
 if prev_week_count > 0:
 growth = ((total - prev_week_count) / prev_week_count * 100)
 else:
 growth = 0
 
 # Busiest day
 busiest = day_counter.most_common(1)[0] if day_counter else ("N/A", 0)
 
 # Build stats dict
 stats = {
 "total": total,
 "prev_total": prev_week_count,
 "growth": round(growth, 1),
 "done": done_count,
 "pending": pending_count,
 "completion_rate": round(completion_rate, 1),
 "categories": dict(cat_counter),
 "busiest_day": busiest[0],
 "busiest_count": busiest[1],
 "by_day": dict(day_counter),
 }
 
 # AI Analysis
 prompt = f"""អ្នកគឺជាជំនួយការ AI សម្រាប់ធ្វើ productivity report ជាភាសាខ្មែរ។

ទិន្នន័យសប្តាហ៍នេះ ({week_start.strftime('%Y-%m-%d')} ដល់ {week_end.strftime('%Y-%m-%d')}):

📊 ស្ថិតិ:
{json.dumps(stats, ensure_ascii=False, indent=2)}

📝 Events សប្តាហ៍នេះ:
{json.dumps(current_week_data, ensure_ascii=False, indent=2)}

📅 Events សប្តាហ៍ក្រោយ:
{json.dumps(next_week_data, ensure_ascii=False, indent=2)}

សូមផ្តល់ការវិភាគជា JSON format:
{{
 "highlights": ["ចំណុចលេចធ្លោ 1", "ចំណុចលេចធ្លោ 2", "..."],
 "achievements": ["សមិទ្ធផល 1", "សមិទ្ធផល 2"],
 "concerns": ["បញ្ហាដែលគួរយកចិត្តទុកដាក់ 1", "..."],
 "suggestions": ["ការណែនាំ 1", "ការណែនាំ 2", "ការណែនាំ 3"],
 "next_week_focus": ["ត្រូវផ្តោត 1", "ត្រូវផ្តោត 2"],
 "motivation": "សារលើកទឹកចិត្តសម្រាប់សប្តាហ៍ក្រោយ (1-2 ប្រយោគ)"
}}

ច្បាប់:
1. ប្រើភាសាខ្មែរធម្មជាតិ ស្និទ្ធស្នាល
2. ផ្តោតលើទិន្នន័យពិត មិនប្រឌិត
3. Suggestions ត្រូវជាក់លាក់ អាចធ្វើបាន
4. Motivation ត្រូវវិជ្ជមាន និងផ្ទាល់ខ្លួន
5. បើ events តិច - លើកទឹកចិត្តឲ្យសកម្មជាង
6. បើ events ច្រើន - គោលដៅសម្រាកឲ្យបានគ្រប់គ្រាន់

ឆ្លើយតែ JSON, គ្មានពាក្យបន្ថែម។
"""
 
 response = gemini_client.models.generate_content(
 model="gemini-flash-latest",
 contents=[prompt],
 )
 result = response.text.strip()
 result = re.sub(r"^```json\s*|\s*```$", "", result).strip()
 result = re.sub(r"^```\s*|\s*```$", "", result).strip()
 
 ai_data = json.loads(result)
 
 return {
 "stats": stats,
 "ai": ai_data,
 "week_events": week_events,
 "next_week_events": next_week_events,
 }
 except Exception as e:
 logger.error(f"AI summary error: {e}")
 return None


def format_weekly_summary_telegram(data, week_start, week_end):
 """Format summary for Telegram message"""
 stats = data['stats']
 ai = data['ai']
 
 growth_emoji = "📈" if stats['growth'] >= 0 else "📉"
 growth_text = f"+{stats['growth']}%" if stats['growth'] >= 0 else f"{stats['growth']}%"
 
 msg = f"🤖 *AI Weekly Report*\n"
 msg += f"📅 {week_start.strftime('%Y-%m-%d')} → {week_end.strftime('%Y-%m-%d')}\n\n"
 
 # Overview
 msg += f"📊 *ទិដ្ឋភាពទូទៅ:*\n"
 msg += f" 📝 សរុប: *{stats['total']}* events\n"
 msg += f" {growth_emoji} បើប្រៀបធៀបសប្តាហ៍មុន: *{growth_text}*\n"
 msg += f" ✅ Completion: *{stats['completion_rate']}%* ({stats['done']}/{stats['total']})\n\n"
 
 # Busiest day
 if stats['busiest_count'] > 0:
 msg += f"🏆 *ថ្ងៃរវល់បំផុត:* {stats['busiest_day']} ({stats['busiest_count']} events)\n\n"
 
 # Categories
 if stats['categories']:
 msg += f"🏷 *ចែកតាមប្រភេទ:*\n"
 for cat, count in sorted(stats['categories'].items(), 
 key=lambda x: -x[1]):
 pct = round(count / stats['total'] * 100) if stats['total'] > 0 else 0
 msg += f" {cat}: *{count}* ({pct}%)\n"
 msg += "\n"
 
 # Highlights
 if ai.get('highlights'):
 msg += f"✨ *ចំណុចលេចធ្លោ:*\n"
 for h in ai['highlights'][:3]:
 msg += f" • {h}\n"
 msg += "\n"
 
 # Achievements
 if ai.get('achievements'):
 msg += f"🏆 *សមិទ្ធផល:*\n"
 for a in ai['achievements'][:3]:
 msg += f" ✓ {a}\n"
 msg += "\n"
 
 # Concerns
 if ai.get('concerns'):
 msg += f"⚠️ *ចំណុចត្រូវយកចិត្តទុកដាក់:*\n"
 for c in ai['concerns'][:3]:
 msg += f" ! {c}\n"
 msg += "\n"
 
 # Suggestions
 if ai.get('suggestions'):
 msg += f"💡 *ការណែនាំ AI:*\n"
 for s in ai['suggestions'][:4]:
 msg += f" • {s}\n"
 msg += "\n"
 
 # Next week focus
 if ai.get('next_week_focus'):
 msg += f"🎯 *គោលដៅសប្តាហ៍ក្រោយ:*\n"
 for f in ai['next_week_focus'][:3]:
 msg += f" ✓ {f}\n"
 msg += "\n"
 
 # Motivation
 if ai.get('motivation'):
 msg += f"💪 *លើកទឹកចិត្ត:*\n_{ai['motivation']}_\n\n"
 
 msg += f"━━━━━━━━━━━━━━━━━━━━\n"
 msg += f"🤖 _Powered by Gemini AI_"
 
 return msg


def generate_daily_briefing_ai():
 """AI briefing សម្រាប់ថ្ងៃនេះ"""
 try:
 events = get_all_events()
 today = datetime.now(TZ).date()
 today_str = today.strftime("%Y-%m-%d")
 tomorrow_str = (today + timedelta(days=1)).strftime("%Y-%m-%d")
 
 today_events = [e for e in events if e['date'] == today_str]
 tomorrow_events = [e for e in events if e['date'] == tomorrow_str]
 
 today_events.sort(key=lambda x: x['time'] or "99:99")
 
 if not today_events and not tomorrow_events:
 return None
 
 prompt = f"""អ្នកគឺជាជំនួយការ AI ជាភាសាខ្មែរ។ សូមផ្តល់ daily briefing សម្រាប់ថ្ងៃនេះ។

ថ្ងៃនេះ: {today_str} ({WEEKDAY_NAMES[today.weekday()]})

📅 Events ថ្ងៃនេះ ({len(today_events)}):
{json.dumps([{"time": e['time'], "event": e['event'], "category": e['category']} for e in today_events], ensure_ascii=False, indent=2)}

📅 Events ថ្ងៃស្អែក ({len(tomorrow_events)}):
{json.dumps([{"time": e['time'], "event": e['event'], "category": e['category']} for e in tomorrow_events], ensure_ascii=False, indent=2)}

សូមឆ្លើយជា JSON:
{{
 "greeting": "ការស្វាគមន៍ ១ ប្រយោគ (ជាភាសាខ្មែរធម្មជាតិ)",
 "day_overview": "ការវាយតម្លៃថ្ងៃនេះ ១-២ ប្រយោគ (រវល់/ធម្មតា/ស្រួល)",
 "priority_events": ["Event សំខាន់ 1", "Event សំខាន់ 2"],
 "tips": ["Tip 1", "Tip 2"],
 "tomorrow_preview": "សរុបថ្ងៃស្អែក ១ ប្រយោគ (បើមាន)",
 "quote": "ប្រយោគលើកទឹកចិត្ត"
}}

ច្បាប់:
1. ភាសាខ្មែរធម្មជាតិ ស្និទ្ធស្នាល
2. ជាក់លាក់ចំពោះ events ដែលមាន
3. Tips ត្រូវផ្តល់ដំបូន្មានពិតៗ
4. Quote ខ្លី ២-៣ ពាក្យ

ឆ្លើយតែ JSON។
"""
 
 response = gemini_client.models.generate_content(
 model="gemini-flash-latest",
 contents=[prompt],
 )
 result = response.text.strip()
 result = re.sub(r"^```json\s*|\s*```$", "", result).strip()
 result = re.sub(r"^```\s*|\s*```$", "", result).strip()
 
 ai_data = json.loads(result)
 
 return {
 "ai": ai_data,
 "today_events": today_events,
 "tomorrow_events": tomorrow_events,
 "today": today,
 }
 except Exception as e:
 logger.error(f"Daily briefing error: {e}")
 return None


def format_daily_briefing_telegram(data):
 """Format briefing for Telegram"""
 ai = data['ai']
 today_events = data['today_events']
 today = data['today']
 
 weekday = WEEKDAY_NAMES[today.weekday()]
 
 msg = f"🌅 *Daily Briefing*\n"
 msg += f"📅 {today.strftime('%Y-%m-%d')} • ថ្ងៃ{weekday}\n\n"
 
 # Greeting
 if ai.get('greeting'):
 msg += f"👋 {ai['greeting']}\n\n"
 
 # Overview
 if ai.get('day_overview'):
 msg += f"📊 *ថ្ងៃនេះ:*\n_{ai['day_overview']}_\n\n"
 
 # Today's events
 if today_events:
 msg += f"📌 *កាលវិភាគថ្ងៃនេះ ({len(today_events)}):*\n"
 for e in today_events:
 time_str = f"🕐 {e['time']} " if e['time'] else "🕐 --:-- "
 status_icon = "✅" if STATUS_DONE in e['status'] else "⏳"
 msg += f" {status_icon} {time_str}{e['event']}\n"
 msg += "\n"
 else:
 msg += f"📭 គ្មាន events ថ្ងៃនេះទេ - ថ្ងៃទំនេរ!\n\n"
 
 # Priority
 if ai.get('priority_events'):
 msg += f"🎯 *ចាំបាច់បំផុត:*\n"
 for p in ai['priority_events'][:3]:
 msg += f" ⭐ {p}\n"
 msg += "\n"
 
 # Tips
 if ai.get('tips'):
 msg += f"💡 *Tips ថ្ងៃនេះ:*\n"
 for t in ai['tips'][:3]:
 msg += f" • {t}\n"
 msg += "\n"
 
 # Tomorrow
 if ai.get('tomorrow_preview'):
 msg += f"🔮 *ថ្ងៃស្អែក:*\n_{ai['tomorrow_preview']}_\n\n"
 
 # Quote
 if ai.get('quote'):
 msg += f"✨ _{ai['quote']}_\n"
 
 return msg


def generate_insights_ai():
 """AI Insights ពី data ទាំងអស់"""
 try:
 events = get_all_events()
 if len(events) < 5:
 return None
 
 # Prepare stats
 cat_counter = Counter(e['category'] for e in events)
 status_counter = Counter(e['status'] for e in events)
 
 # Monthly breakdown (last 3 months)
 now = datetime.now(TZ)
 monthly = defaultdict(int)
 for e in events:
 try:
 d = datetime.strptime(e['date'], "%Y-%m-%d")
 key = d.strftime("%Y-%m")
 monthly[key] += 1
 except Exception:
 pass
 
 # Time patterns
 time_slots = {"morning": 0, "afternoon": 0, "evening": 0, "night": 0}
 for e in events:
 if e['time']:
 try:
 h = int(e['time'].split(":")[0])
 if 5 <= h < 12:
 time_slots['morning'] += 1
 elif 12 <= h < 17:
 time_slots['afternoon'] += 1
 elif 17 <= h < 21:
 time_slots['evening'] += 1
 else:
 time_slots['night'] += 1
 except Exception:
 pass
 
 # Weekday patterns
 weekday_counter = Counter()
 for e in events:
 try:
 d = datetime.strptime(e['date'], "%Y-%m-%d")
 weekday_counter[WEEKDAY_NAMES[d.weekday()]] += 1
 except Exception:
 pass
 
 completion_rate = (status_counter.get(STATUS_DONE, 0) / len(events) * 100) if events else 0
 
 data = {
 "total_events": len(events),
 "completion_rate": round(completion_rate, 1),
 "categories": dict(cat_counter),
 "monthly": dict(sorted(monthly.items())[-6:]), # Last 6 months
 "time_slots": time_slots,
 "weekdays": dict(weekday_counter),
 }
 
 prompt = f"""អ្នកគឺជា AI data analyst ជាភាសាខ្មែរ។ សូមវិភាគ productivity data ខាងក្រោម និងផ្តល់ insights ជ្រាលជ្រៅ។

ទិន្នន័យ:
{json.dumps(data, ensure_ascii=False, indent=2)}

សូមឆ្លើយជា JSON:
{{
 "productivity_score": 85,
 "productivity_level": "ខ្ពស់/មធ្យម/ទាប",
 "personality_type": "ប្រភេទបុគ្គលិកលក្ខណៈ (ឧ. 'អ្នកគ្រប់គ្រងពេលវេលា', 'អ្នកចូលចិត្តការងារព្រឹក')",
 "key_findings": [
 "រកឃើញ 1",
 "រកឃើញ 2",
 "រកឃើញ 3"
 ],
 "strengths": ["ចំណុចខ្លាំង 1", "ចំណុចខ្លាំង 2"],
 "improvements": ["ចំណុចត្រូវកែ 1", "ចំណុចត្រូវកែ 2"],
 "patterns": [
 "Pattern ដែលរកឃើញ 1",
 "Pattern ដែលរកឃើញ 2"
 ],
 "recommendations": [
 "ការណែនាំ 1",
 "ការណែនាំ 2",
 "ការណែនាំ 3"
 ]
}}

ច្បាប់:
1. Productivity score: 0-100
2. ជាក់លាក់ ផ្អែកលើទិន្នន័យ
3. Pattern គួរបង្ហាញអ្វីមួយ interesting
4. Recommendations អាចធ្វើបាន

ឆ្លើយតែ JSON។
"""
 
 response = gemini_client.models.generate_content(
 model="gemini-flash-latest",
 contents=[prompt],
 )
 result = response.text.strip()
 result = re.sub(r"^```json\s*|\s*```$", "", result).strip()
 result = re.sub(r"^```\s*|\s*```$", "", result).strip()
 
 ai_data = json.loads(result)
 
 return {
 "data": data,
 "ai": ai_data,
 }
 except Exception as e:
 logger.error(f"Insights error: {e}")
 return None


def format_insights_telegram(data):
 """Format insights for Telegram"""
 stats = data['data']
 ai = data['ai']
 
 score = ai.get('productivity_score', 0)
 if score >= 80:
 score_emoji = "🔥"
 elif score >= 60:
 score_emoji = "⭐"
 elif score >= 40:
 score_emoji = "💪"
 else:
 score_emoji = "🌱"
 
 msg = f"🧠 *AI Productivity Insights*\n\n"
 
 # Score
 msg += f"{score_emoji} *Productivity Score:* `{score}/100`\n"
 msg += f"📊 *កម្រិត:* {ai.get('productivity_level', 'N/A')}\n"
 msg += f"👤 *ប្រភេទ:* _{ai.get('personality_type', 'N/A')}_\n\n"
 
 # Stats
 msg += f"📈 *ស្ថិតិសរុប:*\n"
 msg += f" 📝 Events: *{stats['total_events']}*\n"
 msg += f" ✅ Completion: *{stats['completion_rate']}%*\n\n"
 
 # Key findings
 if ai.get('key_findings'):
 msg += f"🔍 *រកឃើញសំខាន់ៗ:*\n"
 for f in ai['key_findings'][:3]:
 msg += f" • {f}\n"
 msg += "\n"
 
 # Strengths
 if ai.get('strengths'):
 msg += f"💪 *ចំណុចខ្លាំង:*\n"
 for s in ai['strengths'][:3]:
 msg += f" ✓ {s}\n"
 msg += "\n"
 
 # Improvements
 if ai.get('improvements'):
 msg += f"🎯 *ចំណុចត្រូវកែ:*\n"
 for i in ai['improvements'][:3]:
 msg += f" ! {i}\n"
 msg += "\n"
 
 # Patterns
 if ai.get('patterns'):
 msg += f"🔬 *Patterns ដែលរកឃើញ:*\n"
 for p in ai['patterns'][:3]:
 msg += f" 🔸 {p}\n"
 msg += "\n"
 
 # Recommendations
 if ai.get('recommendations'):
 msg += f"💡 *ការណែនាំ:*\n"
 for r in ai['recommendations'][:4]:
 msg += f" → {r}\n"
 msg += "\n"
 
 msg += f"━━━━━━━━━━━━━━━━━━━━\n"
 msg += f"🤖 _Powered by Gemini AI_"
 
 return msg

# ══════════════════════════════════════
# Confirmation UI
# ══════════════════════════════════════

def build_confirmation_keyboard(chat_id):
 kb = [
 [
 InlineKeyboardButton("✅ រក្សាទុក", callback_data=f"save:{chat_id}"),
 InlineKeyboardButton("❌ បោះបង់", callback_data=f"cancel:{chat_id}"),
 ],
 [
 InlineKeyboardButton("📅 កែកាលបរិច្ឆេទ", callback_data=f"editdate:{chat_id}"),
 InlineKeyboardButton("🕐 កែម៉ោង", callback_data=f"edittime:{chat_id}"),
 ],
 [
 InlineKeyboardButton("📝 កែព្រឹត្តិការណ៍", callback_data=f"editevent:{chat_id}"),
 InlineKeyboardButton("🏷 កែប្រភេទ", callback_data=f"editcat:{chat_id}"),
 ],
 ]
 return InlineKeyboardMarkup(kb)


def build_category_keyboard(chat_id):
 kb = []
 row = []
 for key, name in CATEGORIES.items():
 row.append(InlineKeyboardButton(name, callback_data=f"setcat:{chat_id}:{key}"))
 if len(row) == 2:
 kb.append(row)
 row = []
 if row:
 kb.append(row)
 kb.append([InlineKeyboardButton("⬅️ ត្រឡប់", callback_data=f"back:{chat_id}")])
 return InlineKeyboardMarkup(kb)


def format_preview(data):
 time_display = data.get('time') or "គ្មាន"
 return (
 f"👀 *ព្រីវ្យូ*\n\n"
 f"📅 កាលបរិច្ឆេទ: `{data['date']}`\n"
 f"🕐 ម៉ោង: `{time_display}`\n"
 f"📝 ព្រឹត្តិការណ៍: {data['event']}\n"
 f"🏷 ប្រភេទ: {CATEGORIES.get(data['category'], CATEGORIES['other'])}\n"
 f"📊 ស្ថានភាព: {STATUS_PENDING}\n"
 )


async def show_confirmation(update_or_msg, chat_id, data, original_text=""):
 pending_events[chat_id] = data
 text = format_preview(data)

 dups = find_duplicates(data['date'], data['event'])
 if dups:
 text += f"\n⚠️ *ស្រដៀងគ្នា ({len(dups)}):*\n"
 for d in dups[:3]:
 text += f" `#{d['id']}` {d['event'][:40]}\n"

 if original_text:
 text += f"\n💬 _{original_text}_"

 kb = build_confirmation_keyboard(chat_id)

 if hasattr(update_or_msg, 'message'):
 await update_or_msg.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")
 else:
 await update_or_msg.reply_text(text, reply_markup=kb, parse_mode="Markdown")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
 query = update.callback_query
 await query.answer()

 parts = query.data.split(":")
 action = parts[0]

 if action == "setstatus":
 row_num = parts[1]
 status_key = parts[2]
 status_map = {
 "pending": STATUS_PENDING,
 "done": STATUS_DONE,
 "cancel": STATUS_CANCEL,
 }
 new_status = status_map.get(status_key, STATUS_PENDING)
 if edit_row(row_num, "status", new_status):
 await query.edit_message_text(f"✅ ស្ថានភាព #{row_num} → {new_status}")
 else:
 await query.edit_message_text(f"❌ រកមិនឃើញ #{row_num}")
 return

 chat_id = int(parts[1])

 if chat_id not in pending_events:
 await query.edit_message_text("❌ អស់សុពលភាព")
 return

 data = pending_events[chat_id]

 if action == "save":
 row_num = save_to_sheet(
 data['date'], data['time'], data['event'], data['category']
 )

 try:
 category_name = CATEGORIES.get(data['category'], CATEGORIES["other"])
 gcal_id = push_to_google_calendar(
 data['date'], data['time'], data['event'], category_name
 )
 if gcal_id:
 update_gcal_id(row_num, gcal_id)
 except Exception as e:
 logger.warning(f"Auto-push failed: {e}")

 extra_count = 0
 if data.get('is_recurring') and data.get('recurring_day') is not None:
 base_date = datetime.strptime(data['date'], "%Y-%m-%d").date()
 for w in range(1, 9):
 next_date = base_date + timedelta(weeks=w)
 new_row = save_to_sheet(
 next_date.strftime("%Y-%m-%d"),
 data['time'], data['event'], data['category']
 )
 try:
 gid = push_to_google_calendar(
 next_date.strftime("%Y-%m-%d"),
 data['time'], data['event'],
 CATEGORIES.get(data['category'], CATEGORIES["other"])
 )
 if gid:
 update_gcal_id(new_row, gid)
 except Exception:
 pass
 extra_count += 1

 pending_events.pop(chat_id, None)
 msg = f"✅ *រក្សាទុក!* `#{row_num}`\n"
 msg += f"📅 {data['date']} {data['time']}\n"
 msg += f"📝 {data['event']}\n"
 msg += f"🔗 Synced ទៅ Google Calendar"
 if extra_count:
 msg += f"\n🔁 បន្ថែម {extra_count} events (recurring)"
 await query.edit_message_text(msg, parse_mode="Markdown")

 elif action == "cancel":
 pending_events.pop(chat_id, None)
 await query.edit_message_text("❌ បោះបង់ហើយ")

 elif action == "editcat":
 kb = build_category_keyboard(chat_id)
 await query.edit_message_text(
 format_preview(data) + "\n👇 ជ្រើសរើសប្រភេទ:",
 reply_markup=kb, parse_mode="Markdown"
 )

 elif action == "setcat":
 cat_key = parts[2]
 data['category'] = cat_key
 pending_events[chat_id] = data
 kb = build_confirmation_keyboard(chat_id)
 await query.edit_message_text(
 format_preview(data), reply_markup=kb, parse_mode="Markdown"
 )

 elif action == "back":
 kb = build_confirmation_keyboard(chat_id)
 await query.edit_message_text(
 format_preview(data), reply_markup=kb, parse_mode="Markdown"
 )

 elif action in ("editdate", "edittime", "editevent"):
 field_map = {
 "editdate": ("date", "កាលបរិច្ឆេទ (YYYY-MM-DD)"),
 "edittime": ("time", "ម៉ោង (HH:MM ឬ 'គ្មាន')"),
 "editevent": ("event", "ព្រឹត្តិការណ៍ថ្មី"),
 }
 field, label = field_map[action]
 context.user_data['editing_field'] = field
 context.user_data['editing_chat'] = chat_id
 await query.edit_message_text(
 f"✏️ សូមវាយ *{label}* ថ្មី:\n(ឬវាយ /cancel)",
 parse_mode="Markdown"
 )


# ══════════════════════════════════════
# Command Handlers
# ══════════════════════════════════════

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
 welcome = (
 "🎙️ *Voice Tracker Bot v3.2*\n\n"
 "📌 *របៀបប្រើ:*\n"
 "🎤 ផ្ញើសំឡេងខ្មែរ\n"
 "📸 ផ្ញើរូបភាព\n"
 "⌨️ វាយអក្សរផ្ទាល់\n\n"
 "📋 *Commands:*\n"
 "/today - 📌 ព្រឹត្តិការណ៍ថ្ងៃនេះ\n"
 "/week - 📄 កាលវិភាគសប្តាហ៍ (PDF)\n"
 "/nextweek - 📄 សប្តាហ៍ក្រោយ (PDF)\n"
 "/month - 📄 កាលវិភាគខែ (PDF)\n"
 "/calendar - 📅 ៣០ ថ្ងៃខាងមុខ\n"
 "/stats - 📊 ស្ថិតិខែនេះ\n"
 "/history - 📋 ១០ ចុងក្រោយ\n"
 "/search <ពាក្យ> - 🔍 ស្វែងរក\n"
 "/status <លេខ> - 📊 កែស្ថានភាព\n"
 "/edit <លេខ> <អត្ថបទ> - ✏️ កែ\n"
 "/delete <លេខ> - 🗑 លុប\n"
 "/sort - 📶 រៀបតាមកាលបរិច្ឆេទ\n"
 "/sync - 🔄 Sync ពី Google Calendar\n"
 "/export - 📥 Export .ics\n\n"
 "🤖 *AI Features:*\n"
 "/summary - 📊 AI Weekly Report\n"
 "/briefing - 🌅 Daily Briefing\n"
 "/insights - 🧠 AI Insights\n\n"
 "/help - 📖 ជំនួយ\n\n"
 "🔄 *Auto Sync:*\n"
 "_Google Calendar ⇄ Bot រៀងរាល់ ១៥ នាទី_\n\n"
 "🔔 *Reminders:*\n"
 "_• ព្រឹករាល់ថ្ងៃ ៧:០០ - Daily Briefing_\n"
 "_• សុក្រ ៨:០០ - Weekly Calendar PDF_\n"
 "_• អាទិត្យ ២០:០០ - AI Weekly Summary_\n"
 "_• ១ ថ្ងៃមុន event_\n"
 "_• ១ ម៉ោងមុន event_"
 )
 await update.message.reply_text(welcome, parse_mode="Markdown")


async def help_command(update, context):
 await start_command(update, context)


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
 try:
 events = get_all_events()
 today = datetime.now(TZ).date().strftime("%Y-%m-%d")
 today_events = [e for e in events if e['date'] == today]
 if not today_events:
 await update.message.reply_text("📭 គ្មានព្រឹត្តិការណ៍ថ្ងៃនេះទេ")
 return
 today_events.sort(key=lambda x: x['time'] or "99:99")
 msg = f"📌 *ព្រឹត្តិការណ៍ថ្ងៃនេះ ({today}):*\n\n"
 for e in today_events:
 time_str = f"🕐 {e['time']} " if e['time'] else ""
 msg += f"`#{e['id']}` {time_str}{e['event']}\n {e['category']} • {e['status']}\n\n"
 await update.message.reply_text(msg, parse_mode="Markdown")
 except Exception as e:
 await update.message.reply_text(f"❌ {e}")


async def week_command(update, context):
 await update.message.reply_text("📄 កំពុងបង្កើត PDF ស្អាតៗ...")
 try:
 today = datetime.now(TZ).date()
 monday = today - timedelta(days=today.weekday())
 loop = asyncio.get_event_loop()
 pdf_bytes = await loop.run_in_executor(
 None, generate_week_calendar_pdf, monday
 )
 end = monday + timedelta(days=6)

 pdf_bytes.name = f"week_{monday.strftime('%Y%m%d')}.pdf"
 caption = (f"📅 *កាលវិភាគសប្តាហ៍នេះ*\n"
 f"📆 {monday.strftime('%Y-%m-%d')} → {end.strftime('%Y-%m-%d')}")
 await update.message.reply_document(
 document=InputFile(pdf_bytes, filename=pdf_bytes.name),
 caption=caption,
 parse_mode="Markdown"
 )
 except Exception as e:
 logger.error(f"Week PDF error: {e}")
 await update.message.reply_text(f"❌ {e}")


async def nextweek_command(update, context):
 await update.message.reply_text("📄 កំពុងបង្កើត PDF ស្អាតៗ...")
 try:
 today = datetime.now(TZ).date()
 monday = today - timedelta(days=today.weekday()) + timedelta(days=7)
 loop = asyncio.get_event_loop()
 pdf_bytes = await loop.run_in_executor(
 None, generate_week_calendar_pdf, monday
 )
 end = monday + timedelta(days=6)

 pdf_bytes.name = f"week_{monday.strftime('%Y%m%d')}.pdf"
 caption = (f"📅 *កាលវិភាគសប្តាហ៍ក្រោយ*\n"
 f"📆 {monday.strftime('%Y-%m-%d')} → {end.strftime('%Y-%m-%d')}")
 await update.message.reply_document(
 document=InputFile(pdf_bytes, filename=pdf_bytes.name),
 caption=caption,
 parse_mode="Markdown"
 )
 except Exception as e:
 logger.error(f"NextWeek PDF error: {e}")
 await update.message.reply_text(f"❌ {e}")


async def month_command(update, context):
 await update.message.reply_text("📄 កំពុងបង្កើត PDF ខែ...")
 try:
 now = datetime.now(TZ)
 year = now.year
 month = now.month
 if context.args:
 try:
 parts = context.args[0].split("-")
 year = int(parts[0])
 month = int(parts[1])
 except Exception:
 pass

 loop = asyncio.get_event_loop()
 pdf_bytes = await loop.run_in_executor(
 None, generate_month_calendar_pdf, year, month
 )
 pdf_bytes.name = f"month_{year}_{month:02d}.pdf"
 caption = f"📅 *កាលវិភាគខែ {KHMER_MONTHS_NAMES[month]} {year}*"
 await update.message.reply_document(
 document=InputFile(pdf_bytes, filename=pdf_bytes.name),
 caption=caption,
 parse_mode="Markdown"
 )
 except Exception as e:
 logger.error(f"Month PDF error: {e}")
 await update.message.reply_text(f"❌ {e}")


async def history_command(update, context):
 try:
 events = get_all_events()
 if not events:
 await update.message.reply_text("📭 មិនទាន់មានទេ")
 return
 msg = "📋 *១០ ចុងក្រោយ:*\n\n"
 for e in events[-10:]:
 time_str = f"🕐 {e['time']} " if e['time'] else ""
 msg += f"`#{e['id']}` 📅 {e['date']} {time_str}\n"
 msg += f" 📝 {e['event']}\n"
 msg += f" {e['category']} • {e['status']}\n\n"
 await update.message.reply_text(msg, parse_mode="Markdown")
 except Exception as e:
 await update.message.reply_text(f"❌ {e}")


async def stats_command(update, context):
 try:
 events = get_all_events()
 if not events:
 await update.message.reply_text("📭 មិនទាន់មានទេ")
 return
 now = datetime.now(TZ)
 current_month = now.strftime("%Y-%m")
 month_events = [e for e in events if e['date'].startswith(current_month)]

 weekday_counter = Counter()
 cat_counter = Counter()
 status_counter = Counter()
 for e in month_events:
 try:
 d = datetime.strptime(e['date'], "%Y-%m-%d")
 weekday_counter[WEEKDAY_NAMES[d.weekday()]] += 1
 cat_counter[e.get('category', 'other')] += 1
 status_counter[e.get('status', STATUS_PENDING)] += 1
 except Exception:
 pass

 msg = f"📊 *ស្ថិតិខែ {KHMER_MONTHS_NAMES[now.month]} {now.year}*\n\n"
 msg += f"📝 ខែនេះ: *{len(month_events)}*\n"
 msg += f"🗂 សរុប: *{len(events)}*\n\n"

 if status_counter:
 msg += "*📊 ស្ថានភាព:*\n"
 for s, c in status_counter.most_common():
 msg += f" {s}: *{c}*\n"
 msg += "\n"

 if cat_counter:
 msg += "*🏷 ប្រភេទ:*\n"
 for c, cnt in cat_counter.most_common():
 msg += f" {c}: *{cnt}*\n"
 msg += "\n"

 if weekday_counter:
 msg += "*📅 តាមថ្ងៃ:*\n"
 for day, count in weekday_counter.most_common():
 bar = "▓" * min(count, 10)
 msg += f"`{day:12}` {bar} {count}\n"

 await update.message.reply_text(msg, parse_mode="Markdown")
 except Exception as e:
 await update.message.reply_text(f"❌ {e}")


async def calendar_command(update, context):
 try:
 events = get_all_events()
 today = datetime.now(TZ).date()
 by_date = defaultdict(list)
 for e in events:
 try:
 d = datetime.strptime(e['date'], "%Y-%m-%d").date()
 if today <= d <= today + timedelta(days=30):
 by_date[d].append(e)
 except Exception:
 pass

 if not by_date:
 await update.message.reply_text("📅 គ្មានព្រឹត្តិការណ៍ ៣០ ថ្ងៃខាងមុខទេ")
 return

 msg = "📅 *ព្រឹត្តិការណ៍ ៣០ ថ្ងៃ*\n\n"
 for d in sorted(by_date.keys()):
 wd = WEEKDAY_NAMES[d.weekday()]
 diff = (d - today).days
 if diff == 0:
 label = "🔴 ថ្ងៃនេះ"
 elif diff == 1:
 label = "🟡 ថ្ងៃស្អែក"
 else:
 label = f"🟢 នៅ {diff} ថ្ងៃទៀត"
 msg += f"📌 *{d.strftime('%Y-%m-%d')}* ({wd}) {label}\n"
 for e in sorted(by_date[d], key=lambda x: x['time'] or "99:99"):
 time_str = f"🕐 {e['time']} " if e['time'] else ""
 msg += f" `#{e['id']}` {time_str}{e['event']}\n"
 msg += "\n"

 if len(msg) > 4000:
 for i in range(0, len(msg), 4000):
 await update.message.reply_text(msg[i:i+4000], parse_mode="Markdown")
 else:
 await update.message.reply_text(msg, parse_mode="Markdown")
 except Exception as e:
 await update.message.reply_text(f"❌ {e}")


async def delete_command(update, context):
 try:
 if not context.args:
 await update.message.reply_text("❌ ប្រើ: /delete <លេខ>")
 return
 row_num = context.args[0]
 if delete_row(row_num):
 await update.message.reply_text(f"✅ លុប #{row_num}! (ទាំង Google Calendar)")
 else:
 await update.message.reply_text(f"❌ រកមិនឃើញ #{row_num}")
 except Exception as e:
 await update.message.reply_text(f"❌ {e}")


async def edit_command(update, context):
 try:
 if len(context.args) < 2:
 await update.message.reply_text("❌ ប្រើ: /edit <លេខ> <អត្ថបទថ្មី>")
 return
 row_num = context.args[0]
 new_event = " ".join(context.args[1:])
 if edit_row(row_num, "event", new_event):
 await update.message.reply_text(f"✅ កែ #{row_num}: {new_event}")
 else:
 await update.message.reply_text(f"❌ រកមិនឃើញ #{row_num}")
 except Exception as e:
 await update.message.reply_text(f"❌ {e}")


async def status_command(update, context):
 try:
 if not context.args:
 await update.message.reply_text("❌ ប្រើ: /status <លេខ>")
 return
 row_num = context.args[0]
 events = get_all_events()
 target = next((e for e in events if str(e['id']) == str(row_num)), None)
 if not target:
 await update.message.reply_text(f"❌ រកមិនឃើញ #{row_num}")
 return

 kb = InlineKeyboardMarkup([[
 InlineKeyboardButton("⏳ រង់ចាំ", callback_data=f"setstatus:{row_num}:pending"),
 InlineKeyboardButton("✅ រួចរាល់", callback_data=f"setstatus:{row_num}:done"),
 InlineKeyboardButton("❌ បោះបង់", callback_data=f"setstatus:{row_num}:cancel"),
 ]])
 await update.message.reply_text(
 f"📊 #{row_num}: {target['event']}\n"
 f"ស្ថានភាព: {target['status']}\n\n"
 f"ជ្រើសរើសថ្មី:",
 reply_markup=kb
 )
 except Exception as e:
 await update.message.reply_text(f"❌ {e}")


async def search_command(update, context):
 try:
 if not context.args:
 await update.message.reply_text("❌ ប្រើ: /search <ពាក្យ>")
 return
 keyword = " ".join(context.args)
 results = search_events(keyword)
 if not results:
 await update.message.reply_text(f"🔍 រកមិនឃើញ: {keyword}")
 return
 msg = f"🔍 *លទ្ធផលរក '{keyword}' ({len(results)}):*\n\n"
 for e in results[:20]:
 time_str = f"🕐 {e['time']} " if e['time'] else ""
 msg += f"`#{e['id']}` 📅 {e['date']} {time_str}\n 📝 {e['event']}\n\n"
 if len(results) > 20:
 msg += f"_...និង {len(results)-20} ទៀត_"
 await update.message.reply_text(msg, parse_mode="Markdown")
 except Exception as e:
 await update.message.reply_text(f"❌ {e}")


async def sort_command(update, context):
 try:
 await update.message.reply_text("🔄 កំពុងរៀបចំ...")
 loop = asyncio.get_event_loop()
 ok = await loop.run_in_executor(None, sort_sheet_by_date)
 if ok:
 await update.message.reply_text("✅ រៀបតាមកាលបរិច្ឆេទរួចហើយ!")
 else:
 await update.message.reply_text("❌ បញ្ហាក្នុងការរៀប")
 except Exception as e:
 await update.message.reply_text(f"❌ {e}")


async def export_command(update, context):
 try:
 events = get_all_events()
 if not events:
 await update.message.reply_text("📭 គ្មានព្រឹត្តិការណ៍ទេ")
 return
 ics_data = generate_ics(events)
 buf = BytesIO(ics_data)
 buf.name = f"voice_tracker_{datetime.now(TZ).strftime('%Y%m%d')}.ics"
 await update.message.reply_document(
 document=InputFile(buf, filename=buf.name),
 caption=f"📥 Export {len(events)} events\n💡 Import to Google/Apple Calendar"
 )
 except Exception as e:
 await update.message.reply_text(f"❌ {e}")


async def sync_command(update, context):
 await update.message.reply_text("🔄 កំពុង sync ពី Google Calendar...")
 try:
 loop = asyncio.get_event_loop()
 added, updated = await loop.run_in_executor(None, sync_from_google_calendar)
 msg = f"✅ *Sync រួចរាល់*\n\n"
 msg += f"➕ ថ្មី: *{added}* events\n"
 msg += f"✏️ កែ: *{updated}* events"
 await update.message.reply_text(msg, parse_mode="Markdown")
 except Exception as e:
 await update.message.reply_text(f"❌ {e}")


async def cancel_command(update, context):
 context.user_data.pop('editing_field', None)
 context.user_data.pop('editing_chat', None)
 await update.message.reply_text("❌ បោះបង់")

async def summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
 """AI Weekly Summary"""
 await update.message.reply_text("🤖 កំពុងវិភាគសប្តាហ៍ដោយ AI...")
 try:
 today = datetime.now(TZ).date()
 # Last week
 week_end = today - timedelta(days=today.weekday() + 1) # Last Sunday
 week_start = week_end - timedelta(days=6) # Last Monday
 
 loop = asyncio.get_event_loop()
 data = await loop.run_in_executor(
 None, generate_weekly_summary_ai, week_start, week_end
 )
 
 if not data:
 await update.message.reply_text("❌ មិនអាចវិភាគបានទេ")
 return
 
 msg = format_weekly_summary_telegram(data, week_start, week_end)
 
 if len(msg) > 4000:
 for i in range(0, len(msg), 4000):
 await update.message.reply_text(msg[i:i+4000], parse_mode="Markdown")
 else:
 await update.message.reply_text(msg, parse_mode="Markdown")
 except Exception as e:
 logger.error(f"Summary command error: {e}")
 await update.message.reply_text(f"❌ {e}")


async def briefing_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
 """AI Daily Briefing"""
 await update.message.reply_text("🌅 កំពុងរៀបចំ briefing...")
 try:
 loop = asyncio.get_event_loop()
 data = await loop.run_in_executor(None, generate_daily_briefing_ai)
 
 if not data:
 await update.message.reply_text("📭 គ្មាន events សម្រាប់ briefing")
 return
 
 msg = format_daily_briefing_telegram(data)
 await update.message.reply_text(msg, parse_mode="Markdown")
 except Exception as e:
 logger.error(f"Briefing command error: {e}")
 await update.message.reply_text(f"❌ {e}")


async def insights_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
 """AI Insights ពី data ទាំងអស់"""
 await update.message.reply_text("🧠 កំពុងវិភាគ insights ជ្រាលជ្រៅ...")
 try:
 loop = asyncio.get_event_loop()
 data = await loop.run_in_executor(None, generate_insights_ai)
 
 if not data:
 await update.message.reply_text("📭 ត្រូវការ events យ៉ាងតិច ៥ ដើម្បីវិភាគ")
 return
 
 msg = format_insights_telegram(data)
 
 if len(msg) > 4000:
 for i in range(0, len(msg), 4000):
 await update.message.reply_text(msg[i:i+4000], parse_mode="Markdown")
 else:
 await update.message.reply_text(msg, parse_mode="Markdown")
 except Exception as e:
 logger.error(f"Insights command error: {e}")
 await update.message.reply_text(f"❌ {e}")

# ══════════════════════════════════════
# Message Handlers
# ══════════════════════════════════════

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
 await update.message.reply_text("🎧 កំពុងស្តាប់...")
 try:
 voice = update.message.voice or update.message.audio
 file = await context.bot.get_file(voice.file_id)
 file_path = f"/tmp/{voice.file_id}.ogg"
 await file.download_to_drive(file_path)

 await update.message.reply_text("🔄 កំពុងបំលែង...")
 loop = asyncio.get_event_loop()
 text = await loop.run_in_executor(None, transcribe_audio, file_path)

 if not text:
 await update.message.reply_text("❌ មិនអាចបំលែងបាន")
 return

 data = await loop.run_in_executor(None, parse_with_ai, text)
 chat_id = update.effective_chat.id
 await show_confirmation(update, chat_id, data, original_text=text)

 if os.path.exists(file_path):
 os.remove(file_path)
 except Exception as e:
 logger.error(f"Voice error: {e}")
 await update.message.reply_text(f"❌ {e}")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
 await update.message.reply_text("📸 កំពុងវិភាគ...")
 try:
 photo = update.message.photo[-1]
 file = await context.bot.get_file(photo.file_id)
 file_path = f"/tmp/{photo.file_id}.jpg"
 await file.download_to_drive(file_path)

 loop = asyncio.get_event_loop()
 img_data = await loop.run_in_executor(None, extract_from_image, file_path)

 if not img_data:
 await update.message.reply_text("❌ វិភាគមិនបាន")
 return

 data = {
 "date": img_data['date'],
 "time": img_data.get('time', ''),
 "event": img_data['event'],
 "category": detect_category(img_data['event']),
 "is_recurring": False,
 "recurring_day": None,
 }
 chat_id = update.effective_chat.id
 await show_confirmation(update, chat_id, data, original_text="[រូបភាព]")

 if os.path.exists(file_path):
 os.remove(file_path)
 except Exception as e:
 await update.message.reply_text(f"❌ {e}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
 text = update.message.text
 if not text or text.startswith("/"):
 return

 if 'editing_field' in context.user_data:
 field = context.user_data.pop('editing_field')
 chat_id = context.user_data.pop('editing_chat', None)
 if chat_id and chat_id in pending_events:
 data = pending_events[chat_id]
 if field == "date":
 try:
 datetime.strptime(text.strip(), "%Y-%m-%d")
 data['date'] = text.strip()
 except ValueError:
 await update.message.reply_text("❌ Format ខុស (YYYY-MM-DD)")
 context.user_data['editing_field'] = field
 context.user_data['editing_chat'] = chat_id
 return
 elif field == "time":
 if text.strip().lower() in ("គ្មាន", "none", ""):
 data['time'] = ""
 else:
 try:
 datetime.strptime(text.strip(), "%H:%M")
 data['time'] = text.strip()
 except ValueError:
 await update.message.reply_text("❌ Format ខុស (HH:MM)")
 context.user_data['editing_field'] = field
 context.user_data['editing_chat'] = chat_id
 return
 elif field == "event":
 data['event'] = text.strip()
 pending_events[chat_id] = data
 await show_confirmation(update, chat_id, data)
 return

 try:
 loop = asyncio.get_event_loop()
 data = await loop.run_in_executor(None, parse_with_ai, text)
 chat_id = update.effective_chat.id
 await show_confirmation(update, chat_id, data, original_text=text)
 except Exception as e:
 await update.message.reply_text(f"❌ {e}")


# ══════════════════════════════════════
# Scheduled Jobs
# ══════════════════════════════════════

async def send_weekly_reminder(context: ContextTypes.DEFAULT_TYPE):
 logger.info("🔔 Weekly PDF reminder...")
 if not CHAT_ID:
 return
 try:
 today = datetime.now(TZ).date()
 next_monday = today + timedelta(days=(7 - today.weekday()))
 loop = asyncio.get_event_loop()
 pdf_bytes = await loop.run_in_executor(
 None, generate_week_calendar_pdf, next_monday
 )
 end = next_monday + timedelta(days=6)

 pdf_bytes.name = f"week_{next_monday.strftime('%Y%m%d')}.pdf"
 caption = (f"🔔 *រំលឹកកាលវិភាគសប្តាហ៍ក្រោយ*\n\n"
 f"📅 {next_monday.strftime('%Y-%m-%d')} → {end.strftime('%Y-%m-%d')}\n\n"
 f"💡 សូមរៀបចំខ្លួនសម្រាប់សប្តាហ៍ថ្មី!")
 await context.bot.send_document(
 chat_id=CHAT_ID,
 document=InputFile(pdf_bytes, filename=pdf_bytes.name),
 caption=caption,
 parse_mode="Markdown"
 )
 logger.info("✅ Weekly PDF sent!")
 except Exception as e:
 logger.error(f"Weekly reminder error: {e}")


async def send_personal_reminders(context: ContextTypes.DEFAULT_TYPE):
 if not CHAT_ID:
 return
 try:
 now = datetime.now(TZ)
 events = get_all_events()

 for e in events:
 if STATUS_PENDING not in e.get('status', ''):
 continue
 try:
 date = datetime.strptime(e['date'], "%Y-%m-%d")
 if e['time']:
 try:
 t = datetime.strptime(e['time'], "%H:%M").time()
 event_dt = TZ.localize(datetime.combine(date.date(), t))
 except Exception:
 event_dt = TZ.localize(date.replace(hour=9))
 else:
 event_dt = TZ.localize(date.replace(hour=9))

 diff_min = (event_dt - now).total_seconds() / 60

 if 1410 <= diff_min <= 1470:
 msg = (f"🔔 *រំលឹក ១ ថ្ងៃមុន*\n\n"
 f"📅 {e['date']} {e['time']}\n"
 f"📝 {e['event']}\n"
 f"🏷 {e['category']}")
 await context.bot.send_message(chat_id=CHAT_ID, text=msg,
 parse_mode="Markdown")
 elif 30 <= diff_min <= 90:
 msg = (f"⏰ *រំលឹក ១ ម៉ោងមុន*\n\n"
 f"📅 {e['date']} {e['time']}\n"
 f"📝 {e['event']}")
 await context.bot.send_message(chat_id=CHAT_ID, text=msg,
 parse_mode="Markdown")
 except Exception as ex:
 logger.warning(f"Reminder check #{e.get('id')}: {ex}")
 except Exception as e:
 logger.error(f"Personal reminder error: {e}")

async def send_weekly_ai_summary(context: ContextTypes.DEFAULT_TYPE):
 """AI Weekly Summary - Sunday 20:00"""
 logger.info("🤖 Sending AI weekly summary...")
 if not CHAT_ID:
 return
 try:
 today = datetime.now(TZ).date()
 # Current week (Mon-Sun)
 week_start = today - timedelta(days=today.weekday())
 week_end = week_start + timedelta(days=6)
 
 loop = asyncio.get_event_loop()
 data = await loop.run_in_executor(
 None, generate_weekly_summary_ai, week_start, week_end
 )
 
 if not data:
 logger.warning("No data for weekly summary")
 return
 
 msg = format_weekly_summary_telegram(data, week_start, week_end)
 
 if len(msg) > 4000:
 for i in range(0, len(msg), 4000):
 await context.bot.send_message(
 chat_id=CHAT_ID,
 text=msg[i:i+4000],
 parse_mode="Markdown"
 )
 else:
 await context.bot.send_message(
 chat_id=CHAT_ID,
 text=msg,
 parse_mode="Markdown"
 )
 logger.info("✅ Weekly AI summary sent!")
 except Exception as e:
 logger.error(f"Weekly AI summary error: {e}")


async def send_daily_briefing(context: ContextTypes.DEFAULT_TYPE):
 """Daily Briefing - 07:00 AM"""
 logger.info("🌅 Sending daily briefing...")
 if not CHAT_ID:
 return
 try:
 loop = asyncio.get_event_loop()
 data = await loop.run_in_executor(None, generate_daily_briefing_ai)
 
 if not data:
 logger.info("No events for briefing")
 return
 
 msg = format_daily_briefing_telegram(data)
 await context.bot.send_message(
 chat_id=CHAT_ID,
 text=msg,
 parse_mode="Markdown"
 )
 logger.info("✅ Daily briefing sent!")
 except Exception as e:
 logger.error(f"Daily briefing error: {e}")

async def sync_calendar_job(context: ContextTypes.DEFAULT_TYPE):
 try:
 loop = asyncio.get_event_loop()
 added, updated = await loop.run_in_executor(None, sync_from_google_calendar)

 if added > 0 and CHAT_ID:
 msg = f"🔄 *Sync ពី Google Calendar*\n\n"
 msg += f"➕ ថ្មី: *{added}* events\n"
 if updated > 0:
 msg += f"✏️ កែ: *{updated}* events\n"
 msg += f"\nប្រើ /today ឬ /week ដើម្បីមើល"
 await context.bot.send_message(
 chat_id=CHAT_ID, text=msg, parse_mode="Markdown"
 )
 except Exception as e:
 logger.error(f"Sync job error: {e}")


# ══════════════════════════════════════
# Flask
# ══════════════════════════════════════

flask_app = Flask(__name__)


@flask_app.route("/")
def home():
 return "🤖 Voice Tracker Bot v3.2 is running!"


@flask_app.route("/health")
def health():
 return {"status": "ok", "version": "3.2"}


@flask_app.route("/calendar/<secret>")
def calendar_feed(secret):
 if secret != CALENDAR_SECRET:
 return "Unauthorized", 401
 try:
 events = get_all_events()
 ics_data = generate_ics(events)
 return Response(
 ics_data,
 mimetype="text/calendar",
 headers={
 "Content-Disposition": "inline; filename=voice_tracker.ics",
 "Cache-Control": "no-cache, must-revalidate",
 }
 )
 except Exception as e:
 logger.error(f"Calendar feed error: {e}")
 return "Error", 500


def run_flask():
 flask_app.run(host="0.0.0.0", port=PORT)


# ══════════════════════════════════════
# Main
# ══════════════════════════════════════

def run_bot():
 logger.info("🚀 Starting Bot v3.2...")
 app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

 app.add_handler(CommandHandler("start", start_command))
 app.add_handler(CommandHandler("help", help_command))
 app.add_handler(CommandHandler("today", today_command))
 app.add_handler(CommandHandler("week", week_command))
 app.add_handler(CommandHandler("nextweek", nextweek_command))
 app.add_handler(CommandHandler("month", month_command))
 app.add_handler(CommandHandler("history", history_command))
 app.add_handler(CommandHandler("stats", stats_command))
 app.add_handler(CommandHandler("calendar", calendar_command))
 app.add_handler(CommandHandler("delete", delete_command))
 app.add_handler(CommandHandler("edit", edit_command))
 app.add_handler(CommandHandler("status", status_command))
 app.add_handler(CommandHandler("search", search_command))
 app.add_handler(CommandHandler("sort", sort_command))
 app.add_handler(CommandHandler("export", export_command))
 app.add_handler(CommandHandler("sync", sync_command))
 app.add_handler(CommandHandler("cancel", cancel_command))
 app.add_handler(CommandHandler("summary", summary_command))
 app.add_handler(CommandHandler("briefing", briefing_command))
 app.add_handler(CommandHandler("insights", insights_command))

 app.add_handler(CallbackQueryHandler(button_handler))

 app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
 app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
 app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

 job_queue = app.job_queue

 reminder_time = dtime(hour=8, minute=0, tzinfo=TZ)
 job_queue.run_daily(
 send_weekly_reminder, time=reminder_time, days=(4,),
 name="weekly_reminder"
 )
 logger.info("📅 Weekly reminder: Friday 8:00 AM")

 job_queue.run_repeating(
 send_personal_reminders, interval=1800, first=60,
 name="personal_reminders"
 )
 logger.info("⏰ Personal reminders: Every 30 min")

 job_queue.run_repeating(
 sync_calendar_job, interval=900, first=30,
 name="calendar_sync"
 )
 logger.info("🔄 Google Calendar sync: Every 15 min")
​​ 
 # 🤖 AI Weekly Summary - Sunday 20:00
 weekly_ai_time = dtime(hour=20, minute=0, tzinfo=TZ)
 job_queue.run_daily(
 send_weekly_ai_summary, time=weekly_ai_time, days=(6,), # Sunday
 name="weekly_ai_summary"
 )
 logger.info("🤖 Weekly AI summary: Sunday 8:00 PM")
 
 # 🌅 Daily Briefing - Every morning 07:00
 briefing_time = dtime(hour=7, minute=0, tzinfo=TZ)
 job_queue.run_daily(
 send_daily_briefing, time=briefing_time,
 name="daily_briefing"
 )
 logger.info("🌅 Daily briefing: Every day 7:00 AM")

 logger.info("✅ Bot v3.2 is running!")
 app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
 flask_thread = threading.Thread(target=run_flask, daemon=True)
 flask_thread.start()
 logger.info(f"🌐 Flask started on port {PORT}")
 run_bot()
