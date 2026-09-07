import { useCallback, useEffect, useState } from "react";
import type { Bridge, BridgeError, Project, StatusDocument, TagsResponse } from "./api";
import { isError } from "./api";
import AdapterMatrix from "./components/AdapterMatrix";
import Header from "./components/Header";
import ProjectList from "./components/ProjectList";
import RefreshBar from "./components/RefreshBar";
import Tabs from "./components/Tabs";
import { latestTagOf } from "./lib/status";

/** 가벼운 절 주기 — 창이 보일 때만 돈다(§6.5). */
export const LIGHT_INTERVAL_MS = 5000;

/** 영역별 마지막 값 한 칸 — 조회가 실패해도 `status` 는 지우지 않고 `error` 만 붙인다. */
export type Entry = { status?: StatusDocument; error?: BridgeError; at?: number };

type Busy = { light: boolean; heavy: boolean; tags: boolean };

function merged(previous: Entry | undefined, response: StatusDocument | { error: BridgeError },
                at: number): Entry {
  if (isError(response)) return { status: previous?.status, error: response.error, at };
  return { status: response, at };
}

export default function App({ bridge }: { bridge: Bridge }) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [listError, setListError] = useState<BridgeError | null>(null);
  const [light, setLight] = useState<Record<string, Entry>>({});
  const [heavy, setHeavy] = useState<Record<string, Entry>>({});
  const [tags, setTags] = useState<TagsResponse | null>(null);
  const [tagsError, setTagsError] = useState<BridgeError | null>(null);
  const [tagsAt, setTagsAt] = useState<number | undefined>(undefined);
  const [busy, setBusy] = useState<Busy>({ light: false, heavy: false, tags: false });
  const [selected, setSelected] = useState<string | null>(null);
  const [visible, setVisible] = useState(() => document.visibilityState === "visible");
  // 전체 보기(어댑터 매트릭스)는 등록 프로젝트마다 무거운 조회 한 번이라 열어야만 돈다.
  const [matrixOpen, setMatrixOpen] = useState(false);

  const loadProjects = useCallback(async (): Promise<Project[]> => {
    const response = await bridge.list_projects();
    if (isError(response)) {
      setListError(response.error);
      return [];
    }
    setListError(null);
    setProjects(response.projects);
    setSelected((current) => current ?? response.projects[0]?.path ?? null);
    return response.projects;
  }, [bridge]);

  const refreshLight = useCallback(async (paths: string[]) => {
    if (paths.length === 0) return;
    setBusy((current) => ({ ...current, light: true }));
    const results = await Promise.all(
      paths.map(async (path) => [path, await bridge.status(path, false, false)] as const));
    const at = Date.now();
    setLight((previous) => {
      const next = { ...previous };
      for (const [path, response] of results) next[path] = merged(previous[path], response, at);
      return next;
    });
    setBusy((current) => ({ ...current, light: false }));
  }, [bridge]);

  const refreshHeavy = useCallback(async (path: string) => {
    setBusy((current) => ({ ...current, heavy: true }));
    const response = await bridge.status(path, true, false);
    const at = Date.now();
    setHeavy((previous) => ({ ...previous, [path]: merged(previous[path], response, at) }));
    setBusy((current) => ({ ...current, heavy: false }));
  }, [bridge]);

  const refreshTags = useCallback(async () => {
    setBusy((current) => ({ ...current, tags: true }));
    const response = await bridge.latest_tags();
    setTagsAt(Date.now());
    if (isError(response)) setTagsError(response.error);
    else {
      setTagsError(null);
      setTags(response);
    }
    setBusy((current) => ({ ...current, tags: false }));
  }, [bridge]);

  // 시작 — 목록 1회 + 가벼운 절 전부 + 네트워크 1회.
  useEffect(() => {
    void (async () => {
      const loaded = await loadProjects();
      await Promise.all([refreshLight(loaded.map((project) => project.path)), refreshTags()]);
    })();
  }, [loadProjects, refreshLight, refreshTags]);

  // 창이 앞으로 올 때 즉시 1회 · 뒤로 가면 주기 정지.
  useEffect(() => {
    const onVisibility = () => {
      const nowVisible = document.visibilityState === "visible";
      setVisible(nowVisible);
      if (nowVisible) void refreshLight(projects.map((project) => project.path));
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, [projects, refreshLight]);

  // 주기 조회 — 창이 보이지 않으면 걸지 않는다.
  useEffect(() => {
    if (!visible || projects.length === 0) return;
    const timer = setInterval(() => {
      void refreshLight(projects.map((project) => project.path));
    }, LIGHT_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [visible, projects, refreshLight]);

  // 선택한 프로젝트만 무거운 절 1회.
  useEffect(() => {
    if (selected !== null) void refreshHeavy(selected);
  }, [selected, refreshHeavy]);

  const addProject = useCallback(async () => {
    const picked = await bridge.pick_folder();
    if (isError(picked) || picked.path === null) return;
    const response = await bridge.add_project(picked.path);
    if (isError(response)) {
      setListError(response.error);
      return;
    }
    setListError(null);
    setProjects(response.projects);
    await refreshLight([picked.path]);
  }, [bridge, refreshLight]);

  const removeProject = useCallback(async (path: string) => {
    const response = await bridge.remove_project(path);
    if (isError(response)) {
      setListError(response.error);
      return;
    }
    setListError(null);
    setProjects(response.projects);
    setSelected((current) => (current === path ? response.projects[0]?.path ?? null : current));
  }, [bridge]);

  const refreshSelected = useCallback(async () => {
    if (selected === null) return;
    await Promise.all([refreshLight([selected]), refreshHeavy(selected), refreshTags()]);
  }, [selected, refreshLight, refreshHeavy, refreshTags]);

  const selectedLight = selected === null ? undefined : light[selected];
  const selectedHeavy = selected === null ? undefined : heavy[selected];
  const selectedName = projects.find((project) => project.path === selected)?.name ?? "";

  return (
    <div className="flex h-screen bg-white text-[13px] text-slate-800">
      <ProjectList
        projects={projects}
        light={light}
        tags={tags}
        selected={selected}
        listError={listError}
        onSelect={setSelected}
        onAdd={addProject}
        onRemove={removeProject}
        onOpenMatrix={() => setMatrixOpen(true)}
      />
      <main className="relative flex min-w-0 flex-1 flex-col">
        <Header
          name={selectedName}
          entry={selectedLight}
          latestTag={selected === null ? null : latestTagOf(tags, selected)}
        />
        <RefreshBar
          light={{ at: selectedLight?.at, error: selectedLight?.error, busy: busy.light }}
          heavy={{ at: selectedHeavy?.at, error: selectedHeavy?.error, busy: busy.heavy }}
          tags={{ at: tagsAt, error: tagsError, busy: busy.tags }}
          onRefresh={refreshSelected}
        />
        <Tabs bridge={bridge} path={selected} light={selectedLight} heavy={selectedHeavy}
              onOpenUpdate={refreshTags} />
        {matrixOpen && <AdapterMatrix bridge={bridge} onClose={() => setMatrixOpen(false)} />}
      </main>
    </div>
  );
}
