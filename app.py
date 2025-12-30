import os
import json
import logging
import requests
import sqlite3
import io
from flask import (
    Flask, render_template, request, redirect, jsonify,
    url_for, flash, session, Response
)
from werkzeug.utils import secure_filename
from functools import wraps
import pandas as pd
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env file at the very beginning of the script
load_dotenv()

from concurrent_log_handler import ConcurrentRotatingFileHandler
import msal
from datetime import datetime, timezone, date, time, timedelta
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import or_
from graph_utils import send_email_with_graph
from scheduler import scheduler, send_email_job, check_replies_job, send_follow_up_job, send_scheduled_follow_up_job
from apscheduler.schedulers.base import STATE_RUNNING, STATE_PAUSED
from auth import (
    build_auth_url, acquire_token_by_auth_code, msal_app, SCOPES
)
from inbox_api import inbox_api_bp # Import the new blueprint
import zoneinfo

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv("SECRET_KEY") or "a-secret-key"
app.config['UPLOAD_FOLDER'] = "uploads"
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Attachment paths
app.config['BCC_EMAIL'] = 'arjuns@gridsglobal-detailing.com'
# Construct a dynamic, relative path for the attachment to make the app portable.
# Assumes an 'attachments' folder exists in the project root.
attachment_filename = 'Capability statement-GridsGlobal Steel Detailing LLC_US.pdf'
app.config['ATTACHMENT_PDF_PATH'] = os.path.join(app.root_path, 'attachments', attachment_filename)

# Database config
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///crm_app.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

ALLOWED_EXTENSIONS = {"csv", "xlsx"}
TEMPLATE_FILE = Path(app.instance_path) / "email_template.html"
TEMPLATE_FILE_CA = Path(app.instance_path) / "email_template_ca.html"
TEMPLATE_FILE_US = Path(app.instance_path) / "email_template_us.html"
FOLLOW_UP_TEMPLATE_FILE = Path(app.instance_path) / "follow_up_template.html"
FOLLOW_UP_TEMPLATE_1 = Path(app.instance_path) / "follow_up_template_1.html"
FOLLOW_UP_TEMPLATE_2 = Path(app.instance_path) / "follow_up_template_2.html"
JOB_PROGRESS_FILE = Path(app.instance_path) / ".job_progress.json"
FOLLOW_UP_JOB_PROGRESS_FILE = Path(app.instance_path) / ".follow_up_job_progress.json"
REPLY_PROGRESS_FILE = Path(app.instance_path) / ".reply_progress.json"
SETTINGS_FILE = Path(app.instance_path) / "settings.json"
TOKEN_FILE = Path(app.instance_path) / ".graph_token.json"
CAMPAIGN_COMPLETE_FLAG = Path(app.instance_path) / ".campaign_completed_notified"

# Define manual statuses to be used in multiple routes
MANUAL_STATUSES = ["We Have Detailers", "Try Later", "Send Your details", "Number No Longer Available", "Left a voicemail"]

class ContactStatus(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), nullable=False, unique=True)
    name = db.Column(db.String(100))
    company_name = db.Column(db.String(100))
    status = db.Column(db.String(50), default='Pending')
    last_update = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    # Fields for reply tracking, used by the scheduler
    internet_message_id = db.Column(db.String(255), unique=True, nullable=True)
    conversation_id = db.Column(db.String(255), nullable=True, index=True)
    reply_received = db.Column(db.Boolean, default=False)
    reply_content = db.Column(db.Text, nullable=True)
    reply_received_at = db.Column(db.DateTime, nullable=True)
    error_details = db.Column(db.Text, nullable=True)
    # --- New columns for follow-up sequences ---
    sequence_step = db.Column(db.Integer, nullable=False, default=0)
    next_follow_up_at = db.Column(db.DateTime, nullable=True)
    sequence_status = db.Column(db.String(50), nullable=False, default='inactive') # inactive, active, paused, completed

with app.app_context():
    # This new model will store a persistent log of all sent emails.
    # It is NOT cleared when a campaign is reset.
    class EmailLog(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        sent_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
        email = db.Column(db.String(120), nullable=False, index=True)
        name = db.Column(db.String(100))
        company_name = db.Column(db.String(100))
        status = db.Column(db.String(50), nullable=False) # e.g., 'Sent', 'Failed', 'Mono Sent'
        subject = db.Column(db.String(255))
        body = db.Column(db.Text, nullable=True)
        error_details = db.Column(db.Text, nullable=True)
        # --- New columns for reply tracking ---
        internet_message_id = db.Column(db.String(255), unique=True, nullable=True)
        conversation_id = db.Column(db.String(255), nullable=True, index=True)
        reply_received = db.Column(db.Boolean, default=False)
        reply_content = db.Column(db.Text, nullable=True)
        reply_received_at = db.Column(db.DateTime, nullable=True)
    # Run a schema update check before creating all tables
    # This handles adding new columns to existing tables non-destructively.
    db_path = Path(app.instance_path) / 'crm_app.db'
    if db_path.exists():
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            # Check if the table exists first
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='contact_status'")
            if cursor.fetchone() is not None:
                cursor.execute("PRAGMA table_info(contact_status)")
                existing_columns = {row[1] for row in cursor.fetchall()}
                app.logger.info(f"Checking schema for 'contact_status'... Found columns: {existing_columns}")

                columns_to_ensure = [
                    ("internet_message_id", "TEXT"),
                    ("conversation_id", "TEXT"),
                    ("reply_received", "BOOLEAN"),
                    ("reply_content", "TEXT"),
                    ("reply_received_at", "DATETIME"),
                    ("error_details", "TEXT"),
                    ("sequence_step", "INTEGER NOT NULL DEFAULT 0"),
                    ("next_follow_up_at", "DATETIME"),
                    ("sequence_status", "TEXT NOT NULL DEFAULT 'inactive'"),
                ]

                for column_name, column_def in columns_to_ensure:
                    if column_name not in existing_columns:
                        app.logger.info(f"Attempting to add column '{column_name}'...")
                        cursor.execute(f"ALTER TABLE contact_status ADD COLUMN {column_name} {column_def};")
                        app.logger.info(f"SUCCESS: Column '{column_name}' added to the 'contact_status' table.")
                
                conn.commit()
            else:
                app.logger.info("`contact_status` table doesn't exist yet. SQLAlchemy will create it.")
            
            # Check if the EmailLog table exists and add columns
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='email_log'")
            if cursor.fetchone() is not None:
                cursor.execute("PRAGMA table_info(email_log)")
                existing_columns = {row[1] for row in cursor.fetchall()}
                app.logger.info(f"Checking schema for 'email_log'... Found columns: {existing_columns}")

                columns_to_add = [
                    ("internet_message_id", "TEXT"),
                    ("conversation_id", "TEXT"),
                    ("reply_received", "BOOLEAN"),
                    ("reply_content", "TEXT"),
                    ("reply_received_at", "DATETIME"),
                    ("body", "TEXT"),
                ]

                for col_name, col_def in columns_to_add:
                    if col_name not in existing_columns:
                        app.logger.info(f"Attempting to add column '{col_name}' to 'email_log'...")
                        cursor.execute(f"ALTER TABLE email_log ADD COLUMN {col_name} {col_def};")
                        app.logger.info(f"SUCCESS: Column '{col_name}' added to 'email_log'.")
                conn.commit()
            else:
                app.logger.info("`email_log` table doesn't exist yet. SQLAlchemy will create it.")
            
            conn.close()
            app.logger.info("Database schema update check finished.")
        except Exception as e:
            app.logger.error(f"An unexpected error occurred during schema update: {e}")
    else:
        app.logger.info("Database does not exist. It will be created by SQLAlchemy. No schema update needed.")

    db.create_all()

    # Create dummy follow-up templates if they don't exist
    if not FOLLOW_UP_TEMPLATE_1.exists():
        FOLLOW_UP_TEMPLATE_1.write_text("Subject: Following Up\n\n<p>Hi {{ name }},</p><p>Just wanted to gently follow up on my previous email. We're keen to see if our steel detailing services could be a good fit for {{ company_name }}.</p><p>Would you be open to a brief chat next week?</p><p>Best regards,</p><p>Arjun</p>", encoding="utf-8")
        app.logger.info("Created dummy follow-up template 1.")
    if not FOLLOW_UP_TEMPLATE_2.exists():
        FOLLOW_UP_TEMPLATE_2.write_text("Subject: Final Follow Up\n\n<p>Hi {{ name }},</p><p>I'm writing one last time to see if you had a chance to consider our services. We are confident we can provide significant value to your projects.</p><p>If the timing isn't right, I understand. I won't reach out again, but please keep us in mind for the future.</p><p>All the best,</p><p>Arjun</p>", encoding="utf-8")
        app.logger.info("Created dummy follow-up template 2.")
    if not FOLLOW_UP_TEMPLATE_FILE.exists():
        FOLLOW_UP_TEMPLATE_FILE.write_text("Subject: Following up on our conversation\n\n<p>Hi {{ name }},</p><p>Just wanted to touch base regarding our services for {{ company_name }}.</p><p>Best,</p><p>Arjun</p>", encoding="utf-8")
        app.logger.info("Created dummy manual follow-up template.")


    # Register the inbox API blueprint
    app.register_blueprint(inbox_api_bp)

    # Create a dummy Canadian template if it doesn't exist
    if not TEMPLATE_FILE_CA.exists():
        TEMPLATE_FILE_CA.write_text("Subject: Services for {{ company_name }}\n\n<p>Hi {{ name }},</p><p>This is the Canadian template.</p><p>Best regards,</p><p>Arjun</p>", encoding="utf-8")
        app.logger.info("Created dummy Canadian email template.")

    # Create a dummy US template if it doesn't exist
    if not TEMPLATE_FILE_US.exists():
        # Using the content from the provided email_template_us.html
        TEMPLATE_FILE_US.write_text("<p>Dear {{name}},</p><p>I hope you're doing well.</p><p>My name is Arjun, and I serve as the Business Development Manager at GridsGlobal Steel Detailing LLC, an AISC associate member firm with operations across the United States and India. I’m reaching out to share how our team can support detailing and connection design requirements of {{company_name}}.</p><p>We specialize in:</p><ul><li>Structural Steel Detailing</li><li>Miscellaneous Metals Detailing</li><li>PE-Stamped Connection Design (Certified in all U.S. states)</li><li>Estimation and Material Takeoff Services</li></ul><p>We use leading software such as SDS2, Tekla Structures IdeaStatica for detailing and design to ensure speed, accuracy, and full compliance with U.S. fabrication standards (AISC).</p><p>If you're exploring new partnerships or considering outsourcing your detailing needs, we’d love the opportunity to:</p><ul><li>Be added to your prequalification bid list</li><li>Submit a tailored proposal</li><li>Align closely with your project goals and deadlines</li></ul><p>I’d be happy to connect further and explore how we can collaborate.</p><p>Warm regards,<br>Arjun</p>", encoding="utf-8")
        TEMPLATE_FILE_US.write_text("<p>Dear {{ name }},</p><p>I hope you're doing well.</p><p>My name is Arjun, and I serve as the Business Development Manager at GridsGlobal Steel Detailing LLC, an AISC associate member firm with operations across the United States and India. I’m reaching out to share how our team can support detailing and connection design requirements of {{ company_name }}.</p><p>We specialize in:</p><ul><li>Structural Steel Detailing</li><li>Miscellaneous Metals Detailing</li><li>PE-Stamped Connection Design (Certified in all U.S. states)</li><li>Estimation and Material Takeoff Services</li></ul><p>We use leading software such as SDS2, Tekla Structures IdeaStatica for detailing and design to ensure speed, accuracy, and full compliance with U.S. fabrication standards (AISC).</p><p>If you're exploring new partnerships or considering outsourcing your detailing needs, we’d love the opportunity to:</p><ul><li>Be added to your prequalification bid list</li><li>Submit a tailored proposal</li><li>Align closely with your project goals and deadlines</li></ul><p>I’d be happy to connect further and explore how we can collaborate.</p><p>Warm regards,<br>Arjun</p>", encoding="utf-8")
        app.logger.info("Created dummy US email template.")


# Logging setup
os.makedirs(app.instance_path, exist_ok=True)
log_file = Path(app.instance_path) / 'app.log'
# Use ConcurrentRotatingFileHandler to prevent multi-process file locking issues on Windows
file_handler = ConcurrentRotatingFileHandler(log_file, "a", maxBytes=20480, backupCount=5)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)

# Add handler to Flask's logger and APScheduler's logger
app.logger.addHandler(file_handler)
logging.getLogger('apscheduler').addHandler(file_handler)
app.logger.setLevel(logging.INFO)

# Scheduler setup
if not scheduler.get_jobs():
    # Throttled email sending job (1 email per run)
    try:
        # Get interval from environment variable in seconds, with a new default of 30 seconds.
        # The environment variable is now EMAIL_INTERVAL_SECONDS.
        interval_seconds = int(os.getenv("EMAIL_INTERVAL_SECONDS", "30"))
        if interval_seconds <= 0: interval_seconds = 30
    except (ValueError, TypeError):
        interval_seconds = 30
        app.logger.warning("Invalid EMAIL_INTERVAL_SECONDS in .env, defaulting to 30 seconds.")

    app.logger.info(f"Scheduler 'send_email_job' configured to run every {interval_seconds} seconds.")
    scheduler.add_job(
        send_email_job, 'interval', seconds=interval_seconds,
        id='send_email_job', replace_existing=True
    )
    scheduler.add_job(
        check_replies_job, 'interval', minutes=5,
        id='check_replies_job', replace_existing=True
    )
    app.logger.info("Scheduler 'send_follow_up_job' configured to run every 60 minutes.")
    scheduler.add_job(
        send_follow_up_job, 'interval', minutes=60,
        id='send_follow_up_job', replace_existing=True
    )
    scheduler.add_job(
        send_scheduled_follow_up_job, 'interval', seconds=30,
        id='send_scheduled_follow_up_job',
        replace_existing=True,
        # Start this job in a paused state by default
        next_run_time=None
    )
    app.logger.info("Scheduler 'send_scheduled_follow_up_job' configured to run every 30 seconds (initially paused).")


if not scheduler.running:
    scheduler.start(paused=True)
    app.logger.info("Scheduler started in a paused state. It will not send emails until resumed.")

