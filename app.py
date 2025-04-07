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
from collections import defaultdict

app = Flask(__name__)

# ========== ENV VARS ==========
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
ALERT_CHANNEL_ID = os.environ["ALERT_CHANNEL_ID"]
SHEET_ID = os.environ["SHEET_ID"]
SCOPES = json.loads(os.environ.get("GOOGLE_SHEETS_SCOPES", '["https://www.googleapis.com/auth/spreadsheets"]'))

# Map years to spreadsheet IDs
SPREADSHEET_IDS = {
    2025: os.environ["SHEET_ID"],
    2026: "1dlmzbFj5iC92oeDhrFzuJ-_eb_6sjXWMMZ6JNJ6EwoY"
}

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

# Dictionary to store sheets_service instances for each year
sheets_services = {}

def get_sheets_service(year):
    """Get or create a sheets_service instance for the given year."""
    if year not in sheets_services:
        spreadsheet_id = SPREADSHEET_IDS.get(year)
        if not spreadsheet_id:
            print(f"ERROR: No spreadsheet ID defined for year {year}")
            return None

        try:
            creds = service_account.Credentials.from_service_account_info(GOOGLE_SERVICE_ACCOUNT_JSON, scopes=SCOPES)
            sheets_service = build("sheets", "v4", credentials=creds)
            sheets_services[year] = (sheets_service, spreadsheet_id)
            print(f"Initialized Google Sheets service for year {year}")
        except Exception as e:
            print(f"ERROR: Failed to initialize Google Sheets for year {year}: {e}")
            return None

    return sheets_services[year]

# ========== SLACK ==========
headers = {
    "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
    "Content-Type": "application/json"
}

# ========== EMPLOYEE OPTIONS FOR MULTI-SELECT ==========
employee_options = [
    {"text": {"type": "plain_text", "text": "Briana Roque"}, "value": "briana_roque"},
    {"text": {"type": "plain_text", "text": "Carla Hagerman"}, "value": "carla_hagerman"},
    {"text": {"type": "plain_text", "text": "Carleisha Smith"}, "value": "carleisha_smith"},
    {"text": {"type": "plain_text", "text": "Cassandra Dunn"}, "value": "cassandra_dunn"},
    {"text": {"type": "plain_text", "text": "Crystalbell Miranda"}, "value": "crystalbell_miranda"},
    {"text": {"type": "plain_text", "text": "Dajah Blackwell"}, "value": "dajah_blackwell"},
    {"text": {"type": "plain_text", "text": "Felicia Martin"}, "value": "felicia_martin"},
    {"text": {"type": "plain_text", "text": "Felicia Randall"}, "value": "felicia_randall"},
    {"text": {"type": "plain_text", "text": "Indira Gonzalez"}, "value": "indira_gonzalez"},
    {"text": {"type": "plain_text", "text": "Jason McLaughlin"}, "value": "jason_mclaughlin"},
    {"text": {"type": "plain_text", "text": "Jeanette Bantz"}, "value": "jeanette_bantz"},
    {"text": {"type": "plain_text", "text": "Jesse Lorenzana Escarfullery"}, "value": "jesse_lorenzana_escarfullery"},
    {"text": {"type": "plain_text", "text": "Jessica Lopez"}, "value": "jessica_lopez"},
    {"text": {"type": "plain_text", "text": "Lakeira Robinson"}, "value": "lakeira_robinson"},
    {"text": {"type": "plain_text", "text": "Lyne Jean"}, "value": "lyne_jean"},
    {"text": {"type": "plain_text", "text": "Natalie Sukhu"}, "value": "natalie_sukhu"},
    {"text": {"type": "plain_text", "text": "Nicole Coleman"}, "value": "nicole_coleman"},
    {"text": {"type": "plain_text", "text": "Peggy Richardson"}, "value": "peggy_richardson"},
    {"text": {"type": "plain_text", "text": "Ramona Marshall"}, "value": "ramona_marshall"},
    {"text": {"type": "plain_text", "text": "Rebecca Stokes"}, "value": "rebecca_stokes"}
]

# Campaign mapping dictionary
CAMPAIGN_MAPPING = {
    "+13234547738": "SETC Incoming Calls",
    "+413122787476": "Maui Wildfire",
    "+313122192786": "Camp Lejeune",
    "+213122195489": "Depo-Provera",
    "+312132055684": "LA Wildfire"
}

def get_campaign_from_number(phone_number):
    """Map a Vonage phone number to a campaign name."""
    if not phone_number:
        return "Unknown Campaign"
    # Remove any leading "+" if present (just to normalize)
    normalized_number = phone_number.lstrip("+")
    # Check if the normalized number or original number matches any key
    for key, campaign in CAMPAIGN_MAPPING.items():
        if normalized_number == key.lstrip("+") or phone_number == key:
            return campaign
    return "Unknown Campaign"  # Fallback if no match is found

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

def get_week_range(date):
    """Calculate the week range (Monday to Sunday) for a given date."""
    # Find the Monday of the week
    monday = date - timedelta(days=date.weekday())
    # Find the Sunday of the week
    sunday = monday + timedelta(days=6)
    # Format as "Dispositions MMM D–MMM D"
    return f"Dispositions {monday.strftime('%b %-d')}–{sunday.strftime('%b %-d')}"

def get_emoji_for_event(event_type):
    emoji_map = {
        "Wrap": "📝",
        "Outgoing Wrap Up": "📝",
        "Ready": "📞",
        "Ready Outbound": "📤",
        "Handle Time": "📞",
        "Lunch": "🍽️",
        "Break": "☕",
        "Comfort Break": "🚻",
        "Logged Out": "🔌",
        "Device Busy": "💻",
        "Training": "📚",
        "In Meeting": "👥",
        "Paperwork": "🗂️",
        "Idle": "❗",
        "Away": "🚶‍♂️"
    }
    return emoji_map.get(event_type, "⚠️")

