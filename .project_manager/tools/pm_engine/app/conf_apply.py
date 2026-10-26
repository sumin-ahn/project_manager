"""설정 제안 값의 계획·검증·기록과 부분 실패 보고 (층: app)."""
from __future__ import annotations

from collections.abc import Callable
from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple
import os
from pm_engine.app import harness_ctx
from pm_engine.app import update_plan
from pm_engine.app import update_render
from pm_engine.domain import conf_schema
from pm_engine.domain import render_rules
from pm_engine.domain import upstream_rules
from pm_engine.infra import fs
from pm_engine.infra import install_conf
from pm_engine.infra import upstream_git
from pm_engine.infra.harness import contract


# ── local.conf 반영 (`pm-config apply`) ──────────────────────────────────────
# 설정 저장의 입구는 하나다. 제안 값(현재 local.conf + `--set`)을 만들고, 바뀐 키가 걸리는 반영 단계의
# 계획을 전부 끝낸 뒤에만 local.conf → 단계 순서대로 산출물을 쓴다. 계획 단계의 거부는 아무것도 쓰지
# 않았다는 뜻이고 되돌리기 쓰기는 없다. 단계는 키 묶음 표(`CONF_APPLY_STEPS`) 하나로 고르며, 어느
# 묶음에도 없는 키는 local.conf 기록만 한다. 추론 값 확인·계획 판정·산출물 그리기는 같은 하네스 계약
# (`infra.harness.contract`)의 모델 목록 조회 기억을 나눈다.
_LOCAL_CONF_RELPATH = ".project_manager/local.conf"


class ConfApplyError(ValueError):
    """설정 반영 실패 — `write_stage` 가 거짓이면 반영 전 거부(아무 파일도 쓰지 않았다), 참이면 쓰기 단계에서
    멈췄고 `written` 이 그때까지 바꾼 파일이다(바꾼 파일이 없어도 거부가 아니다)."""

    def __init__(self, violations, *, written=(), write_stage: bool = False) -> None:
        self.violations = tuple(violations)
        self.written = tuple(written)
        self.write_stage = write_stage
        if self.written:
            lead = f"{', '.join(self.written)} 를 바꾼 뒤 멈췄습니다"
        elif write_stage:
            lead = "쓰기 단계에서 멈췄습니다 (바꾼 파일 없음)"
        else:
            lead = "설정을 반영하지 않았습니다 (local.conf·파생 파일 그대로)"
        super().__init__("\n".join((
            f"{lead} — 아래를 고친 뒤 다시 실행하십시오.",
            *(f"  - {violation}" for violation in self.violations))))


class ConfApplyResult(NamedTuple):
    """`apply_conf` 결과 — local.conf 가 바뀌었나, 파생 파일에 반영한 줄."""
    changed: bool
    applied: tuple[str, ...]


def render_input_conf_keys() -> frozenset[str]:
    """`@render` 산출물의 렌더 값이 되는 local.conf 키 — 운영 키·위임 모델/추론 키와 위임 하네스 키.

    새 표를 두지 않는다: 렌더 값을 만드는 두 함수(`_operational_from_local_conf`·
    `_delegate_harness_from_local_conf`)가 읽는 표에서 파생한다."""
    return (frozenset(update_render._local_conf_operational_map())
            | frozenset(render_rules.DELEGATE_HARNESS_CONF_KEYS.values()))


