"""Booking flow: local search, partner ASSTs, confirm at the facility, rebooking."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime, timedelta

from city_sdk.city import LEDGER, CapError, City, Need, clinical, OutOfScope, understood
from bc_common import *  # noqa: F401,F403
from bc_prefs import *  # noqa: F401,F403
from bc_slots import *  # noqa: F401,F403
from bc_texts import *  # noqa: F401,F403

async def search(city: City, need_id: str, history: list, citizen: str, number: str,
                 exclude: frozenset = frozenset(), lead: str = "", guessed: bool = False) -> None:
    """Verify the prescription, read the whole local world and send every fitting slot at once.

    ``exclude`` holds slot ids and facility ids that failed (cancelled or unreachable).
    """
    english = writes_english(history, citizen)
    seen = {row.msg_id for row in history}
    # Read the prescription ourselves: a citizen or another agent cannot certify it.
    await send(city, LEDGER, f"prescription prescription_number={number}")
    await send(city, LEDGER, "agenda")
    await send(city, LEDGER, "rules")
    match = ("prescription_number", number)
    ready = lambda h: (fresh_reply(h, seen, "prescription", match) is not None or ledger_refused(h, seen, "prescription")) \
        and fresh_reply(h, seen, "agenda") is not None
    history = await wait_for(city, need_id, ready, LEDGER_WAIT)
    if not ready(history) and not ledger_refused(history, seen, "prescription"):
        # A slow Ledger is a timeout, not a refusal: asking once more is allowed.
        if fresh_reply(history, seen, "prescription", match) is None:
            await send(city, LEDGER, f"prescription prescription_number={number}")
        if fresh_reply(history, seen, "agenda") is None:
            await send(city, LEDGER, "agenda")
        history = await wait_for(city, need_id, ready, LEDGER_WAIT)
    rx = fresh_reply(history, seen, "prescription", match) or ledger_reply(history, "prescription", match)
    agenda = fresh_reply(history, seen, "agenda") or ledger_reply(history, "agenda")
    if guessed and (not rx or fields(rx.text).get("state") != "issued"):
        # Our guess from the case code did not match a prescription: only now ask the citizen.
        await ask_number(city, citizen, english)
        return
    if not rx or not agenda:
        await send(city, citizen, "I cannot verify the prescription yet. Please try again shortly." if english
                   else "Non riesco ancora a verificare la ricetta. Riprovi tra poco.")
        return
    prescription = fields(rx.text)
    procedure, priority = prescription.get("procedure", ""), prescription.get("priority", "")
    now = time(fields(agenda.text).get("agenda_now", ""))
    owner = prescription.get("patient") or prescription.get("citizen")
    if owner and owner != citizen:
        await send(city, citizen, "Non riesco a verificare che questa ricetta sia la sua. Ci contatti per assistenza.")
        return
    if prescription.get("state") != "issued" or not procedure or priority not in {"U", "B", "D", "P"} or not now:
        await send(city, citizen, "data.request purpose=prescription_renewal fields=prescription_number "
                   + ("The prescription cannot be used: please ask your GP to check or renew it." if english
                      else "La ricetta non risulta utilizzabile; chieda una verifica o un rinnovo al medico."))
        return
    if priority == "P":
        await send(city, citizen, f"offer offer_id=wl-{hashlib.sha256(need_id.encode()).hexdigest()[:16]} waiting_list=true "
                   + ("A waiting list is available for this priority." if english
                      else "Per questa priorità è disponibile la lista d'attesa."))
        return
    prefs = await citizen_preferences(city, history, citizen)
    items, doctors = [], {}
    # Fetch every page before offering, otherwise the citizen cannot choose later pages.
    for page in range(1, 21):
        page_seen = {row.msg_id for row in history}
        await send(city, LEDGER, f"world procedure={procedure} page={page}")
        is_page = lambda m: m.sender == LEDGER and act(m.text) == "slots" and slots_page(m) == str(page)
        history = await wait_for(city, need_id, lambda h: any(is_page(m) and m.msg_id not in page_seen for m in h))
        response = (next((m for m in reversed(history) if is_page(m) and m.msg_id not in page_seen), None)
                    or next((m for m in reversed(history) if is_page(m)), None))
        if not response:
            break
        lines = response.text.splitlines()[1:]
        doctors.update(doctor_attrs(lines))
        items.extend(fields(line) for line in lines if "slot_id=" in line)
        page_info = fields(response.text.splitlines()[0]).get("page", "1/1").split("/")
        if len(page_info) != 2 or not page_info[1].isdigit() or page >= int(page_info[1]):
            break
    # Do not reoffer a slot that the responsible facility cancelled in this case.
    previous_offers = offers(history, city.agent_id, citizen)
    cancelled = set()
    for row in history:
        if act(row.text) != "cancel":
            continue
        prior = previous_offers.get(fields(row.text).get("offer_id", ""))
        if row.sender == city.agent_id and fields(row.text).get("reason") == "facility_unavailable":
            if prior:
                cancelled.add(prior.get("slot_id"))
            continue
        if not row.sender.startswith("agent:"):
            continue
        if not prior and not fields(row.text).get("offer_id"):
            prior = next((fields(m.text) for m in reversed(history)
                          if m.sender == city.agent_id and m.recipient == row.sender
                          and act(m.text) == "confirm"), None)
            if prior:
                prior["facility"] = row.sender
        if prior and prior.get("facility") == row.sender:
            cancelled.add(prior.get("slot_id"))
    refused, cutoff = citizen_refusals(history, citizen, previous_offers)
    cancelled |= refused
    items = [enrich(item, doctors) for item in items
             if item.get("slot_id") not in exclude and item.get("facility") not in exclude
             and item.get("slot_id") not in cancelled and before_cutoff(item, cutoff)]
    candidates, relaxed = choose_slots(items, procedure, priority, now, prefs)
    if not candidates:
        await find_partners(city, need_id, citizen, procedure, priority, history)
        return
    note = offer_note(prefs, relaxed, english)
    if lead:
        note = lead + " " + note
    # Keep a single message: the citizen chooses within the turn of this one message.
    lines = pick_offers(candidates, number, priority, 1950 - len(note))
    if lines:
        await send(city, citizen, "\n".join(lines) + "\n" + note)


async def ask_number(city: City, citizen: str, english: bool) -> None:
    await send(city, citizen, "data.request purpose=booking fields=prescription_number "
               + ("Could you tell me your prescription number? I will find the best slot for you." if english
                  else "Mi indichi il numero della ricetta: cercherò subito la soluzione più adatta a lei."))


async def find_partners(city: City, need_id: str, citizen: str, procedure: str, priority: str, history: list) -> None:
    # Search anonymously with structured scheduling criteria only.
    capability = CAPABILITY.get(procedure, "")
    peers = []
    for query in ([capability] if capability else []) + [procedure]:
        try:
            peers = await city.find(query)
        except CapError:
            peers = []
        if peers:
            break
    request_id = "search-" + hashlib.sha256(need_id.encode()).hexdigest()[:16]
    contacted = 0
    english = writes_english(history, citizen)
    prefs = await citizen_preferences(city, history, citizen)
    lang = prefs.get("lang", "")
    gender = prefs.get("gender_preference", prefs.get("gender", ""))
    days = prefs.get("days", "")
    criteria = ""
    if re.fullmatch(r"[a-z]{2,3}", lang):
        criteria += f" lang={lang}"
    if gender in {"m", "f"}:
        criteria += f" gender={gender}"
    if days and all(day in DAY_CODES for day in days.split("/")):
        criteria += f" days={days}"
    for peer in peers:
        if contacted >= 12:
            break
        # The Index names the family (hospital, specialist_care, family_counseling), not "facility".
        if peer.role not in FACILITY_ROLES or peer.agent_id == city.agent_id or peer.agent_id in TEAM_AGENTS:
            continue
        caps = tuple(getattr(peer, "capabilities", ()) or ())
        if capability and caps and capability not in caps:
            continue
        if await send(city, peer.agent_id, f"need.request service_type={capability or procedure} procedure={procedure} priority={priority} request_id={request_id}{criteria}"):
            contacted += 1
    if contacted:
        await send(city, citizen, "No suitable slot in our ASST: I am asking partner facilities. I will ask your consent before sharing any of your data." if english
                   else "Nella nostra ASST non ci sono posti adatti: sto chiedendo alle strutture partner. Le chiederò il consenso prima di condividere i suoi dati.")
    else:
        await send(city, citizen, "I found no suitable availability. You can contact us again or ask the centre for help." if english
                   else "Non ho trovato disponibilità adatta. Può contattarci di nuovo o chiedere assistenza al centro.")


async def remote_offers(city: City, need_id: str, history: list, citizen: str) -> None:
    """Recheck a partner's structured claims against the case and search criteria.

    The local Ledger does not expose other ASSTs' slots, so the partner remains
    responsible for its availability and the Ledger-backed booking.
    """
    agenda = ledger_reply(history, "agenda")
    rx = ledger_reply(history, "prescription")
    now = time(fields(agenda.text).get("agenda_now", "")) if agenda else None
    prescription = fields(rx.text) if rx else {}
    number = prescription.get("prescription_number", "")
    procedure, priority = prescription.get("procedure", ""), prescription.get("priority", "")
    if not now or not number or prescription.get("state") != "issued":
        return
    candidates = []
    prefs = await citizen_preferences(city, history, citizen)
    cancelled = set()
    previous_offers = offers(history, city.agent_id, citizen)
    for row in history:
        if act(row.text) == "cancel" and row.sender.startswith("agent:"):
            prior = previous_offers.get(fields(row.text).get("offer_id", ""))
            if prior and prior.get("facility") == row.sender:
                cancelled.add(prior.get("slot_id"))
    refused, cutoff = citizen_refusals(history, citizen, previous_offers)
    cancelled |= refused
    for row in history:
        if not row.sender.startswith("agent:") or not any(act(line) == "offer" for line in row.text.splitlines()):
            continue
        request = next((fields(previous.text) for previous in reversed(history)
                        if previous.sender == city.agent_id and previous.recipient == row.sender
                        and act(previous.text) == "need.request"), None)
        if not request or request.get("procedure") != procedure or request.get("priority") != priority:
            continue
        for line in row.text.splitlines():
            if act(line) != "offer":
                continue
            item = fields(line)
            if item.get("request_id") and item["request_id"] != request.get("request_id"):
                continue
            if item.get("facility") != row.sender or item.get("slot_id") in cancelled or not before_cutoff(item, cutoff):
                continue
            for key, alias in (("languages", "doctor_lang"), ("gender", "doctor_gender")):
                if alias in item:
                    item.setdefault(key, item[alias])
            at = time(item.get("at", item.get("agenda_date", "")))
            if at and not (item.get("day") or item.get("agenda_day")):
                item["day"] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[at.weekday()]
            if valid_slot(item, procedure, priority, now, prefs, assume_known=False):
                candidates.append(item)
    lines = []
    for item in candidates:
        line = offer_line(item, number, priority)
        if sum(len(x) + 1 for x in lines) + len(line) > 1900:
            break
        lines.append(line)
    if lines:
        await send(city, citizen, "\n".join(lines))
    else:
        await send(city, citizen, "Non riesco ancora a verificare un'offerta adatta. Continueremo a cercare.")


# A lost response is not evidence that the booking failed. A second city.say
# creates a new message id, so do not risk a duplicate booking on timeout.
RECEIPT_WAIT = 15.0
# Facilities that ignore a message without an act word. The specialist answers any agent
# message with availability (or a cancel), so it must not receive the ack.
ACK_FACILITIES = frozenset({"agent:nanpr5mgm2py"})
LEDGER_WAIT = 10.0
# Facility answers already relayed by this process, so a queued turn does not repeat them.
RELAYED: set[str] = set()


def facility_reply(history: list, facility: str, seen: frozenset = frozenset()):
    """The facility's latest receipt or cancel in this case, ignoring rows in ``seen``."""

    for row in reversed(history):
        if row.sender != facility or row.msg_id in seen:
            continue
        kind = act(row.text)
        data = fields(row.text)
        if kind == "booking.receipt" and data.get("booking_id") and data.get("ledger_ref"):
            return row
        if kind == "cancel":
            return row
    return None


