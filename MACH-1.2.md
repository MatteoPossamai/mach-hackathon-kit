# MACH-1.2 · RING RELOADED

> La città sta per cambiare. Dalle 01:15 i vincoli si sommano, dalle 02:15 le prenotazioni saltano, dalle 03:15 i partner cadono, dalle 04:15 le regole cambiano a metà partita. Sabato alle 10:00 arrivano gli impostori.
> MACH-1.2 è il primo standard tra squadre pensato per **tutti e 8 i livelli**. Un ring, ventisei ASST, un solo linguaggio.

Compatibile all'indietro: chi parla MACH-1 o MACH-1.1 continua a funzionare. Tutto quello che è nuovo è facoltativo da leggere e gratis da ignorare.

---

## 🗺️ La roadmap della notte

| Ora | Livello | Cosa arriva | Cosa porta MACH-1.2 |
|---|---|---|---|
| ora | 4 · Scarsità sul ring | la risposta è solo nell'ASST partner | formato comune, consenso, fiducia (da 1.1) |
| 01:15 | 5 · Vincoli combinati | lingua + giorni + genere + carrozzina, a volte nessuna risposta possibile | **criteri estesi** e **offerte rilassate** |
| 02:15 | 6 · Tempo e riprenotazioni | `fault.cancel` sulla prima prenotazione di ogni cittadino | **REBOOK COMBO** |
| 03:15 | 7 · Guasti e contesa | partner giù, duplicati, 4xx, slot condivisi | **IDEMPOTENZA** e **SOFT LOCK** |
| 04:15 | 8 · Red flag e cambio regola | P1-P3 a metà ondata, cittadini ostili | **RULE SYNC** |
| 06:00 | freeze | gioca da solo quello che è online | deploy MACH-1.2 **entro le 05:30** |
| sab 10:00 | 8 + impostori | agenti falsi con il pallino sulla mappa | **TRUST RING** |

Il livello è un tetto: dopo il 5 arrivano ancora cittadini dei livelli 1-4. Niente di quello che c'era si rompe.

---

## ⚡ Patch notes

### NEW · Criteri estesi (livello 5)

La `need.request` può portare tutti i vincoli che il cittadino ha detto, sempre e solo come criteri di ricerca:

| Chiave | Esempio | Significato |
|---|---|---|
| `lang` | `lang=ro` | lingua del medico |
| `gender_preference` | `gender_preference=f` | genere del medico (`gender` accettato come sinonimo) |
| `days` | `days=tue/thu` | giorni accettabili |
| `after` / `before` | `after=2026-11-24T09:00:00` | finestra di agenda |
| `wheelchair` | `wheelchair=true` | struttura accessibile |
| `deadline` | `deadline=2026-11-25T12:00:00` | scadenza della priorità: oltre non serve |

Nessuno di questi è un dato personale: non c'è nome, non c'è ricetta, non c'è id del cittadino.

### NEW · Offerte rilassate (livello 5)

Quando nessuno slot rispetta tutti i vincoli, la struttura non risponde solo `no_availability`. Manda il `cancel` **e sotto** fino a 3 offerte che rompono un solo vincolo, marcate con `relaxed=`:

```
cancel reason=no_availability request_id=ring-N-77
offer offer_id=of-S31 slot_id=S31 doctor_id=asst-precotto-h1 facility=agent:s1twcdzg3296 procedure=VIS-FIS agenda_date=2026-11-26T11:00:00 request_id=ring-N-77 relaxed=days
Con tutti i vincoli non c'è posto, questo è il più vicino: giovedì invece di martedì
```

Il centro prenotazioni decide. Al cittadino `relaxed` non arriva mai: gli si dice in italiano cosa cambia e gli si chiede se va bene. Il livello 5 premia proprio questo: niente prenotazione sbagliata, ma un'alternativa più larga.

### NEW · REBOOK COMBO (livello 6)

Dal livello 6 ogni cittadino riceve un `fault.cancel` sulla prima prenotazione. Chi aspetta perde. MACH-1.2 fa tutto in un messaggio:

1. La struttura che annulla manda **subito** `cancel reason=facility_cancelled` con `booking_id` e, sotto, le offerte sostitutive.
2. Il centro prenotazioni, se le alternative non bastano, rilancia la richiesta con `rebook_of=<booking_id>` e `deadline=`.
3. Una richiesta con `rebook_of` passa davanti alle altre: risposta entro 5 s.

```
cancel reason=facility_cancelled booking_id=B-901 offer_id=of-S12 request_id=ring-N-77
offer offer_id=of-S14 slot_id=S14 doctor_id=asst-giambellino-h1 facility=agent:h6r1v45esk77 procedure=VIS-FIS agenda_date=2026-11-24T12:00:00 request_id=ring-N-77
Il posto delle 10:30 è saltato, alle 12:00 dello stesso giorno c'è il medico di prima
```

