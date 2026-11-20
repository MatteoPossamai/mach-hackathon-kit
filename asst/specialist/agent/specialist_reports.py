"""Check-in, consent, report, and follow-up lifecycle for the cardiology specialist.

The agent module configures this module with its shared SDK and care-flow helpers
after loading. Keeping those helpers in the router preserves the SDK's standalone
module loading model while making the longer report lifecycle independently
maintainable.
"""
from __future__ import annotations


def configure(agent_globals: dict[str, object]) -> None:
    globals().update(agent_globals)


# The agent module provides shared state helpers through configure().

async def _on_checkin(city: City, message: Message, chat: list[Message]) -> None:
    agent = city.agent_id
    fields = msg_fields(message)
    booking_id = _safe_token(fields.get("booking_id"))
    if not booking_id:
        committed = _latest_ledger(chat, "committed")
        if committed is not None:
            booking_id = _safe_token(fields_of(committed.text).get("booking_id"))
    citizen = message.sender if message.sender.startswith("citizen:") else (_citizen(chat) or "")
    if not citizen:
        return

    english = _is_english(chat, message.text)
    lead = _empathy_lead(english=english, anxious=_is_anxious(chat, message.text))

    if not booking_id:
        prompt = (
            f"data.request purpose=checkin fields=booking_id "
            f"{lead}Welcome! To register your check-in, please share your booking code (booking_id)."
            if english
            else f"data.request purpose=checkin fields=booking_id "
            f"{lead}Benvenuto! Per registrare il check-in, può indicarmi il codice prenotazione (booking_id)?"
        )
        await _say(city, citizen, prompt, allowed_citizen=citizen)
        return

    if _booking_has_report(chat, booking_id):
        reported_row = _latest_ledger(chat, "reported", f"booking_id={booking_id}")
        if reported_row is not None:
            await _on_reported(city, reported_row, chat)
        return

    if _checked_in_booking_id(chat) == booking_id and _asked_report_consent(chat, agent, booking_id):
        remind = (
            f"consent purpose=report_sharing {lead}Your check-in is already recorded. "
            "May I share your visit summary (report_sharing)? If you prefer to keep it private, "
            "just let me know and I will file it unshared."
            if english
            else f"consent purpose=report_sharing {lead}Il suo check-in è già registrato. "
            "Mi conferma se posso condividere il suo referto (report_sharing)? "
            "Se preferisce tenerlo riservato, me lo dica pure e lo archivierò senza condivisione."
        )
        await _say(city, citizen, remind, allowed_citizen=citizen)
        return

    token = f"booking_id={booking_id}"
    if not _we(chat, agent, token, to=LEDGER, word="checkin"):
        if not await _say(city, LEDGER, f"checkin booking_id={booking_id} citizen={citizen}"):
            return

    found = await _await_line(city, message.need_id, chat, "checked_in", token, op="checkin")
    if found is None:
        _LOG.info("specialist.checkin.missing", extra={"need_id": (message.need_id or "")[:48]})
        return
    reply, fresh = found
    if first_word(reply.text) == "checked_in":
        await _on_checked_in(city, reply, fresh)
    elif first_word(reply.text) == "refused":
        if "DUPLICATE" in reply.text.upper():
            await _after_checked_in(city, booking_id, fresh)
        else:
            reason = _extract_refusal_reason(reply.text)
            err_msg = (
                f"{lead}I could not register your check-in for booking_id={booking_id} ({reason}). "
                "Please check your booking receipt or contact the booking centre."
                if english
                else f"{lead}Non è stato possibile registrare il check-in per booking_id={booking_id} "
                f"({reason}). Verifichi la ricevuta di prenotazione o contatti il centro prenotazioni."
            )
            await _say(city, citizen, err_msg, allowed_citizen=citizen)


async def _on_checked_in(city: City, message: Message, chat: list[Message]) -> None:
    booking_id = _safe_token(fields_of(message.text).get("booking_id"))
    if not booking_id:
        return
    await _after_checked_in(city, booking_id, chat)


