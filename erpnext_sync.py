import json
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from datetime import datetime, timedelta
from config import (
    ERPNEXT_URL, ERPNEXT_HEADERS, PRIORITY_DAYS_THRESHOLD,
    AVAILABLE_PRINTERS, DEPLETION_THRESHOLD_GRAMS,
    TURNAROUND_SAME_SPOOL_MINUTES, TURNAROUND_SPOOL_CHANGE_MINUTES
)

# Configure resilient session with automatic retries for traveling / fluctuating network
_session = requests.Session()
_retries = Retry(total=4, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
_adapter = HTTPAdapter(max_retries=_retries)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)

def call_erpnext(endpoint, method="GET", payload=None, params=None):
    url = f"{ERPNEXT_URL}{endpoint}"
    for attempt in range(3):
        try:
            resp = _session.request(
                method=method,
                url=url,
                json=payload if payload is not None else None,
                params=params,
                headers=ERPNEXT_HEADERS,
                timeout=25
            )
            resp.raise_for_status()
            return resp.json()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if attempt == 2:
                raise
            time.sleep(2)

def get_pending_sales_orders(ignore_unstarted_draft_builds=False):
    """
    Fetches open Sales Orders, extracts pending files, and prioritizes:
    - Priority 1: Delivery Date is within next 3 days (Today <= Delivery <= Today + 3 days)
    - Priority 2: Delivery Date is > 3 days or not specified
    """
    today = datetime.now().date()
    priority_threshold = today + timedelta(days=PRIORITY_DAYS_THRESHOLD)
    
    # Query all active builds to check real build existence
    try:
        active_builds_res = call_erpnext("/api/resource/Print Build", params={
            "fields": json.dumps(["name", "docstatus", "start_time"]),
            "filters": json.dumps([["docstatus", "!=", 2]]),
            "limit_page_length": 200
        })
        active_build_map = {b["name"]: b for b in active_builds_res.get("data", [])}
    except Exception:
        active_build_map = {}

    # Query non-cancelled, non-closed Sales Orders
    fields = json.dumps(["name", "delivery_date", "transaction_date", "status", "customer_name"])
    filters = json.dumps([["status", "not in", ["Cancelled", "Closed"]]])
    params = {
        "fields": fields,
        "filters": filters,
        "limit_page_length": 100,
        "order_by": "delivery_date asc"
    }
    
    res = call_erpnext("/api/resource/Sales Order", params=params)
    so_list = res.get("data", [])
    
    pending_orders = []
    
    for so_summary in so_list:
        so_name = so_summary["name"]
        so_detail = call_erpnext(f"/api/resource/Sales%20Order/{so_name}").get("data", {})
        
        # Determine effective delivery date
        deliv_date_str = so_detail.get("delivery_date")
        if not deliv_date_str:
            # Check items
            for item in so_detail.get("items", []):
                if item.get("delivery_date"):
                    deliv_date_str = item.get("delivery_date")
                    break
        
        deliv_date = None
        if deliv_date_str:
            try:
                deliv_date = datetime.strptime(deliv_date_str, "%Y-%m-%d").date()
            except Exception:
                deliv_date = None
                
        # Priority Tier
        if deliv_date and deliv_date <= priority_threshold:
            priority = 1
            priority_label = f"HIGH (Due in <= {PRIORITY_DAYS_THRESHOLD} days: {deliv_date})"
        else:
            priority = 2
            priority_label = f"NORMAL (Due: {deliv_date or 'No date'})"
            
        # Extract pending unbuilt files from custom_file_details
        files_to_build = []
        for file_row in so_detail.get("custom_file_details", []):
            row_status = file_row.get("status") or "Not Printed"
            existing_build = file_row.get("build")
            
            # If row is marked Completed, Invoiced, or Delivered, skip it
            if row_status in ["Completed", "Invoiced", "Delivered"]:
                continue

            # Check if linked build is truly active in ERPNext
            is_built = False
            if existing_build:
                if existing_build in active_build_map:
                    b_doc = active_build_map[existing_build]
                    if ignore_unstarted_draft_builds and b_doc.get("docstatus") == 0 and not b_doc.get("start_time"):
                        is_built = False
                    else:
                        is_built = True
                else:
                    # Build was deleted or cancelled in ERPNext
                    is_built = False

            if not is_built:
                qty = int(file_row.get("quantity") or 1)
                files_to_build.append({
                    "row_name": file_row.get("name"),
                    "file_name": file_row.get("file_name"),
                    "description": file_row.get("description", ""),
                    "dimensions": file_row.get("dimensions") or "",
                    "weight": float(file_row.get("weight") or 0.0),
                    "support_weight": float(file_row.get("support_weight") or 0.0),
                    "material_general": file_row.get("material_general") or "PLA",
                    "material_item": file_row.get("material_item") or "JMG-PLA-BL",
                    "custom_finished_good": file_row.get("custom_finished_good"),
                    "order_type": so_detail.get("order_type") or "",
                    "quantity": qty,
                    "sales_order": so_name,
                    "customer_name": so_detail.get("customer_name"),
                    "custom_deal_name": so_detail.get("custom_deal_name"),
                    "custom_google_drive_link": so_detail.get("custom_google_drive_link")
                })
                
        if files_to_build:
            pending_orders.append({
                "sales_order": so_name,
                "customer_name": so_detail.get("customer_name"),
                "custom_deal_name": so_detail.get("custom_deal_name"),
                "custom_google_drive_link": so_detail.get("custom_google_drive_link"),
                "delivery_date": str(deliv_date) if deliv_date else None,
                "priority": priority,
                "priority_label": priority_label,
                "files": files_to_build
            })
            
    # Sort orders: Priority 1 first, then sorted by delivery date
    pending_orders.sort(key=lambda x: (x["priority"], x["delivery_date"] or "9999-99-99"))
    return pending_orders


