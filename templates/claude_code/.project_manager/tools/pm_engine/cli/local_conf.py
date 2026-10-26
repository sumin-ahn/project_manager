"""local.conf 키 표 CLI — `--keys`·`--keys --json`·`--render-adapter-block` (층: cli).

진입 파일 `.project_manager/tools/local_conf.py` 가 `main` 에 위임한다. 레지스트리·판정은
`pm_engine.domain.conf_schema` 가 소유하고, 이 모듈은 그것을 사람 표·기계 JSON·어댑터 생성 블록으로
낸다.
"""
from __future__ import annotations

import json
import sys
import unicodedata

from pm_engine.domain import conf_schema
from pm_engine.infra import console


# ── 어댑터 파서용 데이터 내보내기 (엔진 미import 파서 + 셸 훅) ────────
# Python 어댑터는 엔진 공개 진입(`cli.hooks`)으로 local.conf 를 읽으므로 자체 파서가 없다. 엔진을
# import 하지 않는 독립 파서(opencode JS core · run-tests 셸 훅)는 이 표를 **손으로 복제하면** 표와
# 파서가 갈린다. 그래서 이 모듈이 블록 텍스트를 **생성**하고, 각 파서는 그 산출을 그대로 품는다
# (동일성은 회귀가 값으로 단언).
# 재생성: `python3 local_conf.py --render-adapter-block <python|js|sh>`.
ADAPTER_BLOCK_BEGIN = "생성 시작 — 차단 구키 (local_conf.render_adapter_block · 손편집 금지)"
ADAPTER_BLOCK_END = "생성 끝 — 차단 구키"


def blocking_legacy_key_names() -> tuple[str, ...]:
    """차단 구키 이름 전수 — 생성 블록의 단일 입력(안내 전용 키는 뺀다)."""
    return tuple(sorted(key for key in conf_schema.LEGACY_KEY_MAP
                        if key not in conf_schema.NON_BLOCKING_LEGACY_KEYS))


def render_adapter_block(style: str) -> str:
    """어댑터 파서가 품는 차단 구키 선언 블록을 그 언어 문법으로 낸다(마커 포함)."""
    names = blocking_legacy_key_names()
    if style == "python":
        body = "\n".join(f'    "{name}",' for name in names)
        return (f"# {ADAPTER_BLOCK_BEGIN}\n"
                f"LEGACY_CONF_KEYS = (\n{body}\n)\n"
                f'LEGACY_CONF_KEY_PREFIX = "{conf_schema.LEGACY_SUFFIX_PREFIX}"\n'
                f"# {ADAPTER_BLOCK_END}")
    if style == "js":
        body = "\n".join(f'  "{name}",' for name in names)
        return (f"// {ADAPTER_BLOCK_BEGIN}\n"
                f"const LEGACY_CONF_KEYS = [\n{body}\n];\n"
                f'const LEGACY_CONF_KEY_PREFIX = "{conf_schema.LEGACY_SUFFIX_PREFIX}";\n'
                f"// {ADAPTER_BLOCK_END}")
    raise ValueError(f"모르는 생성 스타일: {style}")


def adapter_stop_message(path, found) -> str:
    """어댑터 파서가 멈출 때 내는 문구 — 엔진 안내와 같은 사실을 짧게 말한다.

    어댑터는 매핑표를 들지 않으므로 신표기 이름을 말하지 못한다. 대신 **무엇이 걸렸는지**와
    전수 지목을 어디서 받는지를 말한다(엔진 도구가 그 표의 소유자다)."""
    return (f"오류: local.conf 에 구표기 키가 남아 있습니다 ({path}) — "
            f"{', '.join(found)}. 값이 조용히 기본값으로 떨어지지 않도록 여기서 멈춥니다. "
            "전수 지목은 `board.py lint` 또는 `pm_update.py` 안내가 냅니다.")

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


# ── 키 표 출력 (`--keys`) ────────────────────────────────────────────────
_KEY_TABLE_HEADERS = ("키", "필수", "기본값", "설명")


