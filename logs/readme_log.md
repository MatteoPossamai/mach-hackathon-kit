# Real-Time Journey Logger (`journey_watch.py`)

Monitor da terminale (solo **stdlib Python 3.10+**, nessuna dipendenza esterna) per vedere **in diretta**:
- **Tutti i messaggi completi** scambiati tra cittadino (`CIT`) e agenti (`GP`, `BC`, `HOSP`, `SPEC`, `CONS`, `PARTNER`), con orario locale e delta dal messaggio precedente (`+1m20s`).
- **Tempi per fase** (`GP`, `BC`, `TOT`) e tempo medio di chiusura (`avg_close`).
- **Coda live (`QUEUE`)**: quante richieste sono aperte, su quale agente sono in attesa (`GP:N BC:N CIT:N`) e l'età della più vecchia (`oldest`).
- **Alert automatici** sui bug ricorrenti (`DUP_OFFER`, `CANCEL_deadline`, `CANCEL_past`, `GP_FALSE_REFUSED`, `MISSED(S_eff=...)`).

---

## Requisiti

- Python 3.10+ (oppure `uv`, già usato dal team per `city-cli`).
- Aver fatto `city login` almeno una volta (lo script legge automaticamente il token in `~/.config/city/teams/ltf-jecatt/session`), **oppure** passare `--token` / `--cookie`.

---

## Come trovare `<ENDPOINT_JOURNEYS>` dalla console squadra

1. Apri [https://projectnandaitaly.ai/team](https://projectnandaitaly.ai/team) nel browser e vai sulla tab **Journeys**.
2. Apri gli **Strumenti per sviluppatori** (`F12` o `Cmd+Opt+I`) → tab **Network** → filtra per **Fetch/XHR**.
3. Ricarica la pagina (o clicca su un journey) e copia l'URL della richiesta JSON dei journey.
   - Se la richiesta lista restituisce solo l'elenco senza `messages`, copia anche l'URL che compare cliccando un singolo journey e sostituisci l'ID con `{need_id}` per usarlo in `--detail-url`.
   - Se l'API usa il cookie del browser invece del Bearer token di `city login`, copia l'header `Cookie` della stessa richiesta e passalo con `--cookie "..."`.

---

## Come lanciarlo su ogni computer

Posizionati nella root della repository (`mach-hackathon-kit/`):

### macOS / Linux

```bash
# Live in terminale (usa già gli URL di default della console, aggiornamento ogni 5s)
python3 logs/journey_watch.py --cookie-file ~/.config/city/console_auth --interval 5

# Oppure passando il cookie direttamente e salvando su file di log
python3 logs/journey_watch.py --cookie "__Host-city_session=<VALORE>" --interval 5 --log-file logs/live.log
```

### Windows (PowerShell)

```powershell
# Con uv (consigliato su Windows se python/py non è nel PATH)
uv run --no-project python logs/journey_watch.py --cookie "__Host-city_session=<VALORE>" --interval 5 --log-file logs/live.log

# Oppure con file cookie
uv run --no-project python logs/journey_watch.py --cookie-file "$HOME\.config\city\console_auth" --interval 5
```

---

## Opzioni utili

| Flag | Descrizione |
|---|---|
| `--url <URL>` | Endpoint JSON lista journey (default: `https://projectnandaitaly.ai/v1/team/journeys`). |
| `--detail-url <TEMPLATE>` | Template dettaglio (default: `https://projectnandaitaly.ai/v1/team/journeys/{need_id}`). |
| `--limit <N>` | Numero di journey più recenti di cui scaricare i messaggi in parallelo (default: `15`, `0` = tutti). |
| `--interval <SEC>` | Intervallo di polling in secondi (default: `5`). |
| `--log-file <PATH>` | Salva tutto l'output anche su file (pulito, senza codici ANSI). |
| `--team <NOME>` | Nome squadra per leggere `~/.config/city/teams/<NOME>/session` (default: `ltf-jecatt`). |
| `--token <TOKEN>` | Bearer token esplicito (sovrascrive il file `session`). |
| `--cookie <COOKIE>` | Header `Cookie` del browser (`__Host-city_session=...`). |
| `--cookie-file <PATH>` | Legge il cookie da file (es. `~/.config/city/console_auth` con `chmod 600`). |
| `--need <TEST>` | Mostra solo i journey il cui `need_id` contiene `<TEST>` (es. `--need 0003450004`). |
| `--max-chars <N>` | Tronca i messaggi oltre `N` caratteri (default: `0` = testo intero). |
| `--summary-only` | Stampa solo stato, tempi e coda senza i testi dei messaggi. |
| `--once` | Stampa lo stato attuale ed esce. |
| `--file <PATH>` | Legge e analizza subito un file JSON esportato (anche più JSON concatenati). |

---

## Esempio: analisi immediata di JSON esportati

Se incolli uno o più JSON di journey in un file (es. `failed.json`):

```bash
# macOS / Linux
python3 logs/journey_watch.py --file failed.json

# Windows
uv run --no-project python logs/journey_watch.py --file failed.json
```

### Esempio di output

```text
[10:53:47] FAILED 0003450004 L7 | VIS-FIS  P=D it | GP= 2m56s BC= 6m08s TOT=18m15s | score=2 (relaxed_alternative) DUP_OFFER(x2) CANCEL_past(x1) GP_FALSE_REFUSED
  [10:15:14     +0s] 0003450004 CIT->GP     | fastidio al ginocchio
  [10:18:10  +2m56s] 0003450004 GP->CIT     | prescription prescription_number=prescription_number-N-2026-11-02-8909100003450004 procedure=VIS-FIS priority=D
  [10:20:11  +2m01s] 0003450004 CIT->BC     | need.request lang=it service_type=hospital.physiatry
  [10:21:09    +59s] 0003450004 GP->CIT     | Il registro non ha confermato la ricetta. Ci contatti per assistenza.
  [10:26:18  +5m09s] 0003450004 BC->CIT     | offer offer_id=of-1 agenda_date=2026-12-21T10:30:00.000Z
  [10:28:15  +1m47s] 0003450004 CIT->BC     | cancel offer_id=of-1 reason=past
[10:53:47] === QUEUE: 0 aperte (vuota) | oldest=- | CHIUSE: 1 (done=0 fail=1) | avg_close=18m15s ===
```

> **Nota**: non committare file `logs/*.log` generati durante la gara se contengono dati locali; sono già utili solo in locale.
