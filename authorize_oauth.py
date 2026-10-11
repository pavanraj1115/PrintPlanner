import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import os
import json
import webbrowser
import requests
from urllib.parse import urlencode, parse_qs, urlparse
from http.server import HTTPServer, BaseHTTPRequestHandler
BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "gdrive_config.json"
cfg = {}
if CONFIG_FILE.exists():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        pass

CLIENT_ID = cfg.get("client_id") or os.environ.get("GDRIVE_CLIENT_ID", "")
CLIENT_SECRET = cfg.get("client_secret") or os.environ.get("GDRIVE_CLIENT_SECRET", "")
if not CLIENT_ID or not CLIENT_SECRET:
    print("Google OAuth Client ID & Secret not configured in gdrive_config.json or environment.")
    CLIENT_ID = input("Enter Google Client ID: ").strip() or CLIENT_ID
    CLIENT_SECRET = input("Enter Google Client Secret: ").strip() or CLIENT_SECRET

PORT = 8085
REDIRECT_URI = f"http://localhost:{PORT}/callback"

captured_code = None

class OAuthHandler(BaseHTTPRequestHandler):
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
                <p style="font-size: 16px;">Refresh Token is being generated. You can close this tab now.</p>
            </body>
            </html>
            """
            self.wfile.write(html.encode("utf-8"))
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"No authorization code received.")
            
    def log_message(self, format, *args):
        pass

auth_params = {
    "client_id": CLIENT_ID,
    "redirect_uri": REDIRECT_URI,
    "response_type": "code",
    "scope": "https://www.googleapis.com/auth/drive",
    "access_type": "offline",
    "prompt": "consent"
}
auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(auth_params)}"

print("=" * 70)
print("GOOGLE OAUTH 2.0 DESKTOP AUTHORIZATION")
print("=" * 70)
print(f"Client ID: {CLIENT_ID}")
print(f"\nOpening browser for authorization...")
print(f"URL: {auth_url}\n")

try:
    webbrowser.open(auth_url)
except Exception:
    pass

try:
    server = HTTPServer(("localhost", PORT), OAuthHandler)
    server.timeout = 180
    print(f"Listening on http://localhost:{PORT}/callback for up to 3 minutes...")
    while not captured_code:
        server.handle_request()
        if captured_code:
            break
except Exception as e:
    print(f"Server error: {e}")

if not captured_code:
    print("No code captured. Exiting.")
    sys.exit(1)

print("\nExchanging authorization code for permanent Refresh Token...")
token_url = "https://oauth2.googleapis.com/token"
token_data = {
    "client_id": CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "code": captured_code,
    "grant_type": "authorization_code",
    "redirect_uri": REDIRECT_URI
}

r = requests.post(token_url, data=token_data)
if r.status_code == 200:
    tokens = r.json()
    refresh_token = tokens.get("refresh_token")
    access_token = tokens.get("access_token")
    print(f"\nSUCCESS! Generated Refresh Token:")
    print(f"GDRIVE_REFRESH_TOKEN = \"{refresh_token}\"")
    
    # Save to gdrive_config.json
    planner_cfg = BASE_DIR / "gdrive_config.json"
    root_fid = cfg.get("root_folder_id", "1G3TsYS0Lk8u-P1hrvJ2RX_FnF2fHmZCY")
    builds_fid = cfg.get("builds_folder_id", "1FaF8oZlPjxWT9wedPMIrgjQDDbSiFHIo")
    cfg_data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": refresh_token,
        "root_folder_id": root_fid,
        "builds_folder_id": builds_fid
    }
    with open(planner_cfg, "w", encoding="utf-8") as f:
        json.dump(cfg_data, f, indent=4)
    print(f"Updated: {planner_cfg}")
    
    # Update frontend .env.local
    frontend_env = Path(r"c:\Users\DELL\Downloads\frontend\.env.local")
    env_content = f"""# Google Drive Configuration
GDRIVE_FOLDER_ID=1G3TsYS0Lk8u-P1hrvJ2RX_FnF2fHmZCY
GDRIVE_BUILDS_FOLDER_ID=1FaF8oZlPjxWT9wedPMIrgjQDDbSiFHIo
GDRIVE_CLIENT_ID={CLIENT_ID}
GDRIVE_CLIENT_SECRET={CLIENT_SECRET}
GDRIVE_REFRESH_TOKEN={refresh_token}

# ERPNext Configuration
ERPNEXT_URL=https://protovalue3d.m.frappe.cloud
ERPNEXT_API_KEY=8bb5cdb1d2ab502
ERPNEXT_API_SECRET=f9cfff5f7186691
"""
    frontend_env.write_text(env_content, encoding="utf-8")
    print(f"Updated: {frontend_env}")
    
    # Test uploading a file
    print("\nTesting file upload to Builds Folder (1FaF8oZlPjxWT9wedPMIrgjQDDbSiFHIo)...")
    upload_url = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&supportsAllDrives=true"
    headers = {"Authorization": f"Bearer {access_token}"}
    metadata = {
        "name": "PV3D_Test_Project.3mf",
        "description": "ProtoValue3D Automated Print Build Project",
        "parents": ["1FaF8oZlPjxWT9wedPMIrgjQDDbSiFHIo"]
    }
    files = {
        "data": ("metadata", json.dumps(metadata), "application/json; charset=UTF-8"),
        "file": ("PV3D_Test_Project.3mf", b"ProtoValue3D Test Slicer Build Content", "application/octet-stream")
    }
    up_res = requests.post(upload_url, headers=headers, files=files, timeout=60)
    print("Upload Status:", up_res.status_code)
    if up_res.status_code == 200:
        up_data = up_res.json()
        print(f"FILE UPLOADED SUCCESSFULLY! File ID: {up_data.get('id')}")
        print(f"Link: https://drive.google.com/file/d/{up_data.get('id')}/view?usp=sharing")
    else:
        print("Upload Error:", up_res.text)
else:
    print(f"Failed to exchange token: {r.text}")