# ========== AGENT ID TO NAME MAPPING ==========
agent_id_to_name = {
    "10008": "Briana Roque",
    "1064": "Carla Hagerman",
    "1044": "Carleisha Smith",
    "10005": "Cassandra Dunn",
    "1033": "Crystalbell Miranda",
    "1113": "Dajah Blackwell",
    "1030": "Felicia Martin",
    "1045": "Felicia Randall",
    "1128": "Indira Gonzalez",
    "1060": "Jason McLaughlin",
    "1115": "Jeanette Bantz",
    "10019": "Jesse Lorenzana Escarfullery",
    "1003": "Jessica Lopez",
    "1058": "Lakeira Robinson",
    "1041": "Lyne Jean",
    "1056": "Natalie Sukhu",
    "1112": "Nicole Coleman",
    "1111": "Peggy Richardson",
    "10016": "Ramona Marshall",
    "1057": "Rebecca Stokes",
}

# ========== SHIFT DETAILS ==========
agent_shifts = {
    "Briana Roque": {
        "timezone": "Canada/Atlantic",
        "shifts": {
            "Mon": ("11am", "7pm"),
            "Tue": ("11am", "7pm"),
            "Wed": ("11am", "7pm"),
            "Thu": ("11am", "7pm"),
            "Fri": ("11am", "7pm")
        }
    },
    "Carla Hagerman": {
        "timezone": "US/Eastern",
        "shifts": {
            "Tue": ("10am", "6pm"),
            "Wed": ("10am", "6pm"),
            "Thu": ("10am", "6pm"),
            "Fri": ("10am", "6pm"),
            "Sat": ("4pm", "12am")
        }
    },
    "Carleisha Smith": {
        "timezone": "US/Central",
        "shifts": {
            "Thu": ("12am", "8am"),
            "Fri": ("12am", "8am"),
            "Sat": ("12am", "8am"),
            "Sun": ("12am", "8am"),
            "Mon": ("12am", "8am")
        }
    },
    "Cassandra Dunn": {
        "timezone": "US/Pacific",
        "shifts": {
            "Mon": ("1pm", "9pm"),
            "Tue": ("1pm", "9pm"),
            "Wed": ("1pm", "9pm"),
            "Thu": ("1pm", "9pm"),
            "Fri": ("1pm", "9pm")
        }
    },
    "Crystalbell Miranda": {
        "timezone": "US/Pacific",
        "shifts": {
            "Tue": ("12pm", "8pm"),
            "Wed": ("12pm", "8pm"),
            "Thu": ("12pm", "8pm"),
            "Fri": ("12pm", "8pm"),
            "Sat": ("12pm", "8pm")
        }
    },
    "Dajah Blackwell": {
        "timezone": "US/Central",
        "shifts": {
            "Sun": ("10am", "6pm"),
            "Mon": ("10am", "6pm"),
            "Tue": ("10am", "6pm"),
            "Wed": ("10am", "6pm"),
            "Thu": ("10am", "6pm")
        }
    },
    "Felicia Martin": {
        "timezone": "US/Eastern",
        "shifts": {
            "Mon": ("8am", "4pm"),
            "Tue": ("8am", "4pm"),
            "Wed": ("8am", "4pm"),
            "Thu": ("8am", "4pm"),
            "Fri": ("8am", "4pm")
        }
    },
    "Felicia Randall": {
        "timezone": "Canada/Atlantic",
        "shifts": {
            "Tue": ("11am", "7pm"),
            "Wed": ("11am", "7pm"),
            "Thu": ("11am", "7pm"),
            "Fri": ("11am", "7pm"),
            "Sat": ("11am", "7pm")
        }
    },
    "Indira Gonzalez": {
        "timezone": "US/Eastern",
        "shifts": {
            "Tue": ("2pm", "10pm"),
            "Wed": ("2pm", "10pm"),
            "Thu": ("2pm", "10pm"),
            "Fri": ("2pm", "10pm"),
            "Sat": ("2pm", "10pm")
        }
    },
    "Jason McLaughlin": {
        "timezone": "US/Central",
        "shifts": {
            "Sun": ("8am", "4pm"),
            "Mon": ("8am", "4pm"),
            "Tue": ("8am", "4pm"),
            "Wed": ("8am", "4pm"),
            "Thu": ("8am", "4pm")
        }
    },
    "Jeanette Bantz": {
        "timezone": "US/Central",
        "shifts": {
            "Tue": ("9am", "5pm"),
            "Wed": ("9am", "5pm"),
            "Thu": ("9am", "5pm"),
            "Fri": ("9am", "5pm"),
            "Sat": ("8am", "4pm")
        }
    },
    "Jesse Lorenzana Escarfullery": {
        "timezone": "Canada/Atlantic",
        "shifts": {
            "Mon": ("11am", "7pm"),
            "Tue": ("11am", "7pm"),
            "Wed": ("11am", "7pm"),
            "Thu": ("11am", "7pm"),
            "Fri": ("11am", "7pm")
        }
    },
    "Jessica Lopez": {
        "timezone": "Canada/Atlantic",
        "shifts": {
            "Mon": ("12am", "8am"),
            "Tue": ("12am", "8am"),
            "Wed": ("12am", "8am"),
            "Thu": ("12am", "8am"),
            "Fri": ("12am", "8am")
        }
    },
    "Lakeira Robinson": {
        "timezone": "US/Eastern",
        "shifts": {
            "Sun": ("10am", "6pm"),
            "Mon": ("10am", "6pm"),
            "Tue": ("10am", "6pm"),
            "Wed": ("10am", "6pm"),
            "Thu": ("10am", "6pm")
        }
    },
    "Lyne Jean": {
        "timezone": "US/Eastern",
        "shifts": {
            "Sun": ("8am", "4pm"),
            "Mon": ("8am", "4pm"),
            "Tue": ("8am", "4pm"),
            "Wed": ("8am", "4pm"),
            "Thu": ("8am", "4pm")
        }
    },
    "Natalie Sukhu": {
        "timezone": "US/Eastern",
        "shifts": {
            "Tue": ("2pm", "10pm"),
            "Wed": ("2pm", "10pm"),
            "Thu": ("2pm", "10pm"),
            "Fri": ("2pm", "10pm"),
            "Sat": ("2pm", "10pm")
        }
    },
    "Nicole Coleman": {
        "timezone": "US/Pacific",
        "shifts": {
            "Tue": ("10am", "6pm"),
            "Wed": ("10am", "6pm"),
            "Thu": ("10am", "6pm"),
            "Fri": ("10am", "6pm"),
            "Sat": ("10am", "6pm")
        }
    },
    "Peggy Richardson": {
        "timezone": "US/Pacific",
        "shifts": {
            "Sun": ("10am", "6pm"),
            "Mon": ("10am", "6pm"),
            "Tue": ("10am", "6pm"),
            "Wed": ("10am", "6pm"),
            "Thu": ("10am", "6pm")
        }
    },
    "Ramona Marshall": {
        "timezone": "US/Eastern",
        "shifts": {
            "Mon": ("8am", "4pm"),
            "Tue": ("8am", "4pm"),
            "Wed": ("8am", "4pm"),
            "Thu": ("8am", "4pm"),
            "Fri": ("8am", "4pm")
        }
    },
    "Rebecca Stokes": {
        "timezone": "US/Central",
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
    "Jesse Lorenzana Escarfullery": "Team Adriana 💎",
    "Nicole Coleman": "Team Adriana 💎",
    "Peggy Richardson": "Team Adriana 💎",
    "Ramona Marshall": "Team Adriana 💎",
    "Lyne Jean": "Team Bee Hive 🐝",
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
    try:
        tz = pytz.timezone(agent_data["timezone"])
    except pytz.exceptions.UnknownTimeZoneError as e:
        print(f"ERROR: Invalid timezone for agent {agent}: {agent_data['timezone']}. Error: {e}")
        return False
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
    # Duration is in milliseconds, convert to minutes
    try:
        duration_ms = int(duration)
        duration_min = duration_ms / 1000 / 60  # Convert ms to minutes
        return duration_min
    except (ValueError, TypeError):
        return 0

# ========== STATUS RULES ==========
def should_trigger_alert(event_type, duration_min, is_in_shift, event_data=None):
    # Map Vonage event types to internal statuses
    status = None
    if event_type == "channel.activityrecord.v0":
        disposition = event_data.get("interaction", {}).get("dispositionCode", "")
        # No longer mapping "No Answer" to Unreachable
        pass
    elif event_type == "channel.disconnected.v1":
        status = "Logged Out"
    elif event_type == "interaction.detailrecord.v0":
        channels = event_data.get("interaction", {}).get("channels", [])
        for channel in channels:
            for event in channel.get("channelEvents", []):
                if event.get("type") == "queue" and duration_min > 2:
                    status = "Ready"
                elif event.get("type") == "connected":
                    connected_duration = parse_duration(event.get("duration", 0))
                    if connected_duration > 8:
                        status = "Handle Time"
                if status:
                    break
            if status:
                break
    elif event_type == "agent.presencechanged.v1":
        presence_type = event_data.get("presence", {}).get("category", {}).get("type", "").lower()
        subcategory = event_data.get("presence", {}).get("category", {}).get("subcategory", "").lower()
        description = event_data.get("presence", {}).get("description", "").lower()
        if presence_type == "ready":
            if "outbound" in subcategory or "outbound" in description or "ready_outbound" in subcategory or "ready_outbound" in description:
                status = "Ready Outbound"
            else:
                status = "Ready"
        elif presence_type == "lunch":
            status = "Lunch"
        elif presence_type == "break":
            status = "Break"
        elif presence_type == "comfort_break":
            status = "Comfort Break"
        elif presence_type == "logged_out":
            status = "Logged Out"
        elif presence_type == "training":
            status = "Training"
        elif presence_type == "meeting":
            status = "In Meeting"
        elif presence_type == "paperwork":
            status = "Paperwork"
        elif presence_type == "idle":
            status = "Idle"
        elif presence_type == "away":
            status = "Away"
    elif event_type == "channel.alerted.v1":
        status = "Ready"  # Agent is being alerted for a call
    elif event_type == "channel.connected.v1":
        status = "Ready"  # Agent is connected to a call
    elif event_type == "channel.connectionfailed.v1":
        status = "Device Busy"  # Agent failed to connect
    elif event_type == "channel.ended.v1":
        status = "Logged Out"  # Call ended, agent might be idle
    elif event_type == "channel.held.v1":
        status = "Break"  # Call on hold, agent might be on a break
    elif event_type == "channel.interrupted.v1":
        status = "Break"  # Call interrupted, agent might be on a break
    elif event_type == "channel.parked.v1":
        status = "Away"  # Call parked, agent might be away
    elif event_type == "channel.resumed.v1":
        status = "Ready"  # Call resumed, agent is active
    elif event_type == "channel.retrieved.v1":
        status = "Ready"  # Call retrieved from hold, agent is active
    elif event_type == "channel.unparked.v1":
        status = "Ready"  # Call unparked, agent is active
    elif event_type == "channel.wrapstarted.v1":
        status = "Wrap"  # Agent is in wrap-up

    if not status:
        status = event_type  # Fallback to event_type if no mapping

    # Store status and duration for logging
    event_data["alert_status"] = status
    event_data["alert_duration_min"] = duration_min

    # Timed Alerts
    if status in ["Wrap", "Outgoing Wrap Up"] and duration_min > 2:
        return True
    elif status in ["Ready", "Ready Outbound"] and duration_min > 2 and is_in_shift:
        return True
    elif status == "Handle Time":
        return True
    elif status == "Lunch" and duration_min > 30:
        return True
    elif status == "Break" and duration_min > 15:
        return True
    elif status == "Comfort Break" and duration_min > 5:
        return True

    # Conditional Alert
    elif status == "Logged Out" and is_in_shift:
        return True

    # Immediate Alerts
    elif status in ["Device Busy", "Idle", "Away"]:
        return True

    # Conditional Approval Alerts
    elif status in ["Training", "In Meeting", "Paperwork"] and not is_scheduled(status, agent, timestamp):
        return True

    return False

def is_scheduled(event_type, agent, timestamp):
    # Placeholder for scheduled event logic
    # In a real implementation, this would check a schedule database or calendar
    return False

# ========== GOOGLE SHEETS HELPER ==========
def get_or_create_sheet_with_headers(service, spreadsheet_id, sheet_name, headers):
    try:
        spreadsheet = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        sheets = [s['properties']['title'] for s in spreadsheet['sheets']]
        if sheet_name not in sheets:
            # Create the sheet
            requests_body = [{'addSheet': {'properties': {'title': sheet_name}}}]
            service.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={'requests': requests_body}).execute()
            # Add headers
            body = {"values": [headers]}
            service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"'{sheet_name}'!A1",
                valueInputOption="RAW",
                body=body
            ).execute()
        return sheet_name
    except Exception as e:
        print(f"Error creating sheet {sheet_name} with headers: {e}")
        return sheet_name

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

