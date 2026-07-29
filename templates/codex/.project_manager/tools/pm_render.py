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
ENGINE_REV = "v1.5.0"

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
) -> str:
    """어댑터 템플릿 → 자족 .md (operational plain replace).

    source: leak 에러에 실을 파일 경로(선택·render_file 이 전달). 진단용일 뿐 렌더엔 무영향.
    empty_keys: 호출자(pm_update)가 local.conf 빈값이라 dict 에서 제외한 token-key 목록(선택).
    렌더러가 직접 감지한 빈값 key 와 합쳐 leak 힌트("값을 채우라")에 싣는다.

    operational 은 행-문맥 무관 plain string replace(omit 없음) → 템플릿 전체에 whole-text
    패스로 적용한다(멱등). 결과는 자족(잔여 `{{...}}`·stray 마커 0) — post-render assertion 이
    잔존 시 RenderLeakError(자족 위반). free-form 토큰·omit-marker 는 어댑터에 없어야 하며
    잔존하면 leak 으로 표면화된다(미마이그레이션 신호).
    """
    operational = operational or {}
    result, detected_empty = _fill_operational(template_text, operational)
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
