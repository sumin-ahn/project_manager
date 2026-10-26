"""설치 공통 render_rules (domain)."""
from __future__ import annotations

from pm_engine.domain import conf_schema
import re

# 하네스 agent 카드의 역할별 모델/추론 토큰 → local.conf 해소 키. **위임 토큰의 단일 표**다
# (하네스별 사본 금지): claude `.claude/agents/*.md`·codex `.codex/agents/*.toml`·opencode
# `.opencode/agents/*.md` 가 같은 토큰 이름을 쓰고 같은 conf 키에서 해소된다. 카드의 model 은
# `delegate.<role>[.<tier>].{model,reasoning}` 의 렌더 파생물이고, 그 밖의 출처를 갖지 않는다.
#
# hard 티어(`developer-hard`)는 **별도 완전 세트**다 — normal 프로필을 상속하지 않는다(엔진의
# 프로필 해소와 같은 규칙). reasoning 토큰은 그 값을 실제로 소비하는 카드가 있는 것만 둔다 —
# 소비처 없는 토큰은 배선만 늘리고 해소를 못 검증한다. 추론 토큰 소비는 claude `effort`·codex
# `model_reasoning_effort`·opencode `variant` 이고, 필드 이름은 카드 **형식**이 정한다
# (CARD_FIELD_TOKEN_PREFIXES).
#
# pm_update 는 이 표를 **역전해** local.conf 채널 배선을 만든다(`_local_conf_operational_map`) —
# 손으로 재타이핑한 사본을 두면 새 역할/티어가 한쪽에만 늘어 조용히 미배선된다.
DELEGATE_MODEL_CONF_KEYS: dict[str, str] = {
    "DELEGATE_MODEL_DEVELOPER": "delegate.developer.model",
    "DELEGATE_REASONING_DEVELOPER": "delegate.developer.reasoning",
    "DELEGATE_MODEL_DEVELOPER_HARD": "delegate.developer.hard.model",
    "DELEGATE_REASONING_DEVELOPER_HARD": "delegate.developer.hard.reasoning",
    "DELEGATE_MODEL_RESEARCHER": "delegate.researcher.model",
    "DELEGATE_REASONING_RESEARCHER": "delegate.researcher.reasoning",
    "DELEGATE_MODEL_ARCHITECT": "delegate.architect.model",
    "DELEGATE_REASONING_ARCHITECT": "delegate.architect.reasoning",
    "DELEGATE_MODEL_CODE_REVIEWER": "delegate.code-reviewer.model",
    "DELEGATE_REASONING_CODE_REVIEWER": "delegate.code-reviewer.reasoning",
}


# 어댑터 네임스페이스 디렉터리 → (토큰 접두 → 그 값을 실을 카드 필드). **카드 형식이 소비 기대를
# 정한다**(하네스별 예외 아님): 형식은 카드가 놓인 어댑터 디렉터리가 정하므로 키도 그 디렉터리다.
# `.claude` frontmatter 는 `model`·`effort`, `.codex` TOML 은 `model`·`model_reasoning_effort`,
# `.opencode` frontmatter 는 `model`·`variant` 를 싣는다. 키 집합은 CARD_HARNESS_BY_ADAPTER_DIR 의 키
# 집합과 같다. 소비 가드·중화 줄 판정(`_MODEL_FIELD_LINE_RE`)·렌더의 추론 값 검증이 이 선언 하나를 읽는다.
CARD_FIELD_TOKEN_PREFIXES: dict[str, dict[str, str]] = {
    ".claude": {"DELEGATE_MODEL_": "model", "DELEGATE_REASONING_": "effort"},
    ".codex": {"DELEGATE_MODEL_": "model", "DELEGATE_REASONING_": "model_reasoning_effort"},
    ".opencode": {"DELEGATE_MODEL_": "model", "DELEGATE_REASONING_": "variant"},
}


def _harness_conf_key(conf_key: str) -> str:
    """`delegate.<role>[.<tier>].{model,reasoning}` → 같은 프로필의 `.harness` 키 (원자 tuple)."""
    return conf_key.rsplit(".", 1)[0] + ".harness"


# 같은 역할/티어의 하네스 키 — 위 표에서 **파생**한다(사본 0). 카드가 선언하는 하네스와 이 값이
# 다르면 그 카드는 미사용 프로필이다(아래 `unused_delegate_profiles`).
DELEGATE_HARNESS_CONF_KEYS: dict[str, str] = {
    token_key: _harness_conf_key(conf_key)
    for token_key, conf_key in DELEGATE_MODEL_CONF_KEYS.items()
}


