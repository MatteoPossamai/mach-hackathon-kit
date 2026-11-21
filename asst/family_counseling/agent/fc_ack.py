"""A warm first reply when the real one is slow (Ledger or relay under load).

Journey 394 (21/11 11:19 CET): a new citizen got no reply at all for 4+ minutes,
while the turn waited on the Ledger. city:review gives -1 to a citizen left without an answer
and +1 to a proactive, respectful one. The note goes out only if, after ACK_DELAY seconds,
nothing at all has been sent to the citizen after their message: fast turns are unchanged.
"""
from __future__ import annotations

import asyncio

ACK_DELAY = 8.0

ACK_IT = ("Grazie per avermi scritto: ho ricevuto il suo messaggio e me ne occupo subito. "
          "Controllo le disponibilità del consultorio e le scrivo appena ho gli orari.")
ACK_EN = ("Thank you for writing: I have received your message and I am working on it now. "
          "I am checking the clinic times and will write to you as soon as I have them.")


def first_contact(case: list, agent: str, citizen: str) -> bool:
    return not any(row.sender == agent and row.recipient == citizen for row in case)


async def with_delayed_ack(city, message, work, say, text: str, delay: float | None = None) -> None:
    """Run ``work``; if it is still silent towards the citizen after ``delay``, send ``text``."""

    async def ack() -> None:
        await asyncio.sleep(ACK_DELAY if delay is None else delay)
        try:
            rows = await city.history(need_id=message.need_id)
        except Exception:  # noqa: BLE001 - a busy relay is exactly when the note matters
            rows = []
        after = next((i for i, row in enumerate(rows) if row.msg_id == message.msg_id), -1)
        if any(row.sender == city.agent_id and row.recipient == message.sender for row in rows[after + 1:]):
            return
        await say(city, message.sender, text)

    task = asyncio.create_task(ack())
    try:
        await work
    finally:
        task.cancel()


HISTORY_RETRY = (1.0, 2.0, 4.0)


def resilient_history(city):
    """Make every ``city.history`` of this turn retry a busy relay (429/5xx/timeout).

    An unprotected history read that hit a 429 killed the turn before any reply: the citizen
    who wrote again at 12:02 (journey 2840007) still had no answer at 12:07."""
    if getattr(city, "_ack_resilient", False):
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
    city._ack_resilient = True
    return city


def untargeted_request(message, need, words) -> bool:
    """A typed need.request from an agent, with no text, for a service this agent does not run.

    Deploy conformance sends one (service=lab, "visit") and waits 5 s for the lease renew, which
    the harness sends only when the turn ends. No reply was ever sent to it, but the history read
    before deciding took > 5 s on a loaded relay: custody.ack failed (hospital 3x, GP 13:12)."""
    if not str(getattr(message, "sender", "")).startswith("agent:") or (message.text or "").strip():
        return False
    body = getattr(message, "body", None)
    if getattr(message, "type", "") != "need.request" or not hasattr(body, "get"):
        return False
    named = " ".join(str(body.get(key) or "") for key in ("procedure", "service_type", "service", "summary"))
    named = (named + " " + str(getattr(need, "service", "") or "")).lower()
    return not any(word in named for word in words)



# need_id -> monotonic time of the note this pod already sent for that case.
ACKED: dict[str, float] = {}


def should_note(message) -> bool:
    """A citizen's plain request (not a confirm, cancel, consent or check-in) that this pod has not noted."""
    from city_sdk.city import understood
    if not str(getattr(message, "sender", "")).startswith("citizen:"):
        return False
    key = getattr(message, "need_id", None) or getattr(message, "msg_id", "")
    if key in ACKED:
        return False
    try:
        kind = understood(message).type
    except Exception:  # noqa: BLE001 - a note is a courtesy, never a reason to fail the turn
        kind = ""
    return kind in ("", "need.request", "chat")


async def with_early_note(city, message, work, say, text: str, delay: float | None = None) -> None:
    """Run ``work``; if nothing reached the citizen after ``delay`` seconds, send ``text``.

    Unlike with_delayed_ack this starts before any history read: after 13:25 citizens stayed at
    msgs=1 for 4-15 minutes because the first history read itself hung on a congested relay."""
    import time as _t
    citizen = message.sender
    key = getattr(message, "need_id", None) or getattr(message, "msg_id", "")
    sent = {"citizen": False}
    raw = city.say

    async def tracked(to, body, *args, **kwargs):
        result = await raw(to, body, *args, **kwargs)
        if to == citizen:
            sent["citizen"] = True
        return result

    city.say = tracked

    async def note() -> None:
        await asyncio.sleep(ACK_DELAY if delay is None else delay)
        if sent["citizen"] or key in ACKED:
            return
        if len(ACKED) > 20000:
            ACKED.clear()
        ACKED[key] = _t.monotonic()
        await say(city, citizen, text)

    task = asyncio.create_task(note())
    try:
        await work
    finally:
        task.cancel()


# Early ack to other agents. city:clock gives +1 for a reply to another agent within 10 s; the
# ack used to wait for the first history read, which under relay load took longer than that.
AGENT_ACKED: dict[str, float] = {}
_NOT_REQUESTS = frozenset({"ack", "offer", "cancel", "booking.receipt", "handoff", "booking.notice",
                           "unavailable", "slots", "held", "committed", "refused"})


def agent_turn(message) -> str:
    """"request" for a partner need.request, "confirm" for a confirm, "" for anything else."""
    if not str(getattr(message, "sender", "")).startswith("agent:"):
        return ""
    text = (getattr(message, "text", "") or "").strip()
    head = text.split(None, 1)[0].lower() if text else ""
    kind = str(getattr(message, "type", "") or "")
    if head == "confirm" or (not head and kind == "confirm"):
        return "confirm"
    if head == "need.request" or (kind == "need.request" and head not in _NOT_REQUESTS):
        return "request"
    return ""


def agent_token(message, name: str) -> str:
    """A safe ``name=value`` token from the text or the typed body, or ""."""
    import re as _re
    value = ""
    for token in (getattr(message, "text", "") or "").split():
        if token.startswith(name + "="):
            value = token.split("=", 1)[1]
            break
    body = getattr(message, "body", None)
    if not value and hasattr(body, "get"):
        value = str(body.get(name) or "")
    return value if _re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", value or "") else ""


async def early_agent_ack(city, message, say, text: str) -> bool:
    """Send ``text`` to the sending agent once per inbound message, before any history read."""
    import time as _t
    key = getattr(message, "msg_id", "") or ""
    if not key or key in AGENT_ACKED:
        return False
    if len(AGENT_ACKED) > 20000:
        AGENT_ACKED.clear()
    AGENT_ACKED[key] = _t.monotonic()
    try:
        await say(city, message.sender, text)
    except Exception:  # noqa: BLE001 - a courtesy note never fails the turn
        return False
    return True
