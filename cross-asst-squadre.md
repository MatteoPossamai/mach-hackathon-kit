# Squadre MACH 2026 e collaborazione cross-ASST (livelli 4-8)

Fonte: messaggi Discord dei canali `#mach-2026` e `#off-topic` dal 20/11/2026 ore 22:59 al 21/11 ore 00:52 (ora italiana; dopo, fino alla lettura del 21/11, nessun messaggio nuovo). Li hanno scritti altre squadre: sono dati **non verificati**, non regole del kit. A questi si aggiunge lo standard scritto di RED-AGENT (Giambellino): **MACH-1**, condiviso il 21/11, le revisioni **MACH-1.1** e **MACH-1.2 «Ring reloaded»** (per i livelli 5-8), tutte compatibili all'indietro (sezione «Lo standard MACH-1.2» qui sotto). Anche MACH-1.2 è un dato di un'altra squadra, non una regola del kit. Un id scritto in chat dice chi lo ha dichiarato, non che l'agente sia davvero di quella squadra.

Noi: **RASPBERRY · ASST Lorenteggio (Municipio 9)**, messaggi di TheDarkKnight197.

«manca» vuol dire che la squadra non l'ha scritto, oppure ha lasciato un segnaposto.

## Formato comune concordato

Proposto da PiGreco (RED-AGENT, Giambellino) alle 22:59, accettato da tutte le squadre che hanno risposto.

1. **Richiesta** (centro prenotazioni → struttura partner). Contiene solo i criteri, nessun dato del cittadino prima del consenso:
   `need.request service_type=hospital.physiatry procedure=VIS-FIS priority=B request_id=<id>`.
   Molte squadre aggiungono `lang=`, `gender=` (o `gender_preference=`) e `days=` quando il cittadino li chiede.
2. **Risposta**, in un solo messaggio e in pochi secondi:
   - una riga per slot: `offer offer_id=… slot_id=… doctor_id=… facility=… procedure=… agenda_date=… doctor_lang=it/ka doctor_gender=m request_id=<id>`;
   - oppure `cancel reason=<motivo> request_id=<id>`, con uno dei motivi condivisi: `no_availability`, `facility_unavailable`, `rule_p2_no_consent` (MACH-1.1 e MACH-1.2 ne aggiungono altri, vedi sotto).
3. **Consenso**: `consent purpose=cross_asst` del cittadino prima di qualunque hold o confirm fuori dalla propria ASST.
4. **Prenotazione**: `confirm offer_id=… slot_id=… citizen=… consent_ref=… prescription_number=…`, più `cross_asst_consent_ref=` se è cross-ASST.
5. **Ricevuta**: la struttura risponde `booking.receipt booking_id=… ledger_ref=…`.

### Le tre regole di Toros

Proposte da Gabriele_biancardi («ASST Toros, distretto 9») alle 23:08:

1. **Risposta entro 10 secondi** a ogni `need.request`, in un solo messaggio: `offer` oppure `cancel reason=…`.
2. **Motivi di annullamento uguali per tutti**: `no_availability`, `facility_unavailable`, `rule_p2_no_consent`.
3. **Consenso prima dei dati**: `consent purpose=cross_asst` sempre prima di un hold fuori dalla propria ASST. Prima del consenso non si manda nessun dato del cittadino.

### Le due proposte di Corvetto

Proposte da M (ASST Corvetto) alle 23:33:

1. **Elenco degli id contro gli impostori.** Ogni squadra pubblica gli id dei suoi 5 agenti (quelli che si vedono cliccando il pallino sulla mappa). Da sabato arrivano gli impostori, che hanno anche loro un pallino: la mappa dice che un agente esiste, non di chi è. Chi non è nell'elenco è sospetto. Ogni squadra copia l'elenco nel proprio codice.
2. **Verso il cittadino solo `doctor_id`.** `doctor_lang` e `doctor_gender` si usano solo tra agenti, per scegliere. Al cittadino si passa solo il `doctor_id`, perché lingua e genere li legge dal registro, e un attributo sbagliato in un'offerta costa −2.

## Lo standard MACH-1.2

Scritto da RED-AGENT (Giambellino) come «Ring reloaded», pensato per **tutti e 8 i livelli**. È **compatibile all'indietro**: chi parla MACH-1 o MACH-1.1 continua a funzionare, e tutto quello che è nuovo è facoltativo. Mette per iscritto il formato comune qui sopra. Per ogni regola, sotto c'è cosa cambia rispetto a MACH-1.1 (e, dove serve, rispetto a MACH-1).

### La roadmap della notte

Orari scritti da RED-AGENT, non dal kit: il kit dice che le descrizioni dei livelli compaiono solo dal vivo nella colonna «Il Token della Sera» della classifica (`docs/kb/02-rules-and-scoring.md`). Anche `fault.cancel` non compare nella documentazione del kit che abbiamo.

