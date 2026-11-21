"""Booking confirmation, Ledger hold/commit, and receipt flow for the specialist."""
from __future__ import annotations


def configure(agent_globals: dict[str, object]) -> None:
    globals().update(agent_globals)


def _our_offer(chat: list[Message], agent: str, offer_id: str) -> dict[str, str]:
    token = f"offer_id={offer_id}"
    for row in chat:
        if row.sender != agent or "offer" not in words(row.text):
            continue
        for line in row.text.splitlines():
            if token in line.split():
                return fields_of(line)
    return {}


def _booking_context(
    chat: list[Message], agent: str, slot_id: str
) -> tuple[str, str, str | None, str, str]:
    """Return ``(requester, consent_ref, prescription_number, offer_id, cross_asst_consent_ref)``."""
    token = f"slot_id={slot_id}"
    requester = ""
    consent = ""
    cross_consent = ""
    number: str | None = None
    offer_id = ""
    for row in chat:
        fields = msg_fields(row)
        row_offer = _safe_token(fields.get("offer_id"))
        if token in row.text.split() or (
            row_offer and _our_offer(chat, agent, row_offer).get("slot_id") == slot_id
        ):
            if "confirm" in words(row.text) and row.sender not in (LEDGER, agent):
                requester = row.sender
                consent = _safe_token(fields.get("consent_ref")) or consent
                cross_consent = (
                    _safe_token(fields.get("cross_asst_consent_ref") or fields.get("cross_asst_consent"))
                    or cross_consent
                )
                number = _safe_token(fields.get("prescription_number")) or number
                offer_id = row_offer or offer_id
            elif row.sender == agent and row.text.strip().startswith("hold "):
                consent = _safe_token(fields.get("consent_ref")) or consent
                cross_consent = (
                    _safe_token(fields.get("cross_asst_consent_ref")) or cross_consent
                )
                number = _safe_token(fields.get("prescription_number")) or number
                requester = requester or _safe_token(fields.get("citizen"))
            elif row.sender == agent and "offer" in words(row.text):
                offer_id = row_offer or offer_id
    return requester, consent, number, offer_id, cross_consent


def _latest_hold_info(chat: list[Message], agent: str) -> tuple[str, str, str | None, str]:
    slot = ""
    consent = ""
    cross_consent = ""
    number: str | None = None
    for row in chat:
        if row.sender != agent or row.recipient != LEDGER:
            continue
        if not row.text.strip().startswith("hold "):
            continue
        fields = fields_of(row.text)
        slot = _safe_token(fields.get("slot_id")) or slot
        consent = _safe_token(fields.get("consent_ref")) or consent
        cross_consent = _safe_token(fields.get("cross_asst_consent_ref")) or cross_consent
        number = _safe_token(fields.get("prescription_number")) or number
    return slot, consent, number, cross_consent


def _hold_slot(chat: list[Message], agent: str, hold_id: str) -> str:
    token = f"hold_id={hold_id}"
    for row in chat:
        if token in row.text.split() and row.sender in (agent, LEDGER):
            slot = _safe_token(fields_of(row.text).get("slot_id"))
            if slot:
                return slot
    return ""


def _our_commit(chat: list[Message], agent: str, hold_id: str) -> bool:
    token = f"hold_id={hold_id}"
    return any(
        row.sender == agent
        and row.recipient == LEDGER
        and row.text.strip().startswith("commit ")
        and token in row.text.split()
        for row in chat
    )


