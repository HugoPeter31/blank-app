from __future__ import annotations

# ============================================================================
# REPORTING TOOL @ HSG (Streamlit)
# ============================================================================
# Application: Streamlit-based reporting system
# Purpose: Facility issue reporting, asset booking, and tracking system
# Developed by: Arthur Lavric & Fabio Patierno
#
# Key Design Choices (why this is structured this way):
# - Secrets are loaded inside the Streamlit app lifecycle (not at import time),
#   so configuration errors show a clear UI message instead of a blank crash.
# - All database writes use transactions (`with con:`) to keep updates atomic.
# - Time handling uses a single timezone source to prevent subtle mismatches.
# - Input normalization happens before validation so common user formats work.
#
# Important Streamlit UX note:
# - Widgets inside `st.form(...)` do NOT rerun on each keystroke; changes apply on submit.
#   Therefore, anything that must react immediately (e.g., SLA info, live char counter)
#   must be rendered OUTSIDE the form.
#
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
APP_TZ = pytz.timezone("Europe/Zurich")  # Single timezone source to avoid mixed-time bugs
DB_PATH = "hsg_reporting.db"
LOGO_PATH = "HSG-logo-new.png"

# Predefined issue types
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

# Service Level Agreement (SLA) definitions in hours
SLA_HOURS_BY_IMPORTANCE: dict[str, int] = {
    "High": 24,
    "Medium": 72,
    "Low": 120,
}

# Validation patterns for user inputs
EMAIL_PATTERN = re.compile(r"^[\w.]+@(student\.)?unisg\.ch$")
# Accept both "A 09-001" and "A09-001"; normalization will standardize.
ROOM_PATTERN = re.compile(r"^[A-Z]\s?\d{2}-\d{3}$")

# Location mapping for asset tracking
LOCATIONS = {
    "R_A_09001": {"label": "Room A 09-001", "x": 10, "y": 20},
    "H_A_09001": {"label": "Hallway near Room A 09-001", "x": 15, "y": 25},
    "R_B_10012": {"label": "Room B 10-012", "x": 40, "y": 60},
    "H_B_10012": {"label": "Hallway near Room B 10-012", "x": 45, "y": 65},
}

# Keeps long descriptions readable in tables while still allowing full access via detail view.
DESCRIPTION_PREVIEW_CHARS = 90

# ============================================================================
# LOGGING CONFIGURATION
# ============================================================================
# Streamlit reruns frequently; a guarded handler setup avoids duplicate log handlers.
logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


# ============================================================================
# DATA MODELS
# ============================================================================
@dataclass(frozen=True)
class Submission:
    """Represents a validated issue submission payload.

    Frozen dataclasses prevent accidental mutation after validation, which helps
    in Streamlit's rerun model (fewer state-related surprises).
    """

    name: str
    hsg_email: str
    issue_type: str
    room_number: str
    importance: str
    user_comment: str


@dataclass(frozen=True)
class AppConfig:
    """Centralizes secrets/config so app behavior is predictable and testable."""

    smtp_server: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    from_email: str
    admin_inbox: str
    admin_password: str
    debug: bool
    assignees: list[str]
    auto_weekly_report: bool
    report_weekday: int
    report_hour: int


# ============================================================================
# SECRETS MANAGEMENT (Streamlit Cloud Secrets)
# ============================================================================
def get_secret(key: str, default: str | None = None) -> str:
    """Safely retrieve a secret from Streamlit secrets configuration.

    We stop the app (with a visible error) for required secrets because partial
    configuration creates confusing runtime behavior later.
    """
    if key in st.secrets:
        return str(st.secrets[key])
    if default is not None:
        return default
    st.error(f"Missing Streamlit secret: {key}")
    st.stop()


