import requests
import base64
import os
from flask import current_app

def send_email_with_graph(access_token, to_email, subject, html_body, bcc_recipients=None, conversation_id=None):
    """
    Sends an email using a two-step Microsoft Graph API process (create draft, then send).
    This ensures that message IDs are returned, as expected by the application.
    If a conversation_id is provided, it will attempt to send the email as a reply
    within that conversation thread.
    
    Args:
        access_token (str): The Graph API access token.
        to_email (str): The recipient's email address.
        subject (str): The email subject.
        html_body (str): The HTML content of the email.
        bcc_recipients (list, optional): A list of email addresses for BCC. Defaults to None.
        conversation_id (str, optional): The ID of the conversation to reply to. Defaults to None.

    Returns:
        tuple: A tuple containing (send_result, error_msg).
               - send_result (dict): The message object from Graph API on success, containing IDs.
               - error_msg (str): An error message string on failure.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    try:
        draft_message = None
        message_to_reply_id = None

        # If a conversation_id is provided, find a message in that thread to reply to.
        if conversation_id:
            find_message_url = f"https://graph.microsoft.com/v1.0/me/messages?$filter=conversationId eq '{conversation_id}'&$top=1"
            try:
                find_resp = requests.get(find_message_url, headers=headers)
                find_resp.raise_for_status()
                messages = find_resp.json().get('value', [])
                if messages:
                    message_to_reply_id = messages[0].get('id')
                    current_app.logger.info(f"Found message {message_to_reply_id} to reply to for conversation {conversation_id}")
            except requests.exceptions.RequestException as e:
                current_app.logger.warning(f"Could not find message to reply to for conversation {conversation_id}: {e}. Sending as new thread.")

        if message_to_reply_id:
            # --- 1a. Create a draft reply ---
            # The body of a reply is a 'comment'. The subject is inherited but can be overridden later.
            reply_payload = { "comment": html_body }
            create_reply_url = f"https://graph.microsoft.com/v1.0/me/messages/{message_to_reply_id}/createReply"
            reply_draft_resp = requests.post(create_reply_url, headers=headers, json=reply_payload)
            reply_draft_resp.raise_for_status()
            draft_message = reply_draft_resp.json()
            
            # --- 1b. Update the draft with the correct subject and BCC recipients ---
            message_id = draft_message.get('id')
            if not message_id:
                return None, "Failed to create reply draft: No message ID returned."

            update_payload = { "subject": subject }
            if bcc_recipients:
                update_payload['bccRecipients'] = [
                    {"emailAddress": {"address": email}} for email in bcc_recipients
                ]
            
            update_draft_url = f"https://graph.microsoft.com/v1.0/me/messages/{message_id}"
            update_resp = requests.patch(update_draft_url, headers=headers, json=update_payload)
            update_resp.raise_for_status()
            
            # The draft_message object already has the IDs we need, but re-assigning from the
            # final patched draft is safer in case any properties changed.
            draft_message = update_resp.json()

        else:
            # --- 1. Create a new draft message (original logic) ---
            message_data = {
                "subject": subject,
                "body": {
                    "contentType": "HTML",
                    "content": html_body
                },
                "toRecipients": [{"emailAddress": {"address": to_email}}]
            }
            if bcc_recipients:
                message_data['bccRecipients'] = [
                    {"emailAddress": {"address": email}} for email in bcc_recipients
                ]

            create_draft_url = "https://graph.microsoft.com/v1.0/me/messages"
            draft_response = requests.post(create_draft_url, headers=headers, json=message_data)
            draft_response.raise_for_status()
            draft_message = draft_response.json()

        if not draft_message:
            return None, "Failed to create a draft message."

        message_id = draft_message.get('id')
        if not message_id:
            return None, "Failed to create draft: No message ID returned."

        # --- 3. Add attachment to the draft message (only for initial emails) ---
        pdf_path = current_app.config.get('ATTACHMENT_PDF_PATH')
        # Attach the PDF if the path is configured, regardless of whether it's a new email or a reply.
        if pdf_path:
            if os.path.exists(pdf_path):
                current_app.logger.info(f"Initial email detected. Adding attachment to draft message ID {message_id}.")
                try:
                    with open(pdf_path, "rb") as f:
                        pdf_content = f.read()
                    encoded_pdf = base64.b64encode(pdf_content).decode('utf-8')
                    
                    attachment_payload = {
                        "@odata.type": "#microsoft.graph.fileAttachment",
                        "name": os.path.basename(pdf_path),
                        "contentType": "application/pdf",
                        "contentBytes": encoded_pdf
                    }
    
                    add_attachment_url = f"https://graph.microsoft.com/v1.0/me/messages/{message_id}/attachments"
                    attachment_response = requests.post(add_attachment_url, headers=headers, json=attachment_payload)
                    attachment_response.raise_for_status()
                    current_app.logger.info(f"Successfully added attachment '{os.path.basename(pdf_path)}' to draft {message_id}.")
    
                except requests.exceptions.RequestException as e:
                    error_msg = f"Failed to add attachment to draft: {e}"
                    if e.response is not None:
                        try:
                            error_data = e.response.json()
                            error_msg = error_data.get("error", {}).get("message", str(e))
                        except (ValueError, AttributeError): pass
                    current_app.logger.error(error_msg)
                    return None, error_msg # Fail the entire send if attachment fails
            else:
                # Log a warning if the path is configured but the file doesn't exist
                current_app.logger.warning(f"Configured PDF attachment not found at path: {pdf_path}. Sending email without it.")

        # --- 4. Send the draft message ---
        send_url = f"https://graph.microsoft.com/v1.0/me/messages/{message_id}/send"
        send_response = requests.post(send_url, headers=headers)
        send_response.raise_for_status()

        # On success, return the original draft object which contains the IDs
        return draft_message, None

    except requests.exceptions.RequestException as e:
        error_msg = str(e)
        if e.response is not None:
            try:
                error_data = e.response.json()
                error_msg = error_data.get("error", {}).get("message", str(e))
            except (ValueError, AttributeError):
                pass # Stick with the original exception string
        
        current_app.logger.error(f"Graph API error sending to {to_email}: {error_msg}")
        return None, error_msg
