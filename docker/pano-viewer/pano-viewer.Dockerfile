# Equirectangular 360 viewer for the generation inputs in assets/panos.
#
#   www/                         WebGL viewer (engine ported from the
#                                dream_editor viewer, see NOTICE.md)
#   /usr/share/nginx/html/files  <- mount assets/panos here (read-only)
FROM nginx:alpine

COPY www/ /usr/share/nginx/html/
COPY nginx.conf /etc/nginx/conf.d/default.conf

# mountpoint must exist inside the read-only parent mount
RUN mkdir -p /usr/share/nginx/html/files

EXPOSE 80
