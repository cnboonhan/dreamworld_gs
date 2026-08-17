# NiceGUI serves its whole frontend bundle itself — nothing reaches a CDN at
# runtime, which is a requirement here, not a nicety.
FROM python:3.12-slim
RUN pip install --no-cache-dir "nicegui~=2.11" pyyaml numpy pillow
ENV NICEGUI_STORAGE_PATH=/tmp/.nicegui
WORKDIR /srv
COPY dreamworld_editor/*.py ./
COPY dreamworld_editor/js ./js
EXPOSE 8080
CMD ["uvicorn", "app:fastapi_app", "--host", "0.0.0.0", "--port", "8080"]