# 어댑터 네임스페이스 디렉터리 → 하네스 이름. 카드가 **어느 하네스 프로필인가**의 단일 판정원
# (렌더 소스 경로가 그 사실을 들고 있다 — 토큰 이름엔 하네스가 없다).
CARD_HARNESS_BY_ADAPTER_DIR: dict[str, str] = {
    ".claude": "claude",
    ".codex": "codex",
    ".opencode": "opencode",
}


# operational — import sed 치환된 리터럴 (local.conf 재유도). plain replace·omit 없음.
# adapter_specs.OPERATIONAL_TOKENS(중괄호 포함)와 동일 집합을 bare key 로 + opencode 전용
# OPENCODE_PRO_MODEL(opencode 채택자 local.conf 만 보유·claude tree 엔 토큰 부재 → no-op).
#
# ⚠️ forward-flag (@render 활성화 시점): local.conf(board.py init 산출)는 이 중
#    일부만 보유한다 — DATE·PROJECT_ROOT·PROJECT_TAGLINE 는 init 이 채우지 않을 수 있어
#    app.update_render._operational_from_local_conf 가 그 token-key 를 dict 에 안 넣는다. 엄격 가드
#    (_assert_no_leak·토큰 0) 하에선, @render 활성화된 어댑터 파일이 그런 미보유 operational
#    토큰을 *담고 있으면 렌더가 실패한다 — 그게 옳다*(미해소를 침묵 출하 대신 표면화). 현재
#    @render path 0 이라 안 깨진다. 활성화 시점에 그 파일들의 operational 해소(또는
#    local.conf 채널 확장)를 보장하는 건 몫. 이 엔진은 leak 을 표면화할 뿐 채우지 않는다.
OPERATIONAL_KEYS: tuple[str, ...] = (
    "PROJECT_NAME",
    "PROJECT_TAGLINE",
    "PROJECT_ROOT",
    "PY",
    "TEST_CMD",
    "DATE",
    # opencode 어댑터 전용 — pm_import 가 local.conf 에 harness.opencode.pro_model 을 기록.
    # local.conf 에 해소돼 있으면 여기 operational 채널로 plain replace(정상 치환). *미해소*(채택자가
    # opencode 없이 import·TODO 폴백)면 leak 이 아니라 intentional-TODO 로 graceful 중화한다
    # (neutralize_model_todo·import 대칭) — 아래 render_adapter 참조.
    "OPENCODE_PRO_MODEL",
    # 하네스 agent 카드 전용 — 역할별 위임 모델/추론(`delegate.<role>[.<tier>].*` 해소값).
    # 스폰의 실효 모델은 카드 frontmatter/TOML 이라, 카드가 리터럴이면 `delegate.*` 선언이
    # 실행면에 닿지 못한다(채택자 제보 — 선언 sonnet ↔ 실행 opus). 역할별 개별 토큰이라야
    # 역할마다 다른 모델을 표현할 수 있다. 미해소(local.conf 키 부재)면 OPENCODE_PRO_MODEL 과
    # 같은 intentional-TODO 중화(neutralize_delegate_model_todo) — rc-fail 하지 않는다.
    # 목록은 DELEGATE_MODEL_CONF_KEYS 에서 **파생**한다(한 표·사본 0).
    *DELEGATE_MODEL_CONF_KEYS,
)


# 잔여 leak 스캔 — 대문자/언더스코어 토큰 (post-render assertion·잔존 시 무조건 raise).
_ANY_TOKEN_RE = re.compile(r"\{\{([A-Z_]+)\}\}")


# stray omit-marker 스캔 — 산출물에 잔존한 옛 free-form drop-section 마커는 무음 출하 금지.
# `<!-- pm:omit-if-empty KEY -->` 류는
# 이제 *어느 어댑터에도 없어야 한다*(어댑터 free-form-free). 잔존 시 미마이그레이션
# 신호로 leak 처리한다(침묵 출하 대신 표면화).
_STRAY_MARKER_RE = re.compile(r"<!--\s*/?pm:omit-if-empty\b[^>]*-->")


