import numpy as np
from shapely.geometry import Polygon, box
from shapely.affinity import translate, rotate
from config import BED_SIZE_X, BED_SIZE_Y, PART_SPACING

class BuildPlate:
    def __init__(self, plate_id, bed_x=BED_SIZE_X, bed_y=BED_SIZE_Y, spacing=PART_SPACING, printer=None, material=None):
        self.plate_id = plate_id
        self.bed_x = bed_x
        self.bed_y = bed_y
        self.spacing = spacing
        self.printer = printer
        self.material = material
        
        # Printable boundary: Part footprint polygon already includes spacing/2 buffer.
        # Leaving margin=4.0mm guarantees physical mesh is at least 4.0mm from bed borders.
        margin = max(spacing / 2.0, 4.0)
        self.min_x = margin
        self.min_y = margin
        self.max_x = bed_x - margin
        self.max_y = bed_y - margin
        self.boundary = box(self.min_x, self.min_y, self.max_x, self.max_y)
        
        self.placed_items = []  # list of dicts: {part_id, sales_order, mesh, polygon, pos_x, pos_y, rotation}
        self.placed_polygons = []
        self.ideal_start_time = None
        self.ideal_finish_time = None
        self.est_hours = 0.0
        self.preferred_spool = None
        self.assigned_spool = None
        self.spool_weight = 0.0
        self.final_weight = 0.0
        self.rem_after_slice = 0.0
        self.build_doc_name = None

    def can_fit(self, candidate_polygon):
        # Check if polygon is strictly inside the bed boundary
        if not self.boundary.contains(candidate_polygon):
            return False
            
        # Fast AABB pre-check before expensive polygon intersection
        cb = candidate_polygon.bounds
        for placed_poly in self.placed_polygons:
            pb = placed_poly.bounds
            if cb[0] >= pb[2] or cb[2] <= pb[0] or cb[1] >= pb[3] or cb[3] <= pb[1]:
                continue
            if candidate_polygon.intersects(placed_poly):
                intersection_area = candidate_polygon.intersection(placed_poly).area
                if intersection_area > 1e-4:
                    return False
        return True

    def place_item(self, item_info, base_polygon):
        """
        Places a part onto this plate using an anchor-based dense Bottom-Left heuristic
        evaluating 0, 90, 180, and 270 deg rotations with visual orientation consistency.
        Returns True if placed, False if plate cannot fit this part.
        """
        minx, miny, maxx, maxy = base_polygon.bounds
        orig_center_x = (minx + maxx) / 2.0
        orig_center_y = (miny + maxy) / 2.0
        
        # Centered at origin for clean rotation and translation
        zeroed_poly = translate(base_polygon, -orig_center_x, -orig_center_y)
        
        # Determine dominant orientation among already placed items to maintain orderly rows
        prev_rotations = [it.get("rotation", 0.0) for it in self.placed_items]
        dominant_rot = max(set(prev_rotations), key=prev_rotations.count) if prev_rotations else None
        
        best_candidate = None
        best_score = float('inf')
        best_pos = None
        best_rot = None
        
        # 1. Candidate anchor points
        anchors = [(self.min_x, self.min_y)]
        for pb_poly in self.placed_polygons:
            pb = pb_poly.bounds
            anchors.append((pb[2], pb[1]))         # Right adjacent (next in row)
            anchors.append((pb[0], pb[3]))         # Directly above (next row)
            anchors.append((self.min_x, pb[3]))    # Start of new row at left margin
            anchors.append((pb[2], pb[3]))         # Top-right corner
            anchors.append((pb[2], self.min_y))    # Start of new column at bottom margin
            
        rotations = [0.0, 90.0, 180.0, 270.0]
        
        for rot in rotations:
            rot_poly = rotate(zeroed_poly, rot, origin=(0, 0)) if rot != 0.0 else zeroed_poly
            rminx, rminy, rmaxx, rmaxy = rot_poly.bounds
            half_w = (rmaxx - rminx) / 2.0
            half_h = (rmaxy - rminy) / 2.0
            
            x_min_pos = self.min_x + half_w
            x_max_pos = self.max_x - half_w
            y_min_pos = self.min_y + half_h
            y_max_pos = self.max_y - half_h
            
            if x_min_pos > x_max_pos or y_min_pos > y_max_pos:
                continue
                
            for ax, ay in anchors:
                cx = ax + half_w
                cy = ay + half_h
                
                if cx < x_min_pos or cx > x_max_pos or cy < y_min_pos or cy > y_max_pos:
                    continue
                    
                cand = translate(rot_poly, cx, cy)
                if self.can_fit(cand):
                    # Bottom-Left scoring: lowest Y takes priority (fill rows bottom to top),
                    # then lowest X (fill left to right).
                    rot_penalty = 0.0
                    if dominant_rot is not None and rot != dominant_rot:
                        rot_penalty = 15.0  # Mild preference to keep identical parts facing same direction
                    score = (cy + half_h) * 1000.0 + (cx + half_w) + rot_penalty
                    if score < best_score:
                        best_score = score
                        best_candidate = cand
                        best_pos = (float(cx), float(cy))
                        best_rot = float(rot)
                        
        # 2. Secondary fine-grid scan fallback (step = 3.0mm) to fill any irregular gaps
        if best_candidate is None:
            step_grid = 3.0
            for rot in rotations:
                rot_poly = rotate(zeroed_poly, rot, origin=(0, 0)) if rot != 0.0 else zeroed_poly
                rminx, rminy, rmaxx, rmaxy = rot_poly.bounds
                half_w = (rmaxx - rminx) / 2.0
                half_h = (rmaxy - rminy) / 2.0
                
                x_min_pos = self.min_x + half_w
                x_max_pos = self.max_x - half_w
                y_min_pos = self.min_y + half_h
                y_max_pos = self.max_y - half_h
                
                if x_min_pos > x_max_pos or y_min_pos > y_max_pos:
                    continue
                    
                x_steps = np.arange(x_min_pos, x_max_pos + 0.1, step_grid)
                y_steps = np.arange(y_min_pos, y_max_pos + 0.1, step_grid)
                
                for cy in y_steps:
                    for cx in x_steps:
                        cand = translate(rot_poly, cx, cy)
                        if self.can_fit(cand):
                            rot_penalty = 15.0 if (dominant_rot is not None and rot != dominant_rot) else 0.0
                            score = (cy + half_h) * 1000.0 + (cx + half_w) + rot_penalty
                            if score < best_score:
                                best_score = score
                                best_candidate = cand
                                best_pos = (float(cx), float(cy))
                                best_rot = float(rot)
                                break
                                
        if best_candidate is not None:
            self.placed_polygons.append(best_candidate)
            placed_data = {
                **item_info,
                "pos_x": best_pos[0],
                "pos_y": best_pos[1],
                "rotation": best_rot,
                "placed_poly": best_candidate
            }
            self.placed_items.append(placed_data)
            return True
            
        return False

    def center_layout(self):
        """
        Centers all placed items symmetrically on the build plate with equal margins around all edges.
        """
        if not self.placed_items:
            return
            
        all_bounds = [it["placed_poly"].bounds for it in self.placed_items]
        min_x = min(b[0] for b in all_bounds)
        max_x = max(b[2] for b in all_bounds)
        min_y = min(b[1] for b in all_bounds)
        max_y = max(b[3] for b in all_bounds)
        
        offset_x = (self.bed_x - (max_x + min_x)) / 2.0
        offset_y = (self.bed_y - (max_y + min_y)) / 2.0
        
        # Verify that shifting keeps all items within printable bounds
        can_shift = True
        for b in all_bounds:
            if (b[0] + offset_x < self.min_x or b[2] + offset_x > self.max_x or
                b[1] + offset_y < self.min_y or b[3] + offset_y > self.max_y):
                can_shift = False
                break
                
        if can_shift and (abs(offset_x) > 0.1 or abs(offset_y) > 0.1):
            new_polygons = []
            for it in self.placed_items:
                it["pos_x"] += offset_x
                it["pos_y"] += offset_y
                it["placed_poly"] = translate(it["placed_poly"], offset_x, offset_y)
                new_polygons.append(it["placed_poly"])
            self.placed_polygons = new_polygons


