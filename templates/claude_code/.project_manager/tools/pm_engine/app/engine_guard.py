"""엔진 호출 및 삭제 판정 (층: app)."""
from __future__ import annotations

from typing import Any
from pathlib import Path
from collections.abc import Sequence
from pm_engine.domain import board_command_rules
from pm_engine.infra import git
from pm_engine.domain import git_policy
from pm_engine.app import hooks
from pm_engine.infra import pm_paths
from pm_engine.domain import shell_policy
import subprocess
import sys
from pm_engine.infra import task_store
from pm_engine.infra import worktree_paths


def _git_anchor_identity(anchor: Path, *, runner: Any=subprocess.run, slot_home: Path | None=None) -> tuple[str, Path | None, Path | None]:
    """(public identity, git root, owning PM home)를 기존 misanchor seam으로 해소한다."""
    root = git._git_repo_root(anchor, runner=runner)
    if root is None:
        shared = git._shared_repository_root(anchor, runner=runner)
        if shared is not None:
            return ('shared-repo', shared, shared.parent.parent)
        return ('non-repo', None, None)
    if pm_paths._has_real_board(root / '.project_manager'):
        return ('pm-home', root, root)
    if slot_home is not None:
        containing = hooks.slot_containing(slot_home, root)
        if containing is not None:
            parent = slot_home / containing[0]['slot']
            if root != parent.resolve() and (not (slot_home / 'work').is_symlink()) and (not parent.is_symlink()):
                return ('foreign', root, None)
    pm_home = pm_paths._pm_home_worktree_misanchor(pm_paths.home_at(root))
    if pm_home is not None:
        registered_slots = worktree_paths._registered_slot_paths(pm_paths.home_at(pm_home), runner=runner)
        return ('slot' if root in registered_slots else 'worktree', root, pm_home)
    return ('foreign', root, None)

def _dangerous_cross_repo_pathspecs(home, repo_root: Path, path_base: Path, pathspecs: Sequence[str], *, runner: Any=subprocess.run) -> tuple[str, ...]:
    """PM 홈과 등록 slot 양쪽에 실재하는 좁은 코드-root pathspec만 반환한다.

    존재하지 않는 path는 git 자체가 fail-loud하므로 deny하지 않는다. ``tests``/``templates``만
    후보인 이유는 PM 홈 기록 commit의 정상 pathspec(``.project_manager/wiki/...`` 등)을 막지 않고,
    실제 add-harness 오염 모드만 고정하기 위해서다.
    """
    slots = worktree_paths._registered_slot_paths(home, runner=runner)
    if not slots:
        return ()
    dangerous: list[str] = []
    for raw in pathspecs:
        if not raw or any((ch in raw for ch in '*?[')):
            continue
        literal = raw
        top_relative = False
        if literal.startswith(':(top)'):
            literal = literal[len(':(top)'):]
            top_relative = True
        elif literal.startswith(':/'):
            literal = literal[2:]
            top_relative = True
        elif literal.startswith(':('):
            continue
        rel = Path(literal)
        if rel.is_absolute():
            try:
                rel = rel.resolve().relative_to(repo_root)
            except (OSError, ValueError):
                continue
        local = ((repo_root if top_relative else path_base) / rel).resolve()
        try:
            normalized = local.relative_to(repo_root)
        except ValueError:
            continue
        parts = normalized.parts
        if not parts or parts[0] not in git_policy._GIT_ANCHOR_DENY_ROOTS:
            continue
        if not local.exists():
            continue
        if any(((slot / normalized).exists() for slot in slots)):
            dangerous.append(normalized.as_posix())
    return tuple(dict.fromkeys(dangerous))

