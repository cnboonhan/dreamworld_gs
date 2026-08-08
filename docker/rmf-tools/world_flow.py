"""build-world — a floorplan becomes a simulable building, as a job.

Runs in the rmf-tools image because only it has Gazebo and
rmf_building_map_tools; the splat generator serves its own flows from its own
image. Both talk to the same Prefect server, so every operation in this repo
shows up in one place at :4200.

Three stages:

  1. generate   building.yaml -> world + models + nav graph  (shells the
                generator, which is a ROS entrypoint, not a library)
  2. inspect    read the nav graph back: levels, waypoints, lanes, doors
  3. publish    a Prefect artifact table of what the building actually contains

Stage 2 is the point of running this as a job rather than a script: the nav
graph is the contract the splat side keys against, so what it holds is worth
recording per run.

  python submit.py build-world/dreamworld project=<p> [map=<m>]
"""

from __future__ import annotations

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


@task(name="3. publish")
def publish(project: str, map_name: str, rows: list[dict]) -> dict:
    create_table_artifact(
        key=f"nav-graph-{project}-{map_name}".lower().replace("_", "-"),
        table=rows,
        description=f"Nav graph for {project}/{map_name} — the waypoints and "
                    f"lanes a capture is indexed against",
    )
    return {"world": f"{project}/worlds/{map_name}/{map_name}.world",
            "nav_graph": f"{project}/worlds/{map_name}/nav_graphs/0.yaml",
            "levels": [r["level"] for r in rows],
            "waypoints": sum(r["waypoints"] for r in rows),
            "lanes": sum(r["lanes"] for r in rows)}


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
    return publish(project, resolved, rows)


if __name__ == "__main__":
    # one at a time: the generator rewrites the world directory in place
    serve(build_world.to_deployment(name="dreamworld", concurrency_limit=1),
          limit=1)
