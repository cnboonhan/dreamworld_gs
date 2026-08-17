# dreamworld_core — the state holder. Stdlib only, so the image is just
# python; the seam should be the most boring service in the stack.
FROM python:3.12-slim
COPY dreamworld_core/server.py /srv/server.py
EXPOSE 8000
CMD ["python", "/srv/server.py"]
