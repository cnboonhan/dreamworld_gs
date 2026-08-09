"""Serve the splat pipelines from one process.

  generate-world     one panorama -> an imagined navigable world (HY-World)
  reconstruct-world  panoramas of a real place -> a measured world. Poses are
                     inferred (SfM), then the splat is aligned into the building
  reconstruct-simulated  panoramas from the simulator, which recorded where it
                     stood -> no solve, no alignment; placed by construction
  render-video       a built splat -> a walkthrough riding its recorded walk
  render-route       a planned route -> a walkthrough of the whole traversal,
                     rendered from the union of the corridors it crosses
  plan-route         two waypoints -> the walk between them, as a route the
                     viewer streams splats along (nothing is rendered)

Concurrency: reconstructions run several at a time, because each fits on one
card and picks the emptiest visible one — a batch of corridors is limited by
how many run at once, not by how fast one runs. Generation and rendering stay
at one: HY-World shards across every GPU it can see, and a render is short.

World generation is a job too, but it needs Gazebo rather than CUDA, so it is
served from the rmf-tools image instead (docker/rmf-tools/world_flow.py). Both
register with the same Prefect server, so every operation shows up at :4200.
"""

import os

from prefect import serve

# One reconstruction per visible GPU. CUDA_VISIBLE_DEVICES is what compose
# hands this container, so counting it needs no torch import at module scope.
RECONSTRUCTIONS = max(1, len(
    [d for d in os.environ.get("CUDA_VISIBLE_DEVICES", "0").split(",") if d.strip()]))

from flow import generate_world
from reconstruct import reconstruct_simulated, reconstruct_world
from route import plan_route
from video import render_route, render_walkthrough

if __name__ == "__main__":
    serve(
        generate_world.to_deployment(name="dreamworld", concurrency_limit=1),
        reconstruct_world.to_deployment(name="dreamworld",
                                        concurrency_limit=RECONSTRUCTIONS),
        reconstruct_simulated.to_deployment(name="dreamworld",
                                            concurrency_limit=RECONSTRUCTIONS),
        render_walkthrough.to_deployment(name="dreamworld", concurrency_limit=1),
        render_route.to_deployment(name="dreamworld", concurrency_limit=1),
        plan_route.to_deployment(name="dreamworld", concurrency_limit=1),
        # The runner's own cap, which overrides every per-deployment limit
        # above — left at 1, it serialised a batch of reconstructions however
        # many GPUs were free and whatever those limits said.
        limit=RECONSTRUCTIONS,
    )