def judge_file_removal(cwd: str, argv: Sequence[str], *, cwd_certain: bool=True, runner: Any=subprocess.run) -> dict[str, str] | None:
    """파일 명령이 슬롯·공용 저장소를 지우거나 옮기면 멈춤 거부 판정, 아니면 None.

    보호 경로는 호출 폴더와 대상 경로 각각의 소유 PM 홈에서 모은다(대상이 PM 홈 조상이면 호출
    폴더 쪽 PM 홈이 잡는다). ``cwd_certain=False`` 는 셸 파서가 실행 위치를 증명 못 한 호출이다.
    """
    shape = shell_policy._file_removal_shape(argv)
    if shape is None:
        return None
    anchor = Path(cwd).expanduser().resolve()
    shown = ' '.join(argv)
    identity, _root, cwd_home = _git_anchor_identity(anchor, runner=runner)
    homes: set[Path] = {cwd_home} if cwd_home is not None else set()
    resolved: list[tuple[Path, str]] = []
    for target in shape.targets:
        path, contents = hooks._removal_path(anchor, target.token, cwd_certain=cwd_certain)
        if path is None:
            if not shape.unresolved_is_destructive:
                continue
            return {'verdict': 'deny', 'cwd_identity': identity, 'code': git_policy.GIT_DESTRUCTIVE_DENY_CODE, 'case': git_policy._GIT_GUARD_CASE_FOLDER_UNKNOWN, 'reason': f'대상 경로를 정적으로 풀 수 없는 파일 {shape.verb} — `{shown}` (대상 `{target.token}` 이 슬롯·공용 저장소일 수 있음)'}
        scope = 'contents' if contents and target.scope == 'self' else target.scope
        resolved.append((path, scope))
        _identity, _target_root, target_home = _git_anchor_identity(hooks._nearest_existing_directory(path), runner=runner)
        if target_home is not None:
            homes.add(target_home)
    entries = [entry for data_home in sorted(homes) for entry in hooks._protected_removal_entries(data_home)]
    for path, scope in resolved:
        kind = shell_policy._protected_removal_kind(path, scope, entries)
        if kind is None:
            continue
        label, case = shell_policy._PROTECTED_KIND_FORMS[kind]
        return {'verdict': 'deny', 'cwd_identity': identity, 'code': git_policy.GIT_DESTRUCTIVE_DENY_CODE, 'case': case, 'reason': f'{label} {shape.verb} — `{shown}` 대상={path}'}
    return None

def _open_todo_count(home, task: str) -> int:
    """실행 엔진으로 대상 Home의 열린 할 일 수를 판독한다."""
    return len(task_store.read_task_todos(home, task))

def _no_open_todo_judgment(home, session: str) -> dict[str, str] | None:
    """열린 todo 가 없는 세션의 파일 쓰기 **경고** — 경고할 근거가 없으면 `None`.

    "무엇을 하는 중인지" 가 장부에 없으면 다음 세션도 사용자도 그 일을 못 본다. 그래서 파일 쓰기
    호출마다 그 사실을 화면에 올린다. **막지는 않는다** — 셸(`sed -i`·리다이렉션)은 이 판정을 타지
    않아 막아도 우회로가 남고, 나머지 여덟 판정과 달리 이 축은 "명령이 위험한가" 가 아니라 "일을
    적었나" 라서 1회 허용 기전(명령 argv 에 묶인다)에도 얹히지 않는다.

    task 를 해소할 수 없으면(부트스트랩 전·하네스 밖·PM 홈 아님) 아무 말도 하지 않는다. 그 경우는
    슬롯 주인 판정(`_slot_write_judgment`)이 이미 거부한다 — 같은 사실을 두 번 말하지 않는다.
    장부가 **있는데** 형식이 틀리면 `_slot_ledger_rows` 가 올린다(빈 장부로 흘려 통과시키지 않는
    슬롯 판정과 같은 규율).
    """
    data_home = home.local_dir.parent.parent
    if not pm_paths.leases_file(home).is_file():
        return None
    _leases, tasks = hooks._slot_ledger_rows(data_home)
    task = hooks._session_task(session, tasks)
    if task is None:
        return None
    open_todos = _open_todo_count(home, task)
    if open_todos > 0:
        return None
    return {'verdict': 'warn', 'cwd_identity': 'non-repo', 'reason': f'task {task} 에 열린 todo 가 없다 — 지금 하는 일이 장부에 없어서 다음 세션도 사용자도 그 일을 못 본다. {git_policy._GIT_GUARD_ACTION_LINES[git_policy._GIT_GUARD_CASE_NO_OPEN_TODO]}'}

