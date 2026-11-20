# Part of hospital/agent/agent.py: loaded into its namespace, in order.
from __future__ import annotations

# --- facility ----------------------------------------------------------------------------------


def _facility_turn(message: Message) -> bool:
    head = first_word(message.text)
    kind = words(message.text)
    if message.sender == LEDGER and head in {"held", "committed", "checked_in", "reported"}:
        return True
    if message.sender == LEDGER and head == "refused" and "hold" in kind:
        return True
    if "checkin" in kind and message.sender.startswith("citizen:"):
        return True
    return "confirm" in kind and (
        message.sender.startswith("agent:") or message.sender.startswith("citizen:")
    )


async def _family(city: City, message: Message, chat: list[Message]) -> None:
    if _facility_turn(message):
        await _facility(city, message, chat, reports=False, hold_self=True)
        return
    await _desk(city, message, chat, hold_self=True, fixed="CONS-GIN")


async def _facility(
    city: City,
    message: Message,
    chat: list[Message],
    *,
    reports: bool,
    hold_self: bool,
) -> None:
    head = first_word(message.text)
    kind = words(message.text)
    if message.sender == LEDGER and head == "held":
        await _on_held(city, message, chat)
    elif message.sender == LEDGER and head == "committed":
        await _on_committed(city, message, chat)
    elif message.sender == LEDGER and head == "refused" and "hold" in kind:
        await _on_refused(city, message, chat, hold_self=hold_self)
    elif message.sender == LEDGER and head == "checked_in":
        await _on_checked_in(city, message, chat, reports=reports)
    elif message.sender == LEDGER and head == "reported":
        await _on_reported(city, message, chat)
    elif "checkin" in kind and message.sender.startswith("citizen:"):
        await _on_checkin(city, message, chat, reports=reports)
    elif "confirm" in kind:
        await _on_confirm(city, message, chat, hold_self=hold_self)
    elif head == "need.request" and message.sender.startswith("agent:"):
        await _partner_request(city, message, chat)


def _all_slot_lines(chat: list[Message]) -> list[str]:
    lines: list[str] = []
    for row in chat:
        if row.sender == LEDGER and first_word(row.text) == "slots":
            lines.extend(line for line in row.text.splitlines()[1:] if "slot_id=" in line)
    return lines


async def _hospital_slot_history(city: City, message: Message, chat: list[Message], slot: str) -> list[Message]:
    """Read our facility's slots from the Ledger, not from the requesting agent."""
    def found(rows: list[Message]) -> bool:
        return any(fields_of(line).get("slot_id") == slot and fields_of(line).get("facility") == city.agent_id
                   for line in _all_slot_lines(rows))
    if found(chat):
        return chat
    for page in range(1, _MAX_PAGE + 1):
        if not await _say(city, LEDGER, f"world procedure=VIS-FIS page={page}"):
            break
        chat = await _until(city, message.need_id, chat, lambda rows: found(rows) or any(
            row.sender == LEDGER and first_word(row.text) == "slots" and _page(row.text)[0] == page
            for row in rows))
        if found(chat):
            break
        result = next((row for row in reversed(chat) if row.sender == LEDGER
                       and first_word(row.text) == "slots" and _page(row.text)[0] == page), None)
        if result is None or page >= _page(result.text)[1]:
            break
    return chat


