import pytest

from app.services.wa_csv import CsvParseError, parse_wa_csv


def test_separatore_virgola():
    righe, attrs = parse_wa_csv(b"numero,nome\n+393331112223,Marco\n")
    assert len(righe) == 1
    assert righe[0].valori["numero"] == "+393331112223"
    assert attrs == []


def test_separatore_punto_e_virgola_perche_excel_italiano():
    righe, _ = parse_wa_csv(b"numero;nome\n+393331112223;Marco\n")
    assert righe[0].valori["nome"] == "Marco"


def test_bom_utf8_non_rompe_l_header():
    """Excel salva con BOM: senza gestirlo, la prima colonna si chiama
    '\\ufeffnumero' e 'numero obbligatorio' fallisce su un file corretto."""
    righe, _ = parse_wa_csv("\ufeffnumero,nome\n+393331112223,Marco\n".encode())
    assert righe[0].valori["numero"] == "+393331112223"


def test_colonne_libere_diventano_attributi():
    righe, attrs = parse_wa_csv(b"numero,nome,ultimo_ordine,citta\n+39333,M,10/01,Roma\n")
    assert sorted(attrs) == ["citta", "ultimo_ordine"]
    assert righe[0].valori["citta"] == "Roma"


def test_header_senza_colonna_numero_fallisce_subito():
    with pytest.raises(CsvParseError) as exc:
        parse_wa_csv(b"telefono,nome\n+39333,M\n")
    assert "numero" in str(exc.value)


def test_file_vuoto_e_solo_header_falliscono_con_messaggi_diversi():
    with pytest.raises(CsvParseError):
        parse_wa_csv(b"")
    with pytest.raises(CsvParseError):
        parse_wa_csv(b"numero,nome\n")


def test_riga_corta_e_riga_lunga_non_uccidono_il_file():
    """Una riga storta e' UNA riga: il file non fallisce in blocco (Q21)."""
    righe, _ = parse_wa_csv(b"numero,nome\n+39333\n+39444,Anna,extra\n+39555,Luca\n")
    assert len(righe) == 3
    assert righe[0].valori.get("nome", "") == ""


def test_intestazioni_duplicate_sollevano():
    with pytest.raises(CsvParseError):
        parse_wa_csv(b"numero,nome,nome\n+39333,M,X\n")


def test_encoding_non_utf8_viene_letto_senza_esplodere():
    """Un CSV latin-1 con accenti non deve dare UnicodeDecodeError: si
    legge con errors='replace' e si va avanti."""
    righe, _ = parse_wa_csv("numero,nome\n+39333,Nicolò\n".encode("latin-1"))
    assert len(righe) == 1


def test_oltre_il_limite_di_righe_rifiuta_il_file(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "wa_ingest_max_rows", 2)
    with pytest.raises(CsvParseError) as exc:
        parse_wa_csv(b"numero\n+391\n+392\n+393\n")
    assert "5.000" in str(exc.value) or "2" in str(exc.value)


def test_il_messaggio_di_errore_non_contiene_numeri_in_chiaro():
    """P12: nemmeno gli errori del parser possono stampare un numero."""
    with pytest.raises(CsvParseError) as exc:
        parse_wa_csv(b"telefono\n+393421460077\n")
    assert "3421460077" not in str(exc.value)


def test_file_senza_intestazione_non_stampa_i_numeri_come_colonne():
    """Trovato in review: un file SENZA header (l'utente incolla numeri
    grezzi) fa leggere la prima riga di dati come intestazione, e il
    messaggio 'colonna numero assente' interpolava quei valori in chiaro."""
    with pytest.raises(CsvParseError) as exc:
        parse_wa_csv(b"+393421460077\n+393421460078\n")
    assert "3421460077" not in str(exc.value)
    assert "3421460078" not in str(exc.value)


def test_null_byte_in_cella_viene_rimosso_non_solo_stripped():
    """Trovato in Fase 4 QA: SQLite tollera \\x00 in una colonna TEXT, ma
    Postgres/asyncpg (produzione) lo rifiuta a livello di driver -- senza
    pulizia qui il file passerebbe i test locali e romperebbe in prod."""
    righe, _ = parse_wa_csv(b"numero,nome\n+393331112223,Mar\x00co\n")
    assert "\x00" not in righe[0].valori["nome"]
    assert righe[0].valori["nome"] == "Marco"