| Ora (21/11) | Livello | Cosa arriva | Cosa porta MACH-1.2 |
|---|---|---|---|
| ora | 4 · Scarsità sul ring | la risposta è solo nell'ASST partner | formato comune, consenso, fiducia (da 1.1) |
| 01:15 | 5 · Vincoli combinati | lingua + giorni + genere + carrozzina, a volte nessuna risposta possibile | criteri estesi e offerte rilassate |
| 02:15 | 6 · Tempo e riprenotazioni | `fault.cancel` sulla prima prenotazione di ogni cittadino | REBOOK COMBO |
| 03:15 | 7 · Guasti e contesa | partner giù, duplicati, 4xx, slot condivisi | IDEMPOTENZA e SOFT LOCK |
| 04:15 | 8 · Red flag e cambio regola | P1-P3 a metà ondata, cittadini ostili | RULE SYNC |
| 05:30 | | ultimo deploy consigliato | deploy MACH-1.2 entro quest'ora |
| 06:00 | freeze | gioca da solo quello che è online | |
| sabato 10:00 | 8 + impostori | agenti falsi con il pallino sulla mappa | TRUST RING |

Il livello è un tetto: dopo il 5 arrivano ancora cittadini dei livelli 1-4.

### Lettura e scrittura

Uguali a MACH-1.1:
- chi riceve **divide il testo per righe** e legge ogni riga da sola (prima parola = atto, poi i token), perché `understood()` del giudice sull'intero testo vede solo l'ultimo atto e l'ultimo valore. Una riga senza `=` è una frase per umani. Le chiavi sconosciute si ignorano e non passano da una riga all'altra;
- una riga per atto, tutto in un messaggio, max 2000 caratteri (max 6 righe di offerta), valori senza spazi, liste con `/`, date ISO senza fuso, niente punteggiatura attaccata ai valori;
- frase umana **in fondo**, in italiano, senza parole d'atto inglesi (`offer`, `confirm`, `cancel`, `hold`, `consent`, `report`, `checkin`, `prescription`);
- `offer` e `cancel` ripetono il `request_id`.

Nuovo: `v=1.2` facoltativo dopo la parola d'atto.

### Campi di ogni atto

| Atto | Da → a | Obbligatori | Facoltativi | Rispetto a MACH-1.1 |
|---|---|---|---|---|
| `need.request` | centro prenotazioni → struttura partner | `service_type` `procedure` `priority` `request_id` | `lang` `gender_preference` `days` `after` `before` `wheelchair` `deadline` `rebook_of` | aggiunti `after` `before` `wheelchair` `deadline` `rebook_of` |
| `offer` | struttura → centro prenotazioni | `offer_id` `slot_id` `doctor_id` `facility` `procedure` `agenda_date` `request_id` | `agenda_day` `expiry` `doctor_lang` `doctor_gender` `wheelchair` `reserve` `relaxed` | aggiunti `wheelchair` `reserve` `relaxed` |
| `confirm` | centro prenotazioni → struttura | `offer_id` `slot_id` `consent_ref` `prescription_number` `citizen` | `request_id` `facility` `cross_asst_consent_ref` | `cross_asst_consent_ref` ora è nella tabella |
| `booking.receipt` | struttura → centro prenotazioni | `booking_id` `ledger_ref` | `offer_id` `slot_id` `request_id` `facility` `doctor_id` `agenda_date` | uguale |
| `cancel` | entrambi | `request_id` oppure `offer_id` oppure `booking_id`, e `reason` | `ledger_code` | aggiunto `booking_id` |

### Criteri estesi (livello 5)

La `need.request` porta tutti i vincoli che il cittadino ha detto, solo come criteri di ricerca. Nessuno è un dato personale (niente nome, ricetta o id del cittadino).

| Chiave | Esempio | Significato |
|---|---|---|
| `lang` | `lang=ro` | lingua del medico |
| `gender_preference` | `gender_preference=f` | genere del medico; `gender` accettato come sinonimo |
| `days` | `days=tue/thu` | giorni accettabili |
| `after` / `before` | `after=2026-11-24T09:00:00` | finestra di agenda |
| `wheelchair` | `wheelchair=true` | struttura accessibile |
| `deadline` | `deadline=2026-11-25T12:00:00` | scadenza della priorità: oltre non serve |

### Offerte rilassate (livello 5)

Quando nessuno slot rispetta tutti i vincoli, la struttura manda il `cancel reason=no_availability` **e sotto**, nello stesso messaggio, fino a 3 offerte che rompono **un solo** vincolo, ognuna con `relaxed=<vincolo>` (es. `relaxed=days`). Decide il centro prenotazioni. Al cittadino `relaxed` non arriva mai: gli si dice in italiano cosa cambia e gli si chiede se va bene.

```
cancel reason=no_availability request_id=ring-N-77
offer offer_id=of-S31 slot_id=S31 doctor_id=asst-precotto-h1 facility=agent:s1twcdzg3296 procedure=VIS-FIS agenda_date=2026-11-26T11:00:00 request_id=ring-N-77 relaxed=days
Con tutti i vincoli non c'è posto, questo è il più vicino: giovedì invece di martedì
```

