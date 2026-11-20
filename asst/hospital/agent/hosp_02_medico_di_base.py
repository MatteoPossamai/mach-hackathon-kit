# Part of hospital/agent/agent.py: loaded into its namespace, in order.
from __future__ import annotations

# --- medico di base ----------------------------------------------------------------------------


def _emergency(message: Message) -> bool:
    if understood(message).body.get("is_emergency") is True:
        return True
    lowered = message.text.lower()
    return any(flag in lowered for flag in _RED_FLAGS)


def _symptom(message: Message) -> str | None:
    code = understood(message).body.get("symptom_code")
    if isinstance(code, str) and code in _PROTOCOLS:
        return code
    lowered = message.text.lower()
    for name, phrases in _KEYWORDS:
        if any(phrase in lowered for phrase in phrases):
            return name
    return None


def _our_rx(chat: list[Message], agent: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for row in chat:
        if row.sender == agent and "prescribe" in words(row.text):
            found = fields_of(row.text)
    return found


async def _gp(city: City, message: Message, chat: list[Message]) -> None:
    agent = city.agent_id
    if message.sender == LEDGER:
        await _announce(city, message, chat, agent)
        return
    if not message.sender.startswith("citizen:"):
        return
    if _emergency(message):
        if not _we(chat, agent, "escalate.emergency", to=message.sender, word="escalate.emergency"):
            await _say(city, message.sender, "escalate.emergency reason=red_flag")
        return
    if _we(chat, agent, "escalate.emergency", to=message.sender, word="escalate.emergency"):
        # Red flag already escalated on this need: the GP stops, nothing else is sent.
        return
    ours = _our_rx(chat, agent)
    if ours.get("prescription_number"):
        # Already prescribed on this need: never prescribe twice, never leave the citizen
        # unanswered. Repeat the prescription tokens with a short reminder.
        procedure = ours.get("procedure", "")
        priority = ours.get("priority", "")
        await _say(
            city,
            message.sender,
            f"prescription prescription_number={ours['prescription_number']} "
            f"procedure={procedure} priority={priority} "
            + _rx_note(procedure, priority, english=_english(message.text), text=message.text, reminder=True),
        )
        return
    code = _symptom(message)
    if code is None:
        code = await _symptom_llm(city, message.text)
        if code == "RED_FLAG":
            if not _we(chat, agent, "escalate.emergency", to=message.sender, word="escalate.emergency"):
                await _say(city, message.sender, "escalate.emergency reason=red_flag")
            return
    if code is None:
        # Never leave a citizen without an answer: ask once which protocol row applies.
        clarify = _CLARIFY_EN if _english(message.text) else _CLARIFY_IT
        if not _exact(chat, agent, clarify):
            await _say(city, message.sender, clarify)
        return
    procedure, priority = _PROTOCOLS[code]
    number = _rx(message.need_id, message.msg_id)
    token = f"prescription_number={number}"
    # The citizen's reply window closes before a ledger round trip comes back, so the
    # prescription is said in this same turn. The ledger write is what the judge reads.
    if not _we(chat, agent, token, to=LEDGER, word="prescribe"):
        if not await _say(
            city,
            LEDGER,
            (
                f"prescribe patient={message.sender} procedure={procedure} priority={priority} "
                f"symptom_code={code} {token}"
            ),
        ):
            return
    if _we(chat, agent, token, to=message.sender, word="prescription"):
        return
    await _say(
        city,
        message.sender,
        f"prescription {token} procedure={procedure} priority={priority} "
        + _rx_note(procedure, priority, english=_english(message.text), text=message.text),
    )


def _english(text: str) -> bool:
    return len(words(text) & _ENGLISH) >= 2


_WORRIED = ("ansia", "ansios", "preoccup", "paura", "spaventat", "worried", "anxious", "scared", "afraid")


def _rx_note(
    procedure: str, priority: str, *, english: bool, text: str = "", reminder: bool = False
) -> str:
    """One polite line after the tokens. Citizens read the tokens; this is for the person."""

    index = 1 if english else 0
    label = _PROCEDURE_LABEL.get(procedure, (procedure, procedure))[index]
    when = _PRIORITY_LABEL.get(priority, (priority, priority))[index]
    worried = any(word in text.lower() for word in _WORRIED)
    if english:
        lead = "I understand your concern. " if worried else ""
        state = "is still valid" if reminder else "is ready"
        note = f"- {lead}Your prescription {state}: {label}, {when}. Use this number to book at the booking centre."
        if procedure == "VIS-FIS":
            note += " The hospital has wheelchair access."
        return note
    lead = "Capisco la sua preoccupazione. " if worried else ""
    state = "è già pronta ed è valida" if reminder else "è pronta"
    note = f"- {lead}La ricetta {state}: {label}, {when}. Con questo numero può prenotare al centro prenotazioni."
    if procedure == "VIS-FIS":
        note += " L'ospedale è accessibile in carrozzina."
    return note


async def _symptom_llm(city: City, text: str) -> str | None:
    """Map free text the keyword table missed to a protocol row, through the City LLM Gateway."""

    if not text.strip():
        return None
    try:
        reply = await asyncio.wait_for(
            city.llm(
                [
                    {"role": "system", "content": _ROUTER_PROMPT},
                    {"role": "user", "content": text[:1000]},
                ]
            ),
            _LLM_TIMEOUT,
        )
    except (CapError, asyncio.TimeoutError):
        return None
    content = str(reply.get("content") or "").upper()
    for code in _LLM_CODES:
        if code in content:
            return code
    return None


async def _announce(city: City, message: Message, chat: list[Message], agent: str) -> None:
    head = first_word(message.text)
    if head == "refused" and "DUPLICATE" not in message.text:
        return
    if head not in {"prescribed", "refused"}:
        return
    fields = fields_of(message.text)
    ours = _our_rx(chat, agent)
    number = fields.get("prescription_number") or ours.get("prescription_number")
    citizen = fields.get("patient") or _citizen(chat)
    if not number or not citizen:
        return
    if _we(chat, agent, f"prescription_number={number}", to=citizen, word="prescription"):
        return
    parts = ["prescription", f"prescription_number={number}"]
    procedure = fields.get("procedure") or ours.get("procedure")
    priority = fields.get("priority") or ours.get("priority")
    if procedure:
        parts.append(f"procedure={procedure}")
    if priority:
        parts.append(f"priority={priority}")
    await _say(city, citizen, " ".join(parts))


