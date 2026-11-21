import asyncio
from datetime import datetime, timezone
import json
import os
import struct
import time
import uuid
from zoneinfo import ZoneInfo
import httpx

LOCAL_TZ = ZoneInfo("America/Los_Angeles")
BACKUP_DIR = "/tmp/command_backups"
os.makedirs(BACKUP_DIR, exist_ok=True)

CONFIG_PATH = os.path.join(BACKUP_DIR, "perimeter_config.json")
STATE_PATH = os.path.join(BACKUP_DIR, "perimeter_state.json")

YOLINK_CLOUD_HTTP = "https://api.yosmart.com"
YOLINK_CLOUD_MQTT_HOST = "api.yosmart.com"
YOLINK_CLOUD_MQTT_PORT = 8003

DEFAULT_MOCK_ZONES: list[dict] = [
    {"entity_id": "mock.front_door", "name": "Front Entry Door", "state": "CLOSED", "battery": 100, "online": True},
    {"entity_id": "mock.garage_entry", "name": "Garage Entry Door", "state": "CLOSED", "battery": 100, "online": True},
    {"entity_id": "mock.back_slider", "name": "Back Patio Slider", "state": "CLOSED", "battery": 100, "online": True},
    {"entity_id": "mock.laundry_door", "name": "Laundry Side Door", "state": "CLOSED", "battery": 75, "online": True},
    {"entity_id": "mock.living_win_l", "name": "Living Rm Win L", "state": "CLOSED", "battery": 100, "online": True},
    {"entity_id": "mock.living_win_r", "name": "Living Rm Win R", "state": "CLOSED", "battery": 100, "online": True},
    {"entity_id": "mock.kitchen_win", "name": "Kitchen Window", "state": "CLOSED", "battery": 100, "online": True},
    {"entity_id": "mock.dining_win", "name": "Dining Window", "state": "CLOSED", "battery": 75, "online": True},
    {"entity_id": "mock.office_win", "name": "Office Window", "state": "CLOSED", "battery": 100, "online": True},
    {"entity_id": "mock.master_win", "name": "Master Bed Window", "state": "OPEN", "battery": 100, "online": True},
    {"entity_id": "mock.bdrm2_win", "name": "Bedroom 2 Window", "state": "CLOSED", "battery": 100, "online": True},
    {"entity_id": "mock.bdrm3_win", "name": "Bedroom 3 Window", "state": "CLOSED", "battery": 100, "online": True},
]


def _mqtt_encode_remaining_length(length: int) -> bytes:
    out = bytearray()
    while True:
        encoded = length % 128
        length //= 128
        if length > 0:
            encoded |= 0x80
        out.append(encoded)
        if length == 0:
            break
    return bytes(out)


def _mqtt_encode_str(s: str) -> bytes:
    raw = s.encode("utf-8")
    return struct.pack("!H", len(raw)) + raw


