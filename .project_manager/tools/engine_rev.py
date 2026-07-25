#!/usr/bin/env python3
"""엔진 사본 rev 스탬프 — 사본 skew fail-loud 의 단일 진실 값 + bump CLI (T-0397).

엔진 도구들은 형제 모듈(`identity_args`·`worktree_pool`·`board`·`pm_handoff` …)을
`spec_from_file_location` 으로 동적 로드한다. 사본 skew(신 도구 + 구 형제 — 부분/수동
복사, 중단된 배포)가 생기면 신 도구가 구 형제의 *부재 속성*에 접근해 임의 지점의
AttributeError 로 폭발한다(회사 실측 2026-07-20: `pm_handoff:identity.task`).

**설계(codex R2 재설계)**: 각 stamped 엔진 모듈은 자기 소스 코드 안에 `ENGINE_REV = "vX.Y.Z"`
**baked 리터럴**을 지닌다 — 이 파일을 *런타임에 읽지 않는다*. sibling 로더는 로드한 형제의
baked 리터럴을 자신의 baked 리터럴과 대조한다. 부분/수동 복사로 신 로더 + 구 형제가 섞이면
각자 새/옛 리터럴을 지녀 mismatch 로 검출된다 — 런타임 공유-읽기(모두 이 파일을 읽음)였다면
같은 디렉토리 안에서 항상 자기-일치라 *바로 그 부분복사 skew 를 구조적으로 미검출*했을 결함을
피한다.

이 파일의 `ENGINE_REV` 는 **bump 단일 진실 값**이다. `--bump vX.Y.Z` CLI 가 이 값 + 전
`STAMPED_MODULES` 의 baked 리터럴을 기계 일괄 재작성한다(사람 N곳 편집 금지 → "기계 1커맨드").
평시 회귀 가드(`tests/test_engine_rev_stamp.py`)가 전 모듈 리터럴 == 이 값을 강제해 bump 누락·
부분 편집을 즉시 red 로 세운다. 릴리즈 게이트(`tests/test_engine_rev_release.py`)는 이 값이
CHANGELOG 최신 릴리스 절과 일치하는지 본다.

거동 변경 0 — 정상(동기된) 사본에선 모든 리터럴이 일치해 아무 것도 바뀌지 않는다. skew 일
때만 cryptic AttributeError 대신 명시 에러로 표출한다(표출 개선·기능 불변).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


# 엔진 사본 rev — 릴리즈 버전과 함께 bump 하는 단일 진실 값 (`--bump` 이 이 값 + STAMPED_MODULES
# 리터럴을 일괄 재작성). 형식은 vX.Y.Z.
ENGINE_REV = "v1.4.3"

# baked `ENGINE_REV` 리터럴을 지니는 엔진 모듈(= sibling skew 대조 대상). `--bump` 와 평시 가드
# 테스트가 이 목록을 참조한다. pm_import 는 제외 — 자기 형제 canonical *source* 트리만 로드해
# skew 가 구조적으로 불가능(pm_import._detected_py 주석 참조).
STAMPED_MODULES = (
    "board.py",
    "pm_handoff.py",
    "ticket_finish.py",
    "worktree_pool.py",
    "pm_bootstrap.py",
    "pm_config.py",
    "identity_args.py",
    "domain.py",
    "contradiction_lint.py",
    "pm_adr.py",
)

_REV_RE = re.compile(r"^v\d+\.\d+\.\d+$")
# 모듈-레벨 baked 리터럴 (줄 시작·문자열 리터럴). `--bump`/가드 스캔이 공유하는 단일 패턴.
_LITERAL_RE = re.compile(r'^ENGINE_REV = "([^"]*)"', re.MULTILINE)


class EngineRevSkew(RuntimeError):
    """엔진 사본 skew — 신 도구가 구/불일치 baked rev 의 형제 모듈을 로드 (T-0397·fail-loud).

    `_engine_rev_skew` 마커 — fail-soft sibling 로더의 `except Exception` 이 이 예외(및 동형
    마커를 단 RuntimeError)를 **재-raise** 로 식별한다. 각 stamped 모듈은 self-contained 라
    (이 파일 런타임 의존 0) 자기 marked RuntimeError 를 내지만 같은 마커 계약을 공유한다.
    """

    _engine_rev_skew = True


def read_literal(module_path: Path) -> str | None:
    """모듈 소스에서 baked `ENGINE_REV` 리터럴을 읽는다 (없으면 None). 가드/도구 공용 스캐너."""
    m = _LITERAL_RE.search(module_path.read_text(encoding="utf-8"))
    return m.group(1) if m else None


def check(sibling_rev, expected_rev, *, sibling_filename: str, loader_filename: str) -> None:
    """형제 baked rev vs 로더 baked rev 대조 — canonical 비교기(모듈 inline verify 의 참조 구현).

    불일치/부재(구형 형제는 리터럴 부재=None)면 `EngineRevSkew`(fail-loud). 양쪽 다 *baked
    리터럴*이라 부분복사 skew 를 정확 검출한다(이 파일 런타임 읽기 아님)."""
    if sibling_rev != expected_rev:
        raise EngineRevSkew(
            f"엔진 사본 버전 불일치 — 로더 {loader_filename}(rev={expected_rev!r})가 "
            f"형제 {sibling_filename}(rev={sibling_rev!r})를 로드했다 (사본 skew: 부분/수동 복사 "
            f"또는 구형 사본). `pm-update`(또는 pm_update.py)로 .project_manager/tools/ "
            f"전체를 재동기하라."
        )


def bump(new_rev: str, *, dry_run: bool = False) -> list[str]:
    """이 파일의 `ENGINE_REV` + 전 STAMPED_MODULES 의 baked 리터럴을 `new_rev` 로 재작성한다.

    각 파일의 첫 모듈-레벨 `ENGINE_REV = "..."` 리터럴만 치환(멱등). 반환 = 실제로 바뀐(또는
    dry-run 시 바뀔) 파일명 목록. 형식(vX.Y.Z) 검증·스탬프 누락 파일은 SystemExit(fail-loud)."""
    if not _REV_RE.match(new_rev):
        raise SystemExit(f"[bump] 형식 오류 — vX.Y.Z 필요 (받음: {new_rev!r}).")
    tools = Path(__file__).resolve().parent
    changed: list[str] = []
    for filename in ("engine_rev.py", *STAMPED_MODULES):
        path = tools / filename
        text = path.read_text(encoding="utf-8")
        new_text, n = _LITERAL_RE.subn(f'ENGINE_REV = "{new_rev}"', text, count=1)
        if n == 0:
            raise SystemExit(
                f"[bump] {filename} 에 `ENGINE_REV = \"...\"` 리터럴이 없다 (스탬프 누락)."
            )
        if new_text != text:
            changed.append(filename)
            if not dry_run:
                path.write_text(new_text, encoding="utf-8")
    return changed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="엔진 rev 스탬프 bump — 전 stamped 모듈의 baked 리터럴을 기계 일괄 재작성 (T-0397).",
    )
    ap.add_argument("--bump", metavar="vX.Y.Z", required=True,
                    help="새 엔진 rev (engine_rev.py + 전 STAMPED_MODULES 리터럴 갱신).")
    ap.add_argument("--dry-run", action="store_true", help="변경 예정만 출력(미적용).")
    args = ap.parse_args(argv)
    changed = bump(args.bump, dry_run=args.dry_run)
    tag = "[dry-run] 변경 예정" if args.dry_run else "✓ 재작성"
    listing = ", ".join(changed) if changed else "(이미 최신 — 변경 없음)"
    print(f"{tag} rev={args.bump} — {len(changed)}개 파일: {listing}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
