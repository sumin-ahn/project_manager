"""설치 공통 adapter_render (app)."""
from __future__ import annotations

from pm_engine.domain import home
from pm_engine.domain import render_rules
from pm_engine.infra import skill_inventory
from pm_engine.infra.harness import contract
import os

def _check_card_reasoning_values(
    home: home.Home,
    text: str,
    operational: dict,
    *,
    card_path: str | None,
    source: str | None,
) -> None:
    """카드에 채울 추론 값이 그 카드 하네스·모델의 허용집합 원소인지 확인한다(밖이면 RenderLeakError).

    대상은 텍스트에 토큰이 있고 operational 에 비어 있지 않은 값이 있는 추론 토큰뿐이다 — 부재는
    중화가, 빈 값은 `_assert_no_leak` 가 처리한다. 허용집합은 그런 값이 있을 때만 하네스 계약
    (`contract.reasoning_allowed`)에 묻는다 — 정적 허용값 하네스는 표를, 모델별 허용값 하네스
    (opencode)는 같은 프로필 모델의 variants 를 실행 Home·이 프로세스 env 로 조회한다. 채울 값이
    없는 렌더는 하네스 계약을 묻지 않는다. 하네스가 무효 값을 에러 없이 버리므로 값을 빼거나 기본값으로
    바꾸지 않고 렌더를 멈춘다. 카드 하네스를 판정할 수 없거나 허용집합을 확인할 수 없어도 멈춘다.
    """
    adapter_dir = render_rules._card_adapter_dir(card_path)
    card_harness = render_rules.card_harness_from_path(card_path)
    where = f" ({source})" if source else ""
    for token_key, conf_key in render_rules.DELEGATE_MODEL_CONF_KEYS.items():
        if not token_key.startswith(render_rules._DELEGATE_REASONING_TOKEN_PREFIX):
            continue
        value = operational.get(token_key)
        token = "{{" + token_key + "}}"
        if value is None or str(value) == "" or token not in text:
            continue
        setting = f"`{conf_key}={value}`"
        if adapter_dir is None:
            raise render_rules.RenderLeakError(
                f"추론 값 렌더{where} 실패 — 카드 하네스를 판정할 수 없어 {setting} 를 검증할 수 "
                f"없다(card_path={card_path!r}). 추론 값은 하네스가 정해진 카드에만 채운다.")
        model_token = render_rules._MODEL_TOKEN_BY_CONF_KEY[conf_key.rsplit(".", 1)[0] + ".model"]
        model = str(operational.get(model_token) or "") or None
        try:
            allowed = contract.reasoning_allowed(
                card_harness, model, cwd=home.root, env=dict(os.environ))
        except contract.HarnessContractError as exc:
            raise render_rules.RenderLeakError(
                f"추론 값 렌더{where} 실패 — {setting} 를 확인할 수 없다: {exc}. opencode 를 이 "
                "프로세스에서 실행할 수 있게 하거나 local.conf 의 그 줄을 고친 뒤 다시 실행한다."
            ) from exc
        if str(value) not in allowed:
            subject = f"{card_harness} {model}" if model else card_harness
            raise render_rules.RenderLeakError(
                f"추론 값 렌더{where} 실패 — {setting} 는 {subject} 허용집합"
                f"({', '.join(allowed) if allowed else '없음'}) 밖이다. 하네스는 허용집합 밖 값을 "
                "오류 없이 버리므로 카드에 싣지 않고 멈춘다. 저장이면 허용집합 안 값을 고르거나 "
                "비운다. 갱신 중이면 local.conf 의 그 줄을 고치거나, OpenCode variant 처럼 모델마다 "
                "허용집합이 다른 값은 pm-config update 를 다시 실행한다(새 갱신기의 conf 이관이 그 "
                "줄을 지우고 알린다).")