def _installed_generation_source(dest_root: Path, conf: Mapping[str, str]) -> Path:
    """렌더 원본 — 설치 세대(`upstream.rev`)와 같은 커밋의 상류 트리. 다르면 `ConfApplyError`.

    fetch 하지 않는다: URL upstream 은 갱신이 받아 둔 캐시 폴더를 그대로 본다. 원본이 설치 세대보다
    앞서 있으면 카드 본문만 다음 세대로 앞서는 부분 흡수가 되므로 렌더하지 않고 거부한다."""
    stored = conf.get("upstream.path", "").strip()
    if not stored:
        raise ConfApplyError([
            "upstream 미등록 — 렌더 원본을 알 수 없습니다. `pm-config upstream set <url|path>` 로 "
            "등록한 뒤 다시 실행하십시오."])
    if upstream_rules.classify_upstream(stored) == "url":
        source_root = upstream_git.upstream_cache_dir(dest_root, stored)
    else:
        source_root = Path(stored).resolve()
    if not source_root.is_dir():
        raise ConfApplyError([
            f"렌더 원본 폴더가 없습니다 ({source_root}) — `pm-config update`(또는 관리자 갱신 탭 "
            "[적용])로 원본을 받은 뒤 다시 실행하십시오."])
    installed = conf.get("upstream.rev", "").strip()
    current = upstream_git.read_upstream_rev(source_root)
    if not installed or current != installed:
        raise ConfApplyError([
            f"렌더 원본({source_root})의 세대 {current or '(알 수 없음)'} 가 설치 세대 `upstream.rev`"
            f"({installed or '없음'})와 다릅니다 — `pm-config update`(또는 관리자 갱신 탭 [적용])로 "
            "설치 세대를 원본에 맞춘 뒤 다시 실행하십시오."])
    return source_root


def _plan_harness_ctx(dest_root: Path, values: dict, opencode_config_dir: Path):
    """사용 한도 묶음 계획 — 검사·모델 한도 조회·계산과 원장 쓰기 가능 확인. 거부는 `ConfApplyError`."""
    try:
        changed, lines, entries = harness_ctx._harness_ctx_plan(dest_root, values, opencode_config_dir)
        if changed:
            harness_ctx._harness_ctx_baseline_writable(dest_root)
    except harness_ctx.HarnessCtxError as exc:
        raise ConfApplyError(exc.violations) from exc
    return values, changed, lines, entries


def _commit_harness_ctx(dest_root: Path, planned) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """사용 한도 묶음 기록 — 하네스 설정 파일·압축 한도 기록. (반영 줄, 바꾼 파일)."""
    values, changed, lines, entries = planned
    try:
        written = harness_ctx._harness_ctx_commit(dest_root, values, changed, entries)
    except harness_ctx.HarnessCtxError as exc:
        raise ConfApplyError(exc.violations, written=exc.written, write_stage=True) from exc
    return lines, written


def _plan_render_outputs(home, dest_root: Path, values: dict):
    """렌더 입력 묶음 계획 — 설치 세대 원본을 확인하고, 제안 값으로 계획한 render change 전부를 메모리에서
    그린다. 계획과 그리기는 같은 실행 Home(`home`)을 쓴다. 렌더 거부는 어느 단계에서 발생해도
    `ConfApplyError`로 전파한다."""
    source_root = _installed_generation_source(dest_root, values)
    try:
        sync_plan = update_plan._resolve_engine_sync_plan(
            home, dest_root, source_root, conf=values)
        render_changes = [change for change in sync_plan["changes"]
                          if getattr(change[2], "render", False)]
        return update_render._render_outputs(home, render_changes, values)
    except render_rules.RenderLeakError as exc:
        raise ConfApplyError([str(exc)]) from exc


def _commit_render_outputs(outputs) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """렌더 입력 묶음 기록 — 다 그린 산출물을 쓴다. (반영 줄 `<relpath> 다시 그림`, 바꾼 파일)."""
    written: list[str] = []
    for output in outputs:
        try:
            update_render._write_render_outputs((output,))
        except OSError as exc:
            raise ConfApplyError([f"{output.relpath} 를 쓰지 못했습니다 ({exc})."],
                                 written=written, write_stage=True) from exc
        written.append(output.relpath)
    return tuple(f"{output.relpath} 다시 그림" for output in outputs), tuple(written)


def _is_reasoning_profile_key(key: str) -> bool:
    """추론 값이 있는 프로필(위임 세트·대체 세트·추가 리뷰어)의 선택 축 키(`.harness`·`.model`·`.reasoning`)인가.

    새 표를 두지 않는다: 레지스트리에서 그 키의 축이 프로필 축(`conf_schema._ROLE_SUFFIXES`)이고 같은 세트의
    `.reasoning` 키 축이 `reasoning` 인지로 판정한다. 하네스·모델만 바꿔도 남은 추론 값이 그 모델 밖이 될 수
    있어 셋 다 건다."""
    spec = conf_schema.key_spec(key)
    if spec is None or spec.choices not in conf_schema._ROLE_SUFFIXES:
        return False
    reasoning = conf_schema.key_spec(f"{key.rsplit('.', 1)[0]}.reasoning")
    return reasoning is not None and reasoning.choices == "reasoning"


