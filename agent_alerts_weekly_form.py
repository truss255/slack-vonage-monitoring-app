from flask import Flask, request, jsonify
import os
import json
import requests
from datetime import datetime, timedelta
from dateutil.parser import parse
from google.oauth2 import service_account
from googleapiclient.discovery import build
import pytz
import logging
import time
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

app = Flask(__name__)

# ========== LOGGING SETUP ==========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("app.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ========== ENV VARS VALIDATION ==========
required_env_vars = [
    "SLACK_BOT_TOKEN", "ALERT_CHANNEL_ID", "WEEKLY_UPDATE_SHEET_ID",
    "FOLLOWUP_SHEET_ID", "GOOGLE_SERVICE_ACCOUNT_JSON"
]
missing_vars = [var for var in required_env_vars if not os.environ.get(var)]
if missing_vars:
    error_msg = f"Missing required environment variables: {', '.join(missing_vars)}"
    logger.error(error_msg)
    raise ValueError(error_msg)

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
ALERT_CHANNEL_ID = os.environ["ALERT_CHANNEL_ID"]
BOT_ERROR_CHANNEL_ID = os.environ.get("BOT_ERROR_CHANNEL_ID", ALERT_CHANNEL_ID)
SCOPES = json.loads(os.environ.get("GOOGLE_SHEETS_SCOPES", '["https://www.googleapis.com/auth/spreadsheets"]'))

# Map years to spreadsheet IDs for weekly updates
WEEKLY_UPDATE_SPREADSHEET_IDS = {
    2025: os.environ["WEEKLY_UPDATE_SHEET_ID"],
    2026: "1dlmzbFj5iC92oeDhrFzuJ-_eb_6sjXWMMZ6JNJ6EwoY"
}

# Map years to spreadsheet IDs for follow-ups
FOLLOWUP_SPREADSHEET_IDS = {
    2025: os.environ["FOLLOWUP_SHEET_ID"],
    2026: "1dlmzbFj5iC92oeDhrFzuJ-_eb_6sjXWMMZ6JNJ6EwoY"
}

# Handle Google service account JSON
GOOGLE_SERVICE_ACCOUNT_JSON = None
try:
    raw_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    GOOGLE_SERVICE_ACCOUNT_JSON = json.loads(raw_json)
    logger.info("Successfully parsed GOOGLE_SERVICE_ACCOUNT_JSON")
except json.JSONDecodeError as e:
    logger.error(f"Error parsing GOOGLE_SERVICE_ACCOUNT_JSON: {e}")
    raw_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    if raw_json.startswith("'") and raw_json.endswith("'"):
        raw_json = raw_json[1:-1]
    fixed_json = raw_json.replace("'", '"').replace("\\n", "\n")
    try:
        GOOGLE_SERVICE_ACCOUNT_JSON = json.loads(fixed_json)
        logger.info("Parsed JSON after fixes")
    except json.JSONDecodeError:
        logger.error("Could not parse JSON, using empty object")
        GOOGLE_SERVICE_ACCOUNT_JSON = {}

if not GOOGLE_SERVICE_ACCOUNT_JSON or not isinstance(GOOGLE_SERVICE_ACCOUNT_JSON, dict):
    logger.warning("Invalid Google service account credentials")
    GOOGLE_SERVICE_ACCOUNT_JSON = {}

# Dictionary to store sheets_service instances for each year and type
sheets_services = {
    "weekly_update": {},
    "followup": {}
}

# Dictionary to store the current Presence State and timestamp for each agent
agent_presence_states = {}

# Dictionary to track the last alert sent for each agent and state (for deduplication)
last_alerts = {}

# Dictionary to track the timestamp when each agent entered a specific state
agent_state_timestamps = {}

ET = pytz.timezone('America/New_York')
logger.info(f"Initialized agent_presence_states as empty dictionary at startup: {datetime.utcnow().replace(tzinfo=pytz.UTC).astimezone(ET).isoformat()}")

def get_sheets_service(year, sheet_type="followup"):
    """Get or create a sheets_service instance for the given year and sheet type."""
    if sheet_type not in sheets_services:
        logger.error(f"Invalid sheet type {sheet_type}")
        return None

    if year not in sheets_services[sheet_type]:
        if sheet_type == "weekly_update":
            spreadsheet_id = WEEKLY_UPDATE_SPREADSHEET_IDS.get(year)
        elif sheet_type == "followup":
            spreadsheet_id = FOLLOWUP_SPREADSHEET_IDS.get(year)
        else:
            logger.error(f"Unknown sheet type {sheet_type}")
            return None

        if not spreadsheet_id:
            logger.error(f"No spreadsheet ID defined for year {year} and type {sheet_type}")
            return None

        try:
            creds = service_account.Credentials.from_service_account_info(GOOGLE_SERVICE_ACCOUNT_JSON, scopes=SCOPES)
            sheets_service = build("sheets", "v4", credentials=creds)
            sheets_services[sheet_type][year] = (sheets_service, spreadsheet_id)
            logger.info(f"Initialized Google Sheets service for year {year} and type {sheet_type}")
        except Exception as e:
            logger.error(f"Failed to initialize Google Sheets for year {year} and type {sheet_type}: {e}")
            return None

    return sheets_services[sheet_type][year]

# ========== SLACK ==========
headers = {
    "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
    "Content-Type": "application/json"
}

# Setup retry strategy for Slack API calls
session = requests.Session()
retries = Retry(total=5, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
session.mount('https://', HTTPAdapter(max_retries=retries))

def post_slack_message(channel, blocks, thread_ts=None, retry_count=5):
    """Post a message to Slack with retry logic for rate-limiting."""
    current_time_et = datetime.utcnow().replace(tzinfo=pytz.UTC).astimezone(ET)
    logger.info(f"Attempting to post to Slack channel: {channel} at {current_time_et.isoformat()}")
    payload = {"channel": channel, "blocks": blocks}
    if thread_ts:
        payload["thread_ts"] = thread_ts

    for attempt in range(retry_count):
        try:
            response = session.post("https://slack.com/api/chat.postMessage", headers=headers, json=payload)
            logger.info(f"Slack API response status: {response.status_code}")
            logger.info(f"Slack API response: {response.text}")
            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", 1))
                logger.warning(f"Rate-limited by Slack, retrying after {retry_after} seconds")
                time.sleep(retry_after)
                continue
            if response.status_code != 200:
                logger.error(f"Failed to post to Slack: {response.status_code} - {response.text}")
                error_blocks = [
                    {"type": "section", "text": {"type": "mrkdwn", "text": f"⚠️ Failed to post to Slack channel {channel}: {response.status_code} - {response.text}"}}
                ]
                session.post("https://slack.com/api/chat.postMessage", headers=headers, json={"channel": BOT_ERROR_CHANNEL_ID, "blocks": error_blocks})
                return None
            logger.info("Successfully posted to Slack")
            return response.json().get("ts")
        except Exception as e:
            logger.error(f"ERROR: Failed to post to Slack: {e}")
            if attempt < retry_count - 1:
                time.sleep(2 ** attempt)
                continue
            error_blocks = [
                {"type": "section", "text": {"type": "mrkdwn", "text": f"⚠️ Failed to post to Slack channel {channel}: {str(e)}"}}
            ]
            session.post("https://slack.com/api/chat.postMessage", headers=headers, json={"channel": BOT_ERROR_CHANNEL_ID, "blocks": error_blocks})
            return None

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
    {"text": {"type": "plain_text", "text": "Rebecca Stokes"}, "value": "rebecca_stokes"},
    {"text": {"type": "plain_text", "text": "Tanya Russell"}, "value": "tanya_russell"}
]

