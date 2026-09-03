#!/usr/bin/env python3
"""공용 local.conf 로더 — 파싱 1벌 + 신키 레지스트리 + 구표기 판정.

`.project_manager/local.conf`(per-clone·git-ignored)를 읽는 엔진 구현이 도구마다 갈려 있었고
(9구현·7모듈), 같은 텍스트가 모듈마다 다른 dict 로 해소됐다(중복 키를 어떤 구현은 first-wins,
어떤 구현은 last-wins 로 읽었다). 이 모듈이 그 파싱 하나와 키 레지스트리 하나를 소유한다.

파싱 계약(이 모듈이 단일 진실 · 전 소비 지점 동일):
  - `KEY=value` 한 줄 · 앞뒤 공백은 걷는다.
  - `#` 로 시작하는 줄과 빈 줄은 무시 · `=` 없는 줄도 무시.
  - **값 안의 `#` 은 주석이 아니다**(inline 주석 없음 — 값 그대로 보존).
  - **중복 키는 last-wins** · 마지막 값이 빈 문자열이면 그 키는 `""`(설정 해제).
  - 파일 부재·`OSError`·`UnicodeError` → 빈 결과(fail-soft — conf 부재는 정상 형상이다).

판정은 파싱과 분리한다. `load()` 는 **raise 하지 않고**, 구표기 잔존 판정은 `assert_no_legacy()`
한 호출이다. 이유는 `pm_update` 의 apply 경로다 — 구표기 conf 를 가진 채택자도 새 엔진 파일은
받아야 안내대로 고칠 수 있다(막으면 고칠 수단 없이 갇힌다). 그래서 apply 경로만 판정을 부르지
않고, 값을 **실제로 소비하는** 지점이 부른다.

`ENGINE_REV` 는 baked 리터럴이다(형제 사본 skew fail-loud · `engine_rev.py --bump` 대상).
stdlib-only 이며 형제는 `file_lock`(공유 읽기 seam) 하나만 경로-앵커로 지연 로드한다.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path


# ── 엔진 사본 rev 스탬프 (형제 사본 skew fail-loud) ──────────────────────
# baked 리터럴 — `identity_args.py`·`file_lock.py` 와 같은 규약이다. 릴리즈 bump 는
# `engine_rev.py --bump vX.Y.Z` 가 전 stamped 모듈을 기계 일괄 재작성한다.
ENGINE_REV = "v1.7.13"
# ── 엔진 중앙 로더 부트스트랩 (형제 로드는 이 한 경로만·`repo_owned_files.load_module`) ──
# 공유 읽기 seam 을 지연 로드하기 위해 필요하다 — 엔진 전체가 `spec_from_file_location`
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



# ── 공유 읽기 (원자 교체 대상 conf 의 판독) ──────────────────────────────
# `local.conf` 는 키 갱신이 **원자 교체**로 이뤄지는 파일이다(`pm_import._write_conf_keys`).
# 일반 `open` 리더가 하나라도 잡고 있으면 Windows 는 그 교체를 WinError 32 로 막으므로 판독도
# 공용 seam 의 공유 읽기를 지난다.
#
# seam 로드는 **호출 시점 지연**이고 실패는 종전 읽기로 강등한다(프로세스당 한 번 사유 출력) —
# `identity_args._read_text_shared` 와 같은 규약이다. 이 모듈은 복구 채널(`pm_import`)도 쓰는
# 판독 leaf 라, 형제 사본이 구세대인 트리에서 conf 판독 자체가 죽으면 자기 자신을 못 고친다.

_shared_read_degraded = False


def _warn_shared_read_degraded(cause: str) -> None:
    """강등 사유를 **프로세스당 한 번** 알린다 (판독마다 찍으면 진단이 자기 소음에 묻힌다)."""
    global _shared_read_degraded
    if _shared_read_degraded:
        return
    _shared_read_degraded = True
    print(
        f"경고: 공유 읽기 seam 을 쓸 수 없어 일반 읽기로 진행합니다 ({cause}) — Windows "
        "에서는 이 판독이 열려 있는 동안 local.conf 의 원자 교체가 실패할 수 있습니다. "
        "`pm_update.py` 로 .project_manager/tools/ 전체를 재동기하십시오.",
        file=sys.stderr,
    )


def _read_text_shared(path: Path, *, encoding: str) -> str:
    """conf 를 공유 읽기로 읽는다 — seam 을 못 쓰면 종전 읽기로 강등한다.

    `identity_args` 와 같이 rev 검증자를 두지 않는다(leaf point-reader). 구세대 사본은 함수
    부재로 드러나므로 `getattr` 로 함께 받는다.
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
    except Exception as exc:  # noqa: BLE001 — 부재/손상 사본은 이 판독의 정상 입력이다.
        _warn_shared_read_degraded(f"{type(exc).__name__}: {exc}")
    if api is not None:
        return api(path, encoding=encoding)
    return path.read_text(encoding=encoding)


