"""부트스트랩 슬롯 결함과 보호 훅 정합화."""
from __future__ import annotations
from pm_engine.app import worktree_status
from pm_engine.infra import pm_paths
from pm_engine.infra import worktree_git
from pm_engine.infra import areas_store
from pm_engine.infra import fs
from pm_engine.infra import slot_snapshot
from pathlib import Path
from pm_engine.app import repo_protection
import sys
from pm_engine.infra import protected_hooks
from pm_engine.infra import branch_store
from pm_engine.domain import bootstrap_rules


def _phase0_slot_folder_exists(home, slot_id: str) -> bool:
    """슬롯 worktree 폴더가 실재하는가 (`slot_path(slot).exists()`).

    **없는 것**(False)과 **못 읽는 것**(경로 해소 실패)을 섞지 않는다 — 해소 실패는 판정
    불능이라 그대로 올린다(미실재로 위장하면 생성 안내가 엉뚱한 처방이 된다).
    """
    return pm_paths.slot_path(home, slot_id).exists()


def _phase0_main_reference_reason(home, repo: str, slot_id: str) -> str | None:
    """슬롯이 main-참조 상태인지 판정 — 사유 문구(거부용) or None(통과) (두 축).

    두 축(어느 하나라도 해당 시 거부) — `main`+`origin/main` 슬롯이다:
      1. **보호브랜치 직접 checkout** — 슬롯 HEAD 브랜치가 보호목록(main 등)이면. `_protected_warning`
         재사용(보호목록=`areas_store._repo_protected`).
      2. **보호브랜치 원격 origin-추적** — `@{upstream}` 이 보호 브랜치 원격(`origin/main` 등)을 가리키면.
         ⚠️ *임의* upstream 이 아니다 — 정상 작업 슬롯은 자기 feature 브랜치(`origin/a5` 등)를 추적하는
         게 정상(미해소=경고)이라 그건 통과시킨다. 보호브랜치 원격을 추적할 때만 main-참조.
    보호브랜치 직접 checkout 이 우선(더 구체적 사유). detached(브랜치 None·upstream 없음·readonly
    등)·풀 미지원은 None(오탐 0). 조회 예외는 전파한다."""
    branch = worktree_git.current_branch(home, slot_id)
    protected = _protected_warning(home, repo, branch)
    if protected is not None:
        return f"보호 브랜치 `{protected}` 를 직접 체크아웃한 상태입니다"
    upstream = _phase0_protected_upstream(home, repo, slot_id)
    if upstream is not None:
        return f"보호 브랜치 원격(`{upstream}`)을 origin-추적하는 상태입니다"
    return None


def _phase0_protected_upstream(home, repo: str, slot_id: str) -> str | None:
    """슬롯 `@{upstream}` 이 **보호 브랜치 원격**(`origin/main` 등)을 추적하면 그 upstream 이름, 아니면 None.

    판정 로직 재구현 금지 — worktree_pool 의 `slot_status`(`_upstream_status` 흡수)를 호출만
    한다. upstream `<remote>/<branch>` 의 branch 부분(첫 `/` 이후·`feature/x` 도 보존)이 보호목록
    (`_protected_warning`)이면 main-참조로 판정한다. **자기 feature 브랜치 추적(`origin/a5`)은 정상
    작업 슬롯이라 제외**(오탐 0 — concern 은 `origin/main` 추적). slot_status 미지원(구 풀)·None·
    미해소 upstream·`/` 없는 이름은 None(보수 판정). 조회 예외는 전파한다."""
    status = worktree_status.slot_status(home, slot_id)
    if status is None or not getattr(status, "upstream_ok", False):
        return None
    upstream = getattr(status, "upstream", None)
    if not upstream or "/" not in upstream:
        return None
    tracked_branch = upstream.split("/", 1)[1]
    return (
        upstream if _protected_warning(home, repo, tracked_branch) is not None else None
    )


