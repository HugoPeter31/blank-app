"""
HSG Reporting Tool (Streamlit)
Developed by: Arthur Lavric & Fabio Patierno

Purpose:
- Allow HSG community members to submit facility-related issues via a simple UI.
- Store submissions in a database.
- Provide an admin-only page to update issue statuses (e.g. Pending, In Progress, Resolved)
  and notify users when resolved.

Note:
Access to the administrative “Overwrite Status” page is password-protected.
For evaluation purposes, the password is:
-> PleaseOpen!
"""

from __future__ import annotations

import logging  # Added for professional error logging (instead of exposing raw errors to users)
import re  # for validation
import sqlite3  # for database
from dataclasses import dataclass
from datetime import datetime  # for the timestamps
from email.message import EmailMessage
from typing import Iterable

import matplotlib.dates as mdates  # for charts
import matplotlib.pyplot as plt
import pandas as pd  # for tables
import pytz  # for right time zone
import smtplib
import streamlit as st


# ----------------------------
# Configuration / Constants
# ----------------------------
APP_TZ = pytz.timezone("Europe/Zurich")
DB_PATH = "hsg_reporting.db"
LOGO_PATH = "HSG-logo-new.png"

ISSUE_TYPES = [
    "Lighting issues",
    "Sanitary problems",
    "Heating, ventilation or air conditioning issues",
    "Cleaning needs due to heavy soiling",
    "Network/internet problems",
    "Issues with/lack of IT equipment",
]

IMPORTANCE_LEVELS = ["Low", "Medium", "High"]
STATUS_LEVELS = ["Pending", "In Progress", "Resolved"]

# Compile regex once (readability + small performance benefit)
EMAIL_PATTERN = re.compile(r"^[\w.]+@(student\.)?unisg\.ch$")
ROOM_PATTERN = re.compile(r"^[A-Z] \d{2}-\d{3}$")


# ----------------------------
# Logging
# ----------------------------
# Logs help debugging without leaking technical details to end users.
logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


# ----------------------------
# Data model
# ----------------------------
@dataclass(frozen=True)
class Submission:
    """Immutable representation of a submission input."""
    name: str
    hsg_email: str
    issue_type: str
    room_number: str
    importance: str
    user_comment: str


# ----------------------------
# Secrets (Streamlit Cloud → Settings → Secrets)
# ----------------------------
def get_secret(key: str, default: str | None = None) -> str:
    """
    Read a Streamlit secret key. If missing, stop early with a helpful error.

    Why: Missing secrets cause confusing runtime failures (e.g., SMTP login errors).
    """
    if key in st.secrets:
        return str(st.secrets[key])
    if default is not None:
        return default
    st.error(f"Missing Streamlit secret: {key}")
    st.stop()


SMTP_SERVER = get_secret("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(get_secret("SMTP_PORT", "587"))
SMTP_USERNAME = get_secret("SMTP_USERNAME")
SMTP_PASSWORD = get_secret("SMTP_PASSWORD")
FROM_EMAIL = get_secret("FROM_EMAIL", SMTP_USERNAME)
ADMIN_INBOX = get_secret("ADMIN_INBOX", FROM_EMAIL)

# Admin page password should be provided via Streamlit secrets (no hardcoding).
ADMIN_PASSWORD = get_secret("ADMIN_PASSWORD")

# Optional debug flag (recommended): set DEBUG = "1" in Streamlit Secrets to show technical email errors.
DEBUG = get_secret("DEBUG", "0") == "1"


# ----------------------------
# Time helpers
# ----------------------------
def now_zurich_str() -> str:
    """
    Return an unambiguous timestamp (ISO 8601) including timezone offset.

    Why: Avoids confusion and parsing errors across environments and daylight saving changes.
    """
    return datetime.now(APP_TZ).isoformat(timespec="seconds")


# ----------------------------
# Validation: Check whether the specified email address complies with the requirements of an official HSG mail address
# ----------------------------
def valid_email(hsg_email: str) -> bool:
    """Return True if the email is an official HSG address."""
    return bool(EMAIL_PATTERN.fullmatch(hsg_email.strip()))


def valid_room_number(room_number: str) -> bool:
    """Return True if the room number matches the HSG format, e.g. 'A 09-001'."""
    return bool(ROOM_PATTERN.fullmatch(room_number.strip()))


def validate_submission_input(sub: Submission) -> list[str]:
    """
    Validate user inputs and return human-readable error messages.

    Why: The UI should guide the user to correct inputs rather than crashing with exceptions.
    """
    errors: list[str] = []

    if not sub.name.strip():
        errors.append("Name is required.")

    if not sub.hsg_email.strip():
        errors.append("HSG Email Address is required.")
    elif not valid_email(sub.hsg_email):
        errors.append("Invalid mail address. Use …@unisg.ch or …@student.unisg.ch.")

    if not sub.room_number.strip():
        errors.append("Room Number is required.")
    elif not valid_room_number(sub.room_number):
        errors.append("Invalid room number format. Example: 'A 09-001'.")

    if sub.issue_type not in ISSUE_TYPES:
        errors.append("Invalid issue type selection.")

    if sub.importance not in IMPORTANCE_LEVELS:
        errors.append("Invalid importance selection.")

    if not sub.user_comment.strip():
        errors.append("Problem Description is required.")

    return errors


def validate_admin_email(email: str) -> list[str]:
    """Validation for admin operations: keep rules consistent across the app."""
    if not email.strip():
        return ["Email address is required."]
    if not valid_email(email):
        return ["Please provide a valid HSG email address (…@unisg.ch or …@student.unisg.ch)."]
    return []


# ----------------------------
# Database
# ----------------------------
@st.cache_resource
def get_connection() -> sqlite3.Connection:
    """
    Create and cache a SQLite connection for Streamlit.

    Why: Streamlit reruns scripts often; caching prevents re-opening connections unnecessarily.
    """
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def init_db(con: sqlite3.Connection) -> None:
    """Create the required table if it does not exist."""
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            hsg_email TEXT NOT NULL,
            issue_type TEXT NOT NULL,
            room_number TEXT NOT NULL,
            importance TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Pending',
            user_comment TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    con.commit()


def migrate_db(con: sqlite3.Connection) -> None:
    """
    Simple schema migration for older DB files.

    Why: If an old DB exists (e.g., from previous versions), charts/reads should not crash.
    This keeps the tool robust without requiring manual DB deletion.
    """
    cols = {row[1] for row in con.execute("PRAGMA table_info(submissions)").fetchall()}

    # Add missing timestamp columns if needed (keeps old databases compatible)
    if "created_at" not in cols:
        con.execute("ALTER TABLE submissions ADD COLUMN created_at TEXT")
        con.execute("UPDATE submissions SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL")

    if "updated_at" not in cols:
        con.execute("ALTER TABLE submissions ADD COLUMN updated_at TEXT")
        con.execute("UPDATE submissions SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL")

    con.commit()


def fetch_submissions(con: sqlite3.Connection) -> pd.DataFrame:
    """Fetch all submissions as a DataFrame (single responsibility)."""
    return pd.read_sql("SELECT * FROM submissions", con)


def insert_submission(con: sqlite3.Connection, sub: Submission) -> None:
    """Insert a validated submission into the database."""
    created_at = now_zurich_str()
    updated_at =_
