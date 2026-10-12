import os
import sys

# Ensure UTF-8 output encoding on Windows PowerShell
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import math
import argparse
import trimesh
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path

from config import (
    MODELS_DIR, BUILDS_DIR, QUEUE_DIR, MAX_BUILDS,
    MAX_PART_DIMENSION, PART_SPACING, DEFAULT_PRINTER,
    AVAILABLE_PRINTERS, MATERIAL_PRIORITY, DEPLETION_THRESHOLD_GRAMS,
    BED_MAX_WEIGHT_GRAMS, TURNAROUND_SAME_SPOOL_MINUTES,
    TURNAROUND_SPOOL_CHANGE_MINUTES
)
from geometry import (
    check_oriented_bounding_box,
    auto_orient_for_minimum_support,
    get_part_2d_footprint
)
from nester import BuildNester, BuildPlate
from erpnext_sync import (
    get_pending_sales_orders,
    get_available_spools_and_stock,
    get_available_printers,
    post_print_build_draft,
    update_print_build_drive_link,
    cancel_draft_builds,
    lock_filament_weight_on_spool,
    reset_locked_filament_on_spools,
    get_printer_status_and_schedule,
    cancel_unstarted_draft_builds,
    slide_unstarted_builds_and_cascade
)
from slicer_bridge import (
    generate_build_project_3mf,
    slice_plate_with_orcaslicer,
    save_queued_build,
    estimate_mesh_filament_weight,
    estimate_print_time_hours,
    generate_fallback_gcode
)
from notifier import send_material_shortage_alert
from gdrive_client import find_and_download_so_files, upload_build_file_to_drive, create_build_folder_and_upload


class BuildPlateList(list):
    """Subclass of list that allows attaching completion_summary and plan metadata."""
    pass


def extract_base_material(material_name):
    """
    Extracts standard base material (PLA, PETG, ABS, TPU) from any descriptive string.
    e.g. 'PLA (Standard Prototyping)' -> 'PLA'
         'JMG_PLA_B' -> 'PLA'
         'PETG-CF' -> 'PETG'
    """
    if not material_name:
        return "PLA"
    m_upper = str(material_name).strip().upper()
    for base in ["PLA", "PETG", "ABS", "TPU", "NYLON", "PC"]:
        if base in m_upper:
            return base
    return m_upper.split()[0] if m_upper else "PLA"


def is_material_allowed(material_name, allowed_materials):
    """
    Returns True if material_name matches any allowed material filter.
    Handles variations like 'PLA (Standard Prototyping)' matching 'PLA'.
    """
    if not allowed_materials:
        return True
    if not material_name:
        return True
    
    m_base = extract_base_material(material_name)
    m_upper = str(material_name).strip().upper()
    
    for allowed in allowed_materials:
        al_upper = str(allowed).strip().upper()
        if not al_upper or al_upper == "ALL MATERIALS":
            return True
        al_base = extract_base_material(al_upper)
        if al_base == m_base or al_upper == m_upper or al_upper in m_upper or m_upper in al_upper:
            return True
    return False


def get_material_rank(material_name):
    """
    Returns integer rank according to strict priority:
    PLA (0) > PETG (1) > ABS (2) > TPU (3) > any other (999)
    """
    if not material_name:
        return 999
    mat_upper = str(material_name).strip().upper()
    for idx, known in enumerate(MATERIAL_PRIORITY):
        if known in mat_upper:
            return idx
    return 999


def get_material_sort_key(mat_name, units):
    """
    Sort key for material batch priority:
    1) Priority tier (Urgent <= 3 days = 1, Normal = 2)
    2) Earliest delivery deadline (ascending date string, e.g. '2026-10-09')
    3) Material hierarchy rank (PLA=0 > PETG=1 > ABS=2 > TPU=3 > other=999)
    """
    min_tier = min(u["priority_tier"] for u in units) if units else 2
    valid_dates = [u["delivery_date"] for u in units if u.get("delivery_date")]
    earliest_date = min(valid_dates) if valid_dates else "9999-99-99"
    rank = get_material_rank(mat_name)
    return (min_tier, earliest_date, rank)


def find_spool_depletion_partition(
    cand_units,
    spool_balance,
    depletion_threshold=DEPLETION_THRESHOLD_GRAMS,
    bed_max_weight=BED_MAX_WEIGHT_GRAMS
):
    """
    Lookahead Spool Depletion Multi-Plate Knapsack Optimizer.
    Evaluates remaining filament on the mounted spool and candidate parts.
    Partitions parts across builds to ensure clean spool completion (leaving <= threshold)
    rather than stranding awkward remnants.
    Returns (budget_for_this_plate, prioritized_unit_ids, is_depletion_targeted).
    """
    if not cand_units or spool_balance <= 0:
        return min(bed_max_weight, max(spool_balance, 0.0)), [], False

    # Extract weights and priority
    items = []
    for u in cand_units:
        w = max(float(u.get("weight") or 30.0) + float(u.get("support_weight") or 0.0), 5.0)
        items.append({
            "unit_id": u["unit_id"],
            "weight": w,
            "int_w": max(1, int(round(w))),
            "priority": (u.get("priority_tier", 2), u.get("delivery_date") or "9999-99-99"),
            "unit": u
        })

    # Sort items by priority tier and delivery date
    items.sort(key=lambda it: (it["priority"], -it["weight"]))
    min_depletion_target = max(spool_balance - depletion_threshold, 0.0)

    # 0/1 Knapsack / Subset-Sum dynamic programming
    def subset_sum_dp(pool, target_min, target_max):
        reach = {0: []}
        best_sum = 0
        best_subset = []

        for it in pool:
            iw = it["int_w"]
            new_reach = {}
            for s, subset in reach.items():
                ns = s + iw
                if ns <= int(target_max) and ns not in reach and ns not in new_reach:
                    new_reach[ns] = subset + [it]
                    if ns >= target_min and ns <= target_max:
                        return ns, subset + [it]
                    if ns > best_sum:
                        best_sum = ns
                        best_subset = subset + [it]
            reach.update(new_reach)
        return best_sum, best_subset

    # Case 1: Can complete in 1 build (spool_balance <= bed_max_weight)
    if spool_balance <= bed_max_weight:
        best_sum, subset = subset_sum_dp(items, int(min_depletion_target), int(spool_balance))
        if best_sum >= min_depletion_target and subset:
            p_ids = [it["unit_id"] for it in subset]
            return float(best_sum), p_ids, True
        elif subset:
            p_ids = [it["unit_id"] for it in subset]
            return min(float(best_sum), spool_balance), p_ids, False
        else:
            return min(spool_balance, bed_max_weight), [], False

    # Case 2: Multi-build required (spool_balance > bed_max_weight, e.g. 700g on spool)
    k_builds = math.ceil(spool_balance / bed_max_weight)
    
    # Try to find a subset of items to completely exhaust the spool across k builds
    best_total_sum, full_subset = subset_sum_dp(items, int(min_depletion_target), int(spool_balance))
    
    if best_total_sum >= min_depletion_target and full_subset:
        # Partition full_subset across k builds so each build <= bed_max_weight
        target_current_w = best_total_sum / float(k_builds)
        _, current_subset = subset_sum_dp(full_subset, int(target_current_w * 0.75), int(bed_max_weight))
        
        if current_subset:
            cur_w = sum(it["weight"] for it in current_subset)
            p_ids = [it["unit_id"] for it in current_subset]
            return cur_w, p_ids, True

    # Fallback: target fair balanced share <= bed_max_weight
    target_fair = min(bed_max_weight, spool_balance / float(k_builds))
    _, fallback_subset = subset_sum_dp(items, int(target_fair * 0.7), int(bed_max_weight))
    if fallback_subset:
        cur_w = sum(it["weight"] for it in fallback_subset)
        return cur_w, [it["unit_id"] for it in fallback_subset], False
    
    return min(bed_max_weight, spool_balance), [], False