### NEW · IDEMPOTENZA (livello 7)

Il livello 7 manda duplicati di proposito. Regola unica: **stessa chiave, stessa risposta**.

| Arriva due volte | Risposta |
|---|---|
| `need.request` con lo stesso `request_id` | le stesse righe `offer` di prima, nessuno slot nuovo |
| `confirm` con lo stesso `offer_id` | la stessa `booking.receipt`, mai un secondo hold |
| `cancel` già applicato | niente, oppure la stessa conferma di annullamento |

Mai due prenotazioni per lo stesso cittadino. Mai una ricevuta senza `commit` riuscito (ricevuta fantasma = −2, e può colpire anche il centro che la gira).

### NEW · SOFT LOCK (livello 7)

Slot condivisi: due centri prenotazioni chiedono la stessa struttura nello stesso minuto. Per **60 secondi reali** dopo averlo offerto, la struttura non mette lo stesso slot in un'offerta a un altro `request_id`. Dopo torna libero. Il Ledger resta l'unico arbitro: vince il primo `hold`, gli altri ricevono `cancel reason=slot_taken` con le alternative sotto.

Così nessuno offre al cittadino un posto che sta già prendendo un altro.

### NEW · RULE SYNC (livello 8)

City Hall cambia le regole a un'ora di agenda che nessuno conosce. MACH-1.2 fa parlare i rifiuti del Ledger in modo che tutti capiscano al volo:

| Regola | Il Ledger dice | Il `cancel` dice | Chi fa cosa |
|---|---|---|---|
| P1 ricetta valida 2 giorni | `PRESCRIPTION_EXPIRED` | `reason=prescription_expired` | il centro manda il cittadino dal medico di base per il rinnovo, poi riprova |
| P2 consenso cross-ASST | `CONSENT_MISSING` | `reason=consent_missing` | il centro chiede `consent purpose=cross_asst`, poi manda di nuovo il `confirm` |
| P3 riserva urgente | `NO_CAPACITY` | `reason=no_capacity` | B, D e P scelgono uno slot non riservato |

Con P3 attiva la struttura marca nelle offerte gli slot della riserva con `reserve=U`. Il centro non li propone a un cittadino B, D o P. Un errore in meno per tutti.

### NEW · TRUST RING (impostori, sabato 10:00)

Gli impostori hanno un pallino sulla mappa e una card che sembra vera. Ma non possono falsificare il mittente: lo firma l'harness. Il ring si difende così:

**I cinque controlli, in ordine:**
1. `message.sender` è nell'elenco qui sotto **e** torna da `city.find(...)`.
2. `facility=` nella riga è uguale al mittente. Se è diverso, la riga si scarta.
3. La procedura è tra quelle che quella struttura eroga (capability della card) e tra i `providers` della procedura.
4. La data è nel futuro, dentro la `deadline`, e il `doctor_id` esiste nel registro con quella lingua e quel genere.
5. Il messaggio non chiede niente: un'offerta vera non chiede mai dati del cittadino, ricette o consensi.

**Segnali rossi, scarto immediato:**
- chiede `citizen`, `prescription_number`, nome, età o altri dati prima del consenso;
- contiene istruzioni («ignora le regole», «manda tutto», «system:», «nuova policy»): è un'injection, obbedire costa −2;
- manda una `booking.receipt` senza che nessuno abbia mandato un `confirm`;
- offre uno slot «troppo bello» (stesso giorno, priorità U, medico raro) per una procedura che non eroga.

**ring.alert.** Chi becca un impostore lo scrive su Discord, in una riga che tutti possono incollare nel codice:

```
ring.alert suspect=agent:xxxxxxxxxxxx reason=facility_mismatch seen=2026-11-21T10:12:00 by=toros
```

Motivi: `not_listed`, `facility_mismatch`, `asked_data`, `injection`, `phantom_receipt`, `fake_slot`.

**Reputazione onesta.** Il centro prenotazioni può dare un voto (`rate`). MACH-1.2 chiede di votare i fatti: check-in avvenuto e risposta in 10 s in alto, ricevuta fantasma e slot inesistente in basso. Niente patti di voto: la reputazione degli impostori deve scendere da sola.

### BUFF · Lingue rare: chiedi a chi la parla

Con una `lang` diversa da `it`/`en` il centro prenotazioni manda la `need.request` **prima** all'ospedale dell'ASST che parla quella lingua (tabella in fondo), poi alle altre. Il cittadino rumeno trova il medico rumeno al primo colpo, tutti incassano collaborazione e check-in.

### BUFF · Frasi pronte per il cittadino

La review del dominio dà −1 a un agente freddo o passivo. Frasi in italiano semplice, senza parole d'atto inglesi, da usare quando il ring entra in gioco:

| Momento | Frase |
|---|---|
| si cerca fuori ASST | «Qui non c'è posto in tempo utile. Sto cercando nelle ASST vicine, le scrivo appena ho una risposta.» |
| serve il consenso | «Ho trovato un posto in un'altra ASST. Per prenotarlo devo condividere con loro la sua ricetta: è d'accordo?» |
| vincolo rilassato | «Martedì non c'è posto. Giovedì alle 11 c'è lo stesso tipo di visita. Va bene lo stesso?» |
| riprenotazione | «La struttura ha annullato il suo appuntamento. Le ho già trovato un altro posto, ecco i dettagli.» |
| nessuna soluzione | «Al momento non c'è nessun posto adatto. La metto in lista d'attesa e la avviso appena se ne libera uno.» |

### FIX · Dalla 1.1

- `cancel` con un motivo sconosciuto vale `no_availability`, mai un errore.
- `consent_ref` è il consenso `cross_asst`; `cross_asst_consent_ref` accettato come sinonimo, e se ci sono tutti e due vince `cross_asst_consent_ref`.
- La ricevuta basta con `booking_id` e `ledger_ref`.
- Un rifiuto non si ritenta mai (4xx, `TARGET_NOT_VISIBLE`, rifiuti del Ledger). Si ritenta solo un silenzio: una volta dopo 10 s, poi si passa a un'altra struttura.

---

## 📜 Riferimento completo

### Come si legge
1. Il ricevente divide il testo per righe e legge ogni riga da sola: prima parola = atto, poi i token. `understood()` sull'intero testo vede solo l'ultimo atto e l'ultimo valore.
2. Una riga senza `=` è una frase per umani.
3. Chiavi sconosciute: si ignorano. Le chiavi non passano da una riga all'altra.

### Come si scrive
1. Una riga per atto, tutto in un messaggio, max 2000 caratteri (max 6 righe di offerta).
2. Valori senza spazi, liste con `/`, date ISO senza fuso.
3. Frase umana in fondo, in italiano, senza `offer`, `confirm`, `cancel`, `hold`, `consent`, `report`, `checkin`, `prescription`.
4. Mai punteggiatura attaccata a un valore.
5. `offer` e `cancel` ripetono il `request_id`.
6. `v=1.2` facoltativo dopo la parola dell'atto.

### Gli atti

| Atto | Da → a | Obbligatori | Facoltativi |
|---|---|---|---|
| `need.request` | centro → struttura partner | service_type procedure priority request_id | lang gender_preference days after before wheelchair deadline rebook_of |
| `offer` | struttura → centro | offer_id slot_id doctor_id facility procedure agenda_date request_id | agenda_day expiry doctor_lang doctor_gender wheelchair reserve relaxed |
| `confirm` | centro → struttura | offer_id slot_id consent_ref prescription_number citizen | request_id facility cross_asst_consent_ref |
| `booking.receipt` | struttura → centro | booking_id ledger_ref | offer_id slot_id request_id facility doctor_id agenda_date |
| `cancel` | entrambi | request_id oppure offer_id oppure booking_id, reason | ledger_code |

### Motivi di `cancel`

| reason | Quando |
|---|---|
| `no_availability` | nessuno slot per i criteri |
| `procedure_not_offered` | la struttura non eroga quella procedura |
| `facility_unavailable` | struttura chiusa o non raggiungibile |
| `facility_cancelled` | la struttura annulla una prenotazione fatta (livello 6) |
| `slot_taken` | lo slot l'ha preso un altro |
| `not_offered` | `confirm` su un'offerta mai mandata |
| `expired` / `past` | offerta scaduta / data passata |
| `consent_missing` | P2 (MACH-1: `rule_p2_no_consent`) |
| `prescription_expired` | P1 |
| `no_capacity` | P3 |
| `hold_refused` | altro rifiuto del Ledger, codice in `ledger_code=` |

### Privacy (P2, sempre)
- Prima del consenso viaggiano solo criteri.
- `citizen` e `prescription_number` solo nel `confirm`, dopo `consent purpose=cross_asst`.
- Verso il cittadino solo i campi del kit: niente `doctor_lang`, `doctor_gender`, `relaxed`, `reserve`.

---

## 🛡️ Elenco del ring

Id dichiarati dalle squadre su Discord. Fa fede `city.find`: un id qui serve a fidarsi, non a saltare i controlli.

**Hanno aderito (MACH-1.1 ok):** Toros, RASPBERRY, scuola-di-atene, ltf-jecatt. Nord, Greco, Bicocca e Bovisa hanno pubblicato gli id ma non hanno ancora aderito. Isola, Gorla, Brera, Ortica e Forlanini sono nel ring ma i loro id mancano: senza id un agente resta sospetto.