# ── intentional-TODO placeholder (import↔self-update 대칭) ──────────────
# `{{OPENCODE_PRO_MODEL}}` 은 채택자가 opencode 없이/모델조회 실패로 import 하면 *결정적으로
# 해소 못 하는* 토큰이다 — import(--fill manual)는 이걸 leak(자족 위반)으로 터뜨리지 않고 YAML
# `model:` 필드 줄을 주석화하며 토큰을 `<provider/model>` 형식힌트로 *중화*해 graceful 하게
# 넘긴다(채택자가 `opencode models`/손으로 나중에 채움).
# self-update(pm_update)의 @render 재렌더도 같은 소스 토큰을 만나므로,
# render_adapter 가 이 중화를 *공유 경로*로 수행해 import 와 대칭이 된다(byte-동일 산출 → 재렌더
# 왕복 0). 이 예외는 **OPENCODE_PRO_MODEL 의 model: 필드 줄에만** 적용된다 — 그 밖의 미해소
# 토큰(다른 위치의 OPENCODE_PRO_MODEL 포함·다른 `{{...}}`)은 계속 _assert_no_leak 가 fail-loud
# (false-green 근절 유지·불변식 c).
OPENCODE_MODEL_TOKEN = "{{OPENCODE_PRO_MODEL}}"


_OPENCODE_MODEL_KEY = "OPENCODE_PRO_MODEL"  # bare operational key — all_empty 판정용(빈값 vs 부재)


_MODEL_TODO_PLACEHOLDER = "<provider/model>"


# 중화 대상 줄 = **카드 필드 줄**. 하네스마다 필드 이름과 대입 표기(YAML `:` · TOML `=`)가 다르나
# 판정은 하나여야 import 와 self-update 가 같은 산출을 낸다. 필드 이름은 CARD_FIELD_TOKEN_PREFIXES
# 의 합집합에서 파생한다(손열거 사본 0 — 표에 필드가 늘면 중화 대상도 같이 는다). 긴 이름부터
# 나열해 패턴이 결정적이다. 산문/헤더의 토큰은 비대상(주석화하면 markdown/TOML 이 깨진다) — 그
# 위치의 미해소 토큰은 계속 `_assert_no_leak` 가 표면화한다.
_CARD_FIELD_NAMES: tuple[str, ...] = tuple(sorted(
    {field for fields in CARD_FIELD_TOKEN_PREFIXES.values() for field in fields.values()},
    key=lambda field: (-len(field), field),
))


_MODEL_FIELD_LINE_RE = re.compile(
    r"^(?:" + "|".join(re.escape(field) for field in _CARD_FIELD_NAMES) + r")\s*[:=]"
)


_DELEGATE_MODEL_TODO_PLACEHOLDER = "<model>"


_DELEGATE_REASONING_TODO_PLACEHOLDER = "<reasoning>"


# 추론 토큰 접두사 — 중화 placeholder 를 값의 종류에 맞춘다(`<model>` 자리에 추론 등급을 쓰면
# 채택자가 무엇을 채울지 오해한다).
_DELEGATE_REASONING_TOKEN_PREFIX = "DELEGATE_REASONING_"


def _delegate_todo_placeholder(token_key: str) -> str:
    """중화된 줄에 남길 형식힌트 — 모델 토큰은 `<model>`, 추론 토큰은 `<reasoning>`."""
    if token_key.startswith(_DELEGATE_REASONING_TOKEN_PREFIX):
        return _DELEGATE_REASONING_TODO_PLACEHOLDER
    return _DELEGATE_MODEL_TODO_PLACEHOLDER


def card_harness_from_path(card_path: str | None) -> str | None:
    """**인스턴스 상대** 카드 경로 → 그 카드가 속한 하네스 (판정 불가면 None).

    판정은 상대경로의 **첫 컴포넌트**만 본다 — 어댑터 네임스페이스(`.claude`/`.codex`/
    `.opencode`)는 인스턴스 루트 바로 아래에 있고, 그 밖의 위치에 같은 이름이 나타나면 그건
    네임스페이스 선언이 아니다. 절대경로 전체를 훑으면 체크아웃이 `~/.claude/...` 아래일 때
    어댑터 밖 파일(공유 wiki·스킬 등)까지 그 하네스로 오분류된다(가드 시야 == 판정 표면).

    경로가 없거나(인메모리 렌더·호출부 미배선) 어댑터 밖이면 None — 그때는 하네스 판정을
    요구하는 규칙(미사용 프로필)이 발화하지 않고 현행 해소가 그대로 간다.
    """
    adapter_dir = _card_adapter_dir(card_path)
    return CARD_HARNESS_BY_ADAPTER_DIR[adapter_dir] if adapter_dir else None


def _card_adapter_dir(card_path: str | None) -> str | None:
    """**인스턴스 상대** 카드 경로 → 그 카드의 어댑터 네임스페이스 디렉터리 (판정 불가면 None).

    `card_harness_from_path` 와 같은 첫 컴포넌트 판정이다 — 카드 필드 표(CARD_FIELD_TOKEN_PREFIXES)
    조회 키가 이 디렉터리다."""
    if not card_path:
        return None
    rel = str(card_path).replace("\\", "/").lstrip("/")
    if not rel:
        return None
    first = rel.split("/", 1)[0]
    return first if first in CARD_HARNESS_BY_ADAPTER_DIR else None


