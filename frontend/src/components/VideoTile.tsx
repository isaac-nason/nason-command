import React, { useEffect, useRef, useState } from 'react';
import { useWhepClient } from '../hooks/useWhepClient';
import type { BoundingBoxDetection, CameraScheduleConfig, CameraZone, HeatmapPoint, ZonePoint } from '../types/telemetry';
import { Bell, BellOff, Check, Clock, Crosshair, Database, Eye, EyeOff, Flame, Lightbulb, Maximize2, Mic, Minimize2, Moon, PauseCircle, Radio, RotateCcw, Send, ShieldAlert, ShieldOff, Siren, Sun, Trash2, Volume2, VolumeX, X } from 'lucide-react';

interface CameraLightState {
  mode: 'ir' | 'smart' | 'color' | 'off';
  isapi_mode?: string;
  auto_brightness: boolean;
  white_brightness: number;
  ir_brightness: number;
  hold_seconds: number;
}

interface CameraHardwareStatus {
  camera_id: string;
  available: boolean;
  vendor?: string;
  model?: string;
  supports_light: boolean;
  supports_two_way_speaker: boolean;
  tapo_auth_configured?: boolean;
  supports_live_mic: boolean;
  light?: CameraLightState;
  mic_enabled?: boolean;
  mic_volume?: number;
  speaker_volume?: number;
  speaker_busy?: boolean;
}

interface VideoTileProps {
  title: string;
  streamName: string;
  hostIp: string;
  detections?: BoundingBoxDetection[];
  zones?: CameraZone[];
  onZonesUpdated?: (zonesByCam: Record<string, CameraZone[]>) => void;
  schedule?: CameraScheduleConfig;
  onScheduleUpdated?: (schedules: Record<string, CameraScheduleConfig>) => void;
  isFocused?: boolean;
  onToggleFocus?: () => void;
}

interface TrailPoint {
  x: number;
  y: number;
  t: number;
}

const CATEGORY_THEME: Record<
  string,
  {
    bracket: string;
    fill: string;
    badge: string;
    stroke: string;
  }
> = {
  person: {
    bracket: 'border-red-500',
    fill: 'bg-red-500/10 border-red-500/30',
    badge: 'bg-red-950/90 text-red-300 border-red-500/60',
    stroke: '#ef4444',
  },
  vehicle: {
    bracket: 'border-sky-400',
    fill: 'bg-sky-400/10 border-sky-400/30',
    badge: 'bg-sky-950/90 text-sky-300 border-sky-400/60',
    stroke: '#38bdf8',
  },
  delivery: {
    bracket: 'border-amber-400',
    fill: 'bg-amber-400/15 border-amber-400/40',
    badge: 'bg-amber-950/90 text-amber-300 border-amber-400/60',
    stroke: '#fbbf24',
  },
  animal: {
    bracket: 'border-amber-400',
    fill: 'bg-amber-400/10 border-amber-400/30',
    badge: 'bg-amber-950/90 text-amber-300 border-amber-400/60',
    stroke: '#fbbf24',
  },
};