def _key_table_rows(registry: dict[str, conf_schema.KeySpec]) -> list[tuple[str, str, str, str]]:
    """표 한 벌의 행 — 필수는 `필수`/`-`, 선언 없는 기본값은 `-` 로 적는다."""
    return [(key, "필수" if spec.required else "-",
             _default_display(spec), spec.description)
            for key, spec in registry.items()]


def _default_display(spec: conf_schema.KeySpec) -> str:
    """표에 적는 기본값 — 선언이 없으면 `-` 다."""
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
            {key: spec for key, spec in conf_schema.KEY_REGISTRY.items() if spec.group == group})
        if not rows:
            continue
        lines.append("")
        lines.append(f"[{group}]")
        lines.extend(_render_key_rows(rows))
    lines.append("")
    lines.append("패턴 키군 (`<...>` 자리 목록은 코드·선언이 소유한다)")
    lines.extend(_render_key_rows(_key_table_rows(conf_schema.PATTERN_REGISTRY)))
    return "\n".join(lines)


# ── 키 표의 기계 표면 (`--keys --json`) ──────────────────────────────────
# 사람 표(`render_key_table`)와 **같은 레지스트리·같은 순서**를 낸다. 다른 점은 둘이다:
# 패턴 키군의 전개된 실명을 `members` 로 함께 내며, 행마다 `choices`(고를 수 있는 값)·`recommended`(권장값)를
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
    """값의 축 이름 → `{버킷: 고를 수 있는 값}`. 목록은 설정 어휘에서 읽는다(여기 복제 0).

    `harness` 는 버킷이 없어 `""` 한 칸이고, `reasoning` 은 CLI 마다 ladder 가 달라 하네스가
    버킷이다 — 버킷은 정적 허용값 하네스뿐이고 모델마다 값이 다른 하네스는 조회 응답에서 받는다.
    `model` 은 닫힌 목록이 없다(자유 문자열 + 조회 명령) — 빈 표다.
    """
    if axis == "harness":
        return {"": list(conf_schema.HARNESS_CHOICES)}
    if axis == "reasoning":
        return {harness: list(values)
                for harness, values in conf_schema.REASONING_ALLOWED.items()}
    if axis == "progress_signal":
        return {"": [kind for kind in _progress_signal_kinds() if kind]}
    return {}


def _progress_signal_kinds() -> tuple[str, ...]:
    """진행 출력 형식 세 값 — 화면에 세울 순서(강함 → 없음)는 여기서 정한다."""
    return (conf_schema.PROGRESS_SIGNAL_EVENT_STREAM, conf_schema.PROGRESS_SIGNAL_PLAINTEXT,
            conf_schema.PROGRESS_SIGNAL_NONE)


def _choices_json(key: str) -> dict | None:
    """이 키가 고를 수 있는 값 — 화면이 읽는 규칙 한 벌 (축이 없는 키는 None).

    화면이 쓰는 규칙은 셋이다. 첫째 `bucket = depends_on ? (그 키의 현재 값 ?? "") : ""`,
    둘째 목록 원천 — 버킷이 `values` 에 있으면 그 목록, 없고 `fetch` 가 있으면 그 명령으로 받은
    응답에서 `fetch.variants_of` 가 있으면 `variants[그 키의 현재 값]`, 없으면 `models` 다. 셋째
    `free_text` 면 직접 입력도 연다. 판별자(`kind`)를 두지 않아 화면에 분기가 생기지 않는다. 종속
    키는 같은 세트의 `harness` 키이고 `variants_of` 는 같은 세트의 `model` 키다 — 세트를 통째로
    쓰는 위임 키와 추가 리뷰어 키가 같은 규칙으로 읽힌다.
    """
    spec = conf_schema.key_spec(key)
    axis = None if spec is None else spec.choices
    if axis is None:
        return None
    harness = _harness_of(key)
    fetch = dict(_MODEL_FETCH) if axis in ("model", "reasoning") else None
    if axis == "reasoning":
        # 모델마다 추론 강도가 다른 하네스는 같은 세트 모델의 variant 를 조회 응답에서 받는다.
        fetch["variants_of"] = f"{key.rsplit('.', 1)[0]}.model"
    if axis == "model" and harness is not None:
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
    return key.split(".")[1] if conf_schema.pattern_for(key) == conf_schema.HARNESS_PATTERN else None


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
    "ctx_window_tokens": "사용 한도 (토큰)",
    "pro_model": "안내 문서에 적을 모델",
}