def unused_delegate_profiles(
    card_path: str | None, delegate_harness: dict[str, str] | None
) -> dict[str, str]:
    """이 카드에서 **미사용 프로필**인 위임 토큰 → 그 역할의 conf 하네스.

    카드의 하네스(`card_harness_from_path`)와 conf 의 `delegate.<role>[.<tier>].harness` 가
    다르면 그 카드는 이번 형상에서 스폰되지 않는 프로필이다 — 다른 하네스용 모델 값을 억지로
    박으면 카드가 conf 와 어긋난 사실(그 역할은 저쪽 하네스로 간다)을 감춘다. 값을 채우는 대신
    intentional-TODO 로 중화하고 이유를 꼬리에 남긴다(rc 불변).

    conf 하네스를 모르면(키 부재) 판정하지 않는다 — 미설정은 오설정이 아니라 정상 형상이고,
    그때는 model 키가 있으면 그대로 해소한다(현행 거동 보존).
    """
    card_harness = card_harness_from_path(card_path)
    if not card_harness or not delegate_harness:
        return {}
    return {
        token_key: harness
        for token_key, harness in delegate_harness.items()
        if harness and harness != card_harness and token_key in DELEGATE_MODEL_CONF_KEYS
    }


def _local_conf_key(token_key: str) -> str:
    """operational token-key → local.conf 키 — 진단 힌트가 실재하지 않는 키를 제시하지 않게.

    고정 표 두 개를 본다 — operational(`conf_schema.OPERATIONAL_CONF_KEYS`)과 역할별 모델 토큰
    (`DELEGATE_MODEL_CONF_KEYS`·`DELEGATE_MODEL_DEVELOPER`→`delegate.developer.model`). 둘 다
    없는 토큰은 lowercase 로 떨어뜨린다(신설 토큰이 표에 안 실린 경우의 최소 힌트).
    """
    if token_key in conf_schema.OPERATIONAL_CONF_KEYS:
        return conf_schema.OPERATIONAL_CONF_KEYS[token_key]
    return DELEGATE_MODEL_CONF_KEYS.get(token_key, token_key.lower())


def _delegate_model_todo_tail(conf_key: str) -> str:
    """중화된 `# model:` 줄 꼬리의 TODO 안내 — 채울 local.conf 키와 재렌더 경로를 명시한다."""
    return (f"  # TODO: local.conf `{conf_key}=` 를 채우고 pm-update 를 다시 실행하면 "
            "이 줄이 그 값으로 렌더된다")


def _delegate_unused_profile_tail(
    conf_key: str, conf_harness: str, card_harness: str | None
) -> str:
    """미사용 프로필로 중화한 줄의 꼬리 — 값이 아니라 **이유**를 남긴다.

    conf 가 이 역할을 다른 하네스로 보내고 있으므로 이 카드는 이번 형상에서 스폰되지 않는다.
    채택자가 이 카드를 쓰려면 고칠 곳은 카드가 아니라 local.conf 의 하네스 키다(단일 진실).
    """
    card = f" (이 카드는 {card_harness})" if card_harness else ""
    return (f"  # TODO: conf 의 이 역할 하네스는 {conf_harness} 라 미사용 프로필이다{card} — "
            f"local.conf `{_harness_conf_key(conf_key)}` 를 이 하네스로 바꾸면 "
            f"이 줄이 `{conf_key}` 값으로 렌더된다")


def _model_todo_tail(available: list[str] | None = None) -> str:
    """중화된 `# model:` 줄 꼬리의 TODO 안내(채택자 발견경로). 조회된 가용 모델은 인라인한다.

    pm_import 의 TODO 폴백(_mark_model_todos)과 **byte-동일** 문구여야 한다 — import 와
    self-update 가 같은 산출을 내야 재렌더가 spurious diff 를 안 만든다(단일 진실).
    """
    if available:
        return ("  # TODO: opencode 모델 ID 를 넣으려면 이 줄 주석 해제 후 provider/model 로 치환 "
                f"(가용: {', '.join(available)})")
    return ("  # TODO: opencode 모델 ID 를 넣으려면 이 줄 주석 해제 후 "
            "provider/model(예: ollama/glm-5.2:cloud) 로 치환")


