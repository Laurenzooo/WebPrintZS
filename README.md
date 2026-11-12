# Web-Druckdienst für den Arbeitsraum

Nutzer laden am Laptop oder Handy ein PDF auf einer Website hoch, wählen die Druckeinstellungen, und der
Drucker im Arbeitsraum druckt automatisch. Es gibt keinen WLAN-Wechsel und keine Treiberinstallation bei den Nutzern.

```
Laptop/Handy ──Upload (HTTPS)──▶ Web-App (Coolify) ──▶ SQLite-Warteschlange + PDF auf Volume
Druck-PC ──pollt alle 5 s (HTTPS, nur ausgehend)──▶ holt Auftrag ──▶ druckt ──▶ meldet Status
```

Der Druck-PC holt die Aufträge selbst ab (Pull-Prinzip). Er muss von außen **nicht** erreichbar sein, er braucht
nur ausgehendes HTTPS. Der Drucker hängt weiterhin nur per LAN am Druck-PC.

> ⚠️ **Betrieb mit der Uni-IT abstimmen.** Bevor der Dienst live geht, mit der IT bzw. dem Rechenzentrum klären:
> ob ein dauerhaft laufender PC mit ausgehenden HTTPS-Verbindungen zu einem externen Server im Uni-Netz zulässig
> ist, ob der Drucker-LAN-Abschnitt so bleiben darf, und welche Vorgaben für Datenschutz und Speicherdauer gelten.
> Im Log stehen Name, IP-Adresse und Dateiname der Nutzer, das ist personenbezogen (DSGVO). Am besten auch mit
> dem Fachschaftsrat oder den Verantwortlichen des Raums absprechen.

---

## Inhalt

