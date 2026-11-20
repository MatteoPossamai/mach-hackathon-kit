"""Watch "Il Token della Sera" on the public scoreboard and report every change.

Reads https://projectnandaitaly.ai/v1/scoreboard (public, read-only) every
--interval seconds. It prints a line, with the terminal bell, for each new
dispatch (levels, faults, rules, arrivals) and for each change of wave level,
state, rule_notice or active_rules.

Usage (Python 3.12+, stdlib only):
    python3 asst/tools/token_watch.py              # watch, 60 s interval
    python3 asst/tools/token_watch.py --once       # print the current state and exit
    python3 asst/tools/token_watch.py --quiet-arrivals   # hide the "Nuovo piano degli arrivi" dispatches
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

URL = "https://projectnandaitaly.ai/v1/scoreboard"
WAVE_KEYS = ("state", "level", "rule_notice", "active_rules")
# Every dispatch kind rings the bell except the routine arrivals plan.
QUIET_KINDS = {"arrivals"}


def now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def local_time(stamp: str) -> str:
    """ISO UTC timestamp -> local HH:MM."""
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone().strftime("%H:%M")
    except ValueError:
        return stamp


def fetch(timeout: float) -> dict:
    request = urllib.request.Request(URL, headers={"User-Agent": "token-watch/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def say(line: str, *, bell: bool) -> None:
    print(("\a" if bell else "") + line, flush=True)


def show_dispatch(item: dict, *, bell: bool) -> None:
    kind = str(item.get("kind", "?"))
    loud = kind not in QUIET_KINDS
    marker = "!!" if loud else "--"
    say(
        f"[{now()}] {marker} TOKEN {local_time(str(item.get('at', '')))} {kind}: "
        f"{item.get('headline', '')} | {item.get('body', '')}",
        bell=bell and loud,
    )


def wave_view(data: dict) -> dict:
    wave = data.get("wave") or {}
    return {key: wave.get(key) for key in WAVE_KEYS}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--interval", type=float, default=60.0, help="seconds between reads (default 60)")
    parser.add_argument("--once", action="store_true", help="print the current state and exit")
    parser.add_argument("--quiet-arrivals", action="store_true", help="hide arrivals dispatches")
    parser.add_argument("--history", type=int, default=5, help="dispatches shown at start (default 5)")
    args = parser.parse_args()

    seen: set[str] = set()
    last_wave: dict | None = None
    failures = 0

    while True:
        try:
            data = fetch(timeout=20)
            failures = 0
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            failures += 1
            if failures in (1, 5) or failures % 30 == 0:
                say(f"[{now()}] ?? scoreboard not readable ({exc}); retrying", bell=False)
            if args.once:
                return 1
            time.sleep(args.interval)
            continue

        press = [p for p in data.get("press") or [] if isinstance(p, dict)]
        if args.quiet_arrivals:
            press = [p for p in press if p.get("kind") != "arrivals"]
        wave = wave_view(data)

        if last_wave is None:
            say(
                f"[{now()}] == level {wave['level']} | wave {wave['state']} | "
                f"rule_notice {wave['rule_notice']} | active_rules {wave['active_rules']}",
                bell=False,
            )
            levels = [p for p in press if p.get("kind") == "level"]
            if levels:
                say(f"[{now()}] == current level: {levels[-1].get('headline')} | {levels[-1].get('body')}", bell=False)
            for item in press[-args.history:] if args.history > 0 else []:
                show_dispatch(item, bell=False)
            seen.update(str(p.get("id")) for p in press)
        else:
            for key in WAVE_KEYS:
                if wave[key] != last_wave[key]:
                    say(f"[{now()}] !! WAVE {key}: {last_wave[key]} -> {wave[key]}", bell=True)
            for item in press:
                ident = str(item.get("id"))
                if ident not in seen:
                    seen.add(ident)
                    show_dispatch(item, bell=True)
        last_wave = wave

        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
