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

import json
import os
import re
import sys
import unicodedata
from pathlib import Path


# ── 엔진 사본 rev 스탬프 (형제 사본 skew fail-loud) ──────────────────────
# baked 리터럴 — `identity_args.py`·`file_lock.py` 와 같은 규약이다. 릴리즈 bump 는
# `engine_rev.py --bump vX.Y.Z` 가 전 stamped 모듈을 기계 일괄 재작성한다.
ENGINE_REV = "v1.8.0"
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
    "ctx_nudge_pct": "ctx.checkpoint_pct",
    "ctx_stop_pct": "ctx.precompact_pct",
    "ctx_window_tokens": "ctx.window_tokens",
    # 이름이 하는 일과 갈렸던 두 축(v1.8.0) — `stop` 은 아무것도 멈추지 않았고 `nudge` 는
    # 무엇을 권하는지 말하지 않았다. 잔여 컨텍스트가 임계 아래일 때 하는 일 그대로 적는다:
    # 중간 기록 권고 / 자동 압축 임박 최종 안내. 갱신이 옮긴다(`pm_update` 이관 단계).
    "ctx.nudge_pct": "ctx.checkpoint_pct",
    "ctx.stop_pct": "ctx.precompact_pct",
    # 제거 — 회귀 수집 하한 폐지(대체 키 없음: 수집수는 직전 기록 대비 증감으로 표시만 한다)
    "regression_min_collected": None,
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
# **이 표가 키의 정본이다** — 이름·설명·기본값·필수 여부를 한 자리에서 소유한다. 코드에 흩어져
# 있던 conf 기본값 상수(board ctx 3 · additional_reviewer 5 · pm_delegate 1)는 여기로 옮겨졌고
# 소비자는 `default_for()` 로 읽는다. 출하 문서는 이 표를 복제하지 않고 `local_conf.py --keys`
# 를 가리킨다 — 복제본은 반드시 drift 한다.
#
# 축 없는 단일값도 축을 만든다(`test.cmd`·`runtime.py`) — "이 키는 왜 다르지" 가 생기지 않게
# 예외를 두지 않는다. 세그먼트 구분자만 점이고, 세그먼트 안의 철자는 그 식별자의 정본을 따른다
# (역할은 `pm_delegate.ROLE_CHOICES` 표기 `code-reviewer`, 속성은 snake_case `idle_timeout`).
# 폐지된 이름은 여기 없다 — 그 표는 `LEGACY_KEY_MAP` 하나다(중복 표기 금지).


class KeySpec:
    """local.conf 키 하나의 선언 — 설명·기본값·필수 여부·표시 묶음.

    (평범한 클래스 — `@dataclass` 미사용 이유는 `ConfResult` 와 같다: 이 모듈은
    `spec_from_file_location` 으로 로드되는데 `from __future__ import annotations` 와 결합 시
    dataclass 처리가 깨진다.)

    `default` 는 **conf 파일에 적히는 문자열**이다(표기가 곧 값). 고정 기본값이 없는 키는 `None`
    이고 편차 판정(`nonstandard`)의 대상이 아니다 — 하네스별 표(`pm_relay.HARNESS_PROFILES`)나
    실행 탐지값처럼 한 칸으로 표현되지 않는 기본을 지어내지 않는다.

    `required` 는 **반드시 명시**한다(기본값 없음) — 새 키를 적으면서 필수 여부를 빠뜨리면
    import 시점에 `TypeError` 로 멈춘다.

    `choices` 는 이 키가 고르는 **값의 축 이름**(`harness`·`model`·`reasoning` = `_ROLE_SUFFIXES`)
    이고 목록 자체는 담지 않는다 — 목록의 정본은 형제 선언(`pm_relay.HARNESS_CHOICES`·
    `REASONING_ALLOWED`·`MODEL_SOURCE`)이라 여기 복제하면 두 자리가 갈린다. 고를 목록이 없는 키는
    `None` 이고, 선택지는 **검증이 아니다**(harness·reasoning 은 닫힌 집합, model 은 자유 문자열).
    """

    __slots__ = ("label", "description", "default", "required", "group", "choices", "unset",
                 "governed_by", "readonly")

    def __init__(self, label: str, description: str, *, default: str | None,
                 required: bool, group: str, choices: str | None = None,
                 unset: str | None = None, governed_by: str | None = None,
                 readonly: bool = False) -> None:
        # `label` = 사람이 읽는 이름(명사구·키 문자열을 옮겨 적지 않는다), `description` = 그 값이
        # 무엇을 바꾸는지 한 줄. 둘 다 종결형 어미를 쓰지 않는다 — 설정 화면 문구의 단일 소유가
        # 여기이고, 화면은 받아 그리기만 한다.
        # `unset` = 이 키를 **안 채웠을 때 실제로 일어나는 일** 한 문장이다. 셋 중 하나로 쓴다:
        # 다른 값을 씀(`운영체제 기본 파이썬을 씀`) · 그 기능이 돌지 않음(`교차 검토를 하지 않음`) ·
        # 그 명령이 멈춤(`이 역할로 위임하면 설정이 없다며 멈춤`). 기본값이 있으면 그 값이 곧 답이라
        # None 이고, 없으면 반드시 채운다 — 아래 불변식이 강제한다(설정 화면에 빈 칸이 없다).
        # `governed_by` = 이 키를 쓰게 만드는 스위치 키(`_MASTER_SWITCHES`). 그 스위치가 꺼져
        # 있으면 이 키는 아무 효과가 없고 화면은 칸을 잠근다.
        # `readonly` = 엔진이 적는 키 — 사람이 고칠 자리가 아니다.
        if default is None and unset is None:
            raise TypeError(
                f"{label}: 기본값이 없는 키는 `unset`(안 채웠을 때 일어나는 일)을 반드시 선언한다 — "
                "빈 칸을 화면에 남기지 않는다")
        self.unset = unset
        self.label = label
        self.description = description
        self.default = default
        self.required = required
        self.group = group
        self.choices = choices
        self.governed_by = governed_by
        self.readonly = readonly

    def __repr__(self) -> str:                                   # 진단 표면(테스트 실패 메시지)
        return (f"KeySpec(label={self.label!r}, default={self.default!r}, "
                f"required={self.required!r}, group={self.group!r})")


# 패턴 키군 — `<name>` 자리에 들어갈 목록은 **코드가 소유**한다(손열거 0). 전개는 형제 모듈의
# 선언에서 파생하고, 형제를 못 읽는 형상(부분 사본·복구 채널)에서는 모양 판정으로 강등한다 —
# 이 판정의 유일한 소비자가 never-block advisory(모르는 키 경고)라 강등이 무엇도 막지 않는다.
_ROLE_SUFFIXES: tuple[str, ...] = ("harness", "model", "reasoning")
# 역할 키의 티어 조합 — `_delegate_pattern_for` 가 파싱하는 것과 같은 닫힌 문법이다
# (선언과 파서가 갈리면 `pattern_for` 왕복 테스트가 red).
_DELEGATE_TIERS: tuple[str, ...] = ("", ".hard", ".fallback", ".hard.fallback")
_HARNESS_SUFFIXES: tuple[str, ...] = (
    "idle_timeout", "wall_timeout", "ctx_window_tokens", "pro_model",
)
_PLATFORM_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}\Z")

