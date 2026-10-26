"""위임 대상 설정 해소 (층: infra)."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from pm_engine.domain import conf_schema
from pm_engine.infra.harness import contract
import datetime
from pm_engine.infra.harness import delegate_env
from pm_engine.domain import delegate_errors
from pm_engine.domain import delegate_run_rules



def _is_enabled(conf: dict[str, str]) -> bool:
    """위임 허용 여부 — **키 부재·빈값은 허용**(기본 ON)이고 명시 거부만 차단한다.

    기본을 OFF 로 두면 기존 채택자의 native 위임이 이 릴리즈에서 새로 막히는 회귀가 된다
    (현행 native 무게이트 동작 보존이 기준). 명시 `false` 만 위임 전면 비허용이다.
    빈값은 conf 파싱 의미대로 **미설정**이라 기본값으로 간다(`delegate.enabled=` 한 줄이
    위임을 조용히 끄지 않는다).
    """
    raw = conf.get(delegate_run_rules.DELEGATE_ENABLED_KEY, "").strip().lower()
    if not raw:
        return True
    return raw in conf_schema.DELEGATE_ENABLED_VALUES


# ── config 해소 (원자 tuple) ────────────────────────────────────────────

def _validate_harness(harness: str) -> str:
    """공용 드라이버 계약(`contract`) 검증의 얇은 wrapper — 예외만 이 표면 타입으로 옮긴다."""
    try:
        return contract.validate_harness(harness)
    except contract.HarnessContractError as exc:
        raise delegate_errors.DelegateError(str(exc)) from exc


def _validate_reasoning(
    harness: str, reasoning: str | None, *, model: str, cwd: Path,
) -> str | None:
    """실행 직전 확인 — reasoning 이 그 하네스·모델의 허용집합 원소인가(공용 계약 wrapper).

    허용집합은 실행과 같은 cwd·env(`build_env(harness)`)로 해소한다 — opencode 는 그 조건의 모델
    목록 조회 결과가 정한다. 밖이거나 확인할 수 없으면 fail-loud(조용한 무시/자동 강등 금지).
    미지정=None(조회 없음). 해소 함수(`resolve_delegate`·`resolve_fallback`)는 실행하지 않는
    경로(채널 가드 훅·프로필 비교)에서도 불리므로 이 확인을 하지 않는다."""
    try:
        return contract.validate_reasoning(
            harness, reasoning, model=model, cwd=cwd, env=delegate_env.build_env(harness))
    except contract.HarnessContractError as exc:
        raise delegate_errors.DelegateError(str(exc)) from exc


def resolve_delegate(
    conf: dict[str, str],
    role: str,
    tier: str,
    cli_harness: str | None,
    cli_model: str | None,
    cli_reasoning: str | None,
) -> tuple[str, str, str | None]:
    """(harness, model, reasoning) 원자 tuple 을 해소한다(단일 알고리즘).

    CLI 완전지정(--harness AND --model)이면 설정 미참조(원자 override). 아니면 티어 키 세트를 통째로
    읽는다(`delegate.<role>[.<tier>].{harness,model,reasoning}` — 혼합 상속 금지). harness/model 부재면
    fail-loud(hard 미설정=normal 강등 금지·normal 미설정=조용한 claude 폴백 금지). CLI 부분 override
    (--harness 만/--model 만)는 호출 전 usage error 로 걸러진 전제(여기선 방어적 재검).
    """
    if cli_harness or cli_model:
        if not (cli_harness and cli_model):
            raise delegate_errors.DelegateError("--harness 와 --model 은 동반 필수(부분 override 금지·원자 tuple).")
        harness = _validate_harness(cli_harness)
        return harness, cli_model, contract.normalize_reasoning(cli_reasoning)

    key = f"delegate.{role}" + (".hard" if tier == "hard" else "")
    harness = (conf.get(f"{key}.harness") or "").strip()
    model = (conf.get(f"{key}.model") or "").strip()
    reasoning = conf.get(f"{key}.reasoning")

    if not harness or not model:
        if tier == "hard":
            raise delegate_errors.DelegateError(
                f"hard 프로필 미설정({key}.harness/.model) — normal 강등 금지·명시 설정을 요구한다"
                ". local.conf 에 hard 티어 세트를 통째로 설정하라."
            )
        raise delegate_errors.DelegateError(
            f"역할 매핑 미설정({key}.harness/.model) — 조용한 폴백 금지. "
            "local.conf 에 delegate.<role>.harness/.model 을 설정하라."
        )
    harness = _validate_harness(harness)
    return harness, model, contract.normalize_reasoning(reasoning)


def resolve_fallback(
    conf: dict[str, str],
    role: str,
    tier: str,
) -> tuple[str, str, str | None] | None:
    """명시된 1단 폴백 tuple 을 해소한다.

    기존 역할 매핑과 동형인
    `delegate.<role>[.hard].fallback.{harness,model,reasoning}` 세트를 통째로 읽는다. 세 키가 모두
    없으면 폴백 미설정(None)이고, 하나라도 있으면 harness/model 완전 세트를 요구한다. hard 는 normal
    폴백을 상속하지 않는다 — 티어 혼합 상속은 주 매핑과 똑같이 금지한다. 엔진 기본 폴백은 없으며,
    미설정은 기존 fail-loud 를 보존한다.
    """
    key = f"delegate.{role}" + (".hard" if tier == "hard" else "") + ".fallback"
    harness = (conf.get(f"{key}.harness") or "").strip()
    model = (conf.get(f"{key}.model") or "").strip()
    reasoning_raw = conf.get(f"{key}.reasoning")
    configured = bool(harness or model or (reasoning_raw and reasoning_raw.strip()))
    if not configured:
        return None
    if not harness or not model:
        raise delegate_errors.DelegateError(
            f"폴백 매핑 불완전({key}.harness/.model) — 폴백은 원자 tuple 로 명시해야 한다. "
            "부분 설정/조용한 기본값은 허용하지 않는다."
        )
    harness = _validate_harness(harness)
    return harness, model, contract.normalize_reasoning(reasoning_raw)


def resolved_delegate_profile(
    harness: str, model: str, reasoning: str | None,
) -> str:
    """stderr/raw가 공유하는 pm_delegate 해소 tuple."""
    return f"(harness={harness}, model={model}, reasoning={reasoning})"


def _format_silence(silence_sec: float | None, idle_killed: bool) -> str:
    """감사 헤더의 침묵 관측치 1줄 값 — 다음 kill 의 원인을 사후 확정하는 입력.

    관측 불가(신호 없는 축·주입 run_fn 등)는 `n/a` 로 남긴다 — 0 으로 위장하지 않는다(관측 없음과
    "침묵 0초"는 다른 사실이다). 무진행 kill 은 사유를 병기해 하네스 상한/기타 kill 과 구분한다."""
    if silence_sec is None:
        return "n/a (진행 신호 미관측)"
    label = f"{silence_sec:.1f}"
    return f"{label} (무진행 판정으로 중단)" if idle_killed else label


def _format_meta(argv: list[str], rc: int, harness: str, model: str,
                 elapsed: float, stdout: str, stderr: str, *,
                 attempt: str = "primary", primary_raw: str | None = None,
                 reasoning: str | None = None,
                 local_conf_path: Path | None = None,
                 profile_source: str = "local-conf",
                 silence_sec: float | None = None,
                 idle_killed: bool = False,
                 transport_sandbox_path: str | None = None,
                 transport_prompt_path: str | None = None,
                 transport_binding_mode: str | None = None) -> str:
    """raw 박제 본문 — 메타(argv·rc·모델·소요·침묵) 헤더 + 원문.

    폴백 attempt 는 `# primary_raw:` 로 앞선 primary raw 경로를 적어 **raw 파일 하나만 봐도** 감사
    체인(왜 이 하네스로 갔는가)이 닫히게 한다. `# silence_sec:` 은 최종 진행 이벤트 이후 침묵으로,
    kill 이 났을 때 원인(하네스 상한 vs 무진행 vs 기타)을 사후에 가르는 유일한 관측치다 — 누적 8회
    kill 이 전부 원인 불명이었던 구멍을 이 줄이 닫는다."""
    header = [
        "# pm_delegate raw 출력 (감사)",
        f"# harness: {harness}",
        f"# model: {model}",
        f"# attempt: {attempt}",
    ]
    if local_conf_path is not None:
        header.extend([
            f"# local_conf: {local_conf_path}",
            f"# profile_source: {profile_source}",
            f"# resolved_profile: {resolved_delegate_profile(harness, model, reasoning)}",
        ])
    if primary_raw:
        header.append(f"# primary_raw: {primary_raw}")
    if transport_sandbox_path is not None:
        header.append(
            f"# transport_sandbox_path_lexical: {transport_sandbox_path}"
        )
    if transport_prompt_path is not None:
        header.append(
            f"# transport_prompt_path_lexical: {transport_prompt_path}"
        )
    if transport_binding_mode is not None:
        header.append(f"# transport_binding_mode: {transport_binding_mode}")
    header += [
        f"# argv: {' '.join(argv)}",
        f"# rc: {rc}",
        f"# elapsed_sec: {elapsed:.1f}",
        f"# silence_sec: {_format_silence(silence_sec, idle_killed)}",
        f"# at: {datetime.datetime.now().isoformat(timespec='seconds')}",
        "",
        "## stdout",
        stdout,
        "",
        "## stderr",
        stderr,
    ]
    return "\n".join(header)


def _resolved_mapping(
    role: str,
    tier: str,
    conf: Mapping[str, object],
) -> tuple[str, str, str]:
    """Resolve through ``resolve_delegate`` without a second policy.

    The engine owns the exact key contract: normal reads only
    ``delegate.<role>.*`` and hard reads only ``delegate.<role>.hard.*``;
    tuples are atomic and hard never inherits normal.  A configuration error is
    returned as data so this guard can stay fail-open without inventing a
    runnable redirect that the engine itself would reject.
    """
    try:
        harness, model, _reasoning = resolve_delegate(
            dict(conf), role, tier, None, None, None
        )
    except delegate_errors.DelegateError as exc:
        return "", "", str(exc)
    return harness, model, ""
