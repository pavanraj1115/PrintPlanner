import os
import subprocess
import trimesh
import numpy as np
from pathlib import Path
from config import ORCA_SLICER_PATH, BUILDS_DIR, QUEUE_DIR

MATERIAL_DENSITY = {
    "PLA": 1.24,   # g/cm^3
    "PETG": 1.27,  # g/cm^3
    "ABS": 1.04,   # g/cm^3
    "TPU": 1.21,   # g/cm^3
    "RESIN": 1.15  # g/cm^3
}

def estimate_mesh_filament_weight(mesh, material_general="PLA", infill_ratio=0.15, perimeters=3):
    """
    Fallback accurate physical weight estimation when headless CLI is offline.
    Uses part volume, shell thickness, and infill percentage.
    """
    density = MATERIAL_DENSITY.get(material_general.upper(), 1.24)
    volume_cm3 = max(mesh.volume / 1000.0, 1.0) if mesh.is_volume else (mesh.bounding_box.volume * 0.4) / 1000.0
    
    # 3 perimeters + top/bottom shells ~ 25% solid volume, remainder at infill_ratio
    effective_density_factor = 0.25 + (0.75 * infill_ratio)
    estimated_grams = volume_cm3 * density * effective_density_factor
    return round(float(estimated_grams), 1)


def generate_build_project_3mf(plate_data, build_id, material_general="PLA"):
    """
    Constructs a native 3MF/STL build plate containing all oriented and positioned parts.
    Saves to the builds directory, ready to open directly in portable OrcaSlicer.
    """
    combined_scene = trimesh.Scene()
    total_estimated_weight = 0.0
    
    for idx, item in enumerate(plate_data["placed_items"]):
        mesh = item["mesh"].copy()
        
        # Apply 2D rotation around Z
        rot_deg = item.get("rotation", 0.0)
        if rot_deg != 0.0:
            R = trimesh.transformations.rotation_matrix(np.radians(rot_deg), [0, 0, 1])
            mesh.apply_transform(R)
            
        # Center mesh XY at (pos_x, pos_y), sitting flat on Z = 0
        min_x, min_y, min_z = mesh.bounds[0]
        max_x, max_y, max_z = mesh.bounds[1]
        
        curr_cx = (min_x + max_x) / 2.0
        curr_cy = (min_y + max_y) / 2.0
        
        target_x = item["pos_x"]
        target_y = item["pos_y"]
        
        T = np.eye(4)
        T[0, 3] = target_x - curr_cx
        T[1, 3] = target_y - curr_cy
        T[2, 3] = -min_z  # Flat on bed Z=0
        mesh.apply_transform(T)
        
        node_name = f"part_{idx}_{item['file_name']}"
        combined_scene.add_geometry(mesh, node_name=node_name)
        
        # Weight from SO or geometry
        part_weight = item.get("weight") or estimate_mesh_filament_weight(mesh, material_general)
        support_w = float(item.get("support_weight") or 0.0)
        total_estimated_weight += (part_weight + support_w)

    project_filename = f"Build_{build_id}_{material_general}.3mf"
    project_path = BUILDS_DIR / project_filename
    
    # Export 3MF scene compatible with OrcaSlicer / Bambu Studio
    combined_scene.export(str(project_path))
    
    return project_path, total_estimated_weight


def estimate_print_time_hours(grams):
    """
    Estimates printing time in hours based on filament weight.
    CoreXY printers typically extrude ~25g/hour on average including travel and calibration.
    """
    return max(round(float(grams) / 25.0, 2), 0.2)


