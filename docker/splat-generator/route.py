"""plan-route — the walk from one waypoint to another, as data.

The nav graph says how to get from A to B; the splats say what it looks like.
This writes down the join: the path through the building in metres, and which
splat covers which stretch of it.

  1. route     Dijkstra over the nav graph -> the waypoints you pass through
  2. resolve   each hop -> the corridor splat covering it (names what is missing)
  3. write     the polyline, and the span of the tour each splat covers

Nothing is rendered. The viewer walks the polyline with its tour parameter and
loads the splat whose span it is currently in, so the route is a small json and
the motion happens live.

The direction through each splat is a dot product, not a rule: a corridor
captured `a -> b` and walked `b -> a` is simply traversed with `reverse` set.

  python submit.py plan-route/dreamworld project=<p> start=<w> goal=<w>
"""

from __future__ import annotations

import heapq
import json
from pathlib import Path

import numpy as np
from prefect import flow, get_run_logger, task

PROJECTS = Path("/workspace/projects")
# one polyline point every this many metres; the viewer interpolates between
POINT_SPACING_M = 0.25


def load_plan(project: str) -> dict:
    plans = sorted((PROJECTS / project / "worlds").glob("*/capture_plan.json"))
    if not plans:
        raise RuntimeError(f"no capture plan for {project} — run: just world")
    return json.loads(plans[0].read_text())


def graph(plan: dict) -> tuple[dict, dict, dict]:
    """(adjacency, edge lookup, vertex positions) keyed by vertex id.

    Levels are independent in an RMF nav graph — a lift is not a lane — so
    there is no rung between floors, and a cross-level route is refused rather
    than silently wrong."""
    adj: dict[str, list[str]] = {}
    edges: dict[tuple[str, str], dict] = {}
    pos: dict[str, np.ndarray] = {}
    for data in plan["levels"].values():
        for v in data["vertices"]:
            pos[v["id"]] = np.array([v["x"], v["y"], 0.0])
            adj.setdefault(v["id"], [])
        for e in data["edges"]:
            a, b = e["a"], e["b"]
            adj.setdefault(a, []).append(b)
            adj.setdefault(b, []).append(a)
            edges[(a, b)] = edges[(b, a)] = e
    return adj, edges, pos


def find_vertex(plan: dict, key: str) -> str:
    """A full id (L11.cafe) or a bare waypoint name (cafe)."""
    ids = [v["id"] for d in plan["levels"].values() for v in d["vertices"]]
    if key in ids:
        return key
    hits = [i for i in ids if i.split(".", 1)[-1] == key]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        named = sorted(i for i in ids
                       if not i.split(".", 1)[-1].startswith("v"))
        raise RuntimeError(f"no waypoint '{key}'. Named ones: {', '.join(named)}")
    raise RuntimeError(f"'{key}' is ambiguous across levels: {', '.join(hits)}")


def dijkstra(adj: dict, edges: dict, s: str, g: str) -> list[str]:
    dist, prev, pq = {s: 0.0}, {}, [(0.0, s)]
    while pq:
        d, u = heapq.heappop(pq)
        if u == g:
            break
        if d > dist.get(u, 1e18):
            continue
        for w in adj.get(u, ()):
            nd = d + edges[(u, w)]["length_m"]
            if nd < dist.get(w, 1e18):
                dist[w], prev[w] = nd, u
                heapq.heappush(pq, (nd, w))
    if g != s and g not in prev:
        raise RuntimeError(
            f"no route {s} -> {g}. Levels are separate graphs, so crossing "
            f"floors needs the lift, which is not modelled yet.")
    path = [g]
    while path[-1] != s:
        path.append(prev[path[-1]])
    return list(reversed(path))


@task(name="1. route")
def route(project: str, start: str, goal: str) -> dict:
    logger = get_run_logger()
    plan = load_plan(project)
    adj, edges, pos = graph(plan)
    s, g = find_vertex(plan, start), find_vertex(plan, goal)
    path = dijkstra(adj, edges, s, g)

    hops = []
    for u, w in zip(path, path[1:]):
        e = edges[(u, w)]
        hops.append({"edge": e["id"], "from": u, "to": w,
                     # the id sorts its endpoints, so walking the other way
                     # means playing that splat backwards
                     "reverse": u != e["a"],
                     "length_m": e["length_m"],
                     "door": e.get("door", "")})
    total = sum(h["length_m"] for h in hops)
    logger.info("%s -> %s: %d waypoints, %d hop(s), %.1f m",
                s, g, len(path), len(hops), total)
    for h in hops:
        logger.info("  %s -> %s via %s%s%s", h["from"], h["to"], h["edge"],
                    "  (reversed)" if h["reverse"] else "",
                    f"  door: {h['door']}" if h["door"] else "")
    return {"path": path, "hops": hops, "metres": round(total, 2),
            "pos": {k: v.tolist() for k, v in pos.items()}}


