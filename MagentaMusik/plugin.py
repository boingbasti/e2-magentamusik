# -*- coding: utf-8 -*-

import os
import threading

try:
    import Queue as _queue_mod                            # Python 2
except ImportError:
    import queue as _queue_mod                             # Python 3

from Plugins.Plugin import PluginDescriptor
from Screens.Screen import Screen
from Screens.MessageBox import MessageBox as _MessageBox
from Screens.VirtualKeyBoard import VirtualKeyBoard
from Screens.ChoiceBox import ChoiceBox
from Components.ActionMap import ActionMap
from Components.Label import Label
from enigma import eTimer, ePoint, eSize, getDesktop

try:
    from Components.Pixmap import Pixmap as _Pixmap
except ImportError:
    _Pixmap = None

try:
    from Tools.LoadPixmap import LoadPixmap as _LoadPixmap
except ImportError:
    _LoadPixmap = None

PLUGIN_VERSION = "1.0.0"

# Manche Festivals haben inzwischen 100+ Items (siehe catalog.py-Paginierung) -
# ein unbegrenzter Pixmap-Cache wuerde beim Durchblaettern aller Seiten immer
# mehr dekodierte Cover im RAM ansammeln und die Box irgendwann aus dem Speicher
# laufen lassen. Deshalb hartes Limit mit FIFO-Verdraengung der aeltesten Eintraege.
from collections import OrderedDict
_PIXMAP_CACHE_MAX = 60
_pixmap_cache = OrderedDict()


def _cached_pixmap(path):
    if not path:
        return None
    if path in _pixmap_cache:
        # OrderedDict.move_to_end() gibt es erst ab Python 3.2 - auf der Box
        # laeuft Python 2.7. pop()+Neueinfuegen verschiebt den Eintrag
        # ebenfalls ans Ende und funktioniert in beiden Versionen.
        px = _pixmap_cache.pop(path)
        _pixmap_cache[path] = px
        return px
    if _LoadPixmap and os.path.isfile(path):
        px = _LoadPixmap(_b(path))
    else:
        px = None
    _pixmap_cache[path] = px
    if len(_pixmap_cache) > _PIXMAP_CACHE_MAX:
        _pixmap_cache.popitem(last=False)
    return px


import catalog as _catalog
from player import play_resolved_stream, resolve_local_playlist, HLSRecorder, format_duration
from downloader import Downloader, convert_mp4_to_ts, format_size
from download_manager import MagentaMusikDownloadManagerScreen

# ------------------------------------------------------------------
# Asynchrones Cover-Nachladen: Seiten/Listenzeilen werden sofort ohne
# Cover gerendert, fehlende Bilder laden im Hintergrund nach und werden
# per Pixmap-Tausch nachgereicht, sobald sie fertig sind. Begrenzter
# Worker-Pool statt Thread-pro-Anfrage - beim schnellen Durchblaettern
# eines 100+-Item-Festivals wuerden sonst (analog zum fruehren OOM-Crash
# durch zu viele gleichzeitige Pixmap-Decodes, siehe Memory) zu viele
# parallele Downloads/Threads auf der Box entstehen.
# ------------------------------------------------------------------
_COVER_WORKER_COUNT  = 3
# LifoQueue statt Queue: beim schnellen Weiterblaettern (mehrere Seiten in
# Folge) sollen die Cover der zuletzt angeforderten (= aktuell sichtbaren)
# Seite zuerst geladen werden, nicht hinter einem Rueckstau laengst
# verlassener Seiten anstehen muessen (FIFO wuerde genau das tun).
_cover_queue         = _queue_mod.LifoQueue()
_cover_workers_started = False
_cover_fetch_inflight  = set()  # image_urls, die gerade von einem Worker geladen werden


def _cover_worker_loop():
    while True:
        image_url, callback = _cover_queue.get()
        try:
            path = _catalog.fetch_cover_if_missing(image_url)
        except Exception:
            path = None
        try:
            from twisted.internet import reactor
            reactor.callFromThread(callback, path)
        except Exception:
            callback(path)


def _ensure_cover_workers():
    global _cover_workers_started
    if _cover_workers_started:
        return
    _cover_workers_started = True
    for _ in range(_COVER_WORKER_COUNT):
        t = threading.Thread(target=_cover_worker_loop)
        t.daemon = True
        t.start()

PLUGIN_DIR = os.path.dirname(__file__)
LOGO_DIR   = os.path.join(PLUGIN_DIR, "logos")

try:
    IS_FHD = getDesktop(0).size().width() > 1280
except Exception:
    IS_FHD = True


def _b(val):
    if isinstance(val, bytes):
        return val
    try:
        return val.encode("utf-8")
    except Exception:
        return str(val)


def _u(val):
    if isinstance(val, bytes):
        return val.decode("utf-8", "replace")
    return val


_MM_DEBUG_FLAG = "/tmp/mm_debug"
_MM_DEBUG_LOG  = "/tmp/magentamusik.log"


def _dbg(msg):
    if not os.path.exists(_MM_DEBUG_FLAG):
        return
    try:
        import time
        with open(_MM_DEBUG_LOG, "a") as f:
            f.write("[%.3f] %s\n" % (time.time(), msg))
    except Exception:
        pass


# ------------------------------------------------------------------
# Einstellungen
# ------------------------------------------------------------------
def _get_setting(key, default=False):
    return _catalog.get_settings().get(key, default)


def _set_setting(key, value):
    s = _catalog.get_settings()
    s[key] = value
    _catalog.save_settings(s)


def _sync_debug_flag():
    # _MM_DEBUG_FLAG liegt in /tmp (tmpfs) und wird bei jedem Box-Neustart
    # geleert, das "debug_log"-Setting wird aber persistent in
    # /etc/enigma2/magentamusik.json gespeichert - ohne diesen Abgleich wuerde
    # nach einem Neustart bei aktiviertem Debug-Log im UI "An" stehen, obwohl
    # _dbg() mangels Flag-Datei still nichts mehr loggt. Beim Plugin-Laden
    # einmalig ausgefuehrt, damit die Flag-Datei wieder zum Setting passt.
    try:
        if _get_setting("debug_log", False):
            if not os.path.exists(_MM_DEBUG_FLAG):
                open(_MM_DEBUG_FLAG, "w").close()
        else:
            if os.path.exists(_MM_DEBUG_FLAG):
                os.remove(_MM_DEBUG_FLAG)
    except Exception:
        pass


_sync_debug_flag()


def _get_settings_list():
    return [
        ("show_covers",              "Vorschaubilder laden",          "toggle"),
        ("wrap_lr",                  "Seite wechseln mit Links/Rechts", "toggle"),
        ("serviceapp_autoconfigure", "ServiceApp auto-konfigurieren", "toggle"),
        ("debug_log",                "Debug-Log",                     "toggle"),
        ("download_dir",             "Download-Ordner",               "path"),
        # "download_convert_ts" bewusst ausgeblendet (nicht geloescht): greift
        # nur, wenn eine Download-Datei auf .mp4 endet, magentamusik.de liefert
        # aber bisher ausnahmslos .m3u8 (Datei landet schon direkt als .ts) -
        # die Option ist damit aktuell wirkungslos. Mechanismus (catalog.py
        # get/set_download_convert_ts, downloader.convert_mp4_to_ts(),
        # _bg_download_done()-Trigger) bleibt unangetastet fuer den Fall, dass
        # magentamusik.de irgendwann doch mal direkte MP4s ausliefert - dann
        # reicht es, diese Zeile wieder einzukommentieren.
        # ("download_convert_ts",    "MP4-Downloads in TS wandeln",   "toggle"),
    ]


_SETTINGS_DEFAULTS = {
    "show_covers":              True,
    "wrap_lr":                  True,
    "serviceapp_autoconfigure": True,
    "debug_log":                False,
    "list_mode":                False,
    "download_dir":             _catalog.DEFAULT_DOWNLOAD_DIR,
    "download_convert_ts":      False,
}


def _shorten_path(path, maxlen=None):
    if maxlen is None:
        maxlen = 34 if IS_FHD else 24
    if not path:
        return ""
    path = _u(path)
    if len(path) <= maxlen:
        return path
    return u"…" + path[-(maxlen - 1):]


# ------------------------------------------------------------------
# Kachel-Layout (angelehnt an e2-StreamAnything)
# ------------------------------------------------------------------
TILE_COLS      = 4
TILE_ROWS      = 3
TILES_PER_PAGE = TILE_COLS * TILE_ROWS

if IS_FHD:
    TILE_W, TILE_H   = 450, 160
    TILE_LABEL_H     = 38
    TILE_LABEL_GAP   = 8
    _TX = [30, 500, 970, 1440]
    _TY = [180, 426, 672]
    _SCREEN_W, _SCREEN_H = 1920, 1080
    _TITLE_X, _TITLE_Y, _TITLE_W, _TITLE_H = 30, 30, 1860, 60
    _LEGEND_Y  = 960
    _LEGEND_H  = 100
    _CONTENT_Y = 100
    _CONTENT_H = 850
    _LIVE_W, _LIVE_H = 90, 30
else:
    TILE_W, TILE_H   = 290, 107
    TILE_LABEL_H     = 25
    TILE_LABEL_GAP   = 5
    _TX = [30, 340, 650, 960]
    _TY = [120, 284, 448]
    _SCREEN_W, _SCREEN_H = 1280, 720
    _TITLE_X, _TITLE_Y, _TITLE_W, _TITLE_H = 20, 20, 1240, 40
    _LEGEND_Y  = 634
    _LEGEND_H  = 60
    _CONTENT_Y = 70
    _CONTENT_H = 554
    _LIVE_W, _LIVE_H = 58, 20

TILE_POSITIONS = [(_TX[c], _TY[r]) for r in range(TILE_ROWS) for c in range(TILE_COLS)]

if IS_FHD:
    LIST_ROWS   = 12
    LIST_ROW_H  = 70
    LIST_ROW_Y0 = _CONTENT_Y
else:
    LIST_ROWS   = 11
    LIST_ROW_H  = 47
    LIST_ROW_Y0 = _CONTENT_Y

_LOGO_W = 220 if IS_FHD else 140
_LOGO_H = 124 if IS_FHD else 79


def _logo_base_rect(idx):
    tx, ty = TILE_POSITIONS[idx]
    lx = tx + (TILE_W - _LOGO_W) // 2
    ly = ty + (TILE_H - _LOGO_H) // 2
    return lx, ly, _LOGO_W, _LOGO_H


# ------------------------------------------------------------------
# Skin-Templates
# ------------------------------------------------------------------
def _tile_widget(idx, x, y, w, h):
    lw = _LOGO_W
    lh = _LOGO_H
    lx = x + (w - lw) // 2
    ly = y + (h - lh) // 2
    bs = 40 if IS_FHD else 28
    bp = 6  if IS_FHD else 4
    return (
        '<widget name="tile_bg_{i}" position="{x},{y}" size="{w},{h}" '
        'backgroundColor="#1A000000" zPosition="-4"/>'
        '<widget name="tile_logo_{i}" position="{lx},{ly}" size="{lw},{lh}" '
        'alphatest="blend" zPosition="1" transparent="1" scale="1"/>'
        '<widget name="tile_sel_{i}" position="{x},{y}" size="{w},{h}" '
        'alphatest="blend" zPosition="3" transparent="1"/>'
        '<widget name="tile_type_{i}" position="{bx},{by}" size="{bs},{bs}" '
        'alphatest="blend" zPosition="4" transparent="1" scale="1"/>'
        '<widget name="tile_live_{i}" position="{lvx},{lvy}" size="{lvw},{lvh}" '
        'zPosition="5" font="Regular;{lvfs}" halign="center" valign="center" '
        'backgroundColor="#00CC2222" foregroundColor="#00FFFFFF" transparent="0"/>'
        '<widget name="tile_label_{i}" position="{x},{labely}" size="{w},{labelh}" '
        'zPosition="2" font="Regular;{fs}" halign="center" '
        'valign="center" foregroundColor="#00E0E0E0" backgroundColor="#33000000" noWrap="1"/>'
    ).format(
        i=idx, x=x, y=y, w=w, h=h,
        lx=lx, ly=ly, lw=lw, lh=lh,
        labely=y + h + TILE_LABEL_GAP,
        labelh=TILE_LABEL_H,
        fs=22 if IS_FHD else 15,
        bx=x + bp,
        by=y + h - bs - bp,
        bs=bs,
        lvx=x + bp, lvy=y + bp, lvw=_LIVE_W, lvh=_LIVE_H,
        lvfs=18 if IS_FHD else 13,
    )


