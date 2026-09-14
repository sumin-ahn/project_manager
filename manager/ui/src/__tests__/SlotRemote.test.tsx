import { describe, expect, it } from "vitest";
import type { SlotRow } from "../api";
import { lineageLines, remoteLines } from "../components/SlotRemote";

const row = (patch: Partial<SlotRow>): SlotRow => ({
  slot: "work/app_1", repo: "app", state: "leased", session: "main", alive: true, role: "work",
  task: "main", branch: "task/x", head: "0af3782a1111111111111111111111111111111", parent: null,
  children: [], stashes: 0, dirty: false, remote: null, ...patch,
});

describe("슬롯 브랜치 관계", () => {
  const parent = { name: "main", fork: "e4f2436f55d79f70147d19fd65187f88fd3650be", ref: "origin/main",
                   ahead: 3, behind: 0 };

  it("부모 이름@갈라진 커밋과 앞뒤를 엔진이 센 그대로 말한다", () => {
    expect(lineageLines(row({ parent }))).toEqual(["부모 main@e4f2436f · 3 커밋 앞"]);
    expect(lineageLines(row({ parent: { ...parent, ahead: 0, behind: 2 } })))
      .toEqual(["부모 main@e4f2436f · 2 커밋 뒤"]);
  });

  it("부모 브랜치가 지워졌거나 기록이 없으면 그 사실을 말한다 — 값을 지어내지 않는다", () => {
    expect(lineageLines(row({ parent: { ...parent, ref: null, ahead: null, behind: null } })))
      .toEqual(["부모 main@e4f2436f · 부모 브랜치 없음"]);
    expect(lineageLines(row({}))).toEqual(["부모 기록 없음"]);
    expect(lineageLines(row({ branch: null }))).toEqual([]);   // 브랜치 없는 작업 슬롯
  });

  it("자식 브랜치와 stash 는 있을 때만 줄이 선다", () => {
    expect(lineageLines(row({ parent, children: ["task/y", "task/z"], stashes: 2 })))
      .toEqual(["부모 main@e4f2436f · 3 커밋 앞", "자식 브랜치 task/y, task/z", "stash 2개"]);
  });
});

describe("슬롯 원격 정보", () => {
  const value = { url: "git@example.invalid:app.git", upstream: null };
  const upstream = { name: "origin/task/x", head: "0af3782a1111111111111111111111111111111", ahead: 0, behind: 0 };

  it("origin 주소와 추적 원격 브랜치를 말한다", () => {
    expect(remoteLines(value, undefined)).toEqual(["origin git@example.invalid:app.git", "추적 원격 브랜치 없음"]);
    expect(remoteLines({ ...value, upstream: { ...upstream, ahead: 1 } }, undefined)[1])
      .toBe("추적 원격 브랜치 origin/task/x@0af3782a · 1 커밋 앞");
    expect(remoteLines({ ...value, upstream: { name: "origin/task/x", head: null, ahead: null, behind: null } },
                       undefined)[1])
      .toBe("추적 원격 브랜치 origin/task/x · 원격에 없음");
  });

  it("조회가 아직 없거나 실패하면 그 사실 한 줄이다", () => {
    expect(remoteLines(null, undefined)).toEqual(["원격 확인 전"]);
    expect(remoteLines(null, { error: { kind: "timeout", message: "git fetch origin 실패",
                                        stderr: "", rc: 1 } }))
      .toEqual(["원격 확인 실패 — git fetch origin 실패"]);
  });
});
