import httpx
from flask import Blueprint, jsonify, session, request, current_app, redirect, url_for
from functools import wraps
from datetime import datetime, timedelta
import dateutil.parser

inbox_api_bp = Blueprint('inbox_api', __name__)

def require_auth(f):
    """Decorator to ensure the user is authenticated and has a valid token."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Use the app's login_required logic implicitly by checking the session
        if "username" not in session:
            # For API calls, return a 401 Unauthorized error
            return jsonify({"error": "User not authenticated. Please log in again."}), 401
        from app import get_access_token # Use the app's main token function
        access_token = get_access_token() # This handles refresh internally
        if not access_token:
            return jsonify({"error": "Authentication token is missing or has expired. Please log in again."}), 401
        
        kwargs['access_token'] = access_token
        return f(*args, **kwargs)
    return decorated_function

def format_received_datetime(dt_string):
    """Formats a UTC datetime string into a user-friendly relative time."""
    if not dt_string:
        return ""
    try:
        received_time = dateutil.parser.isoparse(dt_string)
        now = datetime.now(received_time.tzinfo) # Use timezone-aware now
        diff = now - received_time

        if diff < timedelta(minutes=1):
            return "Just now"
        elif diff < timedelta(hours=1):
            minutes = int(diff.total_seconds() / 60)
            return f"{minutes}m ago"
        elif diff < timedelta(days=1):
            hours = int(diff.total_seconds() / 3600)
            return f"{hours}h ago"
        elif diff < timedelta(days=7):
            days = diff.days
            return f"{days}d ago"
        else:
            return received_time.strftime("%b %d")
    except (ValueError, TypeError):
        return dt_string # Fallback to original string


# --- API Routes ---

# --- API Routes ---

@inbox_api_bp.route("/api/inbox/mail")
@require_auth
def get_mail(access_token):
    """Fetches a list of emails from the user's inbox or other folders."""
    folder_map = {
        'inbox': 'inbox',
        'sent': 'sentitems',
        'archived': 'archive',
        'trash': 'deleteditems'
    }
    
    requested_folder = request.args.get('folder', 'inbox')
    graph_folder_id = folder_map.get(requested_folder, 'inbox')
    
    search_query = request.args.get('search', '')
    page = int(request.args.get('page', 1))
    page_size = 20  # Number of items per page
    skip = (page - 1) * page_size

    # Select fields to keep payload light
    select_fields = "id,conversationId,subject,from,sender,bodyPreview,receivedDateTime,isRead,hasAttachments,importance"
    
    # Base URL for the specific folder's messages
    graph_url = f"https://graph.microsoft.com/v1.0/me/mailFolders/{graph_folder_id}/messages?$select={select_fields}&$orderby=receivedDateTime desc&$top={page_size}&$skip={skip}&$count=true"
    
    if search_query:
        # Note: $search usually requires consistency in query params; 
        # mixing $search with OData filters can store specific constraints.
        # Ideally, we append search separately or use the search endpoint.
        # For simple listing, we append.
        graph_url += f"&$search=\"{search_query}\""

    headers = {'Authorization': 'Bearer ' + access_token}
    
    try:
        with httpx.Client() as client:
            response = client.get(graph_url, headers=headers)
            response.raise_for_status()
            data = response.json()

        total_items = data.get('@odata.count', 0)
        total_pages = (total_items + page_size - 1) // page_size if page_size > 0 else 0

        formatted_items = []
        for item in data.get('value', []):
            # 'sender' is used in Sent Items, 'from' in Inbox.
            if requested_folder == 'sent':
                # In sent items, we usually want to see who we sent TO (conceptually),
                # but the basic message object lists 'toRecipients'.
                # For a simple list, showing the 'subject' is key, but let's try to grab 'toRecipients' if we changed the select query.
                # For now, consistent with standard views, we show the 'To' name or just 'Me' as sender.
                # To make it clear in the list, let's use the 'viewer' logic.
                display_name = "Me"
            else:
                sender_obj = item.get('from', {}) or item.get('sender', {})
                email_address = sender_obj.get('emailAddress', {})
                display_name = email_address.get('name', 'Unknown Sender')

            formatted_items.append({
                'id': item.get('id'),
                'conversationId': item.get('conversationId'),
                'sender': display_name,
                'avatar_initial': display_name[0].upper() if display_name else '?',
                'avatar_color': f'hsl({hash(display_name) % 360}, 60%, 70%)',
                'subject': item.get('subject', 'No Subject'),
                'preview': item.get('bodyPreview', ''),
                'timestamp': format_received_datetime(item.get('receivedDateTime')),
                'unread': not item.get('isRead', True),
                'hasAttachments': item.get('hasAttachments', False),
                'importance': item.get('importance', 'normal')
            })

        return jsonify({
            "items": formatted_items,
            "total_pages": total_pages,
            "current_page": page,
            "has_next": page < total_pages,
            "has_prev": page > 1,
            "folder": requested_folder 
        })

    except httpx.HTTPStatusError as e:
        current_app.logger.error(f"Graph API HTTP error in get_mail: {e.response.text}")
        return jsonify({"error": "Failed to fetch mail", "details": str(e)}), e.response.status_code
    except Exception as e:
        current_app.logger.error(f"Unexpected error in get_mail: {e}", exc_info=True)
        return jsonify({"error": "An unexpected error occurred", "details": str(e)}), 500


