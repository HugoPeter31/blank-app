"""
Reporting Tool at HSG (via Streamlit)
Developed by: Arthur Lavric & Fabio Patierno

Features (overview):
- Issue reporting form (facility issues) stored in SQLite
- Dashboard of submitted issues + charts + CSV export
- Admin page to update issue status (password protected) + audit log
- Booking page for bookable assets (rooms/equipment/furniture) stored in SQLite
- Asset tracking page (assets grouped by location; move assets between locations)

Note:
- Admin page is protected via Streamlit secrets (ADMIN_PASSWORD).
- Email sending requires SMTP secrets (see .streamlit/secrets.toml).
"""

from __future__ import annotations

# ----------------------------
# Imports
# ----------------------------
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Iterable

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd
import pytz
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

# Expected resolution time by importance (hours)
SLA_HOURS_BY_IMPORTANCE: dict[str, int] = {
    "High": 24,
    "Medium": 72,
    "Low": 120,
}

EMAIL_PATTERN = re.compile(r"^[\w.]+@(student\.)?unisg\.ch$")
ROOM_PATTERN = re.compile(r"^[A-Z] \d{2}-\d{3}$")

# Simple location model (for tracking). In a real system this could come from a map API.
LOCATIONS = {
    "R_A_09001": {"label": "Room A 09-001", "x": 10, "y": 20},
    "H_A_09001": {"label": "Hallway near Room A 09-001", "x": 15, "y": 25},
    "R_B_10012": {"label": "Room B 10-012", "x": 40, "y": 60},
    "H_B_10012": {"label": "Hallway near Room B 10-012", "x": 45, "y": 65},
}


# ----------------------------
# Logging
# ----------------------------
logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


# ----------------------------
# Data model
# ----------------------------
@dataclass(frozen=True)
class Submission:
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
    """Read a Streamlit secret (st.secrets)."""
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

ADMIN_PASSWORD = get_secret("ADMIN_PASSWORD")

DEBUG = get_secret("DEBUG", "0") == "1"

ASSIGNEES_RAW = get_secret("ASSIGNEES", "Facility Team")
ASSIGNEES = [a.strip() for a in ASSIGNEES_RAW.split(",") if a.strip()]

AUTO_WEEKLY_REPORT = get_secret("AUTO_WEEKLY_REPORT", "0") == "1"
REPORT_WEEKDAY = int(get_secret("REPORT_WEEKDAY", "0"))  # 0=Mon ... 6=Sun
REPORT_HOUR = int(get_secret("REPORT_HOUR", "7"))


# ----------------------------
# Time helpers
# ----------------------------
def now_zurich() -> datetime:
    """Current Zurich time (timezone-aware)."""
    return datetime.now(APP_TZ)


def now_zurich_str() -> str:
    """ISO 8601 timestamp with timezone offset."""
    return now_zurich().isoformat(timespec="seconds")


def iso_to_dt(value: str) -> datetime | None:
    """Parse ISO string to datetime; returns None if invalid."""
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def expected_resolution_dt(created_at_iso: str, importance: str) -> datetime | None:
    """created_at + SLA(importance)."""
    created_dt = iso_to_dt(created_at_iso)
    sla_hours = SLA_HOURS_BY_IMPORTANCE.get(importance)
    if created_dt is None or sla_hours is None:
        return None
    return created_dt + timedelta(hours=int(sla_hours))


# ----------------------------
# Validation
# ----------------------------
def valid_email(hsg_email: str) -> bool:
    return bool(EMAIL_PATTERN.fullmatch(hsg_email.strip()))


def valid_room_number(room_number: str) -> bool:
    return bool(ROOM_PATTERN.fullmatch(room_number.strip()))


def validate_submission_input(sub: Submission) -> list[str]:
    """Validate inputs for issue submission form."""
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
    """Validation used for admin-triggered emails."""
    if not email.strip():
        return ["Email address is required."]
    if not valid_email(email):
        return ["Please provide a valid HSG email address (…@unisg.ch or …@student.unisg.ch)."]
    return []


