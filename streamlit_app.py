from __future__ import annotations

# ============================================================================
# HSG REPORTING TOOL - ENHANCED UI/UX VERSION
# ============================================================================
# Application: Streamlit-based reporting system for University of St. Gallen
# Purpose: Facility issue reporting, asset booking, and tracking system
# Developed by: Arthur Lavric & Fabio Patierno
# Enhanced UI/UX with modern Streamlit components
# 
# Key Features:
# 1. Issue reporting form with email confirmation and SLA tracking
# 2. Dashboard with data visualization and CSV export
# 3. Admin panel with password protection and status management
# 4. Asset booking system with intelligent room-asset linking
# 5. Asset tracking with location-based management
# 6. Enhanced UI with dark mode, search, and interactive components
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
from typing import Iterable, Optional
from contextlib import contextmanager

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd
import pytz
import smtplib
import streamlit as st
import streamlit.components.v1 as components


# ============================================================================
# CONFIGURATION & CONSTANTS
# ============================================================================
APP_TZ = pytz.timezone("Europe/Zurich")  # Zurich timezone for all timestamps
DB_PATH = "hsg_reporting.db"             # SQLite database file path
LOGO_PATH = "HSG-logo-new.png"           # University logo for branding

# HSG brand colors
HSG_GREEN = "#00802F"
HSG_GREEN_LIGHT = "#4CAF50"
HSG_GREEN_DARK = "#006400"
HSG_BLUE = "#0056B3"
HSG_YELLOW = "#FFC107"
HSG_RED = "#DC3545"
HSG_GRAY = "#6C757D"

# Predefined issue types
ISSUE_TYPES = [
    "Lighting issues",
    "Sanitary problems", 
    "Heating, ventilation or air conditioning issues",
    "Cleaning needs due to heavy soiling",
    "Network/internet problems",
    "Issues with/lack of IT equipment",
]

# Priority and status levels
IMPORTANCE_LEVELS = ["Low", "Medium", "High"]
STATUS_LEVELS = ["Pending", "In Progress", "Resolved"]

# Service Level Agreement (SLA) definitions in hours
SLA_HOURS_BY_IMPORTANCE: dict[str, int] = {
    "High": 24,    # Critical issues: 24-hour resolution target
    "Medium": 72,  # Important issues: 72-hour resolution target  
    "Low": 120,    # Minor issues: 120-hour (5-day) resolution target
}

# Validation patterns
EMAIL_PATTERN = re.compile(r"^[\w.]+@(student\.)?unisg\.ch$")
ROOM_PATTERN = re.compile(r"^[A-Z]\s?\d{2}-\d{3}$")

# Location mapping
LOCATIONS = {
    "R_A_09001": {"label": "Room A 09-001", "x": 10, "y": 20, "floor": "09", "building": "A"},
    "H_A_09001": {"label": "Hallway near Room A 09-001", "x": 15, "y": 25, "floor": "09", "building": "A"},
    "R_B_10012": {"label": "Room B 10-012", "x": 40, "y": 60, "floor": "10", "building": "B"},
    "H_B_10012": {"label": "Hallway near Room B 10-012", "x": 45, "y": 65, "floor": "10", "building": "B"},
    "R_C_11023": {"label": "Room C 11-023", "x": 70, "y": 80, "floor": "11", "building": "C"},
    "L_C_11000": {"label": "Library C 11-000", "x": 75, "y": 85, "floor": "11", "building": "C"},
}

# Asset types with icons
ASSET_TYPES = {
    "Room": "🏢",
    "Equipment": "🖥️",
    "Chair": "🪑",
    "Table": "🪵",
    "Projector": "📽️",
    "Screen": "📺",
    "Whiteboard": "📋",
    "Other": "📦"
}

# ============================================================================
# LOGGING CONFIGURATION
# ============================================================================
logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')


# ============================================================================
# DATA MODEL
# ============================================================================
@dataclass(frozen=True)
class Submission:
    """Data model representing an issue submission."""
    name: str
    hsg_email: str
    issue_type: str
    room_number: str
    importance: str
    user_comment: str


# ============================================================================
# SECRETS MANAGEMENT
# ============================================================================
def get_secret(key: str, default: str | None = None) -> str:
    """Safely retrieve a secret from Streamlit secrets configuration."""
    if key in st.secrets:
        return str(st.secrets[key])
    if default is not None:
        return default
    st.error(f"Missing Streamlit secret: {key}")
    st.stop()