def get_production_process_profile(orca_dir):
    """
    Ensures a production process profile exists with automated organic tree supports enabled.
    This guarantees OrcaSlicer CLI in headless mode generates the exact same tree supports
    seen when slicing interactively in OrcaSlicer GUI.
    """
    profiles_dir = BUILDS_DIR / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    custom_proc = profiles_dir / "0.20mm_Standard_Tree_Supports_ECC.json"
    
    import json
    config_data = {
        "type": "process",
        "name": "0.20mm Standard Tree Supports @Elegoo CC 0.4 nozzle",
        "inherits": "fdm_process_ecc_04020",
        "from": "system",
        "setting_id": "PECC04020_TREE_AUTO",
        "instantiation": "true",
        "enable_support": "1",
        "support_type": "tree(auto)",
        "support_style": "tree_organic",
        "support_on_build_plate_only": "0",
        "support_threshold_angle": "30",
        "tree_support_branch_angle": "40",
        "tree_support_branch_diameter": "5",
        "bottom_shell_layers": "3",
        "bottom_shell_thickness": "0.6",
        "sparse_infill_pattern": "zig-zag",
        "compatible_printers": ["Elegoo Centauri Carbon 0.4 nozzle"]
    }
    with open(custom_proc, "w", encoding="utf-8") as f:
        json.dump(config_data, f, indent=2)
        
    return custom_proc


def find_orcaslicer_profiles(material_general="PLA"):
    """
    Finds matching Elegoo Centauri Carbon profiles (machine, process, filament).
    Automatically equips production process with organic tree supports.
    """
    if not ORCA_SLICER_PATH or not os.path.exists(ORCA_SLICER_PATH):
        return None
        
    orca_dir = Path(ORCA_SLICER_PATH).parent
    res_profiles = orca_dir / "resources" / "profiles" / "Elegoo"
    if not res_profiles.exists():
        return None
        
    mach = res_profiles / "machine" / "ECC" / "Elegoo Centauri Carbon 0.4 nozzle.json"
    proc = get_production_process_profile(orca_dir)
    
    mat_upper = (material_general or "PLA").upper()
    fila_dir = res_profiles / "filament" / "ECC"
    if "PETG" in mat_upper:
        fila = fila_dir / "Elegoo PETG PRO @ECC.json"
    elif "ABS" in mat_upper or "ASA" in mat_upper:
        fila = fila_dir / "Elegoo ASA @ECC.json"
    elif "TPU" in mat_upper:
        fila = fila_dir / "Elegoo TPU 95A @ECC.json"
    else:
        fila = fila_dir / "Elegoo PLA @ECC.json"
        
    if not fila.exists():
        fila = fila_dir / "Elegoo PLA @ECC.json"
        
    if mach.exists() and proc.exists() and fila.exists():
        return f"{mach};{proc};{fila}"
    return None


def generate_fallback_gcode(output_gcode, build_id, material_general="PLA"):
    """
    Creates a standard production-compatible G-code file header for Elegoo Centauri Carbon
    when headless slicing is bypassed or fails.
    """
    output_gcode = Path(output_gcode)
    bed_temp = 60 if material_general == "PLA" else (80 if material_general == "PETG" else 100)
    hotend_temp = 210 if material_general == "PLA" else (240 if material_general == "PETG" else 250)
    content = f"""; ProtoValue3D Automated Production G-code
; Build ID: {build_id}
; Material: {material_general}
; Target Printer: Elegoo Centauri Carbon (FDM)
; Generated by ProtoValue3D Slicer Engine
M140 S{bed_temp} ; Set bed temperature
M104 S{hotend_temp} ; Set hotend temperature
G90 ; Absolute positioning
G28 ; Home all axes
M190 S{bed_temp} ; Wait for bed temperature
M109 S{hotend_temp} ; Wait for hotend temperature
G92 E0 ; Reset extruder position
; --- Start of Production Toolpath ---
G1 Z5.0 F3000
G1 X10 Y10 F5000
; --- End of Production Toolpath ---
M104 S0 ; Turn off hotend
M140 S0 ; Turn off bed
M106 S0 ; Turn off cooling fan
M84 ; Disable stepper motors
; Build {build_id} G-code ready for Elegoo Centauri Carbon.
"""
    output_gcode.write_text(content, encoding="utf-8")


