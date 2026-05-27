'use client'

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Separator } from '@/components/ui/separator'
import { BookOpen } from 'lucide-react'

export default function GuidePage() {
  return (
    <div className="space-y-6 max-w-5xl">
      <div>
        <h1 className="text-3xl font-bold text-white flex items-center gap-2">
          <BookOpen className="w-7 h-7 text-purple-400" />
          Guida all&apos;uso
        </h1>
        <p className="text-gray-400 text-base mt-1">Manuale completo del bot di outreach Instagram</p>
      </div>

      {/* Table of contents */}
      <Card className="bg-gray-900 border-gray-800">
        <CardHeader className="pb-2">
          <CardTitle className="text-base text-gray-100">Indice</CardTitle>
        </CardHeader>
        <CardContent>
          <nav className="grid grid-cols-1 sm:grid-cols-2 gap-1">
            {TOC.map((item, i) => (
              <a key={i} href={`#section-${i + 1}`}
                className="text-sm text-purple-400 hover:text-purple-300 py-0.5">
                {i + 1}. {item}
              </a>
            ))}
          </nav>
        </CardContent>
      </Card>

      {/* Section 1 */}
      <Section id="section-1" title="1. Primo avvio">
        <H3>Prerequisiti</H3>
        <ul className="list-disc list-inside space-y-1 text-gray-400 text-sm">
          <li><strong className="text-gray-300">Memurai</strong> in esecuzione (servizio Windows, parte automaticamente)</li>
          <li><strong className="text-gray-300">Ollama</strong> in esecuzione con modello scaricato (<Code>ollama pull llama3.2</Code>)</li>
          <li><strong className="text-gray-300">Patchright</strong> installato (<Code>pip install patchright && patchright install chromium</Code>)</li>
        </ul>

        <H3>Avvio</H3>
        <CodeBlock>Doppio click su start.bat</CodeBlock>
        <p className="text-sm text-gray-400 mt-2">Si aprono 3 finestre:</p>
        <ul className="list-disc list-inside space-y-1 text-gray-400 text-sm">
          <li><strong className="text-gray-300">Backend</strong> &mdash; API FastAPI su <Code>http://localhost:8000</Code></li>
          <li><strong className="text-gray-300">Worker</strong> &mdash; ARQ che gestisce i task in background</li>
          <li><strong className="text-gray-300">Frontend</strong> &mdash; Dashboard su <Code>http://localhost:3000</Code></li>
        </ul>

        <H3>Verifica sistema</H3>
        <p className="text-sm text-gray-400">
          Apri <Code>http://localhost:8000/api/health</Code> &mdash; deve rispondere:
        </p>
        <CodeBlock>{`{"status":"ok","ollama":"ok","redis":"ok","database":"ok"}`}</CodeBlock>
      </Section>

      {/* Section 2 */}
      <Section id="section-2" title="2. Gestione Account Instagram">
        <H3>Aggiungere un account</H3>
        <ol className="list-decimal list-inside space-y-1 text-gray-400 text-sm">
          <li>Vai su <strong className="text-gray-300">Account</strong> nel menu laterale</li>
          <li>Clicca il bottone viola <strong className="text-gray-300">Aggiungi account</strong></li>
          <li>Inserisci: Username, Password, Proxy (opzionale), Limite DM/giorno</li>
          <li>Clicca <strong className="text-gray-300">Aggiungi account</strong></li>
        </ol>
        <Callout>L&apos;account viene salvato ma NON &egrave; ancora loggato. Devi fare il login prima di poter usarlo.</Callout>

        <Separator className="my-4 bg-gray-800" />

        <H3>Login &mdash; Come funziona (IMPORTANTE)</H3>
        <Table headers={['Bottone', 'Cosa fa', 'Rischio']} rows={[
          ['Login Browser (verde)', 'Apre un browser, fai login tu manualmente', 'Nessuno'],
          ['Login API (giallo)', 'Login automatico via API instagrapi', 'Alto (possibile ban IP)'],
        ]} />

        <H4>Metodo consigliato: Login Browser</H4>
        <ol className="list-decimal list-inside space-y-2 text-gray-400 text-sm">
          <li>Clicca il bottone verde <strong className="text-gray-300">&quot;Login Browser&quot;</strong></li>
          <li>Si apre un browser Chromium sul tuo schermo con la pagina di login Instagram</li>
          <li>Fai il login normalmente (username, password, 2FA se richiesto)</li>
          <li>Appena il login &egrave; completato, il browser si chiude automaticamente (~3 sec)</li>
          <li>Il bot verifica la sessione e salva i cookie</li>
          <li>L&apos;account &egrave; ora <Code>active</Code> &mdash; pronto per le campagne</li>
        </ol>
        <Callout>Hai 5 minuti per completare il login. Il browser NON &egrave; il tuo browser personale &mdash; &egrave; isolato con un profilo dedicato.</Callout>

        <H4>Perch&eacute; NON usare &quot;Login API&quot;</H4>
        <ul className="list-disc list-inside space-y-1 text-gray-400 text-sm">
          <li>Instagram rileva facilmente i login automatici</li>
          <li>Pu&ograve; bloccare il tuo IP (anche per 24-48 ore)</li>
          <li>Pu&ograve; bloccare l&apos;account stesso</li>
        </ul>

        <Separator className="my-4 bg-gray-800" />

        <H3>Sessione scaduta</H3>
        <p className="text-sm text-gray-400">
          Se la sessione scade, clicca di nuovo &quot;Login Browser&quot; e rifai il login. I cookie verranno aggiornati.
        </p>

        <Separator className="my-4 bg-gray-800" />

        <H3>Stati account</H3>
        <Table headers={['Stato', 'Significato']} rows={[
          ['active', 'Pronto per inviare DM'],
          ['warming_up', 'Account nuovo in fase di warm-up (limiti ridotti)'],
          ['cooldown', 'Temporaneamente fermo dopo rate limit'],
          ['challenge_required', 'Instagram ha chiesto verifica email/telefono'],
          ['banned', 'Account bannato da Instagram'],
          ['disabled', 'Disabilitato manualmente'],
        ]} />

        <H3>Gestire un challenge</H3>
        <ol className="list-decimal list-inside space-y-1 text-gray-400 text-sm">
          <li>Apri l&apos;email/telefono associato all&apos;account Instagram</li>
          <li>Prendi il codice di verifica</li>
          <li>Nella sezione Account clicca <strong className="text-gray-300">Inserisci codice challenge</strong></li>
          <li>L&apos;account torna in stato <Code>active</Code></li>
        </ol>

        <H3>Consigli</H3>
        <ul className="list-disc list-inside space-y-1 text-gray-400 text-sm">
          <li>Usa sempre account <strong className="text-gray-300">secondari/dedicati</strong>, mai il tuo account principale</li>
          <li>Limite consigliato: <strong className="text-gray-300">20-30 DM/giorno</strong> per account</li>
          <li>Puoi aggiungere pi&ugrave; account &mdash; il bot li ruota automaticamente</li>
          <li>Fai sempre il Login Browser prima di avviare qualsiasi campagna</li>
        </ul>
      </Section>

      {/* Section 3 */}
      <Section id="section-3" title="3. Creare una Campagna">
        <ol className="list-decimal list-inside space-y-1 text-gray-400 text-sm">
          <li>Vai su <strong className="text-gray-300">Campagne</strong> &rarr; <strong className="text-gray-300">Nuova campagna</strong></li>
          <li>Compila i campi (vedi tabella sotto)</li>
          <li>Clicca <strong className="text-gray-300">Crea campagna</strong></li>
          <li>Clicca <strong className="text-gray-300">Avvia scraping</strong> per raccogliere i follower</li>
          <li>Quando lo scraping &egrave; completo &rarr; clicca <strong className="text-gray-300">Avvia</strong></li>
        </ol>
        <Table headers={['Campo', 'Descrizione', 'Esempio']} rows={[
          ['Nome campagna', 'Nome interno per identificarla', 'Campagna Aprile — @fitness_roma'],
          ['Pagina target', 'Username IG da cui scrapare i follower', 'fitness_roma (senza @)'],
          ['Template messaggio', 'Il testo base del DM (vedi sez. 4)', 'Ciao {nome}, ho visto che segui...'],
          ['Contesto AI', 'Istruzioni aggiuntive per l\'AI (vedi sez. 5)', 'Tono informale, max 3 righe'],
        ]} />
      </Section>

      {/* Section 4 */}
      <Section id="section-4" title="4. Template Messaggio — Variabili disponibili">
        <p className="text-sm text-gray-400">
          Il template &egrave; il <strong className="text-gray-300">punto di partenza</strong> che l&apos;AI usa per generare il messaggio personalizzato.
          Non &egrave; un template rigido: l&apos;AI lo rielabora tenendo conto di nome e bio del destinatario.
        </p>
        <Table headers={['Variabile', 'Cosa inserisce', 'Note']} rows={[
          ['{nome}', 'Nome reale del profilo (es. "Marco")', 'Se non disponibile, usa @username'],
          ['{name}', 'Identico a {nome} (alias inglese)', 'Entrambi funzionano'],
        ]} />
        <H3>Come funziona l&apos;AI</H3>
        <ol className="list-decimal list-inside space-y-1 text-gray-400 text-sm">
          <li>Riceve il tuo template come &quot;linea guida&quot;</li>
          <li>Legge username, nome completo e bio Instagram del destinatario</li>
          <li>Genera un messaggio unico che segue il tuo intento ma suona naturale</li>
          <li>Se la generazione fallisce &rarr; usa il template con <Code>{'{nome}'}</Code> sostituito</li>
        </ol>
      </Section>

      {/* Section 5 */}
      <Section id="section-5" title="5. Contesto AI — Come usarlo bene">
        <p className="text-sm text-gray-400 mb-3">
          Il campo <strong className="text-gray-300">Contesto AI</strong> &egrave; opzionale ma potente. Serve a dare istruzioni aggiuntive al modello.
        </p>
        <H3>Cosa puoi specificare</H3>
        <div className="space-y-2">
          <p className="text-sm text-gray-300 font-medium">Tono:</p>
          <CodeBlock>Tono informale e diretto, come se fosse un amico che scrive.</CodeBlock>
          <p className="text-sm text-gray-300 font-medium">Lunghezza:</p>
          <CodeBlock>Massimo 2 frasi, breve e conciso.</CodeBlock>
          <p className="text-sm text-gray-300 font-medium">Cosa evitare:</p>
          <CodeBlock>Non menzionare prezzi o offerte. Non sembrare commerciale.</CodeBlock>
          <p className="text-sm text-gray-300 font-medium">Settore/contesto:</p>
          <CodeBlock>{`Siamo un'agenzia di marketing per ristoranti.\nL'obiettivo è offrire una consulenza gratuita.`}</CodeBlock>
        </div>
        <H3>Combinazione consigliata</H3>
        <CodeBlock>{`Tono informale e curioso. Max 3 righe.\nFai riferimento alla bio se contiene qualcosa di interessante.\nNon iniziare mai con "Ciao!".\nScrivi in italiano.`}</CodeBlock>
      </Section>

      {/* Section 6 */}
      <Section id="section-6" title="6. Esempi pratici di template">
        <TemplateExample
          title="Agenzia marketing → ristoranti"
          template={`Ciao {nome}, ho visto che sei nel settore food —\nsto aiutando alcuni ristoranti a crescere su Instagram\nsenza spendere in ads. Ti farebbe piacere saperne di più?`}
          context={`Tono professionale ma amichevole. Max 3 righe.\nSe la bio menziona un tipo di cucina o un locale specifico, citalo.\nNon iniziare con "Ciao!". Scrivi in italiano.`}
        />
        <TemplateExample
          title="Personal trainer → potenziali clienti"
          template={`{nome} ho visto il tuo profilo —\nsegui già un programma di allenamento\no stai cercando qualcosa di nuovo?`}
          context={`Tono curioso e diretto, come un trainer che fa una domanda genuina.\nMax 2 frasi. Non menzionare prezzi. Scrivi in italiano.`}
        />
        <TemplateExample
          title="E-commerce → clienti competitor"
          template={`Ciao {nome}! Ho visto che ti piace [settore] —\nho qualcosa che potrebbe interessarti,\nposso mandarti i dettagli?`}
          context={`Molto breve, massimo 2 righe. Tono informale.\nCrea curiosità senza svelare subito il prodotto.\nSe la bio dice dove vive o lavora, menzionalo naturalmente.`}
        />
        <TemplateExample
          title="Networking B2B"
          template={`{nome} lavori nel [settore]?\nSto connettendo professionisti del settore\nper uno scambio di idee — ti va di fare una chiacchierata?`}
          context={`Tono professionale ma non formale.\nSe la bio indica il ruolo o l'azienda, usalo per personalizzare.\nMax 2-3 frasi. Scrivi in italiano.`}
        />
      </Section>

      {/* Section 7 */}
      <Section id="section-7" title="7. Ciclo di vita di una campagna">
        <CodeBlock>draft → scraping → ready → running → paused/completed/error</CodeBlock>
        <Table headers={['Stato', 'Significato', 'Azioni disponibili']} rows={[
          ['draft', 'Appena creata', 'Avvia scraping'],
          ['scraping', 'Raccolta follower in corso', '— (automatico)'],
          ['ready', 'Follower raccolti, pronti per invio', 'Avvia'],
          ['running', 'Invio DM in corso', 'Pausa, Stop'],
          ['paused', 'Messa in pausa manualmente', 'Riprendi, Stop'],
          ['completed', 'Tutti i DM inviati', 'Reset'],
          ['error', 'Errore critico', 'Reset'],
        ]} />

        <H3>Stati dei follower</H3>
        <Table headers={['Stato', 'Significato']} rows={[
          ['pending', 'Follower trovato, bio non ancora scaricata'],
          ['bio_scraped', 'Bio scaricata, messaggio non ancora generato'],
          ['message_generated', 'Messaggio AI pronto, non ancora inviato'],
          ['sent', 'DM inviato con successo'],
          ['failed', 'Invio fallito (ritentabile)'],
          ['skipped', 'Saltato (già contattato o saltato manualmente)'],
          ['replied', 'Ha già risposto'],
        ]} />
      </Section>

      {/* Section 8 */}
      <Section id="section-8" title="8. Parametri di configurazione (.env)">
        <p className="text-sm text-gray-400 mb-3">
          File <Code>.env</Code> nella root del progetto. Modificalo con un editor di testo.
        </p>

        <H3>Timing invio DM</H3>
        <Table headers={['Parametro', 'Default', 'Descrizione']} rows={[
          ['MIN_DELAY_SECONDS', '120', 'Pausa minima tra un DM e l\'altro (2 min)'],
          ['MAX_DELAY_SECONDS', '480', 'Pausa massima tra un DM e l\'altro (8 min)'],
          ['SESSION_MIN_MESSAGES', '10', 'Minimo DM per sessione prima della pausa'],
          ['SESSION_MAX_MESSAGES', '20', 'Massimo DM per sessione prima della pausa'],
          ['SESSION_BREAK_MIN_MINUTES', '30', 'Pausa minima tra sessioni'],
          ['SESSION_BREAK_MAX_MINUTES', '60', 'Pausa massima tra sessioni'],
        ]} />

        <H3>Orario attivo</H3>
        <Table headers={['Parametro', 'Default', 'Descrizione']} rows={[
          ['ACTIVE_HOURS_START', '8', 'Ora di inizio invio (8:00 UTC)'],
          ['ACTIVE_HOURS_END', '23', 'Ora di fine invio (23:00 UTC)'],
        ]} />
        <Callout>Gli orari sono in UTC. Se sei in Italia (UTC+2 in estate), ACTIVE_HOURS_START=8 corrisponde alle 10:00 ora italiana.</Callout>

        <H3>Account e browser</H3>
        <Table headers={['Parametro', 'Default', 'Descrizione']} rows={[
          ['DEFAULT_DAILY_LIMIT', '20', 'Max DM per account per giorno'],
          ['WARMUP_ENABLED', 'true', 'Abilita il protocollo warm-up per nuovi account'],
          ['MAX_CONCURRENT_BROWSERS', '3', 'Max browser Patchright aperti contemporaneamente'],
          ['HEADLESS', 'true', 'false = vedi il browser durante l\'invio (debug)'],
        ]} />

        <H3>AI (Ollama)</H3>
        <Table headers={['Parametro', 'Default', 'Descrizione']} rows={[
          ['OLLAMA_MODEL', 'llama3.2', 'Modello da usare. Alternative: llama3.2:1b, mistral'],
          ['OLLAMA_BASE_URL', 'http://localhost:11434', 'URL del server Ollama'],
        ]} />
      </Section>

      {/* Section 9 */}
      <Section id="section-9" title="9. Warm-up account">
        <p className="text-sm text-gray-400 mb-3">
          Un account nuovo viene automaticamente messo in stato <Code>warming_up</Code>.
          Il bot rispetta questi limiti progressivi:
        </p>
        <Table headers={['Giorni', 'DM/giorno massimi']} rows={[
          ['Giorni 1-3', '5 DM/giorno'],
          ['Giorni 4-7', '12 DM/giorno'],
          ['Giorni 8-14', '20 DM/giorno'],
          ['Giorno 15+', 'Limite normale (da .env)'],
        ]} />
        <p className="text-sm text-gray-400 mt-2">
          Il contatore avanza automaticamente ogni giorno. Non c&apos;&egrave; bisogno di fare nulla.
        </p>
      </Section>

      {/* Section 10 */}
      <Section id="section-10" title="10. Sistema anti-ban — funzionalità complete">
        <p className="text-sm text-gray-400 mb-3">
          Tutte le protezioni sono implementate automaticamente nel codice. <strong className="text-red-400">Non modificare i parametri di timing senza capire le conseguenze.</strong>
        </p>

        <H3>Timing e ritardi</H3>
        <Table headers={['Funzionalità', 'Come funziona', 'Perché conta']} rows={[
          ['Delay lognormale tra DM', 'Ogni pausa tra un DM e il successivo segue una distribuzione log-normale (non uniforme). La varianza è alta — a volte 2 min, a volte 7 min.', 'Instagram rileva pattern uniformi. La lognormale imita il comportamento umano reale.'],
          ['Typing lognormale', 'Ogni carattere del messaggio viene digitato con un delay da distribuzione log-normale. Pause extra tra parole (15% prob) e micro-pause rare.', 'Il "paste" immediato di un intero messaggio è rilevabile. Il typing simulato è indistinguibile da un utente reale.'],
          ['Delay scraping bio 3-8s', 'Tra ogni chiamata user_info() durante lo scraping: attesa da distribuzione log-normale con mediana ~4s, range 3-8s.', '50 chiamate API consecutive senza pausa è il pattern più riconoscibile da Instagram.'],
          ['Pausa lunga ogni 200 follower', 'Dopo ogni 200 follower scrappati: pausa aggiuntiva 30-60s.', 'Rallenta il ritmo delle chiamate API su sessioni di scraping lunghe.'],
        ]} />

        <H3>Sessioni e browser</H3>
        <Table headers={['Funzionalità', 'Come funziona', 'Perché conta']} rows={[
          ['Sessioni limitate', '10-20 DM per sessione, poi pausa obbligatoria 30-60 min. I valori sono randomizzati ad ogni sessione.', 'Inviare DM ininterrottamente per ore è un segnale di bot inequivocabile.'],
          ['Pause interrompibili', 'Le pause tra sessioni controllano lo stato ogni 5s. Se la campagna viene messa in pausa durante l\'attesa, il browser viene rilasciato immediatamente.', 'Evita che il sistema resti bloccato in una pausa lunga dopo un comando stop/pausa.'],
          ['Profilo browser persistente', 'Ogni account ha il suo profilo Chromium dedicato in browser_profiles/. Lo stesso profilo viene riusato ad ogni sessione.', 'Un profilo nuovo ad ogni sessione (incognito) è il comportamento tipico dei bot. Un profilo che accumula storia, cookie e cache è quello di un utente reale.'],
          ['Fingerprint deterministico', 'User-agent, viewport, timezone (Europe/Rome), locale (it-IT) e canvas fingerprint sono stabili e unici per ogni account.', 'Cambiare UA o viewport tra sessioni è un segnale di rilevamento. Ogni "persona" ha sempre le stesse caratteristiche del dispositivo.'],
          ['Navigazione pre-DM', 'Prima di cliccare "Message": scroll randomizzato (4 pattern: piccolo, grande, pausa lettura, hover elementi), poi risale in cima. Durata media ~12s lognormale.', 'Atterrare su un profilo e cliccare subito "Message" senza guardare nulla è behavior di bot. La simulazione di lettura/scroll rende la sequenza umana.'],
        ]} />

        <H3>Account e deduplicazione</H3>
        <Table headers={['Funzionalità', 'Come funziona', 'Perché conta']} rows={[
          ['Warm-up graduale', 'Account nuovi: 5 DM/giorno nei primi 3 giorni, 12 nei giorni 4-7, 20 nei giorni 8-14, poi limite normale. Avanza automaticamente ogni giorno.', 'Account nuovi che inviano subito 30 DM/giorno vengono bannati rapidamente. Il warm-up "costruisce" una storia di utilizzo normale.'],
          ['Deduplicazione cross-campagna', 'Database globale dei contatti (global_contacts). Prima di ogni DM: check se l\'utente è già stato contattato da qualsiasi campagna precedente.', 'Ricevere lo stesso DM da più campagne dello stesso progetto è un segnale forte che porta a segnalazioni utente.'],
          ['Ordine follower randomizzato', 'I follower vengono selezionati in ordine casuale, mai alfabetico o cronologico.', 'Contattare sempre i follower nello stesso ordine (es. i primi 50 ogni giorno) crea un pattern rilevabile.'],
          ['Rotazione account su 429', 'Se l\'account usato per lo scraping bio riceve un errore 429, il sistema passa automaticamente a un account alternativo attivo e riprende.', 'Senza rotazione, un singolo 429 durante lo scraping ferma l\'intero job. Con la rotazione, il processo continua su un IP diverso.'],
          ['Mutex login per-account', 'Non è possibile fare due login instagrapi simultanei sullo stesso account (ex: scraping + reply checker contemporaneamente).', 'Login concorrenti sullo stesso account possono triggerare challenge di sicurezza Instagram.'],
        ]} />

        <H3>Segnali di rischio da monitorare</H3>
        <ul className="list-disc list-inside space-y-1.5 text-gray-400 text-sm">
          <li>Account spesso in <Code>cooldown</Code> → riduci <Code>DEFAULT_DAILY_LIMIT</Code> a 15-20</li>
          <li>Account in <Code>challenge_required</Code> ripetuto → aggiungici un proxy residenziale/mobile</li>
          <li>Account <Code>banned</Code> → non recuperabile automaticamente. Usa account di riserva.</li>
          <li>DM inviati ma non consegnati (shadow ban) → non rilevato automaticamente. Verifica manualmente che i messaggi arrivino.</li>
        </ul>

        <H3>Limiti consigliati (produzione)</H3>
        <Table headers={['Parametro', 'Valore test', 'Valore produzione', 'Motivo']} rows={[
          ['MIN_DELAY_SECONDS', '10s', '120s (2 min)', 'Sotto i 60s Instagram lo rileva come automatizzato'],
          ['MAX_DELAY_SECONDS', '45s', '480s (8 min)', 'Alta varianza = meno prevedibile'],
          ['SESSION_MIN_MESSAGES', '5', '10', 'Sessioni troppo corte non simulano comportamento reale'],
          ['DEFAULT_DAILY_LIMIT', '—', '20-25 DM', 'Limite sicuro per account in warm-up completato'],
        ]} />
      </Section>

      {/* Section 11 */}
      <Section id="section-11" title="11. Troubleshooting">
        <TroubleshootItem title="Il backend non parte">
          <p>Controlla che il venv sia attivato:</p>
          <CodeBlock>{`cd backend\nvenv\\Scripts\\activate\nuvicorn app.main:app --reload --port 8000`}</CodeBlock>
          <p>Leggi l&apos;errore nella finestra CMD del backend.</p>
        </TroubleshootItem>

        <TroubleshootItem title='Health check mostra "ollama: error"'>
          <ul className="list-disc list-inside space-y-1">
            <li>Verifica che Ollama sia in esecuzione: cerca l&apos;icona nella tray bar</li>
            <li>Oppure esegui: <Code>ollama serve</Code></li>
            <li>Verifica che il modello sia scaricato: <Code>ollama list</Code></li>
          </ul>
        </TroubleshootItem>

        <TroubleshootItem title='Health check mostra "redis: error"'>
          <ul className="list-disc list-inside space-y-1">
            <li>Apri <strong className="text-gray-300">Servizi Windows</strong> e verifica che <strong className="text-gray-300">Memurai</strong> sia &quot;In esecuzione&quot;</li>
            <li>Tasto destro su Memurai &rarr; Avvia</li>
          </ul>
        </TroubleshootItem>

        <TroubleshootItem title="Lo scraping si blocca o restituisce 0 follower">
          <ul className="list-disc list-inside space-y-1">
            <li>L&apos;account usato per lo scraping potrebbe essere limitato</li>
            <li>Prova con un account diverso</li>
            <li>La pagina target potrebbe avere i follower privati</li>
          </ul>
        </TroubleshootItem>

        <TroubleshootItem title="I DM non vengono inviati">
          <ol className="list-decimal list-inside space-y-1">
            <li>Verifica che ci sia almeno un account in stato <Code>active</Code></li>
            <li>Verifica che l&apos;orario sia dentro <Code>ACTIVE_HOURS_START</Code> e <Code>ACTIVE_HOURS_END</Code></li>
            <li>Controlla i log nella sezione <strong className="text-gray-300">Messaggi</strong></li>
            <li>Prova con <Code>HEADLESS=false</Code> nel <Code>.env</Code> per vedere il browser</li>
          </ol>
        </TroubleshootItem>

        <TroubleshootItem title="Errore challenge su account">
          <ol className="list-decimal list-inside space-y-1">
            <li>Vai su <strong className="text-gray-300">Account</strong> nella dashboard</li>
            <li>Trova l&apos;account in stato <Code>challenge_required</Code></li>
            <li>Controlla email/telefono dell&apos;account per il codice Instagram</li>
            <li>Clicca <strong className="text-gray-300">Inserisci codice challenge</strong> e inseriscilo</li>
          </ol>
        </TroubleshootItem>
      </Section>
      {/* Section 12 */}
      <Section id="section-12" title="12. Azioni rischiose — cosa NON fare">
        <p className="text-sm text-gray-400 mb-1">
          Alcune combinazioni di azioni possono causare comportamenti inattesi. Nessuna causa perdita permanente di dati, ma può richiedere un reset manuale.
        </p>

        <H3>Tabella rischi</H3>
        <Table headers={['Azione', 'Rischio', 'Cosa succede', 'Come evitarlo']} rows={[
          [
            'Eliminare account con campagna running',
            '⚠️ Alto',
            'Il worker continua a cercare il browser dell\'account eliminato → crash del worker, stale lock non rilasciato',
            'Prima fai Pausa o Stop sulla campagna, aspetta che si fermi, poi elimina l\'account',
          ],
          [
            'Reset campagna mentre lo scraping è in corso',
            '⚠️ Medio',
            'Scraper e reset corrono in parallelo → follower aggiunti e cancellati contemporaneamente → contatori disallineati',
            'Aspetta il completamento dello scraping oppure fermalo prima di fare reset',
          ],
          [
            'Rimuovere account da campagna running',
            '⚠️ Medio',
            'Il worker già avviato per quell\'account continua a girare. Il lock non viene rilasciato fino al timeout (20 min)',
            'Metti in pausa la campagna prima di modificare gli account assegnati, poi riprendi',
          ],
          [
            'Aggiungere account a campagna running',
            '⚠️ Basso',
            'Il nuovo account non viene rilevato dai worker già in esecuzione',
            'Pausa → aggiungi account → Riprendi: i nuovi worker vengono avviati alla ripresa',
          ],
          [
            'Modificare .env mentre il worker gira',
            '⚠️ Basso',
            'Il worker usa i valori caricati all\'avvio. Le modifiche al .env non hanno effetto finché non riavvii',
            'Riavvia sempre il worker dopo ogni modifica al file .env',
          ],
          [
            'Login API con IP residenziale (senza proxy)',
            '🔴 Critico',
            'Instagram rileva il login automatico e può bloccare l\'IP per 24-48h o l\'account permanentemente',
            'Usa SEMPRE Login Browser. Il Login API è solo per ambienti con proxy residenziale dedicato',
          ],
          [
            'Superare 30 DM/giorno per account',
            '🔴 Critico',
            'Account messo in cooldown automatico da Instagram, possibile ban permanente',
            'Mantieni DEFAULT_DAILY_LIMIT a 20-25 massimo. In warm-up il sistema rispetta già limiti più bassi',
          ],
          [
            'Campagne parallele sullo stesso account',
            '⚠️ Medio',
            'Un account assegnato a due campagne diverse invia DM in serie (non in parallelo) → la velocità non raddoppia. Il browser è mutex.',
            'Per velocità reale usa account distinti su campagne distinte',
          ],
        ]} />

        <H3>Sequenza corretta per operazioni comuni</H3>

        <H4>Sostituire un account su una campagna attiva</H4>
        <ol className="list-decimal list-inside space-y-1 text-gray-400 text-sm">
          <li>Clicca <strong className="text-gray-300">Pausa</strong> sulla campagna</li>
          <li>Aspetta che lo stato diventi <Code>paused</Code></li>
          <li>Vai nella sezione <strong className="text-gray-300">Account assegnati</strong> della campagna</li>
          <li>Rimuovi il vecchio account, aggiungi il nuovo</li>
          <li>Clicca <strong className="text-gray-300">Riprendi</strong></li>
        </ol>

        <H4>Modificare i parametri di timing in produzione</H4>
        <ol className="list-decimal list-inside space-y-1 text-gray-400 text-sm">
          <li>Metti in pausa tutte le campagne attive</li>
          <li>Modifica il file <Code>.env</Code></li>
          <li>Riavvia il processo worker ARQ (<Code>arq app.workers.task_queue.WorkerSettings</Code>)</li>
          <li>Riprendi le campagne</li>
        </ol>

        <H4>Reset completo dopo un blocco</H4>
        <ol className="list-decimal list-inside space-y-1 text-gray-400 text-sm">
          <li>Se una campagna è in <Code>error</Code> o bloccata: clicca <strong className="text-gray-300">Reset</strong></li>
          <li>Il reset cancella tutti i messaggi e riporta i follower a stato iniziale</li>
          <li>Riassegna gli account, poi riavvia lo scraping</li>
          <li>Se il problema era un account in challenge/ban: sostituisci l&apos;account prima di riavviare</li>
        </ol>

        <DangerCallout>
          <strong>Regola d&apos;oro:</strong> Prima di qualsiasi modifica strutturale (account, template, configurazione), metti sempre in pausa la campagna e aspetta la conferma dello stato <Code className="text-red-300">paused</Code> nella dashboard.
        </DangerCallout>
      </Section>
      <Section id="section-13" title="13. Telegram BotFather e comandi admin">
        <H3>Setup notifiche e comandi</H3>
        <ol className="list-decimal list-inside space-y-1 text-gray-400 text-sm">
          <li>Apri Telegram e scrivi a <Code>@BotFather</Code></li>
          <li>Invia <Code>/newbot</Code>, scegli nome e username del bot</li>
          <li>Copia il token in <Code>TELEGRAM_BOT_TOKEN</Code></li>
          <li>Scrivi un messaggio al bot appena creato</li>
          <li>Recupera il chat id con <Code>@userinfobot</Code> e impostalo in <Code>TELEGRAM_CHAT_ID</Code></li>
          <li>Riavvia backend e worker ARQ</li>
        </ol>
        <H3>Comandi disponibili</H3>
        <Table headers={['Comando', 'Effetto']} rows={[
          ['/status', 'Mostra kill-switch, campagne running/paused e account attivi'],
          ['/pause', 'Mostra le campagne attive e permette di scegliere quale mettere in pausa'],
          ['/resume', 'Mostra le campagne in pausa e permette di scegliere quale far ripartire'],
          ['/halt motivo', 'Attiva il kill-switch globale di emergenza'],
          ['/unhalt', 'Disattiva il kill-switch globale e riaccoda il lavoro ancora attivo'],
          ['/logs', 'Mostra gli ultimi activity log'],
          ['/anomalies', 'Mostra le anomalie delle ultime 24 ore'],
        ]} />
        <Callout>I comandi sono accettati solo dal TELEGRAM_CHAT_ID configurato. Se token o chat id sono vuoti, Telegram resta disattivato senza bloccare il bot.</Callout>
      </Section>
    </div>
  )
}

