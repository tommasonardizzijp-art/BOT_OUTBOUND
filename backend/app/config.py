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

    # Cap random della mini-sessione bio prima della pausa lunga (era 250 fisso = firma).
    # Pescato per-sessione in [min,max] e persistito su campaigns.current_session_cap.
    bio_session_cap_min: int = 150
    bio_session_cap_max: int = 300

    # Fase Lista: dimensione pagina randomizzata passata come max_amount a
    # user_followers_v1_chunk. Senza un valore piccolo, instagrapi drena l'intera
    # lista in un burst count=200 senza delay -> challenge IG. Con 20-40 ogni
    # chiamata ritorna pochi utenti e il delay sotto agisce (scroll umano).
    list_page_size_min: int = 20
    list_page_size_max: int = 40
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
    # Delay base tra pagine inbox: lognormale clampato a [min,max] (scroll attivo).
    # Mediana = (min+max)/2 = 6s; sigma alto in scrape_inbox per varianza ampia.
    inbox_api_page_delay_min_seconds: int = 2
    inbox_api_page_delay_max_seconds: int = 10
    # Pausa lunga occasionale tra pagine inbox ("si ferma a leggere/rispondere").
    inbox_long_pause_probability: float = 0.08
    inbox_long_pause_min_seconds: int = 20
    inbox_long_pause_max_seconds: int = 60
    # Quante chat raccolte prima del break di sessione (defer ARQ).
    inbox_session_size: int = 300
    inbox_break_min_minutes: int = 30
    inbox_break_max_minutes: int = 60

    # Account defaults
    default_daily_limit: int = 20
    warmup_enabled: bool = True
    max_concurrent_browsers: int = 3

    # ── Warm-up browser alternato (diliuisce il pattern "solo API" per account) ──
    # Sessione organica Patchright (feed scroll, post, like ~35%) eseguita PRIMA di
    # ogni fase di scraping e DURANTE le pause lunghe. Riusa InstagramPage.browse_feed.
    # Migliora il rapporto organico:automatico che il risk-scoring notturno IG misura.
    # NON cura il mismatch web->mobile dell'API: e' mitigazione trust, non una cura.
    warmup_browse_enabled: bool = False           # OFF di default: attivare per campagna/test
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
    bio_browser_scroll_ratio: float = 0.35      # frazione profili con micro-scroll
    bio_browser_scroll_min_s: float = 4.0
    bio_browser_scroll_max_s: float = 5.0
    bio_browser_daily_limit: int | None = None  # cap opzionale profili/account/giorno (None = off)
    bio_browser_stagger_min_s: float = 60.0     # differita prima apertura per account
    bio_browser_stagger_max_s: float = 180.0
    # Cap profili per mini-sessione: PICCOLO (browser ~15s/profilo → deve stare
    # sotto job_timeout=3600s). Distinto da bio_session_cap_min/max (path API).
    bio_browser_session_cap_min: int = 20
    bio_browser_session_cap_max: int = 40
    # ── Pausa attiva sui reel (rimpiazza il "fermarsi a guardare" stazionario) ──
    # Ogni 2-3 profili, invece di restare fermi (vecchia distrazione 15-45s in
    # human_profile_pause, rimossa), l'account scrolla i Reel per un po' — attivita'
    # che un utente vero farebbe comunque. NON tocca mai storie/highlights: guardare
    # una storia lascia una "visualizzazione" visibile al target, quindi restano
    # fuori da qualunque attivita' ambient (browse_feed, browse_reels, micro-scroll).
    bio_browser_reels_every_min: int = 2          # ogni quanti profili una pausa attiva sui reel
    bio_browser_reels_every_max: int = 3
    bio_browser_reels_seconds_min: float = 15.0   # durata mini-sessione reel
    bio_browser_reels_seconds_max: float = 40.0
    bio_browser_open_post_ratio: float = 0.25     # prob. di aprire 1 post su profilo pubblico

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


settings = Settings()
