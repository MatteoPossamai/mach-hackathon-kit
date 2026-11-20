"""Poll city deployments by id and print one line per meaningful change.

Usage: python3 watch_deploys.py name=dep-id [name=dep-id ...]
Prints step/status changes, queue position in buckets of 10, and the final outcome.
"""

import json
import sys
import time
import urllib.request
from pathlib import Path

TOKEN = Path.home().joinpath(".config/city/teams/ltf-jecatt/session").read_text().strip()
DEPLOYS = dict(arg.split("=", 1) for arg in sys.argv[1:])
last: dict[str, tuple] = {}
done: set[str] = set()

while len(done) < len(DEPLOYS):
    for name, dep in DEPLOYS.items():
        if name in done:
            continue
        try:
            request = urllib.request.Request(
                f"https://deploy.projectnandaitaly.ai/deployments/{dep}",
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            view = json.load(urllib.request.urlopen(request, timeout=20))
        except Exception:
            continue
        status, step, outcome = view.get("status"), view.get("step"), view.get("outcome")
        detail = view.get("detail") or ""
        bucket = detail
        if detail.startswith("waiting for a rollout slot"):
            ahead = int(detail.split(",")[1].split()[0])
            bucket = f"queue<={(ahead // 10 + 1) * 10}"
        key = (status, step, bucket, outcome)
        if key != last.get(name):
            last[name] = key
            print(
                f"{time.strftime('%H:%M:%S')} {name}: status={status} step={step} "
                f"detail={detail} outcome={outcome}",
                flush=True,
            )
        if outcome is not None or status in ("failed", "promoted"):
            done.add(name)
    time.sleep(20)
print("ALL DONE", flush=True)
