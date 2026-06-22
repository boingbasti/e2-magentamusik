# -*- coding: utf-8 -*-
import os


def _dbg(msg):
    if not os.path.exists("/tmp/mm_debug"):
        return
    try:
        import time
        with open("/tmp/magentamusik.log", "a") as f:
            f.write("[%.3f] [player] %s\n" % (time.time(), msg))
    except Exception:
        pass

from enigma import eServiceReference

try:
    from Screens.MoviePlayer import MoviePlayer
except ImportError:
    from Screens.InfoBar import MoviePlayer

_OFFLINE_VIDEO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "offline_stream.mp4")


def _offline_ref():
    return eServiceReference(4097, 0, _OFFLINE_VIDEO)


class MMStreamPlayer(MoviePlayer):
    ENABLE_RESUME_SUPPORT = False

    def __init__(self, session, service, streams=None, stream_index=0,
                 autoconfigure_serviceapp=True):
        MoviePlayer.__init__(self, session, service)
        self.skinName = ["MoviePlayer", "InfoBar"]
        self._streams             = streams or []
        self._stream_index        = stream_index
        self._autoconfigure       = autoconfigure_serviceapp
        self._closed               = False
        self._switching            = False
        self.onClose.append(self.__mark_closed)
        if len(self._streams) > 1:
            from Components.ActionMap import ActionMap
            self["_mm_nav"] = ActionMap(
                [b"ChannelSelectBaseActions"],
                {
                    b"nextBouquet": lambda: self._switch_stream(1),
                    b"prevBouquet": lambda: self._switch_stream(-1),
                },
                -1,
            )

    def __mark_closed(self):
        self._closed = True

    def _switch_stream(self, direction):
        _dbg("_switch_stream called direction=%d" % direction)
        if self._switching:
            return
        new_idx = self._stream_index + direction
        if new_idx < 0 or new_idx >= len(self._streams):
            return
        item = self._streams[new_idx]
        url  = item.get("url", "")
        name = item.get("headline", item.get("name", "Stream"))
        if not url:
            return
        # resolve() und der HLS-Audio-Fix (_build_local_playlist) machen
        # blockierende HTTP-Anfragen (DNS-Aufloesung wird vom timeout nicht
        # zuverlaessig abgedeckt) - deshalb im Hintergrundthread, sonst
        # friert bei einem Netzwerk-Haenger der komplette Player (und damit
        # auch das WebIF, da beide den GIL teilen) ein.
        import threading
        self._switching = True
        t = threading.Thread(target=self.__switch_bg, args=(new_idx, url, name))
        t.daemon = True
        t.start()

    def __switch_bg(self, new_idx, url, name):
        try:
            import magentamusik as _mm
            if _mm.is_magentamusik(url):
                resolved = _mm.resolve(url)
                if resolved:
                    url = resolved
        except Exception:
            pass
        # resolve_local_playlist() waehlt die beste Bitrate-Variante vorab
        # aus - ohne das muss exteplayer3 selbst per ABR aushandeln, was den
        # Start um mehrere Sekunden verzoegert. Laeuft im Hintergrundthread,
        # GUI-Freeze-Risiko besteht nicht mehr.
        url_str, user_agent = resolve_local_playlist(url, "", True)

        def _apply():
            self._switching = False
            if self._closed:
                return
            ref = _build_ref(url_str, name, "", user_agent, self._autoconfigure)
            self._stream_index    = new_idx
            self._showing_offline = False
            self.session.nav.playService(ref)

        try:
            from twisted.internet import reactor
            reactor.callFromThread(_apply)
        except Exception:
            _apply()

    def leavePlayer(self):
        self.close()

    def doEofInternal(self, playing):
        _dbg("doEofInternal called playing=%s streams=%d showing_offline=%s" % (
            playing, len(self._streams), getattr(self, "_showing_offline", False)))
        if len(self._streams) > 1:
            self._showing_offline = True
            self.session.nav.playService(_offline_ref())
            return
        self.close()


def _has_serviceapp():
    return os.path.exists("/usr/lib/enigma2/python/Plugins/SystemPlugins/ServiceApp")