def get_settings():
    """Reads settings from a JSON file, returning defaults if not found."""
    if not SETTINGS_FILE.exists():
        return {"bcc_enabled": True} # Default to on
    try:
        with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
            settings = json.load(f)
            # Ensure the key exists, defaulting to True if it doesn't.
            if 'bcc_enabled' not in settings:
                settings['bcc_enabled'] = True
            return settings
    except (IOError, json.JSONDecodeError):
        return {"bcc_enabled": True}

def save_settings(settings_data):
    """Saves settings to a JSON file."""
    try:
        os.makedirs(app.instance_path, exist_ok=True)
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(settings_data, f, indent=2)
        return True
    except IOError as e:
        app.logger.error(f"Failed to save settings file: {e}")
        return False

def log_email_attempt(email, name, company_name, subject, status, body, error_details=None, internet_message_id=None, conversation_id=None):
    """Logs an email attempt to the persistent EmailLog table."""
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
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Failed to create and commit persistent email log for {email}: {e}")

def get_access_token():
    """
    Acquires a token, refreshing it if necessary using the
    stored refresh token. This is the single source of truth for tokens for the web session.
    """
    if not TOKEN_FILE.exists():
        app.logger.error("Graph token file not found. Please log in via the web app first.")
        return None

    try:
        token_data = json.loads(TOKEN_FILE.read_text(encoding='utf-8'))
    except (IOError, json.JSONDecodeError) as e:
        app.logger.error(f"Could not read or parse token file: {e}")
        return None

    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        app.logger.error("Refresh token not found in token file. Please log in again to regenerate it.")
        session.clear()  # Force re-login
        return None

    # Use the global msal_app instance
    result = msal_app.acquire_token_by_refresh_token(refresh_token, scopes=SCOPES)

    if "access_token" in result:
        # Save the new token data back to the file to get a new refresh_token if it was rotated
        try:
            TOKEN_FILE.write_text(json.dumps(result), encoding='utf-8')
        except IOError as e:
            app.logger.warning(f"Could not update token file with refreshed token: {e}")
        return result["access_token"]
    else:
        error_desc = result.get('error_description', 'Unknown error')
        app.logger.error(f"Failed to refresh token for web session: {error_desc}")
        # If refresh fails, the user must log in again.
        flash("Your session has expired. Please log in again.", "warning")
        session.clear()
        return None

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if "username" not in session:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify(success=False, error='Authentication required. Please refresh the page and log in.'), 401
            flash("Please login first", "warning")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapped

def normalize_keys(row):
    return {k.strip().replace(" ", "_").lower(): v for k, v in row.items()}

def get_scheduler_status(job_id='send_email_job', name='Main Campaign'):
    """Helper to get a consistent scheduler status object."""
    # This function now checks the status of a specific job,
    # while also considering the main scheduler's state.
    main_scheduler_state = scheduler.state
    job = scheduler.get_job(job_id)

    status_text = 'not_found'
    next_run = "N/A"
    interval = "N/A"
    pending_count = 0

    if job:
        if main_scheduler_state == STATE_PAUSED:
            status_text = 'paused'
            next_run = "Scheduler Paused"
        elif job.next_run_time is None:
            status_text = 'paused'
            next_run = "Job Paused"
        else:
            status_text = 'running'
            next_run = job.next_run_time.astimezone().strftime('%Y-%m-%d %H:%M:%S')
        
        try:
            interval = job.trigger.interval.total_seconds()
        except AttributeError:
            app.logger.warning(f"Could not read interval from job trigger for {job_id}.")
            interval = "N/A"

        # Add pending counts for each job
        if job_id == 'send_email_job':
            pending_count = ContactStatus.query.filter_by(status='Pending').count()
        elif job_id == 'send_scheduled_follow_up_job':
            # This is a bit more complex, we'll count based on the file for now.
            # A more robust solution might involve a dedicated column in the DB.
            pass # For now, we'll leave this as 0.
    
    return {'name': name, 'job_id': job_id, 'state': status_text, 'next_run': next_run, 'interval': interval, 'pending_count': pending_count}

