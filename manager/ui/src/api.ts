/** 브리지 타입과 pywebview 배선 — 화면이 파이썬을 부르는 유일한 자리. */

export type BridgeError = {
  kind: "old-generation" | "spawn" | "timeout" | "not-json" | "invalid-path";
  message: string;
  stderr: string;
  rc: number | null;
};

export type ErrorResponse = { error: BridgeError };

export type SlotRow = {
  slot: string;
  repo: string;
  state: string;
  session: string | null;
  pid: number;
  alive: boolean;
  role: string;
  task: string | null;
  branch: string | null;
  head: string | null;
  behind: number | null;
  dirty: boolean;
};

export type TaskRow = {
  name: string;
  prefix: string | null;
  pid: number;
  alive: boolean;
  started: string | null;
  slots: string[];
};

export type BoardSection = {
  counts: Record<string, number>;
  open: { id: string; title: string; claimed_by: string | null }[];
  lint: { kind: string; message: string }[];
  regression: Record<string, unknown> | null;
  livegate: Record<string, unknown> | null;
};

export type StatusDocument = {
  schema: number;
  engine_rev: string;
  pm_home: string;
  generated_at: string;
  slots: SlotRow[];
  tasks: TaskRow[];
  git: { branch: string | null; head: string | null; dirty: boolean | null };
  repos: { repo: string; prefix: string | null }[];
  harnesses: string[];
  conf: ConfSection;
  board: BoardSection | null;
  adapters: AdapterDocument | null;
};

/** 설정 완전성 다섯 목록. `missing_for_latest` 는 소스가 해소된 호출에서만 배열이고 아니면 null(모름). */
export type ConfSection = {
  missing_required: string[];
  legacy: { key: string; replacement: string | null }[];
  unknown: string[];
  nonstandard: { key: string; value: string; default: string }[];
  missing_for_latest: string[] | null;
};

/** 키 레지스트리 한 행 — 실명(`kind: "key"`)과 패턴 키군(`kind: "pattern"`)이 한 목록에 있다. */
export type ConfKeyRow = {
  key: string;
  group: string;
  description: string;
  default: string | null;
  required: boolean;
  kind: "key" | "pattern";
  members: { key: string; default: string | null }[] | null;
};

export type ConfKeysDocument = { schema: number; groups: string[]; keys: ConfKeyRow[] };
export type ConfValuesDocument = { values: Record<string, string>; conf: ConfSection };
export type ConfSaveResult = { changed: boolean; conf: ConfSection };

export type AdapterFeature = {
  name: string;
  project: string | number | boolean | null;
  latest: string | number | boolean | null;
  verdict: "same" | "missing" | "differs" | "retired";
};

/**
 * 어댑터 파일 한 행 — 모양이 **둘**이다. 채널 있는 파일은 아래 필드 전부를 들고, 인스턴스 소유
 * 문서(`CLAUDE.md`·`AGENTS.md`)는 `path`·`ownership`·`status` 셋뿐이다(나머지는 null 이 아니라
 * **키가 없다**). 화면은 `"mode" in file` 로 가른다.
 */
export type AdapterFile = {
  path: string;
  status: string;
  ownership?: string;
  harness?: string;
  mode?: "managed" | "report";
  template_rev?: string;
  diff_lines?: { added: number; removed: number };
  diff_vs_baseline?: string;
  diff_vs_latest?: string;
  features?: AdapterFeature[];
  distance?: number;
};

export type AdapterDocument = { source: string; files: AdapterFile[] };
export type AdapterMatrixDocument = Record<string, AdapterDocument | ErrorResponse>;
/** 사본 명령 하나의 산출 — 거부 문구는 엔진 원문이라 화면이 다시 쓰지 않는다. */
export type CommandResult = { rc: number; stdout: string; stderr: string };

/** 새 프로젝트 적용의 단계 하나 — 세 단계가 이 순서로 온다. */
export type ApplyStep = {
  name: "import" | "init" | "register";
  rc: number;
  stdout: string;
  stderr: string;
};

/** 적용 결과 — 중간에 실패해도 그때까지의 단계 로그가 실려 온다. */
export type ApplyResult = {
  steps: ApplyStep[];
  ok: boolean;
  path: string | null;
  question_mode: boolean;
};

/** 스폰·응답 없음처럼 rc 가 없는 실패 — 그때까지의 단계 로그를 함께 나른다. */
export type ApplyFailure = ErrorResponse & { steps?: ApplyStep[] };

/** 적용 대화가 미리 채울 값 — 하네스 이름과 빌드 각인 upstream(둘 다 번들에서 온다). */
export type ApplyDefaults = { harnesses: string[]; upstream: string };

export type Project = { path: string; name: string };
export type ProjectsResponse = { projects: Project[] };
export type TagsResponse = {
  projects: Record<string, { upstream: string | null }>;
  upstreams: Record<string, { tag?: string | null } & Partial<ErrorResponse>>;
};

