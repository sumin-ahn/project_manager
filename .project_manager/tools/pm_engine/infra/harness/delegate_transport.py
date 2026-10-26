"""위임 실행 인자와 격리 경로 연결 (층: infra)."""
from __future__ import annotations

from typing import Callable
from pathlib import Path
from collections.abc import Sequence
from pm_engine.infra.harness import contract
from pm_engine.infra.harness import delegate_env
from pm_engine.domain import delegate_errors
from pm_engine.infra.harness import delegate_run
from pm_engine.domain import delegate_run_rules
from pm_engine.infra.harness import delegate_temp
import os
import subprocess
import sys
import uuid



def _opencode_transport_launch_target(
    transport: delegate_temp._OpenCodeTransportPrompt,
    argv: list[str],
) -> tuple[list[str], str]:
    """runner 직전에 lexical containment와 symlink 아닌 경로 entry를 재검사한다."""
    delegate_temp._assert_opencode_transport_path(transport.sandbox, transport.path)
    delegate_temp._assert_path_entry_not_symlink(
        transport.path.parent, label="opencode prompt 전달 부모",
    )
    delegate_temp._assert_path_entry_not_symlink(
        transport.path, label="opencode prompt 전달 사본",
    )
    transport.launch_binding_mode = "lexical"
    return argv, str(transport.sandbox)



# 쓰기 대상 실측 프로브. mode 비트는 ACL 플랫폼에서 권한을 말해 주지 않고 `os.access` 도 ACL 을
# 반영하지 않으므로, 0바이트 파일을 실제로 만들어 본 결과만 근거로 쓴다.
#
# 이름은 **짧게** 유지한다 — 프로브가 경로 예산을 엔진 자신의 산출물보다 더 먹으면 안 된다.
# Windows 실측: ticket 사본 디렉터리 199자 + `pm_delegate_write_probe_<pid>_<32hex>`
# 61자 = 260자로 MAX_PATH(259) 를 **프로브만** 넘겨 `FileNotFoundError` 가 났다. 같은 디렉터리의
# 실제 산출물(`metadata.json` 213자·`ticket-example.md` 217자)은 모두 들어간다 — 즉 쓸 수 있는
# 디렉터리를 "못 쓴다"고 잘못 판정했다. 이름 길이 예산은 회귀가 못박는다.
_WRITE_PROBE_PREFIX = ".pmw"
_WRITE_PROBE_SUFFIX_HEX = 8
# 짧은 이름이라 이론상 충돌 가능 — O_EXCL 실패를 "쓰기 불가"로 오판하지 않게 유한 재시도한다.
_WRITE_PROBE_NAME_ATTEMPTS = 3


def _write_probe_name() -> str:
    """프로브 파일 이름 하나 — 짧고 고유하다(길이 예산은 `_probe_writable_target` 주석 참조)."""
    return f"{_WRITE_PROBE_PREFIX}{uuid.uuid4().hex[:_WRITE_PROBE_SUFFIX_HEX]}"


def _probe_writable_target(path: Path, *, label: str) -> None:
    """권한을 올리기 전에 그 경로가 **실제로** 쓰기 가능한지 실측한다.

    실패는 `read-only` 강등이 아니라 사유와 함께 loud 다 — 강등하면 "실행은 됐는데 아무것도
    못 쓰는" 위임이 되고, 그 실패는 위임 실패로만 보인다([[no-green-by-disabling]]).
    """
    if not path.is_dir():
        raise delegate_errors.DelegateError(
            f"{label} 쓰기 대상이 디렉터리가 아님: {path} — 권한을 강등하지 않고 중단합니다."
        )
    probe: Path | None = None
    fd: int | None = None
    last_exists: FileExistsError | None = None
    for _attempt in range(_WRITE_PROBE_NAME_ATTEMPTS):
        candidate = path / _write_probe_name()
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            last_exists = exc
            continue
        except OSError as exc:
            raise delegate_errors.DelegateError(
                f"{label} 쓰기 대상 실측 실패: {path}: {type(exc).__name__}: {exc} — "
                "권한을 read-only 로 강등하지 않고 중단합니다."
            ) from exc
        probe = candidate
        break
    if probe is None:
        raise delegate_errors.DelegateError(
            f"{label} 쓰기 대상 실측 실패: {path}: 프로브 이름이 "
            f"{_WRITE_PROBE_NAME_ATTEMPTS}회 연속 선점됨: {last_exists} — "
            "권한을 read-only 로 강등하지 않고 중단합니다."
        )
    delegate_temp._close_transport_fd(fd)
    try:
        os.unlink(probe)
    except OSError as exc:
        # 프로브 잔재는 attempt tmp 회수가 함께 걷어간다 — 주 결과를 덮지 않고 알리기만 한다.
        delegate_temp._warn_read_tmp_cleanup_failure(probe, exc)


