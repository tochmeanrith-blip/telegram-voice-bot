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
# 📄 PDF Calendar Generator (WeasyPrint)
# ══════════════════════════════════════

CATEGORY_COLOR_MAP = {
    "🏢 ការងារ": "#4285F4",
    "👨‍👩‍👧 គ្រួសារ": "#EA4335",
    "💊 សុខភាព": "#34A853",
    "🎉 ព្រឹត្តិការណ៍": "#FBBC04",
    "📚 សិក្សា": "#9C27B0",
    "📌 ផ្សេងៗ": "#00ACC1",
}

FONT_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+Khmer:wght@400;500;700&family=Noto+Color+Emoji&display=swap');
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
                extra_style = "opacity: 0.5; text-decoration: line-through;"

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
{FONT_CSS}

@page {{
    size: A4 landscape;
    margin: 8mm;
}}

* {{
    box-sizing: border-box;
    font-family: 'Noto Sans Khmer', 'Noto Color Emoji', sans-serif;
    margin: 0;
    padding: 0;
}}

body {{
    color: #202124;
    font-size: 10pt;
}}

.header {{
    background-color: #4285F4;
    color: white;
    padding: 18px 22px;
    border-radius: 8px 8px 0 0;
    display: flex;
    justify-content: space-between;
    align-items: center;
}}

.header-left h1 {{
    font-size: 22pt;
    font-weight: 700;
    margin-bottom: 5px;
}}

.header-left .subtitle {{
    font-size: 12pt;
    font-weight: 400;
    opacity: 0.95;
}}

.header-right {{
    font-size: 11pt;
    text-align: right;
    font-weight: 500;
}}

.grid {{
    display: grid;
    grid-template-columns: repeat(7, 1fr);
    gap: 4px;
    margin-top: 4px;
}}

.day-col {{
    background-color: white;
    border: 1px solid #DADCE0;
    border-radius: 6px;
    min-height: 460px;
    padding: 8px 6px;
}}

.day-col.weekend {{
    background-color: #FFF3E0;
}}

.day-col.today {{
    background-color: #E3F2FD;
}}

.day-header {{
    text-align: center;
    padding-bottom: 8px;
    border-bottom: 1px solid #DADCE0;
    margin-bottom: 8px;
}}

.weekday-en {{
    font-size: 9pt;
    font-weight: 700;
    color: #5F6368;
    letter-spacing: 1px;
    font-family: 'Helvetica', sans-serif;
}}

.weekday-en.weekend-text {{
    color: #EA4335;
}}

.weekday-en.today-text {{
    color: #4285F4;
}}

.day-num {{
    font-size: 20pt;
    font-weight: 700;
    color: #202124;
    margin: 4px 0;
    font-family: 'Helvetica', sans-serif;
}}

.day-num.today-circle {{
    display: inline-block;
    background-color: #4285F4;
    color: white;
    width: 40px;
    height: 40px;
    line-height: 40px;
    border-radius: 50%;
    font-size: 16pt;
}}

.weekday-kh {{
    font-size: 9pt;
    color: #5F6368;
    margin-top: 2px;
}}

.events {{
    display: flex;
    flex-direction: column;
    gap: 5px;
}}

.event {{
    color: white;
    border-radius: 4px;
    padding: 5px 7px;
    font-size: 8pt;
    line-height: 1.4;
    page-break-inside: avoid;
    word-wrap: break-word;
}}

.event-header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 3px;
    font-size: 7pt;
    font-weight: 700;
    opacity: 0.95;
    font-family: 'Helvetica', sans-serif;
}}

.event-text {{
    font-size: 8.5pt;
    line-height: 1.35;
    word-wrap: break-word;
    overflow-wrap: break-word;
}}

.footer {{
    margin-top: 10px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-size: 8pt;
    color: #5F6368;
    padding: 0 5px;
}}

.legend {{
    display: flex;
    gap: 14px;
}}

.legend-item {{
    display: flex;
    align-items: center;
    gap: 4px;
}}

.legend-color {{
    width: 10px;
    height: 10px;
    border-radius: 2px;
    display: inline-block;
}}
</style>
</head>
<body>
    <div class="header">
        <div class="header-left">
            <h1>📅 កាលវិភាគសប្តាហ៍</h1>
            <div class="subtitle">{subtitle}</div>
        </div>
        <div class="header-right">
            សរុប: {total} ព្រឹត្តិការណ៍
        </div>
    </div>

    <div class="grid">
        {columns_html}
    </div>

    <div class="footer">
        <div class="legend">
            <div class="legend-item"><span class="legend-color" style="background:#4285F4"></span>ការងារ</div>
            <div class="legend-item"><span class="legend-color" style="background:#EA4335"></span>គ្រួសារ</div>
            <div class="legend-item"><span class="legend-color" style="background:#34A853"></span>សុខភាព</div>
            <div class="legend-item"><span class="legend-color" style="background:#FBBC04"></span>ព្រឹត្តិការណ៍</div>
            <div class="legend-item"><span class="legend-color" style="background:#9C27B0"></span>សិក្សា</div>
        </div>
        <div>Voice Tracker Bot • {footer_time}</div>
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
        weekdays_html += (f'<div class="wd-header">'
                          f'<div class="wd-en">{en}</div>'
                          f'<div class="wd-kh">{kh}</div>'
                          f'</div>')

    first_weekday = first_day.weekday()
    total_days = (last_day - first_day).days + 1
    total_cells = first_weekday + total_days
    rows = (total_cells + 6) // 7

    cells_html = ""
    for cell_idx in range(rows * 7):
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
                event_text = e['event'][:25] + ("…" if len(e['event']) > 25 else "")
                events_html += (f'<div class="mini-event" style="background:{color}">'
                                f'{html_escape(time_prefix + event_text)}</div>')
            if len(events) > 4:
                events_html += f'<div class="more">+ {len(events)-4} ទៀត</div>'

        cells_html += f"""
        <div class="{' '.join(cell_classes)}">
            <div class="{' '.join(num_classes)}">{day_num}</div>
            <div class="cell-events">{events_html}</div>
        </div>
        """

    footer_time = datetime.now(TZ).strftime('%Y-%m-%d %H:%M')

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
{FONT_CSS}