# 패턴 군의 표시 이름 = 레지스트리 키. 전개 가능한 자리는 위 선언에서 만든다(사본 0).
DELEGATE_ROLE_PATTERN = (
    "delegate.<role>[.hard][.fallback].{" + ",".join(_ROLE_SUFFIXES) + "}")
DELEGATE_ALIAS_PATTERN = "delegate.model_alias.<name>"
HARNESS_PATTERN = "harness.<name>.{" + ",".join(_HARNESS_SUFFIXES) + "}"
DIFF_CAP_PATTERN = "diff_cap.<estimate>"
PLATFORM_TEST_PATTERN = "test.<platform>.cmd"

# diff 서킷브레이커 상한 — 값이 estimate 마다 달라 군 선언 한 칸에 담기지 않는다. 이 표가
# estimate 목록의 정본이기도 하다(`_matches_pattern` 이 여기서 이름을 읽는다).
DIFF_CAP_DEFAULTS: dict[str, str] = {"small": "300", "medium": "1000", "large": "2500"}

# 역할 위임의 **권장 조합** — 키는 `"<role><tier>"`(`_DELEGATE_TIERS` 표기 그대로), 값은 접미
# (`_ROLE_SUFFIXES`)별 권장값이다. 권장값은 **기본값이 아니다**: 저장 전에는 아무 효과가 없고
# (`default` 는 계속 None) 빈 위임 설정은 그대로 fail-loud 다 — 고르기 쉽게 하는 값일 뿐이다.
#
# 근거 없는 자리는 비운다(지어내지 않는다): `.fallback` 티어는 "인프라 실패 1회 대체" 에 무엇을
# 권할지가 실측이 아니라 결정이라 없고, `additional_reviewer.*` 는 실측 model 이 버전 붙은 라이브
# 모델 ID 라 엔진에 박으면 반드시 drift 한다. 여기 값이 안전한 이유는 별칭(그 CLI 가 "최신 모델의
# 별칭" 으로 계약한 이름)이기 때문이다.
DELEGATE_RECOMMENDED: dict[str, dict[str, str]] = {
    "developer": {"harness": "claude", "model": "sonnet", "reasoning": "high"},
    "developer.hard": {"harness": "claude", "model": "opus", "reasoning": "high"},
    "researcher": {"harness": "claude", "model": "sonnet", "reasoning": "medium"},
    "architect": {"harness": "claude", "model": "opus", "reasoning": "xhigh"},
    "code-reviewer": {"harness": "claude", "model": "opus", "reasoning": "xhigh"},
}

# 표시 묶음 — `--keys` 출력과 설정 화면이 쓰는 절 이름이다(선언 순서 = 표시 순서).
KEY_GROUPS: tuple[str, ...] = (
    "project", "runtime", "identity", "upstream",
    "ctx", "harness", "delegate", "additional_reviewer",
)

# 절 이름의 사람 말. 묶음 id 는 코드 좌표라 화면에 그대로 세우면 영문 변수명이 보인다 —
# 이름의 주인도 이 표 하나다(화면이 짓지 않는다).
GROUP_LABELS: dict[str, str] = {
    "project": "프로젝트 정보",
    "runtime": "실행·테스트",
    "identity": "사용자",
    "upstream": "업데이트",
    "ctx": "컨텍스트 한도",
    "harness": "도구별 설정",
    "delegate": "작업 위임",
    "additional_reviewer": "교차 검토",
}

# 시간 예산 키의 기본은 **하네스별 표**라 한 칸에 담기지 않는다 — 설명이 정본의 자리를 말한다.
_HARNESS_PROFILE_NOTE = "미설정이면 하네스 프로필(`pm_relay.HARNESS_PROFILES`)이 정본이다"

# 절을 켜는 키 → 꺼져 있을 때 그 절의 나머지 칸에 붙는 한 줄. 켜짐 판정은 소비자 것이다:
# 교차 검토 도구는 값이 있으면 켜짐(`additional_reviewer.resolve_reviewer_target`), 위임 사용은
# 빈 값이면 기본 ON 이고 그 밖에는 `pm_delegate.DELEGATE_ENABLED_VALUES` 안이어야 켜짐이다.
_MASTER_SWITCHES: dict[str, str] = {
    "additional_reviewer.harness": "교차 검토 도구를 정해야 쓰임",
    "delegate.enabled": "위임 사용이 켜져 있어야 쓰임",
}

# 도구·모델·생각 깊이를 한 세트로 읽는 교차 검토 묶음의 이름(`ADDITIONAL_REVIEWER_KEYS` 의 접두).
_REVIEWER_UNIT = "additional_reviewer"

# 기본 시간을 도구마다 따로 가진 키가 안 채워졌을 때.
_TOOL_DEFAULT_TIME = "도구마다 정해진 기본 시간을 씀"
# 안내 문서에 박히는 키가 안 채워졌을 때 — 출하 안내 문서는 설치 때 이미 값이 박혀 평소엔
# 이 키를 다시 읽지 않는다. 그 자리가 남은 원본을 다시 그릴 때만 채우라며 멈춘다
# (`pm_render._assert_no_leak`).
_RENDER_ONLY_UNSET = "평소엔 영향 없음, 안내 문서를 다시 그릴 때 이 자리가 남아 있으면 채우라며 멈춤"

