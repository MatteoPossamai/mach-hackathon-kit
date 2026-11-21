"""Partner identities and the MACH 1.2 availability exchange."""
from __future__ import annotations

from datetime import datetime

from bc_common import CAPABILITY, act, fields, time

# Declared facility IDs from MACH-1.2.md. An ID must also be returned by the
# Index for the service before we send any request. Lambrate is the local ASST.
_ASSTS = (
    ("Precotto", "ro", "s1twcdzg3296", "f2s88vwd3jnf", "nf9pqsgzrwm0"),
    ("Lorenteggio", "", "tmt0bcv0st1f", "n9bf9y6670hp", "mcppmrbjvxag"),
    ("Corvetto", "fr", "x0vr9rbwpesr", "h8425ac7btd8", "p0n57t7vtpq0"),
    ("Giambellino", "ka", "h6r1v45esk77", "hnpe58epeyh1", "bzs050xacs1m"),
    ("Nord", "ur", "gpr1zqe343gb", "zand0f57ec1j", "ccjzqm7yekwx"),
    ("Greco", "si", "xtj1dk9x3qej", "q18es2d82f8r", "pxvd2kk2yx5j"),
    ("Bicocca", "ti", "bb01ggxymm05", "sh35cbsy287d", "nkh372rd16yq"),
    ("Bovisa", "", "gxq3vzgt3h5t", "tty8erdj6t41", "txar5994gbkj"),
    ("Isola", "tl", "", "etpwrfmfa8hm", "mkfzagfe8jtt"),
)
_ROLES = ("hospital", "specialist_care", "family_counseling")
_FACILITIES = {
    f"agent:{agent_id}": (asst, role, rare if role == "hospital" else "")
    for asst, rare, *ids in _ASSTS
    for role, agent_id in zip(_ROLES, ids)
    if agent_id
}
_PRIVATE_FIELDS = {"citizen", "patient", "prescription_number", "name", "age", "consent_ref",
                   "cross_asst_consent_ref"}


# Our own agents never join the ring as partners.
_OWN = frozenset({"agent:gdrf9b9kxxss", "agent:nanpr5mgm2py", "agent:c2ewegr2ea3v",
                  "agent:pky3vqyqbepa", "agent:atp772whjkgd"})
# Other ASSTs' facilities the Index returns beyond the declared list. A partner
# never sees citizen data before consent, and its offer is booked by its own Ledger.
MAX_PARTNERS = 12


def partner_asst(agent_id: str) -> str:
    return _FACILITIES[agent_id][0] if agent_id in _FACILITIES else "un'altra ASST"


def partner_supplies(agent_id: str, procedure: str) -> bool:
    capability = CAPABILITY.get(procedure)
    return bool(capability and agent_id in _FACILITIES
                and _FACILITIES[agent_id][1] == capability.split(".", 1)[0])


def ring_peers(peers: list, procedure: str, language: str, *, capability_lookup: bool) -> list[str]:
    """Declared partners first (rare language match on top), then other ASSTs the Index returns."""

    capability = CAPABILITY.get(procedure)
    family = capability.split(".", 1)[0] if capability else ""
    eligible = {}
    for peer in peers:
        agent_id = getattr(peer, "agent_id", "")
        if not agent_id or agent_id in _OWN:
            continue
        capabilities = getattr(peer, "capabilities", ())
        if capability not in (capabilities or ()) and (capabilities or not capability_lookup):
            continue
        if agent_id in _FACILITIES:
            if not partner_supplies(agent_id, procedure):
                continue
            _, role, rare = _FACILITIES[agent_id]
            if peer.role not in ("facility", role):
                continue
            eligible[agent_id] = 0 if (rare and rare == language) else 1
        elif family and peer.role == family:
            eligible[agent_id] = 2
    return sorted(eligible, key=lambda agent_id: eligible[agent_id])[:MAX_PARTNERS]


def ring_requests(history: list, agent: str) -> dict[str, dict[str, str]]:
    """The ring requests we sent in this case, by partner (read from history, not memory)."""

    requests = {}
    for row in history:
        if (row.sender == agent and row.recipient.startswith("agent:") and row.recipient not in _OWN
                and act(row.text) == "need.request"):
            request = fields(row.text)
            if request.get("procedure"):
                requests[row.recipient] = request
    return requests


def agenda_date(value: str, now: datetime) -> datetime | None:
    parsed = time(value)
    if parsed is not None and parsed.tzinfo is None:
        return parsed.replace(tzinfo=now.tzinfo)
    return parsed


def partner_offer_rows(row, request: dict[str, str], now: datetime):
    """Read only offer lines and normalize wire attributes for the booking filter."""

    for line in row.text.splitlines():
        if act(line) != "offer":
            continue
        item = fields(line)
        if item.get("facility") != row.sender or _PRIVATE_FIELDS.intersection(item):
            continue
        if item.get("request_id") and item["request_id"] != request.get("request_id"):
            continue
        if item.get("procedure") != request.get("procedure") or not item.get("offer_id"):
            continue
        at = agenda_date(item.get("agenda_date", item.get("at", "")), now)
        expiry = agenda_date(item.get("expiry", ""), now)
        if at is None or ("expiry" in item and (expiry is None or expiry <= now)):
            continue
        item["at"] = at.isoformat()
        item["day"] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[at.weekday()]
        for key, alias in (("languages", "doctor_lang"), ("gender", "doctor_gender")):
            if alias in item:
                item[key] = item[alias]
        if item.get("reserve") == "U":
            item["urgent_reserve"] = "true"
        yield item
