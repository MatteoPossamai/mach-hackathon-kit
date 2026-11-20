"""Availability search and direct citizen request flow for the specialist."""
from __future__ import annotations


def configure(agent_globals: dict[str, object]) -> None:
    globals().update(agent_globals)


async def _on_agent_availability_request(
    city: City,
    message: Message,
    chat: list[Message],
    key: str,
) -> None:
    """Answer slot availability queries from own or cross-ASST booking centres."""
    fields = msg_fields(message)
    requested_proc = _safe_token(fields.get("procedure") or fields.get("service_type")) or PRIMARY_PROCEDURE
    request_id = _safe_token(fields.get("request_id"))
    priority = _safe_token(fields.get("priority")) or None
    nl_lang, nl_gender, nl_days = _extract_nl_prefs(message.text)
    explicit_lang = _safe_token(fields.get("lang"))
    lang = explicit_lang or nl_lang or "it"
    gender_raw = _safe_token(fields.get("gender") or fields.get("gender_preference"))
    gender = gender_raw if gender_raw in {"m", "f"} else nl_gender
    days_raw = fields.get("days") or fields.get("day") or ""
    days = {d.strip().lower() for d in re.split(r"[/,|]", days_raw) if d.strip()} or nl_days

    # Serve both VIS-CARD and CTRL-CARD cardiology procedures.
    if requested_proc not in SUPPORTED_PROCEDURES:
        reason_parts = ["cancel"]
        if request_id:
            reason_parts.append(f"request_id={request_id}")
        reason_parts.append("reason=unsupported_procedure")
        reason_parts.append(
            f"Questo ambulatorio gestisce solo {', '.join(sorted(SUPPORTED_PROCEDURES))}."
        )
        await _say(city, message.sender, " ".join(reason_parts))
        return

    now, world, chat = await _fetch_agenda_and_world(city, message.need_id, chat, requested_proc)
    if now is None or world is None:
        return

    # Scope cache by request_id so Level 6/7 rebooking requests always get fresh available slots.
    cache_key = f"{key}:{message.sender}:{request_id or message.msg_id}"
    cache = _offered_cache(cache_key)
    slots = _fitting_slots(
        world,
        now=now,
        agent_id=city.agent_id,
        priority=priority,
        lang=lang,
        gender=gender,
        days=days,
        procedure=requested_proc,
    )

    page, pages = _page_info(world)
    while not slots and page < pages and page < _MAX_PAGE:
        next_page = page + 1
        if not _asked_world(chat, city.agent_id, requested_proc, next_page):
            await _say(city, LEDGER, f"world procedure={requested_proc} page={next_page}")
        chat = await _until(
            city,
            message.need_id,
            chat,
            lambda rows, expected=next_page: _page_info(_latest_slots(rows) or "")[0] >= expected,
        )
        world = _latest_slots(chat) or world
        page, pages = _page_info(world)
        slots = _fitting_slots(
            world,
            now=now,
            agent_id=city.agent_id,
            priority=priority,
            lang=lang,
            gender=gender,
            days=days,
            procedure=requested_proc,
        )

    fresh_slots = [s for s in slots if s["slot_id"] not in cache]
    if fresh_slots:
        # On anonymous searches (no explicit gender/lang), diversify across s1 and s2
        # so the booking centre can satisfy any combined citizen preference (Level 5).
        selected = (
            _diversify_by_doctor(fresh_slots, limit=4)
            if (not explicit_lang and not gender)
            else fresh_slots[:3]
        )
        lines = [
            _offer_line(
                slot,
                agent_id=city.agent_id,
                procedure=requested_proc,
                priority=priority,
                request_id=request_id or None,
                include_doctor_attrs=True,
            )
            for slot in selected
        ]
        if await _say(city, message.sender, "\n".join(lines)):
            for slot in selected:
                cache.add(slot["slot_id"])
        return

    if not cache:
        parts = ["cancel"]
        if request_id:
            parts.append(f"request_id={request_id}")
        parts.append("reason=no_availability")
        criteria = []
        if explicit_lang or nl_lang:
            criteria.append(f"lingua={lang}")
        if gender:
            criteria.append(f"genere={gender}")
        if days:
            criteria.append(f"giorni={','.join(sorted(days))}")
        detail = f" Criteri provati: {', '.join(criteria)}." if criteria else ""
        parts.append(
            f"Nessuno slot {requested_proc} disponibile per questi criteri.{detail} "
            "Se il cittadino accetta di allentarli, inviate una nuova richiesta con i criteri aggiornati."
        )
        await _say(city, message.sender, " ".join(parts))