# ========== VONAGE WEBHOOK ==========
@app.route("/vonage-events", methods=["POST"])
def vonage_events():
    print("Received request to /vonage-events")
    try:
        data = request.json
        print(f"Vonage event payload: {data}")

        # Extract top-level fields
        event_type = data.get("type", None)  # e.g., "channel.activityrecord.v0"
        if not event_type:
            print("ERROR: Missing event type in Vonage payload")
            return jsonify({"status": "error", "message": "Missing event type"}), 400

        timestamp_str = data.get("time", datetime.utcnow().isoformat())
        timestamp = parse(timestamp_str).replace(tzinfo=pytz.UTC)
        interaction_id = data.get("subject", data.get("data", {}).get("interaction", {}).get("interactionId", "-"))

        # Extract nested data
        event_data = data.get("data", {})

        # Extract agent name
        agent = None
        agent_id = None
        if event_type == "agent.presencechanged.v1":
            agent_id = event_data.get("user", {}).get("agentId", None)
            if agent_id:
                agent = agent_id_to_name.get(agent_id, None)
        elif "interaction" in event_data and "channels" in event_data["interaction"]:
            for channel in event_data["interaction"]["channels"]:
                if channel.get("party", {}).get("role") == "agent":
                    agent = channel["party"].get("address", None)
                    break
        elif "channel" in event_data and "party" in event_data["channel"]:
            agent = event_data["channel"]["party"].get("address", None)
            if not agent and event_data["channel"]["party"].get("role") == "agent":
                agent_id = event_data["channel"]["party"].get("agentId", None)
                if agent_id:
                    agent = agent_id_to_name.get(agent_id, None)
        elif "user" in event_data:  # Fallback for events like channel.alerted.v1, channel.connected.v1
            agent_id = event_data.get("user", {}).get("agentId", None)
            if agent_id:
                agent = agent_id_to_name.get(agent_id, None)

        if not agent:
            print(f"WARNING: Could not determine agent name from Vonage payload. Agent ID: {agent_id}")
            return jsonify({"status": "skipped", "message": "Agent name not found, event skipped"}), 200

        # Extract duration (convert from milliseconds to minutes)
        duration_ms = 0
        if "interaction" in event_data and "channels" in event_data["interaction"]:
            for channel in event_data["interaction"]["channels"]:
                if channel.get("party", {}).get("role") == "agent":
                    duration_ms = channel.get("duration", 0)
                    break
        elif "channel" in event_data:
            duration_ms = event_data.get("duration", 0)
        # For presencechanged and other events, duration might not be applicable
        duration_min = parse_duration(duration_ms)

        # Log disposition for activityrecord events
        if event_type == "channel.activityrecord.v0":
            disposition = event_data.get("interaction", {}).get("dispositionCode", "Not Specified")
            start_time = event_data.get("interaction", {}).get("startTime", "")
            initial_direction = event_data.get("interaction", {}).get("initialDirection", "Unknown")
            to_address = event_data.get("interaction", {}).get("toAddress", "")
            campaign = get_campaign_from_number(to_address)
            log_disposition(agent, disposition, timestamp, start_time, initial_direction, campaign, interaction_id)
            return jsonify({"status": "disposition logged"}), 200

        # Skip notifications for these event types
        if event_type in ["channel.ended.v1", "channel.disconnected.v1"]:
            print(f"Skipping notification for event type: {event_type}")
            return jsonify({"status": "skipped", "message": f"Notifications disabled for {event_type}"}), 200

        # Handle status alerts for other event types
        is_in_shift = is_within_shift(agent, timestamp)
        print(f"Event: {event_type}, Agent: {agent}, Duration: {duration_min} min, In Shift: {is_in_shift}")

        if should_trigger_alert(event_type, duration_min, is_in_shift, event_data):
            status = event_data.get("alert_status", event_type)
            duration_min = event_data.get("alert_duration_min", duration_min)
            emoji = get_emoji_for_event(status)
            team = agent_teams.get(agent, "Unknown Team")
            search_link = "https://nam.newvoicemedia.com/interaction-search"
            # List of states that do not have an interactionId
            states_without_interaction = ["Lunch", "Break", "Comfort Break", "Logged Out", "Training", "In Meeting", "Paperwork", "Idle", "Away", "Ready", "Ready Outbound"]
            # Conditional approval alerts for Training, In Meeting, Paperwork
            if status in ["Training", "In Meeting", "Paperwork"]:
                blocks = [
                    {"type": "section", "text": {"type": "mrkdwn", "text": f"{emoji} *{status} Alert*\nAgent: {agent}\nTeam: {team}\nDuration: {duration_min:.2f} min"}},
                    {"type": "actions", "elements": [
                        {"type": "button", "text": {"type": "plain_text", "text": "✅ Approved by Management"}, "value": f"approve|{agent}|{interaction_id}|{status}", "action_id": "approve_event"},
                        {"type": "button", "text": {"type": "plain_text", "text": "❌ Not Approved"}, "value": f"not_approve|{agent}|{interaction_id}|{status}", "action_id": "not_approve_event"}
                    ]}
                ]
            else:
                # Standard alerts
                buttons = [
                    {"type": "button", "text": {"type": "plain_text", "text": "✅ Assigned to Me"}, "value": f"assign|{agent}|{interaction_id}|{status}|{duration_min}", "action_id": "assign_to_me"}
                ]
                # Add a search button if the status has an interactionId
                if status not in states_without_interaction and interaction_id != "-":
                    buttons.append(
                        {"type": "button", "text": {"type": "plain_text", "text": "🔍 Interaction Search"}, "url": search_link, "action_id": "interaction_search"}
                    )
                    blocks = [
                        {"type": "section", "text": {"type": "mrkdwn", "text": f"{emoji} *{status} Alert*\nAgent: {agent}\nTeam: {team}\nDuration: {duration_min:.2f} min\nInteraction ID: `{interaction_id}`"}},
                        {"type": "actions", "elements": buttons}
                    ]
                else:
                    blocks = [
                        {"type": "section", "text": {"type": "mrkdwn", "text": f"{emoji} *{status} Alert*\nAgent: {agent}\nTeam: {team}\nDuration: {duration_min:.2f} min"}},
                        {"type": "actions", "elements": buttons}
                    ]
            post_slack_message(ALERT_CHANNEL_ID, blocks)
        return jsonify({"status": "posted"}), 200
    except Exception as e:
        print(f"Error processing Vonage event: {e}")
        return jsonify({"status": "error"}), 500

