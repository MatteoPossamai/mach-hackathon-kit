"""Shared constants, parsing, prose hygiene, dedup, and Ledger helpers for family_counseling."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from city_sdk.city import LEDGER, CapError, City, Message, Need, understood

PROCEDURE = "CONS-GIN"

MAX_PAGE = 8
PULL_TRIES = 16
PULL_PAUSE = 0.2
URGENT_PULL_PAUSE = 0.08
DEDUP_SECONDS = 120.0

DOCTORS: dict[str, tuple[frozenset[str], str, frozenset[str]]] = {
    "c1": (frozenset({"it", "en"}), "f", frozenset({"mon", "tue", "wed", "thu", "fri"})),
    "h1": (frozenset({"it", "en"}), "f", frozenset({"mon", "tue", "wed"})),
    "h2": (frozenset({"it"}), "m", frozenset({"wed", "thu", "fri"})),
    "h3": (frozenset({"it", "*"}), "m", frozenset({"tue", "thu"})),
    "s1": (frozenset({"it", "en"}), "m", frozenset({"mon", "wed", "fri"})),
    "s2": (frozenset({"it"}), "f", frozenset({"tue", "thu"})),
}

TEAM_AGENTS = frozenset({
    "agent:gdrf9b9kxxss",  # booking_centre
    "agent:nanpr5mgm2py",  # hospital
    "agent:c2ewegr2ea3v",  # gp
    "agent:pky3vqyqbepa",  # specialist
})

# MACH-1.2 declared partner agents (TRUST RING allowlist for Level 8 impostor defense).
TRUSTED_RING_BOOKING_CENTRES = frozenset({
    "agent:ser7es7thgwf",  # Precotto · Toros
    "agent:qtjne8symjas",  # Lorenteggio · RASPBERRY
    "agent:yzcs4p44ep39",  # Corvetto · scuola-di-atene
    "agent:cdc7x6m21nj6",  # Giambellino · RED-AGENT
    "agent:psxp8bh1feaa",  # Nord · agentic-paguri
    "agent:h3n6kfd18r3g",  # Greco · tartufi-pazzi
    "agent:ez8cb7j167p2",  # Bicocca · p4radigm
    "agent:vgtmj5nqfkxj",  # Bovisa · prompt-soccorso
})
TRUSTED_RING_COUNSELING = frozenset({
    "agent:nf9pqsgzrwm0",  # Precotto · Toros
    "agent:mcppmrbjvxag",  # Lorenteggio · RASPBERRY
    "agent:p0n57t7vtpq0",  # Corvetto · scuola-di-atene
    "agent:bzs050xacs1m",  # Giambellino · RED-AGENT
    "agent:ccjzqm7yekwx",  # Nord · agentic-paguri
    "agent:pxvd2kk2yx5j",  # Greco · tartufi-pazzi
    "agent:nkh372rd16yq",  # Bicocca · p4radigm
    "agent:txar5994gbkj",  # Bovisa · prompt-soccorso
    "agent:mkfzagfe8jtt",  # Isola · Diuo
})
TRUSTED_RING_AGENTS = TRUSTED_RING_BOOKING_CENTRES | TRUSTED_RING_COUNSELING

DAY_ALIASES: dict[str, str] = {
    "mon": "mon", "lun": "mon", "lunedi": "mon", "lunedì": "mon", "monday": "mon",
    "tue": "tue", "mar": "tue", "martedi": "tue", "martedì": "tue", "tuesday": "tue",
    "wed": "wed", "mer": "wed", "mercoledi": "wed", "mercoledì": "wed", "wednesday": "wed",
    "thu": "thu", "gio": "thu", "giovedi": "thu", "giovedì": "thu", "thursday": "thu",
    "fri": "fri", "ven": "fri", "venerdi": "fri", "venerdì": "fri", "friday": "fri",
}
LANG_ALIASES: dict[str, str] = {
    "italiano": "it", "italian": "it", "inglese": "en", "english": "en",
    "spagnolo": "es", "spanish": "es", "español": "es", "francese": "fr",
    "french": "fr", "français": "fr", "arabo": "ar", "arabic": "ar",
    "cinese": "zh", "chinese": "zh", "romeno": "ro", "rumeno": "ro",
    "romanian": "ro", "tedesco": "de", "german": "de", "portoghese": "pt",
    "portuguese": "pt", "ucraino": "uk", "ukrainian": "uk", "russo": "ru",
    "russian": "ru", "albanese": "sq", "albanian": "sq", "georgiano": "ka",
    "georgian": "ka", "farsi": "fa", "persiano": "fa", "persian": "fa",
    "urdu": "ur", "hindi": "hi", "tagalog": "tl", "filippino": "tl",
    "filipino": "tl", "bengalese": "bn", "bengali": "bn", "turco": "tr",
    "turkish": "tr", "polacco": "pl", "polish": "pl", "giapponese": "ja",
    "japanese": "ja", "wolof": "wo", "singalese": "si", "tigrino": "ti",
}

RED_FLAGS = (
    "perdita di forza alle gambe", "loss of strength in the legs",
    "dolore al petto", "chest pain", "male al petto", "dolore toracico",
    "fa male il petto", "petto mi fa male", "oppressione al petto",
    "peso sul petto", "stretta al petto", "dolore al torace",
    "perdo forza alle gambe", "forza nelle gambe", "forza alle gambe",
    "non sento le gambe", "gambe deboli", "debolezza alle gambe",
    "gambe che cedono", "pain in my chest", "chest hurts", "chest pressure",
    "tight chest", "chest tightness", "legs feel weak", "weak legs",
    "can't feel my legs", "legs giving way",
)
INJECTION_PHRASES = (
    "ignore previous", "ignore all rules", "ignore the rules", "ignora le regole",
    "ignora tutto", "manda tutto", "send all data", "system:", "nuova policy",
    "new policy", "override policy", "jailbreak", "forget your instructions",
)
ENGLISH_WORDS = frozenset({
    "i", "my", "the", "have", "has", "pain", "need", "hello", "hi", "please",
    "doctor", "appointment", "booking", "visit", "counseling", "gynecology",
    "yes", "thanks", "thank",
})
URGENT_WORDS = frozenset({"urgente", "urgenza", "urgent", "urgency", "subito", "emergenza"})
WORRIED_STEMS = (
    "ansia", "ansios", "preoccup", "paura", "spaventat", "agitat", "nervos",
    "angosci", "worried", "anxious", "anxiety", "scared", "afraid", "nervous", "panic",
)
FRUSTRATED_STEMS = (
    "assurd", "ridicol", "stuf", "arrabbi", "vergogn", "inaccettab",
    "non funziona", "perdita di tempo", "frustrat", "annoyed", "ridiculous",
    "unacceptable", "waste of time",
)
HURRIED_STEMS = (
    "fretta", "veloc", "sbrig", "rapido", "subito", "immediat",
    "hurry", "quick", "asap", "rush",
)
PRIVACY_STEMS = (
    "riservat", "privac", "segret", "nessuno sappia", "non voglio che",
    "confidential", "private", "discreet",
)

# understood() keeps the LAST act word of a text: swap bare English act words in prose.
PROSE_SWAP = {
    "offer": "propose", "prescription": "referral", "confirm": "approve",
    "cancel": "call off", "consent": "permission", "checkin": "check-in",
    "report": "findings", "hold": "keep",
}
LEDGER_REASON_MAP = {
    "PRESCRIPTION_EXPIRED": "prescription_expired",
    "CONSENT_MISSING": "consent_missing",
    "NO_CAPACITY": "no_capacity",
    "SLOT_TAKEN": "slot_taken",
    "ALREADY_BOOKED": "slot_taken",
    "HOLD_EXPIRED": "expired",
    "PAST": "past",
    "SLOT_IN_PAST": "past",
}

_TOKEN_SAFE = re.compile(r"^[A-Za-z0-9_.:/@-]+$")
_REFUSAL_CODE_RE = re.compile(r"\b([A-Z][A-Z0-9_]{2,40})\b")
_CITIZEN_ID_RE = re.compile(r"\bcitizen:[A-Za-z0-9_.:/@-]+\b")
_RECENT_SENDS: dict[tuple[str, str], float] = {}
LOG = logging.getLogger("family_counseling")


def words(text: str) -> set[str]:
    return {tok.strip(".,;:!?()\"'").lower() for tok in text.split() if tok.strip()}


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


def safe_token(value: str | None) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 128 or not _TOKEN_SAFE.fullmatch(cleaned):
        return ""
    return cleaned


def parse_when(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def is_injection(text: str) -> bool:
    lowered = (text or "").lower()
    return any(phrase in lowered for phrase in INJECTION_PHRASES)


def is_emergency(need: Need, message: Message) -> bool:
    if need.is_emergency or understood(message).body.get("is_emergency") is True:
        return True
    lowered = (message.text or need.summary).lower()
    patterns = [re.escape(flag) for flag in RED_FLAGS]
    patterns.extend((r"dolore\s+(?:al|nel|sul)\s+torace", r"dolore\s+(?:al|nel|sul)\s+petto"))
    for match in re.finditer("|".join(patterns), lowered):
        prefix = lowered[max(0, match.start() - 32) : match.start()]
        if re.search(r"\b(?:non|nessun[ao]?|senza|no|without)\b[^,.!?;]*$", prefix):
            continue
        return True
    return False


def family_emergency(message: Message) -> bool:
    """Explicit emergency detector (kept compatible with test_family_emergency.py)."""
    if understood(message).body.get("is_emergency") is True:
        return True
    text = message.text.lower()
    pattern = r"dolore\s+(?:al|nel|sul)\s+(?:petto|torace)|chest pain|non sento le gambe|perdita di forza alle gambe"
    for match in re.finditer(pattern, text):
        prefix = text[max(0, match.start() - 28) : match.start()]
        if not re.search(r"\b(?:non\s+(?:ho|sento|avverto)|senza|nessun[ao]?|no|without)\s+(?:un\s+|forte\s+)?$", prefix):
            return True
    return False


def asks_address(message: Message) -> bool:
    if message.text:
        named, asked = False, False
        for token in message.text.split():
            bare = token.strip(".,;:!?()\"'")
            if bare == "data.request":
                named = True
            key, eq, value = bare.partition("=")
            if eq and key == "fields":
                asked = asked or "address" in {p.strip() for p in value.split(",") if p.strip()}
        return named and asked
    if message.type != "data.request":
        return False
    raw = message.body.get("fields")
    if isinstance(raw, list):
        return "address" in raw
    if isinstance(raw, str):
        return "address" in {p.strip() for p in raw.replace(",", " ").split() if p.strip()}
    return False


def own_street(city: City) -> str:
    street = getattr(city, "address", "")
    if isinstance(street, str) and street.strip():
        return street.strip()
    env = os.environ.get("CITY_ADDRESS", "").strip()
    if env:
        return env
    try:
        return Path(__file__).with_name("door.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def citizen_of(chat: list[Message]) -> str | None:
    for row in chat:
        if row.sender.startswith("citizen:"):
            return row.sender
    for row in chat:
        fields = msg_fields(row)
        for key in ("citizen", "patient"):
            party = safe_token(fields.get(key))
            if party.startswith("citizen:"):
                return party
    return None


def we_sent(chat: list[Message], agent: str, token: str, *, to: str, word: str) -> bool:
    for row in chat:
        if row.sender != agent or row.recipient != to:
            continue
        if token in row.text.split() and word in words(row.text):
            return True
    return False


def exact_sent(chat: list[Message], agent: str, text: str) -> bool:
    return any(row.sender == agent and row.text.strip() == text for row in chat)


def strip_foreign_citizens(text: str, allowed_citizen: str | None) -> str:
    def _replace(match: re.Match[str]) -> str:
        found = match.group(0)
        return found if (allowed_citizen and found == allowed_citizen) else "[redacted]"
    return _CITIZEN_ID_RE.sub(_replace, text)


def clean_prose(text: str) -> str:
    """Keep leading act word and key=value tokens intact; swap bare English act words in prose."""
    out: list[str] = []
    for line in text.split("\n"):
        head, sep, rest = line.partition(" ")
        if rest:
            tokens = []
            for word in rest.split(" "):
                if "=" in word:
                    tokens.append(word)
                else:
                    bare = word.strip(".,;:!?()\"'").lower()
                    swapped = PROSE_SWAP.get(bare)
                    if swapped and bare:
                        tokens.append(re.sub(rf"(?i)\b{re.escape(bare)}\b", swapped, word))
                    else:
                        tokens.append(word)
            rest = " ".join(tokens)
        out.append(head + sep + rest)
    return "\n".join(out)


def reset_dedup_cache() -> None:
    _RECENT_SENDS.clear()


async def say(
    city: City,
    to: str,
    text: str,
    *,
    allowed_citizen: str | None = None,
    dedup: bool = True,
) -> bool:
    if to != LEDGER:
        permitted = allowed_citizen or (to if to.startswith("citizen:") else None)
        text = clean_prose(strip_foreign_citizens(text, permitted))
        if dedup and text:
            need_scope = getattr(getattr(city, "inbound", None), "need_id", "") or ""
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            now = time.monotonic()
            for k in [k for k, ts in _RECENT_SENDS.items() if now - ts > DEDUP_SECONDS]:
                _RECENT_SENDS.pop(k, None)
            dedup_key = (f"{need_scope}:{to}", digest)
            if dedup_key in _RECENT_SENDS:
                return True
            _RECENT_SENDS[dedup_key] = now
    try:
        await city.say(to, text[:2000])
    except CapError as exc:
        LOG.warning("family_counseling.say.failed", extra={"code": exc.code, "to": to})
        return False
    return True


def is_english(chat: list[Message], text: str = "") -> bool:
    for row in reversed(chat):
        if row.sender.startswith("citizen:"):
            lang = msg_fields(row).get("lang", "").lower()
            if lang == "en":
                return True
            if lang == "it":
                return False
    combined = text or " ".join(r.text for r in chat if r.sender.startswith("citizen:"))
    return len(words(combined) & ENGLISH_WORDS) >= 2


def citizen_tone(chat: list[Message], text: str = "") -> str:
    samples = [text] if text else []
    samples.extend(row.text for row in chat if row.sender.startswith("citizen:"))
    joined = " ".join(samples).lower()
    if any(s in joined for s in WORRIED_STEMS):
        return "anxious"
    if any(s in joined for s in FRUSTRATED_STEMS):
        return "frustrated"
    if any(s in joined for s in PRIVACY_STEMS):
        return "privacy"
    if any(s in joined for s in HURRIED_STEMS):
        return "hurried"
    return "neutral"


def empathy_lead(*, english: bool, tone: str = "") -> str:
    if tone == "anxious":
        return (
            "I understand your concern, and you are in safe hands: we are here to support you in full confidentiality. "
            if english else
            "Capisco la sua preoccupazione, può stare tranquilla: siamo qui per assisterla nella massima riservatezza. "
        )
    if tone == "frustrated":
        return (
            "I understand the inconvenience and I will take care of this right away for you. "
            if english else
            "Capisco il disagio e mi occupo subito personalmente di trovare la soluzione migliore per lei. "
        )
    if tone == "privacy":
        return (
            "Your request is handled with strict confidentiality and direct access. "
            if english else
            "La sua richiesta è gestita nella massima riservatezza e con accesso diretto. "
        )
    if tone == "hurried":
        return (
            "Right away — let's handle this quickly for you. "
            if english else
            "Subito — procediamo nel modo più rapido possibile per lei. "
        )
    return ""


def slot_schedule_hint(chat: list[Message], slot_id: str) -> str:
    if not slot_id:
        return ""
    tok = f"slot_id={slot_id}"
    for row in reversed(chat):
        for line in row.text.splitlines():
            if tok in line.split():
                f = fields_of(line)
                when = f.get("agenda_date") or f.get("at") or ""
                day = f.get("agenda_day") or f.get("day") or ""
                return f"{day} {when}".strip() if when else ""
    return ""


def latest_ledger(chat: list[Message], head: str, token: str = "") -> Message | None:
    found: Message | None = None
    for row in chat:
        if row.sender == LEDGER and first_word(row.text) == head:
            if not token or token in row.text.split():
                found = row
    return found


def latest_slots(chat: list[Message]) -> str | None:
    found: str | None = None
    for row in chat:
        if row.sender == LEDGER and first_word(row.text) in {"slots", "world"}:
            if "slot_id=" in row.text or "slots=" in row.text:
                found = row.text
    return found


def agenda_now(chat: list[Message]) -> datetime | None:
    for row in reversed(chat):
        if row.sender != LEDGER:
            continue
        if first_word(row.text) == "agenda":
            parsed = parse_when(fields_of(row.text).get("agenda_now", ""))
            if parsed is not None:
                return parsed
        if row.text.startswith("local ledger:"):
            return datetime.now(UTC)
    return None


def translate_refusal(text: str) -> tuple[str, str]:
    """Map Ledger refusal codes to MACH-1.2 standard reasons (Level 8 RULE SYNC)."""
    fields = fields_of(text)
    raw = ""
    for key in ("reason", "code", "error"):
        val = safe_token(fields.get(key))
        if val:
            raw = val
            break
    if not raw:
        for match in _REFUSAL_CODE_RE.finditer(text):
            code = match.group(1)
            if code not in {"LEDGER", "CONS", "GIN"}:
                raw = code
                break
    upper = raw.upper()
    if upper in LEDGER_REASON_MAP:
        return LEDGER_REASON_MAP[upper], raw
    if raw:
        return raw.lower() if raw.isupper() else raw, raw
    return "hold_refused", ""


async def await_line(
    city: City,
    need_id: str | None,
    chat: list[Message],
    heads: str | tuple[str, ...],
    token: str = "",
    *,
    op: str,
    urgent: bool = False,
) -> tuple[Message, list[Message]] | None:
    wanted = (heads,) if isinstance(heads, str) else heads
    known = {row.msg_id for row in chat}
    fresh = chat
    pause = URGENT_PULL_PAUSE if urgent else PULL_PAUSE
    for attempt in range(PULL_TRIES):
        try:
            fresh = await city.history(need_id=need_id)
        except CapError:
            fresh = chat
        for head in wanted:
            hit = latest_ledger(fresh, head, token)
            if hit is not None:
                return hit, fresh
        for row in fresh:
            if row.msg_id in known or row.sender != LEDGER:
                continue
            if first_word(row.text) == "refused" and (not op or op in words(row.text)):
                return row, fresh
            if row.text.startswith("local ledger:"):
                return row, fresh
        if attempt + 1 < PULL_TRIES:
            await asyncio.sleep(pause)
    return None


async def until(
    city: City,
    need_id: str | None,
    chat: list[Message],
    ready: Callable[[list[Message]], bool],
    *,
    urgent: bool = False,
) -> list[Message]:
    fresh = chat
    pause = URGENT_PULL_PAUSE if urgent else PULL_PAUSE
    for attempt in range(PULL_TRIES):
        try:
            fresh = await city.history(need_id=need_id)
        except CapError:
            fresh = chat
        if ready(fresh):
            return fresh
        if attempt + 1 < PULL_TRIES:
            await asyncio.sleep(pause)
    return fresh