# ----------------------------
# Database: connection + schema
# ----------------------------
@st.cache_resource
def get_connection() -> sqlite3.Connection:
    """
    Create and cache a SQLite connection.
    (Streamlit reruns the script; caching prevents opening a new DB connection on every rerun.)
    """
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db(con: sqlite3.Connection) -> None:
    """Create core tables for the issue reporting module."""
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
            updated_at TEXT NOT NULL,
            assigned_to TEXT,
            resolved_at TEXT
        )
        """
    )

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS status_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            submission_id INTEGER NOT NULL,
            old_status TEXT NOT NULL,
            new_status TEXT NOT NULL,
            changed_at TEXT NOT NULL,
            FOREIGN KEY (submission_id) REFERENCES submissions(id)
        )
        """
    )

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS report_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_type TEXT NOT NULL,
            sent_at TEXT NOT NULL
        )
        """
    )

    con.commit()


def init_booking_table(con: sqlite3.Connection) -> None:
    """Decouples booking logic from issue reporting; demonstrates extension."""
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS bookings (
            booking_id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id TEXT NOT NULL,
            user_name TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    con.commit()


def init_assets_table(con: sqlite3.Connection) -> None:
    """Assets table used by BOTH booking and tracking."""
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS assets (
            asset_id TEXT PRIMARY KEY,
            asset_name TEXT NOT NULL,
            asset_type TEXT NOT NULL,
            location_id TEXT NOT NULL,
            status TEXT NOT NULL
        )
        """
    )
    con.commit()


def migrate_db(con: sqlite3.Connection) -> None:
    """Small migration helper for older DB files (submissions table only)."""
    cols = {row[1] for row in con.execute("PRAGMA table_info(submissions)").fetchall()}

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

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS status_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            submission_id INTEGER NOT NULL,
            old_status TEXT NOT NULL,
            new_status TEXT NOT NULL,
            changed_at TEXT NOT NULL,
            FOREIGN KEY (submission_id) REFERENCES submissions(id)
        )
        """
    )

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS report_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_type TEXT NOT NULL,
            sent_at TEXT NOT NULL
        )
        """
    )

    con.commit()


# ----------------------------
# Database: seed + fetch helpers
# ----------------------------
def seed_assets(con: sqlite3.Connection) -> None:
    """Insert a small set of demo assets (only if not present)."""
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
    return pd.read_sql("SELECT * FROM submissions", con)