def judge_git_anchor(home, session: str, cwd: str, argv: list[str], *, runner: Any=subprocess.run, cwd_certain: bool=True) -> dict[str, str]:
    """raw git 호출의 cwd 안전성을 판정하는 엔진·하네스 중립 seam.

    ``root`` 는 훅이 설치된 PM 홈, ``session`` 은 호출한 하네스 세션 ID 다 — ``root`` 슬롯의 git 쓰기는
    그 세션 task 가 잡은 슬롯일 때만 통과한다(`_slot_write_judgment`).

    deny 는 둘이다. (1) 슬롯·공용 저장소 대상의 손 git 파괴 형태(브랜치 만들기·지우기·이름 바꾸기,
    변경 버리기, 슬롯 지우기·옮기기, 원격 덮어쓰기, 저수준 ref 조작) — 브랜치와 슬롯은 엔진만 만들고
    지운다. 대상을 정적으로 못 정한 호출(``GIT_DIR``·변수 ``-C``·셸 파서가 cwd 를 증명 못 함 =
    ``cwd_certain=False``)은 슬롯·공용 저장소일 수 있으므로 형태만으로 거부한다. 이 deny 는 거부
    경우(``case``)와 거부한 호출만 싣고, 멈춤 지시·다음 행동 문구는 `judge_git_anchor_command` 가
    붙인다. (2) PM 홈에서 등록 slot과 교차 실재하는
    코드-root pathspec을 mutation으로 건드리는 실사고 패턴. 그 밖은 warn(정체 주입)이며 비-repo/
    foreign 대상과 판정불가 비파괴 호출은 정상 작업을 막지 않는다. ``cwd_certain`` 은 argv 호출자가
    ``cwd`` 를 실제 실행 위치로 아는 기본 형태이고, 셸 파서만 증명 못 한 호출에 False 를 넘긴다.
    """
    invocation = hooks._git_invocation(cwd, argv)
    if invocation is None:
        return {'verdict': 'ok', 'cwd_identity': 'non-repo', 'reason': 'git 호출이 아님'}
    identity, repo_root, pm_home = _git_anchor_identity(invocation.cwd, runner=runner, slot_home=Path(home.root))
    target_certain = cwd_certain and invocation.anchor_certain
    containing = hooks.slot_containing(home.root, repo_root) if repo_root is not None else None
    if identity in git_policy._GIT_PROTECTED_IDENTITIES or containing is not None or (not target_certain):
        resolved = hooks._git_alias_resolved(invocation, runner=runner) if target_certain else invocation
        target_certain = target_certain and resolved.anchor_certain
        form = hooks._git_destructive_form(resolved.subcommand, resolved.args, ref_anchor=invocation.cwd if target_certain else None, runner=runner)
        if form is not None:
            where = f'대상={repo_root or invocation.cwd} (정체={identity})' if target_certain else '대상을 정적으로 정할 수 없어 슬롯·공용 저장소일 수 있음'
            shown = ' '.join(('git', resolved.subcommand, *resolved.args))
            return {'verdict': 'deny', 'cwd_identity': identity, 'code': git_policy.GIT_DESTRUCTIVE_DENY_CODE, 'case': form.case if target_certain else git_policy._GIT_GUARD_CASE_FOLDER_UNKNOWN, 'reason': f'손 git {form.label} — `{shown}` {where}'}
        if repo_root is not None and target_certain and git_policy._git_writes_slot(resolved.subcommand, resolved.args):
            owner = hooks._slot_write_judgment(home.root, session, repo_root)
            if owner is not None and owner['verdict'] == 'deny':
                return {**owner, 'cwd_identity': identity, 'reason': f'git {resolved.subcommand} — {owner['reason']}'}
    subcommand = invocation.subcommand
    if subcommand not in git_policy._GIT_MUTATION_SUBCOMMANDS:
        return {'verdict': 'ok', 'cwd_identity': identity, 'reason': f'git {subcommand or '<missing>'}는 mutation 가드 대상이 아님'}
    if not invocation.anchor_certain:
        return {'verdict': 'warn', 'cwd_identity': identity, 'reason': 'GIT_DIR/work-tree 지정 또는 변수 -C 로 실제 mutation anchor를 단일 증명할 수 없음'}
    if identity == 'slot':
        return {'verdict': 'ok', 'cwd_identity': identity, 'reason': f'등록된 활성 linked worktree 앵커={repo_root} — git {subcommand} 허용'}
    if identity == 'pm-home' and repo_root is not None and (pm_home is not None):
        dangerous = _dangerous_cross_repo_pathspecs(pm_paths.home_at(pm_home), repo_root, invocation.cwd, git_policy._git_pathspecs(subcommand, invocation.args), runner=runner)
        if dangerous:
            joined = ', '.join(dangerous)
            return {'verdict': 'deny', 'cwd_identity': identity, 'reason': f'PM 홈에서 등록 worktree와 양쪽에 실재하는 코드 경로({joined})를 git {subcommand} 하려 함 — 대상 worktree cwd에서 다시 실행하라'}
    shown = str(repo_root or invocation.cwd)
    return {'verdict': 'warn', 'cwd_identity': identity, 'reason': f'git {subcommand} 실행 앵커={shown} (정체={identity}) — 의도한 repo인지 확인'}

