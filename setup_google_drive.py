"""
ProtoValue3D — Google Drive Account Setup Assistant
Use this tool to connect a new Google Drive account / folder to your Print Planner & ERPNext.
"""

import sys
import os
import json
import webbrowser
import requests
from pathlib import Path
from urllib.parse import urlencode, parse_qs, urlparse
from http.server import HTTPServer, BaseHTTPRequestHandler

# Import gdrive helpers
from gdrive_client import load_gdrive_config, save_gdrive_config, extract_folder_id_from_url

# Default ProtoValue OAuth credentials
DEFAULT_CLIENT_ID = os.environ.get("GDRIVE_CLIENT_ID", "")
DEFAULT_CLIENT_SECRET = os.environ.get("GDRIVE_CLIENT_SECRET", "")

REDIRECT_PORT = 8085
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"

captured_code = None

class OAuthCallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global captured_code
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if "code" in qs:
            captured_code = qs["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            html = """
            <html>
            <body style="font-family: Arial, sans-serif; text-align: center; padding-top: 50px; background: #0f172a; color: #f8fafc;">
                <h1 style="color: #38bdf8;">ProtoValue3D — Authorization Successful!</h1>
                <p style="font-size: 16px;">You have successfully connected your Google Drive account.</p>
                <p style="color: #94a3b8;">You can now close this browser tab and return to the Setup Assistant terminal.</p>
            </body>
            </html>
            """
            self.wfile.write(html.encode("utf-8"))
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"No authorization code received.")
            
    def log_message(self, format, *args):
        pass  # Suppress default server logs


def test_token_and_get_user(access_token):
    try:
        r = requests.get(
            "https://www.googleapis.com/drive/v3/about?fields=user,storageQuota",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def run_setup():
    print("=" * 72)
    print(" ☁️  PROTOVALUE3D — GOOGLE DRIVE ACCOUNT SETUP & SWITCH ASSISTANT")
    print("=" * 72)
    print("\nThis assistant will help you connect a new Google Drive account and folder.")
    print("It will automatically save your settings into 'gdrive_config.json'.\n")

    current_cfg = load_gdrive_config()
    print("Current Configuration:")
    print(f"  • Client ID: {current_cfg['client_id'][:15]}...")
    print(f"  • Root Folder ID: {current_cfg['root_folder_id']}")
    print(f"  • Builds Folder ID: {current_cfg['builds_folder_id']}")
    print("-" * 72)

    print("\nSelect Setup Mode:")
    print(" [1] Use Service Account Key (JSON file - Recommended, Never Expires!)")
    print(" [2] Sign in via Web Browser (Google OAuth 2.0 User Account)")
    print(" [3] Manually Enter Existing Refresh Token & Credentials")
    print(" [4] Just Update Root / Builds Folder IDs")
    
    choice = input("\nEnter choice [1/2/3/4] (default: 1): ").strip() or "1"

    client_id = current_cfg["client_id"]
    client_secret = current_cfg["client_secret"]
    refresh_token = current_cfg["refresh_token"]
    root_folder = current_cfg["root_folder_id"]
    builds_folder = current_cfg["builds_folder_id"]

    if choice == "1":
        print("\n--- Google Cloud Service Account Setup ---")
        print("In Google Cloud Console -> IAM & Admin -> Service Accounts:")
        print("Select 'protovalue-uploader@protovalue3d-drive-510113.iam.gserviceaccount.com'")
        print("Go to Keys tab -> Add Key -> Create new key -> Select JSON -> Create.")
        print("This downloads a .json file to your computer.\n")
        key_input = input("Enter path to your downloaded .json key file (or drag & drop here): ").strip().strip('"').strip("'")
        
        if not key_input or not os.path.exists(key_input):
            print(f"❌ File not found: {key_input}")
            return
            
        try:
            with open(key_input, "r", encoding="utf-8") as f:
                sa_data = json.load(f)
        except Exception as e:
            print(f"❌ Failed to parse JSON key: {e}")
            return
            
        # Save to local service_account.json
        dest = Path(__file__).resolve().parent / "service_account.json"
        with open(dest, "w", encoding="utf-8") as f:
            json.dump(sa_data, f, indent=2)
        print(f"✅ Saved Service Account key to {dest.name}")
        
        # Test Service Account Token
        from gdrive_client import get_service_account_token
        sa_token = get_service_account_token(sa_data)
        if sa_token:
            print("🎉 Authentication SUCCESSFUL!")
            print(f"   Connected Service Account: {sa_data.get('client_email')}")
            
            # Test access to folders
            headers = {"Authorization": f"Bearer {sa_token}"}
            for fid, label in [(root_folder, "Order Files Folder"), (builds_folder, "Build Files Folder")]:
                r = requests.get(f"https://www.googleapis.com/drive/v3/files/{fid}?fields=id,name,capabilities&supportsAllDrives=true", headers=headers)
                if r.status_code == 200:
                    d = r.json()
                    can_add = d.get('capabilities', {}).get('canAddChildren')
                    print(f"   ✅ {label} accessible! (Name: '{d.get('name')}', Write Access: {can_add})")
                else:
                    print(f"   ⚠️ Could not access {label} ({fid}): {r.text}")
                    print(f"      Make sure you gave 'Editor' access to {sa_data.get('client_email')} on that folder.")
        else:
            print("❌ Could not obtain token with this service account key.")
            return

    elif choice == "2":
        print("\n--- Google Cloud OAuth Client ID ---")
        use_custom = input(f"Do you want to use your own Google Cloud Project Client ID? [y/N]: ").strip().lower()
        if use_custom == "y":
            client_id = input("Enter Client ID: ").strip() or client_id
            client_secret = input("Enter Client Secret: ").strip() or client_secret

        scope = "https://www.googleapis.com/auth/drive"
        auth_params = {
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": scope,
            "access_type": "offline",
            "prompt": "consent"
        }
        auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(auth_params)}"

        print(f"\n1. Starting local callback listener on port {REDIRECT_PORT}...")
        try:
            server = HTTPServer(("localhost", REDIRECT_PORT), OAuthCallbackHandler)
            server.timeout = 180  # 3 minutes
        except Exception as e:
            print(f"Could not bind port {REDIRECT_PORT}: {e}")
            server = None

        print("\n2. Opening your web browser to sign in to your Google Account...")
        print(f"URL: {auth_url}\n")
        try:
            webbrowser.open(auth_url)
        except Exception:
            pass

        code = None
        if server:
            print("⏳ Waiting for you to complete sign-in in your browser...")
            while not captured_code:
                server.handle_request()
                if captured_code:
                    code = captured_code
                    break
        
        if not code:
            print("\nIf the browser didn't redirect automatically, paste the redirect URL or authorization code here:")
            pasted = input("Pasted URL or Code: ").strip()
            if "code=" in pasted:
                code = parse_qs(urlparse(pasted).query).get("code", [""])[0]
            else:
                code = pasted

        if not code:
            print("❌ No authorization code received. Setup aborted.")
            return

        # Exchange code for refresh token
        print("\n3. Exchanging code for permanent Refresh Token...")
        token_url = "https://oauth2.googleapis.com/token"
        token_data = {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI
        }
        res = requests.post(token_url, data=token_data)
        if res.status_code == 200:
            tokens = res.json()
            refresh_token = tokens.get("refresh_token") or refresh_token
            access_token = tokens.get("access_token")
            print("✅ Refresh Token obtained successfully!")
            
            # Verify user info
            about = test_token_and_get_user(access_token)
            if about:
                user_info = about.get("user", {})
                print(f"👤 Connected Account: {user_info.get('displayName')} ({user_info.get('emailAddress')})")
        else:
            print(f"❌ Token exchange failed: {res.text}")
            print("If redirect_uri mismatch occurred, ensure 'http://localhost:8085/callback' is in Google Console Authorized Redirect URIs.")
            return

    elif choice == "2":
        print("\n--- Manual Credentials Entry ---")
        client_id = input(f"Client ID [{client_id[:15]}...]: ").strip() or client_id
        client_secret = input(f"Client Secret [{client_secret[:8]}...]: ").strip() or client_secret
        refresh_token = input("Refresh Token: ").strip() or refresh_token

    # Folder configuration
    print("\n--- Google Drive Folders Setup ---")
    print("Paste your Google Drive Root Folder URL or ID (where Sales Order folders reside):")
    print(f"Current: {root_folder}")
    new_root = input("Root Folder URL/ID [Press Enter to keep current]: ").strip()
    if new_root:
        root_folder = extract_folder_id_from_url(new_root) or new_root

    print("\nPaste your dedicated 'Print Builds' Folder URL or ID (where .3mf files will be saved):")
    print(f"Current: {builds_folder}")
    new_builds = input("Builds Folder URL/ID [Press Enter to use Root Folder]: ").strip()
    if new_builds:
        builds_folder = extract_folder_id_from_url(new_builds) or new_builds
    else:
        builds_folder = root_folder

    # Save to gdrive_config.json
    save_gdrive_config(
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
        root_folder_id=root_folder,
        builds_folder_id=builds_folder
    )

    print("\n" + "=" * 72)
    print("🎉 GOOGLE DRIVE CONFIGURATION SAVED SUCCESSFULLY!")
    print("=" * 72)
    print(f"📁 Root Folder ID:   {root_folder}")
    print(f"🖨️  Builds Folder ID: {builds_folder}")
    print("All Python Print Planner and OrcaSlicer modules are now synchronized with this Google Account.")
    print("=" * 72 + "\n")


if __name__ == "__main__":
    run_setup()
