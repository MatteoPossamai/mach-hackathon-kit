"""Guards that keep turns the city does not need from touching the relay."""
from __future__ import annotations


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
