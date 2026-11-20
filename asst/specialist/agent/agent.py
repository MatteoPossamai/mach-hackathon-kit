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
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
_PULL_TRIES = 16
_PULL_PAUSE = 0.2
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
    """Trust only ``city:ledger`` and agents returned by ``city.find`` (Level 8 impostor guard)."""
    global _TRUSTED_REFRESHED_AT, _TRUSTED_LOCAL_MODE

    if sender_id == LEDGER or sender_id in _TEAM_AGENTS:
        return True
    if not sender_id.startswith("agent:"):
        return False

    now = time.monotonic()
    if sender_id in _TRUSTED_PEERS and (now - _TRUSTED_REFRESHED_AT) < _PEER_CACHE_TTL_S:
        return True
    if _TRUSTED_LOCAL_MODE and sender_id == "agent:local/peer":
        return True

    last_miss = _UNKNOWN_PEER_CHECKED.get(sender_id, 0.0)
    if (now - last_miss) < _PEER_NEG_TTL_S and (now - _TRUSTED_REFRESHED_AT) < _PEER_CACHE_TTL_S:
        return False

    discovered: set[str] = set()
    for query in ("booking_centre", "facility"):
        try:
            peers = await city.find(query)
        except CapError:
            continue
        for peer in peers:
            if not peer.agent_id:
                continue
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

    if discovered:
        _TRUSTED_PEERS.clear()
        _TRUSTED_PEERS.update(discovered)
        _TRUSTED_REFRESHED_AT = now
        # In ``city run`` local chat, MemoryIndex returns only the stand-in peer agent:t05/osp-x
        # while the simulated agent character sends as agent:local/peer.
        _TRUSTED_LOCAL_MODE = discovered == {"agent:t05/osp-x"}

    if sender_id in _TRUSTED_PEERS:
        _UNKNOWN_PEER_CHECKED.pop(sender_id, None)
        return True
    if _TRUSTED_LOCAL_MODE and sender_id == "agent:local/peer":
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

    text = message.text or need.summary
    if text == "__error__" or need.summary == "__error__":
        raise RuntimeError("application")

    if message.sender != LEDGER and clinical(text):
        raise OutOfScope()

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
        await _on_agent_availability_request(city, message, chat, key)
        return

    # 6. Direct citizen request (prescription visit or general inquiry — never leave unanswered)
    if message.sender.startswith("citizen:"):
        await _on_citizen_message(city, message, chat, key)


# --- language, empathy & safety helpers --------------------------------------------------------


def words(text: str) -> set[str]:
    return {token.strip(".,;:!?()\"'").lower() for token in text.split() if token.strip()}


def first_word(text: str) -> str:
    parts = text.split()
    return parts[0].strip(".,;:!?()\"'").lower() if parts else ""


