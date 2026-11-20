import React, { useEffect, useState } from 'react';
import type { ResidentVehicleItem } from '../types/telemetry';
import { Car, ShieldCheck } from 'lucide-react';

export const ResidentVehiclesPanel: React.FC<{ apiBase?: string }> = ({ apiBase }) => {
  const base = apiBase || `http://${window.location.hostname || '192.168.1.27'}:8080`;
  const [vehicles, setVehicles] = useState<ResidentVehicleItem[]>([]);
  const [expanded, setExpanded] = useState<boolean>(true);
  const [owner, setOwner] = useState('');
  const [vehicleLabel, setVehicleLabel] = useState('');
  const [plate, setPlate] = useState('');
  const [visualCues, setVisualCues] = useState('');
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  const [statusMsg, setStatusMsg] = useState<string | null>(null);

  const loadVehicles = async () => {
    try {
      const r = await fetch(`${base}/api/resident-vehicles`);
      if (r.ok) {
        const d = await r.json();
        setVehicles(d.vehicles || []);
      }
    } catch {
      // ignore transient error
    }
  };

  useEffect(() => {
    loadVehicles();
  }, [base]);

  const addVehicle = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!owner.trim() || !vehicleLabel.trim()) {
      setStatusMsg('Enter Owner and Vehicle Make/Model first.');
      return;
    }
    setStatusMsg('Saving & reconciling driveway LPR events...');
    try {
      const r = await fetch(`${base}/api/resident-vehicles`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          owner: owner.trim().toUpperCase(),
          vehicle_label: vehicleLabel.trim().toUpperCase(),
          plate: plate.trim().toUpperCase(),
          visual_cues: visualCues.trim(),
        }),
      });
      if (r.ok) {
        const d = await r.json();
        setVehicles(d.vehicles || []);
        setOwner('');
        setVehicleLabel('');
        setPlate('');
        setVisualCues('');
        setStatusMsg('Household vehicle whitelisted!');
        window.setTimeout(() => setStatusMsg(null), 2500);
      } else {
        setStatusMsg('Failed to save vehicle.');
      }
    } catch {
      setStatusMsg('Network error saving vehicle.');
    }
  };

  const removeVehicle = async (id: string) => {
    try {
      const r = await fetch(`${base}/api/resident-vehicles/${encodeURIComponent(id)}`, {
        method: 'DELETE',
      });
      if (r.ok) {
        const d = await r.json();
        setVehicles(d.vehicles || []);
        setConfirmDeleteId(null);
      }
    } catch {
      // ignore
    }
  };

  return (
    <div className="col-span-full p-3 rounded-lg border border-emerald-500/35 bg-zinc-950/90 self-start">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2.5">
          <Car className="w-4 h-4 text-emerald-400 shrink-0" />
          <span className="text-xs font-mono font-bold text-emerald-300 uppercase tracking-wider">
            🏠 HOUSEHOLD VEHICLES &amp; LPR WHITELIST ({vehicles.length})
          </span>
          <span className="text-[11px] font-mono text-zinc-400 hidden md:inline">
            Auto-identifies resident plates &amp; shields HOME driveway arrivals from false tripwire alarms
          </span>
        </div>
        <button
          onClick={() => setExpanded((prev) => !prev)}
          className="px-2.5 py-1 rounded text-[10px] font-mono font-bold border border-zinc-700 bg-zinc-900 hover:bg-zinc-800 text-zinc-300 cursor-pointer"
        >
          {expanded ? '▲ COLLAPSE PLATES' : '▼ CONFIGURE PLATES'}
        </button>
      </div>

      {expanded && (
        <>
          {vehicles.length === 0 ? (
            <div className="mt-3 py-4 px-3 rounded border border-zinc-800/80 bg-zinc-900/40 text-xs font-mono text-zinc-500">
              No household vehicles enrolled yet. Add family license plates below to auto-tag driveway arrivals.
            </div>
          ) : (
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-2.5 mt-3 items-start">
              {vehicles.map((veh) => (
                <div
                  key={veh.id}
                  className="p-2.5 rounded border border-emerald-500/35 bg-zinc-900/70 flex flex-col justify-between gap-2 h-full"
                >
                  <div className="flex items-start justify-between gap-1.5">
                    <div className="min-w-0">
                      <div className="flex items-center gap-1.5 flex-wrap">
                        <span className="text-[9px] font-mono font-bold px-1.5 py-0.5 rounded bg-emerald-950 text-emerald-300 border border-emerald-500/50 flex items-center gap-1">
                          <ShieldCheck className="w-2.5 h-2.5" />
                          {veh.owner}
                        </span>
                        {veh.plate ? (
                          <span className="text-[10px] font-mono font-bold px-2 py-0.5 rounded bg-sky-950 text-sky-200 border border-sky-400/60 tracking-wider">
                            🔢 {veh.plate}
                          </span>
                        ) : (
                          <span className="text-[9px] font-mono px-1.5 py-0.5 rounded bg-zinc-800 text-zinc-400">
                            NO PLATE SET
                          </span>
                        )}
                      </div>
                      <div className="font-mono font-bold text-xs text-zinc-100 truncate mt-1.5">
                        {veh.vehicle_label}
                      </div>
                    </div>

                    {confirmDeleteId === veh.id ? (
                      <div className="flex items-center gap-1 shrink-0">
                        <button
                          onClick={() => removeVehicle(veh.id)}
                          className="px-1.5 py-0.5 rounded bg-red-600 hover:bg-red-500 text-white text-[9px] font-mono font-bold cursor-pointer"
                        >
                          DEL?
                        </button>
                        <button
                          onClick={() => setConfirmDeleteId(null)}
                          className="px-1 py-0.5 rounded bg-zinc-800 text-zinc-300 text-[9px] font-mono cursor-pointer"
                        >
                          ✕
                        </button>
                      </div>
                    ) : (
                      <button
                        onClick={() => setConfirmDeleteId(veh.id)}
                        className="text-zinc-500 hover:text-red-400 text-xs font-mono px-1 cursor-pointer shrink-0"
                        title="Remove household vehicle"
                      >
                        ✕
                      </button>
                    )}
                  </div>

                  {veh.visual_cues && (
                    <div className="text-[10px] font-mono text-zinc-400 line-clamp-2 leading-relaxed" title={veh.visual_cues}>
                      {veh.visual_cues}
                    </div>
                  )}

                  {veh.aliases && veh.aliases.length > 0 && (
                    <div className="text-[9px] font-mono text-zinc-500 pt-1 border-t border-zinc-800/60 truncate" title={veh.aliases.join(', ')}>
                      OCR Aliases: {veh.aliases.join(', ')}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}

          <form onSubmit={addVehicle} className="mt-3 pt-2.5 border-t border-zinc-800/80 flex flex-wrap items-center gap-2">
            <input
              type="text"
              value={owner}
              onChange={(e) => setOwner(e.target.value)}
              placeholder="Owner (e.g. ISAAC)"
              className="bg-zinc-900 border border-zinc-800 rounded px-2.5 py-1 text-xs font-mono text-zinc-200 w-36 uppercase"
            />
            <input
              type="text"
              value={vehicleLabel}
              onChange={(e) => setVehicleLabel(e.target.value)}
              placeholder="Vehicle (e.g. RED FORD F-150)"
              className="bg-zinc-900 border border-zinc-800 rounded px-2.5 py-1 text-xs font-mono text-zinc-200 w-48 uppercase"
            />
            <input
              type="text"
              value={plate}
              onChange={(e) => setPlate(e.target.value)}
              placeholder="Plate (e.g. ABC123)"
              className="bg-zinc-900 border border-zinc-800 rounded px-2.5 py-1 text-xs font-mono text-sky-200 w-36 uppercase"
            />
            <input
              type="text"
              value={visualCues}
              onChange={(e) => setVisualCues(e.target.value)}
              placeholder="Visual cues (optional, e.g. crew cab, black grille, roof rack)"
              className="bg-zinc-900 border border-zinc-800 rounded px-2.5 py-1 text-xs font-mono text-zinc-200 flex-1 min-w-[200px]"
            />
            <button
              type="submit"
              className="bg-emerald-600 hover:bg-emerald-500 text-white font-mono font-bold text-xs px-3 py-1 rounded cursor-pointer"
            >
              + WHITELIST VEHICLE
            </button>
          </form>
          {statusMsg && <div className="mt-1.5 text-[11px] font-mono text-emerald-300">{statusMsg}</div>}
        </>
      )}
    </div>
  );
};
