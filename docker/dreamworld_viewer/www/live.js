// dreamworld live — stand inside a waypoint's PHOTOGRAPH and let a world
// model animate whatever you are looking at.
//
// The panorama is the ground truth and the only thing panning touches: turn
// with the keyboard and you are moving through real pixels, not generated
// ones. Stop, and the current view is captured off the canvas and handed to
// LingBot-World as its seed image, with the prompt from the box below; the
// rollout then streams over the top until the next keypress. Every rollout
// therefore starts from a true frame and never runs long enough to wander
// far from it — the drift a world model would otherwise accumulate is
// bounded by how long you hold still.

const FILES = '/dreamworld_editor/files';
const GRAPH = '/dreamworld_editor/graph';
const STREAMER = '/streamer';
const STILL_MS = 600;          // held-still before the rollout is seeded
const SEED_MS = 15000;         // a seed request that misses this is lost
const WARM_MS = 45000;         // ...and a rollout that never arrives

const $ = id => document.getElementById(id);
const st = { at: null, look: 'original', graph: null, seq: 0,
             timer: null, streaming: false, seeding: false,
             moving: false, target: null,
             // the core is the one writer of position and this page
             // follows it, exactly as the splat walkthrough does
             follow: true, warm: null };

function chip(cls, text) {
  const el = $('chip');
  el.className = cls;
  el.textContent = text;
}

// ---- the photograph -------------------------------------------------------
function showPano() {
  const v = st.graph.vertices[st.at];
  const file = (v.panos || {})[st.look];
  if (!file) { chip('off', 'no panorama here'); return; }
  const url = `${FILES}/${st.at}/${file}`;
  // first place: build the viewer. Every place after: swap the picture —
  // dwPano refuses to re-initialise a canvas it already owns.
  if (window.dwp('live', 'heading') === null)
    dwPano($('pano').id, url, -1, 'live', { free: true });
  else
    window.dwp('live', 'load', url);
  $('note').textContent = `${Object.keys(v.panos).length} look(s)`;
}

function fillPickers() {
  // where you stand is the plan's business now; the bar only reports it
  const looks = Object.keys(st.graph.vertices[st.at].panos || {});
  if (!looks.includes(st.look)) st.look = looks[0];
  $('where').textContent =
    st.at + (st.look === 'original' ? '' : ` · ${st.look}`);
}

// The seed is the view itself: full width of what is on screen and a
// centred band at the model's 832x464 aspect, so the horizontal field of
// view the viewer reports is exactly the one the rollout inherits and
// nothing is stretched to fit.
function captureView() {
  const src = $('pano');
  const out = document.createElement('canvas');
  out.width = 832; out.height = 464;
  const sw = src.width, sh = Math.min(src.height, src.width * 464 / 832);
  out.getContext('2d').drawImage(src, 0, (src.height - sh) / 2, sw, sh,
                                 0, 0, 832, 464);
  return out.toDataURL('image/jpeg', 0.92);
}

// ---- the rollout ----------------------------------------------------------
function stopStream() {
  clearTimeout(st.warm);
  st.streaming = false;
  $('live').classList.remove('on');
  // drop the connection: an MJPEG <img> keeps its socket open forever
  $('live').src = '';
}

async function seed() {
  if (st.seeding) return;
  st.seeding = true;
  chip('wait', 'seeding…');
  try {
    // the canvas IS the conditioning: exactly the framing on screen, so the
    // rollout inherits the viewer's own field of view rather than guessing
    const view = window.dwp('live', 'view') || { fov: 1.2 };
    const image = captureView();
    // a seed that never answers used to leave the chip reading
    // "seeding…" for good; give it a deadline it can miss out loud
    const ctl = new AbortController();
    const bell = setTimeout(() => ctl.abort(), SEED_MS);
    let r;
    try {
      r = await fetch(`${STREAMER}/seed`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ image, prompt: $('prompt').value,
                               fov: view.fov }),
        signal: ctl.signal
      });
    } finally { clearTimeout(bell); }
    if (!r.ok) throw new Error(await r.text());
    const doc = await r.json();
    st.seq = doc.seq;
    // cache-bust so the browser opens a NEW multipart response per rollout
    $('live').src = `${STREAMER}/stream?s=${doc.seq}`;
    st.streaming = true;
    chip('wait', 'warming up…');
    // the <img> only fires load once a frame lands. If none ever does —
    // the model is still compiling, or the rollout died — this used to
    // sit on "warming up…" for good. Give it a bound and start over.
    clearTimeout(st.warm);
    st.warm = setTimeout(() => {
      if (!st.streaming || $('live').classList.contains('on')) return;
      chip('off', 'no frames yet — starting over');
      stopStream();
      settle();
    }, WARM_MS);
  } catch (e) {
    chip('off', e.name === 'AbortError' ? 'seed timed out — retrying'
                                        : 'streamer unreachable');
    if (e.name === 'AbortError') setTimeout(settle, 1500);
  } finally {
    st.seeding = false;
  }
}