# ── 전수 매핑표 (구표기 → 신표기) ────────────────────────────────────────
# **이 표가 이주 안내와 fail-loud 판정의 단일 입력이다.** 값이 `None` 이면 대체 키 없이 제거된
# 키이고, 그 밖은 "이 키를 저 이름으로 옮겨라" 다. 테스트가 이 표를 양방향으로 소비한다 —
# 표에 있는 구키가 코드에 남아 있으면 red, 코드가 읽는 신키가 표/레지스트리에 없으면 red.
LEGACY_KEY_MAP: dict[str, str | None] = {
    # 위임 축
    "delegate_enabled": "delegate.enabled",
    "delegate_timeout": "delegate.timeout",
    "delegate_idle_timeout": "delegate.idle_timeout",
    # 제거 — 추가 리뷰어 채널 스위치 축 자체가 없어졌다(대체 키 없음). 추가 리뷰어는
    # developer·architect 와 같이 부르면 도는 역할이라 켜고 끄는 키가 없다. 남은 행은 지운다.
    "additional_reviewer.enabled": None,
    "additional_reviewer_enabled": None,
    "external_review_enabled": None,
    # 제거 — 호출 횟수만 세던 판정 라운드 상한 축 자체가 없어졌다(대체 키 없음·수렴 축이 같은
    # 범위를 본다: `additional_reviewer.rounds_max`)
    "additional_reviewer_round_limit": None,
    "external_review_round_limit": None,
    "external_review_incomplete_round_limit":
        "additional_reviewer.incomplete_rounds_max",
    "additional_reviewer_incomplete_round_limit":
        "additional_reviewer.incomplete_rounds_max",
    "external_review_wave_budget": "additional_reviewer.wave_budget",
    "additional_reviewer_wave_budget": "additional_reviewer.wave_budget",
    "review_rounds_max": "additional_reviewer.rounds_max",
    "review_paths": "additional_reviewer.paths",
    # 제거 — 리뷰 내용에서 경로를 빼던 필터 축이 없어졌다(대체 키 없음)
    "review_denylist_extra": None,
    "external_review_timeout": "additional_reviewer.timeout",
    "external_review_idle_timeout": "additional_reviewer.idle_timeout",
    "external_review_progress_signal": "additional_reviewer.progress_signal",
    # 제거 — 리뷰어 전용 env allowlist·임시 홈 축이 없어졌다(대체 키 없음: 리뷰어 실행 조건은
    # 위임 채널과 같은 seam 이 소유한다)
    "reviewer_env_keep_extra": None,
    "reviewer_home_artifacts_extra": None,
    # 제거 — 모델을 고정하지 않는 legacy 실행 경로 폐지(대체 없음·구조화 tuple 이 정본)
    "reviewer_cmd": None,
    # 하네스 축
    "opencode_pro_model": "harness.opencode.pro_model",
    # ctx 임계
    "ctx_nudge_pct": "ctx.nudge_pct",
    "ctx_stop_pct": "ctx.stop_pct",
    "ctx_window_tokens": "ctx.window_tokens",
    # 회귀 가드
    "regression_min_collected": "regression.min_collected",
    # upstream 좌표
    "upstream": "upstream.path",
    "upstream_rev": "upstream.rev",
    "upstream_seen_rev": "upstream.seen_rev",
    # operational (렌더 토큰 재유도)
    "project_name": "project.name",
    "project_tagline": "project.tagline",
    "project_root": "project.root",
    "date": "project.date",
    "py": "runtime.py",
    "test_cmd": "test.cmd",
    # 정체성
    "user": "identity.user",
    # 제거 — slot·task 종속 값은 프로젝트 공용 conf 의 자리가 아니다. 세션의 진실은 lease
    # 장부, prefix 의 진실은 areas.md 칼럼이다.
    "session": None,
    "prefix": None,
}