def _remedy_branch_name(
    home, repo: str, slot_id: str, preferred: str, *, run_git_fn
) -> str | None:
    """main-참조 해소 커맨드가 제시할 **안전한 신규 브랜치명** (소비처 2곳 공용).

    해소 커맨드가 실제로 실행 가능한 값만 돌려준다 — 세션/task 명을 그대로 보간하면 그 이름이
    보호브랜치이거나 이미 존재하는 브랜치일 때 자기모순 안내가 된다.
    규칙(순서·`_REMEDY_BRANCH_PREFIX`·`_REMEDY_BRANCH_SUFFIX_LIMIT`) — 후보 필터 3종:
      1. `preferred` 가 보호목록(`areas_store._repo_protected`·`_protected_warning` 축) 밖이고,
         **git 브랜치명으로 유효**(`_remedy_branch_ref_ok` — 실행 쪽 `create_task_branch` 와 같은 판정)하며,
         그 슬롯에 **미존재** 브랜치면 그대로.
      2. 아니면 `task/<preferred>`(세 검사 재적용).
      3. 그것도 충돌하면 `task/<preferred>-2`, `-3` … 첫 미충돌.
    전 후보 탈락(상한 도달·전부 무효)이면 **None**
    후보(정의상 충돌하는 이름)를 그대로 제시해 안내가 *다시* 실행 불가가 됐다. 이름을 억지로
    짜내는 대신 호출부가 "브랜치명을 직접 지정하라" 분기로 안내한다(`_remedy_branch_command`).

    ⚠ **ref-format 검사가 후보 생성에도 있어야 한다**(codex 게이트 must-fix): task 명
    검증(`identity_args.validate_task_name`)은 `fix:bug`·`fix~1`·`a.lock` 을 통과시키지만
    `git check-ref-format --branch` 는 거부한다 — 제안 쪽에 이 검사가 없으면 remedy 가
    `task branch … fix:bug` 를 안내하고 실행 쪽이 `invalid-ref` 로 튕겨 "실행 가능한 단일 remedy"
    접두 후보도 같은 검사를 탄다
    (`task/fix:bug` 도 여전히 무효).
    **판정만 한다** — 엔진이 브랜치를 만들지 않는다."""
    prefixed = f"{bootstrap_rules._REMEDY_BRANCH_PREFIX}{preferred}"
    candidates = [preferred, prefixed]
    candidates += [
        f"{prefixed}-{n}"
        for n in range(2, bootstrap_rules._REMEDY_BRANCH_SUFFIX_LIMIT + 1)
    ]
    for cand in candidates:
        if _protected_warning(home, repo, cand) is not None:
            continue
        if not _remedy_branch_ref_ok(home, slot_id, cand, run_git_fn=run_git_fn):
            continue
        if _slot_branch_exists(home, slot_id, cand, run_git_fn=run_git_fn):
            continue
        return cand
    return None


def _remedy_branch_ref_ok(home, slot_id: str, branch: str, *, run_git_fn) -> bool:
    """후보가 **`create_task_branch` 가 받아들일** 브랜치명인가 — worktree_pool 판정 재사용 (규칙 중복 0).

    제안 쪽(여기)과 실행 쪽(`task_lifecycle.create_task_branch`)의 수용 규칙이 갈리면 안내가 곧바로 실행 불가가
    된다(codex 게이트 must-fix — 같은 검사의 절반 적용). 그래서 **판정을 다시 구현하지 않고**
    worktree_pool 의 `_normalize_branch_name`(그 규칙의 단일 진실 — `git check-ref-format --branch`
    + 빈/다중줄/공백 방어 + 고정점 확인)을 **슬롯 worktree 바인딩 러너**로 호출한다. 주입/동적로드된
    풀 모듈을 쓰는 기존 seam(`_slot_branch_exists`·`_phase0_protected_upstream` 동형·직접 import 금지).

    통과 조건은 `normalize(cand) == cand` — 정규화 결과가 원문과 다르면(revspec 확장·`@{-1}`)
    제안 이름이 다른 브랜치로 해소되어 안내가 거짓말이 되므로 후보에서 배제한다.

    경로 해소 예외·git 호출 예외는 전파한다. (rc≠0 로 실패하는 git 은 무효와 구분 불가 — 그
    경우 후보가 소진돼 "브랜치명 직접 지정" 안내로 수렴한다. 여전히 실행 가능한 안내다.)"""
    slot_dir = str(pm_paths.slot_path(home, slot_id))

    def runner(argv):
        """슬롯 worktree 바인딩 git 러너 — DI `_run_git_fn` 에 `-C <slot_dir>` 를 붙인다."""
        return run_git_fn(["-C", slot_dir, *argv])

    return branch_store._normalize_branch_name(branch, git_runner=runner) == branch