def _read_tmp_write_targets(
    mode: str, read_tmp: delegate_temp._ReadRoleTemp, cluster_run_dir: Path | None,
) -> tuple[tuple[str, Path], ...]:
    """이 실행이 실제로 여는 쓰기 대상 — 샌드박스 권한 결정의 근거다.

    라운드 준비가 있었던 실행의 쓰기 자리는 **묶음 run-dir 전체**다(`ClusterCopyPlan.run_dir`) —
    그 안 티켓 자리 하나가 아니다. 준비가 넘긴 좌표를 그대로 실측하고, 그대로 연다.
    """
    targets: list[tuple[str, Path]] = [("read 역할 임시", read_tmp.writable_path)]
    if mode == "workspace-add-dir" and cluster_run_dir is not None:
        targets.append(("ticket 사본", cluster_run_dir))
    return tuple(targets)


def _apply_read_tmp_argv(
    argv: list[str], harness: str, role: str, read_tmp: delegate_temp._ReadRoleTemp | None,
    cluster_run_dir: Path | None = None,
) -> list[str]:
    """하네스 실측 수단으로 read tmp 하나만 실행 권한 표면에 연결한다."""
    if role not in delegate_run_rules.READ_ROLES or read_tmp is None:
        return argv
    adjusted = list(argv)
    mode = delegate_run_rules._READ_TMP_ARGV_MODE_BY_HARNESS[harness]
    for label, target in _read_tmp_write_targets(mode, read_tmp, cluster_run_dir):
        _probe_writable_target(target, label=label)
    if cluster_run_dir is not None and mode != "workspace-add-dir":
        print(
            f"경고: {harness} {role} ticket 사본은 단일-path write 격리를 "
            "보장하지 못합니다. 역할 규약과 위임 전후 git/touches 감사로 변경 범위를 "
            f"표면화하며 선택한 {harness} target으로 계속 실행합니다.",
            file=sys.stderr,
        )
    if mode == "workspace-add-dir":
        try:
            sandbox_index = adjusted.index("-s")
        except ValueError as exc:
            raise delegate_errors.DelegateError(
                "codex read 역할 argv에 기대한 -s read-only 계약이 없음"
            ) from exc
        if adjusted[sandbox_index + 1:sandbox_index + 2] != ["read-only"]:
            raise delegate_errors.DelegateError(
                "codex read 역할 argv의 sandbox가 read-only가 아님 — workspace/add-dir로 "
                "안전하게 치환하지 않는다"
            )
        adjusted[sandbox_index + 1] = "workspace-write"
        try:
            cwd_index = adjusted.index("-C")
        except ValueError as exc:
            raise delegate_errors.DelegateError("codex read 역할 argv에 -C 실행 root가 없음") from exc
        if cwd_index + 1 >= len(adjusted):
            raise delegate_errors.DelegateError("codex read 역할 argv의 -C 실행 root 값이 없음")
        # workspace-write는 현재 -C를 암묵적 writable root로 추가하므로 실행 root를 attempt tmp로
        # 옮긴다. 라운드 준비가 있었으면 공개 CLI의 --add-dir로 그 묶음 run-dir 하나만 더 연다.
        adjusted[cwd_index + 1] = str(read_tmp.path)
        if cluster_run_dir is not None:
            adjusted = _codex_workspace_dirs(adjusted, (cluster_run_dir,))
    elif mode == "add-dir":
        adjusted += ["--add-dir", str(read_tmp.path)]
    elif mode == "role-agent":
        try:
            agent_index = adjusted.index("--agent")
        except ValueError as exc:
            raise delegate_errors.DelegateError(
                "opencode read 역할 argv에 기대한 --agent <role> 계약이 없음"
            ) from exc
        if adjusted[agent_index + 1:agent_index + 2] != [role]:
            raise delegate_errors.DelegateError(
                "opencode read 역할 argv가 custom 역할 agent와 불일치함 — default agent로 "
                "강등하지 않는다"
            )
    # opencode는 추가-dir 기능이 없어 custom 역할 agent를 그대로 쓰고, ticket-copy reviewer의
    # repo 쓰기 표면은 위 경고와 실행 전후 감사로 loud하게 관리한다.
    return adjusted


