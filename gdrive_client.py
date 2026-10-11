import os
import re
import requests
from pathlib import Path
from config import BASE_DIR, MODELS_DIR

import json

CONFIG_FILE = BASE_DIR / "gdrive_config.json"

def load_gdrive_config():
    cfg = {}
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:
            print(f"[Google Drive] Error loading {CONFIG_FILE.name}: {e}")
            
    client_id = cfg.get("client_id") or os.environ.get("GDRIVE_CLIENT_ID", "")
    client_secret = cfg.get("client_secret") or os.environ.get("GDRIVE_CLIENT_SECRET", "")
    refresh_token = cfg.get("refresh_token") or os.environ.get("GDRIVE_REFRESH_TOKEN", "")
    root_folder = cfg.get("root_folder_id") or os.environ.get("GDRIVE_ROOT_FOLDER_ID", "1G3TsYS0Lk8u-P1hrvJ2RX_FnF2fHmZCY")
    builds_folder = cfg.get("builds_folder_id") or os.environ.get("GDRIVE_BUILDS_FOLDER_ID", "1FaF8oZlPjxWT9wedPMIrgjQDDbSiFHIo")
    
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "root_folder_id": root_folder,
        "builds_folder_id": builds_folder
    }

def save_gdrive_config(client_id, client_secret, refresh_token, root_folder_id, builds_folder_id=None):
    cfg = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "root_folder_id": root_folder_id,
        "builds_folder_id": builds_folder_id or root_folder_id
    }
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4)
    print(f"[Google Drive] Saved new configuration to {CONFIG_FILE.name}")

_config = load_gdrive_config()
GDRIVE_CLIENT_ID = _config["client_id"]
GDRIVE_CLIENT_SECRET = _config["client_secret"]
GDRIVE_REFRESH_TOKEN = _config["refresh_token"]
GDRIVE_ROOT_FOLDER_ID = _config["root_folder_id"]
GDRIVE_BUILDS_FOLDER_ID = _config["builds_folder_id"]


SERVICE_ACCOUNT_FILE = BASE_DIR / "service_account.json"

def get_service_account_token(sa_source=None):
    """
    Generates a Google Drive access token using a Service Account Private Key.
    """
    try:
        sa_data = None
        if sa_source is None:
            if SERVICE_ACCOUNT_FILE.exists():
                with open(SERVICE_ACCOUNT_FILE, "r", encoding="utf-8") as f:
                    sa_data = json.load(f)
            elif os.environ.get("GDRIVE_SERVICE_ACCOUNT_KEY"):
                raw = os.environ.get("GDRIVE_SERVICE_ACCOUNT_KEY", "").strip()
                if raw.startswith("'") or raw.startswith('"'):
                    raw = raw[1:-1].strip()
                sa_data = json.loads(raw)
        elif isinstance(sa_source, (str, Path)) and os.path.exists(sa_source):
            with open(sa_source, "r", encoding="utf-8") as f:
                sa_data = json.load(f)
        elif isinstance(sa_source, dict):
            sa_data = sa_source

        if not sa_data or not sa_data.get("client_email") or not sa_data.get("private_key"):
            return None

        import time
        import base64
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        now = int(time.time())
        header = {"alg": "RS256", "typ": "JWT"}
        claim_set = {
            "iss": sa_data["client_email"],
            "scope": "https://www.googleapis.com/auth/drive",
            "aud": "https://oauth2.googleapis.com/token",
            "exp": now + 3600,
            "iat": now,
        }

        def b64url(data):
            return base64.urlsafe_b64encode(json.dumps(data).encode("utf-8")).decode("utf-8").rstrip("=")

        encoded_header = b64url(header)
        encoded_claim = b64url(claim_set)
        signing_input = f"{encoded_header}.{encoded_claim}".encode("utf-8")

        private_key = load_pem_private_key(sa_data["private_key"].encode("utf-8"), password=None)
        signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        encoded_sig = base64.urlsafe_b64encode(signature).decode("utf-8").rstrip("=")

        jwt_assertion = f"{encoded_header}.{encoded_claim}.{encoded_sig}"

        resp = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": jwt_assertion,
            },
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json().get("access_token")
        else:
            print(f"[Google Drive Service Account] Token error: {resp.text}")
    except Exception as e:
        print(f"[Google Drive Service Account] Exception: {e}")
    return None


