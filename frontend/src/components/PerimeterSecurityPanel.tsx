import { useEffect, useRef, useState } from 'react';
import type { PerimeterSnapshot } from '../types/telemetry';
import { HOUSE_PIN_ANCHORS, House3DViewer } from './House3DViewer';
import {
  AlertTriangle,
  Battery,
  BellOff,
  Box,
  Check,
  Crosshair,
  LayoutGrid,
  Lock,
  Moon,
  Pencil,
  Radio,
  RotateCcw,
  Settings,
  ShieldAlert,
  ShieldCheck,
  ShieldOff,
  Volume2,
  X,
} from 'lucide-react';

interface PerimeterSecurityPanelProps {
  hostIp: string;
  perimeter?: PerimeterSnapshot;
  onRefreshKillFeed?: () => void;
  onFocusStream?: (streamName: string) => void;
}

function formatGrace(sec?: number): string {
  const safe = Math.max(0, sec || 0);
  const m = Math.floor(safe / 60);
  const s = safe % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

export function PerimeterSecurityPanel({
  hostIp,
  perimeter,
  onRefreshKillFeed,
  onFocusStream,
}: PerimeterSecurityPanelProps) {
  const apiBase = `http://${hostIp}:8080`;
  const [localSnap, setLocalSnap] = useState<PerimeterSnapshot | null>(null);
  const [breachPromptZones, setBreachPromptZones] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [viewMode, setViewMode] = useState<'3d' | 'grid'>('3d');
  const [selectedZoneId, setSelectedZoneId] = useState<string | null>(null);
  const [focusSeq, setFocusSeq] = useState(0);
  const [highlightConfigZoneId, setHighlightConfigZoneId] = useState<string | null>(null);

  const [consoleAudio, setConsoleAudio] = useState<boolean>(() => {
    try {
      return window.localStorage.getItem('command_perimeter_audio') !== 'off';
    } catch {
      return true;
    }
  });

  const [showConfig, setShowConfig] = useState(false);
  const [modalTab, setModalTab] = useState<'hub' | 'zones'>('hub');
  const [cfgMode, setCfgMode] = useState<'auto' | 'cloud' | 'local' | 'mock'>('auto');
  const [uaid, setUaid] = useState('');
  const [secretKey, setSecretKey] = useState('');
  const [hasSavedSecret, setHasSavedSecret] = useState(false);
  const [localHubIp, setLocalHubIp] = useState('');
  const [localClientId, setLocalClientId] = useState('');
  const [localClientSecret, setLocalClientSecret] = useState('');
  const [localSubnetId, setLocalSubnetId] = useState('');
  const [autoDisarmEnabled, setAutoDisarmEnabled] = useState(true);
  const [autoDisarmTime, setAutoDisarmTime] = useState('05:00');
  const [commandUiUrl, setCommandUiUrl] = useState(`http://${hostIp}:3000`);
  const [zoneAliases, setZoneAliases] = useState<Record<string, string>>({});
  const [zonePins, setZonePins] = useState<Record<string, string>>({});
  const [cfgMsg, setCfgMsg] = useState<string | null>(null);

  useEffect(() => {
    if (!perimeter) {
      fetch(`${apiBase}/api/perimeter/status`)
        .then((res) => (res.ok ? res.json() : null))
        .then((data) => {
          if (data) setLocalSnap(data);
        })
        .catch(() => {});
    }
  }, [apiBase, perimeter]);

  const snap = perimeter || localSnap;
  const refreshCbRef = useRef(onRefreshKillFeed);
  refreshCbRef.current = onRefreshKillFeed;
  const prevAlarmStateRef = useRef<string | null>(null);

  useEffect(() => {
    const curr = snap?.alarm_state ?? null;
    if (curr === 'triggered' && prevAlarmStateRef.current !== null && prevAlarmStateRef.current !== 'triggered') {
      setViewMode('3d');
      const timer = window.setTimeout(() => {
        refreshCbRef.current?.();
      }, 650);
      prevAlarmStateRef.current = curr;
      return () => window.clearTimeout(timer);
    }
    prevAlarmStateRef.current = curr;
  }, [snap?.alarm_state]);

  // Browser Console Avionics Master Caution Chime (D5-D5 -> A4 syncopated mallet, 1.25s cycle)
  useEffect(() => {
    const sirenShouldSound =
      snap?.alarm_state === 'triggered' && snap?.siren_state === 'on' && consoleAudio;
    if (!sirenShouldSound) return;

    const AudioCtx =
      window.AudioContext ||
      (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
    if (!AudioCtx) return;

    let ctx: AudioContext | null = null;
    const hits: [number, number, number, number][] = [
      [0.0, 587.33, 0.16, 0.42],
      [0.13, 587.33, 0.16, 0.45],
      [0.28, 440.0, 0.15, 0.62],
    ];

    const playCautionCycle = () => {
      try {
        if (!ctx) ctx = new AudioCtx();
        if (ctx.state === 'suspended') {
          ctx.resume().catch(() => {});
        }
        const base = ctx.currentTime;
        for (const [offset, freq, peak, dur] of hits) {
          const t0 = base + offset;
          const masterGain = ctx.createGain();
          masterGain.gain.setValueAtTime(0.0001, t0);
          masterGain.gain.linearRampToValueAtTime(peak, t0 + 0.004);
          masterGain.gain.exponentialRampToValueAtTime(0.0005, t0 + dur);
          masterGain.connect(ctx.destination);

          const oscSine = ctx.createOscillator();
          const sineMix = ctx.createGain();
          oscSine.type = 'sine';
          oscSine.frequency.setValueAtTime(freq, t0);
          sineMix.gain.setValueAtTime(0.62, t0);
          oscSine.connect(sineMix);
          sineMix.connect(masterGain);

          const oscTri = ctx.createOscillator();
          const triMix = ctx.createGain();
          oscTri.type = 'triangle';
          oscTri.frequency.setValueAtTime(freq, t0);
          triMix.gain.setValueAtTime(0.38, t0);
          oscTri.connect(triMix);
          triMix.connect(masterGain);

          oscSine.start(t0);
          oscTri.start(t0);
          oscSine.stop(t0 + dur + 0.01);
          oscTri.stop(t0 + dur + 0.01);
        }
      } catch {
        // ignore browser autoplay block
      }
    };

    playCautionCycle();
    const interval = window.setInterval(playCautionCycle, 1250);
    return () => {
      window.clearInterval(interval);
      if (ctx) {
        ctx.close().catch(() => {});
      }
    };
  }, [snap?.alarm_state, snap?.siren_state, consoleAudio]);

  const toggleConsoleAudio = () => {
    setConsoleAudio((prev) => {
      const next = !prev;
      try {
        window.localStorage.setItem('command_perimeter_audio', next ? 'on' : 'off');
      } catch {
        // ignore
      }
      return next;
    });
  };

  const openConfigModal = async (initialTab: 'hub' | 'zones' = 'hub', targetEntityId: string | null = null) => {
    setModalTab(initialTab);
    setHighlightConfigZoneId(targetEntityId);
    setShowConfig(true);
    setCfgMsg(null);
    try {
      const res = await fetch(`${apiBase}/api/perimeter/config`);
      if (res.ok) {
        const data = await res.json();
        setCfgMode(data.mode || 'auto');
        setUaid(data.uaid || '');
        setSecretKey('');
        setHasSavedSecret(Boolean(data.has_secret_key));
        setLocalHubIp(data.local_hub_ip || '');
        setLocalClientId(data.local_client_id || '');
        setLocalClientSecret('');
        setLocalSubnetId(data.local_subnet_id || '');
        setAutoDisarmEnabled(data.auto_disarm_enabled !== false);
        setAutoDisarmTime(data.auto_disarm_time || '05:00');
        setCommandUiUrl(data.command_ui_url || `http://${hostIp}:3000`);
        setZoneAliases(data.zone_aliases || {});
        setZonePins(data.zone_pins || {});
      }
    } catch {
      setCfgMsg('Could not fetch current hub config.');
    }
  };

  const saveHubConfig = async () => {
    setCfgMsg('Saving...');
    const payload: Record<string, unknown> = {
      mode: cfgMode,
      uaid,
      local_hub_ip: localHubIp,
      local_client_id: localClientId,
      local_subnet_id: localSubnetId,
      auto_disarm_enabled: autoDisarmEnabled,
      auto_disarm_time: autoDisarmTime,
      command_ui_url: commandUiUrl.trim(),
      zone_aliases: zoneAliases,
      zone_pins: zonePins,
    };
    if (secretKey.trim()) payload.secret_key = secretKey.trim();
    if (localClientSecret.trim()) payload.local_client_secret = localClientSecret.trim();

    try {
      const res = await fetch(`${apiBase}/api/perimeter/config`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (res.ok) {
        const updated = await res.json();
        setHasSavedSecret(Boolean(updated.has_secret_key));
        setZoneAliases(updated.zone_aliases || {});
        setZonePins(updated.zone_pins || {});
        setSecretKey('');
        setLocalClientSecret('');
        setCfgMsg(
          modalTab === 'zones'
            ? 'Saved custom sensor names & 3D pins!'
            : 'Saved! Handshaking with YoLink...'
        );
      } else {
        setCfgMsg('Failed to save config.');
      }
    } catch {
      setCfgMsg('Network error saving config.');
    }
  };

  const handleArmNight = async (force: boolean) => {
    setBusy(true);
    try {
      const res = await fetch(`${apiBase}/api/perimeter/arm-night`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ force }),
      });
      if (res.ok) {
        const data = await res.json();
        if (data.snapshot) setLocalSnap(data.snapshot);
        if (!data.success && Array.isArray(data.open_zones) && data.open_zones.length > 0) {
          setBreachPromptZones(data.open_zones);
        } else {
          setBreachPromptZones(null);
        }
      }
    } catch {
      // ignore transient fetch failure
    } finally {
      setBusy(false);
    }
  };

  const handleDisarmAndSilence = async () => {
    setBusy(true);
    setBreachPromptZones(null);
    try {
      const res = await fetch(`${apiBase}/api/perimeter/disarm`, { method: 'POST' });
      if (res.ok) {
        const data = await res.json();
        if (data.snapshot) setLocalSnap(data.snapshot);
      }
    } catch {
      // ignore
    } finally {
      setBusy(false);
    }
  };

  const handleMockZoneClick = async (entityId: string) => {
    if (!snap?.is_mock) return;
    try {
      const res = await fetch(
        `${apiBase}/api/perimeter/mock-toggle/${encodeURIComponent(entityId)}`,
        {
          method: 'POST',
        }
      );
      if (res.ok) {
        const data = await res.json();
        if (data.snapshot) setLocalSnap(data.snapshot);
      }
    } catch {
      // ignore
    }
  };

  const handleLocateZoneIn3D = (entityId: string) => {
    setSelectedZoneId(entityId);
    setFocusSeq((n) => n + 1);
    setViewMode('3d');
  };

  const handleToggleBypass = async (entityId: string) => {
    try {
      const res = await fetch(
        `${apiBase}/api/perimeter/bypass/${encodeURIComponent(entityId)}`,
        { method: 'POST' }
      );
      if (res.ok) {
        const data = await res.json();
        if (data.snapshot) setLocalSnap(data.snapshot);
      }
    } catch {
      // ignore
    }
  };

  const handleBypassAllOpen = async () => {
    setBusy(true);
    try {
      const res = await fetch(`${apiBase}/api/perimeter/bypass-open`, { method: 'POST' });
      if (res.ok) {
        const data = await res.json();
        if (data.snapshot) setLocalSnap(data.snapshot);
      }
    } catch {
      // ignore
    } finally {
      setBusy(false);
    }
  };

  if (!snap) return null;

  const alarmState = snap.alarm_state;
  const isTriggered = alarmState === 'triggered';
  const isArmed = alarmState === 'armed_night' || alarmState === 'armed_away';
  const isArming = alarmState === 'arming';
  const unbypassedOpenCount = snap.zones.filter((z) => z.state === 'OPEN' && !z.bypassed).length;
  const bypassedCount = snap.zones.filter((z) => Boolean(z.bypassed)).length;

  let bannerStyle = 'bg-emerald-500/15 text-emerald-400 border-emerald-500/40';
  let bannerText = 'DISARMED';
  if (isTriggered) {
    bannerStyle =
      'animate-pulse bg-red-600 text-white border-red-300 shadow-[0_0_22px_rgba(220,38,38,0.85)]';
    bannerText = `🚨 TRIGGERED${snap.triggered_by ? ` // ${snap.triggered_by.toUpperCase()}` : ''}`;
  } else if (alarmState === 'armed_night') {
    bannerStyle =
      'bg-sky-500/20 text-sky-300 border-sky-400/50 shadow-[0_0_12px_rgba(56,189,248,0.25)]';
    bannerText = '🌙 ARMED NIGHT · SENSORS ONLY (CAMS SILENT)';
  } else if (alarmState === 'armed_away') {
    bannerStyle =
      'bg-amber-500/20 text-amber-300 border-amber-400/50 shadow-[0_0_12px_rgba(245,158,11,0.25)]';
    bannerText = '🛫 ARMED AWAY · SENSORS + INDOOR CAMS';
  } else if (isArming) {
    bannerStyle = 'bg-amber-500/20 text-amber-300 border-amber-400/50 animate-pulse';
    bannerText = `⏳ ARMING (${formatGrace(snap.away_grace_remaining)})`;
  }

  return (
    <section
      className={`rounded-xl border p-3 sm:p-3.5 font-mono transition-colors ${
        isTriggered
          ? 'bg-red-950/35 border-red-500 shadow-[0_0_30px_rgba(220,38,38,0.3)]'
          : isArmed
          ? 'bg-zinc-900/90 border-sky-500/40'
          : 'bg-zinc-900/75 border-zinc-800'
      }`}
    >
      {/* Header & Control Row */}
      <div className="flex flex-wrap items-center justify-between gap-2.5 pb-2.5 border-b border-zinc-800/90">
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex items-center gap-1.5 text-xs font-bold uppercase tracking-wider text-zinc-100">
            {isTriggered ? (
              <ShieldAlert className="w-4 h-4 text-red-400 shrink-0" />
            ) : isArmed ? (
              <Lock className="w-4 h-4 text-sky-400 shrink-0" />
            ) : (
              <ShieldCheck className="w-4 h-4 text-emerald-400 shrink-0" />
            )}
            <span>PERIMETER</span>
          </div>

          <span className={`px-2.5 py-0.5 rounded text-[11px] font-bold border ${bannerStyle}`}>
            {bannerText}
          </span>

          {snap.siren_state === 'on' && (
            <span className="animate-bounce px-2 py-0.5 rounded text-[10px] font-bold bg-red-600 text-white border border-red-300 flex items-center gap-1">
              <Volume2 className="w-3 h-3" />
              SIREN ACTIVE
            </span>
          )}

          {/* Hub Link Badge */}
          <span
            onClick={() => openConfigModal('hub')}
            title={
              snap.last_error
                ? `Hub Warning: ${snap.last_error}`
                : snap.is_mock
                ? 'Mock Simulator Active — Click to connect YoLink YS1603-UC Hub'
                : 'YoLink LoRa Hub Connected'
            }
            className={`px-2 py-0.5 rounded text-[10px] font-bold border flex items-center gap-1 cursor-pointer ${
              snap.is_mock
                ? 'bg-zinc-800/90 text-amber-300 border-amber-500/30 hover:bg-zinc-800'
                : snap.mqtt_live
                ? 'bg-emerald-950/80 text-emerald-300 border-emerald-500/30'
                : 'bg-amber-950/80 text-amber-300 border-amber-500/40'
            }`}
          >
            <Radio className="w-3 h-3" />
            {snap.is_mock
              ? 'MOCK / OFFLINE'
              : snap.transport === 'yolink_local'
              ? 'LOCAL HUB · LAN'
              : snap.mqtt_live
              ? 'YOLINK · MQTT LIVE'
              : 'YOLINK · HTTP'}
          </span>

          <span className="text-[10px] sm:text-[11px] text-zinc-400">
            Auto-Disarm:{' '}
            <span className="text-zinc-200 font-semibold">
              {snap.auto_disarm_enabled ? `${snap.auto_disarm_time} AM Daily` : 'OFF'}
            </span>{' '}
            <span className="text-zinc-600">|</span> Open Zones:{' '}
            <span
              className={`font-bold ${
                unbypassedOpenCount > 0
                  ? 'text-red-400'
                  : snap.breached_count > 0
                  ? 'text-amber-300'
                  : 'text-emerald-400'
              }`}
            >
              {snap.breached_count}/{snap.total_zones}
            </span>
            {bypassedCount > 0 && (
              <span className="ml-1 px-1.5 py-0.2 rounded bg-amber-950/90 border border-amber-500/40 text-amber-300 text-[9px] font-bold">
                {bypassedCount} BYPASSED
              </span>
            )}
          </span>
        </div>

        {/* One-Click Tactical Controls */}
        <div className="flex items-center gap-1.5 flex-wrap">
          {unbypassedOpenCount > 0 && (
            <button
              onClick={handleBypassAllOpen}
              disabled={busy}
              title={
                isTriggered
                  ? 'Bypass open zone(s), silence siren, and keep remaining zones armed'
                  : 'Bypass currently open window/door(s) until they physically close'
              }
              className="px-2.5 py-1 rounded text-[11px] font-bold bg-amber-500 hover:bg-amber-400 text-zinc-950 border border-amber-200 flex items-center gap-1 cursor-pointer transition-all shadow-[0_0_12px_rgba(245,158,11,0.35)]"
            >
              <ShieldOff className="w-3.5 h-3.5" />
              {isTriggered ? 'BYPASS & SILENCE' : `BYPASS OPEN (${unbypassedOpenCount})`}
            </button>
          )}

          <button
            onClick={() => handleArmNight(false)}
            disabled={busy || alarmState === 'armed_night'}
            className={`px-2.5 py-1 rounded text-[11px] font-bold border flex items-center gap-1 cursor-pointer transition-all ${
              alarmState === 'armed_night'
                ? 'bg-sky-900/40 text-sky-300 border-sky-500/40 opacity-75 cursor-default'
                : 'bg-sky-600 hover:bg-sky-500 text-white border-sky-400 shadow-[0_0_10px_rgba(2,132,199,0.3)]'
            }`}
          >
            <Moon className="w-3.5 h-3.5" />
            ARM NIGHT
          </button>

          <button
            onClick={handleDisarmAndSilence}
            disabled={busy}
            title="Single-click immediate Disarm & Master Bedroom Siren Kill"
            className={`px-2.5 py-1 rounded text-[11px] font-bold border flex items-center gap-1 cursor-pointer transition-all ${
              isTriggered || snap.siren_state === 'on'
                ? 'bg-red-600 hover:bg-red-500 text-white border-red-300 shadow-[0_0_15px_rgba(220,38,38,0.6)]'
                : 'bg-zinc-800 hover:bg-zinc-700 text-emerald-300 border-zinc-700'
            }`}
          >
            <BellOff className="w-3.5 h-3.5" />
            DISARM &amp; SILENCE
          </button>

          {/* 3D Spatial Twin <-> 12-Pill List Toggle */}
          <button
            onClick={() => setViewMode((v) => (v === '3d' ? 'grid' : '3d'))}
            title={
              viewMode === '3d'
                ? 'Switch to Compact 12-Sensor Pill Grid'
                : 'Switch to Interactive 3D Spatial House View'
            }
            className={`px-2 py-1 rounded text-[10px] font-bold border flex items-center gap-1 cursor-pointer ${
              viewMode === '3d'
                ? 'bg-cyan-950/80 hover:bg-cyan-900/80 text-cyan-300 border-cyan-500/40'
                : 'bg-zinc-900 hover:bg-zinc-800 text-zinc-300 border-zinc-700'
            }`}
          >
            {viewMode === '3d' ? <LayoutGrid className="w-3.5 h-3.5" /> : <Box className="w-3.5 h-3.5" />}
            <span>{viewMode === '3d' ? 'LIST' : '3D TWIN'}</span>
          </button>

          <button
            onClick={toggleConsoleAudio}
            title={
              consoleAudio
                ? 'Browser Console Klaxon ON — Click to mute PC speaker siren'
                : 'Browser Console Klaxon MUTED — Click to enable PC speaker siren'
            }
            className={`px-2 py-1 rounded text-[10px] font-bold border flex items-center gap-1 cursor-pointer ${
              consoleAudio
                ? 'bg-zinc-900 hover:bg-zinc-800 text-emerald-300 border-zinc-700'
                : 'bg-zinc-900 hover:bg-zinc-800 text-zinc-500 border-zinc-800'
            }`}
          >
            {consoleAudio ? <Volume2 className="w-3.5 h-3.5" /> : <BellOff className="w-3.5 h-3.5" />}
            <span className="hidden sm:inline">{consoleAudio ? 'AUDIO' : 'MUTED'}</span>
          </button>

          <button
            onClick={() => openConfigModal('zones')}
            title="Rename Sensors, Set 3D Pins, or Configure YoLink LoRa Hub"
            className="px-2 py-1 rounded text-[10px] font-bold bg-zinc-900 hover:bg-zinc-800 text-zinc-300 hover:text-white border border-zinc-700 flex items-center gap-1 cursor-pointer"
          >
            <Settings className="w-3.5 h-3.5 text-emerald-400" />
            <span className="hidden sm:inline">ZONES / HUB</span>
          </button>
        </div>
      </div>

      {/* Main Viewport: Interactive 3D Spatial House Twin (Default) OR Compact 3x4 Grid Fallback */}
      <div className="pt-2.5">
        {viewMode === '3d' ? (
          <House3DViewer
            snap={snap}
            selectedZoneId={selectedZoneId}
            focusSeq={focusSeq}
            onSelectZone={setSelectedZoneId}
            onEditZone={(eid) => openConfigModal('zones', eid)}
            onToggleBypass={handleToggleBypass}
            onZoneClick={handleMockZoneClick}
            onFocusStream={onFocusStream}
          />
        ) : (
          <div className="space-y-1.5">
            <div className="flex items-center justify-between text-[10px] text-zinc-400 px-0.5">
              <span>
                {snap.is_mock
                  ? 'Click any sensor card to simulate OPEN/CLOSED, or click 🎯 to locate on 3D Twin.'
                  : 'Click any sensor card to isolate its floor & zoom to it in the 3D Twin, or click ✏️ to rename/re-pin.'}
              </span>
              {selectedZoneId && (
                <button
                  type="button"
                  onClick={() => setSelectedZoneId(null)}
                  className="text-cyan-400 hover:text-cyan-300 cursor-pointer"
                >
                  Clear selection
                </button>
              )}
            </div>
            <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-1.5">
              {snap.zones.map((z) => {
                const isOpen = z.state === 'OPEN';
                const isBypassed = Boolean(z.bypassed);
                const isSelected = selectedZoneId === z.entity_id;
                return (
                    <div
                      key={z.entity_id}
                      onClick={() => {
                        if (snap.is_mock) {
                          handleMockZoneClick(z.entity_id);
                        } else {
                          handleLocateZoneIn3D(z.entity_id);
                        }
                      }}
                      title={
                        snap.is_mock
                          ? `${z.name} (${z.state}) — Click to simulate door/window toggle`
                          : `Click to locate ${z.name} (${z.state}) on the 3D Spatial House Twin`
                      }
                      className={`group px-2.5 py-1.5 rounded-md border flex items-center justify-between gap-1.5 text-[11px] cursor-pointer transition-all ${
                        isSelected ? 'ring-1 ring-cyan-400 ' : ''
                      }${
                        isBypassed
                          ? 'bg-amber-950/50 hover:bg-amber-950/70 border-amber-500/60 text-amber-200'
                          : isOpen
                          ? 'bg-red-950/80 hover:bg-red-950 border-red-500 text-red-100 shadow-[0_0_10px_rgba(239,68,68,0.22)]'
                          : 'bg-zinc-950/85 hover:bg-zinc-900 hover:border-cyan-400/50 border-emerald-500/20 text-zinc-300'
                      }`}
                    >
                      <div className="flex items-center gap-1.5 min-w-0">
                        <span
                          className={`w-2 h-2 rounded-full shrink-0 ${
                            isBypassed
                              ? 'bg-amber-400'
                              : isOpen
                              ? 'bg-red-500 animate-ping'
                              : 'bg-emerald-500'
                          }`}
                        />
                        <span className="truncate font-semibold">{z.name}</span>
                      </div>

                      <div className="flex items-center gap-1 shrink-0">
                        {z.battery != null && (
                          <span
                            className={`hidden sm:inline-flex items-center gap-0.5 text-[9px] ${
                              z.battery <= 25 ? 'text-red-400 font-bold' : 'text-zinc-500'
                            }`}
                          >
                            <Battery className="w-2.5 h-2.5" />
                            {z.battery}%
                          </span>
                        )}
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation();
                            handleToggleBypass(z.entity_id);
                          }}
                          title={
                            isBypassed
                              ? `Remove bypass on ${z.name}`
                              : `Bypass ${z.name} (auto-clears once closed)`
                          }
                          className={`px-1.5 py-0.5 rounded text-[9px] font-bold border cursor-pointer ${
                            isBypassed
                              ? 'bg-amber-500/25 hover:bg-amber-500/40 text-amber-200 border-amber-400/60'
                              : isOpen
                              ? 'bg-amber-500 hover:bg-amber-400 text-zinc-950 border-amber-200'
                              : 'bg-zinc-900 hover:bg-amber-500/20 text-zinc-400 hover:text-amber-300 border-zinc-700 opacity-0 group-hover:opacity-100'
                          }`}
                        >
                          {isBypassed ? 'UNBYP' : 'BYPASS'}
                        </button>
                        {snap.is_mock && (
                          <button
                            type="button"
                            onClick={(e) => {
                              e.stopPropagation();
                              handleLocateZoneIn3D(z.entity_id);
                            }}
                            title={`Locate ${z.name} in 3D House Twin`}
                            className="p-0.5 rounded text-cyan-400 hover:bg-cyan-950/80 cursor-pointer"
                          >
                            <Crosshair className="w-3 h-3" />
                          </button>
                        )}
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation();
                            openConfigModal('zones', z.entity_id);
                          }}
                          title={`Rename or change 3D pin for ${z.name}`}
                          className="p-0.5 rounded text-zinc-500 hover:text-emerald-300 hover:bg-zinc-800 opacity-70 group-hover:opacity-100 cursor-pointer"
                        >
                          <Pencil className="w-2.5 h-2.5" />
                        </button>
                        <span
                          className={`text-[9px] font-bold px-1.5 py-0.5 rounded ${
                            isBypassed
                              ? 'bg-amber-500/25 text-amber-300'
                              : isOpen
                              ? 'bg-red-600 text-white'
                              : 'bg-emerald-950/90 text-emerald-400'
                          }`}
                        >
                          {isBypassed ? 'BYPASS' : isOpen ? 'OPEN' : 'CLOSED'}
                        </span>
                      </div>
                    </div>
                );
              })}
            </div>
          </div>
        )}
      </div>

      {/* Pre-Arm Breach Check Modal */}
      {breachPromptZones && (
        <div
          onClick={() => setBreachPromptZones(null)}
          className="fixed inset-0 z-50 bg-black/80 backdrop-blur-xs flex items-center justify-center p-4"
        >
          <div
            onClick={(e) => e.stopPropagation()}
            className="bg-zinc-950 border border-red-500 rounded-xl max-w-md w-full p-4 space-y-3 shadow-[0_0_35px_rgba(239,68,68,0.3)]"
          >
            <div className="flex items-center justify-between border-b border-zinc-800 pb-2">
              <div className="flex items-center gap-2 text-red-400 font-bold text-xs uppercase">
                <AlertTriangle className="w-4 h-4 shrink-0" />
                <span>Pre-Arm Breach Warning</span>
              </div>
              <button
                onClick={() => setBreachPromptZones(null)}
                className="text-zinc-400 hover:text-white cursor-pointer"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            <p className="text-xs text-zinc-300 leading-relaxed">
              Cannot cleanly arm Night Mode —{' '}
              <span className="text-red-400 font-bold">{breachPromptZones.length}</span> perimeter
              zone(s) are currently <span className="text-red-400 font-bold">OPEN</span>:
            </p>

            <div className="bg-red-950/35 border border-red-500/40 rounded-lg p-2.5 space-y-1">
              {breachPromptZones.map((zn) => (
                <div key={zn} className="text-xs font-bold text-red-200 flex items-center gap-2">
                  <span className="w-1.5 h-1.5 rounded-full bg-red-500" />
                  {zn}
                </div>
              ))}
            </div>

            <p className="text-[10px] text-zinc-400">
              Force-arming bypasses the open zone(s) above until they close, while arming the rest of
              your perimeter immediately.
            </p>

            <div className="flex items-center justify-end gap-2 pt-1">
              <button
                onClick={() => setBreachPromptZones(null)}
                className="px-3 py-1.5 rounded bg-zinc-800 hover:bg-zinc-700 text-zinc-200 text-xs font-bold cursor-pointer"
              >
                Cancel
              </button>
              <button
                onClick={() => handleArmNight(true)}
                className="px-3 py-1.5 rounded bg-red-600 hover:bg-red-500 text-white text-xs font-bold border border-red-400 cursor-pointer"
              >
                Force Arm Anyway
              </button>
            </div>
          </div>
        </div>
      )}

      {/* YoLink Hub Credentials, Zone Rename & 3D Floorplan Pin Modal */}
      {showConfig && (
        <div
          onClick={() => setShowConfig(false)}
          className="fixed inset-0 z-50 bg-black/80 backdrop-blur-xs flex items-center justify-center p-4"
        >
          <div
            onClick={(e) => e.stopPropagation()}
            className="bg-zinc-950 border border-emerald-500/50 rounded-xl max-w-2xl w-full p-4 space-y-3 shadow-2xl text-xs max-h-[88vh] overflow-y-auto"
          >
            <div className="flex items-center justify-between border-b border-zinc-800 pb-2">
              <span className="font-bold text-emerald-400 uppercase">
                🛡️ Perimeter Sensors, 3D Floorplan Pins &amp; YoLink Hub
              </span>
              <button
                onClick={() => setShowConfig(false)}
                className="text-zinc-400 hover:text-white cursor-pointer"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            {/* Modal Tabs */}
            <div className="flex gap-2 border-b border-zinc-800 pb-2">
              <button
                onClick={() => {
                  setModalTab('zones');
                  setCfgMsg(null);
                }}
                className={`flex-1 py-1.5 rounded font-bold text-[11px] border cursor-pointer ${
                  modalTab === 'zones'
                    ? 'bg-emerald-600 text-white border-emerald-400'
                    : 'bg-zinc-900 text-zinc-400 border-zinc-800 hover:text-zinc-200'
                }`}
              >
                ✏️ RENAME &amp; 3D PINS ({snap.zones.length})
              </button>
              <button
                onClick={() => {
                  setModalTab('hub');
                  setCfgMsg(null);
                }}
                className={`flex-1 py-1.5 rounded font-bold text-[11px] border cursor-pointer ${
                  modalTab === 'hub'
                    ? 'bg-emerald-600 text-white border-emerald-400'
                    : 'bg-zinc-900 text-zinc-400 border-zinc-800 hover:text-zinc-200'
                }`}
              >
                📡 YOLINK HUB &amp; SCHEDULE
              </button>
            </div>

            {modalTab === 'zones' ? (
              <div className="space-y-2.5">
                <p className="text-[10px] text-zinc-400">
                  Rename any sensor and optionally assign its exact door/window pin on your mirrored{' '}
                  <span className="text-cyan-300 font-bold">PLAN 1622</span> 3D wireframe.
                </p>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 max-h-[52vh] overflow-y-auto pr-1">
                  {snap.zones.map((z) => {
                    const baseName = z.raw_name || z.name;
                    const currentVal = zoneAliases[z.entity_id] ?? '';
                    const currentPin = zonePins[z.entity_id] ?? '';
                    const isCfgHighlighted = highlightConfigZoneId === z.entity_id;
                    return (
                      <div
                        key={z.entity_id}
                        className={`rounded p-2 space-y-1.5 border ${
                          isCfgHighlighted
                            ? 'bg-emerald-950/35 border-emerald-400 ring-1 ring-emerald-400/60'
                            : 'bg-zinc-900/80 border-zinc-800'
                        }`}
                      >
                        <div className="flex items-center justify-between text-[9px] text-zinc-500">
                          <span className="truncate">Sensor: {baseName}</span>
                          {(currentVal || currentPin) && (
                            <button
                              onClick={() => {
                                setZoneAliases((prev) => {
                                  const next = { ...prev };
                                  delete next[z.entity_id];
                                  return next;
                                });
                                setZonePins((prev) => {
                                  const next = { ...prev };
                                  delete next[z.entity_id];
                                  return next;
                                });
                              }}
                              title="Reset name and 3D pin to auto"
                              className="text-amber-400 hover:text-amber-300 flex items-center gap-0.5 cursor-pointer"
                            >
                              <RotateCcw className="w-2.5 h-2.5" />
                              Reset
                            </button>
                          )}
                        </div>
                        <input
                          type="text"
                          value={currentVal}
                          onChange={(e) =>
                            setZoneAliases((prev) => ({
                              ...prev,
                              [z.entity_id]: e.target.value,
                            }))
                          }
                          placeholder={baseName}
                          className="w-full bg-zinc-950 border border-zinc-700 focus:border-emerald-500 rounded px-2 py-1 text-xs text-zinc-100"
                        />
                        <select
                          value={currentPin}
                          onChange={(e) =>
                            setZonePins((prev) => ({
                              ...prev,
                              [z.entity_id]: e.target.value,
                            }))
                          }
                          className="w-full bg-zinc-950 border border-zinc-800 rounded px-1.5 py-1 text-[10px] text-cyan-300"
                        >
                          <option value="">📍 Auto-match 3D Pin by Name</option>
                          {HOUSE_PIN_ANCHORS.map((anc) => (
                            <option key={anc.id} value={anc.id}>
                              {anc.label}
                            </option>
                          ))}
                        </select>
                      </div>
                    );
                  })}
                </div>
              </div>
            ) : (
              <div className="space-y-2.5">
                <div>
                  <label className="text-[10px] text-zinc-400 block mb-1">TRANSPORT MODE:</label>
                  <select
                    value={cfgMode}
                    onChange={(e) => setCfgMode(e.target.value as 'auto' | 'cloud' | 'local' | 'mock')}
                    className="w-full bg-zinc-900 border border-zinc-700 rounded px-2 py-1.5 text-zinc-100"
                  >
                    <option value="auto">AUTO (Use Local Hub if set, else YS1603 Cloud, else Mock)</option>
                    <option value="cloud">YS1603-UC Hub — Direct Outbound MQTT + v2 API (Zero HA)</option>
                    <option value="local">YS1606 Local Hub — 100% Air-Gapped LAN (:1080 / :18080)</option>
                    <option value="mock">MOCK SIMULATOR (12 Simulated Zones)</option>
                  </select>
                </div>

                {(cfgMode === 'auto' || cfgMode === 'cloud') && (
                  <div className="rounded-lg bg-zinc-900/70 border border-zinc-800 p-2.5 space-y-2">
                    <div className="text-[10px] text-sky-300 font-bold">
                      YS1603-UC CREDENTIALS (YoLink App → Profile → Account → Advanced → User Access Credentials)
                    </div>
                    <div>
                      <label className="text-[10px] text-zinc-400 block mb-0.5">UAID:</label>
                      <input
                        type="text"
                        value={uaid}
                        onChange={(e) => setUaid(e.target.value)}
                        placeholder="ua_..."
                        className="w-full bg-zinc-950 border border-zinc-700 rounded px-2 py-1 text-zinc-100"
                      />
                    </div>
                    <div>
                      <label className="text-[10px] text-zinc-400 block mb-0.5">
                        SECRET KEY {hasSavedSecret ? '(saved — leave blank to keep)' : ''}:
                      </label>
                      <input
                        type="password"
                        value={secretKey}
                        onChange={(e) => setSecretKey(e.target.value)}
                        placeholder="sec_v1_..."
                        className="w-full bg-zinc-950 border border-zinc-700 rounded px-2 py-1 text-zinc-100"
                      />
                    </div>
                  </div>
                )}

                {cfgMode === 'local' && (
                  <div className="rounded-lg bg-zinc-900/70 border border-zinc-800 p-2.5 space-y-2">
                    <div className="text-[10px] text-emerald-300 font-bold">
                      YS1606 LOCAL HUB LAN API (Port 1080 HTTP / Port 18080 MQTT)
                    </div>
                    <div className="grid grid-cols-2 gap-2">
                      <input
                        type="text"
                        value={localHubIp}
                        onChange={(e) => setLocalHubIp(e.target.value)}
                        placeholder="Hub IP (e.g. 192.168.1.50)"
                        className="bg-zinc-950 border border-zinc-700 rounded px-2 py-1 text-zinc-100"
                      />
                      <input
                        type="text"
                        value={localSubnetId}
                        onChange={(e) => setLocalSubnetId(e.target.value)}
                        placeholder="Sub-Net ID"
                        className="bg-zinc-950 border border-zinc-700 rounded px-2 py-1 text-zinc-100"
                      />
                    </div>
                    <div className="grid grid-cols-2 gap-2">
                      <input
                        type="text"
                        value={localClientId}
                        onChange={(e) => setLocalClientId(e.target.value)}
                        placeholder="Local ClientId"
                        className="bg-zinc-950 border border-zinc-700 rounded px-2 py-1 text-zinc-100"
                      />
                      <input
                        type="password"
                        value={localClientSecret}
                        onChange={(e) => setLocalClientSecret(e.target.value)}
                        placeholder="Local ClientSecret"
                        className="bg-zinc-950 border border-zinc-700 rounded px-2 py-1 text-zinc-100"
                      />
                    </div>
                  </div>
                )}

                <div className="flex items-center justify-between pt-1">
                  <label className="flex items-center gap-2 cursor-pointer text-[11px] text-zinc-200">
                    <input
                      type="checkbox"
                      checked={autoDisarmEnabled}
                      onChange={(e) => setAutoDisarmEnabled(e.target.checked)}
                    />
                    <span>Daily Night Auto-Disarm (PT — never disarms Away Mode):</span>
                  </label>
                  <input
                    type="time"
                    value={autoDisarmTime}
                    onChange={(e) => setAutoDisarmTime(e.target.value)}
                    className="bg-zinc-900 border border-zinc-700 rounded px-2 py-0.5 text-zinc-100"
                  />
                </div>

                <div>
                  <label className="text-[10px] text-zinc-400 block mb-0.5">
                    MOBILE PUSH APP LINK URL (LAN or Tailscale URL sent in breach alerts):
                  </label>
                  <input
                    type="text"
                    value={commandUiUrl}
                    onChange={(e) => setCommandUiUrl(e.target.value)}
                    placeholder={`http://${hostIp}:3000`}
                    className="w-full bg-zinc-950 border border-zinc-700 rounded px-2 py-1 text-zinc-100"
                  />
                </div>
              </div>
            )}

            {cfgMsg && <div className="text-[11px] text-amber-300 font-bold">{cfgMsg}</div>}

            <div className="flex justify-end gap-2 pt-2 border-t border-zinc-800">
              <button
                onClick={() => setShowConfig(false)}
                className="px-3 py-1.5 rounded bg-zinc-800 hover:bg-zinc-700 text-zinc-300 font-bold cursor-pointer"
              >
                Close
              </button>
              <button
                onClick={saveHubConfig}
                className="px-3 py-1.5 rounded bg-emerald-600 hover:bg-emerald-500 text-white font-bold flex items-center gap-1.5 cursor-pointer"
              >
                <Check className="w-3.5 h-3.5" />
                Save
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
