DOMAIN = "gira_tks"
VERSION = "1.0.1"
PLATFORMS = ["sensor", "binary_sensor", "camera", "button", "event"]

CONF_GATEWAY_HOST = "gateway_host"
CONF_LISTEN_PORT = "listen_port"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"

DEFAULT_LISTEN_PORT = 50050
CONF_SIP_ENABLED = "sip_enabled"
CONF_SIP_USERNAME = "sip_username"
CONF_SIP_PASSWORD = "sip_password"
CONF_SIP_PORT = "sip_port"
CONF_LOCAL_IP = "local_ip"
CONF_MEDIA_PORT = "media_port"
CONF_AUTO_ANSWER = "auto_answer"
DEFAULT_SIP_PORT = 5062
DEFAULT_LOCAL_IP = ""
DEFAULT_MEDIA_PORT = 8282
GATEWAY_PREVIEW_DURATION = 30
GATEWAY_RING_TIMEOUT = 120
EVENT_FRAME_RECEIVED = "gira_tks_frame_received"


def sip_updated_signal(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_sip_updated"


EVENT_SIP_MESSAGE = "gira_tks_sip_message"
EVENT_TKS_RINGING = "gira_tks_ringing"

ATTR_FRAME_TYPE = "frame_type"
ATTR_RECORD = "record"
ATTR_RECEIVED_AT = "received_at"

CONF_SIP_SWITCHING = "tks_door_opener_enabled"

EVENT_KEY_EVENT = "gira_tks_key_event"
EVENT_DOOR_CALL = "gira_tks_door_call"
