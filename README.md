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
- **Empfohlen:** Aktuelles CA-Zertifikat-Paket (`ca-certificates-mozilla`), im Release-Archiv enthalten. Das CA-Bundle in VTi-Images ist von 2014 und kennt viele moderne Root-CAs nicht — ohne das Paket schlägt der HTTPS-Abruf von magentamusik.de stillschweigend fehl (leere Katalogliste, keine Cover). Wer bereits [StreamAnything](https://github.com/boingbasti/e2-StreamAnything) installiert hat, ist damit bereits versorgt.

Ohne aktuelles CA-Bundle und ohne ServiceApp/exteplayer3 ist das Plugin technisch funktionslos: HTTPS-Verbindungen schlagen fehl und HLS-Streams laufen mit dem eingebauten GStreamer-Player nicht zuverlässig.

## Installation

### Methode 1: IPK-Paket (empfohlen)

1. Das Release-Archiv von der [Releases-Seite](https://github.com/boingbasti/e2-magentatv/releases) herunterladen und entpacken. Es enthält:
   - `enigma2-plugin-extensions-magentamusik_*.ipk` — das Plugin
   - `ca-certificates-mozilla_*.ipk` — aktuelles CA-Bundle (falls noch nicht installiert)
2. Beide `.ipk`-Dateien per FTP oder USB auf den Receiver kopieren, z. B. nach `/tmp/`.
3. Per SSH installieren — zuerst das CA-Bundle (falls noch nicht vorhanden), dann das Plugin:

```sh
opkg install /tmp/ca-certificates-mozilla_*.ipk
opkg install /tmp/enigma2-plugin-extensions-magentamusik_*.ipk
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
| **ROT** | Aufnahme starten (Live-Streams) |
| **GRÜN** | Download starten (VOD-Inhalte) |
| **GELB** | Aktive Aufnahmen anzeigen |
| **BLAU** | Einstellungen öffnen |
| **INFO** | Aktive Aufnahmen anzeigen |
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

Aufnahmen werden als `.ts`-Datei im **Download-Ordner** gespeichert und sind nach der Aufnahme in der Enigma2-Mediathek verfügbar. Zu jeder Aufnahme wird eine `.eit`- und `.jpg`-Sidecar-Datei angelegt.

Beim Start der Aufnahme kann die Dauer gewählt werden (15 / 30 / 60 / 120 Minuten oder eine freie Minutenzahl).

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
