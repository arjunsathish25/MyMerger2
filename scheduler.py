import os
import json
import pandas as pd
import requests
import base64
from jinja2 import Template
from apscheduler.schedulers.background import BackgroundScheduler
from flask import current_app
from pathlib import Path

# Constants
INSTANCE_FOLDER = Path("instance")
TOKEN_FILE = INSTANCE_FOLDER / ".graph_token.json"
TEMPLATE_FILE_CA = INSTANCE_FOLDER / "email_template_ca.html"
TEMPLATE_FILE_US = INSTANCE_FOLDER / "email_template_us.html"
JOB_PROGRESS_FILE = INSTANCE_FOLDER / ".job_progress.json"
SETTINGS_FILE = INSTANCE_FOLDER / "settings.json"
REPLY_PROGRESS_FILE = INSTANCE_FOLDER / ".reply_progress.json"
TEMPLATE_FILE = INSTANCE_FOLDER / "email_template.html"
FOLLOW_UP_TEMPLATE_FILE = INSTANCE_FOLDER / "follow_up_template.html"
FOLLOW_UP_UPLOAD_FOLDER = Path("uploads") / "follow_ups"
UPLOAD_FOLDER = Path("uploads")

import msal # Keep msal for token refresh

def get_latest_file(path):
    if not path.exists() or not path.is_dir():
        return None
    files = [p for p in path.iterdir() if p.is_file()]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)

def log_email_attempt(email, name, company_name, subject, status, body, error_details=None, internet_message_id=None, conversation_id=None):
    """Logs an email attempt to the persistent EmailLog table."""
    from app import db, EmailLog
    # This function is now self-contained to ensure logs are always written
    # by committing immediately after adding the log to the session.
    try:
        log_entry = EmailLog(
            email=email,
            name=name,
            company_name=company_name,
            subject=subject,
            status=status,
            body=body,
            error_details=error_details,
            internet_message_id=internet_message_id,
            conversation_id=conversation_id
        )
        db.session.add(log_entry)
        # The commit will happen at the end of the job run.
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Failed to create persistent email log for {email}: {e}")


def get_scheduler_token():
    """
    Acquires a token for the scheduler, refreshing it if necessary using the
    stored refresh token.
    """
    if not TOKEN_FILE.exists():
        current_app.logger.error("Graph token file not found. Please log in via the web app first.")
        return None

    try:
        token_data = json.loads(TOKEN_FILE.read_text(encoding='utf-8'))
    except (IOError, json.JSONDecodeError) as e:
        current_app.logger.error(f"Could not read or parse token file: {e}")
        return None

    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        current_app.logger.error("Refresh token not found in token file. Please log in again to regenerate it.")
        return None

    # These must be read inside the function to ensure dotenv has been loaded by the app.
    client_id = os.getenv("MS_CLIENT_ID")
    client_secret = os.getenv("MS_CLIENT_SECRET")
    tenant_id = os.getenv("MS_TENANT_ID")
    authority = f"https://login.microsoftonline.com/{tenant_id}"
    scope = ["User.Read", "Mail.Send", "Mail.ReadWrite"] # Align with web login scopes

    if not all([client_id, client_secret, tenant_id]):
        current_app.logger.error("Scheduler failed: MS_CLIENT_ID or MS_CLIENT_SECRET environment variables not set.")
        return None

    msal_app = msal.ConfidentialClientApplication(
        client_id, authority=authority, client_credential=client_secret
    )

    result = msal_app.acquire_token_by_refresh_token(refresh_token, scopes=scope)

    if "access_token" in result:
        # Save the new token data back to the file to get a new refresh_token if it was rotated
        try:
            TOKEN_FILE.write_text(json.dumps(result), encoding='utf-8')
        except IOError as e:
            current_app.logger.warning(f"Could not update token file with refreshed token: {e}")
        return result["access_token"]
    else:
        current_app.logger.error(f"Failed to refresh token for scheduler: {result.get('error_description')}")
        return None

