/** 브리지 타입과 HTTP 배선 — 화면이 파이썬을 부르는 유일한 자리. */

export type BridgeError = {
  /**
   * 앞 다섯은 바깥(사본·git·등록부)을 부르다 실패한 자리이고, 다음 둘은 부르기를 **거절한**
   * 자리다(허용 목록 밖 명령 · `--force` · 저장·적용이 도는 프로젝트의 다른 요청 · 브라우저의
   * 폴더 고르기). 거절은 서브프로세스 0개라 `stderr` 가 빈 값이다. 마지막 `invalid-value` 는
   * 사본이 저장값을 검사에서 거부한 자리다(사용 한도·압축 비율 · 문구는 사본 원문).
   */
  kind: "old-generation" | "spawn" | "timeout" | "not-json" | "invalid-path"
      | "not-allowed" | "forbidden-flag" | "invalid-value";
  message: string;
  stderr: string;
  rc: number | null;
};

export type ErrorResponse = { error: BridgeError };

/** 등록 repo 한 행 — areas.md 레지스트리 칼럼 그대로다(`repo list` 와 같은 칼럼·빈 셀은 null). */
export type RepoRow = {
  repo: string;
  prefix: string | null;
  /** 등록된 원격 주소. */
  git: string | null;
  base: string | null;
  protected: string | null;
  test_cmd: string | null;
  area_owner: string | null;
};

export type SlotRow = {
  slot: string;
  repo: string;
  state: string;
  session: string | null;
  /** 열린 기록이 있고 그에 대응하는 종료 기록이 없는가 (프로세스 조회 아님). */
  alive: boolean;
  role: string;
  task: string | null;
  branch: string | null;
  head: string | null;
  /** 부모 브랜치 기록 — 기록이 없으면 null. readonly 슬롯은 따라갈 브랜치. */
  parent: SlotParent | null;
  /** 이 브랜치를 부모로 기록한 브랜치들. */
  children: string[];
  /** 이 브랜치 이름으로 쌓인 stash 수. */
  stashes: number;
  dirty: boolean;
  /** `--with-remote` 로 부른 조회에서만 값이 있다(네트워크). */
  remote: SlotRemote | null;
};

/** 부모 브랜치 — 부모 브랜치가 지워졌으면 `ref`·`ahead`·`behind` 가 null. */
export type SlotParent = {
  name: string;
  fork: string;
  ref: string | null;
  ahead: number | null;
  behind: number | null;
};

/** 슬롯의 원격 정보 — origin 주소 · 추적 원격 브랜치. */
export type SlotRemote = {
  url: string;
  /** 슬롯 브랜치가 연결된 원격 브랜치 — 브랜치가 없거나 연결하지 않았으면 null. */
  upstream: SlotUpstream | null;
};

/** 추적 원격 브랜치 — 원격에서 지워졌으면 이름만 있고 나머지는 null. */
export type SlotUpstream = {
  name: string;
  head: string | null;
  ahead: number | null;
  behind: number | null;
};

/** 슬롯 없는 보존 브랜치 — 슬롯 제거 때 삭제 판정이 남긴 브랜치(쓰던 슬롯은 이미 없다). */
export type PreservedBranchRow = {
  repo: string;
  name: string;
  /** 보존 직전 이 브랜치를 쓰던 슬롯. */
  slot: string;
  /** 보존 시각 (UTC ISO). */
  preserved_at: string;
  /** 삭제 판정 사유 코드 (엔진 `BRANCH_REASON_*` 값 그대로). */
  reason: string;
  /** 부모 브랜치 이름 — 부모 기록이 없으면 null. */
  parent: string | null;
  /** 부모에 합쳐지지 않은 커밋 수 — 부모 기록이 없거나 부모 브랜치가 지워졌으면 null. */
  unmerged: number | null;
};

/** 열린 todo 한 건. 상태 필드가 없다 — 존재 = 열림, 완료 = 목록에서 제거. */
export type TaskTodo = {
  /** task 안에서 유일한 번호. 지워도 재사용되지 않으므로 남은 줄의 번호가 밀리지 않는다. */
  id: number;
  text: string;
  /** 등록 시각(UTC ISO). 비어 있으면 얼마나 묵었는지 알 수 없다. */
  created: string;
};

