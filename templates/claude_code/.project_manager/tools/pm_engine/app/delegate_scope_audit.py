"""위임 전후 범위 감시 (층: app)."""
from __future__ import annotations

from typing import NamedTuple
from pathlib import Path
from pathlib import PurePosixPath
from collections.abc import Sequence
from pm_engine.domain import delegate_run_rules
from pm_engine.infra import delegate_scope_git
from pm_engine.domain import delegate_scope_rules
from pm_engine.infra import pm_paths
from pm_engine.infra import repo_coordinates
import sys
from pm_engine.domain import ticket_rules
from pm_engine.infra import ticket_store



# ── 병렬 위임 touches 교집합 (같은 세션 claimed ticket·never-block) ─────────
# 같은 트리에서 dev 를 동시에 띄우면 touches 가 겹치는 두 위임이 서로의 WIP 를 덮는다. PM 이
# 손으로 하던 disjoint 확인을 계산으로 옮긴다 — 차단하지 않고 표면화한다.
#
# 좌표계는 **이 기능 전체에 하나**다 — 사전(교집합)·사후(겹친 파일 변경) 두 축 모두 board 선언을
# `_folded_touch_pairs` 로 이 workspace 좌표(repo-relative)까지 접은 뒤 비교한다. 실 보드에는 같은
# 파일이 slot 접두형(`work/<repo>_<N>/x`)과 무접두형(`x`) 두 표기로 공존하므로, 표기 정규화만 하고
# 비교하면 접두/무접두 혼합 선언에서 교집합이 조용히 0이 된다.

_TOUCH_OVERLAP_HEADER = "=== ⚠ 병렬 위임 touches 겹침 ==="
_TOUCH_OVERLAP_PRESCRIPTION = (
    "  · 같은 트리 동시 편집은 WIP 를 덮을 수 있다 — 차단하지 않으며 PM 이 판정한다.\n"
    "  · 처방: 순차 실행 또는 `pm-config worktree add <repo>` 로 슬롯 분리"
)
# 진단 목록 표시 상한 — 넘치면 잔여 건수를 병기한다.
_TOUCH_OVERLAP_DISPLAY_LIMIT = 8


class TicketTouchOverlap(NamedTuple):
    """같은 세션이 claim 중인 다른 ticket 하나와의 touches 교집합."""

    ticket: str                      # 상대 ticket id
    claimed_by: str                  # 두 ticket 이 공유하는 claim 주체(= "같은 세션" 근거)
    declarations: tuple[str, ...]    # 상대 ticket 의 **원 선언**(board 에서 그대로 grep 되는 표기)
    paths: tuple[str, ...]           # 접힌 좌표의 **좁은 쪽** 공유 경로(디렉토리∩파일=파일·트리 기준)


def _folded_touch_pairs(
    items: Sequence[str], *, pm_root: Path | str, workspace: Path | str,
) -> tuple[tuple[str, str], ...]:
    """touches 를 `(원 선언, 이 workspace 좌표)` 쌍으로 접는다 — **사전·사후 공용 좌표 규칙**.

    검증된 slot 접두만 벗기는 규칙은 repo_coordinates 단일 진실이다(`delegate_scope_git.allowed_paths`
    가 쓰는 것과 같은 primitive). 이 workspace 로 해소되지 않는 선언(다른 슬롯·traversal)은 항목
    단위로 드롭한다 — 여기서는 정상 입력이라 기본은 조용하다(선언 축의 드롭은 `_warn_dropped_touch`
    가 이미 알린다). 정규화 예외 타입은 좁게 잡는다: 처리하지 않는 예외는 그대로 전파한다.
    """
    pairs: list[tuple[str, str]] = []
    for item in items:
        try:
            normalized = repo_coordinates.normalize_repo_paths(
                [item], pm_root=Path(pm_root), workspace=Path(workspace),
            )
        except (ValueError, OSError) as exc:   # RepoCoordinateError(ValueError) = 타 슬롯/미실재
            continue
        for path in normalized:
            value = ticket_rules._touch_notation(repo_coordinates.canonicalize_path_notation(str(path)))
            if value is not None and value != ".":
                pairs.append((item, value))
    return tuple(dict.fromkeys(pairs))


