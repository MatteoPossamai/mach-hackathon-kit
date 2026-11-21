"""Shared helpers: message tokens, the Ledger mailbox, case history lookups."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime, timedelta

from city_sdk.city import LEDGER, CapError, City, Need, clinical, OutOfScope, understood

TOKEN = re.compile(r"^[A-Za-z0-9_.:/+\-]{1,128}$")
# A prescription number written in free text, without the "prescription_number=" key.
RX_IN_TEXT = re.compile(r"prescription_number-[A-Za-z0-9-]{4,60}")
DAY_CODES = ("mon", "tue", "wed", "thu", "fri")
SERVICES = {"VIS-FIS": "hospital.physiatry", "VIS-CARD": "specialist_care.cardiology"}


# Level 3: a citizen may write only the case code, e.g. "la ricetta N-2026-11-02-890910000081".
NEED_IN_TEXT = re.compile(r"(?<![A-Za-z0-9_-])N-\d{4}-\d{2}-\d{2}-\d{4,}")


def rx_from_text(history: list, citizen: str, text: str) -> str:
    """A prescription number found in free text, newest first, citizen messages only."""

    for candidate in [text] + [row.text for row in reversed(history) if row.sender == citizen]:
        found = RX_IN_TEXT.search(candidate or "")
        if found and TOKEN.fullmatch(found.group(0)):
            return found.group(0)
        bare = NEED_IN_TEXT.search(candidate or "")
        if bare and TOKEN.fullmatch("prescription_number-" + bare.group(0)):
            return "prescription_number-" + bare.group(0)
    return ""


def rx_guess(need_id: str) -> str:
    """The number our GP gives this case's prescription (same rule as the GP's ``_rx``).

    Only a guess: the Ledger must confirm it before anything is offered.
    """

    raw = (need_id or "").upper()
    suffix = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in raw).strip("-")
    number = "prescription_number-" + suffix[:40]
    return number if len(suffix) >= 4 and TOKEN.fullmatch(number) else ""


def ledger_refused(history: list, seen: set, op: str) -> bool:
    """A Ledger refusal of ``op`` that arrived after ``seen`` was taken."""

    return any(row.sender == LEDGER and row.msg_id not in seen and act(row.text) == "refused"
               and op in row.text.lower() for row in history)


# Our own agents (asst/*/card.yaml): never a cross-ASST partner.
TEAM_AGENTS = frozenset({"agent:gdrf9b9kxxss", "agent:nanpr5mgm2py", "agent:c2ewegr2ea3v",
                         "agent:pky3vqyqbepa", "agent:atp772whjkgd"})
CAPABILITY = {"VIS-FIS": "hospital.physiatry", "VIS-CARD": "specialist_care.cardiology",
              "CTRL-CARD": "specialist_care.cardiology", "CONS-GIN": "family_counseling.gynecology"}


def fields(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in text.split():
        key, sep, value = item.strip(".,;!?()\"'").partition("=")
        if sep and key not in result and TOKEN.fullmatch(value):
            result[key] = value
    return result


def act(text: str) -> str:
    return text.strip().split(maxsplit=1)[0] if text.strip() else ""


def time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def citizen_of(history: list) -> str:
    return next((m.sender for m in history if m.sender.startswith("citizen:")), "")


def sent(history: list, agent: str, recipient: str, prefix: str) -> bool:
    return any(m.sender == agent and m.recipient == recipient and m.text.startswith(prefix) for m in history)


# understood() keeps the LAST act word of a text: a bare "prescription" or "offer" in the
# sentence would change what the citizen reads. Swap such words in the prose (MACH-1.1).
PROSE_SWAP = {
    "offer": "propose", "prescription": "referral", "confirm": "approve", "cancel": "call off",
    "consent": "permission", "checkin": "check-in", "report": "findings", "hold": "keep",
}


def clean_prose(text: str) -> str:
    out = []
    for line in text.split("\n"):
        head, sep, rest = line.partition(" ")
        if rest:
            rest = " ".join(word if "=" in word else PROSE_SWAP.get(word, word) for word in rest.split(" "))
        out.append(head + sep + rest)
    return "\n".join(out)


_RECENT_SENDS: dict[tuple[str, str], float] = {}
LAST_SENT: dict[str, float] = {}  # recipient -> monotonic time of our last message
DEDUP_SECONDS = 120.0


async def send(city: City, recipient: str, text: str) -> bool:
    if recipient != LEDGER and text:
        text = clean_prose(text)
        # Parallel turns (a burst of facility cancels) used to send the same offers or the
        # same cancel 4 times; the citizen then refused the repeats. Same text, same peer: once.
        import time as _t
        key = (recipient, hashlib.sha256(text.encode()).hexdigest())
        now = _t.monotonic()
        LAST_SENT[recipient] = now
        if now - _RECENT_SENDS.get(key, -1e9) < DEDUP_SECONDS:
            return True
        if len(_RECENT_SENDS) > 5000:
            _RECENT_SENDS.clear()
        _RECENT_SENDS[key] = now
    try:
        await city.say(recipient, text)
        return True
    except CapError:
        # A 4xx is final. Do not repeat it or pretend a booking succeeded.
        return False


HISTORY_POLL = 1.0  # each history read is up to 5 signed relay pages: poll gently under load
HISTORY_RETRY = (1.0, 2.0, 4.0)


def resilient_history(city: City) -> City:
    """Make every ``city.history`` of this turn retry a busy relay instead of crashing the turn."""
    if getattr(city, "_bc_resilient", False):
        return city
    raw = city.history

    async def history(*args, **kwargs):
        for pause in HISTORY_RETRY:
            try:
                return await raw(*args, **kwargs)
            except Exception:  # noqa: BLE001 - CapError, httpx timeouts: transient under load
                await asyncio.sleep(pause)
        return await raw(*args, **kwargs)

    city.history = history
    city._bc_resilient = True
    return city


async def wait_for(city: City, need_id: str, predicate, seconds: float = 6.0) -> list:
    """Ledger replies may be in history without opening a new agent turn.

    A relay timeout or 429/5xx while polling used to escape the turn: the brain crashed, the
    citizen got silence and the redelivered message ran the brain twice (duplicate offers).
    Keep the last good read and poll again instead."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    history = None
    while True:
        try:
            history = await city.history(need_id=need_id)
        except Exception:  # noqa: BLE001 - CapError, httpx timeouts: the relay is busy, not final
            if history is None and loop.time() >= deadline:
                history = await city.history(need_id=need_id)
        if history is not None and (predicate(history) or loop.time() >= deadline):
            return history
        await asyncio.sleep(max(0.05, min(HISTORY_POLL, deadline - loop.time())))