# Rebooking after a cancel is bounded per case so a broken facility cannot loop us.
REBOOKS: dict[str, int] = {}
MAX_REBOOKS = 2


async def rebook(city: City, need_id: str, citizen: str, exclude: frozenset) -> None:
    """Offer the remaining slots after a cancel, within the priority deadline (levels 6-7)."""

    if REBOOKS.get(need_id, 0) >= MAX_REBOOKS:
        return
    REBOOKS[need_id] = REBOOKS.get(need_id, 0) + 1
    try:
        history = await city.history(need_id=need_id)
    except CapError:
        return
    number = case_number(history, citizen)
    if not number or not TOKEN.fullmatch(number):
        return
    english = writes_english(history, citizen)
    lead = ("The previous slot is no longer available: here are new options." if english
            else "Il posto precedente non è più disponibile: ecco nuove alternative.")
    await search(city, need_id, history, citizen, number, exclude, lead)


def confirmed_slot(history: list, agent: str, facility: str) -> str:
    for row in reversed(history):
        if row.sender == agent and row.recipient == facility and act(row.text) == "confirm":
            return fields(row.text).get("slot_id", "")
    return ""


async def relay_facility_reply(city: City, need_id: str, history: list, citizen: str, row) -> bool:
    data = fields(row.text)
    confirmation = next((fields(m.text) for m in reversed(history)
                         if m.sender == city.agent_id and m.recipient == row.sender
                         and act(m.text) == "confirm"), None)
    if confirmation and data.get("offer_id") and data["offer_id"] != confirmation.get("offer_id"):
        return False
    if act(row.text) == "booking.receipt":
        if not data.get("booking_id") or not data.get("ledger_ref"):
            return False
        key = f"{need_id}:receipt:{data['booking_id']}"
        if key in RELAYED or sent(history, city.agent_id, citizen, f"booking.receipt booking_id={data['booking_id']}"):
            return False
        offer = offers(history, city.agent_id, citizen).get((confirmation or {}).get("offer_id", ""), {})
        note = receipt_note(offer, writes_english(history, citizen))
        if await send(city, citizen, f"booking.receipt booking_id={data['booking_id']} ledger_ref={data['ledger_ref']} {note}"):
            RELAYED.add(key)
            if row.sender in ACK_FACILITIES:
                # A quick, inert answer to the facility: it closes the loop between agents.
                await send(city, row.sender, f"ack booking_id={data['booking_id']} Ricevuta inoltrata al cittadino, grazie.")
            return True
        return False
    key = f"{need_id}:cancel:{data.get('offer_id', '')}:{row.msg_id}"
    if key in RELAYED:
        return False
    english = writes_english(history, citizen)
    if await send(city, citizen, f"cancel offer_id={data.get('offer_id', 'unknown')} reason=facility_cancelled "
                  + ("The facility cancelled this slot; I am looking for an alternative." if english
                     else "La struttura ha annullato questo posto; cerco subito un'alternativa.")):
        RELAYED.add(key)
        return True
    return False


