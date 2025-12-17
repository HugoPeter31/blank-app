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

    # Added: Audit log table for status changes (who/when/what changed).
    # Why: Status updates should be traceable (professional admin workflow).
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

    # Ensure audit-log table exists for older DBs as well.
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

    con.commit()


def fetch_submissions(con: sqlite3.Connection) -> pd.DataFrame:
    """Fetch all submissions as a DataFrame (single responsibility)."""
    return pd.read_sql("SELECT * FROM submissions", con)


def fetch_status_log(con: sqlite3.Connection) -> pd.DataFrame:
    """Fetch status-change audit logs as a DataFrame."""
    return pd.read_sql(
        """
        SELECT submission_id, old_status, new_status, changed_at
        FROM status_log
        ORDER BY changed_at DESC
        """,
        con,
    )


def insert_submission(con: sqlite3.Connection, sub: Submission) -> None:
    """Insert a validated submission into the database."""
    created_at = now_zurich_str()
    updated_at = created_at

    # Added: Normalize inputs before storing.
    # Why: Normalization avoids inconsistent duplicates (e.g., uppercase/lowercase differences).
    normalized_name = sub.name.strip()
    normalized_email = sub.hsg_email.strip().lower()
    normalized_room = sub.room_number.strip().upper()
    normalized_comment = sub.user_comment.strip()

    with con:
        con.execute(
            """
            INSERT INTO submissions
            (name, hsg_email, issue_type, room_number, importance, status, user_comment, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'Pending', ?, ?, ?)
            """,
            (
                normalized_name,
                normalized_email,
                sub.issue_type,
                normalized_room,
                sub.importance,
                normalized_comment,
                created_at,
                updated_at,
            ),
        )


def update_issue_status(con: sqlite3.Connection, issue_id: int, new_status: str) -> None:
    """Update status and timestamp in the database."""
    updated_at = now_zurich_str()
    with con:
        con.execute(
            """
            UPDATE submissions
            SET status = ?, updated_at = ?
            WHERE id = ?
            """,
            (new_status, updated_at, int(issue_id)),
        )


def log_status_change(con: sqlite3.Connection, submission_id: int, old_status: str, new_status: str) -> None:
    """
    Write an audit-log entry for a status change.

    Why: Admin changes should be traceable for accountability and debugging.
    """
    changed_at = now_zurich_str()
    with con:
        con.execute(
            """
            INSERT INTO status_log (submission_id, old_status, new_status, changed_at)
            VALUES (?, ?, ?, ?)
            """,
            (int(submission_id), old_status, new_status, changed_at),
        )


# ----------------------------
# Email
# ----------------------------
def send_email(to_email: str, subject: str, body: str) -> tuple[bool, str]:
    """
    Send an email and return (success, message).

    BCC:
    - A copy is silently sent to the admin inbox (ADMIN_INBOX)
    - The recipient does NOT see the admin email address
    """
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = FROM_EMAIL
    msg["To"] = to_email
    msg.set_content(body)

    # BCC is not added to the headers, only to the SMTP recipient list
    recipients = [to_email, ADMIN_INBOX]

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=10) as smtp:
            smtp.starttls()
            smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            smtp.send_message(msg, to_addrs=recipients)

        logger.info("Email sent | to=%s | bcc=%s | subject=%s", to_email, ADMIN_INBOX, subject)
        return True, "Email sent."

    except Exception as exc:
        logger.exception("Email sending failed")

        if DEBUG:
            return False, f"Email could not be sent: {exc}"
        return False, "Email could not be sent due to a technical issue."


def confirmation_email_text(recipient_name: str) -> tuple[str, str]:
    """Return (subject, body) for a confirmation email."""
    subject = "Issue received!"
    body = f"""Dear {recipient_name},

Thank you for contacting us regarding your concern. We hereby confirm that we have received your issue report and that it is currently under review by the responsible team.

We will keep you informed about the progress and notify you once the matter has been resolved. Should we require any additional information, we will contact you accordingly.

Thank you for your understanding and cooperation.

Kind regards,
HSG Service Team
"""
    return subject, body


