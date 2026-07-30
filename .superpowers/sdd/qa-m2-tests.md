# Test manuali UI — WhatsApp M2 (eseguiti da agente QA via browser)

Convenzioni: prefisso dati di test `QAM2-<random>`; ogni test = PASS/FAIL/SKIP(motivo); screenshot obbligatorio sui FAIL. Dev server frontend + backend attivi, DB di test isolato (mai il DB dev condiviso con altre sessioni). Verificare `D:\dev\tools\ram-guard\guard.ps1 stato` prima di avviare.

## Ingresso e picker canale (1-3)
1. Login → redirect a `/canale` (nessuna preferenza salvata) → due card Instagram/WhatsApp visibili.
2. Click su WhatsApp → atterra su `/wa`, tema verde (`#128C7E`) applicato, nav propria (Campagne · Numeri · Nuova campagna · Cambia canale).
3. Ricarica la pagina dopo aver scelto WhatsApp → `/` reindirizza direttamente a `/wa` (preferenza ricordata in localStorage), non ripassa dal picker.

## Tenant e numeri (4-9)
4. Creazione tenant valido → compare nella select "tenant" della pagina nuova campagna.
5. Creazione numero (via script di seed o endpoint diretto, la UI di creazione numero non è nello scope di M2) → compare in `/wa/numeri` con numero MASCHERATO, mai intero.
6. Numero senza `proxy_url` → avviso "proxy mancante" visibile in UI (non solo nei log).
7. Bottone "Avvia login QR" presente SOLO su stato `pending_qr`/`qr_required` (verificare visivamente su un numero in ciascuno stato, senza eseguire davvero il login: apre un browser vero sul server).
8. Bottone "Riattiva" presente SOLO su `retired`/`suspended`; su un numero `retired`, aprire il dialog e provare a inviare con motivo vuoto → bottone submit resta disabilitato.
9. Riattivazione con motivo compilato → numero passa a `pending_qr` in UI, `sent_today`/`warmup_day` azzerati (verificabile a DB).

## Creazione campagna — marketing (10-13)
10. Passo 1: seleziona tipo `marketing` → banner informativo "opt-out si attiva sempre in automatico" visibile, campo CTA precompilato e obbligatorio.
11. Passo 2: upload CSV pulito (5-10 righe valide) → report mostra "N contatti caricati", zero scarti, zero esclusi.
12. Passo 2: upload CSV con almeno 2 righe scartabili (numero malformato) e 1 duplicato → report mostra i contatori corretti (creati, duplicati, scarti con riga+motivo+valore MASCHERATO) e un link "scarica il report scarti".
13. Scarica il report scarti (CSV) → apri il file: nessun numero di telefono completo, solo forma mascherata.

## Creazione campagna — follow-up e template (14-18)
14. Passo 1: seleziona tipo `followup` → nessuna CTA obbligatoria, banner dice che l'opt-out resta disattivato.
15. Passo 3: chip dei segnaposto reali della lista appena caricata (es. `{ultimo_ordine}` se presente nel CSV) cliccabili, si inseriscono nel testo alla posizione del cursore.
16. Salva un template con un placeholder NON coperto dalle colonne del CSV → 422 con la lista dei placeholder ignoti mostrata testuale (non un errore generico).
17. Corregge il placeholder e salva → messaggio "salvato" compare; modifica il testo SENZA ricliccare Salva → l'etichetta torna a "non ancora salvato" (fix Task 11).
18. Bottone "Avvia campagna" disabilitato finché zero contatti o messaggio non salvato, col motivo scritto accanto.

## Ciclo di vita campagna (19-24)
19. Avvia una campagna senza contatti caricati (crearne una apposta, o con lista rimossa) → rifiutata con messaggio del backend leggibile.
20. Avvia una campagna pronta (numero active, contatti, messaggio salvato) → passa a `running`, KPI aggiornati.
21. Pausa → passa a `paused`, i bottoni disponibili cambiano (Riprendi/Stop).
22. Riprendi → torna a `running`.
23. Prova ad avviare una SECONDA campagna sullo stesso numero mentre la prima è `running` → rifiutata con messaggio "questo numero ha già una campagna in corso".
24. Stop → passa a `stopped`, i contatti/KPI restano visibili (nessuna cancellazione).

## Contatti e KPI (25-29)
25. Rimuovi un contatto normale dalla lista → sparisce, `total_contacts` nella UI si aggiorna (decrementato, fix Task 12).
26. Simula un lock fresco su un contatto (script/DB) → bottone rimuovi disabilitato con spiegazione, non un errore dopo il click.
27. KPI di una campagna appena creata (zero inviati) → card mostra 0 ovunque, nessun crash, nessun "NaN%".
28. KPI con `sent`/`opted_out` valorizzati (via script/DB) → tassi calcolati correttamente, nota di onestà del dato visibile.
29. Superata la soglia 5% opt-out (via script/DB) → badge di allarme visibile, nessuna pausa automatica.

## Chiusura (30)
30. Reload finale di tutte le pagine toccate (`/`, `/canale`, `/wa`, `/wa/numeri`, `/wa/campagne/nuova`, `/wa/campagne/[id]`) e delle pagine Instagram esistenti (`/dashboard`, `/campaigns`, `/accounts`, `/leads`, `/messages`, `/ops`, `/settings`) dopo tutti i test: nessun 500, nessun errore console non gestito, nessuna regressione visibile sul lato Instagram.
