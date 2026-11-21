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