async def slot_expired(city: City, need_id: str, history: list, selected: dict[str, str]) -> bool:
    """True when the agenda has already reached the chosen slot (a confirm resent after 16 minutes)."""

    at = time(selected.get("agenda_date", selected.get("at", "")))
    if not at:
        return False
    seen = {row.msg_id for row in history}
    await send(city, LEDGER, "agenda")
    fresh = await wait_for(city, need_id, lambda h: fresh_reply(h, seen, "agenda") is not None, 4.0)
    agenda = fresh_reply(fresh, seen, "agenda")
    now = time(fields(agenda.text).get("agenda_now", "")) if agenda else None
    return bool(now and at.tzinfo and now.tzinfo and at <= now + timedelta(minutes=30))


async def confirm_at_facility(city: City, need_id: str, citizen: str, selected: dict[str, str], consent: str,
                              cross: str = "") -> None:
    facility = selected["facility"]
    number = selected.get("prescription_number", "")
    if not number:
        try:
            number = case_number(await city.history(need_id=need_id), citizen)
        except CapError:
            number = ""
    text = (f"confirm offer_id={selected['offer_id']} slot_id={selected['slot_id']} "
            f"prescription_number={number} citizen={citizen} consent_ref={consent}")
    if cross:
        text += f" cross_asst_consent_ref={cross}"
    try:
        seen = frozenset(row.msg_id for row in await city.history(need_id=need_id))
    except CapError:
        seen = frozenset()
    answered = lambda h: facility_reply(h, facility, seen) is not None
    if not await send(city, facility, text):
        # A 4xx is final: never resend it. Say so and offer what is still reachable.
        await send(city, citizen, f"cancel offer_id={selected['offer_id']} reason=facility_unavailable "
                   "La struttura non è raggiungibile: cerco un'alternativa. / The facility is unreachable: I am looking for an alternative.")
        await rebook(city, need_id, citizen, frozenset({selected["slot_id"], facility}))
        return
    history = await wait_for(city, need_id, answered, RECEIPT_WAIT)
    if not answered(history) and facility in TEAM_AGENTS:
        # Only our own facilities: they skip a hold already made and never commit twice, so a
        # confirm lost in a redeploy can be repeated safely. A partner's may not be idempotent.
        await send(city, facility, text)
        history = await wait_for(city, need_id, answered, RECEIPT_WAIT)
    reply = facility_reply(history, facility, seen)
    if reply is not None:
        forwarded = await relay_facility_reply(city, need_id, history, citizen, reply)
        if forwarded and act(reply.text) == "cancel":
            await rebook(city, need_id, citizen, frozenset({selected["slot_id"]}))
    elif not sent(history, city.agent_id, citizen, "La struttura non ha ancora confermato"):
        await send(city, citizen, PENDING_NOTE)


