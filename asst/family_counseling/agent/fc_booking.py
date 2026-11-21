"""Direct citizen offers, partner availability (MACH-1.2), booking (hold/commit), and check-in."""
from __future__ import annotations

import re
from collections import OrderedDict

from city_sdk.city import LEDGER, City, Message

import fc_common as c
import fc_ring as ring
import fc_slots as s

_SENT_NEEDS = 4096
_OFFERED_NEEDS: OrderedDict[str, set[str]] = OrderedDict()
# Level 7 idempotency: (sender, request_id) -> exact reply text sent.
_REQ_REPLIES: OrderedDict[tuple[str, str], str] = OrderedDict()


def reset_caches() -> None:
    _OFFERED_NEEDS.clear()
    _REQ_REPLIES.clear()


def _offered_cache(key: str) -> set[str]:
    entry = _OFFERED_NEEDS.get(key)
    if entry is None:
        entry = _OFFERED_NEEDS[key] = set()
        while len(_OFFERED_NEEDS) > _SENT_NEEDS:
            _OFFERED_NEEDS.popitem(last=False)
    else:
        _OFFERED_NEEDS.move_to_end(key)
    return entry


def _remember_req_reply(sender: str, request_id: str, reply: str) -> None:
    if not request_id:
        return
    k = (sender, request_id)
    _REQ_REPLIES[k] = reply
    _REQ_REPLIES.move_to_end(k)
    while len(_REQ_REPLIES) > _SENT_NEEDS:
        _REQ_REPLIES.popitem(last=False)