def get_available_spools_and_stock(material_item, required_weight=0.0, simulated_locks=None, preferred_spool=None, exclude_spools=None):
    """
    Checks stock in Stores for the specified material item.
    - Reads stock balance directly in grams (ERPNext actual_qty is already in grams).
    - Checks Filament Spool doctype, using 'balance_after_locking' as the checkpoint to avoid over-allocating spools.
    - Supports simulated_locks dictionary for dry-run in-memory reservations.
    - Supports preferred_spool for strict printer stickiness (never stolen or abandoned).
    - Supports exclude_spools to prevent choosing spools currently mounted on other printers.
    Returns (total_stock_grams, suggested_spool_code, spool_usable_balance).
    """
    try:
        # 1. Check Bin for total stock balance in Stores (ALREADY IN GRAMS)
        fields = json.dumps(["warehouse", "actual_qty"])
        filters = json.dumps([["item_code", "=", material_item]])
        res = call_erpnext("/api/resource/Bin", params={"fields": fields, "filters": filters})
        bins = res.get("data", [])
        
        total_stock_grams = sum(float(b.get("actual_qty", 0.0)) for b in bins)
        if simulated_locks:
            total_stock_grams = max(total_stock_grams - sum(simulated_locks.values()), 0.0)
        
        # 2. Query Filament Spool doctype for actual serial codes with locked weights
        spool_fields = json.dumps([
            "name", "material_item", "status", "available_weight",
            "locked_filament_weight", "balance_after_locking"
        ])
        spool_filters = json.dumps([
            ["material_item", "=", material_item],
            ["status", "!=", "Depleted"]
        ])
        spool_res = call_erpnext("/api/resource/Filament Spool", params={
            "fields": spool_fields,
            "filters": spool_filters,
            "limit_page_length": 100
        })
        spools = spool_res.get("data", [])
        
        suggested_spool = None
        spool_available_weight = 0.0
        
        if spools:
            # Compute effective usable balance after locking for each spool
            for s in spools:
                avail = float(s.get("available_weight") or 0.0)
                locked = float(s.get("locked_filament_weight") or 0.0)
                bal = float(s.get("balance_after_locking")) if s.get("balance_after_locking") is not None else (avail - locked)
                if simulated_locks and s["name"] in simulated_locks:
                    bal -= float(simulated_locks[s["name"]])
                s["effective_balance"] = max(bal, 0.0)

            # Strict Preferred Spool Stickiness: if preferred_spool is mounted, stick to it exclusively
            if preferred_spool:
                matching_pref = [s for s in spools if s["name"] == preferred_spool]
                if matching_pref:
                    chosen = matching_pref[0]
                    suggested_spool = chosen["name"]
                    spool_available_weight = float(chosen.get("effective_balance") or 0.0)
                    return total_stock_grams, suggested_spool, spool_available_weight

            # Workstation Spool Isolation: Filter out spools mounted on other workstations
            if exclude_spools:
                exclude_set = set(exclude_spools)
                spools = [s for s in spools if s["name"] not in exclude_set]

            # Filter spools where balance_after_locking >= required_weight
            suitable_spools = [s for s in spools if s["effective_balance"] >= required_weight]
            
            if suitable_spools:
                # Best-Fit: sort ascending so we consume partially filled spools first
                suitable_spools.sort(key=lambda s: s["effective_balance"])
                in_use_suitable = [s for s in suitable_spools if s.get("status") == "In Use"]
                chosen = in_use_suitable[0] if in_use_suitable else suitable_spools[0]
            elif spools:
                # If no single spool has enough balance, pick the fullest available remaining spool
                spools.sort(key=lambda s: s["effective_balance"], reverse=True)
                chosen = spools[0]
            else:
                return total_stock_grams, None, 0.0
                
            suggested_spool = chosen["name"]
            spool_available_weight = float(chosen.get("effective_balance") or 0.0)
        else:
            # Fallback check
            any_spool_res = call_erpnext("/api/resource/Filament Spool", params={"fields": '["name"]', "limit_page_length": 1})
            any_spools = any_spool_res.get("data", [])
            if any_spools:
                suggested_spool = any_spools[0]["name"]
                spool_available_weight = total_stock_grams
            else:
                suggested_spool = ""
                spool_available_weight = total_stock_grams
                
        return total_stock_grams, suggested_spool, spool_available_weight
        
    except Exception as e:
        print(f"[ERPNext Sync] Warning checking spool stock: {e}")
        return 0.0, None, 0.0