def neutralize_model_todo(
    text: str, available: list[str] | None = None
) -> tuple[str, bool]:
    """미해소 `{{OPENCODE_PRO_MODEL}}` 이 있는 YAML `model:` 필드 줄을 주석화·토큰 중화한다.

    변환: `model: "{{OPENCODE_PRO_MODEL}}"` → `# model: "<provider/model>"  # TODO: …`.
    줄을 통째 `# ` 로 주석화(YAML frontmatter 에서 model 키 부재 → opencode 기본 모델)
    하면서 리터럴 토큰을 `<provider/model>` 형식힌트로 제거(자족 산출물·render leak 회피).

    **모델 필드 줄만** 대상이다(`_MODEL_FIELD_LINE_RE`) — 산문/헤더/docstring
    의 토큰(예: README 의 "placeholder `{{OPENCODE_PRO_MODEL}}` 로 출하")은 `# ` prepend 시
    markdown 이 깨지므로 건드리지 않는다. 그런 위치의 토큰은 render 가 계속 leak 으로 표면화한다.
    비파괴·멱등: 이미 `# ` 주석이거나 `TODO` 가 붙은 줄은 재처리 안 함.

    반환: (중화된 text, 중화 발생 여부). import(_mark_model_todos)·self-update(render_adapter)
    양쪽이 공유하는 단일 진실 — 같은 입력에 같은 산출.
    """
    return _comment_out_model_line(
        text, OPENCODE_MODEL_TOKEN, _MODEL_TODO_PLACEHOLDER, _model_todo_tail(available))


def _comment_out_model_line(
    text: str, token: str, placeholder: str, tail: str
) -> tuple[str, bool]:
    """모델 필드 줄의 미해소 `token` 을 주석화·중화하는 공유 줄-변환 (판정 사본 금지).

    모델 토큰이 여럿(opencode 어댑터·역할 카드·티어 프로필)이라도 **줄 판정은 하나**여야 import 와
    self-update 가 같은 산출을 낸다. 대상은 카드 필드 줄(`_MODEL_FIELD_LINE_RE` — 필드 이름은
    CARD_FIELD_TOKEN_PREFIXES 파생)뿐이고, 이미 주석이거나 `TODO` 가 붙은 줄은 비파괴·멱등으로
    건너뛴다.
    """
    if token not in text:
        return text, False
    out: list[str] = []
    marked = False
    for line in text.splitlines(keepends=True):
        if (token in line and "TODO" not in line
                and _MODEL_FIELD_LINE_RE.match(line.lstrip())):
            eol = "\n" if line.endswith("\n") else ""
            body = line.rstrip("\n").replace(token, placeholder)
            out.append("# " + body + tail + eol)
            marked = True
        else:
            out.append(line)
    return "".join(out), marked


def neutralize_delegate_model_todo(
    text: str,
    skip_keys: list[str] | tuple[str, ...] | None = None,
    *,
    unused_profiles: dict[str, str] | None = None,
    card_harness: str | None = None,
) -> tuple[str, bool]:
    """미해소 `{{DELEGATE_MODEL_<ROLE>}}` 이 있는 모델 필드 줄을 주석화·토큰 중화한다.

    변환: `model: {{DELEGATE_MODEL_DEVELOPER}}` → `# model: <model>  # TODO: …`. 줄 전체를
    주석화해 frontmatter 에서 `model` 키를 부재시키면 claude 가 세션 기본 모델로 스폰한다
    (graceful degradation) — 활성 리터럴 토큰을 모델명으로 출하하는 것보다 안전하다.

    미해소 사유는 **local.conf 에 `delegate.<role>.model` 키가 없는 것**(위임 매핑 미설정
    채택자)이다. 이건 오설정이 아니라 정상 형상이라 leak 으로 rc-fail 하지 않는다 — 한 토큰
    미해소가 엔진/타 어댑터 update 전체를 막지 않는다(OPENCODE_PRO_MODEL 선례와 동형).
    ``skip_keys``(빈값이라 호출자가 제외한 token-key)는 중화하지 않고 토큰을 남겨
    `_assert_no_leak` 가 "값을 채우라" 로 표면화하게 한다.

    ``unused_profiles``(token-key → conf 하네스)는 **해소값이 있어도** 중화한다 — 그 카드는 conf
    가 다른 하네스로 보내는 역할이라 이번 형상에서 스폰되지 않는다(미사용 프로필). 꼬리에 사유를
    남겨 채택자가 고칠 곳이 카드가 아니라 local.conf 임을 지목한다. ``card_harness`` 는 그 안내에
    실을 이 카드의 하네스다.

    반환: (중화된 text, 중화 발생 여부). import(render_managed_files)·self-update(pm_update 의
    @render 재렌더) 양쪽이 같은 함수를 타 byte-동일 산출을 낸다(재렌더 왕복 0).
    """
    skipped = set(skip_keys or ())
    unused = dict(unused_profiles or {})
    marked = False
    for token_key, conf_key in DELEGATE_MODEL_CONF_KEYS.items():
        if token_key in skipped and token_key not in unused:
            continue
        tail = (
            _delegate_unused_profile_tail(conf_key, unused[token_key], card_harness)
            if token_key in unused
            else _delegate_model_todo_tail(conf_key)
        )
        text, changed = _comment_out_model_line(
            text,
            "{{" + token_key + "}}",
            _delegate_todo_placeholder(token_key),
            tail,
        )
        marked = marked or changed
    return text, marked