# Agent teams - Jessica Lopez moved to Team Adriana
agent_teams = {
    "Carla Hagerman": "Team Adriana 💎",
    "Dajah Blackwell": "Team Adriana 💎",
    "Felicia Martin": "Team Adriana 💎",
    "Felicia Randall": "Team Adriana 💎",
    "Jeanette Bantz": "Team Adriana 💎",
    "Jesse Lorenzana Escarfullery": "Team Adriana 💎",
    "Jessica Lopez": "Team Adriana 💎",
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

# Updated Campaign mapping dictionary (used as fallback)
CAMPAIGN_MAPPING = {
    "+13234547738": "SETC Incoming Calls",
    "+16822725314": "SETC Incoming Calls",
    "+413122787476": "Maui Wildfire",
    "+313122192786": "Camp Lejeune",
    "+213122195489": "Depo-Provera",
    "+312132055684": "LA Wildfire",
    "+13122787476": "Maui Wildfire Incoming Calls",
    "+13128008559": "Maui Wildfire Incoming Calls",
    "+16462168075": "Maui Wildfire Incoming Calls",
    "+16463304966": "Maui Wildfire Incoming Calls",
    "+12132135505": "Panish LA Fire Calls",
    "+13128008564": "Panish LA Fire Calls",
    "+12132055684": "LA Fire Incoming Calls",
    "+13128008583": "LA Fire Incoming Calls",
    "+13132179387": "LA Fire Incoming Calls"
}

def get_campaign_from_number(phone_number):
    """Map a Vonage phone number to a campaign name (fallback method)."""
    if not phone_number:
        return "Unknown Campaign"
    normalized_number = phone_number.lstrip("+")
    for key, campaign in CAMPAIGN_MAPPING.items():
        if normalized_number == key.lstrip("+") or phone_number == key:
            return campaign
    return "Unknown Campaign"

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

# ========== TIMEZONE HANDLING ==========
def is_within_shift(agent, timestamp):
    try:
        agent_data = agent_shifts.get(agent)
        if not agent_data:
            logger.warning(f"Agent {agent} not found in shift data")
            return False
        try:
            tz = pytz.timezone(agent_data["timezone"])
        except pytz.exceptions.UnknownTimeZoneError as e:
            logger.error(f"Invalid timezone for agent {agent}: {agent_data['timezone']}. Error: {e}")
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
    except Exception as e:
        logger.error(f"ERROR in is_within_shift for agent {agent}: {e}")
        return False

# ========== DURATION PARSING ==========
def parse_duration(duration_ms):
    try:
        duration_min = duration_ms / 1000 / 60  # Convert ms to minutes
        return duration_min
    except (ValueError, TypeError) as e:
        logger.error(f"ERROR in parse_duration: {e}")
        return 0

# Calculate duration since the agent entered the current state
def get_event_duration(agent, current_state, current_timestamp):
    try:
        state_key = f"{agent}:{current_state}"
        if state_key not in agent_state_timestamps:
            logger.info(f"Agent {agent} has no recorded timestamp for state {current_state}")
            return 0

        start_timestamp = agent_state_timestamps[state_key]
        duration_ms = (current_timestamp - start_timestamp).total_seconds() * 1000
        logger.info(f"Calculated duration for {agent} in state {current_state}: {duration_ms} ms")
        return duration_ms
    except Exception as e:
        logger.error(f"ERROR in get_event_duration for agent {agent}: {e}")
        return 0

# ========== WEEKLY TAB HELPER ==========
def get_weekly_tab_name(timestamp):
    """Determine the weekly tab name (e.g., 'Weekly Apr 1 - Apr 7') based on the timestamp."""
    try:
        date = timestamp.date()
        days_to_monday = (date.weekday() - 0) % 7
        monday = date - timedelta(days=days_to_monday)
        sunday = monday + timedelta(days=6)
        tab_name = f"Weekly {monday.strftime('%b %-d')} - {sunday.strftime('%b %-d')}"
        return tab_name
    except Exception as e:
        logger.error(f"ERROR in get_weekly_tab_name: {e}")
        return "Weekly Unknown"

# ========== GOOGLE SHEETS HELPER ==========
def get_or_create_sheet_with_headers(service, spreadsheet_id, sheet_name, headers):
    try:
        spreadsheet = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        sheets = [s['properties']['title'] for s in spreadsheet['sheets']]
        if sheet_name not in sheets:
            requests_body = [{'addSheet': {'properties': {'title': sheet_name}}}]
            service.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={'requests': requests_body}).execute()
            body = {"values": [headers]}
            service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"'{sheet_name}'!A1",
                valueInputOption="RAW",
                body=body
            ).execute()
        return sheet_name
    except Exception as e:
        logger.error(f"Error creating sheet {sheet_name} with headers: {e}")
        return sheet_name

# ========== LOGGING TO WEEKLY TAB IN FOLLOWUPS SPREADSHEET ==========
def log_to_followups(agent, timestamp, duration_min, interaction_id, agent_state, campaign, user=None, monitoring=None, action=None, reason=None, notes=None, approval_decision=None, approved_by=None, status="Open"):
    try:
        year = timestamp.year
        sheets_service_info = get_sheets_service(year, sheet_type="followup")
        if not sheets_service_info:
            logger.warning(f"Google Sheets service not available for year {year} (followup), skipping logging")
            return

        sheets_service, spreadsheet_id = sheets_service_info
        sheet_name = get_weekly_tab_name(timestamp)
        headers = [
            "Timestamp", "Agent Name", "Agent State", "Duration (min)", "Interaction ID",
            "Campaign", "Team", "Assigned To (Lead)", "Monitoring Method",
            "Follow-Up Action", "Reason for Issue", "Additional Notes", "Approval Decision", "Approved By (Slack)", "Status"
        ]
        get_or_create_sheet_with_headers(sheets_service, spreadsheet_id, sheet_name, headers)
        team = agent_teams.get(agent, "Unknown Team")

        timestamp_et = timestamp.astimezone(ET)
        formatted_timestamp = timestamp_et.strftime("%Y-%m-%d %I:%M:%S %p")

        states_with_interaction = ["Busy", "Wrap", "Outgoing Wrap Up"]
        interaction_id_value = interaction_id if agent_state in states_with_interaction else "Not Applicable for This State"
        campaign_value = campaign if agent_state in states_with_interaction else "Not Applicable for This State"

        values = [[
            formatted_timestamp, agent, agent_state, duration_min, interaction_id_value,
            campaign_value, team, user if user else "", monitoring if monitoring else "",
            action if action else "", reason if reason else "", notes if notes else "",
            approval_decision if approval_decision else "", approved_by if approved_by else "", status
        ]]
        sheets_service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=f"'{sheet_name}'!A1",
            valueInputOption="USER_ENTERED",
            body={"values": values}
        ).execute()
        logger.info(f"Logged to {sheet_name} tab for {agent} at {formatted_timestamp} with status {status}")
    except Exception as e:
        logger.error(f"Failed to log to {sheet_name} tab: {e}")
        error_blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"⚠️ Failed to log to Google Sheets (sheet: {sheet_name}): {str(e)}"}}
        ]
        session.post("https://slack.com/api/chat.postMessage", headers=headers, json={"channel": BOT_ERROR_CHANNEL_ID, "blocks": error_blocks})

