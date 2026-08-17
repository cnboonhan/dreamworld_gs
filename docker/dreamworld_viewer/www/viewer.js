// The dreamworld walkthrough viewer — main's splat viewer, rebuilt on the
// dreamworld tree. The renderer is main's core (antimatter15 heritage: the
// 32-byte records, counting-sort worker, packed-covariance texture and
// shaders, ported verbatim through the editor's dw_splat). The camera is
// held as yaw/pitch about the world's own up — main's tour philosophy, so
// the horizon never rolls — and because every world's meta gives building
// east and up in ply coordinates, yaw IS a building bearing everywhere.
//
// Crossing an edge: pick a neighbour on the plan, choose the look to
// arrive in, and the camera spins to face the edge's bearing, the
// generated crossing video plays over the canvas while the destination
// world loads behind it, and the video fades out onto the destination
// standing at its capture point facing the same bearing.

const FILES = '/dreamworld_editor/files';
const GRAPH = '/dreamworld_editor/graph';

// ---- matrices (main's helpers) ------------------------------------------
function invert4(a) {
  const b00 = a[0]*a[5]-a[1]*a[4], b01 = a[0]*a[6]-a[2]*a[4],
    b02 = a[0]*a[7]-a[3]*a[4], b03 = a[1]*a[6]-a[2]*a[5],
    b04 = a[1]*a[7]-a[3]*a[5], b05 = a[2]*a[7]-a[3]*a[6],
    b06 = a[8]*a[13]-a[9]*a[12], b07 = a[8]*a[14]-a[10]*a[12],
    b08 = a[8]*a[15]-a[11]*a[12], b09 = a[9]*a[14]-a[10]*a[13],
    b10 = a[9]*a[15]-a[11]*a[13], b11 = a[10]*a[15]-a[11]*a[14];
  const det = b00*b11-b01*b10+b02*b09+b03*b08-b04*b07+b05*b06;
  if (!det) return null;
  return [
    (a[5]*b11-a[6]*b10+a[7]*b09)/det, (a[2]*b10-a[1]*b11-a[3]*b09)/det,
    (a[13]*b05-a[14]*b04+a[15]*b03)/det, (a[10]*b04-a[9]*b05-a[11]*b03)/det,
    (a[6]*b08-a[4]*b11-a[7]*b07)/det, (a[0]*b11-a[2]*b08+a[3]*b07)/det,
    (a[14]*b02-a[12]*b05-a[15]*b01)/det, (a[8]*b05-a[10]*b02+a[11]*b01)/det,
    (a[4]*b10-a[5]*b08+a[7]*b06)/det, (a[1]*b08-a[0]*b10-a[3]*b06)/det,
    (a[12]*b04-a[13]*b02+a[15]*b00)/det, (a[9]*b02-a[8]*b04-a[11]*b00)/det,
    (a[5]*b07-a[4]*b09-a[6]*b06)/det, (a[0]*b09-a[1]*b07+a[2]*b06)/det,
    (a[13]*b01-a[12]*b03-a[14]*b00)/det, (a[8]*b03-a[9]*b01+a[10]*b00)/det,
  ];
}
function multiply4(a, b) {
  const out = new Array(16);
  for (let i = 0; i < 4; i++)
    for (let j = 0; j < 4; j++)
      out[4*i+j] = b[4*i]*a[j] + b[4*i+1]*a[4+j] + b[4*i+2]*a[8+j]
        + b[4*i+3]*a[12+j];
  return out;
}
function getProjectionMatrix(fx, fy, width, height) {
  const znear = 0.1, zfar = 200;
  return [(2*fx)/width, 0, 0, 0, 0, -(2*fy)/height, 0, 0,
          0, 0, zfar/(zfar-znear), 1, 0, 0, -(zfar*znear)/(zfar-znear), 0];
}
const unit = v => { const n = Math.hypot(...v) || 1; return v.map(x => x/n); };
const cross = (a, b) => [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2],
                         a[0]*b[1]-a[1]*b[0]];

