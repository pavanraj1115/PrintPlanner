import numpy as np
import trimesh
from shapely.geometry import Polygon, MultiPolygon
from shapely.ops import unary_union
from config import MAX_PART_DIMENSION, PART_SPACING

def check_oriented_bounding_box(mesh_or_path):
    """
    Computes the Oriented Bounding Box (OBB) in local coordinates (not global AABB).
    Returns (is_valid, dimensions, obb_transform, reason).
    Enforces that length, width, and height are all <= MAX_PART_DIMENSION (250mm).
    """
    if isinstance(mesh_or_path, (str, bytes)):
        mesh = trimesh.load(mesh_or_path, force='mesh')
    else:
        mesh = mesh_or_path

    # Compute minimal oriented bounding box using convex hull for speed and mathematical accuracy
    hull = mesh.convex_hull
    obb_transform, extents = trimesh.bounds.oriented_bounds(hull)
    
    # Sort extents from largest to smallest: Length >= Width >= Height
    sorted_extents = np.sort(extents)[::-1]
    length, width, height = float(sorted_extents[0]), float(sorted_extents[1]), float(sorted_extents[2])
    
    is_valid = True
    reason = "OK"
    
    if length > MAX_PART_DIMENSION:
        is_valid = False
        reason = f"Part length ({length:.1f}mm) exceeds max build dimension ({MAX_PART_DIMENSION}mm)"
    elif width > MAX_PART_DIMENSION:
        is_valid = False
        reason = f"Part width ({width:.1f}mm) exceeds max build dimension ({MAX_PART_DIMENSION}mm)"
    elif height > MAX_PART_DIMENSION:
        is_valid = False
        reason = f"Part height ({height:.1f}mm) exceeds max build dimension ({MAX_PART_DIMENSION}mm)"
        
    return {
        "is_valid": is_valid,
        "length": length,
        "width": width,
        "height": height,
        "extents": (length, width, height),
        "obb_transform": obb_transform,
        "reason": reason
    }


def auto_orient_for_minimum_support(mesh, max_candidates=12):
    """
    Finds the optimal orientation that minimizes required support material
    and provides a stable bed adhesion footprint.
    Tests candidate planar faces from the convex hull placed on Z=0.
    """
    hull = mesh.convex_hull
    face_areas = hull.area_faces
    face_normals = hull.face_normals
    
    # Sort candidate faces by area (largest faces make the most stable bed bases)
    sorted_face_idx = np.argsort(face_areas)[::-1]
    candidate_indices = sorted_face_idx[:min(len(sorted_face_idx), max_candidates)]
    
    best_score = float('inf')
    best_transform = np.eye(4)
    best_mesh = mesh
    
    down_vector = np.array([0.0, 0.0, -1.0])
    cos_45 = np.cos(np.radians(45.0))
    
    for idx in candidate_indices:
        target_normal = face_normals[idx]
        
        # Calculate rotation matrix aligning target_normal with -Z (pointing down at bed)
        R = trimesh.geometry.align_vectors(target_normal, down_vector)
        candidate_mesh = mesh.copy()
        candidate_mesh.apply_transform(R)
        
        # Translate so bottom sits exactly at Z = 0
        min_z = candidate_mesh.bounds[0][2]
        T = np.eye(4)
        T[2, 3] = -min_z
        candidate_mesh.apply_transform(T)
        full_transform = T @ R
        
        # Evaluate support area: faces with downward normal > 45 deg relative to Z-axis
        normals = candidate_mesh.face_normals
        areas = candidate_mesh.area_faces
        downward_projection = -normals[:, 2]  # component pointing along -Z
        
        # Overhang condition: pointing downwards steeper than 45 degrees
        is_overhang = (downward_projection > cos_45) & (candidate_mesh.triangles[:, :, 2].min(axis=1) > 0.5)
        overhang_area = np.sum(areas[is_overhang])
        
        # Bed contact condition: faces touching Z near 0
        is_bed_contact = (candidate_mesh.triangles[:, :, 2].max(axis=1) < 0.8) & (downward_projection > 0.95)
        bed_contact_area = np.sum(areas[is_bed_contact])
        
        part_height = candidate_mesh.bounds[1][2]
        
        # Score metric: low overhang is primary, good bed contact is secondary, lower height preferred
        score = overhang_area - (0.15 * bed_contact_area) + (0.05 * part_height)
        
        if score < best_score:
            best_score = score
            best_transform = full_transform
            best_mesh = candidate_mesh
            
    return best_mesh, best_transform


def get_part_2d_footprint(mesh, spacing=PART_SPACING):
    """
    Projects 3D mesh to 2D XY plane, creates convex 2D hull, and buffers by spacing/2.
    Returns shapely polygon with clearance buffer and its bounding box dimensions.
    """
    from shapely.geometry import MultiPoint
    # 2D projection on XY
    vertices_2d = mesh.vertices[:, :2]
    # Use Shapely 2D convex hull
    hull_2d = MultiPoint(vertices_2d).convex_hull
    
    # Expand by clearance margin (half on each part = full spacing between parts)
    buffered_polygon = hull_2d.buffer(spacing / 2.0, resolution=16)
    
    minx, miny, maxx, maxy = buffered_polygon.bounds
    width = maxx - minx
    height = maxy - miny
    
    return {
        "polygon": buffered_polygon,
        "width": width,
        "height": height,
        "bounds": (minx, miny, maxx, maxy)
    }
