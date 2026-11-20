"""Messages for the citizen, in Italian and English."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime, timedelta

from city_sdk.city import LEDGER, CapError, City, Need, clinical, OutOfScope, understood

OFFER_NOTE = "Queste sono le disponibilità compatibili con la sua ricetta e le sue preferenze: mi indichi quale offerta preferisce."
OFFER_NOTE_EN = "These are the slots that fit your prescription and your preferences: tell me which offer you prefer."
OFFER_NOTE_RELAXED_EN = (
    "No slot meets every preference you gave (doctor or day): these are the closest options that "
    "fit your prescription. Tell me which one you prefer."
)
OFFER_NOTE_RELAXED = (
    "Non ci sono posti con tutte le preferenze indicate (medico o giorno): queste sono le "
    "alternative più vicine compatibili con la sua ricetta. Mi indichi quale preferisce."
)
PENDING_NOTE = (
    "La struttura non ha ancora confermato la prenotazione: le inoltro la ricevuta appena arriva. "
    "The facility has not confirmed yet: I will forward the receipt as soon as it arrives."
)


# --- proactive notes: concrete details and next steps, never clinical advice -----------------

DAYS_IT = ("lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica")
DAYS_EN = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS_IT = ("gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto",
             "settembre", "ottobre", "novembre", "dicembre")
MONTHS_EN = ("January", "February", "March", "April", "May", "June", "July", "August",
             "September", "October", "November", "December")
VISIT_IT = {"VIS-FIS": "visita fisiatrica", "VIS-CARD": "visita cardiologica",
            "CTRL-CARD": "controllo cardiologico", "CONS-GIN": "visita ginecologica"}
VISIT_EN = {"VIS-FIS": "physiatry visit", "VIS-CARD": "cardiology visit",
            "CTRL-CARD": "cardiology follow-up", "CONS-GIN": "gynaecology visit"}
LANG_NAMES = {"en": ("inglese", "English"), "ja": ("giapponese", "Japanese"), "fr": ("francese", "French"),
              "es": ("spagnolo", "Spanish"), "de": ("tedesco", "German"), "zh": ("cinese", "Chinese"),
              "ar": ("arabo", "Arabic"), "ru": ("russo", "Russian"), "ro": ("rumeno", "Romanian"),
              "pt": ("portoghese", "Portuguese")}


def when_text(value: str, english: bool) -> str:
    """'lunedì 9 novembre alle 10:30' from an agenda timestamp; empty if unreadable."""

    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if english:
        return f"{DAYS_EN[at.weekday()]} {at.day} {MONTHS_EN[at.month - 1]} at {at:%H:%M}"
    return f"{DAYS_IT[at.weekday()]} {at.day} {MONTHS_IT[at.month - 1]} alle {at:%H:%M}"


def offer_note(prefs: dict[str, str], relaxed: bool, english: bool) -> str:
    """Say which needs were respected and invite accessibility requests."""

    kept = []
    lang = prefs.get("lang", "")
    if lang and lang != "it":
        name = LANG_NAMES.get(lang, (lang, lang))
        kept.append(f"a doctor who speaks {name[1]}" if english else f"medico che parla {name[0]}")
    gender = prefs.get("gender_preference", prefs.get("gender", ""))
    if gender in {"m", "f"} and not relaxed:
        kept.append(("a female doctor" if gender == "f" else "a male doctor") if english
                    else ("una dottoressa" if gender == "f" else "un medico uomo"))
    days = prefs.get("days", "")
    if days and not relaxed:
        kept.append(f"days {days}" if english else f"giorni {days}")
    if english:
        head = OFFER_NOTE_RELAXED_EN if relaxed else OFFER_NOTE_EN
        extra = f" I took into account: {', '.join(kept)}." if kept else ""
        return (head + extra + " All slots respect your prescription's priority. If you need "
                "accessibility support (wheelchair, a companion), tell me and I will let the facility know.")
    head = OFFER_NOTE_RELAXED if relaxed else OFFER_NOTE
    extra = f" Ho tenuto conto di: {', '.join(kept)}." if kept else ""
    return (head + extra + " Tutti i posti rispettano la priorità della ricetta. Se ha esigenze di "
            "accessibilità (carrozzina, accompagnatore) me lo dica e lo segnalo alla struttura.")


def receipt_note(offer: dict[str, str], english: bool) -> str:
    """What the citizen needs on the day: when, what to bring, how to change it."""

    when = when_text(offer.get("agenda_date", offer.get("at", "")), english)
    visit = (VISIT_EN if english else VISIT_IT).get(offer.get("procedure", ""), "")
    if english:
        what = f"Your {visit or 'appointment'} is confirmed" + (f" for {when}" if when else "") + ". "
        return (what + "Please bring your health card and the prescription number, and arrive 15 minutes "
                "early for check-in at the front desk. If you can no longer come, or need help with "
                "access, write to me and I will rearrange it.")
    what = ("Prenotazione confermata" + (f": {visit}" if visit else "") + (f" {when}" if when else "") + ". ")
    return (what + "Porti la tessera sanitaria e il numero di ricetta e arrivi 15 minuti prima per il "
            "check-in in accettazione. Se non può più venire o ha bisogno di assistenza per "
            "l'accesso, mi scriva e la aiuto a spostare l'appuntamento.")


def recap_note(booking_id: str, offer: dict[str, str], english: bool) -> str:
    when = when_text(offer.get("agenda_date", offer.get("at", "")), english)
    if english:
        return (f"Your booking {booking_id} is confirmed" + (f" for {when}" if when else "")
                + ". I am here if you need to change it or have any other request.")
    return (f"La sua prenotazione {booking_id} è confermata" + (f" per {when}" if when else "")
            + ". Resto a disposizione se deve spostarla o per qualsiasi altra richiesta.")