async def offer_to_citizen(
    city: City,
    message: Message,
    chat: list[Message],
    key: str,
    *,
    replacing: bool,
    urgent: bool = False,
) -> None:
    """Direct access counseling booking: NEVER ask for a prescription, send ALL offers in 1 message."""
    agent = city.agent_id
    citizen = message.sender if message.sender.startswith("citizen:") else (c.citizen_of(chat) or "")
    if not citizen:
        return

    english = c.is_english(chat, message.text)
    lead = c.empathy_lead(english=english, tone=c.citizen_tone(chat, message.text))
    street = c.own_street(city)

    if not replacing:
        committed = c.latest_ledger(chat, "committed")
        if committed is not None:
            bk = c.safe_token(c.fields_of(committed.text).get("booking_id"))
            if bk and c.latest_ledger(chat, "checked_in", f"booking_id={bk}") is None:
                where = (f" at {street}" if english else f" in {street}") if street else ""
                reply = (
                    f"{lead}Your counseling appointment (booking_id={bk}) is already set{where} "
                    f"(direct access, no referral needed, full confidentiality). "
                    f"When you arrive, simply share code {bk} so we can welcome you right away."
                    if english else
                    f"{lead}Il suo appuntamento al consultorio (booking_id={bk}) è già fissato{where} "
                    f"(accesso diretto, senza ricetta, massima riservatezza). "
                    f"Al suo arrivo le basterà comunicare il codice {bk} per essere accolta subito."
                )
                await c.say(city, citizen, reply, allowed_citizen=citizen)
                return

    lang, gender, days, priority = s.citizen_prefs(chat, message)
    is_urg = urgent or priority == "U"
    now, world, chat = await s.fetch_agenda_and_world(city, message.need_id, chat, urgent=is_urg)
    if now is None or world is None:
        return

    excluded = s.cancelled_slots(chat, agent)
    cache = _offered_cache(f"{key}:{citizen}:{'re' if replacing else 'init'}")
    slots = s.fitting_slots(
        world, now=now, agent_id=agent, priority=priority,
        lang=lang, gender=gender, days=days, exclude_slots=excluded,
    )
    page, pages = s.page_info(world)
    while not slots and page < pages and page < c.MAX_PAGE:
        next_page = page + 1
        if not s.asked_world(chat, agent, next_page):
            await c.say(city, LEDGER, f"world procedure={c.PROCEDURE} page={next_page}")
        chat = await c.until(
            city, message.need_id, chat,
            lambda rows, expected=next_page: s.page_info(c.latest_slots(rows) or "")[0] >= expected,
            urgent=is_urg,
        )
        world = c.latest_slots(chat) or world
        page, pages = s.page_info(world)
        slots = s.fitting_slots(
            world, now=now, agent_id=agent, priority=priority,
            lang=lang, gender=gender, days=days, exclude_slots=excluded,
        )

    relaxed = False
    if not slots and (days or gender):
        slots, _ = s.relaxed_slots(
            world, now=now, agent_id=agent, priority=priority,
            lang=lang, gender=gender, days=days, exclude_slots=excluded,
        )
        relaxed = bool(slots)

    fresh_slots = s.rotate_slots([sl for sl in slots if sl["slot_id"] not in cache], key)
    if fresh_slots:
        chosen = fresh_slots[:4]
        lines = [s.offer_line(sl, agent_id=agent, priority=priority) for sl in chosen]
        where = (f" at our clinic ({street})" if english else f" presso la nostra sede ({street})") if street else ""
        if english:
            note = (
                f"{lead}No slot matched every day/doctor preference, so here are the closest available "
                f"family counseling slots{where} (direct access, no referral needed, full confidentiality; "
                "female gynecologist, Italian/English, Mon-Fri). Tell me which slot works best for you."
                if relaxed else
                f"{lead}Here are the available family counseling slots{where} (direct access, no referral needed, "
                "full confidentiality; female gynecologist, Italian/English, Mon-Fri). "
                "Let me know which option you prefer and I will finalize it right away."
            )
        else:
            note = (
                f"{lead}Non ci sono posti con tutte le preferenze indicate, ma ecco le alternative più vicine{where} "
                "(accesso diretto senza ricetta, massima riservatezza; ginecologa lun-ven, italiano e inglese). "
                "Mi indichi pure la scelta preferita e la blocco subito per lei."
                if relaxed else
                f"{lead}Ecco le disponibilità del consultorio{where} (accesso diretto senza ricetta, "
                "massima riservatezza; ginecologa lun-ven, italiano e inglese). "
                "Mi indichi pure la scelta che preferisce e completo subito la prenotazione."
            )
        if await c.say(city, citizen, "\n".join(lines) + f"\n{note}", allowed_citizen=citizen):
            for sl in chosen:
                cache.add(sl["slot_id"])
        return

    if not cache:
        deadline = s.priority_deadline(now, priority) if priority in s.DEADLINE_DAYS else None
        contacted = await ring.find_partner_counseling(
            city, message.need_id, chat, priority=priority or "B",
            lang=lang, gender=gender, days=days, deadline=deadline,
        )
        if contacted:
            reply = (
                f"{lead}No matching slot is currently open in our clinic (direct access, no referral needed): "
                "I am checking partner family counseling clinics right now without sharing any personal data. "
                "As soon as a match arrives, I will ask your authorization before booking."
                if english else
                f"{lead}Nella nostra sede non ci sono posti compatibili al momento (accesso diretto senza ricetta): "
                "sto già verificando i consultori partner sul territorio senza condividere alcun dato personale. "
                "Appena trovo una disponibilità le chiederò il permesso prima di procedere."
            )
            await c.say(city, citizen, reply, allowed_citizen=citizen)
            return
        wl_id = f"wl-{c.safe_token(message.need_id) or 'fc'}"
        reply = (
            f"offer offer_id={wl_id} waiting_list=true "
            f"{lead}All matching slots at our clinic are currently full (direct access, no referral needed). "
            "I can put you on the waiting list or check another day or time — tell me what works best for you."
            if english else
            f"offer offer_id={wl_id} waiting_list=true "
            f"{lead}Al momento tutti i posti compatibili nella nostra sede sono occupati (accesso diretto senza ricetta). "
            "Posso inserirla in lista d'attesa oppure verificare un altro giorno o orario: mi dica pure."
        )
        await c.say(city, citizen, reply, allowed_citizen=citizen)