def fields_of(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for token in text.replace(",", " ").split():
        key, eq, value = token.partition("=")
        if not eq or not key or not value:
            continue
        cleaned = value.strip(".,;:!?\"'()")
        if key not in found and cleaned:
            found[key] = cleaned
    return found


def msg_fields(message: Message) -> dict[str, str]:
    """Merge ``key=value`` tokens from ``message.text`` with scalar keys in ``message.body``."""
    found = fields_of(message.text)
    for key, value in message.body.items():
        if key == "text" or key in found:
            continue
        if isinstance(value, bool):
            found[key] = "true" if value else "false"
        elif isinstance(value, (str, int, float)):
            cleaned = str(value).strip()
            if cleaned:
                found[key] = cleaned
    return found


def _safe_token(value: str | None) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 128 or not _TOKEN_SAFE.fullmatch(cleaned):
        return ""
    return cleaned


def _when(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _is_emergency(need: Need, message: Message) -> bool:
    if need.is_emergency or understood(message).body.get("is_emergency") is True:
        return True
    lowered = (message.text or need.summary).lower()
    return any(flag in lowered for flag in _RED_FLAGS)


def _is_english(chat: list[Message], text: str = "") -> bool:
    for row in reversed(chat):
        if row.sender.startswith("citizen:"):
            lang = msg_fields(row).get("lang", "").lower()
            if lang == "en":
                return True
            if lang == "it":
                return False
    combined = text or " ".join(r.text for r in chat if r.sender.startswith("citizen:"))
    return len(words(combined) & _ENGLISH_WORDS) >= 2


def _is_anxious(chat: list[Message], text: str = "") -> bool:
    samples = [text] if text else []
    samples.extend(row.text for row in chat if row.sender.startswith("citizen:"))
    joined = " ".join(samples).lower()
    return any(stem in joined for stem in _WORRIED_STEMS)


def _empathy_lead(*, english: bool, anxious: bool) -> str:
    if not anxious:
        return ""
    return (
        "I understand your concern, we are here to help. "
        if english
        else "Capisco la sua preoccupazione, siamo qui per aiutarla. "
    )


def _asks_address(message: Message) -> bool:
    text = message.text
    if text:
        named = False
        asked = False
        for token in text.split():
            bare = token.strip(".,;:!?()\"'")
            if bare == "data.request":
                named = True
            key, eq, value = bare.partition("=")
            if eq and key == "fields":
                names = {part.strip() for part in value.split(",") if part.strip()}
                asked = asked or "address" in names
        return named and asked
    if message.type != "data.request":
        return False
    raw = message.body.get("fields")
    if isinstance(raw, list):
        return "address" in raw
    if isinstance(raw, str):
        return "address" in {
            part.strip() for part in raw.replace(",", " ").split() if part.strip()
        }
    return False


def _own_street(city: City) -> str:
    street = getattr(city, "address", "")
    if isinstance(street, str) and street.strip():
        return street.strip()
    env = os.environ.get("CITY_ADDRESS", "").strip()
    if env:
        return env
    path = Path(__file__).with_name("door.txt")
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _citizen(chat: list[Message]) -> str | None:
    for row in chat:
        if row.sender.startswith("citizen:"):
            return row.sender
    for row in chat:
        fields = msg_fields(row)
        for key in ("citizen", "patient"):
            party = _safe_token(fields.get(key))
            if party.startswith("citizen:"):
                return party
    return None


def _we(chat: list[Message], agent: str, token: str, *, to: str, word: str) -> bool:
    for row in chat:
        if row.sender != agent or row.recipient != to:
            continue
        if token in row.text.split() and word in words(row.text):
            return True
    return False


def _exact(chat: list[Message], agent: str, text: str) -> bool:
    return any(row.sender == agent and row.text.strip() == text for row in chat)


def _strip_foreign_citizens(text: str, allowed_citizen: str | None) -> str:
    """Prevent over-disclosure: never let another patient's ``citizen:*`` ID leave this agent."""
    def _replace(match: re.Match[str]) -> str:
        found = match.group(0)
        if allowed_citizen and found == allowed_citizen:
            return found
        return "[redacted]"

    return _CITIZEN_ID_RE.sub(_replace, text)


async def _say(
    city: City,
    to: str,
    text: str,
    *,
    allowed_citizen: str | None = None,
) -> bool:
    if to != LEDGER:
        permitted = allowed_citizen or (to if to.startswith("citizen:") else None)
        text = _strip_foreign_citizens(text, permitted)
    try:
        await city.say(to, text[:2000])
    except CapError as exc:
        _LOG.warning("specialist.say.failed", extra={"code": exc.code, "to": to})
        return False
    return True


def _latest_ledger(chat: list[Message], head: str, token: str = "") -> Message | None:
    found: Message | None = None
    for row in chat:
        if row.sender != LEDGER:
            continue
        if first_word(row.text) != head:
            continue
        if token and token not in row.text.split():
            continue
        found = row
    return found


def _latest_slots(chat: list[Message]) -> str | None:
    found: str | None = None
    for row in chat:
        if row.sender != LEDGER:
            continue
        head = first_word(row.text)
        if head in {"slots", "world"} and ("slot_id=" in row.text or "slots=" in row.text):
            found = row.text
    return found


def _agenda_now(chat: list[Message]) -> datetime | None:
    for row in reversed(chat):
        if row.sender != LEDGER:
            continue
        if first_word(row.text) == "agenda":
            parsed = _when(fields_of(row.text).get("agenda_now", ""))
            if parsed is not None:
                return parsed
        if row.text.startswith("local ledger:"):
            return datetime.now(UTC)
    return None


def _ledger_fields(chat: list[Message], head: str) -> dict[str, str] | None:
    row = _latest_ledger(chat, head)
    return None if row is None else fields_of(row.text)


def _poll_pause(need_id: str | None) -> float:
    return _URGENT_PULL_PAUSE if (need_id and _is_urgent_need(need_id)) else _PULL_PAUSE


async def _await_line(
    city: City,
    need_id: str | None,
    chat: list[Message],
    heads: str | tuple[str, ...],
    token: str = "",
    *,
    op: str,
) -> tuple[Message, list[Message]] | None:
    wanted = (heads,) if isinstance(heads, str) else heads
    known = {row.msg_id for row in chat}
    fresh = chat
    pause = _poll_pause(need_id)
    for attempt in range(_PULL_TRIES):
        try:
            fresh = await city.history(need_id=need_id)
        except CapError:
            fresh = chat
        for head in wanted:
            hit = _latest_ledger(fresh, head, token)
            if hit is not None:
                return hit, fresh
        for row in fresh:
            if row.msg_id in known or row.sender != LEDGER:
                continue
            if first_word(row.text) == "refused" and (not op or op in words(row.text)):
                return row, fresh
            if row.text.startswith("local ledger:"):
                return row, fresh
        if attempt + 1 < _PULL_TRIES:
            await asyncio.sleep(pause)
    return None


async def _until(
    city: City,
    need_id: str | None,
    chat: list[Message],
    ready: Callable[[list[Message]], bool],
) -> list[Message]:
    fresh = chat
    pause = _poll_pause(need_id)
    for attempt in range(_PULL_TRIES):
        try:
            fresh = await city.history(need_id=need_id)
        except CapError:
            fresh = chat
        if ready(fresh):
            return fresh
        if attempt + 1 < _PULL_TRIES:
            await asyncio.sleep(pause)
    return fresh


def _extract_refusal_reason(text: str) -> str:
    fields = fields_of(text)
    for key in ("reason", "code", "error"):
        val = _safe_token(fields.get(key))
        if val:
            return val
    for match in _REFUSAL_CODE_RE.finditer(text):
        code = match.group(1)
        if code not in {"LEDGER", "VIS", "CTRL", "CARD"}:
            return code
    return "facility_cancelled"


# --- slot filtering & availability offers ------------------------------------------------------


def _normalize_day(raw: str) -> str:
    cleaned = raw.strip().lower()
    return _DAY_ALIASES.get(cleaned, cleaned)


def _extract_nl_prefs(text: str) -> tuple[str | None, str | None, set[str] | None]:
    """Extract (lang, gender, days) from natural-language IT/EN preferences (Levels 3 & 5)."""
    lowered = text.lower()
    toks = words(text)

    lang: str | None = None
    if toks & {"english", "inglese"}:
        lang = "en"
    elif toks & {"italian", "italiano"}:
        lang = "it"

    gender: str | None = None
    if any(
        phrase in lowered
        for phrase in ("dottoressa", "medico donna", "donna", "femmina", "female doctor", "female", "woman")
    ):
        gender = "f"
    elif any(
        phrase in lowered
        for phrase in ("medico uomo", "dottore uomo", "maschio", "male doctor", "male")
    ):
        gender = "m"

    days: set[str] = set()
    for alias, canonical in _DAY_ALIASES.items():
        if len(alias) >= 4 and alias in toks:
            days.add(canonical)
    return lang, gender, (days or None)


def _diversify_by_doctor(slots: list[dict[str, str]], limit: int = 4) -> list[dict[str, str]]:
    """Round-robin across doctors (e.g. s1 and s2) so anonymous searches get both profiles."""
    by_doc: dict[str, list[dict[str, str]]] = {}
    doc_order: list[str] = []
    for slot in slots:
        doc = slot.get("doctor_id", "")
        if doc not in by_doc:
            by_doc[doc] = []
            doc_order.append(doc)
        by_doc[doc].append(slot)
    result: list[dict[str, str]] = []
    idx = 0
    while len(result) < limit:
        added = False
        for doc in doc_order:
            bucket = by_doc[doc]
            if idx < len(bucket):
                result.append(bucket[idx])
                added = True
                if len(result) >= limit:
                    break
        if not added:
            break
        idx += 1
    return result


def _speaks(doctor_id: str, lang: str) -> bool:
    if not lang:
        return True
    spec = _DOCTORS.get(doctor_id.rsplit("-", 1)[-1])
    if spec is None:
        return True
    langs = spec[0]
    if lang in langs:
        return True
    return "*" in langs and lang not in {"it", "en"}


def _fitting_slots(
    world_text: str,
    *,
    now: datetime,
    agent_id: str,
    priority: str | None,
    lang: str,
    gender: str | None = None,
    days: set[str] | None = None,
    procedure: str = PRIMARY_PROCEDURE,
) -> list[dict[str, str]]:
    prio = (priority or "").upper() or None
    norm_days = {_normalize_day(d) for d in days} if days else None

    if prio == "U":
        today_end: datetime | None = now.replace(hour=23, minute=59, second=59, microsecond=0)
        fallback_horizon: datetime | None = now + timedelta(days=3)
        earliest = now + _URGENT_LEAD
    elif prio == "P":
        today_end = None
        fallback_horizon = None
        earliest = now + _LEAD
    else:
        today_end = now + timedelta(days=_DEADLINE_DAYS.get(prio or "", 14))
        fallback_horizon = None
        earliest = now + _LEAD

    lines = [line for line in world_text.splitlines() if "slot_id=" in line]

    def _collect(horizon: datetime | None) -> list[dict[str, str]]:
        own_slots: list[tuple[int, datetime, dict[str, str]]] = []
        other_slots: list[tuple[int, datetime, dict[str, str]]] = []
        for line in lines:
            slot = fields_of(line)
            slot_id = _safe_token(slot.get("slot_id"))
            doctor = _safe_token(slot.get("doctor_id"))
            at_raw = slot.get("at") or slot.get("agenda_date", "")
            at = _when(at_raw)
            if not slot_id or not doctor or at is None:
                continue
            if procedure and slot.get("procedure") not in (None, procedure):
                continue
            free = slot.get("free")
            if free is not None and free.isdigit() and int(free) <= 0:
                continue
            is_reserve = slot.get("urgent_reserve", "").lower() == "true"
            # P3 rule: urgent_reserve slots (09:00) are strictly reserved for priority U.
            if is_reserve and prio != "U":
                continue
            if at <= earliest or (horizon is not None and at >= horizon):
                continue
            if not _speaks(doctor, lang):
                continue
            spec = _DOCTORS.get(doctor.rsplit("-", 1)[-1])
            day = _normalize_day(slot.get("day") or slot.get("agenda_day", ""))
            if spec is not None:
                if day and day not in spec[2]:
                    continue
                if gender and spec[1] != gender:
                    continue
            if norm_days and day and day not in norm_days:
                continue
            reserve_rank = 0 if (prio == "U" and is_reserve) else 1
            slot_facility = slot.get("facility", "")
            if slot_facility == agent_id:
                own_slots.append((reserve_rank, at, slot))
            elif not slot_facility or slot_facility.startswith("agent:"):
                other_slots.append((reserve_rank, at, slot))

        chosen = own_slots if own_slots else other_slots
        chosen.sort(key=lambda item: (item[0], item[1], item[2].get("slot_id", "")))
        return [slot for _, _, slot in chosen]

    primary = _collect(today_end)
    if primary or fallback_horizon is None:
        return primary
    return _collect(fallback_horizon)


def _page_info(text: str) -> tuple[int, int]:
    first_line = text.splitlines()[0] if text else ""
    raw = fields_of(first_line).get("page", "")
    left, _, right = raw.partition("/")
    if left.isdigit() and right.isdigit():
        return int(left), int(right)
    return 1, 1


def _asked_world(chat: list[Message], agent: str, procedure: str, page: int) -> bool:
    p_tok = f"page={page}"
    proc_tok = f"procedure={procedure}"
    return any(
        row.sender == agent
        and row.recipient == LEDGER
        and p_tok in row.text.split()
        and proc_tok in row.text.split()
        for row in chat
    )


def _offer_line(
    slot: dict[str, str],
    *,
    agent_id: str,
    procedure: str = PRIMARY_PROCEDURE,
    number: str | None = None,
    priority: str | None = None,
    request_id: str | None = None,
) -> str:
    facility = slot.get("facility") or agent_id
    at_val = slot.get("at") or slot.get("agenda_date", "")
    day_val = slot.get("day") or slot.get("agenda_day", "")
    parts = [
        "offer",
        f"offer_id=of-{slot['slot_id']}",
        f"slot_id={slot['slot_id']}",
        f"doctor_id={slot['doctor_id']}",
        f"facility={facility}",
        f"procedure={slot.get('procedure', procedure)}",
        f"agenda_date={at_val}",
        f"expiry={at_val}",
    ]
    if day_val:
        parts.append(f"agenda_day={day_val}")
    if priority:
        parts.append(f"priority={priority}")
    if number:
        parts.append(f"prescription_number={number}")
    if request_id:
        parts.append(f"request_id={request_id}")
    return " ".join(parts)


async def _fetch_agenda_and_world(
    city: City,
    need_id: str | None,
    chat: list[Message],
    procedure: str,
) -> tuple[datetime | None, str | None, list[Message]]:
    agent = city.agent_id
    now = _agenda_now(chat)
    world = _latest_slots(chat)
    if now is not None and world is not None:
        return now, world, chat

    if now is None and not _exact(chat, agent, "agenda"):
        await _say(city, LEDGER, "agenda")
    if world is None and not _asked_world(chat, agent, procedure, 1):
        await _say(city, LEDGER, f"world procedure={procedure} page=1")

    fresh = await _until(
        city,
        need_id,
        chat,
        lambda rows: _agenda_now(rows) is not None and _latest_slots(rows) is not None,
    )
    return _agenda_now(fresh), _latest_slots(fresh), fresh


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
        for slot in selected:
            line = _offer_line(
                slot,
                agent_id=city.agent_id,
                procedure=requested_proc,
                priority=priority,
                request_id=request_id or None,
            )
            if await _say(city, message.sender, line):
                cache.add(slot["slot_id"])
        return

    if not cache:
        parts = ["cancel"]
        if request_id:
            parts.append(f"request_id={request_id}")
        parts.append("reason=no_slots_available")
        parts.append(f"Nessuno slot {requested_proc} disponibile per i criteri richiesti.")
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


def _our_offer(chat: list[Message], agent: str, offer_id: str) -> dict[str, str]:
    token = f"offer_id={offer_id}"
    for row in chat:
        if row.sender != agent or "offer" not in words(row.text):
            continue
        for line in row.text.splitlines():
            if token in line.split():
                return fields_of(line)
    return {}


def _booking_context(
    chat: list[Message], agent: str, slot_id: str
) -> tuple[str, str, str | None, str, str]:
    """Return ``(requester, consent_ref, prescription_number, offer_id, cross_asst_consent_ref)``."""
    token = f"slot_id={slot_id}"
    requester = ""
    consent = ""
    cross_consent = ""
    number: str | None = None
    offer_id = ""
    for row in chat:
        fields = msg_fields(row)
        row_offer = _safe_token(fields.get("offer_id"))
        if token in row.text.split() or (
            row_offer and _our_offer(chat, agent, row_offer).get("slot_id") == slot_id
        ):
            if "confirm" in words(row.text) and row.sender not in (LEDGER, agent):
                requester = row.sender
                consent = _safe_token(fields.get("consent_ref")) or consent
                cross_consent = (
                    _safe_token(fields.get("cross_asst_consent_ref") or fields.get("cross_asst_consent"))
                    or cross_consent
                )
                number = _safe_token(fields.get("prescription_number")) or number
                offer_id = row_offer or offer_id
            elif row.sender == agent and row.text.strip().startswith("hold "):
                consent = _safe_token(fields.get("consent_ref")) or consent
                cross_consent = (
                    _safe_token(fields.get("cross_asst_consent_ref")) or cross_consent
                )
                number = _safe_token(fields.get("prescription_number")) or number
                requester = requester or _safe_token(fields.get("citizen"))
            elif row.sender == agent and "offer" in words(row.text):
                offer_id = row_offer or offer_id
    return requester, consent, number, offer_id, cross_consent


def _latest_hold_info(chat: list[Message], agent: str) -> tuple[str, str, str | None, str]:
    slot = ""
    consent = ""
    cross_consent = ""
    number: str | None = None
    for row in chat:
        if row.sender != agent or row.recipient != LEDGER:
            continue
        if not row.text.strip().startswith("hold "):
            continue
        fields = fields_of(row.text)
        slot = _safe_token(fields.get("slot_id")) or slot
        consent = _safe_token(fields.get("consent_ref")) or consent
        cross_consent = _safe_token(fields.get("cross_asst_consent_ref")) or cross_consent
        number = _safe_token(fields.get("prescription_number")) or number
    return slot, consent, number, cross_consent


def _hold_slot(chat: list[Message], agent: str, hold_id: str) -> str:
    token = f"hold_id={hold_id}"
    for row in chat:
        if token in row.text.split() and row.sender in (agent, LEDGER):
            slot = _safe_token(fields_of(row.text).get("slot_id"))
            if slot:
                return slot
    return ""


def _our_commit(chat: list[Message], agent: str, hold_id: str) -> bool:
    token = f"hold_id={hold_id}"
    return any(
        row.sender == agent
        and row.recipient == LEDGER
        and row.text.strip().startswith("commit ")
        and token in row.text.split()
        for row in chat
    )


async def _on_confirm(city: City, message: Message, chat: list[Message]) -> None:
    agent = city.agent_id
    fields = msg_fields(message)
    offer_id = _safe_token(fields.get("offer_id"))
    offer = _our_offer(chat, agent, offer_id) if offer_id else {}
    slot = _safe_token(fields.get("slot_id") or offer.get("slot_id"))
    consent = _safe_token(fields.get("consent_ref"))
    cross_consent = _safe_token(
        fields.get("cross_asst_consent_ref") or fields.get("cross_asst_consent")
    )
    citizen = _safe_token(
        fields.get("citizen")
        or (message.sender if message.sender.startswith("citizen:") else "")
        or _citizen(chat)
    )
    number = _safe_token(fields.get("prescription_number") or offer.get("prescription_number"))

    if not slot:
        parts = ["cancel"]
        if offer_id:
            parts.append(f"offer_id={offer_id}")
        parts.append("reason=missing_slot_id")
        parts.append("Manca slot_id per completare la prenotazione.")
        await _say(city, message.sender, " ".join(parts), allowed_citizen=citizen or None)
        return

    # Idempotency on duplicate confirm: return existing receipt without double-holding.
    existing_commit = _latest_ledger(chat, "committed")
    if existing_commit is not None:
        c_fields = fields_of(existing_commit.text)
        bk = _safe_token(c_fields.get("booking_id"))
        ref = _safe_token(c_fields.get("ledger_ref"))
        c_slot = _safe_token(c_fields.get("slot_id")) or _hold_slot(
            chat, agent, _safe_token(c_fields.get("hold_id")) or bk
        )
        if bk and ref and (not c_slot or c_slot == slot):
            if not _we(chat, agent, f"booking_id={bk}", to=message.sender, word="booking.receipt"):
                await _say(
                    city,
                    message.sender,
                    _receipt_text(bk, ref, chat=chat, to_citizen=message.sender.startswith("citizen:")),
                    allowed_citizen=citizen or None,
                )
            return

    if not _we(chat, agent, f"slot_id={slot}", to=LEDGER, word="hold"):
        parts = ["hold", f"slot_id={slot}"]
        if citizen:
            parts.append(f"citizen={citizen}")
        if number:
            parts.append(f"prescription_number={number}")
        if consent:
            parts.append(f"consent_ref={consent}")
        if cross_consent:
            parts.append(f"cross_asst_consent_ref={cross_consent}")
        if not await _say(city, LEDGER, " ".join(parts)):
            return

    found = await _await_line(city, message.need_id, chat, "held", f"slot_id={slot}", op="hold")
    if found is None:
        _LOG.info("specialist.held.missing", extra={"need_id": (message.need_id or "")[:48]})
        return
    reply, fresh = found
    if first_word(reply.text) == "refused":
        await _on_booking_refused(
            city, reply, fresh, slot=slot, offer_id=offer_id, requester=message.sender
        )
        return
    if first_word(reply.text) != "held":
        return

    await _on_held(city, reply, fresh)
    hold_id = _safe_token(fields_of(reply.text).get("hold_id"))
    if not hold_id:
        return

    done = await _await_line(
        city, message.need_id, fresh, "committed", f"booking_id={hold_id}", op="commit"
    )
    if done is None:
        _LOG.info("specialist.commit.missing", extra={"need_id": (message.need_id or "")[:48]})
        return
    c_reply, c_fresh = done
    if first_word(c_reply.text) == "committed":
        await _on_committed(city, c_reply, c_fresh)
    elif first_word(c_reply.text) == "refused":
        # Level 6 recovery: if the hold expired before commit, retry one fresh hold + commit.
        if _extract_refusal_reason(c_reply.text) == "HOLD_EXPIRED":
            retry_parts = ["hold", f"slot_id={slot}"]
            if citizen:
                retry_parts.append(f"citizen={citizen}")
            if number:
                retry_parts.append(f"prescription_number={number}")
            if consent:
                retry_parts.append(f"consent_ref={consent}")
            if cross_consent:
                retry_parts.append(f"cross_asst_consent_ref={cross_consent}")
            if await _say(city, LEDGER, " ".join(retry_parts)):
                re_held = await _await_line(
                    city, message.need_id, c_fresh, "held", f"slot_id={slot}", op="hold"
                )
                if re_held is not None and first_word(re_held[0].text) == "held":
                    new_hid = _safe_token(fields_of(re_held[0].text).get("hold_id"))
                    if new_hid and new_hid != hold_id:
                        await _on_held(city, re_held[0], re_held[1])
                        re_done = await _await_line(
                            city,
                            message.need_id,
                            re_held[1],
                            "committed",
                            f"booking_id={new_hid}",
                            op="commit",
                        )
                        if re_done is not None and first_word(re_done[0].text) == "committed":
                            await _on_committed(city, re_done[0], re_done[1])
                            return
        await _on_booking_refused(
            city, c_reply, c_fresh, slot=slot, offer_id=offer_id, requester=message.sender
        )


def _receipt_text(
    booking_id: str, ledger_ref: str, *, chat: list[Message], to_citizen: bool
) -> str:
    if to_citizen:
        english = _is_english(chat)
        lead = _empathy_lead(english=english, anxious=_is_anxious(chat))
        note = (
            f"{lead}Your cardiology appointment is confirmed. Please keep this receipt for check-in."
            if english
            else f"{lead}La sua prenotazione cardiologica è confermata. Conservi questa ricevuta per il check-in."
        )
    else:
        note = "Prenotazione confermata per la visita specialistica cardiologica."
    return f"booking.receipt booking_id={booking_id} ledger_ref={ledger_ref} {note}"


async def _on_held(city: City, message: Message, chat: list[Message]) -> None:
    fields = fields_of(message.text)
    hold_id = _safe_token(fields.get("hold_id"))
    slot = _safe_token(fields.get("slot_id"))
    if not hold_id or not slot:
        return
    if _our_commit(chat, city.agent_id, hold_id):
        return

    _, consent, number, _, cross_consent = _booking_context(chat, city.agent_id, slot)
    if not consent:
        _, held_consent, held_number, held_cross = _latest_hold_info(chat, city.agent_id)
        consent = held_consent
        number = number or held_number
        cross_consent = cross_consent or held_cross
    need_ref = _safe_token(fields.get("need_id") or message.need_id)
    if not consent and need_ref.startswith("N-"):
        consent = f"cons-{need_ref}-booking"
    if not consent:
        _LOG.info("specialist.held.noconsent", extra={"need_id": need_ref[:48]})
        return

    fence = _safe_token(fields.get("fence")) or "1"
    parts = [
        "commit",
        f"hold_id={hold_id}",
        f"consent_ref={consent}",
        f"fence={fence}",
    ]
    if number:
        parts.append(f"prescription_number={number}")
    if cross_consent:
        parts.append(f"cross_asst_consent_ref={cross_consent}")
    await _say(city, LEDGER, " ".join(parts))


async def _on_committed(city: City, message: Message, chat: list[Message]) -> None:
    fields = fields_of(message.text)
    booking_id = _safe_token(fields.get("booking_id"))
    ledger_ref = _safe_token(fields.get("ledger_ref"))
    if not booking_id or not ledger_ref:
        return

    slot = _safe_token(fields.get("slot_id")) or _hold_slot(
        chat, city.agent_id, _safe_token(fields.get("hold_id")) or booking_id
    )
    requester, _, _, _, _ = (
        _booking_context(chat, city.agent_id, slot) if slot else ("", "", None, "", "")
    )
    target = requester or _citizen(chat)
    if not target:
        return
    token = f"booking_id={booking_id}"
    if _we(chat, city.agent_id, token, to=target, word="booking.receipt"):
        return
    await _say(
        city,
        target,
        _receipt_text(booking_id, ledger_ref, chat=chat, to_citizen=target.startswith("citizen:")),
        allowed_citizen=_citizen(chat),
    )


async def _on_booking_refused(
    city: City,
    message: Message,
    chat: list[Message],
    *,
    slot: str = "",
    offer_id: str = "",
    requester: str = "",
) -> None:
    agent = city.agent_id
    if not slot:
        slot, _, _, _ = _latest_hold_info(chat, agent)
    if slot:
        ctx_req, _, _, ctx_offer, _ = _booking_context(chat, agent, slot)
        requester = requester or ctx_req
        offer_id = offer_id or ctx_offer
    target = requester or _citizen(chat)
    if not target:
        return

    reason = _extract_refusal_reason(message.text)
    dedup_tok = f"offer_id={offer_id}" if offer_id else f"slot_id={slot}"
    if dedup_tok and _we(chat, agent, dedup_tok, to=target, word="cancel"):
        return
    parts = ["cancel"]
    if offer_id:
        parts.append(f"offer_id={offer_id}")
    if slot:
        parts.append(f"slot_id={slot}")
    parts.append(f"reason={reason}")
    if target.startswith("citizen:") and _is_english(chat):
        parts.append(
            f"We could not confirm the requested slot ({reason}). "
            "Please select another available time or check the requirements."
        )
    else:
        parts.append(
            f"Non è stato possibile confermare lo slot richiesto ({reason}). "
            "È necessario selezionare un'altra disponibilità o verificare i requisiti."
        )
    await _say(city, target, " ".join(parts), allowed_citizen=_citizen(chat))


# --- check-in, report sharing consent, CTRL-CARD follow-up, and report -------------------------


def _checked_in_booking_id(chat: list[Message]) -> str:
    row = _latest_ledger(chat, "checked_in")
    if row is None:
        return ""
    return _safe_token(fields_of(row.text).get("booking_id"))


def _checked_in_index(chat: list[Message], booking_id: str) -> int:
    token = f"booking_id={booking_id}"
    found = -1
    for idx, row in enumerate(chat):
        if row.sender == LEDGER and first_word(row.text) == "checked_in" and token in row.text.split():
            found = idx
    return found


def _booking_has_report(chat: list[Message], booking_id: str) -> bool:
    if not booking_id:
        return False
    return _latest_ledger(chat, "reported", f"booking_id={booking_id}") is not None


def _reported_booking_id(chat: list[Message]) -> str:
    row = _latest_ledger(chat, "reported")
    if row is None:
        return ""
    return _safe_token(fields_of(row.text).get("booking_id"))


def _asked_report_consent(chat: list[Message], agent: str, booking_id: str = "") -> bool:
    start = _checked_in_index(chat, booking_id) if booking_id else -1
    for idx, row in enumerate(chat):
        if idx <= start:
            continue
        if (
            row.sender == agent
            and row.recipient.startswith("citizen:")
            and "purpose=report_sharing" in row.text.split()
            and "consent" in words(row.text)
        ):
            return True
    return False


def _awaiting_report_consent(chat: list[Message], agent: str) -> bool:
    booking_id = _checked_in_booking_id(chat)
    if not booking_id:
        return False
    if _booking_has_report(chat, booking_id):
        return False
    if _we(chat, agent, f"booking_id={booking_id}", to=LEDGER, word="report"):
        return False
    return _asked_report_consent(chat, agent, booking_id)


def _consent_explicitly_declined(fields: dict[str, str]) -> bool:
    for key in ("granted", "accepted", "consent", "allow", "allowed"):
        if fields.get(key, "").lower() in {"false", "no", "0", "denied", "refused"}:
            return True
    if fields.get("refused", "").lower() in {"true", "yes", "1"}:
        return True
    return False


def _report_consent_ref(chat: list[Message], agent: str, booking_id: str = "") -> str:
    """Return a citizen's ``report_sharing`` consent_ref granted for the current booking."""
    start = _checked_in_index(chat, booking_id) if booking_id else -1
    asked_idx = -1
    for idx, row in enumerate(chat):
        if idx <= start:
            continue
        if (
            row.sender == agent
            and row.recipient.startswith("citizen:")
            and "purpose=report_sharing" in row.text.split()
        ):
            asked_idx = idx
    for idx, row in enumerate(chat):
        if idx <= start:
            continue
        if not row.sender.startswith("citizen:"):
            continue
        fields = msg_fields(row)
        ref = _safe_token(fields.get("consent_ref"))
        if not ref or _consent_explicitly_declined(fields):
            continue
        purpose = _safe_token(fields.get("purpose"))
        if purpose == "report_sharing" or (asked_idx >= 0 and idx > asked_idx):
            return ref
    return ""


def _booking_prescription_number(chat: list[Message], booking_id: str = "") -> str:
    """Return the prescription_number associated with ``booking_id`` (including CTRL-CARD follow-ups)."""
    if booking_id:
        for row in reversed(chat):
            toks = row.text.split()
            if f"hold_id={booking_id}" in toks or f"booking_id={booking_id}" in toks:
                num = _safe_token(msg_fields(row).get("prescription_number"))
                if num:
                    return num
        slot = ""
        for row in reversed(chat):
            if f"hold_id={booking_id}" in row.text.split():
                slot = _safe_token(fields_of(row.text).get("slot_id"))
                if slot:
                    break
        if slot:
            for row in reversed(chat):
                if f"slot_id={slot}" in row.text.split():
                    num = _safe_token(msg_fields(row).get("prescription_number"))
                    if num:
                        return num
    for row in reversed(chat):
        if row.sender == LEDGER and first_word(row.text) == "prescribed":
            continue
        num = _safe_token(msg_fields(row).get("prescription_number"))
        if num:
            return num
    return ""


def _follow_up_rx_number(booking_id: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in booking_id.upper()).strip("-")
    if len(cleaned) < 2:
        cleaned = (cleaned + "00")[:2]
    candidate = f"prescription_number-FU-{cleaned}"[:60]
    if _RX_RE.fullmatch(candidate):
        return candidate
    return "prescription_number-FU-CTRL"


def _needs_follow_up(chat: list[Message], rx_info: dict[str, str] | None) -> bool:
    """Decide whether a ``CTRL-CARD`` follow-up prescription is required by protocol.

    - Never chain a second follow-up on a ``CTRL-CARD`` visit, an existing
      ``follow_up_of`` prescription, or a routine ``SYM-FOLLOWUP`` visit.
    - Issue a ``CTRL-CARD`` follow-up when the visit is an initial ``VIS-CARD``
      visit (priority U/B/D, e.g. ``SYM-PALPITATIONS``), or when the case
      explicitly asks for a follow-up check.
    """
    if rx_info:
        if rx_info.get("follow_up_of"):
            return False
        if rx_info.get("procedure") == FOLLOW_UP_PROCEDURE:
            return False
        if rx_info.get("symptom_code") == "SYM-FOLLOWUP":
            return False
        if rx_info.get("procedure") == PRIMARY_PROCEDURE and rx_info.get("priority", "").upper() in {
            "U",
            "B",
            "D",
        }:
            return True

    for row in chat:
        if row.sender == LEDGER:
            continue
        fields = msg_fields(row)
        if fields.get("follow_up", "").lower() in {"true", "yes", "1"}:
            return True
        if fields.get("needs_follow_up", "").lower() in {"true", "yes", "1"}:
            return True
        lowered = row.text.lower()
        if any(phrase in lowered for phrase in _FOLLOW_UP_PHRASES):
            return True
    return False


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
            "May I share your visit report (report_sharing)? If you prefer to keep it private, "
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
            "report on the health record (report_sharing)? If you prefer not to share it, "
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