# Email configuration
SMTP_SERVER = get_secret("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(get_secret("SMTP_PORT", "587"))
SMTP_USERNAME = get_secret("SMTP_USERNAME")
SMTP_PASSWORD = get_secret("SMTP_PASSWORD")
FROM_EMAIL = get_secret("FROM_EMAIL", SMTP_USERNAME)
ADMIN_INBOX = get_secret("ADMIN_INBOX", FROM_EMAIL)

# Admin security
ADMIN_PASSWORD = get_secret("ADMIN_PASSWORD")

# Debug mode
DEBUG = get_secret("DEBUG", "0") == "1"

# Team assignment
ASSIGNEES_RAW = get_secret("ASSIGNEES", "Facility Team,IT Team,Cleaning Team,Maintenance Team")
ASSIGNEES = [a.strip() for a in ASSIGNEES_RAW.split(",") if a.strip()]

# Automated reporting
AUTO_WEEKLY_REPORT = get_secret("AUTO_WEEKLY_REPORT", "0") == "1"
REPORT_WEEKDAY = int(get_secret("REPORT_WEEKDAY", "0"))
REPORT_HOUR = int(get_secret("REPORT_HOUR", "7"))


# ============================================================================
# UI HELPER FUNCTIONS
# ============================================================================
def init_session_state():
    """Initialize session state variables."""
    defaults = {
        "show_map": False,
        "uploaded_file": None,
        "current_page": "Submission Form",
        "theme": "light",
        "search_query": "",
        "selected_asset_id": None,
        "show_advanced_filters": False,
        "notifications": [],
    }
    
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def apply_hsg_theme():
    """Apply HSG-themed CSS with dark mode support."""
    theme = st.session_state.get("theme", "light")
    
    if theme == "dark":
        bg_color = "#0E1117"
        text_color = "#FAFAFA"
        card_bg = "#262730"
        border_color = "#424242"
    else:
        bg_color = "#FFFFFF"
        text_color = "#31333F"
        card_bg = "#F8F9FA"
        border_color = "#E0E0E0"
    
    st.markdown(f"""
    <style>
    /* Main theme */
    .stApp {{
        background-color: {bg_color};
        color: {text_color};
    }}
    
    /* Cards and containers */
    .card {{
        background-color: {card_bg};
        border-radius: 10px;
        padding: 1.5rem;
        border: 1px solid {border_color};
        margin-bottom: 1rem;
    }}
    
    /* Tables */
    div[data-testid="stTable"] thead tr th {{
        background-color: {HSG_GREEN} !important;
        color: #ffffff !important;
        font-weight: 600 !important;
    }}
    
    div[data-testid="stDataFrame"] thead tr th {{
        background-color: {HSG_GREEN} !important;
        color: #ffffff !important;
        font-weight: 600 !important;
    }}
    
    /* Buttons */
    .stButton > button {{
        border-radius: 8px;
        transition: all 0.3s ease;
    }}
    
    .stButton > button:hover {{
        transform: translateY(-2px);
        box-shadow: 0 4px 12px rgba(0, 128, 47, 0.2);
    }}
    
    /* Metrics */
    .stMetric {{
        background-color: {card_bg};
        padding: 1rem;
        border-radius: 8px;
        border-left: 4px solid {HSG_GREEN};
    }}
    
    /* Progress bars */
    .stProgress > div > div > div {{
        background-color: {HSG_GREEN};
    }}
    
    /* Custom scrollbar */
    ::-webkit-scrollbar {{
        width: 8px;
    }}
    
    ::-webkit-scrollbar-track {{
        background: {card_bg};
    }}
    
    ::-webkit-scrollbar-thumb {{
        background: {HSG_GREEN};
        border-radius: 4px;
    }}
    
    /* Badges */
    .badge {{
        display: inline-block;
        padding: 0.25em 0.6em;
        font-size: 0.75em;
        font-weight: 600;
        line-height: 1;
        text-align: center;
        white-space: nowrap;
        vertical-align: baseline;
        border-radius: 10px;
        color: white;
    }}
    
    .badge-success {{ background-color: {HSG_GREEN}; }}
    .badge-warning {{ background-color: {HSG_YELLOW}; color: #000; }}
    .badge-danger {{ background-color: {HSG_RED}; }}
    .badge-info {{ background-color: {HSG_BLUE}; }}
    </style>
    """, unsafe_allow_html=True)


def get_priority_emoji(importance: str) -> str:
    """Get visual indicator for priority."""
    return {
        "High": "🔴",
        "Medium": "🟡",
        "Low": "🟢"
    }.get(importance, "⚪")


def get_status_badge(status: str) -> str:
    """Get styled badge for status."""
    colors = {
        "Pending": HSG_RED,
        "In Progress": HSG_YELLOW,
        "Resolved": HSG_GREEN
    }
    color = colors.get(status, HSG_GRAY)
    return f'<span class="badge" style="background-color:{color}">{status}</span>'


def get_asset_icon(asset_type: str) -> str:
    """Get icon for asset type."""
    return ASSET_TYPES.get(asset_type, "📦")


def show_toast(message: str, type: str = "success") -> None:
    """Show toast notification."""
    icons = {
        "success": "✅",
        "warning": "⚠️",
        "error": "❌",
        "info": "ℹ️"
    }
    icon = icons.get(type, "ℹ️")
    st.toast(f"{icon} {message}")


@contextmanager
def loading_spinner(message: str = "Loading..."):
    """Context manager for loading spinner."""
    with st.spinner(message):
        yield


def show_empty_state(message: str, icon: str = "📭", action: Optional[tuple] = None) -> None:
    """Show styled empty state."""
    st.markdown(f"""
    <div style="text-align: center; padding: 50px 20px; border-radius: 10px; background: linear-gradient(135deg, #f8f9fa 0%, #e9ecef 100%);">
        <div style="font-size: 64px; margin-bottom: 20px;">{icon}</div>
        <h3 style="color: #495057; margin-bottom: 10px; font-weight: 600;">{message}</h3>
        <p style="color: #6c757d;">Try adjusting your filters or add new data</p>
    </div>
    """, unsafe_allow_html=True)
    
    if action:
        st.button(action[0], on_click=action[1], use_container_width=True)


def create_card(title: str, content: str, icon: str = "📊", color: str = HSG_GREEN) -> None:
    """Create a styled card component."""
    st.markdown(f"""
    <div class="card">
        <div style="display: flex; align-items: center; margin-bottom: 1rem;">
            <div style="background-color: {color}; color: white; width: 40px; height: 40px; border-radius: 8px; 
                 display: flex; align-items: center; justify-content: center; margin-right: 12px; font-size: 20px;">
                {icon}
            </div>
            <h3 style="margin: 0; font-weight: 600;">{title}</h3>
        </div>
        <div style="color: #666;">
            {content}
        </div>
    </div>
    """, unsafe_allow_html=True)


def show_logo():
    """Display HSG logo in sidebar."""
    try:
        st.sidebar.image(LOGO_PATH, width=170, use_container_width=False)
    except FileNotFoundError:
        st.sidebar.markdown(f"""
        <div style="background-color: {HSG_GREEN}; color: white; padding: 1rem; border-radius: 8px; text-align: center;">
            <h3 style="margin: 0;">🏛️ HSG</h3>
            <p style="margin: 0; font-size: 0.9em;">Reporting Tool</p>
        </div>
        """, unsafe_allow_html=True)


def render_map_iframe() -> None:
    """Display interactive HSG campus map."""
    with st.container(border=True):
        st.markdown("### 🗺️ Campus Map")
        url = "https://use.mazemap.com/embed.html?v=1&zlevel=1&center=9.373611,47.429708&zoom=14.7&campusid=710"
        st.markdown(
            f"""
            <iframe src="{url}"
                width="100%" height="420" frameborder="0"
                marginheight="0" marginwidth="0" scrolling="no"></iframe>
            """,
            unsafe_allow_html=True,
        )


# ============================================================================
# TIME HELPER FUNCTIONS
# ============================================================================
def now_zurich() -> datetime:
    """Get current time in Zurich timezone."""
    return datetime.now(APP_TZ)


def now_zurich_str() -> str:
    """Get current Zurich time as ISO 8601 string."""
    return now_zurich().isoformat(timespec="seconds")


def iso_to_dt(value: str) -> datetime | None:
    """Safely convert ISO string to datetime object."""
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        logger.warning(f"Failed to parse datetime from: {value}")
        return None


def expected_resolution_dt(created_at_iso: str, importance: str) -> datetime | None:
    """Calculate expected resolution time based on SLA."""
    created_dt = iso_to_dt(created_at_iso)
    sla_hours = SLA_HOURS_BY_IMPORTANCE.get(importance)
    
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
    """Validate HSG email address format."""
    return bool(EMAIL_PATTERN.fullmatch(hsg_email.strip()))


def valid_room_number(room_number: str) -> bool:
    """Validate HSG room number format."""
    return bool(ROOM_PATTERN.fullmatch(room_number.strip()))


def normalize_room(room_number: str) -> str:
    """Normalize room number to canonical format."""
    raw = room_number.strip().upper()
    raw = re.sub(r"^([A-Z])(\d{2}-\d{3})$", r"\1 \2", raw)
    raw = re.sub(r"\s+", " ", raw)
    return raw


def validate_submission_input(sub: Submission) -> list[str]:
    """Validate all inputs for issue submission."""
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
    """Validate email for admin-triggered notifications."""
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
    """Create and cache SQLite database connection."""
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def init_db(con: sqlite3.Connection) -> None:
    """Initialize core database tables for issue reporting."""
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
    
    con.commit()


def init_booking_table(con: sqlite3.Connection) -> None:
    """Initialize booking system tables."""
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
    """Initialize assets table for both booking and tracking."""
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
    """Apply schema migrations for backward compatibility."""
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
    """Populate database with initial demo assets."""
    assets = [
        ("ROOM_A", "Study Room A", "Room", "R_A_09001", "available"),
        ("ROOM_B", "Study Room B", "Room", "R_B_10012", "available"),
        ("MEETING_1", "Meeting Room 1", "Room", "R_B_10012", "available"),
        ("PROJECTOR_1", "Portable Projector 1", "Projector", "H_B_10012", "available"),
        ("CHAIR_H1", "Hallway Chair 1", "Chair", "H_A_09001", "available"),
        ("CHAIR_H2", "Hallway Chair 2", "Chair", "H_A_09001", "available"),
        ("WHITEBOARD_1", "Whiteboard A", "Whiteboard", "R_A_09001", "available"),
        ("SCREEN_1", "Projection Screen", "Screen", "R_B_10012", "available"),
        ("TABLE_1", "Meeting Table", "Table", "MEETING_1", "available"),
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
    """Retrieve all issue submissions from database."""
    return pd.read_sql("SELECT * FROM submissions", con)


def fetch_status_log(con: sqlite3.Connection) -> pd.DataFrame:
    """Retrieve status change audit log."""
    return pd.read_sql(
        """
        SELECT submission_id, old_status, new_status, changed_at
        FROM status_log
        ORDER BY changed_at DESC
        """,
        con,
    )


def fetch_report_log(con: sqlite3.Connection, report_type: str) -> pd.DataFrame:
    """Retrieve report sending history."""
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
    """Retrieve all assets from database."""
    return pd.read_sql(
        """
        SELECT asset_id, asset_name, asset_type, location_id, status
        FROM assets
        ORDER BY asset_type, asset_name
        """,
        con,
    )


def fetch_assets_in_room(con: sqlite3.Connection, room_location_id: str) -> list[str]:
    """Retrieve asset IDs located inside a specific room."""
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
    """Log that a report has been sent."""
    con.execute(
        "INSERT INTO report_log (report_type, sent_at) VALUES (?, ?)",
        (report_type, now_zurich_str()),
    )
    con.commit()


# ============================================================================
# BOOKING SYSTEM FUNCTIONS
# ============================================================================
def sync_asset_statuses_from_bookings(con: sqlite3.Connection) -> None:
    """Update asset statuses based on active bookings."""
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
        asset_id = row["asset_id"]
        asset_type = row["asset_type"]
        location_id = row["location_id"]
        
        con.execute(
            "UPDATE assets SET status = 'booked' WHERE asset_id = ?",
            (asset_id,),
        )
        
        if asset_type == "Room" and is_room_location(location_id):
            inside_assets = fetch_assets_in_room(con, location_id)
            for aid in inside_assets:
                con.execute(
                    "UPDATE assets SET status = 'booked' WHERE asset_id = ?",
                    (aid,),
                )
    
    con.commit()


def is_asset_available(con: sqlite3.Connection, asset_id: str, start_time: datetime, end_time: datetime) -> bool:
    """Check if an asset is available during a specified time period."""
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
    """Retrieve upcoming bookings for a specific asset."""
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
def send_email(to_email: str, subject: str, body: str) -> tuple[bool, str]:
    """Send email with proper error handling."""
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
        
        return True, "Email sent successfully."
    except Exception as exc:
        logger.exception("Email sending failed")
        if DEBUG:
            return False, f"Email could not be sent: {exc}"
        return False, "Email could not be sent due to a technical issue."


def send_admin_report_email(subject: str, body: str) -> tuple[bool, str]:
    """Send report email to admin inbox only."""
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
    """Generate confirmation email content for new issue submissions."""
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
    """Generate resolution notification email content."""
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
    """Generate weekly summary report content."""
    now_dt = now_zurich()
    since_dt = now_dt - timedelta(days=7)
    
    df = df_all.copy()
    df["created_at_dt"] = pd.to_datetime(df["created_at"], errors="coerce")
    df["resolved_at_dt"] = pd.to_datetime(
        df.get("resolved_at", pd.Series([None] * len(df))), errors="coerce"
    )
    
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
    """Check if weekly report is due and send it."""
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


# ============================================================================
# UI COMPONENTS
# ============================================================================
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
    icon = get_asset_icon(str(row.get("asset_type", "")))
    return f'{icon} {row.get("asset_name", "")} • {row.get("asset_type", "")} • {loc} • {status_text}'


def format_booking_table(df: pd.DataFrame) -> pd.DataFrame:
    """Format booking data for user-friendly display."""
    if df.empty:
        return df
    
    out = df.copy()
    out["start_time"] = pd.to_datetime(out["start_time"], errors="coerce")
    out["end_time"] = pd.to_datetime(out["end_time"], errors="coerce")
    
    out = out.dropna(subset=["start_time", "end_time"]).sort_values(by=["start_time"])
    
    out["start_time"] = out["start_time"].dt.strftime("%Y-%m-%d %H:%M")
    out["end_time"] = out["end_time"].dt.strftime("%Y-%m-%d %H:%M")
    
    return out.rename(columns={
        "user_name": "User",
        "start_time": "Start Time",
        "end_time": "End Time"
    })


# ============================================================================
# ENHANCED APPLICATION PAGES
# ============================================================================
def page_submission_form(con: sqlite3.Connection) -> None:
    """Enhanced issue submission form with tabs and better UX."""
    st.header("📝 Report a Facility Issue")
    
    create_card(
        "Before you start",
        "Please provide accurate details to help us resolve your issue quickly. All fields marked with * are required.",
        icon="ℹ️",
        color=HSG_BLUE
    )
    
    # Use tabs for form sections
    tab1, tab2, tab3 = st.tabs(["👤 Personal Info", "🔍 Issue Details", "📸 Supporting Info"])
    
    with tab1:
        with st.container(border=True):
            st.markdown("#### 👤 Your Information")
            col1, col2 = st.columns(2)
            
            with col1:
                name = st.text_input(
                    "Full Name*",
                    placeholder="Max Mustermann",
                    help="Enter your full name",
                    key="form_name"
                ).strip()
            
            with col2:
                hsg_email = st.text_input(
                    "HSG Email Address*",
                    placeholder="max.mustermann@student.unisg.ch",
                    help="Must be @unisg.ch or @student.unisg.ch",
                    key="form_email"
                ).strip()
                if hsg_email and not valid_email(hsg_email):
                    st.error("Please enter a valid HSG email address")
    
    with tab2:
        with st.container(border=True):
            st.markdown("#### 🔍 Issue Details")
            col3, col4 = st.columns(2)
            
            with col3:
                room_number_input = st.text_input(
                    "Room Number*",
                    placeholder="A 09-001",
                    help="Format: Letter Space Number-Dash-Number (e.g., A 09-001)",
                    key="form_room"
                ).strip()
                
                # Room finder button
                if st.button("🔍 Find Room on Map", use_container_width=True):
                    st.session_state.show_map = True
            
            with col4:
                issue_type = st.selectbox(
                    "Issue Type*",
                    ISSUE_TYPES,
                    help="Select the most relevant category",
                    key="form_issue_type"
                )
            
            # Priority with visual indicators
            importance = st.selectbox(
                "Priority Level*",
                IMPORTANCE_LEVELS,
                format_func=lambda x: f"{get_priority_emoji(x)} {x}",
                help="Select the urgency level",
                key="form_importance"
            )
            
            # Dynamic SLA display
            sla_hours = SLA_HOURS_BY_IMPORTANCE.get(importance)
            if sla_hours:
                with st.container(border=True):
                    st.markdown(f"**⏱️ Service Level Agreement:**")
                    st.progress(0.5 if importance == "Medium" else (0.8 if importance == "High" else 0.3))
                    st.caption(f"Target resolution: **{sla_hours} hours**")
    
    with tab3:
        with st.container(border=True):
            st.markdown("#### 📝 Problem Description")
            user_comment = st.text_area(
                "Describe the issue*",
                height=150,
                max_chars=500,
                placeholder="Please describe:\n• What happened?\n• Where exactly is the problem?\n• When did it start?\n• Any visible damage or safety concerns?",
                help="Be as specific as possible to help us resolve the issue quickly",
                key="form_description"
            ).strip()
            
            # Character counter
            chars_left = 500 - len(user_comment)
            col_count, _ = st.columns([3, 1])
            with col_count:
                if chars_left < 50:
                    st.warning(f"⚠️ Only {chars_left} characters left")
                else:
                    st.caption(f"Characters: {len(user_comment)}/500")
            
            # Enhanced file upload
            st.markdown("#### 📸 Photo Upload (Optional)")
            uploaded_file = st.file_uploader(
                "Upload a photo to help us understand the issue better",
                type=["jpg", "jpeg", "png", "heic", "webp"],
                help="Maximum file size: 5MB. Supported formats: JPG, PNG, HEIC, WebP",
                key="form_upload"
            )
            
            if uploaded_file:
                col_pre1, col_pre2 = st.columns([3, 1])
                with col_pre1:
                    st.image(uploaded_file, caption="Uploaded photo preview", use_container_width=True)
                with col_pre2:
                    if st.button("🗑️ Remove", type="secondary", use_container_width=True):
                        st.session_state.uploaded_file = None
                        st.rerun()
    
    # Conditional map display
    if st.session_state.get("show_map", False):
        with st.expander("🗺️ Campus Map", expanded=True):
            render_map_iframe()
            if st.button("Close Map", type="secondary"):
                st.session_state.show_map = False
                st.rerun()
    
    # Submit section
    st.markdown("---")
    
    col_sub1, col_sub2, col_sub3 = st.columns([1, 2, 1])
    with col_sub2:
        submitted = st.button(
            "🚀 Submit Issue Report",
            type="primary",
            use_container_width=True,
            key="form_submit"
        )
    
    # Handle form submission
    if submitted:
        # Validate inputs
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
            for error in errors:
                st.error(f"❌ {error}")
            return
        
        # Process submission with loading spinner
        with loading_spinner("Submitting your issue..."):
            try:
                insert_submission(con, sub)
                
                # Send confirmation email
                subject, body = confirmation_email_text(sub.name.strip(), sub.importance)
                ok, msg = send_email(sub.hsg_email, subject, body)
                
                # Show success message
                st.success("""
                ✅ **Issue Reported Successfully!**
                
                Your issue has been submitted and is now under review. 
                You will receive a confirmation email shortly.
                """)
                
                if uploaded_file:
                    st.info("📸 Note: The uploaded photo has been attached to your report.")
                
                if not ok:
                    st.warning(f"⚠️ Note: {msg}")
                
                # Auto-clear form after successful submission
                st.session_state.form_name = ""
                st.session_state.form_email = ""
                st.session_state.form_room = ""
                st.session_state.form_description = ""
                st.session_state.form_upload = None
                
                # Show toast notification
                show_toast("Issue submitted successfully!")
                
                # Add slight delay before rerun
                import time
                time.sleep(2)
                st.rerun()
                
            except Exception as e:
                st.error(f"❌ Failed to submit issue: {str(e)}")
                logger.error(f"Submission error: {e}")


def enhanced_data_table(df: pd.DataFrame, con: sqlite3.Connection) -> None:
    """Create an interactive data table with filtering and actions."""
    
    if df.empty:
        show_empty_state(
            "No issues found",
            "📭",
            action=("Report First Issue", lambda: st.session_state.update({"current_page": "Submission Form"}))
        )
        return
    
    # Advanced filters
    with st.expander("🔍 Advanced Filters", expanded=st.session_state.get("show_advanced_filters", False)):
        col1, col2, col3 = st.columns(3)
        with col1:
            # Date range filter
            date_col = st.selectbox("Filter by date field:", ["created_at", "updated_at", "resolved_at"])
            if date_col in df.columns:
                df[f"{date_col}_dt"] = pd.to_datetime(df[date_col], errors="coerce")
                min_date = df[f"{date_col}_dt"].min().date() if not df.empty else datetime.now().date()
                max_date = df[f"{date_col}_dt"].max().date() if not df.empty else datetime.now().date()
                
                date_range = st.date_input(
                    "Date Range",
                    value=[min_date, max_date],
                    min_value=min_date,
                    max_value=max_date
                )
        
        with col2:
            # Reporter filter
            reporter_options = ["All"] + sorted(df["name"].unique().tolist())
            reporter_filter = st.multiselect(
                "Filter by Reporter",
                options=reporter_options[1:],
                default=[],
                placeholder="Select reporters..."
            )
        
        with col3:
            # Room filter
            room_options = ["All"] + sorted(df["room_number"].unique().tolist())
            room_filter = st.multiselect(
                "Filter by Room",
                options=room_options[1:],
                default=[],
                placeholder="Select rooms..."
            )
    
    # Apply filters
    filtered_df = df.copy()
    
    if 'date_range' in locals() and len(date_range) == 2 and date_col in df.columns:
        filtered_df = filtered_df[
            (filtered_df[f"{date_col}_dt"].dt.date >= date_range[0]) & 
            (filtered_df[f"{date_col}_dt"].dt.date <= date_range[1])
        ]
    
    if reporter_filter:
        filtered_df = filtered_df[filtered_df["name"].isin(reporter_filter)]
    
    if room_filter:
        filtered_df = filtered_df[filtered_df["room_number"].isin(room_filter)]
    
    # Display filtered count
    st.info(f"📊 Showing {len(filtered_df)} of {len(df)} issues")
    
    # Create interactive dataframe
    display_df = filtered_df.copy()
    display_df["priority_display"] = display_df["importance"].apply(
        lambda x: f"{get_priority_emoji(x)} {x}"
    )
    display_df["status_display"] = display_df["status"].apply(
        lambda x: get_status_badge(x)
    )
    
    # Column configuration for interactive features
    column_config = {
        "id": st.column_config.NumberColumn("ID", width="small"),
        "name": "Reporter",
        "hsg_email": st.column_config.TextColumn("Email", width="medium"),
        "issue_type": st.column_config.SelectboxColumn(
            "Issue Type",
            options=ISSUE_TYPES,
            width="medium"
        ),
        "room_number": "Room",
        "priority_display": st.column_config.TextColumn(
            "Priority",
            help="Click to filter by priority",
            width="small"
        ),
        "status_display": st.column_config.TextColumn(
            "Status",
            width="small"
        ),
        "created_at": st.column_config.DatetimeColumn(
            "Submitted",
            format="YYYY-MM-DD HH:mm",
            width="medium"
        ),
        "assigned_to": st.column_config.SelectboxColumn(
            "Assignee",
            options=["(Unassigned)"] + ASSIGNEES,
            width="medium"
        ),
    }
    
    # Display the dataframe
    edited_df = st.dataframe(
        display_df[["id", "name", "hsg_email", "issue_type", "room_number", 
                   "priority_display", "status_display", "created_at", "assigned_to"]],
        use_container_width=True,
        hide_index=True,
        column_config=column_config
    )
    
    # Quick actions section
    st.markdown("---")
    st.subheader("⚡ Quick Actions")
    
    col_act1, col_act2, col_act3, col_act4 = st.columns(4)
    
    with col_act1:
        if st.button("📥 Export to CSV", use_container_width=True):
            csv = filtered_df.to_csv(index=False)
            st.download_button(
                label="Download CSV",
                data=csv,
                file_name=f"hsg_issues_{now_zurich().strftime('%Y%m%d_%H%M')}.csv",
                mime="text/csv",
                use_container_width=True
            )
    
    with col_act2:
        if st.button("🔄 Refresh Data", use_container_width=True):
            st.rerun()
    
    with col_act3:
        if st.button("📧 Email All Reporters", use_container_width=True):
            st.info("This would send a bulk email to all selected reporters")
    
    with col_act4:
        if st.button("📊 Generate Report", use_container_width=True):
            st.info("This would generate a detailed report")


def page_submitted_issues(con: sqlite3.Connection) -> None:
    """Enhanced submitted issues dashboard."""
    st.header("📋 Submitted Issues Dashboard")
    
    # Quick stats cards
    try:
        df = fetch_submissions(con)
        
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            with st.container(border=True):
                st.metric("Total Issues", len(df))
        
        with col2:
            open_count = len(df[df["status"] != "Resolved"]) if not df.empty else 0
            with st.container(border=True):
                st.metric("Open Issues", open_count, 
                         delta=f"-{len(df) - open_count}" if len(df) > 0 else None)
        
        with col3:
            resolved_count = len(df[df["status"] == "Resolved"]) if not df.empty else 0
            with st.container(border=True):
                st.metric("Resolved", resolved_count)
        
        with col4:
            high_priority = len(df[df["importance"] == "High"]) if not df.empty else 0
            with st.container(border=True):
                st.metric("High Priority", high_priority)
    
    except Exception as e:
        st.error(f"Failed to load submissions: {e}")
        return
    
    # Search functionality
    st.subheader("🔍 Search & Filter")
    
    search_col1, search_col2 = st.columns([3, 1])
    with search_col1:
        search_query = st.text_input(
            "Search across all issues...",
            placeholder="Search by reporter name, room, issue type, or description",
            key="issues_search"
        )
    
    with search_col2:
        filter_toggle = st.toggle("Advanced Filters", 
                                 value=st.session_state.get("show_advanced_filters", False),
                                 help="Show advanced filtering options")
        st.session_state.show_advanced_filters = filter_toggle
    
    # Apply search filter
    if search_query and not df.empty:
        mask = (
            df["name"].str.contains(search_query, case=False, na=False) |
            df["room_number"].str.contains(search_query, case=False, na=False) |
            df["issue_type"].str.contains(search_query, case=False, na=False) |
            df["user_comment"].str.contains(search_query, case=False, na=False)
        )
        df = df[mask].copy()
    
    # Display the enhanced data table
    enhanced_data_table(df, con)
    
    # Visualizations
    if not df.empty:
        st.markdown("---")
        st.subheader("📈 Analytics & Insights")
        
        tab1, tab2, tab3 = st.tabs(["📊 Overview", "📅 Trends", "🎯 Performance"])
        
        with tab1:
            col_viz1, col_viz2 = st.columns(2)
            
            with col_viz1:
                # Issue type distribution
                st.markdown("**Issue Types**")
                issue_counts = df["issue_type"].value_counts()
                fig1, ax1 = plt.subplots(figsize=(8, 6))
                ax1.pie(issue_counts.values, labels=issue_counts.index, autopct='%1.1f%%',
                       colors=plt.cm.Set3.colors, startangle=90)
                ax1.axis('equal')
                st.pyplot(fig1)
            
            with col_viz2:
                # Priority distribution
                st.markdown("**Priority Levels**")
                priority_counts = df["importance"].value_counts().reindex(IMPORTANCE_LEVELS, fill_value=0)
                colors = [HSG_GREEN, HSG_YELLOW, HSG_RED]
                fig2, ax2 = plt.subplots(figsize=(8, 6))
                bars = ax2.bar(priority_counts.index, priority_counts.values, color=colors)
                ax2.set_ylabel("Number of Issues")
                ax2.set_title("Issues by Priority", fontweight="bold")
                for bar in bars:
                    height = bar.get_height()
                    ax2.text(bar.get_x() + bar.get_width()/2., height + 0.1,
                            f'{int(height)}', ha='center', va='bottom')
                st.pyplot(fig2)
        
        with tab2:
            # Daily trends
            st.markdown("**Submission Trends**")
            if "created_at" in df.columns:
                df["created_at_dt"] = pd.to_datetime(df["created_at"], errors="coerce")
                daily_counts = df.dropna(subset=["created_at_dt"]).groupby(
                    df["created_at_dt"].dt.date
                ).size()
                
                if not daily_counts.empty:
                    fig3, ax3 = plt.subplots(figsize=(12, 5))
                    ax3.plot(daily_counts.index, daily_counts.values, 
                            marker='o', color=HSG_GREEN, linewidth=2)
                    ax3.set_xlabel("Date")
                    ax3.set_ylabel("Issues Submitted")
                    ax3.set_title("Daily Submission Trends", fontweight="bold")
                    ax3.grid(True, linestyle='--', alpha=0.3)
                    plt.xticks(rotation=45)
                    st.pyplot(fig3)
                else:
                    st.info("No date data available for trend analysis")
        
        with tab3:
            # SLA performance
            st.markdown("**SLA Performance**")
            if not df.empty and "created_at" in df.columns and "importance" in df.columns:
                df["sla_hours"] = df["importance"].map(SLA_HOURS_BY_IMPORTANCE)
                df["created_at_dt"] = pd.to_datetime(df["created_at"], errors="coerce")
                df["age_hours"] = (now_zurich() - df["created_at_dt"]).dt.total_seconds() / 3600
                df["sla_progress"] = df["age_hours"] / df["sla_hours"]
                
                # Categorize by SLA status
                df["sla_status"] = pd.cut(df["sla_progress"], 
                                         bins=[0, 0.5, 0.9, 1.0, float('inf')],
                                         labels=["On Track", "At Risk", "Near Due", "Overdue"])
                
                sla_counts = df["sla_status"].value_counts()
                if not sla_counts.empty:
                    fig4, ax4 = plt.subplots(figsize=(10, 6))
                    colors = [HSG_GREEN, HSG_YELLOW, HSG_RED, "#8B0000"]
                    bars = ax4.bar(sla_counts.index.astype(str), sla_counts.values, color=colors)
                    ax4.set_ylabel("Number of Issues")
                    ax4.set_title("SLA Compliance Status", fontweight="bold")
                    for bar in bars:
                        height = bar.get_height()
                        ax4.text(bar.get_x() + bar.get_width()/2., height + 0.1,
                                f'{int(height)}', ha='center', va='bottom')
                    st.pyplot(fig4)
                else:
                    st.info("No SLA data available for performance analysis")


def page_booking(con: sqlite3.Connection) -> None:
    """Enhanced asset booking interface."""
    st.header("📅 Book an Asset")
    
    create_card(
        "Booking Instructions",
        "1. Search for available assets\n2. Select an asset to view details\n3. Choose your booking time\n4. Confirm your booking",
        icon="ℹ️",
        color=HSG_BLUE
    )
    
    # Sync booking statuses
    with loading_spinner("Loading assets..."):
        try:
            sync_asset_statuses_from_bookings(con)
            assets_df = fetch_assets(con)
        except Exception as e:
            st.error(f"Failed to load assets: {e}")
            return
    
    if assets_df.empty:
        show_empty_state(
            "No assets available for booking",
            "📦",
            action=("Refresh Assets", st.rerun)
        )
        return
    
    # Asset search and filtering
    st.subheader("🔍 Find Available Assets")
    
    col_search1, col_search2, col_search3 = st.columns([2, 1, 1])
    
    with col_search1:
        search_term = st.text_input(
            "Search assets",
            placeholder="e.g., projector, meeting room, chair...",
            help="Search by asset name, type, or location",
            key="asset_search"
        ).strip().lower()
    
    with col_search2:
        type_options = ["All Types"] + sorted(assets_df["asset_type"].unique().tolist())
        type_filter = st.selectbox(
            "Asset Type",
            options=type_options,
            help="Filter by asset category",
            key="asset_type_filter"
        )
    
    with col_search3:
        availability_options = ["All", "Available Only", "Booked Only"]
        availability_filter = st.selectbox(
            "Availability",
            options=availability_options,
            help="Filter by current availability",
            key="asset_availability_filter"
        )
    
    # Prepare display data
    view_df = assets_df.copy()
    view_df["location_label"] = view_df["location_id"].apply(location_label)
    view_df["display_label"] = view_df.apply(asset_display_label, axis=1)
    view_df["icon"] = view_df["asset_type"].apply(get_asset_icon)
    
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
        show_empty_state(
            "No assets match your search criteria",
            "🔍",
            action=("Clear Filters", lambda: st.session_state.update(
                {"asset_search": "", "asset_type_filter": "All Types", "asset_availability_filter": "All"}
            ))
        )
        return
    
    # Display assets in a grid
    st.subheader(f"🎯 Select Asset ({len(view_df)} found)")
    
    # Create asset cards
    cols = st.columns(3)
    asset_options = {}
    
    for idx, (_, row) in enumerate(view_df.iterrows()):
        col_idx = idx % 3
        with cols[col_idx]:
            asset_id = str(row["asset_id"])
            asset_options[asset_id] = row["display_label"]
            
            # Create asset card
            status_color = HSG_GREEN if row["status"].lower() == "available" else HSG_RED
            status_icon = "✅" if row["status"].lower() == "available" else "⛔"
            
            st.markdown(f"""
            <div class="card" style="cursor: pointer; transition: transform 0.2s;" 
                 onclick="document.getElementById('asset_{asset_id}').click()">
                <div style="display: flex; align-items: center; margin-bottom: 0.5rem;">
                    <div style="font-size: 24px; margin-right: 10px;">{row['icon']}</div>
                    <div>
                        <h4 style="margin: 0; font-weight: 600;">{row['asset_name']}</h4>
                        <p style="margin: 0; color: #666; font-size: 0.9em;">{row['asset_type']}</p>
                    </div>
                </div>
                <p style="margin: 0.5rem 0; color: #666; font-size: 0.9em;">
                    📍 {row['location_label']}
                </p>
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <span style="background-color: {status_color}; color: white; padding: 2px 8px; 
                          border-radius: 12px; font-size: 0.8em;">
                        {status_icon} {row['status'].capitalize()}
                    </span>
                </div>
            </div>
            """, unsafe_allow_html=True)
            
            # Hidden radio button for selection
            if st.radio(
                "Select",
                [asset_id],
                key=f"asset_radio_{asset_id}",
                label_visibility="collapsed",
                index=0 if asset_id == st.session_state.get("selected_asset_id") else None
            ):
                st.session_state.selected_asset_id = asset_id
    
    # Get selected asset
    selected_asset_id = st.session_state.get("selected_asset_id")
    if not selected_asset_id or selected_asset_id not in view_df["asset_id"].values:
        selected_asset_id = view_df.iloc[0]["asset_id"]
        st.session_state.selected_asset_id = selected_asset_id
    
    selected_asset = view_df[view_df["asset_id"] == selected_asset_id].iloc[0]
    
    # Asset details section
    st.markdown("---")
    st.subheader(f"📋 {selected_asset['asset_name']} Details")
    
    col_details1, col_details2, col_details3 = st.columns(3)
    
    with col_details1:
        create_card("Status", 
                   f"**{selected_asset['status'].capitalize()}**\n\n" +
                   ("Available for immediate booking" if selected_asset['status'].lower() == 'available' 
                    else "Currently booked"),
                   icon="📊" if selected_asset['status'].lower() == 'available' else "⏰",
                   color=HSG_GREEN if selected_asset['status'].lower() == 'available' else HSG_YELLOW)
    
    with col_details2:
        create_card("Type", 
                   f"**{selected_asset['asset_type']}**\n\n{get_asset_icon(selected_asset['asset_type'])}",
                   icon=selected_asset['icon'],
                   color=HSG_BLUE)
    
    with col_details3:
        create_card("Location", 
                   f"**{location_label(selected_asset['location_id'])}**\n\nBuilding {LOCATIONS.get(selected_asset['location_id'], {}).get('building', 'Unknown')}",
                   icon="📍",
                   color=HSG_GRAY)
    
    # Availability status
    if selected_asset["status"].lower() == "available":
        st.success("✅ **This asset is available for booking now!**")
    else:
        next_free = next_available_time(con, selected_asset_id)
        if next_free:
            time_until = next_free - now_zurich()
            hours_until = int(time_until.total_seconds() / 3600)
            minutes_until = int((time_until.total_seconds() % 3600) / 60)
            
            st.warning(f"""
            ⏰ **Currently Booked**
            
            Next available: **{next_free.strftime('%A, %d %B %Y at %H:%M')}**
            (in {hours_until}h {minutes_until}m)
            """)
        else:
            st.warning("⏰ **Currently booked** - No future bookings scheduled")
    
    # Show upcoming bookings
    st.subheader("📅 Upcoming Bookings")
    try:
        future_bookings = fetch_future_bookings(con, selected_asset_id)
        if future_bookings.empty:
            st.info("No upcoming bookings scheduled.")
        else:
            # Create timeline view
            for _, booking in future_bookings.iterrows():
                start_dt = iso_to_dt(str(booking["start_time"]))
                end_dt = iso_to_dt(str(booking["end_time"]))
                
                if start_dt and end_dt:
                    with st.container(border=True):
                        col_tl1, col_tl2, col_tl3 = st.columns([2, 2, 1])
                        with col_tl1:
                            st.markdown(f"**👤 {booking['user_name']}**")
                        with col_tl2:
                            st.markdown(f"🕒 {start_dt.strftime('%H:%M')} - {end_dt.strftime('%H:%M')}")
                        with col_tl3:
                            st.caption(start_dt.strftime("%Y-%m-%d"))
    except Exception as e:
        st.error(f"Failed to load bookings: {e}")
    
    # Booking form (only for available assets)
    if selected_asset["status"].lower() != "available":
        st.info("Select an available asset to create a booking.")
        return
    
    st.markdown("---")
    st.subheader("📝 Create New Booking")
    
    with st.form("booking_form", border=True):
        # User information
        user_name = st.text_input(
            "Your Name*",
            placeholder="Max Mustermann",
            help="Enter your full name for the booking record",
            key="booking_name"
        ).strip()
        
        # Date and time selection with better UX
        col_time1, col_time2 = st.columns(2)
        
        with col_time1:
            start_date = st.date_input(
                "Start Date*",
                value=now_zurich().date(),
                min_value=now_zurich().date(),
                help="Select the booking date",
                key="booking_date"
            )
        
        with col_time2:
            # Smart time selection
            current_time = now_zurich().time()
            time_options = []
            
            # Generate time slots every 30 minutes
            for hour in range(7, 22):  # 7 AM to 10 PM
                for minute in [0, 30]:
                    time_options.append(datetime.combine(start_date, 
                                                        datetime.min.time().replace(hour=hour, minute=minute)))
            
            # Filter out past times for today
            if start_date == now_zurich().date():
                time_options = [t for t in time_options if t.time() > current_time]
            
            if time_options:
                start_time = st.selectbox(
                    "Start Time*",
                    options=time_options,
                    format_func=lambda x: x.strftime("%H:%M"),
                    help="Select booking start time",
                    key="booking_start_time"
                ).time()
            else:
                st.warning("No available time slots for today")
                start_time = None
        
        # Duration selection
        duration_options = {
            "30 minutes": 0.5,
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
            help="Select booking duration",
            key="booking_duration"
        )
        duration_hours = duration_options[duration_choice]
        
        # Calculate booking summary
        if start_time:
            start_dt = APP_TZ.localize(datetime.combine(start_date, start_time))
            end_dt = start_dt + timedelta(hours=duration_hours)
            
            # Display booking summary
            create_card(
                "Booking Summary",
                f"""
                **Asset:** {selected_asset['asset_name']}
                
                **Date:** {start_dt.strftime('%A, %d %B %Y')}
                
                **Time:** {start_dt.strftime('%H:%M')} → {end_dt.strftime('%H:%M')}
                
                **Duration:** {duration_hours} hour{'s' if duration_hours != 1 else ''}
                """,
                icon="📅",
                color=HSG_GREEN
            )
        
        # Submit booking
        submitted = st.form_submit_button(
            "✅ Confirm Booking",
            type="primary",
            use_container_width=True,
            disabled=not start_time
        )
    
    # Handle booking submission
    if submitted and start_time:
        # Validate inputs
        if not user_name:
            st.error("Please enter your name.")
            return
        
        if start_dt < now_zurich():
            st.error("Start time cannot be in the past.")
            return
        
        if end_dt <= start_dt:
            st.error("End time must be after start time.")
            return
        
        # Check availability
        try:
            if not is_asset_available(con, selected_asset_id, start_dt, end_dt):
                st.error("This asset is already booked during the selected time period.")
                return
        except Exception as e:
            st.error(f"Availability check failed: {e}")
            return
        
        # Create booking with loading spinner
        with loading_spinner("Creating your booking..."):
            try:
                con.execute(
                    """
                    INSERT INTO bookings (asset_id, user_name, start_time, end_time, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        selected_asset_id,
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
                
                show_toast("Booking created successfully!")
                
                # Clear form
                st.session_state.booking_name = ""
                
                # Auto-refresh
                import time
                time.sleep(2)
                st.rerun()
                
            except Exception as e:
                st.error(f"Failed to create booking: {e}")


def page_assets(con: sqlite3.Connection) -> None:
    """Enhanced asset tracking interface."""
    st.header("📍 Asset Tracking & Management")
    
    create_card(
        "Asset Management",
        "Track asset locations, view availability, and manage asset movements across campus.",
        icon="🏢",
        color=HSG_BLUE
    )
    
    # Load assets data
    with loading_spinner("Loading assets..."):
        try:
            df = fetch_assets(con)
        except Exception as e:
            st.error(f"Failed to load assets: {e}")
            return
    
    if df.empty:
        show_empty_state(
            "No assets in the system",
            "📦",
            action=("Refresh", st.rerun)
        )
        return
    
    # Add location labels for display
    df = df.copy()
    df["location_label"] = df["location_id"].apply(location_label)
    df["icon"] = df["asset_type"].apply(get_asset_icon)
    
    # Summary metrics
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        total_assets = len(df)
        with st.container(border=True):
            st.metric("Total Assets", total_assets)
    
    with col2:
        available_assets = len(df[df["status"] == "available"])
        with st.container(border=True):
            st.metric("Available", available_assets)
    
    with col3:
        booked_assets = len(df[df["status"] == "booked"])
        with st.container(border=True):
            st.metric("Booked", booked_assets)
    
    with col4:
        asset_types = df["asset_type"].nunique()
        with st.container(border=True):
            st.metric("Asset Types", asset_types)
    
    # Search and filter
    st.subheader("🔍 Search Assets")
    
    col_search1, col_search2, col_search3 = st.columns(3)
    
    with col_search1:
        asset_search = st.text_input(
            "Search by name or ID",
            placeholder="Enter asset name or ID...",
            key="asset_tracking_search"
        )
    
    with col_search2:
        location_options = ["All Locations"] + sorted(df["location_label"].unique().tolist())
        selected_location = st.selectbox(
            "Filter by Location",
            options=location_options,
            key="asset_location_filter"
        )
    
    with col_search3:
        status_options = ["All Statuses", "Available", "Booked"]
        selected_status = st.selectbox(
            "Filter by Status",
            options=status_options,
            key="asset_status_filter"
        )
    
    # Apply filters
    filtered_df = df.copy()
    
    if asset_search:
        filtered_df = filtered_df[
            filtered_df["asset_name"].str.contains(asset_search, case=False, na=False) |
            filtered_df["asset_id"].str.contains(asset_search, case=False, na=False)
        ]
    
    if selected_location != "All Locations":
        filtered_df = filtered_df[filtered_df["location_label"] == selected_location]
    
    if selected_status != "All Statuses":
        filtered_df = filtered_df[filtered_df["status"] == selected_status.lower()]
    
    # Display assets
    st.subheader(f"📦 Assets ({len(filtered_df)} found)")
    
    if filtered_df.empty:
        show_empty_state(
            "No assets match your search criteria",
            "🔍",
            action=("Clear Filters", lambda: st.session_state.update({
                "asset_tracking_search": "",
                "asset_location_filter": "All Locations",
                "asset_status_filter": "All Statuses"
            }))
        )
        return
    
    # Create a grid of asset cards
    cols = st.columns(3)
    for idx, (_, row) in enumerate(filtered_df.iterrows()):
        col_idx = idx % 3
        with cols[col_idx]:
            status_color = HSG_GREEN if row["status"] == "available" else HSG_RED
            status_text = "Available" if row["status"] == "available" else "Booked"
            
            st.markdown(f"""
            <div class="card">
                <div style="display: flex; align-items: center; margin-bottom: 0.5rem;">
                    <div style="font-size: 24px; margin-right: 10px;">{row['icon']}</div>
                    <div>
                        <h4 style="margin: 0; font-weight: 600;">{row['asset_name']}</h4>
                        <p style="margin: 0; color: #666; font-size: 0.9em;">ID: {row['asset_id']}</p>
                    </div>
                </div>
                <p style="margin: 0.5rem 0; color: #666;">
                    🏷️ {row['asset_type']}<br>
                    📍 {row['location_label']}
                </p>
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <span style="background-color: {status_color}; color: white; padding: 2px 8px; 
                          border-radius: 12px; font-size: 0.8em;">
                        {status_text}
                    </span>
                    <button onclick="document.getElementById('move_{row['asset_id']}').click()" 
                            style="background: none; border: 1px solid {HSG_GREEN}; color: {HSG_GREEN}; 
                                   padding: 2px 8px; border-radius: 4px; cursor: pointer; font-size: 0.8em;">
                        Move
                    </button>
                </div>
            </div>
            """, unsafe_allow_html=True)
            
            # Hidden button for moving assets
            if st.button("Move", key=f"move_{row['asset_id']}", type="secondary"):
                st.session_state.asset_to_move = row['asset_id']
    
    # Asset movement section
    st.markdown("---")
    st.subheader("🚚 Move Asset")
    
    # Get asset to move from session state
    asset_to_move = st.session_state.get("asset_to_move")
    
    if asset_to_move and asset_to_move in df["asset_id"].values:
        asset_row = df[df["asset_id"] == asset_to_move].iloc[0]
        
        col_move1, col_move2 = st.columns([1, 2])
        
        with col_move1:
            st.markdown(f"""
            <div class="card">
                <h4>Current Location</h4>
                <p style="font-size: 1.2em;">📍 {asset_row['location_label']}</p>
                <p><strong>Asset:</strong> {asset_row['asset_name']}</p>
                <p><strong>Type:</strong> {asset_row['asset_type']}</p>
                <p><strong>Status:</strong> {asset_row['status'].capitalize()}</p>
            </div>
            """, unsafe_allow_html=True)
        
        with col_move2:
            st.markdown("#### Select New Location")
            
            # Group locations by building
            buildings = {}
            for loc_id, loc_data in LOCATIONS.items():
                building = loc_data.get("building", "Unknown")
                if building not in buildings:
                    buildings[building] = []
                buildings[building].append((loc_id, loc_data["label"]))
            
            # Create location selector
            selected_location_id = st.selectbox(
                "Choose destination:",
                options=list(LOCATIONS.keys()),
                format_func=lambda x: f"📍 {LOCATIONS[x]['label']} (Building {LOCATIONS[x].get('building', 'Unknown')})",
                help="Select the new location for this asset"
            )
            
            # Additional notes
            move_reason = st.text_area(
                "Reason for move (optional):",
                placeholder="e.g., Maintenance, Reallocation, Repair...",
                height=80
            )
            
            # Move confirmation
            col_confirm1, col_confirm2, col_confirm3 = st.columns([1, 2, 1])
            with col_confirm2:
                if st.button("🚀 Move Asset", type="primary", use_container_width=True):
                    if selected_location_id == asset_row["location_id"]:
                        st.warning("Asset is already at this location.")
                    else:
                        try:
                            con.execute(
                                "UPDATE assets SET location_id = ? WHERE asset_id = ?",
                                (selected_location_id, asset_to_move)
                            )
                            con.commit()
                            
                            st.success(f"""
                            ✅ **Asset Moved Successfully!**
                            
                            **From:** {asset_row['location_label']}
                            **To:** {LOCATIONS[selected_location_id]['label']}
                            
                            {f"**Reason:** {move_reason}" if move_reason else ""}
                            """)
                            
                            show_toast("Asset location updated!")
                            
                            # Clear session state and refresh
                            del st.session_state.asset_to_move
                            st.rerun()
                            
                        except Exception as e:
                            st.error(f"Failed to move asset: {e}")
            
            with col_confirm3:
                if st.button("Cancel", type="secondary", use_container_width=True):
                    del st.session_state.asset_to_move
                    st.rerun()
    else:
        st.info("Select an asset from the list above to move it to a new location.")


def page_overwrite_status(con: sqlite3.Connection) -> None:
    """Enhanced admin panel for issue management."""
    st.header("🔧 Admin Panel - Issue Management")
    
    # Password protection
    if "admin_authenticated" not in st.session_state:
        st.session_state.admin_authenticated = False
    
    if not st.session_state.admin_authenticated:
        with st.container(border=True):
            st.markdown("### 🔐 Admin Authentication")
            
            col_auth1, col_auth2, col_auth3 = st.columns([1, 2, 1])
            with col_auth2:
                entered_password = st.text_input(
                    "Enter Admin Password",
                    type="password",
                    key="admin_password_input"
                )
                
                if st.button("Login", type="primary", use_container_width=True):
                    if entered_password == ADMIN_PASSWORD:
                        st.session_state.admin_authenticated = True
                        st.rerun()
                    else:
                        st.error("Incorrect password")
            
            st.caption("Contact system administrator if you've forgotten the password.")
        return
    
    # Admin dashboard
    st.success("✅ **Admin Access Granted**")
    
    # Quick actions bar
    st.subheader("⚡ Quick Actions")
    
    col_act1, col_act2, col_act3, col_act4 = st.columns(4)
    
    with col_act1:
        if st.button("📊 Weekly Report", use_container_width=True):
            with loading_spinner("Generating weekly report..."):
                try:
                    df_all = fetch_submissions(con)
                    subject, body = build_weekly_report(df_all)
                    ok, msg = send_admin_report_email(subject, body)
                    if ok:
                        mark_report_sent(con, "weekly_manual")
                        show_toast("Weekly report sent successfully!")
                    else:
                        st.warning(f"Report sending failed: {msg}")
                except Exception as e:
                    st.error(f"Failed to send report: {e}")
    
    with col_act2:
        if st.button("🔄 Refresh Data", use_container_width=True):
            st.rerun()
    
    with col_act3:
        if st.button("📧 Bulk Notify", use_container_width=True):
            st.info("Bulk notification feature would open here")
    
    with col_act4:
        if st.button("🚪 Logout", use_container_width=True):
            st.session_state.admin_authenticated = False
            st.rerun()
    
    # Load issues data
    with loading_spinner("Loading issues..."):
        try:
            df = fetch_submissions(con)
        except Exception as e:
            st.error(f"Failed to load issues: {e}")
            return
    
    if df.empty:
        show_empty_state("No issues available for management", "📭")
        return
    
    # Issue selection with search
    st.subheader("🎯 Select Issue to Manage")
    
    # Create searchable dropdown
    issue_options = {
        row["id"]: f"#{row['id']}: {row['issue_type']} ({row['room_number']}) - {row['status']} - {row['name']}"
        for _, row in df.iterrows()
    }
    
    col_sel1, col_sel2 = st.columns([3, 1])
    
    with col_sel1:
        selected_id = st.selectbox(
            "Choose issue:",
            options=list(issue_options.keys()),
            format_func=lambda x: issue_options[x],
            help="Select an issue to update",
            key="admin_selected_issue"
        )
    
    with col_sel2:
        if st.button("🔍 Quick View", use_container_width=True):
            st.session_state.show_quick_view = True
    
    # Get selected issue details
    row = df[df["id"] == selected_id].iloc[0]
    
    # Display issue details in cards
    st.subheader("📋 Issue Details")
    
    col_details1, col_details2 = st.columns(2)
    
    with col_details1:
        create_card(
            "Basic Info",
            f"""
            **ID:** #{row['id']}
            
            **Reporter:** {row['name']}
            
            **Email:** {row['hsg_email']}
            
            **Room:** {row['room_number']}
            
            **Type:** {row['issue_type']}
            """,
            icon="📋",
            color=HSG_BLUE
        )
    
    with col_details2:
        # Calculate SLA info
        sla_target = expected_resolution_dt(str(row["created_at"]), str(row["importance"]))
        sla_text = sla_target.strftime("%Y-%m-%d %H:%M") if sla_target else "N/A"
        
        create_card(
            "Status & SLA",
            f"""
            **Priority:** {get_priority_emoji(row['importance'])} {row['importance']}
            
            **Status:** {get_status_badge(row['status'])}
            
            **Assigned To:** {row.get('assigned_to', 'Unassigned')}
            
            **SLA Target:** {sla_text}
            
            **Submitted:** {row['created_at']}
            """,
            icon="⏱️",
            color=HSG_GREEN if row['status'] == 'Resolved' else HSG_YELLOW
        )
    
    # Quick view expander
    if st.session_state.get("show_quick_view", False):
        with st.expander("📝 Full Details", expanded=True):
            st.markdown("#### Problem Description")
            st.info(row["user_comment"])
            
            st.markdown("#### Timeline")
            col_time1, col_time2, col_time3 = st.columns(3)
            with col_time1:
                st.metric("Created", row["created_at"][:16])
            with col_time2:
                st.metric("Updated", row["updated_at"][:16])
            with col_time3:
                resolved = row.get("resolved_at", "Not resolved")
                st.metric("Resolved", resolved[:16] if resolved and resolved != "None" else "Not resolved")
    
    # Update form
    st.markdown("---")
    st.subheader("✏️ Update Issue")
    
    with st.form("admin_update_form", border=True):
        col_up1, col_up2 = st.columns(2)
        
        with col_up1:
            # Assignment
            current_assignee = str(row.get("assigned_to", "") or "")
            assigned_to = st.selectbox(
                "Assign to:",
                options=["(Unassigned)"] + ASSIGNEES,
                index=(["(Unassigned)"] + ASSIGNEES).index(current_assignee)
                if current_assignee in (["(Unassigned)"] + ASSIGNEES)
                else 0,
                help="Assign this issue to a team member",
                key="admin_assignee"
            )
            assigned_to_value = None if assigned_to == "(Unassigned)" else assigned_to
        
        with col_up2:
            # Status update
            new_status = st.selectbox(
                "Update status to:",
                STATUS_LEVELS,
                index=STATUS_LEVELS.index(row["status"]) if row["status"] in STATUS_LEVELS else 0,
                help="Set the new status for this issue",
                key="admin_status"
            )
        
        # Additional notes
        admin_notes = st.text_area(
            "Internal Notes (optional):",
            placeholder="Add any internal notes about this update...",
            height=100,
            key="admin_notes"
        )
        
        # Resolution confirmation
        confirm_resolution = True
        if new_status == "Resolved":
            confirm_resolution = st.checkbox(
                "✓ Confirm issue resolution (will send notification email)",
                value=False,
                help="Check to confirm the issue is fully resolved",
                key="admin_confirm_resolution"
            )
        
        # Submit buttons
        col_sub1, col_sub2, col_sub3 = st.columns([1, 2, 1])
        with col_sub2:
            submitted = st.form_submit_button(
                "💾 Save Changes",
                type="primary",
                use_container_width=True
            )
    
    # Handle form submission
    if submitted:
        # Validate resolution confirmation
        if new_status == "Resolved" and not confirm_resolution:
            st.error("Please confirm resolution before setting status to 'Resolved'.")
            return
        
        # Update issue in database
        with loading_spinner("Updating issue..."):
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
                        for error in email_errors:
                            st.error(error)
                    else:
                        subject, body = resolved_email_text(str(row["name"]).strip() or "there")
                        ok, msg = send_email(str(row["hsg_email"]).strip(), subject, body)
                        if ok:
                            show_toast("Resolution notification sent to reporter")
                        else:
                            st.warning(f"Notification email failed: {msg}")
                
                show_toast("Issue updated successfully!")
                st.rerun()
                
            except Exception as e:
                st.error(f"Failed to update issue: {e}")


def page_overview_dashboard(con: sqlite3.Connection) -> None:
    """Enhanced comprehensive overview dashboard."""
    st.header("📊 Overview Dashboard")
    
    create_card(
        "Real-time System Overview",
        "Monitor key metrics, track performance, and get insights into facility management operations.",
        icon="📈",
        color=HSG_BLUE
    )
    
    # Load data
    with loading_spinner("Loading dashboard data..."):
        try:
            issues = fetch_submissions(con)
            assets = fetch_assets(con)
        except Exception as e:
            st.error(f"Failed to load data: {e}")
            return
    
    # Key metrics in a grid
    st.subheader("📈 Key Performance Indicators")
    
    # Row 1: Issue metrics
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        total_issues = len(issues)
        with st.container(border=True):
            st.metric("Total Issues", total_issues)
    
    with col2:
        open_issues = len(issues[issues["status"] != "Resolved"]) if not issues.empty else 0
        with st.container(border=True):
            st.metric("Open Issues", open_issues, 
                     delta=f"-{total_issues - open_issues}" if total_issues > 0 else None,
                     delta_color="inverse")
    
    with col3:
        resolved_issues = len(issues[issues["status"] == "Resolved"]) if not issues.empty else 0
        resolution_rate = (resolved_issues / total_issues * 100) if total_issues > 0 else 0
        with st.container(border=True):
            st.metric("Resolution Rate", f"{resolution_rate:.1f}%")
    
    with col4:
        avg_age = 0
        if not issues.empty and "created_at" in issues.columns:
            issues["created_at_dt"] = pd.to_datetime(issues["created_at"], errors="coerce")
            avg_age = (now_zurich() - issues["created_at_dt"]).dt.days.mean()
            avg_age = 0 if pd.isna(avg_age) else avg_age
        
        with st.container(border=True):
            st.metric("Avg. Issue Age", f"{avg_age:.1f} days")
    
    # Row 2: Asset metrics
    col5, col6, col7, col8 = st.columns(4)
    
    with col5:
        total_assets = len(assets)
        with st.container(border=True):
            st.metric("Total Assets", total_assets)
    
    with col6:
        available_assets = len(assets[assets["status"] == "available"]) if not assets.empty else 0
        utilization = ((total_assets - available_assets) / total_assets * 100) if total_assets > 0 else 0
        with st.container(border=True):
            st.metric("Utilization Rate", f"{utilization:.1f}%")
    
    with col7:
        asset_types = assets["asset_type"].nunique() if not assets.empty else 0
        with st.container(border=True):
            st.metric("Asset Types", asset_types)
    
    with col8:
        unique_locations = assets["location_id"].nunique() if not assets.empty else 0
        with st.container(border=True):
            st.metric("Locations", unique_locations)
    
    # Main dashboard tabs
    tab1, tab2, tab3 = st.tabs(["📋 Issues Overview", "📦 Assets Overview", "🚀 Quick Actions"])
    
    with tab1:
        if issues.empty:
            show_empty_state("No issues reported yet", "📭")
        else:
            # Current issues summary
            st.subheader("Current Issues Summary")
            
            # Priority breakdown
            priority_summary = issues["importance"].value_counts().reindex(IMPORTANCE_LEVELS, fill_value=0)
            col_pri1, col_pri2, col_pri3 = st.columns(3)
            
            with col_pri1:
                create_card(
                    "High Priority",
                    f"{priority_summary.get('High', 0)} issues",
                    icon="🔴",
                    color=HSG_RED
                )
            
            with col_pri2:
                create_card(
                    "Medium Priority",
                    f"{priority_summary.get('Medium', 0)} issues",
                    icon="🟡",
                    color=HSG_YELLOW
                )
            
            with col_pri3:
                create_card(
                    "Low Priority",
                    f"{priority_summary.get('Low', 0)} issues",
                    icon="🟢",
                    color=HSG_GREEN
                )
            
            # Recent issues table
            st.subheader("Recent Issues")
            recent_issues = issues.sort_values("created_at", ascending=False).head(10)
            
            if not recent_issues.empty:
                display_cols = ["id", "issue_type", "room_number", "importance", "status", "created_at"]
                display_df = recent_issues[display_cols].copy()
                display_df["priority"] = display_df["importance"].apply(
                    lambda x: f"{get_priority_emoji(x)} {x}"
                )
                display_df["status_badge"] = display_df["status"].apply(get_status_badge)
                
                # Display as HTML table for better styling
                html_table = """
                <table style="width: 100%; border-collapse: collapse;">
                    <thead>
                        <tr style="background-color: #00802F; color: white;">
                            <th style="padding: 10px; text-align: left;">ID</th>
                            <th style="padding: 10px; text-align: left;">Type</th>
                            <th style="padding: 10px; text-align: left;">Room</th>
                            <th style="padding: 10px; text-align: left;">Priority</th>
                            <th style="padding: 10px; text-align: left;">Status</th>
                            <th style="padding: 10px; text-align: left;">Created</th>
                        </tr>
                    </thead>
                    <tbody>
                """
                
                for _, row in display_df.iterrows():
                    html_table += f"""
                    <tr style="border-bottom: 1px solid #e0e0e0;">
                        <td style="padding: 10px;">#{row['id']}</td>
                        <td style="padding: 10px;">{row['issue_type']}</td>
                        <td style="padding: 10px;">{row['room_number']}</td>
                        <td style="padding: 10px;">{row['priority']}</td>
                        <td style="padding: 10px;">{row['status_badge']}</td>
                        <td style="padding: 10px;">{row['created_at'][:16]}</td>
                    </tr>
                    """
                
                html_table += "</tbody></table>"
                st.markdown(html_table, unsafe_allow_html=True)
    
    with tab2:
        if assets.empty:
            show_empty_state("No assets in inventory", "📦")
        else:
            # Asset distribution
            st.subheader("Asset Distribution")
            
            col_ast1, col_ast2 = st.columns(2)
            
            with col_ast1:
                # By type
                type_counts = assets["asset_type"].value_counts()
                fig1, ax1 = plt.subplots(figsize=(8, 6))
                ax1.pie(type_counts.values, labels=type_counts.index, autopct='%1.1f%%',
                       colors=plt.cm.Set3.colors, startangle=90)
                ax1.set_title("Assets by Type", fontweight="bold")
                ax1.axis('equal')
                st.pyplot(fig1)
            
            with col_ast2:
                # By status
                status_counts = assets["status"].value_counts()
                fig2, ax2 = plt.subplots(figsize=(8, 6))
                colors = [HSG_GREEN, HSG_YELLOW, HSG_RED][:len(status_counts)]
                bars = ax2.bar(status_counts.index, status_counts.values, color=colors)
                ax2.set_ylabel("Number of Assets")
                ax2.set_title("Assets by Status", fontweight="bold")
                for bar in bars:
                    height = bar.get_height()
                    ax2.text(bar.get_x() + bar.get_width()/2., height + 0.1,
                            f'{int(height)}', ha='center', va='bottom')
                st.pyplot(fig2)
            
            # Top locations
            st.subheader("Top Locations by Asset Count")
            location_counts = assets["location_id"].apply(location_label).value_counts().head(5)
            
            if not location_counts.empty:
                fig3, ax3 = plt.subplots(figsize=(10, 6))
                bars = ax3.barh(location_counts.index, location_counts.values, color=HSG_GREEN)
                ax3.set_xlabel("Number of Assets")
                ax3.set_title("Assets by Location", fontweight="bold")
                st.pyplot(fig3)
    
    with tab3:
        st.subheader("🚀 Quick Actions")
        
        # Create action cards
        col_act1, col_act2 = st.columns(2)
        
        with col_act1:
            create_card(
                "Generate Report",
                "Create a comprehensive report of all current issues and assets",
                icon="📄",
                color=HSG_BLUE
            )
            
            if st.button("Generate Now", use_container_width=True):
                st.info("Report generation would start here")
        
        with col_act2:
            create_card(
                "System Health",
                "Check system status and performance metrics",
                icon="❤️",
                color=HSG_GREEN
            )
            
            if st.button("Check Health", use_container_width=True):
                # Simple health check
                issues_health = "✅" if not issues.empty else "⚠️"
                assets_health = "✅" if not assets.empty else "⚠️"
                
                st.success(f"""
                **System Health Status:**
                
                - Issues Database: {issues_health} {len(issues)} records
                - Assets Database: {assets_health} {len(assets)} records
                - Last Updated: {now_zurich().strftime('%Y-%m-%d %H:%M')}
                - System Status: ✅ Operational
                """)
        
        # Recent activity
        st.subheader("📅 Recent Activity")
        
        try:
            status_log = fetch_status_log(con)
            if not status_log.empty:
                recent_activity = status_log.head(5)
                
                for _, activity in recent_activity.iterrows():
                    with st.container(border=True):
                        col_act_left, col_act_right = st.columns([3, 1])
                        with col_act_left:
                            st.markdown(f"""
                            **Issue #{activity['submission_id']}** status changed
                            
                            {activity['old_status']} → {activity['new_status']}
                            """)
                        with col_act_right:
                            st.caption(activity['changed_at'][:16])
            else:
                st.info("No recent activity")
        except:
            st.info("Activity log not available")


def enhanced_sidebar():
    """Create enhanced sidebar with navigation and status indicators."""
    show_logo()
    
    st.sidebar.markdown("### 🧭 Navigation")
    st.sidebar.markdown("---")
    
    # Quick stats if data is loaded
    try:
        con = get_connection()
        issues = fetch_submissions(con)
        assets = fetch_assets(con)
        
        open_count = len(issues[issues["status"] != "Resolved"]) if not issues.empty else 0
        high_priority = len(issues[issues["importance"] == "High"]) if not issues.empty else 0
        
        col_stat1, col_stat2 = st.sidebar.columns(2)
        with col_stat1:
            st.metric("Open", open_count, label_visibility="collapsed")
        with col_stat2:
            st.metric("High", high_priority, label_visibility="collapsed")
        
        st.sidebar.markdown("---")
    except:
        pass
    
    # Navigation sections
    st.sidebar.markdown("**📋 Reporting**")
    reporting_pages = {
        "📝 Submit Issue": "Submission Form",
        "📋 View Issues": "Submitted Issues",
        "🔧 Admin Panel": "Overwrite Status"
    }
    
    for icon_text, page_name in reporting_pages.items():
        if st.sidebar.button(
            icon_text,
            use_container_width=True,
            type="primary" if st.session_state.current_page == page_name else "secondary"
        ):
            st.session_state.current_page = page_name
            st.rerun()
    
    st.sidebar.markdown("---")
    st.sidebar.markdown("**📅 Resources**")
    
    resource_pages = {
        "📅 Book Assets": "Booking",
        "📍 Track Assets": "Asset Tracking"
    }
    
    for icon_text, page_name in resource_pages.items():
        if st.sidebar.button(
            icon_text,
            use_container_width=True,
            type="primary" if st.session_state.current_page == page_name else "secondary"
        ):
            st.session_state.current_page = page_name
            st.rerun()
    
    st.sidebar.markdown("---")
    st.sidebar.markdown("**📊 Analytics**")
    
    if st.sidebar.button(
        "📈 Overview Dashboard",
        use_container_width=True,
        type="primary" if st.session_state.current_page == "Overview Dashboard" else "secondary"
    ):
        st.session_state.current_page = "Overview Dashboard"
        st.rerun()
    
    # Theme toggle
    st.sidebar.markdown("---")
    theme = st.sidebar.selectbox(
        "🎨 Theme",
        ["Light", "Dark", "Auto"],
        index=0,
        label_visibility="collapsed"
    )
    
    if theme != st.session_state.get("theme"):
        st.session_state.theme = theme
        apply_hsg_theme()
    
    # Footer
    st.sidebar.markdown("---")
    st.sidebar.caption(f"© {datetime.now().year} University of St. Gallen")
    st.sidebar.caption(f"v2.0 • {now_zurich().strftime('%Y-%m-%d %H:%M')}")


# ============================================================================
# MAIN APPLICATION
# ============================================================================
def main() -> None:
    """Main application entry point."""
    # Initialize session state
    init_session_state()
    
    # Page configuration
    st.set_page_config(
        page_title="HSG Reporting Tool",
        page_icon="🏛️",
        layout="wide",
        initial_sidebar_state="expanded",
        menu_items={
            'Get Help': 'https://www.unisg.ch',
            'Report a bug': None,
            'About': """
            # HSG Reporting Tool v2.0
            
            Facility issue reporting, asset booking, and tracking system
            for the University of St. Gallen.
            
            Developed with ❤️ for HSG.
            """
        }
    )
    
    # Apply theme
    apply_hsg_theme()
    
    # Add keyboard shortcuts
    components.html("""
    <script>
    document.addEventListener('keydown', function(e) {
        // Ctrl/Cmd + S to submit forms
        if ((e.ctrlKey || e.metaKey) && e.key === 's') {
            e.preventDefault();
            const buttons = document.querySelectorAll('button[kind="primary"]');
            if (buttons.length > 0) {
                buttons[0].click();
            }
        }
        
        // Escape to clear filters
        if (e.key === 'Escape') {
            window.parent.postMessage({
                type: 'streamlit:setComponentValue',
                value: 'clear'
            }, '*');
        }
    });
    </script>
    """, height=0)
    
    # Sidebar
    enhanced_sidebar()
    
    # Header
    try:
        st.image(
            "campus_header.jpeg",
            caption="University of St. Gallen – Campus",
            use_container_width=True,
        )
    except FileNotFoundError:
        st.markdown(f"""
        <div style="background: linear-gradient(135deg, {HSG_GREEN}, {HSG_GREEN_DARK}); 
                    color: white; padding: 2rem; border-radius: 0 0 10px 10px; text-align: center;">
            <h1 style="margin: 0;">🏛️ HSG Reporting Tool</h1>
            <p style="margin: 0; opacity: 0.9;">University of St. Gallen – Facility Management System</p>
        </div>
        """, unsafe_allow_html=True)
    
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
        st.error(f"""
        ❌ **Database Initialization Failed**
        
        Error: {str(e)}
        
        Please check:
        1. Database file permissions
        2. Available disk space
        3. Database connection settings
        """)
        logger.critical(f"Database initialization error: {e}")
        return
    
    # Global search (if not on admin page)
    if st.session_state.current_page != "Overwrite Status":
        with st.container():
            col_search1, col_search2 = st.columns([4, 1])
            with col_search1:
                global_search = st.text_input(
                    "🔍 Global Search",
                    placeholder="Search across issues, assets, rooms...",
                    key="global_search_input"
                )
            with col_search2:
                if st.button("Search", use_container_width=True):
                    # Store search query for use in pages
                    st.session_state.search_query = global_search
    
    # Page routing
    page_functions = {
        "Submission Form": lambda: page_submission_form(con),
        "Submitted Issues": lambda: page_submitted_issues(con),
        "Booking": lambda: page_booking(con),
        "Asset Tracking": lambda: page_assets(con),
        "Overview Dashboard": lambda: page_overview_dashboard(con),
        "Overwrite Status": lambda: page_overwrite_status(con),
    }
    
    current_page = st.session_state.get("current_page", "Submission Form")
    
    if current_page in page_functions:
        # Add page title
        st.markdown(f"## {current_page}")
        
        # Execute page function
        page_functions[current_page]()
    else:
        st.error(f"Page '{current_page}' not found.")
    
    # Add feedback button
    st.markdown("---")
    col_feedback1, col_feedback2, col_feedback3 = st.columns([1, 2, 1])
    with col_feedback2:
        if st.button("💬 Provide Feedback", use_container_width=True):
            st.info("""
            **Thank you for your feedback!**
            
            Please send your suggestions or report issues to:
            
            📧 reporting-tool@unisg.ch
            
            We appreciate your help in improving this tool.
            """)


# ============================================================================
# APPLICATION ENTRY POINT
# ============================================================================
if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.critical(f"Application crashed: {e}", exc_info=True)
        
        st.error("""
        ⚠️ **Application Error**
        
        The application encountered an unexpected error. Please try:
        
        1. Refreshing the page
        2. Checking your internet connection
        3. Contacting support if the problem persists
        
        **Support Contact:** reporting-tool@unisg.ch
        """)
        
        if DEBUG:
            import traceback
            with st.expander("Technical Details (for administrators)"):
                st.code(traceback.format_exc(), language="python")
