#!/usr/bin/env python3
"""어댑터 render 엔진 — 어댑터 파일을 *생성 산출물* 로 렌더한다.

framework 본문 템플릿 → 자족 .md. operational 토큰(`{{KEY}}` placeholder)을 local.conf
재유도값으로 plain string replace 한다(Jinja/표현식/루프 없음·DSL 아님). (엔진 fix →
어댑터 전파)의 근본 fix: framework 본문 변경을 전파하면서 채택자 customization(local.conf)을
영영 clobber 하지 않는다 — 손편집되던 출하 .md 가 템플릿의 재렌더 산출물이 된다.

토큰 (어댑터 .md 의 `{{KEY}}` placeholder):
  - **operational** (OPERATIONAL_KEYS) — import 시 sed 치환된 리터럴(local.conf 재유도).
    plain string replace(omit 없음). 출하 파일엔 이미 리터럴이라 렌더는 보통 no-op.

free-form(채택자 손편집 산문 — `{{PROJECT_CONSTRAINTS}}` 등)은 *이 엔진이 다루지 않는다*:
free-form 을 canonical home(root doc §프로젝트 고유 제약 + `pm_role.local.md`
§보호 영역)으로 일원화하고, pm_import 의 FILL 채널(`FREE_FORM_TOKENS`)이 전담한다. render-overlay
free-form value-fill 기계(overlay.local.yaml·`FREEFORM_KEYS`·slot-fill·conditional-omit)는
free-form 은 FILL 채널 단일 채널이고 어댑터는 free-form-free 다
(`tests/test_adapter_free_form_free.py` lock-in).

자족 산출물 = 토큰 0: 렌더 결과에 잔여 `{{[A-Z_]+}}` 가 *하나라도* 있으면
post-render assertion 이 emission 순간 hard-fail(allow-list 없음). operational 은 plain replace
로 해소돼야 한다 — 미해소 operational(local.conf 미보유 key 등)·템플릿 저자가 새로 넣은
`{{FOO}}`·미배선 토큰을 침묵 출하 대신 큰소리로 표면화한다. board.py `render-leak` lint 가
상시 backstop(2중 차단).

**단 하나의 예외 = intentional-TODO placeholder**: `{{OPENCODE_PRO_MODEL}}` 은 채택자가
opencode 없이 import 하면 결정적 해소가 불가능한 토큰이라, 미해소 시 leak(자족 위반)이 아니라
`# model: "<provider/model>"  # TODO: …` 로 graceful 중화한다(pm_import --fill manual 과 대칭·
채택자-fill 대기). 이건 자족 산출물 위반이 아니다 — 리터럴 `{{...}}` 토큰이 제거되고(자족 유지)
채택자가 나중에 채우는 지점만 남는다. neutralize_model_todo 참조. 그 밖의 미해소 토큰은
여전히 fail-loud(false-green 근절 유지).

순수 함수 중심(stdlib). pm_update(재렌더)·pm_import(최초) 양쪽이 호출한다.
"""

from __future__ import annotations

import re
from pathlib import Path

# baked 엔진 rev — engine_rev.py --bump가 기계 일괄 재작성한다.
ENGINE_REV = "v1.7.5"