export type TaskRow = {
  name: string;
  prefix: string | null;
  /** 열린 기록이 있고 그에 대응하는 종료 기록이 없는가 (프로세스 조회 아님). */
  alive: boolean;
  started: string | null;
  slots: string[];
  /** 이 task 가 지금 무엇을 하는 중인지. 비어 있으면 파일을 고칠 때 경고가 붙는다. */
  todos: TaskTodo[];
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
  preserved_branches: PreservedBranchRow[];
  tasks: TaskRow[];
  git: { branch: string | null; head: string | null; dirty: boolean | null };
  repos: RepoRow[];
  harnesses: string[];
  conf: ConfSection;
  board: BoardSection | null;
  adapters: AdapterDocument | null;
};

/** 설정 파일 값이 이 머신 기록 값과 다른 경로 하나 — `file` 은 설정 파일에서 읽은 값 그대로이고 키가 없으면 null. */
export type CtxBudgetMismatch = { path: string; file: unknown; record: number };
/**
 * 압축 한도 기록의 도구 항목(`pm_import.ctx_budget_status`) — 적용할 때 조회한 모델 가용 한도 C·적용 한도 E·압축 지점 P와
 * 설정 파일 값이 기록과 다른 경로(`settings_mismatch`, 같으면 빈 배열).
 */
export type CtxBudget =
  | { status: "ok"; models: string[]; C: number; E: number; P: number; applied_at: string; basis_text: string;
      settings_mismatch: CtxBudgetMismatch[] }
  | { status: "error"; reason: string };
/** 설정 완전성 다섯 목록. `missing_for_latest` 는 소스가 해소된 호출에서만 배열이고 아니면 null(모름). */
export type ConfSection = {
  ctx_budget?: Record<string, CtxBudget>;
  missing_required: string[];
  legacy: { key: string; replacement: string | null }[];
  unknown: string[];
  nonstandard: { key: string; value: string; default: string }[];
  missing_for_latest: string[] | null;
};

/**
 * 그 키에 고를 수 있는 값 — 엔진 레지스트리가 소유한다(화면에 목록 상수를 두지 않는다).
 *
 * `values` 는 **버킷** 표다: `depends_on` 이 없으면 키가 `""` 인 한 칸이고, 있으면 그 키의 현재
 * 값이 버킷 이름이다(하네스마다 다른 추론 강도). 목록 원천 규칙은 하나다: 버킷이 `values` 에 있으면
 * 그 목록, 없고 `fetch` 가 있으면 그 명령으로 받아 온 응답에서 `fetch.variants_of` 가 있으면
 * `variants[그 키의 현재 값]`, 없으면 `models`. `free_text` 는 목록 밖 값도 유효하다는 뜻이고 그런
 * 칸에만 직접 입력이 열린다.
 */
export type ConfChoices = {
  depends_on: string | null;
  values: Record<string, string[]>;
  /**
   * `value` 는 조회 대상이 키에 이미 박혀 있을 때다(하네스 군) — 종속 키를 볼 것이 없다.
   * `variants_of` 는 응답의 모델별 변형(추론 강도)을 고를 모델 키다(같은 세트의 모델 칸).
   */
  fetch: { command: string; arg: string; value?: string; variants_of?: string } | null;
  free_text: boolean;
};

/** 키 레지스트리 한 행 — 실명(`kind: "key"`)과 패턴 키군(`kind: "pattern"`)이 한 목록에 있다. */
export type ConfKeyRow = {
  key: string;
  group: string;
  label: string;
  description: string;
  unit_labels: Record<string, string>;
  default: string | null;
  /** 안 채웠을 때 일어나는 일 한 문장 — 기본값이 있으면 null(그 값이 답이다). */
  unset: string | null;
  required: boolean;
  kind: "key" | "pattern";
  choices: ConfChoices | null;
  recommended: string | null;
  members: ConfKeyMember[] | null;
} & ConfRowAxes;

/**
 * 그 절을 켜는 스위치 — 꺼져 있으면 이 행은 아무 효과가 없다.
 * `on` 은 켜진 것으로 치는 값이고 null 이면 "값이 있으면 켜짐"이다.
 */
export type ConfGovernor = { key: string; on: string[] | null; reason: string };