KEY_REGISTRY: dict[str, KeySpec] = {
    "project.name": KeySpec(
        "프로젝트 이름",
        "안내 문서(CLAUDE.md 등)에 들어갈 이름",
        default=None, unset=_RENDER_ONLY_UNSET, required=False, group="project"),
    "project.tagline": KeySpec(
        "한 줄 소개",
        "안내 문서 첫머리에 들어갈 소개",
        default=None, unset=_RENDER_ONLY_UNSET, required=False, group="project"),
    "project.root": KeySpec(
        "기준 경로",
        "안내 문서에 적을 경로의 기준",
        default=None, unset=_RENDER_ONLY_UNSET, required=False, group="project"),
    "project.date": KeySpec(
        "기준 날짜",
        "안내 문서에 적을 날짜",
        default=None, unset=_RENDER_ONLY_UNSET, required=False, group="project"),
    "runtime.py": KeySpec(
        "파이썬 실행 파일",
        "엔진을 실행할 파이썬 (예: python3, Windows 는 py -3.12), 첫 설치 때 실행해 보고 채움",
        default=None, unset="운영체제 기본 파이썬(python3, Windows 는 py -3)을 씀",
        required=True, group="runtime"),
    "test.cmd": KeySpec(
        "테스트 명령",
        "테스트를 돌릴 때 그대로 실행할 명령",
        default=None, unset="파이썬 기본 테스트 명령(python -m pytest tests/ -q)을 씀",
        required=True, group="runtime"),
    "qa.platforms": KeySpec(
        "테스트할 운영체제 목록",
        "쉼표로 구분 (예: linux, windows), 적으면 운영체제마다 테스트 명령을 따로 정해야 하고 적은 순서대로 실행",
        default=None, unset="운영체제 구분 없이 테스트 명령 하나만 돎", required=False, group="runtime"),
    "identity.user": KeySpec(
        "내 계정",
        "작업 기록에 남길 이름",
        default=None, unset="git 설정의 이메일을 씀, 그것도 없으면 이름 없이 기록",
        required=False, group="identity"),
    "upstream.path": KeySpec(
        "원본 위치",
        "업데이트를 받아올 폴더 경로나 주소",
        default=None, unset="받아올 곳이 없어 업데이트가 멈춤", required=False, group="upstream"),
    "upstream.rev": KeySpec(
        "마지막으로 받은 커밋",
        "업데이트 차이를 재는 기준, 업데이트가 적음",
        default=None, unset="아직 업데이트를 받은 적 없음", required=False, group="upstream",
        readonly=True),
    "upstream.seen_rev": KeySpec(
        "마지막으로 확인한 커밋",
        "원본에서 마지막으로 본 커밋, 업데이트가 적음",
        default=None, unset="아직 원본을 확인한 적 없음", required=False, group="upstream",
        readonly=True),
    "ctx.checkpoint_pct": KeySpec(
        "중간 기록 권고 (%)",
        "남은 컨텍스트가 이 비율 이하가 되면 중간 기록을 권함, 작업은 계속",
        default="30", required=False, group="ctx"),
    "ctx.precompact_pct": KeySpec(
        "압축 임박 경고 (%)",
        "남은 컨텍스트가 이 비율 이하가 되면 자동 압축이 곧 일어난다고 보고 마지막으로 중간 기록을 권함, 작업을 멈추거나 세션을 바꾸지는 않음",
        default="20", required=False, group="ctx"),
    "ctx.window_tokens": KeySpec(
        "컨텍스트 크기 (토큰)",
        "위 두 비율의 분모, 도구별 값이 없을 때 쓰는 기준, 실제보다 작게 두면 더 일찍 넘김",
        default="200000", required=False, group="ctx"),
    "delegate.enabled": KeySpec(
        "위임 사용",
        "끄면 다른 도구에 작업을 맡기지 않음",
        default="true", required=False, group="delegate"),
    "delegate.timeout": KeySpec(
        "위임 최대 실행 시간 (초)",
        "이 시간을 넘으면 중단",
        default=None, unset=_TOOL_DEFAULT_TIME, required=False, group="delegate",
        governed_by="delegate.enabled"),
    "delegate.idle_timeout": KeySpec(
        "위임 응답 없을 때 대기 (초)",
        "이만큼 출력이 없으면 멈춘 것으로 봄",
        default=None, unset=_TOOL_DEFAULT_TIME, required=False, group="delegate",
        governed_by="delegate.enabled"),
    "delegate.code-reviewer.rounds_max": KeySpec(
        "검토 최대 횟수",
        "같은 작업을 다시 검토할 최대 횟수",
        default="3", required=False, group="delegate", governed_by="delegate.enabled"),
    "additional_reviewer.harness": KeySpec(
        "교차 검토 도구",
        "다른 모델로 한 번 더 검토할 때 쓸 도구, 모델과 함께 채움",
        default=None, unset="교차 검토를 하지 않음", required=False, group="additional_reviewer",
        choices="harness"),
    "additional_reviewer.model": KeySpec(
        "교차 검토 모델",
        "그 도구가 받는 표기 그대로",
        default=None, unset="교차 검토를 하지 않음", required=False, group="additional_reviewer",
        choices="model"),
    "additional_reviewer.reasoning": KeySpec(
        "교차 검토 생각 깊이",
        "고를 수 있는 값은 도구마다 다름",
        default=None, unset="교차 검토를 하지 않음", required=False, group="additional_reviewer",
        choices="reasoning"),
    "additional_reviewer.rounds_max": KeySpec(
        "교차 검토 최대 횟수",
        "한 건을 다시 볼 최대 횟수",
        default="2", required=False, group="additional_reviewer",
        governed_by="additional_reviewer.harness"),
    "additional_reviewer.incomplete_rounds_max": KeySpec(
        "교차 검토 재시도 횟수",
        "결과 없이 끝난 호출을 다시 부를 최대 횟수",
        default="2", required=False, group="additional_reviewer",
        governed_by="additional_reviewer.harness"),
    "additional_reviewer.wave_budget": KeySpec(
        "교차 검토 세션당 총 횟수",
        "한 세션에서 쓸 수 있는 전체 호출 수",
        default="24", required=False, group="additional_reviewer",
        governed_by="additional_reviewer.harness"),
    "additional_reviewer.paths": KeySpec(
        "교차 검토할 폴더",
        "쉼표나 공백으로 구분한 경로 목록",
        default="src/,tests/,scripts/,.project_manager/tools/",
        required=False, group="additional_reviewer", governed_by="additional_reviewer.harness"),
    "additional_reviewer.timeout": KeySpec(
        "교차 검토 최대 실행 시간 (초)",
        "이 시간을 넘으면 중단",
        default=None, unset=_TOOL_DEFAULT_TIME, required=False, group="additional_reviewer",
        governed_by="additional_reviewer.harness"),
    "additional_reviewer.idle_timeout": KeySpec(
        "교차 검토 응답 없을 때 대기 (초)",
        "이만큼 출력이 없으면 멈춘 것으로 봄",
        default=None, unset=_TOOL_DEFAULT_TIME, required=False, group="additional_reviewer",
        governed_by="additional_reviewer.harness"),
    "additional_reviewer.progress_signal": KeySpec(
        "교차 검토 진행 출력 형식",
        "그 도구가 진행 상황을 어떤 모양으로 내는지",
        default=None, unset="도구에 맞춰 자동으로 고름", required=False, group="additional_reviewer",
        choices="progress_signal", governed_by="additional_reviewer.harness"),
}

PATTERN_REGISTRY: dict[str, KeySpec] = {
    HARNESS_PATTERN: KeySpec(
        "도구마다 따로 정하는 값",
        "실행 시간·대기 시간·컨텍스트 크기와 안내 문서에 적을 모델 이름",
        default=None, unset=_TOOL_DEFAULT_TIME, required=False, group="harness"),
    DELEGATE_ROLE_PATTERN: KeySpec(
        "역할별 담당 도구",
        "역할마다 도구·모델·생각 깊이를 한 묶음으로 정함, 실패 시 대신 쓸 묶음을 따로 둘 수 있음 (어려운 작업용 묶음은 구현만)",
        default=None, unset="이 역할로 위임하면 설정이 없다며 멈춤", required=False,
        group="delegate", governed_by="delegate.enabled"),
    DELEGATE_ALIAS_PATTERN: KeySpec(
        "모델 별칭",
        "같은 모델의 다른 표기를 한 이름으로 묶음, 구현과 검토가 같은 모델인지 볼 때만 씀",
        default=None, unset="표기가 다르면 다른 모델로 봄", required=False, group="delegate"),
    DIFF_CAP_PATTERN: KeySpec(
        "작업 크기별 변경 줄 수 상한",
        "작업을 마칠 때와 교차 검토 전에 확인, 추가·삭제 줄 수 합계가 넘으면 멈춤, 모르는 크기에는 제한을 두지 않음",
        default=None, unset="제한을 두지 않음", required=False, group="additional_reviewer"),
    PLATFORM_TEST_PATTERN: KeySpec(
        "운영체제별 테스트 명령",
        "위 '테스트할 운영체제 목록' 에 적은 이름마다 하나씩, 목록에 없는 이름은 모르는 항목으로 표시",
        default=None, unset="목록에 적은 운영체제면 테스트가 멈춤", required=False, group="runtime"),
}

