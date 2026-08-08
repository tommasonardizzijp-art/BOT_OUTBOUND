"""C.2: lock Redis cross-processo sul profilo browser IG. Pattern e stile
ricalcati apposta da test_wa_profile_lock.py (stesso schema token+heartbeat,
stesso uso di fakeredis) — le differenze deliberate rispetto a WA (TTL corto
+ rinnovo automatico, fail-closed sull'acquisizione) hanno ciascuna un test
dedicato che non esiste nel file WA."""
import asyncio
import uuid

import pytest
import pytest_asyncio
import fakeredis.aioredis

from app.browser import profile_lock


@pytest.fixture
def fake_redis(monkeypatch):
    client = fakeredis.aioredis.FakeRedis()

    async def _fake_pool():
        return client

    monkeypatch.setattr(profile_lock.arq, "create_pool", lambda *_a, **_k: _fake_pool())
    return client


@pytest.mark.asyncio
async def test_held_acquisisce_e_rilascia(fake_redis):
    async with profile_lock.held("acc-1"):
        assert await fake_redis.exists("ig:profile-lock:acc-1")
    assert not await fake_redis.exists("ig:profile-lock:acc-1")


@pytest.mark.asyncio
async def test_held_solleva_se_gia_occupato(fake_redis):
    async with profile_lock.held("acc-1"):
        with pytest.raises(profile_lock.AccountBrowserBusy):
            async with profile_lock.held("acc-1"):
                pass


@pytest.mark.asyncio
async def test_held_non_rilascia_lock_altrui_scaduto(fake_redis):
    """Se il TTL e' scaduto e un altro possessore ha gia' preso il lock,
    l'uscita del primo `held` NON deve cancellare il lock del secondo --
    e' il motivo per cui si confronta un token, non un DELETE incondizionato."""
    await fake_redis.set("ig:profile-lock:acc-1", "token-vecchio", ex=1)
    await asyncio.sleep(1.1)
    async with profile_lock.held("acc-1") as token_nuovo:
        assert token_nuovo != "token-vecchio"
        current = await fake_redis.get("ig:profile-lock:acc-1")
        assert profile_lock._token_di(current) == token_nuovo
    assert not await fake_redis.exists("ig:profile-lock:acc-1")


@pytest.mark.asyncio
async def test_held_non_rilascia_lock_altrui_scritto_a_sessione_viva(fake_redis):
    """Rilievo review: il test precedente non aveva mai un SECONDO possessore
    mentre eravamo DENTRO `held()` (chiave vuota, la riprendevamo e liberavamo
    noi) — non avrebbe mai potuto scoprire una release non-token-aware. Qui un
    secondo processo scrive il PROPRIO token sulla stessa chiave MENTRE il
    nostro `held()` e' ancora aperto (TTL nostro scaduto/bypassato, l'altro
    acquisisce legittimamente dal suo punto di vista). All'uscita, il rilascio
    del codice REALE di `held()` deve vedere che il token non e' piu' il
    nostro e non toccare la chiave."""
    async with profile_lock.held("acc-1") as token_mio:
        assert await fake_redis.exists("ig:profile-lock:acc-1")
        # Un secondo processo "vero" (nei limiti del test: stessa chiave,
        # nuovo token, nessuna chiamata a held() nostra coinvolta) scrive
        # sopra la nostra entry mentre siamo ancora dentro il blocco.
        await fake_redis.set("ig:profile-lock:acc-1", profile_lock._valore("token-altrui"), ex=180)

    # Il rilascio (finally di held(), codice di produzione, non simulato) deve
    # aver confrontato il token e aver lasciato stare la chiave dell'altro.
    current = await fake_redis.get("ig:profile-lock:acc-1")
    assert current is not None, "il lock del secondo possessore e' sparito"
    assert profile_lock._token_di(current) == "token-altrui"


@pytest.mark.asyncio
async def test_held_rilascia_anche_se_il_corpo_solleva(fake_redis):
    """Il rilascio e' nel `finally`: un'eccezione a meta' sessione (es. il
    browser crasha durante l'uso) non deve lasciare il lock orfano."""
    with pytest.raises(RuntimeError):
        async with profile_lock.held("acc-1"):
            assert await fake_redis.exists("ig:profile-lock:acc-1")
            raise RuntimeError("crash a meta' sessione")
    assert not await fake_redis.exists("ig:profile-lock:acc-1")