async def on_agent_availability_request(
    city: City, message: Message, chat: list[Message], key: str, *, urgent: bool = False
) -> None:
    """MACH-1.2 partner need.request: idempotency (L7), soft lock (L7), relaxed offers (L5), reserve=U (L8)."""
    if c.is_injection(message.text):
        return
    fields = c.msg_fields(message)
    request_id = c.safe_token(fields.get("request_id"))
    if request_id and (message.sender, request_id) in _REQ_REPLIES:
        await c.say(city, message.sender, _REQ_REPLIES[(message.sender, request_id)], dedup=False)
        return

    requested_proc = c.safe_token(fields.get("procedure") or fields.get("service_type")) or c.PROCEDURE
    if requested_proc not in {c.PROCEDURE, "family_counseling.gynecology", "family_counseling"}:
        parts = ["cancel"]
        if request_id:
            parts.append(f"request_id={request_id}")
        parts.extend(["reason=procedure_not_offered", f"Il consultorio gestisce solo {c.PROCEDURE}."])
        reply = " ".join(parts)
        _remember_req_reply(message.sender, request_id, reply)
        await c.say(city, message.sender, reply)
        return

    priority = c.safe_token(fields.get("priority")) or None
    nl_lang, nl_gender, nl_days = s.extract_nl_prefs(message.text)
    lang = c.safe_token(fields.get("lang")) or nl_lang or "it"
    g_raw = c.safe_token(fields.get("gender_preference") or fields.get("gender"))
    gender = g_raw if g_raw in {"m", "f"} else nl_gender
    days_raw = fields.get("days") or fields.get("day") or ""
    days = {s.normalize_day(d) for d in re.split(r"[/,|]", days_raw) if d.strip()} or nl_days
    after = c.parse_when(fields.get("after", "") or fields.get("earliest", ""))  # MACH-1.3 R4
    before = c.parse_when(fields.get("before", ""))
    deadline = c.parse_when(fields.get("deadline", ""))

    # MACH-1.1: acknowledge within seconds (once per request), the Ledger read below takes longer.
    ack_token = f"request_id={request_id}" if request_id else ""
    if not (ack_token and c.we_sent(chat, city.agent_id, ack_token, to=message.sender, word="ack")):
        await c.say(city, message.sender,
                    "ack" + (f" {ack_token}" if ack_token else "") + " Ricevuto, verifico la disponibilità.")

    now, world, chat = await s.fetch_agenda_and_world(city, message.need_id, chat, urgent=urgent or priority == "U")
    if now is None or world is None:
        return

    strict = s.fitting_slots(
        world, now=now, agent_id=city.agent_id, priority=priority, lang=lang,
        gender=gender, days=days, after=after, before=before, deadline=deadline,
        request_id=request_id, respect_soft_lock=True,
    )
    if strict:
        chosen = s.rotate_slots(strict, f"{key}:{request_id}")[:3]
        lines = [
            s.offer_line(sl, agent_id=city.agent_id, priority=priority, request_id=request_id or None, for_partner=True)
            for sl in chosen
        ]
        lines = [line + " lease_s=120 mach=1.3" for line in lines]  # MACH-1.3
        reply = "\n".join(lines)
        if await c.say(city, message.sender, reply):
            s.soft_lock_slots([sl["slot_id"] for sl in chosen], request_id or key)
            _remember_req_reply(message.sender, request_id, reply)
        return

    # Level 5 MACH-1.2: cancel reason=no_availability + up to 3 relaxed offers below it.
    rel_slots, rel_tag = s.relaxed_slots(
        world, now=now, agent_id=city.agent_id, priority=priority, lang=lang,
        gender=gender, days=days, after=after, before=before, deadline=deadline,
        request_id=request_id, respect_soft_lock=True,
    )
    cancel_parts = ["cancel", "reason=no_availability"]
    if request_id:
        cancel_parts.append(f"request_id={request_id}")
    cancel_parts.append("mach=1.3")
    lines = [" ".join(cancel_parts)]
    for sl in rel_slots[:3]:
        lines.append(
            s.offer_line(
                sl, agent_id=city.agent_id, priority=priority,
                request_id=request_id or None, for_partner=True, relaxed_tag=rel_tag,
            )
        )
    lines.append(
        "Con tutti i vincoli non c'è posto; sopra le alternative più vicine."
        if rel_slots else "Nessuno slot CONS-GIN disponibile per i criteri richiesti."
    )
    reply = "\n".join(lines)
    if await c.say(city, message.sender, reply):
        _remember_req_reply(message.sender, request_id, reply)


