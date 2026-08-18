# dreamworld_viewer — the walkthrough: main's splat viewer rebuilt on the
# dreamworld tree. Static files only; the graph and every asset come from
# the editor's routes, so this stays an nginx and nothing else.
FROM nginx:alpine
COPY dreamworld_viewer/default.conf /etc/nginx/conf.d/default.conf
COPY dreamworld_viewer/www /usr/share/nginx/html
