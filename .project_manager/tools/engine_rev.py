#!/usr/bin/env python3
"""엔진 사본 rev 스탬프 — 사본 skew fail-loud 의 단일 진실 값 + bump CLI.

엔진 도구들은 형제 모듈(`identity_args`·`worktree_pool`·`board`·`pm_handoff` …)을
`spec_from_file_location` 으로 동적 로드한다. 사본 skew(신 도구 + 구 형제 — 부분/수동
복사, 중단된 배포)가 생기면 신 도구가 구 형제의 *부재 속성*에 접근해 임의 지점의
AttributeError 로 폭발한다.

각 stamped 엔진 모듈은 자기 소스 코드 안에 `ENGINE_REV = "vX.Y.Z"`
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
import os
import re
import sys
from pathlib import Path

_TOOLS_BOOTSTRAP = os.path.dirname(os.path.abspath(__file__))
_TOOLS_BOOTSTRAP_FILE = os.path.realpath(
    os.path.join(_TOOLS_BOOTSTRAP, "repo_owned_files.py")
)
_TOOLS_BOOTSTRAP_KEY = f"_project_manager_repo_owned_files_bootstrap:{_TOOLS_BOOTSTRAP_FILE}"
_TOOLS_BOOTSTRAP_MODULE = sys.modules.get(_TOOLS_BOOTSTRAP_KEY)
_TOOLS_BOOTSTRAP_SENTINEL = object()
try:
    if (
        _TOOLS_BOOTSTRAP_MODULE is not None
        and os.path.realpath(getattr(_TOOLS_BOOTSTRAP_MODULE, "__file__", ""))
        != _TOOLS_BOOTSTRAP_FILE
    ):
        sys.modules.pop(_TOOLS_BOOTSTRAP_KEY, None)
        _TOOLS_BOOTSTRAP_MODULE = None
    if _TOOLS_BOOTSTRAP_MODULE is None:
        _TOOLS_BOOTSTRAP_PREVIOUS = sys.modules.pop(
            "repo_owned_files", _TOOLS_BOOTSTRAP_SENTINEL
        )
        _TOOLS_BOOTSTRAP_ADDED = not sys.path or sys.path[0] != _TOOLS_BOOTSTRAP
        if _TOOLS_BOOTSTRAP_ADDED:
            sys.path.insert(0, _TOOLS_BOOTSTRAP)
        try:
            import repo_owned_files as _TOOLS_BOOTSTRAP_MODULE
            if (
                os.path.realpath(getattr(_TOOLS_BOOTSTRAP_MODULE, "__file__", ""))
                != _TOOLS_BOOTSTRAP_FILE
            ):
                raise ImportError(
                    "repo_owned_files 형제 경로 불일치: "
                    f"{getattr(_TOOLS_BOOTSTRAP_MODULE, '__file__', None)!r}"
                )
            sys.modules[_TOOLS_BOOTSTRAP_KEY] = _TOOLS_BOOTSTRAP_MODULE
        finally:
            # 엔진 import bootstrap은 메인 스레드 전용이다. 그래도 위치를 가정한 pop(0)은
            # 피하고, 우리가 넣은 값이 남아 있을 때 그 값만 제거한다.
            if _TOOLS_BOOTSTRAP_ADDED:
                try:
                    sys.path.remove(_TOOLS_BOOTSTRAP)
                except ValueError:
                    pass
            if sys.modules.get("repo_owned_files") is _TOOLS_BOOTSTRAP_MODULE:
                sys.modules.pop("repo_owned_files", None)
            if _TOOLS_BOOTSTRAP_PREVIOUS is not _TOOLS_BOOTSTRAP_SENTINEL:
                sys.modules["repo_owned_files"] = _TOOLS_BOOTSTRAP_PREVIOUS
    _load_module_from_path = _TOOLS_BOOTSTRAP_MODULE.load_module
except Exception as _TOOLS_BOOTSTRAP_ERROR:
    if sys.modules.get(_TOOLS_BOOTSTRAP_KEY) is _TOOLS_BOOTSTRAP_MODULE:
        sys.modules.pop(_TOOLS_BOOTSTRAP_KEY, None)

    def _load_module_from_path(
        path,
        expected_filename,
        *,
        verifier=None,
        allow_unverified=False,
        cache=False,
        cache_key=None,
    ):
        """구형/손상 중앙 seam에서 복구 명령까지 띄우는 import-by-name 폴백."""
        target = os.path.realpath(os.fspath(path))
        if os.path.basename(target) != expected_filename:
            raise ValueError(
                f"module filename mismatch: expected {expected_filename!r}, "
                f"got {os.path.basename(target)!r}"
            )
        if verifier is not None and allow_unverified:
            raise ValueError("choose verifier or allow_unverified=True, not both")
        if verifier is None and not allow_unverified:
            raise ValueError(
                "module load requires verifier or explicit allow_unverified=True"
            )
        module_key = cache_key or f"_project_manager_legacy_loaded:{target}"
        module = sys.modules.get(module_key) if cache else None
        inserted = False
        try:
            if module is None:
                if (
                    target == _TOOLS_BOOTSTRAP_FILE
                    and _TOOLS_BOOTSTRAP_MODULE is not None
                ):
                    module = _TOOLS_BOOTSTRAP_MODULE
                else:
                    import_name = os.path.splitext(expected_filename)[0]
                    previous = sys.modules.pop(
                        import_name, _TOOLS_BOOTSTRAP_SENTINEL
                    )
                    parent = os.path.dirname(target)
                    added = not sys.path or sys.path[0] != parent
                    if added:
                        sys.path.insert(0, parent)
                    try:
                        module = __import__(import_name)
                        if os.path.realpath(getattr(module, "__file__", "")) != target:
                            raise ImportError(
                                f"{expected_filename} 형제 경로 불일치"
                            )
                    finally:
                        if added:
                            try:
                                sys.path.remove(parent)
                            except ValueError:
                                pass
                        if sys.modules.get(import_name) is module:
                            sys.modules.pop(import_name, None)
                        if previous is not _TOOLS_BOOTSTRAP_SENTINEL:
                            sys.modules[import_name] = previous
                if cache:
                    sys.modules[module_key] = module
                    inserted = True
            if verifier is not None:
                verifier(module, expected_filename)
            return module
        except Exception as exc:
            if cache and (inserted or sys.modules.get(module_key) is module):
                sys.modules.pop(module_key, None)
            if target == _TOOLS_BOOTSTRAP_FILE:
                raise RuntimeError(
                    f"엔진 공용 로더 {target}를 불러올 수 없음; "
                    "pm-update로 .project_manager/tools 전체를 재동기화하라."
                ) from exc
            raise


# 엔진 사본 rev — 릴리즈 버전과 함께 bump 하는 단일 진실 값 (`--bump` 이 이 값 + STAMPED_MODULES
# 리터럴을 일괄 재작성). 형식은 vX.Y.Z.
ENGINE_REV = "v1.6.2"

# 엔진 런타임 Python 하한의 단일 진실. pm_import 가 stdlib `tomllib` 를 직접 사용하므로
# Python 3.11 이 지배 제약이다(PEP 585 표기 자체의 3.9 하한보다 높음). board 탐지와
# Python 외부 파사드의 미러 리터럴은 이 값을 테스트로 대조해 skew 를 차단한다.
MIN_PYTHON = (3, 11)

# baked `ENGINE_REV` 리터럴을 지니는 엔진 모듈(= sibling skew 대조 대상). `--bump` 와 평시 가드
# 테스트가 이 목록을 참조한다. deep-import AST 가드는 중앙 loader 호출의 expected filename,
# verifier, 코드 소유 예외 및 각 ``_STAMPED_SIBLINGS`` gate를 이 목록과 대조한다. 따라서 새 형제
# 로드나 gate 누락, 미등록 미검증 호출이 생기면 회귀가 red가 된다.
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
    "repo_coordinates.py",
    "console_encoding.py",
    "pm_delegate.py",
    "delegate_scope.py",
    "pm_relay.py",
    "external_review.py",
    "gate_snapshot.py",
    "pm_render.py",
    "pm_import.py",
    "pm_log.py",
    "repo_owned_files.py",
    "file_lock.py",
)

# deep-import target/loader인데 의도적으로 baked stamp 또는 경계 검증에서 제외하는 복구 채널.
# AST 가드는 이 코드 소유 목록 외의 예외를 허용하지 않으며, 사유가 빈 문자열이어도 red다.
#
# engine_rev.py는 baked rev의 단일 진실 자체라 STAMPED_MODULES(그 값을 복제하는 대상)에 들어갈 수
# 없다. 또한 중단된 --bump를 재실행해야 하므로 main의 console helper 검증도 금지한다.
# pm_update.py는 부분 동기/skew를 실제로 해소하는 복구 채널이라 그 파일 자체가 로더인 경계와
# 다른 모듈이 pm_update.py를 target으로 로드하는 경계 모두 검증하지 않는다. 어느 방향에서든
# 복구 도구가 skew로 자기잠김되면 복구 경로가 닫힌다.
EXEMPT_FROM_STAMP = {
    "engine_rev.py": "baked rev 단일 진실 및 중단된 --bump 복구 채널",
    "pm_update.py": "self-update 복구 채널(자신이 로드하는 형제와 자신을 target으로 로드하는 경계 모두)",
}

# 호출자/채택자 경로를 받는 loader도 verifier 보유를 AST 가드가 단언한다. 이 예외 축은
# ``EXEMPT_FROM_STAMP``의 엔진 모듈/복구채널 축과 별개인 *adopter/dest 경로* 축이다. 따라서
# dest 사본의 리터럴 파일명이 우연히 stamped 형제와 같아도, 여기에 호출 지점과 사유를 등록하면
# canonical sibling 검증을 잘못 강요하지 않는다. 현재는 인스턴스 lint hook만 해당하며, 예외는
# 코드 소유·빈 사유 금지라 새 미검증 loader가 자동으로 섞이지 않는다.
EXEMPT_UNVERIFIED_DEEP_IMPORTS = {
    ("board.py", "_run_lint_hooks"): "채택자 소유 .project_manager/hooks/lint_*.py 확장점",
}

_REV_RE = re.compile(r"^v\d+\.\d+\.\d+$")
# 모듈-레벨 baked 리터럴 (줄 시작·문자열 리터럴). `--bump`/가드 스캔이 공유하는 단일 패턴.
_LITERAL_RE = re.compile(r'^ENGINE_REV = "([^"]*)"', re.MULTILINE)


class EngineRevSkew(RuntimeError):
    """엔진 사본 skew — 신 도구가 구/불일치 baked rev 의 형제 모듈을 로드 (fail-loud).

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


