from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=["../.env", ".env"],  # look in project root first, then cwd
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Database
    database_url: str = "sqlite+aiosqlite:///./data/bot.db"

    # Security
    secret_key: str = ""

    @field_validator("secret_key")
    @classmethod
    def validate_secret_key(cls, v: str) -> str:
        if not v:
            raise ValueError(
                "SECRET_KEY non impostato nel file .env. "
                'Genera una chiave con: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
            )
        try:
            from cryptography.fernet import Fernet
            Fernet(v.encode())
        except Exception:
            raise ValueError("SECRET_KEY non è una chiave Fernet valida (32 byte url-safe base64)")
        return v

    # CORS
    cors_origins: str = "http://localhost:3000"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",")]

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Ollama (legacy, used when ai_provider=ollama)
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2"
    ollama_timeout_seconds: int = 90  # httpx timeout per generate request

    # AI provider — ollama | groq | gemini
    # groq:  free tier, OpenAI-compatible. Default model: llama-3.3-70b-versatile
    # gemini: Google AI Studio free tier. Default model: gemini-2.0-flash
    ai_provider: str = "ollama"
    ai_api_key: str = ""
    # If empty, uses provider default (Groq → llama-3.3-70b-versatile, Gemini → gemini-2.0-flash)
    ai_model: str = ""
    # Override base URL for OpenAI-compatible providers (groq/ollama). Empty = provider default.
    ai_base_url: str = ""
    # Override the system prompt. Empty = use built-in optimized default.
    ai_system_prompt: str = ""
    # Sampling temperature. Lower = more consistent. 0.35 recommended for business DMs.
    ai_temperature: float = 0.35

    # Failover AI provider — se il primario fallisce (429/5xx/timeout/connessione)
    # la generazione ripiega su questo. Vuoto = nessun failover (single provider).
    # Es: AI_PROVIDER=gemini + AI_PROVIDER_FALLBACK=groq + AI_API_KEY_FALLBACK=gsk_...
    ai_provider_fallback: str = ""
    ai_api_key_fallback: str = ""
    ai_model_fallback: str = ""      # vuoto = default del provider fallback
    ai_base_url_fallback: str = ""   # per provider OpenAI-compatible (groq)

    # Anti-tempesta: backoff del worker quando la generazione AI fallisce per
    # rate-limit/timeout. Evita l'hot-loop che riclaimava gli stessi follower a
    # delay zero amplificando il 429. Dopo N transient consecutivi rimanda il batch.
    ai_gen_failure_threshold: int = 3       # transient consecutivi → defer batch
    ai_gen_backoff_base_seconds: int = 30   # backoff iniziale, raddoppia a ogni fallimento
    ai_gen_backoff_cap_seconds: int = 300   # tetto del backoff

    # Reply-check (rileva risposte leggendo l'inbox via API = pattern tracciabile).
    # Girare RARO e mirato per ridurre il footprint API (rischio checkpoint):
    # solo campagne attive e solo follower contattati negli ultimi N giorni.
    reply_check_max_age_days: int = 7
    # Dopo il COMPLETAMENTO di una campagna si continua a cercare risposte per
    # ancora N giorni (le risposte tardive sono ancora interessanti), poi stop:
    # oltre non ci si aspetta piu' risposte utili. Bilancia footprint API vs
    # risposte perse (caso reale: PODCAST completed, risposte a 24h non tracciate).
    reply_check_completed_grace_days: int = 3

    # Timing defaults
    min_delay_seconds: int = 120
    max_delay_seconds: int = 480
    session_min_messages: int = 10
    session_max_messages: int = 20
    session_break_min_minutes: int = 30
    session_break_max_minutes: int = 60
    active_hours_start: int = 8
    active_hours_end: int = 23
    # Timezone offset vs UTC (e.g. 2 for Italy UTC+2). Used only for active_hours check.
    timezone_offset_hours: int = 2

    # Distraction pause (occasional longer break simulating human distraction)
    # Set to 0 to disable. Defaults auto-scale if not set explicitly.
    distraction_pause_min_seconds: int = 0   # 0 = auto (3x max_delay, min 60s)
    distraction_pause_max_seconds: int = 0   # 0 = auto (10x max_delay, max 900s)
    distraction_pause_probability: float = 0.03  # 3% chance per inter-message gap; set 0 to disable

    # Max user_info lookups/day/account for scraping (anti-ban). Per-campaign override on campaigns.scrape_daily_limit.
    scrape_daily_limit: int = 300

    # Tetto giornaliero PERSISTITO ai like ambientali (browse_feed). Un like e'
    # una SCRITTURA, con vettore di blocco proprio, peggiore dello scrape in
    # lettura sopra: oggi il limite in browse_feed e' solo locale alla sessione
    # (0-2, azzerato a ogni chiamata) -- questo e' il tetto vero, per account,
    # persistito (migrazione 030). Default conservativo: browse_feed limita
    # gia' 0-2 like/sessione e viene chiamato poche volte/giorno (warmup + tra
    # i batch DM), quindi 10 e' vicino al comportamento attuale non ancora
    # vincolato -- si alza in scaglioni osservando gli account, mai in un
    # colpo solo (vedi memory botoutbound-checkpoint-pattern-api).
    daily_like_cap: int = 10

    # Cap random della mini-sessione bio prima della pausa lunga (era 250 fisso = firma).
    # Pescato per-sessione in [min,max] e persistito su campaigns.current_session_cap.
    bio_session_cap_min: int = 150
    bio_session_cap_max: int = 300

    # Fase Lista: page-size FISSO passato come max_amount (-> param `count`) alla
    # richiesta friendships/{id}/followers/.
    # MISURATO (probe 2026-07-07): l'endpoint ritorna SEMPRE ~25 utenti/risposta a
    # prescindere dal count richiesto (50,75,100,150,200 -> 25; count=250 -> HTTP
    # 400). Quindi 25 = tetto reale dell'endpoint per questo client.
    # Perche' FISSO e non random (era 20-40): un count variabile e' una firma
    # anomala per il classificatore IG (nessun client reale randomizza il count) +
    # mismatch col fingerprint dello User-Agent. Vedi memory
    # botoutbound-antidetect-protocollo-rigido.
    # Perche' proprio 25: `max_amount=25` fa rompere il loop interno di instagrapi
    # dopo UNA sola richiesta (25>=25) -> 1 richiesta per delay, niente burst. Un
    # valore piu' grande (es. 30-40) faceva ciclare instagrapi 2 volte a vuoto
    # (chiedeva 30, IG ne dava 25, 25<30 -> ri-richiesta senza delay).
    list_page_size: int = 25
    # Delay tra pagine lista (lognormale, non uniforme).
    list_page_delay_min_seconds: int = 5
    list_page_delay_max_seconds: int = 10
    # Pausa lunga occasionale tra pagine lista (scroll che si ferma).
    list_long_pause_probability: float = 0.06   # ~ogni 15-20 pagine
    list_long_pause_min_seconds: int = 30
    list_long_pause_max_seconds: int = 60

    # ── Inbox DM scraping (scrape_mode=dm_threads) ─────────────────────────
    # Solo engine API (direct_v2/inbox): pacing tra pagine. Lo scraping via
    # browser e' stato rimosso (la lista DM web non espone username/pk).
    # Delay base tra pagine inbox: lognormale TRONCATA su [min,max] (scroll attivo),
    # vedi _sample_page_delay. Mediana = sqrt(min*max) = 24.5s, sigma 0.9 in
    # scrape_inbox per varianza ampia (CV ~50%).
    # Range 10-60 (era 10-40): il clamp precedente accumulava il 45% dei delay
    # esattamente sui due bound, firma piu' netta di un delay costante.
    inbox_api_page_delay_min_seconds: int = 10
    inbox_api_page_delay_max_seconds: int = 60
    # Pausa lunga occasionale tra pagine inbox ("si ferma a leggere/rispondere").
    # DEVE stare sopra il max base: a 20-60 era interamente dentro il range
    # normale e non aggiungeva nessuna modalita' distinta alla distribuzione.
    inbox_long_pause_probability: float = 0.08
    inbox_long_pause_min_seconds: int = 90
    inbox_long_pause_max_seconds: int = 240
    # Quante chat raccolte prima del break di sessione (defer ARQ).
    inbox_session_size: int = 300
    inbox_break_min_minutes: int = 30
    inbox_break_max_minutes: int = 60
    # Pagine inbox consecutive con 0 contatti NUOVI dopo cui fermarsi + avvisare:
    # oltre questo punto l'inbox e' gente gia' in lista (IG puo' tenere has_older
    # sempre True, quindi la lista girerebbe a vuoto all'infinito in silenzio).
    inbox_empty_page_stop: int = 8
    # Batch invio DM: quanti DM consecutivi (random tra min e max) prima di fare
    # il feed browse/riposo. Dentro il batch nessuna attesa aggiunta tra i DM (il
    # browse del profilo target fa gia' da gap). Riduce la frequenza dello scroll.
    dm_batch_min: int = 1
    dm_batch_max: int = 4

    # Account defaults
    default_daily_limit: int = 20
    warmup_enabled: bool = True
    max_concurrent_browsers: int = 3

    # ── Warm-up browser alternato (diliuisce il pattern "solo API" per account) ──
    # Sessione organica Patchright (feed scroll, post, like ~35%) eseguita PRIMA di
    # ogni fase di scraping e DURANTE le pause lunghe. Riusa InstagramPage.browse_feed.
    # Migliora il rapporto organico:automatico che il risk-scoring notturno IG misura.
    # NON cura il mismatch web->mobile dell'API: e' mitigazione trust, non una cura.
    # ON di default (task B.4): la sessione organica e' collaudata dentro il
    # flusso DM e prima restava spenta di default, quindi non partiva mai in
    # produzione. bio_browser_batch_enabled resta OFF (Step 3, sotto): cambia
    # la FORMA della sessione (blocco di N profili scrapati dentro la pausa) e
    # va osservato prima di attivarlo, non e' lo stesso rischio del warmup.
    warmup_browse_enabled: bool = True
    warmup_browse_min_minutes: float = 4.0        # durata min sessione organica
    warmup_browse_max_minutes: float = 9.0        # durata max sessione organica
    warmup_browse_headless: bool = True           # headless in produzione worker
    # Warm-up durante le pause lunghe di lista/bio: se la pausa e' >= questa soglia,
    # infila una breve sessione organica (5-10 min) mentre il job API e' parcheggiato.
    warmup_browse_on_pause_min_pause_minutes: float = 20.0

    # ── Bio via browser a BLOCCO nella pausa (Step 3) ──
    # Lo screening via browser NON e' per-profilo sparso tra le chiamate API (aprire il
    # browser per 1 solo profilo non e' umano). Gira a BLOCCO dentro la pausa lunga bio,
    # nella STESSA sessione dello scroll organico: prima scroll, poi N profili scrapati.
    # Naviga il profilo con Patchright (piu' credibile, NON consuma il cap API mobile).
    bio_browser_batch_enabled: bool = False       # OFF default: attivare per test
    bio_browser_batch_min: int = 10               # min profili scrapati per pausa
    bio_browser_batch_max: int = 15               # max profili scrapati per pausa

    # --- Motore Fase Bio via browser (bio_engine='browser') ---
    bio_browser_headless: bool = False          # test: finestra visibile; prod: True
    # Attesa delle sorgenti PASSIVE dei dati profilo (passo 4). Si esce appena una
    # arriva: questo e' il tetto, non il tempo speso. Non abbassarlo a pochi secondi
    # -- la GraphQL arriva a mediana ~4s, con 2s le catture misurate sono ZERO e si
    # ricadrebbe sulla fetch esplicita, cioe' una richiesta attribuibile per profilo.
    bio_browser_source_wait_s: float = 8.0
    bio_browser_scroll_ratio: float = 0.35      # frazione profili con micro-scroll
    bio_browser_scroll_min_s: float = 4.0
    bio_browser_scroll_max_s: float = 5.0
    bio_browser_daily_limit: int | None = None  # cap opzionale profili/account/giorno (None = off)
    bio_browser_stagger_min_s: float = 60.0     # differita prima apertura per account
    bio_browser_stagger_max_s: float = 180.0
    # Cap profili per mini-sessione. Misurato in prod ~20-24s/profilo (nav +
    # /info/ + micro-scroll + pausa reel amortizzata), non ~15s: 40-70 profili
    # = sessione ~13-28 min (range largo voluto: durate piu' variabili tra
    # account = meno correlazione), ben sotto job_timeout=3600s anche nel
    # caso reel-heavy. Distinto da bio_session_cap_min/max (path API).
    bio_browser_session_cap_min: int = 40
    bio_browser_session_cap_max: int = 70
    # ── Pausa attiva sui reel (rimpiazza il "fermarsi a guardare" stazionario) ──
    # Dopo un numero random di profili (in [every_min, every_max]), invece di restare
    # fermi (vecchia distrazione 15-45s in human_profile_pause, rimossa), l'account va
    # sui Reel e ne SCORRE un numero random (in [count_min, count_max]), fermandosi su
    # ciascuno un tempo random (in [dwell_min_s, dwell_max_s]) prima di passare al
    # successivo — attivita' che un utente vero farebbe comunque. NON tocca mai
    # storie/highlights: guardare una storia lascia una "visualizzazione" visibile al
    # target, quindi restano fuori da qualunque attivita' ambient (browse_feed,
    # browse_reels, micro-scroll).
    # every_min/count_min/dwell_min_s NON possono essere 0 (task B.1): erano
    # minimi di un sorteggio, quindi "pausa disattivata" e "pausa di durata
    # zero uscita a caso" erano lo stesso stato osservabile, e la pausa reel
    # SOSTITUISCE quella umana (if/else in browser_bio.py/browser_import.py)
    # -- una pausa reel da 0s toglieva anche la pausa che ci sarebbe stata.
    # Caso peggiore misurato con i vecchi minimi: 0.0s di pausa per profilo
    # (vedi worst_case_delay_budget_s in browser_bio.py). Con questi minimi
    # il caso peggiore sale a ~5.3s (every_min=3, count_min=2, dwell_min_s=3:
    # ((3-1)*5.0 + 2*3.0) / 3 = 16/3).
    bio_browser_reels_every_min: int = 3          # dopo quanti profili scatta la pausa reel (random)
    bio_browser_reels_every_max: int = 10
    bio_browser_reels_count_min: int = 2          # quanti reel scorrere nella pausa (random)
    bio_browser_reels_count_max: int = 10
    bio_browser_reels_dwell_min_s: float = 3.0    # sosta su ciascun reel prima di scorrere
    bio_browser_reels_dwell_max_s: float = 10.0
    bio_browser_open_post_ratio: float = 0.25     # prob. di aprire 1 post su profilo pubblico
    # Arricchimento contatti via /api/v1/users/{pk}/info/ (in-page fetch web-autenticato):
    # web_profile_info NON espone email/telefono business (business_email=null); /info/ con
    # app-id web li da' in public_email/public_phone_number (misurato 08/07). Senza, il motore
    # browser perde ~95% delle email. ON di default (e' lo scopo). Kill-switch se un giorno
    # /info/ dal browser venisse rate-limitato a volume.
    bio_browser_contact_info_enabled: bool = True
    # Gate professional (passo 4, §4.3): /info/ parte solo sui profili professional,
    # letti dal payload GIA' scaricato -> zero richieste in piu' per decidere. Misurato
    # su tre probe (44 casi): ogni profilo con un contatto reale era professional, zero
    # persi; risparmio atteso ~34% delle chiamate. ON di default (e' lo scopo del
    # passo 4). Spegnerlo se sul campo si vedessero contatti persi: la raccolta
    # contatti resta accesa, cade solo la selezione.
    bio_browser_professional_gate_enabled: bool = True
    # Breaker soft-block sul canale browser (mirror del guard consecutivi del path API):
    # dopo N mini-sessioni CONSECUTIVE di UN account chiuse in soft-block (429), invece
    # di ritentare all'infinito ogni 15-30min, la campagna va in pausa e l'operatore
    # viene avvisato. Il contatore si azzera appena l'account torna a scrapare (>=1 done).
    bio_browser_soft_block_pause_threshold: int = 4

    # ── App-like media fetch dopo user_info in Fase Bio (Ramo B) ──
    # DISATTIVO di default. Su sessione API "nuda" ogni user_medias_v1 e' una 2a
    # chiamata a gap zero dopo user_info, sull'endpoint /feed/user che IG rate-limita
    # molto piu' duro di /info/ -> RADDOPPIA il volume per profilo e ANTICIPA il 429
    # (osservato live 05/07). L'apertura profilo app-like vera va fatta sul canale
    # browser (bio_browser_batch), non sull'API mobile. Riattivare SOLO dietro un test
    # volume che dimostri che regge il rate.
    bio_app_like_media_enabled: bool = False

    # ── from_module realistico su user_info in Fase Bio ──
    # DISATTIVO di default. `user_info_v1` di serie usa from_module="self_profile"; su
    # profili altrui e' una firma per il checkpoint "attività automatizzata" del giorno
    # dopo. Cambiarlo in feed_timeline/reel_feed_timeline manda entry_point=profile: piu'
    # realistico come SIGNATURE, ma su sessione nuda senza feed/reel realmente caricati e'
    # una claim di contesto che la sessione non regge, sospettata di throttle 429 piu' duro
    # (osservato 05/07, non provato). OFF = call identica alla baseline storica (self_profile)
    # che NON dava 429 immediato. Riattivare solo su account sani + A/B controllato: priorita'
    # a "scraping che gira" sul "checkpoint del giorno dopo".
    bio_realistic_from_module_enabled: bool = False

    # ── Device unico per account (device_pool) ──
    # OFF di default. Se ON, ogni "Login Browser" assegna all'account un device dal pool
    # (device_pool.py) invece del OnePlus 6T di default instagrapi, per rompere la firma
    # "tutti sullo stesso telefono". ⚠️ NON abilitare finche' ogni entry del pool non e'
    # stata verificata contro uno user-agent Instagram Android REALE: un device incoerente
    # (codename/SoC/dpi che nessun telefono vero emette) e' una firma PEGGIORE del default,
    # che almeno e' un device reale. Meglio un device reale condiviso che uno unico ma finto.
    device_diversify_enabled: bool = False

    # Warm-up daily limits — format "day_start-day_end:limit,..." (ranges inclusive).
    # Applies to accounts with warmup_day in 1..14. Day 0 = warmup finished.
    warmup_limits: str = "1-3:5,4-7:12,8-14:20"

    # Age-based hard cap on DMs/day, by days since account row created in our DB.
    # Format "threshold_day:limit,..." — entries cumulative (last matching threshold wins).
    # Use "none" for "no cap". Bypassed once account total_messages_sent >= proven_account_threshold.
    age_based_caps: str = "0:0,3:3,7:8,14:none"

    # Total DMs sent before an account is considered "proven" → age cap stops applying.
    proven_account_threshold: int = 30

    # Browser
    browser_profiles_dir: str = "./data/browser_profiles"
    headless: bool = True

    # Chiave HMAC per gli pseudonimi dei numeri di telefono (P12).
    # DEDICATA, non SECRET_KEY: ruotare SECRET_KEY e' un'operazione normale,
    # ruotare questa significa perdere l'aggancio a TUTTI i phone_hmac gia'
    # scritti a DB. Vanno tenute separate proprio per poter ruotare l'una
    # senza distruggere l'altra.
    phone_hmac_key: str = ""

    # JWT auth (multi-user). Generate jwt_secret with `secrets.token_urlsafe(32)`.
    # Empty disables the auth router and route guards (legacy single-user mode).
    jwt_secret: str = ""

    @field_validator("jwt_secret")
    @classmethod
    def validate_jwt_secret(cls, v: str) -> str:
        if not v or len(v) < 16:
            raise ValueError(
                "JWT_SECRET non impostato (o troppo corto) nel file .env. "
                'Genera con: python -c "import secrets; print(secrets.token_urlsafe(32))"'
            )
        return v

    jwt_algorithm: str = "HS256"
    jwt_expires_minutes: int = 60 * 24  # 24h default; override via .env JWT_EXPIRES_MINUTES

    # Telegram notifications. Both must be set to enable.
    # Get token from @BotFather, chat_id from @userinfobot.
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_commands_enabled: bool = True
    telegram_poll_timeout_seconds: int = 5
    telegram_session_recap_enabled: bool = True

    # Auth login rate limit (in-memory per backend process).
    auth_login_rate_limit_attempts: int = 5
    auth_login_rate_limit_window_minutes: int = 15
    auth_trust_forwarded_for: bool = False

    # Anomaly detector — auto-pause on critical patterns.
    anomaly_auto_stop_enabled: bool = True
    anomaly_ban_threshold_per_hour: int = 3            # 3+ banned accounts/h → pause all using that account
    anomaly_consecutive_dm_failures: int = 5           # 5+ consecutive failures → pause that campaign
    anomaly_challenge_threshold_per_day: int = 3       # 3+ challenge events/24h → pause everything
    anomaly_worker_crash_threshold_per_hour: int = 3   # 3+ worker crashes/h → notify (no auto-stop)

    # --- Canale WhatsApp: ingest e campagne (M2) -------------------------
    # Prefisso applicato ai numeri senza '+' (SDD Q14). Un numero estero si
    # accetta solo se gia' in E.164.
    wa_ingest_default_country: str = "39"
    # Soft limit per file (SDD Q22): con cap 100-200/giorno, 5.000 contatti
    # sono MESI di campagna. Rifiutare e' piu' onesto che accettare.
    wa_ingest_max_rows: int = 5000
    # Tetto agli attributi liberi per contatto (SDD Q15).
    wa_ingest_max_attrs_bytes: int = 2048

    # --- Canale WhatsApp: invio (M3) -------------------------------------
    # Master switch fail-closed: nessun invio finche' non lo si accende a
    # mano. Nessun task di M2 lo tocca.
    wa_send_enabled: bool = False
    wa_daily_cap_default: int = 20              # SDD 10.3, warmup giorno 1-3
    # ATTENZIONE: proposta NON misurata (SDD 10.3). A6 si verifica solo con
    # la rampa di M5.
    # La rampa di volume vive TUTTA qui: ogni voce e' il tetto in MESSAGGI di
    # un giorno, e warmup_day e' l'indice 1-based che dice a che punto della
    # lista si trova un numero. Per cambiare la velocita' della rampa si
    # cambia QUESTA lista, non il passo qui sotto.
    wa_warmup_steps: str = "20,20,30,40,60,80,100"
    # Interruttore globale della rampa (G4, 08/08): a False il gradino di
    # warmup NON entra piu' nel min() di effective_wa_daily_cap() e
    # advance_wa_warmup_if_needed() non avanza NESSUN numero, QUALUNQUE sia
    # il valore di warmup_day sulla riga. Serve perche' warmup_day da solo
    # e' ambiguo: 0 poteva voler dire "rampa mai partita" o "spenta apposta",
    # e riattiva() (wa_numbers.py) scrive warmup_day=1 incondizionatamente
    # ad ogni riattivazione (decisione precedente e tuttora valida: un
    # numero sospeso non deve ripartire dal cap alto a cui era arrivato) --
    # senza questo flag separato, riattivare un numero con la rampa spenta
    # la riaccendeva in silenzio.
    #
    # Default False dal 12/08, era True. Il default True voleva dire "chi non
    # tocca il flag vede il comportamento di sempre", ma la decisione di
    # Tommaso (rampa spenta, 08/08) viveva in UNA riga di UN solo file:
    # WA_WARMUP_ENABLED=false nell'.env alla radice del repo. Ogni altro
    # albero -- un worktree di QA, un backend avviato su un'altra porta --
    # non ha quella riga, quindi girava con la rampa ACCESA e
    # advance_wa_warmup_if_needed() avanzava warmup_day sui numeri REALI.
    # Misurato il 12/08: entrambi i numeri Primero avevano
    # warmup_advanced_date = quel giorno, stampato da un backend di QA su
    # porta 8020 mentre la produzione girava col flag a false. Lo stato
    # sicuro non deve dipendere da una riga di .env che il prossimo albero
    # non avra'. Chi vuole la rampa la accende con WA_WARMUP_ENABLED=true.
    wa_warmup_enabled: bool = False
    # GRADINI DELLA LISTA QUI SOPRA AL GIORNO -- **NON** messaggi al giorno.
    # Deve restare 1: alzarlo significa SALTARE gradini, non mandare piu'
    # messaggi. Con 10 su una lista di 7 voci un numero nuovo passava da 20 a
    # 100 msg/giorno al primo avanzamento (cioe' al primo riavvio dell'app),
    # saltando l'intera rampa che esiste per non farlo bannare -- verificato a
    # runtime nel collaudo M5, e' il motivo per cui questo commento e' cosi'
    # lungo. Il nome del campo dice "steps" apposta: era l'ambiguita' del nome
    # precedente ("per_day") ad aver prodotto l'errore.
    wa_warmup_advance_steps_per_day: int = 1
    # Deciso con Tommaso 08/08 pomeriggio, sostituisce la proposta iniziale
    # (mediana 90s, SDD 10.3 -- "proposta, da tarare sulla rampa M5"): a
    # sigma invariata, mediana 15s tiene il tipico dentro 5-30s (coda
    # lognormale, non un range uniforme -- vedi wa_timing.wa_send_delay_seconds,
    # ogni tanto un delay piu' lungo di 30s o piu' corto di 5s e' voluto,
    # e' la stessa firma non-piatta di prima con un centro piu' basso).
    # SDD-whatsapp-channel.md §10.3 va allineato quando l'altra sessione in
    # corso sugli stessi file la lascia libera (non toccato qui apposta).
    wa_send_delay_median_s: int = 15
    wa_send_delay_sigma: float = 0.7            # SDD 10.3
    wa_session_min_msg: int = 8                 # SDD 10.3
    wa_session_max_msg: int = 15                # SDD 10.3
    wa_break_min_min: int = 20                  # SDD 10.3
    wa_break_max_min: int = 40                  # SDD 10.3
    # Deciso con Tommaso 07/08 sera per l'uso quotidiano reale (Primero): il
    # default originale (09:30-19:30, SDD 10.3) era troppo stretto, osservato
    # piu' volte nei collaudi A2/A3. Europe/Rome.
    wa_active_hours: str = "09:00-20:00"
    # Finestra del browser di INVIO. Era `headless=True` cablato nella
    # chiamata di wa_worker.esegui_mini_sessione: HEADLESS=false nell'.env non
    # aveva effetto sull'invio (ce l'ha sul login assistito, che passa False
    # esplicito, e sulla Fase A discover, che ha il parametro), quindi guardare
    # il primo messaggio partire richiedeva di modificare il sorgente.
    # Default True e non `headless`: il worker gira per giorni senza nessuno
    # davanti, e una finestra che si apre da sola su un PC di casa non e' un
    # default sano. Si mette a false per il collaudo, poi si rimette.
    wa_send_headless: bool = True
    # STIMATO, non misurato: finestra in cui la sincronizzazione post
    # riconnessione rende cieca la guardia (A9/FM16). Da rimisurare quando
    # SYNC_INDICATOR sara' catalogato. Abbassato da 15 a 2 (decisione
    # Tommaso 08/08, dopo aver visto 15 min morti a ogni riavvio del
    # browser nel collaudo dal vivo): resta una stima, non una misura --
    # se la guardia mostra falsi "vuoto" dopo la riconnessione, il primo
    # sospettato e' questo valore troppo basso, non un bug altrove.
    wa_resync_quarantine_min: int = 2
    wa_guard_tail_n: int = 40                   # default del POM
    wa_guard_history_min: int = 80              # default del POM
    # Stesso valore di campaign_orchestrator.LOCK_TIMEOUT_MINUTES.
    wa_lock_timeout_min: int = 20
    wa_max_failures_per_contact: int = 3        # SDD 8.2
    # Parole DURE: opt-out immediato, nessun caso ambiguo in italiano comune
    # (review G6, 07/08). "basta" spostata sotto: e' comunissima in frasi che
    # non sono un opt-out ("mi basta sapere se siete aperti").
    wa_stop_words: str = "stop,cancellami,non scrivermi,unsubscribe,rimuovimi"
    # Parole AMBIGUE: opt-out immediato SOLO se il messaggio e' sostanzialmente
    # quella parola sola (wa_optout.looks_like_stop, testo normalizzato <= 3
    # parole); altrimenti nessun opt-out automatico -- vedi
    # looks_like_ambiguous_stop_needs_review per la segnalazione umana.
    wa_stop_words_ambigue: str = "basta"
    wa_global_daily_cap: int = 200              # SDD Q70, safety valve macchina
    # Circuit breaker sul tasso di opt-out (review P4, 07/08): il numero che
    # rischia il ban e' del CLIENTE, non nostro. Sotto wa_optout_breaker_min_invii
    # il campione e' troppo piccolo per significare qualcosa (1 opt-out su 2
    # invii e' 50%, rumore puro) -- il breaker resta muto finche' non c'e'
    # abbastanza segnale. Soglia (25%) volutamente piu' alta del solo
    # "allarme" mostrato in UI (SOGLIA_ALLARME_OPTOUT_PCT=5%, wa_campaigns.py):
    # quello e' un warning da leggere con calma, questo ferma il canale.
    wa_optout_breaker_min_invii: int = 10
    wa_optout_breaker_pct: float = 25.0
    # Dead-man's switch esterno (review P6, 07/08): il backend gira sul PC
    # di casa -- se il PC si spegne, l'health-check che dovrebbe avvisare e'
    # dentro il processo morto, non avvisa nessuno. Un ping periodico verso
    # un servizio esterno (healthchecks.io o simile, URL con token univoco
    # generato LORO) e' l'unico allarme che sopravvive al processo che
    # dovrebbe generarlo. Vuoto = disabilitato, fail-safe: nessun URL
    # configurato non deve rompere il boot ne' i cron esistenti.
    wa_deadman_ping_url: str = ""

    # --- Canale WhatsApp: reply-watcher + opt-out (M4) --------------------
    # Lucchetto profilo Chromium. Il conto vero di una mini-sessione nel caso
    # peggiore: wa_session_max_msg=15 messaggi, ciascuno con il delay
    # lognormale fra i messaggi (mediana 90s, coda destra inclusa) PIU' il
    # costo di invio+guardia misurato in PoC-2 (~53s/messaggio) -- media ~40
    # min, deviazione standard ~6 min. A 45 min circa una sessione su cinque
    # sforava il TTL, e uno sforamento significa un secondo Chromium
    # legittimamente aperto sullo stesso profilo (profilo corrotto, sessione
    # WhatsApp persa, QR da far riscansionare al cliente). 90 min mette la
    # scadenza a ~8 sigma dalla media; la difesa vera restano comunque
    # l'heartbeat (wa_profile_lock.renew dopo ogni messaggio) e il cap
    # wall-clock del loop di invio, non questo numero.
    wa_profile_lock_ttl_min: int = 90
    # Pulizia proattiva del lock (wa_profile_lock.release_stale), separata
    # dal TTL sopra apposta: quello e' anche il cap wall-clock della
    # mini-sessione, questo e' solo "da quanto manca un heartbeat prima di
    # considerare il possessore morto". Vedi rationale nel docstring di
    # release_stale.
    wa_profile_lock_stale_min: int = 25
    # Retry breve quando un job di invio trova il profilo occupato (health-
    # check o reply-scan in corso): non e' la fine-sessione (break_s, minuti-
    # decine), e' "riprova fra un attimo".
    wa_lock_busy_retry_s: int = 90
    # Quanto a lungo dopo l'ultimo invio un numero resta scansionabile dal
    # reply-watcher per catturare risposte tardive. Serve perche' le campagne
    # MVP sono a un solo step (SDD Q29): finito l'invio i contatti passano a
    # 'completed' e il numero non avrebbe piu' "lavoro vivo" proprio quando le
    # risposte iniziano ad arrivare (ore o giorni dopo). Valore arbitrario ma
    # delimitato: oltre questa finestra si assume che una risposta non
    # arrivera' piu' o non e' piu' rilevante operativamente -- un'inclusione
    # senza limite dei 'completed' farebbe crescere la lista per sempre.
    wa_reply_scan_window_days: int = 3

    # Lock Redis cross-processo sul profilo browser IG (C.2, passo 4): TTL
    # CORTO + rinnovo automatico dentro il chokepoint (context_manager),
    # non lungo per prudenza come il lock WA sopra (90min, gia' causa di un
    # orfano risolto a mano — vedi app/browser/profile_lock.py). Un crash a
    # meta' sessione si autolibera in pochi minuti invece di bloccare
    # l'account per un'ora e mezza. Rapporto renew:TTL circa 1:3, cosi' un
    # singolo rinnovo mancato (blip Redis) lascia comunque margine prima
    # della scadenza.
    browser_profile_lock_ttl_s: int = 180
    browser_profile_lock_renew_s: int = 60
    # Rilievo review C.1-C.3: un blip Redis singolo durante il rinnovo resta
    # fail-open (non abbatte una sessione viva per un hiccup), ma oltre N
    # fallimenti CONSECUTIVI il lock e' trattato come perso (fail-closed).
    # N=2 = almeno un ciclo di rinnovo intero (60s, vedi sopra) di Redis
    # confermata irraggiungibile, non un singolo blip isolato; con TTL 180s
    # questo lascia comunque margine di reazione prima della scadenza
    # naturale, invece di aspettarla e basta.
    browser_profile_lock_max_renew_errors: int = 2


settings = Settings()
