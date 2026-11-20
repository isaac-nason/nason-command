import { useEffect, useMemo, useRef, useState } from 'react';
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import type { PerimeterSnapshot, PerimeterZone } from '../types/telemetry';
import { Camera, Compass, Layers, Maximize2, Minimize2, Pencil, RotateCcw, Volume2, X } from 'lucide-react';

export interface HousePinAnchor {
  id: string;
  label: string;
  floor: 1 | 2;
  x: number;
  z: number;
  normal: [number, number];
  kind: 'door' | 'window';
}

/**
 * Architectural Door, Gate & Window Anchor Points for PLAN 1622 (Mirrored Left <-> Right).
 * Coordinate system (in feet):
 *   +Z = Front (Street / Driveway / Garage)
 *   -Z = Rear (Backyard / 10x10 Patio)
 *   -X = Mirrored Left Side (Entry, Stairs, Loft, Primary Suite, Bdrm 3)
 *   +X = Mirrored Right Side (Side Gate, Kitchen, Dining 1 & 2, Utility, Primary Bath/WIC, Bdrm 2)
 */
export const HOUSE_PIN_ANCHORS: HousePinAnchor[] = [
  { id: 'front_entry', label: '1F · Front Entry Door (Left Porch)', floor: 1, x: -10.0, z: 4.5, normal: [-1.0, 0.35], kind: 'door' },
  { id: 'garage_entry', label: '1F · Door to Garage (Interior Entry)', floor: 1, x: -2.0, z: 9.5, normal: [-0.75, 0.65], kind: 'door' },
  { id: 'garage_overhead', label: '1F · Garage Overhead Roll-Up Door', floor: 1, x: 0.5, z: 29.0, normal: [0.15, 1.0], kind: 'door' },
  { id: 'side_gate', label: '1F · Side Yard Fence Gate (Right)', floor: 1, x: 13.2, z: 9.5, normal: [0.45, 1.0], kind: 'door' },
  { id: 'back_slider', label: '1F · Sliding Glass Door (Patio)', floor: 1, x: 3.2, z: -26.0, normal: [0.3, -1.0], kind: 'door' },
  { id: 'kitchen_win', label: '1F · Kitchen Sink Window (Right)', floor: 1, x: 10.0, z: 3.8, normal: [1.0, 0.25], kind: 'window' },
  { id: 'dining_win', label: '1F · Dining Room Window 1 (Right)', floor: 1, x: 10.0, z: -4.2, normal: [1.0, 0.05], kind: 'window' },
  { id: 'dining_win_2', label: '1F · Dining Room Window 2 (Right)', floor: 1, x: 10.0, z: -9.4, normal: [1.0, -0.1], kind: 'window' },
  { id: 'great_rear_win', label: '1F · Living / Great Room Rear Win', floor: 1, x: -5.2, z: -26.0, normal: [-0.35, -1.0], kind: 'window' },
  { id: 'great_side_win', label: '1F · Living / Great Room Side Win', floor: 1, x: 10.0, z: -18.5, normal: [1.0, -0.25], kind: 'window' },
  { id: 'laundry_door', label: '2F · Utility / Laundry Room (Right)', floor: 2, x: 10.0, z: -3.0, normal: [1.0, 0.15], kind: 'window' },
  { id: 'loft_win', label: '2F · Loft / Office Window (Upper)', floor: 2, x: -10.0, z: -2.5, normal: [-1.0, 0.2], kind: 'window' },
  { id: 'master_win_l', label: '2F · Primary Suite Rear Win L', floor: 2, x: -5.8, z: -26.0, normal: [-0.35, -1.0], kind: 'window' },
  { id: 'master_win_r', label: '2F · Primary Suite Rear Win R', floor: 2, x: -0.4, z: -26.0, normal: [0.15, -1.0], kind: 'window' },
  { id: 'master_bath_win', label: '2F · Primary Bath Window (Right)', floor: 2, x: 10.0, z: -16.2, normal: [1.0, -0.2], kind: 'window' },
  { id: 'bdrm2_win', label: '2F · Bdrm 2 Front Win (Front-Right)', floor: 2, x: 4.8, z: 21.5, normal: [0.35, 1.0], kind: 'window' },
  { id: 'bdrm3_win', label: '2F · Bdrm 3 / Front Win (Front-Left)', floor: 2, x: -5.2, z: 19.2, normal: [-0.35, 1.0], kind: 'window' },
];

interface RoomSpec {
  id: string;
  name: string;
  floor: 1 | 2;
  x1: number;
  x2: number;
  z1: number;
  z2: number;
  height?: number;
  showLabel?: boolean;
  isSlabOnly?: boolean;
}

// Mirrored Left <-> Right PLAN 1622 Room Definitions (1F = 702 SF + Garage/Patio, 2F = 920 SF)
const PLAN_1622_ROOMS: RoomSpec[] = [
  // --- MAIN FLOOR (1F) ---
  { id: 'garage', name: 'GARAGE', floor: 1, x1: -10.0, x2: 10.0, z1: 9.5, z2: 29.0, showLabel: true },
  { id: 'powder', name: 'PWDR', floor: 1, x1: -10.0, x2: -6.2, z1: 6.8, z2: 12.8, showLabel: false },
  { id: 'entry', name: 'ENTRY', floor: 1, x1: -10.0, x2: -2.8, z1: 0.5, z2: 6.8, showLabel: true },
  { id: 'kitchen', name: 'KITCHEN', floor: 1, x1: -2.8, x2: 10.0, z1: -1.5, z2: 9.5, showLabel: true },
  { id: 'island', name: 'ISLAND', floor: 1, x1: 0.4, x2: 5.4, z1: 1.5, z2: 4.8, height: 3.0, showLabel: false },
  { id: 'dining', name: 'DINING', floor: 1, x1: -6.2, x2: 10.0, z1: -12.0, z2: -1.5, showLabel: true },
  { id: 'great_room', name: 'GREAT ROOM', floor: 1, x1: -10.0, x2: 10.0, z1: -26.0, z2: -12.0, showLabel: true },
  { id: 'patio', name: 'PATIO', floor: 1, x1: -0.8, x2: 9.2, z1: -36.0, z2: -26.0, height: 0.4, showLabel: true, isSlabOnly: true },

  // --- UPPER FLOOR (2F) ---
  { id: 'bdrm3', name: 'BDRM 3', floor: 2, x1: -10.0, x2: -0.3, z1: 6.5, z2: 19.2, showLabel: true },
  { id: 'bdrm2', name: 'BDRM 2', floor: 2, x1: -0.3, x2: 10.0, z1: 7.5, z2: 21.5, showLabel: true },
  { id: 'loft', name: 'LOFT', floor: 2, x1: -10.0, x2: 3.2, z1: -11.5, z2: 6.5, showLabel: true },
  { id: 'hall_bath', name: 'BATH', floor: 2, x1: 3.2, x2: 10.0, z1: 0.5, z2: 7.5, showLabel: false },
  { id: 'utility', name: 'UTLTY', floor: 2, x1: 3.2, x2: 10.0, z1: -6.5, z2: 0.5, showLabel: true },
  { id: 'air_linen', name: 'HVAC', floor: 2, x1: 3.2, x2: 10.0, z1: -11.5, z2: -6.5, showLabel: false },
  { id: 'primary', name: 'PRIMARY SUITE', floor: 2, x1: -10.0, x2: 3.3, z1: -26.0, z2: -11.5, showLabel: true },
  { id: 'primary_bath', name: 'P. BATH', floor: 2, x1: 3.3, x2: 10.0, z1: -20.5, z2: -11.5, showLabel: false },
  { id: 'wic', name: 'WIC', floor: 2, x1: 3.3, x2: 10.0, z1: -26.0, z2: -20.5, showLabel: true },
];