def _codex_workspace_dirs(argv: list[str], roots: Sequence[Path]) -> list[str]:
    """공용 전역 `--add-dir` formatter 의 계약 예외를 위임 표면 예외로 옮긴다."""
    try:
        return contract.with_codex_workspace_dirs(argv, [str(root) for root in roots])
    except contract.HarnessContractError as exc:
        raise delegate_errors.DelegateError(str(exc)) from exc


# 원 작업공간을 쓰는 codex 실행에서 git 이 실제로 쓰는 메타데이터 위치. workspace-write 는 `-C`
# 안의 `.git` 과 포인터가 가리키는 git 디렉터리를 읽기 전용으로 보호하므로, 명시적 추가 루트로
# 열지 않으면 `git add` 가 index·objects 쓰기에서 막힌다. linked worktree 만이 아니라 일반 clone·
# submodule 도 같은 조회로 처리한다(형상 판별 대신 git 이 답한 위치를 쓴다).
_GIT_METADATA_QUERY: tuple[str, ...] = (
    "rev-parse", "--path-format=absolute", "--git-common-dir", "--git-dir",
)
_GIT_METADATA_LINE_COUNT = 2


def _git_metadata_write_roots(
    cwd: Path, *, git_run_fn: Callable | None = None,
) -> tuple[Path, ...]:
    """`cwd` 저장소의 common_dir·git_dir 를 실경로로 해소한 쓰기 루트 집합.

    두 경로가 같으면 하나, 다르면 포함 관계여도 둘 다 반환한다. codex 는 `-C` 의 `.git` 포인터가
    가리키는 git_dir 를 바깥 쓰기 루트 안에서도 따로 잠그므로, linked worktree 에서 common_dir 만
    열면 index 쓰기가 막힌다. 부모 경로를 합쳐 범위를 넓히지 않는다. 조회 실패·손상 출력·실재하지 않는 경로는 원인을 담은
    `DelegateError` 로 멈춘다 — 빈 루트로 바꿔 실행을 이어가지 않는다."""
    runner = git_run_fn or subprocess.run
    command = ["git", "-C", str(cwd), *_GIT_METADATA_QUERY]
    shown = " ".join(command)
    try:
        result = runner(
            command, capture_output=True, text=True, encoding="utf-8", errors="strict",
            check=False,
        )
    except (OSError, UnicodeDecodeError, subprocess.SubprocessError) as exc:
        raise delegate_errors.DelegateError(
            f"git 메타데이터 위치 조회 실패(호출 전 중단): {shown}: {exc}"
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or "").strip() or (result.stdout or "").strip() or "원인 미상"
        raise delegate_errors.DelegateError(
            f"git 메타데이터 위치 조회 실패 rc={result.returncode}(호출 전 중단): "
            f"{shown}: {detail}"
        )
    lines = (result.stdout or "").splitlines()
    if len(lines) != _GIT_METADATA_LINE_COUNT or not all(line.strip() for line in lines):
        raise delegate_errors.DelegateError(
            f"git 메타데이터 위치 조회 출력이 두 경로가 아님(호출 전 중단): {shown}: {lines!r}"
        )
    resolved: list[Path] = []
    for line in lines:
        candidate = Path(line)
        if not candidate.is_absolute():
            raise delegate_errors.DelegateError(
                f"git 메타데이터 위치가 절대경로가 아님(호출 전 중단): {shown}: {line!r}"
            )
        try:
            real = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise delegate_errors.DelegateError(
                f"git 메타데이터 위치 해소 실패(호출 전 중단): {line}: {exc}"
            ) from exc
        if not real.is_dir():
            raise delegate_errors.DelegateError(
                f"git 메타데이터 위치가 디렉터리가 아님(호출 전 중단): {real}"
            )
        resolved.append(real)
    common_dir, git_dir = resolved
    if git_dir == common_dir:
        return (common_dir,)
    return (common_dir, git_dir)


