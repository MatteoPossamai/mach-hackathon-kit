"""Slot filtering, agenda-weekday deadlines, soft-lock (L7), rotation, and relaxed offers (L5)."""
from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime, timedelta

from city_sdk.city import LEDGER, City, Message

import fc_common as c

# One agenda hour is one real minute. Citizens confirmed 1-8 real minutes after the offer and
# hold/commit took ~4 more, so 4 h slots came back "cancel reason=past" (05:13Z, 07:12Z).
LEAD = timedelta(hours=12)
URGENT_LEAD = timedelta(minutes=30)
DEADLINE_DAYS = {"B": 2, "D": 5}
SOFT_LOCK_TTL_S = 60.0

# slot_id -> (request_id, expires_monotonic)
_SOFT_LOCKS: dict[str, tuple[str, float]] = {}


def reset_soft_locks() -> None:
    _SOFT_LOCKS.clear()


def soft_lock_slots(slot_ids: list[str], request_id: str) -> None:
    if not request_id:
        return
    now = time.monotonic()
    for sid in list(_SOFT_LOCKS):
        if _SOFT_LOCKS[sid][1] <= now:
            _SOFT_LOCKS.pop(sid, None)
    expires = now + SOFT_LOCK_TTL_S
    for sid in slot_ids:
        _SOFT_LOCKS[sid] = (request_id, expires)


def is_soft_locked(slot_id: str, request_id: str = "") -> bool:
    entry = _SOFT_LOCKS.get(slot_id)
    if entry is None:
        return False
    holder, expires = entry
    if time.monotonic() >= expires:
        _SOFT_LOCKS.pop(slot_id, None)
        return False
    return bool(holder and holder != request_id)


def add_agenda_days(start: datetime, agenda_days: int) -> datetime:
    """Advance ``start`` by ``agenda_days`` weekdays (Mon-Fri; Sat/Sun do not exist in agenda)."""
    cur = start
    added = 0
    while added < agenda_days:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            added += 1
    return cur


def normalize_day(raw: str) -> str:
    cleaned = raw.strip().lower()
    return c.DAY_ALIASES.get(cleaned, cleaned)


def extract_nl_prefs(text: str) -> tuple[str | None, str | None, set[str] | None]:
    lowered = text.lower()
    toks = c.words(text)
    lang: str | None = None
    for alias, code in c.LANG_ALIASES.items():
        if alias in toks:
            lang = code
            break
    gender: str | None = None
    if any(p in lowered for p in ("dottoressa", "medico donna", "donna", "femmina", "female doctor", "female", "woman")):
        gender = "f"
    elif any(p in lowered for p in ("medico uomo", "dottore uomo", "maschio", "male doctor", "male")):
        gender = "m"
    days: set[str] = set()
    for alias, canonical in c.DAY_ALIASES.items():
        if len(alias) >= 4 and alias in toks:
            days.add(canonical)
    return lang, gender, (days or None)


def citizen_prefs(chat: list[Message], message: Message) -> tuple[str, str | None, set[str] | None, str | None]:
    """Return ``(lang, gender, days, priority)``; explicit tokens always win over prose."""
    tok_lang, nl_lang_found = "", ""
    gender: str | None = None
    days: set[str] = set()
    priority: str | None = None
    for row in [*chat, message]:
        if not row.sender.startswith("citizen:"):
            continue
        f = c.msg_fields(row)
        if f.get("lang"):
            tok_lang = c.safe_token(f.get("lang")).lower() or tok_lang
        g = c.safe_token(f.get("gender_preference") or f.get("gender")).lower()
        if g in {"m", "f"}:
            gender = g
        d_raw = f.get("days") or f.get("day") or ""
        for part in re.split(r"[/,|]", d_raw):
            if part.strip():
                days.add(normalize_day(part))
        p = c.safe_token(f.get("priority")).upper()
        if p in {"U", "B", "D", "P"}:
            priority = p
        nl_l, nl_g, nl_d = extract_nl_prefs(row.text)
        if not nl_lang_found and nl_l:
            nl_lang_found = nl_l
        if not gender and nl_g:
            gender = nl_g
        if nl_d:
            days.update(nl_d)
    lang = tok_lang or nl_lang_found or ("en" if c.is_english(chat, message.text) else "it")
    return lang, gender, (days or None), priority


