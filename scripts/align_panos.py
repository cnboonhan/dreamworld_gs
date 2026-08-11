"""Turn each real panorama to face the way the building map says it does.

A 360 camera records no heading, so a panorama shot at a vertex is a full view
of the right place pointing an unknown way round. Two shot at neighbouring
vertices cannot be chained, and nothing downstream can place either in the
building. Structure from motion would recover it, but only if consecutive
standpoints share enough view to match — and a capture taken one panorama per
vertex is metres apart, which is exactly the case it cannot solve.

The map already knows the answer to everything except that one angle. A
panorama's position is the vertex it was shot at, and the lanes leaving that
vertex say which bearings a corridor should open along. So this draws those
bearings over the panorama and lets you turn it until they land on the
corridors. One angle, by eye, from what the building already knows.

Saving rolls the image itself rather than writing the angle down beside it. A
rolled equirect is still an equirect, so every reader downstream — reprojection,
HunyuanWorld, the range grid in capture.py — gets a panorama already in the
building's frame, with no new field to plumb through and nothing to forget.

    python scripts/align_panos.py [--project multilevel_office] [--port 8085]

Then open http://localhost:8085 (tunnel it if you are not on the box).
"""

from __future__ import annotations

import argparse
import io
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import yaml
from PIL import Image

Image.MAX_IMAGE_PIXELS = None

REPO = Path(__file__).resolve().parent.parent
# wide enough to aim by, small enough that a browser holds fourteen of them
PREVIEW_W = 2048


def load_building(project: str, level: str):
    """(vertex name or index -> metres, lanes) for one level, in the gz frame.

    building.yaml is in drawing pixels and capture_plan.json is in metres, so
    the transform between them is fitted from the vertices named in both —
    seven of them here, agreeing to half a millimetre.
    """
    root = REPO / "assets" / "projects" / project
    b = yaml.safe_load((root / "maps" / f"{project}.building.yaml").read_text())
    plan = json.loads(next((root / "worlds").glob("*/capture_plan.json")).read_text())

    px = {v[3]: np.array(v[:2], dtype=float)
          for v in b["levels"][level]["vertices"] if v[3]}
    metres = {v["id"].split(".", 1)[1]: np.array([v["x"], v["y"]])
              for v in plan["levels"][level]["vertices"]}
    both = sorted(set(px) & set(metres))
    P = np.stack([px[k] for k in both])
    M = np.stack([metres[k] for k in both])
    fit = np.linalg.lstsq(np.c_[P, np.ones(len(P))], M, rcond=None)[0]
    to_m = lambda p: np.r_[np.asarray(p, dtype=float), 1.0] @ fit

    verts = {i: to_m(v[:2]) for i, v in enumerate(b["levels"][level]["vertices"])}
    walls = [[to_m(b["levels"][level]["vertices"][w[0]][:2]).tolist(),
              to_m(b["levels"][level]["vertices"][w[1]][:2]).tolist()]
             for w in b["levels"][level]["walls"]]
    named = {v["id"].split(".", 1)[1]: np.array([v["x"], v["y"]])
             for v in plan["levels"][level]["vertices"]}
    lanes = [(e["a"].split(".", 1)[1], e["b"].split(".", 1)[1])
             for e in plan["levels"][level]["edges"]]
    return verts, named, lanes, walls


def bearings(index: int, verts: dict, named: dict, lanes: list) -> list[dict]:
    """Where each lane leaving this vertex should appear, as a compass bearing."""
    here = verts[index]
    # the nav graph names vertices, the drawing numbers them; match by position
    name = min(named, key=lambda k: np.linalg.norm(named[k] - here))
    if np.linalg.norm(named[name] - here) > 0.5:
        return []
    out = []
    for a, b in lanes:
        other = b if a == name else (a if b == name else None)
        if other is None:
            continue
        d = named[other] - here
        out.append({"to": other,
                    "bearing": float(np.arctan2(d[1], d[0])),
                    "metres": round(float(np.linalg.norm(d)), 2)})
    return sorted(out, key=lambda o: o["bearing"])


