"""Serve the splat pipelines from one process.

  generate-world     one panorama -> an imagined navigable world (HY-World)
  reconstruct-world  panoramas of a real place -> a measured world. Poses are
                     inferred (SfM), then the splat is aligned into the building
  reconstruct-simulated  panoramas from the simulator, which recorded where it
                     stood -> no solve, no alignment; placed by construction
  render-video       a built splat -> a walkthrough along its capture path
  render-route       a planned route -> a walkthrough of the whole traversal,
                     rendered from the union of the corridors it crosses
  plan-route         two waypoints -> the walk between them, as a route the
                     viewer streams splats along (nothing is rendered)

Concurrency is 1 apiece: each wants the GPUs to itself.

World generation is a job too, but it needs Gazebo rather than CUDA, so it is
served from the rmf-tools image instead (docker/rmf-tools/world_flow.py). Both
register with the same Prefect server, so every operation shows up at :4200.
"""

from prefect import serve

from flow import generate_world
from reconstruct import reconstruct_simulated, reconstruct_world
from route import plan_route
from video import render_route, render_walkthrough

if __name__ == "__main__":
    serve(
        generate_world.to_deployment(name="dreamworld", concurrency_limit=1),
        reconstruct_world.to_deployment(name="dreamworld", concurrency_limit=1),
        reconstruct_simulated.to_deployment(name="dreamworld", concurrency_limit=1),
        render_walkthrough.to_deployment(name="dreamworld", concurrency_limit=1),
        render_route.to_deployment(name="dreamworld", concurrency_limit=1),
        plan_route.to_deployment(name="dreamworld", concurrency_limit=1),
        limit=1,
    )