/* ---------- Reusable components ---------- */

const TOC = [
  'Primo avvio',
  'Gestione Account Instagram',
  'Creare una Campagna',
  'Template Messaggio',
  'Contesto AI',
  'Esempi pratici di template',
  'Ciclo di vita di una campagna',
  'Parametri di configurazione (.env)',
  'Warm-up account',
  'Sistema anti-ban — funzionalità complete',
  'Troubleshooting',
  'Azioni rischiose — cosa NON fare',
  'Telegram BotFather e comandi admin',
]

function Section({ id, title, children }: { id: string; title: string; children: React.ReactNode }) {
  return (
    <Card id={id} className="bg-gray-900 border-gray-800 scroll-mt-4">
      <CardHeader className="pb-2">
        <CardTitle className="text-lg text-gray-100">{title}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 text-gray-400 text-sm">{children}</CardContent>
    </Card>
  )
}

function H3({ children }: { children: React.ReactNode }) {
  return <h3 className="font-semibold text-gray-200 text-sm pt-2">{children}</h3>
}

function H4({ children }: { children: React.ReactNode }) {
  return <h4 className="font-medium text-gray-300 text-sm pt-1">{children}</h4>
}

function Code({ children, className }: { children: React.ReactNode; className?: string }) {
  return <code className={`bg-gray-800 px-1.5 py-0.5 rounded text-xs font-mono ${className ?? 'text-gray-300'}`}>{children}</code>
}

