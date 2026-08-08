"""Build a COLMAP model from poses we already know, instead of inferring them.

A simulated capture knows exactly where its camera stood. Structure from motion
exists to recover that from images alone, which a real 360 capture needs and a
simulated one does not — and an empty corridor of flat planes is close to the
worst case for it, because views of a plane leave camera position and depth
trading off against each other however well textured it is.

So when `poses.json` sits beside the panoramas, the poses go straight in and
only the points are triangulated. The splat is then born in the building's
coordinate frame: no alignment, no metric scale to infer from walked distance,
no direction to guess.

The one thing to get right is the frame. `capture.py` writes an equirectangular
image in the gz convention (+X forward, +Y left, +Z up), and
`equirect_to_pinhole` reads one in its own (Y as the pole axis). Both are
standard layouts — top of image is up — but they disagree about which world
axis that is, so a pose has to be carried across. Equating the two mappings
pixel for pixel gives a fixed rotation, derived in `GZ_TO_PANO` below.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

# capture.py:      d_gz = [cos(lat)cos(lon), cos(lat)sin(lon), sin(lat)]
#                  with lat = +pi/2 at the top of the image
# equirect_to_pinhole: lon = atan2(d.x, d.z), lat = asin(d.y)
#                  with lat = -pi/2 at the top of the image
#
# Same pixel, so lon matches and lat negates, giving
#     d_pano.x =  d_gz.y      d_pano.y = -d_gz.z      d_pano.z =  d_gz.x
GZ_TO_PANO = np.array([[0.0, 1.0, 0.0],
                       [0.0, 0.0, -1.0],
                       [1.0, 0.0, 0.0]])


def load(panos: Path) -> list[dict] | None:
    """The standpoints a simulated capture recorded, or None for a real one."""
    f = Path(panos) / "poses.json"
    if not f.is_file():
        return None
    try:
        doc = json.loads(f.read_text())
    except (OSError, ValueError):
        return None
    pts = doc.get("standpoints") or []
    return pts or None


def view_rotation(yaw: float, pitch: float) -> np.ndarray:
    """Camera rays -> panorama frame, exactly as equirect_to_pinhole builds it."""
    cy, sy = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    return (np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
            @ np.array([[1, 0, 0], [0, cp, -sp], [0, sp, cp]]))


def cam_from_world(centre_gz, yaw: float, pitch: float):
    """(R, t) placing one reprojected view, in building coordinates.

    A world point goes into the panorama's frame by GZ_TO_PANO after moving the
    origin to the standpoint, then into the camera by the inverse of that
    view's rotation.
    """
    R = view_rotation(yaw, pitch).T @ GZ_TO_PANO
    return R, -R @ np.asarray(centre_gz, dtype=np.float64)


def build(scene: Path, db, images: Path, standpoints, angles, size: int,
          fov: float, sparse_out: Path, logger) -> int:
    """Write a reconstruction at the known poses and triangulate points into it."""
    import pycolmap

    f = 0.5 * size / math.tan(math.radians(fov) * 0.5)
    rec = pycolmap.Reconstruction()
    cam = pycolmap.Camera.create(1, "PINHOLE", f, size, size)
    cam.camera_id = 1
    rec.add_camera(cam)

    database = pycolmap.Database(str(db))
    by_name = {im.name: im.image_id for im in database.read_all_images()}
    database.close()

    placed = 0
    for s_i, sp in enumerate(standpoints):
        centre = sp["xyz"]
        stem = Path(sp["image"]).stem
        for k, (yaw, pitch) in enumerate(angles):
            name = f"view_{k:03d}/{stem}.jpg"
            if name not in by_name:
                continue
            R, t = cam_from_world(centre, yaw, pitch)
            im = pycolmap.Image(
                name=name, camera_id=1, image_id=by_name[name],
                cam_from_world=pycolmap.Rigid3d(pycolmap.Rotation3d(R), t))
            rec.add_image(im)
            rec.register_image(im.image_id)
            placed += 1
    logger.info("placed %d view(s) at the poses the capture recorded, "
                "over %d standpoint(s)", placed, len(standpoints))

    sparse_out.mkdir(parents=True, exist_ok=True)
    # poses are fixed, so this only has to find where the rays meet
    pycolmap.triangulate_points(rec, str(db), str(images), str(sparse_out),
                                refine_intrinsics=False)
    return placed