# 기존 호출부(`unknown_keys`·`_matches_pattern`)가 그대로 도는 파생 튜플 — 레지스트리가 정본이다.
KNOWN_KEYS: tuple[str, ...] = tuple(KEY_REGISTRY)


# `harness.<name>.*` 의 미설정 동작은 접미마다 다르다 — 시간 두 축은 하네스 프로필이 받고,
# 컨텍스트 크기는 공용 기준값(`ctx.window_tokens`)으로 흘러가며, 모델 이름은 안내 문서 렌더가
# 자리를 TODO 로 남긴다(`pm_render.neutralize_model_todo`).
_HARNESS_MEMBER_UNSET: dict[str, str] = {
    "idle_timeout": _TOOL_DEFAULT_TIME,
    "wall_timeout": _TOOL_DEFAULT_TIME,
    "ctx_window_tokens": "위 공통 컨텍스트 크기를 씀",
    "pro_model": "안내 문서의 모델 자리를 TODO 로 남김",
}

# 역할 묶음이 안 채워졌을 때 — 티어마다 다르다(`pm_delegate.resolve_delegate`·`resolve_fallback`).
_DELEGATE_TIER_UNSET: dict[str, str] = {
    "": "이 역할로 위임하면 설정이 없다며 멈춤",
    ".hard": "어려운 작업 위임이 멈춤, 보통 묶음으로 대신하지 않음",
    ".fallback": "실패하면 대신 쓰지 않고 실패로 끝남",
    ".hard.fallback": "실패하면 대신 쓰지 않고 실패로 끝남",
}


def key_spec(key: str) -> KeySpec | None:
    """이 키의 선언 — 실명은 레지스트리에서, 패턴 키는 그 군에서 해소한다(모르는 키는 None)."""
    spec = KEY_REGISTRY.get(key)
    if spec is not None:
        return spec
    pattern = pattern_for(key)
    if pattern is None:
        return None
    spec = PATTERN_REGISTRY[pattern]
    if pattern == DIFF_CAP_PATTERN:
        # 상한이 estimate 마다 다르다 — 군 선언은 값을 담지 못하므로 이 키의 값으로 채워 준다.
        return KeySpec(spec.label, spec.description, default=DIFF_CAP_DEFAULTS[key.split(".")[1]],
                       required=spec.required, group=spec.group)
    if pattern == DELEGATE_ROLE_PATTERN:
        # 축이 멤버마다 다르다(harness/model/reasoning) — 군 한 칸에 담기지 않으므로 이 키의
        # 접미로 채워 준다(`_delegate_pattern_for` 가 그 접미를 `_ROLE_SUFFIXES` 로 이미 닫았다).
        # 안 채웠을 때 일어나는 일은 티어마다 다르다(`_DELEGATE_TIER_UNSET`).
        role, _, rest = key[len("delegate."):].partition(".")
        tier = "." + rest.rpartition(".")[0] if "." in rest else ""
        return KeySpec(spec.label, spec.description,
                       default=spec.default, required=spec.required,
                       group=spec.group, choices=key.rsplit(".", 1)[1],
                       unset=_DELEGATE_TIER_UNSET[tier], governed_by=spec.governed_by)
    if pattern == HARNESS_PATTERN:
        suffix = key.rsplit(".", 1)[1]
        return KeySpec(spec.label, spec.description,
                       default=spec.default, required=spec.required, group=spec.group,
                       unset=_HARNESS_MEMBER_UNSET[suffix],
                       choices="model" if suffix == "pro_model" else None)
    return spec


def default_for(key: str) -> str | None:
    """이 키의 엔진 기본값(conf 표기 그대로) — 선언이 없거나 모르는 키면 None."""
    spec = key_spec(key)
    return None if spec is None else spec.default


def unset_for(key: str) -> str | None:
    """이 키를 **안 채웠을 때 적용되는 상태** 한 낱말 — 기본값이 있으면 None(그 값이 답이다).

    `default_for` 와 짝이다: 둘 중 정확히 하나가 항상 값이므로 화면에 빈 칸이 생기지 않는다.
    """
    spec = key_spec(key)
    return None if spec is None else spec.unset


def recommended_for(key: str) -> str | None:
    """이 키의 **권장값** — 근거가 있는 자리만 값이 있고 나머지는 None (기본값이 아니다).

    `default_for` 와 다른 축이다: 기본값은 미설정 시 엔진이 실제로 쓰는 값이고, 권장값은 채택자가
    저장해야 효과가 나는 제안이다. 표는 역할 위임 키만 담는다(`DELEGATE_RECOMMENDED`).
    """
    if pattern_for(key) != DELEGATE_ROLE_PATTERN:
        return None
    role_tier, _, suffix = key[len("delegate."):].rpartition(".")
    return DELEGATE_RECOMMENDED.get(role_tier, {}).get(suffix)


def required_keys() -> tuple[str, ...]:
    """없으면 핵심 명령(회귀·init)이 못 도는 키 — 선언 순서.

    역할 위임 키는 필수가 아니다: 없으면 위임 시점에 이미 fail-loud 이고, 필수로 두면 새로
    import 한 프로젝트가 전부 누락 배너를 달고 태어난다.
    """
    return tuple(key for key, spec in KEY_REGISTRY.items() if spec.required)


def missing_required(values: dict[str, str]) -> tuple[str, ...]:
    """`values` 에 **선언 자체가 없는** 필수 키 — 빈 문자열은 "있음" 으로 본다.

    값의 내용 판정은 그 값을 소비하는 지점이 한다(빈값 허용 키를 여기서 오탐하지 않는다).
    """
    return tuple(key for key in required_keys() if key not in values)


def nonstandard(values: dict[str, str]) -> dict[str, tuple[str, str]]:
    """기본값이 선언된 키 중 값이 기본과 다른 것 → `{키: (값, 기본값)}`.

    기본값이 없는 키(하네스별 표·탐지값·per-clone 좌표)는 비교 대상이 아니다 — 기준이 없는
    자리에 기준을 지어내지 않는다.
    """
    out: dict[str, tuple[str, str]] = {}
    for key, value in values.items():
        default = default_for(key)
        if default is not None and value != default:
            out[key] = (value, default)
    return out


# ── 키 표 출력 (`--keys`) ────────────────────────────────────────────────
_KEY_TABLE_HEADERS = ("키", "필수", "기본값", "설명")


