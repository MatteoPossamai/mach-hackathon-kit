"""Specialist agent brain (``specialist_care.cardiology``, ASST asst-lambrate).

Handles the complete cardiology specialist lifecycle on Milano Agentic City:
- Supported procedures: ``VIS-CARD`` (09:00 urgent reserve, 10:30, 12:00, 14:30)
  and ``CTRL-CARD`` (16:00 follow-up visit).
- Doctors: ``s1`` (m, it/en, mon/wed/fri) and ``s2`` (f, it, tue/thu).
- Urgent-first scheduling: red-flag emergencies bypass every queue; ``priority=U``
  turns preempt normal ``B``/``D``/``P`` turns, prefer ``urgent_reserve=true``
  slots first, and fall back to the next open business day when clinics are closed.
- Security (Level 8 impostors & audit): verifies ``agent:*`` senders against
  ``city.find(...)``, ignores embedded instructions, sanitizes all Ledger tokens,
  never leaks another ``citizen:*`` identifier, and never includes clinical advice.
- Care flow:
  1. Availability (``need.request`` from own/cross-ASST ``booking_centre`` or direct
     citizen with prescription) -> ``world`` + ``agenda`` -> ``offer`` (or ``cancel``).
  2. Booking (``confirm``) -> ``hold`` -> ``commit`` -> ``booking.receipt`` (never
     ghost receipts; propagates Ledger refusal reasons via ``cancel``).
  3. Check-in (``checkin``) -> Ledger ``checked_in`` -> polite bilingual check-in
     confirmation + ``consent purpose=report_sharing`` -> optional follow-up
     ``prescribe ... procedure=CTRL-CARD priority=D follow_up_of=<booking_id>`` ->
     Ledger ``report`` (only ``outcome_code``, no clinical content) -> citizen
     ``report`` (and ``prescription`` when follow-up was issued).
"""

from __future__ import annotations

import asyncio
import heapq
import logging
import os
import re
import sys
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import specialist_guard as _guard
import specialist_reports as _reports
import specialist_text as _text
import specialist_slots as _slots
import specialist_report_state as _report_state
import specialist_availability as _availability
import specialist_booking as _booking
from city_sdk.city import LEDGER, CapError, City, Message, Need, OutOfScope, clinical, understood

# --- specialist configuration ------------------------------------------------------------------

PRIMARY_PROCEDURE = "VIS-CARD"
FOLLOW_UP_PROCEDURE = "CTRL-CARD"
SUPPORTED_PROCEDURES = frozenset({PRIMARY_PROCEDURE, FOLLOW_UP_PROCEDURE})
FOLLOW_UP_PRIORITY = "D"
OUTCOME_CODE = "VISIT"

_MAX_PAGE = 8
_LEAD = timedelta(hours=4)
_URGENT_LEAD = timedelta(minutes=30)
_DEADLINE_DAYS = {"B": 2, "D": 5}
_PULL_TRIES = 8  # 0.2 s x 16 hammered the relay (429s for the whole team); same window
_PULL_PAUSE = 0.5
_URGENT_PULL_PAUSE = 0.08
_NORMAL_YIELD_CAP_S = 1.5
_MAX_NORMAL_CONCURRENCY = 4
_SENT_NEEDS = 4096
_PEER_CACHE_TTL_S = 60.0
_PEER_NEG_TTL_S = 10.0