def _remedy_branch_command(
    home, repo: str, slot_id: str, owner: str, *, run_git_fn
) -> str:
    """main-참조 해소 커맨드 문자열 — 엔진이 브랜치를 만들고 부모를 기록하는 단일 커맨드 (소비처 2곳 공용).

    raw `git switch -c <b>` 는 부모 기록과 장부 스냅을 남기지 않는다. `pm_config.py task branch` 는
    부모 브랜치 최신 커밋에서 브랜치를 만들고 부모·갈라진 커밋·도착 스냅을 함께 기록한다. 부모는
    repo 등록 base(areas.md)이고, 등록에 base 가 없으면 `<부모-브랜치>` 자리로 둔다.

    브랜치명은 `_remedy_branch_name`이 산출한다(규칙 중복 구현 0). 후보 소진(None)이면
    이름을 제안하지 않고 **직접 지정**을 안내한다 — 상한 후보는 정의상 충돌하므로 그대로 실으면
    실행 불가 안내를 재생산한다.

    복합 문자열(`git … && python3 …`)은 쓰지 않는다 — **PowerShell 5.x 가 `&&` 미지원**
    (Windows 채택자에서 깨진다·실측)."""
    branch = _remedy_branch_name(home, repo, slot_id, owner, run_git_fn=run_git_fn)
    base = areas_store._repo_base(home, repo)
    cmd = f"{bootstrap_rules._CARD_TOOL_INVOKE}/pm_config.py task branch {owner} {slot_id}"
    tail = f" --from {base or '<부모-브랜치>'}"
    if branch is None:
        return f"{cmd} <새-브랜치명>{tail}  (제안 후보 소진 — `{owner}`·`{bootstrap_rules._REMEDY_BRANCH_PREFIX}{owner}`·`-2`…`-{bootstrap_rules._REMEDY_BRANCH_SUFFIX_LIMIT}` 가 모두 보호목록/기존 브랜치와 충돌하거나 git 브랜치명으로 부적합합니다. 브랜치명을 직접 지정하세요.)"
    return f"{cmd} {branch}{tail}"


