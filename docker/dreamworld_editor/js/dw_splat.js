// A minimal 3DGS viewer, ported from main's splat viewer (itself of
// antimatter15/splat heritage): the same PLY -> 32-byte records, the same
// counting-sort worker and packed-covariance texture, the same shaders and
// front-to-back blending. What is NOT here is everything main built around
// it — tours, handovers, caching. This renders one world and lets you look:
// drag turns, wheel walks forward and back, shift-drag pans.
//
// The camera opens on the scene's own world.cam.json when the export wrote
// one, so a world starts the right way up.

function dwSplatWorker(self) {
  let buffer;
  let vertexCount = 0;
  let viewProj;
  const rowLength = 3 * 4 + 3 * 4 + 4 + 4;
  let lastProj = [];
  let lastVertexCount = 0;

  const _floatView = new Float32Array(1);
  const _int32View = new Int32Array(_floatView.buffer);
  function floatToHalf(f0) {
    _floatView[0] = f0;
    const f = _int32View[0];
    const sign = (f >> 31) & 0x0001;
    const exp = (f >> 23) & 0x00ff;
    let frac = f & 0x007fffff;
    let newExp;
    if (exp === 0) newExp = 0;
    else if (exp < 113) {
      newExp = 0;
      frac |= 0x00800000;
      frac = frac >> (113 - exp);
      if (frac & 0x01000000) { newExp = 1; frac = 0; }
    } else if (exp < 142) newExp = exp - 112;
    else { newExp = 31; frac = 0; }
    return (sign << 15) | (newExp << 10) | (frac >> 13);
  }
  const packHalf2x16 = (x, y) =>
    (floatToHalf(x) | (floatToHalf(y) << 16)) >>> 0;

  function generateTexture() {
    if (!buffer) return;
    const f_buffer = new Float32Array(buffer);
    const u_buffer = new Uint8Array(buffer);
    const texwidth = 1024 * 2;
    const texheight = Math.ceil((2 * vertexCount) / texwidth);
    const texdata = new Uint32Array(texwidth * texheight * 4);
    const texdata_c = new Uint8Array(texdata.buffer);
    const texdata_f = new Float32Array(texdata.buffer);
    for (let i = 0; i < vertexCount; i++) {
      texdata_f[8 * i + 0] = f_buffer[8 * i + 0];
      texdata_f[8 * i + 1] = f_buffer[8 * i + 1];
      texdata_f[8 * i + 2] = f_buffer[8 * i + 2];
      texdata_c[4 * (8 * i + 7) + 0] = u_buffer[32 * i + 24 + 0];
      texdata_c[4 * (8 * i + 7) + 1] = u_buffer[32 * i + 24 + 1];
      texdata_c[4 * (8 * i + 7) + 2] = u_buffer[32 * i + 24 + 2];
      texdata_c[4 * (8 * i + 7) + 3] = u_buffer[32 * i + 24 + 3];
      const scale = [f_buffer[8 * i + 3], f_buffer[8 * i + 4],
                     f_buffer[8 * i + 5]];
      const rot = [(u_buffer[32 * i + 28 + 0] - 128) / 128,
                   (u_buffer[32 * i + 28 + 1] - 128) / 128,
                   (u_buffer[32 * i + 28 + 2] - 128) / 128,
                   (u_buffer[32 * i + 28 + 3] - 128) / 128];
      const M = [
        1.0 - 2.0 * (rot[2] * rot[2] + rot[3] * rot[3]),
        2.0 * (rot[1] * rot[2] + rot[0] * rot[3]),
        2.0 * (rot[1] * rot[3] - rot[0] * rot[2]),
        2.0 * (rot[1] * rot[2] - rot[0] * rot[3]),
        1.0 - 2.0 * (rot[1] * rot[1] + rot[3] * rot[3]),
        2.0 * (rot[2] * rot[3] + rot[0] * rot[1]),
        2.0 * (rot[1] * rot[3] + rot[0] * rot[2]),
        2.0 * (rot[2] * rot[3] - rot[0] * rot[1]),
        1.0 - 2.0 * (rot[1] * rot[1] + rot[2] * rot[2]),
      ].map((k, j) => k * scale[Math.floor(j / 3)]);
      const sigma = [
        M[0] * M[0] + M[3] * M[3] + M[6] * M[6],
        M[0] * M[1] + M[3] * M[4] + M[6] * M[7],
        M[0] * M[2] + M[3] * M[5] + M[6] * M[8],
        M[1] * M[1] + M[4] * M[4] + M[7] * M[7],
        M[1] * M[2] + M[4] * M[5] + M[7] * M[8],
        M[2] * M[2] + M[5] * M[5] + M[8] * M[8],
      ];
      texdata[8 * i + 4] = packHalf2x16(4 * sigma[0], 4 * sigma[1]);
      texdata[8 * i + 5] = packHalf2x16(4 * sigma[2], 4 * sigma[3]);
      texdata[8 * i + 6] = packHalf2x16(4 * sigma[4], 4 * sigma[5]);
    }
    self.postMessage({ texdata, texwidth, texheight }, [texdata.buffer]);
  }

  function runSort(viewProj) {
    if (!buffer) return;
    const f_buffer = new Float32Array(buffer);
    if (lastVertexCount === vertexCount) {
      const dot = lastProj[2] * viewProj[2] + lastProj[6] * viewProj[6] +
        lastProj[10] * viewProj[10];
      if (Math.abs(dot - 1) < 0.01) return;
    } else {
      generateTexture();
      lastVertexCount = vertexCount;
    }
    let maxDepth = -Infinity, minDepth = Infinity;
    const sizeList = new Int32Array(vertexCount);
    for (let i = 0; i < vertexCount; i++) {
      const depth = ((viewProj[2] * f_buffer[8 * i + 0] +
        viewProj[6] * f_buffer[8 * i + 1] +
        viewProj[10] * f_buffer[8 * i + 2]) * 4096) | 0;
      sizeList[i] = depth;
      if (depth > maxDepth) maxDepth = depth;
      if (depth < minDepth) minDepth = depth;
    }
    const depthInv = (256 * 256 - 1) / (maxDepth - minDepth);
    const counts0 = new Uint32Array(256 * 256);
    for (let i = 0; i < vertexCount; i++) {
      sizeList[i] = ((sizeList[i] - minDepth) * depthInv) | 0;
      counts0[sizeList[i]]++;
    }
    const starts0 = new Uint32Array(256 * 256);
    for (let i = 1; i < 256 * 256; i++)
      starts0[i] = starts0[i - 1] + counts0[i - 1];
    const depthIndex = new Uint32Array(vertexCount);
    for (let i = 0; i < vertexCount; i++)
      depthIndex[starts0[sizeList[i]]++] = i;
    lastProj = viewProj;
    self.postMessage({ depthIndex, viewProj, vertexCount },
                     [depthIndex.buffer]);
  }

  function processPlyBuffer(inputBuffer) {
    const ubuf = new Uint8Array(inputBuffer);
    const header = new TextDecoder().decode(ubuf.slice(0, 1024 * 10));
    const header_end = "end_header\n";
    const header_end_index = header.indexOf(header_end);
    if (header_end_index < 0) throw new Error("no ply header");
    const vertexCount = parseInt(/element vertex (\d+)\n/.exec(header)[1]);
    let row_offset = 0;
    const offsets = {}, types = {};
    const TYPE_MAP = { double: "getFloat64", int: "getInt32",
      uint: "getUint32", float: "getFloat32", short: "getInt16",
      ushort: "getUint16", uchar: "getUint8" };
    for (const prop of header.slice(0, header_end_index).split("\n")
        .filter((k) => k.startsWith("property "))) {
      const [, type, pname] = prop.split(" ");
      const arrayType = TYPE_MAP[type] || "getInt8";
      types[pname] = arrayType;
      offsets[pname] = row_offset;
      row_offset += parseInt(arrayType.replace(/[^\d]/g, "")) / 8;
    }
    const dataView = new DataView(inputBuffer,
                                  header_end_index + header_end.length);
    let row = 0;
    const attrs = new Proxy({}, {
      get(target, prop) {
        if (!types[prop]) throw new Error(prop + " not found");
        return dataView[types[prop]](row * row_offset + offsets[prop], true);
      },
    });
    const sizeList = new Float32Array(vertexCount);
    const sizeIndex = new Uint32Array(vertexCount);
    for (row = 0; row < vertexCount; row++) {
      sizeIndex[row] = row;
      if (!types["scale_0"]) continue;
      const size = Math.exp(attrs.scale_0) * Math.exp(attrs.scale_1) *
        Math.exp(attrs.scale_2);
      const opacity = 1 / (1 + Math.exp(-attrs.opacity));
      sizeList[row] = size * opacity;
    }
    sizeIndex.sort((b, a) => sizeList[a] - sizeList[b]);
    const buffer = new ArrayBuffer(rowLength * vertexCount);
    for (let j = 0; j < vertexCount; j++) {
      row = sizeIndex[j];
      const position = new Float32Array(buffer, j * rowLength, 3);
      const scales = new Float32Array(buffer, j * rowLength + 12, 3);
      const rgba = new Uint8ClampedArray(buffer, j * rowLength + 24, 4);
      const rot = new Uint8ClampedArray(buffer, j * rowLength + 28, 4);
      if (types["scale_0"]) {
        const qlen = Math.sqrt(attrs.rot_0 ** 2 + attrs.rot_1 ** 2 +
          attrs.rot_2 ** 2 + attrs.rot_3 ** 2);
        rot[0] = (attrs.rot_0 / qlen) * 128 + 128;
        rot[1] = (attrs.rot_1 / qlen) * 128 + 128;
        rot[2] = (attrs.rot_2 / qlen) * 128 + 128;
        rot[3] = (attrs.rot_3 / qlen) * 128 + 128;
        scales[0] = Math.exp(attrs.scale_0);
        scales[1] = Math.exp(attrs.scale_1);
        scales[2] = Math.exp(attrs.scale_2);
      } else {
        scales[0] = scales[1] = scales[2] = 0.01;
        rot[0] = 255; rot[1] = rot[2] = rot[3] = 0;
      }
      position[0] = attrs.x;
      position[1] = attrs.y;
      position[2] = attrs.z;
      if (types["f_dc_0"]) {
        const SH_C0 = 0.28209479177387814;
        rgba[0] = (0.5 + SH_C0 * attrs.f_dc_0) * 255;
        rgba[1] = (0.5 + SH_C0 * attrs.f_dc_1) * 255;
        rgba[2] = (0.5 + SH_C0 * attrs.f_dc_2) * 255;
      } else {
        rgba[0] = attrs.red; rgba[1] = attrs.green; rgba[2] = attrs.blue;
      }
      rgba[3] = types["opacity"]
        ? (1 / (1 + Math.exp(-attrs.opacity))) * 255 : 255;
    }
    return buffer;
  }

  let sortRunning;
  const throttledSort = () => {
    if (!sortRunning) {
      sortRunning = true;
      const lastView = viewProj;
      runSort(lastView);
      setTimeout(() => {
        sortRunning = false;
        if (lastView !== viewProj) throttledSort();
      }, 0);
    }
  };

  self.onmessage = (e) => {
    if (e.data.ply) {
      vertexCount = 0;
      buffer = processPlyBuffer(e.data.ply);
      vertexCount = Math.floor(buffer.byteLength / rowLength);
      self.postMessage({ vertexCount });
    } else if (e.data.view) {
      viewProj = e.data.view;
      throttledSort();
    }
  };
}

