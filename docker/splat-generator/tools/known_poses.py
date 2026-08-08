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
    """Write a reconstruction at the recorded poses, then triangulate into it.

    No rig here, deliberately. A rig exists to hold views together while their
    poses are being solved for — it is what stopped SfM sliding the views of
    one panorama apart. These poses are not being solved for; they are being
    written. Twelve views end up at one centre because that is the centre each
    is given, so a rig would constrain nothing and only add structure to get
    wrong.

    COLMAP 3.12 keeps a pose on a Frame rather than an Image, so each view gets
    its own single-sensor frame. That is the smallest construction the model
    allows.

    STILL FAILING, one layer further in: adding a camera appears to create a rig
    implicitly, so adding one explicitly collides —
    "existing_rig.RefSensorId() == rig.RefSensorId()". Next: do not add rigs at
    all, read back whatever rec.add_camera made (rec.rigs) and point the frames
    at it. Worth doing against a scratch reconstruction in a REPL rather than
    through a 4-minute pipeline run — every layer of this has cost a rebuild
    and a submit to learn one fact.
    """
    import pycolmap

    rec = pycolmap.Reconstruction()
    database = pycolmap.Database(str(db))
    # take the camera the database already has; inventing one collides on the
    # model ("kPinhole vs. kSimpleRadial") when triangulation reads it back
    cams = database.read_all_cameras()
    db_images = {im.name: im for im in database.read_all_images()}
    database.close()
    for cam in cams:
        rec.add_camera(cam)

    # one rig per camera, holding just that camera: frames need a rig to belong
    # to, and this is the degenerate one that adds no constraint
    rigs = {}
    for cam in cams:
        rig = pycolmap.Rig()
        rig.rig_id = cam.camera_id
        rig.add_ref_sensor(pycolmap.sensor_t(type=pycolmap.SensorType.CAMERA,
                                             id=cam.camera_id))
        rec.add_rig(rig)
        rigs[cam.camera_id] = rig.rig_id

    placed = 0
    frame_id = 0
    for sp in standpoints:
        stem = Path(sp["image"]).stem
        for k, (yaw, pitch) in enumerate(angles):
            name = f"view_{k:03d}/{stem}.jpg"
            db_im = db_images.get(name)
            if db_im is None:
                continue
            R, t = cam_from_world(sp["xyz"], yaw, pitch)
            frame_id += 1
            frame = pycolmap.Frame()
            frame.frame_id = frame_id
            frame.rig_id = rigs[db_im.camera_id]
            frame.rig_from_world = pycolmap.Rigid3d(pycolmap.Rotation3d(R), t)

            im = pycolmap.Image(name=name, camera_id=db_im.camera_id,
                                image_id=db_im.image_id)
            im.frame_id = frame_id
            frame.add_data_id(im.data_id)
            # the frame first: adding an image validates the frame it names
            rec.add_frame(frame)
            rec.add_image(im)
            placed += 1

    logger.info("placed %d view(s) at the poses the capture recorded, "
                "across %d standpoint(s)", placed, len(standpoints))

    sparse_out.mkdir(parents=True, exist_ok=True)
    # poses are given; this only has to find where the rays meet
    pycolmap.triangulate_points(rec, str(db), str(images), str(sparse_out),
                                refine_intrinsics=False)
    return placed