def _engine_other_copy(cwd_root: Path | None, script_root: Path, script_identity: str, pm_home: Path | None, relative_script: Path, *, runner: Any) -> tuple[Path, Path, str] | None:
    """함께 존재하는 반대편(PM import/canonical) 엔진 사본 하나를 찾는다.

    세 번째 값은 그 사본의 **정체 해소 키**다 — ``cwd`` 는 호출 cwd 의 이미 측정된 정체를
    그대로 쓰라는 뜻이고, ``slot``/``pm-home`` 은 후보를 고를 때 이미 확정된 정체다. 방향
    판정(어느 사본이 뒤졌는지)이 추가 앵커 조회 없이 두 사본의 정체를 갖게 하는 자리다.
    """
    candidates: list[tuple[Path, str]] = []
    if script_identity == 'pm-home' and pm_home is not None:
        if cwd_root is not None and cwd_root != script_root:
            candidates.append((cwd_root, 'cwd'))
        candidates.extend(((slot, 'slot') for slot in worktree_paths._registered_slot_paths(pm_paths.home_at(pm_home), runner=runner)))
    elif script_identity in {'slot', 'worktree'} and pm_home is not None:
        candidates.append((pm_home, 'pm-home'))
    for root, identity_key in candidates:
        root = root.resolve()
        candidate = root / relative_script
        if root != script_root and candidate.is_file():
            return (root, candidate, identity_key)
    return None

def judge_engine_invocation(cwd: str, argv: list[str], *, runner: Any=subprocess.run) -> dict[str, str]:
    """엔진/pytest 호출의 사본·소유 앵커를 판정한다.

    deny는 PM 홈에 대상 ``tests/``가 없는 pytest 호출과 non-PM 엔진 사본의 board
    mutation 두 축뿐이다. 상대 엔진 경로는 실행 사본의 절대경로와 반대편 사본의
    도구 파일 sha256 비교를 warn으로 주입한다. sha 가 다를 때만 ``_engine_copy_freshness``
    가 방향(어느 사본이 뒤졌는지)을 worktree git 상태로 1회 판정하며, 판정이 서지 않으면
    단정하지 않는다.
    """
    cwd_path = Path(cwd).expanduser().resolve()
    identity, cwd_root, cwd_pm_home = _git_anchor_identity(cwd_path, runner=runner)
    details = shell_policy._engine_invocation_details(cwd, argv)
    if details is None:
        return {'verdict': 'ok', 'cwd_identity': identity, 'reason': '엔진/pytest 호출이 아님'}
    if details.kind == 'pytest':
        if identity == 'pm-home' and shell_policy._pytest_targets_tests(details.args) and (not (cwd_path / 'tests').is_dir()):
            return {'verdict': 'deny', 'cwd_identity': identity, 'code': 'pm-home-relative-pytest-tests-missing', 'reason': 'PM 홈 cwd에서 pytest tests/를 실행하려 하나 cwd의 tests/ 디렉터리가 실재하지 않음 — 대상 worktree의 workdir에서 실행하라'}
        return {'verdict': 'ok', 'cwd_identity': identity, 'reason': 'pytest 호출은 PM 홈 tests/ 오앵커 패턴이 아님'}
    assert details.script is not None
    unexpanded = shell_policy._unexpanded_shell_parameters(details.script)
    unresolved_reason = f'엔진 호출 경로의 미확장 토큰={', '.join(unexpanded)} — 확장 불가 — 실제 사본 미상; 이 사본의 repo 앵커도 미상' if unexpanded else ''
    unresolved_script_name = Path(details.script.replace('\\', '/')).name
    unresolved_mutation = board_command_rules._board_mutation_subcommand(details.args) if unresolved_script_name == 'board.py' else ''
    if unexpanded:
        if unresolved_mutation and identity in {'slot', 'worktree'}:
            return {'verdict': 'deny', 'cwd_identity': identity, 'reason': f"board mutation '{unresolved_mutation}'이 non-PM cwd에서 감지됨 — {unresolved_reason}; board mutation은 PM 홈 사본으로 실행하라"}
        return {'verdict': 'warn', 'cwd_identity': identity, 'reason': f'{unresolved_reason}; board mutation은 PM 홈 사본으로 실행하라'}
    script_token = Path(details.script).expanduser()
    relative = not script_token.is_absolute()
    script_path = (cwd_path / script_token).resolve() if relative else script_token.resolve()
    script_root = script_path.parents[2]
    script_identity, _resolved_root, script_pm_home = _git_anchor_identity(script_root, runner=runner)
    relative_script = Path('.project_manager') / 'tools' / script_path.name
    mutation = board_command_rules._board_mutation_subcommand(details.args) if script_path.name == 'board.py' else ''
    if mutation and script_identity in {'slot', 'worktree'}:
        return {'verdict': 'deny', 'cwd_identity': identity, 'reason': f"board mutation '{mutation}'이 non-PM 엔진 사본 {script_path}를 선택함 — 이 사본의 repo 앵커={script_root} ({script_identity}); board mutation은 PM 홈 사본 {(script_pm_home / relative_script if script_pm_home else relative_script)} 로 실행하라"}
    if not relative:
        return {'verdict': 'ok', 'cwd_identity': identity, 'reason': f'절대경로 엔진 사본={script_path}; 이 사본의 repo 앵커={script_root} ({script_identity})'}
    reason = f'상대경로 엔진 호출의 실행 사본={script_path}; 이 사본의 repo 앵커={script_root} ({script_identity}); board mutation은 PM 홈 사본으로 실행하라'
    other = _engine_other_copy(cwd_root, script_root, script_identity, script_pm_home or cwd_pm_home, relative_script, runner=runner)
    if other is not None:
        other_root, other_path, other_identity_key = other
        current_hash = hooks._file_sha256(script_path)
        other_hash = hooks._file_sha256(other_path)
        if current_hash is None or other_hash is None:
            hash_state = '도구 파일 sha256 비교 불가(파일 읽기 실패) — 사본 경로를 직접 확인'
        elif current_hash == other_hash:
            hash_state = f'도구 파일 sha256 동일({current_hash})'
        else:
            if other_identity_key == 'cwd':
                other_identity, other_pm_home = (identity, cwd_pm_home)
            else:
                other_identity = other_identity_key
                other_pm_home = script_pm_home or cwd_pm_home
            hash_state = f'도구 파일 sha256 다름({current_hash} != {other_hash}) — ' + hooks._engine_copy_freshness((script_identity, script_root, script_path, script_pm_home), (other_identity, other_root, other_path, other_pm_home), relative_script, runner=runner)
        reason += f'; 함께 존재하는 다른 사본={other_path}; {hash_state}'
    return {'verdict': 'warn', 'cwd_identity': identity, 'reason': reason}

