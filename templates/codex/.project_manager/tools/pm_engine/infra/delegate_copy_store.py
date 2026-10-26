"""위임 사본과 장부 저장 (층: infra)."""
from __future__ import annotations

from pathlib import Path
import contextlib
from pm_engine.infra import delegate_copy_git
from pm_engine.infra.harness import delegate_env
from pm_engine.domain import delegate_errors
from pm_engine.infra import delegate_ledger
from pm_engine.domain import delegate_rounds
from pm_engine.infra.harness import delegate_temp
from pm_engine.infra import fs
import json
import os
from pm_engine.infra import pm_paths
from pm_engine.infra import raw_output
import re
from pm_engine.domain import round_rules
from pm_engine.infra import round_store
import stat
import subprocess

_TICKET_COPY_FD_SUPPORTED = (
    hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
    and os.open in getattr(os, "supports_dir_fd", frozenset())
    and os.mkdir in getattr(os, "supports_dir_fd", frozenset())
)


def _write_exclusive_file(
    path: Path, data: bytes, mode: int, *, parent_fd: int | None = None,
) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    target = path.name if parent_fd is not None else str(path)
    open_kwargs = {"dir_fd": parent_fd} if parent_fd is not None else {}
    fd = os.open(target, flags, mode, **open_kwargs)
    try:
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(fd)


def _append_delegate_rounds_ledger(home, row: dict) -> None:
    """공유 atomic-append seam 으로 0600 장부에 한 행을 덧붙인다(기존 bytes 불변)."""
    pm_home_root = home.local_dir.parent.parent
    delegate_ledger._delegate_rounds_ledger_row(row, line_number=0)
    local_dir, local_fd = _secure_machine_dir(
        pm_home_root, delegate_ledger.DELEGATE_ROUNDS_LEDGER_REL_PATH.parent.parts,
        label="PM delegate-rounds 장부 디렉터리",
    )
    path = local_dir / delegate_ledger.DELEGATE_ROUNDS_LEDGER_REL_PATH.name
    try:
        try:
            observed = path.lstat()
        except FileNotFoundError:
            observed = None
        if observed is not None and not stat.S_ISREG(observed.st_mode):
            raise delegate_errors.DelegateError(f"delegate-rounds 장부가 regular file 이 아님: {path}")
        payload = (
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        )
        fs.append_atomic(path, payload, mode=0o600)
    except OSError as exc:
        raise delegate_errors.DelegateError(f"delegate-rounds 장부 append 실패: {path}: {exc}") from exc
    finally:
        if local_fd is not None:
            os.close(local_fd)


def _delegate_round_record(home, copy_path: Path) -> dict:
    """이 슬롯 파일의 **준비 기록** — 없으면 회수를 거부한다(신뢰 뿌리는 PM 홈 장부다)."""
    pm_home_root = home.local_dir.parent.parent
    resolved = str(Path(copy_path).resolve())
    matches = [
        row for row in delegate_ledger._delegate_rounds_ledger_records(pm_home_root)
        if str(Path(row["copy"]).resolve()) == resolved
    ]
    if not matches:
        raise delegate_errors.DelegateError(
            f"delegate-rounds 장부에 준비 기록 없음: {resolved} — 준비하지 않은 파일은 "
            "회수하지 않습니다"
        )
    return matches[-1]


