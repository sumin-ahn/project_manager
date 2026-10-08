import { useCallback, useEffect, useState } from "react";
import type {
  Bridge, BridgeError, ConfKeysDocument, DelegateModelsDocument, Project, StatusDocument,
  TagsResponse } from "./api";
import { chooseFolder, isError, subscribeChanges } from "./api";
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

/**
 * 프로젝트별 설정 두 조회의 마지막 값 — 설정 탭은 이것을 받아 그린다(탭 진입 조회 0).
 *
 * 프로젝트를 고를 때 무거운 절과 함께 미리 받고, 다시 읽는 때는 저장 직후·갱신 적용 직후·수동
 * 새로고침 셋뿐이다. 다시 읽는 동안에도 마지막 값을 지우지 않는다(빈 화면 0).
 */
export type SettingsEntry = {
  keys?: ConfKeysDocument;
  values?: Record<string, string>;
  /** 하네스별 모델 목록 — 프로젝트를 고를 때 배경으로 받아 둔다(설정 탭 진입 호출 0 유지). */
  models?: Record<string, DelegateModelsDocument>;
  error?: BridgeError;
  at?: number;
};

type Busy = { light: boolean; heavy: boolean; tags: boolean };

/**
 * 마지막 값 한 칸의 갱신 — 실패를 화면에 말하는 자리는 **사람이 누른 조회**뿐이다.
 *
 * 배경 조회(주기·창 복귀·서버 알림)는 실패해도 마지막 값을 그대로 둔다. 저장·적용이 도는
 * 프로젝트의 배경 조회는 서버가 거절하는데, 그것을 사람에게 경고로 띄우면 성공하는 갱신이
 * 실패로 보인다(§6.5 재조회 규칙).
 */
function merged(previous: Entry | undefined, response: StatusDocument | { error: BridgeError },
                at: number, explicit: boolean): Entry {
  if (isError(response)) {
    if (!explicit) return previous ?? {};
    return { status: previous?.status, error: response.error, at };
  }
  return { status: response, at };
}

