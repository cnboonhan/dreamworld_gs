# interactive — the tool surface that drives the splat viewer and the Galaxea R1.
#
# Ported from dreamworld/docker/dream_interactive/dream_interactive.Dockerfile,
# minus everything the stitcher needed: there is no photorealistic video to stitch
# here, so no opencv and no move_dream/stitch.py. A walk is the splat viewer riding
# a corridor live, which leaves this image as flask + pyyaml over one file.
#
#   docker build -t dreamworld/interactive -f interactive/interactive.Dockerfile .
FROM python:3.11-slim
RUN pip install --no-cache-dir flask pyyaml
COPY interactive/interactive.py /app/interactive.py
WORKDIR /app
ENTRYPOINT ["python", "interactive.py"]
