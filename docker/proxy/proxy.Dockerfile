# proxy — one forwarded port for every surface.
FROM nginx:alpine
COPY proxy/nginx.conf /etc/nginx/conf.d/default.conf
COPY proxy/index.html /usr/share/nginx/html/index.html
