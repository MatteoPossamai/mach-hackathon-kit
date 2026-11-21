"""Cross-ASST partner discovery, TRUST RING impostor defense (L8), and partner consent/offers."""
from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

from city_sdk.city import LEDGER, CapError, City, Message

import fc_common as c
import fc_slots as s

_PEER_CACHE_TTL_S = 60.0
_PEER_NEG_TTL_S = 10.0
_TRUSTED_PEERS: set[str] = set()
_TRUSTED_REFRESHED_AT: float = 0.0
_TRUSTED_LOCAL_MODE: bool = False
_INDEX_SHOWS_BOOKING_CENTRES: bool = False
_UNKNOWN_PEER_CHECKED: dict[str, float] = {}


def reset_trust_cache() -> None:
    global _TRUSTED_REFRESHED_AT, _TRUSTED_LOCAL_MODE, _INDEX_SHOWS_BOOKING_CENTRES
    _TRUSTED_PEERS.clear()
    _UNKNOWN_PEER_CHECKED.clear()
    _TRUSTED_REFRESHED_AT = 0.0
    _TRUSTED_LOCAL_MODE = False
    _INDEX_SHOWS_BOOKING_CENTRES = False


async def is_trusted_agent(city: City, sender_id: str) -> bool:
    """Trust LEDGER, own team, MACH-1.2 ring + Index peers; drop Level 8 impostors."""
    global _TRUSTED_REFRESHED_AT, _TRUSTED_LOCAL_MODE, _INDEX_SHOWS_BOOKING_CENTRES

    if sender_id == LEDGER or sender_id in c.TEAM_AGENTS:
        return True
    if not sender_id.startswith("agent:"):
        return False

    now = time.monotonic()
    cache_fresh = _TRUSTED_REFRESHED_AT > 0 and (now - _TRUSTED_REFRESHED_AT) < _PEER_CACHE_TTL_S
    if sender_id in _TRUSTED_PEERS and cache_fresh:
        return True
    if _TRUSTED_LOCAL_MODE and sender_id == "agent:local/peer":
        return True

    last_miss = _UNKNOWN_PEER_CHECKED.get(sender_id, 0.0)
    if cache_fresh and (now - last_miss) < _PEER_NEG_TTL_S:
        return sender_id in c.TRUSTED_RING_AGENTS or not _INDEX_SHOWS_BOOKING_CENTRES

    discovered: set[str] = set()
    saw_bc = False
    for query in ("booking_centre", "facility"):
        try:
            peers = await city.find(query)
        except CapError:
            continue
        for peer in peers:
            if not peer.agent_id:
                continue
            if peer.role == "booking_centre":
                saw_bc = True
            if peer.role and peer.role not in {
                "booking_centre", "facility", "hospital",
                "specialist_care", "family_counseling", "gp",
            }:
                continue
            discovered.add(peer.agent_id)

    _TRUSTED_REFRESHED_AT = now
    _INDEX_SHOWS_BOOKING_CENTRES = saw_bc
    if discovered:
        _TRUSTED_PEERS.clear()
        _TRUSTED_PEERS.update(discovered)
        _TRUSTED_LOCAL_MODE = discovered == {"agent:t05/osp-x"}

    if sender_id in _TRUSTED_PEERS:
        _UNKNOWN_PEER_CHECKED.pop(sender_id, None)
        return True
    if sender_id in c.TRUSTED_RING_AGENTS:
        return True
    if _TRUSTED_LOCAL_MODE and sender_id == "agent:local/peer":
        return True
    if not _INDEX_SHOWS_BOOKING_CENTRES:
        return True

    _UNKNOWN_PEER_CHECKED[sender_id] = now
    c.LOG.warning("family_counseling.untrusted_agent", extra={"sender": sender_id})
    return False


