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

    UNFINISHED — this segfaults inside pycolmap (exit -11) at the triangulation
    step. The construction below is the right shape and the API calls are the
    3.12 ones, but something about the reconstruction handed to
    triangulate_points is invalid in a way the bindings do not check. Next
    thing to try: write the model to disk with rec.write() and read it back
    before triangulating, which turns a segfault into a readable error; or use
    the CLI (`colmap point_triangulator`) against a text model, which validates
    its input properly.

    Four API differences were found the hard way getting here, all consequences
    of the same 3.12 rigs-and-frames refactor: Image.cam_from_world is
    read-only (a Frame owns the pose), sensor_t takes kwargs, a frame must be
    added before its images, and register_image is gone (a frame with a pose
    is registered). Reading the frame model once would have been quicker than
    four rejections.

    Built as a rig of one sensor per view direction and one frame per
    standpoint, which is how COLMAP 3.12 models this and also the honest shape
    of the thing: a panorama IS a rig of views at one instant. The twelve views
    of a standpoint then share an optical centre exactly, by construction —
    the property three rounds of solving could not be made to hold.
    """
    import pycolmap

    rec = pycolmap.Reconstruction()
    database = pycolmap.Database(str(db))

    # Take the cameras the database already has rather than inventing them.
    # Feature extraction wrote one per view-direction folder, with whatever
    # model it chose; making our own PINHOLE ones collides on import
    # ("kPinhole vs. kSimpleRadial") because triangulation reads them back.
    db_cams = {c.camera_id: c for c in database.read_all_cameras()}
    by_name = {im.name: im for im in database.read_all_images()}
    database.close()
    for cam in db_cams.values():
        rec.add_camera(cam)
    # a view's camera is the one its own image was extracted with
    cam_of = {name: im.camera_id for name, im in by_name.items()}
    sensors = {cid: pycolmap.sensor_t(type=pycolmap.SensorType.CAMERA, id=cid)
               for cid in db_cams}

    # the rig: sensor 0 defines its frame, the rest sit at fixed rotations off
    # it and share its centre (zero translation)
    stem0 = Path(standpoints[0]["image"]).stem
    cam_for_view = {}
    for k in range(len(angles)):
        n = f"view_{k:03d}/{stem0}.jpg"
        if n in cam_of:
            cam_for_view[k] = cam_of[n]

    rig = pycolmap.Rig()
    rig.rig_id = 1
    R0 = view_rotation(*angles[0])
    rig.add_ref_sensor(sensors[cam_for_view[0]])
    for k, (yaw, pitch) in enumerate(angles):
        if k == 0 or k not in cam_for_view:
            continue
        rig.add_sensor(sensors[cam_for_view[k]], pycolmap.Rigid3d(
            pycolmap.Rotation3d(view_rotation(yaw, pitch).T @ R0), np.zeros(3)))
    rec.add_rig(rig)

    placed = 0
    for s_i, sp in enumerate(standpoints):
        stem = Path(sp["image"]).stem
        names = {k: f"view_{k:03d}/{stem}.jpg" for k in range(len(angles))}
        if not any(n in by_name for n in names.values()):
            continue

        # the frame's pose is sensor 0's pose: the rig frame is its camera
        R, t = cam_from_world(sp["xyz"], *angles[0])
        frame = pycolmap.Frame()
        frame.frame_id = s_i + 1
        frame.rig_id = 1
        frame.rig_from_world = pycolmap.Rigid3d(pycolmap.Rotation3d(R), t)

        # The frame has to exist before its images: adding an image validates
        # the frame it names, so the other order fails with "Frame with ID 1
        # does not exist".
        added = []
        for k, name in names.items():
            if name not in by_name or k not in cam_for_view:
                continue
            im = pycolmap.Image(name=name, camera_id=cam_of[name],
                                image_id=by_name[name].image_id)
            im.frame_id = frame.frame_id
            frame.add_data_id(im.data_id)
            added.append(im)
        rec.add_frame(frame)
        for im in added:
            # no register_image in 3.12: a frame with a pose is registered, and
            # its images with it — registration became a property of the frame
            # in the same refactor that moved the pose there
            rec.add_image(im)
            placed += 1

    logger.info("placed %d view(s) as %d rig frame(s) at the recorded poses; "
                "each standpoint's views share a centre exactly",
                placed, len(standpoints))

    sparse_out.mkdir(parents=True, exist_ok=True)
    # poses are given, so this only has to find where the rays meet
    pycolmap.triangulate_points(rec, str(db), str(images), str(sparse_out),
                                refine_intrinsics=False)
    return placed