def _reasoning_values(conf: Mapping[str, str]) -> list[tuple[str, str, str, str | None]]:
    """값이 있는 추론 키 — (키, 값, 같은 세트의 하네스, 모델). 레지스트리 축이 `reasoning` 인 키(위임 세트·대체
    세트·추가 리뷰어)다. 갱신의 정리(`prune_unavailable_reasoning`)와 저장의 확인(`_plan_per_model_reasoning`)이
    같은 열거를 쓰고, 각자 `PER_MODEL_REASONING_HARNESSES` 하네스만 확인한다. 값이 하나도 없으면 레지스트리를
    읽지 않는다(형제 사본이 덜 깔린 갱신 복구 경로에서도 추론 값이 없으면 지나간다)."""
    filled = [(key, value) for key, value in conf.items() if (value or "").strip()]
    if not filled:
        return []
    found: list[tuple[str, str, str, str | None]] = []
    for key, value in filled:
        spec = conf_schema.key_spec(key)
        if spec is None or spec.choices != "reasoning":
            continue
        profile = key.rsplit(".", 1)[0]
        found.append((key, value, (conf.get(f"{profile}.harness") or "").strip(),
                      (conf.get(f"{profile}.model") or "").strip() or None))
    return found


def _plan_per_model_reasoning(dest_root: Path, values: dict) -> None:
    """추론 값 묶음 계획 — 모델별 허용값 하네스 프로필(주·대체·추가 리뷰어)의 제안 추론 값이 그 모델이 받는
    값인지 카드 유무와 무관하게 확인한다. 밖이거나 판정할 수 없으면(조회 실패·모델 값 없음·목록에 모델
    없음) `ConfApplyError` 다. 저장은 값을 지우지 않는다. 조회는 카드 렌더 확인과 같은 하네스 계약
    모듈(`pm_engine.infra.harness.contract`)을 써서 한 프로세스의 조회 기억을 나눈다."""
    violations: list[str] = []
    for key, value, harness, model in _reasoning_values(values):
        if harness not in contract.PER_MODEL_REASONING_HARNESSES:
            continue
        try:
            allowed = contract.reasoning_allowed(harness, model, cwd=Path(dest_root), env=dict(os.environ))
        except contract.HarnessContractError as exc:
            raise ConfApplyError([
                f"{key}={value} 를 확인할 수 없습니다 — {exc}. {harness} 를 이 프로세스의 PATH 에서 실행할 수 "
                "있게 하거나 그 값을 비운 뒤 다시 실행하십시오."]) from exc
        if contract.normalize_reasoning(value) not in allowed:
            violations.append(
                f"{key}={value} 는 {harness} {model} 이 받는 값({', '.join(allowed) or '없음'}) 밖입니다 — "
                "하네스는 이 값을 오류 없이 버리므로 저장하지 않습니다. 받는 값을 고르거나 비우십시오.")
    if violations:
        raise ConfApplyError(violations)


def _commit_per_model_reasoning() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """추론 값 묶음 기록 — 확인만 하는 묶음이라 파생 파일이 없다(local.conf 기록은 `apply_conf` 몫)."""
    return (), ()


# 반영 단계 종류 — 사용 한도·추론 값·렌더 입력. `apply_conf` 가 종류마다 계획·기록 함수를 명시해 부른다.
_STEP_CTX = "ctx"


_STEP_REASONING = "reasoning"


_STEP_RENDER = "render"


class _ConfApplyStep(NamedTuple):
    """키 묶음 하나의 반영. `key_filter(키)` 는 그 묶음 키 판정, `kind` 는 단계 종류
    (`_STEP_CTX`·`_STEP_REASONING`·`_STEP_RENDER`)다."""
    key_filter: Callable[[str], bool]
    kind: str