def _our_offer(chat: list[Message], agent: str, offer_id: str) -> dict[str, str]:
    tok = f"offer_id={offer_id}"
    for row in chat:
        if row.sender == agent and "offer" in c.words(row.text):
            for line in row.text.splitlines():
                if tok in line.split():
                    return c.fields_of(line)
    return {}


def _booking_context(chat: list[Message], agent: str, slot_id: str) -> tuple[str, str, str, str]:
    tok = f"slot_id={slot_id}"
    requester, consent, offer_id, cross_consent = "", "", "", ""
    for row in chat:
        f = c.msg_fields(row)
        row_offer = c.safe_token(f.get("offer_id"))
        if tok in row.text.split() or (row_offer and _our_offer(chat, agent, row_offer).get("slot_id") == slot_id):
            if "confirm" in c.words(row.text) and row.sender not in (LEDGER, agent):
                requester = row.sender
                consent = c.safe_token(f.get("consent_ref")) or consent
                cross_consent = c.safe_token(f.get("cross_asst_consent_ref") or f.get("cross_asst_consent")) or cross_consent
                offer_id = row_offer or offer_id
            elif row.sender == agent and row.text.strip().startswith("hold "):
                consent = c.safe_token(f.get("consent_ref")) or consent
                cross_consent = c.safe_token(f.get("cross_asst_consent_ref")) or cross_consent
                requester = requester or c.safe_token(f.get("citizen"))
            elif row.sender == agent and "offer" in c.words(row.text):
                offer_id = row_offer or offer_id
    return requester, consent, offer_id, cross_consent


def _latest_hold_info(chat: list[Message], agent: str) -> tuple[str, str, str]:
    slot, consent, cross_consent = "", "", ""
    for row in chat:
        if row.sender == agent and row.recipient == LEDGER and row.text.strip().startswith("hold "):
            f = c.fields_of(row.text)
            slot = c.safe_token(f.get("slot_id")) or slot
            consent = c.safe_token(f.get("consent_ref")) or consent
            cross_consent = c.safe_token(f.get("cross_asst_consent_ref")) or cross_consent
    return slot, consent, cross_consent


def _hold_slot(chat: list[Message], agent: str, hold_id: str) -> str:
    tok = f"hold_id={hold_id}"
    for row in chat:
        if tok in row.text.split() and row.sender in (agent, LEDGER):
            slot = c.safe_token(c.fields_of(row.text).get("slot_id"))
            if slot:
                return slot
    return ""