_DOCTORS: dict[str, tuple[frozenset[str], str, frozenset[str]]] = {
    "h1": (frozenset({"it", "en"}), "f", frozenset({"mon", "tue", "wed"})),
    "h2": (frozenset({"it"}), "m", frozenset({"wed", "thu", "fri"})),
    "h3": (frozenset({"it", "*"}), "m", frozenset({"tue", "thu"})),
    "s1": (frozenset({"it", "en"}), "m", frozenset({"mon", "wed", "fri"})),
    "s2": (frozenset({"it"}), "f", frozenset({"tue", "thu"})),
    "c1": (frozenset({"it", "en"}), "f", frozenset({"mon", "tue", "wed", "thu", "fri"})),
}
_DAY_ALIASES: dict[str, str] = {
    "mon": "mon",
    "lun": "mon",
    "lunedi": "mon",
    "lunedì": "mon",
    "monday": "mon",
    "tue": "tue",
    "mar": "tue",
    "martedi": "tue",
    "martedì": "tue",
    "tuesday": "tue",
    "wed": "wed",
    "mer": "wed",
    "mercoledi": "wed",
    "mercoledì": "wed",
    "wednesday": "wed",
    "thu": "thu",
    "gio": "thu",
    "giovedi": "thu",
    "giovedì": "thu",
    "thursday": "thu",
    "fri": "fri",
    "ven": "fri",
    "venerdi": "fri",
    "venerdì": "fri",
    "friday": "fri",
}
_RED_FLAGS = (
    "perdita di forza alle gambe",
    "loss of strength in the legs",
    "dolore al petto",
    "chest pain",
    "male al petto",
    "dolore toracico",
    "fa male il petto",
    "petto mi fa male",
    "oppressione al petto",
    "peso sul petto",
    "stretta al petto",
    "dolore al torace",
    "perdo forza alle gambe",
    "forza nelle gambe",
    "forza alle gambe",
    "non sento le gambe",
    "gambe deboli",
    "debolezza alle gambe",
    "gambe che cedono",
    "pain in my chest",
    "chest hurts",
    "chest pressure",
    "tight chest",
    "chest tightness",
    "legs feel weak",
    "weak legs",
    "can't feel my legs",
    "legs giving way",
)
_ENGLISH_WORDS = frozenset(
    {
        "i",
        "my",
        "the",
        "have",
        "has",
        "pain",
        "need",
        "hello",
        "hi",
        "please",
        "doctor",
        "appointment",
        "booking",
        "visit",
        "report",
        "share",
        "yes",
        "thanks",
        "thank",
        "heart",
        "check",
    }
)
_WORRIED_STEMS = (
    "ansia",
    "ansios",
    "preoccup",
    "paura",
    "spaventat",
    "agitat",
    "nervos",
    "angosci",
    "worried",
    "anxious",
    "anxiety",
    "scared",
    "afraid",
    "nervous",
    "panic",
)
_URGENT_WORDS = frozenset({"urgente", "urgenza", "urgent", "urgency", "subito", "emergenza"})
_FOLLOW_UP_PHRASES = (
    "follow-up",
    "follow up",
    "follow_up",
    "ctrl-card",
    "visita di controllo",
    "ricetta di controllo",
    "prossimo controllo",
    "controllo cardiologico",
)
_TOKEN_SAFE = re.compile(r"^[A-Za-z0-9_.:/@-]+$")
_RX_RE = re.compile(r"^prescription_number-[A-Z0-9-]{4,40}$")
_REFUSAL_CODE_RE = re.compile(r"\b([A-Z][A-Z0-9_]{2,40})\b")
_CITIZEN_ID_RE = re.compile(r"\bcitizen:[A-Za-z0-9_.:/@-]+\b")
_LOG = logging.getLogger("specialist")


# --- concurrency, priority scheduling & peer trust cache ---------------------------------------


class _Gate:
    __slots__ = ("lock", "turns")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.turns = 0


_GATES: dict[str, _Gate] = {}
_OFFERED_NEEDS: OrderedDict[str, set[str]] = OrderedDict()
_URGENT_NEEDS: OrderedDict[str, None] = OrderedDict()
_TRUSTED_PEERS: set[str] = set()
# Our own team's agents (asst/*/card.yaml) are always trusted, even when the Index does not
# list them for this role: otherwise our booking centre's confirm would be dropped silently.
_TEAM_AGENTS = frozenset({
    "agent:gdrf9b9kxxss",  # booking_centre
    "agent:nanpr5mgm2py",  # hospital
    "agent:c2ewegr2ea3v",  # gp
    "agent:atp772whjkgd",  # family_counseling
})
_TRUSTED_REFRESHED_AT: float = 0.0
_TRUSTED_LOCAL_MODE: bool = False
_INDEX_SHOWS_BOOKING_CENTRES: bool = False
_UNKNOWN_PEER_CHECKED: dict[str, float] = {}


