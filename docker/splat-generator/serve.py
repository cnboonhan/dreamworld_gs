"""Serve the splat pipeline from one process.

  generate-world     one panorama -> an imagined navigable world (HY-World)

One at a time, because HY-World shards across every GPU it can see.

Building the Gazebo world is a job too, but it needs Gazebo rather than CUDA,
so it is served from the rmf-tools image instead
(docker/rmf-tools/world_flow.py). Both register with the same Prefect server,
so every operation shows up at :4200.
"""

from prefect import serve

from flow import generate_world

if __name__ == "__main__":
    serve(generate_world.to_deployment(name="dreamworld", concurrency_limit=1),
          limit=1)