def render_adapter(
    home: home.Home,
    template_text: str,
    operational: dict | None = None,
    *,
    source: str | None = None,
    empty_keys: list[str] | None = None,
    template_dir: str | tuple[str, ...] | list[str] | None = None,
    delegate_harness: dict[str, str] | None = None,
    card_path: str | None = None,
) -> str:
    """어댑터 템플릿 → 자족 .md (operational plain replace).

    source: leak 에러에 실을 파일 경로(선택·render_file 이 전달). 진단용일 뿐 렌더엔 무영향.
    empty_keys: 호출자(pm_update)가 local.conf 빈값이라 dict 에서 제외한 token-key 목록(선택).
    렌더러가 직접 감지한 빈값 key 와 합쳐 leak 힌트("값을 채우라")에 싣는다.
    delegate_harness: 위임 token-key → conf 의 그 역할/티어 하네스(선택·pm_update 가 local.conf
    에서 해소). 카드 하네스와 다르면 그 토큰은 미사용 프로필이라 값 대신 이유를 중화로 남긴다.
    card_path: 이 산출물의 **인스턴스 상대 경로**(선택·pm_update 가 계획의 dest 좌표를 넘긴다).
    카드 하네스 판정의 유일한 입력이다 — 미배선이면 미사용 프로필 규칙이 발화하지 않는다.
    카드에 추론 값을 채울 때 그 값이 카드 하네스·모델의 허용집합(`contract.reasoning_allowed`) 밖이거나
    확인할 수 없으면 RenderLeakError 다(`_check_card_reasoning_values`).

    template_dir가 주어지면 operational보다 먼저 canonical 스킬 호출 토큰을 그 하네스 값으로
    치환한다. 값은 SKILL_ENTRY_PREFIX_BY_TEMPLATE_DIR 단일 registry에서만 읽는다.

    operational 은 행-문맥 무관 plain string replace(omit 없음) → 템플릿 전체에 whole-text
    패스로 적용한다(멱등). 결과는 자족(잔여 `{{...}}`·stray 마커 0) — post-render assertion 이
    잔존 시 RenderLeakError(자족 위반). free-form 토큰·omit-marker 는 어댑터에 없어야 하며
    잔존하면 leak 으로 표면화된다(미마이그레이션 신호).
    """
    operational = operational or {}
    # 미사용 프로필(카드 하네스 ≠ conf 의 그 역할 하네스)은 **치환 전에** 제외한다 — 값을 먼저
    # 채우면 아래 중화가 볼 토큰이 없어져 다른 하네스용 모델이 그대로 카드에 박힌다.
    unused = render_rules.unused_delegate_profiles(card_path, delegate_harness)
    if unused:
        operational = {
            key: value for key, value in operational.items() if key not in unused
        }
    result = (
        render_rules.render_skill_entry_notation(
            template_text, template_dir, skill_names=skill_inventory._installed_skill_entry_names(home), source=source
        )
        if template_dir is not None
        else template_text
    )
    # 추론 값 검증은 미사용 프로필을 뺀 **뒤**, 치환 **전**이다 — 이 카드에 실제로 채울 값만 본다.
    _check_card_reasoning_values(
        home,
        result, operational, card_path=card_path, source=source,
    )
    result, detected_empty = render_rules._fill_operational(result, operational)
    # pm_update 가 excluded 한 빈값 key(empty_keys) + 렌더러가 직접 감지한 빈값 key 를 합쳐
    # leak 힌트에 싣는다(중복 제거·순서 보존).
    all_empty = list(dict.fromkeys([*(empty_keys or []), *detected_empty]))
    # intentional-TODO graceful (import 대칭): harness.opencode.pro_model 이 local.conf 에
    # *부재*(채택자가 opencode 없이 import — 키 자체가 없음)면 `{{OPENCODE_PRO_MODEL}}` 을 leak
    # 시키는 대신 model: 줄을 주석화·중화한다(import --fill manual 과 byte-동일·재렌더 왕복 0·
    # 불변식 a: 이미 해소돼 있으면 위 _fill_operational 가 치환해 no-op).
    #   ⚠ **`harness.opencode.pro_model=` 빈값(present-but-empty)은 중화하지 않는다** — 빈값은 미설정이
    #   아니라 *오설정* 신호다(pm_import 는 해소 시에만 이 키를 쓰므로 빈값=손-편집/손상).
    #   대로 leak 시켜 "값을 채우라" 로 표면화한다. 빈값은
    #   all_empty 에 실리므로 그때는 중화를 건너뛰어 토큰을 남긴다 → _assert_no_leak 가 잡는다.
    # 그 밖의 미해소 토큰(다른 위치·다른 `{{...}}`)도 계속 fail-loud(불변식 c).
    if render_rules._OPENCODE_MODEL_KEY not in all_empty:
        result, _ = render_rules.neutralize_model_todo(result)
    # 같은 intentional-TODO 규약 — 역할 카드의 `{{DELEGATE_MODEL_<ROLE>}}` 는 local.conf 에
    # `delegate.<role>[.<tier>].model` 이 없는 채택자(위임 매핑 미설정)에서 결정적으로 해소되지
    # 않는다. 빈값(present-but-empty) 키는 all_empty 로 넘겨 중화에서 빼고 leak 으로 표면화한다
    # (오설정 신호). 미사용 프로필은 해소값 유무와 무관하게 사유 꼬리로 중화한다(위 unused).
    result, _ = render_rules.neutralize_delegate_model_todo(
        result, all_empty,
        unused_profiles=unused,
        card_harness=render_rules.card_harness_from_path(card_path),
    )
    render_rules._assert_no_leak(result, source=source, empty_keys=all_empty)
    return result