function settle() {                 // called after every camera change
  pushState();                      // where we look is part of where we are
  if (st.moving) return;            // a walk owns the screen until it lands
  stopStream();
  clearTimeout(st.timer);
  st.timer = setTimeout(seed, STILL_MS);
}

// the stream fades in only once real frames arrive, so a slow first block
// never shows a black rectangle over a perfectly good photograph
$('live').addEventListener('load', () => {
  clearTimeout(st.warm);
  if (st.streaming) { $('live').classList.add('on'); chip('on', 'live'); }
});
$('live').addEventListener('error', () => {
  if (st.streaming) chip('off', 'stream ended');
});

// ---- controls -------------------------------------------------------------
const KEYS = {
  KeyA: { dyaw: +0.06 }, ArrowLeft: { dyaw: +0.06 },
  KeyD: { dyaw: -0.06 }, ArrowRight: { dyaw: -0.06 },
  KeyW: { dpitch: +0.04 }, ArrowUp: { dpitch: +0.04 },
  KeyS: { dpitch: -0.04 }, ArrowDown: { dpitch: -0.04 },
  KeyQ: { dfov: 0.97 }, KeyE: { dfov: 1.03 },
};
const held = new Set();
addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT') return;      // the prompt box types
  if (!KEYS[e.code]) return;
  e.preventDefault();
  held.add(e.code);
});
addEventListener('keyup', e => held.delete(e.code));
addEventListener('blur', () => held.clear());

(function pan() {
  if (held.size) {
    let d = { dyaw: 0, dpitch: 0, dfov: 1 };
    for (const k of held) {
      const a = KEYS[k];
      d.dyaw += a.dyaw || 0;
      d.dpitch += a.dpitch || 0;
      d.dfov *= a.dfov || 1;
    }
    window.dwp('live', 'aim', d);
    settle();                       // panning cancels the rollout
  }
  requestAnimationFrame(pan);
})();

// dragging the panorama with the mouse is dw_pano's own; watch the heading
// and treat any change as a camera move
let lastHeading = null;
setInterval(() => {
  const v = window.dwp('live', 'view');
  if (!v) return;
  const key = `${v.yaw.toFixed(3)}|${v.pitch.toFixed(3)}|${v.fov.toFixed(3)}`;
  if (lastHeading !== null && key !== lastHeading) settle();
  lastHeading = key;
}, 150);

$('apply').onclick = () => seed();
$('prompt').addEventListener('keydown', e => {
  if (e.key === 'Enter') seed();
});



// ---- the graph, as the walkthrough reads it ------------------------------
const hasPano = n => Object.keys(
  (st.graph.vertices[n] || {}).panos || {}).length > 0;
const tagOf = (n, look) => (look === 'original' ? n : `${n}@${look}`);

function neighbours(n) {
  const out = [];
  for (const [a, b] of st.graph.edges) {
    if (a === n) out.push(b);
    if (b === n) out.push(a);
  }
  // the same lift's stops on other levels are the building's own edges
  const me = st.graph.vertices[n];
  if (me && me.lift)
    for (const [k, v] of Object.entries(st.graph.vertices))
      if (k !== n && v.lift === me.lift && v.level !== me.level) out.push(k);
  return out;
}

function bearingTo(to) {
  const a = st.graph.vertices[st.at], b = st.graph.vertices[to];
  // a lift ride is vertical: face the departing cabin's own door
  if (a.level !== b.level) return a.door_bearing || 0;
  return Math.atan2(-(b.y - a.y), b.x - a.x);
}

function crossingSecs(to) {
  const a = st.graph.vertices[st.at], b = st.graph.vertices[to];
  let d;
  if (a.level !== b.level) {
    const e = l => (st.graph.levels[l] || {}).elevation || 0;
    d = Math.abs(e(b.level) - e(a.level));
  } else {
    const sc = (st.graph.levels[a.level] || {}).scale || 0.05;
    d = Math.hypot(b.x - a.x, b.y - a.y) * sc;
  }
  return Math.min(15, Math.max(2.5, d / 1.4));
}

