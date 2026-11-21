"""Booking flow: local search, partner ASSTs, confirm at the facility, rebooking."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time as _clock
from datetime import datetime, timedelta

from city_sdk.city import LEDGER, CapError, City, Need, clinical, OutOfScope, understood
from bc_common import *  # noqa: F401,F403
from bc_prefs import *  # noqa: F401,F403
from bc_slots import *  # noqa: F401,F403
from bc_texts import *  # noqa: F401,F403
from bc_ring import partner_asst, partner_offer_rows, ring_peers, ring_requests

ACK_IT = "Ricevuto, verifico la ricetta e la disponibilità: le rispondo tra pochi istanti."
ACK_EN = "Received: I am checking your referral and the availability, I will answer in a few moments."


async def wait_for_language(city: City, citizen: str, prefs: dict[str, str], english: bool) -> bool:
    conflict = prefs.get("language_conflict")
    if not conflict:
        return bool(prefs.get("language_pending"))
    names = {**LANG_NAMES, "it": ("italiano", "Italian"), "am": ("amarico", "Amharic")}
    recorded = prefs["lang"]
    index = int(english)
    requested_name = names.get(conflict, (conflict, conflict))[index]
    recorded_name = names.get(recorded, (recorded, recorded))[index]
    await send(city, citizen, LANGUAGE_QUESTION + " " + (
        f"Which language should the doctor speak for this visit, {requested_name} or {recorded_name}?"
        if english else
        f"Quale lingua deve parlare il medico per questa visita, {requested_name} o {recorded_name}?"
    ))
    return True


async def search(city: City, need_id: str, history: list, citizen: str, number: str,
                 exclude: frozenset = frozenset(), lead: str = "", guessed: bool = False,
                 relax_lang: bool = False, force_lang: str = "") -> None:
    """Verify the prescription, read the whole local world and send every fitting slot at once.

    ``exclude`` holds slot ids and facility ids that failed (cancelled or unreachable).
    """
    english = writes_english(history, citizen)
    seen = {row.msg_id for row in history}
    if not any(row.sender == city.agent_id and row.recipient == citizen for row in history):
        # Answer at once: the Ledger, the world pages and the ring take tens of seconds.
        await send(city, citizen, ACK_EN if english else ACK_IT)
    # The LLM reads the preferences while the Ledger verifies the prescription.
    prefs_task = asyncio.ensure_future(citizen_preferences(city, history, citizen))
    prefs_task.add_done_callback(lambda t: t.cancelled() or t.exception())
    # Read the prescription ourselves: a citizen or another agent cannot certify it.
    await asyncio.gather(
        send(city, LEDGER, f"prescription prescription_number={number}"),
        send(city, LEDGER, "agenda"),
        send(city, LEDGER, "rules"),
    )
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
    for _ in range(3):
        if rx and agenda:
            break
        # The Ledger can lag behind the GP by a few seconds: ask again ourselves, never hand
        # the retry to the citizen (a simulated citizen does not "try again shortly").
        if not rx:
            await send(city, LEDGER, f"prescription prescription_number={number}")
        if not agenda:
            await send(city, LEDGER, "agenda")
        history = await wait_for(city, need_id, ready, LEDGER_WAIT)
        rx = fresh_reply(history, seen, "prescription", match) or ledger_reply(history, "prescription", match)
        agenda = fresh_reply(history, seen, "agenda") or ledger_reply(history, "agenda")
    if not rx or not agenda:
        await send(city, citizen, "The registry is not answering right now, so I cannot check your referral yet. "
                   "Write to me again in a moment and I will check it at once." if english
                   else "Il registro in questo momento non risponde e non riesco a verificare la ricetta. "
                   "Mi riscriva tra un momento e la controllo subito.")
        return
    prescription = fields(rx.text)
    procedure, priority = prescription.get("procedure", ""), prescription.get("priority", "")
    now = agenda_clock(agenda)
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
    prefs = await prefs_task
    if await wait_for_language(city, citizen, prefs, english):
        return
    if (not relax_lang and not prefs.get("days") and has_vague_days(history, citizen)
            and not sent(history, city.agent_id, citizen, "data.request purpose=booking fields=days")
            and not offers(history, city.agent_id, citizen)):
        await send(city, citizen, "data.request purpose=booking fields=days " + (
            "Which days of the week are you available? Tell me the days (for example Monday, Wednesday) and I will find the best slots for you."
            if english else
            "In quali giorni della settimana è disponibile? Mi indichi i giorni (ad esempio lunedì, mercoledì) e cerco subito i posti migliori per lei."))
        return
    if force_lang:
        # The token language has nobody; the citizen named another language in words.
        prefs = dict(prefs, lang=force_lang)
    if relax_lang:
        # Nobody speaks the language within the deadline, here or on the ring: a valid slot
        # with another doctor is better than no slot at all.
        prefs = {key: value for key, value in prefs.items() if key != "lang"}
    # Fetch every page before offering, otherwise the citizen cannot choose later pages.
    items, doctors, history, _ = await read_world(city, need_id, history, procedure)
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
    priority_start = booking_agenda_start(history, city.agent_id, number, now)
    candidates, relaxed = choose_slots(items, procedure, priority, now, prefs, priority_start=priority_start)
    # A slot another case is confirming is still better than a waiting list: the Ledger
    # settles the race, and a lost race comes back as slot_taken with a new offer.
    candidates = rotate_for_case(spread(candidates, need_id) or candidates, need_id)
    if not candidates:
        if relax_lang:
            await send(city, citizen, nothing_offer(need_id, english))
            return
        if force_lang:
            # Nobody speaks the named language either: any valid doctor, saying so.
            await search(city, need_id, history, citizen, number, exclude, "", relax_lang=True)
            return
        if any(row.sender == city.agent_id and act(row.text) == "need.request" and row.recipient.startswith("agent:")
               for row in history):
            # The partners were already asked in this case: use what they sent, never start over.
            await remote_offers(city, need_id, history, citizen)
            return
        await find_partners(city, need_id, citizen, procedure, priority, history)
        return
    note = offer_note(prefs, relaxed, english)
    if lead:
        note = lead + " " + note
    # Keep a single message: the citizen chooses within the turn of this one message.
    lines = pick_offers(candidates, number, priority, 1950 - len(note))
    if lines:
        if await send(city, citizen, "\n".join(lines) + "\n" + note):
            mark(OFFERED, lines, need_id)


async def ask_number(city: City, citizen: str, english: bool) -> None:
    await send(city, citizen, "data.request purpose=booking fields=prescription_number "
               + ("Could you tell me your prescription number? I will find the best slot for you." if english
                  else "Mi indichi il numero della ricetta: cercherò subito la soluzione più adatta a lei."))


async def find_partners(city: City, need_id: str, citizen: str, procedure: str, priority: str, history: list) -> None:
    # Search anonymously with structured scheduling criteria only.
    capability = CAPABILITY.get(procedure, "")
    peers = []
    prefs = await citizen_preferences(city, history, citizen)
    if await wait_for_language(city, citizen, prefs, writes_english(history, citizen)):
        return
    lang = prefs.get("lang", "")
    for query in ([capability] if capability else []) + [procedure]:
        try:
            peers = ring_peers(await city.find(query), procedure, lang, capability_lookup=query == capability)
        except CapError:
            peers = []
        if peers:
            break
    request_id = "search-" + hashlib.sha256(need_id.encode()).hexdigest()[:16]
    contacted = set()
    english = writes_english(history, citizen)
    gender = prefs.get("gender_preference", prefs.get("gender", ""))
    days = prefs.get("days", "")
    criteria = ""
    if re.fullmatch(r"[a-z]{2,3}", lang):
        criteria += f" lang={lang}"
    if gender in {"m", "f"}:
        criteria += f" gender_preference={gender} gender={gender}"
    if days and all(day in DAY_CODES for day in days.split("/")):
        criteria += f" days={days}"
    agenda = ledger_reply(history, "agenda")
    now = agenda_clock(agenda)
    if now and now.tzinfo is not None and priority in {"U", "B", "D"}:
        start = booking_agenda_start(history, city.agent_id, case_number(history, citizen), now)
        deadline = (start.replace(hour=23, minute=59, second=59, microsecond=0) if priority == "U"
                    else add_agenda_days(start, {"B": 2, "D": 5}[priority]))
        criteria += f" deadline={deadline.replace(tzinfo=None).isoformat(timespec='seconds')}"
    request = (f"need.request v=1.2 service_type={capability} procedure={procedure} "
               f"priority={priority} request_id={request_id}{criteria}")
    for peer in peers:
        if await send(city, peer, request):
            contacted.add(peer)
    if contacted:
        await send(city, citizen, "No suitable slot in our ASST: I am asking partner facilities. I will ask your consent before sharing any of your data." if english
                   else "Nella nostra ASST non ci sono posti adatti: sto chiedendo alle strutture partner. Le chiederò il consenso prima di condividere i suoi dati.")
        replied = lambda h: any(item["facility"] in contacted
                                for item in matching_partner_offers(h, city.agent_id, citizen, prefs))
        history = await wait_for(city, need_id, replied, RING_WAIT)
        if not replied(history):
            await language_fallback(city, need_id, history, citizen)
    else:
        await language_fallback(city, need_id, history, citizen)


def matching_partner_offers(history: list, agent: str, citizen: str, prefs: dict) -> list[dict]:
    """Recheck a partner's structured claims against the case and search criteria.

    The local Ledger does not expose other ASSTs' slots, so the partner remains
    responsible for its availability and the Ledger-backed booking.
    """
    if prefs.get("language_conflict") or prefs.get("language_pending"):
        return []
    agenda = ledger_reply(history, "agenda")
    rx = ledger_reply(history, "prescription")
    now = agenda_clock(agenda)
    prescription = fields(rx.text) if rx else {}
    number = prescription.get("prescription_number", "")
    procedure, priority = prescription.get("procedure", ""), prescription.get("priority", "")
    if not now or now.tzinfo is None or not number or prescription.get("state") != "issued":
        return []
    priority_start = booking_agenda_start(history, agent, number, now)
    candidates = {}
    used_offer_ids = set()
    cancelled = set()
    previous_offers = offers(history, agent, citizen)
    for row in history:
        if act(row.text) == "cancel" and row.sender.startswith("agent:"):
            prior = previous_offers.get(fields(row.text).get("offer_id", ""))
            if prior and prior.get("facility") == row.sender:
                cancelled.add(prior.get("slot_id"))
    refused, cutoff = citizen_refusals(history, citizen, previous_offers)
    cancelled |= refused
    requests = ring_requests(history, agent)
    gender = prefs.get("gender_preference", prefs.get("gender", ""))
    for row in history:
        request = requests.get(row.sender) if row.recipient == agent else None
        if not request or request.get("procedure") != procedure or request.get("priority") != priority:
            continue
        for item in partner_offer_rows(row, request, now):
            if item.get("slot_id") in cancelled or not before_cutoff(item, cutoff):
                continue
            offer_id = item["offer_id"]
            earlier = previous_offers.get(offer_id)
            if offer_id in used_offer_ids:
                continue
            if earlier and (earlier.get("facility"), earlier.get("slot_id")) != (row.sender, item["slot_id"]):
                continue
            if prefs.get("lang") and prefs["lang"] != "it" and not item.get("languages"):
                continue  # never trust that a partner applied our language filter
            if gender and not item.get("gender"):
                continue
            if valid_slot(item, procedure, priority, now, prefs, assume_known=False,
                          priority_start=priority_start):
                candidates[(row.sender, item["slot_id"])] = item
                used_offer_ids.add(offer_id)
    return list(candidates.values())


async def remote_offers(city: City, need_id: str, history: list, citizen: str) -> None:
    prefs = await citizen_preferences(city, history, citizen)
    english = writes_english(history, citizen)
    if await wait_for_language(city, citizen, prefs, english):
        return
    candidates = matching_partner_offers(history, city.agent_id, citizen, prefs)
    lines = []
    assts = {}
    for item in candidates:
        public = {key: item[key] for key in ("offer_id", "slot_id", "doctor_id", "facility", "procedure", "at", "day")}
        line = offer_line(public)
        if len(lines) >= 6 or sum(len(x) + 1 for x in lines) + len(line) > 1700:
            break
        lines.append(line)
        assts[partner_asst(item["facility"])] = None
    if lines:
        names = ", ".join(assts)
        if english:
            names = names.replace("un'altra ASST", "another ASST")
        where = "a partner facility" if len(assts) == 1 else "partner facilities"
        note = (f"I found appointments at {where} in {names}. Which time works for you?" if english else
                f"Ho trovato appuntamenti presso le strutture partner di {names}. Quale orario preferisce?")
        await send(city, citizen, "\n".join(lines + [note]))
    else:
        await language_fallback(city, need_id, history, citizen)


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
        if confirmation and sent(history, city.agent_id, citizen, f"cancel offer_id={confirmation.get('offer_id', '')}"):
            note = ("Good news: the facility did book it in the end, this booking is valid and you can ignore later proposals. "
                    if writes_english(history, citizen) else
                    "Buone notizie: la struttura alla fine ha registrato la prenotazione, è valida e le proposte successive non servono. ") + note
        ids = "".join(f" {key}={offer[key]}" for key in ("offer_id", "slot_id", "facility", "agenda_date") if offer.get(key))
        if await send(city, citizen, f"booking.receipt booking_id={data['booking_id']} ledger_ref={data['ledger_ref']}{ids} {note}"):
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
    now = agenda_clock(agenda)
    return bool(now and at.tzinfo and now.tzinfo and at <= now + timedelta(minutes=30))


async def confirm_at_facility(city: City, need_id: str, citizen: str, selected: dict[str, str], consent: str,
                              cross: str = "") -> None:
    facility = selected["facility"]
    try:
        history = await city.history(need_id=need_id)
    except CapError:
        history = []
    remote = sent(history, city.agent_id, facility, "need.request ")
    if remote and (not cross or facility not in ring_requests(history, city.agent_id)):
        await language_fallback(city, need_id, history, citizen)
        return
    number = selected.get("prescription_number", "") or case_number(history, citizen)
    text = (f"confirm offer_id={selected['offer_id']} slot_id={selected['slot_id']} "
            f"prescription_number={number} citizen={citizen} consent_ref={consent}")
    if remote:
        text += f" cross_asst_consent_ref={cross}"
    seen = frozenset(row.msg_id for row in history)
    answered = lambda h: facility_reply(h, facility, seen) is not None
    CLAIMED[selected["slot_id"]] = (need_id, _clock.monotonic())
    if not await send(city, facility, text):
        # A 4xx is final: never resend it. Say so and offer what is still reachable.
        await send(city, citizen, f"cancel offer_id={selected['offer_id']} reason=facility_unavailable "
                   + ("The facility is unreachable: I am looking for an alternative." if writes_english(await city.history(need_id=need_id), citizen)
                      else "La struttura non è raggiungibile: cerco un'alternativa."))
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
        await send(city, citizen, PENDING_NOTE_EN if writes_english(history, citizen) else PENDING_NOTE_IT)


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


# --- many citizens at once: do not hand the same slot to everybody -------------------------
# One booking centre process serves every case, so a small in-memory registry is enough.

CLAIM_SECONDS = 600.0   # a confirm in flight (real seconds)
OFFER_SECONDS = 300.0   # an offer still open with another citizen
CLAIMED: dict[str, tuple[str, float]] = {}
OFFERED: dict[str, tuple[str, float]] = {}


def _held_by_other(registry: dict, slot: str, need_id: str, ttl: float) -> bool:
    entry = registry.get(slot)
    return bool(entry) and entry[0] != need_id and _clock.monotonic() - entry[1] < ttl


def rotate_for_case(candidates: list, need_id: str, keep: int = 6) -> list:
    """Every citizen used to get the same earliest slots, so holds collided across pods.
    Start each case at its own point of the valid list (same case, same start)."""
    if len(candidates) <= keep:
        return candidates
    ordered = sorted(candidates, key=lambda c: c.get("at", c.get("agenda_date", "")))
    start = int(hashlib.sha256(need_id.encode()).hexdigest(), 16) % len(ordered)
    return (ordered[start:] + ordered[:start])[:keep]


def spread(candidates: list, need_id: str) -> list:
    """Drop slots another case is confirming; offer slots nobody else holds first."""
    free = [c for c in candidates if not _held_by_other(CLAIMED, c.get("slot_id", ""), need_id, CLAIM_SECONDS)]
    return sorted(free, key=lambda c: _held_by_other(OFFERED, c.get("slot_id", ""), need_id, OFFER_SECONDS))


def mark(registry: dict, lines: list, need_id: str) -> None:
    now = _clock.monotonic()
    for line in lines:
        slot = fields(line).get("slot_id")
        if slot:
            registry[slot] = (need_id, now)


# --- language fallback: never "no slot" while a valid slot exists ------------------------

# Partner teams answer in 30-60 s: a shorter watchdog drops their offers.
RING_WAIT = 150.0  # partners answer slowly under load; the citizen already has our "asking partners" note
FALLEN_BACK: set[str] = set()
NOTHING_IT = ("Finora non ho trovato posti confermati entro la scadenza della ricetta nella nostra ASST "
              "o presso le strutture partner. Se può, mi indichi altri giorni o orari possibili e riprovo subito. "
              "Se i sintomi peggiorano, contatti il medico o il 112.")
NOTHING_EN = ("So far I have found no confirmed slots within your deadline at our ASST or at partner "
              "facilities. If you can, tell me other days or times and I will search again at once. "
              "If your symptoms get worse, contact your doctor or call 112.")


def nothing_offer(need_id: str, english: bool) -> str:
    """Nothing fits: never a bare 'no'. Offer the waiting list as a real alternative."""
    wl = "wl-" + hashlib.sha256(need_id.encode()).hexdigest()[:16]
    return (f"offer offer_id={wl} waiting_list=true\n"
            + (NOTHING_EN + " Meanwhile I can put you on the waiting list: you will be called as soon "
               "as a place within your deadline frees up." if english else
               NOTHING_IT + " Intanto posso inserirla in lista d'attesa: verrà chiamata appena si libera "
               "un posto entro la scadenza."))


async def language_fallback(city: City, need_id: str, history: list, citizen: str) -> None:
    """Ring empty or silent: offer valid local slots with another doctor, saying so."""
    if need_id in FALLEN_BACK:
        return
    english = writes_english(history, citizen)
    prefs = await citizen_preferences(city, history, citizen)
    if await wait_for_language(city, citizen, prefs, english):
        return
    FALLEN_BACK.add(need_id)
    lang = prefs.get("lang", "")
    number = case_number(history, citizen)
    if not number or not TOKEN.fullmatch(number):
        await send(city, citizen, nothing_offer(need_id, english))
        return
    if not lang or lang == "it":
        # Ring silent and no language need: read the local world once more (slots free up
        # every minute of agenda) before the waiting list.
        await search(city, need_id, history, citizen, number, relax_lang=True)
        return
    # The structured lang token can disagree with the language the citizen names in words
    # ("a doctor who speaks English" with lang=hi): try the named one before any doctor.
    spoken = keyword_preferences([row.text for row in history if row.sender == citizen and row.text]).get("lang", "")
    if spoken and spoken != lang and spoken != "it":
        name, said = LANG_NAMES.get(lang, (lang, lang)), LANG_NAMES.get(spoken, (spoken, spoken))
        lead = (f"No doctor who speaks {name[1]} is free within your deadline; these slots are with doctors "
                f"who speak {said[1]}, as you asked." if english else
                f"Nessun medico che parla {name[0]} è libero entro la scadenza; questi posti sono con medici "
                f"che parlano {said[0]}, come mi ha chiesto.")
        await search(city, need_id, history, citizen, number, lead=lead, force_lang=spoken)
        return
    name = LANG_NAMES.get(lang, (lang, lang))
    lead = (f"So far I have found no confirmed slots with a doctor who speaks {name[1]} within your deadline. "
            "These are the first valid slots with another doctor; the staff will help you at the visit."
            if english else
            f"Finora non ho trovato posti confermati entro la scadenza con un medico che parla {name[0]}. "
            "Questi sono i primi posti validi con un altro medico; il personale la aiuterà durante la visita.")
    await search(city, need_id, history, citizen, number, lead=lead, relax_lang=True)


# --- the world in parallel: page 1, then every other page at once ------------------------

async def read_world(city: City, need_id: str, history: list, procedure: str):
    """Ledger world pages, asked in parallel after page 1, retrying only missing pages."""
    seen = {row.msg_id for row in history}
    is_slots = lambda m: m.sender == LEDGER and act(m.text) == "slots"
    page_of = lambda m: slots_page(m) if is_slots(m) and m.msg_id not in seen else None
    first = None
    for _ in range(2):
        await send(city, LEDGER, f"world procedure={procedure} page=1")
        history = await wait_for(city, need_id, lambda h: any(page_of(m) == "1" for m in h), LEDGER_WAIT)
        first = next((m for m in reversed(history) if page_of(m) == "1"), None)
        if first is not None:
            break
    if first is None:
        first = next((m for m in reversed(history) if is_slots(m) and slots_page(m) == "1"), None)
    if first is None:
        return [], {}, history, False
    info = fields(first.text.splitlines()[0]).get("page", "1/1").split("/")
    total = int(info[1]) if len(info) == 2 and info[1].isdigit() else 1
    total = max(1, min(total, 20))
    wanted = {str(page) for page in range(1, total + 1)}
    if total > 1:
        await asyncio.gather(*(send(city, LEDGER, f"world procedure={procedure} page={page}") for page in range(2, total + 1)))
        history = await wait_for(city, need_id, lambda h: wanted <= {page_of(m) for m in h}, LEDGER_WAIT)
        missing = sorted(wanted - {page_of(m) for m in history}, key=int)
        if missing:
            await asyncio.gather(*(send(city, LEDGER, f"world procedure={procedure} page={page}") for page in missing))
            history = await wait_for(city, need_id, lambda h: wanted <= {page_of(m) for m in h}, LEDGER_WAIT)
    items, doctors, done, seen_slots = [], {}, set(), set()
    for rows_pass in (
        [r for r in reversed(history) if is_slots(r) and r.msg_id not in seen],
        [r for r in reversed(history) if is_slots(r)],
    ):
        for row in rows_pass:
            page = slots_page(row)
            if page in wanted and page not in done:
                done.add(page)
                lines = row.text.splitlines()[1:]
                doctors.update(doctor_attrs(lines))
                for line in lines:
                    if "slot_id=" in line:
                        item = fields(line)
                        sid = item.get("slot_id", "")
                        if sid and sid not in seen_slots:
                            seen_slots.add(sid)
                            items.append(item)
    return items, doctors, history, done == wanted