def _key_table_rows(registry: dict[str, KeySpec]) -> list[tuple[str, str, str, str]]:
    """표 한 벌의 행 — 필수는 `필수`/`-`, 선언 없는 기본값은 `-` 로 적는다."""
    return [(key, "필수" if spec.required else "-",
             _default_display(key, spec), spec.description)
            for key, spec in registry.items()]


def _default_display(key: str, spec: KeySpec) -> str:
    """표에 적는 기본값 — 군 전체가 한 값을 갖지 않으면 멤버별로 펼쳐 적는다."""
    if key == DIFF_CAP_PATTERN:
        return " · ".join(f"{name} {value}" for name, value in DIFF_CAP_DEFAULTS.items())
    return spec.default if spec.default is not None else "-"


def _display_width(text: str) -> int:
    """전각(한글·CJK)을 2칸으로 센 표시 폭 — `str.ljust` 는 한글 헤더에서 칸을 어긋나게 한다."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _render_key_rows(rows: list[tuple[str, str, str, str]]) -> list[str]:
    """앞 세 칸만 폭을 맞춘다(설명은 자유 길이 — 줄바꿈으로 정렬을 흉내내지 않는다)."""
    table = [_KEY_TABLE_HEADERS, *rows]
    widths = [max(_display_width(row[i]) for row in table) for i in range(3)]
    out = []
    for row in table:
        cells = [row[i] + " " * (widths[i] - _display_width(row[i])) for i in range(3)]
        out.append("  ".join([*cells, row[3]]).rstrip())
    return out


def render_key_table() -> str:
    """`--keys` 출력 — 실명 키 표 + 패턴 키군 표. 이 산출이 키 목록의 사람 표면이다."""
    lines = [
        "local.conf 키 — 정본은 `local_conf.py` 레지스트리다(문서는 이 표를 복제하지 않는다).",
        "필수 = 없으면 핵심 명령(회귀·init)이 못 도는 키 · 기본값 `-` = 선언된 고정 기본값 없음.",
    ]
    for group in _declared_groups():
        rows = _key_table_rows(
            {key: spec for key, spec in KEY_REGISTRY.items() if spec.group == group})
        if not rows:
            continue
        lines.append("")
        lines.append(f"[{group}]")
        lines.extend(_render_key_rows(rows))
    lines.append("")
    lines.append("패턴 키군 (`<...>` 자리 목록은 코드·선언이 소유한다)")
    lines.extend(_render_key_rows(_key_table_rows(PATTERN_REGISTRY)))
    return "\n".join(lines)


# ── 키 표의 기계 표면 (`--keys --json`) ──────────────────────────────────
# 사람 표(`render_key_table`)와 **같은 레지스트리·같은 순서**를 낸다. 다른 점은 셋이다:
# 표시용 합성 표기(`_default_display` 의 `small 300 · medium 1000 · …`)를 싣지 않고, 패턴 키군의
# 전개된 실명을 `members` 로 함께 내며, 행마다 `choices`(고를 수 있는 값)·`recommended`(권장값)를
# 싣는다. 소비자는 관리자 화면이고, 화면은 **그 프로젝트 사본의** 이 산출을 읽는다(세대가 다르면
# 키 집합이 다르므로 남의 레지스트리로 대신 그리지 않는다).
#
# schema 2 = 1 + `choices`·`recommended`. `keys[]`·`members[]` 의 **모든 행**이 두 키를 갖고(없으면
# null) 사람 표는 바이트 그대로다 — 새 필드는 이 기계 표면에만 산다.
# schema 3 = 2 + `label`(사람이 읽는 이름). 화면이 키 문자열을 행 이름으로 쓰지 않게 하려는 것이고,
# 문구의 단일 소유는 계속 이 파일이다(화면 상수 0).
# schema 4 = 3 + `unset`(안 채웠을 때 적용되는 상태 한 낱말). 기본값과 짝이라 둘 중 하나는 항상
# 값이고, 그래서 설정 화면에 빈 칸이 없다.
# schema 5 = 4 + `group_labels`(절 이름) · `field_labels`(한 줄로 묶는 군의 칸 이름) · 하네스
# 군의 `unit_labels` 전개 · 하네스가 키에 박힌 모델 칸의 조회 대상(`choices.fetch.value`). 화면에 영문 묶음 이름·같은 이름 12줄·
# 손입력 모델 칸이 남지 않게 하려는 것이고, 셋 다 이름의 주인이 이 표라는 같은 규칙이다.
# schema 6 = 5 + 행마다 `unit`(도구·모델·생각 깊이를 한 세트로 읽는 묶음 이름)·`governed_by`(그 절을
# 켜는 스위치와 꺼졌을 때의 한 줄)·`readonly`(엔진이 적는 키) · 문서에 `units`(묶음 이름)·
# `unit_fields`(묶음 칸 이름). 역할 군의 `field_labels` 와 줄 이름 `unit_labels` 는 `units` 로
# 옮겨 지웠고, `unset` 은 낱말이 아니라 안 채웠을 때 일어나는 일 한 문장이다. 없는 칸(설계·조사·
# 검토의 어려운 작업용, opencode 밖의 안내 문서 모델)은 전개하지 않는다.
KEY_JSON_SCHEMA = 6

# 모델 목록을 불러오는 조회 명령 — 화면은 `arg` 이름의 플래그에 버킷 값을 실어 부른다
# (`pm-config delegate models --harness <값> --json`). 명령 이름의 자리는 여기 하나다.
_MODEL_FETCH: dict[str, str] = {"command": "delegate models", "arg": "harness"}


def _axis_values(axis: str) -> dict[str, list[str]]:
    """값의 축 이름 → `{버킷: 고를 수 있는 값}`. 목록은 형제 선언에서 읽는다(여기 복제 0).

    `harness` 는 버킷이 없어 `""` 한 칸이고, `reasoning` 은 CLI 마다 ladder 가 달라 하네스가
    버킷이다. `model` 은 닫힌 목록이 없다(자유 문자열 + 조회 명령) — 빈 표다.
    """
    if axis == "harness":
        return {"": list(_harness_names())}
    if axis == "reasoning":
        return {harness: list(values) for harness, values in _reasoning_ladders().items()}
    if axis == "progress_signal":
        return {"": [kind for kind in _progress_signal_kinds() if kind]}
    return {}


def _progress_signal_kinds() -> tuple[str, ...]:
    """진행 출력 형식 세 값 — `pm_relay` 의 상수 셋에서 읽는다(값 복제 0).

    집합 선언(`PROGRESS_SIGNAL_KINDS`)은 생성자 호출이라 리터럴 판독이 닿지 않는다. 값 자체를
    들고 있는 상수 셋을 읽고, 화면에 세울 순서(강함 → 없음)는 여기서 정한다.
    """
    return tuple(
        _sibling_literal("pm_relay.py", name) or ""
        for name in ("PROGRESS_SIGNAL_EVENT_STREAM", "PROGRESS_SIGNAL_PLAINTEXT",
                     "PROGRESS_SIGNAL_NONE"))


def _choices_json(key: str) -> dict | None:
    """이 키가 고를 수 있는 값 — 화면이 읽는 규칙 한 벌 (축이 없는 키는 None).

    화면이 쓰는 규칙은 셋이다. 첫째 `bucket = depends_on ? (그 키의 현재 값 ?? "") : ""`,
    둘째 `list = values[bucket] ?? []`, 셋째 `fetch` 가 있으면 그 명령으로 받은 목록이 `list` 를
    대신하고 `free_text` 면 직접 입력도 연다. 판별자(`kind`)를 두지 않아 화면에 분기가 생기지
    않는다. 종속 키는 같은 세트의 `harness` 키다 — 세트를 통째로 쓰는 위임 키와 추가 리뷰어 키가
    같은 규칙으로 읽힌다.
    """
    spec = key_spec(key)
    axis = None if spec is None else spec.choices
    if axis is None:
        return None
    harness = _harness_of(key)
    fetch = dict(_MODEL_FETCH) if axis == "model" else None
    if fetch is not None and harness is not None:
        # 하네스가 키에 박힌 군은 종속 키를 볼 것이 없다 — 무엇으로 조회할지를 표가 실어 준다.
        fetch["value"] = harness
    depends_on = None
    if harness is None and axis not in ("harness", "progress_signal"):
        depends_on = f"{key.rsplit('.', 1)[0]}.harness"
    return {
        "depends_on": depends_on,
        "values": _axis_values(axis),
        "fetch": fetch,
        "free_text": axis == "model",
    }


def _harness_of(key: str) -> str | None:
    """키 자체에 하네스 이름이 박힌 군이면 그 이름 (그 밖은 None)."""
    return key.split(".")[1] if pattern_for(key) == HARNESS_PATTERN else None


# 한 줄로 묶이는 키군의 **줄 이름** — 화면이 `delegate.developer.hard` 같은 키를 그대로 이름으로
# 쓰지 않게 한다. 축(역할·티어)은 이 모듈이 이미 소유하고, 여기서는 그 축의 사람 말 이름만 준다.
_ROLE_LABELS = {
    "developer": "구현",
    "researcher": "조사",
    "architect": "설계",
    "code-reviewer": "검토",
}
# 한 줄로 묶이는 군의 **칸 이름** — 표 머리에 접미사(`harness`·`model`·`reasoning`)를 그대로
# 세우면 영문 코드 조각이 화면에 남는다. 칸 이름의 주인도 이 표다.
_ROLE_FIELD_LABELS = {
    "harness": "도구",
    "model": "모델",
    "reasoning": "생각 깊이",
}
_TIER_LABELS = {
    "": "보통",
    ".hard": "어려움",
    ".fallback": "대체",
    ".hard.fallback": "어려움 대체",
}


# 하네스 군의 **칸 이름** — 하네스 이름은 채택자가 고르지만 그 아래 네 칸은 엔진이 정한
# 닫힌 집합이라, 이름을 안 주면 같은 군 이름이 하네스마다 네 줄씩 반복된다(실측 12줄).
_HARNESS_SUFFIX_LABELS = {
    "idle_timeout": "응답 없을 때 대기 (초)",
    "wall_timeout": "최대 실행 시간 (초)",
    "ctx_window_tokens": "컨텍스트 크기 (토큰)",
    "pro_model": "안내 문서에 적을 모델",
}


# 작업 크기 세 값의 사람 이름 — 이름이 셋 다 같으면 화면에서 어느 줄인지 갈리지 않는다.
_ESTIMATE_LABELS = {"small": "작은 작업", "medium": "보통 작업", "large": "큰 작업"}


def _pattern_unit_labels(pattern: str) -> dict[str, str]:
    """키군 안에서 행마다 갈리는 이름 `<키> → 사람이 읽는 이름`. 안 갈리는 군은 빈 표다.

    군 선언의 이름 하나가 멤버 전부에 그대로 붙으면 같은 이름이 여러 줄에 서서 어느 줄이
    무엇인지 갈리지 않는다. 축을 이 모듈이 소유한 군만 여기서 갈라 준다 — 채택자가 이름을
    정하는 군(`test.<platform>`·`delegate.model_alias.<name>`)은 그 이름 자체가 이미 갈라
    주므로 지어내지 않는다. 한 줄로 묶이는 역할 군의 줄 이름은 여기가 아니라 `_units_json` 이다.

    `harness.<name>` 은 이름이 채택자 것이어도 **그 아래 칸은 엔진이 정한 닫힌 집합**이라
    여기서 갈라 준다 — 안 갈라 주면 하네스마다 같은 군 이름이 여러 줄씩 선다.
    """
    if pattern == HARNESS_PATTERN:
        return {f"harness.{name}.{suffix}": f"{name} {_HARNESS_SUFFIX_LABELS[suffix]}"
                for name in _harness_names() for suffix in _harness_suffixes(name)}
    if pattern == DIFF_CAP_PATTERN:
        return {f"diff_cap.{name}": f"{PATTERN_REGISTRY[pattern].label} ({label})"
                for name, label in _ESTIMATE_LABELS.items()}
    return {}


def _pattern_members(pattern: str) -> list[dict]:
    """패턴 키군의 전개된 실명과 각 기본값 — 축은 이 모듈이 이미 소유한 선언에서 온다.

    코드가 축을 갖지 않는 군은 빈 목록이다(지어내지 않는다): `test.<platform>.cmd` 의 축은 그
    프로젝트의 `qa.platforms` 값이고, `delegate.model_alias.<name>` 의 축은 채택자가 정하는
    이름이다. 그 두 군의 실키는 화면이 **conf 에 이미 있는 값**에서 읽는다.
    """
    if pattern == HARNESS_PATTERN:
        keys = [f"harness.{name}.{suffix}"
                for name in _harness_names() for suffix in _harness_suffixes(name)]
    elif pattern == DELEGATE_ROLE_PATTERN:
        keys = [f"delegate.{role}{tier}.{suffix}"
                for role in _role_names() for tier in _role_tiers(role)
                for suffix in _ROLE_SUFFIXES]
    elif pattern == DIFF_CAP_PATTERN:
        keys = [f"diff_cap.{name}" for name in DIFF_CAP_DEFAULTS]
    else:
        keys = []
    return [{"key": key, "default": default_for(key), "unset": unset_for(key),
             "choices": _choices_json(key), "recommended": recommended_for(key),
             **_row_axes(key)}
            for key in keys]


def _row_axes(key: str) -> dict:
    """행마다 싣는 schema 6 축 셋 — 묶음 이름·스위치·엔진이 적는 키인지."""
    spec = key_spec(key)
    return {"unit": unit_of(key), "governed_by": _governed_json(spec),
            "readonly": spec is not None and spec.readonly}


def unit_of(key: str) -> str | None:
    """도구·모델·생각 깊이를 **한 세트로 읽는** 묶음이면 그 이름(키의 마지막 점 앞), 아니면 None.

    세트는 통째로 읽힌다 — 역할 묶음은 `pm_delegate.resolve_delegate`, 교차 검토 묶음은
    `additional_reviewer.resolve_reviewer_target` 이 셋 중 하나만 있어도 나머지를 요구한다. 그래서
    화면은 이 묶음에 체크박스를 하나만 둔다(칸마다 두면 한 칸만 켠 반쪽 세트가 저장된다).
    """
    prefix, _, suffix = key.rpartition(".")
    if suffix not in _ROLE_SUFFIXES:
        return None
    if prefix == _REVIEWER_UNIT or pattern_for(key) == DELEGATE_ROLE_PATTERN:
        return prefix
    return None


def _units_json() -> dict[str, str]:
    """묶음 이름 → 줄 이름 (화면이 `delegate.developer.hard` 같은 키를 이름으로 세우지 않게)."""
    units = {f"delegate.{role}{tier}": f"{_ROLE_LABELS[role]}({_TIER_LABELS[tier]})"
             for role in _role_names() for tier in _role_tiers(role)}
    units[_REVIEWER_UNIT] = GROUP_LABELS[_REVIEWER_UNIT]
    return units


def _governed_json(spec: KeySpec | None) -> dict | None:
    """그 절을 켜는 스위치 — `on` 은 켜진 것으로 치는 값 목록이고 None 이면 "값이 있으면 켜짐".

    빈 값은 그 스위치의 기본값으로 읽는다(위임 사용은 기본 ON). 목록은 소비자 선언에서 읽는다.
    """
    if spec is None or spec.governed_by is None:
        return None
    master = spec.governed_by
    on = (list(_sibling_sequence("pm_delegate.py", "DELEGATE_ENABLED_VALUES"))
          if master == "delegate.enabled" else None)
    return {"key": master, "on": on, "reason": _MASTER_SWITCHES[master]}


def key_registry_json() -> dict:
    """`--keys --json` 산출 — 실명 키와 패턴 키군을 `kind` 로 가른 한 벌.

    `kind` 는 어느 레지스트리에서 왔는지다(`KeySpec` 에 그 필드는 없다 — 레지스트리가 둘이다).
    `groups` 는 `KEY_GROUPS` 순서 그대로이고 그것이 화면의 절 순서다.
    """
    keys: list[dict] = [
        {"key": key, "group": spec.group, "label": spec.label,
         "description": spec.description,
         "default": spec.default, "unset": spec.unset, "required": spec.required,
         "kind": "key", "members": None, "unit_labels": {},
         "choices": _choices_json(key), "recommended": recommended_for(key), **_row_axes(key)}
        for key, spec in KEY_REGISTRY.items()
    ]
    keys.extend(
        {"key": pattern, "group": spec.group, "label": spec.label,
         "description": spec.description,
         "default": spec.default, "unset": spec.unset, "required": spec.required,
         "kind": "pattern", "members": _pattern_members(pattern),
         "unit_labels": _pattern_unit_labels(pattern),
         "choices": None, "recommended": None,
         "unit": None, "governed_by": _governed_json(spec), "readonly": spec.readonly}
        for pattern, spec in PATTERN_REGISTRY.items()
    )
    return {"schema": KEY_JSON_SCHEMA, "groups": list(KEY_GROUPS),
            "group_labels": {group: GROUP_LABELS[group] for group in KEY_GROUPS},
            "units": _units_json(), "unit_fields": dict(_ROLE_FIELD_LABELS),
            "keys": keys}


def _declared_groups() -> list[str]:
    """실제 선언에 쓰인 묶음 이름 — 표시 순서는 `KEY_GROUPS` 하나가 정한다.

    `KEY_GROUPS` 밖의 묶음을 뒤에 덧붙이는 자리는 두지 않는다 — 완전성 테스트가 모든
    `spec.group` 이 `KEY_GROUPS` 안에 있음을 강제하므로 그 확장은 발화할 수 없다(새 묶음을
    쓰려면 `KEY_GROUPS` 에 순서를 선언하는 것이 유일한 경로다).
    """
    return [group for group in KEY_GROUPS
            if any(spec.group == group for spec in KEY_REGISTRY.values())]


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


def rename_updates(result: ConfResult) -> dict[str, str | None]:
    """이름만 바뀐 구표기 키를 신표기로 옮기는 **conf writer 입력** (값은 그대로 간다).

    이름을 바꾼 것은 갱신이지 채택자가 아니다 — 바꾼 쪽이 옮긴다. 갱신은 엔진 파일을
    배달하면서 이 계획도 함께 실행하므로(`pm_update` 이관 단계), 채택자가 손으로 두 줄을
    고칠 때까지 그 값을 쓰는 명령이 멈춰 서 있지 않는다.

    담지 **않는** 것 둘:
      - 대체 키가 없는 폐지 키(`None`) — 값이 갈 자리가 없어 옮기는 것이 곧 버리는 것이다.
        무엇을 버릴지는 사람이 정한다(교체 안내가 계속 지목한다).
      - 신표기에 이미 값이 있는 키 — 사람이 새 이름으로 명시한 값이 이긴다. 그때는 옛 줄만
        지운다(옛 값이 새 값을 덮어쓰지 않게).

    반환은 `{키: 값 또는 None}` 이고 `None` 은 그 줄을 지우라는 뜻이다. 옮길 것이 없으면 빈
    dict — 호출부가 그것을 "무대상" 으로 읽어 파일을 건드리지 않는다(멱등).
    """
    updates: dict[str, str | None] = {}
    for old, new in result.legacy.items():
        if new is None:
            continue
        updates[old] = None
        if new not in result.values:
            updates[new] = result.values[old]
    return updates


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

    **이름만 바뀐 키는 갱신이 옮긴다**(`pm_update` 이관 단계 · `rename_updates`) — 여기서는
    무엇이 옮겨졌는지 알릴 뿐 사람에게 시키지 않는다. 이름을 바꾼 것이 갱신이기 때문이다.

    사람이 정할 것은 대체 키가 없는 폐지 키뿐이다 — 값이 갈 자리가 없어 옮기는 것이 곧 값을
    버리는 것이라, 무엇을 버릴지는 기계가 정하지 않는다.
    """
    lines = [DELEGATE_DEFAULT_CHANGE_NOTICE]
    if not result.legacy:
        return lines
    where = f" ({result.path})" if result.path is not None else ""
    renamed = {old: new for old, new in result.legacy.items() if new is not None}
    retired = {old: new for old, new in result.legacy.items() if new is None}
    if renamed:
        lines.append(
            f"local.conf 키 이름이 바뀌었습니다{where} — 갱신이 값을 그대로 옮깁니다.")
        lines.extend(migration_lines(renamed))
    if retired:
        lines.append(
            f"아래 키는 대체가 없어 갱신이 옮기지 못합니다{where} — 지울지는 직접 정하세요. "
            "남아 있는 동안 그 값을 소비하는 명령은 fail-loud 로 멈춥니다.")
        lines.extend(migration_lines(retired))
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
    """패턴 키군 소속 여부 — 어느 군인지는 `pattern_for` 가 말한다."""
    return pattern_for(key) is not None


