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
                (one panorama per waypoint — that is what `just plan` reads)

Stages 2 and 3 are the point of running this as a job rather than a script: the
nav graph is the contract the splat side keys against, so what it holds — and
what still needs photographing — is worth recording per run.

  python submit.py build-world/dreamworld project=<p> [map=<m>]

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

    Vertices are what you photograph; edges are what you walk between them.

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
            # One panorama per waypoint, as a file rather than a folder: a
            # world is reconstructed from a single vantage point, so standing
            # at a place and shooting once is the whole capture. The
            # corridors between them are then walks across worlds, not
            # captures of their own.
            capture.append({"kind": "vertex", "id": vid, "level": level,
                            "pano": f"panos/{vid}", "splat": f"splats/{vid}"})

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
        table=[{"id": c["id"], "level": c["level"],
                "panorama goes in": f"{c['pano']}.jpg"} for c in capture],
        description=f"Where to photograph {project}/{map_name}: one panorama "
                    f"per waypoint",
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


if __name__ == "__main__":
    # One at a time: build-world rewrites the world directory in place.
    serve(build_world.to_deployment(name="dreamworld", concurrency_limit=1),
          limit=1)
