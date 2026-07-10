from flask import Flask, request
import requests
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
import os
import json

app = Flask(__name__)

# ទាញយក Environment Variables
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '')
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '')

def setup_sheet():
    """
    តភ្ជាប់ទៅ Google Sheets
    Sheet Name: ReminderSheet
    Tab Name: VoiceRecord
    """
    try:
        # អាន credentials ពី environment variable
        creds_json = os.environ.get('SHEET_CREDS', '{}')
        creds_dict = json.loads(creds_json)
        
        # កំណត់ scope
        scope = [
            'https://spreadsheets.google.com/feeds',
            'https://www.googleapis.com/auth/drive'
        ]
        
        # បង្កើត credentials
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)
        
        # បើក Sheet ឈ្មោះ "ReminderSheet" និង Tab ឈ្មោះ "VoiceRecord"
        spreadsheet = client.open("ReminderSheet")
        worksheet = spreadsheet.worksheet("VoiceRecord")
        
        return worksheet
        
    except Exception as e:
        print(f"❌ Sheet connection error: {e}")
        return None

def transcribe_audio(file_url):
    """
    បម្លែងសំលេងទៅអត្ថបទដោយប្រើ Groq Whisper API
    Language: Khmer (km)
    Model: whisper-large-v3
    """
    try:
        # ទាញយកឯកសារសំលេងពី Telegram
        print(f"📥 Downloading audio from: {file_url}")
        audio_response = requests.get(file_url, timeout=30)
        
        if audio_response.status_code != 200:
            return f"❌ មិនអាចទាញយកឯកសារបាន (Status: {audio_response.status_code})"
        
        # រៀបចំ request ទៅ Groq API
        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}"
        }
        
        files = {
            "file": ("audio.ogg", audio_response.content, "audio/ogg")
        }
        
        data = {
            "model": "whisper-large-v3",
            "language": "km",  # ភាសាខ្មែរ
            "temperature": 0.0,
            "response_format": "json"
        }
        
        # ផ្ញើ request ទៅ Groq
        print("🔄 Sending to Groq API...")
        response = requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers=headers,
            files=files,
            data=data,
            timeout=30
        )
        
        # ពិនិត្យលទ្ធផល
        if response.status_code == 200:
            result = response.json()
            text = result.get("text", "")
            
            if text.strip():
                print(f"✅ Transcription success: {text[:50]}...")
                return text
            else:
                return "❌ មិនអាចបម្លែងសំលេងបាន (ទទេ)"
        else:
            error_msg = f"❌ Groq API Error (Status: {response.status_code})"
            print(f"{error_msg}: {response.text}")
            return error_msg
            
    except requests.exceptions.Timeout:
        return "❌ ការបម្លែងយូរពេក សូមព្យាយាមម្តងទៀត"
    except Exception as e:
        error_msg = f"❌ Error: {str(e)}"
        print(error_msg)
        return error_msg

