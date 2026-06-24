#!/usr/bin/env python3
"""어댑터 render 엔진 — 어댑터 파일을 *생성 산출물* 로 렌더한다 (ADR-0028·T-0131·ADR-0031).

framework 본문 템플릿 → 자족 .md. operational 토큰(`{{KEY}}` placeholder)을 local.conf
재유도값으로 plain string replace 한다(Jinja/표현식/루프 없음·DSL 아님). 이게 D17(엔진 fix →
어댑터 전파)의 근본 fix: framework 본문 변경을 전파하면서 채택자 customization(local.conf)을
영영 clobber 하지 않는다 — 손편집되던 출하 .md 가 템플릿의 재렌더 산출물이 된다.

토큰 (어댑터 .md 의 `{{KEY}}` placeholder):
  - **operational** (OPERATIONAL_KEYS) — import 시 sed 치환된 리터럴(local.conf 재유도).
    plain string replace(omit 없음). 출하 파일엔 이미 리터럴이라 렌더는 보통 no-op.

free-form(채택자 손편집 산문 — `{{PROJECT_CONSTRAINTS}}` 등)은 *이 엔진이 다루지 않는다*:
ADR-0030 이 free-form 을 canonical home(root doc §프로젝트 고유 제약 + `pm_role.local.md`
§보호 영역)으로 일원화하고, pm_import 의 FILL 채널(`FREE_FORM_TOKENS`)이 전담한다. render-overlay
free-form value-fill 기계(overlay.local.yaml·`FREEFORM_KEYS`·slot-fill·conditional-omit)는
ADR-0031 로 제거됐다 — free-form 은 FILL 채널 단일 채널이고 어댑터는 free-form-free 다
(`tests/test_adapter_free_form_free.py` lock-in).

자족 산출물 = 토큰 0 (ADR-0028): 렌더 결과에 잔여 `{{[A-Z_]+}}` 가 *하나라도* 있으면
post-render assertion 이 emission 순간 hard-fail(allow-list 없음). operational 은 plain replace
로 해소돼야 한다 — 미해소 operational(local.conf 미보유 key 등)·템플릿 저자가 새로 넣은
`{{FOO}}`·미배선 토큰을 침묵 출하 대신 큰소리로 표면화한다. board.py `render-leak` lint 가
상시 backstop(2중 차단).

순수 함수 중심(stdlib). pm_update(재렌더)·pm_import(최초) 양쪽이 호출한다.
"""

from __future__ import annotations

import re
from pathlib import Path

# operational — import sed 치환된 리터럴 (local.conf 재유도). plain replace·omit 없음.
# pm_import.OPERATIONAL_TOKENS(중괄호 포함)와 동일 집합을 bare key 로 + opencode 전용
# OPENCODE_PRO_MODEL(opencode 채택자 local.conf 만 보유·claude tree 엔 토큰 부재 → no-op).
#
# ⚠️ D17-2 forward-flag (@render 활성화 시점): local.conf(board.py init 산출)는 이 중
#    일부만 보유한다 — DATE·PROJECT_ROOT·PROJECT_TAGLINE 는 init 이 채우지 않을 수 있어
#    pm_update._operational_from_local_conf 가 그 token-key 를 dict 에 안 넣는다. 엄격 가드
#    (_assert_no_leak·토큰 0) 하에선, @render 활성화된 어댑터 파일이 그런 미보유 operational
#    토큰을 *담고 있으면 렌더가 실패한다 — 그게 옳다*(미해소를 침묵 출하 대신 표면화). 현재
#    @render path 0 이라 안 깨진다. 활성화 시점(D17-2)에 그 파일들의 operational 해소(또는
#    local.conf 채널 확장)를 보장하는 건 D17-2 몫. 이 엔진은 leak 을 표면화할 뿐 채우지 않는다.
OPERATIONAL_KEYS: tuple[str, ...] = (
    "PROJECT_NAME",
    "PROJECT_TAGLINE",
    "PROJECT_ROOT",
    "PY",
    "TEST_CMD",
    "DATE",
    # opencode 어댑터 전용 — pm_import 가 local.conf 에 opencode_pro_model 을 기록(T-0033).
    # opencode @render 활성화 시 `{{OPENCODE_PRO_MODEL}}` 토큰이 미배선이면 leak 하므로
    # operational 채널에 포함한다(local.conf 재유도·plain replace).
    "OPENCODE_PRO_MODEL",
)

# 잔여 leak 스캔 — 대문자/언더스코어 토큰 (post-render assertion·잔존 시 무조건 raise).
_ANY_TOKEN_RE = re.compile(r"\{\{([A-Z_]+)\}\}")

