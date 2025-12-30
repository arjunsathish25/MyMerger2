import json
import os
from msal import PublicClientApplication
from dotenv import load_dotenv

load_dotenv() # Load variables from .env file

# Use the same Client ID and Scopes as the main application for consistency
CLIENT_ID = os.getenv("MS_CLIENT_ID")
if not CLIENT_ID:
    raise ValueError("MS_CLIENT_ID not found in .env file. Please set it up.")

TENANT_ID = os.getenv("MS_TENANT_ID")
if not TENANT_ID:
    raise ValueError("MS_TENANT_ID not found in .env file. Please set it up.")
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
SCOPES = ["User.Read", "Mail.Send", "Mail.ReadWrite"] # Align with web login scopes

print("--- Initializing login process ---")
print(f"Using Client ID: {CLIENT_ID}")
print(f"Using Authority: {AUTHORITY}\n")

app = PublicClientApplication(CLIENT_ID, authority=AUTHORITY)

def device_code_flow():
    """Initiates a device code flow for command-line authentication."""
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise ValueError("Failed to create device flow")

    print(f"\n--- Device Login Required ---")
    print(f"To sign in, open a web browser and go to: {flow['verification_uri']}")
    print(f"Then, enter the following code: {flow['user_code']}\n")
    print("The script will continue automatically after you sign in.")

    # Wait for user to authenticate
    result = app.acquire_token_by_device_flow(flow)
    return result

if __name__ == "__main__":
    token_response = device_code_flow()
    if "access_token" in token_response:
        print("\nAuthentication successful. Saving token...")
        instance_path = "instance"
        os.makedirs(instance_path, exist_ok=True)
        token_file_path = os.path.join(instance_path, ".graph_token.json")
        with open(token_file_path, "w") as f:
            json.dump(token_response, f)
        print(f"Token saved to {token_file_path}")
        print("You can now start the main application by running: python app.py")
    else:
        print("\n--- Authentication Failed ---")
        print(f"Error: {token_response.get('error')}")
        print(f"Description: {token_response.get('error_description')}")
