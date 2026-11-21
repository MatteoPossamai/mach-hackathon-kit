"""Family counseling agent brain (``family_counseling.gynecology``, ASST asst-lambrate).

Modular entry point & router (AGENTS.md):
- ``fc_common.py``: constants, parsing, prose hygiene, 120s dedup, Ledger helpers, RULE SYNC.
- ``fc_slots.py``: agenda-weekday deadlines, 60s soft-lock (L7), slot rotation, relaxed offers (L5).
- ``fc_ring.py``: MACH-1.2 TRUST RING impostor defense (L8), cross-ASST discovery & consent.
- ``fc_booking.py``: direct citizen offers (no prescription), partner availability, hold/commit, check-in.
"""
from __future__ import annotations

import asyncio
import heapq
import sys
from collections import OrderedDict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from city_sdk.city import LEDGER, City, Message, Need, OutOfScope, clinical, understood

import fc_booking as bk
import fc_common as c
import fc_ring as ring
import fc_ack

# Exported for existing unit tests (tests/test_family_emergency.py).
_family_emergency = c.family_emergency

_NORMAL_YIELD_CAP_S = 1.5
_MAX_NORMAL_CONCURRENCY = 4
_URGENT_CAP = 4096


class _Gate:
    __slots__ = ("lock", "turns")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.turns = 0


class _PriorityScheduler:
    """Urgent-first scheduler: rank-0 (emergency, U, rebook_of) preempts normal turns."""

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
            if self._urgent_active == 0 and self._normal_active < _MAX_NORMAL_CONCURRENCY and not self._waiters:
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


_GATES: dict[str, _Gate] = {}
_URGENT_NEEDS: OrderedDict[str, None] = OrderedDict()
_SCHEDULER = _PriorityScheduler()


def _mark_urgent(key: str) -> None:
    if not key:
        return
    _URGENT_NEEDS[key] = None
    _URGENT_NEEDS.move_to_end(key)
    while len(_URGENT_NEEDS) > _URGENT_CAP:
        _URGENT_NEEDS.popitem(last=False)


def _turn_priority_rank(need: Need, message: Message, key: str) -> int:
    if need.is_emergency or key in _URGENT_NEEDS:
        return 0
    f = c.msg_fields(message)
    prio = f.get("priority", "").upper()
    # MACH-1.2: rebook_of requests jump the queue for <5s reply.
    if prio == "U" or f.get("rebook_of") or f.get("urgent", "").lower() in {"true", "yes", "1"}:
        _mark_urgent(key)
        return 0
    w = c.words(message.text)
    if message.sender != LEDGER and (w & c.URGENT_WORDS):
        _mark_urgent(key)
        return 0
    if prio == "B" or "confirm" in w or "checkin" in w or message.sender == LEDGER:
        return 1
    if prio == "P":
        return 3
    return 2


async def main(need: Need, city: City) -> None:
    message = city.inbound
    if message is None or message.sender == city.agent_id:
        return
    if fc_ack.untargeted_request(message, need, ("family", "counsel", "consultor", "gynec", "ginec", "cons-gin")):
        return
    fc_ack.resilient_history(city)

    text = message.text or need.summary
    if text == "__error__" or need.summary == "__error__":
        raise RuntimeError("application")

    # Red-flag emergency bypasses all queues and stops immediately after escalation.
    if message.sender.startswith("citizen:") and (c.is_emergency(need, message) or c.family_emergency(message)):
        case = await city.history(need_id=message.need_id)
        if not c.we_sent(case, city.agent_id, "escalate.emergency", to=message.sender, word="escalate.emergency"):
            english = c.is_english(case, text)
            msg = (
                "escalate.emergency reason=red_flag Your symptoms indicate an emergency. "
                "Please call 112 immediately or go to the nearest emergency room."
                if english else
                "escalate.emergency reason=red_flag I sintomi indicano un'emergenza. "
                "Chiami subito il 112 o si rechi immediatamente al pronto soccorso."
            )
            await c.say(city, message.sender, msg, allowed_citizen=message.sender, dedup=False)
        return

    if message.sender != LEDGER and clinical(text):
        raise OutOfScope()

    if message.sender.startswith("agent:") and not await ring.is_trusted_agent(city, message.sender):
        return

    street = c.own_street(city)
    if street and c.asks_address(message):
        await c.say(city, message.sender, f"address={street}")
        return

    key = message.need_id or message.msg_id or "default"
    rank = _turn_priority_rank(need, message, key)
    urgent = rank == 0
    await _SCHEDULER.enter(rank)
    try:
        gate = _GATES.get(key)
        if gate is None:
            gate = _GATES[key] = _Gate()
        gate.turns += 1
        try:
            async with gate.lock:
                case = await city.history(need_id=message.need_id)
                if message.sender.startswith("citizen:") and c.we_sent(
                    case, city.agent_id, "escalate.emergency", to=message.sender, word="escalate.emergency"
                ):
                    return
                work = _dispatch(city, message, case, key, urgent=urgent)
                if message.sender.startswith("citizen:") and fc_ack.first_contact(case, city.agent_id, message.sender):
                    note = fc_ack.ACK_EN if c.is_english(case, text) else fc_ack.ACK_IT
                    await fc_ack.with_delayed_ack(
                        city, message, work,
                        lambda cy, to, t: c.say(cy, to, t, allowed_citizen=message.sender), note)
                else:
                    await work
        finally:
            gate.turns -= 1
            if gate.turns == 0 and _GATES.get(key) is gate:
                del _GATES[key]
    finally:
        await _SCHEDULER.leave(rank)


