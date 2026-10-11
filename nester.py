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
        
        # Printable boundary (leaving margin from bed edges)
        margin = spacing
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
        Attempts to place a part onto this plate using bottom-left heuristic with 0, 90, 180, 270 deg rotation permutations.
        Returns True if placed, False if plate cannot fit this part.
        """
        minx, miny, maxx, maxy = base_polygon.bounds
        orig_center_x = (minx + maxx) / 2.0
        orig_center_y = (miny + maxy) / 2.0
        
        # Centered at origin for clean rotation and translation
        zeroed_poly = translate(base_polygon, -orig_center_x, -orig_center_y)
        
        step_grid = 8.0  # mm search step resolution
        
        # Test 4 rotation permutations (0, 90, 180, 270 degrees)
        for rot in [0.0, 90.0, 180.0, 270.0]:
            rot_poly = rotate(zeroed_poly, rot, origin=(0, 0)) if rot != 0.0 else zeroed_poly
            rminx, rminy, rmaxx, rmaxy = rot_poly.bounds
            half_w = (rmaxx - rminx) / 2.0
            half_h = (rmaxy - rminy) / 2.0
            
            x_min_pos = self.min_x + half_w
            x_max_pos = self.max_x - half_w
            y_min_pos = self.min_y + half_h
            y_max_pos = self.max_y - half_h
            
            if x_min_pos > x_max_pos or y_min_pos > y_max_pos:
                continue  # Rotated footprint exceeds bed boundaries in this orientation
                
            x_steps = np.arange(x_min_pos, x_max_pos + 0.1, step_grid)
            y_steps = np.arange(y_min_pos, y_max_pos + 0.1, step_grid)
            
            for cy in y_steps:
                for cx in x_steps:
                    candidate = translate(rot_poly, cx, cy)
                    if self.can_fit(candidate):
                        # Center placed mesh
                        self.placed_polygons.append(candidate)
                        placed_data = {
                            **item_info,
                            "pos_x": float(cx),
                            "pos_y": float(cy),
                            "rotation": float(rot),
                            "placed_poly": candidate
                        }
                        self.placed_items.append(placed_data)
                        return True
                        
        return False


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
        Packs a list of part items onto the plates.
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
                
        return self.plates, unpacked
