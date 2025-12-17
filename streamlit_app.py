# Group Arthur Lavric & Fabio Patierno

# This is our Streamlit application for the HSG Reporting Tool. 
# Our tool solves the problem of facility issues on the HSG campus. You can just submit your issue through our Streamlit application and it gets stored in a database.
# It is even possible for the facility management team to overwrite the status of the submitted issues. Therefore this application has a real use case!

# Our app is accessible via https://groupsixpointtwo.streamlit.app/

# Please make sure to submit an issue on the first page so a database can be created on your device. Otherwise the second page will raise an error for the charts.
# Additionally you need the HSG logo downloaded and named HSG-logo-new.png

# Information just for Facility Management Team:
# The Password for the third page "Overwrite Status" is PleaseOpen! (see line 336)

import pandas as pd # Added for tables
import sqlite3 # Added for database
import re # Added for validation
from datetime import datetime # Added for the timestamps
import pytz # Added for right time zone
import matplotlib.pyplot as plt # Added for charts
import matplotlib.dates as mdates # Added for charts
import smtplib  # Added for sending emails
import streamlit as st # Added for Streamlit


# Create a SQLite database connection
con = sqlite3.connect('hsg_reporting.db')
c = con.cursor()

# Implement correct time zone
st.time_zone = 'Europe/Zurich'

# Create a table to store submitted data with required information
c.execute('''
    CREATE TABLE IF NOT EXISTS submissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        hsg_email TEXT,
        issue_type TEXT,
        room_number TEXT,
        importance TEXT,
        submission_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        status TEXT DEFAULT 'Pending',
        user_comment TEXT 
    )
''')
con.commit()

# insert HSG logo on top of the page with spaces afterwards
image_path = "HSG-logo-new.png"
st.image(image_path, use_column_width=True)
st.write("")
st.write("")
st.write("")

# Check whether the specified email address complies with the requirements of an official HSG mail address
def valid_email(hsg_email):
    if hsg_email:
        hsg_email_pattern = r'^[\w.]+@(student\.)?unisg\.ch$'
        match = re.match(hsg_email_pattern, hsg_email)
        return bool(match)
    else:
        return True
    
# Check whether the specified room number complies with the correct format required by HSG
def valid_room_number(room_number):
    if room_number:
        room_number_pattern = r'^[A-Z] \d{2}-\d{3}$'
        match = re.match(room_number_pattern, room_number)
        return bool(match)
    else:
        return True
    
# Global variables for email configuration to ensure e-mail configuration functioning
smtp_server = st.secrets["SMTP_SERVER"]
smtp_port = int(st.secrets["SMTP_PORT"])
smtp_username = st.secrets["SMTP_USERNAME"]
smtp_password = st.secrets["SMTP_PASSWORD"]
from_email = st.secrets["FROM_EMAIL"]
correct_password = st.secrets["ADMIN_PASSWORD"]

