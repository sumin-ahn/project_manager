/** 화면이 프로젝트 사본을 부르는 자리 — 브리지 `run` 하나와 그 상태. */
import { createContext, useCallback, useContext, useState } from "react";
import type { Bridge, CommandResult, ErrorResponse, StatusDocument } from "../api";

/**
 * 선택된 프로젝트를 부르는 데 필요한 전부.
 *
 * 현황 탭의 읽기 구조(행 렌더)를 고치지 않고 행 동작에 바깥을 주려고 컨텍스트로 내린다 —
 * 행 컴포넌트는 표가 넘겨 주는 `row` 하나만 받는다.
 */
export type ProjectRun = {
  bridge: Bridge;
  path: string;
  /** 가벼운 절의 마지막 값 — 표(repo·슬롯·하네스)와 task 목록의 출처다. */
  status: StatusDocument | undefined;
  /** 동작 직후 그 프로젝트의 가벼운 절 + 무거운 절을 다시 읽는다(낙관적 갱신 없음). */
  onChanged: () => void;
};

export const ProjectRunContext = createContext<ProjectRun | null>(null);

/** 컨텍스트가 없으면 멈춘다 — 빈 값으로 내려가면 버튼이 조용히 아무것도 안 하는 화면이 된다. */
export function useProjectRun(): ProjectRun {
  const value = useContext(ProjectRunContext);
  if (value === null) {
    throw new Error("프로젝트 실행 컨텍스트가 없다 — ProjectRunContext.Provider 안에서 그린다.");
  }
  return value;
}

export type CommandState = {
  outcome: CommandResult | ErrorResponse | null;
  busy: boolean;
  call: (argv: string[]) => Promise<void>;
};

/** 명령 한 번 — 마지막 산출 하나만 들고, 부른 뒤에는 언제나 그 프로젝트를 다시 읽는다. */
export function useCommand(): CommandState {
  const { bridge, path, onChanged } = useProjectRun();
  const [outcome, setOutcome] = useState<CommandResult | ErrorResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const call = useCallback(async (argv: string[]) => {
    setBusy(true);
    setOutcome(await bridge.run(path, argv));
    setBusy(false);
    onChanged();
  }, [bridge, path, onChanged]);
  return { outcome, busy, call };
}
