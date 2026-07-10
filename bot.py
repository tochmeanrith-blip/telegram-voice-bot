import os
import re
import json
import asyncio
import logging
import threading
from datetime import datetime, timedelta

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

# ─── Load Environment Variables ───
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS")
PORT = int(os.environ.get("PORT", 10000))

# ─── Logging ───
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Gemini Setup (New Library) ───
gemini_client = genai.Client(api_key=GEMINI_API_KEY)
GEMINI_MODEL = "gemini-flash-latest"
logger.info("Gemini AI (new SDK) configured!")

# ─── Google Sheets Setup ───
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

logger.info("Google Sheet connected!")


# ──────────────────────────────────────
# Khmer Date Parsing
# ──────────────────────────────────────

KHMER_MONTHS = {
    "មករា": 1, "កុម្ភៈ": 2, "មីនា": 3, "មេសា": 4,
    "ឧសភា": 5, "មិថុនា": 6, "កក្កដា": 7, "សីហា": 8,
    "កញ្ញា": 9, "តុលា": 10, "វិច្ឆិកា": 11, "ធ្នូ": 12,
}

KHMER_RELATIVE_DAYS = {
    "ថ្ងៃនេះ": 0,
    "ថ្ងៃស្អែក": 1,
    "ខានស្អែក": 2,
    "ម្សិលមិញ": -1,
    "ម្សិលម៉្ង": -2,
}

KHMER_WEEKDAYS = {
    "ថ្ងៃច័ន្ទ": 0, "ថ្ងៃអង្គារ": 1, "ថ្ងៃពុធ": 2,
    "ថ្ងៃព្រហស្បតិ៍": 3, "ថ្ងៃសុក្រ": 4,
    "ថ្ងៃសៅរ៍": 5, "ថ្ងៃអាទិត្យ": 6,
}

KHMER_DIGITS = str.maketrans("០១២៣៤៥៦៧៨៩", "0123456789")


def khmer_to_arabic(text):
    return text.translate(KHMER_DIGITS)


def parse_khmer_date(text):
    today = datetime.now()
    normalized = khmer_to_arabic(text)

    for keyword, delta in KHMER_RELATIVE_DAYS.items():
        if keyword in text:
            target = today + timedelta(days=delta)
            return target.strftime("%Y-%m-%d")

    for keyword, weekday in KHMER_WEEKDAYS.items():
        if keyword in text:
            current_weekday = today.weekday()
            diff = weekday - current_weekday
            if diff <= 0:
                diff += 7
            target = today + timedelta(days=diff)
            return target.strftime("%Y-%m-%d")

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

    date_pattern = re.search(
        r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})", normalized
    )
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
    event = re.sub(
        r"ថ្ងៃទី\s*\S+\s*ខែ\s*\S+(\s*ឆ្នាំ\s*\S+)?", "", event
    )
    event = re.sub(r"\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}", "", event)
    event = re.sub(r"\s+", " ", event).strip()
    for prefix in ["មាន", "នឹង", "ត្រូវ", "ទៅ", "គឺ", ","]:
        if event.startswith(prefix):
            event = event[len(prefix):].strip()
    return event if event else text


def save_to_sheet(date_str, event, original_text):
    now = datetime.now()
    time_str = now.strftime("%H:%M:%S")
    all_values = worksheet.get_all_values()
    row_num = len(all_values)
    new_row = [row_num, date_str, time_str, event, original_text]
    worksheet.append_row(new_row, value_input_option="USER_ENTERED")
    logger.info(f"Saved: {new_row}")
    return row_num


# ──────────────────────────────────────
# Speech-to-Text (Gemini 2.5 Flash)
# ──────────────────────────────────────

def transcribe_audio(file_path):
    """Use Gemini to transcribe Khmer audio with retry & fallback."""
    import time

    # Fallback models (បើមួយ busy ប្តូរទៅមួយផ្សេង)
    models_to_try = [
        "gemini-flash-latest",
        "gemini-2.0-flash",
        "gemini-flash-lite-latest",
        "gemini-2.5-flash-lite",
    ]

    prompt = (
        "សូមស្តាប់សំឡេងនេះ ហើយបំលែងទៅជាអក្សរខ្មែរ។ "
        "សូមឆ្លើយតែអក្សរខ្មែរប៉ុណ្ណោះ គ្មានការពន្យល់អ្វីទេ។ "
        "Please transcribe this Khmer audio to Khmer text. "
        "Return ONLY the Khmer text, no explanation, no translation."
    )

    try:
        logger.info(f"Reading audio file: {file_path}")
        with open(file_path, "rb") as f:
            audio_bytes = f.read()
    except Exception as e:
        logger.error(f"Cannot read audio: {e}")
        return None

    # ព្យាយាមជាមួយ Model នីមួយៗ
    for model_name in models_to_try:
        # Retry 3 ដងក្នុង Model នីមួយៗ
        for attempt in range(3):
            try:
                logger.info(f"Trying {model_name} (attempt {attempt + 1})...")
                response = gemini_client.models.generate_content(
                    model=model_name,
                    contents=[
                        prompt,
                        types.Part.from_bytes(
                            data=audio_bytes,
                            mime_type="audio/ogg",
                        ),
                    ],
                )
                text = response.text.strip()
                logger.info(f"✅ Success with {model_name}: {text}")
                return text

            except Exception as e:
                error_msg = str(e)
                logger.warning(f"❌ {model_name} attempt {attempt + 1}: {error_msg[:100]}")

                # បើ 503 (busy) → រង់ចាំ 2 វិនាទី រួច retry
                if "503" in error_msg or "UNAVAILABLE" in error_msg:
                    time.sleep(2)
                    continue
                # បើ 429 (rate limit) → រង់ចាំ 5 វិនាទី
                elif "429" in error_msg:
                    time.sleep(5)
                    continue
                # Error ផ្សេងៗ → ប្តូរ Model
                else:
                    break

    logger.error("All models failed")
    return None


