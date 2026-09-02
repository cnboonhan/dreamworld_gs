set shell := ["bash", "-euo", "pipefail", "-c"]
set dotenv-load := true

repo    := justfile_directory()
assets  := repo / "assets"
project := env_var_or_default("DW_PROJECT", "multilevel_office")

_default:
    @just --list

# Idempotent and cheap to re-run: a repo already in the cache is recognised
# without touching the network. The list is scripts/models.txt.
#
# Download every model the pipeline needs (~190GB, needs network).
fetch:
    HF_HOME={{assets}}/hf uv run --with huggingface_hub --no-project \
        {{repo}}/scripts/fetch_assets.py {{assets}}

# Pack the WHOLE assets/projects/<project> into dist/ — maps, worlds, panos,
# splats, training intermediates, everything. Weights are separate (just fetch).
#
# Carry a project to another machine, entire.
pack proj=project dest="dist":
    @python3 {{repo}}/scripts/pack.py pack {{assets}} {{proj}} {{dest}}

# Restore a packed project, in place (merges into an existing one, with a note).
unpack FILE:
    @python3 {{repo}}/scripts/pack.py unpack {{repo}} {{FILE}}

_env:
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p {{assets}}/projects
    grep -q "^DW_PROJECT=" {{repo}}/.env 2>/dev/null || \
        echo "DW_PROJECT={{project}}" >> {{repo}}/.env
    grep -q "^DW_UID=" {{repo}}/.env 2>/dev/null || \
        printf 'DW_UID=%s\nDW_GID=%s\n' "$(id -u)" "$(id -g)" >> {{repo}}/.env

# Rebuild the Gazebo world + nav graph from building.yaml (after map edits).
world: _env
    docker compose -f compose.full.yaml run --rm --no-deps rmfsim world {{project}}

# FULL: everything — authoring, simulation, and all four generators (8 GPUs).
up: _env
    docker compose -f compose.full.yaml up -d --build
    @echo "  http://localhost:8080                     every surface, one port"
    @echo "  http://localhost:8080/sim_editor          trace the building map"
    @echo "  http://localhost:8080/dreamworld_editor   grow the dreamworld"
    @echo "  http://localhost:8080/dreamworld_viewer   walk the dreamworld"
    @echo "  http://localhost:8080/rmfsim              the building under simulation"
    @echo "  http://localhost:8080/harness             drive it by tool call"

# MINIMAL: walk + simulate + command an already-generated dreamworld
# (1 GPU, for the mission agent's VLM). No generators, no traffic editor.
minimal: _env
    docker compose -f compose.minimal.yaml up -d --build
    @echo "  http://localhost:8080/dreamworld_viewer   walk the dreamworld"
    @echo "  http://localhost:8080/rmfsim              the building under simulation"
    @echo "  http://localhost:8080/harness             drive it by tool call"

# Pack the walkable demo into assets/projects/<project>/bundle as a
# SELF-CONTAINED static site: viewer + graph + every world's records +
# every crossing video, relative paths throughout — any static file host
# serves it (the panoramas never leave the dreamworld tree).
bundle proj=project:
    #!/usr/bin/env bash
    set -euo pipefail
    docker compose -f compose.full.yaml run --rm --no-deps dreamworld_editor \
        python bundle.py /projects/{{proj}}/bundle
    dest={{assets}}/projects/{{proj}}/bundle
    cp -r {{repo}}/docker/dreamworld_viewer/www/. "$dest/"
    sed -i 's|<script src="viewer.js|<script>window.DW_BASE={files:"files",graph:"graph.json"}</script>\n  <script src="viewer.js|' "$dest/index.html"
    # A public bundle must say what it is: the world models' acceptable-use
    # terms require machine-generated content in a public context to be
    # conspicuously identified. On the hint overlay, always on screen.
    sed -i 's|plan to cross</div>|plan to cross<br><b style="color:#e3b341">AI-generated</b> — every view is a machine-made reconstruction</div>|' "$dest/index.html"
    echo "self-contained site at $dest — any static file server can host it"

# DEMO: serve the bundle locally — it is a static site, so no Docker,
# no backend: any file server does. Ctrl-C stops it.
demo proj=project port="8080":
    @echo "  http://localhost:{{port}}   walk the bundle (ctrl-c to stop)"
    @cd {{assets}}/projects/{{proj}}/bundle && python3 -m http.server {{port}}

# Stage the bundle onto the gh-pages branch as ONE parentless commit —
# rerunning replaces it, so history never grows. Deliberately does NOT
# push: the bundle is a photoreal reconstruction of a real building, and
# publishing it is a clearance decision, not a build step.
#
#   git push -f origin gh-pages       when cleared
#   repo Settings -> Pages -> deploy from branch gh-pages
pages proj=project: (bundle proj)
    #!/usr/bin/env bash
    set -euo pipefail
    dest={{assets}}/projects/{{proj}}/bundle
    # Pages runs Jekyll by default, and Jekyll drops dot-paths — which
    # would silently vanish every crossing under files/.crossings
    touch "$dest/.nojekyll"
    # -u: a fresh PATH, not a file — git refuses an existing empty index
    export GIT_INDEX_FILE=$(mktemp -u)
    git --work-tree="$dest" add -A
    tree=$(git write-tree)
    commit=$(git commit-tree "$tree" -m "pages: {{proj}} bundle")
    git update-ref refs/heads/gh-pages "$commit"
    rm -f "$GIT_INDEX_FILE"
    echo "gh-pages staged at $(git rev-parse --short gh-pages) — push is yours:"
    echo "    git push -f origin gh-pages"
