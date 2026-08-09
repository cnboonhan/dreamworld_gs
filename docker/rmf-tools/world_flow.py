"""build-world — a floorplan becomes a simulable building, as a job.

Runs in the rmf-tools image because only it has Gazebo and
rmf_building_map_tools; the splat generator serves its own flows from its own
image. Both talk to the same Prefect server, so every operation in this repo
shows up in one place at :4200.

Three stages:

  1. generate   building.yaml -> world + models + nav graph  (shells the
                generator, which is a ROS entrypoint, not a library)
  2. inspect    read the nav graph back: levels, waypoints, lanes, doors
  3. plan       name every vertex and edge, and write the capture plan

Stages 2 and 3 are the point of running this as a job rather than a script: the
nav graph is the contract the splat side keys against, so what it holds — and
what still needs photographing — is worth recording per run.

  python submit.py build-world/dreamworld project=<p> [map=<m>]

Also serves capture-edge: photograph one corridor of the simulated building, so
the splat pipeline can be exercised end to end without anyone walking it.

  python submit.py capture-edge/dreamworld project=<p> edge=<id> [spacing=0.5] [zigzag=0.1]
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import yaml
from prefect import flow, get_run_logger, serve, task
from prefect.artifacts import create_table_artifact

PROJECTS = Path(os.environ.get("DW_PROJECTS_DIR", "/projects"))
# How many corridors may be photographed at once. Each capture is a whole
# Gazebo rendering 2688x2016 frames, which measured around eight cores apiece,
# and each now runs on its own partition and ROS domain so they cannot see one
# another. Capped at seven: past that they contend for the box, not for a bus.
CAPTURES = max(1, min(7, (os.cpu_count() or 8) // 8))


def resolve_map(project: str, map_name: str) -> str:
    """One map per project is the common case, so name it only when there is
    a choice. Resolved here rather than parsed back out of the script's log."""
    if map_name:
        return map_name
    maps = PROJECTS / project / "maps"
    if (maps / f"{project}.building.yaml").is_file():
        return project
    found = sorted(maps.glob("*.building.yaml"))
    if not found:
        raise RuntimeError(
            f"project '{project}' has no map: {maps} holds no .building.yaml")
    return found[0].name[: -len(".building.yaml")]


@task(name="1. generate")
def generate(project: str, map_name: str) -> str:
    """building_map_generator, via the shell entrypoint that sets up ROS."""
    logger = get_run_logger()
    proc = subprocess.run(["/app/generate_world.sh", project, map_name],
                          capture_output=True, text=True)
    for line in (proc.stdout + proc.stderr).splitlines():
        logger.info("%s", line)
    if proc.returncode != 0:
        raise RuntimeError(f"world generation failed for {project}/{map_name}")
    return map_name


@task(name="2. inspect")
def inspect(project: str, map_name: str) -> list[dict]:
    """What the nav graph holds — the part the splat side keys against."""
    logger = get_run_logger()
    nav = PROJECTS / project / "worlds" / map_name / "nav_graphs" / "0.yaml"
    if not nav.is_file():
        raise RuntimeError(f"no nav graph at {nav}")
    graph = yaml.safe_load(nav.read_text())

    rows = []
    for level, data in (graph.get("levels") or {}).items():
        verts = data.get("vertices") or []
        lanes = data.get("lanes") or []
        named = [v[2].get("name") for v in verts
                 if len(v) > 2 and isinstance(v[2], dict) and v[2].get("name")]
        doors = sorted({l[2].get("door_name") for l in lanes
                        if len(l) > 2 and isinstance(l[2], dict)
                        and l[2].get("door_name")})
        rows.append({
            "level": level,
            "vertices": len(verts),
            "waypoints": len(named),
            "lanes": len(lanes),
            "doors_on_lanes": len(doors),
            "named": ", ".join(sorted(named)) or "—",
        })
        logger.info("%s: %d vertices (%d named), %d lanes, %d door lane(s)",
                    level, len(verts), len(named), len(lanes), len(doors))
    return rows


def local_id(index: int, name: str) -> str:
    """A vertex's handle within its level: its name, or its index if unnamed.

    Most vertices are unnamed corners, and an index is the only thing that
    distinguishes them."""
    return name if name else f"v{index}"


