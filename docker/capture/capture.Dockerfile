# capture — the one surface for getting a 360 into the building.
FROM python:3.11-slim
RUN pip install --no-cache-dir flask numpy pillow pyyaml requests
COPY capture/capture.py capture/index.html /opt/capture/
WORKDIR /opt/capture
ENTRYPOINT ["python", "capture.py"]