def resolved_email_text(recipient_name: str) -> tuple[str, str]:
    """Return (subject, body) for a resolved notification email."""
    subject = "Issue resolved!"
    body = f"""Hello {recipient_name},

We are pleased to inform you that the issue you reported via the HSG Reporting Tool has been resolved.

If you have further questions or require assistance in the future, please do not hesitate to contact us.

Kind regards,
HSG Service Team
"""
    return subject, body


# ----------------------------
# UI helpers
# ----------------------------
def show_errors(errors: Iterable[str]) -> None:
    """Render validation errors consistently across pages."""
    for msg in errors:
        st.error(msg)


def show_logo() -> None:
    """Display the logo if present; otherwise show a helpful hint."""
    try:
        st.image(LOGO_PATH, use_container_width=True)
    except Exception:
        st.info("Logo not found. Add 'HSG-logo-new.png' to the repository root.")


def render_map_iframe() -> None:
    """Embed MazeMap (optional)."""
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

    with st.form("issue_form", clear_on_submit=True):
        name = st.text_input("Name*").strip()
        hsg_email = st.text_input("HSG Email Address*").strip()

        # Helpful hints reduce validation errors and improve the user experience.
        st.caption("Accepted emails: …@unisg.ch or …@student.unisg.ch")
        st.caption("Room example: A 09-001")

        uploaded_file = st.file_uploader(
            "Upload a Photo (optional)",
            type=["jpg", "jpeg", "png"],
        )
        if uploaded_file is not None:
            st.image(uploaded_file, caption="Uploaded Photo (not stored)", use_container_width=True)

        room_number = st.text_input("Room Number* (e.g., A 09-001)").strip()
        issue_type = st.selectbox("Issue Type*", ISSUE_TYPES)
        importance = st.selectbox("Importance*", IMPORTANCE_LEVELS)
        user_comment = st.text_area("Problem Description* (max 500 chars)", max_chars=500).strip()

        render_map_iframe()
        submitted = st.form_submit_button("Submit")

    if not submitted:
        return

    # Added: Normalize inputs early to ensure consistent validation + storage.
    # Why: Users often enter different casing; normalization prevents accidental mismatches.
    normalized_email = hsg_email.strip().lower()
    normalized_room = room_number.strip().upper()

    sub = Submission(
        name=name,
        hsg_email=normalized_email,
        issue_type=issue_type,
        room_number=normalized_room,
        importance=importance,
        user_comment=user_comment,
    )

    errors = validate_submission_input(sub)
    if errors:
        show_errors(errors)
        return

    insert_submission(con, sub)

    # Email is best-effort; submission should still succeed without email.
    subject, body = confirmation_email_text(sub.name.strip())
    ok, msg = send_email(sub.hsg_email, subject, body)
    if ok:
        st.success("Submission successful! A confirmation email was sent.")
    else:
        st.success("Submission successful!")
        st.warning(msg)


def page_submitted_issues(con: sqlite3.Connection) -> None:
    st.header("Submitted Issues")

    df = fetch_submissions(con)
    st.subheader(f"Total Issues: {len(df)}")

    if df.empty:
        st.info("No submitted issues yet. Please submit an issue first.")
        return

    # Added: Status filter
    # Why: Makes the table and charts more useful for users and admins.
    status_filter = st.multiselect(
        "Filter by status",
        options=STATUS_LEVELS,
        default=STATUS_LEVELS,
    )
    df = df[df["status"].isin(status_filter)]

    if df.empty:
        st.info("No issues match the selected status filter.")
        return

    display_df = build_display_table(df)
    st.subheader("List of Submitted Issues")
    st.dataframe(display_df, use_container_width=True, hide_index=True)

    # Added: CSV export
    # Why: Enables offline reporting and makes the tool practical for a real facility team workflow.
    csv_bytes = df.to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download CSV",
        data=csv_bytes,
        file_name="hsg_reporting_issues.csv",
        mime="text/csv",
    )

    render_charts(df)

    # Added: Optional audit-log view on this page (read-only).
    # Why: Demonstrates traceability and professional admin processes.
    with st.expander("Show status change audit log"):
        log_df = fetch_status_log(con)
        if log_df.empty:
            st.info("No status changes recorded yet.")
        else:
            st.dataframe(log_df, use_container_width=True, hide_index=True)


