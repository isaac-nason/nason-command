import asyncio
import base64
import json
import os
import re
import time
from datetime import datetime as _dt, timedelta as _td, timezone as _tz
from zoneinfo import ZoneInfo as _ZoneInfo
import cv2
import httpx
from sqlalchemy import select, func, delete
from app.db.session import AsyncSessionLocal
from app.db.models import DetectionEvent, CameraZone, KnownFace as _KnownFace, CameraSchedule as _CameraSchedule
from app.services.face_engine import face_engine
from app.services.push_notifier import get_push_config, send_tactical_push, CAM_NAMES
from app.services.delivery_rules import get_delivery_rules as _get_delivery_rules, record_delivery_sighting as _record_delivery_sighting
from app.services.ai_lessons import get_ai_lessons as _get_ai_lessons

WORKSTATION_GPU_URL = os.getenv("WORKSTATION_OLLAMA_URL", "http://192.168.1.223:11434")
SNAPSHOT_DIR = "/tmp/command_snapshots"
GPU_SEMAPHORE = asyncio.Semaphore(1)

_GPU_CACHE: dict = {"checked_at": 0.0, "online": False, "models": []}


async def probe_workstation_ollama(force: bool = False) -> tuple[bool, list[str]]:
    now = time.time()
    ttl = 60.0 if _GPU_CACHE["online"] else 30.0
    if not force and (now - _GPU_CACHE["checked_at"]) < ttl:
        return _GPU_CACHE["online"], list(_GPU_CACHE["models"])

    try:
        async with httpx.AsyncClient(timeout=2.2) as client:
            res = await client.get(f"{WORKSTATION_GPU_URL}/api/tags")
            if res.status_code == 200:
                models = [m["name"] for m in res.json().get("models", [])]
                _GPU_CACHE.update({"checked_at": now, "online": True, "models": models})
                return True, models
    except Exception:
        pass

    _GPU_CACHE.update({"checked_at": now, "online": False, "models": []})
    return False, []


def pick_best_vlm(models: list[str]) -> str | None:
    for preferred in ("qwen2.5vl", "qwen2-vl", "minicpm-v", "llama3.2-vision", "llava"):
        for m in models:
            if preferred in m.lower():
                return m
    return None


def extract_json_dict(raw_text: str) -> dict:
    start = raw_text.find("{")
    end = raw_text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(raw_text[start : end + 1])
    return json.loads(raw_text)


_LOCAL_TZ = _ZoneInfo("America/Los_Angeles")


