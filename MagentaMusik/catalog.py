# -*- coding: utf-8 -*-
import calendar
import hashlib
import io
import json
import os
import re
import threading
import time
import uuid

try:
    import ssl
    ssl._create_default_https_context = ssl._create_unverified_context
except Exception:
    pass

try:
    import urllib2 as _urlreq
except ImportError:
    import urllib.request as _urlreq

try:
    from html import unescape as _html_unescape          # Python 3
except ImportError:
    from HTMLParser import HTMLParser                     # Python 2.7
    _html_unescape = HTMLParser().unescape

try:
    _unichr = unichr                                       # Python 2
except NameError:
    _unichr = chr                                          # Python 3

_JSON_UESC_RE = re.compile(r'\\u([0-9a-fA-F]{4})')


def _decode_json_unicode_escapes(s):
    """Embedded JSON-Strings in den Seiten haben Nicht-ASCII-Zeichen als
    literale \\uXXXX-Escapes (kein HTML-Entity) - html.unescape() deckt
    das nicht ab, muss separat dekodiert werden."""
    if not s or "\\u" not in s:
        return s
    return _JSON_UESC_RE.sub(lambda m: _unichr(int(m.group(1), 16)), s)

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

PLUGIN_DIR  = os.path.dirname(os.path.abspath(__file__))
COVER_DIR   = os.path.join(PLUGIN_DIR, "covers")
SETTINGS_FILE = "/etc/enigma2/magentamusik.json"

CACHE_DIR        = "/tmp/magentamusik_cache"
FESTIVALS_CACHE  = os.path.join(CACHE_DIR, "festivals.json")
ITEMS_CACHE_FMT  = os.path.join(CACHE_DIR, "items_%s.json")
LIVE_CACHE       = os.path.join(CACHE_DIR, "live_now.json")

TTL_FESTIVALS = 6 * 3600
TTL_ITEMS     = 1 * 3600
TTL_LIVE      = 5 * 60

HOME_URL = "https://www.magentamusik.de/"
COLLECTION_URL_FMT = "https://www.magentamusik.de/collection/%s"

_config_lock = threading.Lock()
_last_error  = None


def _dbg(msg):
    if not os.path.exists("/tmp/mm_debug"):
        return
    try:
        with open("/tmp/magentamusik.log", "a") as f:
            f.write("[%.3f] [catalog] %s\n" % (time.time(), msg))
    except Exception:
        pass


def last_fetch_error():
    return _last_error


def _set_error(msg):
    global _last_error
    _last_error = msg
    _dbg("error: %s" % msg)


def _clear_error():
    global _last_error
    _last_error = None


# ------------------------------------------------------------------
# HTTP
# ------------------------------------------------------------------
def _fetch(url, timeout=10):
    req  = _urlreq.Request(url, headers={"User-Agent": _UA})
    resp = _urlreq.urlopen(req, timeout=timeout)
    return resp.read()


def _fetch_text(url, timeout=10):
    return _fetch(url, timeout=timeout).decode("utf-8", "replace")


# ------------------------------------------------------------------
# Datei-Cache mit TTL + Stale-Fallback
# ------------------------------------------------------------------
def _ensure_cache_dir():
    try:
        if not os.path.isdir(CACHE_DIR):
            os.makedirs(CACHE_DIR)
    except Exception:
        pass


def _read_cache(path, ttl):
    """-> (data, is_stale) wenn Cache existiert, sonst (None, None)."""
    try:
        if not os.path.exists(path):
            return None, None
        with io.open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        fetched_at = payload.get("fetched_at", 0)
        is_stale   = (time.time() - fetched_at) > ttl
        return payload.get("data"), is_stale
    except Exception as e:
        _dbg("_read_cache failed for %s: %s" % (path, e))
        return None, None


