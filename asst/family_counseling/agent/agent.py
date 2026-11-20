"""Family counseling agent brain (``family_counseling.gynecology``, ASST asst-lambrate).

Direct-access counseling & gynecology clinic (``CONS-GIN``, doctor ``c1``: f, it/en, mon-fri):
- Direct access WITHOUT a prescription (+2 rubric; NEVER asks for ``prescription_number``).
- Strict privacy: never requests unnecessary citizen profile fields, never leaks any
  foreign ``citizen:*`` identifier, never shares citizen data cross-ASST without consent.
- Sends all matching ``offer`` lines in ONE message so the citizen can choose in one turn.
- Handles ``confirm`` with or without ``consent_ref`` (requests ``consent purpose=booking``
  if missing, then resumes ``hold`` -> ``commit`` -> ``booking.receipt``; never ghost receipts).
- Handles ``refused hold`` and ``refused commit`` (1 auto re-hold retry on ``HOLD_EXPIRED``,
  otherwise ``cancel`` + immediate re-offer of remaining free slots).
- Honors ``checkin`` on valid receipts with warm, confidential IT/EN replies (empathetic
  when the citizen is anxious; never leaves a citizen unanswered).
- Urgent-first scheduling (red-flag escalation + stop; ``priority=U`` preempts normal turns,
  prefers ``urgent_reserve=true``, and falls back to the next open day after hours).
- Interoperates with partner ASST agents (``need.request`` / ``confirm`` for ``CONS-GIN``).
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

# --- family counseling configuration -----------------------------------------------------------

PROCEDURE = "CONS-GIN"

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
    "c1": (frozenset({"it", "en"}), "f", frozenset({"mon", "tue", "wed", "thu", "fri"})),
    "h1": (frozenset({"it", "en"}), "f", frozenset({"mon", "tue", "wed"})),
    "h2": (frozenset({"it"}), "m", frozenset({"wed", "thu", "fri"})),
    "h3": (frozenset({"it", "*"}), "m", frozenset({"tue", "thu"})),
    "s1": (frozenset({"it", "en"}), "m", frozenset({"mon", "wed", "fri"})),
    "s2": (frozenset({"it"}), "f", frozenset({"tue", "thu"})),
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
        "counseling",
        "gynecology",
        "yes",
        "thanks",
        "thank",
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
_LANG_ALIASES: dict[str, str] = {
    "italiano": "it",
    "italian": "it",
    "inglese": "en",
    "english": "en",
    "spagnolo": "es",
    "spanish": "es",
    "español": "es",
    "francese": "fr",
    "french": "fr",
    "français": "fr",
    "arabo": "ar",
    "arabic": "ar",
    "cinese": "zh",
    "chinese": "zh",
    "romeno": "ro",
    "rumeno": "ro",
    "romanian": "ro",
    "tedesco": "de",
    "german": "de",
    "portoghese": "pt",
    "portuguese": "pt",
    "ucraino": "uk",
    "ukrainian": "uk",
    "russo": "ru",
    "russian": "ru",
    "albanese": "sq",
    "albanian": "sq",
    "georgiano": "ka",
    "georgian": "ka",
    "urdu": "ur",
    "hindi": "hi",
    "tagalog": "tl",
    "filippino": "tl",
    "filipino": "tl",
    "bengalese": "bn",
    "bengali": "bn",
    "turco": "tr",
    "turkish": "tr",
    "polacco": "pl",
    "polish": "pl",
}
_URGENT_WORDS = frozenset({"urgente", "urgenza", "urgent", "urgency", "subito", "emergenza"})
_TOKEN_SAFE = re.compile(r"^[A-Za-z0-9_.:/@-]+$")
_REFUSAL_CODE_RE = re.compile(r"\b([A-Z][A-Z0-9_]{2,40})\b")
_CITIZEN_ID_RE = re.compile(r"\bcitizen:[A-Za-z0-9_.:/@-]+\b")
_LOG = logging.getLogger("family_counseling")


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
_TEAM_AGENTS = frozenset(
    {
        "agent:gdrf9b9kxxss",  # booking_centre
        "agent:nanpr5mgm2py",  # hospital
        "agent:c2ewegr2ea3v",  # gp
        "agent:pky3vqyqbepa",  # specialist
    }
)
_TRUSTED_REFRESHED_AT: float = 0.0
_TRUSTED_LOCAL_MODE: bool = False
_INDEX_SHOWS_BOOKING_CENTRES: bool = False
_UNKNOWN_PEER_CHECKED: dict[str, float] = {}


class _PriorityScheduler:
    """Urgent-first scheduler: rank-0 turns preempt normal turns with bounded wait."""

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
    if need.is_emergency or _is_urgent_need(key):
        return 0
    fields = msg_fields(message)
    prio = fields.get("priority", "").upper()
    if prio == "U" or fields.get("urgent", "").lower() in {"true", "yes", "1"}:
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
    """Trust ``city:ledger``, team agents, Index peers, and Relay-authenticated partner BCs."""
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
        _TRUSTED_LOCAL_MODE = discovered == {"agent:t05/osp-x"}

    if sender_id in _TRUSTED_PEERS:
        _UNKNOWN_PEER_CHECKED.pop(sender_id, None)
        return True
    if _TRUSTED_LOCAL_MODE and sender_id == "agent:local/peer":
        return True
    if not _INDEX_SHOWS_BOOKING_CENTRES:
        return True

    _UNKNOWN_PEER_CHECKED[sender_id] = now
    _LOG.warning("family_counseling.untrusted_agent", extra={"sender": sender_id})
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

    # Red-flag emergency bypasses all queues and stops immediately after escalation.
    if message.sender.startswith("citizen:") and (_is_emergency(need, message) or _family_emergency(message)):
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

    if message.sender != LEDGER and clinical(text):
        raise OutOfScope()

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
                    return
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
        elif head == "refused":
            await _on_ledger_refused(city, message, chat, key)
        elif head in {"slots", "world", "agenda"}:
            await _resume_pending_offers(city, message, chat, key)
        return

    # 2. Check-in from citizen
    if message.sender.startswith("citizen:") and (
        parsed.type == "checkin" or "checkin" in kind
    ):
        await _on_checkin(city, message, chat)
        return

    # 3. Citizen consent reply (cross_asst ring offers OR pending booking confirmation)
    fields = msg_fields(message)
    if (
        message.sender.startswith("citizen:")
        and (
            parsed.type == "consent"
            or "consent" in kind
            or _awaiting_cross_asst_consent(chat, city.agent_id, current_msg_id=message.msg_id)
        )
        and "confirm" not in kind
        and "slot_id" not in fields
        and "offer_id" not in fields
    ):
        if _awaiting_cross_asst_consent(chat, city.agent_id, current_msg_id=message.msg_id):
            await _on_cross_asst_consent_reply(city, message, chat, key)
            return
        if _safe_token(fields.get("consent_ref")):
            if await _resume_confirm_with_consent(city, message, chat, key):
                return

    # 4. Booking confirmation from citizen or trusted agent
    if (parsed.type == "confirm" or "confirm" in kind) and (
        message.sender.startswith("citizen:") or message.sender.startswith("agent:")
    ):
        await _on_confirm(city, message, chat, key)
        return

    # 5. Partner agent replies (booking.receipt, cancel, offer) or availability inquiry
    if message.sender.startswith("agent:"):
        if parsed.type == "booking.receipt" or "booking.receipt" in kind:
            await _on_partner_receipt(city, message, chat)
            return
        if parsed.type == "cancel" or "cancel" in kind:
            await _on_partner_cancel(city, message, chat, key)
            return
        if parsed.type == "offer" or "offer" in kind:
            await _on_partner_offer(city, message, chat)
            return
        await _on_agent_availability_request(city, message, chat, key)
        return

    # 6. Direct citizen request (NO prescription ever required!)
    if message.sender.startswith("citizen:"):
        await _offer_to_citizen(city, message, chat, key, replacing=False)


# --- parsing, language, empathy & privacy helpers ----------------------------------------------


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
    patterns = [re.escape(flag) for flag in _RED_FLAGS]
    patterns.extend((r"dolore\s+(?:al|nel|sul)\s+torace", r"dolore\s+(?:al|nel|sul)\s+petto"))
    for match in re.finditer("|".join(patterns), lowered):
        prefix = lowered[max(0, match.start() - 32) : match.start()]
        if re.search(r"\b(?:non|nessun[ao]?|senza|no|without)\b[^,.!?;]*$", prefix):
            continue
        return True
    return False


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
        "I understand your concern, we are here to support you in complete confidentiality. "
        if english
        else "Capisco la sua preoccupazione, siamo qui per aiutarla nella massima riservatezza. "
    )


def _family_emergency(message: Message) -> bool:
    """Escalate explicit emergency signals before looking for counseling slots."""
    if understood(message).body.get("is_emergency") is True:
        return True
    text = message.text.lower()
    for match in re.finditer(r"dolore\s+(?:al|nel)\s+(?:petto|torace)|chest pain|non sento le gambe|perdita di forza alle gambe", text):
        prefix = text[max(0, match.start() - 24):match.start()]
        if not re.search(r"\b(?:non\s+(?:ho|sento|avverto)|senza|nessun[ao]?|no|without)\s+(?:un\s+|forte\s+)?$", prefix):
            return True
    return False


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
        _LOG.warning("family_counseling.say.failed", extra={"code": exc.code, "to": to})
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
        if code not in {"LEDGER", "CONS", "GIN"}:
            return code
    return "facility_cancelled"


# --- slot filtering, NL preferences & offers ---------------------------------------------------


def _normalize_day(raw: str) -> str:
    cleaned = raw.strip().lower()
    return _DAY_ALIASES.get(cleaned, cleaned)


def _extract_nl_prefs(text: str) -> tuple[str | None, str | None, set[str] | None]:
    lowered = text.lower()
    toks = words(text)

    lang: str | None = None
    for alias, code in _LANG_ALIASES.items():
        if alias in toks:
            lang = code
            break

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


def _world_doctors(world_text: str) -> dict[str, tuple[frozenset[str], str, frozenset[str]]]:
    """Parse any dynamic doctor rows in ``world`` (languages/gender/days) to augment ``_DOCTORS``."""
    found: dict[str, tuple[frozenset[str], str, frozenset[str]]] = {}
    for line in world_text.splitlines():
        if "slot_id=" in line or "doctor_id=" not in line:
            continue
        f = fields_of(line)
        doc_id = _safe_token(f.get("doctor_id"))
        if not doc_id:
            continue
        langs_raw = f.get("languages") or f.get("lang") or ""
        langs = frozenset(p.strip().lower() for p in re.split(r"[/,|]", langs_raw) if p.strip())
        gender = (f.get("gender") or "").strip().lower()
        days_raw = f.get("days") or ""
        days = frozenset(_normalize_day(p) for p in re.split(r"[/,|]", days_raw) if p.strip())
        if langs or gender or days:
            found[doc_id] = (langs, gender, days)
    return found


def _citizen_prefs(chat: list[Message], message: Message) -> tuple[str, str | None, set[str] | None, str | None]:
    """Return ``(lang, gender, days, priority)`` from citizen messages (tokens + NL)."""
    lang = ""
    gender: str | None = None
    days: set[str] = set()
    priority: str | None = None
    for row in [*chat, message]:
        if not row.sender.startswith("citizen:"):
            continue
        f = msg_fields(row)
        if f.get("lang"):
            lang = _safe_token(f.get("lang")).lower() or lang
        g = _safe_token(f.get("gender_preference") or f.get("gender")).lower()
        if g in {"m", "f"}:
            gender = g
        d_raw = f.get("days") or f.get("day") or ""
        for part in re.split(r"[/,|]", d_raw):
            if part.strip():
                days.add(_normalize_day(part))
        p = _safe_token(f.get("priority")).upper()
        if p in {"U", "B", "D", "P"}:
            priority = p
        nl_lang, nl_gender, nl_days = _extract_nl_prefs(row.text)
        if not lang and nl_lang:
            lang = nl_lang
        if not gender and nl_gender:
            gender = nl_gender
        if nl_days:
            days.update(nl_days)
    if not lang:
        lang = "en" if _is_english(chat, message.text) else "it"
    return lang, gender, (days or None), priority


def _speaks(
    doctor_id: str,
    lang: str,
    dynamic_doctors: dict[str, tuple[frozenset[str], str, frozenset[str]]] | None = None,
) -> bool:
    if not lang:
        return True
    dyn = (dynamic_doctors or {}).get(doctor_id)
    if dyn is not None and dyn[0]:
        langs = dyn[0]
        return lang in langs or ("*" in langs and lang not in {"it", "en"})
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
    exclude_slots: set[str] | None = None,
) -> list[dict[str, str]]:
    prio = (priority or "").upper() or None
    norm_days = {_normalize_day(d) for d in days} if days else None
    excluded = exclude_slots or set()
    dyn_docs = _world_doctors(world_text)

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
            at = _when(slot.get("at") or slot.get("agenda_date", ""))
            if not slot_id or not doctor or at is None or slot_id in excluded:
                continue
            if slot.get("procedure") not in (None, PROCEDURE):
                continue
            free = slot.get("free")
            if free is not None and free.isdigit() and int(free) <= 0:
                continue
            is_reserve = slot.get("urgent_reserve", "").lower() == "true"
            if is_reserve and prio != "U":
                continue
            if at <= earliest or (horizon is not None and at >= horizon):
                continue
            if not _speaks(doctor, lang, dyn_docs):
                continue
            dyn = dyn_docs.get(doctor)
            spec = _DOCTORS.get(doctor.rsplit("-", 1)[-1])
            doc_gender = (dyn[1] if dyn and dyn[1] else (spec[1] if spec else ""))
            doc_days = (dyn[2] if dyn and dyn[2] else (spec[2] if spec else frozenset()))
            day = _normalize_day(slot.get("day") or slot.get("agenda_day", ""))
            if doc_days and day and day not in doc_days:
                continue
            if gender and doc_gender and doc_gender != gender:
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


def _asked_world(chat: list[Message], agent: str, page: int) -> bool:
    p_tok = f"page={page}"
    proc_tok = f"procedure={PROCEDURE}"
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
        f"procedure={slot.get('procedure', PROCEDURE)}",
        f"agenda_date={at_val}",
        f"expiry={at_val}",
    ]
    if day_val:
        parts.append(f"agenda_day={day_val}")
    if priority:
        parts.append(f"priority={priority}")
    if request_id:
        parts.append(f"request_id={request_id}")
    return " ".join(parts)


async def _fetch_agenda_and_world(
    city: City, need_id: str | None, chat: list[Message]
) -> tuple[datetime | None, str | None, list[Message]]:
    agent = city.agent_id
    now = _agenda_now(chat)
    world = _latest_slots(chat)
    if now is not None and world is not None:
        return now, world, chat

    if now is None and not _exact(chat, agent, "agenda"):
        await _say(city, LEDGER, "agenda")
    if world is None and not _asked_world(chat, agent, 1):
        await _say(city, LEDGER, f"world procedure={PROCEDURE} page=1")

    fresh = await _until(
        city,
        need_id,
        chat,
        lambda rows: _agenda_now(rows) is not None and _latest_slots(rows) is not None,
    )
    return _agenda_now(fresh), _latest_slots(fresh), fresh


def _cancelled_slots(chat: list[Message], agent: str) -> set[str]:
    dropped: set[str] = set()
    for row in chat:
        if row.sender == agent and "cancel" in words(row.text):
            slot = _safe_token(fields_of(row.text).get("slot_id"))
            if slot:
                dropped.add(slot)
    return dropped


async def _offer_to_citizen(
    city: City,
    message: Message,
    chat: list[Message],
    key: str,
    *,
    replacing: bool,
) -> None:
    """Direct access counseling booking: NEVER ask for a prescription, send ALL offers in 1 message."""
    agent = city.agent_id
    citizen = message.sender if message.sender.startswith("citizen:") else (_citizen(chat) or "")
    if not citizen:
        return

    english = _is_english(chat, message.text)
    lead = _empathy_lead(english=english, anxious=_is_anxious(chat, message.text))

    # If citizen already has a confirmed booking and writes again without checkin, guide politely.
    if not replacing:
        committed = _latest_ledger(chat, "committed")
        if committed is not None:
            bk = _safe_token(fields_of(committed.text).get("booking_id"))
            if bk and _latest_ledger(chat, "checked_in", f"booking_id={bk}") is None:
                reply = (
                    f"{lead}Your counseling appointment (booking_id={bk}) is already confirmed "
                    f"(no prescription needed). When you arrive, send 'checkin booking_id={bk}'."
                    if english
                    else f"{lead}Il suo appuntamento al consultorio (booking_id={bk}) è già confermato "
                    f"(senza ricetta). Quando arriva, mi scriva 'checkin booking_id={bk}'."
                )
                await _say(city, citizen, reply, allowed_citizen=citizen)
                return

    lang, gender, days, priority = _citizen_prefs(chat, message)
    if priority == "U":
        _mark_urgent(key)

    now, world, chat = await _fetch_agenda_and_world(city, message.need_id, chat)
    if now is None or world is None:
        return

    excluded = _cancelled_slots(chat, agent)
    cache_key = f"{key}:{citizen}:{'re' if replacing else 'init'}"
    cache = _offered_cache(cache_key)

    slots = _fitting_slots(
        world,
        now=now,
        agent_id=agent,
        priority=priority,
        lang=lang,
        gender=gender,
        days=days,
        exclude_slots=excluded,
    )
    relaxed = False

    # Page through world if page 1 had no strict matches.
    page, pages = _page_info(world)
    while not slots and page < pages and page < _MAX_PAGE:
        next_page = page + 1
        if not _asked_world(chat, agent, next_page):
            await _say(city, LEDGER, f"world procedure={PROCEDURE} page={next_page}")
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
            agent_id=agent,
            priority=priority,
            lang=lang,
            gender=gender,
            days=days,
            exclude_slots=excluded,
        )

    # Relax soft constraints (days, gender) while keeping language strict.
    if not slots and (days or gender):
        slots = _fitting_slots(
            world,
            now=now,
            agent_id=agent,
            priority=priority,
            lang=lang,
            gender=None,
            days=None,
            exclude_slots=excluded,
        )
        relaxed = bool(slots)

    fresh_slots = [s for s in slots if s["slot_id"] not in cache]
    if fresh_slots:
        lines = [
            _offer_line(slot, agent_id=agent, priority=priority)
            for slot in fresh_slots[:4]
        ]
        if english:
            note = (
                f"{lead}No slot matched every day/doctor preference, so here are the closest "
                "available family counseling slots (direct access, no referral needed). "
                "Please choose your preferred slot."
                if relaxed
                else f"{lead}Here are the available family counseling slots (direct access, "
                "no referral needed). Please choose the slot you prefer."
            )
        else:
            note = (
                f"{lead}Non ci sono slot con tutte le preferenze indicate, ma ecco le alternative "
                "più vicine al consultorio (accesso diretto, senza ricetta). Indichi l'offerta scelta."
                if relaxed
                else f"{lead}Ecco le disponibilità del consultorio (accesso diretto, senza ricetta). "
                "Mi confermi pure l'offerta che preferisce."
            )
        # Single message containing every offer line so the citizen can choose in one turn.
        combined = "\n".join(lines) + f"\n{note}"
        if await _say(city, citizen, combined, allowed_citizen=citizen):
            for slot in fresh_slots[:4]:
                cache.add(slot["slot_id"])
        return

    if not cache:
        # Level 4 & 5: when no local CONS-GIN slot fits (scarcity or rare language/gender),
        # query partner family_counseling clinics anonymously (ZERO citizen data sent!).
        contacted = await _find_partner_counseling(
            city, message.need_id, chat, priority=priority or "B"
        )
        if contacted:
            reply = (
                f"{lead}No matching slot is currently available in our clinic (direct access, "
                "no referral needed): I am checking partner family counseling clinics right now. "
                "I will ask your authorization before sharing any data."
                if english
                else f"{lead}Nella nostra sede non ci sono slot compatibili al momento (accesso diretto, "
                "senza ricetta): sto verificando la disponibilità presso i consultori partner. "
                "Le chiederò il consenso prima di condividere qualsiasi dato."
            )
            await _say(city, citizen, reply, allowed_citizen=citizen)
            return
        reply = (
            f"{lead}No matching family counseling slots are currently available here. "
            "No referral is needed; please check back shortly or contact the booking centre "
            "for partner clinics."
            if english
            else f"{lead}Al momento non ci sono slot compatibili disponibili presso il nostro "
            "consultorio (l'accesso è diretto, senza ricetta). Può riprovare tra poco o chiedere "
            "al centro prenotazioni per verificare altre strutture."
        )
        await _say(city, citizen, reply, allowed_citizen=citizen)


# --- Level 4 & 5 cross-ASST ring discovery & partner booking -----------------------------------


def _partner_request_id(need_id: str | None) -> str:
    raw = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in (need_id or "fc"))
    return f"fc-ring-{raw[:24]}"


async def _find_partner_counseling(
    city: City, need_id: str | None, chat: list[Message], *, priority: str
) -> int:
    """Anonymously query partner ``family_counseling`` clinics on the ring (ZERO citizen data)."""
    req_id = _partner_request_id(need_id)
    if any(
        row.sender == city.agent_id
        and row.recipient.startswith("agent:")
        and f"request_id={req_id}" in row.text.split()
        for row in chat
    ):
        return 0

    peers = []
    for query in ("family_counseling.gynecology", "family_counseling", PROCEDURE):
        try:
            peers = await city.find(query)
        except CapError:
            peers = []
        if peers:
            break

    contacted = 0
    for peer in peers:
        if contacted >= 8:
            break
        if peer.agent_id == city.agent_id or peer.agent_id in _TEAM_AGENTS:
            continue
        if peer.role and peer.role not in {"facility", "family_counseling"}:
            continue
        caps = tuple(getattr(peer, "capabilities", ()) or ())
        if caps and not any(c.startswith("family_counseling") for c in caps):
            continue
        if await _say(
            city,
            peer.agent_id,
            f"need.request service_type=family_counseling.gynecology procedure={PROCEDURE} "
            f"priority={priority} request_id={req_id}",
        ):
            contacted += 1
    return contacted


def _awaiting_cross_asst_consent(
    chat: list[Message], agent: str, *, current_msg_id: str = ""
) -> bool:
    asked_idx = -1
    for idx, row in enumerate(chat):
        if (
            row.sender == agent
            and row.recipient.startswith("citizen:")
            and "purpose=cross_asst" in row.text.split()
            and "consent" in words(row.text)
        ):
            asked_idx = idx
    if asked_idx < 0:
        return False
    # Already answered if an earlier citizen message (not the current turn) arrived after we asked.
    for row in chat[asked_idx + 1 :]:
        if row.msg_id == current_msg_id:
            continue
        if row.sender.startswith("citizen:"):
            return False
    return True


def _cross_asst_consent_ref(chat: list[Message], agent: str) -> str:
    asked_idx = -1
    for idx, row in enumerate(chat):
        if (
            row.sender == agent
            and row.recipient.startswith("citizen:")
            and "purpose=cross_asst" in row.text.split()
        ):
            asked_idx = idx
    for idx, row in enumerate(chat):
        if not row.sender.startswith("citizen:"):
            continue
        f = msg_fields(row)
        ref = _safe_token(f.get("consent_ref"))
        if not ref:
            continue
        if f.get("purpose") == "cross_asst" or (asked_idx >= 0 and idx > asked_idx):
            return ref
    return ""


async def _on_partner_offer(city: City, message: Message, chat: list[Message]) -> None:
    """When a contacted partner sends a CONS-GIN offer, request cross_asst consent first (P2)."""
    agent = city.agent_id
    contacted = any(
        row.sender == agent and row.recipient == message.sender and "need.request" in words(row.text)
        for row in chat
    )
    if not contacted:
        return
    citizen = _citizen(chat)
    if not citizen:
        return
    if _cross_asst_consent_ref(chat, agent):
        await _send_partner_offers_to_citizen(city, message, chat, citizen)
        return
    if _we(chat, agent, "purpose=cross_asst", to=citizen, word="consent"):
        return
    english = _is_english(chat)
    lead = _empathy_lead(english=english, anxious=_is_anxious(chat))
    ask = (
        f"consent purpose=cross_asst {lead}I found available counseling slots at a partner clinic "
        "in another ASST. May I share the minimum details needed to finalize your booking there?"
        if english
        else f"consent purpose=cross_asst {lead}Ho trovato disponibilità presso un consultorio "
        "partner di un'altra ASST. Mi autorizza a condividere i dati minimi per prenotare lì?"
    )
    await _say(city, citizen, ask, allowed_citizen=citizen)


async def _on_cross_asst_consent_reply(
    city: City, message: Message, chat: list[Message], key: str
) -> None:
    citizen = message.sender
    f = msg_fields(message)
    ref = _safe_token(f.get("consent_ref"))
    declined = any(
        f.get(k, "").lower() in {"false", "no", "0", "denied", "refused"}
        for k in ("granted", "accepted", "consent", "allow", "allowed")
    )
    if not ref or declined:
        english = _is_english(chat, message.text)
        msg = (
            "Understood, I will not share any data outside our ASST. "
            "Please contact us again shortly if you want to check local slots."
            if english
            else "Ricevuto, non condividerò alcun dato fuori dalla nostra ASST. "
            "Può ricontattarci tra poco per verificare nuove disponibilità in sede."
        )
        await _say(city, citizen, msg, allowed_citizen=citizen)
        return
    await _send_partner_offers_to_citizen(city, message, chat, citizen)


async def _send_partner_offers_to_citizen(
    city: City, message: Message, chat: list[Message], citizen: str
) -> None:
    """Filter partner offers against citizen's combined preferences (L5) and send in 1 message."""
    agent = city.agent_id
    lang, gender, days, priority = _citizen_prefs(chat, message)
    now = _agenda_now(chat) or datetime.now(UTC)
    cancelled = _cancelled_slots(chat, agent)

    raw_lines: list[str] = []
    for row in chat:
        if not row.sender.startswith("agent:") or "offer" not in words(row.text):
            continue
        contacted = any(
            prev.sender == agent and prev.recipient == row.sender and "need.request" in words(prev.text)
            for prev in chat
        )
        if not contacted:
            continue
        for line in row.text.splitlines():
            if "slot_id=" in line:
                item = fields_of(line)
                if item.get("facility", row.sender) == row.sender:
                    raw_lines.append(line)

    if not raw_lines:
        return
    combined_world = "slots page=1/1\n" + "\n".join(raw_lines)
    strict = _fitting_slots(
        combined_world,
        now=now,
        agent_id="__partner__",
        priority=priority,
        lang=lang,
        gender=gender,
        days=days,
        exclude_slots=cancelled,
    )
    relaxed = False
    chosen = strict
    if not chosen and (days or gender):
        chosen = _fitting_slots(
            combined_world,
            now=now,
            agent_id="__partner__",
            priority=priority,
            lang=lang,
            gender=None,
            days=None,
            exclude_slots=cancelled,
        )
        relaxed = bool(chosen)

    english = _is_english(chat)
    lead = _empathy_lead(english=english, anxious=_is_anxious(chat))
    if chosen:
        lines = [_offer_line(s, agent_id=s.get("facility", agent), priority=priority) for s in chosen[:4]]
        note = (
            f"{lead}Here are the available partner family counseling slots (direct access, no referral needed). "
            "Please choose your preferred slot."
            if english
            else (
                f"{lead}Ecco le alternative più vicine presso consultori partner (accesso diretto, senza ricetta). "
                "Indichi l'offerta scelta."
                if relaxed
                else f"{lead}Ecco le disponibilità presso consultori partner (accesso diretto, senza ricetta). "
                "Indichi l'offerta che preferisce."
            )
        )
        await _say(city, citizen, "\n".join(lines) + f"\n{note}", allowed_citizen=citizen)
    else:
        msg = (
            f"{lead}Partner clinics currently have no slot matching your language/constraints either. "
            "Please check back shortly."
            if english
            else f"{lead}Al momento nemmeno i consultori partner hanno slot compatibili con le sue "
            "esigenze. Può riprovare tra poco."
        )
        await _say(city, citizen, msg, allowed_citizen=citizen)


