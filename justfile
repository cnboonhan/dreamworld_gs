set shell := ["bash", "-euo", "pipefail", "-c"]
set dotenv-load := true

repo    := justfile_directory()
assets  := repo / "assets"
project := env_var_or_default("DW_PROJECT", "multilevel_office")

_default:
    @just --list
