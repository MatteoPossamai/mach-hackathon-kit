# Part of gp/agent/agent.py: loaded into its namespace, in order.
from __future__ import annotations

# --- medico di base ----------------------------------------------------------------------------


def _emergency(message: Message) -> bool:
    if understood(message).body.get("is_emergency") is True:
        return True
    lowered = message.text.lower()
    # Allow ordinary wording variations without assuming all mentions of
    # chest or leg strength are emergencies. Check a short preceding window
    # for explicit negation ("non ho dolore al petto").
    patterns = [re.escape(flag) for flag in _RED_FLAGS]
    patterns.extend((r"dolore\s+(?:al|nel|sul)\s+torace",
                     r"dolore\s+(?:al|nel|sul)\s+petto"))
    for match in re.finditer("|".join(patterns), lowered):
        prefix = lowered[max(0, match.start() - 32):match.start()]
        if re.search(r"\b(?:non\s+(?:ho|avverto|sento)|nessun[ao]?|senza|no|without)\s+(?:un[ao]?\s+|alcun[ao]?\s+|forte\s+)?$", prefix):
            continue
        return True
    return False


def _escalated(chat: list[Message], agent: str) -> bool:
    # A late Ledger reply can arrive on the same need after an escalation.
    return any(row.sender == agent and "escalate.emergency" in words(row.text) for row in chat)


async def _escalate(city: City, message: Message, chat: list[Message]) -> None:
    if not _escalated(chat, city.agent_id):
        await _say(city, message.sender,
                   "escalate.emergency reason=red_flag Capisco la sua preoccupazione. "
                   "Chiami subito il 112 per ricevere assistenza.")


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
        if row.sender == agent and row.recipient == LEDGER and first_word(row.text) == "prescribe":
            found = fields_of(row.text)
    return found


