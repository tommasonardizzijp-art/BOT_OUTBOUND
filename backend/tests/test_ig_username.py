from app.utils.ig_username import parse_username, parse_lines


def test_full_url():
    assert parse_username("https://www.instagram.com/john.doe/") == "john.doe"

def test_url_with_query_and_no_scheme():
    assert parse_username("instagram.com/john_doe?hl=it") == "john_doe"

def test_at_handle():
    assert parse_username("@John_Doe") == "john_doe"

def test_bare_username():
    assert parse_username("john.doe") == "john.doe"

def test_csv_first_column():
    assert parse_username("john.doe,Mario Rossi,note") == "john.doe"

def test_invalid_returns_none():
    assert parse_username("not a username!!") is None
    assert parse_username("") is None
    assert parse_username("https://instagram.com/p/ABC123/") is None  # post, non profilo

def test_parse_lines_dedup_and_skip():
    raw = "john.doe\n@john.doe\n\nhttps://instagram.com/jane/\nbad input!!\n"
    result = parse_lines(raw)
    assert result["valid"] == [("john.doe", "john.doe"), ("jane", "https://instagram.com/jane/")]
    assert result["duplicates"] == 1
    assert result["skipped_invalid"] == 1