def fetch_status_log(con: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql(
        """
        SELECT submission_id, old_status, new_status, changed_at
        FROM status_log
        ORDER BY changed_at DESC
        """,
        con,
    )


def fetch_report_log(con: sqlite3.Connection, report_type: str) -> pd.DataFrame:
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
    return pd.read_sql(
        """
        SELECT asset_id, asset_name, asset_type, location_id, status
        FROM assets
        ORDER BY asset_type, asset_name
        """,
        con,
    )


def mark_report_sent(con: sqlite3.Connection, report_type: str) -> None:
    con.execute(
        "INSERT INTO report_log (report_type, sent_at) VALUES (?, ?)",
        (report_type, now_zurich_str()),
    )
    con.commit()


# ----------------------------
# Booking helpers
# ----------------------------
def sync_asset_statuses_from_bookings(con: sqlite3.Connection) -> None:
    """Marks assets as booked if there is an active booking right now."""
    now_iso = now_zurich().isoformat(timespec="seconds")

    active = pd.read_sql(
        """
        SELECT DISTINCT asset_id
        FROM bookings
        WHERE start_time <= ? AND end_time > ?
        """,
        con,
        params=(now_iso, now_iso),
    )
    active_ids = set(active["asset_id"].tolist())

    # Reset booked -> available, then set active bookings -> booked
    con.execute("UPDATE assets SET status = 'available' WHERE status = 'booked'")
    con.commit()

    for aid in active_ids:
        con.execute("UPDATE assets SET status = 'booked' WHERE asset_id = ?", (aid,))
    con.commit()


def is_asset_available(con: sqlite3.Connection, asset_id: str, start_time: datetime, end_time: datetime) -> bool:
    """True if no overlapping booking exists."""
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
    """Upcoming bookings for the selected asset."""
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


# ----------------------------
# Issue admin helpers
# ----------------------------
def update_issue_admin_fields(
    con: sqlite3.Connection,
    issue_id: int,
    new_status: str,
    assigned_to: str | None,
    old_status: str,
) -> None:
    """Update status/assignment and write audit log (if status changes)."""
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
    """Insert issue submission."""
    created_at = now_zurich_str()
    updated_at = created_at

    with con:
        con.execute(
            """
            INSERT INTO submissions
            (name, hsg_email, issue_type, room_number, importance, status, user_comment, created_at, updated_at, assigned_to, resolved_at)
            VALUES (?, ?, ?, ?, ?, 'Pending', ?, ?, ?, NULL, NULL)
            """,
            (
                sub.name.strip(),
                sub.hsg_email.strip().lower(),
                sub.issue_type,
                sub.room_number.strip().upper(),
                sub.importance,
                sub.user_comment.strip(),
                created_at,
                updated_at,
            ),
        )


# ----------------------------
# Email helpers
# ----------------------------
def send_email(to_email: str, subject: str, body: str) -> tuple[bool, str]:
    """Best-effort email sending. Returns (ok, message)."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = FROM_EMAIL
    msg["To"] = to_email
    msg.set_content(body)

    recipients = [to_email] + ([ADMIN_INBOX] if ADMIN_INBOX else [])

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=10) as smtp:
            smtp.starttls()
            smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            smtp.send_message(msg, to_addrs=recipients)

        return True, "Email sent."
    except Exception as exc:
        logger.exception("Email sending failed")
        if DEBUG:
            return False, f"Email could not be sent: {exc}"
        return False, "Email could not be sent due to a technical issue."


def send_admin_report_email(subject: str, body: str) -> tuple[bool, str]:
    """Send report email to admin inbox."""
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
        return True, "Report email sent."
    except Exception as exc:
        logger.exception("Report email sending failed")
        if DEBUG:
            return False, f"Report email could not be sent: {exc}"
        return False, "Report email could not be sent due to a technical issue."


def confirmation_email_text(recipient_name: str, importance: str) -> tuple[str, str]:
    subject = "Issue received!"
    sla_hours = SLA_HOURS_BY_IMPORTANCE.get(importance)
    sla_text = f"Expected handling time (SLA): within {sla_hours} hours." if sla_hours is not None else "Expected handling time (SLA): n/a."

    body = f"""Dear {recipient_name},

Thank you for contacting us regarding your concern. We hereby confirm that we have received your issue report and that it is currently under review by the responsible team.

{sla_text}

We will keep you informed about the progress and notify you once the matter has been resolved.

Kind regards,
HSG Service Team
"""
    return subject, body


def resolved_email_text(recipient_name: str) -> tuple[str, str]:
    subject = "Issue resolved!"
    body = f"""Hello {recipient_name},

We are pleased to inform you that the issue you reported via the HSG Reporting Tool has been resolved.

Kind regards,
HSG Service Team
"""
    return subject, body


# ----------------------------
# Reporting helpers
# ----------------------------
def build_weekly_report(df_all: pd.DataFrame) -> tuple[str, str]:
    now_dt = now_zurich()
    since_dt = now_dt - timedelta(days=7)

    df = df_all.copy()
    df["created_at_dt"] = pd.to_datetime(df["created_at"], errors="coerce")
    df["resolved_at_dt"] = pd.to_datetime(df.get("resolved_at", pd.Series([None] * len(df))), errors="coerce")

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

    if not open_issues.empty:
        top_types = open_issues["issue_type"].value_counts().head(5)
        for issue_type, count in top_types.items():
            body += f"- {issue_type}: {count}\n"
    else:
        body += "- n/a\n"

    body += "\nThis email was generated by the HSG Reporting Tool."
    return subject, body


def send_weekly_report_if_due(con: sqlite3.Connection) -> None:
    """Runs when app is opened; sends report only if due."""
    if not AUTO_WEEKLY_REPORT:
        return

    now_dt = now_zurich()
    if now_dt.weekday() != REPORT_WEEKDAY or now_dt.hour != REPORT_HOUR:
        return

    log_df = fetch_report_log(con, "weekly")
    if not log_df.empty:
        last_sent = iso_to_dt(str(log_df.iloc[0]["sent_at"]))
        if last_sent is not None and last_sent.date() == now_dt.date():
            return

    df_all = fetch_submissions(con)
    subject, body = build_weekly_report(df_all)
    ok, _ = send_admin_report_email(subject, body)
    if ok:
        mark_report_sent(con, "weekly")


# ----------------------------
# UI helpers
# ----------------------------
def show_errors(errors: Iterable[str]) -> None:
    for msg in errors:
        st.error(msg)


def show_logo() -> None:
    try:
        st.sidebar.image(LOGO_PATH, width=170)
    except Exception:
        st.sidebar.info("Logo not found. Add 'HSG-logo-new.png' to the repository root.")


def render_map_iframe() -> None:
    st.markdown("**Map** (optional)")
    url = "https://use.mazemap.com/embed.html?v=1&zlevel=1&center=9.373611,47.429708&zoom=14.7&campusid=710"
    st.markdown(
        f"""
        <iframe src="{url}"
            width="100%" height="420" frameborder="0"
            marginheight="0" marginwidth="0" scrolling="no"></iframe>
        """,
        unsafe_allow_html=True,
    )


# ----------------------------
# Pages
# ----------------------------
def page_submission_form(con: sqlite3.Connection) -> None:
    st.header("Submission Form")
    st.info("Please use this form to report facility-related issues on campus.")

    with st.form("issue_form", clear_on_submit=True):
        name = st.text_input("Name*").strip()
        hsg_email = st.text_input("HSG Email Address*").strip()
        st.caption("Accepted emails: …@unisg.ch or …@student.unisg.ch")

        room_number = st.text_input("Room Number*").strip()
        st.caption("Room example: A 09-001")

        issue_type = st.selectbox("Issue Type*", ISSUE_TYPES)
        importance = st.selectbox("Importance*", IMPORTANCE_LEVELS)
        user_comment = st.text_area("Problem Description*", max_chars=500).strip()
        st.caption("Please be concise (max. 500 characters).")

        uploaded_file = st.file_uploader("Upload a Photo (optional)", type=["jpg", "jpeg", "png"])
        if uploaded_file is not None:
            st.image(uploaded_file, caption="Uploaded Photo (not stored)", use_container_width=True)

        sla_hours = SLA_HOURS_BY_IMPORTANCE.get(importance)
        if sla_hours is not None:
            st.info(f"Expected handling time: within {sla_hours} hours.")

        render_map_iframe()
        submitted = st.form_submit_button("Submit")

    if not submitted:
        return

    sub = Submission(
        name=name,
        hsg_email=hsg_email.strip().lower(),
        issue_type=issue_type,
        room_number=room_number.strip().upper(),
        importance=importance,
        user_comment=user_comment,
    )

    errors = validate_submission_input(sub)
    if errors:
        show_errors(errors)
        return

    insert_submission(con, sub)

    subject, body = confirmation_email_text(sub.name.strip(), sub.importance)
    ok, msg = send_email(sub.hsg_email, subject, body)
    st.success("Submission successful!")
    if not ok:
        st.warning(msg)


def build_display_table(df: pd.DataFrame) -> pd.DataFrame:
    display_df = df.copy().rename(
        columns={
            "id": "ID",
            "name": "NAME",
            "hsg_email": "HSG MAIL ADDRESS",
            "issue_type": "ISSUE TYPE",
            "room_number": "ROOM NR.",
            "importance": "IMPORTANCE",
            "status": "STATUS",
            "user_comment": "PROBLEM DESCRIPTION",
            "created_at": "SUBMITTED AT",
            "updated_at": "LAST UPDATED",
            "assigned_to": "ASSIGNED TO",
            "resolved_at": "RESOLVED AT",
            "expected_resolved_at": "SLA TARGET",
        }
    )

    importance_order = {"High": 0, "Medium": 1, "Low": 2}
    display_df["_imp_rank"] = display_df["IMPORTANCE"].map(importance_order).fillna(99).astype(int)

    display_df = display_df.sort_values(
        by=["ISSUE TYPE", "_imp_rank", "SUBMITTED AT"],
        ascending=[True, True, False],
    ).drop(columns=["_imp_rank"])

    return display_df


def render_charts(df: pd.DataFrame) -> None:
    st.subheader("Number of Issues by Issue Type")
    issue_counts = df["issue_type"].value_counts().reindex(ISSUE_TYPES, fill_value=0)
    fig, ax = plt.subplots()
    ax.barh(issue_counts.index, issue_counts.values)
    ax.set_xlabel("Number of Issues")
    ax.set_ylabel("Issue Type")
    st.pyplot(fig)

    st.subheader("Issues Submitted per Day")
    df_dates = df.copy()
    df_dates["created_at"] = pd.to_datetime(df_dates["created_at"], errors="coerce")
    df_dates = df_dates.dropna(subset=["created_at"])

    if df_dates.empty:
        st.info("No valid submission dates available for daily chart.")
    else:
        date_index = pd.date_range(
            start=df_dates["created_at"].min().date(),
            end=df_dates["created_at"].max().date(),
            freq="D",
        )
        per_day = (
            df_dates.groupby(df_dates["created_at"].dt.date)
            .size()
            .reindex(date_index.date, fill_value=0)
        )

        fig, ax = plt.subplots()
        ax.plot(date_index, per_day.values, marker="o")
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
        fig.autofmt_xdate(rotation=45)
        ax.set_xlabel("Date")
        ax.set_ylabel("Number of Issues Submitted")
        ax.grid(True, linestyle="--", alpha=0.4)
        st.pyplot(fig)

    st.subheader("Number of Issues by Importance Level")
    imp_counts = df["importance"].value_counts().reindex(IMPORTANCE_LEVELS, fill_value=0)
    fig, ax = plt.subplots()
    ax.bar(imp_counts.index, imp_counts.values)
    ax.set_xlabel("Importance Level")
    ax.set_ylabel("Number of Issues")
    st.pyplot(fig)

    st.subheader("Distribution of Statuses")
    status_counts = df["status"].value_counts().reindex(STATUS_LEVELS, fill_value=0)
    if status_counts.sum() == 0:
        st.info("No status data available for the selected filter.")
        return

    fig, ax = plt.subplots()
    ax.pie(status_counts.values, labels=status_counts.index, autopct="%1.1f%%", startangle=90)
    ax.axis("equal")
    st.pyplot(fig)


def page_submitted_issues(con: sqlite3.Connection) -> None:
    st.header("Submitted Issues")

    df = fetch_submissions(con)
    st.subheader(f"Total Issues: {len(df)}")

    if df.empty:
        st.info("No submitted issues yet. Please submit an issue first.")
        return

    status_filter = st.multiselect("Filter by status", options=STATUS_LEVELS, default=STATUS_LEVELS)
    df = df[df["status"].isin(status_filter)]
    if df.empty:
        st.info("No issues match the selected status filter.")
        return

    df_view = df.copy()
    df_view["expected_resolved_at"] = df_view.apply(
        lambda r: (
            expected_resolution_dt(str(r["created_at"]), str(r["importance"])).isoformat(timespec="seconds")
            if expected_resolution_dt(str(r["created_at"]), str(r["importance"])) is not None
            else None
        ),
        axis=1,
    )

    df_view["created_at_dt"] = pd.to_datetime(df_view["created_at"], errors="coerce")
    df_view["resolved_at_dt"] = pd.to_datetime(df_view.get("resolved_at", None), errors="coerce")
    resolved_only = df_view[df_view["resolved_at_dt"].notna() & df_view["created_at_dt"].notna()].copy()
    if not resolved_only.empty:
        resolved_only["resolution_hours"] = (resolved_only["resolved_at_dt"] - resolved_only["created_at_dt"]).dt.total_seconds() / 3600.0
        st.metric("Avg. resolution time (hours)", f"{float(resolved_only['resolution_hours'].mean()):.1f}")
    else:
        st.metric("Avg. resolution time (hours)", "n/a")

    display_df = build_display_table(df_view)
    st.subheader("List of Submitted Issues")
    st.dataframe(display_df, use_container_width=True, hide_index=True)

    csv_bytes = df_view.to_csv(index=False).encode("utf-8")
    st.download_button("Download CSV", data=csv_bytes, file_name="hsg_reporting_issues.csv", mime="text/csv")

    render_charts(df_view)

    with st.expander("Show status change audit log"):
        log_df = fetch_status_log(con)
        if log_df.empty:
            st.info("No status changes recorded yet.")
        else:
            st.dataframe(log_df, use_container_width=True, hide_index=True)


def page_booking(con: sqlite3.Connection) -> None:
    st.header("Booking")

    sync_asset_statuses_from_bookings(con)
    assets_df = fetch_assets(con)
    if assets_df.empty:
        st.warning("No assets available.")
        return

    def loc_label(loc_id: str) -> str:
        return LOCATIONS[loc_id]["label"] if loc_id in LOCATIONS else "Unknown location"

    asset_labels: dict[str, str] = {}
    for _, r in assets_df.iterrows():
        asset_labels[str(r["asset_id"])] = (
            f'{r["asset_name"]} ({r["asset_type"]}) — {loc_label(str(r["location_id"]))} [{r["status"]}]'
        )

    asset_id = st.selectbox("Select asset", options=list(asset_labels.keys()), format_func=lambda x: asset_labels[x])
    selected = assets_df[assets_df["asset_id"] == asset_id].iloc[0]

   if selected["status"] != "available":
       st.warning("This asset is currently not available for booking.")
       
    st.subheader("Upcoming bookings")
    future = fetch_future_bookings(con, asset_id)
    if future.empty:
        st.info("No upcoming bookings.")
    else:
        st.dataframe(future, hide_index=True, use_container_width=True)

    if selected["status"] != "available":
        return

    
    if selected["status"] != "available":
        st.warning("This asset is currently not available for booking.")
        return

    st.divider()
    st.subheader("Book this asset")

    with st.form("booking_form"):
        user_name = st.text_input("Your name")
        start_date = st.date_input("Start date")
        start_time = st.time_input("Start time")
        duration_hours = st.number_input("Duration (hours)", min_value=1, max_value=12, value=1, step=1)
        submit = st.form_submit_button("Confirm booking")

    if not submit:
        return

    if not user_name.strip():
        st.error("Name is required.")
        return

    # Build timezone-aware datetimes (Zurich)
    start_dt_naive = datetime.combine(start_date, start_time)
    start_dt = APP_TZ.localize(start_dt_naive)
    end_dt = start_dt + timedelta(hours=duration_hours)

    if start_dt < now_zurich():
        st.error("Start time cannot be in the past.")
        return

    if not is_asset_available(con, asset_id, start_dt, end_dt):
        st.error("This asset is already booked during the selected time.")
        return

    con.execute(
        """
        INSERT INTO bookings (asset_id, user_name, start_time, end_time, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            asset_id,
            user_name.strip(),
            start_dt.isoformat(timespec="seconds"),
            end_dt.isoformat(timespec="seconds"),
            now_zurich_str(),
        ),
    )
    con.commit()

    sync_asset_statuses_from_bookings(con)
    st.success("Booking confirmed.")


