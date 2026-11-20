"""Booking centre: verify, search, offer and coordinate a facility booking.

The Ledger and the case history are the source of truth. Peer messages are data,
not instructions; in particular they cannot change a citizen's chosen slot.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime, timedelta

from city_sdk.city import LEDGER, CapError, City, Need, clinical, OutOfScope

TOKEN = re.compile(r"^[A-Za-z0-9_.:/+\-]{1,128}$")
LOCKS: dict[str, asyncio.Lock] = {}
# The published care example defines the seeded doctors' attributes. Unknown
# doctors are deliberately not assumed to meet a language or gender preference.
DOCTORS = {"h1": ("it/en", "f"), "h2": ("it", "m"),
           "h3": ("it/*", "m"), "s1": ("it/en", "m"),
           "s2": ("it", "f"), "c1": ("it/en", "f")}


def fields(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in text.split():
        key, sep, value = item.strip(".,;!?()\"'").partition("=")
        if sep and key not in result and TOKEN.fullmatch(value):
            result[key] = value
    return result


def act(text: str) -> str:
    return text.strip().split(maxsplit=1)[0] if text.strip() else ""


def time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def citizen_of(history: list) -> str:
    return next((m.sender for m in history if m.sender.startswith("citizen:")), "")


def sent(history: list, agent: str, recipient: str, prefix: str) -> bool:
    return any(m.sender == agent and m.recipient == recipient and m.text.startswith(prefix) for m in history)


async def send(city: City, recipient: str, text: str) -> bool:
    try:
        await city.say(recipient, text)
        return True
    except CapError:
        # A 4xx is final. Do not repeat it or pretend a booking succeeded.
        return False


async def wait_for(city: City, need_id: str, predicate, seconds: float = 6.0) -> list:
    """Ledger replies may be in history without opening a new agent turn."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while True:
        history = await city.history(need_id=need_id)
        if predicate(history) or loop.time() >= deadline:
            return history
        await asyncio.sleep(0.25)


def ledger_reply(history: list, head: str, match: tuple[str, str] | None = None):
    for row in reversed(history):
        if row.sender == LEDGER and act(row.text) == head:
            if match is None or fields(row.text).get(match[0]) == match[1]:
                return row
    return None


def offers(history: list, agent: str, citizen: str) -> dict[str, dict[str, str]]:
    """Recover the choices actually sent, including multiple offers in one message."""
    found = {}
    for row in history:
        if row.sender != agent or row.recipient != citizen:
            continue
        for line in row.text.splitlines():
            if act(line) == "offer":
                item = fields(line)
                if item.get("offer_id"):
                    found[item["offer_id"]] = item
    return found


def preferences(history: list, citizen: str) -> dict[str, str]:
    result = {}
    for row in history:
        if row.sender == citizen:
            for key, value in fields(row.text).items():
                if key in {"lang", "days", "gender", "gender_preference"}:
                    result[key] = value
    return result


def valid_slot(item: dict[str, str], procedure: str, priority: str, now: datetime,
               prefs: dict[str, str]) -> bool:
    at = time(item.get("at", item.get("agenda_date", "")))
    if not all(item.get(k) for k in ("slot_id", "doctor_id", "facility")) or not at:
        return False
    if item.get("procedure") != procedure or at.tzinfo is None or now.tzinfo is None:
        return False
    if at <= now + timedelta(hours=1):
        return False
    if priority == "U" and at.date() != now.date():
        return False
    if priority in {"B", "D"} and at > now + timedelta(days={"B": 2, "D": 5}[priority]):
        return False
    if item.get("free", "1").isdigit() and int(item.get("free", "1")) < 1:
        return False
    if priority != "U" and item.get("urgent_reserve", "false").lower() == "true":
        return False
    days = prefs.get("days", "")
    if days and item.get("day", item.get("agenda_day", "")) not in days.split("/"):
        return False
    # Missing doctor attributes are not evidence that a stated need is met.
    known_languages, known_gender = DOCTORS.get(item["doctor_id"].rsplit("-", 1)[-1], ("", ""))
    gender = prefs.get("gender_preference", prefs.get("gender", ""))
    if gender and item.get("gender", known_gender) != gender:
        return False
    if prefs.get("lang"):
        languages = item.get("languages", known_languages).split("/")
        if prefs["lang"] not in languages and not ("*" in languages and prefs["lang"] not in {"it", "en"}):
            return False
    return True