class RenderLeakError(RuntimeError):
    """렌더가 자족 산출물 또는 카드 값 조건을 위반했다.

    자족 위반은 리터럴 `{{...}}`·stray omit-marker 잔존(미해소 leak)이고, 카드 값 조건 위반은
    카드에 채울 추론 값이 그 카드 하네스의 허용집합 밖이거나 검증할 수 없는 경우다."""


# 스킬 호출 표기의 단일 진실. 키는 공개 harness 별칭이 아니라 `templates/` 아래 실 디렉터리명이다.
# pm_update --all-targets 가 같은 디렉터리 축을 발견해 이 registry에 넘기고, 출하 표기 가드도
# 이 값을 직접 읽는다. 새 template 디렉터리에 canonical 호출 토큰이 있는데 값이 미등록이면 아래
# renderer가 fail-loud한다(알 수 없는 하네스를 조용히 `/`로 복사하지 않음).
SKILL_ENTRY_PREFIX_BY_TEMPLATE_DIR: dict[str, str] = {
    "claude_code": "/",
    "codex": "$",
    "opencode": "/",
}


_HARNESS_LABEL_BY_TEMPLATE_DIR: dict[str, str] = {
    "claude_code": "claude",
    "codex": "codex",
    "opencode": "opencode",
}


def render_skill_entry_notation(
    text: str,
    template_dir: str | tuple[str, ...] | list[str],
    *,
    skill_names: tuple[str, ...],
    source: str | None = None,
) -> str:
    """canonical ``/<설치 카드>`` 호출 토큰만 선택 template 하네스의 실제 표기로 치환한다.

    단일 하네스는 표기 하나만 낸다. 같은 물리 경로를 서로 다른 표기의 하네스가 함께 읽으면
    설치된 하네스만 병기한다(같은 표기는 label을 묶음). 예: codex+opencode는
    ``$pm-bootstrap``(codex) / ``/pm-bootstrap``(opencode). 경로/확장자/하이픈 식별자와
    concrete ``$pm-*`` 설명은 비대상이다. 토큰이 있는데 어느 template 값이든 미등록이면 원문을
    복사하지 않고 RenderLeakError로 중단한다.

    설치 카드가 하나도 없으면(빈 root) 아래 정규식들의 대안이 비어 맨 `/`·`$`에 매칭하므로
    산문을 훼손한다 — 그 경우는 렌더 대상 자체가 없으니 입구에서 원문을 그대로 돌려준다.
    """
    if not skill_names:
        return text
    _SKILL_ENTRY_NAME_ALT = "|".join(
        re.escape(name) for name in sorted(skill_names, key=len, reverse=True)
    )

    _CANONICAL_INLINE_SKILL_ENTRY_RE = re.compile(
        rf"(?<![A-Za-z0-9_.>/\-])`/(?P<skill>{_SKILL_ENTRY_NAME_ALT})"
        rf"(?P<args>(?:[ \t][^`\n]*)?)`"
        # 경로 판정은 위의 opening/closing backtick **안**에서 끝낸다. 닫는 backtick 뒤의
        # ``.``/``/`` 등은 코드 스팬 밖 문장부호·산문이므로 확장자/경로 경계가 아니다. 이미
        # 하네스 label이 붙은 렌더 산출만 멱등성을 위해 제외한다.
        rf"(?!\((?:claude|codex|opencode)(?:·(?:claude|codex|opencode))*\))"
    )

    _CANONICAL_SKILL_ENTRY_RE = re.compile(
        rf"(?<![\w.>/=\-`])(?<!\]\()/(?P<skill>{_SKILL_ENTRY_NAME_ALT})"
        # 문장부호 ``.``는 호출 경계지만 ``.md`` 같은 확장자는 경로다.
        rf"(?![\w>/\-]|\.[A-Za-z0-9_])"
    )

    _CONCRETE_CODEX_SKILL_ENTRY_RE = re.compile(
        rf"(?<![A-Za-z0-9_.>/\-])\$(?P<skill>{_SKILL_ENTRY_NAME_ALT})"
        rf"(?![A-Za-z0-9_.>/\-])"
        rf"(?![^`\n]*`\((?:claude|codex|opencode)(?:·(?:claude|codex|opencode))*\))"
    )

    _ANNOTATED_SKILL_ENTRY_GROUP_RE = re.compile(
        rf"`[/\$](?P<skill>{_SKILL_ENTRY_NAME_ALT})"
        rf"(?P<args>(?:[ \t][^`\n]*)?)`"
        rf"\((?:claude|codex|opencode)(?:·(?:claude|codex|opencode))*\)"
        rf"(?: / `[/\$](?P=skill)(?P=args)`"
        rf"\((?:claude|codex|opencode)(?:·(?:claude|codex|opencode))*\))+"
    )
    template_dirs = (template_dir,) if isinstance(template_dir, str) else tuple(template_dir)
    template_dirs = tuple(dict.fromkeys(template_dirs))
    if len(template_dirs) > 1:
        # 이미 두 하네스 표기로 병기된 라이브 문서에 세 번째 하네스를 추가하는 경우도 같은
        # 입력 형태로 합류시킨다. 전체 병기 group만 접어 경로/확장자와 일반 산문은 건드리지 않는다.
        text = _ANNOTATED_SKILL_ENTRY_GROUP_RE.sub(
            lambda match: f"`/{match.group('skill')}{match.group('args')}`",
            text,
        )
    has_canonical = bool(
        _CANONICAL_INLINE_SKILL_ENTRY_RE.search(text)
        or _CANONICAL_SKILL_ENTRY_RE.search(text)
    )
    has_multi_codex_source = (
        len(template_dirs) > 1
        and _CONCRETE_CODEX_SKILL_ENTRY_RE.search(text) is not None
    )
    if not has_canonical and not has_multi_codex_source:
        return text
    unknown = [
        dirname for dirname in template_dirs
        if dirname not in SKILL_ENTRY_PREFIX_BY_TEMPLATE_DIR
        or dirname not in _HARNESS_LABEL_BY_TEMPLATE_DIR
    ]
    if not template_dirs or unknown:
        where = f" ({source})" if source else ""
        raise RenderLeakError(
            f"스킬 호출 표기 렌더{where} 실패 — templates/"
            f"{', '.join(unknown) if unknown else '(empty)'} 표기 값이 미등록이다. "
            "새 하네스의 실제 호출 표기를 registry에 명시하라."
        )

    by_prefix: dict[str, list[str]] = {}
    for dirname in template_dirs:
        prefix = SKILL_ENTRY_PREFIX_BY_TEMPLATE_DIR[dirname]
        by_prefix.setdefault(prefix, []).append(_HARNESS_LABEL_BY_TEMPLATE_DIR[dirname])

    # 공존 root doc의 선언된 중립 source가 codex 단일 문서면 `$`가 canonical 입력이다. 서로 다른
    # prefix가 실제 공존할 때만 이를 slash token-form으로 되돌려 아래 단일 병기 경로에 합류시킨다.
    if len(by_prefix) > 1:
        text = _CONCRETE_CODEX_SKILL_ENTRY_RE.sub(
            lambda match: "/" + match.group("skill"),
            text,
        )

    def inline_replacement(match: re.Match[str]) -> str:
        skill = match.group("skill")
        args = match.group("args")
        if len(by_prefix) == 1:
            prefix = next(iter(by_prefix))
            return f"`{prefix}{skill}{args}`"
        return " / ".join(
            f"`{prefix}{skill}{args}`({'·'.join(labels)})"
            for prefix, labels in by_prefix.items()
        )

    def plain_replacement(match: re.Match[str]) -> str:
        skill = match.group("skill")
        if len(by_prefix) == 1:
            return next(iter(by_prefix)) + skill
        return " / ".join(
            f"`{prefix}{skill}`({'·'.join(labels)})"
            for prefix, labels in by_prefix.items()
        )

    result = _CANONICAL_INLINE_SKILL_ENTRY_RE.sub(inline_replacement, text)
    return _CANONICAL_SKILL_ENTRY_RE.sub(plain_replacement, result)


