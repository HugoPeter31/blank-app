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
# Utility / helpers
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
    except (TypeError, ValueError):
        return None


def expected_resolution_dt(created_at_iso: str, importance: str) -> datetime | None:
    """created_at + SLA(importance)."""
    created_dt = iso_to_dt(created_at_iso)
    sla_hours = SLA_HOURS_BY_IMPORTANCE.get(importance)
    if created_dt is None or sla_hours is None:
        return None
    return created_dt + timedelta(hours=int(sla_hours))


# STEP 1 — helper to identify rooms
def is_room_location(location_id: str) -> bool:
    """Return True if the location represents a room."""
    return str(location_id).startswith("R_")


# ----------------------------
# Validation
# ----------------------------
def valid_email(hsg_email: str) -> bool:
    return bool(EMAIL_PATTERN.fullmatch(hsg_email.strip()))


def valid_room_number(room_number: str) -> bool:
    return bool(ROOM_PATTERN.fullmatch(room_number.strip()))


def normalize_room(room_number: str) -> str:
    """
    Normalize room numbers to the canonical format: 'A 09-001'
    Accepts user inputs like 'A09-001' or 'A 09-001'.
    """
    raw = room_number.strip().upper()
    raw = re.sub(r"^([A-Z])(\d{2}-\d{3})$", r"\1 \2", raw)  # A09-001 -> A 09-001
    raw = re.sub(r"\s+", " ", raw)
    return raw


def validate_submission_input(sub: Submission) -> list[str]:
    """Validate inputs for issue submission form."""
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


# ----------------------------
# Database: seed + fetch helpers
# ----------------------------
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


# STEP 2 — DB helper: assets inside a room
def fetch_assets_in_room(con: sqlite3.Connection, room_location_id: str) -> list[str]:
    """
    Return asset_ids of all non-room assets located inside a given room.
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
    con.execute(
        "INSERT INTO report_log (report_type, sent_at) VALUES (?, ?)",
        (report_type, now_zurich_str()),
    )
    con.commit()


# ----------------------------
# Booking helpers
# ----------------------------
# STEP 3 — Replace status sync logic (CORE CHANGE)
def sync_asset_statuses_from_bookings(con: sqlite3.Connection) -> None:
    """
    Update asset statuses based on active bookings.
    If a room is booked, all assets inside that room are booked as well.
    """
    now_iso = now_zurich().isoformat(timespec="seconds")

    # Reset all assets
    con.execute("UPDATE assets SET status = 'available'")
    con.commit()

    # Active bookings
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

        # Always mark the booked asset
        con.execute(
            "UPDATE assets SET status = 'booked' WHERE asset_id = ?",
            (asset_id,),
        )

        # Auto-book assets inside a room
        if asset_type == "Room" and is_room_location(location_id):
            inside_assets = fetch_assets_in_room(con, location_id)
            for aid in inside_assets:
                con.execute(
                    "UPDATE assets SET status = 'booked' WHERE asset_id = ?",
                    (aid,),
                )

    con.commit()


def is_asset_available(con: sqlite3.Connection, asset_id: str, start_time: datetime, end_time: datetime) -> bool:
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
    """If currently booked, return the next end_time; otherwise None."""
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
    loc = location_label(str(row.get("location_id", "")))
    return f'{row.get("asset_name", "")} • {row.get("asset_type", "")} • {loc} • {status_text}'


def format_booking_table(df: pd.DataFrame) -> pd.DataFrame:
    """Make upcoming bookings easier to read (local time + sorted)."""
    if df.empty:
        return df

    out = df.copy()
    out["start_time"] = pd.to_datetime(out["start_time"], errors="coerce")
    out["end_time"] = pd.to_datetime(out["end_time"], errors="coerce")

    out = out.dropna(subset=["start_time", "end_time"]).sort_values(by=["start_time"])
    out["start_time"] = out["start_time"].dt.strftime("%Y-%m-%d %H:%M")
    out["end_time"] = out["end_time"].dt.strftime("%Y-%m-%d %H:%M")

    return out.rename(columns={"user_name": "User", "start_time": "Start", "end_time": "End"})


# ----------------------------
# Pages (UI tweaks only where needed)
# ----------------------------
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

    insert_submission(con, sub)

    subject, body = confirmation_email_text(sub.name.strip(), sub.importance)
    ok, msg = send_email(sub.hsg_email, subject, body)

    st.success("Submission received.")
    if not ok:
        st.warning(msg)


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
    st.dataframe(format_booking_table(future), hide_index=True, use_container_width=True) if not future.empty else st.caption("No upcoming bookings.")

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
            st.dataframe(
                group[["asset_id", "asset_name", "asset_type", "status"]],
                hide_index=True,
                use_container_width=True,
            )


def page_overwrite_status(con: sqlite3.Connection) -> None:
    st.header("Admin – update issue status")

    entered_password = st.text_input("Admin password", type="password")
    if entered_password != ADMIN_PASSWORD:
        st.info("Enter the admin password to access this page.")
        return

    # (rest of your admin logic unchanged)
    # ... keep your original function body below this point ...


# ----------------------------
# Main (simplified navigation)
# ----------------------------
def main() -> None:
    st.set_page_config(page_title="Reporting Tool @ HSG", layout="centered")

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
        page_submitted_issues(con)  # unchanged
    elif page == "Booking":
        page_booking(con)
    elif page == "Asset Tracking":
        page_assets(con)
    elif page == "Overview Dashboard":
        page_overview_dashboard(con)  # unchanged
    else:
        page_overwrite_status(con)


if __name__ == "__main__":
    main()