# A silent Ledger is load, not an answer: S counts every slot, so giving up costs -1 no_booking.
# Keep asking for up to LEDGER_PATIENCE seconds, with a growing pause so we do not add to the load.
LEDGER_PATIENCE = 300.0
LEDGER_BACKOFF = (0.0, 3.0, 6.0, 12.0, 20.0, 30.0)


async def ledger_pause(attempt: int, started: float) -> bool:
    """Wait before Ledger retry number ``attempt``; False once the patience budget is spent."""
    loop = asyncio.get_running_loop()
    pause = LEDGER_BACKOFF[min(attempt, len(LEDGER_BACKOFF) - 1)]
    if loop.time() + pause - started > LEDGER_PATIENCE:
        return False
    await asyncio.sleep(pause)
    return True


def ledger_reply(history: list, head: str, match: tuple[str, str] | None = None):
    for row in reversed(history):
        if row.sender == LEDGER and act(row.text) == head:
            if match is None or fields(row.text).get(match[0]) == match[1]:
                return row
    return None


def offers(history: list, agent: str, citizen: str) -> dict[str, dict[str, str]]:
    """Recover the choices actually sent, including multiple offers in one message."""
    found = {}
    for row in history:
        if row.sender != agent or row.recipient != citizen:
            continue
        for line in row.text.splitlines():
            if act(line) == "offer":
                item = fields(line)
                if item.get("offer_id"):
                    found[item["offer_id"]] = item
    return found


def fresh_reply(history: list, seen: set, head: str, match: tuple[str, str] | None = None):
    """The newest Ledger reply that arrived after ``seen`` was taken."""

    for row in reversed(history):
        if row.msg_id in seen or row.sender != LEDGER or act(row.text) != head:
            continue
        if match is None or fields(row.text).get(match[0]) == match[1]:
            return row
    return None


def slots_page(row) -> str:
    return fields(row.text.splitlines()[0]).get("page", "1/1").split("/")[0]


def case_number(history: list, citizen: str) -> str:
    rx = ledger_reply(history, "prescription")
    number = fields(rx.text).get("prescription_number", "") if rx else ""
    return number or rx_from_text(history, citizen, "")


def asked_consent(history: list, agent: str, citizen: str, purpose: str) -> bool:
    return (sent(history, agent, citizen, f"data.request purpose={purpose} fields=consent_ref")
            or sent(history, agent, citizen, f"consent purpose={purpose}"))


