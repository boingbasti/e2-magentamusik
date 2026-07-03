# -*- coding: utf-8 -*-
# downloader.py
# HTTP-Download fuer MagentaMusik - laedt HLS-/MP4-Streams direkt auf die Festplatte.
# Generische Engine ohne Abhaengigkeit von catalog.py/magentamusik.py - bekommt
# Zielordner, Cover-Pfad und Metadaten als fertige Werte uebergeben.

import os
import re
import shutil
import subprocess
import threading
import time

try:
    from urllib2 import urlopen, Request, HTTPRedirectHandler, build_opener, HTTPSHandler
except ImportError:
    from urllib.request import urlopen, Request, HTTPRedirectHandler, build_opener, HTTPSHandler

try:
    from urlparse import urlparse
except ImportError:
    from urllib.parse import urlparse

try:
    import httplib as _httplib
except ImportError:
    import http.client as _httplib

try:
    import ssl
    _ssl_context = ssl._create_unverified_context()
except Exception:
    _ssl_context = None

_LOG_FILE   = "/tmp/magentamusik.log"
_DEBUG_FLAG = "/tmp/mm_debug"

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def _log(msg):
    if not os.path.exists(_DEBUG_FLAG):
        return
    line = "[%.3f] [downloader] %s" % (time.time(), str(msg))
    try:
        with open(_LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# --------------------------------------------------------------------------
# Redirect-Handler (Behaelt Tarn-Header bei, blockiert aber falschen Host)
# --------------------------------------------------------------------------
class KeepHeadersRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        newreq = HTTPRedirectHandler.redirect_request(self, req, fp, code, msg, headers, newurl)
        if newreq:
            if hasattr(req, 'headers'):
                for key, val in req.headers.items():
                    if key.lower() not in ['host', 'content-length']:
                        newreq.add_header(key, val)
            if hasattr(req, 'unredirected_hdrs'):
                for key, val in req.unredirected_hdrs.items():
                    if key.lower() not in ['host', 'content-length']:
                        newreq.add_unredirected_header(key, val)
        return newreq


# --------------------------------------------------------------------------
# Sidecar-Dateien
# --------------------------------------------------------------------------
def write_info_txt(filepath, title, description=None, duration=None, topic=None):
    """Schreibt eine .txt Datei mit Stream-Infos neben die Download-Datei."""
    try:
        txt_path = os.path.splitext(filepath)[0] + ".txt"
        def _dec(v):
            if isinstance(v, bytes):
                return v.decode("utf-8", "replace")
            return v or ""
        lines = []
        t = _dec(title)
        if t:
            lines.append(t)
        d = _dec(description)
        if d:
            lines.append(d)
        dur = _dec(duration)
        if dur:
            lines.append(u"Laufzeit: " + dur)
        top = _dec(topic)
        if top and top.lower() != t.lower():
            lines.append(u"Festival: " + top)
        if lines:
            with open(txt_path, "w") as f:
                f.write(u"\n\n".join(lines).encode("utf-8"))
    except Exception:
        pass


def write_meta(filepath, title, description=None, duration=None):
    """Schreibt eine Enigma2 .meta Datei neben die Download-Datei (Datum, Titel, Beschreibung)."""
    try:
        meta_path = filepath + ".meta"
        def _dec(v):
            if isinstance(v, bytes):
                return v.decode("utf-8", "replace")
            return v or u""
        display_name = os.path.splitext(os.path.basename(filepath))[0]
        desc_str  = _dec(description)
        ts        = int(time.time())
        dur_secs  = 0
        dur_str   = _dec(duration)
        if dur_str:
            parts = dur_str.strip().split(":")
            try:
                if len(parts) == 3:
                    dur_secs = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                elif len(parts) == 2:
                    dur_secs = int(parts[0]) * 60 + int(parts[1])
            except (ValueError, IndexError):
                pass
        lines = [
            u"",
            display_name,
            desc_str,
            str(ts),
            u"",
            str(dur_secs) if dur_secs else u"",
        ]
        with open(meta_path, "w") as f:
            f.write(u"\n".join(lines).encode("utf-8"))
    except Exception:
        pass


def _copy_cover(filepath, cover_path):
    """Kopiert das bereits gecachte Vorschaubild neben die Download-Datei."""
    if not cover_path or not os.path.isfile(cover_path):
        return
    try:
        shutil.copyfile(cover_path, os.path.splitext(filepath)[0] + ".jpg")
    except Exception as e:
        _log("_copy_cover Fehler: %s" % e)


def convert_mp4_to_ts(mp4_path, on_done=None, on_error=None):
    """Konvertiert mp4_path verlustfrei zu .ts (ffmpeg -c copy) in einem Background-Thread."""
    def _run():
        ts_path = os.path.splitext(mp4_path)[0] + ".ts"
        try:
            _log("ffmpeg Start: %s" % mp4_path)
            cmd = ["ffmpeg", "-y", "-i", mp4_path, "-c", "copy", ts_path]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            _out, _err = proc.communicate()
            if proc.returncode != 0:
                raise Exception("ffmpeg Fehler (Code %d)" % proc.returncode)
            try:
                os.remove(mp4_path)
            except Exception:
                pass
            for ext in (".meta", ".txt", ".jpg"):
                try:
                    old = mp4_path + ext if ext == ".meta" else os.path.splitext(mp4_path)[0] + ext
                    new = ts_path + ext if ext == ".meta" else os.path.splitext(ts_path)[0] + ext
                    if os.path.exists(old):
                        os.rename(old, new)
                except Exception:
                    pass
            _log("ffmpeg Fertig: %s" % ts_path)
            if on_done:
                on_done(ts_path)
        except Exception as e:
            _log("ffmpeg Fehler: %s - %s" % (mp4_path, str(e)))
            try:
                if os.path.exists(ts_path):
                    os.remove(ts_path)
            except Exception:
                pass
            if on_error:
                on_error(str(e))
    t = threading.Thread(target=_run)
    t.daemon = True
    t.start()


# --------------------------------------------------------------------------
# Hilfsfunktionen
# --------------------------------------------------------------------------
def _sanitize(text):
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    allowed = set(u"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 -_\xe4\xf6\xfc\xc4\xd6\xdc\xdf")
    return u"".join(c for c in text if c in allowed).strip()


def _make_filename(title, url, topic=None):
    ext = ".ts" if url.split("?")[0].lower().endswith((".m3u8", ".m3u")) else ".mp4"
    safe_title = _sanitize(title) or "download"
    if topic:
        safe_topic = _sanitize(topic)
        if safe_topic and safe_topic.lower() != safe_title.lower():
            combined = safe_topic + " - " + safe_title
        else:
            combined = safe_title
    else:
        combined = safe_title
    result = combined[:100] + ext
    if isinstance(result, bytes):
        return result
    return result.encode("utf-8")


def format_size(size_bytes):
    if size_bytes <= 0:
        return "unbekannte Gr\xc3\xb6\xc3\x9fe"
    if size_bytes >= 1024 * 1024 * 1024:
        return "%.1f GB" % (size_bytes / 1024.0 / 1024.0 / 1024.0)
    if size_bytes >= 1024 * 1024:
        return "%.0f MB" % (size_bytes / 1024.0 / 1024.0)
    return "%.0f KB" % (size_bytes / 1024.0)


# --------------------------------------------------------------------------
# Keep-Alive-Verbindungspool fuer den Segment-Download
#
# Jeder urlopen()/opener.open()-Aufruf baut eine komplett neue TCP+TLS-
# Verbindung auf. Auf der schwachen ARM-CPU der Box (Software-TLS) kostet
# der Handshake pro Segment so viel Zeit, dass er den eigentlichen Download
# dominiert - gemessen: 16 HLS-Segmente (107MB) brauchten 22.6s mit je
# einer neuen Verbindung, nur 5.3s mit einer wiederverwendeten Verbindung
# (4-5x schneller, reine CPU-Ersparnis, keine zusaetzliche Bandbreite).
# httplib/http.client-HTTPSConnection-Objekte sind nicht parallel
# benutzbar - deshalb eine eigene Connection PRO WORKER-SLOT statt ein
# global geteilter Pool, jede Connection wird nur sequenziell von genau
# einem Worker-Thread benutzt.
# --------------------------------------------------------------------------
class _KeepAliveFetcher(object):

    def __init__(self, headers):
        self._headers = headers
        self._conns   = {}  # slot -> (scheme, host, HTTPSConnection/HTTPConnection)

    def fetch(self, url, slot, timeout=30, retries=3):
        parsed = urlparse(url)
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
        last_exc = None
        for attempt in range(retries):
            try:
                conn = self._get_conn(slot, parsed.scheme, parsed.netloc, timeout)
                conn.request("GET", path, headers=self._headers)
                resp = conn.getresponse()
                data = resp.read()
                if resp.status >= 400:
                    raise Exception("HTTP %d fuer %s" % (resp.status, url))
                return data
            except Exception as e:
                last_exc = e
                self._drop_conn(slot)
                if attempt < retries - 1:
                    time.sleep(0.5)
        raise last_exc

    def _get_conn(self, slot, scheme, host, timeout):
        cached = self._conns.get(slot)
        if cached and cached[0] == scheme and cached[1] == host:
            return cached[2]
        if cached:
            try:
                cached[2].close()
            except Exception:
                pass
        if scheme == "https":
            conn = _httplib.HTTPSConnection(host, timeout=timeout, context=_ssl_context)
        else:
            conn = _httplib.HTTPConnection(host, timeout=timeout)
        self._conns[slot] = (scheme, host, conn)
        return conn

    def _drop_conn(self, slot):
        cached = self._conns.pop(slot, None)
        if cached:
            try:
                cached[2].close()
            except Exception:
                pass

    def close_all(self):
        for cached in self._conns.values():
            try:
                cached[2].close()
            except Exception:
                pass
        self._conns.clear()


# --------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------
class Downloader(object):
    CHUNK_SIZE = 256 * 1024

    def __init__(self, url, title, save_dir, topic=None, description=None, duration=None,
                 cover_path=None, on_progress=None, on_done=None, on_error=None):
        self.url         = url
        self.title       = title
        self.description = description
        self.duration    = duration
        self.topic       = topic
        self.cover_path  = cover_path
        self.on_progress = on_progress
        self.on_done     = on_done
        self.on_error    = on_error

        self._cancelled  = False
        self._thread     = None
        self._downloaded = 0
        self._total      = 0
        self._converting = False
        self._muxing     = False

        if isinstance(save_dir, bytes):
            save_dir = save_dir.decode("utf-8", "replace")
        filename = _make_filename(title, url, topic=topic)
        if isinstance(filename, bytes):
            filename = filename.decode("utf-8", "replace")
        base, ext = os.path.splitext(filename)
        candidate = os.path.join(save_dir, filename).encode("utf-8")
        counter = 1
        while os.path.exists(candidate):
            candidate = os.path.join(save_dir, u"%s_%d%s" % (base, counter, ext)).encode("utf-8")
            counter += 1
        self.filepath = candidate
        self.save_dir = save_dir

    def start(self):
        self._thread = threading.Thread(target=self._run)
        self._thread.daemon = True
        self._thread.start()

    def cancel(self):
        self._cancelled = True

    def _download_hls_parallel(self, workers=4):
        """Laedt HLS-Segmente parallel (workers gleichzeitig) und muxiert Audio+Video mit ffmpeg.
        Gibt es keinen separaten Audio-Track, wird die Videospur direkt uebernommen."""
        try:
            from urlparse import urljoin
        except ImportError:
            from urllib.parse import urljoin

        try:
            _fetch_opener = build_opener(HTTPSHandler(context=_ssl_context)) if _ssl_context else None
        except Exception:
            _fetch_opener = None

        def fetch(url, retries=4):
            for attempt in range(retries):
                try:
                    r = Request(url)
                    r.add_header("User-Agent", _UA)
                    if _fetch_opener:
                        return _fetch_opener.open(r, timeout=30).read()
                    return urlopen(r, timeout=30).read()
                except Exception as e:
                    if attempt < retries - 1:
                        time.sleep(2 ** attempt)
                    else:
                        raise

        def get_segments(playlist_url):
            data = fetch(playlist_url).decode("utf-8", "ignore")
            return [urljoin(playlist_url, l.strip())
                    for l in data.splitlines()
                    if l.strip() and not l.strip().startswith("#")]

        master = fetch(self.url).decode("utf-8", "ignore")
        lines = master.splitlines()
        audio_url, best_bw, best_video_url, best_stream_inf = None, -1, None, None
        i = 0
        while i < len(lines):
            if lines[i].startswith("#EXT-X-STREAM-INF"):
                bw_m = re.search(r"BANDWIDTH=(\d+)", lines[i])
                bw = int(bw_m.group(1)) if bw_m else 0
                for j in range(i + 1, len(lines)):
                    v = lines[j].strip()
                    if v and not v.startswith("#"):
                        if bw > best_bw:
                            best_bw, best_video_url = bw, urljoin(self.url, v)
                            best_stream_inf = lines[i]
                        break
            i += 1

        # Nur den Default-Audio-Track der passenden Gruppe verwenden (gleiche
        # Logik wie player.py::_build_local_playlist - sonst kann ein falscher
        # Audio-Track, z.B. eine andere Sprache, gemuxt werden).
        audio_group_m = re.search(r'AUDIO="([^"]+)"', best_stream_inf or '')
        audio_group = audio_group_m.group(1) if audio_group_m else None
        for line in lines:
            if line.startswith("#EXT-X-MEDIA") and "TYPE=AUDIO" in line:
                if audio_group and ('GROUP-ID="%s"' % audio_group) not in line:
                    continue
                if "DEFAULT=YES" not in line:
                    continue
                m = re.search(r'URI="([^"]+)"', line)
                if m:
                    audio_url = urljoin(self.url, m.group(1))
        if not best_video_url:
            best_video_url = self.url

        video_segs = get_segments(best_video_url)
        audio_segs = get_segments(audio_url) if audio_url else []
        _log("HLS parallel: %d Video + %d Audio Segmente, %d workers" % (len(video_segs), len(audio_segs), workers))

        fp = self.filepath if isinstance(self.filepath, str) else self.filepath.decode("utf-8", "replace")
        vid_tmp = fp + ".vid.tmp"
        aud_tmp = fp + ".aud.tmp"
        self._total = 0
        self._downloaded = 0
        self._total_segs = len(video_segs) + len(audio_segs)
        self._segs_done = 0

        # Eine Connection pro Worker-Slot, wiederverwendet ueber alle Batches
        # UND ueber Video- und Audio-Download hinweg (meist derselbe Host) -
        # vermeidet den TLS-Handshake pro Segment, siehe Klassen-Docstring.
        keepalive = _KeepAliveFetcher({"User-Agent": _UA})

        def download_batched(segs, out_path):
            with open(out_path, "wb") as f:
                for start in range(0, len(segs), workers):
                    if self._cancelled:
                        return
                    batch = segs[start:start + workers]
                    results = [None] * len(batch)
                    errors = [None]

                    def _worker(url, idx):
                        try:
                            results[idx] = keepalive.fetch(url, idx)
                        except Exception as e:
                            errors[0] = e

                    threads = [threading.Thread(target=_worker, args=(url, idx))
                               for idx, url in enumerate(batch)]
                    for t in threads:
                        t.start()
                    for t in threads:
                        t.join()
                    if errors[0]:
                        raise errors[0]
                    for data in results:
                        if data:
                            f.write(data)
                            self._downloaded += len(data)
                            self._segs_done += 1
                            if self.on_progress:
                                self.on_progress(self._downloaded, 0)

        try:
            download_batched(video_segs, vid_tmp)
            if self._cancelled:
                try: os.remove(vid_tmp)
                except Exception: pass
                return

            if audio_segs:
                download_batched(audio_segs, aud_tmp)
                if self._cancelled:
                    for p in (vid_tmp, aud_tmp):
                        try: os.remove(p)
                        except Exception: pass
                    return
                cmd = ["ffmpeg", "-y", "-i", vid_tmp, "-i", aud_tmp,
                       "-c", "copy", "-f", "mpegts", fp]
                self._muxing = True
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                proc.wait()
                self._muxing = False
                for p in (vid_tmp, aud_tmp):
                    try: os.remove(p)
                    except Exception: pass
                if proc.returncode != 0:
                    err = proc.stderr.read()[-300:]
                    raise Exception("ffmpeg Mux Fehler (Code %d): %s" % (proc.returncode, err))
            else:
                os.rename(vid_tmp, fp)
            _log("HLS parallel fertig: %s" % fp)
        finally:
            keepalive.close_all()

    def _download_m3u8(self, opener, url):
        """Sequenzieller Fallback (nur Video-/Single-Track-Segmente aneinanderhaengen)."""
        req = Request(url)
        req.add_header("User-Agent", _UA)
        resp = opener.open(req, timeout=30)
        manifest = resp.read().decode('utf-8', 'ignore')
        lines = manifest.split('\n')

        if "#EXT-X-STREAM-INF" in manifest:
            sub_url = None
            for i, line in enumerate(lines):
                if line.startswith("#EXT-X-STREAM-INF"):
                    for j in range(i + 1, len(lines)):
                        if lines[j].strip() and not lines[j].startswith("#"):
                            sub_url = lines[j].strip()
                            break
            if sub_url:
                try:
                    from urlparse import urljoin
                except ImportError:
                    from urllib.parse import urljoin
                if not sub_url.startswith("http"):
                    sub_url = urljoin(url, sub_url)
                return self._download_m3u8(opener, sub_url)

        segments = []
        try:
            from urlparse import urljoin
        except ImportError:
            from urllib.parse import urljoin

        for line in lines:
            line = line.strip()
            if line and not line.startswith("#"):
                if not line.startswith("http"):
                    line = urljoin(url, line)
                segments.append(line)

        if not segments:
            raise Exception("Keine Videosegmente im Stream gefunden")

        self._total = 0
        self._downloaded = 0

        with open(self.filepath, "wb") as f:
            for seg_url in segments:
                if self._cancelled:
                    break
                seg_req = Request(seg_url)
                seg_req.add_header("User-Agent", _UA)
                seg_resp = opener.open(seg_req, timeout=30)
                chunk = seg_resp.read()
                f.write(chunk)
                self._downloaded += len(chunk)
                if self.on_progress:
                    self.on_progress(self._downloaded, 0)

    def _run(self):
        try:
            _log("Start: %s" % self.title)
            save_dir_b = self.save_dir if isinstance(self.save_dir, bytes) else self.save_dir.encode("utf-8")
            if not os.path.exists(save_dir_b):
                os.makedirs(save_dir_b)

            handlers = [KeepHeadersRedirectHandler()]
            if _ssl_context:
                handlers.append(HTTPSHandler(context=_ssl_context))
            opener = build_opener(*handlers)

            is_m3u8 = self.url.split("?")[0].lower().endswith((".m3u8", ".m3u"))

            if is_m3u8:
                try:
                    self._download_hls_parallel(workers=4)
                except Exception as e:
                    if self._cancelled:
                        raise
                    _log("HLS parallel fehlgeschlagen (%s), Fallback auf sequenziell" % e)
                    self._download_m3u8(opener, self.url)
            else:
                req = Request(self.url)
                req.add_header("User-Agent", _UA)
                resp = opener.open(req, timeout=30)

                total = 0
                try:
                    length = resp.headers.get("Content-Length") or resp.info().get("Content-Length")
                    if length:
                        total = int(length)
                except Exception:
                    pass

                downloaded = 0
                with open(self.filepath, "wb") as f:
                    while not self._cancelled:
                        chunk = resp.read(self.CHUNK_SIZE)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        self._downloaded = downloaded
                        self._total      = total
                        if self.on_progress:
                            self.on_progress(downloaded, total)

            if self._cancelled:
                try:
                    os.remove(self.filepath)
                except Exception:
                    pass
                _log("Abgebrochen: %s" % self.title)
                if self.on_error:
                    self.on_error("Abgebrochen")
            else:
                write_info_txt(self.filepath, self.title, self.description, self.duration, self.topic)
                write_meta(self.filepath, self.title, self.description, self.duration)
                _copy_cover(self.filepath, self.cover_path)
                _log("Fertig: %s" % self.title)
                if self.on_done:
                    self.on_done(self.filepath)

        except Exception as e:
            _log("Fehler: %s - %s" % (self.title, str(e)))
            try:
                if os.path.exists(self.filepath):
                    os.remove(self.filepath)
            except Exception:
                pass
            if self.on_error:
                self.on_error(str(e))