def send_email_job():
    from app import app, db, ContactStatus, EmailLog, scheduler, CAMPAIGN_COMPLETE_FLAG

    with app.app_context():
        from graph_utils import send_email_with_graph
        from datetime import datetime, timezone
        current_app.logger.info("--- Starting throttled email job (1 email per run) ---")

        # Get BCC setting
        bcc_recipients = None
        region = 'us'
        mcp_server_config = None # Placeholder for MCP server settings
        try:
            if SETTINGS_FILE.exists():
                settings = json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
                if settings.get("bcc_enabled"):
                    # Access the BCC email from the app config
                    bcc_recipients = [app.config['BCC_EMAIL']]
                    current_app.logger.info("BCC is enabled for this scheduler run.")
                
                # Get campaign region, default to 'us'
                region = settings.get('campaign_region', 'us')
                current_app.logger.info(f"Scheduler using region: '{region}'")

                # Read MCP server configuration
                if "mcp_servers" in settings and settings["mcp_servers"]:
                    # Here you could add logic to select a server. For now, we'll just log it.
                    mcp_server_config = settings["mcp_servers"]
                    current_app.logger.info(f"Loaded {len(mcp_server_config)} MCP server configurations.")
        except Exception as e:
            current_app.logger.error(f"Could not read settings for scheduler: {e}")

        # Get latest data file
        data_file = get_latest_file(UPLOAD_FOLDER)
        if not data_file:
            current_app.logger.warning("No data file found in 'uploads'. Skipping job.")
            return

        # Load job progress or initialize it
        progress = {"file_path": "", "next_row": 0}
        if JOB_PROGRESS_FILE.exists():
            try:
                progress = json.loads(JOB_PROGRESS_FILE.read_text(encoding='utf-8'))
            except (json.JSONDecodeError, IOError) as e:
                current_app.logger.error(f"Could not read job progress file, resetting. Error: {e}")

        # Reset progress if a new file is detected
        is_new_campaign = progress.get("file_path") != str(data_file)
        if is_new_campaign:
            current_app.logger.info(f"New data file '{data_file.name}' detected. Starting from beginning.")
            progress = {"file_path": str(data_file), "next_row": 0}

        # Load data and template
        try:
            if region == 'ca':
                template_to_use = TEMPLATE_FILE_CA
            elif region == 'us':
                template_to_use = TEMPLATE_FILE_US
            else: # Fallback
                template_to_use = TEMPLATE_FILE
            df = pd.read_csv(data_file) if data_file.suffix == '.csv' else pd.read_excel(data_file)
            template_str = template_to_use.read_text(encoding='utf-8')
            email_template = Template(template_str)
        except FileNotFoundError as e:
            current_app.logger.error(f"Required file not found: {e}. Cannot proceed.")
            return
        except Exception as e:
            current_app.logger.error(f"Error reading data file or template: {e}")
            return

        # If this is a new campaign, populate the database with all contacts as 'Pending'.
        # This ensures the campaign state is accurate from the start and prevents premature completion.
        if is_new_campaign:
            current_app.logger.info("Populating database with all contacts for the new campaign.")
            df.columns = [str(col).strip().replace(' ', '_').lower() for col in df.columns]
            email_col = 'email_id'
            if email_col not in df.columns:
                current_app.logger.error(f"Data file '{data_file.name}' must contain an '{email_col}' column. Halting job.")
                return

            # Since this is a new campaign run, clear previous statuses to ensure a clean start.
            ContactStatus.query.delete()

            contacts_to_add = []
            for _, row in df.iterrows():
                raw_email = row.get(email_col)
                email = str(raw_email).strip().strip('|').strip() if pd.notna(raw_email) else None
                if email:
                    name = row.get('name', row.get('first_name', ''))
                    company = row.get('company_name', row.get('company', ''))
                    contacts_to_add.append(ContactStatus(email=email, name=name, company_name=company, status='Pending'))
            db.session.bulk_save_objects(contacts_to_add)
            db.session.commit()
            current_app.logger.info(f"Database populated with {len(contacts_to_add)} contacts.")

        current_row_index = progress.get("next_row", 0)
        # Check if we've finished processing the file
        if current_row_index >= len(df):
            current_app.logger.info(f"Campaign completed: All contacts in {data_file.name} have been processed.")
            
            # Only pause and flag if not already done to prevent repeated logs
            if not CAMPAIGN_COMPLETE_FLAG.exists():
                try:
                    job = scheduler.get_job('send_email_job')
                    if job and job.next_run_time is not None:
                        scheduler.pause_job('send_email_job')
                        current_app.logger.info("Main campaign job ('send_email_job') has been paused automatically upon completion.")
                except Exception as e:
                    current_app.logger.error(f"Could not auto-pause main campaign job: {e}")

                CAMPAIGN_COMPLETE_FLAG.touch()
                current_app.logger.info("Campaign completion flag has been set.")
            return

        # Normalize columns and find email column
        df.columns = [str(col).strip().replace(' ', '_').lower() for col in df.columns]
        email_col = 'email_id' # Standardize on 'email_id' to match manual sending logic
        if email_col not in df.columns:
            current_app.logger.error(f"Data file '{data_file.name}' must contain an '{email_col}' column. Halting job for this file.")
            return

        # Process the single contact for this run
        contact = df.iloc[current_row_index]
        raw_email = contact.get(email_col)
        # Clean the email address: convert to string, strip whitespace and extra characters like '|'
        email = str(raw_email).strip().strip('|').strip() if pd.notna(raw_email) else None
        # Try to find name and company from common column names
        name = contact.get('name', contact.get('first_name', ''))
        company = contact.get('company_name', contact.get('company', ''))

        def update_db_status(status_text, send_result=None, error_details=None):
            if not pd.notna(email):
                return
            contact_status = ContactStatus.query.filter_by(email=email).first()
            if not contact_status:
                contact_status = ContactStatus(email=email, name=name, company_name=company)
                db.session.add(contact_status)
            contact_status.status = status_text
            contact_status.error_details = error_details
            if send_result:
                contact_status.internet_message_id = send_result.get("internetMessageId")
                contact_status.conversation_id = send_result.get("conversationId")
                contact_status.reply_received = False
                contact_status.error_details = None # Clear error on success
                # --- Start Follow-up Sequence on initial send ---
                if status_text == 'Sent (Scheduled)':
                    from datetime import datetime, timezone, timedelta
                    contact_status.sequence_status = 'active'
                    contact_status.sequence_step = 0
                    contact_status.next_follow_up_at = datetime.now(timezone.utc) + timedelta(days=3) # Hardcoded for now
            contact_status.last_update = datetime.now(timezone.utc)

        should_advance = True
        if email:
            current_app.logger.info(f"Processing row {current_row_index}: sending email to {email}")
            subject = "Introduction to GridsGlobal Steel Detailing LLC - Engineering services"
            html_body = "" # Initialize in case template rendering fails
            try:
                access_token = get_scheduler_token()
                html_body = email_template.render(contact.to_dict())
                send_result, error_msg = send_email_with_graph(access_token, email, subject, html_body, bcc_recipients=bcc_recipients)

                if send_result:
                    status_text = 'Sent (Scheduled)'
                    update_db_status(status_text, send_result=send_result)
                    log_email_attempt(email, name, company, subject, status_text, html_body, internet_message_id=send_result.get("internetMessageId"), conversation_id=send_result.get("conversationId"))
                else:
                    should_advance = True # Advance to the next contact even if sending fails.
                    status_text = 'Failed'
                    update_db_status(status_text, error_details=error_msg)
                    log_email_attempt(email, name, company, subject, status_text, html_body, error_details=error_msg)
                    current_app.logger.warning(f"Failed to send to {email}. Moving to next. Error: {error_msg}")
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                should_advance = True
                status_text = 'Failed'
                error_details = str(e)
                update_db_status(status_text, error_details=error_details)
                log_email_attempt(email, name, company, subject, status_text, html_body, error_details=error_details)
                db.session.commit()
                current_app.logger.error(f"Unhandled exception processing contact {email}: {e}. Moving to next.")
        else:
            current_app.logger.warning(f"Skipping row {current_row_index} due to missing email address.")

        # Update progress for the next run
        if should_advance:
            progress["next_row"] = current_row_index + 1

        try:
            JOB_PROGRESS_FILE.write_text(json.dumps(progress), encoding='utf-8')
        except IOError as e:
            current_app.logger.error(f"Fatal: Could not write to job progress file: {e}")

        current_app.logger.info("--- Throttled email job run finished ---")

