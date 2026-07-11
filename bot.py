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
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InputFile
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes,
)
from flask import Flask

from khmer_font import get_khmer_font

# ─── Environment ───
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS")
CHAT_ID = os.environ.get("CHAT_ID")
PORT = int(os.environ.get("PORT", 10000))

TZ = pytz.timezone("Asia/Phnom_Penh")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Gemini ───
gemini_client = genai.Client(api_key=GEMINI_API_KEY)
logger.info("Gemini configured!")

# ─── Google Sheets ───
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
credentials_info = json.loads(GOOGLE_CREDENTIALS)
creds = Credentials.from_service_account_info(credentials_info, scopes=SCOPES)
gc = gspread.authorize(creds)
spreadsheet = gc.open_by_key(GOOGLE_SHEET_ID)
worksheet = spreadsheet.sheet1

# ✅ 7 Columns
HEADERS = [
    "#", "កាលបរិច្ឆេទ", "ម៉ោងព្រឹត្តិការណ៍",
    "ព្រឹត្តិការណ៍", "ប្រភេទ", "ស្ថានភាព", "ម៉ោងបញ្ចូល"
]

try:
    header = worksheet.row_values(1)
    if not header or header != HEADERS:
        worksheet.update("A1:G1", [HEADERS])
        logger.info("Headers updated to v2.0")
except Exception as e:
    logger.error(f"Header error: {e}")

logger.info("Sheet connected!")


# ══════════════════════════════════════
# Constants
# ══════════════════════════════════════

KHMER_MONTHS = {
    "មករា": 1, "កុម្ភៈ": 2, "មីនា": 3, "មេសា": 4,
    "ឧសភា": 5, "មិថុនា": 6, "កក្កដា": 7, "សីហា": 8,
    "កញ្ញា": 9, "តុលា": 10, "វិច្ឆិកា": 11, "ធ្នូ": 12,
}
KHMER_MONTHS_NAMES = {v: k for k, v in KHMER_MONTHS.items()}

KHMER_RELATIVE_DAYS = {
    "ថ្ងៃនេះ": 0, "ថ្ងៃស្អែក": 1, "ខានស្អែក": 2,
    "ម្សិលមិញ": -1, "ម្សិលម៉្ង": -2,
}
KHMER_WEEKDAYS = {
    "ថ្ងៃច័ន្ទ": 0, "ថ្ងៃអង្គារ": 1, "ថ្ងៃពុធ": 2,
    "ថ្ងៃព្រហស្បតិ៍": 3, "ថ្ងៃសុក្រ": 4,
    "ថ្ងៃសៅរ៍": 5, "ថ្ងៃអាទិត្យ": 6,
}
WEEKDAY_NAMES = {
    0: "ច័ន្ទ", 1: "អង្គារ", 2: "ពុធ",
    3: "ព្រហស្បតិ៍", 4: "សុក្រ", 5: "សៅរ៍", 6: "អាទិត្យ",
}
WEEKDAY_SHORT = {
    0: "MON", 1: "TUE", 2: "WED",
    3: "THU", 4: "FRI", 5: "SAT", 6: "SUN",
}
KHMER_DIGITS = str.maketrans("០១២៣៤៥៦៧៨៩", "0123456789")

# ✅ Categories
CATEGORIES = {
    "work": "🏢 ការងារ",
    "family": "👨‍👩‍👧 គ្រួសារ",
    "health": "💊 សុខភាព",
    "event": "🎉 ព្រឹត្តិការណ៍",
    "study": "📚 សិក្សា",
    "other": "📌 ផ្សេងៗ",
}

# ✅ Status
STATUS_PENDING = "⏳ រង់ចាំ"
STATUS_DONE = "✅ រួចរាល់"
STATUS_CANCEL = "❌ បោះបង់"

