"""추가 리뷰 범위 해소 (층: app)."""
from __future__ import annotations

from pathlib import Path
from typing import Sequence
from pm_engine.infra import lease_store
from pm_engine.infra import pm_paths
from pm_engine.infra import review_diff


def _ticket_worktree_candidates(
    home, touches: Sequence[str], candidates: Sequence[Path],
) -> tuple[Path, ...]:
    """PM-home 좌표 touches가 실제로 가리키는 worktree 후보만 고른다."""
    pm_home = home.local_dir.parent.parent
    selected: set[Path] = set()
    for raw in touches:
        path = Path(raw)
        absolute = path.resolve() if path.is_absolute() else (pm_home / path).resolve()
        for candidate in candidates:
            try:
                absolute.relative_to(candidate)
            except ValueError:
                continue
            selected.add(candidate)
    return tuple(sorted(selected))


def _candidate_has_diff(root: Path, base: str, paths: Sequence[str]) -> bool:
    """등록 슬롯 후보가 **한 diff 단계**에서 변경을 갖는지 read-only 판정.

    단계 문자열과 실행은 추출(`extract_diff`)과 같은 코드 경로(`_stage_diff_text`)를 타므로 폭
    해석이 두 벌로 갈리지 않는다. 어느 단계를 근거로 삼을지는 호출부(`_resolve_diff_root`)가
    `_slot_selection_bases` 로 정한다. git 실행 자체가 불가능한 후보는 변경 없음으로 본다
    (fail-soft·선택 대상 밖).
    """
    try:
        return bool(review_diff._stage_diff_text(root, base, paths).strip())
    except OSError:
        return False


def _resolve_diff_root(
    home, engine_repo: Path,
    *,
    paths: Sequence[str],
    base: str,
    ticket_selected: bool,
) -> Path:
    """명시 paths/touches와 엔진-홈 관계에서 diff worktree를 유일하게 파생한다."""
    pm_home = home.local_dir.parent.parent
    absolute_roots = {
        root
        for raw in paths
        if Path(raw).is_absolute()
        and (root := pm_paths._path_repo_root(Path(raw).resolve())) is not None
    }
    if len(absolute_roots) > 1:
        raise pm_paths.AnchorResolutionError(
            "--paths가 여러 git repo를 가리킵니다 — 추가 리뷰 1회는 diff 앵커 하나만 허용합니다."
        )
    if absolute_roots:
        selected_root = next(iter(absolute_roots)).resolve()
        # 비-ticket 절대 경로는 이미 repo 하나를 확정했다. 복구 채널이 손상된 PM-home
        # lease 장부 때문에 자기잠김하지 않도록 장부를 읽기 전에 반환한다.
        if not ticket_selected:
            return selected_root
        candidates = lease_store._registered_worktrees(pm_home)
        if (
            ticket_selected
            and engine_repo.resolve() != pm_home.resolve()
            and selected_root in candidates
            and selected_root != engine_repo.resolve()
        ):
            raise pm_paths.AnchorResolutionError(
                "ticket touches가 실행 엔진 worktree와 다른 등록 worktree를 가리킵니다: "
                f"engine={engine_repo.resolve()} touches={selected_root}"
            )
        return selected_root

    candidates = lease_store._registered_worktrees(pm_home)

    if ticket_selected and candidates:
        selected = _ticket_worktree_candidates(home, paths, candidates)
        if len(selected) == 1:
            if (
                engine_repo.resolve() != pm_home.resolve()
                and selected[0] != engine_repo.resolve()
            ):
                raise pm_paths.AnchorResolutionError(
                    "ticket touches가 실행 엔진 worktree와 다른 등록 worktree를 가리킵니다: "
                    f"engine={engine_repo.resolve()} touches={selected[0]}"
                )
            return selected[0]
        if len(selected) > 1:
            raise pm_paths.AnchorResolutionError(
                "ticket touches가 여러 등록 worktree를 가리킵니다 — diff 앵커가 모호합니다."
            )

    if engine_repo.resolve() != pm_home.resolve():
        return engine_repo.resolve()

    if not candidates:
        # 아직 슬롯 행이 없는 repo에는 lease 장부가 없다. 이 경우 명시 paths의 유일한 repo는
        # 엔진 자기 repo이며, PM-home 다중 슬롯 오해소와 구분된다.
        return engine_repo.resolve()
    # 슬롯 소유 근거가 되는 단계(`_slot_selection_bases`)만 순서대로 본다 — 암묵 폴백 단계는
    # 놀고 있는 슬롯까지 후보로 만들기 때문에 여기서 제외된다. 선택된 슬롯에서 추출이 어느
    # 단계를 쓰든, 슬롯을 *고른* 근거는 항상 사용자가 지정한 base 자신이다.
    changed: tuple[Path, ...] = ()
    for stage_base in review_diff._slot_selection_bases(base):
        changed = tuple(
            candidate for candidate in candidates
            if _candidate_has_diff(candidate, stage_base, paths)
        )
        if changed:
            break
    if len(changed) == 1:
        return changed[0]
    if len(candidates) == 1:
        return candidates[0]
    detail = ", ".join(str(path) for path in (changed or candidates)) or "(후보 0)"
    raise pm_paths.AnchorResolutionError(
        "명시 경로에서 diff worktree를 하나로 해소할 수 없습니다. "
        f"ticket touches에 등록 슬롯 경로를 포함하거나 절대 --paths를 사용하세요: {detail}\n"
        "  · 변경을 이미 커밋했다면 `--base <공통 base ref>`로 앵커를 명시하세요 — 직전 커밋 한 "
        "칸(HEAD~1..HEAD)은 아무 것도 안 한 슬롯도 공유 base 의 마지막 커밋으로 걸리므로 슬롯 "
        "소유 근거로 쓰지 않습니다."
    )


def _normalize_review_paths(
    home, paths: Sequence[str], *, diff_root: Path, ticket_selected: bool,
) -> tuple[str, ...]:
    """PM-home/절대 좌표를 선택된 diff repo의 pathspec으로 변환한다."""
    pm_home = home.local_dir.parent.parent
    normalized: list[str] = []
    for raw in paths:
        path = Path(raw)
        if path.is_absolute():
            absolute = path.resolve()
            try:
                value = absolute.relative_to(diff_root).as_posix()
            except ValueError as exc:
                raise pm_paths.AnchorResolutionError(
                    f"검토 경로가 해소된 diff worktree 밖입니다: {raw} (diff={diff_root})"
                ) from exc
        elif ticket_selected:
            absolute = (pm_home / path).resolve()
            try:
                value = absolute.relative_to(diff_root).as_posix()
            except ValueError:
                # legacy ticket은 이미 repo-relative touches를 저장한다.
                value = path.as_posix()
        else:
            value = path.as_posix()
        if value:
            normalized.append(value)
    if not normalized:
        raise pm_paths.AnchorResolutionError("검토 경로가 0개로 해소됐습니다.")
    return tuple(dict.fromkeys(normalized))