export function VideoTile({
  title,
  streamName,
  hostIp,
  detections = [],
  zones = [],
  onZonesUpdated,
  schedule,
  onScheduleUpdated,
  isFocused = false,
  onToggleFocus,
}: VideoTileProps) {
  const whepUrl = `http://${hostIp}:8889/${streamName}/whep`;
  const apiBase = `http://${hostIp}:8080`;
  const { videoRef, streamState, hasAudioTrack } = useWhepClient(whepUrl);

  const [trails, setTrails] = useState<Record<string, { category: string; points: TrailPoint[] }>>({});
  const [zoom, setZoom] = useState<number>(1);
  const [pan, setPan] = useState<{ x: number; y: number }>({ x: 0, y: 0 });
  const [autoLock, setAutoLock] = useState<boolean>(false);
  const [showBbox, setShowBbox] = useState<boolean>(true);
  const [showHeatmap, setShowHeatmap] = useState<boolean>(false);
  const [heatmapPoints, setHeatmapPoints] = useState<HeatmapPoint[]>([]);
  const [isDragging, setIsDragging] = useState<boolean>(false);
  const dragStartRef = useRef<{ x: number; y: number }>({ x: 0, y: 0 });

  const [drawMode, setDrawMode] = useState<'exclusion' | 'tripwire' | null>(null);
  const [draftPoints, setDraftPoints] = useState<ZonePoint[]>([]);
  const canvasWrapRef = useRef<HTMLDivElement | null>(null);

  const [showSchedModal, setShowSchedModal] = useState<boolean>(false);
  const [schedMode, setSchedMode] = useState<'always_record' | 'paused' | 'schedule'>('always_record');
  const [schedStart, setSchedStart] = useState<string>('21:00');
  const [schedEnd, setSchedEnd] = useState<string>('06:00');
  const [alertWhitelist, setAlertWhitelist] = useState<boolean>(false);
  const [silentLogging, setSilentLogging] = useState<boolean>(false);

  // Live WebRTC Microphone Listen + Hardware Light & 2-Way Speaker Intercom State
  const [liveAudioOn, setLiveAudioOn] = useState<boolean>(false);
  const [hwStatus, setHwStatus] = useState<CameraHardwareStatus | null>(null);
  const [showLightModal, setShowLightModal] = useState<boolean>(false);
  const [showTalkModal, setShowTalkModal] = useState<boolean>(false);
  const [lightBusy, setLightBusy] = useState<boolean>(false);
  const [draftWhiteBr, setDraftWhiteBr] = useState<number>(35);
  const [draftAutoBr, setDraftAutoBr] = useState<boolean>(false);
  const [draftMicVol, setDraftMicVol] = useState<number>(85);
  const [draftSpkVol, setDraftSpkVol] = useState<number>(85);
  const [draftMicEnabled, setDraftMicEnabled] = useState<boolean>(true);

  const [ttsText, setTtsText] = useState<string>('');
  const [talkBusy, setTalkBusy] = useState<boolean>(false);
  const [talkStatusMsg, setTalkStatusMsg] = useState<string>('');
  const [isRecordingPtt, setIsRecordingPtt] = useState<boolean>(false);
  const [tapoPassInput, setTapoPassInput] = useState<string>('');
  const [showTapoRelink, setShowTapoRelink] = useState<boolean>(false);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const pttChunksRef = useRef<BlobPart[]>([]);
  const pttStreamRef = useRef<MediaStream | null>(null);
  const canBrowserMic = typeof navigator !== 'undefined' && Boolean(navigator.mediaDevices?.getUserMedia);

  const linkTapoSpeakers = async () => {
    const pw = tapoPassInput.trim();
    if (!pw || talkBusy) return;
    setTalkBusy(true);
    setTalkStatusMsg('Verifying with Tapo C113 on LAN...');
    try {
      const res = await fetch(`${apiBase}/api/cameras/tapo/auth`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cloud_password: pw }),
      });
      const data = await res.json();
      if (res.ok) {
        setTapoPassInput('');
        setShowTapoRelink(false);
        setTalkStatusMsg('Linked! Loft & Living Room speakers ready.');
        await refreshHardwareStatus();
      } else {
        setTalkStatusMsg(data.detail || 'Tapo password verification failed');
      }
    } catch {
      setTalkStatusMsg('Could not reach backend');
    } finally {
      setTalkBusy(false);
    }
  };

  const refreshHardwareStatus = async () => {
    try {
      const res = await fetch(`${apiBase}/api/cameras/${streamName}/hardware`);
      if (!res.ok) return;
      const data: CameraHardwareStatus = await res.json();
      setHwStatus(data);
      if (data.light) {
        setDraftWhiteBr(data.light.white_brightness);
        setDraftAutoBr(data.light.auto_brightness);
      }
      if (typeof data.mic_volume === 'number') setDraftMicVol(data.mic_volume);
      if (typeof data.speaker_volume === 'number') setDraftSpkVol(data.speaker_volume);
      if (typeof data.mic_enabled === 'boolean') setDraftMicEnabled(data.mic_enabled);
    } catch {
      // ignore transient offline
    }
  };

  useEffect(() => {
    refreshHardwareStatus();
  }, [apiBase, streamName]);

  const toggleLiveAudio = () => {
    setLiveAudioOn((prev) => {
      const next = !prev;
      if (videoRef.current) {
        videoRef.current.muted = !next;
        videoRef.current.volume = 1.0;
        if (next) videoRef.current.play().catch(() => {});
      }
      return next;
    });
  };

  const applyLightConfig = async (
    mode: 'ir' | 'smart' | 'color' | 'off',
    whiteBrOverride?: number,
    autoOverride?: boolean
  ) => {
    setLightBusy(true);
    try {
      const res = await fetch(`${apiBase}/api/cameras/${streamName}/light`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          mode,
          white_brightness: whiteBrOverride !== undefined ? whiteBrOverride : draftWhiteBr,
          auto_brightness: autoOverride !== undefined ? autoOverride : draftAutoBr,
        }),
      });
      if (res.ok) {
        const updated: CameraHardwareStatus = await res.json();
        setHwStatus(updated);
        if (updated.light) {
          setDraftWhiteBr(updated.light.white_brightness);
          setDraftAutoBr(updated.light.auto_brightness);
        }
      }
    } catch (err) {
      console.error('Failed to update camera light:', err);
    } finally {
      setLightBusy(false);
    }
  };

  const saveCameraAudioHw = async (micEnabledOverride?: boolean) => {
    setLightBusy(true);
    try {
      const res = await fetch(`${apiBase}/api/cameras/${streamName}/audio-config`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          mic_enabled: micEnabledOverride !== undefined ? micEnabledOverride : draftMicEnabled,
          mic_volume: draftMicVol,
          speaker_volume: draftSpkVol,
        }),
      });
      if (res.ok) {
        const updated: CameraHardwareStatus = await res.json();
        setHwStatus(updated);
        if (typeof updated.mic_enabled === 'boolean') setDraftMicEnabled(updated.mic_enabled);
      }
    } catch (err) {
      console.error('Failed to update camera audio config:', err);
    } finally {
      setLightBusy(false);
    }
  };

  const sendSpeakerMessage = async (textOrPreset: string) => {
    const clean = textOrPreset.trim();
    if (!clean || talkBusy) return;
    setTalkBusy(true);
    setTalkStatusMsg('Broadcasting to speaker...');
    try {
      const res = await fetch(`${apiBase}/api/cameras/${streamName}/speak`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: clean }),
      });
      const data = await res.json();
      if (res.ok) {
        setTalkStatusMsg(`Played (${data.duration_sec}s)`);
        if (clean === ttsText.trim()) setTtsText('');
      } else {
        setTalkStatusMsg(data.detail || 'Speaker error');
      }
    } catch {
      setTalkStatusMsg('Failed to reach speaker');
    } finally {
      setTalkBusy(false);
    }
  };

  const triggerSpeakerChime = async (style: 'polite' | 'warning', announceWarning = false) => {
    if (talkBusy) return;
    setTalkBusy(true);
    setTalkStatusMsg(style === 'warning' ? 'Sounding deterrent chime...' : 'Ringing chime...');
    try {
      const res = await fetch(`${apiBase}/api/cameras/${streamName}/chime`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ style, announce_warning: announceWarning }),
      });
      const data = await res.json();
      setTalkStatusMsg(res.ok ? `Played (${data.duration_sec}s)` : data.detail || 'Chime error');
    } catch {
      setTalkStatusMsg('Failed to reach speaker');
    } finally {
      setTalkBusy(false);
    }
  };

  const startPushToTalk = async () => {
    if (!canBrowserMic || isRecordingPtt || talkBusy) return;
    try {
      const micStream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      pttStreamRef.current = micStream;
      pttChunksRef.current = [];
      const rec = new MediaRecorder(micStream);
      rec.ondataavailable = (ev) => {
        if (ev.data && ev.data.size > 0) pttChunksRef.current.push(ev.data);
      };
      rec.onstop = async () => {
        micStream.getTracks().forEach((t) => t.stop());
        pttStreamRef.current = null;
        const blob = new Blob(pttChunksRef.current, { type: rec.mimeType || 'audio/webm' });
        pttChunksRef.current = [];
        if (blob.size < 400) {
          setTalkStatusMsg('Hold button slightly longer to speak');
          return;
        }
        setTalkBusy(true);
        setTalkStatusMsg('Transmitting voice to speaker...');
        try {
          const buf = await blob.arrayBuffer();
          const res = await fetch(`${apiBase}/api/cameras/${streamName}/talk`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/octet-stream' },
            body: buf,
          });
          const data = await res.json();
          setTalkStatusMsg(res.ok ? `Voice sent (${data.duration_sec}s)` : data.detail || 'PTT error');
        } catch {
          setTalkStatusMsg('Failed to transmit voice');
        } finally {
          setTalkBusy(false);
        }
      };
      mediaRecorderRef.current = rec;
      rec.start();
      setIsRecordingPtt(true);
      setTalkStatusMsg('🎙️ Recording... release to broadcast');
    } catch {
      setTalkStatusMsg('Mic permission denied or unavailable');
    }
  };

  const stopPushToTalk = () => {
    if (!isRecordingPtt) return;
    setIsRecordingPtt(false);
    if (mediaRecorderRef.current && mediaRecorderRef.current.state !== 'inactive') {
      mediaRecorderRef.current.stop();
    }
  };

  useEffect(() => {
    if (schedule) {
      setSchedMode(schedule.mode);
      setSchedStart(schedule.start_time || '21:00');
      setSchedEnd(schedule.end_time || '06:00');
      setAlertWhitelist(Boolean(schedule.alert_whitelist));
      setSilentLogging(Boolean(schedule.silent_logging));
    }
  }, [schedule]);

  useEffect(() => {
    if (!showHeatmap) return;
    const fetchHeat = async () => {
      try {
        const res = await fetch(`${apiBase}/api/detections/heatmap/${streamName}`);
        if (res.ok) {
          const data = await res.json();
          setHeatmapPoints(data.points || []);
        }
      } catch (err) {
        console.error('Failed to load heatmap:', err);
      }
    };
    fetchHeat();
    const timer = window.setInterval(fetchHeat, 8000);
    return () => window.clearInterval(timer);
  }, [showHeatmap, apiBase, streamName]);

  const hasBreach = detections.some((d) => d.breach);
  const isRecordingNow = schedule ? schedule.currently_recording : true;

  useEffect(() => {
    const now = Date.now();

    setTrails((prev) => {
      const next: Record<string, { category: string; points: TrailPoint[] }> = {};
      for (const [slot, data] of Object.entries(prev)) {
        const fresh = data.points.filter((p) => now - p.t < 2500);
        if (fresh.length > 0) {
          next[slot] = { category: data.category, points: fresh };
        }
      }
      for (const det of detections) {
        const cx = (det.x + det.w / 2) * 1000;
        const cy = (det.y + det.h / 2) * 1000;
        const existing = next[det.slot]?.points || [];
        const lastPt = existing[existing.length - 1];
        const jumped = lastPt && Math.hypot(cx - lastPt.x, cy - lastPt.y) > 300;
        const basePoints = jumped ? [] : existing;

        next[det.slot] = {
          category: det.category,
          points: [...basePoints.slice(-14), { x: cx, y: cy, t: now }],
        };
      }
      return next;
    });

    if (autoLock && !drawMode) {
      if (detections.length > 0) {
        const primary = detections[0];
        const cxNorm = primary.x + primary.w / 2;
        const cyNorm = primary.y + primary.h / 2;
        const targetZoom = Math.max(zoom, 2.1);
        const maxShift = ((targetZoom - 1) / (2 * targetZoom)) * 100;

        const desiredX = Math.max(-maxShift, Math.min(maxShift, (0.5 - cxNorm) * 100));
        const desiredY = Math.max(-maxShift, Math.min(maxShift, (0.5 - cyNorm) * 100));

        setZoom(targetZoom);
        setPan({ x: desiredX, y: desiredY });
      } else {
        setZoom(1);
        setPan({ x: 0, y: 0 });
      }
    }
  }, [detections, autoLock, drawMode]);

  const saveSchedule = async (
    modeOverride?: 'always_record' | 'paused' | 'schedule',
    alertWhitelistOverride?: boolean,
    silentLoggingOverride?: boolean
  ) => {
    const targetMode = modeOverride || schedMode;
    const targetAlertWhitelist =
      alertWhitelistOverride !== undefined ? alertWhitelistOverride : alertWhitelist;
    const targetSilentLogging =
      silentLoggingOverride !== undefined ? silentLoggingOverride : silentLogging;
    try {
      const res = await fetch(`${apiBase}/api/cameras/${streamName}/schedule`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          mode: targetMode,
          start_time: schedStart,
          end_time: schedEnd,
          alert_whitelist: targetAlertWhitelist,
          silent_logging: targetSilentLogging,
        }),
      });
      if (res.ok && onScheduleUpdated) {
        const updated = await res.json();
        onScheduleUpdated(updated);
      }
      setShowSchedModal(false);
    } catch (err) {
      console.error('Failed to save camera schedule:', err);
    }
  };

  const cycleCameraAlertPosture = async () => {
    // Cycles: FAM·SILENT -> LOG·ONLY (record & log silently, zero alarms) -> FAM·ALERT -> FAM·SILENT
    if (!silentLogging && !alertWhitelist) {
      setSilentLogging(true);
      setAlertWhitelist(false);
      await saveSchedule(schedMode, false, true);
    } else if (silentLogging) {
      setSilentLogging(false);
      setAlertWhitelist(true);
      await saveSchedule(schedMode, true, false);
    } else {
      setSilentLogging(false);
      setAlertWhitelist(false);
      await saveSchedule(schedMode, false, false);
    }
  };

  const startDrawing = (type: 'exclusion' | 'tripwire') => {
    setAutoLock(false);
    setZoom(1);
    setPan({ x: 0, y: 0 });
    setDrawMode(type);
    setDraftPoints([]);
  };

  const cancelDrawing = () => {
    setDrawMode(null);
    setDraftPoints([]);
  };

  const handleCanvasClick = (e: React.MouseEvent<HTMLDivElement>) => {
    if (!drawMode || !canvasWrapRef.current) return;
    e.stopPropagation();
    const rect = canvasWrapRef.current.getBoundingClientRect();
    const x = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    const y = Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height));
    setDraftPoints((prev) => [...prev, { x: Number(x.toFixed(4)), y: Number(y.toFixed(4)) }]);
  };

  const saveZone = async () => {
    if (!drawMode || draftPoints.length < 3) return;
    try {
      const res = await fetch(`${apiBase}/api/zones`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          camera_id: streamName,
          name: `${drawMode.toUpperCase()}_${zones.length + 1}`,
          zone_type: drawMode,
          points: draftPoints,
        }),
      });
      if (res.ok && onZonesUpdated) {
        const updated = await res.json();
        onZonesUpdated(updated);
      }
    } catch (err) {
      console.error('Failed to save zone:', err);
    } finally {
      setDrawMode(null);
      setDraftPoints([]);
    }
  };

  const clearZones = async () => {
    try {
      const res = await fetch(`${apiBase}/api/zones/camera/${streamName}`, {
        method: 'DELETE',
      });
      if (res.ok && onZonesUpdated) {
        const updated = await res.json();
        onZonesUpdated(updated);
      }
    } catch (err) {
      console.error('Failed to clear zones:', err);
    }
  };

  const handleWheel = (e: React.WheelEvent<HTMLDivElement>) => {
    e.preventDefault();
    if (autoLock || drawMode) return;
    const delta = e.deltaY < 0 ? 0.25 : -0.25;
    setZoom((prev) => {
      const next = Math.min(5, Math.max(1, Number((prev + delta).toFixed(2))));
      if (next === 1) setPan({ x: 0, y: 0 });
      return next;
    });
  };

  const handleMouseDown = (e: React.MouseEvent<HTMLDivElement>) => {
    if (zoom <= 1 || autoLock || drawMode) return;
    setIsDragging(true);
    dragStartRef.current = { x: e.clientX - pan.x * 4, y: e.clientY - pan.y * 4 };
  };

  const handleMouseMove = (e: React.MouseEvent<HTMLDivElement>) => {
    if (!isDragging || zoom <= 1 || autoLock || drawMode) return;
    const maxShift = ((zoom - 1) / (2 * zoom)) * 100;
    const rawX = (e.clientX - dragStartRef.current.x) / 4;
    const rawY = (e.clientY - dragStartRef.current.y) / 4;
    setPan({
      x: Math.max(-maxShift, Math.min(maxShift, rawX)),
      y: Math.max(-maxShift, Math.min(maxShift, rawY)),
    });
  };

  const handleMouseUp = () => setIsDragging(false);

  const resetPtz = () => {
    setAutoLock(false);
    setZoom(1);
    setPan({ x: 0, y: 0 });
  };

  return (
    <div
      ref={canvasWrapRef}
      onClick={handleCanvasClick}
      onWheel={handleWheel}
      onMouseDown={handleMouseDown}
      onMouseMove={handleMouseMove}
      onMouseUp={handleMouseUp}
      onMouseLeave={handleMouseUp}
      onDoubleClick={() => !drawMode && onToggleFocus && onToggleFocus()}
      className={`relative rounded-lg overflow-hidden border bg-black aspect-video flex flex-col justify-between select-none transition-all ${
        hasBreach
          ? 'border-red-500 shadow-[0_0_25px_rgba(239,68,68,0.65)]'
          : detections.length > 0
          ? 'border-emerald-500/50'
          : 'border-zinc-800'
      } ${
        drawMode
          ? 'cursor-crosshair ring-2 ring-amber-400/70'
          : zoom > 1 && !autoLock
          ? 'cursor-grab active:cursor-grabbing'
          : 'cursor-pointer'
      }`}
    >
      <div
        className={`w-full h-full relative origin-center ${
          isDragging ? '' : 'transition-transform duration-300 ease-out'
        }`}
        style={{
          transform: `scale(${zoom}) translate(${pan.x}%, ${pan.y}%)`,
        }}
      >
        <video
          ref={videoRef}
          autoPlay
          playsInline
          muted={!liveAudioOn || isRecordingPtt}
          className="w-full h-full object-cover"
        />

        <svg
          viewBox="0 0 1000 1000"
          preserveAspectRatio="none"
          className="absolute inset-0 w-full h-full pointer-events-none"
        >
          <defs>
            <radialGradient id={`heatGradRed-${streamName}`} cx="50%" cy="50%" r="50%">
              <stop offset="0%" stopColor="#ef4444" stopOpacity="0.32" />
              <stop offset="45%" stopColor="#f59e0b" stopOpacity="0.16" />
              <stop offset="100%" stopColor="#10b981" stopOpacity="0" />
            </radialGradient>
            <radialGradient id={`heatGradSky-${streamName}`} cx="50%" cy="50%" r="50%">
              <stop offset="0%" stopColor="#38bdf8" stopOpacity="0.55" />
              <stop offset="50%" stopColor="#10b981" stopOpacity="0.25" />
              <stop offset="100%" stopColor="#000000" stopOpacity="0" />
            </radialGradient>
          </defs>

          {/* 24-Hour Tactical Thermal Heatmap Overlay (True 16:9 Circular Plumes) */}
          {showHeatmap && (
            <g style={{ mixBlendMode: 'screen' }}>
              {heatmapPoints.map((pt, idx) => (
                <ellipse
                  key={idx}
                  cx={pt.x * 1000}
                  cy={pt.y * 1000}
                  rx={pt.category === 'person' || pt.breach ? 24 : 20}
                  ry={pt.category === 'person' || pt.breach ? 42 : 35}
                  fill={
                    pt.category === 'person' || pt.breach
                      ? `url(#heatGradRed-${streamName})`
                      : `url(#heatGradSky-${streamName})`
                  }
                />
              ))}
            </g>
          )}

          {zones.map((z) => {
            const pts = z.points.map((p) => `${p.x * 1000},${p.y * 1000}`).join(' ');
            const isExcl = z.zone_type === 'exclusion';
            const firstPt = z.points[0];
            return (
              <g key={z.id}>
                <polygon
                  points={pts}
                  fill={isExcl ? 'rgba(239, 68, 68, 0.18)' : 'rgba(245, 158, 11, 0.16)'}
                  stroke={isExcl ? '#ef4444' : '#f59e0b'}
                  strokeWidth="2.5"
                  strokeDasharray={isExcl ? '8 4' : '12 4'}
                />
                {firstPt && (
                  <text
                    x={firstPt.x * 1000 + 8}
                    y={firstPt.y * 1000 + 18}
                    fill={isExcl ? '#fca5a5' : '#fde68a'}
                    fontSize="20"
                    fontFamily="monospace"
                    fontWeight="bold"
                  >
                    {z.name}
                  </text>
                )}
              </g>
            );
          })}

          {drawMode && draftPoints.length > 0 && (
            <g>
              <polygon
                points={draftPoints.map((p) => `${p.x * 1000},${p.y * 1000}`).join(' ')}
                fill={drawMode === 'exclusion' ? 'rgba(239, 68, 68, 0.28)' : 'rgba(245, 158, 11, 0.28)'}
                stroke={drawMode === 'exclusion' ? '#ef4444' : '#f59e0b'}
                strokeWidth="3"
              />
              {draftPoints.map((p, idx) => (
                <circle
                  key={idx}
                  cx={p.x * 1000}
                  cy={p.y * 1000}
                  r="6"
                  fill="#ffffff"
                  stroke="#000000"
                  strokeWidth="2"
                />
              ))}
            </g>
          )}

          {showBbox &&
            Object.entries(trails).map(([slot, trail]) => {
              if (trail.points.length < 2) return null;
              const theme = CATEGORY_THEME[trail.category] || CATEGORY_THEME.vehicle;
              const pointsAttr = trail.points.map((p) => `${p.x},${p.y}`).join(' ');
              return (
                <g key={slot}>
                  <polyline
                    fill="none"
                    stroke={theme.stroke}
                    strokeWidth="2.5"
                    strokeDasharray="6 4"
                    strokeOpacity="0.75"
                    points={pointsAttr}
                  />
                  {trail.points.map((pt, idx) => (
                    <circle
                      key={idx}
                      cx={pt.x}
                      cy={pt.y}
                      r={idx === trail.points.length - 1 ? 4.5 : 2.2}
                      fill={theme.stroke}
                      fillOpacity={(idx + 1) / trail.points.length}
                    />
                  ))}
                </g>
              );
            })}
        </svg>

        {showBbox && (
          <div className="absolute inset-0 pointer-events-none overflow-hidden">
            {detections.map((det) => {
              const theme = CATEGORY_THEME[det.category] || CATEGORY_THEME.vehicle;
              const shortSlot = det.slot.replace('Motion_', 'M');
              const pctConf = Math.round(det.confidence * 100);

              return (
                <div
                  key={det.slot}
                  className={`absolute border transition-all duration-150 ease-out ${
                    det.breach ? 'bg-red-600/25 border-red-500 animate-pulse' : theme.fill
                  }`}
                  style={{
                    left: `${det.x * 100}%`,
                    top: `${det.y * 100}%`,
                    width: `${det.w * 100}%`,
                    height: `${det.h * 100}%`,
                  }}
                >
                  <div className={`absolute -top-0.5 -left-0.5 w-3 h-3 border-t-2 border-l-2 ${theme.bracket}`} />
                  <div className={`absolute -top-0.5 -right-0.5 w-3 h-3 border-t-2 border-r-2 ${theme.bracket}`} />
                  <div className={`absolute -bottom-0.5 -left-0.5 w-3 h-3 border-b-2 border-l-2 ${theme.bracket}`} />
                  <div className={`absolute -bottom-0.5 -right-0.5 w-3 h-3 border-b-2 border-r-2 ${theme.bracket}`} />

                  <div className="absolute inset-0 flex items-center justify-center opacity-60">
                    <div className={`w-1.5 h-1.5 rounded-full ${theme.bracket} border`} />
                  </div>

                  <span
                    className={`absolute -top-5 left-0 px-1.5 py-0.5 text-[10px] font-mono font-bold tracking-tight rounded-xs whitespace-nowrap border ${
                      det.breach ? 'bg-red-600 text-white border-red-300' : theme.badge
                    }`}
                  >
                    {shortSlot} // {det.sub_type.toUpperCase()} {det.breach ? '⚠️ BREACH' : `· ${pctConf}%`}
                  </span>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Stationary Top HUD Control Bar */}
      <div
        className="absolute top-2 left-2 right-2 flex flex-wrap justify-between items-center gap-1 z-10"
        onClick={(e) => e.stopPropagation()}
        onDoubleClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-1.5 pointer-events-none">
          <span className="bg-zinc-950/85 border border-zinc-800 px-2 py-0.5 sm:px-2.5 sm:py-1 rounded text-[11px] sm:text-xs font-mono font-bold text-zinc-100">
            {title}
          </span>
          {hasBreach && (
            <span className="bg-red-600 text-white px-2 py-0.5 rounded text-[10px] font-mono font-bold animate-bounce">
              🚨 BREACH
            </span>
          )}
          {showHeatmap && (
            <span className="bg-orange-600/90 text-white px-1.5 py-0.5 rounded text-[10px] font-mono font-bold">
              🔥 24H HEAT ({heatmapPoints.length})
            </span>
          )}
          {zoom > 1 && (
            <span className="bg-sky-950/90 border border-sky-500/40 px-1.5 py-0.5 rounded text-[10px] font-mono font-bold text-sky-300">
              {zoom.toFixed(1)}x
            </span>
          )}
        </div>

        <div className="flex items-center gap-1 flex-wrap justify-end">
          {drawMode ? (
            <>
              <span className="bg-amber-950/90 border border-amber-500/50 px-2 py-1 rounded text-[10px] font-mono text-amber-300">
                {Math.max(0, 3 - draftPoints.length)}+ PTS ({draftPoints.length})
              </span>
              <button
                onClick={saveZone}
                disabled={draftPoints.length < 3}
                className="bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40 px-2 py-1 rounded text-[10px] font-mono font-bold text-white flex items-center gap-1 cursor-pointer"
              >
                <Check className="w-3 h-3" />
                SAVE
              </button>
              <button
                onClick={cancelDrawing}
                className="bg-zinc-900 hover:bg-zinc-800 border border-zinc-700 px-2 py-1 rounded text-[10px] font-mono text-zinc-300 flex items-center gap-1 cursor-pointer"
              >
                <X className="w-3 h-3" />
              </button>
            </>
          ) : (
            <>
              <button
                onClick={toggleLiveAudio}
                title={
                  liveAudioOn
                    ? 'Live Camera Microphone UNMUTED — Click to mute'
                    : hasAudioTrack
                    ? 'Unmute Live Camera Microphone (WebRTC Audio Ready)'
                    : 'Unmute Live Camera Microphone'
                }
                className={`px-1.5 py-1 rounded text-[10px] font-mono font-bold border flex items-center gap-1 cursor-pointer transition-colors ${
                  liveAudioOn
                    ? 'bg-emerald-600 text-white border-emerald-400 shadow-[0_0_10px_rgba(16,185,129,0.5)]'
                    : 'bg-zinc-950/85 hover:bg-zinc-800 text-zinc-400 border-zinc-800'
                }`}
              >
                {liveAudioOn ? <Volume2 className="w-3 h-3" /> : <VolumeX className="w-3 h-3" />}
                <span className="hidden sm:inline">{liveAudioOn ? 'MIC·ON' : 'MIC'}</span>
              </button>

              {hwStatus?.supports_light && (
                <button
                  onClick={() => {
                    setShowTalkModal(false);
                    setShowSchedModal(false);
                    setShowLightModal((prev) => {
                      const next = !prev;
                      if (next) refreshHardwareStatus();
                      return next;
                    });
                  }}
                  title="Configure Camera Night Vision, White Floodlight & Hardware Audio"
                  className={`px-1.5 py-1 rounded text-[10px] font-mono font-bold border flex items-center gap-1 cursor-pointer transition-colors ${
                    hwStatus.light?.mode === 'ir'
                      ? 'bg-indigo-950/90 text-indigo-300 border-indigo-500/60'
                      : hwStatus.light?.mode === 'color'
                      ? 'bg-amber-500 text-zinc-950 border-amber-300'
                      : hwStatus.light?.mode === 'off'
                      ? 'bg-zinc-900 text-zinc-400 border-zinc-700'
                      : 'bg-zinc-950/85 hover:bg-zinc-800 text-amber-300 border-zinc-800'
                  }`}
                >
                  {hwStatus.light?.mode === 'ir' ? (
                    <Moon className="w-3 h-3" />
                  ) : hwStatus.light?.mode === 'color' ? (
                    <Sun className="w-3 h-3" />
                  ) : (
                    <Lightbulb className="w-3 h-3" />
                  )}
                  <span className="hidden sm:inline">
                    {hwStatus.light?.mode === 'ir'
                      ? 'IR·ONLY'
                      : hwStatus.light?.mode === 'color'
                      ? `FLOOD·${hwStatus.light.white_brightness}%`
                      : hwStatus.light?.mode === 'off'
                      ? 'LIGHT·OFF'
                      : `SMART·${hwStatus.light?.auto_brightness ? 'AUTO' : `${hwStatus.light?.white_brightness ?? 100}%`}`}
                  </span>
                </button>
              )}

              {hwStatus?.supports_two_way_speaker && (
                <button
                  onClick={() => {
                    setShowLightModal(false);
                    setShowSchedModal(false);
                    setShowTalkModal((prev) => !prev);
                  }}
                  title="2-Way Camera Speaker Intercom, Voice Presets & Deterrent Chime"
                  className={`px-1.5 py-1 rounded text-[10px] font-mono font-bold border flex items-center gap-1 cursor-pointer transition-colors ${
                    isRecordingPtt
                      ? 'bg-red-600 text-white border-red-300 animate-pulse'
                      : showTalkModal
                      ? 'bg-cyan-600 text-white border-cyan-300'
                      : 'bg-zinc-950/85 hover:bg-zinc-800 text-cyan-300 border-zinc-800'
                  }`}
                >
                  <Radio className="w-3 h-3" />
                  <span className="hidden sm:inline">TALK</span>
                </button>
              )}

              <button
                onClick={() => {
                  setShowLightModal(false);
                  setShowTalkModal(false);
                  setShowSchedModal((prev) => !prev);
                }}
                title="Configure Recording / Pause Schedule"
                className={`px-1.5 py-1 rounded text-[10px] font-mono font-bold border flex items-center gap-1 cursor-pointer transition-colors ${
                  schedule?.away_override
                    ? 'bg-amber-600 text-white border-amber-300 shadow-[0_0_10px_rgba(245,158,11,0.45)]'
                    : !isRecordingNow
                    ? 'bg-amber-950/90 text-amber-300 border-amber-500/60'
                    : schedule?.mode === 'schedule'
                    ? 'bg-sky-950/90 text-sky-300 border-sky-500/50'
                    : 'bg-zinc-950/85 hover:bg-zinc-800 text-emerald-400 border-zinc-800'
                }`}
              >
                {schedule?.mode === 'schedule' && !schedule?.away_override ? (
                  <Clock className="w-3 h-3" />
                ) : isRecordingNow ? (
                  <Database className="w-3 h-3" />
                ) : (
                  <PauseCircle className="w-3 h-3" />
                )}
                <span>
                  {schedule?.away_override
                    ? 'AWAY·24/7'
                    : schedule?.mode === 'schedule'
                    ? isRecordingNow
                      ? 'SCHED·REC'
                      : 'SCHED·PAUSED'
                    : isRecordingNow
                    ? 'REC'
                    : 'PAUSED'}
                </span>
              </button>

              <button
                onClick={cycleCameraAlertPosture}
                title={
                  silentLogging
                    ? 'LOG·ONLY ON — Recording & Face ID logging silently with zero push alarms while HOME. Click to switch to FAM·ALERT.'
                    : alertWhitelist
                    ? 'FAM·ALERT ON — Alerting on Family & All detections (ISAAC exempt). Click to switch to FAM·SILENT.'
                    : 'FAM·SILENT — Alerting on Strangers/Deliveries, silent on Family. Click to switch to LOG·ONLY (silent logging, zero alarms).'
                }
                className={`px-1.5 py-1 rounded text-[10px] font-mono font-bold border flex items-center gap-1 cursor-pointer transition-colors ${
                  silentLogging
                    ? 'bg-violet-600 text-white border-violet-300 shadow-[0_0_10px_rgba(139,92,246,0.45)]'
                    : alertWhitelist
                    ? 'bg-sky-600 text-white border-sky-400 shadow-[0_0_10px_rgba(14,165,233,0.45)]'
                    : 'bg-zinc-950/85 hover:bg-zinc-800 text-zinc-400 border-zinc-800'
                }`}
              >
                {silentLogging ? (
                  <Database className="w-3 h-3" />
                ) : alertWhitelist ? (
                  <Bell className="w-3 h-3" />
                ) : (
                  <BellOff className="w-3 h-3" />
                )}
                <span>
                  {silentLogging ? 'LOG·ONLY' : alertWhitelist ? 'FAM·ALERT' : 'FAM·SILENT'}
                </span>
              </button>

              <button
                onClick={() => setShowHeatmap((prev) => !prev)}
                title="Toggle 24-Hour Tactical Thermal Heatmap Overlay"
                className={`px-1.5 py-1 rounded text-[10px] font-mono font-bold border flex items-center gap-1 cursor-pointer ${
                  showHeatmap
                    ? 'bg-orange-600 text-white border-orange-400'
                    : 'bg-zinc-950/85 hover:bg-zinc-800 text-orange-300 border-zinc-800'
                }`}
              >
                <Flame className="w-3 h-3" />
                <span className="hidden sm:inline">HEAT</span>
              </button>
              {showHeatmap && (
                <button
                  onClick={async () => {
                    await fetch(`${apiBase}/api/detections/heatmap/${streamName}/clear`, { method: 'POST' });
                    setHeatmapPoints([]);
                  }}
                  title="Clear only the thermal heat signatures on this camera"
                  className="bg-zinc-950/90 hover:bg-red-950 border border-orange-500/50 px-1.5 py-1 rounded text-[10px] font-mono font-bold text-orange-300 cursor-pointer"
                >
                  CLEAR HEAT
                </button>
              )}

              <button
                onClick={() => setShowBbox((prev) => !prev)}
                title="Toggle Target Bounding Boxes"
                className={`px-1.5 py-1 rounded text-[10px] font-mono border flex items-center gap-1 cursor-pointer ${
                  showBbox
                    ? 'bg-zinc-950/85 text-emerald-300 border-zinc-800'
                    : 'bg-zinc-900 text-zinc-500 border-zinc-800'
                }`}
              >
                {showBbox ? <Eye className="w-3 h-3" /> : <EyeOff className="w-3 h-3" />}
                <span className="hidden sm:inline">BBOX</span>
              </button>

              <button
                onClick={() => startDrawing('exclusion')}
                title="Draw Red Exclusion Mask"
                className="bg-zinc-950/85 hover:bg-red-950/80 border border-zinc-800 hover:border-red-500/50 px-1.5 py-1 rounded text-[10px] font-mono text-red-300 flex items-center gap-1 cursor-pointer"
              >
                <ShieldOff className="w-3 h-3" />
                <span className="hidden sm:inline">+EXCL</span>
              </button>
              <button
                onClick={() => startDrawing('tripwire')}
                title="Draw Amber Perimeter Tripwire"
                className="bg-zinc-950/85 hover:bg-amber-950/80 border border-zinc-800 hover:border-amber-500/50 px-1.5 py-1 rounded text-[10px] font-mono text-amber-300 flex items-center gap-1 cursor-pointer"
              >
                <ShieldAlert className="w-3 h-3" />
                <span className="hidden sm:inline">+TRIP</span>
              </button>
              {zones.length > 0 && (
                <button
                  onClick={clearZones}
                  title="Clear all polygons on this camera"
                  className="bg-zinc-950/85 hover:bg-zinc-800 border border-zinc-800 p-1 rounded text-zinc-400 hover:text-red-400 cursor-pointer"
                >
                  <Trash2 className="w-3 h-3" />
                </button>
              )}
              {zoom > 1 && (
                <button
                  onClick={resetPtz}
                  title="Reset Digital Zoom (1.0x)"
                  className="bg-zinc-950/85 hover:bg-zinc-800 border border-zinc-700 px-1.5 py-1 rounded text-[10px] font-mono text-zinc-300 flex items-center gap-1 cursor-pointer"
                >
                  <RotateCcw className="w-3 h-3" />
                  1x
                </button>
              )}
              <button
                onClick={() => setAutoLock((prev) => !prev)}
                title="Auto-Zoom & Track Primary Target"
                className={`px-1.5 py-1 rounded text-[10px] font-mono font-bold border flex items-center gap-1 cursor-pointer transition-colors ${
                  autoLock
                    ? 'bg-amber-500/25 text-amber-300 border-amber-500/60'
                    : 'bg-zinc-950/85 hover:bg-zinc-800 text-zinc-400 border-zinc-800'
                }`}
              >
                <Crosshair className="w-3 h-3" />
                <span className="hidden sm:inline">{autoLock ? 'LOCKED' : 'LOCK'}</span>
              </button>
              {onToggleFocus && (
                <button
                  onClick={onToggleFocus}
                  title={isFocused ? 'Return to Grid View' : 'Maximize to Theater View'}
                  className="bg-zinc-950/85 hover:bg-zinc-800 border border-zinc-800 p-1 rounded text-zinc-300 cursor-pointer"
                >
                  {isFocused ? <Minimize2 className="w-3.5 h-3.5" /> : <Maximize2 className="w-3.5 h-3.5" />}
                </button>
              )}
            </>
          )}

          <span
            className={`px-1.5 py-0.5 rounded text-[10px] font-mono font-bold pointer-events-none ${
              streamState === 'LIVE'
                ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/40'
                : streamState === 'CONNECTING'
                ? 'bg-amber-500/20 text-amber-300'
                : 'bg-red-500/20 text-red-400'
            }`}
          >
            {streamState}
          </span>
        </div>
      </div>

      {showSchedModal && (
        <div
          onClick={(e) => e.stopPropagation()}
          className="absolute top-11 right-2 z-30 bg-zinc-950/95 border border-zinc-700 rounded-lg p-3 w-64 shadow-2xl font-mono text-xs space-y-2.5"
        >
          <div className="flex items-center justify-between border-b border-zinc-800 pb-1.5">
            <span className="font-bold text-emerald-400">{title} // SCHEDULE</span>
            <button onClick={() => setShowSchedModal(false)} className="text-zinc-400 hover:text-white cursor-pointer">
              <X className="w-3.5 h-3.5" />
            </button>
          </div>

          <div className="grid grid-cols-3 gap-1">
            <button
              onClick={() => saveSchedule('always_record')}
              className={`py-1.5 rounded text-[10px] font-bold border cursor-pointer ${
                schedMode === 'always_record'
                  ? 'bg-emerald-600 text-white border-emerald-400'
                  : 'bg-zinc-900 text-zinc-300 border-zinc-800'
              }`}
            >
              24/7 REC
            </button>
            <button
              onClick={() => saveSchedule('paused')}
              className={`py-1.5 rounded text-[10px] font-bold border cursor-pointer ${
                schedMode === 'paused'
                  ? 'bg-amber-600 text-white border-amber-400'
                  : 'bg-zinc-900 text-zinc-300 border-zinc-800'
              }`}
            >
              PAUSE NOW
            </button>
            <button
              onClick={() => setSchedMode('schedule')}
              className={`py-1.5 rounded text-[10px] font-bold border cursor-pointer ${
                schedMode === 'schedule'
                  ? 'bg-sky-600 text-white border-sky-400'
                  : 'bg-zinc-900 text-zinc-300 border-zinc-800'
              }`}
            >
              SCHEDULE
            </button>
          </div>

          {schedMode === 'schedule' && (
            <div className="space-y-2 pt-1 border-t border-zinc-800">
              <div className="text-[10px] text-zinc-400">Active Recording Window (PT):</div>
              <div className="flex items-center justify-between gap-2">
                <label className="text-[10px] text-zinc-400">START:</label>
                <input
                  type="time"
                  value={schedStart}
                  onChange={(e) => setSchedStart(e.target.value)}
                  className="bg-zinc-900 border border-zinc-700 rounded px-2 py-1 text-xs text-zinc-100"
                />
              </div>
              <div className="flex items-center justify-between gap-2">
                <label className="text-[10px] text-zinc-400">STOP:</label>
                <input
                  type="time"
                  value={schedEnd}
                  onChange={(e) => setSchedEnd(e.target.value)}
                  className="bg-zinc-900 border border-zinc-700 rounded px-2 py-1 text-xs text-zinc-100"
                />
              </div>
              <button
                onClick={() => saveSchedule('schedule')}
                className="w-full bg-emerald-600 hover:bg-emerald-500 text-white font-bold py-1.5 rounded text-[11px] cursor-pointer"
              >
                SAVE WINDOW
              </button>
            </div>
          )}

          <div className="pt-2 border-t border-zinc-800 space-y-1.5">
            <div className="text-[10px] font-bold text-zinc-300">CAMERA PUSH POSTURE (HOME):</div>
            <div className="grid grid-cols-3 gap-1">
              <button
                onClick={() => {
                  setSilentLogging(false);
                  setAlertWhitelist(false);
                  saveSchedule(schedMode, false, false);
                }}
                className={`py-1 rounded text-[9px] font-bold border cursor-pointer ${
                  !silentLogging && !alertWhitelist
                    ? 'bg-emerald-600 text-white border-emerald-400'
                    : 'bg-zinc-900 text-zinc-400 border-zinc-800'
                }`}
              >
                FAM·SILENT
              </button>
              <button
                onClick={() => {
                  setSilentLogging(true);
                  setAlertWhitelist(false);
                  saveSchedule(schedMode, false, true);
                }}
                title="Record & log clips/Face ID silently to Kill-Feed with zero push alarms while HOME"
                className={`py-1 rounded text-[9px] font-bold border cursor-pointer ${
                  silentLogging
                    ? 'bg-violet-600 text-white border-violet-300'
                    : 'bg-zinc-900 text-zinc-400 border-zinc-800'
                }`}
              >
                LOG·ONLY
              </button>
              <button
                onClick={() => {
                  setSilentLogging(false);
                  setAlertWhitelist(true);
                  saveSchedule(schedMode, true, false);
                }}
                className={`py-1 rounded text-[9px] font-bold border cursor-pointer ${
                  !silentLogging && alertWhitelist
                    ? 'bg-sky-600 text-white border-sky-400'
                    : 'bg-zinc-900 text-zinc-400 border-zinc-800'
                }`}
              >
                FAM·ALERT
              </button>
            </div>
          </div>
        </div>
      )}

      {showLightModal && hwStatus?.supports_light && (
        <div
          onClick={(e) => e.stopPropagation()}
          className="absolute top-11 right-2 z-30 bg-zinc-950/95 border border-zinc-700 rounded-lg p-3 w-72 shadow-2xl font-mono text-xs space-y-2.5"
        >
          <div className="flex items-center justify-between border-b border-zinc-800 pb-1.5">
            <span className="font-bold text-amber-300">{title} // OPTICS & AUDIO</span>
            <button onClick={() => setShowLightModal(false)} className="text-zinc-400 hover:text-white cursor-pointer">
              <X className="w-3.5 h-3.5" />
            </button>
          </div>

          <div className="space-y-1.5">
            <div className="text-[10px] text-zinc-400 font-bold">NIGHT SUPPLEMENT LIGHT MODE:</div>
            <div className="grid grid-cols-2 gap-1.5">
              <button
                disabled={lightBusy}
                onClick={() => applyLightConfig('ir')}
                title="Invisible IR LEDs only — White floodlight NEVER turns on (zero reflective vest glare)"
                className={`py-1.5 px-2 rounded text-[10px] font-bold border flex items-center justify-center gap-1 cursor-pointer ${
                  hwStatus.light?.mode === 'ir'
                    ? 'bg-indigo-600 text-white border-indigo-300'
                    : 'bg-zinc-900 hover:bg-zinc-800 text-zinc-300 border-zinc-800'
                }`}
              >
                <Moon className="w-3 h-3" />
                IR ONLY (NO FLOOD)
              </button>
              <button
                disabled={lightBusy}
                onClick={() => applyLightConfig('smart', 30, false)}
                title="IR until person detected, then gentle 30% white floodlight so vests & plates don't blow out"
                className={`py-1.5 px-2 rounded text-[10px] font-bold border flex items-center justify-center gap-1 cursor-pointer ${
                  hwStatus.light?.mode === 'smart' && !hwStatus.light?.auto_brightness
                    ? 'bg-amber-600 text-white border-amber-300'
                    : 'bg-zinc-900 hover:bg-zinc-800 text-amber-300 border-zinc-800'
                }`}
              >
                <Lightbulb className="w-3 h-3" />
                SMART · 30%
              </button>
              <button
                disabled={lightBusy}
                onClick={() => applyLightConfig('color', draftWhiteBr, false)}
                title="Keep white color floodlight on continuously at night"
                className={`py-1.5 px-2 rounded text-[10px] font-bold border flex items-center justify-center gap-1 cursor-pointer ${
                  hwStatus.light?.mode === 'color'
                    ? 'bg-amber-500 text-zinc-950 border-amber-200'
                    : 'bg-zinc-900 hover:bg-zinc-800 text-zinc-300 border-zinc-800'
                }`}
              >
                <Sun className="w-3 h-3" />
                ALWAYS COLOR
              </button>
              <button
                disabled={lightBusy}
                onClick={() => applyLightConfig('off')}
                title="Turn off both IR and white LEDs (use street/porch light only)"
                className={`py-1.5 px-2 rounded text-[10px] font-bold border flex items-center justify-center gap-1 cursor-pointer ${
                  hwStatus.light?.mode === 'off'
                    ? 'bg-zinc-700 text-white border-zinc-400'
                    : 'bg-zinc-900 hover:bg-zinc-800 text-zinc-400 border-zinc-800'
                }`}
              >
                ALL LIGHTS OFF
              </button>
            </div>
          </div>

          {hwStatus.light?.mode !== 'ir' && hwStatus.light?.mode !== 'off' && (
            <div className="space-y-1.5 pt-1.5 border-t border-zinc-800">
              <div className="flex items-center justify-between text-[10px]">
                <span className="text-zinc-400">FLOODLIGHT LEVEL:</span>
                <span className="text-amber-300 font-bold">
                  {draftAutoBr ? 'AUTO (100%)' : `${draftWhiteBr}% MANUAL`}
                </span>
              </div>
              <div className="flex items-center gap-2">
                <input
                  type="range"
                  min={5}
                  max={100}
                  step={5}
                  value={draftWhiteBr}
                  onChange={(e) => {
                    setDraftAutoBr(false);
                    setDraftWhiteBr(Number(e.target.value));
                  }}
                  className="flex-1 accent-amber-400 cursor-pointer"
                />
                <button
                  disabled={lightBusy}
                  onClick={() => applyLightConfig(hwStatus.light?.mode || 'smart', draftWhiteBr, false)}
                  className="bg-amber-600 hover:bg-amber-500 text-white px-2 py-1 rounded text-[10px] font-bold cursor-pointer"
                >
                  SET
                </button>
              </div>
            </div>
          )}

          <div className="space-y-2 pt-2 border-t border-zinc-800">
            <div className="flex items-center justify-between">
              <span className="text-[10px] text-zinc-400 font-bold">CAMERA RTSP MIC STREAM:</span>
              <button
                disabled={lightBusy}
                onClick={() => {
                  const next = !draftMicEnabled;
                  setDraftMicEnabled(next);
                  saveCameraAudioHw(next);
                }}
                className={`px-2 py-0.5 rounded text-[10px] font-bold border cursor-pointer ${
                  draftMicEnabled
                    ? 'bg-emerald-600 text-white border-emerald-400'
                    : 'bg-zinc-900 text-zinc-400 border-zinc-700'
                }`}
              >
                {draftMicEnabled ? 'ENABLED' : 'DISABLED'}
              </button>
            </div>
            <div className="flex items-center justify-between gap-2 text-[10px]">
              <span className="text-zinc-400 w-16">MIC {draftMicVol}%</span>
              <input
                type="range"
                min={0}
                max={100}
                step={5}
                value={draftMicVol}
                onChange={(e) => setDraftMicVol(Number(e.target.value))}
                className="flex-1 accent-emerald-400 cursor-pointer"
              />
            </div>
            <div className="flex items-center justify-between gap-2 text-[10px]">
              <span className="text-zinc-400 w-16">SPK {draftSpkVol}%</span>
              <input
                type="range"
                min={0}
                max={100}
                step={5}
                value={draftSpkVol}
                onChange={(e) => setDraftSpkVol(Number(e.target.value))}
                className="flex-1 accent-cyan-400 cursor-pointer"
              />
              <button
                disabled={lightBusy}
                onClick={() => saveCameraAudioHw()}
                className="bg-zinc-800 hover:bg-zinc-700 border border-zinc-600 text-zinc-200 px-2 py-0.5 rounded text-[10px] font-bold cursor-pointer"
              >
                SAVE
              </button>
            </div>
          </div>
        </div>
      )}

      {showTalkModal && hwStatus?.supports_two_way_speaker && (
        <div
          onClick={(e) => e.stopPropagation()}
          className="absolute top-11 right-2 z-30 bg-zinc-950/95 border border-cyan-500/50 rounded-lg p-3 w-76 shadow-2xl font-mono text-xs space-y-2.5"
        >
          <div className="flex items-center justify-between border-b border-zinc-800 pb-1.5">
            <span className="font-bold text-cyan-300">{title} // 2-WAY SPEAKER</span>
            <div className="flex items-center gap-2">
              {hwStatus.vendor === 'tapo_rtsp' && hwStatus.tapo_auth_configured && (
                <button
                  onClick={() => setShowTapoRelink((prev) => !prev)}
                  title="Update TP-Link / Tapo Cloud password hash"
                  className="text-[10px] text-zinc-400 hover:text-cyan-300 cursor-pointer"
                >
                  RE-LINK
                </button>
              )}
              <button onClick={() => setShowTalkModal(false)} className="text-zinc-400 hover:text-white cursor-pointer">
                <X className="w-3.5 h-3.5" />
              </button>
            </div>
          </div>

          {hwStatus.vendor === 'tapo_rtsp' && (!hwStatus.tapo_auth_configured || showTapoRelink) ? (
            <div className="space-y-2 bg-zinc-900/90 border border-amber-500/40 rounded p-2.5">
              <div className="text-[10px] text-amber-200 leading-snug">
                <span className="font-bold">1-Time Tapo Speaker Link:</span> Enter your TP-Link / Tapo app password (verified live & stored only as a SHA-256 hash; unlocks both Loft & Living Room).
              </div>
              <div className="flex items-center gap-1.5">
                <input
                  type="password"
                  value={tapoPassInput}
                  onChange={(e) => setTapoPassInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') linkTapoSpeakers();
                  }}
                  placeholder="TP-Link / Tapo app password..."
                  className="flex-1 bg-zinc-950 border border-zinc-700 rounded px-2 py-1 text-[11px] text-zinc-100 placeholder:text-zinc-500"
                />
                <button
                  disabled={talkBusy || !tapoPassInput.trim()}
                  onClick={linkTapoSpeakers}
                  className="bg-amber-600 hover:bg-amber-500 disabled:opacity-40 text-white px-2.5 py-1 rounded text-[10px] font-bold cursor-pointer"
                >
                  LINK
                </button>
              </div>
            </div>
          ) : (
            <>
              {canBrowserMic ? (
                <button
                  disabled={talkBusy && !isRecordingPtt}
                  onMouseDown={startPushToTalk}
                  onMouseUp={stopPushToTalk}
                  onMouseLeave={stopPushToTalk}
                  onTouchStart={(e) => {
                    e.preventDefault();
                    startPushToTalk();
                  }}
                  onTouchEnd={(e) => {
                    e.preventDefault();
                    stopPushToTalk();
                  }}
                  className={`w-full py-2 rounded font-bold text-[11px] border flex items-center justify-center gap-1.5 cursor-pointer select-none transition-colors ${
                    isRecordingPtt
                      ? 'bg-red-600 text-white border-red-300 animate-pulse'
                      : 'bg-cyan-950/90 hover:bg-cyan-900 text-cyan-200 border-cyan-500/60'
                  }`}
                >
                  <Mic className="w-3.5 h-3.5" />
                  {isRecordingPtt ? 'RELEASE TO BROADCAST' : 'HOLD TO TALK (LIVE MIC)'}
                </button>
              ) : (
                <div className="text-[10px] text-zinc-400 bg-zinc-900/90 border border-zinc-800 rounded px-2 py-1.5 leading-snug">
                  Browser live mic requires HTTPS/localhost — quick presets & custom voice below work over HTTP!
                </div>
              )}

              {hwStatus.vendor === 'tapo_rtsp' ? (
                <div className="grid grid-cols-2 gap-1.5">
                  <button
                    disabled={talkBusy}
                    onClick={() => sendSpeakerMessage('dinner_ready')}
                    className="bg-zinc-900 hover:bg-zinc-800 border border-zinc-700 py-1.5 px-2 rounded text-[10px] text-amber-300 font-bold cursor-pointer text-left"
                  >
                    🍽️ Dinner's Ready
                  </button>
                  <button
                    disabled={talkBusy}
                    onClick={() => sendSpeakerMessage('check_in')}
                    className="bg-zinc-900 hover:bg-zinc-800 border border-zinc-700 py-1.5 px-2 rounded text-[10px] text-emerald-300 font-bold cursor-pointer text-left"
                  >
                    👋 Check In
                  </button>
                  <button
                    disabled={talkBusy}
                    onClick={() => sendSpeakerMessage('coming_out')}
                    className="bg-zinc-900 hover:bg-zinc-800 border border-zinc-700 py-1.5 px-2 rounded text-[10px] text-zinc-300 font-bold cursor-pointer text-left"
                  >
                    🏃 One Moment
                  </button>
                  <button
                    disabled={talkBusy}
                    onClick={() => triggerSpeakerChime('polite', false)}
                    className="bg-zinc-900 hover:bg-zinc-800 border border-zinc-700 py-1.5 px-2 rounded text-[10px] text-sky-300 font-bold cursor-pointer text-left"
                  >
                    🔔 Polite Chime
                  </button>
                </div>
              ) : (
                <div className="grid grid-cols-2 gap-1.5">
                  <button
                    disabled={talkBusy}
                    onClick={() => sendSpeakerMessage('leave_door')}
                    className="bg-zinc-900 hover:bg-zinc-800 border border-zinc-700 py-1.5 px-2 rounded text-[10px] text-amber-300 font-bold cursor-pointer text-left"
                  >
                    📦 Leave at Door
                  </button>
                  <button
                    disabled={talkBusy}
                    onClick={() => sendSpeakerMessage('coming_out')}
                    className="bg-zinc-900 hover:bg-zinc-800 border border-zinc-700 py-1.5 px-2 rounded text-[10px] text-emerald-300 font-bold cursor-pointer text-left"
                  >
                    👋 Coming Out
                  </button>
                  <button
                    disabled={talkBusy}
                    onClick={() => sendSpeakerMessage('no_soliciting')}
                    className="bg-zinc-900 hover:bg-zinc-800 border border-zinc-700 py-1.5 px-2 rounded text-[10px] text-zinc-300 font-bold cursor-pointer text-left"
                  >
                    🚫 No Soliciting
                  </button>
                  <button
                    disabled={talkBusy}
                    onClick={() => triggerSpeakerChime('polite', false)}
                    className="bg-zinc-900 hover:bg-zinc-800 border border-zinc-700 py-1.5 px-2 rounded text-[10px] text-sky-300 font-bold cursor-pointer text-left"
                  >
                    🔔 Polite Chime
                  </button>
                </div>
              )}

              <button
                disabled={talkBusy}
                onClick={() => triggerSpeakerChime('warning', true)}
                className="w-full bg-red-950/90 hover:bg-red-900 border border-red-500/60 py-1.5 px-2 rounded text-[10px] text-red-200 font-bold flex items-center justify-center gap-1.5 cursor-pointer"
              >
                <Siren className="w-3.5 h-3.5 text-red-400" />
                SIREN CHIME + RECORDING WARNING
              </button>

              <div className="flex items-center gap-1.5 pt-1 border-t border-zinc-800">
                <input
                  type="text"
                  value={ttsText}
                  onChange={(e) => setTtsText(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') sendSpeakerMessage(ttsText);
                  }}
                  placeholder="Type custom message to speak..."
                  className="flex-1 bg-zinc-900 border border-zinc-700 rounded px-2 py-1 text-[11px] text-zinc-100 placeholder:text-zinc-500"
                />
                <button
                  disabled={talkBusy || !ttsText.trim()}
                  onClick={() => sendSpeakerMessage(ttsText)}
                  className="bg-cyan-600 hover:bg-cyan-500 disabled:opacity-40 text-white px-2.5 py-1 rounded text-[10px] font-bold flex items-center gap-1 cursor-pointer"
                >
                  <Send className="w-3 h-3" />
                  SAY
                </button>
              </div>
            </>
          )}

          {talkStatusMsg && (
            <div className="text-[10px] text-cyan-300 truncate pt-0.5">{talkStatusMsg}</div>
          )}
        </div>
      )}
    </div>
  );
}
