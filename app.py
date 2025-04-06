from flask import Flask, request, jsonify
import os
import json
import requests
from datetime import datetime, timedelta
from dateutil.parser import parse
from google.oauth2 import service_account
from googleapiclient.discovery import build
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
import hmac
import hashlib
import time

app = Flask(__name__)

# ========== ENV VARS ==========
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]
ALERT_CHANNEL_ID = os.environ["ALERT_CHANNEL_ID"]
SHEET_ID = os.environ["SHEET_ID"]
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Handle Google service account JSON
GOOGLE_SERVICE_ACCOUNT_JSON = None
try:
    raw_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    GOOGLE_SERVICE_ACCOUNT_JSON = json.loads(raw_json)
    print("Successfully parsed GOOGLE_SERVICE_ACCOUNT_JSON")
except json.JSONDecodeError as e:
    print(f"Error parsing GOOGLE_SERVICE_ACCOUNT_JSON: {e}")
    raw_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    if raw_json.startswith("'") and raw_json.endswith("'"):
        raw_json = raw_json[1:-1]
    fixed_json = raw_json.replace("'", '"').replace("\\n", "\n")
    try:
        GOOGLE_SERVICE_ACCOUNT_JSON = json.loads(fixed_json)
        print("Parsed JSON after fixes")
    except json.JSONDecodeError:
        print("Could not parse JSON, using empty object")
        GOOGLE_SERVICE_ACCOUNT_JSON = {}

if not GOOGLE_SERVICE_ACCOUNT_JSON or not isinstance(GOOGLE_SERVICE_ACCOUNT_JSON, dict):
    print("WARNING: Invalid Google service account credentials")
    GOOGLE_SERVICE_ACCOUNT_JSON = {}

# ========== GOOGLE SHEETS CLIENT ==========
sheets_service = None
try:
    if GOOGLE_SERVICE_ACCOUNT_JSON.get("type") == "service_account":
        creds = service_account.Credentials.from_service_account_info(GOOGLE_SERVICE_ACCOUNT_JSON, scopes=SCOPES)
        sheets_service = build("sheets", "v4", credentials=creds)
        print("Initialized Google Sheets service")
except Exception as e:
    print(f"ERROR: Failed to initialize Google Sheets: {e}")

# ========== SLACK ==========
headers = {
    "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
    "Content-Type": "application/json"
}

def post_slack_message(channel, blocks):
    print(f"Attempting to post to Slack channel: {channel}")
    payload = {"channel": channel, "blocks": blocks}
    response = requests.post("https://slack.com/api/chat.postMessage", headers=headers, json=payload)
    if response.status_code != 200:
        print(f"Failed to post to Slack: {response.text}")
    else:
        print("Successfully posted to Slack")

# ========== UTILITY FUNCTIONS ==========
def current_week_range():
    today = datetime.utcnow()
    monday = today - timedelta(days=today.weekday())
    end = monday + timedelta(days=6)
    return f"{monday.strftime('%b %d')}–{end.strftime('%b %d')}"

def get_emoji_for_event(event_type):
    emoji_map = {
        "Wrap": "📝",
        "Outgoing Wrap Up": "📝",
        "Ready": "📞",
        "Lunch": "🍽️",
        "Break": "☕",
        "Comfort Break": "🚻",
        "Logged Out": "🔌",
        "Device Busy": "💻",
        "Unreachable": "⚠️",
        "Training": "📚",
        "In Meeting": "👥",
        "Paperwork": "🗂️",
        "Idle": "❗",
        "Away": "🚶‍♂️"
    }
    return emoji_map.get(event_type, "⚠️")