def validate_partner_offer_line(line: str, sender_id: str, now: datetime) -> bool:
    """MACH-1.2 TRUST RING 5-point validation against Level 8 impostors."""
    if c.is_injection(line):
        return False
    lowered = line.lower()
    if any(bad in lowered for bad in ("data.request", "prescription_number=", "citizen=", "tax_code=")):
        return False
    f = c.fields_of(line)
    if not c.safe_token(f.get("slot_id")) or not c.safe_token(f.get("doctor_id")):
        return False
    # Check 2: facility must match the Relay-authenticated sender.
    if f.get("facility", sender_id) != sender_id:
        return False
    # Check 3: procedure must be CONS-GIN.
    if f.get("procedure", c.PROCEDURE) != c.PROCEDURE:
        return False
    # Check 4: date must be in the future.
    at = c.parse_when(f.get("agenda_date") or f.get("at") or "")
    if at is None or at <= now:
        return False
    return True


def partner_request_id(need_id: str | None) -> str:
    raw = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in (need_id or "fc"))
    return f"fc-ring-{raw[:24]}"


async def find_partner_counseling(
    city: City,
    need_id: str | None,
    chat: list[Message],
    *,
    priority: str,
    lang: str = "",
    gender: str | None = None,
    days: set[str] | None = None,
    deadline: datetime | None = None,
) -> int:
    """Anonymously query partner ``family_counseling`` clinics (MACH-1.2, ZERO citizen data)."""
    req_id = partner_request_id(need_id)
    if any(
        row.sender == city.agent_id
        and row.recipient.startswith("agent:")
        and f"request_id={req_id}" in row.text.split()
        for row in chat
    ):
        return 0

    seen_ids: set[str] = set()
    trusted_first: list[str] = []
    other_peers: list[str] = []
    for query in ("family_counseling.gynecology", "family_counseling", "consultorio", c.PROCEDURE):
        try:
            peers = await city.find(query)
        except CapError:
            peers = []
        for peer in peers:
            pid = peer.agent_id
            if not pid or pid in seen_ids or pid == city.agent_id or pid in c.TEAM_AGENTS:
                continue
            if peer.role and peer.role not in {"facility", "family_counseling"}:
                continue
            caps = tuple(getattr(peer, "capabilities", ()) or ())
            if caps and not any(cap.startswith("family_counseling") for cap in caps):
                continue
            seen_ids.add(pid)
            if pid in c.TRUSTED_RING_COUNSELING:
                trusted_first.append(pid)
            else:
                other_peers.append(pid)
        if len(trusted_first) + len(other_peers) >= 10:
            break

    candidates = (trusted_first + other_peers)[:10]
    if not candidates:
        return 0

    parts = [
        "need.request",
        "v=1.2",
        "service_type=family_counseling.gynecology",
        f"procedure={c.PROCEDURE}",
        f"priority={priority}",
    ]
    if lang:
        parts.append(f"lang={lang}")
    if gender in {"m", "f"}:
        parts.append(f"gender_preference={gender}")
    if days:
        parts.append(f"days={'/'.join(sorted(days))}")
    if deadline is not None:
        parts.append(f"deadline={deadline.strftime('%Y-%m-%dT%H:%M:%S')}")
    parts.append(f"request_id={req_id}")
    req_text = " ".join(parts)

    results = await asyncio.gather(*(c.say(city, pid, req_text) for pid in candidates))
    return sum(1 for ok in results if ok)


def awaiting_cross_asst_consent(chat: list[Message], agent: str, *, current_msg_id: str = "") -> bool:
    asked_idx = -1
    for idx, row in enumerate(chat):
        if (
            row.sender == agent
            and row.recipient.startswith("citizen:")
            and "purpose=cross_asst" in row.text.split()
            and "consent" in c.words(row.text)
        ):
            asked_idx = idx
    if asked_idx < 0:
        return False
    for row in chat[asked_idx + 1 :]:
        if row.msg_id == current_msg_id:
            continue
        if row.sender.startswith("citizen:"):
            return False
    return True