class _PriorityScheduler:
    """Urgent-first scheduler: rank-0 (``priority=U`` / urgent) turns execute immediately
    and preempt normal turns (``B`` -> ``D`` -> ``P``), with a bounded wait cap so lower
    priorities never starve or miss the 10 s fast-reply window."""

    def __init__(self) -> None:
        self._cond: asyncio.Condition | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._urgent_active = 0
        self._normal_active = 0
        self._seq = 0
        self._waiters: list[tuple[int, int]] = []

    def _ensure_cond(self) -> asyncio.Condition:
        loop = asyncio.get_running_loop()
        if self._cond is None or self._loop is not loop:
            self._cond = asyncio.Condition()
            self._loop = loop
            self._urgent_active = 0
            self._normal_active = 0
            self._waiters = []
        return self._cond

    async def enter(self, rank: int) -> None:
        cond = self._ensure_cond()
        if rank == 0:
            async with cond:
                self._urgent_active += 1
            return

        async with cond:
            if (
                self._urgent_active == 0
                and self._normal_active < _MAX_NORMAL_CONCURRENCY
                and not self._waiters
            ):
                self._normal_active += 1
                return
            self._seq += 1
            ticket = (rank, self._seq)
            heapq.heappush(self._waiters, ticket)
            deadline = asyncio.get_running_loop().time() + _NORMAL_YIELD_CAP_S
            while True:
                if (
                    self._urgent_active == 0
                    and self._normal_active < _MAX_NORMAL_CONCURRENCY
                    and self._waiters
                    and self._waiters[0] == ticket
                ):
                    heapq.heappop(self._waiters)
                    self._normal_active += 1
                    return
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    try:
                        self._waiters.remove(ticket)
                        heapq.heapify(self._waiters)
                    except ValueError:
                        pass
                    self._normal_active += 1
                    return
                try:
                    await asyncio.wait_for(cond.wait(), timeout=remaining)
                except TimeoutError:
                    continue

    async def leave(self, rank: int) -> None:
        cond = self._ensure_cond()
        async with cond:
            if rank == 0:
                self._urgent_active = max(0, self._urgent_active - 1)
            else:
                self._normal_active = max(0, self._normal_active - 1)
            cond.notify_all()


_SCHEDULER = _PriorityScheduler()


def _mark_urgent(key: str) -> None:
    if not key:
        return
    _URGENT_NEEDS[key] = None
    _URGENT_NEEDS.move_to_end(key)
    while len(_URGENT_NEEDS) > _SENT_NEEDS:
        _URGENT_NEEDS.popitem(last=False)


def _is_urgent_need(key: str) -> bool:
    return key in _URGENT_NEEDS


def _turn_priority_rank(need: Need, message: Message, key: str) -> int:
    """0 = urgent (U), 1 = high (B / confirm / checkin / ledger), 2 = normal (D), 3 = low (P)."""
    if need.is_emergency or _is_urgent_need(key):
        return 0
    fields = msg_fields(message)
    prio = fields.get("priority", "").upper()
    if prio == "U" or fields.get("urgent", "").lower() in {"true", "yes", "1"}:
        _mark_urgent(key)
        return 0
    if fields.get("urgency", "").lower() in {"high", "urgent", "u"}:
        _mark_urgent(key)
        return 0
    w = words(message.text)
    if message.sender != LEDGER and (w & _URGENT_WORDS):
        _mark_urgent(key)
        return 0
    if prio == "B" or "confirm" in w or "checkin" in w or message.sender == LEDGER:
        return 1
    if prio == "P":
        return 3
    return 2


def _offered_cache(key: str) -> set[str]:
    entry = _OFFERED_NEEDS.get(key)
    if entry is None:
        entry = _OFFERED_NEEDS[key] = set()
        while len(_OFFERED_NEEDS) > _SENT_NEEDS:
            _OFFERED_NEEDS.popitem(last=False)
    else:
        _OFFERED_NEEDS.move_to_end(key)
    return entry


async def _is_trusted_agent(city: City, sender_id: str) -> bool:
    """Trust ``city:ledger``, own team agents, and peers returned by ``city.find``.

    Note: on the live Index, a ``facility`` calling ``city.find("booking_centre")`` may receive
    zero ``booking_centre`` peers (role-scoped discovery). When the Index does not expose
    ``booking_centre`` peers to this facility, Relay-authenticated ``agent:*`` callers are
    still allowed for standard care acts (the Ledger enforces all booking/consent rules).
    """
    global _TRUSTED_REFRESHED_AT, _TRUSTED_LOCAL_MODE, _INDEX_SHOWS_BOOKING_CENTRES

    if sender_id == LEDGER or sender_id in _TEAM_AGENTS:
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
        return not _INDEX_SHOWS_BOOKING_CENTRES

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
            # Level 8 impostor hardening: only trust peers with valid care roles.
            if peer.role and peer.role not in {
                "booking_centre",
                "facility",
                "hospital",
                "specialist_care",
                "family_counseling",
                "gp",
            }:
                continue
            discovered.add(peer.agent_id)

    _TRUSTED_REFRESHED_AT = now
    _INDEX_SHOWS_BOOKING_CENTRES = saw_bc
    if discovered:
        _TRUSTED_PEERS.clear()
        _TRUSTED_PEERS.update(discovered)
        # In ``city run`` local chat, MemoryIndex returns only the stand-in peer agent:t05/osp-x
        # while the simulated agent character sends as agent:local/peer.
        _TRUSTED_LOCAL_MODE = discovered == {"agent:t05/osp-x"}

    if sender_id in _TRUSTED_PEERS:
        _UNKNOWN_PEER_CHECKED.pop(sender_id, None)
        return True
    if _TRUSTED_LOCAL_MODE and sender_id == "agent:local/peer":
        return True
    if not _INDEX_SHOWS_BOOKING_CENTRES:
        # Index hides booking_centre peers from facilities: allow Relay-authenticated partner BCs.
        return True

    _UNKNOWN_PEER_CHECKED[sender_id] = now
    _LOG.warning("specialist.untrusted_agent", extra={"sender": sender_id})
    return False