def _slot_branch_exists(home, slot_id: str, branch: str, *, run_git_fn) -> bool:
    """그 슬롯 worktree 에 로컬 브랜치 `branch` 가 이미 있는가 (remedy 후보 충돌 검사).

    `git -C <slot_dir> show-ref --verify --quiet refs/heads/<branch>`(argv-list·shell 미경유)를
    DI 러너(`_run_git_fn`)로 돌린다 — rc 0 = 존재, rc≠0 = 미존재. 경로 해소·git 호출 예외는
    전파한다."""
    slot_dir = str(pm_paths.slot_path(home, slot_id))
    rc, _out = run_git_fn(
        ["-C", slot_dir, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"]
    )
    return rc == 0


def _task_slot_fault(
    home, lease, task: str, *, run_git_fn
) -> "tuple[str, str, str] | None":
    """task 보유 슬롯 1개의 0단계 fault → (label, 근거, 해소커맨드) 또는 None(통과).

    기존 0단계 프리미티브를 per-slot 재사용한다(판정 재구현 0) — 각 검사는 **print 없이** 사유+
    해소만 돌려줘 caller 가 전 fault 를 일괄 표시하게 한다(순차 발견 금지). 검사 순서(우선):
      1. **stale** (장부 有·worktree 폴더 無)
         `slots_for_task` 로 이미 장부엔 있으므로 폴더 부재만 보면 stale(해소=prune-stale).
      2. **불완전 생성** (state=creating).
      3. **main-참조** (보호브랜치 직접 checkout / origin-추적·`_phase0_main_reference_reason`).
      4. **기록↔live diverged** (`compare_slot_git` fail_loud·`_phase0_diverge_reason` 근거).
    task 자기 명의 슬롯이라 타-점유는 자연 무해(session==task)라 대상 아님(생략)."""
    slot_id = getattr(lease, "slot", None)
    if not slot_id:
        return None
    repo = getattr(lease, "repo", None) or slot_id.rpartition("/")[2].rsplit("_", 1)[0]
    if not _phase0_slot_folder_exists(home, slot_id):
        return (
            "stale (장부에 lease 존재·worktree 폴더 부재)",
            "장부엔 이 슬롯의 task 명의 lease 가 있으나 worktree 디렉터리가 없습니다 — 외부 삭제/미생성(클래스·엉뚱한 트리 vacuous 진입 방지).",
            f"{bootstrap_rules._CARD_TOOL_INVOKE}/pm_config.py worktree prune-stale",
        )
    if bootstrap_rules._phase0_incomplete_create(lease):
        return (
            "불완전 생성 (state=creating)",
            "생성 중/중단된 슬롯이라 안전히 진입할 수 없습니다.",
            f"{bootstrap_rules._CARD_TOOL_INVOKE}/pm_config.py worktree status",
        )
    reason = _phase0_main_reference_reason(home, repo, slot_id)
    if reason is not None:
        return (
            "main-참조 (보호브랜치 직접 checkout / origin-추적)",
            f"{reason} — 이 슬롯 커밋이 canonical/보호 브랜치로 새면 공유 이력이 오염된다.",
            _remedy_branch_command(home, repo, slot_id, task, run_git_fn=run_git_fn),
        )
    result = slot_snapshot.compare_slot_git(home, slot_id)
    if result is not None and getattr(result, "fail_loud", False):
        return (
            "기록↔live diverged (외부 개입 가능성)",
            bootstrap_rules._phase0_diverge_reason(result),
            f"{bootstrap_rules._CARD_TOOL_INVOKE}/worktree_pool.py record {slot_id}",
        )
    return None


def _task_slot_row(home, lease, slot_id: str) -> dict:
    """슬롯 1개의 열거 행 — branch/head/dirty(`slot_git_status`) + 기록↔live(`compare_slot_git`)."""
    repo = getattr(lease, "repo", None) or slot_id.rpartition("/")[2].rsplit("_", 1)[0]
    status = _task_slot_git_status(home, slot_id)
    head = status.get("head")
    head_disp = head[:12] if isinstance(head, str) and head else "(미상)"
    return {
        "slot": slot_id,
        "repo": repo,
        "branch": status.get("branch") or "(detached)",
        "head": head_disp,
        "dirty": bool(status.get("dirty")),
        "record_live": _task_slot_record_live(home, slot_id),
    }


def _task_slot_git_status(home, slot_id: str) -> dict:
    """`slot_git_status(slot)`(branch·head·dirty) 조회 — 결과 없음은 {}, 조회 예외는 전파."""
    return worktree_status.slot_git_status(home, slot_id) or {}


def _task_slot_record_live(home, slot_id: str) -> str:
    """기록↔live 상태 문자열 — `compare_slot_git` 소비 (ok|unrecorded|diverged|unknown). 조회 예외는 전파."""
    result = slot_snapshot.compare_slot_git(home, slot_id)
    if result is None:
        return "unknown"
    if getattr(result, "unrecorded", False):
        return "unrecorded"
    if getattr(result, "fail_loud", False):
        return "diverged"
    return "ok"


def _protected_warning(home, repo: str, branch: str | None) -> str | None:
    """등록부 보호목록에 포함된 live 브랜치의 소프트 경고.

    areas_store로 대상 Home의 보호목록을 직접 읽는다. 브랜치 미해소는 경고를 생략하고 판독 예외는 전파한다."""
    if not branch:
        return None
    protected = areas_store._repo_protected(home, repo)
    return branch if branch in protected else None


def _reconcile_protected_sidecar(home, repo: str) -> bool:
    """훅 sidecar의 보호목록 **또는 증거 계약**이 현재 해소값과 다르면 재설치한다.

    훅이 실제로 읽는 건 areas.md 가 아니라 설치 시점에 복사된 sidecar
    (`.local/repo-hooks/<repo>/protected`)다 — *다른 clone/사용자*가 목록을 바꾸면 이 clone 의
    훅은 옛 값으로 계속 동작한다(silent). 세션 시작(그 세션의 첫 커밋보다 앞)에 그 드리프트를
    흡수한다.

    **비교 우선**: sidecar 를 읽어 resolve 된 목록과 같고 **훅이 배선돼 있으면** 아무것도 하지
    않는다(subprocess 0 — 정합이 정상 상태이므로 매 부트스트랩이 git config 를 때리지 않는다).
    다를 때만 `repo_protection._install_protected_hook(home, repo)` 깔때기를 부른다. 비교 단계에서도
    현재 upstream provenance의 gate mode/test command를 매번 재해소해, 한 원자
    `gate-contract` sidecar 실값과 **둘 다** 대조한다. 이 축이 없으면 areas
    `test_cmd`만 바뀐 뒤 보호목록이 같을 때 stale command가 다음 pm_update까지
    살아남는다. drift에서는 훅 본문·sidecar·bare `core.hooksPath`를 멱등 재설치한다.
    훅 *본문* 신버전 배포는 `pm_update` 축 소유라
    여기선 드리프트만 본다.

    **배선 축을 함께 본다(should-fix)**: `install_protected_hook` 은 sidecar 기록 뒤 bare
    `core.hooksPath` 배선을 하는데, 뒤가 실패하면 **sidecar 는 최신인데 훅은 안 걸린** 상태가
    된다. 내용만 비교하면 이 상태에서 영구 침묵한다(보호가 꺼진 채). 판정은 `protected_hooks.protected_hook_wired`(읽기 전용)를 쓰고, `None`("모름")은
    드리프트로 치지 않는다(오탐 0).

    보호목록은 대상 Home의 areas_store로 읽고 보호 패키지를 직접 호출한다.
    판독·비교·재설치 예외는 전파한다.

    **재설치 False 는 조용하지 않다(must-fix)**: drift 를 *발견한 뒤* 재설치가 실패하면
    (`install_protected_hook` 은 bare 부재·`core.hooksPath` 설정 실패 시 **예외 없이 False**),
    sidecar 는 여전히 stale 인데 "정합화했습니다" 를 내면 이 티켓이 닫으려던 값-연결 끊김을
    *정합하다고 거짓 보고*하는 것이 된다(사용자가 확인할 이유를 없앤다). 그래서 반환 False 면
    **실패 안내 + 재실행 커맨드**를 loud 하게 내고 False 를 돌려준다. 진입은 막지 않는다(보호
    훅은 추가 가드).

    True = 재설치를 실제로 수행하고 **성공**함.
    """
    hooks_dir = pm_paths.repo_hooks_dir(home)
    contract_name = protected_hooks.PROTECTED_GATE_CONTRACT_NAME
    protected = list(areas_store._repo_protected(home, repo))
    sidecar = Path(hooks_dir) / repo / "protected"
    if not sidecar.is_file():
        return False
    current = [
        line.strip()
        for line in fs.read_text_shared(sidecar, encoding="utf-8").splitlines()
        if line.strip()
    ]
    protected_ok = current == protected
    gate_mode, test_cmd = repo_protection._protected_push_gate_config(
        home, repo, report_downgrade=False
    )
    expected_contract = f"{gate_mode}\n{test_cmd}\n"
    contract_sidecar = Path(hooks_dir) / repo / str(contract_name)
    current_contract = (
        fs.read_text_shared(contract_sidecar, encoding="utf-8")
        if contract_sidecar.is_file()
        else None
    )
    contract_ok = current_contract == expected_contract
    wired = _protected_hook_wired(home, repo)
    if protected_ok and contract_ok and (wired is not False):
        return False
    drift_axes = []
    if not protected_ok:
        drift_axes.append("보호 브랜치 목록")
    if not contract_ok:
        drift_axes.append("증거 계약(gate mode/test command)")
    if wired is False:
        drift_axes.append("bare `core.hooksPath` 배선")
    cause = "·".join(drift_axes) + "이 현재 해소값과 달라"
    stale_parts = []
    if not protected_ok:
        stale_parts.append(f"옛 목록({', '.join(current) or '(빈 목록)'})")
    if not contract_ok:
        stale_parts.append("옛/미설치 증거 계약")
    if wired is False:
        stale_parts.append("미배선(훅 미발화)")
    stale_state = "·".join(stale_parts) + " 상태로 동작"
    if wired is False:
        enforcement_state = f"현재 목록({', '.join(protected)})은 강제되지 않습니다"
    elif not protected_ok:
        enforcement_state = (
            f"현재 목록({', '.join(protected)}) 대신 옛 목록이 강제됩니다"
        )
    else:
        enforcement_state = (
            "보호목록은 강제 중이지만 옛/미설치 증거 계약으로 동작합니다"
        )
    installed = bool(repo_protection._install_protected_hook(home, repo))
    if not installed:
        reason = repo_protection.protected_hook_install_failure_reason(home, repo)
        reason = reason or "install_protected_hook이 원인 미해소 실패를 반환"
        print(
            f"[경고·0단계] {repo} {cause} 이 clone 의 훅을 **정합화하지 못했습니다** — {reason}. 훅은 아직 {stale_state} 이며, {enforcement_state}.\n  → 재실행:  {_protected_retry_command(home, repo)}   (멱등·bare 부재면 `repo add {repo}` 먼저)",
            file=sys.stderr,
        )
        return False
    print(
        f"[알림·0단계] {repo} {cause} 이 clone 의 훅을 정합화했습니다 — 보호목록={', '.join(protected)}, 계약={gate_mode}.",
        file=sys.stderr,
    )
    return True


def _protected_retry_command(home, repo: str) -> str:
    """대상 Home의 보호목록 상태에 따른 재실행 명령."""
    return repo_protection.protected_retry_command(home, repo)


def _protected_hook_wired(home, repo: str) -> bool | None:
    """대상 Home의 bare hooksPath를 보호 훅 모듈로 조회한다."""
    return protected_hooks.protected_hook_wired(home, repo)