def pattern_for(key: str) -> str | None:
    """이 키가 속한 패턴 군의 표시 이름 (어느 군도 아니면 None)."""
    segments = key.split(".")
    if len(segments) < 2:
        return None
    head = segments[0]
    if head == "diff_cap":
        return DIFF_CAP_PATTERN if (
            len(segments) == 2 and segments[1] in DIFF_CAP_DEFAULTS) else None
    if head == "harness":
        return HARNESS_PATTERN if (
            len(segments) == 3 and segments[1] in _harness_names()
            and segments[2] in _harness_suffixes(segments[1])) else None
    if head == "delegate":
        return _delegate_pattern_for(segments[1:])
    if head == "test":
        return PLATFORM_TEST_PATTERN if _platform_command_name(key) else None
    return None


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


def _delegate_pattern_for(rest: list[str]) -> str | None:
    """`delegate.` 아래 닫힌 문법 — 역할 매핑 tuple 과 model_alias 표 (아니면 None)."""
    if not rest:
        return None
    if rest[0] == "model_alias":
        return DELEGATE_ALIAS_PATTERN if (len(rest) == 2 and rest[1]) else None
    if rest[0] not in _role_names():
        return None
    tail = rest[1:]
    if tail and tail[0] == "hard":
        if rest[0] not in _tiered_roles():
            return None                         # 티어가 없는 역할의 hard 칸은 어디서도 안 읽힌다
        tail = tail[1:]
    if tail and tail[0] == "fallback":
        tail = tail[1:]
    return DELEGATE_ROLE_PATTERN if (
        len(tail) == 1 and tail[0] in _ROLE_SUFFIXES) else None