def vertex_id(level: str, local: str) -> str:
    """The full id: always level-qualified.

    Waypoint names are not unique across a building — the sample map has a
    `lift_lobby` on L1 and another on L11, one directly above the other. Two
    different places must not share a folder, so the level is always part of
    the address, named or not."""
    return f"{level}.{local}"


def edge_id(level: str, a: str, b: str) -> str:
    """One corridor, one id.

    Every lane in these graphs is bidirectional, so sorting the endpoints names
    the edge the same whichever way you walked it. Lanes never cross levels, so
    the level is written once rather than on both ends."""
    return f"{level}.{'--'.join(sorted((a, b)))}"


@task(name="3. plan")
def plan(project: str, map_name: str, rows: list[dict]) -> dict:
    """Name every vertex and edge, and record what to photograph where.

    This is the join between the two halves: a capture belongs to a place in
    the building, and this file says which places exist and what each is
    called."""
    logger = get_run_logger()
    nav = PROJECTS / project / "worlds" / map_name / "nav_graphs" / "0.yaml"
    graph = yaml.safe_load(nav.read_text())

    levels: dict = {}
    capture: list[dict] = []
    for level, data in (graph.get("levels") or {}).items():
        verts = data.get("vertices") or []
        locals_, ids = [], []
        for i, v in enumerate(verts):
            props = v[2] if len(v) > 2 and isinstance(v[2], dict) else {}
            loc = local_id(i, props.get("name") or "")
            vid = vertex_id(level, loc)
            locals_.append(loc)
            ids.append(vid)
            entry = {"id": vid, "level": level, "index": i,
                     "x": round(float(v[0]), 3), "y": round(float(v[1]), 3),
                     "named": bool(props.get("name")),
                     "lift": bool(props.get("lift") or props.get("lift_cabin"))}
            levels.setdefault(level, {"vertices": [], "edges": []})
            levels[level]["vertices"].append(entry)


        seen = set()
        for lane in (data.get("lanes") or []):
            u, w = int(lane[0]), int(lane[1])
            if u == w or not (0 <= u < len(ids) and 0 <= w < len(ids)):
                continue
            eid = edge_id(level, locals_[u], locals_[w])
            if eid in seen:
                continue
            seen.add(eid)
            props = lane[2] if len(lane) > 2 and isinstance(lane[2], dict) else {}
            ax, ay = float(verts[u][0]), float(verts[u][1])
            bx, by = float(verts[w][0]), float(verts[w][1])
            levels[level]["edges"].append({
                "id": eid, "level": level, "a": ids[u], "b": ids[w],
                "length_m": round(((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5, 2),
                "door": props.get("door_name") or "",
            })
            # Only edges are captured. A corridor walk starts and ends on its
            # two vertices, so the vertex views are already in it — and the
            # capture burden halves. Vertices stay above as routing nodes.
            capture.append({"kind": "edge", "id": eid, "level": level,
                            "panos": f"panos/{eid}",
                            "splat": f"splats/{eid}"})

    doc = {"project": project, "map": map_name, "levels": levels,
           "capture": capture}
    out = PROJECTS / project / "worlds" / map_name / "capture_plan.json"
    out.write_text(json.dumps(doc, indent=1))

    nv = sum(len(l["vertices"]) for l in levels.values())
    ne = sum(len(l["edges"]) for l in levels.values())
    logger.info("capture plan: %d vertices, %d edges -> %s", nv, ne, out)

    create_table_artifact(
        key=f"nav-graph-{project}-{map_name}".lower().replace("_", "-"),
        table=rows,
        description=f"Nav graph for {project}/{map_name} — the waypoints and "
                    f"lanes a capture is indexed against",
    )
    create_table_artifact(
        key=f"capture-plan-{project}-{map_name}".lower().replace("_", "-"),
        table=[{"kind": c["kind"], "id": c["id"], "level": c["level"],
                "panoramas go in": c["panos"]} for c in capture],
        description=f"Where to photograph {project}/{map_name}: one folder per "
                    f"vertex and per edge",
    )
    return {"world": f"{project}/worlds/{map_name}/{map_name}.world",
            "nav_graph": f"{project}/worlds/{map_name}/nav_graphs/0.yaml",
            "capture_plan": f"{project}/worlds/{map_name}/capture_plan.json",
            "levels": [r["level"] for r in rows],
            "vertices": nv, "edges": ne}


def _run_name() -> str:
    """One run, one world — named for it, so the queue is readable."""
    from prefect.runtime import flow_run

    p = flow_run.parameters
    project = p.get("project", "?")
    return f"{project}/{p.get('map') or project}"


@flow(name="build-world", log_prints=True, flow_run_name=_run_name)
def build_world(project: str, map: str = "") -> dict:
    """project: a directory under assets/projects holding maps/<map>.building.yaml.

    map defaults to the project's own name, or to the only map it has.
    """
    resolved = resolve_map(project, map)
    generate(project, resolved)
    rows = inspect(project, resolved)
    return plan(project, resolved, rows)


def _capture_name() -> str:
    """One run, one corridor — named for it."""
    from prefect.runtime import flow_run

    p = flow_run.parameters
    return f"{p.get('project', '?')}/{p.get('edge', '?')}"


# A corridor takes about four minutes. This is the ceiling before the run is
# called stuck rather than slow — generous, because a long corridor with close
# spacing is legitimately several times the typical one, and because the retry
# above gets a clean sim to try again in.
CAPTURE_TIMEOUT_S = 20 * 60


@task(name="capture", retries=1)
def photograph(project: str, map_name: str, edge: str, spacing: float,
               zigzag: float = 0.0) -> dict:
    """Walk the corridor in sim, writing panoramas and nothing else."""
    logger = get_run_logger()
    try:
        proc = subprocess.run(
            ["/app/capture.sh", project, map_name, edge, str(spacing)]
            + ([str(zigzag)] if zigzag else []),
            capture_output=True, text=True, timeout=CAPTURE_TIMEOUT_S)
    except subprocess.TimeoutExpired as err:
        # capture.sh's own trap does not run when it is killed from outside, so
        # clear the sim and the bridge here — otherwise the survivors spin on a
        # CPU and starve every capture that follows.
        subprocess.run(["pkill", "-f", "ruby.*gz sim"], check=False)
        subprocess.run(["pkill", "-f", "ros_gz_bridge/parameter_bridge"], check=False)
        raise RuntimeError(
            f"capture of {project}/{edge} still running after "
            f"{CAPTURE_TIMEOUT_S // 60} min — killed") from err
    walked = spacing
    for line in (proc.stdout + proc.stderr).splitlines():
        if line.startswith("SPACING "):
            walked = float(line.split()[1])
            continue
        logger.info("%s", line)
    if proc.returncode != 0:
        raise RuntimeError(f"capture failed for {project}/{edge}")
    out = PROJECTS / project / "panos" / edge
    shots = sorted(p.name for p in out.glob("*.png"))
    if len(shots) < 3:
        raise RuntimeError(f"only {len(shots)} panorama(s) in {out}; need 3+")
    # A corridor's length is rarely a whole number of strides, so the interval
    # actually walked differs a little from the one asked for. It is what makes
    # the reconstruction metric, so it is reported rather than assumed.
    logger.info("%d panoramas, walked %.3f m apart -> "
                "reconstruct with: just generate %s %.3f",
                len(shots), walked, edge, walked)
    return {"panos": f"{project}/panos/{edge}", "count": len(shots),
            "spacing_m": round(walked, 4), "first": shots[0], "last": shots[-1]}


@flow(name="capture-edge", log_prints=True, flow_run_name=_capture_name)
def capture_edge(project: str, edge: str, spacing: float = 0.5,
                 zigzag: float = 0.0,
                 map: str = "") -> dict:
    """Photograph one corridor. One run, one corridor, one folder of images.

    The output is a folder of numbered equirectangular panoramas — the same
    thing a person hands over after walking it with a 360 camera. Nothing about
    where the camera stood is written down, so the pipeline downstream gets no
    help it would not have from a real capture.
    """
    resolved = resolve_map(project, map)
    return photograph(project, resolved, edge, spacing, zigzag)


if __name__ == "__main__":
    # one at a time apiece: build-world rewrites the world directory in place,
    # and a capture boots its own Gazebo
    # Captures run several at a time: each brings up its own Gazebo on its own
    # partition and its own ROS domain, so they cannot see each other. World
    # generation stays alone — it rewrites the very files a capture reads.
    #
    # `limit` is the runner's own cap and overrides the per-deployment ones
    # beneath it; leaving it at 1 serialises everything whatever they say.
    serve(build_world.to_deployment(name="dreamworld", concurrency_limit=1),
          capture_edge.to_deployment(name="dreamworld",
                                     concurrency_limit=CAPTURES),
          limit=CAPTURES)