// ---- the render worker (main's, verbatim through dw_splat) ---------------
function dwWorker(self) {
  let buffer, vertexCount = 0, viewProj;
  const rowLength = 32;
  let lastProj = [], lastVertexCount = 0;
  const _f = new Float32Array(1), _i = new Int32Array(_f.buffer);
  function floatToHalf(f0) {
    _f[0] = f0;
    const f = _i[0], sign = (f >> 31) & 1, exp = (f >> 23) & 0xff;
    let frac = f & 0x7fffff, newExp;
    if (exp === 0) newExp = 0;
    else if (exp < 113) {
      newExp = 0; frac |= 0x800000; frac >>= (113 - exp);
      if (frac & 0x1000000) { newExp = 1; frac = 0; }
    } else if (exp < 142) newExp = exp - 112;
    else { newExp = 31; frac = 0; }
    return (sign << 15) | (newExp << 10) | (frac >> 13);
  }
  const pack = (x, y) => (floatToHalf(x) | (floatToHalf(y) << 16)) >>> 0;
  function generateTexture() {
    if (!buffer) return;
    const f = new Float32Array(buffer), u = new Uint8Array(buffer);
    const tw = 2048, th = Math.ceil((2 * vertexCount) / tw);
    const td = new Uint32Array(tw * th * 4);
    const tc = new Uint8Array(td.buffer), tf = new Float32Array(td.buffer);
    for (let i = 0; i < vertexCount; i++) {
      tf[8*i] = f[8*i]; tf[8*i+1] = f[8*i+1]; tf[8*i+2] = f[8*i+2];
      tc[4*(8*i+7)] = u[32*i+24]; tc[4*(8*i+7)+1] = u[32*i+25];
      tc[4*(8*i+7)+2] = u[32*i+26]; tc[4*(8*i+7)+3] = u[32*i+27];
      const s = [f[8*i+3], f[8*i+4], f[8*i+5]];
      const r = [(u[32*i+28]-128)/128, (u[32*i+29]-128)/128,
                 (u[32*i+30]-128)/128, (u[32*i+31]-128)/128];
      const M = [
        1-2*(r[2]*r[2]+r[3]*r[3]), 2*(r[1]*r[2]+r[0]*r[3]),
        2*(r[1]*r[3]-r[0]*r[2]), 2*(r[1]*r[2]-r[0]*r[3]),
        1-2*(r[1]*r[1]+r[3]*r[3]), 2*(r[2]*r[3]+r[0]*r[1]),
        2*(r[1]*r[3]+r[0]*r[2]), 2*(r[2]*r[3]-r[0]*r[1]),
        1-2*(r[1]*r[1]+r[2]*r[2]),
      ].map((k, j) => k * s[Math.floor(j / 3)]);
      const sig = [M[0]*M[0]+M[3]*M[3]+M[6]*M[6],
        M[0]*M[1]+M[3]*M[4]+M[6]*M[7], M[0]*M[2]+M[3]*M[5]+M[6]*M[8],
        M[1]*M[1]+M[4]*M[4]+M[7]*M[7], M[1]*M[2]+M[4]*M[5]+M[7]*M[8],
        M[2]*M[2]+M[5]*M[5]+M[8]*M[8]];
      td[8*i+4] = pack(4*sig[0], 4*sig[1]);
      td[8*i+5] = pack(4*sig[2], 4*sig[3]);
      td[8*i+6] = pack(4*sig[4], 4*sig[5]);
    }
    self.postMessage({ texdata: td, texwidth: tw, texheight: th },
                     [td.buffer]);
  }
  function runSort(vp) {
    if (!buffer) return;
    const f = new Float32Array(buffer);
    if (lastVertexCount === vertexCount) {
      const dot = lastProj[2]*vp[2] + lastProj[6]*vp[6] + lastProj[10]*vp[10];
      if (Math.abs(dot - 1) < 0.01) return;
    } else { generateTexture(); lastVertexCount = vertexCount; }
    let maxD = -Infinity, minD = Infinity;
    const sizes = new Int32Array(vertexCount);
    for (let i = 0; i < vertexCount; i++) {
      const d = ((vp[2]*f[8*i] + vp[6]*f[8*i+1] + vp[10]*f[8*i+2]) * 4096) | 0;
      sizes[i] = d;
      if (d > maxD) maxD = d;
      if (d < minD) minD = d;
    }
    const inv = (256*256-1) / (maxD - minD);
    const counts = new Uint32Array(256*256);
    for (let i = 0; i < vertexCount; i++) {
      sizes[i] = ((sizes[i] - minD) * inv) | 0;
      counts[sizes[i]]++;
    }
    const starts = new Uint32Array(256*256);
    for (let i = 1; i < 256*256; i++) starts[i] = starts[i-1] + counts[i-1];
    const idx = new Uint32Array(vertexCount);
    for (let i = 0; i < vertexCount; i++) idx[starts[sizes[i]]++] = i;
    lastProj = vp;
    self.postMessage({ depthIndex: idx, vertexCount }, [idx.buffer]);
  }
  self.onmessage = e => {
    if (e.data.records) {
      buffer = e.data.records;
      vertexCount = Math.floor(buffer.byteLength / rowLength);
      lastVertexCount = -1;
      self.postMessage({ loadedCount: vertexCount });
    } else if (e.data.view) { viewProj = e.data.view; runSort(viewProj); }
  };
}

