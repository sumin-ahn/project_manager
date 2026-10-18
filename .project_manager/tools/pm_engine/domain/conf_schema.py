"""local.conf 키 레지스트리·설정 어휘·판정 (층: domain).

`.project_manager/local.conf`(per-clone·git-ignored)를 읽는 엔진 구현이 도구마다 갈려 있었고
(9구현·7모듈), 같은 텍스트가 모듈마다 다른 dict 로 해소됐다(중복 키를 어떤 구현은 first-wins,
어떤 구현은 last-wins 로 읽었다). 이 모듈이 그 파싱 하나와 키 레지스트리 하나를 소유한다.

파싱 계약(이 모듈이 단일 진실 · 전 소비 지점 동일):
  - `KEY=value` 한 줄 · 앞뒤 공백은 걷는다.
  - `#` 로 시작하는 줄과 빈 줄은 무시 · `=` 없는 줄도 무시.
  - **값 안의 `#` 은 주석이 아니다**(inline 주석 없음 — 값 그대로 보존).
  - **중복 키는 last-wins** · 마지막 값이 빈 문자열이면 그 키는 `""`(설정 해제).
  - 파일 부재·`OSError`·`UnicodeError` → 빈 결과(fail-soft — conf 부재는 정상 형상이다).

판정은 파싱과 분리한다. `conf_store.load()` 는 **raise 하지 않고**, 구표기 잔존 판정은
`assert_no_legacy()` 한 호출이다. 이유는 `pm_update` 의 apply 경로다 — 구표기 conf 를 가진 채택자도
새 엔진 파일은 받아야 안내대로 고칠 수 있다(막으면 고칠 수단 없이 갇힌다). 그래서 apply 경로만
판정을 부르지 않고, 값을 **실제로 소비하는** 지점이 부른다.

파일 읽기는 `pm_engine.infra.conf_store`, 키 표·JSON·어댑터 블록 출력은 `pm_engine.cli.local_conf`
가 맡는다. 이 모듈은 파일을 열지 않는다.
"""
from __future__ import annotations

import re
from pathlib import Path


# ── 설정 어휘 (conf 키의 허용값·패턴을 만드는 상수 · 단일 정의) ─────────────
# 하네스 드라이버(`pm_relay`)·위임(`pm_delegate`)·안내 문서 렌더(`pm_render`)와 이 레지스트리가 같은
# 이름 집합을 쓴다. 정의는 여기 하나이고 그 모듈들은 이 모듈을 import 해 읽는다.

HARNESS_CHOICES: tuple[str, ...] = ("claude", "codex", "opencode")

# reasoning 드라이버별 정적 허용집합 — (codex-cli 0.145.0·claude 2.1.218). 허용집합 밖 지정은
# fail-loud(조용한 무시/강등 금지):
#   · codex(`-c model_reasoning_effort`): low/medium/high/xhigh/max — 0.145.0 xhigh 실측 수용(exec
#     앞/뒤 위치 무관 rc=0). `max` 는 추가 리뷰어 기본 프로필(gpt-5.6-sol)이 쓰는 ladder 상단이며,
#     codex 는 미지원값을 provider 로 넘기지 않고 CLI 에서 세우므로 허용집합에 명시 등재한다.
#   · claude(`--effort`): low/medium/high/xhigh/max — claude CLI 가 미지원값에 "Valid values: low,
#     medium, high, xhigh, max" 경고를 뱉어 **CLI-authoritative 실측**.
#   opencode 는 이 표에 없다. 추론 강도(카드 `variant`·cross `--variant`)는 그 모델의 `variants` 키
#   중 하나여야 적용되고, 없는 이름은 오류 없이 버려진다(1.18.5·1.18.34 실행 파일 판독). 모델마다
#   variants 가 다르므로(없음·low/medium/high 등) 허용값은 정적 표가 아니라 모델 목록 조회
#   (`opencode models --verbose`)의 `variants` 에서 얻는다 — `pm_relay.PER_MODEL_REASONING_HARNESSES`·
#   `pm_relay.reasoning_allowed`. 순서는 ladder 순이다(설정 화면의 선택지 순서).
REASONING_ALLOWED: dict[str, tuple[str, ...]] = {
    "codex": ("low", "medium", "high", "xhigh", "max"),
    "claude": ("low", "medium", "high", "xhigh", "max"),
}