@pytest.mark.asyncio
async def test_held_ttl_scade_se_il_processo_muore_senza_rilasciare(fake_redis):
    """Simula un crash: __aenter__ senza mai raggiungere __aexit__ (nessun
    rilascio esplicito). Il lock deve comunque sparire da solo al TTL, non
    restare bloccato per sempre — a differenza del lock WA (90 min fissi
    senza rinnovo), qui il TTL e' corto apposta."""
    cm = profile_lock.held("acc-1", ttl_s=1)
    await cm.__aenter__()  # mai chiamato __aexit__: simula un processo morto
    assert await fake_redis.exists("ig:profile-lock:acc-1")
    await asyncio.sleep(1.2)
    assert not await fake_redis.exists("ig:profile-lock:acc-1"), (
        "un lock orfano che non scade blocca l'account fino a un intervento manuale"
    )
    # E un nuovo processo puo' ripartire senza bisogno di una DELETE a mano.
    async with profile_lock.held("acc-1"):
        pass


@pytest.mark.asyncio
async def test_renew_rimette_il_ttl_pieno(fake_redis):
    """L'heartbeat deve spostare la scadenza in avanti: senza, un TTL corto
    (qui 180s di default) scadrebbe a meta' di una sessione lunga (es. browse
    manuale fino a 60 min) e un altro processo entrerebbe."""
    async with profile_lock.held("acc-1", ttl_s=5) as token:
        assert await fake_redis.ttl("ig:profile-lock:acc-1") <= 5
        assert await profile_lock.renew("acc-1", token, ttl_s=180) is True
        assert await fake_redis.ttl("ig:profile-lock:acc-1") > 5


@pytest.mark.asyncio
async def test_renew_non_tocca_lock_di_altri(fake_redis):
    await fake_redis.set("ig:profile-lock:acc-1", "token-di-un-altro", ex=600)
    assert await profile_lock.renew("acc-1", "il-mio-token-vecchio") is False
    assert (await fake_redis.get("ig:profile-lock:acc-1")).decode() == "token-di-un-altro"


@pytest.mark.asyncio
async def test_renew_non_solleva_se_redis_non_risponde(monkeypatch):
    """Rinnovo: fail-OPEN deliberato (diverso dall'acquisizione, vedi sotto).
    Un blip Redis a meta' sessione non deve abbattere un browser gia' aperto."""
    async def _pool_rotto(*_a, **_k):
        raise ConnectionError("redis irraggiungibile")
    monkeypatch.setattr(profile_lock.arq, "create_pool", _pool_rotto)

    assert await profile_lock.renew("acc-1", "token") is False


@pytest.mark.asyncio
async def test_held_fail_closed_se_redis_non_risponde_in_connessione(monkeypatch):
    """Acquisizione: fail-CLOSED deliberato (decisione esplicita in review).
    Redis irraggiungibile in fase di connessione -> AccountBrowserBusy, MAI
    un'apertura silenziosa del browser."""
    async def _pool_rotto(*_a, **_k):
        raise ConnectionError("redis irraggiungibile")
    monkeypatch.setattr(profile_lock.arq, "create_pool", _pool_rotto)

    with pytest.raises(profile_lock.AccountBrowserBusy):
        async with profile_lock.held("acc-1"):
            pytest.fail("non deve mai entrare nel blocco protetto se Redis non risponde")


@pytest.mark.asyncio
async def test_held_fail_closed_se_redis_non_risponde_durante_set(fake_redis, monkeypatch):
    """Stesso fail-closed, ma il blip arriva durante il SET (non la connessione)."""
    async def _set_rotto(*_a, **_k):
        raise ConnectionError("redis irraggiungibile a meta' comando")
    monkeypatch.setattr(fake_redis, "set", _set_rotto)

    with pytest.raises(profile_lock.AccountBrowserBusy):
        async with profile_lock.held("acc-1"):
            pytest.fail("non deve mai entrare nel blocco protetto se Redis non risponde")


