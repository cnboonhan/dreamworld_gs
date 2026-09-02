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
const LINGBOT = '/lingbot';
const STILL_MS = 600;          // held-still before the rollout is seeded

const $ = id => document.getElementById(id);
const st = { at: null, look: 'original', graph: null, seq: 0,
             timer: null, streaming: false, seeding: false };

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
  dwPano($('pano').id, `${FILES}/${st.at}/${file}`, -1, 'live',
         { free: true });
  $('note').textContent = `${Object.keys(v.panos).length} look(s)`;
}

function fillPickers() {
  const where = $('where'), look = $('look');
  const names = Object.keys(st.graph.vertices)
    .filter(n => Object.keys(st.graph.vertices[n].panos || {}).length)
    .sort();
  where.innerHTML = names.map(n =>
    `<option${n === st.at ? ' selected' : ''}>${n}</option>`).join('');
  const looks = Object.keys(st.graph.vertices[st.at].panos || {});
  if (!looks.includes(st.look)) st.look = looks[0];
  look.innerHTML = looks.map(l =>
    `<option${l === st.look ? ' selected' : ''}>${l}</option>`).join('');
}

// ---- the rollout ----------------------------------------------------------
function stopStream() {
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
    const view = window.dwp('live', 'view') || { fov: 1.6 };
    const image = $('pano').toDataURL('image/jpeg', 0.92);
    const r = await fetch(`${LINGBOT}/seed`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ image, prompt: $('prompt').value,
                             fov: view.fov })
    });
    if (!r.ok) throw new Error(await r.text());
    const doc = await r.json();
    st.seq = doc.seq;
    // cache-bust so the browser opens a NEW multipart response per rollout
    $('live').src = `${LINGBOT}/stream?s=${doc.seq}`;
    st.streaming = true;
    chip('wait', 'warming up…');
  } catch (e) {
    chip('off', 'lingbot unreachable');
  } finally {
    st.seeding = false;
  }
}

function settle() {                 // called after every camera change
  stopStream();
  clearTimeout(st.timer);
  st.timer = setTimeout(seed, STILL_MS);
}

// the stream fades in only once real frames arrive, so a slow first block
// never shows a black rectangle over a perfectly good photograph
$('live').addEventListener('load', () => {
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

$('where').onchange = e => {
  st.at = e.target.value;
  fillPickers(); showPano(); settle();
};
$('look').onchange = e => { st.look = e.target.value; showPano(); settle(); };
$('apply').onclick = () => seed();
$('prompt').addEventListener('keydown', e => {
  if (e.key === 'Enter') seed();
});
$('freeze').onclick = () => { clearTimeout(st.timer); stopStream();
                              chip('', 'frozen'); };

// ---- boot -----------------------------------------------------------------
(async () => {
  st.graph = await (await fetch(GRAPH)).json();
  const want = new URLSearchParams(location.search).get('at');
  const withPano = Object.keys(st.graph.vertices)
    .filter(n => Object.keys(st.graph.vertices[n].panos || {}).length).sort();
  st.at = st.graph.vertices[want] && withPano.includes(want)
    ? want : withPano[0];
  if (!st.at) { chip('off', 'no panoramas in this project'); return; }
  fillPickers();
  showPano();
  // say whether the model is even up before the first still moment
  try {
    const h = await (await fetch(`${LINGBOT}/health`)).json();
    chip(h.status === 'ok' ? '' : 'wait',
         h.status === 'ok' ? 'lingbot ready' : 'lingbot loading…');
  } catch (e) { chip('off', 'lingbot offline'); }
  settle();
})();
