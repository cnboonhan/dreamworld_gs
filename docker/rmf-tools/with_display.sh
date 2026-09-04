#!/bin/bash
# Run a GUI program on a private X server and publish it as a web page.
#
#   with_display.sh <command...>
#
# Xvfb gives the program a screen nobody is looking at; x11vnc exports that
# screen; websockify wraps VNC in WebSocket so noVNC can render it in a browser
# tab. The chain exists because the two Qt apps here (the Gazebo GUI and the
# traffic editor) have no headless mode, and this stack is reached over ssh port
# forwards rather than X11.
#
# The wrapped command owns the container's lifetime: when it exits, so does this.
set -euo pipefail

W="${DW_VNC_W:-1600}"
H="${DW_VNC_H:-900}"
PORT="${DW_VNC_PORT:-8080}"

# Both Qt apps here map a 1x1 window and wait for someone to drag a corner: the
# Gazebo GUI's size lives in an SDF <window> element this version no longer
# reads, and nobody is sitting at this X server. Size it to the screen instead.
fit_window() {
    local pat="$1" best="" area=0
    for _ in $(seq 1 180); do
        best=""; area=0
        # An app can own several windows matching the name (the Gazebo GUI has a
        # 1x1 helper alongside its real one), so take the biggest rather than
        # trusting an exact title.
        for id in $(xdotool search --name "$pat" 2>/dev/null); do
            eval "$(xdotool getwindowgeometry --shell "$id" 2>/dev/null)" || continue
            if [ $((WIDTH * HEIGHT)) -gt "$area" ]; then
                area=$((WIDTH * HEIGHT)); best="$id"
            fi
        done
        if [ -n "$best" ] && [ "$area" -gt 10000 ]; then
            sleep 2                      # let it finish mapping before resizing
            xdotool windowmove "$best" 0 0 windowsize "$best" "$W" "$H" 2>/dev/null || true
            echo "fitted '$pat' (window $best) to ${W}x${H}" >&2
            return 0
        fi
        sleep 1
    done
    echo "no window matching '$pat' appeared" >&2
}

cleanup() {
    kill "${WS_PID:-}" "${VNC_PID:-}" "${WM_PID:-}" "${XVFB_PID:-}" 2>/dev/null || true
}
trap cleanup EXIT

# `docker compose restart` keeps the filesystem, so the last run's lock
# files survive into this one and Xvfb dies with "Server is already active
# for display :99" — a permanent crash loop, since every restart inherits
# the same stale lock. If nothing actually answers on the display, the
# locks are leftovers: clear them.
DNUM="${DISPLAY#:}"
if ! xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
    rm -f "/tmp/.X${DNUM}-lock" "/tmp/.X11-unix/X${DNUM}"
fi

Xvfb "$DISPLAY" -screen 0 "${W}x${H}x24" -nolisten tcp &
XVFB_PID=$!
# wait for the server to accept connections before anything tries to draw
ready=""
for _ in $(seq 1 100); do
    if xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then ready=1; break; fi
    sleep 0.2
done
[ -n "$ready" ] || { echo "X server never came up on $DISPLAY" >&2; exit 1; }

# a window manager, so dialogs can be moved and the app fills the screen
openbox --sm-disable >/dev/null 2>&1 &
WM_PID=$!

x11vnc -display "$DISPLAY" -forever -shared -nopw -quiet -localhost -rfbport 5900 &
VNC_PID=$!
sleep 1

# --heartbeat keeps the browser's WebSocket alive through an idle ssh tunnel
websockify --web /usr/share/novnc --heartbeat 30 "0.0.0.0:${PORT}" localhost:5900 &
WS_PID=$!

mkdir -p "${XDG_RUNTIME_DIR:-/tmp/runtime}"
chmod 700 "${XDG_RUNTIME_DIR:-/tmp/runtime}" 2>/dev/null || true

# DW_FIT_WINDOW names the app's window, so it can be sized once it appears
[ -n "${DW_FIT_WINDOW:-}" ] && fit_window "$DW_FIT_WINDOW" &

echo "noVNC on :${PORT} (${W}x${H}) -> $*" >&2
"$@"