def _pattern_unit_labels(pattern: str) -> dict[str, str]:
    """키군 안에서 행마다 갈리는 이름 `<키> → 사람이 읽는 이름`. 안 갈리는 군은 빈 표다.

    군 선언의 이름 하나가 멤버 전부에 그대로 붙으면 같은 이름이 여러 줄에 서서 어느 줄이
    무엇인지 갈리지 않는다. 축을 이 모듈이 소유한 군만 여기서 갈라 준다 — 채택자가 이름을
    정하는 군(`test.<platform>`·`delegate.model_alias.<name>`)은 그 이름 자체가 이미 갈라
    주므로 지어내지 않는다. 한 줄로 묶이는 역할 군의 줄 이름은 여기가 아니라 `_units_json` 이다.

    `harness.<name>` 은 이름이 채택자 것이어도 **그 아래 칸은 엔진이 정한 닫힌 집합**이라
    여기서 갈라 준다 — 안 갈라 주면 하네스마다 같은 군 이름이 여러 줄씩 선다.
    """
    if pattern == conf_schema.HARNESS_PATTERN:
        return {f"harness.{name}.{suffix}": f"{name} {_HARNESS_SUFFIX_LABELS[suffix]}"
                for name in conf_schema.HARNESS_CHOICES for suffix in conf_schema._harness_suffixes(name)}
    return {}


def _pattern_members(pattern: str) -> list[dict]:
    """패턴 키군의 전개된 실명과 각 기본값 — 축은 이 모듈이 이미 소유한 선언에서 온다.

    코드가 축을 갖지 않는 군은 빈 목록이다(지어내지 않는다): `test.<platform>.cmd` 의 축은 그
    프로젝트의 `qa.platforms` 값이고, `delegate.model_alias.<name>` 의 축은 채택자가 정하는
    이름이다. 그 두 군의 실키는 화면이 **conf 에 이미 있는 값**에서 읽는다.
    """
    if pattern == conf_schema.HARNESS_PATTERN:
        keys = [f"harness.{name}.{suffix}"
                for name in conf_schema.HARNESS_CHOICES for suffix in conf_schema._harness_suffixes(name)]
    elif pattern == conf_schema.DELEGATE_ROLE_PATTERN:
        keys = [f"delegate.{role}{tier}.{suffix}"
                for role in conf_schema.ROLE_CHOICES for tier in conf_schema._role_tiers(role)
                for suffix in conf_schema._ROLE_SUFFIXES]
    else:
        keys = []
    return [{"key": key, "default": conf_schema.default_for(key), "unset": conf_schema.unset_for(key),
             "choices": _choices_json(key), "recommended": conf_schema.recommended_for(key),
             **_row_axes(key)}
            for key in keys]


def _row_axes(key: str) -> dict:
    """행마다 싣는 schema 6 축 셋 — 묶음 이름·스위치·엔진이 적는 키인지."""
    spec = conf_schema.key_spec(key)
    return {"unit": unit_of(key), "governed_by": _governed_json(spec),
            "readonly": spec is not None and spec.readonly}


def unit_of(key: str) -> str | None:
    """도구·모델·생각 깊이를 **한 세트로 읽는** 묶음이면 그 이름(키의 마지막 점 앞), 아니면 None.

    세트는 통째로 읽힌다 — 역할 묶음은 `pm_delegate.resolve_delegate`, 교차 검토 묶음은
    `additional_reviewer.resolve_reviewer_target` 이 셋 중 하나만 있어도 나머지를 요구한다. 그래서
    화면은 이 묶음에 체크박스를 하나만 둔다(칸마다 두면 한 칸만 켠 반쪽 세트가 저장된다).
    """
    prefix, _, suffix = key.rpartition(".")
    if suffix not in conf_schema._ROLE_SUFFIXES:
        return None
    if (prefix == conf_schema._REVIEWER_UNIT
            or conf_schema.pattern_for(key) == conf_schema.DELEGATE_ROLE_PATTERN):
        return prefix
    return None