@st.cache_resource
def get_config() -> AppConfig:
    """Load secrets once per session.

    Why: loading secrets at import time can crash the app before Streamlit renders.
    This way, misconfiguration is surfaced as a clean UI error.
    """
    smtp_server = get_secret("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(get_secret("SMTP_PORT", "587"))
    smtp_username = get_secret("SMTP_USERNAME")
    smtp_password = get_secret("SMTP_PASSWORD")
    from_email = get_secret("FROM_EMAIL", smtp_username)
    admin_inbox = get_secret("ADMIN_INBOX", from_email)

    admin_password = get_secret("ADMIN_PASSWORD")
    debug = get_secret("DEBUG", "0") == "1"

    assignees_raw = get_secret("ASSIGNEES", "Facility Team")
    assignees = [a.strip() for a in assignees_raw.split(",") if a.strip()]

    auto_weekly_report = get_secret("AUTO_WEEKLY_REPORT", "0") == "1"
    report_weekday = int(get_secret("REPORT_WEEKDAY", "0"))  # 0=Monday, 6=Sunday
    report_hour = int(get_secret("REPORT_HOUR", "7"))  # 24h format

    return AppConfig(
        smtp_server=smtp_server,
        smtp_port=smtp_port,
        smtp_username=smtp_username,
        smtp_password=smtp_password,
        from_email=from_email,
        admin_inbox=admin_inbox,
        admin_password=admin_password,
        debug=debug,
        assignees=assignees,
        auto_weekly_report=auto_weekly_report,
        report_weekday=report_weekday,
        report_hour=report_hour,
    )


# ============================================================================
# TIME HELPER FUNCTIONS
# ============================================================================
def now_zurich() -> datetime:
    """Get current time in Zurich timezone (single source of truth)."""
    return datetime.now(APP_TZ)


def now_zurich_str() -> str:
    """Get current Zurich time as ISO 8601 string."""
    return now_zurich().isoformat(timespec="seconds")


def iso_to_dt(value: str) -> datetime | None:
    """Safely convert ISO string to datetime object."""
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        logger.warning("Failed to parse datetime from value=%r", value)
        return None


def safe_localize(dt_naive: datetime) -> datetime:
    """Localize a naive datetime into APP_TZ safely.

    DST transitions can create ambiguous or non-existent local times.
    We choose deterministic fallbacks to avoid runtime crashes.
    """
    try:
        return APP_TZ.localize(dt_naive, is_dst=None)
    except pytz.AmbiguousTimeError:
        # Prefer standard time to keep ordering stable.
        return APP_TZ.localize(dt_naive, is_dst=False)
    except pytz.NonExistentTimeError:
        # Push forward by 1 hour if time doesn't exist (DST spring forward).
        return APP_TZ.localize(dt_naive + timedelta(hours=1), is_dst=True)


def expected_resolution_dt(created_at_iso: str, importance: str) -> datetime | None:
    """Calculate expected resolution time based on SLA.

    Why: must be deterministic and based on stored data (importance argument),
    not on UI state (session_state), otherwise dashboards/admin views break.
    """
    created_dt = iso_to_dt(created_at_iso)
    sla_hours = SLA_HOURS_BY_IMPORTANCE.get(str(importance))
    if created_dt is None or sla_hours is None:
        return None
    return created_dt + timedelta(hours=int(sla_hours))


def is_room_location(location_id: str) -> bool:
    """Check if a location ID represents a room."""
    return str(location_id).startswith("R_")


# ============================================================================
# VALIDATION FUNCTIONS
# ============================================================================
def valid_email(hsg_email: str) -> bool:
    """Validate email address format."""
    return bool(EMAIL_PATTERN.fullmatch(hsg_email.strip()))


def normalize_room(room_number: str) -> str:
    """Normalize room number to canonical format.

    Normalization reduces downstream branching by ensuring storage uses one format.
    """
    raw = room_number.strip().upper()
    raw = re.sub(r"^([A-Z])(\d{2}-\d{3})$", r"\1 \2", raw)  # A09-001 -> A 09-001
    raw = re.sub(r"\s+", " ", raw)  # collapse whitespace
    return raw


def valid_room_number(room_number: str) -> bool:
    """Validate room number format after normalization."""
    return bool(ROOM_PATTERN.fullmatch(normalize_room(room_number)))


def validate_submission_input(sub: Submission) -> list[str]:
    """Validate all inputs for issue submission."""
    errors: list[str] = []

    if not sub.name.strip():
        errors.append("Name is required.")

    if not sub.hsg_email.strip():
        errors.append("Email address is required.")
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
    """Validate email for admin-triggered notifications."""
    if not email.strip():
        return ["Email address is required."]
    if not valid_email(email):
        return ["Please provide a valid email address (…@unisg.ch or …@student.unisg.ch)."]
    return []


# ============================================================================
# DATABASE MANAGEMENT
# ============================================================================
@st.cache_resource
def get_connection() -> sqlite3.Connection:
    """Create and cache SQLite database connection."""
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def init_db(con: sqlite3.Connection) -> None:
    """Initialize core database tables for issue reporting (idempotent)."""
    with con:
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


def init_booking_table(con: sqlite3.Connection) -> None:
    """Initialize booking system tables (idempotent)."""
    with con:
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


def init_assets_table(con: sqlite3.Connection) -> None:
    """Initialize assets table for both booking and tracking (idempotent)."""
    with con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS assets (
                asset_id TEXT PRIMARY KEY,
                asset_name TEXT NOT NULL,
                asset_type TEXT NOT NULL,
                location_id TEXT NOT NULL,
                status TEXT NOT NULL
            )
        """)


def migrate_db(con: sqlite3.Connection) -> None:
    """Apply schema migrations for backward compatibility."""
    cols = {row[1] for row in con.execute("PRAGMA table_info(submissions)").fetchall()}
    now_iso = now_zurich_str()

    with con:
        if "created_at" not in cols:
            con.execute("ALTER TABLE submissions ADD COLUMN created_at TEXT")
            con.execute("UPDATE submissions SET created_at = ? WHERE created_at IS NULL", (now_iso,))

        if "updated_at" not in cols:
            con.execute("ALTER TABLE submissions ADD COLUMN updated_at TEXT")
            con.execute("UPDATE submissions SET updated_at = ? WHERE updated_at IS NULL", (now_iso,))

        if "assigned_to" not in cols:
            con.execute("ALTER TABLE submissions ADD COLUMN assigned_to TEXT")

        if "resolved_at" not in cols:
            con.execute("ALTER TABLE submissions ADD COLUMN resolved_at TEXT")

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


def seed_assets(con: sqlite3.Connection) -> None:
    """Populate database with initial demo assets (idempotent)."""
    assets = [
        ("ROOM_A", "Study Room A", "Room", "R_A_09001", "available"),
        ("ROOM_B", "Study Room B", "Room", "R_B_10012", "available"),
        ("MEETING_1", "Meeting Room 1", "Room", "R_B_10012", "available"),
        ("PROJECTOR_1", "Portable Projector 1", "Equipment", "H_B_10012", "available"),
        ("CHAIR_H1", "Hallway Chair 1", "Chair", "H_A_09001", "available"),
        ("CHAIR_H2", "Hallway Chair 2", "Chair", "H_A_09001", "available"),
    ]

    with con:
        for asset in assets:
            con.execute(
                """
                INSERT OR IGNORE INTO assets
                (asset_id, asset_name, asset_type, location_id, status)
                VALUES (?, ?, ?, ?, ?)
                """,
                asset,
            )


def fetch_submissions(con: sqlite3.Connection) -> pd.DataFrame:
    """Retrieve all issue submissions."""
    return pd.read_sql("SELECT * FROM submissions", con)


def fetch_status_log(con: sqlite3.Connection) -> pd.DataFrame:
    """Retrieve status change audit log (most recent first)."""
    return pd.read_sql(
        """
        SELECT submission_id, old_status, new_status, changed_at
        FROM status_log
        ORDER BY changed_at DESC
        """,
        con,
    )


def fetch_report_log(con: sqlite3.Connection, report_type: str) -> pd.DataFrame:
    """Retrieve report sending history for a report type."""
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
    """Retrieve all assets."""
    return pd.read_sql(
        """
        SELECT asset_id, asset_name, asset_type, location_id, status
        FROM assets
        ORDER BY asset_type, asset_name
        """,
        con,
    )


def fetch_assets_in_room(con: sqlite3.Connection, room_location_id: str) -> list[str]:
    """Retrieve asset IDs located inside a specific room (excluding the room itself)."""
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
    """Log that a report has been sent to prevent duplicates."""
    with con:
        con.execute(
            "INSERT INTO report_log (report_type, sent_at) VALUES (?, ?)",
            (report_type, now_zurich_str()),
        )


# ============================================================================
# BOOKING SYSTEM FUNCTIONS
# ============================================================================
def sync_asset_statuses_from_bookings(con: sqlite3.Connection) -> None:
    """Update asset statuses based on active bookings."""
    now_iso = now_zurich().isoformat(timespec="seconds")

    with con:
        con.execute("UPDATE assets SET status = 'available'")

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

    with con:
        for _, row in active.iterrows():
            asset_id = row["asset_id"]
            asset_type = row["asset_type"]
            location_id = row["location_id"]

            con.execute("UPDATE assets SET status = 'booked' WHERE asset_id = ?", (asset_id,))

            if asset_type == "Room" and is_room_location(location_id):
                for aid in fetch_assets_in_room(con, location_id):
                    con.execute("UPDATE assets SET status = 'booked' WHERE asset_id = ?", (aid,))


def is_asset_available(con: sqlite3.Connection, asset_id: str, start_time: datetime, end_time: datetime) -> bool:
    """Check if an asset is available during a specified time period."""
    count = con.execute(
        """
        SELECT COUNT(*) FROM bookings
        WHERE asset_id = ?
          AND start_time < ?
          AND end_time > ?
        """,
        (asset_id, end_time.isoformat(timespec="seconds"), start_time.isoformat(timespec="seconds")),
    ).fetchone()[0]
    return count == 0


def fetch_future_bookings(con: sqlite3.Connection, asset_id: str) -> pd.DataFrame:
    """Retrieve upcoming bookings for a specific asset."""
    now_iso = now_zurich().isoformat(timespec="seconds")
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
    """Find the next available time for a currently booked asset."""
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
    """Update issue status and assignment with audit logging."""
    updated_at = now_zurich_str()
    set_resolved_at = (new_status == "Resolved")

    with con:
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

        if new_status != old_status:
            con.execute(
                """
                INSERT INTO status_log (submission_id, old_status, new_status, changed_at)
                VALUES (?, ?, ?, ?)
                """,
                (int(issue_id), old_status, new_status, updated_at),
            )


def insert_submission(con: sqlite3.Connection, sub: Submission) -> None:
    """Insert a new issue submission into the database."""
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
def send_email(to_email: str, subject: str, body: str, *, config: AppConfig) -> tuple[bool, str]:
    """Send email with proper error handling."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.from_email
    msg["To"] = to_email
    msg.set_content(body)

    recipients = [to_email]
    if config.admin_inbox:
        recipients.append(config.admin_inbox)

    try:
        with smtplib.SMTP(config.smtp_server, config.smtp_port, timeout=10) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(config.smtp_username, config.smtp_password)
            smtp.send_message(msg, to_addrs=recipients)
        return True, "Email sent successfully."
    except Exception as exc:
        logger.exception("Email sending failed")
        if config.debug:
            return False, f"Email could not be sent: {exc}"
        return False, "Email could not be sent due to a technical issue."


def send_admin_report_email(subject: str, body: str, *, config: AppConfig) -> tuple[bool, str]:
    """Send report email to admin inbox only."""
    if not config.admin_inbox:
        return False, "ADMIN_INBOX is not configured."

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.from_email
    msg["To"] = config.admin_inbox
    msg.set_content(body)

    try:
        with smtplib.SMTP(config.smtp_server, config.smtp_port, timeout=10) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(config.smtp_username, config.smtp_password)
            smtp.send_message(msg, to_addrs=[config.admin_inbox])
        return True, "Report email sent successfully."
    except Exception as exc:
        logger.exception("Report email sending failed")
        if config.debug:
            return False, f"Report email could not be sent: {exc}"
        return False, "Report email could not be sent due to a technical issue."


def confirmation_email_text(recipient_name: str, importance: str) -> tuple[str, str]:
    """Generate confirmation email content for new issue submissions."""
    subject = "Reporting Tool @ HSG: Issue Received"
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
Service Team
"""
    return subject, body


def resolved_email_text(recipient_name: str) -> tuple[str, str]:
    """Generate resolution notification email content."""
    subject = "Reporting Tool @ HSG: Issue Resolved"
    body = f"""Hello {recipient_name},

We are pleased to inform you that the issue you reported via the Reporting Tool @ HSG has been resolved.

Kind regards,
Service Team
"""
    return subject, body


# ============================================================================
# REPORTING FUNCTIONS
# ============================================================================
def build_weekly_report(df_all: pd.DataFrame) -> tuple[str, str]:
    """Generate weekly summary report content."""
    now_dt = now_zurich()
    since_dt = now_dt - timedelta(days=7)

    df = df_all.copy()
    df["created_at_dt"] = pd.to_datetime(df.get("created_at"), errors="coerce")
    df["resolved_at_dt"] = pd.to_datetime(df.get("resolved_at"), errors="coerce")

    new_last_7d = df[df["created_at_dt"] >= since_dt]
    resolved_last_7d = df[(df["resolved_at_dt"].notna()) & (df["resolved_at_dt"] >= since_dt)]
    open_issues = df[df["status"] != "Resolved"]

    subject = f"Reporting Tool – Weekly Summary ({now_dt.strftime('%Y-%m-%d')})"
    body = (
        "Weekly summary (last 7 days):\n"
        f"- New issues: {len(new_last_7d)}\n"
        f"- Resolved issues: {len(resolved_last_7d)}\n"
        f"- Open issues (current): {len(open_issues)}\n\n"
        "Top issue types (open):\n"
    )

    if not open_issues.empty:
        top_types = open_issues["issue_type"].value_counts().head(5)
        for issue_type, count in top_types.items():
            body += f"- {issue_type}: {count}\n"
    else:
        body += "- n/a\n"

    body += "\nThis email was generated by the Reporting Tool @ HSG."
    return subject, body


def send_weekly_report_if_due(con: sqlite3.Connection, *, config: AppConfig) -> None:
    """Check if weekly report is due and send it."""
    if not config.auto_weekly_report:
        return

    now_dt = now_zurich()
    if now_dt.weekday() != config.report_weekday or now_dt.hour != config.report_hour:
        return

    log_df = fetch_report_log(con, "weekly")
    if not log_df.empty:
        last_sent = iso_to_dt(str(log_df.iloc[0]["sent_at"]))
        if last_sent is not None and last_sent.date() == now_dt.date():
            return

    df_all = fetch_submissions(con)
    subject, body = build_weekly_report(df_all)
    ok, _ = send_admin_report_email(subject, body, config=config)
    if ok:
        mark_report_sent(con, "weekly")


# ============================================================================
# UI HELPER FUNCTIONS
# ============================================================================
def show_errors(errors: Iterable[str]) -> None:
    """Display validation errors to the user."""
    for msg in errors:
        st.error(msg)


def show_logo() -> None:
    """Display logo in sidebar with graceful fallback."""
    try:
        st.sidebar.image(LOGO_PATH, width=170, use_container_width=False)
    except FileNotFoundError:
        st.sidebar.warning("Logo image not found. Ensure the logo file is in the repository root.")


def render_map_iframe() -> None:
    """Display interactive campus map in a collapsible section."""
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
    """Convert location ID to human-readable label."""
    return LOCATIONS.get(str(loc_id), {}).get("label", "Unknown location")


def asset_display_label(row: pd.Series) -> str:
    """Generate descriptive label for asset selection dropdown."""
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
    """Format booking data for user-friendly display."""
    if df.empty:
        return df

    out = df.copy()
    out["start_time"] = pd.to_datetime(out.get("start_time"), errors="coerce")
    out["end_time"] = pd.to_datetime(out.get("end_time"), errors="coerce")

    out = out.dropna(subset=["start_time", "end_time"]).sort_values(by=["start_time"])
    out["start_time"] = out["start_time"].dt.strftime("%Y-%m-%d %H:%M")
    out["end_time"] = out["end_time"].dt.strftime("%Y-%m-%d %H:%M")

    return out.rename(columns={"user_name": "User", "start_time": "Start Time", "end_time": "End Time"})


def truncate_text(value: str, max_chars: int = DESCRIPTION_PREVIEW_CHARS) -> str:
    """Create a stable preview for long text fields in tables."""
    text = (value or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"

def bordered_container(*, key: str) -> st.delta_generator.DeltaGenerator:
    """Create a visually grouped container with a subtle border.

    Why:
    - Improves visual hierarchy without custom CSS
    - Makes the form feel like a card / panel
    - Streamlit-native (robust for grading & deployment)
    """
    return st.container(border=True, key=key)


# ============================================================================
# APPLICATION PAGES
# ============================================================================
def page_submission_form(con: sqlite3.Connection, *, config: AppConfig) -> None:
    """Submission page with the requested order, compact + user-friendly + framed."""
    st.header("📝 Report a Facility Issue")
    st.caption("Fields marked with * are mandatory.")

    # Stable defaults (prevents KeyErrors on first render; keeps reruns predictable)
    st.session_state.setdefault("issue_name", "")
    st.session_state.setdefault("issue_email", "")
    st.session_state.setdefault("issue_room", "")
    st.session_state.setdefault("issue_type", ISSUE_TYPES[0])
    st.session_state.setdefault("issue_priority", "Low")
    st.session_state.setdefault("issue_description", "")

    # ✅ Visual frame around the whole user flow
    with bordered_container(key="issue_form_card"):
        # 1) Your information
        st.subheader("👤 Your Information")
        c1, c2 = st.columns(2)
        with c1:
            st.text_input("Name*", placeholder="e.g., Max Muster", key="issue_name")
        with c2:
            st.text_input(
                "Email Address*",
                placeholder="firstname.lastname@student.unisg.ch",
                key="issue_email",
                help="Must be @unisg.ch or @student.unisg.ch",
            )

        # 2) Issue details (includes: room, type, priority, description)
        st.subheader("📋 Issue Details")

        c3, c4 = st.columns(2)
        with c3:
            room_raw = st.text_input("Room Number*", placeholder="e.g., A 09-001", key="issue_room").strip()
            if room_raw:
                normalized = normalize_room(room_raw)
                if normalized != room_raw:
                    st.caption(f"Saved as: **{normalized}**")
        with c4:
            st.selectbox("Issue Type*", ISSUE_TYPES, key="issue_type")

        st.selectbox(
            "Priority Level*",
            options=IMPORTANCE_LEVELS,
            key="issue_priority",
            help="Used to determine the SLA target handling time.",
        )
        sla_hours = SLA_HOURS_BY_IMPORTANCE.get(str(st.session_state["issue_priority"]))
        sla_part = f"SLA: {sla_hours}h" if sla_hours is not None else "SLA: n/a"
        st.caption(f"{sla_part}")
        
        desc = st.text_area(
            "Problem Description*",
            max_chars=500,
            placeholder="What happened? Where exactly? Since when? Any impact?",
            height=110,
            key="issue_description",
        ).strip()
        st.caption(f"Please limit your description to 500 characters.")

        # 5) Upload photo
        st.subheader("📸 Upload Photo")
        uploaded_file = st.file_uploader(
            "Optional: add a photo (jpg / png)",
            type=["jpg", "jpeg", "png"],
            help="Avoid personal data in the photo where possible.",
            key="issue_photo",
        )
        if uploaded_file is not None:
            st.image(uploaded_file, caption="Preview", use_container_width=True)

        # 6) Map (kept in required order; already collapsible)
        render_map_iframe()

        # 7) Submit button (last)
        submitted = st.button("🚀 Submit Issue Report", type="primary", use_container_width=True)

    # --- Submit handling stays outside the container (logic stays identical)
    if not submitted:
        return

    sub = Submission(
        name=str(st.session_state["issue_name"]).strip(),
        hsg_email=str(st.session_state["issue_email"]).strip().lower(),
        issue_type=str(st.session_state["issue_type"]),
        room_number=normalize_room(str(st.session_state["issue_room"])),
        importance=str(st.session_state["issue_priority"]),
        user_comment=str(st.session_state["issue_description"]).strip(),
    )

    errors = validate_submission_input(sub)
    if errors:
        show_errors(errors)
        return

    try:
        insert_submission(con, sub)
    except Exception as e:
        st.error("Database error while saving your report. Please try again.")
        logger.error("Failed to insert submission: %s", e)
        return

    subject, body = confirmation_email_text(sub.name.strip(), sub.importance)
    ok, msg = send_email(sub.hsg_email, subject, body, config=config)

    st.success("✅ Issue reported successfully!")
    if ok:
        st.balloons()
    else:
        st.warning(f"Note: {msg}")

    for k in [
        "issue_name",
        "issue_email",
        "issue_room",
        "issue_type",
        "issue_priority",
        "issue_description",
        "issue_photo",
    ]:
        st.session_state.pop(k, None)


def build_display_table(df: pd.DataFrame) -> pd.DataFrame:
    """Format submissions data for user-friendly display."""
    display_df = df.copy()
    display_df["user_comment_preview"] = display_df["user_comment"].astype(str).apply(truncate_text)

    display_df = display_df.rename(
        columns={
            "id": "ID",
            "name": "Reporter Name",
            "hsg_email": "Email",
            "issue_type": "Issue Type",
            "room_number": "Room Number",
            "importance": "Priority",
            "status": "Status",
            "user_comment_preview": "Description",
            "created_at": "Submitted",
            "updated_at": "Last Updated",
            "assigned_to": "Assigned To",
            "resolved_at": "Resolved At",
            "expected_resolved_at": "SLA Target",
        }
    )

    importance_order = {"High": 0, "Medium": 1, "Low": 2}
    display_df["_priority_rank"] = display_df["Priority"].map(importance_order).fillna(99).astype(int)

    display_df = (
        display_df.sort_values(by=["_priority_rank", "Submitted"], ascending=[True, False])
        .drop(columns=["_priority_rank"])
    )

    display_df = display_df.drop(columns=["user_comment"], errors="ignore")
    return display_df


def render_charts(df: pd.DataFrame) -> None:
    """Render lightweight analytics using built-in Streamlit charts."""
    if df.empty:
        st.info("No data available for charts.")
        return

    df_local = df.copy()
    df_local["created_at_dt"] = pd.to_datetime(df_local.get("created_at"), errors="coerce")

    tab1, tab2, tab3, tab4 = st.tabs(
        ["📊 Issue Types", "📅 Daily Trends", "🎯 Priority Levels", "📈 Status Distribution"]
    )

    with tab1:
        st.subheader("Issues by Type")
        issue_counts = df_local["issue_type"].value_counts().reindex(ISSUE_TYPES, fill_value=0)
        st.bar_chart(issue_counts)

    with tab2:
        st.subheader("Submission Trends")
        df_dates = df_local.dropna(subset=["created_at_dt"]).copy()
        if df_dates.empty:
            st.info("No valid submission dates available.")
        else:
            df_dates["date"] = df_dates["created_at_dt"].dt.date
            daily_counts = df_dates.groupby("date").size()
            st.line_chart(daily_counts)

    with tab3:
        st.subheader("Priority Distribution")
        imp_counts = df_local["importance"].value_counts().reindex(IMPORTANCE_LEVELS, fill_value=0)
        st.bar_chart(imp_counts)

    with tab4:
        st.subheader("Status Overview")
        status_counts = df_local["status"].value_counts().reindex(STATUS_LEVELS, fill_value=0)
        st.bar_chart(status_counts)


def page_submitted_issues(con: sqlite3.Connection) -> None:
    """Display submitted issues with filtering, quick-detail view, and analytics."""
    st.header("📋 Submitted Issues Dashboard")
    st.caption("All times are Europe/Zurich.")

    try:
        df = fetch_submissions(con)
    except Exception as e:
        st.error(f"Failed to load submissions: {e}")
        logger.error("Database error in submitted issues: %s", e)
        return

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
        high_priority = len(df[df["importance"] == "High"]) if not df.empty else 0
        st.metric("High Priority", high_priority)

    if df.empty:
        st.info("No issues have been submitted yet.")
        return

    st.subheader("🔍 Filter Options")
    col_filter1, col_filter2, col_filter3 = st.columns([1, 1, 1])

    with col_filter1:
        status_filter = st.multiselect(
            "Status",
            options=STATUS_LEVELS,
            default=["Pending", "In Progress"],
            help="Select statuses to display",
        )

    with col_filter2:
        importance_filter = st.multiselect(
            "Priority",
            options=IMPORTANCE_LEVELS,
            default=IMPORTANCE_LEVELS,
            help="Select priority levels to display",
        )

    with col_filter3:
        issue_type_filter = st.multiselect(
            "Issue Type",
            options=ISSUE_TYPES,
            default=ISSUE_TYPES,
            help="Select issue types to display",
        )

    col_filter4, col_filter5 = st.columns([1, 2])
    with col_filter4:
        open_only = st.toggle(
            "Open issues only",
            value=False,
            help="When enabled, hides resolved issues regardless of Status filter.",
        )

    with col_filter5:
        date_range_label_to_days = {
            "Last 7 days": 7,
            "Last 30 days": 30,
            "Last 90 days": 90,
            "All time": None,
        }
        date_range_choice = st.selectbox(
            "Date range (by submitted date)",
            options=list(date_range_label_to_days.keys()),
            index=1,
        )

    filtered_df = df[
        df["status"].isin(status_filter)
        & df["importance"].isin(importance_filter)
        & df["issue_type"].isin(issue_type_filter)
    ].copy()

    if open_only:
        filtered_df = filtered_df[filtered_df["status"] != "Resolved"].copy()

    days = date_range_label_to_days[date_range_choice]
    if days is not None:
        cutoff = now_zurich() - timedelta(days=int(days))
        filtered_df["created_at_dt"] = pd.to_datetime(filtered_df.get("created_at"), errors="coerce")
        filtered_df = filtered_df[
            filtered_df["created_at_dt"].notna() & (filtered_df["created_at_dt"] >= cutoff)
        ].copy()
        filtered_df = filtered_df.drop(columns=["created_at_dt"], errors="ignore")

    if filtered_df.empty:
        st.info("No issues match the selected filters.")
        return

    def _sla_target_row(r: pd.Series) -> str | None:
        dt_target = expected_resolution_dt(str(r.get("created_at", "")), str(r.get("importance", "")))
        return dt_target.isoformat(timespec="seconds") if dt_target is not None else None

    filtered_df["expected_resolved_at"] = filtered_df.apply(_sla_target_row, axis=1)

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

    st.subheader("🧾 Quick Issue Details")
    issue_ids = filtered_df["id"].astype(int).tolist()
    selected_issue_id = st.selectbox(
        "Select an issue to view full details:",
        options=issue_ids,
        index=0,
        format_func=lambda i: f"#{i}",
    )
    selected_row = filtered_df[filtered_df["id"].astype(int) == int(selected_issue_id)].iloc[0]

    with st.expander("View full details", expanded=True):
        col_a, col_b, col_c = st.columns(3)
        with col_a:
            st.write("**Issue Type:**", selected_row.get("issue_type", ""))
            st.write("**Room:**", selected_row.get("room_number", ""))
        with col_b:
            st.write("**Priority:**", selected_row.get("importance", ""))
            st.write("**Status:**", selected_row.get("status", ""))
        with col_c:
            st.write("**Assigned To:**", selected_row.get("assigned_to", "Unassigned") or "Unassigned")
            st.write("**Submitted:**", selected_row.get("created_at", ""))

        st.divider()
        st.write("**Reporter:**", selected_row.get("name", ""))
        st.write("**Email:**", selected_row.get("hsg_email", ""))
        st.write("**Description:**")
        st.write(selected_row.get("user_comment", ""))

    st.subheader(f"📊 Results ({len(filtered_df)} issues)")
    display_df = build_display_table(filtered_df)

    column_config = {
        "ID": st.column_config.NumberColumn("ID", help="Unique issue identifier"),
        "Submitted": st.column_config.DatetimeColumn("Submitted", help="When the issue was submitted"),
        "Last Updated": st.column_config.DatetimeColumn("Last Updated", help="Last status/assignment update"),
        "Resolved At": st.column_config.DatetimeColumn("Resolved At", help="When the issue was marked resolved"),
        "SLA Target": st.column_config.DatetimeColumn("SLA Target", help="Expected resolution time based on SLA"),
        "Description": st.column_config.TextColumn(
            "Description",
            help="Preview only. Use 'Quick Issue Details' to read the full description.",
            width="large",
        ),
    }

    st.dataframe(
        display_df,
        use_container_width=True,
        hide_index=True,
        height=420,
        column_config=column_config,
    )

    st.subheader("💾 Export")
    col_export1, col_export2 = st.columns(2)

    with col_export1:
        csv_bytes = filtered_df.to_csv(index=False).encode("utf-8")
        st.download_button(
            "Download CSV",
            data=csv_bytes,
            file_name=f"issues_{now_zurich().strftime('%Y%m%d')}.csv",
            mime="text/csv",
            use_container_width=True,
        )

    with col_export2:
        if st.button("Refresh", use_container_width=True):
            st.rerun()

    st.subheader("📈 Visualizations")
    render_charts(filtered_df)

    with st.expander("📋 Status Change History"):
        try:
            log_df = fetch_status_log(con)
            if log_df.empty:
                st.info("No status changes recorded yet.")
            else:
                st.dataframe(log_df, use_container_width=True, hide_index=True)
        except Exception as e:
            st.error(f"Failed to load audit log: {e}")


def page_booking(con: sqlite3.Connection) -> None:
    """Display asset booking interface with availability checking."""
    st.header("📅 Book an Asset")
    st.caption("All times are Europe/Zurich.")

    try:
        sync_asset_statuses_from_bookings(con)
        assets_df = fetch_assets(con)
    except Exception as e:
        st.error(f"Failed to load assets: {e}")
        logger.error("Database error in booking page: %s", e)
        return

    if assets_df.empty:
        st.warning("No assets available for booking.")
        return

    st.subheader("🔍 Find Assets")

    col_search1, col_search2, col_search3 = st.columns([2, 1, 1])
    with col_search1:
        search_term = st.text_input(
            "Search",
            placeholder="e.g., projector, meeting room, chair...",
            help="Search by asset name, type, or location",
        ).strip().lower()

    with col_search2:
        type_filter = st.selectbox(
            "Asset Type",
            options=["All Types"] + sorted(assets_df["asset_type"].unique().tolist()),
        )

    with col_search3:
        availability_filter = st.selectbox(
            "Availability",
            options=["All", "Available Only", "Booked Only"],
        )

    view_df = assets_df.copy()
    view_df["location_label"] = view_df["location_id"].apply(location_label)
    view_df["display_label"] = view_df.apply(asset_display_label, axis=1)

    if type_filter != "All Types":
        view_df = view_df[view_df["asset_type"] == type_filter]

    if availability_filter == "Available Only":
        view_df = view_df[view_df["status"].astype(str).str.lower() == "available"]
    elif availability_filter == "Booked Only":
        view_df = view_df[view_df["status"].astype(str).str.lower() == "booked"]

    if search_term:
        mask = (
            view_df["asset_name"].str.lower().str.contains(search_term, na=False)
            | view_df["asset_type"].str.lower().str.contains(search_term, na=False)
            | view_df["location_label"].str.lower().str.contains(search_term, na=False)
        )
        view_df = view_df[mask].copy()

    view_df["_status_rank"] = (
        view_df["status"].astype(str).str.lower().map({"available": 0, "booked": 1}).fillna(99).astype(int)
    )
    view_df = view_df.sort_values(by=["_status_rank", "asset_type", "asset_name"]).drop(columns=["_status_rank"])

    if view_df.empty:
        st.info("No assets match your search criteria.")
        return

    st.subheader("🎯 Select Asset")
    asset_labels = {str(r["asset_id"]): str(r["display_label"]) for _, r in view_df.iterrows()}

    default_asset_id = st.session_state.get("booking_asset_id")
    if default_asset_id not in asset_labels:
        default_asset_id = list(asset_labels.keys())[0]

    asset_id = st.selectbox(
        "Choose asset:",
        options=list(asset_labels.keys()),
        index=list(asset_labels.keys()).index(default_asset_id),
        format_func=lambda aid: asset_labels[aid],
    )
    st.session_state["booking_asset_id"] = asset_id

    selected_asset = assets_df[assets_df["asset_id"] == asset_id].iloc[0]

    st.subheader("📋 Asset Details")
    col_details1, col_details2, col_details3 = st.columns(3)
    with col_details1:
        st.metric("Status", str(selected_asset["status"]).capitalize())
    with col_details2:
        st.metric("Type", selected_asset["asset_type"])
    with col_details3:
        st.metric("Location", location_label(str(selected_asset["location_id"])))

    if str(selected_asset["status"]).lower() == "available":
        st.success("✅ This asset is available for booking.")
    else:
        next_free = next_available_time(con, asset_id)
        if next_free:
            st.warning(f"⛔ Currently booked. Next available: **{next_free.strftime('%Y-%m-%d %H:%M')}**")
        else:
            st.warning("⛔ Currently booked. No future bookings found.")

    st.subheader("📅 Upcoming Bookings")
    try:
        future_bookings = fetch_future_bookings(con, asset_id)
        if future_bookings.empty:
            st.info("No upcoming bookings scheduled.")
        else:
            st.dataframe(format_booking_table(future_bookings), use_container_width=True, hide_index=True)
    except Exception as e:
        st.error(f"Failed to load bookings: {e}")

    if str(selected_asset["status"]).lower() != "available":
        st.info("Select an available asset to create a booking.")
        return

    st.divider()
    st.subheader("📝 Create New Booking")

    with st.form("booking_form"):
        user_name = st.text_input("Your Name*", placeholder="e.g., Max Muster").strip()

        col_time1, col_time2, col_time3 = st.columns(3)
        with col_time1:
            start_date = st.date_input(
                "Start Date*",
                value=now_zurich().date(),
                min_value=now_zurich().date(),
            )

        with col_time2:
            current_time = now_zurich().time()
            rounded_minute = 30 * ((current_time.minute + 14) // 30)
            if rounded_minute == 60:
                default_time = current_time.replace(
                    hour=min(current_time.hour + 1, 23),
                    minute=0,
                    second=0,
                    microsecond=0,
                )
            else:
                default_time = current_time.replace(minute=rounded_minute, second=0, microsecond=0)

            start_time = st.time_input(
                "Start Time*",
                value=default_time,
                step=1800,
                help="30-minute intervals",
            )

        with col_time3:
            duration_options = {"1 hour": 1, "2 hours": 2, "3 hours": 3, "4 hours": 4, "6 hours": 6, "8 hours": 8}
            duration_choice = st.selectbox("Duration*", options=list(duration_options.keys()))
            duration_hours = duration_options[duration_choice]

        start_dt = safe_localize(datetime.combine(start_date, start_time))
        end_dt = start_dt + timedelta(hours=duration_hours)

        if start_dt < now_zurich():
            st.warning("Selected start time is in the past. Please choose a later time.")

        st.info(
            f"**Booking Summary:**\n"
            f"- **Asset:** {selected_asset['asset_name']}\n"
            f"- **Date:** {start_dt.strftime('%A, %d %B %Y')}\n"
            f"- **Time:** {start_dt.strftime('%H:%M')} → {end_dt.strftime('%H:%M')}\n"
            f"- **Duration:** {duration_hours} hour{'s' if duration_hours > 1 else ''}"
        )

        submitted = st.form_submit_button("✅ Confirm Booking", type="primary", use_container_width=True)

    if not submitted:
        return

    if not user_name:
        st.error("Please enter your name.")
        return

    if start_dt < now_zurich():
        st.error("Start time cannot be in the past.")
        return

    if end_dt <= start_dt:
        st.error("End time must be after start time.")
        return

    try:
        if not is_asset_available(con, asset_id, start_dt, end_dt):
            st.error("This asset is already booked during the selected time period.")
            return
    except Exception as e:
        st.error(f"Availability check failed: {e}")
        logger.error("Availability check error: %s", e)
        return

    try:
        with con:
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

        sync_asset_statuses_from_bookings(con)

        st.success(
            f"🎉 **Booking Confirmed!**\n\n"
            f"- Asset: {selected_asset['asset_name']}\n"
            f"- Date: {start_dt.strftime('%A, %d %B %Y')}\n"
            f"- Time: {start_dt.strftime('%H:%M')} - {end_dt.strftime('%H:%M')}\n"
            f"- Duration: {duration_hours} hour{'s' if duration_hours > 1 else ''}"
        )
        st.rerun()

    except Exception as e:
        st.error(f"Failed to create booking: {e}")
        logger.error("Booking creation error: %s", e)


def page_assets(con: sqlite3.Connection) -> None:
    """Display asset tracking and management interface."""
    st.header("📍 Asset Tracking")

    try:
        df = fetch_assets(con)
    except Exception as e:
        st.error(f"Failed to load assets: {e}")
        logger.error("Database error in asset tracking: %s", e)
        return

    if df.empty:
        st.info("No assets available in the system.")
        return

    df = df.copy()
    df["location_label"] = df["location_id"].apply(location_label)

    st.subheader("🔍 Filter Assets")
    col_filter1, col_filter2 = st.columns(2)

    with col_filter1:
        location_filter = st.multiselect(
            "Location",
            options=sorted(df["location_label"].unique()),
            default=sorted(df["location_label"].unique()),
        )

    with col_filter2:
        status_filter = st.multiselect(
            "Status",
            options=sorted(df["status"].unique()),
            default=sorted(df["status"].unique()),
        )

    filtered_df = df[(df["location_label"].isin(location_filter)) & (df["status"].isin(status_filter))]

    st.subheader("📦 Assets by Location")
    if filtered_df.empty:
        st.info("No assets match the selected filters.")
    else:
        for location, group in filtered_df.groupby("location_label"):
            with st.expander(f"🏢 {location} ({len(group)} assets)", expanded=False):
                display_data = group[["asset_id", "asset_name", "asset_type", "status"]].copy()
                display_data = display_data.rename(
                    columns={"asset_id": "ID", "asset_name": "Name", "asset_type": "Type", "status": "Status"}
                )
                st.dataframe(display_data, use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("🚚 Move Asset to New Location")

    assets_df = fetch_assets(con).copy()
    assets_df["location_label"] = assets_df["location_id"].apply(location_label)
    assets_df["display_label"] = assets_df.apply(asset_display_label, axis=1)

    asset_options = {str(r["asset_id"]): str(r["display_label"]) for _, r in assets_df.iterrows()}
    if not asset_options:
        st.info("No assets available for movement.")
        return

    asset_id = st.selectbox(
        "Select asset to move:",
        options=list(asset_options.keys()),
        format_func=lambda aid: asset_options[aid],
    )

    selected_asset = assets_df[assets_df["asset_id"] == asset_id].iloc[0]
    col_current1, col_current2, col_current3 = st.columns(3)
    with col_current1:
        st.metric("Current Status", str(selected_asset["status"]).capitalize())
    with col_current2:
        st.metric("Asset Type", selected_asset["asset_type"])
    with col_current3:
        st.metric("Current Location", str(selected_asset["location_label"]))

    new_location_id = st.selectbox(
        "New location:",
        options=list(LOCATIONS.keys()),
        format_func=lambda x: LOCATIONS[x]["label"],
    )

    if st.button("Move asset", type="primary", use_container_width=True):
        if new_location_id == selected_asset["location_id"]:
            st.warning("Asset is already at this location.")
        else:
            try:
                with con:
                    con.execute("UPDATE assets SET location_id = ? WHERE asset_id = ?", (new_location_id, asset_id))

                st.success(
                    "✅ Move request submitted successfully!\n\n"
                    f"**Asset:** {selected_asset['asset_name']}\n"
                    f"**From:** {selected_asset['location_label']}\n"
                    f"**To:** {LOCATIONS[new_location_id]['label']}"
                )
                st.rerun()
            except Exception as e:
                st.error(f"Failed to move asset: {e}")
                logger.error("Asset movement error: %s", e)


def page_overwrite_status(con: sqlite3.Connection, *, config: AppConfig) -> None:
    """Admin interface for managing issue statuses and assignments (password protected)."""
    st.header("🔧 Admin Panel - Issue Management")

    entered_password = st.text_input("Enter Admin Password", type="password")
    if entered_password != config.admin_password:
        st.info("🔐 Please enter the admin password to continue.")
        return

    st.subheader("⚡ Quick Actions")
    col_action1, col_action2 = st.columns(2)

    with col_action1:
        if st.button("Send weekly report now", use_container_width=True):
            try:
                df_all = fetch_submissions(con)
                subject, body = build_weekly_report(df_all)
                ok, msg = send_admin_report_email(subject, body, config=config)
                if ok:
                    mark_report_sent(con, "weekly_manual")
                    st.success("Weekly report sent successfully!")
                else:
                    st.warning(f"Report sending failed: {msg}")
            except Exception as e:
                st.error(f"Failed to send report: {e}")

    with col_action2:
        if st.button("Refresh", use_container_width=True):
            st.rerun()

    try:
        df = fetch_submissions(con)
    except Exception as e:
        st.error(f"Failed to load issues: {e}")
        return

    if df.empty:
        st.info("No issues available for management.")
        return

    st.subheader("🔍 Filter Issues")
    admin_status_filter = st.multiselect(
        "Show issues with status:",
        options=STATUS_LEVELS,
        default=["Pending", "In Progress"],
    )

    filtered_df = df[df["status"].isin(admin_status_filter)]
    if filtered_df.empty:
        st.info("No issues match the selected filters.")
        return

    st.subheader("🎯 Select Issue to Update")
    issue_options = {
        row["id"]: f"#{row['id']}: {row['issue_type']} ({row['room_number']}) - {row['status']}"
        for _, row in filtered_df.iterrows()
    }

    selected_id = st.selectbox("Choose issue:", options=list(issue_options.keys()), format_func=lambda x: issue_options[x])
    row = df[df["id"] == selected_id].iloc[0]

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
        st.metric("Assigned To", row.get("assigned_to", "Unassigned") or "Unassigned")
        st.metric("Room", row["room_number"])

    with st.expander("📝 View Full Details", expanded=False):
        st.write("**Reporter:**", row["name"])
        st.write("**Email:**", row["hsg_email"])
        st.write("**Issue Type:**", row["issue_type"])
        st.write("**Submitted:**", row["created_at"])
        st.write("**Last Updated:**", row["updated_at"])
        st.write("**Resolved At:**", row.get("resolved_at", "Not resolved"))
        st.write("**Description:**", row["user_comment"])

    st.divider()
    st.subheader("✏️ Update Issue")

    with st.form("admin_update_form"):
        current_assignee = str(row.get("assigned_to", "") or "")
        assignee_options = ["(Unassigned)"] + config.assignees
        assigned_to = st.selectbox(
            "Assign to:",
            options=assignee_options,
            index=assignee_options.index(current_assignee) if current_assignee in assignee_options else 0,
        )
        assigned_to_value = None if assigned_to == "(Unassigned)" else assigned_to

        new_status = st.selectbox(
            "Update status to:",
            STATUS_LEVELS,
            index=STATUS_LEVELS.index(row["status"]) if row["status"] in STATUS_LEVELS else 0,
        )

        confirm_resolution = True
        if new_status == "Resolved":
            confirm_resolution = st.checkbox(
                "✓ Confirm issue resolution (will send notification email)",
                value=False,
            )

        submitted = st.form_submit_button("Save changes", type="primary", use_container_width=True)

    if not submitted:
        return

    if new_status == "Resolved" and not confirm_resolution:
        st.error("Please confirm resolution before setting status to 'Resolved'.")
        return

    try:
        old_status = str(row["status"])
        update_issue_admin_fields(
            con=con,
            issue_id=int(selected_id),
            new_status=new_status,
            assigned_to=assigned_to_value,
            old_status=old_status,
        )

        if new_status == "Resolved":
            email_errors = validate_admin_email(str(row["hsg_email"]))
            if email_errors:
                show_errors(email_errors)
            else:
                subject, body = resolved_email_text(str(row["name"]).strip() or "there")
                ok, msg = send_email(str(row["hsg_email"]).strip(), subject, body, config=config)
                if ok:
                    st.success("✓ Resolution notification sent to reporter.")
                else:
                    st.warning(f"Notification email failed: {msg}")

        st.success("✅ Issue updated successfully!")
        st.rerun()

    except Exception as e:
        st.error(f"Failed to update issue: {e}")
        logger.error("Admin update error: %s", e)


def page_overview_dashboard(con: sqlite3.Connection) -> None:
    """Display overview dashboard with key metrics."""
    st.header("📊 Overview Dashboard")
    st.caption("Real-time overview of system status. All times are Europe/Zurich.")

    try:
        issues = fetch_submissions(con)
        assets = fetch_assets(con)
    except Exception as e:
        st.error(f"Failed to load data: {e}")
        logger.error("Dashboard data loading error: %s", e)
        return

    st.subheader("📈 Key Metrics")
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        st.metric("Total Issues", len(issues))
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

    tab1, tab2 = st.tabs(["📋 Issues Overview", "📦 Assets Overview"])

    with tab1:
        st.subheader("Current Issues")
        if issues.empty:
            st.info("No issues reported yet.")
        else:
            open_issues_df = issues[issues["status"] != "Resolved"]
            if not open_issues_df.empty:
                st.write(f"**Open Issues ({len(open_issues_df)}):**")
                display_open = open_issues_df[
                    ["id", "issue_type", "room_number", "importance", "status", "created_at"]
                ].copy()
                display_open = display_open.rename(
                    columns={
                        "id": "ID",
                        "issue_type": "Type",
                        "room_number": "Room",
                        "importance": "Priority",
                        "status": "Status",
                        "created_at": "Reported",
                    }
                )
                st.dataframe(display_open, use_container_width=True, hide_index=True)
            else:
                st.success("✅ All issues are resolved!")

            st.subheader("📊 Quick Statistics")
            col_stat1, col_stat2, col_stat3 = st.columns(3)
            with col_stat1:
                st.metric("High Priority", len(issues[issues["importance"] == "High"]))
            with col_stat2:
                created_dt = pd.to_datetime(issues.get("created_at"), errors="coerce")
                if created_dt.notna().any():
                    avg_age_days = (now_zurich() - created_dt).dt.days.mean()
                    st.metric("Avg. Issue Age", f"{avg_age_days:.1f} days")
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
            assets_display = assets.copy()
            assets_display["location"] = assets_display["location_id"].apply(location_label)

            st.dataframe(
                assets_display[["asset_id", "asset_name", "asset_type", "status", "location"]],
                use_container_width=True,
                hide_index=True,
                column_config={
                    "asset_id": "ID",
                    "asset_name": "Name",
                    "asset_type": "Type",
                    "status": "Status",
                    "location": "Location",
                },
            )

            st.subheader("📊 Asset Statistics")
            col_asset1, col_asset2, col_asset3 = st.columns(3)
            with col_asset1:
                st.metric("Asset Types", assets["asset_type"].nunique())
            with col_asset2:
                st.metric("Currently Booked", len(assets[assets["status"] == "booked"]))
            with col_asset3:
                top_location = assets["location_id"].mode()[0] if not assets.empty else ""
                st.metric("Busiest Location", location_label(top_location) if top_location else "N/A")


# ============================================================================
# MAIN APPLICATION
# ============================================================================
def main() -> None:
    """Main application entry point."""
    st.set_page_config(
        page_title="Reporting Tool @ HSG",
        page_icon="🏛️",
        layout="centered",
        initial_sidebar_state="expanded",
    )

    config = get_config()

    show_logo()
    st.sidebar.markdown("### Navigation")

    section = st.sidebar.radio(
        "Select section:",
        ["📋 Reporting Tool", "📅 Booking & Tracking", "📊 Overview"],
        label_visibility="collapsed",
    )

    if section == "📋 Reporting Tool":
        page = st.sidebar.selectbox(
            "Select page:",
            ["📝 Submit Issue", "📋 View Issues", "🔧 Admin Panel"],
            label_visibility="collapsed",
        )
        page_map = {
            "📝 Submit Issue": "Submission Form",
            "📋 View Issues": "Submitted Issues",
            "🔧 Admin Panel": "Overwrite Status",
        }
        current_page = page_map[page]
    elif section == "📅 Booking & Tracking":
        page = st.sidebar.selectbox(
            "Select page:",
            ["📅 Book Assets", "📍 Track Assets"],
            label_visibility="collapsed",
        )
        page_map = {"📅 Book Assets": "Booking", "📍 Track Assets": "Asset Tracking"}
        current_page = page_map[page]
    else:
        current_page = "Overview Dashboard"

    try:
        st.image(
            "campus_header.jpeg",
            caption="Campus of the University of St.Gallen (HSG), St.Gallen, Switzerland",
            use_container_width=True,
        )
    except FileNotFoundError:
        st.caption("Reporting Tool @ HSG")

    try:
        con = get_connection()
        init_db(con)
        migrate_db(con)
        init_booking_table(con)
        init_assets_table(con)
        seed_assets(con)
        sync_asset_statuses_from_bookings(con)
        send_weekly_report_if_due(con, config=config)
    except Exception as e:
        st.error(f"❌ Database initialization failed: {e}")
        logger.critical("Database initialization error: %s", e)
        return

    st.title("Reporting Tool @ HSG")
    st.caption("Facility issue reporting, booking, and tracking.")

    page_functions = {
        "Submission Form": lambda: page_submission_form(con, config=config),
        "Submitted Issues": lambda: page_submitted_issues(con),
        "Booking": lambda: page_booking(con),
        "Asset Tracking": lambda: page_assets(con),
        "Overview Dashboard": lambda: page_overview_dashboard(con),
        "Overwrite Status": lambda: page_overwrite_status(con, config=config),
    }

    if current_page in page_functions:
        page_functions[current_page]()
    else:
        st.error(f"Page '{current_page}' not found.")

    st.sidebar.markdown("---")
    st.sidebar.caption(f"© {datetime.now().year} University of St.Gallen")
    st.sidebar.caption(f"Last updated: {now_zurich().strftime('%Y-%m-%d %H:%M')}")


# ============================================================================
# APPLICATION ENTRY POINT
# ============================================================================
if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.critical("Application crashed: %s", e, exc_info=True)

        st.error(
            "⚠️ **Application Error**\n\n"
            "The application encountered an unexpected error. Try:\n"
            "1) Refresh the page\n"
            "2) Check your internet connection\n"
            "3) Contact support if the problem persists\n\n"
            "Error details (for administrators):\n"
            f"```\n{str(e)}\n```"
        )

        try:
            if get_config().debug:
                import traceback

                st.code(traceback.format_exc(), language="python")
        except Exception:
            pass
