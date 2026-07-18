import uuid
from datetime import datetime
from sqlalchemy import String, Integer, Float, Boolean, Text, DateTime, BigInteger, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base
import enum


class CampaignStatus(str, enum.Enum):
    draft = "draft"
    listing = "listing"
    listing_break = "listing_break"
    scraping = "scraping"
    scraping_break = "scraping_break"
    scraping_and_running = "scraping_and_running"
    ready = "ready"
    running = "running"
    paused = "paused"
    completed = "completed"
    error = "error"


# Stati in cui la Fase Bio e' ATTIVA. Include scraping_and_running (DM in parallelo):
# i worker Bio DEVONO continuare a girare mentre i DM partono. Fonte unica per tutti
# i gate di stato dei worker (scraper.py, scrape_bios.py, browser_bio.py) — evita il
# drift che faceva uscire il worker Bio quando la campagna passava a scraping_and_running.
SCRAPING_ACTIVE_STATES = (
    CampaignStatus.scraping,
    CampaignStatus.scraping_break,
    CampaignStatus.scraping_and_running,
)


def bio_done_status(current: "CampaignStatus") -> "CampaignStatus":
    """Stato di destinazione a Fase Bio completata: se il DM gira in parallelo
    (scraping_and_running) resta 'running' cosi' i worker DM continuano; altrimenti
    'ready' (attende l'avvio manuale dei DM)."""
    return (
        CampaignStatus.running
        if current == CampaignStatus.scraping_and_running
        else CampaignStatus.ready
    )


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    target_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 'scrape' = scrape follower/following di una pagina; 'import' = lista profili caricata da file
    source_type: Mapped[str] = mapped_column(String(20), nullable=False, default='scrape')
    target_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    base_message_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_prompt_context: Mapped[str | None] = mapped_column(Text, nullable=True)
    # M10: optional second template for A/B testing (50/50 random split)
    message_template_b: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Template mode: se False (default nuove campagne) i DM sono renderizzati
    # localmente dai template A/B/C + spintax, SENZA chiamate AI. Migration 023
    # setta True sulle campagne esistenti (comportamento invariato).
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Terzo template opzionale (variante 'c'), simmetrico a message_template_b.
    message_template_c: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Quarto template opzionale (variante 'd'), simmetrico a b/c. Migration 024.
    message_template_d: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Istruzioni AI per-campagna: override del prompt di sistema globale (.env).
    # NULL/vuoto = usa il globale.
    ai_system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[CampaignStatus] = mapped_column(
        SAEnum(CampaignStatus, native_enum=False), nullable=False, default=CampaignStatus.draft
    )
    total_followers: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    messages_sent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    messages_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    messages_pending: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Maximum DMs to send per day across ALL accounts for this campaign. NULL = unlimited.
    daily_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # M15 rev: if True, a sample of AI-generated messages must be approved before sending
    require_approval: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # How many messages to put in approval queue per pre-gen run (default 5)
    approval_sample_size: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    # 'followers' = scrape who follows target; 'following' = scrape who target follows
    scrape_mode: Mapped[str] = mapped_column(String(20), nullable=False, default='followers')
    # Parallel scraping + DM config (per-campaign)
    scrape_session_size: Mapped[int] = mapped_column(Integer, default=250, nullable=False)
    # Cap random della mini-sessione bio in corso (persistito per restart-safety del
    # next_long_break; None = da pescare). Vedi bio_session_cap_min/max in config.
    current_session_cap: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scrape_break_minutes_min: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    scrape_break_minutes_max: Mapped[int] = mapped_column(Integer, default=45, nullable=False)
    bio_fetch_delay_min: Mapped[float] = mapped_column(Float, default=5.0, nullable=False)
    bio_fetch_delay_max: Mapped[float] = mapped_column(Float, default=8.0, nullable=False)
    auto_generate: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # If False, this is a scraping-only campaign: no DM workers, no AI generation.
    messaging_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Per-campaign override of SCRAPE_DAILY_LIMIT (lookups/day/account). NULL = use .env default.
    scrape_daily_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scrape_break_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    scrape_break_prev_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    scrape_cursor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Engine estrazione lista per scrape_mode=dm_threads. Default 'api'.
    # 'browser' e' DEPRECATO/no-op: lo scraping via browsing del DOM e' stato
    # rimosso (la lista DM web mostra solo il nome visualizzato, non username/pk)
    # — il backend usa sempre l'API. Valore accettato per retrocompat. Migration 020.
    inbox_engine: Mapped[str] = mapped_column(String(10), nullable=False, default='api', server_default='api')
    # Motore Fase Bio. 'api' = instagrapi (user_info, veloce, consuma cap). Default.
    # 'browser' = Patchright (web_profile_info, prudente, no cap API). Vedi migration 022.
    bio_engine: Mapped[str] = mapped_column(
        String(10), nullable=False, default='api', server_default='api'
    )
    list_target: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bio_target: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 'completed' | 'partial' | 'rate_limited' — esito ultimo scraping
    scrape_outcome: Mapped[str | None] = mapped_column(String(20), nullable=True)
    scrape_completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
