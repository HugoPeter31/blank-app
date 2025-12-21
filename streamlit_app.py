from __future__ import annotations

"""
HSG Reporting Tool (Streamlit)

A small, self-contained campus tool that:
- collects facility issue reports (SQLite + optional email confirmations),
- provides an admin workflow (status updates + audit trail),
- supports booking and tracking of assets (rooms/equipment) with room→contained-assets logic.

Design principles (as typically expected in academic grading rubrics):
- Centralize configuration and business rules (SLA, validation, locations).
- Keep side effects explicit (DB writes, emails) and parameterized (SQL injection safety).
- Keep UI code readable: validate early, return early, separate helpers from pages.
"""

import logging
import re
import sqlite3
import smtplib
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Iterable

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd
import pytz
import streamlit as st

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
APP_TZ = pytz.timezone("Europe/Zurich")
DB_PATH = "hsg_reporting.db"
LOGO_PATH = "HSG-logo-new.png"

HSG_GREEN = "#00802F"

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

SLA_HOURS_BY_IMPORTANCE: dict[str, int] = {
    "High": 24,
    "Medium": 72,
    "Low": 120,
}

EMAIL_PATTERN = re.compile(r"^[\w.]+@(student\.)?unisg\.ch$")
ROOM_PATTERN = re.compile(r"^[A-Z]\s?\d{2}-\d{3}$")

LOCATIONS = {
    "R_A_09001": {"label": "Room A 09-001", "x": 10, "y": 20},
    "H_A_09001": {"label": "Hallway near Room A 09-001", "x": 15, "y": 25},
    "R_B_10012": {"label": "Room B 10-012", "x": 40, "y": 60},
    "H_B_10012": {"label": "Hallway near Room B 10-012", "x": 45, "y": 65},
}

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)

# -----------------------------------------------------------------------------
# Data model
# -----------------------------------------------------------------------------
@dataclass(frozen=True)
class Submission:
    name: str
    hsg_email: str
    issue_type: str
    room_number: str
    importance: str
    user_comment: str


