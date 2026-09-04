# rmf-tools — the RMF/Gazebo side of the stack, in one image with three roles.
#
#   jobs     serves build-world as a Prefect deployment
#   galaxea  spawns the Galaxea R1 into its own gazebo and serves its bridge
#   world    a traffic-editor building.yaml -> Gazebo world + models + nav graph
#   sim      that world running headless under RMF, shown over noVNC
#   editor   the traffic editor itself, shown over noVNC
#
# NOT on ghcr.io/open-rmf/rmf/rmf_demos: that image is published for
# linux/amd64 only (every tag — jazzy, kilted, rolling, lyrical, latest), so
# on aarch64 there is nothing to pull. Nothing needs building from source
# though: every RMF debian is released for arm64 as well as amd64 — the two
# package lists are identical, 47 each — and ros:jazzy is multi-arch. So the
# base is stock ROS and RMF arrives through apt, which is how the upstream
# image's own contents get there.
#
# Gazebo Harmonic comes with it: ros-gz-sim pulls ROS's gz-*-vendor packages,
# so Harmonic needs no second apt source. gz-tools-vendor is named explicitly
# because entrypoint.sh shells out to the `gz` CLI to wait for the server
# (`gz topic -l`) and to attach the GUI (`gz sim -g`).
#
# What the base lacks is a display — the sim GUI and the editor are both Qt
# apps, and this stack is used over ssh with forwarded ports, not X11. So we
# add a virtual X server and publish it as a web page: Xvfb -> x11vnc ->
# websockify -> noVNC.
#
# Rendering is software (llvmpipe). Passing a GPU through to a GLX app inside a
# container needs a real X server on the host, which defeats the point; these are
# floorplan-scale scenes and the physics runs on the CPU regardless.
#
# Build: just setup images   (or: docker compose build rmfsim)
FROM ros:jazzy-ros-base

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        ros-jazzy-rmf-traffic-editor \
        ros-jazzy-rmf-traffic-editor-assets \
        ros-jazzy-rmf-building-map-tools \
        ros-jazzy-rmf-traffic-ros2 \
        ros-jazzy-rmf-fleet-adapter \
        ros-jazzy-rmf-task-ros2 \
        ros-jazzy-rmf-building-sim-gz-plugins \
        ros-jazzy-rmf-robot-sim-gz-plugins \
        ros-jazzy-ros-gz-sim \
        ros-jazzy-ros-gz-bridge \
        ros-jazzy-gz-tools-vendor \
        ros-jazzy-ros2launch \
        xvfb x11vnc novnc websockify openbox xterm \
        x11-utils xdotool libgl1-mesa-dri libglx-mesa0 mesa-utils \
        python3-venv \
        python3-flask \
    && rm -rf /var/lib/apt/lists/*

# python3-flask above is the one thing the old rmf_demos base carried that
# stock ros:jazzy does not: infra_bridge.py serves doors, lifts and items to
# the harness over HTTP, and robot_bridge.py does the same for the robot.
# From apt, not pip — ROS's python tree is externally managed (PEP 668), and
# both scripts run under the system interpreter.

# The apt lift plugin (2.3.3) carries upstream rmf_simulation#132: the boot
# LiftCmd is created without door_state set, and when that uninitialized
# field is not zero, the command never completes and EVERY request is
# discarded as "busy" — from boot, for every lift, with no recovery. x86
# boxes usually get lucky zeros; this box's allocator reliably does not,
# which is why lifts never rode here. Jazzy never got the backport
# (its branch still reads 2.3.3), so apt cannot fix it: rebuild the one
# plugin from the SAME 2.3.3 source plus the one-commit fix, as a colcon
# overlay that shadows the deb. Everything else stays apt.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates build-essential cmake python3-colcon-common-extensions \
        qtbase5-dev qtdeclarative5-dev \
    && git clone --depth 1 --branch 2.3.3 \
        https://github.com/open-rmf/rmf_simulation /tmp/rmf_sim \
    && curl -sL https://github.com/open-rmf/rmf_simulation/commit/0a409cbb.patch \
        | git -C /tmp/rmf_sim apply \
    && grep -q "door_state = DoorModeCmp::CLOSE" \
        /tmp/rmf_sim/rmf_building_sim_gz_plugins/src/lift.cpp \
    && . /opt/ros/jazzy/setup.sh \
    && colcon build \
        --base-paths /tmp/rmf_sim \
        --packages-select rmf_building_sim_gz_plugins \
        --build-base /tmp/rmf_build \
        --install-base /rmf_demos_ws/install \
        --cmake-args -DCMAKE_BUILD_TYPE=Release \
    && rm -rf /tmp/rmf_sim /tmp/rmf_build /var/lib/apt/lists/*

# The scripts source /rmf_demos_ws/install/setup.bash — on the upstream
# amd64 image that was the from-source overlay, and here it is colcon's own
# setup for the patched plugin, which chains back to /opt/ros/jazzy (the
# underlay sourced during its build). Overlay first on every search path,
# so gz loads the FIXED liblift.so and the deb's copy never runs.

# ROS installs a package's executables under lib/<pkg>/ rather than on PATH,
# and entrypoint.sh calls `traffic-editor` bare. Link it wherever the deb put
# it — and fail the build here, not at first launch, if it is not there.
RUN set -eux; \
    editor="$(find /opt/ros/jazzy -type f -perm -u+x -name traffic-editor | head -1)"; \
    test -n "$editor"; \
    ln -sf "$editor" /usr/local/bin/traffic-editor

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
