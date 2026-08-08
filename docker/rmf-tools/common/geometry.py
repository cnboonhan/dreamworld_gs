"""Shared camera pose math (gz convention: +X forward, +Z up)."""

import math

import numpy as np


def quat(yaw, pitch=0.0):
    """(yaw, pitch) -> (x, y, z, w) unit quaternion, matching the sim camera."""
    cy, sy = math.cos(yaw / 2), math.sin(yaw / 2)
    cp, sp = math.cos(pitch / 2), math.sin(pitch / 2)
    return (-sp * sy, sp * cy, cp * sy, cp * cy)


def quat_to_R(q):
    """(x,y,z,w) unit quaternion -> 3x3 rotation matrix (columns are the rotated
    basis axes, i.e. the camera/model axes expressed in the world frame)."""
    x, y, z, w = q
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w),     2 * (x * z + y * w)],
        [2 * (x * y + z * w),     1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w),     2 * (y * z + x * w),     1 - 2 * (x * x + y * y)],
    ])


def wrap(a):
    """Wrap an angle to [-pi, pi]."""
    return math.atan2(math.sin(a), math.cos(a))
