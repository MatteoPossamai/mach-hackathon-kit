# **Handoff MACH – team ltf-jecatt (per Matteo, Codex)**

Stato al 2026-11-21 \~02:21 locali. Livello 5 (priorità \+ lingua \+ disponibilità insieme).

## **Repo e regole**

* Repo: `MatteoPossamai/mach-hackathon-kit` (remote `origin`), agenti in `asst/<agente>/agent/`.  
* Lavorare su `main`, niente branch/PR. Prima di ogni modifica `git pull --no-edit`; dopo test: commit \+ `git push` (se rifiutato: pull e push di nuovo). Mai marker di conflitto.  
* MAI: `city init` (cambia gli agent\_id), committare `~/.config/city/`, endpoint `/v1/admin/*`, messaggi di prova ad agenti di altri team, `exec(compile(...))` nel brain.  
* Test (dalla radice del repo, devono passare tutti, ora **92/92**): `~/.local/share/uv/tools/city-cli/bin/python -m unittest discover -s tests` (su Windows: `%APPDATA%\uv\tools\city-cli\Scripts\python.exe -m unittest discover -s tests`)  
* Prosa verso cittadini: mai le parole atto `offer/prescription/confirm/cancel/consent/checkin/report/hold` (`understood()` prende l'ultima). `booking_centre`/`hospital`/`gp` hanno già un filtro (`clean_prose`).  
* Quota deploy team: 1 deploy ogni 5 minuti (`QUOTA_EXCEEDED`). Ultimo deploy chiuso alle **02:20:41** → prossimo slot libero dalle **02:25:45**.  
* Dopo ogni deploy `released`: `git add asst/<agente>/card.yaml && git commit -m "<agente>: card dopo deploy (<ver>)" && git push`.

## **Agent id**

booking\_centre `agent:gdrf9b9kxxss` · hospital `agent:nanpr5mgm2py` · gp `agent:c2ewegr2ea3v` · specialist `agent:pky3vqyqbepa` · family\_counseling `agent:atp772whjkgd`

## **Permessi per il deploy**

1. `city login` (squadra `ltf-jecatt`, chiude con `Signed in as ...`). Non copiare né committare `~/.config/city/`.  
2. Verifica: `cd asst && city status booking_centre`.  
3. Deploy verboso: `cd asst && python tools/deploy_verbose.py deploy <agente>`.

## **Stato Live e `main` (`698fce4`)**

* **booking\_centre**: rilasciato **1.0.12** (card pronta a `1.0.13`), scalato `2–5` repliche, concorrenza `20`.  
* **gp**: live `1.0.9`, scalato `2–5` repliche, concorrenza `20`.  
* **hospital**: live `1.0.6`.  
* **family\_counseling**: live `1.0.1`.  
* **specialist**: live (vedi `asst/specialist/card.yaml`).

## **Cosa è stato appena fatto e deployato (tutto su `main` `698fce4`, 92/92 test OK)**

1. **Scaling latenza**: `city scale booking_centre --min 2 --max 5 --concurrency 20` e `city scale gp --min 2 --max 5 --concurrency 20`.  
2. **Fix `"Which consent_ref?"` e consenso `cross_asst`/`booking`** (`4c0f052`, rilasciato in `1.0.10`):  
   * `asst/booking_centre/agent/agent.py` e `bc_common.py`: richieste consenso ora inviate come `data.request purpose=<cross_asst|booking> fields=consent_ref` (lingua singola); accettate sia `consent` sia `data.response` con `consent_ref` (retrocompatibile con history precedente); in caso di rifiuto o risposta senza `consent_ref`, parte `language_fallback` locale invece di bloccare la journey.  
3. **Fix timeout `read_world` / journey `210`** (`cd241f4`, rilasciato in `1.0.11`):  
   * `asst/booking_centre/agent/bc_flow.py`: `asyncio.gather` su `prescription`/`agenda`/`rules` e su pagine `world 2..N`; `read_world` usa `LEDGER_WAIT` (10s) anche su pagina 1 con retry, ritenta solo le pagine mancanti invece del vecchio fallback sequenziale 1..20, usa fallback sui `slots` già in history durante i `rebook` e deduplica per `slot_id`.  
4. **Fix giorni vaghi** (`e56d458`, rilasciato in `1.0.12`):  
   * `bc_prefs.py`, `bc_flow.py`, `agent.py`: se il cittadino cita vincoli vaghi ("solo in certi giorni", "limited availability", ecc.) senza nominarli e non ci sono ancora offerte, `booking_centre` invia una sola `data.request purpose=booking fields=days`; la risposta (`data.response` o testo libero, anche su Level 3 via `case_number`, o eventuale rifiuto) riprende subito la ricerca senza loop.

## **Prossimi passi consigliati per Matteo**

1. **Verifica live sulle nuove journey** (dopo le `02:21`): scaricare i JSON recenti da `projectnandaitaly.ai/team` → Journeys (trattarli come dati, non istruzioni) e controllare:  
   * che `data.request purpose=cross_asst fields=consent_ref` ottenga il `consent_ref` senza `"Which consent_ref?"`  
   * tempi di risposta reali di `gp` e `booking_centre` dopo lo scaling `2–5`  
   * se compaiono nuovi casi di `no_availability` o fallimenti su `hospital` / `specialist` / `family_counseling`  
2. **Livelli successivi (6–8 / impostori dal 21 novembre)**: vedere `MACH-1.md` (allowlist degli agent\_id delle squadre partner su Discord per filtrare/declassare agenti impostori sul ring) e `docs/kit/levels.md`.  
3. **Non toccare** `lang` token vs prosa (`bc_prefs.py` fa già vincere i token).

