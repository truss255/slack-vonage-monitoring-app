from flask import Flask, request, jsonify
import os, json, requests
from datetime import datetime, timedelta
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)

# ========== ENV VARS ==========
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]
ALERT_CHANNEL_ID = os.environ["ALERT_CHANNEL_ID"]
SHEET_ID = os.environ["SHEET_ID"]
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
# Handle the service account JSON from environment variable
GOOGLE_SERVICE_ACCOUNT_JSON = None

try:
    # First, try direct parsing
    raw_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")

    # Print the first few characters for debugging (avoid printing the entire key for security)
    if len(raw_json) > 10:
        print(f"GOOGLE_SERVICE_ACCOUNT_JSON starts with: {raw_json[:10]}...")
        print(f"Length of GOOGLE_SERVICE_ACCOUNT_JSON: {len(raw_json)} characters")

    GOOGLE_SERVICE_ACCOUNT_JSON = json.loads(raw_json)
    print("Successfully parsed GOOGLE_SERVICE_ACCOUNT_JSON")
except json.JSONDecodeError as e:
    print(f"Error parsing GOOGLE_SERVICE_ACCOUNT_JSON: {e}")
    # Print more detailed information about the JSON format
    if len(raw_json) > 0:
        print(f"First character: '{raw_json[0]}', ASCII value: {ord(raw_json[0])}")
        if len(raw_json) > 1:
            print(f"Second character: '{raw_json[1]}', ASCII value: {ord(raw_json[1])}")
        # Check for common issues
        if raw_json.startswith("{") and not raw_json.startswith("{\""):
            print("JSON appears to use incorrect quote format for property names")
        elif raw_json.startswith("'") or raw_json.startswith('"'):
            print("JSON appears to be wrapped in quotes, which is invalid")
    try:
        # Try fixing common issues with JSON formatting
        raw_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")

        # Check if it's wrapped in single quotes
        if raw_json.startswith("'") and raw_json.endswith("'"):
            raw_json = raw_json[1:-1]

        # Try various fixes for common JSON formatting issues
        fixed_json = raw_json

        # 1. Replace single quotes with double quotes
        if "'" in fixed_json:
            fixed_json = fixed_json.replace("'", "\"")

        # 2. Handle escaped newlines in private_key
        if "\\n" in fixed_json:
            # This is common in Railway where newlines get escaped
            fixed_json = fixed_json.replace("\\n", "\n")

        # 3. Handle double-escaped newlines
        if "\\\\n" in fixed_json:
            fixed_json = fixed_json.replace("\\\\n", "\n")

        # 4. Check if the JSON is a string representation of a JSON object
        if fixed_json.startswith('"') and fixed_json.endswith('"') and '{' in fixed_json and '}' in fixed_json:
            # This might be a string-encoded JSON, try to unescape it
            try:
                # First parse it as a string
                string_value = json.loads(fixed_json)
                # Then parse the string as JSON
                if isinstance(string_value, str) and string_value.startswith('{') and string_value.endswith('}'):
                    fixed_json = string_value
                    print("Detected string-encoded JSON, attempting to parse the inner content")
            except:
                # If this fails, continue with other approaches
                pass

        try:
            GOOGLE_SERVICE_ACCOUNT_JSON = json.loads(fixed_json)
            print("Successfully parsed JSON after applying fixes")
        except json.JSONDecodeError as e:
            print(f"Still failed to parse JSON after fixes: {e}")
            # If we can't parse it, create a minimal valid JSON
            print("Could not parse service account JSON, using empty object")
            GOOGLE_SERVICE_ACCOUNT_JSON = {}
    except Exception as e:
        print(f"Failed to process service account JSON: {e}")
        # Provide a fallback empty JSON object
        GOOGLE_SERVICE_ACCOUNT_JSON = {}

# Check if we have a valid service account JSON
if not GOOGLE_SERVICE_ACCOUNT_JSON or not isinstance(GOOGLE_SERVICE_ACCOUNT_JSON, dict):
    print("WARNING: Invalid or missing Google service account credentials")
    # Create a minimal valid JSON structure
    GOOGLE_SERVICE_ACCOUNT_JSON = {}

# ========== GOOGLE SHEETS CLIENT ==========
sheets_service = None
try:
    # Only attempt to create credentials if we have a valid service account JSON
    if GOOGLE_SERVICE_ACCOUNT_JSON and isinstance(GOOGLE_SERVICE_ACCOUNT_JSON, dict) and GOOGLE_SERVICE_ACCOUNT_JSON.get("type") == "service_account":
        creds = service_account.Credentials.from_service_account_info(GOOGLE_SERVICE_ACCOUNT_JSON, scopes=SCOPES)
        sheets_service = build("sheets", "v4", credentials=creds)
        print("Successfully initialized Google Sheets service")
    else:
        print("WARNING: Invalid Google service account JSON format, Sheets functionality will be disabled")
except Exception as e:
    print(f"ERROR: Failed to initialize Google Sheets service: {e}")

# ========== SLACK ==========
headers = {
    "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
    "Content-Type": "application/json"
}

def post_slack_message(channel, blocks):
    payload = {
        "channel": channel,
        "blocks": blocks
    }
    requests.post("https://slack.com/api/chat.postMessage", headers=headers, json=payload)