const VERT = `#version 300 es
precision highp float; precision highp int;
uniform highp usampler2D u_texture; uniform mat4 projection, view;
uniform vec2 focal; uniform vec2 viewport;
in vec2 position; in int index;
out vec4 vColor; out vec2 vPosition;
void main () {
  uvec4 cen = texelFetch(u_texture, ivec2((uint(index) & 0x3ffu) << 1, uint(index) >> 10), 0);
  vec4 cam = view * vec4(uintBitsToFloat(cen.xyz), 1);
  vec4 pos2d = projection * cam;
  float clip = 1.2 * pos2d.w;
  if (pos2d.z < -clip || pos2d.x < -clip || pos2d.x > clip || pos2d.y < -clip || pos2d.y > clip) {
    gl_Position = vec4(0.0, 0.0, 2.0, 1.0); return; }
  uvec4 cov = texelFetch(u_texture, ivec2(((uint(index) & 0x3ffu) << 1) | 1u, uint(index) >> 10), 0);
  vec2 u1 = unpackHalf2x16(cov.x), u2 = unpackHalf2x16(cov.y), u3 = unpackHalf2x16(cov.z);
  mat3 Vrk = mat3(u1.x, u1.y, u2.x, u1.y, u2.y, u3.x, u2.x, u3.x, u3.y);
  mat3 J = mat3(focal.x / cam.z, 0., -(focal.x * cam.x) / (cam.z * cam.z),
    0., -focal.y / cam.z, (focal.y * cam.y) / (cam.z * cam.z), 0., 0., 0.);
  mat3 T = transpose(mat3(view)) * J;
  mat3 cov2d = transpose(T) * Vrk * T;
  float mid = (cov2d[0][0] + cov2d[1][1]) / 2.0;
  float radius = length(vec2((cov2d[0][0] - cov2d[1][1]) / 2.0, cov2d[0][1]));
  float l1 = mid + radius, l2 = mid - radius;
  if (l2 < 0.0) return;
  vec2 dv = normalize(vec2(cov2d[0][1], l1 - cov2d[0][0]));
  vec2 maj = min(sqrt(2.0 * l1), 1024.0) * dv;
  vec2 min2 = min(sqrt(2.0 * l2), 1024.0) * vec2(dv.y, -dv.x);
  vColor = clamp(pos2d.z/pos2d.w+1.0, 0.0, 1.0)
    * vec4((cov.w) & 0xffu, (cov.w >> 8) & 0xffu, (cov.w >> 16) & 0xffu, (cov.w >> 24) & 0xffu) / 255.0;
  vPosition = position;
  vec2 c = vec2(pos2d) / pos2d.w;
  gl_Position = vec4(c + position.x * maj / viewport + position.y * min2 / viewport, 0.0, 1.0);
}`;
const FRAG = `#version 300 es
precision highp float;
in vec4 vColor; in vec2 vPosition; out vec4 fragColor;
void main () {
  float A = -dot(vPosition, vPosition);
  if (A < -4.0) discard;
  float B = exp(A) * vColor.a;
  fragColor = vec4(B * vColor.rgb, B);
}`;

// ---- state ---------------------------------------------------------------
const $ = id => document.getElementById(id);
const cv = $('cv'), plan = $('plan');
let graph = null;
const st = {
  at: null, look: 'original', target: null, moving: false, follow: true,
  cam: { eye: [0, 0, 0], yaw: 0, pitch: 0 },
  basis: null,           // {east, north, up} of the CURRENT world
  vertexCount: 0,
  transition: null,      // {to, look, phase} while crossing an edge
};

// ---- where we are, told to the editor for the harness to read -------------
// Main's truth-protocol shape, inverted for v2: the walker reports, the
// broker holds, anyone may ask. Pushed on change and heartbeaten each
// second, so /dreamworld_editor/viewer/state is never more than a beat old.
let lastPush = 0, lastSent = '', coreOk = 0;
// the bar says whether dreamworld_core is hearing us: green while pushes
// land, red within seconds of them failing — the harness's view of this
// walker is only as good as this light
function coreMark() {
  const el = $('core');
  if (!el) return;
  if (!st.follow) {
    el.textContent = '\u25cb core unsynced';
    el.style.color = '#8b98a8';
    return;
  }
  const live = performance.now() - coreOk < 3000;
  el.textContent = live ? '\u25cf core synced' : '\u25cb core unreachable';
  el.style.color = live ? '#3fb950' : '#f85149';
}
setInterval(coreMark, 1000);
// the chip IS the toggle: click to walk free of the core, click again
// to follow. Rejoining catches up by TELEPORT — wherever the core went
// meanwhile is not a walk you took, so a fade says so honestly
addEventListener('DOMContentLoaded', () => {
  $('core').onclick = async () => {
    st.follow = !st.follow;
    coreMark();
    if (!st.follow || st.moving) return;
    try {
      const doc = await (await fetch('/dreamworld_core/position')).json();
      lastSeq = doc.seq || 0;
      const p2 = doc.position;
      if (p2 && (p2.at !== st.at || p2.look !== st.look)
          && graph.vertices[p2.at]
          && graph.vertices[p2.at].looks[p2.look]) {
        await jumpTo(p2.at, p2.look);
      }
    } catch (e) {}
  };
});

