import uuid
from datetime import datetime
from sqlalchemy import String, Integer, Float, Boolean, Text, DateTime, BigInteger, Enum as SAEnum, text
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


# Livello di arricchimento: decide quali RICHIESTE si fanno a Instagram, non se si
# salvano i dati che arrivano gratis (decisione Tommaso, passo 4 A.5). Ortogonale a
# `bio_engine`, che decide COME (instagrapi o browser) — i due motori pagano il
# livello 'contacts' con richieste diverse, ma a parita' di livello raccolgono lo
# STESSO tipo di dato:
#   none     = nessuna visita dedicata. I dati arrivano solo dalla Fase Lista, piu'
#              l'harvest passivo durante la visita che il DM fa comunque.
#   bio      = visita dedicata: bio/follower/link, PIU' i contatti che si trovano
#              gratis nel testo della bio (email/telefono via regex) o in un link
#              whatsapp — non costano nessuna richiesta oltre alla bio stessa.
#              Nessuna richiesta DEDICATA ai contatti.
#              - canale BROWSER: qui si vede la differenza — /info/ non parte,
#                quindi i campi business (public_email/public_phone_number) restano
#                vuoti; solo il testo della bio puo' dare un contatto.
#              - canale API: user_info_v1/fetch_profile_app_like porta SEMPRE quei
#                campi business nello stesso payload della bio (zero richieste in
#                piu' del browser a questo livello), ma a 'bio' NON si salvano —
#                sono l'equivalente di cio' che sul browser costerebbe la richiesta
#                dedicata. Si salva solo cio' che arriverebbe gratis anche li' (bio-
#                testo, link, whatsapp). Vedi `extract_contacts(includi_campi_dedicati=)`
#                in contact_extract.py.
#   contacts = come 'bio' + i campi business dedicati (public_email/public_phone_number).
#              - canale BROWSER: costano la richiesta /info/, quindi partono solo se
#                il profilo e' anche professional (gate separato, vedi
#                `contatti_richiesti` in browser_bio.py).
#              - canale API: sono gia' nel payload; il livello decide solo se
#                includerli (vedi `contatti_richiesti_dal_livello` sotto).
ENRICHMENT_NONE = "none"
ENRICHMENT_BIO = "bio"
ENRICHMENT_CONTACTS = "contacts"
ENRICHMENT_LEVELS = (ENRICHMENT_NONE, ENRICHMENT_BIO, ENRICHMENT_CONTACTS)

# Livelli che prevedono una visita dedicata al profilo (Fase Bio o risoluzione import).
ENRICHMENT_WITH_VISIT = (ENRICHMENT_BIO, ENRICHMENT_CONTACTS)


def contatti_richiesti_dal_livello(campaign) -> bool:
    """True se il livello scelto in UI e' 'contacts'.

    E' il pezzo comune ai due canali (valuta SOLO `enrichment_level == 'contacts'`,
    nient'altro), ma i due canali lo usano in due modi diversi (passo 4, A.5 —
    decisione Tommaso: il livello governa le RICHIESTE, non i dati gia' arrivati
    gratis):
      - canale BROWSER: se False blocca la richiesta `/info/` (combinata con
        kill-switch operativo + gate professional, vedi `contatti_richiesti` in
        browser_bio.py) — a livello 'bio' quella richiesta non parte, quindi i
        campi business restano vuoti e basta.
      - canale API: i campi business sono GIA' nel payload scaricato per la bio
        (zero richieste in piu'), quindi qui il risultato diventa il flag
        `includi_campi_dedicati` di `extract_contacts()` — se False quei DUE campi
        (public_email, public_phone_number/contact_phone_number) non si salvano,
        ma tutto il resto (bio-testo, link, whatsapp) si salva comunque: e' dato
        gratuito, non e' governato dal livello.

    Retrocompatibilita': campo assente o None => procede (comportamento pre-livelli).
    Questo ramo NON e' raggiungibile da una vera riga Campaign: la colonna e'
    `NOT NULL` con `server_default='none'` (migration 029), quindi ogni riga nel DB
    ha sempre un valore concreto ('none'/'bio'/'contacts'), mai None — un
    `Campaign(enrichment_level=None)` diventa 'none' gia' al commit (default
    Python-side di SQLAlchemy che scatta su qualunque None, esplicito o no). Il
    ramo serve solo per oggetti costruiti in memoria (test, SimpleNamespace, o
    payload che non passano dal modello ORM): non toglierlo pensando sia morto,
    e non contarci per la retrocompatibilita' di righe DB reali — quella la
    garantisce gia' il default della colonna.
    """
    livello = getattr(campaign, "enrichment_level", None)
    return livello is None or livello == ENRICHMENT_CONTACTS


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
    # Vedi ENRICHMENT_LEVELS sopra. Default 'none' sulle campagne nuove: un livello
    # che consuma account si accende di proposito, non si trova acceso. Migration 029.
    enrichment_level: Mapped[str] = mapped_column(
        String(10), nullable=False, default=ENRICHMENT_NONE, server_default=ENRICHMENT_NONE
    )
    list_target: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bio_target: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Segnalibro della Fase Lista via browser: la data della riga di lista piu'
    # vecchia gia' lavorata. In modalita' segnalibro si scorre senza aprire
    # finche' le righe sono piu' recenti di questa soglia. E' una DATA, non il
    # riferimento a una chat: se proprio quella chat ricevesse una risposta
    # risalirebbe in cima e il riferimento sparirebbe. Migration 033.
    inbox_cursor_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    inbox_cursor_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Fase Lista inbox via API: il fondo dell'inbox e' gia' stato raggiunto almeno
    # una volta? Finche' e' False si SCENDE (pagine di soli gia' noti sono normali:
    # fermarsi li' renderebbe irraggiungibili i thread piu' vecchi, che sono il
    # motivo per cui si usa l'API). Quando diventa True ogni giro riparte dalla
    # cima solo per intercettare i DM nuovi. Migration 036.
    inbox_bottom_reached: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # 'completed' | 'partial' | 'rate_limited' — esito ultimo scraping
    scrape_outcome: Mapped[str | None] = mapped_column(String(20), nullable=True)
    scrape_completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