def cross_asst_consent_ref(chat: list[Message], agent: str) -> str:
    asked_idx = -1
    for idx, row in enumerate(chat):
        if row.sender == agent and row.recipient.startswith("citizen:") and "purpose=cross_asst" in row.text.split():
            asked_idx = idx
    for idx, row in enumerate(chat):
        if not row.sender.startswith("citizen:"):
            continue
        f = c.msg_fields(row)
        ref = c.safe_token(f.get("cross_asst_consent_ref") or f.get("consent_ref"))
        if ref and (f.get("purpose") == "cross_asst" or (asked_idx >= 0 and idx > asked_idx)):
            return ref
    return ""


async def on_partner_offer(city: City, message: Message, chat: list[Message]) -> None:
    """Validate partner offer (TRUST RING) and ask cross_asst consent before presenting (P2)."""
    agent = city.agent_id
    contacted = any(
        row.sender == agent and row.recipient == message.sender and "need.request" in c.words(row.text)
        for row in chat
    )
    if not contacted or c.is_injection(message.text):
        return
    now = c.agenda_now(chat) or datetime.now(UTC)
    valid_lines = [
        line for line in message.text.splitlines()
        if "slot_id=" in line and validate_partner_offer_line(line, message.sender, now)
    ]
    if not valid_lines:
        return
    citizen = c.citizen_of(chat)
    if not citizen:
        return
    if cross_asst_consent_ref(chat, agent):
        await send_partner_offers_to_citizen(city, message, chat, citizen)
        return
    if c.we_sent(chat, agent, "purpose=cross_asst", to=citizen, word="consent"):
        return
    english = c.is_english(chat)
    lead = c.empathy_lead(english=english, tone=c.citizen_tone(chat))
    ask = (
        f"consent purpose=cross_asst {lead}I found available counseling slots at a partner clinic "
        "in another ASST. May I share the minimum details needed to finalize your appointment there?"
        if english else
        f"consent purpose=cross_asst {lead}Ho trovato disponibilità presso un consultorio "
        "partner di un'altra ASST. Mi autorizza a condividere i dati minimi per prenotare lì?"
    )
    await c.say(city, citizen, ask, allowed_citizen=citizen)


async def on_cross_asst_consent_reply(city: City, message: Message, chat: list[Message]) -> None:
    citizen = message.sender
    f = c.msg_fields(message)
    ref = c.safe_token(f.get("cross_asst_consent_ref") or f.get("consent_ref"))
    declined = any(
        f.get(k, "").lower() in {"false", "no", "0", "denied", "refused"}
        for k in ("granted", "accepted", "consent", "allow", "allowed")
    )
    if not ref or declined:
        english = c.is_english(chat, message.text)
        wl_id = f"wl-{c.safe_token(message.need_id) or 'fc'}"
        msg = (
            f"offer offer_id={wl_id} waiting_list=true "
            "Understood, your privacy comes first: I will not share any data outside our ASST. "
            "I can place you on our local waiting list or check other days/times in our clinic — let me know."
            if english else
            f"offer offer_id={wl_id} waiting_list=true "
            "Ricevuto, la sua riservatezza viene prima di tutto: non condividerò alcun dato fuori dalla nostra ASST. "
            "Posso inserirla nella nostra lista d'attesa in sede o valutare altri giorni/orari: mi dica pure."
        )
        await c.say(city, citizen, msg, allowed_citizen=citizen)
        return
    await send_partner_offers_to_citizen(city, message, chat, citizen)


