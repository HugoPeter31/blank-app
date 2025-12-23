from __future__ import annotations

# ============================================================================
# HSG REPORTING TOOL
# ============================================================================
# Application: Streamlit-based reporting system for University of St. Gallen
# Purpose: Facility issue reporting, asset booking, and tracking system
# Developed by: Arthur Lavric & Fabio Patierno
# 
# Key Features:
# 1. Issue reporting form with email confirmation and SLA tracking
# 2. Dashboard with data visualization and CSV export
# 3. Admin panel with password protection and status management
# 4. Asset booking system with intelligent room-asset linking
# 5. Asset tracking with location-based management
# 
# Security Notes:
# - Admin access protected via Streamlit secrets (ADMIN_PASSWORD)
# - Email functionality requires SMTP secrets configuration
# - All database operations use parameterized queries to prevent SQL injection
# ============================================================================

# ============================================================================
# IMPORTS
# ============================================================================
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Iterable

import pandas as pd
import pytz
import smtplib
import streamlit as st


# ============================================================================
# CONFIGURATION & CONSTANTS
# ============================================================================
APP_TZ = pytz.timezone("Europe/Zurich")  # Zurich timezone for all timestamps
DB_PATH = "hsg_reporting.db"             # SQLite database file path
LOGO_PATH = "HSG-logo-new.png"           # University logo for branding

# HSG brand green color (official brand color for consistent UI)
# Source: HSG Brand Guidelines, ensures professional visual identity
HSG_GREEN = "#00802F"

# Predefined issue types - these represent common facility problems at HSG
# Keeping this list centralized makes maintenance easier
ISSUE_TYPES = [
    "Lighting issues",
    "Sanitary problems", 
    "Heating, ventilation or air conditioning issues",
    "Cleaning needs due to heavy soiling",
    "Network/internet problems",
    "Issues with/lack of IT equipment",
]

# Priority and status levels - these drive SLA calculations and workflow
IMPORTANCE_LEVELS = ["Low", "Medium", "High"]
STATUS_LEVELS = ["Pending", "In Progress", "Resolved"]

# Service Level Agreement (SLA) definitions in hours
# These define expected resolution times based on issue priority
SLA_HOURS_BY_IMPORTANCE: dict[str, int] = {
    "High": 24,    # Critical issues: 24-hour resolution target
    "Medium": 72,  # Important issues: 72-hour resolution target  
    "Low": 120,    # Minor issues: 120-hour (5-day) resolution target
}

# Validation patterns for user inputs
EMAIL_PATTERN = re.compile(r"^[\w.]+@(student\.)?unisg\.ch$")  # Only HSG emails allowed
ROOM_PATTERN = re.compile(r"^[A-Z]\s?\d{2}-\d{3}$")           # Standard HSG room format

# Location mapping for asset tracking
# In production, this could be replaced with a database table or API integration
LOCATIONS = {
    "R_A_09001": {"label": "Room A 09-001", "x": 10, "y": 20},
    "H_A_09001": {"label": "Hallway near Room A 09-001", "x": 15, "y": 25},
    "R_B_10012": {"label": "Room B 10-012", "x": 40, "y": 60},
    "H_B_10012": {"label": "Hallway near Room B 10-012", "x": 45, "y": 65},
}


# ============================================================================
# LOGGING CONFIGURATION
# ============================================================================
# Configure application logging for debugging and monitoring
# In production, this could be extended to file or cloud logging
logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


# ============================================================================
# DATA MODEL
# ============================================================================
@dataclass(frozen=True)
class Submission:
    """Data model representing an issue submission.
    
    Attributes:
        name: Reporter's full name
        hsg_email: Validated HSG email address
        issue_type: Type of issue from predefined list
        room_number: HSG room number in standardized format
        importance: Priority level (Low/Medium/High)
        user_comment: Detailed problem description
    """
    name: str
    hsg_email: str
    issue_type: str
    room_number: str
    importance: str
    user_comment: str


# ============================================================================
# SECRETS MANAGEMENT (Streamlit Cloud Secrets)
# ============================================================================
def get_secret(key: str, default: str | None = None) -> str:
    """Safely retrieve a secret from Streamlit secrets configuration.
    
    Args:
        key: The secret key to retrieve
        default: Optional default value if key doesn't exist
        
    Returns:
        The secret value as a string
        
    Raises:
        SystemExit: If secret is required but missing
    """
    if key in st.secrets:
        return str(st.secrets[key])
    if default is not None:
        return default
    # Critical failure: missing required secret
    st.error(f"Missing Streamlit secret: {key}")
    st.stop()