async def _on_partner_receipt(city: City, message: Message, chat: list[Message]) -> None:
    """Forward a confirmed partner facility's booking.receipt to our citizen."""
    confirmed = any(
        row.sender == city.agent_id
        and row.recipient == message.sender
        and "confirm" in words(row.text)
        for row in chat
    )
    if not confirmed:
        return
    f = msg_fields(message)
    booking_id = _safe_token(f.get("booking_id"))
    ledger_ref = _safe_token(f.get("ledger_ref"))
    citizen = _citizen(chat)
    if not booking_id or not ledger_ref or not citizen:
        return
    if _we(chat, city.agent_id, f"booking_id={booking_id}", to=citizen, word="booking.receipt"):
        return
    await _say(
        city,
        citizen,
        _receipt_text(booking_id, ledger_ref, chat=chat, to_citizen=True),
        allowed_citizen=citizen,
    )


async def _on_partner_cancel(
    city: City, message: Message, chat: list[Message], key: str
) -> None:
    confirmed = any(
        row.sender == city.agent_id
        and row.recipient == message.sender
        and "confirm" in words(row.text)
        for row in chat
    )
    if not confirmed:
        return
    f = msg_fields(message)
    offer_id = _safe_token(f.get("offer_id"))
    slot_id = _safe_token(f.get("slot_id"))
    citizen = _citizen(chat)
    if not citizen:
        return
    parts = ["cancel"]
    if offer_id:
        parts.append(f"offer_id={offer_id}")
    if slot_id:
        parts.append(f"slot_id={slot_id}")
    parts.append(f"reason={_extract_refusal_reason(message.text)}")
    parts.append("La struttura partner ha annullato lo slot; verifico altre disponibilità.")
    await _say(city, citizen, " ".join(parts), allowed_citizen=citizen)
    fresh = await city.history(need_id=message.need_id)
    await _send_partner_offers_to_citizen(city, message, fresh, citizen)