def get_mutex_protected_spools(
    current_printer,
    all_printers,
    selected_printers,
    printer_schedules,
    printer_mounted_spool,
    printer_build_counts
):
    """
    Determines which spools are MUTEX PROTECTED against current_printer.
    A spool on workstation P_other is protected IF AND ONLY IF:
    1. P_other is actively running a physical print right now (is_printing == True in real life).
    2. OR P_other is involved in the current plan (P_other in selected_printers) AND:
       - P_other already has planned builds assigned to it (printer_build_counts[P_other] > 0).

    If P_other is NOT included in the current plan (not in selected_printers),
    OR P_other has no printing to do in the plan (0 builds planned and not printing),
    then its mounted spool is NOT protected and CAN be borrowed/used by current_printer!
    """
    protected = set()
    for p_other in all_printers:
        if p_other == current_printer:
            continue
        m_spool = printer_mounted_spool.get(p_other)
        if not m_spool:
            continue
        sched = printer_schedules.get(p_other, {})
        is_printing = sched.get("is_printing", False)
        is_in_plan = (p_other in selected_printers)
        has_planned_builds = (printer_build_counts.get(p_other, 0) > 0)

        if is_printing:
            protected.add(m_spool)
        elif is_in_plan and has_planned_builds:
            protected.add(m_spool)
        # Otherwise: not protected! Spool is free to be used by active printers in the plan.
    return protected


def find_best_fit_batch_combinatorial(
    cand_units,
    max_weight_budget=BED_MAX_WEIGHT_GRAMS,
    bed_x=256.0,
    bed_y=256.0,
    priority_subset_ids=None,
    log_func=print
):
    """
    Combinatorial Global Batch Optimizer (evaluating O(2^N) permutations & combinations).
    Plans candidate builds from the global parts pool all at once instead of one-by-one greedily.
    Evaluates subsets of candidate parts against bed area, weight budget, priority deadlines,
    and geometric 2D rotation nesting to select the globally highest-utilization batch.
    """
    import itertools
    import time
    from nester import BuildPlate

    if not cand_units:
        return []

    start_t = time.time()
    usable_bed_area = (bed_x - 14.0) * (bed_y - 14.0)
    max_pack_area = usable_bed_area * 1.15

    # 1. Filter out candidate units that exceed build plate boundaries individually
    valid_units = []
    for u in cand_units:
        l = float(u.get("dim_l") or 0.0)
        w = float(u.get("dim_w") or 0.0)
        # Part must fit within the bed (accounting for margins) in at least one orientation
        if (l > bed_x - 14.0 and w > bed_x - 14.0) or (l > bed_y - 14.0 and w > bed_y - 14.0):
            continue
        tot_w = float(u.get("weight") or 30.0) + float(u.get("support_weight") or 0.0)
        u["_total_w"] = tot_w
        u["_area"] = float(u.get("footprint_area") or 3000.0)
        valid_units.append(u)

    if not valid_units:
        return []

    N = len(valid_units)
    subset_id_set = set(priority_subset_ids or [])
    candidate_subsets = []

    # 2. Combinatorial Space Evaluation O(2^N)
    if N <= 16:
        # Full exact power set evaluation
        for r in range(1, N + 1):
            for subset in itertools.combinations(valid_units, r):
                tot_w = sum(u["_total_w"] for u in subset)
                if tot_w > max_weight_budget:
                    continue
                tot_a = sum(u["_area"] for u in subset)
                if tot_a > max_pack_area:
                    continue

                # Scoring: Priority Tier + Deadlines + Weight Utilization + Bed Footprint + Batch Count
                p_score = sum(1000.0 / float(u.get("priority_tier") or 2) for u in subset)
                if subset_id_set:
                    p_score += sum(200.0 for u in subset if u.get("unit_id") in subset_id_set)
                w_score = (tot_w / max_weight_budget) * 60.0
                a_score = (tot_a / usable_bed_area) * 80.0
                count_bonus = len(subset) * 250.0  # Strongly favor packing more parts onto plate to reduce build count!

                score = p_score + w_score + a_score + count_bonus
                candidate_subsets.append((score, subset, tot_w, tot_a))
    else:
        # Large pool: prioritize by priority tier, delivery deadline, and footprint area
        valid_units.sort(key=lambda u: (
            0 if u.get("unit_id") in subset_id_set else 1,
            u.get("priority_tier", 2),
            u.get("delivery_date") or "9999-99-99",
            -u["_area"]
        ))
        eval_pool = valid_units[:24]
        max_k = min(len(eval_pool), 12)
        for r in range(1, max_k + 1):
            r_count = 0
            for subset in itertools.combinations(eval_pool, r):
                tot_w = sum(u["_total_w"] for u in subset)
                if tot_w > max_weight_budget:
                    continue
                tot_a = sum(u["_area"] for u in subset)
                if tot_a > max_pack_area:
                    continue

                p_score = sum(1000.0 / float(u.get("priority_tier") or 2) for u in subset)
                if subset_id_set:
                    p_score += sum(200.0 for u in subset if u.get("unit_id") in subset_id_set)
                w_score = (tot_w / max_weight_budget) * 60.0
                a_score = (tot_a / usable_bed_area) * 80.0
                count_bonus = len(subset) * 250.0

                score = p_score + w_score + a_score + count_bonus
                candidate_subsets.append((score, subset, tot_w, tot_a))
                r_count += 1
                if r_count > 6000:
                    break

    candidate_subsets.sort(key=lambda x: x[0], reverse=True)
    elapsed = time.time() - start_t
    log_func(f"   📊 Combinatorial search completed: Evaluated {len(candidate_subsets)} valid batch combinations in {elapsed*1000:.1f}ms.")

    # 3. Geometric 2D Rotation Nesting Verification
    # Test top-ranked candidate batches to ensure complete physical fit on the bed
    for idx, (score, subset, tot_w, tot_a) in enumerate(candidate_subsets[:50]):
        test_plate = BuildPlate(0, bed_x=bed_x, bed_y=bed_y)
        all_fit = True
        sorted_items = sorted(subset, key=lambda it: -it["_area"])
        for it in sorted_items:
            if not test_plate.place_item(it, it["polygon_footprint"]):
                all_fit = False
                break
        if all_fit:
            log_func(f"   🏆 Selected Best Combinatorial Batch #{idx+1}: {len(subset)} unit(s) | {tot_w:.1f}g ({tot_w/max_weight_budget*100:.1f}% budget) | {tot_a:.0f}mm² ({tot_a/usable_bed_area*100:.1f}% bed fill)")
            return list(sorted_items)

    # Fallback if no full subset nested: return items of top candidate subset
    if candidate_subsets:
        top_subset = list(candidate_subsets[0][1])
        top_subset.sort(key=lambda it: -it["_area"])
        return top_subset

    return valid_units


def find_or_create_mesh(file_name, target_weight_g=150.0, dl_files=None):
    """
    Locates matching STL/3MF in models/ directory.
    Returns (mesh, file_path) if found on disk / downloaded from Google Drive,
    or (None, None) if the file is not present.
    """
    if file_name:
        clean_name = str(file_name).split("(")[0].strip()
        if clean_name:
            candidates = list(MODELS_DIR.glob(f"*{clean_name}*")) + list(MODELS_DIR.glob(f"{clean_name}*"))
            for c in candidates:
                if c.suffix.lower() in [".stl", ".3mf", ".step", ".stp", ".obj"]:
                    try:
                        mesh = trimesh.load(str(c), force='mesh')
                        if not mesh.is_empty:
                            return mesh, str(c)
                    except Exception:
                        pass
                        
    # Fallback to files downloaded from this specific SO's Drive folder
    if dl_files:
        for f in dl_files:
            p = Path(f)
            if p.exists() and p.suffix.lower() in [".stl", ".3mf", ".step", ".stp", ".obj"]:
                try:
                    mesh = trimesh.load(str(p), force='mesh')
                    if not mesh.is_empty:
                        return mesh, str(p)
                except Exception:
                    pass
                    
    return None, None


