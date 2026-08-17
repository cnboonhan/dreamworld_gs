// The edge transition, scaffolded from main's mid-corridor handover: the
// camera walks out of world A along its spawn forward, the two canvases
// crossfade through the middle of the walk, and the camera arrives into
// world B from behind its spawn. What main earned with marks and fitted
// walk lines — the REAL corridor, in each splat's own frame — this stands
// in for with a nominal straight line, until splat-to-building placement
// arrives. The shape of the crossing is already the right one.
//
// Instanced like the pano viewers: an edge shows one walk per direction,
// so each registers under a namespace and Python drives it through
// dwWalk(ns, fn, arg).
window.dwWalk = (ns, fn, arg) => {
  const w = window._dww && window._dww[ns];
  return w && w[fn] ? w[fn](arg) : null;
};
window.dwWalkInit = (ns, aId, bId, aNs, bNs, dist) => {
  const a = document.getElementById('c' + aId);
  const b = document.getElementById('c' + bId);
  if (!a || !b) return;
  const setT = t => {
    t = Math.max(0, Math.min(1, t));
    const wa = window._dws && window._dws[aNs];
    const wb = window._dws && window._dws[bNs];
    if (wa) wa.offset(t * dist);
    if (wb) wb.offset((t - 1) * dist);
    const alpha = Math.max(0, Math.min(1, (t - 0.35) / 0.3));
    a.style.opacity = String(1 - alpha);
    b.style.opacity = String(alpha);
  };
  let anim = null;
  window._dww = window._dww || {};
  window._dww[ns] = {
    t: setT,
    play: secs => {
      if (anim) cancelAnimationFrame(anim);
      const t0 = performance.now();
      const step = now => {
        const t = (now - t0) / (secs * 1000);
        setT(t);
        if (t < 1 && a.isConnected) anim = requestAnimationFrame(step);
        else anim = null;
      };
      anim = requestAnimationFrame(step);
    },
  };
  setT(0);
};
