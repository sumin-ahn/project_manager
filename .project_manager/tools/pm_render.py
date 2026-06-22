#!/usr/bin/env python3
"""어댑터 render 엔진 — 어댑터 파일을 *생성 산출물* 로 렌더한다 (ADR-0028·T-0131).

framework 본문 템플릿 + 채택자 overlay → 자족 .md. 렌더는 **두 primitive 만** —
slot-fill + conditional-omit (Jinja/표현식/루프 없음·DSL 아님). 이게 D17(엔진 fix →
어댑터 전파)의 근본 fix: framework 본문 변경을 전파하면서 채택자 customization 을
영영 clobber 하지 않는다 — 손편집되던 출하 .md 가 템플릿+overlay 의 재렌더 산출물이 된다.

토큰 3종 (어댑터 .md 의 `{{KEY}}` placeholder):
  - **free-form 3종** (FREEFORM_KEYS) — 채택자 손편집 산문(보존 난제). overlay.local.yaml 공급.
    값 있으면 SLOT-FILL, 빈/부재면 CONDITIONAL-OMIT(host 행/구역 drop). 이 엔진이 처리.
  - **operational** (OPERATIONAL_KEYS) — import 시 sed 치환된 리터럴(local.conf 재유도).
    plain string replace(omit 없음). 출하 파일엔 이미 리터럴이라 렌더는 보통 no-op.

자족 산출물 = 토큰 0 (ADR-0028): 렌더 결과에 잔여 `{{[A-Z_]+}}` 가 *하나라도* 있으면
post-render assertion 이 emission 순간 hard-fail(allow-list 없음). free-form 은 slot/omit
으로, operational 은 plain replace 로 전부 해소돼야 한다 — 미해소 operational(local.conf 미보유
key 등)·템플릿 저자가 새로 넣은 `{{FOO}}`·미배선 토큰을 침묵 출하 대신 큰소리로 표면화한다.
board.py `render-leak` lint 가 상시 backstop(2중 차단).

순수 함수 중심(stdlib + PyYAML). pm_update(재렌더)·pm_import(최초) 양쪽이 호출한다.
"""

from __future__ import annotations

import re
from pathlib import Path

# free-form 3종 — 채택자 손편집 산문 (overlay.local.yaml 공급·SLOT-FILL+CONDITIONAL-OMIT).
# pm_import.FREE_FORM_TOKENS(중괄호 포함)와 동일 집합을 bare key 로(이 엔진은 key 로 다룬다).
FREEFORM_KEYS: tuple[str, ...] = (
    "PROJECT_CONSTRAINTS",
    "PROTECTED_PATHS",
    "USER_GATE_ITEMS",
)

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

# overlay 파일 위치 (instance-owned·manifest-제외·§3.1).
OVERLAY_RELPATH = ".project_manager/overlay.local.yaml"

# DROP-SECTION 짝 마커 (§3.2). `<!-- pm:omit-if-empty KEY -->` … `<!-- /pm:omit-if-empty -->`.
# 빈 key 면 span 통째 drop·값 있으면 마커 줄만 strip(안쪽 유지). 렌더-제어 전용 → 출하물에선 항상 strip.
# 중첩 마커 미지원(§3.2 minimal) — open 다음 첫 close 가 짝. unmatched open(close 없음)은
# EOF 까지 span 으로 삼키고, 산출물에 잔존한 stray 마커는 _assert_no_leak 가 잡는다(아래).
_OMIT_OPEN_RE = re.compile(r"<!--\s*pm:omit-if-empty\s+([A-Z_]+)\s*-->")
_OMIT_CLOSE_RE = re.compile(r"<!--\s*/pm:omit-if-empty\s*-->")

# free-form 토큰 `{{KEY}}` 매칭 — FREEFORM_KEYS 만 (operational 은 의도적으로 제외).
_FREEFORM_TOKEN_RE = re.compile(
    r"\{\{(" + "|".join(re.escape(k) for k in FREEFORM_KEYS) + r")\}\}"
)

# 잔여 leak 스캔 — 대문자/언더스코어 토큰 (post-render assertion·잔존 시 무조건 raise).
_ANY_TOKEN_RE = re.compile(r"\{\{([A-Z_]+)\}\}")

# stray omit-marker 스캔 — 산출물에 잔존한 open/close 마커(중첩/미짝)는 무음 출하 금지.
_STRAY_MARKER_RE = re.compile(r"<!--\s*/?pm:omit-if-empty\b[^>]*-->")


class RenderLeakError(RuntimeError):
    """렌더 산출물에 리터럴 `{{...}}` 또는 stray omit-marker 가 잔존 — 미해소 leak(자족 산출물 위반)."""


