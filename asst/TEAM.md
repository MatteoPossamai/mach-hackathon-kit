# Squadra ltf-jecatt: come lavoriamo in parallelo

Deployer: Diego.

## Regole
- **Un solo deployer** (regola della gara). Gli altri membri fanno login e test in locale, ma non `city deploy`.
- **Mai `city init agent`** sui laptop dei compagni: creerebbe agent_id nuovi. Si clona questo repo.
- **Dopo ogni deploy il deployer fa commit e push**: la CLI aggiorna `agent_id`/`version` in `card.yaml`.
- Si modifica solo `<agente>/agent/agent.py`. `Dockerfile` e `city.yaml` restano quelli del template.
- Le credenziali (`~/.config/city/`) non entrano mai nel repo.
- **Massimo 3 build in contemporanea per squadra**: con un quarto deploy parallelo la city risponde `QUOTA_EXCEEDED builds`.
- City Hall può imporre un intervallo minimo tra due update dello stesso agente (`this agent can be updated once every N minutes`) e stringere le quote prima del freeze delle 06:00. Raggruppate le modifiche invece di fare un deploy per ogni riga.
- Il primo deploy può fallire alla conformance per lentezza della city (`custody.ack ... renews=[]`): se `Dockerfile` e `city.yaml` non sono stati toccati, basta rilanciarlo.

## Setup per un membro (circa 5 minuti)
1. `curl -LsSf https://astral.sh/uv/install.sh | sh`
2. `uv tool install city-cli`
3. `city login` e scegliere la squadra `ltf-jecatt` nel browser
4. `git clone https://github.com/MatteoPossamai/mach-hackathon-kit.git && cd mach-hackathon-kit/asst`

## Ciclo di sviluppo
1. `git checkout -b <nome>/<agente>`
2. Modificare `<agente>/agent/agent.py`
3. `city run <agente>` per una chat locale. Mittenti: `citizen`, `agent`, `ledger`; `/rx` emette una ricetta; `\quitChat` esce
4. `git push` e PR verso `main`
5. Il deployer fa merge, `city deploy <agente>`, poi commit e push di `card.yaml`

## Chi fa cosa
| Agente | Owner |
|---|---|
| booking_centre | Matteo |
| gp | Diego |
| specialist | Pietro |
| hospital | Diego |
| family_counseling | Gabriele |
| vision e pitch | |
