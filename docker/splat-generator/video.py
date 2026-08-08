"""render-video — a walkthrough of a built splat, as a job.

The camera follows the capture path, because that is where the scene was
actually observed; straying from it is where a gaussian splat looks worst,
since nothing constrained the geometry there.

Three stages, so a long render reports where it is rather than going quiet:

  1. plan       the capture path -> one camera pose per frame
  2. render     rasterise every frame (GPU)
  3. encode     H.264, so browsers and players accept it

  python submit.py render-video/dreamworld \
      scene=/workspace/projects/<p>/splats/<s> seconds=20 path=line
"""

from __future__ import annotations

import sys
from pathlib import Path

from prefect import flow, get_run_logger, task

sys.path.insert(0, str(Path(__file__).parent / "tools"))
import render_video as rv  # noqa: E402


@task(name="1. plan path")
def plan(scene: str, kind: str, n_frames: int) -> dict:
    logger = get_run_logger()
    eyes, targets, up, n_stand = rv.plan_path(Path(scene), kind, n_frames)
    span = float(((eyes[-1] - eyes[0]) ** 2).sum() ** 0.5)
    logger.info("%s path over %d standpoints, %.2f m end to end, %d frames",
                kind, n_stand, span, n_frames)
    return {"eyes": eyes.tolist(), "targets": targets.tolist(),
            "up": up.tolist(), "standpoints": n_stand}


@task(name="2. render")
def render(scene: str, plan: dict, out: str, width: int, height: int,
           fov: float, fps: int) -> str:
    import numpy as np

    logger = get_run_logger()
    tmp = rv.render_frames(Path(scene), np.array(plan["eyes"]),
                           np.array(plan["targets"]), np.array(plan["up"]),
                           Path(out), width, height, fov, fps)
    logger.info("rasterised %d frames at %dx%d", len(plan["eyes"]), width, height)
    return str(tmp)


@task(name="3. encode")
def encode(tmp: str, out: str) -> dict:
    logger = get_run_logger()
    rv.encode(Path(tmp), Path(out))
    mb = Path(out).stat().st_size / 1e6
    logger.info("wrote %s (%.1f MB)", out, mb)
    return {"video": out, "mb": round(mb, 1)}


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


@flow(name="render-video", log_prints=True,
      flow_run_name=_run_name)
def render_walkthrough(scene: str, seconds: float = 20.0, fps: int = 30,
                       width: int = 1280, height: int = 720, fov: float = 75.0,
                       path: str = "line", out: str = "") -> dict:
    """scene: a splats/<name> directory holding world.ply and its COLMAP model."""
    logger = get_run_logger()
    if not (Path(scene) / "world.ply").is_file():
        raise SystemExit(f"no world.ply in {scene} — build the splat first")
    out = out or str(Path(scene) / "walkthrough.mp4")
    n_frames = int(seconds * fps)
    logger.info("rendering %s -> %s", scene, out)

    p = plan(scene, path, n_frames)
    tmp = render(scene, p, out, width, height, fov, fps)
    result = encode(tmp, out)
    result["standpoints"] = p["standpoints"]
    result["frames"] = n_frames
    return result
