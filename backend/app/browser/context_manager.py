"""
Browser context pool manager.

Each Instagram account gets its own persistent Chromium profile stored on disk.
This ensures cookies, localStorage, and session data persist across bot runs,
making the account look like a returning user rather than a fresh browser.

Uses Patchright (undetected Playwright fork) for anti-detection.
"""
import asyncio
import os
from contextlib import asynccontextmanager
from urllib.parse import urlparse
from loguru import logger
from app.config import settings
from app.browser.fingerprint import get_fingerprint

# Sentinel: distinguishes "proxy not provided, fetch from DB" (worker path, runs
# on the main loop) from "proxy explicitly None = no proxy" (resolved by the caller
# on the main loop and passed down into a thread-private loop, e.g. manual login).
_UNSET = object()

# Per-account mutex: prevents two concurrent browser launches for the same
# Chromium profile directory, which causes about:blank pages and session corruption.
# Keyed by (account_id) -> (loop, Lock). An asyncio.Lock binds to the loop on which
# it is first awaited; the manual-login/browse sync wrappers spin a NEW loop in a
# thread, so a lock created on the main loop would raise "attached to a different
# loop" there. Storing the owning loop lets us hand out a fresh lock per loop.
_account_locks: dict[str, tuple] = {}


def _get_account_lock(account_id: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    entry = _account_locks.get(account_id)
    if entry is None or entry[0] is not loop:
        lock = asyncio.Lock()
        _account_locks[account_id] = (loop, lock)
        return lock
    return entry[1]


def parse_proxy_url(url: str | None) -> dict | None:
    """
    Parse proxy URL into Patchright proxy dict.
    Accepts: http://user:pass@host:port  |  http://host:port  |  socks5://...
    Returns: {"server": "...", "username": "...", "password": "..."} or None.
    """
    if not url or not url.strip():
        return None
    p = urlparse(url.strip())
    if not p.hostname or not p.port:
        logger.warning(f"Proxy URL malformed (host/port missing): {url!r}")
        return None
    scheme = p.scheme or "http"
    out = {"server": f"{scheme}://{p.hostname}:{p.port}"}
    if p.username:
        out["username"] = p.username
    if p.password:
        out["password"] = p.password
    return out


async def _fetch_account_proxy(account_id: str) -> str | None:
    """Lookup proxy URL for account from DB. Returns None if no account / no proxy."""
    from app.database import AsyncSessionLocal
    from app.models.account import InstagramAccount
    from sqlalchemy import select
    async with AsyncSessionLocal() as db:
        r = await db.execute(select(InstagramAccount.proxy).where(InstagramAccount.id == account_id))
        return r.scalar_one_or_none()


def _import_async_playwright():
    try:
        from patchright.async_api import async_playwright
    except ImportError:
        raise ImportError(
            "Patchright is not installed. Run: pip install patchright && patchright install chromium"
        )
    return async_playwright


async def _prepare_launch(account_id: str, headless: bool | None, proxy_url=_UNSET) -> tuple[dict, dict]:
    """Build (launch_kwargs, fingerprint) for a Patchright persistent context.
    Validates proxy and sets --no-proxy-server when no proxy is configured.

    proxy_url: pass a value (str or None) already resolved on the main loop to skip
    the DB lookup here — required when this runs inside a thread-private event loop
    (manual login/browse), because the shared async DB pool is bound to the main loop
    and querying it from another loop raises "Future attached to a different loop".
    Left as _UNSET (worker path on the main loop), the proxy is fetched from the DB."""
    profile_dir = os.path.join(settings.browser_profiles_dir, account_id)
    os.makedirs(profile_dir, exist_ok=True)

    # Remove Chromium single-instance lock files left by a previous crashed/killed session.
    # If present, Chrome detects "existing session", forwards the launch to the ghost PID,
    # and exits immediately — causing TargetClosedError before patchright can connect.
    for lock_file in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        lock_path = os.path.join(profile_dir, lock_file)
        try:
            if os.path.exists(lock_path):
                os.remove(lock_path)
                logger.debug(f"Removed stale {lock_file} for account {account_id[:8]}…")
        except OSError as e:
            logger.warning(f"Could not remove {lock_file} for {account_id[:8]}…: {e}")

    fingerprint = get_fingerprint(account_id)
    if proxy_url is _UNSET:
        proxy_url = await _fetch_account_proxy(account_id)
    proxy_cfg = parse_proxy_url(proxy_url)
    if proxy_url and not proxy_cfg:
        logger.error(
            f"Account {account_id}: proxy configured but unparseable — refusing to launch browser "
            f"to avoid leaking real IP. Fix proxy field or remove it."
        )
        raise ValueError(f"Proxy URL malformed for account {account_id}: {proxy_url!r}")

    effective_headless = settings.headless if headless is None else headless

    chromium_args = [
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
        f"--js-flags=--harmony",
    ]
    launch_kwargs = dict(
        user_data_dir=profile_dir,
        headless=effective_headless,
        viewport=fingerprint["viewport"],
        user_agent=fingerprint["user_agent"],
        locale=fingerprint["locale"],
        timezone_id=fingerprint["timezone_id"],
        args=chromium_args,
        ignore_default_args=["--enable-automation"],
    )
    if proxy_cfg:
        launch_kwargs["proxy"] = proxy_cfg
        logger.info(f"Browser for account {account_id} using proxy {proxy_cfg['server']}")
    else:
        # Force direct connection — without this Chromium auto-detects Windows
        # system proxy (WPAD/PAC) which can cause ERR_PROXY_CONNECTION_FAILED
        # if stale or interfere with local hotspot routing.
        chromium_args.append("--no-proxy-server")
        logger.warning(
            f"Browser for account {account_id} launching WITHOUT proxy "
            f"— traffic exits from local IP. Set account.proxy to route via mobile/residential IP."
        )
    return launch_kwargs, fingerprint


@asynccontextmanager
async def get_browser_context(account_id: str, headless: bool | None = None, proxy_url=_UNSET):
    """
    Context manager that provides a Patchright browser context for the given account.
    The browser profile is stored persistently at {BROWSER_PROFILES_DIR}/{account_id}/

    Acquires a per-account asyncio lock so only one browser instance per account
    can be open at any time (defense-in-depth against duplicate ARQ jobs).

    Pass headless=False to override default for manual browse sessions.

    Usage:
        async with get_browser_context(account_id) as context:
            page = await context.new_page()
            ...
    """
    async_playwright = _import_async_playwright()

    lock = _get_account_lock(account_id)
    async with lock:
        launch_kwargs, fingerprint = await _prepare_launch(account_id, headless, proxy_url)

        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(**launch_kwargs)
            await context.add_init_script(_build_fingerprint_script(fingerprint))

            logger.debug(
                f"Browser context opened for account {account_id} | "
                f"viewport={fingerprint['viewport']} | "
                f"webgl={fingerprint['webgl_renderer'][:40]}..."
            )
            try:
                yield context
            finally:
                await context.close()
                logger.debug(f"Browser context closed for account {account_id}")


class BrowserSession:
    """
    Long-lived Patchright browser session for one account, kept alive across
    multiple DMs in a single sending session.

    Unlike `get_browser_context` (one-shot async-with), BrowserSession is opened
    explicitly via `await session.open()` and closed via `await session.close()`.
    The per-account asyncio lock is held for the whole lifetime — so concurrent
    workers for the same account queue (correct behavior).

    Usage:
        session = BrowserSession(account_id)
        await session.open()
        try:
            page = session.page  # InstagramPage instance
            await page.ensure_logged_in(account_id)
            await page.browse_feed(120)
            await page.send_dm("target", "hello")
        finally:
            await session.close()

    Cleanup is idempotent and safe to call from `finally` blocks even if open() raised.
    """

    def __init__(self, account_id: str, headless: bool | None = None):
        self.account_id = account_id
        self.headless = headless
        self._lock: asyncio.Lock | None = None
        self._lock_acquired = False
        self._pw_cm = None  # async_playwright() context manager instance
        self._pw = None
        self._context = None
        self._page_obj = None  # InstagramPage instance

    async def open(self) -> "BrowserSession":
        from app.browser.fingerprint import get_fingerprint
        from app.browser.instagram_page import InstagramPage
        from app.database import AsyncSessionLocal
        from app.models.account import InstagramAccount
        from sqlalchemy import select
        from datetime import datetime

        async_playwright = _import_async_playwright()

        # Decide extended_browse before launch (mirrors dm_sender logic)
        timing_multiplier = get_fingerprint(self.account_id).get("timing_multiplier", 1.0)
        extended_browse = False
        async with AsyncSessionLocal() as db:
            r = await db.execute(select(InstagramAccount).where(InstagramAccount.id == self.account_id))
            acc = r.scalar_one_or_none()
            if acc:
                age_days = (datetime.utcnow() - acc.created_at).days if acc.created_at else 999
                if age_days < 7 or (1 <= acc.warmup_day <= 6):
                    extended_browse = True

        self._lock = _get_account_lock(self.account_id)
        await self._lock.acquire()
        self._lock_acquired = True

        try:
            launch_kwargs, fingerprint = await _prepare_launch(self.account_id, self.headless)
            self._pw_cm = async_playwright()
            self._pw = await self._pw_cm.__aenter__()
            self._context = await self._pw.chromium.launch_persistent_context(**launch_kwargs)
            await self._context.add_init_script(_build_fingerprint_script(fingerprint))

            self._page_obj = InstagramPage(
                self._context,
                timing_multiplier=timing_multiplier,
                extended_browse=extended_browse,
            )

            logger.info(
                f"[BrowserSession] Opened for account {self.account_id[:8]}… | "
                f"viewport={fingerprint['viewport']} | extended_browse={extended_browse}"
            )
            return self
        except Exception:
            await self.close()
            raise

    @property
    def context(self):
        return self._context

    @property
    def page(self):
        return self._page_obj

    async def close(self) -> None:
        """Idempotent cleanup. Safe to call even after a failed open()."""
        # 1. Close browser context
        if self._context is not None:
            try:
                await self._context.close()
            except Exception as e:
                logger.warning(f"[BrowserSession] context.close() failed: {e}")
            self._context = None

        # 2. Stop playwright
        if self._pw_cm is not None:
            try:
                await self._pw_cm.__aexit__(None, None, None)
            except Exception as e:
                logger.warning(f"[BrowserSession] playwright stop failed: {e}")
            self._pw_cm = None
            self._pw = None

        self._page_obj = None

        # 3. Release account lock (always last)
        if self._lock is not None and self._lock_acquired:
            try:
                self._lock.release()
            except RuntimeError:
                pass  # already released
            self._lock_acquired = False
            logger.debug(f"[BrowserSession] Closed for account {self.account_id[:8]}…")


def _build_fingerprint_script(fp: dict) -> str:
    """
    Build the JavaScript init script injected into every page before any site JS runs.

    Overrides:
    - Canvas getImageData + toDataURL: adds per-render noise → unique canvas hash
    - navigator.hardwareConcurrency / deviceMemory: realistic per-account values
    - window.screen (width/height/availWidth/availHeight): matches viewport, not physical monitor
    - WebGL RENDERER + VENDOR strings: per-account GPU profile from a realistic pool
    - AudioContext: adds sub-perceptible noise to channel data → unique audio hash
    - Font metric noise: slight canvas text width variation → breaks font enumeration
    """
    hw = fp["hardware_concurrency"]
    dm = fp["device_memory"]
    sw = fp["screen_width"]
    sh = fp["screen_height"]
    vw = fp["viewport"]["width"]
    vh = fp["viewport"]["height"]
    wr = fp["webgl_renderer"].replace("\\", "\\\\").replace('"', '\\"')
    wv = fp["webgl_vendor"].replace("\\", "\\\\").replace('"', '\\"')

    return f"""
(function() {{
    // ── Canvas: getImageData noise ──
    const _origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
    CanvasRenderingContext2D.prototype.getImageData = function(x, y, w, h) {{
        const imageData = _origGetImageData.call(this, x, y, w, h);
        const data = imageData.data;
        const noise = Math.floor(Math.random() * 3);
        for (let i = 0; i < data.length; i += 4) {{
            data[i] = Math.min(255, data[i] + noise);
        }}
        return imageData;
    }};

    // ── Canvas: toDataURL noise ──
    const _origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function(...args) {{
        const ctx = this.getContext('2d');
        if (ctx) {{
            const noise = Math.random() * 0.001;
            ctx.fillStyle = `rgba(0,0,0,${{noise}})`;
            ctx.fillRect(0, 0, 1, 1);
        }}
        return _origToDataURL.apply(this, args);
    }};

    // ── Canvas: toBlob noise ──
    const _origToBlob = HTMLCanvasElement.prototype.toBlob;
    HTMLCanvasElement.prototype.toBlob = function(callback, ...args) {{
        const ctx = this.getContext('2d');
        if (ctx) {{
            ctx.fillStyle = `rgba(0,0,0,${{Math.random() * 0.001}})`;
            ctx.fillRect(0, 0, 1, 1);
        }}
        return _origToBlob.call(this, callback, ...args);
    }};

    // ── Canvas: measureText font noise ──
    // Adds sub-pixel variation to text measurements → breaks font enumeration fingerprinting
    const _origMeasureText = CanvasRenderingContext2D.prototype.measureText;
    CanvasRenderingContext2D.prototype.measureText = function(text) {{
        const metrics = _origMeasureText.call(this, text);
        const _origWidth = metrics.width;
        const noise = (Math.random() - 0.5) * 0.02;
        Object.defineProperty(metrics, 'width', {{
            get: () => _origWidth + noise,
            configurable: true,
        }});
        return metrics;
    }};

    // ── Navigator: hardwareConcurrency / deviceMemory ──
    Object.defineProperty(navigator, 'hardwareConcurrency', {{
        get: () => {hw},
        configurable: true,
    }});
    Object.defineProperty(navigator, 'deviceMemory', {{
        get: () => {dm},
        configurable: true,
    }});

    // ── Screen: mask physical monitor dimensions ──
    // Without this, window.screen.width reveals the actual monitor regardless of viewport.
    const _screenProto = Object.getPrototypeOf(window.screen);
    const _defineScreen = (prop, val) => {{
        try {{
            Object.defineProperty(window.screen, prop, {{ get: () => val, configurable: true }});
        }} catch(e) {{
            Object.defineProperty(_screenProto, prop, {{ get: () => val, configurable: true }});
        }}
    }};
    _defineScreen('width',       {sw});
    _defineScreen('height',      {sh});
    _defineScreen('availWidth',  {vw});
    _defineScreen('availHeight', {vh});
    _defineScreen('colorDepth',  24);
    _defineScreen('pixelDepth',  24);

    // ── WebGL: renderer + vendor strings ──
    // Same machine = same GPU = same renderer string across all accounts without this.
    const _origGetParameter = WebGLRenderingContext.prototype.getParameter;
    const _patchWebGL = (proto) => {{
        proto.getParameter = function(param) {{
            if (param === 0x1F01) return "{wr}";  // RENDERER
            if (param === 0x1F00) return "{wv}";  // VENDOR
            return _origGetParameter.call(this, param);
        }};
    }};
    _patchWebGL(WebGLRenderingContext.prototype);
    if (typeof WebGL2RenderingContext !== 'undefined') {{
        _patchWebGL(WebGL2RenderingContext.prototype);
    }}

    // ── AudioContext: channel data noise ──
    // Prevents audio fingerprinting by adding inaudible noise to processed audio buffers.
    const _origGetChannelData = AudioBuffer.prototype.getChannelData;
    AudioBuffer.prototype.getChannelData = function(channel) {{
        const data = _origGetChannelData.call(this, channel);
        for (let i = 0; i < data.length; i += 100) {{
            data[i] += (Math.random() - 0.5) * 1e-7;
        }}
        return data;
    }};

    // ── MediaDevices: enumerate spoofing ──
    // Returns a realistic device list (camera + mic) seeded from account fingerprint
    // so each account looks like a different physical machine.
    if (navigator.mediaDevices && navigator.mediaDevices.enumerateDevices) {{
        const _origEnumDevices = navigator.mediaDevices.enumerateDevices.bind(navigator.mediaDevices);
        const _seed = ({hw} * 7 + {dm} * 11) % 1000;
        const _hex = (n) => Math.abs(Math.sin(_seed + n) * 1e9).toString(16).slice(0, 64);
        const _fakeDevices = [
            {{ kind: 'audioinput',  deviceId: _hex(1), groupId: _hex(2), label: '' }},
            {{ kind: 'videoinput',  deviceId: _hex(3), groupId: _hex(4), label: '' }},
            {{ kind: 'audiooutput', deviceId: _hex(5), groupId: _hex(6), label: '' }},
        ];
        navigator.mediaDevices.enumerateDevices = async function() {{
            try {{
                const real = await _origEnumDevices();
                if (real && real.length > 0) return real;
            }} catch(e) {{}}
            return _fakeDevices.map(d => ({{
                ...d,
                toJSON: function() {{ return d; }},
            }}));
        }};
    }}

    // ── Battery API: spoof realistic laptop battery state ──
    // Without this every account reports "no battery API" or identical state.
    if (navigator.getBattery) {{
        const _battery = {{
            charging: ({hw} % 2 === 0),
            chargingTime: ({hw} % 2 === 0) ? 1200 + ({dm} * 60) : Infinity,
            dischargingTime: ({hw} % 2 === 0) ? Infinity : 8000 + ({dm} * 100),
            level: 0.4 + (({hw} * 7) % 60) / 100,
            addEventListener: () => {{}},
            removeEventListener: () => {{}},
            dispatchEvent: () => true,
        }};
        navigator.getBattery = () => Promise.resolve(_battery);
    }}

    // ── Plugins / MimeTypes: realistic empty (modern Chrome ships zero by default) ──
    // Old fingerprint scripts that check navigator.plugins.length need consistent answer.
    try {{
        Object.defineProperty(navigator, 'plugins', {{
            get: () => ({{
                length: 0,
                item: () => null,
                namedItem: () => null,
                refresh: () => {{}},
                [Symbol.iterator]: function*() {{}},
            }}),
            configurable: true,
        }});
    }} catch(e) {{}}

    // ── WebRTC: prevent local IP leak via STUN ──
    // Without this, getUserMedia + RTCPeerConnection leaks LAN IP regardless of proxy.
    if (window.RTCPeerConnection) {{
        const _OrigPC = window.RTCPeerConnection;
        window.RTCPeerConnection = function(config, ...rest) {{
            if (config && config.iceServers) {{
                config = {{ ...config, iceServers: [] }};
            }}
            return new _OrigPC(config, ...rest);
        }};
        window.RTCPeerConnection.prototype = _OrigPC.prototype;
    }}

}})();
"""
