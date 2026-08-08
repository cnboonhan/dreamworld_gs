#!/usr/bin/env python3
"""Photograph one corridor of the simulated building, as if walking it.

Stands a camera at each of K standpoints along a lane and captures a full 360
panorama at each, the way someone walks a corridor stopping every half metre
with a 360 camera.

Each panorama is photosphere-stitched: a grid of perspective views over yaw and
pitch, reprojected into an equirectangular canvas using the camera's known pose
and intrinsics. Because the poses are exact this needs no feature matching, so
it works on flat sim walls that no feature detector would survive.

**Only images are written.** No poses, no positions, no marker that this came
from a simulator — the output is a folder of numbered equirectangular PNGs,
which is exactly what a person hands over after walking a corridor. Everything
downstream must work from that alone, or it has not been tested at all.

Needs a running sim with the rec_cam model, its image topic, and the
world's set_pose service bridged into ROS. capture.sh arranges that.

Ported from the dreamworld pipeline's panorama_gz stage; the depth output is
dropped, since a 360 camera does not produce depth.
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


class Cam(Node):
    def __init__(self, world, name):
        super().__init__("capture")
        self.name = name
        self.latest = None
        self.count = 0            # frames seen, so a fresh one can be waited for
        self.create_subscription(Image, ROS_TOPIC, self._cb, 10)
        self.cli = self.create_client(SetEntityPose, f"/world/{world}/set_pose")

    def _cb(self, msg):
        self.latest = msg
        self.count += 1

    def wait_fresh(self, n, timeout):
        """Block until n new frames arrive. A fixed sleep alone grabs frames
        still in flight from the previous pose, which reproject as ghostly
        overlapping domes."""
        start, t0 = self.count, time.time()
        while self.count - start < n and time.time() - t0 < timeout:
            time.sleep(0.005)
        return self.latest

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
        frame = node.wait_fresh(2, a.frame_timeout)
        shots.append((frame_rgb(frame).astype(np.float32),
                      quat_to_R(quat(float(yaw), float(pitch)))))

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
    for frame, R in shots:
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

    pano = (acc / np.maximum(wsum, 1e-6)[..., None]).clip(0, 255).astype(np.uint8)
    write_png(out, np.ascontiguousarray(pano))
    print(f"  -> {os.path.basename(out)} ({Wc}x{Hc})", flush=True)


# How far a standpoint wanders from the perfect line, in metres. A person
# walking a corridor does not stop at exactly even intervals along a
# mathematically straight line — and that regularity is not merely unrealistic,
# it is degenerate: perfectly collinear, evenly spaced, identically oriented
# cameras give bundle adjustment a rank-deficient problem, which COLMAP reports
# as repeated "unable to perform dense Cholesky factorization". Real captures
# never hit it because real walking is untidy.
JITTER_M = 0.12


def standpoints(plan, edge_id, n, seed=0):
    """n points along the lane, first at the id's first endpoint.

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
            nx, ny = -dy / length, dx / length      # across the corridor
            rng = np.random.default_rng(seed)
            out = []
            for i in range(n):
                f = i / (n - 1)
                x, y = ax + dx * f, ay + dy * f
                if 0 < i < n - 1:
                    along = rng.uniform(-JITTER_M, JITTER_M)
                    across = rng.uniform(-JITTER_M, JITTER_M)
                    x += dx / length * along + nx * across
                    y += dy / length * along + ny * across
                out.append((x, y))
            return out
    raise SystemExit(f"no edge '{edge_id}' in the capture plan")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--plan", required=True, help="capture_plan.json")
    ap.add_argument("--edge", required=True, help="edge id, e.g. L11.cafe--v7")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--standpoints", type=int, default=5,
                    help="stops along the corridor, endpoints included")
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

    if a.standpoints < 3:
        raise SystemExit("need at least 3 standpoints: two give a weak axis")
    plan = json.loads(open(a.plan).read())
    # seeded by the corridor, so a re-capture reproduces the same walk
    seed = int(hashlib.sha256(a.edge.encode()).hexdigest()[:8], 16)
    points = standpoints(plan, a.edge, a.standpoints, seed)
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
    print(f"walking {a.edge}: {len(points)} standpoints", flush=True)
    rng = np.random.default_rng(seed ^ 0x9E37)
    for i, (x, y) in enumerate(points):
        # and nobody holds a 360 camera at exactly one height
        a.height = base_height + float(rng.uniform(-0.05, 0.05))
        # zero-padded, so lexicographic order is walk order — the pipeline reads
        # direction of travel from filename order and nothing else
        out = os.path.join(a.out_dir, f"{i:03d}.png")
        panorama(node, x, y, a, out, f"{i + 1}/{len(points)}")

    print(f"wrote {len(points)} panoramas to {a.out_dir}", flush=True)
    # hard-exit past rclpy's static-destructor teardown, which can abort with a
    # non-zero code and fail the stage
    os._exit(0)


if __name__ == "__main__":
    main()
