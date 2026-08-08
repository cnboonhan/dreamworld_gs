#!/usr/bin/env python3
"""Photograph one corridor of the simulated building, as if walking it.

Stands a camera at each of K standpoints along a lane and captures a full 360
panorama at each, the way someone walks a corridor stopping every half metre
with a 360 camera.

Each panorama is photosphere-stitched: a grid of perspective views over yaw and
pitch, reprojected into an equirectangular canvas using the camera's known pose
and intrinsics. Because the poses are exact this needs no feature matching, so
it works on flat sim walls that no feature detector would survive.

Alongside the panoramas it writes `poses.json`: where the camera actually
stood, in building metres. A real 360 capture cannot supply that, so the splat
pipeline still runs structure-from-motion when it is absent — but when a
simulated walk is the input there is no reason to re-derive by inference what
we already know exactly, and an empty corridor of flat planes is close to the
worst case for inferring it. Known poses make a simulated capture reconstruct
correctly by construction, which is what makes it useful for developing
everything downstream.

Needs a running sim with the rec_cam model, its image topic, and the
world's set_pose service bridged into ROS. capture.sh arranges that.

Alongside each panorama it also writes `NNN.range.npy`: how far away the
surface is along every ray of that sphere, straight from the simulator's depth
camera. A real 360 capture supplies nothing of the kind — this is the ground
truth the simulator happens to have, and the simulated pipeline uses it to
start gaussian splatting from the actual surfaces. Without it a corridor
reconstructs as soup, because every camera centre sits on one walked line and
depth along a ray is then almost free to be wrong.

Ported from the dreamworld pipeline's panorama_gz stage.
"""

import argparse
import hashlib
import json
import math
import os
import sys
import threading
import time

import numpy as np
import rclpy
from geometry_msgs.msg import Point, Pose, Quaternion
from rclpy.node import Node
from ros_gz_interfaces.msg import Entity
from ros_gz_interfaces.srv import SetEntityPose
from sensor_msgs.msg import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "common"))
from geometry import quat, quat_to_R  # noqa: E402
from png_io import write_png  # noqa: E402

ROS_TOPIC = "/rec_cam"
DEPTH_TOPIC = "/rec_depth"


class Cam(Node):
    def __init__(self, world, name):
        super().__init__("capture")
        self.name = name
        self.latest = None
        self.count = 0            # frames seen, so a fresh one can be waited for
        self.depth = None
        self.depth_count = 0
        self.create_subscription(Image, ROS_TOPIC, self._cb, 10)
        self.create_subscription(Image, DEPTH_TOPIC, self._depth_cb, 10)
        self.cli = self.create_client(SetEntityPose, f"/world/{world}/set_pose")

    def _cb(self, msg):
        self.latest = msg
        self.count += 1

    def _depth_cb(self, msg):
        self.depth = msg
        self.depth_count += 1

    def wait_fresh(self, n, timeout):
        """Block until n new frames arrive on both topics. A fixed sleep alone
        grabs frames still in flight from the previous pose, which reproject as
        ghostly overlapping domes."""
        start, dstart, t0 = self.count, self.depth_count, time.time()
        while time.time() - t0 < timeout:
            if self.count - start >= n and (self.depth is None
                                            or self.depth_count - dstart >= n):
                break
            time.sleep(0.005)
        return self.latest, self.depth

    def set_pose(self, x, y, z, yaw, pitch, timeout=2.0):
        qx, qy, qz, qw = quat(yaw, pitch)
        req = SetEntityPose.Request()
        req.entity = Entity(name=self.name, type=Entity.MODEL)
        req.pose = Pose(position=Point(x=x, y=y, z=z),
                        orientation=Quaternion(x=qx, y=qy, z=qz, w=qw))
        fut = self.cli.call_async(req)
        t0 = time.time()
        while not fut.done() and time.time() - t0 < timeout:
            time.sleep(0.001)
        return fut.done()