# NOTA sul fail-closed: non serve una prova del nove separata come sopra.
# `test_held_fail_closed_se_redis_non_risponde_in_connessione` e
# `test_held_fail_closed_se_redis_non_risponde_durante_set` GIA' chiamano
# `held()` vero contro un Redis vero (rotto) e verificano che sollevi
# `AccountBrowserBusy` — se il fail-closed venisse tolto (es. `except
# Exception: pass` al posto del `raise`), quei due test diventerebbero rossi
# da soli: sono gia' la prova del nove di se stessi. Una versione "fail-open
# scritta ad-hoc nel test" (rimossa da qui) non passa mai dal codice di
# produzione: prova solo che una implementazione diversa si comporta
# diversamente, non che QUESTA implementazione si difende.


@pytest_asyncio.fixture
async def _redis_o_skip():
    import arq
    from app.services.work_enqueue import arq_redis_settings
    try:
        pool = await arq.create_pool(arq_redis_settings())
        await pool.ping()
        await pool.aclose()
    except Exception:
        pytest.skip("Redis non raggiungibile in questo ambiente")


@pytest.mark.asyncio
async def test_held_vero_contro_redis_reale(_redis_o_skip):
    """Senza monkeypatch: verifica la mutua esclusione vera, non solo la
    logica mockata sopra."""
    account_id = f"lock-test-{uuid.uuid4().hex[:8]}"
    async with profile_lock.held(account_id):
        with pytest.raises(profile_lock.AccountBrowserBusy):
            async with profile_lock.held(account_id):
                pass
    async with profile_lock.held(account_id):
        pass


@pytest.mark.asyncio
async def test_held_with_renew_rinnova_in_background_e_rilascia(fake_redis):
    """held_with_renew: il TTL iniziale e' cortissimo (1s) ma il rinnovo
    automatico (ogni 0.3s) deve tenerlo vivo per tutta la durata del blocco,
    anche oltre il TTL iniziale — questa e' la garanzia che rende sicuro un
    TTL corto per sessioni lunghe (browse manuale, login manuale)."""
    async with profile_lock.held_with_renew("acc-1", ttl_s=1, renew_every_s=0.3) as token:
        await asyncio.sleep(1.5)  # oltre il TTL iniziale
        current = await fake_redis.get("ig:profile-lock:acc-1")
        assert current is not None, "il lock e' scaduto nonostante il rinnovo automatico"
        assert profile_lock._token_di(current) == token
    assert not await fake_redis.exists("ig:profile-lock:acc-1")


@pytest.mark.asyncio
async def test_held_with_renew_prova_del_nove_senza_rinnovo_scadrebbe(fake_redis):
    """Prova del nove: `held()` nudo (senza renew) con lo stesso TTL cortissimo
    NON sopravvive alla stessa attesa — dimostra che il rinnovo automatico del
    test precedente sta facendo davvero qualcosa, non e' un test che passa a
    prescindere."""
    async with profile_lock.held("acc-1", ttl_s=1):
        await asyncio.sleep(1.5)
        assert not await fake_redis.exists("ig:profile-lock:acc-1")


# ───────────── Rilievo review: il rinnovo che perde il possesso ─────────────
# _renew_loop ignorava il valore di ritorno del rinnovo: se il token non era
# piu' nostro (un altro processo aveva ripreso il lock) o Redis era giu' da
# tempo, il loop si limitava a loggare e proseguiva all'infinito — due
# processi entrambi convinti di possedere il profilo. I test sotto verificano
# la correzione: `held_with_renew` distingue i due casi e, quando il possesso
# e' perso (o troppi rinnovi di fila falliscono), solleva `AccountBrowserBusy`
# alla chiusura del blocco del chiamante — codice REALE, nessuna simulazione:
# un secondo possessore vero scrive sulla chiave mentre siamo dentro il blocco.