def _build_skin():
    sw, sh = _SCREEN_W, _SCREEN_H
    tiles_xml = "".join(
        _tile_widget(i, x, y, TILE_W, TILE_H)
        for i, (x, y) in enumerate(TILE_POSITIONS)
    )

    if IS_FHD:
        lr_x, lr_w = 30, sw - 60
        lo_x, lo_w = lr_x + 10, 100
        lt_s       = 40
        lt_x       = lo_x + lo_w + 8
        lt_oy      = (LIST_ROW_H - lt_s) // 2
        ll_x       = lt_x + lt_s + 8
        ll_w       = lr_x + lr_w - 10 - ll_x
        l_rf       = 32
        ls_x       = lo_x + lo_w + 5
        ls_w       = lr_x + lr_w - ls_x
        lv_w, lv_h = 70, 24
    else:
        lr_x, lr_w = 30, sw - 60
        lo_x, lo_w = lr_x + 8, 65
        lt_s       = 26
        lt_x       = lo_x + lo_w + 5
        lt_oy      = (LIST_ROW_H - lt_s) // 2
        ll_x       = lt_x + lt_s + 5
        ll_w       = lr_x + lr_w - 8 - ll_x
        l_rf       = 21
        ls_x       = lo_x + lo_w + 4
        ls_w       = lr_x + lr_w - ls_x
        lv_w, lv_h = 46, 16

    list_xml = ""
    for i in range(LIST_ROWS):
        y = LIST_ROW_Y0 + i * LIST_ROW_H
        list_xml += (
            '<widget name="list_sel_{i}"   position="{sx},{y}"   size="{sw},{rh}" '
            'backgroundColor="#11cc0066" zPosition="1" transparent="0"/>'
            '<widget name="list_logo_{i}"  position="{lox},{y}"  size="{low},{rh}" '
            'alphatest="blend" zPosition="2" transparent="1" scale="1"/>'
            '<widget name="list_live_{i}"  position="{lox},{y}"  size="{lvw},{lvh}" '
            'zPosition="3" font="Regular;{lvfs}" halign="center" valign="center" '
            'backgroundColor="#00CC2222" foregroundColor="#00FFFFFF" transparent="0"/>'
            '<widget name="list_label_{i}" position="{lbx},{y}"  size="{lbw},{rh}" '
            'zPosition="2" font="Regular;{rf}" halign="left" valign="center" '
            'foregroundColor="#00E0E0E0" backgroundColor="#33000000" transparent="1" noWrap="1"/>'
            '<widget name="list_type_{i}"  position="{ltx},{lty}" size="{lts},{lts}" '
            'alphatest="blend" zPosition="2" transparent="1" scale="1"/>'
        ).format(i=i, y=y, sx=ls_x, sw=ls_w, rh=LIST_ROW_H,
                 lox=lo_x, low=lo_w, lbx=ll_x, lbw=ll_w, rf=l_rf,
                 ltx=lt_x, lty=y + lt_oy, lts=lt_s,
                 lvw=lv_w, lvh=lv_h, lvfs=15 if IS_FHD else 11)

    if IS_FHD:
        ly, lh = _LEGEND_Y, _LEGEND_H
        pip_y  = ly + (lh - 60) // 2
        pip_h  = 60
        pip_w  = 8
        fs     = 32
        legend = (
            '<eLabel backgroundColor="#1A000000" position="30,{ly}" size="1860,{lh}" zPosition="-3" transparent="0"/>'
            '<eLabel backgroundColor="#1AEE0000" position="50,{py}" size="{pw},{ph}" zPosition="2" transparent="0"/>'
            '<widget name="hint_red"    position="68,{ly}"   size="230,{lh}" zPosition="4" transparent="1" backgroundColor="#1A000000" font="Regular;{fs}" halign="left"  valign="center" foregroundColor="#CCCCCC"/>'
            '<eLabel backgroundColor="#1A00AA00" position="330,{py}" size="{pw},{ph}" zPosition="2" transparent="0"/>'
            '<widget name="hint_green"  position="348,{ly}"  size="220,{lh}" zPosition="4" transparent="1" backgroundColor="#1A000000" font="Regular;{fs}" halign="left"  valign="center" foregroundColor="#CCCCCC"/>'
            '<eLabel backgroundColor="#1ACCAA00" position="588,{py}" size="{pw},{ph}" zPosition="2" transparent="0"/>'
            '<widget name="hint_yellow" position="606,{ly}"  size="200,{lh}" zPosition="4" transparent="1" backgroundColor="#1A000000" font="Regular;{fs}" halign="left"  valign="center" foregroundColor="#CCCCCC"/>'
            '<widget name="blue_pip" position="830,{py}" size="{pw},{ph}" zPosition="2" backgroundColor="#1A0066CC" transparent="0"/>'
            '<widget name="hint_blue"   position="848,{ly}" size="280,{lh}" zPosition="4" transparent="1" backgroundColor="#1A000000" font="Regular;{fs}" halign="left"  valign="center" foregroundColor="#CCCCCC"/>'
            '<widget name="hint_menu"   position="1150,{ly}" size="230,{lh}" zPosition="4" transparent="1" backgroundColor="#1A000000" font="Regular;{fs}" halign="left"  valign="center" foregroundColor="#CCCCCC"/>'
            '<widget name="hint_info"   position="1395,{ly}" size="270,{lh}" zPosition="4" transparent="1" backgroundColor="#1A000000" font="Regular;{fs}" halign="left"  valign="center" foregroundColor="#CCCCCC" noWrap="1"/>'
            '<widget name="page_label"  position="1680,{ly}" size="210,{lh}" zPosition="4" transparent="1" backgroundColor="#1A000000" font="Regular;28" halign="right" valign="center" foregroundColor="#AAAAAA" noWrap="1"/>'
        ).format(ly=ly, lh=lh, py=pip_y, ph=pip_h, pw=pip_w, fs=fs)
    else:
        ly, lh = _LEGEND_Y, _LEGEND_H
        pip_y  = ly + (lh - 30) // 2
        pip_h  = 30
        pip_w  = 5
        fs     = 21
        legend = (
            '<eLabel backgroundColor="#1A000000" position="30,{ly}" size="1220,{lh}" zPosition="-3" transparent="0"/>'
            '<eLabel backgroundColor="#1AEE0000" position="33,{py}" size="{pw},{ph}" zPosition="2" transparent="0"/>'
            '<widget name="hint_red"    position="42,{ly}"   size="150,{lh}" zPosition="4" transparent="1" backgroundColor="#1A000000" font="Regular;{fs}" halign="left"  valign="center" foregroundColor="#CCCCCC"/>'
            '<eLabel backgroundColor="#1A00AA00" position="205,{py}" size="{pw},{ph}" zPosition="2" transparent="0"/>'
            '<widget name="hint_green"  position="214,{ly}"  size="140,{lh}" zPosition="4" transparent="1" backgroundColor="#1A000000" font="Regular;{fs}" halign="left"  valign="center" foregroundColor="#CCCCCC"/>'
            '<eLabel backgroundColor="#1ACCAA00" position="370,{py}" size="{pw},{ph}" zPosition="2" transparent="0"/>'
            '<widget name="hint_yellow" position="379,{ly}"  size="130,{lh}" zPosition="4" transparent="1" backgroundColor="#1A000000" font="Regular;{fs}" halign="left"  valign="center" foregroundColor="#CCCCCC"/>'
            '<widget name="blue_pip" position="527,{py}" size="{pw},{ph}" zPosition="2" backgroundColor="#1A0066CC" transparent="0"/>'
            '<widget name="hint_blue"   position="536,{ly}"  size="180,{lh}" zPosition="4" transparent="1" backgroundColor="#1A000000" font="Regular;{fs}" halign="left"  valign="center" foregroundColor="#CCCCCC"/>'
            '<widget name="hint_menu"   position="733,{ly}"  size="150,{lh}" zPosition="4" transparent="1" backgroundColor="#1A000000" font="Regular;{fs}" halign="left"  valign="center" foregroundColor="#CCCCCC"/>'
            '<widget name="hint_info"   position="893,{ly}"  size="180,{lh}" zPosition="4" transparent="1" backgroundColor="#1A000000" font="Regular;{fs}" halign="left"  valign="center" foregroundColor="#CCCCCC" noWrap="1"/>'
            '<widget name="page_label"  position="1083,{ly}" size="150,{lh}" zPosition="4" transparent="1" backgroundColor="#1A000000" font="Regular;{fs}" halign="right" valign="center" foregroundColor="#AAAAAA" noWrap="1"/>'
        ).format(ly=ly, lh=lh, py=pip_y, ph=pip_h, pw=pip_w, fs=fs)

    # Logo links in der Titelzeile (Seitenverhaeltnis 400x160 = 2.5:1 des
    # plugin.png beibehalten, sonst wirkt es gestaucht/verzerrt).
    if IS_FHD:
        logo_m  = 8
        logo_lh = _TITLE_H - 2 * logo_m
        logo_lw = int(logo_lh * 400 / 160)
        logo_lx = _TITLE_X + 10
        logo_ly = _TITLE_Y + logo_m
        tgap    = 15
    else:
        logo_m  = 5
        logo_lh = _TITLE_H - 2 * logo_m
        logo_lw = int(logo_lh * 400 / 160)
        logo_lx = _TITLE_X + 6
        logo_ly = _TITLE_Y + logo_m
        tgap    = 10
    tpad = 30 if IS_FHD else 20
    tpx_old = _TITLE_X + tpad
    tpx     = logo_lx + logo_lw + tgap
    if IS_FHD:
        ttw, stx, stw, vtx, vtw = 900, 950, 520, 1490, 370
        tfs, sfs, ifs = 36, 24, 26
    else:
        ttw, stx, stw, vtx, vtw = 600, 650, 300, 960, 280
        tfs, sfs, ifs = 24, 16, 18
    ttw -= (tpx - tpx_old)
    ttw  = min(ttw, stx - tpx - 10)  # Titel-Box darf die Status-Box nie ueberlappen
    return (
        '<screen backgroundColor="transparent" flags="wfNoBorder" '
        'position="0,0" size="{sw},{sh}" title="MagentaMusik">'
        '<eLabel backgroundColor="#66000000" position="0,0" size="{sw},{sh}" zPosition="-6" transparent="0"/>'
        '<eLabel backgroundColor="#0A000000" position="{tx},{ty}" size="{tw},{th}" zPosition="-5" transparent="0"/>'
        '<eLabel backgroundColor="#00cc0066" position="{tx},{tby}" size="{tw},{tbs}" zPosition="-4" transparent="0"/>'
        '<eLabel backgroundColor="#33000000" position="{tx},{cy}" size="{tw},{ch}" zPosition="-5" transparent="0"/>'
        '<widget name="header_logo" position="{lx},{ly}" size="{lw},{lh}" '
        'zPosition="4" alphatest="blend" transparent="1" scale="1"/>'
        '<widget name="title" position="{tpx},{ty}" size="{ttw},{th}" '
        'zPosition="4" backgroundColor="#0A000000" font="Regular;{tfs}" halign="left" noWrap="1" '
        'valign="center" foregroundColor="#00cc0066"/>'
        '<widget name="status" position="{stx},{ty}" size="{stw},{th}" '
        'zPosition="4" backgroundColor="#0A000000" font="Regular;{sfs}" halign="right" noWrap="1" '
        'valign="center" foregroundColor="#00CCAA00"/>'
        '<eLabel text="v{ver}" position="{vtx},{ty}" size="{vtw},{th}" '
        'zPosition="4" backgroundColor="#0A000000" font="Regular;{ifs}" halign="right" '
        'valign="center" foregroundColor="#00888888"/>'
        '{legend}'
        '{tiles}'
        '{list_rows}'
        '</screen>'
    ).format(
        sw=sw, sh=sh,
        tx=_TITLE_X, ty=_TITLE_Y, tw=_TITLE_W, th=_TITLE_H,
        tby=_TITLE_Y + _TITLE_H, tbs=3 if IS_FHD else 2,
        lx=logo_lx, ly=logo_ly, lw=logo_lw, lh=logo_lh,
        tpx=tpx, ttw=ttw, stx=stx, stw=stw, vtx=vtx, vtw=vtw, ver=PLUGIN_VERSION,
        cy=_CONTENT_Y, ch=_CONTENT_H,
        tfs=tfs, sfs=sfs, ifs=ifs,
        legend=legend,
        tiles=tiles_xml,
        list_rows=list_xml,
    )