# **안내만 하고 막지는 않는 구키** — 읽는 코드가 이미 0인 키다(폴백이 먼저 폐지됐다).
# 값을 공급하지 않으므로 잔존해도 조용히 강등되는 축이 없고(불변식 6 의 대상이 아니다), 여기서
# 막으면 무해한 잔존 한 줄이 기존 채택자의 전 명령을 세운다. 교체 안내는 이 키들도 지목한다.
NON_BLOCKING_LEGACY_KEYS: frozenset[str] = frozenset({"session", "prefix"})

# suffix per-harness 표기(`ctx_window_tokens_<harness>`)는 접두 패턴으로 판정한다 — 구표기는
# 하네스명이 f-string 으로 붙는 **열린 집합**이었고, 신표기 `harness.<name>.ctx_window_tokens`
# 로 옮기면 `harness.<name>.*` 패턴에 흡수돼 닫힌 집합이 된다.
LEGACY_SUFFIX_PREFIX = "ctx_window_tokens_"


# ── 어댑터 파서용 데이터 내보내기 (엔진 미import 파서 4개 + 셸 훅) ────────
# 어댑터(claude ctx_guard · codex/opencode driver · opencode JS core · run-tests 훅)는 엔진을
# import 하지 않는 독립 파서라 이 표를 **손으로 복제하면** 표와 파서가 갈린다. 그래서 이 모듈이
# 블록 텍스트를 **생성**하고, 각 파서는 그 산출을 그대로 품는다(동일성은 회귀가 값으로 단언).
# 재생성: `python3 local_conf.py --render-adapter-block <python|js|sh>`.
ADAPTER_BLOCK_BEGIN = "생성 시작 — 차단 구키 (local_conf.render_adapter_block · 손편집 금지)"
ADAPTER_BLOCK_END = "생성 끝 — 차단 구키"


def blocking_legacy_key_names() -> tuple[str, ...]:
    """차단 구키 이름 전수 — 생성 블록의 단일 입력(안내 전용 키는 뺀다)."""
    return tuple(sorted(key for key in LEGACY_KEY_MAP
                        if key not in NON_BLOCKING_LEGACY_KEYS))


def render_adapter_block(style: str) -> str:
    """어댑터 파서가 품는 차단 구키 선언 블록을 그 언어 문법으로 낸다(마커 포함)."""
    names = blocking_legacy_key_names()
    if style == "python":
        body = "\n".join(f'    "{name}",' for name in names)
        return (f"# {ADAPTER_BLOCK_BEGIN}\n"
                f"LEGACY_CONF_KEYS = (\n{body}\n)\n"
                f'LEGACY_CONF_KEY_PREFIX = "{LEGACY_SUFFIX_PREFIX}"\n'
                f"# {ADAPTER_BLOCK_END}")
    if style == "js":
        body = "\n".join(f'  "{name}",' for name in names)
        return (f"// {ADAPTER_BLOCK_BEGIN}\n"
                f"const LEGACY_CONF_KEYS = [\n{body}\n];\n"
                f'const LEGACY_CONF_KEY_PREFIX = "{LEGACY_SUFFIX_PREFIX}";\n'
                f"// {ADAPTER_BLOCK_END}")
    raise ValueError(f"모르는 생성 스타일: {style}")


def adapter_stop_message(path, found) -> str:
    """어댑터 파서가 멈출 때 내는 문구 — 엔진 안내와 같은 사실을 짧게 말한다.

    어댑터는 매핑표를 들지 않으므로 신표기 이름을 말하지 못한다. 대신 **무엇이 걸렸는지**와
    전수 지목을 어디서 받는지를 말한다(엔진 도구가 그 표의 소유자다)."""
    return (f"오류: local.conf 에 구표기 키가 남아 있습니다 ({path}) — "
            f"{', '.join(found)}. 값이 조용히 기본값으로 떨어지지 않도록 여기서 멈춥니다. "
            "전수 지목은 `board.py lint` 또는 `pm_update.py` 안내가 냅니다.")


def legacy_replacement(key: str) -> str | None:
    """구표기 `key` 의 신표기 (대체 없이 제거된 키·구표기가 아닌 키는 None)."""
    if key in LEGACY_KEY_MAP:
        return LEGACY_KEY_MAP[key]
    if key.startswith(LEGACY_SUFFIX_PREFIX):
        harness = key[len(LEGACY_SUFFIX_PREFIX):].strip()
        if harness:
            return f"harness.{harness}.ctx_window_tokens"
    return None


