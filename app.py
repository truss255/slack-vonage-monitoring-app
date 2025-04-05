from flask import Flask, request, jsonify
import os
import json
import requests
from datetime import datetime, timedelta
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)

# Slack + Sheet config
SLACK_BOT_TOKEN = os.environ['SLACK_BOT_TOKEN']
SLACK_SIGNING_SECRET = os.environ['SLACK_SIGNING_SECRET']
SHEET_ID = os.environ['SHEET_ID']
ALERT_CHANNEL_ID = os.environ['ALERT_CHANNEL_ID']

# Google Sheets credentials from file
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
SERVICE_ACCOUNT_FILE = os.getenv('SERVICE_ACCOUNT_FILE', 'service_account.json')
creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
sheets_service = build('sheets', 'v4', credentials=creds)

# Slack post message setup
headers = {
    'Authorization': f'Bearer {SLACK_BOT_TOKEN}',
    'Content-Type': 'application/json'
}

def post_slack_message(channel, blocks):
    payload = {
        "channel": channel,
        "blocks": blocks
    }
    requests.post('https://slack.com/api/chat.postMessage', headers=headers, json=payload)

@app.route("/vonage-events", methods=["POST"])
def vonage_events():
    data = request.json
    agent = data.get("agent", {}).get("name", "Unknown")
    event_type = data.get("eventType")
    duration = data.get("duration", "N/A")
    interaction_id = data.get("interactionId", "-")

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{event_type} Alert*\nAgent: @{agent}\nDuration: {duration}\nInteraction ID: {interaction_id}"}},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "Assigned to Me"}, "value": "assign"}
        ]}
    ]
    post_slack_message(ALERT_CHANNEL_ID, blocks)
    return jsonify({"status": "alert posted"}), 200

@app.route("/slack/interactions", methods=["POST"])
def slack_interactions():
    payload = json.loads(request.form["payload"])
    user = payload["user"]["username"]
    action = payload["actions"][0]["value"]
    response_url = payload["response_url"]

    if action == "assign":
        requests.post(response_url, json={"text": f"✅ Assigned to: @{user}"})
    return "", 200

@app.route("/daily-report", methods=["GET"])
def daily_report():
    now = datetime.utcnow() - timedelta(days=1)
    report_date = now.strftime("%b %d")
    top_performer = "@Jeanette Bantz"

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📊 Daily Agent Performance Report – {report_date}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "🚨 *Agents Who Missed 1+ Thresholds:*\n• @Crystalbell Miranda – Wrap ❗\n• @Rebecca Stokes – Call Time ❗\n• @Carleisha Smith – Ready ❗ Not Ready ❗"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "✅ *Agents Meeting All Targets:*\n• @Jessica Lopez\n• @Jason McLaughlin"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"🏅 *Top Performer:* {top_performer} – 0 alerts 🎯"}]},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "👁️ Acknowledge Report"}, "value": "acknowledge"}
        ]}
    ]
    post_slack_message(ALERT_CHANNEL_ID, blocks)
    return jsonify({"status": "report sent"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
