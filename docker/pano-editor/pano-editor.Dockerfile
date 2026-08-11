# pano-editor — edit a waypoint's panorama by facing it and saying what to change.
#
# Ported from dreamworld/docker/dream_editor, which ran on the host because Save
# shelled out to build_library. Here Save writes one file under panos/ and the
# propagation is a `just generate <id>` on the queue, so it containerises without
# needing the repo mounted into it.
#
#   docker build -t dreamworld/pano-editor -f pano-editor/pano-editor.Dockerfile .
FROM python:3.11-slim
RUN pip install --no-cache-dir flask requests pyyaml pillow numpy
COPY pano-editor/pano_editor.py pano-editor/index.html /app/
WORKDIR /app
ENTRYPOINT ["python", "pano_editor.py"]