PAGE = """<!doctype html><meta charset=utf-8><title>Manual Panorama Alignment</title>
<style>
 :root{color-scheme:dark}
 body{margin:0;background:#12161c;color:#dfe6ef;
      font:13px/1.5 ui-sans-serif,system-ui,sans-serif}
 header{display:flex;gap:10px;align-items:center;padding:9px 14px;
        border-bottom:1px solid #232c38;background:#161b23;flex-wrap:wrap}
 button{background:#1e2530;color:#dfe6ef;border:1px solid #2f3a48;border-radius:6px;
        padding:5px 10px;font:inherit;cursor:pointer}
 button:hover{background:#273040}
 button.on{background:#4ea1ff;color:#08121e;border-color:#4ea1ff;font-weight:600}
 #wrap{display:flex;height:calc(100vh - 52px)}
 #stage{position:relative;flex:1;min-width:0}
 canvas#cv{position:absolute;inset:0;width:100%;height:100%;cursor:ew-resize;
           touch-action:none}
 #cross{position:absolute;left:50%;top:0;bottom:0;width:0;
        border-left:2px dashed #4ea1ff;pointer-events:none}
 #cross b{position:absolute;top:10px;left:8px;background:#4ea1ff;color:#08121e;
          padding:2px 7px;border-radius:3px;white-space:nowrap}
 #busy{position:absolute;left:14px;bottom:12px;color:#8b98a8;pointer-events:none;
       text-shadow:0 1px 3px #000}
 #side{width:340px;border-left:1px solid #232c38;background:#0e1218;padding:10px;
       overflow:auto}
 #plan{width:320px;height:320px;background:#0a0d12;border:1px solid #232c38;
       border-radius:6px}
 #off{font-variant-numeric:tabular-nums;font-size:15px;font-weight:600}
 .done{color:#6c6}.warn{color:#f0a35e}
 #list{display:flex;flex-wrap:wrap;gap:5px;margin-top:10px}
 #list a{color:#9bd;text-decoration:none;border:1px solid #2f3a48;padding:2px 7px;
         border-radius:3px;font-size:12px}
 #list a.cur{background:#4ea1ff;color:#08121e;border-color:#4ea1ff}
 #list a.gone{color:#5a6675;border-color:#242c37;border-style:dashed}
 #list a.gone:hover{color:#8b98a8;border-color:#3a4757}
 #list a.gone.cur{background:#3a4757;color:#dfe6ef;border-color:#5a6675}
 #list a.ok{border-color:#3f7d55;color:#8fd6a6}
 #list a.ok.cur{color:#08121e}
 .k{color:#8b98a8}
</style>
<header>
  <b style="color:#8b98a8;font-weight:600">Manual Panorama Alignment</b>
  <b id=title></b>
  <span class=k>face:</span><span id=faces></span>
  <span class=k>| turn the panorama:</span>
  <button onclick=nudge(-5)>&larr;5&deg;</button><button onclick=nudge(-1)>&larr;1&deg;</button>
  <span id=off>0.0&deg;</span>
  <button onclick=nudge(1)>1&deg;&rarr;</button><button onclick=nudge(5)>5&deg;&rarr;</button>
  <button onclick=save() style="background:#2c6e3f;border-color:#3a8a52">save</button>
  <button onclick=rescan() title="pick up renamed or added files">rescan</button>
  <span id=msg></span>
</header>
<div id=wrap>
  <div id=stage><canvas id=cv></canvas><div id=cross><b id=facing></b></div>
  <div id=busy></div></div>
  <div id=side>
    <canvas id=plan width=320 height=320></canvas>
    <div class=k style=margin-top:8px>
      Pick a lane to face. The dashed line is that bearing — drag left/right
      until the corridor it names is centred on it. Check the others agree,
      then save.
    </div>
    <div id=list></div>
  </div>
</div>
<script>
const $=id=>document.getElementById(id);
let META={}, meta=[], cur=0, off=0, look=0, pitch=0, fov=1.6, drag=null, ready=false;

const VS=`attribute vec2 p;void main(){gl_Position=vec4(p,0.0,1.0);}`;
// our convention: column c holds lon = pi - 2pi(c+0.5)/W, so u = (pi-lon)/2pi.
// `corr` previews the roll that save() will bake into the file.
const FS=`precision highp float;uniform sampler2D tex;uniform vec2 res;
uniform float yaw,pitch,fov,corr;const float PI=3.14159265358979;
void main(){
 vec3 F=vec3(cos(pitch)*cos(yaw),cos(pitch)*sin(yaw),sin(pitch));
 vec3 R=normalize(cross(F,vec3(0.0,0.0,1.0)));vec3 U=cross(R,F);
 float t=tan(fov*0.5);vec2 c=(gl_FragCoord.xy-0.5*res)/(0.5*res.x);
 vec3 d=normalize(F+c.x*t*R+c.y*t*U);
 float lon=atan(d.y,d.x)-corr,lat=asin(clamp(d.z,-1.0,1.0));
 gl_FragColor=texture2D(tex,vec2((PI-lon)/(2.0*PI),0.5-lat/PI));}`;

const cv=$("cv"), gl=cv.getContext("webgl",{antialias:true});
const mk=(t,s)=>{const o=gl.createShader(t);gl.shaderSource(o,s);gl.compileShader(o);return o;};
const prog=gl.createProgram();
gl.attachShader(prog,mk(gl.VERTEX_SHADER,VS));
gl.attachShader(prog,mk(gl.FRAGMENT_SHADER,FS));
gl.linkProgram(prog);gl.useProgram(prog);
const buf=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,buf);
gl.bufferData(gl.ARRAY_BUFFER,new Float32Array([-1,-1,3,-1,-1,3]),gl.STATIC_DRAW);
const pl=gl.getAttribLocation(prog,"p");gl.enableVertexAttribArray(pl);
gl.vertexAttribPointer(pl,2,gl.FLOAT,false,0,0);
const U={};for(const n of["res","yaw","pitch","fov","corr"])U[n]=gl.getUniformLocation(prog,n);
const tex=gl.createTexture();
gl.bindTexture(gl.TEXTURE_2D,tex);
gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_S,gl.REPEAT);
gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_T,gl.CLAMP_TO_EDGE);
gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MIN_FILTER,gl.LINEAR);
gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MAG_FILTER,gl.LINEAR);

function draw(){
  const w=cv.clientWidth,h=cv.clientHeight;
  if(cv.width!==w||cv.height!==h){cv.width=w;cv.height=h;}
  gl.viewport(0,0,w,h);
  gl.uniform2f(U.res,w,h);gl.uniform1f(U.yaw,look);gl.uniform1f(U.pitch,pitch);
  gl.uniform1f(U.fov,fov);gl.uniform1f(U.corr,off*Math.PI/180);
  if(ready){gl.drawArrays(gl.TRIANGLES,0,3);}
  else{gl.clearColor(0.05,0.07,0.09,1);gl.clear(gl.COLOR_BUFFER_BIT);}
  requestAnimationFrame(draw);
}
draw();

async function boot(){
  META=await (await fetch('/meta')).json(); meta=META.panos; show(0);
}
function show(i){
  cur=i;off=0;ready=false;
  const m=meta[i];
  $("title").innerHTML=(m.missing?'<span class=k>'+m.file.replace(/\\.JPG$/i,'')+
    '</span>':m.file)+'  <span class=k>vertex '+m.index+
    (m.name?" ("+m.name+")":"")+'</span>'+
    (m.applied!==null?'  <span class=done>\u2713 '+m.applied.toFixed(1)+
     '\u00b0 already rolled in</span>':'  <span class=k>not yet aligned</span>');
  $("faces").innerHTML=m.bearings.map((b,k)=>
    '<button id=f'+k+' onclick=face('+k+')>'+b.to+' · '+b.metres+'m</button>').join(' ');
  if(m.missing){
    $("busy").innerHTML='<span class=k>no panorama shot here yet \u2014 '+
      'the plan and its lanes are shown for reference</span>';
    face(0);links();return;
  }
  $("busy").textContent="loading "+m.file+"\u2026";
  const im=new Image(); const want=m.file;
  im.onload=()=>{
    if(meta[cur].file!==want) return;      // a later click won the race
    gl.bindTexture(gl.TEXTURE_2D,tex);
    gl.texImage2D(gl.TEXTURE_2D,0,gl.RGB,gl.RGB,gl.UNSIGNED_BYTE,im);
    ready=true;$("busy").textContent="";};
  im.onerror=()=>{ if(meta[cur].file===want)
    $("busy").innerHTML='<span class=warn>'+want+' would not load</span>'; };
  im.src='/pano/'+encodeURIComponent(m.file)+'?t='+Date.now();
  face(0);links();
}
function face(k){
  const b=meta[cur].bearings[k];
  if(!b)return;
  look=b.bearing;pitch=0;
  meta[cur].bearings.forEach((_,j)=>$("f"+j)&&$("f"+j).classList.toggle("on",j===k));
  $("facing").textContent="should be "+b.to;
  plan(k);
}
function nudge(d){off=(off+d+360)%360;$("off").textContent=off.toFixed(1)+"\\u00b0";}
cv.addEventListener('mousedown',e=>drag=e.clientX);
addEventListener('mousemove',e=>{
  if(drag===null)return;
  off=(off+(e.clientX-drag)*0.12+360)%360;drag=e.clientX;
  $("off").textContent=off.toFixed(1)+"\\u00b0";});
addEventListener('mouseup',()=>drag=null);
cv.addEventListener('wheel',e=>{e.preventDefault();
  fov=Math.max(0.5,Math.min(2.6,fov*(1+e.deltaY*0.001)));},{passive:false});

function plan(k){
  const c=$("plan"),x=c.getContext('2d'),m=meta[cur];
  const R=9;                                  // metres shown around the vertex
  x.fillStyle="#0a0d12";x.fillRect(0,0,320,320);
  const P=p=>[160+(p[0]-m.at[0])/R*150, 160-(p[1]-m.at[1])/R*150];
  x.strokeStyle="#3a4757";x.lineWidth=2;x.beginPath();
  for(const w of META.walls){const a=P(w[0]),b=P(w[1]);
    x.moveTo(a[0],a[1]);x.lineTo(b[0],b[1]);}
  x.stroke();
  m.bearings.forEach((b,j)=>{
    x.strokeStyle=j===k?"#4ea1ff":"#5d6b7d";x.lineWidth=j===k?3:1.5;
    x.beginPath();x.moveTo(160,160);
    x.lineTo(160+Math.cos(b.bearing)*b.metres/R*150,
             160-Math.sin(b.bearing)*b.metres/R*150);x.stroke();
    x.fillStyle=j===k?"#4ea1ff":"#8b98a8";x.font="11px system-ui";
    x.fillText(b.to,162+Math.cos(b.bearing)*b.metres/R*150*0.6,
               158-Math.sin(b.bearing)*b.metres/R*150*0.6);});
  x.fillStyle="#ffd479";x.beginPath();x.arc(160,160,5,0,7);x.fill();
}
async function save(){
  const msg=$("msg");
  if(meta[cur].missing){msg.className="warn";
    msg.textContent=" nothing to save \\u2014 no panorama at this waypoint";return;}
  msg.textContent=" saving...";msg.className="";
  const r=await fetch('/save',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({file:meta[cur].file,degrees:off})});
  const j=await r.json();
  msg.className=j.ok?"done":"warn";
  msg.textContent=" "+(j.ok?("rolled "+j.pixels+" px"):j.error);
  if(j.ok){meta[cur].applied=(meta[cur].applied||0)+off;if(cur+1<meta.length)setTimeout(()=>show(cur+1),500);else links();}
}
async function rescan(){
  META=await (await fetch('/meta?t='+Date.now())).json();
  const was=meta[cur]&&meta[cur].file; meta=META.panos;
  const i=Math.max(0, meta.findIndex(m=>m.file===was)); show(i);
}
function links(){
  const have=meta.filter(m=>!m.missing);
  const done=have.filter(m=>m.applied!==null).length;
  $("list").innerHTML=meta.map((m,i)=>{
    if(m.missing) return '<a href=# class="gone'+(i===cur?' cur':'')+
      '" title="not photographed \\u2014 click to see it on the plan"'+
      ' onclick="show('+i+');return false">'+
      m.file.replace(/\\.JPG$/i,'')+'</a>';
    return '<a href=# class="'+(i===cur?'cur':'')+(m.applied!==null?' ok':'')+
      '" onclick="show('+i+');return false">'+(m.applied!==null?"\\u2713 ":"")+
      m.file.replace(/\\.JPG$/i,'')+'</a>';}).join('')+
    '<div class=k style="width:100%;margin-top:8px">'+done+' of '+have.length+
    ' aligned  \u00b7  '+meta.filter(m=>m.missing).length+
    ' waypoints not yet photographed</div>';
}
boot();
</script>"""