def receipt_text(
    booking_id: str,
    ledger_ref: str,
    *,
    chat: list[Message],
    to_citizen: bool,
    slot_id: str = "",
    offer_id: str = "",
    street: str = "",
) -> str:
    parts = ["booking.receipt", f"booking_id={booking_id}", f"ledger_ref={ledger_ref}"]
    if not to_citizen:
        if slot_id:
            parts.append(f"slot_id={slot_id}")
        if offer_id:
            parts.append(f"offer_id={offer_id}")
        parts.append("Prenotazione registrata per il consultorio (CONS-GIN).")
        return " ".join(parts)
    english = c.is_english(chat)
    lead = c.empathy_lead(english=english, tone=c.citizen_tone(chat))
    when = c.slot_schedule_hint(chat, slot_id)
    when_s = (f" for {when}" if english else f" per {when}") if when else ""
    where_s = (f" at {street}" if english else f" presso la sede di {street}") if street else ""
    note = (
        f"{lead}Your family counseling appointment is set{when_s}{where_s} "
        f"(direct access, no referral needed, full confidentiality). "
        f"Please keep this receipt and share code {booking_id} on arrival."
        if english else
        f"{lead}Il suo appuntamento al consultorio è fissato{when_s}{where_s} "
        f"(accesso diretto senza ricetta, massima riservatezza). "
        f"Conservi questa ricevuta e presenti il codice {booking_id} al suo arrivo."
    )
    parts.append(note)
    return " ".join(parts)


async def resume_confirm_with_consent(city: City, message: Message, chat: list[Message], key: str) -> bool:
    consent_ref = c.safe_token(c.msg_fields(message).get("consent_ref"))
    if not consent_ref:
        return False
    for row in reversed(chat):
        if row.sender == message.sender and "confirm" in c.words(row.text):
            f = c.msg_fields(row)
            offer_id, slot = c.safe_token(f.get("offer_id")), c.safe_token(f.get("slot_id"))
            if offer_id or slot:
                synth = Message(
                    type="chat", sender=message.sender, recipient=city.agent_id,
                    msg_id=message.msg_id, need_id=message.need_id, conversation_id=message.conversation_id,
                    body={"text": f"confirm offer_id={offer_id} slot_id={slot} consent_ref={consent_ref}"},
                )
                await on_confirm(city, synth, chat, key)
                return True
    return False


