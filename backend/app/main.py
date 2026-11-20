import asyncio
import base64
import glob
import json
import os
import time
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
import cv2
import httpx
import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select, delete, text
from app.api.ws import router as ws_router, telemetry_and_cv_broadcaster
from app.db.session import init_db, AsyncSessionLocal
from app.db.models import DetectionEvent, CameraZone, CameraSchedule, KnownFace, PushSettings
from app.services.cv_pipeline import (
    EdgeCVEngine,
    db_persistence_worker,
    periodic_maintenance_worker,
    health_and_sitrep_worker,
    build_and_send_sitrep,
    SNAPSHOT_DIR,
    LOCAL_TZ,
    is_routine_commute_window,
    get_snapshots_storage_mb,
)
from app.services.face_engine import face_engine, FACE_CROPS_DIR
from app.services.watchlist_scraper import periodic_watchlist_worker, sync_public_mugshot_feeds
from app.services.push_notifier import get_push_config, send_tactical_push
from app.services.discord_bot import (
    discord_bot_worker,
    discord_bot_state,
    notify_bot_config_changed,
    set_away_mode_db,
    set_mute_minutes_db,
    sync_discord_cheatsheet,
)
from app.agent.orchestrator import (
    deep_inspect_detection,
    process_command_prompt,
    _event_touches_tripwire,
    _scrub_parked_red_truck,
)
from app.services.perimeter_security import perimeter_service

STREAMS = ["annke_1", "tapo_1", "tapo_2"]
os.makedirs(SNAPSHOT_DIR, exist_ok=True)
os.makedirs(FACE_CROPS_DIR, exist_ok=True)


class PointDTO(BaseModel):
    x: float
    y: float


class CreateZoneDTO(BaseModel):
    camera_id: str
    name: str
    zone_type: str
    points: list[PointDTO]


class ScheduleDTO(BaseModel):
    mode: str
    start_time: str
    end_time: str
    alert_whitelist: bool = False


class EnrollFaceDTO(BaseModel):
    name: str
    role: str = "whitelist"


class UpdateFaceDTO(BaseModel):
    name: str
    role: str


class UploadFaceImageDTO(BaseModel):
    name: str
    role: str = "watchlist"
    image_base64: str


class ScrapeFaceUrlDTO(BaseModel):
    name: str
    role: str = "watchlist"
    image_url: str


class PushConfigDTO(BaseModel):
    enabled: bool
    ntfy_topic: str
    discord_webhook: str
    discord_bot_token: str | None = None
    notify_breach: bool
    notify_watchlist: bool
    notify_delivery: bool
    daily_sitrep_enabled: bool = True
    daily_sitrep_time: str = "05:00"


class AwayModeDTO(BaseModel):
    away_mode: bool
    grace_seconds: int = 180


class MuteDTO(BaseModel):
    minutes: int = 60


class AgentCommandDTO(BaseModel):
    prompt: str


async def sync_zones_to_engine(cv_engine: EdgeCVEngine):
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(CameraZone))
        rows = result.scalars().all()
        grouped: dict[str, list[dict]] = {s: [] for s in STREAMS}
        for r in rows:
            grouped.setdefault(r.camera_id, []).append({
                "id": r.id,
                "camera_id": r.camera_id,
                "name": r.name,
                "zone_type": r.zone_type,
                "points": json.loads(r.points_json),
            })
        cv_engine.set_zones(grouped)
        return grouped


async def sync_schedules_to_engine(cv_engine: EdgeCVEngine):
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(CameraSchedule))
        rows = result.scalars().all()
        sched_map = {
            s: {
                "camera_id": s,
                "mode": "schedule" if s in ("tapo_1", "tapo_2") else "always_record",
                "start_time": "23:30" if s in ("tapo_1", "tapo_2") else "21:00",
                "end_time": "05:00" if s in ("tapo_1", "tapo_2") else "06:00",
                "alert_whitelist": False,
            }
            for s in STREAMS
        }
        for r in rows:
            sched_map[r.camera_id] = {
                "camera_id": r.camera_id,
                "mode": r.mode,
                "start_time": r.start_time,
                "end_time": r.end_time,
                "alert_whitelist": bool(getattr(r, "alert_whitelist", False)),
            }
        cv_engine.set_schedules(sched_map)
        return cv_engine.get_schedules_status()