async def _after_checked_in(city: City, booking_id: str, chat: list[Message]) -> None:
    agent = city.agent_id
    citizen = _citizen(chat)
    if not citizen:
        return

    english = _is_english(chat)
    lead = _empathy_lead(english=english, anxious=_is_anxious(chat))

    token = f"booking_id={booking_id}"
    if not _we(chat, agent, token, to=citizen, word="checkin"):
        msg = (
            f"checkin booking_id={booking_id} "
            f"{lead}Welcome! Your check-in for the cardiology visit has been registered."
            if english
            else f"checkin booking_id={booking_id} "
            f"{lead}Benvenuto! Il suo check-in per la visita cardiologica è stato registrato correttamente."
        )
        await _say(city, citizen, msg, allowed_citizen=citizen)

    # Prefetch the original prescription for THIS booking while waiting for report_sharing consent
    # so follow-up (CTRL-CARD) decision has zero extra round-trip latency when consent arrives.
    orig_rx = _booking_prescription_number(chat, booking_id)
    if orig_rx and _latest_ledger(chat, "prescription", f"prescription_number={orig_rx}") is None:
        if not _we(chat, agent, f"prescription_number={orig_rx}", to=LEDGER, word="prescription"):
            await _say(city, LEDGER, f"prescription prescription_number={orig_rx}")

    existing_consent = _report_consent_ref(chat, agent, booking_id)
    if existing_consent:
        await _file_report(city, booking_id, citizen, chat, consent_ref=existing_consent)
        return

    if not _asked_report_consent(chat, agent, booking_id):
        ask = (
            f"consent purpose=report_sharing {lead}Your visit is complete. May I share your "
            "visit summary on the health record (report_sharing)? If you prefer not to share it, "
            "please let me know and I will archive it privately."
            if english
            else f"consent purpose=report_sharing {lead}La visita è stata completata. Posso "
            "condividere il suo referto sul fascicolo sanitario (report_sharing)? Se preferisce "
            "non condividerlo, me lo indichi pure e lo archivierò in forma riservata."
        )
        await _say(city, citizen, ask, allowed_citizen=citizen)


async def _on_report_consent_reply(city: City, message: Message, chat: list[Message]) -> None:
    booking_id = _checked_in_booking_id(chat)
    citizen = message.sender if message.sender.startswith("citizen:") else (_citizen(chat) or "")
    if not booking_id or not citizen:
        return

    fields = msg_fields(message)
    raw_ref = _safe_token(fields.get("consent_ref"))
    consent_ref = "" if _consent_explicitly_declined(fields) else raw_ref
    await _file_report(city, booking_id, citizen, chat, consent_ref=consent_ref)