async def on_confirm(city: City, message: Message, chat: list[Message], key: str, *, urgent: bool = False) -> None:
    agent = city.agent_id
    f = c.msg_fields(message)
    offer_id = c.safe_token(f.get("offer_id"))
    offer = _our_offer(chat, agent, offer_id) if offer_id else {}
    slot = c.safe_token(f.get("slot_id") or offer.get("slot_id"))
    facility = c.safe_token(f.get("facility") or offer.get("facility")) or agent
    consent = c.safe_token(f.get("consent_ref"))
    # MACH-1.2: if both consent_ref and cross_asst_consent_ref exist, cross_asst_consent_ref wins for cross-ASST.
    cross_consent = c.safe_token(
        f.get("cross_asst_consent_ref") or f.get("cross_asst_consent") or ring.cross_asst_consent_ref(chat, agent)
    )
    citizen = c.safe_token(f.get("citizen") or (message.sender if message.sender.startswith("citizen:") else "") or c.citizen_of(chat))

    if not slot:
        parts = ["cancel"]
        if offer_id:
            parts.append(f"offer_id={offer_id}")
        parts.extend(["reason=not_offered", "Manca slot_id per completare la prenotazione."])
        await c.say(city, message.sender, " ".join(parts), allowed_citizen=citizen or None)
        return

    if not consent and message.sender.startswith("citizen:"):
        english = c.is_english(chat, message.text)
        lead = c.empathy_lead(english=english, tone=c.citizen_tone(chat, message.text))
        ask = (
            f"consent purpose=booking {lead}To finalize your counseling appointment, may I proceed with this slot?"
            if english else
            f"consent purpose=booking {lead}Per fissare il suo appuntamento al consultorio, mi autorizza a procedere con questo posto?"
        )
        await c.say(city, message.sender, ask, allowed_citizen=message.sender)
        return

    if message.sender.startswith("citizen:") and facility != agent and facility.startswith("agent:"):
        if not cross_consent:
            english = c.is_english(chat, message.text)
            ask = (
                "consent purpose=cross_asst Before booking at the partner clinic, may I share your details outside our ASST?"
                if english else
                "consent purpose=cross_asst Prima di prenotare presso il consultorio partner, mi autorizza a condividere i dati fuori ASST?"
            )
            await c.say(city, message.sender, ask, allowed_citizen=message.sender)
            return
        if not c.we_sent(chat, agent, f"slot_id={slot}", to=facility, word="confirm"):
            parts = [
                "confirm", f"offer_id={offer_id or f'of-{slot}'}", f"slot_id={slot}",
                f"citizen={citizen}", f"consent_ref={consent}",
                f"cross_asst_consent_ref={cross_consent}", f"facility={facility}",
            ]
            if not await c.say(city, facility, " ".join(parts), allowed_citizen=citizen):
                await c.say(
                    city, message.sender,
                    f"cancel offer_id={offer_id or f'of-{slot}'} slot_id={slot} reason=facility_unavailable "
                    "La struttura partner non è raggiungibile.",
                    allowed_citizen=message.sender,
                )
        return

    # Level 7 idempotency: duplicate confirm returns existing receipt without double-holding.
    existing = c.latest_ledger(chat, "committed")
    if existing is not None:
        cf = c.fields_of(existing.text)
        bk, ref = c.safe_token(cf.get("booking_id")), c.safe_token(cf.get("ledger_ref"))
        c_slot = c.safe_token(cf.get("slot_id")) or _hold_slot(chat, agent, c.safe_token(cf.get("hold_id")) or bk)
        if bk and ref and (not c_slot or c_slot == slot):
            await c.say(
                city, message.sender,
                receipt_text(bk, ref, chat=chat, to_citizen=message.sender.startswith("citizen:"),
                             slot_id=slot, offer_id=offer_id, street=c.own_street(city)),
                allowed_citizen=citizen or None, dedup=False,
            )
            return

    if not c.we_sent(chat, agent, f"slot_id={slot}", to=LEDGER, word="hold"):
        parts = ["hold", f"slot_id={slot}"]
        if citizen:
            parts.append(f"citizen={citizen}")
        if consent:
            parts.append(f"consent_ref={consent}")
        if cross_consent:
            parts.append(f"cross_asst_consent_ref={cross_consent}")
        if not await c.say(city, LEDGER, " ".join(parts)):
            return

    found = await c.await_line(city, message.need_id, chat, "held", f"slot_id={slot}", op="hold", urgent=urgent)
    if found is None:
        return
    reply, fresh = found
    if c.first_word(reply.text) == "refused":
        await on_booking_refused(city, reply, fresh, key, slot=slot, offer_id=offer_id, requester=message.sender)
        return
    if c.first_word(reply.text) != "held":
        return

    await on_held(city, reply, fresh)
    hold_id = c.safe_token(c.fields_of(reply.text).get("hold_id"))
    if not hold_id:
        return

    done = await c.await_line(city, message.need_id, fresh, "committed", f"booking_id={hold_id}", op="commit", urgent=urgent)
    if done is None:
        return
    c_reply, c_fresh = done
    if c.first_word(c_reply.text) == "committed":
        await on_committed(city, c_reply, c_fresh)
    elif c.first_word(c_reply.text) == "refused":
        reason, raw = c.translate_refusal(c_reply.text)
        if raw.upper() == "HOLD_EXPIRED":
            retry = ["hold", f"slot_id={slot}"]
            if citizen:
                retry.append(f"citizen={citizen}")
            if consent:
                retry.append(f"consent_ref={consent}")
            if cross_consent:
                retry.append(f"cross_asst_consent_ref={cross_consent}")
            if await c.say(city, LEDGER, " ".join(retry)):
                re_held = await c.await_line(city, message.need_id, c_fresh, "held", f"slot_id={slot}", op="hold", urgent=urgent)
                if re_held and c.first_word(re_held[0].text) == "held":
                    new_hid = c.safe_token(c.fields_of(re_held[0].text).get("hold_id"))
                    if new_hid and new_hid != hold_id:
                        await on_held(city, re_held[0], re_held[1])
                        re_done = await c.await_line(city, message.need_id, re_held[1], "committed", f"booking_id={new_hid}", op="commit", urgent=urgent)
                        if re_done and c.first_word(re_done[0].text) == "committed":
                            await on_committed(city, re_done[0], re_done[1])
                            return
        await on_booking_refused(city, c_reply, c_fresh, key, slot=slot, offer_id=offer_id, requester=message.sender)


