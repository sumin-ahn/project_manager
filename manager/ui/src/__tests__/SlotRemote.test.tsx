import { describe, expect, it } from "vitest";
import { remoteLines } from "../components/SlotRemote";

describe("슬롯의 원격 대조 세 줄", () => {
  const value = { url: "git@example.invalid:app.git", head_branch: "main",
                  head: "e4f2436f55d79f70147d19fd65187f88fd3650be", ahead: 0, behind: 0, upstream: null };
  const upstream = { name: "origin/task/x", head: "0af3782a1111111111111111111111111111111", ahead: 0, behind: 0 };

  it("origin 주소 · 추적 원격 브랜치 · 원격 HEAD 브랜치를 엔진이 센 그대로 말한다", () => {
    expect(remoteLines(value, undefined)).toEqual([
      "origin git@example.invalid:app.git",
      "추적 원격 브랜치 없음",
      "원격 HEAD origin/main@e4f2436f · 같음",
    ]);
    expect(remoteLines({ ...value, behind: 3 }, undefined)[2]).toContain("3 커밋 뒤");
    expect(remoteLines({ ...value, ahead: 2 }, undefined)[2]).toContain("2 커밋 앞");
    expect(remoteLines({ ...value, ahead: 2, behind: 3 }, undefined)[2]).toContain("갈라짐 (앞 2 · 뒤 3)");
  });

  it("추적 원격 브랜치는 이름@커밋과 앞뒤이고, 원격에서 지워졌으면 그 사실이다", () => {
    expect(remoteLines({ ...value, upstream: { ...upstream, ahead: 1 } }, undefined)[1])
      .toBe("추적 원격 브랜치 origin/task/x@0af3782a · 1 커밋 앞");
    expect(remoteLines({ ...value, upstream: { name: "origin/task/x", head: null, ahead: null, behind: null } },
                       undefined)[1])
      .toBe("추적 원격 브랜치 origin/task/x · 원격에 없음");
  });

  it("대조가 아직 없거나 실패하면 그 사실 한 줄이다 — 값을 지어내지 않는다", () => {
    expect(remoteLines(null, undefined)).toEqual(["원격 확인 전"]);
    expect(remoteLines(null, { error: { kind: "timeout", message: "git fetch origin 실패",
                                        stderr: "", rc: 1 } }))
      .toEqual(["원격 확인 실패 — git fetch origin 실패"]);
  });
});