async def _on_citizen_message(
    city: City,
    message: Message,
    chat: list[Message],
    key: str,
) -> None:
    """Handle a direct citizen inquiry (never leave a citizen without a respectful reply)."""
    fields = msg_fields(message)
    number = _safe_token(fields.get("prescription_number") or fields.get("rx"))
    english = _is_english(chat, message.text)
    lead = _empathy_lead(english=english, anxious=_is_anxious(chat, message.text))

    committed = _latest_ledger(chat, "committed")
    committed_bk = _safe_token(fields_of(committed.text).get("booking_id")) if committed else ""
    reported = _latest_ledger(chat, "reported")
    reported_bk = _safe_token(fields_of(reported.text).get("booking_id")) if reported else ""
    if reported_bk and (not committed_bk or reported_bk == committed_bk):
        reply = (
            f"{lead}Your cardiology visit (booking_id={reported_bk}) is already completed and the report "
            "has been filed. To book a new visit, please contact the booking centre."
            if english
            else f"{lead}La sua visita cardiologica (booking_id={reported_bk}) risulta già completata e "
            "refertata. Per un nuovo appuntamento può rivolgersi al centro prenotazioni."
        )
        await _say(city, message.sender, reply, allowed_citizen=message.sender)
        return

    if number:
        await _offer_direct_to_citizen(city, message, chat, key, number)
        return

    if committed_bk and not _booking_has_report(chat, committed_bk):
        reply = (
            f"{lead}Your cardiology appointment (booking_id={committed_bk}) is confirmed. "
            f"When you arrive at the clinic, please send 'checkin booking_id={committed_bk}' "
            "and I will register your arrival right away."
            if english
            else f"{lead}La sua prenotazione cardiologica (booking_id={committed_bk}) è confermata. "
            f"Quando arriva in ambulatorio, mi scriva 'checkin booking_id={committed_bk}' "
            "e registrerò subito il suo ingresso."
        )
        await _say(city, message.sender, reply, allowed_citizen=message.sender)
        return

    reply = (
        f"{lead}Welcome to the Cardiology Specialist Clinic (VIS-CARD / CTRL-CARD). "
        "Please send your prescription number (prescription_number=...) to check available slots, "
        "contact the booking centre to schedule, or send 'checkin booking_id=...' when you arrive."
        if english
        else f"{lead}Benvenuto nell'ambulatorio di cardiologia specialistica (VIS-CARD / CTRL-CARD). "
        "Può inviare il numero di ricetta (prescription_number=...) per verificare le disponibilità, "
        "rivolgersi al centro prenotazioni, oppure scrivere 'checkin booking_id=...' al suo arrivo."
    )
    await _say(city, message.sender, reply, allowed_citizen=message.sender)