// ---- following the core: main's protocol, kept — the core is the ONLY
// writer of position. The harness posts where the walker should be; so
// does our own go button; the viewer notices the sequence advance and
// enacts it: a spin-and-crossing for a neighbour, a cut for anywhere
// else, a reload in place for a change of look. The follow toggle turns
// the viewer back into a free agent.
let lastSeq = 0;
function followTruth(pos, seq) {
  if (!st.follow || st.moving || !graph) return;
  if (!(seq > lastSeq)) return;
  lastSeq = seq;
  enact(pos.at, pos.look, pos.yaw_deg);
}
async function enact(to, look, yawDeg) {
  if (!graph.vertices[to] || !graph.vertices[to].looks[look]) return;
  if (to === st.at && look === st.look) {
    // same place: the command is a TURN — the harness's face and turn
    // tools ride the position's yaw
    if (yawDeg != null) {
      st.moving = true;
      await spinTo(yawDeg * Math.PI / 180, 600);
      st.moving = false;
      pushState(true);
    }
    return;
  }
  if (to !== st.at && neighbours(st.at).includes(to)) {
    await runGo(to, look);
  } else {
    await jumpTo(to, look);
  }
}
async function jumpTo(to, look) {
  st.moving = true;
  st.transition = { to, look, phase: 'cut' };
  pushState(true);
  $('shade').style.opacity = '1';
  const ab = await fetchRecords(to, look);
  await new Promise(r => setTimeout(r, 300));
  const meta = graph.vertices[to].looks[look].meta;
  st.basis = basisOf(meta);
  st.cam.eye = meta.center.slice();
  const nbrs2 = neighbours(to);
  if (to !== st.at && nbrs2.length) st.cam.yaw = bearingToFrom(to, nbrs2[0]);
  st.cam.pitch = 0;
  await showRecords(ab);
  await new Promise(r => requestAnimationFrame(
    () => requestAnimationFrame(r)));
  $('shade').style.opacity = '0';
  st.at = to;
  st.look = look;
  st.transition = null;
  updateBar();
  st.moving = false;
  pushState(true);
  preheat();
}
function bearingToFrom(frm, to) {
  const a = graph.vertices[frm], b = graph.vertices[to];
  return Math.atan2(-(b.y - a.y), b.x - a.x);
}
function postPosition(at, look) {
  return fetch('/dreamworld_core/position', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ at, look }) }).catch(() => {});
}
function pushState(force) {
  if (!graph || !st.at) return;
  const now = performance.now();
  if (!force && now - lastPush < 250) return;
  const me = graph.vertices[st.at];
  const doc = {
    at: st.at, look: st.look, level: me.level, x: me.x, y: me.y,
    lift: me.lift || null,
    yaw_deg: Math.round(st.cam.yaw * 1800 / Math.PI) / 10,
    pitch_deg: Math.round(st.cam.pitch * 1800 / Math.PI) / 10,
    moving: st.moving,
    transition: st.transition,
  };
  const s = JSON.stringify(doc);
  if (!force && s === lastSent && now - lastPush < 1000) return;
  lastSent = s;
  lastPush = now;
  fetch('/dreamworld_core/viewer/state', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: s }).then(async r => {
      if (r.ok) {
        coreOk = performance.now();
        // the response carries the TRUTH: core's position, which this
        // viewer follows — the harness (or our own go button) moved it
        const doc = await r.json();
        if (doc.position) followTruth(doc.position, doc.seq);
      }
      coreMark(); }).catch(() => coreMark());
}

function basisOf(meta) {
  const up = unit(meta.up), east = unit(meta.east);
  return { up, east, north: unit(cross(up, east)) };
}
function viewMatrix() {
  const { east, north, up } = st.basis;
  const cy = Math.cos(st.cam.yaw), sy = Math.sin(st.cam.yaw);
  const cp = Math.cos(st.cam.pitch), sp = Math.sin(st.cam.pitch);
  const flat = east.map((e, i) => cy * e + sy * north[i]);
  const fwd = unit(flat.map((f, i) => cp * f + sp * up[i]));
  const right = unit(cross(fwd, up));
  const down = cross(fwd, right);
  const R = [right, down, fwd];
  const e = st.cam.eye;
  const t = R.map(r => -(r[0]*e[0] + r[1]*e[1] + r[2]*e[2]));
  return [R[0][0], R[1][0], R[2][0], 0, R[0][1], R[1][1], R[2][1], 0,
          R[0][2], R[1][2], R[2][2], 0, t[0], t[1], t[2], 1];
}

// ---- renderer -------------------------------------------------------------
const worker = new Worker(URL.createObjectURL(new Blob(
  ['(', dwWorker.toString(), ')(self)'],
  { type: 'application/javascript' })));
const gl = cv.getContext('webgl2', { antialias: false });
const mk = (t, s) => { const o = gl.createShader(t);
  gl.shaderSource(o, s); gl.compileShader(o);
  if (!gl.getShaderParameter(o, gl.COMPILE_STATUS))
    console.error(gl.getShaderInfoLog(o));
  return o; };