def is_legacy_key(key: str) -> bool:
    """이 키가 이번 표기 통일로 **사라진 이름**인가 (대체 유무 무관)."""
    return key in LEGACY_KEY_MAP or (
        key.startswith(LEGACY_SUFFIX_PREFIX) and len(key) > len(LEGACY_SUFFIX_PREFIX))


# ── 신키 레지스트리 ──────────────────────────────────────────────────────
# 축 없는 단일값도 축을 만든다(`test.cmd`·`runtime.py`) — "이 키는 왜 다르지" 가 생기지 않게
# 예외를 두지 않는다. 세그먼트 구분자만 점이고, 세그먼트 안의 철자는 그 식별자의 정본을 따른다
# (역할은 `pm_delegate.ROLE_CHOICES` 표기 `code-reviewer`, 속성은 snake_case `idle_timeout`).
KNOWN_KEYS: tuple[str, ...] = (
    "delegate.enabled",
    "delegate.timeout",
    "delegate.idle_timeout",
    # 내부 code-reviewer 수렴 상한 — 역할 한 곳에만 있는 고정 키라 패턴이 아니라 실명으로 둔다
    # (`pm_delegate.INTERNAL_REVIEW_ROUNDS_MAX_KEY` 와 글자 단위로 같다·회귀가 대조한다).
    "delegate.code-reviewer.rounds_max",
    "additional_reviewer.harness",
    "additional_reviewer.model",
    "additional_reviewer.reasoning",
    "additional_reviewer.incomplete_rounds_max",
    "additional_reviewer.wave_budget",
    "additional_reviewer.rounds_max",
    "additional_reviewer.paths",
    "additional_reviewer.timeout",
    "additional_reviewer.idle_timeout",
    "additional_reviewer.progress_signal",
    "ctx.nudge_pct",
    "ctx.stop_pct",
    "ctx.window_tokens",
    "qa.platforms",
    "regression.min_collected",
    "upstream.path",
    "upstream.rev",
    "upstream.seen_rev",
    "project.name",
    "project.tagline",
    "project.root",
    "project.date",
    "runtime.py",
    "test.cmd",
    "identity.user",
)

# 패턴 키군 — `<name>` 자리에 들어갈 목록은 **코드가 소유**한다(손열거 0). 전개는 형제 모듈의
# 선언에서 파생하고, 형제를 못 읽는 형상(부분 사본·복구 채널)에서는 모양 판정으로 강등한다 —
# 이 판정의 유일한 소비자가 never-block advisory(모르는 키 경고)라 강등이 무엇도 막지 않는다.
_ROLE_SUFFIXES: tuple[str, ...] = ("harness", "model", "reasoning")
_HARNESS_SUFFIXES: tuple[str, ...] = (
    "idle_timeout", "wall_timeout", "ctx_window_tokens", "pro_model",
)
_PLATFORM_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}\Z")


class ConfResult:
    """`load()` 의 반환 — 파싱값 + 구표기 잔존 + (지연 계산) 모르는 키.

    (평범한 클래스 — `@dataclass` 미사용: 엔진 도구는 `spec_from_file_location` 으로 로드되는데
    `from __future__ import annotations` 와 결합 시 모듈이 `sys.modules` 에 등록 안 돼 있으면
    dataclass 처리가 깨진다 — `identity_args.Identity` 와 같은 회피 관용구다.)

    `unknown` 은 **속성 접근 시점에 계산**한다. 레지스트리 패턴 전개가 형제 모듈 선언을 읽는데,
    파싱마다 그 로드를 강제하면 conf 를 읽는 모든 지점이 형제 하나만큼 무거워지고 로드 순환의
    입구가 된다(형제도 conf 를 읽는다).
    """

    __slots__ = ("values", "legacy", "path")

    def __init__(self, values: dict[str, str], legacy: dict[str, str | None],
                 path: Path | None = None) -> None:
        self.values = values
        self.legacy = legacy
        self.path = path

    @property
    def unknown(self) -> tuple[str, ...]:
        """레지스트리에 없고 구표기도 아닌 키 (advisory 입력 — 오타 조기 발견)."""
        return unknown_keys(self.values)