async def _file_report(
    city: City,
    booking_id: str,
    citizen: str,
    chat: list[Message],
    *,
    consent_ref: str,
) -> None:
    agent = city.agent_id
    need_id = city.inbound.need_id if city.inbound else None
    token = f"booking_id={booking_id}"

    if _we(chat, agent, token, to=LEDGER, word="report"):
        reported = _latest_ledger(chat, "reported", token)
        if reported is not None:
            await _on_reported(city, reported, chat)
        return

    # Inspect the prescription for THIS booking so CTRL-CARD follow-ups never loop.
    orig_rx = _booking_prescription_number(chat, booking_id)
    rx_row = (
        _latest_ledger(chat, "prescription", f"prescription_number={orig_rx}")
        if orig_rx
        else _latest_ledger(chat, "prescription")
    )
    rx_info = None if rx_row is None else fields_of(rx_row.text)
    if orig_rx and rx_info is None:
        if not _we(chat, agent, f"prescription_number={orig_rx}", to=LEDGER, word="prescription"):
            await _say(city, LEDGER, f"prescription prescription_number={orig_rx}")
        chat = await _until(
            city,
            need_id,
            chat,
            lambda rows: _latest_ledger(rows, "prescription", f"prescription_number={orig_rx}")
            is not None,
        )
        rx_row = _latest_ledger(chat, "prescription", f"prescription_number={orig_rx}")
        rx_info = None if rx_row is None else fields_of(rx_row.text)

    follow_up_rx = ""
    if _needs_follow_up(chat, rx_info):
        follow_up_rx = await _issue_follow_up_prescription(
            city, booking_id, citizen, need_id, chat
        )

    parts = ["report", f"booking_id={booking_id}", f"outcome_code={OUTCOME_CODE}"]
    if consent_ref:
        parts.append(f"consent_ref={consent_ref}")
    if follow_up_rx:
        parts.append(f"follow_up_prescription_number={follow_up_rx}")

    if not await _say(city, LEDGER, " ".join(parts)):
        return

    found = await _await_line(city, need_id, chat, "reported", token, op="report")
    if found is None:
        _LOG.info("specialist.report.missing", extra={"need_id": (need_id or "")[:48]})
        return
    reply, fresh = found
    if first_word(reply.text) == "reported":
        await _on_reported(city, reply, fresh)
        return
    if first_word(reply.text) == "refused" and consent_ref:
        fallback = ["report", f"booking_id={booking_id}", f"outcome_code={OUTCOME_CODE}"]
        if follow_up_rx:
            fallback.append(f"follow_up_prescription_number={follow_up_rx}")
        if await _say(city, LEDGER, " ".join(fallback)):
            retry = await _await_line(city, need_id, fresh, "reported", token, op="report")
            if retry is not None and first_word(retry[0].text) == "reported":
                await _on_reported(city, retry[0], retry[1])


async def _issue_follow_up_prescription(
    city: City,
    booking_id: str,
    citizen: str,
    need_id: str | None,
    chat: list[Message],
) -> str:
    agent = city.agent_id
    rx_num = _follow_up_rx_number(booking_id)
    rx_token = f"prescription_number={rx_num}"
    if not _we(chat, agent, rx_token, to=LEDGER, word="prescribe"):
        ok = await _say(
            city,
            LEDGER,
            (
                f"prescribe patient={citizen} procedure={FOLLOW_UP_PROCEDURE} "
                f"priority={FOLLOW_UP_PRIORITY} follow_up_of={booking_id} {rx_token}"
            ),
        )
        if not ok:
            return ""
    found = await _await_line(city, need_id, chat, "prescribed", rx_token, op="prescribe")
    if found is None:
        return ""
    reply, _ = found
    if first_word(reply.text) == "prescribed" or "DUPLICATE" in reply.text.upper():
        return _safe_token(fields_of(reply.text).get("prescription_number")) or rx_num
    return ""


async def _on_prescribed(city: City, message: Message, chat: list[Message]) -> None:
    """Handle an asynchronous ``prescribed`` confirmation for a CTRL-CARD follow-up."""
    fields = fields_of(message.text)
    rx_num = _safe_token(fields.get("prescription_number"))
    booking_id = _safe_token(fields.get("follow_up_of")) or _checked_in_booking_id(chat)
    citizen = _safe_token(fields.get("patient")) or _citizen(chat)
    if not rx_num or not booking_id or not citizen:
        return
    if _reported_booking_id(chat) != booking_id:
        return
    if _we(chat, city.agent_id, f"prescription_number={rx_num}", to=citizen, word="prescription"):
        return
    priority = _safe_token(fields.get("priority")) or FOLLOW_UP_PRIORITY
    procedure = _safe_token(fields.get("procedure")) or FOLLOW_UP_PROCEDURE
    english = _is_english(chat)
    lead = _empathy_lead(english=english, anxious=_is_anxious(chat))
    note = (
        f"{lead}Your follow-up prescription ({procedure}) is ready: please use this number at the booking centre."
        if english
        else f"{lead}La ricetta per il controllo cardiologico ({procedure}) è pronta: può prenotarla al centro prenotazioni."
    )
    await _say(
        city,
        citizen,
        f"prescription prescription_number={rx_num} procedure={procedure} "
        f"priority={priority} follow_up_of={booking_id} {note}",
        allowed_citizen=citizen,
    )


