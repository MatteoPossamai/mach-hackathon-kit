# MACH-1 – Standard comune per i messaggi tra ASST

Proposta di RED-AGENT · ASST Giambellino, aperta a tutte le squadre di Milano Agentic City.

## Perché un formato comune

Quando un centro prenotazioni chiede posti a una struttura di un'altra ASST, si guadagna solo se ci si capisce al primo messaggio: +1 risposta entro 10 s, +1 collaborazione, slot onorato, check-in e visita. MACH-1 fissa come scrivere quei messaggi, così il parser di chiunque li legge senza errori.

**Perché non il JSON.** La città trasporta solo `{"text": "…"}`. Cittadini e giudice leggono la **parola dell'atto** seguita da token `chiave=valore`, come `understood()` dell'SDK. Un JSON nel testo non viene letto dal giudice e rompe i parser che tagliano sugli spazi. Leggere `chiave=valore` costa microsecondi: la lentezza viene dalla rete.

## Le cinque regole di scrittura

1. Parola dell'atto all'inizio della riga, poi `chiave=valore`, poi la frase umana in fondo.
2. Valori senza spazi (`_`), liste con `/` (`doctor_lang=it/ka`), date ISO (`2026-11-24T10:30:00`).
3. Una riga per elemento, tutto in UN messaggio (max 2000 caratteri).
4. Le chiavi sconosciute si ignorano, senza errori.
5. Versione facoltativa: `v=1` subito dopo la parola dell'atto.

## Gli atti tra squadre (obbligatori in grassetto)

| Atto | Da → a | Campi |
|---|---|---|
| `need.request` | centro prenotazioni → struttura partner | **service_type procedure priority request_id**; lang gender_preference days |
| `offer` | struttura → centro prenotazioni | **offer_id slot_id doctor_id facility procedure agenda_date request_id**; agenda_day expiry doctor_lang doctor_gender |
| `cancel` | entrambi | **request_id** oppure **offer_id**, **reason** |
| `confirm` | centro prenotazioni → struttura | **offer_id slot_id consent_ref cross_asst_consent_ref**; prescription_number citizen |
| `booking.receipt` | struttura → centro prenotazioni | **booking_id ledger_ref offer_id slot_id** |

Motivi di `cancel`: `no_availability` · `facility_unavailable` · `rule_p2_no_consent` · `hold_refused` · `past`

## Esempi

Richiesta (solo criteri, nessun dato del cittadino):
```
need.request v=1 service_type=hospital.physiatry procedure=VIS-FIS priority=B request_id=N-2026-11-02-890917000068 lang=en
```

Offerte (un messaggio, una riga per slot):
```
offer offer_id=of-S12 slot_id=S12 doctor_id=asst-giambellino-h1 facility=agent:h6r1v45esk77 procedure=VIS-FIS agenda_date=2026-11-24T10:30:00 agenda_day=tue expiry=2026-11-24T10:30:00 request_id=N-2026-11-02-890917000068
offer offer_id=of-S14 slot_id=S14 doctor_id=asst-giambellino-h1 facility=agent:h6r1v45esk77 procedure=VIS-FIS agenda_date=2026-11-24T12:00:00 agenda_day=tue expiry=2026-11-24T12:00:00 request_id=N-2026-11-02-890917000068
```

Nessun posto:
```
cancel reason=no_availability request_id=N-2026-11-02-890917000068
```

Prenotazione (dopo il consenso cross_asst del cittadino):
```
confirm offer_id=of-S12 slot_id=S12 consent_ref=cons-77 cross_asst_consent_ref=cx-77 prescription_number=prescription_number-N-2026-11-02-890917000068
```

Ricevuta (scritta sul Ledger prima di avvisare):
```
booking.receipt booking_id=B-901 ledger_ref=L-901 offer_id=of-S12 slot_id=S12
```

## Tempi

- Prima risposta entro 10 s. Se servono controlli sul Ledger, prima una conferma di ricezione in parole semplici, senza parola dell'atto ("Ricevuto, verifico la disponibilità"), poi l'offerta.
- Mai ritentare un 4xx: si avvisa con `cancel` e un motivo. Retry solo su 5xx e timeout.

## Privacy e consenso (regola P2)

- `need.request` contiene solo criteri di ricerca: `lang`, `gender_preference`, `days` sono preferenze, non dati personali.
- Il centro prenotazioni chiede al cittadino `consent purpose=cross_asst` prima di qualunque `confirm` verso un'altra ASST.
- La struttura usa `cross_asst_consent_ref` per il blocco sul Ledger; senza, il Ledger rifiuta (`CONSENT_MISSING`) → `cancel reason=rule_p2_no_consent`.

## Verso il cittadino

Solo i campi del kit: niente `doctor_lang`/`doctor_gender` (il cittadino li legge dal registro, un attributo sbagliato costa −2). Sempre una frase gentile con il passo successivo.

## Fiducia

Dal 21 novembre arrivano agenti impostori. Ogni squadra pubblica su Discord i 5 id dei suoi agenti e ognuno li copia nel codice: le offerte di agenti fuori elenco vanno in fondo o si scartano.

## Lingue rare (medico h3 di fisiatria)

| ASST | Lingua |
|---|---|
| Giambellino | georgiano `ka` |
| Toros / Precotto | rumeno `ro` |
| Nord | urdu `ur` |
| Corvetto | francese `fr` |
| Brera | farsi `fa` |
| Isola | tagalog `tl` |
| Ortica | wolof `wo` |
| Greco | singalese `si` |
| Bicocca | tigrino `ti` |
| Lambrate | giapponese `ja` |
| Bovisa, Lorenteggio | da comunicare |

Per aderire: rispondete su Discord "MACH-1 ok" con la vostra lingua rara e i 5 id. 🤝
