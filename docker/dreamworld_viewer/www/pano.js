// The panorama viewer, ported from main's align_panos.py: the same fragment
// shader (our convention: column c holds lon = pi - 2pi(c+0.5)/W), the same
// fov wheel. `corr` previews the roll that saving will bake into the file.
//
// Instanced, not a singleton: the alignment box and the variants box each
// run their own camera, so aiming an edit never turns the alignment. Every
// instance registers under a namespace and Python calls through
// dwp(ns, fn, arg). Two modes: the default drags the PANORAMA (off, the
// alignment turn, pitch locked, 0.12 deg/px — main's numbers); {free:true}
// drags the CAMERA (look and pitch), which is how an edit is aimed.
// {arrow:true} lets this instance drive the map's facing arrow — the
// alignment's privilege, so the edit camera never swings it.
window._dwp = window._dwp || {};
window.dwp = (ns, fn, arg) => {
  const a = window._dwp[ns];
  return a && a[fn] ? a[fn](arg) : null;
};
window.dwPano = (id, url, offId, ns, opts) => {
  // NiceGUI names its elements c<n>; this viewer uses plain ids,
  // and passing one that resolved to nothing silently rendered
  // NO panorama — which then seeded the world model with a blank
  // frame instead of the view.
  const cv = document.getElementById('c' + id)
          || document.getElementById(id);
  if (!cv || cv.dataset.dw) return;
  cv.dataset.dw = 1;
  opts = opts || {};
  // 1.2 rad, NOT an arbitrary default: it is the horizontal fov the
  // crossing videos are rendered at (crossing.py extracts 832x480 at
  // 1.2), so at rest the panorama and a crossing share one framing and
  // walking between waypoints needs no zoom at all. Q/E still adjusts;
  // a crossing then eases to 1.2 and hands the user's value back.
  const st = { off: 0, look: 0, pitch: 0, fov: 1.2, drag: null, ready: false };
  const gl = cv.getContext('webgl', { antialias: true,
                                    preserveDrawingBuffer: true });
  const VS = 'attribute vec2 p;void main(){gl_Position=vec4(p,0.0,1.0);}';
  const FS = 'precision highp float;uniform sampler2D tex;uniform vec2 res;' +
    'uniform float yaw,pitch,fov,corr;const float PI=3.14159265358979;' +
    'void main(){' +
    'vec3 F=vec3(cos(pitch)*cos(yaw),cos(pitch)*sin(yaw),sin(pitch));' +
    'vec3 R=normalize(cross(F,vec3(0.0,0.0,1.0)));vec3 U=cross(R,F);' +
    'float t=tan(fov*0.5);vec2 c=(gl_FragCoord.xy-0.5*res)/(0.5*res.x);' +
    'vec3 d=normalize(F+c.x*t*R+c.y*t*U);' +
    'float lon=atan(d.y,d.x)-corr,lat=asin(clamp(d.z,-1.0,1.0));' +
    'gl_FragColor=texture2D(tex,vec2((PI-lon)/(2.0*PI),0.5-lat/PI));}';
  const mk = (t, s) => { const o = gl.createShader(t);
    gl.shaderSource(o, s); gl.compileShader(o); return o; };
  const prog = gl.createProgram();
  gl.attachShader(prog, mk(gl.VERTEX_SHADER, VS));
  gl.attachShader(prog, mk(gl.FRAGMENT_SHADER, FS));
  gl.linkProgram(prog); gl.useProgram(prog);
  const buf = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]),
                gl.STATIC_DRAW);
  const pl = gl.getAttribLocation(prog, 'p');
  gl.enableVertexAttribArray(pl);
  gl.vertexAttribPointer(pl, 2, gl.FLOAT, false, 0, 0);
  const U = {};
  for (const n of ['res', 'yaw', 'pitch', 'fov', 'corr'])
    U[n] = gl.getUniformLocation(prog, n);
  const tex = gl.createTexture();
  gl.bindTexture(gl.TEXTURE_2D, tex);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.REPEAT);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
  const readout = () => {
    const el = document.getElementById('c' + offId);
    if (el) el.textContent = st.off.toFixed(1) + '°';
    if (!opts.arrow) return;
    // swing the map's facing arrow with the turn: the content under the
    // centre line sits at bearing look - off until the roll is saved.
    // Negated for the drawing's y-down frame.
    const ar = document.getElementById('dwface');
    if (ar) { ar.style.visibility = 'visible';
      ar.style.setProperty('--dwrot',
        (st.off - st.look * 180 / Math.PI) + 'deg'); }
  };
  // one heading per GROUP of viewers: whoever the user turns tells its
  // peers — the vertex's align and edit viewers share the default group,
  // each edge card's panorama pair shares its own — and lookAt() sets
  // without re-broadcasting, so this cannot ring. Free-look peers follow
  // pitch too; the alignment view keeps its locked horizon.
  const grp = opts.group || 'main';
  const share = () => {
    for (const k in window._dwp) {
      const a = window._dwp[k];
      if (k !== ns && a._grp === grp && a.lookAt)
        a.lookAt(st.look, st.pitch);
    }
  };
  cv.addEventListener('pointerdown', e => {
    st.drag = [e.clientX, e.clientY]; cv.setPointerCapture(e.pointerId); });
  cv.addEventListener('pointermove', e => {
    if (st.drag === null) return;
    const dx = e.clientX - st.drag[0], dy = e.clientY - st.drag[1];
    st.drag = [e.clientX, e.clientY];
    if (opts.free) {          // aim the camera: content follows the pointer
      const s = st.fov / Math.max(1, cv.clientWidth);
      st.look += dx * s;
      st.pitch = Math.min(1.45, Math.max(-1.45, st.pitch + dy * s));
      share();                // this viewer's group follows
    } else {                  // turn the panorama: the alignment offset
      st.off = (st.off + dx * 0.12 + 360) % 360;
    }
    readout(); });
  cv.addEventListener('pointerup', () => st.drag = null);
  cv.addEventListener('wheel', e => { e.preventDefault();
    st.fov = Math.max(0.5, Math.min(2.6, st.fov * (1 + e.deltaY * 0.001)));
  }, { passive: false });
  // the edit rectangle: what part of the view the edit will touch. Movable
  // by its body, resizable by its corner; stored as fractions so it renders
  // the same place through canvas resizes. Its sub-frustum IS what view()
  // reports, so the crop the model edits is exactly what the box shows.
  if (opts.rect) {
    st.rect = { fx: 0.15, fy: 0.18, fw: 0.7, fh: 0.62 };
    const rc = document.createElement('div');
    rc.style.cssText = 'position:absolute;border:1.5px dashed #4ea1ff;' +
      'background:rgba(78,161,255,.08);cursor:move;touch-action:none;' +
      'z-index:4;border-radius:3px';
    const hd = document.createElement('div');
    hd.style.cssText = 'position:absolute;right:-8px;bottom:-8px;' +
      'width:16px;height:16px;border-radius:4px;background:#4ea1ff;' +
      'cursor:nwse-resize;touch-action:none';
    rc.appendChild(hd);
    cv.parentElement.appendChild(rc);
    st.place = () => { const W = cv.clientWidth, H = cv.clientHeight;
      rc.style.left = st.rect.fx * W + 'px';
      rc.style.top = st.rect.fy * H + 'px';
      rc.style.width = st.rect.fw * W + 'px';
      rc.style.height = st.rect.fh * H + 'px'; };
    st.place();
    let mode = null, last = null;
    const grab = m => e => { mode = m; last = [e.clientX, e.clientY];
      e.stopPropagation(); e.preventDefault();
      e.target.setPointerCapture(e.pointerId); };
    rc.addEventListener('pointerdown', grab('move'));
    hd.addEventListener('pointerdown', grab('size'));
    const track = e => { if (!mode) return;
      const W = cv.clientWidth, H = cv.clientHeight, r = st.rect;
      const dx = (e.clientX - last[0]) / W, dy = (e.clientY - last[1]) / H;
      last = [e.clientX, e.clientY];
      if (mode === 'move') {
        r.fx = Math.min(1 - r.fw, Math.max(0, r.fx + dx));
        r.fy = Math.min(1 - r.fh, Math.max(0, r.fy + dy));
      } else {
        r.fw = Math.min(1 - r.fx, Math.max(0.1, r.fw + dx));
        r.fh = Math.min(1 - r.fy, Math.max(0.1, r.fh + dy));
      }
      st.place(); };
    rc.addEventListener('pointermove', track);
    hd.addEventListener('pointermove', track);
    rc.addEventListener('pointerup', () => mode = null);
    hd.addEventListener('pointerup', () => mode = null);
  }
  const im = new Image();
  im.onload = () => { gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGB, gl.RGB, gl.UNSIGNED_BYTE, im);
    st.ready = true; };
  im.src = url;
  // swapping the picture, not rebuilding the viewer: the live page keeps
  // one canvas for a whole walk, and re-calling dwPano is refused by the
  // guard above — which is why arriving somewhere new used to leave the
  // OLD panorama on screen
  st.load = (nextUrl) => { st.ready = false; im.src = nextUrl; };
  const loop = () => {
    if (!cv.isConnected) return;               // gone with its card
    const w = cv.clientWidth, h = cv.clientHeight;
    if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h;
      if (st.place) st.place(); }
    gl.viewport(0, 0, w, h);
    gl.uniform2f(U.res, w, h);
    gl.uniform1f(U.yaw, st.look); gl.uniform1f(U.pitch, st.pitch);
    gl.uniform1f(U.fov, st.fov);
    gl.uniform1f(U.corr, st.off * Math.PI / 180);
    if (st.ready) gl.drawArrays(gl.TRIANGLES, 0, 3);
    else { gl.clearColor(0.04, 0.05, 0.07, 1); gl.clear(gl.COLOR_BUFFER_BIT); }
    requestAnimationFrame(loop);
  };
  loop();
  // born facing where its group already faces
  for (const k in window._dwp) {
    const a = window._dwp[k];
    if (k !== ns && a._grp === grp && a.heading) {
      st.look = a.heading();
      break;
    }
  }
  window._dwp[ns] = {
    _grp: grp,
    face: r => { st.look = r; st.pitch = 0; readout(); share(); },
    aim: d => {                       // relative pan, both axes
      st.look += d.dyaw || 0;
      st.pitch = Math.max(-1.45, Math.min(1.45,
                          st.pitch + (d.dpitch || 0)));
      if (d.dfov) st.fov = Math.max(0.5, Math.min(2.6,
                                    st.fov * d.dfov));
      readout(); share();
    },
    nudge: d => { st.off = (st.off + d + 360) % 360; readout(); },
    zoom: f => { st.fov = Math.max(0.5, Math.min(2.6, f));
      readout(); share(); },
    heading: () => st.look,
    load: u => st.load(u),          // show a different panorama
    lookAt: (r, p) => { st.look = r;
      st.pitch = (opts.free && p != null) ? p : (opts.free ? st.pitch : 0);
      readout(); },
    // back to the last SAVED alignment: the saved roll lives in the file,
    // so discarding the pending turn is all a reset is
    reset: () => { st.off = 0; readout(); },
    off: () => st.off,
    // what to edit: the rectangle's sub-frustum when there is one — its
    // centre ray re-aimed through the same camera basis as the shader,
    // its width shrinking the fov — else the whole camera view
    view: () => {
      const W = Math.max(1, cv.clientWidth), H = Math.max(1, cv.clientHeight);
      let yaw = st.look, pitch = st.pitch, fov = st.fov, aspect = H / W;
      if (st.rect) {
        const r = st.rect, t = Math.tan(st.fov / 2);
        const ncx = (r.fx + r.fw / 2) * 2 - 1;
        const ncy = -((r.fy + r.fh / 2) * H - H / 2) / (W / 2);
        const cp = Math.cos(st.pitch), sp = Math.sin(st.pitch);
        const F = [cp * Math.cos(st.look), cp * Math.sin(st.look), sp];
        let R = [F[1], -F[0], 0];
        const rn = Math.hypot(R[0], R[1]) || 1;
        R = [R[0] / rn, R[1] / rn, 0];
        const U = [R[1] * F[2], -R[0] * F[2], R[0] * F[1] - R[1] * F[0]];
        const d = [F[0] + ncx * t * R[0] + ncy * t * U[0],
                   F[1] + ncx * t * R[1] + ncy * t * U[1],
                   F[2] + ncx * t * R[2] + ncy * t * U[2]];
        const dn = Math.hypot(d[0], d[1], d[2]) || 1;
        yaw = Math.atan2(d[1], d[0]);
        pitch = Math.asin(Math.max(-1, Math.min(1, d[2] / dn)));
        fov = 2 * Math.atan(t * r.fw);
        aspect = (r.fh * H) / (r.fw * W);
      }
      return { yaw, pitch, fov, off: st.off, aspect };
    },
  };
};