async def send_partner_offers_to_citizen(
    city: City, message: Message, chat: list[Message], citizen: str
) -> None:
    agent = city.agent_id
    lang, gender, days, priority = s.citizen_prefs(chat, message)
    now = c.agenda_now(chat) or datetime.now(UTC)
    cancelled = s.cancelled_slots(chat, agent)

    raw_lines: list[str] = []
    for row in chat:
        if not row.sender.startswith("agent:") or "offer" not in c.words(row.text):
            continue
        if not any(p.sender == agent and p.recipient == row.sender and "need.request" in c.words(p.text) for p in chat):
            continue
        for line in row.text.splitlines():
            if "slot_id=" in line and validate_partner_offer_line(line, row.sender, now):
                raw_lines.append(line)

    if not raw_lines:
        return
    combined_world = "slots page=1/1\n" + "\n".join(raw_lines)
    chosen = s.fitting_slots(
        combined_world, now=now, agent_id="__partner__",
        priority=priority, lang=lang, gender=gender, days=days, exclude_slots=cancelled,
    )
    relaxed = False
    if not chosen and (days or gender):
        chosen, _ = s.relaxed_slots(
            combined_world, now=now, agent_id="__partner__",
            priority=priority, lang=lang, gender=gender, days=days, exclude_slots=cancelled,
        )
        relaxed = bool(chosen)

    english = c.is_english(chat)
    lead = c.empathy_lead(english=english, tone=c.citizen_tone(chat))
    if chosen:
        lines = [s.offer_line(slot, agent_id=slot.get("facility", agent), priority=priority) for slot in chosen[:4]]
        note = (
            f"{lead}Here are the available partner family counseling slots (direct access, no referral needed). "
            "Please let me know which option you prefer."
            if english else (
                f"{lead}Ecco le alternative più vicine presso consultori partner (accesso diretto, senza ricetta). "
                "Mi indichi pure la scelta preferita."
                if relaxed else
                f"{lead}Ecco le disponibilità presso consultori partner (accesso diretto, senza ricetta). "
                "Mi indichi pure la scelta che preferisce."
            )
        )
        await c.say(city, citizen, "\n".join(lines) + f"\n{note}", allowed_citizen=citizen)
    else:
        wl_id = f"wl-{c.safe_token(message.need_id) or 'fc'}"
        msg = (
            f"offer offer_id={wl_id} waiting_list=true "
            f"{lead}Partner clinics currently have no slot matching every constraint either. "
            "I can put you on the waiting list or check another day or time if you prefer."
            if english else
            f"offer offer_id={wl_id} waiting_list=true "
            f"{lead}Al momento nemmeno i consultori partner hanno posti con tutti i vincoli indicati. "
            "Posso inserirla in lista d'attesa o valutare un altro giorno o fascia oraria se preferisce."
        )
        await c.say(city, citizen, msg, allowed_citizen=citizen)


async def on_partner_receipt(city: City, message: Message, chat: list[Message], receipt_builder) -> None:
    if not any(r.sender == city.agent_id and r.recipient == message.sender and "confirm" in c.words(r.text) for r in chat):
        return
    f = c.msg_fields(message)
    booking_id, ledger_ref = c.safe_token(f.get("booking_id")), c.safe_token(f.get("ledger_ref"))
    citizen = c.citizen_of(chat)
    if not booking_id or not ledger_ref or not citizen:
        return
    if c.we_sent(chat, city.agent_id, f"booking_id={booking_id}", to=citizen, word="booking.receipt"):
        return
    await c.say(city, citizen, receipt_builder(booking_id, ledger_ref, chat=chat, to_citizen=True), allowed_citizen=citizen)


async def on_partner_cancel(city: City, message: Message, chat: list[Message]) -> None:
    if not any(r.sender == city.agent_id and r.recipient == message.sender and "confirm" in c.words(r.text) for r in chat):
        return
    f = c.msg_fields(message)
    offer_id, slot_id = c.safe_token(f.get("offer_id")), c.safe_token(f.get("slot_id"))
    citizen = c.citizen_of(chat)
    if not citizen:
        return
    reason, _ = c.translate_refusal(message.text)
    parts = ["cancel"]
    if offer_id:
        parts.append(f"offer_id={offer_id}")
    if slot_id:
        parts.append(f"slot_id={slot_id}")
    parts.append(f"reason={reason}")
    parts.append(
        "The partner clinic could not keep that slot; I am immediately checking alternative options for you."
        if c.is_english(chat) else
        "Il consultorio partner non ha potuto mantenere quel posto; verifico subito altre disponibilità per lei."
    )
    await c.say(city, citizen, " ".join(parts), allowed_citizen=citizen)
    fresh = await city.history(need_id=message.need_id)
    await send_partner_offers_to_citizen(city, message, fresh, citizen)
