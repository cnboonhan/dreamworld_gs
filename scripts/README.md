# scripts/

Host-side helpers (these do *not* run inside the containers).

| File | Purpose |
| --- | --- |
| `extract_sam3_image.py` | Derives the SAM 3 **image** model from the video packaging that ModelScope distributes. Called by `just fetch-sam3`. |
|  `HYWORLD_REF` (Dockerfile) | Pinned as the `HYWORLD_REF` build arg in `docker/splat-generator/splat-generator.Dockerfile`. |
|  `../docker/splat-generator/hyworld.patch` | Every change we make to that tree, applied during the image build (excludes the vendored recastnavigation submodule and the glm clone the build adds). |

## Where HY-World comes from

The generator image clones it at build time (pinned commit, submodules, then
`patch -p1 < hyworld.patch`) — see `docker/splat-generator/splat-generator.Dockerfile`.
Nothing is vendored in the repo. To work against a different upstream commit:

```bash
docker build --build-arg HYWORLD_REF=<sha> \
    -f docker/splat-generator/splat-generator.Dockerfile \
    -t dreamworld/splat-generator:latest docker/splat-generator
```

If the patch stops applying against a newer commit, regenerate it by diffing a
pristine clone against a patched one.

## What the patch fixes

- SAM 3 model paths come from `SAM3_IMAGE_DIR` / `SAM3_VIDEO_DIR` instead of
  the gated `facebook/sam3` repo id (three call sites; the planner needs the
  image model, the memory bank needs the video model).
- `HF_CACHE_DIR` respects `HF_HOME` rather than hardcoding `~/.cache`.
- `local_files_only` is threaded into `_load_transformer` — upstream drops it
  there, so the sharded checkpoint load ignored offline mode.
- The base model repo id is resolved to its local snapshot directory when
  running offline: some of its subfolders (e.g. `tokenizer/`) have no
  `config.json`, and transformers hard-fails on the missing file offline
  instead of falling back as it does online.