// ---- the plan -------------------------------------------------------------
const plan = $('plan'), planBox = $('planbox'), px = plan.getContext('2d');
let hits = [];

function drawPlan() {
  if (!st.graph || !st.at || planBox.style.display === 'none') return;
  const me = st.graph.vertices[st.at];
  const L = st.graph.levels[me.level] || { walls: [] };
  const nbrs = neighbours(st.at);
  const far = Math.max(40, ...nbrs.map(n => {
    const v = st.graph.vertices[n];
    return Math.hypot(v.x - me.x, v.y - me.y);
  })) * 1.4;
  const W = plan.width, H = plan.height;
  const cx = W / 2, cy = H / 2, R = Math.min(W, H) / 2 - 15;
  const P = (x, y) => [cx + (x - me.x) / far * R, cy + (y - me.y) / far * R];
  px.clearRect(0, 0, W, H);
  px.strokeStyle = '#3a4757'; px.lineWidth = 2; px.beginPath();
  for (const w of L.walls) {
    const a = P(w[0], w[1]), b = P(w[2], w[3]);
    px.moveTo(a[0], a[1]); px.lineTo(b[0], b[1]);
  }
  px.stroke();
  hits = [];
  for (const n of nbrs) {
    const v = st.graph.vertices[n];
    if (v.level !== me.level) continue;
    const [x, y] = P(v.x, v.y);
    const ok = hasPano(n);            // a place you can STAND, here
    px.strokeStyle = ok ? '#3a5f8f' : '#7d4348';
    px.lineWidth = 1.5; px.beginPath();
    px.moveTo(cx, cy); px.lineTo(x, y); px.stroke();
    px.beginPath();
    if (v.lift) {
      px.save(); px.translate(x, y); px.rotate(Math.PI / 4);
      px.fillStyle = ok ? '#d24dcf' : '#0a0d12';
      px.strokeStyle = '#d24dcf';
      px.fillRect(-4, -4, 8, 8); px.strokeRect(-4, -4, 8, 8);
      px.restore();
    } else {
      px.fillStyle = ok ? '#9cc7ff' : '#0a0d12';
      px.strokeStyle = ok ? '#9cc7ff' : '#ff9d97';
      px.arc(x, y, 4, 0, 7); px.fill(); px.stroke();
    }
    px.fillStyle = ok ? '#9cc7ff' : '#ff9d97';
    px.font = '11px system-ui';
    const label = n.split('.').pop();
    const lx = Math.min(cx + (x - cx) * 0.62 + 3,
                        W - 4 - px.measureText(label).width);
    px.fillText(label, lx, cy + (y - cy) * 0.62 - 3);
    if (ok) hits.push({ n, x, y });
  }
  // standing in a lift: its stops on other levels, drawn as the editor
  // draws them — vertical arrows, one per level, clickable like any way out
  if (me.lift) {
    const elev = l => (st.graph.levels[l] || {}).elevation || 0;
    const mates = nbrs.filter(n => st.graph.vertices[n].level !== me.level)
      .sort((a, b) => elev(st.graph.vertices[a].level)
                    - elev(st.graph.vertices[b].level));
    let up = 0, dn = 0;
    px.font = '11px system-ui'; px.textAlign = 'left';
    for (const n of mates) {
      const v = st.graph.vertices[n];
      const rising = elev(v.level) > elev(me.level);
      const y = rising ? cy - 28 - 22 * up++ : cy + 28 + 22 * dn++;
      const ok = hasPano(n);
      px.fillStyle = ok ? '#d24dcf' : '#5a4458';
      px.beginPath();
      if (rising) { px.moveTo(cx, y - 7); px.lineTo(cx - 6, y + 4);
                    px.lineTo(cx + 6, y + 4); }
      else { px.moveTo(cx, y + 7); px.lineTo(cx - 6, y - 4);
             px.lineTo(cx + 6, y - 4); }
      px.closePath(); px.fill();
      px.fillText(v.level, cx + 10, y + 4);
      if (ok) hits.push({ n, x: cx, y });
    }
    px.textAlign = 'start';
  }
  // us: a triangle carrying the heading the panorama is actually showing
  const v = window.dwp('live', 'view');
  const a2 = -((v && v.yaw) || 0);
  const T = (r, off) => [cx + r * Math.cos(a2 + off), cy + r * Math.sin(a2 + off)];
  const tip = T(11, 0), l = T(8, 2.55), r2 = T(8, -2.55);
  px.fillStyle = '#4ea1ff'; px.beginPath();
  px.moveTo(tip[0], tip[1]); px.lineTo(l[0], l[1]);
  px.lineTo(cx, cy); px.lineTo(r2[0], r2[1]); px.fill();
}
setInterval(drawPlan, 120);          // the heading moves as you pan