@pytest.mark.asyncio
async def test_held_with_renew_possesso_perso_solleva_busy_alla_chiusura(fake_redis):
    """Un secondo processo scrive il proprio token sulla chiave MENTRE siamo
    dentro held_with_renew (renew_every_s abbastanza corto da farlo scoprire
    prima che il blocco del chiamante finisca). Il blocco del chiamante non
    solleva nulla di suo: held_with_renew deve accorgersi del cambio di token
    al prossimo rinnovo e sollevare AccountBrowserBusy alla chiusura."""
    with pytest.raises(profile_lock.AccountBrowserBusy, match="possesso"):
        async with profile_lock.held_with_renew("acc-1", ttl_s=180, renew_every_s=0.1):
            await asyncio.sleep(0.05)
            # Un altro processo "vero" riprende il lock (dal suo punto di
            # vista era libero/scaduto) mentre il nostro blocco e' aperto.
            await fake_redis.set("ig:profile-lock:acc-1", profile_lock._valore("token-altrui"), ex=180)
            await asyncio.sleep(0.3)  # lascia girare almeno un ciclo di rinnovo

    # La chiave del secondo possessore non e' stata toccata dal nostro rilascio.
    current = await fake_redis.get("ig:profile-lock:acc-1")
    assert current is not None
    assert profile_lock._token_di(current) == "token-altrui"


@pytest.mark.asyncio
async def test_held_with_renew_possesso_perso_non_maschera_eccezione_del_chiamante(fake_redis):
    """Se il blocco del chiamante solleva una SUA eccezione, quella ha la
    precedenza: il possesso perso viene comunque rilevato e loggato (vedi
    stderr del test), ma non sostituisce l'errore originale con uno che
    parla solo del lock e nasconde la causa vera."""
    class _ErroreDelChiamante(Exception):
        pass

    with pytest.raises(_ErroreDelChiamante):
        async with profile_lock.held_with_renew("acc-1", ttl_s=180, renew_every_s=0.1):
            await asyncio.sleep(0.05)
            await fake_redis.set("ig:profile-lock:acc-1", profile_lock._valore("token-altrui"), ex=180)
            await asyncio.sleep(0.3)
            raise _ErroreDelChiamante("il browser e' crashato per conto suo")


@pytest.mark.asyncio
async def test_held_with_renew_singolo_blip_di_connessione_non_interrompe(fake_redis, monkeypatch):
    """Fail-open per un numero limitato di blip consecutivi: un singolo
    hiccup Redis durante un rinnovo non deve abbattere una sessione viva."""
    reale_set = fake_redis.set
    chiamate = {"n": 0}

    async def _set_rotto_una_volta(*a, **k):
        chiamate["n"] += 1
        if chiamate["n"] == 1:
            raise ConnectionError("blip isolato")
        return await reale_set(*a, **k)

    async with profile_lock.held_with_renew(
        "acc-1", ttl_s=180, renew_every_s=0.1, max_consecutive_renew_errors=2,
    ) as token:
        monkeypatch.setattr(fake_redis, "set", _set_rotto_una_volta)
        await asyncio.sleep(0.35)  # abbastanza per un paio di cicli di rinnovo
    # Nessuna AccountBrowserBusy sollevata: la sessione e' finita pulita
    # nonostante il blip isolato — il lock resta comunque nostro fino qui.
    assert chiamate["n"] >= 1


@pytest.mark.asyncio
async def test_held_with_renew_troppi_blip_consecutivi_trattati_come_perso(fake_redis, monkeypatch):
    """Oltre max_consecutive_renew_errors blip di fila, il lock e' trattato
    come perso (fail-closed) anche se nessun secondo possessore si e' mai
    fatto vivo — perche' non lo sappiamo, e non saperlo e' gia' il rischio."""
    async def _set_sempre_rotto(*_a, **_k):
        raise ConnectionError("redis irraggiungibile a lungo")

    with pytest.raises(profile_lock.AccountBrowserBusy, match="possesso"):
        async with profile_lock.held_with_renew(
            "acc-1", ttl_s=180, renew_every_s=0.1, max_consecutive_renew_errors=2,
        ):
            monkeypatch.setattr(fake_redis, "set", _set_sempre_rotto)
            await asyncio.sleep(0.35)  # >= 2 cicli di rinnovo, tutti rotti


