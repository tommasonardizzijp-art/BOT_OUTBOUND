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
    scrape_daily_limit: int = 180

    # Follower-LIST page size. CRITICO: passato come max_amount a
    # user_followers_v1_chunk. Senza questo (max_amount=0), instagrapi fa un loop
    # interno che drena l'INTERA lista in un burst di richieste count=200 SENZA
    # delay → challenge "comportamento automatizzato" immediata. Con un valore
    # piccolo (es. 30) ogni chiamata ritorna ~30 utenti e poi il delay sotto
    # agisce tra le pagine, simulando lo scroll umano del modale follower.
    scrape_page_size: int = 30

    # Delay between follower-LIST pagination calls (user_followers_v1_chunk).
    # This is the highest-risk endpoint for IG bot detection — keep it slow and
    # well-randomized to mimic a human scrolling the followers modal. The old
    # hardcoded 5-15s was too fast/regular and triggered "automated behavior"
    # challenges on 9k+ lists. Lognormal jitter is applied on top of this range.
    scrape_page_delay_min_seconds: int = 25
    scrape_page_delay_max_seconds: int = 70
    # Occasional long "human distraction" pause during list pagination.
    scrape_page_long_pause_probability: float = 0.08   # 8% chance per page
    scrape_page_long_pause_min_seconds: int = 120
    scrape_page_long_pause_max_seconds: int = 300

    # Account defaults
    default_daily_limit: int = 20
    warmup_enabled: bool = True
    max_concurrent_browsers: int = 3

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
