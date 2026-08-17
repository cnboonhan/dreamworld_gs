"""The level line drawing — the splat viewer's kind of minimap.

An abstract plan on a dark ground, no floorplan raster: walls as the
picker draws them, doors amber, lanes and edges blue, vertices as
traffic-light dots. Vertices in building.yaml are already in a single
pixel frame (coordinate_system is reference_image), so the geometry needs
no projection, only a shift to the drawing's own bounding box.
"""

import html

from config import (C_BG, C_DOOR, C_INK, C_LABEL, C_LANE, C_LIFT, C_SEL,
                    C_WALL)
from store import lift_neighbors, state_color


def level_scene(lv: dict, dream: dict, edges: list, level: str,
                selected, pending, selected_edge=None):
    """(svg, width, height, shift) for one level. The yaml's pixel frame is
    shifted to the drawing's own bounding box, so the image is exactly as
    big as the building plus a margin."""
    mine = {n: v for n, v in dream.items() if v["level"] == level}
    pts = [(x, y) for seg in ("walls", "doors", "lanes")
           for x1, y1, x2, y2 in lv[seg] for x, y in ((x1, y1), (x2, y2))]
    pts += [(x, y) for _, x, y in lv["verts"]]
    pts += [(x, y) for _, x, y in lv.get("lifts") or []]
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
            on = selected_edge and set(selected_edge) == {a, b}
            parts.append(f'<line x1="{mine[a]["x"]}" y1="{mine[a]["y"]}" '
                         f'x2="{mine[b]["x"]}" y2="{mine[b]["y"]}" '
                         f'stroke="{C_SEL if on else C_LANE}" '
                         f'stroke-width="{4 if on else 2.5}" '
                         f'opacity="{1 if on else 0.85}"/>')

    # every marker is a group translated to its spot and scaled by --dwk,
    # which dw_view.js sets to 1/zoom — so dots and labels keep a constant
    # size on SCREEN while the geometry underneath zooms
    def marker(x, y, inner):
        return (f'<g style="transform:translate({x}px,{y}px) '
                f'scale(var(--dwk,1))">{inner}</g>')

    def label(text, color):
        return (f'<text x="0" y="-10" font-size="12" text-anchor="middle" '
                f'fill="{color}">{html.escape(text)}</text>')

    for name, x, y in lv["verts"]:
        parts.append(marker(x, y,
                            f'<circle r="6" fill="{C_LABEL}" '
                            f'stroke="{C_INK}" stroke-width="1.5"/>'
                            + label(name, C_LABEL)))
    # lift stops are materialized into the dream tree by the store, so they
    # draw with everything else below — diamonds among the dots
    for name, v in mine.items():
        inner = ""
        if name == selected or (selected_edge and name in selected_edge):
            inner += (f'<circle r="11" fill="none" stroke="{C_SEL}" '
                      f'stroke-width="2.5"/>')
        if name == selected:
            if v["pano"]:
                # where the panorama viewer is facing, driven live by
                # dw_pano.js through --dwrot; hidden until the viewer
                # first speaks. Negated there: bearings are y-up,
                # the drawing is y-down.
                inner += (f'<g id="dwface" style="visibility:hidden;'
                          f'transform:rotate(var(--dwrot,0deg))">'
                          f'<line x1="9" y1="0" x2="26" y2="0" '
                          f'stroke="{C_SEL}" stroke-width="2.5"/>'
                          f'<path d="M34 0 L24 -5 L24 5 Z" '
                          f'fill="{C_SEL}"/></g>')
        if name == pending:
            inner += (f'<circle r="11" fill="none" stroke="{C_LANE}" '
                      f'stroke-width="2" stroke-dasharray="4 3"/>')
        if v.get("lift"):
            # a lift stop wears a diamond — a different KIND of waypoint,
            # and the same lift's diamond on another level is the same
            # cabin. Its fill is its work state, its magenta ring its
            # nature — and its up and down arrows are the building's
            # vertical edges: lit where a stop exists on that side,
            # dimmed where the shaft ends.
            nb = lift_neighbors(name)
            for dy, tgt in ((-1, nb["up"]), (1, nb["down"])):
                col = C_LIFT if tgt else C_WALL
                inner += (f'<path d="M0 {dy * 24} L-5 {dy * 13} '
                          f'L5 {dy * 13} Z" fill="{col}"/>')
            inner += (f'<rect x="-6" y="-6" width="12" height="12" '
                      f'transform="rotate(45)" fill="{state_color(v)}" '
                      f'stroke="{C_LIFT}" stroke-width="2"/>')
        else:
            inner += (f'<circle r="6" fill="{state_color(v)}" '
                      f'stroke="{C_INK}" stroke-width="1.5"/>')
        inner += label(name, C_SEL if name == selected
                       else (C_LIFT if v.get("lift") else C_LABEL))
        parts.append(marker(v["x"], v["y"], inner))
    parts.append('</g>')
    return "".join(parts), w, h, (tx, ty)