# ========== AGENT STATE RULES ==========
def should_trigger_alert(agent_state, duration_min, is_in_shift, event_data=None):
    try:
        event_data["alert_agent_state"] = agent_state
        event_data["alert_duration_min"] = duration_min

        if agent_state in ["Wrap", "Outgoing Wrap Up"] and duration_min > 2:
            return True
        elif agent_state in ["Ready", "Ready Outbound", "Idle", "Idle (Outbound)"] and duration_min > 2 and is_in_shift:
            return True
        elif agent_state == "Busy" and duration_min > 8:
            return True
        elif agent_state == "Lunch" and duration_min > 30:
            return True
        elif agent_state == "Break" and duration_min > 15:
            return True
        elif agent_state == "Comfort Break" and duration_min > 5:
            return True
        elif agent_state == "Logged Out" and is_in_shift:
            return True
        elif agent_state in ["Device Busy", "Device Unreachable", "Fault", "In Meeting", "Paperwork", "Team Meeting", "Training"]:
            return True

        return False
    except Exception as e:
        logger.error(f"ERROR in should_trigger_alert: {e}")
        return False

def get_emoji_for_event(agent_state):
    emoji_map = {
        "Wrap": "📝",
        "Outgoing Wrap Up": "📝",
        "Ready": "📞",
        "Ready Outbound": "📤",
        "Idle": "❗",
        "Idle (Outbound)": "❗",
        "Busy": "💻",
        "Lunch": "🍽️",
        "Break": "☕",
        "Comfort Break": "🚻",
        "Logged Out": "🔌",
        "Device Busy": "💻",
        "Device Unreachable": "🔌",
        "Fault": "⚠️",
        "In Meeting": "👥",
        "Paperwork": "🗂️",
        "Team Meeting": "👥",
        "Training": "📚"
    }
    return emoji_map.get(agent_state, "⚠️")

# ========== HEALTH CHECK ENDPOINT ==========
@app.route("/health", methods=["GET"])
def health_check():
    """Health check endpoint to verify the application's status."""
    health_status = {"status": "healthy", "checks": {}}

    try:
        response = session.post("https://slack.com/api/auth.test", headers=headers)
        if response.status_code == 200 and response.json().get("ok"):
            health_status["checks"]["slack"] = "healthy"
        else:
            health_status["checks"]["slack"] = f"unhealthy: {response.text}"
            health_status["status"] = "unhealthy"
    except Exception as e:
        health_status["checks"]["slack"] = f"unhealthy: {str(e)}"
        health_status["status"] = "unhealthy"

    try:
        year = datetime.utcnow().year
        sheets_service_info = get_sheets_service(year, sheet_type="followup")
        if sheets_service_info:
            health_status["checks"]["google_sheets"] = "healthy"
        else:
            health_status["checks"]["google_sheets"] = "unhealthy: Failed to initialize service"
            health_status["status"] = "unhealthy"
    except Exception as e:
        health_status["checks"]["google_sheets"] = f"unhealthy: {str(e)}"
        health_status["status"] = "unhealthy"

    return jsonify(health_status), 200 if health_status["status"] == "healthy" else 500

