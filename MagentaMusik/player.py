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


# ------------------------------------------------------------------
# Live-Aufnahme (Hintergrund-HLS-Downloader, kein natives Enigma2-Recording)
# Portiert aus StreamAnything/player.py (dortige HLSRecorder-Klasse, dort
# auf der Box vollstaendig verifiziert inkl. Deep-Standby-Wecktimer) - siehe
# Memory project_live_recording_feature im StreamAnything-Projekt.
# ------------------------------------------------------------------
def _sanitize_filename(text):
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    allowed = set(u"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 -_"
                 u"\xe4\xf6\xfc\xc4\xd6\xdc\xdf")
    return u"".join(c for c in text if c in allowed).strip()


def _make_recording_filepath(title, save_dir):
    import time as _time
    safe_title = _sanitize_filename(title) or "Aufnahme"
    if isinstance(save_dir, bytes):
        save_dir = save_dir.decode("utf-8", "replace")
    filename = u"%s_%s.ts" % (safe_title[:80], _time.strftime("%Y%m%d_%H%M%S"))
    return os.path.join(save_dir, filename).encode("utf-8")


def format_duration(seconds):
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h:
        return "%d:%02d:%02d" % (h, m, s)
    return "%d:%02d" % (m, s)


def _resolve_recording_targets(master_url, user_agent=""):
    # Wie der Bitrate-Teil von _build_local_playlist (waehlt die Variante mit
    # der hoechsten BANDWIDTH), liefert aber zusaetzlich die separate Audio-
    # Media-Playlist-URL mit, falls die gewaehlte Variante ihr Audio nicht
    # selbst enthaelt, sondern per eigenem #EXT-X-MEDIA-Track referenziert
    # (z.B. MagentaMusik-Live-Buehnen). Rueckgabe:
    # (video_media_playlist_url, audio_media_playlist_url_or_None)
    if not master_url.lower().split("?")[0].endswith(".m3u8"):
        return master_url, None
    try:
        try:
            from urllib2 import urlopen, Request
        except ImportError:
            from urllib.request import urlopen, Request
        try:
            from urlparse import urljoin
        except ImportError:
            from urllib.parse import urljoin
        import re
        headers = {"User-Agent": user_agent or "Mozilla/5.0"}
        resp = urlopen(Request(master_url, headers=headers), timeout=10)
        effective_url = resp.geturl()
        content = resp.read().decode("utf-8", "replace")
        if "#EXT-X-STREAM-INF" not in content:
            return master_url, None

        lines = content.splitlines()
        best_bw, best_inf, best_url = -1, None, None
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF"):
                m = re.search(r"BANDWIDTH=(\d+)", line)
                bw = int(m.group(1)) if m else 0
                if bw > best_bw and i + 1 < len(lines):
                    candidate = lines[i + 1].strip()
                    if candidate and not candidate.startswith("#"):
                        best_bw, best_inf = bw, line
                        best_url = urljoin(effective_url, candidate)
        if not best_url:
            return master_url, None

        audio_url = None
        audio_group_m = re.search(r'AUDIO="([^"]+)"', best_inf or "")
        if audio_group_m:
            audio_group = audio_group_m.group(1)
            for line in lines:
                if (line.startswith("#EXT-X-MEDIA") and "TYPE=AUDIO" in line and
                       ('GROUP-ID="%s"' % audio_group) in line and "DEFAULT=YES" in line):
                    uri_m = re.search(r'URI="([^"]+)"', line)
                    if uri_m:
                        audio_url = urljoin(effective_url, uri_m.group(1))
                    break
        return best_url, audio_url
    except Exception:
        return master_url, None