# ✅ Recurring keywords
RECURRING_KEYWORDS = {
    "រៀងរាល់ថ្ងៃច័ន្ទ": 0, "រាល់ថ្ងៃច័ន្ទ": 0,
    "រៀងរាល់ថ្ងៃអង្គារ": 1, "រាល់ថ្ងៃអង្គារ": 1,
    "រៀងរាល់ថ្ងៃពុធ": 2, "រាល់ថ្ងៃពុធ": 2,
    "រៀងរាល់ថ្ងៃព្រហស្បតិ៍": 3, "រាល់ថ្ងៃព្រហស្បតិ៍": 3,
    "រៀងរាល់ថ្ងៃសុក្រ": 4, "រាល់ថ្ងៃសុក្រ": 4,
    "រៀងរាល់ថ្ងៃសៅរ៍": 5, "រាល់ថ្ងៃសៅរ៍": 5,
    "រៀងរាល់ថ្ងៃអាទិត្យ": 6, "រាល់ថ្ងៃអាទិត្យ": 6,
}

# Temporary storage for pending confirmations
pending_events = {}  # {chat_id: {date, time, event, category, ...}}


# ══════════════════════════════════════
# Helpers
# ══════════════════════════════════════

def khmer_to_arabic(text):
    return text.translate(KHMER_DIGITS)


def similarity(a, b):
    return SequenceMatcher(None, a, b).ratio()


# ══════════════════════════════════════
# AI Parser (Enhanced with time, category, recurring)
# ══════════════════════════════════════