# ========== VONAGE WEBHOOK FOR REAL-TIME ALERTS ==========
@app.route("/vonage-events", methods=["POST"])
def vonage_events():
    current_time_et = datetime.utcnow().replace(tzinfo=pytz.UTC).astimezone(ET)
    logger.info(f"Received request to /vonage-events at {current_time_et.isoformat()}")
    try:
        data = request.json
        if not data:
            logger.error("No JSON data in request")
            return jsonify({"status": "error", "message": "No JSON data in request"}), 400

        logger.debug(f"Vonage event payload: {json.dumps(data, indent=2, default=str)}")

        event_type = data.get("type", None)
        if not event_type:
            logger.error("Missing event type in Vonage payload")
            return jsonify({"status": "error", "message": "Missing event type"}), 400

        if event_type in ["channel.alerted.v1", "channel.connected.v1"]:
            logger.info(f"Skipping event type {event_type} as per requirements")
            return jsonify({"status": "skipped", "message": f"Event type {event_type} not processed"}), 200

        timestamp_str = data.get("time", datetime.utcnow().isoformat())
        try:
            timestamp = parse(timestamp_str).replace(tzinfo=pytz.UTC)
        except Exception as e:
            logger.error(f"Failed to parse timestamp {timestamp_str}: {e}")
            return jsonify({"status": "error", "message": "Invalid timestamp"}), 400

        interaction_id = data.get("subject", "-") if event_type != "agent.presencechanged.v1" else "-"

        event_data = data.get("data", {})
        event_data["timestamp"] = timestamp

        # Extract agent name directly
        agent = None
        if event_type == "agent.presencechanged.v1":
            user_data = event_data.get("user", {})
            agent = user_data.get("name", None) or user_data.get("displayName", None) or user_data.get("agentName", None)
        elif "interaction" in event_data:
            if "channel" in event_data["interaction"]:
                channel = event_data["interaction"]["channel"]
                agent = channel.get("agentName", None) or channel.get("name", None)
            elif "channels" in event_data["interaction"]:
                for channel in event_data["interaction"]["channels"]:
                    agent = channel.get("agentName", None) or channel.get("name", None)
                    if agent:
                        break
        elif "channel" in event_data and "party" in event_data["channel"]:
            agent = event_data["channel"].get("agentName", None) or event_data["channel"].get("name", None)
        elif "user" in event_data:
            agent = event_data["user"].get("name", None) or event_data["user"].get("displayName", None) or event_data["user"].get("agentName", None)

        if not agent:
            logger.warning(f"Could not determine agent name from Vonage payload. Full payload: {json.dumps(data, indent=2, default=str)}")
            return jsonify({"status": "skipped", "message": "Agent name not found, event skipped"}), 200

        # Validate agent name against known agents
        if agent not in agent_shifts:
            logger.warning(f"Agent name '{agent}' not recognized in agent_shifts. Full payload: {json.dumps(data, indent=2, default=str)}")
            return jsonify({"status": "skipped", "message": "Unrecognized agent name"}), 200

        event_data["agent"] = agent

        # Extract campaign from groups or skills (preferred), fall back to phone number
        campaign = "Unknown Campaign"
        if "interaction" in event_data:
            interaction = event_data["interaction"]
            
            # Try to get the campaign from 'groups' first
            groups = interaction.get("groups", [])
            if groups and isinstance(groups, list) and len(groups) > 0:
                campaign = groups[0]  # Take the first group as the campaign name
                logger.info(f"Campaign extracted from groups: {campaign}")
            else:
                # If no groups, try 'skills'
                skills = interaction.get("skills", [])
                if skills and isinstance(skills, list) and len(skills) > 0:
                    campaign = skills[0]  # Take the first skill as the campaign name
                    logger.info(f"Campaign extracted from skills: {campaign}")
                else:
                    # Fall back to phone number mapping if neither groups nor skills are available
                    campaign_phone = interaction.get("fromAddress", None) or interaction.get("toAddress", None)
                    campaign = get_campaign_from_number(campaign_phone)
                    logger.info(f"Campaign extracted from phone number: {campaign}")
        else:
            logger.warning("No interaction data found in event payload, defaulting campaign to 'Unknown Campaign'")

        # Determine agent state
        agent_state = None
        if event_type == "agent.presencechanged.v1":
            presence_type = event_data.get("presence", {}).get("category", {}).get("type", "").lower()
            subcategory = event_data.get("presence", {}).get("category", {}).get("subcategory", "").lower()
            description = event_data.get("presence", {}).get("description", "").lower()
            if presence_type == "ready":
                if "outbound" in subcategory or "outbound" in description or "ready_outbound" in subcategory or "ready_outbound" in description:
                    agent_state = "Ready Outbound"
                else:
                    agent_state = "Ready"
            elif presence_type == "lunch":
                agent_state = "Lunch"
            elif presence_type == "break":
                agent_state = "Break"
            elif presence_type == "comfort_break":
                agent_state = "Comfort Break"
            elif presence_type == "logged_out":
                agent_state = "Logged Out"
            elif presence_type == "training":
                agent_state = "Training"
            elif presence_type == "meeting":
                agent_state = "In Meeting"
            elif presence_type == "paperwork":
                agent_state = "Paperwork"
            elif presence_type == "idle":
                if "outbound" in subcategory or "outbound" in description:
                    agent_state = "Idle (Outbound)"
                else:
                    agent_state = "Idle"
            elif presence_type == "team_meeting":
                agent_state = "Team Meeting"

            # Update the agent's presence state in memory
            if agent_state:
                agent_presence_states[agent] = (agent_state, timestamp)
                state_key = f"{agent}:{agent_state}"
                agent_state_timestamps[state_key] = timestamp
                logger.info(f"Updated presence state for {agent}: {agent_state} at {timestamp.astimezone(ET).isoformat()}")

        # Handle activity record events for state tracking
        elif event_type == "channel.activityrecord.v0":
            interaction = event_data.get("interaction", {})
            state = interaction.get("state", "").lower()
            start_time_str = interaction.get("startTime")
            end_time_str = interaction.get("endTime")

            if state:
                if state == "wrap":
                    agent_state = "Wrap"
                elif state == "busy":
                    agent_state = "Busy"
                elif state == "ready":
                    agent_state = "Ready"

            if agent_state and start_time_str:
                try:
                    start_timestamp = parse(start_time_str).replace(tzinfo=pytz.UTC)
                    state_key = f"{agent}:{agent_state}"
                    agent_state_timestamps[state_key] = start_timestamp
                    logger.info(f"Updated state timestamp for {agent} in state {agent_state}: {start_timestamp.astimezone(ET).isoformat()}")
                except Exception as e:
                    logger.error(f"Failed to parse startTime {start_time_str}: {e}")

            if end_time_str:
                try:
                    end_timestamp = parse(end_time_str).replace(tzinfo=pytz.UTC)
                    state_key = f"{agent}:{agent_state}"
                    if state_key in agent_state_timestamps:
                        del agent_state_timestamps[state_key]
                        logger.info(f"Cleared state timestamp for {agent} in state {agent_state} at {end_timestamp.astimezone(ET).isoformat()}")
                except Exception as e:
                    logger.error(f"Failed to parse endTime {end_time_str}: {e}")

        # If not a presence change or activity record, check for specific channel events
        if not agent_state:
            if event_type == "channel.connectionfailed.v1":
                agent_state = "Device Busy"
            elif event_type == "channel.ended.v1":
                agent_state = "Logged Out"
            elif event_type == "channel.held.v1":
                agent_state = "Break"
            elif event_type == "channel.interrupted.v1":
                agent_state = "Break"
            elif event_type == "channel.resumed.v1":
                agent_state = "Ready"
            elif event_type == "channel.retrieved.v1":
                agent_state = "Ready"
            elif event_type == "channel.unparked.v1":
                agent_state = "Ready"
            elif event_type == "channel.wrapstarted.v1":
                agent_state = "Wrap"

        # If we still don't have an agent state, check the stored presence state
        if not agent_state and agent in agent_presence_states:
            agent_state = agent_presence_states[agent][0]

        if not agent_state:
            agent_state = "Unknown"
            logger.warning(f"Agent state could not be determined for {agent}, defaulting to Unknown")

        # Update the state timestamp if not already set by activity record
        state_key = f"{agent}:{agent_state}"
        if state_key not in agent_state_timestamps:
            agent_state_timestamps[state_key] = timestamp
            logger.info(f"Set initial timestamp for {agent} in state {agent_state}: {timestamp.astimezone(ET).isoformat()}")

        # Calculate duration based on the time the agent entered the current state
        duration_ms = get_event_duration(agent, agent_state, timestamp)
        duration_min = parse_duration(duration_ms)

        if event_type in ["channel.ended.v1", "channel.disconnected.v1", "interaction.detailrecord.v0"]:
            logger.info(f"Skipping notification for event type: {event_type}")
            if state_key in agent_state_timestamps:
                del agent_state_timestamps[state_key]
                logger.info(f"Cleared state timestamp for {agent} in state {agent_state}")
            return jsonify({"status": "skipped", "message": f"Notifications disabled for {event_type}"}), 200

        is_in_shift = is_within_shift(agent, timestamp)
        logger.info(f"Event: {event_type}, Agent: {agent}, Agent State: {agent_state}, Duration: {duration_min} min, In Shift: {is_in_shift}, Campaign: {campaign}, Interaction ID: {interaction_id}")

        # Deduplicate alerts
        alert_key = f"{agent}:{agent_state}"
        if alert_key in last_alerts:
            last_alert_time = last_alerts[alert_key]
            time_since_last_alert = (timestamp - last_alert_time).total_seconds() / 60
            if time_since_last_alert < 5:
                logger.info(f"Skipping duplicate alert for {agent}: {agent_state} (last sent {time_since_last_alert:.2f} minutes ago)")
                return jsonify({"status": "skipped", "message": "Duplicate alert skipped"}), 200

        if should_trigger_alert(agent_state, duration_min, is_in_shift, event_data):
            agent_state = event_data.get("alert_agent_state", agent_state)
            duration_min = event_data.get("alert_duration_min", duration_min)
            emoji = get_emoji_for_event(agent_state)
            team = agent_teams.get(agent, "Unknown Team")
            vonage_link = "https://nam.newvoicemedia.com/CallCentre/portal/interactionsearch"
            states_without_interaction = ["Idle", "Idle (Outbound)", "Device Busy", "Device Unreachable", "Fault", "In Meeting", "Paperwork", "Team Meeting", "Training", "Logged Out"]

            # Log the alert to the weekly tab
            log_to_followups(agent, timestamp, duration_min, interaction_id, agent_state, campaign, status="Open")

            if agent_state in ["Training", "In Meeting", "Paperwork", "Team Meeting"]:
                blocks = [
                    {"type": "section", "text": {"type": "mrkdwn", "text": f"{emoji} *{agent_state} Alert*\nAgent: {agent}\nTeam: {team}\nDuration: {duration_min:.2f} min"}},
                    {"type": "actions", "elements": [
                        {"type": "button", "text": {"type": "plain_text", "text": "✅ Approved by Management"}, "value": f"approve|{agent}|{interaction_id}|{agent_state}", "action_id": "approve_event"},
                        {"type": "button", "text": {"type": "plain_text", "text": "❌ Not Approved"}, "value": f"not_approve|{agent}|{interaction_id}|{agent_state}", "action_id": "not_approve_event"}
                    ]}
                ]
            elif agent_state in states_without_interaction:
                blocks = [
                    {"type": "section", "text": {"type": "mrkdwn", "text": f"{emoji} *{agent_state} Alert*\nAgent: {agent}\nTeam: {team}\nDuration: {duration_min:.2f} min"}},
                    {"type": "actions", "elements": [
                        {"type": "button", "text": {"type": "plain_text", "text": "✅ Assigned to Me"}, "value": f"assign|{agent}|{interaction_id}|{agent_state}|{duration_min}", "action_id": "assign_to_me"}
                    ]}
                ]
            else:
                buttons = [
                    {"type": "button", "text": {"type": "plain_text", "text": "✅ Assigned to Me"}, "value": f"assign|{agent}|{interaction_id}|{agent_state}|{duration_min}", "action_id": "assign_to_me"},
                    {"type": "button", "text": {"type": "plain_text", "text": "📋 Copy"}, "value": interaction_id, "action_id": "copy_interaction_id"},
                    {"type": "button", "text": {"type": "plain_text", "text": "🔗 Vonage"}, "url": vonage_link, "action_id": "vonage_link"}
                ]
                # Removed Interaction ID from the message text
                blocks = [
                    {"type": "section", "text": {"type": "mrkdwn", "text": f"{emoji} *{agent_state} Alert*\nAgent: {agent}\nTeam: {team}\nDuration: {duration_min:.2f} min\nCampaign: {campaign}"}},
                    {"type": "actions", "elements": buttons}
                ]
            post_result = post_slack_message(ALERT_CHANNEL_ID, blocks)
            if post_result:
                last_alerts[alert_key] = timestamp
                logger.info(f"Successfully posted alert to Slack with ts: {post_result}")
            else:
                logger.error("Failed to post alert to Slack")
        else:
            logger.info(f"Alert not triggered for {agent}: Agent State={agent_state}, Duration={duration_min}, In Shift={is_in_shift}")
        return jsonify({"status": "posted"}), 200
    except Exception as e:
        logger.error(f"ERROR in /vonage-events: {e}")
        return jsonify({"status": "error", "message": str(e)}), 200