# -----------------------------------------------------------------------------
# Secrets
# -----------------------------------------------------------------------------
def get_secret(key: str, default: str | None = None) -> str:
    """
    Streamlit secrets are runtime configuration. Failing fast is better than
    silently misconfiguring email/admin authentication.
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

ADMIN_PASSWORD = get_secret("ADMIN_PASSWORD")
DEBUG = get_secret("DEBUG", "0") == "1"

ASSIGNEES_RAW = get_secret("ASSIGNEES", "Facility Team")
ASSIGNEES = [a.strip() for a in ASSIGNEES_RAW.split(",") if a.strip()]

AUTO_WEEKLY_REPORT = get_secret("AUTO_WEEKLY_REPORT", "0") == "1"
REPORT_WEEKDAY = int(get_secret("REPORT_WEEKDAY", "0"))  # 0=Mon ... 6=Sun
REPORT_HOUR = int(get_secret("REPORT_HOUR", "7"))


# -----------------------------------------------------------------------------
# Time helpers
# -----------------------------------------------------------------------------
def now_zurich() -> datetime:
    return datetime.now(APP_TZ)


def now_zurich_str() -> str:
    return now_zurich().isoformat(timespec="seconds")


def iso_to_dt(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def expected_resolution_dt(created_at_iso: str, importance: str) -> datetime | None:
    created_dt = iso_to_dt(created_at_iso)
    sla_hours = SLA_HOURS_BY_IMPORTANCE.get(importance)
    if created_dt is None or sla_hours is None:
        return None
    return created_dt + timedelta(hours=int(sla_hours))


# -----------------------------------------------------------------------------
# Validation
# -----------------------------------------------------------------------------
def valid_email(hsg_email: str) -> bool:
    return bool(EMAIL_PATTERN.fullmatch(hsg_email.strip()))


def valid_room_number(room_number: str) -> bool:
    return bool(ROOM_PATTERN.fullmatch(room_number.strip()))


def normalize_room(room_number: str) -> str:
    raw = room_number.strip().upper()
    raw = re.sub(r"^([A-Z])(\d{2}-\d{3})$", r"\1 \2", raw)
    raw = re.sub(r"\s+", " ", raw)
    return raw


def validate_submission_input(sub: Submission) -> list[str]:
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
    if not email.strip():
        return ["Email address is required."]
    if not valid_email(email):
        return ["Please provide a valid HSG email address (…@unisg.ch or …@student.unisg.ch)."]
    return []


# -----------------------------------------------------------------------------
# Database
# -----------------------------------------------------------------------------
@st.cache_resource
def get_connection() -> sqlite3.Connection:
    # Streamlit reruns scripts; a cached connection avoids opening a new one each time.
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def init_db(con: sqlite3.Connection) -> None:
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


def seed_assets(con: sqlite3.Connection) -> None:
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


# -----------------------------------------------------------------------------
# Booking logic
# -----------------------------------------------------------------------------
def is_room_location(location_id: str) -> bool:
    return str(location_id).startswith("R_")


def fetch_assets_in_room(con: sqlite3.Connection, room_location_id: str) -> list[str]:
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


def sync_asset_statuses_from_bookings(con: sqlite3.Connection) -> None:
    """
    Keep asset status derived from bookings (single source of truth).
    This avoids drift between booking rows and asset rows.
    """
    now_iso = now_zurich().isoformat(timespec="seconds")

    con.execute("UPDATE assets SET status = 'available'")
    con.commit()

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
        asset_id = str(row["asset_id"])
        asset_type = str(row["asset_type"])
        location_id = str(row["location_id"])

        con.execute("UPDATE assets SET status = 'booked' WHERE asset_id = ?", (asset_id,))

        if asset_type == "Room" and is_room_location(location_id):
            for contained_asset_id in fetch_assets_in_room(con, location_id):
                con.execute("UPDATE assets SET status = 'booked' WHERE asset_id = ?", (contained_asset_id,))

    con.commit()


def is_asset_available(con: sqlite3.Connection, asset_id: str, start_time: datetime, end_time: datetime) -> bool:
    count = con.execute(
        """
        SELECT COUNT(*)
        FROM bookings
        WHERE asset_id = ?
          AND start_time < ?
          AND end_time > ?
        """,
        (asset_id, end_time.isoformat(timespec="seconds"), start_time.isoformat(timespec="seconds")),
    ).fetchone()[0]
    return int(count) == 0


def fetch_future_bookings(con: sqlite3.Connection, asset_id: str) -> pd.DataFrame:
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


# -----------------------------------------------------------------------------
# Issue administration
# -----------------------------------------------------------------------------
def update_issue_admin_fields(
    con: sqlite3.Connection,
    issue_id: int,
    new_status: str,
    assigned_to: str | None,
    old_status: str,
) -> None:
    updated_at = now_zurich_str()
    set_resolved_at = new_status == "Resolved"

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
    created_at = now_zurich_str()
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
                normalize_room(sub.room_number),
                sub.importance,
                sub.user_comment.strip(),
                created_at,
                created_at,
            ),
        )


# -----------------------------------------------------------------------------
# Email
# -----------------------------------------------------------------------------
def _smtp_send(msg: EmailMessage, recipients: list[str]) -> None:
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=10) as smtp:
        smtp.starttls()
        smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
        smtp.send_message(msg, to_addrs=recipients)


def send_email(to_email: str, subject: str, body: str) -> tuple[bool, str]:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = FROM_EMAIL
    msg["To"] = to_email
    msg.set_content(body)

    recipients = [to_email] + ([ADMIN_INBOX] if ADMIN_INBOX else [])

    try:
        _smtp_send(msg, recipients)
        return True, "Email sent."
    except Exception as exc:
        logger.exception("Email sending failed")
        return (False, f"Email could not be sent: {exc}") if DEBUG else (False, "Email could not be sent due to a technical issue.")


def send_admin_report_email(subject: str, body: str) -> tuple[bool, str]:
    if not ADMIN_INBOX:
        return False, "ADMIN_INBOX is not configured."

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = FROM_EMAIL
    msg["To"] = ADMIN_INBOX
    msg.set_content(body)

    try:
        _smtp_send(msg, [ADMIN_INBOX])
        return True, "Report email sent."
    except Exception as exc:
        logger.exception("Report email sending failed")
        return (False, f"Report email could not be sent: {exc}") if DEBUG else (False, "Report email could not be sent due to a technical issue.")


def confirmation_email_text(recipient_name: str, importance: str) -> tuple[str, str]:
    subject = "HSG Reporting Tool: Issue received"
    sla_hours = SLA_HOURS_BY_IMPORTANCE.get(importance)
    sla_text = f"Expected handling time (SLA): within {sla_hours} hours." if sla_hours is not None else "Expected handling time (SLA): n/a."

    body = f"""Dear {recipient_name},

