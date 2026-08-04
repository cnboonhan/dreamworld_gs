# Browser 3DGS viewer: serves any .ply under the mounted scenes dir.
#
#   www/            vendored antimatter15/splat WebGL viewer (?url= patched
#                   to resolve against this origin instead of a CDN)
#   /usr/share/nginx/html/files  <- mount your scenes here (read-only)
FROM nginx:alpine

COPY www/ /usr/share/nginx/html/
COPY nginx.conf /etc/nginx/conf.d/default.conf

# mountpoint must exist inside the read-only parent mount
RUN mkdir -p /usr/share/nginx/html/files

EXPOSE 80
