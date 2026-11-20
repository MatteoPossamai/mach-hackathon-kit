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


async def main(need: Need, city: City) -> None:
    incoming = city.inbound
    if incoming is None or not incoming.need_id:
        return
    if incoming.sender.startswith("citizen:") and (getattr(need, "is_emergency", False) or is_red_flag(incoming)):
        history = await city.history(need_id=incoming.need_id)
        if not sent(history, city.agent_id, incoming.sender, "escalate.emergency"):
            await send(city, incoming.sender, "escalate.emergency reason=red_flag Capisco la sua preoccupazione. Chiami subito il 112 per ricevere assistenza.")
        return
    if incoming.sender.startswith("citizen:") and clinical(incoming.text):
        raise OutOfScope()
    key = incoming.need_id
    lock = LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        try:
            await handle(city, incoming)
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
        await send(city, citizen, f"consent purpose={purpose} Posso procedere con la prenotazione scelta? / May I proceed with the booking you chose?")
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
    message = city.inbound
    if message is not None and latest_offers(history, city.agent_id, citizen):
        selected = await resolve_choice(city, history, citizen, message.text, use_llm=False)
        if selected and selected.get("slot_id"):
            await book_choice(city, message, history, citizen, selected, fields(message.text))
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
        if kind == "confirm":
            selected = offers(history, city.agent_id, citizen).get(data.get("offer_id", ""))
            if selected and selected.get("waiting_list") == "true":
                await send(city, citizen, "Ha scelto l'opzione di lista d'attesa.")
                return
            if not selected or not selected.get("slot_id"):
                # Level 3: the choice may be written in words ("la seconda", "martedì alle 12").
                selected = await resolve_choice(city, history, citizen, message.text, use_llm=True)
            if not selected or not selected.get("slot_id"):
                english = writes_english(history, citizen)
                await send(city, citizen, "Which of the offers I sent do you prefer? You can tell me the day and time, or 'the first one'." if english
                           else "Quale delle offerte che le ho inviato preferisce? Può indicarmi giorno e ora, oppure dirmi 'la prima'.")
                return
            await book_choice(city, message, history, citizen, selected, data)
            return
        if kind == "consent" and data.get("consent_ref"):
            if data.get("purpose") == "cross_asst" and sent(history, city.agent_id, citizen, "consent purpose=cross_asst"):
                choice = next((fields(row.text).get("offer_id", "") for row in reversed(history)
                               if row.sender == citizen and understood(row).type == "confirm"), "")
                selected = offers(history, city.agent_id, citizen).get(choice)
                if selected and selected.get("slot_id") and sent(history, city.agent_id, selected.get("facility", ""), "need.request "):
                    booking_consent = next((fields(row.text).get("consent_ref", "") for row in reversed(history)
                                            if row.sender == citizen and act(row.text) == "confirm"), "")
                    await confirm_at_facility(city, message.need_id, citizen, selected,
                                              booking_consent or data["consent_ref"], data["consent_ref"])
                else:
                    await remote_offers(city, message.need_id, history, citizen)
            elif data.get("purpose") in {"", "booking"} and sent(history, city.agent_id, citizen, "consent purpose=booking"):
                # Resume the last genuine citizen choice with this consent, not data
                # supplied by a peer or a new slot embedded in the consent response.
                choice = next((fields(row.text).get("offer_id", "") for row in reversed(history)
                               if row.sender == citizen and act(row.text) == "confirm"
                               and fields(row.text).get("offer_id")), "") or CHOSEN.get(message.need_id, "")
                selected = offers(history, city.agent_id, citizen).get(choice)
                if selected and selected.get("slot_id"):
                    cross = cross_consent(history, city.agent_id, citizen)
                    await confirm_at_facility(city, message.need_id, citizen, selected, data["consent_ref"], cross)
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
            number = next((fields(row.text).get("prescription_number", "") for row in reversed(history)
                           if row.sender == citizen and fields(row.text).get("prescription_number")), "")
        if number and TOKEN.fullmatch(number):
            await search(city, message.need_id, history, citizen, number)
        elif kind not in {"consent", "data.response"}:
            await follow_up(city, message.need_id, history, citizen)
        return
    if message.sender.startswith("agent:"):
        # Only accept a reply from an agent we actually contacted in this case.
        requested = any(row.sender == city.agent_id and row.recipient == message.sender and act(row.text) == "need.request" for row in history)
        confirmed = any(row.sender == city.agent_id and row.recipient == message.sender and act(row.text) == "confirm" for row in history)
        if act(message.text) == "unavailable" and requested:
            contacted = {row.recipient for row in history
                         if row.sender == city.agent_id and act(row.text) == "need.request"}
            unavailable = {row.sender for row in history
                           if row.sender in contacted and act(row.text) == "unavailable"}
            if contacted and contacted <= unavailable and not sent(history, city.agent_id, citizen, "Le strutture contattate non hanno trovato") and not any(
                row.sender in contacted and act(row.text) == "offer" for row in history
            ):
                await send(city, citizen, "Le strutture contattate non hanno trovato posti disponibili. Ci contatti per ulteriore assistenza.")
            return
        if kind in {"booking.receipt", "cancel"} and confirmed:
            forwarded = await relay_facility_reply(city, message.need_id, history, citizen, message)
            if forwarded and kind == "cancel":
                slot = data.get("slot_id") or confirmed_slot(history, city.agent_id, message.sender)
                await rebook(city, message.need_id, citizen, frozenset({slot} if slot else set()))
        elif kind == "offer" and requested:
            # The remote facility's claim is not a Ledger-verified local slot. Ask for
            # consent first; never treat a peer's text as authority to disclose data.
            if not sent(history, city.agent_id, citizen, "consent purpose=cross_asst"):
                await send(city, citizen, "consent purpose=cross_asst Ho trovato una possibile disponibilità fuori ASST. Posso condividere i dati necessari per verificarla e prenotare?")