async def _cleanup_recent_headless_false_ids():
    """
    Repairs recent person events (last 24h) that were mislabeled `ID: ...` from poisoned dynamic vectors
    when the saved person crop contains zero valid facial features, restoring `_BREACH` if inside a tripwire.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    repaired = 0
    async with AsyncSessionLocal() as session:
        zones_res = await session.execute(select(CameraZone))
        zones_by_cam: dict[str, list[CameraZone]] = {}
        for z in zones_res.scalars().all():
            zones_by_cam.setdefault(z.camera_id, []).append(z)

        ev_res = await session.execute(
            select(DetectionEvent)
            .where(
                DetectionEvent.category == "person",
                DetectionEvent.timestamp >= cutoff,
                DetectionEvent.sub_type.like("ID:%"),
            )
            .order_by(DetectionEvent.timestamp.desc())
            .limit(40)
        )
        for ev in ev_res.scalars().all():
            snap_id = (ev.attributes or "").split("|")[0].strip()
            crop_p = f"{SNAPSHOT_DIR}/crop_{snap_id}" if snap_id else ""
            has_real_face = False
            if crop_p and os.path.exists(crop_p):
                img = cv2.imread(crop_p)
                if img is not None and img.size > 0:
                    vec, _, _ = face_engine.extract_person_crop_face(img, for_enrollment=False)
                    has_real_face = vec is not None
            if not has_real_face:
                in_tw = "BREACH" in (ev.full_label or "") or _event_touches_tripwire(
                    ev, zones_by_cam.get(ev.camera_id, [])
                )
                slot = ev.motion_slot or "Motion_1"
                ev.sub_type = "person"
                ev.full_label = f"{slot}_person{'_BREACH' if in_tw else ''}"
                old_note = ev.ai_notes or ""
                if "]" in old_note and old_note.startswith("🧬 ["):
                    old_note = old_note.split("]", 1)[1].strip()
                ev.ai_notes = old_note or "[Biometric sanitizer cleared non-face false ID]"
                ev.attributes = f"{snap_id}|{ev.ai_notes}"[:250] if snap_id else ev.ai_notes[:250]
                repaired += 1
        if repaired:
            await session.commit()
            print(f"[FACE SANITIZER] Repaired {repaired} recent non-face DetectionEvent card(s).")


async def purge_routine_walkers_and_scrub_notes() -> dict:
    """
    1. Adjusts annke_1 TRIPWIRE_1 far-right sidewalk clearance (y=0.45 -> 0.54) so angled sidewalk walkers never false-breach.
    2. Purges routine Mon-Fri school-commute sidewalk walkers & backpack students (and deletes their JPG/MP4 files on HDD).
    3. Scrubs '[PLATE: ABC1234 OR NONE]' and parked red Ford F-150 background chatter from remaining AI notes.
    """
    purged_count = 0
    freed_bytes = 0
    scrubbed_notes = 0
    purged_visitor_tokens: set[str] = set()

    async with AsyncSessionLocal() as session:
        # 1. Ensure annke_1 TRIPWIRE_1 clears the angled right-side sidewalk beside the utility box
        tw_res = await session.execute(
            select(CameraZone).where(CameraZone.camera_id == "annke_1", CameraZone.name == "TRIPWIRE_1")
        )
        for z in tw_res.scalars().all():
            try:
                pts = json.loads(z.points_json)
                changed_pts = False
                for pt in pts:
                    if abs(float(pt.get("x", 0.0)) - 0.998) < 0.01 and abs(float(pt.get("y", 0.0)) - 0.45) < 0.02:
                        pt["y"] = 0.54
                        changed_pts = True
                if changed_pts:
                    z.points_json = json.dumps(pts)
            except Exception:
                pass

        # 2. Identify routine school-commute / backpack walker rows on annke_1
        all_res = await session.execute(select(DetectionEvent).order_by(DetectionEvent.id.asc()))
        all_rows = list(all_res.scalars().all())

        seed_purge_rows: list[DetectionEvent] = []
        for ev in all_rows:
            if ev.camera_id != "annke_1" or ev.category != "person" or not ev.timestamp:
                continue
            u_sub = (ev.sub_type or "").upper()
            u_lbl = (ev.full_label or "").upper()
            if any(k in u_sub or k in u_lbl for k in ("ISAAC", "JENNIFER", "BEAU", "LYLA", "WATCHLIST", "DELIVERY", "SERVICE")):
                continue
            dt_loc = ev.timestamp.astimezone(LOCAL_TZ)
            in_commute = is_routine_commute_window(dt_loc)
            cx = float(ev.bbox_x or 0.0) + float(ev.bbox_w or 0.0) * 0.5
            fy = float(ev.bbox_y or 0.0) + float(ev.bbox_h or 0.0) * 0.95
            on_sidewalk = fy <= (0.34 + max(0.0, cx - 0.62) * 0.52)
            has_backpack = "backpack" in (ev.ai_notes or "").lower()
            if (in_commute and (on_sidewalk or has_backpack)) or (on_sidewalk and has_backpack):
                seed_purge_rows.append(ev)

        purge_ids = {r.id for r in seed_purge_rows}
        # Include same-slot sibling frames within ±5s of a confirmed backpack commute track
        for ev in all_rows:
            if ev.id in purge_ids or ev.camera_id != "annke_1" or ev.category != "person" or not ev.timestamp:
                continue
            u_sub = (ev.sub_type or "").upper()
            u_lbl = (ev.full_label or "").upper()
            if any(k in u_sub or k in u_lbl for k in ("ISAAC", "JENNIFER", "BEAU", "LYLA", "WATCHLIST", "DELIVERY", "SERVICE")):
                continue
            if any(
                s.motion_slot == ev.motion_slot
                and "backpack" in (s.ai_notes or "").lower()
                and abs((ev.timestamp - s.timestamp).total_seconds()) <= 5.0
                for s in seed_purge_rows
            ):
                purge_ids.add(ev.id)

        remaining_notes_blob = []
        for ev in all_rows:
            snap_id = (ev.attributes or "").split("|")[0].strip()
            if ev.id in purge_ids:
                for tok in (ev.sub_type or "").replace("(", " ").replace(")", " ").split():
                    if tok.startswith("VISITOR_"):
                        purged_visitor_tokens.add(tok)
                if snap_id:
                    for fp in (
                        f"{SNAPSHOT_DIR}/crop_{snap_id}",
                        f"{SNAPSHOT_DIR}/full_{snap_id}",
                        f"{SNAPSHOT_DIR}/plate_{snap_id}",
                        f"{SNAPSHOT_DIR}/clip_{snap_id}.mp4",
                    ):
                        if os.path.exists(fp):
                            try:
                                freed_bytes += os.path.getsize(fp)
                                os.remove(fp)
                            except OSError:
                                pass
                await session.execute(delete(DetectionEvent).where(DetectionEvent.id == ev.id))
                purged_count += 1
            else:
                old_note = ev.ai_notes or ""
                new_note = (
                    old_note.replace("[PLATE: ABC1234 OR NONE] ", "")
                    .replace("[PLATE: ABC1234] ", "")
                    .replace("[PLATE: NONE] ", "")
                )
                if ev.camera_id == "annke_1" and ev.category != "vehicle":
                    new_note = _scrub_parked_red_truck(new_note)
                if new_note != old_note:
                    ev.ai_notes = new_note
                    ev.attributes = f"{snap_id}|{new_note}"[:250] if snap_id else new_note[:250]
                    scrubbed_notes += 1
                remaining_notes_blob.append((ev.sub_type or "") + " " + (ev.ai_notes or ""))

        joined_remaining = "\n".join(remaining_notes_blob)
        for vtok in purged_visitor_tokens:
            if vtok not in joined_remaining:
                vf_res = await session.execute(
                    select(KnownFace).where(KnownFace.name == vtok, KnownFace.role == "visitor")
                )
                for vf in vf_res.scalars().all():
                    if vf.sample_crop and str(vf.sample_crop).startswith("/face_crops/"):
                        disk_crop = f"{FACE_CROPS_DIR}/{os.path.basename(vf.sample_crop)}"
                        if os.path.exists(disk_crop):
                            try:
                                os.remove(disk_crop)
                            except OSError:
                                pass
                    await session.execute(delete(KnownFace).where(KnownFace.id == vf.id))

        await session.commit()

    freed_mb = round(freed_bytes / 1048576.0, 2)
    if purged_count or scrubbed_notes:
        print(
            f"[COMMUTE & NOTE CLEANUP] Purged {purged_count} routine walker/backpack events "
            f"(freed {freed_mb} MB), scrubbed {scrubbed_notes} AI note(s)."
        )
    return {"purged_events": purged_count, "freed_mb": freed_mb, "scrubbed_notes": scrubbed_notes}


async def purge_perimeter_breach_events() -> dict:
    purged_rows = 0
    removed_files = 0
    async with AsyncSessionLocal() as session:
        res = await session.execute(
            select(DetectionEvent).where(
                (DetectionEvent.motion_slot == "Perimeter")
                | (DetectionEvent.full_label == "Perimeter_SENSOR_BREACH")
            )
        )
        rows = res.scalars().all()
        for r in rows:
            snap_id = (r.attributes or "").split("|")[0].strip() if r.attributes else ""
            if snap_id and snap_id.endswith(".jpg"):
                for fp in (f"{SNAPSHOT_DIR}/crop_{snap_id}", f"{SNAPSHOT_DIR}/full_{snap_id}"):
                    if os.path.exists(fp):
                        try:
                            os.remove(fp)
                            removed_files += 1
                        except OSError:
                            pass
            await session.execute(delete(DetectionEvent).where(DetectionEvent.id == r.id))
            purged_rows += 1
        await session.commit()

    for orphan_fp in glob.glob(f"{SNAPSHOT_DIR}/*perimeter_*.jpg"):
        try:
            os.remove(orphan_fp)
            removed_files += 1
        except OSError:
            pass

    if purged_rows or removed_files:
        print(f"[PERIMETER CLEANUP] Purged {purged_rows} perimeter breach event(s) and {removed_files} snapshot file(s).")
    return {"purged_events": purged_rows, "removed_files": removed_files}


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await face_engine.sanitize_gallery_on_startup()
    await _cleanup_recent_headless_false_ids()
    await purge_routine_walkers_and_scrub_notes()
    test_breach_marker = "/tmp/command_backups/.purged_initial_test_breaches_v74"
    if not os.path.exists(test_breach_marker):
        await purge_perimeter_breach_events()
        try:
            os.makedirs("/tmp/command_backups", exist_ok=True)
            with open(test_breach_marker, "w", encoding="utf-8") as mf:
                mf.write(str(int(time.time())))
        except OSError:
            pass
    loop = asyncio.get_running_loop()
    event_queue: asyncio.Queue = asyncio.Queue()

    cv_engine = EdgeCVEngine(streams=STREAMS, event_queue=event_queue, loop=loop)
    app.state.cv_engine = cv_engine
    perimeter_service.bind_cv_engine(cv_engine)
    init_push = await get_push_config()
    cv_engine.set_away_and_mute_state(init_push.away_mode, init_push.away_armed_at, init_push.mute_until)
    if init_push.away_mode:
        await perimeter_service.sync_with_away_mode(init_push.away_mode, init_push.away_armed_at)
    await sync_zones_to_engine(cv_engine)
    await sync_schedules_to_engine(cv_engine)
    cv_engine.start()

    ws_task = asyncio.create_task(telemetry_and_cv_broadcaster(cv_engine))
    db_task = asyncio.create_task(db_persistence_worker(event_queue))
    scraper_task = asyncio.create_task(periodic_watchlist_worker())
    maint_task = asyncio.create_task(periodic_maintenance_worker())
    health_task = asyncio.create_task(health_and_sitrep_worker(cv_engine))
    bot_task = asyncio.create_task(discord_bot_worker(cv_engine))
    perim_task = asyncio.create_task(perimeter_service.watchdog_and_mqtt_worker())
    yield
    cv_engine.stop()
    ws_task.cancel()
    db_task.cancel()
    scraper_task.cancel()
    maint_task.cancel()
    health_task.cancel()
    bot_task.cancel()
    perim_task.cancel()


app = FastAPI(title="NasonNation | Command API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/snapshots", StaticFiles(directory=SNAPSHOT_DIR), name="snapshots")
app.mount("/face_crops", StaticFiles(directory=FACE_CROPS_DIR), name="face_crops")
app.include_router(ws_router)


@app.get("/api/health")
async def health_check():
    return {
        "status": "ONLINE",
        "system": "NasonNation | Command",
        "enrolled_faces": len(face_engine.gallery),
        "storage_mb": get_snapshots_storage_mb(),
    }


@app.get("/api/push/settings")
async def read_push_settings():
    cfg = await get_push_config()
    now = time.time()
    return {
        "enabled": cfg.enabled,
        "ntfy_topic": cfg.ntfy_topic,
        "discord_webhook": cfg.discord_webhook,
        "discord_bot_token": getattr(cfg, "discord_bot_token", "") or "",
        "notify_breach": cfg.notify_breach,
        "notify_watchlist": cfg.notify_watchlist,
        "notify_delivery": cfg.notify_delivery,
        "away_mode": bool(getattr(cfg, "away_mode", False)),
        "away_grace_remaining": max(0, int(round((getattr(cfg, "away_armed_at", 0.0) or 0.0) - now))) if getattr(cfg, "away_mode", False) else 0,
        "mute_remaining": max(0, int(round((getattr(cfg, "mute_until", 0.0) or 0.0) - now))),
        "daily_sitrep_enabled": bool(getattr(cfg, "daily_sitrep_enabled", True)),
        "daily_sitrep_time": getattr(cfg, "daily_sitrep_time", "05:00") or "05:00",
        "bot_status": discord_bot_state,
    }


@app.post("/api/push/settings")
async def save_push_settings(payload: PushConfigDTO):
    async with AsyncSessionLocal() as session:
        cfg = await session.get(PushSettings, 1)
        if not cfg:
            cfg = PushSettings(id=1)
            session.add(cfg)
        cfg.enabled = payload.enabled
        cfg.ntfy_topic = payload.ntfy_topic.strip()
        cfg.discord_webhook = payload.discord_webhook.strip()
        if payload.discord_bot_token is not None:
            cfg.discord_bot_token = payload.discord_bot_token.strip()
        cfg.notify_breach = payload.notify_breach
        cfg.notify_watchlist = payload.notify_watchlist
        cfg.notify_delivery = payload.notify_delivery
        cfg.daily_sitrep_enabled = payload.daily_sitrep_enabled
        cfg.daily_sitrep_time = (payload.daily_sitrep_time or "05:00").strip()
        await session.commit()
    notify_bot_config_changed()
    return await read_push_settings()


@app.post("/api/away")
async def toggle_away_mode_endpoint(payload: AwayModeDTO):
    state = await set_away_mode_db(app.state.cv_engine, payload.away_mode, payload.grace_seconds)
    schedules = await sync_schedules_to_engine(app.state.cv_engine)
    return {**state, "schedules": schedules}


@app.post("/api/push/mute")
async def mute_push_endpoint(payload: MuteDTO):
    return await set_mute_minutes_db(app.state.cv_engine, payload.minutes)


@app.post("/api/push/sitrep")
async def trigger_manual_sitrep():
    return await build_and_send_sitrep(
        app.state.cv_engine,
        silent=True,
        header="📸 ON-DEMAND SILENT SITREP // BOSSMAN",
    )


@app.post("/api/push/cheatsheet")
async def trigger_discord_cheatsheet():
    return await sync_discord_cheatsheet(force_new=True)


@app.post("/api/push/test")
async def trigger_test_push():
    crop_path, full_path, plate_path, clip_path, conf = None, None, None, None, 0.96
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(DetectionEvent).order_by(DetectionEvent.timestamp.desc()).limit(1))
        latest = res.scalar_one_or_none()
        if latest and latest.attributes:
            snap_id = latest.attributes.split("|")[0].strip()
            if snap_id:
                crop_path = f"{SNAPSHOT_DIR}/crop_{snap_id}"
                full_path = f"{SNAPSHOT_DIR}/full_{snap_id}"
                plate_path = f"{SNAPSHOT_DIR}/plate_{snap_id}"
                clip_path = f"{SNAPSHOT_DIR}/clip_{snap_id}.mp4"
                conf = latest.confidence
    return await send_tactical_push(
        title="🛡️ NASONNATION // COMMAND TEST",
        message="Tactical Push Alert link verified from node BOSSMAN (192.168.1.27) — Wide Scene + Crop + 5s MP4 Clip enabled.",
        crop_path=crop_path,
        full_path=full_path,
        plate_path=plate_path,
        clip_path=clip_path,
        confidence=conf,
        priority="high",
        tags="white_check_mark,shield",
        bypass_mute=True,
    )


@app.get("/api/faces")
async def list_enrolled_faces(include_auto: bool = False):
    items = await face_engine.sync_gallery_from_db_and_disk()
    visible = [
        {
            "id": f["id"],
            "name": f["name"],
            "role": "watchlist" if f["role"] == "watchlist_auto" else f["role"],
            "raw_role": f["role"],
            "sample_crop": f["sample_crop"] if str(f["sample_crop"] or "").startswith("/") else None,
            "sightings": f["sightings"],
            "last_seen": f["last_seen"],
            "created_at": f["created_at"],
        }
        for f in items
        if include_auto or f["role"] != "watchlist_auto" or int(f.get("sightings") or 0) > 0
    ]
    return visible


@app.get("/api/faces/watchlist_status")
async def get_watchlist_status():
    auto_count = sum(1 for f in face_engine.gallery if f["role"] == "watchlist_auto")
    return {"auto_watchlist_vectors": auto_count}


@app.post("/api/faces/sync_watchlist")
async def trigger_watchlist_sync():
    return await sync_public_mugshot_feeds(max_new_per_pass=50)


@app.patch("/api/faces/{face_id}")
async def update_face_entry(face_id: int, payload: UpdateFaceDTO):
    clean_name = payload.name.strip().upper()
    if not clean_name:
        raise HTTPException(status_code=400, detail="Name cannot be empty")
    async with AsyncSessionLocal() as session:
        row = await session.get(KnownFace, face_id)
        if not row:
            raise HTTPException(status_code=404, detail="Face vector not found")
        row.name = clean_name
        row.role = payload.role
        await session.commit()
    return await list_enrolled_faces()


@app.delete("/api/faces/{face_id}")
async def delete_face_entry(face_id: int):
    async with AsyncSessionLocal() as session:
        row = await session.get(KnownFace, face_id)
        if not row:
            raise HTTPException(status_code=404, detail="Face vector not found")
        await session.execute(delete(KnownFace).where(KnownFace.id == face_id))
        await session.commit()
    return await list_enrolled_faces()


@app.post("/api/faces/enroll_image")
async def enroll_face_from_uploaded_image(payload: UploadFaceImageDTO):
    clean_name = payload.name.strip().upper()
    if not clean_name:
        raise HTTPException(status_code=400, detail="Name is required")

    b64_str = payload.image_base64
    if "," in b64_str:
        b64_str = b64_str.split(",", 1)[1]

    try:
        raw_bytes = base64.b64decode(b64_str)
        np_arr = np.frombuffer(raw_bytes, np.uint8)
        img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid image file")

    vec, _, face_crop = face_engine.extract_face_data(img)
    if vec is None:
        raise HTTPException(status_code=422, detail="YuNet could not detect a clear face in the uploaded photo.")

    fname = f"upload_{clean_name}_{int(time.time() * 1000)}.jpg"
    cv2.imwrite(f"{FACE_CROPS_DIR}/{fname}", face_crop if face_crop is not None else img, [int(cv2.IMWRITE_JPEG_QUALITY), 92])

    async with AsyncSessionLocal() as session:
        row = KnownFace(
            name=clean_name,
            role=payload.role,
            embedding_json=json.dumps(vec.tolist()),
            sample_crop=f"/face_crops/{fname}",
            sightings=1,
            last_seen=datetime.now(timezone.utc),
        )
        session.add(row)
        await session.commit()

    return await list_enrolled_faces()


@app.post("/api/faces/enroll_url")
async def enroll_face_from_url(payload: ScrapeFaceUrlDTO):
    clean_name = payload.name.strip().upper()
    if not clean_name or not payload.image_url.strip():
        raise HTTPException(status_code=400, detail="Name and Image URL are required")

    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
            resp = await client.get(payload.image_url.strip(), headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200:
                raise HTTPException(status_code=400, detail=f"HTTP {resp.status_code} fetching URL")
            np_arr = np.frombuffer(resp.content, np.uint8)
            img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to download image URL: {e}")

    vec, _, face_crop = face_engine.extract_face_data(img)
    if vec is None:
        raise HTTPException(status_code=422, detail="YuNet could not isolate a clear face from that URL.")

    fname = f"url_{clean_name}_{int(time.time() * 1000)}.jpg"
    cv2.imwrite(f"{FACE_CROPS_DIR}/{fname}", face_crop if face_crop is not None else img, [int(cv2.IMWRITE_JPEG_QUALITY), 92])

    async with AsyncSessionLocal() as session:
        row = KnownFace(
            name=clean_name,
            role=payload.role,
            embedding_json=json.dumps(vec.tolist()),
            sample_crop=f"/face_crops/{fname}",
            sightings=1,
            last_seen=datetime.now(timezone.utc),
        )
        session.add(row)
        await session.commit()

    return await list_enrolled_faces()


@app.post("/api/detections/{event_id}/enroll_face")
async def enroll_face_from_detection(event_id: int, payload: EnrollFaceDTO):
    clean_name = payload.name.strip().upper()
    if not clean_name:
        raise HTTPException(status_code=400, detail="Name cannot be empty")

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(DetectionEvent).where(DetectionEvent.id == event_id))
        ev = result.scalar_one_or_none()
        if not ev or not ev.attributes:
            raise HTTPException(status_code=404, detail="Detection snapshot not found")

        snap_id = (ev.attributes or "").split("|")[0].strip()
        crop_path = f"{SNAPSHOT_DIR}/crop_{snap_id}"

        # Clean up any auto-enrolled VISITOR_XX that was assigned to this event
        old_sub = ev.sub_type or ""
        if "VISITOR_" in old_sub:
            for token in old_sub.replace("(", " ").replace(")", " ").split():
                if token.startswith("VISITOR_"):
                    await session.execute(delete(KnownFace).where(KnownFace.name == token))

        # Strictly extract ONLY from the upper head/shoulders of the target crop (never scan full_path background)
        vec, _, face_crop = (None, 0.0, None)
        if os.path.exists(crop_path):
            vec, _, face_crop = face_engine.extract_person_crop_face(cv2.imread(crop_path), for_enrollment=True)

        ev.sub_type = f"⚠️ WATCHLIST: {clean_name}" if payload.role == "watchlist" else f"ID: {clean_name}"
        ev.full_label = f"{ev.motion_slot}_{clean_name}"

        if vec is not None and face_crop is not None:
            persistent_fname = f"enrolled_{clean_name}_{int(time.time() * 1000)}.jpg"
            cv2.imwrite(f"{FACE_CROPS_DIR}/{persistent_fname}", face_crop, [int(cv2.IMWRITE_JPEG_QUALITY), 92])

            row = KnownFace(
                name=clean_name,
                role=payload.role,
                embedding_json=json.dumps(vec.tolist()),
                sample_crop=f"/face_crops/{persistent_fname}",
                sightings=1,
                last_seen=datetime.now(timezone.utc),
            )
            session.add(row)
            ai_note = f"🧬 [ENROLLED {payload.role.upper()}: {clean_name}]"
            status_str = "ENROLLED"
        else:
            ai_note = f"🏷️ [LABELED {clean_name} — No clear frontal face in crop; vector gallery kept clean]"
            status_str = "LABELED_NO_VECTOR"

        ev.ai_notes = ai_note
        ev.attributes = f"{snap_id}|{ai_note}"[:250] if snap_id else ai_note[:250]
        await session.commit()

    await face_engine.sync_gallery_from_db_and_disk()
    return {
        "status": status_str,
        "name": clean_name,
        "sub_type": ev.sub_type,
        "full_label": ev.full_label,
        "ai_notes": ev.ai_notes,
        "total_faces": len(face_engine.gallery),
    }


@app.get("/api/cameras/schedules")
async def get_camera_schedules():
    return await sync_schedules_to_engine(app.state.cv_engine)


@app.post("/api/cameras/{camera_id}/schedule")
async def update_camera_schedule(camera_id: str, payload: ScheduleDTO):
    async with AsyncSessionLocal() as session:
        existing = await session.get(CameraSchedule, camera_id)
        if existing:
            existing.mode = payload.mode
            existing.start_time = payload.start_time
            existing.end_time = payload.end_time
            existing.alert_whitelist = payload.alert_whitelist
        else:
            session.add(
                CameraSchedule(
                    camera_id=camera_id,
                    mode=payload.mode,
                    start_time=payload.start_time,
                    end_time=payload.end_time,
                    alert_whitelist=payload.alert_whitelist,
                )
            )
        await session.commit()
    return await sync_schedules_to_engine(app.state.cv_engine)


@app.get("/api/zones")
async def get_zones():
    return await sync_zones_to_engine(app.state.cv_engine)


@app.post("/api/zones")
async def create_zone(payload: CreateZoneDTO):
    async with AsyncSessionLocal() as session:
        row = CameraZone(
            camera_id=payload.camera_id,
            name=payload.name,
            zone_type=payload.zone_type,
            points_json=json.dumps([p.model_dump() for p in payload.points]),
        )
        session.add(row)
        await session.commit()
    return await sync_zones_to_engine(app.state.cv_engine)


@app.delete("/api/zones/camera/{camera_id}")
async def clear_camera_zones(camera_id: str):
    async with AsyncSessionLocal() as session:
        await session.execute(delete(CameraZone).where(CameraZone.camera_id == camera_id))
        await session.commit()
    return await sync_zones_to_engine(app.state.cv_engine)


@app.post("/api/detections/heatmap/{camera_id}/clear")
async def clear_camera_heatmap_endpoint(camera_id: str):
    cv_engine = getattr(app.state, "cv_engine", None)
    if cv_engine:
        cv_engine.clear_camera_heatmap(camera_id)
    return {"camera_id": camera_id, "cleared": True, "points": []}


@app.get("/api/detections/heatmap/{camera_id}")
async def get_camera_heatmap(camera_id: str):
    cv_engine = getattr(app.state, "cv_engine", None)
    default_cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    cutoff = (
        max(default_cutoff, cv_engine.heatmap_cleared_at.get(camera_id, default_cutoff))
        if cv_engine
        else default_cutoff
    )

    points = []
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(DetectionEvent)
            .where(DetectionEvent.camera_id == camera_id, DetectionEvent.timestamp >= cutoff)
            .limit(400)
        )
        for r in result.scalars().all():
            cx = round(r.bbox_x + r.bbox_w * 0.5, 4)
            cy = round(r.bbox_y + (r.bbox_h * 0.88 if r.category in ("person", "animal") else r.bbox_h * 0.5), 4)
            points.append({
                "x": cx,
                "y": cy,
                "category": r.category,
                "breach": "BREACH" in (r.full_label or ""),
            })

    if cv_engine and hasattr(cv_engine, "heatmap_traces"):
        now_ts = time.time()
        for pt in list(cv_engine.heatmap_traces.get(camera_id, [])):
            if now_ts - pt.get("ts", now_ts) <= 86400:
                points.append({
                    "x": pt["x"],
                    "y": pt["y"],
                    "category": pt["category"],
                    "breach": pt.get("breach", False),
                })

    return {"camera_id": camera_id, "count": len(points), "points": points[-1200:]}


@app.get("/api/detections")
async def get_recent_detections(limit: int = 60, category: str | None = None, camera_id: str | None = None):
    async with AsyncSessionLocal() as session:
        stmt = select(DetectionEvent).order_by(DetectionEvent.timestamp.desc())
        if category and category.upper() != "ALL":
            stmt = stmt.where(DetectionEvent.category == category.lower())
        if camera_id and camera_id.upper() != "ALL":
            stmt = stmt.where(DetectionEvent.camera_id == camera_id.lower())
        stmt = stmt.limit(min(max(1, limit), 1000))
        result = await session.execute(stmt)
        rows = result.scalars().all()
        out = []
        for r in rows:
            raw_attr = r.attributes or ""
            snap_id = raw_attr.split("|")[0].strip()
            pipe_note = raw_attr.split("|", 1)[1].strip() if "|" in raw_attr else None
            clip_exists = bool(snap_id and os.path.exists(f"{SNAPSHOT_DIR}/clip_{snap_id}.mp4"))
            plate_exists = bool(snap_id and os.path.exists(f"{SNAPSHOT_DIR}/plate_{snap_id}"))
            out.append({
                "id": r.id,
                "timestamp": r.timestamp.isoformat(),
                "camera_id": r.camera_id,
                "category": r.category,
                "sub_type": r.sub_type,
                "full_label": r.full_label,
                "confidence": r.confidence,
                "bbox_x": r.bbox_x,
                "bbox_y": r.bbox_y,
                "bbox_w": r.bbox_w,
                "bbox_h": r.bbox_h,
                "crop_url": f"/snapshots/crop_{snap_id}" if snap_id else None,
                "full_url": f"/snapshots/full_{snap_id}" if snap_id else None,
                "plate_url": f"/snapshots/plate_{snap_id}" if plate_exists else None,
                "clip_url": f"/snapshots/clip_{snap_id}.mp4" if clip_exists else None,
                "ai_notes": r.ai_notes or pipe_note,
            })
        return out


@app.delete("/api/detections")
async def purge_all_detections():
    for ext in ("*.jpg", "*.mp4"):
        for f in glob.glob(f"{SNAPSHOT_DIR}/{ext}"):
            try:
                os.remove(f)
            except OSError:
                pass
    cv_engine = getattr(app.state, "cv_engine", None)
    if cv_engine and hasattr(cv_engine, "heatmap_traces"):
        for k in cv_engine.heatmap_traces:
            cv_engine.clear_camera_heatmap(k)
    async with AsyncSessionLocal() as session:
        await session.execute(text("TRUNCATE TABLE detection_events RESTART IDENTITY;"))
        await session.commit()
    return {"status": "PURGED"}


@app.post("/api/system/reset")
async def full_system_reset():
    from app.services.ai_lessons import reset_ai_lessons
    from app.services.delivery_rules import DEFAULT_RULES, save_delivery_rules

    for ext in ("*.jpg", "*.mp4"):
        for f in glob.glob(f"{SNAPSHOT_DIR}/{ext}"):
            try:
                os.remove(f)
            except OSError:
                pass
    for f in glob.glob(f"{FACE_CROPS_DIR}/*.jpg"):
        try:
            os.remove(f)
        except OSError:
            pass
    cv_engine = getattr(app.state, "cv_engine", None)
    if cv_engine and hasattr(cv_engine, "heatmap_traces"):
        for k in cv_engine.heatmap_traces:
            cv_engine.clear_camera_heatmap(k)
    async with AsyncSessionLocal() as session:
        await session.execute(text("TRUNCATE TABLE detection_events RESTART IDENTITY;"))
        await session.execute(delete(KnownFace).where(KnownFace.role != "watchlist_auto"))
        await session.commit()
    reset_ai_lessons()
    save_delivery_rules([dict(r) for r in DEFAULT_RULES])
    await face_engine.sync_gallery_from_db_and_disk()
    return {"status": "FRESH_START_COMPLETE", "remaining_watchlist_vectors": len(face_engine.gallery)}


@app.post("/api/detections/{event_id}/inspect")
async def inspect_detection_endpoint(event_id: int):
    return await deep_inspect_detection(event_id)


@app.post("/api/maintenance/purge-routine-walkers")
async def purge_routine_walkers_endpoint():
    stats = await purge_routine_walkers_and_scrub_notes()
    cv_engine = getattr(app.state, "cv_engine", None)
    if cv_engine:
        await sync_zones_to_engine(cv_engine)
    await face_engine.sync_gallery_from_db_and_disk()
    return stats


@app.post("/api/agent/command")
async def agent_command_endpoint(payload: AgentCommandDTO):
    return await process_command_prompt(payload.prompt)

# --- v7 Delivery & Service Rules Endpoints ---
from app.services.delivery_rules import (
    get_delivery_rules,
    upsert_delivery_rule,
    delete_delivery_rule,
    decrement_delivery_sighting,
)

@app.get("/api/delivery-rules")
async def api_get_delivery_rules():
    return {"rules": get_delivery_rules()}

@app.post("/api/delivery-rules")
async def api_upsert_delivery_rule(payload: dict):
    name = payload.get("name", "")
    if not name:
        return {"error": "name required"}
    kind = payload.get("kind", "carrier")
    alert_mode = payload.get("alert_mode", "alert")
    cues = payload.get("cues", "")
    rules = upsert_delivery_rule(name=name, kind=kind, alert_mode=alert_mode, cues=cues)
    return {"rules": rules}

@app.delete("/api/delivery-rules/{rule_id}")
async def api_delete_delivery_rule(rule_id: str):
    rules = delete_delivery_rule(rule_id)
    return {"rules": rules}

# --- v7.1 Teach / Correct AI Endpoint & Inspect Fix ---
from sqlalchemy import select as _main_select, update as _main_update
from app.db.session import AsyncSessionLocal as _MainAsyncSessionLocal
from app.db.models import DetectionEvent as _MainDetectionEvent
from app.services.ai_lessons import add_ai_lesson, get_ai_lessons


@app.get("/api/ai-lessons")
async def api_get_ai_lessons():
    return {"lessons": get_ai_lessons()}


@app.post("/api/detections/{event_id}/teach")
async def api_teach_detection(event_id: int, payload: dict):
    correct_category = (payload.get("category") or "animal").strip().lower()
    correct_sub_type = (payload.get("sub_type") or "cat").strip()
    note = (payload.get("note") or "").strip()

    async with _MainAsyncSessionLocal() as session:
        res = await session.execute(_main_select(_MainDetectionEvent).where(_MainDetectionEvent.id == event_id))
        ev = res.scalar_one_or_none()
        if not ev:
            return {"error": "Event not found"}

        wrong_sub = ev.sub_type or ev.category
        snap_id = (ev.attributes or "").split("|")[0].strip()
        slot_prefix = ev.motion_slot or (
            (ev.full_label or "Motion_1").split("_")[0]
            + "_"
            + ((ev.full_label or "Motion_1").split("_")[1] if "_" in (ev.full_label or "") else "1")
        )

        was_delivery = (ev.category == "delivery") or ("DELIVERY:" in (wrong_sub or "").upper()) or ("SERVICE:" in (wrong_sub or "").upper())
        wrong_carrier = ""
        if was_delivery:
            wrong_carrier = (
                (wrong_sub or "")
                .replace("📦", "")
                .replace("🔇", "")
                .replace("DELIVERY:", "")
                .replace("SERVICE:", "")
                .split("[")[0]
                .strip()
                .upper()
            )
            if wrong_carrier and correct_category != "delivery":
                decrement_delivery_sighting(wrong_carrier)

        lessons = get_ai_lessons()
        if correct_category == "person":
            clean_person = correct_sub_type.replace("ID:", "").strip().upper()
            crop_path = f"{SNAPSHOT_DIR}/crop_{snap_id}" if snap_id else ""
            if clean_person in ("PERSON", "STRANGER", "VISITOR", "UNKNOWN"):
                purged_vecs = await face_engine.purge_matching_enrolled_vectors(crop_path, wrong_sub)
                cz_res = await session.execute(select(CameraZone).where(CameraZone.camera_id == ev.camera_id))
                in_tw = "BREACH" in (ev.full_label or "") or _event_touches_tripwire(ev, cz_res.scalars().all())
                final_sub = "person"
                new_label = f"{slot_prefix}_person{'_BREACH' if in_tw else ''}"
                purge_msg = f" (purged {purged_vecs} bad face vector)" if purged_vecs else ""
                ai_note = f"[🎓 CORRECTION] Cleared ID '{wrong_sub}' -> Unknown Stranger{purge_msg}"
            else:
                final_sub = f"ID: {clean_person}"
                new_label = f"{slot_prefix}_{clean_person}"
                # Strictly enroll ONLY if a sharp frontal/3-quarter face exists in the upper person crop (never full_path)
                vec, _, face_crop = (None, 0.0, None)
                if crop_path and os.path.exists(crop_path):
                    vec, _, face_crop = face_engine.extract_person_crop_face(
                        cv2.imread(crop_path), for_enrollment=True
                    )
                if "VISITOR_" in wrong_sub:
                    for token in wrong_sub.replace("(", " ").replace(")", " ").split():
                        if token.startswith("VISITOR_"):
                            await session.execute(delete(KnownFace).where(KnownFace.name == token))
                if vec is not None and face_crop is not None:
                    persistent_fname = f"enrolled_{clean_person}_{int(time.time() * 1000)}.jpg"
                    cv2.imwrite(f"{FACE_CROPS_DIR}/{persistent_fname}", face_crop, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
                    session.add(
                        KnownFace(
                            name=clean_person,
                            role="whitelist",
                            embedding_json=json.dumps(vec.tolist()),
                            sample_crop=f"/face_crops/{persistent_fname}",
                            sightings=1,
                            last_seen=datetime.now(timezone.utc),
                        )
                    )
                    ai_note = f"🧬 [ENROLLED WHITELIST: {clean_person}] Corrected from '{wrong_sub}'"
                else:
                    ai_note = f"🏷️ [LABELED: {clean_person} — No clear face enrolled] Corrected from '{wrong_sub}'"

            # If VLM falsely labeled a resident/person as a delivery carrier (e.g. rolling blue trash bins -> AMAZON),
            # write a clean generic negative carrier lesson (never storing family names in VLM clothing rules).
            if was_delivery and wrong_carrier:
                lessons = add_ai_lesson(
                    camera_id=ev.camera_id,
                    wrong_sub_type=f"delivery: {wrong_carrier.lower()}",
                    correct_category="person",
                    correct_sub_type="person",
                    note=note
                    or f"Casual resident/person (e.g. rolling wheeled curbside trash/recycling bins or driveway activity without commercial uniform/van) is NOT {wrong_carrier} delivery.",
                )
        elif correct_category == "delivery":
            clean_carrier = correct_sub_type.replace("📦 DELIVERY:", "").replace("🔇 SERVICE:", "").strip().upper()
            rule_map = {r["id"]: r for r in get_delivery_rules()}
            rule = rule_map.get(clean_carrier)
            if rule and rule.get("alert_mode") == "silent":
                final_sub = f"🔇 SERVICE: {clean_carrier}"
                new_label = f"{slot_prefix}_SERVICE_{clean_carrier}"
            else:
                final_sub = f"📦 DELIVERY: {clean_carrier}"
                new_label = f"{slot_prefix}_DELIVERY_{clean_carrier}"
            ai_note = f"[🎓 LEARNED CORRECTION] Corrected '{wrong_sub}' -> '{clean_carrier}'"
            lessons = add_ai_lesson(
                camera_id=ev.camera_id,
                wrong_sub_type=wrong_sub,
                correct_category=correct_category,
                correct_sub_type=clean_carrier,
                note=note or f"Corrected {wrong_sub} to {clean_carrier}",
            )
        else:
            final_sub = correct_sub_type.strip().lower()
            new_label = f"{slot_prefix}_{final_sub.replace(' ', '_')}"
            ai_note = (
                f"[🎓 LEARNED CORRECTION] {note}"
                if note
                else f"[🎓 LEARNED CORRECTION] Corrected '{wrong_sub}' -> '{final_sub}'"
            )
            lessons = add_ai_lesson(
                camera_id=ev.camera_id,
                wrong_sub_type=wrong_sub,
                correct_category=correct_category,
                correct_sub_type=final_sub,
                note=note or f"Corrected {wrong_sub} to {final_sub}",
            )

        new_attr = f"{snap_id}|{ai_note}"[:250] if snap_id else ai_note[:250]

        await session.execute(
            _main_update(_MainDetectionEvent)
            .where(_MainDetectionEvent.id == event_id)
            .values(
                category=correct_category,
                sub_type=final_sub[:120],
                full_label=new_label[:160],
                attributes=new_attr[:250],
                ai_notes=ai_note,
            )
        )
        await session.commit()

    if correct_category == "person":
        await face_engine.sync_gallery_from_db_and_disk()

    return {
        "id": event_id,
        "category": correct_category,
        "sub_type": final_sub,
        "full_label": new_label,
        "attributes": new_attr,
        "ai_notes": ai_note,
        "lessons_count": len(lessons),
    }


# --- v7.4 Native YoLink LoRa Perimeter Security & Siren Endpoints ---
class ArmNightDTO(BaseModel):
    force: bool = False


@app.get("/api/perimeter/status")
async def api_perimeter_status():
    return perimeter_service.get_snapshot()


@app.get("/api/perimeter/config")
async def api_get_perimeter_config():
    return perimeter_service.get_public_config()


@app.post("/api/perimeter/config")
async def api_save_perimeter_config(payload: dict):
    return perimeter_service.save_config(payload)


@app.post("/api/perimeter/arm-night")
async def api_perimeter_arm_night(payload: ArmNightDTO):
    return await perimeter_service.arm_night(force=payload.force)


@app.post("/api/perimeter/disarm")
async def api_perimeter_disarm():
    return await perimeter_service.disarm_and_silence(reason="Dashboard DISARM & SILENCE")


@app.post("/api/perimeter/mock-toggle/{entity_id}")
async def api_perimeter_mock_toggle(entity_id: str):
    return await perimeter_service.toggle_mock_zone(entity_id)


@app.post("/api/perimeter/purge-breaches")
async def api_perimeter_purge_breaches():
    return await purge_perimeter_breach_events()