@inbox_api_bp.route("/api/inbox/send", methods=['POST'])
@require_auth
def send_mail(access_token):
    """
    Sends a new email with optional read receipts.
    """
    data = request.json
    subject = data.get('subject')
    body = data.get('body')
    to_recipients = data.get('to') # List of email strings
    read_receipt = data.get('read_receipt', False)

    if not subject or not body or not to_recipients:
        return jsonify({"error": "Missing required fields (to, subject, body)"}), 400

    # Format recipients for Graph API
    recipient_list = [{"emailAddress": {"address": email.strip()}} for email in to_recipients]

    # Construct the message payload
    message = {
        "subject": subject,
        "body": {
            "contentType": "HTML",
            "content": body
        },
        "toRecipients": recipient_list
    }

    # Add internet message headers for read receipt
    if read_receipt:
        # To verify: The logged-in user's email is needed for the Disposition-Notification-To header.
        # We can try to get it from the session token claims if stored, or make a call to /me.
        # For efficiency, let's look at the session first.
        user_email = session.get("user", {}).get("userPrincipalName")
        if not user_email:
             # Fallback: make a quick call to /me to get the address
             pass # Kept simple: Graph API 'isReadReceiptRequested' property is cleaner than manual headers.
        
        # Method A: The Graph API way
        message["isReadReceiptRequested"] = True
        message["isDeliveryReceiptRequested"] = True # Optional: ask for delivery receipt too

    url = "https://graph.microsoft.com/v1.0/me/sendMail"
    headers = {'Authorization': 'Bearer ' + access_token, 'Content-Type': 'application/json'}
    payload = {"message": message, "saveToSentItems": "true"}

    try:
        with httpx.Client() as client:
            response = client.post(url, headers=headers, json=payload)
            if response.status_code != 202:
                raise httpx.HTTPStatusError(f"Send failed: {response.text}", request=response.request, response=response)

        return jsonify({"success": True, "message": "Email sent successfully."})

    except Exception as e:
        current_app.logger.error(f"Error sending mail: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@inbox_api_bp.route("/api/inbox/conversation/<conversation_id>")
@require_auth
def get_conversation(access_token, conversation_id):
    """Fetches all messages in a specific conversation."""
    graph_url = f"https://graph.microsoft.com/v1.0/me/messages?$filter=conversationId eq '{conversation_id}'&$select=id,subject,from,toRecipients,body,receivedDateTime,isRead"
    headers = {'Authorization': 'Bearer ' + access_token}

    try:
        with httpx.Client() as client:
            response = client.get(graph_url, headers=headers)
            response.raise_for_status()
            data = response.json()

        if not data.get('value'):
            return jsonify({"error": "Conversation not found"}), 404

        messages_data = data['value']
        #Sort messages by receivedDateTime ascending (oldest first)
        messages_data.sort(key=lambda x: x.get('receivedDateTime', ''))
        first_message = messages_data[0]
        
        # Identify the conversation partner (simplified logic)
        # If I am the sender of the first message, the partner is the first 'To'.
        # If I am not the sender, the sender is the partner.
        user_email = session.get("user", {}).get("userPrincipalName", "").lower()
        first_sender_email = first_message.get('from', {}).get('emailAddress', {}).get('address', '').lower()
        
        # Partner details for the header
        if first_sender_email == user_email:
             # I started the thread
             recips = first_message.get('toRecipients', [])
             partner_obj = recips[0]['emailAddress'] if recips else {'name': 'Unknown', 'address': ''}
        else:
             partner_obj = first_message.get('from', {}).get('emailAddress', {})

        partner_name = partner_obj.get('name', 'Unknown')
        
        messages = []
        for msg in messages_data:
            msg_sender_email = msg.get('from', {}).get('emailAddress', {}).get('address', '')
            is_me = msg_sender_email.lower() == user_email
            
            messages.append({
                'id': msg.get('id'),
                'from_user': is_me,
                'sender_name': 'You' if is_me else msg.get('from', {}).get('emailAddress', {}).get('name'),
                'body': msg.get('body', {}).get('content', ''),
                'timestamp': format_received_datetime(msg.get('receivedDateTime')),
                'is_read': msg.get('isRead', True)
            })

            # Check if we need to mark this specific message as read?
            # Creating a side-effect in a GET request is technically not RESTful, 
            # but efficient for this usecase. Let's do it in a separate call from frontend for cleanliness.

        return jsonify({
            'id': conversation_id,
            'subject': first_message.get('subject', 'No Subject'),
            'participant': {
                'name': partner_name,
                'email': partner_obj.get('address', ''),
                'avatar_initial': partner_name[0].upper() if partner_name else '?',
                'avatar_color': f'hsl({hash(partner_name) % 360}, 60%, 70%)'
            },
            'messages': messages
        })

    except httpx.HTTPStatusError as e:
        current_app.logger.error(f"Graph API HTTP error in get_conversation: {e.response.text}")
        return jsonify({"error": "Failed to fetch conversation", "details": str(e)}), e.response.status_code
    except Exception as e:
        current_app.logger.error(f"Unexpected error in get_conversation: {e}", exc_info=True)
        return jsonify({"error": "An unexpected error occurred", "details": str(e)}), 500


@inbox_api_bp.route("/api/inbox/reply/<conversation_id>", methods=['POST'])
@require_auth
def send_reply(access_token, conversation_id):
    """
    Sends a reply to the *latest* message in the conversation.
    """
    body = request.json.get('body')
    if not body:
        return jsonify({"error": "Reply body is required"}), 400

    headers = {'Authorization': 'Bearer ' + access_token}

    try:
        with httpx.Client() as client:
            # 1. Find the ID of the last message in the conversation to fetch the correct Reply-To chain
            find_last_msg_url = f"https://graph.microsoft.com/v1.0/me/messages?$filter=conversationId eq '{conversation_id}'&$orderby=receivedDateTime desc&$top=1&$select=id"
            last_msg_response = client.get(find_last_msg_url, headers=headers)
            last_msg_response.raise_for_status()
            last_msg_data = last_msg_response.json()
            
            messages = last_msg_data.get('value', [])
            if not messages:
                return jsonify({"error": "Could not find a message to reply to."}), 404
            
            actual_message_id = messages[0].get('id')

            # 2. Use the "createReply" endpoint which creates a draft
            # Actually, simply POST to .../reply sends it directly if we don't use 'createReply'
            reply_url = f"https://graph.microsoft.com/v1.0/me/messages/{actual_message_id}/reply"
            
            payload = {"comment": body}
            reply_headers = {'Authorization': 'Bearer ' + access_token, 'Content-Type': 'application/json'}
            
            # The /reply endpoint sends the message.
            response = client.post(reply_url, headers=reply_headers, json=payload)
            if response.status_code != 202:
                # 202 Accepted = Success for sending
                 raise httpx.HTTPStatusError(f"Error from Graph API: {response.text}", request=response.request, response=response)

        return jsonify({"success": True, "message": "Reply sent."})

    except Exception as e:
        current_app.logger.error(f"Error sending reply: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@inbox_api_bp.route("/api/inbox/message/<message_id>/read", methods=['POST'])
@require_auth
def mark_read(access_token, message_id):
    """Marks a single message as read."""
    url = f"https://graph.microsoft.com/v1.0/me/messages/{message_id}"
    headers = {'Authorization': 'Bearer ' + access_token, 'Content-Type': 'application/json'}
    payload = {"isRead": True}
    
    try:
        with httpx.Client() as client:
             response = client.patch(url, headers=headers, json=payload)
             response.raise_for_status()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@inbox_api_bp.route("/api/inbox/move", methods=['POST'])
@require_auth
def move_message(access_token):
    """Moves a message (or conversation representative) to a different folder."""
    data = request.json
    message_id = data.get('message_id') # We ideally need the specific message ID, not conversation ID
    destination = data.get('destination') # 'archive' or 'trash'
    
    # Map friendly names to Graph API known folder names
    # Note: 'archive' is a bit special. It's a well-known folder but needs to be found. 
    # For now, let's assume standard 'archive' or 'deleteditems'.
    dest_map = {
        'archive': 'archive',
        'trash': 'deleteditems',
        'inbox': 'inbox'
    }
    target_folder_id = dest_map.get(destination)
    
    if not message_id or not target_folder_id:
        return jsonify({"error": "Invalid parameters"}), 400

    url = f"https://graph.microsoft.com/v1.0/me/messages/{message_id}/move"
    headers = {'Authorization': 'Bearer ' + access_token, 'Content-Type': 'application/json'}
    payload = {"destinationId": target_folder_id}

    try:
        with httpx.Client() as client:
            response = client.post(url, headers=headers, json=payload)
            # If 404, maybe it's already moved.
            if response.status_code != 201:
                 # 201 Created is the success code for a move (creates new item in dest)
                 pass 
            response.raise_for_status()
            
        return jsonify({"success": True})
    except Exception as e:
        current_app.logger.error(f"Error moving message: {e}")
        return jsonify({"error": str(e)}), 500