class HLSRecorder(object):
    """Nimmt einen Live-HLS-Stream segmentweise auf: Media-Playlist alle paar
    Sekunden neu laden, neue Segmente per HTTP GET roh ans Ende der
    Ausgabedatei anhaengen - kein ffmpeg/Remuxing noetig, solange die Quelle
    Video+Audio in einem Segment liefert. Gleicher Grundmechanismus wie
    streamlink (stream/hls.py), bewusst vereinfacht/sequenziell statt
    Thread-Pool, weil Live-Pacing (ein Segment alle paar Sekunden) das nicht
    braucht.

    Hat KEIN bekanntes Ende wie ein VOD-Download - laeuft bis duration
    erreicht ist oder cancel() aufgerufen wird. Falls die Quelle Audio als
    separaten #EXT-X-MEDIA-Track ausliefert (z.B. MagentaMusik-Live), werden
    Video und Audio in zwei Temp-Dateien aufgenommen und am Ende per ffmpeg
    (Stream-Copy, kein Reencoding) zu einer Datei gemuxt - genau wie beim
    VOD-Downloader (downloader.py). Schlaegt das Muxen fehl, bleiben die
    beiden Teildateien erhalten statt Daten zu verlieren.
    """

    def __init__(self, url, title, save_dir, user_agent="", duration=None,
                 on_progress=None, on_done=None, on_error=None):
        import uuid
        self.rec_id      = str(uuid.uuid4())
        self.title       = title
        self.duration    = duration  # Sekunden, None = nur manueller Stop
        self.on_progress = on_progress
        self.on_done     = on_done
        self.on_error    = on_error

        self._url         = url
        self._user_agent  = user_agent
        self._cancelled   = False
        self._thread      = None
        self._downloaded  = 0
        self._segs_done   = 0
        self._started_at  = None
        self.filepath      = _make_recording_filepath(title, save_dir)

    def start(self):
        import threading
        self._thread = threading.Thread(target=self._run)
        self._thread.daemon = True
        self._thread.start()

    def cancel(self):
        self._cancelled = True

    def elapsed(self):
        if not self._started_at:
            return 0
        import time
        return time.time() - self._started_at

    def _wait(self, seconds):
        # In 0.5s-Schritten warten statt einmal lange zu schlafen, damit
        # cancel() zuegig reagiert statt bis zu target_duration zu blockieren.
        import time
        steps = int(seconds / 0.5) or 1
        for _ in range(steps):
            if self._cancelled:
                return
            time.sleep(0.5)

    def _mux(self, video_path, audio_path, out_path):
        import subprocess
        try:
            ffmpeg_bin = "/usr/bin/ffmpeg" if os.path.exists("/usr/bin/ffmpeg") else "ffmpeg"
            cmd = [ffmpeg_bin, "-y", "-i", video_path, "-i", audio_path,
                  "-c", "copy", "-map", "0:v:0", "-map", "1:a:0", out_path]
            devnull = open(os.devnull, "wb")
            rc = subprocess.call(cmd, stdout=devnull, stderr=devnull)
            devnull.close()
            if rc == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                for p in (video_path, audio_path):
                    try:
                        os.remove(p)
                    except Exception:
                        pass
                return True
        except Exception as e:
            _dbg("HLSRecorder Muxing fehlgeschlagen: %s" % e)
        return False

    def _run(self):
        f_video = None
        f_audio = None
        try:
            import re
            import time
            try:
                from urllib2 import urlopen, Request
            except ImportError:
                from urllib.request import urlopen, Request
            try:
                from urlparse import urljoin
            except ImportError:
                from urllib.parse import urljoin

            if not self._url.lower().split("?")[0].endswith(".m3u8"):
                raise Exception("Aufnahme wird aktuell nur fuer HLS (.m3u8) Streams unterstuetzt")

            self._started_at = time.time()
            headers = {"User-Agent": self._user_agent or "Mozilla/5.0"}

            video_url, audio_url = _resolve_recording_targets(self._url, self._user_agent)

            video_path = self.filepath
            audio_path = None
            if audio_url:
                base = self.filepath[:-3] if self.filepath.endswith(b".ts") else self.filepath
                video_path = base + b".video.ts"
                audio_path = base + b".audio.ts"

            f_video = open(video_path, "wb")
            f_audio = open(audio_path, "wb") if audio_path else None

            video_state = {"seq": -1, "errors": 0}
            audio_state = {"seq": -1, "errors": 0}

            def fetch_segments(url, f, state):
                # Laedt eine Media-Playlist neu, haengt neue Segmente roh an
                # f an. Rueckgabe: (reload_pause_sekunden, ist_zu_ende).
                try:
                    resp = urlopen(Request(url, headers=headers), timeout=10)
                    effective_url = resp.geturl()
                    text = resp.read().decode("utf-8", "replace")
                    state["errors"] = 0
                except Exception:
                    state["errors"] += 1
                    if state["errors"] >= 5:
                        raise Exception("Playlist wiederholt nicht erreichbar")
                    return 5, False

                if re.search(r"#EXT-X-KEY:(?!METHOD=NONE)", text):
                    raise Exception("Verschluesselte Segmente werden nicht unterstuetzt")

                target_duration = 6
                m = re.search(r"#EXT-X-TARGETDURATION:(\d+)", text)
                if m:
                    target_duration = max(1, int(m.group(1)))

                media_sequence = 0
                m = re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", text)
                if m:
                    media_sequence = int(m.group(1))

                segments = []
                seq_num = media_sequence
                for line in text.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    segments.append((seq_num, urljoin(effective_url, line)))
                    seq_num += 1

                if state["seq"] < 0:
                    # Live-Einstieg am aktuellen Rand, nicht am Anfang des
                    # (ohnehin nur ~30-60s umfassenden) Sliding-Window-Puffers.
                    state["seq"] = segments[-1][0] if segments else 0

                for num, seg_url in [s for s in segments if s[0] >= state["seq"]]:
                    if self._cancelled:
                        break
                    try:
                        seg_data = urlopen(Request(seg_url, headers=headers), timeout=10).read()
                    except Exception:
                        continue
                    f.write(seg_data)
                    f.flush()
                    self._downloaded += len(seg_data)
                    self._segs_done  += 1
                    state["seq"] = num + 1
                    if self.on_progress:
                        try:
                            self.on_progress(self)
                        except Exception:
                            pass
                    if self.duration and self.elapsed() >= self.duration:
                        self._cancelled = True
                        break

                return target_duration, ("#EXT-X-ENDLIST" in text)

            while not self._cancelled:
                v_reload, v_end = fetch_segments(video_url, f_video, video_state)
                a_reload, a_end = (v_reload, True)
                if f_audio:
                    a_reload, a_end = fetch_segments(audio_url, f_audio, audio_state)

                if self._cancelled or (v_end and a_end):
                    break
                if self.duration and self.elapsed() >= self.duration:
                    break
                self._wait(min(v_reload, a_reload))

            f_video.close(); f_video = None
            if f_audio:
                f_audio.close(); f_audio = None

            if audio_path:
                self._mux(video_path, audio_path, self.filepath)

            if self.on_done:
                try:
                    self.on_done(self)
                except Exception:
                    pass
        except Exception as e:
            _dbg("HLSRecorder error: %s" % e)
            if self.on_error:
                try:
                    self.on_error(self, e)
                except Exception:
                    pass
        finally:
            if f_video:
                f_video.close()
            if f_audio:
                f_audio.close()
