import { useEffect, useMemo, useState } from 'react';
import type { DrawMode, InspectorSelection, Project } from './types';
import { buildDemoProjects } from './data/demoProjects';
import ProjectsSpace from './components/ProjectsSpace';
import DataSpace from './components/DataSpace';
import ScenarioSpace from './components/ScenarioSpace';
import MapSpace, { type MapClickInfo } from './components/MapSpace';
import InspectorSpace from './components/InspectorSpace';
import ResultsSpace from './components/ResultsSpace';

const TABS = ['Projets', 'Données', 'Scénario', 'Carte', 'Inspecteur', 'Résultats'] as const;
type Tab = (typeof TABS)[number];

const STORAGE_KEY = 'amiad-interface-projects-v1';

function loadProjects(): Project[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as Project[];
      if (Array.isArray(parsed) && parsed.length) return parsed;
    }
  } catch {
    /* ignore */
  }
  return buildDemoProjects();
}

export default function App() {
  const [projects, setProjects] = useState<Project[]>(loadProjects);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>('Projets');
  const [selection, setSelection] = useState<InspectorSelection | null>(null);
  const [drawMode, setDrawMode] = useState<DrawMode>(null);
  const [pendingPointName, setPendingPointName] = useState('');
  const [pendingZone, setPendingZone] = useState<[number, number][]>([]);
  const [pendingZoneKind, setPendingZoneKind] = useState<'deployment' | 'restricted'>('deployment');

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(projects));
  }, [projects]);

  const active = useMemo(() => projects.find((p) => p.id === activeId) ?? projects[0] ?? null, [projects, activeId]);

  function updateActive(fn: (p: Project) => Project) {
    if (!active) return;
    setProjects((ps) => ps.map((p) => (p.id === active.id ? fn(p) : p)));
    setSelection(null);
  }

  function handleMapClick(info: MapClickInfo) {
    if (!active) return;
    if (drawMode === 'point') {
      const name = pendingPointName.trim() || `Point ${active.scenario.points.length + 1}`;
      updateActive((p) => ({
        ...p,
        scenario: {
          ...p.scenario,
          points: [...p.scenario.points, { id: `p-${Date.now()}`, name, lon: info.lngLat.lng, lat: info.lngLat.lat }],
        },
      }));
      setPendingPointName('');
      return;
    }
    if (drawMode === 'zone-deployment' || drawMode === 'zone-restricted') {
      setPendingZone((z) => [...z, [info.lngLat.lng, info.lngLat.lat]]);
      return;
    }
    if (info.features.length) {
      const f = info.features[0];
      setSelection({
        sourceId: f.sourceId,
        sourceName: active.sources.find((s) => s.id === f.sourceId)?.name ?? f.sourceId,
        role: active.sources.find((s) => s.id === f.sourceId)?.role ?? 'resultat',
        geometryType: f.geometryType,
        properties: f.properties ?? {},
      });
      setTab('Inspecteur');
    } else {
      setSelection(null);
    }
  }

  function handleMapDblClick(lngLat: { lng: number; lat: number }) {
    if ((drawMode === 'zone-deployment' || drawMode === 'zone-restricted') && pendingZone.length >= 3 && active) {
      const kind = drawMode === 'zone-deployment' ? 'deployment' : 'restricted';
      updateActive((p) => ({
        ...p,
        scenario: {
          ...p.scenario,
          zones: [...p.scenario.zones, { id: `z-${Date.now()}`, name: kind === 'deployment' ? 'DA' : 'RA', kind, coords: pendingZone }],
        },
      }));
      setPendingZone([]);
      setDrawMode(null);
    }
  }

  function startZoneDraw(kind: 'deployment' | 'restricted') {
    setPendingZoneKind(kind);
    setPendingZone([]);
    setDrawMode(kind === 'deployment' ? 'zone-deployment' : 'zone-restricted');
  }

  return (
    <div className="app">
      <header className="app-header">
        <h1>AMIAD — Interface</h1>
        <span className="subtitle">lecture seule du dépôt · aucun calcul lancé · valeurs absentes = « Non renseigné »</span>
        <nav className="tabs">
          {TABS.map((t) => (
            <button key={t} className={tab === t ? 'active' : ''} onClick={() => setTab(t)}>
              {t}
            </button>
          ))}
        </nav>
      </header>
      <div className="app-body">
        <aside className="side">
          {tab === 'Projets' && (
            <ProjectsSpace
              projects={projects}
              activeId={active?.id ?? null}
              onSelect={setActiveId}
              onCreate={(name) => {
                const p: Project = {
                  id: `proj-${Date.now()}`,
                  name,
                  description: 'Nouveau projet (sources à ajouter dans l’espace Données).',
                  createdAt: new Date().toISOString(),
                  sources: [],
                  scenario: { text: '', points: [], zones: [], images: [] },
                  resultPaths: [],
                };
                setProjects((ps) => [...ps, p]);
                setActiveId(p.id);
              }}
              onDelete={(id) => {
                setProjects((ps) => ps.filter((p) => p.id !== id));
                if (activeId === id) setActiveId(null);
              }}
            />
          )}
          {tab === 'Données' && active && <DataSpace project={active} onUpdate={updateActive} />}
          {tab === 'Données' && !active && <p className="muted">Aucun projet.</p>}
          {tab === 'Scénario' && active && (
            <ScenarioSpace
              project={active}
              onUpdate={updateActive}
              drawMode={drawMode}
              setDrawMode={setDrawMode}
              pendingPointName={pendingPointName}
              setPendingPointName={setPendingPointName}
              pendingZone={pendingZone}
              pendingZoneKind={pendingZoneKind}
              startZoneDraw={startZoneDraw}
              clearPendingZone={() => setPendingZone([])}
            />
          )}
          {tab === 'Scénario' && !active && <p className="muted">Aucun projet.</p>}
          {tab === 'Carte' && active && <DataSpace project={active} onUpdate={updateActive} layersOnly />}
          {tab === 'Inspecteur' && <InspectorSpace selection={selection} project={active} />}
          {tab === 'Résultats' && active && <ResultsSpace project={active} />}
          {tab === 'Résultats' && !active && <p className="muted">Aucun projet.</p>}
        </aside>
        <div className="map-wrap">
          <MapSpace
            project={active}
            drawMode={drawMode}
            pendingZone={pendingZone}
            onMapClick={handleMapClick}
            onMapDblClick={handleMapDblClick}
          />
          <div className="map-hint">
            {drawMode === 'point' && 'Mode point : cliquez sur la carte pour placer le point saisi.'}
            {(drawMode === 'zone-deployment' || drawMode === 'zone-restricted') &&
              `Mode zone ${pendingZoneKind === 'deployment' ? 'DA' : 'RA'} : cliquez pour les sommets, double-clic pour fermer (${pendingZone.length} sommets).`}
            {!drawMode && 'Clic sur une entité : inspecteur. Le TIFF est un fond cartographique, jamais un réseau.'}
          </div>
        </div>
      </div>
    </div>
  );
}
