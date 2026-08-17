"""Where the editor lives and how it looks.

MOUNT must match the proxy's location: NiceGUI mounts ITSELF at this
prefix, so nginx passes the URI through unstripped and page, assets and
socket all agree on where they live.
"""

import os
from pathlib import Path

MOUNT = "/dreamworld_editor"
PROJECT = os.environ.get("DW_PROJECT", "multilevel_office")
PROJ = Path("/projects") / PROJECT
DREAM = PROJ / "dreamworld"
PREVIEW_W = 2048        # main's number: wide enough to aim by, light enough

# the splat viewer minimap's palette for the drawing, the dashboard's for
# lanes, doors and labels; vertex state in the traffic-light trio
C_BG, C_WALL = "#0a0d12", "#3a4757"
C_LANE, C_DOOR = "#58a6ff", "#e0a030"
C_INK, C_LABEL, C_SEL = "#0d1117", "#7d8590", "#4ea1ff"
C_RED, C_YEL, C_GRN = "#f85149", "#e3b341", "#3fb950"

# the plan's cursor says which mode you are in; dw_view.js reads it too —
# 'move' is its signal to leave dragging to the vertex, not the pan
CURSOR = {None: "grab", "add": "crosshair", "edge": "cell", "move": "move"}
