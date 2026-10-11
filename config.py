import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# --- Directories ---
MODELS_DIR = BASE_DIR / "models"
BUILDS_DIR = BASE_DIR / "builds"
QUEUE_DIR = BASE_DIR / "queued_builds"

for d in [MODELS_DIR, BUILDS_DIR, QUEUE_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# --- ERPNext Configuration ---
ERPNEXT_URL = os.environ.get("ERPNEXT_URL", "https://protovalue3d.m.frappe.cloud")
ERPNEXT_API_KEY = os.environ.get("ERPNEXT_API_KEY", "8bb5cdb1d2ab502")
ERPNEXT_API_SECRET = os.environ.get("ERPNEXT_API_SECRET", "f9cfff5f7186691")
ERPNEXT_HEADERS = {
    "Authorization": f"token {ERPNEXT_API_KEY}:{ERPNEXT_API_SECRET}",
    "Content-Type": "application/json",
}

# --- Slicer Configuration ---
# Supports installed OrcaSlicer or portable folder inside this project or Downloads
POSSIBLE_ORCA_PATHS = [
    r"C:\Program Files\OrcaSlicer\orca-slicer.exe",
    str(BASE_DIR / "OrcaSlicer" / "orca-slicer.exe"),
    r"C:\Users\DELL\Downloads\OrcaSlicer\orca-slicer.exe",
    r"C:\Users\DELL\Downloads\OrcaSlicer_Windows_V2.3.1_Portable\orca-slicer.exe",
]

ORCA_SLICER_PATH = None
for p in POSSIBLE_ORCA_PATHS:
    if os.path.exists(p):
        ORCA_SLICER_PATH = p
        break

# --- 3D Printer & Build Plate Configuration ---
# 3 printers: 256 x 256 x 256 mm (e.g. Bambu Lab X1C / P1S / Elegoo Centauri Carbon)
BED_SIZE_X = 256.0  # mm
BED_SIZE_Y = 256.0  # mm
BED_SIZE_Z = 256.0  # mm
MAX_PART_DIMENSION = 250.0  # mm (safe bounding limit: length, width, height <= 250mm)
PART_SPACING = 7.0  # mm (spacing between parts and bed border)
MAX_BUILDS = 7  # Maximum builds to generate per planning session
PRIORITY_DAYS_THRESHOLD = 3  # Orders ending in next 3 days are Priority 1

# Workstations / Printers in ERPNext
DEFAULT_PRINTER = "ElegooCentauriCarbon_1"
AVAILABLE_PRINTERS = [
    "ElegooCentauriCarbon_1",
    "ElegooCentauriCarbon_2",
    "ElegooCentauriCarbon_3",
    "ElegooCentauriCarbon_4",
    "ElegooCentauriCarbon_5",
]

# Turnaround & Setup intervals
TURNAROUND_SAME_SPOOL_MINUTES = 10   # 10 min for removing parts, cleaning build plate, etc.
TURNAROUND_SPOOL_CHANGE_MINUTES = 15 # 15 min when changing filament spool

# Material Priority Hierarchy (when deadlines are identical)
# Strict user priority: PLA > PETG > ABS > TPU > any other
MATERIAL_PRIORITY = ["PLA", "PETG", "ABS", "TPU"]

# Spool Depletion Threshold: Grams remaining below which a spool is considered completed/exhausted
DEPLETION_THRESHOLD_GRAMS = 25.0

# Practical maximum filament payload per build plate (Elegoo Centauri Carbon 256x256 bed)
BED_MAX_WEIGHT_GRAMS = 450.0

# --- Notification Configuration ---
ALERT_EMAIL_RECIPIENT = "info@protovalue3d.in"
SMTP_HOST = "smtp.hostinger.com"
SMTP_PORT = 465
SMTP_USER = "info@protovalue3d.in"
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "Protov@217")
