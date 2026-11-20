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