def send_scheduled_follow_up_job():
    from app import app, db, ContactStatus, EmailLog, FOLLOW_UP_JOB_PROGRESS_FILE
    from graph_utils import send_email_with_graph
    from jinja2 import Template
    from datetime import datetime, timezone

    with app.app_context():
        current_app.logger.info("--- Starting scheduled follow-up job (1 email per run) ---")

        # Get BCC setting
        bcc_recipients = None
        try:
            if SETTINGS_FILE.exists():
                settings = json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
                if settings.get("bcc_enabled"):
                    bcc_recipients = [app.config['BCC_EMAIL']]
        except Exception as e:
            current_app.logger.error(f"Could not read BCC settings for follow-up scheduler: {e}")

        # Get latest data file from the follow-up folder
        data_file = get_latest_file(FOLLOW_UP_UPLOAD_FOLDER)
        if not data_file:
            current_app.logger.info("No data file found in 'uploads/follow_ups'. Skipping follow-up job.")
            return

        # Load job progress or initialize it
        progress = {"file_path": "", "next_row": 0}
        if FOLLOW_UP_JOB_PROGRESS_FILE.exists():
            try:
                progress = json.loads(FOLLOW_UP_JOB_PROGRESS_FILE.read_text(encoding='utf-8'))
            except (json.JSONDecodeError, IOError) as e:
                current_app.logger.error(f"Could not read follow-up job progress file, resetting. Error: {e}")

        # Reset progress if a new file is detected
        if progress.get("file_path") != str(data_file):
            current_app.logger.info(f"New follow-up data file '{data_file.name}' detected. Starting from beginning.")
            progress = {"file_path": str(data_file), "next_row": 0}

        # Load data and template
        try:
            df = pd.read_csv(data_file) if data_file.suffix == '.csv' else pd.read_excel(data_file)
            
            template_content = FOLLOW_UP_TEMPLATE_FILE.read_text(encoding='utf-8')
            parts = template_content.split('\n\n', 1)
            subject_template_str = "Follow-up regarding {{ company_name }}"
            body_template_str = template_content
            if len(parts) == 2 and parts[0].lower().startswith('subject:'):
                subject_template_str = parts[0][len('subject:'):].strip()
                body_template_str = parts[1]
            
            subject_template = Template(subject_template_str)
            body_template = Template(body_template_str)

        except FileNotFoundError as e:
            current_app.logger.error(f"Required file not found for follow-up job: {e}. Cannot proceed.")
            return
        except Exception as e:
            current_app.logger.error(f"Error reading follow-up data file or template: {e}")
            return

        current_row_index = progress.get("next_row", 0)
        if current_row_index >= len(df):
            current_app.logger.info(f"Follow-up campaign completed: All contacts in {data_file.name} have been processed.")
            # Pause the job upon completion to prevent it from running unnecessarily
            from app import scheduler
            try:
                job = scheduler.get_job('send_scheduled_follow_up_job')
                if job and job.next_run_time is not None:
                    scheduler.pause_job('send_scheduled_follow_up_job')
                    current_app.logger.info("Follow-up campaign job has been paused automatically upon completion.")
            except Exception as e:
                current_app.logger.error(f"Could not auto-pause follow-up campaign job: {e}")
            return

        df.columns = [str(col).strip().replace(' ', '_').lower() for col in df.columns]
        email_col = 'email_id'
        if email_col not in df.columns:
            current_app.logger.error(f"Follow-up data file '{data_file.name}' must contain an '{email_col}' column. Halting job.")
            return

        contact_data = df.iloc[current_row_index]
        raw_email = contact_data.get(email_col)
        email = str(raw_email).strip().strip('|').strip() if pd.notna(raw_email) else None
        name = contact_data.get('name', '')
        company = contact_data.get('company_name', '')

        should_advance = True
        if email:
            current_app.logger.info(f"Processing follow-up row {current_row_index}: sending to {email}")
            try:
                access_token = get_scheduler_token()
                
                render_data = contact_data.to_dict()
                subject = subject_template.render(render_data)
                html_body = body_template.render(render_data)

                contact_status = ContactStatus.query.filter_by(email=email).first()
                conversation_id_to_use = contact_status.conversation_id if contact_status else None
                if not conversation_id_to_use:
                    current_app.logger.warning(f"No existing conversation ID found for {email}. Sending as new thread.")

                send_result, error_msg = send_email_with_graph(
                    access_token, email, subject, html_body,
                    bcc_recipients=bcc_recipients, conversation_id=conversation_id_to_use
                )

                status_text = 'Follow-up Sent (Scheduled)' if send_result else 'Follow-up Failed (Scheduled)'
                
                if contact_status:
                    contact_status.status = status_text
                    contact_status.last_update = datetime.now(timezone.utc)
                    contact_status.error_details = error_msg if not send_result else None

                # Corrected call to log_email_attempt
                log_email_attempt(
                    email=email,
                    name=name,
                    company_name=company,
                    subject=subject,
                    status=status_text,
                    body=html_body,
                    error_details=error_msg if not send_result else None,
                    internet_message_id=send_result.get("internetMessageId") if send_result else None,
                    conversation_id=send_result.get("conversationId") if send_result else conversation_id_to_use
                )
                db.session.commit()

            except Exception as e:
                db.session.rollback()
                current_app.logger.error(f"Unhandled exception processing follow-up for {email}: {e}", exc_info=True)
        else:
            current_app.logger.warning(f"Skipping follow-up row {current_row_index} due to missing email address.")

        if should_advance:
            progress["next_row"] = current_row_index + 1

        try:
            FOLLOW_UP_JOB_PROGRESS_FILE.write_text(json.dumps(progress), encoding='utf-8')
        except IOError as e:
            current_app.logger.error(f"Fatal: Could not write to follow-up job progress file: {e}")

        current_app.logger.info("--- Scheduled follow-up job run finished ---")

