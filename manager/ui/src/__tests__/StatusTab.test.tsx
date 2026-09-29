import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Bridge, StatusDocument } from "../api";
import fixture from "../fixtures/status.sample.json";
import { ProjectRunContext } from "../lib/run";
import StatusTab, { recordLine } from "../tabs/StatusTab";

const document_ = fixture as unknown as StatusDocument;

describe("슬롯의 원격 대조", () => {
  it("슬롯 행 아래에 그 슬롯의 대조가 선다", () => {
    const slot = document_.slots[0];
    const value = { url: "git@example.invalid:app.git", upstream: null };
    const remote = { status: { ...document_, slots: document_.slots.map((row) => (
      row.slot === slot.slot ? { ...row, remote: value } : row)) } };
    const light = { ...document_, slots: document_.slots.map((row) => (
      row.slot === slot.slot ? { ...row, parent: { name: "main", fork: "e4f2436f55d79f70147d19fd65187f88fd3650be",
                                                   ref: "origin/main", ahead: 0, behind: 1 } } : row)) };
    renderTab({ light: { status: light }, heavy: undefined, remote });
    openShared();

    const cell = screen.getByTestId(`slot-remote-${slot.slot}`);
    expect(cell).toHaveTextContent("부모 main@e4f2436f · 1 커밋 뒤");
    expect(cell).toHaveTextContent("origin git@example.invalid:app.git");
    cleanup();
  });
});

/**
 * 실측 sidecar 원문 — 이 PM 홈의 `.project_manager/.local/{regression,livegate}.json`(경로만
 * 일반화). 픽스처 PM 홈에는 두 기록이 없어(null) 그 자리를 이 값으로 태운다.
 */
const REGRESSION = {
  head: "e4f2436f55d79f70147d19fd65187f88fd3650be", status: "pass", rc: 0, scope: "full",
  collected: 12882, floor: 7000, conf_anchor: "/projects/framework",
  ts: "2026-09-04T07:34:00+00:00",
  platforms: [{ name: "windows", command: "/projects/ops/win-qa", status: "pass", rc: 0 }],
};
const LIVEGATE = {
  head: "e4f2436f55d79f70147d19fd65187f88fd3650be", status: "pass", n: 22, rc: 0,
  ts: "2026-09-06T10:42:33+00:00", conf_anchor: "/projects/framework",
  platforms: [{ name: "windows", command: "/projects/ops/win-qa", status: "pass", rc: 0 }],
};

/** 기록 둘이 실린 보드 — 무거운 절이 그대로 나르는 값이다(화면이 만들지 않는다). */
function withRecords(): StatusDocument {
  return { ...document_,
           board: { ...document_.board!, regression: REGRESSION, livegate: LIVEGATE } };
}

/** 공용 탭으로 옮긴다 — 픽스처의 슬롯은 어느 task 도 쥐지 않아 전부 그 탭에 선다. */
function openShared() {
  fireEvent.mouseDown(screen.getByTestId("task-tab-shared"));
}

/** 행 동작이 사본을 부르는 자리 — 읽기 부분을 그리는 데도 컨텍스트가 있어야 한다(T-0913). */
function renderTab(props: Parameters<typeof StatusTab>[0]) {
  return render(
    <ProjectRunContext.Provider value={{
      bridge: { run: vi.fn(async () => ({ rc: 0, stdout: "", stderr: "" })) } as unknown as Bridge,
      path: "/projects/demo", status: document_, onChanged: vi.fn(),
    }}>
      <StatusTab {...props} />
    </ProjectRunContext.Provider>);
}

afterEach(cleanup);