def _fill_operational(text: str, operational: dict) -> tuple[str, list[str]]:
    """operational 토큰(`{{PROJECT_NAME}}` 등)을 plain string replace — omit 없음·행-문맥 불요.

    text 는 단일 행이든 산출물 전체든 무관(plain replace 라 멱등). 출하/채움 파일엔 이미
    리터럴(import sed)이라 보통 no-op. operational 값이 주어지면 재렌더가 그 토큰을 리터럴로
    채운다(local.conf 재유도). operational 가 안 채운 토큰은 그대로 남고, 그러면 _assert_no_leak
    가 leak 으로 잡는다(자족 산출물 = 토큰 0·미해소 침묵 출하 금지).

    미보유 key(dict 부재)는 물론, **값이 빈 문자열인 key 도 치환하지 않는다**(호출자 무관
    이중화·pm_import 경로 포함) — 토큰을 그대로 남겨 _assert_no_leak 가 leak 으로 잡게 한다.
    `.get(key, "")` 나 빈값 치환은 미해소를 *침묵 비움*(예: `project.name=` 빈값 → description 이
    " 프로젝트")으로 출하해 탐지 신호 자체를 없앤다 — 잔여 토큰보다 더 나쁘다.

    반환: (치환된 text, 빈값이라 건너뛴 token-key 목록). 후자는 render_adapter 가 _assert_no_leak
    힌트("local.conf `<key>=` 가 빈값 — 값을 채우라")에 싣는다.
    """
    if not operational:
        return text, []
    empty_keys: list[str] = []
    for key in OPERATIONAL_KEYS:
        if key not in operational:
            # 미보유 key 는 치환하지 않는다 — 토큰을 그대로 남겨 _assert_no_leak 가 잡게 한다.
            continue
        value = operational[key]
        if value is None or str(value) == "":
            # 빈값 key 도 치환하지 않는다 — silent-empty = leak 클래스. 토큰을 남겨
            # _assert_no_leak 가 잡되, 빈값 원인임을 힌트로 표면화한다(값을 채우라).
            empty_keys.append(key)
            continue
        token = "{{" + key + "}}"
        if token in text:
            text = text.replace(token, str(value))
    return text, empty_keys


