"""Modelli del canale WhatsApp (SDD 5.2): tenants a parte in app.models.tenant,
qui le 8 tabelle wa_*.

Stile: come app/models/campaign.py -- Mapped/mapped_column, enum str+enum.Enum
con SAEnum(native_enum=False) (validazione lato Python, colonna VARCHAR a DB;
vedi nota sugli enum nella migrazione 025 per il motivo).
"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


# ---------------------------------------------------------------------------
# Enum
# ---------------------------------------------------------------------------

class WaNumberStatus(str, enum.Enum):
    """State machine SDD 8.3."""
    pending_qr = "pending_qr"
    active = "active"
    qr_required = "qr_required"
    disconnected = "disconnected"
    cooldown = "cooldown"
    suspended = "suspended"
    retired = "retired"


class WaCampaignStatus(str, enum.Enum):
    """State machine SDD 8.1."""
    draft = "draft"
    running = "running"
    paused = "paused"
    stopped = "stopped"
    completed = "completed"
    error = "error"


class WaCampaignType(str, enum.Enum):
    """SDD 5.2 (wa_campaigns.campaign_type): pilota l'obbligo opt-out (V10).
    Non nell'elenco enum del brief -- aggiunto per coerenza di stile con gli
    altri campi tipizzati: il valore e' chiuso a due opzioni nella SDD, non
    una stringa libera."""
    marketing = "marketing"
    followup = "followup"


class WaContactStatus(str, enum.Enum):
    """State machine SDD 8.2 (wa_campaign_contacts.status). Terminali: replied,
    completed, opted_out, skipped. failed e' transitorio (retry)."""
    queued = "queued"
    in_sequence = "in_sequence"
    replied = "replied"
    completed = "completed"
    opted_out = "opted_out"
    skipped = "skipped"
    failed = "failed"


class WaSendCondition(str, enum.Enum):
    always = "always"
    if_no_reply = "if_no_reply"
    if_replied = "if_replied"


class WaMessageStatus(str, enum.Enum):
    queued = "queued"
    sending = "sending"
    sent = "sent"
    failed = "failed"
    skipped = "skipped"


class WaDeliveryCheck(str, enum.Enum):
    """Cosa ha visto il POM dopo l'invio (best-effort, PoC-2 dice quanto e'
    affidabile)."""
    none = "none"
    clock = "clock"
    single_tick = "single_tick"
    double_tick = "double_tick"


class WaMatchedBy(str, enum.Enum):
    chat_title = "chat_title"
    phone = "phone"
    none = "none"


class WaDncReason(str, enum.Enum):
    optout = "optout"
    unreachable = "unreachable"
    invalid_number = "invalid_number"
    manual = "manual"


# ---------------------------------------------------------------------------
# Tabelle
# ---------------------------------------------------------------------------

class WaNumber(Base):
    """Analogo di InstagramAccount per il canale WA (SDD 5.2 wa_numbers).
    Niente password: l'autenticazione e' la sessione QR nel profilo browser."""
    __tablename__ = "wa_numbers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True,
                                    default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"),
                                           nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    # HMAC del numero del cliente (chiave interna) -- mai il numero in chiaro.
    phone_hmac: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # Fernet -- serve solo per display admin mascherato e diagnostica.
    encrypted_phone: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[WaNumberStatus] = mapped_column(
        SAEnum(WaNumberStatus, name="wa_number_status", native_enum=False),
        default=WaNumberStatus.pending_qr, nullable=False)
    # Path profilo Chromium persistente (convenzione data/browser_profiles/wa_<id>).
    browser_profile: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Proxy mobile assegnato (V9: max 2 numeri per proxy, stesso tenant --
    # applicato a livello applicativo, non qui).
    proxy_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Cap messaggi/giorno per numero (default basso, modificabile a mano).
    daily_cap: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    # Riuso semantica IG: rampa graduale del cap.
    warmup_day: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # Guardia idempotente per l'avanzamento automatico giornaliero (M5),
    # stesso pattern/tipo di InstagramAccount.warmup_advanced_date: se
    # diverso da oggi (UTC, "YYYY-MM-DD") il numero non e' ancora stato
    # avanzato oggi, sicuro sia chiamato al boot sia dal cron giornaliero.
    warmup_advanced_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # Contatore date-aware, stesso pattern lazy-reset di scrape_lookups_date
    # (migrazione 018).
    sent_today: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sent_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    session_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                                 nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=datetime.utcnow, nullable=False)