def _touch_notations(
    raw: object, *, pm_root: Path | str, workspace: Path | str,
) -> tuple[tuple[str, str], ...] | None:
    """frontmatter touches → 접힌 `(원 선언, 좌표)` 쌍. **형식 불명이면 None**(판정 보류).

    형식 판정은 단일 진실(`ticket_rules._normalized_touches`)을 쓴다 — 스칼라/비문자열 원소를
    여기서 다시 해석하면 같은 frontmatter 를 두 문법으로 읽게 된다.
    """
    items = ticket_rules._normalized_touches(raw)
    if items is None:
        return None
    return _folded_touch_pairs(
        items, pm_root=pm_root, workspace=workspace,
    )


def _narrower_touch_path(left: str, right: str) -> str:
    """겹치는 두 선언이 실제로 공유하는 경로 — 접두(디렉토리)가 아닌 좁은 쪽."""
    return right if right.startswith(left + "/") else left


def claimed_touch_overlaps(
    ticket: str, *, pm_root: Path | str, workspace: Path | str,
) -> tuple[TicketTouchOverlap, ...]:
    """대상 ticket 과 **같은 세션**이 claim 중인 다른 ticket 들의 touches 교집합.

    "같은 세션" 은 `claimed_by` 값 일치다(`<user>/<task>` 또는 슬롯-only 둘 다 그 값 그대로
    비교한다 — 다른 사용자/다른 task 의 동시 claim 은 이 트리를 공유하지 않는다). 대상 ticket
    자신과 `claimed_by` 가 없는 ticket 은 제외한다.

    비교 좌표는 `workspace`(= 이 위임의 git 최상위) 기준으로 접은 값이라 접두/무접두 혼합 선언도
    같은 파일로 만난다. board 형상 둘(board-git 분리 `.project_manager/board/tickets/` · legacy
    `wiki/tickets/`)은 `pm_paths.tickets_dir` 해소를 그대로 타므로 여기서 갈라 보지 않는다.
    손상 frontmatter 는 순회 로더(`ticket_store.load_ticket_soft`)가 경고 1줄과 함께 건너뛴다.
    """
    home = pm_paths.home_at(Path(pm_root).resolve())
    found = ticket_store.find_ticket_exact(home, ticket)
    if found is None:
        return ()
    target = ticket_store.load_ticket_soft(home, found[1])
    if target is None:
        return ()
    target_fm = target[0]
    claimed_by = str(target_fm.get("claimed_by") or "").strip()
    target_pairs = _touch_notations(
        target_fm.get("touches"),
        pm_root=pm_root, workspace=workspace,
    )
    if not claimed_by or not target_pairs:
        return ()

    overlaps: list[TicketTouchOverlap] = []
    for path in sorted((pm_paths.tickets_dir(home) / "claimed").glob("*.md")):
        loaded = ticket_store.load_ticket_soft(home, path)
        if loaded is None:
            continue
        other_fm = loaded[0]
        other_id = str(other_fm.get("id") or "").strip()
        if not other_id or other_id == ticket:
            continue
        if str(other_fm.get("claimed_by") or "").strip() != claimed_by:
            continue
        other_pairs = _touch_notations(
            other_fm.get("touches"),
            pm_root=pm_root, workspace=workspace,
        )
        if not other_pairs:
            continue
        declarations: list[str] = []
        shared: list[str] = []
        for other_raw, other_folded in other_pairs:
            for _target_raw, target_folded in target_pairs:
                if not ticket_rules._touch_paths_overlap(other_folded, target_folded):
                    continue
                declarations.append(other_raw)
                shared.append(_narrower_touch_path(other_folded, target_folded))
        if shared:
            overlaps.append(TicketTouchOverlap(
                other_id,
                claimed_by,
                tuple(dict.fromkeys(declarations)),
                tuple(dict.fromkeys(shared)),
            ))
    return tuple(overlaps)


def _format_overlap_items(items: Sequence[str]) -> str:
    """진단 목록 1줄 — 상한 초과분은 개수로 접는다."""
    listed = tuple(items)
    head = ", ".join(listed[:_TOUCH_OVERLAP_DISPLAY_LIMIT])
    remainder = len(listed) - _TOUCH_OVERLAP_DISPLAY_LIMIT
    return head + (f" … 외 {remainder}건" if remainder > 0 else "")