def _units_json() -> dict[str, str]:
    """묶음 이름 → 줄 이름 (화면이 `delegate.developer.hard` 같은 키를 이름으로 세우지 않게)."""
    units = {f"delegate.{role}{tier}": f"{_ROLE_LABELS[role]}({_TIER_LABELS[tier]})"
             for role in conf_schema.ROLE_CHOICES for tier in conf_schema._role_tiers(role)}
    units[conf_schema._REVIEWER_UNIT] = GROUP_LABELS[conf_schema._REVIEWER_UNIT]
    return units


def _governed_json(spec: conf_schema.KeySpec | None) -> dict | None:
    """그 절을 켜는 스위치 — `on` 은 켜진 것으로 치는 값 목록이고 None 이면 "값이 있으면 켜짐".

    빈 값은 그 스위치의 기본값으로 읽는다(위임 사용은 기본 ON). 목록은 설정 어휘에서 읽는다.
    """
    if spec is None or spec.governed_by is None:
        return None
    master = spec.governed_by
    on = list(conf_schema.DELEGATE_ENABLED_VALUES) if master == "delegate.enabled" else None
    return {"key": master, "on": on, "reason": conf_schema._MASTER_SWITCHES[master]}


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
         "choices": _choices_json(key), "recommended": conf_schema.recommended_for(key), **_row_axes(key)}
        for key, spec in conf_schema.KEY_REGISTRY.items()
    ]
    keys.extend(
        {"key": pattern, "group": spec.group, "label": spec.label,
         "description": spec.description,
         "default": spec.default, "unset": spec.unset, "required": spec.required,
         "kind": "pattern", "members": _pattern_members(pattern),
         "unit_labels": _pattern_unit_labels(pattern),
         "choices": None, "recommended": None,
         "unit": None, "governed_by": _governed_json(spec), "readonly": spec.readonly}
        for pattern, spec in conf_schema.PATTERN_REGISTRY.items()
    )
    return {"schema": KEY_JSON_SCHEMA, "groups": list(conf_schema.KEY_GROUPS),
            "group_labels": {group: GROUP_LABELS[group] for group in conf_schema.KEY_GROUPS},
            "units": _units_json(), "unit_fields": dict(_ROLE_FIELD_LABELS),
            "keys": keys}


def _declared_groups() -> list[str]:
    """실제 선언에 쓰인 묶음 이름 — 표시 순서는 `KEY_GROUPS` 하나가 정한다.

    `KEY_GROUPS` 밖의 묶음을 뒤에 덧붙이는 자리는 두지 않는다 — 완전성 테스트가 모든
    `spec.group` 이 `KEY_GROUPS` 안에 있음을 강제하므로 그 확장은 발화할 수 없다(새 묶음을
    쓰려면 `KEY_GROUPS` 에 순서를 선언하는 것이 유일한 경로다).
    """
    return [group for group in conf_schema.KEY_GROUPS
            if any(spec.group == group for spec in conf_schema.KEY_REGISTRY.values())]


def main(argv: list[str] | None = None) -> int:
    """키 표(`--keys`)와 어댑터 생성 블록 출력 — 사람이 손으로 옮겨 적는 표면을 없앤다."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["--keys"] and args[1:] in ([], ["--json"]):
        if args[1:]:
            # 기계 판독 페이로드는 콘솔 텍스트 레이어를 건너뛴다 — 그 레이어는 PowerShell 캡처에서
            # 콘솔 codepage 로 강등되고 그 치환은 되돌릴 수 없다(사람 표는 종전 `print` 그대로).
            console.write_machine_line(
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