def get_recent_logs(num_lines=100):
    """
    Retrieves the last `num_lines` from the application's log file.
    Returns a string with the log content or an error message if something goes wrong.
    """
    log_file_path = Path(app.instance_path) / 'app.log'
    if not log_file_path.exists():
        return f"Log file not found at '{log_file_path}'."

    try:
        # Open the file with a specific encoding and read the lines
        with open(log_file_path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
            # Get the last `num_lines`, or all lines if the file is shorter
            recent_lines = lines[-num_lines:]
            return "".join(recent_lines)
    except Exception as e:
        # Catch any other potential errors during file reading
        return f"An error occurred while reading the log file: {e}"

@app.template_filter('timesince')
def timesince(dt, default="just now"):
    """
    Returns a string representing "time since" a given datetime.
    e.g., "3 days ago", "5 hours ago", etc.
    """
    if dt is None:
        return ""

    # Ensure the input datetime is aware of its timezone (assuming UTC if naive)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    diff = now - dt
    
    periods = (
        (diff.days / 365, "year", "years"),
        (diff.days / 30, "month", "months"),
        (diff.days / 7, "week", "weeks"),
        (diff.days, "day", "days"),
        (diff.seconds / 3600, "hour", "hours"),
        (diff.seconds / 60, "minute", "minutes"),
        (diff.seconds, "second", "seconds"),
    )

    for period, singular, plural in periods:
        if period >= 1:
            period = int(period)
            return f"{period} {singular if period == 1 else plural} ago"

    return default

@app.template_filter('to_ist')
def to_ist_filter(dt):
    """Converts a UTC datetime object to Indian Standard Time (IST)."""
    if dt is None:
        return ""
    # Ensure the input datetime is aware of its timezone (assuming UTC if naive)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    # Get the IST timezone
    ist_tz = zoneinfo.ZoneInfo("Asia/Kolkata")
    # Convert the datetime to IST
    return dt.astimezone(ist_tz)

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        # Get admin credentials from environment variables
        admin_user = os.getenv("ADMIN_USERNAME")
        admin_pass = os.getenv("ADMIN_PASSWORD")

        if username == admin_user and password == admin_pass:
            session["username"] = username
            session['just_logged_in'] = True
            flash(f"Welcome, {username}!", "success")
            return redirect(url_for("dashboard"))
        else:
            flash("Invalid username or password.", "danger")
            return redirect(url_for("login"))

    # For GET request, just show the login page
    return render_template("login.html", body_class="login-page")

@app.route("/ms_login")
def ms_login():
    """Route to initiate the one-time Microsoft login to get the API token."""
    try:
        auth_url = build_auth_url()
    except ValueError as e:
        app.logger.error(f"Failed to build auth URL: {e}")
        flash("Configuration error: Check OAUTH_REDIRECT_URI in .env", "danger")
        return redirect(url_for("login"))

    if not auth_url:
        app.logger.error("Failed to build authentication URL. Check MSAL configuration (MS_CLIENT_ID, MS_CLIENT_SECRET, MS_TENANT_ID in .env).")
        flash("Microsoft authentication service is misconfigured. Check application logs.", "danger")
        return redirect(url_for("login"))
    return redirect(auth_url)

@app.route("/getAToken")
def authorized():
    if request.args.get('error'):
        return f"Login failed: {request.args.get('error')} - {request.args.get('error_description')}", 400

    code = request.args.get("code")
    if not code:
        return "Authorization code missing", 400
    
    try:
        result = acquire_token_by_auth_code(code)
    except Exception as e:
        # This is a critical catch-all. If the MSAL library itself crashes, we log it.
        app.logger.error(f"CRITICAL: An unhandled exception occurred in acquire_token_by_auth_code: {e}", exc_info=True)
        return f"A critical error occurred during authentication. Please check the application logs. Error: {e}", 500

    if "access_token" in result:
        # Store user info and token in the session for the web app and inbox API
        claims = result.get("id_token_claims", {})
        
        # Robustly determine username (fallback to email if name is missing)
        user_name = claims.get("name") or claims.get("preferred_username") or claims.get("email") or "Microsoft User"
        
        session["user"] = {
            "name": claims.get("name"),
            "name": user_name,
            "userPrincipalName": claims.get("preferred_username")
        }
        session["username"] = claims.get("name") # Keep for compatibility with other parts of the app
        session["username"] = user_name
        session['just_logged_in'] = True
        session.permanent = True
        # session["token_cache"] = result # REMOVED: Storing full token cache exceeds 4KB cookie limit

        # Save the full token (including refresh_token) for the scheduler
        try:
            os.makedirs(app.instance_path, exist_ok=True)
            TOKEN_FILE.write_text(json.dumps(result), encoding='utf-8')
        except IOError as e:
            app.logger.error(f"Could not write token file for scheduler: {e}")
            flash("Warning: Could not save token for background jobs. Scheduled sending may fail.", "warning")

        flash(f"Welcome, {session['username']}!", "success")
        return redirect(url_for("dashboard"))
    else:
        app.logger.error(f"MSAL Error Result: {result}") # Log the full result
        print(f"DEBUG: MSAL Result: {result}") # Print to stdout for immediate visibility
        error = result.get("error_description") or "Login failed"
        return f"Microsoft login failed: {error}", 400

@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out.", "info")
    return redirect(url_for("login"))

@app.route("/")
def home():
    if "username" not in session:
        return redirect(url_for("login"))
    return redirect(url_for("dashboard")) # Redirect to the protected dashboard

@app.route("/dashboard", methods=["GET", "POST"]) # New route for the actual dashboard
@login_required
def dashboard():
    preview_data = {}
    campaign_progress = {
        'processed': 0, 'total': 0, 'percent': 0
    }

    # Check for the flag set upon login to show the welcome banner.
    just_logged_in = 'just_logged_in' in session
    if just_logged_in:
        session.pop('just_logged_in', None) # Consume the flag so it doesn't show on refresh

    filename = session.get("uploaded_file")

    if request.method == "POST":
        file = request.files.get("file") # This POST is for file upload on dashboard
        if not file or file.filename == "":
            flash("No file selected", "warning")
            return redirect(url_for("dashboard"))

        if file.filename.lower().endswith('.pdf'):
            flash("PDF files are not supported for data campaigns. Please upload a CSV or XLSX file.", "danger")
            return redirect(url_for("dashboard"))

        if file and allowed_file(file.filename):
            # A new file upload is treated as the start of a new campaign.
            # We will clear all previous campaign data to avoid mixing results.

            # First, save the new file
            filename = secure_filename(file.filename)
            filepath = Path(app.config["UPLOAD_FOLDER"]) / filename
            file.save(filepath)
            session["uploaded_file"] = filename
            app.logger.info(f"User '{session.get('username')}' uploaded new file: {filename}")

            try:
                # Pause the scheduler to safely reset state
                if scheduler.state == STATE_RUNNING:
                    scheduler.pause()
                    app.logger.info("Scheduler paused for data reset.")

                # Clear the ContactStatus table to remove old campaign data
                num_deleted = db.session.query(ContactStatus).delete()
                db.session.commit()
                app.logger.info(f"Cleared {num_deleted} records from ContactStatus table due to new file upload.")

                # Delete the scheduler progress file to force a restart from the beginning of the new file
                if JOB_PROGRESS_FILE.exists():
                    JOB_PROGRESS_FILE.unlink()
                    app.logger.info("Deleted scheduler progress file.")

                # Delete the campaign completion flag to allow for new notifications
                if CAMPAIGN_COMPLETE_FLAG.exists():
                    CAMPAIGN_COMPLETE_FLAG.unlink()
                    app.logger.info("Deleted campaign completion flag for new campaign.")

                flash(f"File '{filename}' uploaded. All previous campaign data has been cleared.", "success")
            except Exception as e:
                db.session.rollback()
                app.logger.error(f"Error clearing old campaign data on new file upload: {e}")
                flash(f"File uploaded, but failed to clear old campaign data: {str(e)}", "danger")

            return redirect(url_for("dashboard"))
        else:
            flash("Invalid file type. Please upload a CSV or XLSX file.", "danger")
            return redirect(url_for("dashboard"))

    if filename: # This part is for displaying the preview data
        filepath = Path(app.config["UPLOAD_FOLDER"]) / filename
        if filepath.exists():
            try:
                df = pd.read_csv(filepath) if filename.endswith(".csv") else pd.read_excel(filepath)
                df.columns = [str(c).strip().replace(" ", "_").lower() for c in df.columns]

                # Fetch all current statuses from the database for efficient lookup
                statuses = {contact.email: contact.status for contact in ContactStatus.query.all()}

                pending_contacts = []
                processed_contacts = []
                all_contacts = []
                all_columns = list(df.columns)

                for i, row in df.iterrows():
                    row_dict = row.to_dict()
                    contact_data = {"index": i + 1}
                    for col in all_columns:
                        contact_data[col] = row_dict.get(col) if pd.notna(row_dict.get(col)) else ""

                    # Add status to the contact data
                    email = str(contact_data.get('email_id', '')).strip()
                    status = statuses.get(email, 'Pending')
                    contact_data['status'] = status

                    if status in ['Pending']:
                        pending_contacts.append(contact_data)
                    else:
                        processed_contacts.append(contact_data)
                    all_contacts.append(contact_data)

                # The template now expects a single list of all contacts.
                preview_data = {'all_contacts': all_contacts}
                
                # --- Calculate campaign progress ---
                total = len(pending_contacts) + len(processed_contacts)
                if total > 0:
                    processed = len(processed_contacts)
                    campaign_progress = {
                        'processed': processed,
                        'total': total,
                        'percent': (processed / total * 100) if total > 0 else 0
                    }

            except Exception as e:
                flash(f"Error reading file: {e}", "danger")
        else:
            flash(f"Uploaded file '{filename}' not found. Please upload again.", "warning")
            session.pop("uploaded_file", None)

    return render_template("dashboard.html", preview_data=preview_data,
                           username=session.get("username"), scheduler_status=get_scheduler_status(),
                           campaign_progress=campaign_progress,
                           campaign_region=session.get('campaign_region', 'us'),
                           just_logged_in=just_logged_in,
                           bcc_email=app.config['BCC_EMAIL'])

@app.route("/campaign/set_region", methods=["POST"])
@login_required
def set_campaign_region():
    region = request.form.get('region', 'us')
    session['campaign_region'] = region

    # Save the region to the persistent settings file for the scheduler
    settings = get_settings()
    settings['campaign_region'] = region
    save_settings(settings)

    # Set a session flag to skip the page loader for a smoother UI experience
    session['skip_loader'] = True
    flash(f"Campaign template set to {session['campaign_region'].upper()}.", "info")
    return redirect(url_for('dashboard'))

@app.route("/send_emails_now", methods=["POST"])
@login_required
def send_emails_now():
    """Streams the progress of sending all emails back to the client."""
    filename = session.get("uploaded_file")
    if not filename:
        return jsonify({"error": "Please upload a data file first."}), 400

    filepath = Path(app.config["UPLOAD_FOLDER"]) / filename
    if not filepath.exists():
        return jsonify({"error": "Uploaded file not found."}), 404

    region = session.get('campaign_region', 'us')
    if region == 'ca':
        template_to_use = TEMPLATE_FILE_CA
    elif region == 'us':
        template_to_use = TEMPLATE_FILE_US
    else: # Fallback for old template
        template_to_use = TEMPLATE_FILE

    if not template_to_use.exists():
        return jsonify({"error": "Email template not found."}), 400

    def generate_progress_with_context():
        try:
            app.logger.info(f"Starting bulk send for region: '{region}'. Using template: {template_to_use}")

            template_str = template_to_use.read_text(encoding="utf-8")
            from jinja2 import Template
            email_template = Template(template_str)
            
            settings = get_settings()
            bcc_recipients = [app.config['BCC_EMAIL']] if settings.get("bcc_enabled") else None
            app.logger.info(f"Bulk send stream started. BCC enabled: {settings.get('bcc_enabled')}")

            df = pd.read_csv(filepath) if filename.endswith(".csv") else pd.read_excel(filepath)
            df.columns = [str(c).strip().replace(" ", "_").lower() for c in df.columns]
            
            total = len(df)
            yield json.dumps({"type": "start", "total": total}) + "\n"

            for i, row in df.iterrows():
                norm_row = row.to_dict()
                raw_email = norm_row.get("email_id")
                email = str(raw_email).strip().strip('|').strip() if pd.notna(raw_email) else None
                name = norm_row.get("name","")
                company = norm_row.get("company_name","")
                
                progress_data = {"type": "progress", "current": i + 1, "total": total, "email": email}

                if not email:
                    progress_data["status"] = "skipped"
                    progress_data["error"] = "Missing email"
                    yield json.dumps(progress_data) + "\n"
                    continue

                access_token = get_access_token()
                if not access_token:
                    yield json.dumps({"type": "error", "message": "Session expired. Please log in again."}) + "\n"
                    return

                html_body = email_template.render(norm_row)
                subject = "Introduction to GridsGlobal Steel Detailing LLC - Engineering services"
                send_result, error_msg = send_email_with_graph(access_token, email, subject, html_body, bcc_recipients=bcc_recipients)

                contact_status = ContactStatus.query.filter_by(email=email).first()
                if not contact_status:
                    contact_status = ContactStatus(email=email, name=name, company_name=company)
                    db.session.add(contact_status)

                if send_result:
                    contact_status.status = 'Sent'
                    contact_status.internet_message_id = send_result.get("internetMessageId")
                    contact_status.conversation_id = send_result.get("conversationId")
                    contact_status.error_details = None
                    log_email_attempt(email, name, company, subject, 'Sent', html_body, internet_message_id=send_result.get("internetMessageId"), conversation_id=send_result.get("conversationId"))
                    progress_data["status"] = "success"
                else:
                    contact_status.status = 'Failed'
                    contact_status.error_details = error_msg
                    log_email_attempt(email, name, company, subject, 'Failed', html_body, error_msg)
                    progress_data["status"] = "failed"
                    progress_data["error"] = error_msg or "Unknown error"
                
                db.session.commit()
                yield json.dumps(progress_data) + "\n"
            
            yield json.dumps({"type": "done", "message": "All emails processed."}) + "\n"

        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Error during streaming bulk send: {e}", exc_info=True)
            yield json.dumps({"type": "error", "message": str(e)}) + "\n"

    def generate_progress():
        with app.app_context():
            yield from generate_progress_with_context()

    return Response(generate_progress(), mimetype='application/x-ndjson')

@app.route("/send_single_email", methods=["POST"])
@login_required
def send_single_email():
    data = request.get_json()
    row_index = data.get("index")

    if row_index is None:
        return jsonify({"success": False, "error": "Row index not provided."}), 400

    filename = session.get("uploaded_file")
    if not filename:
        return jsonify({"success": False, "error": "No data file uploaded in this session."}), 400

    filepath = Path(app.config["UPLOAD_FOLDER"]) / filename
    if not filepath.exists():
        return jsonify({"success": False, "error": "Uploaded file not found."}), 404

    region = session.get('campaign_region', 'us')
    if region == 'ca':
        template_to_use = TEMPLATE_FILE_CA
    elif region == 'us':
        template_to_use = TEMPLATE_FILE_US
    else: # Fallback for old template
        template_to_use = TEMPLATE_FILE

    if not template_to_use.exists():
        return jsonify({"success": False, "error": "Email template not found."}), 400

    try:
        template_str = template_to_use.read_text(encoding="utf-8")
        from jinja2 import Template
        email_template = Template(template_str)

        df = pd.read_csv(filepath) if filename.endswith(".csv") else pd.read_excel(filepath)

        actual_index = row_index - 1
        if not (0 <= actual_index < len(df)):
            return jsonify({"success": False, "error": "Invalid row index."}), 400

        row = df.iloc[actual_index]
        norm_row = normalize_keys(row.to_dict())

        raw_email = norm_row.get("email_id")
        # Clean the email address to remove leading/trailing whitespace and extra characters like '|'
        email = str(raw_email).strip().strip('|').strip() if pd.notna(raw_email) else None
        name = norm_row.get("name", "")
        company = norm_row.get("company_name", "")

        if not email:
            return jsonify({"success": False, "error": "Email address is missing or invalid for this contact."}), 400

        access_token = get_access_token()
        if not access_token:
            return jsonify({"success": False, "error": "Session expired. Please log in again."}), 401

        settings = get_settings()
        bcc_recipients = [app.config['BCC_EMAIL']] if settings.get("bcc_enabled") else None

        html_body = email_template.render(norm_row)
        subject = "Introduction to GridsGlobal Steel Detailing LLC - Engineering services"
        send_result, error_msg = send_email_with_graph(access_token, email, subject, html_body, bcc_recipients=bcc_recipients)

        contact_status = ContactStatus.query.filter_by(email=email).first()
        if not contact_status:
            contact_status = ContactStatus(email=email, name=name, company_name=company)
            db.session.add(contact_status)

        if send_result:
            contact_status.status = 'Sent'
            contact_status.internet_message_id = send_result.get("internetMessageId")
            contact_status.conversation_id = send_result.get("conversationId")
            contact_status.error_details = None
            # --- Start Follow-up Sequence ---
            contact_status.sequence_status = 'active'
            contact_status.sequence_step = 0
            # Hardcoded for now; could be a setting later
            contact_status.next_follow_up_at = datetime.now(timezone.utc) + timedelta(days=3)
            log_email_attempt(email, name, company, subject, 'Sent', html_body, internet_message_id=send_result.get("internetMessageId"), conversation_id=send_result.get("conversationId"))
            db.session.commit()
            app.logger.info(f"User '{session.get('username')}' manually sent email to {email}.")
            return jsonify({"success": True, "message": f"Email sent to {email}."})
        else:
            contact_status.status = 'Failed'
            contact_status.error_details = error_msg
            log_email_attempt(email, name, company, subject, 'Failed', html_body, error_msg)
            db.session.commit()
            app.logger.warning(f"User '{session.get('username')}' failed to manually send email to {email}: {error_msg}")
            return jsonify({"success": False, "error": f"Failed to send email: {error_msg}"})

    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error sending single email for row {row_index}: {e}")
        return jsonify({"success": False, "error": f"An internal error occurred: {str(e)}"}), 500

@app.route("/api/send_single_follow_up", methods=["POST"])
@login_required
def api_send_single_follow_up():
    data = request.get_json()
    row_index = data.get("index")

    if row_index is None:
        return jsonify({"success": False, "error": "Row index not provided."}), 400

    filename = session.get("follow_up_uploaded_file")
    if not filename:
        return jsonify({"success": False, "error": "No follow-up file uploaded."}), 400

    follow_up_upload_folder = Path(app.config["UPLOAD_FOLDER"]) / "follow_ups"
    filepath = follow_up_upload_folder / filename
    if not filepath.exists():
        return jsonify({"success": False, "error": "Uploaded follow-up file not found."}), 404

    if not FOLLOW_UP_TEMPLATE_FILE.exists():
        return jsonify({"success": False, "error": "Follow-up email template not found."}), 400

    try:
        from jinja2 import Template
        template_content = FOLLOW_UP_TEMPLATE_FILE.read_text(encoding="utf-8")
        parts = template_content.split('\n\n', 1)
        subject_template_str = "Follow-up: {{ company_name }}"
        body_template_str = template_content
        if len(parts) == 2 and parts[0].lower().startswith('subject:'):
            subject_template_str = parts[0][len('subject:'):].strip()
            body_template_str = parts[1]

        subject_template = Template(subject_template_str)
        body_template = Template(body_template_str)

        df = pd.read_csv(filepath) if filename.endswith(".csv") else pd.read_excel(filepath)
        actual_index = row_index - 1
        if not (0 <= actual_index < len(df)):
            return jsonify({"success": False, "error": "Invalid row index."}), 400

        row = df.iloc[actual_index]
        norm_row = normalize_keys(row.to_dict())
        email = str(norm_row.get("email_id", "")).strip()
        name = norm_row.get("name", "")
        company = norm_row.get("company_name", "")

        if not email:
            return jsonify({"success": False, "error": "Email address is missing for this contact."}), 400

        access_token = get_access_token()
        if not access_token:
            return jsonify({"success": False, "error": "Session expired. Please log in again."}), 401

        settings = get_settings()
        bcc_recipients = [app.config['BCC_EMAIL']] if settings.get("bcc_enabled") else None
        subject = subject_template.render(norm_row)
        html_body = body_template.render(norm_row)

        contact_status = ContactStatus.query.filter_by(email=email).first()
        conversation_id_to_use = contact_status.conversation_id if contact_status else None

        send_result, error_msg = send_email_with_graph(access_token, email, subject, html_body, bcc_recipients=bcc_recipients, conversation_id=conversation_id_to_use)

        if send_result:
            log_email_attempt(email, name, company, subject, 'Follow-up Sent', html_body, internet_message_id=send_result.get("internetMessageId"), conversation_id=send_result.get("conversationId"))
            return jsonify({"success": True, "message": f"Follow-up sent to {email}."})
        else:
            log_email_attempt(email, name, company, subject, 'Follow-up Failed', html_body, error_msg)
            return jsonify({"success": False, "error": f"Failed to send follow-up: {error_msg}"})
    except Exception as e:
        app.logger.error(f"Error sending single follow-up for row {row_index}: {e}", exc_info=True)
        return jsonify({"success": False, "error": f"An internal error occurred: {str(e)}"}), 500

@app.route("/send_mono_mail", methods=["POST"])
@login_required
def send_mono_mail():
    name = request.form.get("name")
    company_name = request.form.get("company_name")
    raw_email = request.form.get("email_id")
    email = str(raw_email).strip().strip('|').strip() if raw_email else None

    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest' # Check if it's an AJAX request

    if not all([name, email]):
        if is_ajax: # If AJAX, return JSON error
            return jsonify({"success": False, "error": "Name and Email are required."}), 400
        flash("Name and Email are required for Mono Mail.", "warning")
        return redirect(url_for("dashboard"))

    region = session.get('campaign_region', 'us')
    if region == 'ca':
        template_to_use = TEMPLATE_FILE_CA
    elif region == 'us':
        template_to_use = TEMPLATE_FILE_US
    else: # Fallback for old template
        template_to_use = TEMPLATE_FILE

    if not template_to_use.exists():
        if is_ajax: # If AJAX, return JSON error
            return jsonify({"success": False, "error": "Email template not found."}), 500
        flash("Email template not found. Please create one on the Template page.", "danger")
        return redirect(url_for("dashboard"))

    access_token = get_access_token()
    if not access_token:
        if is_ajax: # If AJAX, return JSON error
            return jsonify({"success": False, "error": "Session expired. Please log in again."}), 401
        flash("Session expired. Please log in again.", "warning")
        return redirect(url_for("login"))

    try:
        template_str = template_to_use.read_text(encoding="utf-8")
        from jinja2 import Template
        email_template = Template(template_str)

        render_data = {
            "name": name,
            "company_name": company_name,
            "email_id": email
        }
        norm_row = normalize_keys(render_data)

        settings = get_settings()
        bcc_recipients = [app.config['BCC_EMAIL']] if settings.get("bcc_enabled") else None

        html_body = email_template.render(norm_row)
        subject = "Introduction to GridsGlobal Steel Detailing LLC - Engineering services"
        send_result, error_msg = send_email_with_graph(access_token, email, subject, html_body, bcc_recipients=bcc_recipients)

        contact_status = ContactStatus.query.filter_by(email=email).first()
        if not contact_status:
            contact_status = ContactStatus(email=email, name=name, company_name=company_name)
            db.session.add(contact_status)

        if send_result:
            contact_status.status = 'Mono Sent'
            contact_status.internet_message_id = send_result.get("internetMessageId")
            contact_status.conversation_id = send_result.get("conversationId")
            contact_status.error_details = None
            # --- Start Follow-up Sequence ---
            contact_status.sequence_status = 'active'
            contact_status.sequence_step = 0
            # Hardcoded for now
            contact_status.next_follow_up_at = datetime.now(timezone.utc) + timedelta(days=3)
            log_email_attempt(email, name, company_name, subject, 'Mono Sent', html_body, internet_message_id=send_result.get("internetMessageId"), conversation_id=send_result.get("conversationId"))
            db.session.commit()
            app.logger.info(f"User '{session.get('username')}' sent a mono mail to {email}.")
            if is_ajax: # If AJAX, return JSON success
                return jsonify({"success": True, "message": f"Mono mail sent successfully to {email}."})
            flash(f"Mono mail sent successfully to {email}.", "success")
        else:
            contact_status.status = 'Mono Failed'
            contact_status.error_details = error_msg
            log_email_attempt(email, name, company_name, subject, 'Mono Failed', html_body, error_msg)
            db.session.commit()
            app.logger.warning(f"User '{session.get('username')}' failed to send mono mail to {email}: {error_msg}")
            if is_ajax: # If AJAX, return JSON error
                return jsonify({"success": False, "error": f"Failed to send mono mail: {error_msg}"})
            flash(f"Failed to send mono mail to {email}: {error_msg}", "danger")

    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error sending mono mail to {email}: {e}")
        if is_ajax: # If AJAX, return JSON error
            return jsonify({"success": False, "error": f"An internal error occurred: {str(e)}"}), 500
        flash(f"An internal error occurred while sending the mono mail: {str(e)}", "danger")

    return redirect(url_for("dashboard"))

@app.route("/send_manual_follow_up_mono", methods=["POST"])
@login_required
def send_manual_follow_up_mono():
    name = request.form.get("name")
    company_name = request.form.get("company_name")
    raw_email = request.form.get("email_id")
    email = str(raw_email).strip().strip('|').strip() if raw_email else None

    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    if not all([name, email]):
        if is_ajax:
            return jsonify({"success": False, "error": "Name and Email are required."}), 400
        flash("Name and Email are required for Mono Mail.", "warning")
        return redirect(url_for("follow_up_dashboard"))

    if not FOLLOW_UP_TEMPLATE_FILE.exists():
        if is_ajax:
            return jsonify({"success": False, "error": "Follow-up email template not found."}), 500
        flash("Follow-up email template not found. Please create one.", "danger")
        return redirect(url_for("follow_up_dashboard"))

    access_token = get_access_token()
    if not access_token:
        if is_ajax:
            return jsonify({"success": False, "error": "Session expired. Please log in again."}), 401
        flash("Session expired. Please log in again.", "warning")
        return redirect(url_for("login"))

    try:
        from jinja2 import Template
        template_content = FOLLOW_UP_TEMPLATE_FILE.read_text(encoding="utf-8")
        
        # Parse subject and body from template
        parts = template_content.split('\n\n', 1)
        subject_template_str = "Follow-up regarding {{ company_name }}" # Default subject
        body_template_str = template_content
        if len(parts) == 2 and parts[0].lower().startswith('subject:'):
            subject_template_str = parts[0][len('subject:'):].strip()
            body_template_str = parts[1]

        subject_template = Template(subject_template_str)
        body_template = Template(body_template_str)

        render_data = {"name": name, "company_name": company_name, "email_id": email}
        norm_row = normalize_keys(render_data)

        settings = get_settings()
        bcc_recipients = [app.config['BCC_EMAIL']] if settings.get("bcc_enabled") else None

        subject = subject_template.render(norm_row)
        html_body = body_template.render(norm_row)
        
        # Find contact *before* sending to get existing conversation ID
        contact_status = ContactStatus.query.filter_by(email=email).first()
        conversation_id_to_use = contact_status.conversation_id if contact_status else None

        send_result, error_msg = send_email_with_graph(
            access_token, email, subject, html_body, 
            bcc_recipients=bcc_recipients,
            conversation_id=conversation_id_to_use
        )

        if not contact_status:
            contact_status = ContactStatus(email=email, name=name, company_name=company_name)
            db.session.add(contact_status)

        if send_result:
            status_text = 'Follow-up Mono Sent'
            contact_status.status = status_text
            contact_status.internet_message_id = send_result.get("internetMessageId")
            contact_status.conversation_id = send_result.get("conversationId")
            contact_status.error_details = None
            log_email_attempt(email, name, company_name, subject, status_text, html_body, internet_message_id=send_result.get("internetMessageId"), conversation_id=send_result.get("conversationId"))
            db.session.commit()
            app.logger.info(f"User '{session.get('username')}' sent a follow-up mono mail to {email}.")
            if is_ajax:
                return jsonify({"success": True, "message": f"Follow-up mono mail sent successfully to {email}."})
            flash(f"Follow-up mono mail sent successfully to {email}.", "success")
        else:
            status_text = 'Follow-up Mono Failed'
            contact_status.status = status_text
            contact_status.error_details = error_msg
            log_email_attempt(email, name, company_name, subject, status_text, html_body, error_msg)
            db.session.commit()
            app.logger.warning(f"User '{session.get('username')}' failed to send follow-up mono mail to {email}: {error_msg}")
            if is_ajax:
                return jsonify({"success": False, "error": f"Failed to send follow-up mono mail: {error_msg}"})
            flash(f"Failed to send follow-up mono mail to {email}: {error_msg}", "danger")
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error sending follow-up mono mail to {email}: {e}", exc_info=True)
        if is_ajax:
            return jsonify({"success": False, "error": f"An internal error occurred: {str(e)}"}), 500
        flash(f"An internal error occurred while sending the follow-up mono mail: {str(e)}", "danger")
    return redirect(url_for("follow_up_dashboard"))

@app.route("/send_manual_follow_ups", methods=["POST"])
@login_required
def send_manual_follow_ups():
    """
    Sends follow-up emails to all contacts from the uploaded list and streams
    the progress back to the client using newline-delimited JSON.
    """
    filename = session.get("follow_up_uploaded_file")
    if not filename:
        return jsonify({"error": "Please upload a follow-up data file first."}), 400

    follow_up_upload_folder = Path(app.config["UPLOAD_FOLDER"]) / "follow_ups"
    filepath = follow_up_upload_folder / filename
    if not filepath.exists():
        return jsonify({"error": "Uploaded follow-up file not found."}), 404

    if not FOLLOW_UP_TEMPLATE_FILE.exists():
        return jsonify({"error": "Follow-up email template not found."}), 400

    def generate_progress_with_context():
        try:
            from jinja2 import Template
            template_content = FOLLOW_UP_TEMPLATE_FILE.read_text(encoding="utf-8")
            
            parts = template_content.split('\n\n', 1)
            subject_template_str = "Follow-up regarding {{ company_name }}"
            body_template_str = template_content
            if len(parts) == 2 and parts[0].lower().startswith('subject:'):
                subject_template_str = parts[0][len('subject:'):].strip()
                body_template_str = parts[1]

            subject_template = Template(subject_template_str)
            body_template = Template(body_template_str)
            
            settings = get_settings()
            bcc_recipients = [app.config['BCC_EMAIL']] if settings.get("bcc_enabled") else None
            app.logger.info(f"Manual follow-up 'send all' stream started. BCC enabled: {settings.get('bcc_enabled')}")

            df = pd.read_csv(filepath) if filename.endswith(".csv") else pd.read_excel(filepath)
            df.columns = [str(c).strip().replace(" ", "_").lower() for c in df.columns]
            
            total = len(df)
            yield json.dumps({"type": "start", "total": total}) + "\n"

            for i, row in df.iterrows():
                norm_row = row.to_dict()
                raw_email = norm_row.get("email_id")
                email = str(raw_email).strip().strip('|').strip() if pd.notna(raw_email) else None
                name = norm_row.get("name","")
                company = norm_row.get("company_name","")
                
                progress_data = {"type": "progress", "current": i + 1, "total": total, "email": email}

                if not email:
                    progress_data["status"] = "skipped"
                    progress_data["error"] = "Missing email"
                    yield json.dumps(progress_data) + "\n"
                    continue

                access_token = get_access_token()
                if not access_token:
                    yield json.dumps({"type": "error", "message": "Session expired. Please log in again."}) + "\n"
                    return

                subject = subject_template.render(norm_row)
                html_body = body_template.render(norm_row)

                contact_status = ContactStatus.query.filter_by(email=email).first()
                conversation_id_to_use = contact_status.conversation_id if contact_status else None

                send_result, error_msg = send_email_with_graph(
                    access_token, email, subject, html_body,
                    bcc_recipients=bcc_recipients,
                    conversation_id=conversation_id_to_use
                )

                if not contact_status:
                    contact_status = ContactStatus(email=email, name=name, company_name=company)
                    db.session.add(contact_status)

                if send_result:
                    status_text = 'Follow-up Sent'
                    contact_status.status = status_text
                    contact_status.internet_message_id = send_result.get("internetMessageId")
                    contact_status.conversation_id = send_result.get("conversationId")
                    log_email_attempt(email, name, company, subject, status_text, html_body, internet_message_id=send_result.get("internetMessageId"), conversation_id=send_result.get("conversationId"))
                    progress_data["status"] = "success"
                else:
                    status_text = 'Follow-up Failed'
                    contact_status.status = status_text
                    contact_status.error_details = error_msg
                    log_email_attempt(email, name, company, subject, status_text, html_body, error_msg)
                    progress_data["status"] = "failed"
                    progress_data["error"] = error_msg or "Unknown error"
                
                db.session.commit()
                yield json.dumps(progress_data) + "\n"
            
            yield json.dumps({"type": "done", "message": "All follow-ups processed."}) + "\n"

        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Error during streaming manual follow-ups: {e}", exc_info=True)
            yield json.dumps({"type": "error", "message": str(e)}) + "\n"

    def generate_progress():
        with app.app_context():
            yield from generate_progress_with_context()

    return Response(generate_progress(), mimetype='application/x-ndjson')

@app.route("/follow-up-campaign/start", methods=["POST"])
@login_required
def start_follow_up_campaign():
    """Resumes the scheduled follow-up job."""
    try:
        # The main scheduler must be running for any job to run.
        if scheduler.state == STATE_PAUSED:
            scheduler.resume()
            flash("Main scheduler was paused and has been resumed.", "info")
            app.logger.info("Main scheduler resumed via follow-up campaign start.")

        scheduler.resume_job('send_scheduled_follow_up_job')
        flash("Scheduled follow-up campaign has been started.", "success")
        app.logger.info("User started 'send_scheduled_follow_up_job'.")
    except Exception as e:
        app.logger.error(f"Error starting follow-up campaign: {e}")
        flash(f"Error starting follow-up campaign: {e}", "danger")
    return redirect(url_for('follow_up_dashboard'))

@app.route("/follow-up-campaign/stop", methods=["POST"])
@login_required
def stop_follow_up_campaign():
    """Pauses the scheduled follow-up job."""
    try:
        scheduler.pause_job('send_scheduled_follow_up_job')
        flash("Scheduled follow-up campaign has been paused.", "info")
        app.logger.info("User paused 'send_scheduled_follow_up_job'.")
    except Exception as e:
        app.logger.error(f"Error pausing follow-up campaign: {e}")
        flash(f"Error stopping follow-up campaign: {e}", "danger")
    return redirect(url_for('follow_up_dashboard'))

@app.route("/follow-up-campaign/clear", methods=["POST"])
@login_required
def clear_follow_up_campaign():
    """Clears the uploaded follow-up file and its progress."""
    try:
        # Pause the specific job to prevent race conditions
        try:
            scheduler.pause_job('send_scheduled_follow_up_job')
            app.logger.info("Follow-up scheduler job paused for campaign clear.")
        except Exception:
            # It might already be paused or not exist, which is fine.
            pass

        # Delete the physical uploaded file
        filename = session.pop("follow_up_uploaded_file", None)
        if filename:
            follow_up_upload_folder = Path(app.config["UPLOAD_FOLDER"]) / "follow_ups"
            filepath = follow_up_upload_folder / filename
            if filepath.exists():
                filepath.unlink()
                app.logger.info(f"Deleted uploaded follow-up file: {filename}")

        # Delete the scheduler progress file for the follow-up job
        if FOLLOW_UP_JOB_PROGRESS_FILE.exists():
            FOLLOW_UP_JOB_PROGRESS_FILE.unlink()
            app.logger.info("Deleted follow-up scheduler progress file.")

        flash("The follow-up campaign data and uploaded file have been cleared.", "success")
    except Exception as e:
        app.logger.error(f"Error clearing follow-up campaign data: {e}")
        flash(f"An error occurred while clearing follow-up data: {str(e)}", "danger")
    return redirect(url_for('follow_up_dashboard'))

@app.route("/follow-up-campaign/send_remaining_now", methods=["POST"])
@login_required
def follow_up_campaign_send_remaining_now():
    """
    Stops the scheduled follow-up campaign and immediately sends emails to all
    remaining contacts in the follow-up list.
    """
    def generate_progress():
        try:
            # 1. Pause the scheduler job
            scheduler.pause_job('send_scheduled_follow_up_job')
            app.logger.info("Scheduler job 'send_scheduled_follow_up_job' paused for 'send remaining' operation.")

            # 2. Get file and template
            filename = session.get("follow_up_uploaded_file")
            if not filename:
                yield json.dumps({"type": "error", "message": "No follow-up data file found in session."}) + "\n"
                return

            follow_up_upload_folder = Path(app.config["UPLOAD_FOLDER"]) / "follow_ups"
            filepath = follow_up_upload_folder / filename
            if not filepath.exists() or not FOLLOW_UP_TEMPLATE_FILE.exists():
                yield json.dumps({"type": "error", "message": "Follow-up data file or template not found."}) + "\n"
                return

            # 3. Determine starting point
            next_row = 0
            if FOLLOW_UP_JOB_PROGRESS_FILE.exists():
                try:
                    progress = json.loads(FOLLOW_UP_JOB_PROGRESS_FILE.read_text(encoding='utf-8'))
                    if progress.get("file_path") == str(filepath):
                        next_row = progress.get("next_row", 0)
                except (json.JSONDecodeError, IOError):
                    app.logger.warning("Could not read follow-up job progress file. Starting from the beginning.")

            # 4. Load data and slice for remaining contacts
            df = pd.read_csv(filepath) if filename.endswith(".csv") else pd.read_excel(filepath)
            df.columns = [str(c).strip().replace(" ", "_").lower() for c in df.columns]
            remaining_df = df.iloc[next_row:]

            if remaining_df.empty:
                yield json.dumps({"type": "done", "message": "No remaining contacts to send."}) + "\n"
                return

            total = len(remaining_df)
            yield json.dumps({"type": "start", "total": total}) + "\n"

            # 5. Loop and send emails (similar to send_manual_follow_ups)
            from jinja2 import Template
            template_content = FOLLOW_UP_TEMPLATE_FILE.read_text(encoding="utf-8")
            parts = template_content.split('\n\n', 1)
            subject_template_str = "Follow-up regarding {{ company_name }}"
            body_template_str = template_content
            if len(parts) == 2 and parts[0].lower().startswith('subject:'):
                subject_template_str = parts[0][len('subject:'):].strip()
                body_template_str = parts[1]
            subject_template = Template(subject_template_str)
            body_template = Template(body_template_str)
            settings = get_settings()
            bcc_recipients = [app.config['BCC_EMAIL']] if settings.get("bcc_enabled") else None

            for i, row in remaining_df.iterrows():
                norm_row = row.to_dict()
                raw_email = norm_row.get("email_id")
                email = str(raw_email).strip().strip('|').strip() if pd.notna(raw_email) else None
                progress_data = {"type": "progress", "current": i - next_row + 1, "total": total, "email": email}

                if not email:
                    progress_data["status"] = "skipped"; progress_data["error"] = "Missing email"
                    yield json.dumps(progress_data) + "\n"; continue

                access_token = get_access_token()
                if not access_token:
                    yield json.dumps({"type": "error", "message": "Session expired. Please log in again."}) + "\n"; return

                subject = subject_template.render(norm_row)
                html_body = body_template.render(norm_row)
                contact_status = ContactStatus.query.filter_by(email=email).first()
                conversation_id_to_use = contact_status.conversation_id if contact_status else None
                send_result, error_msg = send_email_with_graph(access_token, email, subject, html_body, bcc_recipients=bcc_recipients, conversation_id=conversation_id_to_use)

                if send_result: progress_data["status"] = "success"
                else: progress_data["status"] = "failed"; progress_data["error"] = error_msg or "Unknown error"
                yield json.dumps(progress_data) + "\n"

            # 6. Mark campaign as complete
            with open(FOLLOW_UP_JOB_PROGRESS_FILE, 'w', encoding='utf-8') as f:
                json.dump({"file_path": str(filepath), "next_row": len(df)}, f)
            yield json.dumps({"type": "done", "message": "All remaining follow-ups processed. Campaign complete."}) + "\n"
        except Exception as e:
            app.logger.error(f"Error during 'send remaining follow-ups' operation: {e}", exc_info=True)
            yield json.dumps({"type": "error", "message": str(e)}) + "\n"
    return Response(generate_progress(), mimetype='application/x-ndjson')

@app.route("/campaign/send_remaining_now", methods=["POST"])
@login_required
def send_remaining_now():
    """
    Stops the scheduled campaign and immediately sends emails to all remaining
    contacts in the list. Streams progress back to the client.
    """
    def generate_progress():
        try:
            # 1. Pause the scheduler job
            scheduler.pause_job('send_email_job')
            app.logger.info("Scheduler job 'send_email_job' paused for 'send remaining' operation.")
            
            # 2. Get file and template
            filename = session.get("uploaded_file")
            if not filename:
                yield json.dumps({"type": "error", "message": "No data file found in session."}) + "\n"
                return

            filepath = Path(app.config["UPLOAD_FOLDER"]) / filename
            if not filepath.exists() or not TEMPLATE_FILE.exists():
                yield json.dumps({"type": "error", "message": "Data file or template not found."}) + "\n"
                return

            # 3. Determine starting point
            next_row = 0
            if JOB_PROGRESS_FILE.exists():
                try:
                    progress = json.loads(JOB_PROGRESS_FILE.read_text(encoding='utf-8'))
                    if progress.get("file_path") == str(filepath):
                        next_row = progress.get("next_row", 0)
                except (json.JSONDecodeError, IOError):
                    app.logger.warning("Could not read job progress file. Starting from the beginning.")

            # 4. Load data and slice for remaining contacts
            df = pd.read_csv(filepath) if filename.endswith(".csv") else pd.read_excel(filepath)
            df.columns = [str(c).strip().replace(" ", "_").lower() for c in df.columns]
            remaining_df = df.iloc[next_row:]

            if remaining_df.empty:
                yield json.dumps({"type": "done", "message": "No remaining contacts to send."}) + "\n"
                return

            total = len(remaining_df)
            yield json.dumps({"type": "start", "total": total}) + "\n"

            # 5. Loop and send emails (similar to send_emails_now)
            from jinja2 import Template
            template_str = TEMPLATE_FILE.read_text(encoding="utf-8")
            email_template = Template(template_str)
            settings = get_settings()
            bcc_recipients = [app.config['BCC_EMAIL']] if settings.get("bcc_enabled") else None

            for i, row in remaining_df.iterrows():
                norm_row = row.to_dict()
                raw_email = norm_row.get("email_id")
                email = str(raw_email).strip().strip('|').strip() if pd.notna(raw_email) else None
                name = norm_row.get("name","")
                company = norm_row.get("company_name","")
                
                progress_data = {"type": "progress", "current": i - next_row + 1, "total": total, "email": email}

                if not email:
                    progress_data["status"] = "skipped"
                    progress_data["error"] = "Missing email"
                    yield json.dumps(progress_data) + "\n"
                    continue

                access_token = get_access_token()
                if not access_token:
                    yield json.dumps({"type": "error", "message": "Session expired. Please log in again."}) + "\n"
                    return

                html_body = email_template.render(norm_row)
                subject = "Introduction to GridsGlobal Steel Detailing LLC - Engineering services"
                send_result, error_msg = send_email_with_graph(access_token, email, subject, html_body, bcc_recipients=bcc_recipients)

                contact_status = ContactStatus.query.filter_by(email=email).first()
                if not contact_status:
                    contact_status = ContactStatus(email=email, name=name, company_name=company)
                    db.session.add(contact_status)

                if send_result:
                    contact_status.status = 'Sent'
                    contact_status.internet_message_id = send_result.get("internetMessageId")
                    contact_status.conversation_id = send_result.get("conversationId")
                    log_email_attempt(email, name, company, subject, 'Sent', html_body, internet_message_id=send_result.get("internetMessageId"), conversation_id=send_result.get("conversationId"))
                    progress_data["status"] = "success"
                else:
                    contact_status.status = 'Failed'
                    contact_status.error_details = error_msg
                    log_email_attempt(email, name, company, subject, 'Failed', html_body, error_msg)
                    progress_data["status"] = "failed"
                    progress_data["error"] = error_msg or "Unknown error"
                
                db.session.commit()
                yield json.dumps(progress_data) + "\n"

            # 6. Mark campaign as complete
            try:
                with open(JOB_PROGRESS_FILE, 'w', encoding='utf-8') as f:
                    json.dump({"file_path": str(filepath), "next_row": len(df)}, f)
                app.logger.info("Main campaign marked as complete after 'send remaining' operation.")
            except IOError as e:
                app.logger.error(f"Failed to update job progress file after completion: {e}")

            yield json.dumps({"type": "done", "message": "All remaining emails processed. Campaign complete."}) + "\n"

        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Error during 'send remaining' operation: {e}", exc_info=True)
            yield json.dumps({"type": "error", "message": str(e)}) + "\n"

    return Response(generate_progress(), mimetype='application/x-ndjson')

@app.route("/results")
@login_required
def results():
    # Get query parameters for filtering, searching, and pagination
    page = request.args.get('page', 1, type=int)
    active_filter = request.args.get('status', None)
    search_query = request.args.get('q', None)

    # Base query
    query = ContactStatus.query

    # Apply filter
    if active_filter:
        if active_filter == 'Sent':
            query = query.filter(or_(
                ContactStatus.status == 'Sent',
                ContactStatus.status == 'Sent (Scheduled)'
            ))
        elif active_filter == 'Pending':
            query = query.filter(ContactStatus.status == 'Pending')
        elif active_filter == 'Manual':
            query = query.filter(ContactStatus.status.in_(MANUAL_STATUSES))
        else:
            query = query.filter(ContactStatus.status == active_filter)

    # Apply search
    if search_query:
        search_term = f"%{search_query}%"
        query = query.filter(or_(
            ContactStatus.email.ilike(search_term),
            ContactStatus.name.ilike(search_term),
            ContactStatus.company_name.ilike(search_term)
        ))

    # Paginate the results
    pagination = query.order_by(ContactStatus.last_update.desc()).paginate(
        page=page, per_page=20, error_out=False
    )

    # Get overall stats from the persistent EmailLog table for a lifetime view.
    from collections import Counter
    all_log_statuses = [log.status for log in EmailLog.query.with_entities(EmailLog.status).all()]
    counts = Counter(all_log_statuses)

    # Consolidate different types of 'Sent' and 'Failed' statuses from the log
    sent_count = sum(counts.get(s, 0) for s in ['Sent', 'Sent (Scheduled)', 'Mono Sent', 'Follow-up Sent', 'Follow-up Mono Sent', 'Follow-up Sent (Scheduled)'])
    failed_count = sum(counts.get(s, 0) for s in ['Failed', 'Mono Failed', 'Follow-up Failed', 'Follow-up Failed (Scheduled)'])
    # For the filter tabs, the count should come from the ContactStatus table to match what's displayed.
    replied_count = ContactStatus.query.filter_by(status='Replied').count()
    # Get specific mono and follow-up mono counts
    fup_mono_sent_count = counts.get('Follow-up Mono Sent', 0)
    fup_mono_failed_count = counts.get('Follow-up Mono Failed', 0)

    stats = {
        'Sent': sent_count,
        'Replied': replied_count,
        'Failed': failed_count,
        # 'Pending' is a status that only exists for the current campaign, so we get it from ContactStatus.
        'Pending': ContactStatus.query.filter_by(status='Pending').count(),
        'Total': ContactStatus.query.count(), # Total contacts in the *current* campaign.
        'Mono Sent': counts.get('Mono Sent', 0),
        'Mono Failed': counts.get('Mono Failed', 0),
        'Follow-up Mono Sent': fup_mono_sent_count,
        'Follow-up Mono Failed': fup_mono_failed_count,
    }

    # --- Calculate stats for the CURRENT campaign from ContactStatus ---
    current_campaign_statuses = [s.status for s in ContactStatus.query.with_entities(ContactStatus.status).all()]
    current_counts = Counter(current_campaign_statuses)
    current_stats = {
        'Total': ContactStatus.query.count(),
        'Sent': current_counts.get('Sent', 0) + current_counts.get('Sent (Scheduled)', 0),
        'Replied': current_counts.get('Replied', 0),
        'Failed': current_counts.get('Failed', 0),
        'Pending': current_counts.get('Pending', 0),
        'Mono Sent': current_counts.get('Mono Sent', 0),
        'Mono Failed': current_counts.get('Mono Failed', 0),
        'Follow-up Mono Sent': current_counts.get('Follow-up Mono Sent', 0),
        'Follow-up Mono Failed': current_counts.get('Follow-up Mono Failed', 0),
    }

    # Check if it's an AJAX request for dynamic content loading
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return render_template("_results_partial.html", pagination=pagination, stats=stats, active_filter=active_filter, search_query=search_query)

    # For a full page load
    scheduler_status = get_scheduler_status(job_id='send_email_job', name='Main Campaign')
    follow_up_scheduler_status = get_scheduler_status(job_id='send_scheduled_follow_up_job', name='Follow-up Campaign')

    # Consume the skip_loader flag if it exists
    if 'skip_loader' in session:
        session.pop('skip_loader', None)

    logs = get_recent_logs()
    return render_template(
        "results.html", 
        pagination=pagination,
        scheduler_status=scheduler_status,
        follow_up_scheduler_status=follow_up_scheduler_status,
        stats=stats,
        active_filter=active_filter,
        search_query=search_query,
        current_stats=current_stats,
        logs=logs
    )

@app.route("/report")
@login_required
def report():
    """Generates and displays a campaign performance report."""
    from collections import Counter
    
    # Get overall stats (same as results page)
    all_statuses = [s.status for s in ContactStatus.query.with_entities(ContactStatus.status).all()]
    counts = Counter(all_statuses)
    
    stats = {
        'Sent': counts.get('Sent', 0) + counts.get('Sent (Scheduled)', 0),
        'Replied': counts.get('Replied', 0),
        'Failed': counts.get('Failed', 0),
        'Pending': counts.get('Pending', 0),
        'Total': len(all_statuses)
    }

    # Calculate key metrics
    total_sent = stats['Sent']
    total_contacts = stats['Total']
    
    metrics = {
        'reply_rate': (stats['Replied'] / total_sent) if total_sent > 0 else 0,
        'failure_rate': (stats['Failed'] / total_sent) if total_sent > 0 else 0,
        'completion_rate': ((total_sent + stats['Failed']) / total_contacts) if total_contacts > 0 else 0,
    }

    # Get contacts who have replied
    replied_contacts = ContactStatus.query.filter_by(status='Replied').order_by(ContactStatus.reply_received_at.desc()).all()

    generation_date = datetime.now(timezone.utc).strftime('%B %d, %Y at %H:%M %Z')

    return render_template(
        "report.html",
        stats=stats,
        metrics=metrics,
        replied_contacts=replied_contacts,
        generation_date=generation_date
    )

@app.route("/template", methods=["GET", "POST"])
@login_required
def template():
    """Handles editing for both the main and follow-up email templates."""
    os.makedirs(app.instance_path, exist_ok=True)
    main_content = ""
    follow_up_content = ""

    if TEMPLATE_FILE.exists():
        main_content = TEMPLATE_FILE.read_text(encoding="utf-8")
    if FOLLOW_UP_TEMPLATE_FILE.exists():
        follow_up_content = FOLLOW_UP_TEMPLATE_FILE.read_text(encoding="utf-8")

    if request.method == "POST":
        main_content = request.form.get("main_template_content", "")
        follow_up_content = request.form.get("follow_up_template_content", "")
        
        TEMPLATE_FILE.write_text(main_content, encoding="utf-8")
        FOLLOW_UP_TEMPLATE_FILE.write_text(follow_up_content, encoding="utf-8")
        
        flash("Email templates saved successfully.", "success")
        return redirect(url_for("template"))

    return render_template("template.html", 
                           main_template_content=main_content,
                           follow_up_template_content=follow_up_content)

@app.route("/preview")
@login_required
def preview():
    content = ""
    if TEMPLATE_FILE.exists():
        content = TEMPLATE_FILE.read_text(encoding="utf-8")
    preview_vars = {'name':'John Doe', 'company_name':'Acme Corp'}
    from jinja2 import Template
    rendered_template = Template(content).render(preview_vars)
    return render_template("preview.html", preview_content=rendered_template)

@app.route("/preview_follow_up")
@login_required
def preview_follow_up():
    """Renders a preview of the follow-up email template."""
    content = ""
    subject = "Default Follow-up Subject"
    if FOLLOW_UP_TEMPLATE_FILE.exists():
        template_content = FOLLOW_UP_TEMPLATE_FILE.read_text(encoding="utf-8")
        from jinja2 import Template

        parts = template_content.split('\n\n', 1)
        subject_template_str = "Follow-up: {{ company_name }}"
        body_template_str = template_content
        if len(parts) == 2 and parts[0].lower().startswith('subject:'):
            subject_template_str = parts[0][len('subject:'):].strip()
            body_template_str = parts[1]

        preview_vars = {'name': 'Jane Smith', 'company_name': 'Innovate Corp'}
        subject = Template(subject_template_str).render(preview_vars)
        content = Template(body_template_str).render(preview_vars)

    return render_template("preview.html", preview_content=content, subject=subject)

@app.route("/campaign/start", methods=["POST"])
@login_required
def start_campaign():
    """Resumes the main campaign scheduler job."""
    try:
        # The main scheduler must be running for any job to run.
        if scheduler.state == STATE_PAUSED:
            scheduler.resume()
            flash("Main scheduler was paused and has been resumed.", "info")
            app.logger.info("Main scheduler resumed via campaign start.")

        scheduler.resume_job('send_email_job')
        flash("Campaign has been started/resumed.", "success")
        app.logger.info("User started 'send_email_job'.")
    except Exception as e:
        app.logger.error(f"Error resuming campaign: {e}")
        flash(f"Error starting campaign: {e}", "danger")
    return redirect(url_for('dashboard'))

@app.route("/campaign/stop", methods=["POST"])
@login_required
def stop_campaign():
    """Pauses the main campaign scheduler job."""
    try:
        scheduler.pause_job('send_email_job')
        flash("Campaign has been paused.", "info")
        app.logger.info("User paused 'send_email_job'.")
    except Exception as e:
        app.logger.error(f"Error pausing campaign: {e}")
        flash(f"Error stopping campaign: {e}", "danger")
    return redirect(url_for('dashboard'))

@app.route("/campaign/restart", methods=["POST"])
@login_required
def restart_campaign():
    """Stops the scheduler, resets all progress, and sets contacts to Pending."""
    try:
        # 1. Pause the scheduler to prevent it from running during reset
        scheduler.pause()
        app.logger.info("Campaign paused for reset.")

        # 2. Reset job progress file
        if JOB_PROGRESS_FILE.exists():
            JOB_PROGRESS_FILE.unlink()
            app.logger.info("Job progress file deleted.")

        # 3. Reset database statuses for all non-replied contacts
        # This makes them eligible to be picked up by the scheduler again.
        ContactStatus.query.filter(ContactStatus.reply_received == False).update({
            'status': 'Pending',
            'internet_message_id': None,
            'conversation_id': None,
            'last_update': datetime.now(timezone.utc)
        }, synchronize_session=False)
        db.session.commit()
        app.logger.info("Reset status for all non-replied contacts to 'Pending'.")

        flash("Campaign has been stopped and all progress has been reset.", "success")
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error restarting campaign: {e}")
        flash(f"Error restarting campaign: {e}", "danger")
    # Redirect back to the page the user came from (e.g., results or dashboard)
    return redirect(request.referrer or url_for('results'))

@app.route("/campaign/clear", methods=["POST"])
@login_required
def clear_campaign():
    """Clears all campaign data: uploaded file, DB records, and progress."""
    try:
        # Pause scheduler to prevent race conditions
        if scheduler.state == STATE_RUNNING:
            scheduler.pause()
            app.logger.info("Scheduler paused for campaign clear.")

        # Delete the physical uploaded file
        filename = session.pop("uploaded_file", None)
        if filename:
            filepath = Path(app.config["UPLOAD_FOLDER"]) / filename
            if filepath.exists():
                filepath.unlink()
                app.logger.info(f"Deleted uploaded file: {filename}")

        # Clear the ContactStatus table
        num_deleted = db.session.query(ContactStatus).delete()
        db.session.commit()
        app.logger.info(f"Cleared {num_deleted} records from ContactStatus table.")

        # Delete the scheduler progress file
        if JOB_PROGRESS_FILE.exists():
            JOB_PROGRESS_FILE.unlink()
            app.logger.info("Deleted scheduler progress file.")

        # Also delete the reply progress file to ensure a complete reset
        if REPLY_PROGRESS_FILE.exists():
            REPLY_PROGRESS_FILE.unlink()
            app.logger.info("Deleted reply progress file.")

        # Delete the campaign completion flag
        if CAMPAIGN_COMPLETE_FLAG.exists():
            CAMPAIGN_COMPLETE_FLAG.unlink()
            app.logger.info("Deleted campaign completion flag.")

        flash("All campaign data and the uploaded file have been cleared. You can now start over.", "success")
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error clearing all campaign data: {e}")
        flash(f"An error occurred while clearing data: {str(e)}", "danger")

    return redirect(url_for('dashboard'))

@app.route("/api/settings", methods=["GET"])
@login_required
def api_get_settings():
    return jsonify(get_settings())

@app.route("/api/settings/update", methods=["POST"])
@login_required
def api_update_settings():
    data = request.get_json()
    if data is None or 'bcc_enabled' not in data:
        return jsonify({"success": False, "error": "Invalid payload"}), 400
    
    current_settings = get_settings()
    current_settings['bcc_enabled'] = bool(data['bcc_enabled'])

    if save_settings(current_settings):
        app.logger.info(f"User '{session.get('username')}' updated BCC setting to: {current_settings['bcc_enabled']}")
        return jsonify({"success": True, "settings": current_settings})
    else:
        app.logger.error("Failed to save settings file.")
        return jsonify({"success": False, "error": "Failed to save settings"}), 500

@app.route("/api/resend/<int:contact_id>", methods=["POST"])
@login_required
def resend_email(contact_id):
    """Resets a failed contact's status to 'Pending' to be retried by the scheduler."""
    contact = ContactStatus.query.get_or_404(contact_id)

    # Only allow resending for failed contacts
    if 'Failed' not in contact.status:
        return jsonify({"success": False, "error": "Can only resend for failed contacts."}), 400

    try:
        contact.status = 'Pending'
        contact.error_details = None # Clear the old error
        contact.last_update = datetime.now(timezone.utc)
        db.session.commit()

        app.logger.info(f"User '{session.get('username')}' manually queued contact ID {contact_id} ({contact.email}) for resend.")

        return jsonify({
            "success": True,
            "message": f"Email to {contact.email} has been queued for resending.",
            "new_status": "Pending"
        })
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error resending email for contact ID {contact_id}: {e}")
        return jsonify({"success": False, "error": "An internal error occurred."}), 500

@app.route("/api/contact/<int:contact_id>/set_status", methods=["POST"])
@login_required
def set_contact_status(contact_id):
    """Manually sets the status for a contact."""
    contact = ContactStatus.query.get_or_404(contact_id)
    data = request.get_json()
    new_status = data.get("status")

    if not new_status or new_status not in MANUAL_STATUSES:
        return jsonify({"success": False, "error": "Invalid status provided."}), 400

    try:
        contact.status = new_status
        contact.last_update = datetime.now(timezone.utc)
        # Clear error details when a manual status is set, as it supersedes previous errors
        contact.error_details = None
        db.session.commit()

        app.logger.info(f"User '{session.get('username')}' manually set status for contact ID {contact_id} to '{new_status}'.")

        return jsonify({
            "success": True,
            "message": f"Status for {contact.email} updated to '{new_status}'.",
            "new_status": new_status
        })
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error setting manual status for contact ID {contact_id}: {e}")
        return jsonify({"success": False, "error": "An internal error occurred."}), 500

@app.route("/api/follow-up/<int:contact_id>/status", methods=["POST"])
@login_required
def api_set_follow_up_status(contact_id):
    """Sets the follow-up sequence status for a contact (paused, active, inactive)."""
    contact = ContactStatus.query.get_or_404(contact_id)
    data = request.get_json()
    new_status = data.get("status")

    allowed_statuses = ['active', 'paused', 'inactive']
    if not new_status or new_status not in allowed_statuses:
        return jsonify({"success": False, "error": "Invalid status provided."}), 400

    try:
        contact.sequence_status = new_status
        # If we are deactivating, clear the next follow-up time
        if new_status == 'inactive':
            contact.next_follow_up_at = None
        
        db.session.commit()

        app.logger.info(f"User '{session.get('username')}' set follow-up status for contact ID {contact_id} to '{new_status}'.")

        return jsonify({
            "success": True,
            "message": f"Follow-up sequence for {contact.email} set to '{new_status}'.",
            "new_status": new_status
        })
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error setting follow-up status for contact ID {contact_id}: {e}")
        return jsonify({"success": False, "error": "An internal error occurred."}), 500


@app.route("/api/scheduler/interval", methods=["POST"])
@login_required
def set_scheduler_interval():
    data = request.get_json()
    if not data or "interval" not in data:
        return jsonify({"status": "error", "message": "Missing interval value."}), 400

    try:
        interval_seconds = int(data["interval"])
        if interval_seconds <= 0:
            raise ValueError("Interval must be positive.")
    except (ValueError, TypeError):
        return jsonify({"status": "error", "message": "Invalid interval value. Must be a positive integer."}), 400

    try:
        email_job = scheduler.get_job('send_email_job')
        if email_job:
            # Use reschedule_job to modify the trigger
            scheduler.reschedule_job('send_email_job', trigger='interval', seconds=interval_seconds)
            app.logger.info(f"User '{session.get('username')}' updated scheduler interval to {interval_seconds} seconds.")
            return jsonify({"status": "success", "message": f"Scheduler interval updated to {interval_seconds} seconds."})
        else:
            return jsonify({"status": "error", "message": "Scheduler job not found."}), 404
    except Exception as e:
        app.logger.error(f"Failed to update scheduler interval: {e}")
        return jsonify({"status": "error", "message": f"An internal error occurred: {e}"}), 500

@app.route("/api/scheduler/follow_up_interval", methods=["POST"])
@login_required
def set_follow_up_scheduler_interval():
    data = request.get_json()
    if not data or "interval" not in data:
        return jsonify({"status": "error", "message": "Missing interval value."}), 400

    try:
        interval_seconds = int(data["interval"])
        if interval_seconds <= 0:
            raise ValueError("Interval must be positive.")
    except (ValueError, TypeError):
        return jsonify({"status": "error", "message": "Invalid interval value. Must be a positive integer."}), 400

    try:
        follow_up_job = scheduler.get_job('send_scheduled_follow_up_job')
        if follow_up_job:
            # Use reschedule_job to modify the trigger
            scheduler.reschedule_job('send_scheduled_follow_up_job', trigger='interval', seconds=interval_seconds)
            app.logger.info(f"User '{session.get('username')}' updated follow-up scheduler interval to {interval_seconds} seconds.")
            return jsonify({"status": "success", "message": f"Follow-up scheduler interval updated to {interval_seconds} seconds."})
        else:
            return jsonify({"status": "error", "message": "Follow-up scheduler job not found."}), 404
    except Exception as e:
        app.logger.error(f"Failed to update follow-up scheduler interval: {e}")
        return jsonify({"status": "error", "message": f"An internal error occurred: {e}"}), 500

@app.route("/export_csv")
@login_required
def export_csv():
    active_filter = request.args.get('status', None)
    search_query = request.args.get('q', None)

    # Base query - same as results page
    query = ContactStatus.query

    # Apply filter - same as results page
    if active_filter:
        if active_filter == 'Sent':
            query = query.filter(or_(
                ContactStatus.status == 'Sent',
                ContactStatus.status == 'Sent (Scheduled)'
            ))
        elif active_filter == 'Pending':
            query = query.filter(ContactStatus.status == 'Pending')
        elif active_filter == 'Manual':
            query = query.filter(ContactStatus.status.in_(MANUAL_STATUSES))
        else:
            query = query.filter(ContactStatus.status == active_filter)

    # Apply search - same as results page
    if search_query:
        search_term = f"%{search_query}%"
        query = query.filter(or_(
            ContactStatus.email.ilike(search_term),
            ContactStatus.name.ilike(search_term),
            ContactStatus.company_name.ilike(search_term)
        ))

    # Fetch all matching contacts
    contacts = query.order_by(ContactStatus.last_update.desc()).all()

    # Create DataFrame for CSV export
    data = [{
        'Email': c.email,
        'Name': c.name,
        'Company': c.company_name,
        'Status': c.status,
        'Last Updated': c.last_update.strftime('%Y-%m-%d %H:%M:%S'),
        'Replied At': c.reply_received_at.strftime('%Y-%m-%d %H:%M:%S') if c.reply_received_at else '',
        'Error Details': c.error_details
    } for c in contacts]
    df = pd.DataFrame(data)

    # Create in-memory CSV using an in-memory string buffer
    output = io.StringIO()
    df.to_csv(output, index=False)
    csv_data = output.getvalue()

    # Return as a file download
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=campaign_results.csv"}
    )