def _build_settings_skin():
    n = len(_get_settings_list())
    if IS_FHD:
        sx, sy          = 510, 200
        sw              = 900
        tf, rf          = 36, 26
        row_h           = 70
        row_y0          = 110
        sel_h           = 60
        val_w           = 420
        leg_h           = 60
        sh              = 140 + n * row_h + 40 + leg_h
        leg_y           = sh - leg_h
        pip_py          = leg_y + (leg_h - 30) // 2
        pip_h, pip_w    = 30, 6
        lfs             = 26
        rp_x, r_x, r_w  = 20,  36,  248
        gp_x, g_x, g_w  = 306, 322, 248
        ok_x,  ok_w     = 590, 260
    else:
        sx, sy          = 340, 133
        sw              = 600
        tf, rf          = 24, 18
        row_h           = 47
        row_y0          = 73
        sel_h           = 40
        val_w           = 260
        leg_h           = 40
        sh              = 93 + n * row_h + 27 + leg_h
        leg_y           = sh - leg_h
        pip_py          = leg_y + (leg_h - 20) // 2
        pip_h, pip_w    = 20, 5
        lfs             = 18
        rp_x, r_x, r_w  = 15,  28,  164
        gp_x, g_x, g_w  = 215, 228, 164
        ok_x,  ok_w     = 410, 170

    rows_xml = ""
    for i in range(n):
        y = row_y0 + i * row_h
        rows_xml += (
            '<widget name="s_sel_{i}" position="0,{y}" size="{w},{sh}" '
            'backgroundColor="#11cc0066" zPosition="-1" transparent="0"/>'
            '<widget name="s_label_{i}" position="20,{y}" size="{lw},{sh}" '
            'zPosition="1" font="Regular;{rf}" halign="left" '
            'valign="center" foregroundColor="#00E0E0E0" backgroundColor="#33000000" transparent="1"/>'
            '<widget name="s_value_{i}" position="{vx},{y}" size="{vw},{sh}" '
            'zPosition="1" font="Regular;{rf}" halign="right" noWrap="1" '
            'valign="center" foregroundColor="#00FFFFFF" backgroundColor="#33000000" transparent="1"/>'
        ).format(
            i=i, y=y, w=sw, sh=sel_h,
            lw=sw - val_w - 40,
            vx=sw - val_w - 20, vw=val_w,
            rf=rf,
        )

    legend_xml = (
        '<eLabel backgroundColor="#1A000000" position="0,{ly}" size="{sw},{lh}" zPosition="-2" transparent="0"/>'
        '<eLabel backgroundColor="#1AEE0000" position="{rpx},{ppy}" size="{pw},{ph}" zPosition="2" transparent="0"/>'
        '<widget name="hint_red"   position="{rx},{ly}"  size="{rw},{lh}" zPosition="4" transparent="1" backgroundColor="#1A000000" font="Regular;{lfs}" halign="left"  valign="center" foregroundColor="#CCCCCC" noWrap="1"/>'
        '<eLabel backgroundColor="#1A00AA00" position="{gpx},{ppy}" size="{pw},{ph}" zPosition="2" transparent="0"/>'
        '<widget name="hint_green" position="{gx},{ly}"  size="{gw},{lh}" zPosition="4" transparent="1" backgroundColor="#1A000000" font="Regular;{lfs}" halign="left"  valign="center" foregroundColor="#CCCCCC" noWrap="1"/>'
        '<widget name="hint_ok"    position="{okx},{ly}" size="{okw},{lh}" zPosition="4" transparent="1" backgroundColor="#1A000000" font="Regular;{lfs}" halign="left"  valign="center" foregroundColor="#CCCCCC" noWrap="1"/>'
    ).format(
        sw=sw, lh=leg_h, ly=leg_y, lfs=lfs,
        pw=pip_w, ph=pip_h, ppy=pip_py,
        okx=ok_x, okw=ok_w,
        gpx=gp_x, gx=g_x, gw=g_w,
        rpx=rp_x, rx=r_x, rw=r_w,
    )

    return (
        '<screen backgroundColor="transparent" flags="wfNoBorder" '
        'position="{sx},{sy}" size="{sw},{sh}">'
        '<eLabel backgroundColor="#1A000000" position="0,0" size="{sw},{sh}" zPosition="-5" transparent="0"/>'
        '<eLabel backgroundColor="#cc0066" position="0,0" size="{sw},4" zPosition="1" transparent="0"/>'
        '<widget name="s_title" position="20,20" size="{tw},{th}" '
        'zPosition="1" font="Regular;{tf}" halign="left" '
        'valign="center" foregroundColor="#00cc0066" backgroundColor="#1A000000"/>'
        '{rows}'
        '{legend}'
        '</screen>'
    ).format(
        sx=sx, sy=sy, sw=sw, sh=sh,
        tw=sw - 40, th=tf + 14,
        tf=tf,
        rows=rows_xml,
        legend=legend_xml,
    )


# ------------------------------------------------------------------
# Verzeichnis-Browser (Download-Ordner waehlen) - Pfade werden konsequent
# als UTF-8-Byte-Strings behandelt (nicht unicode), das ist die bewaehrte
# Loesung fuer Umlaute in Pfaden unter Python 2.7 (siehe Memory
# feedback_python27_enigma2_pitfalls, Methode aus e2-oe-mediathek uebernommen
# und dort bereits live mit Umlaut-Ordnern verifiziert).
# ------------------------------------------------------------------
_DB_LIST_ROWS = 10


class MagentaMusikDirBrowser(Screen):

    @staticmethod
    def _make_skin():
        if IS_FHD:
            lx, ly0, lw, rh, rf = 40, 150, 1320, 58, 32
        else:
            lx, ly0, lw, rh, rf = 27, 100, 880, 38, 21
        list_xml = ""
        for i in range(_DB_LIST_ROWS):
            y = ly0 + i * rh
            list_xml += (
                '<widget name="list_sel_{i}" position="{x},{y}" size="{w},{rh}" '
                'backgroundColor="#11cc0066" zPosition="1" transparent="0"/>'
                '<widget name="list_label_{i}" position="{lbx},{y}" size="{lbw},{rh}" '
                'zPosition="2" font="Regular;{rf}" halign="left" valign="center" '
                'foregroundColor="#00CCCCCC" backgroundColor="#33000000" transparent="1" noWrap="1"/>'
            ).format(i=i, x=lx, y=y, w=lw, lbx=lx + 12, lbw=lw - 12, rh=rh, rf=rf)

        if IS_FHD:
            return (
                '<screen backgroundColor="transparent" flags="wfNoBorder" '
                'position="260,140" size="1400,800">'
                '<eLabel position="0,0" size="1400,800" backgroundColor="#33000000" zPosition="-6"/>'
                '<eLabel position="0,0" size="1400,4" backgroundColor="#cc0066" zPosition="1"/>'
                '<widget name="title_label" position="40,20" size="1320,60" font="Regular;38" '
                'halign="center" foregroundColor="#00cc0066" transparent="1"/>'
                '<widget name="path_label" position="40,90" size="1320,50" font="Regular;28" '
                'foregroundColor="#00AAAAAA" transparent="1" noWrap="1"/>'
                + list_xml +
                '<widget name="hint_label" position="40,730" size="1320,50" font="Regular;28" '
                'halign="center" foregroundColor="#00AAAAAA" transparent="1"/>'
                '</screen>'
            )
        return (
            '<screen backgroundColor="transparent" flags="wfNoBorder" '
            'position="173,93" size="933,534">'
            '<eLabel position="0,0" size="933,534" backgroundColor="#33000000" zPosition="-6"/>'
            '<eLabel position="0,0" size="933,3" backgroundColor="#cc0066" zPosition="1"/>'
            '<widget name="title_label" position="27,13" size="880,40" font="Regular;25" '
            'halign="center" foregroundColor="#00cc0066" transparent="1"/>'
            '<widget name="path_label" position="27,60" size="880,33" font="Regular;19" '
            'foregroundColor="#00AAAAAA" transparent="1" noWrap="1"/>'
            + list_xml +
            '<widget name="hint_label" position="27,487" size="880,33" font="Regular;19" '
            'halign="center" foregroundColor="#00AAAAAA" transparent="1"/>'
            '</screen>'
        )

    def __init__(self, session, start_dir=None):
        self.skin = self._make_skin()
        Screen.__init__(self, session)
        self._cur     = start_dir or b"/"
        self._entries = []
        self._sel     = 0
        self._scroll  = 0
        self._result  = None

        self["title_label"] = Label(_b("Download-Ordner wählen"))
        self["path_label"]  = Label(_b(self._cur))
        self["hint_label"]  = Label(_b("OK = Öffnen/Wählen   |   Gelb = Neuer Ordner   |   EXIT = Abbrechen"))

        for i in range(_DB_LIST_ROWS):
            self["list_sel_%d"   % i] = Label(_b(""))
            self["list_label_%d" % i] = Label(_b(""))
            self["list_sel_%d"   % i].hide()
            self["list_label_%d" % i].hide()

        self["actions"] = ActionMap(
            ["OkCancelActions", "DirectionActions", "ColorActions", "ListboxActions"],
            {
                "ok":           self._on_ok,
                "cancel":       self._on_cancel,
                "yellow":       self._new_folder,
                "up":           self._step(-1),
                "down":         self._step(1),
                "upRepeated":   self._step(-1),
                "downRepeated": self._step(1),
                "left":         self._page(-1),
                "right":        self._page(1),
                "pageUp":       self._page(-1),
                "pageDown":     self._page(1),
            },
            -1,
        )
        self._fill(self._cur)

    # kleine Wrapper, damit ActionMap echte Methodenreferenzen statt
    # Lambdas mit spaetem Binding bekommt
    def _step(self, delta):
        return lambda: self._list_step(delta)

    def _page(self, direction):
        return lambda: self._list_page(direction)

    @staticmethod
    def _normalize_path(path):
        if not isinstance(path, bytes):
            path = path.encode("utf-8")
        return path

    def _fill(self, path):
        path = self._normalize_path(path)
        self._cur = path
        entries = []
        if path not in (b"/",):
            entries.append((u"[..] Übergeordneter Ordner", None))
        entries.append((u"»  Hier speichern", path))
        try:
            names = sorted(os.listdir(path))
            for name in names:
                if not isinstance(name, bytes):
                    name = name.encode("utf-8")
                full = os.path.join(path, name)
                if os.path.isdir(full):
                    label = u"[" + name.decode("utf-8", "replace") + u"]"
                    entries.append((label, full))
        except Exception as e:
            _dbg("DirBrowser _fill Fehler: %s" % e)
        self._entries = entries
        self._sel = 0
        self._scroll = 0
        self["path_label"].setText(_b(path))
        self._render_list()

    def _render_list(self):
        rows  = _DB_LIST_ROWS
        total = len(self._entries)
        if total == 0:
            self._sel = self._scroll = 0
        else:
            self._sel = max(0, min(self._sel, total - 1))
            if self._sel < self._scroll:
                self._scroll = self._sel
            elif self._sel >= self._scroll + rows:
                self._scroll = self._sel - rows + 1
            self._scroll = max(0, min(self._scroll, max(0, total - rows)))
        for i in range(rows):
            abs_idx = self._scroll + i
            if abs_idx < total:
                label, _full = self._entries[abs_idx]
                self["list_label_%d" % i].setText(_b(label))
                self["list_label_%d" % i].show()
                if abs_idx == self._sel:
                    self["list_sel_%d" % i].show()
                else:
                    self["list_sel_%d" % i].hide()
            else:
                self["list_sel_%d"   % i].hide()
                self["list_label_%d" % i].hide()

    def _list_step(self, step):
        total = len(self._entries)
        if total == 0:
            return
        self._sel = (self._sel + step) % total
        self._render_list()

    def _list_page(self, direction):
        if not self._entries:
            return
        self._sel = max(0, min(self._sel + direction * _DB_LIST_ROWS, len(self._entries) - 1))
        self._render_list()

    def _on_ok(self):
        if not self._entries or self._sel >= len(self._entries):
            return
        _label, full = self._entries[self._sel]
        if full is None:
            parent = os.path.dirname(self._cur.rstrip(b"/")) or b"/"
            self._fill(parent)
        elif full == self._cur:
            self._result = self._cur
            self.close()
        else:
            self._fill(full)

    def _new_folder(self):
        self.session.openWithCallback(
            self._create_folder, VirtualKeyBoard, title=_b("Neuer Ordnername:"), text="")

    def _create_folder(self, name):
        if not name:
            return
        name = name.strip()
        if not name:
            return
        try:
            new_path = os.path.join(self._cur, self._normalize_path(name))
            os.makedirs(new_path)
            self._fill(self._cur)
        except Exception as e:
            _dbg("DirBrowser _create_folder Fehler: %s" % e)

    def _on_cancel(self):
        self._result = None
        self.close()


