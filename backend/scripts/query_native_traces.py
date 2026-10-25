"""Read every page of the pinned SDK's SmithDB runs API, including all export boundaries."""

import asyncio
import time


async def query_native_traces(client, project_id, start, required):
    deadline = time.monotonic() + 25
    while True:
        runs = [
            run
            async for run in client.runs.query(
                project_ids=[str(project_id)],
                min_start_time=start,
                page_size=100,
                selects=[
                    "ID",
                    "NAME",
                    "RUN_TYPE",
                    "INPUTS",
                    "OUTPUTS",
                    "METADATA",
                    "EXTRA",
                    "ERROR",
                    "END_TIME",
                    "EVENTS",
                    "ATTACHMENTS",
                    "TAGS",
                    "MANIFEST",
                    "TRACE_ID",
                    "PARENT_RUN_IDS",
                    "TOTAL_TOKENS",
                    "APP_PATH",
                ],
                timeout=15,
            )
        ]
        if required <= {run.name for run in runs} or time.monotonic() > deadline:
            return runs
        await asyncio.sleep(2)