class LegacyConfKeyError(RuntimeError):
    """구표기 키가 남은 conf 를 **값 소비 지점**이 만났다(조용한 기본값 강등 차단)."""

    # fail-soft 해소 경로가 이 예외를 **import 없이** 식별하는 표식이다(`_engine_rev_skew` 와
    # 같은 규약) — 중앙 로더가 멈춘 것을 호출부가 일반 해소 실패로 삼키면 fail-loud 가 무효가 된다.
    _legacy_conf_key = True

    def __init__(self, legacy: dict[str, str | None], path: Path | None = None) -> None:
        self.legacy = dict(legacy)
        self.path = path
        super().__init__(legacy_error_message(legacy, path))


def parse(text: str) -> dict[str, str]:
    """local.conf 텍스트 → `{키: 값}` (파싱 계약은 모듈 docstring)."""
    conf: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        conf[key.strip()] = value.strip()
    return conf


def legacy_keys(values: dict[str, str]) -> dict[str, str | None]:
    """`values` 안의 구표기 키 → 신표기(대체 없으면 None) — 선언 순서 보존."""
    return {key: legacy_replacement(key) for key in values if is_legacy_key(key)}


def load(path: Path | str) -> ConfResult:
    """`path` 의 local.conf 를 읽어 `ConfResult` 로 돌려준다 — **raise 하지 않는다**.

    부재·`OSError`·`UnicodeError` 는 빈 결과다(conf 없는 트리는 정상 형상). 구표기 잔존 판정은
    호출부가 `assert_no_legacy` 로 명시한다.
    """
    conf_path = Path(path)
    try:
        text = _read_text_shared(conf_path, encoding="utf-8")
    except (OSError, UnicodeError):
        return ConfResult({}, {}, conf_path)
    values = parse(text)
    return ConfResult(values, legacy_keys(values), conf_path)


def load_strict(path: Path | str) -> ConfResult:
    """`load` 와 같지만 **판독 실패를 삼키지 않는다** — `OSError`·`UnicodeError` 를 그대로 올린다.

    "conf 가 없다" 와 "conf 를 읽지 못했다" 를 구분해야 하는 소비 지점용이다(예: 실행 프로필을
    conf 에서 해소하는 자리 — 못 읽었는데 빈 conf 로 진행하면 기본값으로 조용히 갈린다).
    부재도 예외(`FileNotFoundError`)이므로 존재 판정은 호출부가 자기 축으로 한다."""
    conf_path = Path(path)
    values = parse(_read_text_shared(conf_path, encoding="utf-8"))
    return ConfResult(values, legacy_keys(values), conf_path)


def legacy_error_message(legacy: dict[str, str | None], path: Path | None = None) -> str:
    """구표기 잔존 안내 — **키 단위로 지목**한다(무엇을 무엇으로 바꾸는지)."""
    where = f" ({path})" if path is not None else ""
    lines = [
        f"local.conf 에 구표기 키가 남아 있습니다{where} — 값이 조용히 엔진 기본값으로 "
        "떨어지지 않도록 여기서 멈춥니다.",
    ]
    lines.extend(migration_lines(legacy))
    lines.append(
        "모델 값은 자동으로 옮기지 않습니다 — 하네스·모델 조합은 환경마다 다르므로 "
        "PM 과 합의해 수동으로 설정하십시오.")
    # 예외 전문도 안내와 **같은 데이터**로 채택자 소유 파일을 지목한다 — 이 예외가 유일한
    # 복구 경로 안내인 형상(도구가 여기서 멈춘다)에서 포인터가 빠지면 채택자는 엔진이 고쳐
    # 주지 않는 파일이 남아 있다는 사실을 어디서도 듣지 못한다.
    lines.extend(adopter_pointer_lines())
    return "\n".join(lines)


def migration_lines(legacy: dict[str, str | None]) -> list[str]:
    """구표기 → 신표기 지목 줄(안내 표면 공용 · 정렬은 선언 순서)."""
    out: list[str] = []
    for key, replacement in legacy.items():
        if replacement is None:
            out.append(f"  · `{key}` → 제거(대체 키 없음)")
        else:
            out.append(f"  · `{key}` → `{replacement}`")
    return out


# ── 교체 안내 (pm_import·pm_update 공용 문구) ────────────────────────────
# 기본값 변경은 **형상 무관 1줄**로 무조건 알린다. 구키가 아예 없는 채택자(키 미설정)는 fail-loud
# 가 잡지 못하는 유일한 형상이고, "안 켰다" 가 "켜졌다" 로 바뀌는 것도 그 형상뿐이라 기계 통지
# 수단이 이 한 줄밖에 없다. 재질문은 하지 않는다 — 폐지한 동의 축을 되살리는 것이기 때문이다.
DELEGATE_DEFAULT_CHANGE_NOTICE = (
    "위임 기본값이 허용으로 바뀝니다 — 위임을 원치 않으면 local.conf 에 "
    "`delegate.enabled=false` 를 명시하세요."
)

