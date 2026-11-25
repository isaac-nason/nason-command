import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
import random
import re
import struct
import time
from urllib.parse import urlparse, unquote, quote
import xml.etree.ElementTree as ET
import httpx
import numpy as np

MEDIAMTX_CONFIG_PATH = "/tmp/mediamtx.yml"
HW_STATE_PATH = "/tmp/command_backups/camera_hardware_state.json"

# Maps clean UI/API modes <-> Annke/Hikvision ISAPI SupplementLight enum values
LIGHT_MODE_TO_ISAPI = {
    "ir": "irLight",
    "smart": "eventIntelligence",
    "color": "colorVuWhiteLight",
    "off": "close",
}
ISAPI_TO_LIGHT_MODE = {v: k for k, v in LIGHT_MODE_TO_ISAPI.items()}

PRESET_VOICE_MESSAGES: dict[str, str] = {
    "leave_door": "Hello! Please leave the package by the front door. Thank you!",
    "coming_out": "Hello! Give me just one moment, I am coming right out.",
    "no_soliciting": "No soliciting, thank you. Have a good day.",
    "dinner_ready": "Hey guys, dinner is ready! Come on down.",
    "check_in": "Hey! Everything good in there?",
    "warning": "Warning. You are on security camera and being recorded.",
}

GO2RTC_API_BASE = "http://127.0.0.1:1984"
GO2RTC_CFG_PATH = "/tmp/go2rtc_talk.yaml"


def _strip_xml_ns(xml_text: str) -> str:
    return re.sub(r'\sxmlns="[^"]+"', "", xml_text or "", count=5)


