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
from PIL import Image, ImageDraw, ImageFont
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

# PDF
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.colors import HexColor, white, black
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.units import mm, cm

from khmer_font import get_khmer_font

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
        logger.info("✅ Headers updated to v3.1")
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

# Pending confirmations
pending_events = {}


# ══════════════════════════════════════
# Utilities
# ══════════════════════════════════════

def similarity(a, b):
    return SequenceMatcher(None, a, b).ratio()


def detect_category(text):
    """Auto-detect category from text"""
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


# ══════════════════════════════════════
# AI Parser
# ══════════════════════════════════════

def parse_with_ai(text):
    """Parse Khmer text ជាមួយ Gemini"""
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
    """Save event ចូល Sheet"""
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
    """លុប row + លុប Google Calendar event"""
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
    """Edit field"""
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
    """Update Column H (GCal_ID)"""
    all_values = worksheet.get_all_values()
    for idx, row in enumerate(all_values[1:], start=2):
        if row and str(row[0]) == str(row_num):
            worksheet.update_cell(idx, 8, gcal_id)
            return True
    return False


def get_all_events():
    """ទាញ events ទាំងអស់"""
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
    """Sort sheet by date ascending"""
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
    """Detect similar events same date"""
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
    """ទាញ events ពី Google Calendar"""
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
    """Parse Google Calendar event → dict"""
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
    """List of synced GCal IDs from Sheet"""
    try:
        all_values = worksheet.get_all_values()
        return {row[7] for row in all_values[1:] if len(row) > 7 and row[7]}
    except Exception:
        return set()


def save_gcal_to_sheet(gcal_data):
    """Save gcal event ចូល Sheet"""
    return save_to_sheet(
        gcal_data['date'],
        gcal_data['time'],
        gcal_data['event'],
        gcal_data['category'],
        gcal_id=gcal_data['gcal_id']
    )


def update_sheet_from_gcal(gcal_id, gcal_data):
    """Update row from gcal changes"""
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
    """Pull events ពី Google Calendar → Sheet"""
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
    """Push event ទៅ Google Calendar"""
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
    """Generate .ics calendar file"""
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
# 📄 PDF Calendar Generator
# ══════════════════════════════════════

KHMER_FONT_REGISTERED = False


def register_khmer_font_pdf():
    """Register Khmer font for ReportLab"""
    global KHMER_FONT_REGISTERED
    if KHMER_FONT_REGISTERED:
        return True
    try:
        font_path = get_khmer_font()
        if font_path and os.path.exists(font_path):
            pdfmetrics.registerFont(TTFont('Khmer', font_path))
            KHMER_FONT_REGISTERED = True
            logger.info("✅ Khmer font registered for PDF")
            return True
    except Exception as e:
        logger.error(f"Font register error: {e}")
    return False


def wrap_pdf_text(text, max_chars_per_line=25):
    """Wrap text ជាបន្ទាត់ៗ"""
    if not text:
        return [""]
    lines = []
    current = ""
    for char in text:
        if len(current) >= max_chars_per_line:
            lines.append(current)
            current = char
        else:
            current += char
    if current:
        lines.append(current)
    return lines