def get_gdrive_access_token():
    """
    Obtains a fresh Google Drive access token:
    1. Checks for user OAuth Refresh Token (uses full user storage quota)
    2. Falls back to Service Account credentials
    """
    cfg = load_gdrive_config()
    client_id = cfg.get("client_id")
    client_secret = cfg.get("client_secret")
    refresh_token = cfg.get("refresh_token")
    
    # 1. Prefer OAuth User Token (supports full file uploads without quota block)
    if client_id and client_secret and refresh_token:
        url = "https://oauth2.googleapis.com/token"
        data = {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token"
        }
        try:
            resp = requests.post(url, data=data, timeout=15)
            if resp.status_code == 200:
                return resp.json().get("access_token")
            else:
                print(f"[Google Drive OAuth] Token error: {resp.text}")
        except Exception as e:
            print(f"[Google Drive OAuth] Connection error: {e}")

    # 2. Fall back to Service Account
    return get_service_account_token()
        
    url = "https://oauth2.googleapis.com/token"
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token"
    }
    
    try:
        resp = requests.post(url, data=data, timeout=15)
        if resp.status_code == 200:
            return resp.json().get("access_token")
        else:
            print(f"[Google Drive] Token exchange error: {resp.text}")
    except Exception as e:
        print(f"[Google Drive] Connection error during token exchange: {e}")
        
    return None


def extract_folder_id_from_url(url_or_id):
    """
    Extracts 33-character Google Drive folder ID from full URL or returns string.
    """
    if not url_or_id:
        return None
    url_str = str(url_or_id).strip()
    match = re.search(r"folders/([a-zA-Z0-9_-]+)", url_str)
    if match:
        return match.group(1)
    match_id = re.search(r"id=([a-zA-Z0-9_-]+)", url_str)
    if match_id:
        return match_id.group(1)
    if len(url_str) >= 20 and "/" not in url_str:
        return url_str
    return None


def list_files_in_folder(folder_id, access_token=None):
    """
    Lists all files in a specific Google Drive folder.
    """
    token = access_token or get_gdrive_access_token()
    if not token:
        return []
        
    url = f"https://www.googleapis.com/drive/v3/files"
    params = {
        "q": f"'{folder_id}' in parents and trashed = false",
        "fields": "files(id, name, mimeType, size, webViewLink)",
        "pageSize": 100,
        "supportsAllDrives": True,
        "includeItemsFromAllDrives": True
    }
    headers = {"Authorization": f"Bearer {token}"}
    
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=20)
        if resp.status_code == 200:
            return resp.json().get("files", [])
    except Exception as e:
        print(f"[Google Drive] Error listing files in folder {folder_id}: {e}")
        
    return []