export default function App({ bridge }: { bridge: Bridge }) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [listError, setListError] = useState<BridgeError | null>(null);
  const [light, setLight] = useState<Record<string, Entry>>({});
  const [heavy, setHeavy] = useState<Record<string, Entry>>({});
  // 원격 대조(fetch) — 네트워크라 시작·수동 새로고침 때만 돈다(주기 조회 0).
  const [remote, setRemote] = useState<Record<string, Entry>>({});
  const [settings, setSettings] = useState<Record<string, SettingsEntry>>({});
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

  const refreshLight = useCallback(async (paths: string[], explicit = true) => {
    if (paths.length === 0) return;
    setBusy((current) => ({ ...current, light: true }));
    const results = await Promise.all(
      paths.map(async (path) => [path, await bridge.status(path, false, false)] as const));
    const at = Date.now();
    setLight((previous) => {
      const next = { ...previous };
      for (const [path, response] of results) {
        next[path] = merged(previous[path], response, at, explicit);
      }
      return next;
    });
    setBusy((current) => ({ ...current, light: false }));
  }, [bridge]);

  const refreshHeavy = useCallback(async (path: string, explicit = true) => {
    setBusy((current) => ({ ...current, heavy: true }));
    const response = await bridge.status(path, true, false);
    const at = Date.now();
    setHeavy((previous) => ({ ...previous,
                              [path]: merged(previous[path], response, at, explicit) }));
    setBusy((current) => ({ ...current, heavy: false }));
  }, [bridge]);

  /**
   * 모델 목록 배경 선읽기 — 기다리지 않는다.
   *
   * 설정 탭은 진입 때 사본을 부르지 않는다는 계약이라(T-0917) 목록도 그 계약 안에서 받아야
   * 한다. 프로젝트를 고르는 시점에 **await 없이** 던져 두면 도착하는 대로 모델 칸이
   * 드롭다운이 되고, 도착 전에는 지금처럼 텍스트 칸 + [목록 불러오기] 다. 어느 하네스를
   * 물을지는 키 레지스트리가 정한다(`choices.fetch` 가 있는 행의 `depends_on` 현재 값) —
   * 화면이 하네스 이름을 들지 않는다. 생각 깊이 칸도 같은 하네스의 같은 응답(`variants`)을 쓰므로
   * 조회가 늘지 않는다.
   */
  const prefetchModels = useCallback((
    path: string, keys: ConfKeysDocument, values: Record<string, string>,
  ) => {
    const wanted = new Set<string>();
    for (const entry of keys.keys) {
      const rows = entry.kind === "key" ? [entry] : (entry.members ?? []);
      for (const row of rows) {
        const axis = row.choices;
        if (axis === null || axis.fetch === null || axis.depends_on === null) continue;
        const harness = values[axis.depends_on] ?? "";
        if (harness !== "") wanted.add(harness);
      }
    }
    for (const harness of wanted) {
      void bridge.delegate_models(path, harness).then((response) => {
        if (isError(response)) return;          // 배경 조회의 실패는 말하지 않는다.
        setSettings((previous) => {
          const entry = previous[path];
          if (entry === undefined) return previous;
          return { ...previous,
                   [path]: { ...entry, models: { ...entry.models, [harness]: response } } };
        });
      });
    }
  }, [bridge]);

  // 설정 두 조회 — 키 레지스트리와 값 파일이다(판정은 가벼운 절의 `conf` 절이라 여기서 안 부른다).
  const refreshSettings = useCallback(async (path: string, explicit = true) => {
    const [keys, values] = await Promise.all([bridge.conf_keys(path), bridge.conf_values(path)]);
    const failure = isError(keys) ? keys.error : isError(values) ? values.error : undefined;
    const at = Date.now();
    setSettings((previous) => {
      const last = previous[path];
      // 배경 조회의 실패는 말하지 않는다 — 마지막 값 그대로.
      if (failure !== undefined && !explicit) return previous;
      return { ...previous,
               [path]: { keys: isError(keys) ? last?.keys : keys,
                         values: isError(values) ? last?.values : values.values,
                         models: last?.models, error: failure, at } };
    });
    if (!isError(keys) && !isError(values)) prefetchModels(path, keys, values.values);
  }, [bridge, prefetchModels]);

  const refreshRemote = useCallback(async (paths: string[]) => {
    const results = await Promise.all(
      paths.map(async (path) => [path, await bridge.status(path, false, false, true)] as const));
    const at = Date.now();
    setRemote((previous) => {
      const next = { ...previous };
      for (const [path, response] of results) next[path] = merged(previous[path], response, at, true);
      return next;
    });
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

  // 시작 — 목록 1회 + 가벼운 절 전부 + 네트워크 1회(최신 태그 · 원격 대조).
  useEffect(() => {
    void (async () => {
      const loaded = await loadProjects();
      const paths = loaded.map((project) => project.path);
      await Promise.all([refreshLight(paths), refreshTags(), refreshRemote(paths)]);
    })();
  }, [loadProjects, refreshLight, refreshTags, refreshRemote]);

  // 창이 앞으로 올 때 즉시 1회 · 뒤로 가면 주기 정지.
  useEffect(() => {
    const onVisibility = () => {
      const nowVisible = document.visibilityState === "visible";
      setVisible(nowVisible);
      if (nowVisible) void refreshLight(projects.map((project) => project.path), false);
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, [projects, refreshLight]);

  // 주기 조회 — 창이 보이지 않으면 걸지 않는다. 저장·적용이 도는 프로젝트를 빼는 판정은
  // 서버가 한다(손님이 둘이라 화면이 세면 값이 틀린다) — 그 동안의 조회는 거절로 돌아온다.
  useEffect(() => {
    if (!visible || projects.length === 0) return;
    const timer = setInterval(() => {
      void refreshLight(projects.map((project) => project.path), false);
    }, LIGHT_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [visible, projects, refreshLight]);

  // 선택한 프로젝트만 무거운 절 1회 + 설정 두 조회 선읽기(탭을 열기 전에 채워 둔다).
  useEffect(() => {
    if (selected === null) return;
    void refreshHeavy(selected);
    void refreshSettings(selected);
  }, [selected, refreshHeavy, refreshSettings]);

  // 다른 손님(창·브라우저)이 저장·적용을 끝내면 서버가 알린다 — 그 신호로 다시 읽는다.
  useEffect(() => subscribeChanges(() => {
    void refreshLight(projects.map((project) => project.path), false);
    if (selected !== null) {
      void refreshHeavy(selected, false);
      void refreshSettings(selected, false);
    }
  }), [projects, selected, refreshLight, refreshHeavy, refreshSettings]);

  const addProject = useCallback(async () => {
    const picked = await chooseFolder(bridge);
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
    await Promise.all([refreshLight([selected]), refreshHeavy(selected), refreshTags(),
                       refreshSettings(selected), refreshRemote([selected])]);
  }, [selected, refreshLight, refreshHeavy, refreshTags, refreshSettings, refreshRemote]);

  // 설정을 저장한 직후 — 값(설정 조회)과 판정(가벼운 절)을 다시 읽는다. 낙관적 갱신은 없다.
  const reloadSettings = useCallback(() => {
    if (selected === null) return;
    void Promise.all([refreshSettings(selected), refreshLight([selected])]);
  }, [selected, refreshSettings, refreshLight]);

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
              remote={selected === null ? undefined : remote[selected]}
              settings={selected === null ? undefined : settings[selected]}
              latestTag={selected === null ? null : latestTagOf(tags, selected)}
              onOpenUpdate={refreshTags} onApplied={refreshSelected}
              onChanged={refreshAfterAction} onReloadSettings={reloadSettings}
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