def _write_cache(path, data):
    _ensure_cache_dir()
    try:
        payload = {"fetched_at": time.time(), "data": data}
        content = json.dumps(payload, ensure_ascii=False)
        if isinstance(content, bytes):
            content = content.decode("utf-8")
        tmp = path + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.rename(tmp, path)
    except Exception as e:
        _dbg("_write_cache failed for %s: %s" % (path, e))


def _cached_fetch(cache_path, ttl, fetch_fn, force_refresh=False):
    """Generisches TTL-Cache-Muster: liefert (data, from_cache_stale).
    Bei Netzwerkfehler: liefert abgelaufenen Cache falls vorhanden (stale=True),
    sonst None und setzt last_fetch_error()."""
    cached, is_stale = _read_cache(cache_path, ttl)
    if cached is not None and not is_stale and not force_refresh:
        _clear_error()
        return cached

    try:
        fresh = fetch_fn()
        _write_cache(cache_path, fresh)
        _clear_error()
        return fresh
    except Exception as e:
        _set_error("Netzwerkfehler: %s" % e)
        if cached is not None:
            _dbg("using stale cache for %s" % cache_path)
            return cached
        return None


# ------------------------------------------------------------------
# Festival-Liste (Header-Nav der Startseite)
# ------------------------------------------------------------------
_FESTIVAL_RE = re.compile(
    r'<a href="(https://www\.magentamusik\.de/collection/[a-z0-9-]+)" '
    r'class="a-tag a-tag--primary" aria-label="([^"]+)"'
)

# Jede Collection-Seite hat ein eigenes Hero-Bild als og:image (1200x630,
# ~180KB) - keine kleineren srcset-Varianten verfuegbar (CDN liefert 404
# bei abweichender Groesse). Das Tag steht weit oben im <head>, daher reicht
# ein Teil-Download der ersten paar KB statt der kompletten Seite (~165KB).
_OG_IMAGE_RE = re.compile(r'<meta content="([^"]+)" property="og:image"')
_OG_IMAGE_FETCH_BYTES = 16384


def _fetch_og_image(collection_url, timeout=8):
    try:
        req = _urlreq.Request(collection_url, headers={"User-Agent": _UA})
        resp = _urlreq.urlopen(req, timeout=timeout)
        chunk = resp.read(_OG_IMAGE_FETCH_BYTES).decode("utf-8", "replace")
        m = _OG_IMAGE_RE.search(chunk)
        return m.group(1) if m else None
    except Exception as e:
        _dbg("_fetch_og_image failed for %s: %s" % (collection_url, e))
        return None


def _parse_festivals(html_text):
    out = []
    seen = set()
    for m in _FESTIVAL_RE.finditer(html_text):
        url, name = m.group(1), _decode_json_unicode_escapes(_html_unescape(m.group(2)))
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        if slug in seen:
            continue
        seen.add(slug)
        out.append({"slug": slug, "name": name, "url": url})
    return out


def get_festivals(force_refresh=False):
    def _do_fetch():
        html_text = _fetch_text(HOME_URL)
        festivals = _parse_festivals(html_text)
        for f in festivals:
            f["image_url"] = _fetch_og_image(f["url"])
        return festivals
    result = _cached_fetch(FESTIVALS_CACHE, TTL_FESTIVALS, _do_fetch, force_refresh)
    return result or []


# ------------------------------------------------------------------
# Festival-Items (echte JSON-API hinter dem "Mehr laden"-Button)
# ------------------------------------------------------------------
# Die Collection-Seite rendert serverseitig nur die ersten 18 Items
# (data-filter-config). Der Rest kommt erst per Klick auf "Mehr laden"
# ueber diese API nach - bei manchen Festivals (z.B. Wacken: 151 Items)
# ist das die grosse Mehrheit der Inhalte. limit=18, offset in 18er-
# Schritten erhoehen bis count erreicht ist. Liefert sauberes JSON
# (json.loads dekodiert \uXXXX-Escapes automatisch korrekt), daher hier
# kein html.unescape/_decode_json_unicode_escapes mehr noetig.
ITEMS_API_URL_FMT = "https://www.magentamusik.de/api/teaser-grid/filter/collections/%s"
_ITEMS_PAGE_LIMIT = 18
_ITEMS_MAX_PAGES  = 30  # Sicherheitsgrenze (=540 Items), gegen Endlosschleife bei API-Anomalien