def download_file_from_drive(file_id, target_path, access_token=None):
    """
    Downloads a binary file from Google Drive to local disk.
    """
    token = access_token or get_gdrive_access_token()
    if not token:
        return False
        
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media&supportsAllDrives=true"
    headers = {"Authorization": f"Bearer {token}"}
    
    try:
        with requests.get(url, headers=headers, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(target_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    f.write(chunk)
        return True
    except Exception as e:
        print(f"[Google Drive] Failed to download file {file_id}: {e}")
        return False


def search_folder_by_name(folder_name, access_token=None, parent_folder_id=None):
    """
    Searches for a folder in Google Drive by its name (exact, case-insensitive, or substring).
    Checks inside parent_folder_id / root_folder_id first, then globally in Drive.
    Returns: (folder_id, folder_name) or (None, None)
    """
    token = access_token or get_gdrive_access_token()
    if not token or not folder_name:
        return None, None
        
    cfg = load_gdrive_config()
    root_parent = parent_folder_id or cfg.get("root_folder_id")
    clean_target = str(folder_name).strip().lower()
    
    # 1. Search inside the Root Folder (PV3D_OrderFiles)
    if root_parent:
        try:
            q = f"'{root_parent}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
            params = {
                "q": q,
                "fields": "files(id, name)",
                "pageSize": 100,
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true"
            }
            resp = requests.get("https://www.googleapis.com/drive/v3/files", headers={"Authorization": f"Bearer {token}"}, params=params, timeout=15)
            if resp.status_code == 200:
                folders = resp.json().get("files", [])
                for f in folders:
                    if f["name"].strip().lower() == clean_target:
                        return f["id"], f["name"]
                for f in folders:
                    if clean_target in f["name"].strip().lower() or f["name"].strip().lower() in clean_target:
                        return f["id"], f["name"]
        except Exception as e:
            print(f"[Google Drive] Error listing folders in root '{root_parent}': {e}")
            
    # 2. Search globally in Drive
    try:
        clean_safe = clean_target.replace("'", "\\'")
        q_global = f"mimeType = 'application/vnd.google-apps.folder' and name contains '{clean_safe}' and trashed = false"
        params_global = {
            "q": q_global,
            "fields": "files(id, name)",
            "pageSize": 20,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true"
        }
        resp = requests.get("https://www.googleapis.com/drive/v3/files", headers={"Authorization": f"Bearer {token}"}, params=params_global, timeout=15)
        if resp.status_code == 200:
            folders = resp.json().get("files", [])
            for f in folders:
                if f["name"].strip().lower() == clean_target:
                    return f["id"], f["name"]
            if folders:
                return folders[0]["id"], folders[0]["name"]
    except Exception as e:
        print(f"[Google Drive] Error searching global folders: {e}")
        
    return None, None


def search_file_by_name(file_name, access_token=None):
    """
    Searches globally in Google Drive for a file with the given name.
    Returns file_id or None.
    """
    token = access_token or get_gdrive_access_token()
    if not token or not file_name:
        return None
        
    clean_name = str(file_name).strip().replace("'", "\\'")
    q = f"name = '{clean_name}' and trashed = false"
    params = {
        "q": q,
        "fields": "files(id, name, mimeType, size)",
        "pageSize": 10,
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true"
    }
    try:
        resp = requests.get("https://www.googleapis.com/drive/v3/files", headers={"Authorization": f"Bearer {token}"}, params=params, timeout=15)
        if resp.status_code == 200:
            files = resp.json().get("files", [])
            if files:
                return files[0]["id"]
    except Exception as e:
        print(f"[Google Drive] Error searching file '{file_name}': {e}")
        
    return None


def find_and_download_so_files(so_doc, models_dir=MODELS_DIR):
    """
    Locates the 3D files for a Sales Order in Google Drive.
    First searches for Google Drive folder named after the Deal Name (custom_deal_name),
    then checks custom_google_drive_link, and falls back to searching Drive for missing parts.
    Returns: (downloaded_files, error_message)
    """
    deal_name = (so_doc.get("custom_deal_name") or so_doc.get("deal_name") or "").strip()
    drive_link = (so_doc.get("custom_google_drive_link") or "").strip()
    so_id = so_doc.get("sales_order") or so_doc.get("name") or "Order"
    
    token = get_gdrive_access_token()
    if not token:
        return [], f"file is not uploaded in the drive: Google Drive authentication failed for {so_id}"
        
    folder_id = None
    folder_name_found = None
    files = []
    
    # 1. Primary Strategy: Search for Drive folder named after Deal Name (custom_deal_name)
    if deal_name:
        fid, fname = search_folder_by_name(deal_name, access_token=token)
        if fid:
            folder_id = fid
            folder_name_found = fname
            files = list_files_in_folder(folder_id, access_token=token)
            if files:
                print(f"[Google Drive] Found Drive folder '{fname}' for deal '{deal_name}' on {so_id} ({len(files)} items).")
                
    # 2. Secondary Strategy: If not found or empty by deal name, try custom_google_drive_link
    if not files and drive_link:
        link_fid = extract_folder_id_from_url(drive_link)
        if link_fid and link_fid != folder_id:
            link_files = list_files_in_folder(link_fid, access_token=token)
            if link_files:
                folder_id = link_fid
                files = link_files
                print(f"[Google Drive] Found files in link folder '{link_fid}' for {so_id} ({len(files)} items).")
                
    # Download any CAD files found in the folder
    cad_files = [f for f in files if any(f.get("name", "").lower().endswith(ext) for ext in [".stl", ".3mf", ".step", ".stp", ".obj"])]
    downloaded = []
    for f in cad_files:
        fname = f.get("name", "")
        dest = models_dir / fname
        if not dest.exists() or dest.stat().st_size == 0:
            print(f"[Google Drive] Downloading {fname} ({f.get('size', 'N/A')} bytes)...")
            if download_file_from_drive(f["id"], dest, access_token=token):
                downloaded.append(dest)
        else:
            downloaded.append(dest)
            
    # 3. Tertiary Strategy: Check missing required parts from custom_file_details
    downloaded_names = {p.name.lower() for p in downloaded}
    req_file_names = [f.get("file_name") for f in so_doc.get("files", []) if f.get("file_name")]
    
    for req_fname in req_file_names:
        if req_fname.lower() not in downloaded_names:
            # Check if file is already in models_dir
            local_cand = models_dir / req_fname
            if local_cand.exists() and local_cand.stat().st_size > 0:
                downloaded.append(local_cand)
                downloaded_names.add(req_fname.lower())
                continue
                
            # Check Downloads folder (e.g. C:\Users\desig\Downloads\...)
            try:
                import shutil
                user_dl = Path.home() / "Downloads" / req_fname
                if user_dl.exists() and user_dl.stat().st_size > 0:
                    shutil.copy2(user_dl, local_cand)
                    downloaded.append(local_cand)
                    downloaded_names.add(req_fname.lower())
                    print(f"[Google Drive] Located '{req_fname}' in user Downloads folder and copied to models.")
                    if folder_id:
                        upload_build_file_to_drive(local_cand, parent_folder_id=folder_id)
                    continue
            except Exception:
                pass
                
            # Search Google Drive globally for this file
            print(f"[Google Drive] Searching Drive for missing file '{req_fname}'...")
            found_fid = search_file_by_name(req_fname, access_token=token)
            if found_fid:
                if download_file_from_drive(found_fid, local_cand, access_token=token):
                    downloaded.append(local_cand)
                    downloaded_names.add(req_fname.lower())
                    print(f"[Google Drive] Downloaded '{req_fname}' from Drive search.")
                    
    if not downloaded:
        return [], f"file is not uploaded in the drive: No 3D models found in Drive for {so_id} (Deal: '{deal_name or 'N/A'}', Folder: '{folder_name_found or drive_link or 'N/A'}')"
        
    return downloaded, None


def create_gdrive_folder(folder_name, parent_folder_id=None):
    """
    Creates a new Google Drive folder and returns its (folder_id, webViewLink).
    """
    cfg = load_gdrive_config()
    target_parent = parent_folder_id or cfg.get("root_folder_id")
    token = get_gdrive_access_token()
    if not token:
        return None, "Authentication failed. Token invalid."
        
    url = "https://www.googleapis.com/drive/v3/files?supportsAllDrives=true"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    
    metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    if target_parent:
        metadata["parents"] = [target_parent]
        
    try:
        r = requests.post(url, json=metadata, headers=headers, timeout=20)
        if r.status_code == 200:
            data = r.json()
            fid = data["id"]
            link = f"https://drive.google.com/drive/folders/{fid}"
            
            # Make readable by anyone with link
            try:
                perm_url = f"https://www.googleapis.com/drive/v3/files/{fid}/permissions?supportsAllDrives=true"
                requests.post(perm_url, json={"role": "reader", "type": "anyone"}, headers=headers, timeout=10)
            except Exception:
                pass
                
            return fid, link
        else:
            return None, r.text
    except Exception as e:
        return None, str(e)


def upload_build_file_to_drive(local_file_path, parent_folder_id=None):
    """
    Uploads a generated .3mf or .gcode build file to Google Drive and returns shareable link.
    """
    cfg = load_gdrive_config()
    target_parent = parent_folder_id or cfg.get("builds_folder_id")
    token = get_gdrive_access_token()
    if not token:
        return None
        
    file_path = Path(local_file_path)
    if not file_path.exists():
        return None
        
    url = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&supportsAllDrives=true"
    headers = {"Authorization": f"Bearer {token}"}
    
    metadata = {
        "name": file_path.name,
        "description": "ProtoValue3D Automated Print Build Project"
    }
    if target_parent:
        metadata["parents"] = [target_parent]
        
    try:
        size_kb = round(file_path.stat().st_size / 1024, 1)
        with open(file_path, "rb") as f_stream:
            files = {
                "data": ("metadata", json_dumps(metadata), "application/json; charset=UTF-8"),
                "file": f_stream
            }
            r = requests.post(url, headers=headers, files=files, timeout=120)
            
        if r.status_code == 200:
            fid = r.json().get("id")
            # Permission
            try:
                requests.post(f"https://www.googleapis.com/drive/v3/files/{fid}/permissions?supportsAllDrives=true", json={"role": "reader", "type": "anyone"}, headers=headers, timeout=10)
            except Exception:
                pass
            print(f"[Google Drive] Uploaded '{file_path.name}' ({size_kb} KB)")
            return f"https://drive.google.com/file/d/{fid}/view?usp=sharing"
        else:
            print(f"[Google Drive] Upload failed for {file_path.name}: {r.status_code} - {r.text}")
    except Exception as e:
        print(f"[Google Drive] Failed uploading build file {file_path.name}: {e}")
        
    return None


def create_build_folder_and_upload(build_name, file_paths, parent_folder_id=None):
    """
    Creates a dedicated folder for the build inside the Builds vault,
    uploads all generated .3mf and .gcode files into it, and returns the folder URL.
    """
    cfg = load_gdrive_config()
    target_parent = parent_folder_id or cfg.get("builds_folder_id") or "1FaF8oZlPjxWT9wedPMIrgjQDDbSiFHIo"
    
    folder_id, folder_url = create_gdrive_folder(build_name, parent_folder_id=target_parent)
    if not folder_id:
        print(f"[Google Drive] Failed creating folder for build {build_name}: {folder_url}")
        return None
        
    print(f"[Google Drive] Created build folder for '{build_name}': {folder_url}")
    
    paths = [file_paths] if isinstance(file_paths, (str, Path)) else file_paths
    for p in paths:
        if p and Path(p).exists():
            upload_build_file_to_drive(p, parent_folder_id=folder_id)
            print(f"[Google Drive] Uploaded {Path(p).name} into build folder.")
            
    return folder_url

def json_dumps(obj):
    import json
    return json.dumps(obj)
