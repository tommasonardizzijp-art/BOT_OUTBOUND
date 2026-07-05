"""Pool di device Android reali, per dare a OGNI account un telefono DIVERSO ma STABILE.

Problema: instagrapi `Client()` usa un unico device di default (OnePlus 6T "devitron",
Android 8.0.0). Tutti gli account del cluster escono quindi dallo stesso identico telefono
simulato = firma da bot-farm ("4 persone diverse, stesso vecchio OnePlus").

Soluzione: `device_for_account(username)` sceglie in modo DETERMINISTICO (hash dello
username) un profilo hardware da questo pool. Deterministico = lo STESSO account riceve
SEMPRE lo stesso telefono ad ogni re-login (un telefono che "non cambia mai" e' realistico);
username diversi ricadono su telefoni diversi (rompe il cluster).

⚠️ Cambiare device a un account GIA' in uso e' esso stesso un segnale ("ha cambiato
telefono"). Per questo l'assegnazione entra in vigore solo al PROSSIMO "Login Browser"
(vedi manual_login._build_session), non a meta' sessione. Rollout: re-login degli account
uno alla volta, scaglionati su piu' giorni.

Chiavi = solo HARDWARE. app_version/version_code/bloks li tiene instagrapi (set_app),
coerenti su tutti = build app unica e valida (fissa anche il disallineamento 364 vs 385).

⚠️⚠️ STATO: NON VERIFICATO — il flag `device_diversify_enabled` e' OFF di default. ⚠️⚠️
Queste specs sono scritte a mano su conoscenza generale. Alcune entry (codename/SoC di
certi Samsung/Xiaomi/OnePlus) NON sono garantite contro uno user-agent Instagram Android
REALE. Un device incoerente (es. model/codename/cpu/dpi che nessun telefono vero emette) e'
una firma PEGGIORE del OnePlus 6T di default (che e' un device reale e coerente). Prima di
abilitare: verificare OGNI entry contro un UA IG Android reale (catturato da un telefono
vero o da una fonte autorevole) e togliere quelle non confermate. Formato UA reale:
`Instagram <app> Android (<api>/<rel>; <dpi>; <res>; <manuf>; <model>; <device>; <cpu>; <locale>; <vcode>)`.
Esempi reali noti: Samsung SM-N910F/trlte/qcom (23/6.0.1; 640dpi; 1440x2560),
Samsung SM-G930P/heroqltespr (24/7.0; 480dpi; 1080x1920), OnePlus 6T/devitron/qcom (default).
"""
import hashlib

# Ogni entry: profilo hardware di un telefono Android reale, moderno, diffuso.
DEVICE_POOL: list[dict] = [
    {"manufacturer": "samsung", "model": "SM-G991B", "device": "o1s",
     "cpu": "exynos2100", "android_release": "13", "android_version": 33,
     "dpi": "421dpi", "resolution": "1080x2400"},                       # Galaxy S21 5G
    {"manufacturer": "samsung", "model": "SM-S901B", "device": "r0s",
     "cpu": "exynos2200", "android_release": "14", "android_version": 34,
     "dpi": "480dpi", "resolution": "1080x2340"},                       # Galaxy S22
    {"manufacturer": "samsung", "model": "SM-A546B", "device": "a54x",
     "cpu": "s5e8835", "android_release": "14", "android_version": 34,
     "dpi": "450dpi", "resolution": "1080x2340"},                       # Galaxy A54 5G
    {"manufacturer": "Google", "model": "Pixel 7", "device": "panther",
     "cpu": "gs201", "android_release": "14", "android_version": 34,
     "dpi": "420dpi", "resolution": "1080x2400"},                       # Pixel 7
    {"manufacturer": "Google", "model": "Pixel 6a", "device": "bluejay",
     "cpu": "gs101", "android_release": "14", "android_version": 34,
     "dpi": "429dpi", "resolution": "1080x2400"},                       # Pixel 6a
    {"manufacturer": "OnePlus", "model": "LE2113", "device": "OnePlus9",
     "cpu": "qcom", "android_release": "13", "android_version": 33,
     "dpi": "480dpi", "resolution": "1080x2400"},                       # OnePlus 9
    {"manufacturer": "Xiaomi", "model": "2201117TG", "device": "spes",
     "cpu": "qcom", "android_release": "13", "android_version": 33,
     "dpi": "440dpi", "resolution": "1080x2400"},                       # Redmi Note 11
    {"manufacturer": "Xiaomi", "model": "2201123G", "device": "cupid",
     "cpu": "qcom", "android_release": "13", "android_version": 33,
     "dpi": "480dpi", "resolution": "1080x2400"},                       # Xiaomi 12
    {"manufacturer": "samsung", "model": "SM-G780G", "device": "r8q",
     "cpu": "qcom", "android_release": "13", "android_version": 33,
     "dpi": "420dpi", "resolution": "1080x2400"},                       # Galaxy S20 FE
    {"manufacturer": "motorola", "model": "XT2203-1", "device": "dubai",
     "cpu": "qcom", "android_release": "13", "android_version": 33,
     "dpi": "420dpi", "resolution": "1080x2400"},                       # Edge 30
    {"manufacturer": "OnePlus", "model": "CPH2451", "device": "OP5551L1",
     "cpu": "qcom", "android_release": "14", "android_version": 34,
     "dpi": "450dpi", "resolution": "1080x2412"},                       # OnePlus 11
    {"manufacturer": "samsung", "model": "SM-A536B", "device": "a53x",
     "cpu": "s5e8825", "android_release": "13", "android_version": 33,
     "dpi": "450dpi", "resolution": "1080x2400"},                       # Galaxy A53 5G
]

# Chiavi hardware che sovrascriviamo sul device di default (le altre — app_version,
# version_code, bloks_versioning_id — le gestisce instagrapi.set_app, coerenti).
_HARDWARE_KEYS = ("manufacturer", "model", "device", "cpu",
                  "android_release", "android_version", "dpi", "resolution")


def device_for_account(username: str) -> dict:
    """Profilo hardware STABILE per questo username (stesso account -> stesso device sempre)."""
    digest = hashlib.sha256(username.encode("utf-8")).hexdigest()
    idx = int(digest, 16) % len(DEVICE_POOL)
    return dict(DEVICE_POOL[idx])