async def on_held(city: City, message: Message, chat: list[Message]) -> None:
    f = c.fields_of(message.text)
    hold_id, slot = c.safe_token(f.get("hold_id")), c.safe_token(f.get("slot_id"))
    if not hold_id or not slot:
        return
    if any(r.sender == city.agent_id and r.recipient == LEDGER and r.text.strip().startswith("commit ") and f"hold_id={hold_id}" in r.text.split() for r in chat):
        return
    _, consent, _, cross_consent = _booking_context(chat, city.agent_id, slot)
    if not consent:
        _, consent, held_cross = _latest_hold_info(chat, city.agent_id)
        cross_consent = cross_consent or held_cross
    need_ref = c.safe_token(f.get("need_id") or message.need_id)
    if not consent and need_ref.startswith("N-"):
        consent = f"cons-{need_ref}-booking"
    if not consent:
        return
    fence = c.safe_token(f.get("fence")) or "1"
    parts = ["commit", f"hold_id={hold_id}", f"consent_ref={consent}", f"fence={fence}"]
    if cross_consent:
        parts.append(f"cross_asst_consent_ref={cross_consent}")
    await c.say(city, LEDGER, " ".join(parts))


async def on_committed(city: City, message: Message, chat: list[Message]) -> None:
    f = c.fields_of(message.text)
    booking_id, ledger_ref = c.safe_token(f.get("booking_id")), c.safe_token(f.get("ledger_ref"))
    if not booking_id or not ledger_ref:
        return
    slot = c.safe_token(f.get("slot_id")) or _hold_slot(chat, city.agent_id, c.safe_token(f.get("hold_id")) or booking_id)
    requester, _, offer_id, _ = _booking_context(chat, city.agent_id, slot) if slot else ("", "", "", "")
    target = requester or c.citizen_of(chat)
    if not target or c.we_sent(chat, city.agent_id, f"booking_id={booking_id}", to=target, word="booking.receipt"):
        return
    await c.say(
        city, target,
        receipt_text(booking_id, ledger_ref, chat=chat, to_citizen=target.startswith("citizen:"),
                     slot_id=slot, offer_id=offer_id, street=c.own_street(city)),
        allowed_citizen=c.citizen_of(chat),
    )


async def on_booking_refused(
    city: City, message: Message, chat: list[Message], key: str,
    *, slot: str = "", offer_id: str = "", requester: str = "",
) -> None:
    agent = city.agent_id
    if not slot:
        slot, _, _ = _latest_hold_info(chat, agent)
    if slot:
        ctx_req, _, ctx_offer, _ = _booking_context(chat, agent, slot)
        requester = requester or ctx_req
        offer_id = offer_id or ctx_offer
    target = requester or c.citizen_of(chat)
    if not target:
        return
    reason, raw = c.translate_refusal(message.text)
    dedup_tok = f"offer_id={offer_id}" if offer_id else f"slot_id={slot}"
    if not (dedup_tok and c.we_sent(chat, agent, dedup_tok, to=target, word="cancel")):
        parts = ["cancel"]
        if offer_id:
            parts.append(f"offer_id={offer_id}")
        if slot:
            parts.append(f"slot_id={slot}")
        parts.append(f"reason={reason}")
        if raw and raw.lower() != reason:
            parts.append(f"ledger_code={raw}")
        lines = [" ".join(parts)]
        # MACH-1.2: on slot_taken to a partner, append replacement offers right below cancel.
        if target.startswith("agent:") and reason in {"slot_taken", "expired", "facility_cancelled"}:
            now, world = c.agenda_now(chat), c.latest_slots(chat)
            if now and world:
                alts = s.fitting_slots(world, now=now, agent_id=agent, priority=None, lang="it", exclude_slots={slot} if slot else None)
                for alt in alts[:2]:
                    lines.append(s.offer_line(alt, agent_id=agent, for_partner=True))
        if target.startswith("citizen:"):
            lines.append(
                f"That slot is no longer available ({reason}). Let me check other available times for you."
                if c.is_english(chat) else
                f"Il posto scelto non è più disponibile ({reason}). Verifico subito altre disponibilità."
            )
        await c.say(city, target, "\n".join(lines), allowed_citizen=c.citizen_of(chat))
    if target.startswith("citizen:"):
        fresh = await city.history(need_id=message.need_id)
        await offer_to_citizen(city, message, fresh, key, replacing=True)


