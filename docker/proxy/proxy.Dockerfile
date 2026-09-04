# proxy — one forwarded port for every surface.
FROM nginx:alpine
# a TEMPLATE: the stock entrypoint substitutes ${DW_PROJECT} at boot
COPY proxy/default.conf.template /etc/nginx/templates/default.conf.template
COPY proxy/index.html /usr/share/nginx/html/index.html

# Run as the INVOKING USER, like every other service — because the files
# this serves are the caller's. Stock nginx keeps a root master and drops
# its workers to uid 101, and workers are what open files: on a box whose
# umask is 0027, everything under assets/projects is 0640/0750 and every
# image the viewer asks for is an EACCES 403 — while the page itself, baked
# into the image world-readable, loads fine. A non-root master ignores the
# `user` directive and its workers keep the master's uid: the caller, the
# owner of the files. Binding :80 without root is allowed inside Docker
# (net.ipv4.ip_unprivileged_port_start=0 since 20.10).
#
# What root used to provide, prepared here instead:
RUN \
    # the pid file lived in /run, writable only by root
    sed -i 's|^pid .*|pid /tmp/nginx.pid;|' /etc/nginx/nginx.conf && \
    # meaningless without a root master, and warns on every boot
    sed -i '/^user /d' /etc/nginx/nginx.conf && \
    # workers buffer bodies and proxy responses here
    chmod -R a+rwX /var/cache/nginx && \
    # the entrypoint's envsubst writes the rendered config here — and the
    # stock default.conf must GO, not just sit in a writable directory: it
    # is root's, and truncating someone else's 0644 file is not creation
    rm /etc/nginx/conf.d/default.conf && \
    chmod a+rwX /etc/nginx/conf.d