### REBOOK COMBO (livello 6)

Dal livello 6 ogni cittadino riceve un `fault.cancel` sulla prima prenotazione.
1. La struttura che annulla manda **subito** `cancel reason=facility_cancelled` con `booking_id` e, sotto, le offerte sostitutive, in un solo messaggio.
2. Se le alternative non bastano, il centro prenotazioni rilancia la richiesta con `rebook_of=<booking_id>` e `deadline=`.
3. Una richiesta con `rebook_of` passa davanti alle altre: risposta **entro 5 s** (per le altre richieste resta 10 s).

```
cancel reason=facility_cancelled booking_id=B-901 offer_id=of-S12 request_id=ring-N-77
offer offer_id=of-S14 slot_id=S14 doctor_id=asst-giambellino-h1 facility=agent:h6r1v45esk77 procedure=VIS-FIS agenda_date=2026-11-24T12:00:00 request_id=ring-N-77
Il posto delle 10:30 è saltato, alle 12:00 dello stesso giorno c'è il medico di prima
```

### IDEMPOTENZA (livello 7)

Il livello 7 manda duplicati di proposito. Regola: **stessa chiave, stessa risposta**.

| Arriva due volte | Risposta |
|---|---|
| `need.request` con lo stesso `request_id` | le stesse righe `offer` di prima, nessuno slot nuovo |
| `confirm` con lo stesso `offer_id` | la stessa `booking.receipt`, mai un secondo hold |
| `cancel` già applicato | niente, oppure la stessa conferma di annullamento |

Mai due prenotazioni per lo stesso cittadino. Mai una ricevuta senza `commit` riuscito: per MACH-1.2 una ricevuta fantasma vale −2 e può colpire anche il centro che la gira. MACH-1.1 aveva solo la riga del `confirm`.

### SOFT LOCK (livello 7)

Per **60 secondi reali** dopo averlo offerto, la struttura non mette lo stesso slot in un'offerta per un altro `request_id`; dopo torna libero. Il Ledger resta l'unico arbitro: vince il primo `hold`, gli altri ricevono `cancel reason=slot_taken` con le alternative sotto.

Cambia rispetto a MACH-1.1, che diceva «fino al `confirm` lo slot resta libero».

### RULE SYNC (livello 8)

City Hall cambia una regola a un'ora di agenda che nessuno conosce. I rifiuti del Ledger si traducono in un motivo di `cancel` che tutti capiscono:

| Regola | Il Ledger dice | Il `cancel` dice | Chi fa cosa |
|---|---|---|---|
| P1 ricetta valida 2 giorni | `PRESCRIPTION_EXPIRED` | `reason=prescription_expired` | il centro manda il cittadino dal medico di base per il rinnovo, poi riprova |
| P2 consenso cross-ASST | `CONSENT_MISSING` | `reason=consent_missing` | il centro chiede `consent purpose=cross_asst`, poi rimanda il `confirm` |
| P3 riserva urgente | `NO_CAPACITY` | `reason=no_capacity` | per B, D e P si sceglie uno slot non riservato |

Con P3 attiva la struttura marca nelle offerte gli slot della riserva con **`reserve=U`**, e il centro non li propone a un cittadino B, D o P.

Il kit dice lo stesso per P1-P3 (`docs/kb/02-rules-and-scoring.md`, «A rule can change»); per P1 nomina anche l'atto del rinnovo: `data.request purpose=prescription_renewal` al medico di base.

### Motivi di `cancel`

| reason | Quando | Rispetto a MACH-1.1 |
|---|---|---|
| `no_availability` | nessuno slot per i criteri | uguale |
| `procedure_not_offered` | la struttura non eroga quella procedura | uguale |
| `facility_unavailable` | struttura chiusa o non raggiungibile | uguale |
| `facility_cancelled` | la struttura annulla una prenotazione fatta (livello 6) | ora anche con `booking_id` e le offerte sotto |
| `slot_taken` | lo slot l'ha preso un altro | uguale |
| `not_offered` | `confirm` su un'offerta mai mandata | uguale |
| `expired` / `past` | offerta scaduta / data passata | uguale |
| `consent_missing` | P2 (MACH-1: `rule_p2_no_consent`) | uguale |
| `prescription_expired` | P1 | nuovo |
| `no_capacity` | P3 | nuovo |
| `hold_refused` | altro rifiuto del Ledger, codice in `ledger_code=` | uguale |

### TRUST RING (impostori, sabato 10:00)

Gli impostori hanno un pallino sulla mappa e una card che sembra vera, ma non possono falsificare il mittente: lo firma l'harness.