# ------------------------------------------------------------------
# Ja/Nein-Bestaetigung (z.B. "Einstellungen ohne Speichern verlassen?") -
# kleine Cursor-Liste mit zwei Zeilen statt Farbtasten, analog zum
# _sa_confirm()/_SAChoiceScreen-Muster aus e2-StreamAnything: Hoch/Runter
# bewegt die Auswahl, OK bestaetigt, EXIT bricht sicher mit "Nein" ab.
# ------------------------------------------------------------------
class MagentaMusikConfirmScreen(Screen):

    @staticmethod
    def _make_skin():
        if IS_FHD:
            sw, mh, row_h, hint_h = 700, 110, 60, 50
            mf, rf, hf = 30, 28, 24
        else:
            sw, mh, row_h, hint_h = 460, 73, 40, 33
            mf, rf, hf = 20, 19, 16
        margin = 20 if IS_FHD else 14
        row_y0 = margin + mh + (10 if IS_FHD else 7)
        hint_y = row_y0 + 2 * row_h + (10 if IS_FHD else 7)
        sh     = hint_y + hint_h
        sx = (_SCREEN_W - sw) // 2
        sy = (_SCREEN_H - sh) // 2
        rows_xml = ""
        for i in range(2):
            y = row_y0 + i * row_h
            rows_xml += (
                '<widget name="c_sel_{i}" position="0,{y}" size="{sw},{rh}" '
                'backgroundColor="#11cc0066" zPosition="1" transparent="0"/>'
                '<widget name="c_label_{i}" position="20,{y}" size="{lw},{rh}" '
                'zPosition="2" font="Regular;{rf}" halign="left" valign="center" '
                'foregroundColor="#00E0E0E0" backgroundColor="#33000000" transparent="1"/>'
            ).format(i=i, y=y, sw=sw, rh=row_h, lw=sw - 40, rf=rf)
        return (
            '<screen backgroundColor="transparent" flags="wfNoBorder" '
            'position="{sx},{sy}" size="{sw},{sh}">'
            '<eLabel position="0,0" size="{sw},{sh}" backgroundColor="#1A000000" zPosition="-5" transparent="0"/>'
            '<eLabel position="0,0" size="{sw},4" backgroundColor="#cc0066" zPosition="1" transparent="0"/>'
            '<widget name="message" position="{m},{m}" size="{mw},{mh}" font="Regular;{mf}" '
            'halign="center" valign="center" foregroundColor="#00FFFFFF" backgroundColor="#1A000000" transparent="1"/>'
            '{rows}'
            '<widget name="hint_label" position="0,{hy}" size="{sw},{hh}" zPosition="4" '
            'transparent="1" backgroundColor="#1A000000" font="Regular;{hf}" halign="center" '
            'valign="center" foregroundColor="#AAAAAA"/>'
            '</screen>'
        ).format(
            sx=sx, sy=sy, sw=sw, sh=sh, m=margin, mw=sw - 2 * margin, mh=mh, mf=mf,
            rows=rows_xml, hy=hint_y, hh=hint_h, hf=hf,
        )

    def __init__(self, session, message):
        self.skin = self._make_skin()
        Screen.__init__(self, session)
        self._sel = 1  # Cursor startet auf "Nein" - sicherer Default
        self["message"]    = Label(_b(message))
        self["c_label_0"]  = Label(_b("Ja"))
        self["c_label_1"]  = Label(_b("Nein"))
        self["c_sel_0"]    = Label(_b(""))
        self["c_sel_1"]    = Label(_b(""))
        self["hint_label"] = Label(_b("OK = Auswählen   |   EXIT = Abbrechen"))

        self["actions"] = ActionMap(
            ["OkCancelActions", "DirectionActions"],
            {
                "ok":           self._on_ok,
                "cancel":       self._no,
                "up":           self._move,
                "down":         self._move,
                "upRepeated":   self._move,
                "downRepeated": self._move,
            },
            -1,
        )
        self._refresh()

    def _move(self):
        self._sel = 1 - self._sel
        self._refresh()

    def _refresh(self):
        self["c_sel_0"].show() if self._sel == 0 else self["c_sel_0"].hide()
        self["c_sel_1"].show() if self._sel == 1 else self["c_sel_1"].hide()

    def _on_ok(self):
        self.close(self._sel == 0)

    def _no(self):
        self.close(False)


# ------------------------------------------------------------------
# Settings-Screen
# ------------------------------------------------------------------
class MagentaMusikSettingsScreen(Screen):

    skin = _build_settings_skin()

    def __init__(self, session):
        Screen.__init__(self, session)
        self._sel = 0

        self["s_title"] = Label(_b("Einstellungen"))
        for i, (key, label, kind) in enumerate(_get_settings_list()):
            self["s_sel_%d"   % i] = Label(_b(""))
            self["s_label_%d" % i] = Label(_b(label))
            self["s_value_%d" % i] = Label(_b(""))

        self["hint_ok"]    = Label(_b("OK = Ändern"))
        self["hint_green"] = Label(_b("Speichern"))
        self["hint_red"]   = Label(_b("Abbrechen"))

        self["actions"] = ActionMap(
            ["OkCancelActions", "DirectionActions", "ColorActions"],
            {
                "ok":           self._on_ok,
                "cancel":       self._on_red,
                "up":           self._move_up,
                "down":         self._move_down,
                "upRepeated":   self._move_up,
                "downRepeated": self._move_down,
                "green":        self._on_green,
                "red":          self._on_red,
            },
            -1,
        )

        self._pending = {}
        for key, label, kind in _get_settings_list():
            self._pending[key] = _get_setting(key, _SETTINGS_DEFAULTS.get(key, False))
        self._original = dict(self._pending)

        self._refresh()

    def _refresh(self):
        settings = _get_settings_list()
        for i, (key, label, kind) in enumerate(settings):
            val = self._pending.get(key, False)
            if kind == "path":
                self["s_value_%d" % i].setText(_b(_shorten_path(val)))
            else:
                self["s_value_%d" % i].setText(_b("EIN" if val else "AUS"))
            if i == self._sel:
                self["s_sel_%d" % i].show()
            else:
                self["s_sel_%d" % i].hide()

    def _move_up(self):
        n = len(_get_settings_list())
        if n == 0:
            return
        self._sel = (self._sel - 1) % n
        self._refresh()

    def _move_down(self):
        n = len(_get_settings_list())
        if n == 0:
            return
        self._sel = (self._sel + 1) % n
        self._refresh()

    def _on_ok(self):
        key, label, kind = _get_settings_list()[self._sel]
        if kind == "path":
            self._browse_download_dir(key)
            return
        self._pending[key] = not self._pending.get(key, False)
        self._refresh()

    def _browse_download_dir(self, key):
        cur = _b(self._pending.get(key) or _catalog.DEFAULT_DOWNLOAD_DIR)
        start = cur
        while start and start != b"/" and not os.path.isdir(start):
            start = os.path.dirname(start)
        if not start or not os.path.isdir(start):
            start = b"/media"
        self._dir_browser_key = key
        self._dir_browser = self.session.open(MagentaMusikDirBrowser, start)
        self._dir_browser.onClose.append(self._dir_browser_closed)

    def _dir_browser_closed(self):
        try:
            result = self._dir_browser._result
            if result:
                if isinstance(result, bytes):
                    result = result.decode("utf-8", "replace")
                self._pending[self._dir_browser_key] = result
        except Exception:
            pass
        self._refresh()

    def _on_green(self):
        settings = _catalog.get_settings()
        for key, label, kind in _get_settings_list():
            settings[key] = self._pending[key]
        _catalog.save_settings(settings)
        _sync_debug_flag()
        self.close()

    def _on_red(self):
        if self._pending != self._original:
            self.session.openWithCallback(
                self._on_discard_confirmed, MagentaMusikConfirmScreen,
                "Einstellungen ohne Speichern verlassen?")
        else:
            self.close()

    def _on_discard_confirmed(self, confirmed):
        if confirmed:
            self.close()


# ------------------------------------------------------------------
# Download-Queue
# Anders als bei einem bereits vorab gescrapten Katalog liegt hier nur die
# rohe magentamusik.de Event-URL vor - die eigentliche .m3u8-Stream-URL
# wird erst aufgeloest, wenn ein Warteschlangen-Eintrag tatsaechlich an der
# Reihe ist (entweder beim allerersten Enqueue auf dem UI-Thread, exakt das
# bereits etablierte Verhalten beim Abspielen per OK-Taste, oder beim
# Verketten auf den naechsten Eintrag innerhalb des laufenden
# Download-Hintergrundthreads - dort ohne jedes UI-Blocking).
# ------------------------------------------------------------------
_active_downloader  = None
_download_queue     = []   # Liste von {"title","event_url","topic","image_url"}
_bg_download_result = None
_user_cancelled_all  = False


def _downloads_active():
    active = _active_downloader is not None and _active_downloader._thread is not None \
        and _active_downloader._thread.is_alive()
    return bool(active or _download_queue)


def _format_duration(meta):
    # downloader.write_meta()/write_info_txt() interpretieren ein "duration"-
    # Argument mit Doppelpunkt als Uhrzeit-Dauer (MM:SS bzw. H:MM:SS, Sekunden
    # immer als letzter Teil) - magentamusik.de liefert nur volle Minuten ohne
    # Sekunden, ein "H:MM"-String wuerde also faelschlich als "MM Minuten,
    # SS Sekunden" geparst (z.B. "0:02" = 2 Sekunden statt 2 Minuten). Echte
    # Downloads aus e2-oe-mediathek liefern ebenfalls keinen Uhrzeit-String
    # ("17 Min."), wodurch dort die .meta-Laufzeit ohnehin leer bleibt -
    # gleiches Format hier verwenden statt einen eigenen Clock-String zu
    # erfinden.
    minutes = (meta or {}).get("runtime_min") or 0
    if not minutes:
        return None
    return "%d Min." % int(minutes)


def _bg_download_done(fp):
    global _active_downloader
    if _catalog.get_download_convert_ts() and fp and fp.lower().endswith(".mp4"):
        if _active_downloader is not None:
            _active_downloader._converting = True
        convert_mp4_to_ts(fp, on_done=lambda ts: _bg_convert_done(), on_error=lambda e: _queue_next())
    else:
        _queue_next()


def _bg_convert_done():
    if _active_downloader is not None:
        _active_downloader._converting = False
    _queue_next()


def _cancel_current_download():
    if _active_downloader:
        _active_downloader.cancel()


def _cancel_all_downloads():
    global _download_queue, _user_cancelled_all
    _download_queue = []
    if _active_downloader:
        _user_cancelled_all = True
        _active_downloader.cancel()


def _queue_error(msg):
    global _active_downloader, _bg_download_result
    _active_downloader  = None
    _bg_download_result = "err:" + str(msg)
    _dbg("Download-Fehler: %s" % msg)
    _queue_next()


def _queue_next():
    """Stoesst die Verarbeitung der Warteschlange an - laeuft IMMER in einem
    Hintergrundthread, egal von welchem Thread aus aufgerufen. Wichtig fuer
    den allerersten Download: der wird per Tastendruck direkt auf dem
    GUI-Thread ausgeloest, ohne diesen Dispatch wuerde resolve_full() (3
    sequenzielle HTTP-Requests an magentamusik.de) die Oberflaeche blockieren
    (sichtbar als Lade-Spinner). Nachfolgende Downloads laufen ohnehin schon
    im Hintergrundthread des jeweils vorherigen Downloaders - der zusaetzliche
    Thread-Spawn dort ist unschaedlich."""
    t = threading.Thread(target=_queue_next_worker)
    t.daemon = True
    t.start()


def _queue_next_worker():
    """Startet den naechsten Download aus der Warteschlange (loest dabei die
    Event-URL erst jetzt auf), oder meldet alle fertig."""
    global _active_downloader, _download_queue, _bg_download_result, _user_cancelled_all
    if not _download_queue:
        _active_downloader = None
        if _user_cancelled_all:
            _bg_download_result = "cancelled"
            _user_cancelled_all = False
        else:
            _bg_download_result = "ok"
            _notify_downloads_done()
        return
    item = _download_queue.pop(0)
    try:
        from magentamusik import resolve_full as _resolve_full
        stream_url, meta = _resolve_full(item["event_url"])
        if not stream_url:
            raise Exception("Stream konnte nicht aufgelöst werden")
        image_url  = item.get("image_url")
        cover_path = _catalog.fetch_cover_if_missing(image_url) if image_url else None
        dl = Downloader(
            stream_url,
            item["title"],
            _catalog.get_download_dir(),
            topic=item.get("topic"),
            description=(meta or {}).get("description"),
            duration=_format_duration(meta),
            cover_path=cover_path,
            on_done=_bg_download_done,
            on_error=lambda msg: _queue_error(msg),
        )
        dl.on_progress = lambda *a: None
        _active_downloader = dl
        dl.start()
    except Exception as e:
        _dbg("_queue_next Fehler: %s" % e)
        _queue_next_worker()


# Referenzzaehler statt einfachem Bool, da es bei uns (anders als OeMediathek
# mit nur einem MainScreen) zwei verschachtelte Screen-Klassen gibt
# (Festival- und Items-Screen, beide von _BrowseScreenBase): wenn man aus
# einem Festival zurueck zur Liste geht, schliesst nur der innere Screen,
# das Plugin als Ganzes bleibt offen. Erst wenn der letzte _BrowseScreenBase
# schliesst, ist das Plugin wirklich verlassen.
_open_screen_count   = 0
_notify_title_timers = []


def _plugin_is_open():
    return _open_screen_count > 0