def generate_week_calendar_pdf(start_date=None):
    """PDF Calendar សប្តាហ៍មួយ (A4 Landscape)"""
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

    has_khmer = register_khmer_font_pdf()
    KHMER = 'Khmer' if has_khmer else 'Helvetica'

    output = BytesIO()
    PAGE = landscape(A4)
    PAGE_W, PAGE_H = PAGE
    c = canvas.Canvas(output, pagesize=PAGE)

    COLOR_HEADER = HexColor("#4285F4")
    COLOR_HEADER_TEXT = HexColor("#FFFFFF")
    COLOR_WEEKEND = HexColor("#FFF3E0")
    COLOR_TODAY = HexColor("#E3F2FD")
    COLOR_BORDER = HexColor("#DADCE0")
    COLOR_TEXT_DARK = HexColor("#202124")
    COLOR_TEXT_GRAY = HexColor("#5F6368")
    COLOR_TEXT_RED = HexColor("#EA4335")

    CATEGORY_COLORS = {
        "🏢 ការងារ": HexColor("#4285F4"),
        "👨‍👩‍👧 គ្រួសារ": HexColor("#EA4335"),
        "💊 សុខភាព": HexColor("#34A853"),
        "🎉 ព្រឹត្តិការណ៍": HexColor("#FBBC04"),
        "📚 សិក្សា": HexColor("#9C27B0"),
        "📌 ផ្សេងៗ": HexColor("#00ACC1"),
    }

    STATUS_COLORS_PDF = {
        STATUS_DONE: HexColor("#34A853"),
        STATUS_CANCEL: HexColor("#9E9E9E"),
    }

    MARGIN = 15 * mm
    HEADER_H = 40 * mm
    DAY_HEADER_H = 22 * mm
    COL_GAP = 2 * mm

    content_x = MARGIN
    content_w = PAGE_W - (2 * MARGIN)
    col_w = (content_w - (6 * COL_GAP)) / 7

    # Header
    c.setFillColor(COLOR_HEADER)
    c.rect(0, PAGE_H - HEADER_H, PAGE_W, HEADER_H, fill=1, stroke=0)
    c.setFillColor(COLOR_HEADER_TEXT)
    c.setFont(KHMER, 22)
    c.drawString(MARGIN, PAGE_H - 18 * mm, "📅 កាលវិភាគសប្តាហ៍")

    end_date = start_date + timedelta(days=6)
    subtitle = (f"{start_date.day} {KHMER_MONTHS_NAMES[start_date.month]} - "
                f"{end_date.day} {KHMER_MONTHS_NAMES[end_date.month]} {end_date.year}")
    c.setFont(KHMER, 14)
    c.drawString(MARGIN, PAGE_H - 30 * mm, subtitle)

    total = sum(len(events_by_date[d]) for d in week_dates)
    c.setFont(KHMER, 12)
    c.drawRightString(PAGE_W - MARGIN, PAGE_H - 25 * mm,
                      f"សរុប: {total} ព្រឹត្តិការណ៍")

    today = datetime.now(TZ).date()
    columns_y_top = PAGE_H - HEADER_H - 5 * mm
    columns_bottom = MARGIN + 15 * mm

    for i, d in enumerate(week_dates):
        col_x = content_x + i * (col_w + COL_GAP)
        col_top = columns_y_top

        is_weekend = d.weekday() >= 5
        is_today = d == today

        if is_today:
            c.setFillColor(COLOR_TODAY)
        elif is_weekend:
            c.setFillColor(COLOR_WEEKEND)
        else:
            c.setFillColor(white)
        c.rect(col_x, columns_bottom, col_w, col_top - columns_bottom,
               fill=1, stroke=0)

        c.setStrokeColor(COLOR_BORDER)
        c.setLineWidth(0.5)
        c.rect(col_x, columns_bottom, col_w, col_top - columns_bottom,
               fill=0, stroke=1)

        day_header_y = col_top - DAY_HEADER_H

        c.setFont('Helvetica-Bold', 9)
        if is_weekend:
            c.setFillColor(COLOR_TEXT_RED)
        elif is_today:
            c.setFillColor(COLOR_HEADER)
        else:
            c.setFillColor(COLOR_TEXT_GRAY)
        day_short = WEEKDAY_SHORT[d.weekday()]
        c.drawCentredString(col_x + col_w / 2, col_top - 6 * mm, day_short)

        day_num = str(d.day)
        if is_today:
            c.setFillColor(COLOR_HEADER)
            c.circle(col_x + col_w / 2, col_top - 12 * mm, 5 * mm,
                     fill=1, stroke=0)
            c.setFillColor(white)
            c.setFont('Helvetica-Bold', 14)
            c.drawCentredString(col_x + col_w / 2, col_top - 13.5 * mm, day_num)
        else:
            c.setFillColor(COLOR_TEXT_DARK)
            c.setFont('Helvetica-Bold', 14)
            c.drawCentredString(col_x + col_w / 2, col_top - 13.5 * mm, day_num)

        c.setFillColor(COLOR_TEXT_GRAY)
        c.setFont(KHMER, 8)
        day_khmer = WEEKDAY_NAMES[d.weekday()]
        c.drawCentredString(col_x + col_w / 2, col_top - 19 * mm, day_khmer)

        c.setStrokeColor(COLOR_BORDER)
        c.setLineWidth(0.3)
        c.line(col_x + 2, day_header_y, col_x + col_w - 2, day_header_y)

        events = events_by_date[d]
        event_y = day_header_y - 4 * mm

        for e in events:
            category = e.get('category', '📌 ផ្សេងៗ')
            base_color = CATEGORY_COLORS.get(category, HexColor("#00ACC1"))

            if STATUS_DONE in e.get('status', ''):
                base_color = STATUS_COLORS_PDF[STATUS_DONE]
            elif STATUS_CANCEL in e.get('status', ''):
                base_color = STATUS_COLORS_PDF[STATUS_CANCEL]

            max_chars = max(int(col_w / (2.5 * mm)), 10)
            event_lines = wrap_pdf_text(e['event'], max_chars)

            line_h = 3.5 * mm
            header_h = 5 * mm
            box_h = header_h + (len(event_lines) * line_h) + 3 * mm
            box_h = max(box_h, 12 * mm)

            box_x = col_x + 2
            box_y = event_y - box_h
            box_w = col_w - 4

            if box_y < columns_bottom + 2:
                c.setFillColor(COLOR_TEXT_GRAY)
                c.setFont(KHMER, 7)
                remaining = len(events) - events.index(e)
                c.drawString(col_x + 3, event_y - 3, f"+ {remaining} ទៀត...")
                break

            c.setFillColor(base_color)
            c.setStrokeColor(base_color)
            c.roundRect(box_x, box_y, box_w, box_h, 2 * mm, fill=1, stroke=1)

            c.setFillColor(white)
            c.setFont('Helvetica-Bold', 6.5)
            c.drawString(box_x + 3 * mm, box_y + box_h - 3.5 * mm, f"#{e['id']}")

            if e['time']:
                c.setFont('Helvetica-Bold', 7)
                c.drawRightString(box_x + box_w - 2 * mm,
                                  box_y + box_h - 3.5 * mm,
                                  f"{e['time']}")

            c.setFont(KHMER, 7.5)
            text_y = box_y + box_h - 7 * mm
            for line in event_lines:
                c.drawString(box_x + 3 * mm, text_y, line)
                text_y -= line_h

            event_y = box_y - 2 * mm

    # Footer
    c.setFillColor(COLOR_TEXT_GRAY)
    c.setFont(KHMER, 8)
    footer = f"Voice Tracker Bot • {datetime.now(TZ).strftime('%Y-%m-%d %H:%M')}"
    c.drawCentredString(PAGE_W / 2, 5 * mm, footer)

    # Legend
    legend_y = 8 * mm
    legend_x = MARGIN
    c.setFont(KHMER, 7)
    legend_items = [
        (HexColor("#4285F4"), "ការងារ"),
        (HexColor("#EA4335"), "គ្រួសារ"),
        (HexColor("#34A853"), "សុខភាព"),
        (HexColor("#FBBC04"), "ព្រឹត្តិការណ៍"),
        (HexColor("#9C27B0"), "សិក្សា"),
    ]
    for color, name in legend_items:
        c.setFillColor(color)
        c.rect(legend_x, legend_y, 3 * mm, 3 * mm, fill=1, stroke=0)
        c.setFillColor(COLOR_TEXT_DARK)
        c.drawString(legend_x + 4 * mm, legend_y + 0.5 * mm, name)
        legend_x += 22 * mm

    c.showPage()
    c.save()
    output.seek(0)
    return output