# Kacheln sind max. ~220px breit (FHD) - die groesste srcset-Variante (oft 1440px)
# zu laden/dekodieren ueberlastet den RAM der Box beim Nachladen vieler Cover auf
# einmal (z.B. beim Umblaettern einer Seite) und fuehrt zu einem nativen OOM-Crash
# (kein abfangbarer Python-Fehler). Stattdessen die kleinste Variante >= Zielbreite.
_TARGET_IMAGE_WIDTH = 400


def _fetch_json(url, timeout=10):
    return json.loads(_fetch(url, timeout=timeout).decode("utf-8", "replace"))


def _pick_image_url(image):
    if not image:
        return None
    srcset = image.get("srcset") or []
    # "src" enthaelt noch den angehaengten Breiten-Deskriptor ("...jpg 540w"),
    # wie im srcset-HTML-Attribut - fuer die eigentliche Download-URL abtrennen.
    entries = []
    for e in srcset:
        src = e.get("src")
        if not src:
            continue
        url = src.rsplit(" ", 1)[0] if src.rsplit(" ", 1)[-1].endswith("w") else src
        entries.append((url, e.get("width", 0)))
    if not entries:
        return None
    entries.sort(key=lambda e: e[1])
    chosen = next((e for e in entries if e[1] >= _TARGET_IMAGE_WIDTH), entries[-1])
    return chosen[0]


def _normalize_teaser(t):
    asset_id = t.get("assetId")
    href     = t.get("href")
    headline = t.get("headline")
    if not (asset_id and href and headline):
        return None
    return {
        "id":        t.get("id"),
        "headline":  headline,
        "slug":      t.get("slug", ""),
        "url":       href,
        "asset_id":  asset_id,
        "image_url": _pick_image_url(t.get("image")),
    }


def _fetch_all_items(slug):
    api_url = ITEMS_API_URL_FMT % slug
    items   = []
    offset  = 0
    count   = None
    for _ in range(_ITEMS_MAX_PAGES):
        data = _fetch_json("%s?offset=%d" % (api_url, offset))
        teasers = data.get("teasers") or []
        if count is None:
            count = data.get("count", len(teasers))
        for t in teasers:
            normalized = _normalize_teaser(t)
            if normalized:
                items.append(normalized)
        if not teasers:
            break
        offset += data.get("limit", _ITEMS_PAGE_LIMIT) or _ITEMS_PAGE_LIMIT
        if offset >= count:
            break
    return items


def get_festival_items(slug, force_refresh=False):
    cache_path = ITEMS_CACHE_FMT % slug

    def _do_fetch():
        return _fetch_all_items(slug)

    result = _cached_fetch(cache_path, TTL_ITEMS, _do_fetch, force_refresh)
    return result or []


# ------------------------------------------------------------------
# Live-Status (Main-Stage-Banner der Startseite)
# Siehe Memory project_magentamusik_catalog_scraping: statuslane auf
# Collection-Items ist KEIN Live-Indikator. Live laeuft ausschliesslich
# ueber diesen separaten Banner mit eigener Asset-/Event-URL.
# ------------------------------------------------------------------
_MAINSTAGE_CFG_RE   = re.compile(
    r'data-js-element="o-main-stage__config">\s*\{(?P<json>.*?)\}\s*</script>', re.DOTALL
)
_MAINSTAGE_TYPE_RE  = re.compile(r'"type":"([a-z]+)"')
_MAINSTAGE_ASSET_RE = re.compile(r'"assetId":"(DMM_MOVIE_\d+)"')
_MAINSTAGE_TIME_RE  = re.compile(
    r'data-expires-at="(?P<exp>\d*)"\s+data-available-at="(?P<avail>\d*)"'
)
_MAINSTAGE_CTA_RE = re.compile(
    # magentamusik.de liefert hier inzwischen ein Leerzeichen vor dem
    # schliessenden Anfuehrungszeichen ("Jetzt live " statt "Jetzt live") -
    # \s* vor dem " macht das robust gegen diese und aehnliche Whitespace-
    # Varianten, statt sich auf den exakten Text zu verlassen.
    r'aria-label="Jetzt live\s*" title="Jetzt live\s*"\s*\n?href="(?P<href>[^"]+)"'
)
_MAINSTAGE_HEADLINE_RE = re.compile(
    r'<h1 class="a-text a-text--headline-md[^"]*"\s*>(?P<headline>[^<]+)</h1>'
)