async def _offer_direct_to_citizen(
    city: City,
    message: Message,
    chat: list[Message],
    key: str,
    number: str,
) -> None:
    agent = city.agent_id
    english = _is_english(chat, message.text)
    lead = _empathy_lead(english=english, anxious=_is_anxious(chat, message.text))

    rx_reply = _ledger_fields(chat, "prescription")
    if rx_reply is None or rx_reply.get("prescription_number") != number:
        if not _we(chat, agent, f"prescription_number={number}", to=LEDGER, word="prescription"):
            await _say(city, LEDGER, f"prescription prescription_number={number}")
        chat = await _until(
            city,
            message.need_id,
            chat,
            lambda rows: _ledger_fields(rows, "prescription") is not None,
        )
        rx_reply = _ledger_fields(chat, "prescription")
        if rx_reply is None:
            return

    if rx_reply.get("state") not in (None, "issued"):
        reply = (
            f"data.request purpose=prescription_renewal fields=prescription_number "
            f"{lead}Prescription prescription_number={number} is not currently bookable "
            f"(state={rx_reply.get('state')}). Please contact your GP for a renewal."
            if english
            else f"data.request purpose=prescription_renewal fields=prescription_number "
            f"{lead}La ricetta prescription_number={number} non risulta prenotabile "
            f"(stato: {rx_reply.get('state')}). Contatti il medico di base per un rinnovo."
        )
        await _say(city, message.sender, reply, allowed_citizen=message.sender)
        return

    priority = _safe_token(rx_reply.get("priority")) or None
    procedure = _safe_token(rx_reply.get("procedure")) or PRIMARY_PROCEDURE
    if priority and priority.upper() == "U":
        _mark_urgent(key)

    if procedure not in SUPPORTED_PROCEDURES:
        reply = (
            f"{lead}Our clinic handles cardiology visits (VIS-CARD and CTRL-CARD). "
            f"For {procedure}, please contact the booking centre."
            if english
            else f"{lead}Il nostro ambulatorio gestisce solo visite cardiologiche (VIS-CARD e CTRL-CARD). "
            f"Per {procedure} si rivolga al centro prenotazioni."
        )
        await _say(city, message.sender, reply, allowed_citizen=message.sender)
        return

    if priority == "P":
        offer_id = f"wl-{message.need_id or message.msg_id}"
        if not _we(chat, agent, f"offer_id={offer_id}", to=message.sender, word="offer"):
            note = (
                f"{lead}Your prescription has priority P: I can place you on the waiting list."
                if english
                else f"{lead}La sua ricetta ha priorità P: posso inserirla in lista d'attesa."
            )
            await _say(
                city,
                message.sender,
                f"offer offer_id={offer_id} waiting_list=true prescription_number={number} {note}",
                allowed_citizen=message.sender,
            )
        return

    now, world, chat = await _fetch_agenda_and_world(city, message.need_id, chat, procedure)
    if now is None or world is None:
        return

    fields = msg_fields(message)
    nl_lang, nl_gender, nl_days = _extract_nl_prefs(message.text)
    lang = _safe_token(fields.get("lang")) or nl_lang or ("en" if english else "it")
    gender_raw = _safe_token(fields.get("gender") or fields.get("gender_preference"))
    gender = gender_raw if gender_raw in {"m", "f"} else nl_gender
    days_raw = fields.get("days") or fields.get("day") or ""
    days = {d.strip().lower() for d in re.split(r"[/,|]", days_raw) if d.strip()} or nl_days

    slots = _fitting_slots(
        world,
        now=now,
        agent_id=agent,
        priority=priority,
        lang=lang,
        gender=gender,
        days=days,
        procedure=procedure,
    )
    cache = _offered_cache(f"{key}:{message.sender}")
    fresh_slots = [s for s in slots if s["slot_id"] not in cache]
    if fresh_slots:
        lines = [
            _offer_line(
                slot,
                agent_id=agent,
                procedure=procedure,
                number=number,
                priority=priority,
            )
            for slot in fresh_slots[:3]
        ]
        closing = (
            f"{lead}Here are the available cardiology slots matching your prescription."
            if english
            else f"{lead}Ecco le disponibilità cardiologiche compatibili con la sua ricetta."
        )
        if await _say(
            city,
            message.sender,
            "\n".join(lines) + f"\n{closing}",
            allowed_citizen=message.sender,
        ):
            for slot in fresh_slots[:3]:
                cache.add(slot["slot_id"])
    elif not cache:
        reply = (
            f"{lead}No matching cardiology slots are currently available here. "
            "Please contact the booking centre to check partner clinics as well."
            if english
            else f"{lead}Al momento non ci sono slot cardiologici compatibili presso il nostro "
            "ambulatorio. Le consiglio di contattare il centro prenotazioni per verificare "
            "anche altre strutture."
        )
        await _say(city, message.sender, reply, allowed_citizen=message.sender)


async def _resume_pending_offers(
    city: City,
    message: Message,
    chat: list[Message],
    key: str,
) -> None:
    """Complete a deferred offer lookup when a Ledger reply opens a fresh turn."""
    for row in reversed(chat):
        if row.sender in (LEDGER, city.agent_id):
            continue
        kind = words(row.text)
        if "confirm" in kind or "checkin" in kind:
            return
        if row.sender.startswith("agent:"):
            await _on_agent_availability_request(city, row, chat, key)
            return
        if row.sender.startswith("citizen:"):
            number = _safe_token(msg_fields(row).get("prescription_number"))
            if number:
                await _offer_direct_to_citizen(city, row, chat, key, number)
            return


# --- booking flow: confirm -> hold -> commit -> booking.receipt --------------------------------


