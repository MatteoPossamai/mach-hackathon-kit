"""Message parsing, citizen-safe replies, and Ledger-history helpers."""
from __future__ import annotations


def configure(agent_globals: dict[str, object]) -> None:
    globals().update(agent_globals)


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