@app.route("/history")
@login_required
def history():
    page = request.args.get('page', 1, type=int)
    search_query = request.args.get('q', None)
    sort_by = request.args.get('sort_by', 'sent_at') # Default sort
    sort_order = request.args.get('sort_order', 'desc') # Default order
    start_date_str = request.args.get('start_date', None)
    end_date_str = request.args.get('end_date', None)

    # Base query
    log_query = EmailLog.query

    # Apply search
    if search_query:
        search_term = f"%{search_query}%"
        log_query = log_query.filter(or_(
            EmailLog.email.ilike(search_term),
            EmailLog.name.ilike(search_term),
            EmailLog.company_name.ilike(search_term)
        ))

    # Apply date range filter
    if start_date_str:
        try:
            start_date_obj = date.fromisoformat(start_date_str)
            # Filter from the beginning of the start day
            log_query = log_query.filter(EmailLog.sent_at >= datetime.combine(start_date_obj, time.min))
        except (ValueError, TypeError):
            flash("Invalid start date format. Please use YYYY-MM-DD.", "warning")
            start_date_str = None # Clear invalid date

    if end_date_str:
        try:
            end_date_obj = date.fromisoformat(end_date_str)
            # Filter until the end of the end day
            log_query = log_query.filter(EmailLog.sent_at <= datetime.combine(end_date_obj, time.max))
        except (ValueError, TypeError):
            flash("Invalid end date format. Please use YYYY-MM-DD.", "warning")
            end_date_str = None # Clear invalid date

    # Apply sorting
    sortable_columns = {
        'sent_at': EmailLog.sent_at,
        'email': EmailLog.email,
        'name': EmailLog.name,
        'company_name': EmailLog.company_name,
        'status': EmailLog.status,
    }
    if sort_by in sortable_columns:
        sort_column = sortable_columns[sort_by]
        if sort_order == 'asc':
            log_query = log_query.order_by(sort_column.asc())
        else:
            log_query = log_query.order_by(sort_column.desc())
    else:
        # Fallback to default sort if invalid sort_by is provided
        log_query = log_query.order_by(EmailLog.sent_at.desc())
    
    # The total count should reflect the search results, not all logs.
    total_sent = log_query.count()
    
    pagination = log_query.paginate(page=page, per_page=50, error_out=False)

    return render_template("history.html", pagination=pagination, total_sent=total_sent, 
                           search_query=search_query, sort_by=sort_by, sort_order=sort_order,
                           start_date=start_date_str, end_date=end_date_str)


