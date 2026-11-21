"""Real-time monitor dei journey: mostra in diretta TUTTI i messaggi scambiati,
i tempi per fase, la coda attiva e gli alert sui bug.

Uso (Python 3.12+, solo stdlib):
    # Live polling dall'endpoint Journeys della console squadra:
    uv run --no-project python asst/tools/journey_watch.py --url "<ENDPOINT_JOURNEYS>" --interval 5

    # Con salvataggio su file log (senza codici colore):
    uv run --no-project python asst/tools/journey_watch.py --url "<ENDPOINT_JOURNEYS>" --log-file live.log

    # Se la lista non include i messaggi, aggiungi il template del dettaglio:
    uv run --no-project python asst/tools/journey_watch.py --url "<LIST_URL>" --detail-url "<BASE>/{need_id}"

    # Con cookie del browser (se la console non accetta il Bearer token):
    uv run --no-project python asst/tools/journey_watch.py --url "<ENDPOINT_JOURNEYS>" --cookie "session=..."

    # Modalità lettura immediata su un file JSON esportato:
    uv run --no-project python asst/tools/journey_watch.py --file failed.json
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

AGENTS = {
    "agent:c2ewegr2ea3v": "GP",
    "agent:gdrf9b9kxxss": "BC",
    "agent:nanpr5mgm2py": "HOSP",
    "agent:pky3vqyqbepa": "SPEC",
    "agent:atp772whjkgd": "CONS",
    "city:ledger": "LEDGER",
}

ANSI = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if ANSI else text


def now_hms() -> str:
    return datetime.now().strftime("%H:%M:%S")


def parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def fmt_ts(ts: datetime | None) -> str:
    if ts is None:
        return "--:--:--"
    return ts.astimezone().strftime("%H:%M:%S")


def fmt_dur(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "-"
    s = int(round(seconds))
    m, sec = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h{m:02d}m{sec:02d}s"
    if m:
        return f"{m}m{sec:02d}s"
    return f"{sec}s"


def role_of(party: str) -> str:
    if party in AGENTS:
        return AGENTS[party]
    if party.startswith("citizen:"):
        return "CIT"
    if party.startswith("agent:"):
        return "PARTNER"
    return party or "?"


def default_token(team: str) -> str:
    path = Path.home() / ".config" / "city" / "teams" / team / "session"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def fetch_json(url: str, *, token: str = "", cookie: str = "", timeout: float = 15.0) -> Any:
    headers = {"User-Agent": "journey-watch/1.1", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def load_concatenated_json(text: str) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()
    idx, length = 0, len(text)
    out: list[dict[str, Any]] = []
    while idx < length:
        while idx < length and text[idx].isspace():
            idx += 1
        if idx >= length:
            break
        obj, idx = decoder.raw_decode(text, idx)
        if isinstance(obj, list):
            out.extend(x for x in obj if isinstance(x, dict))
        elif isinstance(obj, dict):
            if "journeys" in obj and isinstance(obj["journeys"], list):
                out.extend(x for x in obj["journeys"] if isinstance(x, dict))
            elif "items" in obj and isinstance(obj["items"], list):
                out.extend(x for x in obj["items"] if isinstance(x, dict))
            else:
                out.append(obj)
    return out


def msg_text(msg: dict[str, Any]) -> str:
    body = msg.get("body")
    if isinstance(body, dict):
        return str(body.get("text") or "")
    return str(msg.get("text") or "")


def sorted_messages(raw: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(
        [m for m in (raw.get("messages") or []) if isinstance(m, dict)],
        key=lambda m: str(m.get("sent_at") or ""),
    )


def emit(line: str, log_fp: TextIO | None = None) -> None:
    print(line, flush=True)
    if log_fp is not None:
        log_fp.write(ANSI_RE.sub("", line) + "\n")
        log_fp.flush()


def format_message_lines(
    short_id: str,
    msg: dict[str, Any],
    prev_ts: datetime | None,
    *,
    max_chars: int = 0,
) -> list[str]:
    frm = role_of(str(msg.get("from") or ""))
    to = role_of(str(msg.get("to") or ""))
    ts = parse_ts(msg.get("sent_at"))
    delta = f"+{fmt_dur((ts - prev_ts).total_seconds())}" if (ts and prev_ts) else "+0s"
    txt = msg_text(msg).strip()
    if max_chars > 0 and len(txt) > max_chars:
        txt = txt[:max_chars] + "..."

    arrow = f"{frm}->{to}"
    if frm == "CIT":
        arrow_col = c(f"{arrow:<11}", "33;1")  # giallo: cittadino
    elif frm == "GP":
        arrow_col = c(f"{arrow:<11}", "32;1")  # verde: medico di base
    elif frm == "BC":
        arrow_col = c(f"{arrow:<11}", "34;1")  # blu: centro prenotazioni
    else:
        arrow_col = c(f"{arrow:<11}", "35;1")  # magenta: facility/partner

    prefix = f"  [{fmt_ts(ts)} {delta:>7}] {short_id} {arrow_col} | "
    indent = " " * len(ANSI_RE.sub("", prefix))
    lines = txt.splitlines() or [""]
    out = [prefix + lines[0]]
    for extra in lines[1:]:
        out.append(indent + extra)
    return out


def classify_waiting(msg: dict[str, Any]) -> str:
    frm = role_of(str(msg.get("from") or ""))
    to = role_of(str(msg.get("to") or ""))
    txt = msg_text(msg)
    if frm == "CIT":
        return to
    if frm == "GP":
        if txt.startswith("prescription "):
            return "CIT"
        if "escalate.emergency" in txt:
            return "DONE"
        return "GP"
    if frm == "BC":
        if txt.startswith(("offer ", "consent ", "data.request ")):
            return "CIT"
        if txt.startswith("booking.receipt"):
            return "FACILITY"
        return "BC"
    return to


@dataclass
class JourneyStats:
    need_id: str
    short_id: str
    level: Any
    state: str
    score: Any
    started_at: datetime | None = None
    closed_at: datetime | None = None
    last_msg_at: datetime | None = None
    gp_dur: float | None = None
    bc_dur: float | None = None
    total_dur: float | None = None
    waiting_on: str = "?"
    procedure: str = ""
    priority: str = ""
    lang: str = ""
    s_eff: int | None = None
    bc_reason: str = ""
    alerts: list[str] = field(default_factory=list)


def analyze_journey(raw: dict[str, Any]) -> JourneyStats:
    j = raw.get("journey") if isinstance(raw.get("journey"), dict) else raw
    need_id = str(j.get("need_id") or raw.get("need_id") or "?")
    state = str(j.get("state") or "open")
    st = JourneyStats(
        need_id=need_id,
        short_id=need_id[-10:],
        level=j.get("level", "?"),
        state=state,
        score=j.get("score"),
    )

    msgs = sorted_messages(raw)
    gp_start: datetime | None = None
    gp_rx_at: datetime | None = None
    bc_start: datetime | None = None
    bc_end: datetime | None = None
    offer_hashes: dict[str, int] = {}
    cancel_reasons: dict[str, int] = {}
    gp_sent_rx = False
    gp_false_refused = False

    for m in msgs:
        ts = parse_ts(m.get("sent_at"))
        if ts and st.started_at is None:
            st.started_at = ts
        if ts:
            st.last_msg_at = ts
        st.waiting_on = classify_waiting(m)

        frm = role_of(str(m.get("from") or ""))
        to = role_of(str(m.get("to") or ""))
        txt = msg_text(m)

        if frm == "CIT" and to == "GP" and gp_start is None:
            gp_start = ts
        if frm == "GP" and txt.startswith("prescription "):
            gp_sent_rx = True
            if gp_rx_at is None:
                gp_rx_at = ts
            mp = re.search(r"procedure=([A-Z0-9-]+)", txt)
            mpr = re.search(r"priority=([UBDP])", txt)
            if mp:
                st.procedure = mp.group(1)
            if mpr:
                st.priority = mpr.group(1)
        if frm == "GP" and gp_sent_rx and "non ha confermato la ricetta" in txt.lower():
            gp_false_refused = True
        if frm == "CIT" and to == "BC" and bc_start is None:
            bc_start = ts
            ml = re.search(r"lang=([a-z]{2,3})", txt)
            if ml:
                st.lang = ml.group(1)
        if frm == "BC" and txt.startswith("offer "):
            offer_hashes[txt] = offer_hashes.get(txt, 0) + 1
            if bc_end is None:
                bc_end = ts
        if frm == "BC" and txt.startswith("booking.receipt"):
            bc_end = ts
        if frm == "CIT" and "cancel" in txt.split():
            mr = re.search(r"reason=([A-Za-z0-9_.-]+)", txt)
            if mr:
                r = mr.group(1)
                cancel_reasons[r] = cancel_reasons.get(r, 0) + 1

    if gp_start and gp_rx_at:
        st.gp_dur = (gp_rx_at - gp_start).total_seconds()
    if bc_start and bc_end:
        st.bc_dur = (bc_end - bc_start).total_seconds()

    verdict = raw.get("verdict") if isinstance(raw.get("verdict"), dict) else None
    if verdict:
        st.closed_at = parse_ts(verdict.get("time"))
        ans = verdict.get("answer_set")
        if isinstance(ans, dict) and ans.get("effective_size") is not None:
            st.s_eff = int(ans["effective_size"])
        for row in verdict.get("rows") or []:
            if isinstance(row, dict) and role_of(str(row.get("subject") or "")) == "BC":
                st.bc_reason = str(row.get("reason") or "")

    if st.started_at:
        end_ref = st.closed_at or (st.last_msg_at if is_closed(state) else None)
        if end_ref:
            st.total_dur = (end_ref - st.started_at).total_seconds()

    max_dup = max(offer_hashes.values(), default=0)
    if max_dup > 1:
        st.alerts.append(f"DUP_OFFER(x{max_dup})")
    for r, cnt in sorted(cancel_reasons.items()):
        st.alerts.append(f"CANCEL_{r}(x{cnt})")
    if gp_false_refused:
        st.alerts.append("GP_FALSE_REFUSED")
    if st.bc_reason == "no_booking" and (st.s_eff or 0) > 0:
        st.alerts.append(f"MISSED(S_eff={st.s_eff})")

    return st


def is_closed(state: str) -> bool:
    return state.lower() in {"done", "failed", "unserved", "escalated", "city_fault", "no_reply", "undelivered"}


def format_status_line(st: JourneyStats) -> str:
    state_tag = st.state.upper()
    if state_tag == "DONE":
        state_col = c(f"{state_tag:<6}", "32;1")
    elif state_tag in {"FAILED", "UNSERVED"}:
        state_col = c(f"{state_tag:<6}", "31;1")
    else:
        state_col = c(f"{state_tag:<6}", "33;1")

    meta = f"{st.procedure or '?':<8} P={st.priority or '?'} {st.lang or '-':<2}"
    times = f"GP={fmt_dur(st.gp_dur):>6} BC={fmt_dur(st.bc_dur):>6} TOT={fmt_dur(st.total_dur):>6}"
    score = f"score={st.score}" if st.score is not None else "score=-"
    reason = f"({st.bc_reason})" if st.bc_reason else ""
    alerts = c(" " + " ".join(st.alerts), "31") if st.alerts else ""
    return f"[{now_hms()}] {state_col} {st.short_id} L{st.level} | {meta} | {times} | {score} {reason}{alerts}"


def print_queue_summary(states: dict[str, JourneyStats], *, now_utc: datetime, log_fp: TextIO | None) -> None:
    open_items = [s for s in states.values() if not is_closed(s.state)]
    closed_items = [s for s in states.values() if is_closed(s.state)]
    by_wait: dict[str, int] = {}
    oldest_age = 0.0
    for s in open_items:
        by_wait[s.waiting_on] = by_wait.get(s.waiting_on, 0) + 1
        if s.started_at:
            oldest_age = max(oldest_age, (now_utc - s.started_at).total_seconds())

    avg_close = None
    durs = [s.total_dur for s in closed_items if s.total_dur is not None]
    if durs:
        avg_close = sum(durs) / len(durs)

    done_n = sum(1 for s in closed_items if s.state.lower() == "done")
    fail_n = sum(1 for s in closed_items if s.state.lower() == "failed")
    queue_parts = " ".join(f"{k}:{v}" for k, v in sorted(by_wait.items())) or "vuota"
    line = (
        f"[{now_hms()}] === QUEUE: {len(open_items)} aperte ({queue_parts}) | "
        f"oldest={fmt_dur(oldest_age if open_items else None)} | "
        f"CHIUSE: {len(closed_items)} (done={done_n} fail={fail_n}) | "
        f"avg_close={fmt_dur(avg_close)} ==="
    )
    emit(c(line, "36;1"), log_fp)


def extract_journeys(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("journeys", "items", "data", "results"):
            if isinstance(payload.get(key), list):
                return [x for x in payload[key] if isinstance(x, dict)]
        return [payload]
    return []


def msg_key(msg: dict[str, Any], idx: int) -> str:
    return str(msg.get("msg_id") or f"{idx}:{msg.get('sent_at')}:{msg.get('from')}")


def print_new_messages(
    raw: dict[str, Any],
    st: JourneyStats,
    seen_ids: set[str],
    *,
    max_chars: int,
    log_fp: TextIO | None,
) -> bool:
    msgs = sorted_messages(raw)
    prev_ts: datetime | None = None
    printed = False
    for idx, m in enumerate(msgs):
        ts = parse_ts(m.get("sent_at"))
        key = msg_key(m, idx)
        if key not in seen_ids:
            seen_ids.add(key)
            for line in format_message_lines(st.short_id, m, prev_ts, max_chars=max_chars):
                emit(line, log_fp)
            printed = True
        if ts:
            prev_ts = ts
    return printed


def run_file(args: argparse.Namespace, log_fp: TextIO | None) -> int:
    raw_items = load_concatenated_json(args.file.read_text(encoding="utf-8-sig"))
    states: dict[str, JourneyStats] = {}
    for item in raw_items:
        st = analyze_journey(item)
        if args.need and args.need not in st.need_id:
            continue
        states[st.need_id] = st
        emit(format_status_line(st), log_fp)
        if not args.summary_only:
            print_new_messages(item, st, set(), max_chars=args.max_chars, log_fp=log_fp)
    print_queue_summary(states, now_utc=datetime.now(timezone.utc), log_fp=log_fp)
    return 0


DEFAULT_LIST_URL = "https://projectnandaitaly.ai/v1/team/journeys"
DEFAULT_DETAIL_URL = "https://projectnandaitaly.ai/v1/team/journeys/{need_id}"


def run_live(args: argparse.Namespace, log_fp: TextIO | None) -> int:
    token = args.token or default_token(args.team)
    seen_msg_ids: dict[str, set[str]] = {}
    seen_state: dict[str, str] = {}
    closed_cache: dict[str, dict[str, Any]] = {}
    states: dict[str, JourneyStats] = {}
    ticks = 0

    def load_detail(item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        j = item.get("journey") if isinstance(item.get("journey"), dict) else item
        need_id = str(j.get("need_id") or "")
        if not need_id:
            return "", item
        if need_id in closed_cache:
            return need_id, closed_cache[need_id]
        if args.detail_url and not item.get("messages") and not args.summary_only:
            try:
                detail = fetch_json(args.detail_url.format(need_id=need_id), token=token, cookie=args.cookie)
                if isinstance(detail, dict):
                    dj = detail.get("journey") if isinstance(detail.get("journey"), dict) else detail
                    if is_closed(str(dj.get("state") or "")):
                        closed_cache[need_id] = detail
                    return need_id, detail
            except Exception:
                pass
        return need_id, item

    while True:
        try:
            payload = fetch_json(args.url, token=token, cookie=args.cookie)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            emit(c(f"[{now_hms()}] ERRORE lettura {args.url}: {exc}", "31"), log_fp)
            if args.once:
                return 1
            time.sleep(args.interval)
            continue

        all_items = extract_journeys(payload)
        # Update overall queue counts from the full list
        for item in all_items:
            j = item.get("journey") if isinstance(item.get("journey"), dict) else item
            nid = str(j.get("need_id") or "")
            if nid and nid not in states:
                states[nid] = analyze_journey(item)

        filtered = []
        for item in all_items:
            j = item.get("journey") if isinstance(item.get("journey"), dict) else item
            nid = str(j.get("need_id") or "")
            if not nid or (args.need and args.need not in nid):
                continue
            filtered.append(item)
        if args.limit > 0:
            filtered = filtered[:args.limit]

        with ThreadPoolExecutor(max_workers=8) as pool:
            loaded = list(pool.map(load_detail, filtered))

        changed = False
        for need_id, item in reversed(loaded):
            if not need_id:
                continue
            st = analyze_journey(item)
            states[need_id] = st
            seen = seen_msg_ids.setdefault(need_id, set())
            prev_s = seen_state.get(need_id)

            new_msgs = False
            if not args.summary_only:
                new_msgs = print_new_messages(item, st, seen, max_chars=args.max_chars, log_fp=log_fp)

            if new_msgs or st.state != prev_s:
                seen_state[need_id] = st.state
                emit(format_status_line(st), log_fp)
                changed = True

        ticks += 1
        if changed or ticks == 1 or ticks % 6 == 0:
            print_queue_summary(states, now_utc=datetime.now(timezone.utc), log_fp=log_fp)

        if args.once:
            return 0
        time.sleep(args.interval)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=DEFAULT_LIST_URL, help=f"Endpoint HTTP Journeys (default: {DEFAULT_LIST_URL})")
    p.add_argument("--detail-url", default=DEFAULT_DETAIL_URL, help=f"Template dettaglio (default: {DEFAULT_DETAIL_URL})")
    p.add_argument("--limit", type=int, default=15, help="Numero di journey più recenti di cui scaricare i messaggi (default: 15, 0=tutti)")
    p.add_argument("--team", default="ltf-jecatt", help="Nome squadra per leggere ~/.config/city/teams/<team>/session")
    p.add_argument("--token", default="", help="Bearer token esplicito (sovrascrive il file session)")
    p.add_argument("--cookie", default="", help="Header Cookie della console browser, se richiesto")
    p.add_argument("--cookie-file", type=Path, help="File con header Cookie (es. ~/.config/city/console_auth)")
    p.add_argument("--interval", type=float, default=5.0, help="Secondi tra un poll e il successivo (default 5)")
    p.add_argument("--once", action="store_true", help="Stampa lo stato attuale ed esce")
    p.add_argument("--file", type=Path, help="Analizza subito un file JSON di journey")
    p.add_argument("--log-file", type=Path, help="Salva tutto il log anche su questo file")
    p.add_argument("--need", default="", help="Filtra solo i journey il cui need_id contiene questa stringa")
    p.add_argument("--max-chars", type=int, default=0, help="Tronca i messaggi oltre N caratteri (0 = testo completo)")
    p.add_argument("--summary-only", action="store_true", help="Mostra solo riepilogo/coda senza i testi dei messaggi")
    args = p.parse_args()

    if not args.cookie and args.cookie_file:
        args.cookie = args.cookie_file.expanduser().read_text(encoding="utf-8").strip()

    log_fp = args.log_file.open("a", encoding="utf-8") if args.log_file else None
    try:
        if args.file:
            return run_file(args, log_fp)
        return run_live(args, log_fp)
    finally:
        if log_fp is not None:
            log_fp.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
