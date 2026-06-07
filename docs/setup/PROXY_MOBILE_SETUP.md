# PROXY MOBILE — Setup IP residenziale + mobile per profilo

> Guida sintetica e risolutiva. Obiettivo: far uscire profili diversi da IP diversi, **dallo stesso PC**, senza cambiare la rete dell'OS.

---

## Concetto chiave (risolve il dubbio "PC su due reti")

Il bot instrada il proxy **per-account, a livello applicazione** — NON a livello sistema operativo:

- Browser DM: `launch_kwargs["proxy"] = proxy_cfg` per account → `backend/app/browser/context_manager.py:116`
- Scraping instagrapi: `client.set_proxy(account.proxy)` → `backend/app/api/accounts.py:314`

Quindi il PC resta sul WiFi di casa. Solo i browser/scraping dei profili con proxy impostato escono da un IP diverso. **Non serve collegare il PC a due reti.**

```
PC (codice + tutti i profili)
 ├─ Profilo A1, A2  →  proxy VUOTO          →  esce da WiFi casa (IP residenziale)
 └─ Profilo B1, B2  →  proxy = telefono     →  esce da IP mobile 5G
```

---

## Campo proxy account

Tabella `instagram_accounts.proxy` (String). Formati accettati (`parse_proxy_url` in `context_manager.py:29`):

```
http://host:port
http://user:pass@host:port
socks5://host:port
```

- **Profilo su IP casa**: campo `proxy` VUOTO → traffico esce da IP del PC.
- **Profilo su IP mobile**: campo `proxy` = URL del proxy sul telefono.

---

## REGOLA CRITICA anti-ban

L'account **deve nascere sull'IP da cui lavorerà**. Il primo login va fatto con il proxy già impostato.

Mai loggare un account su IP casa e poi spostarlo su mobile (o viceversa): IG vede lo stesso device/cookie teletrasportarsi di ASN in 1 secondo → challenge/block immediato. (È la causa del blocco già subìto.)

Proxy datacenter economici (Webshare & simili) = ASN datacenter riconosciuto → block. Usare solo IP mobile/residenziale reali.

---

## Opzione consigliata per iniziare: telefono stessa stanza, USB tethering (GRATIS)

Separa le interfacce di rete: USB crea un'interfaccia dedicata, il default route del PC resta WiFi.

### Hardware
- Telefono Android con SIM dati (es. Samsung S22 Ultra).
- Cavo USB-C↔USB-C (ok per S22 Ultra). **Verificare** che la porta USB-C del PC sia dati, non solo ricarica.

### Passi rapidi (overview)
1. Telefono solo su 5G, WiFi OFF.
2. Cavo USB → attiva Tethering USB.
3. Every Proxy: avvia server HTTP su porta 8080.
4. **Windows**: tieni il WiFi come rotta default (sezione sotto), altrimenti TUTTO il PC esce dal telefono.
5. Campo proxy account B = `http://<ip-usb-telefono>:8080`.
6. Primo login account B con proxy già impostato.

---

### A. Configurare Every Proxy sul telefono

1. **Play Store → installa "Every Proxy"** (sviluppatore: dps0340 / "Every Proxy", icona blu).
2. Sul telefono: **WiFi OFF**, **Dati mobili ON** (5G). Verifica di navigare in 5G.
3. Collega il telefono al PC col cavo USB-C.
4. Android: **Impostazioni → Connessioni → Router WiFi e tethering → Tethering USB → ON**.
   (Appare solo col cavo collegato.)
5. Apri **Every Proxy**:
   - Tab **HTTP**: attiva il toggle **HTTP**. Porta default **8080** (lascia così).
   - (Opzionale **SOCKS5**: attiva se preferisci `socks5://`, porta es. 1080. Per IG basta HTTP.)
   - **Authentication**: lascia OFF per iniziare (rete USB è privata PC↔telefono). Se vuoi password, impostala e usa `http://user:pass@ip:8080`.