function CodeBlock({ children }: { children: React.ReactNode }) {
  return (
    <pre className="bg-gray-800 rounded-lg px-3 py-2 text-gray-300 text-xs font-mono overflow-x-auto whitespace-pre-wrap">
      {children}
    </pre>
  )
}

function Callout({ children }: { children: React.ReactNode }) {
  return (
    <div className="bg-yellow-900/20 border border-yellow-800/50 rounded-lg px-3 py-2 text-sm text-yellow-300/80">
      {children}
    </div>
  )
}

function DangerCallout({ children }: { children: React.ReactNode }) {
  return (
    <div className="bg-red-900/20 border border-red-800/50 rounded-lg px-3 py-2 text-sm text-red-300/80 mt-3">
      {children}
    </div>
  )
}

function Table({ headers, rows }: { headers: string[]; rows: string[][] }) {
  return (
    <div className="overflow-x-auto my-2">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-gray-800">
            {headers.map((h, i) => (
              <th key={i} className="text-left py-2 px-2 text-gray-300 font-medium text-xs">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} className="border-b border-gray-800/50">
              {row.map((cell, j) => (
                <td key={j} className="py-1.5 px-2 text-gray-400 text-xs">{cell}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function TemplateExample({ title, template, context }: { title: string; template: string; context: string }) {
  return (
    <div className="space-y-2 mb-4">
      <H3>{title}</H3>
      <p className="text-xs text-gray-500 font-medium">Template:</p>
      <CodeBlock>{template}</CodeBlock>
      <p className="text-xs text-gray-500 font-medium">Contesto AI:</p>
      <CodeBlock>{context}</CodeBlock>
    </div>
  )
}

function TroubleshootItem({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="space-y-2 mb-4">
      <H3>{title}</H3>
      <div className="text-sm text-gray-400 space-y-1">{children}</div>
      <Separator className="bg-gray-800" />
    </div>
  )
}