def lock_filament_weight_on_spool(spool_name, weight_grams):
    """
    Locks filament weight on the chosen spool in ERPNext.
    Increments locked_filament_weight and updates balance_after_locking.
    """
    if not spool_name or weight_grams <= 0:
        return
    try:
        spool_doc = call_erpnext(f"/api/resource/Filament%20Spool/{spool_name}").get("data", {})
        avail = float(spool_doc.get("available_weight") or 0.0)
        curr_locked = float(spool_doc.get("locked_filament_weight") or 0.0)
        new_locked = round(curr_locked + weight_grams, 1)
        new_bal = round(max(avail - new_locked, 0.0), 1)
        
        call_erpnext(
            f"/api/resource/Filament%20Spool/{spool_name}",
            method="PUT",
            payload={
                "locked_filament_weight": new_locked,
                "balance_after_locking": new_bal
            }
        )
        print(f"[ERPNext Sync] Locked {weight_grams:.1f}g on Spool {spool_name}. Balance after locking: {new_bal:.1f}g (Total locked: {new_locked:.1f}g)")
    except Exception as e:
        print(f"[ERPNext Sync] Notice locking filament on spool {spool_name}: {e}")


def reset_locked_filament_on_spools():
    """
    Called upon Re-Plan.
    Resets locked_filament_weight to 0.0 and restores balance_after_locking = available_weight
    for all spools, providing a fresh checkpoint.
    """
    try:
        spool_res = call_erpnext("/api/resource/Filament Spool", params={"limit_page_length": 100})
        for s in spool_res.get("data", []):
            s_name = s["name"]
            doc = call_erpnext(f"/api/resource/Filament%20Spool/{s_name}").get("data", {})
            avail = float(doc.get("available_weight") or 0.0)
            call_erpnext(
                f"/api/resource/Filament%20Spool/{s_name}",
                method="PUT",
                payload={
                    "locked_filament_weight": 0.0,
                    "balance_after_locking": avail
                }
            )
        print("[ERPNext Sync] Reset locked filament weight to 0.0 across all spools for Re-Plan.")
    except Exception as e:
        print(f"[ERPNext Sync] Notice resetting locked filament: {e}")


