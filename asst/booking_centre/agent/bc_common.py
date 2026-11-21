"""Shared helpers: message tokens, the Ledger mailbox, case history lookups."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime, timedelta

from city_sdk.city import LEDGER, CapError, City, Need, clinical, OutOfScope, understood

TOKEN = re.compile(r"^[A-Za-z0-9_.:/+\-]{1,128}$")
# A prescription number written in free text, without the "prescription_number=" key.
RX_IN_TEXT = re.compile(r"prescription_number-[A-Za-z0-9-]{4,60}")
DAY_CODES = ("mon", "tue", "wed", "thu", "fri")
SERVICES = {"VIS-FIS": "hospital.physiatry", "VIS-CARD": "specialist_care.cardiology"}


# Level 3: a citizen may write only the case code, e.g. "la ricetta N-2026-11-02-890910000081".
NEED_IN_TEXT = re.compile(r"(?<![A-Za-z0-9_-])N-\d{4}-\d{2}-\d{2}-\d{4,}")


def rx_from_text(history: list, citizen: str, text: str) -> str:
    """A prescription number found in free text, newest first, citizen messages only."""

    for candidate in [text] + [row.text for row in reversed(history) if row.sender == citizen]:
        found = RX_IN_TEXT.search(candidate or "")
        if found and TOKEN.fullmatch(found.group(0)):
            return found.group(0)
        bare = NEED_IN_TEXT.search(candidate or "")
        if bare and TOKEN.fullmatch("prescription_number-" + bare.group(0)):
            return "prescription_number-" + bare.group(0)
    return ""


def rx_guess(need_id: str) -> str:
    """The number our GP gives this case's prescription (same rule as the GP's ``_rx``).

    Only a guess: the Ledger must confirm it before anything is offered.
    """

    raw = (need_id or "").upper()
    suffix = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in raw).strip("-")
    number = "prescription_number-" + suffix[:40]
    return number if len(suffix) >= 4 and TOKEN.fullmatch(number) else ""


def ledger_refused(history: list, seen: set, op: str) -> bool:
    """A Ledger refusal of ``op`` that arrived after ``seen`` was taken."""

    return any(row.sender == LEDGER and row.msg_id not in seen and act(row.text) == "refused"
               and op in row.text.lower() for row in history)


# Our own agents (asst/*/card.yaml): never a cross-ASST partner.
TEAM_AGENTS = frozenset({"agent:gdrf9b9kxxss", "agent:nanpr5mgm2py", "agent:c2ewegr2ea3v",
                         "agent:pky3vqyqbepa", "agent:atp772whjkgd"})
CAPABILITY = {"VIS-FIS": "hospital.physiatry", "VIS-CARD": "specialist_care.cardiology",
              "CTRL-CARD": "specialist_care.cardiology", "CONS-GIN": "family_counseling.gynecology"}


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


# understood() keeps the LAST act word of a text: a bare "prescription" or "offer" in the
# sentence would change what the citizen reads. Swap such words in the prose (MACH-1.1).
PROSE_SWAP = {
    "offer": "propose", "prescription": "referral", "confirm": "approve", "cancel": "call off",
    "consent": "permission", "checkin": "check-in", "report": "findings", "hold": "keep",
}


def clean_prose(text: str) -> str:
    out = []
    for line in text.split("\n"):
        head, sep, rest = line.partition(" ")
        if rest:
            rest = " ".join(word if "=" in word else PROSE_SWAP.get(word, word) for word in rest.split(" "))
        out.append(head + sep + rest)
    return "\n".join(out)


async def send(city: City, recipient: str, text: str) -> bool:
    if recipient != LEDGER and text:
        text = clean_prose(text)
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


def fresh_reply(history: list, seen: set, head: str, match: tuple[str, str] | None = None):
    """The newest Ledger reply that arrived after ``seen`` was taken."""

    for row in reversed(history):
        if row.msg_id in seen or row.sender != LEDGER or act(row.text) != head:
            continue
        if match is None or fields(row.text).get(match[0]) == match[1]:
            return row
    return None


def slots_page(row) -> str:
    return fields(row.text.splitlines()[0]).get("page", "1/1").split("/")[0]


def case_number(history: list, citizen: str) -> str:
    rx = ledger_reply(history, "prescription")
    number = fields(rx.text).get("prescription_number", "") if rx else ""
    return number or rx_from_text(history, citizen, "")


def cross_consent(history: list, agent: str, citizen: str) -> str:
    if not sent(history, agent, citizen, "consent purpose=cross_asst"):
        return ""
    return next((fields(row.text).get("consent_ref", "") for row in reversed(history)
                 if row.sender == citizen and act(row.text) == "consent"
                 and fields(row.text).get("purpose") == "cross_asst"), "")
