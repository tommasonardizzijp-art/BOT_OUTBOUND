"""Pure parsing of Instagram profile URLs / usernames from imported file lines."""
import re

_USERNAME_RE = re.compile(r"^[a-z0-9._]{1,30}$")
# Path segments che NON sono username profilo
_RESERVED = {"p", "reel", "reels", "stories", "explore", "tv", "s", "accounts", "direct"}


def parse_username(token: str) -> str | None:
    """Extract a normalized IG username from a URL, @handle, or bare username.
    Returns None if no valid username can be derived."""
    if not token:
        return None
    token = token.strip()
    if not token:
        return None
    # CSV: prendi la prima colonna
    if "," in token:
        token = token.split(",", 1)[0].strip()
    # URL → primo path segment
    if "instagram.com" in token.lower():
        after = re.split(r"instagram\.com/", token, maxsplit=1, flags=re.IGNORECASE)
        if len(after) < 2:
            return None
        path = after[1].split("?", 1)[0].split("#", 1)[0]
        seg = path.strip("/").split("/")[0]
        token = seg
    token = token.lstrip("@").strip().lower()
    if token in _RESERVED:
        return None
    if not _USERNAME_RE.match(token):
        return None
    return token


def parse_lines(raw: str) -> dict:
    """Parse a multi-line blob. Returns valid (list of (username, raw_line)),
    duplicates count, skipped_invalid count. Dedup is case-insensitive on username."""
    valid: list[tuple[str, str]] = []
    seen: set[str] = set()
    duplicates = 0
    skipped_invalid = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        username = parse_username(line)
        if username is None:
            skipped_invalid += 1
            continue
        if username in seen:
            duplicates += 1
            continue
        seen.add(username)
        valid.append((username, line))
    return {"valid": valid, "duplicates": duplicates, "skipped_invalid": skipped_invalid}