# 키 묶음 표 — 이 순서로 계획하고 기록한다. 한 키가 여러 묶음에 걸리면 그 묶음을 모두 돈다(위임 세트
# 키는 추론 값 묶음과 렌더 입력 묶음 둘 다다). 사용 한도 키(`ctx.`·`harness.<harness>.ctx_window_tokens`)는
# 다른 두 묶음과 겹치지 않는다. 추론 값 묶음을 렌더 앞에 두어 무효 값은 렌더 계획 전에 저장 문구로 거부한다.
CONF_APPLY_STEPS: tuple[_ConfApplyStep, ...] = (
    _ConfApplyStep(harness_ctx.is_harness_ctx_input_key, _STEP_CTX),
    _ConfApplyStep(_is_reasoning_profile_key, _STEP_REASONING),
    _ConfApplyStep(lambda key: key in render_input_conf_keys(), _STEP_RENDER),
)


def _plan_step(home, step: _ConfApplyStep, dest_root: Path, values: dict, opencode_config_dir: Path):
    """단계 하나의 쓰기 전 계획 — 거부는 `ConfApplyError`. 모르는 종류는 호출 오류다."""
    if step.kind == _STEP_CTX:
        return _plan_harness_ctx(dest_root, values, opencode_config_dir)
    if step.kind == _STEP_REASONING:
        return _plan_per_model_reasoning(dest_root, values)
    if step.kind == _STEP_RENDER:
        return _plan_render_outputs(home, dest_root, values)
    raise ValueError(f"알 수 없는 설정 반영 단계: {step.kind!r}")


def _commit_step(step: _ConfApplyStep, dest_root: Path, planned) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """단계 하나의 계획 기록 — (반영 줄, 바꾼 파일). 모르는 종류는 호출 오류다."""
    if step.kind == _STEP_CTX:
        return _commit_harness_ctx(dest_root, planned)
    if step.kind == _STEP_REASONING:
        return _commit_per_model_reasoning()
    if step.kind == _STEP_RENDER:
        return _commit_render_outputs(planned)
    raise ValueError(f"알 수 없는 설정 반영 단계: {step.kind!r}")


def apply_conf(home, dest_root: Path, *, updates: Mapping[str, str | None] | None,
               opencode_config_dir: Path) -> ConfApplyResult:
    """local.conf 에 `updates`(None 은 줄 삭제)를 쓰고 파생 파일에 반영한다 — `pm-config apply` 의 본문.

    `updates` 가 없으면 모든 반영 단계를, 있으면 바뀐 키가 걸리는 단계만 돈다. 순서: local.conf 쓰기 락 →
    제안 값(`harness_ctx._conf_values`, 옛 키면 멈춤) → 선택된 단계의 계획 전부 → local.conf 기록 → 단계
    순서대로 기록. 렌더 계획은 실행 Home(`home`)으로 그린다(저장 한 번의 모델 목록 조회는 한 번).
    계획의 거부는 `ConfApplyError`(`write_stage` 거짓)이고 어느 파일도 바꾸지 않는다.
    기록 실패는 `ConfApplyError(write_stage=True, written=바꾼 파일)` 이다. 락 안에서 부르는 함수는 락을
    다시 잡지 않는다(같은 락 파일을 두 번 잡으면 교착)."""
    dest_root = Path(dest_root)
    conf_path = dest_root / _LOCAL_CONF_RELPATH
    with fs.local_conf_write_lock(conf_path):
        values = harness_ctx._conf_values(conf_path, updates)
        steps = [step for step in CONF_APPLY_STEPS
                 if not updates or any(map(step.key_filter, updates))]

        planned = [(step, _plan_step(home, step, dest_root, values, opencode_config_dir))
                   for step in steps]
        changed = install_conf._write_conf_keys_locked(conf_path, dict(updates)) if updates else False
        written: list[str] = [_LOCAL_CONF_RELPATH] if changed else []
        applied: list[str] = []
        for step, step_plan in planned:
            try:
                lines, step_written = _commit_step(step, dest_root, step_plan)
            except ConfApplyError as exc:
                raise ConfApplyError(exc.violations, written=(*written, *exc.written),
                                     write_stage=True) from exc
            applied.extend(lines)
            written.extend(step_written)
    return ConfApplyResult(bool(changed), tuple(applied))