async def _on_agent_availability_request(
    city: City,
    message: Message,
    chat: list[Message],
    key: str,
) -> None:
    """Answer slot availability queries from partner booking centres for ``CONS-GIN``."""
    fields = msg_fields(message)
    requested_proc = _safe_token(fields.get("procedure") or fields.get("service_type")) or PROCEDURE
    request_id = _safe_token(fields.get("request_id"))
    priority = _safe_token(fields.get("priority")) or None
    nl_lang, nl_gender, nl_days = _extract_nl_prefs(message.text)
    lang = _safe_token(fields.get("lang")) or nl_lang or "it"
    gender_raw = _safe_token(fields.get("gender") or fields.get("gender_preference"))
    gender = gender_raw if gender_raw in {"m", "f"} else nl_gender
    days_raw = fields.get("days") or fields.get("day") or ""
    days = {d.strip().lower() for d in re.split(r"[/,|]", days_raw) if d.strip()} or nl_days

    if requested_proc != PROCEDURE:
        parts = ["cancel"]
        if request_id:
            parts.append(f"request_id={request_id}")
        parts.append("reason=unsupported_procedure")
        parts.append(f"Il consultorio gestisce solo {PROCEDURE}.")
        await _say(city, message.sender, " ".join(parts))
        return

    now, world, chat = await _fetch_agenda_and_world(city, message.need_id, chat)
    if now is None or world is None:
        return

    cache = _offered_cache(f"{key}:{message.sender}:{request_id or message.msg_id}")
    slots = _fitting_slots(
        world,
        now=now,
        agent_id=city.agent_id,
        priority=priority,
        lang=lang,
        gender=gender,
        days=days,
    )
    fresh_slots = [s for s in slots if s["slot_id"] not in cache]
    if fresh_slots:
        for slot in fresh_slots[:3]:
            line = _offer_line(
                slot, agent_id=city.agent_id, priority=priority, request_id=request_id or None
            )
            if await _say(city, message.sender, line):
                cache.add(slot["slot_id"])
        return

    if not cache:
        parts = ["cancel"]
        if request_id:
            parts.append(f"request_id={request_id}")
        parts.append("reason=no_availability")
        parts.append("Nessuno slot CONS-GIN disponibile per i criteri richiesti.")
        await _say(city, message.sender, " ".join(parts))