# 드라이버 진행-신호 능력 선언 (분기 특례가 아니라 테이블). 무진행 판정은 "드라이버가 살아있음을
# 스트림으로 알릴 수 있는가"에만 의존한다 — 하니스별 if 분기를 만들지 않고 **능력을 선언**한다.
PROGRESS_SIGNAL_EVENT_STREAM = "event-stream"  # 줄 단위 이벤트(codex --json·opencode --format json·claude stream-json)
PROGRESS_SIGNAL_PLAINTEXT = "plaintext"        # 평문 증분(codex exec 기본 — 진행 로그가 stderr 로 흐름)
PROGRESS_SIGNAL_NONE = "none"                  # 종료 시 단일 덩어리 — 무진행 판정 불가 → 벽시계 유지

PROGRESS_SIGNAL_KINDS: frozenset[str] = frozenset(
    {PROGRESS_SIGNAL_EVENT_STREAM, PROGRESS_SIGNAL_PLAINTEXT, PROGRESS_SIGNAL_NONE}
)

ROLE_CHOICES: tuple[str, ...] = ("developer", "researcher", "architect", "code-reviewer")
# 티어(`--tier`·`delegate.<role>.hard.*`)를 갖는 역할 — 나머지 역할은 늘 normal 이다. 레지스트리도
# 이 표에서 hard 칸을 전개한다.
TIERED_ROLES: tuple[str, ...] = ("developer",)

# 위임 사용(`delegate.enabled`)이 켜진 것으로 치는 값(소문자·앞뒤 공백 제거 뒤) — 빈 값은 미설정이라
# 기본 ON 이다(`pm_delegate._is_enabled`).
DELEGATE_ENABLED_VALUES: tuple[str, ...] = ("true", "1", "yes", "on")

# 안내 문서 operational token-key → local.conf 키. 규칙(uppercase→lowercase)으로 유도할 수 없다 —
# conf 표기가 dot notation 이라 세그먼트 경계가 토큰 이름에 남아 있지 않다(`PROJECT_NAME` 은
# `project.name` 이고 `OPENCODE_PRO_MODEL` 은 `harness.opencode.pro_model`). 힌트가 실재하지 않는
# 키를 제시하면 채택자가 그 키를 conf 에 적고도 해소되지 않으므로 표로 고정한다(`pm_render`).
OPERATIONAL_CONF_KEYS: dict[str, str] = {
    "PROJECT_NAME": "project.name",
    "PROJECT_TAGLINE": "project.tagline",
    "PROJECT_ROOT": "project.root",
    "PY": "runtime.py",
    "TEST_CMD": "test.cmd",
    "DATE": "project.date",
    "OPENCODE_PRO_MODEL": "harness.opencode.pro_model",
}


# 이름만이 아니라 **값의 뜻**도 바뀐 이동 두 가지 — `rename_updates` 가 이 두 표로 값을 만든다.
#   · 남은 % → 쓴 % : 대상 새 키에 `100 − 옛 값` 을 쓴다.
#   · 공통 한도 → 하네스별 사용 한도 : 표시값 대신 등록 하네스마다 `harness.<name>.ctx_window_tokens` 에 쓴다.
_USED_PCT_FROM_REMAINING = frozenset({"ctx.checkpoint_at_pct", "ctx.precompact_at_pct"})
_PER_HARNESS_WINDOW_DISPLAY = "harness.<name>.ctx_window_tokens"


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
    # ctx 임계 — 옛 비율 여섯은 전부 **남은** % 였고 새 두 키는 사용 한도 대비 **쓴** % 다. 갱신이
    # 옮길 때 값도 `100 − 옛 값` 으로 바꾼다(`_USED_PCT_FROM_REMAINING` · `rename_updates`).
    # 옛 공통 한도 둘은 하네스별 사용 한도로 갈라졌다 — 표시값의 `<name>` 자리는 등록 하네스 전부다.
    "ctx_nudge_pct": "ctx.checkpoint_at_pct",
    "ctx_stop_pct": "ctx.precompact_at_pct",
    "ctx_window_tokens": _PER_HARNESS_WINDOW_DISPLAY,
    "ctx.nudge_pct": "ctx.checkpoint_at_pct",
    "ctx.stop_pct": "ctx.precompact_at_pct",
    "ctx.checkpoint_pct": "ctx.checkpoint_at_pct",
    "ctx.precompact_pct": "ctx.precompact_at_pct",
    "ctx.window_tokens": _PER_HARNESS_WINDOW_DISPLAY,
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
    # 제거 — 묶음 리뷰는 라운드 파일 판정으로 돌고 엔진이 리뷰 횟수를 세지 않는다(대체 키 없음).
    "delegate.code-reviewer.rounds_max": None,
    # 제거 — 작업 크기별 변경 줄 수 상한 축이 없어졌다(대체 키 없음). 남은 행은 지운다.
    "diff_cap.small": None,
    "diff_cap.medium": None,
    "diff_cap.large": None,
}