6. Premi **START**. Every Proxy mostra gli indirizzi su cui ascolta, es:
   ```
   http://192.168.42.129:8080   ← interfaccia USB (questo ci serve)
   http://192.168.x.x:8080      ← eventuale altra interfaccia
   ```
   Annota l'IP che inizia con **`192.168.42.`** (è la subnet standard del tethering USB Android).
7. Lascia Every Proxy aperto/attivo. (Disattiva l'ottimizzazione batteria per l'app così Android non la chiude: Impostazioni → App → Every Proxy → Batteria → Senza restrizioni.)

---

### B. Collegare il PC al tethering SENZA perdere il WiFi (Windows)

Quando attivi il tethering USB, Windows aggiunge una seconda scheda di rete **con il suo gateway**. Di default Windows potrebbe mandarci TUTTO il traffico → anche i profili A e il resto del PC uscirebbero dal telefono. Va forzato il WiFi come **rotta default**; il telefono resta raggiungibile solo come IP locale (per il proxy).

> Perché funziona: per raggiungere `192.168.42.x` Windows usa la **rotta diretta** della scheda USB (sottorete connessa), che NON dipende dal gateway default. Quindi il proxy funziona anche se il default route è il WiFi.

**Passi (PowerShell come Amministratore):**

1. Attiva il tethering USB (sezione A). Compare la scheda "Ethernet" del telefono.
2. Elenca le interfacce e le metriche:
   ```powershell
   Get-NetIPInterface -AddressFamily IPv4 | Sort-Object InterfaceMetric | Format-Table InterfaceAlias, InterfaceMetric, ConnectionState
   ```
3. Identifica l'alias della scheda USB del telefono (di solito `Ethernet` o `Ethernet 2`, stato `Connected`, comparsa col cavo) e quello del WiFi (`Wi-Fi`).
4. Forza il WiFi a metrica bassa (preferito) e la scheda telefono a metrica alta:
   ```powershell
   Set-NetIPInterface -InterfaceAlias "Wi-Fi" -InterfaceMetric 10
   Set-NetIPInterface -InterfaceAlias "Ethernet" -InterfaceMetric 80   # usa l'alias reale del tethering
   ```
   (Metrica più bassa = preferita. Il WiFi vince come default route.)
5. Verifica che la rotta default `0.0.0.0/0` punti al gateway del WiFi:
   ```powershell
   Get-NetRoute -DestinationPrefix 0.0.0.0/0 | Format-Table ifIndex, NextHop, RouteMetric, InterfaceAlias
   ```
   Deve comparire in cima il NextHop del router di casa (es. `192.168.1.1`), non `192.168.42.129`.

**Verifica finale (la prova del nove):**
```powershell
# IP di uscita normale del PC (deve essere quello di casa)
curl.exe https://api.ipify.org
# IP di uscita PASSANDO dal telefono (deve essere l'IP mobile 5G, diverso)
curl.exe --proxy http://192.168.42.129:8080 https://api.ipify.org
```
Se i due IP sono **diversi** → setup corretto: PC su casa, proxy su mobile.
Se sono **uguali** → il telefono ha ancora il WiFi acceso, oppure la rotta default è sbagliata.

**Note Windows:**
- Le metriche si **resettano** scollegando/ricollegando il cavo. Se cambi spesso, rifai il passo 4 o crea uno script.
- Se la scheda USB non compare: cambia porta USB, o reinstalla i driver "Remote NDIS" (Gestione dispositivi).
- Se il PC ha l'IP del telefono come default nonostante la metrica, in ultima istanza puoi rimuovere il gateway della scheda telefono (lasciando solo l'IP della sottorete) — ma di norma la metrica basta.

---

### C. Procedura testata su questo setup (Samsung S22 Ultra + Windows) — RIUSO RAPIDO

Sequenza esatta che ha funzionato (da rifare ogni volta che ricolleghi dopo una pausa):

1. **Collega prima il cavo USB-C↔USB-C.**
2. Telefono: dati mobili 5G ON, WiFi OFF.
3. Telefono: Impostazioni → Connessioni → Router WiFi e tethering → **Tethering USB ON**.
   - ⚠️ Sul S22 il toggle a volte non si attiva al primo tentativo: entra nel menu tethering e attivalo **da dentro le impostazioni**, non dalla notifica rapida.