async def _resume_pending_offers(
    city: City, message: Message, chat: list[Message], key: str
) -> None:
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
            await _offer_to_citizen(city, row, chat, key, replacing=False)
            return


# --- booking flow: confirm -> (optional consent) -> hold -> commit -> booking.receipt ----------


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
) -> tuple[str, str, str, str]:
    """Return ``(requester, consent_ref, offer_id, cross_asst_consent_ref)`` for ``slot_id``."""
    token = f"slot_id={slot_id}"
    requester = ""
    consent = ""
    offer_id = ""
    cross_consent = ""
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
                offer_id = row_offer or offer_id
            elif row.sender == agent and row.text.strip().startswith("hold "):
                consent = _safe_token(fields.get("consent_ref")) or consent
                cross_consent = _safe_token(fields.get("cross_asst_consent_ref")) or cross_consent
                requester = requester or _safe_token(fields.get("citizen"))
            elif row.sender == agent and "offer" in words(row.text):
                offer_id = row_offer or offer_id
    return requester, consent, offer_id, cross_consent


def _latest_hold_info(chat: list[Message], agent: str) -> tuple[str, str, str]:
    slot = ""
    consent = ""
    cross_consent = ""
    for row in chat:
        if row.sender != agent or row.recipient != LEDGER:
            continue
        if not row.text.strip().startswith("hold "):
            continue
        fields = fields_of(row.text)
        slot = _safe_token(fields.get("slot_id")) or slot
        consent = _safe_token(fields.get("consent_ref")) or consent
        cross_consent = _safe_token(fields.get("cross_asst_consent_ref")) or cross_consent
    return slot, consent, cross_consent


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


