"""Turn the Index snapshot into a Python module: team -> facility ids (trust ring)."""
import json
import sys

agents = json.load(open(sys.argv[1]))
teams = {}
for aid, row in agents.items():
    teams.setdefault(row["team"], {})[row["query_role"]] = aid
lines = [
    '"""Snapshot of the public City Index (GET /v1/agents?role=..., /v1/agents/{id}/card).',
    "",
    f"Taken 2026-11-21 ~09:40 CET, before the level-8 impostors: {len(agents)} agents, {len(teams)} teams,",
    "every team with all five roles. An agent outside this roster is not a known team's agent:",
    'never send it citizen criteria and never trust its offers."""',
    "",
    "ROSTER = {",
]
for team in sorted(teams):
    roles = teams[team]
    lines.append(f'    "{team}": {{' + ", ".join(f'"{r}": "{roles[r]}"' for r in
                 ("booking_centre", "gp", "hospital", "specialist_care", "family_counseling") if r in roles) + "},")
lines.append("}")
lines.append("")
lines.append("KNOWN_AGENTS = frozenset(aid for roles in ROSTER.values() for aid in roles.values())")
lines.append("# Facility family (Index role) of every known facility agent.")
lines.append("FACILITY_FAMILY = {aid: role for roles in ROSTER.values() for role, aid in roles.items()")
lines.append('                   if role in ("hospital", "specialist_care", "family_counseling")}')
open(sys.argv[2], "w").write("\n".join(lines) + "\n")
print(len(teams), "teams written")