@app.route("/export_history_csv")
@login_required
def export_history_csv():
    """Exports the email log to a CSV file, filtered by search query if provided."""
    search_query = request.args.get('q', None)
    start_date_str = request.args.get('start_date', None)
    end_date_str = request.args.get('end_date', None)

    # Base query
    query = EmailLog.query

    # Apply search
    if search_query:
        search_term = f"%{search_query}%"
        query = query.filter(or_(
            EmailLog.email.ilike(search_term),
            EmailLog.name.ilike(search_term),
            EmailLog.company_name.ilike(search_term)
        ))

    # Apply date range filter
    if start_date_str:
        try:
            start_date_obj = date.fromisoformat(start_date_str)
            query = query.filter(EmailLog.sent_at >= datetime.combine(start_date_obj, time.min))
        except (ValueError, TypeError):
            pass # Ignore invalid date on export

    if end_date_str:
        try:
            end_date_obj = date.fromisoformat(end_date_str)
            query = query.filter(EmailLog.sent_at <= datetime.combine(end_date_obj, time.max))
        except (ValueError, TypeError):
            pass # Ignore invalid date on export

    # Fetch all matching logs
    logs = query.order_by(EmailLog.sent_at.desc()).all()

    # Create DataFrame for CSV export
    data = [{
        'Sent At': log.sent_at.strftime('%Y-%m-%d %H:%M:%S'),
        'Email': log.email,
        'Name': log.name,
        'Company': log.company_name,
        'Status': log.status,
        'Subject': log.subject,
        'Error Details': log.error_details
    } for log in logs]
    df = pd.DataFrame(data)

    # Create in-memory CSV
    output = io.StringIO()
    df.to_csv(output, index=False)
    csv_data = output.getvalue()

    # Return as a file download
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=email_history.csv"}
    )