async def _partner_request(city: City, message: Message, chat: list[Message]) -> None:
    """Another booking centre asks for our free slots (shortage on the ring).

    The request is anonymous and so is the answer: only our own free slots, no citizen data.
    The booking itself follows through ``confirm`` (consent included), like any local booking.
    """

    agent = city.agent_id
    peer = message.sender
    data = fields_of(message.text)
    body = message.body if message.type != "chat" and isinstance(message.body, dict) else {}
    procedure = data.get("procedure") or data.get("service_type") or str(
        body.get("procedure") or body.get("service_type") or ""
    )
    priority = data.get("priority") or str(body.get("priority") or "") or None
    request_id = data.get("request_id") or str(body.get("request_id") or "")
    if request_id and not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", request_id):
        return
    if priority and priority not in {"U", "B", "D", "P"}:
        return
    if request_id and any(row.sender == agent and row.recipient == peer
                          and fields_of(row.text).get("request_id") == request_id for row in chat):
        return
    if not re.fullmatch(r"[A-Z]{2,5}-[A-Z]{2,5}", procedure or ""):
        return
    if not _has_agenda(chat) and not _exact(chat, agent, "agenda"):
        await _say(city, LEDGER, "agenda")
    if not _asked(chat, agent, procedure, 1):
        await _say(city, LEDGER, f"world procedure={procedure} page=1")
    chat = await _until(
        city, message.need_id, chat,
        lambda rows: _has_agenda(rows) and _latest_slots(rows) is not None,
    )
    agenda = _ledger(chat, "agenda")
    now = _when("" if agenda is None else agenda.get("agenda_now", ""))
    world = _latest_slots(chat)
    if now is None or world is None:
        await _say(city, peer, f"unavailable procedure={procedure}{' request_id=' + request_id if request_id else ''} Registro temporaneamente non disponibile.")
        return
    page, pages = _page(world)
    while page < min(pages, _MAX_PAGE):
        page += 1
        if not _asked(chat, agent, procedure, page):
            await _say(city, LEDGER, f"world procedure={procedure} page={page}")
        chat = await _until(
            city, message.need_id, chat,
            lambda rows, p=page: any(
                row.sender == LEDGER and first_word(row.text) == "slots"
                and _page(row.text)[0] == p for row in rows
            ),
        )
    chosen = fitting(
        _all_slot_lines(chat), now=now, priority=priority,
        lang=data.get("lang", "it"), facility=agent, procedure=procedure,
    )[:3]
    tail = f" request_id={request_id}" if request_id else ""
    if not chosen:
        await _say(
            city, peer,
            f"unavailable procedure={procedure}{tail} Al momento non abbiamo posti "
            "compatibili con questa richiesta. Riprovate più tardi.",
        )
        return
    for slot in chosen:
        await _say(city, peer, _offer_line(slot, None, priority) + tail)


def _en(chat: list[Message]) -> bool:
    return _lang(chat) == "en"


_RECEIPT_IT = (
    " Prenotazione confermata. Al suo arrivo in ospedale faccia il check-in con questo "
    "numero di prenotazione; porti tessera sanitaria e ricetta."
)
_RECEIPT_EN = (
    " Booking confirmed. When you arrive at the hospital, check in with this booking "
    "number; bring your health card and prescription."
)
_CHECKIN_IT = " Check-in registrato: la attendiamo in reparto, verrà chiamato a breve."
_CHECKIN_EN = " Check-in done: please wait in the ward, you will be called shortly."


def _booking_of(chat: list[Message], agent: str, slot_id: str) -> tuple[str, str, str | None]:
    """Who asked us to book this slot, the consent, and the prescription, if we have them."""

    token = f"slot_id={slot_id}"
    sender = ""
    consent = ""
    number: str | None = None
    for row in chat:
        if token not in row.text.split():
            continue
        fields = fields_of(row.text)
        if "confirm" in words(row.text) and row.sender not in (LEDGER, agent):
            sender = row.sender
            consent = fields.get("consent_ref", consent)
            number = fields.get("prescription_number") or number
        if row.sender == agent and "hold" in words(row.text):
            consent = fields.get("consent_ref", consent)
            number = fields.get("prescription_number") or number
            sender = sender or fields.get("citizen", "")
    return sender, consent, number


def _hold_slot(chat: list[Message], agent: str, hold_id: str) -> str:
    token = f"hold_id={hold_id}"
    for row in chat:
        if token in row.text.split() and (row.sender == agent or row.sender == LEDGER):
            slot = fields_of(row.text).get("slot_id", "")
            if slot:
                return slot
    return ""


def _our_commit(chat: list[Message], agent: str, hold_id: str) -> bool:
    """A commit we actually sent. The ledger's held line also contains the word commit."""

    token = f"hold_id={hold_id}"
    for row in chat:
        if row.sender != agent or row.recipient != LEDGER:
            continue
        if row.text.strip().startswith("commit ") and token in row.text.split():
            return True
    return False


def _latest_hold(chat: list[Message], agent: str) -> tuple[str, str | None]:
    consent = ""
    number: str | None = None
    for row in chat:
        if row.sender != agent or not row.text.strip().startswith("hold "):
            continue
        fields = fields_of(row.text)
        consent = fields.get("consent_ref", consent)
        number = fields.get("prescription_number") or number
    return consent, number