# ========== SHIFT DETAILS ==========
agent_shifts = {
    "Adriana Jimenez Cartegena": {
        "timezone": "Atlantic",
        "shifts": {
            "Mon": ("9am", "5pm"),
            "Tue": ("9am", "5pm"),
            "Wed": ("9am", "5pm"),
            "Thu": ("9am", "5pm"),
            "Fri": ("9am", "5pm")
        }
    },
    "Angie Rivera": {
        "timezone": "Eastern",
        "shifts": {
            "Mon": ("12PM", "8PM"),
            "Tue": ("12PM", "8PM"),
            "Wed": ("12PM", "8PM"),
            "Thu": ("12PM", "8PM"),
            "Fri": ("11am", "7pm")
        }
    },
    "Brandon Pagan Sostre": {
        "timezone": "Atlantic",
        "shifts": {
            "Mon": ("11am", "7pm"),
            "Tue": ("11am", "7pm"),
            "Wed": ("11am", "7pm"),
            "Thu": ("11am", "7pm"),
            "Fri": ("11am", "7pm")
        }
    },
    "Briana Pagan": {
        "timezone": "Atlantic",
        "shifts": {
            "Mon": ("11am", "7pm"),
            "Tue": ("11am", "7pm"),
            "Wed": ("11am", "7pm"),
            "Thu": ("11am", "7pm"),
            "Fri": ("11am", "7pm")
        }
    },
    "Brittany Bland": {
        "timezone": "Central",
        "shifts": {
            "Mon": ("8am", "4pm"),
            "Tue": ("8am", "4pm"),
            "Wed": ("8am", "4pm"),
            "Thu": ("8am", "4pm"),
            "Fri": ("8am", "4pm")
        }
    },
    "Carla Hagerman": {
        "timezone": "Eastern",
        "shifts": {
            "Tue": ("10am", "6pm"),
            "Wed": ("10am", "6pm"),
            "Thu": ("10am", "6pm"),
            "Fri": ("10am", "6pm"),
            "Sat": ("4pm", "12am")
        }
    },
    "Carleisha Smith": {
        "timezone": "Central",
        "shifts": {
            "Thu": ("12am", "8am"),
            "Fri": ("12am", "8am"),
            "Sat": ("12am", "8am"),
            "Sun": ("12am", "8am"),
            "Mon": ("12am", "8am")
        }
    },
    "Cassandra Dunn": {
        "timezone": "Pacific",
        "shifts": {
            "Mon": ("1pm", "9pm"),
            "Tue": ("1pm", "9pm"),
            "Wed": ("1pm", "9pm"),
            "Thu": ("1pm", "9pm"),
            "Fri": ("1pm", "9pm")
        }
    },
    "Crystalbell Miranda": {
        "timezone": "Pacific",
        "shifts": {
            "Tue": ("12pm", "8pm"),
            "Wed": ("12pm", "8pm"),
            "Thu": ("12pm", "8pm"),
            "Fri": ("12pm", "8pm"),
            "Sat": ("12pm", "8pm")
        }
    },
    "Danitza Maravilla": {
        "timezone": "Pacific",
        "shifts": {
            "Sun": ("11am", "7pm"),
            "Mon": ("11am", "7pm"),
            "Tue": ("11am", "7pm"),
            "Wed": ("11am", "7pm"),
            "Thu": ("11am", "7pm")
        }
    },
    "Dejah \"Dee\" Blackwell": {
        "timezone": "Central",
        "shifts": {
            "Sun": ("10am", "6pm"),
            "Mon": ("10am", "6pm"),
            "Tue": ("10am", "6pm"),
            "Wed": ("10am", "6pm"),
            "Thu": ("10am", "6pm")
        }
    },
    "Felicia Martin": {
        "timezone": "Eastern",
        "shifts": {
            "Mon": ("8am", "4pm"),
            "Tue": ("8am", "4pm"),
            "Wed": ("8am", "4pm"),
            "Thu": ("8am", "4pm"),
            "Fri": ("8am", "4pm")
        }
    },
    "Felicia Randall": {
        "timezone": "Atlantic",
        "shifts": {
            "Tue": ("11am", "7pm"),
            "Wed": ("11am", "7pm"),
            "Thu": ("11am", "7pm"),
            "Fri": ("11am", "7pm"),
            "Sat": ("11am", "7pm")
        }
    },
    "Indira Gonzalez": {
        "timezone": "Eastern",
        "shifts": {
            "Tue": ("2pm", "10pm"),
            "Wed": ("2pm", "10pm"),
            "Thu": ("2pm", "10pm"),
            "Fri": ("2pm", "10pm"),
            "Sat": ("2pm", "10pm")
        }
    },
    "Jason McLaughlin": {
        "timezone": "Central",
        "shifts": {
            "Sun": ("8am", "4pm"),
            "Mon": ("8am", "4pm"),
            "Tue": ("8am", "4pm"),
            "Wed": ("8am", "4pm"),
            "Thu": ("8am", "4pm")
        }
    },
    "Jeanette Bantz": {
        "timezone": "Central",
        "shifts": {
            "Tue": ("9am", "5pm"),
            "Wed": ("9am", "5pm"),
            "Thu": ("9am", "5pm"),
            "Fri": ("9am", "5pm"),
            "Sat": ("8am", "4pm")
        }
    },
    "Jesse Lorenzana": {
        "timezone": "Atlantic",
        "shifts": {
            "Mon": ("11am", "7pm"),
            "Tue": ("11am", "7pm"),
            "Wed": ("11am", "7pm"),
            "Thu": ("11am", "7pm"),
            "Fri": ("11am", "7pm")
        }
    },
    "Jessica Lopez": {
        "timezone": "Atlantic",
        "shifts": {
            "Mon": ("12am", "8am"),
            "Tue": ("12am", "8am"),
            "Wed": ("12am", "8am"),
            "Thu": ("12am", "8am"),
            "Fri": ("12am", "8am")
        }
    },
    "Lakeira Robinson": {
        "timezone": "Eastern",
        "shifts": {
            "Sun": ("10am", "6pm"),
            "Mon": ("10am", "6pm"),
            "Tue": ("10am", "6pm"),
            "Wed": ("10am", "6pm"),
            "Thu": ("10am", "6pm")
        }
    },
    "Lyne Jean": {
        "timezone": "Eastern",
        "shifts": {
            "Sun": ("8am", "4pm"),
            "Mon": ("8am", "4pm"),
            "Tue": ("8am", "4pm"),
            "Wed": ("8am", "4pm"),
            "Thu": ("8am", "4pm")
        }
    },
    "Natalie Sukhu": {
        "timezone": "Eastern",
        "shifts": {
            "Tue": ("2pm", "10pm"),
            "Wed": ("2pm", "10pm"),
            "Thu": ("2pm", "10pm"),
            "Fri": ("2pm", "10pm"),
            "Sat": ("2pm", "10pm")
        }
    },
    "Nicole Coleman": {
        "timezone": "Pacific",
        "shifts": {
            "Tue": ("10am", "6pm"),
            "Wed": ("10am", "6pm"),
            "Thu": ("10am", "6pm"),
            "Fri": ("10am", "6pm"),
            "Sat": ("10am", "6pm")
        }
    },
    "Peggy Richardson": {
        "timezone": "Pacific",
        "shifts": {
            "Sun": ("10am", "6pm"),
            "Mon": ("10am", "6pm"),
            "Tue": ("10am", "6pm"),
            "Wed": ("10am", "6pm"),
            "Thu": ("10am", "6pm")
        }
    },
    "Ramona Marshall": {
        "timezone": "Eastern",
        "shifts": {
            "Mon": ("8am", "4pm"),
            "Tue": ("8am", "4pm"),
            "Wed": ("8am", "4pm"),
            "Thu": ("8am", "4pm"),
            "Fri": ("8am", "4pm")
        }
    },
    "Rebecca Stokes": {
        "timezone": "Central",
        "shifts": {
            "Sun": ("4pm", "12am"),
            "Mon": ("4pm", "12am"),
            "Tue": ("4pm", "12am"),
            "Wed": ("4pm", "12am"),
            "Thu": ("4pm", "12am")
        }
    }
}

