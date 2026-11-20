"""First booking-centre step: look up a prescription before searching for slots."""

from __future__ import annotations

from city_sdk.city import LEDGER, City, Need, understood


async def main(need: Need, city: City) -> None:
    incoming = city.inbound
    if incoming is None:
        return
    message = understood(incoming)
    number = message.body.get("prescription_number")

    # Turn 1: a citizen supplies a prescription number. Ask the Ledger about it.
    if message.sender.startswith("citizen:"):
        if (
            not isinstance(number, str)
            or not number
            or len(number) > 128
            or any(character.isspace() for character in number)
        ):
            await city.say(
                message.sender,
                "data.request purpose=booking fields=prescription_number "
                "Please send your prescription number so I can check it.",
            )
            return
        await city.say(LEDGER, f"prescription prescription_number={number}")
        return

    # Turn 2: the Ledger replies. Other agents cannot verify prescriptions for us.
    if message.sender != LEDGER or message.type != "prescription":
        return
    if not isinstance(number, str) or not number or not message.need_id:
        return

    # Find the citizen who supplied this number in this case, without global state.
    case = await city.history(need_id=message.need_id)
    citizen = next(
        (
            row.sender
            for row in reversed(case)
            if row.sender.startswith("citizen:")
            and understood(row).body.get("prescription_number") == number
        ),
        None,
    )
    if citizen is None:
        return

    state = message.body.get("state")
    procedure = message.body.get("procedure")
    priority = message.body.get("priority")
    if state == "issued" and procedure and priority:
        await city.say(
            citizen,
            f"Prescription found: prescription_number={number} "
            f"procedure={procedure} priority={priority} state=issued. "
            "No appointment has been booked yet.",
        )
    elif "not found" in message.text.lower():
        await city.say(citizen, "Prescription not found. Please check the number and send it again.")
    else:
        await city.say(citizen, "I could not verify that the prescription is ready for booking.")