/** 행마다 붙는 축 셋 (schema 6). */
export type ConfRowAxes = {
  /** 도구·모델·생각 깊이를 한 세트로 읽는 묶음 이름 — 체크박스가 하나다. */
  unit: string | null;
  governed_by: ConfGovernor | null;
  /** 엔진이 적는 키 — 고칠 자리가 없다. */
  readonly: boolean;
};

/** 패턴 키군에서 전개된 실명 한 행 — 선택지·권장값을 각자 든다. */
export type ConfKeyMember = {
  key: string;
  default: string | null;
  unset: string | null;
  choices: ConfChoices | null;
  recommended: string | null;
} & ConfRowAxes;

export type ConfKeysDocument = {
  schema: number;
  groups: string[];
  /** 절 이름의 사람 말 — 묶음 id 는 코드 좌표라 화면에 그대로 세우지 않는다. */
  group_labels: Record<string, string>;
  /** 묶음 이름 → 줄 이름. */
  units: Record<string, string>;
  /** 묶음 칸 이름 `<접미사> → 사람이 읽는 이름`. */
  unit_fields: Record<string, string>;
  keys: ConfKeyRow[];
};
/** 값 파일 그대로 — 판정(다섯 목록)은 가벼운 현황 조회의 `conf` 절이 나른다(조회 1회). */
export type ConfValuesDocument = { values: Record<string, string> };
/**
 * 저장 결과 — `applied` 는 사용 한도·압축 비율 저장이 하네스 설정 파일에서 바꾼 줄이다
 * (`<파일> <경로> <옛 값> → <새 값>` · 그 밖의 저장은 빈 목록).
 */
export type ConfSaveResult = { changed: boolean; conf: ConfSection; applied: string[] };
/**
 * `delegate models --harness <h> --json` 산출 그대로 — 빈 목록 성공은 없다(0개면 실패다).
 * `variants` 는 모델별 변형(추론 강도) 목록이고 그것을 내는 원천만 채운다(나머지는 null). 변형이
 * 없는 모델은 빈 배열이다.
 */
export type DelegateModelsDocument = {
  harness: string; kind: string; models: string[]; variants: Record<string, string[]> | null;
};

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
 * `status: null` = 선언된 대상인데 **판정이 없다**(대개 dest 파일 부재 · template 를 읽지 못한
 * report 대상 포함). 행은 전수라 사라지지 않는다 — 그 행은 `diff_lines` 두 칸도 널이다.
 */
export type AdapterFile = {
  path: string;
  status: string | null;
  ownership?: string;
  harness?: string;
  mode?: "managed" | "report";
  template_rev?: string;
  diff_lines?: { added: number | null; removed: number | null };
  // 엔진은 기준 사본을 모르는 행에서 이 값을 널로 낸다(`pm_import.AdapterConfigReport`).
  diff_vs_baseline?: string | null;
  diff_vs_latest?: string;
  features?: AdapterFeature[];
  distance?: number;
};

/** 엔진 `status --json --with-adapters` 의 `adapters` 절 그대로. */
export type AdapterDocument = { source: string; files: AdapterFile[] };
/**
 * 브리지 `adapters(path)` 응답 — 같은 호출의 `conf` 절을 함께 싣는다. 소스가 해소된 호출에서만
 * `missing_for_latest` 가 배열이라(그 밖에는 null=모름), 다섯 번째 목록이 화면에 닿는 자리는
 * 이 응답 하나다.
 */
export type AdapterResponse = AdapterDocument & { conf: ConfSection };
export type AdapterMatrixDocument = Record<string, AdapterResponse | ErrorResponse>;
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
  status(path: string, withBoard: boolean, withAdapters: boolean, withRemote?: boolean):
    Promise<StatusDocument | ErrorResponse>;
  latest_tags(): Promise<TagsResponse | ErrorResponse>;
  pick_folder(): Promise<{ path: string | null } | ErrorResponse>;
  conf_keys(path: string): Promise<ConfKeysDocument | ErrorResponse>;
  conf_values(path: string): Promise<ConfValuesDocument | ErrorResponse>;
  /** 값 null 은 그 줄을 지운다(엔진 conf writer 의 삭제). */
  conf_save(path: string, updates: Record<string, string | null>):
    Promise<ConfSaveResult | ErrorResponse>;
  conf_reset(path: string, key: string): Promise<ConfSaveResult | ErrorResponse>;
  delegate_models(path: string, harness: string):
    Promise<DelegateModelsDocument | ErrorResponse>;
  adapters(path: string): Promise<AdapterResponse | ErrorResponse>;
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
  /** 사본의 `pm-config <argv…>` 한 번 — 허용 목록 밖·`--force` 는 서브프로세스 없이 거절된다. */
  run(path: string, argv: string[]): Promise<CommandResult | ErrorResponse>;
};

