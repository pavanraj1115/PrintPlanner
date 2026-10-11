import os
import sys
import threading
import queue
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

# Ensure UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import customtkinter as ctk

from config import (
    BASE_DIR, BUILDS_DIR, QUEUE_DIR, MODELS_DIR,
    DEFAULT_PRINTER, PART_SPACING, MAX_BUILDS,
    BED_SIZE_X, BED_SIZE_Y, BED_SIZE_Z, MAX_PART_DIMENSION,
    AVAILABLE_PRINTERS, ORCA_SLICER_PATH
)
from erpnext_sync import (
    get_pending_sales_orders,
    get_available_spools_and_stock,
    get_available_printers,
    post_print_build_draft,
    update_print_build_drive_link,
    cancel_draft_builds,
    slide_unstarted_builds_and_cascade,
    call_erpnext
)
from geometry import (
    check_oriented_bounding_box,
    auto_orient_for_minimum_support,
    get_part_2d_footprint
)
from nester import BuildNester
from slicer_bridge import (
    generate_build_project_3mf,
    slice_plate_with_orcaslicer,
    save_queued_build,
    estimate_mesh_filament_weight,
    estimate_print_time_hours
)
from notifier import send_material_shortage_alert
from build_planner import run_planner, find_or_create_mesh
from gdrive_client import find_and_download_so_files, upload_build_file_to_drive, create_build_folder_and_upload
from gantt_view import GanttChartWindow


class PrintPlannerApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        # --- Window Setup ---
        self.title("ProtoValue3D — Multi-Printer Build Planner & Slicer Orchestrator")
        self.geometry("1220x780")
        self.minsize(1020, 700)

        # Set modern dark theme
        ctk.set_appearance_mode("Dark")
        ctk.set_default_color_theme("blue")

        self.log_queue = queue.Queue()
        self.is_running = False
        self.pending_orders = []
        self.last_planned_plates = None
        self.gantt_window = None

        # Printer & Sales Order selection states
        self.printer_vars = {}        # {printer_name: BooleanVar}
        self.so_checkbox_vars = {}    # {so_name: BooleanVar}

        self._build_ui()
        self._start_log_consumer()

        # Initial fetch of printers and pending orders
        self.after(200, self._load_initial_printers)
        self.after(500, self.fetch_orders_thread)

    def _load_initial_printers(self):
        try:
            discovered = get_available_printers()
        except Exception:
            discovered = list(AVAILABLE_PRINTERS)
        self._refresh_printer_checkboxes(discovered)

    def _build_ui(self):
        # Configure layout grid
        self.grid_columnconfigure(0, weight=0, minsize=320)  # Sidebar
        self.grid_columnconfigure(1, weight=1)                # Main content
        self.grid_rowconfigure(0, weight=1)

        # =========================================================================
        # 1. SIDEBAR CONTROLS (Scrollable for universal display fit)
        # =========================================================================
        sidebar = ctk.CTkScrollableFrame(self, corner_radius=0, width=320, fg_color="#0f172a")
        sidebar.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)

        # Brand / Title
        brand_label = ctk.CTkLabel(
            sidebar,
            text="ProtoValue3D",
            font=ctk.CTkFont(size=22, weight="bold"),
            text_color="#38bdf8"
        )
        brand_label.pack(anchor="w", padx=16, pady=(16, 2))

        sub_label = ctk.CTkLabel(
            sidebar,
            text="Multi-Printer Build Planner & Slicer Bridge",
            font=ctk.CTkFont(size=11),
            text_color="#94a3b8"
        )
        sub_label.pack(anchor="w", padx=16, pady=(0, 12))

        # Divider
        ctk.CTkFrame(sidebar, height=2, fg_color="#334155").pack(fill="x", padx=12, pady=4)

        # -------------------------------------------------------------------------
        # Printer Multi-Selection
        # -------------------------------------------------------------------------
        printer_header_frame = ctk.CTkFrame(sidebar, fg_color="transparent")
        printer_header_frame.pack(fill="x", padx=16, pady=(8, 2))
        
        ctk.CTkLabel(
            printer_header_frame,
            text="🖨️ Active Printers:",
            font=ctk.CTkFont(weight="bold", size=13)
        ).pack(side="left")

        btn_prt_all = ctk.CTkButton(
            printer_header_frame, text="All", width=36, height=20, font=ctk.CTkFont(size=10),
            fg_color="#334155", hover_color="#475569", command=self._select_all_printers
        )
        btn_prt_all.pack(side="right", padx=(2, 0))

        btn_prt_none = ctk.CTkButton(
            printer_header_frame, text="None", width=38, height=20, font=ctk.CTkFont(size=10),
            fg_color="#334155", hover_color="#475569", command=self._clear_all_printers
        )
        btn_prt_none.pack(side="right", padx=(2, 2))

        # Scrollable container for printer checkboxes
        self.printers_container = ctk.CTkFrame(sidebar, fg_color="#1e293b", corner_radius=6)
        self.printers_container.pack(fill="x", padx=16, pady=(2, 4))

        # + Add Printer button
        self.btn_add_printer = ctk.CTkButton(
            sidebar,
            text="➕ Add New Printer Workstation",
            height=24,
            font=ctk.CTkFont(size=11),
            fg_color="#0f766e",
            hover_color="#115e59",
            command=self._prompt_add_printer
        )
        self.btn_add_printer.pack(fill="x", padx=16, pady=(0, 8))

        # -------------------------------------------------------------------------
        # Target Completion Date (Optional)
        # -------------------------------------------------------------------------
        ctk.CTkLabel(
            sidebar,
            text="📅 Target Completion Date (Optional):",
            font=ctk.CTkFont(weight="bold", size=12)
        ).pack(anchor="w", padx=16, pady=(6, 2))

        date_input_frame = ctk.CTkFrame(sidebar, fg_color="transparent")
        date_input_frame.pack(fill="x", padx=16, pady=(0, 2))

        self.completion_date_entry = ctk.CTkEntry(
            date_input_frame,
            placeholder_text="YYYY-MM-DD (e.g. 2026-10-12)",
            height=28
        )
        self.completion_date_entry.pack(fill="x")

        # Quick preset buttons
        preset_frame = ctk.CTkFrame(sidebar, fg_color="transparent")
        preset_frame.pack(fill="x", padx=16, pady=(2, 8))

        ctk.CTkButton(preset_frame, text="Today", width=50, height=20, font=ctk.CTkFont(size=10),
                      fg_color="#334155", hover_color="#475569", command=lambda: self._set_target_date(0)).pack(side="left", padx=(0, 2))
        ctk.CTkButton(preset_frame, text="+1 Day", width=52, height=20, font=ctk.CTkFont(size=10),
                      fg_color="#334155", hover_color="#475569", command=lambda: self._set_target_date(1)).pack(side="left", padx=2)
        ctk.CTkButton(preset_frame, text="+3 Days", width=52, height=20, font=ctk.CTkFont(size=10),
                      fg_color="#334155", hover_color="#475569", command=lambda: self._set_target_date(3)).pack(side="left", padx=2)
        ctk.CTkButton(preset_frame, text="Clear", width=44, height=20, font=ctk.CTkFont(size=10),
                      fg_color="#475569", hover_color="#64748b", command=lambda: self.completion_date_entry.delete(0, "end")).pack(side="left", padx=(2, 0))

        # -------------------------------------------------------------------------
        # Material Multi-Selection
        # -------------------------------------------------------------------------
        mat_header_frame = ctk.CTkFrame(sidebar, fg_color="transparent")
        mat_header_frame.pack(fill="x", padx=16, pady=(6, 2))

        ctk.CTkLabel(
            mat_header_frame,
            text="🧵 Materials (Multi-Select):",
            font=ctk.CTkFont(weight="bold", size=12)
        ).pack(side="left")

        btn_mat_all = ctk.CTkButton(
            mat_header_frame, text="All", width=36, height=20, font=ctk.CTkFont(size=10),
            fg_color="#334155", hover_color="#475569", command=self._select_all_materials
        )
        btn_mat_all.pack(side="right", padx=(2, 0))

        btn_mat_none = ctk.CTkButton(
            mat_header_frame, text="None", width=38, height=20, font=ctk.CTkFont(size=10),
            fg_color="#334155", hover_color="#475569", command=self._clear_all_materials
        )
        btn_mat_none.pack(side="right", padx=(2, 2))

        # Container for material checkboxes
        self.materials_container = ctk.CTkFrame(sidebar, fg_color="#1e293b", corner_radius=6)
        self.materials_container.pack(fill="x", padx=16, pady=(2, 8))

        self.material_vars = {}
        for m in ["PLA", "PETG", "ABS", "TPU"]:
            self.material_vars[m] = ctk.BooleanVar(value=True)
            cb = ctk.CTkCheckBox(
                self.materials_container,
                text=m,
                variable=self.material_vars[m],
                font=ctk.CTkFont(size=11),
                height=22,
                checkbox_height=16,
                checkbox_width=16
            )
            cb.pack(anchor="w", padx=8, pady=3)

        # Max Builds Target
        ctk.CTkLabel(sidebar, text="Max Target Builds (Plates):", font=ctk.CTkFont(weight="bold", size=12)).pack(anchor="w", padx=16, pady=(4, 2))
        self.max_builds_var = ctk.StringVar(value="7")
        self.max_builds_dropdown = ctk.CTkOptionMenu(
            sidebar,
            variable=self.max_builds_var,
            values=["1", "2", "3", "4", "5", "6", "7", "8", "9", "10"]
        )
        self.max_builds_dropdown.pack(fill="x", padx=16, pady=(0, 8))

        # Spacing info
        spacing_frame = ctk.CTkFrame(sidebar, fg_color="transparent")
        spacing_frame.pack(fill="x", padx=16, pady=(4, 8))
        ctk.CTkLabel(spacing_frame, text="Part Spacing:", font=ctk.CTkFont(size=12)).pack(side="left")
        self.spacing_label = ctk.CTkLabel(spacing_frame, text=f"{PART_SPACING} mm", text_color="#38bdf8", font=ctk.CTkFont(weight="bold"))
        self.spacing_label.pack(side="right")

        # Divider
        ctk.CTkFrame(sidebar, height=2, fg_color="#334155").pack(fill="x", padx=12, pady=4)

        # -------------------------------------------------------------------------
        # Action Buttons
        # -------------------------------------------------------------------------
        self.btn_refresh = ctk.CTkButton(
            sidebar,
            text="🔄 Refresh Orders (ERPNext)",
            command=self.fetch_orders_thread,
            fg_color="#334155",
            hover_color="#475569"
        )
        self.btn_refresh.pack(fill="x", padx=16, pady=(8, 5))

        self.btn_dry_run = ctk.CTkButton(
            sidebar,
            text="🧪 Test Run (Simulation)",
            command=lambda: self.run_planner_thread(dry_run=True),
            fg_color="#0284c7",
            hover_color="#0369a1"
        )
        self.btn_dry_run.pack(fill="x", padx=16, pady=5)

        self.btn_execute = ctk.CTkButton(
            sidebar,
            text="🚀 Generate & Post Draft Builds",
            command=lambda: self.run_planner_thread(dry_run=False),
            fg_color="#16a34a",
            hover_color="#15803d",
            font=ctk.CTkFont(weight="bold")
        )
        self.btn_execute.pack(fill="x", padx=16, pady=5)

        self.btn_replan = ctk.CTkButton(
            sidebar,
            text="⚡ Re-Plan (Rush Order)",
            command=lambda: self.run_planner_thread(dry_run=False, replan=True),
            fg_color="#d97706",
            hover_color="#b45309"
        )
        self.btn_replan.pack(fill="x", padx=16, pady=5)

        self.btn_gantt = ctk.CTkButton(
            sidebar,
            text="📊 Production Gantt View",
            command=self.open_gantt_view,
            fg_color="#6366f1",
            hover_color="#4f46e5",
            font=ctk.CTkFont(weight="bold")
        )
        self.btn_gantt.pack(fill="x", padx=16, pady=5)

        self.btn_open_folder = ctk.CTkButton(
            sidebar,
            text="📂 Open Builds Folder",
            command=self.open_builds_folder,
            fg_color="#1e293b",
            hover_color="#334155"
        )
        self.btn_open_folder.pack(fill="x", padx=16, pady=5)

        self.btn_gdrive = ctk.CTkButton(
            sidebar,
            text="☁️ Google Drive Connect",
            command=self.open_gdrive_setup,
            fg_color="#0369a1",
            hover_color="#0284c7"
        )
        self.btn_gdrive.pack(fill="x", padx=16, pady=(5, 12))

        # Status footer
        self.slicer_status_label = ctk.CTkLabel(
            sidebar,
            text=f"Slicer: {'OrcaSlicer CLI Ready' if ORCA_SLICER_PATH else 'Geometric Engine Active'}",
            font=ctk.CTkFont(size=11),
            text_color="#10b981" if ORCA_SLICER_PATH else "#f59e0b"
        )
        self.slicer_status_label.pack(side="bottom", pady=8)

        # =========================================================================
        # 2. MAIN CONTENT AREA
        # =========================================================================
        main_frame = ctk.CTkFrame(self, corner_radius=0, fg_color="#0f172a")
        main_frame.grid(row=0, column=1, sticky="nsew", padx=0, pady=0)
        main_frame.grid_columnconfigure(0, weight=1)
        main_frame.grid_rowconfigure(2, weight=1)  # Table section
        main_frame.grid_rowconfigure(4, weight=1)  # Console log section

        # Header Info Bar
        info_bar = ctk.CTkFrame(main_frame, fg_color="#1e293b", height=45, corner_radius=8)
        info_bar.grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 6))
        
        self.stat_orders = ctk.CTkLabel(info_bar, text="Pending Orders: 0", font=ctk.CTkFont(weight="bold"))
        self.stat_orders.pack(side="left", padx=16)

        self.stat_parts = ctk.CTkLabel(info_bar, text="Parts in Queue: 0", font=ctk.CTkFont(weight="bold"))
        self.stat_parts.pack(side="left", padx=16)

        self.stat_material = ctk.CTkLabel(info_bar, text="Stores Stock: Checking...", font=ctk.CTkFont(weight="bold"), text_color="#38bdf8")
        self.stat_material.pack(side="right", padx=16)

        # Table Control Top Bar
        table_top_bar = ctk.CTkFrame(main_frame, fg_color="transparent")
        table_top_bar.grid(row=1, column=0, sticky="ew", padx=16, pady=(4, 2))

        table_label = ctk.CTkLabel(
            table_top_bar,
            text="📋 Pending Sales Orders & 3D Files (ERPNext)",
            font=ctk.CTkFont(size=14, weight="bold")
        )
        table_label.pack(side="left")

        btn_gantt_top = ctk.CTkButton(
            table_top_bar, text="📊 Gantt View", width=110, height=24, font=ctk.CTkFont(size=11, weight="bold"),
            fg_color="#6366f1", hover_color="#4f46e5", command=self.open_gantt_view
        )
        btn_gantt_top.pack(side="right", padx=(4, 0))

        btn_sel_all_so = ctk.CTkButton(
            table_top_bar, text="✓ Select All Orders", width=125, height=24, font=ctk.CTkFont(size=11),
            fg_color="#334155", hover_color="#475569", command=self._select_all_so
        )
        btn_sel_all_so.pack(side="right", padx=(4, 0))

        btn_clr_so = ctk.CTkButton(
            table_top_bar, text="✗ Clear Selection", width=115, height=24, font=ctk.CTkFont(size=11),
            fg_color="#334155", hover_color="#475569", command=self._clear_all_so
        )
        btn_clr_so.pack(side="right", padx=4)

        # Scrollable table container
        self.table_frame = ctk.CTkScrollableFrame(main_frame, fg_color="#1e293b", corner_radius=8, height=190)
        self.table_frame.grid(row=2, column=0, sticky="nsew", padx=16, pady=(0, 8))
        self.table_frame.grid_columnconfigure((0, 1, 2, 3, 4, 5, 6, 7), weight=1)

        self._render_table_headers()

        # Log Section
        log_header = ctk.CTkLabel(main_frame, text="🖥️ Live Execution & Slicing Process Log", font=ctk.CTkFont(size=14, weight="bold"))
        log_header.grid(row=3, column=0, sticky="w", padx=20, pady=(4, 2))

        self.log_textbox = ctk.CTkTextbox(
            main_frame,
            fg_color="#090d16",
            text_color="#e2e8f0",
            font=ctk.CTkFont(family="Consolas", size=12),
            wrap="word",
            corner_radius=8
        )
        self.log_textbox.grid(row=4, column=0, sticky="nsew", padx=16, pady=(0, 14))

    # =========================================================================
    # PRINTER CHECKBOXES MANAGEMENT
    # =========================================================================
    def _refresh_printer_checkboxes(self, printer_names):
        for widget in self.printers_container.winfo_children():
            widget.destroy()

        for name in printer_names:
            if name not in self.printer_vars:
                self.printer_vars[name] = ctk.BooleanVar(value=True)
            
            cb = ctk.CTkCheckBox(
                self.printers_container,
                text=name,
                variable=self.printer_vars[name],
                font=ctk.CTkFont(size=11),
                height=22,
                checkbox_height=16,
                checkbox_width=16
            )
            cb.pack(anchor="w", padx=8, pady=3)

    def _select_all_printers(self):
        for var in self.printer_vars.values():
            var.set(True)

    def _clear_all_printers(self):
        for var in self.printer_vars.values():
            var.set(False)

    def _select_all_materials(self):
        for var in self.material_vars.values():
            var.set(True)

    def _clear_all_materials(self):
        for var in self.material_vars.values():
            var.set(False)

    def _prompt_add_printer(self):
        dialog = ctk.CTkInputDialog(
            title="Add Printer Workstation",
            text="Enter new Printer Workstation Name:\n(e.g. ElegooCentauriCarbon_4 or BambuLab_X1C_2)"
        )
        new_name = dialog.get_input()
        if new_name:
            clean_name = new_name.strip()
            if clean_name:
                self.printer_vars[clean_name] = ctk.BooleanVar(value=True)
                self._refresh_printer_checkboxes(list(self.printer_vars.keys()))
                self.log(f"➕ Added custom printer workstation: '{clean_name}' (selected)")

    def _set_target_date(self, days_ahead):
        target = (datetime.now().date() + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
        self.completion_date_entry.delete(0, "end")
        self.completion_date_entry.insert(0, target)

    # =========================================================================
    # SALES ORDER SELECTION & TABLE RENDERING
    # =========================================================================
    def _select_all_so(self):
        for var in self.so_checkbox_vars.values():
            var.set(True)

    def _clear_all_so(self):
        for var in self.so_checkbox_vars.values():
            var.set(False)

    def _render_table_headers(self):
        for widget in self.table_frame.winfo_children():
            widget.destroy()

        headers = ["Plan?", "Sales Order", "Customer", "Delivery Date", "Priority Tier", "Part / File Name", "Material", "Qty"]
        for col_idx, h in enumerate(headers):
            lbl = ctk.CTkLabel(
                self.table_frame,
                text=h,
                font=ctk.CTkFont(weight="bold", size=12),
                text_color="#94a3b8"
            )
            lbl.grid(row=0, column=col_idx, padx=6, pady=6, sticky="w")

    def _update_table_rows(self, orders):
        self._render_table_headers()
        row_num = 1
        total_parts = 0

        # Preserve / initialize checkbox vars
        for so in orders:
            so_name = so["sales_order"]
            if so_name not in self.so_checkbox_vars:
                self.so_checkbox_vars[so_name] = ctk.BooleanVar(value=True)

        for so in orders:
            so_name = so["sales_order"]
            so_var = self.so_checkbox_vars[so_name]
            so_files = so["files"]
            
            # Checkbox on first row of this Sales Order
            cb = ctk.CTkCheckBox(
                self.table_frame,
                text="",
                variable=so_var,
                width=24,
                checkbox_height=16,
                checkbox_width=16
            )
            cb.grid(row=row_num, column=0, padx=6, pady=3, sticky="w")

            for f_idx, f in enumerate(so_files):
                total_parts += int(f.get("quantity") or 1)
                
                # Priority color
                p_color = "#f87171" if so["priority"] == 1 else "#38bdf8"
                p_text = "⚡ HIGH (≤3 Days)" if so["priority"] == 1 else "NORMAL"

                if f_idx > 0:
                    # Secondary file row for same SO
                    ctk.CTkLabel(self.table_frame, text="↳", text_color="#64748b", font=ctk.CTkFont(size=11)).grid(row=row_num, column=0, padx=6, pady=3, sticky="e")

                ctk.CTkLabel(self.table_frame, text=so["sales_order"], font=ctk.CTkFont(size=11, weight="bold")).grid(row=row_num, column=1, padx=6, pady=3, sticky="w")
                ctk.CTkLabel(self.table_frame, text=f.get("customer_name") or "—", font=ctk.CTkFont(size=11)).grid(row=row_num, column=2, padx=6, pady=3, sticky="w")
                ctk.CTkLabel(self.table_frame, text=so["delivery_date"] or "No Date", font=ctk.CTkFont(size=11)).grid(row=row_num, column=3, padx=6, pady=3, sticky="w")
                ctk.CTkLabel(self.table_frame, text=p_text, text_color=p_color, font=ctk.CTkFont(size=11, weight="bold")).grid(row=row_num, column=4, padx=6, pady=3, sticky="w")
                ctk.CTkLabel(self.table_frame, text=f["file_name"], font=ctk.CTkFont(size=11, weight="bold")).grid(row=row_num, column=5, padx=6, pady=3, sticky="w")
                ctk.CTkLabel(self.table_frame, text=f["material_general"], font=ctk.CTkFont(size=11)).grid(row=row_num, column=6, padx=6, pady=3, sticky="w")
                ctk.CTkLabel(self.table_frame, text=str(f.get("quantity", 1)), font=ctk.CTkFont(size=11)).grid(row=row_num, column=7, padx=6, pady=3, sticky="w")
                
                row_num += 1

        self.stat_orders.configure(text=f"Pending Orders: {len(orders)}")
        self.stat_parts.configure(text=f"Parts in Queue: {total_parts}")

        if not orders:
            empty_lbl = ctk.CTkLabel(
                self.table_frame,
                text="✅ No pending unbuilt Sales Orders found in ERPNext.",
                text_color="#10b981",
                font=ctk.CTkFont(size=13)
            )
            empty_lbl.grid(row=1, column=0, columnspan=8, pady=20)

    def log(self, text):
        self.log_queue.put(text)

    def _start_log_consumer(self):
        while not self.log_queue.empty():
            try:
                line = self.log_queue.get_nowait()
                self.log_textbox.insert("end", line + "\n")
                self.log_textbox.see("end")
            except queue.Empty:
                break
        self.after(80, self._start_log_consumer)

    def open_builds_folder(self):
        os.startfile(str(BUILDS_DIR))

    def open_gdrive_setup(self):
        subprocess.Popen(["cmd.exe", "/c", "start", "python", "setup_google_drive.py"], cwd=str(BASE_DIR))

    def open_gantt_view(self):
        if self.gantt_window is not None and self.gantt_window.winfo_exists():
            self.gantt_window.lift()
            self.gantt_window.focus_force()
            self.gantt_window.refresh()
        else:
            self.gantt_window = GanttChartWindow(self)

    # =========================================================================
    # BACKGROUND THREAD ACTIONS
    # =========================================================================
    def fetch_orders_thread(self):
        if self.is_running:
            return
        threading.Thread(target=self._fetch_orders_worker, daemon=True).start()

    def _fetch_orders_worker(self):
        self.is_running = True
        self.log(f"[{datetime.now().strftime('%H:%M:%S')}] Connecting to ERPNext to fetch pending Sales Orders...")
        try:
            # Auto-align any overdue unstarted builds in ERPNext to real-time clock
            slide_unstarted_builds_and_cascade(dry_run=False, log_func=self.log)
            orders = get_pending_sales_orders()
            self.pending_orders = orders
            self.after(0, lambda: self._update_table_rows(orders))
            self.log(f"[{datetime.now().strftime('%H:%M:%S')}] Successfully fetched {len(orders)} open Sales Order(s).")
            
            # Check stock overview for PLA
            stock_g, spool, _ = get_available_spools_and_stock("JMG_PLA_B")
            self.after(0, lambda: self.stat_material.configure(text=f"PLA Stores Stock: {stock_g/1000:.1f} kg"))
        except Exception as e:
            self.log(f"❌ Error fetching from ERPNext: {e}")
        finally:
            self.is_running = False

    def run_planner_thread(self, dry_run=False, replan=False):
        if self.is_running:
            messagebox.showinfo("In Progress", "A planning process is already actively running.")
            return

        # Validate printer selection
        selected_printers = [p for p, v in self.printer_vars.items() if v.get()]
        if not selected_printers:
            messagebox.showwarning("No Printer Selected", "Please select at least one 3D printer workstation.")
            return

        # Validate Material selection
        selected_materials = [m for m, v in self.material_vars.items() if v.get()]
        if not selected_materials:
            messagebox.showwarning("No Material Selected", "Please select at least one material (PLA, PETG, ABS, TPU).")
            return

        # Validate Sales Order selection
        selected_so_list = [so for so, v in self.so_checkbox_vars.items() if v.get()]
        if not selected_so_list and self.pending_orders:
            messagebox.showwarning("No Sales Order Selected", "Please select at least one Sales Order using the checkboxes.")
            return

        threading.Thread(target=self._run_planner_worker, args=(dry_run, replan, selected_printers, selected_materials, selected_so_list), daemon=True).start()

    def _run_planner_worker(self, dry_run, replan, selected_printers, selected_materials, selected_so_list):
        self.is_running = True
        max_builds = int(self.max_builds_var.get())
        target_date_val = self.completion_date_entry.get().strip() or None

        try:
            planned_plates = run_planner(
                selected_printers=selected_printers,
                selected_materials=selected_materials,
                selected_sales_orders=selected_so_list,
                target_completion_date=target_date_val,
                max_builds=max_builds,
                dry_run=dry_run,
                replan=replan,
                log_func=self.log
            )
            self.last_planned_plates = planned_plates
            # Refresh orders table after planning
            self.fetch_orders_thread()

            # Automatically update Gantt Chart if window is already open
            if self.gantt_window is not None and self.gantt_window.winfo_exists():
                self.after(0, self.gantt_window.refresh)

            # If delayed, show interactive popup with the projected ideal completion date & time
            summary = getattr(planned_plates, "completion_summary", None)
            if summary and summary.get("is_delayed"):
                target_str = summary.get("target_date") or "Desired Deadline"
                finish_dt = summary.get("ideal_finish_dt")
                delay_h = summary.get("delay_hours", 0.0)
                delay_d = delay_h / 24.0
                finish_fmt = finish_dt.strftime("%A, %d %B %Y at %I:%M %p") if finish_dt else "N/A"

                self.after(0, lambda: messagebox.showwarning(
                    "Target Completion Date Exceeded",
                    f"⚠️ Orders in this plan CANNOT be completed by the desired deadline ({target_str}).\n\n"
                    f"📅 Projected Ideal Completion Date & Time:\n"
                    f"{finish_fmt}\n\n"
                    f"⏱️ Estimated Production Delay: +{delay_h:.1f} hours (~{delay_d:.1f} days past deadline).\n\n"
                    f"Please check the Terminal Log for order-by-order breakdown and recovery options."
                ))
        except Exception as e:
            self.log(f"\n❌ Error during execution: {e}")
            import traceback
            self.log(traceback.format_exc())
        finally:
            self.is_running = False


if __name__ == "__main__":
    app = PrintPlannerApp()
    app.mainloop()