class BuildNester:
    def __init__(self, max_plates=7, bed_x=BED_SIZE_X, bed_y=BED_SIZE_Y, spacing=PART_SPACING):
        self.max_plates = max_plates
        self.bed_x = bed_x
        self.bed_y = bed_y
        self.spacing = spacing
        self.plates = []

    def get_or_create_plate(self, printer=None, material=None):
        if not self.plates or len(self.plates) < self.max_plates:
            plate_num = len(self.plates) + 1
            new_plate = BuildPlate(plate_num, self.bed_x, self.bed_y, self.spacing, printer=printer, material=material)
            self.plates.append(new_plate)
            return new_plate
        return None

    def pack_parts(self, part_items):
        """
        Packs a list of part items onto the plates and centers them.
        Returns (packed_items, remaining_unpacked_items)
        """
        unpacked = []
        
        for item in part_items:
            placed = False
            base_poly = item["polygon_footprint"]
            
            # Try placing on existing plates first (packing tight)
            for plate in self.plates:
                if plate.place_item(item, base_poly):
                    placed = True
                    break
                    
            # If not placed and we haven't reached max plates, open a new plate
            if not placed:
                new_plate = self.get_or_create_plate()
                if new_plate is not None:
                    if new_plate.place_item(item, base_poly):
                        placed = True
                        
            if not placed:
                unpacked.append(item)
                
        # Symmetrically center layout on each plate
        for plate in self.plates:
            plate.center_layout()
            
        return self.plates, unpacked
