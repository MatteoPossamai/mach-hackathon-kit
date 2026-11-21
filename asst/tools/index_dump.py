"""Read-only dump of the public City Index: every agent of every role, with its card (team)."""
import json
import sys
import time
import urllib.request

BASE = "https://index.projectnandaitaly.ai"
ROLES = ("booking_centre", "gp", "hospital", "specialist_care", "family_counseling")
OUT = sys.argv[1]


def get(path):
    for attempt in range(3):
        try:
            req = urllib.request.Request(BASE + path, headers={"X-Caller": "anonymous", "User-Agent": "team-index-reader"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1 + attempt)
    return {"_error": str(last)}


agents = {}
for role in ROLES:
    for include, verified in ((None, True), ("unverified", False)):
        q = f"/v1/agents?role={role}" + (f"&include={include}" if include else "")
        for row in get(q).get("agents", []):
            aid = row["agent_id"]
            if aid not in agents:
                agents[aid] = dict(row, query_role=role, verified=verified)

for i, (aid, row) in enumerate(sorted(agents.items())):
    card = get(f"/v1/agents/{aid}/card")
    row["team"] = card.get("team", "")
    row["name"] = card.get("name", "")
    row["card_role"] = card.get("role", "")
    row["version"] = card.get("version", "")
    row["card_error"] = card.get("_error", "")
    time.sleep(0.1)

json.dump(agents, open(OUT, "w"), indent=1, ensure_ascii=False)
teams = {}
for aid, row in agents.items():
    teams.setdefault(row["team"] or "?", []).append(row)
print(len(agents), "agents,", len(teams), "teams,", sum(not r["verified"] for r in agents.values()), "unverified")
for team, rows in sorted(teams.items()):
    roles = {r["query_role"]: r["agent_id"] for r in rows}
    print(team, len(rows), {k: roles.get(k, "-") for k in ROLES})
