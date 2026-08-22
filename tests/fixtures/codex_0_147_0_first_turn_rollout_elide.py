#!/usr/bin/env python3
"""T-0835 fixture 조달 재현 스크립트 (F-001 fix · code-reviewer R4 must-fix).

`codex_0_147_0_first_turn_rollout.jsonl`(첫 turn rollout 6레코드 픽스처)이 라이브 원본에서
어떤 변환으로 만들어졌는지를 **실행 가능한 코드**로 고정한다 — 손 재타이핑도, 검증 불가능한
scratchpad 산출물 참조도 없다.

입력: 격리 CODEX_HOME 라이브 프로브가 남긴 rollout JSONL 원본(새 thread 첫 `UserPromptSubmit`
시점 = 앞 6줄만 쓴다). 출력: 이 디렉터리의 픽스처와 바이트 동일한 elided JSONL.

변환 규칙(고정 · 판정 로직 아님·데이터 조달 규칙):
  1. `session_meta.payload.cwd`에서 읽은 작업 디렉터리 절대경로 문자열을 전 레코드에서
     `<work>`로 치환한다(개인 로컬 경로 제거).
  2. `session_meta.payload.base_instructions.text` — codex 기본 지시문 원문. 길이만 남기고
     `<codex 기본 지시문 원문 · elided · 원본 N자>`로 축약한다(원문은 공개 repo에 넣지 않는다
     · PM 비준 Q6).
  3. `response_item.payload.role == "developer"`인 메시지의 `content[0].text`(skills 지시문
     본문, 500자 초과분만) — 같은 규칙으로 축약.
  4. `world_state.payload.state.host_skills.body` — (3)과 같은 본문의 world_state 중복 — 같은
     규칙으로 축약.
  그 밖 모든 필드·bytes는 원본 그대로(구조가 "종류에 token_count가 없음"을 증언하므로 형태
  보존이 목적이다).

사용법:
    python3 codex_0_147_0_first_turn_rollout_elide.py <원본 rollout.jsonl> <출력 .jsonl>

stdout에 elide된 각 필드의 `path`·`original_length`·`original_sha256`을 JSON 한 줄로 낸다 —
이 값이 fixture metadata(`codex_0_147_0_live_hook_payloads.json`의 `first_turn_rollout_provenance`)
에 박제된 값과 같아야 재현이 확인된 것이다.
"""
from __future__ import annotations

import hashlib
import json
import sys

RECORD_COUNT = 6  # 첫 UserPromptSubmit 시점까지의 rollout 레코드 수 (T-0835 architect R1 §2).
ELIDE_MIN_LENGTH = 500  # 이보다 짧은 텍스트는 elide 하지 않는다(짧은 필드는 verbatim 유지).


def _digest(text: str) -> tuple[int, str]:
    return len(text), hashlib.sha256(text.encode("utf-8")).hexdigest()


def elide(records: list[dict]) -> tuple[list[dict], list[dict]]:
    """레코드 리스트를 elide 하고, (elide 된 레코드, 필드별 provenance 리스트)를 돌려준다."""
    work_path = None
    for record in records:
        if record.get("type") == "session_meta":
            work_path = record.get("payload", {}).get("cwd")
            break
    provenance = []

    def substitute(value):
        if work_path and isinstance(value, str):
            return value.replace(work_path, "<work>")
        if isinstance(value, list):
            return [substitute(v) for v in value]
        if isinstance(value, dict):
            return {k: substitute(v) for k, v in value.items()}
        return value

    out = [substitute(record) for record in records]

    for index, record in enumerate(out):
        record_type = record.get("type")
        payload = record.get("payload", {})
        if record_type == "session_meta":
            bi = payload.get("base_instructions")
            if isinstance(bi, dict) and isinstance(bi.get("text"), str):
                n, digest = _digest(bi["text"])
                provenance.append({"path": f"line{index}.payload.base_instructions.text",
                                   "original_length": n, "original_sha256": digest})
                bi["text"] = f"<codex 기본 지시문 원문 · elided · 원본 {n}자>"
        elif record_type == "response_item" and payload.get("role") == "developer":
            content = payload.get("content")
            if isinstance(content, list) and content and isinstance(content[0], dict):
                text = content[0].get("text")
                if isinstance(text, str) and len(text) > ELIDE_MIN_LENGTH:
                    n, digest = _digest(text)
                    provenance.append({"path": f"line{index}.payload.content[0].text",
                                       "original_length": n, "original_sha256": digest})
                    content[0]["text"] = f"<skills 지시문 본문 · elided · 원본 {n}자>"
        elif record_type == "world_state":
            host_skills = payload.get("state", {}).get("host_skills")
            if isinstance(host_skills, dict) and isinstance(host_skills.get("body"), str):
                n, digest = _digest(host_skills["body"])
                provenance.append({
                    "path": f"line{index}.payload.state.host_skills.body",
                    "original_length": n, "original_sha256": digest})
                host_skills["body"] = f"<skills 지시문 본문(world_state 중복) · elided · 원본 {n}자>"
    return out, provenance


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    source_path, out_path = argv[1], argv[2]
    with open(source_path, "r", encoding="utf-8") as handle:
        lines = handle.read().splitlines()[:RECORD_COUNT]
    records = [json.loads(line) for line in lines]
    elided, provenance = elide(records)
    with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(json.dumps(r, ensure_ascii=False) for r in elided) + "\n")
    for entry in provenance:
        print(json.dumps(entry, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
