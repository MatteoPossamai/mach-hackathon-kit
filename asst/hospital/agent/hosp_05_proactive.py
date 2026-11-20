# Part of hospital/agent/agent.py: loaded into its namespace, in order.
from __future__ import annotations

# --- proactive desk: trust, check-in in words, never silent ----------------------------------

# Our own team's agents (asst/*/card.yaml). The Index may hide booking centres from facilities.
_TEAM_AGENTS = frozenset({"agent:gdrf9b9kxxss", "agent:nanpr5mgm2py", "agent:c2ewegr2ea3v",
                          "agent:pky3vqyqbepa", "agent:atp772whjkgd"})


async def _trusted_booking_centre(city: City, sender: str) -> bool:
    """Our booking centre always; another one if the Index lists it, or lists none to us.

    The slot is still checked against our own Ledger world before any hold.
    """

    if sender in _TEAM_AGENTS:
        return True
    if not sender.startswith("agent:"):
        return False
    try:
        peers = await city.find("booking_centre")
    except CapError:
        return True
    centres = [peer for peer in peers if peer.role == "booking_centre"]
    return not centres or any(peer.agent_id == sender for peer in centres)


_RECEIPT_IT = (
    " Prenotazione confermata. Al suo arrivo in ospedale mi scriva che è arrivato, oppure "
    "'checkin' con questo numero: registro io il check-in. Porti tessera sanitaria e ricetta e "
    "arrivi 15 minuti prima. L'ospedale è accessibile in carrozzina."
)
_RECEIPT_EN = (
    " Booking confirmed. When you arrive at the hospital just tell me you are here, or write "
    "'checkin' with this number: I will register the check-in. Bring your health card and "
    "prescription and arrive 15 minutes early. The hospital is wheelchair accessible."
)
_CHECKIN_IT = (
    " Check-in registrato, grazie. Si accomodi in sala d'attesa: verrà chiamato a breve. "
    "Se ha bisogno di assistenza per muoversi o di un accompagnatore, lo dica all'accettazione."
)
_CHECKIN_EN = (
    " Check-in done, thank you. Please take a seat in the waiting room: you will be called "
    "shortly. If you need help moving around or a companion, tell the front desk."
)
_ARRIVAL_WORDS = (
    "sono arrivat", "sono qui", "sono in ospedale", "sono in accettazione", "sono all'ingresso",
    "check-in", "check in", "registrar", "i'm here", "i am here", "i have arrived", "i've arrived",
    "arrived", "at the hospital", "for my appointment",
)


def _case_booking(chat: list[Message], agent: str) -> str:
    """The latest booking id of this case: our Ledger commit, else a receipt sent to the citizen."""

    for row in reversed(chat):
        head = first_word(row.text)
        if row.sender == LEDGER and head == "committed":
            found = fields_of(row.text).get("booking_id", "")
            if found:
                return found
        if head == "booking.receipt" and row.recipient.startswith("citizen:"):
            found = fields_of(row.text).get("booking_id", "")
            if found:
                return found
    return ""


async def _citizen_desk(city: City, message: Message, chat: list[Message], *, reports: bool) -> None:
    """A citizen message no care step handled: check in from words, or answer helpfully."""

    text = (message.text or "").lower()
    booking_id = fields_of(message.text).get("booking_id", "") or _case_booking(chat, city.agent_id)
    english = _en(chat)
    if booking_id and any(word in text for word in _ARRIVAL_WORDS):
        checkin = Message(type="chat", sender=message.sender, recipient=message.recipient,
                          msg_id=message.msg_id, need_id=message.need_id,
                          body={"text": f"checkin booking_id={booking_id}"})
        await _on_checkin(city, checkin, chat, reports=reports)
        return
    if booking_id:
        reply = (f"Your booking {booking_id} is confirmed. When you arrive, just tell me you are here and "
                 "I will register the check-in. Bring your health card and prescription; the hospital is "
                 "wheelchair accessible. Is there anything else I can do for you?" if english else
                 f"La sua prenotazione {booking_id} è confermata. Quando arriva mi scriva che è qui e "
                 "registro io il check-in. Porti tessera sanitaria e ricetta; l'ospedale è accessibile in "
                 "carrozzina. Posso aiutarla in altro?")
    else:
        reply = ("Hello, this is the hospital front desk. To book a visit you need your GP's prescription: "
                 "with its number the booking centre will offer you the available times. Once booked, "
                 "tell me when you arrive and I will check you in. How can I help you?" if english else
                 "Buongiorno, sono l'accettazione dell'ospedale. Per prenotare una visita serve la ricetta "
                 "del medico di base: con il suo numero il centro prenotazioni le propone gli orari "
                 "disponibili. Dopo la prenotazione mi scriva quando arriva e registro io il check-in. "
                 "Come posso aiutarla?")
    if not _exact(chat, city.agent_id, reply):
        await _say(city, message.sender, reply)


_facility_core = _facility


async def _facility(
    city: City,
    message: Message,
    chat: list[Message],
    *,
    reports: bool,
    hold_self: bool,
) -> None:
    kind = words(message.text)
    if message.sender.startswith("citizen:") and "checkin" not in kind and "confirm" not in kind:
        await _citizen_desk(city, message, chat, reports=reports)
        return
    await _facility_core(city, message, chat, reports=reports, hold_self=hold_self)
