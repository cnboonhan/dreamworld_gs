// Paced panorama upload. The stock uploader fires the whole file as one
// full-rate burst, and a burst that big starves an SSH tunnel's keepalives
// until VS Code declares the connection dead. nginx meters what the box
// SENDS (limit_rate on the files route), but no server knob can slow down
// what a browser sends — pacing has to happen here, at the sender. So the
// file goes up in small slices with silence between them: the tunnel
// breathes during the gaps, and the same bytes arrive a few seconds later.
async function dwUpload(input, vertex) {
  const f = input.files && input.files[0];
  if (!f) return;
  const out = document.getElementById('dwup-' + vertex);
  const say = (t) => { if (out) out.textContent = t; };
  const CHUNK = 2 * 1024 * 1024;      // one slice
  const GAP = 300;                    // ms of silence between slices
  const total = Math.max(1, Math.ceil(f.size / CHUNK));
  const id = Math.random().toString(36).slice(2, 10);
  // data-replace="1" on the input turns the upload into a replacement:
  // the route then overwrites the existing panorama and resets alignment
  const replace = input.dataset.replace === '1' ? '&replace=1' : '';
  input.disabled = true;
  try {
    for (let i = 0; i < total; i++) {
      const r = await fetch('upload_pano?vertex=' + encodeURIComponent(vertex)
                            + '&id=' + id + '&seq=' + i
                            + '&last=' + (i === total - 1 ? 1 : 0)
                            + '&name=' + encodeURIComponent(f.name) + replace,
                            { method: 'POST',
                              body: f.slice(i * CHUNK, (i + 1) * CHUNK) });
      if (!r.ok) throw new Error(await r.text());
      say('uploading '
          + (Math.min((i + 1) * CHUNK, f.size) / 1e6).toFixed(1)
          + ' / ' + (f.size / 1e6).toFixed(1) + ' MB');
      if (i < total - 1) await new Promise((res) => setTimeout(res, GAP));
    }
    say('saved ' + (f.size / 1e6).toFixed(1) + ' MB — processing…');
  } catch (e) {
    say('upload failed: ' + e.message);
    input.disabled = false;
    input.value = '';
  }
}