# --- entry point -------------------------------------------------------------------------------


async def main(need: Need, city: City) -> None:
    message = city.inbound
    if message is None:
        return
    if message.sender == city.agent_id:
        return
    if _guard.untargeted_request(message, need, ("cardio", "specialist", "vis-card", "cardiol")):
        return

    text = message.text or need.summary
    if text == "__error__" or need.summary == "__error__":
        raise RuntimeError("application")

    # Red-flag emergency bypasses all queues and stops immediately after escalation.
    if message.sender.startswith("citizen:") and _is_emergency(need, message):
        case = await city.history(need_id=message.need_id)
        if not _we(
            case,
            city.agent_id,
            "escalate.emergency",
            to=message.sender,
            word="escalate.emergency",
        ):
            english = _is_english(case, text)
            msg = (
                "escalate.emergency reason=red_flag "
                "Your symptoms indicate an emergency. Please call 112 immediately "
                "or go to the nearest emergency room."
                if english
                else "escalate.emergency reason=red_flag "
                "I sintomi indicano un'emergenza. Chiami subito il 112 "
                "o si rechi immediatamente al pronto soccorso."
            )
            await _say(city, message.sender, msg, allowed_citizen=message.sender)
        return

    if message.sender.startswith("citizen:") and clinical(text):
        raise OutOfScope()

    # Level 8 impostor defense: ignore untrusted agent:* senders not listed on the Index.
    if message.sender.startswith("agent:"):
        if not await _is_trusted_agent(city, message.sender):
            return

    street = _own_street(city)
    if street and _asks_address(message):
        await _say(city, message.sender, f"address={street}")
        return

    key = message.need_id or message.msg_id or "default"
    rank = _turn_priority_rank(need, message, key)
    await _SCHEDULER.enter(rank)
    try:
        gate = _GATES.get(key)
        if gate is None:
            gate = _GATES[key] = _Gate()
        gate.turns += 1
        try:
            async with gate.lock:
                case = await city.history(need_id=message.need_id)
                if message.sender.startswith("citizen:") and _we(
                    case,
                    city.agent_id,
                    "escalate.emergency",
                    to=message.sender,
                    word="escalate.emergency",
                ):
                    # Already escalated red flag on this need: stop sending anything else.
                    return
                rx_prev = _ledger_fields(case, "prescription")
                if rx_prev and rx_prev.get("priority", "").upper() == "U":
                    _mark_urgent(key)
                await _dispatch(city, message, case, key)
        finally:
            gate.turns -= 1
            if gate.turns == 0 and _GATES.get(key) is gate:
                del _GATES[key]
    finally:
        await _SCHEDULER.leave(rank)


# --- routing -----------------------------------------------------------------------------------