def format_touch_overlap_warning(
    ticket: str, overlaps: Sequence[TicketTouchOverlap],
) -> str:
    """교집합을 사전 loud 경고 1블록으로 만든다. 겹침이 없으면 출력도 없다."""
    if not overlaps:
        return ""
    lines = [
        _TOUCH_OVERLAP_HEADER,
        f"경고: 병렬 위임 touches 겹침 — 같은 세션({overlaps[0].claimed_by})이 claim 중인 "
        f"ticket 과 {ticket} 의 범위가 겹칩니다.",
    ]
    lines.extend(
        f"  - {overlap.ticket}({_format_overlap_items(overlap.declarations)}) ∩ {ticket}: "
        f"{_format_overlap_items(overlap.paths)}"
        for overlap in overlaps[:_TOUCH_OVERLAP_DISPLAY_LIMIT]
    )
    hidden = len(overlaps) - _TOUCH_OVERLAP_DISPLAY_LIMIT
    if hidden > 0:
        lines.append(f"  - … 외 {hidden}건의 ticket 이 더 겹칩니다")
    lines.append(_TOUCH_OVERLAP_PRESCRIPTION)
    return "\n".join(lines)


def overlap_touch_paths(overlaps: Sequence[TicketTouchOverlap]) -> tuple[str, ...]:
    """모든 교집합 경로의 합집합 — 회수 시점 "겹친 파일이 실제로 바뀌었나" 판정 입력."""
    paths: list[str] = []
    for overlap in overlaps:
        paths.extend(overlap.paths)
    return tuple(sorted(dict.fromkeys(paths)))


def _changed_overlap_paths(
    audit: ScopeAudit, after, role: str,
) -> tuple[str, ...]:
    """교집합 경로 중 이 위임 시간창에 **실제로 바뀐** workspace 경로들.

    `ScopeAudit.overlap_paths` 는 사전 축이 이미 이 workspace 좌표로 접어 둔 값이라(같은
    `_folded_touch_pairs` 규칙) 여기서 좌표를 다시 만들지 않는다 — 한 기능에 좌표 규칙 하나.
    """
    if not audit.overlap_paths or role not in delegate_run_rules.WRITE_ROLES:
        return ()
    changed = set(delegate_scope_rules.changed_status_paths(audit.before, after))
    changed.update(delegate_scope_git.committed_paths(
        audit.before, after, workspace=audit.workspace,
    ))
    return tuple(sorted(
        path for path in changed
        if any(ticket_rules._touch_paths_overlap(path, root) for root in audit.overlap_paths)
    ))


def format_changed_overlap_warning(paths: Sequence[str]) -> str:
    """회수 시점 1줄 — 겹친 파일이 실제로 바뀌었으면 공존 여부 확인을 요구한다."""
    if not paths:
        return ""
    return (
        "경고: 겹친 파일 변경됨 — 다른 dev 산출 공존 여부를 확인하라: "
        + _format_overlap_items(paths)
    )


# ── 위임 범위 밖 변경 감지 훅 (delegate_scope 판정 재사용·never-block) ──────

class ScopeAudit(NamedTuple):
    """위임 **전체 단위**(primary + 폴백 attempt 포함) 범위 판정 입력."""

    touches: tuple[str, ...] | None  # None이면 generic 축만 강등(ticket 준비 실패)
    before: delegate_scope_rules.WorktreeState
    workspace: Path               # git toplevel(=--cwd 가 하위 디렉토리여도 판정 기준은 루트)
    pm_root: Path                # --cwd lease에서 해소한 board/config 소유 PM 홈
    adapter_roots: tuple[str, ...] = ()  # preamble과 같은 실행-전 등록부 스냅샷
    overlap_paths: tuple[str, ...] = ()  # 같은 세션 claimed ticket과 겹치는 touches 경로


def _warn_dropped_touch(item: str, reason: str) -> None:
    """정규화 못 한 touches 항목을 loud 하게 알린다(드롭 = 그만큼 허용 범위가 좁아진다)."""
    print(
        f"경고: touches 항목 '{item}' 을 이 workspace 좌표로 해소하지 못해 범위 판정에서 뺍니다"
        f"({reason}).",
        file=sys.stderr,
    )


