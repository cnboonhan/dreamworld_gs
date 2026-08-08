#!/usr/bin/env python3
"""Fix up a building_map_generator world in place so Gazebo will load it.

The generator emits SDF that is nearly right; these are the edits needed before
gz will run it cleanly. Ported from the dreamworld pipeline, minus the passes
that existed only to feed a generative video model (texture flattening, door
colour masking, the doors-removed and recoloured world variants) — here the
world is simulated and looked at, so its appearance is left as authored.

Usage: postprocess_world.py <world> <building.yaml>
"""
import re
import sys

import yaml


def uniquify_null_names(text):
    """Name the unnamed. The generator emits name="null" for doors the map
    didn't name, and Gazebo rejects a world with duplicate model names."""
    n = [0]

    def rename(m):
        n[0] += 1
        return f'{m.group(1)}name="door_auto_{n[0]}"'

    text = re.sub(r'(<(?:model|door)\s+)name="null"', rename, text)
    print(f"  named {n[0]} unnamed element(s)")
    return text


def fix_camera(text):
    """Aim the GUI's initial camera at the building from above, instead of the
    generator's default pose — which for most maps is off in empty space, so the
    sim opens on a grey void until you find the building by hand."""
    poses = re.findall(r"<pose>([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)", text)
    if poses:
        xs = [float(p[0]) for p in poses]
        ys = [float(p[1]) for p in poses]
        zs = [float(p[2]) for p in poses]
        cx, cy, cz = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, max(zs)
    else:
        cx, cy, cz = 0.0, 0.0, 15.0
    span = max(max(xs) - min(xs), max(ys) - min(ys)) if poses else 40.0
    cam = f"{cx} {cy} {cz + max(12.0, span * 0.45)} 0 1.2 0"
    text = re.sub(r"<camera_pose>[^<]+</camera_pose>", f"<camera_pose>{cam}</camera_pose>", text)
    print(f"  camera aimed at the building centre ({cam})")
    return text


def inject_sensors_plugin(text):
    """Add the sensors system, without which no camera or lidar in the world
    renders. Inserted after SceneBroadcaster so the render pipeline is up."""
    if "gz-sim-sensors-system" in text:
        return text
    plugin = ('    <plugin filename="gz-sim-sensors-system" '
              'name="gz::sim::systems::Sensors">\n'
              "      <render_engine>ogre2</render_engine>\n"
              "    </plugin>")
    text = re.sub(r"(gz-sim-scene-broadcaster-system.*?</plugin>)", r"\1\n" + plugin,
                  text, count=1, flags=re.DOTALL)
    print("  injected the sensors system")
    return text


def strip_lift_plugin(text):
    """Remove liblift.so from a building with no lifts — there is no cabin or
    shaft for it to drive, and it complains on every tick."""
    text = re.sub(r"\s*<plugin[^>]*filename=[\"']liblift\.so[\"'][^/]*/>\s*", "\n", text)
    text = re.sub(r"\s*<plugin[^>]*filename=[\"']liblift\.so[\"'][^>]*>.*?</plugin>\s*",
                  "\n", text, flags=re.DOTALL)
    print("  stripped liblift.so (building has no lifts)")
    return text


def inject_rec_cam(text):
    """Add the capture camera the panorama stage drives.

    It has to exist when the world loads: a sensor spawned at runtime never
    renders in server-only mode, so there is no way to add it later. It sits
    idle until `capture.py` teleports it, and publishes on /rec_cam. The
    horizontal FOV must match what capture.py reprojects with (2.2 rad).

    A depth camera rides alongside it, on the same link and the same frustum,
    publishing to /rec_depth. A 360 camera in a real building produces no such
    thing — this is the simulator telling us where its surfaces are, and it is
    used only by the simulated pipeline, to start gaussian splatting from real
    geometry rather than from noise. Without it the corridor reconstructs as
    soup: every camera centre sits on one walked line, so depth along a ray is
    nearly free and gaussians settle wherever they were initialised.
    """
    if 'name="rec_cam"' in text:
        return text
    cam = """
    <model name="rec_cam"><pose>0 0 2 0 0 0</pose>
      <link name="link"><gravity>false</gravity>
        <sensor name="rec_cam_sensor" type="camera">
          <camera><horizontal_fov>2.2</horizontal_fov>
            <image><width>640</width><height>480</height></image>
            <clip><near>0.1</near><far>500</far></clip></camera>
          <always_on>1</always_on><update_rate>30</update_rate>
          <topic>rec_cam</topic>
        </sensor>
        <sensor name="rec_depth_sensor" type="depth_camera">
          <camera><horizontal_fov>2.2</horizontal_fov>
            <image><width>640</width><height>480</height></image>
            <clip><near>0.1</near><far>100</far></clip></camera>
          <always_on>1</always_on><update_rate>30</update_rate>
          <topic>rec_depth</topic>
        </sensor></link>
    </model>
"""
    text = text.replace("</world>", cam + "  </world>", 1)
    print("  injected the capture camera (rec_cam) and its depth camera")
    return text


def make_doors_opaque(text):
    """The generator gives doors alpha 0.6. Half-transparent doors read as open
    when they are shut, which is exactly the state you are watching for here."""
    n = [0]

    def fix(m):
        n[0] += 1
        return f"{m.group(1)}1{m.group(3)}"

    def opaque_block(m):
        return re.sub(r"(<(ambient|diffuse)>\s*(?:[\d.eE+-]+\s+){3})[\d.eE+-]+(\s*</\2>)",
                      fix, m.group(0))

    text = re.sub(r"<model name=\"[^\"]*[Dd]oor[^\"]*\">.*?</model>", opaque_block,
                  text, flags=re.DOTALL)
    print(f"  forced {n[0]} door material(s) opaque")
    return text


def main():
    path, building_path = sys.argv[1], sys.argv[2]
    text = open(path).read()
    building = yaml.safe_load(open(building_path))

    print("postprocessing the world:")
    text = uniquify_null_names(text)
    text = fix_camera(text)
    text = inject_sensors_plugin(text)
    if not building.get("lifts"):
        text = strip_lift_plugin(text)
    text = inject_rec_cam(text)
    text = make_doors_opaque(text)

    open(path, "w").write(text)


if __name__ == "__main__":
    main()