# **안내만 하고 막지는 않는 구키** — 읽는 코드가 이미 0인 키다(폴백이 먼저 폐지됐다).
# 값을 공급하지 않으므로 잔존해도 조용히 강등되는 축이 없고(불변식 6 의 대상이 아니다), 여기서
# 막으면 무해한 잔존 한 줄이 기존 채택자의 전 명령을 세운다. 교체 안내는 이 키들도 지목한다.
NON_BLOCKING_LEGACY_KEYS: frozenset[str] = frozenset({"session", "prefix"})

# suffix per-harness 표기(`ctx_window_tokens_<harness>`)는 접두 패턴으로 판정한다 — 구표기는
# 하네스명이 f-string 으로 붙는 **열린 집합**이었고, 신표기 `harness.<name>.ctx_window_tokens`
# 로 옮기면 `harness.<name>.*` 패턴에 흡수돼 닫힌 집합이 된다.
LEGACY_SUFFIX_PREFIX = "ctx_window_tokens_"


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
# (역할은 `ROLE_CHOICES` 표기 `code-reviewer`, 속성은 snake_case `idle_timeout`).
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
    이고 목록 자체는 담지 않는다 — 목록의 정본은 설정 어휘(`HARNESS_CHOICES`·`REASONING_ALLOWED`)와
    `pm_relay.MODEL_SOURCE` 라 여기 복제하면 두 자리가 갈린다. reasoning 은 정적 허용값
    버킷(claude·codex)이거나 모델 목록 조회 응답의 그 모델 variant(opencode)다. 고를 목록이 없는 키는
    `None` 이고, 선택지는 **검증이 아니다**(harness·reasoning 은 닫힌 집합, model 은 자유 문자열).
    """

    __slots__ = ("label", "description", "default", "required", "group", "choices", "unset",
                 "governed_by", "readonly")

    def __init__(self, label: str, description: str, *, default: str | None,
                 required: bool, group: str, choices: str | None = None,
                 unset: str | None = None, governed_by: str | None = None,
                 readonly: bool = False) -> None:
        # `label` = 사람이 읽는 이름(명사구·키 문자열을 옮겨 적지 않는다), `description` = 그 값이
        # 무엇을 바꾸는지. 설명·`unset`·스위치 사유(`_MASTER_SWITCHES`)는 합니다체 문장으로 쓰고
        # 마침표로 끝낸다. 이름은 명사구 그대로다 — 설정 화면 문구의 단일
        # 소유가 여기이고, 화면은 받아 그리기만 한다.
        # `unset` = 이 키를 **안 채웠을 때 실제로 일어나는 일**이다. 셋 중 하나로 쓴다:
        # 다른 값을 씀(`운영체제 기본 파이썬을 씁니다.`) · 그 기능이 돌지 않음(`교차 검토를 하지
        # 않습니다.`) · 그 명령이 멈춤(`이 역할로 위임하면 설정이 없다며 멈춥니다.`). 기본값이 있으면 그 값이 곧 답이라
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


# 패턴 키군 — `<name>` 자리에 들어갈 목록은 **코드가 소유**한다(손열거 0). 전개는 이 모듈의 설정
# 어휘에서 파생한다.
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
PLATFORM_TEST_PATTERN = "test.<platform>.cmd"

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

# 절을 켜는 키 → 꺼져 있을 때 그 절의 나머지 칸에 붙는 한 줄. 켜짐 판정은 소비자 것이다:
# 교차 검토 도구는 값이 있으면 켜짐(`additional_reviewer.resolve_reviewer_target`), 위임 사용은
# 빈 값이면 기본 ON 이고 그 밖에는 `DELEGATE_ENABLED_VALUES` 안이어야 켜짐이다.
_MASTER_SWITCHES: dict[str, str] = {
    "additional_reviewer.harness": "교차 검토 도구를 정해야 쓰입니다.",
    "delegate.enabled": "위임 사용이 켜져 있어야 쓰입니다.",
}

# 도구·모델·생각 깊이를 한 세트로 읽는 교차 검토 묶음의 이름(`ADDITIONAL_REVIEWER_KEYS` 의 접두).
_REVIEWER_UNIT = "additional_reviewer"

# 기본 시간을 도구마다 따로 가진 키가 안 채워졌을 때.
_TOOL_DEFAULT_TIME = "도구마다 정해진 기본 시간을 씁니다."
# 안내 문서에 박히는 키가 안 채워졌을 때 — 출하 안내 문서는 설치 때 이미 값이 박혀 평소엔
# 이 키를 다시 읽지 않는다. 그 자리가 남은 원본을 다시 그릴 때만 채우라며 멈춘다
# (`pm_render._assert_no_leak`).
_RENDER_ONLY_UNSET = "평소에는 영향이 없습니다. 안내 문서를 다시 그릴 때 이 자리가 남아 있으면 채우라며 멈춥니다."

KEY_REGISTRY: dict[str, KeySpec] = {
    "project.name": KeySpec(
        "프로젝트 이름",
        "안내 문서(CLAUDE.md 등)에 들어갈 이름입니다.",
        default=None, unset=_RENDER_ONLY_UNSET, required=False, group="project"),
    "project.tagline": KeySpec(
        "한 줄 소개",
        "안내 문서 첫머리에 들어갈 소개입니다.",
        default=None, unset=_RENDER_ONLY_UNSET, required=False, group="project"),
    "project.root": KeySpec(
        "기준 경로",
        "안내 문서에 적을 경로의 기준입니다.",
        default=None, unset=_RENDER_ONLY_UNSET, required=False, group="project"),
    "project.date": KeySpec(
        "기준 날짜",
        "안내 문서에 적을 날짜입니다.",
        default=None, unset=_RENDER_ONLY_UNSET, required=False, group="project"),
    "runtime.py": KeySpec(
        "파이썬 실행 파일",
        "엔진을 실행할 파이썬입니다(예: python3, Windows 는 py -3.12). 첫 설치 때 실행해 보고 채웁니다.",
        default=None, unset="운영체제 기본 파이썬(python3, Windows 는 py -3)을 씁니다.",
        required=True, group="runtime"),
    "test.cmd": KeySpec(
        "테스트 명령",
        "테스트를 돌릴 때 그대로 실행할 명령입니다.",
        default=None, unset="파이썬 기본 테스트 명령(python -m pytest tests/ -q)을 씁니다.",
        required=True, group="runtime"),
    "qa.platforms": KeySpec(
        "테스트할 운영체제 목록",
        "쉼표로 구분합니다(예: linux, windows). 적으면 운영체제마다 테스트 명령을 따로 정해야 하고, 적은 순서대로 실행합니다.",
        default=None, unset="운영체제 구분 없이 테스트 명령 하나만 실행합니다.", required=False, group="runtime"),
    "identity.user": KeySpec(
        "내 계정",
        "작업 기록에 남길 이름입니다.",
        default=None, unset="git 설정의 이메일을 씁니다. 그것도 없으면 이름 없이 기록합니다.",
        required=False, group="identity"),
    "upstream.path": KeySpec(
        "원본 위치",
        "업데이트를 받아올 폴더 경로나 주소입니다.",
        default=None, unset="받아올 곳이 없어 업데이트가 멈춥니다.", required=False, group="upstream"),
    "upstream.rev": KeySpec(
        "마지막으로 받은 커밋",
        "업데이트 차이를 재는 기준입니다. 업데이트가 적습니다.",
        default=None, unset="아직 업데이트를 받은 적이 없습니다.", required=False, group="upstream",
        readonly=True),
    "upstream.seen_rev": KeySpec(
        "마지막으로 확인한 커밋",
        "원본에서 마지막으로 본 커밋입니다. 업데이트가 적습니다.",
        default=None, unset="아직 원본을 확인한 적이 없습니다.", required=False, group="upstream",
        readonly=True),
    "ctx.checkpoint_at_pct": KeySpec(
        "중간 기록 권고 (%)",
        "도구별 사용 한도 대비 쓴 컨텍스트가 이 비율 이상이 되면 중간 기록을 권합니다. 작업은 계속합니다.",
        default="70", required=False, group="ctx"),
    "ctx.precompact_at_pct": KeySpec(
        "압축 임박 경고 (%)",
        "쓴 컨텍스트가 이 비율 이상이 되면 자동 압축이 곧 일어난다고 보고 마지막으로 중간 기록을 권합니다. 작업을 멈추거나 세션을 바꾸지는 않습니다.",
        default="80", required=False, group="ctx"),
    "ctx.compact_at_pct": KeySpec(
        "압축 (%)",
        "쓴 컨텍스트가 이 비율에 닿으면 자동 압축하도록 각 도구의 설정 파일에 압축 지점을 씁니다. Codex·opencode 는 그 지점에서, Claude Code 는 그보다 조금 앞에서 압축합니다.",
        default="90", required=False, group="ctx"),
    "delegate.enabled": KeySpec(
        "위임 사용",
        "끄면 다른 도구에 작업을 맡기지 않습니다.",
        default="true", required=False, group="delegate"),
    "delegate.timeout": KeySpec(
        "위임 최대 실행 시간 (초)",
        "이 시간을 넘으면 중단합니다.",
        default=None, unset=_TOOL_DEFAULT_TIME, required=False, group="delegate",
        governed_by="delegate.enabled"),
    "delegate.idle_timeout": KeySpec(
        "위임 응답 없을 때 대기 (초)",
        "이만큼 출력이 없으면 멈춘 것으로 봅니다.",
        default=None, unset=_TOOL_DEFAULT_TIME, required=False, group="delegate",
        governed_by="delegate.enabled"),
    "additional_reviewer.harness": KeySpec(
        "교차 검토 도구",
        "다른 모델로 한 번 더 검토할 때 쓸 도구입니다. 모델과 함께 채웁니다.",
        default=None, unset="교차 검토를 하지 않습니다.", required=False, group="additional_reviewer",
        choices="harness"),
    "additional_reviewer.model": KeySpec(
        "교차 검토 모델",
        "그 도구가 받는 표기 그대로 적습니다.",
        default=None, unset="교차 검토를 하지 않습니다.", required=False, group="additional_reviewer",
        choices="model"),
    "additional_reviewer.reasoning": KeySpec(
        "교차 검토 생각 깊이",
        "고를 수 있는 값은 도구마다 다릅니다.",
        default=None, unset="교차 검토를 하지 않습니다.", required=False, group="additional_reviewer",
        choices="reasoning"),
    "additional_reviewer.rounds_max": KeySpec(
        "교차 검토 최대 횟수",
        "한 건을 다시 볼 최대 횟수입니다.",
        default="2", required=False, group="additional_reviewer",
        governed_by="additional_reviewer.harness"),
    "additional_reviewer.incomplete_rounds_max": KeySpec(
        "교차 검토 재시도 횟수",
        "결과 없이 끝난 호출을 다시 부를 최대 횟수입니다.",
        default="2", required=False, group="additional_reviewer",
        governed_by="additional_reviewer.harness"),
    "additional_reviewer.wave_budget": KeySpec(
        "교차 검토 세션당 총 횟수",
        "한 세션에서 쓸 수 있는 전체 호출 수입니다.",
        default="24", required=False, group="additional_reviewer",
        governed_by="additional_reviewer.harness"),
    "additional_reviewer.paths": KeySpec(
        "교차 검토할 폴더",
        "쉼표나 공백으로 구분한 경로 목록입니다.",
        default="src/,tests/,scripts/,.project_manager/tools/",
        required=False, group="additional_reviewer", governed_by="additional_reviewer.harness"),
    "additional_reviewer.timeout": KeySpec(
        "교차 검토 최대 실행 시간 (초)",
        "이 시간을 넘으면 중단합니다.",
        default=None, unset=_TOOL_DEFAULT_TIME, required=False, group="additional_reviewer",
        governed_by="additional_reviewer.harness"),
    "additional_reviewer.idle_timeout": KeySpec(
        "교차 검토 응답 없을 때 대기 (초)",
        "이만큼 출력이 없으면 멈춘 것으로 봅니다.",
        default=None, unset=_TOOL_DEFAULT_TIME, required=False, group="additional_reviewer",
        governed_by="additional_reviewer.harness"),
    "additional_reviewer.progress_signal": KeySpec(
        "교차 검토 진행 출력 형식",
        "그 도구가 진행 상황을 어떤 모양으로 내는지 정합니다.",
        default=None, unset="도구에 맞춰 자동으로 고릅니다.", required=False, group="additional_reviewer",
        choices="progress_signal", governed_by="additional_reviewer.harness"),
}

PATTERN_REGISTRY: dict[str, KeySpec] = {
    HARNESS_PATTERN: KeySpec(
        "도구마다 따로 정하는 값",
        "실행 시간·대기 시간·사용 한도(압축 지점과 경고의 기준)와 안내 문서에 적을 모델 이름입니다. "
        "설정한 희망 기준을 보존하며, 실제 적용 기준은 독립 근거로 확인한 모델의 가용 한도 이하로 제한합니다.",
        default=None, unset=_TOOL_DEFAULT_TIME, required=False, group="harness"),
    DELEGATE_ROLE_PATTERN: KeySpec(
        "역할별 담당 도구",
        "역할마다 도구·모델·생각 깊이를 한 묶음으로 정합니다. 실패 시 대신 쓸 묶음을 따로 둘 수 있습니다(어려운 작업용 묶음은 구현만).",
        default=None, unset="이 역할로 위임하면 설정이 없다며 멈춥니다.", required=False,
        group="delegate", governed_by="delegate.enabled"),
    DELEGATE_ALIAS_PATTERN: KeySpec(
        "모델 별칭",
        "같은 모델의 다른 표기를 한 이름으로 묶습니다. 구현과 검토가 같은 모델인지 볼 때만 씁니다.",
        default=None, unset="표기가 다르면 다른 모델로 봅니다.", required=False, group="delegate"),
    PLATFORM_TEST_PATTERN: KeySpec(
        "운영체제별 테스트 명령",
        "위 '테스트할 운영체제 목록'에 적은 이름마다 하나씩 정합니다. 목록에 없는 이름은 모르는 항목으로 표시합니다.",
        default=None, unset="목록에 적은 운영체제면 테스트가 멈춥니다.", required=False, group="runtime"),
}

# 기존 호출부(`unknown_keys`·`_matches_pattern`)가 그대로 도는 파생 튜플 — 레지스트리가 정본이다.
KNOWN_KEYS: tuple[str, ...] = tuple(KEY_REGISTRY)


# `harness.<name>.*` 의 기본값은 접미마다 다르다 — 사용 한도만 한 값(모든 하네스 공통 200000)을
# 갖고, 나머지는 기본값이 없어 안 채웠을 때 일어나는 일을 적는다: 시간 두 축은 하네스 프로필이
# 받고, 모델 이름은 안내 문서 렌더가 자리를 TODO 로 남긴다(`pm_render.neutralize_model_todo`).
_HARNESS_MEMBER_DEFAULTS: dict[str, str] = {
    "ctx_window_tokens": "200000",
}
_HARNESS_MEMBER_UNSET: dict[str, str] = {
    "idle_timeout": _TOOL_DEFAULT_TIME,
    "wall_timeout": _TOOL_DEFAULT_TIME,
    "pro_model": "안내 문서의 모델 자리를 TODO 로 남깁니다.",
}

# 역할 묶음이 안 채워졌을 때 — 티어마다 다르다(`pm_delegate.resolve_delegate`·`resolve_fallback`).
_DELEGATE_TIER_UNSET: dict[str, str] = {
    "": "이 역할로 위임하면 설정이 없다며 멈춥니다.",
    ".hard": "어려운 작업 위임이 멈춥니다. 보통 묶음으로 대신하지 않습니다.",
    ".fallback": "실패하면 대신 쓰지 않고 실패로 끝납니다.",
    ".hard.fallback": "실패하면 대신 쓰지 않고 실패로 끝납니다.",
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
                       default=_HARNESS_MEMBER_DEFAULTS.get(suffix), required=spec.required,
                       group=spec.group, unset=_HARNESS_MEMBER_UNSET.get(suffix),
                       choices="model" if suffix == "pro_model" else None)
    return spec
CTX_BUDGET_SCHEMA = 5
CTX_HARNESSES = ("claude", "codex", "opencode")
CTX_INPUT_KEYS = tuple(f"harness.{name}.ctx_window_tokens" for name in CTX_HARNESSES) + (
    "ctx.checkpoint_at_pct", "ctx.precompact_at_pct", "ctx.compact_at_pct")
_CTX_ENTRY_INTEGERS = ("W", "C", "E", "P", "checkpoint", "precompact")
_CTX_RERUN = "`pm_config.py apply` 를 다시 실행하십시오"
# 모델 가용 한도 출처(basis)의 형식 — Claude Code 조회 · Codex 모델 목록 · opencode 모델 목록(input 은 원본에 있을 때만).
_CTX_BASIS_FORMS = ({"model_window", "auto_compact_threshold"}, {"context_window", "max_context_window", "model_context_window", "percent"},
                    {"context", "output", "source"}, {"context", "output", "source", "input"})


class CtxBudgetError(ValueError):
    """압축 한도 기록을 쓸 수 없다 — 부재·손상·이전 형식·도구 기록 없음·local.conf 입력 불일치."""


def ctx_input_values(values):
    return {key: str(values.get(key) or default_for(key)).strip() for key in CTX_INPUT_KEYS}


def _ctx_positive(value) -> bool:
    return type(value) is int and value > 0


def _ctx_model_name(model: dict) -> str:
    return f"{model['provider']}/{model['id']}"


def _ctx_budget_view(record, entry, harness, session_model):
    """세션 모델은 기록 모델의 id 나 `provider/id` 와 같아야 한다(도구마다 세션이 알리는 형식이 다르다)."""
    names = [model["id"] for model in entry["models"]]
    warning = None
    if session_model is not None and session_model not in names + [_ctx_model_name(model) for model in entry["models"]]:
        warning = f"{harness} 모델이 바뀌었습니다 (기록 {', '.join(names)} · 현재 {session_model}) — {_CTX_RERUN}."
    inputs = record["inputs"]
    return {"E": entry["E"], "P": entry["P"], "checkpoint": entry["checkpoint"],
            "precompact": entry["precompact"], "checkpoint_at_pct": int(inputs["ctx.checkpoint_at_pct"]),
            "precompact_at_pct": int(inputs["ctx.precompact_at_pct"]), "models": names,
            "applied_at": record["applied_at"], "warning": warning}


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


class ConfResult:
    """`load()` 의 반환 — 파싱값 + 구표기 잔존 + (지연 계산) 모르는 키.

    (평범한 클래스 — `@dataclass` 미사용: 엔진 도구는 `spec_from_file_location` 으로 로드되는데
    `from __future__ import annotations` 와 결합 시 모듈이 `sys.modules` 에 등록 안 돼 있으면
    dataclass 처리가 깨진다 — `identity_args.Identity` 와 같은 회피 관용구다.)

    `unknown` 은 **속성 접근 시점에 계산**한다. 값만 쓰는 소비 지점이 패턴 전개 판정을 치르지 않게
    한다.
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
    """이름이 바뀐 구표기 키를 신표기로 옮기는 **conf writer 입력**.

    이름을 바꾼 것은 갱신이지 채택자가 아니다 — 바꾼 쪽이 옮긴다. 갱신은 엔진 파일을
    배달하면서 이 계획도 함께 실행하므로(`pm_update` 이관 단계), 채택자가 손으로 두 줄을
    고칠 때까지 그 값을 쓰는 명령이 멈춰 서 있지 않는다.

    값은 대개 그대로 가고, 뜻이 바뀐 두 이동만 값을 만든다:
      - 남은 % → 쓴 %(`_USED_PCT_FROM_REMAINING`) — 새 키에 `100 − 옛 값` 을 쓴다. 옮길 옛 값이
        정수가 아니면 그 키 이름을 담은 `ValueError` 로 멈춘다(지어낸 값으로 옮기지 않는다).
      - 공통 한도 → 하네스별 사용 한도(`_PER_HARNESS_WINDOW_DISPLAY`) — 등록 하네스 전부의
        `harness.<name>.ctx_window_tokens` 중 값이 없는 키에 같은 값을 쓴다. 같은 계획 안에서는
        하네스 전용 구키(`ctx_window_tokens_<name>`)가 공통 키를 이긴다.

    담지 **않는** 것 셋:
      - 대체 키가 없는 폐지 키(`None`) — 값이 갈 자리가 없어 옮기는 것이 곧 버리는 것이다.
        무엇을 버릴지는 사람이 정한다(교체 안내가 계속 지목한다).
      - 신표기에 이미 값이 있는 키 — 사람이 새 이름으로 명시한 값이 이긴다. 그때는 옛 줄만
        지운다(옛 값이 새 값을 덮어쓰지 않게). 버려질 옛 값은 변환하지도 검사하지도 않는다 —
        정수가 아닌 옛 값이 명시된 새 값의 이전을 멈추지 않게.
      - 옛 키가 없는 새 키 — 옮길 값이 없으면 새 줄도 만들지 않는다(기본값은 레지스트리가 준다).

    여러 옛 이름이 같은 새 키로 가면 표(`LEGACY_KEY_MAP`)에서 뒤에 선언된 이름(더 최근 이름)이
    이긴다 — conf 줄 순서에 결과가 달려 있지 않게 한다. 뜻이 바뀐 두 이동에서 옛 값이 비어
    있으면(설정 해제) 옛 줄만 지운다.

    반환은 `{키: 값 또는 None}` 이고 `None` 은 그 줄을 지우라는 뜻이다. 옮길 것이 없으면 빈
    dict — 호출부가 그것을 "무대상" 으로 읽어 파일을 건드리지 않는다(멱등).
    """
    declared = {key: index for index, key in enumerate(LEGACY_KEY_MAP)}
    updates: dict[str, str | None] = {}
    shared_window: str | None = None
    for old in sorted(result.legacy, key=lambda key: declared.get(key, len(declared))):
        new = result.legacy[old]
        if new is None:
            continue
        updates[old] = None
        value = result.values[old]
        if new == _PER_HARNESS_WINDOW_DISPLAY:
            shared_window = value.strip() or shared_window
            continue
        if new in result.values:
            continue
        if new in _USED_PCT_FROM_REMAINING:
            if not value.strip():
                continue
            value = _used_pct_from_remaining(old, value)
        updates[new] = value
    if shared_window is not None:
        for harness in HARNESS_CHOICES:
            key = f"harness.{harness}.ctx_window_tokens"
            if key not in result.values and key not in updates:
                updates[key] = shared_window
    return updates