# Pure-Python FIPS-197 AES-128 inverse cipher tables (zero external crypto pip packages required)
_AES_SBOX = (
    0x63, 0x7C, 0x77, 0x7B, 0xF2, 0x6B, 0x6F, 0xC5, 0x30, 0x01, 0x67, 0x2B, 0xFE, 0xD7, 0xAB, 0x76,
    0xCA, 0x82, 0xC9, 0x7D, 0xFA, 0x59, 0x47, 0xF0, 0xAD, 0xD4, 0xA2, 0xAF, 0x9C, 0xA4, 0x72, 0xC0,
    0xB7, 0xFD, 0x93, 0x26, 0x36, 0x3F, 0xF7, 0xCC, 0x34, 0xA5, 0xE5, 0xF1, 0x71, 0xD8, 0x31, 0x15,
    0x04, 0xC7, 0x23, 0xC3, 0x18, 0x96, 0x05, 0x9A, 0x07, 0x12, 0x80, 0xE2, 0xEB, 0x27, 0xB2, 0x75,
    0x09, 0x83, 0x2C, 0x1A, 0x1B, 0x6E, 0x5A, 0xA0, 0x52, 0x3B, 0xD6, 0xB3, 0x29, 0xE3, 0x2F, 0x84,
    0x53, 0xD1, 0x00, 0xED, 0x20, 0xFC, 0xB1, 0x5B, 0x6A, 0xCB, 0xBE, 0x39, 0x4A, 0x4C, 0x58, 0xCF,
    0xD0, 0xEF, 0xAA, 0xFB, 0x43, 0x4D, 0x33, 0x85, 0x45, 0xF9, 0x02, 0x7F, 0x50, 0x3C, 0x9F, 0xA8,
    0x51, 0xA3, 0x40, 0x8F, 0x92, 0x9D, 0x38, 0xF5, 0xBC, 0xB6, 0xDA, 0x21, 0x10, 0xFF, 0xF3, 0xD2,
    0xCD, 0x0C, 0x13, 0xEC, 0x5F, 0x97, 0x44, 0x17, 0xC4, 0xA7, 0x7E, 0x3D, 0x64, 0x5D, 0x19, 0x73,
    0x60, 0x81, 0x4F, 0xDC, 0x22, 0x2A, 0x90, 0x88, 0x46, 0xEE, 0xB8, 0x14, 0xDE, 0x5E, 0x0B, 0xDB,
    0xE0, 0x32, 0x3A, 0x0A, 0x49, 0x06, 0x24, 0x5C, 0xC2, 0xD3, 0xAC, 0x62, 0x91, 0x95, 0xE4, 0x79,
    0xE7, 0xC8, 0x37, 0x6D, 0x8D, 0xD5, 0x4E, 0xA9, 0x6C, 0x56, 0xF4, 0xEA, 0x65, 0x7A, 0xAE, 0x08,
    0xBA, 0x78, 0x25, 0x2E, 0x1C, 0xA6, 0xB4, 0xC6, 0xE8, 0xDD, 0x74, 0x1F, 0x4B, 0xBD, 0x8B, 0x8A,
    0x70, 0x3E, 0xB5, 0x66, 0x48, 0x03, 0xF6, 0x0E, 0x61, 0x35, 0x57, 0xB9, 0x86, 0xC1, 0x1D, 0x9E,
    0xE1, 0xF8, 0x98, 0x11, 0x69, 0xD9, 0x8E, 0x94, 0x9B, 0x1E, 0x87, 0xE9, 0xCE, 0x55, 0x28, 0xDF,
    0x8C, 0xA1, 0x89, 0x0D, 0xBF, 0xE6, 0x42, 0x68, 0x41, 0x99, 0x2D, 0x0F, 0xB0, 0x54, 0xBB, 0x16,
)
_AES_INV_SBOX = tuple(_AES_SBOX.index(i) for i in range(256))
_AES_RCON = (0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _gf_mul(a: int, b: int) -> int:
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return p


def _aes128_expand_key(key16: bytes) -> list[list[int]]:
    w = [list(key16[i : i + 4]) for i in range(0, 16, 4)]
    for i in range(4, 44):
        temp = list(w[i - 1])
        if i % 4 == 0:
            temp = [_AES_SBOX[temp[1]] ^ _AES_RCON[i // 4], _AES_SBOX[temp[2]], _AES_SBOX[temp[3]], _AES_SBOX[temp[0]]]
        w.append([w[i - 4][j] ^ temp[j] for j in range(4)])
    return [sum(w[r * 4 : (r + 1) * 4], []) for r in range(11)]


def _aes128_decrypt_block(block16: bytes, round_keys: list[list[int]]) -> bytes:
    s = [block16[i] ^ round_keys[10][i] for i in range(16)]
    for rnd in range(9, 0, -1):
        # InvShiftRows
        s = [
            s[0], s[13], s[10], s[7],
            s[4], s[1], s[14], s[11],
            s[8], s[5], s[2], s[15],
            s[12], s[9], s[6], s[3],
        ]
        # InvSubBytes + AddRoundKey
        s = [_AES_INV_SBOX[s[i]] ^ round_keys[rnd][i] for i in range(16)]
        # InvMixColumns
        ns = [0] * 16
        for c in range(4):
            idx = c * 4
            a0, a1, a2, a3 = s[idx], s[idx + 1], s[idx + 2], s[idx + 3]
            ns[idx] = _gf_mul(a0, 0x0E) ^ _gf_mul(a1, 0x0B) ^ _gf_mul(a2, 0x0D) ^ _gf_mul(a3, 0x09)
            ns[idx + 1] = _gf_mul(a0, 0x09) ^ _gf_mul(a1, 0x0E) ^ _gf_mul(a2, 0x0B) ^ _gf_mul(a3, 0x0D)
            ns[idx + 2] = _gf_mul(a0, 0x0D) ^ _gf_mul(a1, 0x09) ^ _gf_mul(a2, 0x0E) ^ _gf_mul(a3, 0x0B)
            ns[idx + 3] = _gf_mul(a0, 0x0B) ^ _gf_mul(a1, 0x0D) ^ _gf_mul(a2, 0x09) ^ _gf_mul(a3, 0x0E)
        s = ns
    s = [
        s[0], s[13], s[10], s[7],
        s[4], s[1], s[14], s[11],
        s[8], s[5], s[2], s[15],
        s[12], s[9], s[6], s[3],
    ]
    return bytes(_AES_INV_SBOX[s[i]] ^ round_keys[0][i] for i in range(16))


def _aes128_cbc_decrypt_pkcs7(ciphertext: bytes, key16: bytes, iv16: bytes) -> bytes:
    if not ciphertext or len(ciphertext) % 16 != 0:
        raise ValueError(f"Invalid AES-128-CBC ciphertext length: {len(ciphertext)}")
    rk = _aes128_expand_key(key16)
    out = bytearray()
    prev = iv16
    for off in range(0, len(ciphertext), 16):
        blk = ciphertext[off : off + 16]
        dec = _aes128_decrypt_block(blk, rk)
        out.extend(dec[i] ^ prev[i] for i in range(16))
        prev = blk
    pad_len = out[-1]
    if pad_len < 1 or pad_len > 16 or any(b != pad_len for b in out[-pad_len:]):
        raise ValueError("Invalid PKCS7 padding on Tapo session payload")
    return bytes(out[:-pad_len])


def _tapo_psi_packet(pid: int, section: bytes) -> bytes:
    """Wrap an MPEG-TS PAT/PMT section with MPEG-2 CRC32 into a 188-byte TS packet."""
    crc = 0xFFFFFFFF
    for val in section:
        crc ^= val << 24
        for _ in range(8):
            crc = ((crc << 1) ^ (0x04C11DB7 if crc & 0x80000000 else 0)) & 0xFFFFFFFF
    pkt = bytes((0x47, 0x40 | (pid >> 8), pid & 0xFF, 0x10, 0)) + section + crc.to_bytes(4, "big")
    return pkt.ljust(188, b"\xff")


def _tapo_clean_http_block(raw: bytes) -> str:
    """Strip Tapo's occasional 'HTTP ERROR 401' prefix glued ahead of 'HTTP/1.0 200 OK'."""
    txt = raw.decode("utf-8", errors="ignore")
    for ver in ("HTTP/1.1", "HTTP/1.0", "HTTP/0.9"):
        if txt.startswith(ver):
            return txt
    for ver in ("HTTP/1.1", "HTTP/1.0", "HTTP/0.9"):
        pos = txt.find(ver)
        if pos != -1:
            return txt[pos:]
    return txt


def _parse_mediamtx_cameras() -> dict[str, dict]:
    """
    Parses git-ignored /tmp/mediamtx.yml at runtime so zero camera IPs or passwords
    are ever hardcoded in source files.
    """
    cams: dict[str, dict] = {}
    if not os.path.exists(MEDIAMTX_CONFIG_PATH):
        return cams
    try:
        with open(MEDIAMTX_CONFIG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return cams

    current_cam: str | None = None
    in_paths = False
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == "paths:":
            in_paths = True
            continue
        if not in_paths:
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent == 2 and stripped.endswith(":"):
            current_cam = stripped[:-1].strip()
        elif current_cam and stripped.startswith("source:"):
            rtsp_url = stripped.split("source:", 1)[1].strip().strip("'\"")
            parsed = urlparse(rtsp_url)
            if parsed.hostname:
                path = parsed.path or "/"
                is_isapi = "/Streaming/Channels/" in path
                cams[current_cam] = {
                    "camera_id": current_cam,
                    "host": parsed.hostname,
                    "rtsp_port": parsed.port or 554,
                    "username": unquote(parsed.username or ""),
                    "password": unquote(parsed.password or ""),
                    "rtsp_path": path,
                    "vendor": "annke_isapi" if is_isapi else "tapo_rtsp",
                    "supports_light": is_isapi,
                    "supports_two_way_speaker": True,
                    "supports_live_mic": True,
                }
    return cams


class CameraHardwareService:
    def __init__(self) -> None:
        self._speaker_lock = asyncio.Lock()
        self._config_lock = asyncio.Lock()
        self._go2rtc_lock = asyncio.Lock()
        self._go2rtc_proc: asyncio.subprocess.Process | None = None
        self._saved_state: dict = self._load_saved_state()
        env_tapo_pw = (os.getenv("TAPO_CLOUD_PASSWORD") or "").strip()
        if env_tapo_pw and not (self._saved_state.get("_tapo_auth") or {}).get("sha256"):
            self._saved_state["_tapo_auth"] = {
                "sha256": hashlib.sha256(env_tapo_pw.encode("utf-8")).hexdigest().upper(),
                "md5": hashlib.md5(env_tapo_pw.encode("utf-8")).hexdigest().upper(),
                "updated_at": int(time.time()),
            }
            self._save_state()

    def _load_saved_state(self) -> dict:
        if os.path.exists(HW_STATE_PATH):
            try:
                with open(HW_STATE_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return {}

    def _save_state(self) -> None:
        try:
            os.makedirs(os.path.dirname(HW_STATE_PATH), exist_ok=True)
            with open(HW_STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(self._saved_state, f, indent=2)
        except OSError:
            pass

    def get_camera_cred(self, camera_id: str) -> dict | None:
        return _parse_mediamtx_cameras().get(camera_id)

    async def sync_tapo_onvif_clock(self, info: dict) -> bool:
        """
        Syncs UTC + PST8PDT system clock to TP-Link Tapo cameras over LAN ONVIF (:2020)
        using existing RTSP credentials. Tapo C113 lacks an RTC battery and boots at its
        firmware build date (2026-02-13) if WAN/NTP is blocked or slow, which breaks TLS
        certificate validity and corrupts OSD timestamps.
        """
        host = info.get("host")
        user = info.get("username")
        pw = info.get("password")
        if not (host and user and pw):
            return False

        now_u = datetime.now(timezone.utc)
        nonce_raw = os.urandom(16)
        created = now_u.strftime("%Y-%m-%dT%H:%M:%SZ")
        digest = base64.b64encode(hashlib.sha1(nonce_raw + created.encode() + pw.encode()).digest()).decode()
        nonce_b64 = base64.b64encode(nonce_raw).decode()
        is_dst = bool(time.localtime().tm_isdst > 0)

        soap_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
            'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
            'xmlns:tt="http://www.onvif.org/ver10/schema">'
            "<s:Header>"
            '<Security s:mustUnderstand="1" xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
            "<UsernameToken>"
            f"<Username>{user}</Username>"
            f'<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</Password>'
            f'<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{nonce_b64}</Nonce>'
            f'<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">{created}</Created>'
            "</UsernameToken>"
            "</Security>"
            "</s:Header>"
            "<s:Body>"
            "<tds:SetSystemDateAndTime>"
            "<tds:DateTimeType>Manual</tds:DateTimeType>"
            f"<tds:DaylightSavings>{'true' if is_dst else 'false'}</tds:DaylightSavings>"
            "<tds:TimeZone><tt:TZ>PST8PDT</tt:TZ></tds:TimeZone>"
            "<tds:UTCDateTime>"
            f"<tt:Time><tt:Hour>{now_u.hour}</tt:Hour><tt:Minute>{now_u.minute}</tt:Minute><tt:Second>{now_u.second}</tt:Second></tt:Time>"
            f"<tt:Date><tt:Year>{now_u.year}</tt:Year><tt:Month>{now_u.month}</tt:Month><tt:Day>{now_u.day}</tt:Day></tt:Date>"
            "</tds:UTCDateTime>"
            "</tds:SetSystemDateAndTime>"
            "</s:Body>"
            "</s:Envelope>"
        )
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.post(
                f"http://{host}:2020/onvif/device_service",
                content=soap_xml.encode("utf-8"),
                headers={"Content-Type": "application/soap+xml; charset=utf-8"},
            )
            return resp.status_code == 200

    async def initialize_on_startup(self) -> None:
        """
        Ensures RTSP G.711u live microphone ingest and TwoWayAudio readiness are enabled
        on Annke ISAPI cameras on boot, and syncs LAN ONVIF system clocks on Tapo cameras.
        """
        cams = _parse_mediamtx_cameras()
        for cam_id, info in cams.items():
            if info.get("vendor") == "tapo_rtsp":
                try:
                    await self.sync_tapo_onvif_clock(info)
                except Exception as e:
                    print(f"[CAM HW INIT WARNING] {cam_id} ONVIF clock sync failed: {e}")
                continue
            if info.get("vendor") != "annke_isapi":
                continue
            saved = self._saved_state.get(cam_id) or {}
            want_mic = bool(saved.get("mic_enabled", True))
            mic_vol = int(saved.get("mic_volume", 85))
            spk_vol = int(saved.get("speaker_volume", 85))
            try:
                await self.set_audio_config(
                    cam_id,
                    mic_enabled=want_mic,
                    mic_volume=mic_vol,
                    speaker_volume=spk_vol,
                )
            except Exception as e:
                print(f"[CAM HW INIT WARNING] {cam_id} audio init failed: {e}")

    async def get_status(self, camera_id: str) -> dict:
        info = self.get_camera_cred(camera_id)
        if not info:
            return {
                "camera_id": camera_id,
                "available": False,
                "supports_light": False,
                "supports_two_way_speaker": False,
                "supports_live_mic": False,
            }

        if info["vendor"] != "annke_isapi":
            tapo_auth = self._saved_state.get("_tapo_auth") or {}
            return {
                "camera_id": camera_id,
                "available": True,
                "vendor": info["vendor"],
                "model": "TP-Link Tapo C113",
                "supports_light": False,
                "supports_two_way_speaker": True,
                "tapo_auth_configured": bool(tapo_auth.get("sha256")),
                "supports_live_mic": True,
                "mic_enabled": True,
                "audio_codec": "PCMA/8000",
                "speaker_busy": self._speaker_lock.locked(),
                "presets": list(PRESET_VOICE_MESSAGES.keys()),
            }

        host = info["host"]
        auth = httpx.DigestAuth(info["username"], info["password"])
        light_state = {
            "mode": "smart",
            "isapi_mode": "eventIntelligence",
            "auto_brightness": True,
            "white_brightness": 100,
            "ir_brightness": 100,
            "hold_seconds": 60,
        }
        audio_state = {
            "mic_enabled": False,
            "mic_volume": 80,
            "speaker_volume": 80,
            "noise_reduce": True,
            "audio_codec": "G.711ulaw",
        }
        model_name = "Annke ISAPI"

        try:
            async with httpx.AsyncClient(auth=auth, timeout=4.5) as client:
                r_light, r_twoway, r_stream, r_dev = await asyncio.gather(
                    client.get(f"http://{host}/ISAPI/Image/channels/1/supplementLight"),
                    client.get(f"http://{host}/ISAPI/System/TwoWayAudio/channels/1"),
                    client.get(f"http://{host}/ISAPI/Streaming/channels/101"),
                    client.get(f"http://{host}/ISAPI/System/deviceInfo"),
                    return_exceptions=True,
                )

                if isinstance(r_dev, httpx.Response) and r_dev.status_code == 200:
                    root_d = ET.fromstring(_strip_xml_ns(r_dev.text))
                    m_el = root_d.find("model")
                    if m_el is not None and m_el.text:
                        model_name = f"Annke {m_el.text.strip()}"

                if isinstance(r_light, httpx.Response) and r_light.status_code == 200:
                    root_l = ET.fromstring(_strip_xml_ns(r_light.text))
                    raw_mode = (root_l.findtext("supplementLightMode") or "eventIntelligence").strip()
                    reg_mode = (
                        root_l.findtext("EventIntelligenceModeCfg/brightnessRegulatMode")
                        or root_l.findtext("mixedLightBrightnessRegulatMode")
                        or "auto"
                    ).strip()
                    w_br = (
                        root_l.findtext("EventIntelligenceModeCfg/whiteLightBrightness")
                        or root_l.findtext("whiteLightBrightness")
                        or "100"
                    )
                    ir_br = (
                        root_l.findtext("EventIntelligenceModeCfg/irLightBrightness")
                        or root_l.findtext("irLightBrightness")
                        or "100"
                    )
                    hold_s = root_l.findtext("EventIntelligenceModeCfg/whiteLightTime") or "60"
                    light_state = {
                        "mode": ISAPI_TO_LIGHT_MODE.get(raw_mode, "smart"),
                        "isapi_mode": raw_mode,
                        "auto_brightness": reg_mode.lower() == "auto",
                        "white_brightness": int(w_br),
                        "ir_brightness": int(ir_br),
                        "hold_seconds": int(hold_s),
                    }

                if isinstance(r_twoway, httpx.Response) and r_twoway.status_code == 200:
                    root_t = ET.fromstring(_strip_xml_ns(r_twoway.text))
                    audio_state["speaker_volume"] = int(root_t.findtext("speakerVolume") or 80)
                    audio_state["mic_volume"] = int(root_t.findtext("microphoneVolume") or 80)
                    audio_state["noise_reduce"] = (root_t.findtext("noisereduce") or "true").lower() == "true"

                if isinstance(r_stream, httpx.Response) and r_stream.status_code == 200:
                    root_s = ET.fromstring(_strip_xml_ns(r_stream.text))
                    audio_state["mic_enabled"] = (root_s.findtext("Audio/enabled") or "false").lower() == "true"
                    audio_state["audio_codec"] = (root_s.findtext("Audio/audioCompressionType") or "G.711ulaw").strip()
        except Exception as e:
            return {
                "camera_id": camera_id,
                "available": False,
                "error": str(e),
                "supports_light": True,
                "supports_two_way_speaker": True,
                "supports_live_mic": True,
            }

        return {
            "camera_id": camera_id,
            "available": True,
            "vendor": info["vendor"],
            "model": model_name,
            "supports_light": True,
            "supports_two_way_speaker": True,
            "supports_live_mic": True,
            "light": light_state,
            **audio_state,
            "speaker_busy": self._speaker_lock.locked(),
            "presets": list(PRESET_VOICE_MESSAGES.keys()),
        }

    async def set_light_config(
        self,
        camera_id: str,
        mode: str = "ir",
        white_brightness: int | None = None,
        ir_brightness: int | None = None,
        auto_brightness: bool | None = None,
        hold_seconds: int | None = None,
    ) -> dict:
        info = self.get_camera_cred(camera_id)
        if not info or not info.get("supports_light"):
            raise ValueError(f"Camera '{camera_id}' does not support ISAPI supplement light control")

        clean_mode = mode.strip().lower()
        if clean_mode not in LIGHT_MODE_TO_ISAPI:
            raise ValueError(f"Invalid light mode '{mode}'. Expected one of: {list(LIGHT_MODE_TO_ISAPI)}")
        isapi_mode = LIGHT_MODE_TO_ISAPI[clean_mode]

        cur = await self.get_status(camera_id)
        cur_light = cur.get("light") or {}
        w_br = max(5, min(100, int(white_brightness if white_brightness is not None else cur_light.get("white_brightness", 35))))
        i_br = max(10, min(100, int(ir_brightness if ir_brightness is not None else cur_light.get("ir_brightness", 100))))
        hold_s = max(10, min(300, int(hold_seconds if hold_seconds is not None else cur_light.get("hold_seconds", 60))))

        # Whenever the operator explicitly passes a white_brightness < 100 and doesn't ask for auto,
        # switch regulation to 'manual' so the camera actually obeys the slider instead of blasting 100%!
        if auto_brightness is None:
            use_auto = False if white_brightness is not None else bool(cur_light.get("auto_brightness", False))
        else:
            use_auto = bool(auto_brightness)
        reg_str = "auto" if use_auto else "manual"

        xml_payload = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<SupplementLight version="2.0" xmlns="http://www.std-cgi.com/ver20/XMLSchema">'
            f"<supplementLightMode>{isapi_mode}</supplementLightMode>"
            f"<mixedLightBrightnessRegulatMode>{reg_str}</mixedLightBrightnessRegulatMode>"
            f"<whiteLightBrightness>{w_br}</whiteLightBrightness>"
            f"<irLightBrightness>{i_br}</irLightBrightness>"
            "<EventIntelligenceModeCfg>"
            f"<brightnessRegulatMode>{reg_str}</brightnessRegulatMode>"
            f"<whiteLightBrightness>{w_br}</whiteLightBrightness>"
            f"<irLightBrightness>{i_br}</irLightBrightness>"
            f"<whiteLightTime>{hold_s}</whiteLightTime>"
            "<associatedVMDHuman>true</associatedVMDHuman>"
            "</EventIntelligenceModeCfg>"
            "</SupplementLight>"
        )

        auth = httpx.DigestAuth(info["username"], info["password"])
        async with self._config_lock:
            async with httpx.AsyncClient(auth=auth, timeout=6.0) as client:
                resp = await client.put(
                    f"http://{info['host']}/ISAPI/Image/channels/1/supplementLight",
                    content=xml_payload.encode("utf-8"),
                    headers={"Content-Type": "application/xml"},
                )
                if resp.status_code not in (200, 204):
                    raise RuntimeError(f"ISAPI supplementLight HTTP {resp.status_code}: {resp.text[:220]}")

            cam_saved = self._saved_state.setdefault(camera_id, {})
            cam_saved.update(
                {
                    "light_mode": clean_mode,
                    "white_brightness": w_br,
                    "ir_brightness": i_br,
                    "auto_brightness": use_auto,
                    "hold_seconds": hold_s,
                }
            )
            self._save_state()

        return await self.get_status(camera_id)

    async def set_audio_config(
        self,
        camera_id: str,
        mic_enabled: bool | None = None,
        mic_volume: int | None = None,
        speaker_volume: int | None = None,
    ) -> dict:
        info = self.get_camera_cred(camera_id)
        if not info or info["vendor"] != "annke_isapi":
            return await self.get_status(camera_id)

        host = info["host"]
        auth = httpx.DigestAuth(info["username"], info["password"])
        async with self._config_lock:
            async with httpx.AsyncClient(auth=auth, timeout=6.5) as client:
                # 1. Update TwoWayAudioChannel volumes & noise reduction
                r_tw = await client.get(f"http://{host}/ISAPI/System/TwoWayAudio/channels/1")
                if r_tw.status_code == 200:
                    root_t = ET.fromstring(_strip_xml_ns(r_tw.text))
                    cur_spk = int(root_t.findtext("speakerVolume") or 80)
                    cur_mic = int(root_t.findtext("microphoneVolume") or 80)
                    new_spk = max(0, min(100, int(speaker_volume if speaker_volume is not None else cur_spk)))
                    new_mic = max(0, min(100, int(mic_volume if mic_volume is not None else cur_mic)))
                    tw_xml = (
                        '<?xml version="1.0" encoding="UTF-8"?>'
                        '<TwoWayAudioChannel version="2.0" xmlns="http://www.std-cgi.com/ver20/XMLSchema">'
                        "<id>1</id>"
                        "<enabled>true</enabled>"
                        "<audioCompressionType>G.711ulaw</audioCompressionType>"
                        f"<speakerVolume>{new_spk}</speakerVolume>"
                        f"<microphoneVolume>{new_mic}</microphoneVolume>"
                        "<noisereduce>true</noisereduce>"
                        "<audioInputType>MicIn</audioInputType>"
                        "</TwoWayAudioChannel>"
                    )
                    await client.put(
                        f"http://{host}/ISAPI/System/TwoWayAudio/channels/1",
                        content=tw_xml.encode("utf-8"),
                        headers={"Content-Type": "application/xml"},
                    )

                # 2. Toggle RTSP Channel 101 live microphone <Audio><enabled> (G.711ulaw for zero-CPU WebRTC!)
                if mic_enabled is not None:
                    r_st = await client.get(f"http://{host}/ISAPI/Streaming/channels/101")
                    if r_st.status_code == 200:
                        raw_xml = r_st.text
                        cur_en = "<Audio>\r\n<enabled>true</enabled>" in raw_xml or "<Audio>\n<enabled>true</enabled>" in raw_xml
                        target_str = "true" if mic_enabled else "false"
                        if cur_en != bool(mic_enabled) or "G.711ulaw" not in raw_xml:
                            updated_xml = re.sub(
                                r"<Audio>.*?</Audio>",
                                (
                                    "<Audio>"
                                    f"<enabled>{target_str}</enabled>"
                                    "<audioInputChannelID>1</audioInputChannelID>"
                                    "<audioCompressionType>G.711ulaw</audioCompressionType>"
                                    "</Audio>"
                                ),
                                raw_xml,
                                count=1,
                                flags=re.DOTALL,
                            )
                            await client.put(
                                f"http://{host}/ISAPI/Streaming/channels/101",
                                content=updated_xml.encode("utf-8"),
                                headers={"Content-Type": "application/xml"},
                            )

            cam_saved = self._saved_state.setdefault(camera_id, {})
            if mic_enabled is not None:
                cam_saved["mic_enabled"] = bool(mic_enabled)
            if mic_volume is not None:
                cam_saved["mic_volume"] = max(0, min(100, int(mic_volume)))
            if speaker_volume is not None:
                cam_saved["speaker_volume"] = max(0, min(100, int(speaker_volume)))
            self._save_state()

        return await self.get_status(camera_id)

    async def _ffmpeg_to_ulaw(self, raw_audio_bytes: bytes) -> bytes:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            "-ac",
            "1",
            "-ar",
            "8000",
            "-af",
            "highpass=f=160,lowpass=f=3600,volume=1.5",
            "-f",
            "mulaw",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(input=raw_audio_bytes), timeout=12.0)
        if proc.returncode != 0 or not out:
            raise RuntimeError(f"ffmpeg ulaw conversion failed: {err.decode(errors='ignore')[:200]}")
        # Cap single transmission at 20s (160,000 bytes @ 8kHz)
        return out[:160000]

    async def _synthesize_tts_ulaw(self, text: str) -> bytes:
        clean = re.sub(r"[^\w\s.,!?'-]", " ", (text or "").strip())[:260].strip()
        if not clean:
            raise ValueError("Message text cannot be empty")
        tts_proc = await asyncio.create_subprocess_exec(
            "espeak-ng",
            "-v",
            "en-us",
            "-s",
            "158",
            "-p",
            "46",
            "--stdout",
            clean,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        wav_bytes, tts_err = await asyncio.wait_for(tts_proc.communicate(), timeout=8.0)
        if tts_proc.returncode != 0 or not wav_bytes:
            raise RuntimeError(f"espeak-ng synthesis failed: {tts_err.decode(errors='ignore')[:180]}")
        return await self._ffmpeg_to_ulaw(wav_bytes)

    async def _synthesize_chime_ulaw(self, style: str = "warning") -> bytes:
        sr = 8000
        if style == "polite":
            # Crisp two-note doorbell chime (E5 -> C5)
            t1 = np.linspace(0, 0.32, int(sr * 0.32), endpoint=False)
            t2 = np.linspace(0, 0.48, int(sr * 0.48), endpoint=False)
            n1 = np.sin(2 * np.pi * 659.25 * t1) * np.exp(-3.5 * t1)
            n2 = np.sin(2 * np.pi * 523.25 * t2) * np.exp(-2.8 * t2)
            wave = np.concatenate([n1, np.zeros(int(sr * 0.05)), n2])
        else:
            # High-visibility 2.2s dual-tone security siren pulse (880Hz <-> 1245Hz)
            parts = []
            seg_t = np.linspace(0, 0.22, int(sr * 0.22), endpoint=False)
            for idx in range(9):
                freq = 1245.0 if idx % 2 == 0 else 880.0
                tone = (0.75 * np.sin(2 * np.pi * freq * seg_t) + 0.25 * np.sin(4 * np.pi * freq * seg_t))
                parts.append(tone)
            wave = np.concatenate(parts)

        pcm16 = np.clip(wave * 26000.0, -32767, 32767).astype("<i2").tobytes()
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "s16le",
            "-ar",
            "8000",
            "-ac",
            "1",
            "-i",
            "pipe:0",
            "-f",
            "mulaw",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(input=pcm16), timeout=6.0)
        return out

    async def _stream_ulaw_onvif_backchannel(self, info: dict, ulaw_bytes: bytes) -> bool:
        """
        Streams 8000Hz G.711 PCMU (PT=0) audio directly to the camera's physical speaker
        over standard ONVIF RTSP RTP-over-TCP backchannel (trackID=4 on Annke I51HP).
        """
        host = info["host"]
        port = int(info.get("rtsp_port") or 554)
        user = info["username"]
        pwd = info["password"]
        base_url = f"rtsp://{host}:{port}{info['rtsp_path']}"

        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=4.5)
        cseq = 0
        realm = ""
        nonce = ""
        session_id = ""

        def _make_auth(method: str, uri: str) -> str:
            if not realm or not nonce:
                return ""
            ha1 = hashlib.md5(f"{user}:{realm}:{pwd}".encode()).hexdigest()
            ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
            resp_h = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
            return (
                f'Authorization: Digest username="{user}", realm="{realm}", '
                f'nonce="{nonce}", uri="{uri}", response="{resp_h}"\r\n'
            )

        async def _send_rtsp(method: str, uri: str, extra_headers: str = "") -> tuple[int, str, str]:
            nonlocal cseq, realm, nonce
            cseq += 1
            auth_hdr = _make_auth(method, uri)
            sess_hdr = f"Session: {session_id}\r\n" if session_id else ""
            req = (
                f"{method} {uri} RTSP/1.0\r\n"
                f"CSeq: {cseq}\r\n"
                f"{auth_hdr}"
                f"{sess_hdr}"
                "Require: www.onvif.org/ver20/backchannel\r\n"
                f"{extra_headers}"
                "\r\n"
            )
            writer.write(req.encode())
            await writer.drain()

            raw_hdr = b""
            while b"\r\n\r\n" not in raw_hdr:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=4.0)
                if not chunk:
                    break
                raw_hdr += chunk
            if b"\r\n\r\n" not in raw_hdr:
                return (0, "", "")
            hdr_bytes, body_rem = raw_hdr.split(b"\r\n\r\n", 1)
            hdr_text = hdr_bytes.decode(errors="ignore")
            first_line = hdr_text.splitlines()[0] if hdr_text else ""
            m_code = re.search(r"RTSP/1\.0\s+(\d+)", first_line)
            code = int(m_code.group(1)) if m_code else 0

            m_cl = re.search(r"Content-Length:\s*(\d+)", hdr_text, re.IGNORECASE)
            content_len = int(m_cl.group(1)) if m_cl else 0
            while len(body_rem) < content_len:
                more = await asyncio.wait_for(reader.read(content_len - len(body_rem)), timeout=4.0)
                if not more:
                    break
                body_rem += more
            body_text = body_rem[:content_len].decode(errors="ignore")

            if code == 401:
                mr = re.search(r'realm="([^"]+)"', hdr_text)
                mn = re.search(r'nonce="([^"]+)"', hdr_text)
                if mr and mn:
                    realm, nonce = mr.group(1), mn.group(1)
            return (code, hdr_text, body_text)

        try:
            code, hdr, sdp = await _send_rtsp("DESCRIBE", base_url, "Accept: application/sdp\r\n")
            if code == 401:
                code, hdr, sdp = await _send_rtsp("DESCRIBE", base_url, "Accept: application/sdp\r\n")
            if code != 200 or not sdp:
                return False

            # Locate the sendonly audio backchannel control URL in SDP
            backchannel_uri = ""
            cur_media = ""
            cur_ctrl = ""
            cur_sendonly = False
            for line in sdp.splitlines():
                s = line.strip()
                if s.startswith("m="):
                    if cur_media.startswith("m=audio") and cur_sendonly and cur_ctrl:
                        backchannel_uri = cur_ctrl
                        break
                    cur_media = s
                    cur_ctrl = ""
                    cur_sendonly = False
                elif s == "a=sendonly":
                    cur_sendonly = True
                elif s.startswith("a=control:") and cur_media.startswith("m=audio"):
                    cur_ctrl = s.split("a=control:", 1)[1].strip()
            if not backchannel_uri and cur_media.startswith("m=audio") and cur_sendonly and cur_ctrl:
                backchannel_uri = cur_ctrl
            if not backchannel_uri:
                return False
            if not backchannel_uri.startswith("rtsp://"):
                backchannel_uri = f"{base_url.rstrip('/')}/{backchannel_uri.lstrip('/')}"

            code, hdr, _ = await _send_rtsp(
                "SETUP",
                backchannel_uri,
                "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n",
            )
            if code != 200:
                return False
            m_sess = re.search(r"Session:\s*([^\r\n;]+)", hdr, re.IGNORECASE)
            if not m_sess:
                return False
            session_id = m_sess.group(1).strip()

            m_ch = re.search(r"interleaved=(\d+)", hdr, re.IGNORECASE)
            rtp_chan = int(m_ch.group(1)) if m_ch else 0

            code, _, _ = await _send_rtsp("PLAY", base_url)
            if code != 200:
                return False

            # Pad audio with 120ms of u-law silence (0xFF) at tail so final syllable isn't clipped
            padded = ulaw_bytes + (b"\xff" * 960)
            seq = random.randint(100, 30000)
            ts = random.randint(1000, 1000000)
            ssrc = random.randint(1, 0x7FFFFFFF)
            start_perf = time.perf_counter()

            total_frames = (len(padded) + 159) // 160
            for idx in range(total_frames):
                chunk = padded[idx * 160 : (idx + 1) * 160]
                if len(chunk) < 160:
                    chunk = chunk + (b"\xff" * (160 - len(chunk)))
                marker_pt = 0x80 if idx == 0 else 0x00  # PT=0 (PCMU/8000)
                rtp_hdr = struct.pack(">BBHII", 0x80, marker_pt, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc)
                pkt = struct.pack(">BBH", 0x24, rtp_chan, 12 + len(chunk)) + rtp_hdr + chunk
                writer.write(pkt)
                if idx % 4 == 3:
                    await writer.drain()
                seq += 1
                ts += 160
                target_t = start_perf + (idx + 1) * 0.020
                sleep_dt = target_t - time.perf_counter()
                if sleep_dt > 0.001:
                    await asyncio.sleep(sleep_dt)

            await writer.drain()
            await asyncio.sleep(0.12)
            try:
                await _send_rtsp("TEARDOWN", base_url)
            except Exception:
                pass
            return True
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _stream_ulaw_isapi_twoway(self, info: dict, ulaw_bytes: bytes) -> bool:
        """
        Fallback Hikvision/Annke ISAPI TwoWayAudio HTTP streaming transport.
        """
        host = info["host"]
        auth = httpx.DigestAuth(info["username"], info["password"])

        async def _paced_chunks():
            padded = ulaw_bytes + (b"\xff" * 960)
            start_p = time.perf_counter()
            step = 1600  # 200ms per chunk @ 8000 Hz
            total = (len(padded) + step - 1) // step
            for i in range(total):
                yield padded[i * step : (i + 1) * step]
                wait_s = (start_p + (i + 1) * 0.20) - time.perf_counter()
                if wait_s > 0.002:
                    await asyncio.sleep(wait_s)

        async with httpx.AsyncClient(auth=auth, timeout=25.0) as client:
            try:
                await client.put(f"http://{host}/ISAPI/System/TwoWayAudio/channels/1/close")
            except Exception:
                pass
            r_open = await client.put(f"http://{host}/ISAPI/System/TwoWayAudio/channels/1/open")
            if r_open.status_code not in (200, 204):
                return False
            try:
                r_audio = await client.put(
                    f"http://{host}/ISAPI/System/TwoWayAudio/channels/1/audioData",
                    content=_paced_chunks(),
                    headers={"Content-Type": "application/octet-stream"},
                )
                return r_audio.status_code in (200, 204)
            finally:
                try:
                    await client.put(f"http://{host}/ISAPI/System/TwoWayAudio/channels/1/close")
                except Exception:
                    pass

    async def _open_tapo_8800_session(
        self, host: str, candidates: list[tuple[str, str]]
    ) -> tuple[tuple[asyncio.StreamReader, asyncio.StreamWriter, dict[str, str], str, str] | None, str]:
        """
        Authenticates to TP-Link Tapo's port 8800 Streamd server using keep-alive + Content-Length: -1.
        Tries both reconnect-after-401 (tapo_v4/tapo_media.py verified on 2026 TPAP firmware) and
        same-socket reuse (pytapo.media_stream) so both Streamd behaviors are covered.
        """
        last_diag = "no_response"
        for idx, (uname, secret) in enumerate(candidates):
            if not uname or not secret:
                continue
            modes = (True, False) if idx < 2 else (True,)
            for reconnect_after_401 in modes:
                reader: asyncio.StreamReader | None = None
                writer: asyncio.StreamWriter | None = None
                try:
                    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, 8800), timeout=4.0)
                    req1 = (
                        "POST /stream HTTP/1.1\r\n"
                        "Content-Type: multipart/mixed;boundary=--client-stream-boundary--\r\n"
                        "Connection: keep-alive\r\n"
                        "Content-Length: -1\r\n\r\n"
                    ).encode("utf-8")
                    writer.write(req1)
                    await writer.drain()

                    raw1 = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=4.0)
                    txt1 = _tapo_clean_http_block(raw1)
                    first1 = (txt1.splitlines()[0] if txt1 else "").strip()
                    m_auth = re.search(r"WWW-Authenticate:\s*Digest\s+([^\r\n]+)", txt1, re.IGNORECASE)
                    if not m_auth:
                        last_diag = first1 or "missing_www_auth"
                        writer.close()
                        continue

                    chall = m_auth.group(1)
                    m_realm = re.search(r'realm="([^"]+)"', chall)
                    m_nonce = re.search(r'nonce="([^"]+)"', chall)
                    m_qop = re.search(r'qop="([^"]+)"', chall)
                    m_opaque = re.search(r'opaque="([^"]+)"', chall)
                    if not (m_realm and m_nonce):
                        last_diag = "malformed_digest_challenge"
                        writer.close()
                        continue

                    if reconnect_after_401:
                        writer.close()
                        try:
                            await writer.wait_closed()
                        except Exception:
                            pass
                        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, 8800), timeout=4.0)

                    realm = m_realm.group(1)
                    nonce = m_nonce.group(1)
                    qop = m_qop.group(1) if m_qop else "auth"
                    opaque = m_opaque.group(1) if m_opaque else ""
                    uri = "/stream"
                    nc = "00000001"
                    cnonce = os.urandom(12).hex()

                    ha1 = hashlib.md5(f"{uname}:{realm}:{secret}".encode("utf-8")).hexdigest()
                    ha2 = hashlib.md5(f"POST:{uri}".encode("utf-8")).hexdigest()
                    resp_dig = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode("utf-8")).hexdigest()

                    auth_hdr = (
                        f'Digest username="{uname}",realm="{realm}",uri="{uri}",algorithm=MD5,'
                        f'nonce="{nonce}",nc={nc},cnonce="{cnonce}",qop={qop},response="{resp_dig}"'
                    )
                    if opaque:
                        auth_hdr += f',opaque="{opaque}"'

                    req2 = (
                        "POST /stream HTTP/1.1\r\n"
                        "Content-Type: multipart/mixed;boundary=--client-stream-boundary--\r\n"
                        "Connection: keep-alive\r\n"
                        "Content-Length: -1\r\n"
                        f"Authorization: {auth_hdr}\r\n\r\n"
                    ).encode("utf-8")
                    writer.write(req2)
                    await writer.drain()

                    raw2 = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=4.0)
                    txt2 = _tapo_clean_http_block(raw2)
                    lines2 = [ln.strip() for ln in txt2.splitlines() if ln.strip()]
                    status_line = lines2[0] if lines2 else "empty_auth_reply"
                    last_diag = status_line

                    if " 200 " in f" {status_line} ":
                        hdr_map: dict[str, str] = {}
                        for ln in lines2[1:]:
                            if ":" in ln:
                                k, v = ln.split(":", 1)
                                hdr_map[k.strip()] = v.strip()
                        return ((reader, writer, hdr_map, uname, secret), status_line)

                    writer.close()
                    try:
                        await writer.wait_closed()
                    except Exception:
                        pass
                except Exception as exc:
                    last_diag = f"{type(exc).__name__}"
                    if writer is not None:
                        writer.close()
                        try:
                            await writer.wait_closed()
                        except Exception:
                            pass
                    continue
        return (None, last_diag)

    async def set_tapo_cloud_password(self, cloud_password: str) -> dict:
        """
        Verifies the user's TP-Link/Tapo Cloud password live against Tapo port 8800 Streamd
        using the single-socket Digest handshake, then stores ONLY the irreversible hash on disk.
        """
        pw = (cloud_password or "").strip()
        if not pw:
            raise ValueError("Tapo Cloud password cannot be empty")

        sha_up = hashlib.sha256(pw.encode("utf-8")).hexdigest().upper()
        sha_low = sha_up.lower()
        md5_up = hashlib.md5(pw.encode("utf-8")).hexdigest().upper()
        md5_low = md5_up.lower()

        cams = _parse_mediamtx_cameras()
        tapo_cams = [c for c in cams.values() if c.get("vendor") == "tapo_rtsp"]
        verified_host = None
        matched_user = "admin"
        matched_secret = sha_up
        reachable_any = False

        last_diag = "unreachable"
        for c in tapo_cams:
            host = c["host"]
            rtsp_u = c.get("username") or ""
            cands: list[tuple[str, str]] = [
                ("admin", sha_up),
                ("admin", md5_up),
                ("admin", sha_low),
                ("admin", md5_low),
            ]
            if rtsp_u and rtsp_u != "admin":
                cands.extend([(rtsp_u, sha_up), (rtsp_u, md5_up), (rtsp_u, pw)])
            if re.fullmatch(r"[0-9a-fA-F]{64}", pw) or re.fullmatch(r"[0-9a-fA-F]{32}", pw):
                cands.insert(0, ("admin", pw.upper()))

            try:
                _, w_chk = await asyncio.wait_for(asyncio.open_connection(host, 8800), timeout=2.5)
                w_chk.close()
                try:
                    await w_chk.wait_closed()
                except Exception:
                    pass
                reachable_any = True
            except Exception:
                continue

            sess_tuple, diag = await self._open_tapo_8800_session(host, cands)
            last_diag = f"{host}:8800 -> {diag}"
            if sess_tuple is not None:
                _, sess_writer, _, matched_user, matched_secret = sess_tuple
                sess_writer.close()
                try:
                    await sess_writer.wait_closed()
                except Exception:
                    pass
                verified_host = host
                break

        if reachable_any and not verified_host:
            if "401" in last_diag:
                raise ValueError(
                    f"Tapo camera returned 401 ({last_diag}) — double-check your main TP-Link ID / Tapo app login password"
                )
            print(f"[TAPO AUTH NOTE] Camera busy during probe ({last_diag}); saving SHA-256 hash for on-demand speaker.")

        # Never persist raw plaintext if an RTSP raw fallback ever matched
        stored_secret = matched_secret if matched_secret != pw else sha_up
        async with self._config_lock:
            self._saved_state["_tapo_auth"] = {
                "cred_user": matched_user,
                "cred_secret": stored_secret,
                "sha256": sha_up,
                "md5": md5_up,
                "verified_host": verified_host,
                "updated_at": int(time.time()),
            }
            self._save_state()

        return {
            "status": "LINKED",
            "tapo_auth_configured": True,
            "verified_host": verified_host,
        }

    async def _stream_ulaw_tapo_native(self, camera_id: str, info: dict, ulaw_bytes: bytes) -> bool:
        """
        Native asyncio TP-Link Tapo port 8800 Streamd speaker backchannel (PCMA/8000 in MPEG-TS).
        Ported from pytapo.media_stream (session.py + talk.py) so Loft & Living Room speakers work
        reliably on TPAP/V4 firmware without go2rtc socket-close or encrypted-JSON bugs.
        """
        tapo_auth = self._saved_state.get("_tapo_auth") or {}
        sha_up = tapo_auth.get("sha256")
        md5_up = tapo_auth.get("md5")
        if not sha_up:
            raise ValueError(
                "Tapo speaker not linked yet — enter your TP-Link/Tapo app password once in the TALK panel to unlock Loft & Living Room speakers."
            )

        primary_u = tapo_auth.get("cred_user") or "admin"
        primary_s = tapo_auth.get("cred_secret") or sha_up
        cands: list[tuple[str, str]] = [(primary_u, primary_s)]
        for extra_u, extra_s in (("admin", sha_up), ("admin", md5_up or "")):
            if extra_s and (extra_u, extra_s) not in cands:
                cands.append((extra_u, extra_s))

        # Transcode 8kHz mulaw -> 8kHz mono A-law (PCMA) with 650ms speaker-amp wake delay
        ff_proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "mulaw",
            "-ar",
            "8000",
            "-ac",
            "1",
            "-i",
            "pipe:0",
            "-af",
            "adelay=650:all=1",
            "-ac",
            "1",
            "-ar",
            "8000",
            "-c:a",
            "pcm_alaw",
            "-f",
            "alaw",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        alaw_bytes, ff_err = await asyncio.wait_for(ff_proc.communicate(input=ulaw_bytes), timeout=10.0)
        if ff_proc.returncode != 0 or not alaw_bytes:
            raise RuntimeError(f"FFmpeg PCMA conversion failed: {ff_err.decode(errors='ignore')[:160]}")

        host = info["host"]
        sess_tuple, diag = await self._open_tapo_8800_session(host, cands)
        if sess_tuple is None:
            raise RuntimeError(f"Tapo speaker auth failed on {host}:8800 ({diag})")

        reader, writer, res_headers, matched_u, matched_s = sess_tuple
        try:
            # Derive optional AES-128-CBC key/IV from Key-Exchange header for encrypted JSON control replies
            kx_hdr = res_headers.get("Key-Exchange") or res_headers.get("key-exchange") or ""
            kx_pairs = dict(re.findall(r'([A-Za-z0-9_-]+)="?([^",\s]+)"?', kx_hdr))
            kx_nonce = (kx_pairs.get("nonce") or "").encode("utf-8")
            kx_user = (kx_pairs.get("username") or matched_u or "admin").encode("utf-8")
            if kx_user == b"none":
                aes_secret = b"TPL075526460603"
            else:
                aes_secret = matched_s.encode("utf-8")
            aes_key = hashlib.md5(kx_nonce + b":" + aes_secret).digest() if kx_nonce else b""
            aes_iv = hashlib.md5(kx_user + b":" + kx_nonce).digest() if kx_nonce else b""

            dev_boundary = b"--device-stream-boundary--"
            ct_hdr = res_headers.get("Content-Type") or res_headers.get("content-type") or ""
            m_bnd = re.search(r"boundary=([^\s;]+)", ct_hdr, re.IGNORECASE)
            if m_bnd:
                dev_boundary = m_bnd.group(1).strip().strip('"').encode("utf-8")

            # Request 2-way talk backchannel session (AEC mode)
            seq_id = random.randint(1000, 30000)
            req_json = json.dumps(
                {"type": "request", "seq": seq_id, "params": {"talk": {"mode": "aec"}, "method": "get"}},
                separators=(",", ":"),
            ).encode("utf-8")
            talk_req_frame = (
                b"----client-stream-boundary--\r\n"
                b"Content-Type: application/json\r\n"
                + f"Content-Length: {len(req_json)}\r\n".encode("ascii")
                + b"X-Data-Window-Size: 500\r\n\r\n"
                + req_json
                + b"\r\n"
            )
            writer.write(talk_req_frame)
            await writer.drain()

            session_id: str | None = None
            for _ in range(8):
                await asyncio.wait_for(reader.readuntil(dev_boundary), timeout=5.0)
                part_hdr_raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=4.0)
                part_hdrs: dict[str, str] = {}
                for ln in part_hdr_raw.decode("utf-8", errors="ignore").splitlines():
                    if ":" in ln:
                        k, v = ln.split(":", 1)
                        part_hdrs[k.strip()] = v.strip()

                p_len = int(part_hdrs.get("Content-Length") or "0")
                p_enc = int(part_hdrs.get("X-If-Encrypt") or "0") == 1
                p_type = (part_hdrs.get("Content-Type") or "").lower()
                if "X-Session-Id" in part_hdrs:
                    session_id = part_hdrs["X-Session-Id"]

                body_bytes = await asyncio.wait_for(reader.readexactly(p_len), timeout=4.0) if p_len > 0 else b""
                if p_enc and body_bytes and aes_key and aes_iv:
                    body_bytes = _aes128_cbc_decrypt_pkcs7(body_bytes, aes_key, aes_iv)

                if "application/json" in p_type and body_bytes:
                    jdata = json.loads(body_bytes.decode("utf-8", errors="ignore"))
                    params = jdata.get("params") or {}
                    err_code = params.get("error_code", jdata.get("error_code", 0))
                    if err_code not in (0, None):
                        raise RuntimeError(
                            f"Tapo speaker refused talk session (error_code={err_code} — close Tapo phone app talkback if open)"
                        )
                    if params.get("session_id") is not None:
                        session_id = str(params["session_id"])
                    if jdata.get("type") == "response" and session_id is not None:
                        break

            if not session_id:
                raise RuntimeError(f"Tapo camera {camera_id} did not return a talk session_id")

            async def _send_mp2t_part(ts_data: bytes) -> None:
                part = (
                    b"----client-stream-boundary--\r\n"
                    b"Content-Type: audio/mp2t\r\n"
                    b"X-If-Encrypt: 0\r\n"
                    + f"X-Session-Id: {session_id}\r\n".encode("ascii")
                    + f"Content-Length: {len(ts_data)}\r\n\r\n".encode("ascii")
                    + ts_data
                )
                writer.write(part)
                await writer.drain()

            # PAT (PID 0 -> PMT 0x1000) + PMT (PCMA stream_type 0x90 on PID 0x0100)
            psi_hdr = _tapo_psi_packet(0, bytes.fromhex("00b00d0001c100000001f000")) + _tapo_psi_packet(
                0x1000, bytes.fromhex("02b0120001c10000fffff00090e100f000")
            )
            await _send_mp2t_part(psi_hdr)

            # Pace 160-byte (20ms @ 8kHz) PCMA frames wrapped in PES + 188-byte MPEG-TS packets
            cc = 0
            samples_sent = 0
            loop = asyncio.get_running_loop()
            deadline = loop.time()

            for off in range(0, len(alaw_bytes), 160):
                chunk_alaw = alaw_bytes[off : off + 160]
                wait_s = deadline - loop.time()
                if wait_s > 0:
                    await asyncio.sleep(wait_s)

                pts = (samples_sent * 90000 // 8000) & ((1 << 33) - 1)
                pts_bytes = bytes(
                    (
                        0x21 | ((pts >> 29) & 0x0E),
                        (pts >> 22) & 0xFF,
                        1 | ((pts >> 14) & 0xFE),
                        (pts >> 7) & 0xFF,
                        1 | ((pts << 1) & 0xFE),
                    )
                )
                pes = b"\x00\x00\x01\xc0" + (len(chunk_alaw) + 8).to_bytes(2, "big") + b"\x80\x80\x05" + pts_bytes + chunk_alaw
                ts_pkts = bytearray()
                first_pkt = True
                rem = pes
                while rem:
                    part_b, rem = rem[:184], rem[184:]
                    flags = 0x10 if len(part_b) == 184 else 0x30
                    ts_pkts.extend((0x47, 0x41 if first_pkt else 0x01, 0x00, flags | cc))
                    if len(part_b) < 184:
                        pad_n = 183 - len(part_b)
                        ts_pkts.append(pad_n)
                        if pad_n:
                            ts_pkts.extend(b"\x00" + (b"\xff" * (pad_n - 1)))
                    ts_pkts.extend(part_b)
                    cc = (cc + 1) & 0x0F
                    first_pkt = False

                await _send_mp2t_part(bytes(ts_pkts))
                samples_sent += len(chunk_alaw)
                deadline = max(deadline + (len(chunk_alaw) / 8000.0), loop.time())

            await asyncio.sleep(0.30)
            return True
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _ensure_go2rtc_running(self) -> None:
        async with self._go2rtc_lock:
            if self._go2rtc_proc and self._go2rtc_proc.returncode is None:
                return
            cfg_yaml = (
                "api:\n"
                "  listen: 127.0.0.1:1984\n"
                "rtsp:\n"
                "  listen: 127.0.0.1:18554\n"
                "webrtc:\n"
                "  listen: \"\"\n"
                "log:\n"
                "  level: warn\n"
            )
            with open(GO2RTC_CFG_PATH, "w", encoding="utf-8") as f:
                f.write(cfg_yaml)
            self._go2rtc_proc = await asyncio.create_subprocess_exec(
                "/usr/local/bin/go2rtc",
                "-config",
                GO2RTC_CFG_PATH,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            async with httpx.AsyncClient(timeout=2.0) as client:
                for _ in range(15):
                    await asyncio.sleep(0.15)
                    try:
                        r = await client.get(f"{GO2RTC_API_BASE}/api")
                        if r.status_code == 200:
                            return
                    except Exception:
                        pass
            raise RuntimeError("Local go2rtc audio helper failed to start on 127.0.0.1:1984")

    async def _stream_ulaw_tapo_go2rtc(self, camera_id: str, info: dict, ulaw_bytes: bytes) -> bool:
        """
        Streams audio to TP-Link Tapo C113 built-in speaker via local go2rtc backchannel
        (connecting on-demand to port 8800 and tearing down immediately afterward so the
        Tapo mobile app never experiences talkback slot contention).
        """
        tapo_auth = self._saved_state.get("_tapo_auth") or {}
        sha_up = tapo_auth.get("sha256")
        md5_up = tapo_auth.get("md5")
        if not sha_up:
            raise ValueError(
                "Tapo speaker not linked yet — enter your TP-Link/Tapo app password once in the TALK panel to unlock Loft & Living Room speakers."
            )

        await self._ensure_go2rtc_running()
        host = info["host"]
        wav_path = f"/tmp/tapo_talk_{camera_id}.wav"

        # Convert 8kHz G.711 u-law buffer into a standard 8kHz 16-bit PCM WAV file for go2rtc's #input=file reader
        padded_ulaw = ulaw_bytes + (b"\xff" * 960)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "mulaw",
            "-ar",
            "8000",
            "-ac",
            "1",
            "-i",
            "pipe:0",
            "-acodec",
            "pcm_s16le",
            "-ar",
            "8000",
            "-ac",
            "1",
            wav_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await asyncio.wait_for(proc.communicate(input=padded_ulaw), timeout=8.0)
        if proc.returncode != 0 or not os.path.exists(wav_path):
            raise RuntimeError(f"WAV prep failed: {err.decode(errors='ignore')[:160]}")

        stream_key = f"{camera_id}_talk"
        duration_s = len(padded_ulaw) / 8000.0
        primary_u = tapo_auth.get("cred_user") or "admin"
        primary_s = tapo_auth.get("cred_secret") or sha_up
        auth_candidates: list[str] = [f"{primary_u}:{primary_s}"]
        for extra in (f"admin:{sha_up}", f"admin:{md5_up}" if md5_up else ""):
            if extra and extra not in auth_candidates:
                auth_candidates.append(extra)

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                for cred in auth_candidates:
                    tapo_src = f"tapo://{cred}@{host}"
                    await client.put(
                        f"{GO2RTC_API_BASE}/api/streams",
                        params={"name": stream_key, "src": tapo_src},
                    )
                    play_src = f"ffmpeg:{wav_path}#audio=pcma#input=file"
                    r_play = await client.post(
                        f"{GO2RTC_API_BASE}/api/streams",
                        params={"dst": stream_key, "src": play_src},
                    )
                    if r_play.status_code in (200, 201, 204):
                        await asyncio.sleep(max(1.2, duration_s + 0.45))
                        try:
                            await client.delete(
                                f"{GO2RTC_API_BASE}/api/streams",
                                params={"src": stream_key},
                            )
                        except Exception:
                            pass
                        return True
            return False
        finally:
            if os.path.exists(wav_path):
                try:
                    os.remove(wav_path)
                except OSError:
                    pass

    async def transmit_ulaw(self, camera_id: str, ulaw_bytes: bytes) -> dict:
        info = self.get_camera_cred(camera_id)
        if not info or not info.get("supports_two_way_speaker"):
            raise ValueError(f"Camera '{camera_id}' does not expose a two-way speaker channel")
        if not ulaw_bytes:
            raise ValueError("Empty audio buffer")

        duration_s = round(len(ulaw_bytes) / 8000.0, 2)
        async with self._speaker_lock:
            if info["vendor"] == "tapo_rtsp":
                transport_used = "tapo_native_8800"
                try:
                    ok = await self._stream_ulaw_tapo_native(camera_id, info, ulaw_bytes)
                except Exception as native_err:
                    print(f"[TAPO SPEAKER NATIVE] {camera_id}: {native_err}")
                    if os.path.exists("/usr/local/bin/go2rtc"):
                        transport_used = "tapo_go2rtc_8800"
                        ok = await self._stream_ulaw_tapo_go2rtc(camera_id, info, ulaw_bytes)
                    else:
                        raise
                if not ok:
                    raise RuntimeError(
                        f"Failed to stream audio to '{camera_id}' Tapo speaker (ensure Tapo App -> Me -> Tapo Lab -> Third-Party Compatibility is ON)"
                    )
                return {
                    "status": "PLAYED",
                    "camera_id": camera_id,
                    "duration_sec": duration_s,
                    "transport": transport_used,
                }

            transport_used = "onvif_backchannel"
            ok = False
            try:
                ok = await self._stream_ulaw_onvif_backchannel(info, ulaw_bytes)
            except Exception as e:
                print(f"[CAM SPEAKER ONVIF FALLBACK] {camera_id}: {e}")
                ok = False

            if not ok:
                transport_used = "isapi_twoway"
                ok = await self._stream_ulaw_isapi_twoway(info, ulaw_bytes)

            if not ok:
                raise RuntimeError(f"Failed to stream audio to '{camera_id}' speaker over ONVIF and ISAPI")

        return {
            "status": "PLAYED",
            "camera_id": camera_id,
            "duration_sec": duration_s,
            "transport": transport_used,
        }

    async def speak_text(self, camera_id: str, text_or_preset: str) -> dict:
        key = (text_or_preset or "").strip().lower()
        spoken_text = PRESET_VOICE_MESSAGES.get(key, text_or_preset.strip())
        ulaw = await self._synthesize_tts_ulaw(spoken_text)
        res = await self.transmit_ulaw(camera_id, ulaw)
        return {**res, "spoken_text": spoken_text}

    async def play_chime(self, camera_id: str, style: str = "warning", announce_warning: bool = False) -> dict:
        ulaw = await self._synthesize_chime_ulaw(style=style)
        if announce_warning:
            voice_ulaw = await self._synthesize_tts_ulaw(PRESET_VOICE_MESSAGES["warning"])
            ulaw = ulaw + (b"\xff" * 800) + voice_ulaw
        res = await self.transmit_ulaw(camera_id, ulaw)
        return {**res, "style": style}

    async def push_to_talk_raw(self, camera_id: str, uploaded_audio_bytes: bytes) -> dict:
        ulaw = await self._ffmpeg_to_ulaw(uploaded_audio_bytes)
        return await self.transmit_ulaw(camera_id, ulaw)


camera_hardware_service = CameraHardwareService()