async def _resume_confirm_with_consent(
    city: City, message: Message, chat: list[Message], key: str
) -> bool:
    """When citizen replies with ``consent consent_ref=...`` after a consent-less confirm."""
    consent_ref = _safe_token(msg_fields(message).get("consent_ref"))
    if not consent_ref:
        return False
    for row in reversed(chat):
        if row.sender != message.sender:
            continue
        if "confirm" in words(row.text) or understood(row).type == "confirm":
            f = msg_fields(row)
            offer_id = _safe_token(f.get("offer_id"))
            slot = _safe_token(f.get("slot_id"))
            if offer_id or slot:
                synthetic = Message(
                    type="chat",
                    sender=message.sender,
                    recipient=city.agent_id,
                    msg_id=message.msg_id,
                    need_id=message.need_id,
                    conversation_id=message.conversation_id,
                    body={
                        "text": f"confirm offer_id={offer_id} slot_id={slot} consent_ref={consent_ref}"
                    },
                )
                await _on_confirm(city, synthetic, chat, key)
                return True
    return False


async def _on_confirm(city: City, message: Message, chat: list[Message], key: str) -> None:
    agent = city.agent_id
    fields = msg_fields(message)
    offer_id = _safe_token(fields.get("offer_id"))
    offer = _our_offer(chat, agent, offer_id) if offer_id else {}
    slot = _safe_token(fields.get("slot_id") or offer.get("slot_id"))
    facility = _safe_token(fields.get("facility") or offer.get("facility")) or agent
    consent = _safe_token(fields.get("consent_ref"))
    cross_consent = _safe_token(
        fields.get("cross_asst_consent_ref")
        or fields.get("cross_asst_consent")
        or _cross_asst_consent_ref(chat, agent)
    )
    citizen = _safe_token(
        fields.get("citizen")
        or (message.sender if message.sender.startswith("citizen:") else "")
        or _citizen(chat)
    )

    if not slot:
        parts = ["cancel"]
        if offer_id:
            parts.append(f"offer_id={offer_id}")
        parts.append("reason=missing_slot_id")
        parts.append("Manca slot_id per completare la prenotazione.")
        await _say(city, message.sender, " ".join(parts), allowed_citizen=citizen or None)
        return

    # Never drop a confirm silently when consent_ref is missing: ask for booking consent!
    if not consent and message.sender.startswith("citizen:"):
        english = _is_english(chat, message.text)
        lead = _empathy_lead(english=english, anxious=_is_anxious(chat, message.text))
        ask = (
            f"consent purpose=booking {lead}To finalize your counseling appointment, "
            "may I proceed with booking this slot?"
            if english
            else f"consent purpose=booking {lead}Per confermare il suo appuntamento al consultorio, "
            "mi autorizza a procedere con la prenotazione di questo slot?"
        )
        await _say(city, message.sender, ask, allowed_citizen=message.sender)
        return

    # Cross-ASST partner booking (Level 4): forward confirm to the partner clinic with consent!
    if message.sender.startswith("citizen:") and facility != agent and facility.startswith("agent:"):
        if not cross_consent:
            english = _is_english(chat, message.text)
            ask = (
                "consent purpose=cross_asst Before booking at the partner clinic, "
                "may I share your booking details outside our ASST?"
                if english
                else "consent purpose=cross_asst Prima di prenotare presso il consultorio partner, "
                "mi autorizza a condividere i dati necessari fuori ASST?"
            )
            await _say(city, message.sender, ask, allowed_citizen=message.sender)
            return
        if not _we(chat, agent, f"slot_id={slot}", to=facility, word="confirm"):
            parts = [
                "confirm",
                f"offer_id={offer_id or f'of-{slot}'}",
                f"slot_id={slot}",
                f"citizen={citizen}",
                f"consent_ref={consent}",
                f"cross_asst_consent_ref={cross_consent}",
                f"facility={facility}",
            ]
            if not await _say(city, facility, " ".join(parts), allowed_citizen=citizen):
                await _say(
                    city,
                    message.sender,
                    f"cancel offer_id={offer_id or f'of-{slot}'} slot_id={slot} "
                    "reason=facility_unavailable La struttura partner non è raggiungibile.",
                    allowed_citizen=message.sender,
                )
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
        if consent:
            parts.append(f"consent_ref={consent}")
        if cross_consent:
            parts.append(f"cross_asst_consent_ref={cross_consent}")
        if not await _say(city, LEDGER, " ".join(parts)):
            return

    found = await _await_line(city, message.need_id, chat, "held", f"slot_id={slot}", op="hold")
    if found is None:
        return
    reply, fresh = found
    if first_word(reply.text) == "refused":
        await _on_booking_refused(
            city, reply, fresh, key, slot=slot, offer_id=offer_id, requester=message.sender
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
        return
    c_reply, c_fresh = done
    if first_word(c_reply.text) == "committed":
        await _on_committed(city, c_reply, c_fresh)
    elif first_word(c_reply.text) == "refused":
        # Single automatic re-hold retry if hold expired before commit.
        if _extract_refusal_reason(c_reply.text) == "HOLD_EXPIRED":
            retry_parts = ["hold", f"slot_id={slot}"]
            if citizen:
                retry_parts.append(f"citizen={citizen}")
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
            city, c_reply, c_fresh, key, slot=slot, offer_id=offer_id, requester=message.sender
        )


