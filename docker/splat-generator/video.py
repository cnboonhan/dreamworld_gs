"""render-video — a walkthrough of a built splat, as a job.

The camera rides `world.path.json`: for a generated world that is the corridor
it was built from, straight out of the building map; for a reconstructed one it
is the walk the panoramas were shot along. Either way it is where the scene was
actually observed, and straying from it is where a gaussian splat looks worst.

Three stages, so a long render reports where it is rather than going quiet:

  1. plan       the walk -> one camera pose per frame
  2. render     rasterise every frame (GPU)
  3. encode     H.264, so browsers and players accept it

  python submit.py render-video/dreamworld \
      scene=/workspace/projects/<p>/splats/<s>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from prefect import flow, get_run_logger, task

sys.path.insert(0, str(Path(__file__).parent / "tools"))
import render_video as rv  # noqa: E402


@task(name="1. plan path")
def plan(scene: str, n_frames: int) -> dict:
    logger = get_run_logger()
    eyes, targets, up = rv.plan_path(Path(scene), n_frames)
    logger.info("the walk, %d frames", n_frames)
    return {"eyes": eyes.tolist(), "targets": targets.tolist(), "up": up.tolist()}


@task(name="2. render")
def render(plan: dict, out: str) -> str:
    import numpy as np

    logger = get_run_logger()
    plys = [Path(p) for p in plan["plys"]]
    tmp = rv.render_frames(np.array(plan["eyes"]), np.array(plan["targets"]),
                           np.array(plan["up"]), Path(out), plys)
    logger.info("rasterised %d frames at %dx%d from %d splat(s)",
                len(plan["eyes"]), rv.WIDTH, rv.HEIGHT, len(plys))
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


@flow(name="render-video", log_prints=True, flow_run_name=_run_name)
def render_walkthrough(scene: str) -> dict:
    """scene: a splats/<name> directory holding world.ply and world.path.json.

    The walk is paced at walking speed from the length the sidecar records, so
    a one-metre corridor and a six-metre one are watched at the same speed
    rather than stretched to a common duration.
    """
    logger = get_run_logger()
    if not (Path(scene) / "world.ply").is_file():
        raise SystemExit(f"no world.ply in {scene} — build the splat first")
    out = str(Path(scene) / "walkthrough.mp4")
    doc = json.loads((Path(scene) / "world.path.json").read_text())
    n_frames = rv.frames_for(doc["length_m"])
    logger.info("rendering %s -> %s", scene, out)

    p = plan(scene, n_frames)
    p["plys"] = [str(Path(scene) / "world.ply")]
    result = encode(render(p, out), out)
    result["frames"] = n_frames
    return result


@task(name="1. plan route")
def plan_route_path(route: str, n_frames: int) -> dict:
    """The route's polyline, as camera poses — and the splats it crosses."""
    logger = get_run_logger()
    doc = json.loads(Path(route).read_text())
    eyes, targets, up = rv.route_path(doc, n_frames)
    # .../<project>/traversals/<name>.route.json -> .../<project>
    root = Path(route).parent.parent
    plys, seen = [], set()
    for s in doc["segments"]:                       # a route may revisit one
        if s["splat"] not in seen:
            seen.add(s["splat"])
            plys.append(str(root / s["splat"]))
    missing = [p for p in plys if not Path(p).is_file()]
    if missing:
        raise SystemExit("no splat at " + ", ".join(missing))
    logger.info("%s: %.1f m over %d splat(s), %d frames",
                " -> ".join(doc["waypoints"]), doc["metres"], len(plys), n_frames)
    return {"eyes": eyes.tolist(), "targets": targets.tolist(),
            "up": up.tolist(), "plys": plys}


def _route_run_name() -> str:
    from prefect.runtime import flow_run

    return Path(flow_run.parameters.get("route", "?")).name.replace(".route.json", "")


@flow(name="render-route", log_prints=True, flow_run_name=_route_run_name)
def render_route(route: str) -> dict:
    """A walkthrough of a whole route, the way the viewer streams it.

    The viewer is the live version of this and needs no render; this is the
    same walk written to a file, for showing someone who is not at the machine
    — and, because it is rasterised from the union of the corridors' gaussians,
    it is also the check that they meet without a step at the vertex.
    """
    logger = get_run_logger()
    doc = json.loads(Path(route).read_text())
    out = str(Path(route).with_suffix("").with_suffix("")) + ".mp4"
    n_frames = rv.frames_for(doc["metres"])
    logger.info("rendering %s -> %s (%d frames)", route, out, n_frames)

    p = plan_route_path(route, n_frames)
    result = encode(render(p, out), out)
    result["waypoints"] = doc["waypoints"]
    result["splats"] = len(p["plys"])
    result["frames"] = n_frames
    return result
