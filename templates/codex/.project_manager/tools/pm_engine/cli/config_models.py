"""설치된 하네스의 위임 모델 안내·조회 (층: cli)."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pm_engine.app import import_fill
from pm_engine.cli import config_status
from pm_engine.domain import install_rules
from pm_engine.infra import conf_store
from pm_engine.infra.harness import contract


_CONSOLE_MENU = (
    "\n메뉴: [r] repo 추가 · [w] worktree 추가 · [b] 슬롯 빌드명령 설정/변경 · "
    "[m] 위임 모델 안내 · [u] 엔진 갱신 · [s] 새로고침 · [q] 종료"
)


# ── 위임 모델 안내 (`delegate.<role>[.hard].model` 에 무엇을 넣는가) ────────────
# 모델 **목록을 엔진에 하드코딩하지 않는다** — 모델은 자주 바뀌고 하드코딩은 곧 stale 이다.
# 조회 수단이 있는 하네스만 실조회 목록을 보여주고, 없는 하네스는 값 **형식**과 확인 방법만
# 안내한다. 값 형식이 하네스마다 근본적으로 달라 값 자체의 통일은 불가능하고, 통일 대상은
# 취급 방식(= local.conf 단일 진실)이다.
#
# 형식·확인 방법·원천의 표는 이 파일에 없다 — `contract.MODEL_SOURCE` 한 자리이고 사람 안내
# (`[m]`)·기계 조회(`delegate models`)·설정 화면 선택지가 모두 그 표를 읽는다.
# 실조회 목록은 길 수 있다 — 안내는 콘솔 한 화면을 넘기지 않는다(전량은 조회 명령으로).
_DELEGATE_MODEL_LIST_LIMIT = 12
_MODEL_ALIAS_CONF_PREFIX = "delegate.model_alias."


def _installed_harnesses() -> list[str]:
    """등록 하네스 중 **이 환경에 설치된** 것만 (registry 순서 보존).

    설치되지 않은 하네스의 모델을 안내하지 않는다 — 채택자가 쓸 수 없는 값을 고르게 만드는
    안내는 안내가 아니다. 판정은 `import_fill._harness_binary_available`(shutil.which) 재사용이라
    새 감지 수단을 만들지 않는다.
    """
    return [
        harness
        for harness in install_rules.REGISTERED_HARNESSES
        if import_fill._harness_binary_available(harness)
    ]


def _model_alias_members(conf: dict) -> dict[str, list[str]]:
    """`delegate.model_alias.<name>=m1, m2` → {alias: [members]} (선언 없으면 빈 dict).

    alias 표의 **의미는 바꾸지 않는다** — 같은 기반 모델의 묶음이고(lint `delegate-same-model`
    판정용), 안내는 그 멤버를 "이 환경에서 쓰는 모델" 로 보여줄 뿐이다. 목록 선언용으로 alias
    를 강제하지 않는다(한 모델만 쓰는 채택자가 목록 때문에 alias 를 만들 이유가 없어야 한다).
    """
    members: dict[str, list[str]] = {}
    for key, raw in conf.items():
        if not key.startswith(_MODEL_ALIAS_CONF_PREFIX):
            continue
        name = key[len(_MODEL_ALIAS_CONF_PREFIX):].strip()
        values = [part.strip() for part in str(raw).split(",") if part.strip()]
        if name and values:
            members[name] = values
    return members


def _print_delegate_model_guidance(
    home,
    *,
    conf=None,
) -> int:
    """`[m]` — 설치된 하네스별로 `delegate.<role>[.hard].model` 에 넣을 값을 안내한다.

    키 형식은 문서에 있지만 **유효한 값의 목록·확인 방법**이 어디에도 없었다. 위임 시점 검증
    함수는 만들지 않는다(codex 값은 자유 문자열이라 형식 검증이 공허하고, alias 미선언 채택자에선
    멤버십 검증이 아무것도 잡지 않는다) — 이 비대칭은 안내로 닫는다.

    조회 실패(바이너리 부재·rc·timeout·파싱 0)는 형식 안내로 **강등**하되 조용하지 않다:
    강등 사실을 그 자리에 적고, 실패 사유 자체는 조회 seam 이 stderr 한 줄로 남긴다.
    conf 주입으로 hermetic 테스트(라이브 CLI 미구동).
    """
    model_source = contract.MODEL_SOURCE

    print("\n## 위임 모델 안내 (local.conf `delegate.<role>[.hard].model` 단일 진실):")
    installed = _installed_harnesses()
    if not installed:
        print("  (설치된 하네스 없음 — 안내할 대상이 없다. 하네스를 설치한 뒤 다시 보라.)")
    for harness in installed:
        source = model_source[harness]
        value_format, how_to_check = source.value_format, source.how_to_check
        print(f"  - {harness}: 값 형식 {value_format} · 확인 {how_to_check}")
        if source.kind != "live":
            continue
        runner = import_fill._real_models_runner
        ok, models = runner(source)
        if not ok or not models:
            print(
                "      ⚠ 목록 조회 실패 — 위 형식 안내로 강등한다(사유는 위 stderr 한 줄). "
                f"직접 확인: {how_to_check}"
            )
            continue
        shown = models[:_DELEGATE_MODEL_LIST_LIMIT]
        print(f"      가용 모델 {len(models)}개: {', '.join(shown)}")
        if len(models) > len(shown):
            print(f"      (앞 {len(shown)}개만 표시 — 전량은 {how_to_check})")

    values = (conf if conf is not None
              else conf_store.load_checked(home.local_conf))
    aliases = _model_alias_members(values or {})
    if aliases:
        print("  이 환경에서 쓰는 모델 (`delegate.model_alias` 선언 멤버):")
        for name in sorted(aliases):
            print(f"    - {name}: {', '.join(aliases[name])}")
    else:
        print(
            "  (`delegate.model_alias` 선언 없음 — 같은 기반 모델의 다른 표기를 묶을 때만 "
            "선언한다. 목록을 위해 만들 필요는 없다.)"
        )
    return 0


def _console_delegate_models(home) -> None:
    """`[m]` 콘솔 액션 — 모델 안내를 출력만 한다(장부 변경 0)."""
    _print_delegate_model_guidance(home)
    return None


# ── 모델 목록 조회 (`delegate models --harness <h> --json`) ────────────────────
# 설정 화면과 사람이 "이 하네스에 무엇을 적나" 를 받는 **읽기 전용** 자리다. 조회는 `contract.model_listing`
# 하나이고(하네스 이름 분기 0) 응답은 `{"harness", "kind", "models", "variants"}` 네 필드다. 조회는 파일을
# 쓰지 않고, 실패를 빈 목록으로 위장하지 않는다 — 실패(0개 포함)는 rc 3 이다.
_DELEGATE_MODELS_QUERY_FAILED = 3


def _conf_assignment(text: str) -> tuple[str, str]:
    """`--set 키=값` 한 개 → (키, 값). 빈 값(`키=`)은 그 줄을 지우라는 뜻이다."""
    key, separator, value = text.partition("=")
    if not separator or not key.strip():
        raise argparse.ArgumentTypeError(f"`키=값` 꼴이 아니다: {text!r}")
    return key.strip(), value.strip()


# 반영 전 거부 — 처리하지 못한 예외로 끝난 파이썬의 1 과 argparse 인자 오류의 2 와 겹치지 않는 전용
# 값이다. 관리자 저장은 이 값만 "잘못된 값"(`invalid-value`)으로 보인다
# (`manager/bridge_settings.py` 의 `APPLY_REFUSED_RC`).
_APPLY_REFUSED = 3


def cmd_delegate_models(home, args, *, listing=None) -> int:
    """`delegate models --harness <h> --json` — 그 하네스가 인정하는 모델 목록(읽기 전용).

    응답은 `{"harness", "kind", "models", "variants"}` 하나다. `variants` 는 모델별 추론 강도
    (`{모델: [variant…]}`, variant 없는 모델은 `[]`)이고 그것을 내는 원천(opencode)만 채운다 — 나머지는
    null. 모델 목록과 같은 한 번의 조회에서 나온다. `free_text`(모델은 검증상 언제나 자유 문자열)는
    싣지 않는다 — 그 사실은 키 레지스트리의 `choices.free_text` 가 소유하고, 두 자리에 두면 갈린다.
    `listing` 은 조회 함수(원천 행 → `ModelListing`)이고 기본은 `contract.model_listing` 을 이 사본
    폴더·이 프로세스 env 로 부르는 것이다.
    rc: 0 성공 · 2 `--harness`/`--json` 미지정·미지원 · 3 조회 실패(사유는 stderr 한 줄).
    """
    table = contract.MODEL_SOURCE
    source = table.get(args.harness)
    if source is None:
        print(f"[중단] 미지원 harness {args.harness!r} — 지원: {', '.join(table)}.",
              file=sys.stderr)
        return 2
    lookup = listing if listing is not None else (
        lambda row: contract.model_listing(row, cwd=home.root, env=dict(os.environ)))
    try:
        result = lookup(source)
    except contract.HarnessContractError as exc:
        print(f"[중단] {args.harness} 모델 목록을 얻지 못했다 — {exc}", file=sys.stderr)
        return _DELEGATE_MODELS_QUERY_FAILED
    variants = None if result.variants is None else {
        model: list(names) for model, names in result.variants.items()}
    config_status._write_machine_line(json.dumps(
        {"harness": args.harness, "kind": source.kind, "models": list(result.models),
         "variants": variants},
        ensure_ascii=False))
    return 0
