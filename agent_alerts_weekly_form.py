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
# Configure logging to both console and file
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
BOT_ERROR_CHANNEL_ID = os.environ.get("BOT_ERROR_CHANNEL_ID", ALERT_CHANNEL_ID)  # Fallback to ALERT_CHANNEL_ID if not set
SCOPES = json.loads(os.environ.get("GOOGLE_SHEETS_SCOPES", '["https://www.googleapis.com/auth/spreadsheets"]'))

# Map years to spreadsheet IDs for weekly updates
WEEKLY_UPDATE_SPREADSHEET_IDS = {
    2025: os.environ["WEEKLY_UPDATE_SHEET_ID"],
    2026: "1dlmzbFj5iC92oeDhrFzuJ-_eb_6sjXWMMZ6JNJ6EwoY"
}

# Map years to spreadsheet IDs for follow-ups (used for both real-time alerts and follow-up submissions)
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
# Clear on startup to start fresh
agent_presence_states = {}

# Dictionary to track the last alert sent for each agent and state (for deduplication)
last_alerts = {}

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
retries = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
session.mount('https://', HTTPAdapter(max_retries=retries))

def post_slack_message(channel, blocks, thread_ts=None, retry_count=3):
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
            if response.status_code == 429:  # Rate-limited
                retry_after = int(response.headers.get("Retry-After", 1))
                logger.warning(f"Rate-limited by Slack, retrying after {retry_after} seconds")
                time.sleep(retry_after)
                continue
            if response.status_code != 200:
                logger.error(f"Failed to post to Slack: {response.status_code} - {response.text}")
                # Post error to bot error channel
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
                time.sleep(2 ** attempt)  # Exponential backoff
                continue
            # Post error to bot error channel
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

# Agent teams - Jessica Lopez moved to Team Adriana
agent_teams = {
    "Carla Hagerman": "Team Adriana 💎",
    "Dajah Blackwell": "Team Adriana 💎",
    "Felicia Martin": "Team Adriana 💎",
    "Felicia Randall": "Team Adriana 💎",
    "Jeanette Bantz": "Team Adriana 💎",
    "Jesse Lorenzana Escarfullery": "Team Adriana 💎",
    "Jessica Lopez": "Team Adriana 💎",  # Moved from "Team Bee Hive 🐝"
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

# Updated Campaign mapping dictionary
CAMPAIGN_MAPPING = {
    "+13234547738": "SETC Incoming Calls",
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
    """Map a Vonage phone number to a campaign name."""
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
def parse_duration(duration):
    try:
        duration_ms = int(duration)
        duration_min = duration_ms / 1000 / 60  # Convert ms to minutes
        return duration_min
    except (ValueError, TypeError) as e:
        logger.error(f"ERROR in parse_duration: {e}")
        return 0

# ========== WEEKLY TAB HELPER ==========
def get_weekly_tab_name(timestamp):
    """Determine the weekly tab name (e.g., 'Weekly Apr 1 - Apr 7') based on the timestamp."""
    try:
        # Find the Monday of the current week (assuming weeks start on Monday)
        date = timestamp.date()
        days_to_monday = (date.weekday() - 0) % 7  # 0 is Monday
        monday = date - timedelta(days=days_to_monday)
        # Find the Sunday of the same week
        sunday = monday + timedelta(days=6)
        # Format the tab name
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
def log_to_followups(agent, timestamp, duration_min, interaction_id, agent_state, campaign, user=None, monitoring=None, action=None, reason=None, notes=None, approval_decision=None, approved_by=None):
    try:
        year = timestamp.year
        sheets_service_info = get_sheets_service(year, sheet_type="followup")
        if not sheets_service_info:
            logger.warning(f"Google Sheets service not available for year {year} (followup), skipping logging")
            return

        sheets_service, spreadsheet_id = sheets_service_info
        # Determine the weekly tab name (e.g., "Weekly Apr 1 - Apr 7")
        sheet_name = get_weekly_tab_name(timestamp)
        headers = [
            "Timestamp", "Agent Name", "Agent State", "Duration (min)", "Interaction ID",
            "Campaign", "Team", "Assigned To (Lead)", "Monitoring Method",
            "Follow-Up Action", "Reason for Issue", "Additional Notes", "Approval Decision", "Approved By (Slack)"
        ]
        get_or_create_sheet_with_headers(sheets_service, spreadsheet_id, sheet_name, headers)
        team = agent_teams.get(agent, "Unknown Team")

        # Convert timestamp to EDT for display
        timestamp_et = timestamp.astimezone(ET)
        formatted_timestamp = timestamp_et.strftime("%Y-%m-%d %I:%M:%S %p")

        # Determine if Interaction ID and Campaign are applicable for this state
        states_with_interaction = ["Busy", "Wrap", "Outgoing Wrap Up"]
        interaction_id_value = interaction_id if agent_state in states_with_interaction else "Not Applicable for This State"
        campaign_value = campaign if agent_state in states_with_interaction else "Not Applicable for This State"

        values = [[
            formatted_timestamp, agent, agent_state, duration_min, interaction_id_value,
            campaign_value, team, user if user else "", monitoring if monitoring else "",
            action if action else "", reason if reason else "", notes if notes else "",
            approval_decision if approval_decision else "", approved_by if approved_by else ""
        ]]
        sheets_service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=f"'{sheet_name}'!A1",
            valueInputOption="USER_ENTERED",
            body={"values": values}
        ).execute()
        logger.info(f"Logged to {sheet_name} tab for {agent} at {formatted_timestamp}")
    except Exception as e:
        logger.error(f"Failed to log to {sheet_name} tab: {e}")
        # Post error to bot error channel
        error_blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"⚠️ Failed to log to Google Sheets (sheet: {sheet_name}): {str(e)}"}}
        ]
        session.post("https://slack.com/api/chat.postMessage", headers=headers, json={"channel": BOT_ERROR_CHANNEL_ID, "blocks": error_blocks})