class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # Nothing here is worth caching and everything here changes: a saved
        # roll rewrites the panorama, a rename moves it to another vertex. A
        # held copy shows the previous state and reads as the tool being wrong.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        # the viewer is served from another port and posts marks here
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        if self.path == "/scenes":
            return self._send(200, json.dumps(self.server.scenes()).encode(),
                              "application/json")
        if self.path == "/meta":
            doc = {"panos": self.server.scan(), "walls": self.server.walls}
            return self._send(200, json.dumps(doc).encode(), "application/json")
        if self.path.startswith("/pano/"):
            from urllib.parse import unquote
            name = unquote(self.path[len("/pano/"):])
            p = self.server.preview_of(name)
            if not p.is_file():
                return self._send(404, b"no such panorama", "text/plain")
            buf = io.BytesIO()
            Image.open(p).save(buf, "JPEG", quality=88)
            return self._send(200, buf.getvalue(), "image/jpeg")
        self._send(404, b"?", "text/plain")

    def do_OPTIONS(self):
        self._send(204, b"", "text/plain")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n) or b"{}")
        try:
            if self.path == "/place":
                doc = self.server.place(req["scene"], req["vertex"], req["at"])
                walks = self.server.rewalk(req["scene"])
                return self._send(200, json.dumps(
                    {"ok": True, "placed": doc["placed"], "walks": walks}).encode(),
                    "application/json")
            if self.path == "/pose":
                doc = self.server.pose(req["scene"], float(req["height"]),
                                       float(req["pitch"]))
                return self._send(200, json.dumps({"ok": True, **doc}).encode(),
                                  "application/json")
            if self.path == "/mark":
                doc = self.server.mark(req["scene"], req["lane"],
                                       float(req["units"]), float(req["metres"]))
                body = json.dumps({"ok": True, **doc})
                return self._send(200, body.encode(), "application/json")
            px = self.server.apply(req["file"], float(req["degrees"]))
            body = json.dumps({"ok": True, "pixels": px})
        except Exception as err:                       # surfaced in the page
            body = json.dumps({"ok": False, "error": f"{type(err).__name__}: {err}"})
        self._send(200, body.encode(), "application/json")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", default="multilevel_office")
    ap.add_argument("--level", default="L11")
    ap.add_argument("--port", type=int, default=8085)
    a = ap.parse_args()

    root = REPO / "assets" / "projects" / a.project
    src = root / "panos"
    previews = src / ".previews"
    previews.mkdir(exist_ok=True)
    verts, named, lanes, walls = load_building(a.project, a.level)

    def scan() -> list[dict]:
        """The panoramas on disk right now.

        Rebuilt per request rather than at startup: the vertex a panorama
        belongs to is carried in its filename, so renaming one is how you
        correct a mis-labelled capture — and a list cached at boot would go on
        showing the old name however often you refreshed.
        """
        out, shot = [], set()
        for f in sorted(src.iterdir()):
            if f.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                continue
            vertex = resolve(f.stem)
            if vertex is None:
                out.append({"file": f.name, "index": -1, "name": "",
                            "at": [0, 0], "bearings": [], "applied": None,
                            "problem": f"no vertex of {a.level} is named by this"})
                continue
            i, nav = vertex
            shot.add(nav)
            out.append({"file": f.name, "index": i, "name": nav,
                        "at": verts[i].tolist(),
                        "bearings": bearings(i, verts, named, lanes),
                        "applied": applied_to(f.name)})
        # Every waypoint the map defines, photographed or not. A capture is
        # judged as much by what is missing as by what is there, and a gap in
        # the list is easier to see than a gap in a folder.
        for nav in sorted(named):
            if nav in shot:
                continue
            i = min(verts, key=lambda k: np.linalg.norm(verts[k] - named[nav]))
            out.append({"file": f"{a.level}.{nav}.JPG", "index": i,
                        "name": nav, "at": verts[i].tolist(),
                        "bearings": bearings(i, verts, named, lanes),
                        "applied": None, "missing": True})
        return out

    def applied_to(name: str):
        """Degrees already rolled into this file, or None if never saved.

        Kept beside the panorama rather than in it: the file itself carries no
        record of having been turned, and a second pass has to add to the roll
        already baked in rather than start from zero.
        """
        rec = src / ".aligned" / (name + ".json")
        if rec.is_file():
            try:
                return float(json.loads(rec.read_text())["degrees"])
            except (OSError, ValueError, KeyError):
                return None
        # an earlier run of this tool wrote the angle as bare text under the
        # panorama's own name; those files are aligned and must read as aligned
        old = src / ".aligned" / name
        if old.is_file():
            try:
                return float(old.read_text().strip())
            except (OSError, ValueError):
                return None
        return None

    def resolve(stem: str):
        """(drawing index, nav name) for a panorama's filename.

        The standard is <level>.<vertex>.JPG, named for the vertex it was shot
        at, because that name is the scene id everything downstream uses. A
        camera's own numbering — 224_apex_lab_entrance — is accepted too, since
        that is what comes off the card before anyone has renamed anything.
        """
        want = stem.split(".", 1)[1] if stem.startswith(a.level + ".") else None
        if want is None:
            m = re.match(r"(\d+)(?:_.*)?$", stem)
            if m and int(m.group(1)) in verts:
                here = verts[int(m.group(1))]
                nav = min(named, key=lambda k: np.linalg.norm(named[k] - here))
                return int(m.group(1)), nav
            return None
        if want not in named:
            return None
        i = min(verts, key=lambda k: np.linalg.norm(verts[k] - named[want]))
        return i, want

    splats = root / "splats"
    marks = splats / ".aligned"

    def scenes() -> list[dict]:
        """Every built world, with the lane marks placed in it so far.

        A world generated at a waypoint is in HunyuanWorld's own units, and no
        fit relates them to metres reliably — measured across 62 bearings of
        L11.v6 the implied scale spans 4.5x. So the scale is marked by hand,
        one number per lane: walk to where the neighbour actually is and say
        so. Kept beside the splats the way .aligned is kept beside the panos.
        """
        out = []
        for d in sorted(splats.iterdir()) if splats.is_dir() else []:
            if not (d / "world.ply").is_file():
                continue
            rec = marks / f"{d.name}.json"
            saved = json.loads(rec.read_text()) if rec.is_file() else {}
            walks = []
            paths = d / "world.paths.json"
            if paths.is_file():
                walks = [w["to"] for w in json.loads(paths.read_text())["walks"]]
            out.append({"scene": d.name, "lanes": walks,
                        "marked": sorted(saved.get("lanes", {})),
                        "units_per_metre": saved.get("units_per_metre"),
                        "placed": sorted(saved.get("placed", {})),
                        "done": bool(walks) and all(w in saved.get("lanes", {}) for w in walks)})
        return out

    def place(scene: str, vertex: str, at: list) -> dict:
        """Where a vertex actually is, in this world's own coordinates.

        Flown to and marked, rather than fitted. A position needs no scale and
        no bearing — the walk between two of them is a straight line — and the
        estimator this replaces was off by 2x and direction-dependent by 4.5x
        across L11.v6's bearings.
        """
        marks.mkdir(parents=True, exist_ok=True)
        rec = marks / f"{scene}.json"
        doc = json.loads(rec.read_text()) if rec.is_file() else {}
        doc.setdefault("placed", {})[vertex] = [round(float(v), 5) for v in at]
        rec.write_text(json.dumps(doc, indent=1))
        return doc

    def pose(scene: str, height: float, pitch: float) -> dict:
        """Eye height and tilt for a world, in its own units and degrees.

        One per world, not per lane: a capture is at one tripod height, and the
        walk should sit at eye level in the corridor rather than at whatever
        height the generator's origin landed on.
        """
        marks.mkdir(parents=True, exist_ok=True)
        rec = marks / f"{scene}.json"
        doc = json.loads(rec.read_text()) if rec.is_file() else {"lanes": {}}
        doc["height"] = round(height, 4)
        doc["pitch_deg"] = round(pitch, 2)
        rec.write_text(json.dumps(doc, indent=1))
        return doc

    def mark(scene: str, lane: str, units: float, metres: float) -> dict:
        """Record where one neighbour actually is, in this world's units."""
        marks.mkdir(parents=True, exist_ok=True)
        rec = marks / f"{scene}.json"
        doc = json.loads(rec.read_text()) if rec.is_file() else {"lanes": {}}
        doc["lanes"][lane] = {"units": round(units, 4), "metres": round(metres, 3),
                              "units_per_metre": round(units / max(metres, 1e-6), 4)}
        per = [v["units_per_metre"] for v in doc["lanes"].values()]
        doc["units_per_metre"] = round(float(np.median(per)), 4)
        doc["spread"] = round(max(per) / min(per), 3) if len(per) > 1 else 1.0
        rec.write_text(json.dumps(doc, indent=1))
        return doc

    def preview_of(name: str) -> Path:
        """The downscaled copy the browser gets, made on first ask."""
        f = src / name
        p = previews / (Path(name).stem + ".jpg")
        if f.is_file() and (not p.is_file()
                            or p.stat().st_mtime < f.stat().st_mtime):
            Image.open(f).convert("RGB").resize(
                (PREVIEW_W, PREVIEW_W // 2), Image.LANCZOS).save(p, quality=88)
        return p

    def apply(name: str, degrees: float) -> int:
        f = src / name
        im = np.asarray(Image.open(f).convert("RGB"))
        shift = int(round(degrees / 360.0 * im.shape[1]))
        Image.fromarray(np.roll(im, shift, axis=1)).save(f, quality=95)
        (previews / (f.stem + ".jpg")).unlink(missing_ok=True)
        preview_of(name)
        done = src / ".aligned"
        done.mkdir(exist_ok=True)
        total = ((applied_to(name) or 0.0) + degrees) % 360.0
        (done / (name + ".json")).write_text(json.dumps(
            {"degrees": round(total, 2), "last": round(degrees, 2)}, indent=1))
        return shift

    # Threaded: one held connection used to block every other request, and a
    # browser keeps its connection open. Building a preview from a 30-megapixel
    # JPEG takes seconds, and single-threaded that wedged the whole tool.
    srv = ThreadingHTTPServer(("0.0.0.0", a.port), Handler)
    srv.daemon_threads = True
    srv.scan, srv.walls, srv.preview_of, srv.apply = scan, walls, preview_of, apply
    def rewalk(scene: str):
        """Rebuild the walks from the marks, so the tour follows a save at once."""
        import subprocess
        subprocess.run(["docker", "compose", "exec", "-T", "generator", "python",
                        "/opt/tools/edge_walks.py",
                        f"/workspace/projects/{a.project}/splats/{scene}"],
                       cwd=REPO, capture_output=True, timeout=180)
        f = splats / scene / "world.paths.json"
        return json.loads(f.read_text())["walks"] if f.is_file() else None

    srv.scenes, srv.mark, srv.pose = scenes, mark, pose
    srv.place, srv.rewalk = place, rewalk
    found = scan()
    print(f"{len(found)} panoramas — http://localhost:{a.port}")
    for m in found:
        if m.get("problem"):
            print(f"  {m['file']}: {m['problem']}")
        elif not m["bearings"]:
            print(f"  {m['file']}: no lanes at this vertex, nothing to aim by")
    srv.serve_forever()


if __name__ == "__main__":
    main()