const CAMERA_NODES = [
  { stream: 'annke_1', label: 'DRIVEWAY', floor: 1 as const, x: -3.5, yOffset: 6.8, z: 29.3 },
  { stream: 'tapo_2', label: 'LIVING RM', floor: 1 as const, x: -8.6, yOffset: 6.2, z: -24.6 },
  { stream: 'tapo_1', label: 'LOFT', floor: 2 as const, x: -2.0, yOffset: 6.2, z: 4.8 },
];

const FLOOR_1_Y = 0.0;
const FLOOR_2_Y = 12.2;
const WALL_HEIGHT = 7.0;

const OVERVIEW_CAM_POS = new THREE.Vector3(-42, 34, 46);
const OVERVIEW_TARGET = new THREE.Vector3(0, 8.5, 0);

function getSemanticAnchorCandidates(zone: PerimeterZone): string[] {
  const hay = `${zone.entity_id} ${zone.name} ${zone.raw_name || ''}`.toLowerCase();
  const candidates: string[] = [];

  if (hay.includes('front') && hay.includes('door')) candidates.push('front_entry');
  if (hay.includes('front') && hay.includes('win')) candidates.push('bdrm3_win', 'bdrm2_win');
  if (hay.includes('to garage') || hay.includes('garage entry') || hay.includes('interior')) {
    candidates.push('garage_entry');
  } else if (hay.includes('garage door') || (hay.includes('garage') && hay.includes('over'))) {
    candidates.push('garage_overhead', 'garage_entry');
  } else if (hay.includes('garage')) {
    candidates.push('garage_entry', 'garage_overhead');
  }
  if (hay.includes('gate') || hay.includes('fence') || hay.includes('yard')) candidates.push('side_gate');
  if (hay.includes('slid') || hay.includes('patio') || hay.includes('glass door') || (hay.includes('back') && hay.includes('door'))) {
    candidates.push('back_slider');
  }
  if (hay.includes('laundry') || hay.includes('util')) candidates.push('laundry_door');
  if (hay.includes('kitchen')) candidates.push('kitchen_win');
  if (hay.includes('dining') && hay.includes('2')) {
    candidates.push('dining_win_2', 'dining_win');
  } else if (hay.includes('dining')) {
    candidates.push('dining_win', 'dining_win_2');
  }
  if (hay.includes('living') && hay.includes('l')) candidates.push('great_side_win');
  if (hay.includes('living') || hay.includes('great')) candidates.push('great_rear_win', 'great_side_win');
  if (hay.includes('office') || hay.includes('loft')) candidates.push('loft_win', 'bdrm3_win', 'master_win_r');
  if (hay.includes('master') && hay.includes('bath')) candidates.push('master_bath_win');
  if (hay.includes('master') || hay.includes('primary')) candidates.push('master_win_l', 'master_win_r');
  if (hay.includes('bdrm 2') || hay.includes('bedroom 2')) candidates.push('bdrm2_win');
  if (hay.includes('bdrm 3') || hay.includes('bedroom 3') || hay.includes('guest')) {
    candidates.push('bdrm3_win');
  }
  if (candidates.length === 0 && hay.includes('win')) {
    candidates.push('bdrm2_win', 'bdrm3_win');
  }

  return candidates;
}

/**
 * 3-pass deterministic resolver so alphabetical order never lets a fallback sensor steal
 * another sensor's explicit or semantic door/window pin.
 */
export function resolveAllZoneAnchors(zones: PerimeterZone[]): HousePinAnchor[] {
  const assigned: (HousePinAnchor | null)[] = new Array(zones.length).fill(null);
  const usedIds = new Set<string>();

  // Pass 1: Explicit user-configured 3D pins
  zones.forEach((z, idx) => {
    if (z.pin_id) {
      const explicit = HOUSE_PIN_ANCHORS.find((a) => a.id === z.pin_id);
      if (explicit) {
        assigned[idx] = explicit;
        usedIds.add(explicit.id);
      }
    }
  });

  // Pass 2: Semantic name matches
  zones.forEach((z, idx) => {
    if (assigned[idx]) return;
    const candidates = getSemanticAnchorCandidates(z);
    for (const cid of candidates) {
      if (!usedIds.has(cid)) {
        const found = HOUSE_PIN_ANCHORS.find((a) => a.id === cid);
        if (found) {
          assigned[idx] = found;
          usedIds.add(found.id);
          break;
        }
      }
    }
  });

  // Pass 3: Remaining unused anchors for any unrecognized custom sensor names
  zones.forEach((_, idx) => {
    if (assigned[idx]) return;
    const fallback =
      HOUSE_PIN_ANCHORS.find((a) => !usedIds.has(a.id)) ||
      HOUSE_PIN_ANCHORS[idx % HOUSE_PIN_ANCHORS.length];
    assigned[idx] = fallback;
    usedIds.add(fallback.id);
  });

  return assigned as HousePinAnchor[];
}

function isZoneTriggeredInSnap(zone: PerimeterZone, snap: PerimeterSnapshot): boolean {
  if (snap.alarm_state !== 'triggered') return false;
  if (snap.triggered_entity_ids && snap.triggered_entity_ids.includes(zone.entity_id)) {
    return true;
  }
  const lowerName = zone.name.toLowerCase();
  if (snap.triggered_zones && snap.triggered_zones.some((tz) => tz.toLowerCase() === lowerName)) {
    return true;
  }
  if (snap.triggered_by && snap.triggered_by.toLowerCase() === lowerName) {
    return true;
  }
  return zone.state === 'OPEN' && !zone.bypassed;
}

interface ProjectedLabel {
  id: string;
  text: string;
  x: number;
  y: number;
  visible: boolean;
  floor: 1 | 2;
}