class WaContact(Base):
    """Anagrafica per-tenant, cross-campagna (SDD 5.2 wa_contacts). Dedup
    cross-campagna analogo di GlobalContact, ma per-tenant e per-canale (V4)."""
    __tablename__ = "wa_contacts"
    __table_args__ = (
        # Un doppio upload dello stesso CSV non deve creare due contatti:
        # sarebbe la stessa persona contattata due volte.
        UniqueConstraint("tenant_id", "phone_hmac", name="uq_wa_contacts_tenant_phone"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True,
                                    default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"),
                                           nullable=False)
    phone_hmac: Mapped[str] = mapped_column(String(64), nullable=False)
    # Fernet -- decifrato SOLO da wa_sender al momento dell'apertura chat.
    encrypted_phone: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Titolo della chat WA Web (appreso al primo invio, serve al reply-watcher,
    # SDD 7.3). NULLABLE e mai popolato se il titolo e' un numero: il contatto
    # non e' in rubrica del cliente, e salvare il numero in chiaro qui
    # violerebbe P12 -- il matching in quel caso usa gia' phone_hmac (review
    # 23/07).
    chat_title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Colonne libere del CSV ({ultimo_ordine: "2026-01-10", ...}) -- placeholder
    # per i template.
    attributes: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    opted_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                           nullable=True)
    do_not_contact: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    dnc_reason: Mapped[WaDncReason | None] = mapped_column(
        SAEnum(WaDncReason, name="wa_dnc_reason", native_enum=False), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                     default=datetime.utcnow, nullable=False)
    last_contacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                                nullable=True)
    last_replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                              nullable=True)