# ========== DISPOSITION LOGGING ==========
def log_disposition(agent, disposition, timestamp, start_time, initial_direction, campaign, interaction_id):
    """Log agent call dispositions to Google Sheets"""
    try:
        # Determine the year from the timestamp
        year = timestamp.year
        sheets_service_info = get_sheets_service(year)
        if not sheets_service_info:
            print(f"WARNING: Google Sheets service not available for year {year}, skipping disposition logging")
            return

        sheets_service, spreadsheet_id = sheets_service_info

        # Calculate the week range for the tab name
        sheet_name = get_week_range(timestamp)
        headers = ["Agent Name", "Disposition Code", "Count", "Start Time", "Initial Direction (Inbound/Outbound)", "Campaign Name", "Interaction ID"]
        get_or_create_sheet_with_headers(sheets_service, spreadsheet_id, sheet_name, headers)
        values = [[agent, disposition, 1, start_time, initial_direction, campaign, interaction_id]]
        sheets_service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=f"'{sheet_name}'!A1",
            valueInputOption="USER_ENTERED",
            body={"values": values}
        ).execute()
        print(f"Logged disposition for {agent}: {disposition} in sheet {sheet_name} for year {year}")
    except Exception as e:
        print(f"ERROR: Failed to log disposition: {e}")