# 프로필 conf 키(`delegate.<role>[.<tier>]`) + `.model` → 그 모델 토큰. 위 표에서 파생한다(사본 0).
_MODEL_TOKEN_BY_CONF_KEY: dict[str, str] = {
    conf_key: token_key for token_key, conf_key in DELEGATE_MODEL_CONF_KEYS.items()
}


def _assert_no_leak(
    text: str,
    *,
    source: str | None = None,
    empty_keys: list[str] | None = None,
) -> None:
    """렌더 산출물에 잔여 `{{[A-Z_]+}}` 토큰 또는 stray omit-marker 가 있으면 RenderLeakError.

    자족 산출물 = 토큰 0 (allow-list 없음): 잔여 토큰이 *하나라도* 있으면 emission
    순간 hard-fail. operational 은 plain replace 로 전부 해소돼야 한다 — 미해소(템플릿 저자가
    넣은 새 `{{FOO}}`·local.conf 미보유 operational·미배선 토큰·옛 free-form 토큰 잔존)를
    침묵 출하 대신 큰소리로 표면화한다(half-rendered 토큰이 출하되는 것을 막음).

    추가로 stray omit-marker(`<!-- pm:omit-if-empty ... -->`·open 또는 close)가 잔존하면
    같은 에러로 잡는다
    어댑터엔 절대 없어야 하며, 잔존 시 미마이그레이션 신호로 무음 출하를 막는다.
    """
    leaked = sorted(set(_ANY_TOKEN_RE.findall(text)))
    stray = _STRAY_MARKER_RE.findall(text)
    if not leaked and not stray:
        return
    where = f" ({source})" if source else ""
    parts: list[str] = []
    if leaked:
        toks = ", ".join("{{" + k + "}}" for k in leaked)
        parts.append(
            f"미해소 토큰 잔존: {toks} — 자족 산출물은 토큰 0 이어야 한다. "
            f"템플릿 저자가 새 토큰을 넣었거나 local.conf 채널 배선이 누락됐다.")
        # 빈값(local.conf `<key>=`)이 원인인 leak 은 값을 채우라는 실행가능 힌트를 더한다.
        # empty_keys·leaked 는 둘 다 uppercase token-key → `_local_conf_key` 로 local.conf key 제시.
        empty_leaked = [k for k in (empty_keys or []) if k in leaked]
        if empty_leaked:
            conf_keys = ", ".join("`" + _local_conf_key(k) + "=`" for k in empty_leaked)
            parts.append(
                f"위 토큰은 local.conf {conf_keys} 가 빈값이라 미해소 — 값을 채우라.")
    if stray:
        parts.append(
            f"stray omit-marker 잔존: {', '.join(dict.fromkeys(stray))} — 옛 free-form "
            f"drop-section 마커(제거)는 어댑터에 절대 없어야 한다.")
    raise RenderLeakError(f"렌더 산출물{where} 위반 — " + " / ".join(parts))