def _apply_workspace_git_dirs(argv: list[str], mode: str, source_cwd: Path) -> list[str]:
    """최종 argv 가 원 작업공간을 workspace-write 로 열면 그 git 메타데이터 루트를 더한다.

    판정 입력은 역할 이름이 아니라 최종 sandbox 와 `-C` 실경로다. 읽기 역할의 tmp 재앵커처럼
    실행 좌표가 원 작업공간과 다르거나 read-only 가 남은 실행은 원 git 을 조회하지 않고 그대로
    둔다. 전역 `--add-dir` 형식(`workspace-add-dir`)이 아닌 하네스 argv 는 그대로 반환한다."""
    if mode != "workspace-add-dir":
        return argv
    try:
        _exec_index, global_options = contract.read_codex_global_section(argv)
    except contract.HarnessContractError as exc:
        raise delegate_errors.DelegateError(str(exc)) from exc
    sandbox = dict(global_options).get("-s")
    if sandbox not in contract.CODEX_SANDBOX.values():
        raise delegate_errors.DelegateError(
            f"codex argv 의 sandbox 값이 선언 밖: {sandbox!r}"
        )
    if sandbox != contract.CODEX_SANDBOX["write"]:
        return argv
    # `-C` 는 신규 실행에선 exec 뒤, 재개 실행에선 exec 앞에 온다(재개 하위 명령에 -C 가 없다).
    try:
        cwd_index = argv.index("-C")
    except ValueError as exc:
        raise delegate_errors.DelegateError("codex argv 에 -C 실행 root 가 없음") from exc
    if cwd_index + 1 >= len(argv):
        raise delegate_errors.DelegateError("codex argv 의 -C 실행 root 값이 없음")
    if os.path.realpath(argv[cwd_index + 1]) != os.path.realpath(source_cwd):
        return argv
    return _codex_workspace_dirs(argv, _git_metadata_write_roots(source_cwd))


# 재앵커된 read 실행(현재 codex만·_READ_TMP_REANCHOR_EXEC_ROOT_BY_HARNESS)의 3-절대경로
# preamble — 반려 근거: `--dry-run` 실측에서 합성 프롬프트에 정체성·
# 금지사항·회귀 범위·산출 형식은 실렸지만 해소된 `--cwd` 절대경로가 **한 글자도 없었다**. 엔진이
# `-C`를 빈 tmp로 재앵커해 놓고 실 대상 좌표는 PM 산문에만 맡긴 것은 모델 준수 문제가 아니라
# 엔진 결함이라는 판정이다 — 좌표는 엔진만 아는 기계 사실이므로 엔진이 합성 preamble에 직접
# 싣는다([[mechanize-dont-instruct-llm]]). 실측 3건에서 모델의
# 첫 명령이 `&&`로 묶인 **상대** 명령(`pwd && ls && git rev-parse ...`)이라 격리 root의 빈 pwd에서
# 곧장 죽었다 — 그래서 예시 명령은 전부 `-C <절대경로>`/명시 target 플래그로 줘서 pwd를 몰라도
# 그대로 맞게 한다. `_execute_attempt`(실행)와 `main()` dry-run 미리보기가 이 한 함수를 공유한다
# (값만 다르다 — 실행은 방금 만든 실 read_tmp, dry-run은 부작용 0 인 `_predict_read_tmp_paths`).
def _reanchor_exec_root_preamble(
    cwd: Path, exec_root: Path, writable_path: Path,
) -> str:
    """실행 root가 재앵커된 read 실행에 검토 대상·실행 root·쓰기 경로 절대값을 박아 준다."""
    git_status_cmd = delegate_env.render_shell_command(["git", "-C", str(cwd), "status", "--short"])
    git_diff_cmd = delegate_env.render_shell_command(["git", "-C", str(cwd), "diff", "--cached"])
    git_head_cmd = delegate_env.render_shell_command(["git", "-C", str(cwd), "rev-parse", "HEAD"])
    pytest_cmd = delegate_env.render_shell_command([
        delegate_env._prescribed_interpreter(), "-m", "pytest", str(cwd),
        "-p", "no:cacheprovider", "--basetemp", str(writable_path / "pytest"),
    ])
    return (
        "[격리 실행 좌표 — 엔진 값]\n"
        f"검토 대상(diff·git 상태·회귀 소스) 절대경로: {cwd}\n"
        f"이 프로세스의 실행 root(-C): {exec_root} — 이 경로는 검토 대상이 아니다"
        "(격리를 위해 매번 새로 만드는 빈 디렉터리다 — 검토 대상의 source·tests가 없다).\n"
        "다만 그 자리는 저장소 안의 무시되는 런타임 폴더라 `-C` 없는 git 명령은 검토 대상이 "
        "아닌 저장소로 해소될 수 있다 — git 은 아래처럼 항상 `-C <절대경로>`로 실행하라.\n"
        f"쓰기 가능 임시 경로: {writable_path}\n"
        "pwd를 가정하지 말고 절대경로 플래그로 명령하라 — 예:\n"
        f"  {git_status_cmd}\n"
        f"  {git_diff_cmd}\n"
        f"  {git_head_cmd}\n"
        f"  {pytest_cmd} ...\n"
    )