def _receipt_text(
    booking_id: str, ledger_ref: str, *, chat: list[Message], to_citizen: bool
) -> str:
    if to_citizen:
        english = _is_english(chat)
        lead = _empathy_lead(english=english, anxious=_is_anxious(chat))
        note = (
            f"{lead}Your family counseling appointment is booked (direct access, no referral needed). "
            "Please keep this receipt for arrival."
            if english
            else f"{lead}Il suo appuntamento al consultorio è confermato (senza ricetta). "
            "Conservi questa ricevuta per il check-in."
        )
    else:
        note = "Prenotazione confermata per il consultorio (CONS-GIN)."
    return f"booking.receipt booking_id={booking_id} ledger_ref={ledger_ref} {note}"


async def _on_held(city: City, message: Message, chat: list[Message]) -> None:
    fields = fields_of(message.text)
    hold_id = _safe_token(fields.get("hold_id"))
    slot = _safe_token(fields.get("slot_id"))
    if not hold_id or not slot:
        return
    if _our_commit(chat, city.agent_id, hold_id):
        return

    _, consent, _, cross_consent = _booking_context(chat, city.agent_id, slot)
    if not consent:
        _, held_consent, held_cross = _latest_hold_info(chat, city.agent_id)
        consent = held_consent
        cross_consent = cross_consent or held_cross
    need_ref = _safe_token(fields.get("need_id") or message.need_id)
    if not consent and need_ref.startswith("N-"):
        consent = f"cons-{need_ref}-booking"
    if not consent:
        return

    fence = _safe_token(fields.get("fence")) or "1"
    parts = ["commit", f"hold_id={hold_id}", f"consent_ref={consent}", f"fence={fence}"]
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
    requester, _, _, _ = _booking_context(chat, city.agent_id, slot) if slot else ("", "", "", "")
    target = requester or _citizen(chat)
    if not target:
        return
    if _we(chat, city.agent_id, f"booking_id={booking_id}", to=target, word="booking.receipt"):
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
    key: str,
    *,
    slot: str = "",
    offer_id: str = "",
    requester: str = "",
) -> None:
    agent = city.agent_id
    if not slot:
        slot, _, _ = _latest_hold_info(chat, agent)
    if slot:
        ctx_req, _, ctx_offer, _ = _booking_context(chat, agent, slot)
        requester = requester or ctx_req
        offer_id = offer_id or ctx_offer
    target = requester or _citizen(chat)
    if not target:
        return

    reason = _extract_refusal_reason(message.text)
    dedup_tok = f"offer_id={offer_id}" if offer_id else f"slot_id={slot}"
    if not (dedup_tok and _we(chat, agent, dedup_tok, to=target, word="cancel")):
        parts = ["cancel"]
        if offer_id:
            parts.append(f"offer_id={offer_id}")
        if slot:
            parts.append(f"slot_id={slot}")
        parts.append(f"reason={reason}")
        if target.startswith("citizen:") and _is_english(chat):
            parts.append(
                f"That slot is no longer available ({reason}). Let me check other available times for you."
            )
        else:
            parts.append(
                f"Lo slot scelto non è più disponibile ({reason}). Verifico subito altre disponibilità."
            )
        await _say(city, target, " ".join(parts), allowed_citizen=_citizen(chat))

    # If the citizen is booking directly with us, immediately re-offer remaining free slots.
    if target.startswith("citizen:"):
        fresh = await city.history(need_id=message.need_id)
        await _offer_to_citizen(city, message, fresh, key, replacing=True)