async def _finish_booking(
    city: City, message: Message, chat: list[Message], slot: str, *, hold_self: bool
) -> None:
    """Hold is in the mailbox. Read the held line and commit before this turn ends."""

    found = await _await_line(city, message.need_id, chat, "held", f"slot_id={slot}", op="hold")
    if found is None:
        _LOG.info("care.held.missing", extra={"need_id": (message.need_id or "")[:48]})
        return
    reply, fresh = found
    if first_word(reply.text) == "refused":
        await _on_refused(city, reply, fresh, hold_self=hold_self)
        return
    await _on_held(city, reply, fresh)
    hold_id = fields_of(reply.text).get("hold_id", "")
    if not hold_id:
        return
    # A booking ID and a hold ID are different identifiers. Match a new Ledger
    # response to the hold when the response includes it, never assume equality.
    known = {row.msg_id for row in fresh}
    def result(rows: list[Message]) -> Message | None:
        return next((row for row in reversed(rows) if row.msg_id not in known and row.sender == LEDGER
                     and ((first_word(row.text) == "committed" and fields_of(row.text).get("hold_id", hold_id) == hold_id)
                          or (first_word(row.text) == "refused" and "commit" in words(row.text)))), None)
    fresh = await _until(city, message.need_id, fresh, lambda rows: result(rows) is not None)
    reply = result(fresh)
    if reply is None:
        _LOG.info("care.commit.missing", extra={"need_id": (message.need_id or "")[:48]})
        return
    if first_word(reply.text) == "committed":
        await _on_committed(city, reply, fresh)
        return
    if first_word(reply.text) == "refused":
        await _on_refused(city, reply, fresh, hold_self=hold_self)


async def _on_held(city: City, message: Message, chat: list[Message]) -> None:
    fields = fields_of(message.text)
    hold_id = fields.get("hold_id", "")
    slot = fields.get("slot_id", "")
    if not hold_id or not slot:
        _LOG.info("care.held.skip", extra={"head": first_word(message.text)[:40]})
        return
    if _our_commit(chat, city.agent_id, hold_id):
        return
    hold_index = next((index for index in range(len(chat) - 1, -1, -1)
                       if chat[index].sender == city.agent_id and chat[index].recipient == LEDGER
                       and first_word(chat[index].text) == "hold"
                       and fields_of(chat[index].text).get("slot_id") == slot), None)
    request = fields_of(chat[hold_index].text) if hold_index is not None else None
    confirm = next((fields_of(row.text) for row in reversed(chat[:hold_index])
                    if row.sender.startswith("agent:") and row.recipient == city.agent_id
                    and "confirm" in words(row.text) and fields_of(row.text).get("slot_id") == slot), None) if hold_index is not None else None
    consent = confirm.get("consent_ref", "") if request and confirm else ""
    number = request.get("prescription_number") if request else None
    if not consent:
        _LOG.info("care.held.noconsent", extra={"need_id": (message.need_id or "")[:48]})
        return
    parts = [
        "commit",
        f"hold_id={hold_id}",
        f"consent_ref={consent}",
    ]
    if fields.get("fence"):
        parts.append(f"fence={fields['fence']}")
    if number:
        parts.append(f"prescription_number={number}")
    await _say(city, LEDGER, " ".join(parts))


async def _on_committed(city: City, message: Message, chat: list[Message]) -> None:
    fields = fields_of(message.text)
    booking_id = fields.get("booking_id", "")
    ledger_ref = fields.get("ledger_ref", "")
    if not booking_id or not ledger_ref:
        return
    hold_id = fields.get("hold_id", "")
    if not hold_id and _our_commit(chat, city.agent_id, booking_id):
        hold_id = booking_id
    if not hold_id:
        commits = {fields_of(row.text).get("hold_id", "") for row in chat
                   if row.sender == city.agent_id and row.recipient == LEDGER and first_word(row.text) == "commit"}
        commits.discard("")
        if len(commits) == 1:
            hold_id = next(iter(commits))
    if not hold_id or not _our_commit(chat, city.agent_id, hold_id):
        return
    slot = _hold_slot(chat, city.agent_id, hold_id)
    if fields.get("slot_id") and fields["slot_id"] != slot:
        return
    hold_index = next((index for index in range(len(chat) - 1, -1, -1)
                       if chat[index].sender == city.agent_id and chat[index].recipient == LEDGER
                       and first_word(chat[index].text) == "hold"
                       and fields_of(chat[index].text).get("slot_id") == slot), None)
    target = next((row.sender for row in reversed(chat[:hold_index])
                   if row.sender.startswith("agent:") and row.recipient == city.agent_id
                   and "confirm" in words(row.text) and fields_of(row.text).get("slot_id") == slot), "") if hold_index is not None else ""
    if not target:
        return
    token = f"booking_id={booking_id}"
    if _we(chat, city.agent_id, token, to=target, word="booking.receipt"):
        return
    await _say(
        city, target,
        f"booking.receipt booking_id={booking_id} ledger_ref={ledger_ref}"
        + (_RECEIPT_EN if _en(chat) else _RECEIPT_IT),
    )


