"""Serve the splat pipelines from one process.

  generate-world     one panorama -> an imagined navigable world (HY-World)
  reconstruct-world  a folder of panoramas -> a measured world (SfM + 3DGS)
  render-video       a built splat -> a walkthrough along its capture path

Concurrency is 1 apiece: each wants the GPUs to itself.

World generation is a job too, but it needs Gazebo rather than CUDA, so it is
served from the rmf-tools image instead (docker/rmf-tools/world_flow.py). Both
register with the same Prefect server, so every operation shows up at :4200.
"""

from prefect import serve

from flow import generate_world
from reconstruct import reconstruct_world
from video import render_walkthrough

if __name__ == "__main__":
    serve(
        generate_world.to_deployment(name="dreamworld", concurrency_limit=1),
        reconstruct_world.to_deployment(name="dreamworld", concurrency_limit=1),
        render_walkthrough.to_deployment(name="dreamworld", concurrency_limit=1),
        limit=1,
    )