def generate_disposition_summary(date):
    """Generate a summary of dispositions for a specific date"""
    try:
        # Determine the year from the date
        year = datetime.strptime(date, "%Y-%m-%d").year
        sheets_service_info = get_sheets_service(year)
        if not sheets_service_info:
            return f"Google Sheets service not available for year {year}"

        sheets_service, spreadsheet_id = sheets_service_info

        # Calculate the week range for the tab name
        sheet_name = get_week_range(datetime.strptime(date, "%Y-%m-%d"))
        try:
            result = sheets_service.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id,
                range=f"'{sheet_name}'!A1:G"
            ).execute()
        except Exception:
            return "No data found for this date."

        rows = result.get("values", [])
        if not rows or len(rows) <= 1:
            return "No disposition data found for this date."

        # Aggregate by Agent Name, Disposition Code, and Campaign
        count_map = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
        for row in rows[1:]:  # Skip header row
            if len(row) >= 7:
                agent, dispo, _, _, _, campaign, _ = row[:7]
                count_map[agent][dispo][campaign] += 1

        summary = ["*Disposition Summary:*\n"]
        for agent, dispos in count_map.items():
            summary.append(f"• {agent}:")
            for dispo, campaigns in dispos.items():
                for campaign, count in campaigns.items():
                    summary.append(f"   - {dispo}: {count} ({campaign})")
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
        # Determine the year from the date to get the correct spreadsheet_id
        year = datetime.strptime(date, "%Y-%m-%d").year
        sheets_service_info = get_sheets_service(year)
        if not sheets_service_info:
            return jsonify({"status": "error", "message": f"No spreadsheet ID defined for year {year}"}), 500
        _, spreadsheet_id = sheets_service_info
        export_link = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit#gid=0"
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