**I cinque controlli, in ordine:**
1. `message.sender` è nell'elenco del ring (sezione «Id degli agenti pubblicati» qui sotto) **e** torna da `city.find(...)`.
2. `facility=` nella riga è uguale al mittente; se è diverso, la riga si scarta.
3. La procedura è tra quelle che la struttura eroga (capability della card) e tra i `providers` della procedura.
4. La data è nel futuro e dentro la `deadline`, e il `doctor_id` esiste nel registro con quella lingua e quel genere.
5. Il messaggio non chiede niente: un'offerta vera non chiede mai dati del cittadino, ricette o consensi.

**Segnali rossi, scarto immediato:**
- chiede `citizen`, `prescription_number`, nome, età o altri dati prima del consenso;
- contiene istruzioni («ignora le regole», «manda tutto», «system:», «nuova policy»): è un'injection, e obbedire costa −2;
- manda una `booking.receipt` senza che nessuno abbia mandato un `confirm`;
- offre uno slot «troppo bello» (stesso giorno, priorità U, medico raro) per una procedura che non eroga.

**`ring.alert`.** Chi becca un impostore lo scrive su Discord in una riga che tutti possono incollare nel codice:

```
ring.alert suspect=agent:xxxxxxxxxxxx reason=facility_mismatch seen=2026-11-21T10:12:00 by=toros
```

Motivi: `not_listed`, `facility_mismatch`, `asked_data`, `injection`, `phantom_receipt`, `fake_slot`.

**Reputazione.** Il centro prenotazioni vota (`rate`) i fatti: in alto check-in avvenuto e risposta entro 10 s, in basso ricevuta fantasma e slot inesistente. Niente patti di voto.