export function isError<T>(response: T | ErrorResponse): response is ErrorResponse {
  return typeof response === "object" && response !== null && "error" in response;
}

/** 서버가 보내는 알림 하나 — 저장·적용이 끝났다는 사실과 그 자리. */
export type ChangeEvent = { name: string; path: string };

/** 창이 자기를 밝히는 표식 — 창이 여는 주소의 질의 하나다(그 밖의 손님은 브라우저다). */
const CLIENT_QUERY = "client";
const CLIENT_HEADER = "X-Manager-Client";
const WINDOW_CLIENT = "window";
const BROWSER_CLIENT = "browser";

export function isWindowClient(): boolean {
  return new URLSearchParams(window.location.search).get(CLIENT_QUERY) === WINDOW_CLIENT;
}

/** 화면이 파이썬을 부르는 유일한 자리 — 창도 브라우저도 같은 서버의 손님이다. */
export function httpBridge(): Bridge {
  const call = async (name: string, ...args: unknown[]) => {
    const response = await fetch(`/api/${name}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        [CLIENT_HEADER]: isWindowClient() ? WINDOW_CLIENT : BROWSER_CLIENT,
      },
      body: JSON.stringify({ args }),
    });
    return response.json();
  };
  return {
    list_projects: () => call("list_projects") as Promise<ProjectsResponse | ErrorResponse>,
    add_project: (path) => call("add_project", path) as Promise<ProjectsResponse | ErrorResponse>,
    remove_project: (path) =>
      call("remove_project", path) as Promise<ProjectsResponse | ErrorResponse>,
    status: (path, withBoard, withAdapters, withRemote = false) =>
      call("status", path, withBoard, withAdapters, withRemote) as
        Promise<StatusDocument | ErrorResponse>,
    latest_tags: () => call("latest_tags") as Promise<TagsResponse | ErrorResponse>,
    pick_folder: () => call("pick_folder") as Promise<{ path: string | null } | ErrorResponse>,
    conf_keys: (path) => call("conf_keys", path) as Promise<ConfKeysDocument | ErrorResponse>,
    conf_values: (path) =>
      call("conf_values", path) as Promise<ConfValuesDocument | ErrorResponse>,
    conf_save: (path, updates) =>
      call("conf_save", path, updates) as Promise<ConfSaveResult | ErrorResponse>,
    conf_reset: (path, key) =>
      call("conf_reset", path, key) as Promise<ConfSaveResult | ErrorResponse>,
    delegate_models: (path, harness) =>
      call("delegate_models", path, harness) as Promise<DelegateModelsDocument | ErrorResponse>,
    adapters: (path) => call("adapters", path) as Promise<AdapterResponse | ErrorResponse>,
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
    run: (path, argv) => call("run", path, argv) as Promise<CommandResult | ErrorResponse>,
  };
}

/**
 * 서버 알림 구독 — 한쪽에서 저장·적용이 끝나면 다른 쪽이 다시 읽는다(스트림 하나).
 *
 * 되돌려 주는 함수를 부르면 구독이 끝난다.
 */
export function subscribeChanges(listener: (event: ChangeEvent) => void): () => void {
  const source = new EventSource("/events");
  source.onmessage = (message: MessageEvent) => listener(JSON.parse(message.data) as ChangeEvent);
  return () => source.close();
}

/**
 * 폴더 고르기 — 창은 OS 대화상자, 브라우저는 경로 직접 입력이다.
 *
 * 창에서만 되는 것을 브라우저에서 흉내 내지 않는다(서버도 창 아닌 손님의 `pick_folder` 를
 * 거절한다). 돌려주는 모양은 두 자리 모두 `pick_folder` 와 같다.
 */
export async function chooseFolder(bridge: Bridge):
    Promise<{ path: string | null } | ErrorResponse> {
  if (isWindowClient()) return bridge.pick_folder();
  const typed = window.prompt("프로젝트 폴더 경로")?.trim();
  return { path: typed ? typed : null };
}