async def _resume_pending_offers(city: City, chat: list[Message], key: str, *, urgent: bool) -> None:
    for row in reversed(chat):
        if row.sender in (LEDGER, city.agent_id):
            continue
        kind = c.words(row.text)
        if "confirm" in kind or "checkin" in kind:
            return
        if row.sender.startswith("agent:"):
            if c.first_word(row.text) in {"ack", "handoff", "booking.notice"}:
                continue
            await bk.on_agent_availability_request(city, row, chat, key, urgent=urgent)
            return
        if row.sender.startswith("citizen:"):
            await bk.offer_to_citizen(city, row, chat, key, replacing=False, urgent=urgent)
            return


async def _on_ledger_refused(city: City, message: Message, chat: list[Message], key: str) -> None:
    kind = c.words(message.text)
    if "hold" in kind or "commit" in kind:
        await bk.on_booking_refused(city, message, chat, key)
    elif "checkin" in kind:
        citizen = c.citizen_of(chat)
        if not citizen:
            return
        if "DUPLICATE" in message.text.upper():
            booking_id = c.safe_token(c.fields_of(message.text).get("booking_id"))
            if booking_id:
                await bk.confirm_checkin(city, booking_id, citizen, chat)
        else:
            reason, _ = c.translate_refusal(message.text)
            msg = (
                f"We could not register your arrival ({reason}). Please verify your receipt."
                if c.is_english(chat) else
                f"Non è stato possibile registrare il suo arrivo ({reason}). Verifichi la ricevuta."
            )
            await c.say(city, citizen, msg, allowed_citizen=citizen)


async def _dispatch(city: City, message: Message, chat: list[Message], key: str, *, urgent: bool) -> None:
    head = c.first_word(message.text)
    kind = c.words(message.text)
    parsed = understood(message)

    if message.sender == LEDGER:
        if head == "held":
            await bk.on_held(city, message, chat)
        elif head == "committed":
            await bk.on_committed(city, message, chat)
        elif head == "checked_in":
            booking_id = c.safe_token(c.fields_of(message.text).get("booking_id"))
            citizen = c.citizen_of(chat)
            if booking_id and citizen:
                await bk.confirm_checkin(city, booking_id, citizen, chat)
        elif head == "refused":
            await _on_ledger_refused(city, message, chat, key)
        elif head in {"slots", "world", "agenda"}:
            await _resume_pending_offers(city, chat, key, urgent=urgent)
        return

    if message.sender.startswith("citizen:") and (parsed.type == "checkin" or "checkin" in kind):
        await bk.on_checkin(city, message, chat, urgent=urgent)
        return

    f = c.msg_fields(message)
    if (
        message.sender.startswith("citizen:")
        and (
            parsed.type == "consent"
            or "consent" in kind
            or ring.awaiting_cross_asst_consent(chat, city.agent_id, current_msg_id=message.msg_id)
        )
        and "confirm" not in kind
        and "slot_id" not in f
        and "offer_id" not in f
    ):
        if ring.awaiting_cross_asst_consent(chat, city.agent_id, current_msg_id=message.msg_id):
            await ring.on_cross_asst_consent_reply(city, message, chat)
            return
        if c.safe_token(f.get("consent_ref")) and await bk.resume_confirm_with_consent(city, message, chat, key):
            return

    if (parsed.type == "confirm" or "confirm" in kind) and (
        message.sender.startswith("citizen:") or message.sender.startswith("agent:")
    ):
        await bk.on_confirm(city, message, chat, key, urgent=urgent)
        return

    if message.sender.startswith("agent:"):
        if parsed.type == "booking.receipt" or "booking.receipt" in kind:
            await ring.on_partner_receipt(city, message, chat, bk.receipt_text)
            return
        if parsed.type == "cancel" or "cancel" in kind:
            await ring.on_partner_cancel(city, message, chat)
            return
        if parsed.type == "offer" or "offer" in kind:
            await ring.on_partner_offer(city, message, chat)
            return
        if head in {"ack", "handoff", "booking.notice"}:
            # A notice or an acknowledgement is not an availability request: no ping-pong.
            return
        await bk.on_agent_availability_request(city, message, chat, key, urgent=urgent)
        return

    if message.sender.startswith("citizen:"):
        await bk.offer_to_citizen(city, message, chat, key, replacing=False, urgent=urgent)