def get_printer_status_and_schedule(printers):
    """
    Checks workstation status (Idle, Printing) and inspects in-progress builds.
    If a real start_time is filled (Start button was clicked), calculates projected
    end time from print_duration and schedules new builds only after completion.
    Also detects any currently mounted spool and its remaining usable balance
    to enforce spool stickiness and spool completion across builds.
    Returns: dict mapping printer_name -> {status, is_printing, next_available_time, projected_end_time, running_build, mounted_spool, mounted_material, spool_balance}
    """
    now = datetime.now()
    schedule = {}
    
    for p in printers:
        is_printing = False
        running_build = None
        next_available_dt = now
        projected_end_dt = None
        printer_status = "Idle"
        mounted_spool = None
        mounted_material = None
        spool_balance = 0.0
        
        try:
            # Query Workstation status
            ws = call_erpnext(f"/api/resource/Workstation/{p}").get("data", {})
            ws_status = ws.get("custom_printer_status") or "Idle"
            
            # Query Print Builds on this printer
            fields = json.dumps(["name", "start_time", "end_time", "print_duration", "ideal_start_time", "docstatus", "printer", "spool", "material", "creation"])
            filters = json.dumps([["printer", "=", p]])
            pb_res = call_erpnext("/api/resource/Print Build", params={
                "fields": fields,
                "filters": filters,
                "limit_page_length": 10,
                "order_by": "creation desc"
            })
            builds = pb_res.get("data", [])
            
            for b in builds:
                st_str = b.get("start_time")
                et_str = b.get("end_time")
                # If Start button was clicked and Finish not clicked yet
                if st_str and not et_str and b.get("docstatus", 0) == 0:
                    try:
                        st_dt = datetime.strptime(st_str.split(".")[0], "%Y-%m-%d %H:%M:%S")
                    except Exception:
                        st_dt = now
                        
                    duration_hrs = float(b.get("print_duration") or 2.0)
                    proj_end = st_dt + timedelta(hours=duration_hrs)
                    
                    if proj_end > now:
                        is_printing = True
                        running_build = b["name"]
                        projected_end_dt = proj_end
                        next_available_dt = proj_end
                        printer_status = "Printing"
                        mounted_spool = b.get("spool")
                        mounted_material = b.get("material")
                        break
                    else:
                        # Overdue but not marked finished
                        is_printing = True
                        running_build = b["name"]
                        projected_end_dt = now + timedelta(minutes=15)
                        next_available_dt = projected_end_dt
                        printer_status = "Printing (Finishing Soon)"
                        mounted_spool = b.get("spool")
                        mounted_material = b.get("material")
                        break
                        
            # If not actively printing, check most recent build on this printer for loaded spool
            if not mounted_spool and builds:
                latest_b = builds[0]
                mounted_spool = latest_b.get("spool")
                mounted_material = latest_b.get("material")

            # Check remaining usable balance on mounted spool
            if mounted_spool:
                try:
                    s_doc = call_erpnext(f"/api/resource/Filament%20Spool/{mounted_spool}").get("data", {})
                    bal = float(s_doc.get("balance_after_locking") if s_doc.get("balance_after_locking") is not None else (s_doc.get("available_weight") or 0.0))
                    spool_balance = max(bal, 0.0)
                    st = s_doc.get("status")
                    if st == "Depleted" or spool_balance <= DEPLETION_THRESHOLD_GRAMS:
                        mounted_spool = None
                        mounted_material = None
                        spool_balance = 0.0
                    else:
                        # Normalize material name (PLA, PETG, ABS, TPU)
                        mat_str = str(mounted_material or s_doc.get("material_item") or "").upper()
                        if "PETG" in mat_str: mounted_material = "PETG"
                        elif "ABS" in mat_str: mounted_material = "ABS"
                        elif "TPU" in mat_str: mounted_material = "TPU"
                        else: mounted_material = "PLA"
                except Exception:
                    mounted_spool = None
                    spool_balance = 0.0

            if not is_printing and ws_status == "In Production":
                printer_status = "In Production"
                next_available_dt = now + timedelta(minutes=30)
                
        except Exception as e:
            print(f"[ERPNext Sync] Notice checking status for {p}: {e}")
            
        schedule[p] = {
            "status": printer_status,
            "is_printing": is_printing,
            "next_available_time": next_available_dt,
            "projected_end_time": projected_end_dt,
            "running_build": running_build,
            "mounted_spool": mounted_spool,
            "mounted_material": mounted_material,
            "spool_balance": spool_balance
        }
        
    return schedule


