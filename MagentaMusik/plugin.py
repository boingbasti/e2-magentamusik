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
from player import play_stream
from downloader import Downloader, convert_mp4_to_ts
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


def _get_settings_list():
    return [
        ("show_covers",              "Vorschaubilder laden",          "toggle"),
        ("wrap_lr",                  "Seite wechseln mit Links/Rechts", "toggle"),
        ("prefer_best_quality",      "Höchste Qualität bevorzugen",   "toggle"),
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
    "prefer_best_quality":      True,
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
            '<widget name="hint_ok"     position="588,{ly}"  size="258,{lh}" zPosition="4" transparent="1" backgroundColor="#1A000000" font="Regular;{fs}" halign="left"  valign="center" foregroundColor="#CCCCCC"/>'
            '<eLabel backgroundColor="#1ACCAA00" position="870,{py}" size="{pw},{ph}" zPosition="2" transparent="0"/>'
            '<widget name="hint_yellow" position="888,{ly}"  size="200,{lh}" zPosition="4" transparent="1" backgroundColor="#1A000000" font="Regular;{fs}" halign="left"  valign="center" foregroundColor="#CCCCCC"/>'
            '<eLabel backgroundColor="#1A0066CC" position="1110,{py}" size="{pw},{ph}" zPosition="2" transparent="0"/>'
            '<widget name="hint_blue"   position="1128,{ly}" size="280,{lh}" zPosition="4" transparent="1" backgroundColor="#1A000000" font="Regular;{fs}" halign="left"  valign="center" foregroundColor="#CCCCCC"/>'
            '<widget name="hint_ch"     position="1430,{ly}" size="320,{lh}" zPosition="4" transparent="1" backgroundColor="#1A000000" font="Regular;{fs}" halign="left"  valign="center" foregroundColor="#CCCCCC" noWrap="1"/>'
            '<widget name="page_label"  position="1770,{ly}" size="100,{lh}" zPosition="4" transparent="1" backgroundColor="#1A000000" font="Regular;28" halign="right" valign="center" foregroundColor="#AAAAAA"/>'
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
            '<widget name="hint_ok"     position="370,{ly}"  size="172,{lh}" zPosition="4" transparent="1" backgroundColor="#1A000000" font="Regular;{fs}" halign="left"  valign="center" foregroundColor="#CCCCCC"/>'
            '<eLabel backgroundColor="#1ACCAA00" position="560,{py}" size="{pw},{ph}" zPosition="2" transparent="0"/>'
            '<widget name="hint_yellow" position="568,{ly}"  size="130,{lh}" zPosition="4" transparent="1" backgroundColor="#1A000000" font="Regular;{fs}" halign="left"  valign="center" foregroundColor="#CCCCCC"/>'
            '<eLabel backgroundColor="#1A0066CC" position="715,{py}" size="{pw},{ph}" zPosition="2" transparent="0"/>'
            '<widget name="hint_blue"   position="723,{ly}"  size="180,{lh}" zPosition="4" transparent="1" backgroundColor="#1A000000" font="Regular;{fs}" halign="left"  valign="center" foregroundColor="#CCCCCC"/>'
            '<widget name="hint_ch"     position="920,{ly}"  size="220,{lh}" zPosition="4" transparent="1" backgroundColor="#1A000000" font="Regular;{fs}" halign="left"  valign="center" foregroundColor="#CCCCCC" noWrap="1"/>'
            '<widget name="page_label"  position="1158,{ly}" size="62,{lh}"  zPosition="4" transparent="1" backgroundColor="#1A000000" font="Regular;{fs}" halign="right" valign="center" foregroundColor="#AAAAAA"/>'
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
        if self._pending.get("debug_log", False):
            try:
                open(_MM_DEBUG_FLAG, "w").close()
            except Exception:
                pass
        else:
            try:
                os.remove(_MM_DEBUG_FLAG)
            except Exception:
                pass
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
        _queue_next()


def _fire_download_notification():
    try:
        from Tools.Notifications import AddPopup
        AddPopup("Alle Downloads abgeschlossen", _MessageBox.TYPE_INFO, timeout=5, id="magentamusik_dl_done")
    except Exception:
        pass


def _notify_downloads_done():
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
# Gemeinsame Kachel-/Listen-Engine fuer Festival- und Item-Screen
# ------------------------------------------------------------------
class _BrowseScreenBase(Screen):

    skin = _build_skin()

    def __init__(self, session, title):
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
        self["hint_ok"]     = Label(_b(""))
        self["hint_ch"]     = Label(_b("CH+/- = Seite"))
        self["hint_yellow"] = Label(_b(""))
        self["hint_green"]  = Label(_b("Einstellungen"))
        self["hint_red"]    = Label(_b(""))
        self["hint_blue"]   = Label(_b("Downloads"))
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
             "ChannelSelectBaseActions"],
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
        self._closed = True

    # --- von Subklassen zu ueberschreiben ---
    def _fetch_items(self):
        return []

    def _on_select_item(self, item, idx):
        pass

    # --- generisches Laden ---
    def _load(self):
        self._items = self._fetch_items()
        self._error = _catalog.last_fetch_error()
        self._page  = 0
        self._sel   = 0
        self._render()

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
        self.session.open(
            MagentaMusikDownloadManagerScreen,
            lambda: _active_downloader, lambda: _download_queue,
            _cancel_all_downloads, _cancel_current_download,
        )

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
        self["hint_blue"].setText(_b("Downloads (%d)" % n if n else "Downloads"))
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

        page_label = "Seite %d/%d" % (self._page + 1, pages) if pages > 1 else ""
        self["page_label"].setText(_b(page_label))
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
            self._list_scroll = max(0, min(self._list_scroll, max(0, total - LIST_ROWS)))

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

        count_str = "%d/%d" % (self._list_sel + 1, total) if total > 0 else ""
        self["page_label"].setText(_b(count_str))
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
        if item and item.get("type") == "folder":
            self["hint_ok"].setText(_b("OK = Öffnen"))
        else:
            self["hint_ok"].setText(_b("OK = Abspielen"))
        self["hint_red"].setText(_b("Download") if item and item.get("type") == "stream" else _b(""))

        if self._list_mode:
            total = len(self._items)
            self["page_label"].setText(_b("%d/%d" % (self._list_sel + 1, total) if total > 0 else ""))
            self["hint_ch"].setText(_b(""))
        else:
            self["hint_ch"].setText(_b("CH+/- = Seite blättern"))

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
        if self._list_sel < self._list_scroll:
            self._list_scroll = self._list_sel
        elif self._list_sel >= self._list_scroll + LIST_ROWS:
            self._list_scroll = self._list_sel - LIST_ROWS + 1
        self._list_scroll = max(0, min(self._list_scroll, max(0, total - LIST_ROWS)))
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
        from magentamusik import is_magentamusik as _is_mm, resolve as _resolve
        raw_url = item["url"]
        # Zusaetzliche Buehnen (siehe get_live_stages) liefern bereits eine
        # fertige .m3u8-Stream-URL direkt vom CDN, keine magentamusik.de-Seite
        # zum Aufloesen - resolve() nur fuer echte magentamusik.de-URLs nutzen.
        url = _resolve(raw_url) if _is_mm(raw_url) else raw_url
        if not url:
            return
        play_stream(
            self.session, url, title=item.get("name", "Live"), is_live=True,
            autoconfigure_serviceapp=_get_setting("serviceapp_autoconfigure", True),
            prefer_best_quality=_get_setting("prefer_best_quality", True),
            hls_audio_fix=True,
        )


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
            })
        return out

    def _on_select_item(self, item, idx):
        from magentamusik import resolve as _resolve
        url = _resolve(item["url"])
        if not url:
            return
        play_stream(
            self.session, url, title=item.get("name", "Stream"), is_live=True,
            autoconfigure_serviceapp=_get_setting("serviceapp_autoconfigure", True),
            prefer_best_quality=_get_setting("prefer_best_quality", True),
            streams=self._items, stream_index=idx,
            hls_audio_fix=True,
        )

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


def Plugins(**kwargs):
    return [
        PluginDescriptor(
            name        = b"MagentaMusik",
            description = _b("Festivals & Konzerte von magentamusik.de"),
            where       = PluginDescriptor.WHERE_PLUGINMENU,
            icon        = b"plugin.png",
            fnc         = main,
        ),
    ]