const prog = gl.createProgram();
gl.attachShader(prog, mk(gl.VERTEX_SHADER, VERT));
gl.attachShader(prog, mk(gl.FRAGMENT_SHADER, FRAG));
gl.linkProgram(prog); gl.useProgram(prog);
gl.disable(gl.DEPTH_TEST);
gl.enable(gl.BLEND);
gl.blendFuncSeparate(gl.ONE_MINUS_DST_ALPHA, gl.ONE,
                     gl.ONE_MINUS_DST_ALPHA, gl.ONE);
gl.blendEquationSeparate(gl.FUNC_ADD, gl.FUNC_ADD);
const u_proj = gl.getUniformLocation(prog, 'projection');
const u_view = gl.getUniformLocation(prog, 'view');
const u_focal = gl.getUniformLocation(prog, 'focal');
const u_vp = gl.getUniformLocation(prog, 'viewport');
const vbuf = gl.createBuffer();
gl.bindBuffer(gl.ARRAY_BUFFER, vbuf);
gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-2,-2, 2,-2, 2,2, -2,2]),
              gl.STATIC_DRAW);
const a_pos = gl.getAttribLocation(prog, 'position');
gl.enableVertexAttribArray(a_pos);
gl.vertexAttribPointer(a_pos, 2, gl.FLOAT, false, 0, 0);
const tex = gl.createTexture();
gl.bindTexture(gl.TEXTURE_2D, tex);
gl.uniform1i(gl.getUniformLocation(prog, 'u_texture'), 0);
const ibuf = gl.createBuffer();
const a_idx = gl.getAttribLocation(prog, 'index');
gl.enableVertexAttribArray(a_idx);
gl.bindBuffer(gl.ARRAY_BUFFER, ibuf);
gl.vertexAttribIPointer(a_idx, 1, gl.INT, false, 0, 0);
gl.vertexAttribDivisor(a_idx, 1);

let projection = [];
function resize() {
  const w = innerWidth, h = innerHeight;
  // the crossing videos were extracted at 1.2 rad horizontal fov; the
  // splat camera matches it, so the video-to-splat handoff keeps scale
  const f = (0.5 / Math.tan(0.6)) * w;
  cv.width = w; cv.height = h;
  gl.viewport(0, 0, w, h);
  gl.uniform2fv(u_focal, new Float32Array([f, f]));
  gl.uniform2fv(u_vp, new Float32Array([w, h]));
  projection = getProjectionMatrix(f, f, w, h);
  gl.uniformMatrix4fv(u_proj, false, projection);
}
addEventListener('resize', resize);
resize();

let onLoaded = null;
worker.onmessage = e => {
  if (e.data.texdata) {
    const { texdata, texwidth, texheight } = e.data;
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA32UI, texwidth, texheight, 0,
                  gl.RGBA_INTEGER, gl.UNSIGNED_INT, texdata);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, tex);
  } else if (e.data.depthIndex) {
    gl.bindBuffer(gl.ARRAY_BUFFER, ibuf);
    gl.bufferData(gl.ARRAY_BUFFER, e.data.depthIndex, gl.DYNAMIC_DRAW);
    st.vertexCount = e.data.vertexCount;
    if (onLoaded) { onLoaded(); onLoaded = null; }
  }
};

// ---- preheat: main's habit, carried over — fetch the whole building's
// worlds and crossings in the background, neighbours of where you stand
// first, so a crossing never waits on the network. Records are kept as
// buffers and COPIED before each transfer to the worker (a transferred
// buffer is gone); crossing videos become blob URLs that start instantly.
const heat = { records: new Map(), videos: new Map(), busy: false };
const KEEP = 24;      // record buffers held; the oldest leave first