# 엔진이 전파하지 않는 **채택자 소유** 파일. 표기 통일이 이 파일들의 문구를 고치지 못하므로
# 안내가 키 단위로 지목한다(안내가 없으면 영구 stale).
ADOPTER_OWNED_POINTERS: tuple[tuple[str, str], ...] = (
    (".codex/config.toml", "`delegate.enabled` 문구(위임 허용 스위치 서술)"),
)


def adopter_pointer_lines() -> list[str]:
    """채택자 소유 파일 지목 줄 — 예외 전문과 교체 안내가 **같은 데이터**로 낸다(사본 0)."""
    return [f"{path} 는 채택자 소유라 갱신되지 않습니다 — {what} 를 직접 고치세요."
            for path, what in ADOPTER_OWNED_POINTERS]


def migration_notice(result: ConfResult) -> list[str]:
    """이 conf 가 받아야 할 교체 안내 — 기본값 1줄 + (구표기가 있으면) 키 단위 지목.

    자동 이관은 하지 않는다. 특히 **모델 값**은 하네스·모델 조합이 환경마다 달라 잘못 옮기면
    조용히 틀린 모델로 돌기 때문에, 채택자가 자기 PM 과 합의해 수동으로 설정한다.
    """
    lines = [DELEGATE_DEFAULT_CHANGE_NOTICE]
    if not result.legacy:
        return lines
    where = f" ({result.path})" if result.path is not None else ""
    lines.append(
        f"local.conf 표기가 통일됐습니다{where} — 아래 키를 바꾸세요(엔진은 채택자 conf 를 "
        "대신 고쳐 쓰지 않습니다). 바꾸기 전까지 그 값을 소비하는 명령은 fail-loud 로 멈춥니다.")
    lines.extend(migration_lines(result.legacy))
    lines.append(
        "모델 값은 자동으로 옮기지 않습니다 — 하네스·모델 조합은 환경마다 다르므로 PM 과 "
        "합의해 수동으로 설정하십시오.")
    lines.extend(adopter_pointer_lines())
    return lines


def blocking_legacy(legacy: dict[str, str | None]) -> dict[str, str | None]:
    """구표기 중 **값 공급을 잃어 조용히 강등될 수 있는** 키만 (안내 전용 키는 뺀다)."""
    return {key: value for key, value in legacy.items()
            if key not in NON_BLOCKING_LEGACY_KEYS}


def assert_no_legacy(result: ConfResult) -> None:
    """구표기 잔존이면 `LegacyConfKeyError` — 값을 **소비하는** 지점이 부른다.

    `pm_update` 의 apply 경로는 이것을 부르지 않는다(엔진 파일은 받게 하고, 값을 쓰는 지점에서
    멈춘다). 판정을 소비 지점마다 복제하지 않기 위해 이 한 호출이 유일한 수단이다.
    `NON_BLOCKING_LEGACY_KEYS` 는 안내만 받고 여기서 막지 않는다.
    """
    blocking = blocking_legacy(result.legacy)
    if blocking:
        raise LegacyConfKeyError(blocking, result.path)


def load_checked(path: Path | str) -> dict[str, str]:
    """`load` + `assert_no_legacy` — **판독 실패까지 빈 결과**로 접는 소비 지점용.

    conf 를 못 읽어도 자기 기본값으로 계속 도는 것이 옳은 자리(부트스트랩 인터프리터 해소·
    `pm-config` 단일 키 조회)가 쓴다. 못 읽은 사실이 실행을 멈춰야 하는 자리는
    `load_checked_readable` 이다 — 두 정책을 한 함수에 섞으면 어느 호출부가 무엇을 기대하는지
    코드에서 사라진다."""
    result = load(path)
    assert_no_legacy(result)
    return result.values


def load_checked_readable(path: Path | str) -> dict[str, str]:
    """부재는 빈 결과, **존재하는데 판독 실패면 예외를 그대로 올린다** + 구표기 fail-loud.

    `local_config()` 계열(board·ticket_finish·additional_reviewer)의 정책이다 — 그 호출부들은
    `OSError`/`UnicodeError` 를 잡아 "실행 전에 중단" 으로 닫는다. 여기서 빈 dict 로 강등하면
    설정 선언을 **확인하지 못한 채** 통과한 실행이 된다."""
    conf_path = Path(path)
    if not conf_path.exists():
        return {}
    result = load_strict(conf_path)
    assert_no_legacy(result)
    return result.values


