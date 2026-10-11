# 🚀 ProtoValue3D Print Build Planner & OrcaSlicer Automation

An automated local print-farm planning engine and desktop GUI for **ProtoValue3D**, integrating **ERPNext** (`protovalue3d.m.frappe.cloud`), **Google Drive CAD Vault**, and **OrcaSlicer** on your Windows laptop with **zero GPU/3D overhead**.

---

## 🖥️ Running the Application

### Method 1: Desktop GUI (Recommended)
Double-click:
```
run_planner.bat
```
or run:
```powershell
python app_ui.py
```
- **0 GPU Overhead**: Uses lightweight CPU-rendered modern dark theme (~130 MB RAM).
- **Controls**: Filter materials, select target printer, choose max build limit, run simulation dry runs, or execute live ERPNext postings.
- **Real-Time Monitor**: Live pending orders table with high-priority delivery indicators and full execution logs.
- **One-Click Folder & Drive Buttons**: Open the builds output folder or configure Google Drive with a single click.

### Method 2: Command Line (CLI)
```powershell
# Simulation / Dry Run
python build_planner.py --dry-run

# Live Post to ERPNext Print Build (Draft)
python build_planner.py

# Filter by material
python build_planner.py --material PLA

# Re-plan priority queue
python build_planner.py --replan
```

---

## 🎯 Key Features & Solved Requirements

1. **Delivery Date Priority Queuing**:
   - **Tier 1 (High Priority)**: Orders due within the next **3 days** (Today $\to$ Today + 3 days).
   - **Tier 2 (Normal Priority)**: Orders with later delivery dates.
2. **Local Oriented Bounding Box (OBB) Check**:
   - Calculates true $(L, W, H)$ using Principal Component Analysis on the model's convex hull.
   - Strictly enforces that no dimension exceeds **$250\text{ mm}$** for your $256 \times 256 \times 256\text{ mm}$ printers.
3. **Minimum Support Auto-Orientation**:
   - Evaluates planar facets to find the orientation with minimal overhang volume ($> 45^\circ$) and maximum bed adhesion area.
4. **2D Plate Nesting with 7mm Spacing**:
   - Packs parts onto $256 \times 256\text{ mm}$ plates with strict **$7\text{ mm}$ clearance buffer** between parts and bed edges.
5. **Multi-Order Clubbing (Up to 7 Builds)**:
   - Packs Sales Order #1. If completed and total builds $< 7$, pulls from subsequent Sales Orders sharing the same material until 7 builds are generated or orders are exhausted.
6. **Stock Balance in Grams (Fixed)**:
   - Reads ERPNext `Bin.actual_qty` strictly in **grams** (e.g. `4127` = $4127\text{ g}$, not kilograms).
7. **Filament Spool Serial Code Matching (Fixed Error 417)**:
   - Automatically queries `Filament Spool` in ERPNext for active spools matching the material (e.g. `UNQ_PLA_B_001`, `UNQ_PLA_TEST3`) and assigns them directly to `Print Build.spool`.
8. **Google Drive Integration**:
   - **CAD Auto-Download**: Automatically checks the Sales Order's `custom_google_drive_link` or searches Google Drive for the folder named after `custom_deal_name`. Downloads `.stl`, `.3mf`, `.step`, and `.obj` models to `models/`.
   - **Builds Upload**: Uploads generated `.3mf` project files to your designated Google Drive folder and attaches the shareable link to `Print Build.custom_drive_link`.
   - **ERPNext Workflow**:
     - `custom_google_drive_link` carries over automatically: `Lead` $\to$ `Opportunity` $\to$ `Quotation` $\to$ `Sales Order`.
     - In `Opportunity` and `Sales Order`: If the field is empty, a button appears under **Create** $\to$ **Google Drive Folder** to easily link/save the CAD folder URL.
     - When filled, a quick action button **Open CAD Drive Folder** lets you jump to Google Drive in 1 click.

---

## 🔑 How to Change Google Drive Account & Folder

To connect your new Google Account and change the storage folder:

### Step 1: Run the Interactive Setup Assistant
In terminal:
```powershell
python setup_google_drive.py
```
*(Or click the **☁️ Google Drive Connect** button inside the Desktop GUI).*

### Step 2: Choose Mode [1] (Browser OAuth)
1. The assistant opens your default web browser to the Google sign-in page.
2. Sign in with your **new Google Account**.
3. Click **Continue / Allow** to grant Google Drive file access.
4. The local listener on your laptop automatically captures the authorization and exchanges it for a fresh permanent **Refresh Token**!

### Step 3: Enter Your New Google Drive Folder ID
1. In your new Google Drive, create two folders (or one main folder):
   - A folder for Customer Deals/Orders (Root Folder).
   - A dedicated folder for generated 3D Print Builds (Builds Folder).
2. Open the folder in your browser and copy the ID from the URL:
   - `https://drive.google.com/drive/folders/`**`1ABC_xyz123...`**
3. Paste this ID into the setup prompt.
4. The assistant automatically saves the settings to `gdrive_config.json`:
   ```json
   {
       "client_id": "...",
       "client_secret": "...",
       "refresh_token": "...",
       "root_folder_id": "...",
       "builds_folder_id": "..."
   }
   ```
5. You're done! All subsequent slice operations and ERPNext postings will use this new Google account.

---

## 📂 Project Structure

```text
protovalue3D print planner/
├── app_ui.py             # Modern lightweight CustomTkinter Desktop GUI
├── run_planner.bat       # One-click desktop launcher
├── build_planner.py      # Core CLI planning engine
├── gdrive_client.py      # Google Drive API downloader, uploader & folder linker
├── setup_google_drive.py # Interactive Google Account connection assistant
├── gdrive_config.json    # Local credentials storage for Google Drive
├── erpnext_sync.py       # REST API connector for Sales Orders, Spools, and Print Builds
├── config.py             # Bed dimensions, spacing, thresholds, and paths
├── geometry.py           # OBB bounding box (local coords) & minimal support orientation
├── nester.py             # 2D build plate packing with 7mm clearance
├── slicer_bridge.py      # OrcaSlicer 3MF project export and CLI slicing
├── notifier.py           # Material shortage email alerts
├── models/               # Local cache of CAD models downloaded from Google Drive
├── builds/               # Generated OrcaSlicer .3mf projects and G-code
└── queued_builds/        # Builds held locally due to material shortage
```
