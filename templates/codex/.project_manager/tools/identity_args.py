#!/usr/bin/env python3
"""공용 정체성 인자 모듈 — `--repo`/`--slot` 파싱 + 리스(활성슬롯) 읽기·해소.

슬롯 정체성 인자를 받는 전 CLI(board·pm_config·pm_bootstrap·pm_handoff·ticket_finish·pm_relay·
worktree_pool)가 이 한 모듈로 수렴한다(단일 진실·DRY). 지금까지 도구마다 복붙됐던 리스 원장
읽기(`board._leased_sessions`·`pm_config._leased_sessions`·`pm_bootstrap._repo_slot_numbers`)와
슬롯 해소(`pm_bootstrap.SlotResolutionError` 규칙)를 여기 하나로 흡수한다
모듈 신설만 — 기존 도구의 로컬 리더 제거·교체는 채택 몫).

두 층으로 응집한다(같은 파일·별 함수):
  - **순수 인자 층**: `add_identity_args`·`parse_identity` — 파일 IO 0·부작용 0.
  - **리스 IO 층**: `leased_sessions`·`repo_slot_numbers`·`resolve_actor_slot` — 리스 장부
    (`worktree-leases.json`) 를 stdlib json 으로 직접 point-read 한다. `worktree_pool` 을
    import 하지 않는다(데이터 결합만, 모듈 결합 아님). **콜백 없음**
    이 모듈이 리스 읽기를 직접 소유한다.

해소 규칙:
  ```
  --repo X --slot N  → kind="slot"  · session="X_N"
  --repo X (슬롯 무) → kind="repo"  · session=None(repo-scope — view 전체 vs actor 활성슬롯
                        해소는 caller 몫. actor 는 resolve_actor_slot 을 별도 호출한다)
  --slot N (repo 무) → fail-loud(ValueError): "--slot 은 --repo 필수 — --repo <name> --slot <N>"
  (인자 전무)        → kind="none"
  ```
`parse_identity` 는 **discriminated** `Identity(kind, repo, slot, session)` 를 반환한다(PM 67
리뷰 A 수정) — 모호한 단일 문자열을 반환하지 않는다. caller 는 `kind` 로 명시 분기한다.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path


# ── 엔진 사본 rev 스탬프 (형제 사본 skew fail-loud) ──────────────────────
# baked 리터럴 — 이 값은 이 파일 코드 안에 고정된다(engine_rev.py 런타임 읽기 아님). 부분/수동
# 복사로 신 로더 + 구 형제가 섞이면 각자 새/옛 리터럴을 지녀 대조에서 skew 로 검출된다(런타임
# 공유-읽기였다면 같은 디렉토리 안 자기-일치라 미검출). 릴리즈 bump 는 `engine_rev.py --bump
# vX.Y.Z` 가 전 stamped 모듈 리터럴을 기계 일괄 재작성한다(사람 N곳 편집 0). 평시 회귀 가드
# (test_engine_rev_stamp)가 전 모듈 리터럴 == engine_rev.ENGINE_REV 를 강제한다.
ENGINE_REV = "v1.7.8"

# task pm_state 신규 세션 window의 단일 empty marker. state 생성(worktree_pool)과
# handoff 갱신(pm_handoff)이 같은 literal을 소비해야 하므로 공용 경계 모듈이 소유한다.
TASK_PM_STATE_EMPTY_MARKER = "  - (아직 완료된 task 세션 없음)"

_CODEX_HARNESS_SESSION_MARKERS: tuple[str, ...] = (
    "CODEX_THREAD_ID",
    "CODEX_CI",
)


def _runtime_skill_entry_prefix() -> str:
    """현재 실행 하네스의 스킬 진입 접두사. Codex env marker 외 경로는 slash다."""
    return "$" if any(os.environ.get(key) for key in _CODEX_HARNESS_SESSION_MARKERS) else "/"


def _runtime_skill_entry(skill: str) -> str:
    """사용자에게 제시할 현재 하네스의 PM 스킬 호출 표기."""
    return f"{_runtime_skill_entry_prefix()}{skill}"


class Identity:
    """`parse_identity` 의 discriminated 결과 — caller 는 `kind` 로 분기한다.

    - `kind="slot"`: `--repo X --slot N` 둘 다 지정 — repo=X·slot=N·session="X_N"(정체성 완전 해소).
    - `kind="repo"`: `--repo X` 만 지정(슬롯 무) — repo=X·slot=None·session=None(repo-scope).
      caller 가 view(그 repo 의 내 슬롯 전체) 인지 actor(활성슬롯 1개 해소) 인지 판단 —
      actor 라면 `resolve_actor_slot(repo, leases_file)` 를 별도 호출한다.
    - `kind="none"`: 정체성 인자 전무 — repo/slot/session 전부 None. no-flag 기본 해소
      의 env/lease/local.conf 유도 체인)는 이 모듈 범위 밖 — caller 가 그대로 이어간다.

    (평범한 클래스 — `@dataclass` 미사용: 엔진 도구는 `spec_from_file_location` 으로 로드되는데
    `from __future__ import annotations`(문자열 지연평가) 와 결합 시 모듈이 `sys.modules` 에
    등록 안 돼 있으면 dataclass 처리가 `AttributeError` 로 깨진다 — `worktree_pool.Lease`·
    `pm_relay` 의 동일 회피 관용구를 따른다.)
    """

    def __init__(self, kind: str, repo: str | None, slot: int | None, session: str | None,
                 task: str | None = None):
        self.kind = kind
        self.repo = repo
        self.slot = slot
        self.session = session
        # task 축 — `--task` 명시 시 그 task 이름(없으면 None). slot 축과
        # **직교**한다: task 는 `--repo --slot` 과 공존 가능(task 바인딩 + 슬롯 바인딩)하고, 단독
        # (`--task` 만)으로도 존재한다. 그래서 `kind`(slot/repo/none·repo/slot 축) 는 task 유무로
        # 바뀌지 않는다 — task 를 안 쓰는 caller 는 이 필드를 무시하면 현행과 동일.
        self.task = task

    def __repr__(self) -> str:
        return (f"Identity(kind={self.kind!r}, repo={self.repo!r}, "
                f"slot={self.slot!r}, session={self.session!r}, task={self.task!r})")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Identity):
            return NotImplemented
        return (self.kind, self.repo, self.slot, self.session, self.task) == (
            other.kind, other.repo, other.slot, other.session, other.task)


def add_identity_args(parser: argparse.ArgumentParser) -> None:
    """`--repo`·`--slot` 을 parser 에 추가 — 순수(파일 IO 0·부작용 0·canonical 인자).

    `--slot` 단독(``--repo`` 없이) 금지 규칙은 여기(add 시점)가 아니라 parse 후 `parse_identity`
    가 검사한다 — argparse 만으로는 "A 있으면 B 도 필수"를 표현할 수단이 마땅치 않고(양방향
    `required` 조합 불가), 에러 메시지도 전 도구가 동일해야 하므로(카드↔CLI 정합) 검사를
    한 곳(`parse_identity`)에 모은다.
    """
    parser.add_argument(
        "--repo", metavar="이름", default=None,
        help="repo 이름 — 슬롯 정체성 지정 (canonical). 단독이면 repo-scope, "
             "--slot 과 함께면 슬롯 정체성 <repo>_<N> 로 해소.",
    )
    parser.add_argument(
        "--slot", metavar="N", type=int, default=None,
        help="슬롯 번호 — --repo 필수(단독 사용 불가). 함께 주면 세션 <repo>_<N> 로 해소.",
    )
    parser.add_argument(
        "--task", metavar="이름", default=None,
        help="task 이름 — 작업 단위 정체성 축이며 정체성 해소 체인 "
             "전반이 소비한다. 포맷 자유(prefix 아님)·`<등록 repo>_<N>` 예약 패턴은 거부. "
             "repo/slot과의 조합 허용 여부는 소비 도구 계약을 따른다. pm_bootstrap/"
             "pm_handoff에서는 task 단독만 허용하고 보유 슬롯 집합을 자동 해소한다.",
    )


def parse_identity(args: argparse.Namespace) -> Identity:
    """parsed args(`.repo`·`.slot`)에서 discriminated `Identity` 로 해소한다.

    fail-loud(`ValueError`) 두 경우 — (1) `--slot` 있는데 `--repo` 없음, (2) `--slot < 1`(슬롯
    번호는 1부터·`work/<repo>_<N>` 정합). caller 가 `parser.error(str(exc))` 로 그대로 보인다
    (`pm_bootstrap.resolve_repo_arg` 동형). 그 외 세 경우(slot/repo/none)는 성공한다.

    slot≥1 검증은 `pm_bootstrap` 원 계약(pm_bootstrap.py:2935·`test_bootstrap_slot_below_one_rejected`
    게이트) 보존 — canonical 모듈이 여기서 빠뜨리면 채택시 회귀.
    """
    repo = getattr(args, "repo", None)
    slot = getattr(args, "slot", None)
    # task 축 — repo/slot 축과 직교. 여기선 값만 실어주고(kind 는
    # repo/slot 로 그대로 결정), `<등록 repo>_<N>` 예약 패턴 거부는 등록 repo 집합이 필요하므로
    # 순수 층 밖에서(`is_reserved_task_name`·caller 가 등록 repo 를 넘김) 검증한다(이 함수=파일 IO 0).
    task = getattr(args, "task", None)
    if slot is not None and repo is None:
        raise ValueError("--slot 은 --repo 필수 — --repo <name> --slot <N>")
    if slot is not None and slot < 1:
        raise ValueError("--slot 은 1 이상의 슬롯 번호여야 한다 (work/<repo>_<N>).")
    if repo is not None and slot is not None:
        return Identity(kind="slot", repo=repo, slot=slot, session=f"{repo}_{slot}", task=task)
    if repo is not None:
        return Identity(kind="repo", repo=repo, slot=None, session=None, task=task)
    return Identity(kind="none", repo=None, slot=None, session=None, task=task)


def is_reserved_task_name(name: str, registered_repos: "list[str] | set[str]") -> bool:
    """task 명이 `<등록 repo>_<N>` 슬롯 세션 패턴과 충돌하면 True — 예약(task 명 검증).

    순수 함수(파일 IO 0) — 등록 repo 집합을 caller 가 넘긴다(`parse_identity` 의 순수 층 규율
    보존·등록 repo 는 areas/leases 유래라 IO 층 밖). 등록된 각 repo `R` 에 대해 `^R_<정수>$` 를
    검사한다: task 이름이 그 형태면(예 `myproj_2`) 슬롯 세션 정체성(`<repo>_<N>`)과 시각적·기계적
    으로 충돌하므로 거부한다(task 는 자유 포맷이되 슬롯 세션 이름공간은 예약). **등록 repo
    집합에 없는** repo 로 시작하는 `_N` 형태는 무관(자유 포맷 허용) — 실재 슬롯과만 충돌 방지한다.
    """
    for repo in registered_repos:
        if re.match(rf"^{re.escape(repo)}_\d+$", name):
            return True
    return False


class InvalidTaskName(ValueError):
    """CLI 정체성 깔때기의 task 명 검증 실패 — 공백/괄호/path/선행 `.`/예약패턴 (fail-loud·게이트).

    `worktree_pool._validate_task_name`(엔진 bind 층)과 **동형 규칙·독립 구현**이다 — board 는
    worktree_pool 을 import 하지 않으므로(격리 관성·`is_reserved_task_name` 이 예약 정규식을
    이미 같은 근거로 mirror) CLI 층 공유 validator 를 여기 둔다. board 의 정체성 깔때기
    (`_actor_session_override`·`cmd_new`)가 이 **하나**를 소비해, 무검증 task 명이 `created_by`/
    `claimed_by`/lease-session 으로 영속되는 클래스를 소비 지점 전체에서 한 번에 닫는다. `ValueError`
    서브클래스라 caller 의 기존 `except ValueError`(parse_identity fail-loud 관례)가 그대로 잡는다.
    """

    def __init__(self, name: str, reason: str):
        self.name = name
        self.reason = reason
        super().__init__(f"부적합 task 명 {name!r} — {reason}")


def validate_task_name(name: str, registered_repos: "list[str] | set[str] | None" = None) -> None:
    """CLI 층 task 명 검증 — 위반 시 `InvalidTaskName`(fail-loud) (worktree_pool._validate_task_name 동형).

    task 명 영속 지점(`created_by`/`claimed_by`·lease session)이 무검증 값을 저장하지 못하게 board
    정체성 깔때기가 **부작용 이전** 소비하는 공유 validator. 문자 도메인은 하류 구문 표면(CLI 인자
    경계·relay slash·log 태그 delimiter·path)에 맞춘 협소화로 worktree_pool 엔진 validator 와 규칙이
    동형이다(모듈 격리라 독립 구현·`is_reserved_task_name` 재사용). 거부: 빈/공백-only·whitespace·괄호
    `(`/`)`·path separator(`/`·`\\`)·선행 `.`(traversal)·단일 컴포넌트 아님·(registered_repos 주면)
    `<repo>_<N>` 슬롯 세션 예약. 한글·하이픈·언더스코어·숫자는 통과(어느 표면과도 무충돌).
    """
    if not name or not name.strip():
        raise InvalidTaskName(name, "빈 이름(공백 포함)")
    if any(ch.isspace() for ch in name):
        raise InvalidTaskName(
            name, "공백·탭 등 whitespace 불가 (CLI/relay `--task <이름>` 인자 경계 파손 방지)")
    if "(" in name or ")" in name:
        raise InvalidTaskName(
            name, "괄호 `(`·`)` 불가 (log 헤더 태그 `(task:<이름>)` delimiter 파손 방지)")
    if "/" in name or "\\" in name:
        raise InvalidTaskName(name, "path separator(`/`·`\\`) 불가 — 단일 이름이어야")
    if name.startswith("."):
        raise InvalidTaskName(name, "선행 `.` 불가(숨김/`.`/`..` 상대경로 traversal 방지)")
    if Path(name).name != name:
        raise InvalidTaskName(name, "단일 path 컴포넌트가 아님")
    if registered_repos and is_reserved_task_name(name, registered_repos):
        raise InvalidTaskName(name, "슬롯 세션 예약 패턴(<repo>_<N>·⑥)")


# ── 엔진 중앙 로더 부트스트랩 (형제 로드는 이 한 경로만·`repo_owned_files.load_module`) ──
# 공유 읽기 seam 을 지연 로드하기 위해 필요하다([[T-0729]]) — 엔진 전체가 `spec_from_file_location`
# 을 중앙 로더 한 곳에서만 부르는 불변식(deep-import 가드)이라 여기서도 그 경로를 쓴다.
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
                    # 런타임에 만든 형제 모듈(중앙 로더 선복구가 방금 복사한 seam 등)을
                    # 이름으로 import 한다 — FileFinder 는 디렉터리 목록을 mtime 으로 캐시하고
                    # 인터프리터 시작 뒤 생긴 파일은 invalidate 없이는 인식이 보장되지 않는다
                    # (Python 문서 `importlib.invalidate_caches` · Windows 실측 간헐
                    # ModuleNotFoundError). 블록은 stdlib-only 라 지역 import 로 두되 sys.path 에
                    # parent 를 넣기 전에 가져와 그 트리의 동명 파일이 stdlib 를 가리지 않게 한다.
                    import importlib as _bootstrap_importlib
                    added = not sys.path or sys.path[0] != parent
                    if added:
                        sys.path.insert(0, parent)
                    try:
                        _bootstrap_importlib.invalidate_caches()
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

# REPO = 스크립트 위치 기반(cwd 무관) — board.py·worktree_pool.py·pm_config.py 와 동일
# 앵커 관례(어느 worktree cwd 에서 호출돼도 multi-PM 루트 .project_manager 를 자동 타깃).


# ── 공유 읽기 (원자 교체 대상 장부·[[T-0729]]) ────────────────────────────
# 이 모듈이 읽는 리스 장부(`worktree-leases.json`)는 `worktree_pool` 이 **원자 교체**하는
# 파일이다. 일반 `open` 리더가 하나라도 잡고 있으면 Windows 는 그 교체를 WinError 32 로 막으므로,
# 판독도 공용 seam 의 공유 읽기를 지난다.
#
# seam 로드는 **호출 시점 지연**이다 — board 가 이 모듈을 *import 시점*에 로드하므로 모듈 최상단
# 에서 형제를 끌어오면 board 의 import 경계가 형제 하나만큼 넓어진다. 그리고 이 모듈의 판독은
# "부재/손상 → None" 이 문서화된 fail-soft point-read 라, seam 을 못 쓰는 형상도 그 계약 안에서
# **종전 읽기로 강등**한다(등재 예외 · 조용하지 않게 프로세스당 한 번 사유를 남긴다). 여기서
# 올리면 실재하는 장부를 "읽을 수 없음" 으로 위장하게 된다.

_shared_read_degraded = False


def _warn_shared_read_degraded(cause: str) -> None:
    """강등 사유를 **프로세스당 한 번** 알린다 (판독마다 찍으면 진단이 자기 소음에 묻힌다)."""
    global _shared_read_degraded
    if _shared_read_degraded:
        return
    _shared_read_degraded = True
    print(
        f"경고: 공유 읽기 seam 을 쓸 수 없어 일반 읽기로 진행합니다 ({cause}) — Windows "
        "에서는 이 판독이 열려 있는 동안 리스 장부의 원자 교체가 실패할 수 있습니다. "
        "`pm-update` 로 .project_manager/tools/ 전체를 재동기하십시오.",
        file=sys.stderr,
    )


def _read_text_shared(path: Path, *, encoding: str) -> str:
    """리스 장부를 공유 읽기로 읽는다 — seam 을 못 쓰면 종전 읽기로 강등한다.

    이 모듈은 rev 검증자를 두지 않는다(board 가 import 시점에 로드하는 leaf point-reader 라
    형제 판정 계층을 갖지 않는 것이 설계다 — 그래서 중앙 로더에 `allow_unverified=True` 로
    묻는다). 구세대 사본은 함수 부재로 드러나므로 `getattr` 로 함께 받는다 — 쓰기 축 등재 예외와
    같은 규칙이다.
    """
    api = None
    try:
        module = _load_module_from_path(
            Path(__file__).resolve().with_name("file_lock.py"), "file_lock.py",
            allow_unverified=True, cache=True,
        )
        api = getattr(module, "read_text_shared", None)
        if api is None:
            _warn_shared_read_degraded("구세대 file_lock 사본에 read_text_shared 가 없음")
    except Exception as exc:  # noqa: BLE001 — 부재/손상 사본은 이 point-read 의 정상 입력이다.
        _warn_shared_read_degraded(f"{type(exc).__name__}: {exc}")
    if api is not None:
        return api(path, encoding=encoding)
    return path.read_text(encoding=encoding)


def task_prefix(name: str, leases_file: Path) -> str | None:
    """장부 top-level `tasks` 컬렉션에서 task `name` 의 board prefix 를 읽는다 — 없으면 None.

    `board.py new --task <이름>` 이 `--prefix` 명시가 없을 때 task 설정 prefix(`task prefix`
    로 설정·기본 None)를 참조한다. 순수 point-read(부작용 0·`worktree_pool` 미import·
    데이터 결합) — 부재/손상/미설정 → None(fail-soft·caller 가 유도 체인으로 폴백).
    """
    try:
        data = json.loads(_read_text_shared(leases_file, encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    tasks = data.get("tasks", [])
    if not isinstance(tasks, list):
        return None
    for t in tasks:
        if isinstance(t, dict) and t.get("name") == name:
            return t.get("prefix") or None
    return None


# ── 리스 IO 층 (worktree-leases.json 원장 point-read·격리 관성) ──────────


def _load_lease_rows(leases_file: Path) -> list[dict] | None:
    """`leases_file` 의 `leases` 배열을 dict 리스트로 읽는다 — `leased_sessions`·`repo_slot_numbers`
    가 공유하는 내부 IO 프리미티브(모듈 내 DRY).

    부재/JSON 깨짐/스키마 불일치(최상위 dict 아님·`leases` 가 list 아님) → `None`(fail-soft 신호 —
    "읽을 수 없음"). 두 공개 함수가 이 `None` 을 각자의 관례로 번역한다.
    """
    try:
        data = json.loads(_read_text_shared(leases_file, encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    rows = data.get("leases", [])
    if not isinstance(rows, list):
        return None
    return rows


def leased_sessions(leases_file: Path) -> list[str]:
    """lease 장부에서 `state=="leased"` 행들의 session 목록 (count-based 유도용).

    `board._leased_sessions`·`pm_config._leased_sessions` 흡수(동형·byte-for-byte 동작 보존) —
    `state` 가 **정확히 "leased"** 인 행만 센다(back-compat 기본값 없음 — 원 두 사본과 동형).
    장부 부재/파싱실패/손상 → 빈 리스트(fail-soft — 세션 해소가 장부 손상으로 죽지 않게). session
    이 빈/None 인 행은 제외.

    주의(의도된 비대칭): `repo_slot_numbers` 는 `state` 키 **부재**를 `"leased"` 로 back-compat
    처리한다(원 `pm_bootstrap._repo_slot_numbers` 동형) — 이 함수는 그러지 않는다(원 두 `_leased_
    sessions` 사본 동형).
    """
    rows = _load_lease_rows(leases_file)
    if rows is None:
        return []
    sessions: list[str] = []
    for row in rows:
        if isinstance(row, dict) and row.get("state") == "leased":
            sess = row.get("session")
            if sess:
                sessions.append(sess)
    return sessions


def lease_row_count(leases_file: Path) -> int | None:
    """lease 장부 `leases` 배열의 행 수 — **상태 무관**(leased/idle 모두 포함) · **부재와 손상을
    구분**(F-001 — 리뷰 라운드 03 must-fix 수정).

    `board.session_name` 의 단일-등록 유도 층이 "장부에 행이 하나도 없다"를 판별하는
    전용 술어 — `leased_sessions`(state=="leased" 만)와 달리 idle 행도 센다. 풀 형상(행 ≥1·전부
    idle 포함)에서 그 층이 발화하지 않게 하는 것이 목적이라 state 필터를 두지 않는다.

    반환:
      - `0` — 장부 파일이 **부재**(fresh 홈)이거나, **정상 파싱된** `leases` 배열의 길이가 0
        (구조적으로 확인된 빈 장부 — 유도 허용 대상).
      - 양의 정수 — 정상 파싱된 행 수(유도는 이미 상위 층에서 걸러짐 — 이 값 자체가 차단 신호).
      - `None` — 파일은 **존재하나** 읽기 실패(OSError)·JSON 파손·최상위/`leases` 스키마 불일치
        (**손상 — 행 수를 모른다**). "확인된 0행"과 "모름"을 접으면 실제로 풀 행을 보유한 홈도
        장부가 손상되기만 하면 `<repo>_1` 로 오해소된다(리뷰 라운드 03 재현: JSON
        `'{broken json'` 장부 → 구현 전 `0` → `session_name(required=True)` 가 `'solo_1'` 을
        내 `cmd_claim` 이 rc=0 로 오귀속 기록). 호출부(`single_registration_session`)는 `None`
        을 "행이 있는 것"과 동일하게 취급해 유도를 막는다 — 부재(exists 로 먼저 판별)와
        손상(존재하지만 읽기/파싱 실패)을 이 함수 안에서 구분해 그 신호를 그대로 전달한다.
    """
    if not leases_file.exists():
        return 0
    rows = _load_lease_rows(leases_file)
    if rows is None:
        return None
    return len(rows)


def single_registration_session(registered_repos: "set[str] | list[str]",
                                 leases_file: Path) -> str | None:
    """단일-등록 유도 — 세 모듈 공유 술어(F-002 — 리뷰 라운드 03 PM 재비준).

    `registered_repos`(caller 주입 — areas.md 등록 repo 집합)가 정확히 1개 && lease 장부가
    구조적으로 완전히 비어 있으면(`lease_row_count(leases_file) == 0` — 부재 또는 정상 파싱된
    빈 배열; **손상(None)은 제외** — F-001 의 부재/손상 구분을 그대로 공유해 손상 장부에서
    유도가 발화하지 않는다) `f"{repo}_1"`, 아니면 `None`.

    `board.session_name`·`worktree_pool._default_session`·`pm_config._default_session` 세
    모듈이 이 술어 하나를 공유해 우선순위를 통일한다(docstring 불변식 "세 모듈 같은 우선순위·
    tail 만 상이"). 격리 실측(리뷰 F-002)에서 세 모듈이 이 층 없이 따로 갈렸다 —
    `board.session_name()` 은 `<repo>_1` 을 유도해 bare claim 이 `claimed_by` 에 그 값을
    저장하는데, 이후 무명시 첫 worktree 생성(`worktree_pool.create_slot`/`alloc`)이
    `<host>-<pid>` 로 lease.session 을 저장하면 board 의 상위 단일-lease 층이 그 값으로 바뀌어
    방금 만든 claim 이 "mine" 필터에서 사라졌다. areas.md 파싱 자체는 이 모듈의 관심사가
    아니므로(파일 IO 순수성 — 이 모듈은 board.py 를 import 하지 않는다) `registered_repos`
    집합은 호출부가 이미 가진 수단(board.registered_repos()·worktree_pool/pm_config 의 동적
    board 로더)으로 산출해 주입한다(순환 import 회피).
    """
    if len(registered_repos) != 1:
        return None
    if lease_row_count(leases_file) != 0:
        return None
    repo = next(iter(registered_repos))
    return f"{repo}_1"


def repo_slot_numbers(repo: str, leases_file: Path) -> list[int] | None:
    """`leases_file` 장부에서 `repo` 의 **활성(leased)** worktree 슬롯 번호(`work/<repo>_<N>`→N).

    `pm_bootstrap._repo_slot_numbers` 흡수(동형·byte-for-byte 동작 보존). `state` 가 `"leased"`
    인 엔트리만 센다 — **`state` 키 부재는 `"leased"` 로 back-compat**(`worktree_pool.from_dict`
    default 동형·`leased_sessions` 와의 의도된 비대칭은 위 docstring 참고). 같은 슬롯 N 의 중복
    장부 엔트리는 dedup(정렬된 unique 목록).

    파일 부재/JSON 깨짐/스키마 불일치 → `None`(fail-soft·"읽을 수 없음"); 정상 read 인데 그 repo
    의 leased 슬롯이 0개면 빈 리스트 `[]`("읽었으나 활성 슬롯 없음"). 호출부는 두 경우를 구분한다
    (`resolve_actor_slot` 이 이 구분을 그대로 위임한다).
    """
    rows = _load_lease_rows(leases_file)
    if rows is None:
        return None
    slot_re = re.compile(rf"^work/{re.escape(repo)}_(\d+)$")
    slot_nums: set[int] = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("repo") != repo:
            continue
        # 활성(leased) 슬롯만 — idle(반납)은 죽은 세션이라 라우팅 대상 아님(codex must-fix 원 흡수).
        # state 키 부재는 leased 로 본다(worktree_pool from_dict default·back-compat).
        if row.get("state", "leased") != "leased":
            continue
        m = slot_re.match(str(row.get("slot") or ""))
        if m:
            slot_nums.add(int(m.group(1)))
    return sorted(slot_nums)


class SlotResolutionError(Exception):
    """`resolve_actor_slot` 이 활성 슬롯을 자동 해소할 수 없을 때(≥2 개·모호) — fail-loud.

    `--repo`-단독 actor 해소(claim/finish/handoff/regression/livegate) 전용 규칙(결정
    3) — 원 `pm_bootstrap.SlotResolutionError`(session-entry 용·default-1 규칙 포함)와 이름은
    같으나(의미 보존·단일 진실로 수렴 예정) 이 모듈의 판정은 더 단순하다: **1개면 해소·0개/≥2개는
    각각 None/raise** — default-1 규칙(slot 1 우선)은 여기 적용하지 않는다(그건 session-entry
    전용 관심사).
    """


def resolve_actor_slot(repo: str, leases_file: Path) -> str | None:
    """actor(`--repo`-단독) 슬롯 해소 — `repo` 의 활성(leased) 슬롯이 정확히 1개면 그 session.

    `--repo` 단독 인자에서 actor 연산(claim/finish/handoff/regression/livegate)
    은 활성 슬롯이 1개면 자동 해소하고, ≥2 개면 모호해 `SlotResolutionError`(fail-loud) — 기존
    SlotResolutionError 의미를 보존한다. 활성 슬롯이 0개(장부 부재/파싱실패/그 repo 활성 슬롯 없음)
    는 `None`(fail-soft) — actor 정체성이 미해소라는 신호일 뿐이며, `--session` 필요 여부(required)
    판단은 caller 몫이다(`board.session_name` 의 `required` 패턴과 동형).
    """
    slot_nums = repo_slot_numbers(repo, leases_file)
    if not slot_nums:
        return None
    if len(slot_nums) == 1:
        return f"{repo}_{slot_nums[0]}"
    raise SlotResolutionError(
        f"repo '{repo}' 활성 슬롯 {len(slot_nums)}개"
        f"({', '.join(f'work/{repo}_{n}' for n in slot_nums)}) 중 하나로 특정할 수 없다 — "
        f"`--slot <N>` 으로 명시하라."
    )


# ── 작업공간(slot) 2단 해소 — task-aware ──────────
# 실행 위치가 필요한 도구(regression run·livegate record·ticket_finish·dev-delegate)가
# **어느 worktree 에서 도는지**를 task 보유 슬롯 중에서 특정한다. 위 `resolve_actor_slot`
# (slot-mode `--repo` 단독·활성 lease 유일해소)와 판정 축이 다르다 — 이건 **task 축**(lease.session
# == task 이름)이라 "내 task 가 보유한 슬롯"을 본다. cwd 는 해소에 **비참여** —
# 순전히 리스 장부 + 명시 인자(`--repo`/`--slot`/`--task`)로만 판정한다.

_LEASE_ROLE_READONLY = "readonly"   # role="readonly" 공유 슬롯 — 무소유가 정상·소유검사 예외.


class Workspace:
    """작업공간 해소 결과 — 실행 위치(slot·절대경로 surface 소스) + 슬롯 메타.

    `resolve_task_workspace` 가 반환한다. `slot` = "work/<repo>_<N>"(worktree_pool.slot_path 와
    같은 상대형 — caller 가 `REPO / slot` 으로 **절대경로 surface**). `repo` = 그 repo. `session` =
    그 슬롯 lease.session(task-mode 는 task 이름·readonly 공유 슬롯은 무소유라 None 일 수 있음).
    `test_cmd` = 슬롯 바인딩 회귀명령(None=미바인딩·caller 가 다음 레이어 폴백). `readonly` =
    role="readonly" 공유 자산이라 소유검사를 우회했는지.

    (dataclass 미사용 — `Identity`·`worktree_pool.Lease` 와 동일: `spec_from_file_location` 로드 시
    `from __future__ import annotations` 결합으로 forward-ref 해소가 깨진다. 평범한 클래스로 회피.)
    """

    def __init__(self, slot: str, repo: str | None, session: str | None,
                 test_cmd: str | None = None, readonly: bool = False):
        self.slot = slot
        self.repo = repo
        self.session = session
        self.test_cmd = test_cmd
        self.readonly = readonly

    def __repr__(self) -> str:
        return (f"Workspace(slot={self.slot!r}, repo={self.repo!r}, session={self.session!r}, "
                f"test_cmd={self.test_cmd!r}, readonly={self.readonly!r})")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Workspace):
            return NotImplemented
        return (self.slot, self.repo, self.session, self.test_cmd, self.readonly) == (
            other.slot, other.repo, other.session, other.test_cmd, other.readonly)


class WorkspaceResolutionError(Exception):
    """작업공간 해소 실패 — 미보유·모호(≥2)·`--slot` 단독 등.

    같은 repo 슬롯 ≥2 를 허용하되 **유일=자동 해소·모호=에러**(첫번째/최근 암묵 선택
    금지·[[mechanize-dont-instruct-llm]]). caller(board·ticket_finish)가 자기 관례(`[중단]` 접두)로
    surface 한다. `SlotResolutionError`(slot-mode `--repo` 단독)와 **별개** — 이건 task-mode
    작업공간 전용이라 판정 축(내 task 보유)이 다르다.
    """


def _lease_dict_rows(leases_file: Path) -> list[dict]:
    """장부 `leases` 배열의 dict 행만 (해소 원천·조회 전용). 부재/손상 → 빈 리스트(fail-soft)."""
    rows = _load_lease_rows(leases_file)
    if rows is None:
        return []
    return [r for r in rows if isinstance(r, dict)]


def resolve_task_workspace(identity: Identity, leases_file: Path) -> Workspace:
    """2단 작업공간 해소 — task 가 보유한 슬롯 중 실행 위치를 특정한다.

    표:
      - `--repo X --slot N`(kind="slot") → 그 작업공간 — **내 task 보유 아니면 에러**. 단 그 슬롯이
        role="readonly" 공유 자산이면 소유검사 **비적용**(조회/참조 지칭 허용·무소유가
        정상).
      - `--repo X` 만(kind="repo") → 내 task 가 X 에서 보유한 게 유일하면 그것 / 0·≥2 는 **에러**.
      - 아무것도 없음(kind="none"·task 만) → 내 task 보유가 통틀어 유일하면 그것 / 0·≥2 는 **에러**.
      - `--slot N` 만 → `parse_identity` 가 이미 `ValueError`(여기 도달 전 — repo 없는 번호는 식별자 아님).

    슬롯↔task 연결 = `lease.session == task 이름`(`worktree_pool.slots_for_task` 정합) — leased
    (`state` 부재는 leased 로 봄·`worktree_pool.from_dict` default 동형). cwd 는 해소에 **참여하지
    않는다**. 모호·미보유는 `WorkspaceResolutionError`(fail-loud). `identity.task`
    가 있어야 한다(1단 귀속·caller 는 task-mode 에서만 호출).
    """
    task = identity.task
    rows = _lease_dict_rows(leases_file)
    # 이 task 가 보유한 leased 슬롯(session==task·state=leased·부재=leased·slots_for_task 정합).
    held = [r for r in rows
            if r.get("session") == task and r.get("state", "leased") == "leased"]

    if identity.kind == "slot":
        target = f"work/{identity.repo}_{identity.slot}"
        owned = next((r for r in held if r.get("slot") == target), None)
        if owned is not None:
            return Workspace(slot=target, repo=owned.get("repo") or identity.repo,
                             session=task, test_cmd=owned.get("test_cmd"))
        # readonly 공유 슬롯 carve-out — role="readonly" 자산은 무소유가 정상이라
        # 소유검사를 비적용하고 조회/참조 지칭을 허용한다(장부에 그 슬롯이 실재해야). 쓰기 조작
        # 거부는 readonly 거부 몫 — 여기선 소유검사 예외만 배선한다.
        ro = next((r for r in rows
                   if r.get("slot") == target and r.get("role") == _LEASE_ROLE_READONLY), None)
        if ro is not None:
            return Workspace(slot=target, repo=ro.get("repo") or identity.repo,
                             session=ro.get("session") or None,
                             test_cmd=ro.get("test_cmd"), readonly=True)
        raise WorkspaceResolutionError(
            f"작업공간 {target} 은 task {task!r} 보유가 아니다 — F6 소유검사 거부(⑦). 내 task 가 "
            f"보유한 슬롯을 `--repo/--slot` 으로 지칭하거나 `{_runtime_skill_entry('pm-env')} "
            f"alloc {identity.repo} --task "
            f"{task}` 로 대여하라 (readonly 공유 슬롯이면 조회 지칭은 허용)."
        )

    if identity.kind == "repo":
        in_repo = [r for r in held if r.get("repo") == identity.repo]
        if len(in_repo) == 1:
            r = in_repo[0]
            return Workspace(slot=r.get("slot"), repo=identity.repo,
                             session=task, test_cmd=r.get("test_cmd"))
        if not in_repo:
            raise WorkspaceResolutionError(
                f"task {task!r} 이(가) repo {identity.repo!r} 에서 보유한 작업공간이 없다 — "
                f"`{_runtime_skill_entry('pm-env')} alloc {identity.repo} --task {task}` 로 먼저 대여하라."
            )
        slots = ", ".join(sorted(r.get("slot") or "" for r in in_repo))
        raise WorkspaceResolutionError(
            f"task {task!r} 이(가) repo {identity.repo!r} 에서 {len(in_repo)}개 작업공간({slots})을 "
            f"보유 — 모호하다(⑦·암묵 선택 금지). `--slot <N>` 으로 번호를 명시하라."
        )

    # kind == "none" — 위치 인자 없음(task 만). 통틀어 유일해소 / 0·≥2 는 에러.
    if len(held) == 1:
        r = held[0]
        return Workspace(slot=r.get("slot"), repo=r.get("repo"),
                         session=task, test_cmd=r.get("test_cmd"))
    if not held:
        raise WorkspaceResolutionError(
            f"task {task!r} 이(가) 보유한 작업공간이 없다 — "
            f"`{_runtime_skill_entry('pm-env')} alloc <repo> --task {task}` "
            f"로 먼저 대여하라."
        )
    slots = ", ".join(sorted(r.get("slot") or "" for r in held))
    raise WorkspaceResolutionError(
        f"task {task!r} 이(가) {len(held)}개 작업공간({slots})을 보유 — 통틀어 모호하다(⑦). "
        "암묵 선택하지 않는다. 쓰지 않는 잉여 슬롯을 "
        f"`pm_config.py release <slot> --task {task}`로 반납한 뒤 다시 실행하라."
    )