def assert_values_no_legacy(values: dict[str, str], path: Path | None = None) -> None:
    """이미 파싱된 dict 에 대한 같은 판정(자체 파서를 아직 지닌 호출부·주입 conf 용)."""
    found = blocking_legacy(legacy_keys(values))
    if found:
        raise LegacyConfKeyError(found, path)


def unknown_keys(values: dict[str, str]) -> tuple[str, ...]:
    """레지스트리에 없고 구표기도 아닌 키 (never-block advisory 입력)."""
    known = set(KNOWN_KEYS)
    declared = {
        name.strip() for name in values.get("qa.platforms", "").split(",")
        if name.strip()
    }
    out: list[str] = []
    for key in values:
        platform_name = _platform_command_name(key)
        if platform_name is not None and platform_name not in declared:
            out.append(key)
            continue
        if key in known or is_legacy_key(key) or _matches_pattern(key):
            continue
        out.append(key)
    return tuple(out)


def _matches_pattern(key: str) -> bool:
    """패턴 키군(`delegate.<role>…`·`harness.<name>.*`·`diff_cap.<estimate>`) 소속 여부."""
    segments = key.split(".")
    if len(segments) < 2:
        return False
    head = segments[0]
    if head == "diff_cap":
        return len(segments) == 2 and segments[1] in _diff_cap_names()
    if head == "harness":
        return (len(segments) == 3 and segments[1] in _harness_names()
                and segments[2] in _HARNESS_SUFFIXES)
    if head == "delegate":
        return _matches_delegate_pattern(segments[1:])
    if head == "test":
        return _platform_command_name(key) is not None
    return False


def _platform_command_name(key: str) -> str | None:
    """정확한 `test.<name>.cmd` 키면 name, 아니면 None."""
    segments = key.split(".")
    if (
        len(segments) == 3 and segments[0] == "test" and segments[2] == "cmd"
        and _PLATFORM_NAME_RE.fullmatch(segments[1]) and segments[1] != "core"
    ):
        return segments[1]
    return None


def platform_test_commands(values: dict[str, str]) -> tuple[tuple[str, str], ...]:
    """`qa.platforms` 선언을 순서 보존 `(name, command)` tuple로 fail-closed 해소한다.

    선언 부재/빈값은 legacy no-platform 형상이라 platform 의미 검사를 전혀 하지 않는다.
    nonempty 선언부터 이름·중복·command 누락·미선언 orphan을 모두 오류로 올린다.
    """
    raw = values.get("qa.platforms")
    if raw is None or not raw.strip():
        return ()
    names = [part.strip() for part in raw.split(",")]
    for name in names:
        if not _PLATFORM_NAME_RE.fullmatch(name) or name == "core":
            raise ValueError(
                "qa.platforms 이름은 [a-z0-9][a-z0-9_-]{0,31} 이어야 하며 "
                f"`core`는 예약어다: {name!r}"
            )
    if len(set(names)) != len(names):
        raise ValueError(f"qa.platforms 중복 선언: {raw!r}")
    declared = set(names)
    orphaned = [
        key for key in values
        if (name := _platform_command_name(key)) is not None and name not in declared
    ]
    if orphaned:
        raise ValueError("qa.platforms 미선언 platform command: " + ", ".join(orphaned))
    resolved: list[tuple[str, str]] = []
    for name in names:
        key = f"test.{name}.cmd"
        command = values.get(key, "").strip()
        if not command:
            raise ValueError(f"qa.platforms 선언에 필요한 `{key}`가 없거나 비어 있다")
        resolved.append((name, command))
    return tuple(resolved)


def _matches_delegate_pattern(rest: list[str]) -> bool:
    """`delegate.` 아래 닫힌 문법 — 역할 매핑 tuple 과 model_alias 표."""
    if not rest:
        return False
    if rest[0] == "model_alias":
        return len(rest) == 2 and bool(rest[1])
    if rest[0] not in _role_names():
        return False
    tail = rest[1:]
    if tail and tail[0] == "hard":
        tail = tail[1:]
    if tail and tail[0] == "fallback":
        tail = tail[1:]
    return len(tail) == 1 and tail[0] in _ROLE_SUFFIXES