async def on_checkin(city: City, message: Message, chat: list[Message], *, urgent: bool = False) -> None:
    agent = city.agent_id
    f = c.msg_fields(message)
    booking_id = c.safe_token(f.get("booking_id"))
    if not booking_id:
        committed = c.latest_ledger(chat, "committed")
        if committed:
            booking_id = c.safe_token(c.fields_of(committed.text).get("booking_id"))
    citizen = message.sender if message.sender.startswith("citizen:") else (c.citizen_of(chat) or "")
    if not citizen:
        return
    english = c.is_english(chat, message.text)
    lead = c.empathy_lead(english=english, tone=c.citizen_tone(chat, message.text))
    if not booking_id:
        prompt = (
            f"data.request purpose=checkin fields=booking_id {lead}Welcome! Please share your booking_id so I can register your arrival."
            if english else
            f"data.request purpose=checkin fields=booking_id {lead}Benvenuta! Può indicarmi il booking_id per registrare il suo arrivo al consultorio?"
        )
        await c.say(city, citizen, prompt, allowed_citizen=citizen)
        return
    tok = f"booking_id={booking_id}"
    if not c.we_sent(chat, agent, tok, to=LEDGER, word="checkin"):
        if not await c.say(city, LEDGER, f"checkin booking_id={booking_id} citizen={citizen}"):
            return
    found = await c.await_line(city, message.need_id, chat, "checked_in", tok, op="checkin", urgent=urgent)
    if found is None:
        return
    reply, fresh = found
    if c.first_word(reply.text) == "checked_in" or "DUPLICATE" in reply.text.upper():
        await confirm_checkin(city, booking_id, citizen, fresh)
    elif c.first_word(reply.text) == "refused":
        reason, _ = c.translate_refusal(reply.text)
        err = (
            f"{lead}I could not register your arrival for booking_id={booking_id} ({reason}). Please verify your receipt."
            if english else
            f"{lead}Non è stato possibile registrare il suo arrivo per booking_id={booking_id} ({reason}). Verifichi la ricevuta."
        )
        await c.say(city, citizen, err, allowed_citizen=citizen)


async def confirm_checkin(city: City, booking_id: str, citizen: str, chat: list[Message]) -> None:
    if c.we_sent(chat, city.agent_id, f"booking_id={booking_id}", to=citizen, word="checkin"):
        return
    english = c.is_english(chat)
    lead = c.empathy_lead(english=english, tone=c.citizen_tone(chat))
    msg = (
        f"checkin booking_id={booking_id} {lead}Welcome! Your arrival at the family counseling clinic is registered. "
        "Everything is ready — no referral or extra paperwork is needed, and our gynecologist will welcome you shortly in full confidentiality."
        if english else
        f"checkin booking_id={booking_id} {lead}Benvenuta! Il suo arrivo al consultorio è stato registrato correttamente. "
        "È tutto pronto: non serve alcuna ricetta e la nostra ginecologa la accoglierà a breve nella massima riservatezza."
    )
    await c.say(city, citizen, msg, allowed_citizen=citizen)