def _with_template_propagation(
    touches: Sequence[str], *, workspace: Path, pm_root: Path | None = None,
) -> tuple[str, ...]:
    """엔진 tools touch에 대응하는 실재 templates 하네스 경로를 허용 집합에 보탠다."""
    templates = workspace / "templates"
    harnesses = tuple(
        path.name for path in sorted(templates.iterdir()) if path.is_dir()
    ) if templates.is_dir() else ()
    expanded = list(touches)
    for raw in touches:
        parts = PurePosixPath(raw.replace("\\", "/")).parts
        # ticket이 실제 workspace 루트 엔진 tools를 허용한 경우에만 기계 전파본을 보탠다.
        # 이미 templates 아래인 touch나 경로 중간의 tools 표기는 다른 하네스 전체를 열지 않는다.
        marker = next(
            (
                index for index in range(len(parts) - 1)
                if parts[index:index + 2] == (".project_manager", "tools")
            ),
            None,
        )
        if marker is None:
            continue
        prefix = PurePosixPath(*parts[:marker])
        root_touch = not prefix.parts
        if pm_root is not None and prefix.parts:
            root_touch = (pm_root / prefix).resolve() == workspace.resolve()
        if not root_touch:
            continue
        suffix = parts[marker:]
        for harness in harnesses:
            candidate = workspace / "templates" / harness
            if (candidate / ".project_manager" / "tools").is_dir():
                expanded.append(PurePosixPath("templates", harness, *suffix).as_posix())
    return tuple(dict.fromkeys(expanded))


def begin_scope_audit(
    home, ticket: str | None,
    cwd: Path,
    *,
    pm_root: Path | None = None,
    adapter_roots: Sequence[str],
) -> ScopeAudit | None:
    """위임 실행 **직전** worktree 상태를 캡처한다.

    호출 시점은 실행-전 게이트(매핑·재앵커·dry-run)를 **모두 통과한
    뒤**다 — 아무것도 실행하지 않은 경로에서 판정을 켜면 무의미한 git 호출·오탐만 는다.
    캡처/정규화 기준은 `--cwd` 가 아니라 **git toplevel** 이다 — repo 하위 디렉토리를 --cwd 로 주면
    슬롯 루트와 좌표가 어긋나 판정이 통째로 꺼진다. `--ticket` 이 없으면 `touches=()` 라 허용 경로가
    0이다(delegate_scope_rules·delegate_scope_git 계약 — 변경이 있으면 전부 경고).

    ``ScopeAudit.overlap_paths``는 실행 **전**에 이미 계산해 경고한 병렬 위임 교집합 경로다
    (같은 세션이 claim 중인 다른 ticket 과 겹치는 선언). 이 함수는 그 축을 계산하지 않는다 —
    호출부가 회수 시점 판정용으로 결과에 실어 준다(``_replace``).

    공통 베이스라인(toplevel·worktree 캡처)이 실패하면 전후 차이를 만들 수 없어 generic과
    어댑터 두 축이 모두 죽는다. 반면 board 로드·ticket 부재/손상으로 touches 준비만 실패하면
    generic 축만 loud 강등하고, 같은 worktree 캡처를 쓰는 어댑터 축은 보존한다. 일반 실패는 위임을 막지 않고 경고한다."""
    roots = tuple(adapter_roots)
    try:
        workspace = delegate_scope_git.resolve_workspace_root(cwd)
        before = delegate_scope_git.capture_worktree_state(workspace)
    except Exception as exc:  # noqa: BLE001 — 공통 캡처 실패만 두 축을 함께 끈다.
        print(
            "경고: 위임 범위 판정 준비 실패"
            f"(공통 worktree 캡처: {exc}) — generic·어댑터 두 축 모두 판정 불가, 비차단 진행.",
            file=sys.stderr,
        )
        return None

    # 해시 대상이 있는데 지문을 하나도 못 구했으면 이미 dirty 한 파일의 재수정을 못 잡는다 —
    # 강등된 채로 조용히 통과시키지 않는다(축소된 감지력을 PM 이 알아야 한다).
    try:
        degraded = delegate_scope_git.content_signal_missing(before, workspace)
    except Exception as exc:  # noqa: BLE001 — 보강 신호 진단 실패로 두 판정축을 버리지 않는다.
        degraded = False
        print(f"경고: 내용 해시 보강 상태 확인 실패({exc}) — 비차단 진행.", file=sys.stderr)
    if degraded:
        print(
            "경고: 내용 해시 보강 신호 없음 — 이미 dirty 한 파일의 재수정은 감지 불가"
            "(상태코드/mode 신호로만 판정).",
            file=sys.stderr,
        )

    try:
        resolved_pm_root = pm_root or home.root
        touches = delegate_scope_git.ticket_touches(
            home, ticket,
        ) if ticket else ()
        touches = _with_template_propagation(
            touches, workspace=workspace, pm_root=resolved_pm_root,
        )
    except Exception as exc:  # noqa: BLE001 — touches의 형제인 어댑터 축은 보존한다.
        touches = None
        print(
            "=== ⚠ 위임 범위 판정 축 강등 ===\n"
            f"ticket touches 준비 실패({exc}) — generic 범위 축만 판정할 수 없습니다. "
            "공통 worktree 캡처가 살아 있어 어댑터 편집 축은 계속 판정합니다.",
            file=sys.stderr,
        )
    return ScopeAudit(touches, before, workspace, pm_root or home.root, roots)