def _parse_live_now(html_text):
    cfg_m = _MAINSTAGE_CFG_RE.search(html_text)
    if not cfg_m:
        return None
    cfg_text = "{" + cfg_m.group("json") + "}"
    type_m = _MAINSTAGE_TYPE_RE.search(cfg_text)
    if not type_m or type_m.group(1) != "live":
        return None

    asset_m = _MAINSTAGE_ASSET_RE.search(cfg_text)
    time_m  = _MAINSTAGE_TIME_RE.search(html_text)
    cta_m   = _MAINSTAGE_CTA_RE.search(html_text)
    head_m  = _MAINSTAGE_HEADLINE_RE.search(html_text)

    if not (asset_m and time_m and cta_m):
        return None

    avail = time_m.group("avail")
    exp   = time_m.group("exp")
    now   = time.time()
    avail_ts = int(avail) if avail else None
    exp_ts   = int(exp) if exp else None

    is_live = bool(avail_ts and now >= avail_ts and (not exp_ts or now < exp_ts))
    if not is_live:
        return None

    headline = head_m.group("headline").strip() if head_m else "Live"
    return {
        "asset_id": asset_m.group(1),
        "headline": _decode_json_unicode_escapes(_html_unescape(headline)),
        "url":      cta_m.group("href"),
        "is_live":  True,
    }


def get_live_now(force_refresh=False):
    def _do_fetch():
        html_text = _fetch_text(HOME_URL)
        return _parse_live_now(html_text)
    return _cached_fetch(LIVE_CACHE, TTL_LIVE, _do_fetch, force_refresh)


# ------------------------------------------------------------------
# Mehrere gleichzeitige Buehnen (z. B. Hurricane: Forest Stage + River
# Stage). Die Event-Seite (z. B. hurricane-2026) zeigt dafuer mehrere
# "<Name> Stage"-Buttons mit "?stage=N"-Query-Param - der eigentliche
# Stream-Wechsel passiert aber rein client-seitig per JS-Klick (auch mit
# dem Query-Param liefert ein direkter HTTP-Abruf serverseitig immer
# dieselbe Buehne, verifiziert). Workaround: die Live-Streams liegen auf
# festen durchnummerierten CDN-Slots (mm001_hd, mm002_hd, ...) - das ist
# eine Heuristik (kein offizieller API-Weg gefunden), daher vor Anzeige
# IMMER per Segment-Aktualitaet verifizieren, dass der Slot wirklich
# gerade aktiv sendet und nicht zufaellig auf einen fremden/alten Slot
# zeigt. Slot-Index = (stage-Index aus dem Button) + 1.
# ------------------------------------------------------------------
_STAGE_CTA_RE = re.compile(
    r'aria-label="([^"]+)" title="[^"]+"\s+href="(https://www\.magentamusik\.de/[a-z0-9-]+/\?stage=(\d+))"'
)
_STAGE_SLOT_URL_FMT      = "https://svc42.main.sl.t-online.de/bpk-tv/%s/HLS_CMAF/index.m3u8"
_STAGE_SLOT_MAX_AGE_SEC  = 180  # grosszuegig: gemessene Live-Segment-Latenz lag real bei ~60-90s
_STAGE_SLOT_MAX_COUNT    = 4    # Sicherheitsgrenze gegen ausufernde Probe-Anfragen