export type Bridge = {
  list_projects(): Promise<ProjectsResponse | ErrorResponse>;
  add_project(path: string): Promise<ProjectsResponse | ErrorResponse>;
  remove_project(path: string): Promise<ProjectsResponse | ErrorResponse>;
  status(path: string, withBoard: boolean, withAdapters: boolean):
    Promise<StatusDocument | ErrorResponse>;
  latest_tags(): Promise<TagsResponse | ErrorResponse>;
  pick_folder(): Promise<{ path: string | null } | ErrorResponse>;
  conf_keys(path: string): Promise<ConfKeysDocument | ErrorResponse>;
  conf_values(path: string): Promise<ConfValuesDocument | ErrorResponse>;
  conf_save(path: string, updates: Record<string, string>):
    Promise<ConfSaveResult | ErrorResponse>;
  conf_reset(path: string, key: string): Promise<ConfSaveResult | ErrorResponse>;
  adapters(path: string): Promise<AdapterDocument | ErrorResponse>;
  adapter_accept(path: string, relpath: string): Promise<CommandResult | ErrorResponse>;
  adapter_matrix(): Promise<AdapterMatrixDocument | ErrorResponse>;
  upstream_show(path: string): Promise<CommandResult | ErrorResponse>;
  upstream_set(path: string, value: string): Promise<CommandResult | ErrorResponse>;
  update_preview(path: string): Promise<CommandResult | ErrorResponse>;
  update_apply(path: string): Promise<CommandResult | ErrorResponse>;
  apply_defaults(): Promise<ApplyDefaults | ErrorResponse>;
  apply_new(dest: string, harness: string, name: string, prefix: string, area: string,
            owner: string, upstream: string, prefixAck: string):
    Promise<ApplyResult | ApplyFailure>;
};

export function isError<T>(response: T | ErrorResponse): response is ErrorResponse {
  return typeof response === "object" && response !== null && "error" in response;
}

declare global {
  interface Window {
    pywebview?: { api: Record<string, (...args: unknown[]) => Promise<unknown>> };
  }
}

/** pywebview 가 API 를 주입한 뒤에만 부른다(주입 전 호출은 undefined 로 터진다). */
function whenReady(): Promise<void> {
  if (window.pywebview?.api) return Promise.resolve();
  return new Promise((resolve) => {
    window.addEventListener("pywebviewready", () => resolve(), { once: true });
  });
}

export function pywebviewBridge(): Bridge {
  const call = async (name: string, ...args: unknown[]) => {
    await whenReady();
    return window.pywebview!.api[name](...args);
  };
  return {
    list_projects: () => call("list_projects") as Promise<ProjectsResponse | ErrorResponse>,
    add_project: (path) => call("add_project", path) as Promise<ProjectsResponse | ErrorResponse>,
    remove_project: (path) =>
      call("remove_project", path) as Promise<ProjectsResponse | ErrorResponse>,
    status: (path, withBoard, withAdapters) =>
      call("status", path, withBoard, withAdapters) as Promise<StatusDocument | ErrorResponse>,
    latest_tags: () => call("latest_tags") as Promise<TagsResponse | ErrorResponse>,
    pick_folder: () => call("pick_folder") as Promise<{ path: string | null } | ErrorResponse>,
    conf_keys: (path) => call("conf_keys", path) as Promise<ConfKeysDocument | ErrorResponse>,
    conf_values: (path) =>
      call("conf_values", path) as Promise<ConfValuesDocument | ErrorResponse>,
    conf_save: (path, updates) =>
      call("conf_save", path, updates) as Promise<ConfSaveResult | ErrorResponse>,
    conf_reset: (path, key) =>
      call("conf_reset", path, key) as Promise<ConfSaveResult | ErrorResponse>,
    adapters: (path) => call("adapters", path) as Promise<AdapterDocument | ErrorResponse>,
    adapter_accept: (path, relpath) =>
      call("adapter_accept", path, relpath) as Promise<CommandResult | ErrorResponse>,
    adapter_matrix: () => call("adapter_matrix") as Promise<AdapterMatrixDocument | ErrorResponse>,
    upstream_show: (path) => call("upstream_show", path) as Promise<CommandResult | ErrorResponse>,
    upstream_set: (path, value) =>
      call("upstream_set", path, value) as Promise<CommandResult | ErrorResponse>,
    update_preview: (path) =>
      call("update_preview", path) as Promise<CommandResult | ErrorResponse>,
    update_apply: (path) => call("update_apply", path) as Promise<CommandResult | ErrorResponse>,
    apply_defaults: () => call("apply_defaults") as Promise<ApplyDefaults | ErrorResponse>,
    apply_new: (dest, harness, name, prefix, area, owner, upstream, prefixAck) =>
      call("apply_new", dest, harness, name, prefix, area, owner, upstream, prefixAck) as
        Promise<ApplyResult | ApplyFailure>,
  };
}