describe("현황 탭 읽기 부분", () => {
  it("슬롯 행마다 엔진 값 그대로의 점을 보인다", () => {
    renderTab({ light: { status: document_ }, heavy: { status: document_ } });
    openShared();

    expect(screen.getByTestId("slot-dot-work/app_1")).toHaveAttribute("data-dot", "blue");
    expect(screen.getByTestId("slot-dot-work/app_2")).toHaveAttribute("data-dot", "orange");
    expect(screen.getByTestId("slot-dot-work/app_3")).toHaveAttribute("data-dot", "gray");
    expect(screen.getByTestId("slot-dot-.")).toHaveAttribute("data-dot", "orange");
    expect(screen.getByTestId("slot-.")).toHaveTextContent("작업 중");
  });

  it("보드 counts·lint 수는 무거운 절이 왔을 때만 보인다", () => {
    renderTab({ light: { status: document_ }, heavy: undefined });
    expect(screen.queryByTestId("board-counts")).toBeNull();
    expect(screen.getByText("선택 시 1회 조회")).toBeInTheDocument();
    cleanup();

    renderTab({ light: { status: document_ }, heavy: { status: document_ } });
    expect(screen.getByTestId("board-counts")).toHaveTextContent("open 3");
    expect(screen.getByTestId("board-counts")).toHaveTextContent("lint 22");
  });

  it("task 행은 열림 표시·prefix 를 그대로 보인다", () => {
    renderTab({ light: { status: document_ }, heavy: { status: document_ } });
    expect(screen.getByTestId("task-demo")).toHaveTextContent("닫힘");
    expect(screen.getByTestId("task-demo")).toHaveTextContent("prefix 없음");
    // 다른 task 는 그 탭을 골라야 나온다 — 한 화면에 남의 task 를 섞지 않는다.
    expect(screen.queryByTestId("task-live")).toBeNull();
    fireEvent.mouseDown(screen.getByTestId("task-tab-live"));
    expect(screen.getByTestId("task-live")).toHaveTextContent("작업 중");
  });

  it("열린 todo 를 ID·등록일과 함께 펼치고 줄마다 [완료]를 붙인다", () => {
    renderTab({ light: { status: document_ }, heavy: { status: document_ } });
    fireEvent.mouseDown(screen.getByTestId("task-tab-live"));

    expect(screen.getByTestId("todos-live"))
      .toHaveTextContent("#1 슬롯 쓰기 가드 실측 (등록 2026-09-16");
    expect(screen.getByTestId("todo-done-live-1")).toBeEnabled();
    // 열린 todo 가 0건이면 자리 자체가 없다(빈 목록을 위한 빈 칸을 만들지 않는다).
    fireEvent.mouseDown(screen.getByTestId("task-tab-demo"));
    expect(screen.queryByTestId("todos-demo")).toBeNull();
    fireEvent.mouseDown(screen.getByTestId("task-tab-live"));
    // 등록 버튼은 입력 전엔 비활성이고, 작업 중 task 라고 막지 않는다.
    expect(screen.getByTestId("todo-add-live")).toBeDisabled();
  });

  it("행마다 동작 자리가 하나씩 붙고 대여는 표 머리 하나다", () => {
    renderTab({ light: { status: document_ }, heavy: { status: document_ } });
    // task 탭에는 그 task 행 하나 + 그 task 가 쥔 슬롯만 있다(픽스처는 보유 0).
    expect(screen.getAllByTestId("row-actions")).toHaveLength(1);
    expect(screen.queryByTestId("alloc")).toBeNull();

    openShared();
    // 공용 탭에는 무소속 슬롯 전부 + [대여] 하나. 아이들이 둘이라도 대여는 repo 축 하나다.
    const shared = document_.slots.filter((row) => row.task == null);
    expect(screen.getAllByTestId("row-actions")).toHaveLength(shared.length);
    expect(screen.getAllByTestId("alloc")).toHaveLength(1);
    expect(screen.getByTestId("release-work/app_2")).toBeDisabled();   // 작업 중
    expect(screen.getByTestId("release-.")).toBeDisabled();            // 열린 기록
  });

  it("회귀·게이트는 사람이 읽는 한 줄이고 원문은 접혀 있다", () => {
    renderTab({ light: { status: document_ }, heavy: { status: withRecords() } });

    const regression = screen.getByTestId("record-regression");
    expect(regression).toHaveTextContent("회귀 pass · 12882개 수집 · 2026-09-04T07:34:00+00:00");
    expect(regression).not.toHaveAttribute("open");                   // 원문은 열어야 나온다
    // 게이트는 하한이 없는 축이라 수집 수를 `n` 에서 고른다(값 이름이 다르고 판정은 그대로다).
    expect(screen.getByTestId("record-livegate"))
      .toHaveTextContent("게이트 pass · 22개 수집 · 2026-09-06T10:42:33+00:00");
    // 접힌 상세 안에는 엔진 원문이 그대로 있다(요약이 지운 축을 잃지 않는다).
    expect(regression).toHaveTextContent("conf_anchor");
  });

  it("기록이 없으면 그 줄 자체가 없다 — 빈 값을 지어내지 않는다", () => {
    renderTab({ light: { status: document_ }, heavy: { status: document_ } });

    expect(document_.board!.regression).toBeNull();
    expect(screen.queryByTestId("record-regression")).toBeNull();
    expect(screen.queryByTestId("record-livegate")).toBeNull();
  });
});