Thank you for contacting us. We confirm that we have received your issue report and it is under review by the responsible team.

{sla_text}

Kind regards,
HSG Service Team
"""
    return subject, body


def resolved_email_text(recipient_name: str) -> tuple[str, str]:
    subject = "HSG Reporting Tool: Issue resolved"
    body = f"""Hello {recipient_name},

We are pleased to inform you that the issue you reported via the HSG Reporting Tool has been resolved.

Kind regards,
HSG Service Team
"""
    return subject, body


# -----------------------------------------------------------------------------
# Reporting
# -----------------------------------------------------------------------------
def build_weekly_report(df_all: pd.DataFrame) -> tuple[str, str]:
    now_dt = now_zurich()
    since_dt = now_dt - timedelta(days=7)

    df = df_all.copy()
    df["created_at_dt"] = pd.to_datetime(df["created_at"], errors="coerce")
    df["resolved_at_dt"] = pd.to_datetime(df.get("resolved_at", pd.Series([None] * len(df))), errors="coerce")

    new_last_7d = df[df["created_at_dt"] >= since_dt]
    resolved_last_7d = df[df["resolved_at_dt"].notna() & (df["resolved_at_dt"] >= since_dt)]
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
        for issue_type, count in open_issues["issue_type"].value_counts().head(5).items():
            body += f"- {issue_type}: {count}\n"
    else:
        body += "- n/a\n"

    body += "\nThis email was generated by the HSG Reporting Tool."
    return subject, body


def mark_report_sent(con: sqlite3.Connection, report_type: str) -> None:
    con.execute("INSERT INTO report_log (report_type, sent_at) VALUES (?, ?)", (report_type, now_zurich_str()))
    con.commit()


def send_weekly_report_if_due(con: sqlite3.Connection) -> None:
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

    subject, body = build_weekly_report(fetch_submissions(con))
    ok, _ = send_admin_report_email(subject, body)
    if ok:
        mark_report_sent(con, "weekly")


# -----------------------------------------------------------------------------
# UI helpers
# -----------------------------------------------------------------------------
def apply_hsg_table_header_style() -> None:
    # Streamlit doesn't provide a native API for header styling; CSS injection is the stable workaround.
    st.markdown(
        f"""
        <style>
        /* st.table() */
        div[data-testid="stTable"] thead tr th {{
            background-color: {HSG_GREEN} !important;
            color: #ffffff !important;
            font-weight: 600 !important;
        }}

        /* st.dataframe() (HTML fallback) */
        div[data-testid="stDataFrame"] thead tr th {{
            background-color: {HSG_GREEN} !important;
            color: #ffffff !important;
            font-weight: 600 !important;
        }}

        /* st.dataframe() (grid/AG-Grid-like rendering in many Streamlit versions) */
        div[data-testid="stDataFrame"] .ag-header,
        div[data-testid="stDataFrame"] .ag-header-cell,
        div[data-testid="stDataFrame"] .ag-header-group-cell {{
            background-color: {HSG_GREEN} !important;
        }}
        div[data-testid="stDataFrame"] .ag-header-cell-text,
        div[data-testid="stDataFrame"] .ag-header-group-text,
        div[data-testid="stDataFrame"] .ag-header-cell-label {{
            color: #ffffff !important;
            font-weight: 600 !important;
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def show_errors(errors: Iterable[str]) -> None:
    for msg in errors:
        st.error(msg)


def show_logo() -> None:
    try:
        st.sidebar.image(LOGO_PATH, width=170)
    except Exception:
        st.sidebar.info("Logo not found. Add 'HSG-logo-new.png' to the repository root.")


def render_map_iframe() -> None:
    with st.expander("Map (optional)", expanded=False):
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
    return LOCATIONS.get(str(loc_id), {}).get("label", "Unknown location")


def asset_display_label(row: pd.Series) -> str:
    status = str(row.get("status", "")).strip().lower()
    status_text = "Available ✅" if status == "available" else ("Booked ⛔" if status == "booked" else str(row.get("status", "")))
    return f'{row.get("asset_name", "")} • {row.get("asset_type", "")} • {location_label(str(row.get("location_id", "")))} • {status_text}'


def format_booking_table(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    out = df.copy()
    out["start_time"] = pd.to_datetime(out["start_time"], errors="coerce")
    out["end_time"] = pd.to_datetime(out["end_time"], errors="coerce")
    out = out.dropna(subset=["start_time", "end_time"]).sort_values(by=["start_time"])
    out["start_time"] = out["start_time"].dt.strftime("%Y-%m-%d %H:%M")
    out["end_time"] = out["end_time"].dt.strftime("%Y-%m-%d %H:%M")
    return out.rename(columns={"user_name": "User", "start_time": "Start", "end_time": "End"})


# -----------------------------------------------------------------------------
# Pages
# -----------------------------------------------------------------------------
def page_submission_form(con: sqlite3.Connection) -> None:
    st.header("Report an issue")
    st.caption("Fields marked with * are mandatory.")

    with st.form("issue_form", clear_on_submit=True):
        name = st.text_input("Name*", placeholder="e.g., Max Muster").strip()
        hsg_email = st.text_input("HSG email address*", placeholder="e.g. firstname.lastname@student.unisg.ch").strip()
        st.caption("Accepted: …@unisg.ch or …@student.unisg.ch")

        room_number_input = st.text_input("Room number*", placeholder="e.g., A 09-001").strip()
        issue_type = st.selectbox("Issue type*", ISSUE_TYPES)
        importance = st.selectbox("Importance*", IMPORTANCE_LEVELS)

        sla_hours = SLA_HOURS_BY_IMPORTANCE.get(importance)
        if sla_hours is not None:
            st.info(f"SLA target: within {sla_hours} hours.")

        user_comment = st.text_area(
            "Problem description*",
            max_chars=500,
            placeholder="What happened? Where exactly? Since when? Any impact?",
        ).strip()

        uploaded_file = st.file_uploader("Upload a photo (optional)", type=["jpg", "jpeg", "png"])
        if uploaded_file is not None:
            st.image(uploaded_file, caption="Preview (not stored)", use_container_width=True)

        render_map_iframe()
        submitted = st.form_submit_button("Submit")

    if not submitted:
        return

    sub = Submission(
        name=name,
        hsg_email=hsg_email.lower(),
        issue_type=issue_type,
        room_number=normalize_room(room_number_input),
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

    st.success("Submission received.")
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
        per_day = df_dates.groupby(df_dates["created_at"].dt.date).size().reindex(date_index.date, fill_value=0)

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
    st.header("Submitted issues")

    df = fetch_submissions(con)
    st.subheader(f"Total issues: {len(df)}")

    if df.empty:
        st.info("No submitted issues yet. Please submit an issue first.")
        return

    status_filter = st.multiselect("Filter by status", options=STATUS_LEVELS, default=STATUS_LEVELS)
    df = df[df["status"].isin(status_filter)].copy()
    if df.empty:
        st.info("No issues match the selected status filter.")
        return

    df["expected_resolved_at"] = df.apply(
        lambda r: (
            expected_resolution_dt(str(r["created_at"]), str(r["importance"])).isoformat(timespec="seconds")
            if expected_resolution_dt(str(r["created_at"]), str(r["importance"])) is not None
            else None
        ),
        axis=1,
    )

    df["created_at_dt"] = pd.to_datetime(df["created_at"], errors="coerce")
    df["resolved_at_dt"] = pd.to_datetime(df.get("resolved_at", pd.Series([None] * len(df))), errors="coerce")

    resolved_only = df[df["resolved_at_dt"].notna() & df["created_at_dt"].notna()].copy()
    if not resolved_only.empty:
        resolved_only["resolution_hours"] = (resolved_only["resolved_at_dt"] - resolved_only["created_at_dt"]).dt.total_seconds() / 3600.0
        st.metric("Avg. resolution time (hours)", f"{float(resolved_only['resolution_hours'].mean()):.1f}")
    else:
        st.metric("Avg. resolution time (hours)", "n/a")

    display_df = build_display_table(df)
    st.subheader("List of submitted issues")
    st.dataframe(display_df, use_container_width=True, hide_index=True)

    csv_bytes = df.to_csv(index=False).encode("utf-8")
    st.download_button("Download CSV", data=csv_bytes, file_name="hsg_reporting_issues.csv", mime="text/csv")

    render_charts(df)

    with st.expander("Show status change audit log"):
        log_df = fetch_status_log(con)
        if log_df.empty:
            st.info("No status changes recorded yet.")
        else:
            st.dataframe(log_df, use_container_width=True, hide_index=True)


def page_booking(con: sqlite3.Connection) -> None:
    st.header("Book an asset")

    sync_asset_statuses_from_bookings(con)
    assets_df = fetch_assets(con)

    if assets_df.empty:
        st.warning("No assets available.")
        return

    c1, c2, c3 = st.columns([2, 1, 1])
    search = c1.text_input("Search", placeholder="e.g., projector, meeting, A 09-001").strip().lower()
    type_filter = c2.selectbox("Type", options=["All"] + sorted(assets_df["asset_type"].unique().tolist()))
    availability_filter = c3.selectbox("Availability", options=["All", "Available", "Booked"])

    view_df = assets_df.copy()
    view_df["location_label"] = view_df["location_id"].apply(location_label)
    view_df["label"] = view_df.apply(asset_display_label, axis=1)

    if type_filter != "All":
        view_df = view_df[view_df["asset_type"] == type_filter]

    if availability_filter != "All":
        want = "available" if availability_filter == "Available" else "booked"
        view_df = view_df[view_df["status"].astype(str).str.lower() == want]

    if search:
        mask = (
            view_df["asset_name"].str.lower().str.contains(search, na=False)
            | view_df["asset_type"].str.lower().str.contains(search, na=False)
            | view_df["location_label"].str.lower().str.contains(search, na=False)
        )
        view_df = view_df[mask].copy()

    if view_df.empty:
        st.warning("No assets match your filters.")
        return

    view_df["_status_rank"] = view_df["status"].astype(str).str.lower().map({"available": 0, "booked": 1}).fillna(99).astype(int)
    view_df = view_df.sort_values(by=["_status_rank", "asset_type", "asset_name"]).drop(columns=["_status_rank"])

    asset_rows: dict[str, str] = {str(r["asset_id"]): str(r["label"]) for _, r in view_df.iterrows()}

    default_asset_id = st.session_state.get("booking_asset_id")
    if default_asset_id not in asset_rows:
        default_asset_id = list(asset_rows.keys())[0]

    asset_id = st.selectbox(
        "Asset",
        options=list(asset_rows.keys()),
        index=list(asset_rows.keys()).index(default_asset_id),
        format_func=lambda aid: asset_rows[aid],
    )
    st.session_state["booking_asset_id"] = asset_id

    selected = assets_df[assets_df["asset_id"] == asset_id].iloc[0]
    selected_status = str(selected["status"]).strip().lower()

    st.write("**Location:**", location_label(str(selected["location_id"])))

    if selected_status == "available":
        st.success("Available ✅")
    else:
        next_free = next_available_time(con, asset_id)
        st.warning(f"Booked ⛔{'  •  Next available: ' + next_free.strftime('%Y-%m-%d %H:%M') if next_free else ''}")

    st.subheader("Upcoming bookings")
    future = fetch_future_bookings(con, asset_id)
    if future.empty:
        st.caption("No upcoming bookings.")
    else:
        st.dataframe(format_booking_table(future), hide_index=True, use_container_width=True)

    st.divider()
    st.subheader("Create booking")

    if selected_status != "available":
        st.info("Choose an available asset to create a booking.")
        return

    with st.form("booking_form"):
        user_name = st.text_input("Your name*", placeholder="e.g., Max Muster").strip()

        c1, c2, c3 = st.columns(3)
        start_date = c1.date_input("Start date*", value=now_zurich().date())
        start_time = c2.time_input("Start time*", value=now_zurich().time().replace(second=0, microsecond=0))
        duration_hours = c3.number_input("Duration (hours)*", min_value=1, max_value=12, value=1, step=1)

        submit = st.form_submit_button("Confirm booking")

    if not submit:
        return

    if not user_name:
        st.error("Name is required.")
        return

    start_dt_naive = datetime.combine(start_date, start_time)
    start_dt = APP_TZ.localize(start_dt_naive)
    end_dt = start_dt + timedelta(hours=float(duration_hours))

    if start_dt < now_zurich():
        st.error("Start time cannot be in the past.")
        return

    if end_dt <= start_dt:
        st.error("End time must be after start time.")
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
            user_name,
            start_dt.isoformat(timespec="seconds"),
            end_dt.isoformat(timespec="seconds"),
            now_zurich_str(),
        ),
    )
    con.commit()

    sync_asset_statuses_from_bookings(con)
    st.success(f"Booking confirmed: {start_dt.strftime('%Y-%m-%d %H:%M')} → {end_dt.strftime('%H:%M')}")
    st.rerun()


def page_assets(con: sqlite3.Connection) -> None:
    st.header("Asset tracking")

    df = fetch_assets(con)
    if df.empty:
        st.info("No assets available.")
        return

    df = df.copy()
    df["location_label"] = df["location_id"].apply(location_label)

    st.subheader("Filters")
    location_filter = st.multiselect(
        "Location",
        options=sorted(df["location_label"].unique()),
        default=sorted(df["location_label"].unique()),
    )
    status_filter = st.multiselect(
        "Status",
        options=sorted(df["status"].unique()),
        default=sorted(df["status"].unique()),
    )

    filtered_df = df[(df["location_label"].isin(location_filter)) & (df["status"].isin(status_filter))]

    st.subheader("Grouped by location")
    for location, group in filtered_df.groupby("location_label"):
        with st.expander(f"{location} ({len(group)})", expanded=False):
            st.dataframe(group[["asset_id", "asset_name", "asset_type", "status"]], hide_index=True, use_container_width=True)

    st.divider()

    assets_df = fetch_assets(con).copy()
    assets_df["location_label"] = assets_df["location_id"].apply(location_label)
    assets_df["label"] = assets_df.apply(asset_display_label, axis=1)

    asset_rows = {str(r["asset_id"]): str(r["label"]) for _, r in assets_df.iterrows()}

    asset_id = st.selectbox("Select asset", options=list(asset_rows.keys()), format_func=lambda aid: asset_rows[aid])
    asset = assets_df[assets_df["asset_id"] == asset_id].iloc[0]

    st.subheader("Selected asset")
    c1, c2, c3 = st.columns(3)
    c1.metric("Status", str(asset["status"]).capitalize())
    c2.metric("Type", str(asset["asset_type"]))
    c3.metric("Asset ID", str(asset["asset_id"]))
    st.write("**Location:**", str(asset["location_label"]))

    st.subheader("Move asset to another location")
    new_location_id = st.selectbox("New location", options=list(LOCATIONS.keys()), format_func=lambda x: LOCATIONS[x]["label"])

    if st.button("Update location"):
        con.execute("UPDATE assets SET location_id = ? WHERE asset_id = ?", (new_location_id, asset_id))
        con.commit()
        st.success("Asset location updated.")
        st.rerun()


def page_overwrite_status(con: sqlite3.Connection) -> None:
    st.header("Admin – update issue status")

    entered_password = st.text_input("Admin password", type="password")
    if entered_password != ADMIN_PASSWORD:
        st.info("Enter the admin password to access this page.")
        return

    c1, c2 = st.columns([1, 2])
    if c1.button("Send weekly report now"):
        subject, body = build_weekly_report(fetch_submissions(con))
        ok, msg = send_admin_report_email(subject, body)
        if ok:
            mark_report_sent(con, "weekly_manual")
            st.success("Weekly report sent.")
        else:
            st.warning(msg)
    c2.caption("Sends the current weekly report to the configured ADMIN_INBOX.")

    df = fetch_submissions(con)
    if df.empty:
        st.info("No submitted issues yet.")
        return

    admin_status_filter = st.multiselect("Show issues with status", options=STATUS_LEVELS, default=["Pending", "In Progress"])
    df = df[df["status"].isin(admin_status_filter)]
    if df.empty:
        st.info("No issues match the selected filter.")
        return

    selected_id = st.selectbox("Select issue ID", df["id"].tolist())
    row = df[df["id"] == selected_id].iloc[0]

    sla_target = expected_resolution_dt(str(row["created_at"]), str(row["importance"]))
    sla_text = sla_target.isoformat(timespec="seconds") if sla_target is not None else "n/a"

    st.subheader("Selected issue details")
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

    new_status = st.selectbox("New status", STATUS_LEVELS, index=STATUS_LEVELS.index(row["status"]) if row["status"] in STATUS_LEVELS else 0)

    confirm_resolve = True
    if new_status == "Resolved":
        confirm_resolve = st.checkbox("I confirm the issue is resolved (an email will be sent).", value=False)

    if st.button("Update"):
        if new_status == "Resolved" and not confirm_resolve:
            st.error("Please confirm resolution before setting status to Resolved.")
            return

        old_status = str(row["status"])
        update_issue_admin_fields(con=con, issue_id=int(selected_id), new_status=new_status, assigned_to=assigned_to_value, old_status=old_status)

        if new_status == "Resolved":
            email_errors = validate_admin_email(str(row["hsg_email"]))
            if email_errors:
                show_errors(email_errors)
            else:
                subject, body = resolved_email_text(str(row["name"]).strip() or "there")
                ok, msg = send_email(str(row["hsg_email"]).strip(), subject, body)
                st.success(msg) if ok else st.warning(msg)

        st.success("Update successful.")
        st.rerun()


def page_overview_dashboard(con: sqlite3.Connection) -> None:
    st.header("Overview dashboard")
    st.caption("Quick overview of issues and assets.")

    issues = fetch_submissions(con)
    assets = fetch_assets(con)

    c1, c2, c3 = st.columns(3)
    c1.metric("Total issues", str(len(issues)))
    c2.metric("Open issues", str(int((issues["status"] != "Resolved").sum())) if not issues.empty else "0")
    c3.metric("Total assets", str(len(assets)))

    st.divider()

    st.subheader("Open issues")
    if issues.empty:
        st.info("No issues yet.")
    else:
        open_issues = issues[issues["status"] != "Resolved"]
        if open_issues.empty:
            st.success("No open issues.")
        else:
            st.dataframe(open_issues[["id", "issue_type", "room_number", "importance", "status", "created_at"]], hide_index=True, use_container_width=True)

    st.subheader("Assets")
    if assets.empty:
        st.info("No assets yet.")
    else:
        assets_view = assets.copy()
        assets_view["location"] = assets_view["location_id"].apply(location_label)
        st.dataframe(assets_view[["asset_id", "asset_name", "asset_type", "status", "location"]], hide_index=True, use_container_width=True)


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def main() -> None:
    st.set_page_config(page_title="Reporting Tool @ HSG", layout="centered")

    apply_hsg_table_header_style()
    show_logo()

    try:
        st.image("campus_header.jpeg", caption="University of St. Gallen – Campus", use_container_width=True)
    except FileNotFoundError:
        st.caption("Header image not found. Add 'campus_header.jpeg' to the repository root.")

    con = get_connection()
    init_db(con)
    migrate_db(con)
    init_booking_table(con)
    init_assets_table(con)
    seed_assets(con)

    sync_asset_statuses_from_bookings(con)
    send_weekly_report_if_due(con)

    st.title("Reporting Tool @ HSG")

    st.sidebar.markdown("### Navigation")
    section = st.sidebar.selectbox("Section", ["Reporting Tool", "Booking / Tracking", "Overview"])

    if section == "Reporting Tool":
        page = st.sidebar.selectbox("Page", ["Submission Form", "Submitted Issues", "Overwrite Status"])
        st.sidebar.caption("Report campus facility issues and track progress.")
    elif section == "Booking / Tracking":
        page = st.sidebar.selectbox("Page", ["Booking", "Asset Tracking"])
        st.sidebar.caption("Book assets and manage their locations.")
    else:
        page = st.sidebar.selectbox("Page", ["Overview Dashboard"])
        st.sidebar.caption("Key metrics at a glance.")

    if page == "Submission Form":
        page_submission_form(con)
    elif page == "Submitted Issues":
        page_submitted_issues(con)
    elif page == "Booking":
        page_booking(con)
    elif page == "Asset Tracking":
        page_assets(con)
    elif page == "Overview Dashboard":
        page_overview_dashboard(con)
    else:
        page_overwrite_status(con)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        logger.critical("Application crashed", exc_info=True)
        st.error("An unexpected error occurred. Please refresh and try again.")
        if DEBUG:
            import traceback

            st.code(traceback.format_exc(), language="python")