def _secure_machine_dir(
    root: Path, relative_parts: tuple[str, ...], *, label: str,
) -> tuple[Path, int | None]:
    """root 아래 기계 디렉터리를 no-symlink chain으로 만들고 0700으로 고정한다."""
    root = root.resolve()
    current = root
    if _TICKET_COPY_FD_SUPPORTED:
        try:
            current_fd = os.open(
                str(root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
        except OSError as exc:
            raise delegate_errors.DelegateError(f"slot cwd를 안전하게 열 수 없음: {root}: {exc}") from exc
        try:
            for part in relative_parts:
                if part in ("", ".", "..") or "/" in part or "\\" in part:
                    raise delegate_errors.DelegateError(f"{label} 경로 성분 거부: {part!r}")
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
                try:
                    child_fd = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=current_fd,
                    )
                except OSError as exc:
                    raise delegate_errors.DelegateError(
                        f"{label} 경로 symlink/비-directory 거부: {current / part}: {exc}"
                    ) from exc
                os.close(current_fd)
                current_fd = child_fd
                current = current / part
            delegate_temp._assert_attempt_child_path(root, current, label=label)
            return current, current_fd
        except BaseException:
            os.close(current_fd)
            raise
    for part in relative_parts:
        if part in ("", ".", "..") or "/" in part or "\\" in part:
            raise delegate_errors.DelegateError(f"{label} 경로 성분 거부: {part!r}")
        candidate = current / part
        try:
            candidate.mkdir(mode=0o700)
        except FileExistsError:
            pass
        try:
            observed = candidate.lstat()
        except OSError as exc:
            raise delegate_errors.DelegateError(f"{label} 검사 실패: {candidate}: {exc}") from exc
        if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode):
            raise delegate_errors.DelegateError(f"{label} symlink/비-directory 거부: {candidate}")
        resolved = candidate.resolve()
        if resolved.parent != current.resolve():
            raise delegate_errors.DelegateError(f"{label} escape/교체 거부: {candidate}")
        current = candidate
    delegate_temp._assert_attempt_child_path(root, current, label=label)
    return current, None


def _secure_cluster_run_dir(cwd: Path, cluster: str, run_id: str) -> tuple[Path, int | None]:
    return _secure_machine_dir(
        cwd, delegate_ledger._cluster_run_relative_dir(cluster, run_id).parts,
        label="티켓 라운드 run-dir",
    )


def _secure_ticket_copy_dir(
    cwd: Path, cluster: str, run_id: str, ticket: str, *,
    extra_parts: tuple[str, ...] = (),
) -> tuple[Path, int | None]:
    return _secure_machine_dir(
        cwd,
        (*delegate_ledger._ticket_run_relative_dir(cluster, run_id, ticket).parts, *extra_parts),
        label="티켓 라운드 run-dir",
    )


def _ticket_copy_relative_path(
    cluster: str, run_id: str, ticket: str, filename: str,
) -> Path:
    """run-dir 안 한 파일의 cwd 상대 경로 (ignore 검증·경로 예산 단언의 입력)."""
    return delegate_ledger._ticket_run_relative_dir(cluster, run_id, ticket) / filename


def _parse_check_ignore_verbose_line(line: str) -> tuple[str, str, str] | None:
    """`git check-ignore -v` 한 줄을 (source, linenum, pattern) 으로 분해한다.

    비-`-z` 출력 형식은 `소스:줄번호:패턴` 다음에 탭 하나, 그리고 pathname 이 온다( F-005 —
    대안으로 `git check-ignore -v -z --stdin` 에 pathname 하나를 stdin 으로 주면 rc=0 에 소스·줄번호·
    패턴·pathname 4필드가 NUL 로 구분돼 나와 모호성이 0 이지만(실측), 위임 호출은 pathname 1건
    고정값이라 stdin 배관을 더할 이득이 낮아 채택하지 않았다 — 단일 인자에 `-vz`(`--stdin` 없이)는
    "fatal: -z 옵션은 --stdin 옵션과 같이 써야만 의미가 있습니다" 로 즉시 실패한다(git 2.43 실측)).
    pathname 은 호출부가 구성한 알려진 상대경로라 마지막 탭 분리(`rpartition`)로 안전하게 떼어낸다 —
    패턴 문자열 자체에 탭이 들어 있어도 pathname 은 안 잘린다. 남는 `소스:줄번호:패턴`은 그리디
    정규식으로 오른쪽에서부터 역추적해 최우측 콜론-숫자-콜론 구분자를 찾는다 — Windows 절대경로
    출처(드라이브 문자 뒤 콜론, 확장경로 접두 표기의 백슬래시 두 개·물음표·백슬래시)의 콜론이
    숫자로 이어지지 않아 구분자로 오인되지 않고, source 경로 내부에 콜론이 더 있어도 줄번호와
    가장 가까운(최우측) 매치를 고른다.
    """
    prefix, tab, _pathname = line.rpartition("\t")
    if not tab:
        return None
    match = re.match(r"^(.*):(\d+):(.*)$", prefix)
    if match is None:
        return None
    return match.group(1), match.group(2), match.group(3)