async def _on_reported(city: City, message: Message, chat: list[Message]) -> None:
    fields = fields_of(message.text)
    booking_id = _safe_token(fields.get("booking_id"))
    citizen = _citizen(chat)
    if not booking_id or not citizen:
        return

    token = f"booking_id={booking_id}"
    consent_ref = _safe_token(fields.get("consent_ref"))
    follow_up_rx = _safe_token(fields.get("follow_up_prescription_number"))
    for row in reversed(chat):
        if row.sender == city.agent_id and row.recipient == LEDGER and token in row.text.split():
            if row.text.strip().startswith("report "):
                r_fields = fields_of(row.text)
                consent_ref = consent_ref or _safe_token(r_fields.get("consent_ref"))
                follow_up_rx = follow_up_rx or _safe_token(
                    r_fields.get("follow_up_prescription_number")
                )
                break

    english = _is_english(chat)
    lead = _empathy_lead(english=english, anxious=_is_anxious(chat))

    if not _we(chat, city.agent_id, token, to=citizen, word="report"):
        # Administrative confirmation only: outcome_code without any clinical content or diagnosis.
        parts = ["report", f"booking_id={booking_id}", f"outcome_code={OUTCOME_CODE}"]
        if consent_ref:
            parts.append(f"consent_ref={consent_ref}")
        if follow_up_rx:
            parts.append(f"follow_up_prescription_number={follow_up_rx}")
        if english:
            note = (
                f"{lead}Your visit report has been archived and shared as authorized. Thank you."
                if consent_ref
                else f"{lead}Your visit report has been archived privately without sharing, as requested."
            )
        else:
            note = (
                f"{lead}Il referto della sua visita è stato archiviato e condiviso come da suo consenso. Grazie."
                if consent_ref
                else f"{lead}Il referto della sua visita è stato archiviato in forma riservata senza condivisione, come richiesto."
            )
        parts.append(note)
        await _say(city, citizen, " ".join(parts), allowed_citizen=citizen)

    if follow_up_rx and not _we(
        chat, city.agent_id, f"prescription_number={follow_up_rx}", to=citizen, word="prescription"
    ):
        note = (
            f"{lead}I have also issued your follow-up prescription ({FOLLOW_UP_PROCEDURE}): "
            "you can schedule it through the booking centre."
            if english
            else f"{lead}Ho emesso anche la ricetta per il controllo cardiologico ({FOLLOW_UP_PROCEDURE}): "
            "può prenotarla rivolgendosi al centro prenotazioni."
        )
        await _say(
            city,
            citizen,
            f"prescription prescription_number={follow_up_rx} procedure={FOLLOW_UP_PROCEDURE} "
            f"priority={FOLLOW_UP_PRIORITY} follow_up_of={booking_id} {note}",
            allowed_citizen=citizen,
        )


async def _on_ledger_refused(city: City, message: Message, chat: list[Message]) -> None:
    kind = words(message.text)
    if "hold" in kind or "commit" in kind:
        await _on_booking_refused(city, message, chat)
    elif "checkin" in kind:
        if "DUPLICATE" in message.text.upper():
            bk = _safe_token(fields_of(message.text).get("booking_id"))
            if bk:
                await _after_checked_in(city, bk, chat)
        else:
            citizen = _citizen(chat)
            if citizen:
                reason = _extract_refusal_reason(message.text)
                english = _is_english(chat)
                msg = (
                    f"We could not register your check-in ({reason}). "
                    "Please verify your booking receipt or contact the booking centre."
                    if english
                    else f"Non è stato possibile registrare il check-in ({reason}). "
                    "Verifichi la sua ricevuta o contatti il centro prenotazioni."
                )
                await _say(city, citizen, msg, allowed_citizen=citizen)
