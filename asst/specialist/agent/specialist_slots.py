"""Slot preference parsing, ranking, and Ledger availability fetch helpers."""
from __future__ import annotations

PRIMARY_PROCEDURE = "VIS-CARD"


def configure(agent_globals: dict[str, object]) -> None:
    globals().update(agent_globals)


# MACH-1.2 RULE SYNC: Ledger codes become the shared cancel reasons every team understands.
_RULE_SYNC = {
    "PRESCRIPTION_EXPIRED": "prescription_expired",
    "CONSENT_MISSING": "consent_missing",
    "NO_CAPACITY": "no_capacity",
    "SLOT_TAKEN": "slot_taken",
    "HOLD_EXPIRED": "hold_expired",
}


def _extract_refusal_reason(text: str) -> str:
    raw = _extract_refusal_reason_raw(text)
    return _RULE_SYNC.get(raw.upper(), raw) if raw.upper() in _RULE_SYNC else raw


def _extract_refusal_reason_raw(text: str) -> str:
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
    include_doctor_attrs: bool = False,
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
    if str(slot.get("urgent_reserve", "")).lower() == "true":
        # MACH-1.2: a reserve slot is marked so no centre offers it to B, D or P.
        parts.append("reserve=U")
    if include_doctor_attrs:
        spec = _DOCTORS.get(slot["doctor_id"].rsplit("-", 1)[-1])
        if spec is not None:
            langs = "/".join(sorted(spec[0]))
            if langs:
                parts.append(f"doctor_lang={langs}")
            if spec[1]:
                parts.append(f"doctor_gender={spec[1]}")
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