4. Sul PC compare una nuova scheda **"Ethernet"** nella sezione Rete.
5. ⚠️ **Imposta la scheda Ethernet del telefono come "connessione a consumo" (metered)** in Windows
   (Impostazioni → Rete e Internet → Ethernet → la scheda del telefono → **Connessione a consumo: ON**).
   Su questo setup, **senza metered il tethering non instradava** (la rete mobile è a consumo: senza il flag Windows si comportava male). Con metered ON ha funzionato.
6. In Every Proxy verifica che **`usb0` sia `Up`** (verde). Se è `Down` → il link USB non è stabilito (vedi troubleshooting sotto).
7. Forza il WiFi come rotta default (sezione B passo 4) e fai la verifica IP (sotto).

> Nota sulla sottorete: su questo telefono l'IP del tethering è **`10.88.254.x`** (gateway `10.88.254.212`), NON `192.168.42.x`. Ogni telefono/ROM può usare una sottorete diversa: **leggi sempre l'IP reale dal PC**, non assumerlo.

#### Comandi di verifica (riusabili ogni volta)

```powershell
# 1. Trova l'IP del telefono = Default Gateway della scheda USB/Ethernet del telefono
ipconfig

# 2. IP di uscita NORMALE del PC (deve essere quello di casa / WiFi)
curl.exe https://api.ipify.org
#   → es. 77.39.171.16  (IP residenziale)

# 3. IP di uscita PASSANDO dal telefono (deve essere l'IP mobile 5G, DIVERSO)
curl.exe --proxy http://10.88.254.212:8080 https://api.ipify.org
#   → es. 109.55.253.29  (IP mobile)
```

- I due IP **diversi** = tutto a posto. Il primo conferma che il PC/profili-A restano su casa, il secondo che il proxy esce da mobile.
- I due IP **uguali** = WiFi del telefono acceso, oppure metrica/rotta default sbagliata, oppure `usb0` Down.

#### Troubleshooting `usb0 = Down`
- Cavo solo-ricarica → testa se Windows vede il telefono per trasferire file; se no, cambia cavo.
- Modalità USB: apri la notifica USB sul telefono → consenti dati / Tethering (non "Solo ricarica").
- Driver mancante: in Gestione dispositivi deve esserci una scheda "Remote NDIS" / "Samsung".

---

Risultato: solo i profili B (con `proxy=http://10.88.254.x:8080`) passano dal telefono (IP mobile). Resto del PC e profili A su WiFi casa. Costo: solo la SIM dati.

---

## Cosa succede se il proxy cade mentre il bot lavora (cavo staccato/rotto)

**Risposta breve: NESSUN leak di IP. Il bot va in errore, non passa all'IP del WiFi.**

Perché è sicuro by-design (verificato nel codice):
- **Browser DM (Patchright)**: il proxy è impostato al **launch del browser** (`launch_kwargs["proxy"]` in `context_manager.py:116`). Tutto il traffico di quel browser è vincolato al proxy. Se il proxy non è raggiungibile → Chromium restituisce `ERR_PROXY_CONNECTION_FAILED` → l'invio DM **fallisce con errore**. Non esiste fallback "diretto".
- **Scraping (instagrapi)**: `client.set_proxy(account.proxy)` (`instagrapi_client.py:138`). Le richieste passano dal proxy; se cade → `ProxyError`/`ConnectionError` → la call **fallisce**. Nessun ripiego sull'IP locale.
- Il default route su WiFi vale per il **resto del PC e per i profili-A** (proxy vuoto). I profili-B sono "pinnati" esplicitamente su `10.88.254.212`: se quell'host sparisce, la connessione fallisce e basta — non viene reinstradata sul WiFi.

Inoltre: se il campo proxy è impostato ma **illeggibile/malformato**, il bot **si rifiuta di lanciare il browser** (`context_manager.py:91-96`, solleva `ValueError`) proprio per non rischiare di uscire dall'IP sbagliato.

