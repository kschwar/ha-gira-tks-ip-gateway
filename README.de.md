# Gira TKS-IP Gateway für Home Assistant

🇬🇧 [English version](README.md)

Benutzerdefinierte Integration für [Home Assistant](https://www.home-assistant.io/),
die direkt mit einem **Gira TKS-IP-Gateway** (Türkommunikation) spricht. Es wird
kein HomeServer, keine Bridge und kein Cloud-Dienst benötigt.

Kein offizielles Produkt von Gira und nicht von Gira unterstützt. Nutzung auf
eigene Verantwortung.

## Funktionen

- **Türrufe und Tastenereignisse**, die das Gateway per TCP meldet (die
  HomeServer-Schnittstelle, Standardport 50050): welche Station und welcher
  Taster geklingelt hat, Lichttaste, Türöffnertaste, Gespräch angenommen /
  beendet.
- **SIP-Teilnehmer** (optional): Klingelerkennung, Live-Video (H.264) der
  Türstation während eines Rufs, Buttons zum Annehmen und Ablehnen.
- **Kameravorschau ohne Klingeln**: die Kamera der Türstation bei Bedarf
  einschalten, wie mit der Kamerataste des TKS-Communicators.
- **Türöffner und Licht** (optional): beides aus Home Assistant auslösen, über
  den verschlüsselten SIP-Steuerkanal des Gateways. Der Button meldet nur dann
  Erfolg, wenn das Gateway den Befehl quittiert hat.

Audio und Gegensprechen werden nicht unterstützt. Rufe ohne Video werden als
Klingeln gemeldet, können in Home Assistant aber nicht angenommen werden.

Die Integration ist auf Deutsch und Englisch verfügbar; Home Assistant zeigt
Namen und Zustände in der eingestellten Sprache an.

## Installation

### HACS

1. HACS → ⋮ → **Benutzerdefinierte Repositories** → URL dieses Repositorys
   hinzufügen, Typ **Integration**.
2. **Gira TKS-IP Gateway** installieren und Home Assistant neu starten.

### Manuell

`custom_components/gira_tks` nach `/config/custom_components/gira_tks`
kopieren und Home Assistant neu starten.

## Einrichtung

### 1. Gateway: HomeServer-Verbindung

Im **Gira TKS-Assistenten** unter **Anlage einrichten → HomeServer /
FacilityServer einrichten → HomeServer-Verbindungsdaten** eintragen:

- die IP-Adresse deines Home-Assistant-Systems,
- Port `50050`,
- einen frei gewählten Benutzernamen und ein Passwort.

Dann in Home Assistant die Integration hinzufügen (**Einstellungen → Geräte &
Dienste → Integration hinzufügen → Gira TKS-IP Gateway**) und die IPv4-Adresse
des Gateways, den Port sowie denselben Benutzernamen und dasselbe Passwort
eingeben.

### 2. Optional: SIP, Video, Türöffner und Licht

Den Dialog **Konfigurieren** der Integration öffnen:

| Option | Bedeutung |
|---|---|
| SIP-Teilnehmer aktivieren | Meldet einen SIP-Teilnehmer am Gateway an (UDP 5060). |
| SIP-Benutzername / SIP-Passwort | Zugangsdaten eines TKS-Communicator-Teilnehmers, angelegt im Gira TKS-Assistenten unter **TKS-Communicator einrichten**. Nicht die HomeServer-Zugangsdaten aus Schritt 1. |
| Lokale IP-Adresse | Adresse, die dem Gateway mitgeteilt wird. Leer lassen für automatische Erkennung. |
| Lokaler SIP-Port / H.264-RTP-Port | Lokale UDP-Ports, Standard 5062 und 8282. Das Gateway muss sie erreichen können. |
| Gespräch automatisch annehmen | Videorufe automatisch annehmen (standardmäßig aus). |
| Schaltbuttons Türöffner und Licht anzeigen | Fügt die Buttons **Türöffner** und **Licht** hinzu (standardmäßig aus). |

Hinweise:

- Einen Teilnehmer verwenden, der nicht gleichzeitig woanders angemeldet ist
  (z. B. ein TKS-Communicator auf einem PC mit demselben Konto). Jede
  gleichzeitige Anmeldung zählt gegen die Teilnehmerlizenz des Gateways.
- **Türöffner und Licht** funktionieren nur mit einem SIP-Konto, dem das
  Gateway das Schalten erlaubt. Antwortet das Gateway nicht oder passt das
  SIP-Passwort nicht, zeigt der Button eine Fehlermeldung mit dem Grund an.
- Speichern der Optionen lädt die Integration neu.

## Entitäten

Die Entitäts-IDs hängen vom Namen des Eintrags und der Sprache bei der
Einrichtung ab; die genaue ID steht in den Einstellungen der jeweiligen
Entität.

| Entität | Typ | Beschreibung |
|---|---|---|
| Ruf aktiv | Binärsensor | An, solange ein eingehender SIP-Ruf klingelt. |
| Türruf | Ereignis | Ereignistypen `ring` (Türruf), `internal_call` (Internruf), `floor_call` (Etagenruf); Station und Taster als Attribute. |
| Lichttaste, Türöffnertaste, Gespräch angenommen, Gespräch beendet | Ereignis | Tastenereignisse, die das Gateway meldet. |
| Letzte Taste | Sensor | Letztes Tastenereignis als Text. |
| Letztes Tastenereignis, Letzter Türruf | Sensor | Zeitstempel, Details als Attribute. |
| TKS-Ruf | Sensor | Zustand des SIP-Rufs. |
| TCP-Listener, SIP-Registrierung, Letzter Datensatz, Letzte SIP-Nachricht | Sensor | Diagnose. |
| Video RTP-Pakete | Sensor | Video-Diagnose; standardmäßig deaktiviert, bei Bedarf in den Entitätseinstellungen aktivieren. |
| Türstation | Kamera | Video des aktuellen Rufs. |
| Türstation (Kameravorschau) | Kamera | Kameravorschau auf Knopfdruck. |
| Anruf annehmen, Anruf ablehnen / beenden | Button | Rufsteuerung. |
| Kameravorschau starten / beenden | Button | Kameravorschau (endet nach 30 s von selbst). |
| SIP-Verbindung neu starten | Button | SIP-Teilnehmer neu anmelden. |
| Türöffner, Licht | Button | Nur mit eingeschalteter Option für die Schaltbuttons. |

## Ereignisse (Event-Bus)

| Ereignis | Daten |
|---|---|
| `gira_tks_door_call` | `call_type` (`door_call`, `internal_call`, `floor_call`), `bus_address`, `aufsatz`, `tastennummer`, `sender_type`, `received_at` |
| `gira_tks_key_event` | `key_event` (`light`, `door_opener`, `call_accepted`, `call_ended`), `communicator_number`, `bus_address`, `target_address` |
| `gira_tks_ringing` | `call_state`, `signal`, `video_available` (SIP-Ruf beginnt zu klingeln) |
| `gira_tks_frame_received` | Jeder gültige Datensatz vom Gateway (Diagnose) |

Alle Ereignisse enthalten `entry_id`.

### Beispiel: Benachrichtigung bei Türruf

```yaml
triggers:
  - trigger: event
    event_type: gira_tks_door_call
    event_data:
      call_type: door_call
actions:
  - action: notify.mobile_app_dein_handy
    data:
      message: "Es klingelt an der Tür."
```

### Beispiel: Tür öffnen

```yaml
actions:
  - action: button.press
    target:
      entity_id: button.DEIN_TUEROEFFNER_BUTTON  # ID aus den Entitätseinstellungen
```

## Netzwerk

| Richtung | Port | Zweck |
|---|---|---|
| Gateway → Home Assistant | TCP 50050 | Türrufe und Tastenereignisse |
| Home Assistant ↔ Gateway | UDP 5060 (Gateway) / 5062 (HA) | SIP |
| Gateway → Home Assistant | UDP 8282 | H.264-Video (RTP) |

## Grenzen

- Kein Audio, kein Gegensprechen.
- Das Gateway begrenzt, wie lange die Kamera laufen darf; danach zeigt es für
  eine Weile eine Sperre (`0x03`).
- Die Kameravorschau startet nur auf ausdrücklichen Knopfdruck, nicht schon
  beim Öffnen der Kamera-Entität.
- Video wird an die Stream-Integration von Home Assistant durchgereicht und von
  dieser Integration nicht aufgezeichnet.

## Sicherheit

- Die Gateway-Protokolle laufen unverschlüsselt per UDP/TCP im lokalen Netz.
  Eingehende SIP-Nachrichten werden nur von der IP-Adresse des Gateways
  angenommen; wer diese Adresse im LAN fälschen kann, könnte aber ein Klingeln
  vortäuschen. Schaltbefehle (Türöffner, Licht) sind verschlüsselt und ohne
  das SIP-Passwort nicht fälschbar.
- HomeServer- und SIP-Passwort liegen im Konfigurationseintrag von Home
  Assistant und werden nie ins Log geschrieben.
- Die intern genutzte Video-Stream-Adresse enthält ein zufälliges Token, das
  sich bei jedem Neustart ändert.

## Lizenz

PolyForm Strict License 1.0.0, siehe [LICENSE](LICENSE).