_RECORD_TASK_SESSION_CLAIM_PROGRAM = 'import sys\nsys.path.insert(0, sys.argv[1])\nfrom pathlib import Path\nfrom pm_engine.infra import pm_paths, task_store\ntask_store.record_task_session_claim(pm_paths.home_at(Path(sys.argv[1]).parents[1]), sys.argv[2], sys.argv[3])\n'

def _record_bootstrap_session_claims(session: str, parsed: shell_policy._ShellParseResult) -> None:
    """`pm_bootstrap.py --task <task>` 호출마다 이 세션 ID 기록을 그 부트스트랩 사본의 홈에 남긴다.

    부트스트랩 프로세스는 자기 하네스 세션 ID 를 알 수 없어(codex·opencode 는 도구 프로세스 환경에
    없다) 이 기록을 가져가 task 에 쓴다(`task_store.take_task_session_claim`). 실행 위치·인자를
    정적으로 못 정한 호출과 세션 ID 없는 훅은 남기지 않는다 — 그러면 부트스트랩이 기록 없음으로 멈춘다.

    기록은 그 사본 홈의 엔진이 하위 프로세스로 쓴다. 이 프로세스에는 이 홈의 `pm_engine` 이 이미
    적재돼 있어, 다른 홈의 패키지 코드를 이 프로세스에 적재하지 않고, 해당 실행 사본의 task_store가
    기록한다. 하위 프로세스가 실패하면 stderr를 담아 그대로 올린다.
    """
    if not session:
        return
    for call in parsed.engine_invocations:
        details = shell_policy._engine_invocation_details(call.cwd, call.argv)
        if not call.certain or details is None or details.kind != 'engine' or (Path(details.script).name != 'pm_bootstrap.py'):
            continue
        task = shell_policy._bootstrap_task_argument(details.args)
        if task is None:
            continue
        script = (Path(call.cwd).expanduser() / details.script).resolve()
        result = subprocess.run([sys.executable, '-c', _RECORD_TASK_SESSION_CLAIM_PROGRAM, str(script.parent), task, session], capture_output=True, text=True, encoding='utf-8')
        if result.returncode != 0:
            raise RuntimeError(f'세션 기록을 남기지 못했다 ({script.with_name('worktree_pool.py')} · rc={result.returncode}): {result.stderr.strip()}')