def _adapter_edit_paths(
    before,
    after,
    *,
    workspace: Path,
    roots: Sequence[str],
) -> tuple[str, ...] | None:
    """touches와 무관하게 등록 어댑터 루트 전체에서 바뀐 경로를 반환한다.

    대상은 출하 카드 하위로 좁히지 않는다. 실제 템플릿의 등록 루트에는 카드뿐 아니라
    settings/hooks/runner/agent 정의도 함께 있고, 역할 preamble도 "통합 루트 전체"를 금지한다.
    여기서 별도 하위 목록을 만들면 새 어댑터 표면이 다시 빠지므로 preamble 합성 때 선행 등록부에서
    파생해 ``ScopeAudit``에 보존한 동일 스냅샷을 소비한다. 회수 시 등록부를 다시 읽으면 위임 중
    registry 변경에 따라 전달한 금지 범위와 판정 범위가 갈린다.

    ``None``은 비-literal 표기/형제 파일 부재가 기존 graceful-degrade 경로를 타 등록 루트가
    0개가 된 경우다. 추측 목록으로 보충하지 않고 호출부가 판정 불가를 loud 경고한다.
    """
    roots = tuple(roots)
    if not roots:
        return None
    changed = set(delegate_scope_rules.changed_status_paths(before, after))
    changed.update(delegate_scope_git.committed_paths(before, after, workspace=workspace))
    return tuple(
        path for path in sorted(changed)
        if any(path == root or path.startswith(root.rstrip("/") + "/") for root in roots)
    )


def _format_adapter_edit_warning(paths: Sequence[str]) -> str:
    """역할 공통 금지 위반을 기존 범위-밖 축과 구분된 advisory 블록으로 만든다."""
    unique = tuple(sorted(set(paths)))
    if not unique:
        return ""
    lines = [
        "=== ⚠ 역할 공통 금지 위반: 어댑터 편집 ===",
        "어댑터 디렉토리 수정이 감지되었습니다. 이는 ticket touches 포함 여부와 무관한 "
        "역할 공통 금지 위반입니다.",
        "차단하지 않으며 PM이 정당한 어댑터 작업으로 수용할지 격리/복원할지 판정해야 합니다.",
        "  · gitignored 산출물(.project_manager/.local 등)은 git status 입력에 없어 판정 대상이 아닙니다.",
        "  · 위임 시간창 기준이라 다른 터미널/도구가 만든 변경도 섞일 수 있습니다.",
        "  · 판정 범위는 이 git repo(toplevel) 안입니다 — 그 밖/중첩 repo 안의 변경은 보이지 않습니다.",
    ]
    lines.extend(f"  - {path}" for path in unique)
    return "\n".join(lines)