@app.route("/history/delete/<int:log_id>", methods=["POST"])
@login_required
def delete_history_log(log_id):
    """Deletes a single log entry from the history."""
    log = EmailLog.query.get_or_404(log_id)
    try:
        db.session.delete(log)
        db.session.commit()
        app.logger.info(f"User '{session.get('username')}' deleted history log ID {log_id} for email {log.email}.")
        flash("History entry deleted successfully.", "success")
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error deleting history log ID {log_id}: {e}")
        flash(f"An error occurred while deleting the entry: {e}", "danger")
    return redirect(request.referrer or url_for('history'))

@app.route("/api/history/log/<int:log_id>/set_status", methods=["POST"])
@login_required
def set_history_log_status(log_id):
    """Manually sets the status for a history log entry."""
    log = EmailLog.query.get_or_404(log_id)
    data = request.get_json()
    new_status = data.get("status")

    # Combine original statuses with manual override statuses from the main campaign
    allowed_statuses = ["Sent", "Failed", "Not Sent", "Mono Sent", "Mono Failed"] + MANUAL_STATUSES

    if not new_status or new_status not in allowed_statuses:
        return jsonify({"success": False, "error": "Invalid status provided."}), 400

    try:
        original_status = log.status
        log.status = new_status
        db.session.commit()

        app.logger.info(f"User '{session.get('username')}' manually changed history log ID {log_id} status from '{original_status}' to '{new_status}'.")

        return jsonify({
            "success": True,
            "message": f"Log status for {log.email} updated to '{new_status}'.",
            "new_status": new_status
        })
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error setting manual status for history log ID {log_id}: {e}")
        return jsonify({"success": False, "error": "An internal error occurred."}), 500