async def _gp(city: City, message: Message, chat: list[Message]) -> None:
    agent = city.agent_id
    if _escalated(chat, agent):
        return
    if message.sender == LEDGER:
        if first_word(message.text) == "prescription":
            original = fields_of(message.text).get("prescription_number", "")
            request = next((row for row in reversed(chat) if row.sender.startswith("citizen:")
                            and fields_of(row.text).get("prescription_number") == original), None)
            if request and original and not _our_rx(chat, agent):
                await _renew(city, request, chat, original)
            return
        await _announce(city, message, chat, agent)
        return
    if not message.sender.startswith("citizen:"):
        return
    if _emergency(message):
        await _escalate(city, message, chat)
        return
    ours = _our_rx(chat, agent)
    if ours.get("prescription_number"):
        # Sending a Ledger request is not proof that it accepted the write.
        confirmed = _latest(chat, "prescribed", f"prescription_number={ours['prescription_number']}")
        if confirmed:
            await _announce(city, confirmed, chat, agent, repeat=True)
        else:
            await _say(city, message.sender, "Sto verificando la ricetta con il registro; le risponderò appena confermata.")
        return
    old_number = understood(message).body.get("prescription_number")
    if isinstance(old_number, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", old_number):
        await _renew(city, message, chat, old_number)
        return
    # A routine symptom can coexist with a less familiar red-flag phrase.
    # Ask the classifier about potentially relevant context before routing a
    # routine keyword, so "palpitazioni ... dolore nel torace" cannot be
    # silently reduced to palpitations. Known flags never wait for the model.
    context = message.text.lower()
    ambiguous = any(word in context for word in ("petto", "torac", "chest", "gambe", "legs"))
    routed = await _symptom_llm(city, message.text) if ambiguous else None
    if routed == "RED_FLAG":
        await _escalate(city, message, chat)
        return
    if ambiguous and routed is None:
        await _say(city, message.sender, _CLARIFY_EN if _english(message.text) else _CLARIFY_IT)
        return
    code = _symptom(message) or routed
    if code is None:
        code = None if ambiguous else await _symptom_llm(city, message.text)
        if code == "RED_FLAG":
            await _escalate(city, message, chat)
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
    if not _we(chat, agent, token, to=LEDGER, word="prescribe"):
        if not await _say(
            city,
            LEDGER,
            (
                f"prescribe patient={message.sender} procedure={procedure} priority={priority} "
                f"symptom_code={code} {token}"
            ),
        ):
            await _say(city, message.sender, "Non riesco a contattare il registro per la ricetta. Riprovi tra poco.")
            return
    await _finish_prescription(city, message, chat, number)


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
        state = "was issued" if reminder else "is ready"
        note = f"- {lead}Your prescription {state}: {label}, {when}. Use this number to book at the booking centre."
        return note
    lead = "Capisco la sua preoccupazione. " if worried else ""
    state = "è stata emessa" if reminder else "è pronta"
    note = f"- {lead}La ricetta {state}: {label}, {when}. Con questo numero può prenotare al centro prenotazioni."
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
    content = str(reply.get("content") or "").strip().upper()
    return content if content in _LLM_CODES else None


async def _finish_prescription(city: City, message: Message, chat: list[Message], number: str) -> None:
    """A Relay acceptance is not a Ledger write; wait for the authoritative result."""
    result = await _await_line(city, message.need_id, chat, "prescribed",
                               f"prescription_number={number}", op="prescribe")
    if result is None:
        await _say(city, message.sender, "Sto verificando la ricetta con il registro; le risponderò appena confermata.")
        return
    reply, fresh = result
    if first_word(reply.text) == "refused":
        await _say(city, message.sender, "Il registro non ha confermato la ricetta. Ci contatti per assistenza.")
        return
    await _announce(city, reply, fresh, city.agent_id)


async def _renew(city: City, message: Message, chat: list[Message], original: str) -> None:
    """Renew only after the Ledger identifies the original prescription and owner."""
    token = f"prescription_number={original}"
    reply = _latest(chat, "prescription", token)
    if reply is None:
        if not await _say(city, LEDGER, f"prescription {token}"):
            await _say(city, message.sender, "Non riesco a verificare la ricetta per il rinnovo. Riprovi tra poco.")
            return
        found = await _await_line(city, message.need_id, chat, "prescription", token, op="prescription")
        if found is None:
            await _say(city, message.sender, "Sto verificando la ricetta per il rinnovo; le risponderò appena possibile.")
            return
        reply, chat = found
    data = fields_of(reply.text)
    patient = data.get("patient") or data.get("citizen")
    procedure, priority = data.get("procedure", ""), data.get("priority", "")
    if (first_word(reply.text) != "prescription" or patient != message.sender
            or not procedure or priority not in {"U", "B", "D", "P"}):
        await _say(city, message.sender, "Non riesco a verificare la ricetta per il rinnovo. Ci contatti per assistenza.")
        return
    number = _rx("RENEW-" + (message.need_id or message.msg_id), message.msg_id)
    if number == original:
        number = _rx("RENEW-" + (message.need_id or message.msg_id), message.msg_id)
    if number == original:
        await _say(city, message.sender, "Non riesco a creare un nuovo numero di ricetta. Ci contatti per assistenza.")
        return
    if not await _say(city, LEDGER, f"prescribe patient={message.sender} procedure={procedure} priority={priority} prescription_number={number}"):
        await _say(city, message.sender, "Non riesco a contattare il registro per il rinnovo. Riprovi tra poco.")
        return
    await _finish_prescription(city, message, chat, number)


async def _announce(city: City, message: Message, chat: list[Message], agent: str, *, repeat: bool = False) -> None:
    head = first_word(message.text)
    if message.sender != LEDGER:
        return
    ours = _our_rx(chat, agent)
    if head == "refused" and "prescribe" in words(message.text) and ours.get("patient"):
        await _say(city, ours["patient"], "Il registro non ha confermato la ricetta. Ci contatti per assistenza.")
        return
    if head != "prescribed":
        return
    fields = fields_of(message.text)
    number = fields.get("prescription_number", "")
    citizen = ours.get("patient", "")
    if not number or number != ours.get("prescription_number") or not citizen:
        return
    if fields.get("patient", citizen) != citizen:
        return
    if any(fields.get(key) and fields[key] != ours.get(key) for key in ("procedure", "priority")):
        return
    if not repeat and _we(chat, agent, f"prescription_number={number}", to=citizen, word="prescription"):
        return
    parts = ["prescription", f"prescription_number={number}"]
    procedure = fields.get("procedure") or ours.get("procedure")
    priority = fields.get("priority") or ours.get("priority")
    if procedure:
        parts.append(f"procedure={procedure}")
    if priority:
        parts.append(f"priority={priority}")
    citizen_text = next((row.text for row in reversed(chat) if row.sender == citizen), "")
    parts.append(_rx_note(procedure or "", priority or "", english=_english(citizen_text), text=citizen_text))
    await _say(city, citizen, " ".join(parts))


