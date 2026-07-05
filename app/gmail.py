import os
import base64
import re
from datetime import datetime
from pathlib import Path
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from app.config import load_config

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
BASE_DIR = Path(__file__).parent.parent
CREDENTIALS_FILE = BASE_DIR / "credentials.json"
TOKEN_FILE = BASE_DIR / "token.json"
DATE_FORMAT = "%a, %d %b %Y %H:%M:%S %z"


def get_gmail_service():
    """Authenticate with Gmail and return an authorized API service client."""
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def get_label_id(service, label_name: str) -> str | None:
    """Return the Gmail label ID for the given label name, or None if not found."""
    labels = service.users().labels().list(userId="me").execute()
    for label in labels.get("labels", []):
        if label["name"].lower() == label_name.lower():
            return label["id"]
    return None


def extract_see_all_jobs_url(body: str) -> str | None:
    """Extract the LinkedIn 'See all jobs' URL from an email body."""
    # LinkedIn "See all jobs" links are long tracking URLs — grab the href
    pattern = r'href="(https://www\.linkedin\.com/comm/jobs/[^"]+)"[^>]*>\s*See all jobs'
    match = re.search(pattern, body, re.IGNORECASE)
    if match:
        return match.group(1)
    # Fallback: any LinkedIn jobs search URL in the body
    pattern = r'(https://www\.linkedin\.com/comm/jobs/search[^">\s]+)'
    match = re.search(pattern, body, re.IGNORECASE)
    return match.group(1) if match else None


def get_job_alert_emails(max_results: int = 50) -> list[dict]:
    """Fetch unread LinkedIn job alert emails and return their metadata and job URLs.

    The label to pull from comes from profiles/config.toml ([gmail] label).
    """
    label_name = load_config().gmail_label
    service = get_gmail_service()
    label_id = get_label_id(service, label_name)
    if not label_id:
        raise ValueError(f"Gmail label '{label_name}' not found")

    messages = service.users().messages().list(
        userId="me", labelIds=[label_id, "UNREAD"], maxResults=max_results
    ).execute().get("messages", [])

    results = []
    for msg in messages:
        full = service.users().messages().get(
            userId="me", id=msg["id"], format="full"
        ).execute()

        subject = next(
            (h["value"] for h in full["payload"]["headers"] if h["name"] == "Subject"),
            "No subject"
        )
        date = next(
            (h["value"] for h in full["payload"]["headers"] if h["name"] == "Date"),
            ""
        )

        body = _extract_body(full["payload"])
        url = extract_see_all_jobs_url(body)

        results.append({
            "message_id": msg["id"],
            "subject": subject,
            "date": date,
            "see_all_jobs_url": url,
        })

    if not results:
        return []

    results.sort(key=lambda x: datetime.strptime(x["date"].split(" (")[0], DATE_FORMAT))
    return results[:1]


def mark_as_read(service, message_id: str):
    """Remove the UNREAD label from a Gmail message."""
    service.users().messages().modify(
        userId="me",
        id=message_id,
        body={"removeLabelIds": ["UNREAD"]}
    ).execute()


def mark_email_read(message_id: str) -> None:
    """Mark a single message read, building its own Gmail service.

    Called by the runner after an email's jobs are written to the database, so an
    email is only cleared from the unread queue once we're done processing it.
    """
    mark_as_read(get_gmail_service(), message_id)


def _extract_body(payload: dict) -> str:
    """Recursively extract decoded text content from a Gmail message payload."""
    if "parts" in payload:
        for part in payload["parts"]:
            body = _extract_body(part)
            if body:
                return body
    data = payload.get("body", {}).get("data")
    if data:
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
    return ""