def page_assets(con: sqlite3.Connection) -> None:
    st.header("Asset Tracking")

    df = fetch_assets(con)
    if df.empty:
        st.info("No assets available.")
        return

    df = df.copy()
    df["location_label"] = df["location_id"].apply(
        lambda lid: LOCATIONS[lid]["label"] if lid in LOCATIONS else "Unknown location"
    )

    st.subheader("Filters")
    location_filter = st.multiselect(
        "Filter by location",
        options=sorted(df["location_label"].unique()),
        default=sorted(df["location_label"].unique()),
    )
    status_filter = st.multiselect(
        "Filter by status",
        options=sorted(df["status"].unique()),
        default=sorted(df["status"].unique()),
    )

    filtered_df = df[(df["location_label"].isin(location_filter)) & (df["status"].isin(status_filter))]

    st.subheader("Assets grouped by location")
    for location, group in filtered_df.groupby("location_label"):
        st.markdown(f"### {location}")
        st.dataframe(group[["asset_id", "asset_type", "status"]], hide_index=True, use_container_width=True)

    st.divider()

    # Select asset to view and move
    assets_df = fetch_assets(con)
    asset_labels = {
        row["asset_id"]: f'{row["asset_name"]} [{row["status"]}]'
        for _, row in assets_df.iterrows()
    }

    asset_id = st.selectbox("Select asset", options=list(asset_labels.keys()), format_func=lambda x: asset_labels[x])
    asset = df[df["asset_id"] == asset_id].iloc[0]

    st.write(
        {
            "Asset ID": asset["asset_id"],
            "Type": asset["asset_type"],
            "Current location": asset["location_label"],
            "Status": asset["status"],
        }
    )

    st.subheader("Move asset to another location")
    new_location_id = st.selectbox(
        "New location",
        options=list(LOCATIONS.keys()),
        format_func=lambda x: LOCATIONS[x]["label"],
    )

    if st.button("Update location"):
        con.execute("UPDATE assets SET location_id = ? WHERE asset_id = ?", (new_location_id, asset_id))
        con.commit()
        st.success("Asset location updated.")
        st.rerun()