class WaCampaign(Base):
    """Tabella dedicata (decisione D2b, SDD 5.2): il canale IG in produzione
    non tocca campaigns per questo cantiere."""
    __tablename__ = "wa_campaigns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True,
                                    default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"),
                                           nullable=False)
    # Un numero dedicato per campagna (decisione 23/07). Max 1 campagna
    # 'running' per numero alla volta -- vincolo applicativo allo start, non a
    # DB (review 23/07).
    wa_number_id: Mapped[str] = mapped_column(String(36), ForeignKey("wa_numbers.id"),
                                              nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    campaign_type: Mapped[WaCampaignType] = mapped_column(
        SAEnum(WaCampaignType, name="wa_campaign_type", native_enum=False), nullable=False)
    status: Mapped[WaCampaignStatus] = mapped_column(
        SAEnum(WaCampaignStatus, name="wa_campaign_status", native_enum=False),
        default=WaCampaignStatus.draft, nullable=False)
    daily_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    optout_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    optout_cta: Mapped[str | None] = mapped_column(String(500), nullable=True)
    active_hours_start: Mapped[str | None] = mapped_column(String(5), nullable=True)
    active_hours_end: Mapped[str | None] = mapped_column(String(5), nullable=True)
    session_min_messages: Mapped[int | None] = mapped_column(Integer, nullable=True)
    session_max_messages: Mapped[int | None] = mapped_column(Integer, nullable=True)
    break_min_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    break_max_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Contatori denormalizzati.
    total_contacts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    replied: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    opted_out: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=datetime.utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WaSequenceStep(Base):
    """La sequenza della campagna (SDD 5.2 wa_sequence_steps). Schema creato
    per intero in M1; l'MVP (Q29) usa un solo step per campagna -- il motore
    multi-step si accende post-MVP senza migrazione."""
    __tablename__ = "wa_sequence_steps"
    __table_args__ = (
        UniqueConstraint("campaign_id", "step_index", name="uq_wa_steps_campaign_index"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True,
                                    default=lambda: str(uuid.uuid4()))
    campaign_id: Mapped[str] = mapped_column(String(36), ForeignKey("wa_campaigns.id"),
                                             nullable=False)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # A obbligatorio, B/C/D opzionali -- stesso formato IG (spintax +
    # placeholder); render via template_renderer.pick_template()/render_template().
    template_a: Mapped[str] = mapped_column(Text, nullable=False)
    template_b: Mapped[str | None] = mapped_column(Text, nullable=True)
    template_c: Mapped[str | None] = mapped_column(Text, nullable=True)
    template_d: Mapped[str | None] = mapped_column(Text, nullable=True)
    send_condition: Mapped[WaSendCondition] = mapped_column(
        SAEnum(WaSendCondition, name="wa_send_condition", native_enum=False),
        default=WaSendCondition.always, nullable=False)
    # Attesa dallo step precedente prima di valutare la condizione.
    wait_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class WaCampaignContact(Base):
    """Stato per contatto x campagna (SDD 5.2 wa_campaign_contacts, state
    machine 8.2)."""
    __tablename__ = "wa_campaign_contacts"
    __table_args__ = (
        UniqueConstraint("campaign_id", "contact_id", name="uq_wa_camp_contact"),
        # next_action_at e' la colonna su cui gira il tick del sequence engine:
        # senza indice, ogni tick e' una scansione completa della tabella.
        Index("ix_wa_campaign_contacts_next_action", "next_action_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True,
                                    default=lambda: str(uuid.uuid4()))
    campaign_id: Mapped[str] = mapped_column(String(36), ForeignKey("wa_campaigns.id"),
                                             nullable=False)
    contact_id: Mapped[str] = mapped_column(String(36), ForeignKey("wa_contacts.id"),
                                            nullable=False)
    status: Mapped[WaContactStatus] = mapped_column(
        SAEnum(WaContactStatus, name="wa_contact_status", native_enum=False),
        default=WaContactStatus.queued, nullable=False)
    # Ultimo step inviato (-1 = nessuno).
    current_step: Mapped[int] = mapped_column(Integer, default=-1, nullable=False)
    # Quando il sequence engine deve rivalutare questo contatto.
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                             nullable=True)
    # A che step ha risposto (KPI drop-off, F6).
    replied_at_step: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Claim atomico per worker, stesso pattern locked_by_account_id di
    # followers (timeout stale identico).
    locked_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class WaMessage(Base):
    """Log invii, analogo di Message (SDD 5.2 wa_messages)."""
    __tablename__ = "wa_messages"
    __table_args__ = (
        # Indice unico PARZIALE, non una UniqueConstraint piena sulla tripla
        # (AVVIO 12/08 §5, migration 034): wa_sender.py permette una riga
        # nuova quando la precedente per lo stesso step e' 'failed' (retry).
        # Solo 'sending'/'sent' sono lo stato che il codice applicativo gia'
        # tratta come "invio gia' registrato, non rimandare" -- l'indice
        # rispecchia quell'invariante, non lo stringe.
        Index("uq_wa_messages_campaign_contact_step_attivo",
             "campaign_id", "contact_id", "step_index", unique=True,
             postgresql_where=text("status IN ('sending', 'sent')"),
             sqlite_where=text("status IN ('sending', 'sent')")),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True,
                                    default=lambda: str(uuid.uuid4()))
    campaign_id: Mapped[str] = mapped_column(String(36), ForeignKey("wa_campaigns.id"),
                                             nullable=False)
    contact_id: Mapped[str] = mapped_column(String(36), ForeignKey("wa_contacts.id"),
                                            nullable=False)
    wa_number_id: Mapped[str] = mapped_column(String(36), ForeignKey("wa_numbers.id"),
                                              nullable=False)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # 'a'..'d' (A/B testing come IG).
    template_variant: Mapped[str] = mapped_column(String(1), nullable=False)
    # Testo effettivamente inviato (audit + retry; contiene PII del contenuto,
    # non il numero -- vedi SDD 12).
    rendered_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[WaMessageStatus] = mapped_column(
        SAEnum(WaMessageStatus, name="wa_message_status", native_enum=False),
        default=WaMessageStatus.queued, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                default=datetime.utcnow, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Cosa ha visto il POM dopo l'invio (best-effort).
    delivery_check: Mapped[WaDeliveryCheck | None] = mapped_column(
        SAEnum(WaDeliveryCheck, name="wa_delivery_check", native_enum=False), nullable=True)


class WaInboundEvent(Base):
    """Risposte rilevate (SDD 5.2 wa_inbound_events). Minimizzazione: in MVP
    non si apre la chat per leggere le risposte e non si archivia il testo
    completo -- basta *che* abbia risposto (branching) + la preview
    (opt-out "STOP")."""
    __tablename__ = "wa_inbound_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True,
                                    default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"),
                                           nullable=False)
    wa_number_id: Mapped[str] = mapped_column(String(36), ForeignKey("wa_numbers.id"),
                                              nullable=False)
    # NULL se il matching fallisce (chat non riconducibile a un contatto
    # nostro -- ignorata ai fini del flow, tenuta per diagnostica).
    contact_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("wa_contacts.id"),
                                                    nullable=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                   default=datetime.utcnow, nullable=False)
    # SOLO la preview dalla lista chat (troncata da WhatsApp), usata per
    # opt-out detection; non si salva la conversazione (minimizzazione, SDD 12).
    preview_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    matched_by: Mapped[WaMatchedBy] = mapped_column(
        SAEnum(WaMatchedBy, name="wa_matched_by", native_enum=False), nullable=False)
    processed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class WaDiscoveredChat(Base):
    """Staging delle chat scoperte dalla Fase A auto-discover (spec 5.4).

    Tabella separata da wa_contacts per una ragione strutturale, non estetica:
    li' encrypted_phone e phone_hmac sono NOT NULL, quindi una chat di cui non
    si legge il numero -- il 100% dei gruppi e una parte delle 1:1 (misurato nel
    PoC-4) -- non potrebbe proprio esistere. Salvarla comunque, marcata, e' una
    decisione presa: il dato resta recuperabile a mano dalla rubrica del
    telefono. Il grezzo raccolto dal browser non tocca i contatti veri del bot
    finche' la Fase B non lo promuove.
    """
    __tablename__ = "wa_discovered_chats"
    __table_args__ = (
        # Ri-scansionare lo stesso numero deve essere innocuo (spec 5.3): la
        # seconda passata aggiorna, non duplica.
        #
        # La chiave e' (numero, TITOLO) e non il numero di telefono, perche' il
        # telefono manca in tutti i gruppi e in parte delle 1:1: una unique su
        # phone_hmac lascerebbe senza protezione proprio i casi piu' frequenti.
        # E include number_id perche' due numeri WhatsApp hanno rubriche
        # diverse -- 'Fulvio' nel telefono del negozio e 'Fulvio' in quello
        # personale sono due chat distinte.
        UniqueConstraint("number_id", "chat_title",
                         name="uq_wa_discovered_number_title"),
        # SECONDA rete, e non e' ridondanza. In SQL NULL != NULL: la unique qui
        # sopra NON vede due righe con chat_title NULL -- e chat_title e' NULL
        # proprio nel caso piu' frequente, quando il titolo E' il numero e per
        # P12 non si salva in chiaro (il 39% delle chat di Primero, misurato).
        # Senza questa, ri-scansionare duplicherebbe in silenzio due contatti su
        # cinque, cioe' l'opposto dell'"innocuo" promesso dalla spec 5.3.
        # Le due unique si coprono a vicenda: quando manca il titolo c'e' il
        # numero, quando manca il numero c'e' il titolo.
        UniqueConstraint("number_id", "phone_hmac",
                         name="uq_wa_discovered_number_phone"),
        Index("ix_wa_discovered_number_status", "number_id", "status"),
        Index("ix_wa_discovered_phone_hmac", "phone_hmac"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True,
                                    default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"),
                                           nullable=False)
    number_id: Mapped[str] = mapped_column(String(36), ForeignKey("wa_numbers.id"),
                                           nullable=False)
    # Il titolo COSI' COME APPARE nella sidebar -- tranne quando il titolo E' il
    # numero: in quel caso resta None e il numero va cifrato sotto (P12, stessa
    # regola di WaContact.chat_title). Non e' un caso di bordo: e' il 39% delle
    # chat di Primero e il 14% di un numero personale (misurato, PoC-5).
    chat_title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Fernet, come WaContact.encrypted_phone. Nullable: qui il numero puo'
    # legittimamente mancare.
    encrypted_phone: Mapped[str | None] = mapped_column(Text, nullable=True)
    phone_hmac: Mapped[str | None] = mapped_column(String(64), nullable=True)
    numero_leggibile: Mapped[bool] = mapped_column(Boolean, default=False,
                                                   nullable=False)
    # 'individuale' | 'gruppo' | 'ignoto'. TRI-STATO, non booleano: il PoC-4 ha
    # misurato recall 1/6 per il rilevamento gruppo e un 5% di pannelli che non
    # si aprono nemmeno su chat 1:1 vere. "Non lo so" e' uno stato frequente e
    # va detto: con un booleano quel dubbio diventerebbe una risposta secca, e
    # persone contattabili verrebbero scartate senza lasciare traccia del perche'.
    tipo_chat: Mapped[str] = mapped_column(String(20), default="ignoto",
                                           nullable=False)
    # Quale filtro/etichetta era attivo durante lo scan. Oggi sempre None: le
    # Liste non si sincronizzano su WhatsApp Web e Primero non e' un account
    # Business (PoC-5, 11/08). Il campo resta perche' costa nulla e il giorno in
    # cui il filtro tornera' praticabile serve sapere da dove viene ogni riga.
    source_filtro: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # 'nuovo' | 'promosso' | 'scartato' (spec 5.4). Lo muove la Fase B.
    status: Mapped[str] = mapped_column(String(20), default="nuovo", nullable=False)
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False)


class WaDiscoverRun(Base):
    """Una scansione auto-discover: quando, chi l'ha chiesta, cosa ha raccolto.

    Esiste per rispondere a "perche' stavolta ne ha trovati 12" senza aprire i
    log. Col discover periodico (cantiere 2) diventa l'unica traccia: li'
    nessuno guarda lo schermo mentre gira.

    L'indice unico PARZIALE su (number_id) WHERE stato='running' e' la guardia
    "una scansione alla volta per numero" scritta nel DB e non solo nel codice:
    due click ravvicinati sul bottone non possono aprire due run.
    """
    __tablename__ = "wa_discover_runs"
    __table_args__ = (
        Index("ix_wa_discover_runs_number_started", "number_id", "started_at"),
        Index("uq_wa_discover_runs_una_running_per_numero", "number_id",
              unique=True,
              sqlite_where=text("stato = 'running'"),
              postgresql_where=text("stato = 'running'")),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True,
                                    default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"),
                                           nullable=False)
    number_id: Mapped[str] = mapped_column(String(36), ForeignKey("wa_numbers.id"),
                                           nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    stato: Mapped[str] = mapped_column(String(20), default="running", nullable=False)
    avviato_da: Mapped[str] = mapped_column(String(20), default="manuale", nullable=False)

    salvate: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    aggiornate: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    saltate_gia_note: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    non_verificate: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    dichiarato: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Percentuale 0-100 della raccolta sul dichiarato. Salvata invece che
    # ricalcolata: il conto cambia (l'incrementale ha aggiunto i salti) e una
    # run vecchia deve restare leggibile con la formula del suo tempo.
    copertura: Mapped[int | None] = mapped_column(Integer, nullable=True)
    motivo: Mapped[str] = mapped_column(String(30), default="in_corso", nullable=False)
    sync_letta: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sync_stato: Mapped[str] = mapped_column(String(10), default="ignota", nullable=False)
    errore: Mapped[str | None] = mapped_column(Text, nullable=True)