# ──────────────────────────────────────
# Telegram Handlers
# ──────────────────────────────────────

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome = (
        "🎙️ សួស្តី! ខ្ញុំជា Voice Tracker Bot\n\n"
        "របៀបប្រើ:\n"
        "1. ផ្ញើសំឡេង (Voice Message) ជាភាសាខ្មែរ\n"
        "2. ខ្ញុំនឹងកត់ត្រាទៅ Google Sheet\n\n"
        "ឧទាហរណ៍:\n"
        "🗣 ថ្ងៃស្អែក មានប្រជុំជាមួយក្រុមការងារ\n"
        "🗣 ថ្ងៃទី ១៥ ខែ មករា ទៅជួបគ្រូពេទ្យ\n\n"
        "Commands:\n"
        "/start - ចាប់ផ្តើម\n"
        "/history - មើលកំណត់ត្រាថ្មីៗ"
    )
    await update.message.reply_text(welcome)


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        all_values = worksheet.get_all_values()
        if len(all_values) <= 1:
            await update.message.reply_text("📭 មិនទាន់មានកំណត់ត្រាទេ")
            return
        recent = all_values[-5:]
        msg = "📋 កំណត់ត្រាថ្មីៗ:\n\n"
        for row in recent:
            if len(row) >= 4:
                msg += f"📅 {row[1]} | ⏰ {row[2]}\n📝 {row[3]}\n\n"
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"❌ បញ្ហា: {e}")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎧 កំពុងស្តាប់សំឡេង... សូមរង់ចាំ")
    try:
        voice = update.message.voice or update.message.audio
        if not voice:
            await update.message.reply_text("❌ រកមិនឃើញសំឡេង")
            return

        file = await context.bot.get_file(voice.file_id)
        file_path = f"/tmp/{voice.file_id}.ogg"
        await file.download_to_drive(file_path)

        await update.message.reply_text("🔄 កំពុងបំលែងសំឡេង (Gemini 2.5)...")
        loop = asyncio.get_event_loop()
        text = await loop.run_in_executor(
            None, transcribe_audio, file_path
        )

        if not text:
            await update.message.reply_text("❌ មិនអាចបំលែងបាន សូមព្យាយាមម្ដងទៀត")
            return

        date_str = parse_khmer_date(text)
        event = extract_event(text)
        row_num = save_to_sheet(date_str, event, text)

        reply = (
            f"✅ កត់ត្រាបានជោគជ័យ!\n\n"
            f"🔢 លេខរៀង: {row_num}\n"
            f"📅 កាលបរិច្ឆេទ: {date_str}\n"
            f"📝 ព្រឹត្តិការណ៍: {event}\n"
            f"💬 អត្ថបទដើម: {text}"
        )
        await update.message.reply_text(reply)

        if os.path.exists(file_path):
            os.remove(file_path)

    except Exception as e:
        logger.error(f"Voice error: {e}")
        await update.message.reply_text(f"❌ បញ្ហា: {e}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text or text.startswith("/"):
        return
    try:
        date_str = parse_khmer_date(text)
        event = extract_event(text)
        row_num = save_to_sheet(date_str, event, text)
        reply = (
            f"✅ កត់ត្រាបានជោគជ័យ!\n\n"
            f"🔢 លេខរៀង: {row_num}\n"
            f"📅 កាលបរិច្ឆេទ: {date_str}\n"
            f"📝 ព្រឹត្តិការណ៍: {event}"
        )
        await update.message.reply_text(reply)
    except Exception as e:
        await update.message.reply_text(f"❌ បញ្ហា: {e}")


# ──────────────────────────────────────
# Flask Web Server
# ──────────────────────────────────────

flask_app = Flask(__name__)


@flask_app.route("/")
def home():
    return "🤖 Telegram Bot is running!"


@flask_app.route("/health")
def health():
    return {"status": "ok", "bot": "running"}


def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT)


# ──────────────────────────────────────
# Run Telegram Bot
# ──────────────────────────────────────

def run_bot():
    logger.info("Starting Telegram bot...")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(
        MessageHandler(filters.VOICE | filters.AUDIO, handle_voice)
    )
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )
    logger.info("Bot is running!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info(f"Flask web server started on port {PORT}")

    run_bot()