# Agent teams
agent_teams = {
    "Carla Hagerman": "Team Adriana 💎",
    "Dajah Blackwell": "Team Adriana 💎",
    "Felicia Martin": "Team Adriana 💎",
    "Felicia Randall": "Team Adriana 💎",
    "Jeanette Bantz": "Team Adriana 💎",
    "Jesse Lorenzana": "Team Adriana 💎",
    "Nicole Coleman": "Team Adriana 💎",
    "Peggy Richardson": "Team Adriana 💎",
    "Ramona Marshall": "Team Adriana 💎",
    "Lyne Jean": "Team Bee Hive 🐝",
    "Danitza Maravilla": "Team Bee Hive 🐝",
    "Crystalbell Miranda": "Team Bee Hive 🐝",
    "Cassandra Dunn": "Team Bee Hive 🐝",
    "Briana Roque": "Team Bee Hive 🐝",
    "Natalie Sukhu": "Team Bee Hive 🐝",
    "Rebecca Stokes": "Team Bee Hive 🐝",
    "Jason McLaughlin": "Team Bee Hive 🐝",
    "Indira Gonzalez": "Team Bee Hive 🐝",
    "Carleisha Smith": "Team Bee Hive 🐝",
    "Lakeira Robinson": "Team Bee Hive 🐝"
}

# ========== TIMEZONE HANDLING ==========
def is_within_shift(agent, timestamp):
    agent_data = agent_shifts.get(agent)
    if not agent_data:
        print(f"WARNING: Agent {agent} not found in shift data")
        return False
    tz = pytz.timezone(agent_data["timezone"])
    local_time = timestamp.astimezone(tz)
    day = local_time.strftime("%a")
    shift = agent_data["shifts"].get(day)
    if not shift:
        return False
    start_str, end_str = shift
    start_time = tz.localize(datetime.strptime(f"{local_time.date()} {start_str}", "%Y-%m-%d %I%p"))
    end_time = tz.localize(datetime.strptime(f"{local_time.date()} {end_str}", "%Y-%m-%d %I%p"))
    return start_time <= local_time <= end_time

# ========== DURATION PARSING ==========
def parse_duration(duration):
    if "min" in duration:
        return float(duration.split()[0])
    elif "s" in duration:
        return float(duration.split()[0]) / 60
    else:
        return 0