window.dwSplat = (id, plyUrl, camUrl, ns) => {
  const cv = document.getElementById('c' + id);
  if (!cv || cv.dataset.dw) return;
  cv.dataset.dw = 1;

  // main's loading marker, ported with its stylesheet: the blue cube
  // turns while the ply downloads and unpacks, and leaves with the
  // first sorted frame
  if (!document.getElementById('dwsp-style')) {
    const st = document.createElement('style');
    st.id = 'dwsp-style';
    st.textContent = `
.dwsp-spin{position:absolute;inset:0;display:flex;align-items:center;
  justify-content:center;pointer-events:none;z-index:3}
.dwsp-spin .cube-wrapper{transform-style:preserve-3d}
.dwsp-spin .cube{transform-style:preserve-3d;
  transform:rotateX(45deg) rotateZ(45deg);animation:dwsp-rot 2s infinite}
.dwsp-spin .cube-faces{transform-style:preserve-3d;height:40px;width:40px;
  position:relative;transform-origin:0 0;
  transform:translateX(0) translateY(0) translateZ(-20px)}
.dwsp-spin .cube-face{position:absolute;inset:0;background:#0017ff;
  border:solid 1px #ffffff}
.dwsp-spin .cube-face.top{transform:translateZ(40px)}
.dwsp-spin .cube-face.front{transform-origin:0 50%;transform:rotateY(-90deg)}
.dwsp-spin .cube-face.back{transform-origin:0 50%;
  transform:rotateY(-90deg) translateZ(-40px)}
.dwsp-spin .cube-face.right{transform-origin:50% 0;
  transform:rotateX(-90deg) translateY(-40px)}
.dwsp-spin .cube-face.left{transform-origin:50% 0;
  transform:rotateX(-90deg) translateY(-40px) translateZ(40px)}
@keyframes dwsp-rot{
  0%{transform:rotateX(45deg) rotateY(0) rotateZ(45deg);
    animation-timing-function:cubic-bezier(.17,.84,.44,1)}
  50%{transform:rotateX(45deg) rotateY(0) rotateZ(225deg);
    animation-timing-function:cubic-bezier(.76,.05,.86,.06)}
  100%{transform:rotateX(45deg) rotateY(0) rotateZ(405deg)}}`;
    document.head.appendChild(st);
  }
  const wrap = cv.parentElement;
  if (getComputedStyle(wrap).position === 'static')
    wrap.style.position = 'relative';
  const spinner = document.createElement('div');
  spinner.className = 'dwsp-spin';
  spinner.innerHTML =
    '<div class="cube-wrapper"><div class="cube"><div class="cube-faces">' +
    ['bottom', 'top', 'left', 'right', 'back', 'front'].map(
      f => `<div class="cube-face ${f}"></div>`).join('') +
    '</div></div></div>';
  wrap.appendChild(spinner);

  const VERT = `#version 300 es
precision highp float;
precision highp int;
uniform highp usampler2D u_texture;
uniform mat4 projection, view;
uniform vec2 focal;
uniform vec2 viewport;
in vec2 position;
in int index;
out vec4 vColor;
out vec2 vPosition;
void main () {
  uvec4 cen = texelFetch(u_texture, ivec2((uint(index) & 0x3ffu) << 1, uint(index) >> 10), 0);
  vec4 cam = view * vec4(uintBitsToFloat(cen.xyz), 1);
  vec4 pos2d = projection * cam;
  float clip = 1.2 * pos2d.w;
  if (pos2d.z < -clip || pos2d.x < -clip || pos2d.x > clip || pos2d.y < -clip || pos2d.y > clip) {
    gl_Position = vec4(0.0, 0.0, 2.0, 1.0);
    return;
  }
  uvec4 cov = texelFetch(u_texture, ivec2(((uint(index) & 0x3ffu) << 1) | 1u, uint(index) >> 10), 0);
  vec2 u1 = unpackHalf2x16(cov.x), u2 = unpackHalf2x16(cov.y), u3 = unpackHalf2x16(cov.z);
  mat3 Vrk = mat3(u1.x, u1.y, u2.x, u1.y, u2.y, u3.x, u2.x, u3.x, u3.y);
  mat3 J = mat3(
    focal.x / cam.z, 0., -(focal.x * cam.x) / (cam.z * cam.z),
    0., -focal.y / cam.z, (focal.y * cam.y) / (cam.z * cam.z),
    0., 0., 0.);
  mat3 T = transpose(mat3(view)) * J;
  mat3 cov2d = transpose(T) * Vrk * T;
  float mid = (cov2d[0][0] + cov2d[1][1]) / 2.0;
  float radius = length(vec2((cov2d[0][0] - cov2d[1][1]) / 2.0, cov2d[0][1]));
  float lambda1 = mid + radius, lambda2 = mid - radius;
  if (lambda2 < 0.0) return;
  vec2 diagonalVector = normalize(vec2(cov2d[0][1], lambda1 - cov2d[0][0]));
  vec2 majorAxis = min(sqrt(2.0 * lambda1), 1024.0) * diagonalVector;
  vec2 minorAxis = min(sqrt(2.0 * lambda2), 1024.0) * vec2(diagonalVector.y, -diagonalVector.x);
  vColor = clamp(pos2d.z/pos2d.w+1.0, 0.0, 1.0) * vec4((cov.w) & 0xffu, (cov.w >> 8) & 0xffu, (cov.w >> 16) & 0xffu, (cov.w >> 24) & 0xffu) / 255.0;
  vPosition = position;
  vec2 vCenter = vec2(pos2d) / pos2d.w;
  gl_Position = vec4(vCenter + position.x * majorAxis / viewport
                             + position.y * minorAxis / viewport, 0.0, 1.0);
}`;
  const FRAG = `#version 300 es
precision highp float;
in vec4 vColor;
in vec2 vPosition;
out vec4 fragColor;
void main () {
  float A = -dot(vPosition, vPosition);
  if (A < -4.0) discard;
  float B = exp(A) * vColor.a;
  fragColor = vec4(B * vColor.rgb, B);
}`;

  const invert4 = (a) => {
    let b00 = a[0] * a[5] - a[1] * a[4], b01 = a[0] * a[6] - a[2] * a[4],
      b02 = a[0] * a[7] - a[3] * a[4], b03 = a[1] * a[6] - a[2] * a[5],
      b04 = a[1] * a[7] - a[3] * a[5], b05 = a[2] * a[7] - a[3] * a[6],
      b06 = a[8] * a[13] - a[9] * a[12], b07 = a[8] * a[14] - a[10] * a[12],
      b08 = a[8] * a[15] - a[11] * a[12], b09 = a[9] * a[14] - a[10] * a[13],
      b10 = a[9] * a[15] - a[11] * a[13], b11 = a[10] * a[15] - a[11] * a[14];
    const det = b00 * b11 - b01 * b10 + b02 * b09 + b03 * b08 - b04 * b07
      + b05 * b06;
    if (!det) return null;
    return [
      (a[5] * b11 - a[6] * b10 + a[7] * b09) / det,
      (a[2] * b10 - a[1] * b11 - a[3] * b09) / det,
      (a[13] * b05 - a[14] * b04 + a[15] * b03) / det,
      (a[10] * b04 - a[9] * b05 - a[11] * b03) / det,
      (a[6] * b08 - a[4] * b11 - a[7] * b07) / det,
      (a[0] * b11 - a[2] * b08 + a[3] * b07) / det,
      (a[14] * b02 - a[12] * b05 - a[15] * b01) / det,
      (a[8] * b05 - a[10] * b02 + a[11] * b01) / det,
      (a[4] * b10 - a[5] * b08 + a[7] * b06) / det,
      (a[1] * b08 - a[0] * b10 - a[3] * b06) / det,
      (a[12] * b04 - a[13] * b02 + a[15] * b00) / det,
      (a[9] * b02 - a[8] * b04 - a[11] * b00) / det,
      (a[5] * b07 - a[4] * b09 - a[6] * b06) / det,
      (a[0] * b09 - a[1] * b07 + a[2] * b06) / det,
      (a[13] * b01 - a[12] * b03 - a[14] * b00) / det,
      (a[8] * b03 - a[9] * b01 + a[10] * b00) / det,
    ];
  };
  const rotate4 = (a, rad, x, y, z) => {
    const len = Math.hypot(x, y, z);
    x /= len; y /= len; z /= len;
    const s = Math.sin(rad), c = Math.cos(rad), t = 1 - c;
    const b00 = x * x * t + c, b01 = y * x * t + z * s, b02 = z * x * t - y * s,
      b10 = x * y * t - z * s, b11 = y * y * t + c, b12 = z * y * t + x * s,
      b20 = x * z * t + y * s, b21 = y * z * t - x * s, b22 = z * z * t + c;
    return [
      a[0] * b00 + a[4] * b01 + a[8] * b02,
      a[1] * b00 + a[5] * b01 + a[9] * b02,
      a[2] * b00 + a[6] * b01 + a[10] * b02,
      a[3] * b00 + a[7] * b01 + a[11] * b02,
      a[0] * b10 + a[4] * b11 + a[8] * b12,
      a[1] * b10 + a[5] * b11 + a[9] * b12,
      a[2] * b10 + a[6] * b11 + a[10] * b12,
      a[3] * b10 + a[7] * b11 + a[11] * b12,
      a[0] * b20 + a[4] * b21 + a[8] * b22,
      a[1] * b20 + a[5] * b21 + a[9] * b22,
      a[2] * b20 + a[6] * b21 + a[10] * b22,
      a[3] * b20 + a[7] * b21 + a[11] * b22,
      ...a.slice(12, 16),
    ];
  };
  const translate4 = (a, x, y, z) => [
    ...a.slice(0, 12),
    a[0] * x + a[4] * y + a[8] * z + a[12],
    a[1] * x + a[5] * y + a[9] * z + a[13],
    a[2] * x + a[6] * y + a[10] * z + a[14],
    a[3] * x + a[7] * y + a[11] * z + a[15],
  ];
  const multiply4 = (a, b) => {
    const out = new Array(16);
    for (let i = 0; i < 4; i++)
      for (let j = 0; j < 4; j++)
        out[4 * i + j] = b[4 * i + 0] * a[j] + b[4 * i + 1] * a[4 + j] +
          b[4 * i + 2] * a[8 + j] + b[4 * i + 3] * a[12 + j];
    return out;
  };
  const getProjectionMatrix = (fx, fy, width, height) => {
    const znear = 0.1, zfar = 200;
    return [
      (2 * fx) / width, 0, 0, 0,
      0, -(2 * fy) / height, 0, 0,
      0, 0, zfar / (zfar - znear), 1,
      0, 0, -(zfar * znear) / (zfar - znear), 0,
    ];
  };

  const worker = new Worker(URL.createObjectURL(new Blob(
    ['(', dwSplatWorker.toString(), ')(self)'],
    { type: 'application/javascript' })));

  const gl = cv.getContext('webgl2', { antialias: false });
  const mk = (t, s) => { const o = gl.createShader(t);
    gl.shaderSource(o, s); gl.compileShader(o);
    if (!gl.getShaderParameter(o, gl.COMPILE_STATUS))
      console.error(gl.getShaderInfoLog(o));
    return o; };
  const program = gl.createProgram();
  gl.attachShader(program, mk(gl.VERTEX_SHADER, VERT));
  gl.attachShader(program, mk(gl.FRAGMENT_SHADER, FRAG));
  gl.linkProgram(program); gl.useProgram(program);
  gl.disable(gl.DEPTH_TEST);
  gl.enable(gl.BLEND);
  gl.blendFuncSeparate(gl.ONE_MINUS_DST_ALPHA, gl.ONE,
                       gl.ONE_MINUS_DST_ALPHA, gl.ONE);
  gl.blendEquationSeparate(gl.FUNC_ADD, gl.FUNC_ADD);

  const u_projection = gl.getUniformLocation(program, 'projection');
  const u_viewport = gl.getUniformLocation(program, 'viewport');
  const u_focal = gl.getUniformLocation(program, 'focal');
  const u_view = gl.getUniformLocation(program, 'view');

  const vertexBuffer = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, vertexBuffer);
  gl.bufferData(gl.ARRAY_BUFFER,
                new Float32Array([-2, -2, 2, -2, 2, 2, -2, 2]),
                gl.STATIC_DRAW);
  const a_position = gl.getAttribLocation(program, 'position');
  gl.enableVertexAttribArray(a_position);
  gl.vertexAttribPointer(a_position, 2, gl.FLOAT, false, 0, 0);

  const texture = gl.createTexture();
  gl.bindTexture(gl.TEXTURE_2D, texture);
  gl.uniform1i(gl.getUniformLocation(program, 'u_texture'), 0);

  const indexBuffer = gl.createBuffer();
  const a_index = gl.getAttribLocation(program, 'index');
  gl.enableVertexAttribArray(a_index);
  gl.bindBuffer(gl.ARRAY_BUFFER, indexBuffer);
  gl.vertexAttribIPointer(a_index, 1, gl.INT, false, 0, 0);
  gl.vertexAttribDivisor(a_index, 1);

  // a straight-ahead default; world.cam.json replaces it when present
  let viewMatrix = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 1.5, 1];
  let baseView = viewMatrix;
  let projectionMatrix = [];
  let vertexCount = 0;

  const resize = () => {
    const w = cv.clientWidth || 1, h = cv.clientHeight || 1;
    const f = 0.785 * h;                       // ~65 degrees vertical
    gl.uniform2fv(u_focal, new Float32Array([f, f]));
    projectionMatrix = getProjectionMatrix(f, f, w, h);
    gl.uniform2fv(u_viewport, new Float32Array([w, h]));
    cv.width = w; cv.height = h;
    gl.viewport(0, 0, w, h);
    gl.uniformMatrix4fv(u_projection, false, projectionMatrix);
  };
  resize();

  worker.onmessage = (e) => {
    if (e.data.texdata) {
      const { texdata, texwidth, texheight } = e.data;
      gl.bindTexture(gl.TEXTURE_2D, texture);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA32UI, texwidth, texheight, 0,
                    gl.RGBA_INTEGER, gl.UNSIGNED_INT, texdata);
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, texture);
    } else if (e.data.depthIndex) {
      gl.bindBuffer(gl.ARRAY_BUFFER, indexBuffer);
      gl.bufferData(gl.ARRAY_BUFFER, e.data.depthIndex, gl.DYNAMIC_DRAW);
      vertexCount = e.data.vertexCount;
      if (vertexCount > 0) spinner.remove();
    } else if (e.data.vertexCount) {
      vertexCount = 0;               // texture arrives with the first sort
    }
  };

  // drag looks, shift-drag pans, wheel walks — all in the camera's own frame
  let drag = null;
  cv.addEventListener('pointerdown', e => {
    drag = [e.clientX, e.clientY, e.shiftKey];
    cv.setPointerCapture(e.pointerId); });
  cv.addEventListener('pointermove', e => {
    if (!drag) return;
    const dx = e.clientX - drag[0], dy = e.clientY - drag[1];
    let inv = invert4(viewMatrix);
    if (drag[2]) inv = translate4(inv, -dx * 0.005, -dy * 0.005, 0);
    else {
      inv = rotate4(inv, dx * 0.003, 0, 1, 0);
      inv = rotate4(inv, dy * 0.003, 1, 0, 0);
    }
    viewMatrix = invert4(inv);
    drag = [e.clientX, e.clientY, drag[2]]; });
  cv.addEventListener('pointerup', () => drag = null);
  cv.addEventListener('wheel', e => { e.preventDefault();
    let inv = invert4(viewMatrix);
    inv = translate4(inv, 0, 0, e.deltaY * -0.003);
    viewMatrix = invert4(inv); }, { passive: false });

  fetch(camUrl).then(r => r.ok ? r.json() : null).then(cam => {
    // make_spawn_cam writes {"viewMatrix": [...16]}; accept a bare array too
    const m = Array.isArray(cam) ? cam : cam && cam.viewMatrix;
    if (Array.isArray(m) && m.length === 16) {
      viewMatrix = m;
      baseView = m;
    }
  }).catch(() => {});
  // a named instance can be walked from outside: offset(z) places the
  // camera z units along the spawn view's own forward axis — what the
  // edge transition drives
  if (ns) {
    window._dws = window._dws || {};
    window._dws[ns] = {
      offset: z => {
        viewMatrix = invert4(translate4(invert4(baseView), 0, 0, z));
      },
    };
  }
  fetch(plyUrl).then(r => {
    if (!r.ok) throw new Error(r.status);
    return r.arrayBuffer();
  }).then(ab => worker.postMessage({ ply: ab }, [ab]))
    .catch(err => console.error('splat load failed', err));

  const frame = () => {
    if (!cv.isConnected) { worker.terminate(); return; }
    if (cv.width !== cv.clientWidth) resize();
    const viewProj = multiply4(projectionMatrix, viewMatrix);
    worker.postMessage({ view: viewProj });
    // alpha ZERO, as main clears: front-to-back blending scales every
    // splat by ONE_MINUS_DST_ALPHA, and a framebuffer cleared to alpha 1
    // multiplies them all by nothing — the whole canvas stays black.
    // The dark ground is the canvas CSS background behind it.
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    if (vertexCount > 0) {
      gl.uniformMatrix4fv(u_view, false, viewMatrix);
      gl.drawArraysInstanced(gl.TRIANGLE_FAN, 0, 4, vertexCount);
    }
    requestAnimationFrame(frame);
  };
  frame();
};