def parse_with_ai(text):
    """
    Parse text ជាមួយ Gemini
    Returns dict: {date, time, event, category, is_recurring, recurring_day}
    """
    try:
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        weekday_today = WEEKDAY_NAMES[datetime.now(TZ).weekday()]

        prompt = f"""អ្នកគឺជាកម្មវិធីវិភាគអត្ថបទខ្មែរដ៏ឆ្លាតវៃ។

ថ្ងៃនេះ: {today} (ថ្ងៃ{weekday_today})

សូមបំបែកអត្ថបទជា JSON:
{{
  "date": "YYYY-MM-DD",
  "time": "HH:MM" ឬ "" (បើគ្មាន),
  "event": "ព្រឹត្តិការណ៍តែប៉ុណ្ណោះ (កាត់ចេញ date/time)",
  "category": "work|family|health|event|study|other",
  "is_recurring": true/false,
  "recurring_day": 0-6 (Monday=0) ឬ null
}}

ច្បាប់ការវិភាគ:
1. Date: បើគ្មាន → ប្រើ {today}
2. Time: "៩ ព្រឹក"→"09:00", "២ រសៀល"→"14:00", "៦ ល្ងាច"→"18:00", "៨ យប់"→"20:00"
3. Category:
   - work: ប្រជុំ, ការងារ, office, meeting, deadline
   - family: គ្រួសារ, កូន, ម្តាយ, ឪពុក, បងប្អូន
   - health: មន្ទីរពេទ្យ, គ្រូពេទ្យ, ថ្នាំ, ពិនិត្យសុខភាព
   - event: ពិធី, ខួប, រៀបការ, បុណ្យ, party
   - study: រៀន, សិក្សា, ថ្នាក់, ប្រឡង, class
   - other: ផ្សេងទៀត
4. Recurring: បើមាន "រៀងរាល់", "រាល់ថ្ងៃ..." → is_recurring=true, recurring_day=លេខថ្ងៃ
5. Event ត្រូវរក្សាអត្ថន័យ តែកាត់ date/time words ចេញ

ឆ្លើយតែ JSON, គ្មានពាក្យបន្ថែម។

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

        # Validate & set defaults
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
            "category": "other",
            "is_recurring": False,
            "recurring_day": None,
        }


# ══════════════════════════════════════
# Sheet Operations (v2.0 - 7 columns)
# ══════════════════════════════════════

def save_to_sheet(date_str, time_str, event, category_key, status=None):
    """✅ v2.0: 7 columns"""
    now = datetime.now(TZ)
    created = now.strftime("%Y-%m-%d %H:%M:%S")
    all_values = worksheet.get_all_values()
    row_num = len(all_values)  # ID
    category = CATEGORIES.get(category_key, CATEGORIES["other"])
    status = status or STATUS_PENDING
    new_row = [row_num, date_str, time_str, event, category, status, created]
    worksheet.append_row(new_row, value_input_option="USER_ENTERED")
    return row_num


def delete_row(row_num):
    all_values = worksheet.get_all_values()
    for idx, row in enumerate(all_values[1:], start=2):
        if row and str(row[0]) == str(row_num):
            worksheet.delete_rows(idx)
            renumber_rows()
            return True
    return False


def edit_row(row_num, field, new_value):
    """
    field: 'date'(2), 'time'(3), 'event'(4), 'category'(5), 'status'(6)
    """
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


def get_all_events():
    """✅ v2.0: 7 fields"""
    all_values = worksheet.get_all_values()
    events = []
    for row in all_values[1:]:
        if len(row) >= 4 and row[0]:
            events.append({
                "id": row[0],
                "date": row[1],
                "time": row[2] if len(row) > 2 else "",
                "event": row[3] if len(row) > 3 else "",
                "category": row[4] if len(row) > 4 else CATEGORIES["other"],
                "status": row[5] if len(row) > 5 else STATUS_PENDING,
                "created": row[6] if len(row) > 6 else "",
            })
    return events


def sort_sheet_by_date():
    """✅ Sort sheet by date ascending"""
    try:
        all_values = worksheet.get_all_values()
        if len(all_values) <= 1:
            return
        header = all_values[0]
        data = all_values[1:]
        # Sort by date (Column B, index 1)
        data.sort(key=lambda x: (x[1] if len(x) > 1 else "", x[2] if len(x) > 2 else ""))
        # Renumber
        for idx, row in enumerate(data, start=1):
            row[0] = str(idx)
        # Write back
        worksheet.clear()
        worksheet.update("A1", [header] + data)
        logger.info("Sheet sorted by date")
        return True
    except Exception as e:
        logger.error(f"Sort error: {e}")
        return False


def find_duplicates(date_str, event, threshold=0.75):
    """✅ Detect similar events on same date"""
    events = get_all_events()
    duplicates = []
    for e in events:
        if e['date'] == date_str:
            if similarity(e['event'].lower(), event.lower()) >= threshold:
                duplicates.append(e)
    return duplicates


def search_events(keyword):
    """✅ Search events by keyword"""
    events = get_all_events()
    keyword_lower = keyword.lower()
    return [e for e in events if keyword_lower in e['event'].lower()]


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
        '{"date": "YYYY-MM-DD", "time": "HH:MM" ឬ "", "event": "ការពិពណ៌នាជាភាសាខ្មែរ"}\n'
        "បើគ្មានកាលបរិច្ឆេទ, ប្រើថ្ងៃនេះ។ Return ONLY JSON."
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
# 🎨 Calendar Image (unchanged - works with new schema)
# ══════════════════════════════════════

def wrap_text(text, font, max_width, draw):
    lines = []
    words = list(text)
    current_line = ""
    for char in words:
        test_line = current_line + char
        bbox = draw.textbbox((0, 0), test_line, font=font)
        w = bbox[2] - bbox[0]
        if w <= max_width:
            current_line = test_line
        else:
            if current_line:
                lines.append(current_line)
            current_line = char
    if current_line:
        lines.append(current_line)
    return lines


def generate_week_calendar(start_date=None):
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

    # Sort by time then id
    for d in events_by_date:
        events_by_date[d].sort(
            key=lambda x: (x['time'] or "99:99",
                           int(x['id']) if str(x['id']).isdigit() else 0)
        )

    font_path = get_khmer_font()
    try:
        font_title = ImageFont.truetype(font_path, 38)
        font_subtitle = ImageFont.truetype(font_path, 24)
        font_date = ImageFont.truetype(font_path, 32)
        font_day = ImageFont.truetype(font_path, 20)
        font_event = ImageFont.truetype(font_path, 16)
        font_time = ImageFont.truetype(font_path, 14)
        font_id = ImageFont.truetype(font_path, 12)
        font_small = ImageFont.truetype(font_path, 14)
    except Exception:
        font_title = font_subtitle = font_date = font_day = font_event = font_time = font_id = font_small = ImageFont.load_default()

    WIDTH = 1800
    HEADER_H = 130
    DAY_HEADER_H = 110
    COL_W = WIDTH // 7
    EVENT_PADDING = 10
    EVENT_LINE_HEIGHT = 21
    EVENT_MIN_HEIGHT = 60
    EVENT_GAP = 8

    max_content_h = 0
    temp_img = Image.new("RGB", (100, 100))
    temp_draw = ImageDraw.Draw(temp_img)

    for d in week_dates:
        events = events_by_date[d]
        total_h = 0
        for e in events:
            text_max_w = COL_W - (EVENT_PADDING * 2) - 20
            lines = wrap_text(e['event'], font_event, text_max_w, temp_draw)
            box_h = 40 + (len(lines) * EVENT_LINE_HEIGHT) + 10
            total_h += max(box_h, EVENT_MIN_HEIGHT) + EVENT_GAP
        if total_h > max_content_h:
            max_content_h = total_h

    CONTENT_H = max(max_content_h + 30, 500)
    FOOTER_H = 40
    HEIGHT = HEADER_H + DAY_HEADER_H + CONTENT_H + FOOTER_H

    BG = (245, 247, 250)
    HEADER_BG = (66, 133, 244)
    WEEKEND_BG = (255, 243, 224)
    TODAY_BG = (232, 240, 254)
    BORDER = (218, 220, 224)
    TEXT_DARK = (32, 33, 36)
    TEXT_GRAY = (95, 99, 104)
    TEXT_WHITE = (255, 255, 255)
    STATUS_COLORS = {
        STATUS_PENDING: (255, 152, 0),
        STATUS_DONE: (76, 175, 80),
        STATUS_CANCEL: (158, 158, 158),
    }
    EVENT_COLORS = [
        (52, 168, 83), (251, 188, 5), (234, 67, 53),
        (156, 39, 176), (0, 172, 193), (255, 112, 67),
    ]

    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(img)

    draw.rectangle([0, 0, WIDTH, HEADER_H], fill=HEADER_BG)
    end_date = start_date + timedelta(days=6)
    title = "📅 កាលវិភាគសប្តាហ៍"
    subtitle = (f"{start_date.day} {KHMER_MONTHS_NAMES[start_date.month]} - "
                f"{end_date.day} {KHMER_MONTHS_NAMES[end_date.month]} {end_date.year}")
    draw.text((30, 25), title, font=font_title, fill=TEXT_WHITE)
    draw.text((30, 78), subtitle, font=font_subtitle, fill=TEXT_WHITE)

    total = sum(len(events_by_date[d]) for d in week_dates)
    count_text = f"សរុប: {total} ព្រឹត្តិការណ៍"
    bbox = draw.textbbox((0, 0), count_text, font=font_subtitle)
    draw.text((WIDTH - (bbox[2] - bbox[0]) - 30, 50), count_text,
              font=font_subtitle, fill=TEXT_WHITE)

    today = datetime.now(TZ).date()
    for i, d in enumerate(week_dates):
        x = i * COL_W
        y = HEADER_H

        is_weekend = d.weekday() >= 5
        is_today = d == today

        if is_today:
            bg = TODAY_BG
        elif is_weekend:
            bg = WEEKEND_BG
        else:
            bg = (255, 255, 255)

        draw.rectangle([x, y, x + COL_W, HEIGHT - FOOTER_H], fill=bg)
        draw.line([x, y, x, HEIGHT - FOOTER_H], fill=BORDER, width=1)

        day_short = WEEKDAY_SHORT[d.weekday()]
        day_khmer = WEEKDAY_NAMES[d.weekday()]

        header_color = (234, 67, 53) if is_weekend else TEXT_GRAY
        if is_today:
            header_color = HEADER_BG

        bbox = draw.textbbox((0, 0), day_short, font=font_day)
        w = bbox[2] - bbox[0]
        draw.text((x + (COL_W - w) // 2, y + 12), day_short,
                  font=font_day, fill=header_color)

        day_num = str(d.day)
        bbox = draw.textbbox((0, 0), day_num, font=font_date)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]

        if is_today:
            cx = x + COL_W // 2
            cy = y + 60
            r = 28
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=HEADER_BG)
            draw.text((cx - w // 2, cy - h // 2 - 3), day_num,
                      font=font_date, fill=TEXT_WHITE)
        else:
            draw.text((x + (COL_W - w) // 2, y + 42), day_num,
                      font=font_date, fill=TEXT_DARK)

        bbox = draw.textbbox((0, 0), day_khmer, font=font_small)
        w = bbox[2] - bbox[0]
        draw.text((x + (COL_W - w) // 2, y + 88), day_khmer,
                  font=font_small, fill=TEXT_GRAY)

        events = events_by_date[d]
        event_y = y + DAY_HEADER_H + 10

        for j, e in enumerate(events):
            color = EVENT_COLORS[j % len(EVENT_COLORS)]
            # Color by status
            if STATUS_DONE in e.get('status', ''):
                color = STATUS_COLORS[STATUS_DONE]
            elif STATUS_CANCEL in e.get('status', ''):
                color = STATUS_COLORS[STATUS_CANCEL]

            box_x = x + 8
            box_w = COL_W - 16
            text_max_w = box_w - (EVENT_PADDING * 2)
            lines = wrap_text(e['event'], font_event, text_max_w, draw)

            box_h = 40 + (len(lines) * EVENT_LINE_HEIGHT) + 10
            box_h = max(box_h, EVENT_MIN_HEIGHT)

            draw.rectangle([box_x, event_y, box_x + box_w, event_y + box_h],
                           fill=color)

            # Top row: #ID + time
            id_text = f"#{e['id']}"
            time_text = f"🕐 {e['time']}" if e['time'] else ""
            draw.text((box_x + EVENT_PADDING, event_y + 5), id_text,
                      font=font_id, fill=TEXT_WHITE)
            if time_text:
                bbox = draw.textbbox((0, 0), time_text, font=font_time)
                tw = bbox[2] - bbox[0]
                draw.text((box_x + box_w - tw - EVENT_PADDING, event_y + 4),
                          time_text, font=font_time, fill=TEXT_WHITE)

            # Event lines
            text_y = event_y + 22
            for line in lines:
                draw.text((box_x + EVENT_PADDING, text_y), line,
                          font=font_event, fill=TEXT_WHITE)
                text_y += EVENT_LINE_HEIGHT

            event_y += box_h + EVENT_GAP

    footer_y = HEIGHT - 28
    footer_text = f"Voice Tracker Bot • {datetime.now(TZ).strftime('%Y-%m-%d %H:%M')}"
    draw.text((30, footer_y), footer_text, font=font_small, fill=TEXT_GRAY)

    output = BytesIO()
    img.save(output, format="PNG", quality=95)
    output.seek(0)
    return output


# ══════════════════════════════════════
# 📅 ICS Export
# ══════════════════════════════════════

def generate_ics(events):
    """✅ Generate .ics calendar file"""
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
            logger.warning(f"ICS skip event {e.get('id')}: {ex}")

    lines.append("END:VCALENDAR")
    return "\n".join(lines).encode("utf-8")


# ══════════════════════════════════════
# 🔊 TTS (Voice Reply)
# ══════════════════════════════════════

def generate_tts_khmer(text):
    """
    ព្យាយាមប្រើ Gemini TTS ឬ fallback ទៅ gTTS
    Returns BytesIO ឬ None
    """
    try:
        from gtts import gTTS
        tts = gTTS(text=text, lang="km", slow=False)
        buf = BytesIO()
        tts.write_to_fp(buf)
        buf.seek(0)
        return buf
    except Exception as e:
        logger.warning(f"TTS error: {e}")
        return None


# ══════════════════════════════════════
# 🎯 Confirmation Flow
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
    """Show preview + buttons"""
    pending_events[chat_id] = data
    text = format_preview(data)

    # Duplicate warning
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
    chat_id = int(parts[1])

    if chat_id not in pending_events:
        # Handle status change from history: setstatus:ID:status
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
        await query.edit_message_text("❌ អស់សុពលភាព (បានផុតកំណត់)")
        return

    data = pending_events[chat_id]

    if action == "save":
        row_num = save_to_sheet(
            data['date'], data['time'], data['event'],
            data['category']
        )

        # Recurring: create multiple events
        extra_count = 0
        if data.get('is_recurring') and data.get('recurring_day') is not None:
            base_date = datetime.strptime(data['date'], "%Y-%m-%d").date()
            for w in range(1, 9):  # add 8 weeks
                next_date = base_date + timedelta(weeks=w)
                save_to_sheet(
                    next_date.strftime("%Y-%m-%d"),
                    data['time'], data['event'], data['category']
                )
                extra_count += 1

        pending_events.pop(chat_id, None)
        msg = f"✅ *រក្សាទុក!* `#{row_num}`\n"
        msg += f"📅 {data['date']} {data['time']}\n"
        msg += f"📝 {data['event']}\n"
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
            "edittime": ("time", "ម៉ោង (HH:MM ឬ វាយ 'គ្មាន')"),
            "editevent": ("event", "ព្រឹត្តិការណ៍ថ្មី"),
        }
        field, label = field_map[action]
        context.user_data['editing_field'] = field
        context.user_data['editing_chat'] = chat_id
        await query.edit_message_text(
            f"✏️ សូមវាយ *{label}* ថ្មី:\n(ឬវាយ /cancel ដើម្បីបោះបង់)",
            parse_mode="Markdown"
        )


# ══════════════════════════════════════
# Telegram Command Handlers
# ══════════════════════════════════════

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome = (
        "🎙️ *Voice Tracker Bot v2.0*\n\n"
        "📌 *របៀបប្រើ:*\n"
        "🎤 ផ្ញើសំឡេងខ្មែរ\n"
        "📸 ផ្ញើរូបភាព\n"
        "⌨️ វាយអក្សរផ្ទាល់\n\n"
        "📋 *Commands:*\n"
        "/today - 📌 ព្រឹត្តិការណ៍ថ្ងៃនេះ\n"
        "/week - 🖼 កាលវិភាគសប្តាហ៍\n"
        "/nextweek - 🖼 សប្តាហ៍ក្រោយ\n"
        "/calendar - 📅 ៣០ ថ្ងៃខាងមុខ\n"
        "/stats - 📊 ស្ថិតិខែនេះ\n"
        "/history - 📋 ១០ ចុងក្រោយ\n"
        "/search <ពាក្យ> - 🔍 ស្វែងរក\n"
        "/status <លេខ> - 📊 កែស្ថានភាព\n"
        "/delete <លេខ> - 🗑 លុប\n"
        "/edit <លេខ> <អត្ថបទ> - ✏️ កែ\n"
        "/sort - 📶 រៀបតាមកាលបរិច្ឆេទ\n"
        "/export - 📥 Export .ics\n"
        "/help - 📖 ជំនួយ\n\n"
        "🔔 _រំលឹករៀងរាល់ថ្ងៃសុក្រ ៨:០០ ព្រឹក_\n"
        "🔔 _រំលឹកមុន event ១ ថ្ងៃ + ១ ម៉ោង_"
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
    await update.message.reply_text("🎨 កំពុងបង្កើតរូបភាព...")
    try:
        today = datetime.now(TZ).date()
        monday = today - timedelta(days=today.weekday())
        loop = asyncio.get_event_loop()
        img_bytes = await loop.run_in_executor(None, generate_week_calendar, monday)
        end = monday + timedelta(days=6)
        caption = (f"📅 *កាលវិភាគសប្តាហ៍នេះ*\n"
                   f"📆 {monday.strftime('%Y-%m-%d')} → {end.strftime('%Y-%m-%d')}")
        await update.message.reply_photo(photo=img_bytes, caption=caption, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


async def nextweek_command(update, context):
    await update.message.reply_text("🎨 កំពុងបង្កើតរូបភាព...")
    try:
        today = datetime.now(TZ).date()
        monday = today - timedelta(days=today.weekday()) + timedelta(days=7)
        loop = asyncio.get_event_loop()
        img_bytes = await loop.run_in_executor(None, generate_week_calendar, monday)
        end = monday + timedelta(days=6)
        caption = (f"📅 *កាលវិភាគសប្តាហ៍ក្រោយ*\n"
                   f"📆 {monday.strftime('%Y-%m-%d')} → {end.strftime('%Y-%m-%d')}")
        await update.message.reply_photo(photo=img_bytes, caption=caption, parse_mode="Markdown")
    except Exception as e:
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
            await update.message.reply_text(f"✅ លុប #{row_num}!")
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
            await update.message.reply_text(
                "❌ ប្រើ: /status <លេខ>\nឧ. `/status 5`", parse_mode="Markdown"
            )
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
            f"ស្ថានភាពបច្ចុប្បន្ន: {target['status']}\n\n"
            f"ជ្រើសរើសស្ថានភាពថ្មី:",
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


async def cancel_command(update, context):
    context.user_data.pop('editing_field', None)
    context.user_data.pop('editing_chat', None)
    await update.message.reply_text("❌ បោះបង់ការកែ")


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
            "category": "other",
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

    # Check if user is editing a field
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
# 🔔 Reminders
# ══════════════════════════════════════

async def send_weekly_reminder(context: ContextTypes.DEFAULT_TYPE):
    logger.info("🔔 Weekly reminder...")
    if not CHAT_ID:
        return
    try:
        today = datetime.now(TZ).date()
        next_monday = today + timedelta(days=(7 - today.weekday()))
        loop = asyncio.get_event_loop()
        img_bytes = await loop.run_in_executor(None, generate_week_calendar, next_monday)
        end = next_monday + timedelta(days=6)
        caption = (f"🔔 *រំលឹកកាលវិភាគសប្តាហ៍ក្រោយ*\n\n"
                   f"📅 {next_monday.strftime('%Y-%m-%d')} → {end.strftime('%Y-%m-%d')}")
        await context.bot.send_photo(chat_id=CHAT_ID, photo=img_bytes,
                                     caption=caption, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Weekly reminder error: {e}")


async def send_personal_reminders(context: ContextTypes.DEFAULT_TYPE):
    """
    Check events every 30 min:
    - Event ស្អែក (~24h) → notify
    - Event ១ ម៉ោងទៀត → notify
    """
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

                # 24 hours before (± 30 min window)
                if 1410 <= diff_min <= 1470:
                    msg = (f"🔔 *រំលឹក ១ ថ្ងៃមុន*\n\n"
                           f"📅 {e['date']} {e['time']}\n"
                           f"📝 {e['event']}\n"
                           f"🏷 {e['category']}")
                    await context.bot.send_message(chat_id=CHAT_ID, text=msg,
                                                   parse_mode="Markdown")
                # 1 hour before (± 30 min)
                elif 30 <= diff_min <= 90:
                    msg = (f"⏰ *រំលឹក ១ ម៉ោងមុន*\n\n"
                           f"📅 {e['date']} {e['time']}\n"
                           f"📝 {e['event']}")
                    await context.bot.send_message(chat_id=CHAT_ID, text=msg,
                                                   parse_mode="Markdown")
            except Exception as ex:
                logger.warning(f"Reminder check error for #{e.get('id')}: {ex}")
    except Exception as e:
        logger.error(f"Personal reminder error: {e}")


# ══════════════════════════════════════
# Flask
# ══════════════════════════════════════

flask_app = Flask(__name__)


@flask_app.route("/")
def home():
    return "🤖 Bot v2.0 is running!"


@flask_app.route("/health")
def health():
    return {"status": "ok"}


def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT)


# ══════════════════════════════════════
# Main
# ══════════════════════════════════════

def run_bot():
    logger.info("Starting bot v2.0...")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("today", today_command))
    app.add_handler(CommandHandler("week", week_command))
    app.add_handler(CommandHandler("nextweek", nextweek_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("calendar", calendar_command))
    app.add_handler(CommandHandler("delete", delete_command))
    app.add_handler(CommandHandler("edit", edit_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(CommandHandler("sort", sort_command))
    app.add_handler(CommandHandler("export", export_command))
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
    logger.info("📅 Weekly: Every Friday 8:00 AM")

    # Personal reminders every 30 minutes
    job_queue.run_repeating(
        send_personal_reminders, interval=1800, first=60,
        name="personal_reminders"
    )
    logger.info("⏰ Personal reminders: Every 30 min")

    logger.info("Bot v2.0 is running!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info(f"Flask started on port {PORT}")
    run_bot()
