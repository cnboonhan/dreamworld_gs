// Pan and zoom, the one interaction NiceGUI does not carry: a CSS transform
// on the drawing inside a clipping box. Wheel pans (a touchpad's two-finger
// scroll), pinch or ctrl+wheel zooms toward the cursor (a touchpad pinch
// arrives as exactly that), dragging pans, double-click refits. offsetX-based
// hit-testing is computed in the element's local frame, so click handlers
// keep working untouched under any transform.
//
// NO pointer capture here: capturing on pointerdown redirects the matching
// mouseup away from the image element, and the click handler — which needs
// both halves to tell a click from a pan — never hears the second one. The
// drag listens on the window instead, swapping any previous pair out first
// so refreshes don't stack them.
window.dwView = (id, w, h, key) => {
  const box = document.getElementById('c' + id);
  if (!box || box.dataset.dw) return;
  box.dataset.dw = 1;
  const kid = box.firstElementChild;
  // the imageless interactive_image has NO intrinsic size — just an
  // aspect-ratio and width:100%, so it lays out as wide as this box and
  // the fit math scales garbage. Pin its width and the aspect-ratio makes
  // layout size equal declared size, which the math assumes.
  kid.style.width = w + 'px';
  kid.style.transformOrigin = '0 0';
  let k = 1, px = 0, py = 0, drag = null;
  // every refresh rebuilds this element, so the view lives OUTSIDE it,
  // remembered per level: selecting a vertex must not lose your zoom
  const mem = window._dwvMem = window._dwvMem || {};
  const apply = () => { kid.style.transform =
    `translate(${px}px,${py}px) scale(${k})`; mem[key] = { k, px, py }; };
  const fit = () => { const r = box.getBoundingClientRect();
    k = Math.min(r.width / w, r.height / h);
    px = (r.width - w * k) / 2; py = (r.height - h * k) / 2; apply(); };
  if (mem[key]) { ({ k, px, py } = mem[key]); apply(); } else fit();
  box.addEventListener('wheel', e => { e.preventDefault();
    if (e.ctrlKey || e.metaKey) {
      const r = box.getBoundingClientRect();
      const cx = e.clientX - r.left, cy = e.clientY - r.top;
      const nk = Math.min(20, Math.max(0.05, k * Math.exp(-e.deltaY * 0.01)));
      px = cx - (cx - px) * nk / k; py = cy - (cy - py) * nk / k; k = nk;
    } else { px -= e.deltaX; py -= e.deltaY; }
    apply(); }, {passive: false});
  box.addEventListener('pointerdown', e => {
    // in move mode the drag belongs to the vertex, not the pan
    if (box.style.cursor === 'move') return;
    drag = [e.clientX, e.clientY]; });
  if (window._dwvMove) {
    removeEventListener('pointermove', window._dwvMove);
    removeEventListener('pointerup', window._dwvUp);
  }
  window._dwvMove = e => { if (!drag || !box.isConnected) return;
    px += e.clientX - drag[0]; py += e.clientY - drag[1];
    drag = [e.clientX, e.clientY]; apply(); };
  window._dwvUp = () => drag = null;
  addEventListener('pointermove', window._dwvMove);
  addEventListener('pointerup', window._dwvUp);
  box.addEventListener('dblclick', fit);
};