new ResizeObserver(() => {
  const w = Math.max(160, planBox.clientWidth | 0);
  const h = Math.max(120, planBox.clientHeight | 0);
  if (plan.width !== w || plan.height !== h) { plan.width = w; plan.height = h; }
  $('panel').style.top = (planBox.offsetTop + planBox.offsetHeight + 10) + 'px';
  drawPlan();
}).observe(planBox);

(() => {                              // the plan's own resize grip
  const grip = $('plangrip'); let d = null;
  grip.addEventListener('pointerdown', e => {
    e.preventDefault(); grip.setPointerCapture(e.pointerId);
    const r = planBox.getBoundingClientRect(); d = { right: r.right, top: r.top };
  });
  grip.addEventListener('pointermove', e => {
    if (!d) return;
    planBox.style.width = Math.max(160, d.right - e.clientX) + 'px';
    planBox.style.height = Math.max(120, e.clientY - d.top) + 'px';
  });
  grip.addEventListener('pointerup', () => { d = null; });
})();

$('planBtn').onclick = () => {
  const off = planBox.style.display === 'none';
  planBox.style.display = off ? '' : 'none';
  $('panel').style.display = 'none';
  $('planBtn').textContent = off ? '−' : '☰';
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

function openPanel(to) {
  st.target = to;
  $('tgt').textContent = to;
  const looks = Object.keys(st.graph.vertices[to].panos || {});
  $('tlook').innerHTML = looks.map(l =>
    `<option${l === 'original' ? ' selected' : ''}>${l}</option>`).join('');
  const key = tagOf(st.at, st.look) + '__' + tagOf(to, looks[0]);
  $('tnote').textContent = (st.graph.crossings || []).includes(key)
    ? 'a crossing video carries this walk' : 'no crossing video — it will cut';
  $('panel').style.display = 'block';
}
$('cancelBtn').onclick = () => { $('panel').style.display = 'none'; };
$('goBtn').onclick = () => {
  $('panel').style.display = 'none';
  if (!st.target) return;
  const to = st.target, look = $('tlook').value;
  st.target = null;
  // one writer of position: while synced, even our own button asks the
  // core to move the walker and the follow loop enacts what comes back,
  // so the harness and this page never hold different beliefs
  if (st.follow) postPosition(to, look);
  else walkTo(to, look);
};

// ---- crossing an edge -----------------------------------------------------
function spinTo(bearing, ms) {
  return new Promise(res => {
    const from = window.dwp('live', 'heading') || 0;
    let d = bearing - from;
    while (d > Math.PI) d -= 2 * Math.PI;
    while (d < -Math.PI) d += 2 * Math.PI;
    const t0 = performance.now();
    (function step() {
      const t = Math.min(1, (performance.now() - t0) / ms);
      const e = t < 0.5 ? 2 * t * t : -1 + (4 - 2 * t) * t;   // ease in-out
      window.dwp('live', 'face', from + d * e);
      if (t < 1) requestAnimationFrame(step); else res();
    })();
  });
}

function playVideo(url, secs) {
  return new Promise(res => {
    const v = $('vid');
    v.src = url;
    v.onended = () => res(true);
    v.onerror = () => res(false);
    v.oncanplay = () => {
      if (secs && v.duration)
        v.playbackRate = Math.min(1.8, Math.max(0.6, v.duration / secs));
      v.style.opacity = '1';
      v.play();
    };
    v.load();
  });
}

// The crossing videos are 832x480 frames extracted at 1.2 rad horizontal
// fov and drawn with object-fit COVER — which crops by whichever dimension
// must give. The panorama sits at the USER'S fov (1.6 by default), so a
// straight cut into the video read as a zoom-in, and the cut back out as a
// zoom-out. This is the video's fov as it lands on THIS window: match the
// panorama to it before the video fades in, and hand the user's own fov
// back once the crossing is done.
function coverFov() {
  const w = Math.max(1, innerWidth), h = Math.max(1, innerHeight);
  const scale = Math.max(w / 832, h / 480);
  return 2 * Math.atan(Math.tan(0.6) * (w / scale) / 832);
}

function fovTo(target, ms) {
  return new Promise(res => {
    const from = (window.dwp('live', 'view') || { fov: 1.2 }).fov;
    if (Math.abs(target - from) < 0.02) return res();   // already there
    const t0 = performance.now();
    (function step() {
      const t = Math.min(1, (performance.now() - t0) / ms);
      const e = t < 0.5 ? 2 * t * t : -1 + (4 - 2 * t) * t;
      window.dwp('live', 'zoom', from + (target - from) * e);
      if (t < 1) requestAnimationFrame(step); else res();
    })();
  });
}

async function walkTo(to, look) {
  if (st.moving) return;
  st.moving = true;
  clearTimeout(st.timer);
  stopStream();                       // the rollout belongs to where we were
  chip('', 'walking…');
  const bearing = bearingTo(to);
  const myFov = (window.dwp('live', 'view') || { fov: 1.2 }).fov;
  await spinTo(bearing, 700);         // 1. turn to face the way we go
  const key = tagOf(st.at, st.look) + '__' + tagOf(to, look);
  const crossing = (st.graph.crossings || []).includes(key);
  if (crossing) {
    await fovTo(coverFov(), 320);     // meet the video's framing first
    await playVideo(`${FILES}/.crossings/${key}/crossing.mp4`,
                    crossingSecs(to));   // 2. the crossing carries the walk
  } else {
    $('shade').style.opacity = '1';
    await new Promise(r => setTimeout(r, 350));
  }
  st.at = to; st.look = look;         // 3. arrive, facing the way we walked
  $('prompt').value = '';             // that prompt was about where we were
  fillPickers();
  showPano();
  window.dwp('live', 'face', bearing);
  await new Promise(r => requestAnimationFrame(
    () => requestAnimationFrame(r)));
  $('vid').style.opacity = '0';
  $('shade').style.opacity = '0';
  if (crossing) await fovTo(myFov, 400);   // your zoom, given back
  st.moving = false;
  drawPlan();
  pushState(true);
  settle();                           // and the new view comes alive
}

// ---- the core: the one writer of position ---------------------------------
// The splat walkthrough already follows dreamworld_core, so the live page
// follows it the same way rather than inventing a second belief about
// where the walker stands. The core holds the position; the harness (or
// our own go button) moves it; we notice the sequence advance and enact
// it — a spin and a crossing for a neighbour, a cut for anywhere else.
// The chip is the toggle: click to walk free, click again to catch up.
let lastSeq = 0, coreOk = 0, lastSent = '', lastPush = 0;

function coreMark() {
  const el = $('core');
  if (!el) return;
  if (!st.follow) {
    el.textContent = '○ core unsynced';
    el.className = '';
    return;
  }
  const live = performance.now() - coreOk < 3000;
  el.textContent = live ? '● core synced' : '○ core unreachable';
  el.className = live ? 'on' : 'off';
}
setInterval(coreMark, 1000);

function postPosition(at, look) {
  return fetch('/dreamworld_core/position', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ at, look }) }).catch(() => {});
}

