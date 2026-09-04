"""Prefect flow: 360 panorama -> navigable 3DGS world + Isaac Sim USDZ.

Each pipeline stage is a task, so the Prefect UI shows per-stage status,
timing and logs, and a failed run can be retried from the stage that broke
rather than from the top.

Two ways to run it:

    python flow.py serve                 # long-running; waits for job submissions
    python flow.py run <scene-dir> [--gpus N] [--steps N]    # one-shot, local

`serve` is what the container does by default; `just generate` submits to it.
"""

from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

from prefect import flow, get_run_logger, task

WORLDGEN = Path("/opt/hyworld/hyworld2/worldgen")
TOOLS = Path("/opt/tools")
DEPLOYMENT = "dreamworld"


def sh(cmd: list[str], logger) -> None:
    """Run a command in the worldgen dir, streaming output into Prefect logs."""
    logger.info("$ %s", " ".join(str(c) for c in cmd))
    proc = subprocess.Popen(
        [str(c) for c in cmd], cwd=WORLDGEN,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    for line in proc.stdout:
        logger.info(line.rstrip())
    if proc.wait() != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {cmd[0]}")


def torchrun(gpus: int, script: str, *args) -> list[str]:
    return ["torchrun", "--nproc_per_node", str(gpus), script, *args]


@task(name="1. trajectory planning")
def plan_trajectory(scene: str, llm: dict) -> None:
    """WorldNav: VLM picks targets, SAM 3 segments them, navmesh plans paths."""
    sh(["python", "traj_generate.py", "--target_path", scene,
        "--llm_addr", llm["addr"], "--llm_port", llm["port"],
        "--llm_name", llm["name"],
        "--apply_nav_traj", "--apply_up_route", "--apply_recon_iteration",
        "--force_vlm"], get_run_logger())


@task(name="2. trajectory rendering")
def render_trajectories(scene: str, gpus: int, llm: dict) -> None:
    sh(torchrun(gpus, "traj_render.py", "--target_path", scene,
                "--llm_addr", llm["addr"], "--llm_port", llm["port"],
                "--llm_name", llm["name"]), get_run_logger())


@task(name="3. world expansion")
def expand_world(scene: str, gpus: int) -> None:
    """WorldStereo 2.0 generates consistent keyframes along the trajectories.

    --local_files_only: diffusers' sharded-checkpoint loader raises under
    HF_HUB_OFFLINE instead of falling back to the local cache.
    """
    sh(torchrun(gpus, "video_gen.py", "--target_path", scene,
                "--fsdp", "--local_files_only"), get_run_logger())


@task(name="4. gaussian training data")
def build_gs_data(scene: str, gpus: int) -> None:
    sh(torchrun(gpus, "gen_gs_data.py", "--root_path", scene,
                "--save_normal", "--split_sky"), get_run_logger())


@task(name="5. 3DGS training")
def train_gaussians(scene: str, steps: int) -> None:
    """Note: no --antialiased. AA-trained opacities only render correctly in
    renderers applying the same compensation; classic keeps the PLY portable
    to SuperSplat, web viewers and Isaac."""
    half = steps // 2
    sh(["python", "-m", "world_gs_trainer", "default",
        "--data_dir", f"{scene}/gs_data", "--result_dir", f"{scene}/gs_result",
        "--max_steps", steps, "--save_steps", steps, "--eval_steps", steps,
        "--ply_steps", steps, "--save_ply", "--disable_video",
        "--use_scale_regularization", "--depth_loss", "--normal_loss",
        "--sky_depth_from_pcd", "--use_mask_gaussian", "--mask_export_stochastic",
        "--no-mask-export-anchor-protection", "--use_anchor_protection",
        "--strategy.refine-start-iter", 200, "--strategy.refine-stop-iter", half,
        "--strategy.refine-every", 100, "--strategy.refine-scale2d-stop-iter", half,
        "--strategy.reset-every", 99990, "--strategy.grow-grad2d", 0.0001,
        "--strategy.prune-scale3d", 0.1], get_run_logger())


@task(name="6. export")
def export_world(scene: str) -> dict:
    """Isaac Sim USDZ + the spawn camera the viewer opens at."""
    logger = get_run_logger()
    plys = sorted(Path(f"{scene}/gs_result/ply").glob("*.ply"),
                  key=lambda p: p.stat().st_mtime)
    if not plys:
        raise RuntimeError("training produced no ply")
    world = Path(scene) / "world.ply"
    world.write_bytes(plys[-1].read_bytes())

    # The USDZ is for Isaac Sim and nothing in this stack reads it -- a job
    # is judged done by world.ply. pxr (usd-core) has no aarch64 build, so
    # rather than fail the whole run at the last stage, skip it and say so.
    usdz = f"{scene}/world.usdz"
    if importlib.util.find_spec("pxr") is not None:
        sh(["python", TOOLS / "ply_to_isaac.py", world, usdz], logger)
    else:
        usdz = None
        logger.info("no pxr (usd-core has no aarch64 build) -- skipping the "
                    "Isaac USDZ export; world.ply is unaffected")
    sh(["python", TOOLS / "make_spawn_cam.py", scene], logger)
    # A world generated at a waypoint has one walk per corridor leaving it, and
    # the viewer needs the lanes before any of them can be marked. Derived and
    # idempotent, so it belongs here rather than in whatever ran the build.
    sh(["python", TOOLS / "edge_walks.py", scene], logger)
    return {"ply": str(world), "usdz": usdz}


def _run_name() -> str:
    """Name the run after the one thing it produces, so the queue at :4200 reads
    as a list of places in a building rather than a list of random adjectives.
    One run, one artifact — that is the tracking unit."""
    from prefect.runtime import flow_run

    parts = Path(flow_run.parameters.get("scene", "?")).parts
    # .../<project>/splats/<id>
    if len(parts) >= 3 and parts[-2] == "splats":
        return f"{parts[-3]}/{parts[-1]}"
    return "/".join(parts[-2:]) if len(parts) > 1 else str(parts[-1])


@flow(name="generate-world", log_prints=True,
      flow_run_name=_run_name)
def generate_world(scene: str, gpus: int = 4, steps: int = 2000,
                   llm_addr: str = "127.0.0.1", llm_port: str = "8000",
                   llm_name: str = "Qwen/Qwen3-VL-8B-Instruct") -> dict:
    """scene: a splats/<name> directory under /workspace/projects, holding panorama.png."""
    if not Path(scene, "panorama.png").exists():
        raise FileNotFoundError(f"{scene}/panorama.png missing")
    llm = {"addr": llm_addr, "port": llm_port, "name": llm_name}

    plan_trajectory(scene, llm)
    render_trajectories(scene, gpus, llm)
    expand_world(scene, gpus)
    build_gs_data(scene, gpus)
    train_gaussians(scene, steps)
    out = export_world(scene)
    print(f"3DGS : {out['ply']}")
    if out.get("usdz"):
        print(f"Isaac: {out['usdz']}")
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="mode", required=True)
    sub.add_parser("serve", help="wait for submitted runs (default in-container)")
    one = sub.add_parser("run", help="run once, locally")
    one.add_argument("scene")
    one.add_argument("--gpus", type=int, default=4)
    one.add_argument("--steps", type=int, default=2000)
    args = p.parse_args()

    if args.mode == "serve":
        # one run at a time: the stages want all the GPUs to themselves
        generate_world.serve(name=DEPLOYMENT, limit=1)
    else:
        generate_world(args.scene, gpus=args.gpus, steps=args.steps)


if __name__ == "__main__":
    main()
