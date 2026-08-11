"""Walk a route corridor by corridor, each splat in its own coordinates.

A generated world sits in whatever frame HunyuanWorld gave it, and no two
agree. That does not have to be reconciled to traverse them: only one corridor
is on screen at a time, its walk is already the lane it was built from, and the
route says which way through it. So each hop is rendered in its own frame and
the hops are joined end to end.

The turn at each junction is real, not a cut. The next hop's heading is known
in building metres, and this world's own metres-to-world map — the one that
placed its walk — carries that heading into the frame the camera is standing
in. Nothing is moved to find it. The angles that come out match the nav graph's
to within a degree, which is a free check on that map, since nothing here knows
the building.

What this cannot do is show two corridors at once, which is the only way to see
whether they meet at a vertex without a step. That needs them in one frame.

    python route_video.py <project> <start> <goal> <out.mp4>
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from collections import deque
from pathlib import Path

import cv2
import numpy as np
import torch
from gsplat import rasterization

sys.path.insert(0, str(Path(__file__).parent))
import make_spawn_cam as msc  # noqa: E402
import pick_panorama  # noqa: E402
import render_video as rv  # noqa: E402

PROJECTS = Path("/workspace/projects")
# how long a right-angle turn takes. A turn is part of walking a route and
# reads as motion; a cut with no turn reads as a mistake.
TURN_S = 1.2


def route(plan: dict, start: str, goal: str) -> list[str]:
    """The waypoints from start to goal, fewest hops."""
    adj: dict[str, list[str]] = {}
    for data in plan["levels"].values():
        for e in data["edges"]:
            adj.setdefault(e["a"], []).append(e["b"])
            adj.setdefault(e["b"], []).append(e["a"])
    prev, q = {start: None}, deque([start])
    while q:
        u = q.popleft()
        for v in adj.get(u, []):
            if v not in prev:
                prev[v] = u
                q.append(v)
    if goal not in prev:
        raise SystemExit(f"no route {start} -> {goal}")
    path, c = [], goal
    while c is not None:
        path.append(c)
        c = prev[c]
    return path[::-1]


def turn_frames(here: np.ndarray, want: np.ndarray) -> int:
    sweep = math.acos(float(np.clip(here @ want, -1, 1)))
    return int(TURN_S * rv.FPS * min(1.0, sweep / (math.pi / 2))), sweep


def main() -> None:
    project, start, goal, out = sys.argv[1], sys.argv[2], sys.argv[3], Path(sys.argv[4])
    root = PROJECTS / project
    plan = json.loads(next(root.glob("worlds/*/capture_plan.json")).read_text())
    edges = {}
    verts = {}
    for data in plan["levels"].values():
        for v in data["vertices"]:
            verts[v["id"]] = np.array([v["x"], v["y"], 0.0])
        for e in data["edges"]:
            edges[(e["a"], e["b"])] = edges[(e["b"], e["a"])] = e

    path = route(plan, start, goal)
    # the walk sidecar runs the way the id names; this route may not
    hops = [(edges[(a, b)]["id"], edges[(a, b)]["b"] == a)
            for a, b in zip(path, path[1:])]
    heading = [msc.unit(verts[b] - verts[a]) for a, b in zip(path, path[1:])]
    print(f"{start} -> {goal}: {len(hops)} hops")

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".raw.mp4")
    writer = cv2.VideoWriter(str(tmp), cv2.VideoWriter_fourcc(*"mp4v"), rv.FPS,
                             (rv.WIDTH, rv.HEIGHT))
    f = 0.5 * rv.WIDTH / np.tan(np.radians(rv.FOV) * 0.5)
    K = torch.tensor([[[f, 0, rv.WIDTH / 2], [0, f, rv.HEIGHT / 2], [0, 0, 1]]],
                     dtype=torch.float32, device="cuda:0")
    total = 0

    for i, (edge, reverse) in enumerate(hops, 1):
        scene = root / "splats" / f"{edge}@world"
        doc = json.loads((scene / "world.path.json").read_text())
        pts = np.asarray(doc["points"], dtype=np.float64)
        if reverse:
            pts = pts[::-1]
        up = np.asarray(doc["up"], dtype=np.float64)
        # One pace for the whole walk. render_video floors a clip at six
        # seconds so a one-metre corridor is watchable alone; inside a
        # traversal that floor crawls the short hops and strides the long.
        n = max(15, int(doc["length_m"] / rv.WALK_MS * rv.FPS))
        eyes, targets = rv.along_polyline(pts, up, n)
        splat = rv.load_splats([scene / "world.ply"], "cuda:0")
        quats = torch.nn.functional.normalize(splat["quats"], dim=1)
        label = f"{i}/{len(hops)}  {edge}{'  (reversed)' if reverse else ''}"

        def shoot(eye, target):
            vm = torch.tensor(rv.look_at(eye, target, up), dtype=torch.float32,
                              device="cuda:0")
            with torch.no_grad():
                rgb, _, _ = rasterization(
                    splat["means"], quats, splat["scales"], splat["opacities"],
                    splat["colors"], vm[None], K, rv.WIDTH, rv.HEIGHT,
                    near_plane=0.01, rasterize_mode="classic")
            frame = cv2.cvtColor((rgb[0].clamp(0, 1) * 255).byte().cpu().numpy(),
                                 cv2.COLOR_RGB2BGR)
            for colour, thick in (((0, 0, 0), 4), ((255, 255, 255), 1)):
                cv2.putText(frame, label, (18, rv.HEIGHT - 22),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, colour, thick, cv2.LINE_AA)
            writer.write(frame)

        for eye, tgt in zip(eyes, targets):
            shoot(eye, tgt)

        turned, sweep = 0, 0.0
        if i < len(hops):
            panos = root / "panos" / edge
            xyz = {s["image"]: np.asarray(s["xyz"], dtype=np.float64)
                   for s in json.loads((panos / "poses.json").read_text())["standpoints"]}
            chosen = pick_panorama.pick(panos)
            M = msc.to_building(scene, panos, chosen, xyz[chosen])[:, :3]
            here = msc.unit(pts[-1] - pts[-2])
            want = msc.unit(M @ heading[i])
            want = msc.unit(want - up * (want @ up))       # level, like the walk
            turned, sweep = turn_frames(here, want)
            axis = np.cross(here, want)
            axis = msc.unit(axis) if np.linalg.norm(axis) > 1e-6 else up
            for k in range(turned):
                a = sweep * (k + 1) / turned
                d = (here * math.cos(a) + np.cross(axis, here) * math.sin(a)
                     + axis * (axis @ here) * (1 - math.cos(a)))
                shoot(pts[-1], pts[-1] + d)

        total += n + turned
        print(f"  {label}  {doc['length_m']:.2f} m, {n} frames"
              + (f" + {turned} turning {math.degrees(sweep):.0f} deg" if turned else ""),
              flush=True)
        del splat
        torch.cuda.empty_cache()
    writer.release()

    rv.encode(tmp, out)
    print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB, {total} frames, "
          f"{total / rv.FPS:.0f} s)")


if __name__ == "__main__":
    main()
