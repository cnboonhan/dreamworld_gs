# splatgen — main's splat-generator image, unchanged, with the Prefect
# deployment swapped for a one-job HTTP wrapper. The 24GB of pipeline
# underneath is exactly the image main built; only the front door differs.
FROM dreamworld/splat-generator:latest

COPY splatgen/server.py /opt/server.py

EXPOSE 8000

# the base image's ENTRYPOINT is python
CMD ["server.py"]