def world_doctors(world_text: str) -> dict[str, tuple[frozenset[str], str, frozenset[str]]]:
    found: dict[str, tuple[frozenset[str], str, frozenset[str]]] = {}
    for line in world_text.splitlines():
        if "slot_id=" in line or "doctor_id=" not in line:
            continue
        f = c.fields_of(line)
        doc_id = c.safe_token(f.get("doctor_id"))
        if not doc_id:
            continue
        langs = frozenset(p.strip().lower() for p in re.split(r"[/,|]", f.get("languages") or f.get("lang") or "") if p.strip())
        gender = (f.get("gender") or "").strip().lower()
        days = frozenset(normalize_day(p) for p in re.split(r"[/,|]", f.get("days") or "") if p.strip())
        if langs or gender or days:
            found[doc_id] = (langs, gender, days)
    return found


def speaks(doctor_id: str, lang: str, dyn_docs: dict[str, tuple[frozenset[str], str, frozenset[str]]] | None = None) -> bool:
    if not lang:
        return True
    dyn = (dyn_docs or {}).get(doctor_id)
    if dyn is not None and dyn[0]:
        return lang in dyn[0] or ("*" in dyn[0] and lang not in {"it", "en"})
    spec = c.DOCTORS.get(doctor_id.rsplit("-", 1)[-1])
    if spec is None:
        return True
    return lang in spec[0] or ("*" in spec[0] and lang not in {"it", "en"})


def rotate_slots(slots: list[dict[str, str]], seed: str) -> list[dict[str, str]]:
    """Deterministically rotate slots per need_id to reduce concurrent hold collisions (L7)."""
    if len(slots) <= 1 or not seed:
        return slots
    offset = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16) % len(slots)
    return slots[offset:] + slots[:offset]


def fitting_slots(
    world_text: str,
    *,
    now: datetime,
    agent_id: str,
    priority: str | None,
    lang: str,
    gender: str | None = None,
    days: set[str] | None = None,
    after: datetime | None = None,
    before: datetime | None = None,
    deadline: datetime | None = None,
    exclude_slots: set[str] | None = None,
    request_id: str = "",
    respect_soft_lock: bool = False,
) -> list[dict[str, str]]:
    prio = (priority or "").upper() or None
    norm_days = {normalize_day(d) for d in days} if days else None
    excluded = exclude_slots or set()
    dyn_docs = world_doctors(world_text)

    if prio == "U":
        today_end: datetime | None = now.replace(hour=23, minute=59, second=59, microsecond=0)
        fallback_horizon: datetime | None = add_agenda_days(now, 2)
        earliest = now + URGENT_LEAD
    elif prio == "P":
        today_end, fallback_horizon, earliest = None, None, now + LEAD
    else:
        today_end = add_agenda_days(now, DEADLINE_DAYS.get(prio or "", 10))
        fallback_horizon, earliest = None, now + LEAD

    if after and after > earliest:
        earliest = after

    lines = [line for line in world_text.splitlines() if "slot_id=" in line]

    def _collect(horizon: datetime | None) -> list[dict[str, str]]:
        eff_horizon = horizon
        for bound in (before, deadline):
            if bound is not None and (eff_horizon is None or bound < eff_horizon):
                eff_horizon = bound
        own_slots: list[tuple[int, datetime, dict[str, str]]] = []
        other_slots: list[tuple[int, datetime, dict[str, str]]] = []
        for line in lines:
            slot = c.fields_of(line)
            slot_id = c.safe_token(slot.get("slot_id"))
            doctor = c.safe_token(slot.get("doctor_id"))
            at = c.parse_when(slot.get("at") or slot.get("agenda_date", ""))
            if not slot_id or not doctor or at is None or slot_id in excluded:
                continue
            if respect_soft_lock and is_soft_locked(slot_id, request_id):
                continue
            if slot.get("procedure") not in (None, c.PROCEDURE):
                continue
            free = slot.get("free")
            if free is not None and free.isdigit() and int(free) <= 0:
                continue
            is_reserve = slot.get("urgent_reserve", "").lower() == "true" or slot.get("reserve", "").upper() == "U"
            if is_reserve and prio != "U":
                continue
            if at <= earliest or (eff_horizon is not None and at >= eff_horizon):
                continue
            inline_langs = frozenset(
                p.strip().lower()
                for p in re.split(r"[/,|]", slot.get("doctor_lang") or slot.get("languages") or "")
                if p.strip()
            )
            if inline_langs:
                if lang and lang not in inline_langs and not ("*" in inline_langs and lang not in {"it", "en"}):
                    continue
            elif not speaks(doctor, lang, dyn_docs):
                continue
            dyn = dyn_docs.get(doctor)
            spec = c.DOCTORS.get(doctor.rsplit("-", 1)[-1])
            doc_gender = (
                (slot.get("doctor_gender") or slot.get("gender") or "").strip().lower()
                or (dyn[1] if dyn and dyn[1] else (spec[1] if spec else ""))
            )
            doc_days = dyn[2] if dyn and dyn[2] else (spec[2] if spec else frozenset())
            day = normalize_day(slot.get("day") or slot.get("agenda_day", ""))
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