# Calculate duration since last state change for presence alerts
def get_event_duration(agent, current_timestamp, current_agent_state):
    try:
        # Check if the agent has a recorded presence state
        if agent not in agent_presence_states:
            logger.info(f"Agent {agent} has no recorded presence state")
            return 0

        last_state, last_timestamp = agent_presence_states[agent]
        if last_state == current_agent_state:
            logger.info(f"Agent {agent} state unchanged: {last_state}")
            return 0  # No duration if the state hasn't changed

        duration_ms = (current_timestamp - last_timestamp).total_seconds() * 1000
        logger.info(f"Calculated duration for {agent}: {duration_ms} ms")
        return duration_ms
    except Exception as e:
        logger.error(f"ERROR in get_event_duration for agent {agent}: {e}")
        return 0

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
        elif agent_state in ["Device Busy", "Device Unreachable", "Fault", "Away", "Extended Away", "In Meeting", "Paperwork", "Team Meeting", "Training"]:
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
        "Away": "🚶‍♂️",
        "Extended Away": "🚶‍♂️",
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

    # Check Slack API connectivity
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

    # Check Google Sheets connectivity
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

        logger.debug(f"Vonage event payload: {json.dumps(data, indent=2)}")

        event_type = data.get("type", None)
        if not event_type:
            logger.error("Missing event type in Vonage payload")
            return jsonify({"status": "error", "message": "Missing event type"}), 400

        # Skip processing for channel.alerted.v1 and channel.connected.v1
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

        # Extract agent name
        agent = None
        agent_id = None
        if event_type == "agent.presencechanged.v1":
            user_data = event_data.get("user", {})
            if not user_data:
                logger.error("Missing user data in agent.presencechanged.v1 event")
                return jsonify({"status": "error", "message": "Missing user data"}), 400
            agent_id = user_data.get("agentId", None)
            if agent_id:
                agent = agent_id_to_name.get(agent_id, None)
        elif "interaction" in event_data:
            if "channel" in event_data["interaction"]:
                channel = event_data["interaction"]["channel"]
                if channel.get("party", {}).get("role") == "agent":
                    agent_id = channel["party"].get("agentId", None)
                    if agent_id:
                        agent = agent_id_to_name.get(agent_id, None)
            elif "channels" in event_data["interaction"]:
                for channel in event_data["interaction"]["channels"]:
                    if channel.get("party", {}).get("role") == "agent":
                        agent_id = channel["party"].get("agentId", None)
                        if agent_id:
                            agent = agent_id_to_name.get(agent_id, None)
                        break
        elif "channel" in event_data and "party" in event_data["channel"]:
            agent_id = event_data["channel"]["party"].get("agentId", None)
            if agent_id:
                agent = agent_id_to_name.get(agent_id, None)
        elif "user" in event_data:
            agent_id = event_data["user"].get("agentId", None)
            if agent_id:
                agent = agent_id_to_name.get(agent_id, None)

        if not agent:
            logger.warning(f"Could not determine agent name from Vonage payload. Agent ID: {agent_id}")
            return jsonify({"status": "skipped", "message": "Agent name not found, event skipped"}), 200

        event_data["agent"] = agent

        # Extract campaign phone number (only for interaction-based events)
        campaign_phone = None
        if "interaction" in event_data:
            campaign_phone = event_data["interaction"].get("fromAddress", None) or event_data["interaction"].get("toAddress", None)
        campaign = get_campaign_from_number(campaign_phone)

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
            elif presence_type == "away":
                agent_state = "Away"
            elif presence_type == "extended_away":
                agent_state = "Extended Away"
            elif presence_type == "team_meeting":
                agent_state = "Team Meeting"

            # Update the agent's presence state in memory
            if agent_state:
                agent_presence_states[agent] = (agent_state, timestamp)
                logger.info(f"Updated presence state for {agent}: {agent_state} at {timestamp.astimezone(ET).isoformat()}")

        # If not a presence change, check for specific channel events
        if not agent_state:
            if event_type == "channel.connectionfailed.v1":
                agent_state = "Device Busy"
            elif event_type == "channel.ended.v1":
                agent_state = "Logged Out"
            elif event_type == "channel.held.v1":
                agent_state = "Break"
            elif event_type == "channel.interrupted.v1":
                agent_state = "Break"
            elif event_type == "channel.parked.v1":
                agent_state = "Away"
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

        # Calculate duration based on the last presence state change
        duration_ms = get_event_duration(agent, timestamp, agent_state)
        duration_min = parse_duration(duration_ms)

        if event_type in ["channel.ended.v1", "channel.disconnected.v1", "channel.activityrecord.v0", "interaction.detailrecord.v0"]:
            logger.info(f"Skipping notification for event type: {event_type}")
            return jsonify({"status": "skipped", "message": f"Notifications disabled for {event_type}"}), 200

        is_in_shift = is_within_shift(agent, timestamp)
        logger.info(f"Event: {event_type}, Agent: {agent}, Agent State: {agent_state}, Duration: {duration_min} min, In Shift: {is_in_shift}, Campaign: {campaign}, Interaction ID: {interaction_id}")

        # Deduplicate alerts
        alert_key = f"{agent}:{agent_state}"
        if alert_key in last_alerts:
            last_alert_time = last_alerts[alert_key]
            time_since_last_alert = (timestamp - last_alert_time).total_seconds() / 60  # in minutes
            if time_since_last_alert < 5:  # Avoid sending the same alert within 5 minutes
                logger.info(f"Skipping duplicate alert for {agent}: {agent_state} (last sent {time_since_last_alert:.2f} minutes ago)")
                return jsonify({"status": "skipped", "message": "Duplicate alert skipped"}), 200

        if should_trigger_alert(agent_state, duration_min, is_in_shift, event_data):
            agent_state = event_data.get("alert_agent_state", agent_state)
            duration_min = event_data.get("alert_duration_min", duration_min)
            emoji = get_emoji_for_event(agent_state)
            team = agent_teams.get(agent, "Unknown Team")
            vonage_link = "https://nam.newvoicemedia.com/CallCentre/portal/interactionsearch"
            states_without_interaction = ["Idle", "Idle (Outbound)", "Device Busy", "Device Unreachable", "Fault", "Away", "Break", "Comfort Break", "Extended Away", "In Meeting", "Lunch", "Paperwork", "Team Meeting", "Training", "Logged Out"]

            # Log the alert to the weekly tab
            log_to_followups(agent, timestamp, duration_min, interaction_id, agent_state, campaign)

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
                blocks = [
                    {"type": "section", "text": {"type": "mrkdwn", "text": f"{emoji} *{agent_state} Alert*\nAgent: {agent}\nTeam: {team}\nDuration: {duration_min:.2f} min\nCampaign: {campaign}\nInteraction ID: {interaction_id}"}},
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
        return jsonify({"status": "error", "message": str(e)}), 200  # Return 200 to Vonage to prevent it from stopping event delivery

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
            # Post error to bot error channel
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
                blocks = [
                    {"type": "section", "text": {"type": "mrkdwn", "text": f"🔍 @{user} is investigating this {agent_state} alert for {agent}."}},
                    {"type": "actions", "elements": [
                        {"type": "button", "text": {"type": "plain_text", "text": "📝 Follow-Up"}, "value": f"followup|{agent}|{campaign}|{agent_state}|{duration_min}", "action_id": "open_followup"}
                    ]}
                ]
                response = session.post(response_url, json={"replace_original": True, "blocks": blocks})
                logger.info(f"Updated Slack message with Follow-Up button: {response.status_code} - {response.text}")

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
                    approval_decision="Assigned"
                )
                logger.info(f"Logged 'Assigned to Me' action for {agent}: {agent_state}")

            elif action_id == "approve_event":
                value = payload["actions"][0]["value"]
                _, agent, campaign, agent_state = value.split("|")
                blocks = [
                    {"type": "section", "text": {"type": "mrkdwn", "text": f"✅ *{agent_state} Approved*\nAgent: {agent}\nApproved by: @{user}\nInteraction ID: {campaign}"}}
                ]
                session.post(response_url, json={"replace_original": True, "blocks": blocks})

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
                    approved_by=user
                )
                logger.info(f"Logged approval for {agent}: {agent_state}")

            elif action_id == "not_approve_event":
                value = payload["actions"][0]["value"]
                _, agent, campaign, agent_state = value.split("|")
                blocks = [
                    {"type": "section", "text": {"type": "mrkdwn", "text": f"🔍 @{user} is investigating this {agent_state} alert for {agent}."}},
                    {"type": "actions", "elements": [
                        {"type": "button", "text": {"type": "plain_text", "text": "📝 Follow-Up"}, "value": f"followup|{agent}|{campaign}|{agent_state}|0", "action_id": "open_followup"}
                    ]}
                ]
                session.post(response_url, json={"replace_original": True, "blocks": blocks})

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
                    approved_by=user
                )
                logger.info(f"Logged non-approval for {agent}: {agent_state}")

            elif action_id == "copy_interaction_id":
                value = payload["actions"][0]["value"]
                blocks = [
                    {"type": "section", "text": {"type": "mrkdwn", "text": f"📋 Interaction ID `{value}` - Please copy it manually from here."}}
                ]
                session.post(response_url, json={"replace_original": False, "blocks": blocks})
                logger.info(f"User {user} requested to copy Interaction ID: {value}")

            elif action_id == "open_followup":
                logger.info(f"Handling open_followup action for user: {user} at {datetime.utcnow().replace(tzinfo=pytz.UTC).astimezone(ET).isoformat()}")
                value = payload["actions"][0]["value"]
                logger.debug(f"Button value: {value}")
                _, agent, campaign, agent_state, duration_min = value.split("|")
                duration_min = float(duration_min)
                trigger_id = payload["trigger_id"]
                logger.debug(f"Trigger ID: {trigger_id}")
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
                        "private_metadata": json.dumps({"agent": agent, "interaction_id": campaign, "agent_state": agent_state, "duration_min": duration_min, "user": user})
                    }
                }
                logger.info(f"Sending views.open request to Slack with modal: {json.dumps(modal, indent=2)}")
                # Retry logic for views.open
                for attempt in range(3):
                    try:
                        response = session.post("https://slack.com/api/views.open", headers=headers, json=modal)
                        logger.info(f"Slack API response status: {response.status_code}")
                        logger.info(f"Slack API response: {response.text}")
                        if response.status_code == 429:  # Rate-limited
                            retry_after = int(response.headers.get("Retry-After", 1))
                            logger.warning(f"Rate-limited by Slack, retrying after {retry_after} seconds")
                            time.sleep(retry_after)
                            continue
                        if response.status_code != 200 or not response.json().get("ok"):
                            logger.error(f"Failed to open follow-up modal: {response.text}")
                            # Post a fallback message to the user
                            fallback_blocks = [
                                {"type": "section", "text": {"type": "mrkdwn", "text": f"⚠️ Failed to open the follow-up modal for {agent}. Please try again."}}
                            ]
                            session.post(response_url, json={"replace_original": False, "blocks": fallback_blocks})
                            # Post error to bot error channel
                            error_blocks = [
                                {"type": "section", "text": {"type": "mrkdwn", "text": f"⚠️ Failed to open follow-up modal for {agent}: {response.text}"}}
                            ]
                            session.post("https://slack.com/api/chat.postMessage", headers=headers, json={"channel": BOT_ERROR_CHANNEL_ID, "blocks": error_blocks})
                            break
                        logger.info("Follow-up modal request sent to Slack successfully")
                        break
                    except Exception as e:
                        logger.error(f"ERROR: Failed to open follow-up modal: {e}")
                        if attempt < 2:
                            time.sleep(2 ** attempt)  # Exponential backoff
                            continue
                        # Post a fallback message to the user
                        fallback_blocks = [
                            {"type": "section", "text": {"type": "mrkdwn", "text": f"⚠️ Failed to open the follow-up modal for {agent}. Please try again."}}
                        ]
                        session.post(response_url, json={"replace_original": False, "blocks": fallback_blocks})
                        # Post error to bot error channel
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
                        notes=notes
                    )
                    logger.info(f"Logged follow-up submission for {agent}: {agent_state}")

                elif callback_id == "weekly_update_modal":
                    logger.info("Handling weekly_update_modal")
                    values = payload["view"]["state"]["values"]
                    metadata = json.loads(payload["view"]["private_metadata"])
                    channel_id = metadata["channel_id"]
                    start_date = datetime.strptime(values["start_date"]["start_date_picker"]["selected_date"], "%Y-%m-%d")
                    end_date = datetime.strptime(values["end_date"]["end_date_picker"]["selected_date"], "%Y-%m-%d")
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
                    else:
                        logger.warning(f"Could not log to Google Sheets for year {year} (weekly_update)")

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
                    post_slack_message(channel_id, summary_blocks)

                    # Post the success message
                    success_message = f"✅ Weekly update for {week} submitted successfully by {user}!"
                    success_blocks = [
                        {"type": "section", "text": {"type": "mrkdwn", "text": success_message}}
                    ]
                    post_slack_message(channel_id, success_blocks)

                return jsonify({"response_action": "clear"}), 200

        return "", 200
    except Exception as e:
        logger.error(f"ERROR in /slack/interactions: {e}")
        return "", 200  # Return 200 to Slack to acknowledge the interaction

# ========== MAIN ==========
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))