def _predict_read_tmp_paths(home, harness: str) -> tuple[Path, Path]:
    """dry-run 전용 — `_create_read_role_temp`와 같은 이름 규칙(`_read_tmp_attempt_name`)으로
    대표 경로만 계산한다(attempt 자리는 만들지 않는다 · 공유 임시 루트는 앵커 seam 이 소유한다).
    dry-run 은 실행하지 않으므로 이 값이 이후 실행의 read_tmp와 같은 문자열일 필요는 없다 —
    이번 invocation이 실행됐다면 만들었을 이름 규칙의 진짜 값(placeholder 아님)을 보여주는 게
    목적이다."""
    temp_parent, _optional_parent_name = delegate_temp._read_tmp_parent(home, harness)
    attempt_path = temp_parent / delegate_temp._read_tmp_attempt_name()
    writable_component = delegate_run_rules._READ_TMP_WRITABLE_COMPONENT_BY_HARNESS[harness]
    writable_path = (
        attempt_path / writable_component
        if writable_component is not None else attempt_path
    )
    return attempt_path, writable_path


def _read_tmp_prompt_note(
    harness: str, cwd: Path, read_tmp: delegate_temp._ReadRoleTemp | None,
) -> str:
    """실 경로·회귀 실행법·worktree 금지를 read role이 놓치지 않게 한다."""
    if read_tmp is None:
        return delegate_run_rules.READ_REGRESSION_UNAVAILABLE_NOTE
    pytest_temp = (
        f'"{delegate_env._temp_env_reference()}/{delegate_run_rules._READ_TMP_PYTEST_REL_BY_HARNESS[harness]}"'
    )
    env_note = (
        f"TMPDIR은 권한 glob 기준인 {read_tmp.path}, TMP/TEMP는 실제 쓰기 가능한 "
        f"{read_tmp.writable_path}를 가리킨다"
        if delegate_run_rules._READ_TMP_TMP_TEMP_USE_WRITABLE_PATH_BY_HARNESS[harness]
        else f"TMPDIR/TMP/TEMP가 모두 {read_tmp.path}를 가리킨다"
    )
    codex_note = (
        f" Codex 실행 root도 {read_tmp.path}로 옮겼으므로 worktree 명령은 절대경로를 쓰거나 "
        f"`cd {delegate_env.render_shell_token(str(cwd))}` 뒤 실행하라."
        if delegate_run_rules._READ_TMP_REANCHOR_EXEC_ROOT_BY_HARNESS[harness]
        else ""
    )
    reanchor_preamble = (
        "\n\n" + _reanchor_exec_root_preamble(cwd, read_tmp.path, read_tmp.writable_path)
        if delegate_run_rules._READ_TMP_REANCHOR_EXEC_ROOT_BY_HARNESS[harness]
        else ""
    )
    return (
        f"read 역할 실행용 격리 임시 디렉터리: {read_tmp.writable_path}. "
        f"실행 환경의 {env_note}. 회귀 산출물은 이 경로에만 "
        f"쓰고 worktree({cwd})에는 어떤 파일도 만들거나 수정하지 마라. pytest는 예를 들어 "
        f"`{delegate_env._prescribed_interpreter()} -m pytest -p no:cacheprovider "
        f"--basetemp {pytest_temp} ...`처럼 "
        "cacheprovider를 끄고 basetemp를 지정하라. PYTHONDONTWRITEBYTECODE=1도 설정되어 있다. "
        f"하네스 권한 근거: {harness}.{codex_note}"
        f"{reanchor_preamble}"
    )


