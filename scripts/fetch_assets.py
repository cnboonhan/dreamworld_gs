"""Download every model the pipeline needs into assets/.

Idempotent: already-complete downloads are skipped, interrupted ones resume.

Usage:
    python scripts/fetch_assets.py <assets-dir>
"""

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def fetch_hf(assets: Path) -> None:
    from huggingface_hub import snapshot_download

    repos = [
        line.strip()
        for line in (HERE / "models.txt").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    for repo in repos:
        print(f"==> {repo}", flush=True)
        snapshot_download(repo)


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
        subprocess.run(
            [sys.executable, str(HERE / "extract_sam3_image.py"),
             str(video_dir), str(image_dir)],
            check=True,
        )


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
