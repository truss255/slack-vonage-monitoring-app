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
try:
    GOOGLE_SERVICE_ACCOUNT_JSON = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
except json.JSONDecodeError as e:
    print(f"Error parsing GOOGLE_SERVICE_ACCOUNT_JSON: {e}")
    # If the JSON is improperly formatted, try to fix common issues
    raw_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    # Replace single quotes with double quotes if needed
    if raw_json.startswith("{") and "'" in raw_json:
        fixed_json = raw_json.replace("'", "\"")
        try:
            GOOGLE_SERVICE_ACCOUNT_JSON = json.loads(fixed_json)
            print("Successfully parsed JSON after fixing quotes")
        except json.JSONDecodeError:
            print("Failed to parse JSON even after fixing quotes")
            raise

# ========== GOOGLE SHEETS CLIENT ==========
creds = service_account.Credentials.from_service_account_info(GOOGLE_SERVICE_ACCOUNT_JSON, scopes=SCOPES)
sheets_service = build("sheets", "v4", credentials=creds)

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

    # Log into Google Sheet
    sheet_name = date_range
    body = {"values": [[datetime.utcnow().isoformat(), f"@{user}", action]]}
    sheets_service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range=f"'{sheet_name}'!A1",
        valueInputOption="USER_ENTERED",
        body=body
    ).execute()

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