async def _on_confirm(city: City, message: Message, chat: list[Message]) -> None:
    agent = city.agent_id
    fields = msg_fields(message)
    offer_id = _safe_token(fields.get("offer_id"))
    offer = _our_offer(chat, agent, offer_id) if offer_id else {}
    slot = _safe_token(fields.get("slot_id") or offer.get("slot_id"))
    consent = _safe_token(fields.get("consent_ref"))
    cross_consent = _safe_token(
        fields.get("cross_asst_consent_ref") or fields.get("cross_asst_consent")
    )
    citizen = _safe_token(
        fields.get("citizen")
        or (message.sender if message.sender.startswith("citizen:") else "")
        or _citizen(chat)
    )
    number = _safe_token(fields.get("prescription_number") or offer.get("prescription_number"))

    if not slot:
        parts = ["cancel"]
        if offer_id:
            parts.append(f"offer_id={offer_id}")
        parts.append("reason=missing_slot_id")
        parts.append("Manca slot_id per completare la prenotazione.")
        await _say(city, message.sender, " ".join(parts), allowed_citizen=citizen or None)
        return

    # Idempotency on duplicate confirm: return existing receipt without double-holding.
    existing_commit = _latest_ledger(chat, "committed")
    if existing_commit is not None:
        c_fields = fields_of(existing_commit.text)
        bk = _safe_token(c_fields.get("booking_id"))
        ref = _safe_token(c_fields.get("ledger_ref"))
        c_slot = _safe_token(c_fields.get("slot_id")) or _hold_slot(
            chat, agent, _safe_token(c_fields.get("hold_id")) or bk
        )
        if bk and ref and (not c_slot or c_slot == slot):
            if not _we(chat, agent, f"booking_id={bk}", to=message.sender, word="booking.receipt"):
                await _say(
                    city,
                    message.sender,
                    _receipt_text(bk, ref, chat=chat, to_citizen=message.sender.startswith("citizen:")),
                    allowed_citizen=citizen or None,
                )
            return

    if not _we(chat, agent, f"slot_id={slot}", to=LEDGER, word="hold"):
        parts = ["hold", f"slot_id={slot}"]
        if citizen:
            parts.append(f"citizen={citizen}")
        if number:
            parts.append(f"prescription_number={number}")
        if consent:
            parts.append(f"consent_ref={consent}")
        if cross_consent:
            parts.append(f"cross_asst_consent_ref={cross_consent}")
        if not await _say(city, LEDGER, " ".join(parts)):
            return

    found = await _await_line(city, message.need_id, chat, "held", f"slot_id={slot}", op="hold")
    if found is None:
        _LOG.info("specialist.held.missing", extra={"need_id": (message.need_id or "")[:48]})
        return
    reply, fresh = found
    if first_word(reply.text) == "refused":
        await _on_booking_refused(
            city, reply, fresh, slot=slot, offer_id=offer_id, requester=message.sender
        )
        return
    if first_word(reply.text) != "held":
        return

    await _on_held(city, reply, fresh)
    hold_id = _safe_token(fields_of(reply.text).get("hold_id"))
    if not hold_id:
        return

    done = await _await_line(
        city, message.need_id, fresh, "committed", f"booking_id={hold_id}", op="commit"
    )
    if done is None:
        _LOG.info("specialist.commit.missing", extra={"need_id": (message.need_id or "")[:48]})
        return
    c_reply, c_fresh = done
    if first_word(c_reply.text) == "committed":
        await _on_committed(city, c_reply, c_fresh)
    elif first_word(c_reply.text) == "refused":
        # Level 6 recovery: if the hold expired before commit, retry one fresh hold + commit.
        if _extract_refusal_reason(c_reply.text) == "HOLD_EXPIRED":
            retry_parts = ["hold", f"slot_id={slot}"]
            if citizen:
                retry_parts.append(f"citizen={citizen}")
            if number:
                retry_parts.append(f"prescription_number={number}")
            if consent:
                retry_parts.append(f"consent_ref={consent}")
            if cross_consent:
                retry_parts.append(f"cross_asst_consent_ref={cross_consent}")
            if await _say(city, LEDGER, " ".join(retry_parts)):
                re_held = await _await_line(
                    city, message.need_id, c_fresh, "held", f"slot_id={slot}", op="hold"
                )
                if re_held is not None and first_word(re_held[0].text) == "held":
                    new_hid = _safe_token(fields_of(re_held[0].text).get("hold_id"))
                    if new_hid and new_hid != hold_id:
                        await _on_held(city, re_held[0], re_held[1])
                        re_done = await _await_line(
                            city,
                            message.need_id,
                            re_held[1],
                            "committed",
                            f"booking_id={new_hid}",
                            op="commit",
                        )
                        if re_done is not None and first_word(re_done[0].text) == "committed":
                            await _on_committed(city, re_done[0], re_done[1])
                            return
        await _on_booking_refused(
            city, c_reply, c_fresh, slot=slot, offer_id=offer_id, requester=message.sender
        )


def _receipt_text(
    booking_id: str,
    ledger_ref: str,
    *,
    chat: list[Message],
    to_citizen: bool,
    slot_id: str = "",
    offer_id: str = "",
) -> str:
    parts = ["booking.receipt", f"booking_id={booking_id}", f"ledger_ref={ledger_ref}"]
    if not to_citizen:
        if slot_id:
            parts.append(f"slot_id={slot_id}")
        if offer_id:
            parts.append(f"offer_id={offer_id}")
        parts.append("Prenotazione confermata per la visita specialistica cardiologica.")
        return " ".join(parts)
    english = _is_english(chat)
    lead = _empathy_lead(english=english, anxious=_is_anxious(chat))
    note = (
        f"{lead}Your cardiology appointment is confirmed. Please keep this receipt for arrival."
        if english
        else f"{lead}La sua prenotazione cardiologica è confermata. Conservi questa ricevuta per il check-in."
    )
    parts.append(note)
    return " ".join(parts)