# stray omit-marker 스캔 — 산출물에 잔존한 옛 free-form drop-section 마커는 무음 출하 금지.
# free-form value-fill 기계(ADR-0031 제거)가 처리하던 `<!-- pm:omit-if-empty KEY -->` 류는
# 이제 *어느 어댑터에도 없어야 한다*(어댑터 free-form-free·ADR-0030). 잔존 시 미마이그레이션
# 신호로 leak 처리한다(침묵 출하 대신 표면화).
_STRAY_MARKER_RE = re.compile(r"<!--\s*/?pm:omit-if-empty\b[^>]*-->")


class RenderLeakError(RuntimeError):
    """렌더 산출물에 리터럴 `{{...}}` 또는 stray omit-marker 가 잔존 — 미해소 leak(자족 산출물 위반)."""


def _fill_operational(text: str, operational: dict) -> str:
    """operational 토큰(`{{PROJECT_NAME}}` 등)을 plain string replace — omit 없음·행-문맥 불요.

    text 는 단일 행이든 산출물 전체든 무관(plain replace 라 멱등). 출하/채움 파일엔 이미
    리터럴(import sed)이라 보통 no-op. operational 값이 주어지면 재렌더가 그 토큰을 리터럴로
    채운다(local.conf 재유도). operational 가 안 채운 토큰은 그대로 남고, 그러면 _assert_no_leak
    가 leak 으로 잡는다(자족 산출물 = 토큰 0·미해소 침묵 출하 금지).
    """
    if not operational:
        return text
    for key in OPERATIONAL_KEYS:
        if key not in operational:
            # 미보유 key 는 치환하지 않는다 — 토큰을 그대로 남겨 _assert_no_leak 가 잡게 한다.
            # `.get(key, "")` 로 빈 문자열 치환하면 미해소를 *침묵 비움*(예: 기존 채택자
            # local.conf 미보유 opencode_pro_model → `model: ` 로 덮음)으로 출하한다(codex·docstring 의도).
            continue
        token = "{{" + key + "}}"
        if token in text:
            text = text.replace(token, str(operational[key]))
    return text


def render_adapter(
    template_text: str,
    operational: dict | None = None,
    *,
    source: str | None = None,
) -> str:
    """어댑터 템플릿 → 자족 .md (operational plain replace·§3.2·ADR-0031).

    source: leak 에러에 실을 파일 경로(선택·render_file 이 전달). 진단용일 뿐 렌더엔 무영향.

    operational 은 행-문맥 무관 plain string replace(omit 없음) → 템플릿 전체에 whole-text
    패스로 적용한다(멱등). 결과는 자족(잔여 `{{...}}`·stray 마커 0) — post-render assertion 이
    잔존 시 RenderLeakError(자족 위반). free-form 토큰·omit-marker 는 어댑터에 없어야 하며
    (ADR-0030 free-form-free), 잔존하면 leak 으로 표면화된다(미마이그레이션 신호).
    """
    operational = operational or {}
    result = _fill_operational(template_text, operational)
    _assert_no_leak(result, source=source)
    return result


def _assert_no_leak(text: str, *, source: str | None = None) -> None:
    """렌더 산출물에 잔여 `{{[A-Z_]+}}` 토큰 또는 stray omit-marker 가 있으면 RenderLeakError.

    자족 산출물 = 토큰 0 (ADR-0028·allow-list 없음): 잔여 토큰이 *하나라도* 있으면 emission
    순간 hard-fail. operational 은 plain replace 로 전부 해소돼야 한다 — 미해소(템플릿 저자가
    넣은 새 `{{FOO}}`·local.conf 미보유 operational·미배선 토큰·옛 free-form 토큰 잔존)를
    침묵 출하 대신 큰소리로 표면화한다(half-rendered 토큰이 출하되는 것을 막음).

    추가로 stray omit-marker(`<!-- pm:omit-if-empty ... -->`·open 또는 close)가 잔존하면
    같은 에러로 잡는다 — 옛 free-form drop-section 마커는 ADR-0031 로 제거된 기계의 잔재라
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
            f"미해소 토큰 잔존: {toks} — 자족 산출물(ADR-0028)은 토큰 0 이어야 한다. "
            f"템플릿 저자가 새 토큰을 넣었거나 local.conf 채널 배선이 누락됐다.")
    if stray:
        parts.append(
            f"stray omit-marker 잔존: {', '.join(dict.fromkeys(stray))} — 옛 free-form "
            f"drop-section 마커(ADR-0031 제거)는 어댑터에 절대 없어야 한다.")
    raise RenderLeakError(f"렌더 산출물{where} 위반 — " + " / ".join(parts))


def render_file(
    template_path: Path,
    operational: dict | None = None,
) -> str:
    """템플릿 파일 → 렌더 텍스트 (편의 래퍼·source 를 leak 에러에 명시)."""
    text = Path(template_path).read_text(encoding="utf-8")
    return render_adapter(text, operational, source=str(template_path))
