# Gira TKS-IP Gateway for Home Assistant

🇩🇪 [Deutsche Version](README.de.md)

Custom integration for [Home Assistant](https://www.home-assistant.io/) that
talks directly to a **Gira TKS-IP Gateway** (door communication system). No
HomeServer, bridge or cloud service is needed.

Not affiliated with or endorsed by Gira. Use at your own risk.

## Features

- **Door calls and key events** pushed by the gateway over TCP (the HomeServer
  interface, default port 50050): which station and button rang, light key,
  door opener key, call accepted / ended.
- **SIP participant** (optional): ring detection, live H.264 video of the door
  station during a call, answer and decline buttons.
- **Camera preview without a ring**: switch the door station camera on on
  demand, like the camera key of the TKS-Communicator.
- **Door opener, light and switching action** (optional): trigger them from
  Home Assistant over the gateway's encrypted SIP control channel. The switching
  action is the one assigned to function key 1 of the SIP participant, for
  example a switch actuator for a garage door. The button only reports success
  when the gateway acknowledges the command.

Audio and two-way talk are not supported. Calls without video are reported as
ringing but cannot be answered in Home Assistant.

The integration is available in English and German; Home Assistant shows names
and states in the configured language.

## Installation

### HACS / Marketplace

[![Open in HACS or Marketplace](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=kschwar&repository=ha-gira-tks-ip-gateway&category=integration)

The integration is not yet included in the default catalog. Use the button
above to add it as a custom repository in HACS or Marketplace.

In Home Assistant 2026.11 and later, open **Settings → Marketplace** and add
`https://github.com/kschwar/ha-gira-tks-ip-gateway` as a custom repository,
category **Integration**. Install it and restart Home Assistant.

For older Home Assistant versions with HACS:

1. HACS → ⋮ → **Custom repositories** → add this repository's URL, type
   **Integration**.
2. Install **Gira TKS-IP Gateway** and restart Home Assistant.

### Manual

Copy `custom_components/gira_tks` to `/config/custom_components/gira_tks` and
restart Home Assistant.

## Setup

### 1. Gateway: HomeServer connection

In the **Gira TKS Assistant** (German UI), open **Anlage einrichten →
HomeServer / FacilityServer einrichten → HomeServer-Verbindungsdaten** and
enter

- the IP address of your Home Assistant system,
- port `50050`,
- a user name and password of your choice.

Then add the integration in Home Assistant (**Settings → Devices & services →
Add integration → Gira TKS-IP Gateway**) and enter the gateway's IPv4 address,
the port and the same user name and password.

**Discovery:** Home Assistant detects the gateway on the network by its Gira MAC
address and shows it under **Settings → Devices & services → Discovered**. The
integration confirms it with the search request of the Gira TKS-IP-Gateway-Finder
(UDP broadcast to port 31337, read-only) and fills in the IP address. When you add
the integration manually, it also searches and suggests the gateway it finds.
Discovery only finds gateways in the same network segment; the user name and
password are always entered by hand. If the gateway gets a new IP address via
DHCP, discovery updates the configured entry.

### 2. Optional: SIP, video and switching buttons

Open the integration's **Configure** dialog:

| Option | Meaning |
|---|---|
| Enable SIP participant | Registers a SIP participant at the gateway (UDP 5060). |
| SIP user name / password | Credentials of a TKS-Communicator participant created in the Gira TKS Assistant under **TKS-Communicator einrichten**. Not the HomeServer credentials from step 1. |
| Local IP address | Address announced to the gateway. Leave empty for automatic detection. |
| Local SIP port / H.264 RTP port | Local UDP ports, default 5062 and 8282. The gateway must be able to reach them. |
| Answer calls automatically | Answer video calls automatically (off by default). |
| Show switching buttons | Adds the **Door opener**, **Light** and **Switching action** buttons (off by default). |

Notes:

- Use a participant that is not logged in elsewhere at the same time (for
  example a TKS-Communicator on a PC with the same account). Each concurrent
  login counts against the gateway's participant licence. While the same
  participant is also logged in with the TKS-Communicator, the gateway rejects
  door opener and light commands from Home Assistant (it answers `0402`).
- **Door opener and light** only work with a SIP account the gateway allows to
  switch. If the gateway does not answer or the SIP password is wrong, the
  button shows an error with the reason.
- **Switching action** triggers the switching action assigned to **function
  key 1** of this TKS-Communicator participant in the Gira TKS Assistant
  (**Function key**; selecting it under switching actions alone is not
  enough). Without that assignment the gateway rejects the command. Which
  devices react, for example one or more switch actuators, is set in the
  Assistant. Further function keys are not supported yet.
- Saving the options reloads the integration.

## Entities

Entity IDs depend on the entry name and the language at setup; the exact ID is
shown in each entity's settings.

| Entity | Type | Description |
|---|---|---|
| Ringing | binary sensor | On while an incoming SIP call is ringing. |
| Door call | event | Event types `ring` (door call), `internal_call`, `floor_call`; station and button as attributes. |
| Light key, Door opener key, Call accepted, Call ended | event | Key events reported by the gateway. |
| Last key | sensor | Last key event as text. |
| Last key event, Last door call | sensor | Timestamps, details as attributes. |
| TKS call | sensor | State of the SIP call. |
| TCP listener, SIP registration, Last record, Last SIP message | sensor | Diagnostics. |
| Video RTP packets | sensor | Video diagnostics; disabled by default, enable it in the entity settings when needed. |
| Door station | camera | Video of the current call. |
| Door station (camera preview) | camera | On-demand camera preview. |
| Answer call, Decline / end call | button | Call control. |
| Start / stop camera preview | button | Camera preview (stops by itself after 30 s). |
| Restart SIP connection | button | Re-register the SIP participant. |
| Door opener, Light, Switching action | button | Only with the switching buttons option enabled. |

## Events (event bus)

| Event | Data |
|---|---|
| `gira_tks_door_call` | `call_type` (`door_call`, `internal_call`, `floor_call`), `bus_address`, `aufsatz`, `tastennummer`, `sender_type`, `received_at` |
| `gira_tks_key_event` | `key_event` (`light`, `door_opener`, `call_accepted`, `call_ended`), `communicator_number`, `bus_address`, `target_address` |
| `gira_tks_ringing` | `call_state`, `signal`, `video_available` (SIP call started ringing) |
| `gira_tks_frame_received` | Every valid record from the gateway (diagnostics) |

All events contain `entry_id`. `aufsatz` and `tastennummer` are Gira's terms for
the button module and button number of a station.

### Example: notification on a door call

```yaml
triggers:
  - trigger: event
    event_type: gira_tks_door_call
    event_data:
      call_type: door_call
actions:
  - action: notify.mobile_app_your_phone
    data:
      message: "Someone is at the door."
```

### Example: open the door

```yaml
actions:
  - action: button.press
    target:
      entity_id: button.YOUR_DOOR_OPENER_BUTTON  # ID from the entity settings
```

## Network

| Direction | Port | Purpose |
|---|---|---|
| Gateway → Home Assistant | TCP 50050 | Door calls and key events |
| Home Assistant ↔ Gateway | UDP 5060 (gateway) / 5062 (HA) | SIP |
| Gateway → Home Assistant | UDP 8282 | H.264 video (RTP) |
| Home Assistant ↔ Gateway | UDP 31337 | Gateway search (broadcast, only during discovery and setup) |

## Limits

- No audio, no two-way talk.
- The gateway limits how long the camera may run; after that it shows a
  temporary block (`0x03`) for a while.
- The camera preview only starts on an explicit button press, not when the
  camera entity is opened.
- Video is passed through to Home Assistant's stream integration and is not
  recorded by this integration.

## Security

- The gateway protocols are plain UDP/TCP on the local network. Incoming SIP
  messages are only accepted from the gateway's IP address, but anyone on the
  LAN who can spoof that address could fake a ring. Switching commands (door
  opener, light, switching action) are encrypted and cannot be forged without the SIP password.
- The HomeServer and SIP passwords are stored in Home Assistant's config entry
  and are never logged.
- The video stream URL used internally by Home Assistant contains a random
  token that changes on every restart.

## License

MIT License, see [LICENSE](LICENSE).