# ========== STATUS RULES ==========
def should_trigger_alert(event_type, duration_min, is_in_shift):
    if event_type in ["Wrap", "Outgoing Wrap Up"] and duration_min > 3:
        return True
    elif event_type == "Ready" and duration_min > 2 and is_in_shift:
        return True
    elif event_type == "Lunch" and duration_min > 30:
        return True
    elif event_type == "Break" and duration_min > 15:
        return True
    elif event_type == "Comfort Break" and duration_min > 5:
        return True
    elif event_type == "Logged Out" and is_in_shift:
        return True
    elif event_type in ["Device Busy", "Unreachable"] and duration_min > 3:
        return True
    elif event_type in ["Training", "In Meeting", "Paperwork"] and not is_scheduled(event_type, agent, timestamp):
        return True
    elif event_type in ["Idle", "Away"]:
        return True
    return False

def is_scheduled(event_type, agent, timestamp):
    # Placeholder for scheduled event logic
    return False

# ========== VONAGE WEBHOOK ==========
@app.route("/vonage-events", methods=["POST"])
def vonage_events():
    print("Received request to /vonage-events")
    try:
        data = request.json
        print(f"Vonage event payload: {data}")
        event_type = data.get("eventType", "Unknown")

        # Handle disposition records
        if event_type == "activityrecord":
            agent = data.get("agent", {}).get("name", "Unknown")
            timestamp_str = data.get("timestamp", datetime.utcnow().isoformat())
            timestamp = parse(timestamp_str).replace(tzinfo=pytz.UTC)
            disposition = data.get("disposition", "Unknown")

            # Log the disposition to Google Sheets
            log_disposition(agent, disposition, timestamp)
            return jsonify({"status": "disposition logged"}), 200

        # Handle status alerts (original functionality)
        agent = data.get("agent", {}).get("name", "Unknown")
        duration = data.get("duration", "N/A")
        campaign = data.get("interactionId", "-")
        timestamp_str = data.get("timestamp", datetime.utcnow().isoformat())
        timestamp = parse(timestamp_str).replace(tzinfo=pytz.UTC)

        duration_min = parse_duration(duration)
        is_in_shift = is_within_shift(agent, timestamp)
        print(f"Event: {event_type}, Duration: {duration_min}, In Shift: {is_in_shift}")

        if should_trigger_alert(event_type, duration_min, is_in_shift):
            emoji = get_emoji_for_event(event_type)
            team = agent_teams.get(agent, "Unknown Team")
            interaction_link = f"https://dashboard.vonage.com/interactions/{campaign}"
            blocks = [
                {"type": "section", "text": {"type": "mrkdwn", "text": f"{emoji} *{event_type} Alert*\nAgent: @{agent}\nTeam: {team}\nDuration: {duration}\nCampaign: {campaign}"}},
                {"type": "actions", "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "✅ Assigned to Me"}, "value": f"assign|{agent}|{campaign}", "action_id": "assign_to_me"},
                    {"type": "button", "text": {"type": "plain_text", "text": "🔗 View Interaction"}, "url": interaction_link, "action_id": "view_interaction"}
                ]}
            ]
            post_slack_message(ALERT_CHANNEL_ID, blocks)
        return jsonify({"status": "posted"}), 200
    except Exception as e:
        print(f"Error processing Vonage event: {e}")
        return jsonify({"status": "error"}), 500

# ========== DISPOSITION LOGGING ==========
def log_disposition(agent, disposition, timestamp):
    """Log agent call dispositions to Google Sheets"""
    try:
        if not sheets_service:
            print("WARNING: Google Sheets service not available, skipping disposition logging")
            return

        date_str = timestamp.strftime("%Y-%m-%d")
        sheet_name = f"Dispositions {date_str}"
        get_or_create_sheet(sheets_service, SHEET_ID, sheet_name)
        values = [[timestamp.isoformat(), agent, disposition]]
        sheets_service.spreadsheets().values().append(
            spreadsheetId=SHEET_ID,
            range=f"'{sheet_name}'!A1",
            valueInputOption="USER_ENTERED",
            body={"values": values}
        ).execute()
        print(f"Logged disposition for {agent}: {disposition}")
    except Exception as e:
        print(f"ERROR: Failed to log disposition: {e}")

def generate_disposition_summary(date):
    """Generate a summary of dispositions for a specific date"""
    try:
        if not sheets_service:
            return "Google Sheets service not available"

        sheet_name = f"Dispositions {date}"
        try:
            result = sheets_service.spreadsheets().values().get(
                spreadsheetId=SHEET_ID,
                range=f"'{sheet_name}'!A1:C"
            ).execute()
        except Exception:
            return "No data found for this date."

        rows = result.get("values", [])
        if not rows or len(rows) <= 1:
            return "No disposition data found for this date."

        from collections import defaultdict
        count_map = defaultdict(lambda: defaultdict(int))
        for row in rows[1:]:  # Skip header row
            if len(row) >= 3:
                _, agent, dispo = row[:3]
                count_map[agent][dispo] += 1

        summary = ["*Disposition Summary:*\n"]
        for agent, dispos in count_map.items():
            summary.append(f"• *{agent}*:")
            for dispo, count in dispos.items():
                summary.append(f"   - {dispo}: {count}")
        return "\n".join(summary)
    except Exception as e:
        print(f"ERROR: Failed to generate disposition summary: {e}")
        return f"Error generating disposition summary: {str(e)}"