def run_planner(
    selected_printers=None,
    selected_material=None,
    selected_materials=None,
    selected_sales_orders=None,
    target_completion_date=None,
    max_builds=MAX_BUILDS,
    dry_run=False,
    replan=False,
    log_func=print
):
    """
    Orchestrates multi-printer and multi-material build planning:
    1. Evaluates all pending files across all active Sales Orders (or selected Sales Orders).
    2. Sorts by delivery deadline first, then material priority (PLA > PETG > ABS > TPU > other).
    3. If target completion date is provided, prioritizes items and balances parallel printer makespan.
    4. Allocates builds across all selected/available printers.
    5. Enforces global exclusion tracking so placed files are never duplicated in other builds.
    6. Validates stock in Stores and posts Draft Print Builds to ERPNext with assigned printer workstation.
    """
    # 0. Resolve target printers
    if not selected_printers:
        selected_printers = get_available_printers()
    elif isinstance(selected_printers, str):
        if "all" in selected_printers.lower():
            selected_printers = get_available_printers()
        else:
            selected_printers = [selected_printers.strip()]
            
    # Remove duplicates preserving order
    selected_printers = list(dict.fromkeys(selected_printers))
    if not selected_printers:
        selected_printers = list(AVAILABLE_PRINTERS)

    # Normalize selected materials (supports list, set, or comma-separated string)
    mats_input = selected_materials or selected_material
    allowed_materials = None
    if mats_input:
        if isinstance(mats_input, str):
            if mats_input.strip() and mats_input.strip().lower() != "all materials":
                allowed_materials = {m.strip().upper() for m in mats_input.split(",") if m.strip()}
        elif isinstance(mats_input, (list, tuple, set)):
            allowed_materials = {str(m).strip().upper() for m in mats_input if str(m).strip()}

    # Parse target completion date if provided
    target_date = None
    available_hours = None
    if target_completion_date:
        try:
            target_date = datetime.strptime(str(target_completion_date).strip(), "%Y-%m-%d").date()
            now_dt = datetime.now()
            target_dt = datetime.combine(target_date, datetime.max.time().replace(hour=23, minute=59, second=59))
            available_hours = max((target_dt - now_dt).total_seconds() / 3600.0, 0.0)
        except Exception as e:
            target_date = None

    mat_label = ", ".join(sorted(allowed_materials)) if allowed_materials else "All Materials"
    log_func("=" * 70)
    log_func(" 🚀 PROTOVALUE3D MULTI-PRINTER BUILD PLANNER & SLICER ORCHESTRATOR")
    log_func(f" Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log_func(f" Allocated Printers ({len(selected_printers)}): {', '.join(selected_printers)}")
    log_func(f" Material Filter: {mat_label} | Max Builds Limit: {max_builds}")
    if target_date:
        log_func(f" Target Completion Date: {target_date} (~{available_hours:.1f}h available window)")
    if selected_sales_orders:
        log_func(f" Selected Sales Orders ({len(selected_sales_orders)}): {', '.join(selected_sales_orders)}")
    log_func("=" * 70)

    # Re-plan Handling
    if replan:
        log_func("\n🔄 Re-Plan Active: Cancelling unstarted draft builds & resetting spool locks...")
        if dry_run:
            log_func("   [DRY RUN] Would cancel unstarted draft builds and reset spool locks.")
        else:
            cancelled = cancel_unstarted_draft_builds(selected_printers)
            if cancelled:
                log_func(f"   • Cancelled {len(cancelled)} unstarted draft build(s): {', '.join(cancelled)}")
            else:
                log_func("   • No unstarted draft builds found to cancel.")
            reset_locked_filament_on_spools()
            log_func("   • Reset locked filament weight to 0.0 across all spools.")

    # Check real-time printer status & schedules across all workshop workstations
    log_func("\n[Printer Check] Querying real-time printer status & schedules from ERPNext...")
    all_workshop_printers = get_available_printers() or list(AVAILABLE_PRINTERS)
    printer_schedules = get_printer_status_and_schedule(all_workshop_printers)
    for p in selected_printers:
        sched = printer_schedules.get(p, {})
        p_status = sched.get("status", "Idle")
        is_printing = sched.get("is_printing", False)
        running_b = sched.get("running_build")
        m_spool = sched.get("mounted_spool")
        m_bal = float(sched.get("spool_balance") or 0.0)
        m_mat = sched.get("mounted_material")
        spool_desc = f" | Spool: {m_spool} ({m_mat}, {m_bal:.1f}g left)" if m_spool else " | Spool: None (Ready for new spool)"
        next_av = sched.get("next_available_time", datetime.now())
        if is_printing and running_b:
            log_func(f"  • Workstation [{p}] (In Plan): 🔴 Status: {p_status} | Running: '{running_b}' | Next Available: {next_av.strftime('%Y-%m-%d %H:%M')}{spool_desc}")
        else:
            log_func(f"  • Workstation [{p}] (In Plan): 🟢 Status: {p_status} | Available Immediately{spool_desc}")

    # Also log any workstations NOT in current plan that have spools available to borrow
    unselected_printers = [p for p in all_workshop_printers if p not in selected_printers]
    for p in unselected_printers:
        sched = printer_schedules.get(p, {})
        m_spool = sched.get("mounted_spool")
        m_bal = float(sched.get("spool_balance") or 0.0)
        m_mat = sched.get("mounted_material")
        if m_spool and not sched.get("is_printing"):
            log_func(f"  ℹ️ Workstation [{p}] (Not in Plan): Spool '{m_spool}' ({m_mat}, {m_bal:.1f}g left) is unassigned & available to borrow.")

    # 1. Fetch open Sales Orders from ERPNext
    log_func("\n[Step 1/5] Fetching open Sales Orders from ERPNext...")
    orders = get_pending_sales_orders(ignore_unstarted_draft_builds=replan)
    
    if not orders:
        log_func("✅ No pending unbuilt Sales Orders found in ERPNext.")
        return []

    # Filter by user-selected Sales Orders if specified
    if selected_sales_orders:
        selected_so_set = set(selected_sales_orders)
        orders = [so for so in orders if so["sales_order"] in selected_so_set]
        if not orders:
            log_func(f"⚠️ None of the selected Sales Orders ({', '.join(selected_sales_orders)}) have pending parts.")
            return []

    log_func(f"Processing {len(orders)} open Sales Order(s):")
    for so in orders:
        log_func(f"  • {so['sales_order']} | Due: {so['delivery_date'] or 'N/A'} | Priority: {so['priority_label']} ({len(so['files'])} parts)")

    # 2. Extract, pre-orient, and assemble global parts pool
    log_func(f"\n[Step 2/5] Pooling parts across {len(orders)} Sales Order(s) & Batch Pre-Orienting...")

    # Step 2a: Gather all CAD models from Drive in batch per Sales Order
    synced_sales_orders = set()
    so_dl_files = {}
    for so in orders:
        so_id = so["sales_order"]
        if so_id not in synced_sales_orders:
            log_func(f"📥 Checking CAD models in Google Drive for {so_id}...")
            dl_files, drive_err = find_and_download_so_files(so)
            if drive_err:
                log_func(f"   ℹ️ {drive_err}")
            elif dl_files:
                log_func(f"   • Downloaded {len(dl_files)} 3D file(s) from Drive folder.")
            synced_sales_orders.add(so_id)
            so_dl_files[so_id] = dl_files or []

    # Step 2b: Pre-orient all unique CAD models for minimum support overhang and calculate dimensions
    unique_model_cache = {}
    for so in orders:
        so_id = so["sales_order"]
        for f in so["files"]:
            fname = f["file_name"]
            mat_raw = f.get("material_general") or "PLA"
            if allowed_materials and not is_material_allowed(mat_raw, allowed_materials):
                continue

            if fname not in unique_model_cache:
                log_func(f"\n🔍 Pre-Orienting Model: '{fname}' (from {so_id})...")
                mesh, _ = find_or_create_mesh(fname, f.get("weight", 150.0), dl_files=so_dl_files.get(so_id))
                if not mesh:
                    log_func(f"   ❌ MODEL REJECTED: File is not uploaded in Drive for '{fname}'. Slicer cannot process missing file.")
                    unique_model_cache[fname] = None
                    continue

                # 1. Check raw oriented bounding box
                obb = check_oriented_bounding_box(mesh)
                l, w, h = obb["length"], obb["width"], obb["height"]
                if not obb["is_valid"]:
                    log_func(f"   ❌ MODEL REJECTED: {obb['reason']}. Skipping.")
                    unique_model_cache[fname] = None
                    continue

                # 2. Auto-orient for minimum support overhang
                log_func("   • Auto-orienting for minimum overhang surface & bed adhesion...")
                oriented_mesh, _ = auto_orient_for_minimum_support(mesh)

                # 3. Post-orientation dimensions and 2D footprint (with 7mm spacing buffer)
                oriented_obb = check_oriented_bounding_box(oriented_mesh)
                ol, ow, oh = oriented_obb["length"], oriented_obb["width"], oriented_obb["height"]
                footprint = get_part_2d_footprint(oriented_mesh, spacing=PART_SPACING)
                area = float(footprint["polygon"].area)
                fw, fh = footprint["width"], footprint["height"]

                geom_weight = estimate_mesh_filament_weight(oriented_mesh, material_general=extract_base_material(mat_raw))
                max_solid_box_g = (ol * ow * oh * 1.25) / 1000.0  # theoretical maximum solid box weight in grams
                log_func(f"   • Oriented Dimensions: {ol:.1f} x {ow:.1f} x {oh:.1f} mm (Footprint: {fw:.1f} x {fh:.1f} mm, Area: {area:.0f} mm², Sliced Est: {geom_weight:.1f}g)")
                unique_model_cache[fname] = {
                    "mesh": oriented_mesh,
                    "length": ol,
                    "width": ow,
                    "height": oh,
                    "dimensions_str": f"{ol:.1f} x {ow:.1f} x {oh:.1f} mm",
                    "footprint": footprint["polygon"],
                    "footprint_bounds": footprint["bounds"],
                    "footprint_area": area,
                    "footprint_width": fw,
                    "footprint_height": fh,
                    "geom_weight": geom_weight,
                    "max_solid_box_g": max_solid_box_g
                }

    # Step 2c: Assemble the global candidate pool of discrete printable units (accounting for full quantity)
    part_units = []
    log_func(f"\n📦 Assembling Global Parts Pool from Sales Orders (accounting for quantities)...")

    for so in orders:
        so_id = so["sales_order"]
        so_deliv_date_str = so.get("delivery_date")
        so_priority_tier = so.get("priority", 2)

        if target_date and so_deliv_date_str:
            try:
                d = datetime.strptime(so_deliv_date_str, "%Y-%m-%d").date()
                if d <= target_date:
                    so_priority_tier = 1
            except Exception:
                pass

        for f in so["files"]:
            fname = f["file_name"]
            mat_raw = f.get("material_general") or "PLA"
            if allowed_materials and not is_material_allowed(mat_raw, allowed_materials):
                continue

            geo = unique_model_cache.get(fname)
            if not geo:
                continue

            base_mat = extract_base_material(mat_raw)
            qty = max(1, int(f.get("quantity") or 1))
            erp_part_w = float(f.get("weight") or 0.0)
            supp_w = float(f.get("support_weight") or 0.0)
            geo_w = geo.get("geom_weight", 30.0)
            max_box_w = geo.get("max_solid_box_g", 1000.0)

            # Plausibility check: if ERPNext has placeholder or inflated weight (e.g. 500g entered for a 40mm part), auto-correct using real 3D geometry
            if erp_part_w <= 0.0 or erp_part_w > (max_box_w * 1.5):
                part_w = geo_w
                if erp_part_w > (max_box_w * 1.5):
                    log_func(f"   ℹ️ Auto-corrected inflated weight for '{fname}': ERPNext had {erp_part_w:.1f}g, physical 3D mesh is {geo_w:.1f}g.")
            else:
                part_w = erp_part_w

            total_w = part_w + supp_w
            erp_dims = f.get("dimensions") or geo["dimensions_str"]

            for q in range(qty):
                unit_id = f"{so_id}_{f.get('row_name') or fname}_{q+1}"
                unit = {
                    "unit_id": unit_id,
                    "sales_order": so_id,
                    "customer_name": so.get("customer_name"),
                    "delivery_date": so_deliv_date_str,
                    "priority_tier": so_priority_tier,
                    "priority_label": so.get("priority_label", ""),
                    "file_name": fname,
                    "row_name": f.get("row_name", ""),
                    "material_general": base_mat,
                    "material_detail": mat_raw,
                    "material_item": f.get("material_item") or f"JMG-{base_mat}-BL",
                    "custom_finished_good": f.get("custom_finished_good"),
                    "order_type": f.get("order_type") or "",
                    "dimensions": erp_dims,
                    "dim_l": geo["length"],
                    "dim_w": geo["width"],
                    "dim_h": geo["height"],
                    "weight": part_w,
                    "support_weight": supp_w,
                    "total_weight": total_w,
                    "mesh": geo["mesh"],
                    "polygon_footprint": geo["footprint"],
                    "footprint_bounds": geo["footprint_bounds"],
                    "footprint_area": geo["footprint_area"],
                    "footprint_width": geo["footprint_width"],
                    "footprint_height": geo["footprint_height"],
                    "qty_index": q + 1,
                    "total_qty": qty,
                    "so_dict": so
                }
                part_units.append(unit)

    log_func(f"✅ Pool successfully assembled: {len(part_units)} total printable units ready across {len(orders)} order(s).")

    if not part_units:
        log_func("⚠️ No eligible 3D parts ready for build planning.")
        return []

    # 3. Multi-Printer & Multi-Material Build Planning Loop
    log_func(f"\n[Step 3/5] Planning builds across {len(selected_printers)} printer(s) with priority permutations...")
    log_func(f"Total parts to plan: {len(part_units)}")

    global_placed_unit_ids = set()
    planned_plates = BuildPlateList()
    printer_build_counts = {p: 0 for p in selected_printers}
    printer_est_hours = {p: 0.0 for p in selected_printers}
    printer_material_map = {}
    now_dt = datetime.now()
    printer_timeline = {p: printer_schedules.get(p, {}).get("next_available_time", now_dt) for p in selected_printers}
    printer_last_spool = {p: printer_schedules.get(p, {}).get("mounted_spool") for p in all_workshop_printers}
    printer_last_plate_end = {p: None for p in selected_printers}

    # Spool Stickiness & Completion Tracking across workshop printers
    printer_mounted_spool = {p: printer_schedules.get(p, {}).get("mounted_spool") for p in all_workshop_printers}
    printer_spool_balance = {p: float(printer_schedules.get(p, {}).get("spool_balance") or 0.0) for p in all_workshop_printers}
    printer_material_map = {p: printer_schedules.get(p, {}).get("mounted_material") for p in all_workshop_printers}
    simulated_spool_locks = {}

    while len(planned_plates) < max_builds:
        # A. Remaining unplaced units
        available_units = [u for u in part_units if u["unit_id"] not in global_placed_unit_ids]
        if not available_units:
            break

        # B. Group remaining units by material
        mat_groups = {}
        for u in available_units:
            mat_groups.setdefault(u["material_general"], []).append(u)

        assigned_printer = None
        current_mat = None
        preferred_spool = None
        spool_budget = 1000.0

        # PRIORITY 1: SPOOL COMPLETION ON MOUNTED PRINTERS
        # Check if any printer has a mounted spool with balance > DEPLETION_THRESHOLD_GRAMS
        # AND there are matching unplaced parts for that mounted material.
        spool_completion_candidates = []
        for p in selected_printers:
            m_spool = printer_mounted_spool.get(p)
            m_bal = printer_spool_balance.get(p, 0.0)
            m_mat = printer_material_map.get(p)
            m_mat_base = extract_base_material(m_mat)
            if m_spool and m_bal > DEPLETION_THRESHOLD_GRAMS and m_mat_base in mat_groups:
                spool_completion_candidates.append({
                    "printer": p,
                    "spool": m_spool,
                    "balance": m_bal,
                    "material": m_mat_base,
                    "timeline": printer_timeline[p],
                    "earliest_due": min((u["delivery_date"] or "9999-99-99") for u in mat_groups[m_mat_base])
                })

        if spool_completion_candidates:
            # Sort candidates by earliest due date, then earliest printer availability
            spool_completion_candidates.sort(key=lambda c: (c["earliest_due"], c["timeline"]))
            chosen = spool_completion_candidates[0]
            assigned_printer = chosen["printer"]
            current_mat = chosen["material"]
            preferred_spool = chosen["spool"]
            spool_budget = chosen["balance"]
            cand_units = mat_groups[current_mat]
            log_func(f"\n🧵 [Spool Completion Priority] Workstation [{assigned_printer}] has mounted spool '{preferred_spool}' ({current_mat}) with {spool_budget:.1f}g remaining.")
            log_func(f"   ➔ Prioritizing completion of this spool before changing filament.")
        else:
            # PRIORITY 2: STANDARD MATERIAL RANKING & PRINTER SELECTION
            # Rank materials by:
            # 1. Delivery Deadline (Earliest date first; urgent <= 3 days tier first)
            # 2. Material Hierarchy (PLA > PETG > ABS > TPU > other) when deadlines tie
            ranked_materials = sorted(mat_groups.keys(), key=lambda m: get_material_sort_key(m, mat_groups[m]))
            current_mat = ranked_materials[0]
            cand_units = mat_groups[current_mat]

            # Assign Printer:
            # Rule 1: Idle printers matching current material (filament already mounted)
            # Rule 2: Idle printers with NO spool currently mounted (avoids swapping out existing spools!)
            # Rule 3: Any idle printer (earliest timeline)
            # Rule 4: Zero-build printer matching material or with no spool
            # Rule 5: Workstation that frees up earliest across all selected printers
            idle_zero = [
                p for p in selected_printers 
                if printer_build_counts[p] == 0 and not printer_schedules.get(p, {}).get("is_printing")
            ]
            idle_matching = [p for p in idle_zero if extract_base_material(printer_material_map.get(p)) == current_mat]
            idle_no_spool = [p for p in idle_zero if not printer_mounted_spool.get(p)]

            if idle_matching:
                assigned_printer = min(idle_matching, key=lambda p: printer_timeline[p])
            elif idle_no_spool:
                assigned_printer = min(idle_no_spool, key=lambda p: printer_timeline[p])
            elif idle_zero:
                assigned_printer = min(idle_zero, key=lambda p: printer_timeline[p])
            else:
                zero_printers = [p for p in selected_printers if printer_build_counts[p] == 0]
                zero_matching = [p for p in zero_printers if extract_base_material(printer_material_map.get(p)) == current_mat]
                zero_no_spool = [p for p in zero_printers if not printer_mounted_spool.get(p)]
                if zero_matching:
                    assigned_printer = min(zero_matching, key=lambda p: printer_timeline[p])
                elif zero_no_spool:
                    assigned_printer = min(zero_no_spool, key=lambda p: printer_timeline[p])
                elif zero_printers:
                    assigned_printer = min(zero_printers, key=lambda p: printer_timeline[p])
                else:
                    mat_matching = [p for p in selected_printers if extract_base_material(printer_material_map.get(p)) == current_mat]
                    if mat_matching:
                        assigned_printer = min(mat_matching, key=lambda p: printer_timeline[p])
                    else:
                        assigned_printer = min(selected_printers, key=lambda p: printer_timeline[p])

            printer_material_map[assigned_printer] = current_mat

            # Find best spool in Stores for this fresh assignment
            # Mutex-protect spools ONLY if other workstations are involved in the plan and printing!
            protected_spools = get_mutex_protected_spools(
                assigned_printer,
                all_workshop_printers,
                selected_printers,
                printer_schedules,
                printer_mounted_spool,
                printer_build_counts
            )
            rep_mat_item = cand_units[0].get("material_item") or f"JMG-{current_mat}-BL"
            _, suggested_spool, spool_usable_bal = get_available_spools_and_stock(
                rep_mat_item,
                required_weight=30.0,
                simulated_locks=simulated_spool_locks,
                exclude_spools=protected_spools
            )
            preferred_spool = suggested_spool
            spool_budget = spool_usable_bal if spool_usable_bal > 0 else 1000.0

            # Did we borrow an unassigned spool from an inactive workstation (not in plan or no builds)?
            borrowed_from = None
            for p_other, s in list(printer_mounted_spool.items()):
                if p_other != assigned_printer and s == suggested_spool:
                    borrowed_from = p_other
                    printer_mounted_spool[p_other] = None
                    printer_spool_balance[p_other] = 0.0
                    break

            printer_mounted_spool[assigned_printer] = preferred_spool
            printer_spool_balance[assigned_printer] = spool_budget
            if preferred_spool:
                if borrowed_from:
                    log_func(f"\n🔄 [Spool Transfer] Borrowed unassigned spool '{preferred_spool}' ({spool_budget:.1f}g) from inactive Workstation [{borrowed_from}] for Workstation [{assigned_printer}] ({current_mat}).")
                else:
                    log_func(f"\n📦 [New Spool Mount] Selected best spool '{preferred_spool}' ({spool_budget:.1f}g) on Workstation [{assigned_printer}] for {current_mat}.")

        # E. Create Build Plate
        plate_num = len(planned_plates) + 1
        plate = BuildPlate(plate_num, printer=assigned_printer, material=current_mat)

        # Calculate turnaround gap (10 min same spool for bed cleaning / part removal, 15 min for spool swap)
        prev_s = printer_last_spool.get(assigned_printer)
        is_spool_change = (prev_s is None or prev_s != preferred_spool)
        gap_mins = TURNAROUND_SPOOL_CHANGE_MINUTES if is_spool_change else TURNAROUND_SAME_SPOOL_MINUTES

        if printer_build_counts[assigned_printer] == 0:
            # First planned build on this printer in this session
            if printer_schedules.get(assigned_printer, {}).get("is_printing"):
                # Printer is actively running another job; turnaround starts after it finishes
                base_time = printer_timeline[assigned_printer]
                plate.ideal_start_time = base_time + timedelta(minutes=gap_mins)
            else:
                # Printer is currently idle
                if is_spool_change:
                    plate.ideal_start_time = max(printer_timeline[assigned_printer], now_dt) + timedelta(minutes=TURNAROUND_SPOOL_CHANGE_MINUTES)
                else:
                    plate.ideal_start_time = max(printer_timeline[assigned_printer], now_dt)
        else:
            # Consecutive build on this printer
            last_end = printer_last_plate_end[assigned_printer] or printer_timeline[assigned_printer]
            plate.ideal_start_time = last_end + timedelta(minutes=gap_mins)

        plate.turnaround_gap_minutes = gap_mins
        plate.is_spool_change = is_spool_change
        plate.preferred_spool = preferred_spool

        # F. Optimize plate target weight & parts partition for clean spool depletion
        plate_budget, priority_subset_ids, is_depletion_targeted = find_spool_depletion_partition(
            cand_units,
            spool_balance=spool_budget,
            depletion_threshold=DEPLETION_THRESHOLD_GRAMS,
            bed_max_weight=BED_MAX_WEIGHT_GRAMS
        )

        log_func(f"\n🧩 Planning Build Plate #{plate_num} on [{assigned_printer}] for Material [{current_mat}] (Remaining Spool: {spool_budget:.1f}g)...")
        if is_depletion_targeted:
            log_func(f"   🎯 [Spool Depletion Knapsack] Partitioning spool ({spool_budget:.1f}g left): Target for this plate is {plate_budget:.1f}g to achieve clean completion.")
        else:
            log_func(f"   ⚖️ [Spool Depletion Knapsack] Balanced plate target: {plate_budget:.1f}g (Remaining spool: {spool_budget:.1f}g).")

        # Sort candidates: prioritize items selected by knapsack partitioner
        if priority_subset_ids:
            subset_set = set(priority_subset_ids)
            cand_units.sort(key=lambda u: (
                0 if u["unit_id"] in subset_set else 1,
                u["priority_tier"],
                u["delivery_date"] or "9999-99-99",
                -u["footprint_area"]
            ))
        else:
            cand_units.sort(key=lambda u: (u["priority_tier"], u["delivery_date"] or "9999-99-99", -u["footprint_area"]))

        earliest_due = min([u["delivery_date"] for u in cand_units if u["delivery_date"]] or ["No Date"])
        log_func(f"   • Priority: Tier {min(u['priority_tier'] for u in cand_units)} | Earliest Deadline: {earliest_due}")

        # G. Combinatorial Global Batch Optimization O(2^N)
        # Instead of iterating through candidate parts one-by-one greedily, evaluate candidate
        # subsets (permutations & combinations O(2^N)) across the pool all at once, picking the
        # globally highest-density batch that fits weight, bed boundaries, and delivery priorities.
        available_cand_units = [u for u in cand_units if u["unit_id"] not in global_placed_unit_ids]

        log_func(f"   🎲 [Combinatorial Batch Planner] Evaluating permutations & combinations O(2^N) across {len(available_cand_units)} pool units...")

        # Determine effective plate weight limit:
        # If is_depletion_targeted is True (spool is finishing within <=25g), honor plate_budget + 25g.
        # Otherwise, the plate can hold as much as can physically fit up to min(spool_budget, BED_MAX_WEIGHT_GRAMS)!
        if is_depletion_targeted:
            effective_plate_weight_limit = min(plate_budget + 25.0, spool_budget, BED_MAX_WEIGHT_GRAMS)
        else:
            effective_plate_weight_limit = min(spool_budget, BED_MAX_WEIGHT_GRAMS)

        best_batch = find_best_fit_batch_combinatorial(
            available_cand_units,
            max_weight_budget=effective_plate_weight_limit,
            bed_x=plate.bed_x,
            bed_y=plate.bed_y,
            priority_subset_ids=priority_subset_ids,
            log_func=log_func
        )

        plate_est_weight = 0.0
        # Place items from the selected best combinatorial batch
        for u in best_batch:
            if u["unit_id"] in global_placed_unit_ids:
                continue
            part_est_w = float(u.get("weight") or 30.0) + float(u.get("support_weight") or 0.0)
            if plate_est_weight + part_est_w > effective_plate_weight_limit:
                continue
            if plate.place_item(u, u["polygon_footprint"]):
                global_placed_unit_ids.add(u["unit_id"])
                plate_est_weight += part_est_w
                log_func(f"   • Placed '{u['file_name']}' ({u['sales_order']}, Unit {u.get('qty_index', 1)}/{u.get('total_qty', 1)}) [{u.get('dimensions', '')}] ➔ {part_est_w:.1f}g (Part: {u.get('weight', 0):.1f}g, Supp: {u.get('support_weight', 0):.1f}g)")

        # Secondary pass: if plate has remaining space and filament, fill with any other fitting units from pool!
        for u in available_cand_units:
            if u["unit_id"] in global_placed_unit_ids:
                continue
            part_est_w = float(u.get("weight") or 30.0) + float(u.get("support_weight") or 0.0)
            if plate_est_weight + part_est_w > effective_plate_weight_limit:
                continue
            if plate.place_item(u, u["polygon_footprint"]):
                global_placed_unit_ids.add(u["unit_id"])
                plate_est_weight += part_est_w
                log_func(f"   • [Fill] Placed '{u['file_name']}' ({u['sales_order']}, Unit {u.get('qty_index', 1)}/{u.get('total_qty', 1)}) [{u.get('dimensions', '')}] ➔ {part_est_w:.1f}g")

        # Symmetrically center layout on the plate with equal margins around all edges
        plate.center_layout()

        if not plate.placed_items:
            # If spool budget prevented packing, or parts don't fit, mark spool completed so we can move on
            if preferred_spool and printer_mounted_spool.get(assigned_printer) == preferred_spool:
                log_func(f"   ℹ️ Spool '{preferred_spool}' on [{assigned_printer}] has {printer_spool_balance[assigned_printer]:.1f}g remaining, but no unplaced parts fit within this balance.")
                log_func(f"   🏁 Marking spool '{preferred_spool}' completed/freed for Workstation [{assigned_printer}].")
                printer_mounted_spool[assigned_printer] = None
                printer_spool_balance[assigned_printer] = 0.0
                continue
            else:
                log_func(f"   ⚠️ Could not fit remaining parts on plate #{plate_num}. Stopping.")
                break

        # Update spool balance tracking and simulated locks
        if preferred_spool:
            rem_bal = max(spool_budget - plate_est_weight, 0.0)
            printer_spool_balance[assigned_printer] = rem_bal
            simulated_spool_locks[preferred_spool] = simulated_spool_locks.get(preferred_spool, 0.0) + plate_est_weight
            if rem_bal <= DEPLETION_THRESHOLD_GRAMS:
                log_func(f"   🏁 Projected spool '{preferred_spool}' depletion on [{assigned_printer}] ({rem_bal:.1f}g <= {DEPLETION_THRESHOLD_GRAMS:.0f}g threshold). Spool completed.")
                printer_mounted_spool[assigned_printer] = None
                printer_spool_balance[assigned_printer] = 0.0
            else:
                log_func(f"   🧵 Spool Stickiness: Retaining '{preferred_spool}' on [{assigned_printer}] ({rem_bal:.1f}g remaining > {DEPLETION_THRESHOLD_GRAMS:.0f}g).")

        # Rough hours estimate for scheduling load-balancing
        est_plate_g = sum(max(float(it.get('weight') or 30.0) + float(it.get('support_weight') or 0.0), 10.0) for it in plate.placed_items)
        plate_est_hrs = estimate_print_time_hours(est_plate_g)
        plate.est_hours = plate_est_hrs
        plate.ideal_finish_time = plate.ideal_start_time + timedelta(hours=plate_est_hrs)

        # Advance timeline and tracking for this printer
        printer_last_plate_end[assigned_printer] = plate.ideal_finish_time
        printer_last_spool[assigned_printer] = preferred_spool
        printer_timeline[assigned_printer] = plate.ideal_finish_time
        printer_est_hours[assigned_printer] += plate_est_hrs

        planned_plates.append(plate)
        printer_build_counts[assigned_printer] += 1
        
        gap_desc = "15m Spool Swap" if plate.is_spool_change else "10m Bed Cleaning/Turnaround"
        log_func(f"   ✅ Plate #{plate_num} packed with {len(plate.placed_items)} part(s) on [{assigned_printer}].")
        if getattr(plate, "turnaround_gap_minutes", 0):
            log_func(f"      ⏱️ Turnaround Buffer: +{plate.turnaround_gap_minutes}m ({gap_desc})")
        log_func(f"      🕒 Ideal Start: {plate.ideal_start_time.strftime('%Y-%m-%d %H:%M')} ➔ Est Finish: {plate.ideal_finish_time.strftime('%Y-%m-%d %H:%M')} (~{plate_est_hrs:.1f}h)")

    # 4. Slicing, Stores Stock Validation & ERPNext Posting
    log_func(f"\n[Step 4/5] Slicing & Validating Stock for {len(planned_plates)} Generated Build(s)...")
    created_build_names = []
    actual_spool_locks = {}

    for plate in planned_plates:
        p_printer = plate.printer
        p_mat = plate.material
        log_func(f"\n⚙️ Finalizing Build Plate #{plate.plate_id} on Printer [{p_printer}] ({p_mat})...")

        # Generate OrcaSlicer Project (.3mf)
        project_path, geom_weight = generate_build_project_3mf(
            {"placed_items": plate.placed_items},
            build_id=plate.plate_id,
            material_general=p_mat
        )
        log_func(f"   • Generated OrcaSlicer 3MF: {project_path.name}")

        # Headless slice or accurate geometric estimation
        slice_res = slice_plate_with_orcaslicer(project_path, plate.plate_id, material_general=p_mat)
        final_weight = slice_res["grams"] if (slice_res and slice_res.get("grams")) else geom_weight
        
        # Update refined print time if slice returned exact duration
        if slice_res and slice_res.get("print_time_hours"):
            diff = slice_res["print_time_hours"] - plate.est_hours
            printer_est_hours[p_printer] += diff
            plate.est_hours = slice_res["print_time_hours"]
            plate.ideal_finish_time = plate.ideal_start_time + timedelta(hours=plate.est_hours)
            printer_last_plate_end[p_printer] = plate.ideal_finish_time
            # Shift ideal_start_time and ideal_finish_time of subsequent plates on the same printer
            found_this = False
            for other_pl in planned_plates:
                if other_pl is plate:
                    found_this = True
                    continue
                if found_this and other_pl.printer == p_printer and other_pl.ideal_start_time:
                    other_pl.ideal_start_time += timedelta(hours=diff)
                    if getattr(other_pl, "ideal_finish_time", None):
                        other_pl.ideal_finish_time += timedelta(hours=diff)

        log_func(f"   • Estimated Filament: {final_weight:.1f} g | Est Print Time: {plate.est_hours:.1f} h")
        if plate.ideal_start_time:
            log_func(f"   • Ideal Start Time: {plate.ideal_start_time.strftime('%Y-%m-%d %H:%M:%S')}")

        # ERPNext Material Item
        mat_item = plate.placed_items[0].get("material_item") or f"JMG-{p_mat}-BL"

        protected_spools = get_mutex_protected_spools(
            p_printer,
            all_workshop_printers,
            selected_printers,
            printer_schedules,
            printer_mounted_spool,
            printer_build_counts
        )
        # Check stock and select best spool in ERPNext using balance_after_locking, respecting preferred_spool
        available_grams, suggested_spool, spool_weight = get_available_spools_and_stock(
            mat_item,
            required_weight=final_weight,
            simulated_locks=actual_spool_locks,
            preferred_spool=plate.preferred_spool,
            exclude_spools=protected_spools
        )
        log_func(f"   • Stores Stock: {available_grams:.1f} g (Selected Spool: '{suggested_spool}', Usable Balance: {spool_weight:.1f} g)")

        if available_grams >= final_weight and suggested_spool:
            rem_after_slice = max(spool_weight - final_weight, 0.0)
            plate.assigned_spool = suggested_spool
            plate.spool_weight = spool_weight
            plate.final_weight = final_weight
            plate.rem_after_slice = rem_after_slice
            plate.ideal_finish_time = (plate.ideal_start_time + timedelta(hours=plate.est_hours)) if plate.ideal_start_time else None

            log_func(f"   ✅ Stock & Spool Validated ({available_grams:.1f}g in Stores, {spool_weight:.1f}g on Spool).")
            if rem_after_slice <= DEPLETION_THRESHOLD_GRAMS:
                log_func(f"   🏁 Spool Depletion Notice: Spool '{suggested_spool}' remaining balance is {rem_after_slice:.1f}g (<= {DEPLETION_THRESHOLD_GRAMS:.0f}g threshold). Spool will be completed.")
            else:
                log_func(f"   🧵 Spool Continuity: Spool '{suggested_spool}' will have {rem_after_slice:.1f}g remaining (> {DEPLETION_THRESHOLD_GRAMS:.0f}g threshold). Retained on [{p_printer}].")

            if dry_run:
                plate.build_doc_name = f"SIM-BUILD-{plate.plate_id:03d}"
                st_str = plate.ideal_start_time.strftime('%Y-%m-%d %H:%M:%S') if plate.ideal_start_time else 'Immediate'
                log_func(f"   [DRY RUN] Would create ERPNext Print Build Draft on Workstation [{p_printer}] with Spool [{suggested_spool}].")
                log_func(f"   [DRY RUN] Ideal Start: {st_str} | Print Duration: {plate.est_hours:.2f}h")
                log_func(f"   [DRY RUN] Would lock {final_weight:.1f}g on Spool '{suggested_spool}' (new balance: {rem_after_slice:.1f}g).")
                actual_spool_locks[suggested_spool] = actual_spool_locks.get(suggested_spool, 0.0) + final_weight
            else:
                # 1. Create Print Build Draft in ERPNext with assigned printer workstation, ideal start time & print duration!
                build_doc_name = post_print_build_draft(
                    {"placed_items": plate.placed_items},
                    material_item=mat_item,
                    printer=p_printer,
                    suggested_spool=suggested_spool,
                    available_weight=spool_weight,
                    ideal_start_time=plate.ideal_start_time,
                    print_duration=plate.est_hours
                )
                plate.build_doc_name = build_doc_name
                log_func(f"   🎉 CREATED ERPNEXT PRINT BUILD (DRAFT): {build_doc_name} on [{p_printer}]")
                created_build_names.append(build_doc_name)

                # 2. Lock filament on the selected spool in ERPNext so subsequent builds don't duplicate or over-allocate it
                lock_filament_weight_on_spool(suggested_spool, final_weight)
                actual_spool_locks[suggested_spool] = actual_spool_locks.get(suggested_spool, 0.0) + final_weight

                # 3. Rename local 3MF and gcode to match official ERPNext name
                build_folder_name = build_doc_name
                named_project_path = project_path.parent / f"{build_doc_name}.3mf"
                if project_path.exists():
                    try:
                        project_path.replace(named_project_path)
                        project_path = named_project_path
                    except Exception:
                        pass

                files_to_upload = [project_path]
                
                # Pair corresponding .gcode file with .3mf for upload to Google Drive
                gcode_cand = None
                if slice_res and slice_res.get("gcode_path") and Path(slice_res["gcode_path"]).exists():
                    gcode_cand = Path(slice_res["gcode_path"])
                elif (BUILDS_DIR / f"Build_{plate.plate_id}.gcode").exists():
                    gcode_cand = BUILDS_DIR / f"Build_{plate.plate_id}.gcode"
                    
                named_gcode_path = project_path.parent / f"{build_doc_name}.gcode"
                if gcode_cand and gcode_cand.exists():
                    try:
                        if gcode_cand != named_gcode_path:
                            gcode_cand.replace(named_gcode_path)
                        files_to_upload.append(named_gcode_path)
                    except Exception:
                        files_to_upload.append(gcode_cand)
                else:
                    generate_fallback_gcode(named_gcode_path, build_doc_name, p_mat)
                    files_to_upload.append(named_gcode_path)
                    
                log_func(f"   📄 Build files bundled for upload: {[f.name for f in files_to_upload]}")

                # 4. Create Google Drive build folder with exact same name as ERPNext build
                drive_build_url = create_build_folder_and_upload(build_folder_name, files_to_upload)
                if drive_build_url:
                    log_func(f"   ☁️ Created Build Folder on Google Drive: {drive_build_url}")
                    update_print_build_drive_link(build_doc_name, drive_build_url)
                else:
                    log_func(f"   ℹ️ Local project saved: {project_path.name}")
        else:
            plate.assigned_spool = suggested_spool or plate.preferred_spool or "No Spool (Shortage)"
            plate.spool_weight = available_grams
            plate.final_weight = final_weight
            plate.rem_after_slice = 0.0
            plate.ideal_finish_time = (plate.ideal_start_time + timedelta(hours=plate.est_hours)) if plate.ideal_start_time else None
            plate.build_doc_name = f"QUEUED-BUILD-{plate.plate_id:03d}"
            log_func(f"   ❌ STOCK SHORTAGE! Required: {final_weight:.1f}g, Available: {available_grams:.1f}g.")
            log_func(f"   ⚠️ Holding build in local queue (NOT posting to Print Build)...")
            queue_file = save_queued_build(
                {"placed_items": plate.placed_items},
                build_id=plate.plate_id,
                missing_material=mat_item,
                required_weight=final_weight,
                available_weight=available_grams
            )
            log_func(f"   • Saved to local queue: {queue_file.name}")
            so_set = list({it["sales_order"] for it in plate.placed_items})
            send_material_shortage_alert(
                build_id=plate.plate_id,
                material_name=mat_item,
                required_grams=final_weight,
                available_grams=available_grams,
                sales_orders=so_set
            )

    # 5. Throughput, Completion Timeline & Deadline Feasibility Analysis
    log_func("\n" + "=" * 70)
    log_func(f"✨ PLANNING COMPLETED: Total {len(planned_plates)} Build Plate(s) planned across {len(selected_printers)} printer(s).")
    log_func(f"\n📊 Throughput, Printer Schedules & Timeline Analysis:")
    if target_date:
        log_func(f"   • Target Completion Date: {target_date} (~{available_hours:.1f}h available window)")

    # 1. Printer-by-printer timeline summary
    max_printer_finish = max((printer_timeline[p] for p in selected_printers), default=now_dt)
    for p in selected_printers:
        cnt = printer_build_counts.get(p, 0)
        h = printer_est_hours.get(p, 0.0)
        p_info = printer_schedules.get(p, {})
        init_st = p_info.get("status", "Idle")
        running_b = p_info.get("running_build")
        
        final_finish_dt = printer_timeline.get(p, now_dt)
        status_note = f"Est. Completion: {final_finish_dt.strftime('%b %d, %H:%M')}"
        if target_date:
            if final_finish_dt.date() <= target_date:
                status_note += " ✅ ON TRACK"
            else:
                status_note += " ⚠️ EXCEEDS TARGET DATE"
                
        log_func(f"\n   • Workstation [{p}] (Current: {init_st}):")
        if running_b:
            avail_t = p_info.get("next_available_time")
            avail_fmt = avail_t.strftime('%b %d, %H:%M') if avail_t else 'Soon'
            log_func(f"     - Running Build: '{running_b}' finishes ~{avail_fmt}")
        log_func(f"     - Planned Builds: {cnt} | New Work: {h:.1f} hrs | {status_note}")
        
        printer_plates = [pl for pl in planned_plates if pl.printer == p]
        for pl in printer_plates:
            st_fmt = pl.ideal_start_time.strftime('%b %d, %H:%M') if pl.ideal_start_time else "Immediate"
            end_dt = pl.ideal_start_time + timedelta(hours=pl.est_hours) if pl.ideal_start_time else None
            end_fmt = end_dt.strftime('%b %d, %H:%M') if end_dt else "N/A"
            log_func(f"       ‣ Plate #{pl.plate_id} [{pl.material}]: Ideal Start: {st_fmt} ➔ Finish: {end_fmt} ({pl.est_hours:.1f}h)")

    # 2. Check for remaining unplaced units
    unplaced_units = [u for u in part_units if u["unit_id"] not in global_placed_unit_ids]
    unplaced_weight = sum(float(u.get("weight") or 30.0) + float(u.get("support_weight") or 0.0) for u in unplaced_units)
    unplaced_extra_hours = estimate_print_time_hours(unplaced_weight) / max(1, len(selected_printers)) if unplaced_units else 0.0

    ideal_overall_finish_dt = max_printer_finish + timedelta(hours=unplaced_extra_hours)

    # 3. Per-Order Completion Analysis
    order_breakdowns = []
    target_dt = datetime.combine(target_date, datetime.max.time().replace(hour=23, minute=59, second=59)) if target_date else None

    for so in orders:
        so_id = so["sales_order"]
        cust = so.get("customer_name") or "Customer"
        so_due_str = so.get("delivery_date")
        so_due_dt = None
        if so_due_str:
            try:
                so_due_d = datetime.strptime(so_due_str, "%Y-%m-%d").date()
                so_due_dt = datetime.combine(so_due_d, datetime.max.time().replace(hour=23, minute=59, second=59))
            except Exception:
                pass
        
        # Finish time of plates containing parts for this SO
        so_plates = [pl for pl in planned_plates if any(it.get("sales_order") == so_id for it in pl.placed_items)]
        so_placed_finish = max((pl.ideal_start_time + timedelta(hours=pl.est_hours) for pl in so_plates if pl.ideal_start_time), default=now_dt)
        
        # Any unplaced parts for this SO?
        so_unplaced = [u for u in unplaced_units if u.get("sales_order") == so_id]
        if so_unplaced:
            so_unplaced_w = sum(float(u.get("weight") or 30.0) + float(u.get("support_weight") or 0.0) for u in so_unplaced)
            so_extra_h = estimate_print_time_hours(so_unplaced_w) / max(1, len(selected_printers))
            so_finish = max(so_placed_finish, max_printer_finish) + timedelta(hours=so_extra_h)
        else:
            so_finish = so_placed_finish

        # Reference deadline: order's own due date, or the global target_dt
        effective_deadline = target_dt or so_due_dt
        is_delayed = False
        delay_h = 0.0
        if effective_deadline and so_finish > effective_deadline:
            is_delayed = True
            delay_h = (so_finish - effective_deadline).total_seconds() / 3600.0

        order_breakdowns.append({
            "so_id": so_id,
            "customer_name": cust,
            "due_str": so_due_str,
            "finish_dt": so_finish,
            "is_delayed": is_delayed,
            "delay_hours": delay_h,
            "unplaced_count": len(so_unplaced)
        })

    # Overall Feasibility Verdict
    is_overall_delayed = False
    overall_delay_hours = 0.0
    if target_dt:
        if ideal_overall_finish_dt > target_dt:
            is_overall_delayed = True
            overall_delay_hours = (ideal_overall_finish_dt - target_dt).total_seconds() / 3600.0
    elif any(ob["is_delayed"] for ob in order_breakdowns):
        is_overall_delayed = True
        overall_delay_hours = max((ob["delay_hours"] for ob in order_breakdowns if ob["is_delayed"]), default=0.0)

    # 4. Formatted Summary Banner
    log_func("\n" + "=" * 70)
    if is_overall_delayed:
        log_func("🚨 DEADLINE FEASIBILITY ALERT: ORDERS CANNOT BE COMPLETED BY DESIRED END DATE!")
        log_func("=" * 70)
        if target_date:
            log_func(f"🎯 Desired Completion Deadline: {target_date.strftime('%A, %d %B %Y')} (11:59 PM)")
        log_func(f"📅 PROJECTED IDEAL COMPLETION DATE & TIME: {ideal_overall_finish_dt.strftime('%A, %d %B %Y at %I:%M %p')}")
        delay_days = overall_delay_hours / 24.0
        log_func(f"⏱️ Estimated Production Delay: +{overall_delay_hours:.1f} hours (~{delay_days:.1f} days past deadline)")
        if unplaced_units:
            log_func(f"⚠️ Unplaced Pool Parts Remaining: {len(unplaced_units)} item(s) could not fit within the {max_builds} max builds limit.")
        log_func("-" * 70)
        log_func("📋 Per-Order Completion Breakdown:")
        for ob in order_breakdowns:
            due_txt = f"Due: {ob['due_str']}" if ob.get('due_str') else "No date specified"
            status_tag = f"⚠️ DELAYED (+{ob['delay_hours']:.1f}h)" if ob['is_delayed'] else "✅ ON TRACK"
            unplaced_tag = f" ({ob['unplaced_count']} unplaced parts)" if ob['unplaced_count'] > 0 else ""
            log_func(f"  • {ob['so_id']} ({ob['customer_name']}) | {due_txt}:")
            log_func(f"    ➔ Projected Finish: {ob['finish_dt'].strftime('%a, %d %b %Y at %I:%M %p')} [{status_tag}]{unplaced_tag}")
        log_func("-" * 70)
        log_func("💡 Actionable Recovery Options:")
        rec_date = (ideal_overall_finish_dt + timedelta(hours=1)).strftime('%A, %d %B %Y at %I:%M %p')
        log_func(f"  1. Add more printers to the plan to distribute parallel print hours.")
        log_func(f"  2. Extend customer delivery commitment to: {rec_date}.")
    else:
        log_func("🎉 SUCCESS: ALL ORDERS WILL BE COMPLETED WITHIN DESIRED END DATE!")
        log_func("=" * 70)
        if target_date:
            log_func(f"🎯 Desired Completion Deadline: {target_date.strftime('%A, %d %B %Y')} (11:59 PM)")
        log_func(f"📅 Projected Final Completion: {ideal_overall_finish_dt.strftime('%A, %d %B %Y at %I:%M %p')}")
        if target_dt:
            buffer_h = (target_dt - ideal_overall_finish_dt).total_seconds() / 3600.0
            if buffer_h > 0:
                log_func(f"⏱️ Buffer: Completes {buffer_h:.1f} hours (~{buffer_h/24.0:.1f} days) ahead of desired deadline!")
        log_func("-" * 70)
        log_func("📋 Per-Order Completion Breakdown:")
        for ob in order_breakdowns:
            due_txt = f"Due: {ob['due_str']}" if ob.get('due_str') else "No date specified"
            log_func(f"  • {ob['so_id']} ({ob['customer_name']}) | {due_txt}:")
            log_func(f"    ➔ Projected Finish: {ob['finish_dt'].strftime('%a, %d %b %Y at %I:%M %p')} [✅ ON TRACK]")
    log_func("=" * 70)

    # Attach structured summary to returned list
    planned_plates.completion_summary = {
        "target_date": target_date.strftime('%Y-%m-%d') if target_date else None,
        "is_delayed": is_overall_delayed,
        "ideal_finish_dt": ideal_overall_finish_dt,
        "delay_hours": overall_delay_hours,
        "order_breakdowns": order_breakdowns,
        "unplaced_units_count": len(unplaced_units)
    }

    return planned_plates


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ProtoValue3D Multi-Printer Build Planner & OrcaSlicer Automation")
    parser.add_argument("--printer", "--printers", type=str, default=None, help="Specific printer workstation or comma-separated list")
    parser.add_argument("--material", "--materials", type=str, default=None, help="Filter by material or comma-separated materials (e.g. PLA,PETG)")
    parser.add_argument("--orders", type=str, default=None, help="Filter by comma-separated Sales Order IDs")
    parser.add_argument("--target-date", type=str, default=None, help="Target completion date (YYYY-MM-DD)")
    parser.add_argument("--max-builds", type=int, default=MAX_BUILDS, help="Maximum builds limit")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without posting to ERPNext")
    parser.add_argument("--replan", action="store_true", help="Cancel previous draft builds and re-prioritize")
    
    args = parser.parse_args()
    printers = [p.strip() for p in args.printer.split(",")] if args.printer else None
    orders = [o.strip() for o in args.orders.split(",")] if args.orders else None
    run_planner(
        selected_printers=printers,
        selected_materials=args.material,
        selected_sales_orders=orders,
        target_completion_date=args.target_date,
        max_builds=args.max_builds,
        dry_run=args.dry_run,
        replan=args.replan
    )