def _fire_download_notification():
    if _plugin_is_open():
        return
    try:
        from Tools.Notifications import AddPopup, current_notifications
        _id = "magentamusik_dl_done"
        AddPopup("Alle Downloads abgeschlossen", _MessageBox.TYPE_INFO, timeout=5, id=_id)

        # AddPopup zeigt im Titel standardmaessig "Enigma2" o.ae. an - die
        # Notification traegt ihren eigenen Titel erst, NACHDEM sie tatsaechlich
        # in current_notifications gelandet ist, daher ein kurzer Timer statt
        # direktem Ueberschreiben. Referenz auf den Timer muss modulweit
        # gehalten werden, sonst wird er vor dem Feuern garbage-collected.
        def _set_title():
            global _notify_title_timers
            _notify_title_timers = []
            for entry in current_notifications:
                try:
                    if entry[0] == _id:
                        entry[1].origTitle = "MagentaMusik"
                        entry[1].setTitle("MagentaMusik")
                except Exception:
                    pass

        t = eTimer()
        t.callback.append(_set_title)
        t.start(100, True)
        _notify_title_timers.append(t)
    except Exception:
        pass


def _notify_downloads_done():
    if _plugin_is_open():
        return
    try:
        from twisted.internet import reactor
        reactor.callFromThread(_fire_download_notification)
    except Exception:
        pass


def _enqueue_download(title, event_url, topic, image_url):
    """Reiht einen Download ein und startet ihn sofort, falls gerade nichts
    laeuft. Gibt "queued" oder "started" fuer eine kurze Statusmeldung zurueck."""
    global _active_downloader, _download_queue
    entry = {"title": title, "event_url": event_url, "topic": topic, "image_url": image_url}
    active_thread = _active_downloader._thread if _active_downloader else None
    if active_thread is not None and active_thread.is_alive():
        _download_queue.append(entry)
        return "queued"
    _active_downloader = None
    _download_queue.append(entry)
    _queue_next()
    return "started"


# ------------------------------------------------------------------
# Live-Aufnahme: parallele Hintergrund-Aufnahmen (kein Warteschlangen-
# Modell wie bei VOD-Downloads oben - eine wartende Live-Aufnahme wuerde
# den gewuenschten Moment verpassen, daher laufen beliebig viele Aufnahmen
# gleichzeitig statt eine aktiv + Rest in Reihe). Portiert aus
# StreamAnything/plugin.py (dort auf der Box vollstaendig verifiziert,
# siehe Memory project_live_recording_feature).
# ------------------------------------------------------------------
_active_recordings = []
_recordings_lock    = threading.Lock()


def _get_active_recordings():
    with _recordings_lock:
        return list(_active_recordings)


def _start_recording(item, duration_seconds):
    raw_url = item.get("url", "")
    if not raw_url:
        return
    name = item.get("name", "Aufnahme")
    t = threading.Thread(target=_start_recording_bg, args=(raw_url, name, duration_seconds, None))
    t.daemon = True
    t.start()


def _start_recording_from_timer(timer):
    # Vom Scheduler (_check_recording_timers) zur geplanten Zeit aufgerufen.
    # timer_id wird durchgereicht, damit beim Abschluss der recording_timers-
    # Eintrag korrekt auf done/error gesetzt werden kann.
    _catalog.update_recording_timer_status(timer.get("id"), "running")
    t = threading.Thread(target=_start_recording_bg, args=(
        timer.get("url", ""), timer.get("name", "Aufnahme"),
        timer.get("duration"), timer.get("id"),
    ))
    t.daemon = True
    t.start()


def _start_recording_bg(raw_url, name, duration_seconds, timer_id):
    # magentamusik.resolve() macht bis zu 3 sequenzielle HTTP-Requests - im
    # Hintergrundthread, sonst friert bei einem Netzwerk-Haenger der
    # komplette Enigma2-Prozess (inkl. WebIF, gleicher GIL) ein.
    try:
        from magentamusik import is_magentamusik as _is_mm, resolve as _resolve
        url = _resolve(raw_url) if _is_mm(raw_url) else raw_url
    except Exception:
        url = None

    def _on_finished(rec, *args):
        _on_recording_finished(rec, *args)
        if timer_id:
            try:
                _catalog.update_recording_timer_status(timer_id, "error" if args else "done")
            except Exception:
                pass

    if not url:
        _dbg("Aufnahme-Start fehlgeschlagen: %s nicht aufloesbar" % name)
        if timer_id:
            try:
                _catalog.update_recording_timer_status(timer_id, "error")
            except Exception:
                pass
        return

    save_dir = _catalog.get_download_dir()
    if not os.path.isdir(save_dir):
        try:
            os.makedirs(save_dir)
        except Exception:
            pass

    rec = HLSRecorder(
        url, name, save_dir, duration=duration_seconds,
        on_done=_on_finished, on_error=_on_finished,
    )
    with _recordings_lock:
        _active_recordings.append(rec)
    rec.start()


def _on_recording_finished(rec, *args):
    # Gemeinsamer Callback fuer on_done (rec) und on_error (rec, err) -
    # in beiden Faellen einfach aus der Liste der laufenden Aufnahmen
    # entfernen, Fehlerdetails landen ohnehin nur im Debug-Log.
    with _recordings_lock:
        if rec in _active_recordings:
            _active_recordings.remove(rec)
    if args:
        _dbg("Aufnahme-Fehler: %s - %s" % (rec.title, args[0]))
    else:
        _dbg("Aufnahme fertig: %s -> %s" % (rec.title, rec.filepath))


def _cancel_recording(rec):
    rec.cancel()


# ------------------------------------------------------------------
# Deep-Standby-Wecktimer: ein reiner "justplay"-Eintrag im nativen
# Enigma2-RecordTimer-System, der NICHTS aufnimmt - er dient ausschliesslich
# dazu, die Box rechtzeitig aus dem Deep-Standby zu wecken (Enigma2s
# RTC-Aufwach-Mechanismus beruecksichtigt alle anstehenden Timer-Eintraege,
# nicht nur echte Aufnahmen). Der tatsaechliche Aufnahme-Start passiert
# danach ausschliesslich ueber unseren eigenen Scheduler weiter unten,
# sobald die Box wieder laeuft. dontSave=True haelt ihn aus der dauerhaft
# gespeicherten Timer-Liste raus - nach einem echten Reboot wird er daher
# in _start_scheduler() fuer alle noch offenen Timer frisch neu registriert.
# ------------------------------------------------------------------
_WAKEUP_NAME_PREFIX     = "MagentaMusik-Wecktimer: "
_wakeup_reregistered    = False


def _register_wakeup_timer(timer_id, name, start_time):
    try:
        import NavigationInstance
        if NavigationInstance.instance is None:
            _dbg("Wecktimer-Registrierung: NavigationInstance.instance ist None")
            return
        from RecordTimer import RecordTimerEntry
        from ServiceReference import ServiceReference
        ref = NavigationInstance.instance.getCurrentlyPlayingServiceReference()
        if ref is None:
            from enigma import eServiceReference
            ref = eServiceReference(eServiceReference.idDVB, 0)
        # Eigener Name statt des aktuell laufenden Senders, damit der reine
        # Wecktimer in der nativen Timer-Liste nicht mit einem zufaelligen/
        # verwirrenden Kanalnamen auftaucht (Zap-Ziel bleibt unveraendert,
        # nur die Anzeige wird ueberschrieben).
        ref.setName(_b("MagentaMusik"))
        begin = int(start_time)
        end   = begin + 300
        entry_name = _u(_WAKEUP_NAME_PREFIX) + u"%s [%s]" % (_u(name), timer_id)
        entry = RecordTimerEntry(ServiceReference(ref), begin, end, _b(entry_name), _b(""), None, justplay=True)
        entry.dontSave = True
        NavigationInstance.instance.RecordTimer.record(entry)
        _dbg("Wecktimer registriert: %s @ %s" % (entry_name, begin))
    except Exception as e:
        _dbg("Wecktimer-Registrierung fehlgeschlagen: %s" % e)


def _unregister_wakeup_timer(timer_id):
    try:
        import NavigationInstance
        if NavigationInstance.instance is None:
            return
        rt = NavigationInstance.instance.RecordTimer
        suffix = u"[%s]" % timer_id
        for entry in list(rt.timer_list) + list(rt.processed_timers):
            ename = _u(entry.name) if entry.name else u""
            if ename.startswith(_u(_WAKEUP_NAME_PREFIX)) and ename.endswith(suffix):
                rt.removeEntry(entry)
    except Exception as e:
        _dbg("Wecktimer-Entfernung fehlgeschlagen: %s" % e)


def _has_wakeup_timer(timer_id):
    try:
        import NavigationInstance
        if NavigationInstance.instance is None:
            return False
        rt = NavigationInstance.instance.RecordTimer
        suffix = u"[%s]" % timer_id
        for entry in list(rt.timer_list) + list(rt.processed_timers):
            ename = _u(entry.name) if entry.name else u""
            if ename.startswith(_u(_WAKEUP_NAME_PREFIX)) and ename.endswith(suffix):
                return True
    except Exception:
        pass
    return False


def _get_valid_pending_timers():
    """Pending-Timer aus JSON, die noch einen aktiven Wecktimer-Eintrag haben.
    Timer, deren RecordTimerEntry im VTI-Editor extern gelöscht wurde,
    werden dabei still aus JSON entfernt."""
    import time as _time
    now = _time.time()
    result = []
    for t in _catalog.get_recording_timers():
        if t.get("status") != "pending":
            continue
        start = t.get("start_time", 0)
        if now >= start:
            result.append(t)
            continue
        if not _wakeup_reregistered or _has_wakeup_timer(t.get("id")):
            result.append(t)
        else:
            _catalog.delete_recording_timer(t.get("id"))
            _dbg("Wecktimer extern geloescht (VTI?), JSON-Eintrag entfernt: %s" % t.get("name"))
    return result


# ------------------------------------------------------------------
# Timer-Scheduler: prueft periodisch, ob ein geplanter recording_timer
# faellig ist. Laeuft unabhaengig davon, ob die Plugin-GUI offen ist
# (gestartet aus autostart() bei Enigma2-Boot) - deckt zusammen mit dem
# Wecktimer oben sowohl "Box an"/normales Standby als auch Deep-Standby ab.
# ------------------------------------------------------------------
_scheduler_timer = None
_TIMER_LATE_GRACE_SECONDS = 600  # mehr als 10min zu spaet -> Box war vermutlich aus, nicht mehr sinnvoll starten


def _check_recording_timers():
    import time as _time
    now = _time.time()
    for t in _get_valid_pending_timers():
        start = t.get("start_time", 0)
        if now < start:
            continue
        _unregister_wakeup_timer(t.get("id"))
        if now - start > _TIMER_LATE_GRACE_SECONDS:
            _catalog.update_recording_timer_status(t.get("id"), "error")
            _dbg("Timer verpasst (Box vermutlich aus): %s" % t.get("name"))
            continue
        _start_recording_from_timer(t)


_wakeup_reregister_timer = None


def _reregister_wakeup_timers():
    # Nach einem echten Reboot/GUI-Neustart sind alle dontSave=True-
    # Wecktimer weg (siehe _register_wakeup_timer) - fuer alle noch offenen
    # Timer frisch neu registrieren, sonst wuerde ein geplanter Deep-
    # Standby-Wakeup nach einem Neustart verpasst. Laeuft verzoegert (siehe
    # _start_scheduler), weil NavigationInstance.instance direkt beim
    # Boot/Plugin-Start noch None ist (Session ist da noch nicht bereit).
    global _wakeup_reregistered
    pending = [t for t in _catalog.get_recording_timers() if t.get("status") == "pending"]
    _dbg("_reregister_wakeup_timers: %d pending Timer" % len(pending))
    for t in pending:
        _register_wakeup_timer(t.get("id"), t.get("name", "Aufnahme"), t.get("start_time", 0))
    _wakeup_reregistered = True


def _start_scheduler():
    global _scheduler_timer, _wakeup_reregister_timer
    if _scheduler_timer is not None:
        return
    _scheduler_timer = eTimer()
    _scheduler_timer.callback.append(_check_recording_timers)
    _scheduler_timer.start(30000, False)

    _wakeup_reregister_timer = eTimer()
    _wakeup_reregister_timer.callback.append(_reregister_wakeup_timers)
    _wakeup_reregister_timer.start(8000, True)


def _open_record_duration_menu(session, item):
    presets = [
        ("30 Minuten",     30 * 60),
        ("1 Stunde",       60 * 60),
        ("2 Stunden",      2 * 60 * 60),
        ("3 Stunden",      3 * 60 * 60),
        ("6 Stunden",      6 * 60 * 60),
        ("Bis ich stoppe", None),
    ]
    choices = [(_b(label), seconds) for label, seconds in presets]
    choices.append((_b("Eigene Dauer (Minuten) …"), "custom"))
    choices.append((_b("Für später planen …"), "schedule"))

    def on_custom_minutes(text):
        if not text:
            return
        try:
            minutes = int(_u(text).strip())
        except (ValueError, TypeError):
            return
        if minutes <= 0:
            return
        _start_recording(item, minutes * 60)

    def on_duration(choice):
        if choice is None:
            return
        if choice[1] == "custom":
            session.openWithCallback(on_custom_minutes, VirtualKeyBoard,
                                     title=_b("Dauer in Minuten eingeben:"), text="")
        elif choice[1] == "schedule":
            _open_native_timer_editor(session, item)
        else:
            _start_recording(item, choice[1])

    session.openWithCallback(on_duration, ChoiceBox,
                             title=_b("Aufnahmedauer wählen"), list=choices)