async def _on_refused(
    city: City, message: Message, chat: list[Message], *, hold_self: bool
) -> None:
    agent = city.agent_id
    slot = ""
    for row in chat:
        if row.sender == agent and row.recipient == LEDGER and "hold" in words(row.text):
            slot = fields_of(row.text).get("slot_id", slot)
    if not slot:
        return
    target, _, _ = _booking_of(chat, agent, slot)
    offer_id = ""
    for row in chat:
        if f"slot_id={slot}" in row.text.split():
            offer_id = fields_of(row.text).get("offer_id", offer_id)
    if (
        target
        and offer_id
        and not _we(chat, agent, f"offer_id={offer_id}", to=target, word="cancel")
    ):
        await _say(city, target, f"cancel offer_id={offer_id} reason=facility_cancelled")
    if hold_self:
        await _advance(city, message, chat, fixed="CONS-GIN", replacing=True)


async def _on_checkin(city: City, message: Message, chat: list[Message], *, reports: bool) -> None:
    booking_id = fields_of(message.text).get("booking_id", "")
    citizen = message.sender if message.sender.startswith("citizen:") else _citizen(chat)
    if not booking_id or not citizen:
        return
    token = f"booking_id={booking_id}"
    if not _we(chat, city.agent_id, token, to=LEDGER, word="checkin"):
        if not await _say(city, LEDGER, f"checkin booking_id={booking_id} citizen={citizen}"):
            return
    found = await _await_line(city, message.need_id, chat, "checked_in", token, op="checkin")
    if found is None:
        _LOG.info("care.checkin.missing", extra={"need_id": (message.need_id or "")[:48]})
        return
    reply, fresh = found
    if first_word(reply.text) == "checked_in":
        await _on_checked_in(city, reply, fresh, reports=reports)
    elif first_word(reply.text) == "refused" and not _we(
        fresh, city.agent_id, token, to=citizen, word="refused"
    ):
        # Never silent: the citizen learns why and what to do next.
        reason = fields_of(reply.text).get("reason", "")
        await _say(
            city, citizen,
            f"refused booking_id={booking_id}"
            + (f" reason={reason}" if reason else "")
            + (" I cannot register the check-in with this booking number. Please check the number "
               "on your receipt, or write to the booking centre: they will help you right away."
               if _en(chat) else
               " Non riesco a registrare il check-in con questo numero di prenotazione. "
               "Controlli il numero sulla ricevuta o scriva al centro prenotazioni: la aiuteranno subito."),
        )


async def _on_checked_in(
    city: City, message: Message, chat: list[Message], *, reports: bool
) -> None:
    data = fields_of(message.text)
    booking_id = data.get("booking_id", "")
    request = next((fields_of(row.text) for row in reversed(chat)
                    if row.sender == city.agent_id and row.recipient == LEDGER
                    and first_word(row.text) == "checkin"
                    and fields_of(row.text).get("booking_id") == booking_id), None)
    citizen = request.get("citizen", "") if request else ""
    if not booking_id:
        return
    token = f"booking_id={booking_id}"
    if reports and not _we(chat, city.agent_id, token, to=LEDGER, word="report"):
        await _say(city, LEDGER, f"report booking_id={booking_id} outcome_code=VISIT")
    if citizen and not _we(chat, city.agent_id, token, to=citizen, word="checkin"):
        incoming = next((fields_of(row.text) for row in reversed(chat)
                         if row.sender == citizen and "checkin" in words(row.text)
                         and fields_of(row.text).get("booking_id") == booking_id), {})
        ledger_ref = data.get("ledger_ref") or incoming.get("ledger_ref", "")
        await _say(
            city, citizen,
            f"checkin booking_id={booking_id}" + (f" ledger_ref={ledger_ref}" if ledger_ref else "")
            + (_CHECKIN_EN if _en(chat) else _CHECKIN_IT),
        )
    if not reports:
        return
    found = await _await_line(city, message.need_id, chat, "reported", token, op="report")
    if found is None:
        _LOG.info("care.report.missing", extra={"need_id": (message.need_id or "")[:48]})
        return
    reply, fresh = found
    if first_word(reply.text) == "reported":
        await _on_reported(city, reply, fresh)


async def _on_reported(city: City, message: Message, chat: list[Message]) -> None:
    booking_id = fields_of(message.text).get("booking_id", "")
    citizen = _citizen(chat)
    if not booking_id or not citizen:
        return
    token = f"booking_id={booking_id}"
    if _we(chat, city.agent_id, token, to=citizen, word="report"):
        return
    await _say(city, citizen, f"report booking_id={booking_id} outcome_code=VISIT")