def generate_month_calendar_pdf(year=None, month=None):
    """PDF Calendar ខែមួយ (Grid view)"""
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

    has_khmer = register_khmer_font_pdf()
    KHMER = 'Khmer' if has_khmer else 'Helvetica'

    output = BytesIO()
    PAGE = landscape(A4)
    PAGE_W, PAGE_H = PAGE
    c = canvas.Canvas(output, pagesize=PAGE)

    COLOR_HEADER = HexColor("#4285F4")
    COLOR_WEEKEND = HexColor("#FFF3E0")
    COLOR_TODAY = HexColor("#E3F2FD")
    COLOR_OTHER_MONTH = HexColor("#F5F5F5")
    COLOR_BORDER = HexColor("#DADCE0")
    COLOR_TEXT_DARK = HexColor("#202124")
    COLOR_TEXT_GRAY = HexColor("#5F6368")
    COLOR_TEXT_RED = HexColor("#EA4335")

    CATEGORY_COLORS = {
        "🏢 ការងារ": HexColor("#4285F4"),
        "👨‍👩‍👧 គ្រួសារ": HexColor("#EA4335"),
        "💊 សុខភាព": HexColor("#34A853"),
        "🎉 ព្រឹត្តិការណ៍": HexColor("#FBBC04"),
        "📚 សិក្សា": HexColor("#9C27B0"),
        "📌 ផ្សេងៗ": HexColor("#00ACC1"),
    }

    MARGIN = 15 * mm
    HEADER_H = 30 * mm

    c.setFillColor(COLOR_HEADER)
    c.rect(0, PAGE_H - HEADER_H, PAGE_W, HEADER_H, fill=1, stroke=0)
    c.setFillColor(white)
    c.setFont(KHMER, 22)
    title = f"📅 {KHMER_MONTHS_NAMES[month]} {year}"
    c.drawCentredString(PAGE_W / 2, PAGE_H - 18 * mm, title)

    total = sum(len(events_by_date[d]) for d in events_by_date)
    c.setFont(KHMER, 11)
    c.drawCentredString(PAGE_W / 2, PAGE_H - 25 * mm,
                        f"សរុប: {total} ព្រឹត្តិការណ៍")

    grid_top = PAGE_H - HEADER_H - 5 * mm
    grid_bottom = MARGIN
    grid_left = MARGIN
    grid_right = PAGE_W - MARGIN
    grid_w = grid_right - grid_left

    day_header_h = 8 * mm
    grid_content_top = grid_top - day_header_h

    weekdays_en = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
    col_w = grid_w / 7
    for i, wd in enumerate(weekdays_en):
        x = grid_left + i * col_w
        c.setFillColor(COLOR_HEADER)
        c.setFont('Helvetica-Bold', 10)
        c.drawCentredString(x + col_w / 2, grid_top - 5 * mm, wd)

    first_weekday = first_day.weekday()
    total_days = (last_day - first_day).days + 1
    total_cells = first_weekday + total_days
    rows = (total_cells + 6) // 7

    row_h = (grid_content_top - grid_bottom) / rows
    today = datetime.now(TZ).date()

    for cell_idx in range(rows * 7):
        row = cell_idx // 7
        col = cell_idx % 7
        cell_x = grid_left + col * col_w
        cell_y = grid_content_top - (row + 1) * row_h

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

        if not is_this_month:
            c.setFillColor(COLOR_OTHER_MONTH)
        elif is_today:
            c.setFillColor(COLOR_TODAY)
        elif is_weekend:
            c.setFillColor(COLOR_WEEKEND)
        else:
            c.setFillColor(white)
        c.rect(cell_x, cell_y, col_w, row_h, fill=1, stroke=0)

        c.setStrokeColor(COLOR_BORDER)
        c.setLineWidth(0.3)
        c.rect(cell_x, cell_y, col_w, row_h, fill=0, stroke=1)

        day_num = str(cell_date.day)
        if is_today:
            c.setFillColor(COLOR_HEADER)
            c.circle(cell_x + 5 * mm, cell_y + row_h - 4 * mm, 3 * mm,
                     fill=1, stroke=0)
            c.setFillColor(white)
            c.setFont('Helvetica-Bold', 9)
            c.drawCentredString(cell_x + 5 * mm, cell_y + row_h - 5 * mm, day_num)
        elif is_this_month:
            c.setFillColor(COLOR_TEXT_RED if is_weekend else COLOR_TEXT_DARK)
            c.setFont('Helvetica-Bold', 10)
            c.drawString(cell_x + 2 * mm, cell_y + row_h - 5 * mm, day_num)
        else:
            c.setFillColor(HexColor("#BDBDBD"))
            c.setFont('Helvetica', 9)
            c.drawString(cell_x + 2 * mm, cell_y + row_h - 5 * mm, day_num)

        if is_this_month and cell_date in events_by_date:
            events = events_by_date[cell_date]
            event_y = cell_y + row_h - 8 * mm
            max_events = max(int((row_h - 10 * mm) / (3.5 * mm)), 1)

            for i, e in enumerate(events[:max_events]):
                category = e.get('category', '📌 ផ្សេងៗ')
                color = CATEGORY_COLORS.get(category, HexColor("#00ACC1"))

                pill_h = 3 * mm
                c.setFillColor(color)
                c.roundRect(cell_x + 1.5 * mm, event_y - pill_h,
                            col_w - 3 * mm, pill_h, 0.5 * mm,
                            fill=1, stroke=0)

                c.setFillColor(white)
                c.setFont(KHMER, 6)
                display_text = e['event'][:15]
                if e['time']:
                    display_text = f"{e['time']} {display_text}"
                c.drawString(cell_x + 2 * mm, event_y - pill_h + 0.8 * mm,
                             display_text)

                event_y -= (pill_h + 0.5 * mm)

            if len(events) > max_events:
                c.setFillColor(COLOR_TEXT_GRAY)
                c.setFont(KHMER, 6)
                c.drawString(cell_x + 2 * mm, event_y - 2 * mm,
                             f"+ {len(events) - max_events} ទៀត")

    c.setFillColor(COLOR_TEXT_GRAY)
    c.setFont(KHMER, 8)
    footer = f"Voice Tracker Bot • {datetime.now(TZ).strftime('%Y-%m-%d %H:%M')}"
    c.drawCentredString(PAGE_W / 2, 5 * mm, footer)

    c.showPage()
    c.save()
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

    # Status change
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

        # Push to Google Calendar
        try:
            category_name = CATEGORIES.get(data['category'], CATEGORIES["other"])
            gcal_id = push_to_google_calendar(
                data['date'], data['time'], data['event'], category_name
            )
            if gcal_id:
                update_gcal_id(row_num, gcal_id)
        except Exception as e:
            logger.warning(f"Auto-push failed: {e}")

        # Recurring events
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
        "🎙️ *Voice Tracker Bot v3.1*\n\n"
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
    """សប្តាហ៍នេះ - PDF"""
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
    """សប្តាហ៍ក្រោយ - PDF"""
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
    """PDF Calendar ខែនេះ"""
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
    """Manual sync ពី Google Calendar"""
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

    # Editing mode
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
    """Friday 8AM - ផ្ញើ PDF Calendar"""
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
    """Check events - remind 24h and 1h before"""
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
    """Auto pull ពី Google Calendar (រៀងរាល់ ១៥ នាទី)"""
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
    return "🤖 Voice Tracker Bot v3.1 is running!"


@flask_app.route("/health")
def health():
    return {"status": "ok", "version": "3.1"}


@flask_app.route("/calendar/<secret>")
def calendar_feed(secret):
    """Public .ics feed"""
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
    logger.info("🚀 Starting Bot v3.1...")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Commands
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

    # Callbacks
    app.add_handler(CallbackQueryHandler(button_handler))

    # Messages
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Jobs
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

    logger.info("✅ Bot v3.1 is running!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info(f"🌐 Flask started on port {PORT}")
    run_bot()