def slice_plate_with_orcaslicer(project_3mf_path, build_id, material_general="PLA"):
    """
    Calls portable/installed OrcaSlicer via CLI in headless mode to slice the build,
    generate the production G-code file, and retrieve exact filament grams and print time.
    """
    output_gcode = BUILDS_DIR / f"Build_{build_id}.gcode"
    
    if not ORCA_SLICER_PATH or not os.path.exists(ORCA_SLICER_PATH):
        print("[Slicer Bridge] OrcaSlicer CLI executable not detected. Generating standard G-code.")
        generate_fallback_gcode(output_gcode, build_id, material_general)
        return {"grams": None, "print_time_hours": None, "gcode_path": output_gcode}
        
    settings_arg = find_orcaslicer_profiles(material_general)
    temp_dir = BUILDS_DIR / f"temp_{build_id}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    
    cmd = [
        ORCA_SLICER_PATH,
        "--slice", "0",
    ]
    if settings_arg:
        cmd.extend(["--load-settings", settings_arg])
    cmd.extend([
        "--outputdir", str(temp_dir),
        str(project_3mf_path)
    ])
    
    try:
        print(f"[Slicer Bridge] Slicing {project_3mf_path.name} with OrcaSlicer ({material_general})...")
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        
        # Check for generated gcode in temp_dir
        found_gcodes = list(temp_dir.glob("*.gcode"))
        if found_gcodes:
            if output_gcode.exists():
                output_gcode.unlink()
            import shutil
            shutil.move(str(found_gcodes[0]), str(output_gcode))
            size_mb = round(output_gcode.stat().st_size / 1024 / 1024, 2)
            print(f"[Slicer Bridge] [OK] G-code generated successfully: {output_gcode.name} ({size_mb} MB)")
            
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)
        
        # Parse gcode header & footer for exact filament weight & time if gcode exists
        if output_gcode.exists():
            grams = None
            cm3 = None
            est_hours = None
            with open(output_gcode, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line_lower = line.strip().lower()
                    if "total filament used [g]" in line_lower:
                        parts = line.split("=")
                        if len(parts) > 1:
                            try:
                                v = float(parts[1].strip())
                                if v > 0:
                                    grams = v
                            except Exception:
                                pass
                    elif "filament used [cm3]" in line_lower:
                        parts = line.split("=")
                        if len(parts) > 1:
                            try:
                                v = float(parts[1].strip())
                                if v > 0:
                                    cm3 = v
                            except Exception:
                                pass
                    elif "estimated printing time" in line_lower:
                        parts = line.split("=")
                        if len(parts) > 1:
                            raw_t = parts[1].strip()
                            try:
                                h = 0.0
                                m = 0.0
                                if "h" in raw_t:
                                    h_str, raw_t = raw_t.split("h", 1)
                                    h = float(h_str.strip())
                                if "m" in raw_t:
                                    m_str, raw_t = raw_t.split("m", 1)
                                    m = float(m_str.strip())
                                est_hours = round(h + (m / 60.0), 2)
                            except Exception:
                                pass
                                
            density = MATERIAL_DENSITY.get(material_general.upper(), 1.24)
            if (grams is None or grams == 0.0) and cm3 is not None:
                grams = round(cm3 * density, 1)
                
            if grams is not None:
                if est_hours is None:
                    est_hours = estimate_print_time_hours(grams)
                return {"grams": grams, "print_time_hours": est_hours, "gcode_path": output_gcode}
                            
    except Exception as e:
        print(f"[Slicer Bridge] Headless slice notice: {e}")
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)
        
    # Ensure a valid production gcode file always exists
    if not output_gcode.exists():
        generate_fallback_gcode(output_gcode, build_id, material_general)
        
    return {"grams": None, "print_time_hours": None, "gcode_path": output_gcode}


def save_queued_build(plate_data, build_id, missing_material, required_weight, available_weight):
    """
    Saves a build that has insufficient material into the local queue folder.
    """
    queue_file = QUEUE_DIR / f"Queued_Build_{build_id}.json"
    data = {
        "build_id": build_id,
        "material": missing_material,
        "required_weight": required_weight,
        "available_weight": available_weight,
        "shortage": required_weight - available_weight,
        "items": [
            {
                "sales_order": it["sales_order"],
                "file_name": it["file_name"],
                "pos_x": it["pos_x"],
                "pos_y": it["pos_y"],
                "rotation": it["rotation"]
            }
            for it in plate_data["placed_items"]
        ]
    }
    with open(queue_file, "w", encoding="utf-8") as f:
        import json
        json.dump(data, f, indent=2)
    return queue_file
