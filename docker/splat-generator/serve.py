"""Serve both pipelines from one process.

  generate-world     one panorama  -> an imagined navigable world (HY-World)
  reconstruct-world  a folder of panoramas -> a measured world (SfM + 3DGS)

Concurrency is 1 apiece: each pipeline wants the GPUs to itself.
"""

from prefect import serve

from flow import generate_world
from reconstruct import reconstruct_world

if __name__ == "__main__":
    serve(
        generate_world.to_deployment(name="dreamworld", concurrency_limit=1),
        reconstruct_world.to_deployment(name="dreamworld", concurrency_limit=1),
        limit=1,
    )