def cancel_unstarted_draft_builds(selected_printers=None):
    """
    Cancels draft Print Builds where start_time is NOT set (Start button not clicked).
    Builds where Start button was clicked are preserved (physically printing).
    """
    try:
        filters = [["docstatus", "=", 0]]
        if selected_printers:
            filters.append(["printer", "in", selected_printers])
            
        params = {
            "fields": json.dumps(["name", "printer", "start_time"]),
            "filters": json.dumps(filters),
            "limit_page_length": 100
        }
        res = call_erpnext("/api/resource/Print Build", params=params)
        drafts = res.get("data", [])
        
        cancelled = []
        for d in drafts:
            # Only cancel if Start button was NOT clicked
            if not d.get("start_time"):
                b_name = d["name"]
                try:
                    # Unlink sales order files before deleting build
                    full_b = call_erpnext(f"/api/resource/Print%20Build/{b_name}").get("data", {})
                    for bf in full_b.get("build_files", []):
                        row_ref = bf.get("file_row_reference")
                        if row_ref:
                            try:
                                call_erpnext(
                                    "/api/method/frappe.client.set_value",
                                    method="POST",
                                    payload={
                                        "doctype": "sales order files",
                                        "name": row_ref,
                                        "fieldname": "build",
                                        "value": ""
                                    }
                                )
                                call_erpnext(
                                    "/api/method/frappe.client.set_value",
                                    method="POST",
                                    payload={
                                        "doctype": "sales order files",
                                        "name": row_ref,
                                        "fieldname": "status",
                                        "value": "Not Printed"
                                    }
                                )
                            except Exception:
                                pass
                    call_erpnext(f"/api/resource/Print%20Build/{b_name}", method="DELETE")
                    cancelled.append(b_name)
                    print(f"[ERPNext Sync] Cancelled unstarted draft build {b_name} on {d.get('printer')} for re-planning.")
                except Exception as e:
                    print(f"[ERPNext Sync] Notice cancelling draft {b_name}: {e}")
                    
        return cancelled
    except Exception as e:
        print(f"[ERPNext Sync] Notice checking unstarted draft builds: {e}")
        return []