# Add route for disposition report
@app.route("/disposition-report", methods=["GET"])
def disposition_report():
    print("Received request to /disposition-report")
    try:
        date = request.args.get('date', (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d"))
        summary = generate_disposition_summary(date)
        export_link = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid=0"
        blocks = [
            {"type": "header", "text": {"type": "plain_text", "text": f"📊 Disposition Report – {date}"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": summary}},
            {"type": "context", "elements": [
                {"type": "mrkdwn", "text": f"📎 *Full Report:* <{export_link}|View in Google Sheets>"}
            ]}
        ]
        post_slack_message(ALERT_CHANNEL_ID, blocks)
        return jsonify({"status": "report sent", "date": date})
    except Exception as e:
        print(f"Error in disposition-report: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# ========== SLACK REQUEST VERIFICATION ==========
def verify_slack_request(request):
    print("Verifying Slack request")
    timestamp = request.headers.get('X-Slack-Request-Timestamp')
    if not timestamp or abs(time.time() - int(timestamp)) > 60 * 5:
        print(f"Verification failed: Invalid timestamp {timestamp}")
        return False
    sig_basestring = f"v0:{timestamp}:{request.get_data().decode('utf-8')}"
    my_signature = 'v0=' + hmac.new(
        SLACK_SIGNING_SECRET.encode('utf-8'),
        sig_basestring.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    slack_signature = request.headers.get('X-Slack-Signature')
    if not hmac.compare_digest(my_signature, slack_signature):
        print(f"Verification failed: Signature mismatch. Expected {my_signature}, got {slack_signature}")
        return False
    print("Slack request verified successfully")
    return True

# ========== SLACK INTERACTIONS ==========
@app.route("/slack/interactions", methods=["POST"])
def slack_interactions():
    print("Received request to /slack/interactions")
    if not verify_slack_request(request):
        return "Invalid request", 403
    payload = json.loads(request.form["payload"])
    print(f"Interactivity payload: {payload}")
    action_id = payload["actions"][0]["action_id"] if payload["type"] == "block_actions" else ""
    user = payload["user"]["username"]
    response_url = payload["response_url"]

    if action_id == "assign_to_me":
        value = payload["actions"][0]["value"]
        _, agent, campaign = value.split("|")
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"🔍 @{user} is investigating this alert for @{agent}."}},
            {"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "📝 Follow-Up"}, "value": f"followup|{agent}|{campaign}", "action_id": "open_followup"}
            ]}
        ]
        requests.post(response_url, json={"replace_original": True, "blocks": blocks})

    elif action_id == "open_followup":
        value = payload["actions"][0]["value"]
        _, agent, campaign = value.split("|")
        trigger_id = payload["trigger_id"]
        modal = {
            "trigger_id": trigger_id,
            "view": {
                "type": "modal",
                "callback_id": "followup_submit",
                "title": {"type": "plain_text", "text": "Follow-Up"},
                "submit": {"type": "plain_text", "text": "Submit"},
                "blocks": [
                    {"type": "input", "block_id": "monitoring", "element": {
                        "type": "static_select", "placeholder": {"type": "plain_text", "text": "Select an option"},
                        "options": [
                            {"text": {"type": "plain_text", "text": "Listen In"}, "value": "listen_in"},
                            {"text": {"type": "plain_text", "text": "Coach"}, "value": "coach"},
                            {"text": {"type": "plain_text", "text": "Join"}, "value": "join"},
                            {"text": {"type": "plain_text", "text": "None"}, "value": "none"}
                        ],
                        "action_id": "monitoring_method"
                    }, "label": {"type": "plain_text", "text": "Monitoring Method"}},
                    {"type": "input", "block_id": "action", "element": {
                        "type": "plain_text_input", "action_id": "action_taken",
                        "placeholder": {"type": "plain_text", "text": "e.g. Coached agent, verified call handling"}
                    }, "label": {"type": "plain_text", "text": "What did you do?"}},
                    {"type": "input", "block_id": "reason", "element": {
                        "type": "plain_text_input", "action_id": "reason_for_issue",
                        "placeholder": {"type": "plain_text", "text": "e.g. Client had multiple questions"}
                    }, "label": {"type": "plain_text", "text": "Reason for issue"}},
                    {"type": "input", "block_id": "notes", "element": {
                        "type": "plain_text_input", "action_id": "additional_notes",
                        "placeholder": {"type": "plain_text", "text": "Optional comments"}
                    }, "label": {"type": "plain_text", "text": "Additional notes"}}
                ],
                "private_metadata": json.dumps({"agent": agent, "campaign": campaign})
            }
        }
        requests.post("https://slack.com/api/views.open", headers=headers, json=modal)

    return "", 200

# ========== FOLLOW-UP MODAL SUBMISSION ==========
@app.route("/slack/view_submission", methods=["POST"])
def view_submission():
    print("Received request to /slack/view_submission")
    if not verify_slack_request(request):
        return "Invalid request", 403
    payload = json.loads(request.form["payload"])
    print(f"View submission payload: {payload}")
    if payload["type"] != "view_submission":
        return "", 200

    if payload["view"]["callback_id"] == "followup_submit":
        values = payload["view"]["state"]["values"]
        metadata = json.loads(payload["view"]["private_metadata"])
        agent = metadata["agent"]
        campaign = metadata["campaign"]
        monitoring = values["monitoring"]["monitoring_method"]["selected_option"]["value"]
        action = values["action"]["action_taken"]["value"]
        reason = values["reason"]["reason_for_issue"]["value"]
        notes = values["notes"]["additional_notes"]["value"]
        user = payload["user"]["username"]

        try:
            if sheets_service:
                sheet_name = "FollowUps"
                get_or_create_sheet(sheets_service, SHEET_ID, sheet_name)
                body = {"values": [[datetime.utcnow().isoformat(), agent, campaign, monitoring, action, reason, notes, user]]}
                sheets_service.spreadsheets().values().append(
                    spreadsheetId=SHEET_ID,
                    range=f"'{sheet_name}'!A1",
                    valueInputOption="USER_ENTERED",
                    body=body
                ).execute()
                print(f"Logged follow-up to Google Sheet: {sheet_name}")
        except Exception as e:
            print(f"ERROR: Failed to log to Google Sheet: {e}")

    elif payload["view"]["callback_id"] == "weekly_submit":
        values = payload["view"]["state"]["values"]
        week = values["week"]["week_input"]["value"]
        top_performers = values["top_performers"]["top_performers_input"]["value"]
        support_actions = values["support_actions"]["support_actions_input"]["value"]
        bottom_performers = values["bottom_performers"]["bottom_performers_input"]["value"]
        action_plans = values["action_plans"]["action_plans_input"]["value"]
        improvement_status = values["improvement_status"]["improvement_status_input"]["value"]
        trends = values["trends"]["trends_input"]["value"]
        team_progress = values["team_progress"]["team_progress_input"]["value"]
        user = payload["user"]["username"]

        try:
            if sheets_service:
                sheet_name = f"Weekly {week}"
                body = {
                    "values": [[
                        datetime.utcnow().isoformat(), f"@{user}", top_performers, support_actions,
                        bottom_performers, action_plans, improvement_status, trends, team_progress
                    ]]
                }
                sheets_service.spreadsheets().values().append(
                    spreadsheetId=SHEET_ID,
                    range=f"'{sheet_name}'!A1",
                    valueInputOption="USER_ENTERED",
                    body=body
                ).execute()
                print(f"Logged weekly update to Google Sheet: {sheet_name}")
        except Exception as e:
            print(f"ERROR: Failed to log to Google Sheet: {e}")

    return jsonify({"response_action": "clear"}), 200

# Helper function to create or get sheet
def get_or_create_sheet(service, spreadsheet_id, sheet_name):
    try:
        spreadsheet = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        sheets = [s['properties']['title'] for s in spreadsheet['sheets']]
        if sheet_name not in sheets:
            requests_body = [{'addSheet': {'properties': {'title': sheet_name}}}]
            service.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={'requests': requests_body}).execute()
        return sheet_name
    except Exception as e:
        print(f"Error creating sheet {sheet_name}: {e}")
        return sheet_name

# ========== SLACK COMMANDS ==========
@app.route("/slack/commands/daily_report", methods=["GET", "POST"])
def slack_command_daily_report():
    print(f"Received {request.method} request to /slack/commands/daily_report")
    if request.method == "GET":
        print("Slack verification request received")
        return "This endpoint is for Slack slash commands. Please use POST to send a command.", 200

    print(f"Slash command payload: {request.form}")
    if not verify_slack_request(request):
        print("Slack verification failed")
        return "Invalid request", 403

    print(f"Request form: {request.form}")
    # Generate and post a daily report
    today = datetime.utcnow()
    report_date = today.strftime("%b %d")
    date_for_dispositions = today.strftime("%Y-%m-%d")

    # Get disposition summary for today (or yesterday if specified)
    text = request.form.get("text", "").strip()
    if text.lower() == "yesterday":
        today = today - timedelta(days=1)
        report_date = today.strftime("%b %d")
        date_for_dispositions = today.strftime("%Y-%m-%d")

    disposition_summary = generate_disposition_summary(date_for_dispositions)
    export_link = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid=0"

    top_performer = "@Jeanette Bantz"
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📊 Daily Agent Report – {report_date}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "🚨 *Missed Targets:*\n• @Crystalbell Miranda – Wrap ❗\n• @Rebecca Stokes – Call Time ❗\n• @Carleisha Smith – Ready ❗ Not Ready ❗"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "✅ *Met All Targets:*\n• @Jessica Lopez\n• @Jason McLaughlin"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"🏅 *Top Performer:* {top_performer} – 0 alerts 🎯"}]},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": disposition_summary}},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"📎 *Full Disposition Report:* <{export_link}|View in Google Sheets>"}
        ]},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "👁️ Acknowledge"}, "value": "ack_report"}
        ]}
    ]
    channel_id = request.form.get("channel_id")
    print(f"Posting to channel: {channel_id}")
    post_slack_message(channel_id, blocks)
    print("Message posted successfully")
    return "", 200