class ReasoningPruneError(RuntimeError):
    """갱신의 추론 값 정리가 판정하지 못했거나(조회 실패·모델 없음) 판정 중 local.conf 가 바뀌어 아무것도
    지우지 않고 멈췄다."""


def prune_unavailable_reasoning(dest_root: Path, conf: Mapping[str, str], *,
                                write: bool) -> dict[str, str]:
    """모델별 허용값 하네스 프로필의 추론 값 중 그 모델이 받지 않는 값을 local.conf 에서 지우고 알린다.

    대상은 레지스트리 축이 `reasoning` 인 키(위임 세트·대체 세트·추가 리뷰어) 중 값이 있고 같은 세트의
    하네스가 `contract.PER_MODEL_REASONING_HARNESSES`(opencode)인 것이다. 정적 허용값(claude·codex)은
    대상이 아니다 — 엔진이 정한 값이라 바뀌지 않고, 틀리면 렌더가 멈춘다. 판정은 설치 폴더·이 프로세스
    env 의 모델 목록 조회다(`contract.reasoning_allowed`). 하나라도 판정하지 못하면(조회 실패·모델 값
    없음·목록에 모델 없음) 아무것도 지우지 않고 `ReasoningPruneError` 다.

    쓰기는 판정 뒤 local.conf 쓰기 락 안에서 다시 읽어 값이 판정 때와 같을 때만 한 번에 지운다(다르면
    `ReasoningPruneError`). `write=False` 면 출력만 하고 쓰지 않는다. 반환은 지운(지울) 키를 뺀 conf 다 —
    호출부가 계획·렌더에 그대로 넘긴다. 갱신의 전량 동기에서 계획보다 먼저 돈다(렌더가 무효 값을
    만나기 전에 정리한다)."""
    reasoning_values = _reasoning_values(conf)
    if not reasoning_values:
        return dict(conf)
    misses: list[tuple[str, str, str, str | None, tuple[str, ...]]] = []
    for key, value, harness, model in reasoning_values:
        if harness not in contract.PER_MODEL_REASONING_HARNESSES:
            continue
        try:
            allowed = contract.reasoning_allowed(
                harness, model, cwd=Path(dest_root), env=dict(os.environ))
        except contract.HarnessContractError as exc:
            raise ReasoningPruneError(
                f"conf 이관: {key}={value} 를 판정할 수 없어 지우지 않았다 — {exc}. opencode 를 이 "
                "프로세스의 PATH 에서 실행할 수 있게 한 뒤 다시 실행하거나 local.conf 의 그 줄을 직접 "
                "고친다.") from exc
        if contract.normalize_reasoning(value) not in allowed:
            misses.append((key, value, harness, model, allowed))
    if not misses:
        return dict(conf)
    if write:
        conf_path = Path(dest_root) / _LOCAL_CONF_RELPATH
        with fs.local_conf_write_lock(conf_path):
            current = install_conf._read_local_conf(conf_path)
            if any(current.get(key) != value for key, value, *_ in misses):
                raise ReasoningPruneError("local.conf 가 판정 중에 바뀌었다 — 다시 실행한다")
            install_conf._write_conf_keys_locked(
                conf_path, {key: None for key, *_ in misses})
    lead = (f"  ✓ local.conf 추론 값 {len(misses)}개 지움" if write
            else f"[dry-run] local.conf 추론 값 {len(misses)}개 지울 예정")
    print(f"{lead} — 그 모델이 받지 않는 variant 라 갱신이 지운다(OpenCode 는 이 값을 오류 없이 "
          "버린다):")
    for key, value, harness, model, allowed in misses:
        print(f"    {key}={value} ({harness} {model} · 받는 값: "
              f"{', '.join(allowed) if allowed else '없음'})")
    removed = {key for key, *_ in misses}
    return {key: value for key, value in conf.items() if key not in removed}