def _slot_is_fresh(slot, timeout=8):
    try:
        master = _fetch_text(_STAGE_SLOT_URL_FMT % slot, timeout=timeout)
        if "#EXT-X-STREAM-INF" not in master:
            return False
        base = _STAGE_SLOT_URL_FMT % slot
        base = base[:base.rfind("/") + 1]
        lines = master.splitlines()
        best_bw, best_url = -1, None
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF"):
                m = re.search(r"BANDWIDTH=(\d+)", line)
                bw = int(m.group(1)) if m else 0
                if bw > best_bw and i + 1 < len(lines):
                    best_bw, best_url = bw, lines[i + 1].strip()
        if not best_url:
            return False
        seg = _fetch_text(base + best_url, timeout=timeout)
        times = re.findall(r"#EXT-X-PROGRAM-DATE-TIME:(\S+)", seg)
        if not times:
            return False
        last = times[-1].rstrip("Z")
        # Python-2.7-kompatibles Parsen ohne datetime.fromisoformat (erst ab Py3.7).
        # calendar.timegm() statt time.mktime(): der Zeitstempel ist UTC (Z-Suffix),
        # timegm() interpretiert das struct_time direkt als UTC ohne Rueckgriff auf
        # die lokale Zeitzone/Sommerzeit - time.mktime()-time.timezone war hier ein
        # Bug (lieferte bei aktiver Sommerzeit/CEST eine um 1h falsche Differenz,
        # liess frische Streams faelschlich als "alt" durchfallen).
        date_part, _, frac_part = last.partition(".")
        ts = calendar.timegm(time.strptime(date_part, "%Y-%m-%dT%H:%M:%S"))
        age = time.time() - ts
        return 0 <= age < _STAGE_SLOT_MAX_AGE_SEC
    except Exception as e:
        _dbg("_slot_is_fresh(%s) failed: %s" % (slot, e))
        return False


def get_live_stages(force_refresh=False):
    """Liefert alle aktuell aktiven Live-Buehnen als Liste (0, 1 oder mehrere
    Eintraege je {headline, url, is_live}). Buehne 0 nutzt immer die
    bewaehrte get_live_now()-URL (offizieller Resolver-Pfad). Weitere
    Buehnen nur, wenn ihr CDN-Slot per Frische-Check bestaetigt aktiv ist."""
    live = get_live_now(force_refresh)
    if not live:
        return []
    try:
        event_html = _fetch_text(live["url"])
    except Exception as e:
        _dbg("get_live_stages: Event-Seite nicht ladbar: %s" % e)
        return [live]

    stage_buttons = _STAGE_CTA_RE.findall(event_html)
    if len(stage_buttons) <= 1:
        return [live]

    result = []
    for label, _href, stage_idx_str in stage_buttons[:_STAGE_SLOT_MAX_COUNT]:
        stage_idx = int(stage_idx_str)
        label = _decode_json_unicode_escapes(_html_unescape(label))
        if stage_idx == 0:
            result.append({
                "headline": "%s – %s" % (live["headline"], label),
                "url":      live["url"],
                "is_live":  True,
            })
            continue
        slot = "mm%03d_hd" % (stage_idx + 1)
        if _slot_is_fresh(slot):
            result.append({
                "headline": "%s – %s" % (live["headline"], label),
                "url":      _STAGE_SLOT_URL_FMT % slot,
                "is_live":  True,
            })
    return result or [live]


# ------------------------------------------------------------------
# Cover-Cache (persistent, kein TTL — Bilder aendern sich nicht)
# ------------------------------------------------------------------
def cover_path_for(image_url):
    if not image_url:
        return None
    fname = hashlib.sha1(image_url.encode("utf-8")).hexdigest() + ".jpg"
    return os.path.join(COVER_DIR, fname)


