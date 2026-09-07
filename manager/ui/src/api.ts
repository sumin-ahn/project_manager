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
  conf: { missing_required: string[]; unknown: string[] };
  board: BoardSection | null;
  adapters: { source: string; files: { path: string; status?: string }[] } | null;
};

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
  };
}