@app.route("/history/clear_all", methods=["POST"])
@login_required
def clear_all_history():
    """Deletes all entries from the EmailLog table."""
    try:
        num_deleted = db.session.query(EmailLog).delete()
        db.session.commit()
        app.logger.info(f"User '{session.get('username')}' deleted all {num_deleted} email history logs.")
        flash(f"Successfully deleted all {num_deleted} email history records.", "success")
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error clearing all email history: {e}")
        flash(f"An error occurred while clearing history: {str(e)}", "danger")
    return redirect(url_for('history'))

@app.route("/api/contacts/update", methods=["POST"])
@login_required
def api_update_contacts():
    filename = session.get("uploaded_file")
    if not filename:
        return jsonify({"error": "No file uploaded in this session."}), 400

    filepath = Path(app.config["UPLOAD_FOLDER"]) / filename
    if not filepath.exists():
        return jsonify({"error": "Uploaded file not found."}), 404

    try:
        df = pd.read_csv(filepath) if filename.endswith(".csv") else pd.read_excel(filepath)
        
        # Normalize DF columns to match keys from JS. This will modify the file's headers on save.
        df.columns = [str(c).strip().replace(" ", "_").lower() for c in df.columns]

        updates = request.get_json()
        if not isinstance(updates, list):
            return jsonify({"error": "Invalid data format."}), 400

        new_rows_data = []
        
        # Add any new columns from the payload to the dataframe
        for update in updates:
            for col in update:
                if col != 'index' and col not in df.columns:
                    df[col] = pd.NA

        for update in updates:
            index = update.pop("index")
            if index > 0: # Update existing row (1-based index from JS)
                row_index = index - 1
                if row_index < len(df):
                    for col, val in update.items():
                        if col in df.columns:
                            df.loc[row_index, col] = val
            else: # Add new row (negative index from JS)
                new_rows_data.append(update)

        if new_rows_data:
            new_df = pd.DataFrame(new_rows_data)
            df = pd.concat([new_df, df], ignore_index=True)

        # Save back to file with normalized columns
        if filename.endswith(".csv"):
            df.to_csv(filepath, index=False)
        else:
            df.to_excel(filepath, index=False)
        
        flash("Contacts updated successfully. The preview has been refreshed.", "success")
        return jsonify({"success": True, "reload": True})

    except Exception as e:
        app.logger.error(f"Error updating contacts: {e}")
        return jsonify({"error": f"An internal error occurred: {e}"}), 500

@app.route("/api/contacts/delete", methods=["POST"])
@login_required
def api_delete_contact():
    filename = session.get("uploaded_file")
    if not filename:
        return jsonify({"error": "No file uploaded in this session."}), 400

    filepath = Path(app.config["UPLOAD_FOLDER"]) / filename
    if not filepath.exists():
        return jsonify({"error": "Uploaded file not found."}), 404

    data = request.get_json()
    index_to_delete = data.get("index")

    if index_to_delete is None:
        return jsonify({"error": "Index to delete is missing."}), 400

    try:
        df = pd.read_csv(filepath) if filename.endswith(".csv") else pd.read_excel(filepath)
        row_index = index_to_delete - 1 # JS sends 1-based index
        if 0 <= row_index < len(df):
            df = df.drop(row_index).reset_index(drop=True)
        else:
            return jsonify({"error": "Invalid index."}), 400

        if filename.endswith(".csv"):
            df.to_csv(filepath, index=False)
        else:
            df.to_excel(filepath, index=False)
        return jsonify({"success": True})
    except Exception as e:
        app.logger.error(f"Error deleting contact: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/contacts/bulk_delete", methods=["POST"])
@login_required
def api_bulk_delete_contacts():
    filename = session.get("uploaded_file")
    if not filename:
        return jsonify({"error": "No file uploaded in this session."}), 400
    filepath = Path(app.config["UPLOAD_FOLDER"]) / filename
    if not filepath.exists():
        return jsonify({"error": "Uploaded file not found."}), 404
    data = request.get_json()
    indices_to_delete = data.get("indices", [])
    try:
        df = pd.read_csv(filepath) if filename.endswith(".csv") else pd.read_excel(filepath)
        zero_based_indices = [i - 1 for i in indices_to_delete]
        valid_indices = [i for i in zero_based_indices if 0 <= i < len(df)]
        df = df.drop(valid_indices).reset_index(drop=True)
        if filename.endswith(".csv"):
            df.to_csv(filepath, index=False)
        else:
            df.to_excel(filepath, index=False)
        return jsonify({"success": True})
    except Exception as e:
        app.logger.error(f"Error during bulk delete: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/stats")
@app.route("/api/scheduled_stats")
@login_required
def api_scheduled_stats():
    """Provides status counts for initial scheduled campaign history, with optional date filtering."""
    from datetime import date, datetime, time
    start_date_str = request.args.get('start_date')
    end_date_str = request.args.get('end_date')

    query = EmailLog.query

    if start_date_str:
        try:
            start_date = datetime.combine(date.fromisoformat(start_date_str), time.min)
            query = query.filter(EmailLog.sent_at >= start_date)
        except (ValueError, TypeError):
            pass # Ignore invalid date
    if end_date_str:
        try:
            end_date = datetime.combine(date.fromisoformat(end_date_str), time.max)
            query = query.filter(EmailLog.sent_at <= end_date)
        except (ValueError, TypeError):
            pass # Ignore invalid date

    from collections import Counter
    statuses = [log.status for log in query.with_entities(EmailLog.status).all()]
    counts = Counter(statuses)
    
    # Consolidate statuses for cleaner reporting
    sent_count = sum(counts.get(s, 0) for s in ['Sent', 'Sent (Scheduled)'])
    failed_count = counts.get('Failed', 0)
    
    # Replies are tracked across all types, so we can include them here.
    replied_count = EmailLog.query.filter(EmailLog.reply_received == True).distinct(EmailLog.conversation_id).count()

    stats = {
        'Sent': sent_count,
        'Replied': replied_count,
        'Failed': failed_count,
    }
    
    # Filter out zero-value stats for a cleaner chart
    return jsonify({k: v for k, v in stats.items() if v > 0})

@app.route("/api/follow_up_stats")
@login_required
def api_follow_up_stats():
    """Provides status counts for all follow-up email history, with optional date filtering."""
    from datetime import date, datetime, time
    start_date_str = request.args.get('start_date')
    end_date_str = request.args.get('end_date')

    query = EmailLog.query

    if start_date_str:
        try:
            start_date = datetime.combine(date.fromisoformat(start_date_str), time.min)
            query = query.filter(EmailLog.sent_at >= start_date)
        except (ValueError, TypeError):
            pass
    if end_date_str:
        try:
            end_date = datetime.combine(date.fromisoformat(end_date_str), time.max)
            query = query.filter(EmailLog.sent_at <= end_date)
        except (ValueError, TypeError):
            pass

    from collections import Counter
    statuses = [log.status for log in query.with_entities(EmailLog.status).all()]
    counts = Counter(statuses)
    
    # Consolidate statuses for cleaner reporting
    sent_count = sum(counts.get(s, 0) for s in ['Follow-up Sent', 'Follow-up Sent (Scheduled)', 'Follow-up Mono Sent'])
    failed_count = sum(counts.get(s, 0) for s in ['Follow-up Failed', 'Follow-up Failed (Scheduled)', 'Follow-up Mono Failed'])

    stats = {
        'Follow-up Sent': sent_count,
        'Follow-up Failed': failed_count,
    }
    
    # Filter out zero-value stats for a cleaner chart
    return jsonify({k: v for k, v in stats.items() if v > 0})

@app.route("/api/mono_stats")
@login_required
def api_mono_stats():
    """Provides status counts for mono/one-off email history, with optional date filtering."""
    from datetime import date, datetime, time
    start_date_str = request.args.get('start_date')
    end_date_str = request.args.get('end_date')

    query = EmailLog.query

    if start_date_str:
        try:
            start_date = datetime.combine(date.fromisoformat(start_date_str), time.min)
            query = query.filter(EmailLog.sent_at >= start_date)
        except (ValueError, TypeError):
            pass
    if end_date_str:
        try:
            end_date = datetime.combine(date.fromisoformat(end_date_str), time.max)
            query = query.filter(EmailLog.sent_at <= end_date)
        except (ValueError, TypeError):
            pass

    from collections import Counter
    statuses = [log.status for log in query.with_entities(EmailLog.status).all()]
    counts = Counter(statuses)
    
    # Consolidate statuses for cleaner reporting
    initial_mono_sent = counts.get('Mono Sent', 0)
    initial_mono_failed = counts.get('Mono Failed', 0)
    follow_up_mono_sent = counts.get('Follow-up Mono Sent', 0)
    follow_up_mono_failed = counts.get('Follow-up Mono Failed', 0)

    stats = {
        'Initial Mono Sent': initial_mono_sent,
        'Initial Mono Failed': initial_mono_failed,
        'Follow-up Mono Sent': follow_up_mono_sent,
        'Follow-up Mono Failed': follow_up_mono_failed,
    }
    
    # Filter out zero-value stats for a cleaner chart
    return jsonify({k: v for k, v in stats.items() if v > 0})

