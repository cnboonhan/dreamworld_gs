"""Prefect flow: a folder of 360 panoramas -> one fused 3DGS world.

Reconstruction rather than generation: with several panoramas shot from
different standpoints in the same space, the extra viewpoints carry measured
parallax, so geometry is recovered instead of imagined. Quality tracks
capture coverage — more standpoints beat a bigger model.

    assets/panos/<scene>/*.jpg   several panoramas of one space
        -> reproject   each panorama into pinhole views
        -> sfm         COLMAP poses across all of them
        -> train       gaussian splatting against the posed views
        -> export      world.ply + world.usdz + world.cam.json

Two ways to run it:

    python reconstruct.py serve
    python reconstruct.py run <scene-dir> <panos-dir> [--spacing M]
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
from prefect import flow, get_run_logger, task

TOOLS = Path("/opt/tools")
DEPLOYMENT = "dreamworld"
PANO_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
SH_C0 = 0.28209479177387814
# How much longer a gaussian's longest axis may be than its *middle* one
# before the loss pushes back. Measuring against the middle axis rather than
# the shortest is the whole point: a splat lying on a floor or wall is a flat
# disc (two long axes, one flat) and is correct, while a needle is long in
# one axis only. Penalising longest/shortest hits the discs and inflates them
# into a haze over the floor.
NEEDLE_MAX = 15.0
# Nothing may be longer than this, whatever its shape. A corridor here is about
# 1.5 m wide, so a 7 m gaussian is not a wall being represented efficiently, it
# is a sheet through the building — and edge-on it draws a line across the
# frame.
SPLAT_MAX_M = 1.0
# The chain from panorama to splat, fixed because every link depends on the
# others. A 100-degree view at 1024 px is 10.2 pixels per degree, which is what
# a 7680-wide equirect downsamples cleanly into — the same ratio the real 360
# captures go through. Eight yaws plus four tilts overlap enough that
# neighbouring standpoints share surface.
VIEWS = 8
VIEW_PX = 1024
VIEW_FOV = 100.0
ITERS = 15000
# How hard the loss pushes back on depth disagreeing with the range maps.
DEPTH_WEIGHT = 1.0


GPU_LOCKS = Path("/tmp/dw-gpu")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                      # someone else's, so still held
    return True


def _claim(f: Path) -> bool:
    """Create this lock exclusively, taking it over if its holder is gone."""
    for attempt in (1, 2):
        try:
            fd = os.open(f, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if attempt == 2:
                return False
            try:
                pid = int(f.read_text().strip() or 0)
            except (OSError, ValueError):
                pid = 0
            if pid and _alive(pid):
                return False
            f.unlink(missing_ok=True)    # holder died; try once more
            continue
        except OSError:
            return False
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    return False


def claim_gpu(logger=None):
    """Take one visible CUDA device exclusively. Returns (device, release).

    Picking the emptiest card by free memory looked simpler and was wrong twice
    over. `torch.cuda.mem_get_info(i)` creates a context on every device it
    asks about, so each concurrent run spun one up on all of them; and two runs
    starting together still read the same card as free and landed on it. Under
    that, two of twenty-six reconstructions died with SIGSEGV during init and
    then rebuilt cleanly on their own.

    A lock file per device is exact instead: whoever creates it owns that card,
    no device is touched by a run that does not own it, and the pid inside lets
    the next run reclaim a slot whose holder died rather than leaking it.
    """
    import torch

    if not torch.cuda.is_available():
        return "cpu", lambda: None
    GPU_LOCKS.mkdir(parents=True, exist_ok=True)
    n = torch.cuda.device_count()        # unlike mem_get_info, creates nothing
    for i in range(n):
        f = GPU_LOCKS / f"{i}.lock"
        if _claim(f):
            if logger:
                logger.info("cuda:%d of %d visible", i, n)
            return f"cuda:{i}", lambda f=f: f.unlink(missing_ok=True)
    # Should not happen: concurrency is capped at one run per device. Share
    # rather than fail, and say so, because a stuck lock would look like this.
    if logger:
        logger.warning("all %d GPU(s) claimed — sharing cuda:0. If nothing "
                       "else is running, clear %s", n, GPU_LOCKS)
    return "cuda:0", lambda: None


# ---------------------------------------------------------------- reprojection

def equirect_to_pinhole(pano: np.ndarray, yaw: float, pitch: float,
                        fov_deg: float, size: int) -> np.ndarray:
    """Sample an equirectangular image into a pinhole view (cv2.remap)."""
    import cv2

    f = 0.5 * size / math.tan(math.radians(fov_deg) * 0.5)
    j, i = np.meshgrid(np.arange(size), np.arange(size), indexing="xy")
    x = (j - size * 0.5) / f
    y = (i - size * 0.5) / f
    dirs = np.stack([x, y, np.ones_like(x)], -1)
    dirs /= np.linalg.norm(dirs, axis=-1, keepdims=True)

    cy, sy = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    R = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]]) @ \
        np.array([[1, 0, 0], [0, cp, -sp], [0, sp, cp]])
    d = dirs @ R.T

    lon = np.arctan2(d[..., 0], d[..., 2])
    lat = np.arcsin(np.clip(d[..., 1], -1, 1))
    H, W = pano.shape[:2]
    map_x = ((lon / (2 * math.pi) + 0.5) * W).astype(np.float32)
    map_y = ((lat / math.pi + 0.5) * H).astype(np.float32)
    return cv2.remap(pano, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)


@task(name="1. reproject")
def reproject(scene: str, panos_dir: str) -> dict:
    """Each panorama -> `views` overlapping pinhole images.

    Pinhole views are what SfM and 3DGS expect. A wide field of view on a
    ring of yaws (plus tilts) keeps neighbouring standpoints sharing features:
    too narrow and the reconstruction fragments where the overlap runs out.
    """
    import cv2
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    logger = get_run_logger()
    srcs = sorted(p for p in Path(panos_dir).iterdir()
                  if p.suffix.lower() in PANO_SUFFIXES)
    if not srcs:
        raise FileNotFoundError(f"no panoramas in {panos_dir}")

    out = Path(scene) / "images"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)

    # a ring of yaws at eye level, plus one tilted up and one down: enough
    # overlap to match, without flooding SfM with near-duplicate views
    ring = [(i * 2 * math.pi / VIEWS, 0.0) for i in range(VIEWS)]
    # half as many tilted views as ring views: exhaustive matching is
    # quadratic in image count, and the tilted ones (ceiling, floor) are the
    # least likely to register anyway
    tilts = [(i * 4 * math.pi / VIEWS + math.pi / VIEWS,
              math.radians(35 if i % 2 else -35))
             for i in range(VIEWS // 2)]
    angles = ring + tilts

    n = 0
    for src in srcs:
        pano = cv2.cvtColor(np.array(Image.open(src).convert("RGB")), cv2.COLOR_RGB2BGR)
        h, w = pano.shape[:2]
        if abs(w / h - 2) > 0.02:
            logger.warning("%s is %dx%d (not 2:1) — reprojection assumes "
                           "equirectangular", src.name, w, h)
        for k, (yaw, pitch) in enumerate(angles):
            view = equirect_to_pinhole(pano, yaw, pitch, VIEW_FOV, VIEW_PX)
            # One directory per view direction, the panorama's name inside it.
            # COLMAP names a rig's sensors by image *prefix* and its frames by
            # the remainder, so the direction has to lead: view_007/000.jpg
            # reads "sensor 7, standpoint 000".
            vdir = out / f"view_{k:03d}"
            vdir.mkdir(exist_ok=True)
            cv2.imwrite(str(vdir / f"{src.stem}.jpg"), view,
                        [cv2.IMWRITE_JPEG_QUALITY, 95])
            n += 1
        logger.info("%s -> %d views", src.name, len(angles))

    logger.info("%d panoramas -> %d pinhole views over %d direction(s)",
                len(srcs), n, len(angles))
    # the angles ARE the rig: each view's fixed rotation off the panorama's own
    # frame, which is what holds a standpoint together in the solve
    return {"panos": len(srcs), "views": n,
            "angles": [[float(y), float(p)] for y, p in angles]}


# ------------------------------------------------------------------------ sfm

def standpoints(rec) -> list[np.ndarray]:
    """One camera centre per panorama, in capture order.

    Views are named <panorama>_<k>, so all views from one 360 shot share a
    physical position; the median of the group is robust to a stray view.
    """
    groups: dict[str, list[np.ndarray]] = {}
    for _, im in rec.images.items():
        cfw = im.cam_from_world() if callable(im.cam_from_world) else im.cam_from_world
        m = np.asarray(cfw.matrix(), dtype=np.float64)
        key = im.name.split("/")[-1] if "/" in im.name else im.name.rsplit("_", 1)[0]
        groups.setdefault(key, []).append(-m[:3, :3].T @ m[:3, 3])
    return [np.median(np.stack(v), 0) for _, v in sorted(groups.items())]


def standpoint_map(rec) -> dict:
    """panorama name -> its camera centre."""
    groups: dict[str, list[np.ndarray]] = {}
    for _, im in rec.images.items():
        cfw = im.cam_from_world() if callable(im.cam_from_world) else im.cam_from_world
        m = np.asarray(cfw.matrix(), dtype=np.float64)
        key = im.name.split("/")[-1] if "/" in im.name else im.name.rsplit("_", 1)[0]
        groups.setdefault(key, []).append(-m[:3, :3].T @ m[:3, 3])
    return {k: np.median(np.stack(v), 0) for k, v in groups.items()}


def pick_model(sparse: Path, logger):
    """Largest reconstruction, and a warning when the capture fragmented.

    A walk whose standpoints stop overlapping — through a doorway, past a
    blank wall — breaks the feature chain and COLMAP starts a second model.
    Only the largest is usable: fragments never share an image, so aligning
    them relies on the few panoramas that appear in both, and measured on
    this capture that alignment was off by 15% of a standpoint step. Merging
    on those terms doubles geometry rather than extending it. What helps is
    knowing it happened, and where.
    """
    import pycolmap

    models = []
    for d in sorted(sparse.iterdir()):
        if d.is_dir():
            try:
                models.append((d, pycolmap.Reconstruction(d)))
            except Exception as e:                       # noqa: BLE001
                # say why: a model that will not load is a bug to fix, not a
                # fragment to skip past quietly
                logger.warning("could not read %s: %s", d.name, e)
    if not models:
        return None, None, {}
    models.sort(key=lambda kv: -kv[1].num_reg_images())
    best_dir, best = models[0]

    info = {"models": len(models)}
    if len(models) > 1:
        unused = sum(r.num_reg_images() for _, r in models[1:])
        info["unused_views"] = unused
        logger.warning("SfM split into %d models (%s views); using the "
                       "largest, so %d registered views are unused",
                       len(models), ", ".join(str(r.num_reg_images())
                                              for _, r in models), unused)
        for d, r in models[1:]:
            panos = sorted(standpoint_map(r))
            if panos:
                logger.warning("  model %s covers %d panoramas, %s .. %s",
                               d.name, len(panos), panos[0][-13:], panos[-1][-13:])
        logger.warning("  the break is between those ranges: shoot extra "
                       "standpoints there so the chain reconnects")
    return best_dir, best, info


def rescale_to_metric(model: Path, spacing: float, logger) -> float:
    """Put the reconstruction in metres, using the known standpoint spacing.

    SfM recovers geometry only up to scale. If the panoramas were shot a known
    distance apart, the recovered distance between consecutive standpoints is
    a ruler: the median ratio converts the whole model to metres, which is
    what Isaac Sim (and any robot policy) needs.
    """
    import pycolmap

    rec = pycolmap.Reconstruction(model)
    centres = standpoints(rec)
    if len(centres) < 2:
        logger.warning("only %d standpoint(s); cannot infer scale", len(centres))
        return 1.0

    steps = np.linalg.norm(np.diff(np.stack(centres), axis=0), axis=1)
    median_step = float(np.median(steps))
    if median_step <= 0:
        logger.warning("standpoints coincide; cannot infer scale")
        return 1.0

    scale = spacing / median_step
    spread = float(np.max(steps) / np.min(steps)) if np.min(steps) > 0 else float("inf")
    if spread > 3:
        logger.warning("standpoint spacing varies %.1fx — the median is a poor "
                       "ruler if the walk was not evenly spaced", spread)
    rec.transform(pycolmap.Sim3d(scale, pycolmap.Rotation3d(), np.zeros(3)))
    rec.write(str(model))
    logger.info("metric scale: %.4f (median step %.4f -> %.2f m)",
                scale, median_step, spacing)
    return scale


def rig_config(angles, pycolmap):
    """Declare the reprojected views of one panorama a rigid camera rig.

    They share an optical centre exactly — they are cut from a single image —
    and their relative rotations are the yaw/pitch we reprojected at. Saying so
    replaces 6 unknowns per view with 6 per standpoint: a 5-panorama walk goes
    from 360 pose parameters to 30, and the shared centre stops being something
    the solver might discover and becomes something it cannot violate.

    `equirect_to_pinhole` builds R mapping camera directions into the panorama's
    frame, so the rig-to-camera rotation is its transpose. Translation is zero.
    """
    import numpy as np

    def rot(yaw, pitch):
        """Camera directions -> panorama frame, as equirect_to_pinhole builds it."""
        cy, sy = math.cos(yaw), math.sin(yaw)
        cp, sp = math.cos(pitch), math.sin(pitch)
        return (np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
                @ np.array([[1, 0, 0], [0, cp, -sp], [0, sp, cp]]))

    # sensor 0 defines the rig's frame, so every other sensor's pose is
    # measured against it: cam_k_from_rig = cam_k_from_pano @ pano_from_cam_0
    R0 = rot(*angles[0])
    cams = []
    for k, (yaw, pitch) in enumerate(angles):
        cam = pycolmap.RigConfigCamera(image_prefix=f"view_{k:03d}/")
        if k == 0:
            cam.ref_sensor = True
        else:
            cam.cam_from_rig = pycolmap.Rigid3d(
                pycolmap.Rotation3d(rot(yaw, pitch).T @ R0), np.zeros(3))
        cams.append(cam)
    return pycolmap.RigConfig(cameras=cams)


def apply_rig(db, angles, logger):
    """Write the rig into the database, so mapping solves per standpoint."""
    import pycolmap

    if not angles:
        logger.warning("no rig geometry from the reproject stage; "
                       "every view will be solved independently")
        return
    cfg = rig_config(angles, pycolmap)
    database = pycolmap.Database(str(db))
    try:
        pycolmap.apply_rig_config([cfg], database)
    finally:
        database.close()
    logger.info("declared a %d-sensor rig: the views of one panorama now share "
                "a centre by construction", len(angles))


@task(name="2. structure from motion")
def run_sfm(scene: str, spacing: float = 0.0, angles=None,
            panos: str = "", require_sfm: bool = False) -> dict:
    """COLMAP poses across every view from every panorama.

    Exhaustive matching: the views come from a handful of standpoints rather
    than a continuous walk, so there is no sequential ordering to exploit.
    """
    import pycolmap

    logger = get_run_logger()
    work = Path(scene)
    images = work / "images"
    db = work / "colmap.db"
    db.unlink(missing_ok=True)
    sparse = work / "sparse_tmp"
    shutil.rmtree(sparse, ignore_errors=True)
    sparse.mkdir(parents=True)

    # Defaults are tuned for textured outdoor scenes. Interiors are full of
    # bare wall and plain floor, where the default peak threshold finds
    # almost nothing — so lower it and allow more features per view.
    sift = pycolmap.SiftExtractionOptions()
    sift.max_num_features = 16384
    sift.peak_threshold = 0.002
    sift.estimate_affine_shape = True   # helps on surfaces seen at a slant
    sift.domain_size_pooling = True
    logger.info("feature extraction (peak %.4f, up to %d features/view)",
                sift.peak_threshold, sift.max_num_features)
    # A rig's sensors each need their own camera, and the per-direction folders
    # make PER_FOLDER land exactly one per sensor — the layout COLMAP's rig
    # workflow expects. But the rig only earns its place when poses have to be
    # solved for. With poses recorded there is nothing to constrain, so one
    # camera for everything is both simpler and enough.
    known = bool(panos) and (Path(panos) / "poses.json").is_file() and not require_sfm
    mode = (pycolmap.CameraMode.SINGLE if known
            else pycolmap.CameraMode.PER_FOLDER)
    pycolmap.extract_features(db, images, camera_mode=mode, sift_options=sift)
    n_img = sum(1 for _ in images.rglob("*.jpg"))
    logger.info("exhaustive matching over %d views (%d pairs)",
                n_img, n_img * (n_img - 1) // 2)
    pycolmap.match_exhaustive(db)

    apply_rig(db, angles, logger)
    # mapping is one long call with no output of its own; say so, otherwise
    # the run looks hung for minutes
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).parent / "tools"))
    import known_poses as kp

    recorded = kp.load_poses(Path(panos)) if panos else None
    if recorded and not require_sfm:
        # Nothing to solve and nothing to build: the poses go straight to
        # training. No reconstruction, so no rigs, frames, sensors or database
        # consistency to satisfy — all of which existed only to carry poses we
        # already had.
        logger.info("poses.json found: %d standpoint(s) recorded, so no "
                    "reconstruction is needed", len(recorded))
        return {"registered": len(recorded) * len(angles or []),
                "total": len(recorded) * len(angles or []),
                "points": 0, "metric_scale": 1.0, "models": 1,
                "poses": "recorded", "skip_colmap": True}

    logger.info("incremental mapping (no output until it finishes)")
    opts = pycolmap.IncrementalPipelineOptions()
    # Pin the rig. Its relative poses are not estimates: we generate the
    # reprojection, so we know each view's rotation off the panorama and that
    # they share a centre exactly. Verified numerically — cam_from_rig
    # reproduces the reprojection's own geometry to 1e-12. Left free, bundle
    # adjustment drifts the sensors 0.49 m apart, identically in every frame,
    # which is the depth ambiguity reappearing once per rig.
    opts.ba_refine_sensor_from_rig = False
    # Pinning leaves only one place to find a baseline: between panoramas.
    # Two views of the same one now have a genuinely zero triangulation angle,
    # so the initial pair must span standpoints — and standpoints are 0.9 m
    # apart looking at walls a few metres away, which is a modest angle. The
    # 16-degree default rejects those honest pairs along with the impossible
    # ones, and only two attempts are made before giving up; COLMAP registered
    # nothing in 80 minutes. Accept a smaller angle, and let it look harder.
    opts.mapper.init_min_tri_angle = 4.0
    opts.mapper.init_max_reg_trials = 20
    recs = pycolmap.incremental_mapping(db, images, sparse, options=opts)
    if not recs:
        raise RuntimeError(
            "SfM found no reconstruction — the panoramas probably do not "
            "overlap enough. Shoot standpoints closer together.")

    best_dir, best, frag = pick_model(sparse, logger)
    if best_dir is None:
        raise RuntimeError(
            f"no readable reconstruction in {sparse} — see the log above for "
            f"why each model failed to load")
    best_id = best_dir.name
    total = sum(1 for _ in images.rglob("*.jpg"))
    logger.info("registered %d/%d views, %d points, %.2f px reproj error",
                best.num_reg_images(), total, best.num_points3D(),
                best.compute_mean_reprojection_error())
    if best.num_reg_images() < 0.5 * total:
        logger.warning("under half the views registered; geometry will be thin")

    # undistort into the conventional images/ + sparse/0 layout
    undist = work / "undistorted"
    shutil.rmtree(undist, ignore_errors=True)
    pycolmap.undistort_images(undist, sparse / str(best_id), images)
    model = undist / "sparse" / "0"
    model.mkdir(parents=True, exist_ok=True)
    for f in ("cameras.bin", "images.bin", "points3D.bin"):
        src = undist / "sparse" / f
        if src.exists():
            src.rename(model / f)
    shutil.rmtree(undist / "stereo", ignore_errors=True)

    scale = rescale_to_metric(model, spacing, logger) if spacing > 0 else 1.0
    return {"registered": best.num_reg_images(), "total": total,
            "points": best.num_points3D(), "metric_scale": scale, **frag}


# ---------------------------------------------------------------------- train

def load_colmap(data: Path, device: str):
    import cv2
    import pycolmap
    import torch

    rec = pycolmap.Reconstruction(data / "sparse" / "0")
    Ks, viewmats, images = [], [], []
    for _, im in sorted(rec.images.items()):
        cam = rec.cameras[im.camera_id]
        p = np.asarray(cam.params)
        if cam.model.name == "PINHOLE":
            fx, fy, cx, cy = p
        elif cam.model.name == "SIMPLE_PINHOLE":
            fx = fy = p[0]; cx, cy = p[1:]
        else:
            raise ValueError(f"unexpected camera model {cam.model.name}")
        img = cv2.cvtColor(cv2.imread(str(data / "images" / im.name)), cv2.COLOR_BGR2RGB)
        Ks.append([[fx, 0, cx], [0, fy, cy], [0, 0, 1]])
        cfw = im.cam_from_world() if callable(im.cam_from_world) else im.cam_from_world
        m = np.eye(4, dtype=np.float32)
        m[:3, :] = np.asarray(cfw.matrix(), dtype=np.float32)
        viewmats.append(m)
        images.append(torch.from_numpy(img))

    pts = np.array([p.xyz for p in rec.points3D.values()], dtype=np.float32)
    rgb = np.array([p.color for p in rec.points3D.values()], dtype=np.float32) / 255.0
    Ks = torch.tensor(Ks, dtype=torch.float32, device=device)
    viewmats = torch.tensor(np.stack(viewmats), device=device)
    centers = torch.linalg.inv(viewmats)[:, :3, 3]
    scale = float((centers - centers.mean(0)).norm(dim=1).max()) * 1.1
    # no depth: a real 360 camera records none, so that path trains on colour
    # alone exactly as it always has
    return Ks, viewmats, images, pts, rgb, max(scale, 1e-3), None


def save_ply(params, path: Path) -> int:
    import torch
    from plyfile import PlyData, PlyElement

    means = params["means"].detach().cpu().numpy()
    scales = params["scales"].detach().cpu().numpy()
    quats = torch.nn.functional.normalize(params["quats"].detach(), dim=1).cpu().numpy()
    opac = params["opacities"].detach().cpu().numpy()
    color = torch.sigmoid(params["colors"].detach()).cpu().numpy()
    f_dc = (color - 0.5) / SH_C0

    n = means.shape[0]
    fields = [(c, "f4") for c in
              ("x", "y", "z", "nx", "ny", "nz", "f_dc_0", "f_dc_1", "f_dc_2",
               "opacity", "scale_0", "scale_1", "scale_2",
               "rot_0", "rot_1", "rot_2", "rot_3")]
    arr = np.zeros(n, dtype=fields)
    arr["x"], arr["y"], arr["z"] = means.T
    for i in range(3):
        arr[f"f_dc_{i}"] = f_dc[:, i]
        arr[f"scale_{i}"] = scales[:, i]
    arr["opacity"] = opac
    for i in range(4):
        arr[f"rot_{i}"] = quats[:, i]
    path.parent.mkdir(parents=True, exist_ok=True)
    PlyData([PlyElement.describe(arr, "vertex")]).write(str(path))
    return n


def cut_slivers(params):
    """Bound how needle-like, and how large, a gaussian may be. Returns the mask.

    Two rules, because one of them was measured to be the wrong shape. Bounding
    length only above an absolute size let 3,611 needles through on one
    corridor — under 25 cm, so untouched, and 0.005 cm thick. A needle's
    footprint on screen is its length times about a pixel, not its length times
    its thickness, so they scored 0.3% by area while each drew a line across a
    quarter of the frame. That is what the starbursts are.

    So the shape rule applies at every size: nothing may be more than
    NEEDLE_MAX times longer than it is wide. It scales with thickness, which is
    the point — a hair-thin gaussian is cut to nothing, while a 5 cm thick wall
    patch may still run 75 cm.

    And a flat cap on top, because a sheet is not a needle by that test: a 7 m
    gaussian with an aspect ratio of 9 passed the shape rule and still draws a
    line through the building when seen edge-on.
    """
    import torch

    with torch.no_grad():
        sc = torch.exp(params["scales"])
        before = sc.clone()
        # every axis, not only the longest: clamping the longest alone just
        # promotes the middle one, which is how a 141 cm gaussian survived a
        # 100 cm cap
        sc = sc.clamp(max=SPLAT_MAX_M)
        srt, order = sc.sort(dim=1, descending=True)
        rows = torch.arange(sc.shape[0], device=sc.device)
        sc[rows, order[:, 0]] = torch.minimum(srt[:, 0], srt[:, 1] * NEEDLE_MAX)
        changed = (sc != before).any(dim=1)
        params["scales"].copy_(torch.log(sc.clamp(min=1e-6)))
    return changed


@task(name="3. gaussian splatting")
def train(scene: str, panos: str, angles, device: str) -> dict:
    """Optimise gaussians against the posed views.

    Classic rasterization (not antialiased): AA-trained opacities only render
    correctly in renderers applying the same compensation, and these PLYs go
    to web viewers and Isaac.
    """
    import torch
    from gsplat import DefaultStrategy, rasterization
    from torchmetrics.functional.image import structural_similarity_index_measure as ssim_fn

    logger = get_run_logger()
    dev = device
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).parent / "tools"))
    import known_poses as kp

    if panos and kp.load_poses(Path(panos)):
        # the capture recorded where it stood, so the poses go straight in and
        # no reconstruction is read
        Ks, viewmats, images, pts, rgb, scale, depths = kp.load(
            Path(panos), Path(scene) / "images", angles, dev, logger)
    else:
        data = Path(scene) / "undistorted"
        Ks, viewmats, images, pts, rgb, scale, depths = load_colmap(data, dev)
    probe = None
    if panos and kp.load_poses(Path(panos)):
        probe = kp.free_space_probe(Path(panos), kp.load_poses(Path(panos)), dev)

    n_views = len(images)
    # Hold out whole viewpoints, not scattered views.
    #
    # A panorama becomes a dozen views sharing one camera centre, so holding
    # out every eighth view leaves eleven neighbours of it in training. That
    # measures how well the splat interpolates in *direction* from a centre it
    # already fits, and never tests depth — which is how 38 dB coexisted with a
    # corridor smeared over three times its width.
    #
    # Grouping by camera centre states the requirement directly: a held-out
    # view must not share its viewpoint with a trained one. Recorded poses
    # group exactly. A solved reconstruction scatters one panorama's views by
    # more than the spacing between standpoints, so there each view is its own
    # group and the old caveat stands.
    centres = torch.linalg.inv(viewmats)[:, :3, 3].cpu().numpy()
    seen: dict[tuple, int] = {}
    groups = np.array([seen.setdefault(tuple(c), len(seen))
                       for c in np.round(centres, 4)])
    n_groups = len(seen)
    # a short walk keeps every viewpoint and reports no PSNR, rather than
    # crippling the splat that is the actual output to produce a number
    drop = set(range(1, n_groups, 4)) if n_groups >= 6 else set()
    holdout = {i for i in range(n_views) if groups[i] in drop}
    train_ids = [i for i in range(n_views) if i not in holdout]
    logger.info("%d views from %d viewpoint(s) (%d train / %d eval over %d "
                "held-out viewpoint(s)), %d seed points, scene scale %.2f",
                n_views, n_groups, len(train_ids), len(holdout), len(drop),
                len(pts), scale)

    means = torch.tensor(pts, device=dev)
    # Chunk by the size of the distance matrix, not by a fixed row count: it is
    # rows x all points, so a fixed 8192 rows quietly grew from a 2 GB
    # allocation to 6.5 GB when the seed cloud went from noise to surfaces —
    # which segfaulted the process rather than raising.
    n_seed = means.shape[0]
    rows = int(min(8192, max(256, 64_000_000 // max(n_seed, 1))))
    knn = []
    for i in range(0, n_seed, rows):
        d = torch.cdist(means[i:i + rows], means)
        knn.append(d.topk(4, largest=False).values[:, 1:].mean(1))
    params = torch.nn.ParameterDict({
        "means": torch.nn.Parameter(means),
        "scales": torch.nn.Parameter(
            torch.log(torch.cat(knn).clamp(min=1e-5))[:, None].repeat(1, 3)),
        "quats": torch.nn.Parameter(
            torch.tensor([[1.0, 0, 0, 0]], device=dev).repeat(means.shape[0], 1)),
        "opacities": torch.nn.Parameter(
            torch.logit(torch.full((means.shape[0],), 0.1, device=dev))),
        "colors": torch.nn.Parameter(
            torch.logit(torch.tensor(rgb, device=dev).clamp(1e-4, 1 - 1e-4))),
    }).to(dev)

    lrs = {"means": 1.6e-4 * scale, "scales": 5e-3, "quats": 1e-3,
           "opacities": 5e-2, "colors": 2.5e-3}
    opts = {k: torch.optim.Adam([v], lr=lrs[k], eps=1e-15) for k, v in params.items()}
    sched = torch.optim.lr_scheduler.ExponentialLR(opts["means"], gamma=0.01 ** (1 / ITERS))
    # Leave prune_scale3d at its default. Its threshold is
    # prune_scale3d * scene_scale, and scene_scale here is the spread of the
    # capture standpoints, not the size of the room — tightening it to 0.04
    # pruned legitimate wall and floor splats and cost 23 dB.
    strategy = DefaultStrategy(refine_stop_iter=ITERS // 2)
    strategy.check_sanity(params, opts)
    state = strategy.initialize_state(scene_scale=scale)

    def render(idx):
        gt = images[idx].to(dev).float() / 255.0
        H, W = gt.shape[:2]
        out, alpha, info = rasterization(
            params["means"], torch.nn.functional.normalize(params["quats"], dim=1),
            torch.exp(params["scales"]), torch.sigmoid(params["opacities"]),
            torch.sigmoid(params["colors"]), viewmats[idx][None], Ks[idx][None],
            W, H, packed=False, rasterize_mode="classic",
            render_mode="RGB+ED" if depths is not None else "RGB")
        return gt, out, alpha, info

    for step in range(ITERS):
        i = train_ids[torch.randint(len(train_ids), (1,)).item()]
        gt, out, alpha, info = render(i)
        strategy.step_pre_backward(params, opts, state, step, info)
        colour = out[..., :3]
        l1 = (colour[0] - gt).abs().mean()
        ssim = ssim_fn(colour.permute(0, 3, 1, 2), gt[None].permute(0, 3, 1, 2),
                       data_range=1.0)
        loss = 0.8 * l1 + 0.2 * (1 - ssim)
        # Where the simulator says the surface is.
        #
        # Colour alone cannot place a gaussian along the ray it is seen on, and
        # every standpoint of a corridor walk sits on one line — so a splat can
        # reproduce every training image with 1.8% of its gaussians sitting in
        # free space a standpoint saw straight through, covering 8.9% of the
        # frame. Those are the artifacts in clear air. Depth puts them out.
        #
        # The error is relative, so a wall four metres down the corridor does
        # not outweigh one an arm's length away.
        if depths is not None:
            gz = depths[i].to(dev)
            keep = gz > 1e-3
            if keep.any():
                loss = loss + DEPTH_WEIGHT * (
                    (out[0, ..., 3][keep] - gz[keep]).abs()
                    / gz[keep].clamp(min=0.5)).mean()

        loss.backward()
        strategy.step_post_backward(params, opts, state, step, info, packed=False)
        for o in opts.values():
            o.step(); o.zero_grad(set_to_none=True)
        sched.step()
        # Clear what the capture proved is empty.
        #
        # A gaussian sitting where a standpoint saw straight through is wrong
        # whatever it does for the loss, and no photometric term reliably says
        # so: on an untextured wall a cloud that averages to the right grey is
        # nearly free, and 47% of this corridor's gaussians ended up in space
        # the capture had already measured as open — a third of every frame.
        # Depth is a mean and a cloud drags it; alpha says "end this ray" and
        # is satisfied by filling it. Ground truth answers directly, so the
        # opacity of those gaussians is driven to nothing and the strategy
        # prunes them on its next pass.
        # For the whole run, not the first half. Densification stops halfway,
        # but optimisation does not: cleared gaussians grow back over the
        # remaining steps and sprawl again, which left a corridor with 1.8% of
        # its gaussians carrying 74% of the frame.
        if probe is not None and step and step % 500 == 0:
            with torch.no_grad():
                # seven probes bounding each ellipsoid, not one at its centre:
                # a gaussian lying on a wall but a metre across still hangs in
                # the air in front of it, and those few carried most of the
                # cloud — 7% of gaussians, 59% of the frame on one corridor
                n_free = kp.sprawl(probe, params["means"].detach(),
                                   params["quats"].detach(),
                                   torch.exp(params["scales"].detach()))
                adrift = n_free >= 4          # more out of the room than in it
                over = (n_free >= 1) & ~adrift
                if adrift.any():
                    params["opacities"][adrift] = -20.0

                # Cut long slivers back, rather than asking the loss to.
                #
                # A penalty averaged over every gaussian is diluted by the 99%
                # that are fine: each offender feels a hundredth of the
                # gradient, and once the walls had a texture there was a strong
                # photometric reason to grow along it. Ten of twenty-six
                # corridors went past 30 cm, two past 70. Weighting the penalty
                # up instead would push on the shape of everything.
                #
                # Nothing in a corridor is a 70 cm splinter a centimetre thick,
                # so this states that rather than bargaining for it: the
                # longest axis of a gaussian that is both long in metres and
                # shaped like a sliver is set back to the bound. A wall's wide
                # flat disc is not a sliver and is untouched.
                long_sliver = cut_slivers(params)
                if over.any():
                    # reaching into open air but anchored: pull it back rather
                    # than delete it, since its centre is on something real
                    params["scales"][over] -= 0.35        # log space, about x0.7
                if step % 2000 == 0:
                    logger.info("step %d: cleared %d adrift, pulled in %d "
                                "overreaching, cut %d long sliver(s)", step,
                                int(adrift.sum()), int(over.sum()),
                                int(long_sliver.sum()))
        if step % 1000 == 0:
            logger.info("step %d/%d  %d gaussians", step, ITERS, params["means"].shape[0])

    psnrs = []
    with torch.no_grad():
        for i in sorted(holdout):
            gt, out, _, _ = render(i)
            mse = ((out[0, ..., :3].clamp(0, 1) - gt) ** 2).mean().item()
            psnrs.append(-10 * math.log10(max(mse, 1e-10)))
    # Once more before saving. The clamp above last runs 500 steps from the
    # end, and the scale learning rate is 5e-3 in log space — enough for a
    # gaussian to grow twelvefold in what is left, which is how a 4.6 m sliver
    # survived a 25 cm bound. The bound has to hold on what is written out,
    # not only during training.
    with torch.no_grad():
        cut = cut_slivers(params)
    if cut.any():
        logger.info("cut %d sliver(s) that regrew after the last pass",
                    int(cut.sum()))

    # None, not NaN: no held-out viewpoint means the number was not
    # measured, which is different from measuring it badly
    psnr = round(float(np.mean(psnrs)), 2) if psnrs else None

    n = save_ply(params, Path(scene) / "world.ply")
    logger.info("%d gaussians, held-out PSNR %s", n,
                f"{psnr:.2f} dB" if psnr is not None else
                "not measured (too few viewpoints to spare one)")
    # The worst aspect ratio left, and the largest gaussian. Length alone was
    # the wrong thing to record: the needles that draw the starbursts are
    # under 25 cm and hair-thin, so they looked harmless by every measure that
    # multiplied length by thickness.
    with torch.no_grad():
        sc = torch.sort(torch.exp(params["scales"]), dim=1, descending=True).values
        keep = torch.sigmoid(params["opacities"]) > 0.5
        solid = sc[keep]
        ratio = float((solid[:, 0] / solid[:, 1].clamp(min=1e-8)).max())
        longest = float(solid[:, 0].max() * 100)
    logger.info("%d solid gaussians, worst aspect ratio %.1f, largest %.1f cm",
                int(keep.sum()), ratio, longest)
    return {"gaussians": n, "psnr": psnr,
            "worst_ratio": round(ratio, 1), "longest_cm": round(longest, 1)}


# --------------------------------------------------------------------- export

@task(name="4. align")
def align(scene: str, panos: str) -> dict:
    """Place the splat where it belongs in the building.

    Only for a corridor: the id has to name a lane, because the lane is what
    supplies the direction and position. A capture of a room has neither, so it
    stays in its own frame and says so.
    """
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).parent / "tools"))
    import align as al

    logger = get_run_logger()
    scene_p = Path(scene)
    edge_id = scene_p.name
    if (Path(panos) / "poses.json").is_file():
        # built at poses recorded in building metres, so it is already where it
        # belongs — there is nothing to solve, and no residual to report
        logger.info("built from recorded poses: already in building coordinates")
        return {"aligned": True, "edge": edge_id, "align_residual_m": 0.0,
                "placed_by": "recorded poses"}
    project = scene_p.parent.parent                 # .../<project>/splats/<id>
    plans = sorted((project / "worlds").glob("*/capture_plan.json"))
    if not plans:
        logger.info("no capture plan for %s; leaving the splat in its own frame",
                    project.name)
        return {"aligned": False, "why": "no capture plan"}

    plan = json.loads(plans[0].read_text())
    R, t, report = al.solve(scene_p, plan, edge_id, Path(panos))
    if not report.get("aligned"):
        logger.info("not aligned: %s", report.get("why"))
        return report

    n = al.apply_to_ply(scene_p / "world.ply", R, t)
    logger.info("placed %s in building coordinates: walked from %s, "
                "%.2f m of walk onto a %.2f m lane, endpoints off by %.2f m",
                edge_id, report["walked_from"], report["walk_span_m"],
                report["lane_length_m"], report["align_residual_m"])
    if report["align_residual_m"] > 0.5:
        logger.warning("that is a large residual — the capture may not be the "
                       "corridor this id names, or it was walked the other way")
    logger.info("moved %d gaussians", n)
    return report


@task(name="5. export")
def export(scene: str, panos: str = "") -> dict:
    """Isaac Sim USDZ, plus the spawn camera and tour path the viewer opens at."""
    logger = get_run_logger()
    world = Path(scene) / "world.ply"
    usdz = Path(scene) / "world.usdz"

    sys.path.insert(0, str(TOOLS))
    import known_poses as kp

    cmd = ["python", str(TOOLS / "ply_to_isaac.py"), str(world), str(usdz)]
    logger.info("$ %s", " ".join(cmd))
    subprocess.run(cmd, check=True)

    if panos and kp.load_poses(Path(panos)):
        # the walk is recorded, so the spawn pose and the tour path come
        # straight from it rather than out of a reconstruction
        got = kp.write_sidecars(Path(panos), world)
        logger.info("sidecars from the recorded walk: %d standpoint(s), "
                    "%d path point(s)", got["standpoints"], got["points"])
    else:
        cmd = ["python", str(TOOLS / "make_spawn_cam.py"), "--colmap",
               f"{scene}/undistorted/sparse/0", str(world)]
        logger.info("$ %s", " ".join(cmd))
        subprocess.run(cmd, check=True)

    logger.info("%s (%.1f MB)", usdz.name, usdz.stat().st_size / 1e6)
    return {"ply": str(world), "usdz": str(usdz)}


def _run_name() -> str:
    """Name the run after the one thing it produces, so the queue at :4200 reads
    as a list of places in a building rather than a list of random adjectives.
    One run, one artifact — that is the tracking unit."""
    from prefect.runtime import flow_run

    parts = Path(flow_run.parameters.get("scene", "?")).parts
    # .../<project>/splats/<id>
    if len(parts) >= 3 and parts[-2] == "splats":
        return f"{parts[-3]}/{parts[-1]}"
    return "/".join(parts[-2:]) if len(parts) > 1 else str(parts[-1])


@flow(name="reconstruct-simulated", log_prints=True, retries=1,
      retry_delay_seconds=15, flow_run_name=_run_name)
def reconstruct_simulated(scene: str, panos: str) -> dict:
    """A capture from the simulator, which recorded where it stood.

    Separate from reconstruct-world on purpose. A real 360 capture cannot tell
    you its poses, so that one has to infer them and then work out where the
    result belongs in the building. This one is handed both, so it skips the
    solve and the alignment entirely — different work, different job, and the
    queue says which ran rather than leaving it to a file's presence.
    """
    if not (Path(panos) / "poses.json").is_file():
        raise RuntimeError(
            f"no poses.json in {panos}. Captures from the simulator record "
            f"one; a real capture cannot, and belongs in reconstruct-world.")
    return reconstruct_world(scene=scene, panos=panos, spacing=0.0)


# One retry, because the failure this guards against is transient by nature:
# a lost race for a GPU, or a driver hiccup under several concurrent runs. A
# corridor that genuinely cannot be reconstructed fails twice and says so.
@flow(name="reconstruct-world", log_prints=True, retries=1,
      retry_delay_seconds=15, flow_run_name=_run_name)
def reconstruct_world(scene: str, panos: str, spacing: float = 0.5) -> dict:
    """scene: output dir; panos: panoramas of one space.

    spacing: metres between consecutive standpoints, and the one thing that
    genuinely differs between the two sources. A real capture is scale-free
    until you say how far you walked; a simulated one passes 0, because its
    poses are already in building metres.
    """
    shot = reproject(scene, panos)
    sfm = run_sfm(scene, spacing, shot["angles"], panos,
                  require_sfm=spacing > 0)
    # Hold one card for the length of the training, and give it back after.
    # The claim lives here rather than inside train() so the release is tied to
    # the flow run finishing, however it finishes.
    dev, release = claim_gpu()
    try:
        stats = train(scene, panos, shot["angles"], dev)
    finally:
        release()
    # align before export, so the sidecars and the Isaac USDZ describe the
    # splat where it actually sits in the building
    placed = align(scene, panos)
    out = export(scene, panos)

    # Everything worth judging the result by, written next to it. These numbers
    # were only ever in the run log before, which made two splats impossible to
    # compare without opening two flow runs — `just plan` reads this back.
    info = {
        "units": "metres" if spacing > 0 else "unitless",
        "standpoint_spacing_m": spacing,
        "metric_scale": sfm["metric_scale"],
        "panoramas": shot["panos"],
        "views": shot["views"],
        "registered": sfm["registered"],
        "points": sfm["points"],
        "gaussians": stats["gaussians"],
        "psnr_db": stats["psnr"],
        "worst_ratio": stats.get("worst_ratio"),
        "longest_cm": stats.get("longest_cm"),
        **{k: v for k, v in placed.items() if k != "transform"},
    }
    if placed.get("transform"):
        info["transform"] = placed["transform"]
    if "models" in sfm:
        info["sfm_models"] = sfm["models"]
    Path(scene, "world.info.json").write_text(json.dumps(info, indent=2))

    if spacing > 0:
        print(f"metric: {sfm['metric_scale']:.4f}x applied "
              f"({spacing} m between standpoints)")
    print(f"3DGS : {out['ply']}  ({stats['gaussians']:,} gaussians, "
          f"{stats['psnr']} dB)")
    print(f"Isaac: {out['usdz']}")
    return {**out, **stats}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="mode", required=True)
    sub.add_parser("serve")
    one = sub.add_parser("run")
    one.add_argument("scene")
    one.add_argument("panos")
    one.add_argument("--spacing", type=float, default=0.5,
                     help="metres between standpoints (enables metric scale)")
    args = p.parse_args()

    if args.mode == "serve":
        reconstruct_world.serve(name=DEPLOYMENT, limit=1)
    else:
        reconstruct_world(args.scene, args.panos, spacing=args.spacing)


if __name__ == "__main__":
    main()
