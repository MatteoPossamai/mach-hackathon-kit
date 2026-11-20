"""Small state and policy helpers for the specialist report lifecycle."""
from __future__ import annotations


def configure(agent_globals: dict[str, object]) -> None:
    globals().update(agent_globals)


# --- check-in, report sharing consent, CTRL-CARD follow-up, and report -------------------------


def _checked_in_booking_id(chat: list[Message]) -> str:
    row = _latest_ledger(chat, "checked_in")
    if row is None:
        return ""
    return _safe_token(fields_of(row.text).get("booking_id"))


def _checked_in_index(chat: list[Message], booking_id: str) -> int:
    token = f"booking_id={booking_id}"
    found = -1
    for idx, row in enumerate(chat):
        if row.sender == LEDGER and first_word(row.text) == "checked_in" and token in row.text.split():
            found = idx
    return found


def _booking_has_report(chat: list[Message], booking_id: str) -> bool:
    if not booking_id:
        return False
    return _latest_ledger(chat, "reported", f"booking_id={booking_id}") is not None


def _reported_booking_id(chat: list[Message]) -> str:
    row = _latest_ledger(chat, "reported")
    if row is None:
        return ""
    return _safe_token(fields_of(row.text).get("booking_id"))


def _asked_report_consent(chat: list[Message], agent: str, booking_id: str = "") -> bool:
    start = _checked_in_index(chat, booking_id) if booking_id else -1
    for idx, row in enumerate(chat):
        if idx <= start:
            continue
        if (
            row.sender == agent
            and row.recipient.startswith("citizen:")
            and "purpose=report_sharing" in row.text.split()
            and "consent" in words(row.text)
        ):
            return True
    return False


def _awaiting_report_consent(chat: list[Message], agent: str) -> bool:
    booking_id = _checked_in_booking_id(chat)
    if not booking_id:
        return False
    if _booking_has_report(chat, booking_id):
        return False
    if _we(chat, agent, f"booking_id={booking_id}", to=LEDGER, word="report"):
        return False
    return _asked_report_consent(chat, agent, booking_id)


def _consent_explicitly_declined(fields: dict[str, str]) -> bool:
    for key in ("granted", "accepted", "consent", "allow", "allowed"):
        if fields.get(key, "").lower() in {"false", "no", "0", "denied", "refused"}:
            return True
    if fields.get("refused", "").lower() in {"true", "yes", "1"}:
        return True
    return False


def _report_consent_ref(chat: list[Message], agent: str, booking_id: str = "") -> str:
    """Return a citizen's ``report_sharing`` consent_ref granted for the current booking."""
    start = _checked_in_index(chat, booking_id) if booking_id else -1
    asked_idx = -1
    for idx, row in enumerate(chat):
        if idx <= start:
            continue
        if (
            row.sender == agent
            and row.recipient.startswith("citizen:")
            and "purpose=report_sharing" in row.text.split()
        ):
            asked_idx = idx
    for idx, row in enumerate(chat):
        if idx <= start:
            continue
        if not row.sender.startswith("citizen:"):
            continue
        fields = msg_fields(row)
        ref = _safe_token(fields.get("consent_ref"))
        if not ref or _consent_explicitly_declined(fields):
            continue
        purpose = _safe_token(fields.get("purpose"))
        if purpose == "report_sharing" or (asked_idx >= 0 and idx > asked_idx):
            return ref
    return ""


def _booking_prescription_number(chat: list[Message], booking_id: str = "") -> str:
    """Return the prescription_number associated with ``booking_id`` (including CTRL-CARD follow-ups)."""
    if booking_id:
        for row in reversed(chat):
            toks = row.text.split()
            if f"hold_id={booking_id}" in toks or f"booking_id={booking_id}" in toks:
                num = _safe_token(msg_fields(row).get("prescription_number"))
                if num:
                    return num
        slot = ""
        for row in reversed(chat):
            if f"hold_id={booking_id}" in row.text.split():
                slot = _safe_token(fields_of(row.text).get("slot_id"))
                if slot:
                    break
        if slot:
            for row in reversed(chat):
                if f"slot_id={slot}" in row.text.split():
                    num = _safe_token(msg_fields(row).get("prescription_number"))
                    if num:
                        return num
    for row in reversed(chat):
        if row.sender == LEDGER and first_word(row.text) == "prescribed":
            continue
        num = _safe_token(msg_fields(row).get("prescription_number"))
        if num:
            return num
    return ""


def _follow_up_rx_number(booking_id: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in booking_id.upper()).strip("-")
    if len(cleaned) < 2:
        cleaned = (cleaned + "00")[:2]
    candidate = f"prescription_number-FU-{cleaned}"[:60]
    if _RX_RE.fullmatch(candidate):
        return candidate
    return "prescription_number-FU-CTRL"


def _needs_follow_up(chat: list[Message], rx_info: dict[str, str] | None) -> bool:
    """Decide whether a ``CTRL-CARD`` follow-up prescription is required by protocol.

    - Never chain a second follow-up on a ``CTRL-CARD`` visit, an existing
      ``follow_up_of`` prescription, or a routine ``SYM-FOLLOWUP`` visit.
    - Issue a ``CTRL-CARD`` follow-up only when the case explicitly says one is
      needed. A primary visit or its priority alone is not evidence that another
      prescription is appropriate.
    """
    if rx_info:
        if rx_info.get("follow_up_of"):
            return False
        if rx_info.get("procedure") == FOLLOW_UP_PROCEDURE:
            return False
        if rx_info.get("symptom_code") == "SYM-FOLLOWUP":
            return False
    for row in chat:
        # A citizen may request an appointment, but cannot authorize a clinical
        # prescription by mentioning a follow-up phrase in chat.
        if row.sender == LEDGER or row.sender.startswith("citizen:"):
            continue
        if row.sender.startswith("agent:") and row.sender not in _TEAM_AGENTS:
            continue
        fields = msg_fields(row)
        if fields.get("follow_up", "").lower() in {"true", "yes", "1"}:
            return True
        if fields.get("needs_follow_up", "").lower() in {"true", "yes", "1"}:
            return True
        lowered = row.text.lower()
        if any(phrase in lowered for phrase in _FOLLOW_UP_PHRASES):
            return True
    return False