def report_scope_audit(audit: ScopeAudit | None, role: str) -> None:
    """위임 **회수 시점**(모든 attempt 종료 후 1회)의 변경을 두 독립 축으로 loud 경고한다.

    축 1은 ticket touches 기반 범위 밖 변경, 축 2는 touches와 독립적인 역할 공통 어댑터 편집
    금지 위반이다. 축 1에는 병렬 위임 교집합(실행 전 경고한 겹침 경로)이 실제로 바뀌었는지
    1줄이 따라붙는다 — 같은 전후 캡처를 소비하는 같은 축의 사후 신호다.
    반환값/rc 를 바꾸지 않는다 — 격리/복원/수용 판정은 PM 몫이다. 한 축의 판정
    실패가 다른 축까지 지우지 않으며 둘 다 비차단이다. 쓰기 허용 역할집합은 이 모듈의
    WRITE_ROLES 를 주입해 단일 출처로 쓴다(감지기 기본값과의 드리프트는 테스트가 막는다).
    raw 박제 기본 `.project_manager/.local/delegate/`는 gitignored라 판정에 안 잡히며,
    `--output-dir`를 repo 안의 비-ignore 경로로 주면 그 산출물도
    '위임이 만든 변경'으로 잡힌다(의도). 일반 판정 실패는 경고하고 비차단 진행한다.

    형제-가지 여집합 결정: 어댑터 축은 ``--ticket``이 있을 때만 돌지 않는다. 생략 실행에서도
    반드시 돌며, 그때 같은 경로가 "허용 0의 범위 밖"과 "역할 공통 금지 위반" 두 블록에 나오는
    중복은 의도적이다. 한 블록으로 합치면 PM이 정책 성격을 오판하므로 경로 중복보다 축 식별을
    우선한다."""
    if audit is None:
        return
    try:
        after = delegate_scope_git.capture_worktree_state(audit.workspace)
        if delegate_scope_rules.head_moved(audit.before, after):
            # 커밋 자체가 역할 계약 위반 신호다(위임 역할은 commit/push 금지) — 범위 안이든 밖이든
            # 별도로 알린다. 커밋된 경로는 아래 판정 입력에 합산된다(worktree 는 clean 이라 무증거).
            print(
                "경고: 위임 중 커밋이 발생했습니다(위임 역할은 commit 금지) — "
                f"HEAD {audit.before.head or '(없음)'} → {after.head or '(없음)'}. "
                "커밋된 변경도 범위 판정에 합산합니다.",
                file=sys.stderr,
            )
    except Exception as exc:  # noqa: BLE001 — 공통 캡처 실패면 두 축 모두 판정 불가.
        print(f"경고: 위임 범위 판정 실패({exc}) — 비차단 진행.", file=sys.stderr)
        return

    if audit.touches is not None or audit.overlap_paths:
        try:
            if audit.touches is not None:
                paths = delegate_scope_git.out_of_scope_changes(
                    audit.before, after,
                    touches=audit.touches, role=role, pm_root=audit.pm_root,
                    workspace=audit.workspace,
                    write_roles=delegate_run_rules.WRITE_ROLES, on_drop=_warn_dropped_touch,
                )
                warning = delegate_scope_rules.format_warning(paths)
                # **계산 직후 즉시** 출력한다 — 뒤따르는 교집합 계산이 실패해도 이미 확정된
                # 범위-밖 신호가 함께 사라지면 "한 축 실패가 다른 축을 지우지 않는다"가 깨진다.
                if warning:
                    print(warning, file=sys.stderr)
            # 교집합 축은 같은 전후 캡처를 소비하므로 같은 판정 경계 안에서 계산한다(같은
            # 비차단 경고 하나). 위 출력이 선행하므로 이 축의 실패는 기존 신호를 못 지운다.
            overlap_warning = format_changed_overlap_warning(
                _changed_overlap_paths(audit, after, role)
            )
            if overlap_warning:
                print(overlap_warning, file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 — 일반 판정 실패는 경고 후 비차단 진행.
            print(f"경고: 위임 범위 판정 실패({exc}) — 비차단 진행.", file=sys.stderr)

    try:
        adapter_paths = _adapter_edit_paths(
            audit.before, after, workspace=audit.workspace,
            roots=audit.adapter_roots,
        )
    except Exception as exc:  # noqa: BLE001 — 어댑터 축만 강등, 기존 범위 축은 보존.
        print(f"경고: 어댑터 편집 경고 축 판정 실패({exc}) — 비차단 진행.", file=sys.stderr)
        return
    if adapter_paths is None:
        print(
            "=== ⚠ 어댑터 편집 경고 축 강등 ===\n"
            "선행 등록부 파생 경로가 0개라 역할 공통 어댑터 수정 금지 위반을 판정할 수 없습니다. "
            "새 목록으로 추측하지 않으며 비차단 진행합니다.",
            file=sys.stderr,
        )
        return
    adapter_warning = _format_adapter_edit_warning(adapter_paths)
    if adapter_warning:
        print(adapter_warning, file=sys.stderr)
