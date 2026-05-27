"""
Human-like timing utilities.
All delays use log-normal distributions to avoid the "machine-gun" pattern
of uniform random delays that Instagram's anti-bot systems can detect.
"""
import random
import math
from app.config import settings


def random_delay_seconds() -> float:
    """
    Return a delay sampled from a log-normal distribution with high sigma (0.7)
    for more natural variance: mostly short delays, occasional longer ones.
    """
    min_s = settings.min_delay_seconds
    max_s = settings.max_delay_seconds
    mid = (min_s + max_s) / 2
    mu = math.log(mid)
    sigma = 0.7  # Higher sigma = more spread/variance (more human-like)
    delay = random.lognormvariate(mu, sigma)
    return max(min_s, min(max_s, delay))


def distraction_pause_seconds() -> float:
    """
    Occasional longer pause simulating human distraction.

    If DISTRACTION_PAUSE_MIN/MAX_SECONDS are set in .env, those values are used.
    Otherwise auto-scales with MAX_DELAY_SECONDS:
      - low  = max(60s,  max_delay * 3)
      - high = min(900s, max_delay * 10)

    Examples:
      MAX_DELAY_SECONDS=45  → auto range  135s – 450s  (2–7 min)
      MAX_DELAY_SECONDS=120 → auto range  360s – 900s  (6–15 min)
      MAX_DELAY_SECONDS=480 → auto range  180s – 900s  (3–15 min, capped)
    """
    max_s = settings.max_delay_seconds

    if settings.distraction_pause_min_seconds > 0:
        low = float(settings.distraction_pause_min_seconds)
    else:
        low = max(60.0, max_s * 3.0)

    if settings.distraction_pause_max_seconds > 0:
        high = float(settings.distraction_pause_max_seconds)
    else:
        high = min(900.0, max(low + 30.0, max_s * 10.0))

    # Safety: ensure low < high
    if high <= low:
        high = low + 60.0

    t = random.lognormvariate(math.log((low + high) / 2), 0.4)
    return max(low, min(high, t))


def should_take_distraction_pause() -> bool:
    """Probabilistic check for a distraction pause (configurable, default 6%)."""
    if settings.distraction_pause_probability <= 0:
        return False
    return random.random() < settings.distraction_pause_probability


def session_break_seconds() -> float:
    """Break between sending sessions — lognormal for more natural variance."""
    min_s = settings.session_break_min_minutes * 60
    max_s = settings.session_break_max_minutes * 60
    mid = (min_s + max_s) / 2
    # sigma=0.6 gives wider spread (~30–60 min range fully utilized) vs 0.4 which clusters near mid
    break_s = random.lognormvariate(math.log(mid), 0.6)
    return max(min_s, min(max_s, break_s))


def session_message_count() -> int:
    """How many messages to send in one session before taking a break."""
    return random.randint(settings.session_min_messages, settings.session_max_messages)


def typing_delay_ms(char_count: int) -> float:
    """
    Simulate typing delay in ms for a message of char_count characters.
    Base speed: 80-200ms per character, with natural variation.
    """
    base_ms_per_char = random.uniform(80, 200)
    total_ms = base_ms_per_char * char_count
    # Add occasional mid-message pauses (thinking)
    pauses = random.randint(0, max(1, char_count // 30))
    total_ms += pauses * random.uniform(500, 2000)
    return total_ms


def pre_dm_browse_seconds() -> float:
    """Time spent browsing target's profile — lognormal around 8s, range 4-20s."""
    t = random.lognormvariate(math.log(8), 0.5)
    return max(4, min(20, t))


def extended_pre_dm_browse_seconds() -> float:
    """Extended browsing for fresh / low-warmup accounts — lognormal around 180s, range 90-360s.
    Reduces 'login → instant DM' bot pattern that triggers IG behavior model on young accounts."""
    t = random.lognormvariate(math.log(180), 0.4)
    return max(90, min(360, t))


def initial_session_browse_seconds() -> float:
    """Ambient feed browse at session start (before first DM). Lognormal around 50s, range 30-90s."""
    t = random.lognormvariate(math.log(50), 0.4)
    return max(30, min(90, t))


def post_dm_dwell_seconds() -> float:
    """Time to linger in DM thread after pressing Enter — simulates user reading own message + thread. 3-10s."""
    return random.uniform(3, 10)