@app.route("/api/current_campaign_stats")
@login_required
def api_current_campaign_stats():
    """Provides status counts for today's campaign activity from the ContactStatus table."""
    from collections import Counter
    from datetime import datetime, time, timezone, timedelta

    # Define the reset time in UTC. 3:00 PM IST is 9:30 AM UTC.
    reset_hour_utc = 9
    reset_minute_utc = 30
    reset_time_naive = time(reset_hour_utc, reset_minute_utc) # Naive time for comparison
    reset_time_utc = time(reset_hour_utc, reset_minute_utc, tzinfo=timezone.utc)

    now_utc = datetime.now(timezone.utc)

    # Determine the start and end of the current "chart day" based on the reset time.
    if now_utc.time() < reset_time_naive:
        # If current time is before 9:30 AM UTC, the "chart day" started yesterday at 9:30 AM UTC.
        today_end = datetime.combine(now_utc.date(), reset_time_utc)
        today_start = today_end - timedelta(days=1)
    else:
        # If current time is after 9:30 AM UTC, the "chart day" started today at 9:30 AM UTC.
        today_start = datetime.combine(now_utc.date(), reset_time_utc)
        today_end = today_start + timedelta(days=1)

    # 1. Query EmailLog for sent/failed/replied events within the time window
    email_logs = EmailLog.query.filter(
        EmailLog.sent_at >= today_start,
        EmailLog.sent_at < today_end
    ).all()
    
    # 2. Query ContactStatus for manual status updates within the time window
    manual_status_updates = ContactStatus.query.filter(
        ContactStatus.last_update >= today_start,
        ContactStatus.last_update < today_end,
        ContactStatus.status.in_(MANUAL_STATUSES)
    ).all()

    # 3. Combine the counts from both sources
    counts = Counter(log.status for log in email_logs)
    counts.update(contact.status for contact in manual_status_updates)

    # Consolidate statuses for cleaner reporting.
    stats = {
        'Sent': counts.get('Sent', 0) + counts.get('Sent (Scheduled)', 0),
        'Replied': counts.get('Replied', 0),
        'Failed': counts.get('Failed', 0),
        # Add other consolidated statuses if needed in the future
    }
    
    # Add any manual statuses that have a count greater than zero for today
    for status in MANUAL_STATUSES:
        if counts.get(status, 0) > 0:
            stats[status] = counts.get(status, 0)

    return jsonify({k: v for k, v in stats.items() if v > 0})

@app.route("/api/activity_over_time")
@login_required
def api_activity_over_time():
    """Provides daily activity counts for the last 7 days."""
    from datetime import timedelta

    # Initialize last 7 days with 0 counts in chronological order
    today = datetime.now(timezone.utc).date()
    # Last 7 days including today
    dates = [(today - timedelta(days=i)) for i in range(6, -1, -1)]
    
    activity_by_day = {d.strftime('%b %d'): {'Sent': 0, 'Failed': 0, 'Replied': 0} for d in dates}

    # Query contacts where the last_update was within the last 7 days
    start_date = today - timedelta(days=6)

    # Query email logs instead of contact statuses
    logs = EmailLog.query.filter(
        EmailLog.sent_at >= datetime.combine(start_date, time.min, tzinfo=timezone.utc),
        EmailLog.sent_at <= datetime.combine(today, time.max, tzinfo=timezone.utc) # Ensure it covers up to the end of today
    ).all()

    for log in logs:
        day_key = log.sent_at.strftime('%b %d')
        if day_key in activity_by_day:
            status = log.status
            if 'Sent' in status:
                activity_by_day[day_key]['Sent'] += 1
            elif 'Failed' in status:
                activity_by_day[day_key]['Failed'] += 1
            elif status == 'Replied':
                activity_by_day[day_key]['Replied'] += 1


    labels = list(activity_by_day.keys())
    sent_data = [day['Sent'] for day in activity_by_day.values()]
    failed_data = [day['Failed'] for day in activity_by_day.values()]
    replied_data = [day['Replied'] for day in activity_by_day.values()]

    return jsonify({
        'labels': labels,
        'datasets': [
            {'label': 'Sent', 'data': sent_data, 'backgroundColor': 'rgba(40, 167, 69, 0.7)'},
            {'label': 'Failed', 'data': failed_data, 'backgroundColor': 'rgba(220, 53, 69, 0.7)'},
            {'label': 'Replied', 'data': replied_data, 'backgroundColor': 'rgba(23, 162, 184, 0.7)'}
        ]
    })

@app.route("/api/daily_sent_count")
@login_required
def api_daily_sent_count():
    """Provides the total count of sent emails for each day."""
    from sqlalchemy import func, cast, Date as SQLDate
    from datetime import timedelta, date, datetime

    sent_statuses = [
        'Sent', 'Sent (Scheduled)', 'Mono Sent', 
        'Follow-up Sent', 'Follow-up Mono Sent', 'Follow-up Sent (Scheduled)'
    ]

    # 1. Define the date range from request args, or default to last 30 days
    start_date_str = request.args.get('start_date')
    end_date_str = request.args.get('end_date')

    if start_date_str and end_date_str:
        start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
        end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
    else: # Default to last 30 days if no dates provided
        end_date = date.today()
        start_date = end_date - timedelta(days=29)
    
    days_diff = (end_date - start_date).days
    date_range = [start_date + timedelta(days=x) for x in range(days_diff + 1)]

    # Query to get counts of sent emails grouped by date
    daily_counts = db.session.query(
        func.date(EmailLog.sent_at).label('sent_date'),
        func.count(EmailLog.id)
    ).filter( # Filter by status and then by date range (inclusive of start and end day)
        EmailLog.status.in_(sent_statuses), # All statuses considered "sent"
        EmailLog.sent_at >= datetime.combine(start_date, time.min, tzinfo=timezone.utc),
        EmailLog.sent_at <= datetime.combine(end_date, time.max, tzinfo=timezone.utc)
    ).group_by(
        func.date(EmailLog.sent_at)
    ).order_by(
        func.date(EmailLog.sent_at)
    ).all()

    # 2. Create a dictionary for quick lookup
    counts_dict = {row[0]: row[1] for row in daily_counts}

    # 3. Build the final labels and data, filling in zeros for days with no activity
    labels = [d.strftime('%b %d') for d in date_range]
    data = [counts_dict.get(d.strftime('%Y-%m-%d'), 0) for d in date_range]

    return jsonify({'labels': labels, 'data': data})

@app.route("/api/file_columns")
@login_required
def api_file_columns():
    """Returns the column headers from the uploaded file for template personalization."""
    filename = session.get("uploaded_file")
    if not filename:
        return jsonify({"columns": [], "error": "No file uploaded."}), 404

    filepath = Path(app.config["UPLOAD_FOLDER"]) / filename
    if not filepath.exists():
        return jsonify({"columns": [], "error": "Uploaded file not found."}), 404

    try:
        # Read only the first row to be efficient
        df = pd.read_csv(filepath, nrows=1) if filename.endswith(".csv") else pd.read_excel(filepath, nrows=1)
        # Normalize columns just like the sending logic to ensure they match
        normalized_columns = [str(c).strip().replace(" ", "_").lower() for c in df.columns]
        return jsonify({"columns": normalized_columns})
    except Exception as e:
        app.logger.error(f"Could not read columns from {filename}: {e}")
        return jsonify({"columns": [], "error": f"Error reading file: {e}"}), 500

@app.route("/api/follow_up_file_columns")
@login_required
def api_follow_up_file_columns():
    """Returns the column headers from the uploaded follow-up file."""
    filename = session.get("follow_up_uploaded_file")
    if not filename:
        return jsonify({"columns": [], "error": "No follow-up file uploaded."}), 404

    follow_up_upload_folder = Path(app.config["UPLOAD_FOLDER"]) / "follow_ups"
    filepath = follow_up_upload_folder / filename
    if not filepath.exists():
        return jsonify({"columns": [], "error": "Uploaded follow-up file not found."}), 404

    try:
        df = pd.read_csv(filepath, nrows=1) if filename.endswith(".csv") else pd.read_excel(filepath, nrows=1)
        normalized_columns = [str(c).strip().replace(" ", "_").lower() for c in df.columns]
        return jsonify({"columns": normalized_columns})
    except Exception as e:
        return jsonify({"columns": [], "error": f"Error reading file: {e}"}), 500

@app.route("/api/get_template/<string:country>")
@login_required
def get_template_content(country):
    """API endpoint to get the content of a specific template."""
    # This route is likely for a preview feature. Let's make it consistent.
    # The old `email_template.html` is now effectively the US one.
    if country == 'us' or country == 'default':
        template_path = TEMPLATE_FILE_US
    elif country == 'ca':
        template_path = TEMPLATE_FILE_CA
    elif country == 'old': # Keep a way to see the very old one if needed
        template_path = TEMPLATE_FILE
    elif country == 'ca':
        template_path = TEMPLATE_FILE_CA
    else:
        return "Template not found", 404

    if not template_path.exists():
        return f"Template file for {country.upper()} not found on server.", 404

    try:
        content = template_path.read_text(encoding="utf-8")
        return Response(content, mimetype='text/html')
    except IOError as e:
        app.logger.error(f"Could not read template file {template_path}: {e}")
        return "Error reading template file.", 500

@app.route('/follow-up-dashboard', methods=["GET", "POST"])
@login_required
def follow_up_dashboard():
    """Renders the dashboard for the manual follow-up campaign."""
    pending_contacts = []
    processed_contacts = []
    campaign_completed = False
    total_contacts = 0
    processed_count = 0
    # Use a separate session key to not conflict with the main dashboard's file
    filename = session.get("follow_up_uploaded_file")

    if request.method == "POST":
        file = request.files.get("file")
        if not file or file.filename == "":
            flash("No file selected", "warning")
            return redirect(url_for("follow_up_dashboard"))

        if file and allowed_file(file.filename):
            # A new file upload should clear the progress for the follow-up job
            if FOLLOW_UP_JOB_PROGRESS_FILE.exists():
                FOLLOW_UP_JOB_PROGRESS_FILE.unlink()
                app.logger.info("Cleared follow-up job progress for new file upload.")

            # Save the new file
            filename = secure_filename(file.filename)
            # Save to a dedicated subfolder to avoid conflicts
            follow_up_upload_folder = Path(app.config["UPLOAD_FOLDER"]) / "follow_ups"
            os.makedirs(follow_up_upload_folder, exist_ok=True)
            filepath = follow_up_upload_folder / filename
            file.save(filepath)
            session["follow_up_uploaded_file"] = filename
            app.logger.info(f"User '{session.get('username')}' uploaded new follow-up file: {filename}")
            flash(f"Follow-up file '{filename}' uploaded successfully.", "success")
            return redirect(url_for("follow_up_dashboard"))
        else:
            flash("Invalid file type. Please upload a CSV or XLSX file.", "danger")
            return redirect(url_for("follow_up_dashboard"))

    if filename:
        follow_up_upload_folder = Path(app.config["UPLOAD_FOLDER"]) / "follow_ups"
        filepath = follow_up_upload_folder / filename
        if filepath.exists():
            try:
                df = pd.read_csv(filepath) if filename.endswith(".csv") else pd.read_excel(filepath)
                # Use robust column normalization to consistently find contact data
                df.columns = [str(c).strip().replace(" ", "_").lower() for c in df.columns]
                total_contacts = len(df)

                # Determine progress
                processed_count = 0
                if FOLLOW_UP_JOB_PROGRESS_FILE.exists():
                    try:
                        progress = json.loads(FOLLOW_UP_JOB_PROGRESS_FILE.read_text(encoding='utf-8'))
                        # Check if progress file is for the current data file
                        if progress.get("file_path") == str(filepath):
                            processed_count = progress.get("next_row", 0)
                    except (json.JSONDecodeError, IOError):
                        app.logger.warning("Could not read follow-up progress file. Assuming start.")
                
                campaign_completed = total_contacts > 0 and processed_count >= total_contacts

                # Split dataframe
                processed_df = df.iloc[:processed_count]
                pending_df = df.iloc[processed_count:]

                # Convert to list of dicts for template
                for index, row in pending_df.iterrows():
                    row_dict = row.to_dict()
                    pending_contacts.append({
                        "index": index + 1,
                        "name": row_dict.get("name", row_dict.get("first_name", "")),
                        "email_id": row_dict.get("email_id", row_dict.get("email", "")),
                        "company_name": row_dict.get("company_name", row_dict.get("company", ""))
                    })
                for index, row in processed_df.iterrows():
                    row_dict = row.to_dict()
                    processed_contacts.append({
                        "index": index + 1,
                        "name": row_dict.get("name", row_dict.get("first_name", "")),
                        "email_id": row_dict.get("email_id", row_dict.get("email", "")),
                        "company_name": row_dict.get("company_name", row_dict.get("company", ""))
                    })
            except Exception as e:
                flash(f"Error reading follow-up file: {e}", "danger")
                # If the file is bad, remove it from the session to prevent repeated errors
                session.pop("follow_up_uploaded_file", None)
        else:
            flash(f"Could not find file '{filename}'. Please upload it again.", "warning")
            session.pop("follow_up_uploaded_file", None)

    # Get status for the specific follow-up job
    scheduler_status = get_scheduler_status('send_scheduled_follow_up_job')

    return render_template(
        "follow_up_dashboard.html",
        username=session.get("username"),
        pending_contacts=pending_contacts,
        processed_contacts=processed_contacts,
        campaign_completed=campaign_completed,
        total_contacts=total_contacts,
        processed_count=processed_count,
        scheduler_status=scheduler_status,
    )

@app.route('/inbox')
@login_required
def inbox():
    """
    Renders the main inbox page. The data will be fetched dynamically by the frontend via the inbox_api.
    """
    return render_template('inbox.html')

@app.route('/api/inbox/conversation/<string:conversation_id>')
@login_required
def get_conversation(conversation_id):
    """API endpoint to fetch all messages for a given conversation ID."""
    if not conversation_id:
        return jsonify({"error": "Conversation ID is required."}), 400

    # Fetch all logs for this conversation, order by time.
    # 'sent_at' is used for both sent and received times in this model.
    logs = EmailLog.query.filter_by(conversation_id=conversation_id).order_by(EmailLog.sent_at.asc()).all()

    if not logs:
        return jsonify({"error": "Conversation not found."}), 404

    conversation_data = []
    for log in logs:
        # Determine message type based on the 'status' field
        # A 'sent' message is anything that isn't explicitly 'Received'.
        if log.status != 'Received':
            msg_type = 'sent'
            # The body of sent messages is now stored in the 'body' column.
            content = log.body or f"<i>The body of this sent email was not saved.</i>"
        else: # It's a received item, status is 'Received'
            msg_type = 'reply'
            content = log.reply_content or "<i>This reply did not have any content.</i>"
        conversation_data.append({
            'id': log.id,
            'type': msg_type,
            'email': log.email,
            'name': log.name,
            'subject': log.subject,
            'content': content,
            'timestamp': (log.reply_received_at or log.sent_at).isoformat(),
            'status': log.status
        })

    return jsonify({"conversation": conversation_data})

if __name__ == "__main__":
    # The Werkzeug reloader can cause issues on Windows, especially when a file
    # like the token file is written during a request, causing a server crash.
    # Disabling the reloader (`use_reloader=False`) prevents this crash during login.
    # You will need to manually restart the server to see code changes.
    app.run(debug=True, use_reloader=False)
