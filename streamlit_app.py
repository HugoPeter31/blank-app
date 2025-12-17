# HSG Reporting Tool (Group Arthur Lavric & Fabio Patierno)
# This is our Streamlit application for the HSG Reporting Tool. 
# Our tool solves the problem of facility issues on the HSG campus. 
# You can just submit your issue through our Streamlit application and it gets stored in a database. 
# It is even possible for the facility management team to overwrite the status of the submitted issues. 

# Information just for Facility Management Team: 
# The Password for the third page "Overwrite Status" is PleaseOpen! (see line 336XXXXXX)

import re # Added for validation
import sqlite3 # Added for database
from datetime import datetime # Added for the timestamps
from email.message import EmailMessage
import smtplib # Added for sending emails

import pandas as pd # Added for tables
import pytz # Added for right time zone
import streamlit as st # Added for Streamlit
import matplotlib.pyplot as plt
import matplotlib.dates as mdates # Added for charts


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


# ----------------------------
# Secrets (Streamlit Cloud → Settings → Secrets)
# ----------------------------
def get_secret(key: str, default: str | None = None) -> str:
    """Helper: get secret or provide readable error."""
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
ADMIN_PASSWORD = get_secret("ADMIN_PASSWORD")  # for overwrite page


# ----------------------------
# Database
# ----------------------------
@st.cache_resource
def get_connection() -> sqlite3.Connection:
    # check_same_thread=False is important for Streamlit's reruns/sessions
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
            updated_at TEXT NOT NULL
        )
        """
    )
    con.commit()


def now_zurich_str() -> str:
    return datetime.now(APP_TZ).strftime("%Y-%m-%d %H:%M:%S")


# ----------------------------
# Validation: Check whether the specified email address complies with the requirements of an official HSG mail address
# ----------------------------
def valid_email(hsg_email: str) -> bool:
    # Accept: xxx@unisg.ch OR xxx@student.unisg.ch
    pattern = r"^[\w.]+@(student\.)?unisg\.ch$"
    return bool(re.match(pattern, hsg_email.strip()))

# Check whether the specified room number complies with the correct format required by HSG
def valid_room_number(room_number: str) -> bool:
    # Example format: "A 09-001"
    pattern = r"^[A-Z] \d{2}-\d{3}$"
    return bool(re.match(pattern, room_number.strip()))


# ----------------------------
# Email
# ----------------------------
def send_email(to_email: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = FROM_EMAIL
    msg["To"] = to_email
    msg.set_content(body)

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as smtp:
        smtp.starttls()
        smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
        smtp.send_message(msg)


def send_confirmation_email(recipient_email: str, recipient_name: str) -> None:
    subject = "Issue received!"
    body = f"""Dear {recipient_name},

Thank you for reaching out to us with your concerns. We confirm that we have received your issue report and are reviewing it.

We will keep you updated on our progress and notify you as soon as your issue has been resolved.

Best regards,
Your HSG Service Team
"""
    send_email(recipient_email, subject, body)


def send_resolved_email(recipient_email: str, recipient_name: str) -> None:
    subject = "Issue resolved!"
    body = f"""Hello {recipient_name},

Great news!
The issue you reported via the HSG Reporting Tool has been resolved.

If you have further questions or encounter new issues, please do not hesitate to reach out again.

