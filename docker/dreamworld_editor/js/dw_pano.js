// The panorama viewer, ported from main's align_panos.py: the same fragment
// shader (our convention: column c holds lon = pi - 2pi(c+0.5)/W), the same
// drag-to-turn at 0.12 degrees per pixel, the same fov wheel. `corr` previews
// the roll that saving will bake into the file. Controls stay in Python;
// this only renders, turns, and answers dwPanoOff() when asked.
window.dwPano = (id, url, offId) => {
  const cv = document.getElementById('c' + id);
  if (!cv || cv.dataset.dw) return;
  cv.dataset.dw = 1;
  const st = { off: 0, look: 0, pitch: 0, fov: 1.6, drag: null, ready: false };
  const gl = cv.getContext('webgl', { antialias: true });
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
  const readout = () => { const el = document.getElementById('c' + offId);
    if (el) el.textContent = st.off.toFixed(1) + '°'; };
  cv.addEventListener('pointerdown', e => {
    st.drag = e.clientX; cv.setPointerCapture(e.pointerId); });
  cv.addEventListener('pointermove', e => { if (st.drag === null) return;
    st.off = (st.off + (e.clientX - st.drag) * 0.12 + 360) % 360;
    st.drag = e.clientX; readout(); });
  cv.addEventListener('pointerup', () => st.drag = null);
  cv.addEventListener('wheel', e => { e.preventDefault();
    st.fov = Math.max(0.5, Math.min(2.6, st.fov * (1 + e.deltaY * 0.001)));
  }, { passive: false });
  const im = new Image();
  im.onload = () => { gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGB, gl.RGB, gl.UNSIGNED_BYTE, im);
    st.ready = true; };
  im.src = url;
  const loop = () => {
    if (!cv.isConnected) return;               // gone with its card
    const w = cv.clientWidth, h = cv.clientHeight;
    if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
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
  window.dwPanoFace = r => { st.look = r; st.pitch = 0; };
  window.dwPanoNudge = d => { st.off = (st.off + d + 360) % 360; readout(); };
  window.dwPanoOff = () => st.off;
};