# Set up our first page name:"Submission Form"
def submission_form():
    st.header("Submission Form")

    # Get user input for the submission form
    name = st.text_input("Name:")
    hsg_email = st.text_input("HSG Email Address:")

    # Function returns  an error when the mail address is invalid
    if not valid_email(hsg_email):
        st.error("Invalid mail address. Please check that you have entered your HSG mail address correctly.")

    # Implement the File uploader for photos
    uploaded_file = st.file_uploader("Upload a Photo:", type=["jpg", "jpeg", "png"])

    # Display the uploaded folder
    if uploaded_file is not None:
        st.image(uploaded_file, caption="Uploaded Photo", use_column_width=True)

    # Insert room number input
    room_number = st.text_input("Room Number:")

    # Returning an error when the room number pattern is invalid
    if not valid_room_number(room_number):
        st.error("Invalid room number format. Please enter a room number in the format 'A 09-001'.") 

    # Maze Map with a fixed focus on the University of St. Gallen (Campus_ID = 710)
    # We implemented Maze Map as an iframe
    maze_map_url = "https://use.mazemap.com/embed.html?v=1&zlevel=1&center=9.373611,47.429708&zoom=14.7&campusid=710"
    st.markdown(f"""
        <iframe src="{maze_map_url}"
            width="100%" height="420" frameborder="0" marginheight="0" marginwidth="0"
            scrolling="no"></iframe>
    """, unsafe_allow_html=True)

    # Implement the different Issue Types with checkboxes
    st.subheader("Issue Type:")
    lighting_issues = st.checkbox("Lighting issues")
    sanitary_problems = st.checkbox("Sanitary problems")
    havc_issues = st.checkbox("Heating, ventilation or air conditioning issues")
    cleaning_needs = st.checkbox("Cleaning needs due to heavy soiling")
    network_internet_problems = st.checkbox("Network/internet problems")
    it_equipment = st.checkbox("Issues with/lack of IT equipment")
    
    # Include the Importance selection with a dropdown menu
    importance = st.selectbox("Importance:", ['Low', 'Medium', 'High'])
    
    # Configuration of Comment box for problem description for detailed problem description
    user_comment = st.text_area("Problem Description:", max_chars=500)

    # When "Submit" button is clicked
    if st.button("Submit"):
        # Checking that at least one issue type is selected
        issue_type_selected = lighting_issues or sanitary_problems or havc_issues or cleaning_needs or network_internet_problems or it_equipment
        # Checking if all required fields are filled out, collecting information about issues
        all_fields_filled = all([name, hsg_email, room_number, issue_type_selected, user_comment])

        if all_fields_filled and valid_email(hsg_email):
            selected_issue_types = []
            if lighting_issues:
                selected_issue_types.append("Lighting issues")
            if sanitary_problems:
                selected_issue_types.append("Sanitary problems")
            if havc_issues:
                selected_issue_types.append("Heating, ventilation or air conditioning issues")
            if cleaning_needs:
                selected_issue_types.append("Cleaning needs due to heavy soiling")
            if network_internet_problems:
                selected_issue_types.append("Network/internet problems")
            if it_equipment:
                selected_issue_types.append("Issues with/lack of IT equipment")
            issue_types = ', '.join(selected_issue_types)

            # Implement 'Europe/Zurich' as the standard time zone for the application
            # We had to implement it again because first it was always CET
            desired_time_zone = pytz.timezone('Europe/Zurich')

            # Implement datetime.now() with the selected time zone (Zurich)
            submission_time = datetime.now(desired_time_zone).strftime("%Y-%m-%d %H:%M:%S")
            # Found this solution through Tutorials

            # Import data to the database
            c.execute('''
                INSERT INTO submissions (name, hsg_email, issue_type, room_number, importance, submission_time, user_comment)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (name, hsg_email, issue_types, room_number, importance, submission_time, user_comment))
            con.commit()

            # Send confirmation email to the submitter
            send_confirmation_email(hsg_email, name)
        
            st.success("Submission Successful!")
        else:
            # Raise an Error if not all fields are filled out
            st.error("Please fill in all required fields and select at least one issue type.")

# Function to send the confirmation email
def send_confirmation_email(recipient_email, recipient_name):
    subject = 'Issue received!'
    body = f'''Dear {recipient_name},

Thank you for reaching out to us with your concerns. We would like to confirm that we have received your issue report and are giving it our utmost attention. Our team is already in the process of reviewing the details you provided, and we are committed to resolving it as swiftly and efficiently as possible.

We will keep you updated on our progress and notify you as soon as your issue has been resolved. Should you have any further questions or require additional assistance in the meantime, please feel free to contact us. Your patience and understanding in this matter are greatly appreciated.

Best regards,
Your HSG Service Team'''
    
    message = f'Subject: {subject}\n\n{body}'

    try:
        with smtplib.SMTP(smtp_server, smtp_port) as smtp:
            smtp.starttls()
            smtp.login(smtp_username, smtp_password)
            smtp.sendmail(from_email, recipient_email, message)
        st.success("Confirmation Email Sent Successfully!")
    except Exception as e:
        st.error(f"An error occurred while sending the confirmation email: {str(e)}")
# This feature was partly implemented through help from Tutorials and ChatGPT but we had to do the right implementation and troubleshooting by ourselves

# Set up our second page with name: "Submitted Issues" to display the submitted issues
def submitted_issues():
    st.header("Submitted Issues")

    # Retrieve submitted data from the database
    submitted_data = pd.read_sql('SELECT * FROM submissions', con)

    # Count total issues
    total_issues = len(submitted_data)

    # Display the counter of total issues
    st.subheader(f"Total Issues: {total_issues}")
    
    # Sort the issues by issue type and importance
    submitted_data = submitted_data.sort_values(by=['issue_type', 'importance'], ascending=[True, False])

    # Change column names for optimal visualisation
    submitted_data = submitted_data.rename(columns={
        'name': 'NAME',
        'hsg_email': 'HSG MAIL ADDRESS',
        'room_number': 'ROOM NR.',
        'importance': 'IMPORTANCE',
        'submission_time': 'SUBMITTED AT',
        'status': 'STATUS',
        'user_comment': 'PROBLEM DESCRIPTION'
    })

    # Set the index to the issue type
    submitted_data = submitted_data.set_index('issue_type')

    # Drop the 'id' column
    submitted_data = submitted_data.drop(columns=['id'])

    # Display the list of submitted issues
    st.subheader("List of Submitted Issues:")
    st.table(submitted_data)

   # Create a bar chart for the number of issues per issue type
    st.subheader("Number of Issues categorized by Issue Type")
    issue_type_counts = submitted_data.index.value_counts()

    # Set the color to dark green
    color = 'darkgreen'

    fig, ax = plt.subplots()
    shortened_labels = [label[:20] for label in issue_type_counts.index]
    issue_type_counts.plot(kind='bar', ax=ax, color=color)
    ax.set_xticklabels(shortened_labels, rotation=45, ha='right')  # Rotate and align x-axis labels
    ax.set_xlabel("Issue Type")
    ax.set_ylabel("Number of Issues")
    st.pyplot(fig)

    # Added spaces
    st.write("")
    st.write("")

    # Create a time series plot for the number of issues submitted per day
    st.subheader("Issues Submitted per Day")
    submitted_data['SUBMITTED AT'] = pd.to_datetime(submitted_data['SUBMITTED AT'])
    submitted_data['Date'] = submitted_data['SUBMITTED AT'].dt.date
    issues_per_day = submitted_data.groupby('Date').size()

    # Set the color to dark green
    color = 'darkgreen'

    fig, ax = plt.subplots()
    ax.bar(issues_per_day.index, issues_per_day.values, width=0.7, align='center', color=color, alpha=1)  # Adjusted width and added alpha for transparency
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))

    # Adjust spacing between bars
    bar_width = 1  # Adjust the width based on your preference
    ax.xaxis.set_major_locator(mdates.DayLocator(interval=1))  # Set the interval to control spacing
    ax.xaxis.set_minor_locator(mdates.DayLocator())

    # Rotate the date labels for better readability
    plt.xticks(rotation=45, ha='right')

    ax.set_xlabel("Date")
    ax.set_ylabel("Number of Issues Submitted")
    ax.set_title("Issues Submitted per Day")

    # Add grid lines for better visualization
    ax.grid(axis='y', linestyle='--', alpha=1)

    # Add a background color for better contrast
    ax.set_facecolor('#f0f0f0')  # Light gray background color

    st.pyplot(fig)

    # Added spaces
    st.write("")
    st.write("")

    # Create a bar chart for the number of issues per importance level
    st.subheader("Count of Issues classified according to their Level of Importance")
    importance_counts = submitted_data['IMPORTANCE'].value_counts()

    # Set the color to dark green
    color = 'darkgreen'

    fig, ax = plt.subplots()
    importance_counts.plot(kind='bar', ax=ax, color=color, alpha=1)
    ax.set_xlabel("Importance Level")
    ax.set_ylabel("Number of Issues")
    ax.set_title("Number of Issues by Importance Level")
    st.pyplot(fig)

    # Added spaces
    st.write("")
    st.write("")

    # Create a pie chart for the distribution of statuses
    st.subheader("Distribution of Statuses")
    status_counts = submitted_data['STATUS'].value_counts()

    # Set the colors to dark green and a few shades
    colors = ['darkgreen', 'forestgreen', 'limegreen', 'mediumseagreen']

    fig, ax = plt.subplots()
    ax.pie(status_counts, labels=status_counts.index, autopct='%1.1f%%', startangle=90, colors=colors, wedgeprops=dict(width=0.3))
    ax.axis('equal')  # Equal aspect ratio ensures the pie chart is circular.
    ax.set_title("Distribution of Statuses")

    # Add a circle in the middle for a donut chart look
    centre_circle = plt.Circle((0,0),0.10,fc='white')
    ax.add_artist(centre_circle)

    # Add a legend for better readability
    ax.legend(status_counts.index, loc='lower right')

    st.pyplot(fig)

# Set a password for accessing safely the "Overwrite Status" page
correct_password = "PleaseOpen!"

# Set up our third page with name: "Overwrite Status"
def overwrite_status():
    global smtp_server, smtp_port, smtp_username, smtp_password, from_email

    st.header("Overwrite Status")

    # Implement Password protection for the "Overwrite Status" page to restrict access
    entered_password = st.sidebar.text_input("Enter Password", "", type="password")

    if entered_password != correct_password:
        st.warning("You haven't entered a password / the password is incorrect. Please enter the correct password to access this page.")
        return

    # Continue with the rest of the function if the correct password is entered
    # Retrieve submitted data from the database
    submitted_data = pd.read_sql('SELECT * FROM submissions', con)

    # Check if the DataFrame is empty
    if submitted_data.empty:
        st.subheader("No submitted issues yet.")
        return

    # Display a selection box to choose the issue to update
    selected_issue_id = st.selectbox("Select Issue ID to Overwrite Status:", submitted_data['id'].tolist())

    # Display the details of the selected issue
    selected_issue = submitted_data[submitted_data['id'] == selected_issue_id].iloc[0]
    st.subheader("Selected Issue Details:")
    st.write(selected_issue)

    # Display the Name and Email fields filled with values from the selected issue
    name_input = st.text_input("Name:", value=selected_issue['name'], key='name')
    hsg_email_input = st.text_input("HSG Email Address:", value=selected_issue['hsg_email'], key='hsg_email')

    # Display the status update form
    new_status_options = ['Pending', 'In Progress', 'Resolved']
    new_status = st.selectbox("Select New Status:", new_status_options)

    # When "Update Status" button is clicked
    if st.button("Update Status"):
        # Implement 'Europe/Zurich' as the standard time zone for the application
        # We had to implement it again because first it was always CET
        desired_time_zone = pytz.timezone('Europe/Zurich')

        # Implement datetime.now() with the selected time zone (Zurich)
        submission_time = datetime.now(desired_time_zone).strftime("%Y-%m-%d %H:%M:%S")
        # Found this solution through Tutorials

        # Ask for confirmation before updating status to "Resolved"
        if new_status == 'Resolved':
            
            # Use the HSG email input box value for the recipient
            submitter_email = hsg_email_input

            # Compose and send the email
            subject = 'Issue Resolved!'
            body = f'''Hello {name_input},

Great news!
The issue you brought to our attention via the HSG Reporting Tool has been effectively resolved. We sincerely appreciate your patience and understanding throughout this process.
Should you have any further questions, need additional assistance, or encounter any other issues, please do not hesitate to reach out to us.

Thank you for using our HSG Reporting Tool. We are committed to continually providing you with exceptional service.

Best regards,
Your HSG Service Team'''
            
            message = f'Subject: {subject}\n\n{body}'

            try:
                with smtplib.SMTP(smtp_server, smtp_port) as smtp:
                    smtp.starttls()
                    smtp.login(smtp_username, smtp_password)
                    smtp.sendmail(from_email, submitter_email, message)
                st.success("Email Sent Successfully!")
            except Exception as e:
                st.error(f"An error occurred while sending the email: {str(e)}")

        # Update the status and timestamp in the database
        c.execute('''
            UPDATE submissions
            SET status = ?, submission_time = ?
            WHERE id = ?
        ''', (new_status, submission_time, selected_issue_id))
        con.commit()
        st.success("Status Updated Successfully!")
# This feature was partly implemented through help from Tutorials and ChatGPT but we had to do the right implementation and troubleshooting by ourselves
# For example this email wasn't sent at first, there was always something wrong with getting the information (hsg_email) from the dataframe but never a real error occured just the email wasn't sent
# Then we went to the coaching but at the end the problem wasn't solved at all. So we had to spend much time thinking of a new way to do it. We came up with the idea to insert a textbox, which gets the name and email out of the dataframe
# Finally the email sending feature now gets its information from these textboxes and so it works :)
        
# Defining the main() function at the end allows for a more organized structure where all the functions and code specific to the script's functionality are defined first, and the main execution logic is separated at the end. It also makes it easier to read and understand the flow of the script.
# This pattern is used to enhance code readability, maintainability, and to ensure that the code inside the main() function is executed only when the script is run directly and not when it's imported as a module elsewhere.
def main():
    st.title("HSG Reporting Tool")

    # Display the pages on the sidebar
    page = st.sidebar.radio("Select Page:", ['Submission Form', 'Submitted Issues', 'Overwrite Status'])

    if page == 'Submission Form':
        submission_form()
    elif page == 'Submitted Issues':
        submitted_issues()
    elif page == 'Overwrite Status':
        overwrite_status()

if __name__ == "__main__":
    main()
