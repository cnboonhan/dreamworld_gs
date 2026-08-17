# rmf-tools — the RMF/Gazebo side of the stack, in one image with three roles.
#
#   jobs     serves build-world as a Prefect deployment
#   galaxea  spawns the Galaxea R1 into its own gazebo and serves its bridge
#   world    a traffic-editor building.yaml -> Gazebo world + models + nav graph
#   sim      that world running headless under RMF, shown over noVNC
#   editor   the traffic editor itself, shown over noVNC
#
# One image because all three want the same base: ghcr.io/open-rmf/rmf/rmf_demos
# ships Gazebo Harmonic, rmf_building_map_tools and traffic-editor already. What
# it lacks is a display — the sim GUI and the editor are both Qt apps, and this
# stack is used over ssh with forwarded ports, not X11. So we add a virtual X
# server and publish it as a web page: Xvfb -> x11vnc -> websockify -> noVNC.
#
# Rendering is software (llvmpipe). Passing a GPU through to a GLX app inside a
# container needs a real X server on the host, which defeats the point; these are
# floorplan-scale scenes and the physics runs on the CPU regardless.
#
# Build: just setup images   (or: docker compose build rmfsim)
FROM ghcr.io/open-rmf/rmf/rmf_demos:jazzy-rmf-latest

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11vnc novnc websockify openbox xterm \
        x11-utils xdotool libgl1-mesa-dri libglx-mesa0 mesa-utils \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Prefect, so world generation is a tracked job like everything else. In its own
# venv: this base pins ROS's python packages system-wide and pip refuses to
# touch them (PEP 668), which is the right call — ROS keeps its tree, we keep
# ours. --system-site-packages so the flow can still import yaml alongside it.
RUN python3 -m venv --system-site-packages /opt/prefect \
    && /opt/prefect/bin/pip install --no-cache-dir 'prefect==3.8.1' \
    && /opt/prefect/bin/python -c "import prefect, yaml; print('prefect', prefect.__version__)"

# Land on a connected session, not on noVNC's connect dialog: opening the port
# should show the application, the way the other viewers in this stack do.
RUN printf '%s\n' \
      '<!doctype html><meta charset="utf-8"><title>dreamworld rmf</title>' \
      '<script>location.replace("vnc.html?autoconnect=true&resize=scale&reconnect=true&show_dot=true"+location.hash)</script>' \
      > /usr/share/novnc/index.html

# Xvfb refuses to create this itself when it is not root, and compose runs this
# image as the calling user.
RUN mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix

WORKDIR /app
COPY rmf-tools/entrypoint.sh rmf-tools/with_display.sh rmf-tools/generate_world.sh \
     rmf-tools/postprocess_world.py rmf-tools/sim.launch.xml.template \
     rmf-tools/world_flow.py rmf-tools/texturize.py \
     rmf-tools/robot_bridge.py rmf-tools/infra_bridge.py rmf-tools/galaxea.sh /app/
RUN chmod +x /app/entrypoint.sh /app/with_display.sh /app/generate_world.sh \
             /app/galaxea.sh

# Xvfb, x11vnc and openbox all want a writable HOME; compose runs this as the
# calling user, who has none inside the image.
ENV HOME=/tmp \
    XDG_RUNTIME_DIR=/tmp/runtime \
    DISPLAY=:99 \
    GZ_PARTITION=dreamworld_rmf

EXPOSE 8080
ENTRYPOINT ["/app/entrypoint.sh"]