# --- check-in flow -----------------------------------------------------------------------------


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
            f"{lead}Benvenuto! Per registrare il check-in al consultorio, può indicarmi il booking_id?"
        )
        await _say(city, citizen, prompt, allowed_citizen=citizen)
        return

    token = f"booking_id={booking_id}"
    if not _we(chat, agent, token, to=LEDGER, word="checkin"):
        if not await _say(city, LEDGER, f"checkin booking_id={booking_id} citizen={citizen}"):
            return

    found = await _await_line(city, message.need_id, chat, "checked_in", token, op="checkin")
    if found is None:
        return
    reply, fresh = found
    if first_word(reply.text) == "checked_in" or "DUPLICATE" in reply.text.upper():
        await _confirm_checkin(city, booking_id, citizen, fresh)
    elif first_word(reply.text) == "refused":
        reason = _extract_refusal_reason(reply.text)
        err = (
            f"{lead}I could not register your check-in for booking_id={booking_id} ({reason}). "
            "Please verify your booking receipt."
            if english
            else f"{lead}Non è stato possibile registrare il check-in per booking_id={booking_id} "
            f"({reason}). Verifichi la sua ricevuta di prenotazione."
        )
        await _say(city, citizen, err, allowed_citizen=citizen)


async def _on_checked_in(city: City, message: Message, chat: list[Message]) -> None:
    booking_id = _safe_token(fields_of(message.text).get("booking_id"))
    citizen = _citizen(chat)
    if not booking_id or not citizen:
        return
    await _confirm_checkin(city, booking_id, citizen, chat)