def post_print_build_draft(plate_data, material_item, printer, suggested_spool, available_weight, drive_link=None, ideal_start_time=None, print_duration=None):
    """
    Creates Print Build in ERPNext as Draft (docstatus = 0, without submitting).
    Saves ideal_start_time and print_duration from slicer.
    Leaves start_time blank (will be populated when user clicks Start button).
    """
    sales_orders_set = list({item["sales_order"] for item in plate_data["placed_items"]})
    
    build_files_rows = []
    for item in plate_data["placed_items"]:
        ot = item.get("order_type", "")
        target_wh = "Finished Goods - PVlabs"
        fg = item.get("custom_finished_good")
        if ot == "Display Parts":
            target_wh = "DisplayParts - PVlabs"
            if not fg:
                mat = (item.get("material_general") or material_item).upper()
                if "PETG" in mat: fg = "DISPLAY_PETG"
                elif "ABS" in mat: fg = "DISPLAY_ABS"
                elif "RESIN" in mat or "SLA" in mat: fg = "DISPLAY_RESIN"
                elif "TPU" in mat: fg = "DISPLAY_TPU"
                else: fg = "DISPLAY_PLA"
        elif ot == "Product Development" or "Testing" in ot:
            target_wh = "Testing - PVlabs"

        build_files_rows.append({
            "sales_order": item["sales_order"],
            "source_type": "Sales Order",
            "file_row_reference": item.get("row_name", ""),
            "file_name": item["file_name"],
            "weight": float(item.get("weight") or 0.0),
            "support_weight": float(item.get("support_weight") or 0.0),
            "custom_finished_good": fg,
            "target_warehouse": target_wh
        })
        
    sales_order_rows = [{"sales_order": so} for so in sales_orders_set]
    
    payload = {
        "docstatus": 0,  # DRAFT ONLY
        "printer": printer,
        "material": material_item,
        "spool": suggested_spool,
        "spool_available_weight": available_weight,
        "sales_orders": sales_order_rows,
        "build_files": build_files_rows
    }
    
    if drive_link:
        payload["custom_drive_link"] = drive_link

    if ideal_start_time:
        if isinstance(ideal_start_time, datetime):
            payload["ideal_start_time"] = ideal_start_time.strftime("%Y-%m-%d %H:%M:%S")
        else:
            payload["ideal_start_time"] = str(ideal_start_time)
            
    if print_duration is not None:
        payload["print_duration"] = round(float(print_duration), 2)
    
    try:
        res = call_erpnext("/api/resource/Print Build", method="POST", payload=payload)
    except requests.exceptions.HTTPError as err:
        error_msg = ""
        try:
            err_json = err.response.json()
            error_msg = err_json.get("exception") or str(err_json.get("_server_messages", ""))
        except Exception:
            error_msg = err.response.text
        raise RuntimeError(f"ERPNext Error: {error_msg}")
        
    created_doc = res.get("data", {})
    build_name = created_doc.get("name")
    
    # Update back-link on child rows in Sales Orders
    for item in plate_data["placed_items"]:
        so_name = item["sales_order"]
        row_name = item.get("row_name")
        if row_name and build_name:
            try:
                # Update custom_file_details row build link and status via RPC set_value
                call_erpnext(
                    "/api/method/frappe.client.set_value",
                    method="POST",
                    payload={
                        "doctype": "sales order files",
                        "name": row_name,
                        "fieldname": "build",
                        "value": build_name
                    }
                )
                call_erpnext(
                    "/api/method/frappe.client.set_value",
                    method="POST",
                    payload={
                        "doctype": "sales order files",
                        "name": row_name,
                        "fieldname": "status",
                        "value": "Build"
                    }
                )
            except Exception as e:
                print(f"[ERPNext Sync] Notice updating SO row {row_name}: {e}")
                
    return build_name


def cancel_draft_builds(build_names):
    """
    Deletes or cancels unsubmitted Draft Print Builds for dynamic re-planning.
    """
    for b_name in build_names:
        try:
            full_b = call_erpnext(f"/api/resource/Print%20Build/{b_name}").get("data", {})
            for bf in full_b.get("build_files", []):
                row_ref = bf.get("file_row_reference")
                if row_ref:
                    try:
                        call_erpnext(
                            "/api/method/frappe.client.set_value",
                            method="POST",
                            payload={
                                "doctype": "sales order files",
                                "name": row_ref,
                                "fieldname": "build",
                                "value": ""
                            }
                        )
                        call_erpnext(
                            "/api/method/frappe.client.set_value",
                            method="POST",
                            payload={
                                "doctype": "sales order files",
                                "name": row_ref,
                                "fieldname": "status",
                                "value": "Not Printed"
                            }
                        )
                    except Exception:
                        pass
            call_erpnext(f"/api/resource/Print%20Build/{b_name}", method="DELETE")
            print(f"[ERPNext Sync] Cancelled draft build {b_name} for re-planning.")
        except Exception as e:
            print(f"[ERPNext Sync] Notice cancelling build {b_name}: {e}")