def load_repo_owned_files(path: Path, *, verifier=None, allow_unverified: bool = False):
    """세 소비자가 공유하는 ``repo_owned_files.py`` 캐시/로드 단일 seam.

    동일한 path-key 캐시는 검증 면제 복구 채널(pm_update)도 채울 수 있다. 따라서 캐시를 만든
    주체를 신뢰하지 않고 *매 소비 시점* 전달된 ``verifier``를 실행한다. 면제 채널만
    ``allow_unverified=True``를 명시할 수 있으며, 그 캐시는 이후 stamped 소비자가 다시 검증한다.
    exec/검증 실패 시 오염된 공용 캐시를 제거하고 원 예외를 그대로 보존한다.
    """
    path = Path(path).resolve()
    if verifier is None and not allow_unverified:
        raise ValueError("repo_owned_files loader는 verifier 또는 명시적 복구채널 면제가 필요하다")
    module_name = f"_project_manager_repo_owned_files:{path}"
    return _load_module_from_path(
        path,
        "repo_owned_files.py",
        verifier=verifier,
        allow_unverified=allow_unverified,
        cache=True,
        cache_key=module_name,
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
    _console_encoding = _load_module_from_path(
        Path(__file__).resolve().with_name("console_encoding.py"),
        "console_encoding.py",
        allow_unverified=True,
    )
    # 복구 도구는 skew 검증 금지: 중단된 bump(engine_rev만 신 rev)를 재실행해 나머지를 복구해야 한다.
    _console_encoding.configure_console_utf8()
    ap = argparse.ArgumentParser(
        description="엔진 rev 스탬프 bump — 전 stamped 모듈의 baked 리터럴을 기계 일괄 재작성.",
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