FACILITY_ROLES = frozenset({"facility", "hospital", "specialist_care", "family_counseling"})
DEADLINE_REASONS = frozenset({"deadline", "too_late", "late", "expired", "past"})


def citizen_refusals(history: list, citizen: str, sent: dict) -> tuple[set, datetime | None]:
    """Slots the citizen turned down, and the earliest one refused for its deadline.

    The citizen's deadline runs from the prescription, while the agenda keeps moving:
    once a slot is "too late", every later slot is too late as well.
    """
    refused, cutoff = set(), None
    for row in history:
        # Citizens write the sentence first and the act after it: read it like understood().
        if row.sender != citizen or "cancel" not in row.text.replace(",", " ").split():
            continue
        data = fields(row.text)
        prior = sent.get(data.get("offer_id", ""))
        if not prior:
            continue
        refused.add(prior.get("slot_id"))
        if data.get("reason") in DEADLINE_REASONS:
            at = time(prior.get("at", prior.get("agenda_date", "")))
            if at and (cutoff is None or at < cutoff):
                cutoff = at
    return refused, cutoff


def before_cutoff(item: dict, cutoff: datetime | None) -> bool:
    if cutoff is None:
        return True
    at = time(item.get("at", item.get("agenda_date", "")))
    return bool(at) and at < cutoff