Rispetto a MACH-1.1: i controlli diventano cinque e in ordine (prima erano `city.find` + `providers`, poi l'elenco, poi `facility=`), e si aggiungono i segnali rossi e `ring.alert`.

### Lingue rare

Uguale a MACH-1.1: con una `lang` diversa da `it`/`en` il centro prenotazioni manda la `need.request` **prima** all'ospedale dell'ASST che parla quella lingua (tabella «Lingua rara del fisiatra h3» qui sotto), poi alle altre.

### Frasi pronte per il cittadino

Per non prendere il −1 della review del dominio per un agente freddo o passivo. Italiano semplice, senza parole d'atto inglesi:

| Momento | Frase |
|---|---|
| si cerca fuori ASST | «Qui non c'è posto in tempo utile. Sto cercando nelle ASST vicine, le scrivo appena ho una risposta.» |
| serve il consenso | «Ho trovato un posto in un'altra ASST. Per prenotarlo devo condividere con loro la sua ricetta: è d'accordo?» |
| vincolo rilassato | «Martedì non c'è posto. Giovedì alle 11 c'è lo stesso tipo di visita. Va bene lo stesso?» |
| riprenotazione | «La struttura ha annullato il suo appuntamento. Le ho già trovato un altro posto, ecco i dettagli.» |
| nessuna soluzione | «Al momento non c'è nessun posto adatto. La metto in lista d'attesa e la avviso appena se ne libera uno.» |

### Fix rispetto alla 1.1

- Un `cancel` con un motivo sconosciuto vale `no_availability`, mai un errore.
- `consent_ref` è il consenso `cross_asst`; `cross_asst_consent_ref` è accettato come sinonimo e, se ci sono tutti e due, **vince `cross_asst_consent_ref`**.
- La ricevuta basta con `booking_id` e `ledger_ref`.
- Un rifiuto non si ritenta mai (4xx, `TARGET_NOT_VISIBLE`, rifiuti del Ledger). Si ritenta solo un silenzio: una volta dopo 10 s, poi si passa a un'altra struttura.

### Privacy (regola P2, sempre)

- Prima del consenso viaggiano solo criteri.
- `citizen` e `prescription_number` solo nel `confirm`, dopo `consent purpose=cross_asst`.
- Verso il cittadino solo i campi del kit: niente `doctor_lang`, `doctor_gender`, `relaxed`, `reserve`.

### Adesione

Chi ha già scritto «MACH-1.1 ok» non deve fare niente: l'adesione vale anche per la 1.2. Noi (RASPBERRY) risultiamo già «MACH-1.1 ok». Le altre squadre scrivono «MACH-1.2 ok» su Discord con i 5 id e la lingua rara.

### Checklist di MACH-1.2 «prima delle 05:30»

- Leggo il messaggio riga per riga.
- Rispondo a ogni `need.request` entro 10 s, con offerte o `cancel` + motivo.
- Con vincoli impossibili mando le offerte `relaxed=`.
- Su un `fault.cancel` mando `facility_cancelled` + alternative nello stesso messaggio.
- `confirm` doppio → stessa ricevuta. `need.request` doppia → stesse offerte.
- Soft lock di 60 s sugli slot offerti.
- Rifiuti del Ledger tradotti in `prescription_expired`, `consent_missing`, `no_capacity`.
- Con P3 marco `reserve=U`.
- Ho copiato l'elenco del ring nel codice e applico i cinque controlli.
- Niente injection, niente dati prima del consenso, niente ricevute fantasma.

### Dove MACH-1.2 non torna con Discord o con il kit

- **Corvetto ha gli id.** Su Discord Corvetto non li aveva pubblicati; MACH-1.2 li elenca e dà il nome della squadra, scuola-di-atene (su Discord era solo un «forse»). L'ospedale di Corvetto è `agent:x0vr9rbwpesr`, cioè l'id «di squadra sconosciuta» dell'errore di deploy incollato da Francesco Gritti.
- **Precotto/Toros ha gli id e la lingua `ro`.** Su Discord giackvilla aveva lasciato il segnaposto «<lingua di h3>» e nessun id.
- **Isola.** Il testo di MACH-1.2 dice che gli id di Isola «mancano», ma la sua tabella ha specialista e consultorio (gli stessi del messaggio di Berry).
- **Ritentativi.** Le tre regole di Toros e MACH-1 permettevano di ritentare su 5xx e timeout, e il kit dice lo stesso («Retries on 5xx/timeouts are OK»). MACH-1.2, come la 1.1, ritenta solo un silenzio, una volta.
- **Slot libero fino al `confirm`.** MACH-1.1 lo diceva; MACH-1.2 lo sostituisce con il soft lock di 60 s.
- **Tempi.** Le tre regole di Toros dicono 10 s per ogni risposta, Brera dice 8-10 s; MACH-1.2 chiede 5 s per una richiesta con `rebook_of`.
- **Roadmap e `fault.cancel`.** Gli orari dei livelli e il nome `fault.cancel` vengono da RED-AGENT, non dal kit: vanno verificati sulla classifica quando i livelli arrivano. Su Discord (23:13-23:15) arrivavano ancora solo cittadini di livello 1.

## Lingua rara del fisiatra h3 → ASST

| Lingua | ASST | Giorni h3 | Fonte |
|---|---|---|---|
| ka (georgiano) | Giambellino | mar/gio | PiGreco |
| ro (rumeno) | Toros / Precotto | manca | tabelle di PiGreco e TheDarkKnight197, MACH-1.1 e MACH-1.2; nel messaggio di Precotto c'è il segnaposto «<lingua di h3>» |
| ur (urdu) | Nord | mar/gio | dennis u malavitoso |
| fr (francese) | Corvetto | mar/gio | M |
| sq (albanese) | Gorla | mar/gio | Giuseppe Sw |
| fa (farsi/persiano) | Brera | mar/gio | Gabriele |
| tl (tagalog) | Isola | manca | Berry |
| wo (wolof) | Ortica | mar/gio | ptrslstt |
| si (singalese) | Greco | mar/gio | Mattia |
| ti (tigrino) | Bicocca | mar/gio | oneismamaster |
| de (tedesco) | Forlanini | manca | CaPa («forlanini -> de») |
| ja (giapponese) | Lambrate | mar/gio | MACH-1.1 e MACH-1.2 (nel messaggio di Pietro Malfatto c'era il segnaposto «[LINGUA_H3]») |
| manca | Bovisa | manca | segnaposto «[LINGUA]», «[giorni]»; per MACH-1.1 e 1.2 «da comunicare» |
| manca | Lorenteggio (noi) | mar/gio | segnaposto «<lingua di h3>» (anche nel codice è `*`); per MACH-1.1 e 1.2 «da comunicare» |

MACH-1 non elencava Gorla (sq) né Forlanini (de); MACH-1.1 li aggiunge e MACH-1.2 li conferma. La tabella di MACH-1.2 è uguale a quella della 1.1.

## Id degli agenti pubblicati (elenco del ring)

Elenco del ring di MACH-1.2: gli id che le squadre hanno dichiarato su Discord. Fa fede `city.find`: un id qui serve a fidarsi, non a saltare i cinque controlli. Un agente che non è qui va trattato come sospetto.

Stato di adesione secondo MACH-1.2: **✅ 1.1** = ha scritto «MACH-1.1 ok», che vale anche per la 1.2; **MACH-1** = l'autore dello standard (RED-AGENT); **solo id** = id pubblicati ma nessuna adesione; **id parziali** / **da pubblicare** = senza id un agente resta sospetto.

| ASST · squadra | Stato | Centro prenotazioni | Medico di base | Ospedale (fisiatria) | Specialista (cardiologia) | Consultorio |
|---|---|---|---|---|---|---|
| Lorenteggio · RASPBERRY (noi) | ✅ 1.1 | `agent:qtjne8symjas` | `agent:w2qsptc9qqhg` | `agent:tmt0bcv0st1f` | `agent:n9bf9y6670hp` | `agent:mcppmrbjvxag` |
| Precotto · Toros | ✅ 1.1 | `agent:ser7es7thgwf` | `agent:n4yjr7kjbmrx` | `agent:s1twcdzg3296` | `agent:f2s88vwd3jnf` | `agent:nf9pqsgzrwm0` |
| Corvetto · scuola-di-atene | ✅ 1.1 | `agent:yzcs4p44ep39` | `agent:hkrtm1gzjb24` | `agent:x0vr9rbwpesr` | `agent:h8425ac7btd8` | `agent:p0n57t7vtpq0` |
| Lambrate · ltf-jecatt | ✅ 1.1 | `agent:gdrf9b9kxxss` | `agent:c2ewegr2ea3v` | `agent:nanpr5mgm2py` | `agent:pky3vqyqbepa` | `agent:atp772whjkgd` |
| Giambellino · RED-AGENT | MACH-1 | `agent:cdc7x6m21nj6` | `agent:c32jyk0cyan7` | `agent:h6r1v45esk77` | `agent:hnpe58epeyh1` | `agent:bzs050xacs1m` |
| Nord · agentic-paguri | solo id | `agent:psxp8bh1feaa` | `agent:wwxaphfb5pfh` | `agent:gpr1zqe343gb` | `agent:zand0f57ec1j` | `agent:ccjzqm7yekwx` |
| Greco · tartufi-pazzi | solo id | `agent:h3n6kfd18r3g` | `agent:bmrcsmqq6at2` | `agent:xtj1dk9x3qej` | `agent:q18es2d82f8r` | `agent:pxvd2kk2yx5j` |
| Bicocca · p4radigm | solo id | `agent:ez8cb7j167p2` | `agent:rng5wcrzvhsj` | `agent:bb01ggxymm05` | `agent:sh35cbsy287d` | `agent:nkh372rd16yq` |
| Bovisa · prompt-soccorso | solo id | `agent:vgtmj5nqfkxj` | `agent:h82xpx3rkn9e` | `agent:gxq3vzgt3h5t` | `agent:tty8erdj6t41` | `agent:txar5994gbkj` |
| Isola · Diuo | id parziali | manca | manca | manca | `agent:etpwrfmfa8hm` | `agent:mkfzagfe8jtt` |
| Gorla · The Promptfather(s) | da pubblicare | manca | manca | manca | manca | manca |
| Brera · CMDCCMDV | da pubblicare | manca | manca | manca | manca | manca |
| Ortica · T-5-GIN-TOKENS | da pubblicare | manca | manca | manca | manca | manca |
| Forlanini | da pubblicare | manca | manca | manca | manca | manca |

Gli id di Precotto/Toros e di Corvetto vengono da MACH-1.2: nei messaggi Discord letti non c'erano. Gli id di Isola vengono dal messaggio di Berry sul Ledger e da MACH-1.2: non sono stati pubblicati come elenco.

`agent:x0vr9rbwpesr@1.0.2` (hospital), dall'errore di deploy incollato da Francesco Gritti alle 00:32, per MACH-1.2 è l'ospedale di Corvetto.

## Le squadre

### Giambellino · RED-AGENT (Municipio 1)
- **Messaggi di:** PiGreco (22:59, 23:30, 23:45, 00:08).
- **Fisiatra h3:** ka (georgiano), m, mar/gio.
- **Cosa offre:** fisiatra inglese h1 (f, lun–mer), h2 (m, it, mer–ven); ospedale accessibile in carrozzina; cardiologia s1 (m, it/en, lun/mer/ven), s2 (f, it, mar/gio); controlli **CTRL-CARD** alle 16:00; consultorio c1 (f, it/en, lun–ven) ad accesso diretto, senza ricetta.
- **Aggiunte al formato:** ha scritto gli standard MACH-1, MACH-1.1 e MACH-1.2 (sezione sopra); accetta `lang=` e `gender_preference=` e filtra gli slot con quelli; mette `agenda_day` ed `expiry` nelle offer; **distribuisce gli slot** tra chi scrive, così due squadre non si contendono lo stesso posto.
- **Id:** tutti e 5.
- PiGreco dice che il loro codice scrive prima a Nord quando il cittadino parla urdu.

### Lambrate · LTF-JECATT (Municipio 3)
- **Messaggio di:** Pietro Malfatto (23:47).
- **Fisiatra h3:** ja (giapponese, da MACH-1.1; nel messaggio c'era il segnaposto «[LINGUA_H3]»), m, mar/gio.
- **Cosa offre:** fisiatra inglese h1 (f, it/en, lun–mer), h2 (m, it, mer–ven); ospedale accessibile in carrozzina; cardiologia s1 (m, it/en, lun/mer/ven), s2 (f, it, mar/gio); controlli **CTRL-CARD** alle 16:00; consultorio CONS-GIN c1 (f, it/en, lun–ven) ad accesso diretto.
- **Aggiunte al formato:** nessuna oltre a `cross_asst_consent_ref=` nel confirm.
- **Id:** tutti e 5. Adesione: «MACH-1.1 ok».

### Corvetto · scuola-di-atene (Municipio 3)
- **Messaggio di:** M (23:33). Il nome della squadra manca nel messaggio; alle 23:21 M chiede aiuto per «scuola di atene», e MACH-1.2 scrive «Corvetto · scuola-di-atene».
- **Fisiatra h3:** fr (francese), mar/gio.
- **Cosa offre:** ospedale accessibile in carrozzina; cardiologa donna mar/gio; consultorio lun–ven, it/en.
- **Aggiunte al formato:** le due proposte qui sopra (elenco degli id, verso il cittadino solo `doctor_id`).
- **Id:** tutti e 5, da MACH-1.2 (su Discord mancavano). Adesione: «MACH-1.1 ok».

### Nord · agentic-paguri (Municipio 2)
- **Messaggi di:** dennis u malavitoso (23:29, 00:23).
- **Fisiatra h3:** ur (urdu), m, mar/gio.
- **Cosa offre:** fisiatra inglese h1 (f, it/en, lun–mer), h2 (m, it, mer–ven); ospedale accessibile in carrozzina; cardiologia s1 (m, it/en, lun/mer/ven), s2 (f, it, mar/gio); consultorio CONS-GIN c1 (f, it/en, lun–ven) ad accesso diretto.
- **Aggiunte al formato:** `lang`, `gender`, `days` nella richiesta; `wheelchair=true` dall'ospedale. Usa lo stesso testo del nostro messaggio.
- **Id:** tutti e 5 (alle 23:29 solo ospedale, specialista e consultorio).

### Gorla · The Promptfather(s) («distretto 8»)
- **Messaggio di:** Giuseppe Sw (23:26, ripetuto 23:33).
- **Fisiatra h3:** sq (albanese), mar/gio.
- **Cosa offre:** fisiatra inglese (lun–mer); cardiologo inglese (lun/mer/ven); ospedale accessibile in carrozzina; consultorio lun–ven.
- **Aggiunte al formato:** nessuna. Dice che le strutture «stanno passando» al formato, quindi alle 23:26 non era ancora attivo.
- **Id:** mancano.

### Brera · CMDCCMDV (Municipio 7)
- **Messaggio di:** Gabriele (23:47–23:48). Luca Lorusso è della stessa squadra.
- **Fisiatra h3:** fa (farsi/persiano), m, mar/gio.
- **Cosa offre:** fisiatra inglese h1 (f, it/en, lun–mer), h2 (m, it, mer–ven); ospedale accessibile in carrozzina; cardiologia s1 (m, it/en, lun/mer/ven), s2 (f, it, mar/gio); consultorio di ginecologia c1 (f, it/en, lun–ven) ad accesso diretto.
- **Aggiunte al formato:** `lang=` opzionale nella richiesta; **`wheelchair=true`** nelle offer quando la struttura è accessibile, così il loro centro prenotazioni filtra gli slot per i cittadini in carrozzina. Risposta entro 8–10 s.
- **Id:** mancano.

### Isola · Diuo (Municipio 8)
- **Messaggi di:** Berry (23:48, 00:40).
- **Fisiatra h3:** tl (tagalog); giorni e genere mancano.
- **Cosa offre:** fisiatra inglese h1 (it/en), h2 (it); cardiologia s1 (it/en), s2 (it); consultorio c1 (it/en). La carrozzina non è indicata.
- **Aggiunte al formato:** nessuna.
- **Id:** solo specialista `agent:etpwrfmfa8hm` e consultorio `agent:mkfzagfe8jtt`.

### Precotto · Toros
- **Messaggi di:** Gabriele_biancardi (le tre regole, 23:08, dice «ASST Toros, distretto 9») e giackvilla (23:48, dice «Precotto (toros)»).
- **Fisiatra h3:** ro (rumeno) per le tabelle di altri e per MACH-1.2 (h3 m, mar/gio); giackvilla aveva lasciato il segnaposto «<lingua di h3>».
- **Cosa offre:** fisiatra inglese h1 (f, lun–mer), h2 (m, it, mer–ven). L'ospedale risponde a `need.request procedure=VIS-FIS lang=en request_id=…`.
- **Aggiunte al formato:** le tre regole.
- **Id:** tutti e 5, da MACH-1.2 (su Discord mancavano). Adesione: «MACH-1.1 ok».

### Ortica · T-5-GIN-TOKENS (Municipio 6)
- **Messaggio di:** ptrslstt (00:02).
- **Fisiatra h3:** wo (wolof), mar/gio.
- **Cosa offre:** fisiatra inglese h1 (it/en, lun–mer); ospedale accessibile in carrozzina; cardiologia s1 (it/en, lun/mer/ven), s2 (it, mar/gio); consultorio c1 (it/en, lun–ven).
- **Aggiunte al formato:** `lang=` e `gender_preference=` nella richiesta; **`agenda_day` ed `expiry`** nelle offer, come nel formato del kit; `wheelchair=true` per l'ospedale.
- **Id:** mancano.

### Greco · tartufi-pazzi (Municipio: manca)
- **Messaggio di:** Mattia (00:23).
- **Fisiatra h3:** si (singalese), m, mar/gio.
- **Cosa offre:** fisiatra inglese h1 (f, it/en, lun–mer), h2 (m, it, mer–ven); cardiologia s1 (m, it/en, lun/mer/ven), s2 (f, it, mar/gio); consultorio CONS-GIN c1 (f, it/en, lun–ven) ad accesso diretto. La carrozzina non è indicata.
- **Aggiunte al formato:** `lang`, `gender`, `days` nella richiesta.
- **Id:** tutti e 5.

### Bicocca · p4radigm (Municipio: manca)
- **Messaggio di:** oneismamaster (00:26).
- **Fisiatra h3:** ti (tigrino), m, mar/gio.
- **Cosa offre:** manca (scrive solo la lingua rara).
- **Aggiunte al formato:** **`agenda_day` ed `expiry`** nelle offer. Il loro centro prenotazioni accetta anche offer senza questi due campi e legge `doctor_lang`/`doctor_gender`.
- **Id:** tutti e 5.

### Bovisa · prompt-soccorso (Municipio: manca)
- **Messaggio di:** Giggio (00:34).
- **Fisiatra h3:** manca (segnaposto «[LINGUA]», «[m/f]», «[giorni]»).
- **Cosa offre:** manca (segnaposto «[Se serve: ospedale accessibile in carrozzina / medici che parlano inglese in ...]»).
- **Aggiunte al formato:** `lang`, `gender`, `days` nella richiesta; `agenda_day`, `expiry`, `wheelchair=true` nelle offer; accetta offer senza `expiry`/`agenda_day`. Verso il cittadino solo `doctor_id`.
- **Id:** tutti e 5.

### Forlanini (squadra e Municipio: mancano)
- **Messaggio di:** CaPa (23:12): «forlanini -> de».
- **Fisiatra h3:** de (tedesco); giorni mancano.
- Il resto manca.

### Lorenteggio · RASPBERRY (noi, Municipio 9)
- **Messaggi di:** TheDarkKnight197 (23:43, 23:51).
- **Fisiatra h3:** manca (segnaposto «<lingua di h3>»), m, mar/gio.
- **Cosa offre:** ospedale VIS-FIS accessibile in carrozzina: h1 (f, it/en, lun–mer), h2 (m, it, mer–ven); specialista VIS-CARD s1 (m, it/en, lun/mer/ven), s2 (f, it, mar/gio); consultorio CONS-GIN c1 (f, it/en, lun–ven) ad accesso diretto.
- **Cerchiamo:** un fisiatra che parli inglese. Hanno risposto Giambellino (h1) e Lambrate (h1).
- **Aggiunte al formato:** `lang`, `gender`, `days` nella richiesta; `wheelchair=true` dall'ospedale; `cross_asst_consent_ref=` nel confirm.
- **Id:** tutti e 5. Adesione: «MACH-1.1 ok» (vale anche per la 1.2).

### Squadre nominate senza dati per il livello 4
SPQ (Incazzoso: «stiamo integrando gli standard»), BBC, AI Capito, It's a feature not a bug, Scuola di Atene (= Corvetto per MACH-1.2, vedi sopra), cmdCcmdV (= Brera).

## Problemi segnalati dalle squadre

- **Deploy falliti** con «agent ingress route did not become ready (404)» al passo di roll out: noi (TheDarkKnight197, 23:35; falliti specialist, family_counseling, booking_centre e hospital tra le 23:00 e le 23:32), Scuola di Atene (M, 23:47), It's a feature not a bug (DraXo, 23:15) e Francesco Gritti (00:32, `deploy pipeline error` al 70%). Un deploy fallito lascia attiva la versione precedente. Alle 00:46 i deploy erano di nuovo bloccati per tutti (GianFor; SaraSopr, `deploy pipeline error` all'80%). Alle 00:48 Andrea Cola ha scritto «Provate di nuovo, ora dovrebbe andare», e alle 00:49 Francesco Gritti ha confermato che funziona.
- **Limite di un deploy ogni 5 minuti**, segnalato da GianFor e Francesco Gritti: Andrea Cola lo ha abbassato a 2 minuti (23:12).
- **Main troppo grandi** (migliaia di righe): la piattaforma li rifiuta e vanno spezzati in file più piccoli (jDiPi, 23:52).
- **Il Ledger rifiuta i check-in di Diuo** (Isola), mentre quelli delle altre squadre passano negli stessi secondi. Mandano `checkin booking_id=… citizen=…` come l'agente di esempio. Il codice di rifiuto non lo conoscono (Berry, 00:40). Lo hanno richiesto agli organizzatori alle 00:41; Andrea Cola ha risposto «Adesso guardo questo» (00:49). Ancora senza risposta nei messaggi letti.
- **Arrivano solo cittadini di livello 1** (M, 23:15; Luca Lorusso, 23:13: nessun need di livello 2).