def last_asked_consent(history: list, agent: str, citizen: str) -> str:
    """If our latest message to the citizen asked for consent, return its purpose."""
    return consent_request_purpose(last_sent_to_citizen(history, agent, citizen))


def last_sent_to_citizen(history: list, agent: str, citizen: str) -> str:
    return next((row.text for row in reversed(history)
                 if row.sender == agent and row.recipient == citizen), "")


def consent_request_purpose(text: str) -> str:
    data = fields(text)
    purpose = data.get("purpose", "")
    if purpose in {"booking", "cross_asst"} and (
        act(text) == "consent" or (act(text) == "data.request" and data.get("fields") == "consent_ref")
    ):
        return purpose
    return ""


def consent_reply(row) -> tuple[str, str, bool]:
    """Return the scope, reference and explicit refusal of a protocol reply."""
    parsed = understood(row)
    if parsed.type not in {"consent", "data.response"}:
        return "", "", False
    body = parsed.body
    provided = body.get("provided")
    ref = body.get("consent_ref")
    if not ref and isinstance(provided, dict):
        ref = provided.get("consent_ref")
    purpose = body.get("purpose", "")
    denied = body.get("granted") in (False, "false", "False")
    return (purpose if isinstance(purpose, str) else "",
            ref if isinstance(ref, str) and TOKEN.fullmatch(ref) else "", denied)


def cross_consent(history: list, agent: str, citizen: str) -> str:
    requested = False
    pending = ""
    cross = ""
    for row in history:
        if row.sender == agent and row.recipient == citizen:
            pending = consent_request_purpose(row.text)
            requested = requested or pending == "cross_asst"
        elif requested and row.sender == citizen and row.recipient in {"", agent}:
            purpose, ref, denied = consent_reply(row)
            if (purpose or pending) == "cross_asst":
                if denied:
                    cross = ""
                elif ref:
                    cross = ref
    return cross


AGENDA_RATE = 60.0  # one real second is one agenda minute (wave rate)


def agenda_clock(row):
    """``agenda_now`` of a Ledger agenda reply, moved forward by the real time since it was sent.

    A reply read again from history minutes later would otherwise offer slots already past."""

    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    if row is None:
        return None
    now = time(fields(row.text).get("agenda_now", ""))
    sent = getattr(row, "sent_at", "") or ""
    if now is None or not sent:
        return now
    try:
        stamp = _dt.fromisoformat(sent.replace("Z", "+00:00"))
    except ValueError:
        return now
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=_tz.utc)
    elapsed = (_dt.now(_tz.utc) - stamp).total_seconds()
    if elapsed <= 0 or elapsed > 6 * 3600:
        return now
    return now + _td(seconds=elapsed * AGENDA_RATE)


# The last Ledger agenda reply this pod saw, reused by later cases (agenda_clock moves it forward
# by the real time since it was sent). At 13:50 every "agenda" queued ~17 min behind the others.
AGENDA_TTL = 120.0
AGENDA_SEEN: list = [None, 0.0]  # [row, loop-independent monotonic time]


def remember_agenda(row) -> None:
    import time as _mono
    if row is not None and agenda_clock(row) is not None:
        AGENDA_SEEN[0], AGENDA_SEEN[1] = row, _mono.monotonic()


def cached_agenda():
    """A recent agenda reply of this pod, or None (then the case asks the Ledger)."""
    import time as _mono
    row, at = AGENDA_SEEN
    if row is None or AGENDA_TTL <= 0 or _mono.monotonic() - at > AGENDA_TTL:
        return None
    return row


# (need_id, purpose) -> monotonic time of the consent request sent by this pod.
_CONSENT_ASKED: dict[tuple[str, str], float] = {}


async def ask_consent_once(city: City, need_id: str, history: list, citizen: str, purpose: str, text: str) -> bool:
    """Ask the citizen for one consent purpose at most once per case.

    Journey 374: several partner replies opened parallel turns on different pods; each read a
    history without the request just sent and asked again (6 times), and the citizen answered
    "Which consent_ref?". Guard per pod, then re-read the history right before sending."""
    import time as _t
    key = (need_id, purpose)
    if key in _CONSENT_ASKED or asked_consent(history, city.agent_id, citizen, purpose):
        return False
    try:
        fresh = await city.history(need_id=need_id)
    except Exception:  # noqa: BLE001 - a busy relay must not cost the citizen the question
        fresh = history
    if asked_consent(fresh, city.agent_id, citizen, purpose) or key in _CONSENT_ASKED:
        return False
    if len(_CONSENT_ASKED) > 5000:
        _CONSENT_ASKED.clear()
    _CONSENT_ASKED[key] = _t.monotonic()
    return await send(city, citizen, text)