# codex read 역할만 `-C`(실행 root)를 격리 tmp로 재앵커한다(_READ_TMP_REANCHOR_EXEC_ROOT_BY_HARNESS
# — claude·opencode는 -C/--dir을 건드리지 않아 이 클래스가 구조적으로 없다). 재앵커된 tmp는 절대
# git 저장소가 아니므로(매 attempt 새로 만드는 빈 0700 디렉터리), 모델은 프롬프트 지시
# (`_read_tmp_prompt_note`의 codex_note)로 `--cwd` 복귀를 기대받는다 — 그 준수를 기계가 보장할
# 수는 없다: `codex exec --help` 실측상 `-C`는 항상 암묵적 쓰기 가능 root이고 `--add-dir`는
# 추가만 한다 — "주 워크스페이스는 read-only·별도 root만 write" 조합이 CLI에 없다(실측 —
# `-c permissions.<name>=` 동적 override 시도도 codex-cli 0.147.0에서 patch 도구가 read-only로
# 남아 이미 실패한 바 있다·위 주석). 그래서 기계가 보장할 수 있는 건 재앵커 **이전**의 `--cwd`
# 자신이 리뷰 가능한 형상인가뿐이다 — 재앵커된 tmp에서 다시 rev-parse해 봐야 항상 실패하는
# 무의미한 검사가 된다. 이 함수는 `--cwd` 를 스폰 전에 검증해 불량 입력을 과금 전에 끊는다
# (실측 3건 — 세 라운드 모두 `--cwd` 자체는 정상이었다·즉 모델이
# 격리 tmp에서 `--cwd`로 되짚어가지 못한 것이 근본 원인이고, 이 preflight는 그 모델 준수까지
# 기계로 보장하진 못한다. 대신 `--cwd` 부실(비-저장소·하위디렉터리 오지정)이라는 인접 실패
# 클래스를 스폰 전에 닫아, 같은 종류의 무의미한 유료 라운드를 줄인다).
#
# 검사는 재앵커를 공유하는 모든 codex read 역할(code-reviewer·researcher)에 같다 — 어떤 역할이든
# `--cwd` 가 실제 저장소 최상위가 아니면 재앵커·`_read_tmp_prompt_note`의 절대경로 안내 자체가
# 잘못된 값을 참조하게 되는, 역할 무관 공통 전제다. 리뷰 입력은 미커밋 작업물이 아니라 묶음
# 브랜치 diff 라(준비 트리 조건이 작업 트리 변경 0 을 요구한다) staged 변경은 요구하지 않는다.


