# dreamworld_gs v2 — panorama -> navigable 3DGS world (+ Isaac Sim USDZ)
#
# Everything about one building lives in assets/projects/<project>/:
#
#   maps/    the authored floorplan (<map>.building.yaml + its images)
#   worlds/  generated from it: Gazebo world, models, nav graph
#   panos/   360s of the real place, one per waypoint, named for it
#   splats/  the world generated from each (variants as <id>@<name>)
#
# Conventions this rebuild keeps from v1:
#   - one project active at a time: DW_PROJECT in .env, every recipe defaults to it
#   - anything measured in minutes is a queued job (watched, retried at :4200);
#     anything you talk to while looking at it is a service in compose.yaml
#   - one run produces one artifact and is named for it
#   - recipes only submit and follow — ^C stops following, the job keeps running
#
# Recipes return here one at a time as the services are rebuilt.

set shell := ["bash", "-euo", "pipefail", "-c"]
set dotenv-load := true

repo    := justfile_directory()
assets  := repo / "assets"
project := env_var_or_default("DW_PROJECT", "multilevel_office")

# compose runs containers as you, not root, so outputs stay writable
export DW_UID := `id -u`
export DW_GID := `id -g`

_default:
    @just --list