async def _mqtt_read_packet(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    first = await reader.readexactly(1)
    pkt_type = first[0]
    multiplier = 1
    rem_len = 0
    while True:
        b = (await reader.readexactly(1))[0]
        rem_len += (b & 0x7F) * multiplier
        if (b & 0x80) == 0:
            break
        multiplier *= 128
        if multiplier > 128 * 128 * 128:
            raise ValueError("Malformed MQTT remaining length")
    body = await reader.readexactly(rem_len) if rem_len > 0 else b""
    return pkt_type, body


def _battery_step_to_pct(raw_bat) -> int | None:
    if raw_bat is None:
        return None
    try:
        v = int(raw_bat)
        if v <= 4:
            return max(0, min(100, v * 25))
        return max(0, min(100, v))
    except Exception:
        return None


class PerimeterSecurityService:
    """
    Native Zero-Home-Assistant Perimeter Security Engine for NasonNation // Command.
    Supports:
      1. YoLink Cloud v2 HTTP + outbound MQTT (:8003) for YS1603-UC hubs (zero inbound ports).
      2. YoLink Local Hub (:1080 HTTP + :18080 LAN MQTT) for 100% air-gapped YS1606-UC operation.
      3. Deterministic 12-zone Mock Mode when unconfigured so UI & state machine work out of the box.
    """

    def __init__(self):
        self.config: dict = self._load_config()
        saved_state = self._load_state()
        self.alarm_state: str = saved_state.get("alarm_state", "disarmed")
        self.siren_state: str = saved_state.get("siren_state", "off")
        self.bypassed_zones: set[str] = set(saved_state.get("bypassed_zones", []))
        self.triggered_by: str | None = saved_state.get("triggered_by")
        raw_trig_zones = saved_state.get("triggered_zones")
        self.triggered_zones: list[str] = (
            list(raw_trig_zones)
            if isinstance(raw_trig_zones, list)
            else ([self.triggered_by] if self.triggered_by else [])
        )
        raw_trig_ids = saved_state.get("triggered_entity_ids")
        self.triggered_entity_ids: list[str] = list(raw_trig_ids) if isinstance(raw_trig_ids, list) else []
        self.triggered_at: float = float(saved_state.get("triggered_at") or 0.0)
        self.siren_silenced_after_timeout: bool = False
        self.armed_via_away: bool = bool(saved_state.get("armed_via_away", False))
        self.away_grace_until: float = 0.0
        self.last_auto_disarm_date: str = saved_state.get("last_auto_disarm_date", "")

        self.online: bool = False
        self.mqtt_live: bool = False
        self.last_error: str | None = None
        self.access_token: str = ""
        self.token_expires_at: float = 0.0
        self.home_id: str = ""

        self.zones_map: dict[str, dict] = {}
        self.sirens_map: dict[str, dict] = {}
        self.mock_zones: list[dict] = [dict(z) for z in DEFAULT_MOCK_ZONES]

        self._reload_event = asyncio.Event()
        self._cv_engine = None

    def bind_cv_engine(self, cv_engine):
        self._cv_engine = cv_engine

    def _load_config(self) -> dict:
        cfg = {
            "mode": os.getenv("YOLINK_MODE", "auto").strip().lower() or "auto",
            "uaid": os.getenv("YOLINK_UAID", "").strip(),
            "secret_key": os.getenv("YOLINK_SECRET_KEY", "").strip(),
            "local_hub_ip": os.getenv("YOLINK_LOCAL_HUB_IP", "").strip(),
            "local_client_id": os.getenv("YOLINK_LOCAL_CLIENT_ID", "").strip(),
            "local_client_secret": os.getenv("YOLINK_LOCAL_CLIENT_SECRET", "").strip(),
            "local_subnet_id": os.getenv("YOLINK_LOCAL_SUBNET_ID", "").strip(),
            "auto_disarm_enabled": True,
            "auto_disarm_time": os.getenv("PERIMETER_AUTO_DISARM_TIME", "05:00").strip() or "05:00",
            "siren_duration_sec": 300,
            "command_ui_url": os.getenv("COMMAND_UI_URL", "").strip()
            or f"http://{os.getenv('HOST_LAN_IP', '192.168.1.27').strip() or '192.168.1.27'}:3000",
            "zone_aliases": {},
            "zone_pins": {},
        }
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    disk = json.load(f)
                for k, v in disk.items():
                    if v is not None and (v != "" or not cfg.get(k)):
                        cfg[k] = v
            except Exception:
                pass
        return cfg

    def save_config(self, updates: dict) -> dict:
        for k in (
            "mode",
            "uaid",
            "secret_key",
            "local_hub_ip",
            "local_client_id",
            "local_client_secret",
            "local_subnet_id",
            "auto_disarm_enabled",
            "auto_disarm_time",
            "siren_duration_sec",
            "command_ui_url",
        ):
            if k in updates and updates[k] is not None:
                self.config[k] = updates[k].strip() if isinstance(updates[k], str) else updates[k]
        if "zone_aliases" in updates and isinstance(updates["zone_aliases"], dict):
            cleaned_aliases: dict[str, str] = {}
            for zid, alias in updates["zone_aliases"].items():
                if isinstance(zid, str) and isinstance(alias, str) and alias.strip():
                    cleaned_aliases[zid.strip()] = alias.strip()[:36]
            self.config["zone_aliases"] = cleaned_aliases

        if "zone_pins" in updates and isinstance(updates["zone_pins"], dict):
            cleaned_pins: dict[str, str] = {}
            for zid, pin in updates["zone_pins"].items():
                if isinstance(zid, str) and isinstance(pin, str) and pin.strip():
                    cleaned_pins[zid.strip()] = pin.strip()[:40]
            self.config["zone_pins"] = cleaned_pins

        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2)
        except Exception:
            pass
        if any(k in updates for k in ("mode", "uaid", "secret_key", "local_hub_ip", "local_client_id", "local_client_secret", "local_subnet_id")):
            self.access_token = ""
            self.token_expires_at = 0.0
            self.last_error = None
            self._reload_event.set()
        return self.get_public_config()

    def get_public_config(self) -> dict:
        return {
            "mode": self.config.get("mode", "auto"),
            "uaid": self.config.get("uaid", ""),
            "has_secret_key": bool(self.config.get("secret_key")),
            "local_hub_ip": self.config.get("local_hub_ip", ""),
            "local_client_id": self.config.get("local_client_id", ""),
            "has_local_secret": bool(self.config.get("local_client_secret")),
            "local_subnet_id": self.config.get("local_subnet_id", ""),
            "auto_disarm_enabled": bool(self.config.get("auto_disarm_enabled", True)),
            "auto_disarm_time": self.config.get("auto_disarm_time", "05:00"),
            "siren_duration_sec": int(self.config.get("siren_duration_sec") or 300),
            "command_ui_url": self.config.get("command_ui_url")
            or f"http://{os.getenv('HOST_LAN_IP', '192.168.1.27').strip() or '192.168.1.27'}:3000",
            "zone_aliases": dict(self.config.get("zone_aliases") or {}),
            "zone_pins": dict(self.config.get("zone_pins") or {}),
            "active_transport": self._resolve_transport(),
            "online": self.online,
            "mqtt_live": self.mqtt_live,
            "last_error": self.last_error,
        }

    def _load_state(self) -> dict:
        if os.path.exists(STATE_PATH):
            try:
                with open(STATE_PATH, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _format_triggered_summary(self) -> str | None:
        if not self.triggered_zones:
            return None
        if len(self.triggered_zones) == 1:
            return self.triggered_zones[0]
        if len(self.triggered_zones) == 2:
            return f"{self.triggered_zones[0]} + {self.triggered_zones[1]}"
        return f"{self.triggered_zones[0]} +{len(self.triggered_zones) - 1} MORE"

    def _persist_state(self):
        data = {
            "alarm_state": self.alarm_state,
            "siren_state": self.siren_state,
            "bypassed_zones": sorted(self.bypassed_zones),
            "triggered_by": self.triggered_by,
            "triggered_zones": list(self.triggered_zones),
            "triggered_entity_ids": list(self.triggered_entity_ids),
            "triggered_at": self.triggered_at,
            "armed_via_away": self.armed_via_away,
            "last_auto_disarm_date": self.last_auto_disarm_date,
        }
        try:
            with open(STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    def _resolve_transport(self) -> str:
        m = (self.config.get("mode") or "auto").lower()
        has_local = bool(
            self.config.get("local_hub_ip")
            and self.config.get("local_client_id")
            and self.config.get("local_client_secret")
        )
        has_cloud = bool(self.config.get("uaid") and self.config.get("secret_key"))
        if m == "mock":
            return "mock"
        if m == "local":
            return "yolink_local" if has_local else "mock"
        if m == "cloud":
            return "yolink_cloud" if has_cloud else "mock"
        if has_local:
            return "yolink_local"
        if has_cloud:
            return "yolink_cloud"
        return "mock"

    def _display_name(self, entity_id: str, raw_name: str) -> str:
        aliases = self.config.get("zone_aliases") or {}
        custom = aliases.get(entity_id)
        return custom.strip() if isinstance(custom, str) and custom.strip() else raw_name

    def _active_zones_list(self) -> list[dict]:
        transport = self._resolve_transport()
        if transport == "mock" or not self.zones_map:
            raw = self.mock_zones
        else:
            raw = sorted(self.zones_map.values(), key=lambda z: z["name"].lower())
        pins = self.config.get("zone_pins") or {}
        out = []
        for z in raw:
            zid = z["entity_id"]
            out.append(
                {
                    "entity_id": zid,
                    "name": self._display_name(zid, z["name"]),
                    "raw_name": z["name"],
                    "pin_id": pins.get(zid),
                    "state": z["state"],
                    "battery": z.get("battery"),
                    "online": bool(z.get("online", True)),
                    "bypassed": zid in self.bypassed_zones,
                    "updated_at": z.get("updated_at"),
                }
            )
        return out

    def get_snapshot(self) -> dict:
        transport = self._resolve_transport()
        is_mock = transport == "mock" or (not self.online and not self.zones_map)
        zones = self._active_zones_list()
        open_zones = [z["name"] for z in zones if z["state"] == "OPEN"]
        now = time.time()
        grace_rem = max(0, int(round(self.away_grace_until - now))) if self.alarm_state == "arming" else 0
        siren_names = [s["name"] for s in self.sirens_map.values()]
        siren_label = siren_names[0] if siren_names else "Master Bedroom Siren"

        return {
            "online": self.online if not is_mock else False,
            "is_mock": is_mock,
            "transport": "mock" if is_mock else transport,
            "mqtt_live": self.mqtt_live if not is_mock else False,
            "alarm_state": self.alarm_state,
            "siren_state": self.siren_state,
            "siren_name": siren_label,
            "breached_count": len(open_zones),
            "total_zones": len(zones),
            "open_zones": open_zones,
            "bypassed_zones": [z["name"] for z in zones if z["bypassed"]],
            "triggered_by": self.triggered_by,
            "triggered_zones": list(self.triggered_zones),
            "triggered_entity_ids": list(self.triggered_entity_ids),
            "auto_disarm_enabled": bool(self.config.get("auto_disarm_enabled", True)),
            "auto_disarm_time": self.config.get("auto_disarm_time", "05:00"),
            "away_grace_remaining": grace_rem,
            "last_error": self.last_error,
            "zones": zones,
        }

    async def arm_night(self, force: bool = False) -> dict:
        snap = self.get_snapshot()
        zones = snap["zones"]
        open_items = [z for z in zones if z["state"] == "OPEN"]
        unbypassed_open = [z for z in open_items if not z["bypassed"]]
        if unbypassed_open and not force:
            return {
                "success": False,
                "open_zones": [z["name"] for z in unbypassed_open],
                "message": f"Pre-arm check blocked: {len(unbypassed_open)} un-bypassed zone(s) currently OPEN.",
                "snapshot": snap,
            }
        if force:
            self.bypassed_zones |= {z["entity_id"] for z in open_items}
        self.alarm_state = "armed_night"
        self.armed_via_away = False
        self.triggered_by = None
        self.triggered_zones = []
        self.triggered_entity_ids = []
        self.triggered_at = 0.0
        self.siren_silenced_after_timeout = False
        self._persist_state()
        return {
            "success": True,
            "bypassed_zones": [z["name"] for z in zones if z["entity_id"] in self.bypassed_zones],
            "message": "Perimeter ARMED NIGHT.",
            "snapshot": self.get_snapshot(),
        }

    async def sync_with_away_mode(self, away: bool, away_armed_at: float):
        now = time.time()
        if away:
            self.armed_via_away = True
            if away_armed_at > now + 1.0:
                self.away_grace_until = away_armed_at
                if self.alarm_state != "triggered":
                    self.alarm_state = "arming"
                self._persist_state()
            else:
                self._lock_armed_away()
        else:
            self.away_grace_until = 0.0
            if self.armed_via_away or self.alarm_state in ("arming", "armed_away"):
                await self.disarm_and_silence(reason="Home Mode restored")

    def _lock_armed_away(self):
        zones = self._active_zones_list()
        self.bypassed_zones = {z["entity_id"] for z in zones if z["state"] == "OPEN"}
        self.alarm_state = "armed_away"
        self.away_grace_until = 0.0
        self.triggered_by = None
        self.triggered_zones = []
        self.triggered_entity_ids = []
        self.triggered_at = 0.0
        self.siren_silenced_after_timeout = False
        self._persist_state()

    async def disarm_and_silence(self, reason: str = "Operator disarm") -> dict:
        was_siren_on = self.siren_state == "on" or bool(self.sirens_map)
        still_open_ids = {z["entity_id"] for z in self._active_zones_list() if z["state"] == "OPEN"}
        self.alarm_state = "disarmed"
        self.siren_state = "off"
        self.triggered_by = None
        self.triggered_zones = []
        self.triggered_entity_ids = []
        self.triggered_at = 0.0
        self.siren_silenced_after_timeout = False
        # Preserve manual bypasses on still-OPEN windows so cracked windows stay bypassed until closed
        self.bypassed_zones &= still_open_ids
        self.armed_via_away = False
        self.away_grace_until = 0.0
        self._persist_state()

        if was_siren_on and self._resolve_transport() != "mock":
            asyncio.create_task(self._set_hardware_sirens(False))

        print(f"[PERIMETER] Disarmed & silenced ({reason}).")
        return {"success": True, "snapshot": self.get_snapshot()}

    async def _resolve_trigger_after_bypass(self):
        if self.alarm_state != "triggered":
            return
        zones = self._active_zones_list()
        id_to_name = {z["entity_id"]: z["name"] for z in zones}
        self.triggered_entity_ids = [
            zid for zid in self.triggered_entity_ids if zid not in self.bypassed_zones
        ]
        bypassed_names_lower = {
            id_to_name[zid].lower() for zid in self.bypassed_zones if zid in id_to_name
        }
        self.triggered_zones = [
            zn for zn in self.triggered_zones if zn.lower() not in bypassed_names_lower
        ]
        unbypassed_open = [z for z in zones if z["state"] == "OPEN" and z["entity_id"] not in self.bypassed_zones]
        if not self.triggered_zones and not unbypassed_open:
            was_siren_on = self.siren_state == "on" or bool(self.sirens_map)
            self.alarm_state = "armed_away" if self.armed_via_away else "armed_night"
            self.siren_state = "off"
            self.triggered_by = None
            self.triggered_at = 0.0
            self.siren_silenced_after_timeout = False
            if was_siren_on and self._resolve_transport() != "mock":
                asyncio.create_task(self._set_hardware_sirens(False))
        else:
            self.triggered_by = self._format_triggered_summary()

    async def set_zone_bypass(self, entity_id: str, bypass: bool | None = None) -> dict:
        zones = self._active_zones_list()
        target = next((z for z in zones if z["entity_id"] == entity_id), None)
        if not target:
            return {"error": "Zone not found", "snapshot": self.get_snapshot()}
        should_bypass = (entity_id not in self.bypassed_zones) if bypass is None else bool(bypass)
        if should_bypass:
            self.bypassed_zones.add(entity_id)
            await self._resolve_trigger_after_bypass()
        else:
            self.bypassed_zones.discard(entity_id)
        self._persist_state()
        return {
            "success": True,
            "entity_id": entity_id,
            "bypassed": entity_id in self.bypassed_zones,
            "snapshot": self.get_snapshot(),
        }

    async def bypass_all_open_zones(self) -> dict:
        zones = self._active_zones_list()
        open_ids = [z["entity_id"] for z in zones if z["state"] == "OPEN"]
        for zid in open_ids:
            self.bypassed_zones.add(zid)
        await self._resolve_trigger_after_bypass()
        self._persist_state()
        return {
            "success": True,
            "bypassed_count": len(open_ids),
            "snapshot": self.get_snapshot(),
        }

    async def toggle_mock_zone(self, entity_id: str) -> dict:
        target = next((z for z in self.mock_zones if z["entity_id"] == entity_id), None)
        if not target:
            return {"error": "Mock zone not found", "snapshot": self.get_snapshot()}
        new_state = "CLOSED" if target["state"] == "OPEN" else "OPEN"
        target["state"] = new_state
        target["updated_at"] = datetime.now(timezone.utc).isoformat()
        disp_name = self._display_name(target["entity_id"], target["name"])
        await self._handle_zone_transition(target["entity_id"], disp_name, new_state)
        return {"success": True, "snapshot": self.get_snapshot()}

    async def _handle_zone_transition(self, entity_id: str, zone_name: str, new_state: str):
        if new_state == "CLOSED" and entity_id in self.bypassed_zones:
            # Once a bypassed window/door is closed, automatically fold it back into the protected perimeter
            self.bypassed_zones.discard(entity_id)
            self._persist_state()
            return

        if new_state == "OPEN" and entity_id not in self.bypassed_zones:
            if self.alarm_state in ("armed_night", "armed_away"):
                prior_mode = self.alarm_state
                self.alarm_state = "triggered"
                self.siren_state = "on"
                self.triggered_zones = [zone_name]
                self.triggered_entity_ids = [entity_id]
                self.triggered_by = self._format_triggered_summary()
                self.triggered_at = time.time()
                self.siren_silenced_after_timeout = False
                self._persist_state()
                print(f"[PERIMETER BREACH] Zone '{zone_name}' opened while {prior_mode.upper()}! Firing siren & push.")
                if self._resolve_transport() != "mock":
                    asyncio.create_task(self._set_hardware_sirens(True))
                asyncio.create_task(self._dispatch_breach_alert(zone_name, prior_mode))
            elif (
                self.alarm_state == "triggered"
                and entity_id not in self.triggered_entity_ids
                and zone_name not in self.triggered_zones
            ):
                self.triggered_zones.append(zone_name)
                self.triggered_entity_ids.append(entity_id)
                self.triggered_by = self._format_triggered_summary()
                self.siren_state = "on"
                self.triggered_at = time.time()
                self.siren_silenced_after_timeout = False
                self._persist_state()
                multi_label = f"multi_zone_{len(self.triggered_zones)}_breached"
                print(
                    f"[PERIMETER MULTI-BREACH] Additional zone '{zone_name}' opened ({len(self.triggered_zones)} total)!"
                )
                if self._resolve_transport() != "mock":
                    asyncio.create_task(self._set_hardware_sirens(True))
                asyncio.create_task(self._dispatch_breach_alert(zone_name, multi_label))

    async def trigger_indoor_camera_alarm(
        self,
        camera_id: str,
        cam_friendly: str,
        summary_note: str = "",
    ) -> bool:
        """
        Fires the Master Bedroom Siren + 3D Breach state strictly when an INDOOR camera
        (tapo_1 = LOFT, tapo_2 = LIVING_ROOM) detects a verified person while ARMED AWAY.
        Driveway camera (annke_1) and ARMED NIGHT mode NEVER trigger the siren via cameras.
        """
        if camera_id not in ("tapo_1", "tapo_2"):
            return False

        entity_id = f"cam.{camera_id}"
        zone_label = f"Indoor Cam: {cam_friendly}"
        if entity_id in self.bypassed_zones:
            return False

        if self.alarm_state == "armed_away":
            prior_mode = self.alarm_state
            self.alarm_state = "triggered"
            self.siren_state = "on"
            self.triggered_zones = [zone_label]
            self.triggered_entity_ids = [entity_id]
            self.triggered_by = self._format_triggered_summary()
            self.triggered_at = time.time()
            self.siren_silenced_after_timeout = False
            self._persist_state()
            print(f"[PERIMETER INDOOR CAM BREACH] {zone_label} detected person while ARMED AWAY! Firing siren.")
            if self._resolve_transport() != "mock":
                asyncio.create_task(self._set_hardware_sirens(True))
            asyncio.create_task(
                self._dispatch_breach_alert(
                    zone_label,
                    prior_mode,
                    record_db_card=False,
                    extra_detail=summary_note,
                )
            )
            return True

        if (
            self.alarm_state == "triggered"
            and self.armed_via_away
            and entity_id not in self.triggered_entity_ids
            and zone_label not in self.triggered_zones
        ):
            self.triggered_zones.append(zone_label)
            self.triggered_entity_ids.append(entity_id)
            self.triggered_by = self._format_triggered_summary()
            self.siren_state = "on"
            self.triggered_at = time.time()
            self.siren_silenced_after_timeout = False
            self._persist_state()
            if self._resolve_transport() != "mock":
                asyncio.create_task(self._set_hardware_sirens(True))
            asyncio.create_task(
                self._dispatch_breach_alert(
                    zone_label,
                    f"multi_zone_{len(self.triggered_zones)}_breached",
                    record_db_card=False,
                    extra_detail=summary_note,
                )
            )
            return True

        return False

    async def _dispatch_breach_alert(
        self,
        zone_name: str,
        prior_mode: str,
        record_db_card: bool = True,
        extra_detail: str = "",
    ):
        try:
            from app.db.session import AsyncSessionLocal
            from app.db.models import DetectionEvent
            from app.services.cv_pipeline import SNAPSHOT_DIR
            from app.services.push_notifier import CAM_NAMES, get_command_ui_url, send_discord_files, send_tactical_push

            now_pt = datetime.now(LOCAL_TZ)
            stamp = now_pt.strftime("%H:%M:%S PT")
            ui_url = get_command_ui_url()
            note = f"🚨 [PERIMETER SIREN TRIGGERED] '{zone_name}' triggered while {prior_mode.upper()} at {stamp}."
            if extra_detail:
                note = f"{note} ({extra_detail[:110]})"

            files_data: list[tuple[str, bytes, str]] = []
            if self._cv_engine:
                for s in self._cv_engine.streams:
                    friendly = CAM_NAMES.get(s, s.upper())
                    jpg = self._cv_engine.capture_live_jpeg_bytes(s, stamp_label=f"BREACH: {zone_name.upper()} // {friendly}")
                    if jpg:
                        files_data.append((f"{s}_breach.jpg", jpg, "image/jpeg"))

            snap_id = ""
            mosaic_bytes: bytes | None = None
            if files_data:
                import cv2
                import numpy as np

                snap_id = f"perimeter_{int(time.time() * 1000)}.jpg"
                decoded_imgs = []
                for _, jpg_b, _ in files_data:
                    arr = np.frombuffer(jpg_b, dtype=np.uint8)
                    im = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                    if im is not None and im.size > 0:
                        decoded_imgs.append(im)

                full_bytes = files_data[0][1]
                crop_bytes = files_data[0][1]
                if len(decoded_imgs) >= 3:
                    # 16:9 (1280x720) 2x2 Tactical Quad Grid so mobile lock-screen notifications preview ALL 3 cameras cleanly!
                    tl_im = cv2.resize(decoded_imgs[0], (640, 360))
                    tr_im = cv2.resize(decoded_imgs[1], (640, 360))
                    bl_im = cv2.resize(decoded_imgs[2], (640, 360))

                    hud = np.full((360, 640, 3), (16, 12, 22), dtype=np.uint8)
                    cv2.rectangle(hud, (14, 14), (626, 346), (48, 48, 225), 2)
                    cv2.putText(hud, "PERIMETER BREACH", (34, 72), cv2.FONT_HERSHEY_SIMPLEX, 1.05, (60, 60, 255), 3, cv2.LINE_AA)
                    cv2.putText(hud, f"ZONE: {zone_name.upper()[:24]}", (34, 138), cv2.FONT_HERSHEY_SIMPLEX, 0.78, (245, 245, 250), 2, cv2.LINE_AA)
                    cv2.putText(hud, f"MODE: {prior_mode.upper()[:22]}", (34, 194), cv2.FONT_HERSHEY_SIMPLEX, 0.72, (120, 210, 255), 2, cv2.LINE_AA)
                    cv2.putText(hud, "SIREN: MASTER BDRM ACTIVE", (34, 250), cv2.FONT_HERSHEY_SIMPLEX, 0.72, (80, 80, 255), 2, cv2.LINE_AA)
                    cv2.putText(hud, f"TIME: {now_pt.strftime('%Y-%m-%d %H:%M:%S')}", (34, 306), cv2.FONT_HERSHEY_SIMPLEX, 0.68, (180, 190, 205), 2, cv2.LINE_AA)

                    top_row = np.hstack([tl_im, tr_im])
                    bot_row = np.hstack([bl_im, hud])
                    mosaic = np.vstack([top_row, bot_row])
                    cv2.line(mosaic, (640, 0), (640, 720), (38, 38, 220), 3)
                    cv2.line(mosaic, (0, 360), (1280, 360), (38, 38, 220), 3)
                    ok_f, enc_f = cv2.imencode(".jpg", mosaic, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
                    if ok_f:
                        full_bytes = enc_f.tobytes()
                        mosaic_bytes = full_bytes
                    thumb = cv2.resize(mosaic, (640, 360))
                    ok_c, enc_c = cv2.imencode(".jpg", thumb, [int(cv2.IMWRITE_JPEG_QUALITY), 86])
                    if ok_c:
                        crop_bytes = enc_c.tobytes()
                elif len(decoded_imgs) == 2:
                    l_im = cv2.resize(decoded_imgs[0], (640, 360))
                    r_im = cv2.resize(decoded_imgs[1], (640, 360))
                    mosaic = np.hstack([l_im, r_im])
                    cv2.line(mosaic, (640, 0), (640, 360), (38, 38, 220), 3)
                    ok_f, enc_f = cv2.imencode(".jpg", mosaic, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
                    if ok_f:
                        full_bytes = enc_f.tobytes()
                        crop_bytes = full_bytes
                        mosaic_bytes = full_bytes

                try:
                    with open(f"{SNAPSHOT_DIR}/full_{snap_id}", "wb") as f_full:
                        f_full.write(full_bytes)
                    with open(f"{SNAPSHOT_DIR}/crop_{snap_id}", "wb") as f_crop:
                        f_crop.write(crop_bytes)
                except OSError:
                    snap_id = ""

            if record_db_card:
                async with AsyncSessionLocal() as session:
                    ev = DetectionEvent(
                        timestamp=datetime.now(timezone.utc),
                        camera_id="annke_1",
                        motion_slot="Perimeter",
                        category="person",
                        sub_type=f"🚨 PERIMETER: {zone_name.upper()}"[:120],
                        full_label="Perimeter_SENSOR_BREACH",
                        confidence=1.0,
                        bbox_x=0.0,
                        bbox_y=0.0,
                        bbox_w=0.0,
                        bbox_h=0.0,
                        attributes=(f"{snap_id}|{note}" if snap_id else f"|{note}")[:250],
                        ai_notes=note,
                    )
                    session.add(ev)
                    await session.commit()

            # Do not spam @everyone push alerts on simulated Mock clicks; real YoLink breaches always alert
            if self._resolve_transport() == "mock":
                return

            if files_data:
                # Put the 16:9 3-camera stitched quad mosaic FIRST (files[0]) so Android/iOS lock-screen
                # notifications display ALL 3 cameras in the single preview banner!
                discord_attachments = (
                    [("00_all_cameras_mosaic.jpg", mosaic_bytes, "image/jpeg"), *files_data]
                    if mosaic_bytes
                    else files_data
                )
                breach_components = [
                    {
                        "type": 1,
                        "components": [
                            {
                                "type": 2,
                                "style": 4,
                                "label": "DISARM & SILENCE",
                                "custom_id": "perim_disarm",
                                "emoji": {"name": "🔕"},
                            },
                            {
                                "type": 2,
                                "style": 1,
                                "label": "FORCE RE-ARM NIGHT",
                                "custom_id": "perim_arm_force",
                                "emoji": {"name": "🌙"},
                            },
                            {
                                "type": 2,
                                "style": 2,
                                "label": "LIVE 3-CAM SNAP",
                                "custom_id": "perim_snap_all",
                                "emoji": {"name": "📸"},
                            },
                        ],
                    }
                ]
                await send_discord_files(
                    title=f"🚨 PERIMETER BREACH // {zone_name.upper()}",
                    lines=[
                        f"Sensor:   {zone_name} (OPEN)",
                        f"Posture:  {prior_mode.upper()} -> SIREN ACTIVE",
                        f"Time:     {now_pt.strftime('%Y-%m-%d %H:%M:%S %Z')}",
                        f"Open App: {ui_url}",
                    ],
                    files_data=discord_attachments,
                    silent=False,
                    components=breach_components,
                )
            else:
                await send_tactical_push(
                    title=f"🚨 PERIMETER BREACH // {zone_name.upper()}",
                    message=f"{zone_name} opened while {prior_mode.upper()} — Master Bedroom Siren ACTIVE!",
                    priority="urgent",
                    tags="rotating_light,warning",
                    bypass_mute=True,
                )
        except Exception as e:
            print(f"[PERIMETER ALERT ERROR] {e}")

    def _http_base_and_creds(self) -> tuple[str, str, str]:
        t = self._resolve_transport()
        if t == "yolink_local":
            ip = self.config["local_hub_ip"].replace("http://", "").replace("https://", "").split(":")[0]
            return f"http://{ip}:1080", self.config["local_client_id"], self.config["local_client_secret"]
        return YOLINK_CLOUD_HTTP, self.config["uaid"], self.config["secret_key"]

    async def _ensure_token(self, client: httpx.AsyncClient) -> str:
        now = time.time()
        if self.access_token and now < self.token_expires_at - 60:
            return self.access_token
        base_url, cid, csec = self._http_base_and_creds()
        resp = await client.post(
            f"{base_url}/open/yolink/token",
            data={"grant_type": "client_credentials", "client_id": cid, "client_secret": csec},
        )
        resp.raise_for_status()
        body = resp.json()
        tok = body.get("access_token")
        if not tok:
            raise RuntimeError(body.get("msg") or body.get("error_description") or "YoLink token error")
        self.access_token = tok
        self.token_expires_at = now + float(body.get("expires_in") or 7200)
        return tok

    async def _bddp_call(self, client: httpx.AsyncClient, payload: dict) -> dict:
        base_url, _, _ = self._http_base_and_creds()
        tok = await self._ensure_token(client)
        body = {"time": int(time.time() * 1000), **payload}
        resp = await client.post(
            f"{base_url}/open/yolink/v2/api",
            headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
            json=body,
        )
        resp.raise_for_status()
        return resp.json()

    async def _set_hardware_sirens(self, turn_on: bool):
        if not self.sirens_map:
            return
        async with httpx.AsyncClient(timeout=10.0) as client:
            for sid, sdev in list(self.sirens_map.items()):
                dev_token = sdev.get("token", "")
                # Try YoLink v2 Siren alarm dict format first, then fallback to switch-style open/close
                for candidate_params in (
                    {"state": {"alarm": bool(turn_on)}},
                    {"state": "open" if turn_on else "close"},
                ):
                    try:
                        res = await self._bddp_call(
                            client,
                            {
                                "method": "Siren.setState",
                                "targetDevice": sid,
                                "token": dev_token,
                                "params": candidate_params,
                            },
                        )
                        if str(res.get("code", "")) == "000000":
                            self.siren_state = "on" if turn_on else "off"
                            break
                    except Exception as e:
                        print(f"[PERIMETER SIREN WARNING] {e}")

    async def _sync_yolink_devices_and_states(self):
        async with httpx.AsyncClient(timeout=12.0) as client:
            await self._ensure_token(client)
            if self._resolve_transport() == "yolink_cloud" and not self.home_id:
                gen = await self._bddp_call(client, {"method": "Home.getGeneralInfo"})
                if str(gen.get("code")) == "000000":
                    self.home_id = str((gen.get("data") or {}).get("id") or "")

            dlist = await self._bddp_call(client, {"method": "Home.getDeviceList"})
            if str(dlist.get("code")) != "000000":
                raise RuntimeError(f"Home.getDeviceList code={dlist.get('code')} desc={dlist.get('desc')}")

            devices = (dlist.get("data") or {}).get("devices") or []
            new_zones: dict[str, dict] = {}
            new_sirens: dict[str, dict] = {}

            for d in devices:
                did = str(d.get("deviceId") or "")
                dtype = str(d.get("type") or "")
                dname = str(d.get("name") or did)
                dtok = str(d.get("token") or "")
                if not did:
                    continue
                if dtype in ("DoorSensor", "GarageDoor") or "door" in dtype.lower() or "window" in dname.lower():
                    if dtype == "Hub" or dtype == "SpeakerHub" or dtype == "Siren":
                        pass
                    elif dtype in ("DoorSensor",):
                        prev = self.zones_map.get(did, {})
                        new_zones[did] = {
                            "entity_id": did,
                            "name": dname,
                            "token": dtok,
                            "type": dtype,
                            "state": prev.get("state", "CLOSED"),
                            "battery": prev.get("battery", 100),
                            "online": prev.get("online", True),
                            "updated_at": prev.get("updated_at"),
                        }
                if dtype == "Siren" or "siren" in dtype.lower():
                    prev_s = self.sirens_map.get(did, {})
                    new_sirens[did] = {
                        "entity_id": did,
                        "name": dname,
                        "token": dtok,
                        "type": dtype,
                        "state": prev_s.get("state", "off"),
                    }

            # Query current live state for each discovered door/window sensor & siren
            for did, z in new_zones.items():
                try:
                    st_res = await self._bddp_call(
                        client,
                        {"method": f"{z['type']}.getState", "targetDevice": did, "token": z["token"], "params": {}},
                    )
                    if str(st_res.get("code")) == "000000":
                        d_obj = st_res.get("data") or {}
                        z["online"] = bool(d_obj.get("online", True))
                        s_obj = d_obj.get("state")
                        if isinstance(s_obj, dict):
                            raw_st = str(s_obj.get("state") or "closed").lower()
                            z["state"] = "OPEN" if raw_st == "open" else "CLOSED"
                            bat = _battery_step_to_pct(s_obj.get("battery"))
                            if bat is not None:
                                z["battery"] = bat
                        elif isinstance(s_obj, str):
                            z["state"] = "OPEN" if s_obj.lower() == "open" else "CLOSED"
                        z["updated_at"] = d_obj.get("reportAt") or datetime.now(timezone.utc).isoformat()
                    await asyncio.sleep(0.18)
                except Exception:
                    pass

            for sid, s in new_sirens.items():
                try:
                    st_res = await self._bddp_call(
                        client,
                        {"method": "Siren.getState", "targetDevice": sid, "token": s["token"], "params": {}},
                    )
                    if str(st_res.get("code")) == "000000":
                        s_obj = (st_res.get("data") or {}).get("state")
                        if isinstance(s_obj, dict):
                            raw_st = str(s_obj.get("state") or "").lower()
                            is_on = bool(s_obj.get("alarm")) or raw_st in ("alert", "open", "on")
                        else:
                            is_on = str(s_obj or "").lower() in ("alert", "open", "on")
                        s["state"] = "on" if is_on else "off"
                        self.siren_state = s["state"]
                except Exception:
                    pass

            self.zones_map = new_zones
            self.sirens_map = new_sirens
            self.online = True
            self.last_error = None

    async def _ingest_mqtt_report(self, topic: str, payload_bytes: bytes):
        try:
            msg = json.loads(payload_bytes.decode("utf-8", errors="ignore"))
        except Exception:
            return
        parts = topic.split("/")
        topic_dev_id = parts[2] if len(parts) >= 4 else ""
        did = str(msg.get("deviceId") or topic_dev_id)
        event_name = str(msg.get("event") or msg.get("method") or "")
        data = msg.get("data") or {}

        if did in self.zones_map:
            z = self.zones_map[did]
            raw_state = data.get("state")
            new_st = None
            if isinstance(raw_state, dict):
                s_val = str(raw_state.get("state") or "").lower()
                if s_val in ("open", "closed"):
                    new_st = "OPEN" if s_val == "open" else "CLOSED"
                bat = _battery_step_to_pct(raw_state.get("battery"))
                if bat is not None:
                    z["battery"] = bat
            elif isinstance(raw_state, str):
                s_val = raw_state.lower()
                if s_val in ("open", "closed"):
                    new_st = "OPEN" if s_val == "open" else "CLOSED"
            bat_top = _battery_step_to_pct(data.get("battery"))
            if bat_top is not None:
                z["battery"] = bat_top
            z["online"] = True
            z["updated_at"] = datetime.now(timezone.utc).isoformat()
            if new_st and new_st != z["state"]:
                z["state"] = new_st
                await self._handle_zone_transition(did, self._display_name(did, z["name"]), new_st)
        elif did in self.sirens_map or "Siren" in event_name:
            raw_state = data.get("state")
            if isinstance(raw_state, dict):
                is_on = bool(raw_state.get("alarm")) or str(raw_state.get("state") or "").lower() in ("alert", "open", "on")
            else:
                is_on = str(raw_state or "").lower() in ("alert", "open", "on")
            self.siren_state = "on" if is_on else "off"

    async def _run_mqtt_stream(self):
        transport = self._resolve_transport()
        if transport == "yolink_local":
            host = self.config["local_hub_ip"].replace("http://", "").replace("https://", "").split(":")[0]
            port = 18080
            username = self.config["local_client_id"]
            password = self.config["local_client_secret"]
            subnet = self.config.get("local_subnet_id") or "+"
            topic = f"ylsubnet/{subnet}/+/report"
        else:
            host = YOLINK_CLOUD_MQTT_HOST
            port = YOLINK_CLOUD_MQTT_PORT
            async with httpx.AsyncClient(timeout=10.0) as client:
                username = await self._ensure_token(client)
            password = ""
            if not self.home_id:
                raise RuntimeError("Missing YoLink homeId for MQTT subscription")
            topic = f"yl-home/{self.home_id}/+/report"

        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=10.0)
        try:
            client_id = f"bossman-{uuid.uuid4().hex[:10]}"
            flags = 0x82 | (0x40 if password else 0x00)
            var_hdr = b"\x00\x04MQTT\x04" + bytes([flags]) + struct.pack("!H", 45)
            payload = _mqtt_encode_str(client_id) + _mqtt_encode_str(username)
            if password:
                payload += _mqtt_encode_str(password)
            connect_pkt = b"\x10" + _mqtt_encode_remaining_length(len(var_hdr) + len(payload)) + var_hdr + payload
            writer.write(connect_pkt)
            await writer.drain()

            pkt_type, body = await asyncio.wait_for(_mqtt_read_packet(reader), timeout=8.0)
            if (pkt_type >> 4) != 2 or len(body) < 2 or body[1] != 0:
                rc = body[1] if len(body) >= 2 else -1
                raise RuntimeError(f"MQTT CONNACK rejected (code={rc})")

            sub_body = struct.pack("!H", 1) + _mqtt_encode_str(topic) + b"\x00"
            sub_pkt = b"\x82" + _mqtt_encode_remaining_length(len(sub_body)) + sub_body
            writer.write(sub_pkt)
            await writer.drain()

            self.mqtt_live = True
            self.online = True
            print(f"[PERIMETER MQTT] Connected to {host}:{port} ({topic})")

            last_ping = time.time()
            last_resync = time.time()
            while not self._reload_event.is_set():
                now = time.time()
                if now - last_ping >= 28.0:
                    writer.write(b"\xc0\x00")
                    await writer.drain()
                    last_ping = now
                if now - last_resync >= 180.0:
                    last_resync = now
                    asyncio.create_task(self._safe_background_resync())

                try:
                    pkt_type, body = await asyncio.wait_for(_mqtt_read_packet(reader), timeout=2.0)
                except asyncio.TimeoutError:
                    continue

                high_nib = pkt_type >> 4
                if high_nib == 3 and len(body) >= 2:  # PUBLISH
                    qos = (pkt_type >> 1) & 0x03
                    t_len = struct.unpack("!H", body[:2])[0]
                    idx = 2 + t_len
                    pub_topic = body[2:idx].decode("utf-8", errors="ignore")
                    if qos > 0 and len(body) >= idx + 2:
                        pkt_id = body[idx : idx + 2]
                        idx += 2
                        writer.write(b"\x40\x02" + pkt_id)
                        await writer.drain()
                    await self._ingest_mqtt_report(pub_topic, body[idx:])
        finally:
            self.mqtt_live = False
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _safe_background_resync(self):
        try:
            await self._sync_yolink_devices_and_states()
        except Exception:
            pass

    async def watchdog_and_mqtt_worker(self):
        asyncio.create_task(self._timers_loop())
        while True:
            self._reload_event.clear()
            transport = self._resolve_transport()
            if transport == "mock":
                self.online = False
                self.mqtt_live = False
                await self._reload_event.wait()
                continue

            try:
                await self._sync_yolink_devices_and_states()
                await self._run_mqtt_stream()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.mqtt_live = False
                self.online = bool(self.zones_map)
                self.last_error = str(e)[:180]
                print(f"[PERIMETER HUB WARNING] {e}")
                try:
                    await asyncio.wait_for(self._reload_event.wait(), timeout=15.0)
                except asyncio.TimeoutError:
                    pass

    async def _timers_loop(self):
        """
        Enforces:
        1. Away Mode exit-grace lock-in (arming -> armed_away once countdown hits 0).
        2. 05:00 AM PT daily auto-disarm for ARMED NIGHT (never disarms ARMED AWAY during vacations!).
        3. 300s (5-min) siren auto-cutout safety timer.
        """
        while True:
            try:
                now = time.time()
                # 1. Away Mode exit grace completion
                if self.alarm_state == "arming" and self.away_grace_until > 0 and now >= self.away_grace_until:
                    self._lock_armed_away()
                    print("[PERIMETER] Away Mode exit grace expired -> ARMED AWAY locked.")

                # 2. 05:00 AM PT Daily Auto-Disarm (strictly for ARMED NIGHT, never ARMED AWAY)
                if bool(self.config.get("auto_disarm_enabled", True)) and self.alarm_state == "armed_night":
                    now_pt = datetime.now(LOCAL_TZ)
                    today_str = now_pt.strftime("%Y-%m-%d")
                    target_hm = (self.config.get("auto_disarm_time") or "05:00").strip()
                    if now_pt.strftime("%H:%M") == target_hm and self.last_auto_disarm_date != today_str:
                        self.last_auto_disarm_date = today_str
                        await self.disarm_and_silence(reason=f"Daily {target_hm} PT Night Auto-Disarm")

                # 3. 300s Siren Auto-Cutout (silences horn after 5m while keeping UI TRIGGERED red until cleared)
                max_siren = int(self.config.get("siren_duration_sec") or 300)
                if (
                    self.alarm_state == "triggered"
                    and self.siren_state == "on"
                    and self.triggered_at > 0
                    and (now - self.triggered_at) >= max_siren
                    and not self.siren_silenced_after_timeout
                ):
                    self.siren_silenced_after_timeout = True
                    self.siren_state = "off"
                    self._persist_state()
                    if self._resolve_transport() != "mock":
                        asyncio.create_task(self._set_hardware_sirens(False))
                    print(f"[PERIMETER] Siren auto-silenced after {max_siren}s timeout (alarm remains TRIGGERED).")
            except Exception as e:
                print(f"[PERIMETER TIMER ERROR] {e}")
            await asyncio.sleep(1.0)


perimeter_service = PerimeterSecurityService()