1. [Projektstruktur](#1-projektstruktur)
2. [Web-App deployen (Coolify)](#2-web-app-deployen-coolify)
3. [Druck-Agent auf dem Windows-PC installieren](#3-druck-agent-auf-dem-windows-pc-installieren)
4. [Druckeroptionen auslesen und Instanzen einrichten](#4-druckeroptionen-auslesen-und-instanzen-einrichten)
5. [IPP-Direktdruck (HP OfficeJet Pro 7740)](#5-ipp-direktdruck-hp-officejet-pro-7740)
6. [Test-Checkliste](#6-test-checkliste)
7. [Betrieb und Fehlerbehebung](#7-betrieb-und-fehlerbehebung)
8. [Referenz: Agent-API, Datenmodell](#8-referenz)

---

## 1. Projektstruktur

```
docker-compose.yml        Coolify / lokal
.env.example              alle Einstellungen der Web-App
server/
  Dockerfile, requirements.txt
  presets.json            Voreinstellungen ("Abgabe A3 farbig", ...)
  app/
    main.py               Routen: Login, Upload, Status, Admin, Agent-API
    db.py                 SQLite (Tabelle jobs = Warteschlange + Log)
    pdfcheck.py           MIME/Magic-Bytes/Seitenzahl, Seitenbereiche
    cleanup.py            Verfall, Timeouts, Dateien löschen, Log-Aufbewahrung
    templates/, static/   Oberfläche (deutsch, mobilfreundlich, ohne externe Ressourcen)
agent/
  druckagent.py           der Agent (nur Python-Standardbibliothek)
  ipp.py                  minimaler IPP-Client (Direktdruck + Druckerstatus/Tinte)
  config.example.toml
  windows/
    install-task.ps1          Autostart per Aufgabenplanung
    show-printer-options.ps1  Treiberoptionen auslesen
    setup-instances.ps1       Druckerinstanzen mit Voreinstellungen anlegen
```

**Stack:** Python 3.12, FastAPI, Jinja2, SQLite (ohne ORM), pypdf. Läuft in einem Container mit genau einem
Prozess. Das reicht für einen Arbeitsraum locker aus.

**Warum nicht Vercel?** Vercel-Funktionen sind serverlos und haben keine dauerhafte Festplatte, also keine
SQLite-Datei und keine zwischengespeicherten PDFs. Request-Bodies sind auf ca. 4,5 MB begrenzt, und es gibt keine
Hintergrundprozesse fürs Aufräumen. Man bräuchte zusätzlich Blob-Storage und eine externe Datenbank. Für dieses
Projekt sind Coolify, ein kleiner VPS oder ein Homeserver mit Cloudflare Tunnel die bessere Wahl. Die
`docker-compose.yml` läuft überall gleich.

---

## 2. Web-App deployen (Coolify)

### 2.1 Geheimnisse erzeugen

```bash
python3 -c "import secrets; print('AGENT_TOKEN=' + secrets.token_urlsafe(32)); print('SECRET_KEY=' + secrets.token_urlsafe(48))"
```

`ACCESS_CODE` (für Nutzer, z. B. am Drucker aushängen) und `ADMIN_PASSWORD` (lang!) selbst wählen.

### 2.2 In Coolify

1. Repo in Git pushen (GitHub/GitLab/Gitea) und in Coolify **New Resource → Public/Private Repository** wählen.
2. **Build Pack: Docker Compose**, Pfad `/docker-compose.yml`.
3. Unter **Environment Variables** mindestens `ACCESS_CODE`, `ADMIN_PASSWORD`, `AGENT_TOKEN` und `SECRET_KEY`
   setzen, optional weitere aus `.env.example`.
4. Beim Service `web` die **Domain** eintragen (z. B. `https://druck.example.de`). Coolify routet auf Port 8000
   und holt das Let's-Encrypt-Zertifikat.
5. Deploy. Prüfen: `https://druck.example.de/healthz` liefert `{"ok":true}`.

Das Volume `druckdienst-data` (→ `/data`) enthält die Datenbank und die PDFs. Für Backups reicht
`druckdienst.db`, die PDFs sind kurzlebig.

> Coolify bzw. Traefik setzt `X-Forwarded-For`. uvicorn läuft mit `--proxy-headers`, damit Rate-Limit und Log die
> echte Client-IP sehen. Wichtig: Der Container darf nur über den Proxy erreichbar sein, keinen Port direkt ins
> Internet freigeben.

### 2.3 Lokal testen

```bash
cp .env.example .env          # Werte eintragen, COOKIE_SECURE=false
# in docker-compose.yml ports auf "8000:8000" ändern
docker compose up --build     # → http://localhost:8000
```

Ohne Docker:

```bash
cd server && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
ACCESS_CODE=test ADMIN_PASSWORD=admin AGENT_TOKEN=$(python3 -c "import secrets;print(secrets.token_urlsafe(32))") \
SECRET_KEY=$(python3 -c "import secrets;print(secrets.token_urlsafe(48))") DATA_DIR=./data COOKIE_SECURE=false \
.venv/bin/uvicorn app.main:app --reload
```

### 2.4 Einstellungen (`.env`)

| Variable | Standard | Bedeutung |
|---|---|---|
| `ACCESS_CODE` | – | Zugangscode für Nutzer. Ändern meldet alle Nutzer ab. |
| `ADMIN_PASSWORD` | – | Passwort für `/admin` |
| `AGENT_TOKEN` | – | Geheimnis des Druck-Agenten (≥ 24 Zeichen) |
| `SECRET_KEY` | – | signiert Session-Cookies (≥ 32 Zeichen) |
| `MAX_UPLOAD_MB` | 50 | Größenlimit pro PDF |
| `MAX_PAGES` / `MAX_COPIES` | 300 / 20 | Grenzen pro Auftrag |
| `RATE_LIMIT_JOBS_PER_HOUR` | 20 | Aufträge pro Stunde je IP, Name oder Sitzung |
| `MAX_PAGES_PER_DAY` | 0 | Druckseiten je Nutzer in 24 h (0 = aus) |
| `JOB_EXPIRE_HOURS` | 12 | nicht gedruckte Aufträge danach verwerfen und Datei löschen |
| `PRINTING_TIMEOUT_MIN` | 15 | „Wird gedruckt“ ohne Rückmeldung → Fehler |
| `LOG_RETENTION_DAYS` | 365 | Logeinträge danach löschen |
| `FORMATS` | A4,A3 | angebotene Papierformate |
| `DUPLEX_FORMATS` | A4,A3 | Formate mit Duplex-Option (am Gerät prüfen, siehe Checkliste) |
| `COOKIE_SECURE` | true | lokal ohne HTTPS auf `false` |

### 2.5 Presets

`server/presets.json` legt fest, was im Feld „Voreinstellung“ erscheint. Die `id` wird im Log gespeichert und vom
Agenten fürs Routing auf Druckerinstanzen genutzt (siehe Abschnitt 4).

```json
{ "id": "abgabe-a3-farbe", "name": "Abgabe A3 farbig", "format": "A3", "farbe": true,
  "duplex": "none", "kopien": 1, "hinweis": "Bitte prüfen, ob A3-Papier eingelegt ist." }
```

Ändert ein Nutzer nach der Preset-Auswahl ein Feld, wird das Preset abgewählt. So stimmt das Routing immer mit den
tatsächlichen Einstellungen überein.

### 2.6 Sicherheitsmaßnahmen im Überblick

- Zugang nur mit Zugangscode und Name. Fehlversuche werden pro IP begrenzt (8 in 15 min).
- Session-Cookie signiert, `HttpOnly`, `SameSite=Lax`, `Secure`. Dazu CSRF-Token in allen Formularen.
- Upload: MIME-Typ **und** Magic Bytes `%PDF-` werden geprüft, die Datei muss mit pypdf lesbar sein
  (passwortgeschützte PDFs werden abgelehnt). Dazu Größen-, Seiten- und Kopienlimit.
- Rate-Limit pro IP, Name und Sitzung, persistent in der Datenbank.
- Agent-API nur mit Bearer-Token. Ein Auftrag wird atomar „geclaimt“, damit nichts doppelt gedruckt wird.
- Dateien werden nach erfolgreichem Druck sofort gelöscht, nicht abgeholte nach `JOB_EXPIRE_HOURS`.
- Strenge CSP, keine externen Skripte, `noindex`.

---

## 3. Druck-Agent auf dem Windows-PC installieren

### 3.1 Voraussetzungen

1. **Drucker im LAN mit fester IP.** Am HP-Display oder in der Weboberfläche des Druckers
   (`http://<IP>` → Netzwerk → IPv4) eine feste IP vergeben, z. B. `192.168.1.50`. Alternativ eine
   DHCP-Reservierung einrichten.
2. **HP-Treiber installieren** („HP OfficeJet Pro 7740 Full Feature Software“ oder „Basic Driver“ von hp.com)
   und den Drucker über TCP/IP mit genau dieser IP hinzufügen. Testseite drucken.
3. **Python 3.11 oder neuer** von python.org. Im Installer anhaken: *Add python.exe to PATH* und
   *Install for all users* (wichtig für die Aufgabenplanung).
4. **SumatraPDF** (Installer von sumatrapdfreader.org) nach `C:\Program Files\SumatraPDF\`.
5. **Energieoptionen:** Standby und Ruhezustand auf „Nie“ stellen. Windows-Updates außerhalb der Raumzeiten
   neu starten lassen (Nutzungszeit einstellen).

### 3.2 Agent einrichten

```powershell
# z. B. nach C:\WebDruck kopieren (Inhalt von agent\)
cd C:\WebDruck
copy config.example.toml config.toml
notepad config.toml        # server_url, api_token (= AGENT_TOKEN), printer
python druckagent.py --list-printers      # exakten Druckernamen nachsehen
```

Testdruck ganz ohne Server, nur mit Routing und Druckweg:

```powershell
python druckagent.py --test-print C:\Users\Public\test.pdf --format A4 --duplex long --kopien 1
python druckagent.py --test-print C:\Users\Public\test.pdf --format A3 --farbe --preset abgabe-a3-farbe
```

Gegen den Server testen (ein Durchlauf, Ausgabe in der Konsole):

```powershell
python druckagent.py --once
```

### 3.3 Als Autostart einrichten (Aufgabenplanung)

PowerShell **als Administrator**:

```powershell
cd C:\WebDruck
Set-ExecutionPolicy -Scope Process Bypass
.\windows\install-task.ps1            # empfohlen: läuft unter einem Benutzerkonto (fragt nach Passwort)
# oder: .\windows\install-task.ps1 -AsSystem
```

Die Aufgabe `WebDruckAgent` startet 30 s nach dem Hochfahren, auch ohne Anmeldung. Bei einem Absturz startet
sie jede Minute neu und hat keine Laufzeitbegrenzung. Der Agent selbst übersteht Netzwerkausfälle mit
Backoff bis 2 min. Statusmeldungen, die nicht ankommen, landen in `work\pending_reports.json` und werden später
nachgereicht. Es läuft immer nur eine Instanz.

- **Benutzerkonto (empfohlen):** Am besten ein eigenes lokales Konto, z. B. `druckagent`, einmal anmelden und
  prüfen, dass Drucker und Instanzen dort sichtbar sind.
- **SYSTEM:** kein Passwort nötig. Funktioniert mit lokal installierten Druckern (TCP/IP-Port) in der Regel auch,
  bei Problemen auf das Benutzerkonto umsteigen.

Kontrolle: Die Logdatei (`log_file` in `config.toml`, rotiert 5 × 1 MB) und die Admin-Ansicht der Web-App, die
den Agenten als „Online“ mit Heartbeat zeigen sollte.

Deinstallieren: `.\windows\install-task.ps1 -Uninstall`

### 3.4 Was „Gedruckt“ bedeutet

- **sumatra:** SumatraPDF übergibt an die Windows-Warteschlange. Mit `wait_for_spooler = true` wartet der Agent,
  bis der Auftrag die Warteschlange verlassen hat, also der Drucker die Daten angenommen hat. Papierstau oder
  leere Tinte, die danach auftreten, bekommt der Agent nicht mehr mit.
- **ipp:** Der Agent fragt den Auftragsstatus direkt beim Drucker ab und meldet erst bei `completed` „Gedruckt“.
  Bei `aborted` oder `canceled` meldet er einen Fehler mit dem Grund.

### 3.5 Linux / macOS (CUPS)

`method = "cups"`, `printer = "<CUPS-Name>"`. Für den Autostart unter Linux eine systemd-Unit anlegen:

```ini
# /etc/systemd/system/druckagent.service
[Unit]
Description=Web-Druckdienst Agent
After=network-online.target cups.service
Wants=network-online.target

[Service]
User=druckagent
ExecStart=/usr/bin/python3 /opt/druckagent/druckagent.py --config /opt/druckagent/config.toml
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

`sudo systemctl enable --now druckagent`

---

## 4. Druckeroptionen auslesen und Instanzen einrichten

Der Agent kombiniert drei Wege, in dieser Reihenfolge:

1. **Kommandozeilenoptionen** für jeden Auftrag: Format, Farbe, Duplex, Kopien und Seitenbereich, unter Windows
   über SumatraPDF, z. B.
   `SumatraPDF.exe -print-to "HP7740_A3_farbe" -print-settings "1-3,paper=A3,color,simplex,2x,fit" -silent datei.pdf`
2. **Druckerinstanzen** für alles, was die Kommandozeile nicht erreicht (Papierfach, Qualität, Papiertyp). Das sind
   derselbe Drucker mehrfach installiert, jeweils mit eigenen Treiber-Standardwerten. Die `[[route]]`-Regeln in
   `config.toml` legen fest, welche Instanz ein Auftrag bekommt.
3. **IPP direkt** am Treiber vorbei, siehe Abschnitt 5.

### 4.1 Optionen auslesen

```powershell
cd C:\WebDruck\windows
.\show-printer-options.ps1                                      # alle Drucker
.\show-printer-options.ps1 -PrinterName "HP OfficeJet Pro 7740 series" -OutFile optionen.txt
```

Das Skript zeigt den Treiber, den Port und die IP, die aktuellen Standardwerte und **alle Features aus den
PrintCapabilities** mit ihren Optionen. Für das Mapping sind diese relevant:

| Feature | Bedeutung | Weg |
|---|---|---|
| `psk:PageMediaSize` | Papierformat | Kommandozeile (`paper=`) |
| `psk:PageOutputColor` | Farbe / Graustufen | Kommandozeile (`color`/`monochrome`) |
| `psk:JobDuplexAllDocumentsContiguously` | Duplex | Kommandozeile (`duplexlong`/`duplexshort`) |
| `psk:JobInputBin` / `PageInputBin` | Papierfach (beim 7740 Fach 1 und Fach 2) | Instanz oder `bin = "2"` in der Route |
| `psk:PageOutputQuality` | Entwurf / Normal / Optimal | nur Instanz |
| `psk:PageMediaType` | Normal-, Foto-, Broschürenpapier | nur Instanz |

Linux/macOS: `lpoptions -p <Drucker> -l` zeigt alle PPD-Optionen (z. B. `InputSlot`, `HPPrintQuality`).

### 4.2 Instanzen anlegen (Windows)

`windows\setup-instances.ps1` öffnen und die Liste `$instances` anpassen. Die Namen müssen zu den Routen in
`config.toml` passen. Dann **als Administrator**:

```powershell
.\setup-instances.ps1 -BasePrinter "HP OfficeJet Pro 7740 series" -OpenDialogs
```

Das Skript legt jede Instanz mit demselben Treiber und Port an (`Add-Printer`) und setzt Format, Farbe und
Duplex (`Set-PrintConfiguration`). Mit `-OpenDialogs` öffnet es danach den Treiberdialog, in dem du einmalig Fach
und Qualität einstellst und mit OK speicherst.

Beispiel für den 7740: Wenn A3-Papier dauerhaft in Fach 2 liegt, bekommt `HP7740_A3_farbe` die Papierquelle
„Fach 2“ und die Qualität „Optimal“, `HP7740_A4_sw_duplex` bekommt „Entwurf“ und „Graustufen“.

CUPS-Gegenstück: `lpoptions -p HP7740 -o media=A3 -o ColorModel=RGB` legt Standardwerte fest. Instanzen
(`lpoptions -p HP7740/A3farbe -o InputSlot=Tray2`) werden mit `printer = "HP7740/A3farbe"` angesprochen.

### 4.3 Mapping in `config.toml`

```toml
[[route]]
name    = "Abgabe A3 farbig"
preset  = "abgabe-a3-farbe"        # Preset-id aus server/presets.json
printer = "HP7740_A3_farbe"

[[route]]
format  = "A3"                     # alle übrigen A3-Aufträge
printer = "HP7740_A3_farbe"

[[route]]
format  = "A4"
farbe   = false
duplex  = "long"
printer = "HP7740_A4_sw_duplex"
```

Bedingungen sind `preset`, `format`, `farbe` und `duplex`. Ziele sind `printer`, `method`, `bin` und `extra`.
Die erste passende Regel gewinnt. Die Kommandozeilenoptionen werden trotzdem mitgeschickt und überschreiben die
Instanz-Standardwerte für Format, Farbe und Duplex. Fach und Qualität kommen aus der Instanz.

---

## 5. IPP-Direktdruck (HP OfficeJet Pro 7740)

Der 7740 unterstützt IPP bzw. AirPrint auf Port 631. Ob er **PDF nativ** annimmt, hängt von der Firmware ab und
lässt sich nur am Gerät sicher sagen. Prüfen:

```powershell
python druckagent.py --check-ipp ipp://192.168.1.50/ipp/print
```

Die Ausgabe enthält `document-format-supported`, `media-supported`, `sides-supported`, `media-source-supported`
und die Tintenstände, dazu am Ende die Bewertung:

- **✔ application/pdf vorhanden:** `method = "ipp"` und `[ipp] uri = "ipp://192.168.1.50/ipp/print"` setzen.
  Dann ist kein Treiber und kein SumatraPDF nötig, die Einstellungen gehen als IPP-Attribute raus (`media`,
  `sides`, `print-color-mode`, `copies`, `page-ranges`, `print-scaling`). Einzelne Routen können auch gemischt
  laufen (`method = "ipp"` nur für bestimmte Regeln).
- **✘ kein PDF:** Viele HP-Tintenstrahler nehmen über IPP nur `image/urf`, `image/pwg-raster` oder `image/jpeg`
  an. Dann bleibt es bei SumatraPDF.

**In jedem Fall sinnvoll:** `[ipp] status_uri` setzen. Der Agent liest dann bei jedem Heartbeat Druckerstatus,
Warnungen (z. B. `media-empty`, `marker-supply-low`) und **Tintenstände** aus, und die Admin-Ansicht zeigt sie an.

---

## 6. Test-Checkliste

**Web-App**
- [ ] `/healthz` liefert `{"ok":true}` über HTTPS
- [ ] `/` leitet ohne Login auf `/login` um. Ein falscher Code zeigt einen Fehler, nach 8 Fehlversuchen kommt eine Sperre.
- [ ] Login mit Code und Name, die Statusleiste zeigt „Drucker bereit“ (sobald der Agent läuft)
- [ ] Upload einer `.txt`-Datei, die in `.pdf` umbenannt wurde → „keine gültige PDF-Datei“
- [ ] Upload > `MAX_UPLOAD_MB` → „Datei zu groß“
- [ ] Passwortgeschütztes PDF → wird abgelehnt
- [ ] Seitenbereich `99` bei einem 5-seitigen PDF → Fehlermeldung
- [ ] Preset wählen → Felder werden vorbelegt. Ein Feld ändern → Preset springt auf „eigene Einstellungen“.
- [ ] Für Formate außerhalb von `DUPLEX_FORMATS` ist Duplex ausgegraut
- [ ] Mehr als `RATE_LIMIT_JOBS_PER_HOUR` Aufträge → „Zu viele Aufträge“
- [ ] Wartenden Auftrag abbrechen → „Abgebrochen“, die Datei ist im Volume gelöscht
- [ ] Auf dem Handy (schmaler Bildschirm) ist alles bedienbar

**Agent und Drucker**
- [ ] `--list-printers` zeigt den Drucker und alle Instanzen
- [ ] `--test-print` A4 s/w einseitig, A4 duplex lange Kante, A3 farbig: Ergebnis am Papier kontrollieren
- [ ] **A3 duplex** am 7740 testen. Wenn der Duplexer A3 nicht kann: `DUPLEX_FORMATS=A4` setzen.
- [ ] Kopien = 2 und Seitenbereich `2-3` werden eingehalten
- [ ] **Ausrichtung:** Querformat-PDF mit „Automatisch“ drucken, es kommt quer heraus. Dasselbe PDF mit „Hochformat“
  drucken. Für feste Ausrichtung sendet der Agent `portrait`/`landscape` an SumatraPDF (CUPS: `orientation-requested`,
  IPP: `orientation-requested`). Bei „Automatisch“ dreht SumatraPDF die Seiten selbst passend.
- [ ] **Beidseitig mit Querformat:** Die Rückseiten stehen beim seitlichen Umblättern richtig herum (die App wählt hier
  automatisch die kurze Kante).
- [ ] Farbe/s-w: Ein farbiges PDF mit „Schwarzweiß“ drucken, es kommt ohne Farbtinte heraus
- [ ] Preset „Abgabe A3 farbig“ landet auf Instanz `HP7740_A3_farbe` (steht im Agent-Log: `Route '...'`)
- [ ] `--check-ipp` ausgeführt, Ergebnis notiert (PDF ja/nein)
- [ ] Ende-zu-Ende: Handy im Uni-WLAN → Upload → Druck → Status wechselt auf „Gedruckt“
- [ ] Papier herausnehmen und drucken → der Auftrag hängt und wird nach `print_timeout` als Fehler gemeldet
  (ipp bzw. sumatra mit `wait_for_spooler`)
- [ ] Netzwerkkabel des Druck-PCs ziehen → Admin zeigt den Agenten nach ca. 60 s „Offline“. Hochgeladene
  Aufträge bleiben in der Warteschlange. Kabel wieder rein → der Agent druckt sie ohne Neustart.
- [ ] Druck-PC neu starten, ohne sich anzumelden → der Agent ist nach ca. 1 min wieder online
- [ ] Agent-Prozess im Task-Manager beenden → die Aufgabenplanung startet ihn innerhalb von 1 min neu

**Admin**
- [ ] `/admin` zeigt Agent-Status, Heartbeat, Tintenstände (bei `status_uri`), Warteschlange und Verbrauch
- [ ] Auftrag in der Warteschlange abbrechen. Einen fehlgeschlagenen Auftrag „Erneut drucken“.
- [ ] CSV-Export lässt sich in Excel öffnen, Umlaute sind korrekt

---

## 7. Betrieb und Fehlerbehebung

| Symptom | Ursache / Lösung |
|---|---|
| Admin: Agent „Noch nie verbunden“ | `server_url` oder `api_token` falsch. Agent-Log prüfen, bei 401 steht dort „lehnt das API-Token ab“. |
| Agent online, aber nichts wird gedruckt | `python druckagent.py --test-print ...` in der Konsole ausführen und die Fehlermeldung lesen. Druckername exakt? |
| „SumatraPDF nicht gefunden“ | `[sumatra] path` anpassen |
| Fehler „Keine Rückmeldung vom Druck-PC“ | Der Agent ist während des Drucks abgestürzt oder hat das Netz verloren. Am Gerät prüfen, ob gedruckt wurde. Bei Bedarf im Admin „Erneut drucken“. **Es wird nie automatisch erneut gedruckt**, damit nichts doppelt kommt. |
| Auftrag hängt in der Windows-Warteschlange | Papier, Tinte oder Stau prüfen. Warteschlange über „Drucker & Scanner → Warteschlange öffnen“ leeren. |
| Verzerrter oder abgeschnittener Druck | `[sumatra] scaling` (`fit` / `shrink` / `noscale`) anpassen |
| Zugangscode ist rumgegangen | `ACCESS_CODE` in Coolify ändern und neu deployen. Alle Nutzer werden abgemeldet. |

**Logs:** Web-App unter Coolify → Logs (Upload, Agent-Abholung, Status). Agent in der Datei unter `log_file`.
**Druck-Log und Verbrauch:** `/admin/log` und CSV-Export.

---

## 8. Referenz

### Agent-API (Header `Authorization: Bearer <AGENT_TOKEN>`)

| Methode | Pfad | Antwort |
|---|---|---|
| `POST` | `/api/agent/claim` | `200` + Auftrag (atomar auf `printing` gesetzt) oder `204` (leer). Zählt auch als Lebenszeichen. |
| `GET` | `/api/agent/jobs/{id}/file` | PDF (nur im Status `printing`) |
| `POST` | `/api/agent/jobs/{id}/status` | Body `{"status":"done"}` oder `{"status":"error","fehler":"..."}`. Bei `409` ist der Auftrag nicht mehr im Druck (z. B. abgebrochen). |
| `POST` | `/api/agent/heartbeat` | Body `{version, hostname, method, printer, printer_status, message}` |

Beispiel eines geclaimten Auftrags:

```json
{ "id": "d0464868…", "datei": "plan.pdf", "format": "A3", "farbe": true, "duplex": "none",
  "kopien": 2, "seiten": "1-3", "seitenzahl": 12, "druckseiten": 6, "preset": "abgabe-a3-farbe",
  "nutzer": "Alex M.", "status": "printing", "erstellt": "2026-11-12T13:57:20Z", "fehler": null,
  "download_url": "/api/agent/jobs/d0464868…/file" }
```

### Status

`queued` (in Warteschlange) → `printing` (wird gedruckt) → `done` (gedruckt) oder `error` (Fehler).
Daneben gibt es `cancelled` (vom Nutzer oder Admin abgebrochen) und `expired` (nicht rechtzeitig gedruckt).

### Datenmodell

Die Tabelle `jobs` ist zugleich Warteschlange und Log. Gespeichert werden: Datei, Größe, Seiten im Dokument,
Druckseiten (Bereich × Kopien), Blatt (Duplex berücksichtigt), Format, Farbe, Duplex, Kopien, Bereich, Preset,
Name, IP, Status, Fehler und Zeitstempel. Die PDFs liegen als `/data/files/<id>.pdf` daneben und werden gelöscht,
sobald sie nicht mehr gebraucht werden.
