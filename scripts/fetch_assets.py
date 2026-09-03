"""Download every model the pipeline needs into assets/.

Idempotent, and cheaply so: a repo already in the cache is recognised without a
network call, so running this again on a complete box takes about three seconds
rather than revalidating half a terabyte against the hub. Interrupted downloads
still resume — a partial cache is treated as absent, not present.

Usage:
    python scripts/fetch_assets.py <assets-dir>
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def already_here(repo: str):
    """The cached snapshot of `repo`, or None if it needs downloading.

    A local-only resolve answers this in microseconds and without touching the
    network, where a plain snapshot_download revalidates every file against the
    hub — which on a 54 GB repo is a long way to go to be told nothing changed.
    On a box with no route to huggingface.co it is also the difference between
    starting and hanging.

    An interrupted download leaves .incomplete blobs behind and a snapshot that
    resolves but is short of files, so both are checked: a partial cache must
    look like a miss, not a hit.
    """
    from huggingface_hub import snapshot_download

    try:
        path = Path(snapshot_download(repo, local_files_only=True))
    except Exception:                       # noqa: BLE001 — any failure means fetch it
        return None
    if list(path.parent.parent.glob("blobs/*.incomplete")):
        return None
    if any(f.is_symlink() and not f.exists() for f in path.rglob("*")):
        return None
    return path


def fetch_hf(assets: Path) -> None:
    from huggingface_hub import snapshot_download

    repos = [
        line.strip()
        for line in (HERE / "models.txt").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    for repo in repos:
        here = already_here(repo)
        if here is not None:
            size = sum(f.stat().st_size for f in here.rglob("*") if f.is_file())
            print(f"    {repo:44} have it ({size / 1e9:.1f} GB)", flush=True)
            continue
        print(f"==> {repo}", flush=True)
        snapshot_download(repo)


def extract_sam3_image(src: Path, dst: Path) -> None:
    """Derive a standalone SAM 3 image model from the SAM 3 video packaging.

    HY-World's trajectory planner loads `Sam3Model`/`Sam3Processor`, but the
    only ungated distribution of SAM 3 (ModelScope facebook/sam3) ships the
    *video* packaging: config with nested detector_config/tracker_config,
    weights prefixed detector_model.*/tracker_model.*. This promotes the
    detector half into a standalone transformers repo the image classes load.
    """
    from safetensors import safe_open
    from safetensors.torch import save_file

    prefix = "detector_model."
    dst.mkdir(parents=True, exist_ok=True)

    cfg = json.load(open(src / "config.json"))
    det = cfg["detector_config"]
    det["architectures"] = ["Sam3Model"]
    det.setdefault("dtype", cfg.get("dtype", "float32"))
    json.dump(det, open(dst / "config.json", "w"), indent=2)

    tensors = {}
    with safe_open(src / "model.safetensors", framework="pt") as f:
        for k in f.keys():
            if k.startswith(prefix):
                tensors[k[len(prefix):]] = f.get_tensor(k)
    save_file(tensors, str(dst / "model.safetensors"))

    for name in ("tokenizer.json", "tokenizer_config.json",
                 "special_tokens_map.json", "vocab.json", "merges.txt"):
        if (src / name).exists():
            shutil.copy2(src / name, dst / name)

    # the packaged tokenizer_config asks for the slow CLIPTokenizer, which
    # transformers 5.2 cannot build against tokenizers>=0.23
    tc_path = dst / "tokenizer_config.json"
    if tc_path.exists():
        tc = json.load(open(tc_path))
        tc["tokenizer_class"] = "CLIPTokenizerFast"
        json.dump(tc, open(tc_path, "w"), indent=2)

    json.dump({"image_processor_type": "Sam3ImageProcessorFast",
               "processor_class": "Sam3Processor"},
              open(dst / "preprocessor_config.json", "w"), indent=2)

    print(f"    extracted {len(tensors)} tensors -> {dst}")


def fetch_sam3(assets: Path) -> None:
    """SAM 3: the HuggingFace repo is gated, ModelScope serves it ungated."""
    video_dir = assets / "models" / "sam3_video"
    image_dir = assets / "models" / "sam3_image"

    if not (video_dir / "model.safetensors").exists():
        print("==> facebook/sam3 (ModelScope)", flush=True)
        from modelscope import snapshot_download as ms_download

        ms_download("facebook/sam3", local_dir=str(video_dir))

    # upstream distributes only the video packaging; the trajectory planner
    # loads Sam3Model/Sam3Processor, so derive that variant from it
    if not (image_dir / "model.safetensors").exists():
        print("==> deriving sam3_image", flush=True)
        extract_sam3_image(video_dir, image_dir)


def main() -> None:
    assets = Path(sys.argv[1]).resolve()
    (assets / "models").mkdir(parents=True, exist_ok=True)
    fetch_hf(assets)
    fetch_sam3(assets)
    total = subprocess.run(["du", "-sh", str(assets)], capture_output=True,
                           text=True).stdout.split()[0]
    print(f"assets ready: {total}")


if __name__ == "__main__":
    main()
