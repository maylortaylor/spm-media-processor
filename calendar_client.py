import os
from pathlib import Path
from datetime import date, timedelta

PROJECT_ROOT = Path(__file__).parent
CREDS_FILE = PROJECT_ROOT / "credentials.json"
TOKEN_FILE = PROJECT_ROOT / "token.json"

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


def authenticate():
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
        os.chmod(TOKEN_FILE, 0o600)

    return creds


def get_events_on_date(calendar_id: str, target_date: date) -> list[dict]:
    """Return events on target_date from the calendar."""
    if not CREDS_FILE.exists():
        return []

    try:
        from googleapiclient.discovery import build

        creds = authenticate()
        service = build("calendar", "v3", credentials=creds)

        time_min = f"{target_date.isoformat()}T00:00:00Z"
        time_max = f"{(target_date + timedelta(days=1)).isoformat()}T00:00:00Z"

        result = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )

        events = []
        for item in result.get("items", []):
            events.append(
                {
                    "summary": item.get("summary", ""),
                    "description": item.get("description", ""),
                    "start": item.get("start", {}).get("dateTime", item.get("start", {}).get("date", "")),
                    "end": item.get("end", {}).get("dateTime", item.get("end", {}).get("date", "")),
                }
            )
        return events
    except Exception as e:
        print(f"  Calendar lookup failed: {e}")
        return []