def _has_new_exteplayer3():
    # exteplayer3 >= v181 (feedplus/manuell) bringt eigene Libs in /usr/lib/exteplayer3_deps/
    return os.path.isdir("/usr/lib/exteplayer3_deps")


def _build_local_playlist(master_url, user_agent=""):
    _dbg("_build_local_playlist url=%s" % master_url)
    if not master_url.lower().split("?")[0].endswith(".m3u8"):
        _dbg("not m3u8, skip")
        return None
    try:
        try:
            from urllib2 import urlopen, Request as _Req
            from urlparse import urljoin as _urljoin
        except ImportError:
            from urllib.request import urlopen, Request as _Req
            from urllib.parse import urljoin as _urljoin
        import re
        import threading
        try:
            from BaseHTTPServer import HTTPServer, BaseHTTPRequestHandler
        except ImportError:
            from http.server import HTTPServer, BaseHTTPRequestHandler

        headers = {"User-Agent": user_agent or "Mozilla/5.0"}
        req = _Req(master_url, headers=headers)
        resp = urlopen(req, timeout=8)
        effective_url = resp.geturl()
        content = resp.read().decode("utf-8", "replace")
        lines = content.splitlines()

        if "#EXT-X-STREAM-INF" not in content:
            return None

        best_bw, best_inf, best_url = -1, None, None
        i = 0
        while i < len(lines):
            line = lines[i]
            if line.startswith("#EXT-X-STREAM-INF"):
                m = re.search(r"BANDWIDTH=(\d+)", line)
                bw = int(m.group(1)) if m else 0
                for j in range(i + 1, len(lines)):
                    v = lines[j].strip()
                    if v and not v.startswith("#"):
                        if bw > best_bw:
                            best_bw = bw
                            best_inf = line
                            best_url = _urljoin(effective_url, v)
                        break
            i += 1

        if not best_url:
            return None

        audio_group_m = re.search(r'AUDIO="([^"]+)"', best_inf or "")
        audio_group = audio_group_m.group(1) if audio_group_m else None

        out = ["#EXTM3U", "#EXT-X-VERSION:4", "#EXT-X-INDEPENDENT-SEGMENTS", ""]
        for line in lines:
            if line.startswith("#EXT-X-MEDIA"):
                if "TYPE=AUDIO" not in line:
                    continue
                if audio_group and ('GROUP-ID="%s"' % audio_group) not in line:
                    continue
                if "DEFAULT=YES" not in line:
                    continue
                line = re.sub(
                    r'URI="([^"]+)"',
                    lambda m: 'URI="' + _urljoin(effective_url, m.group(1)) + '"',
                    line
                )
                out.append(line)
        out.extend(["", best_inf, best_url, ""])
        data = "\n".join(out).encode("utf-8")

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                self.end_headers()
                self.wfile.write(data)
            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), _Handler)
        port = server.server_address[1]
        t = threading.Thread(target=lambda: (server.handle_request(), server.server_close()))
        t.daemon = True
        t.start()
        return "http://127.0.0.1:%d/live.m3u8" % port
    except Exception:
        return None