describe("task 가 쥔 슬롯", () => {
  /**
   * 픽스처에는 task↔슬롯 연결이 없다(실 산출물 동등 고정이라 손대지 않는다) — 연결이 있는
   * 형상은 여기서 파생 문서로 만든다. 그 task 탭에는 그 task 의 슬롯만, 공용 탭에는 나머지만.
   */
  function linked(): StatusDocument {
    const [first, ...rest] = document_.slots;
    return { ...document_,
             tasks: document_.tasks.map((task) => (
               task.name === "live" ? { ...task, slots: [first.slot] } : task)),
             slots: [{ ...first, task: "live" }, ...rest] };
  }

  it("그 task 탭에는 그 task 가 쥔 슬롯만 선다", () => {
    renderTab({ light: { status: linked() }, heavy: { status: linked() } });
    fireEvent.mouseDown(screen.getByTestId("task-tab-live"));

    const held = document_.slots[0].slot;
    expect(screen.getByTestId(`slot-${held}`)).toBeInTheDocument();
    expect(screen.queryByTestId("slot-work/app_3")).toBeNull();
  });

  it("쥔 슬롯은 공용 탭에서 빠진다 — 같은 슬롯이 두 자리에 서지 않는다", () => {
    renderTab({ light: { status: linked() }, heavy: { status: linked() } });
    openShared();

    expect(screen.queryByTestId(`slot-${document_.slots[0].slot}`)).toBeNull();
    expect(screen.getByTestId("slot-work/app_3")).toBeInTheDocument();
  });

  it("한 task 가 여러 슬롯을 쥐면 전부 선다 — 개수도 그 수다", () => {
    const [one, two, ...rest] = document_.slots;
    const many: StatusDocument = { ...document_,
      tasks: document_.tasks.map((task) => (
        task.name === "live" ? { ...task, slots: [one.slot, two.slot] } : task)),
      slots: [{ ...one, task: "live" }, { ...two, task: "live" }, ...rest] };
    renderTab({ light: { status: many }, heavy: { status: many } });
    fireEvent.mouseDown(screen.getByTestId("task-tab-live"));

    expect(screen.getByTestId(`slot-${one.slot}`)).toBeInTheDocument();
    expect(screen.getByTestId(`slot-${two.slot}`)).toBeInTheDocument();
    expect(screen.getByTestId("held-count-live")).toHaveTextContent("쥔 슬롯 2개");
  });

  it("task 탭에서 그 task 명의로 슬롯을 더 대여한다 — repo 만 고른다", () => {
    renderTab({ light: { status: document_ }, heavy: { status: document_ } });

    // 공용 탭의 대여와 다른 자리다(그쪽은 task 도 골라야 한다).
    expect(screen.getByTestId("task-alloc-demo")).toBeDisabled();   // repo 를 고르기 전
    expect(screen.queryByTestId("alloc")).toBeNull();
  });

  it("쥔 슬롯이 없는 task 탭은 빈 표 대신 다음 할 일을 말한다", () => {
    renderTab({ light: { status: linked() }, heavy: { status: linked() } });

    expect(screen.getByText(/이 task 가 쥔 슬롯이 없습니다/)).toBeInTheDocument();
  });
});

describe("기계 기록 한 줄", () => {
  it("판정·수집 수·시각을 그 순서로 잇는다 — 넘긴 하한은 적지 않는다", () => {
    expect(recordLine(REGRESSION)).toBe("pass · 12882개 수집 · 2026-09-04T07:34:00+00:00");
    expect(recordLine(LIVEGATE)).toBe("pass · 22개 수집 · 2026-09-06T10:42:33+00:00");
  });

  it("수집 수가 하한에 못 미치면 그 사실을 적는다", () => {
    expect(recordLine({ ...REGRESSION, collected: 6999 }))
      .toBe("pass · 6999개 수집 (하한 7000 미달) · 2026-09-04T07:34:00+00:00");
    expect(recordLine({ ...REGRESSION, collected: 7000 }))
      .toBe("pass · 7000개 수집 · 2026-09-04T07:34:00+00:00");
  });

  it("없는 축은 자리를 비운다 — 하한 없이 남은 옛 회귀 기록", () => {
    expect(recordLine({ head: "abc", status: "pass", rc: 0, scope: "full",
                        ts: "2026-07-22T03:35:31+00:00" }))
      .toBe("pass · 2026-07-22T03:35:31+00:00");
  });
});