# ========== SLACK COMMANDS ==========
@app.route("/slack/commands/weekly_update_form", methods=["GET", "POST"])
def slack_command_weekly_update_form():
    current_time_et = datetime.utcnow().replace(tzinfo=pytz.UTC).astimezone(ET)
    logger.info(f"Received {request.method} request to /slack/commands/weekly_update_form at {current_time_et.isoformat()}")
    if request.method == "GET":
        logger.info("Slack verification request received")
        return "This endpoint is for Slack slash commands. Please use POST to send a command.", 200

    try:
        logger.debug(f"Slash command payload: {request.form}")
        trigger_id = request.form.get("trigger_id")
        channel_id = request.form.get("channel_id")
        
        if not trigger_id:
            logger.error("Missing trigger_id in request.form")
            return "Missing trigger_id", 400
        if not channel_id:
            logger.error("Missing channel_id in request.form")
            return "Missing channel_id", 400

        logger.info(f"SLACK_BOT_TOKEN: {'Set' if SLACK_BOT_TOKEN else 'Not Set'}")
        logger.debug(f"Headers: {headers}")

        modal = {
            "trigger_id": trigger_id,
            "view": {
                "type": "modal",
                "callback_id": "weekly_update_modal",
                "title": {"type": "plain_text", "text": "Team Progress Log"},
                "submit": {"type": "plain_text", "text": "Submit"},
                "close": {"type": "plain_text", "text": "Close"},
                "private_metadata": json.dumps({"channel_id": channel_id}),
                "blocks": [
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": "*Let’s capture this week’s wins, challenges, and team progress below. 👇*"}
                    },
                    {
                        "type": "input",
                        "block_id": "start_date",
                        "element": {
                            "type": "datepicker",
                            "action_id": "start_date_picker",
                            "placeholder": {"type": "plain_text", "text": "Select start date"}
                        },
                        "label": {"type": "plain_text", "text": "Start of Week"}
                    },
                    {
                        "type": "input",
                        "block_id": "end_date",
                        "element": {
                            "type": "datepicker",
                            "action_id": "end_date_picker",
                            "placeholder": {"type": "plain_text", "text": "Select end date"}
                        },
                        "label": {"type": "plain_text", "text": "End of Week"}
                    },
                    {
                        "type": "input",
                        "block_id": "top_performers",
                        "element": {
                            "type": "multi_static_select",
                            "action_id": "top_performers_select",
                            "placeholder": {"type": "plain_text", "text": "Select top performers"},
                            "options": employee_options
                        },
                        "label": {"type": "plain_text", "text": "Top Performers"}
                    },
                    {
                        "type": "input",
                        "block_id": "top_support",
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "top_support_input",
                            "multiline": True,
                            "placeholder": {"type": "plain_text", "text": "How are you supporting top performers?"}
                        },
                        "label": {"type": "plain_text", "text": "Support Actions for Top Performers"}
                    },
                    {
                        "type": "input",
                        "block_id": "bottom_performers",
                        "element": {
                            "type": "multi_static_select",
                            "action_id": "bottom_performers_select",
                            "placeholder": {"type": "plain_text", "text": "Select bottom performers"},
                            "options": employee_options
                        },
                        "label": {"type": "plain_text", "text": "Bottom Performers"}
                    },
                    {
                        "type": "input",
                        "block_id": "bottom_actions",
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "bottom_actions_input",
                            "multiline": True,
                            "placeholder": {"type": "plain_text", "text": "Describe coaching, follow-up, etc."}
                        },
                        "label": {"type": "plain_text", "text": "Support Actions for Bottom Performers"}
                    },
                    {
                        "type": "input",
                        "block_id": "improvement_plan",
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "improvement_plan_input",
                            "multiline": True,
                            "placeholder": {"type": "plain_text", "text": "Are they improving? What's the plan?"}
                        },
                        "label": {"type": "plain_text", "text": "Improvement Plan"}
                    },
                    {
                        "type": "input",
                        "block_id": "team_momentum",
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "team_momentum_input",
                            "multiline": True,
                            "placeholder": {"type": "plain_text", "text": "Are you rising together or are there support gaps?"}
                        },
                        "label": {"type": "plain_text", "text": "Team Momentum"}
                    },
                    {
                        "type": "input",
                        "block_id": "trends",
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "trends_input",
                            "multiline": True,
                            "placeholder": {"type": "plain_text", "text": "Any recurring behaviors, client feedback, or performance shifts?"}
                        },
                        "label": {"type": "plain_text", "text": "Trends"}
                    },
                    {
                        "type": "input",
                        "block_id": "additional_notes",
                        "optional": True,
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "notes_input",
                            "multiline": True,
                            "placeholder": {"type": "plain_text", "text": "Shoutouts, observations, anything else to share?"}
                        },
                        "label": {"type": "plain_text", "text": "Additional Notes"}
                    }
                ]
            }
        }
        logger.info("Opening modal for weekly update form")
        response = session.post("https://slack.com/api/views.open", headers=headers, json=modal)
        logger.info(f"Slack API response status: {response.status_code}")
        logger.info(f"Slack API response: {response.text}")
        if response.status_code != 200 or not response.json().get("ok"):
            logger.error(f"Failed to open modal: {response.text}")
            error_blocks = [
                {"type": "section", "text": {"type": "mrkdwn", "text": f"⚠️ Failed to open weekly update modal: {response.text}"}}
            ]
            session.post("https://slack.com/api/chat.postMessage", headers=headers, json={"channel": BOT_ERROR_CHANNEL_ID, "blocks": error_blocks})
        else:
            logger.info("Modal request sent to Slack successfully")
        return "", 200
    except Exception as e:
        logger.error(f"ERROR in /slack/commands/weekly_update_form: {e}")
        return "Internal server error", 500