async def _on_held(city: City, message: Message, chat: list[Message]) -> None:
    fields = fields_of(message.text)
    hold_id = _safe_token(fields.get("hold_id"))
    slot = _safe_token(fields.get("slot_id"))
    if not hold_id or not slot:
        return
    if _our_commit(chat, city.agent_id, hold_id):
        return

    _, consent, number, _, cross_consent = _booking_context(chat, city.agent_id, slot)
    if not consent:
        _, held_consent, held_number, held_cross = _latest_hold_info(chat, city.agent_id)
        consent = held_consent
        number = number or held_number
        cross_consent = cross_consent or held_cross
    need_ref = _safe_token(fields.get("need_id") or message.need_id)
    if not consent and need_ref.startswith("N-"):
        consent = f"cons-{need_ref}-booking"
    if not consent:
        _LOG.info("specialist.held.noconsent", extra={"need_id": need_ref[:48]})
        return

    fence = _safe_token(fields.get("fence")) or "1"
    parts = [
        "commit",
        f"hold_id={hold_id}",
        f"consent_ref={consent}",
        f"fence={fence}",
    ]
    if number:
        parts.append(f"prescription_number={number}")
    if cross_consent:
        parts.append(f"cross_asst_consent_ref={cross_consent}")
    await _say(city, LEDGER, " ".join(parts))


async def _on_committed(city: City, message: Message, chat: list[Message]) -> None:
    fields = fields_of(message.text)
    booking_id = _safe_token(fields.get("booking_id"))
    ledger_ref = _safe_token(fields.get("ledger_ref"))
    if not booking_id or not ledger_ref:
        return

    slot = _safe_token(fields.get("slot_id")) or _hold_slot(
        chat, city.agent_id, _safe_token(fields.get("hold_id")) or booking_id
    )
    requester, _, _, offer_id, _ = (
        _booking_context(chat, city.agent_id, slot) if slot else ("", "", None, "", "")
    )
    target = requester or _citizen(chat)
    if not target:
        return
    token = f"booking_id={booking_id}"
    if _we(chat, city.agent_id, token, to=target, word="booking.receipt"):
        return
    await _say(
        city,
        target,
        _receipt_text(
            booking_id,
            ledger_ref,
            chat=chat,
            to_citizen=target.startswith("citizen:"),
            slot_id=slot,
            offer_id=offer_id,
        ),
        allowed_citizen=_citizen(chat),
    )


async def _on_booking_refused(
    city: City,
    message: Message,
    chat: list[Message],
    *,
    slot: str = "",
    offer_id: str = "",
    requester: str = "",
) -> None:
    agent = city.agent_id
    if not slot:
        slot, _, _, _ = _latest_hold_info(chat, agent)
    if slot:
        ctx_req, _, _, ctx_offer, _ = _booking_context(chat, agent, slot)
        requester = requester or ctx_req
        offer_id = offer_id or ctx_offer
    target = requester or _citizen(chat)
    if not target:
        return

    reason = _extract_refusal_reason(message.text)
    dedup_tok = f"offer_id={offer_id}" if offer_id else f"slot_id={slot}"
    if dedup_tok and _we(chat, agent, dedup_tok, to=target, word="cancel"):
        return
    parts = ["cancel"]
    if offer_id:
        parts.append(f"offer_id={offer_id}")
    if slot:
        parts.append(f"slot_id={slot}")
    parts.append(f"reason={reason}")
    if reason == "prescription_expired" and target.startswith("citizen:"):
        parts.append(
            "Your prescription has expired under the current rule: please ask your GP to renew it "
            "(send them your prescription number), then the booking centre will book again at once."
            if _is_english(chat) else
            "La ricetta risulta scaduta con la regola in vigore: chieda al medico di base il rinnovo "
            "(gli mandi il numero della ricetta), poi il centro prenotazioni prenota subito."
        )
    elif target.startswith("citizen:") and _is_english(chat):
        parts.append(
            f"We could not confirm the requested slot ({reason}). "
            "Please select another available time or check the requirements."
        )
    else:
        parts.append(
            f"Non è stato possibile confermare lo slot richiesto ({reason}). "
            "È necessario selezionare un'altra disponibilità o verificare i requisiti."
        )
    await _say(city, target, " ".join(parts), allowed_citizen=_citizen(chat))


