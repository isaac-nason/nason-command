import { DeliveryRulesPanel } from './DeliveryRulesPanel';
import { ResidentVehiclesPanel } from './ResidentVehiclesPanel';
import React, { useEffect, useMemo, useState } from 'react';
import type { KnownFaceItem } from '../types/telemetry';
import {
  AlertTriangle,
  Check,
  ChevronLeft,
  ChevronRight,
  Clock,
  Download,
  Edit3,
  ExternalLink,
  Globe,
  Layers,
  Lock,
  Plus,
  RefreshCw,
  ShieldCheck,
  Trash2,
  Upload,
  UserCheck,
  Users,
  X,
} from 'lucide-react';

interface FaceGalleryProps {
  hostIp: string;
}

interface GroupedFaceSubject {
  groupKey: string;
  name: string;
  role: 'whitelist' | 'visitor' | 'watchlist';
  vectors: KnownFaceItem[];
  totalSightings: number;
  latestSeen: string | null;
}

const PAGE_SIZE = 12;

const ROLE_STYLES: Record<string, { label: string; badge: string; border: string }> = {
  whitelist: {
    label: 'WHITELIST (TRUSTED)',
    badge: 'bg-emerald-950 text-emerald-300 border-emerald-500/50',
    border: 'border-emerald-500/40',
  },
  visitor: {
    label: 'REPEAT VISITOR',
    badge: 'bg-amber-950 text-amber-300 border-amber-500/50',
    border: 'border-amber-500/40',
  },
  watchlist: {
    label: '🚨 WATCHLIST ALERT',
    badge: 'bg-red-950 text-red-300 border-red-500/70',
    border: 'border-red-500/70 shadow-[0_0_15px_rgba(239,68,68,0.2)]',
  },
};

const ROLE_SORT_ORDER: Record<string, number> = {
  whitelist: 0,
  watchlist: 1,
  visitor: 2,
};