def frame_rgb(msg):
    """sensor_msgs/Image -> (H, W, 3) uint8 RGB, stripping any row padding."""
    row = msg.width * 3
    buf = bytes(msg.data)
    if msg.step != row:
        buf = b"".join(buf[i * msg.step:i * msg.step + row]
                       for i in range(msg.height))
    arr = np.frombuffer(buf, dtype=np.uint8).reshape(msg.height, msg.width, 3)
    return arr[:, :, ::-1] if msg.encoding == "bgr8" else arr


def frame_depth(msg):
    """sensor_msgs/Image -> (H, W) float32 of depth along the optical axis.

    Gazebo publishes 32FC1, and misses are +inf rather than a sentinel."""
    if msg is None:
        return None
    arr = np.frombuffer(bytes(msg.data), dtype=np.float32)
    arr = arr[:msg.height * msg.width].reshape(msg.height, msg.width)
    return np.where(np.isfinite(arr), arr, 0.0)


def panorama(node, x, y, a, out, label):
    """Capture the yaw x pitch grid at (x, y) and write one equirect PNG."""
    pitches = (np.linspace(-1, 1, a.pitch_steps) * math.radians(72)
               if a.pitch_steps > 1 else np.array([0.0]))
    yaws = np.arange(a.yaw_steps) * (2 * math.pi / a.yaw_steps)
    views = [(yaw, pitch) for pitch in pitches for yaw in yaws]
    print(f"  {label} at ({x:.2f},{y:.2f}): {len(views)} views "
          f"({a.yaw_steps} yaw x {a.pitch_steps} pitch)", flush=True)

    shots = []
    for yaw, pitch in views:
        node.set_pose(x, y, a.height, float(yaw), float(pitch))
        time.sleep(a.settle)
        frame, dmsg = node.wait_fresh(2, a.frame_timeout)
        shots.append((frame_rgb(frame).astype(np.float32),
                      quat_to_R(quat(float(yaw), float(pitch))),
                      frame_depth(dmsg)))

    H, W = shots[0][0].shape[:2]
    fx = (W / 2) / math.tan(a.fov / 2)          # square pixels, so fy = fx
    cxp, cyp = W / 2.0, H / 2.0

    # equirect grid -> world ray directions; even width keeps it exactly 2:1
    Wc = a.width - (a.width % 2)
    Hc = Wc // 2
    lon = (np.arange(Wc) + 0.5) / Wc * 2 * math.pi - math.pi
    lat = math.pi / 2 - (np.arange(Hc) + 0.5) / Hc * math.pi
    lon_g, lat_g = np.meshgrid(lon, lat)
    cl = np.cos(lat_g)
    d = np.stack([cl * np.cos(lon_g), cl * np.sin(lon_g), np.sin(lat_g)], -1)

    acc = np.zeros((Hc, Wc, 3), np.float32)
    wsum = np.zeros((Hc, Wc), np.float32)
    # the range map is picked, not blended: averaging two views' depths across
    # a seam invents a surface between them that neither one saw
    rng_map = np.zeros((Hc, Wc), np.float32)
    rng_w = np.zeros((Hc, Wc), np.float32)
    for frame, R, dep in shots:
        fwd, left, up = R[:, 0], R[:, 1], R[:, 2]   # gz: +X fwd, +Z up
        depth = d @ fwd
        rightc = -(d @ left)                        # image right is -Y
        upc = d @ up
        front = depth > 1e-6
        dd = np.where(front, depth, 1.0)
        ui = np.round(cxp + fx * (rightc / dd)).astype(np.int32)
        vi = np.round(cyp - fx * (upc / dd)).astype(np.int32)
        inb = front & (ui >= 0) & (ui < W) & (vi >= 0) & (vi < H)
        vic, uic = np.clip(vi, 0, H - 1), np.clip(ui, 0, W - 1)
        # feather toward each view's centre so the overlaps blend
        wgt = np.where(inb, np.clip(depth, 0, 1) ** 4, 0.0).astype(np.float32)
        acc += wgt[..., None] * frame[vic, uic]
        wsum += wgt
        if dep is not None:
            # the sensor reports depth along its own axis; the equirect wants
            # the range along this ray, and dd is the cosine between them
            r = dep[vic, uic] / np.maximum(dd, 1e-6)
            take = inb & (dep[vic, uic] > 1e-3) & (wgt > rng_w)
            rng_map = np.where(take, r, rng_map)
            rng_w = np.where(take, wgt, rng_w)

    pano = (acc / np.maximum(wsum, 1e-6)[..., None]).clip(0, 255).astype(np.uint8)
    write_png(out, np.ascontiguousarray(pano))
    if rng_w.any():
        # float16 halves the file and is far finer than the geometry it seeds
        np.save(out.replace(".png", ".range.npy"), rng_map.astype(np.float16))
        seen = rng_map[rng_map > 0]
        print(f"  -> {os.path.basename(out)} ({Wc}x{Hc}), range "
              f"{seen.min():.2f}-{seen.max():.2f} m over "
              f"{100 * (rng_map > 0).mean():.0f}% of the sphere", flush=True)
    else:
        print(f"  -> {os.path.basename(out)} ({Wc}x{Hc})", flush=True)