# operational — import sed 치환된 리터럴 (local.conf 재유도). plain replace·omit 없음.
# pm_import.OPERATIONAL_TOKENS(중괄호 포함)와 동일 집합을 bare key 로 + opencode 전용
# OPENCODE_PRO_MODEL(opencode 채택자 local.conf 만 보유·claude tree 엔 토큰 부재 → no-op).
#
# ⚠️ forward-flag (@render 활성화 시점): local.conf(board.py init 산출)는 이 중
#    일부만 보유한다 — DATE·PROJECT_ROOT·PROJECT_TAGLINE 는 init 이 채우지 않을 수 있어
#    pm_update._operational_from_local_conf 가 그 token-key 를 dict 에 안 넣는다. 엄격 가드
#    (_assert_no_leak·토큰 0) 하에선, @render 활성화된 어댑터 파일이 그런 미보유 operational
#    토큰을 *담고 있으면 렌더가 실패한다 — 그게 옳다*(미해소를 침묵 출하 대신 표면화). 현재
#    @render path 0 이라 안 깨진다. 활성화 시점()에 그 파일들의 operational 해소(또는
#    local.conf 채널 확장)를 보장하는 건 몫. 이 엔진은 leak 을 표면화할 뿐 채우지 않는다.
OPERATIONAL_KEYS: tuple[str, ...] = (
    "PROJECT_NAME",
    "PROJECT_TAGLINE",
    "PROJECT_ROOT",
    "PY",
    "TEST_CMD",
    "DATE",
    # opencode 어댑터 전용 — pm_import 가 local.conf 에 opencode_pro_model 을 기록.
    # local.conf 에 해소돼 있으면 여기 operational 채널로 plain replace(정상 치환). *미해소*(채택자가
    # opencode 없이 import·TODO 폴백)면 leak 이 아니라 intentional-TODO 로 graceful 중화한다
    # (neutralize_model_todo·import 대칭) — 아래 render_adapter 참조.
    "OPENCODE_PRO_MODEL",
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

    **model: 필드 줄만** 대상이다(`line.lstrip().startswith("model:")`) — 산문/헤더/docstring
    의 토큰(예: README 의 "placeholder `{{OPENCODE_PRO_MODEL}}` 로 출하")은 `# ` prepend 시
    markdown 이 깨지므로 건드리지 않는다. 그런 위치의 토큰은 render 가 계속 leak 으로 표면화한다.
    비파괴·멱등: 이미 `# ` 주석이거나 `TODO` 가 붙은 줄은 재처리 안 함.

    반환: (중화된 text, 중화 발생 여부). import(_mark_model_todos)·self-update(render_adapter)
    양쪽이 공유하는 단일 진실 — 같은 입력에 같은 산출.
    """
    if OPENCODE_MODEL_TOKEN not in text:
        return text, False
    tail = _model_todo_tail(available)
    out: list[str] = []
    marked = False
    for line in text.splitlines(keepends=True):
        if (OPENCODE_MODEL_TOKEN in line and "TODO" not in line
                and line.lstrip().startswith("model:")):
            eol = "\n" if line.endswith("\n") else ""
            body = line.rstrip("\n").replace(OPENCODE_MODEL_TOKEN, _MODEL_TODO_PLACEHOLDER)
            out.append("# " + body + tail + eol)
            marked = True
        else:
            out.append(line)
    return "".join(out), marked


class RenderLeakError(RuntimeError):
    """렌더 산출물에 리터럴 `{{...}}` 또는 stray omit-marker 가 잔존 — 미해소 leak(자족 산출물 위반)."""


# 스킬 호출 표기의 단일 진실. 키는 공개 harness 별칭이 아니라 `templates/` 아래 실 디렉터리명이다.
# pm_update --all-targets 가 같은 디렉터리 축을 발견해 이 registry에 넘기고, 출하 표기 가드도
# 이 값을 직접 읽는다. 새 template 디렉터리에 canonical 호출 토큰이 있는데 값이 미등록이면 아래
# renderer가 fail-loud한다(알 수 없는 하네스를 조용히 `/`로 복사하지 않음).
SKILL_ENTRY_PREFIX_BY_TEMPLATE_DIR: dict[str, str] = {
    "claude_code": "/",
    "codex": "$",
    "opencode": "/",
}


def _installed_skill_entry_names() -> tuple[str, ...]:
    """현재 설치 root의 모든 실제 스킬 카드 이름을 파생한다.

    canonical/Claude/OpenCode는 ``.claude/skills``, Codex는 ``.agents/skills``를 쓴다.
    두 root를 모두 열어 특정 접두사나 손 목록 없이 ``*/SKILL.md`` 실재 카드만 소비한다.
    """
    project_root = Path(__file__).resolve().parents[2]
    return tuple(sorted({
        card.parent.name
        for skills_root in (
            project_root / ".claude" / "skills",
            project_root / ".agents" / "skills",
        )
        for card in skills_root.glob("*/SKILL.md")
    }))


# 일반 하이픈 식별자를 전부 호출로 간주하지 않고 실제 설치 카드 집합만 렌더한다. 새 비-pm
# 카드도 파일을 추가하는 순간 자동 편입된다. 빈 설치 root면 이 집합이 비고, 그러면 아래
# `_SKILL_ENTRY_NAME_ALT`가 빈 문자열이라 정규식이 **맨 `/`·`$`에 매칭해 산문을 훼손한다**
# (`'a / b 로 구분한다'` → `'a $ b 로 구분한다'` 실측). 그래서 render_skill_entry_notation이
# 입구에서 빈 집합을 무동작 반환으로 막는다(아래 가드). 출하 형상의 비어 있음 자체는 출하
# inventory 테스트가 fail-loud한다.
SKILL_ENTRY_NAMES: tuple[str, ...] = _installed_skill_entry_names()

_HARNESS_LABEL_BY_TEMPLATE_DIR: dict[str, str] = {
    "claude_code": "claude",
    "codex": "codex",
    "opencode": "opencode",
}

# canonical 스킬 카드에서 렌더 대상으로 삼는 것은 설치 카드 이름과 일치하는 호출 토큰뿐이다. 양쪽 경계가
# 경로/확장자/식별자 문자가 아니어야 하므로 `.claude/skills/pm-*`·`/pm-bootstrap.md`·
# `/pm-bootstrap/sub`·`/pm-bootstrap-extra`를 그대로 둔다. inline-code 호출은 인자까지 함께
# 소비해 다중 하네스 산출에서 각 전체 호출을 독립 inline-code로 쓴다. 이미 병기된 slash 대안 뒤의
# harness label은 멱등 재렌더 대상이 아니다. `$<설치 카드>`는 이미 concrete라 source token으로 보지 않는다.
_SKILL_ENTRY_NAME_ALT = "|".join(
    re.escape(name) for name in sorted(SKILL_ENTRY_NAMES, key=len, reverse=True)
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


def render_skill_entry_notation(
    text: str,
    template_dir: str | tuple[str, ...] | list[str],
    *,
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
    if not SKILL_ENTRY_NAMES:
        return text
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
    `.get(key, "")` 나 빈값 치환은 미해소를 *침묵 비움*(예: `project_name=` 빈값 → description 이
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


def render_adapter(
    template_text: str,
    operational: dict | None = None,
    *,
    source: str | None = None,
    empty_keys: list[str] | None = None,
    template_dir: str | tuple[str, ...] | list[str] | None = None,
) -> str:
    """어댑터 템플릿 → 자족 .md (operational plain replace).

    source: leak 에러에 실을 파일 경로(선택·render_file 이 전달). 진단용일 뿐 렌더엔 무영향.
    empty_keys: 호출자(pm_update)가 local.conf 빈값이라 dict 에서 제외한 token-key 목록(선택).
    렌더러가 직접 감지한 빈값 key 와 합쳐 leak 힌트("값을 채우라")에 싣는다.

    template_dir가 주어지면 operational보다 먼저 canonical 스킬 호출 토큰을 그 하네스 값으로
    치환한다. 값은 SKILL_ENTRY_PREFIX_BY_TEMPLATE_DIR 단일 registry에서만 읽는다.

    operational 은 행-문맥 무관 plain string replace(omit 없음) → 템플릿 전체에 whole-text
    패스로 적용한다(멱등). 결과는 자족(잔여 `{{...}}`·stray 마커 0) — post-render assertion 이
    잔존 시 RenderLeakError(자족 위반). free-form 토큰·omit-marker 는 어댑터에 없어야 하며
    잔존하면 leak 으로 표면화된다(미마이그레이션 신호).
    """
    operational = operational or {}
    result = (
        render_skill_entry_notation(
            template_text, template_dir, source=source
        )
        if template_dir is not None
        else template_text
    )
    result, detected_empty = _fill_operational(result, operational)
    # pm_update 가 excluded 한 빈값 key(empty_keys) + 렌더러가 직접 감지한 빈값 key 를 합쳐
    # leak 힌트에 싣는다(중복 제거·순서 보존).
    all_empty = list(dict.fromkeys([*(empty_keys or []), *detected_empty]))
    # intentional-TODO graceful (import 대칭): opencode_pro_model 이 local.conf 에
    # *부재*(채택자가 opencode 없이 import — 키 자체가 없음)면 `{{OPENCODE_PRO_MODEL}}` 을 leak
    # 시키는 대신 model: 줄을 주석화·중화한다(import --fill manual 과 byte-동일·재렌더 왕복 0·
    # 불변식 a: 이미 해소돼 있으면 위 _fill_operational 가 치환해 no-op).
    #   ⚠ **`opencode_pro_model=` 빈값(present-but-empty)은 중화하지 않는다** — 빈값은 미설정이
    #   아니라 *오설정* 신호다(pm_import 는 해소 시에만 이 키를 쓰므로 빈값=손-편집/손상).
    #   대로 leak 시켜 "값을 채우라" 로 표면화한다. 빈값은
    #   all_empty 에 실리므로 그때는 중화를 건너뛰어 토큰을 남긴다 → _assert_no_leak 가 잡는다.
    # 그 밖의 미해소 토큰(다른 위치·다른 `{{...}}`)도 계속 fail-loud(불변식 c).
    if _OPENCODE_MODEL_KEY not in all_empty:
        result, _ = neutralize_model_todo(result)
    _assert_no_leak(result, source=source, empty_keys=all_empty)
    return result


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
        # empty_keys·leaked 는 둘 다 uppercase token-key → `.lower()` 로 local.conf key 를 제시.
        empty_leaked = [k for k in (empty_keys or []) if k in leaked]
        if empty_leaked:
            conf_keys = ", ".join("`" + k.lower() + "=`" for k in empty_leaked)
            parts.append(
                f"위 토큰은 local.conf {conf_keys} 가 빈값이라 미해소 — 값을 채우라.")
    if stray:
        parts.append(
            f"stray omit-marker 잔존: {', '.join(dict.fromkeys(stray))} — 옛 free-form "
            f"drop-section 마커(제거)는 어댑터에 절대 없어야 한다.")
    raise RenderLeakError(f"렌더 산출물{where} 위반 — " + " / ".join(parts))


def render_file(
    template_path: Path,
    operational: dict | None = None,
) -> str:
    """템플릿 파일 → 렌더 텍스트 (편의 래퍼·source 를 leak 에러에 명시)."""
    text = Path(template_path).read_text(encoding="utf-8")
    return render_adapter(text, operational, source=str(template_path))