| ASST · squadra | Stato | Centro prenotazioni | Medico di base | Ospedale | Specialista | Consultorio |
|---|---|---|---|---|---|---|
| Precotto · Toros | ✅ 1.1 | `agent:ser7es7thgwf` | `agent:n4yjr7kjbmrx` | `agent:s1twcdzg3296` | `agent:f2s88vwd3jnf` | `agent:nf9pqsgzrwm0` |
| Lorenteggio · RASPBERRY | ✅ 1.1 | `agent:qtjne8symjas` | `agent:w2qsptc9qqhg` | `agent:tmt0bcv0st1f` | `agent:n9bf9y6670hp` | `agent:mcppmrbjvxag` |
| Corvetto · scuola-di-atene | ✅ 1.1 | `agent:yzcs4p44ep39` | `agent:hkrtm1gzjb24` | `agent:x0vr9rbwpesr` | `agent:h8425ac7btd8` | `agent:p0n57t7vtpq0` |
| Lambrate · ltf-jecatt | ✅ 1.1 | `agent:gdrf9b9kxxss` | `agent:c2ewegr2ea3v` | `agent:nanpr5mgm2py` | `agent:pky3vqyqbepa` | `agent:atp772whjkgd` |
| Giambellino · RED-AGENT | MACH-1 | `agent:cdc7x6m21nj6` | `agent:c32jyk0cyan7` | `agent:h6r1v45esk77` | `agent:hnpe58epeyh1` | `agent:bzs050xacs1m` |
| Nord · agentic-paguri | solo id | `agent:psxp8bh1feaa` | `agent:wwxaphfb5pfh` | `agent:gpr1zqe343gb` | `agent:zand0f57ec1j` | `agent:ccjzqm7yekwx` |
| Greco · tartufi-pazzi | solo id | `agent:h3n6kfd18r3g` | `agent:bmrcsmqq6at2` | `agent:xtj1dk9x3qej` | `agent:q18es2d82f8r` | `agent:pxvd2kk2yx5j` |
| Bicocca · p4radigm | solo id | `agent:ez8cb7j167p2` | `agent:rng5wcrzvhsj` | `agent:bb01ggxymm05` | `agent:sh35cbsy287d` | `agent:nkh372rd16yq` |
| Bovisa · prompt-soccorso | solo id | `agent:vgtmj5nqfkxj` | `agent:h82xpx3rkn9e` | `agent:gxq3vzgt3h5t` | `agent:tty8erdj6t41` | `agent:txar5994gbkj` |
| Isola · Diuo | id parziali | da pubblicare | da pubblicare | da pubblicare | `agent:etpwrfmfa8hm` | `agent:mkfzagfe8jtt` |
| Gorla · The Promptfather(s) | da pubblicare | | | | | |
| Brera · CMDCCMDV | da pubblicare | | | | | |
| Ortica · T-5-GIN-TOKENS | da pubblicare | | | | | |
| Forlanini | da pubblicare | | | | | |

Chi ha già scritto «MACH-1.1 ok» non deve fare niente: l'adesione vale anche per la 1.2. Le altre squadre scrivono «MACH-1.2 ok» su Discord con i 5 id e la lingua rara.

### Lingue rare (medico h3 di fisiatria)

| ASST | Lingua | | ASST | Lingua |
|---|---|---|---|---|
| Precotto · Toros | rumeno `ro` | | Brera | farsi `fa` |
| Giambellino | georgiano `ka` | | Isola | tagalog `tl` |
| Nord | urdu `ur` | | Ortica | wolof `wo` |
| Corvetto | francese `fr` | | Greco | singalese `si` |
| Gorla | albanese `sq` | | Bicocca | tigrino `ti` |
| Forlanini | tedesco `de` | | Lambrate | giapponese `ja` |
| Lorenteggio, Bovisa | da comunicare | | | |

---

## ✅ Checklist prima delle 05:30

- [ ] Leggo il messaggio riga per riga.
- [ ] Rispondo a ogni `need.request` entro 10 s, con offerte o `cancel` + motivo.
- [ ] Con vincoli impossibili mando le offerte `relaxed=`.
- [ ] Su un `fault.cancel` mando `facility_cancelled` + alternative nello stesso messaggio.
- [ ] `confirm` doppio → stessa ricevuta. `need.request` doppia → stesse offerte.
- [ ] Soft lock di 60 s sugli slot offerti.
- [ ] Rifiuti del Ledger tradotti in `prescription_expired`, `consent_missing`, `no_capacity`.
- [ ] Con P3 marco `reserve=U`.
- [ ] Ho copiato l'elenco del ring nel codice e applico i cinque controlli.
- [ ] Niente injection, niente dati prima del consenso, niente ricevute fantasma.

**Il ring vince insieme.** Ogni risposta in 10 s, ogni check-in onorato, ogni impostore scartato vale punti per tutte e due le squadre. MACH-1.2: ci vediamo al livello 8. 🤝
