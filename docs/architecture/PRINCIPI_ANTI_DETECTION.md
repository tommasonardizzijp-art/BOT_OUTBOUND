# PRINCIPI ANTI-DETECTION (IMPORTANTE)

Le regole di comportamento del bot e i valori di timing. Estratto da `CLAUDE.md` il 2026-07-29 (contenuto invariato).

> Questo file = **cosa deve fare il bot**. Per l'analisi dei vettori di rilevamento IG, proxy, rischi ban e sicurezza operativa vedi [ANTI_DETECTION.md](ANTI_DETECTION.md).

---

Non modificare il comportamento di timing o simulazione umana senza considerare questi principi:

1. **Mai delay uniformi** — usare sempre distribuzioni log-normali con sigma alto (0.7) per più varianza naturale
2. **Sessioni limitate** — 5-12 DM per sessione (test) / 10-20 (produzione), poi pausa obbligatoria
3. **Finestra oraria** — nessun invio fuori da `ACTIVE_HOURS_START` - `ACTIVE_HOURS_END`
4. **Profili browser persistenti** — ogni account ha un profilo Chromium dedicato, NON aprire in incognito
5. **Warm-up graduale** — account nuovi iniziano con 3-5 DM/giorno e aumentano nel tempo
6. **Deduplicazione obbligatoria** — controllare sempre `global_contacts` prima di inviare
7. **Scroll-to-top prima del click** — dopo `_simulate_browsing`, risalire sempre in cima alla pagina prima di cliccare "Message"
8. **Ordine follower randomizzato** — `ORDER BY func.random()` per non contattare sempre nello stesso ordine
9. **Typing lognormale** — delay per tasto da distribuzione lognormale + pause tra parole (15% prob) + micro-pause rare
10. **IP diversificazione** — con 3+ account è necessario usare proxy distinti (vedi [SCALA_E_PARALLELISMO.md](SCALA_E_PARALLELISMO.md))
11. **Pause sessione vincolanti** — un recap "riparte alle HH:MM" non deve essere aggirato da recovery/reenqueue; prima di riaccodare verificare Redis (`job`, `retry`, `in-progress`) e lease account.
12. **Stories browsing consentito ma reversibile** — mantenere la visita alle storie per naturalezza, ma chiudere sempre il viewer prima dei controlli DM; non cercare input DM dentro il viewer storie.

## Valori timing

| Parametro | Test aggressivo | Produzione consigliata |
|---|---|---|
| `MIN_DELAY_SECONDS` | 10 | 120 |
| `MAX_DELAY_SECONDS` | 45 | 480 |
| `SESSION_MIN_MESSAGES` | 5 | 10 |
| `SESSION_MAX_MESSAGES` | 12 | 20 |
| `SESSION_BREAK_MIN_MINUTES` | 10 | 30 |
| `SESSION_BREAK_MAX_MINUTES` | 25 | 60 |

## Volumi e account

- Instagram limita ~50-100 DM/giorno per account. Non superare mai questo limite, meglio stare su 20-30
- L'account Instagram usato per lo scraping **non deve** necessariamente essere lo stesso che invia i DM
- Raccomandato usare **proxy residenziali** per account con alto volume (non incluso nel MVP)
- **Fase Lista**: con `max_amount=0` instagrapi drena l'intera lista in un burst `count=200` senza delay → challenge IG "comportamento automatizzato". Passare sempre un `max_amount` piccolo (`LIST_PAGE_SIZE_MIN/MAX`) — vedi [../setup/CONFIGURAZIONE.md](../setup/CONFIGURAZIONE.md)

---

Vedi anche: [ANTI_DETECTION.md](ANTI_DETECTION.md) · [BROWSER.md](BROWSER.md) · [../setup/PROXY_MOBILE_SETUP.md](../setup/PROXY_MOBILE_SETUP.md)
