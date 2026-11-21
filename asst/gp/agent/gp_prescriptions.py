"""Verify a prescription using the Ledger's write acknowledgment or readback."""

import asyncio

from city_sdk.city import LEDGER, CapError, City, Message


def fields_of(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for token in text.replace(",", " ").split():
        key, eq, value = token.partition("=")
        value = value.strip(".,;:!?")
        if eq and key and value:
            found.setdefault(key, value)
    return found


def confirmation(message: Message, request: dict[str, str]) -> bool:
    if message.sender != LEDGER or not request.get("patient"):
        return False
    parts = message.text.split(maxsplit=1)
    if len(parts) != 2:
        return False
    head, rest = parts
    data = fields_of(rest)
    number = data.get("prescription_number")
    if not number or number != request.get("prescription_number"):
        return False
    owner = data.get("patient") or data.get("citizen")
    if head == "prescription":
        return data.get("state") == "issued" and owner == request["patient"] and all(
            data.get(key) and data[key] == request.get(key) for key in ("procedure", "priority")
        )
    return head == "prescribed" and (not owner or owner == request["patient"]) and all(
        not data.get(key) or data[key] == request.get(key) for key in ("procedure", "priority")
    )


def latest_confirmation(history: list[Message], request: dict[str, str]) -> Message | None:
    return next((row for row in reversed(history) if confirmation(row, request)), None)


async def wait_for_result(
    city: City, need_id: str | None, history: list[Message], request: dict[str, str],
    pauses: tuple[float, ...], *, seen: set[str],
) -> tuple[Message | None, list[Message]]:
    """Poll briefly for a push, then at increasing intervals after a lookup."""
    for pause in pauses:
        if pause:
            await asyncio.sleep(pause)
        try:
            history = await city.history(need_id=need_id)
        except CapError:
            continue
        confirmed = latest_confirmation(history, request)
        if confirmed:
            return confirmed, history
        for row in reversed(history):
            if row.sender != LEDGER or row.msg_id in seen:
                continue
            parts = row.text.split()
            if parts and parts[0] == "refused" and "prescribe" in parts[1:]:
                number = fields_of(row.text).get("prescription_number")
                if not number or number == request.get("prescription_number"):
                    return row, history
    return None, history