def update_print_build_drive_link(build_name, drive_link):
    """
    Updates the custom_drive_link on an existing Print Build in ERPNext.
    """
    if not build_name or not drive_link:
        return
    try:
        call_erpnext(
            f"/api/resource/Print%20Build/{build_name}",
            method="PUT",
            payload={"custom_drive_link": drive_link}
        )
        print(f"[ERPNext Sync] Attached Drive Link to Print Build {build_name}: {drive_link}")
    except Exception as e:
        print(f"[ERPNext Sync] Notice updating Print Build drive link: {e}")


def get_available_printers():
    """
    Discovers available 3D printers from ERPNext Workstation doctype.
    Filters by FDM_Printer workstation_type and not disabled.
    Falls back to AVAILABLE_PRINTERS if offline or empty.
    """
    try:
        fields = json.dumps(["name", "workstation_name", "custom_printer_status"])
        filters = json.dumps([
            ["workstation_type", "=", "FDM_Printer"],
            ["disabled", "=", 0]
        ])
        res = call_erpnext("/api/resource/Workstation", params={"fields": fields, "filters": filters})
        printers = [w["name"] for w in res.get("data", [])]
        if printers:
            return printers
    except Exception as e:
        print(f"[ERPNext Sync] Notice retrieving workstations from ERPNext: {e}")
    return list(AVAILABLE_PRINTERS)


def get_all_active_print_builds():
    """
    Fetches active/draft Print Build records from ERPNext along with their child tables
    (sales_orders and build_files) for visualization in the Production Gantt Chart.
    """
    try:
        fields = json.dumps([
            "name", "printer", "material", "spool",
            "spool_available_weight", "ideal_start_time",
            "print_duration", "docstatus", "creation", "start_time", "end_time"
        ])
        filters = json.dumps([["docstatus", "!=", 2]])
        res = call_erpnext("/api/resource/Print Build", params={
            "fields": fields,
            "filters": filters,
            "limit_page_length": 50,
            "order_by": "creation desc"
        })
        build_summaries = res.get("data", [])
        detailed_builds = []
        for bs in build_summaries:
            b_name = bs.get("name")
            if not b_name:
                continue
            try:
                full_doc = call_erpnext(f"/api/resource/Print Build/{b_name}").get("data", {})
                if full_doc:
                    detailed_builds.append(full_doc)
                else:
                    detailed_builds.append(bs)
            except Exception:
                detailed_builds.append(bs)
        return detailed_builds
    except Exception as e:
        print(f"[ERPNext Sync] Notice fetching active Print Builds: {e}")
        return []