def _configure_serviceapp_for_live():
    try:
        from Components.config import config
        from Plugins.SystemPlugins.ServiceApp.serviceapp_client import (
            setExtEplayer3Settings, setServiceAppSettings, OPTIONS_SERVICEEXTEPLAYER3
        )
        key  = "serviceexteplayer3"
        opts = config.plugins.serviceapp.options[key]
        ext3 = config.plugins.serviceapp.exteplayer3[key]

        if not ext3.downmix.value:
            ext3.downmix.value = True; ext3.downmix.save()

        if _has_new_exteplayer3():
            # v181+: exteplayer3 parst Master-Playlist selbst -> HLS-Explorer deaktivieren
            if opts.hls_explorer.value:
                opts.hls_explorer.value = False; opts.hls_explorer.save()
            if not opts.autoselect_stream.value:
                opts.autoselect_stream.value = True; opts.autoselect_stream.save()
        else:
            # Alte exteplayer3 (Feed): HLS-Explorer an, autoselect an, AAC SW-Decode an
            if not opts.hls_explorer.value:
                opts.hls_explorer.value = True; opts.hls_explorer.save()
            if not opts.autoselect_stream.value:
                opts.autoselect_stream.value = True; opts.autoselect_stream.save()
            if not ext3.aac_swdecoding.value:
                ext3.aac_swdecoding.value = True; ext3.aac_swdecoding.save()

        # v181 erwartet '-a 0|1|2|3', altes serviceapp.so generiert '-a' ohne Wert -> haengt
        aac_sw = False if _has_new_exteplayer3() else ext3.aac_swdecoding.value
        setExtEplayer3Settings(
            OPTIONS_SERVICEEXTEPLAYER3,
            aac_sw,
            ext3.dts_swdecoding.value,
            ext3.wma_swdecoding.value,
            ext3.lpcm_injecion.value,
            ext3.downmix.value
        )
        setServiceAppSettings(
            OPTIONS_SERVICEEXTEPLAYER3,
            opts.hls_explorer.value,
            opts.autoselect_stream.value,
            opts.connection_speed_kb.value,
            opts.autoturnon_subtitles.value
        )
    except Exception:
        pass


def resolve_local_playlist(stream_url, user_agent="", hls_audio_fix=True):
    # Netzwerkteil des HLS-Audio-Fixes (_build_local_playlist macht eine
    # blockierende HTTP-Anfrage, deren DNS-Aufloesung vom timeout=8 nicht
    # zuverlaessig abgedeckt wird). MUSS im Hintergrundthread aufgerufen
    # werden - nie im GUI-/Reactor-Thread, sonst friert bei einem
    # Netzwerk-Haenger der komplette Player (inkl. WebIF, gleicher GIL) ein.
    url_str = stream_url.decode("utf-8", "replace") if isinstance(stream_url, bytes) else stream_url
    if hls_audio_fix:
        local_url = _build_local_playlist(url_str, user_agent)
        if local_url:
            return local_url, ""
    return url_str, user_agent


def _build_ref(url, title, player, user_agent, autoconfigure_serviceapp=True, is_live=True):
    # Netzwerkfrei - der Aufrufer muss hls_audio_fix bereits per
    # resolve_local_playlist() im Hintergrundthread erledigt haben.
    url_str = url.decode("utf-8", "replace") if isinstance(url, bytes) else url
    if user_agent:
        sep = "&" if "|" in url_str else "|"
        url_str = url_str + sep + "User-Agent=" + user_agent
    url_bytes   = url_str.encode("utf-8") if not isinstance(url_str, bytes) else url_str
    title_bytes = title.encode("utf-8")   if not isinstance(title, bytes)   else title
    if player == "exteplayer3":
        if autoconfigure_serviceapp and _has_serviceapp():
            _configure_serviceapp_for_live()
        player_id = 5002
    elif player == "gstplayer":
        player_id = 5001
    elif player == "default":
        player_id = 4097
    else:
        if is_live and _has_serviceapp():
            if autoconfigure_serviceapp:
                _configure_serviceapp_for_live()
            player_id = 5002
        else:
            player_id = 4097
    _dbg("_build_ref player_id=%d url=%s" % (player_id, url_str))
    ref = eServiceReference(player_id, 0, url_bytes)
    ref.setName(title_bytes)
    return ref


def play_resolved_stream(session, stream_url, title="Stream", is_live=True, player="", user_agent="",
                         autoconfigure_serviceapp=True,
                         streams=None, stream_index=0):
    # GUI-Thread-sicher: erwartet, dass resolve_local_playlist() (Netzwerk-
    # zugriff) bereits vorher im Hintergrundthread gelaufen ist.
    ref = _build_ref(stream_url, title, player, user_agent,
                     autoconfigure_serviceapp, is_live)
    session.open(MMStreamPlayer, ref,
                 streams=streams or [],
                 stream_index=stream_index,
                 autoconfigure_serviceapp=autoconfigure_serviceapp)