def fetch_cover_if_missing(image_url, timeout=5):
    path = cover_path_for(image_url)
    if not path:
        return None
    if os.path.exists(path):
        return path
    try:
        if not os.path.isdir(COVER_DIR):
            os.makedirs(COVER_DIR)
        data = _fetch(image_url, timeout=timeout)
        if not data or len(data) > 2 * 1024 * 1024:
            return None
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.rename(tmp, path)
        return path
    except Exception as e:
        _dbg("fetch_cover_if_missing failed for %s: %s" % (image_url, e))
        return None


# ------------------------------------------------------------------
# Settings (kein items-Array noetig, Inhalte sind nicht nutzerverwaltet)
# ------------------------------------------------------------------
DEFAULT_DOWNLOAD_DIR = "/media/hdd/movie/MagentaMusik"

_SETTINGS_DEFAULTS = {
    "show_covers":              True,
    "wrap_lr":                  True,
    "serviceapp_autoconfigure": True,
    "debug_log":                False,
    "download_dir":             DEFAULT_DOWNLOAD_DIR,
    "download_convert_ts":      False,
    "recording_timers":         [],
}


def get_settings():
    with _config_lock:
        try:
            if os.path.exists(SETTINGS_FILE):
                with io.open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        merged = dict(_SETTINGS_DEFAULTS)
                        merged.update(data)
                        return merged
        except Exception:
            pass
        return dict(_SETTINGS_DEFAULTS)


def save_settings(settings):
    with _config_lock:
        try:
            content = json.dumps(settings, ensure_ascii=False, indent=2)
            tmp = SETTINGS_FILE + ".tmp"
            with open(tmp, "wb") as f:
                f.write(content.encode("utf-8") if not isinstance(content, bytes) else content)
            os.rename(tmp, SETTINGS_FILE)
            return True
        except Exception as e:
            _dbg("save_settings failed: %s" % e)
            return False


def get_download_dir():
    return get_settings().get("download_dir", DEFAULT_DOWNLOAD_DIR)


def set_download_dir(path):
    if isinstance(path, bytes):
        path = path.decode("utf-8", "replace")
    s = get_settings()
    s["download_dir"] = path
    save_settings(s)


def get_download_convert_ts():
    return bool(get_settings().get("download_convert_ts", False))


def set_download_convert_ts(enabled):
    s = get_settings()
    s["download_convert_ts"] = bool(enabled)
    save_settings(s)


# ------------------------------------------------------------------
# Aufnahme-Timer (geplante Live-Aufnahmen, siehe player.HLSRecorder)
# ------------------------------------------------------------------

def get_recording_timers():
    return get_settings().get("recording_timers", [])


def add_recording_timer(name, url, start_time, user_agent="", duration=None):
    s = get_settings()
    timer = {
        "id":         str(uuid.uuid4()),
        "name":       name,
        "url":        url,
        "user_agent": user_agent,
        "start_time": int(start_time),
        "duration":   int(duration) if duration else None,
        "status":     "pending",
    }
    s.setdefault("recording_timers", []).append(timer)
    save_settings(s)
    return timer


def update_recording_timer_status(timer_id, status):
    s = get_settings()
    for t in s.get("recording_timers", []):
        if t.get("id") == timer_id:
            t["status"] = status
            break
    save_settings(s)


def update_recording_timer(timer_id, name, start_time, duration):
    s = get_settings()
    timer = None
    for t in s.get("recording_timers", []):
        if t.get("id") == timer_id:
            t["name"]       = name
            t["start_time"] = int(start_time)
            t["duration"]   = int(duration) if duration else None
            timer = t
            break
    save_settings(s)
    return timer


def delete_recording_timer(timer_id):
    s = get_settings()
    s["recording_timers"] = [t for t in s.get("recording_timers", []) if t.get("id") != timer_id]
    save_settings(s)