def _used_pct_from_remaining(key: str, value: str) -> str:
    """옛 남은 % 값 → 새 쓴 % 값(`100 − 값`). 정수가 아니면 그 키 이름으로 멈춘다."""
    text = value.strip()
    if not re.fullmatch(r"-?[0-9]+", text):
        raise ValueError(
            f"local.conf `{key}={value}` 는 정수가 아니라 쓴 % 로 옮길 수 없습니다 — "
            "값을 정수로 고치거나 줄을 지운 뒤 다시 실행하십시오.")
    return str(100 - int(text))


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
            f"local.conf 키 이름이 바뀌었습니다{where} — 갱신이 새 이름으로 옮깁니다(남은 % 는 "
            "쓴 % 로, 공통 사용 한도는 하네스마다 같은 값으로).")
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
    if head == "harness":
        return HARNESS_PATTERN if (
            len(segments) == 3 and segments[1] in HARNESS_CHOICES
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
    if rest[0] not in ROLE_CHOICES:
        return None
    tail = rest[1:]
    if tail and tail[0] == "hard":
        if rest[0] not in TIERED_ROLES:
            return None                         # 티어가 없는 역할의 hard 칸은 어디서도 안 읽힌다
        tail = tail[1:]
    if tail and tail[0] == "fallback":
        tail = tail[1:]
    return DELEGATE_ROLE_PATTERN if (
        len(tail) == 1 and tail[0] in _ROLE_SUFFIXES) else None


def _role_tiers(role: str) -> tuple[str, ...]:
    """그 역할에 실제로 있는 티어 조합 (`_DELEGATE_TIERS` 표기) — 티어 없는 역할은 hard 를 뺀다."""
    if role in TIERED_ROLES:
        return _DELEGATE_TIERS
    return tuple(tier for tier in _DELEGATE_TIERS if ".hard" not in tier)


def _harness_suffixes(name: str) -> tuple[str, ...]:
    """그 도구에서 실제로 읽히는 접미 — 안내 문서 모델 칸은 렌더가 그 도구 토큰을 가질 때만 있다.

    모델 칸의 소비자는 안내 문서 렌더 하나다(`OPERATIONAL_CONF_KEYS`). 그 표에 없는
    도구의 모델 칸은 적어도 어디서도 읽히지 않는다.
    """
    rendered_keys = set(OPERATIONAL_CONF_KEYS.values())
    return tuple(suffix for suffix in _HARNESS_SUFFIXES
                 if suffix != "pro_model" or f"harness.{name}.pro_model" in rendered_keys)
