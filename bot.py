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

import pytz
from PIL import Image, ImageDraw, ImageFont
from google import genai
from google.genai import types
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from flask import Flask

from khmer_font import get_khmer_font

# ─── Load Environment Variables ───
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS")
CHAT_ID = os.environ.get("CHAT_ID")
PORT = int(os.environ.get("PORT", 10000))

# Timezone
TZ = pytz.timezone("Asia/Phnom_Penh")

# ─── Logging ───
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Gemini Setup ───
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

try:
    if not worksheet.row_values(1):
        worksheet.update("A1:E1", [[
            "#", "កាលបរិច្ឆេទ", "ម៉ោង",
            "ព្រឹត្តិការណ៍", "អត្ថបទដើម"
        ]])
except Exception:
    worksheet.update("A1:E1", [[
        "#", "កាលបរិច្ឆេទ", "ម៉ោង",
        "ព្រឹត្តិការណ៍", "អត្ថបទដើម"
    ]])

logger.info("Sheet connected!")


# ══════════════════════════════════════
# Khmer Date Parsing
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


def khmer_to_arabic(text):
    return text.translate(KHMER_DIGITS)


def parse_khmer_date(text):
    today = datetime.now(TZ)
    normalized = khmer_to_arabic(text)

    for keyword, delta in KHMER_RELATIVE_DAYS.items():
        if keyword in text:
            return (today + timedelta(days=delta)).strftime("%Y-%m-%d")

    for keyword, weekday in KHMER_WEEKDAYS.items():
        if keyword in text:
            current = today.weekday()
            diff = weekday - current
            if diff <= 0:
                diff += 7
            return (today + timedelta(days=diff)).strftime("%Y-%m-%d")

    for month_name, month_num in KHMER_MONTHS.items():
        pattern = rf"ថ្ងៃទី\s*(\d{{1,2}})\s*ខែ\s*{month_name}"
        match = re.search(pattern, normalized)
        if match:
            day = int(match.group(1))
            year_match = re.search(r"ឆ្នាំ\s*(\d{4})", normalized)
            year = int(year_match.group(1)) if year_match else today.year
            try:
                return datetime(year, month_num, day).strftime("%Y-%m-%d")
            except ValueError:
                pass

    date_pattern = re.search(r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})", normalized)
    if date_pattern:
        day = int(date_pattern.group(1))
        month = int(date_pattern.group(2))
        year = int(date_pattern.group(3))
        if year < 100:
            year += 2000
        try:
            return datetime(year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            pass

    return today.strftime("%Y-%m-%d")


def extract_event(text):
    event = text
    for keyword in KHMER_RELATIVE_DAYS:
        event = event.replace(keyword, "")
    for keyword in KHMER_WEEKDAYS:
        event = event.replace(keyword, "")
    event = re.sub(r"ថ្ងៃទី\s*\S+\s*ខែ\s*\S+(\s*ឆ្នាំ\s*\S+)?", "", event)
    event = re.sub(r"\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}", "", event)
    event = re.sub(r"\s+", " ", event).strip()
    for prefix in ["មាន", "នឹង", "ត្រូវ", "ទៅ", "គឺ", ",", "."]:
        if event.startswith(prefix):
            event = event[len(prefix):].strip()
    return event if event else text


# ══════════════════════════════════════
# Sheet Operations
# ══════════════════════════════════════

def save_to_sheet(date_str, event, original_text):
    now = datetime.now(TZ)
    time_str = now.strftime("%H:%M:%S")
    all_values = worksheet.get_all_values()
    row_num = len(all_values)
    new_row = [row_num, date_str, time_str, event, original_text]
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


def edit_row(row_num, new_event):
    all_values = worksheet.get_all_values()
    for idx, row in enumerate(all_values[1:], start=2):
        if row and str(row[0]) == str(row_num):
            worksheet.update_cell(idx, 4, new_event)
            return True
    return False


def renumber_rows():
    all_values = worksheet.get_all_values()
    for idx in range(1, len(all_values)):
        worksheet.update_cell(idx + 1, 1, idx)


def get_all_events():
    all_values = worksheet.get_all_values()
    events = []
    for row in all_values[1:]:
        if len(row) >= 4 and row[0]:
            events.append({
                "id": row[0], "date": row[1], "time": row[2],
                "event": row[3],
                "original": row[4] if len(row) > 4 else "",
            })
    return events


# ══════════════════════════════════════
# Speech / Vision (Gemini)
# ══════════════════════════════════════

def transcribe_audio(file_path):
    models = ["gemini-flash-latest", "gemini-2.0-flash",
              "gemini-flash-lite-latest", "gemini-2.5-flash-lite"]
    prompt = (
        "សូមស្តាប់សំឡេងនេះ ហើយបំលែងទៅជាអក្សរខ្មែរ។ "
        "Return ONLY the Khmer text."
    )
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
    models = ["gemini-flash-latest", "gemini-2.0-flash",
              "gemini-flash-lite-latest"]
    prompt = (
        "សូមមើលរូបភាពនេះ។ ស្រង់យក JSON:\n"
        '{"date": "YYYY-MM-DD", "event": "ការពិពណ៌នាជាភាសាខ្មែរ"}\n'
        "បើគ្មានកាលបរិច្ឆេទ, ប្រើថ្ងៃនេះ។ Return ONLY JSON."
    )
    try:
        with open(file_path, "rb") as f:
            image_bytes = f.read()
    except Exception:
        return None, None

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
                data = json.loads(text)
                return (data.get("date", datetime.now(TZ).strftime("%Y-%m-%d")),
                        data.get("event", "រូបភាព"))
            except json.JSONDecodeError:
                return datetime.now(TZ).strftime("%Y-%m-%d"), text[:200]
            except Exception as e:
                if "503" in str(e) or "429" in str(e):
                    time.sleep(2)
                else:
                    break
    return None, None


# ══════════════════════════════════════
# 🎨 GENERATE WEEKLY CALENDAR IMAGE (V2)
# ══════════════════════════════════════

def wrap_text(text, font, max_width, draw):
    """បំបែកអត្ថបទចុះបន្ទាត់ស្វ័យប្រវត្តិ"""
    lines = []
    words = list(text)  # split by character (សម្រាប់ខ្មែរ)

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
    """បង្កើតរូបភាព Calendar សម្រាប់សប្តាហ៍មួយ - V2 (Full Description)"""

    # Setup
    if start_date is None:
        today = datetime.now(TZ).date()
        start_date = today - timedelta(days=today.weekday())

    week_dates = [start_date + timedelta(days=i) for i in range(7)]

    # Get events
    all_events = get_all_events()
    events_by_date = defaultdict(list)
    for e in all_events:
        try:
            d = datetime.strptime(e['date'], "%Y-%m-%d").date()
            if start_date <= d <= start_date + timedelta(days=6):
                events_by_date[d].append(e)
        except Exception:
            pass

    # ═══ Load fonts ═══
    font_path = get_khmer_font()
    try:
        font_title = ImageFont.truetype(font_path, 38) if font_path else ImageFont.load_default()
        font_subtitle = ImageFont.truetype(font_path, 24) if font_path else ImageFont.load_default()
        font_date = ImageFont.truetype(font_path, 32) if font_path else ImageFont.load_default()
        font_day = ImageFont.truetype(font_path, 20) if font_path else ImageFont.load_default()
        font_event = ImageFont.truetype(font_path, 17) if font_path else ImageFont.load_default()
        font_id = ImageFont.truetype(font_path, 13) if font_path else ImageFont.load_default()
        font_small = ImageFont.truetype(font_path, 14) if font_path else ImageFont.load_default()
    except Exception:
        font_title = font_subtitle = font_date = font_day = font_event = font_id = font_small = ImageFont.load_default()

    # ═══ Image dimensions ═══
    WIDTH = 1800  # ធំជាងមុន
    HEADER_H = 130
    DAY_HEADER_H = 110
    COL_W = WIDTH // 7
    EVENT_PADDING = 10
    EVENT_LINE_HEIGHT = 22
    EVENT_MIN_HEIGHT = 55
    EVENT_GAP = 8

    # ═══ Calculate needed height ═══
    # ស្វែងរកកម្ពស់អតិបរមាដែលត្រូវការសម្រាប់ថ្ងៃដែលមាន events ច្រើនបំផុត
    max_content_h = 0

    # Temporary draw for text measurement
    temp_img = Image.new("RGB", (100, 100))
    temp_draw = ImageDraw.Draw(temp_img)

    day_heights = {}
    for d in week_dates:
        events = events_by_date[d]
        total_h = 0
        for e in events:
            event_text = e['event']
            text_max_w = COL_W - (EVENT_PADDING * 2) - 20
            lines = wrap_text(event_text, font_event, text_max_w, temp_draw)
            # ID line + content lines + padding
            box_h = 25 + (len(lines) * EVENT_LINE_HEIGHT) + 15
            total_h += max(box_h, EVENT_MIN_HEIGHT) + EVENT_GAP
        day_heights[d] = total_h
        if total_h > max_content_h:
            max_content_h = total_h

    # ═══ Total image height ═══
    CONTENT_H = max(max_content_h + 30, 500)  # យ៉ាងតិច 500px
    FOOTER_H = 40
    HEIGHT = HEADER_H + DAY_HEADER_H + CONTENT_H + FOOTER_H

    # ═══ Colors ═══
    BG = (245, 247, 250)
    HEADER_BG = (66, 133, 244)
    WEEKEND_BG = (255, 243, 224)
    TODAY_BG = (232, 240, 254)
    BORDER = (218, 220, 224)
    TEXT_DARK = (32, 33, 36)
    TEXT_GRAY = (95, 99, 104)
    TEXT_WHITE = (255, 255, 255)
    EVENT_COLORS = [
        (52, 168, 83),
        (251, 188, 5),
        (234, 67, 53),
        (156, 39, 176),
        (0, 172, 193),
        (255, 112, 67),
    ]

    # ═══ Create image ═══
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(img)

    # ═══ Header ═══
    draw.rectangle([0, 0, WIDTH, HEADER_H], fill=HEADER_BG)
    end_date = start_date + timedelta(days=6)
    title = "📅 កាលវិភាគសប្តាហ៍"
    subtitle = (f"{start_date.day} {KHMER_MONTHS_NAMES[start_date.month]} - "
                f"{end_date.day} {KHMER_MONTHS_NAMES[end_date.month]} "
                f"{end_date.year}")

    draw.text((30, 25), title, font=font_title, fill=TEXT_WHITE)
    draw.text((30, 78), subtitle, font=font_subtitle, fill=TEXT_WHITE)

    # Total count
    total = sum(len(events_by_date[d]) for d in week_dates)
    count_text = f"សរុប: {total} ព្រឹត្តិការណ៍"
    bbox = draw.textbbox((0, 0), count_text, font=font_subtitle)
    draw.text((WIDTH - (bbox[2] - bbox[0]) - 30, 50),
              count_text, font=font_subtitle, fill=TEXT_WHITE)

    # ═══ Day columns ═══
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

        # Day header
        day_short = WEEKDAY_SHORT[d.weekday()]
        day_khmer = WEEKDAY_NAMES[d.weekday()]

        header_color = (234, 67, 53) if is_weekend else TEXT_GRAY
        if is_today:
            header_color = HEADER_BG

        # Weekday name (English)
        bbox = draw.textbbox((0, 0), day_short, font=font_day)
        w = bbox[2] - bbox[0]
        draw.text((x + (COL_W - w) // 2, y + 12),
                  day_short, font=font_day, fill=header_color)

        # Day number
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
            draw.text((x + (COL_W - w) // 2, y + 42),
                      day_num, font=font_date, fill=TEXT_DARK)

        # Khmer weekday name
        bbox = draw.textbbox((0, 0), day_khmer, font=font_small)
        w = bbox[2] - bbox[0]
        draw.text((x + (COL_W - w) // 2, y + 88),
                  day_khmer, font=font_small, fill=TEXT_GRAY)

        # ═══ Events ═══
        events = events_by_date[d]
        event_y = y + DAY_HEADER_H + 10

        for j, e in enumerate(events):
            color = EVENT_COLORS[j % len(EVENT_COLORS)]

            box_x = x + 8
            box_w = COL_W - 16

            # Wrap event text
            event_text = e['event']
            text_max_w = box_w - (EVENT_PADDING * 2)
            lines = wrap_text(event_text, font_event, text_max_w, draw)

            # Calculate box height
            box_h = 25 + (len(lines) * EVENT_LINE_HEIGHT) + 15
            box_h = max(box_h, EVENT_MIN_HEIGHT)

            # Draw event box
            draw.rectangle([box_x, event_y, box_x + box_w, event_y + box_h],
                           fill=color)

            # Event ID
            id_text = f"#{e['id']}"
            draw.text((box_x + EVENT_PADDING, event_y + 6), id_text,
                      font=font_id, fill=TEXT_WHITE)

            # Draw wrapped text lines
            text_y = event_y + 25
            for line in lines:
                draw.text((box_x + EVENT_PADDING, text_y),
                          line, font=font_event, fill=TEXT_WHITE)
                text_y += EVENT_LINE_HEIGHT

            event_y += box_h + EVENT_GAP

    # ═══ Footer ═══
    footer_y = HEIGHT - 28
    footer_text = f"Generated by Voice Tracker Bot • {datetime.now(TZ).strftime('%Y-%m-%d %H:%M')}"
    draw.text((30, footer_y), footer_text, font=font_small, fill=TEXT_GRAY)

    # Save
    output = BytesIO()
    img.save(output, format="PNG", quality=95)
    output.seek(0)
    return output


# ══════════════════════════════════════
# Telegram Handlers
# ══════════════════════════════════════

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome = (
        "🎙️ *Voice Tracker Bot*\n\n"
        "📌 *របៀបប្រើ:*\n"
        "🎤 ផ្ញើសំឡេងខ្មែរ\n"
        "📸 ផ្ញើរូបភាព\n"
        "⌨️ វាយអក្សរផ្ទាល់\n\n"
        "📋 *Commands:*\n"
        "/week - 🖼 កាលវិភាគសប្តាហ៍ (រូបភាព)\n"
        "/nextweek - 🖼 សប្តាហ៍ក្រោយ\n"
        "/stats - 📊 ស្ថិតិខែនេះ\n"
        "/calendar - 📅 ៣០ ថ្ងៃខាងមុខ\n"
        "/history - 📋 ១០ ចុងក្រោយ\n"
        "/delete <លេខ> - 🗑 លុប\n"
        "/edit <លេខ> <អត្ថបទ> - ✏️ កែ\n"
        "/help - 📖 ជំនួយ\n\n"
        "🔔 _រំលឹកកាលវិភាគស្វ័យប្រវត្តិ_\n"
        "_រៀងរាល់ថ្ងៃសុក្រ ម៉ោង ៨:០០ ព្រឹក_"
    )
    await update.message.reply_text(welcome, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_command(update, context)


async def week_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """🖼 បង្ហាញ Calendar សប្តាហ៍នេះ"""
    await update.message.reply_text("🎨 កំពុងបង្កើតរូបភាព...")
    try:
        today = datetime.now(TZ).date()
        # Monday of current week
        monday = today - timedelta(days=today.weekday())

        loop = asyncio.get_event_loop()
        img_bytes = await loop.run_in_executor(
            None, generate_week_calendar, monday
        )

        end = monday + timedelta(days=6)
        caption = (
            f"📅 *កាលវិភាគសប្តាហ៍នេះ*\n"
            f"📆 {monday.strftime('%Y-%m-%d')} → {end.strftime('%Y-%m-%d')}"
        )
        await update.message.reply_photo(
            photo=img_bytes, caption=caption, parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Week error: {e}")
        await update.message.reply_text(f"❌ បញ្ហា: {e}")


async def nextweek_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """🖼 បង្ហាញ Calendar សប្តាហ៍ក្រោយ"""
    await update.message.reply_text("🎨 កំពុងបង្កើតរូបភាព...")
    try:
        today = datetime.now(TZ).date()
        # Monday of next week
        monday = today - timedelta(days=today.weekday()) + timedelta(days=7)

        loop = asyncio.get_event_loop()
        img_bytes = await loop.run_in_executor(
            None, generate_week_calendar, monday
        )

        end = monday + timedelta(days=6)
        caption = (
            f"📅 *កាលវិភាគសប្តាហ៍ក្រោយ*\n"
            f"📆 {monday.strftime('%Y-%m-%d')} → {end.strftime('%Y-%m-%d')}"
        )
        await update.message.reply_photo(
            photo=img_bytes, caption=caption, parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"NextWeek error: {e}")
        await update.message.reply_text(f"❌ បញ្ហា: {e}")


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        events = get_all_events()
        if not events:
            await update.message.reply_text("📭 មិនទាន់មានទេ")
            return
        msg = "📋 *១០ ចុងក្រោយ:*\n\n"
        for e in events[-10:]:
            msg += f"`#{e['id']}` 📅 {e['date']}\n📝 {e['event']}\n\n"
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        events = get_all_events()
        if not events:
            await update.message.reply_text("📭 មិនទាន់មានទេ")
            return
        now = datetime.now(TZ)
        current_month = now.strftime("%Y-%m")
        month_events = [e for e in events if e['date'].startswith(current_month)]

        weekday_counter = Counter()
        for e in month_events:
            try:
                d = datetime.strptime(e['date'], "%Y-%m-%d")
                weekday_counter[WEEKDAY_NAMES[d.weekday()]] += 1
            except Exception:
                pass

        msg = f"📊 *ស្ថិតិខែ {KHMER_MONTHS_NAMES[now.month]} {now.year}*\n\n"
        msg += f"📝 ខែនេះ: *{len(month_events)}*\n"
        msg += f"🗂 សរុប: *{len(events)}*\n\n"

        if weekday_counter:
            msg += "*📅 តាមថ្ងៃ:*\n"
            for day, count in weekday_counter.most_common():
                bar = "▓" * min(count, 10)
                msg += f"`{day:12}` {bar} {count}\n"

        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


async def calendar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            for e in by_date[d]:
                msg += f"   `#{e['id']}` {e['event']}\n"
            msg += "\n"

        if len(msg) > 4000:
            for i in range(0, len(msg), 4000):
                await update.message.reply_text(msg[i:i+4000], parse_mode="Markdown")
        else:
            await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
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


async def edit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if len(context.args) < 2:
            await update.message.reply_text(
                "❌ ប្រើ: /edit <លេខ> <អត្ថបទថ្មី>"
            )
            return
        row_num = context.args[0]
        new_event = " ".join(context.args[1:])
        if edit_row(row_num, new_event):
            await update.message.reply_text(
                f"✅ កែ #{row_num}: {new_event}"
            )
        else:
            await update.message.reply_text(f"❌ រកមិនឃើញ #{row_num}")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


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

        date_str = parse_khmer_date(text)
        event = extract_event(text)
        row_num = save_to_sheet(date_str, event, text)

        reply = (
            f"✅ *កត់ត្រា!*\n\n"
            f"🔢 `#{row_num}`\n"
            f"📅 {date_str}\n"
            f"📝 {event}\n"
            f"💬 _{text}_\n\n"
            f"🗑 `/delete {row_num}`\n"
            f"✏️ `/edit {row_num} <អត្ថបទ>`"
        )
        await update.message.reply_text(reply, parse_mode="Markdown")

        if os.path.exists(file_path):
            os.remove(file_path)
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📸 កំពុងវិភាគ...")
    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        file_path = f"/tmp/{photo.file_id}.jpg"
        await file.download_to_drive(file_path)

        loop = asyncio.get_event_loop()
        date_str, event = await loop.run_in_executor(
            None, extract_from_image, file_path
        )

        if not event:
            await update.message.reply_text("❌ វិភាគមិនបាន")
            return

        row_num = save_to_sheet(date_str, event, "[រូបភាព]")
        reply = (
            f"✅ *កត់ត្រាពីរូបភាព!*\n\n"
            f"🔢 `#{row_num}`\n"
            f"📅 {date_str}\n"
            f"📝 {event}"
        )
        await update.message.reply_text(reply, parse_mode="Markdown")

        if os.path.exists(file_path):
            os.remove(file_path)
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text or text.startswith("/"):
        return
    try:
        date_str = parse_khmer_date(text)
        event = extract_event(text)
        row_num = save_to_sheet(date_str, event, text)
        reply = (
            f"✅ *កត់ត្រា!*\n\n"
            f"🔢 `#{row_num}`\n📅 {date_str}\n📝 {event}\n\n"
            f"🗑 `/delete {row_num}`\n"
            f"✏️ `/edit {row_num} <អត្ថបទ>`"
        )
        await update.message.reply_text(reply, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


# ══════════════════════════════════════
# 🔔 AUTO REMINDER (Friday 8:00 AM)
# ══════════════════════════════════════

async def send_weekly_reminder(context: ContextTypes.DEFAULT_TYPE):
    """ផ្ញើ Calendar សប្តាហ៍ក្រោយរាល់ថ្ងៃសុក្រ ព្រឹកម៉ោង ៨:០០"""
    logger.info("🔔 Sending weekly reminder...")

    if not CHAT_ID:
        logger.warning("CHAT_ID not set, skipping reminder")
        return

    try:
        today = datetime.now(TZ).date()
        # Monday ក្រោយ
        next_monday = today + timedelta(days=(7 - today.weekday()))

        loop = asyncio.get_event_loop()
        img_bytes = await loop.run_in_executor(
            None, generate_week_calendar, next_monday
        )

        end = next_monday + timedelta(days=6)
        caption = (
            f"🔔 *រំលឹកកាលវិភាគសប្តាហ៍ក្រោយ*\n\n"
            f"📅 {next_monday.strftime('%Y-%m-%d')} → {end.strftime('%Y-%m-%d')}\n\n"
            f"💡 សូមរៀបចំខ្លួនសម្រាប់សប្តាហ៍ថ្មី!"
        )

        await context.bot.send_photo(
            chat_id=CHAT_ID,
            photo=img_bytes,
            caption=caption,
            parse_mode="Markdown"
        )
        logger.info("✅ Weekly reminder sent!")
    except Exception as e:
        logger.error(f"Reminder error: {e}")


# ══════════════════════════════════════
# Flask
# ══════════════════════════════════════

flask_app = Flask(__name__)


@flask_app.route("/")
def home():
    return "🤖 Bot is running!"


@flask_app.route("/health")
def health():
    return {"status": "ok"}


def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT)


# ══════════════════════════════════════
# Main
# ══════════════════════════════════════

def run_bot():
    logger.info("Starting bot...")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("week", week_command))
    app.add_handler(CommandHandler("nextweek", nextweek_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("calendar", calendar_command))
    app.add_handler(CommandHandler("delete", delete_command))
    app.add_handler(CommandHandler("edit", edit_command))

    # Messages
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # 🔔 Schedule Weekly Reminder (Friday 8:00 AM Phnom Penh)
    job_queue = app.job_queue
    reminder_time = dtime(hour=8, minute=0, tzinfo=TZ)
    job_queue.run_daily(
        send_weekly_reminder,
        time=reminder_time,
        days=(4,),  # Friday = 4 (Monday=0)
        name="weekly_reminder"
    )
    logger.info("📅 Weekly reminder scheduled: Every Friday at 8:00 AM")

    logger.info("Bot is running!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info(f"Flask started on port {PORT}")
    run_bot()