# ========== SLACK INTERACTIONS AND VIEW SUBMISSIONS ==========
@app.route("/slack/interactions", methods=["POST"])
def slack_interactions():
    print("Received request to /slack/interactions")
    payload = json.loads(request.form["payload"])
    print(f"Interactivity payload: {payload}")

    # Handle block actions (e.g., button clicks)
    if payload["type"] == "block_actions":
        action_id = payload["actions"][0]["action_id"]
        user = payload["user"]["username"]
        response_url = payload["response_url"]

        if action_id == "assign_to_me":
            value = payload["actions"][0]["value"]
            _, agent, campaign, status, duration_min = value.split("|")
            duration_min = float(duration_min)
            blocks = [
                {"type": "section", "text": {"type": "mrkdwn", "text": f"🔍 @{user} is investigating this {status} alert for {agent}."}},
                {"type": "actions", "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "📝 Follow-Up"}, "value": f"followup|{agent}|{campaign}|{status}|{duration_min}", "action_id": "open_followup"}
                ]}
            ]
            requests.post(response_url, json={"replace_original": True, "blocks": blocks})
            # Log to Team Lead Follow-Up Log
            year = datetime.utcnow().year
            sheets_service_info = get_sheets_service(year)
            if sheets_service_info:
                sheets_service, spreadsheet_id = sheets_service_info
                sheet_name = "FollowUps"
                headers = ["Timestamp (UTC)", "Agent Name", "Status", "Duration (min)", "Campaign", "Interaction ID", "Team", "Alert Acknowledged By", "Assigned To (Lead)", "Monitoring Method", "Follow-Up Action", "Reason for Issue", "Additional Notes", "Approval Decision", "Approved By"]
                get_or_create_sheet_with_headers(sheets_service, spreadsheet_id, sheet_name, headers)
                team = agent_teams.get(agent, "Unknown Team")
                body = {"values": [[datetime.utcnow().isoformat(), agent, status, duration_min, "", campaign, team, user, user, "", "", "", "", "", ""]]}
                sheets_service.spreadsheets().values().append(
                    spreadsheetId=spreadsheet_id,
                    range=f"'{sheet_name}'!A1",
                    valueInputOption="USER_ENTERED",
                    body=body
                ).execute()
                print(f"Logged alert acknowledgment for {agent}: {status}")
            else:
                print(f"WARNING: Could not log to Google Sheets for year {year}")

        elif action_id == "approve_event":
            value = payload["actions"][0]["value"]
            _, agent, campaign, event_type = value.split("|")
            blocks = [
                {"type": "section", "text": {"type": "mrkdwn", "text": f"✅ *{event_type} Approved*\nAgent: {agent}\nApproved by: @{user}\nInteraction ID: {campaign}"}}
            ]
            requests.post(response_url, json={"replace_original": True, "blocks": blocks})
            # Log approval to Google Sheets
            year = datetime.utcnow().year
            sheets_service_info = get_sheets_service(year)
            if sheets_service_info:
                sheets_service, spreadsheet_id = sheets_service_info
                sheet_name = "FollowUps"
                headers = ["Timestamp (UTC)", "Agent Name", "Status", "Duration (min)", "Campaign", "Interaction ID", "Team", "Alert Acknowledged By", "Assigned To (Lead)", "Monitoring Method", "Follow-Up Action", "Reason for Issue", "Additional Notes", "Approval Decision", "Approved By"]
                get_or_create_sheet_with_headers(sheets_service, spreadsheet_id, sheet_name, headers)
                team = agent_teams.get(agent, "Unknown Team")
                body = {"values": [[datetime.utcnow().isoformat(), agent, event_type, 0, "", campaign, team, "", user, "", "", "", "", "Approved", user]]}
                sheets_service.spreadsheets().values().append(
                    spreadsheetId=spreadsheet_id,
                    range=f"'{sheet_name}'!A1",
                    valueInputOption="USER_ENTERED",
                    body=body
                ).execute()
                print(f"Logged approval for {agent}: {event_type}")
            else:
                print(f"WARNING: Could not log to Google Sheets for year {year}")

        elif action_id == "not_approve_event":
            value = payload["actions"][0]["value"]
            _, agent, campaign, event_type = value.split("|")
            blocks = [
                {"type": "section", "text": {"type": "mrkdwn", "text": f"🔍 @{user} is investigating this {event_type} alert for {agent}."}},
                {"type": "actions", "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "📝 Follow-Up"}, "value": f"followup|{agent}|{campaign}|{event_type}|0", "action_id": "open_followup"}
                ]}
            ]
            requests.post(response_url, json={"replace_original": True, "blocks": blocks})
            # Log non-approval to Google Sheets
            year = datetime.utcnow().year
            sheets_service_info = get_sheets_service(year)
            if sheets_service_info:
                sheets_service, spreadsheet_id = sheets_service_info
                sheet_name = "FollowUps"
                headers = ["Timestamp (UTC)", "Agent Name", "Status", "Duration (min)", "Campaign", "Interaction ID", "Team", "Alert Acknowledged By", "Assigned To (Lead)", "Monitoring Method", "Follow-Up Action", "Reason for Issue", "Additional Notes", "Approval Decision", "Approved By"]
                get_or_create_sheet_with_headers(sheets_service, spreadsheet_id, sheet_name, headers)
                team = agent_teams.get(agent, "Unknown Team")
                body = {"values": [[datetime.utcnow().isoformat(), agent, event_type, 0, "", campaign, team, "", user, "", "", "", "", "Not Approved", user]]}
                sheets_service.spreadsheets().values().append(
                    spreadsheetId=spreadsheet_id,
                    range=f"'{sheet_name}'!A1",
                    valueInputOption="USER_ENTERED",
                    body=body
                ).execute()
                print(f"Logged non-approval for {agent}: {event_type}")
            else:
                print(f"WARNING: Could not log to Google Sheets for year {year}")

        elif action_id == "open_followup":
            value = payload["actions"][0]["value"]
            _, agent, campaign, status, duration_min = value.split("|")
            duration_min = float(duration_min)
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
                    "private_metadata": json.dumps({"agent": agent, "interaction_id": campaign, "status": status, "duration_min": duration_min})
                }
            }
            requests.post("https://slack.com/api/views.open", headers=headers, json=modal)

        return "", 200

    # Handle view submissions (e.g., modal submissions)
    elif payload["type"] == "view_submission":
        callback_id = payload["view"]["callback_id"]
        print(f"Processing view submission with callback_id: {callback_id}")

        if callback_id == "followup_submit":
            print("Handling followup_submit")
            values = payload["view"]["state"]["values"]
            metadata = json.loads(payload["view"]["private_metadata"])
            agent = metadata["agent"]
            interaction_id = metadata["interaction_id"]
            status = metadata["status"]
            duration_min = float(metadata["duration_min"])
            monitoring = values["monitoring"]["monitoring_method"]["selected_option"]["value"]
            action = values["action"]["action_taken"]["value"]
            reason = values["reason"]["reason_for_issue"]["value"]
            notes = values["notes"]["additional_notes"]["value"]
            user = payload["user"]["username"]

            # Log to Google Sheets
            year = datetime.utcnow().year
            sheets_service_info = get_sheets_service(year)
            if sheets_service_info:
                sheets_service, spreadsheet_id = sheets_service_info
                sheet_name = "FollowUps"
                headers = ["Timestamp (UTC)", "Agent Name", "Status", "Duration (min)", "Campaign", "Interaction ID", "Team", "Alert Acknowledged By", "Assigned To (Lead)", "Monitoring Method", "Follow-Up Action", "Reason for Issue", "Additional Notes", "Approval Decision", "Approved By"]
                get_or_create_sheet_with_headers(sheets_service, spreadsheet_id, sheet_name, headers)
                team = agent_teams.get(agent, "Unknown Team")
                body = {"values": [[datetime.utcnow().isoformat(), agent, status, duration_min, "", interaction_id, team, user, user, monitoring, action, reason, notes, "", ""]]}
                sheets_service.spreadsheets().values().append(
                    spreadsheetId=spreadsheet_id,
                    range=f"'{sheet_name}'!A1",
                    valueInputOption="USER_ENTERED",
                    body=body
                ).execute()
                print(f"Logged follow-up to Google Sheet: {sheet_name}")
            else:
                print(f"WARNING: Could not log to Google Sheets for year {year}")

        elif callback_id == "weekly_update_modal":
            print("Handling weekly_update_modal")
            values = payload["view"]["state"]["values"]
            start_date = values["start_date"]["start_date_picker"]["selected_date"]
            end_date = values["end_date"]["end_date_picker"]["selected_date"]
            top_performers = [option["value"] for option in values["top_performers"]["top_performers_select"]["selected_options"]]
            top_support = values["top_support"]["top_support_input"]["value"]
            bottom_performers = [option["value"] for option in values["bottom_performers"]["bottom_performers_select"]["selected_options"]]
            bottom_actions = values["bottom_actions"]["bottom_actions_input"]["value"]
            improvement_plan = values["improvement_plan"]["improvement_plan_input"]["value"]
            team_momentum = values["team_momentum"]["team_momentum_input"]["value"]
            trends = values["trends"]["trends_input"]["value"]
            additional_notes = values["additional_notes"]["notes_input"]["value"] if "additional_notes" in values else ""

            user = payload["user"]["username"]
            week = f"{start_date} to {end_date}"

            # Log to Google Sheets using the year from start_date
            year = datetime.strptime(start_date, "%Y-%m-%d").year
            sheets_service_info = get_sheets_service(year)
            if sheets_service_info:
                sheets_service, spreadsheet_id = sheets_service_info
                sheet_name = f"Weekly {week}"
                # Ensure the sheet exists and has headers
                get_or_create_sheet_with_headers(sheets_service, spreadsheet_id, sheet_name, [
                    "Timestamp (UTC)", "Submitted By", "Start Date", "End Date", "Top Performers", "Support Actions",
                    "Bottom Performers", "Action Plans", "Improvement Plan", "Team Momentum", "Trends", "Additional Notes"
                ])
                body = {
                    "values": [[
                        datetime.utcnow().isoformat(), user, start_date, end_date, ", ".join(top_performers), top_support,
                        ", ".join(bottom_performers), bottom_actions, improvement_plan, team_momentum, trends, additional_notes
                    ]]
                }
                sheets_service.spreadsheets().values().append(
                    spreadsheetId=spreadsheet_id,
                    range=f"'{sheet_name}'!A2",  # Start at A2 to leave room for headers
                    valueInputOption="USER_ENTERED",
                    body=body
                ).execute()
                print(f"Logged weekly update to Google Sheet: {sheet_name} for year {year}")
            else:
                print(f"WARNING: Could not log to Google Sheets for year {year}")

            # Post a confirmation message to the channel
            metadata = json.loads(payload["view"]["private_metadata"])
            channel_id = metadata["channel_id"]
            blocks = [
                {"type": "section", "text": {"type": "mrkdwn", "text": f"✅ Weekly update for *{week}* submitted successfully by {user}!"}}
            ]
            post_slack_message(channel_id, blocks)

        return jsonify({"response_action": "clear"}), 200

    return "", 200

