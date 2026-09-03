"""Push one project's assets to a PRIVATE HuggingFace dataset repo.

The project tree is the only part of this stack that cannot be
re-downloaded: the panoramas shot at each waypoint, the worlds built from
them, the crossing videos between them, and the map they all hang off.
Weights come from the hub with `just fetch`, so nothing here uploads a
model.

    python scripts/push_project.py <assets-dir> <project> <owner> [--public]

The repo is created PRIVATE unless --public is passed, and the flag is
deliberately explicit: these are photographs of a real building.
"""
import argparse
import sys
from pathlib import Path

from huggingface_hub import HfApi

# regenerable from what else is uploaded, and a straight duplicate of it
SKIP = ["bundle/*", "**/.ipynb_checkpoints/*"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("assets")
    ap.add_argument("project")
    ap.add_argument("owner")
    ap.add_argument("--public", action="store_true",
                    help="create the repo public (default: private)")
    a = ap.parse_args()

    folder = Path(a.assets).resolve() / "projects" / a.project
    if not folder.is_dir():
        raise SystemExit(f"no such project: {folder}")
    repo_id = f"{a.owner}/dreamworld-{a.project.replace('_', '-')}"

    api = HfApi()
    who = api.whoami()["name"]
    size = sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())
    print(f"as {who}: {folder} ({size / 1e9:.1f} GB) -> {repo_id} "
          f"({'PUBLIC' if a.public else 'private'})", flush=True)

    api.create_repo(repo_id, repo_type="dataset", private=not a.public,
                    exist_ok=True)
    api.upload_large_folder(repo_id=repo_id, repo_type="dataset",
                            folder_path=str(folder), ignore_patterns=SKIP)
    print(f"done: https://huggingface.co/datasets/{repo_id}", flush=True)


if __name__ == "__main__":
    main()
