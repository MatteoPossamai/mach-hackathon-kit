"""A warm first reply when the real one is slow (Ledger or relay under load).

Journey 394 (21/11 11:19 CET): a new citizen got no reply at all for 4+ minutes,
while the turn waited on the Ledger. city:review gives -1 to a citizen left without an answer
and +1 to a proactive, respectful one. The note goes out only if, after ACK_DELAY seconds,
nothing at all has been sent to the citizen after their message: fast turns are unchanged.
"""
from __future__ import annotations

import asyncio

ACK_DELAY = 8.0

ACK_IT = ("Grazie per avermi scritto: ho ricevuto il suo messaggio e me ne occupo subito. "
          "Controllo le disponibilità del consultorio e le scrivo appena ho gli orari.")
ACK_EN = ("Thank you for writing: I have received your message and I am working on it now. "
          "I am checking the clinic times and will write to you as soon as I have them.")


def first_contact(case: list, agent: str, citizen: str) -> bool:
    return not any(row.sender == agent and row.recipient == citizen for row in case)


async def with_delayed_ack(city, message, work, say, text: str, delay: float | None = None) -> None:
    """Run ``work``; if it is still silent towards the citizen after ``delay``, send ``text``."""

    async def ack() -> None:
        await asyncio.sleep(ACK_DELAY if delay is None else delay)
        try:
            rows = await city.history(need_id=message.need_id)
        except Exception:  # noqa: BLE001 - a busy relay is exactly when the note matters
            rows = []
        after = next((i for i, row in enumerate(rows) if row.msg_id == message.msg_id), -1)
        if any(row.sender == city.agent_id and row.recipient == message.sender for row in rows[after + 1:]):
            return
        await say(city, message.sender, text)

    task = asyncio.create_task(ack())
    try:
        await work
    finally:
        task.cancel()


HISTORY_RETRY = (1.0, 2.0, 4.0)


def resilient_history(city):
    """Make every ``city.history`` of this turn retry a busy relay (429/5xx/timeout).

    An unprotected history read that hit a 429 killed the turn before any reply: the citizen
    who wrote again at 12:02 (journey 2840007) still had no answer at 12:07."""
    if getattr(city, "_ack_resilient", False):
        return city
    raw = city.history

    async def history(*args, **kwargs):
        for pause in HISTORY_RETRY:
            try:
                return await raw(*args, **kwargs)
            except Exception:  # noqa: BLE001 - CapError, httpx timeouts: transient under load
                await asyncio.sleep(pause)
        return await raw(*args, **kwargs)

    city.history = history
    city._ack_resilient = True
    return city