# ========== SLACK COMMANDS ==========
@app.route("/slack/commands/daily_report", methods=["GET", "POST"])
def slack_command_daily_report():
    print(f"Received {request.method} request to /slack/commands/daily_report")
    if request.method == "GET":
        print("Slack verification request received")
        return "This endpoint is for Slack slash commands. Please use POST to send a command.", 200

    print(f"Slash command payload: {request.form}")
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
    # Determine the year from the date to get the correct spreadsheet_id
    year = datetime.strptime(date_for_dispositions, "%Y-%m-%d").year
    sheets_service_info = get_sheets_service(year)
    if not sheets_service_info:
        return jsonify({"status": "error", "message": f"No spreadsheet ID defined for year {year}"}), 500
    _, spreadsheet_id = sheets_service_info
    export_link = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit#gid=0"

    top_performer = "Jeanette Bantz"  # Removed @ symbol
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📊 Daily Agent Report – {report_date}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "🚨 *Missed Targets:*\n• Crystalbell Miranda – Wrap ❗\n• Rebecca Stokes – Call Time ❗\n• Carleisha Smith – Ready ❗ Not Ready ❗"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "✅ *Met All Targets:*\n• Jessica Lopez\n• Jason McLaughlin"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"🏅 *Top Performer:* {top_performer} – 0 alerts 🎯"}]},
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

    try:
        print(f"Slash command payload: {request.form}")
        trigger_id = request.form.get("trigger_id")
        channel_id = request.form.get("channel_id")
        
        if not trigger_id:
            print("ERROR: Missing trigger_id in request.form")
            return "Missing trigger_id", 400
        if not channel_id:
            print("ERROR: Missing channel_id in request.form")
            return "Missing channel_id", 400

        print(f"SLACK_BOT_TOKEN: {'Set' if SLACK_BOT_TOKEN else 'Not Set'}")
        print(f"Headers: {headers}")

        # Open the weekly update modal
        modal = {
            "trigger_id": trigger_id,
            "view": {
                "type": "modal",
                "callback_id": "weekly_update_modal",
                "title": {
                    "type": "plain_text",
                    "text": "Team Progress & Performance Log"
                },
                "submit": {
                    "type": "plain_text",
                    "text": "Submit"
                },
                "close": {
                    "type": "plain_text",
                    "text": "Close"
                },
                "private_metadata": json.dumps({"channel_id": channel_id}),
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "*Let’s capture this week’s wins, challenges, and team progress below. 👇*"
                        }
                    },
                    {
                        "type": "input",
                        "block_id": "start_date",
                        "element": {
                            "type": "datepicker",
                            "action_id": "start_date_picker",
                            "placeholder": {
                                "type": "plain_text",
                                "text": "Select start date"
                            }
                        },
                        "label": {
                            "type": "plain_text",
                            "text": "Start of Week"
                        }
                    },
                    {
                        "type": "input",
                        "block_id": "end_date",
                        "element": {
                            "type": "datepicker",
                            "action_id": "end_date_picker",
                            "placeholder": {
                                "type": "plain_text",
                                "text": "Select end date"
                            }
                        },
                        "label": {
                            "type": "plain_text",
                            "text": "End of Week"
                        }
                    },
                    {
                        "type": "input",
                        "block_id": "top_performers",
                        "element": {
                            "type": "multi_static_select",
                            "action_id": "top_performers_select",
                            "placeholder": {
                                "type": "plain_text",
                                "text": "Select top performers"
                            },
                            "options": employee_options
                        },
                        "label": {
                            "type": "plain_text",
                            "text": "Top Performers"
                        }
                    },
                    {
                        "type": "input",
                        "block_id": "top_support",
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "top_support_input",
                            "placeholder": {
                                "type": "plain_text",
                                "text": "How are you supporting top performers?"
                            }
                        },
                        "label": {
                            "type": "plain_text",
                            "text": "Support Actions for Top Performers"
                        }
                    },
                    {
                        "type": "input",
                        "block_id": "bottom_performers",
                        "element": {
                            "type": "multi_static_select",
                            "action_id": "bottom_performers_select",
                            "placeholder": {
                                "type": "plain_text",
                                "text": "Select bottom performers"
                            },
                            "options": employee_options
                        },
                        "label": {
                            "type": "plain_text",
                            "text": "Bottom Performers"
                        }
                    },
                    {
                        "type": "input",
                        "block_id": "bottom_actions",
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "bottom_actions_input",
                            "placeholder": {
                                "type": "plain_text",
                                "text": "Describe coaching, follow-up, etc."
                            }
                        },
                        "label": {
                            "type": "plain_text",
                            "text": "Support Actions for Bottom Performers"
                        }
                    },
                    {
                        "type": "input",
                        "block_id": "improvement_plan",
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "improvement_plan_input",
                            "placeholder": {
                                "type": "plain_text",
                                "text": "Are they improving? What's the plan?"
                            }
                        },
                        "label": {
                            "type": "plain_text",
                            "text": "Improvement Plan"
                        }
                    },
                    {
                        "type": "input",
                        "block_id": "team_momentum",
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "team_momentum_input",
                            "multiline": True,
                            "placeholder": {
                                "type": "plain_text",
                                "text": "Are you rising together or are there support gaps?"
                            }
                        },
                        "label": {
                            "type": "plain_text",
                            "text": "Team Momentum"
                        }
                    },
                    {
                        "type": "input",
                        "block_id": "trends",
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "trends_input",
                            "multiline": True,
                            "placeholder": {
                                "type": "plain_text",
                                "text": "Any recurring behaviors, client feedback, or performance shifts?"
                            }
                        },
                        "label": {
                            "type": "plain_text",
                            "text": "Trends"
                        }
                    },
                    {
                        "type": "input",
                        "block_id": "additional_notes",
                        "optional": True,
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "notes_input",
                            "multiline": True,
                            "placeholder": {
                                "type": "plain_text",
                                "text": "Shoutouts, observations, anything else to share?"
                            }
                        },
                        "label": {
                            "type": "plain_text",
                            "text": "Additional Notes (Optional)"
                        }
                    }
                ]
            }
        }
        print("Opening modal for weekly update form")
        response = requests.post("https://slack.com/api/views.open", headers=headers, json=modal)
        print(f"Slack API response status: {response.status_code}")
        print(f"Slack API response: {response.text}")
        if response.status_code != 200 or not response.json().get("ok"):
            print(f"ERROR: Failed to open modal: {response.text}")
        else:
            print("Modal request sent to Slack successfully")
        return "", 200
    except Exception as e:
        print(f"ERROR in /slack/commands/weekly_update_form: {e}")
        return "Internal server error", 500