def relaxed_slots(
    world_text: str,
    *,
    now: datetime,
    agent_id: str,
    priority: str | None,
    lang: str,
    gender: str | None,
    days: set[str] | None,
    after: datetime | None = None,
    before: datetime | None = None,
    deadline: datetime | None = None,
    exclude_slots: set[str] | None = None,
    request_id: str = "",
    respect_soft_lock: bool = False,
) -> tuple[list[dict[str, str]], str]:
    """Level 5: relax one constraint at a time (days -> gender -> window), keeping language strict."""
    attempts: list[tuple[str, dict]] = []
    if days:
        attempts.append(("days", {"days": None, "gender": gender, "after": after, "before": before}))
    if gender:
        attempts.append(("gender", {"days": days, "gender": None, "after": after, "before": before}))
    if after or before:
        attempts.append(("time", {"days": days, "gender": gender, "after": None, "before": None}))
    if days and gender:
        attempts.append(("days", {"days": None, "gender": None, "after": None, "before": None}))
    for tag, overrides in attempts:
        found = fitting_slots(
            world_text,
            now=now,
            agent_id=agent_id,
            priority=priority,
            lang=lang,
            deadline=deadline,
            exclude_slots=exclude_slots,
            request_id=request_id,
            respect_soft_lock=respect_soft_lock,
            **overrides,
        )
        if found:
            return found, tag
    return [], ""


def page_info(text: str) -> tuple[int, int]:
    raw = c.fields_of(text.splitlines()[0] if text else "").get("page", "")
    left, _, right = raw.partition("/")
    return (int(left), int(right)) if (left.isdigit() and right.isdigit()) else (1, 1)


def asked_world(chat: list[Message], agent: str, page: int) -> bool:
    p_tok, proc_tok = f"page={page}", f"procedure={c.PROCEDURE}"
    return any(
        row.sender == agent and row.recipient == LEDGER
        and p_tok in row.text.split() and proc_tok in row.text.split()
        for row in chat
    )


def offer_line(
    slot: dict[str, str],
    *,
    agent_id: str,
    priority: str | None = None,
    request_id: str | None = None,
    for_partner: bool = False,
    relaxed_tag: str = "",
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
        f"procedure={slot.get('procedure', c.PROCEDURE)}",
        f"agenda_date={at_val}",
        f"expiry={at_val}",
    ]
    if day_val:
        parts.append(f"agenda_day={day_val}")
    if priority:
        parts.append(f"priority={priority}")
    if for_partner:
        spec = c.DOCTORS.get(slot["doctor_id"].rsplit("-", 1)[-1])
        if spec is not None:
            langs = "/".join(sorted(spec[0]))
            if langs:
                parts.append(f"doctor_lang={langs}")
            if spec[1]:
                parts.append(f"doctor_gender={spec[1]}")
        if slot.get("urgent_reserve", "").lower() == "true":
            parts.append("reserve=U")
        if relaxed_tag:
            parts.append(f"relaxed={relaxed_tag}")
        if request_id:
            parts.append(f"request_id={request_id}")
    return " ".join(parts)


async def fetch_agenda_and_world(
    city: City, need_id: str | None, chat: list[Message], *, urgent: bool = False
) -> tuple[datetime | None, str | None, list[Message]]:
    agent = city.agent_id
    now, world = c.agenda_now(chat), c.latest_slots(chat)
    if now is not None and world is not None:
        return now, world, chat
    if now is None and not c.exact_sent(chat, agent, "agenda"):
        await c.say(city, LEDGER, "agenda")
    if world is None and not asked_world(chat, agent, 1):
        await c.say(city, LEDGER, f"world procedure={c.PROCEDURE} page=1")
    fresh = await c.until(
        city, need_id, chat,
        lambda rows: c.agenda_now(rows) is not None and c.latest_slots(rows) is not None,
        urgent=urgent,
    )
    return c.agenda_now(fresh), c.latest_slots(fresh), fresh


def cancelled_slots(chat: list[Message], agent: str) -> set[str]:
    dropped: set[str] = set()
    for row in chat:
        if row.sender == agent and "cancel" in c.words(row.text):
            slot = c.safe_token(c.fields_of(row.text).get("slot_id"))
            if slot:
                dropped.add(slot)
    return dropped