def load_overlay(dest_root: Path) -> dict:
    """`dest_root/.project_manager/overlay.local.yaml` → flat dict (부재면 {}).

    PyYAML `yaml.safe_load` 사용 (런타임 이미 의존). 부재/빈 파일/non-dict → {} —
    그러면 모든 free-form host 가 conditional-omit(깨끗한 출하-기본·리터럴 토큰 0).
    이게 D17 근본 fix: 채택자가 overlay 를 한 번 편집 → 매 pm_update 가 fresh 템플릿 +
    그들 overlay 로 재렌더 → 엔진 본문 변경 전파 AND customization 생존.
    """
    overlay_path = Path(dest_root) / OVERLAY_RELPATH
    if not overlay_path.is_file():
        return {}
    import yaml  # 지연 import — overlay 미사용 경로에 PyYAML 강제하지 않음.

    try:
        data = yaml.safe_load(overlay_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _sole_freeform_token(line: str) -> str | None:
    """행에 free-form `{{KEY}}` 가 *정확히 1개* 면 그 key 반환, 아니면 None.

    None → 그 줄은 free-form slot/omit 대상이 아니다(operational 토큰·평문·마커는 별도 처리).
    2개 이상이면 host 단위가 모호하므로 None(엔진은 추론하지 않는다·§3.2 "템플릿에 인코딩").
    재저작 규율(T-0130)로 모든 free-form 토큰은 자체 host 행에 단독으로 앉으므로 1개가 정상.
    """
    matches = _FREEFORM_TOKEN_RE.findall(line)
    if len(matches) == 1:
        return matches[0]
    return None


def _fill_operational(text: str, operational: dict) -> str:
    """operational 토큰(`{{PROJECT_NAME}}` 등)을 plain string replace — omit 없음·행-문맥 불요.

    text 는 단일 행이든 산출물 전체든 무관(plain replace 라 멱등). render_adapter 는 루프 후
    *결과 전체* 에 최종 1회 호출해 operational+free-form 공존 행도 균일 처리한다. 출하/채움
    파일엔 이미 리터럴(import sed)이라 보통 no-op. operational 값이 주어지면 재렌더가 그
    토큰을 리터럴로 채운다(local.conf 재유도). operational 가 안 채운 토큰은 그대로 남고,
    그러면 _assert_no_leak 가 leak 으로 잡는다(자족 산출물 = 토큰 0·미해소 침묵 출하 금지).
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


def _slot_fill(line: str, key: str, value: str) -> str:
    """free-form SLOT-FILL — `{{KEY}}` 를 value 로 치환. 멀티라인 값은 토큰 컬럼에 indent.

    토큰 앞 들여쓰기(공백 prefix)를 추출해 value 의 2번째 줄 이후에도 같은 들여쓰기를
    붙인다 — bullet/구역 안 멀티라인 값이 정렬되게(§3.2 "토큰 컬럼에 indent").
    """
    token = "{{" + key + "}}"
    idx = line.find(token)
    # 토큰이 시작하는 컬럼까지의 prefix 가 전부 공백이면 그 들여쓰기를 이어붙임.
    prefix = line[:idx]
    indent = prefix if prefix.strip() == "" else ""
    if "\n" in value and indent:
        first, *rest = value.split("\n")
        value = first + "".join("\n" + (indent + r if r else r) for r in rest)
    return line.replace(token, value, 1)


def render_adapter(
    template_text: str,
    overlay: dict | None = None,
    operational: dict | None = None,
    *,
    source: str | None = None,
    _skip_assert: bool = False,
) -> str:
    """어댑터 템플릿 → 자족 .md (slot-fill + conditional-omit·§3.2).

    source: leak 에러에 실을 파일 경로(선택·render_file 이 전달). 진단용일 뿐 렌더엔 무영향.
    _skip_assert: 내부 재귀(drop-section 안쪽)용 — leak 검사는 최상위 result 에서 1회만.

    행 단위 스캔(free-form·마커):
      - DROP-SECTION 짝 마커 `<!-- pm:omit-if-empty KEY -->` … `<!-- /pm:omit-if-empty -->`:
        overlay 값 있으면 안쪽 유지(마커 줄만 strip), 빈/부재면 span 통째 drop. 마커는
        렌더-제어 전용이라 출하물에선 *항상* strip. 중첩 미지원(§3.2 minimal)·unmatched
        open(close 없음)은 EOF 까지 무음 삼킴 — 잔존 stray 마커는 assertion 이 차단.
      - free-form `{{KEY}}` 단독 행(DROP-LINE/BULLET): 값 있으면 SLOT-FILL, 빈/부재면 host 행 drop.

    operational 은 행-문맥 무관 plain string replace(omit 없음) → 루프 후 결과 전체에 *최종 1회*
    whole-text 패스로 균일 적용한다(operational+free-form 공존 행도 둘 다 해소·멱등). 결과는
    자족(잔여 `{{...}}`·stray 마커 0) — post-render assertion 이 잔존 시 RenderLeakError(자족 위반).
    """
    overlay = overlay or {}
    operational = operational or {}

    out_lines: list[str] = []
    lines = template_text.splitlines(keepends=True)
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        open_match = _OMIT_OPEN_RE.search(line)
        if open_match:
            # DROP-SECTION: 짝 close 마커까지 span 을 수집. 값 유무로 keep/drop 결정.
            key = open_match.group(1)
            span: list[str] = []
            j = i + 1
            while j < n and not _OMIT_CLOSE_RE.search(lines[j]):
                span.append(lines[j])
                j += 1
            # j = close 마커 줄 (또는 n=마커 미짝). 마커 줄들은 항상 strip.
            val = str(overlay.get(key, "")).strip()
            if val:
                # 값 있음 — 안쪽 유지(open/close 마커 줄만 제거). 안쪽의 free-form 토큰은 정상
                # 렌더 경로를 타도록 재귀 처리한다(중첩 마커 없음 가정·§3.2 minimal). operational
                # 은 최상위 whole-text 패스가 일괄 처리하므로 재귀엔 넘기지 않는다(중복 없음·멱등).
                # 재귀는 _skip_assert — leak 검사는 최상위 result 에서 source 와 함께 1회.
                inner = render_adapter(
                    "".join(span), overlay, _skip_assert=True)
                out_lines.append(inner)
            # 빈/부재 — span 전체 drop (아무것도 append 안 함).
            i = j + 1 if j < n else j
            continue

        tok = _sole_freeform_token(line)
        if tok is None:
            # free-form slot 아님 — 그대로 출력(operational 은 루프 후 whole-text 패스에서 채움).
            out_lines.append(line)
            i += 1
            continue

        val = str(overlay.get(tok, "")).strip()
        if val:
            out_lines.append(_slot_fill(line, tok, val))
        # 빈/부재 — CONDITIONAL-OMIT: host 행 drop (아무것도 append 안 함).
        i += 1

    result = "".join(out_lines)
    # operational 최종 패스(plain replace·omit 없음·행-문맥 불요·멱등) — operational+free-form
    # 공존 행도 균일 처리한다. _skip_assert 재귀는 자기 span 만 다루므로 여기서 일괄 1회면 충분.
    result = _fill_operational(result, operational)
    if not _skip_assert:
        _assert_no_leak(result, source=source)
    return result


def _assert_no_leak(text: str, *, source: str | None = None) -> None:
    """렌더 산출물에 잔여 `{{[A-Z_]+}}` 토큰 또는 stray omit-marker 가 있으면 RenderLeakError.

    자족 산출물 = 토큰 0 (ADR-0028·allow-list 없음): 잔여 토큰이 *하나라도* 있으면 emission
    순간 hard-fail. free-form 은 slot/omit, operational 은 plain replace 로 전부 해소돼야 한다
    — 미해소(템플릿 저자가 넣은 새 `{{FOO}}`·local.conf 미보유 operational·미배선 토큰)를
    침묵 출하 대신 큰소리로 표면화한다(half-rendered 토큰이 출하되는 것을 막음).

    추가로 stray omit-marker(`<!-- pm:omit-if-empty ... -->`·open 또는 close)가 잔존하면
    같은 에러로 잡는다 — 마커는 렌더-제어 전용이라 출하물엔 절대 없어야 하며, 중첩/미짝 마커가
    무음 출하되는 것을 방지(§3.2 minimal·중첩 미지원·unmatched open 은 EOF 까지 삼킴).
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
            f"템플릿 저자가 새 토큰을 넣었거나 overlay/local.conf 채널 배선이 누락됐다.")
    if stray:
        parts.append(
            f"stray omit-marker 잔존: {', '.join(dict.fromkeys(stray))} — 중첩/미짝 마커는 "
            f"렌더-제어 전용으로 출하 금지(§3.2 중첩 미지원).")
    raise RenderLeakError(f"렌더 산출물{where} 위반 — " + " / ".join(parts))


def render_file(
    template_path: Path,
    overlay: dict | None = None,
    operational: dict | None = None,
) -> str:
    """템플릿 파일 → 렌더 텍스트 (편의 래퍼·source 를 leak 에러에 명시)."""
    text = Path(template_path).read_text(encoding="utf-8")
    return render_adapter(text, overlay, operational, source=str(template_path))
