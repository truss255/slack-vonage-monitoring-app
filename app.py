# slack_vonage_app.py
from flask import Flask, request, jsonify
import os, json, requests
from datetime import datetime, timedelta
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)

# ENV variables
SLACK_BOT_TOKEN = os.environ['SLACK_BOT_TOKEN']
SLACK_SIGNING_SECRET = os.environ['SLACK_SIGNING_SECRET']
SHEET_ID = os.environ['SHEET_ID']
ALERT_CHANNEL_ID = os.environ['ALERT_CHANNEL_ID']
SERVICE_ACCOUNT_FILE = os.getenv('SERVICE_ACCOUNT_FILE', 'service_account.json')

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
sheets_service = build('sheets', 'v4', credentials=creds)

# Slack headers
headers = {
    'Authorization': f'Bearer {SLACK_BOT_TOKEN}',
    'Content-Type': 'application/json'
}

# ========== Helper Functions ==========
def post_slack_message(channel, blocks):
    payload = {
        "channel": channel,
        "blocks": blocks
    }
    requests.post('https://slack.com/api/chat.postMessage', headers=headers, json=payload)

def append_to_sheet(sheet_tab, values):
    sheets_service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range=f"{sheet_tab}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": [values]}
    ).execute()

def format_duration(seconds):
    return str(timedelta(seconds=int(seconds)))

# ========== Real-Time Alert Route ==========
@app.route("/vonage-events", methods=["POST"])
def vonage_events():
    data = request.json
    event_type = data.get("eventType")
    agent = data.get("agent", {}).get("name", "Unknown")
    duration = format_duration(data.get("duration", 0))
    interaction_id = data.get("interactionId", "-")
    slack_user = f"@{agent}"  # Must match Slack username

    alert_text = f"*{event_type} Alert*\nAgent: {slack_user}\nDuration: {duration}\nInteraction ID: {interaction_id}"
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": alert_text}},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "Assigned to Me"}, "value": "assign", "action_id": "assign_action"}
        ]}
    ]
    post_slack_message(ALERT_CHANNEL_ID, blocks)
    return jsonify({"status": "posted"}), 200

# ========== Slack Interactions ==========
@app.route("/slack/interactions", methods=["POST"])
def slack_interactions():
    payload = json.loads(request.form["payload"])
    action_id = payload["actions"][0]["action_id"]
    user = payload["user"]["username"]
    response_url = payload["response_url"]

    if action_id == "assign_action":
        # Post new button to trigger follow-up modal
        follow_up_button = {
            "replace_original": True,
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": f"✅ Assigned to: @{user}"}},
                {"type": "actions", "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "Submit Follow-Up"}, "value": "followup", "action_id": "followup_modal"}
                ]}
            ]
        }
        requests.post(response_url, json=follow_up_button)

    return "", 200

# ========== Slack Modal Trigger ==========
@app.route("/slack/commands", methods=["POST"])
def slack_commands():
    command = request.form.get("command")
    trigger_id = request.form.get("trigger_id")

    if command == "/weeklyupdate":
        modal_view = {
            "type": "modal",
            "callback_id": "weekly_update_modal",
            "title": {"type": "plain_text", "text": "Weekly Report"},
            "submit": {"type": "plain_text", "text": "Submit"},
            "blocks": [
                {"type": "input", "block_id": "date_range", "label": {"type": "plain_text", "text": "Week Date Range (e.g. Apr 2–Apr 8)"},
                 "element": {"type": "plain_text_input", "action_id": "range"}},
                {"type": "input", "block_id": "top_perf", "label": {"type": "plain_text", "text": "Top Performers"},
                 "element": {"type": "plain_text_input", "action_id": "value"}},
                {"type": "input", "block_id": "bottom_perf", "label": {"type": "plain_text", "text": "Bottom Performers"},
                 "element": {"type": "plain_text_input", "action_id": "value"}},
                {"type": "input", "block_id": "coaching", "label": {"type": "plain_text", "text": "Coaching Notes"},
                 "element": {"type": "plain_text_input", "action_id": "value"}}
            ]
        }
        requests.post("https://slack.com/api/views.open", headers=headers, json={
            "trigger_id": trigger_id,
            "view": modal_view
        })
        return "", 200

    return "", 404

# ========== Slack View Submission ==========
@app.route("/slack/views", methods=["POST"])
def handle_modal_submission():
    payload = json.loads(request.form["payload"])
    state_values = payload["view"]["state"]["values"]

    date_range = state_values["date_range"]["range"]["value"]
    top_perf = state_values["top_perf"]["value"]["value"]
    bottom_perf = state_values["bottom_perf"]["value"]["value"]
    coaching = state_values["coaching"]["value"]["value"]

    sheets_service.spreadsheets().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"requests": [
            {"addSheet": {"properties": {"title": date_range}}}
        ]}
    ).execute()

    append_to_sheet(date_range, ["Top Performers", top_perf])
    append_to_sheet(date_range, ["Bottom Performers", bottom_perf])
    append_to_sheet(date_range, ["Coaching Notes", coaching])

    return "", 200

# ========== Daily Report Trigger (e.g., via cron job) ==========
@app.route("/daily-report", methods=["GET"])
def daily_report():
    report_date = (datetime.utcnow() - timedelta(days=1)).strftime("%b %d")
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📊 Daily Agent Performance Report – {report_date}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "🚨 *Agents Who Missed 1+ Thresholds:*\n• @Crystalbell Miranda – Wrap ❗\n• @Rebecca Stokes – Call Time ❗\n• @Carleisha Smith – Ready ❗ Not Ready ❗"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "✅ *Agents Meeting All Targets:*\n• @Jessica Lopez\n• @Jason McLaughlin"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "🏅 *Top Performer:* @Jeanette Bantz – 0 alerts 🎯"}]},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "👁️ Acknowledge Report"}, "value": "acknowledge"}
        ]}
    ]
    post_slack_message(ALERT_CHANNEL_ID, blocks)
    return jsonify({"status": "report sent"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)