# ========== DAILY REPORT SCHEDULER ==========
def trigger_daily_report():
    print("Triggering daily report")
    # Get yesterday's date for the report
    yesterday = datetime.utcnow() - timedelta(days=1)
    report_date = yesterday.strftime("%b %d")
    date_for_dispositions = yesterday.strftime("%Y-%m-%d")

    # Get disposition summary
    disposition_summary = generate_disposition_summary(date_for_dispositions)
    # Determine the year from the date to get the correct spreadsheet_id
    year = datetime.strptime(date_for_dispositions, "%Y-%m-%d").year
    sheets_service_info = get_sheets_service(year)
    if not sheets_service_info:
        print(f"WARNING: Could not generate report - no spreadsheet ID defined for year {year}")
        return
    _, spreadsheet_id = sheets_service_info
    export_link = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit#gid=0"

    # Agent performance data
    top_performer = "Jeanette Bantz"  # Removed @ symbol

    # Create the report blocks
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📊 Daily Agent Report – {report_date}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "🚨 *Missed Targets:*\n• Crystalbell Miranda – Wrap ❗\n• Rebecca Stokes – Call Time ❗\n• Carleisha Smith – Ready ❗ Not Ready ❗"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "✅ *Met All Targets:*\n• Jessica Lopez\n• Jason McLaughlin"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"🏅 *Top Performer:* {top_performer} – 0 alerts 🎯"}]},
        {"type": "section", "text": {"type": "mrkdwn", "text": disposition_summary}},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"📎 *Full Disposition Report:* <{export_link}|View in Google Sheets>"}
        ]},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "👁️ Acknowledge"}, "value": "ack_report"}
        ]}
    ]
    post_slack_message(ALERT_CHANNEL_ID, blocks)

# ========== WEEKLY REPORT WITH VONAGE METRICS ==========
def generate_weekly_report():
    print("Generating weekly report")
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

    post_slack_message(ALERT_CHANNEL_ID, blocks)

# Set up the scheduler
scheduler = BackgroundScheduler(timezone="US/Eastern")
# Add the daily report job to run at 7:00 AM Eastern Time
scheduler.add_job(trigger_daily_report, 'cron', hour=7, minute=0)
scheduler.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))