Best regards,
Your HSG Service Team
"""
    send_email(recipient_email, subject, body)


# ----------------------------
# Pages
# ----------------------------
def page_submission_form(con: sqlite3.Connection) -> None:
    st.header("Submission Form")

    with st.form("issue_form", clear_on_submit=True):
        name = st.text_input("Name*").strip()
        hsg_email = st.text_input("HSG Email Address*").strip()

        uploaded_file = st.file_uploader("Upload a Photo (optional)", type=["jpg", "jpeg", "png"])
        if uploaded_file is not None:
            st.image(uploaded_file, caption="Uploaded Photo (not stored)", use_container_width=True)

        room_number = st.text_input("Room Number* (e.g., A 09-001)").strip()

        issue_type = st.selectbox("Issue Type*", ISSUE_TYPES)
        importance = st.selectbox("Importance*", IMPORTANCE_LEVELS)

        user_comment = st.text_area("Problem Description* (max 500 chars)", max_chars=500).strip()

        # MazeMap embed (Focus on the University of St.Gallen (Campus_ID 710)
        st.markdown("**Map** (optional)")
        maze_map_url = "https://use.mazemap.com/embed.html?v=1&zlevel=1&center=9.373611,47.429708&zoom=14.7&campusid=710"
        st.markdown(
            f"""
            <iframe src="{maze_map_url}"
                width="100%" height="420" frameborder="0"
                marginheight="0" marginwidth="0" scrolling="no"></iframe>
            """,
            unsafe_allow_html=True,
        )

        submitted = st.form_submit_button("Submit")

    if not submitted:
        return

    # Validation (on submit only)
    errors = []
    if not name:
        errors.append("Name is required.")
    if not hsg_email:
        errors.append("HSG Email Address is required.")
    elif not valid_email(hsg_email):
        errors.append("Invalid mail address. Please enter your official HSG email (…@unisg.ch or …@student.unisg.ch).")
    if not room_number:
        errors.append("Room Number is required.")
    elif not valid_room_number(room_number):
        errors.append("Invalid room number format. Please use: 'A 09-001'.")
    if not user_comment:
        errors.append("Problem Description is required.")

    if errors:
        for e in errors:
            st.error(e)
        return

    created_at = now_zurich_str()
    updated_at = created_at

    # Insert into DB
    with con:
        con.execute(
            """
            INSERT INTO submissions
            (name, hsg_email, issue_type, room_number, importance, status, user_comment, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'Pending', ?, ?, ?)
            """,
            (name, hsg_email, issue_type, room_number, importance, user_comment, created_at, updated_at),
        )

    # Send email (non-blocking would be nicer, but keep simple & reliable)
    try:
        send_confirmation_email(hsg_email, name)
        st.success("Submission successful! A confirmation email was sent.")
    except Exception as e:
        st.success("Submission successful!")
        st.warning(f"Could not send confirmation email: {e}")


def page_submitted_issues(con: sqlite3.Connection) -> None:
    st.header("Submitted Issues")

    df = pd.read_sql("SELECT * FROM submissions", con)
    st.subheader(f"Total Issues: {len(df)}")

    if df.empty:
        st.info("No submitted issues yet. Please submit an issue first.")
        return

    # Display table
    display_df = df.copy()
    display_df = display_df.rename(
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

    # Sort: Issue type then importance (High > Medium > Low)
    importance_order = {"High": 0, "Medium": 1, "Low": 2}
    display_df["_imp_rank"] = display_df["IMPORTANCE"].map(importance_order).fillna(99).astype(int)
    display_df = display_df.sort_values(by=["ISSUE TYPE", "_imp_rank", "SUBMITTED AT"], ascending=[True, True, False])
    display_df = display_df.drop(columns=["_imp_rank"])

    st.subheader("List of Submitted Issues")
    st.dataframe(display_df, use_container_width=True, hide_index=True)

    # Charts
    st.subheader("Number of Issues by Issue Type")
    issue_counts = df["issue_type"].value_counts().sort_index()
    fig, ax = plt.subplots()
    ax.bar(issue_counts.index, issue_counts.values)
    ax.set_xlabel("Issue Type")
    ax.set_ylabel("Number of Issues")
    plt.xticks(rotation=35, ha="right")
    st.pyplot(fig)

    st.subheader("Issues Submitted per Day")
    df_dates = df.copy()
    df_dates["created_at"] = pd.to_datetime(df_dates["created_at"])
    per_day = df_dates.groupby(df_dates["created_at"].dt.date).size()
    fig, ax = plt.subplots()
    ax.bar(per_day.index, per_day.values, width=0.7, align="center")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    plt.xticks(rotation=45, ha="right")
    ax.set_xlabel("Date")
    ax.set_ylabel("Number of Issues Submitted")
    ax.grid(axis="y", linestyle="--", alpha=0.5)
    st.pyplot(fig)

    st.subheader("Number of Issues by Importance Level")
    imp_counts = df["importance"].value_counts()
    fig, ax = plt.subplots()
    ax.bar(imp_counts.index, imp_counts.values)
    ax.set_xlabel("Importance Level")
    ax.set_ylabel("Number of Issues")
    st.pyplot(fig)

    st.subheader("Distribution of Statuses")
    status_counts = df["status"].value_counts()
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

    df = pd.read_sql("SELECT * FROM submissions", con)
    if df.empty:
        st.info("No submitted issues yet.")
        return

    # Choose issue by ID
    ids = df["id"].tolist()
    selected_id = st.selectbox("Select Issue ID to update:", ids)

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

    # Editable fields
    name_input = st.text_input("Name", value=str(row["name"]))
    email_input = st.text_input("HSG Email Address", value=str(row["hsg_email"]))
    new_status = st.selectbox("New Status", STATUS_LEVELS, index=STATUS_LEVELS.index(row["status"]) if row["status"] in STATUS_LEVELS else 0)

    confirm_resolve = True
    if new_status == "Resolved":
        confirm_resolve = st.checkbox("I confirm the issue is resolved (and an email will be sent).", value=False)

    if st.button("Update Status"):
        if not email_input.strip() or not valid_email(email_input):
            st.error("Please provide a valid HSG email address before updating.")
            return

        if new_status == "Resolved" and not confirm_resolve:
            st.error("Please confirm resolution before setting status to Resolved.")
            return

        updated_at = now_zurich_str()

        # Send resolved email if needed
        if new_status == "Resolved":
            try:
                send_resolved_email(email_input.strip(), name_input.strip() or "there")
                st.success("Resolved email sent.")
            except Exception as e:
                st.warning(f"Could not send resolved email: {e}")

        # Update DB
        with con:
            con.execute(
                """
                UPDATE submissions
                SET status = ?, updated_at = ?
                WHERE id = ?
                """,
                (new_status, updated_at, int(selected_id)),
            )

        st.success("Status updated successfully.")
        st.rerun()


# ----------------------------
# Main
# ----------------------------
def main() -> None:
    st.set_page_config(page_title="HSG Reporting Tool", layout="centered")

    # Logo
    try:
        st.image(LOGO_PATH, use_container_width=True)
    except Exception:
        st.info("Logo not found. Add 'HSG-logo-new.png' to the repository root.")

    con = get_connection()
    init_db(con)

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