# Email configuration for notifications
SMTP_SERVER = get_secret("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(get_secret("SMTP_PORT", "587"))
SMTP_USERNAME = get_secret("SMTP_USERNAME")
SMTP_PASSWORD = get_secret("SMTP_PASSWORD")
FROM_EMAIL = get_secret("FROM_EMAIL", SMTP_USERNAME)
ADMIN_INBOX = get_secret("ADMIN_INBOX", FROM_EMAIL)

# Admin security
ADMIN_PASSWORD = get_secret("ADMIN_PASSWORD")

# Debug mode - enables detailed error messages
DEBUG = get_secret("DEBUG", "0") == "1"

# Team assignment configuration
ASSIGNEES_RAW = get_secret("ASSIGNEES", "Facility Team")
ASSIGNEES = [a.strip() for a in ASSIGNEES_RAW.split(",") if a.strip()]

# Automated reporting configuration
AUTO_WEEKLY_REPORT = get_secret("AUTO_WEEKLY_REPORT", "0") == "1"
REPORT_WEEKDAY = int(get_secret("REPORT_WEEKDAY", "0"))  # 0=Monday, 6=Sunday
REPORT_HOUR = int(get_secret("REPORT_HOUR", "7"))        # Hour in 24h format


# ============================================================================
# TIME HELPER FUNCTIONS
# ============================================================================
def now_zurich() -> datetime:
    """Get current time in Zurich timezone.
    
    Returns:
        Timezone-aware datetime object for Zurich
    """
    return datetime.now(APP_TZ)


def now_zurich_str() -> str:
    """Get current Zurich time as ISO 8601 string.
    
    Returns:
        ISO formatted timestamp with timezone (e.g., "2024-01-15T14:30:00+01:00")
    """
    return now_zurich().isoformat(timespec="seconds")


def iso_to_dt(value: str) -> datetime | None:
    """Safely convert ISO string to datetime object.
    
    Args:
        value: ISO 8601 formatted datetime string
        
    Returns:
        datetime object or None if conversion fails
    """
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        logger.warning(f"Failed to parse datetime from: {value}")
        return None


def expected_resolution_dt(created_at_iso: str, importance: str) -> datetime | None:
    """Calculate expected resolution time based on SLA.
    
    Args:
        created_at_iso: Issue creation timestamp
        importance: Priority level (High/Medium/Low)
        
    Returns:
        Expected resolution datetime or None if inputs are invalid
    """
    created_dt = iso_to_dt(created_at_iso)
    sla_hours = SLA_HOURS_BY_IMPORTANCE.get(importance)
    
    if created_dt is None or sla_hours is None:
        return None
        
    return created_dt + timedelta(hours=int(sla_hours))


def is_room_location(location_id: str) -> bool:
    """Check if a location ID represents a room.
    
    Args:
        location_id: Location identifier
        
    Returns:
        True if location is a room (starts with "R_"), False otherwise
    """
    return str(location_id).startswith("R_")


# ============================================================================
# VALIDATION FUNCTIONS
# ============================================================================
def valid_email(hsg_email: str) -> bool:
    """Validate HSG email address format.
    
    Args:
        hsg_email: Email address to validate
        
    Returns:
        True if email matches HSG pattern (@unisg.ch or @student.unisg.ch)
    """
    return bool(EMAIL_PATTERN.fullmatch(hsg_email.strip()))


def valid_room_number(room_number: str) -> bool:
    """Validate HSG room number format.
    
    Args:
        room_number: Room number to validate
        
    Returns:
        True if room number matches pattern (e.g., "A 09-001")
    """
    return bool(ROOM_PATTERN.fullmatch(room_number.strip()))


def normalize_room(room_number: str) -> str:
    """Normalize room number to canonical format.
    
    Converts various inputs like "A09-001" or "A  09-001" to "A 09-001".
    
    Args:
        room_number: Raw room number input
        
    Returns:
        Standardized room number string
    """
    raw = room_number.strip().upper()
    # Insert space after letter if missing (A09-001 → A 09-001)
    raw = re.sub(r"^([A-Z])(\d{2}-\d{3})$", r"\1 \2", raw)
    # Collapse multiple spaces to single space
    raw = re.sub(r"\s+", " ", raw)
    return raw


def validate_submission_input(sub: Submission) -> list[str]:
    """Validate all inputs for issue submission.
    
    Args:
        sub: Submission data object
        
    Returns:
        List of error messages, empty if validation passes
    """
    errors: list[str] = []

    if not sub.name.strip():
        errors.append("Name is required.")

    if not sub.hsg_email.strip():
        errors.append("HSG email address is required.")
    elif not valid_email(sub.hsg_email):
        errors.append("Invalid email address. Use …@unisg.ch or …@student.unisg.ch.")

    if not sub.room_number.strip():
        errors.append("Room number is required.")
    elif not valid_room_number(sub.room_number):
        errors.append("Invalid room number format. Example: 'A 09-001'.")

    if sub.issue_type not in ISSUE_TYPES:
        errors.append("Invalid issue type selection.")

    if sub.importance not in IMPORTANCE_LEVELS:
        errors.append("Invalid importance selection.")

    if not sub.user_comment.strip():
        errors.append("Problem description is required.")

    return errors


def validate_admin_email(email: str) -> list[str]:
    """Validate email for admin-triggered notifications.
    
    Args:
        email: Email address to validate
        
    Returns:
        List of error messages, empty if validation passes
    """
    if not email.strip():
        return ["Email address is required."]
    if not valid_email(email):
        return ["Please provide a valid HSG email address (…@unisg.ch or …@student.unisg.ch)."]
    return []


# ============================================================================
# DATABASE MANAGEMENT
# ============================================================================
@st.cache_resource
def get_connection() -> sqlite3.Connection:
    """Create and cache SQLite database connection.
    
    Caching prevents opening new connections on every Streamlit rerun,
    improving performance and preventing connection exhaustion.
    
    Returns:
        SQLite connection object
    """
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def init_db(con: sqlite3.Connection) -> None:
    """Initialize core database tables for issue reporting.
    
    Creates tables if they don't exist. This is idempotent and safe to run
    multiple times.
    
    Args:
        con: Active database connection
    """
    # Main submissions table - stores all reported issues
    con.execute("""
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
            updated_at TEXT NOT NULL,
            assigned_to TEXT,
            resolved_at TEXT
        )
    """)
    
    # Audit log for status changes - provides traceability
    con.execute("""
        CREATE TABLE IF NOT EXISTS status_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            submission_id INTEGER NOT NULL,
            old_status TEXT NOT NULL,
            new_status TEXT NOT NULL,
            changed_at TEXT NOT NULL,
            FOREIGN KEY (submission_id) REFERENCES submissions(id)
        )
    """)
    
    # Report sending log - prevents duplicate automated reports
    con.execute("""
        CREATE TABLE IF NOT EXISTS report_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_type TEXT NOT NULL,
            sent_at TEXT NOT NULL
        )
    """)
    
    con.commit()


def init_booking_table(con: sqlite3.Connection) -> None:
    """Initialize booking system tables.
    
    Separate from issue reporting to maintain modularity.
    
    Args:
        con: Active database connection
    """
    con.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            booking_id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id TEXT NOT NULL,
            user_name TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    con.commit()


def init_assets_table(con: sqlite3.Connection) -> None:
    """Initialize assets table for both booking and tracking.
    
    Args:
        con: Active database connection
    """
    con.execute("""
        CREATE TABLE IF NOT EXISTS assets (
            asset_id TEXT PRIMARY KEY,
            asset_name TEXT NOT NULL,
            asset_type TEXT NOT NULL,
            location_id TEXT NOT NULL,
            status TEXT NOT NULL
        )
    """)
    con.commit()


def migrate_db(con: sqlite3.Connection) -> None:
    """Apply schema migrations for backward compatibility.
    
    Handles database upgrades by adding missing columns to existing tables.
    This ensures the app works with older database versions.
    
    Args:
        con: Active database connection
    """
    # Get existing columns in submissions table
    cols = {row[1] for row in con.execute("PRAGMA table_info(submissions)").fetchall()}
    
    # Add missing columns with safe defaults
    if "created_at" not in cols:
        con.execute("ALTER TABLE submissions ADD COLUMN created_at TEXT")
        con.execute("UPDATE submissions SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL")
    
    if "updated_at" not in cols:
        con.execute("ALTER TABLE submissions ADD COLUMN updated_at TEXT")
        con.execute("UPDATE submissions SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL")
    
    if "assigned_to" not in cols:
        con.execute("ALTER TABLE submissions ADD COLUMN assigned_to TEXT")
    
    if "resolved_at" not in cols:
        con.execute("ALTER TABLE submissions ADD COLUMN resolved_at TEXT")
    
    # Ensure audit tables exist
    con.execute("""
        CREATE TABLE IF NOT EXISTS status_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            submission_id INTEGER NOT NULL,
            old_status TEXT NOT NULL,
            new_status TEXT NOT NULL,
            changed_at TEXT NOT NULL,
            FOREIGN KEY (submission_id) REFERENCES submissions(id)
        )
    """)
    
    con.execute("""
        CREATE TABLE IF NOT EXISTS report_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_type TEXT NOT NULL,
            sent_at TEXT NOT NULL
        )
    """)
    
    con.commit()


def seed_assets(con: sqlite3.Connection) -> None:
    """Populate database with initial demo assets.
    
    Only inserts assets that don't already exist (idempotent).
    
    Args:
        con: Active database connection
    """
    # Demo data representing typical HSG assets
    assets = [
        ("ROOM_A", "Study Room A", "Room", "R_A_09001", "available"),
        ("ROOM_B", "Study Room B", "Room", "R_B_10012", "available"),
        ("MEETING_1", "Meeting Room 1", "Room", "R_B_10012", "available"),
        ("PROJECTOR_1", "Portable Projector 1", "Equipment", "H_B_10012", "available"),
        ("CHAIR_H1", "Hallway Chair 1", "Chair", "H_A_09001", "available"),
        ("CHAIR_H2", "Hallway Chair 2", "Chair", "H_A_09001", "available"),
    ]
    
    for asset in assets:
        con.execute(
            """
            INSERT OR IGNORE INTO assets
            (asset_id, asset_name, asset_type, location_id, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            asset,
        )
    con.commit()


def fetch_submissions(con: sqlite3.Connection) -> pd.DataFrame:
    """Retrieve all issue submissions from database.
    
    Args:
        con: Active database connection
        
    Returns:
        DataFrame containing all submissions
    """
    return pd.read_sql("SELECT * FROM submissions", con)


def fetch_status_log(con: sqlite3.Connection) -> pd.DataFrame:
    """Retrieve status change audit log.
    
    Args:
        con: Active database connection
        
    Returns:
        DataFrame of status changes ordered by most recent
    """
    return pd.read_sql(
        """
        SELECT submission_id, old_status, new_status, changed_at
        FROM status_log
        ORDER BY changed_at DESC
        """,
        con,
    )


def fetch_report_log(con: sqlite3.Connection, report_type: str) -> pd.DataFrame:
    """Retrieve report sending history.
    
    Args:
        con: Active database connection
        report_type: Type of report to filter by
        
    Returns:
        DataFrame of report logs for specified type
    """
    return pd.read_sql(
        """
        SELECT report_type, sent_at
        FROM report_log
        WHERE report_type = ?
        ORDER BY sent_at DESC
        """,
        con,
        params=(report_type,),
    )


def fetch_assets(con: sqlite3.Connection) -> pd.DataFrame:
    """Retrieve all assets from database.
    
    Args:
        con: Active database connection
        
    Returns:
        DataFrame containing all assets
    """
    return pd.read_sql(
        """
        SELECT asset_id, asset_name, asset_type, location_id, status
        FROM assets
        ORDER BY asset_type, asset_name
        """,
        con,
    )


def fetch_assets_in_room(con: sqlite3.Connection, room_location_id: str) -> list[str]:
    """Retrieve asset IDs located inside a specific room.
    
    Used for intelligent booking: when a room is booked, all assets
    inside it are automatically marked as booked.
    
    Args:
        con: Active database connection
        room_location_id: Location ID of the room
        
    Returns:
        List of asset IDs located in the room (excluding the room itself)
    """
    rows = con.execute(
        """
        SELECT asset_id
        FROM assets
        WHERE location_id = ?
          AND asset_type != 'Room'
        """,
        (room_location_id,),
    ).fetchall()
    return [r[0] for r in rows]


def mark_report_sent(con: sqlite3.Connection, report_type: str) -> None:
    """Log that a report has been sent.
    
    Prevents duplicate automated reports by tracking when they were last sent.
    
    Args:
        con: Active database connection
        report_type: Type of report that was sent
    """
    con.execute(
        "INSERT INTO report_log (report_type, sent_at) VALUES (?, ?)",
        (report_type, now_zurich_str()),
    )
    con.commit()


# ============================================================================
# BOOKING SYSTEM FUNCTIONS
# ============================================================================
def sync_asset_statuses_from_bookings(con: sqlite3.Connection) -> None:
    """Update asset statuses based on active bookings.
    
    This is a core feature: when a room is booked, all assets inside
    that room are automatically marked as booked. This prevents double-booking
    and ensures consistency.
    
    Args:
        con: Active database connection
    """
    now_iso = now_zurich().isoformat(timespec="seconds")
    
    # Reset all assets to available
    con.execute("UPDATE assets SET status = 'available'")
    con.commit()
    
    # Find currently active bookings
    active = pd.read_sql(
        """
        SELECT b.asset_id, a.asset_type, a.location_id
        FROM bookings b
        JOIN assets a ON a.asset_id = b.asset_id
        WHERE b.start_time <= ? AND b.end_time > ?
        """,
        con,
        params=(now_iso, now_iso),
    )
    
    for _, row in active.iterrows():
        asset_id = row["asset_id"]
        asset_type = row["asset_type"]
        location_id = row["location_id"]
        
        # Mark the booked asset itself
        con.execute(
            "UPDATE assets SET status = 'booked' WHERE asset_id = ?",
            (asset_id,),
        )
        
        # If booking a room, automatically book all assets inside it
        if asset_type == "Room" and is_room_location(location_id):
            inside_assets = fetch_assets_in_room(con, location_id)
            for aid in inside_assets:
                con.execute(
                    "UPDATE assets SET status = 'booked' WHERE asset_id = ?",
                    (aid,),
                )
    
    con.commit()


def is_asset_available(con: sqlite3.Connection, asset_id: str, start_time: datetime, end_time: datetime) -> bool:
    """Check if an asset is available during a specified time period.
    
    Args:
        con: Active database connection
        asset_id: ID of asset to check
        start_time: Desired booking start time
        end_time: Desired booking end time
        
    Returns:
        True if asset is available (no overlapping bookings), False otherwise
    """
    query = """
        SELECT COUNT(*) FROM bookings
        WHERE asset_id = ?
          AND start_time < ?
          AND end_time > ?
    """
    count = con.execute(
        query,
        (asset_id, end_time.isoformat(timespec="seconds"), start_time.isoformat(timespec="seconds")),
    ).fetchone()[0]
    return count == 0


def fetch_future_bookings(con: sqlite3.Connection, asset_id: str) -> pd.DataFrame:
    """Retrieve upcoming bookings for a specific asset.
    
    Args:
        con: Active database connection
        asset_id: ID of asset to get bookings for
        
    Returns:
        DataFrame of future bookings ordered by start time
    """
    now_iso = now_zurich().isoformat()
    return pd.read_sql(
        """
        SELECT user_name, start_time, end_time
        FROM bookings
        WHERE asset_id = ?
          AND end_time >= ?
        ORDER BY start_time
        """,
        con,
        params=(asset_id, now_iso),
    )


def next_available_time(con: sqlite3.Connection, asset_id: str) -> datetime | None:
    """Find the next available time for a currently booked asset.
    
    Args:
        con: Active database connection
        asset_id: ID of asset to check
        
    Returns:
        Next available datetime if currently booked, None if available now
    """
    now_iso = now_zurich().isoformat(timespec="seconds")
    row = con.execute(
        """
        SELECT MIN(end_time)
        FROM bookings
        WHERE asset_id = ?
          AND end_time > ?
        """,
        (asset_id, now_iso),
    ).fetchone()
    
    if not row or not row[0]:
        return None
    return iso_to_dt(str(row[0]))


# ============================================================================
# ISSUE ADMINISTRATION FUNCTIONS
# ============================================================================
def update_issue_admin_fields(
    con: sqlite3.Connection,
    issue_id: int,
    new_status: str,
    assigned_to: str | None,
    old_status: str,
) -> None:
    """Update issue status and assignment with audit logging.
    
    Args:
        con: Active database connection
        issue_id: ID of issue to update
        new_status: New status to set
        assigned_to: Person assigned to the issue (None for unassigned)
        old_status: Previous status for audit logging
    """
    updated_at = now_zurich_str()
    set_resolved_at = (new_status == "Resolved")
    
    with con:
        # Update issue with new status and assignment
        con.execute(
            """
            UPDATE submissions
            SET status = ?,
                updated_at = ?,
                assigned_to = ?,
                resolved_at = CASE
                    WHEN ? = 1 AND (resolved_at IS NULL OR resolved_at = '') THEN ?
                    ELSE resolved_at
                END
            WHERE id = ?
            """,
            (
                new_status,
                updated_at,
                (assigned_to.strip() if assigned_to and assigned_to.strip() else None),
                1 if set_resolved_at else 0,
                updated_at,
                int(issue_id),
            ),
        )
        
        # Log status change for audit trail
        if new_status != old_status:
            con.execute(
                """
                INSERT INTO status_log (submission_id, old_status, new_status, changed_at)
                VALUES (?, ?, ?, ?)
                """,
                (int(issue_id), old_status, new_status, updated_at),
            )


def insert_submission(con: sqlite3.Connection, sub: Submission) -> None:
    """Insert a new issue submission into the database.
    
    Args:
        con: Active database connection
        sub: Validated submission object
    """
    created_at = now_zurich_str()
    updated_at = created_at
    
    with con:
        con.execute(
            """
            INSERT INTO submissions
            (name, hsg_email, issue_type, room_number, importance, status, 
             user_comment, created_at, updated_at, assigned_to, resolved_at)
            VALUES (?, ?, ?, ?, ?, 'Pending', ?, ?, ?, NULL, NULL)
            """,
            (
                sub.name.strip(),
                sub.hsg_email.strip().lower(),
                sub.issue_type,
                normalize_room(sub.room_number),
                sub.importance,
                sub.user_comment.strip(),
                created_at,
                updated_at,
            ),
        )


# ============================================================================
# EMAIL FUNCTIONS
# ============================================================================
def send_email(to_email: str, subject: str, body: str) -> tuple[bool, str]:
    """Send email with proper error handling.
    
    Args:
        to_email: Recipient email address
        subject: Email subject line
        body: Email body content
        
    Returns:
        Tuple of (success boolean, status message)
    """
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = FROM_EMAIL
    msg["To"] = to_email
    msg.set_content(body)
    
    # Always CC admin inbox for record keeping
    recipients = [to_email] + ([ADMIN_INBOX] if ADMIN_INBOX else [])
    
    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=10) as smtp:
            smtp.starttls()
            smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            smtp.send_message(msg, to_addrs=recipients)
        
        return True, "Email sent successfully."
    except Exception as exc:
        logger.exception("Email sending failed")
        # Return user-friendly error (debug details only in debug mode)
        if DEBUG:
            return False, f"Email could not be sent: {exc}"
        return False, "Email could not be sent due to a technical issue."


def send_admin_report_email(subject: str, body: str) -> tuple[bool, str]:
    """Send report email to admin inbox only.
    
    Args:
        subject: Email subject line
        body: Email body content
        
    Returns:
        Tuple of (success boolean, status message)
    """
    if not ADMIN_INBOX:
        return False, "ADMIN_INBOX is not configured."
    
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = FROM_EMAIL
    msg["To"] = ADMIN_INBOX
    msg.set_content(body)
    
    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=10) as smtp:
            smtp.starttls()
            smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            smtp.send_message(msg, to_addrs=[ADMIN_INBOX])
        return True, "Report email sent successfully."
    except Exception as exc:
        logger.exception("Report email sending failed")
        if DEBUG:
            return False, f"Report email could not be sent: {exc}"
        return False, "Report email could not be sent due to a technical issue."


def confirmation_email_text(recipient_name: str, importance: str) -> tuple[str, str]:
    """Generate confirmation email content for new issue submissions.
    
    Args:
        recipient_name: Name of the person who submitted the issue
        importance: Priority level of the issue
        
    Returns:
        Tuple of (subject, body) for the confirmation email
    """
    subject = "HSG Reporting Tool: Issue Received"
    sla_hours = SLA_HOURS_BY_IMPORTANCE.get(importance)
    
    sla_text = (
        f"Expected handling time (SLA): within {sla_hours} hours."
        if sla_hours is not None
        else "Expected handling time (SLA): n/a."
    )
    
    body = f"""Dear {recipient_name},

Thank you for contacting us regarding your concern. We hereby confirm that we have received your issue report and that it is currently under review by the responsible team.

{sla_text}

We will keep you informed about the progress and notify you once the matter has been resolved.

Kind regards,
HSG Service Team
"""
    return subject, body


def resolved_email_text(recipient_name: str) -> tuple[str, str]:
    """Generate resolution notification email content.
    
    Args:
        recipient_name: Name of the person who reported the issue
        
    Returns:
        Tuple of (subject, body) for the resolution email
    """
    subject = "HSG Reporting Tool: Issue Resolved"
    body = f"""Hello {recipient_name},

We are pleased to inform you that the issue you reported via the HSG Reporting Tool has been resolved.

Kind regards,
HSG Service Team
"""
    return subject, body


# ============================================================================
# REPORTING FUNCTIONS
# ============================================================================
def build_weekly_report(df_all: pd.DataFrame) -> tuple[str, str]:
    """Generate weekly summary report content.
    
    Args:
        df_all: DataFrame containing all submissions
        
    Returns:
        Tuple of (subject, body) for the weekly report email
    """
    now_dt = now_zurich()
    since_dt = now_dt - timedelta(days=7)
    
    df = df_all.copy()
    df["created_at_dt"] = pd.to_datetime(df["created_at"], errors="coerce")
    df["resolved_at_dt"] = pd.to_datetime(
        df.get("resolved_at", pd.Series([None] * len(df))), errors="coerce"
    )
    
    # Calculate metrics for the past 7 days
    new_last_7d = df[df["created_at_dt"] >= since_dt]
    resolved_last_7d = df[(df["resolved_at_dt"].notna()) & (df["resolved_at_dt"] >= since_dt)]
    open_issues = df[df["status"] != "Resolved"]
    
    subject = f"HSG Reporting Tool – Weekly Summary ({now_dt.strftime('%Y-%m-%d')})"
    body = (
        "Weekly summary (last 7 days):\n"
        f"- New issues: {len(new_last_7d)}\n"
        f"- Resolved issues: {len(resolved_last_7d)}\n"
        f"- Open issues (current): {len(open_issues)}\n\n"
        "Top issue types (open):\n"
    )
    
    # Add top issue types
    if not open_issues.empty:
        top_types = open_issues["issue_type"].value_counts().head(5)
        for issue_type, count in top_types.items():
            body += f"- {issue_type}: {count}\n"
    else:
        body += "- n/a\n"
    
    body += "\nThis email was generated by the HSG Reporting Tool."
    return subject, body


def send_weekly_report_if_due(con: sqlite3.Connection) -> None:
    """Check if weekly report is due and send it.
    
    Runs when app is opened; sends report only if:
    1. Auto-reporting is enabled
    2. Current day matches configured weekday
    3. Current hour matches configured hour
    4. Report hasn't been sent today already
    
    Args:
        con: Active database connection
    """
    if not AUTO_WEEKLY_REPORT:
        return
    
    now_dt = now_zurich()
    if now_dt.weekday() != REPORT_WEEKDAY or now_dt.hour != REPORT_HOUR:
        return
    
    # Check when report was last sent
    log_df = fetch_report_log(con, "weekly")
    if not log_df.empty:
        last_sent = iso_to_dt(str(log_df.iloc[0]["sent_at"]))
        if last_sent is not None and last_sent.date() == now_dt.date():
            return
    
    # Generate and send report
    df_all = fetch_submissions(con)
    subject, body = build_weekly_report(df_all)
    ok, _ = send_admin_report_email(subject, body)
    if ok:
        mark_report_sent(con, "weekly")


# ============================================================================
# UI HELPER FUNCTIONS
# ============================================================================
def apply_hsg_table_header_style() -> None:
    """Apply HSG brand green styling to all Streamlit table headers.
    
    This CSS injection ensures consistent branding across the application
    without needing per-table styling. It targets both st.dataframe and st.table
    components.
    """
    st.markdown(
        f"""
        <style>
        /* Style for st.table() headers */
        div[data-testid="stTable"] thead tr th {{
            background-color: {HSG_GREEN} !important;
            color: #ffffff !important;
            font-weight: 600 !important;
        }}
        
        /* Style for st.dataframe() headers */
        div[data-testid="stDataFrame"] thead tr th {{
            background-color: {HSG_GREEN} !important;
            color: #ffffff !important;
            font-weight: 600 !important;
        }}
        
        /* Ensure consistent hover effects */
        div[data-testid="stDataFrame"] thead tr th:hover {{
            background-color: {HSG_GREEN} !important;
            opacity: 0.9 !important;
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def show_errors(errors: Iterable[str]) -> None:
    """Display validation errors to the user.
    
    Args:
        errors: List of error messages to display
    """
    for msg in errors:
        st.error(msg)


def show_logo() -> None:
    """Display HSG logo in sidebar with error handling."""
    try:
        st.sidebar.image(LOGO_PATH, width=170, use_container_width=False)
    except FileNotFoundError:
        st.sidebar.warning("Logo image not found. Ensure 'HSG-logo-new.png' is in the repository root.")


def render_map_iframe() -> None:
    """Display interactive HSG campus map in a collapsible section.
    
    The map helps users identify room locations when reporting issues.
    """
    with st.expander("📍 Campus Map Reference", expanded=False):
        url = "https://use.mazemap.com/embed.html?v=1&zlevel=1&center=9.373611,47.429708&zoom=14.7&campusid=710"
        st.markdown(
            f"""
            <iframe src="{url}"
                width="100%" height="420" frameborder="0"
                marginheight="0" marginwidth="0" scrolling="no"></iframe>
            """,
            unsafe_allow_html=True,
        )


def location_label(loc_id: str) -> str:
    """Convert location ID to human-readable label.
    
    Args:
        loc_id: Location identifier
        
    Returns:
        Human-readable location name or "Unknown location" if not found
    """
    return LOCATIONS.get(str(loc_id), {}).get("label", "Unknown location")


def asset_display_label(row: pd.Series) -> str:
    """Generate descriptive label for asset selection dropdown.
    
    Args:
        row: DataFrame row containing asset data
        
    Returns:
        Formatted label with asset name, type, location, and status
    """
    status = str(row.get("status", "")).strip().lower()
    if status == "available":
        status_text = "✅ Available"
    elif status == "booked":
        status_text = "⛔ Booked"
    else:
        status_text = str(row.get("status", ""))
    
    loc = location_label(str(row.get("location_id", "")))
    return f'{row.get("asset_name", "")} • {row.get("asset_type", "")} • {loc} • {status_text}'


def format_booking_table(df: pd.DataFrame) -> pd.DataFrame:
    """Format booking data for user-friendly display.
    
    Args:
        df: Raw booking data DataFrame
        
    Returns:
        Formatted DataFrame with readable timestamps
    """
    if df.empty:
        return df
    
    out = df.copy()
    out["start_time"] = pd.to_datetime(out["start_time"], errors="coerce")
    out["end_time"] = pd.to_datetime(out["end_time"], errors="coerce")
    
    # Remove invalid rows and sort
    out = out.dropna(subset=["start_time", "end_time"]).sort_values(by=["start_time"])
    
    # Format timestamps for display
    out["start_time"] = out["start_time"].dt.strftime("%Y-%m-%d %H:%M")
    out["end_time"] = out["end_time"].dt.strftime("%Y-%m-%d %H:%M")
    
    return out.rename(columns={
        "user_name": "User",
        "start_time": "Start Time",
        "end_time": "End Time"
    })


# ============================================================================
# APPLICATION PAGES
# ============================================================================
def page_submission_form(con: sqlite3.Connection) -> None:
    """Display issue submission form with validation and confirmation.
    
    Args:
        con: Active database connection
    """
    st.header("📝 Report a Facility Issue")
    st.info("""
    Use this form to report facility-related issues. 
    You will receive a confirmation email with SLA details after submitting.
    """)
    st.caption("Fields marked with * are mandatory.")
    
    with st.form("issue_form", clear_on_submit=True):
        # Personal information section
        st.subheader("👤 Your Information")
        col1, col2 = st.columns(2)
        
        with col1:
            name = st.text_input("Name*", placeholder="e.g., Max Muster").strip()
        
        with col2:
            hsg_email = st.text_input(
                "HSG Email Address*",
                placeholder="firstname.lastname@student.unisg.ch"
            ).strip()
            st.caption("Must be @unisg.ch or @student.unisg.ch")
        
        # Issue details section
        st.subheader("📋 Issue Details")
        col3, col4 = st.columns(2)
        
        with col3:
            room_number_input = st.text_input(
                "Room Number*", 
                placeholder="e.g., A 09-001"
            ).strip()
        
        with col4:
            issue_type = st.selectbox("Issue Type*", ISSUE_TYPES)
        
        # Priority and description
        importance = st.selectbox("Priority Level*", IMPORTANCE_LEVELS)
        
        # Show SLA information based on priority
        sla_hours = SLA_HOURS_BY_IMPORTANCE.get(importance)
        if sla_hours is not None:
            sla_color = {
                "High": "🔴",
                "Medium": "🟡", 
                "Low": "🟢"
            }.get(importance, "")
            st.info(f"{sla_color} **SLA Target:** Resolution within {sla_hours} hours")
        
        user_comment = st.text_area(
            "Problem Description*",
            max_chars=500,
            placeholder="Describe the issue in detail:\n• What happened?\n• Where exactly?\n• Since when?\n• What is the impact?",
            height=120,
        ).strip()
        st.caption(f"Character count: {len(user_comment)}/500")
        
        # Optional photo upload
        st.subheader("📸 Optional Photo Upload")
        uploaded_file = st.file_uploader(
            "Upload a photo to help us understand the issue better",
            type=["jpg", "jpeg", "png"],
            help="Maximum file size: 5MB"
        )
        if uploaded_file is not None:
            st.image(uploaded_file, caption="Uploaded photo preview", use_container_width=True)
        
        # Campus map reference
        render_map_iframe()
        
        # Submit button
        submitted = st.form_submit_button(
            "🚀 Submit Issue Report",
            type="primary",
            use_container_width=True
        )
    
    # Handle form submission
    if not submitted:
        return
    
    # Validate and process submission
    sub = Submission(
        name=name,
        hsg_email=hsg_email.strip().lower(),
        issue_type=issue_type,
        room_number=normalize_room(room_number_input),
        importance=importance,
        user_comment=user_comment,
    )
    
    errors = validate_submission_input(sub)
    if errors:
        show_errors(errors)
        return
    
    # Database insertion
    try:
        insert_submission(con, sub)
    except Exception as e:
        st.error(f"Database error: {e}")
        logger.error(f"Failed to insert submission: {e}")
        return
    
    # Send confirmation email
    subject, body = confirmation_email_text(sub.name.strip(), sub.importance)
    ok, msg = send_email(sub.hsg_email, subject, body)
    
    # Show success message
    st.success("✅ Issue reported successfully!")
    st.balloons()
    
    if not ok:
        st.warning(f"Note: {msg}")


def build_display_table(df: pd.DataFrame) -> pd.DataFrame:
    """Format submissions data for user-friendly display.
    
    Args:
        df: Raw submissions DataFrame
        
    Returns:
        Formatted DataFrame with proper column names and sorting
    """
    display_df = df.copy().rename(
        columns={
            "id": "ID",
            "name": "Reporter Name",
            "hsg_email": "HSG Email",
            "issue_type": "Issue Type",
            "room_number": "Room Number",
            "importance": "Priority",
            "status": "Status",
            "user_comment": "Description",
            "created_at": "Submitted",
            "updated_at": "Last Updated",
            "assigned_to": "Assigned To",
            "resolved_at": "Resolved At",
            "expected_resolved_at": "SLA Target",
        }
    )
    
    # Sort by priority (High first) and recency
    importance_order = {"High": 0, "Medium": 1, "Low": 2}
    display_df["_priority_rank"] = display_df["Priority"].map(importance_order).fillna(99).astype(int)
    
    display_df = display_df.sort_values(
        by=["_priority_rank", "Submitted"],
        ascending=[True, False],
    ).drop(columns=["_priority_rank"])
    
    return display_df


def render_charts(df: pd.DataFrame) -> None:
    """Generate interactive analytics charts (Plotly-only for consistent UI)."""
    if df.empty:
        st.info("No data available for charts.")
        return

    # Ensure datetime parsing once (robust + avoids repeated conversions)
    df_local = df.copy()
    df_local["created_at_dt"] = pd.to_datetime(df_local.get("created_at"), errors="coerce")

    # Create tabs for different chart types
    tab1, tab2, tab3, tab4 = st.tabs([
        "📊 Issue Types",
        "📅 Daily Trends",
        "🎯 Priority Levels",
        "📈 Status Distribution",
    ])

    base_layout = dict(
        template="plotly_white",
        margin=dict(l=10, r=10, t=50, b=10),
        height=420,
    )

    with tab1:
        st.subheader("Issues by Type")

        issue_counts = (
            df_local["issue_type"]
            .value_counts()
            .reindex(ISSUE_TYPES, fill_value=0)
        )

        fig = go.Figure(
            data=[
                go.Bar(
                    x=issue_counts.values,
                    y=issue_counts.index,
                    orientation="h",
                    marker=dict(color=HSG_GREEN),
                    hovertemplate="Issues: %{x}<extra></extra>",
                )
            ]
        )
            # Plotly-friendly palette (stable + avoids matplotlib dependency)
        palette = [
            "#636EFA", "#EF553B", "#00CC96", "#AB63FA", "#FFA15A",
            "#19D3F3", "#FF6692", "#B6E880", "#FF97FF", "#FECB52"
        ]

        fig.update_layout(
            **base_layout,
            title="Issue Frequency by Type",
            xaxis_title="Number of Issues",
            yaxis_title="Issue Type",
        )

        st.plotly_chart(fig, use_container_width=True)

    with tab2:
        st.subheader("Submission Trends")

        df_dates = df_local.dropna(subset=["created_at_dt"]).copy()
        if df_dates.empty:
            st.info("No valid submission dates available.")
        else:
            df_dates["date"] = df_dates["created_at_dt"].dt.date
            date_range = pd.date_range(
                start=df_dates["created_at_dt"].min().date(),
                end=df_dates["created_at_dt"].max().date(),
                freq="D",
            )

            daily_counts = df_dates.groupby("date").size()
            daily_counts = daily_counts.reindex(date_range.date, fill_value=0)

            fig = go.Figure(
                data=[
                    go.Scatter(
                        x=date_range,
                        y=daily_counts.values,
                        mode="lines+markers",
                        line=dict(color=HSG_GREEN),
                        hovertemplate="%{x|%Y-%m-%d}<br>Issues: %{y}<extra></extra>",
                    )
                ]
            )
            fig.update_layout(
                **base_layout,
                title="Daily Submission Trends",
                xaxis_title="Date",
                yaxis_title="Issues Submitted",
            )

            st.plotly_chart(fig, use_container_width=True)

    with tab3:
        st.subheader("Priority Distribution")

        imp_counts = (
            df_local["importance"]
            .value_counts()
            .reindex(IMPORTANCE_LEVELS, fill_value=0)
        )

        # Keep meaningful priority colors, but still consistent in Plotly UI
        priority_colors = {
            "High": "#ff6b6b",
            "Medium": "#ffd93d",
            "Low": "#6bcf7f",
        }

        fig = go.Figure(
            data=[
                go.Bar(
                    x=imp_counts.index,
                    y=imp_counts.values,
                    marker=dict(color=[priority_colors.get(i, HSG_GREEN) for i in imp_counts.index]),
                    text=imp_counts.values,
                    textposition="auto",
                    hovertemplate="Count: %{y}<extra></extra>",
                )
            ]
        )
        fig.update_layout(
            **base_layout,
            title="Issues by Priority Level",
            xaxis_title="Priority Level",
            yaxis_title="Number of Issues",
        )

        st.plotly_chart(fig, use_container_width=True)

    with tab4:
        st.subheader("Status Overview")

        status_counts = (
            df_local["status"]
            .value_counts()
            .reindex(STATUS_LEVELS, fill_value=0)
        )

        if status_counts.sum() == 0:
            st.info("No status data available.")
        else:
            status_colors = {
                "Pending": "#ff6b6b",
                "In Progress": "#ffd93d",
                "Resolved": "#6bcf7f",
            }

            fig = go.Figure(
                data=[
                    go.Pie(
                        labels=status_counts.index,
                        values=status_counts.values,
                        hole=0.35,
                        marker=dict(colors=[status_colors.get(s, HSG_GREEN) for s in status_counts.index]),
                        textinfo="percent+label",
                        hovertemplate="%{label}<br>%{value} issues<extra></extra>",
                    )
                ]
            )
            fig.update_layout(
                **base_layout,
                title="Issue Status Distribution",
                height=460,
            )

            st.plotly_chart(fig, use_container_width=True)


def page_submitted_issues(con: sqlite3.Connection) -> None:
    """Display submitted issues with filtering and analytics.
    
    Args:
        con: Active database connection
    """
    st.header("📋 Submitted Issues Dashboard")
    
    # Load data with error handling
    try:
        df = fetch_submissions(con)
    except Exception as e:
        st.error(f"Failed to load submissions: {e}")
        logger.error(f"Database error in submitted issues: {e}")
        return
    
    # Display summary metrics
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Total Issues", len(df))
    with col2:
        open_count = len(df[df["status"] != "Resolved"]) if not df.empty else 0
        st.metric("Open Issues", open_count)
    with col3:
        resolved_count = len(df[df["status"] == "Resolved"]) if not df.empty else 0
        st.metric("Resolved", resolved_count)
    with col4:
        if not df.empty:
            high_priority = len(df[df["importance"] == "High"])
            st.metric("High Priority", high_priority)
        else:
            st.metric("High Priority", 0)
    
    if df.empty:
        st.info("No issues have been submitted yet.")
        return
    
    # Filtering options
    st.subheader("🔍 Filter Options")
    col_filter1, col_filter2, col_filter3 = st.columns(3)
    
    with col_filter1:
        status_filter = st.multiselect(
            "Filter by Status",
            options=STATUS_LEVELS,
            default=["Pending", "In Progress"],
            help="Select statuses to display"
        )
    
    with col_filter2:
        importance_filter = st.multiselect(
            "Filter by Priority",
            options=IMPORTANCE_LEVELS,
            default=IMPORTANCE_LEVELS,
            help="Select priority levels to display"
        )
    
    with col_filter3:
        issue_type_filter = st.multiselect(
            "Filter by Issue Type",
            options=ISSUE_TYPES,
            default=ISSUE_TYPES,
            help="Select issue types to display"
        )
    
    # Apply filters
    filtered_df = df[
        df["status"].isin(status_filter) &
        df["importance"].isin(importance_filter) &
        df["issue_type"].isin(issue_type_filter)
    ]
    
    if filtered_df.empty:
        st.info("No issues match the selected filters.")
        return
    
    # Calculate SLA metrics
    filtered_df["expected_resolved_at"] = filtered_df.apply(
        lambda r: (
            expected_resolution_dt(str(r["created_at"]), str(r["importance"])).isoformat(timespec="seconds")
            if expected_resolution_dt(str(r["created_at"]), str(r["importance"])) is not None
            else None
        ),
        axis=1,
    )
    
    # Calculate average resolution time for resolved issues
    resolved_df = filtered_df[filtered_df["status"] == "Resolved"].copy()
    if not resolved_df.empty and "created_at" in resolved_df.columns and "resolved_at" in resolved_df.columns:
        resolved_df["created_at_dt"] = pd.to_datetime(resolved_df["created_at"], errors="coerce")
        resolved_df["resolved_at_dt"] = pd.to_datetime(resolved_df["resolved_at"], errors="coerce")
        resolved_df = resolved_df.dropna(subset=["created_at_dt", "resolved_at_dt"])
        
        if not resolved_df.empty:
            resolved_df["resolution_hours"] = (
                (resolved_df["resolved_at_dt"] - resolved_df["created_at_dt"]).dt.total_seconds() / 3600.0
            )
            avg_resolution = resolved_df["resolution_hours"].mean()
            st.metric("Average Resolution Time", f"{avg_resolution:.1f} hours")
    
    # Display filtered data
    st.subheader(f"📊 Filtered Results ({len(filtered_df)} issues)")
    display_df = build_display_table(filtered_df)
    st.dataframe(display_df, use_container_width=True, hide_index=True, height=400)
    
    # Export functionality
    st.subheader("💾 Data Export")
    col_export1, col_export2 = st.columns(2)
    
    with col_export1:
        csv_bytes = filtered_df.to_csv(index=False).encode("utf-8")
        st.download_button(
            "Download CSV",
            data=csv_bytes,
            file_name=f"hsg_issues_{now_zurich().strftime('%Y%m%d')}.csv",
            mime="text/csv",
            use_container_width=True
        )
    
    with col_export2:
        if st.button("Refresh Data", use_container_width=True):
            st.rerun()
    
    # Visualizations
    st.subheader("📈 Data Visualizations")
    render_charts(filtered_df)
    
    # Audit log (collapsible)
    with st.expander("📋 View Status Change History"):
        try:
            log_df = fetch_status_log(con)
            if log_df.empty:
                st.info("No status changes recorded yet.")
            else:
                st.dataframe(log_df, use_container_width=True, hide_index=True)
        except Exception as e:
            st.error(f"Failed to load audit log: {e}")


def page_booking(con: sqlite3.Connection) -> None:
    """Display asset booking interface with availability checking.
    
    Args:
        con: Active database connection
    """
    st.header("📅 Book an Asset")
    
    # Sync booking statuses
    try:
        sync_asset_statuses_from_bookings(con)
        assets_df = fetch_assets(con)
    except Exception as e:
        st.error(f"Failed to load assets: {e}")
        logger.error(f"Database error in booking page: {e}")
        return
    
    if assets_df.empty:
        st.warning("No assets available for booking.")
        return
    
    # Asset search and filtering
    st.subheader("🔍 Find Available Assets")
    
    col_search1, col_search2, col_search3 = st.columns([2, 1, 1])
    
    with col_search1:
        search_term = st.text_input(
            "Search assets",
            placeholder="e.g., projector, meeting room, chair...",
            help="Search by asset name, type, or location"
        ).strip().lower()
    
    with col_search2:
        type_filter = st.selectbox(
            "Asset Type",
            options=["All Types"] + sorted(assets_df["asset_type"].unique().tolist()),
            help="Filter by asset category"
        )
    
    with col_search3:
        availability_filter = st.selectbox(
            "Availability",
            options=["All", "Available Only", "Booked Only"],
            help="Filter by current availability"
        )
    
    # Prepare display data
    view_df = assets_df.copy()
    view_df["location_label"] = view_df["location_id"].apply(location_label)
    view_df["display_label"] = view_df.apply(asset_display_label, axis=1)
    
    # Apply filters
    if type_filter != "All Types":
        view_df = view_df[view_df["asset_type"] == type_filter]
    
    if availability_filter == "Available Only":
        view_df = view_df[view_df["status"].astype(str).str.lower() == "available"]
    elif availability_filter == "Booked Only":
        view_df = view_df[view_df["status"].astype(str).str.lower() == "booked"]
    
    if search_term:
        mask = (
            view_df["asset_name"].str.lower().str.contains(search_term, na=False) |
            view_df["asset_type"].str.lower().str.contains(search_term, na=False) |
            view_df["location_label"].str.lower().str.contains(search_term, na=False)
        )
        view_df = view_df[mask].copy()
    
    # Sort: available assets first, then by type and name
    view_df["_status_rank"] = view_df["status"].astype(str).str.lower().map(
        {"available": 0, "booked": 1}
    ).fillna(99).astype(int)
    view_df = view_df.sort_values(
        by=["_status_rank", "asset_type", "asset_name"]
    ).drop(columns=["_status_rank"])
    
    if view_df.empty:
        st.info("No assets match your search criteria.")
        return
    
    # Asset selection
    st.subheader("🎯 Select Asset")
    
    asset_labels = {str(r["asset_id"]): str(r["display_label"]) for _, r in view_df.iterrows()}
    
    # Preserve selection across reruns
    default_asset_id = st.session_state.get("booking_asset_id")
    if default_asset_id not in asset_labels:
        default_asset_id = list(asset_labels.keys())[0]
    
    asset_id = st.selectbox(
        "Choose asset to book:",
        options=list(asset_labels.keys()),
        index=list(asset_labels.keys()).index(default_asset_id),
        format_func=lambda aid: asset_labels[aid],
        help="Select an asset to view details and create booking"
    )
    st.session_state["booking_asset_id"] = asset_id
    
    # Display selected asset details
    selected_asset = assets_df[assets_df["asset_id"] == asset_id].iloc[0]
    
    st.subheader("📋 Asset Details")
    col_details1, col_details2, col_details3 = st.columns(3)
    
    with col_details1:
        st.metric("Status", selected_asset["status"].capitalize())
    
    with col_details2:
        st.metric("Type", selected_asset["asset_type"])
    
    with col_details3:
        st.metric("Location", location_label(str(selected_asset["location_id"])))
    
    # Availability status with next available time
    if selected_asset["status"].lower() == "available":
        st.success("✅ This asset is available for booking.")
    else:
        next_free = next_available_time(con, asset_id)
        if next_free:
            st.warning(
                f"⛔ Currently booked. Next available: **{next_free.strftime('%Y-%m-%d %H:%M')}**"
            )
        else:
            st.warning("⛔ Currently booked. No future bookings found.")
    
    # Show upcoming bookings
    st.subheader("📅 Upcoming Bookings")
    try:
        future_bookings = fetch_future_bookings(con, asset_id)
        if future_bookings.empty:
            st.info("No upcoming bookings scheduled.")
        else:
            display_bookings = format_booking_table(future_bookings)
            st.dataframe(display_bookings, use_container_width=True, hide_index=True)
    except Exception as e:
        st.error(f"Failed to load bookings: {e}")
    
    # Booking form (only for available assets)
    if selected_asset["status"].lower() != "available":
        st.info("Select an available asset to create a booking.")
        return
    
    st.divider()
    st.subheader("📝 Create New Booking")
    
    with st.form("booking_form"):
        # User information
        user_name = st.text_input(
            "Your Name*",
            placeholder="e.g., Max Muster",
            help="Enter your full name"
        ).strip()
        
        # Date and time selection
        col_time1, col_time2, col_time3 = st.columns(3)
        
        with col_time1:
            start_date = st.date_input(
                "Start Date*",
                value=now_zurich().date(),
                min_value=now_zurich().date(),
                help="Cannot book in the past"
            )
        
        with col_time2:
            # Round time to nearest 30 minutes
            current_time = now_zurich().time()
            rounded_minute = 30 * ((current_time.minute + 14) // 30)
            if rounded_minute == 60:
                default_time = current_time.replace(
                    hour=current_time.hour + 1,
                    minute=0,
                    second=0,
                    microsecond=0
                )
            else:
                default_time = current_time.replace(
                    minute=rounded_minute,
                    second=0,
                    microsecond=0
                )
            
            start_time = st.time_input(
                "Start Time*",
                value=default_time,
                step=1800,  # 30 minute increments
                help="Select time in 30-minute intervals"
            )
        
        with col_time3:
            duration_options = {
                "1 hour": 1,
                "2 hours": 2,
                "3 hours": 3,
                "4 hours": 4,
                "6 hours": 6,
                "8 hours": 8
            }
            duration_choice = st.selectbox(
                "Duration*",
                options=list(duration_options.keys()),
                help="Select booking duration"
            )
            duration_hours = duration_options[duration_choice]
        
        # Calculate and display booking summary
        start_dt = APP_TZ.localize(datetime.combine(start_date, start_time))
        end_dt = start_dt + timedelta(hours=duration_hours)
        
        st.info(f"""
        **Booking Summary:**
        - **Asset:** {selected_asset['asset_name']}
        - **Date:** {start_dt.strftime('%A, %d %B %Y')}
        - **Time:** {start_dt.strftime('%H:%M')} → {end_dt.strftime('%H:%M')}
        - **Duration:** {duration_hours} hour{'s' if duration_hours > 1 else ''}
        """)
        
        # Submit booking
        submitted = st.form_submit_button(
            "✅ Confirm Booking",
            type="primary",
            use_container_width=True
        )
    
    # Handle booking submission
    if not submitted:
        return
    
    # Validate inputs
    if not user_name:
        st.error("Please enter your name.")
        return
    
    # Validate time logic
    if start_dt < now_zurich():
        st.error("Start time cannot be in the past.")
        return
    
    if end_dt <= start_dt:
        st.error("End time must be after start time.")
        return
    
    # Check availability
    try:
        if not is_asset_available(con, asset_id, start_dt, end_dt):
            st.error("This asset is already booked during the selected time period.")
            return
    except Exception as e:
        st.error(f"Availability check failed: {e}")
        logger.error(f"Availability check error: {e}")
        return
    
    # Create booking
    try:
        con.execute(
            """
            INSERT INTO bookings (asset_id, user_name, start_time, end_time, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                asset_id,
                user_name,
                start_dt.isoformat(timespec="seconds"),
                end_dt.isoformat(timespec="seconds"),
                now_zurich_str(),
            ),
        )
        con.commit()
        
        # Update asset statuses
        sync_asset_statuses_from_bookings(con)
        
        # Success message
        st.success(f"""
        🎉 **Booking Confirmed!**
        
        **Details:**
        - Asset: {selected_asset['asset_name']}
        - Date: {start_dt.strftime('%A, %d %B %Y')}
        - Time: {start_dt.strftime('%H:%M')} - {end_dt.strftime('%H:%M')}
        - Duration: {duration_hours} hour{'s' if duration_hours > 1 else ''}
        """)
        
        # Auto-refresh to show updated status
        st.rerun()
        
    except Exception as e:
        st.error(f"Failed to create booking: {e}")
        logger.error(f"Booking creation error: {e}")


def page_assets(con: sqlite3.Connection) -> None:
    """Display asset tracking and management interface.
    
    Args:
        con: Active database connection
    """
    st.header("📍 Asset Tracking")
    
    # Load assets data
    try:
        df = fetch_assets(con)
    except Exception as e:
        st.error(f"Failed to load assets: {e}")
        logger.error(f"Database error in asset tracking: {e}")
        return
    
    if df.empty:
        st.info("No assets available in the system.")
        return
    
    # Add location labels for display
    df = df.copy()
    df["location_label"] = df["location_id"].apply(location_label)
    
    # Filtering options
    st.subheader("🔍 Filter Assets")
    col_filter1, col_filter2 = st.columns(2)
    
    with col_filter1:
        location_filter = st.multiselect(
            "Filter by Location",
            options=sorted(df["location_label"].unique()),
            default=sorted(df["location_label"].unique()),
            help="Select locations to display"
        )
    
    with col_filter2:
        status_filter = st.multiselect(
            "Filter by Status",
            options=sorted(df["status"].unique()),
            default=sorted(df["status"].unique()),
            help="Select statuses to display"
        )
    
    # Apply filters
    filtered_df = df[
        (df["location_label"].isin(location_filter)) &
        (df["status"].isin(status_filter))
    ]
    
    # Display assets grouped by location
    st.subheader("📦 Assets by Location")
    
    if filtered_df.empty:
        st.info("No assets match the selected filters.")
    else:
        for location, group in filtered_df.groupby("location_label"):
            with st.expander(f"🏢 {location} ({len(group)} assets)", expanded=False):
                display_data = group[[
                    "asset_id", "asset_name", "asset_type", "status"
                ]].copy()
                display_data = display_data.rename(columns={
                    "asset_id": "ID",
                    "asset_name": "Name", 
                    "asset_type": "Type",
                    "status": "Status"
                })
                st.dataframe(display_data, use_container_width=True, hide_index=True)
    
    st.divider()
    
    # Asset movement functionality
    st.subheader("🚚 Move Asset to New Location")
    
    # Prepare asset selection data
    assets_df = fetch_assets(con).copy()
    assets_df["location_label"] = assets_df["location_id"].apply(location_label)
    assets_df["display_label"] = assets_df.apply(asset_display_label, axis=1)
    
    asset_options = {str(r["asset_id"]): str(r["display_label"]) for _, r in assets_df.iterrows()}
    
    if not asset_options:
        st.info("No assets available for movement.")
        return
    
    # Asset selection
    asset_id = st.selectbox(
        "Select asset to move:",
        options=list(asset_options.keys()),
        format_func=lambda aid: asset_options[aid],
        help="Choose which asset to relocate"
    )
    
    # Display current asset details
    selected_asset = assets_df[assets_df["asset_id"] == asset_id].iloc[0]
    
    col_current1, col_current2, col_current3 = st.columns(3)
    with col_current1:
        st.metric("Current Status", selected_asset["status"].capitalize())
    with col_current2:
        st.metric("Asset Type", selected_asset["asset_type"])
    with col_current3:
        st.metric("Current Location", str(selected_asset["location_label"]))
    
    # New location selection
    st.subheader("🎯 Select New Location")
    new_location_id = st.selectbox(
        "New location:",
        options=list(LOCATIONS.keys()),
        format_func=lambda x: LOCATIONS[x]["label"],
        help="Choose the destination location"
    )
    
    # Move confirmation
    if st.button("🚀 Move Asset", type="primary", use_container_width=True):
        if new_location_id == selected_asset["location_id"]:
            st.warning("Asset is already at this location.")
        else:
            try:
                con.execute(
                    "UPDATE assets SET location_id = ? WHERE asset_id = ?",
                    (new_location_id, asset_id)
                )
                con.commit()
                st.success(f"""
                ✅ Asset moved successfully!
                
                **From:** {selected_asset['location_label']}
                **To:** {LOCATIONS[new_location_id]['label']}
                """)
                st.rerun()
            except Exception as e:
                st.error(f"Failed to move asset: {e}")
                logger.error(f"Asset movement error: {e}")


def page_overwrite_status(con: sqlite3.Connection) -> None:
    """Admin interface for managing issue statuses and assignments.
    
    Password protected to ensure only authorized personnel can modify issue states.
    
    Args:
        con: Active database connection
    """
    st.header("🔧 Admin Panel - Issue Management")
    
    # Password protection
    entered_password = st.text_input(
        "Enter Admin Password",
        type="password",
        help="Enter the admin password to access this panel"
    )
    
    if entered_password != ADMIN_PASSWORD:
        st.info("🔐 Please enter the admin password to continue.")
        return
    
    # Admin quick actions
    st.subheader("⚡ Quick Actions")
    col_action1, col_action2 = st.columns(2)
    
    with col_action1:
        if st.button("📊 Send Weekly Report Now", use_container_width=True):
            try:
                df_all = fetch_submissions(con)
                subject, body = build_weekly_report(df_all)
                ok, msg = send_admin_report_email(subject, body)
                if ok:
                    mark_report_sent(con, "weekly_manual")
                    st.success("Weekly report sent successfully!")
                else:
                    st.warning(f"Report sending failed: {msg}")
            except Exception as e:
                st.error(f"Failed to send report: {e}")
    
    with col_action2:
        if st.button("🔄 Refresh All Data", use_container_width=True):
            st.rerun()
    
    # Load issues data
    try:
        df = fetch_submissions(con)
    except Exception as e:
        st.error(f"Failed to load issues: {e}")
        return
    
    if df.empty:
        st.info("No issues available for management.")
        return
    
    # Filter options
    st.subheader("🔍 Filter Issues")
    admin_status_filter = st.multiselect(
        "Show issues with status:",
        options=STATUS_LEVELS,
        default=["Pending", "In Progress"],
        help="Filter issues by current status"
    )
    
    filtered_df = df[df["status"].isin(admin_status_filter)]
    
    if filtered_df.empty:
        st.info("No issues match the selected filters.")
        return
    
    # Issue selection
    st.subheader("🎯 Select Issue to Update")
    
    # Create descriptive labels for dropdown
    issue_options = {
        row["id"]: f"#{row['id']}: {row['issue_type']} ({row['room_number']}) - {row['status']}"
        for _, row in filtered_df.iterrows()
    }
    
    selected_id = st.selectbox(
        "Choose issue:",
        options=list(issue_options.keys()),
        format_func=lambda x: issue_options[x],
        help="Select an issue to update its status and assignment"
    )
    
    # Get selected issue details
    row = df[df["id"] == selected_id].iloc[0]
    
    # Display issue details
    st.subheader("📋 Issue Details")
    
    col_details1, col_details2 = st.columns(2)
    
    with col_details1:
        st.metric("Issue ID", selected_id)
        st.metric("Priority", row["importance"])
        st.metric("Current Status", row["status"])
    
    with col_details2:
        sla_target = expected_resolution_dt(str(row["created_at"]), str(row["importance"]))
        sla_text = sla_target.strftime("%Y-%m-%d %H:%M") if sla_target else "N/A"
        st.metric("SLA Target", sla_text)
        st.metric("Assigned To", row.get("assigned_to", "Unassigned"))
        st.metric("Room", row["room_number"])
    
    # Display additional details
    with st.expander("📝 View Full Details", expanded=False):
        st.write("**Reporter:**", row["name"])
        st.write("**Email:**", row["hsg_email"])
        st.write("**Issue Type:**", row["issue_type"])
        st.write("**Submitted:**", row["created_at"])
        st.write("**Last Updated:**", row["updated_at"])
        st.write("**Resolved At:**", row.get("resolved_at", "Not resolved"))
        st.write("**Description:**", row["user_comment"])
    
    st.divider()
    
    # Update form
    st.subheader("✏️ Update Issue")
    
    with st.form("admin_update_form"):
        # Assignment selection
        current_assignee = str(row.get("assigned_to", "") or "")
        assigned_to = st.selectbox(
            "Assign to:",
            options=["(Unassigned)"] + ASSIGNEES,
            index=(["(Unassigned)"] + ASSIGNEES).index(current_assignee)
            if current_assignee in (["(Unassigned)"] + ASSIGNEES)
            else 0,
            help="Assign this issue to a team member"
        )
        assigned_to_value = None if assigned_to == "(Unassigned)" else assigned_to
        
        # Status update
        new_status = st.selectbox(
            "Update status to:",
            STATUS_LEVELS,
            index=STATUS_LEVELS.index(row["status"]) if row["status"] in STATUS_LEVELS else 0,
            help="Set the new status for this issue"
        )
        
        # Resolution confirmation (only for Resolved status)
        confirm_resolution = True
        if new_status == "Resolved":
            confirm_resolution = st.checkbox(
                "✓ Confirm issue resolution (will send notification email)",
                value=False,
                help="Check to confirm the issue is fully resolved"
            )
        
        # Submit update
        submitted = st.form_submit_button(
            "💾 Save Changes",
            type="primary",
            use_container_width=True
        )
    
    # Handle form submission
    if not submitted:
        return
    
    # Validate resolution confirmation
    if new_status == "Resolved" and not confirm_resolution:
        st.error("Please confirm resolution before setting status to 'Resolved'.")
        return
    
    # Update issue in database
    try:
        old_status = str(row["status"])
        update_issue_admin_fields(
            con=con,
            issue_id=int(selected_id),
            new_status=new_status,
            assigned_to=assigned_to_value,
            old_status=old_status,
        )
        
        # Send resolution email if status changed to Resolved
        if new_status == "Resolved":
            email_errors = validate_admin_email(str(row["hsg_email"]))
            if email_errors:
                show_errors(email_errors)
            else:
                subject, body = resolved_email_text(str(row["name"]).strip() or "there")
                ok, msg = send_email(str(row["hsg_email"]).strip(), subject, body)
                if ok:
                    st.success("✓ Resolution notification sent to reporter.")
                else:
                    st.warning(f"Notification email failed: {msg}")
        
        st.success("✅ Issue updated successfully!")
        st.rerun()
        
    except Exception as e:
        st.error(f"Failed to update issue: {e}")
        logger.error(f"Admin update error: {e}")


def page_overview_dashboard(con: sqlite3.Connection) -> None:
    """Display comprehensive overview dashboard with key metrics.
    
    Args:
        con: Active database connection
    """
    st.header("📊 Overview Dashboard")
    st.caption("Real-time overview of system status and key metrics.")
    
    # Load data with error handling
    try:
        issues = fetch_submissions(con)
        assets = fetch_assets(con)
    except Exception as e:
        st.error(f"Failed to load data: {e}")
        logger.error(f"Dashboard data loading error: {e}")
        return
    
    # Key metrics
    st.subheader("📈 Key Metrics")
    
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        total_issues = len(issues)
        st.metric("Total Issues", total_issues)
    
    with col2:
        open_issues = len(issues[issues["status"] != "Resolved"]) if not issues.empty else 0
        st.metric("Open Issues", open_issues)
    
    with col3:
        resolved_issues = len(issues[issues["status"] == "Resolved"]) if not issues.empty else 0
        st.metric("Resolved Issues", resolved_issues)
    
    with col4:
        total_assets = len(assets)
        available_assets = len(assets[assets["status"] == "available"]) if not assets.empty else 0
        st.metric("Available Assets", f"{available_assets}/{total_assets}")
    
    # Create tabs for different views
    tab1, tab2 = st.tabs(["📋 Issues Overview", "📦 Assets Overview"])
    
    with tab1:
        st.subheader("Current Issues")
        
        if issues.empty:
            st.info("No issues reported yet.")
        else:
            # Display open issues
            open_issues_df = issues[issues["status"] != "Resolved"]
            if not open_issues_df.empty:
                st.write(f"**Open Issues ({len(open_issues_df)}):**")
                display_open = open_issues_df[[
                    "id", "issue_type", "room_number", "importance", "status", "created_at"
                ]].copy()
                display_open = display_open.rename(columns={
                    "id": "ID",
                    "issue_type": "Type",
                    "room_number": "Room",
                    "importance": "Priority",
                    "status": "Status",
                    "created_at": "Reported"
                })
                st.dataframe(display_open, use_container_width=True, hide_index=True)
            else:
                st.success("✅ All issues are resolved!")
            
            # Quick statistics
            st.subheader("📊 Quick Statistics")
            col_stat1, col_stat2, col_stat3 = st.columns(3)
            
            with col_stat1:
                high_priority = len(issues[issues["importance"] == "High"])
                st.metric("High Priority", high_priority)
            
            with col_stat2:
                if not issues.empty:
                    avg_age = (now_zurich() - pd.to_datetime(issues["created_at"])).dt.days.mean()
                    st.metric("Avg. Issue Age", f"{avg_age:.1f} days")
                else:
                    st.metric("Avg. Issue Age", "N/A")
            
            with col_stat3:
                top_issue = issues["issue_type"].mode()[0] if not issues.empty else "N/A"
                st.metric("Most Common Issue", top_issue)
    
    with tab2:
        st.subheader("Asset Inventory")
        
        if assets.empty:
            st.info("No assets in inventory.")
        else:
            # Add location labels
            assets_display = assets.copy()
            assets_display["location"] = assets_display["location_id"].apply(location_label)
            
            # Display assets
            st.dataframe(
                assets_display[["asset_id", "asset_name", "asset_type", "status", "location"]],
                use_container_width=True,
                hide_index=True,
                column_config={
                    "asset_id": "ID",
                    "asset_name": "Name",
                    "asset_type": "Type",
                    "status": "Status",
                    "location": "Location"
                }
            )
            
            # Asset statistics
            st.subheader("📊 Asset Statistics")
            col_asset1, col_asset2, col_asset3 = st.columns(3)
            
            with col_asset1:
                asset_types = assets["asset_type"].nunique()
                st.metric("Asset Types", asset_types)
            
            with col_asset2:
                booked_count = len(assets[assets["status"] == "booked"])
                st.metric("Currently Booked", booked_count)
            
            with col_asset3:
                # Find location with most assets
                if not assets.empty:
                    top_location = assets["location_id"].mode()[0]
                    top_location_name = location_label(top_location)
                    st.metric("Busiest Location", top_location_name)
                else:
                    st.metric("Busiest Location", "N/A")


# ============================================================================
# MAIN APPLICATION
# ============================================================================
def main() -> None:
    """Main application entry point."""
    # Page configuration
    st.set_page_config(
        page_title="HSG Reporting Tool",
        page_icon="🏛️",
        layout="centered",
        initial_sidebar_state="expanded"
    )
    
    # Apply HSG branding to all table headers
    apply_hsg_table_header_style()
    
    # Sidebar setup
    show_logo()
    st.sidebar.markdown("### 🧭 Navigation")
    
    # Navigation
    section = st.sidebar.radio(
        "Select section:",
        ["📋 Reporting Tool", "📅 Booking & Tracking", "📊 Overview"],
        label_visibility="collapsed"
    )
    
    # Determine page based on section
    if section == "📋 Reporting Tool":
        page = st.sidebar.selectbox(
            "Select page:",
            ["📝 Submit Issue", "📋 View Issues", "🔧 Admin Panel"],
            label_visibility="collapsed"
        )
        page_map = {
            "📝 Submit Issue": "Submission Form",
            "📋 View Issues": "Submitted Issues",
            "🔧 Admin Panel": "Overwrite Status"
        }
        current_page = page_map[page]
    elif section == "📅 Booking & Tracking":
        page = st.sidebar.selectbox(
            "Select page:",
            ["📅 Book Assets", "📍 Track Assets"],
            label_visibility="collapsed"
        )
        page_map = {
            "📅 Book Assets": "Booking",
            "📍 Track Assets": "Asset Tracking"
        }
        current_page = page_map[page]
    else:  # Overview section
        current_page = "Overview Dashboard"
    
    # Header image
    try:
        st.image(
            "campus_header.jpeg",
            caption="University of St. Gallen – Campus",
            use_container_width=True,
        )
    except FileNotFoundError:
        st.caption("🏛️ University of St. Gallen Reporting Tool")
    
    # Database initialization
    try:
        con = get_connection()
        init_db(con)
        migrate_db(con)
        init_booking_table(con)
        init_assets_table(con)
        seed_assets(con)
        
        # Sync booking statuses
        sync_asset_statuses_from_bookings(con)
        
        # Check for automated reports
        send_weekly_report_if_due(con)
        
    except Exception as e:
        st.error(f"❌ Database initialization failed: {e}")
        logger.critical(f"Database initialization error: {e}")
        return
    
    # Application title
    st.title("🏛️ HSG Reporting Tool")
    st.caption("University of St. Gallen – Facility Management System")
    
    # Page routing
    page_functions = {
        "Submission Form": lambda: page_submission_form(con),
        "Submitted Issues": lambda: page_submitted_issues(con),
        "Booking": lambda: page_booking(con),
        "Asset Tracking": lambda: page_assets(con),
        "Overview Dashboard": lambda: page_overview_dashboard(con),
        "Overwrite Status": lambda: page_overwrite_status(con),
    }
    
    if current_page in page_functions:
        page_functions[current_page]()
    else:
        st.error(f"Page '{current_page}' not found.")
    
    # Footer
    st.sidebar.markdown("---")
    st.sidebar.caption(f"© {datetime.now().year} University of St. Gallen")
    st.sidebar.caption(f"Last updated: {now_zurich().strftime('%Y-%m-%d %H:%M')}")


# ============================================================================
# APPLICATION ENTRY POINT
# ============================================================================
if __name__ == "__main__":
    # Add basic error handling for the entire application
    try:
        main()
    except Exception as e:
        # Log the error for debugging
        logger.critical(f"Application crashed: {e}", exc_info=True)
        
        # Show user-friendly error message
        st.error("""
        ⚠️ **Application Error**
        
        The application encountered an unexpected error. Please try the following:
        
        1. Refresh the page
        2. Check your internet connection
        3. Contact support if the problem persists
        
        Error details (for administrators):
        ```
        {}
        ```
        """.format(str(e)))
        
        # Only show traceback in debug mode
        if DEBUG:
            import traceback
            st.code(traceback.format_exc(), language="python")
