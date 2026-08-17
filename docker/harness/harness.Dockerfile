# harness — main's interactive harness on the v2 seams: movement through
# dreamworld_core, doors and lifts through the infra bridge, the mission
# agent against the vLLM. flask + pyyaml over one file, as main kept it.
FROM python:3.11-slim
RUN pip install --no-cache-dir flask pyyaml
# The mission agent. Optional at runtime — without a VLM_BASE_URL the tools
# stay directly callable and /agent says why it is off.
RUN pip install --no-cache-dir "deepagents>=0.1.0" "langchain-openai>=0.3.0" \
    || echo "deepagents unavailable — /agent disabled, tools still work"
COPY harness/interactive.py /app/interactive.py
WORKDIR /app
ENTRYPOINT ["python", "interactive.py"]