# ========== SLACK INTERACTIONS AND VIEW SUBMISSIONS ==========
@app.route("/slack/interactions", methods=["POST"])
def slack_interactions():
    current_time_et = datetime.utcnow().replace(tzinfo=pytz.UTC).astimezone(ET)
    logger.info(f"Received request to /slack/interactions at {current_time_et.isoformat()}")
    try:
        payload = json.loads(request.form["payload"])
        logger.debug(f"Interactivity payload: {json.dumps(payload, indent=2)}")

        if payload["type"] == "block_actions":
            action_id = payload["actions"][0]["action_id"]
            user = payload["user"]["username"].replace(".", " ").title()
            response_url = payload["response_url"]

            if action_id == "assign_to_me":
                value = payload["actions"][0]["value"]
                _, agent, campaign, agent_state, duration_min = value.split("|")
                duration_min = float(duration_min)
                thread_ts = payload["message"]["ts"]
                blocks = [
                    {"type": "section", "text": {"type": "mrkdwn", "text": f"🔍 @{user} is investigating this {agent_state} alert for {agent}."}},
                    {"type": "actions", "elements": [
                        {"type": "button", "text": {"type": "plain_text", "text": "📝 Follow-Up"}, "value": f"followup|{agent}|{campaign}|{agent_state}|{duration_min}", "action_id": "open_followup"}
                    ]}
                ]
                post_slack_message(ALERT_CHANNEL_ID, blocks, thread_ts=thread_ts)

                # Log the "Assigned to Me" action to the weekly tab
                year = datetime.utcnow().year
                team = agent_teams.get(agent, "Unknown Team")
                log_to_followups(
                    agent=agent,
                    timestamp=datetime.utcnow().replace(tzinfo=pytz.UTC),
                    duration_min=duration_min,
                    interaction_id=campaign,
                    agent_state=agent_state,
                    campaign=campaign,
                    user=user,
                    approval_decision="Assigned",
                    status="Assigned"
                )
                logger.info(f"Logged 'Assigned to Me' action for {agent}: {agent_state}")

            elif action_id == "approve_event":
                value = payload["actions"][0]["value"]
                _, agent, campaign, agent_state = value.split("|")
                thread_ts = payload["message"]["ts"]
                blocks = [
                    {"type": "section", "text": {"type": "mrkdwn", "text": f"✅ *{agent_state} Approved*\nAgent: {agent}\nApproved by: @{user}\nInteraction ID: {campaign}"}}
                ]
                post_slack_message(ALERT_CHANNEL_ID, blocks, thread_ts=thread_ts)

                # Log the approval to the weekly tab
                year = datetime.utcnow().year
                team = agent_teams.get(agent, "Unknown Team")
                log_to_followups(
                    agent=agent,
                    timestamp=datetime.utcnow().replace(tzinfo=pytz.UTC),
                    duration_min=0,
                    interaction_id=campaign,
                    agent_state=agent_state,
                    campaign=campaign,
                    user=user,
                    approval_decision="Approved",
                    approved_by=user,
                    status="Resolved"
                )
                logger.info(f"Logged approval for {agent}: {agent_state}")

            elif action_id == "not_approve_event":
                value = payload["actions"][0]["value"]
                _, agent, campaign, agent_state = value.split("|")
                thread_ts = payload["message"]["ts"]
                blocks = [
                    {"type": "section", "text": {"type": "mrkdwn", "text": f"🔍 @{user} is investigating this {agent_state} alert for {agent}."}},
                    {"type": "actions", "elements": [
                        {"type": "button", "text": {"type": "plain_text", "text": "📝 Follow-Up"}, "value": f"followup|{agent}|{campaign}|{agent_state}|0", "action_id": "open_followup"}
                    ]}
                ]
                post_slack_message(ALERT_CHANNEL_ID, blocks, thread_ts=thread_ts)

                # Log the non-approval to the weekly tab
                year = datetime.utcnow().year
                team = agent_teams.get(agent, "Unknown Team")
                log_to_followups(
                    agent=agent,
                    timestamp=datetime.utcnow().replace(tzinfo=pytz.UTC),
                    duration_min=0,
                    interaction_id=campaign,
                    agent_state=agent_state,
                    campaign=campaign,
                    user=user,
                    approval_decision="Not Approved",
                    approved_by=user,
                    status="Assigned"
                )
                logger.info(f"Logged non-approval for {agent}: {agent_state}")

            elif action_id == "copy_interaction_id":
                value = payload["actions"][0]["value"]
                thread_ts = payload["message"]["ts"]
                blocks = [
                    {"type": "section", "text": {"type": "mrkdwn", "text": f"📋 Interaction ID `{value}` - Please copy it manually from here."}}
                ]
                post_slack_message(ALERT_CHANNEL_ID, blocks, thread_ts=thread_ts)
                logger.info(f"User {user} requested to copy Interaction ID: {value}")

            elif action_id == "open_followup":
                logger.info(f"Handling open_followup action for user: {user} at {datetime.utcnow().replace(tzinfo=pytz.UTC).astimezone(ET).isoformat()}")
                value = payload["actions"][0]["value"]
                logger.debug(f"Button value: {value}")
                try:
                    _, agent, interaction_id, agent_state, duration_min = value.split("|")
                    duration_min = float(duration_min)
                except ValueError as e:
                    logger.error(f"Failed to parse button value '{value}': {e}")
                    fallback_blocks = [
                        {"type": "section", "text": {"type": "mrkdwn", "text": f"⚠️ Error processing follow-up request for {agent}. Please try again."}}
                    ]
                    post_slack_message(ALERT_CHANNEL_ID, fallback_blocks, thread_ts=payload["message"]["ts"])
                    return "", 200

                trigger_id = payload["trigger_id"]
                thread_ts = payload["message"]["ts"]
                logger.debug(f"Trigger ID: {trigger_id}")

                # Ensure headers is accessible
                global headers
                if not isinstance(headers, dict) or "Authorization" not in headers:
                    logger.warning("Headers variable is missing or invalid, reinitializing...")
                    headers = {
                        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
                        "Content-Type": "application/json"
                    }

                modal = {
                    "trigger_id": trigger_id,
                    "view": {
                        "type": "modal",
                        "callback_id": "followup_submit",
                        "title": {"type": "plain_text", "text": "Follow-Up"},
                        "submit": {"type": "plain_text", "text": "Submit"},
                        "close": {"type": "plain_text", "text": "Close"},
                        "blocks": [
                            {
                                "type": "section",
                                "text": {"type": "mrkdwn", "text": f"*Follow-Up for {agent} - {agent_state} Alert*"}
                            },
                            {
                                "type": "input",
                                "block_id": "monitoring",
                                "element": {
                                    "type": "static_select",
                                    "placeholder": {"type": "plain_text", "text": "Select an option"},
                                    "options": [
                                        {"text": {"type": "plain_text", "text": "Listen In"}, "value": "listen_in"},
                                        {"text": {"type": "plain_text", "text": "Coach"}, "value": "coach"},
                                        {"text": {"type": "plain_text", "text": "Join"}, "value": "join"},
                                        {"text": {"type": "plain_text", "text": "None"}, "value": "none"}
                                    ],
                                    "action_id": "monitoring_method"
                                },
                                "label": {"type": "plain_text", "text": "Monitoring Method"}
                            },
                            {
                                "type": "input",
                                "block_id": "action",
                                "element": {
                                    "type": "plain_text_input",
                                    "action_id": "action_taken",
                                    "multiline": True,
                                    "placeholder": {"type": "plain_text", "text": "e.g. Coached agent, verified call handling"}
                                },
                                "label": {"type": "plain_text", "text": "What did you do?"}
                            },
                            {
                                "type": "input",
                                "block_id": "reason",
                                "element": {
                                    "type": "plain_text_input",
                                    "action_id": "reason_for_issue",
                                    "multiline": True,
                                    "placeholder": {"type": "plain_text", "text": "e.g. Client had multiple questions"}
                                },
                                "label": {"type": "plain_text", "text": "Reason for issue"}
                            },
                            {
                                "type": "input",
                                "block_id": "notes",
                                "element": {
                                    "type": "plain_text_input",
                                    "action_id": "additional_notes",
                                    "multiline": True,
                                    "placeholder": {"type": "plain_text", "text": "Optional comments"}
                                },
                                "label": {"type": "plain_text", "text": "Additional notes"}
                            }
                        ],
                        "private_metadata": json.dumps({
                            "agent": agent,
                            "interaction_id": interaction_id,
                            "agent_state": agent_state,
                            "duration_min": duration_min,
                            "user": user,
                            "thread_ts": thread_ts
                        })
                    }
                }

                logger.info(f"Sending views.open request to Slack with modal")
                for attempt in range(5):
                    try:
                        response = session.post("https://slack.com/api/views.open", headers=headers, json=modal)
                        logger.info(f"Attempt {attempt + 1}: Slack API response status: {response.status_code}")
                        logger.info(f"Attempt {attempt + 1}: Slack API response: {response.text}")
                        if response.status_code == 429:
                            retry_after = int(response.headers.get("Retry-After", 1))
                            logger.warning(f"Rate-limited by Slack, retrying after {retry_after} seconds")
                            time.sleep(retry_after)
                            continue
                        if response.status_code != 200 or not response.json().get("ok"):
                            error_message = response.json().get("error", "Unknown error")
                            logger.error(f"Failed to open follow-up modal: {response.text}")
                            if error_message == "invalid_trigger":
                                logger.error("Trigger ID expired or invalid. Ensure the button is clicked within 30 seconds.")
                            elif error_message == "missing_scope":
                                logger.error("Missing modals:write scope. Check Slack bot token scopes.")
                            fallback_blocks = [
                                {"type": "section", "text": {"type": "mrkdwn", "text": f"⚠️ Failed to open follow-up modal for {agent}. Error: {error_message}. Please try again or use a manual form."}}
                            ]
                            post_slack_message(ALERT_CHANNEL_ID, fallback_blocks, thread_ts=thread_ts)
                            error_blocks = [
                                {"type": "section", "text": {"type": "mrkdwn", "text": f"⚠️ Failed to open follow-up modal for {agent}: {response.text}"}}
                            ]
                            session.post("https://slack.com/api/chat.postMessage", headers=headers, json={"channel": BOT_ERROR_CHANNEL_ID, "blocks": error_blocks})
                            break
                        logger.info("Follow-up modal request sent to Slack successfully")
                        break
                    except Exception as e:
                        logger.error(f"ERROR: Failed to open follow-up modal: {e}")
                        if attempt < 4:
                            time.sleep(2 ** attempt)
                            continue
                        fallback_blocks = [
                            {"type": "section", "text": {"type": "mrkdwn", "text": f"⚠️ Failed to open the follow-up modal for {agent}. Error: {str(e)}. Please use a manual form to submit your follow-up."}}
                        ]
                        post_slack_message(ALERT_CHANNEL_ID, fallback_blocks, thread_ts=thread_ts)
                        error_blocks = [
                            {"type": "section", "text": {"type": "mrkdwn", "text": f"⚠️ Failed to open follow-up modal for {agent}: {str(e)}"}}
                        ]
                        session.post("https://slack.com/api/chat.postMessage", headers=headers, json={"channel": BOT_ERROR_CHANNEL_ID, "blocks": error_blocks})
                return "", 200

        elif payload["type"] == "view_submission":
            callback_id = payload["view"]["callback_id"]
            logger.info(f"Processing view submission with callback_id: {callback_id}")

            if callback_id == "followup_submit":
                logger.info("Handling followup_submit")
                values = payload["view"]["state"]["values"]
                metadata = json.loads(payload["view"]["private_metadata"])
                agent = metadata["agent"]
                interaction_id = metadata["interaction_id"]
                agent_state = metadata["agent_state"]
                duration_min = float(metadata["duration_min"])
                user = metadata["user"]
                monitoring = values["monitoring"]["monitoring_method"]["selected_option"]["value"]
                action = values["action"]["action_taken"]["value"]
                reason = values["reason"]["reason_for_issue"]["value"]
                notes = values["notes"]["additional_notes"]["value"]

                # Log the follow-up submission to the weekly tab
                year = datetime.utcnow().year
                team = agent_teams.get(agent, "Unknown Team")
                log_to_followups(
                    agent=agent,
                    timestamp=datetime.utcnow().replace(tzinfo=pytz.UTC),
                    duration_min=duration_min,
                    interaction_id=interaction_id,
                    agent_state=agent_state,
                    campaign="",
                    user=user,
                    monitoring=monitoring,
                    action=action,
                    reason=reason,
                    notes=notes,
                    status="Resolved"
                )
                logger.info(f"Logged follow-up submission for {agent}: {agent_state}")

            elif callback_id == "weekly_update_modal":
                logger.info("Handling weekly_update_modal submission")
                values = payload["view"]["state"]["values"]
                metadata = json.loads(payload["view"]["private_metadata"])
                channel_id = metadata["channel_id"]
                try:
                    start_date = datetime.strptime(values["start_date"]["start_date_picker"]["selected_date"], "%Y-%m-%d")
                    end_date = datetime.strptime(values["end_date"]["end_date_picker"]["selected_date"], "%Y-%m-%d")
                except Exception as e:
                    logger.error(f"Failed to parse dates: {e}")
                    error_blocks = [
                        {"type": "section", "text": {"type": "mrkdwn", "text": f"⚠️ Failed to process weekly update submission: Invalid date format. Please try again."}}
                    ]
                    session.post("https://slack.com/api/chat.postMessage", headers=headers, json={"channel": BOT_ERROR_CHANNEL_ID, "blocks": error_blocks})
                    return jsonify({"response_action": "clear"}), 200

                top_performers = [option["value"].replace("_", " ").title() for option in values["top_performers"]["top_performers_select"]["selected_options"]]
                top_support = values["top_support"]["top_support_input"]["value"]
                bottom_performers = [option["value"].replace("_", " ").title() for option in values["bottom_performers"]["bottom_performers_select"]["selected_options"]]
                bottom_actions = values["bottom_actions"]["bottom_actions_input"]["value"]
                improvement_plan = values["improvement_plan"]["improvement_plan_input"]["value"]
                team_momentum = values["team_momentum"]["team_momentum_input"]["value"]
                trends = values["trends"]["trends_input"]["value"]
                additional_notes = values["additional_notes"]["notes_input"]["value"] if "additional_notes" in values else ""

                user = payload["user"]["username"].replace(".", " ").title()
                week = f"{start_date.strftime('%b %-d')} - {end_date.strftime('%b %-d')}"

                # Log to Google Sheet (WEEKLY_UPDATE_SHEET_ID)
                year = start_date.year
                sheets_service_info = get_sheets_service(year, sheet_type="weekly_update")
                if sheets_service_info:
                    sheets_service, spreadsheet_id = sheets_service_info
                    sheet_name = f"Weekly {week}"
                    headers = [
                        "Timestamp (UTC)", "Submitted By", "Top Performers", "Support Actions",
                        "Bottom Performers", "Action Plans", "Improvement Plan", "Team Momentum", "Trends", "Additional Notes"
                    ]
                    try:
                        get_or_create_sheet_with_headers(sheets_service, spreadsheet_id, sheet_name, headers)
                        body = {
                            "values": [[
                                datetime.utcnow().isoformat(), user, ", ".join(top_performers), top_support,
                                ", ".join(bottom_performers), bottom_actions, improvement_plan, team_momentum, trends, additional_notes
                            ]]
                        }
                        sheets_service.spreadsheets().values().append(
                            spreadsheetId=spreadsheet_id,
                            range=f"'{sheet_name}'!A2",
                            valueInputOption="USER_ENTERED",
                            body=body
                        ).execute()
                        logger.info(f"Logged weekly update to Google Sheet: {sheet_name} for year {year}")
                    except Exception as e:
                        logger.error(f"Failed to log weekly update to Google Sheet: {e}")
                        error_blocks = [
                            {"type": "section", "text": {"type": "mrkdwn", "text": f"⚠️ Failed to log weekly update to Google Sheet (sheet: {sheet_name}): {str(e)}"}}
                        ]
                        session.post("https://slack.com/api/chat.postMessage", headers=headers, json={"channel": BOT_ERROR_CHANNEL_ID, "blocks": error_blocks})
                else:
                    logger.warning(f"Could not log to Google Sheets for year {year} (weekly_update)")
                    error_blocks = [
                        {"type": "section", "text": {"type": "mrkdwn", "text": f"⚠️ Could not log weekly update to Google Sheets for year {year} (weekly_update)."}}
                    ]
                    session.post("https://slack.com/api/chat.postMessage", headers=headers, json={"channel": BOT_ERROR_CHANNEL_ID, "blocks": error_blocks})

                # Post the summary to Slack
                summary_blocks = [
                    {"type": "header", "text": {"type": "plain_text", "text": f"📈 Team Progress Log – {week}"}},
                    {"type": "section", "text": {"type": "mrkdwn", "text": f"*Submitted by:* {user}"}},
                    {"type": "section", "text": {"type": "mrkdwn", "text": f"*Top Performers:*\n{', '.join(top_performers)}\n*Support Actions:*\n{top_support}"}},
                    {"type": "section", "text": {"type": "mrkdwn", "text": f"*Bottom Performers:*\n{', '.join(bottom_performers)}\n*Support Actions:*\n{bottom_actions}"}},
                    {"type": "section", "text": {"type": "mrkdwn", "text": f"*Improvement Plan:*\n{improvement_plan}"}},
                    {"type": "section", "text": {"type": "mrkdwn", "text": f"*Team Momentum:*\n{team_momentum}"}},
                    {"type": "section", "text": {"type": "mrkdwn", "text": f"*Trends:*\n{trends}"}},
                    {"type": "section", "text": {"type": "mrkdwn", "text": f"*Additional Notes:*\n{additional_notes}" if additional_notes else "*Additional Notes:*\nNone"}}
                ]
                post_slack_message(ALERT_CHANNEL_ID, summary_blocks)

                # Post the success message to ALERT_CHANNEL_ID
                success_message = f"✅ Weekly update for {week} submitted successfully by {user}!"
                success_blocks = [
                    {"type": "section", "text": {"type": "mrkdwn", "text": success_message}}
                ]
                post_slack_message(ALERT_CHANNEL_ID, success_blocks)

                return jsonify({"response_action": "clear"}), 200

        return "", 200
    except Exception as e:
        logger.error(f"ERROR in /slack/interactions: {e}")
        return "", 200  # Return 200 to Slack to acknowledge the interaction

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=8080)