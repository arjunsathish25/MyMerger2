import os
import msal

CLIENT_ID = os.getenv("MS_CLIENT_ID")
CLIENT_SECRET = os.getenv("MS_CLIENT_SECRET")
print(f"DEBUG: Auth loaded secret length: {len(CLIENT_SECRET) if CLIENT_SECRET else 'None'}")
TENANT_ID = os.getenv("MS_TENANT_ID")
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
REDIRECT_URI = os.getenv("OAUTH_REDIRECT_URI")       # Must exactly match Azure app registration
# Request all scopes needed by the app upfront (web + scheduler)
# Mail.ReadWrite is needed for the reply checking job.
SCOPES = ["User.Read", "Mail.Send", "Mail.ReadWrite"]

msal_app = msal.ConfidentialClientApplication(
    CLIENT_ID,
    authority=AUTHORITY,
    client_credential=CLIENT_SECRET
)

def build_auth_url():
    return msal_app.get_authorization_request_url(
        SCOPES,
        redirect_uri=REDIRECT_URI
    )

def acquire_token_by_auth_code(auth_code):
    return msal_app.acquire_token_by_authorization_code(
        auth_code,
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI
    )
