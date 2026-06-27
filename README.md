# MagentaMusik Plugin für Enigma2

Enigma2-Plugin zum Durchsuchen und Abspielen von Festivals und Konzerten von [magentamusik.de](https://magentamusik.de) — direkt auf der Receiver-Fernbedienung.

![Plugin-Logo](MagentaMusik-Plugin_Logo.png)

## Funktionen

- Kachel- und Listenansicht aller verfügbaren Festivals und Konzerte
- Live-Streams der aktuellen Bühnen direkt abspielen
- Mit **CH+/-** während der Wiedergabe zwischen Streams umschalten
- Aufnahmen von Live-Streams (HLS-Recorder, zeitgesteuert oder sofort)
- Download von VOD-Inhalten ins lokale Verzeichnis
- Vorschaubilder (Cover) werden gecacht
- Einstellungen direkt im Plugin konfigurierbar

## Voraussetzungen

- Enigma2-Receiver (getestet auf **VU+ Uno 4K SE** mit **VTi**)
- Python 2.7 (Standard in Enigma2/VTi)
- **Empfohlen:** [ServiceApp](https://github.com/E2OpenPlugins/e2openplugin-ServiceApp) mit exteplayer3 für optimale HLS-Wiedergabe
- **Empfohlen:** Aktuelles CA-Zertifikat-Paket (`ca-certificates-mozilla`), als separate Datei im Release verfügbar. Das CA-Bundle in VTi-Images ist von 2014 und kennt viele moderne Root-CAs nicht — ohne das Paket schlägt der HTTPS-Abruf von magentamusik.de stillschweigend fehl (leere Katalogliste, keine Cover). Wer das Paket bereits über [StreamAnything](https://github.com/boingbasti/e2-StreamAnything) installiert hat, ist damit bereits versorgt.

Ohne aktuelles CA-Bundle und ohne ServiceApp/exteplayer3 ist das Plugin technisch funktionslos: HTTPS-Verbindungen schlagen fehl und HLS-Streams laufen mit dem eingebauten GStreamer-Player nicht zuverlässig.

## Installation

### Methode 1: IPK-Paket (empfohlen)

1. Beide Dateien vom [Release](https://github.com/boingbasti/e2-magentamusik/releases) herunterladen:
   - `enigma2-plugin-extensions-magentamusik_1.0.0_all.ipk` — das Plugin
   - `ca-certificates-mozilla_2026.05.30_all.ipk` — aktuelles CA-Bundle (falls noch nicht installiert)
2. Beide Dateien per FTP oder USB auf den Receiver kopieren, z. B. nach `/tmp/`.
3. Per SSH installieren — zuerst das CA-Bundle (falls noch nicht vorhanden), dann das Plugin:

```sh
opkg install /tmp/ca-certificates-mozilla_2026.05.30_all.ipk
opkg install /tmp/enigma2-plugin-extensions-magentamusik_1.0.0_all.ipk
```

Oder über den **Softwaremanager** der Box (IPK-Dateien direkt öffnen).

4. Enigma2 neu starten:

```sh
killall -9 enigma2
```

Das Plugin erscheint danach unter **Menü → Plugins → MagentaMusik**.

### Methode 2: Manuell per SSH

1. Das Repository klonen oder als ZIP herunterladen.
2. Den Ordner `MagentaMusik/` auf den Receiver kopieren:

```sh
scp -r MagentaMusik/ root@<IP-DER-BOX>:/usr/lib/enigma2/python/Plugins/Extensions/
```

3. Enigma2 neu starten:

```sh
ssh root@<IP-DER-BOX> "killall -9 enigma2"
```

## Bedienung

| Taste | Funktion |
|-------|----------|
| **OK** | Auswahl bestätigen / Abspielen |
| **Pfeil links/rechts** | Kachelansicht: Seite wechseln (wenn Einstellung aktiv) |
| **Pfeil oben/unten** | Kachel- / Listenbewegung |
| **CH+ / CH-** | Im Player: nächster / vorheriger Stream |
| **ROT** | Download starten (VOD-Inhalte) |
| **GRÜN** | Einstellungen öffnen |
| **GELB** | Zwischen Kachel- und Listenansicht wechseln |
| **BLAU** | Download-Manager öffnen (nur bei aktiven Downloads sichtbar) |
| **MENU** | Aufnahme starten oder planen (nur bei Live-Streams) |
| **INFO** | Aufnahmen und Timer anzeigen (nur bei aktiven Einträgen sichtbar) |
| **EXIT** | Zurück / Plugin schließen |

### Einstellungen

Die Einstellungen sind über die **BLAU**-Taste erreichbar:

| Einstellung | Standard | Beschreibung |
|-------------|----------|--------------|
| Vorschaubilder laden | Ein | Cover von den MagentaMusik-Servern laden und cachen |
| Seite wechseln mit Links/Rechts | Ein | In der Kachelansicht mit Links/Rechts die Seite wechseln statt Zeilensprung |
| ServiceApp auto-konfigurieren | Ein | exteplayer3 vor der Wiedergabe automatisch als Player setzen |
| Download-Ordner | `/media/hdd/movie/MagentaMusik` | Zielverzeichnis für Downloads |
| Debug-Log | Aus | Erweiterte Logmeldungen in die Enigma2-Log-Datei schreiben |

## Aufnahmen

Nicht alle Konzerte sind nach dem Festival als Stream abrufbar — viele Live-Übertragungen verschwinden nach dem Event ersatzlos. Die Aufnahmefunktion ermöglicht es, Konzerte während der Live-Übertragung direkt auf der Festplatte zu sichern.

### Sofortaufnahme

**MENU**-Taste auf einem Live-Stream drücken → Aufnahmedauer wählen:

- 30 Minuten / 1 Stunde / 2 Stunden / 3 Stunden / 6 Stunden
- Eigene Dauer in Minuten
- Bis ich stoppe (läuft unbegrenzt bis zum manuellen Abbruch)

Die Aufnahme startet sofort im Hintergrund, das Plugin bleibt dabei weiter bedienbar.

### Timer (Aufnahme vorplanen)

Im selben Menü gibt es den Eintrag **„Für später planen"** — damit öffnet sich der native Enigma2-Timer-Editor zur Eingabe von Datum, Startzeit und Dauer. Der Receiver startet die Aufnahme automatisch zum geplanten Zeitpunkt, auch aus dem Deep-Standby heraus.

Laufende Aufnahmen und geplante Timer sind über **INFO** in der Aufnahmenübersicht einsehbar und können dort auch vorzeitig gestoppt bzw. gelöscht werden.

Aufnahmen werden als `.ts`-Datei im **Download-Ordner** gespeichert und sind danach in der Enigma2-Mediathek verfügbar. Zu jedem Download werden eine `.txt`- (Titelinformationen) und eine `.meta`-Sidecar-Datei sowie ein `.jpg`-Vorschaubild angelegt.

## Deinstallation

### Wenn per IPK installiert

Per SSH:

```sh
opkg remove enigma2-plugin-extensions-magentamusik
killall -9 enigma2
```

### Wenn manuell installiert

```sh
rm -rf /usr/lib/enigma2/python/Plugins/Extensions/MagentaMusik
killall -9 enigma2
```

### Einstellungen und Cache entfernen (optional)

Die Plugin-Einstellungen und der Cover-Cache liegen im Plugin-Verzeichnis selbst und werden durch die obigen Befehle mit entfernt. Aufnahmen und Downloads im konfigurierten Download-Ordner (Standard: `/media/hdd/movie/MagentaMusik`) werden **nicht** automatisch gelöscht und müssen bei Bedarf manuell entfernt werden:

```sh
rm -rf /media/hdd/movie/MagentaMusik
```

## Bekannte Einschränkungen

- Das Plugin benötigt eine aktive Internetverbindung für den Katalog-Abruf und die Wiedergabe.
- Ohne aktuelles CA-Zertifikat-Paket können HTTPS-Verbindungen zu magentamusik.de fehlschlagen (leere Katalogliste, keine Cover).
- Die Aufnahmefunktion nutzt einen Software-HLS-Recorder — der rote Aufnahme-Punkt im Front-Display des Receivers wird dabei **nicht** angezeigt (technische Einschränkung von Enigma2).
- Verfügbare Inhalte hängen vom aktuellen Programm auf magentamusik.de ab. Außerhalb von Festival-Saisons kann die Liste leer sein.

## Lizenz

GPL v2 — siehe [LICENSE](LICENSE)