def check_replies_job():
    from datetime import datetime, timedelta, timezone
    from app import app, db, ContactStatus, EmailLog
    with app.app_context():
        current_app.logger.info("--- Starting check for replies job ---")
        access_token = get_scheduler_token()
        if not access_token:
            current_app.logger.error("Could not get token for reply check job.")
            return
 
        headers = {'Authorization': f'Bearer {access_token}'}
 
        # Get user's own email address
        try:
            me_resp = requests.get("https://graph.microsoft.com/v1.0/me", headers=headers)
            me_resp.raise_for_status()
            user_email = me_resp.json().get('mail') or me_resp.json().get('userPrincipalName')
            if not user_email:
                current_app.logger.error("Could not determine user's email address from /me endpoint.")
                return
        except requests.RequestException as e:
            current_app.logger.error(f"Failed to get user info: {e}")
            return
 
        # Determine the time window for checking emails
        last_check_time = None
        if REPLY_PROGRESS_FILE.exists():
            try:
                progress = json.loads(REPLY_PROGRESS_FILE.read_text(encoding='utf-8'))
                last_check_time = progress.get('last_check_utc')
            except (json.JSONDecodeError, IOError):
                current_app.logger.warning("Could not read reply progress file, checking last 24 hours.")
 
        if not last_check_time:
            # Default to checking the last 24 hours on first run
            last_check_time_dt = datetime.now(timezone.utc) - timedelta(days=1)
        else:
            last_check_time_dt = datetime.fromisoformat(last_check_time)
 
        # The filter needs to be in ISO 8601 format
        filter_time_str = last_check_time_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
        
        # New, broader query to fetch ALL incoming mail, not just replies to tracked conversations.
        query = f"receivedDateTime gt {filter_time_str} and from/emailAddress/address ne '{user_email}'"
        select_fields = "id,conversationId,receivedDateTime,bodyPreview,from,subject,body"

        messages = []
        try:
            # First, get the ID of the 'inbox' folder to make the query more robust.
            mail_folders_url = "https://graph.microsoft.com/v1.0/me/mailFolders?$filter=wellKnownName eq 'inbox'"
            folders_resp = requests.get(mail_folders_url, headers=headers)
            folders_resp.raise_for_status()
            folders = folders_resp.json().get('value')
            if not folders:
                current_app.logger.error("Could not find the Inbox folder via Graph API.")
                return
            inbox_id = folders[0]['id']
            current_app.logger.info(f"Found Inbox folder with ID: {inbox_id}")

            # Now, query for messages within that specific folder ID.
            messages_url = f"https://graph.microsoft.com/v1.0/me/mailFolders/{inbox_id}/messages?$filter={query}&$select={select_fields}&$top=100"
            messages_resp = requests.get(messages_url, headers=headers)
            messages_resp.raise_for_status()
            messages = messages_resp.json().get('value', [])
        except requests.RequestException as e:
            current_app.logger.error(f"Failed to fetch recent messages: {e}")
            return
 
        if not messages:
            current_app.logger.info("No new incoming messages found in the inbox.")
        else:
            current_app.logger.info(f"Found {len(messages)} new incoming messages to process.")
            # Process oldest first to maintain chronological order in DB
            for message in reversed(messages):
                message_id = message.get('id')
                conv_id = message.get('conversationId')
 
                # Skip if we've already logged this specific message
                if EmailLog.query.filter_by(internet_message_id=message_id).first():
                    continue
 
                reply_time = datetime.fromisoformat(message['receivedDateTime'].replace('Z', '+00:00'))
                sender_info = message.get('from', {}).get('emailAddress', {})
                sender_email = sender_info.get('address')
                sender_name = sender_info.get('name')
                
                # Try to get full HTML body, fallback to preview
                body_content = message.get('body', {}).get('content', message.get('bodyPreview', ''))
 
                # Check if this is a reply to one of our tracked emails to update campaign status
                original_sent_log = EmailLog.query.filter(
                    EmailLog.conversation_id == conv_id,
                    EmailLog.reply_received == False,
                    EmailLog.status.like('%Sent%')
                ).first()
 
                if original_sent_log:
                    # It's a reply to a campaign email. Update the original sent log.
                    original_sent_log.status = 'Replied'
                    original_sent_log.reply_received = True
                    original_sent_log.reply_received_at = reply_time
                    current_app.logger.info(f"Marking original sent log {original_sent_log.id} as replied.")
 
                # Also update the live ContactStatus for the results page
                # Try to find by conversation_id first, but fall back to email if needed.
                contact_to_update = ContactStatus.query.filter(
                    ContactStatus.conversation_id == conv_id,
                    ContactStatus.reply_received == False
                ).first()

                if not contact_to_update and sender_email:
                    # Fallback: if a reply comes from a different email address but is in the same thread,
                    # we might not find the contact. Let's try to find the original contact by the email in the log.
                    contact_to_update = ContactStatus.query.filter_by(email=original_sent_log.email, reply_received=False).first() if original_sent_log else None

                if contact_to_update:
                    contact_to_update.status = 'Replied'
                    contact_to_update.reply_received = True
                    contact_to_update.reply_content = message.get('bodyPreview', '') # Keep preview on contact status
                    contact_to_update.reply_received_at = reply_time
                    contact_to_update.sequence_status = 'inactive' # Stop the follow-up sequence
                    contact_to_update.next_follow_up_at = None
                    current_app.logger.info(f"Updating live campaign ContactStatus for {contact_to_update.email} and stopping follow-up sequence.")
                
                # Create a NEW log entry for the incoming message itself
                new_log_entry = EmailLog(
                    email=sender_email,
                    name=sender_name,
                    subject=message.get('subject', '(No Subject)'),
                    status='Received', # A new status for incoming mail
                    sent_at=reply_time, # Use reply time as the "sent_at" for this log
                    conversation_id=conv_id,
                    internet_message_id=message_id,
                    reply_received=True, # This log entry IS a received item
                    reply_content=body_content,
                    reply_received_at=reply_time
                )
                db.session.add(new_log_entry)
                current_app.logger.info(f"Created new log entry for incoming message from {sender_email}.")
            
            db.session.commit()
 
        # Save the new last check time
        new_check_time = datetime.now(timezone.utc).isoformat()
        try:
            REPLY_PROGRESS_FILE.write_text(json.dumps({'last_check_utc': new_check_time}), encoding='utf-8')
        except IOError as e:
            current_app.logger.error(f"Could not write to reply progress file: {e}")

        current_app.logger.info("--- Finished check for replies job ---")