def _ignore_source_definitely_untracked(cwd: Path, source: str) -> bool:
    """`ls-files` 호출 없이도 tracked 일 수 없다고 판정 가능한 출처 형태( F-001/F-002/F-006).

    저장소 밖 출처(전역 `core.excludesFile` — 절대경로뿐 아니라 `../outside_ignore` 처럼 저장소
    밖을 가리키는 상대경로도 있다·실측 — 와 linked worktree 의 `.git/info/exclude` 는 공유 gitdir
    바깥 절대경로로 보고된다)와 `.git/` 내부 경로(`.git/info/exclude`)는 git 인덱스가 원천 추적할
    수 없는 위치다. 이런 값을 `ls-files` 에 그대로 넘기면 "저장소 밖" 류 fatal 로 엉뚱하게 실패하므로,
    호출 전에 `(cwd/source).resolve()` 가 `cwd.resolve()` 아래인지로 절대·상대 두 형태를 한 규칙으로
    걸러 바로 untracked 로 판정한다.
    """
    resolved_source = (cwd / source).resolve()
    if not delegate_env._is_relative_to(resolved_source, cwd.resolve()):
        return True
    return Path(source).parts[:1] == (".git",)


def _check_ignore_source_is_tracked(cwd: Path, source: str) -> bool:
    """`source`(check-ignore 가 보고한 -C cwd 상대경로)가 git 인덱스에 tracked 인지 확인한다.

    rc=0 tracked · rc=1 untracked · 그 외는 도구 실패로 fail-loud(board.py:4405 `ls-files
    --error-unmatch` 와 같은 seam).
    """
    if _ignore_source_definitely_untracked(cwd, source):
        return False
    result = subprocess.run(
        ["git", "-C", str(cwd), "ls-files", "--error-unmatch", "--", source],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    cause = result.stderr.strip() or result.stdout.strip() or "원인 미상"
    raise delegate_errors.DelegateError(f"git ls-files 실패(rc={result.returncode}): {cause}")


def _pool_bare_for_code_root(cwd: Path) -> Path | None:
    """이 코드 트리가 두-git 풀 슬롯이면 그 풀의 공용 bare 를, 임베드면 None 을 돌려준다.

    형상 규칙 사본을 만들지 않는다 — 소유 PM 홈은 `pm_paths.owning_pm_home` 이 이 트리의 `.git`
    선언에서 유도하고, 여기서는 두 값만 비교한다. 그 유도가 인정하는 공용 저장소 형상은
    `<X>/.git`(임베드·단일 clone·그 clone 이 판 linked worktree)과 `<X>/.repos/<repo>.git`
    (풀 슬롯) 둘뿐이고 그 밖은 예외이므로, 공용 저장소가 `<PM 홈>/.git` 이 아니면 남는 형상은
    풀 bare 하나이며 그 값이 곧 bare 다.
    """
    common = Path(delegate_copy_git._cluster_git(cwd, "rev-parse", "--git-common-dir").strip())
    if not common.is_absolute():
        common = cwd / common
    common = common.resolve()
    pm_home_root = Path(pm_paths.owning_pm_home(cwd)).resolve()
    if common == (pm_home_root / ".git").resolve():
        return None
    return common


def _check_ignore_observation(result: subprocess.CompletedProcess) -> str:
    """check-ignore 관측을 진단 한 조각으로 요약한다 — 이 문자열은 **판정 입력이 아니다**.

    풀 갈래가 보는 것은 rc(사본 루트가 실제로 무시되는가)와 bare 규칙 존재 둘뿐이고, 어느
    출처가 그 판정을 냈는지는 진단 문구에만 싣는다.
    """
    if result.returncode == 1:
        return "현재 이 경로를 무시하는 규칙이 없다"
    lines = [entry for entry in (result.stdout or "").splitlines() if entry]
    parsed = _parse_check_ignore_verbose_line(lines[0]) if len(lines) == 1 else None
    if parsed is None:
        return f"현재 check-ignore 출력={result.stdout!r}"
    source, linenum, pattern = parsed
    return f"현재 check-ignore 출처={source}:{linenum} 패턴={pattern!r}"


def _assert_pool_bare_ignore_rule(bare: Path, *, ignored: bool, observation: str) -> None:
    """풀 슬롯의 정본 — 사본 루트가 실제로 무시되고 그 규칙이 이 풀 bare 의 `info/exclude` 에
    있는지 fail-loud 확인한다. 둘을 함께 요구한다.

    규칙 존재만 보면 슬롯 안 `.gitignore` 의 부정 규칙 한 줄이 정본을 뒤집어도 통과한다 — 그
    형상에서 사본 루트는 실제로 무시되지 않아 게이트가 지키려던 성질이 이 갈래에서만 사라진다.
    거꾸로 check-ignore 가 어느 출처를 보고했는지는 판정 입력이 아니다. 중첩 `.gitignore` 가
    `info/exclude` 보다 우선순위가 높아, 출처로 판정하면 규칙이 없는 풀도 그 중첩 파일 하나로
    우연히 통과한다. 파일 부재는 규칙 부재와 같은 거부이고, 읽기 실패는 그대로 올린다
    (빈 값으로 바꾸지 않는다).
    """
    exclude = bare / "info" / "exclude"
    if not ignored:
        raise delegate_errors.DelegateError(
            f"사본 루트가 이 슬롯에서 실제로 무시되지 않음 — {observation} · 정본은 {exclude} 의 "
            f"`{delegate_rounds.TICKET_COPY_POOL_IGNORE_RULE}` 줄이고 `pm-config repo add {bare.stem}` 1회로 "
            "엔진이 멱등 설치한다 · 그 줄이 이미 있으면 슬롯 안 `.gitignore` 의 부정 규칙이 "
            "정본보다 우선해 뒤집은 것이니 그 줄을 지워라"
        )
    lines = (
        fs
        .read_text_shared(exclude, encoding="utf-8", errors="replace")
        .splitlines()
        if exclude.is_file() else []
    )
    if any(line.strip() == delegate_rounds.TICKET_COPY_POOL_IGNORE_RULE for line in lines):
        return
    raise delegate_errors.DelegateError(
        f"사본 루트를 숨기는 ignore 규칙이 이 풀의 bare 에 없음 — 정본은 {exclude} 의 "
        f"`{delegate_rounds.TICKET_COPY_POOL_IGNORE_RULE}` 줄이다({observation}) · 이 bare 를 공유하는 모든 "
        f"슬롯이 그 규칙을 함께 받는다 · `pm-config repo add {bare.stem}` 1회로 엔진이 멱등 "
        "설치한다"
    )


def _assert_ticket_copy_root_ignored(
    cwd: Path, *, cluster: str, run_id: str, ticket: str,
) -> None:
    """ignore 규칙이 실제 ticket-copy 경로 형상을 숨기는지, 임베드 형상이면 그 규칙이 tracked
    `.project_manager/.gitignore` 유래인지 fail-loud 확인한다.

    정본은 형상마다 정확히 하나다 — 임베드·단일 clone 은 tracked
    `.project_manager/.gitignore`(아래 본문), 두-git 풀 슬롯은 그 풀 bare 의 `info/exclude`
    (`_assert_pool_bare_ignore_rule`)다. 한쪽이 거부돼도 다른 후보로 재시도하지 않는다.
    앱 저장소에 `.project_manager` 가 없는 풀 슬롯은 tracked 정본을 가질 수 없어, 정본을
    파일 경로 하나로 못박으면 구조적으로 통과할 수 없다.

    경로가 정본 위치와 같아도 그 파일 자체가 untracked 면(예: fresh import 직후 아직 `git add`
    하지 않은 형상) 다른 클론에는 없는 로컬 산출물일 수 있어 별도로 `ls-files --error-unmatch` 로
    확인한다(F-001). 정본 위치가 아닌 출처는 그 출처 자신이 tracked 인지에 따라 진단 문구를
    가른다(F-002) — tracked 비정본 위치(예: 루트 `.gitignore`)는 다른 클론에도 있으므로 "이
    클론에만 있다"는 말은 거짓이고, untracked 출처(`.git/info/exclude`·전역 `core.excludesFile`·
    untracked 상위 `.gitignore`)만 그 말이 사실이다. tracked 판정은 `ls-files --error-unmatch`
    그대로라 인덱스 등록(staged)까지를 tracked 로 본다 — 커밋 여부는 보지 않는다(F-009: 첫 커밋
    전 채택자 트리를 오차단하지 않기 위해서다).
    """
    result = subprocess.run(
        [
            "git", "-C", str(cwd), "check-ignore", "-v",
            _ticket_copy_relative_path(
                cluster, run_id, ticket, delegate_rounds.TICKET_COPY_SPEC_NAME,
            ).as_posix(),
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    if result.returncode not in (0, 1):
        cause = result.stderr.strip() or result.stdout.strip() or "원인 미상"
        raise delegate_errors.DelegateError(f"git check-ignore 실패(rc={result.returncode}): {cause}")
    bare = _pool_bare_for_code_root(cwd)
    if bare is not None:
        _assert_pool_bare_ignore_rule(
            bare,
            ignored=result.returncode == 0,
            observation=_check_ignore_observation(result),
        )
        return
    if result.returncode == 1:
        raise delegate_errors.DelegateError(
            "사본 루트가 git 무시 대상이 아님 — `.project_manager/.gitignore` 의 "
            "`.local/` 규칙을 복원하라"
        )
    lines = [entry for entry in (result.stdout or "").splitlines() if entry]
    parsed = _parse_check_ignore_verbose_line(lines[0]) if len(lines) == 1 else None
    if parsed is None:
        raise delegate_errors.DelegateError(
            "git check-ignore -v 출력을 해석하지 못함(source:linenum:pattern 형식 아님): "
            f"{result.stdout!r}"
        )
    source, linenum, pattern = parsed
    expected = (cwd / delegate_rounds.TICKET_COPY_IGNORE_TRACKED_SOURCE).resolve()
    observed = (cwd / source).resolve()
    if observed != expected:
        if _check_ignore_source_is_tracked(cwd, source):
            reason = "tracked 비정본 위치 유래 — 다른 클론에도 있지만 정본 위치가 아님"
        else:
            reason = "로컬 전용 소스 유래 — 다른 클론·채택자 트리에는 이 규칙이 없음"
        raise delegate_errors.DelegateError(
            "사본 루트를 숨기는 ignore 규칙이 정본 위치(`.project_manager/.gitignore`) 밖 출처 — "
            f"{reason} — 출처={source}:{linenum} 패턴={pattern!r} · `.project_manager/.gitignore` "
            "에 `.local/` 규칙을 복원하라"
        )
    if not _check_ignore_source_is_tracked(cwd, delegate_rounds.TICKET_COPY_IGNORE_TRACKED_SOURCE.as_posix()):
        raise delegate_errors.DelegateError(
            "사본 루트를 숨기는 `.project_manager/.gitignore` 규칙이 untracked 상태 — "
            f"출처={source}:{linenum} 패턴={pattern!r} · 이 클론에만 있는 파일이라 다른 "
            "클론·채택자 트리에는 없어 사본이 노출됩니다 — `.project_manager/.gitignore` 를 "
            "git add 로 커밋 이력에 넣어라"
        )


def _reserved_round_residue(board_rel: str, ordinal: int) -> str:
    """예약 뒤 실패 경로의 진단 꼬리 — 남은 board 라운드 좌표와 그 라운드의 이후 상태.

    예약은 되돌리지 않는다(보상 삭제는 동시 준비가 다음 순번을 이미 잡았을 때 순번 빈틈을
    만든다). 그래서 실패 진단이 무엇이 남았는지 좌표로 말해야 운영자가 board 에서 그 라운드를
    찾을 수 있다.
    """
    return (
        f" · 예약한 board 라운드는 남습니다: {board_rel}(순번 {ordinal}) — 산출 없음으로 남고 "
        "다음 준비는 다음 순번을 씁니다"
    )


def _expected_slot_round_path(cwd: Path, row: dict) -> Path:
    """장부 행이 인가하는 **유일한** 슬롯 경로 — 회수 입력 검증의 단일 단언.

    행의 (ticket, run_id, ordinal, role) 로 경로를 다시 조립해 전량 일치를 요구한다. 규칙이
    하나라 containment(사본 루트 밖)·깊이(run-dir 상위·하위)·파일명 결속(순번·역할)·티켓 결속이
    함께 닫힌다 — 축마다 다른 검사를 두면 하나가 빠졌을 때 조용히 넓어진다.

    비교는 **어휘적**이다(경로 해소를 끼우지 않는다). 해소하면 그 자리가 symlink 로 바뀐 형상에서
    양쪽이 같은 대상으로 접혀 결속 판정이 눈먼다 — 대상 무결성은 chain lstat 과 nofollow 판독이
    따로 본다.
    """
    return Path(cwd) / delegate_ledger._row_ticket_relative_dir(row) / round_rules.round_filename(
        row["ordinal"], row["role"],
    )


def _same_lexical_path(left: Path, right: Path) -> bool:
    """두 절대경로가 같은 자리를 가리키는가 (정규화만 · 해소 없음)."""
    return os.path.normpath(str(left)) == os.path.normpath(str(right))


def _prune_empty_run_dir(ticket_dir: Path) -> None:
    """티켓 자리를 닫은 뒤 **빈** run-dir 을 걷는다 — 마지막 자리가 닫히면 run 도 닫힌다.

    크기 1 묶음이면 자리가 하나라 회수·포기 즉시 run-dir 이 사라진다(종전과 같은 관측).
    다른 티켓의 자리가 남아 있으면 아무것도 하지 않는다. `rmdir` 은 빈 디렉터리에서만
    성공하므로 이 정리가 남의 산출을 지울 수 없다(재귀 삭제 아님).
    """
    parent = Path(ticket_dir).parent
    if not parent.name or parent.name == delegate_ledger.TICKET_COPY_REL_ROOT.name:
        return
    with contextlib.suppress(OSError):
        parent.rmdir()


def _read_slot_round_text(copy_path: Path) -> str:
    """슬롯 라운드 파일을 이식 경계 seam 으로 읽는다 (정규 파일·nofollow·UTF-8).

    경로가 장부 행이 인가한 그것인지는 호출부가 이미 단언했다. 여기서 막는 것은 **그 경로가
    가리키는 대상**이 회수 뒤에 바뀐 형상이다 — 최종 파일의 symlink 와, 사본 루트로 가는 길목의
    디렉터리 교체.
    """
    copy_path = Path(copy_path)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        descriptor = os.open(copy_path, flags)
    except OSError as exc:
        raise delegate_errors.DelegateError(f"티켓 라운드 사본 읽기 실패: {copy_path}: {exc}") from exc
    try:
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode):
            raise delegate_errors.DelegateError(
                f"티켓 라운드 사본이 정규 파일이 아님: {copy_path}"
            )
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read()
    except OSError as exc:
        raise delegate_errors.DelegateError(f"티켓 라운드 사본 읽기 실패: {copy_path}: {exc}") from exc
    finally:
        os.close(descriptor)
    try:
        return raw.decode("utf-8")
    except UnicodeError as exc:
        raise delegate_errors.DelegateError(
            f"티켓 라운드 사본 UTF-8 손상: {copy_path}: {exc}"
        ) from exc


def _assert_slot_run_dir_chain_is_plain(cwd: Path, relative_dir: Path) -> None:
    """티켓 자리까지의 **모든** 마디가 실 디렉터리인지 본다 (symlink 교체 거부).

    시야는 검사 대상 표면과 같아야 한다 — 사본 루트까지만 보면 `<묶음>/<run_id>/<티켓>` 세
    마디가 검사 밖이라, 그 자리를 symlink 로 갈아끼우면 다른 트리의 파일이 회수되고
    `force_rmtree` 가 남의 디렉터리를 지운다. 최종 파일의 nofollow 판독은 그 다음 층이다.
    입력은 장부 행이 인가한 상대 경로 그대로다(`_row_ticket_relative_dir`) — 여기서 규약을
    다시 조립하면 옛 세대 행의 마디를 놓친다.
    """
    current = Path(cwd)
    for part in Path(relative_dir).parts:
        current = current / part
        try:
            observed = current.lstat()
        except OSError as exc:
            raise delegate_errors.DelegateError(f"티켓 라운드 사본 경로 검사 실패: {current}: {exc}") from exc
        if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode):
            raise delegate_errors.DelegateError(
                f"티켓 라운드 사본 경로 symlink/비-directory 거부: {current}"
            )


def _board_relative_path(home, board_path: Path, ) -> str:
    """장부에 싣는 board 상대 경로 — PM 홈 밖으로 해소되면 경계 오류로 표면화한다."""
    pm_home_root = home.local_dir.parent.parent
    try:
        return board_path.resolve().relative_to(pm_home_root).as_posix()
    except ValueError as exc:
        raise delegate_errors.DelegateError(
            f"board 라운드 경로가 PM 홈 밖으로 해소됨: board={board_path} · pm_home_root={pm_home_root}"
        ) from exc


def _reopen_round_path(
    tickets_dir, ticket: str, role: str, *, ordinal: int, lock,
) -> Path:
    """재개방 대상 라운드의 경로 — 판정은 사이드카 seam 하나가 소유한다.

    준비는 이 호출을 두 번 한다(run-dir 만들기 전 사전판정 · board_lock 안 최종). 두 지점이
    같은 규칙을 봐야 흔한 실패가 잔여 없이 끝나고 경합 실패도 같은 문구로 거부된다. 오류형만
    이 층의 것으로 옮긴다 — 준비의 다른 거부와 같은 rc·같은 경로로 나온다.
    """
    try:
        return round_store.reopen_round(
            tickets_dir, ticket, role, ordinal=ordinal, lock=lock,
        )
    except round_rules.RoundsError as exc:
        raise delegate_errors.DelegateError(f"{ticket}: {exc}") from exc


def _cluster_run_rows(home, run_dir: Path) -> list[dict]:
    """run-dir 이 가리키는 **미처분** 장부 행들 — `(묶음, run_id)` 로 해소한다.

    디렉터리 인자는 행을 찾는 **입력 형식**일 뿐이고 인가는 여전히 행이 한다 — 행마다 기존
    단일-경로 전량 일치 단언을 다시 통과시킨다(판정식 완화 0). 이미 회수·포기된 행은 빠진다
    (재회수 개념이 없다).
    """
    pm_home_root = home.local_dir.parent.parent
    run_dir = Path(run_dir).resolve()
    run_id = run_dir.name
    cluster = run_dir.parent.name
    latest: dict[str, dict] = {}
    for row in delegate_ledger._delegate_rounds_ledger_records(pm_home_root):
        if row.get("run_id") == run_id and str(row.get("cluster") or "") == cluster:
            latest[row["copy"]] = row
    rows = [
        row for row in latest.values()
        if row["harvested_at"] is None and "abandoned_at" not in row
    ]
    if not rows:
        raise delegate_errors.DelegateError(
            f"delegate-rounds 장부에 이 run 의 미회수 준비가 없습니다: {run_dir} — "
            "준비하지 않았거나 이미 처분된 run 입니다"
        )
    return sorted(rows, key=lambda row: (row["ticket"], row["ordinal"]))


def _abandon_raw_ledger_hint(home, row: dict) -> str:
    """거부 문구에 붙이는 **비-권위 참고** — 같은 (ticket, role) 의 미마감 raw 레코드.

    판정 술어에는 넣지 않는다. cross 실위임 raw 행에는 `run_id`·`copy` 가 실려 이 예약과
    문자열로 결속되지만, 이 참고는 그 결속을 판정에 쓰지 않고 여전히 (ticket, role) 휴리스틱
    조인만 쓴다 — 판정식 교체는 이 함수 소유가 아니다. 하네스 subprocess 를 띄우지 않는 native
    위임은 raw 행 자체를 만들지 않는다(결속 대상 없음). 휴리스틱을 결정 입력으로 쓰면 증거 없는
    예약이 조용히 "증거 있음"으로 뒤바뀐다. 참고를 읽지 못하면 참고만 빠진다(판정은 그대로다).
    """
    pm_home_root = home.local_dir.parent.parent
    try:
        _raw_dir, ledger_path = raw_output.raw_storage_paths(pm_home_root, "delegate")
        rows = raw_output.raw_records(ledger_path, unfinished_only=True, lock=False)
    except (OSError, ValueError, UnicodeError):
        return ""
    matches = [
        item for item in rows
        if item.get("ticket") == row["ticket"] and item.get("role") == row["role"]
    ]
    if not matches:
        return ""
    latest = matches[0]
    pid = latest.get("pid")
    liveness = "생존" if raw_output.pid_is_alive(pid) else "사망"
    return (
        " · 참고(판정 입력 아님): 같은 티켓·역할의 미마감 raw 레코드가 있습니다 — "
        f"id={latest.get('id')} pid={pid}({liveness})"
    )


def _read_board_round_text(board_path: Path) -> str:
    """board 라운드 파일의 현재 bytes — 개행 표기를 보존해 읽는다(대조·기준선 단일 판독).

    부재는 여기서 답하지 않는다 — 호출부가 부재를 먼저 관측해 그 자리마다 다른 결론(선언을
    받아 종결 / 예약 소실 거부)을 낸다.
    """
    try:
        return fs.read_text_shared(
            board_path, encoding="utf-8", newline="",
        )
    except (OSError, UnicodeError) as exc:
        raise delegate_errors.DelegateError(
            f"board 라운드 파일 읽기 실패: {board_path}: {exc}"
        ) from exc


def _abandon_liveness_problem(
    home, row: dict, *, copy_path: Path, ) -> str | None:
    """종료 증거 판정 — 거부 사유(통과면 None). **파괴 연산의 기본값은 거부다.**

    기계가 증명할 수 있는 것만 기계가 막는다: 행에 `owner_pid` 가 있고 그 프로세스가 살아 있으면
    run 은 진행 중이다. 표식이 없는 행(기존 준비 전부·native 위임)은 증명 수단 자체가 없으므로
    통과시키지 않고 운영자 명시 확인을 요구한다 — 거부도 무조건 허용도 아닌 제3 선택지다.

    거부 문구는 좌표를 값으로 찍는다. 매번 "무엇을 지우는지" 를 보게 하는 것이 명시 확인 플래그가
    습관이 되는 것을 막는 유일한 수단이다.
    """
    coordinates = (
        f"ticket={row['ticket']} · role={row['role']} · ordinal={row['ordinal']} · "
        f"prepared_at={row['prepared_at']} · copy={copy_path}"
    )
    hint = _abandon_raw_ledger_hint(home, row)
    owner_pid = row.get("owner_pid")
    if owner_pid is None:
        return (
            "이 예약에는 종료 증거가 없습니다(장부 행에 owner_pid 없음) — run 이 끝났음을 "
            f"확인했다면 {delegate_rounds.ABANDON_ASSUME_DEAD_FLAG} 로 다시 부르세요: {coordinates}{hint}"
        )
    if raw_output.pid_is_alive(owner_pid):
        return (
            f"준비를 소유한 프로세스가 실행 중이라 포기하지 않습니다 — pid={owner_pid} "
            f"(`ps -p {owner_pid}` 로 확인) · 의도한 우회는 {delegate_rounds.ABANDON_ASSUME_DEAD_FLAG}: "
            f"{coordinates}{hint}"
        )
    return None
