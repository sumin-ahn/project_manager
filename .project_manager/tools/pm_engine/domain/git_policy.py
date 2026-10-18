"""git 명령 판정 규칙 (층: domain).

서브커맨드 분류, 멈춤 거부 경우·문구, 사용자 1회 허용 표식, 인자·pathspec 해석, 저장소 조회 없이
정해지는 파괴 형태, 허용 위조 판정, 가장 강한 판정 선택. 판정 흐름(`judge_*`)은 board 가 소유한다.
"""
from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, NamedTuple


# raw git mutation 가드도 board dispatch의 mutation 분류와 같은 원칙(한 표 → 모든 소비처)을 쓴다.
# 하네스별 훅은 이 표를 복제하지 않고 ``judge_git_anchor``/``judge_git_anchor_command``만 호출한다.
_GIT_MUTATION_SUBCOMMANDS: frozenset[str] = frozenset({
    "add", "stage", "commit", "push", "checkout", "restore", "reset",
    "rm", "mv", "clean", "apply", "stash", "branch", "worktree", "switch",
})


# 파괴 형태를 거부하는 대상 정체 — 등록 슬롯 · 그 밖의 PM 홈 소유 linked worktree · 공용 저장소(bare).
_GIT_PROTECTED_IDENTITIES: frozenset[str] = frozenset({"slot", "worktree", "shared-repo"})

# 슬롯 작업 트리·인덱스·HEAD 를 바꾸는 git 쓰기 — 슬롯 주인 판정 대상(`_git_writes_slot`).
_GIT_SLOT_WRITE_SUBCOMMANDS: frozenset[str] = frozenset({
    "add", "stage", "commit", "checkout", "switch", "restore", "reset", "rm", "mv", "merge",
    "rebase", "cherry-pick", "revert", "stash", "pull", "am", "apply", "clean",
    "sparse-checkout", "submodule",
})

# 멈춤 거부 deny 표식 — 손 git 파괴 형태·슬롯/공용 저장소 파일 삭제·허용 위조·남의 슬롯 쓰기. 셸 파서의 조건/cwd
# 불확실 강등(deny→warn)은 이 표식이 붙은 판정을 낮추지 않는다 — 대상을 못 정한 호출은 이미
# 슬롯·공용 저장소일 수 있다고 보고 거부한 결과다.
GIT_DESTRUCTIVE_DENY_CODE = "git-destructive-form"

# 멈춤 거부 문구의 첫 줄. 다음 줄은 경우(`_GIT_GUARD_CASE_*`)마다 행동 하나다.
GIT_GUARD_STOP_LINE = "다른 명령으로 같은 일을 하지 마라. 여기서 멈추고 사용자에게 물어라."

# 거부 경우 — 다음 행동을 고르는 키. 사용자 1회 허용으로 풀 수 있는 경우는 ASK_USER 하나다.
_GIT_GUARD_CASE_FOLDER_UNKNOWN = "folder-unknown"

_GIT_GUARD_CASE_LITERAL_ARGUMENTS = "literal-arguments"

_GIT_GUARD_CASE_BRANCH_CREATE = "branch-create"

_GIT_GUARD_CASE_SLOT_REMOVE = "slot-remove"

_GIT_GUARD_CASE_ASK_USER = "ask-user"

_GIT_GUARD_CASE_GRANT_FORGERY = "grant-forgery"

_GIT_GUARD_CASE_SLOT_NO_TASK = "slot-no-task"

_GIT_GUARD_CASE_SLOT_NOT_OWNED = "slot-not-owned"

# 열린 todo 없이 파일을 고치려는 경우 — 무엇을 하는 중인지 장부에 없으면 다음 세션도 사용자도
# 그 일을 못 본다. 등록 경로(`pm-config task todo add`)는 셸 명령이라 이 판정을 타지 않는다.
_GIT_GUARD_CASE_NO_OPEN_TODO = "no-open-todo"

# 사용자 1회 허용 입력 한 줄 — `git 허용: <명령 원문>`. 사용자 프롬프트 훅만 이 줄로 허용 기록을
# 만든다. 도구 호출 입력에 이 표식이나 허용 기록 폴더 이름이 있으면 위조 시도로 거부한다.
GIT_GRANT_LINE = re.compile(r"^[ \t]*git[ \t]+허용[ \t]*:[ \t]*(.*?)[ \t]*$", re.MULTILINE)

_GIT_GRANT_MARKER = re.compile(r"git[ \t]+허용[ \t]*:")