def _role_names() -> tuple[str, ...]:
    """역할 목록 — `pm_delegate.ROLE_CHOICES` 파생(못 읽으면 빈 목록 → 모양 판정 강등)."""
    return _sibling_sequence("pm_delegate.py", "ROLE_CHOICES")


def _tiered_roles() -> tuple[str, ...]:
    """hard 티어를 갖는 역할 — `pm_delegate.TIERED_ROLES` 파생."""
    return _sibling_sequence("pm_delegate.py", "TIERED_ROLES")


def _role_tiers(role: str) -> tuple[str, ...]:
    """그 역할에 실제로 있는 티어 조합 (`_DELEGATE_TIERS` 표기) — 티어 없는 역할은 hard 를 뺀다."""
    if role in _tiered_roles():
        return _DELEGATE_TIERS
    return tuple(tier for tier in _DELEGATE_TIERS if ".hard" not in tier)


def _harness_suffixes(name: str) -> tuple[str, ...]:
    """그 도구에서 실제로 읽히는 접미 — 안내 문서 모델 칸은 렌더가 그 도구 토큰을 가질 때만 있다.

    모델 칸의 소비자는 안내 문서 렌더 하나다(`pm_render._OPERATIONAL_CONF_KEYS`). 그 표에 없는
    도구의 모델 칸은 적어도 어디서도 읽히지 않는다.
    """
    rendered = _sibling_literal("pm_render.py", "_OPERATIONAL_CONF_KEYS")
    rendered_keys = set(rendered.values()) if isinstance(rendered, dict) else set()
    return tuple(suffix for suffix in _HARNESS_SUFFIXES
                 if suffix != "pro_model" or f"harness.{name}.pro_model" in rendered_keys)