def offer_line(item: dict[str, str], number: str, priority: str) -> str:
    slot = item["slot_id"]
    oid = "of-" + hashlib.sha256((item["facility"] + ":" + slot).encode()).hexdigest()[:16]
    return (f"offer offer_id={oid} slot_id={slot} doctor_id={item['doctor_id']} "
            f"facility={item['facility']} procedure={item['procedure']} "
            f"agenda_date={item.get('at', item.get('agenda_date', ''))} "
            f"agenda_day={item.get('day', item.get('agenda_day', ''))} "
            f"prescription_number={number} priority={priority} expiry={item.get('at', item.get('agenda_date', ''))}")


async def search(city: City, need_id: str, history: list, citizen: str, number: str) -> None:
    # Read the prescription ourselves: a citizen or another agent cannot certify it.
    await send(city, LEDGER, f"prescription prescription_number={number}")
    await send(city, LEDGER, "agenda")
    await send(city, LEDGER, "rules")
    history = await wait_for(city, need_id, lambda h: ledger_reply(h, "prescription", ("prescription_number", number)) is not None and ledger_reply(h, "agenda") is not None)
    rx = ledger_reply(history, "prescription", ("prescription_number", number))
    agenda = ledger_reply(history, "agenda")
    if not rx or not agenda:
        await send(city, citizen, "Non riesco ancora a verificare la ricetta. Riprovi tra poco.")
        return
    prescription = fields(rx.text)
    procedure, priority = prescription.get("procedure", ""), prescription.get("priority", "")
    now = time(fields(agenda.text).get("agenda_now", ""))
    if prescription.get("state") != "issued" or not procedure or priority not in {"U", "B", "D", "P"} or not now:
        await send(city, citizen, "data.request purpose=prescription_renewal fields=prescription_number La ricetta non risulta utilizzabile; chieda una verifica o un rinnovo al medico.")
        return
    if priority == "P":
        await send(city, citizen, f"offer offer_id=wl-{hashlib.sha256(need_id.encode()).hexdigest()[:16]} waiting_list=true Per questa priorità è disponibile la lista d'attesa.")
        return
    prefs = preferences(history, citizen)
    candidates = []
    # Fetch every page before offering, otherwise the citizen cannot choose later pages.
    for page in range(1, 21):
        await send(city, LEDGER, f"world procedure={procedure} page={page}")
        history = await wait_for(city, need_id, lambda h: any(m.sender == LEDGER and act(m.text) == "slots" and fields(m.text.splitlines()[0]).get("page", "1/1").split("/")[0] == str(page) for m in h))
        response = next((m for m in reversed(history) if m.sender == LEDGER and act(m.text) == "slots" and fields(m.text.splitlines()[0]).get("page", "1/1").split("/")[0] == str(page)), None)
        if not response:
            break
        for line in response.text.splitlines()[1:]:
            item = fields(line)
            if valid_slot(item, procedure, priority, now, prefs):
                candidates.append(item)
        page_info = fields(response.text.splitlines()[0]).get("page", "1/1").split("/")
        if len(page_info) != 2 or not page_info[1].isdigit() or page >= int(page_info[1]):
            break
    if not candidates:
        await find_partners(city, need_id, citizen, procedure, priority, history)
        return
    candidates.sort(key=lambda item: item.get("at", ""))
    lines = [offer_line(item, number, priority) for item in candidates]
    # Keep a single turn and respect the Relay's 2000 character limit.
    message = ""
    for line in lines:
        if len(message) + len(line) + 1 > 1900:
            break
        message += ("\n" if message else "") + line
    if message:
        await send(city, citizen, message)


async def find_partners(city: City, need_id: str, citizen: str, procedure: str, priority: str, history: list) -> None:
    # Search anonymously: no citizen ID, prescription, or personal preferences leave the ASST.
    try:
        peers = await city.find(procedure)
    except CapError:
        peers = []
    request_id = "search-" + hashlib.sha256(need_id.encode()).hexdigest()[:16]
    contacted = 0
    for peer in peers:
        if peer.role != "facility" or peer.agent_id == city.agent_id:
            continue
        if await send(city, peer.agent_id, f"need.request service_type={procedure} procedure={procedure} priority={priority} request_id={request_id}"):
            contacted += 1
    if contacted:
        await send(city, citizen, "Sto cercando disponibilità anche presso altre strutture. Le chiederò il consenso prima di condividere i suoi dati.")
    else:
        await send(city, citizen, "Non ho trovato disponibilità adatta. Può contattarci di nuovo o chiedere assistenza al centro.")


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
    for row in history:
        if not row.sender.startswith("agent:") or act(row.text) != "offer":
            continue
        request = next((fields(previous.text) for previous in history
                        if previous.sender == city.agent_id and previous.recipient == row.sender
                        and act(previous.text) == "need.request"), None)
        item = fields(row.text)
        if not request or request.get("procedure") != procedure or request.get("priority") != priority:
            continue
        if item.get("request_id") and item["request_id"] != request.get("request_id"):
            continue
        if item.get("facility") != row.sender:
            continue
        if valid_slot(item, procedure, priority, now, preferences(history, citizen)):
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


