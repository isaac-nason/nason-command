import asyncio
import hashlib
import json
import os
import random
import re
import struct
import time
from urllib.parse import urlparse, unquote
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

    async def initialize_on_startup(self) -> None:
        """
        Ensures RTSP G.711u live microphone ingest and TwoWayAudio readiness are enabled
        on Annke ISAPI cameras on boot, without altering the user's current light mode
        unless previously saved in camera_hardware_state.json.
        """
        cams = _parse_mediamtx_cameras()
        for cam_id, info in cams.items():
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

    async def _verify_tapo_8800_digest(self, host: str, candidates: list[tuple[str, str]]) -> tuple[str, str] | None:
        """
        Tests Digest credentials directly against Tapo's port 8800 Streamd endpoint
        (the exact HTTP Digest socket go2rtc uses for 2-way talk) instead of port 443's
        firmware-dependent KLAP/v4 challenge.
        """
        for uname, secret in candidates:
            if not uname or not secret:
                continue
            try:
                r1, w1 = await asyncio.wait_for(asyncio.open_connection(host, 8800), timeout=3.0)
                req1 = (
                    f"POST /stream HTTP/1.1\r\n"
                    f"Host: {host}:8800\r\n"
                    "Content-Type: multipart/mixed; boundary=--client-stream-boundary--\r\n"
                    "Content-Length: 0\r\n\r\n"
                )
                w1.write(req1.encode("utf-8"))
                await w1.drain()
                resp1 = (await asyncio.wait_for(r1.read(2048), timeout=3.0)).decode(errors="ignore")
                w1.close()
                try:
                    await w1.wait_closed()
                except Exception:
                    pass

                m_auth = re.search(r"WWW-Authenticate:\s*Digest\s+([^\r\n]+)", resp1, re.IGNORECASE)
                if not m_auth:
                    continue
                chall = m_auth.group(1)
                m_realm = re.search(r'realm="([^"]+)"', chall)
                m_nonce = re.search(r'nonce="([^"]+)"', chall)
                m_qop = re.search(r'qop="([^"]+)"', chall)
                m_opaque = re.search(r'opaque="([^"]+)"', chall)
                if not (m_realm and m_nonce):
                    continue

                realm = m_realm.group(1)
                nonce = m_nonce.group(1)
                qop = m_qop.group(1) if m_qop else "auth"
                opaque = m_opaque.group(1) if m_opaque else ""
                uri = "/stream"
                nc = "00000001"
                cnonce = os.urandom(16).hex()

                ha1 = hashlib.md5(f"{uname}:{realm}:{secret}".encode("utf-8")).hexdigest()
                ha2 = hashlib.md5(f"POST:{uri}".encode("utf-8")).hexdigest()
                resp_dig = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode("utf-8")).hexdigest()

                auth_hdr = (
                    f'Digest username="{uname}", realm="{realm}", nonce="{nonce}", '
                    f'uri="{uri}", qop={qop}, nc={nc}, cnonce="{cnonce}", response="{resp_dig}"'
                )
                if opaque:
                    auth_hdr += f', opaque="{opaque}", algorithm=MD5'

                r2, w2 = await asyncio.wait_for(asyncio.open_connection(host, 8800), timeout=3.0)
                req2 = (
                    f"POST {uri} HTTP/1.1\r\n"
                    f"Host: {host}:8800\r\n"
                    f"Authorization: {auth_hdr}\r\n"
                    "Content-Type: multipart/mixed; boundary=--client-stream-boundary--\r\n\r\n"
                )
                w2.write(req2.encode("utf-8"))
                await w2.drain()
                line2 = (await asyncio.wait_for(r2.readline(), timeout=3.0)).decode(errors="ignore")
                w2.close()
                try:
                    await w2.wait_closed()
                except Exception:
                    pass

                if " 200 " in line2:
                    return (uname, secret)
            except Exception:
                continue
        return None

    async def set_tapo_cloud_password(self, cloud_password: str) -> dict:
        """
        Verifies the user's TP-Link/Tapo Cloud password directly against Tapo's port 8800
        Streamd HTTP Digest server (the exact socket go2rtc uses), then stores ONLY the
        verified irreversible hash on disk (zero plaintext password stored).
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
                cands.extend([(rtsp_u, sha_up), (rtsp_u, md5_up)])
            # Also support if user pasted an already-computed 64-char SHA256 or 32-char MD5 hex hash
            if re.fullmatch(r"[0-9a-fA-F]{64}", pw) or re.fullmatch(r"[0-9a-fA-F]{32}", pw):
                cands.insert(0, ("admin", pw.upper()))

            try:
                r_chk, w_chk = await asyncio.wait_for(asyncio.open_connection(host, 8800), timeout=2.5)
                w_chk.close()
                try:
                    await w_chk.wait_closed()
                except Exception:
                    pass
                reachable_any = True
            except Exception:
                continue

            matched = await self._verify_tapo_8800_digest(host, cands)
            if matched:
                matched_user, matched_secret = matched
                verified_host = host
                break

        if reachable_any and not verified_host:
            raise ValueError(
                "Rejected by Tapo port 8800 — make sure you enter your main TP-Link / Tapo mobile app login password (not the RTSP camera password)"
            )

        async with self._config_lock:
            self._saved_state["_tapo_auth"] = {
                "cred_user": matched_user,
                "cred_secret": matched_secret,
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
                ok = await self._stream_ulaw_tapo_go2rtc(camera_id, info, ulaw_bytes)
                if not ok:
                    raise RuntimeError(
                        f"Failed to stream audio to '{camera_id}' Tapo speaker (ensure Tapo App -> Me -> Tapo Lab -> Third-Party Compatibility is ON)"
                    )
                return {
                    "status": "PLAYED",
                    "camera_id": camera_id,
                    "duration_sec": duration_s,
                    "transport": "tapo_go2rtc_8800",
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