**Cosa vedi in pratica se stacchi il cavo durante il lavoro:**
1. Le operazioni dell'account-B iniziano a fallire con errori di rete/proxy.
2. Il bot le tratta come errori: l'invio DM va in retry / la campagna può andare in pausa o errore (gestione `consecutive_unexpected_errors` nell'orchestrator).
3. Instagram **non vede mai** un IP diverso per quell'account. Al massimo vede inattività.

**Quindi**: un cavo staccato = lavoro fermo per quell'account, NON un cambio IP sospetto. Ricollega il cavo, rifai metrica + verifica `curl` (sezione C), e riprendi.

> ⚠️ L'unico modo per avere un leak è **rimuovere il proxy dal campo account** mentre l'account ci lavora (o loggare l'account senza proxy). Finché il campo `proxy` resta valorizzato, il peggio che succede è un errore di connessione.

---

## Opzione telefono LONTANO (fuori portata USB/LAN)

Le SIM mobili sono dietro **CGNAT**: nessun IP pubblico in entrata, il PC non raggiunge il telefono direttamente. Serve un **tunnel reverse** (il telefono esce verso un relay, il PC si connette al relay).

| Opzione | Costo | Note |
|---|---|---|
| **iProxy.online** | ~€8-12/porta/mese | tunnel + rotazione IP, zero config rete. Più semplice |
| **Proxidize** | ~€15-25/mese sw | multi-device, self-host |
| **DIY: VPS + frp / reverse SSH** | VPS ~€4/mese | software gratis, telefono apre tunnel verso VPS, PC→VPS→telefono |

---

## Tabella riassuntiva opzioni

| Scenario | Metodo | Costo | Quando |
|---|---|---|---|
| Profilo su IP casa | proxy vuoto | 0 | 1-2 profili sull'IP residenziale del PC |
| Telefono stessa stanza | USB tethering + Every Proxy | 0 (+SIM) | **iniziare qui** |
| Telefono stessa stanza, no USB | 2 schede rete (eth casa + WiFi hotspot) | 0 | solo se USB non possibile |
| Telefono lontano | iProxy.online | ~€8-12/mese | remoto, plug-and-play |
| Telefono lontano, economico | VPS + frp/SSH tunnel | ~€4/mese | smanettone |
| Zero setup | proxy mobile commerciale | €30-80/IP/mese | non si vuole gestire device |

---

## Verificare l'IP dentro il browser del bot

Per controllo visivo definitivo dell'IP di un profilo:

- Apri **Account → "Login Browser"** per quell'account. Quella finestra Chromium usa lo stesso proxy dell'account (`manual_login.py:66` carica `account.proxy`).
- Il proxy è impostato a livello **browser context** (`context_manager.py:116`): **tutta** la finestra (ogni tab, ogni richiesta) passa dall'IP mobile, non solo Instagram.
- Naviga su `https://api.ipify.org` o `https://whatismyipaddress.com` → deve mostrare l'IP mobile (es. `109.55.253.29`), NON quello di casa.
- Protezione leak: il bot inietta uno script che blocca il leak dell'IP LAN via WebRTC (`context_manager.py:468`), altrimenti `RTCPeerConnection` rivelerebbe l'IP reale anche col proxy attivo.

> Un browser **normale** (Chrome/Edge aperto da te) NON passa dal proxy: usa la rotta default del PC = WiFi casa. Solo la Chromium del bot per quell'account è instradata.

---

## Checklist pre-login (evita blocco)

- [ ] Proxy impostato sull'account **prima** del primo login
- [ ] Telefono su dati mobili, WiFi OFF
- [ ] IP di uscita verificato (test con account throwaway prima dell'account vero)
- [ ] Un IP = un account (non condividere lo stesso IP mobile tra più profili)
- [ ] Dopo il login, MAI cambiare il proxy di quell'account

---

## Note

- 1 IP mobile = 1 profilo. Più profili mobili → più device/SIM (gratis scala male: 3 profili mobili = 3 telefoni).
- Il proxy è coinvolto **anche se il telefono è nella stessa stanza**: "proxy" = l'app Every Proxy sul telefono, è il meccanismo che instrada il profilo.
- Per recuperare un account già bloccato: risolvere la challenge dall'app mobile reale, non toccarlo finché non risolto.