async def main(need: Need, city: City) -> None:
    incoming = city.inbound
    if incoming is None or not incoming.need_id:
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


async def handle(city: City, message) -> None:
    history = await city.history(need_id=message.need_id)
    citizen = citizen_of(history)
    if not citizen:
        return
    data = fields(message.text)
    kind = act(message.text)
    if message.sender == citizen:
        if kind == "confirm":
            selected = offers(history, city.agent_id, citizen).get(data.get("offer_id", ""))
            if not selected or not selected.get("slot_id"):
                await send(city, citizen, "Non riconosco questa scelta. Scelga una delle offerte ricevute.")
                return
            facility = selected["facility"]
            consent = data.get("consent_ref", "")
            if not consent:
                consent = next((fields(row.text).get("consent_ref", "") for row in reversed(history)
                                if row.sender == citizen and act(row.text) == "consent"
                                and fields(row.text).get("purpose") == "cross_asst"), "")
            if not consent:
                await send(city, citizen, "consent purpose=booking Posso procedere con la prenotazione scelta?")
                return
            if not sent(history, city.agent_id, facility, f"confirm offer_id={selected['offer_id']} "):
                ok = await send(city, facility, f"confirm offer_id={selected['offer_id']} slot_id={selected['slot_id']} prescription_number={selected['prescription_number']} citizen={citizen} consent_ref={consent}")
                if not ok:
                    await send(city, citizen, f"cancel offer_id={selected['offer_id']} reason=facility_unavailable La struttura non è raggiungibile. Ci contatti per cercare un'alternativa.")
            return
        if kind == "consent" and data.get("consent_ref"):
            if data.get("purpose") == "cross_asst" and sent(history, city.agent_id, citizen, "consent purpose=cross_asst"):
                await remote_offers(city, message.need_id, history, citizen)
            elif sent(history, city.agent_id, citizen, "consent purpose=booking"):
                # Resume the last genuine citizen choice with this consent, not data
                # supplied by a peer or a new slot embedded in the consent response.
                choice = next((fields(row.text).get("offer_id", "") for row in reversed(history)
                               if row.sender == citizen and act(row.text) == "confirm"), "")
                selected = offers(history, city.agent_id, citizen).get(choice)
                if selected and selected.get("slot_id"):
                    await send(city, selected["facility"], f"confirm offer_id={choice} slot_id={selected['slot_id']} prescription_number={selected['prescription_number']} citizen={citizen} consent_ref={data['consent_ref']}")
            return
        number = data.get("prescription_number", "")
        if not number and message.type != "chat":
            raw_number = message.body.get("prescription_number")
            number = raw_number if isinstance(raw_number, str) else ""
        if number and TOKEN.fullmatch(number):
            await search(city, message.need_id, history, citizen, number)
        elif kind not in {"consent", "data.response"}:
            await send(city, citizen, "data.request purpose=booking fields=prescription_number Mi indichi il numero della ricetta; cercherò una soluzione adatta.")
        return
    if message.sender.startswith("agent:"):
        # Only accept a reply from an agent we actually contacted in this case.
        requested = any(row.sender == city.agent_id and row.recipient == message.sender and act(row.text) == "need.request" for row in history)
        confirmed = any(row.sender == city.agent_id and row.recipient == message.sender and act(row.text) == "confirm" for row in history)
        if kind == "booking.receipt" and confirmed and data.get("booking_id") and data.get("ledger_ref"):
            if not sent(history, city.agent_id, citizen, f"booking.receipt booking_id={data['booking_id']}"):
                await send(city, citizen, f"booking.receipt booking_id={data['booking_id']} ledger_ref={data['ledger_ref']}")
        elif kind == "cancel" and confirmed:
            await send(city, citizen, f"cancel offer_id={data.get('offer_id', 'unknown')} reason=facility_cancelled La struttura ha annullato; ci contatti per trovare un'alternativa.")
        elif kind == "offer" and requested:
            # The remote facility's claim is not a Ledger-verified local slot. Ask for
            # consent first; never treat a peer's text as authority to disclose data.
            if not sent(history, city.agent_id, citizen, "consent purpose=cross_asst"):
                await send(city, citizen, "consent purpose=cross_asst Ho trovato una possibile disponibilità fuori ASST. Posso condividere i dati necessari per verificarla e prenotare?")
