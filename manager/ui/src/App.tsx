import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Bridge, BridgeError, Project, StatusDocument, TagsResponse } from "./api";
import { isError } from "./api";
import AdapterMatrix from "./components/AdapterMatrix";
import Header from "./components/Header";
import NewProjectDialog from "./components/NewProjectDialog";
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

/**
 * 쓰기 동작(`update_apply`·`apply_new`·`run`)이 도는 프로젝트를 세는 배선 — 세 호출 모두 첫
 * 인자가 그 프로젝트 폴더다.
 *
 * `pm-config update` 는 그 사본의 엔진 파일을 교체하는 중이라, 그 구간에 시작한 주기 조회는
 * rc≠0(부분 사본)으로 끝나 **성공하는 갱신이** '먼저 갱신'·'조회 실패' 로 보인다. 실패를
 * 숨기는 것이 아니라 그 동안 그 사본을 부르지 않는다 — 동작 뒤 재조회는 명시 호출 그대로다.
 */
function trackWrites(bridge: Bridge, inFlight: Map<string, number>): Bridge {
  const counted = <Arguments extends unknown[], Result>(
    call: (...args: Arguments) => Promise<Result>) => async (...args: Arguments) => {
      const path = String(args[0]);
      inFlight.set(path, (inFlight.get(path) ?? 0) + 1);
      try {
        return await call(...args);
      } finally {
        const running = (inFlight.get(path) ?? 1) - 1;
        if (running > 0) inFlight.set(path, running);
        else inFlight.delete(path);
      }
    };
  return {
    ...bridge,
    update_apply: counted(bridge.update_apply),
    apply_new: counted(bridge.apply_new),
    run: counted(bridge.run),
  };
}

export default function App({ bridge: given }: { bridge: Bridge }) {
  // 쓰기 동작이 도는 프로젝트와 그 동작 수 — 화면에 보이는 값이 아니라 주기 조회의 제외 목록이다.
  const writing = useRef(new Map<string, number>()).current;
  const bridge = useMemo(() => trackWrites(given, writing), [given, writing]);
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
  const [newOpen, setNewOpen] = useState(false);
  // 갓 적용한 프로젝트 — 이 값이 서 있는 동안만 설정 탭이 질문형으로 열린다(사람이 목록에서
  // 무엇이든 고르면 풀린다 · 같은 프로젝트를 다시 고를 때마다 질문형이 되지 않는다).
  const [appliedPath, setAppliedPath] = useState<string | null>(null);

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

  // 주기 조회 — 창이 보이지 않으면 걸지 않고, 쓰기 동작이 도는 프로젝트는 그 동작이 끝날
  // 때까지 뺀다(다른 프로젝트의 주기와 실패 격리는 그대로다).
  useEffect(() => {
    if (!visible || projects.length === 0) return;
    const timer = setInterval(() => {
      void refreshLight(projects.map((project) => project.path)
        .filter((path) => !writing.has(path)));
    }, LIGHT_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [visible, projects, refreshLight, writing]);

  // 선택한 프로젝트만 무거운 절 1회.
  useEffect(() => {
    if (selected !== null) void refreshHeavy(selected);
  }, [selected, refreshHeavy]);

  const addProject = useCallback(async () => {
    const picked = await bridge.pick_folder();
    if (isError(picked) || picked.path === null) return;
    const before = new Set(projects.map((project) => project.path));
    const response = await bridge.add_project(picked.path);   // 선택 대화의 표기는 등록 인자로만
    if (isError(response)) {
      setListError(response.error);
      return;
    }
    setListError(null);
    setProjects(response.projects);
    // 새 행의 조회 키는 **등록부가 돌려준 표기**다. 폴더 선택이 준 표기로 부르면 목록 행과 키가
    // 달라(심볼릭 링크·끝 슬래시·물결표) 그 행이 자기 결과를 못 찾고 다음 주기까지 빈다.
    await refreshLight(response.projects.map((project) => project.path)
      .filter((path) => !before.has(path)));
  }, [bridge, projects, refreshLight]);

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

  // 적용 성공 — 등록부를 다시 읽고 그 프로젝트를 골라 질문형 설정으로 넘긴다.
  const openApplied = useCallback(async (path: string) => {
    setNewOpen(false);
    await loadProjects();
    setSelected(path);
    setAppliedPath(path);
    await refreshLight([path]);
  }, [loadProjects, refreshLight]);

  // 관리자가 그 프로젝트에 쓰기 동작을 한 직후 — 가벼운 절 + 무거운 절 둘만 다시 읽는다
  // (최신 태그는 네트워크라 여기서 돌리지 않는다 · §6.5).
  const refreshAfterAction = useCallback(() => {
    if (selected === null) return;
    void Promise.all([refreshLight([selected]), refreshHeavy(selected)]);
  }, [selected, refreshLight, refreshHeavy]);

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
        onSelect={(path) => { setAppliedPath(null); setSelected(path); }}
        onAdd={addProject}
        onRemove={removeProject}
        onOpenMatrix={() => setMatrixOpen(true)}
        onOpenNew={() => setNewOpen(true)}
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
              latestTag={selected === null ? null : latestTagOf(tags, selected)}
              onOpenUpdate={refreshTags} onApplied={refreshSelected}
              onChanged={refreshAfterAction}
              startAsking={selected !== null && selected === appliedPath} />
        {matrixOpen && <AdapterMatrix bridge={bridge} onClose={() => setMatrixOpen(false)} />}
        {newOpen && (
          <NewProjectDialog bridge={bridge} onClose={() => setNewOpen(false)}
                            onApplied={(path) => void openApplied(path)} />
        )}
      </main>
    </div>
  );
}
