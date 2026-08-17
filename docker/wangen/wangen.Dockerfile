# wangen — Wan 2.2 first+last-frame crossings, built on the qwen-server
# image because it already carries the right torch and diffusers; only the
# video export tooling and the server are added. Weights resolve out of the
# mounted HF cache at runtime; nothing reaches the network.
FROM dreamworld/qwen-server:latest

RUN pip install --no-cache-dir imageio imageio-ffmpeg ftfy

COPY wangen/server.py /app/wan_server.py

EXPOSE 8000

CMD ["python", "/app/wan_server.py"]