def build_display_table(df: pd.DataFrame) -> pd.DataFrame:
    """Prepare a clean, sorted table for the UI (separation of concerns)."""
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
        }
    )

    importance_order = {"High": 0, "Medium": 1, "Low": 2}
    display_df["_imp_rank"] = (
        display_df["IMPORTANCE"].map(importance_order).fillna(99).astype(int)
    )

    # Sort by issue type, then importance rank, then newest submissions first.
    display_df = display_df.sort_values(
        by=["ISSUE TYPE", "_imp_rank", "SUBMITTED AT"],
        ascending=[True, True, False],
    ).drop(columns=["_imp_rank"])

    return display_df


def render_charts(df: pd.DataFrame) -> None:
    """Render charts from the raw DB data."""
    st.subheader("Number of Issues by Issue Type")

    # Keep chart order stable and readable (no random ordering from value_counts()).
    issue_counts = df["issue_type"].value_counts().reindex(ISSUE_TYPES, fill_value=0)
    fig, ax = plt.subplots()
    ax.barh(issue_counts.index, issue_counts.values)
    ax.set_xlabel("Number of Issues")
    ax.set_ylabel("Issue Type")
    st.pyplot(fig)

    # Updated: Time-series chart (plot + fill missing days)
    # Why: A continuous date range makes trends visible and avoids misleading gaps.
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
    fig, ax = plt.subplots()
    ax.pie(status_counts.values, labels=status_counts.index, autopct="%1.1f%%", startangle=90)
    ax.axis("equal")
    st.pyplot(fig)


def page_overwrite_status(con: sqlite3.Connection) -> None:
    st.header("Overwrite Status")

    entered_password = st.sidebar.text_input("Enter Password", "", type="password")
    if entered_password != ADMIN_PASSWORD:
        st.warning("Enter the correct password to access this page.")
        return

    df = fetch_submissions(con)
    if df.empty:
        st.info("No submitted issues yet.")
        return

    # Added: Status filter for admin page
    # Why: Admins typically work on open issues; filtering improves usability and reduces errors.
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
            "Submitted At": row["created_at"],
            "Last Updated": row["updated_at"],
            "Problem Description": row["user_comment"],
        }
    )

    st.divider()

    # Keep admin scope minimal: update status only (prevents accidental data edits).
    new_status = st.selectbox(
        "New Status",
        STATUS_LEVELS,
        index=STATUS_LEVELS.index(row["status"]) if row["status"] in STATUS_LEVELS else 0,
    )

    confirm_resolve = True
    if new_status == "Resolved":
        confirm_resolve = st.checkbox(
            "I confirm the issue is resolved (and an email will be sent).",
            value=False,
        )

    if st.button("Update Status"):
        if new_status == "Resolved" and not confirm_resolve:
            st.error("Please confirm resolution before setting status to Resolved.")
            return

        old_status = str(row["status"])
        update_issue_status(con, int(selected_id), new_status)

        # Added: Audit log for status changes
        # Why: Provides traceability and accountability for admin actions.
        if new_status != old_status:
            log_status_change(con, int(selected_id), old_status, new_status)

        # Notify only when resolved (best effort).
        if new_status == "Resolved":
            email_errors = validate_admin_email(str(row["hsg_email"]))
            if email_errors:
                show_errors(email_errors)
            else:
                subject, body = resolved_email_text(str(row["name"]).strip() or "there")
                ok, msg = send_email(str(row["hsg_email"]).strip(), subject, body)
                (st.success(msg) if ok else st.warning(msg))

        st.success("Status updated successfully.")
        st.rerun()


# ----------------------------
# Main
# ----------------------------
def main() -> None:
    st.set_page_config(page_title="HSG Reporting Tool", layout="centered")
    show_logo()

    con = get_connection()
    init_db(con)
    migrate_db(con)  # keeps existing/older DB files compatible with this version of the app

    st.title("HSG Reporting Tool")
    page = st.sidebar.radio("Select Page:", ["Submission Form", "Submitted Issues", "Overwrite Status"])

    if page == "Submission Form":
        page_submission_form(con)
    elif page == "Submitted Issues":
        page_submitted_issues(con)
    else:
        page_overwrite_status(con)


if __name__ == "__main__":
    main()