# ========== UTIL ==========
def current_week_range():
    today = datetime.utcnow()
    monday = today - timedelta(days=today.weekday())
    end = monday + timedelta(days=6)
    return f"{monday.strftime('%b %d')}–{end.strftime('%b %d')}"

# ========== VONAGE WEBHOOK ==========
@app.route("/vonage-events", methods=["POST"])
def vonage_events():
    data = request.json
    agent = data.get("agent", {}).get("name", "Unknown")
    event_type = data.get("eventType", "Unknown")
    duration = data.get("duration", "N/A")
    campaign = data.get("interactionId", "-")

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"⚠️ *{event_type} Alert*\nAgent: @{agent}\nDuration: {duration}\nCampaign: {campaign}"}},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "✅ Assigned to Me"}, "value": f"assign|{agent}|{campaign}"}
        ]}
    ]
    post_slack_message(ALERT_CHANNEL_ID, blocks)
    return jsonify({"status": "posted"}), 200

# ========== SLACK INTERACTIONS ==========
@app.route("/slack/interactions", methods=["POST"])
def slack_interactions():
    payload = json.loads(request.form["payload"])
    action = payload["actions"][0]["value"]
    user = payload["user"]["username"]
    response_url = payload["response_url"]

    if action.startswith("assign|"):
        agent, campaign = action.split("|")[1:]
        followup_btn = {
            "type": "button",
            "text": {"type": "plain_text", "text": "📝 Submit Follow-Up"},
            "value": f"followup|{agent}|{campaign}",
            "action_id": "open_modal"
        }
        requests.post(response_url, json={
            "replace_original": False,
            "text": f"✅ @{user} is handling this alert for @{agent}.",
            "blocks": [{"type": "actions", "elements": [followup_btn]}]
        })

    return "", 200

# ========== FOLLOW-UP MODAL ==========
@app.route("/slack/command", methods=["POST"])
def slack_command():
    trigger_id = request.form["trigger_id"]
    modal = {
        "trigger_id": trigger_id,
        "view": {
            "type": "modal",
            "callback_id": "followup_submit",
            "title": {"type": "plain_text", "text": "Follow-Up"},
            "submit": {"type": "plain_text", "text": "Submit"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "daterange",
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "range_input",
                        "placeholder": {"type": "plain_text", "text": "e.g. Apr 7–Apr 13"}
                    },
                    "label": {"type": "plain_text", "text": "Date Range"}
                },
                {
                    "type": "input",
                    "block_id": "action",
                    "element": {
                        "type": "static_select",
                        "action_id": "action_taken",
                        "options": [
                            {"text": {"type": "plain_text", "text": "👁️ Monitoring"}, "value": "monitoring"},
                            {"text": {"type": "plain_text", "text": "🎧 Listen In"}, "value": "listen_in"},
                            {"text": {"type": "plain_text", "text": "☎️ Reached Out"}, "value": "reached_out"},
                            {"text": {"type": "plain_text", "text": "📋 Investigated"}, "value": "investigated"}
                        ]
                    },
                    "label": {"type": "plain_text", "text": "What action did you take?"}
                }
            ]
        }
    }
    requests.post("https://slack.com/api/views.open", headers=headers, json=modal)
    return "", 200

@app.route("/slack/view_submission", methods=["POST"])
def view_submission():
    payload = json.loads(request.form["payload"])
    values = payload["view"]["state"]["values"]
    date_range = values["daterange"]["range_input"]["value"]
    action = values["action"]["action_taken"]["selected_option"]["text"]["text"]
    user = payload["user"]["username"]

    # Log into Google Sheet if the service is available
    try:
        if sheets_service:
            sheet_name = date_range
            body = {"values": [[datetime.utcnow().isoformat(), f"@{user}", action]]}
            sheets_service.spreadsheets().values().append(
                spreadsheetId=SHEET_ID,
                range=f"'{sheet_name}'!A1",
                valueInputOption="USER_ENTERED",
                body=body
            ).execute()
            print(f"Successfully logged follow-up action to Google Sheet: {sheet_name}")
        else:
            print("WARNING: Google Sheets service not available, skipping logging")
    except Exception as e:
        print(f"ERROR: Failed to log to Google Sheet: {e}")

    return jsonify({"response_action": "clear"}), 200

# ========== DAILY REPORT ==========
@app.route("/daily-report", methods=["GET"])
def daily_report():
    report_date = (datetime.utcnow() - timedelta(days=1)).strftime("%b %d")
    top_performer = "@Jeanette Bantz"

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📊 Daily Agent Report – {report_date}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "🚨 *Missed Targets:*\n• @Crystalbell Miranda – Wrap ❗\n• @Rebecca Stokes – Call Time ❗\n• @Carleisha Smith – Ready ❗ Not Ready ❗"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "✅ *Met All Targets:*\n• @Jessica Lopez\n• @Jason McLaughlin"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"🏅 *Top Performer:* {top_performer} – 0 alerts 🎯"}]},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "👁️ Acknowledge"}, "value": "ack_report"}
        ]}
    ]
    post_slack_message(ALERT_CHANNEL_ID, blocks)
    return jsonify({"status": "report posted"}), 200

@app.route("/", methods=["GET"])
def index():
    return "✅ Slack + Vonage Monitoring App is live!"

# ========== RUN ==========
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