def send_follow_up_job():
    """
    Sends follow-up emails to contacts in an active sequence.
    """
    from app import app, db, ContactStatus, EmailLog
    from graph_utils import send_email_with_graph
    from datetime import datetime, timezone, timedelta

    with app.app_context():
        current_app.logger.info("--- Starting follow-up email job ---")

        # --- Sequence Configuration ---
        # In a real app, this might come from a settings page in the DB.
        FOLLOW_UP_SEQUENCE_LENGTH = 2  # Number of follow-up emails to send
        FOLLOW_UP_INTERVAL_DAYS = 3    # Days between follow-ups
        FOLLOW_UP_TEMPLATES = {
            1: INSTANCE_FOLDER / "follow_up_template_1.html",
            2: INSTANCE_FOLDER / "follow_up_template_2.html",
        }
        # ---

        # Get BCC setting
        bcc_recipients = None
        try:
            if SETTINGS_FILE.exists():
                settings = json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
                if settings.get("bcc_enabled"):
                    bcc_recipients = [app.config['BCC_EMAIL']]
        except Exception as e:
            current_app.logger.error(f"Could not read BCC settings for follow-up job: {e}")

        contacts_to_follow_up = ContactStatus.query.filter(
            ContactStatus.sequence_status == 'active',
            ContactStatus.next_follow_up_at <= datetime.now(timezone.utc)
        ).all()

        if not contacts_to_follow_up:
            current_app.logger.info("No contacts are due for a follow-up at this time.")
            current_app.logger.info("--- Follow-up email job finished ---")
            return

        current_app.logger.info(f"Found {len(contacts_to_follow_up)} contacts for follow-up.")
        access_token = get_scheduler_token()
        if not access_token:
            current_app.logger.error("Could not get token for follow-up job. Aborting.")
            return

        for contact in contacts_to_follow_up:
            try:
                step = contact.sequence_step + 1
                if step > FOLLOW_UP_SEQUENCE_LENGTH:
                    current_app.logger.warning(f"Contact {contact.email} is at step {contact.sequence_step} but max is {FOLLOW_UP_SEQUENCE_LENGTH}. Completing sequence.")
                    contact.sequence_status = 'completed'
                    contact.next_follow_up_at = None
                    continue

                template_path = FOLLOW_UP_TEMPLATES.get(step)
                if not template_path or not template_path.exists():
                    current_app.logger.error(f"Follow-up template for step {step} not found at {template_path}. Pausing sequence for {contact.email}.")
                    contact.sequence_status = 'paused'
                    contact.error_details = f"Template for step {step} not found."
                    continue

                template_content = template_path.read_text(encoding='utf-8')
                parts = template_content.split('\n\n', 1)
                subject_template_str = f"Re: Follow-up for {contact.company_name}"
                body_template_str = template_content

                if len(parts) == 2 and parts[0].lower().startswith('subject:'):
                    subject_template_str = parts[0][len('subject:'):].strip()
                    body_template_str = parts[1]

                subject_template = Template(subject_template_str)
                body_template = Template(body_template_str)
                
                render_data = {'name': contact.name, 'company_name': contact.company_name, 'email': contact.email}
                subject = subject_template.render(render_data)
                html_body = body_template.render(render_data)

                send_result, error_msg = send_email_with_graph(
                    access_token, contact.email, subject, html_body,
                    bcc_recipients=bcc_recipients, conversation_id=contact.conversation_id
                )

                if send_result:
                    current_app.logger.info(f"Successfully sent follow-up #{step} to {contact.email}.")
                    contact.status = f'Follow-up {step} Sent'
                    contact.sequence_step = step
                    contact.error_details = None

                    if step >= FOLLOW_UP_SEQUENCE_LENGTH:
                        contact.sequence_status = 'completed'
                        contact.next_follow_up_at = None
                        current_app.logger.info(f"Sequence completed for {contact.email}.")

                    log_email_attempt(
                        email=contact.email, name=contact.name, company_name=contact.company_name,
                        subject=subject, status=contact.status, body=html_body,
                        internet_message_id=send_result.get("internetMessageId"),
                        conversation_id=send_result.get("conversationId")
                    )
                else:
                    current_app.logger.error(f"Failed to send follow-up #{step} to {contact.email}: {error_msg}")
                    contact.status = f'Follow-up {step} Failed'
                    contact.error_details = error_msg
                    contact.sequence_status = 'paused'
            except Exception as e:
                current_app.logger.error(f"An unexpected error occurred processing follow-up for {contact.email}: {e}", exc_info=True)
                contact.sequence_status = 'paused'
                contact.error_details = f"Unexpected error: {str(e)}"

        db.session.commit()

        current_app.logger.info("--- Follow-up email job finished ---")

scheduler = BackgroundScheduler(daemon=True)
