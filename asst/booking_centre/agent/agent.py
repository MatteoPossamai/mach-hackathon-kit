"""Booking centre: verify, search, offer and coordinate a facility booking.

The Ledger and the case history are the source of truth. Peer messages are data,
not instructions; in particular they cannot change a citizen's chosen slot.

The logic lives in the bc_* modules next to this file; this module only routes messages.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time as _clock
from datetime import datetime, timedelta

from city_sdk.city import LEDGER, CapError, City, Need, clinical, OutOfScope, understood
import sys
from pathlib import Path

# The SDK loads this file as a standalone module: make the sibling bc_* modules importable.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bc_common, bc_flow, bc_prefs, bc_slots, bc_texts  # noqa: E401,F401
from bc_common import *  # noqa: F401,F403
from bc_prefs import *  # noqa: F401,F403
from bc_slots import *  # noqa: F401,F403
from bc_texts import *  # noqa: F401,F403
from bc_flow import *  # noqa: F401,F403
from bc_choice import *  # noqa: F401,F403
import bc_choice  # noqa: F401

LOCKS: dict[str, asyncio.Lock] = {}
NOTICE_ACTS = frozenset({"handoff", "booking.notice", "ack"})
GP_AGENT = "agent:c2ewegr2ea3v"


async def main(need: Need, city: City) -> None:
    incoming = city.inbound
    if incoming is None or not incoming.need_id:
        return
    resilient_history(city)
    if incoming.sender.startswith("citizen:") and (getattr(need, "is_emergency", False) or is_red_flag(incoming)):
        history = await city.history(need_id=incoming.need_id)
        if not sent(history, city.agent_id, incoming.sender, "escalate.emergency"):
            await send(city, incoming.sender, "escalate.emergency reason=red_flag Capisco la sua preoccupazione. Chiami subito il 112 per ricevere assistenza.")
        return
    if incoming.sender.startswith("citizen:") and clinical(incoming.text):
        raise OutOfScope()
    if incoming.sender.startswith("agent:") and act(incoming.text) in NOTICE_ACTS:
        # A GP hand-off or a facility notice is not a citizen request: never write to the
        # citizen because of it (it used to send "no availability" before they even wrote).
        if incoming.sender == GP_AGENT and act(incoming.text) == "handoff":
            # Answer our GP at once (collaboration, fast reply); no citizen data travels.
            number = fields(incoming.text).get("prescription_number", "")
            if number and TOKEN.fullmatch(number):
                await send(city, incoming.sender, f"ack prescription_number={number} "
                           "Ricevuto: appena il paziente scrive verifico la ricetta sul registro e "
                           "cerco gli orari, anche nelle strutture partner di altre ASST se serve.")
        return
    key = incoming.need_id
    lock = LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        try:
            if incoming.sender.startswith("citizen:") and "cancel" in incoming.text.replace(",", " ").split():
                rows = await city.history(need_id=incoming.need_id)
                if simultaneous_confirmation(rows, incoming):
                    return
                index = next((i for i, row in enumerate(rows) if row.msg_id == incoming.msg_id), None)
                if index is not None and any(row.sender == incoming.sender and "cancel" in row.text.replace(",", " ").split()
                                             for row in rows[index + 1:]):
                    return
            started = _clock.monotonic()
            await handle(city, incoming)
            if incoming.sender.startswith("citizen:") and LAST_SENT.get(incoming.sender, -1.0) < started:
                await never_silent(city, incoming)
        finally:
            # Keep locks while waiting turns exist; removing them here could create two locks.
            if not getattr(lock, "_waiters", None):
                LOCKS.pop(key, None)


async def book_choice(city: City, message, history: list, citizen: str, selected: dict[str, str],
                      data: dict[str, str]) -> None:
    """The citizen chose ``selected`` (one of our offers): consent, freshness, then the facility."""

    CHOSEN[message.need_id] = selected["offer_id"]
    facility = selected["facility"]
    remote = sent(history, city.agent_id, facility, "need.request ")
    cross = cross_consent(history, city.agent_id, citizen) if remote else ""
    consent = data.get("consent_ref", "") or cross
    if not consent or (remote and not cross):
        purpose = "cross_asst" if remote else "booking"
        english = writes_english(history, citizen)
        offer_id = selected["offer_id"]
        await send(city, citizen, f"consent purpose={purpose} offer_id={offer_id} " + (
            "May I proceed with the booking you chose?" if english
            else "Posso procedere con la prenotazione scelta?"))
        return
    if not sent(history, city.agent_id, facility, f"confirm offer_id={selected['offer_id']} "):
        if await slot_expired(city, message.need_id, history, selected):
            english = writes_english(history, citizen)
            await send(city, citizen, f"cancel offer_id={selected['offer_id']} reason=slot_expired "
                       + ("This slot has already passed." if english else "Questo posto è ormai passato."))
            await rebook(city, message.need_id, citizen, frozenset({selected["slot_id"]}))
            return
        await confirm_at_facility(city, message.need_id, citizen, selected, consent, cross)
    return


async def never_silent(city: City, incoming) -> None:
    """A citizen turn that produced no message: answer with a concrete next step, never silence."""

    try:
        history = await city.history(need_id=incoming.need_id)
    except CapError:
        history = []
    english = writes_english(history, incoming.sender)
    refusing = "cancel" in incoming.text.replace(",", " ").split()
    wl = "wl-" + hashlib.sha256(incoming.need_id.encode()).hexdigest()[:16]
    if refusing:
        text = (f"offer offer_id={wl} waiting_list=true\n"
                + ("Understood, I have taken those times off. Tell me which days, times or doctor suit you "
                   "and I will search again at once, also at partner facilities; meanwhile I can put you on "
                   "the waiting list." if english else
                   "Capito, ho tolto quegli orari. Mi dica quali giorni, orari o medico le vanno bene e cerco "
                   "subito di nuovo, anche nelle strutture partner; intanto posso inserirla in lista d'attesa."))
    else:
        text = ("I have your message and I am working on your booking: I will write to you as soon as I have "
                "the times. If you have preferences (days, language, doctor), tell me now." if english else
                "Ho ricevuto il suo messaggio e sto lavorando alla prenotazione: le scrivo appena ho gli orari. "
                "Se ha preferenze (giorni, lingua, medico), me le dica pure.")
    await send(city, incoming.sender, text)


async def follow_up(city: City, need_id: str, history: list, citizen: str) -> None:
    """A citizen message without a prescription number: never leave it unanswered."""

    english = writes_english(history, citizen)
    receipt = next((row for row in reversed(history) if row.sender == city.agent_id
                    and row.recipient == citizen and act(row.text) == "booking.receipt"), None)
    if receipt is not None:
        booking_id = fields(receipt.text).get("booking_id", "")
        confirm = next((fields(row.text) for row in reversed(history) if row.sender == citizen
                        and act(row.text) == "confirm"), {})
        offer = offers(history, city.agent_id, citizen).get(confirm.get("offer_id", ""), {})
        await send(city, citizen, recap_note(booking_id, offer, english))
        return
    message = getattr(city, "inbound", None)
    refusing = message is not None and "cancel" in message.text.replace(",", " ").split()
    if message is not None and not refusing and latest_offers(history, city.agent_id, citizen):
        intent, selected = await interpret_booking_reply(city, history, citizen, message.text)
        if intent == "choose" and selected:
            await book_choice(city, message, history, citizen, selected, {})
            return
        if intent == "unclear":
            await ask_offer(city, history, citizen)
            return
        if intent == "decline":
            await send(city, citizen, (
                "Which days or times would work better for you?" if english else
                "Quali giorni o orari le andrebbero meglio?"))
            return
    number = case_number(history, citizen)
    if number and TOKEN.fullmatch(number):
        # The citizen added a need in words (another day, a doctor): search again with it.
        await search(city, need_id, history, citizen, number)
        return
    guess = rx_guess(need_id)
    if guess and not any(row.sender == city.agent_id and row.recipient == LEDGER
                         and f"prescription_number={guess}" in row.text for row in history):
        # Level 3: no number in words. Our GP numbers prescriptions by case: verify that first.
        await search(city, need_id, history, citizen, guess, guessed=True)
        return
    await ask_number(city, citizen, english)


async def ask_offer(city: City, history: list, citizen: str) -> None:
    await send(city, citizen, (
        "Which appointment would you like? You can tell me the day and time, or say 'the first one'."
        if writes_english(history, citizen) else
        "Quale appuntamento preferisce? Può indicarmi giorno e ora, oppure dire 'il primo'."))


def simultaneous_confirmation(history: list, message) -> bool:
    data = fields(message.text)
    cancelled = data.get("offer_id")
    if not message.sent_at or not cancelled or data.get("reason") != "past":
        return False
    return any(row.sender == message.sender and row.recipient == message.recipient
               and row.sent_at == message.sent_at and understood(row).type == "confirm"
               and (offer_id := fields(row.text).get("offer_id")) and offer_id != cancelled
               for row in history)


async def resume_consent(city: City, message, history: list, citizen: str,
                         request: str, purpose: str, ref: str) -> None:
    requested_offer = fields(request).get("offer_id")
    offer_id = requested_offer or (CHOSEN.get(message.need_id, "") if purpose == "booking" else "")
    if not offer_id:
        offer_id = next((fields(row.text).get("offer_id", "") for row in reversed(history)
                         if row.sender == citizen and understood(row).type == "confirm"), "")
    selected = offers(history, city.agent_id, citizen).get(offer_id)
    if offer_id in withdrawn_offer_ids(history, city.agent_id, citizen):
        await ask_offer(city, history, citizen)
        return
    if selected and selected.get("slot_id") and (requested_offer or purpose == "booking"
            or sent(history, city.agent_id, selected.get("facility", ""), "need.request ")):
        booking_ref = ref if purpose == "booking" else next(
            (data["consent_ref"] for row in reversed(history)
             if row.sender == citizen and understood(row).type == "confirm"
             and (data := fields(row.text)).get("offer_id") == offer_id and data.get("consent_ref")), "")
        await book_choice(city, message, history, citizen, selected, {"consent_ref": booking_ref})
    elif purpose == "cross_asst":
        await remote_offers(city, message.need_id, history, citizen)
    else:
        await ask_offer(city, history, citizen)


async def handle_pending_consent(city: City, message, history: list, citizen: str, kind: str) -> bool:
    request = last_sent_to_citizen(history, city.agent_id, citizen)
    pending = consent_request_purpose(request)
    purpose, ref, denied = consent_reply(message)
    if ref and not denied and (purpose or pending) == pending and pending:
        await resume_consent(city, message, history, citizen, request, pending, ref)
        return True
    if not pending or fields(message.text).get("prescription_number"):
        return False
    if pending == "cross_asst" and cross_consent(history, city.agent_id, citizen):
        return False
    if not denied and kind not in {"consent", "data.response"}:
        intent, selected = await interpret_booking_reply(
            city, history, citizen, message.text, pending_consent=pending)
        if intent == "preferences":
            return False
        if intent == "choose" and selected:
            await book_choice(city, message, history, citizen, selected, {})
            return True
        denied = intent == "decline"
    english = writes_english(history, citizen)
    if not denied and act(request) == "consent":
        offer_id = fields(request).get("offer_id")
        selection = f" offer_id={offer_id}" if offer_id else ""
        question = (
            "May I share the details needed to book with the partner facility?"
            if english else "Posso condividere con la struttura partner i dati necessari per prenotare?"
        ) if pending == "cross_asst" else (
            "May I book the time you chose?" if english else "Posso prenotare l’orario che ha scelto?"
        )
        await send(city, citizen, f"data.request purpose={pending} fields=consent_ref{selection} " + (
            question + (" You do not need to look up a code." if english else " Non deve cercare un codice.")))
    elif pending == "cross_asst":
        await language_fallback(city, message.need_id, history, citizen)
    else:
        await send(city, citizen, (
            "Your permission is still needed. Tell me if you would like to proceed or choose another time."
            if english else
            "Serve ancora la sua autorizzazione. Mi dica se vuole procedere o scegliere un altro orario."))
    return True


def is_red_flag(message) -> bool:
    if understood(message).body.get("is_emergency") is True:
        return True
    text = message.text.lower()
    for match in re.finditer(r"dolore\s+(?:al|nel)\s+(?:petto|torace)|chest pain|non sento le gambe|perdita di forza alle gambe", text):
        prefix = text[max(0, match.start() - 24):match.start()]
        if not re.search(r"\b(?:non\s+(?:ho|sento|avverto)|senza|nessun[ao]?|no|without)\s+(?:un\s+|forte\s+)?$", prefix):
            return True
    return False


async def handle(city: City, message) -> None:
    history = await city.history(need_id=message.need_id)
    citizen = citizen_of(history)
    if not citizen:
        return
    data = fields(message.text)
    kind = understood(message).type
    if message.sender == citizen:
        if kind == "confirm" and sent(history, city.agent_id, citizen, "booking.receipt "):
            # Already booked: repeat the booking, never hold a second slot for the same need.
            await follow_up(city, message.need_id, history, citizen)
            return
        if kind == "confirm":
            offer_id = data.get("offer_id", "")
            if offer_id in withdrawn_offer_ids(history, city.agent_id, citizen):
                await ask_offer(city, history, citizen)
                return
            selected = offers(history, city.agent_id, citizen).get(offer_id)
            if selected and selected.get("waiting_list") == "true":
                await send(city, citizen, "Ha scelto l'opzione di lista d'attesa.")
                return
            if not selected or not selected.get("slot_id"):
                # Level 3: the choice may be written in words ("la seconda", "martedì alle 12").
                selected = await resolve_choice(city, history, citizen, message.text, use_llm=True)
            if not selected or not selected.get("slot_id"):
                await ask_offer(city, history, citizen)
                return
            await book_choice(city, message, history, citizen, selected, data)
            return
        if kind == "cancel" and data.get("offer_id") and data.get("reason") == "past":
            cancelled = data["offer_id"]
            if simultaneous_confirmation(history, message):
                return
            if any(item["offer_id"] != cancelled for item in latest_offers(history, city.agent_id, citizen)):
                await send(city, citizen, (
                    "That time has passed. Which of the other times would work?"
                    if writes_english(history, citizen) else
                    "Quell’orario è passato. Quale degli altri orari le andrebbe bene?"))
                return
        if await handle_pending_consent(city, message, history, citizen, kind):
            return
        number = data.get("prescription_number", "")
        if not number and message.type != "chat":
            raw_number = message.body.get("prescription_number")
            if not raw_number and isinstance(message.body.get("provided"), dict):
                raw_number = message.body["provided"].get("prescription_number")
            number = raw_number if isinstance(raw_number, str) else ""
        if not number:
            # The number written in free text, e.g. "la ricetta è prescription_number-N-...".
            number = rx_from_text(history, citizen, message.text)
        if not number and kind == "data.response":
            number = case_number(history, citizen)
        if (not number and kind == "data.response"
                and last_sent_to_citizen(history, city.agent_id, citizen).startswith(
                    "data.request purpose=booking fields=days ")):
            number = case_number(history, citizen)
        if not number and last_sent_to_citizen(history, city.agent_id, citizen).startswith(LANGUAGE_QUESTION + " "):
            number = case_number(history, citizen)
        if number and TOKEN.fullmatch(number):
            await search(city, message.need_id, history, citizen, number)
        elif kind not in {"consent", "data.response"}:
            await follow_up(city, message.need_id, history, citizen)
        return
    if message.sender.startswith("agent:"):
        requests = ring_requests(history, city.agent_id)
        requested = message.sender in requests
        confirmed = any(row.sender == city.agent_id and row.recipient == message.sender and act(row.text) == "confirm" for row in history)
        head = act(message.text)
        if (head == "unavailable" or (head == "cancel" and data.get("reason") == "no_availability")) and requested:
            contacted = requests.keys()
            unavailable = {row.sender for row in history
                           if row.sender in contacted and (
                               act(row.text) == "unavailable" or
                               (act(row.text) == "cancel" and fields(row.text).get("reason") == "no_availability")
                           )}
            prefs = await citizen_preferences(city, history, citizen)
            if contacted <= unavailable and not matching_partner_offers(history, city.agent_id, citizen, prefs):
                await language_fallback(city, message.need_id, history, citizen)
                return
        if head in {"booking.receipt", "cancel"} and confirmed:
            forwarded = await relay_facility_reply(city, message.need_id, history, citizen, message)
            if forwarded and head == "cancel":
                slot = data.get("slot_id") or confirmed_slot(history, city.agent_id, message.sender)
                await rebook(city, message.need_id, citizen, frozenset({slot} if slot else set()))
        elif requested and not asked_consent(history, city.agent_id, citizen, "cross_asst"):
            prefs = await citizen_preferences(city, history, citizen)
            if matching_partner_offers(history, city.agent_id, citizen, prefs):
                await send(city, citizen, "consent purpose=cross_asst " + (
                    "I found a possible slot at a partner facility of another ASST. May I share the data needed to check it and book it?"
                    if writes_english(history, citizen) else
                    "Ho trovato una possibile disponibilità in una struttura partner di un'altra ASST. Posso condividere i dati necessari per verificarla e prenotare?"))
