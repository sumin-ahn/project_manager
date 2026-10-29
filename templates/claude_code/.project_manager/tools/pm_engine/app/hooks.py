"""도구 호출 판정 보조 (층: app).

git 호출 정규화·alias 풀이, 저장소 조회가 필요한 파괴 형태, 간접 git·삭제 호출, 슬롯 쓰기 주인,
사용자 1회 허용 기록·소비, 엔진 사본 신선도, 판정 마무리. 판정 흐름(`judge_*`)과 어댑터 진입 함수는
board 가 소유한다.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pm_engine.domain import git_policy, session_identity, shell_policy
from pm_engine.infra import fs, git, process


# 허용 명령 원문에 둘 수 없는 문자 — 셸 연산자·리다이렉션·확장(변수·명령 치환·glob·틸드·중괄호
# 전개)·주석. 이런 글자가 없어야 argv 정규화(shlex)와 실제 셸 실행의 의미가 같다.
_GIT_GRANT_UNSAFE_TEXT = re.compile(
    r"[;&|<>()$`*?\[\r\n]|(?:^|[\s=:])~|\{[^}]*(?:,|\.\.)[^}]*\}|(?:^|\s)#"
)

# `,` 뒤따름은 인터프리터 코드의 argv 목록(``['git', 'branch', …]``)에서 따옴표를 걷은 형태다.
_GIT_COMMAND_PREFILTER = re.compile(r"(?<![A-Za-z0-9_.-])['\"]?git['\"]?(?=\s|$|[<>,])")


def _git_invocation(cwd: str | os.PathLike[str], argv: Sequence[str]) -> git_policy._GitInvocation | None:
    """git argv에서 global option/``-C``를 걷고 실제 subcommand를 해소한다.

    모르는 global option은 보수적으로 한 토큰 option으로 취급하고 뒤의 첫 non-option을
    subcommand로 삼는다. ``GIT_DIR``/``GIT_WORK_TREE``·``--git-dir``/``--work-tree``·셸 변수가 든
    ``-C`` 는 실제 대상을 정적으로 정하지 못하므로 ``anchor_certain=False`` 다. 그런 호출은 파괴
    형태일 때만 거부하고(`judge_git_anchor`) 그 밖에는 warn 으로 흐른다. ``git -C``는 cwd 오인
    판정의 일부라 반복 지정도 순서대로 적용한다.
    """
    tokens = [str(value) for value in argv]
    if not tokens:
        return None
    try:
        start = next(i for i, token in enumerate(tokens) if Path(token).name == "git")
    except StopIteration:
        return None
    anchor = Path(cwd).expanduser()
    anchor_certain = not any(
        token.startswith(("GIT_WORK_TREE=", "GIT_DIR=")) for token in tokens[:start]
    )
    config: list[str] = []
    i = start + 1
    value_options = {"-c", "--config-env", "--git-dir", "--work-tree", "--namespace"}
    while i < len(tokens):
        token = tokens[i]
        if token == "-C":
            if i + 1 >= len(tokens):
                return git_policy._GitInvocation(anchor, "", tuple(), anchor_certain, tuple(config))
            if any(ch in tokens[i + 1] for ch in "$`"):
                anchor_certain = False
            target = Path(tokens[i + 1]).expanduser()
            anchor = target if target.is_absolute() else anchor / target
            i += 2
            continue
        if token in value_options:
            if token in {"--git-dir", "--work-tree"}:
                anchor_certain = False
            if token == "-c" and i + 1 < len(tokens):
                config.append(tokens[i + 1])
            i += 2
            continue
        if token.startswith("--git-dir=") or token.startswith("--work-tree=") or token.startswith("--namespace="):
            if token.startswith(("--git-dir=", "--work-tree=")):
                anchor_certain = False
            i += 1
            continue
        if token == "--":
            i += 1
            break
        if token.startswith("-"):
            i += 1
            continue
        break
    if i >= len(tokens):
        return git_policy._GitInvocation(anchor.resolve(), "", tuple(), anchor_certain, tuple(config))
    return git_policy._GitInvocation(
        anchor.resolve(), tokens[i], tuple(tokens[i + 1:]), anchor_certain, tuple(config),
    )


_GIT_SLOT_REMOVE = git_policy._GitDestructiveForm("슬롯 지우기", git_policy._GIT_GUARD_CASE_SLOT_REMOVE)

_GIT_SLOT_MOVE = git_policy._GitDestructiveForm("슬롯 옮기기", git_policy._GIT_GUARD_CASE_ASK_USER)

_GIT_UNRESOLVED_ARGUMENTS = git_policy._GitDestructiveForm(
    "셸 변수·명령 치환이 든 인자(형태를 정적으로 풀 수 없음)", git_policy._GIT_GUARD_CASE_LITERAL_ARGUMENTS,
)

_GIT_UNRESOLVED_REF = git_policy._GitDestructiveForm(
    "대상 저장소를 정할 수 없어 기존 브랜치·커밋 전환인지 확인할 수 없음",
    git_policy._GIT_GUARD_CASE_FOLDER_UNKNOWN,
)

_GIT_UNKNOWN_CHECKOUT_NAME = git_policy._GitDestructiveForm(
    "기존 커밋이 아닌 이름 — 경로 복원(변경 버리기) 또는 원격 추적 브랜치로 브랜치 만들기",
    git_policy._GIT_GUARD_CASE_ASK_USER,
)

_GIT_UNKNOWN_LOCAL_BRANCH = git_policy._GitDestructiveForm(
    "로컬 브랜치가 아닌 이름 — 원격 추적 브랜치로 브랜치 만들기", git_policy._GIT_GUARD_CASE_BRANCH_CREATE,
)


def _git_checkout_form(
    args: Sequence[str], ref_anchor: Path | None, runner: Any,
) -> git_policy._GitDestructiveForm | None:
    parsed = git_policy._git_arguments(
        args, short_values="bB", long_values=frozenset({"--orphan", "--pathspec-from-file"}),
    )
    if git_policy._git_option_given(parsed, "-b", "-B", "--orphan", "-t", "--track"):
        return git_policy._GIT_BRANCH_CREATE
    if git_policy._git_option_given(parsed, "-f", "--force", "-p", "--patch", "--pathspec-from-file"):
        return git_policy._GIT_DISCARD_CHANGES
    if parsed.paths or len(parsed.operands) > 1:
        return git_policy._GIT_DISCARD_CHANGES
    if not parsed.operands or parsed.operands[0] == "-":
        return None
    if ref_anchor is None:
        return _GIT_UNRESOLVED_REF
    # 커밋으로 풀리는 이름만 전환이다. 그 밖은 git 이 경로 복원이나 원격 추적 브랜치 추측 생성으로 읽는다.
    if git._git_commit_exists(ref_anchor, parsed.operands[0], runner=runner):
        return None
    return _GIT_UNKNOWN_CHECKOUT_NAME


def _git_switch_form(
    args: Sequence[str], ref_anchor: Path | None, runner: Any,
) -> git_policy._GitDestructiveForm | None:
    parsed = git_policy._git_arguments(
        args, short_values="cC",
        long_values=frozenset({"--create", "--force-create", "--orphan"}),
    )
    if git_policy._git_option_given(
        parsed, "-c", "-C", "--create", "--force-create", "--orphan", "-t", "--track",
    ):
        return git_policy._GIT_BRANCH_CREATE
    if git_policy._git_option_given(parsed, "-f", "--force", "--discard-changes"):
        return git_policy._GIT_DISCARD_CHANGES
    if git_policy._git_option_given(parsed, "-d", "--detach") or not parsed.operands:
        return None
    if parsed.operands[0] == "-":
        return None
    if ref_anchor is None:
        return _GIT_UNRESOLVED_REF
    if git._git_local_branch_exists(ref_anchor, parsed.operands[0], runner=runner):
        return None
    return _GIT_UNKNOWN_LOCAL_BRANCH


def _git_worktree_form(
    args: Sequence[str], ref_anchor: Path | None, runner: Any,
) -> git_policy._GitDestructiveForm | None:
    parsed = git_policy._git_arguments(
        args, short_values="bB", long_values=frozenset({"--orphan", "--reason", "--expire"}),
    )
    action = parsed.operands[0] if parsed.operands else ""
    if action == "remove":
        return _GIT_SLOT_REMOVE
    if action == "move":
        return _GIT_SLOT_MOVE
    if action != "add":
        return None
    if git_policy._git_option_given(parsed, "-b", "-B", "--orphan"):
        return git_policy._GIT_BRANCH_CREATE
    rest = parsed.operands[1:]
    if git_policy._git_option_given(parsed, "-d", "--detach") or not rest:
        return None
    if ref_anchor is None:
        return _GIT_UNRESOLVED_REF
    # commit-ish 가 커밋으로 풀리지 않으면 원격 추적 브랜치 추측 생성이고, 생략하면 경로 끝 이름의
    # 브랜치를 쓰되 없으면 새로 만든다.
    if len(rest) > 1:
        if git._git_commit_exists(ref_anchor, rest[1], runner=runner):
            return None
        return _GIT_UNKNOWN_LOCAL_BRANCH
    if git._git_local_branch_exists(ref_anchor, Path(rest[0]).name, runner=runner):
        return None
    return git_policy._GIT_BRANCH_CREATE


# 브랜치·작업 변경·슬롯·원격을 되돌리기 어렵게 바꿀 수 있는 서브커맨드. 브랜치와 슬롯은 엔진만
# 만들고 지우므로, 이 안의 파괴 형태(`_git_destructive_form`)는 슬롯·공용 저장소 대상에서 거부한다.
# 저수준 명령(update-ref·symbolic-ref·checkout-index·read-tree)도 같은 결과를 내므로 함께 본다.
_GIT_DESTRUCTIVE_FORM_JUDGES = {
    "branch": git_policy._git_branch_form, "checkout": _git_checkout_form, "switch": _git_switch_form,
    "reset": git_policy._git_reset_form, "restore": git_policy._git_restore_form, "clean": git_policy._git_clean_form,
    "stash": git_policy._git_stash_form, "worktree": _git_worktree_form, "push": git_policy._git_push_form,
    "update-ref": git_policy._git_update_ref_form, "symbolic-ref": git_policy._git_symbolic_ref_form,
    "checkout-index": git_policy._git_checkout_index_form, "read-tree": git_policy._git_read_tree_form,
}


def _git_destructive_form(
    subcommand: str, args: Sequence[str], *, ref_anchor: Path | None, runner: Any,
) -> git_policy._GitDestructiveForm | None:
    """파괴 가능 서브커맨드 호출이 파괴 형태인지 git 의미로 판정한다.

    ``ref_anchor`` 는 기존 브랜치·커밋 여부를 물을 저장소다. 대상을 정적으로 못 정한 호출은 None 을
    넘기고, 그 조회가 필요한 형태는 확인할 수 없으므로 파괴 형태로 본다.
    """
    judge = _GIT_DESTRUCTIVE_FORM_JUDGES.get(subcommand)
    if judge is None:
        return None
    if any(ch in value for value in args for ch in "$`"):
        return _GIT_UNRESOLVED_ARGUMENTS
    return judge(tuple(args), ref_anchor, runner)


def _git_alias_resolved(invocation: git_policy._GitInvocation, *, runner: Any) -> git_policy._GitInvocation:
    """git alias 를 대상 저장소 설정(+ global ``-c``)으로 풀어 실제 서브커맨드 호출을 돌려준다.

    가드 표의 서브커맨드는 git 명령이라 alias 로 가릴 수 없고, git 명령 이름과 겹치는 alias 는 git 이
    무시하므로 풀지 않는다. ``!`` 로 시작하는 셸 alias 는 본문 속 git 파괴 호출을 대상 미확정 호출로
    돌려준다(셸 본문의 대상은 정적으로 풀 수 없다).
    """
    seen: set[str] = set()
    current = invocation
    while (
        current.subcommand and current.subcommand not in git_policy._GIT_MUTATION_SUBCOMMANDS
        and current.subcommand not in seen
    ):
        seen.add(current.subcommand)
        value = git._git_alias_value(current, runner=runner)
        if value is None or current.subcommand in git._git_command_names(current.cwd, runner=runner):
            return current
        if value.startswith("!"):
            hidden = _indirect_git_destructive_argvs(value[1:])
            if not hidden:
                return current
            inner = _git_invocation(current.cwd, hidden[0])
            return inner._replace(anchor_certain=False, config=current.config)
        try:
            words = shlex.split(value)
        except ValueError:
            # 따옴표가 닫히지 않은 alias 는 git 도 "bad alias" 로 실행하지 않는다 — 풀 형태가 없다.
            return current
        if not words:
            return current
        current = current._replace(subcommand=words[0], args=(*words[1:], *current.args))
    return current


_INDIRECT_GIT_CALL = re.compile(r"(?<![A-Za-z0-9_.-])git((?:[ \t]+[^\s;&|()<>'\",\[\]]+)*)")

# 인터프리터 코드의 argv 목록 형태(``['git', 'branch', '-D', 'x']``·``('git', ['reset', '--hard'])``)
# — 따옴표 문자열이 쉼표(뒤따르는 여는 대괄호 허용)로 이어진다.
_INDIRECT_GIT_LIST_CALL = re.compile(r"""(['"])git\1((?:\s*,\s*\[?\s*(['"])[^'"]*\3)*)""")

_QUOTED_LITERAL = re.compile(r"""(['"])([^'"]*)\1""")


def _indirect_git_destructive_argvs(text: str) -> list[tuple[str, ...]]:
    """셸·코드 문자열 속 git 호출 중 대상 미확정으로 읽어 파괴 형태인 것의 argv 목록."""
    candidates = [("git", *match.group(1).split()) for match in _INDIRECT_GIT_CALL.finditer(text)]
    candidates.extend(
        ("git", *(literal for _quote, literal in _QUOTED_LITERAL.findall(match.group(2))))
        for match in _INDIRECT_GIT_LIST_CALL.finditer(text)
    )
    argvs: list[tuple[str, ...]] = []
    for argv in candidates:
        invocation = _git_invocation(".", argv)
        if invocation is not None and _git_destructive_form(
            invocation.subcommand, invocation.args, ref_anchor=None, runner=None,
        ) is not None:
            argvs.append(argv)
    return argvs


def _shell_indirect_git_argvs(command: Sequence[str]) -> list[tuple[str, ...]]:
    """간접 실행 명령(``bash -c``·``eval``·``xargs``·``alias`` 등)의 인자 속 git 파괴 호출 argv."""
    values = list(command)
    index = shell_policy._shell_command_word_index(values)
    if index >= len(values):
        return []
    # 괄호 subshell·명령 치환이 command word 에 붙은 git(``(git …)``·``$(git …``)은 파서가 git 으로 읽지 못한다.
    if values[index].startswith(("$", "(", "`")) and Path(values[index].lstrip("$(`")).name == "git":
        return _indirect_git_destructive_argvs(" ".join(values[index:]))
    executor = shell_policy._shell_indirect_executor(values, index)
    if executor is None:
        return []
    return _indirect_git_destructive_argvs(" ".join(executor[1]))


def _removal_path(anchor: Path, token: str, *, cwd_certain: bool) -> tuple[Path | None, bool]:
    """(대상 경로, 그 아래 항목만 지우는지). 정적으로 못 풀면 (None, False).

    glob 은 앞의 고정 폴더 아래 항목으로, 셸 변수·명령 치환은 그 앞에 ``/`` 로 끝난 고정 폴더가
    있을 때만 그 폴더 아래 항목으로 본다. 마지막 이름은 링크를 따라가지 않는다(링크 삭제는 대상을
    지우지 않는다).
    """
    value = os.path.expanduser(token) if token.startswith("~") else token
    contents = False
    special = re.search(r"[$`*?\[]", value)
    if special is not None:
        prefix = value[:special.start()]
        if special.group(0) in "$`" and "/" not in prefix:
            return None, False
        head = prefix.rpartition("/")[0] if "/" in prefix else ""
        value = head or ("/" if prefix.startswith("/") else ".")
        contents = True
    path = Path(value)
    if not path.is_absolute():
        if not cwd_certain:
            return None, False
        path = anchor / path
    normalized = Path(os.path.normpath(path))
    if not normalized.name:
        return normalized, contents
    return normalized.parent.resolve() / normalized.name, contents


def _protected_removal_entries(pm_home: Path) -> tuple[tuple[Path, str], ...]:
    """PM 홈의 보호 경로 — `work/` 아래 linked worktree(슬롯)와 그 `.git`, 그리고 `.repos/`."""
    entries: list[tuple[Path, str]] = []
    work = pm_home / "work"
    if work.is_dir():
        for child in sorted(work.iterdir()):
            if (child / ".git").is_file():
                root = child.resolve()
                entries.extend(((root, shell_policy._PROTECTED_SLOT), (root / ".git", shell_policy._PROTECTED_SLOT_GIT)))
    repos = pm_home / ".repos"
    if repos.is_dir():
        entries.append((repos.resolve(), shell_policy._PROTECTED_SHARED))
    return tuple(entries)


def _nearest_existing_directory(path: Path) -> Path:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return probe if probe.is_dir() else probe.parent


_GIT_GRANT_USED = "used"

_GIT_GRANT_ABSENT = "absent"

_GIT_GRANT_FORGED = "forged"

# 슬롯 쓰기 주인 판정 — 슬롯(`work/<repo>_<N>`) 안에는 그 슬롯을 잡은 task 의 세션만 쓴다. 이 세션의
# task = task 기록 중 `session` 이 훅 세션 ID 와 같은 단 하나(`session_identity.session_task`). 위임
# 하네스(pm_delegate 가 띄움)는 세션 ID 가 따로라 이 환경변수로 받은 task 로 판정한다(그 task 가 열려
# 있을 때만). 슬롯 밖은 판정하지 않는다.
SLOT_WRITE_DELEGATE_TASK_ENV = "PM_DELEGATE_TASK"

# 파일 쓰기·편집 도구의 대상 경로 입력 키(실측 claude 2.1.272 · opencode 1.18.31).
_WRITE_TOOL_PATH_KEYS: dict[str, str] = {
    "Write": "file_path", "Edit": "file_path", "NotebookEdit": "notebook_path",
    "write": "filePath", "edit": "filePath",
}

# 패치 본문 도구 — 이름은 codex·opencode 공통, 본문 키는 codex `command` · opencode `patchText`(실측
# codex 0.154.0 · opencode 1.18.31). 대상 경로는 본문의 파일 머리 줄에 있다.
_PATCH_TOOL_NAME = "apply_patch"

_PATCH_TOOL_TEXT_KEYS: tuple[str, ...] = ("command", "patchText")

_PATCH_TARGET_LINE = re.compile(
    r"^\*\*\* (?:(?:Add|Update|Delete) File|Move to): (.+?)[ \t]*$", re.MULTILINE
)

# 셸 명령 속 절대경로 — 셸 쓰기 대상은 정적으로 못 정하므로 슬롯을 가리키는지만 본다(경고).
_SHELL_ABSOLUTE_PATH = re.compile(
    "(?<![\\w.~-])(/[^\\s'\"`;&|<>()]+|[A-Za-z]:[\\\\/][^\\s'\"`;&|<>()]+)"
)


def _slot_ledger_rows(pm_home: Path) -> tuple[list[dict], list[dict]]:
    """PM 홈 장부의 (leases, tasks) — 형식이 틀리면 올린다(빈 장부로 흘려 쓰기를 통과시키지 않는다)."""
    ledger = pm_home / ".project_manager" / ".local" / "worktree-leases.json"
    data = json.loads(fs.read_text_shared(ledger, encoding="utf-8"))
    leases = data.get("leases") if isinstance(data, dict) else None
    tasks = data.get("tasks", []) if isinstance(data, dict) else None
    if not isinstance(leases, list) or not isinstance(tasks, list):
        raise ValueError(f"{ledger}: leases·tasks 가 list 가 아니다")
    return (
        [row for row in leases if isinstance(row, dict)],
        [row for row in tasks if isinstance(row, dict)],
    )


def slot_containing(
    pm_home: str | os.PathLike[str], path: str | os.PathLike[str],
) -> tuple[dict, list[dict]] | None:
    """`pm_home` 슬롯 중 `path` 를 담은 것 — (그 슬롯 장부 행, task 행들). 슬롯 밖이면 None.

    슬롯은 PM 홈 장부에 등록된 `work/<repo>_<N>` 폴더다. 판정 범위는 이 PM 홈(훅이 설치된 홈)의 슬롯이다.
    장부가 없는 홈에는 슬롯이 없다."""
    home = Path(pm_home)
    try:
        relative = Path(path).expanduser().resolve().relative_to((home / "work").resolve())
    except ValueError:
        return None
    if not relative.parts or not (home / ".project_manager" / ".local" / "worktree-leases.json").is_file():
        return None
    leases, tasks = _slot_ledger_rows(home)
    key = f"work/{relative.parts[0]}"
    row = next((item for item in leases if item.get("slot") == key), None)
    return None if row is None else (row, tasks)


def _session_task(session: str, tasks: Sequence[dict]) -> tuple[str | None, str | None]:
    """이 세션이 연 task — ``(task, None)`` 또는 ``(None, 미해소 사유)``.

    세션 ID 로 찾는 규칙은 `domain.session_identity.session_task`(정확히 1행) 하나다. 세션 ID 가
    없거나 그 세션이 연 task 가 없으면 위임 환경변수의 열린 task 로 찾는다. 세션이 연 task 가
    여럿이면 위임 환경변수를 보지 않고 미해소다."""
    task, reason = session_identity.session_task(tasks, session)
    if task is not None:
        return task, None
    delegable = {
        session_identity.SESSION_TASK_REASON_NO_SESSION,
        session_identity.SESSION_TASK_REASON_NO_TASK.format(session=session.strip()),
    }
    if reason not in delegable:
        return None, reason
    delegated = os.environ.get(SLOT_WRITE_DELEGATE_TASK_ENV, "")
    if delegated and any(row.get("name") == delegated and row.get("session") for row in tasks):
        return delegated, None
    return None, reason


def _slot_write_judgment(
    root: str | os.PathLike[str], session: str, target: str | os.PathLike[str],
) -> dict[str, str] | None:
    """`target` 쓰기의 슬롯 주인 판정 — `root` 슬롯 밖이면 None, 이 세션 task 가 잡은 슬롯이면 ok, 아니면 deny."""
    found = slot_containing(root, target)
    return None if found is None else _slot_owner_judgment(session, found, target)


def _slot_owner_judgment(
    session: str, found: tuple[dict, list[dict]], target: str | os.PathLike[str],
) -> dict[str, str]:
    row, tasks = found
    slot = row["slot"]
    task, unresolved = _session_task(session, tasks)
    if task is None:
        return {
            "verdict": "deny", "cwd_identity": "slot", "code": git_policy.GIT_DESTRUCTIVE_DENY_CODE,
            "case": git_policy._GIT_GUARD_CASE_SLOT_NO_TASK,
            "reason": f"슬롯 {slot} 쓰기 — {unresolved} · 대상 {target}",
        }
    role = row.get("role", "work")
    if row.get("state") == "leased" and role == "work" and row.get("session") == task:
        return {"verdict": "ok", "cwd_identity": "slot", "reason": f"task {task} 가 잡은 슬롯 {slot}"}
    return {
        "verdict": "deny", "cwd_identity": "slot", "code": git_policy.GIT_DESTRUCTIVE_DENY_CODE,
        "case": git_policy._GIT_GUARD_CASE_SLOT_NOT_OWNED,
        "reason": (
            f"슬롯 {slot} 쓰기 — 이 세션 task={task} · 슬롯 주인={row.get('session') or '없음'} · "
            f"상태={row.get('state')} · 역할={role} · 대상 {target}"
        ),
    }


def _write_tool_targets(cwd: str, tool_name: str, tool_input: dict) -> list[Path]:
    """파일 쓰기·편집 도구 입력의 대상 경로(상대 경로는 `cwd` 기준)."""
    raw: list[str] = []
    key = _WRITE_TOOL_PATH_KEYS.get(tool_name)
    if key is not None and isinstance(tool_input.get(key), str):
        raw.append(tool_input[key])
    if tool_name == _PATCH_TOOL_NAME:
        for text_key in _PATCH_TOOL_TEXT_KEYS:
            text = tool_input.get(text_key)
            if isinstance(text, str):
                raw.extend(match.group(1) for match in _PATCH_TARGET_LINE.finditer(text))
    base = Path(cwd).expanduser()
    return [base / Path(value).expanduser() for value in raw]


def _shell_slot_warnings(
    root: str | os.PathLike[str], session: str, cwd: str, command: str, *, main_thread: bool,
) -> list[dict[str, str]]:
    """셸 명령이 이 세션 task 의 것이 아닌 슬롯을 실행 위치나 절대경로로 가리키면 경고한다.

    셸 쓰기(리다이렉트·`sed -i`·스크립트)는 대상을 정적으로 못 정하므로 막지 않는다 — 슬롯 하나당
    경고 한 줄로 알린다. 서브에이전트(``main_thread`` 거짓)에게는 PM 에게 돌려주라는 안내를 싣는다."""
    warnings: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in (cwd, *_SHELL_ABSOLUTE_PATH.findall(command)):
        found = slot_containing(root, raw)
        if found is None or found[0]["slot"] in seen:
            continue
        slot = found[0]["slot"]
        seen.add(slot)
        judgment = _slot_owner_judgment(session, found, raw)
        if judgment["verdict"] == "ok":
            continue
        warnings.append({
            "verdict": "warn", "cwd_identity": "slot",
            "reason": (
                f"이 세션 task 가 잡지 않은 슬롯 {slot} 을 가리키는 셸 명령 — 셸 쓰기 대상은 판정하지 "
                f"않는다. 쓰기라면 멈춘다: "
                f"{git_policy._GIT_GUARD_ACTION_LINES[git_policy._guard_case_for(judgment['case'], main_thread=main_thread)]}"
            ),
        })
    return warnings


def _git_grant_argv(text: str) -> tuple[str, ...]:
    command = text.strip()
    if not command:
        raise git_policy.GitGrantShapeError("허용할 명령 원문이 비어 있다")
    unsafe = _GIT_GRANT_UNSAFE_TEXT.search(command)
    if unsafe is not None:
        raise git_policy.GitGrantShapeError(
            f"`{command}`: 셸 연산자·확장 문자(`{unsafe.group(0).strip()}`)가 든 명령은 허용할 수 "
            "없다 — 한 명령을 값 그대로 적는다"
        )
    try:
        return tuple(process.split_command_argv(command))
    except ValueError as exc:
        raise git_policy.GitGrantShapeError(f"`{command}`: 명령을 argv 로 나눌 수 없다({exc})") from exc


def _git_grant_record(session: str, cwd: str, argv: Sequence[str]) -> dict[str, object]:
    return {"session": session, "cwd": str(Path(cwd).expanduser().resolve()), "argv": list(argv)}


def _git_grant_path(root: str | os.PathLike[str], record: dict[str, object]) -> Path:
    key = hashlib.sha256(
        json.dumps([record["session"], record["cwd"], record["argv"]], ensure_ascii=False)
        .encode("utf-8")
    ).hexdigest()
    return Path(root) / ".project_manager" / ".local" / git_policy.GIT_GRANT_DIR_NAME / f"{key}.json"


def _use_git_grant(root: str | os.PathLike[str], session: str, cwd: str, command: str) -> str:
    """정확히 같은 (세션·폴더·argv) 허용 기록을 이름 바꾸기로 선점해 지운다 — used/absent/forged.

    선점(rename)은 원자적이라 같은 기록을 두 호출이 함께 쓰지 못한다. 선점한 내용이 키와 다르면
    (위조·손상) 지우고 forged 를 돌려준다. 선점 밖의 파일 시스템 실패는 올린다.
    """
    try:
        argv = _git_grant_argv(command)
    except git_policy.GitGrantShapeError:
        return _GIT_GRANT_ABSENT  # 한 명령 단독 형태가 아닌 호출에는 허용 기록이 있을 수 없다.
    record = _git_grant_record(session, cwd, argv)
    path = _git_grant_path(root, record)
    claimed = path.with_name(f"{path.name}.claimed-{os.getpid()}-{time.monotonic_ns()}")
    try:
        os.rename(path, claimed)
    except FileNotFoundError:
        return _GIT_GRANT_ABSENT
    try:
        stored = json.loads(fs.read_text_shared(claimed, encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        stored = None  # 형식이 깨진 기록은 사용자 프롬프트 훅이 쓴 것이 아니다.
    claimed.unlink()
    return _GIT_GRANT_USED if stored == record else _GIT_GRANT_FORGED


def _file_sha256(path: Path) -> str | None:
    """사본 파일 내용을 sha256으로 비교하되 읽기 실패는 advisory로 남긴다."""
    try:
        return hashlib.sha256(fs.read_bytes_shared(path)).hexdigest()
    except OSError:
        return None


_WORKTREE_IDENTITIES = frozenset({"slot", "worktree"})

# 소유 PM 홈을 측정하지 못한 사본을 판정 불가 문구에 표기할 때 쓰는 라벨.
_UNKNOWN_OWNER = "미상"


def _engine_copy_freshness(
    current: tuple[str, Path, Path, Path | None],
    other: tuple[str, Path, Path, Path | None],
    relative_script: Path, *, runner: Any = subprocess.run,
) -> str:
    """sha 가 다른 두 엔진 사본 중 **어느 쪽이 뒤졌는지**를 worktree 의 git 상태로 판정한다.

    입력은 (정체, repo 앵커, 사본 경로, 소유 PM 홈) 두 짝이다. 네 값 모두 호출부가 이미
    측정한 것이라 추가 앵커 조회가 없고, git 조회는 sha 가 다를 때 이 함수에서 딱 1회
    일어난다. 방향 판정은 두 사본이 **같은 PM 홈에 속한 한 짝**일 때만 성립한다 — 정체
    클래스(pm-home/worktree)가 짝을 이루더라도 worktree 의 소유 PM 홈이 상대 사본의 PM 홈과
    다르면 서로 남남인 저장소라 어느 쪽도 상대의 import 사본이 아니다. 그래서 소유 일치를
    ``git status`` **전에** 값으로 확인하고, 다르면 조회 없이 판정 불가로 남긴다.

    짝이 성립하면 판정 축은 worktree 사본의 미커밋 여부다 — 미커밋이면 그 사본이 편집
    중(미확정)이라 PM 홈이 뒤졌다고 말할 수 없고, 커밋 상태면 PM 홈 import 사본이 뒤진
    것이다. 짝으로 해소되지 않거나 git 상태를 읽지 못하면 **단정하지 않는다** — 내용이
    다르다는 사실만 남기고 두 사본 경로(호출부 reason 이 이미 싣는다)를 직접 대조하게 한다.
    """
    sides = (current, other)
    worktree_sides = [side for side in sides if side[0] in _WORKTREE_IDENTITIES]
    home_sides = [side for side in sides if side[0] == "pm-home"]
    if len(worktree_sides) != 1 or len(home_sides) != 1:
        return (
            "사본 내용이 다름 — 어느 쪽이 최신인지 판정 불가"
            f"(PM 홈–worktree 짝 아님: {current[0]} vs {other[0]}) — "
            "두 사본 경로를 직접 대조하라"
        )
    _worktree_identity, worktree_root, worktree_copy, worktree_pm_home = worktree_sides[0]
    _home_identity, _home_root, _home_copy, home_pm_home = home_sides[0]
    if (
        worktree_pm_home is None or home_pm_home is None
        or worktree_pm_home != home_pm_home
    ):
        return (
            "사본 내용이 다름 — 어느 쪽이 최신인지 판정 불가"
            f"(PM 홈–worktree 짝 아님: worktree {worktree_root} 의 소유 PM 홈="
            f"{worktree_pm_home if worktree_pm_home is not None else _UNKNOWN_OWNER}, "
            f"상대 사본의 PM 홈={home_pm_home if home_pm_home is not None else _UNKNOWN_OWNER}) — "
            "두 사본 경로를 직접 대조하라"
        )
    uncommitted = git._git_path_uncommitted(worktree_root, relative_script, runner=runner)
    if uncommitted is None:
        return (
            "사본 내용이 다름 — 어느 쪽이 최신인지 판정 불가"
            f"(worktree {worktree_root} git 상태 조회 실패) — 두 사본 경로를 직접 대조하라"
        )
    if uncommitted:
        return (
            f"worktree 사본 {worktree_copy} 이 미커밋 변경(편집 중·미확정) — "
            "어느 쪽이 최신인지 단정 불가; worktree 를 커밋으로 확정한 뒤 다시 대조하라"
        )
    return (
        f"worktree 사본 {worktree_copy} 은 커밋 상태(clean) — "
        "stale import 사본 — 게이트 판정은 canonical 로"
    )


def _shell_simple_command(
    anchors: set[Path], cwd_certain: bool, segment: Sequence[str], execute: bool | None,
    *, globally_certain: bool, structure_certain: bool,
) -> tuple[
    set[Path], bool, bool | None,
    list[shell_policy._ShellGitInvocation], list[shell_policy._ShellGitInvocation],
]:
    """simple command 하나의 cwd/status를 추상 실행한다. ``execute=None``은 조건부 실행이다."""
    if execute is False or not segment:
        return set(anchors), cwd_certain, None, [], []
    command, redirected = shell_policy._shell_without_redirections(segment)
    if not command:
        return set(anchors), cwd_certain, None, [], []
    # shlex가 quote/POSIX escape 의미를 먼저 확정한 뒤, 보존된 Windows 절대경로 토큰만
    # 판정용 표기로 접는다. 일반 ``t\\ests``는 shlex 결과 ``tests`` 그대로 남는다.
    command = [shell_policy._normalize_shell_path_notation(value) for value in command]
    git_command = shell_policy._shell_git_command(command)
    if git_command is not None:
        # 조건부 실행 여부와 cwd 확정성은 별개다. ``git status && git add …``에서
        # 후자는 실행될 수도 있지만 실행되는 경우의 cwd는 하나로 확정되므로 위험 pathspec을
        # 놓치지 않는다. false gate는 위에서 호출 자체를 제거한다.
        anchor_certain = (
            structure_certain and cwd_certain and len(anchors) == 1
            and git_command.certain and not git_command.anchor_env_override
        )
        certain = globally_certain and anchor_certain
        calls = [
            shell_policy._ShellGitInvocation(
                str(anchor), tuple(command[git_command.index:]), certain, anchor_certain,
            )
            for anchor in sorted(anchors, key=str)
        ]
        return set(anchors), cwd_certain, None, calls, []

    engine_command = shell_policy._shell_engine_command(command)
    engine_calls = []
    if engine_command is not None:
        details = shell_policy._engine_invocation_details(".", engine_command)
        unresolved_script = bool(
            details is not None and details.script is not None
            and shell_policy._unexpanded_shell_parameters(details.script)
        )
        certain = (
            (globally_certain or unresolved_script)
            and cwd_certain and len(anchors) == 1
        )
        engine_calls = [
            shell_policy._ShellGitInvocation(str(anchor), engine_command, certain, certain)
            for anchor in sorted(anchors, key=str)
        ]

    name = command[0]
    if name == "false":
        return set(anchors), cwd_certain, (False if execute is True else None), [], engine_calls
    if name == "true":
        return set(anchors), cwd_certain, (True if execute is True else None), [], engine_calls
    if name in {"echo", "printf", ":"}:
        # 셸 builtin의 정상 simple-command 형태는 status 0이다. 이 증명이 있어야
        # ``echo … && git …``의 실제 실행 경로를 불필요하게 uncertain으로 낮추지 않는다.
        return set(anchors), cwd_certain, (True if execute is True else None), [], engine_calls
    if name in {"pushd", "popd"}:
        # directory stack을 모델링하지 않는다. 실행 가능성이 있으면 이후 cwd는 unknown이며,
        # false gate로 실행되지 않은 경우만 함수 상단에서 certainty를 보존한다.
        return set(anchors), False, None, [], engine_calls
    if name != "cd":
        return set(anchors), cwd_certain, None, [], engine_calls

    operands = [value for value in command[1:] if value != "--"]
    if (redirected or len(operands) != 1 or operands[0].startswith("-")
            or any(ch in operands[0] for ch in "$`")):
        # cd가 실행될 수 있으나 지원 문법으로 target을 단일 증명하지 못했다.
        return set(anchors), False, None, [], engine_calls
    target_arg = Path(operands[0]).expanduser()
    successes: set[Path] = set()
    failures: set[Path] = set()
    for anchor in anchors:
        target = (target_arg if target_arg.is_absolute() else anchor / target_arg).resolve()
        (successes if target.is_dir() else failures).add(target if target.is_dir() else anchor)
    if execute is None:
        return set(anchors) | successes | failures, False, None, [], engine_calls
    if successes and not failures:
        return successes, cwd_certain, True, [], engine_calls
    if failures and not successes:
        return failures, cwd_certain, False, [], engine_calls
    return successes | failures, False, None, [], engine_calls


def _git_invocations_from_shell(
    cwd: str, command: str
) -> shell_policy._ShellParseResult:
    """shell 제어연산과 ``cd``를 보수적으로 추상 실행해 git/엔진 호출 좌표를 반환한다.

    ``&&``/``||``는 직전 status, ``;``/개행은 무조건 순차, ``|``는 각 pipeline command가 원래
    cwd의 subshell에서 실행되는 의미를 구분한다. ``true``/``false``/실재 여부가 확정된 ``cd``만
    status를 증명한다. 조건·cwd가 여러 가능성이면 call을 ``certain=False``로 표시해 후단 deny를
    warn으로 강등한다(대상을 못 정해 이미 거부한 파괴 형태는 강등하지 않는다 — ``anchor_certain``).
    ``if …; then …; fi``의 단순형은 condition cwd를 body로 전달한다.
    """
    if not isinstance(command, str):
        return shell_policy._ShellParseResult((), (), (), False, False, "")
    executable, heredoc_uncertain = shell_policy._strip_shell_heredoc_bodies(command)
    command = shell_policy._normalize_shell_line_continuations(executable)
    prefilter = shell_policy._git_prefilter_text(command)
    engine_candidate = (
        re.search(r"\.project_manager[\\/]+tools[\\/]+", prefilter) is not None
        or re.search(r"(?<![A-Za-z0-9_.-])pytest(?=\s|$)", prefilter) is not None
        or shell_policy._persistent_cd_prefilter(prefilter)
    )
    removal_candidate = any(
        Path(word).name in shell_policy._FILE_REMOVAL_COMMANDS or shell_policy._FILE_REMOVAL_API.search(word)
        for word in shell_policy._INDIRECT_WORD.findall(prefilter)
    )
    # 구문 불확실 경고는 git·엔진 호출 후보가 있을 때만 의미가 있다(파일 명령은 거부 판정이 따로 본다).
    git_or_engine_candidate = bool(_GIT_COMMAND_PREFILTER.search(prefilter)) or engine_candidate
    if not git_or_engine_candidate and not removal_candidate:
        reason = (
            "shell heredoc을 정적으로 완전히 소비할 수 없음"
            if heredoc_uncertain else ""
        )
        return shell_policy._ShellParseResult((), (), (), False, heredoc_uncertain, reason)
    parsed = shell_policy._shell_segments(command)
    if parsed is None:
        # 구문을 못 푼 명령 속 git 파괴 호출·파일 삭제도 대상 미확정 호출로 올린다(판정불가를 통과로
        # 두지 않는다).
        anchor = str(Path(cwd).expanduser().resolve())
        hidden = tuple(
            shell_policy._ShellGitInvocation(anchor, argv, False, False)
            for argv in _indirect_git_destructive_argvs(command)
        )
        hidden_removals = tuple(
            shell_policy._ShellGitInvocation(anchor, argv, False, False)
            for argv in shell_policy._indirect_file_removal_argvs(command, stdin_operands=False)
        )
        return shell_policy._ShellParseResult(
            hidden, (), hidden_removals, False, git_or_engine_candidate,
            "shell 구문을 정적으로 해석할 수 없음" if git_or_engine_candidate else "",
        )
    segments, operators = parsed
    persistent_cd = False
    anchors = {Path(cwd).expanduser().resolve()}
    cwd_certain = True
    found: list[shell_policy._ShellGitInvocation] = []
    engine_found: list[shell_policy._ShellGitInvocation] = []
    removal_found: list[shell_policy._ShellGitInvocation] = []
    last_status: bool | None = None
    in_if_condition = False
    if_status: bool | None = None
    if_body_gate: bool | None = True
    shell_words = [word for segment in segments for word in segment]
    unsupported_control = any(
        segment and segment[0] in {
            "while", "until", "for", "select", "case", "do", "done", "{", "}",
        }
        for segment in segments
    )
    has_grouping = any(
        (word.startswith("(") or word.endswith(")")) and not word.startswith(":(")
        for word in shell_words
    )
    globally_certain = (
        "$" not in command and "`" not in command
        and not has_grouping and not unsupported_control
    )
    # 실행 위치만의 확실성은 괄호 속 데이터(``echo "(done)"``)로는 흐려지지 않는다. 파서가 좌표를
    # 모델링하지 않는 대상 변경(괄호·명령 치환 속 cd, 셸 변수 GIT_DIR/GIT_WORK_TREE)과 루프만 본다.
    structure_certain = not unsupported_control and not shell_policy._shell_hidden_target_change(shell_words)

    i = 0
    while i < len(segments):
        # Pipeline 전체는 같은 outer cwd에서 병렬 subshell 실행되고 cd가 밖으로 전파되지 않는다.
        end = i
        while end < len(operators) and operators[end] == "|":
            end += 1
        previous_operator = operators[i - 1] if i > 0 else None
        gate = shell_policy._shell_gate(previous_operator, last_status)
        pipeline = end > i
        base_anchors = set(anchors)
        base_cwd_certain = cwd_certain
        group_status: bool | None = None

        for position in range(i, end + 1):
            segment = list(segments[position])
            # loop/case 전체 실행 의미는 지원하지 않는다. 다만 condition/body command 위치의
            # git은 놓치지 않고 uncertain call로 surface해 deny 대신 ambiguity warn을 만든다.
            if unsupported_control and segment and segment[0] in {"while", "until", "do", "{"}:
                segment = segment[1:]
            starts_if = bool(segment and segment[0] == "if")
            starts_then = bool(segment and segment[0] == "then")
            starts_else = bool(segment and segment[0] == "else")
            starts_fi = bool(segment and segment[0] == "fi")
            if starts_if:
                in_if_condition = True
                segment = segment[1:]
            if starts_then:
                in_if_condition = False
                if_body_gate = if_status
                segment = segment[1:]
                gate = if_body_gate
            if starts_else:
                in_if_condition = False
                if_body_gate = None if if_status is None else not if_status
                segment = segment[1:]
                gate = if_body_gate
            if starts_fi:
                in_if_condition = False
                if_body_gate = True
                segment = segment[1:]
            if (not in_if_condition and not starts_then and not starts_else
                    and if_body_gate is not True):
                if gate is True:
                    gate = if_body_gate

            command_anchors = base_anchors if pipeline else anchors
            command_cwd_certain = base_cwd_certain if pipeline else cwd_certain
            new_anchors, new_cwd_certain, status, calls, engine_calls = _shell_simple_command(
                command_anchors, command_cwd_certain, segment, gate,
                globally_certain=globally_certain, structure_certain=structure_certain,
            )
            command_without_redirections, _redirected = shell_policy._shell_without_redirections(segment)
            if not calls and gate is not False:
                # 간접 실행 명령 인자 속 git 파괴 호출은 호출 좌표를 풀 수 없다 — 대상 미확정 호출로 올린다.
                calls = [
                    shell_policy._ShellGitInvocation(str(anchor), argv, False, False)
                    for argv in _shell_indirect_git_argvs(command_without_redirections)
                    for anchor in sorted(command_anchors, key=str)
                ]
            if gate is not False:
                removal_found.extend(shell_policy._shell_removal_calls(
                    command_anchors,
                    structure_certain and command_cwd_certain and len(command_anchors) == 1,
                    command_without_redirections,
                ))
            if (
                not pipeline and not starts_if
                and gate is not False and status is not False
                and command_without_redirections
                and command_without_redirections[0] == "cd"
            ):
                persistent_cd = True
            found.extend(calls)
            engine_found.extend(engine_calls)
            group_status = status
            if not pipeline and gate is not False:
                anchors = new_anchors
                cwd_certain = new_cwd_certain
            if in_if_condition:
                if_status = status

        if pipeline:
            anchors = base_anchors
            cwd_certain = base_cwd_certain
        if gate is not False:
            last_status = group_status
        i = end + 1
    uncertain = heredoc_uncertain or (unsupported_control and git_or_engine_candidate)
    reason = "shell 구문/cwd를 정적으로 단일 증명할 수 없음" if uncertain else ""
    return shell_policy._ShellParseResult(
        tuple(found), tuple(engine_found), tuple(removal_found), persistent_cd, uncertain, reason,
    )


def _finish_git_guard_judgment(
    result: dict[str, str], judgments: Sequence[dict[str, str]], *, cwd: str, command: str,
    grant_root: str | os.PathLike[str] | None, grant_session: str | None,
) -> dict[str, str]:
    """멈춤 거부면 1회 허용을 소비하거나 멈춤 지시·다음 행동을 붙인다. 내부 표식(case)은 뺀다."""
    final = {key: value for key, value in result.items() if key != "case"}
    denies = [item for item in judgments if item["verdict"] == "deny"]
    cases = [item["case"] for item in denies if "case" in item]
    if final["verdict"] != "deny" or not cases:
        return final
    grants_available = grant_session is not None
    notes: list[str] = []
    grantable = len(cases) == len(denies) and all(
        case == git_policy._GIT_GUARD_CASE_ASK_USER for case in cases
    )
    if grants_available and grantable:
        try:
            use = _use_git_grant(grant_root, grant_session, cwd, command)
        except OSError as exc:
            # 허용을 확인하지 못했으면 거부를 유지한다(통과로 흘리지 않는다).
            use = None
            notes.append(f"허용 기록을 확인하지 못해 거부를 유지한다({exc}).")
        if use == _GIT_GRANT_USED:
            return {
                "verdict": "warn", "cwd_identity": final["cwd_identity"],
                "reason": (
                    f"사용자 1회 허용으로 실행한다 — 허용 기록을 썼으므로 이 명령을 다시 실행하려면 "
                    f"새 허용이 필요하다: `{command.strip()}`"
                ),
            }
        if use == _GIT_GRANT_FORGED:
            notes.append("같은 명령의 허용 기록이 사용자 프롬프트 훅이 쓴 형식과 달라 폐기했다.")
    final["reason"] = git_policy._git_guard_stop_reason(
        cases, final["reason"], grants_available=grants_available, notes=notes,
    )
    return final