@page {{
    size: A4 landscape;
    margin: 8mm;
}}

* {{
    box-sizing: border-box;
    font-family: 'Noto Sans Khmer', 'Noto Color Emoji', sans-serif;
    margin: 0;
    padding: 0;
}}

body {{
    color: #202124;
}}

.header {{
    background-color: #4285F4;
    color: white;
    padding: 18px;
    text-align: center;
    border-radius: 8px 8px 0 0;
}}

.header h1 {{
    font-size: 24pt;
    font-weight: 700;
}}

.header .subtitle {{
    font-size: 11pt;
    margin-top: 5px;
}}

.weekdays {{
    display: grid;
    grid-template-columns: repeat(7, 1fr);
    gap: 2px;
    margin-top: 4px;
}}

.wd-header {{
    background-color: #4285F4;
    color: white;
    padding: 8px;
    text-align: center;
}}

.wd-en {{
    font-size: 10pt;
    font-weight: 700;
    font-family: 'Helvetica', sans-serif;
}}

.wd-kh {{
    font-size: 8pt;
    margin-top: 2px;
}}

.grid {{
    display: grid;
    grid-template-columns: repeat(7, 1fr);
    gap: 2px;
    margin-top: 2px;
}}

.cell {{
    background-color: white;
    border: 1px solid #DADCE0;
    min-height: 100px;
    padding: 6px;
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
    font-size: 12pt;
    font-weight: 700;
    color: #202124;
    margin-bottom: 4px;
    font-family: 'Helvetica', sans-serif;
}}

.cell-num.weekend-num {{
    color: #EA4335;
}}

.cell-num.today-num {{
    display: inline-block;
    background-color: #4285F4;
    color: white;
    width: 24px;
    height: 24px;
    line-height: 24px;
    text-align: center;
    border-radius: 50%;
    font-size: 10pt;
}}

.other-month .cell-num {{
    color: #BDBDBD;
}}

.cell-events {{
    display: flex;
    flex-direction: column;
    gap: 2px;
}}

.mini-event {{
    color: white;
    font-size: 7pt;
    padding: 2px 4px;
    border-radius: 2px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}}

.more {{
    font-size: 7pt;
    color: #5F6368;
    font-style: italic;
}}

.footer {{
    margin-top: 8px;
    text-align: center;
    font-size: 8pt;
    color: #5F6368;
}}
</style>
</head>
<body>
    <div class="header">
        <h1>📅 {KHMER_MONTHS_NAMES[month]} {year}</h1>
        <div class="subtitle">សរុប: {total} ព្រឹត្តិការណ៍</div>
    </div>

    <div class="weekdays">
        {weekdays_html}
    </div>

    <div class="grid">
        {cells_html}
    </div>

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
            text += f"   `#{d['id']}` {d['event'][:40]}\n"

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
        "/export - 📥 Export .ics\n"
        "/help - 📖 ជំនួយ\n\n"
        "🔄 *Auto Sync:*\n"
        "_Google Calendar ⇄ Bot រៀងរាល់ ១៥ នាទី_\n\n"
        "🔔 *Reminders:*\n"
        "_• រៀងរាល់ថ្ងៃសុក្រ ៨:០០ ព្រឹក_\n"
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
            msg += f"`#{e['id']}` {time_str}{e['event']}\n   {e['category']} • {e['status']}\n\n"
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
            msg += f"   📝 {e['event']}\n"
            msg += f"   {e['category']} • {e['status']}\n\n"
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
                msg += f"   {s}: *{c}*\n"
            msg += "\n"

        if cat_counter:
            msg += "*🏷 ប្រភេទ:*\n"
            for c, cnt in cat_counter.most_common():
                msg += f"   {c}: *{cnt}*\n"
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
                msg += f"   `#{e['id']}` {time_str}{e['event']}\n"
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
            msg += f"`#{e['id']}` 📅 {e['date']} {time_str}\n   📝 {e['event']}\n\n"
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

    logger.info("✅ Bot v3.2 is running!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info(f"🌐 Flask started on port {PORT}")
    run_bot()
