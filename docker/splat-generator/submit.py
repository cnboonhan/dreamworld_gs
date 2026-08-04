"""Submit a job and follow it by polling, not by holding a stream open.

`prefect deployment run --watch` keeps an events/logs stream open for the
whole run. A pipeline stage that is quiet for ten minutes (SfM is a single
long C++ call) lets that stream go idle and drop, the CLI exits non-zero, and
the run gets cancelled — the job dies for want of a heartbeat on the client
side. Polling the run's state has no such coupling: interrupting this script
leaves the job running, exactly as intended.

Usage:
    python submit.py <flow>/<deployment> key=value [key=value ...]
    python submit.py --follow <flow-run-id>      # attach to a running job
"""

from __future__ import annotations

import asyncio
import sys
import time
import uuid

from prefect.client.orchestration import get_client
from prefect.client.schemas.filters import FlowRunFilter, FlowRunFilterId

TERMINAL = {"COMPLETED", "FAILED", "CRASHED", "CANCELLED"}
POLL_SECONDS = 5


def parse(args: list[str]) -> dict:
    params: dict[str, object] = {}
    for a in args:
        k, _, v = a.partition("=")
        if v.lstrip("-").isdigit():
            params[k] = int(v)
        else:
            params[k] = v
    return params


async def main() -> int:
    args = sys.argv[1:]

    async with get_client() as client:
        if args[0] == "--follow":
            run = await client.read_flow_run(uuid.UUID(args[1]))
            print(f"following {run.name} ({run.id})", flush=True)
        else:
            name, *rest = args
            deployment = await client.read_deployment_by_name(name)
            run = await client.create_flow_run_from_deployment(
                deployment.id, parameters=parse(rest))
            print(f"submitted {run.name} ({run.id})", flush=True)

        seen: dict[str, str] = {}
        started = time.monotonic()
        while True:
            await asyncio.sleep(POLL_SECONDS)
            run = await client.read_flow_run(run.id)

            for tr in sorted(await client.read_task_runs(
                    flow_run_filter=FlowRunFilter(
                        id=FlowRunFilterId(any_=[run.id]))),
                    key=lambda t: t.name):
                state = tr.state.type.value if tr.state else "?"
                if seen.get(tr.name) != state:
                    seen[tr.name] = state
                    mins = (time.monotonic() - started) / 60
                    print(f"[{mins:5.1f}m] {tr.name}: {state.lower()}", flush=True)

            state = run.state.type.value if run.state else "?"
            if state in TERMINAL:
                mins = (time.monotonic() - started) / 60
                print(f"\n{run.name} {state.lower()} after {mins:.1f} min", flush=True)
                if state != "COMPLETED" and run.state and run.state.message:
                    print(run.state.message, flush=True)
                return 0 if state == "COMPLETED" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
