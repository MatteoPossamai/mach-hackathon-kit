# Part of gp/agent/agent.py: loaded into its namespace, in order.
from __future__ import annotations

# --- proactive notes: next steps for the citizen, never a diagnosis -------------------------

_OTHER_IT = (
    "Non riesco a ricondurre il disturbo a una delle visite che posso prescrivere da qui "
    "(fisiatria per schiena e ginocchio, cardiologia per palpitazioni e controlli del cuore). "
    "Per un tema ginecologico può rivolgersi direttamente al consultorio, senza ricetta. "
    "Se vuole, mi descriva di nuovo il disturbo con parole sue. In caso di urgenza chiami il 112."
)
_OTHER_EN = (
    "I cannot match your problem to one of the visits I can prescribe from here (physiatry for "
    "back and knee, cardiology for palpitations and heart checks). For a gynaecological matter "
    "you can go straight to the family counselling centre, no prescription needed. If you like, "
    "describe the problem again in your own words. In an emergency call 112."
)

_rx_note_core = _rx_note


def _rx_note(
    procedure: str, priority: str, *, english: bool, text: str = "", reminder: bool = False
) -> str:
    """The base note plus what happens next, so the citizen knows the way forward."""

    note = _rx_note_core(procedure, priority, english=english, text=text, reminder=reminder)
    if english:
        return (note + " The booking centre will offer you times within this deadline and can "
                "respect your needs (language, doctor, days): just tell them. If things get worse "
                "before the visit, write to me again.")
    return (note + " Il centro prenotazioni le proporrà gli orari entro questa scadenza e può "
            "rispettare le sue esigenze (lingua, medico, giorni): basta dirlo. Se prima della "
            "visita la situazione peggiora, mi riscriva.")