def page_overwrite_status(con: sqlite3.Connection) -> None:
    st.header("Overwrite Status")

    entered_password = st.sidebar.text_input("Enter Password", "", type="password")
    if entered_password != ADMIN_PASSWORD:
        st.warning("Enter the correct password to access this page.")
        return

    if st.sidebar.button("Send weekly report now"):
        df_all = fetch_submissions(con)
        subject, body = build_weekly_report(df_all)
        ok, msg = send_admin_report_email(subject, body)
        if ok:
            mark_report_sent(con, "weekly_manual")
            st.sidebar.success("Weekly report sent.")
        else:
            st.sidebar.warning(msg)

    df = fetch_submissions(con)
    if df.empty:
        st.info("No submitted issues yet.")
        return

    admin_status_filter = st.multiselect(
        "Show issues with status",
        options=STATUS_LEVELS,
        default=["Pending", "In Progress"],
    )
    df = df[df["status"].isin(admin_status_filter)]
    if df.empty:
        st.info("No issues match the selected admin status filter.")
        return

    selected_id = st.selectbox("Select Issue ID to update:", df["id"].tolist())
    row = df[df["id"] == selected_id].iloc[0]

    sla_target = expected_resolution_dt(str(row["created_at"]), str(row["importance"]))
    sla_text = sla_target.isoformat(timespec="seconds") if sla_target is not None else "n/a"

    st.subheader("Selected Issue Details")
    st.write(
        {
            "ID": int(row["id"]),
            "Name": row["name"],
            "Email": row["hsg_email"],
            "Issue Type": row["issue_type"],
            "Room": row["room_number"],
            "Importance": row["importance"],
            "Status": row["status"],
            "Assigned To": row.get("assigned_to", None),
            "Submitted At": row["created_at"],
            "SLA Target": sla_text,
            "Resolved At": row.get("resolved_at", None),
            "Last Updated": row["updated_at"],
            "Problem Description": row["user_comment"],
        }
    )

    st.divider()

    current_assignee = str(row.get("assigned_to", "") or "")
    assigned_to = st.selectbox(
        "Assigned to",
        options=["(unassigned)"] + ASSIGNEES,
        index=(["(unassigned)"] + ASSIGNEES).index(current_assignee) if current_assignee in (["(unassigned)"] + ASSIGNEES) else 0,
    )
    assigned_to_value = None if assigned_to == "(unassigned)" else assigned_to

    new_status = st.selectbox(
        "New Status",
        STATUS_LEVELS,
        index=STATUS_LEVELS.index(row["status"]) if row["status"] in STATUS_LEVELS else 0,
    )

    confirm_resolve = True
    if new_status == "Resolved":
        confirm_resolve = st.checkbox("I confirm the issue is resolved (and an email will be sent).", value=False)

    if st.button("Update"):
        if new_status == "Resolved" and not confirm_resolve:
            st.error("Please confirm resolution before setting status to Resolved.")
            return

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
                ok, msg = send_email(str(row["hsg_email"]).strip(), subject, body)
                (st.success(msg) if ok else st.warning(msg))

        st.success("Update successful.")
        st.rerun()


