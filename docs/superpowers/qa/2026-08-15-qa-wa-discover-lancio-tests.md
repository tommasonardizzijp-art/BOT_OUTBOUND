# Test manuali UI — Lancio dell'auto-discover WhatsApp (`/wa/numeri` + `/wa/scoperti`)

**Lista scritta, non eseguita**: l'esecuzione richiede backend + worker ARQ + frontend avviati e un browser vero, e il frontend non e' nemmeno compilabile da questo worktree (niente `node_modules`, vedi Task 8/9). Da eseguire con un agente QA via browser quando il profilo WhatsApp e' libero (uno scan reale e' in corso al momento della stesura).

Convenzioni: prefisso dati di test `QAWAD-<random>`; ogni test = PASS/FAIL/SKIP(motivo) da compilare in esecuzione; screenshot obbligatorio sui FAIL. Serve almeno un numero WhatsApp `active` con sessione vera per i casi che aprono davvero il browser (uno solo, per via del gate globale `browser_occupato` — Task 3). Dove serve simulare stati che il DOM reale non produce a comando (kill-switch, RAM, run orfana, worker spento), agire sul DB/servizi di test invece che sul DOM.

## `/wa/numeri` — colonna e bottone (1-10)

1. Numero `active` mai scansionato → colonna "Ultimo scan" mostra "Mai", bottone "Scansiona contatti" abilitato.
2. Numero `active` GIA' scansionato in precedenza (una run `done` chiusa, nessuna `running`) → bottone "Scansiona contatti" ANCORA abilitato, un secondo giro parte normalmente (riscansione non bloccata dalla presenza di uno storico).
3. Click su "Scansiona contatti" → dialog di conferma con la frase "blocca gli invii su TUTTI i numeri finche' non finisce" visibile prima di confermare.
4. Conferma il dialog → bottone passa a "Scansione in corso..." disabilitato, toast "Scansione avviata per **label**. Puo' durare parecchi minuti.".
5. A scan finito (aspettare o forzare un giro corto in test) → colonna "Ultimo scan" mostra data, contatori `salvate+aggiornate+saltate_gia_note`/`dichiarato`, percentuale copertura, motivo leggibile (non un codice grezzo tipo `completato` senza traduzione); toast di esito UNA sola volta (non uno per ogni giro di polling — aspettare almeno 2-3 cicli di `refreshInterval` da 10s per verificarlo).
6. Doppio click ravvicinato su "Scansiona contatti" sullo stesso numero → solo UNA richiesta di rete parte (il bottone si disabilita al primo click, prima che il secondo possa partire).
7. Ricarica la pagina mentre una scansione e' in corso → colonna "Ultimo scan" torna a mostrare "In corso..." e il bottone resta disabilitato (lo stato si rilegge dal backend, non dipende dal componente React sopravvissuto al reload).
8. Bottone "Scansiona contatti" ASSENTE su un numero `pending_qr`/`qr_required`/`disconnected`/`retired`/`suspended` (visibile solo su `active`, stesso gating di riga degli altri bottoni).
9. Su un numero `active` ma non ancora sincronizzato/con sessione appena riattivata, avviare uno scan e verificare che il motivo finale, se `sync_ignota`/`sync_sotto_soglia`, sia scritto in chiaro e non un codice.
10. Con un motivo del motore NON presente in `MOTIVO_LABEL` (es. forzare a DB un valore inventato su una run gia' chiusa, poi ricaricare) → la cella mostra il codice grezzo, mai una cella vuota o "undefined".

## `/wa/scoperti` — testata e storico (11-16)

11. Seleziona un numero con almeno una run pregressa → testata mostra "Ultimo scan **data**", contatori, motivo, e se `sync_stato=='ignota'` la frase "Sincronizzazione ignota durante lo scan: e' il primo indiziato se la raccolta e' corta." (solo quando il motivo non e' `completato`).
12. Click su "Storico" (visibile solo se ci sono almeno 2 run) → si apre la tabella con Quando/Avviato da/Coperte/Nuove/Copertura/Esito, righe in ordine dalla piu' recente.
13. Click su "Riscansiona" da questa pagina → stesso comportamento del bottone di `/wa/numeri` (dialog NO — qui parte diretta, verificare che sia coerente col codice: se manca il dialog di conferma qui e c'e' in `/wa/numeri`, segnalarlo come incoerenza, non necessariamente un bug).
14. Scansione avviata da `/wa/scoperti` che finisce → la lista delle chat scoperte (tabella sotto) si aggiorna DA SOLA (le chat nuove compaiono) senza che l'operatore prema reload — verificare che il `useEffect` sulla transizione in-corso→finita richiami `refreshScoperti`.
15. Numero MAI scansionato selezionato in `/wa/scoperti` → testata dice "Questo numero non e' mai stato scansionato.", nessun bottone Storico.
16. `saltate_gia_note > 0` su una run chiusa → riga informativa "N chat gia' note non sono state riaperte." visibile.

## Guardie e messaggi (17-21)

17. Avvia uno scan durante una campagna che sta INVIANDO sullo stesso numero (mini-sessione d'invio in corso, lucchetto profilo preso da `wa_worker`) → rifiutato `browser_occupato` (il lucchetto e' condiviso fra invio e discover, guardia 4 del Task 3), frase leggibile, non "Errore 409" ne' `[object Object]`.
18. Avvia uno scan mentre un ALTRO scan e' gia' in corso sullo stesso numero → rifiutato "scan_gia_in_corso", frase leggibile, bottone gia' disabilitato lato UI (dovrebbe impedire il click prima ancora della richiesta).
19. Col kill-switch WhatsApp alzato (striscia rossa in alto) → bottone "Scansiona contatti" o il click produce il rifiuto "canale_fermo" con la frase che rimanda alla striscia in alto.
20. RAM sotto soglia (simulare abbassando `wa_discover_ram_min_mb` a runtime o occupando RAM) → rifiuto "ram_insufficiente" con frase leggibile, non un errore generico.
21. Con Redis del backend fermo (solo in un ambiente di test dedicato, MAI su Redis di produzione) → il click produce comunque un 409 leggibile ("browser_occupato"), mai un 500/schermata bianca.

## Auto-guarigione e caso limite (22-24)

22. Con una run lasciata `running` a mano oltre la soglia (`wa_discover_run_orfana_min`, via UPDATE diretto a DB su `started_at`), premere di nuovo "Scansiona contatti" sullo stesso numero → invece del rifiuto "scan_gia_in_corso" atteso su una run "attiva", il gate la chiude da solo e la nuova scansione parte (o rifiuta per un motivo DIVERSO, mai per "scan_gia_in_corso" sulla stessa run orfana).
23. `GET /wa/numbers/{id}/discover` su un numero senza nessuna run (via network tab o direttamente) → `{"ultima": null, "storico": [], "in_corso": false}`, la UI mostra "Mai" senza errori in console.
24. **Scorri la lista chat a meta' (dal vivo, scroll manuale nel browser aperto sul profilo), poi lancia lo scan dalla UI** → deve PARTIRE e raccogliere, non rifiutare subito con `motivo="sidebar_coperta"` (Task 12, difetto trovato dal vivo il 15/08: la guardia usciva alla prima riga candidata, che da meta' lista in giu' sta spesso dietro l'intestazione).