def _preflight_codex_read_exec_root(cwd: Path) -> None:
    """codex read 역할 스폰 **전** `--cwd` 저장소 형상을 기계로 확정한다(호출 없음).

    (a) `--cwd` 가 git 저장소인가 (b) 그 `--show-toplevel` 값이 `--cwd` 자신과 일치하는가
    (저장소 하위 디렉터리가 잘못 `--cwd` 로 넘어온 경우를 잡는다). 하나라도 어긋나면
    `DelegateError` 로 중단한다 — 호출부(`_execute_attempt`)는 이 함수를 `run_fn` 호출
    **이전**에만 부르므로 실패는 과금 없이 끝난다.
    """
    resolved_cwd = cwd.resolve()
    toplevel = subprocess.run(
        ["git", "-C", str(resolved_cwd), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    if toplevel.returncode != 0:
        cause = toplevel.stderr.strip() or toplevel.stdout.strip() or "원인 미상"
        raise delegate_errors.DelegateError(
            "codex read 역할 preflight 실패 — --cwd 가 git 저장소가 아님(호출 전 중단): "
            f"cwd={resolved_cwd} · {cause}"
        )
    resolved_toplevel = Path(toplevel.stdout.strip()).resolve()
    if resolved_toplevel != resolved_cwd:
        raise delegate_errors.DelegateError(
            "codex read 역할 preflight 실패 — 실행 root 불일치(호출 전 중단): "
            f"--cwd={resolved_cwd} · git toplevel={resolved_toplevel} "
            "(--cwd 는 저장소 최상위여야 한다)"
        )


def _apply_read_tmp_env(
    env: dict[str, str], harness: str, read_tmp: delegate_temp._ReadRoleTemp | None,
) -> dict[str, str]:
    """정제된 child env에 회귀 임시 경로만 추가한다."""
    if read_tmp is None:
        return env
    adjusted = dict(env)
    adjusted["TMPDIR"] = str(read_tmp.path)
    temp_value = (
        str(read_tmp.writable_path)
        if delegate_run_rules._READ_TMP_TMP_TEMP_USE_WRITABLE_PATH_BY_HARNESS[harness]
        else str(read_tmp.path)
    )
    adjusted["TMP"] = temp_value
    adjusted["TEMP"] = temp_value
    adjusted["PYTHONDONTWRITEBYTECODE"] = "1"
    return adjusted


def _prepare_attempt_transport(
    home, harness: str, model: str, reasoning: str | None, role: str, cwd: Path,
    prompt: str, resume_session_id: str | None = None,
    cluster_run_dir: Path | None = None,
) -> tuple[
    list[str], str | None, delegate_temp._OpenCodeTransportPrompt | None, delegate_temp._ReadRoleTemp | None,
]:
    """하네스 wire + read tmp를 attempt 단위로 함께 준비한다(timeout/판정은 미소유)."""
    read_role = role in delegate_run_rules.READ_ROLES
    read_tmp = delegate_temp._create_read_role_temp(home, harness, cwd) if read_role else None
    if read_role and cluster_run_dir is not None and read_tmp is None:
        print(
            f"경고: {harness} {role}용 격리 temp를 준비하지 못해 ticket 사본의 "
            "단일-path write 격리를 보장하지 못합니다. 역할 규약과 위임 전후 git/touches "
            f"감사로 변경 범위를 표면화하며 선택한 {harness} target으로 계속 실행합니다.",
            file=sys.stderr,
        )
    prompt_path: delegate_temp._OpenCodeTransportPrompt | None = None
    argv_mode = delegate_run_rules._READ_TMP_ARGV_MODE_BY_HARNESS[harness]
    outgoing_prompt = (
        _read_tmp_prompt_note(harness, cwd, read_tmp) + "\n\n" + prompt
        if read_role else prompt
    )
    try:
        if harness == "opencode":
            # PM 홈 감사 raw와 별개로 wire 사본은 opencode ``--dir`` sandbox 안에 둔다.
            wire_cwd = Path(os.path.abspath(cwd))
            prompt_path = delegate_temp._save_opencode_transport_prompt(wire_cwd, outgoing_prompt)
            # 파일 생성 뒤 argv 조립 직전에도 containment를 재검사한다. runner 직전에는 같은
            # lexical 경로와 부모/파일 entry의 symlink 여부를 한 번 더 확인한다.
            delegate_temp._assert_opencode_transport_path(prompt_path.sandbox, prompt_path)
            return (
                _apply_workspace_git_dirs(_apply_read_tmp_argv(delegate_run._build_target_argv(
                    harness, model, reasoning, role, prompt_path.sandbox, prompt_path,
                ), harness, role, read_tmp, cluster_run_dir), argv_mode, cwd),
                None,
                prompt_path,
                read_tmp,
            )
        return (
            _apply_workspace_git_dirs(_apply_read_tmp_argv(delegate_run._build_target_argv(
                harness, model, reasoning, role, cwd, Path(), resume_session_id,
            ), harness, role, read_tmp, cluster_run_dir), argv_mode, cwd),
            outgoing_prompt,
            None,
            read_tmp,
        )
    except BaseException:
        delegate_temp._cleanup_attempt_transport(prompt_path, read_tmp)
        raise