def _open_native_timer_editor(session, item):
    # Nutzt Enigma2s eingebauten Timer-Editor NUR als Eingabemaske fuer
    # Start-/Endzeit (native Datum/Uhrzeit-Spinner, viel angenehmer per
    # Fernbedienung als Texteingabe). Der editierte Eintrag wird NICHT
    # selbst als nativer Timer registriert - es werden nur begin/end aus
    # dem Ergebnis ausgelesen und daraus ganz normal ein eigener
    # recording_timer angelegt, inkl. Wecktimer.
    try:
        from Screens.TimerEntry import TimerEntry
        from ServiceReference import ServiceReference
        from RecordTimer import RecordTimerEntry
        import NavigationInstance
        import time as _time

        ref = None
        if NavigationInstance.instance is not None:
            ref = NavigationInstance.instance.getCurrentlyPlayingServiceReference()
        if ref is None:
            from enigma import eServiceReference
            ref = eServiceReference(eServiceReference.idDVB, 0)
        ref.setName(_b("MagentaMusik"))

        name  = item.get("name", "Aufnahme")
        begin = int(_time.time()) + 3600
        end   = begin + 3600
        draft = RecordTimerEntry(ServiceReference(ref), begin, end, _b(name), _b(""), None, justplay=True)

        def on_edited(answer):
            if not answer or not answer[0]:
                return
            entry = answer[1]
            timer = _catalog.add_recording_timer(
                item.get("name", "Aufnahme"), item.get("url", ""),
                entry.begin, "", max(60, entry.end - entry.begin),
            )
            _register_wakeup_timer(timer["id"], timer["name"], timer["start_time"])

        session.openWithCallback(on_edited, TimerEntry, draft)
    except Exception as e:
        _dbg("Nativer Timer-Editor fehlgeschlagen: %s" % e)


class MagentaMusikRecordingsScreen(Screen):
    if IS_FHD:
        skin = """
        <screen name="MagentaMusikRecordingsScreen" position="360,175" size="1200,730" flags="wfNoBorder">
            <eLabel position="0,0" size="1200,730" backgroundColor="#33000000" zPosition="-6" />
            <eLabel position="0,0" size="1200,4" backgroundColor="#cc0066" zPosition="1" />
            <widget name="title_label" position="40,30"  size="1120,60"  font="Regular;36" halign="center" foregroundColor="#00cc0066" transparent="1" />
            <eLabel position="40,110" size="1120,2" backgroundColor="#44FFFFFF" zPosition="1" />
            <widget name="rec_label"  position="40,130" size="1120,540" font="Regular;28" halign="left" valign="top" foregroundColor="#FFFFFF" transparent="1" />
            <eLabel position="40,690" size="8,40" backgroundColor="#CC0000" zPosition="2" />
            <widget name="hint_red"   position="56,684"  size="500,50" font="Regular;28" halign="left"  valign="center" foregroundColor="#CCCCCC" transparent="1" />
            <widget name="hint_exit"  position="780,684" size="380,50" font="Regular;28" halign="right" valign="center" foregroundColor="#AAAAAA" transparent="1" />
        </screen>"""
    else:
        skin = """
        <screen name="MagentaMusikRecordingsScreen" position="240,116" size="800,488" flags="wfNoBorder">
            <eLabel position="0,0" size="800,488" backgroundColor="#33000000" zPosition="-6" />
            <eLabel position="0,0" size="800,3" backgroundColor="#cc0066" zPosition="1" />
            <widget name="title_label" position="27,20"  size="746,40"  font="Regular;24" halign="center" foregroundColor="#00cc0066" transparent="1" />
            <eLabel position="27,72" size="746,2" backgroundColor="#44FFFFFF" zPosition="1" />
            <widget name="rec_label"  position="27,82"  size="746,358" font="Regular;19" halign="left" valign="top" foregroundColor="#FFFFFF" transparent="1" />
            <eLabel position="27,452" size="5,27" backgroundColor="#CC0000" zPosition="2" />
            <widget name="hint_red"   position="38,449"  size="330,33" font="Regular;19" halign="left"  valign="center" foregroundColor="#CCCCCC" transparent="1" />
            <widget name="hint_exit"  position="520,449" size="253,33" font="Regular;19" halign="right" valign="center" foregroundColor="#AAAAAA" transparent="1" />
        </screen>"""

    def __init__(self, session):
        Screen.__init__(self, session)
        self._sel = 0

        self["title_label"] = Label(_b("Aufnahmen"))
        self["rec_label"]   = Label(_b(""))
        self["hint_red"]    = Label(_b("Markierte Aufnahme/Timer stoppen"))
        self["hint_exit"]   = Label(_b("EXIT = Schließen"))

        self["actions"] = ActionMap(
            ["OkCancelActions", "DirectionActions", "ColorActions"],
            {
                "cancel":       self.close,
                "ok":           self.close,
                "up":           lambda: self._move(-1),
                "down":         lambda: self._move(1),
                "upRepeated":   lambda: self._move(-1),
                "downRepeated": lambda: self._move(1),
                "red":          self._stop_selected,
            },
            -1,
        )

        self._poll_timer = eTimer()
        self._poll_timer.callback.append(self._poll)
        self._poll_timer.start(1000, False)
        self.onClose.append(self.__stop_timer)
        self._poll()

    def __stop_timer(self):
        try:
            self._poll_timer.stop()
        except Exception:
            pass

    def _get_items(self):
        active  = [("active",  rec) for rec in _get_active_recordings()]
        pending = [("pending", t)   for t in _get_valid_pending_timers()]
        return active + pending

    def _move(self, delta):
        items = self._get_items()
        if not items:
            return
        self._sel = (self._sel + delta) % len(items)
        self._render(items)

    def _stop_selected(self):
        items = self._get_items()
        if not items or self._sel >= len(items):
            return
        kind, obj = items[self._sel]
        if kind == "active":
            _cancel_recording(obj)
        else:
            _catalog.delete_recording_timer(obj["id"])
            _unregister_wakeup_timer(obj["id"])

    def _poll(self):
        items = self._get_items()
        if self._sel >= len(items):
            self._sel = max(0, len(items) - 1)
        self._render(items)

    def _render(self, items):
        import time as _time
        if not items:
            self["rec_label"].setText(_b("Keine laufende Aufnahme oder geplanter Timer"))
            return
        lines = []
        for i, (kind, obj) in enumerate(items):
            marker = u"> " if i == self._sel else u"   "
            if kind == "active":
                title = _u(obj.title)
                limit = format_duration(obj.duration) if obj.duration else u"unbegrenzt"
                lines.append(u"%s%s\n   %s / %s  -  %s" % (
                    marker, title, format_duration(obj.elapsed()), limit, format_size(obj._downloaded)
                ))
            else:
                title = _u(obj.get("name", u"?"))
                when  = _time.strftime("%d.%m. %H:%M", _time.localtime(obj.get("start_time", 0)))
                lines.append(u"%sgeplant: %s\n   %s" % (marker, when, title))
        self["rec_label"].setText(_b(u"\n\n".join(lines)))