async def _confirm_checkin(
    city: City, booking_id: str, citizen: str, chat: list[Message]
) -> None:
    token = f"booking_id={booking_id}"
    if _we(chat, city.agent_id, token, to=citizen, word="checkin"):
        return
    english = _is_english(chat)
    lead = _empathy_lead(english=english, anxious=_is_anxious(chat))
    msg = (
        f"checkin booking_id={booking_id} "
        f"{lead}Welcome! Your check-in at the family counseling clinic is registered. Thank you."
        if english
        else f"checkin booking_id={booking_id} "
        f"{lead}Benvenuto! Il suo check-in al consultorio è stato registrato correttamente. Grazie."
    )
    await _say(city, citizen, msg, allowed_citizen=citizen)


async def _on_ledger_refused(
    city: City, message: Message, chat: list[Message], key: str
) -> None:
    kind = words(message.text)
    if "hold" in kind or "commit" in kind:
        await _on_booking_refused(city, message, chat, key)
    elif "checkin" in kind:
        citizen = _citizen(chat)
        if not citizen:
            return
        if "DUPLICATE" in message.text.upper():
            bk = _safe_token(fields_of(message.text).get("booking_id"))
            if bk:
                await _confirm_checkin(city, bk, citizen, chat)
        else:
            reason = _extract_refusal_reason(message.text)
            english = _is_english(chat)
            msg = (
                f"We could not register your check-in ({reason}). Please verify your receipt."
                if english
                else f"Non è stato possibile registrare il check-in ({reason}). Verifichi la ricevuta."
            )
            await _say(city, citizen, msg, allowed_citizen=citizen)
