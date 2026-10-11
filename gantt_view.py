import os
import sys
import json
from datetime import datetime, timedelta
import tkinter as tk
from tkinter import ttk, messagebox
import customtkinter as ctk

# Ensure UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from config import AVAILABLE_PRINTERS
from erpnext_sync import (
    get_all_active_print_builds,
    get_available_printers,
    slide_unstarted_builds_and_cascade
)


class GanttChartWindow(ctk.CTkToplevel):
    """
    Dedicated interactive Gantt Chart window for ProtoValue3D.
    Visualizes:
      - Printer workstations as swimlanes (Y-axis)
      - Timeline with dates and hours (X-axis)
      - Ideal start & ideal finish times for each build
      - Exact Spool ID involved and filament usage / balance
      - Spool stickiness / continuity indicators across builds
      - Target completion deadline milestone line
      - Interactive click-to-inspect build details panel
    """
    def __init__(self, parent_app=None):
        super().__init__(parent_app)
        self.parent_app = parent_app

        # Window configuration
        self.title("ProtoValue3D — Multi-Workstation Production Gantt Chart")
        self.geometry("1280x780")
        self.minsize(1050, 650)
        
        ctk.set_appearance_mode("Dark")
        self.configure(fg_color="#0b0f19")

        # Bring to focus
        self.after(100, self.lift)
        self.after(100, self.focus_force)

        # State variables
        self.source_var = ctk.StringVar(value="📊 Current Optimization Plan")
        self.printer_filter_var = ctk.StringVar(value="All Printers")
        self.material_filter_var = ctk.StringVar(value="All Materials")
        self.zoom_var = ctk.StringVar(value="Standard (50 px/h)")
        self.show_deadline_var = ctk.BooleanVar(value=True)

        self.zoom_scales = {
            "Compact (30 px/h)": 30,
            "Standard (50 px/h)": 50,
            "Expanded (80 px/h)": 80,
            "Ultra (120 px/h)": 120
        }

        self.current_builds = []
        self.selected_build = None
        self.lane_height = 82
        self.header_width = 230
        self.ruler_height = 46

        # Build UI layout
        self._build_header_controls()
        self._build_kpi_summary_bar()
        self._build_gantt_canvas_area()
        self._build_detail_inspector_panel()

        # Handle window close
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Initial data load
        self.after(150, self.refresh)

        # Background live clock auto-ticker (aligns unstarted builds every 30s)
        self._auto_tick_id = None
        self._schedule_auto_ticker()

    def _on_close(self):
        if self._auto_tick_id is not None:
            try:
                self.after_cancel(self._auto_tick_id)
            except Exception:
                pass
            self._auto_tick_id = None
        if self.parent_app and hasattr(self.parent_app, "gantt_window"):
            self.parent_app.gantt_window = None
        self.destroy()

    def _schedule_auto_ticker(self):
        # Auto-align unstarted builds and refresh schedule every 30 seconds
        self._auto_tick_id = self.after(30000, self._on_auto_tick)

    def _on_auto_tick(self):
        try:
            if "Live ERPNext Builds" in self.source_var.get():
                slide_unstarted_builds_and_cascade(dry_run=False)
                self.refresh()
            else:
                self._redraw_chart()
        except Exception:
            pass
        finally:
            self._schedule_auto_ticker()

    def _manual_slide_overdue(self):
        shifted = slide_unstarted_builds_and_cascade(dry_run=False)
        if shifted:
            msg = f"Auto-aligned {len(shifted)} unstarted build(s) to NOW and cascaded subsequent queue:\n\n"
            for s in shifted:
                msg += f"• {s['build_name']} on [{s['printer']}]: {s['old_start']} ➔ {s['new_start']} (+{s['shift_mins']}m, buffer: {s['gap_mins']}m)\n"
            messagebox.showinfo("Schedule Cascaded to Real-Time", msg)
        else:
            messagebox.showinfo("Schedule Up to Date", "All builds are already aligned with current time or actively printing.")
        self.refresh()

    # =========================================================================
    # 1. HEADER CONTROLS BAR
    # =========================================================================
    def _build_header_controls(self):
        header_frame = ctk.CTkFrame(self, fg_color="#111827", corner_radius=0, height=54)
        header_frame.pack(fill="x", padx=0, pady=0)
        header_frame.pack_propagate(False)

        # Left title & branding
        title_box = ctk.CTkFrame(header_frame, fg_color="transparent")
        title_box.pack(side="left", padx=16, pady=6)

        lbl_title = ctk.CTkLabel(
            title_box,
            text="📊 Production Gantt Chart",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color="#38bdf8"
        )
        lbl_title.pack(anchor="w")

        lbl_sub = ctk.CTkLabel(
            title_box,
            text="Workstation Swimlanes • Spool Allocation & Continuity • Delivery Milestones",
            font=ctk.CTkFont(size=10),
            text_color="#94a3b8"
        )
        lbl_sub.pack(anchor="w")

        # Right control widgets
        controls_box = ctk.CTkFrame(header_frame, fg_color="transparent")
        controls_box.pack(side="right", padx=14, pady=8)

        # Data Source Dropdown
        ctk.CTkLabel(controls_box, text="Source:", font=ctk.CTkFont(size=11), text_color="#cbd5e1").pack(side="left", padx=(6, 2))
        self.source_dropdown = ctk.CTkOptionMenu(
            controls_box,
            variable=self.source_var,
            values=["📊 Current Optimization Plan", "🌐 Live ERPNext Builds"],
            width=200,
            height=26,
            font=ctk.CTkFont(size=11),
            command=lambda _: self.refresh()
        )
        self.source_dropdown.pack(side="left", padx=4)

        # Printer Filter
        ctk.CTkLabel(controls_box, text="Printer:", font=ctk.CTkFont(size=11), text_color="#cbd5e1").pack(side="left", padx=(8, 2))
        self.printer_dropdown = ctk.CTkOptionMenu(
            controls_box,
            variable=self.printer_filter_var,
            values=["All Printers"],
            width=140,
            height=26,
            font=ctk.CTkFont(size=11),
            command=lambda _: self._redraw_chart()
        )
        self.printer_dropdown.pack(side="left", padx=4)

        # Material Filter
        ctk.CTkLabel(controls_box, text="Material:", font=ctk.CTkFont(size=11), text_color="#cbd5e1").pack(side="left", padx=(8, 2))
        self.material_dropdown = ctk.CTkOptionMenu(
            controls_box,
            variable=self.material_filter_var,
            values=["All Materials", "PLA", "PETG", "ABS", "TPU"],
            width=120,
            height=26,
            font=ctk.CTkFont(size=11),
            command=lambda _: self._redraw_chart()
        )
        self.material_dropdown.pack(side="left", padx=4)

        # Zoom Option Menu
        ctk.CTkLabel(controls_box, text="Zoom:", font=ctk.CTkFont(size=11), text_color="#cbd5e1").pack(side="left", padx=(8, 2))
        self.zoom_dropdown = ctk.CTkOptionMenu(
            controls_box,
            variable=self.zoom_var,
            values=list(self.zoom_scales.keys()),
            width=145,
            height=26,
            font=ctk.CTkFont(size=11),
            command=lambda _: self._redraw_chart()
        )
        self.zoom_dropdown.pack(side="left", padx=4)

        # Show Deadline Checkbox
        self.cb_deadline = ctk.CTkCheckBox(
            controls_box,
            text="Deadline",
            variable=self.show_deadline_var,
            font=ctk.CTkFont(size=11),
            height=22,
            checkbox_height=16,
            checkbox_width=16,
            command=self._redraw_chart
        )
        self.cb_deadline.pack(side="left", padx=8)

        # Refresh Button
        self.btn_refresh = ctk.CTkButton(
            controls_box,
            text="🔄 Refresh",
            width=75,
            height=26,
            font=ctk.CTkFont(size=11, weight="bold"),
            fg_color="#334155",
            hover_color="#475569",
            command=self.refresh
        )
        self.btn_refresh.pack(side="left", padx=4)

        # Slide Overdue to NOW Button
        self.btn_slide_overdue = ctk.CTkButton(
            controls_box,
            text="⚡ Slide Overdue to NOW",
            width=165,
            height=26,
            font=ctk.CTkFont(size=11, weight="bold"),
            fg_color="#0284c7",
            hover_color="#0369a1",
            command=self._manual_slide_overdue
        )
        self.btn_slide_overdue.pack(side="left", padx=4)

    # =========================================================================
    # 2. KPI METRICS RIBBON
    # =========================================================================
    def _build_kpi_summary_bar(self):
        self.kpi_bar = ctk.CTkFrame(self, fg_color="#1e293b", height=38, corner_radius=0)
        self.kpi_bar.pack(fill="x", padx=0, pady=(1, 0))
        self.kpi_bar.pack_propagate(False)

        self.kpi_printers = ctk.CTkLabel(self.kpi_bar, text="🖨️ Workstations: 0", font=ctk.CTkFont(size=11, weight="bold"), text_color="#f8fafc")
        self.kpi_printers.pack(side="left", padx=16)

        self.kpi_builds = ctk.CTkLabel(self.kpi_bar, text="📦 Planned Builds: 0", font=ctk.CTkFont(size=11, weight="bold"), text_color="#f8fafc")
        self.kpi_builds.pack(side="left", padx=16)

        self.kpi_makespan = ctk.CTkLabel(self.kpi_bar, text="⏱️ Production Makespan: 0.0 hrs", font=ctk.CTkFont(size=11, weight="bold"), text_color="#38bdf8")
        self.kpi_makespan.pack(side="left", padx=16)

        self.kpi_spools = ctk.CTkLabel(self.kpi_bar, text="🧵 Spools Engaged: 0", font=ctk.CTkFont(size=11, weight="bold"), text_color="#a78bfa")
        self.kpi_spools.pack(side="left", padx=16)

        self.kpi_deadline_status = ctk.CTkLabel(self.kpi_bar, text="🎯 Deadline: None", font=ctk.CTkFont(size=11, weight="bold"), text_color="#94a3b8")
        self.kpi_deadline_status.pack(side="right", padx=16)

    # =========================================================================
    # 3. INTERACTIVE GANTT CANVAS WITH STICKY HEADERS
    # =========================================================================
    def _build_gantt_canvas_area(self):
        # Outer container for Gantt chart
        self.chart_container = ctk.CTkFrame(self, fg_color="#090d16", corner_radius=0)
        self.chart_container.pack(fill="both", expand=True, padx=0, pady=0)

        self.chart_container.grid_rowconfigure(1, weight=1)
        self.chart_container.grid_columnconfigure(1, weight=1)

        # Top-Left Corner Box (Fixed)
        self.corner_box = tk.Canvas(
            self.chart_container,
            width=self.header_width,
            height=self.ruler_height,
            bg="#111827",
            highlightthickness=0
        )
        self.corner_box.grid(row=0, column=0, sticky="nsew")
        self.corner_box.create_rectangle(0, 0, self.header_width, self.ruler_height, fill="#111827", outline="#334155")
        self.corner_box.create_text(
            16, self.ruler_height // 2,
            text="WORKSTATION",
            anchor="w",
            fill="#38bdf8",
            font=("Segoe UI", 10, "bold")
        )

        # Top Time Ruler Canvas (Horizontally scrollable, pinned to top)
        self.ruler_canvas = tk.Canvas(
            self.chart_container,
            height=self.ruler_height,
            bg="#0f172a",
            highlightthickness=0
        )
        self.ruler_canvas.grid(row=0, column=1, sticky="nsew")

        # Left Workstations Header Canvas (Vertically scrollable, pinned to left)
        self.left_canvas = tk.Canvas(
            self.chart_container,
            width=self.header_width,
            bg="#111827",
            highlightthickness=0
        )
        self.left_canvas.grid(row=1, column=0, sticky="nsew")

        # Main Gantt Chart Canvas (Both horizontally & vertically scrollable)
        self.main_canvas = tk.Canvas(
            self.chart_container,
            bg="#090d16",
            highlightthickness=0
        )
        self.main_canvas.grid(row=1, column=1, sticky="nsew")

        # Scrollbars
        self.v_scrollbar = ttk.Scrollbar(self.chart_container, orient="vertical", command=self._on_vertical_scroll)
        self.v_scrollbar.grid(row=1, column=2, sticky="ns")

        self.h_scrollbar = ttk.Scrollbar(self.chart_container, orient="horizontal", command=self._on_horizontal_scroll)
        self.h_scrollbar.grid(row=2, column=1, sticky="ew")

        # Connect scroll commands
        self.main_canvas.configure(
            xscrollcommand=self._on_canvas_xscroll,
            yscrollcommand=self._on_canvas_yscroll
        )

        # Bind mouse wheel for seamless scrolling
        self.main_canvas.bind("<MouseWheel>", self._on_mousewheel_vertical)
        self.left_canvas.bind("<MouseWheel>", self._on_mousewheel_vertical)
        self.main_canvas.bind("<Shift-MouseWheel>", self._on_mousewheel_horizontal)
        self.ruler_canvas.bind("<MouseWheel>", self._on_mousewheel_horizontal)

        # Empty state message placeholder
        self.main_canvas.create_text(
            400, 150,
            text="🔄 Loading Build Data & Generating Gantt Chart...",
            fill="#94a3b8",
            font=("Segoe UI", 12)
        )

    def _on_vertical_scroll(self, *args):
        self.left_canvas.yview(*args)
        self.main_canvas.yview(*args)

    def _on_horizontal_scroll(self, *args):
        self.ruler_canvas.xview(*args)
        self.main_canvas.xview(*args)

    def _on_canvas_xscroll(self, first, last):
        self.h_scrollbar.set(first, last)
        self.ruler_canvas.xview_moveto(first)

    def _on_canvas_yscroll(self, first, last):
        self.v_scrollbar.set(first, last)
        self.left_canvas.yview_moveto(first)

    def _on_mousewheel_vertical(self, event):
        delta = -1 * int(event.delta / 120)
        self.left_canvas.yview_scroll(delta, "units")
        self.main_canvas.yview_scroll(delta, "units")

    def _on_mousewheel_horizontal(self, event):
        delta = -1 * int(event.delta / 120)
        self.ruler_canvas.xview_scroll(delta, "units")
        self.main_canvas.xview_scroll(delta, "units")

    # =========================================================================
    # 4. DETAIL INSPECTOR PANEL (BOTTOM DOCK)
    # =========================================================================
    def _build_detail_inspector_panel(self):
        self.inspector_frame = ctk.CTkFrame(self, fg_color="#111827", corner_radius=0, height=185)
        self.inspector_frame.pack(fill="x", side="bottom", padx=0, pady=0)
        self.inspector_frame.pack_propagate(False)

        # Inspector Title & Close
        top_bar = ctk.CTkFrame(self.inspector_frame, fg_color="#1e293b", height=28, corner_radius=0)
        top_bar.pack(fill="x")
        
        lbl_insp_title = ctk.CTkLabel(
            top_bar,
            text="🔍 Selected Build Inspector & Spool Tracking",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color="#38bdf8"
        )
        lbl_insp_title.pack(side="left", padx=12)

        self.btn_copy_schedule = ctk.CTkButton(
            top_bar,
            text="📋 Copy Full Schedule",
            width=135,
            height=20,
            font=ctk.CTkFont(size=10),
            fg_color="#334155",
            hover_color="#475569",
            command=self._copy_schedule_to_clipboard
        )
        self.btn_copy_schedule.pack(side="right", padx=8)

        # 3 Column Layout for Build Cards
        cards_container = ctk.CTkFrame(self.inspector_frame, fg_color="transparent")
        cards_container.pack(fill="both", expand=True, padx=12, pady=6)
        cards_container.grid_columnconfigure((0, 1), weight=1)
        cards_container.grid_columnconfigure(2, weight=2)
        cards_container.grid_rowconfigure(0, weight=1)

        # Card 1: Build & Timing Info
        self.card_build_info = ctk.CTkFrame(cards_container, fg_color="#0f172a", corner_radius=6)
        self.card_build_info.grid(row=0, column=0, sticky="nsew", padx=4, pady=2)

        self.lbl_card1_title = ctk.CTkLabel(self.card_build_info, text="Build Specification", font=ctk.CTkFont(size=11, weight="bold"), text_color="#38bdf8")
        self.lbl_card1_title.pack(anchor="w", padx=10, pady=(6, 2))

        self.lbl_card1_body = ctk.CTkLabel(
            self.card_build_info,
            text="Click any build bar on the Gantt timeline\nabove to inspect its exact timing and part list.",
            font=ctk.CTkFont(size=11),
            text_color="#94a3b8",
            justify="left"
        )
        self.lbl_card1_body.pack(anchor="w", padx=10, pady=2)

        # Card 2: Spool Involved & Continuity
        self.card_spool_info = ctk.CTkFrame(cards_container, fg_color="#0f172a", corner_radius=6)
        self.card_spool_info.grid(row=0, column=1, sticky="nsew", padx=4, pady=2)

        self.lbl_card2_title = ctk.CTkLabel(self.card_spool_info, text="Spool Continuity & Stock", font=ctk.CTkFont(size=11, weight="bold"), text_color="#a78bfa")
        self.lbl_card2_title.pack(anchor="w", padx=10, pady=(6, 2))

        self.lbl_card2_body = ctk.CTkLabel(
            self.card_spool_info,
            text="Spool ID, consumption grams, and remaining\nbalance will display here.",
            font=ctk.CTkFont(size=11),
            text_color="#94a3b8",
            justify="left"
        )
        self.lbl_card2_body.pack(anchor="w", padx=10, pady=2)

        # Card 3: Parts & Sales Orders Nested on Plate
        self.card_parts_info = ctk.CTkFrame(cards_container, fg_color="#0f172a", corner_radius=6)
        self.card_parts_info.grid(row=0, column=2, sticky="nsew", padx=4, pady=2)

        self.lbl_card3_title = ctk.CTkLabel(self.card_parts_info, text="Parts Nested on this Plate", font=ctk.CTkFont(size=11, weight="bold"), text_color="#34d399")
        self.lbl_card3_title.pack(anchor="w", padx=10, pady=(6, 2))

        # Scrollable textbox for part rows
        self.parts_textbox = ctk.CTkTextbox(
            self.card_parts_info,
            fg_color="#090d16",
            text_color="#e2e8f0",
            font=ctk.CTkFont(family="Consolas", size=10),
            wrap="none",
            height=90
        )
        self.parts_textbox.pack(fill="both", expand=True, padx=8, pady=(2, 6))

    # =========================================================================
    # 5. DATA INGESTION & NORMALIZATION
    # =========================================================================
    def refresh(self):
        """Fetches data based on selected source and re-renders."""
        source = self.source_var.get()
        builds = []

        if "Current Optimization Plan" in source:
            planned_plates = getattr(self.parent_app, "last_planned_plates", None)
            if planned_plates:
                builds = self._normalize_planned_plates(planned_plates)
            else:
                # If no active plan has been run yet in this session, fallback to ERPNext
                slide_unstarted_builds_and_cascade(dry_run=False)
                erp_builds = get_all_active_print_builds()
                if erp_builds:
                    self.source_var.set("🌐 Live ERPNext Builds")
                    builds = self._normalize_erpnext_builds(erp_builds)
                else:
                    builds = []
        else:
            slide_unstarted_builds_and_cascade(dry_run=False)
            erp_builds = get_all_active_print_builds()
            builds = self._normalize_erpnext_builds(erp_builds)

        self.current_builds = builds
        self._update_printer_filter_options()
        self._update_kpis()
        self._redraw_chart()

        # Select first build by default if available
        if builds and not self.selected_build:
            self._select_build(builds[0])

    def _normalize_planned_plates(self, plates):
        normalized = []
        now = datetime.now()

        for idx, p in enumerate(plates):
            st_dt = p.ideal_start_time or now
            duration = max(float(getattr(p, "est_hours", 2.0)), 0.1)
            finish_dt = getattr(p, "ideal_finish_time", None) or (st_dt + timedelta(hours=duration))

            # Spool info
            spool_id = getattr(p, "assigned_spool", None) or getattr(p, "preferred_spool", None) or "Unassigned"
            spool_wt = float(getattr(p, "spool_weight", 1000.0))
            final_wt = float(getattr(p, "final_weight", 0.0))
            if final_wt <= 0.0 and p.placed_items:
                final_wt = sum(float(it.get("weight") or 30.0) + float(it.get("support_weight") or 0.0) for it in p.placed_items)
            rem_wt = getattr(p, "rem_after_slice", max(spool_wt - final_wt, 0.0))

            b_id = getattr(p, "build_doc_name", None) or f"Build #{p.plate_id}"

            # Parts list
            items = []
            for it in p.placed_items:
                items.append({
                    "file_name": it.get("file_name", "model.stl"),
                    "sales_order": it.get("sales_order", "—"),
                    "customer_name": it.get("customer_name", "—"),
                    "quantity": it.get("quantity", 1),
                    "dimensions": it.get("dimensions", "—"),
                    "weight": float(it.get("weight") or 0.0),
                    "support_weight": float(it.get("support_weight") or 0.0)
                })

            normalized.append({
                "build_id": b_id,
                "plate_num": p.plate_id,
                "printer": p.printer or "ElegooCentauriCarbon_1",
                "material": str(p.material or "PLA").upper(),
                "spool_id": spool_id,
                "spool_start_weight": spool_wt,
                "consumed_weight": final_wt,
                "rem_weight": rem_wt,
                "start_dt": st_dt,
                "finish_dt": finish_dt,
                "duration_hours": duration,
                "items": items,
                "source": "Optimization Plan"
            })
        return normalized

    def _normalize_erpnext_builds(self, erp_docs):
        normalized = []
        now = datetime.now()

        for idx, doc in enumerate(erp_docs):
            # Parse start and duration
            st_str = doc.get("ideal_start_time") or doc.get("start_time") or doc.get("creation")
            st_dt = now
            if st_str:
                try:
                    clean_str = str(st_str).split(".")[0]
                    st_dt = datetime.strptime(clean_str, "%Y-%m-%d %H:%M:%S")
                except Exception:
                    st_dt = now

            duration = float(doc.get("print_duration") or 2.0)
            finish_dt = st_dt + timedelta(hours=duration)

            spool_id = doc.get("spool") or "Unassigned Spool"
            spool_wt = float(doc.get("spool_available_weight") or 1000.0)
            
            # Parse child items
            items = []
            final_wt = 0.0
            for bf in doc.get("build_files", []):
                w = float(bf.get("weight") or 0.0)
                sw = float(bf.get("support_weight") or 0.0)
                final_wt += (w + sw)
                items.append({
                    "file_name": bf.get("file_name", "model.stl"),
                    "sales_order": bf.get("sales_order", "—"),
                    "customer_name": "—",
                    "quantity": 1,
                    "dimensions": "—",
                    "weight": w,
                    "support_weight": sw
                })

            rem_wt = max(spool_wt - final_wt, 0.0)
            mat_raw = str(doc.get("material") or "PLA").upper()
            clean_mat = "PLA"
            for m in ["PETG", "ABS", "TPU", "PLA"]:
                if m in mat_raw:
                    clean_mat = m
                    break

            normalized.append({
                "build_id": doc.get("name", f"PB-{idx+1}"),
                "plate_num": idx + 1,
                "printer": doc.get("printer") or "ElegooCentauriCarbon_1",
                "material": clean_mat,
                "spool_id": spool_id,
                "spool_start_weight": spool_wt,
                "consumed_weight": final_wt,
                "rem_weight": rem_wt,
                "start_dt": st_dt,
                "finish_dt": finish_dt,
                "duration_hours": duration,
                "items": items,
                "source": "ERPNext"
            })
        return normalized

    def _update_printer_filter_options(self):
        printers = sorted(list({b["printer"] for b in self.current_builds}))
        if not printers:
            try:
                printers = get_available_printers()
            except Exception:
                printers = list(AVAILABLE_PRINTERS)
        
        options = ["All Printers"] + printers
        self.printer_dropdown.configure(values=options)
        if self.printer_filter_var.get() not in options:
            self.printer_filter_var.set("All Printers")

    def _update_kpis(self):
        builds = self.current_builds
        if not builds:
            self.kpi_printers.configure(text="🖨️ Workstations: 0")
            self.kpi_builds.configure(text="📦 Planned Builds: 0")
            self.kpi_makespan.configure(text="⏱️ Production Makespan: 0.0 hrs")
            self.kpi_spools.configure(text="🧵 Spools: 0")
            self.kpi_deadline_status.configure(text="🎯 Deadline: None", text_color="#94a3b8")
            return

        unique_printers = {b["printer"] for b in builds}
        unique_spools = {b["spool_id"] for b in builds if b["spool_id"] != "Unassigned"}

        # Makespan: earliest start to latest finish
        min_start = min(b["start_dt"] for b in builds)
        max_finish = max(b["finish_dt"] for b in builds)
        makespan_h = (max_finish - min_start).total_seconds() / 3600.0

        self.kpi_printers.configure(text=f"🖨️ Workstations: {len(unique_printers)}")
        self.kpi_builds.configure(text=f"📦 Planned Builds: {len(builds)}")
        self.kpi_makespan.configure(text=f"⏱️ Makespan: {makespan_h:.1f} hrs (Finish: {max_finish.strftime('%a %d %b, %I:%M %p')})")
        self.kpi_spools.configure(text=f"🧵 Spools Engaged: {len(unique_spools)}")

        # Deadline evaluation
        target_str = None
        if self.parent_app and hasattr(self.parent_app, "completion_date_entry"):
            target_str = self.parent_app.completion_date_entry.get().strip()

        if target_str:
            try:
                t_date = datetime.strptime(target_str, "%Y-%m-%d").date()
                target_dt = datetime.combine(t_date, datetime.max.time().replace(microsecond=0))
                if max_finish > target_dt:
                    delay_h = (max_finish - target_dt).total_seconds() / 3600.0
                    self.kpi_deadline_status.configure(
                        text=f"🔴 DELAYED (+{delay_h:.1f}h past {target_str})",
                        text_color="#ef4444"
                    )
                else:
                    ahead_h = (target_dt - max_finish).total_seconds() / 3600.0
                    self.kpi_deadline_status.configure(
                        text=f"🟢 ON TRACK (Buffer: +{ahead_h:.1f}h)",
                        text_color="#10b981"
                    )
            except Exception:
                self.kpi_deadline_status.configure(text=f"🎯 Target: {target_str}", text_color="#cbd5e1")
        else:
            self.kpi_deadline_status.configure(text="⚪ No Target Deadline set", text_color="#94a3b8")

    # =========================================================================
    # 6. GANTT CHART RENDERING (CANVAS)
    # =========================================================================
    def _redraw_chart(self):
        # Clear canvases
        self.left_canvas.delete("all")
        self.ruler_canvas.delete("all")
        self.main_canvas.delete("all")

        # Filter builds
        selected_prt = self.printer_filter_var.get()
        selected_mat = self.material_filter_var.get()

        filtered_builds = [
            b for b in self.current_builds
            if (selected_prt == "All Printers" or b["printer"] == selected_prt)
            and (selected_mat == "All Materials" or selected_mat in b["material"])
        ]

        if not filtered_builds:
            msg = "ℹ️ No planned builds found matching current filters.\nRun 'Simulation' or 'Generate Builds' in the main window."
            self.main_canvas.create_text(
                450, 160,
                text=msg,
                fill="#94a3b8",
                font=("Segoe UI", 12),
                justify="center"
            )
            return

        # Determine workstations list for swimlanes
        all_printers_involved = []
        for b in filtered_builds:
            if b["printer"] not in all_printers_involved:
                all_printers_involved.append(b["printer"])

        # Determine time horizon
        now = datetime.now()
        min_dt = min([b["start_dt"] for b in filtered_builds] + [now])
        max_dt = max([b["finish_dt"] for b in filtered_builds] + [now])

        # Add target deadline into time horizon if set
        target_dt = None
        target_str = getattr(self.parent_app, "completion_date_entry", None)
        if target_str and target_str.get().strip():
            try:
                t_date = datetime.strptime(target_str.get().strip(), "%Y-%m-%d").date()
                target_dt = datetime.combine(t_date, datetime.max.time().replace(microsecond=0))
                if target_dt > max_dt:
                    max_dt = target_dt
            except Exception:
                pass

        # Round min_dt down to the start of that day or hour
        timeline_start = min_dt.replace(minute=0, second=0, microsecond=0)
        # Pad timeline end with 4 hours
        timeline_end = max_dt + timedelta(hours=4)
        total_hours = max((timeline_end - timeline_start).total_seconds() / 3600.0, 12.0)

        px_per_hour = self.zoom_scales.get(self.zoom_var.get(), 50)
        total_chart_width = int(total_hours * px_per_hour) + 60
        total_chart_height = len(all_printers_involved) * self.lane_height

        # Configure scroll regions
        self.ruler_canvas.configure(scrollregion=(0, 0, total_chart_width, self.ruler_height))
        self.left_canvas.configure(scrollregion=(0, 0, self.header_width, total_chart_height))
        self.main_canvas.configure(scrollregion=(0, 0, total_chart_width, total_chart_height))

        # --- A. Render Top Time Ruler ---
        self._render_time_ruler(timeline_start, total_hours, px_per_hour, total_chart_width)

        # --- B. Render Left Workstation Swimlanes Header ---
        self._render_left_headers(all_printers_involved, filtered_builds)

        # --- C. Render Grid Lines on Main Canvas ---
        self._render_grid(all_printers_involved, timeline_start, total_hours, px_per_hour, total_chart_width, total_chart_height)

        # --- D. Render NOW & Deadline Vertical Lines ---
        self._render_milestones(timeline_start, px_per_hour, total_chart_height, now, target_dt)

        # --- E. Render Build Bars & Spool Continuity Connectors ---
        self._render_build_bars(all_printers_involved, filtered_builds, timeline_start, px_per_hour, target_dt)

    def _render_time_ruler(self, timeline_start, total_hours, px_per_hour, chart_width):
        self.ruler_canvas.create_rectangle(0, 0, chart_width, self.ruler_height, fill="#0f172a", outline="")

        curr_h = 0
        while curr_h <= total_hours:
            dt = timeline_start + timedelta(hours=curr_h)
            x = curr_h * px_per_hour

            # Date separator at midnight or start
            if dt.hour == 0 or curr_h == 0:
                date_label = dt.strftime("%a, %d %b %Y")
                self.ruler_canvas.create_rectangle(x, 0, x + 200, 20, fill="#1e293b", outline="")
                self.ruler_canvas.create_text(
                    x + 8, 10,
                    text=f"📅 {date_label}",
                    anchor="w",
                    fill="#38bdf8",
                    font=("Segoe UI", 9, "bold")
                )
                self.ruler_canvas.create_line(x, 0, x, self.ruler_height, fill="#475569", width=2)

            # Hour label (every 2, 4, or 6 hours depending on zoom)
            step_hours = 2 if px_per_hour >= 80 else (4 if px_per_hour >= 45 else 6)
            if curr_h % step_hours == 0:
                h_str = dt.strftime("%H:%M")
                self.ruler_canvas.create_line(x, 22, x, self.ruler_height, fill="#334155")
                self.ruler_canvas.create_text(
                    x + 4, 34,
                    text=h_str,
                    anchor="w",
                    fill="#94a3b8",
                    font=("Segoe UI", 8)
                )

            curr_h += 1

    def _render_left_headers(self, printers, builds):
        for idx, printer_name in enumerate(printers):
            y_top = idx * self.lane_height
            y_bottom = y_top + self.lane_height

            # Alternating subtle row background
            row_bg = "#111827" if idx % 2 == 0 else "#161f30"
            self.left_canvas.create_rectangle(0, y_top, self.header_width, y_bottom, fill=row_bg, outline="#1e293b")

            # Printer builds and total hours on this machine
            p_builds = [b for b in builds if b["printer"] == printer_name]
            total_p_hrs = sum(b["duration_hours"] for b in p_builds)
            active_spools = list({b["spool_id"] for b in p_builds if b["spool_id"] != "Unassigned"})
            spool_summary = active_spools[0] if active_spools else "No Spool"

            # Clean display name
            display_name = printer_name.replace("ElegooCentauriCarbon_", "Centauri Carbon #")

            # Workstation Name Label
            self.left_canvas.create_text(
                14, y_top + 22,
                text=f"🖨️ {display_name}",
                anchor="w",
                fill="#f8fafc",
                font=("Segoe UI", 10, "bold")
            )

            # Status / Subtitle line
            self.left_canvas.create_text(
                14, y_top + 44,
                text=f"Builds: {len(p_builds)}  •  Load: {total_p_hrs:.1f}h",
                anchor="w",
                fill="#38bdf8",
                font=("Segoe UI", 8)
            )

            # Mounted spool tag
            self.left_canvas.create_text(
                14, y_top + 62,
                text=f"🧵 Mounted: {spool_summary}",
                anchor="w",
                fill="#a78bfa",
                font=("Segoe UI", 8)
            )

    def _render_grid(self, printers, timeline_start, total_hours, px_per_hour, chart_width, chart_height):
        # Draw swimlane alternating backgrounds
        for idx in range(len(printers)):
            y_top = idx * self.lane_height
            y_bottom = y_top + self.lane_height
            lane_bg = "#090d16" if idx % 2 == 0 else "#0d1322"
            self.main_canvas.create_rectangle(0, y_top, chart_width, y_bottom, fill=lane_bg, outline="#1e293b")

        # Vertical time grid lines
        curr_h = 0
        while curr_h <= total_hours:
            dt = timeline_start + timedelta(hours=curr_h)
            x = curr_h * px_per_hour

            if dt.hour == 0:
                # Midnight line
                self.main_canvas.create_line(x, 0, x, chart_height, fill="#475569", width=2)
            elif curr_h % 6 == 0:
                self.main_canvas.create_line(x, 0, x, chart_height, fill="#1e293b", width=1)
            elif curr_h % 2 == 0:
                self.main_canvas.create_line(x, 0, x, chart_height, fill="#161f30", dash=(2, 4))

            curr_h += 1

    def _render_milestones(self, timeline_start, px_per_hour, chart_height, now, target_dt):
        # 1. NOW indicator line
        x_now = (now - timeline_start).total_seconds() / 3600.0 * px_per_hour
        if x_now >= 0:
            self.main_canvas.create_line(x_now, 0, x_now, chart_height, fill="#10b981", width=2, dash=(4, 4))
            # Label on ruler
            self.ruler_canvas.create_line(x_now, 0, x_now, self.ruler_height, fill="#10b981", width=2)
            self.ruler_canvas.create_rectangle(x_now - 28, 2, x_now + 28, 18, fill="#10b981", outline="")
            self.ruler_canvas.create_text(x_now, 10, text="⏰ NOW", fill="#090d16", font=("Segoe UI", 8, "bold"))

        # 2. Target Deadline indicator line
        if target_dt and self.show_deadline_var.get():
            x_target = (target_dt - timeline_start).total_seconds() / 3600.0 * px_per_hour
            if x_target >= 0:
                self.main_canvas.create_line(x_target, 0, x_target, chart_height, fill="#ef4444", width=2, dash=(6, 4))
                # Label on ruler
                self.ruler_canvas.create_line(x_target, 0, x_target, self.ruler_height, fill="#ef4444", width=2)
                self.ruler_canvas.create_rectangle(x_target - 55, 2, x_target + 55, 18, fill="#ef4444", outline="")
                self.ruler_canvas.create_text(
                    x_target, 10,
                    text="🎯 TARGET DEADLINE",
                    fill="#ffffff",
                    font=("Segoe UI", 7, "bold")
                )

    def _render_build_bars(self, printers, builds, timeline_start, px_per_hour, target_dt):
        # Color palettes by material
        mat_colors = {
            "PLA": {"fill": "#0284c7", "border": "#38bdf8", "text": "#ffffff", "tag": "#0369a1"},
            "PETG": {"fill": "#7c3aed", "border": "#c084fc", "text": "#ffffff", "tag": "#6d28d9"},
            "ABS": {"fill": "#d97706", "border": "#fbbf24", "text": "#ffffff", "tag": "#b45309"},
            "TPU": {"fill": "#059669", "border": "#34d399", "text": "#ffffff", "tag": "#047857"},
        }
        fallback_palette = {"fill": "#334155", "border": "#94a3b8", "text": "#ffffff", "tag": "#1e293b"}

        for p_idx, printer_name in enumerate(printers):
            # Sort builds on this printer sequentially by start time
            p_builds = sorted([b for b in builds if b["printer"] == printer_name], key=lambda x: x["start_dt"])
            prev_spool = None
            prev_finish_dt = None
            prev_finish_x = None

            for b in p_builds:
                st = b["start_dt"]
                et = b["finish_dt"]
                dur = b["duration_hours"]

                x1 = (st - timeline_start).total_seconds() / 3600.0 * px_per_hour
                x2 = (et - timeline_start).total_seconds() / 3600.0 * px_per_hour
                # Enforce minimum visual bar width of 95px for text readability
                if (x2 - x1) < 95:
                    x2 = x1 + 95

                y1 = p_idx * self.lane_height + 9
                y2 = y1 + 64

                # Visual Turnaround Buffer Tag between consecutive builds on this workstation
                if prev_finish_dt is not None and prev_finish_x is not None:
                    gap_mins = (st - prev_finish_dt).total_seconds() / 60.0
                    if gap_mins >= 7:
                        gap_center_x = (prev_finish_x + x1) / 2.0
                        is_swap = (prev_spool is not None and b["spool_id"] != prev_spool and b["spool_id"] != "Unassigned")
                        gap_label = f"🔄 {gap_mins:.0f}m Swap" if is_swap else f"🧹 {gap_mins:.0f}m Clean"
                        gap_bg = "#3b0764" if is_swap else "#064e3b"
                        gap_border = "#c084fc" if is_swap else "#34d399"

                        # Draw subtle dashed turnaround line
                        self.main_canvas.create_line(prev_finish_x, (y1 + y2) / 2, x1, (y1 + y2) / 2, fill=gap_border, width=1, dash=(2, 3))
                        # Draw pill badge if horizontal space allows
                        if (x1 - prev_finish_x) >= 38:
                            half_w = min(max((x1 - prev_finish_x) / 2.0 - 3, 16), 38)
                            self.main_canvas.create_rectangle(
                                gap_center_x - half_w, (y1 + y2) / 2 - 8,
                                gap_center_x + half_w, (y1 + y2) / 2 + 8,
                                fill=gap_bg, outline=gap_border
                            )
                            tag_text = gap_label if (x1 - prev_finish_x) >= 65 else f"{gap_mins:.0f}m"
                            self.main_canvas.create_text(
                                gap_center_x, (y1 + y2) / 2,
                                text=tag_text, fill="#ffffff", font=("Segoe UI", 7, "bold")
                            )

                # Determine material theme
                mat_key = "PLA"
                for m in ["PLA", "PETG", "ABS", "TPU"]:
                    if m in b["material"]:
                        mat_key = m
                        break
                palette = mat_colors.get(mat_key, fallback_palette)

                # Check spool continuity / stickiness with previous build
                is_continuous = (prev_spool is not None and prev_spool == b["spool_id"] and b["spool_id"] != "Unassigned")

                # Check if build exceeds target deadline
                is_overdue = (target_dt and et > target_dt)

                # Draw bar container rectangle
                tag_id = f"build_bar_{id(b)}"
                border_color = "#f87171" if is_overdue else palette["border"]
                border_w = 2 if is_overdue else 1

                # If selected, show highlighted golden ring
                if self.selected_build and self.selected_build["build_id"] == b["build_id"]:
                    border_color = "#facc15"
                    border_w = 3

                rect_item = self.main_canvas.create_rectangle(
                    x1, y1, x2, y2,
                    fill=palette["fill"],
                    outline=border_color,
                    width=border_w,
                    tags=(tag_id, "build_bar")
                )

                # Spool Continuity Link / Badge on left of bar
                if is_continuous:
                    self.main_canvas.create_rectangle(
                        x1, y1, x1 + 6, y2,
                        fill="#10b981",
                        outline="",
                        tags=(tag_id,)
                    )

                # Text inside bar
                # Line 1: Build Name • Material (Duration)
                b_name_disp = b["build_id"]
                if len(b_name_disp) > 14:
                    b_name_disp = b_name_disp[:12] + ".."
                line1 = f"{b_name_disp} • {b['material']} ({dur:.1f}h)"

                self.main_canvas.create_text(
                    x1 + 10, y1 + 14,
                    text=line1,
                    anchor="w",
                    fill=palette["text"],
                    font=("Segoe UI", 9, "bold"),
                    tags=(tag_id,)
                )

                # Line 2: 🧵 Spool ID & Usage
                spool_badge = f"🧵 {b['spool_id']} [{b['consumed_weight']:.0f}g]"
                if is_continuous:
                    spool_badge += " 🔗"
                self.main_canvas.create_text(
                    x1 + 10, y1 + 32,
                    text=spool_badge,
                    anchor="w",
                    fill="#bae6fd",
                    font=("Segoe UI", 8),
                    tags=(tag_id,)
                )

                # Line 3: Parts count & order summary
                part_count = len(b["items"])
                orders_set = list({it["sales_order"] for it in b["items"] if it.get("sales_order")})
                so_str = orders_set[0] if orders_set else "Queue"
                if len(orders_set) > 1:
                    so_str += f" +{len(orders_set)-1}"
                line3 = f"📦 {part_count} part(s) • {so_str}"
                if is_overdue:
                    line3 += " ⚠️ LATE"

                self.main_canvas.create_text(
                    x1 + 10, y1 + 49,
                    text=line3,
                    anchor="w",
                    fill="#e2e8f0",
                    font=("Segoe UI", 8),
                    tags=(tag_id,)
                )

                # Interactive bindings for hover and click
                self.main_canvas.tag_bind(tag_id, "<Button-1>", lambda event, build=b: self._select_build(build))
                self.main_canvas.tag_bind(tag_id, "<Enter>", lambda event, tag=tag_id: self._on_bar_hover(tag))
                self.main_canvas.tag_bind(tag_id, "<Leave>", lambda event, tag=tag_id: self._on_bar_unhover(tag))

                prev_spool = b["spool_id"]
                prev_finish_dt = et
                prev_finish_x = x2

    def _on_bar_hover(self, tag_id):
        self.main_canvas.config(cursor="hand2")

    def _on_bar_unhover(self, tag_id):
        self.main_canvas.config(cursor="")

    # =========================================================================
    # 7. INTERACTIVE SELECTION & INSPECTOR POPULATION
    # =========================================================================
    def _select_build(self, build):
        self.selected_build = build
        self._populate_inspector(build)
        self._redraw_chart()

    def _populate_inspector(self, build):
        # Card 1: Build & Timing
        st_fmt = build["start_dt"].strftime("%a, %d %b %Y at %I:%M %p")
        et_fmt = build["finish_dt"].strftime("%a, %d %b %Y at %I:%M %p")
        dur_hrs = build["duration_hours"]
        dur_h = int(dur_hrs)
        dur_m = int((dur_hrs - dur_h) * 60)

        card1_text = (
            f"• Build ID: {build['build_id']}\n"
            f"• Workstation: {build['printer']}\n"
            f"• Material: {build['material']} (Filament: {build['consumed_weight']:.1f} g)\n"
            f"• Ideal Start: {st_fmt}\n"
            f"• Ideal Finish: {et_fmt}\n"
            f"• Duration: {dur_h}h {dur_m}m ({dur_hrs:.2f} hrs)"
        )
        self.lbl_card1_title.configure(text=f"Build: {build['build_id']}")
        self.lbl_card1_body.configure(text=card1_text)

        # Card 2: Spool Info & Continuity
        rem_g = build["rem_weight"]
        spool_status = "🧵 Retained on Workstation (Spool Continuity)" if rem_g > 30.0 else "🏁 Spool Completed / Depleted"
        if build["spool_id"] == "Unassigned":
            spool_status = "⚠️ No spool designated"

        card2_text = (
            f"• Spool ID: {build['spool_id']}\n"
            f"• Starting Balance: {build['spool_start_weight']:.1f} g\n"
            f"• Print Consumption: {build['consumed_weight']:.1f} g\n"
            f"• Projected Balance: {rem_g:.1f} g remaining\n"
            f"• Spool Continuity: {spool_status}\n"
            f"• Source Data: {build['source']}"
        )
        self.lbl_card2_title.configure(text=f"Spool: {build['spool_id']}")
        self.lbl_card2_body.configure(text=card2_text)

        # Card 3: Nested Parts Table
        self.parts_textbox.delete("1.0", "end")
        items = build["items"]
        self.lbl_card3_title.configure(text=f"Nested Parts on Plate ({len(items)} items)")

        if not items:
            self.parts_textbox.insert("end", "No individual part records attached to this build.\n")
        else:
            header_line = f"{'FILE NAME':<26} {'SALES ORDER':<16} {'QTY':<4} {'DIMS':<16} {'WEIGHT':<8}\n"
            self.parts_textbox.insert("end", header_line)
            self.parts_textbox.insert("end", "-" * 75 + "\n")

            for it in items:
                f_name = it.get("file_name", "—")
                if len(f_name) > 24:
                    f_name = f_name[:22] + ".."
                so_id = it.get("sales_order", "—")
                qty = str(it.get("quantity", 1))
                dims = it.get("dimensions", "—")
                if len(dims) > 15:
                    dims = dims[:14] + ".."
                wt = f"{it.get('weight', 0.0) + it.get('support_weight', 0.0):.1f}g"

                row_str = f"{f_name:<26} {so_id:<16} {qty:<4} {dims:<16} {wt:<8}\n"
                self.parts_textbox.insert("end", row_str)

    def _copy_schedule_to_clipboard(self):
        """Generates clean text summary and copies to clipboard."""
        if not self.current_builds:
            messagebox.showinfo("No Data", "No scheduled builds currently available to copy.")
            return

        lines = [
            "=" * 70,
            "PROTOVALUE3D PRODUCTION GANTT SCHEDULE BREAKDOWN",
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"Total Builds: {len(self.current_builds)} across {len({b['printer'] for b in self.current_builds})} workstation(s)",
            "=" * 70,
            ""
        ]

        printers = sorted(list({b["printer"] for b in self.current_builds}))
        for p in printers:
            p_builds = sorted([b for b in self.current_builds if b["printer"] == p], key=lambda x: x["start_dt"])
            lines.append(f"🖨️ WORKSTATION: {p} ({len(p_builds)} builds)")
            lines.append("-" * 70)
            for b in p_builds:
                st = b["start_dt"].strftime("%Y-%m-%d %H:%M")
                et = b["finish_dt"].strftime("%Y-%m-%d %H:%M")
                lines.append(f"  • [{b['build_id']}] {b['material']} | Spool: {b['spool_id']} ({b['consumed_weight']:.1f}g)")
                lines.append(f"    Timeline: {st} ➔ {et} (~{b['duration_hours']:.1f}h)")
                for it in b["items"]:
                    lines.append(f"      ↳ {it['file_name']} (SO: {it['sales_order']}, Qty: {it['quantity']})")
            lines.append("")

        full_text = "\n".join(lines)
        self.clipboard_clear()
        self.clipboard_append(full_text)
        messagebox.showinfo("Copied", "Production schedule breakdown copied to clipboard!")


if __name__ == "__main__":
    # Standalone preview runner
    root = ctk.CTk()
    root.title("ProtoValue3D Gantt Launcher")
    root.geometry("400x200")
    
    ctk.CTkLabel(root, text="ProtoValue3D Gantt Chart Module", font=ctk.CTkFont(size=14, weight="bold")).pack(pady=20)
    ctk.CTkButton(root, text="Open Gantt Chart", command=lambda: GanttChartWindow(root)).pack(pady=10)
    
    root.mainloop()