def _scrub_parked_red_truck(text: str) -> str:
    if not text:
        return text
    low = text.lower()
    if any(w in low for w in ("open", "touch", "tamper", "handle", "door", "bed", "tailgate", "leaning", "looking into", "breaking")):
        return text
    cleaned = re.sub(
        r",?\s*(?:near|past|beside|by|next\s+to|behind|around)\s+(?:a\s+|the\s+)?(?:parked\s+)?red\s+(?:pickup\s+)?(?:truck|vehicle)(?:\s+with\s+4x4\s+badge)?(?:\s+on\s+(?:the\s+|a\s+)?driveway)?",
        "",
        text,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r",?\s*(?:with\s+)?(?:a\s+|the\s+)?(?:parked\s+)?red\s+(?:pickup\s+)?truck(?:\s+with\s+4x4\s+badge)?(?:\s+(?:parked|visible|seen))?(?:\s+(?:nearby|on\s+(?:the\s+|a\s+)?driveway|in\s+(?:the\s+)?(?:background|driveway)))?",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\s+,", ",", cleaned)
    cleaned = re.sub(r",\s*\.", ".", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ,")
    if cleaned and not cleaned.endswith(".") and text.endswith("."):
        cleaned += "."
    return cleaned


def _sanitize_plate(raw_plate: str | None, category: str | None) -> str:
    if (category or "").lower() != "vehicle":
        return ""
    p = (raw_plate or "").strip().upper()
    if not p or any(tok in p for tok in ("NONE", "N/A", "UNKNOWN", "NULL", "ABC123", "1234", "ILLEGIBLE", "OR ")):
        return ""
    alnum = re.sub(r"[^A-Z0-9 -]", "", p).strip()
    if len(alnum.replace(" ", "").replace("-", "")) < 3 or len(alnum) > 10:
        return ""
    return alnum


def _is_valid_food_delivery(summary: str, in_tripwire: bool, recent_household_car: bool) -> bool:
    if not in_tripwire or recent_household_car:
        return False
    low = (summary or "").lower()
    veto_terms = (
        "child",
        "toddler",
        "kid",
        "laundry",
        "basket",
        "trash",
        "recycling",
        "car door",
        "opens ",
        "opening ",
        "adjusting ",
        "exiting ",
        "entering ",
        "assisting ",
        "inspecting ",
        "shirt",
        "sidewalk",
        "leash",
        "dog",
    )
    if any(v in low for v in veto_terms):
        return False
    positive_terms = (
        "thermal",
        "insulated",
        "pizza box",
        "pizza bag",
        "delivery bag",
        "hot bag",
        "takeout bag",
        "doordash",
        "courier bag",
        "red bag",
        "orange bag",
    )
    return any(p in low for p in positive_terms)


def _infer_carrier_from_summary(summary: str, active_rule_names: list[str]) -> str:
    """
    Deterministic safety net when Qwen2.5-VL explicitly describes a branded carrier vest/uniform/tool
    in 'summary' (e.g. 'Person wearing Amazon delivery vest walking away') while leaving carrier='NONE'.
    """
    low = (summary or "").lower()
    if not low or any(neg in low for neg in ("not amazon", "no amazon", "recycling bin", "trash bin", "yard-waste")):
        return "NONE"
    active = {r.upper() for r in (active_rule_names or [])}
    if "AMAZON" in active and (
        "amazon" in low
        or (
            any(v in low for v in ("delivery vest", "reflective vest", "blue vest"))
            and any(p in low for p in ("package", "parcel", "mailer", "box", "delivery"))
        )
    ):
        return "AMAZON"
    if "UPS" in active and any(k in low for k in ("ups ", "ups driver", "ups vest", "ups uniform", "ups truck")):
        return "UPS"
    if "FEDEX" in active and "fedex" in low:
        return "FEDEX"
    if "USPS" in active and any(k in low for k in ("usps", "postal carrier", "mail carrier", "mail truck")):
        return "USPS"
    if "LANDSCAPERS" in active and any(k in low for k in ("leaf blower", "lawn mower", "string trimmer", "landscap")):
        return "LANDSCAPERS"
    return "NONE"


def _encode_vlm_image_b64(img_path: str, max_dim: int = 896, quality: int = 84) -> str | None:
    if not img_path or not os.path.exists(img_path):
        return None
    try:
        img = cv2.imread(img_path)
        if img is not None and img.size > 0:
            h, w = img.shape[:2]
            peak = max(h, w)
            if peak > max_dim:
                scale = max_dim / float(peak)
                img = cv2.resize(
                    img,
                    (max(1, int(round(w * scale))), max(1, int(round(h * scale)))),
                    interpolation=cv2.INTER_AREA,
                )
            ok, enc = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
            if ok:
                return base64.b64encode(enc.tobytes()).decode("utf-8")
    except Exception:
        pass
    try:
        with open(img_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    except Exception:
        return None


def _is_isaac_event(sub_type: str | None, full_label: str | None, ai_notes: str | None = None) -> bool:
    u_sub = (sub_type or "").upper()
    u_lbl = (full_label or "").upper()
    u_not = (ai_notes or "").upper()
    return (
        "ID: ISAAC" in u_sub
        or u_lbl.endswith("_ISAAC")
        or "_ISAAC_" in u_lbl
        or "[ISAAC (" in u_not
        or "MATCH: ISAAC" in u_not
    )


RESIDENT_VEHICLES_PATH = "/tmp/command_faces/resident_vehicles.json"


def _load_resident_vehicles() -> list[dict]:
    if not os.path.exists(RESIDENT_VEHICLES_PATH):
        return []
    try:
        with open(RESIDENT_VEHICLES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _match_resident_vehicle(plate: str, vlm_sub: str = "", summary: str = "") -> dict | None:
    clean_p = re.sub(r"[^A-Z0-9]", "", (plate or "").upper())
    for rv in _load_resident_vehicles():
        canon = re.sub(r"[^A-Z0-9]", "", str(rv.get("plate") or "").upper())
        aliases = {
            re.sub(r"[^A-Z0-9]", "", str(a).upper())
            for a in (rv.get("aliases") or [])
        }
        aliases.add(canon)
        if clean_p and clean_p in aliases:
            return rv
    return None


def _is_whitelist_family_event(sub_type: str | None, full_label: str | None, face_role: str | None = None) -> bool:
    u_sub = (sub_type or "").upper()
    if "WATCHLIST" in u_sub or "VISITOR_" in u_sub:
        return False
    if face_role == "whitelist" or u_sub.startswith("ID: ") or u_sub.startswith("🏠 "):
        return True
    return False


def _same_subject_track(ev: DetectionEvent, other: DetectionEvent) -> bool:
    if ev.id == other.id:
        return True
    dt = abs((ev.timestamp - other.timestamp).total_seconds()) if (ev.timestamp and other.timestamp) else 999.0
    if ev.motion_slot and other.motion_slot and ev.motion_slot == other.motion_slot and dt <= 18.0:
        return True
    if dt <= 6.0:
        ecx = float(ev.bbox_x or 0.0) + float(ev.bbox_w or 0.0) * 0.5
        ecy = float(ev.bbox_y or 0.0) + float(ev.bbox_h or 0.0) * 0.5
        ocx = float(other.bbox_x or 0.0) + float(other.bbox_w or 0.0) * 0.5
        ocy = float(other.bbox_y or 0.0) + float(other.bbox_h or 0.0) * 0.5
        if ((ecx - ocx) ** 2 + (ecy - ocy) ** 2) ** 0.5 < 0.20:
            return True
    return False


def _point_in_poly(px: float, py: float, pts: list[dict]) -> bool:
    n = len(pts)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = float(pts[i]["x"]), float(pts[i]["y"])
        xj, yj = float(pts[j]["x"]), float(pts[j]["y"])
        if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / ((yj - yi) + 1e-9) + xi):
            inside = not inside
        j = i
    return inside


def _event_touches_tripwire(ev: DetectionEvent, cam_zones: list[CameraZone]) -> bool:
    bx, by, bw, bh = float(ev.bbox_x or 0.0), float(ev.bbox_y or 0.0), float(ev.bbox_w or 0.0), float(ev.bbox_h or 0.0)
    probe_pts = [
        (bx + bw * 0.5, by + bh * 0.95),
        (bx + bw * 0.25, by + bh * 0.92),
        (bx + bw * 0.75, by + bh * 0.92),
        (bx + bw * 0.5, by + bh * 0.65),
    ]
    for z in cam_zones:
        if z.zone_type != "tripwire":
            continue
        try:
            pts = json.loads(z.points_json)
        except Exception:
            continue
        if any(_point_in_poly(px, py, pts) for px, py in probe_pts):
            return True
    return False


async def _evaluate_best_confidence_push(
    event_id: int,
    face_role: str | None = None,
    recognized_face: str | None = None,
):
    """
    Waits briefly (4.5s) so the 5-second MP4 replay clip finishes encoding and any sibling
    detections from the same person/vehicle walking closer are committed. Then selects ONLY
    the single highest-confidence detection in the encounter window (±30s) and enforces a 90s
    per-camera/category cooldown so the user only receives 1 alert with the best confidence.
    """
    try:
        await asyncio.sleep(4.5)
        wait_ticks = 0
        while GPU_SEMAPHORE.locked() and wait_ticks < 24:
            await asyncio.sleep(0.5)
            wait_ticks += 1

        async with AsyncSessionLocal() as session:
            ev = await session.get(DetectionEvent, event_id)
            if not ev:
                return

            cam_sched = await session.get(_CameraSchedule, ev.camera_id)
            cam_alert_all = bool(cam_sched and getattr(cam_sched, "alert_whitelist", False))
            cam_log_only = bool(cam_sched and getattr(cam_sched, "silent_logging", False))
            push_cfg = await get_push_config()

            win_start = ev.timestamp - _td(seconds=30)
            win_end = ev.timestamp + _td(seconds=30)
            burst_res = await session.execute(
                select(DetectionEvent).where(
                    DetectionEvent.camera_id == ev.camera_id,
                    DetectionEvent.timestamp >= win_start,
                    DetectionEvent.timestamp <= win_end,
                )
            )
            burst_events = burst_res.scalars().all()

        now_ts = time.time()
        is_away = bool(getattr(push_cfg, "away_mode", False))
        away_armed_at = float(getattr(push_cfg, "away_armed_at", 0.0) or 0.0)
        in_away_grace = is_away and (now_ts < away_armed_at)
        away_active = is_away and not in_away_grace
        global_cam_log_only = bool(getattr(push_cfg, "cam_silent_logging", False))
        perim_svc = __import__("app.services.perimeter_security", fromlist=["perimeter_service"]).perimeter_service
        perim_state = perim_svc.alarm_state
        perim_triggered = perim_state == "triggered"

        # ARMED NIGHT: Cameras NEVER trigger alarms or push alerts period — ONLY physical door/window sensors set off the alarm!
        if perim_state == "armed_night":
            return

        # Silent Record & Log Mode (per-camera or master header pill): records all clips, Face ID & VLM notes
        # into Kill-Feed with zero camera push alarms while HOME (auto-arms if AWAY MODE or YoLink Perimeter triggers!)
        if (global_cam_log_only or cam_log_only) and (not away_active) and (not perim_triggered):
            return

        # 1. Silent Service exemption across encounter window; ISAAC exemption scoped to same person/track
        if any((b.sub_type or "").startswith("🔇 SERVICE:") for b in burst_events):
            return

        if _is_isaac_event(ev.sub_type, ev.full_label, ev.ai_notes) or (ev.sub_type or "").startswith("🔇 SERVICE:"):
            return

        for b in burst_events:
            if _same_subject_track(ev, b) and _is_isaac_event(b.sub_type, b.full_label, b.ai_notes):
                return

        # 2. Shield whitelisted family / resident vehicles on the SAME track during normal Home Mode
        if (not away_active) and (not cam_alert_all) and ev.category in ("person", "vehicle"):
            if any(_same_subject_track(ev, b) and _is_whitelist_family_event(b.sub_type, b.full_label) for b in burst_events):
                return

        # 2b. Never fire a generic 'person' TRIPWIRE BREACH when a sibling frame in the same driveway encounter
        # is a confirmed delivery/service carrier (e.g. courier vest/logo confirmed walking up or back from porch).
        if ev.category == "person" and any(
            b.id != ev.id
            and b.category == "delivery"
            and (_same_subject_track(ev, b) or ("BREACH" in (ev.full_label or "")))
            for b in burst_events
        ):
            return

        def _cat_group(c: str | None) -> str:
            return "person_or_delivery" if c in ("person", "delivery") else (c or "other")

        my_group = _cat_group(ev.category)
        is_indoor_cam = ev.camera_id in ("tapo_1", "tapo_2")

        def _qualifies_and_score(cand: DetectionEvent) -> tuple[bool, tuple]:
            c_sub = cand.sub_type or ""
            c_lbl = cand.full_label or ""
            if _is_isaac_event(c_sub, c_lbl, cand.ai_notes) or c_sub.startswith("🔇 SERVICE:"):
                return (False, (0, 0, 0, 0, 0, 0.0, 0))

            c_watch = "WATCHLIST" in c_sub or (
                "watchlist" if cand.id == ev.id and face_role in ("watchlist", "watchlist_auto") else ""
            ) != ""
            c_breach = "BREACH" in c_lbl
            c_deliv = cand.category == "delivery" and c_sub.startswith("📦 DELIVERY:")
            c_fam = _is_whitelist_family_event(c_sub, c_lbl, face_role if cand.id == ev.id else None)
            c_indoor_stranger = is_indoor_cam and cand.category == "person" and not c_fam

            qualifies = False
            if c_watch and (push_cfg.notify_watchlist or cam_alert_all or away_active):
                qualifies = True
            elif away_active and cand.category in ("person", "delivery", "vehicle"):
                # Whole household is away: alert on ANY person, delivery, or vehicle on ANY camera
                qualifies = True
            elif cam_alert_all:
                qualifies = True
            elif not c_fam:
                if (
                    ((c_breach or c_indoor_stranger) and push_cfg.notify_breach)
                    or (c_deliv and push_cfg.notify_delivery)
                ):
                    qualifies = True

            has_real_id_or_plate = 1 if (
                "[PLATE:" in c_sub or (c_sub.startswith("ID: ") and "VISITOR_" not in c_sub)
            ) else 0
            score = (
                1 if c_watch else 0,
                1 if c_indoor_stranger else 0,
                1 if c_deliv else 0,
                1 if c_breach else 0,
                has_real_id_or_plate,
                round(float(cand.confidence or 0.0), 4),
                cand.id,
            )
            return (qualifies, score)

        my_qualifies, my_score = _qualifies_and_score(ev)
        if not my_qualifies:
            return

        # 3. Only the single highest-scoring/highest-confidence event in the ±30s burst sends the alert
        for sibling in burst_events:
            if sibling.id == ev.id or _cat_group(sibling.category) != my_group:
                continue
            sib_qualifies, sib_score = _qualifies_and_score(sibling)
            if sib_qualifies and sib_score > my_score:
                return

        snap_id = (ev.attributes or "").split("|")[0].strip()
        crop_path = f"{SNAPSHOT_DIR}/crop_{snap_id}" if snap_id else None
        full_path = f"{SNAPSHOT_DIR}/full_{snap_id}" if snap_id else None
        plate_path = f"{SNAPSHOT_DIR}/plate_{snap_id}" if snap_id else None
        clip_path = f"{SNAPSHOT_DIR}/clip_{snap_id}.mp4" if snap_id else None
        ai_notes_out = ev.ai_notes or (ev.attributes.split("|", 1)[1].strip() if "|" in (ev.attributes or "") else "")

        cam_friendly = CAM_NAMES.get(ev.camera_id, ev.camera_id.upper())
        is_watchlist = face_role in ("watchlist", "watchlist_auto") or "WATCHLIST" in (ev.sub_type or "")
        is_breach = "BREACH" in (ev.full_label or "")
        is_delivery_alert = ev.category == "delivery" and (ev.sub_type or "").startswith("📦 DELIVERY:")
        is_family_whitelist = _is_whitelist_family_event(ev.sub_type, ev.full_label, face_role)
        is_indoor_stranger = is_indoor_cam and ev.category == "person" and not is_family_whitelist
        dedup_key = f"{ev.camera_id}:{my_group}"

        if is_watchlist and (push_cfg.notify_watchlist or cam_alert_all or away_active):
            await send_tactical_push(
                title=f"🚨 WATCHLIST MATCH // {cam_friendly}",
                message=ai_notes_out or ev.full_label,
                crop_path=crop_path,
                full_path=full_path,
                plate_path=plate_path,
                clip_path=clip_path,
                confidence=ev.confidence,
                priority="urgent",
                tags="rotating_light,skull",
                dedup_key=dedup_key,
            )
        elif away_active:
            # In AWAY MODE, ONLY indoor cameras (tapo_1 = LOFT, tapo_2 = LIVING_ROOM) trigger the Master Bedroom Siren
            # on verified person detections (never annke_1 DRIVEWAY, and never stray animals).
            siren_fired = False
            if is_indoor_cam and ev.category == "person" and not is_family_whitelist:
                siren_fired = await perim_svc.trigger_indoor_camera_alarm(
                    ev.camera_id,
                    cam_friendly,
                    ai_notes_out or ev.full_label,
                )
            if is_delivery_alert:
                title_str = f"🛫 {ev.sub_type} // {cam_friendly}"
                prio_str = "high"
                tag_str = "package,airplane"
            elif is_indoor_cam and ev.category == "person":
                title_str = (
                    f"🚨 AWAY INDOOR SIREN TRIGGERED: {(ev.sub_type or 'PERSON').upper()} // {cam_friendly}"
                    if siren_fired
                    else f"🚨 AWAY INTRUSION: {(ev.sub_type or ev.category).upper()} // {cam_friendly}"
                )
                prio_str = "urgent"
                tag_str = "rotating_light,shield"
            elif is_breach:
                title_str = f"🛫 AWAY DRIVEWAY ALERT: {(ev.sub_type or ev.category).upper()} // {cam_friendly}"
                prio_str = "high"
                tag_str = "shield,airplane"
            else:
                title_str = f"🛫 AWAY ALERT: {(ev.sub_type or ev.category).upper()} // {cam_friendly}"
                prio_str = "high"
                tag_str = "shield,airplane"
            await send_tactical_push(
                title=title_str,
                message=ai_notes_out or ev.full_label,
                crop_path=crop_path,
                full_path=full_path,
                plate_path=plate_path,
                clip_path=clip_path,
                confidence=ev.confidence,
                priority=prio_str,
                tags=tag_str,
                dedup_key=dedup_key,
            )
        elif cam_alert_all:
            if is_family_whitelist:
                fam_name = (ev.sub_type or "").replace("ID:", "").strip() or (recognized_face or "FAMILY")
                await send_tactical_push(
                    title=f"🏠 FAMILY DETECTED: {fam_name} // {cam_friendly}",
                    message=ai_notes_out or ev.full_label,
                    crop_path=crop_path,
                    full_path=full_path,
                    plate_path=plate_path,
                    clip_path=clip_path,
                    confidence=ev.confidence,
                    priority="high",
                    tags="house,bell",
                    dedup_key=f"{ev.camera_id}:family:{fam_name}",
                )
            elif is_breach or is_indoor_stranger:
                await send_tactical_push(
                    title=f"⚠️ {'INDOOR INTRUSION' if is_indoor_stranger and not is_breach else 'TRIPWIRE BREACH'} // {cam_friendly}",
                    message=ai_notes_out or ev.full_label,
                    crop_path=crop_path,
                    full_path=full_path,
                    plate_path=plate_path,
                    clip_path=clip_path,
                    confidence=ev.confidence,
                    priority="high",
                    tags="warning,shield",
                    dedup_key=dedup_key,
                )
            elif is_delivery_alert:
                await send_tactical_push(
                    title=f"{ev.sub_type} // {cam_friendly}",
                    message=ai_notes_out or ev.full_label,
                    crop_path=crop_path,
                    full_path=full_path,
                    plate_path=plate_path,
                    clip_path=clip_path,
                    confidence=ev.confidence,
                    priority="default",
                    tags="package,truck",
                    dedup_key=dedup_key,
                )
            else:
                await send_tactical_push(
                    title=f"🔔 {(ev.sub_type or ev.category).upper()} // {cam_friendly}",
                    message=ai_notes_out or ev.full_label,
                    crop_path=crop_path,
                    full_path=full_path,
                    plate_path=plate_path,
                    clip_path=clip_path,
                    confidence=ev.confidence,
                    priority="default",
                    tags="bell,eye",
                    dedup_key=dedup_key,
                )
        elif not is_family_whitelist:
            if (is_breach or is_indoor_stranger) and push_cfg.notify_breach:
                await send_tactical_push(
                    title=f"⚠️ {'INDOOR INTRUSION' if is_indoor_stranger and not is_breach else 'TRIPWIRE BREACH'} // {cam_friendly}",
                    message=ai_notes_out or ev.full_label,
                    crop_path=crop_path,
                    full_path=full_path,
                    plate_path=plate_path,
                    clip_path=clip_path,
                    confidence=ev.confidence,
                    priority="high",
                    tags="warning,shield",
                    dedup_key=dedup_key,
                )
            elif is_delivery_alert and push_cfg.notify_delivery:
                await send_tactical_push(
                    title=f"{ev.sub_type} // {cam_friendly}",
                    message=ai_notes_out or ev.full_label,
                    crop_path=crop_path,
                    full_path=full_path,
                    plate_path=plate_path,
                    clip_path=clip_path,
                    confidence=ev.confidence,
                    priority="default",
                    tags="package,truck",
                    dedup_key=dedup_key,
                )
    except Exception as e:
        print(f"[PUSH DISPATCH ERROR] {e}")


async def deep_inspect_detection(event_id: int) -> dict:
    async with GPU_SEMAPHORE:
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(DetectionEvent).where(DetectionEvent.id == event_id))
            ev = result.scalar_one_or_none()
            if not ev:
                return {"error": "Event not found"}

            raw_attr = ev.attributes or ""
            snap_id = raw_attr.split("|")[0].strip()
            existing_note = raw_attr.split("|", 1)[1].strip() if "|" in raw_attr else ""

            crop_path = f"{SNAPSHOT_DIR}/crop_{snap_id}" if snap_id else ""
            full_path = f"{SNAPSHOT_DIR}/full_{snap_id}" if snap_id else ""
            plate_path = f"{SNAPSHOT_DIR}/plate_{snap_id}" if snap_id else ""
            clip_path = f"{SNAPSHOT_DIR}/clip_{snap_id}.mp4" if snap_id else ""

            recognized_face = None
            face_role = None
            face_info = None
            face_match_score = 0.0
            had_breach = "BREACH" in (ev.full_label or "")

            cam_zones_res = await session.execute(select(CameraZone).where(CameraZone.camera_id == ev.camera_id))
            cam_zones = cam_zones_res.scalars().all()
            in_tripwire = had_breach or (ev.category == "person" and _event_touches_tripwire(ev, cam_zones))

            if ev.category == "person" and os.path.exists(crop_path):
                face_info = await face_engine.process_snapshot_biometrics(crop_path)
                if face_info:
                    fname = face_info["name"]
                    face_role = face_info["role"]
                    face_match_score = float(face_info.get("score") or 0.0)
                    fsightings = face_info["sightings"]
                    if face_role == "visitor":
                        recognized_face = f"{fname} (Seen {fsightings}x)"
                        ev.sub_type = f"ID: {fname} ({fsightings}x)"
                        if in_tripwire and "BREACH" not in (ev.full_label or ""):
                            ev.full_label = f"{ev.motion_slot}_person_BREACH"
                    elif face_role in ("watchlist", "watchlist_auto"):
                        recognized_face = f"⚠️ WATCHLIST: {fname} (Seen {fsightings}x)"
                        ev.sub_type = f"⚠️ WATCHLIST: {fname}"
                        ev.full_label = f"{ev.motion_slot}_{fname}_BREACH"
                    else:
                        recognized_face = f"{fname} (Seen {fsightings}x)"
                        ev.sub_type = f"ID: {fname}"
                        breach_tail = "_BREACH" if (had_breach and face_match_score < 0.52) else ""
                        ev.full_label = f"{ev.motion_slot}_{fname}{breach_tail}"
                elif (ev.sub_type or "").startswith("ID: "):
                    pre_name = (ev.sub_type or "").replace("ID:", "").strip()
                    recognized_face = pre_name
                    face_role = "visitor" if pre_name.startswith("VISITOR_") else "whitelist"

            gpu_online, models = await probe_workstation_ollama()
            chosen_vlm = pick_best_vlm(models) if gpu_online else None

            rules = _get_delivery_rules()
            active_rule_names = [r["id"] for r in rules]
            rules_text = "; ".join([f"{r['id']} ({r.get('cues', '')})" for r in rules])

            lessons = _get_ai_lessons(ev.camera_id)
            lessons_text = "; ".join([
                f"If detector guessed '{L['wrong_sub_type']}', check if it is actually '{L['correct_sub_type']}' ({L['note']})"
                for L in lessons[-8:]
            ])

            img_to_send = (
                full_path
                if (ev.category == "animal" and os.path.exists(full_path))
                else (crop_path if os.path.exists(crop_path) else full_path)
            )
            ai_notes_out = existing_note
            vlm_succeeded = False

            if chosen_vlm and img_to_send and os.path.exists(img_to_send):
                try:
                    primary_max_dim = 1024 if ev.category == "vehicle" else (960 if ev.category == "animal" else 672)
                    primary_b64 = _encode_vlm_image_b64(img_to_send, max_dim=primary_max_dim, quality=88 if ev.category == "vehicle" else 84)
                    images_b64 = [primary_b64] if primary_b64 else []
                    if ev.category in ("person", "delivery") and full_path and full_path != img_to_send and os.path.exists(full_path):
                        ctx_b64 = _encode_vlm_image_b64(full_path, max_dim=896, quality=82)
                        if ctx_b64:
                            images_b64.append(ctx_b64)
                    elif ev.category == "vehicle" and plate_path and os.path.exists(plate_path):
                        plate_b64 = _encode_vlm_image_b64(plate_path, max_dim=960, quality=92)
                        if plate_b64:
                            images_b64.append(plate_b64)

                    resident_truck_rule = (
                        "RESIDENT PARKED TRUCK — IGNORE COMPLETELY: The red pickup truck (Ford F-150) parked on the left side of the driveway belongs to the homeowner and is permanent background scenery. NEVER mention, describe, or analyze the parked red pickup truck in 'summary' or 'sub_type' unless a person is actively touching, opening, or tampering with it. "
                        if ev.camera_id == "annke_1"
                        else ""
                    )

                    resident_veh_list = _load_resident_vehicles()
                    resident_veh_hint = ""
                    recent_household_car_arrival = False
                    if ev.camera_id == "annke_1" and ev.timestamp:
                        hh_q = await session.execute(
                            select(DetectionEvent.id)
                            .where(
                                DetectionEvent.camera_id == "annke_1",
                                DetectionEvent.category == "vehicle",
                                DetectionEvent.sub_type.like("🏠%"),
                                DetectionEvent.timestamp >= ev.timestamp - _td(minutes=6),
                                DetectionEvent.timestamp <= ev.timestamp + _td(seconds=30),
                            )
                            .limit(1)
                        )
                        recent_household_car_arrival = hh_q.scalar_one_or_none() is not None

                    if ev.camera_id == "annke_1" and resident_veh_list:
                        rv_parts = [
                            f"{rv.get('owner')}'s {rv.get('vehicle_label', 'vehicle')} (plate '{rv.get('plate')}', {rv.get('visual_cues', '')})"
                            for rv in resident_veh_list
                        ]
                        resident_veh_hint = (
                            "KNOWN HOUSEHOLD VEHICLES: "
                            + "; ".join(rv_parts)
                            + ". When a household SUV is parked in the driveway, adults/children walking around it, opening/closing its doors, carrying children, carrying laundry baskets/drinks, or loading/unloading items are HOUSEHOLD ACTIVITY ('category': 'person', 'carrier': 'NONE'). "
                        )

                    prompt = (
                        f"Analyze this security camera image from '{ev.camera_id}' (initial detector guess: category={ev.category}, sub_type={ev.sub_type}). "
                        "For person/delivery detections, Image 1 is the close-up subject crop and Image 2 (if provided) is the full wide scene context. "
                        f"{resident_truck_rule}"
                        f"{resident_veh_hint}"
                        f"USER TAUGHT RULES FOR THIS CAMERA: {lessons_text or 'None'}. "
                        f"CONFIGURED DELIVERY/SERVICE RULES: {rules_text}. "
                        "Note: grayscale images are infrared night vision on dry pavement. "
                        "If this is purely an inanimate false trigger (empty pavement, wind-blown tree/shadow, IR spiderweb, stationary unattended trash bin, or headlight glare with NO actual person, vehicle, or animal present), set 'valid_target' to false and 'category' to 'false_alarm'. "
                        "If a person is walking an animal on a sidewalk/leash, classify sub_type as 'dog' and mention the owner/leash in summary. "
                        "If a small four-legged animal is alone on the driveway/pavement with NO person or leash visible, classify sub_type as 'cat'. "
                        "Populate 'plate' ONLY when 'category' is 'vehicle' (an actively moving/arriving/departing vehicle with a legible plate; omit the central Oregon tree icon between digits and letters). For all person, delivery, or animal detections, ALWAYS set 'plate' to 'NONE' (NEVER read license plates off parked background cars). "
                        f"Set 'carrier' to one of {active_rule_names} when clear delivery/service evidence is visible across Image 1 (subject crop) and Image 2 (wide driveway/street context): LANDSCAPERS (mowers/blowers/trimmers/crew vests), AMAZON (Amazon smile arrow logo OR two-tone sky-blue/cyan and dark-navy reflective delivery vest with horizontal silver/grey stripes — even when the chest/back smile logo is covered by a hoodie or held package — especially when carrying a brown cardboard parcel or brown kraft/white padded shipping mailer + handheld phone/scanner, or walking from a commercial cargo/Amazon van stopped at the curb in Image 2), UPS/FEDEX/USPS (branded carrier vest/uniform, delivery van/truck at curb, or shipping parcel + scanner), or FOOD_DELIVERY (ONLY when a courier approaches from the street holding an insulated rectangular red/black thermal pizza/food bag or flat horizontal cardboard pizza boxes — NEVER set FOOD_DELIVERY for empty-handed walkers, children, restaurant logo T-shirts, drinks/cans, laundry baskets, or people interacting with parked driveway cars); otherwise set 'carrier' to 'NONE'. "
                        "CRITICAL GUARDRAIL: Residents rolling wheeled curbside trash, recycling, or yard-waste bins (blue, black, green, or gray plastic carts with lids/wheels) or carrying open plastic laundry baskets are household activity ('category': 'person', 'carrier': 'NONE'). "
                        "Set 'school_commuter' to true ONLY if the subject is a student/youth wearing a school backpack who is casually walking along the public sidewalk or exiting away toward the street with NO package delivery and NO tampering with vehicles/house. "
                        "Set 'face_visible' to true ONLY if a person's actual facial features (eyes/nose) are clearly visible in Image 1 (the close-up crop); set 'face_visible' to false if walking away, only the back of their head/hoodie/backpack is visible, blurry night silhouette, or head cropped out. "
                        "Respond ONLY with valid JSON: "
                        '{"valid_target": true, "face_visible": false, "school_commuter": false, "category": "person|delivery|vehicle|animal|false_alarm", '
                        '"sub_type": "short label like cat, dog, white suv, or person", '
                        '"plate": "NONE", '
                        f'"carrier": "{"|".join(active_rule_names)}|NONE", '
                        '"summary": "Concise 12-20 word tactical description of the moving subject only"}'
                    )

                    async with httpx.AsyncClient(timeout=35.0) as client:
                        resp = await client.post(
                            f"{WORKSTATION_GPU_URL}/api/generate",
                            json={
                                "model": chosen_vlm,
                                "prompt": prompt,
                                "images": images_b64,
                                "format": "json",
                                "stream": False,
                                "options": {"temperature": 0.1, "num_ctx": 8192},
                            },
                        )
                        if resp.status_code != 200 and len(images_b64) > 1:
                            print(f"[DEEP INSPECT VLM HTTP {resp.status_code}] Retrying single-image fallback: {resp.text[:200]}")
                            resp = await client.post(
                                f"{WORKSTATION_GPU_URL}/api/generate",
                                json={
                                    "model": chosen_vlm,
                                    "prompt": prompt,
                                    "images": images_b64[:1],
                                    "format": "json",
                                    "stream": False,
                                    "options": {"temperature": 0.1, "num_ctx": 8192},
                                },
                            )
                        elif resp.status_code != 200:
                            print(f"[DEEP INSPECT VLM HTTP {resp.status_code}] {resp.text[:240]}")

                        if resp.status_code == 200:
                            parsed = extract_json_dict(resp.json().get("response", "{}"))
                            vlm_succeeded = True
                            valid_target = bool(parsed.get("valid_target", True))
                            face_visible = bool(parsed.get("face_visible", True))
                            school_commuter = bool(parsed.get("school_commuter", False))
                            vlm_cat = str(parsed.get("category") or ev.category).strip().lower()
                            summary = (parsed.get("summary") or "").strip()
                            if ev.camera_id == "annke_1":
                                summary = _scrub_parked_red_truck(summary)
                            carrier = (parsed.get("carrier") or "NONE").strip().upper()
                            raw_plate = str(parsed.get("plate") or "NONE").strip().upper()
                            vlm_sub = (parsed.get("sub_type") or "").strip().lower()

                            # VLM Sanity Veto: if no face is visible (back of head, backpack, or waist-down crop),
                            # strip any bogus/inherited biometric ID and restore tripwire BREACH if inside zone.
                            if (
                                ev.category == "person"
                                and not face_visible
                                and recognized_face
                                and face_role not in ("watchlist", "watchlist_auto")
                                and face_match_score < 0.56
                            ):
                                if face_info and face_info.get("auto_enrolled") and str(face_info.get("name", "")).startswith("VISITOR_"):
                                    await session.execute(delete(_KnownFace).where(_KnownFace.name == face_info["name"]))
                                recognized_face = None
                                face_role = None
                                ev.sub_type = "person"
                                ev.full_label = f"{ev.motion_slot}_person{'_BREACH' if in_tripwire else ''}"

                            # Purge confirmed inanimate VLM false alarms before any alert can fire
                            if (not valid_target or vlm_cat == "false_alarm") and not recognized_face:
                                if face_info and face_info.get("auto_enrolled") and str(face_info.get("name", "")).startswith("VISITOR_"):
                                    await session.execute(delete(_KnownFace).where(_KnownFace.name == face_info["name"]))
                                await session.execute(delete(DetectionEvent).where(DetectionEvent.id == ev.id))
                                await session.commit()
                                for fp in (crop_path, full_path, plate_path, clip_path):
                                    if fp and os.path.exists(fp):
                                        try:
                                            os.remove(fp)
                                        except OSError:
                                            pass
                                return {"id": ev.id, "purged": True, "ai_notes": "Purged inanimate VLM false alarm"}

                            if vlm_cat in ("person", "vehicle", "animal") and vlm_cat != ev.category and not recognized_face:
                                ev.category = vlm_cat

                            rule_map = {r["id"]: r for r in rules}
                            effective_cat = "delivery" if (carrier in rule_map and face_role != "whitelist") else ev.category
                            plate = _sanitize_plate(raw_plate, effective_cat)

                            # Auto-purge routine school-backpack sidewalk walkers (and morning school departures exiting away
                            # from the house) during weekday commute hours so they never bloat HDD storage or trigger alerts.
                            if (
                                ev.camera_id == "annke_1"
                                and ev.category == "person"
                                and carrier not in rule_map
                                and face_role not in ("whitelist", "watchlist", "watchlist_auto")
                                and (school_commuter or "backpack" in summary.lower())
                                and ev.timestamp
                            ):
                                push_cfg_now = await get_push_config()
                                away_now = bool(getattr(push_cfg_now, "away_mode", False)) and (
                                    time.time() >= float(getattr(push_cfg_now, "away_armed_at", 0.0) or 0.0)
                                )
                                if not away_now:
                                    dt_loc = ev.timestamp.astimezone(_LOCAL_TZ)
                                    hm_loc = dt_loc.strftime("%H:%M")
                                    in_school_win = dt_loc.weekday() < 5 and (
                                        ("07:15" <= hm_loc <= "09:05") or ("14:10" <= hm_loc <= "16:35")
                                    )
                                    cx_ev = float(ev.bbox_x or 0.0) + float(ev.bbox_w or 0.0) * 0.5
                                    fy_ev = float(ev.bbox_y or 0.0) + float(ev.bbox_h or 0.0) * 0.95
                                    on_sidewalk_ev = (not in_tripwire) or (fy_ev <= (0.34 + max(0.0, cx_ev - 0.62) * 0.52))
                                    exiting_ids: list[DetectionEvent] = []
                                    is_exiting_school_departure = False
                                    if in_school_win and not on_sidewalk_ev and ev.motion_slot:
                                        sib_q = await session.execute(
                                            select(DetectionEvent)
                                            .where(
                                                DetectionEvent.camera_id == "annke_1",
                                                DetectionEvent.motion_slot == ev.motion_slot,
                                                DetectionEvent.category == "person",
                                                DetectionEvent.timestamp >= ev.timestamp - _td(seconds=8),
                                                DetectionEvent.timestamp <= ev.timestamp + _td(seconds=4),
                                            )
                                            .order_by(DetectionEvent.timestamp.asc())
                                        )
                                        exiting_ids = list(sib_q.scalars().all())
                                        if exiting_ids:
                                            first_fy = float(exiting_ids[0].bbox_y or 0.0) + float(exiting_ids[0].bbox_h or 0.0) * 0.95
                                            last_fy = float(exiting_ids[-1].bbox_y or 0.0) + float(exiting_ids[-1].bbox_h or 0.0) * 0.95
                                            if first_fy >= 0.78 and (last_fy < first_fy - 0.06 or len(exiting_ids) == 1 and hm_loc <= "09:05"):
                                                is_exiting_school_departure = True

                                    if on_sidewalk_ev or is_exiting_school_departure:
                                        if face_info and face_info.get("auto_enrolled") and str(face_info.get("name", "")).startswith("VISITOR_"):
                                            await session.execute(delete(_KnownFace).where(_KnownFace.name == face_info["name"]))
                                        rows_to_del = exiting_ids if is_exiting_school_departure else [ev]
                                        for del_row in rows_to_del:
                                            d_snap = (del_row.attributes or "").split("|")[0].strip()
                                            await session.execute(delete(DetectionEvent).where(DetectionEvent.id == del_row.id))
                                            if d_snap:
                                                for fp in (
                                                    f"{SNAPSHOT_DIR}/crop_{d_snap}",
                                                    f"{SNAPSHOT_DIR}/full_{d_snap}",
                                                    f"{SNAPSHOT_DIR}/plate_{d_snap}",
                                                    f"{SNAPSHOT_DIR}/clip_{d_snap}.mp4",
                                                ):
                                                    if os.path.exists(fp):
                                                        try:
                                                            os.remove(fp)
                                                        except OSError:
                                                            pass
                                        await session.commit()
                                        return {"id": ev.id, "purged": True, "ai_notes": "Purged routine school-commute backpack walker"}
                            if carrier == "NONE" and face_role != "whitelist":
                                carrier = _infer_carrier_from_summary(summary, active_rule_names)
                            if carrier == "FOOD_DELIVERY" and not _is_valid_food_delivery(
                                summary, in_tripwire, recent_household_car_arrival
                            ):
                                carrier = "NONE"

                            if carrier in rule_map and face_role != "whitelist":
                                rule = rule_map[carrier]
                                recent_same_deliv = False
                                if ev.timestamp:
                                    rd_q = await session.execute(
                                        select(DetectionEvent.id)
                                        .where(
                                            DetectionEvent.camera_id == ev.camera_id,
                                            DetectionEvent.id != ev.id,
                                            DetectionEvent.category == "delivery",
                                            DetectionEvent.sub_type.ilike(f"%{carrier}%"),
                                            DetectionEvent.timestamp >= ev.timestamp - _td(seconds=60),
                                            DetectionEvent.timestamp <= ev.timestamp + _td(seconds=10),
                                        )
                                        .limit(1)
                                    )
                                    recent_same_deliv = rd_q.scalar_one_or_none() is not None
                                if not recent_same_deliv:
                                    _record_delivery_sighting(carrier, _dt.now(_tz.utc).isoformat())
                                if face_info and face_info.get("auto_enrolled") and str(face_info.get("name", "")).startswith("VISITOR_"):
                                    vf_rows = (
                                        await session.execute(
                                            select(_KnownFace).where(_KnownFace.name == face_info["name"])
                                        )
                                    ).scalars().all()
                                    for vf in vf_rows:
                                        if vf.sample_crop and str(vf.sample_crop).startswith("/face_crops/"):
                                            dcrop = f"/tmp/command_faces/{os.path.basename(vf.sample_crop)}"
                                            if os.path.exists(dcrop):
                                                try:
                                                    os.remove(dcrop)
                                                except OSError:
                                                    pass
                                        await session.execute(delete(_KnownFace).where(_KnownFace.id == vf.id))
                                    recognized_face = None
                                    face_role = None
                                ev.category = "delivery"
                                plate_str = f" [PLATE: {plate}]" if plate else ""
                                if rule.get("alert_mode") == "silent":
                                    ev.sub_type = f"🔇 SERVICE: {carrier}{plate_str}"
                                    ev.full_label = f"{ev.motion_slot}_SERVICE_{carrier}"
                                else:
                                    ev.sub_type = f"📦 DELIVERY: {carrier}{plate_str}"
                                    ev.full_label = f"{ev.motion_slot}_DELIVERY_{carrier}"
                            elif ev.category == "animal":
                                if "cat" in vlm_sub or "cat" in summary.lower() or "feline" in summary.lower():
                                    ev.sub_type = "cat"
                                    ev.full_label = f"{ev.motion_slot}_cat"
                                elif "dog" in vlm_sub or "dog" in summary.lower() or "leash" in summary.lower():
                                    ev.sub_type = "dog"
                                    ev.full_label = f"{ev.motion_slot}_dog"
                                elif vlm_sub and vlm_sub not in ("none", "animal", "unknown"):
                                    ev.sub_type = vlm_sub[:32]
                                    ev.full_label = f"{ev.motion_slot}_{vlm_sub.replace(' ', '_')[:24]}"
                            elif ev.category == "vehicle":
                                matched_rv = _match_resident_vehicle(plate, vlm_sub, summary)
                                if matched_rv:
                                    plate = str(matched_rv.get("plate") or plate).upper()
                                    owner_up = str(matched_rv.get("owner") or "RESIDENT").upper()
                                    veh_lbl = str(matched_rv.get("vehicle_label") or vlm_sub or "VEHICLE").upper()
                                    ev.sub_type = f"🏠 {owner_up}'S {veh_lbl} [PLATE: {plate}]"[:120]
                                    ev.full_label = f"{ev.motion_slot}_{owner_up}_VEHICLE"[:120]
                                else:
                                    plate_str = f" [PLATE: {plate}]" if plate else " [NO PLATE VIEW]"
                                    desc = vlm_sub.upper() if vlm_sub and vlm_sub != "none" else (ev.sub_type or "VEHICLE").upper()
                                    ev.sub_type = f"🚗 {desc}{plate_str}"[:120]
                                    if plate:
                                        ev.full_label = f"{ev.motion_slot}_{plate.replace(' ', '')}"[:120]

                            face_prefix = f"🧬 [{recognized_face}] " if recognized_face else ""
                            plate_prefix = f"[PLATE: {plate}] " if plate else ""
                            if summary:
                                ai_notes_out = f"{face_prefix}{plate_prefix}[RTX 3060 Ti // {chosen_vlm}] {summary}"
                except Exception as e:
                    print(f"[DEEP INSPECT VLM ERROR] {e}")

            upper_sub = (ev.sub_type or "").upper()
            upper_lbl = (ev.full_label or "").upper()
            upper_face = (recognized_face or "").upper()
            is_isaac = (
                "ISAAC" in upper_face
                or "ID: ISAAC" in upper_sub
                or upper_lbl.endswith("_ISAAC")
                or "_ISAAC_" in upper_lbl
            )
            if is_isaac and "_BREACH" in (ev.full_label or ""):
                ev.full_label = ev.full_label.replace("_BREACH", "")

            # Edge Fallback Note when Windows RTX 3060 Ti (192.168.1.223) is asleep/offline
            if not ai_notes_out:
                breach_prefix = "🚨 [BREACH] " if "BREACH" in (ev.full_label or "") else ""
                face_prefix = f"🧬 [{recognized_face}] " if recognized_face else ""
                conf_pct = int(round(float(ev.confidence or 0.0) * 100))
                ai_notes_out = f"{breach_prefix}{face_prefix}[YOLOv8s EDGE] {(ev.sub_type or ev.category).upper()} ({conf_pct}% conf)"

            ev.ai_notes = ai_notes_out or ev.ai_notes
            ev.attributes = f"{snap_id}|{ai_notes_out}"[:250] if (snap_id and ai_notes_out) else (snap_id or ai_notes_out)[:250]

            # Reconcile earlier un-identified 'person' entry frame on the exact same continuous Motion_slot track
            # when a whitelisted family member steps closer into lighting within 15s (slots stay reserved for 25s).
            if (
                face_role == "whitelist"
                and recognized_face
                and (ev.sub_type or "").startswith("ID: ")
                and ev.motion_slot
                and ev.timestamp
            ):
                clean_wl = (ev.sub_type or "").replace("ID:", "").strip()
                sib_res = await session.execute(
                    select(DetectionEvent).where(
                        DetectionEvent.camera_id == ev.camera_id,
                        DetectionEvent.motion_slot == ev.motion_slot,
                        DetectionEvent.id != ev.id,
                        DetectionEvent.category == "person",
                        DetectionEvent.sub_type == "person",
                        DetectionEvent.timestamp >= ev.timestamp - _td(seconds=15),
                        DetectionEvent.timestamp <= ev.timestamp + _td(seconds=3),
                    )
                )
                for sib in sib_res.scalars().all():
                    sib_snap = (sib.attributes or "").split("|")[0].strip()
                    sib_prev = (sib.ai_notes or "").replace("🚨 [BREACH] ", "")
                    sib_note = f"🧬 [Track-confirmed {clean_wl}] {sib_prev}".strip()
                    sib.sub_type = f"ID: {clean_wl}"
                    sib.full_label = f"{sib.motion_slot}_{clean_wl}"
                    sib.ai_notes = sib_note
                    sib.attributes = f"{sib_snap}|{sib_note}"[:250] if sib_snap else sib_note[:250]

            # Reconcile earlier driveway 'person' (_BREACH) frames from the same courier visit ([-45s, +5s])
            # when a delivery/service logo or vest is confirmed as the courier approaches or walks back from the porch.
            if ev.category == "delivery" and ev.timestamp and (
                (ev.sub_type or "").startswith("📦 DELIVERY:") or (ev.sub_type or "").startswith("🔇 SERVICE:")
            ):
                is_silent_srv = (ev.sub_type or "").startswith("🔇 SERVICE:")
                clean_car = (
                    (ev.sub_type or "")
                    .replace("📦 DELIVERY:", "")
                    .replace("🔇 SERVICE:", "")
                    .split("[")[0]
                    .strip()
                    .upper()
                )
                sib_deliv_res = await session.execute(
                    select(DetectionEvent).where(
                        DetectionEvent.camera_id == ev.camera_id,
                        DetectionEvent.id != ev.id,
                        DetectionEvent.category == "person",
                        DetectionEvent.timestamp >= ev.timestamp - _td(seconds=45),
                        DetectionEvent.timestamp <= ev.timestamp + _td(seconds=5),
                    )
                )
                orphan_visitors: set[str] = set()
                for sib in sib_deliv_res.scalars().all():
                    if _is_whitelist_family_event(sib.sub_type, sib.full_label) or "WATCHLIST" in (sib.sub_type or "").upper():
                        continue
                    if "BREACH" not in (sib.full_label or "") and sib.motion_slot != ev.motion_slot:
                        continue
                    for tok in (sib.sub_type or "").replace("(", " ").replace(")", " ").split():
                        if tok.startswith("VISITOR_") and ("(1x)" in (sib.sub_type or "") or "Seen 1x" in (sib.ai_notes or "")):
                            orphan_visitors.add(tok)
                    sib_snap = (sib.attributes or "").split("|")[0].strip()
                    sib_prev = re.sub(r"^🧬\s*\[VISITOR_\d+[^\]]*\]\s*", "", (sib.ai_notes or "")).replace("🚨 [BREACH] ", "").strip()
                    sib_note = f"📦 [Encounter-confirmed {clean_car}] {sib_prev}".strip()
                    sib.category = "delivery"
                    sib.sub_type = f"🔇 SERVICE: {clean_car}" if is_silent_srv else f"📦 DELIVERY: {clean_car}"
                    sib.full_label = f"{sib.motion_slot}_{'SERVICE' if is_silent_srv else 'DELIVERY'}_{clean_car}"
                    sib.ai_notes = sib_note
                    sib.attributes = f"{sib_snap}|{sib_note}"[:250] if sib_snap else sib_note[:250]

                for vtok in orphan_visitors:
                    vf_res = await session.execute(
                        select(_KnownFace).where(_KnownFace.name == vtok, _KnownFace.role == "visitor", _KnownFace.sightings <= 1)
                    )
                    for vf in vf_res.scalars().all():
                        if vf.sample_crop and str(vf.sample_crop).startswith("/face_crops/"):
                            dcrop = f"/tmp/command_faces/{os.path.basename(vf.sample_crop)}"
                            if os.path.exists(dcrop):
                                try:
                                    os.remove(dcrop)
                                except OSError:
                                    pass
                        await session.execute(delete(_KnownFace).where(_KnownFace.id == vf.id))

            await session.commit()

            asyncio.create_task(_evaluate_best_confidence_push(ev.id, face_role, recognized_face))

            return {
                "id": ev.id,
                "category": ev.category,
                "sub_type": ev.sub_type,
                "full_label": ev.full_label,
                "attributes": ev.attributes,
                "ai_notes": ai_notes_out,
                "engine": f"RTX 3060 Ti // {chosen_vlm}" if vlm_succeeded else "EDGE_FALLBACK",
            }


async def process_command_prompt(prompt: str) -> dict:
    clean = prompt.strip()
    low = clean.lower()

    if any(k in low for k in ("grid", "all cam", "matrix", "reset view", "unfocus")):
        return {
            "reply": "Restored 3-camera Tactical Matrix view.",
            "engine": "DETERMINISTIC_TOOL",
            "ui_action": {"type": "SET_FOCUS", "stream": None},
        }

    for alias, stream in [
        ("driveway", "annke_1"), ("garage", "annke_1"), ("annke", "annke_1"),
        ("loft", "tapo_1"), ("tapo_1", "tapo_1"), ("tapo 1", "tapo_1"),
        ("living", "tapo_2"), ("living_room", "tapo_2"), ("tapo_2", "tapo_2"), ("tapo 2", "tapo_2"),
    ]:
        if alias in low and any(v in low for v in ("focus", "maximize", "pull up", "show", "theater", "zoom", "open")):
            return {
                "reply": f"Promoted {alias.upper()} ({stream}) to primary Theater Mode viewport.",
                "engine": "DETERMINISTIC_TOOL",
                "ui_action": {"type": "SET_FOCUS", "stream": stream},
            }

    if "inspect" in low:
        async with AsyncSessionLocal() as session:
            res = await session.execute(
                select(DetectionEvent).order_by(DetectionEvent.timestamp.desc()).limit(1)
            )
            latest = res.scalar_one_or_none()
        if not latest:
            return {"reply": "No logged detection events available to inspect yet.", "engine": "DETERMINISTIC_TOOL"}
        insp = await deep_inspect_detection(latest.id)
        return {
            "reply": f"Inspected Event #{latest.id}: {insp.get('ai_notes')}",
            "engine": insp.get("engine", "RTX_3060_TI"),
            "ui_action": {"type": "REFRESH_RAIL"},
        }

    async with AsyncSessionLocal() as session:
        total_res = await session.execute(
            select(DetectionEvent.category, func.count(DetectionEvent.id)).group_by(DetectionEvent.category)
        )
        counts = {row[0]: row[1] for row in total_res.all()}

        breach_res = await session.execute(
            select(func.count(DetectionEvent.id)).where(DetectionEvent.full_label.like("%BREACH%"))
        )
        breach_count = breach_res.scalar() or 0

        recent_res = await session.execute(
            select(DetectionEvent).order_by(DetectionEvent.timestamp.desc()).limit(5)
        )
        recent_rows = recent_res.scalars().all()
        zone_res = await session.execute(select(CameraZone))
        zones = zone_res.scalars().all()

    gpu_online, models = await probe_workstation_ollama()
    text_models = [m for m in models if "qwen" in m.lower() or "llama" in m.lower()]

    recent_summary = "; ".join(
        [
            f"#{r.id} {r.camera_id} {r.full_label} ({int(r.confidence*100)}%) at {r.timestamp.strftime('%H:%M:%S')} {r.ai_notes or ''}"
            for r in recent_rows
        ]
    ) or "None"

    if gpu_online and text_models:
        chosen_llm = text_models[0]
        sys_context = (
            "You are COMMAND, the tactical AI operating NasonNation's security & robotics node. "
            "Cameras: DRIVEWAY (annke_1), LOFT (tapo_1), LIVING_ROOM (tapo_2). "
            f"Enrolled Face Vectors: {len(face_engine.gallery)}. "
            f"Database totals: {counts}. Tripwire breaches: {breach_count}. Active zones: {len(zones)}. "
            f"Last 5 detections: {recent_summary}. "
            "Answer the operator's query directly and concisely in 1-2 tactical sentences."
        )
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.post(
                    f"{WORKSTATION_GPU_URL}/api/generate",
                    json={"model": chosen_llm, "system": sys_context, "prompt": clean, "stream": False},
                )
                if resp.status_code == 200:
                    ans = resp.json().get("response", "").strip()
                    if ans:
                        return {"reply": ans, "engine": f"RTX 3060 Ti // {chosen_llm}"}
        except Exception:
            pass

    total_all = sum(counts.values())
    return {
        "reply": (
            f"TELEMETRY SUMMARY: {total_all} verified events | Enrolled Faces: {len(face_engine.gallery)} "
            f"(Person: {counts.get('person', 0)} | Vehicle: {counts.get('vehicle', 0)} | Animal: {counts.get('animal', 0)} | Breaches: {breach_count}). "
            f"Recent: {recent_summary}."
        ),
        "engine": "SQL_TELEMETRY_ENGINE",
    }