function pushState(force) {
  if (!st.graph || !st.at) return;
  const now = performance.now();
  if (!force && now - lastPush < 250) return;
  const me = st.graph.vertices[st.at];
  const view = window.dwp('live', 'view') || { yaw: 0, pitch: 0 };
  const doc = {
    at: st.at, look: st.look, level: me.level, x: me.x, y: me.y,
    lift: me.lift || null,
    yaw_deg: Math.round(view.yaw * 1800 / Math.PI) / 10,
    pitch_deg: Math.round(view.pitch * 1800 / Math.PI) / 10,
    moving: st.moving,
    // what this page is, so the harness can tell the two walkers apart
    surface: 'live',
    hidden: document.hidden,
  };
  const s = JSON.stringify(doc);
  if (!force && s === lastSent && now - lastPush < 1000) return;
  lastSent = s;
  lastPush = now;
  hb.postMessage(s);
}

function followTruth(pos, seq) {
  if (!st.follow || st.moving || !st.graph) return;
  // a seq far BELOW ours means the core restarted and began a new count —
  // resync rather than gate out every command until this tab reloads
  if (seq < lastSeq - 100) lastSeq = seq - 1;
  if (!(seq > lastSeq)) return;
  lastSeq = seq;
  enact(pos.at, pos.look, pos.yaw_deg);
}