export function FaceGallery({ hostIp }: FaceGalleryProps) {
  const apiBase = `http://${hostIp}:8080`;
  const [faces, setFaces] = useState<KnownFaceItem[]>([]);
  const [autoWatchlistCount, setAutoWatchlistCount] = useState<number>(0);
  const [showBgWatchlist, setShowBgWatchlist] = useState<boolean>(false);
  const [syncingWatchlist, setSyncingWatchlist] = useState<boolean>(false);
  const [syncMessage, setSyncMessage] = useState<string | null>(null);
  const [filterRole, setFilterRole] = useState<'all' | 'whitelist' | 'visitor' | 'watchlist'>('all');
  const [currentPage, setCurrentPage] = useState<number>(1);
  const [selectedVecByGroup, setSelectedVecByGroup] = useState<Record<string, number>>({});
  const [confirmDeleteId, setConfirmDeleteId] = useState<number | null>(null);

  const [editingId, setEditingId] = useState<number | null>(null);
  const [editName, setEditName] = useState<string>('');
  const [editRole, setEditRole] = useState<'whitelist' | 'visitor' | 'watchlist'>('whitelist');

  const [showImportDrawer, setShowImportDrawer] = useState<boolean>(false);
  const [importName, setImportName] = useState<string>('');
  const [importRole, setImportRole] = useState<'whitelist' | 'watchlist'>('watchlist');
  const [importUrl, setImportUrl] = useState<string>('');
  const [importStatus, setImportStatus] = useState<string | null>(null);

  const fetchFaces = async () => {
    try {
      const [res, statusRes] = await Promise.all([
        fetch(`${apiBase}/api/faces?include_auto=${showBgWatchlist}`),
        fetch(`${apiBase}/api/faces/watchlist_status`),
      ]);
      if (res.ok) setFaces(await res.json());
      if (statusRes.ok) {
        const st = await statusRes.json();
        setAutoWatchlistCount(st.auto_watchlist_vectors || 0);
      }
    } catch (err) {
      console.error('Failed to fetch face gallery:', err);
    }
  };

  useEffect(() => {
    fetchFaces();
    const timer = window.setInterval(fetchFaces, 5000);
    return () => window.clearInterval(timer);
  }, [apiBase, showBgWatchlist]);

  useEffect(() => {
    setCurrentPage(1);
  }, [filterRole, showBgWatchlist]);

  const handleSyncWatchlistNow = async () => {
    setSyncingWatchlist(true);
    setSyncMessage('Paginating FBI & Oregon DOC feeds...');
    try {
      const res = await fetch(`${apiBase}/api/faces/sync_watchlist`, { method: 'POST' });
      if (res.ok) {
        const data = await res.json();
        setSyncMessage(`+${data.added} new fugitive vectors indexed (${data.total_auto} total in RAM)`);
      }
      await fetchFaces();
    } finally {
      setSyncingWatchlist(false);
    }
  };

  const handleSaveEdit = async (id: number) => {
    if (!editName.trim()) return;
    try {
      const res = await fetch(`${apiBase}/api/faces/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: editName.trim(), role: editRole }),
      });
      if (res.ok) {
        setFaces(await res.json());
        setEditingId(null);
      }
    } catch (err) {
      console.error('Failed to update face:', err);
    }
  };

  const handleDeleteFace = async (id: number) => {
    try {
      const res = await fetch(`${apiBase}/api/faces/${id}`, { method: 'DELETE' });
      if (res.ok) {
        setFaces(await res.json());
        setConfirmDeleteId(null);
      }
    } catch (err) {
      console.error('Failed to delete face vector:', err);
    }
  };

  const handleFileUpload = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    if (!importName.trim()) {
      setImportStatus('Enter a Name/Label first before choosing a photo.');
      return;
    }
    setImportStatus('Extracting 128-D SFace vector from photo...');
    const reader = new FileReader();
    reader.onload = async () => {
      try {
        const res = await fetch(`${apiBase}/api/faces/enroll_image`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            name: importName.trim(),
            role: importRole,
            image_base64: String(reader.result),
          }),
        });
        const data = await res.json();
        if (res.ok) {
          setFaces(data);
          setImportStatus(`Enrolled ${importName.toUpperCase()} (${importRole.toUpperCase()})!`);
        } else {
          setImportStatus(data.detail || 'No clear face found in photo.');
        }
      } catch {
        setImportStatus('Upload failed.');
      }
    };
    reader.readAsDataURL(file);
  };

  const handleUrlScrape = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!importName.trim() || !importUrl.trim()) return;
    setImportStatus('Fetching URL & extracting YuNet/SFace biometric vector...');
    try {
      const res = await fetch(`${apiBase}/api/faces/enroll_url`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: importName.trim(),
          role: importRole,
          image_url: importUrl.trim(),
        }),
      });
      const data = await res.json();
      if (res.ok) {
        setFaces(data);
        setImportStatus(`Scraped & enrolled ${importName.toUpperCase()}!`);
        setImportUrl('');
        setImportName('');
      } else {
        setImportStatus(data.detail || 'Could not extract face from URL.');
      }
    } catch {
      setImportStatus('Failed to scrape image URL.');
    }
  };

  const launchOsintLookup = (cropUrl: string | null, engine: 'pimeyes' | 'yandex') => {
    if (cropUrl) {
      const a = document.createElement('a');
      a.href = `${apiBase}${cropUrl}`;
      a.download = `osint_face_crop.jpg`;
      a.target = '_blank';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
    }
    const urls = {
      pimeyes: 'https://pimeyes.com/en',
      yandex: 'https://yandex.com/images/',
    };
    window.open(urls[engine], '_blank', 'noopener,noreferrer');
  };

  const allGroups = useMemo<GroupedFaceSubject[]>(() => {
    const map = new Map<string, GroupedFaceSubject>();
    for (const f of faces) {
      const cleanName = (f.name || 'UNKNOWN').trim().toUpperCase();
      const gKey = `${f.role}::${cleanName}`;
      const existing = map.get(gKey);
      if (!existing) {
        map.set(gKey, {
          groupKey: gKey,
          name: cleanName,
          role: f.role,
          vectors: [f],
          totalSightings: f.sightings || 0,
          latestSeen: f.last_seen,
        });
      } else {
        existing.vectors.push(f);
        existing.totalSightings += f.sightings || 0;
        if (f.last_seen && (!existing.latestSeen || f.last_seen > existing.latestSeen)) {
          existing.latestSeen = f.last_seen;
        }
      }
    }

    const list = Array.from(map.values());
    for (const grp of list) {
      grp.vectors.sort((a, b) => {
        const aUpload = (a.sample_crop || '').includes('upload_') ? 0 : 1;
        const bUpload = (b.sample_crop || '').includes('upload_') ? 0 : 1;
        if (aUpload !== bUpload) return aUpload - bUpload;
        return b.id - a.id;
      });
    }

    list.sort((a, b) => {
      const ra = ROLE_SORT_ORDER[a.role] ?? 9;
      const rb = ROLE_SORT_ORDER[b.role] ?? 9;
      if (ra !== rb) return ra - rb;
      if (a.role === 'whitelist' && b.vectors.length !== a.vectors.length) {
        return b.vectors.length - a.vectors.length;
      }
      return (b.latestSeen || '').localeCompare(a.latestSeen || '');
    });
    return list;
  }, [faces]);

  const filteredGroups = allGroups.filter((g) => (filterRole === 'all' ? true : g.role === filterRole));
  const totalPages = Math.max(1, Math.ceil(filteredGroups.length / PAGE_SIZE));
  const safePage = Math.min(currentPage, totalPages);
  const pagedGroups = filteredGroups.slice((safePage - 1) * PAGE_SIZE, safePage * PAGE_SIZE);

  const wlGroups = allGroups.filter((g) => g.role === 'whitelist');
  const wlVecCount = faces.filter((f) => f.role === 'whitelist').length;
  const visGroups = allGroups.filter((g) => g.role === 'visitor');
  const watchGroups = allGroups.filter((g) => g.role === 'watchlist');

  return (
    <div className="flex-1 flex flex-col gap-4 font-mono">
      <div className="bg-zinc-900/80 border border-zinc-800 rounded-lg p-3 sm:p-4 flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2.5">
          <Users className="w-5 h-5 text-emerald-400" />
          <div>
            <h2 className="text-xs sm:text-sm font-bold text-zinc-100 uppercase">
              YUNET + SFACE BIOMETRIC GALLERY ({allGroups.length} SUBJECTS · {faces.length} VECTORS · {autoWatchlistCount} BG WATCHLIST)
            </h2>
            <p className="text-[10px] text-zinc-400">
              {syncMessage || 'Multi-angle vectors are grouped per person — enroll daylight, side-profile, and night angles for ~100% recall'}
            </p>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <button
            onClick={handleSyncWatchlistNow}
            disabled={syncingWatchlist}
            className="bg-zinc-950 hover:bg-zinc-800 border border-zinc-700 text-amber-300 px-2.5 py-1.5 rounded text-xs font-bold flex items-center gap-1.5 cursor-pointer"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${syncingWatchlist ? 'animate-spin' : ''}`} />
            {syncingWatchlist ? 'SCRAPING NEXT PAGES...' : `SYNC NEXT FBI PAGES (${autoWatchlistCount})`}
          </button>

          <button
            onClick={() => {
              setShowImportDrawer((prev) => !prev);
              setImportStatus(null);
            }}
            className="bg-emerald-600 hover:bg-emerald-500 text-white px-3 py-1.5 rounded text-xs font-bold flex items-center gap-1.5 cursor-pointer"
          >
            <Plus className="w-4 h-4" />
            + ENROLL PHOTO / URL
          </button>
        </div>
      </div>

      {showImportDrawer && (
        <div className="bg-zinc-950 border border-emerald-500/50 rounded-lg p-4 space-y-3">
          <div className="flex items-center justify-between border-b border-zinc-800 pb-2">
            <span className="text-xs font-bold text-emerald-400 uppercase">
              IMPORT FACE ANGLE TO WHITELIST OR LOCAL WATCHLIST
            </span>
            <button onClick={() => setShowImportDrawer(false)} className="text-zinc-400 hover:text-white cursor-pointer">
              <X className="w-4 h-4" />
            </button>
          </div>

          <div className="grid items-start grid-cols-1 md:grid-cols-3 gap-3">
            <div>
              <label className="text-[10px] text-zinc-400 block mb-1">1. SUBJECT NAME / LABEL:</label>
              <input
                type="text"
                value={importName}
                onChange={(e) => setImportName(e.target.value)}
                placeholder="e.g. ISAAC, JENNIFER, BEAU, LYLA..."
                className="w-full bg-zinc-900 border border-zinc-700 rounded px-2.5 py-1.5 text-xs text-zinc-100 uppercase"
              />
            </div>

            <div>
              <label className="text-[10px] text-zinc-400 block mb-1">2. SECURITY ROLE:</label>
              <div className="grid items-start grid-cols-2 gap-1.5">
                <button
                  type="button"
                  onClick={() => setImportRole('whitelist')}
                  className={`py-1.5 rounded text-[10px] font-bold border flex items-center justify-center gap-1 cursor-pointer ${
                    importRole === 'whitelist'
                      ? 'bg-emerald-600 text-white border-emerald-400'
                      : 'bg-zinc-900 text-zinc-400 border-zinc-800'
                  }`}
                >
                  <ShieldCheck className="w-3.5 h-3.5" />
                  WHITELIST
                </button>
                <button
                  type="button"
                  onClick={() => setImportRole('watchlist')}
                  className={`py-1.5 rounded text-[10px] font-bold border flex items-center justify-center gap-1 cursor-pointer ${
                    importRole === 'watchlist'
                      ? 'bg-red-600 text-white border-red-400'
                      : 'bg-zinc-900 text-zinc-400 border-zinc-800'
                  }`}
                >
                  <AlertTriangle className="w-3.5 h-3.5" />
                  WATCHLIST
                </button>
              </div>
            </div>

            <div>
              <label className="text-[10px] text-zinc-400 block mb-1">3A. UPLOAD PHOTO (PC / PHONE):</label>
              <label className="w-full bg-sky-600 hover:bg-sky-500 text-white py-1.5 px-3 rounded text-xs font-bold flex items-center justify-center gap-1.5 cursor-pointer">
                <Upload className="w-3.5 h-3.5" />
                CHOOSE PHOTO FILE
                <input type="file" accept="image/*" onChange={handleFileUpload} className="hidden" />
              </label>
            </div>
          </div>

          <form onSubmit={handleUrlScrape} className="flex flex-wrap items-center gap-2 pt-2 border-t border-zinc-800">
            <Globe className="w-4 h-4 text-amber-400 shrink-0" />
            <input
              type="url"
              value={importUrl}
              onChange={(e) => setImportUrl(e.target.value)}
              placeholder="3B. Or paste public mugshot / offender / photo URL (https://...jpg) to scrape into database..."
              className="flex-1 min-w-[220px] bg-zinc-900 border border-zinc-700 rounded px-2.5 py-1.5 text-xs text-zinc-100"
            />
            <button
              type="submit"
              className="bg-amber-600 hover:bg-amber-500 text-white px-3 py-1.5 rounded text-xs font-bold cursor-pointer"
            >
              SCRAPE URL
            </button>
          </form>

          {importStatus && <div className="text-xs text-amber-300 font-bold">{importStatus}</div>}
        </div>
      )}

      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          {[
            { key: 'all', label: `ALL (${allGroups.length})` },
            { key: 'whitelist', label: `🛡️ WHITELIST (${wlGroups.length} · ${wlVecCount} ANGLES)` },
            { key: 'visitor', label: `👤 VISITORS (${visGroups.length})` },
            { key: 'watchlist', label: `🚨 WATCHLIST (${watchGroups.length})` },
          ].map((tab) => (
            <button
              key={tab.key}
              onClick={() => setFilterRole(tab.key as 'all' | 'whitelist' | 'visitor' | 'watchlist')}
              className={`px-3 py-1.5 rounded text-xs font-bold border cursor-pointer transition-colors ${
                filterRole === tab.key
                  ? 'bg-emerald-600 text-white border-emerald-400'
                  : 'bg-zinc-900 hover:bg-zinc-800 text-zinc-300 border-zinc-800'
              }`}
            >
              {tab.label}
            </button>
          ))}
        </div>

        <div className="flex items-center gap-2">
          {totalPages > 1 && (
            <div className="flex items-center gap-1 bg-zinc-900 border border-zinc-800 rounded px-2 py-1 text-xs">
              <button
                onClick={() => setCurrentPage((p) => Math.max(1, p - 1))}
                disabled={safePage <= 1}
                className="disabled:opacity-30 text-zinc-200 hover:text-emerald-400 cursor-pointer"
              >
                <ChevronLeft className="w-4 h-4" />
              </button>
              <span className="px-2 text-[11px] font-bold text-zinc-300">
                PAGE {safePage} / {totalPages}
              </span>
              <button
                onClick={() => setCurrentPage((p) => Math.min(totalPages, p + 1))}
                disabled={safePage >= totalPages}
                className="disabled:opacity-30 text-zinc-200 hover:text-emerald-400 cursor-pointer"
              >
                <ChevronRight className="w-4 h-4" />
              </button>
            </div>
          )}

          <button
            onClick={() => setShowBgWatchlist((prev) => !prev)}
            className={`px-2.5 py-1.5 rounded text-[11px] font-bold border cursor-pointer ${
              showBgWatchlist
                ? 'bg-red-950 text-red-300 border-red-500/50'
                : 'bg-zinc-900 text-zinc-400 border-zinc-800'
            }`}
          >
            {showBgWatchlist
              ? `HIDE BG FBI/DOC (${autoWatchlistCount})`
              : `SHOW BG FBI/DOC (${autoWatchlistCount} HIDDEN)`}
          </button>
        </div>
      </div>

      {pagedGroups.length === 0 ? (
        <div className="grid items-start grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
          <ResidentVehiclesPanel apiBase={apiBase} />
          <DeliveryRulesPanel apiBase={apiBase} />
          <div className="sm:col-span-2 lg:col-span-3 xl:col-span-4 bg-zinc-900/40 border border-zinc-800 rounded-lg py-16 px-4 text-center text-xs text-zinc-500">
            No enrolled family or visitor face vectors in this filter yet. Use 👤 ID FACE or 🎓 TEACH in the Kill-Feed (or + ENROLL PHOTO above) to enroll family portraits. ({autoWatchlistCount} background FBI/DOC vectors are armed in RAM).
          </div>
        </div>
      ) : (
        <div className="grid items-start grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
          <ResidentVehiclesPanel apiBase={apiBase} />
          <DeliveryRulesPanel apiBase={apiBase} />

          {pagedGroups.map((grp) => {
            const style = ROLE_STYLES[grp.role] || ROLE_STYLES.whitelist;
            const isWhitelisted = grp.role === 'whitelist';
            const selId = selectedVecByGroup[grp.groupKey];
            const activeFace = grp.vectors.find((v) => v.id === selId) || grp.vectors[0];
            const angleCount = grp.vectors.length;

            const lastSeenStr = grp.latestSeen
              ? new Date(grp.latestSeen).toLocaleString([], {
                  month: 'short',
                  day: 'numeric',
                  hour: '2-digit',
                  minute: '2-digit',
                })
              : 'Never';

            return (
              <div
                key={grp.groupKey}
                className={`bg-zinc-950 border rounded-lg p-3 flex flex-col justify-between gap-3 ${style.border}`}
              >
                <div className="flex gap-3 items-start">
                  <div className="w-20 h-20 rounded-md overflow-hidden bg-zinc-900 border border-zinc-800 shrink-0 flex items-center justify-center relative">
                    {activeFace.sample_crop ? (
                      <img
                        src={`${apiBase}${activeFace.sample_crop}`}
                        alt={grp.name}
                        className="w-full h-full object-cover"
                      />
                    ) : (
                      <UserCheck className="w-7 h-7 text-zinc-700" />
                    )}
                    <span className="absolute bottom-0.5 right-0.5 bg-black/80 text-zinc-200 text-[8px] px-1 rounded">
                      #{activeFace.id}
                    </span>
                  </div>

                  <div className="flex-1 min-w-0">
                    <div className="flex items-center justify-between gap-1 flex-wrap">
                      <span className={`text-[9px] font-bold px-1.5 py-0.5 rounded border ${style.badge}`}>
                        {style.label}
                      </span>
                      {isWhitelisted && (
                        <span
                          className={`text-[9px] font-bold px-1.5 py-0.5 rounded border flex items-center gap-0.5 ${
                            angleCount >= 3
                              ? 'bg-emerald-950/90 text-emerald-300 border-emerald-500/40'
                              : angleCount === 2
                              ? 'bg-sky-950/90 text-sky-300 border-sky-500/40'
                              : 'bg-amber-950/90 text-amber-300 border-amber-500/40'
                          }`}
                          title="Enrolled 128-D angle vectors for this person"
                        >
                          <Layers className="w-2.5 h-2.5" />
                          {angleCount} {angleCount === 1 ? 'ANGLE' : 'ANGLES'}
                        </span>
                      )}
                    </div>

                    <div className="text-sm font-bold text-zinc-100 truncate mt-1.5">{grp.name}</div>

                    <div className="text-[11px] text-emerald-400 font-semibold mt-0.5">
                      Seen {grp.totalSightings} {grp.totalSightings === 1 ? 'time' : 'times'}
                    </div>
                    <div className="text-[10px] text-zinc-500 flex items-center gap-1 mt-0.5">
                      <Clock className="w-3 h-3" />
                      {lastSeenStr}
                    </div>
                  </div>
                </div>

                {/* Multi-Angle Thumbnail Selector Strip */}
                {(angleCount > 1 || isWhitelisted) && (
                  <div className="pt-2 border-t border-zinc-900 space-y-1.5">
                    <div className="flex items-center justify-between text-[9px] text-zinc-400">
                      <span>
                        {angleCount === 1
                          ? '1 vector enrolled — add side/night angle:'
                          : `Select angle (${angleCount} enrolled vectors):`}
                      </span>
                      {(isWhitelisted || grp.role === 'watchlist') && (
                        <button
                          onClick={() => {
                            setImportName(grp.name);
                            setImportRole(grp.role === 'watchlist' ? 'watchlist' : 'whitelist');
                            setShowImportDrawer(true);
                            setImportStatus(`Ready to upload a new angle for ${grp.name}`);
                            window.scrollTo({ top: 0, behavior: 'smooth' });
                          }}
                          className="text-emerald-300 hover:text-emerald-200 font-bold flex items-center gap-0.5 cursor-pointer"
                          title={`Upload another photo angle for ${grp.name}`}
                        >
                          <Plus className="w-2.5 h-2.5" />
                          ADD ANGLE
                        </button>
                      )}
                    </div>
                    <div className="flex items-center gap-1.5 overflow-x-auto pb-0.5">
                      {grp.vectors.map((vec) => {
                        const isSelected = vec.id === activeFace.id;
                        const isUpload = (vec.sample_crop || '').includes('upload_');
                        return (
                          <button
                            key={vec.id}
                            type="button"
                            onClick={() => {
                              setSelectedVecByGroup((prev) => ({ ...prev, [grp.groupKey]: vec.id }));
                              setConfirmDeleteId(null);
                            }}
                            title={`Vector #${vec.id} (${isUpload ? 'Uploaded Reference' : 'Camera Capture'} · Seen ${vec.sightings}x)`}
                            className={`relative w-10 h-10 rounded overflow-hidden shrink-0 border transition-all cursor-pointer ${
                              isSelected
                                ? 'border-emerald-400 ring-1 ring-emerald-400 scale-105'
                                : 'border-zinc-800 opacity-70 hover:opacity-100'
                            }`}
                          >
                            {vec.sample_crop ? (
                              <img
                                src={`${apiBase}${vec.sample_crop}`}
                                alt={`#${vec.id}`}
                                className="w-full h-full object-cover"
                              />
                            ) : (
                              <UserCheck className="w-4 h-4 text-zinc-600 m-auto" />
                            )}
                            <span
                              className={`absolute bottom-0 inset-x-0 text-[7px] font-bold text-center leading-tight ${
                                isUpload ? 'bg-sky-950/90 text-sky-200' : 'bg-zinc-950/90 text-amber-200'
                              }`}
                            >
                              {isUpload ? 'REF' : 'CAM'}
                            </span>
                          </button>
                        );
                      })}
                    </div>
                  </div>
                )}

                {editingId === activeFace.id ? (
                  <div className="bg-zinc-900 border border-zinc-700 rounded p-2 space-y-2">
                    <div className="text-[9px] text-zinc-400">Editing vector #{activeFace.id}:</div>
                    <input
                      type="text"
                      value={editName}
                      onChange={(e) => setEditName(e.target.value)}
                      className="w-full bg-zinc-950 border border-zinc-700 rounded px-2 py-1 text-xs text-zinc-100 uppercase"
                    />
                    <div className="grid items-start grid-cols-3 gap-1">
                      {(['whitelist', 'visitor', 'watchlist'] as const).map((r) => (
                        <button
                          key={r}
                          type="button"
                          onClick={() => setEditRole(r)}
                          className={`py-1 rounded text-[9px] font-bold uppercase border cursor-pointer ${
                            editRole === r
                              ? 'bg-sky-600 text-white border-sky-400'
                              : 'bg-zinc-950 text-zinc-400 border-zinc-800'
                          }`}
                        >
                          {r}
                        </button>
                      ))}
                    </div>
                    <div className="flex justify-end gap-1">
                      <button
                        onClick={() => setEditingId(null)}
                        className="px-2 py-1 rounded text-[10px] bg-zinc-800 text-zinc-300 cursor-pointer"
                      >
                        CANCEL
                      </button>
                      <button
                        onClick={() => handleSaveEdit(activeFace.id)}
                        className="px-2.5 py-1 rounded text-[10px] font-bold bg-emerald-600 text-white flex items-center gap-1 cursor-pointer"
                      >
                        <Check className="w-3 h-3" />
                        SAVE
                      </button>
                    </div>
                  </div>
                ) : (
                  <div className="space-y-2 pt-2 border-t border-zinc-900">
                    <div className="flex items-center justify-between gap-1">
                      {activeFace.sample_crop && (
                        <a
                          href={`${apiBase}${activeFace.sample_crop}`}
                          download={`face_${grp.name}_${activeFace.id}.jpg`}
                          title={`Download crop #${activeFace.id} locally`}
                          className="bg-zinc-900 hover:bg-zinc-800 border border-zinc-800 px-2 py-1 rounded text-[10px] text-zinc-300 flex items-center gap-1"
                        >
                          <Download className="w-3 h-3 text-emerald-400" />
                          CROP
                        </a>
                      )}
                      <button
                        disabled={isWhitelisted}
                        onClick={() => !isWhitelisted && launchOsintLookup(activeFace.sample_crop, 'pimeyes')}
                        title={
                          isWhitelisted
                            ? 'OpSec Lock: External OSINT disabled for Whitelisted family members'
                            : 'Download crop & launch PimEyes Face Search'
                        }
                        className="flex-1 bg-zinc-900 hover:bg-zinc-800 disabled:bg-zinc-950 disabled:opacity-35 disabled:text-zinc-600 disabled:cursor-not-allowed border border-zinc-800 py-1 rounded text-[10px] text-sky-300 flex items-center justify-center gap-1 cursor-pointer"
                      >
                        {isWhitelisted ? <Lock className="w-2.5 h-2.5" /> : null}
                        PIMEYES
                        {!isWhitelisted && <ExternalLink className="w-2.5 h-2.5" />}
                      </button>
                      <button
                        disabled={isWhitelisted}
                        onClick={() => !isWhitelisted && launchOsintLookup(activeFace.sample_crop, 'yandex')}
                        title={
                          isWhitelisted
                            ? 'OpSec Lock: External OSINT disabled for Whitelisted family members'
                            : 'Download crop & launch Yandex Reverse Face Search'
                        }
                        className="flex-1 bg-zinc-900 hover:bg-zinc-800 disabled:bg-zinc-950 disabled:opacity-35 disabled:text-zinc-600 disabled:cursor-not-allowed border border-zinc-800 py-1 rounded text-[10px] text-amber-300 flex items-center justify-center gap-1 cursor-pointer"
                      >
                        {isWhitelisted ? <Lock className="w-2.5 h-2.5" /> : null}
                        YANDEX
                        {!isWhitelisted && <ExternalLink className="w-2.5 h-2.5" />}
                      </button>
                    </div>

                    <div className="flex items-center justify-between gap-1.5">
                      <button
                        onClick={() => {
                          setEditingId(activeFace.id);
                          setEditName(activeFace.name);
                          setEditRole(activeFace.role);
                        }}
                        className="flex-1 bg-zinc-900 hover:bg-zinc-800 border border-zinc-700 py-1 rounded text-[10px] text-zinc-200 flex items-center justify-center gap-1 cursor-pointer"
                      >
                        <Edit3 className="w-3 h-3" />
                        RENAME #{activeFace.id}
                      </button>

                      {confirmDeleteId === activeFace.id ? (
                        <div className="flex items-center gap-1">
                          <button
                            onClick={() => handleDeleteFace(activeFace.id)}
                            className="bg-red-600 hover:bg-red-500 border border-red-400 px-2 py-1 rounded text-[10px] font-bold text-white cursor-pointer"
                          >
                            DEL #{activeFace.id}?
                          </button>
                          <button
                            onClick={() => setConfirmDeleteId(null)}
                            className="bg-zinc-800 hover:bg-zinc-700 px-1.5 py-1 rounded text-[10px] text-zinc-300 cursor-pointer"
                          >
                            ✕
                          </button>
                        </div>
                      ) : (
                        <button
                          onClick={() => {
                            if (grp.role === 'visitor') {
                              handleDeleteFace(activeFace.id);
                            } else {
                              setConfirmDeleteId(activeFace.id);
                            }
                          }}
                          title={`Delete angle vector #${activeFace.id}`}
                          className="bg-red-950/70 hover:bg-red-900 border border-red-500/40 px-2.5 py-1 rounded text-[10px] text-red-300 flex items-center gap-1 cursor-pointer"
                        >
                          <Trash2 className="w-3 h-3" />
                          DEL #{activeFace.id}
                        </button>
                      )}
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