# ------------------------------------------------------------------
# Gemeinsame Kachel-/Listen-Engine fuer Festival- und Item-Screen
# ------------------------------------------------------------------
class _BrowseScreenBase(Screen):

    skin = _build_skin()

    def __init__(self, session, title):
        global _open_screen_count
        _open_screen_count += 1
        Screen.__init__(self, session)
        self._page             = 0
        self._sel               = 0
        self._items             = []
        self._error              = None
        self._list_mode          = _get_setting("list_mode", False)
        self._list_sel           = 0
        self._list_scroll        = 0
        self._prev_render_mode   = None
        self._closed             = False
        # (kind, idx) -> image_url, den dieser Slot gerade erwartet. Wird beim
        # Eintreffen eines async geladenen Covers geprueft, um veraltete
        # Ergebnisse (Seite/Zeile inzwischen weitergeblaettert) zu verwerfen.
        self._cover_slots        = {}
        self._timer = eTimer()
        self._timer.callback.append(self._load)
        self.onClose.append(self.__mark_closed)

        self["title"]       = Label(_b(title))
        self["status"]      = Label(_b(""))
        self["hint_menu"]   = Label(_b(""))
        self["hint_info"]   = Label(_b(""))
        self["hint_yellow"] = Label(_b(""))
        self["hint_green"]  = Label(_b("Einstellungen"))
        self["hint_red"]    = Label(_b(""))
        self["hint_blue"]   = Label(_b("Downloads"))
        self["blue_pip"]    = Label(_b(""))
        self["page_label"]  = Label(_b(""))

        if _Pixmap:
            self["header_logo"] = _Pixmap()

        for i in range(TILES_PER_PAGE):
            self["tile_bg_%d"    % i] = Label(_b(""))
            self["tile_label_%d" % i] = Label(_b(""))
            self["tile_live_%d"  % i] = Label(_b(""))
            if _Pixmap:
                self["tile_logo_%d" % i]  = _Pixmap()
                self["tile_sel_%d" % i]   = _Pixmap()
                self["tile_type_%d" % i]  = _Pixmap()
            self["tile_bg_%d" % i].hide()
            self["tile_live_%d" % i].hide()

        for i in range(LIST_ROWS):
            self["list_sel_%d"   % i] = Label(_b(""))
            self["list_label_%d" % i] = Label(_b(""))
            self["list_live_%d"  % i] = Label(_b(""))
            if _Pixmap:
                self["list_logo_%d" % i] = _Pixmap()
                self["list_type_%d" % i] = _Pixmap()
            self["list_sel_%d"   % i].hide()
            self["list_label_%d" % i].hide()
            self["list_live_%d"  % i].hide()

        self["actions"] = ActionMap(
            ["OkCancelActions", "DirectionActions", "ColorActions",
             "ChannelSelectBaseActions", "MenuActions", "EPGSelectActions"],
            {
                "ok":                self._ok,
                "playpauseService":  self._ok,
                "cancel":            self._key_cancel,
                "left":              self._key_left,
                "right":             self._key_right,
                "up":                self._key_up,
                "upRepeated":        self._key_up_repeat,
                "down":              self._key_down,
                "downRepeated":      self._key_down_repeat,
                "nextBouquet":       lambda: self._page_nav(1),
                "prevBouquet":       lambda: self._page_nav(-1),
                "green":             self._key_green,
                "yellow":            self._key_yellow,
                "red":               self._key_red,
                "blue":              self._key_blue,
                "menu":              self._key_menu,
                "info":              self._key_info,
            },
            -1,
        )

        self._flash_timer = None
        self._dl_poll_timer = eTimer()
        self._dl_poll_timer.callback.append(self._update_download_hint)
        self._dl_poll_timer.start(1000, False)
        self.onClose.append(self.__stop_dl_timer)

        self._timer.start(50, True)

    def __stop_dl_timer(self):
        try:
            self._dl_poll_timer.stop()
        except Exception:
            pass

    def __mark_closed(self):
        global _open_screen_count
        self._closed = True
        _open_screen_count = max(0, _open_screen_count - 1)

    # --- von Subklassen zu ueberschreiben ---
    def _fetch_items(self):
        return []

    def _on_select_item(self, item, idx):
        pass

    # --- generisches Laden ---
    # _fetch_items() macht Netzwerk-I/O (Festivalliste/Live-Status bzw.
    # Festival-Items, je nach Subklasse) - direkt auf dem GUI-Thread aufgerufen
    # blockiert das die komplette Oberflaeche fuer die Dauer des Requests
    # (sichtbar als Lade-Spinner der Box). Deshalb in einem Hintergrundthread
    # laden, Ergebnis per reactor.callFromThread zurueck auf den GUI-Thread.
    def _load(self):
        self["status"].setText(_b("Lade…"))
        t = threading.Thread(target=self.__load_bg)
        t.daemon = True
        t.start()

    def __load_bg(self):
        try:
            items = self._fetch_items()
            error = _catalog.last_fetch_error()
        except Exception as e:
            items, error = [], str(e)

        def _apply():
            if self._closed:
                return
            self._items = items
            self._error = error
            self._page  = 0
            self._sel   = 0
            self._render()

        try:
            from twisted.internet import reactor
            reactor.callFromThread(_apply)
        except Exception:
            _apply()

    def _key_cancel(self):
        self.close()

    def _key_green(self):
        self.session.openWithCallback(self._on_settings_closed, MagentaMusikSettingsScreen)

    def _on_settings_closed(self):
        self._render()

    def _selected_item(self):
        if self._list_mode:
            if 0 <= self._list_sel < len(self._items):
                return self._items[self._list_sel]
            return None
        idx = self._page * TILES_PER_PAGE + self._sel
        return self._items[idx] if idx < len(self._items) else None

    def _key_red(self):
        item = self._selected_item()
        if not item or item.get("type") != "stream":
            return
        self._start_download(item)

    def _start_download(self, item):
        # von Subklassen ueberschrieben (Festival-Items koennen heruntergeladen
        # werden, Ordner/Live-Eintraege nicht - siehe _key_red)
        pass

    def _key_blue(self):
        if not _downloads_active():
            return
        self.session.open(
            MagentaMusikDownloadManagerScreen,
            lambda: _active_downloader, lambda: _download_queue,
            _cancel_all_downloads, _cancel_current_download,
        )

    def _key_menu(self):
        item = self._selected_item()
        if not item or not item.get("is_live"):
            return
        _open_record_duration_menu(self.session, item)

    def _key_info(self):
        self.session.openWithCallback(lambda *_: self._update_legend(), MagentaMusikRecordingsScreen)

    def _flash_status(self, msg, ms=2500):
        self["status"].setText(_b(msg))
        if self._flash_timer is not None:
            try:
                self._flash_timer.stop()
            except Exception:
                pass
        t = eTimer()
        t.callback.append(self._render)
        t.start(ms, True)
        self._flash_timer = t

    def _update_download_hint(self):
        global _bg_download_result
        active = _active_downloader is not None and _active_downloader._thread is not None \
            and _active_downloader._thread.is_alive()
        n = len(_download_queue) + (1 if active else 0)
        self["hint_blue"].setText(_b("Downloads (%d)" % n if n else ""))
        if _bg_download_result is not None:
            result = _bg_download_result
            _bg_download_result = None
            if result == "ok":
                self._flash_status("Alle Downloads abgeschlossen")
            elif result == "cancelled":
                self._flash_status("Downloads abgebrochen")
            elif result.startswith("err:"):
                self._flash_status("Download fehlgeschlagen: %s" % result[4:])

    def _key_yellow(self):
        self._list_mode = not self._list_mode
        _set_setting("list_mode", self._list_mode)
        if self._list_mode:
            offset = self._page * TILES_PER_PAGE
            self._list_sel    = min(offset + self._sel, max(0, len(self._items) - 1))
            self._list_scroll = max(0, self._list_sel - LIST_ROWS // 2)
        else:
            self._page = self._list_sel // TILES_PER_PAGE
            self._sel  = self._list_sel % TILES_PER_PAGE
        self._render()

    def _render(self):
        if _Pixmap and not getattr(self, "_header_logo_set", False):
            try:
                logo_px = _cached_pixmap(os.path.join(PLUGIN_DIR, "plugin.png"))
                if logo_px:
                    self["header_logo"].instance.setPixmap(logo_px)
                    self._header_logo_set = True
            except Exception:
                pass
        if self._list_mode:
            if self._prev_render_mode is not True:
                self._clear_all_tiles()
                self._prev_render_mode = True
            self._render_list()
        else:
            if self._prev_render_mode is not False:
                self._clear_all_list()
                self._prev_render_mode = False
            self._render_tiles()
        if self._error:
            status = self._error
        elif not self._items:
            status = "Keine Inhalte verfügbar"
        else:
            status = ""
        self["status"].setText(_b(status))

    def _render_tiles(self):
        total  = len(self._items)
        pages  = max(1, (total + TILES_PER_PAGE - 1) // TILES_PER_PAGE)
        self._page = max(0, min(self._page, pages - 1))

        offset     = self._page * TILES_PER_PAGE
        page_items = self._items[offset:offset + TILES_PER_PAGE]

        for i in range(TILES_PER_PAGE):
            if i < len(page_items):
                item = page_items[i]
                name = _u(item.get("name", ""))
                self["tile_bg_%d"    % i].show()
                self["tile_label_%d" % i].show()
                self["tile_label_%d" % i].setText(_b(name))
                self._load_logo(i, item)
                self._load_type_icon(i, item.get("type", "stream"))
                if item.get("is_live"):
                    self["tile_live_%d" % i].setText(_b("LIVE"))
                    self["tile_live_%d" % i].show()
                else:
                    self["tile_live_%d" % i].hide()
            else:
                self["tile_bg_%d"    % i].hide()
                self["tile_label_%d" % i].hide()
                self["tile_live_%d"  % i].hide()
                self._clear_logo(i)
                self._clear_type_icon(i)

        self._sel = min(self._sel, max(0, len(page_items) - 1))
        self._update_sel_marker()

        self._update_legend()

    def _render_list(self):
        total = len(self._items)
        if total == 0:
            self._list_sel = self._list_scroll = 0
        else:
            self._list_sel = max(0, min(self._list_sel, total - 1))
            if self._list_sel < self._list_scroll:
                self._list_scroll = self._list_sel
            elif self._list_sel >= self._list_scroll + LIST_ROWS:
                self._list_scroll = self._list_sel - LIST_ROWS + 1
            self._list_scroll = max(0, self._list_scroll)

        for i in range(LIST_ROWS):
            abs_idx = self._list_scroll + i
            if abs_idx < total:
                item   = self._items[abs_idx]
                is_sel = (abs_idx == self._list_sel)
                self["list_sel_%d" % i].show() if is_sel else self["list_sel_%d" % i].hide()
                self["list_label_%d" % i].setText(_b(_u(item.get("name", ""))))
                self["list_label_%d" % i].show()
                self._load_list_logo(i, item)
                self._load_list_type_icon(i, item.get("type", "stream"))
                if item.get("is_live"):
                    self["list_live_%d" % i].setText(_b("LIVE"))
                    self["list_live_%d" % i].show()
                else:
                    self["list_live_%d" % i].hide()
            else:
                self["list_sel_%d"   % i].hide()
                self["list_label_%d" % i].hide()
                self["list_live_%d"  % i].hide()
                self._clear_list_logo(i)
                self._clear_list_type_icon(i)

        self._update_legend()

    def _clear_all_tiles(self):
        self._cover_slots.clear()
        for i in range(TILES_PER_PAGE):
            self["tile_bg_%d"    % i].hide()
            self["tile_label_%d" % i].hide()
            self["tile_live_%d"  % i].hide()
            if _Pixmap:
                try:
                    self["tile_logo_%d" % i].instance.setPixmap(None)
                    self["tile_sel_%d"  % i].instance.setPixmap(None)
                    self["tile_type_%d" % i].instance.setPixmap(None)
                except Exception:
                    pass

    def _clear_all_list(self):
        self._cover_slots.clear()
        for i in range(LIST_ROWS):
            self["list_sel_%d"  % i].hide()
            self["list_label_%d" % i].hide()
            self["list_live_%d" % i].hide()
            self._clear_list_logo(i)
            self._clear_list_type_icon(i)

    # ------------------------------------------------------------------
    # Cover-Laden: zuerst nur lokal gecachte Bilder synchron anzeigen (kein
    # Netzwerk, daher nie blockierend). Fehlt ein Cover noch, wird es ueber
    # den Worker-Pool (siehe Modulanfang) im Hintergrund nachgeladen und per
    # _on_cover_fetched() nachtraeglich eingesetzt, sobald fertig.
    # ------------------------------------------------------------------
    def _item_cover_path_local(self, item):
        if not _get_setting("show_covers", True):
            return None
        image_url = item.get("image_url")
        if not image_url:
            return None
        path = _catalog.cover_path_for(image_url)
        if path and os.path.isfile(path):
            return path
        return None

    def _apply_cover_pixmap(self, widget, path, x, y, w, h):
        px = _cached_pixmap(path) if path else None
        if not px:
            return False
        iw, ih = px.size().width(), px.size().height()
        if iw > 0 and ih > 0:
            s  = min(float(w) / iw, float(h) / ih)
            nw = max(1, int(iw * s))
            nh = max(1, int(ih * s))
            ox = (w - nw) // 2
            oy = (h - nh) // 2
            widget.instance.resize(eSize(nw, nh))
            widget.instance.move(ePoint(x + ox, y + oy))
        widget.instance.setPixmap(px)
        return True

    def _request_cover_async(self, item, slot_key):
        if not _get_setting("show_covers", True):
            return
        image_url = item.get("image_url")
        if not image_url:
            self._cover_slots.pop(slot_key, None)
            return
        self._cover_slots[slot_key] = image_url
        if image_url in _cover_fetch_inflight:
            return
        _cover_fetch_inflight.add(image_url)
        _ensure_cover_workers()

        def _on_done(path):
            _cover_fetch_inflight.discard(image_url)
            self._on_cover_fetched(slot_key, image_url, path)

        _cover_queue.put((image_url, _on_done))

    def _on_cover_fetched(self, slot_key, image_url, path):
        if self._closed or not path:
            return
        if self._cover_slots.get(slot_key) != image_url:
            return  # Slot zeigt inzwischen ein anderes/kein Item mehr - verwerfen
        kind, idx = slot_key
        try:
            if kind == "tile":
                lx, ly, lw, lh = _logo_base_rect(idx)
                self._apply_cover_pixmap(self["tile_logo_%d" % idx], path, lx, ly, lw, lh)
            else:
                lox = 40 if IS_FHD else 38
                low = 100 if IS_FHD else 65
                loh = LIST_ROW_H - 10
                loy = LIST_ROW_Y0 + idx * LIST_ROW_H + 5
                self._apply_cover_pixmap(self["list_logo_%d" % idx], path, lox, loy, low, loh)
        except Exception:
            pass

    def _load_list_logo(self, idx, item):
        if not _LoadPixmap or not _Pixmap:
            return
        lox = 40 if IS_FHD else 38
        low = 100 if IS_FHD else 65
        loh = LIST_ROW_H - 10
        loy = LIST_ROW_Y0 + idx * LIST_ROW_H + 5
        widget = self["list_logo_%d" % idx]
        try:
            path = self._item_cover_path_local(item)
            if path and self._apply_cover_pixmap(widget, path, lox, loy, low, loh):
                return
            widget.instance.setPixmap(None)
            widget.instance.resize(eSize(low, loh))
            widget.instance.move(ePoint(lox, loy))
            self._request_cover_async(item, ("list", idx))
        except Exception:
            pass

    def _clear_list_logo(self, idx):
        self._cover_slots.pop(("list", idx), None)
        if not _Pixmap:
            return
        try:
            self["list_logo_%d" % idx].instance.setPixmap(None)
        except Exception:
            pass

    def _load_list_type_icon(self, idx, item_type):
        if not _LoadPixmap or not _Pixmap:
            return
        name = "type_folder.png" if item_type == "folder" else "type_stream.png"
        path = os.path.join(LOGO_DIR, name)
        try:
            px = _cached_pixmap(path)
            if px:
                self["list_type_%d" % idx].instance.setPixmap(px)
                return
            self["list_type_%d" % idx].instance.setPixmap(None)
        except Exception:
            pass

    def _clear_list_type_icon(self, idx):
        if not _Pixmap:
            return
        try:
            self["list_type_%d" % idx].instance.setPixmap(None)
        except Exception:
            pass

    def _update_legend(self):
        if self._list_mode:
            item = self._items[self._list_sel] if 0 <= self._list_sel < len(self._items) else None
        else:
            idx  = self._page * TILES_PER_PAGE + self._sel
            item = self._items[idx] if idx < len(self._items) else None

        self["hint_green"].setText(_b("Einstellungen"))
        if self._list_mode:
            self["hint_yellow"].setText(_b("Kacheln"))
        else:
            self["hint_yellow"].setText(_b("Liste"))
        self["hint_red"].setText(_b("Download") if item and item.get("type") == "stream" else _b(""))
        self["hint_menu"].setText(_b("MENU = Aufnahme") if item and item.get("is_live") else _b(""))
        self["hint_info"].setText(_b("EPG/INFO = Aufnahmen") if _get_active_recordings() or _get_valid_pending_timers() else _b(""))

        if self._list_mode:
            total = len(self._items)
            self["page_label"].setText(_b("%d/%d" % (self._list_sel + 1, total) if total > 0 else ""))
        else:
            pages = max(1, (len(self._items) + TILES_PER_PAGE - 1) // TILES_PER_PAGE)
            self["page_label"].setText(_b("CH+/- Seite %d/%d" % (self._page + 1, pages)) if pages > 1 else _b(""))

    def _load_type_icon(self, idx, item_type):
        if not _LoadPixmap or not _Pixmap:
            return
        name = "type_folder.png" if item_type == "folder" else "type_stream.png"
        path = os.path.join(LOGO_DIR, name)
        px = _cached_pixmap(path)
        if px:
            self["tile_type_%d" % idx].instance.setPixmap(px)

    def _clear_type_icon(self, idx):
        if _Pixmap:
            self["tile_type_%d" % idx].instance.setPixmap(None)

    def _load_logo(self, idx, item):
        if not _LoadPixmap or not _Pixmap:
            return
        lx, ly, lw, lh = _logo_base_rect(idx)
        path = self._item_cover_path_local(item)
        if path and self._apply_cover_pixmap(self["tile_logo_%d" % idx], path, lx, ly, lw, lh):
            return
        self._clear_logo(idx)
        self._request_cover_async(item, ("tile", idx))

    def _clear_logo(self, idx):
        self._cover_slots.pop(("tile", idx), None)
        if _Pixmap:
            lx, ly, lw, lh = _logo_base_rect(idx)
            self["tile_logo_%d" % idx].instance.setPixmap(None)
            self["tile_logo_%d" % idx].instance.resize(eSize(lw, lh))
            self["tile_logo_%d" % idx].instance.move(ePoint(lx, ly))

    def _update_sel_marker(self):
        sel_px = os.path.join(PLUGIN_DIR, "logos", "sel.png")
        for i in range(TILES_PER_PAGE):
            if not _Pixmap:
                break
            if i == self._sel:
                px = _cached_pixmap(sel_px)
                if px:
                    self["tile_sel_%d" % i].instance.setPixmap(px)
            else:
                self["tile_sel_%d" % i].instance.setPixmap(None)

    def _move(self, delta):
        if self._list_mode:
            total = len(self._items)
            if total == 0:
                return
            step = 1 if delta > 0 else -1
            self._list_step(step)
            return

        total      = len(self._items)
        offset     = self._page * TILES_PER_PAGE
        page_count = min(TILES_PER_PAGE, total - offset)
        if page_count <= 0:
            return

        col = self._sel % TILE_COLS
        row = self._sel // TILE_COLS

        if abs(delta) == 1:
            if delta == 1:  # rechts
                if col < TILE_COLS - 1:
                    new_sel = self._sel + 1
                    if new_sel < page_count:
                        self._sel = new_sel
                        self._update_sel_marker()
                        self._update_legend()
                elif _get_setting("wrap_lr", True):
                    new_abs = (offset + self._sel + 1) % total
                    self._page = new_abs // TILES_PER_PAGE
                    self._sel  = new_abs % TILES_PER_PAGE
                    self._render()
                else:
                    self._sel = row * TILE_COLS
                    self._update_sel_marker()
                    self._update_legend()
            else:  # links
                if col > 0:
                    self._sel -= 1
                    self._update_sel_marker()
                    self._update_legend()
                elif _get_setting("wrap_lr", True):
                    new_abs = (offset + self._sel - 1) % total
                    self._page = new_abs // TILES_PER_PAGE
                    self._sel  = new_abs % TILES_PER_PAGE
                    self._render()
                else:
                    self._sel = min(row * TILE_COLS + TILE_COLS - 1, page_count - 1)
                    self._update_sel_marker()
                    self._update_legend()
        else:
            if delta > 0:  # unten
                new_row = (row + 1) % TILE_ROWS
            else:  # oben
                new_row = (row - 1 + TILE_ROWS) % TILE_ROWS
            new_sel = new_row * TILE_COLS + col
            if new_sel >= page_count:
                if new_row * TILE_COLS < page_count:
                    new_sel = page_count - 1
                elif delta < 0:
                    new_sel = ((page_count - col - 1) // TILE_COLS) * TILE_COLS + col if col < page_count else page_count - 1
                else:
                    new_sel = min(col, page_count - 1)
            self._sel = new_sel
            self._update_sel_marker()
            self._update_legend()

    def _list_step(self, step):
        total = len(self._items)
        if total == 0:
            return
        old_sel    = self._list_sel
        old_scroll = self._list_scroll
        self._list_sel = (self._list_sel + step) % total
        if self._list_sel < old_scroll or self._list_sel >= old_scroll + LIST_ROWS:
            # Beim Verlassen der sichtbaren Seite springt der neue Eintrag an den
            # Seitenrand, der in Bewegungsrichtung liegt (Systemlisten-Verhalten:
            # runter -> neuer Eintrag oben, hoch -> neuer Eintrag unten), statt
            # nur zeilenweise mit dem Cursor am Rand kleben zu bleiben.
            if step > 0:
                self._list_scroll = self._list_sel
            else:
                self._list_scroll = self._list_sel - LIST_ROWS + 1
        # Kein oberes Clamping auf total-LIST_ROWS: die letzte Seite darf eine
        # echte, nicht zurueckgezogene Teil-Seite sein (leere Zeilen unterhalb
        # des letzten Eintrags statt Ueberlappung mit der vorherigen Seite).
        self._list_scroll = max(0, self._list_scroll)
        if self._list_scroll != old_scroll:
            self._render_list()
        else:
            old_row = old_sel - old_scroll
            new_row = self._list_sel - self._list_scroll
            if 0 <= old_row < LIST_ROWS:
                self["list_sel_%d" % old_row].hide()
            if 0 <= new_row < LIST_ROWS:
                self["list_sel_%d" % new_row].show()
            self._update_legend()

    def _key_up(self):
        self._move(-1 if self._list_mode else -TILE_COLS)

    def _key_up_repeat(self):
        if self._list_mode:
            self._move(-1)

    def _key_down(self):
        self._move(1 if self._list_mode else TILE_COLS)

    def _key_down_repeat(self):
        if self._list_mode:
            self._move(1)

    def _key_left(self):
        if self._list_mode:
            self._page_nav(-1)
        else:
            self._move(-1)

    def _key_right(self):
        if self._list_mode:
            self._page_nav(1)
        else:
            self._move(1)

    def _page_nav(self, direction):
        if self._list_mode:
            total = len(self._items)
            if total == 0:
                return
            self._list_sel = max(0, min(self._list_sel + direction * LIST_ROWS, total - 1))
            self._render_list()
            return
        total = len(self._items)
        pages = max(1, (total + TILES_PER_PAGE - 1) // TILES_PER_PAGE)
        new_page = self._page + direction
        if 0 <= new_page < pages:
            self._page = new_page
            self._sel  = 0
            self._render()

    def _ok(self):
        if self._list_mode:
            total = len(self._items)
            if total == 0 or self._list_sel >= total:
                return
            self._on_select_item(self._items[self._list_sel], self._list_sel)
            return

        offset = self._page * TILES_PER_PAGE
        idx    = offset + self._sel
        if idx >= len(self._items):
            return
        self._on_select_item(self._items[idx], idx)


# ------------------------------------------------------------------
# Ebene 1: Festival-Liste (+ "Jetzt live"-Eintrag)
# ------------------------------------------------------------------
class MagentaMusikFestivalScreen(_BrowseScreenBase):

    def __init__(self, session):
        _BrowseScreenBase.__init__(self, session, "MagentaMusik")

    def _fetch_items(self):
        items = []
        items.append({
            "type":      "live",
            "name":      u"ZDF live (Test)",
            "url":       "https://zdf-hls-15.akamaized.net/hls/live/2016498/de/high/master.m3u8",
            "is_live":   True,
            "image_url": None,
        })
        for live in _catalog.get_live_stages():
            items.append({
                "type":      "live",
                "name":      live["headline"],
                "url":       live["url"],
                "is_live":   True,
                "image_url": None,
            })
        for f in _catalog.get_festivals():
            items.append({
                "type":      "folder",
                "name":      f["name"],
                "slug":      f["slug"],
                "image_url": f.get("image_url"),
            })
        return items

    def _on_select_item(self, item, idx):
        if item.get("type") == "folder":
            self.session.open(MagentaMusikItemsScreen, item["slug"], item["name"])
            return
        # "live"-Eintrag: direkt abspielen
        self._play(item)

    def _play(self, item):
        raw_url = item["url"]
        self["status"].setText(_b("Lade…"))
        t = threading.Thread(target=self.__play_bg, args=(item, raw_url))
        t.daemon = True
        t.start()

    def __play_bg(self, item, raw_url):
        from magentamusik import is_magentamusik as _is_mm, resolve as _resolve
        # Zusaetzliche Buehnen (siehe get_live_stages) liefern bereits eine
        # fertige .m3u8-Stream-URL direkt vom CDN, keine magentamusik.de-Seite
        # zum Aufloesen - resolve() nur fuer echte magentamusik.de-URLs nutzen.
        # _resolve() macht bis zu 3 sequenzielle HTTP-Requests - im
        # Hintergrundthread, sonst blockiert das den GUI-Thread (Lade-Spinner).
        try:
            url = _resolve(raw_url) if _is_mm(raw_url) else raw_url
        except Exception:
            url = None

        # resolve_local_playlist() macht ebenfalls eine blockierende HTTP-
        # Anfrage (HLS-Audio-Fix) - muss genau wie resolve() im
        # Hintergrundthread laufen, sonst friert beim naechsten Netzwerk-
        # Haenger der komplette Player (inkl. WebIF) ein.
        url_str = user_agent = None
        if url:
            url_str, user_agent = resolve_local_playlist(url, hls_audio_fix=True)

        def _apply():
            if self._closed:
                return
            if not url_str:
                self._render()
                return
            play_resolved_stream(
                self.session, url_str, title=item.get("name", "Live"), is_live=True,
                user_agent=user_agent,
                autoconfigure_serviceapp=_get_setting("serviceapp_autoconfigure", True),
            )
            self._render()

        try:
            from twisted.internet import reactor
            reactor.callFromThread(_apply)
        except Exception:
            _apply()


# ------------------------------------------------------------------
# Ebene 2: Festival-Items (VOD/Live-Konzerte eines Festivals)
# ------------------------------------------------------------------
class MagentaMusikItemsScreen(_BrowseScreenBase):

    def __init__(self, session, slug, name):
        self._slug          = slug
        self._festival_name = name
        _BrowseScreenBase.__init__(self, session, name)

    def _fetch_items(self):
        out = []
        for it in _catalog.get_festival_items(self._slug):
            out.append({
                "type":      "stream",
                "name":      it["headline"],
                "url":       it["url"],
                "image_url": it.get("image_url"),
                "is_live":   it.get("is_live", False),
            })
        return out

    def _on_select_item(self, item, idx):
        self["status"].setText(_b("Lade…"))
        t = threading.Thread(target=self.__play_bg, args=(item, idx))
        t.daemon = True
        t.start()

    def __play_bg(self, item, idx):
        from magentamusik import resolve as _resolve
        # resolve() macht bis zu 3 sequenzielle HTTP-Requests gegen
        # magentamusik.de - im Hintergrundthread, sonst blockiert das den
        # GUI-Thread beim Start jedes Konzerts (Lade-Spinner).
        try:
            url = _resolve(item["url"])
        except Exception:
            url = None

        # VOD-Playlists von magentamusik.de muxen Audio zwar direkt in jede
        # Bitrate-Variante (kein separater #EXT-X-MEDIA AUDIO-Track noetig),
        # aber resolve_local_playlist() waehlt zusaetzlich die beste Variante
        # vorab aus - ohne das muss exteplayer3 selbst per ABR aushandeln,
        # was den Start um mehrere Sekunden verzoegert. Laeuft bereits im
        # Hintergrundthread (s.o.), GUI-Freeze-Risiko besteht nicht mehr.
        url_str = user_agent = None
        if url:
            url_str, user_agent = resolve_local_playlist(url, hls_audio_fix=True)

        def _apply():
            if self._closed:
                return
            if not url_str:
                self._render()
                return
            play_resolved_stream(
                self.session, url_str, title=item.get("name", "Stream"), is_live=True,
                user_agent=user_agent,
                autoconfigure_serviceapp=_get_setting("serviceapp_autoconfigure", True),
                streams=self._items, stream_index=idx,
            )
            self._render()

        try:
            from twisted.internet import reactor
            reactor.callFromThread(_apply)
        except Exception:
            _apply()

    def _start_download(self, item):
        state = _enqueue_download(
            item.get("name", "Download"), item["url"], self._festival_name, item.get("image_url"))
        if state == "queued":
            self._flash_status("Zur Warteschlange hinzugefügt")
        else:
            self._flash_status("Download gestartet")


# ------------------------------------------------------------------
# Enigma2-Plugin-Registrierung
# ------------------------------------------------------------------
def main(session, **kwargs):
    session.open(MagentaMusikFestivalScreen)


def autostart(reason, **kwargs):
    if reason != 0:
        return
    # Timer-Scheduler laeuft unabhaengig davon, ob die Plugin-GUI gerade
    # offen ist - geplante Aufnahmen sollen auch dann feuern, wenn niemand
    # im Menue ist.
    try:
        _start_scheduler()
    except Exception:
        pass


def Plugins(**kwargs):
    return [
        PluginDescriptor(
            name        = b"MagentaMusik",
            description = _b("Festivals & Konzerte von magentamusik.de"),
            where       = PluginDescriptor.WHERE_PLUGINMENU,
            icon        = b"plugin.png",
            fnc         = main,
        ),
        PluginDescriptor(
            name  = b"MagentaMusik",
            where = PluginDescriptor.WHERE_AUTOSTART,
            fnc   = autostart,
        ),
    ]