def _role_names() -> tuple[str, ...]:
    """역할 목록 — `pm_delegate.ROLE_CHOICES` 파생(못 읽으면 빈 목록 → 모양 판정 강등)."""
    return _sibling_sequence("pm_delegate.py", "ROLE_CHOICES")


def _harness_names() -> tuple[str, ...]:
    """하네스 목록 — `pm_relay.HARNESS_CHOICES` 파생."""
    return _sibling_sequence("pm_relay.py", "HARNESS_CHOICES")


def _diff_cap_names() -> tuple[str, ...]:
    """diff 상한 estimate 목록 — `additional_reviewer.DEFAULT_DIFF_CAPS` 키 파생."""
    return _sibling_sequence("additional_reviewer.py", "DEFAULT_DIFF_CAPS")


_SEQUENCE_CACHE: dict[tuple[str, str], tuple[str, ...]] = {}


def _sibling_sequence(filename: str, attribute: str) -> tuple[str, ...]:
    """형제 모듈 선언에서 목록을 읽는다 — **소스 텍스트만** 본다(import 0·순환 0).

    형제를 실행 import 하면 그 형제가 다시 conf 를 읽어 로드 순환이 된다. 필요한 것은 리터럴
    목록 하나뿐이라 `ast` 로 해당 최상위 대입만 평가한다. 실패(부재·구형 사본·비리터럴)는 빈
    목록이고, 호출부는 그 키를 advisory 대상으로 남긴다(막는 것이 없다).
    """
    cache_key = (filename, attribute)
    if cache_key in _SEQUENCE_CACHE:
        return _SEQUENCE_CACHE[cache_key]
    names: tuple[str, ...] = ()
    try:
        import ast as _ast

        source = Path(__file__).resolve().with_name(filename).read_text(encoding="utf-8")
        for node in _ast.parse(source).body:
            targets = []
            if isinstance(node, _ast.Assign):
                targets = [t for t in node.targets if isinstance(t, _ast.Name)]
            elif isinstance(node, _ast.AnnAssign) and isinstance(node.target, _ast.Name):
                targets = [node.target]
            if not any(t.id == attribute for t in targets) or node.value is None:
                continue
            value = _ast.literal_eval(node.value)
            if isinstance(value, dict):
                names = tuple(str(k) for k in value)
            elif isinstance(value, (list, tuple, set, frozenset)):
                names = tuple(str(v) for v in value)
            break
    except Exception:  # noqa: BLE001 — 부재/손상/구형 사본은 advisory 판정의 정상 입력이다.
        names = ()
    _SEQUENCE_CACHE[cache_key] = names
    return names


def _verify_engine_rev(sibling_module, sibling_filename):
    """로드한 형제 모듈의 baked ENGINE_REV 를 이 사본의 것과 대조한다 (fail-loud·skew→명시 에러).

    이 모듈은 conf 판독 leaf 라 **읽기 경로**에는 형제 판정 계층을 두지 않는다(그 지연 로드는
    코드 소유 면제다). CLI 진입(`main`)은 그 경로가 아니라 도구 실행이므로, CLI 를 가진 형제
    모듈들과 같은 규칙으로 사본 skew 를 여기서 fail-loud 로 잡는다.
    """
    got = getattr(sibling_module, "ENGINE_REV", None)
    if got != ENGINE_REV:
        err = RuntimeError(
            f"엔진 사본 버전 불일치 — 로더 {Path(__file__).name}(rev={ENGINE_REV!r})가 "
            f"형제 {sibling_filename}(rev={got!r})를 로드했다 (사본 skew: 부분/수동 복사 또는 "
            f"구형 사본). `pm_update.py`(또는 pm_update.py)로 .project_manager/tools/ 전체를 재동기하라."
        )
        err._engine_rev_skew = True  # fail-soft 로더가 재-raise 식별
        raise err


def main(argv: list[str] | None = None) -> int:
    """어댑터 생성 블록 출력 — 파서에 붙일 텍스트를 사람이 손으로 옮겨 적지 않게 한다."""
    _console_encoding = _load_module_from_path(
        Path(__file__).resolve().with_name("console_encoding.py"),
        "console_encoding.py",
        verifier=_verify_engine_rev,
    )
    _console_encoding.configure_console_utf8()
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 2 and args[0] == "--render-adapter-block":
        print(render_adapter_block(args[1]))
        return 0
    print("사용법: local_conf.py --render-adapter-block <python|js|sh>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