# How far the walk weaves across the corridor, in metres.
#
# This was 0.35 m, for structure from motion. Walking a straight line is the
# worst baseline for the surfaces you walk toward — consecutive standpoints
# move *along* the line of sight, so the far wall barely shifts and its depth
# is weakly constrained. On a straight 2.2 m walk COLMAP scattered the twelve
# views of one panorama by 0.62 m, more than the 0.549 m between standpoints,
# and the recovered walk folded to 0.28 m. Weaving broke that.
#
# A simulated capture no longer runs structure from motion: its poses are
# recorded and its geometry is seeded from the depth camera, so the workaround
# outlived its reason — and it had costs. Swaying 0.7 m across every 0.5 m
# forward makes the walked path 1.7x the corridor, which is nobody's gait, and
# it is the path the viewer rides, since that is the only line the splat was
# observed from. So this is now the sway of ordinary walking.
#
# It stays a parameter: a real 360 capture still goes through SfM, and if one
# is ever simulated for that path it wants the wide weave back.
ZIGZAG_M = 0.10
# and on top of that, a little untidiness: nobody's stride is exact
JITTER_M = 0.06


def standpoints(plan, edge_id, spacing, seed=0, zigzag=ZIGZAG_M):
    """Stops roughly `spacing` metres apart along the lane, endpoints included.

    Spacing is chosen, not derived: a person walks a corridor stopping about
    every half metre, whatever the corridor's length. Dividing the lane into a
    fixed number of stops instead would make the interval depend on the lane,
    and the reconstruction is rescaled to metres using the interval you say you
    walked — so a capture whose real interval is 0.55 m, reconstructed as 0.5 m,
    comes out 9% small. Returns (points, actual_spacing).

    Endpoints included, so the first and last standpoints sit on the two
    vertices — which is what makes the corridor splat cover them. Interior
    stops wander a little, the way a person's do; the endpoints do not, because
    they are the vertices the corridor has to join at.
    """
    for data in plan["levels"].values():
        for e in data["edges"]:
            if e["id"] != edge_id:
                continue
            pos = {v["id"]: (v["x"], v["y"]) for v in data["vertices"]}
            ax, ay = pos[e["a"]]
            bx, by = pos[e["b"]]
            dx, dy = bx - ax, by - ay
            length = math.hypot(dx, dy) or 1.0
            # land exactly on both vertices, at the interval nearest the one
            # asked for; three stops minimum, or the walk axis is too weak
            n = max(3, int(round(length / spacing)) + 1)
            nx, ny = -dy / length, dx / length      # across the corridor
            rng = np.random.default_rng(seed)
            out = []
            for i in range(n):
                f = i / (n - 1)
                x, y = ax + dx * f, ay + dy * f
                # Every stop weaves, the endpoints included. Pinning them to
                # the lane was a mistake: measured, the standpoints that weave
                # collapse their twelve views onto a common centre to within
                # 3 mm, and the two pinned endpoints scattered by 0.67 m — so
                # the stops alignment depends on most were the least
                # constrained. Nobody stops on a mathematical vertex anyway.
                weave = zigzag * (1 if i % 2 else -1)
                along = rng.uniform(-JITTER_M, JITTER_M)
                across = weave + rng.uniform(-JITTER_M, JITTER_M)
                x += dx / length * along + nx * across
                y += dy / length * along + ny * across
                out.append((x, y))
            # The interval that matters is the one actually walked, corner to
            # corner — the weave makes each step longer than the along-lane
            # spacing, and it is the walked distance that sets metric scale.
            steps = [math.dist(out[i], out[i + 1]) for i in range(len(out) - 1)]
            return out, float(np.median(steps))
    raise SystemExit(f"no edge '{edge_id}' in the capture plan")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--plan", required=True, help="capture_plan.json")
    ap.add_argument("--edge", required=True, help="edge id, e.g. L11.cafe--v7")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--zigzag", type=float, default=ZIGZAG_M,
                    help="metres the walk weaves either side of the lane; 0 "
                         "walks straight, which reconstructs poorly")
    ap.add_argument("--spacing", type=float, default=0.5,
                    help="metres between stops; the count follows from the "
                         "corridor's length, as it does when walking")
    ap.add_argument("--world", default="sim_world")
    ap.add_argument("--name", default="rec_cam")
    ap.add_argument("--height", type=float, default=1.6)
    ap.add_argument("--fov", type=float, default=2.2,
                    help="must match the camera's horizontal_fov in the world")
    ap.add_argument("--width", type=int, default=2048,
                    help="equirect width; height is forced to width/2")
    ap.add_argument("--yaw-steps", type=int, default=12)
    ap.add_argument("--pitch-steps", type=int, default=5)
    ap.add_argument("--settle", type=float, default=0.15)
    ap.add_argument("--frame-timeout", type=float, default=5.0)
    a = ap.parse_args()

    plan = json.loads(open(a.plan).read())
    # seeded by the corridor, so a re-capture reproduces the same walk
    seed = int(hashlib.sha256(a.edge.encode()).hexdigest()[:8], 16)
    points, actual = standpoints(plan, a.edge, a.spacing, seed, a.zigzag)
    os.makedirs(a.out_dir, exist_ok=True)

    rclpy.init()
    node = Cam(a.world, a.name)
    threading.Thread(target=rclpy.spin, args=(node,), daemon=True).start()
    if not node.cli.wait_for_service(timeout_sec=30):
        raise SystemExit("set_pose service not bridged (is the sim up?)")
    t0 = time.time()
    while node.latest is None and time.time() - t0 < 30:
        time.sleep(0.05)
    if node.latest is None:
        raise SystemExit(f"no frames from {ROS_TOPIC}")

    base_height = a.height
    print(f"walking {a.edge}: {len(points)} standpoints "
          f"{actual:.3f} m apart", flush=True)
    rng = np.random.default_rng(seed ^ 0x9E37)
    heights = []
    for i, (x, y) in enumerate(points):
        # and nobody holds a 360 camera at exactly one height
        a.height = base_height + float(rng.uniform(-0.05, 0.05))
        heights.append(a.height)
        # zero-padded, so lexicographic order is walk order — the pipeline reads
        # direction of travel from filename order and nothing else
        out = os.path.join(a.out_dir, f"{i:03d}.png")
        panorama(node, x, y, a, out, f"{i + 1}/{len(points)}")

    # the interval actually walked, which is what makes the reconstruction
    # metric — reconstruct with: just generate <edge> <spacing>
    # where the camera stood, for the pipeline to use instead of inferring it
    poses = {
        "frame": "gz",           # +X forward, +Y left, +Z up; metres
        "note": "camera centres in building coordinates, one per panorama",
        "standpoints": [{"image": f"{i:03d}.png",
                         "xyz": [round(x, 6), round(y, 6), round(h, 6)]}
                        for i, ((x, y), h) in enumerate(zip(points, heights))],
    }
    with open(os.path.join(a.out_dir, "poses.json"), "w") as fh:
        json.dump(poses, fh, indent=1)
    print(f"wrote {len(points)} panoramas + poses.json to {a.out_dir}", flush=True)
    print(f"SPACING {actual:.4f}", flush=True)
    # hard-exit past rclpy's static-destructor teardown, which can abort with a
    # non-zero code and fail the stage
    os._exit(0)


if __name__ == "__main__":
    main()