GIT_GRANT_DIR_NAME = "git-grants"

_GIT_ANCHOR_DENY_ROOTS: frozenset[str] = frozenset({"tests", "templates"})


class _GitInvocation(NamedTuple):
    """정규화한 git 호출. ``cwd``는 global ``-C``를 적용한 실제 명령 앵커다."""

    cwd: Path
    subcommand: str
    args: tuple[str, ...]
    anchor_certain: bool
    # global ``-c <키>=<값>`` 들 — alias 풀이가 대상 저장소 설정과 함께 읽는다(git 과 같은 우선순위).
    config: tuple[str, ...]


def _git_operand_pathspecs(
    args: Sequence[str], *, value_options: frozenset[str],
    short_value_options: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    """option 값을 건너뛰고 명시 operand만 반환한다.

    ``--pathspec-from-file``은 파일 *내용*이 pathspec이고 옵션 값은 그 파일 경로일 뿐이다. 훅이
    파일 내용을 읽어 shell 실행 시점 의미를 재현하는 것은 TOCTOU라, 이 축은 통째로 fail-open한다.
    """
    values = list(args)
    if any(
        value in {"--pathspec-from-file", "--pathspec-file-nul"}
        or value.startswith("--pathspec-from-file=")
        for value in values
    ):
        return ()
    operands: list[str] = []
    i = 0
    while i < len(values):
        value = values[i]
        if value == "--":
            operands.extend(values[i + 1:])
            break
        if value in value_options or value in short_value_options:
            i += 2
            continue
        if any(value.startswith(option + "=") for option in value_options if option.startswith("--")):
            i += 1
            continue
        if value.startswith("-") and not value.startswith("--"):
            # Git은 short option을 묶을 수 있다(``git commit -am msg``). 묶음 안에 값 소비
            # option이 있고 그 option 뒤 suffix가 없으면 다음 토큰이 option 값이다.
            consuming = {option[1:] for option in short_value_options if len(option) == 2}
            cluster = value[1:]
            consume_next = any(
                char in consuming and position == len(cluster) - 1
                for position, char in enumerate(cluster)
            )
            i += 2 if consume_next else 1
            continue
        if any(value.startswith(option) and value != option for option in short_value_options):
            i += 1
            continue
        if value.startswith("-"):
            i += 1
            continue
        operands.append(value)
        i += 1
    return tuple(operands)


def _git_pathspecs(subcommand: str, args: Sequence[str]) -> tuple[str, ...]:
    """deny 판정에 쓸 명시 pathspec만 추출한다.

    checkout/reset은 ref와 path 문법이 겹쳐 ``--`` 뒤만 신뢰한다. add/stage·restore·commit은
    Git 문법상 non-option operand가 pathspec이므로 option arity를 걷어 ``--`` 없는 명시 경로도 받는다.
    """
    values = list(args)
    if subcommand == "apply":
        # operand는 target pathspec이 아니라 읽을 patch 파일이다.
        return ()
    if "--" in values:
        return tuple(values[values.index("--") + 1:])
    if subcommand in {"checkout", "reset", "push"}:
        return ()
    if subcommand in {"add", "stage"}:
        return _git_operand_pathspecs(
            values,
            value_options=frozenset({"--chmod", "--pathspec-from-file"}),
        )
    if subcommand in {"rm", "mv", "clean"}:
        return _git_operand_pathspecs(
            values,
            value_options=frozenset(
                {"--pathspec-from-file", "--exclude"} if subcommand == "clean"
                else {"--pathspec-from-file"}
            ),
            short_value_options=frozenset({"-e"}) if subcommand == "clean" else frozenset(),
        )
    if subcommand == "restore":
        return _git_operand_pathspecs(
            values,
            value_options=frozenset({"--source", "--pathspec-from-file"}),
            short_value_options=frozenset({"-s"}),
        )
    if subcommand == "commit":
        return _git_operand_pathspecs(
            values,
            value_options=frozenset({
                "--author", "--date", "--file", "--fixup", "--gpg-sign", "--message",
                "--pathspec-from-file", "--reedit-message", "--reuse-message", "--template",
                "--trailer",
            }),
            short_value_options=frozenset({"-C", "-F", "-S", "-c", "-m", "-t"}),
        )
    return ()


class _GitArguments(NamedTuple):
    """서브커맨드 인자를 git parse-options 규칙으로 푼 결과."""

    # (옵션 이름, 값) — 묶음(``-fd``)은 낱개로, ``--옵션=값``·값 소비 옵션은 값과 함께 푼다.
    options: tuple[tuple[str, str | None], ...]
    operands: tuple[str, ...]
    # ``--`` 뒤 경로. ``--`` 가 없으면 None.
    paths: tuple[str, ...] | None


def _git_arguments(
    args: Sequence[str], *, short_values: str = "",
    long_values: frozenset[str] = frozenset(),
    stop_after_operands: int | None = None,
) -> _GitArguments:
    options: list[tuple[str, str | None]] = []
    operands: list[str] = []
    paths: tuple[str, ...] | None = None
    values = list(args)
    i = 0
    while i < len(values):
        token = values[i]
        if token == "--":
            paths = tuple(values[i + 1:])
            break
        if token.startswith("--"):
            name, separator, value = token.partition("=")
            if not separator and name in long_values and i + 1 < len(values):
                i += 1
                options.append((name, values[i]))
            else:
                options.append((name, value if separator else None))
        elif token.startswith("-") and len(token) > 1:
            cluster = token[1:]
            for position, char in enumerate(cluster):
                if char not in short_values:
                    options.append((f"-{char}", None))
                    continue
                # 값 소비 옵션은 묶음의 나머지를, 나머지가 없으면 다음 토큰을 값으로 삼는다.
                rest = cluster[position + 1:]
                if not rest and i + 1 < len(values):
                    i += 1
                    rest = values[i]
                options.append((f"-{char}", rest or None))
                break
        else:
            operands.append(token)
            if stop_after_operands is not None and len(operands) == stop_after_operands:
                # 실행 명령 시작 뒤 인자는 Git 옵션이 아니라 payload다.
                operands.extend(values[i + 1:])
                break
        i += 1
    return _GitArguments(tuple(options), tuple(operands), paths)


def _git_option_given(arguments: _GitArguments, *names: str) -> bool:
    """옵션이 주어졌는지. git 처럼 긴 옵션의 모호하지 않은 앞부분(``--har``)도 같은 옵션으로 본다.

    앞부분이 여러 옵션에 걸리면 git 자체가 모호함으로 실패하므로 파괴 쪽으로 읽어도 실행되는 일이 없다.
    """
    for given, _value in arguments.options:
        for name in names:
            if given == name or (
                name.startswith("--") and len(given) > 2 and name.startswith(given)
            ):
                return True
    return False


class _GitDestructiveForm(NamedTuple):
    label: str
    # 거부 경우(`_GIT_GUARD_CASE_*`) — 거부 문구의 다음 행동과 1회 허용 가능 여부를 정한다.
    case: str


_GIT_BRANCH_CREATE = _GitDestructiveForm("브랜치 만들기", _GIT_GUARD_CASE_BRANCH_CREATE)

_GIT_BRANCH_DELETE = _GitDestructiveForm("브랜치 지우기", _GIT_GUARD_CASE_ASK_USER)

_GIT_BRANCH_RENAME = _GitDestructiveForm("브랜치 이름 바꾸기", _GIT_GUARD_CASE_ASK_USER)

_GIT_DISCARD_CHANGES = _GitDestructiveForm("변경 버리기", _GIT_GUARD_CASE_ASK_USER)

_GIT_REMOTE_OVERWRITE = _GitDestructiveForm("원격 덮어쓰기", _GIT_GUARD_CASE_ASK_USER)

_GIT_REF_REWRITE = _GitDestructiveForm(
    "브랜치 ref 직접 지우기·옮기기(저수준 명령)", _GIT_GUARD_CASE_ASK_USER,
)


def _git_branch_form(
    args: Sequence[str], ref_anchor: Path | None, runner: Any,
) -> _GitDestructiveForm | None:
    parsed = _git_arguments(
        args, short_values="u",
        long_values=frozenset({"--set-upstream-to", "--sort", "--format", "--points-at"}),
    )
    if _git_option_given(parsed, "-d", "-D", "--delete"):
        return _GIT_BRANCH_DELETE
    if _git_option_given(parsed, "-m", "-M", "--move"):
        return _GIT_BRANCH_RENAME
    if _git_option_given(parsed, "-c", "-C", "--copy"):
        return _GIT_BRANCH_CREATE
    # git 은 --list·필터 옵션이 있거나 operand 가 없을 때만 목록 모드다(``-v``·``-a`` 는 아님).
    listing = _git_option_given(
        parsed, "-l", "--list", "--contains", "--no-contains", "--merged", "--no-merged",
        "--points-at",
    )
    other_mode = _git_option_given(
        parsed, "-u", "--set-upstream-to", "--unset-upstream", "--edit-description",
        "--show-current",
    )
    return _GIT_BRANCH_CREATE if parsed.operands and not listing and not other_mode else None


def _git_reset_form(
    args: Sequence[str], ref_anchor: Path | None, runner: Any,
) -> _GitDestructiveForm | None:
    parsed = _git_arguments(args, long_values=frozenset({"--pathspec-from-file"}))
    return _GIT_DISCARD_CHANGES if _git_option_given(parsed, "--hard") else None


def _git_restore_form(
    args: Sequence[str], ref_anchor: Path | None, runner: Any,
) -> _GitDestructiveForm | None:
    parsed = _git_arguments(
        args, short_values="s", long_values=frozenset({"--source", "--pathspec-from-file"}),
    )
    staged_only = (
        _git_option_given(parsed, "-S", "--staged")
        and not _git_option_given(parsed, "-W", "--worktree")
    )
    return None if staged_only else _GIT_DISCARD_CHANGES


def _git_clean_form(
    args: Sequence[str], ref_anchor: Path | None, runner: Any,
) -> _GitDestructiveForm | None:
    # ``-f`` 없는 clean 도 ``clean.requireForce=false`` 설정이면 지운다 — 미리보기(-n)만 통과.
    parsed = _git_arguments(args, short_values="e", long_values=frozenset({"--exclude"}))
    return None if _git_option_given(parsed, "-n", "--dry-run") else _GIT_DISCARD_CHANGES


def _git_stash_form(
    args: Sequence[str], ref_anchor: Path | None, runner: Any,
) -> _GitDestructiveForm | None:
    parsed = _git_arguments(
        args, short_values="m", long_values=frozenset({"--message", "--pathspec-from-file"}),
    )
    action = parsed.operands[0] if parsed.operands else ""
    return _GIT_DISCARD_CHANGES if action in {"drop", "clear"} else None


def _git_push_form(
    args: Sequence[str], ref_anchor: Path | None, runner: Any,
) -> _GitDestructiveForm | None:
    parsed = _git_arguments(
        args, short_values="o",
        long_values=frozenset({"--repo", "--receive-pack", "--exec", "--push-option"}),
    )
    if _git_option_given(parsed, "-f", "--force", "--force-with-lease", "--mirror"):
        return _GIT_REMOTE_OVERWRITE
    if any(operand.startswith("+") for operand in parsed.operands):
        return _GIT_REMOTE_OVERWRITE
    return None


def _git_update_ref_form(
    args: Sequence[str], ref_anchor: Path | None, runner: Any,
) -> _GitDestructiveForm | None:
    # ``update-ref [-d] <ref> …`` 이 로컬 브랜치(refs/heads/·HEAD)를 지우거나 옮기면 `branch -D`·
    # `reset` 과 같은 결과다. ``--stdin`` 은 명령 목록을 입력으로 받아 대상을 정적으로 볼 수 없다.
    parsed = _git_arguments(args, short_values="m", long_values=frozenset({"--message"}))
    if _git_option_given(parsed, "--stdin"):
        return _GIT_REF_REWRITE
    ref = parsed.operands[0] if parsed.operands else ""
    return _GIT_REF_REWRITE if ref == "HEAD" or ref.startswith("refs/heads/") else None


def _git_symbolic_ref_form(
    args: Sequence[str], ref_anchor: Path | None, runner: Any,
) -> _GitDestructiveForm | None:
    # 이름 하나만 주면 읽기다. 대상까지 주거나 ``-d`` 면 HEAD 가 가리키는 브랜치를 바꾸거나 지운다.
    parsed = _git_arguments(args, short_values="m", long_values=frozenset({"--message"}))
    if _git_option_given(parsed, "-d", "--delete") or len(parsed.operands) > 1:
        return _GIT_REF_REWRITE
    return None


def _git_checkout_index_form(
    args: Sequence[str], ref_anchor: Path | None, runner: Any,
) -> _GitDestructiveForm | None:
    parsed = _git_arguments(args, long_values=frozenset({"--prefix", "--stage"}))
    return _GIT_DISCARD_CHANGES if _git_option_given(parsed, "-f", "--force") else None


def _git_read_tree_form(
    args: Sequence[str], ref_anchor: Path | None, runner: Any,
) -> _GitDestructiveForm | None:
    parsed = _git_arguments(
        args, long_values=frozenset({"--prefix", "--index-output", "--exclude-per-directory"}),
    )
    return _GIT_DISCARD_CHANGES if _git_option_given(parsed, "--reset") else None


# ── 사용자 1회 허용 ────────────────────────────────────────────────────────────────────
# 멈춤 거부 중 사용자에게 물어 실행할 수 있는 경우(ASK_USER)는 사용자가 `git 허용: <명령 원문>`
# 한 줄을 보내면 그 명령 한 번만 통과한다. 허용 기록은 사용자 프롬프트 훅만 만든다
# (`record_git_grants`). 기록은 (세션·실행 폴더·정규화 argv) 에 묶이고, 가드가 정확히 같은 호출을
# 만나면 파일을 이름 바꾸기로 선점해 지운 뒤 통과시킨다 — 두 번째 호출은 기록이 없어 거부된다.
_GIT_GUARD_ACTION_LINES: dict[str, str] = {
    _GIT_GUARD_CASE_FOLDER_UNKNOWN: (
        "대상 폴더를 절대경로로 적어 다시 실행한다(git 은 `git -C <절대경로> …`)."
    ),
    _GIT_GUARD_CASE_LITERAL_ARGUMENTS: "셸 변수·명령 치환을 값 그대로 바꿔 적어 다시 실행한다.",
    _GIT_GUARD_CASE_BRANCH_CREATE: (
        "브랜치는 `pm-config task branch <명의> <슬롯> <브랜치> --from <부모>` 로 만든다."
    ),
    _GIT_GUARD_CASE_SLOT_REMOVE: (
        "사용자에게 슬롯 제거 승인을 묻고, 승인하면 "
        "`pm-config worktree remove <slot> --user-ack <slot>` 로 지운다."
    ),
    _GIT_GUARD_CASE_GRANT_FORGERY: (
        "허용 줄·허용 기록은 사용자 입력으로만 만든다. 필요하면 사용자에게 "
        "`git 허용: <명령 원문>` 입력을 요청한다."
    ),
    _GIT_GUARD_CASE_SLOT_NO_TASK: (
        "이 세션에 열린 task 가 없다. `pm_bootstrap.py --task <task>` 으로 task 를 연 뒤 그 task 가 잡은 "
        "슬롯에서 한다."
    ),
    _GIT_GUARD_CASE_SLOT_NOT_OWNED: (
        "이 세션 task 가 잡은 슬롯에서만 쓴다. 잡은 슬롯이 없으면 `pm-config alloc <repo> --task <task>` "
        "으로 잡는다."
    ),
    _GIT_GUARD_CASE_NO_OPEN_TODO: (
        "지금 하는 일을 먼저 등록한다 — `pm-config task todo add <task> <할 일 한 줄>`. 목록은 "
        "`pm-config task todo list <task>`, 끝낸 것은 `pm-config task todo done <task> <ID>`."
    ),
}

_GIT_GUARD_ASK_WITH_GRANT = (
    "사용자에게 이 명령을 실행할지 묻는다. 허용하면 사용자가 `git 허용: <명령 원문>` 한 줄을 "
    "보낸 뒤 같은 명령을 그대로 한 번 다시 실행한다."
)

_GIT_GUARD_ASK_WITHOUT_GRANT = (
    "사용자에게 이 명령을 실행할지 묻는다. 이 하네스에는 1회 허용 수단이 없으므로, 허용하면 "
    "사용자가 터미널에서 직접 실행한다."
)


class GitGrantShapeError(ValueError):
    """허용 명령 원문이 셸 확장 없는 한 명령 단독 호출이 아니다."""


def _git_guard_stop_reason(
    cases: Sequence[str], detail: str, *, grants_available: bool, notes: Sequence[str] = (),
) -> str:
    """멈춤 지시 첫 줄 + 경우별 다음 행동 한 줄씩 + 추가 사유 + 거부한 호출."""
    lines = [GIT_GUARD_STOP_LINE]
    for case in dict.fromkeys(cases):
        if case == _GIT_GUARD_CASE_ASK_USER:
            lines.append(
                _GIT_GUARD_ASK_WITH_GRANT if grants_available else _GIT_GUARD_ASK_WITHOUT_GRANT
            )
        else:
            lines.append(_GIT_GUARD_ACTION_LINES[case])
    lines.extend(notes)
    lines.append(f"거부한 호출: {detail}")
    return "\n".join(lines)


def _git_grant_forgery_judgment(detail: str) -> dict[str, str]:
    return {
        "verdict": "deny", "cwd_identity": "non-repo", "code": GIT_DESTRUCTIVE_DENY_CODE,
        "case": _GIT_GUARD_CASE_GRANT_FORGERY, "reason": detail,
    }


def _git_grant_forgery_in_command(command: str) -> dict[str, str] | None:
    """셸 명령에 허용 표식·허용 기록 폴더가 있으면 위조 시도 판정(파일 쓰기·중첩 하네스 프롬프트)."""
    text = command.replace("\\", "/").replace("'", "").replace('"', "")
    if GIT_GRANT_DIR_NAME in text or _GIT_GRANT_MARKER.search(text):
        return _git_grant_forgery_judgment(
            f"셸 명령에 허용 표식(`git 허용:`) 또는 허용 기록 폴더(`{GIT_GRANT_DIR_NAME}`)가 있음"
        )
    return None


def _tool_input_strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _tool_input_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _tool_input_strings(item)


def _git_writes_slot(subcommand: str, args: Sequence[str]) -> bool:
    """git 호출이 작업 트리·인덱스·HEAD 를 바꾸는지 — 보기만 하는 형태(stash list·show · clean -n ·
    apply --check·--stat)는 쓰기가 아니다."""
    if subcommand not in _GIT_SLOT_WRITE_SUBCOMMANDS:
        return False
    if subcommand in {"sparse-checkout", "submodule"}:
        leading = _git_arguments(args, long_values=frozenset({"--path"}), stop_after_operands=1)
        foreach = subcommand == "submodule" and leading.operands[:1] == ("foreach",)
        arguments = _git_arguments(
            args, long_values=frozenset({"--path"}),
            stop_after_operands=2 if foreach else None,
        )
        if _git_option_given(arguments, "-h", "--help"):
            return False
        operation = arguments.operands[0] if arguments.operands else None
        if subcommand == "sparse-checkout":
            return operation not in {None, "list", "check-rules", "help"}
        return operation not in {None, "status", "summary", "help"}
    if subcommand == "stash":
        return not args or args[0] not in {"list", "show"}
    arguments = _git_arguments(args)
    if subcommand == "clean":
        return not _git_option_given(arguments, "-n", "--dry-run")
    if subcommand == "apply":
        return _git_option_given(arguments, "--apply") or not _git_option_given(
            arguments, "--check", "--stat", "--numstat", "--summary",
        )
    return True


def _strongest_git_guard_judgment(judgments: Sequence[dict[str, str]]) -> dict[str, str]:
    """가장 강한 판정 하나로 접는다 — 여럿이면 같은 판정 호출들의 사유를 번호와 함께 잇는다."""
    rank = {"ok": 0, "warn": 1, "deny": 2}
    strongest = max(judgments, key=lambda item: rank[item["verdict"]])
    if len(judgments) == 1:
        return strongest
    strongest_judgments = [
        (call, item)
        for call, item in enumerate(judgments, 1)
        if item["verdict"] == strongest["verdict"]
    ]
    rendered: list[str] = []
    index = 0
    while index < len(strongest_judgments):
        start_call, item = strongest_judgments[index]
        key = (item["cwd_identity"], item["verdict"], item["reason"])
        end = index + 1
        while end < len(strongest_judgments):
            candidate_call, candidate = strongest_judgments[end]
            if (
                candidate_call != strongest_judgments[end - 1][0] + 1
                or (candidate["cwd_identity"], candidate["verdict"], candidate["reason"])
                != key
            ):
                break
            end += 1
        end_call = strongest_judgments[end - 1][0]
        if end - index == 1:
            rendered.append(
                f"호출 {start_call} [{item['cwd_identity']}/{item['verdict']}]: "
                f"{item['reason']}"
            )
        else:
            rendered.append(
                f"호출 {start_call}–{end_call} "
                f"[{item['cwd_identity']}/{item['verdict']}] ×{end - index}: "
                f"{item['reason']}"
            )
        index = end
    reasons = " | ".join(rendered)
    result = {**strongest, "reason": reasons}
    denies = [item for item in judgments if item["verdict"] == "deny"]
    if denies and not all(
        item.get("code") == "pm-home-relative-pytest-tests-missing" for item in denies
    ):
        result.pop("code", None)
    return result