@task(name="2. resolve")
def resolve(project: str, plan: dict) -> dict:
    """Point each hop at its splat, and say plainly what is missing."""
    logger = get_run_logger()
    root = PROJECTS / project / "splats"
    missing = [h["edge"] for h in plan["hops"]
               if not (root / h["edge"] / "world.ply").is_file()]
    if missing:
        raise RuntimeError(
            "no splat for " + ", ".join(missing) + ".\n"
            "Photograph each corridor and reconstruct it — "
            "`just capture <id>` then `just generate <id>`; "
            "`just plan missing` lists them.")
    unaligned = []
    for h in plan["hops"]:
        info = root / h["edge"] / "world.info.json"
        try:
            if not json.loads(info.read_text()).get("aligned"):
                unaligned.append(h["edge"])
        except (OSError, ValueError):
            unaligned.append(h["edge"])
    if unaligned:
        # they would each sit in their own frame, so the walk would teleport
        raise RuntimeError(
            "not placed in the building: " + ", ".join(unaligned) + ".\n"
            "Rebuild them so the align stage runs — a splat in its own COLMAP "
            "frame cannot be walked into from the one before it.")
    logger.info("all %d hop(s) have a splat, all placed in the building",
                len(plan["hops"]))
    return plan


def walk_height(project: str, edge: str) -> float:
    """The height the camera actually walked this corridor at.

    A nav graph is a floorplan: its vertices carry x and y, and the level's
    elevation is not in it at all. The splat's own path is, because it was
    written from where the capture stood — so the route takes its height from
    the very thing it is about to show. Without this the polyline sits on z=0
    and the camera rides along under the floor of every upper level.
    """
    f = PROJECTS / project / "splats" / edge / "world.path.json"
    pts = json.loads(f.read_text())["points"]
    return float(np.median([p[2] for p in pts]))


@task(name="3. write")
def write(project: str, plan: dict, out: str) -> dict:
    """The polyline, and which splat covers which span of the tour."""
    logger = get_run_logger()
    pos = {k: np.array(v) for k, v in plan["pos"].items()}
    hops, total = plan["hops"], sum(h["length_m"] for h in plan["hops"]) or 1.0

    # a waypoint sits at the height of the corridors meeting it, so a route
    # that does change level ramps between them rather than stepping
    heights: dict[str, list[float]] = {}
    for h in hops:
        try:
            z = walk_height(project, h["edge"])
        except (OSError, ValueError, KeyError) as err:
            raise RuntimeError(
                f"{h['edge']} has no walk to take its height from ({err}). "
                f"Rebuild it: `just generate {h['edge']}`.") from err
        heights.setdefault(h["from"], []).append(z)
        heights.setdefault(h["to"], []).append(z)
    z_of = {k: sum(v) / len(v) for k, v in heights.items()}

    points: list[list[float]] = []
    segments = []
    doors = []
    travelled = 0.0
    for h in hops:
        a, b = pos[h["from"]].copy(), pos[h["to"]].copy()
        a[2], b[2] = z_of[h["from"]], z_of[h["to"]]
        t0 = travelled / total
        travelled += h["length_m"]
        t1 = travelled / total
        n = max(2, int(round(h["length_m"] / POINT_SPACING_M)))
        # skip the first point after the first hop: it is the previous hop's
        # last, and a duplicate would stall the camera for a frame
        for i in range(0 if not points else 1, n + 1):
            p = a + (b - a) * (i / n)
            points.append([round(float(v), 4) for v in p])
        segments.append({"edge": h["edge"],
                         "splat": f"splats/{h['edge']}/world.ply",
                         "from": h["from"], "to": h["to"],
                         "reverse": h["reverse"],
                         "t_start": round(t0, 6), "t_end": round(t1, 6)})
        if h["door"]:
            doors.append({"name": h["door"], "t": round((t0 + t1) / 2, 6)})

    doc = {"project": project, "waypoints": plan["path"],
           "metres": plan["metres"],
           # the building's up; splats are placed in this frame
           "up": [0.0, 0.0, 1.0],
           "points": points, "segments": segments, "doors": doors}
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(doc, indent=1))
    logger.info("%d point(s) over %d segment(s), %d door(s), eye height "
                "%.2f-%.2f m -> %s", len(points), len(segments), len(doors),
                min(z_of.values()), max(z_of.values()), out)
    return {"route": out, "points": len(points), "segments": len(segments),
            "metres": plan["metres"], "waypoints": plan["path"]}


def _run_name() -> str:
    from prefect.runtime import flow_run

    p = flow_run.parameters
    return f"{p.get('project', '?')}: {p.get('start', '?')} -> {p.get('goal', '?')}"


@flow(name="plan-route", log_prints=True, flow_run_name=_run_name)
def plan_route(project: str, start: str, goal: str, out: str = "") -> dict:
    """Walk start -> goal, as a route the viewer can stream splats along."""
    planned = route(project, start, goal)
    checked = resolve(project, planned)
    name = f"{checked['path'][0]}__{checked['path'][-1]}.route.json"
    return write(project, checked,
                 out or str(PROJECTS / project / "traversals" / name))
