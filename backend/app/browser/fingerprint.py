"""
Browser fingerprint configuration per account.
Each account gets a consistent but unique fingerprint to avoid detection.
The fingerprint is deterministically derived from the account ID so it's
stable across restarts (same account = same viewport, same UA, etc.).
"""
import hashlib
import random

COMMON_VIEWPORTS = [
    {"width": 1366, "height": 628},
    {"width": 1440, "height": 760},
    {"width": 1536, "height": 724},
    {"width": 1920, "height": 940},
    {"width": 1280, "height": 660},
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
]

# Realistic WebGL renderer + vendor pairs (ANGLE on Windows/Mac)
WEBGL_PROFILES = [
    {
        "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        "vendor": "Google Inc. (Intel)",
    },
    {
        "renderer": "ANGLE (Intel, Intel(R) HD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        "vendor": "Google Inc. (Intel)",
    },
    {
        "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1060 6GB Direct3D11 vs_5_0 ps_5_0, D3D11)",
        "vendor": "Google Inc. (NVIDIA)",
    },
    {
        "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 2060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        "vendor": "Google Inc. (NVIDIA)",
    },
    {
        "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        "vendor": "Google Inc. (NVIDIA)",
    },
    {
        "renderer": "ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        "vendor": "Google Inc. (AMD)",
    },
    {
        "renderer": "ANGLE (Intel, Intel(R) Iris(TM) Plus Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)",
        "vendor": "Google Inc. (Intel)",
    },
    {
        "renderer": "ANGLE (NVIDIA, NVIDIA GeForce MX250 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        "vendor": "Google Inc. (NVIDIA)",
    },
]

# Per-account timing multiplier: spreads behavioral patterns across accounts.
# A multiplier of 0.85 means ~15% faster on average; 1.20 means ~20% slower.
TIMING_MULTIPLIERS = [0.80, 0.88, 0.95, 1.00, 1.08, 1.15, 1.22, 1.30]


def get_fingerprint(account_id: str) -> dict:
    """
    Return a deterministic fingerprint for an account.
    Same account_id always gets the same fingerprint.
    """
    seed = int(hashlib.md5(account_id.encode()).hexdigest(), 16)
    rng = random.Random(seed)

    viewport = rng.choice(COMMON_VIEWPORTS)
    webgl = rng.choice(WEBGL_PROFILES)

    # Screen size = viewport + realistic browser chrome offset (tab bar + address bar)
    screen_width = viewport["width"]
    screen_height = viewport["height"] + rng.randint(80, 110)

    return {
        "viewport": viewport,
        "user_agent": rng.choice(USER_AGENTS),
        # Geo coherence: pin browser locale+timezone to Italy. Gli account escono da
        # un IP italiano (SIM/proxy IT); un browser dichiarato en-US o fuso New York
        # su IP italiano e' un mismatch che alza il sospetto anti-bot di Instagram.
        # Manteniamo rng.choice su liste a 1 elemento (invece di valori secchi) per
        # NON alterare la sequenza del rng: cosi' i campi deterministici a valle
        # (hardware_concurrency, device_memory, timing_multiplier) restano IDENTICI
        # per ogni account — cambiano solo locale e fuso, niente shift di fingerprint.
        "locale": rng.choice(["it-IT"]),
        "timezone_id": rng.choice(["Europe/Rome"]),
        "hardware_concurrency": rng.choice([4, 8, 12, 16]),
        "device_memory": rng.choice([4, 8]),
        "webgl_renderer": webgl["renderer"],
        "webgl_vendor": webgl["vendor"],
        "screen_width": screen_width,
        "screen_height": screen_height,
        "timing_multiplier": rng.choice(TIMING_MULTIPLIERS),
    }
