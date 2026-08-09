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


def walk_of(project: str, edge: str, reverse: bool) -> np.ndarray:
    """The corridor's recorded walk, in the direction this route travels it.

    Not the lane. A lane is a routing abstraction — a straight line between two
    nav-graph vertices, with no height, that nobody walked. The splat was built
    around the walk, and the walk is the only place its geometry was ever
    observed: a camera a third of a metre to the side is looking at surfaces
    from a viewpoint no panorama covered, which is exactly where a gaussian
    splat frays. Measured on the sample building, riding the lane put the
    camera 0.35 m on average from the nearest place a panorama was shot, and
    never closer than 0.30 m.
    """
    f = PROJECTS / project / "splats" / edge / "world.path.json"
    pts = np.asarray(json.loads(f.read_text())["points"], dtype=np.float64)
    return pts[::-1] if reverse else pts


def resample(poly: np.ndarray, spacing: float):
    """Evenly spaced points along a polyline, plus each input point's arc.

    The viewer walks its path uniformly in point *index*, so the points have to
    be uniform in distance — otherwise the camera races through a finely
    sampled stretch and crawls over a coarse one.
    """
    step = np.linalg.norm(np.diff(poly, axis=0), axis=1)
    s = np.concatenate([[0.0], np.cumsum(step)])
    n = max(2, int(round(float(s[-1]) / spacing)) + 1)
    u = np.linspace(0.0, float(s[-1]), n)
    return np.stack([np.interp(u, s, poly[:, i]) for i in range(3)], 1), s


@task(name="3. write")
def write(project: str, plan: dict, out: str) -> dict:
    """The polyline, and which splat covers which span of the tour."""
    logger = get_run_logger()
    hops = plan["hops"]

    walks = []
    for h in hops:
        try:
            walks.append(walk_of(project, h["edge"], h["reverse"]))
        except (OSError, ValueError, KeyError) as err:
            raise RuntimeError(
                f"{h['edge']} has no recorded walk ({err}). "
                f"Rebuild it: `just generate {h['edge']}`.") from err

    # One polyline through every walk in turn. Consecutive walks do not meet:
    # each ends wherever that capture's last stop happened to be, and stops
    # weave across the lane rather than landing on the vertex. So a short
    # straight bridge crosses each junction — the only stretch of a route no
    # camera stood on, which is why its length is reported.
    poly = np.concatenate(walks)
    full, arc = resample(poly, POINT_SPACING_M)
    at = np.cumsum([0] + [len(w) for w in walks])
    bounds = [(float(arc[at[i]]), float(arc[at[i + 1] - 1])) for i in range(len(walks))]
    total = float(arc[-1]) or 1.0
    gaps = [float(np.linalg.norm(walks[i + 1][0] - walks[i][-1]))
            for i in range(len(walks) - 1)]

    segments, doors = [], []
    for h, (s0, s1) in zip(hops, bounds):
        segments.append({"edge": h["edge"],
                         "splat": f"splats/{h['edge']}/world.ply",
                         "from": h["from"], "to": h["to"],
                         "reverse": h["reverse"],
                         "t_start": round(s0 / total, 6),
                         "t_end": round(s1 / total, 6)})
        if h["door"]:
            doors.append({"name": h["door"],
                          "t": round((s0 + s1) / 2 / total, 6)})

    # Each bridge is split between the splats on either side, so the spans
    # tile [0, 1] with no gap for the viewer to fall into.
    segments[0]["t_start"] = 0.0
    segments[-1]["t_end"] = 1.0
    for i in range(len(segments) - 1):
        mid = round((segments[i]["t_end"] + segments[i + 1]["t_start"]) / 2, 6)
        segments[i]["t_end"] = segments[i + 1]["t_start"] = mid

    metres = round(total, 2)
    doc = {"project": project, "waypoints": plan["path"],
           "metres": metres,
           # the building's up; splats are placed in this frame
           "up": [0.0, 0.0, 1.0],
           "points": [[round(float(v), 4) for v in p] for p in full],
           "segments": segments, "doors": doors}
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(doc, indent=1))
    logger.info("%d point(s) over %d segment(s), %d door(s); %.2f m walked, "
                "%.2f m of it bridging %d junction(s) -> %s",
                len(full), len(segments), len(doors), metres, sum(gaps),
                len(gaps), out)
    return {"route": out, "points": len(full), "segments": len(segments),
            "metres": metres, "bridged_m": round(sum(gaps), 2),
            "waypoints": plan["path"]}


def _run_name() -> str:
    from prefect.runtime import flow_run

    p = flow_run.parameters
    return f"{p.get('project', '?')}: {p.get('start', '?')} -> {p.get('goal', '?')}"


@flow(name="plan-route", log_prints=True, flow_run_name=_run_name)
def plan_route(project: str, start: str, goal: str) -> dict:
    """Walk start -> goal, as a route the viewer can stream splats along."""
    planned = route(project, start, goal)
    checked = resolve(project, planned)
    name = f"{checked['path'][0]}__{checked['path'][-1]}.route.json"
    return write(project, checked, str(PROJECTS / project / "traversals" / name))