@app.route("/slack/commands/weekly_report", methods=["GET", "POST"])
def slack_command_weekly_report():
    print(f"Received {request.method} request to /slack/commands/weekly_report")
    if request.method == "GET":
        print("Slack verification request received")
        return "This endpoint is for Slack slash commands. Please use POST to send a command.", 200

    print(f"Slash command payload: {request.form}")
    if not verify_slack_request(request):
        print("Slack verification failed")
        return "Invalid request", 403

    # Generate and post the weekly metrics report
    channel_id = request.form.get("channel_id")
    print(f"Posting to channel: {channel_id}")

    # Get the date range for the previous week
    today = datetime.utcnow()
    end_date = today - timedelta(days=today.weekday() + 1)  # Last Sunday
    start_date = end_date - timedelta(days=6)  # Previous Monday
    date_range = f"{start_date.strftime('%b %d')}–{end_date.strftime('%b %d')}"

    # This would be where you'd fetch metrics from Vonage API
    vonage_report_url = f"https://dashboard.vonage.com/reports/weekly/{start_date.strftime('%Y-%m-%d')}"

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📈 Weekly Performance Report – {date_range}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Weekly Metrics Summary:*\n• Average Handle Time: 5m 23s\n• Average Wait Time: 32s\n• Abandonment Rate: 3.2%\n• Total Calls: 1,245"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Team Performance:*\n• Team Adriana 💎: 98.5% SLA\n• Team Bee Hive 🐝: 97.2% SLA"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Download the full report with detailed metrics:*"}},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "📊 Download Full Report"}, "url": vonage_report_url}
        ]}
    ]

    post_slack_message(channel_id, blocks)
    print("Message posted successfully")
    return "", 200