async function enact(to, look, yawDeg) {
  const v = st.graph.vertices[to];
  if (!v || !(v.panos || {})[look]) return;
  if (to === st.at && look === st.look) {
    // the same place: this is a TURN, which the panorama can simply do
    if (yawDeg != null) {
      st.moving = true;
      await spinTo(yawDeg * Math.PI / 180, 600);
      st.moving = false;
      pushState(true);
      settle();
    }
    return;
  }
  if (neighbours(st.at).includes(to)) await walkTo(to, look);
  else await jumpTo(to, look);
}

// somewhere the walk did not carry us: a cut, not a crossing, because
// pretending otherwise would animate a journey nobody took
async function jumpTo(to, look) {
  st.moving = true;
  clearTimeout(st.timer);
  stopStream();
  chip('', 'moving…');
  $('shade').style.opacity = '1';
  await new Promise(r => setTimeout(r, 320));
  st.at = to; st.look = look;
  $('prompt').value = '';
  fillPickers();
  showPano();
  await new Promise(r => requestAnimationFrame(
    () => requestAnimationFrame(r)));
  $('shade').style.opacity = '0';
  st.moving = false;
  drawPlan();
  pushState(true);
  settle();
}

// The heartbeat lives in a WORKER: page timers throttle to one a minute in
// a hidden tab, and this page hidden must not read as this walker dead.
const hb = new Worker(URL.createObjectURL(new Blob([
  // an absolute url, baked in: a blob worker's base is the blob itself,
  // and "/dreamworld_core/..." against that is not a url at all — it
  // threw once a second into the empty catch below, which is a quiet way
  // for a heartbeat to not exist
  "const URL_=" + JSON.stringify(
    location.origin + "/dreamworld_core/viewer/state") + ";" +
  "let body=null;" +
  "onmessage=e=>{body=e.data};" +
  "setInterval(()=>{if(!body)return;" +
  "fetch(URL_,{method:'POST'," +
  "headers:{'Content-Type':'application/json'},body:body})" +
  ".then(async r=>{if(r.ok)postMessage(await r.json())})" +
  ".catch(()=>{})},1000)"], { type: 'application/javascript' })));
hb.onmessage = e => {
  if (!e.data) return;
  coreOk = performance.now();
  if (e.data.position) followTruth(e.data.position, e.data.seq);
  coreMark();
};
document.addEventListener('visibilitychange', () => pushState(true));

$('core').onclick = async () => {
  st.follow = !st.follow;
  coreMark();
  if (!st.follow || st.moving) return;
  // rejoining catches up by CUT — wherever the core went while we were
  // free is not a walk we took, so a fade says so honestly
  try {
    const doc = await (await fetch('/dreamworld_core/position')).json();
    lastSeq = doc.seq || 0;
    const p2 = doc.position;
    if (p2 && (p2.at !== st.at || p2.look !== st.look)
        && st.graph.vertices[p2.at]
        && (st.graph.vertices[p2.at].panos || {})[p2.look]) {
      await jumpTo(p2.at, p2.look);
    }
  } catch (e) {}
};

// ---- boot -----------------------------------------------------------------
(async () => {
  st.graph = await (await fetch(GRAPH)).json();
  const want = new URLSearchParams(location.search).get('at');
  const withPano = Object.keys(st.graph.vertices)
    .filter(n => Object.keys(st.graph.vertices[n].panos || {}).length).sort();
  st.at = st.graph.vertices[want] && withPano.includes(want)
    ? want : withPano[0];
  if (!st.at) { chip('off', 'no panoramas in this project'); return; }
  // the core already knows where the walker stands; start there rather
  // than at whichever vertex sorts first
  try {
    const doc = await (await fetch('/dreamworld_core/position')).json();
    lastSeq = doc.seq || 0;
    const p2 = doc.position;
    if (p2 && st.graph.vertices[p2.at]
        && (st.graph.vertices[p2.at].panos || {})[p2.look]) {
      st.at = p2.at; st.look = p2.look;
    }
  } catch (e) {}
  fillPickers();
  showPano();
  coreMark();
  pushState(true);
  // say whether the model is even up before the first still moment
  try {
    const h = await (await fetch(`${STREAMER}/health`)).json();
    chip(h.status === 'ok' ? '' : 'wait',
         { ok: 'streamer ready', warming: 'streamer warming up…' }[h.status]
         || 'streamer loading…');
  } catch (e) { chip('off', 'streamer offline'); }
  settle();
})();