interface ProjectedSensor {
  entityId: string;
  name: string;
  state: 'OPEN' | 'CLOSED';
  bypassed: boolean;
  battery?: number | null;
  floor: 1 | 2;
  x: number;
  y: number;
  visible: boolean;
  isTriggeredTarget: boolean;
}

interface ProjectedCam {
  stream: string;
  label: string;
  floor: 1 | 2;
  x: number;
  y: number;
  visible: boolean;
}

interface House3DViewerProps {
  snap: PerimeterSnapshot;
  selectedZoneId?: string | null;
  focusSeq?: number;
  onSelectZone?: (entityId: string | null) => void;
  onEditZone?: (entityId: string) => void;
  onZoneClick?: (entityId: string) => void;
  onFocusStream?: (streamName: string) => void;
}

export function House3DViewer({
  snap,
  selectedZoneId = null,
  focusSeq = 0,
  onSelectZone,
  onEditZone,
  onZoneClick,
  onFocusStream,
}: House3DViewerProps) {
  const mountRef = useRef<HTMLDivElement | null>(null);
  const [floorFilter, setFloorFilter] = useState<'all' | 1 | 2>('all');
  const [expanded, setExpanded] = useState(false);
  const [hoveredZoneId, setHoveredZoneId] = useState<string | null>(null);
  const [roomLabels, setRoomLabels] = useState<ProjectedLabel[]>([]);
  const [sensorOverlays, setSensorOverlays] = useState<ProjectedSensor[]>([]);
  const [camOverlays, setCamOverlays] = useState<ProjectedCam[]>([]);

  const mappedZones = useMemo(() => {
    const anchors = resolveAllZoneAnchors(snap.zones);
    return snap.zones.map((z, idx) => {
      const anchor = anchors[idx];
      const baseY = anchor.floor === 1 ? FLOOR_1_Y : FLOOR_2_Y;
      const y = baseY + (anchor.kind === 'door' ? 3.4 : 4.3);
      return {
        zone: z,
        anchor,
        worldPos: new THREE.Vector3(anchor.x, y, anchor.z),
      };
    });
  }, [snap.zones]);

  const selectedMapped = useMemo(
    () => (selectedZoneId ? mappedZones.find((m) => m.zone.entity_id === selectedZoneId) || null : null),
    [mappedZones, selectedZoneId]
  );

  const mappedZonesRef = useRef(mappedZones);
  mappedZonesRef.current = mappedZones;

  const snapRef = useRef(snap);
  snapRef.current = snap;

  const floorFilterRef = useRef(floorFilter);
  floorFilterRef.current = floorFilter;

  const selectedZoneIdRef = useRef(selectedZoneId);
  selectedZoneIdRef.current = selectedZoneId;

  const slewGoalPosRef = useRef<THREE.Vector3 | null>(null);
  const slewGoalTargetRef = useRef<THREE.Vector3 | null>(null);
  const prevTriggeredKeyRef = useRef<string>('');

  const focusOnZone = (worldPos: THREE.Vector3, normal: [number, number], dist = 24) => {
    const nx = normal[0];
    const nz = normal[1];
    const len = Math.hypot(nx, nz) || 1;
    slewGoalTargetRef.current = worldPos.clone();
    slewGoalPosRef.current = new THREE.Vector3(
      worldPos.x + (nx / len) * dist,
      worldPos.y + 11.2,
      worldPos.z + (nz / len) * dist
    );
  };

  const applyFloorOverviewCamera = (targetFloor: 'all' | 1 | 2) => {
    if (targetFloor === 1) {
      slewGoalTargetRef.current = new THREE.Vector3(0, 3.5, 2);
      slewGoalPosRef.current = new THREE.Vector3(-36, 24, 40);
    } else if (targetFloor === 2) {
      slewGoalTargetRef.current = new THREE.Vector3(0, FLOOR_2_Y + 3.5, -2);
      slewGoalPosRef.current = new THREE.Vector3(-36, FLOOR_2_Y + 24, 36);
    } else {
      slewGoalTargetRef.current = OVERVIEW_TARGET.clone();
      slewGoalPosRef.current = OVERVIEW_CAM_POS.clone();
    }
  };

  const handleManualFloorButton = (fl: 'all' | 1 | 2) => {
    setFloorFilter(fl);
    if (fl === 'all') {
      onSelectZone?.(null);
    }
    applyFloorOverviewCamera(fl);
  };

  const resetCameraOverview = () => {
    setFloorFilter('all');
    onSelectZone?.(null);
    applyFloorOverviewCamera('all');
  };

  // Focus & auto-select floor when operator selects a sensor from LIST view (or clicks in 3D)
  useEffect(() => {
    if (!selectedZoneId) return;
    const found = mappedZones.find((m) => m.zone.entity_id === selectedZoneId);
    if (found) {
      setFloorFilter(found.anchor.floor);
      focusOnZone(found.worldPos, found.anchor.normal, 23);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedZoneId, focusSeq]);

  // Smart Multi-Alarm Floor Slicer & Camera Framing:
  //   - Single-floor breach (1 or more sensors on same floor): auto-select that floor (greying out other floor) & zoom in
  //   - Multi-floor breach (sensors across BOTH 1F & 2F): auto-select 'all' so full 2-story wireframe stays live!
  useEffect(() => {
    if (snap.alarm_state === 'triggered') {
      const breached = mappedZones.filter((m) => isZoneTriggeredInSnap(m.zone, snap));
      const trigKey = `${snap.alarm_state}:${(snap.triggered_entity_ids || []).join(',')}:${
        snap.triggered_by || ''
      }:${breached.map((b) => b.zone.entity_id).join(',')}`;

      if (prevTriggeredKeyRef.current !== trigKey) {
        prevTriggeredKeyRef.current = trigKey;
        if (breached.length === 1) {
          const only = breached[0];
          setFloorFilter(only.anchor.floor);
          focusOnZone(only.worldPos, only.anchor.normal, 24);
        } else if (breached.length > 1) {
          const floors = Array.from(new Set(breached.map((b) => b.anchor.floor)));
          const centroid = new THREE.Vector3();
          let sumNx = 0;
          let sumNz = 0;
          for (const b of breached) {
            centroid.add(b.worldPos);
            sumNx += b.anchor.normal[0];
            sumNz += b.anchor.normal[1];
          }
          centroid.divideScalar(breached.length);

          let maxSpread = 0;
          for (const b of breached) {
            maxSpread = Math.max(maxSpread, centroid.distanceTo(b.worldPos));
          }

          if (floors.length === 1) {
            // Multiple alarms on the SAME floor -> isolate that floor & grey out the other floor
            setFloorFilter(floors[0]);
            const nLen = Math.hypot(sumNx, sumNz);
            const norm: [number, number] = nLen > 0.25 ? [sumNx / nLen, sumNz / nLen] : [-0.75, 0.65];
            focusOnZone(centroid, norm, Math.min(48, 25 + maxSpread * 0.85));
          } else {
            // Multiple alarms across BOTH 1F and 2F -> keep FULL 2-story wireframe live!
            setFloorFilter('all');
            const nLen = Math.hypot(sumNx, sumNz);
            const norm: [number, number] = nLen > 0.25 ? [sumNx / nLen, sumNz / nLen] : [-0.72, 0.72];
            const dist = Math.min(56, 38 + maxSpread * 0.55);
            slewGoalTargetRef.current = centroid.clone();
            slewGoalPosRef.current = new THREE.Vector3(
              centroid.x + norm[0] * dist,
              Math.max(28, centroid.y + 18),
              centroid.z + norm[1] * dist
            );
          }
        }
      }
    } else if (prevTriggeredKeyRef.current !== '') {
      prevTriggeredKeyRef.current = '';
      setFloorFilter('all');
      slewGoalTargetRef.current = OVERVIEW_TARGET.clone();
      slewGoalPosRef.current = OVERVIEW_CAM_POS.clone();
    }
  }, [snap, mappedZones]);

  useEffect(() => {
    const container = mountRef.current;
    if (!container) return;

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(
      42,
      Math.max(1, container.clientWidth) / Math.max(1, container.clientHeight),
      0.5,
      300
    );
    camera.position.copy(OVERVIEW_CAM_POS);

    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'high-performance' });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.setSize(container.clientWidth, container.clientHeight);
    container.innerHTML = '';
    container.appendChild(renderer.domElement);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.target.copy(OVERVIEW_TARGET);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.maxPolarAngle = Math.PI * 0.485;
    controls.minDistance = 14;
    controls.maxDistance = 115;

    const cancelAutoSlew = () => {
      slewGoalPosRef.current = null;
      slewGoalTargetRef.current = null;
    };
    controls.addEventListener('start', cancelAutoSlew);

    // Subtle ground radar grid + driveway apron + parked F-150 wireframe silhouette
    const gridHelper = new THREE.GridHelper(96, 32, 0x0d9488, 0x112233);
    gridHelper.position.set(0, -0.08, 0);
    const gridMats = Array.isArray(gridHelper.material) ? gridHelper.material : [gridHelper.material];
    for (const gm of gridMats) {
      gm.transparent = true;
      gm.opacity = 0.35;
    }
    scene.add(gridHelper);

    const drivewayGeo = new THREE.EdgesGeometry(new THREE.BoxGeometry(19.2, 0.12, 16.0));
    const drivewayMat = new THREE.LineBasicMaterial({ color: 0x1e3a5f, transparent: true, opacity: 0.55 });
    const drivewayLines = new THREE.LineSegments(drivewayGeo, drivewayMat);
    drivewayLines.position.set(0, -0.02, 37.2);
    scene.add(drivewayLines);

    // Subtle wireframe silhouette of homeowner's parked F-150 on front-left of driveway for instant spatial orientation
    const truckGroup = new THREE.Group();
    const truckMat = new THREE.LineBasicMaterial({ color: 0x334155, transparent: true, opacity: 0.45 });
    const truckBed = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(5.6, 2.6, 13.5)), truckMat);
    truckBed.position.set(-5.2, 1.35, 36.8);
    const truckCab = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(5.4, 2.1, 6.2)), truckMat);
    truckCab.position.set(-5.2, 3.6, 36.5);
    truckGroup.add(truckBed, truckCab);
    scene.add(truckGroup);

    // Build Floor 1 & Floor 2 Holographic Groups
    const floor1Group = new THREE.Group();
    const floor2Group = new THREE.Group();
    scene.add(floor1Group, floor2Group);

    const f1EdgeMats: THREE.LineBasicMaterial[] = [];
    const f2EdgeMats: THREE.LineBasicMaterial[] = [];
    const f1FillMats: THREE.MeshBasicMaterial[] = [];
    const f2FillMats: THREE.MeshBasicMaterial[] = [];

    const roomCentroids: { id: string; name: string; floor: 1 | 2; pos: THREE.Vector3 }[] = [];

    for (const rm of PLAN_1622_ROOMS) {
      const w = rm.x2 - rm.x1;
      const d = rm.z2 - rm.z1;
      const h = rm.height ?? WALL_HEIGHT;
      const cx = (rm.x1 + rm.x2) * 0.5;
      const cz = (rm.z1 + rm.z2) * 0.5;
      const baseY = rm.floor === 1 ? FLOOR_1_Y : FLOOR_2_Y;

      const boxGeo = new THREE.BoxGeometry(w, h, d);
      const fillMat = new THREE.MeshBasicMaterial({
        color: 0x082f49,
        transparent: true,
        opacity: rm.isSlabOnly ? 0.14 : 0.08,
        depthWrite: false,
      });
      const mesh = new THREE.Mesh(boxGeo, fillMat);
      mesh.position.set(cx, baseY + h * 0.5, cz);

      const edgesGeo = new THREE.EdgesGeometry(boxGeo);
      const edgeMat = new THREE.LineBasicMaterial({
        color: 0x10b981,
        transparent: true,
        opacity: rm.isSlabOnly ? 0.5 : 0.88,
      });
      const wire = new THREE.LineSegments(edgesGeo, edgeMat);
      wire.position.copy(mesh.position);

      if (rm.floor === 1) {
        floor1Group.add(mesh, wire);
        f1EdgeMats.push(edgeMat);
        f1FillMats.push(fillMat);
      } else {
        floor2Group.add(mesh, wire);
        f2EdgeMats.push(edgeMat);
        f2FillMats.push(fillMat);
      }

      if (rm.showLabel) {
        roomCentroids.push({
          id: rm.id,
          name: rm.name,
          floor: rm.floor,
          pos: new THREE.Vector3(cx, baseY + 0.6, cz),
        });
      }
    }

    // Side-yard fence + Gate wing (x = 10.0..15.8, z = 9.5) on 1F for exterior Gate Sensor orientation
    const gateWingMat = new THREE.LineBasicMaterial({ color: 0x10b981, transparent: true, opacity: 0.6 });
    const gateFence = new THREE.LineSegments(
      new THREE.EdgesGeometry(new THREE.BoxGeometry(5.8, 5.2, 0.25)),
      gateWingMat
    );
    gateFence.position.set(12.9, FLOOR_1_Y + 2.6, 9.5);
    floor1Group.add(gateFence);
    f1EdgeMats.push(gateWingMat);
    roomCentroids.push({
      id: 'side_gate_lbl',
      name: 'SIDE GATE',
      floor: 1,
      pos: new THREE.Vector3(13.2, FLOOR_1_Y + 0.5, 9.5),
    });

    // Garage front X-brace architectural detail on z = +29.0
    const garageDoorPts = [
      new THREE.Vector3(-8.0, 0.2, 29.02),
      new THREE.Vector3(8.2, 6.2, 29.02),
      new THREE.Vector3(8.2, 0.2, 29.02),
      new THREE.Vector3(-8.0, 6.2, 29.02),
    ];
    const gDoorMat = new THREE.LineBasicMaterial({ color: 0x0ea5e9, transparent: true, opacity: 0.35 });
    floor1Group.add(new THREE.LineSegments(new THREE.BufferGeometry().setFromPoints(garageDoorPts), gDoorMat));
    f1EdgeMats.push(gDoorMat);

    // 3D Holographic Staircase along mirrored Left wall: starts at Great Room (z = -11.2) and climbs up into Loft (z = -2.0)
    const stairMat = new THREE.LineBasicMaterial({ color: 0x22d3ee, transparent: true, opacity: 0.55 });
    for (let i = 0; i < 9; i++) {
      const stepGeo = new THREE.EdgesGeometry(new THREE.BoxGeometry(3.4, 0.45, 1.05));
      const stepLine = new THREE.LineSegments(stepGeo, stairMat);
      stepLine.position.set(-8.1, FLOOR_1_Y + 0.5 + i * 0.78, -11.2 + i * 1.15);
      floor1Group.add(stepLine);
    }
    f1EdgeMats.push(stairMat);

    // Vertical holographic alignment guide beams between 1F and 2F corners
    const pillarPts: THREE.Vector3[] = [];
    for (const [px, pz] of [
      [-10.0, -26.0],
      [10.0, -26.0],
      [-10.0, 19.2],
      [10.0, 21.5],
    ]) {
      pillarPts.push(new THREE.Vector3(px, FLOOR_1_Y + WALL_HEIGHT, pz), new THREE.Vector3(px, FLOOR_2_Y, pz));
    }
    const pillarMat = new THREE.LineBasicMaterial({ color: 0x1e293b, transparent: true, opacity: 0.5 });
    scene.add(new THREE.LineSegments(new THREE.BufferGeometry().setFromPoints(pillarPts), pillarMat));

    // Master Bedroom Siren Node inside Primary Suite (-5.5, FLOOR_2_Y + 5.8, -18.5)
    const sirenGeo = new THREE.OctahedronGeometry(0.95, 0);
    const sirenMat = new THREE.MeshBasicMaterial({ color: 0x38bdf8, wireframe: true, transparent: true, opacity: 0.6 });
    const sirenMesh = new THREE.Mesh(sirenGeo, sirenMat);
    sirenMesh.position.set(-5.5, FLOOR_2_Y + 5.6, -18.5);
    floor2Group.add(sirenMesh);

    // Dynamic Sensor Beacon Pool + Multi-Sensor Shockwave Ring Pool (supports simultaneous multi-zone breaches!)
    const sensorMeshes: THREE.Mesh[] = [];
    const sensorFrames: THREE.LineSegments[] = [];
    const shockRingsA: THREE.Mesh[] = [];
    const shockRingsB: THREE.Mesh[] = [];

    const sharedSphereGeo = new THREE.SphereGeometry(0.72, 14, 14);
    const sharedFrameGeo = new THREE.EdgesGeometry(new THREE.BoxGeometry(2.3, 2.8, 0.55));
    const sharedShockGeo = new THREE.RingGeometry(0.9, 1.35, 32);
    sharedShockGeo.rotateX(-Math.PI * 0.5);

    for (let i = 0; i < 20; i++) {
      const sph = new THREE.Mesh(
        sharedSphereGeo,
        new THREE.MeshBasicMaterial({ color: 0x10b981, transparent: true, opacity: 1.0 })
      );
      sph.visible = false;
      scene.add(sph);
      sensorMeshes.push(sph);

      const frm = new THREE.LineSegments(
        sharedFrameGeo,
        new THREE.LineBasicMaterial({ color: 0x22d3ee, transparent: true, opacity: 0.75 })
      );
      frm.visible = false;
      scene.add(frm);
      sensorFrames.push(frm);

      const sr1 = new THREE.Mesh(
        sharedShockGeo,
        new THREE.MeshBasicMaterial({ color: 0xef4444, side: THREE.DoubleSide, transparent: true, opacity: 0.0 })
      );
      const sr2 = new THREE.Mesh(
        sharedShockGeo,
        new THREE.MeshBasicMaterial({ color: 0xf87171, side: THREE.DoubleSide, transparent: true, opacity: 0.0 })
      );
      sr1.visible = false;
      sr2.visible = false;
      scene.add(sr1, sr2);
      shockRingsA.push(sr1);
      shockRingsB.push(sr2);
    }

    // Raycaster for clicking 3D sensor spheres directly (ignores sensors on a greyed-out floor)
    const raycaster = new THREE.Raycaster();
    const pointer = new THREE.Vector2();
    let downX = 0;
    let downY = 0;

    const onPointerDown = (ev: PointerEvent) => {
      downX = ev.clientX;
      downY = ev.clientY;
    };
    const onPointerUp = (ev: PointerEvent) => {
      if (Math.hypot(ev.clientX - downX, ev.clientY - downY) > 6) return;
      const rect = renderer.domElement.getBoundingClientRect();
      pointer.x = ((ev.clientX - rect.left) / rect.width) * 2 - 1;
      pointer.y = -((ev.clientY - rect.top) / rect.height) * 2 + 1;
      raycaster.setFromCamera(pointer, camera);
      const ff = floorFilterRef.current;
      const clickableMeshes = sensorMeshes.filter((m, idx) => {
        if (!m.visible) return false;
        const mz = mappedZonesRef.current[idx];
        return Boolean(mz && (ff === 'all' || ff === mz.anchor.floor));
      });
      const hits = raycaster.intersectObjects(clickableMeshes, false);
      if (hits.length > 0) {
        const hitIdx = sensorMeshes.indexOf(hits[0].object as THREE.Mesh);
        const mz = mappedZonesRef.current[hitIdx];
        if (mz) {
          if (snapRef.current.is_mock && onZoneClick) {
            onZoneClick(mz.zone.entity_id);
          } else {
            onSelectZone?.(mz.zone.entity_id);
            setFloorFilter(mz.anchor.floor);
            focusOnZone(mz.worldPos, mz.anchor.normal, 23);
          }
        }
      }
    };
    renderer.domElement.addEventListener('pointerdown', onPointerDown);
    renderer.domElement.addEventListener('pointerup', onPointerUp);

    const onResize = () => {
      if (!container) return;
      const w = Math.max(1, container.clientWidth);
      const h = Math.max(1, container.clientHeight);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
      renderer.setSize(w, h);
    };
    const ro = new ResizeObserver(onResize);
    ro.observe(container);

    let animId = 0;
    let lastOverlayTick = 0;
    const projVec = new THREE.Vector3();

    const animate = (nowMs: number) => {
      animId = window.requestAnimationFrame(animate);
      const t = nowMs * 0.001;
      const curSnap = snapRef.current;
      const curZones = mappedZonesRef.current;
      const ff = floorFilterRef.current;
      const selId = selectedZoneIdRef.current;

      // Smooth kinematic camera slew when auto-facing a triggered/selected zone
      if (slewGoalPosRef.current && slewGoalTargetRef.current) {
        camera.position.lerp(slewGoalPosRef.current, 0.085);
        controls.target.lerp(slewGoalTargetRef.current, 0.095);
        if (
          camera.position.distanceTo(slewGoalPosRef.current) < 0.25 &&
          controls.target.distanceTo(slewGoalTargetRef.current) < 0.25
        ) {
          slewGoalPosRef.current = null;
          slewGoalTargetRef.current = null;
        }
      }

      controls.update();

      // Dynamic wireframe color palette: active floor(s) glow neon, unselected floor is truly greyed out!
      const primaryHex =
        curSnap.alarm_state === 'triggered'
          ? 0xf87171
          : curSnap.alarm_state === 'armed_night'
          ? 0x38bdf8
          : curSnap.alarm_state === 'armed_away' || curSnap.alarm_state === 'arming'
          ? 0xf59e0b
          : 0x10b981;
      const greyedHex = 0x475569;

      const f1Greyed = ff === 2;
      const f2Greyed = ff === 1;

      for (const m of f1EdgeMats) {
        m.color.setHex(f1Greyed ? greyedHex : primaryHex);
        m.opacity = f1Greyed ? 0.15 : 0.9;
      }
      for (const m of f2EdgeMats) {
        m.color.setHex(f2Greyed ? greyedHex : primaryHex);
        m.opacity = f2Greyed ? 0.15 : 0.9;
      }
      for (const fm of f1FillMats) {
        fm.color.setHex(f1Greyed ? 0x0f172a : 0x082f49);
        fm.opacity = f1Greyed ? 0.015 : 0.08;
      }
      for (const fm of f2FillMats) {
        fm.color.setHex(f2Greyed ? 0x0f172a : 0x082f49);
        fm.opacity = f2Greyed ? 0.015 : 0.08;
      }

      // Siren pulse inside Primary Suite (2F)
      if (curSnap.siren_state === 'on') {
        sirenMat.color.setHex(0xef4444);
        sirenMat.opacity = f2Greyed ? 0.28 : 0.65 + Math.sin(t * 14) * 0.35;
        sirenMesh.rotation.y += 0.09;
        const sScale = 1.15 + Math.sin(t * 12) * 0.25;
        sirenMesh.scale.setScalar(sScale);
      } else {
        sirenMat.color.setHex(f2Greyed ? greyedHex : 0x38bdf8);
        sirenMat.opacity = f2Greyed ? 0.12 : 0.45;
        sirenMesh.rotation.y += 0.012;
        sirenMesh.scale.setScalar(1.0);
      }

      // Update 3D sensor nodes & individual shockwave rings for all open/triggered zones
      for (let i = 0; i < sensorMeshes.length; i++) {
        const sph = sensorMeshes[i];
        const frm = sensorFrames[i];
        const sr1 = shockRingsA[i];
        const sr2 = shockRingsB[i];

        if (i >= curZones.length) {
          sph.visible = false;
          frm.visible = false;
          sr1.visible = false;
          sr2.visible = false;
          continue;
        }

        const item = curZones[i];
        const floorGreyed = (ff === 1 && item.anchor.floor === 2) || (ff === 2 && item.anchor.floor === 1);
        sph.visible = true;
        frm.visible = true;
        sph.position.copy(item.worldPos);
        frm.position.copy(item.worldPos);

        const wallOnX = Math.abs(item.anchor.x) >= 9.5 && item.anchor.id !== 'side_gate';
        frm.rotation.y = wallOnX ? Math.PI * 0.5 : 0;

        const sMat = sph.material as THREE.MeshBasicMaterial;
        const fMat = frm.material as THREE.LineBasicMaterial;

        if (floorGreyed) {
          sMat.color.setHex(greyedHex);
          sMat.opacity = 0.2;
          fMat.color.setHex(greyedHex);
          fMat.opacity = 0.14;
          sph.scale.setScalar(0.58);
          sr1.visible = false;
          sr2.visible = false;
          continue;
        }

        sMat.opacity = 1.0;
        const isOpen = item.zone.state === 'OPEN';
        const isByp = Boolean(item.zone.bypassed);
        const isSelected = selId === item.zone.entity_id;
        const isTrigTarget = isZoneTriggeredInSnap(item.zone, curSnap);

        if (isTrigTarget) {
          sMat.color.setHex(0xef4444);
          fMat.color.setHex(0xef4444);
          fMat.opacity = 1.0;
          sph.scale.setScalar(1.38 + Math.sin(t * 12) * 0.3);
        } else if (isOpen) {
          sMat.color.setHex(isByp ? 0xf59e0b : 0xef4444);
          fMat.color.setHex(isByp ? 0xf59e0b : 0xef4444);
          fMat.opacity = 0.95;
          sph.scale.setScalar(1.18 + Math.sin(t * 6) * 0.14);
        } else if (isSelected) {
          sMat.color.setHex(0x34d399);
          fMat.color.setHex(0x34d399);
          fMat.opacity = 1.0;
          sph.scale.setScalar(1.25 + Math.sin(t * 7) * 0.16);
        } else {
          sMat.color.setHex(0x22d3ee);
          fMat.color.setHex(0x22d3ee);
          fMat.opacity = 0.65;
          sph.scale.setScalar(0.85);
        }

        const emitShockwave = isTrigTarget || (isOpen && !isByp);
        if (emitShockwave) {
          sr1.visible = true;
          sr2.visible = true;
          sr1.position.copy(item.worldPos);
          sr2.position.copy(item.worldPos);
          const p1 = (t * 1.4 + i * 0.13) % 1;
          const p2 = (t * 1.4 + i * 0.13 + 0.5) % 1;
          sr1.scale.setScalar(1 + p1 * 5.2);
          (sr1.material as THREE.MeshBasicMaterial).opacity = (1 - p1) * 0.85;
          sr2.scale.setScalar(1 + p2 * 5.2);
          (sr2.material as THREE.MeshBasicMaterial).opacity = (1 - p2) * 0.65;
        } else {
          sr1.visible = false;
          sr2.visible = false;
        }
      }

      renderer.render(scene, camera);

      // Project crisp 2D HTML room & sensor HUD labels at ~24 FPS so React stays silky smooth
      if (nowMs - lastOverlayTick >= 42) {
        lastOverlayTick = nowMs;
        const cw = container.clientWidth;
        const ch = container.clientHeight;

        const nextRooms: ProjectedLabel[] = roomCentroids.map((rc) => {
          projVec.copy(rc.pos).project(camera);
          const vis =
            projVec.z < 1 &&
            (ff === 'all' || ff === rc.floor) &&
            projVec.x >= -0.96 &&
            projVec.x <= 0.96 &&
            projVec.y >= -0.96 &&
            projVec.y <= 0.96;
          return {
            id: rc.id,
            text: rc.name,
            floor: rc.floor,
            x: (projVec.x * 0.5 + 0.5) * cw,
            y: (-projVec.y * 0.5 + 0.5) * ch,
            visible: vis,
          };
        });
        setRoomLabels(nextRooms);

        const nextSensors: ProjectedSensor[] = curZones.map((mz) => {
          projVec.copy(mz.worldPos).project(camera);
          const floorOk = ff === 'all' || ff === mz.anchor.floor;
          const vis =
            floorOk &&
            projVec.z < 1 &&
            projVec.x >= -0.98 &&
            projVec.x <= 0.98 &&
            projVec.y >= -0.98 &&
            projVec.y <= 0.98;
          const isTrig = isZoneTriggeredInSnap(mz.zone, curSnap);
          return {
            entityId: mz.zone.entity_id,
            name: mz.zone.name,
            state: mz.zone.state,
            bypassed: Boolean(mz.zone.bypassed),
            battery: mz.zone.battery,
            floor: mz.anchor.floor,
            x: (projVec.x * 0.5 + 0.5) * cw,
            y: (-projVec.y * 0.5 + 0.5) * ch,
            visible: vis,
            isTriggeredTarget: isTrig,
          };
        });
        setSensorOverlays(nextSensors);

        const nextCams: ProjectedCam[] = CAMERA_NODES.map((cn) => {
          const baseY = cn.floor === 1 ? FLOOR_1_Y : FLOOR_2_Y;
          projVec.set(cn.x, baseY + cn.yOffset, cn.z).project(camera);
          const vis =
            (ff === 'all' || ff === cn.floor) &&
            projVec.z < 1 &&
            projVec.x >= -0.95 &&
            projVec.x <= 0.95 &&
            projVec.y >= -0.95 &&
            projVec.y <= 0.95;
          return {
            stream: cn.stream,
            label: cn.label,
            floor: cn.floor,
            x: (projVec.x * 0.5 + 0.5) * cw,
            y: (-projVec.y * 0.5 + 0.5) * ch,
            visible: vis,
          };
        });
        setCamOverlays(nextCams);
      }
    };

    animId = window.requestAnimationFrame(animate);

    return () => {
      window.cancelAnimationFrame(animId);
      ro.disconnect();
      controls.removeEventListener('start', cancelAutoSlew);
      renderer.domElement.removeEventListener('pointerdown', onPointerDown);
      renderer.domElement.removeEventListener('pointerup', onPointerUp);
      controls.dispose();
      renderer.dispose();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div
      className={`relative w-full rounded-lg overflow-hidden border border-cyan-500/25 bg-[radial-gradient(ellipse_at_center,#081726_0%,#04070d_78%)] select-none transition-all duration-300 ${
        expanded ? 'h-[520px] sm:h-[600px]' : 'h-[275px] sm:h-[305px]'
      }`}
    >
      {/* WebGL Canvas Container */}
      <div ref={mountRef} className="absolute inset-0 cursor-grab active:cursor-grabbing" />

      {/* Projected Architectural Room Labels */}
      <div className="pointer-events-none absolute inset-0 overflow-hidden">
        {roomLabels.map(
          (rl) =>
            rl.visible && (
              <div
                key={rl.id}
                style={{ transform: `translate(-50%, -50%) translate(${rl.x}px, ${rl.y}px)` }}
                className={`absolute text-[9px] sm:text-[10px] font-bold tracking-widest px-1.5 py-0.5 rounded border backdrop-blur-[1px] whitespace-nowrap ${
                  rl.floor === 2
                    ? 'text-cyan-200/85 bg-cyan-950/45 border-cyan-500/25'
                    : 'text-emerald-200/85 bg-emerald-950/45 border-emerald-500/25'
                }`}
              >
                {rl.text}
              </div>
            )
        )}
      </div>

      {/* Projected Interactive Sensor Callouts & Nodes */}
      <div className="pointer-events-none absolute inset-0 overflow-hidden">
        {sensorOverlays.map((so) => {
          if (!so.visible) return null;
          const isOpen = so.state === 'OPEN';
          const isSelected = selectedZoneId === so.entityId;
          const showFullPill =
            isOpen || so.isTriggeredTarget || isSelected || hoveredZoneId === so.entityId;
          return (
            <div
              key={so.entityId}
              style={{ transform: `translate(-50%, -115%) translate(${so.x}px, ${so.y}px)` }}
              onMouseEnter={() => setHoveredZoneId(so.entityId)}
              onMouseLeave={() => setHoveredZoneId(null)}
              onClick={() => {
                if (snap.is_mock && onZoneClick) {
                  onZoneClick(so.entityId);
                } else {
                  onSelectZone?.(so.entityId);
                  const found = mappedZones.find((m) => m.zone.entity_id === so.entityId);
                  if (found) {
                    setFloorFilter(found.anchor.floor);
                    focusOnZone(found.worldPos, found.anchor.normal, 23);
                  }
                }
              }}
              title={
                snap.is_mock
                  ? `${so.name} (${so.state}) — Click to simulate door/window toggle`
                  : `${so.name} (${so.state}) — Click to isolate floor & focus 3D camera`
              }
              className={`pointer-events-auto absolute cursor-pointer transition-transform ${
                showFullPill
                  ? `z-20 px-2 py-0.5 rounded text-[10px] font-bold border flex items-center gap-1 whitespace-nowrap shadow-lg ${
                      so.isTriggeredTarget
                        ? 'animate-bounce bg-red-600 text-white border-red-200 shadow-[0_0_16px_rgba(239,68,68,0.9)]'
                        : isOpen
                        ? so.bypassed
                          ? 'bg-amber-950/95 text-amber-200 border-amber-400'
                          : 'bg-red-950/95 text-red-100 border-red-500 shadow-[0_0_12px_rgba(239,68,68,0.5)]'
                        : isSelected
                        ? 'bg-emerald-950/95 text-emerald-200 border-emerald-400 shadow-[0_0_14px_rgba(16,185,129,0.55)]'
                        : 'bg-zinc-950/95 text-cyan-200 border-cyan-400/60'
                    }`
                  : 'z-10 w-3.5 h-3.5 -translate-y-1 rounded-full bg-cyan-400/20 hover:bg-cyan-400/50 border border-cyan-300/70'
              }`}
            >
              {showFullPill && (
                <>
                  <span
                    className={`w-1.5 h-1.5 rounded-full ${
                      isOpen
                        ? so.bypassed
                          ? 'bg-amber-400'
                          : 'bg-white animate-ping'
                        : 'bg-emerald-400'
                    }`}
                  />
                  <span>{so.name}</span>
                  <span className="text-[8px] opacity-80">
                    {isOpen ? (so.bypassed ? 'BYP' : 'OPEN') : 'OK'}
                  </span>
                </>
              )}
            </div>
          );
        })}

        {/* Projected Live RTSP Camera Quick-Focus Pips */}
        {camOverlays.map(
          (co) =>
            co.visible && (
              <button
                key={co.stream}
                type="button"
                style={{ transform: `translate(-50%, -50%) translate(${co.x}px, ${co.y}px)` }}
                onClick={() => onFocusStream?.(co.stream)}
                title={`Click to open live ${co.label} WebRTC stream in Theater Mode`}
                className="pointer-events-auto absolute z-10 px-1.5 py-0.5 rounded bg-zinc-950/85 hover:bg-emerald-900/90 text-emerald-300 border border-emerald-500/40 text-[8px] font-bold flex items-center gap-1 cursor-pointer"
              >
                <Camera className="w-2.5 h-2.5" />
                {co.label}
              </button>
            )
        )}
      </div>

      {/* Top-Left Floor Slicer & Selected Sensor Callout */}
      <div className="absolute top-2.5 left-2.5 z-20 flex flex-wrap items-center gap-1.5">
        <div className="flex items-center bg-zinc-950/90 border border-zinc-800 rounded-md p-0.5 text-[10px]">
          <span className="px-1.5 text-zinc-500 flex items-center gap-1">
            <Layers className="w-3 h-3 text-cyan-400" />
          </span>
          {(['all', 2, 1] as const).map((fl) => (
            <button
              key={String(fl)}
              type="button"
              onClick={() => handleManualFloorButton(fl)}
              className={`px-2 py-0.5 rounded font-bold cursor-pointer transition-colors ${
                floorFilter === fl
                  ? 'bg-cyan-600 text-white'
                  : 'text-zinc-400 hover:text-zinc-200'
              }`}
            >
              {fl === 'all' ? 'ALL' : fl === 2 ? '2F UPPER' : '1F MAIN'}
            </button>
          ))}
        </div>

        <button
          type="button"
          onClick={resetCameraOverview}
          title="Reset 3D camera to full 2-story house overview"
          className="px-2 py-1 rounded bg-zinc-950/90 hover:bg-zinc-900 text-zinc-300 border border-zinc-800 text-[10px] font-bold flex items-center gap-1 cursor-pointer"
        >
          <RotateCcw className="w-3 h-3 text-cyan-400" />
          RESET VIEW
        </button>

        {selectedMapped && (
          <div className="flex items-center gap-1.5 px-2 py-0.5 rounded bg-emerald-950/90 border border-emerald-500/50 text-[10px] text-emerald-200">
            <span className="font-bold">{selectedMapped.zone.name}</span>
            <span className="text-emerald-400/75 hidden md:inline">({selectedMapped.anchor.label})</span>
            {onEditZone && (
              <button
                type="button"
                onClick={() => onEditZone(selectedMapped.zone.entity_id)}
                title="Rename sensor or change its 3D pin location"
                className="px-1.5 py-0.5 rounded bg-emerald-800/70 hover:bg-emerald-700 text-white flex items-center gap-0.5 cursor-pointer"
              >
                <Pencil className="w-2.5 h-2.5" />
                <span>PIN</span>
              </button>
            )}
            <button
              type="button"
              onClick={resetCameraOverview}
              title="Clear selected sensor & restore full wireframe"
              className="text-emerald-300 hover:text-white cursor-pointer"
            >
              <X className="w-3 h-3" />
            </button>
          </div>
        )}
      </div>

      {/* Top-Right Expand Viewport Button */}
      <div className="absolute top-2.5 right-2.5 z-20 flex items-center gap-1.5">
        {snap.siren_state === 'on' && (
          <span className="px-2 py-1 rounded bg-red-950/90 border border-red-500 text-red-200 text-[9px] font-bold flex items-center gap-1">
            <Volume2 className="w-3 h-3 text-red-400 animate-pulse" />
            PRIMARY SUITE SIREN
          </span>
        )}
        <button
          type="button"
          onClick={() => setExpanded((p) => !p)}
          title={expanded ? 'Collapse 3D stage to docked height' : 'Expand 3D holographic stage'}
          className="px-2 py-1 rounded bg-zinc-950/90 hover:bg-zinc-900 text-cyan-300 border border-cyan-500/30 text-[10px] font-bold flex items-center gap-1 cursor-pointer"
        >
          {expanded ? <Minimize2 className="w-3 h-3" /> : <Maximize2 className="w-3 h-3" />}
          {expanded ? 'DOCK' : 'EXPAND'}
        </button>
      </div>

      {/* Bottom Tactical HUD Legend */}
      <div className="pointer-events-none absolute bottom-2 left-2.5 right-2.5 z-20 flex flex-wrap items-center justify-between gap-2 text-[9px] sm:text-[10px] text-zinc-400">
        <div className="flex items-center gap-2 bg-zinc-950/80 border border-zinc-800/90 px-2 py-0.5 rounded">
          <Compass className="w-3 h-3 text-cyan-400" />
          <span>
            PLAN 1622 MIRRORED · <span className="text-cyan-300">DRAG</span> ORBIT ·{' '}
            <span className="text-cyan-300">SCROLL</span> ZOOM
            {snap.is_mock ? ' · CLICK ANY BEACON TO TEST BREACH' : ' · CLICK BEACON OR LIST ITEM TO FOCUS'}
          </span>
        </div>

        <div className="hidden sm:flex items-center gap-3 bg-zinc-950/80 border border-zinc-800/90 px-2.5 py-0.5 rounded">
          <span className="flex items-center gap-1 text-cyan-300">
            <span className="w-2 h-2 rounded-full bg-cyan-400" />
            CLOSED ({snap.total_zones - snap.breached_count})
          </span>
          <span className="flex items-center gap-1 text-red-300">
            <span className="w-2 h-2 rounded-full bg-red-500" />
            OPEN ({snap.breached_count})
          </span>
        </div>
      </div>
    </div>
  );
}