@app.route("/slack/commands/weekly_update_form", methods=["GET", "POST"])
def slack_command_weekly_update_form():
    print(f"Received {request.method} request to /slack/commands/weekly_update_form")
    if request.method == "GET":
        print("Slack verification request received")
        return "This endpoint is for Slack slash commands. Please use POST to send a command.", 200

    print(f"Slash command payload: {request.form}")
    if not verify_slack_request(request):
        print("Slack verification failed")
        return "Invalid request", 403

    trigger_id = request.form["trigger_id"]
    # Open the weekly update modal
    modal = {
        "trigger_id": trigger_id,
        "view": {
            "type": "modal",
            "callback_id": "weekly_submit",
            "title": {"type": "plain_text", "text": "Weekly Update"},
            "submit": {"type": "plain_text", "text": "Submit"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "week",
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "week_input",
                        "placeholder": {"type": "plain_text", "text": "e.g. Apr 7–Apr 13"}
                    },
                    "label": {"type": "plain_text", "text": "Week Covered"}
                },
                {
                    "type": "input",
                    "block_id": "top_performers",
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "top_performers_input",
                        "placeholder": {"type": "plain_text", "text": "e.g. Jessica Lopez, Jason McLaughlin"}
                    },
                    "label": {"type": "plain_text", "text": "Who are your top performers?"}
                },
                {
                    "type": "input",
                    "block_id": "support_actions",
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "support_actions_input",
                        "placeholder": {"type": "plain_text", "text": "e.g. Recognition, additional training"}
                    },
                    "label": {"type": "plain_text", "text": "What are you doing to support them?"}
                },
                {
                    "type": "input",
                    "block_id": "bottom_performers",
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "bottom_performers_input",
                        "placeholder": {"type": "plain_text", "text": "e.g. Crystalbell Miranda, Rebecca Stokes"}
                    },
                    "label": {"type": "plain_text", "text": "Who are your bottom performers?"}
                },
                {
                    "type": "input",
                    "block_id": "action_plans",
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "action_plans_input",
                        "placeholder": {"type": "plain_text", "text": "e.g. Coaching, performance review"}
                    },
                    "label": {"type": "plain_text", "text": "What actions are being taken?"}
                },
                {
                    "type": "input",
                    "block_id": "improvement_status",
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "improvement_status_input",
                        "placeholder": {"type": "plain_text", "text": "e.g. Yes, No, Plan to improve"}
                    },
                    "label": {"type": "plain_text", "text": "Are they improving? If not, what’s the plan?"}
                },
                {
                    "type": "input",
                    "block_id": "trends",
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "trends_input",
                        "placeholder": {"type": "plain_text", "text": "e.g. Increased call times, better wrap-up"}
                    },
                    "label": {"type": "plain_text", "text": "What trends are you noticing?"}
                },
                {
                    "type": "input",
                    "block_id": "team_progress",
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "team_progress_input",
                        "placeholder": {"type": "plain_text", "text": "e.g. Yes, No, Areas for improvement"}
                    },
                    "label": {"type": "plain_text", "text": "Are we rising as a team?"}
                }
            ]
        }
    }
    print("Opening modal for weekly update form")
    requests.post("https://slack.com/api/views.open", headers=headers, json=modal)
    print("Modal request sent to Slack")
    return "", 200

