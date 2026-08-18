# proxy — one forwarded port for every surface.
FROM nginx:alpine
# a TEMPLATE: the stock entrypoint substitutes ${DW_PROJECT} at boot
COPY proxy/default.conf.template /etc/nginx/templates/default.conf.template
COPY proxy/index.html /usr/share/nginx/html/index.html