async function fetchRecords(name, look) {
  const key = tagOf(name, look);
  if (heat.records.has(key)) {
    const ab = heat.records.get(key);
    heat.records.delete(key);
    heat.records.set(key, ab);      // refreshed: recently used stays
    return ab.slice(0);
  }
  const info = graph.vertices[name].looks[look];
  const url = `${FILES}/${name}/${info.dir}/${info.records}`;
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} for ${url}`);
  const ab = await r.arrayBuffer();
  heat.records.set(key, ab);
  while (heat.records.size > KEEP)
    heat.records.delete(heat.records.keys().next().value);
  return ab.slice(0);
}

async function fetchVideo(key) {
  if (heat.videos.has(key)) return heat.videos.get(key);
  try {
    const r = await fetch(`${FILES}/.crossings/${key}/crossing.mp4`);
    if (!r.ok) return null;
    const url = URL.createObjectURL(await r.blob());
    heat.videos.set(key, url);
    return url;
  } catch (e) { return null; }
}

async function preheat() {
  if (heat.busy || !graph) return;
  heat.busy = true;
  try {
    const jobs = [];
    const pushLooks = n => {
      for (const look of Object.keys(graph.vertices[n].looks))
        jobs.push(['r', n, look]);
    };
    if (st.at) {
      for (const nb of neighbours(st.at)) pushLooks(nb);
      for (const key of graph.crossings)
        if (key.startsWith(tagOf(st.at, st.look) + '__'))
          jobs.push(['v', key]);
    }
    for (const n of Object.keys(graph.vertices)) pushLooks(n);
    for (const key of graph.crossings) jobs.push(['v', key]);
    for (const j of jobs) {                 // sequential: gentle on tunnels
      if (j[0] === 'r') {
        if (heat.records.has(tagOf(j[1], j[2]))) continue;
        await fetchRecords(j[1], j[2]);
      } else {
        await fetchVideo(j[1]);
      }
    }
  } catch (e) { console.warn('preheat stopped:', e); }
  heat.busy = false;
}
function showRecords(ab) {
  return new Promise(res => {
    onLoaded = res;
    st.vertexCount = 0;
    worker.postMessage({ records: ab }, [ab]);
  });
}

// ---- input ---------------------------------------------------------------
let drag = null;
cv.addEventListener('pointerdown', e => {
  cv.focus();
  drag = [e.clientX, e.clientY];
  cv.setPointerCapture(e.pointerId); });
cv.addEventListener('pointermove', e => {
  if (!drag || st.moving) return;
  const s = 1.4 / Math.max(1, innerWidth);
  st.cam.yaw += (e.clientX - drag[0]) * s;
  st.cam.pitch = Math.min(1.45, Math.max(-1.45,
    st.cam.pitch + (e.clientY - drag[1]) * s));
  drag = [e.clientX, e.clientY]; });
cv.addEventListener('pointerup', () => drag = null);
cv.addEventListener('wheel', e => {
  e.preventDefault();
  if (st.moving) return;
  walk(e.deltaY * -0.004);
}, { passive: false });
const keys = new Set();
cv.addEventListener('keydown', e => { keys.add(e.code);
  if (e.code.startsWith('Arrow')) e.preventDefault(); });
cv.addEventListener('keyup', e => keys.delete(e.code));
function walk(d) {
  const { east, north, up } = st.basis;
  const cy = Math.cos(st.cam.yaw), sy = Math.sin(st.cam.yaw);
  const fwd = east.map((e2, i) => cy * e2 + sy * north[i]);
  st.cam.eye = st.cam.eye.map((v, i) => v + fwd[i] * d);
}
function strafe(d) {
  const { east, north, up } = st.basis;
  const cy = Math.cos(st.cam.yaw), sy = Math.sin(st.cam.yaw);
  const fwd = east.map((e2, i) => cy * e2 + sy * north[i]);
  const right = unit(cross(fwd, up));
  st.cam.eye = st.cam.eye.map((v, i) => v + right[i] * d);
}

// ---- the plan overlay: main's picker view — the waypoint you stand at,
// the walls around it, the neighbours you can walk to, nothing more.
// Range scales to the farthest neighbour, as main scaled to its lanes.
const px = plan.getContext('2d');
let hits = [];
function drawPlan() {
  if (!graph || !st.at || plan.style.display === 'none') return;
  const me = graph.vertices[st.at];
  const L = graph.levels[me.level] || { walls: [] };
  const nbrs = neighbours(st.at);
  const far = Math.max(40, ...nbrs.map(n => {
    const v = graph.vertices[n];
    return Math.hypot(v.x - me.x, v.y - me.y);
  })) * 1.4;
  const cx2 = 140, cy2 = 110, R = 95;
  const P = (x, y) => [cx2 + (x - me.x) / far * R,
                       cy2 + (y - me.y) / far * R];
  // clear, not fill: the plan's translucency is its CSS background,
  // so the world stays faintly visible behind it
  px.clearRect(0, 0, 320, 220);
  px.strokeStyle = '#3a4757'; px.lineWidth = 2; px.beginPath();
  for (const w of L.walls) {
    const a = P(w[0], w[1]), b = P(w[2], w[3]);
    px.moveTo(a[0], a[1]); px.lineTo(b[0], b[1]);
  }
  px.stroke();
  hits = [];
  for (const n of nbrs) {
    const v = graph.vertices[n];
    if (v.level !== me.level) continue;
    const [x, y] = P(v.x, v.y);
    const built = Object.keys(v.looks).length > 0;
    px.strokeStyle = built ? '#3a5f8f' : '#7d4348';
    px.lineWidth = 1.5; px.beginPath();
    px.moveTo(cx2, cy2); px.lineTo(x, y); px.stroke();
    px.beginPath();
    if (v.lift) {
      px.save(); px.translate(x, y); px.rotate(Math.PI / 4);
      px.fillStyle = built ? '#d24dcf' : '#0a0d12';
      px.strokeStyle = '#d24dcf';
      px.fillRect(-4, -4, 8, 8); px.strokeRect(-4, -4, 8, 8);
      px.restore();
    } else {
      px.fillStyle = built ? '#9cc7ff' : '#0a0d12';
      px.strokeStyle = built ? '#9cc7ff' : '#ff9d97';
      px.arc(x, y, 4, 0, 7); px.fill(); px.stroke();
    }
    px.fillStyle = built ? '#9cc7ff' : '#ff9d97';
    px.font = '11px system-ui';
    // clamped to the canvas: long names near the right edge used to
    // vanish into the clip
    const label = n.split('.').pop();
    const lx = Math.min(cx2 + (x - cx2) * 0.62 + 3,
                        316 - px.measureText(label).width);
    px.fillText(label, lx, cy2 + (y - cy2) * 0.62 - 3);
    if (built) hits.push({ n, x, y });
  }
  // us: a green triangle carrying the heading, as the dashboard drew it
  // (yaw is a building bearing; the drawing's y runs down)
  const a2 = -st.cam.yaw;
  const T = (r, off) => [cx2 + r * Math.cos(a2 + off),
                         cy2 + r * Math.sin(a2 + off)];
  const tip = T(11, 0), l = T(8, 2.55), r2 = T(8, -2.55);
  px.fillStyle = '#3fb950'; px.beginPath();
  px.moveTo(tip[0], tip[1]); px.lineTo(l[0], l[1]);
  px.lineTo(cx2, cy2); px.lineTo(r2[0], r2[1]); px.fill();
}
const planBtn = $('planBtn');
planBtn.onclick = () => {
  const off = plan.style.display === 'none';
  plan.style.display = off ? '' : 'none';
  $('panel').style.display = 'none';
  planBtn.textContent = off ? '−' : '☰';
  planBtn.title = off ? 'hide the plan' : 'show the plan';
};
plan.addEventListener('click', e => {
  if (st.moving) return;
  const r = plan.getBoundingClientRect();
  const x = e.clientX - r.left, y = e.clientY - r.top;
  let best = null, bd = 14;
  for (const h of hits) {
    const d = Math.hypot(h.x - x, h.y - y);
    if (d < bd) { best = h; bd = d; }
  }
  if (best) openPanel(best.n);
});

function neighbours(n) {
  const out = [];
  for (const [a, b] of graph.edges) {
    if (a === n) out.push(b);
    if (b === n) out.push(a);
  }
  return out;
}
const tagOf = (n, look) => look === 'original' ? n : `${n}@${look}`;
function bearingTo(to) {
  const a = graph.vertices[st.at], b = graph.vertices[to];
  return Math.atan2(-(b.y - a.y), b.x - a.x);
}

// ---- the crossing ---------------------------------------------------------
function openPanel(to) {
  st.target = to;
  $('tgt').textContent = to;
  const sel = $('tlook');
  sel.innerHTML = '';
  for (const look of Object.keys(graph.vertices[to].looks)) {
    const o = document.createElement('option');
    o.value = look; o.textContent = look;
    sel.appendChild(o);
  }
  const key = tagOf(st.at, st.look) + '__' + tagOf(to, sel.value);
  $('tnote').textContent = graph.crossings.includes(key)
    ? 'a crossing video exists for this pair'
    : 'no crossing video yet — the walk will cut';
  sel.onchange = () => {
    const k = tagOf(st.at, st.look) + '__' + tagOf(to, sel.value);
    $('tnote').textContent = graph.crossings.includes(k)
      ? 'a crossing video exists for this pair'
      : 'no crossing video yet — the walk will cut';
  };
  $('panel').style.display = 'block';
}
$('cancelBtn').onclick = () => {
  st.target = null;
  $('panel').style.display = 'none';
};
$('goBtn').onclick = () => {
  if (!st.target || st.moving) return;
  const to = st.target, look = $('tlook').value;
  st.target = null;
  $('panel').style.display = 'none';
  // one writer of position: even our own button asks the core to move
  // the walker, and the follow loop enacts what comes back
  if (st.follow) postPosition(to, look);
  else runGo(to, look);
};

function spinTo(bearing, ms) {
  return new Promise(res => {
    const y0 = st.cam.yaw, p0 = st.cam.pitch;
    let dy = bearing - y0;
    while (dy > Math.PI) dy -= 2 * Math.PI;
    while (dy < -Math.PI) dy += 2 * Math.PI;
    const t0 = performance.now();
    const step = now => {
      const t = Math.min(1, (now - t0) / ms);
      const e = t * t * (3 - 2 * t);          // smoothstep
      st.cam.yaw = y0 + dy * e;
      st.cam.pitch = p0 * (1 - e);
      if (t < 1) requestAnimationFrame(step); else res();
    };
    requestAnimationFrame(step);
  });
}
function playVideo(url) {
  return new Promise(res => {
    const v = $('vid');
    v.src = url;
    v.onended = () => res(true);
    v.onerror = () => res(false);
    v.oncanplay = () => { v.style.opacity = '1'; v.play(); };
    v.load();
  });
}

async function runGo(to, look) {
  st.moving = true;
  const bearing = bearingTo(to);
  // the destination downloads through the WHOLE crossing — spin and video
  // both — so by the time the last frame holds, the world is waiting
  const loading = fetchRecords(to, look);
  // 1. spin in place to face the way we will walk
  st.transition = { to, look, phase: 'spin' };
  pushState(true);
  await spinTo(bearing, 800);
  // 2. the crossing video covers the canvas
  const key = tagOf(st.at, st.look) + '__' + tagOf(to, look);
  const hasVideo = graph.crossings.includes(key);
  st.transition = { to, look, phase: 'crossing' };
  pushState(true);
  if (hasVideo) {
    const vurl = await fetchVideo(key)
      || `${FILES}/.crossings/${key}/crossing.mp4`;
    await playVideo(vurl);
  } else {
    $('shade').style.opacity = '1';
    await new Promise(r => setTimeout(r, 350));
  }
  // 3. arrive: the destination world, standing at its capture point,
  //    facing the same bearing the crossing travelled. The video HOLDS
  //    its last frame — which is the destination's own view — until the
  //    splat's first sorted frame has actually been drawn beneath it,
  //    then fades: that unbroken image is the seam.
  const ab = await loading;
  const meta = graph.vertices[to].looks[look].meta;
  st.basis = basisOf(meta);
  st.cam.eye = meta.center.slice();
  st.cam.yaw = bearing;
  st.cam.pitch = 0;
  await showRecords(ab);
  await new Promise(r => requestAnimationFrame(
    () => requestAnimationFrame(r)));
  $('vid').style.opacity = '0';
  $('shade').style.opacity = '0';
  st.at = to;
  st.look = look;
  st.transition = null;
  updateBar();
  st.moving = false;
  pushState(true);
  preheat();               // the new neighbourhood warms behind you
}

function updateBar() {
  $('at').textContent = st.at;
  $('lookNote').textContent = st.look === 'original'
    ? '' : `· wearing ${st.look}`;
}

// ---- frame loop -----------------------------------------------------------
function frame() {
  if (keys.size && !st.moving) {
    // yaw grows east-toward-north (counter-clockwise), so turning LEFT
    // is yaw increasing — A adds, D subtracts
    if (keys.has('KeyA')) st.cam.yaw += 0.02;
    if (keys.has('KeyD')) st.cam.yaw -= 0.02;
    if (keys.has('KeyW')) st.cam.pitch = Math.min(1.45, st.cam.pitch + 0.01);
    if (keys.has('KeyS')) st.cam.pitch = Math.max(-1.45, st.cam.pitch - 0.01);
    if (keys.has('ArrowUp')) walk(0.05);
    if (keys.has('ArrowDown')) walk(-0.05);
    if (keys.has('ArrowLeft')) strafe(-0.02);
    if (keys.has('ArrowRight')) strafe(0.02);
  }
  if (st.basis) {
    const vm = viewMatrix();
    worker.postMessage({ view: multiply4(projection, vm) });
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    if (st.vertexCount > 0) {
      gl.uniformMatrix4fv(u_view, false, vm);
      gl.drawArraysInstanced(gl.TRIANGLE_FAN, 0, 4, st.vertexCount);
    }
  }
  drawPlan();
  pushState(false);
  requestAnimationFrame(frame);
}

// ---- boot ------------------------------------------------------------------
async function boot() {
  $('spin').style.display = 'flex';
  graph = await (await fetch(GRAPH)).json();
  const q = new URLSearchParams(location.search);
  const want = q.get('at');
  const candidates = Object.entries(graph.vertices)
    .filter(([, v]) => Object.keys(v.looks).length);
  if (!candidates.length) {
    $('at').textContent = 'no worlds built yet — generate splats first';
    $('spin').style.display = 'none';
    return;
  }
  // the core's position is the truth: adopt it when it names a built
  // world, seed it when it holds nothing yet
  let core = null;
  try {
    core = await (await fetch('/dreamworld_core/position')).json();
  } catch (e) { core = null; }
  let start, startLook = null;
  if (core && core.position && graph.vertices[core.position.at]
      && graph.vertices[core.position.at].looks[core.position.look]) {
    start = core.position.at;
    startLook = core.position.look;
    lastSeq = core.seq || 0;
  } else {
    [start] = candidates.find(([n]) => n === want) || candidates[0];
  }
  st.at = start;
  st.look = startLook || Object.keys(graph.vertices[start].looks)[0];
  if (!startLook) {
    const seeded = await postPosition(st.at, st.look);
    try { lastSeq = (await seeded.json()).seq || lastSeq; } catch (e) {}
  }
  const nbrs = neighbours(start);
  const meta = graph.vertices[start].looks[st.look].meta;
  st.basis = basisOf(meta);
  st.cam.eye = meta.center.slice();
  st.cam.yaw = nbrs.length ? bearingTo(nbrs[0]) : 0;
  updateBar();
  const ab = await fetchRecords(start, st.look);
  await showRecords(ab);
  $('spin').style.display = 'none';
  preheat();               // the rest of the building, quietly
}
frame();
boot();