# ========== DAILY REPORT SCHEDULER ==========
def trigger_daily_report():
    print("Triggering daily report")
    # Get yesterday's date for the report
    yesterday = datetime.utcnow() - timedelta(days=1)
    report_date = yesterday.strftime("%b %d")
    date_for_dispositions = yesterday.strftime("%Y-%m-%d")

    # Get disposition summary
    disposition_summary = generate_disposition_summary(date_for_dispositions)
    export_link = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid=0"

    # Agent performance data
    top_performer = "@Jeanette Bantz"

    # Create the report blocks
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📊 Daily Agent Report – {report_date}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "🚨 *Missed Targets:*\n• @Crystalbell Miranda – Wrap ❗\n• @Rebecca Stokes – Call Time ❗\n• @Carleisha Smith – Ready ❗ Not Ready ❗"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "✅ *Met All Targets:*\n• @Jessica Lopez\n• @Jason McLaughlin"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"🏅 *Top Performer:* {top_performer} – 0 alerts 🎯"}]},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": disposition_summary}},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"📎 *Full Disposition Report:* <{export_link}|View in Google Sheets>"}
        ]},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "👁️ Acknowledge"}, "value": "ack_report"}
        ]}
    ]
    post_slack_message(ALERT_CHANNEL_ID, blocks)

scheduler = BackgroundScheduler(timezone="US/Eastern")
# Add the daily report job
scheduler.add_job(trigger_daily_report, 'cron', hour=7, minute=0)

# ========== WEEKLY REPORT WITH VONAGE METRICS ==========
def generate_weekly_report():
    print("Generating weekly report")
    # Get the date range for the previous week
    today = datetime.utcnow()
    end_date = today - timedelta(days=today.weekday() + 1)  # Last Sunday
    start_date = end_date - timedelta(days=6)  # Previous Monday
    date_range = f"{start_date.strftime('%b %d')}–{end_date.strftime('%b %d')}"

    # This would be where you'd fetch metrics from Vonage API
    # For now, we'll use placeholder data
    vonage_report_url = f"https://dashboard.vonage.com/reports/weekly/{start_date.strftime('%Y-%m-%d')}"

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📈 Weekly Performance Report – {date_range}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Weekly Metrics Summary:*\n• Average Handle Time: 5m 23s\n• Average Wait Time: 32s\n• Abandonment Rate: 3.2%\n• Total Calls: 1,245"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Team Performance:*\n• Team Adriana 💎: 98.5% SLA\n• Team Bee Hive 🐝: 97.2% SLA"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Download the full report with detailed metrics:*"}},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "📊 Download Full Report"}, "url": vonage_report_url}
        ]}
    ]

    post_slack_message(ALERT_CHANNEL_ID, blocks)
    return {"status": "weekly report posted", "date_range": date_range}

# Add the weekly report job - runs every Monday at 9:00 AM Eastern
scheduler.add_job(generate_weekly_report, 'cron', day_of_week='mon', hour=9, minute=0)

# Start the scheduler
scheduler.start()

# Add a route to manually trigger the weekly report
@app.route("/weekly-report", methods=["GET"])
def weekly_report():
    print("Received request to /weekly-report")
    result = generate_weekly_report()
    return jsonify(result), 200

@app.route("/", methods=["GET"])
def index():
    print("Received request to /")
    return "✅ Slack + Vonage Monitoring App is live!"

# ========== RUN ==========
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)