def slide_unstarted_builds_and_cascade(selected_printers=None, dry_run=False, log_func=print):
    """
    Checks all active Draft Print Builds across printers.
    If an unstarted build (start_time is empty) has an ideal_start_time in the past
    (e.g., planned for 1:00 PM but current time is 1:11 PM), shifts its ideal_start_time
    forward to current time (1:11 PM), and cascades all subsequent unstarted builds on
    that printer forward, respecting:
      - 10 min turnaround gap if consecutive prints use the same spool (bed cleaning/part removal)
      - 15 min turnaround gap if consecutive prints require a spool change
    Updates ERPNext Print Build records and returns list of updated builds.
    """
    now = datetime.now()
    updated_builds = []
    
    try:
        fields = json.dumps([
            "name", "printer", "material", "spool",
            "spool_available_weight", "ideal_start_time",
            "print_duration", "docstatus", "creation", "start_time", "end_time"
        ])
        filters = json.dumps([["docstatus", "=", 0]])
        res = call_erpnext("/api/resource/Print Build", params={
            "fields": fields,
            "filters": filters,
            "limit_page_length": 100,
            "order_by": "creation asc"
        })
        all_drafts = res.get("data", [])
    except Exception as e:
        log_func(f"[ERPNext Sync] Notice querying draft builds for auto-slide: {e}")
        return []
        
    if not all_drafts:
        return []

    # Group drafts by printer
    printers_map = {}
    for d in all_drafts:
        p = d.get("printer")
        if not p:
            continue
        if selected_printers and p not in selected_printers:
            continue
        printers_map.setdefault(p, []).append(d)

    for p, builds in printers_map.items():
        # Check active printing build (if Start was clicked)
        active_printing = [b for b in builds if b.get("start_time") and not b.get("end_time")]
        unstarted = [b for b in builds if not b.get("start_time")]
        
        earliest_avail_time = now
        last_spool = None
        
        if active_printing:
            act = active_printing[-1]
            last_spool = act.get("spool")
            st_str = act.get("start_time")
            dur = float(act.get("print_duration") or 2.0)
            try:
                st_dt = datetime.strptime(st_str.split(".")[0], "%Y-%m-%d %H:%M:%S")
            except Exception:
                st_dt = now
            act_finish = st_dt + timedelta(hours=dur)
            earliest_avail_time = max(act_finish, now)
        else:
            try:
                ws_status = get_printer_status_and_schedule([p]).get(p, {})
                last_spool = ws_status.get("mounted_spool")
            except Exception:
                pass

        if not unstarted:
            continue

        def parse_ideal_start(b):
            s = b.get("ideal_start_time") or b.get("creation")
            if s:
                try:
                    return datetime.strptime(str(s).split(".")[0], "%Y-%m-%d %H:%M:%S")
                except Exception:
                    pass
            return now
            
        unstarted.sort(key=parse_ideal_start)
        
        current_prev_finish = earliest_avail_time
        is_first_unstarted = True

        for b in unstarted:
            b_name = b.get("name")
            cur_ideal_dt = parse_ideal_start(b)
            dur = float(b.get("print_duration") or 2.0)
            cur_spool = b.get("spool")
            
            # Determine turnaround gap after previous print
            if is_first_unstarted and not active_printing:
                if last_spool and cur_spool and last_spool != cur_spool:
                    gap_mins = TURNAROUND_SPOOL_CHANGE_MINUTES
                else:
                    gap_mins = 0
            else:
                if last_spool and cur_spool and last_spool == cur_spool:
                    gap_mins = TURNAROUND_SAME_SPOOL_MINUTES
                else:
                    gap_mins = TURNAROUND_SPOOL_CHANGE_MINUTES
                    
            min_allowed_start = current_prev_finish + timedelta(minutes=gap_mins)
            new_ideal_dt = max(cur_ideal_dt, min_allowed_start)
            
            # If changed by >= 30 seconds
            if (new_ideal_dt - cur_ideal_dt).total_seconds() >= 30:
                shift_mins = round((new_ideal_dt - cur_ideal_dt).total_seconds() / 60.0)
                new_start_str = new_ideal_dt.strftime("%Y-%m-%d %H:%M:%S")
                old_start_str = cur_ideal_dt.strftime("%Y-%m-%d %H:%M:%S")
                
                if not dry_run:
                    try:
                        call_erpnext(
                            f"/api/resource/Print Build/{b_name}",
                            method="PUT",
                            payload={"ideal_start_time": new_start_str}
                        )
                    except Exception as e:
                        log_func(f"[ERPNext Sync] Notice updating ideal_start_time for {b_name}: {e}")

                updated_builds.append({
                    "build_name": b_name,
                    "printer": p,
                    "old_start": old_start_str,
                    "new_start": new_start_str,
                    "shift_mins": shift_mins,
                    "gap_mins": gap_mins
                })
                log_func(f"⏱️ [Auto-Slide] Build {b_name} on [{p}] shifted {old_start_str} ➔ {new_start_str} (+{shift_mins}m, {gap_mins}m gap).")
                
            current_prev_finish = new_ideal_dt + timedelta(hours=dur)
            last_spool = cur_spool
            is_first_unstarted = False
            
    return updated_builds



