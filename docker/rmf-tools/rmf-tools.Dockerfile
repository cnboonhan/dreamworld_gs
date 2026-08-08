# rmf-tools — the RMF/Gazebo side of the stack, in one image with three roles.
#
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
# Build: just build   (or: docker compose build rmfsim)
FROM ghcr.io/open-rmf/rmf/rmf_demos:jazzy-rmf-latest

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11vnc novnc websockify openbox xterm \
        x11-utils xdotool libgl1-mesa-dri libglx-mesa0 mesa-utils \
    && rm -rf /var/lib/apt/lists/*

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
     rmf-tools/postprocess_world.py rmf-tools/sim.launch.xml.template /app/
RUN chmod +x /app/entrypoint.sh /app/with_display.sh /app/generate_world.sh

# Xvfb, x11vnc and openbox all want a writable HOME; compose runs this as the
# calling user, who has none inside the image.
ENV HOME=/tmp \
    XDG_RUNTIME_DIR=/tmp/runtime \
    DISPLAY=:99 \
    GZ_PARTITION=dreamworld_rmf

EXPOSE 8080
ENTRYPOINT ["/app/entrypoint.sh"]
