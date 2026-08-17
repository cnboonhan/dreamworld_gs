"""The level line drawing — the splat viewer's kind of minimap.

An abstract plan on a dark ground, no floorplan raster: walls as the
picker draws them, doors amber, lanes and edges blue, vertices as
traffic-light dots. Vertices in building.yaml are already in a single
pixel frame (coordinate_system is reference_image), so the geometry needs
no projection, only a shift to the drawing's own bounding box.
"""

import html

from config import (C_BG, C_DOOR, C_INK, C_LABEL, C_LANE, C_SEL, C_WALL)
from store import state_color


def level_scene(lv: dict, dream: dict, edges: list, level: str,
                selected, pending):
    """(svg, width, height, shift) for one level. The yaml's pixel frame is
    shifted to the drawing's own bounding box, so the image is exactly as
    big as the building plus a margin."""
    mine = {n: v for n, v in dream.items() if v["level"] == level}
    pts = [(x, y) for seg in ("walls", "doors", "lanes")
           for x1, y1, x2, y2 in lv[seg] for x, y in ((x1, y1), (x2, y2))]
    pts += [(x, y) for _, x, y in lv["verts"]]
    pts += [(v["x"], v["y"]) for v in mine.values()]
    if not pts:
        return ('<text x="200" y="150" font-size="14" text-anchor="middle" '
                f'fill="{C_LABEL}">nothing on this level yet</text>', 400, 300,
                (0, 0))
    pad = 40
    tx = pad - min(x for x, _ in pts)
    ty = pad - min(y for _, y in pts)
    w = int(max(x for x, _ in pts) + tx + pad + 1)
    h = int(max(y for _, y in pts) + ty + pad + 1)
    parts = [f'<rect x="0" y="0" width="{w}" height="{h}" fill="{C_BG}"/>',
             f'<g transform="translate({tx},{ty})">']
    for x1, y1, x2, y2 in lv["walls"]:
        parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
                     f'stroke="{C_WALL}" stroke-width="2"/>')
    for x1, y1, x2, y2 in lv["doors"]:
        parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
                     f'stroke="{C_DOOR}" stroke-width="3.5"/>')
    for x1, y1, x2, y2 in lv["lanes"]:
        parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
                     f'stroke="{C_LANE}" stroke-width="2.5" opacity="0.85"/>')
    for a, b in edges:
        if a in mine and b in mine:
            parts.append(f'<line x1="{mine[a]["x"]}" y1="{mine[a]["y"]}" '
                         f'x2="{mine[b]["x"]}" y2="{mine[b]["y"]}" '
                         f'stroke="{C_LANE}" stroke-width="2.5" '
                         f'opacity="0.85"/>')

    def label(x, y, text, color):
        return (f'<text x="{x}" y="{y - 10}" font-size="12" '
                f'text-anchor="middle" fill="{color}">'
                f'{html.escape(text)}</text>')

    for name, x, y in lv["verts"]:
        parts.append(f'<circle cx="{x}" cy="{y}" r="6" fill="{C_LABEL}" '
                     f'stroke="{C_INK}" stroke-width="1.5"/>')
        parts.append(label(x, y, name, C_LABEL))
    for name, v in mine.items():
        x, y = v["x"], v["y"]
        if name == selected:
            parts.append(f'<circle cx="{x}" cy="{y}" r="11" fill="none" '
                         f'stroke="{C_SEL}" stroke-width="2.5"/>')
        if name == pending:
            parts.append(f'<circle cx="{x}" cy="{y}" r="11" fill="none" '
                         f'stroke="{C_LANE}" stroke-width="2" '
                         f'stroke-dasharray="4 3"/>')
        parts.append(f'<circle cx="{x}" cy="{y}" r="6" '
                     f'fill="{state_color(v)}" stroke="{C_INK}" '
                     f'stroke-width="1.5"/>')
        parts.append(label(x, y, name,
                           C_SEL if name == selected else C_LABEL))
    parts.append('</g>')
    return "".join(parts), w, h, (tx, ty)