async def _dispatch(city: City, message: Message, chat: list[Message], key: str) -> None:
    head = first_word(message.text)
    kind = words(message.text)
    parsed = understood(message)

    # 1. Ledger messages
    if message.sender == LEDGER:
        if head == "held":
            await _on_held(city, message, chat)
        elif head == "committed":
            await _on_committed(city, message, chat)
        elif head == "checked_in":
            await _on_checked_in(city, message, chat)
        elif head == "reported":
            await _on_reported(city, message, chat)
        elif head == "prescribed":
            await _on_prescribed(city, message, chat)
        elif head == "refused":
            await _on_ledger_refused(city, message, chat)
        elif head in {"slots", "world", "agenda", "prescription"}:
            await _resume_pending_offers(city, message, chat, key)
        return

    # 2. Check-in from citizen
    if message.sender.startswith("citizen:") and (
        parsed.type == "checkin" or "checkin" in kind
    ):
        await _on_checkin(city, message, chat)
        return

    # 3. Citizen reply while waiting for report_sharing consent after check-in
    #    (Checked before confirm so refusals like "I do not confirm sharing" are not misrouted.)
    fields = msg_fields(message)
    if (
        message.sender.startswith("citizen:")
        and _awaiting_report_consent(chat, city.agent_id)
        and "slot_id" not in fields
        and "offer_id" not in fields
    ):
        await _on_report_consent_reply(city, message, chat)
        return

    # 4. Booking confirmation from trusted booking_centre agent or citizen
    if (parsed.type == "confirm" or "confirm" in kind) and (
        message.sender.startswith("agent:") or message.sender.startswith("citizen:")
    ):
        await _on_confirm(city, message, chat)
        return

    # 5. Availability inquiry from a trusted booking_centre (own or cross-ASST partner)
    if message.sender.startswith("agent:"):
        if parsed.type == "cancel" or "cancel" in kind:
            return
        if head in {"ack", "handoff", "booking.notice"}:
            # A notice or an acknowledgement is not an availability request: no ping-pong.
            return
        await _on_agent_availability_request(city, message, chat, key)
        return

    # 6. Direct citizen request (prescription visit or general inquiry — never leave unanswered)
    if message.sender.startswith("citizen:"):
        await _on_citizen_message(city, message, chat, key)


# --- language, empathy & safety helpers --------------------------------------------------------


# Message and slot helpers live in focused modules.
_TEXT_HELPERS = (
    "words first_word fields_of msg_fields _safe_token _when _is_emergency _is_english "
    "_is_anxious _empathy_lead _asks_address _own_street _citizen _we _exact "
    "_strip_foreign_citizens _say _latest_ledger _latest_slots _agenda_now _ledger_fields "
    "_poll_pause _await_line _until"
).split()
for _name in _TEXT_HELPERS:
    globals()[_name] = getattr(_text, _name)
_SLOT_HELPERS = (
    "_extract_refusal_reason _normalize_day _extract_nl_prefs _diversify_by_doctor "
    "_speaks _fitting_slots _page_info _asked_world _offer_line _fetch_agenda_and_world"
).split()
for _name in _SLOT_HELPERS:
    globals()[_name] = getattr(_slots, _name)

# Flow handlers live in focused modules; aliases keep routing and cross-flow calls clear.
_on_agent_availability_request = _availability._on_agent_availability_request
_on_citizen_message = _availability._on_citizen_message
_offer_direct_to_citizen = _availability._offer_direct_to_citizen
_resume_pending_offers = _availability._resume_pending_offers
_our_offer = _booking._our_offer
_booking_context = _booking._booking_context
_latest_hold_info = _booking._latest_hold_info
_hold_slot = _booking._hold_slot
_our_commit = _booking._our_commit
_on_confirm = _booking._on_confirm
_receipt_text = _booking._receipt_text
_on_held = _booking._on_held
_on_committed = _booking._on_committed
_on_booking_refused = _booking._on_booking_refused

# --- check-in, report sharing consent, CTRL-CARD follow-up, and report -------------------------

# These handlers live in specialist_reports.py; keep the established names here for
# routing and for helpers used elsewhere in the specialist brain.
_checked_in_booking_id = _report_state._checked_in_booking_id
_checked_in_index = _report_state._checked_in_index
_booking_has_report = _report_state._booking_has_report
_reported_booking_id = _report_state._reported_booking_id
_asked_report_consent = _report_state._asked_report_consent
_awaiting_report_consent = _report_state._awaiting_report_consent
_consent_explicitly_declined = _report_state._consent_explicitly_declined
_report_consent_ref = _report_state._report_consent_ref
_booking_prescription_number = _report_state._booking_prescription_number
_follow_up_rx_number = _report_state._follow_up_rx_number
_needs_follow_up = _report_state._needs_follow_up
_on_checkin = _reports._on_checkin
_on_checked_in = _reports._on_checked_in
_after_checked_in = _reports._after_checked_in
_on_report_consent_reply = _reports._on_report_consent_reply
_file_report = _reports._file_report
_issue_follow_up_prescription = _reports._issue_follow_up_prescription
_on_prescribed = _reports._on_prescribed
_on_reported = _reports._on_reported
_on_ledger_refused = _reports._on_ledger_refused

_text.configure(globals())
_slots.configure(globals())
_availability.configure(globals())
_booking.configure(globals())
_report_state.configure(globals())
_reports.configure(globals())