# ----------------------------
# Main
# ----------------------------
def main() -> None:
    st.set_page_config(page_title="Reporting Tool at HSG", layout="centered")

    show_logo()

    try:
        st.image(
            "campus_header.jpeg",
            caption="University of St. Gallen – Campus",
            use_container_width=True,
        )
    except Exception:
        st.info("Header image not found. Add 'campus_header.jpeg' to the repository root.")

    con = get_connection()
    init_db(con)
    migrate_db(con)

    init_booking_table(con)
    init_assets_table(con)
    seed_assets(con)

    sync_asset_statuses_from_bookings(con)
    send_weekly_report_if_due(con)

    st.title("Reporting Tool at HSG")

    # Sidebar navigation with 3 sections (categories)
    st.sidebar.markdown("### Navigation")

    section = st.sidebar.radio(
        "Select section:",
        ["Reporting Tool", "Booking / Tracking", "Overview"],
    )

    if section == "Reporting Tool":
        page = st.sidebar.radio(
            "Select page:",
            ["Submission Form", "Submitted Issues", "Overwrite Status"],
        )
    elif section == "Booking / Tracking":
        page = st.sidebar.radio(
            "Select page:",
            ["Booking", "Asset Tracking"],
        )
    else:  # Overview
        page = st.sidebar.radio(
            "Select page:",
            ["Overview Dashboard"],
        )

    if page == "Submission Form":
        page_submission_form(con)
    elif page == "Submitted Issues":
        page_submitted_issues(con)
    elif page == "Booking":
        page_booking(con)
    elif page == "Asset Tracking":
        page_assets(con)
    else:
        page_overwrite_status(con)


if __name__ == "__main__":
    main()