def _harness_names() -> tuple[str, ...]:
    """하네스 목록 — `pm_relay.HARNESS_CHOICES` 파생."""
    return _sibling_sequence("pm_relay.py", "HARNESS_CHOICES")


def _reasoning_ladders() -> dict[str, tuple[str, ...]]:
    """하네스별 추론 강도 허용집합 — `pm_relay.REASONING_ALLOWED` 파생."""
    return _sibling_mapping("pm_relay.py", "REASONING_ALLOWED")


_LITERAL_CACHE: dict[tuple[str, str], object] = {}


def _sibling_literal(filename: str, attribute: str):
    """형제 모듈의 최상위 리터럴 대입 하나 — **소스 텍스트만** 본다(import 0·순환 0).

    형제를 실행 import 하면 그 형제가 다시 conf 를 읽어 로드 순환이 된다. 필요한 것은 리터럴
    하나뿐이라 `ast` 로 해당 최상위 대입만 평가한다. 실패(부재·구형 사본·비리터럴)는 None 이고
    호출부가 빈 값으로 강등한다(막는 것이 없다 — 소비자는 advisory 판정과 표시용 선택지다).
    """
    cache_key = (filename, attribute)
    if cache_key in _LITERAL_CACHE:
        return _LITERAL_CACHE[cache_key]
    literal = None
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
            literal = _ast.literal_eval(node.value)
            break
    except Exception:  # noqa: BLE001 — 부재/손상/구형 사본은 advisory 판정의 정상 입력이다.
        literal = None
    _LITERAL_CACHE[cache_key] = literal
    return literal


def _sibling_sequence(filename: str, attribute: str) -> tuple[str, ...]:
    """형제 선언의 목록(표면 매핑이면 그 키) → 이름 tuple. 못 읽으면 빈 목록."""
    value = _sibling_literal(filename, attribute)
    if isinstance(value, dict):
        return tuple(str(key) for key in value)
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(str(item) for item in value)
    return ()


def _sibling_mapping(filename: str, attribute: str) -> dict[str, tuple[str, ...]]:
    """형제 선언의 표 → `{키: 값 tuple}`. 표가 아니거나 못 읽으면 빈 표.

    값이 목록인 칸만 싣는다. `frozenset({...})` 같은 **Call 노드**는 리터럴이 아니라 표 전체가 빈
    표로 강등되고 그 강등은 조용하다(선택지가 사라질 뿐 아무것도 막지 않는다) — 이 표로 읽히려는
    형제 선언이 리터럴이어야 하는 이유다(`pm_relay.REASONING_ALLOWED` 가 tuple 인 근거).
    """
    value = _sibling_literal(filename, attribute)
    if not isinstance(value, dict):
        return {}
    return {str(key): tuple(str(item) for item in values)
            for key, values in value.items()
            if isinstance(values, (list, tuple, set, frozenset))}


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
    """키 표(`--keys`)와 어댑터 생성 블록 출력 — 사람이 손으로 옮겨 적는 표면을 없앤다."""
    _console_encoding = _load_module_from_path(
        Path(__file__).resolve().with_name("console_encoding.py"),
        "console_encoding.py",
        verifier=_verify_engine_rev,
    )
    _console_encoding.configure_console_utf8()
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["--keys"] and args[1:] in ([], ["--json"]):
        if args[1:]:
            # 기계 판독 페이로드는 콘솔 텍스트 레이어를 건너뛴다 — 그 레이어는 PowerShell 캡처에서
            # 콘솔 codepage 로 강등되고 그 치환은 되돌릴 수 없다(사람 표는 종전 `print` 그대로).
            _console_encoding.write_machine_line(
                json.dumps(key_registry_json(), ensure_ascii=False))
        else:
            print(render_key_table())
        return 0
    if len(args) == 2 and args[0] == "--render-adapter-block":
        print(render_adapter_block(args[1]))
        return 0
    print("사용법: local_conf.py [--keys [--json] | --render-adapter-block <python|js|sh>]",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