def send_message(chat_id, text):
    """ផ្ញើសារទៅ Telegram"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    
    data = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    
    try:
        response = requests.post(url, json=data, timeout=10)
        return response.status_code == 200
    except Exception as e:
        print(f"❌ Failed to send message: {e}")
        return False

@app.route('/' + TELEGRAM_TOKEN, methods=['POST'])
def webhook():
    """
    ទទួល webhook ពី Telegram
    ដំណើរការសារសំលេង និងរក្សាទុកទៅ Google Sheet
    """
    try:
        update = request.get_json()
        print(f"📨 Received update: {update}")
        
        # ពិនិត្យថាមានសារសំលេងឬអត់
        if "message" in update and "voice" in update["message"]:
            message = update["message"]
            chat_id = message["chat"]["id"]
            file_id = message["voice"]["file_id"]
            
            # ទាញយកព័ត៌មានអំពីឯកសារសំលេង
            file_info_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
            print(f"📞 Getting file info from: {file_info_url}")
            
            file_info_response = requests.get(file_info_url, timeout=10)
            file_info = file_info_response.json()
            
            if "result" in file_info and "file_path" in file_info["result"]:
                file_path = file_info["result"]["file_path"]
                file_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
                
                # ផ្ញើសារថាកំពុងដំណើរការ
                send_message(chat_id, "⏳ <b>កំពុងដំណើរការសំលេងរបស់អ្នក...</b>\nសូមរង់ចាំបន្តិច")
                
                # បម្លែងសំលេងទៅអត្ថបទ
                print("🎤 Starting transcription...")
                transcribed_text = transcribe_audio(file_url)
                
                # ទាញយកកាលបរិច្ឆេទ និងពេលវេលា
                now = datetime.now()
                date_str = now.strftime("%d/%m/%Y")  # ទម្រង់: ថ្ងៃ/ខែ/ឆ្នាំ
                time_str = now.strftime("%H:%M:%S")   # ទម្រង់: ម៉ោង:នាទី:វិនាទី
                
                print(f"📅 Date: {date_str}, Time: {time_str}")
                print(f"📝 Text: {transcribed_text}")
                
                # រក្សាទុកទៅ Google Sheet
                sheet = setup_sheet()
                
                if sheet:
                    try:
                        # បន្ថែមជួរថ្មីទៅ Sheet
                        row_data = [date_str, time_str, transcribed_text]
                        sheet.append_row(row_data)
                        print(f"✅ Saved to Sheet: {row_data}")
                        
                        # រៀបចំសារឆ្លើយតប
                        response_message = (
                            f"✅ <b>កត់ត្រារួចរាល់!</b>\n\n"
                            f"📅 <b>កាលបរិច្ឆេទ:</b> {date_str}\n"
                            f"🕐 <b>ម៉ោង:</b> {time_str}\n\n"
                            f"📝 <b>ព្រឹត្តការណ៍:</b>\n{transcribed_text}\n\n"
                            f"💾 ទិន្នន័យត្រូវបានរក្សាទុកក្នុង Google Sheet ហើយ"
                        )
                        
                        send_message(chat_id, response_message)
                        
                    except Exception as sheet_error:
                        error_msg = (
                            f"⚠️ <b>បម្លែងសំលេងបានជោគជ័យ ប៉ុន្តែមិនអាចរក្សាទុកបាន</b>\n\n"
                            f"📝 <b>អត្ថបទ:</b>\n{transcribed_text}\n\n"
                            f"❌ <b>កំហុស:</b> {str(sheet_error)}\n\n"
                            f"💡 សូមពិនិត្យ:\n"
                            f"• Sheet ឈ្មោះ: <code>ReminderSheet</code>\n"
                            f"• Tab ឈ្មោះ: <code>VoiceRecord</code>\n"
                            f"• បាន Share ទៅ Service Account"
                        )
                        send_message(chat_id, error_msg)
                        print(f"❌ Sheet save error: {sheet_error}")
                else:
                    error_msg = (
                        f"⚠️ <b>បម្លែងសំលេងបានជោគជ័យ ប៉ុន្តែមិនអាចភ្ជាប់ Sheet បាន</b>\n\n"
                        f"📝 <b>អត្ថបទ:</b>\n{transcribed_text}\n\n"
                        f"💡 សូមពិនិត្យ:\n"
                        f"• Sheet ត្រូវដាក់ឈ្មោះ: <code>ReminderSheet</code>\n"
                        f"• Tab ត្រូវដាក់ឈ្មោះ: <code>VoiceRecord</code>\n"
                        f"• Sheet ត្រូវ Share ទៅ Service Account email"
                    )
                    send_message(chat_id, error_msg)
            else:
                send_message(chat_id, "❌ មិនអាចទាញយកឯកសារសំលេងបាន")
                
        return "OK", 200
        
    except Exception as e:
        print(f"❌ Webhook error: {str(e)}")
        return "Error", 500

@app.route('/')
def index():
    """ទំព័រដើម - បង្ហាញស្ថានភាព Bot"""
    return """
    <html>
        <head>
            <title>Telegram Voice Bot</title>
            <meta charset="UTF-8">
            <style>
                body {
                    font-family: Arial, sans-serif;
                    max-width: 600px;
                    margin: 50px auto;
                    padding: 20px;
                    background: #f5f5f5;
                }
                .status {
                    background: white;
                    padding: 30px;
                    border-radius: 10px;
                    box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                }
                h1 { color: #0088cc; }
                .info { 
                    background: #e3f2fd;
                    padding: 15px;
                    border-radius: 5px;
                    margin: 10px 0;
                }
                code {
                    background: #f5f5f5;
                    padding: 2px 6px;
                    border-radius: 3px;
                }
            </style>
        </head>
        <body>
            <div class="status">
                <h1>🤖 Telegram Voice Bot</h1>
                <p>✅ Bot កំពុងដំណើរការ!</p>
                
                <div class="info">
                    <h3>📋 ការកំណត់រចនាសម្ព័ន្ធ:</h3>
                    <ul>
                        <li><strong>Sheet Name:</strong> <code>ReminderSheet</code></li>
                        <li><strong>Tab Name:</strong> <code>VoiceRecord</code></li>
                        <li><strong>Columns:</strong> កាលបរិច្ឆេទ | ម៉ោង | ព្រឹត្តការណ៍</li>
                    </ul>
                </div>
                
                <div class="info">
                    <h3>🎤 របៀបប្រើប្រាស់:</h3>
                    <ol>
                        <li>ស្វែងរក Bot របស់អ្នកនៅលើ Telegram</li>
                        <li>ចុច Start</li>
                        <li>ថតសំលេងជាភាសាខ្មែរ</li>
                        <li>ផ្ញើសំលេងទៅ Bot</li>
                        <li>រង់ចាំ Bot បម្លែង និងរក្សាទុក</li>
                    </ol>
                </div>
            </div>
        </body>
    </html>
    """

@app.route('/health')
def health():
    """Health check endpoint សម្រាប់ Render"""
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "sheet_name": "ReminderSheet",
        "tab_name": "VoiceRecord"
    }, 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    print(f"🚀 Starting bot on port {port}")
    print(f"📊 Sheet: ReminderSheet / Tab: VoiceRecord")
    app.run(host='0.0.0.0', port=port)
