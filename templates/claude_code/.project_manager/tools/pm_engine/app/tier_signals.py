"""티켓 tier 신호 (층: app)."""
from __future__ import annotations

from pathlib import Path
from collections.abc import Sequence
import ast
from pm_engine.infra import fs
from pm_engine.infra import repo_files
from pm_engine.domain import ticket_rules


def _list_owned_touch_files(repo: Path, subtree: Path, list_owned=None):
    """repo-owned OWNED 열거 호출 단일 지점(제품/테스트 DI 공유)."""
    if list_owned is not None:
        return list_owned(repo, subtree)
    return repo_files.list_repo_owned_files(repo, subtree, mode=repo_files.OWNED)

def expand_owned_touch_files(repo: Path, touches: Sequence[str], *, list_owned=None) -> ticket_rules.TouchFileExpansion:
    """touches directory를 공용 repo-owned 열거로 파일 단위 전개한다.

    디렉터리를 열거할 수 없으면 빈 집합으로 축소하지 않고 미해소로
    보존한다. 소비자는 tier 상향 기본값/경고에 이 신호를 쓴다.
    """
    repo = Path(repo)
    paths: set[str] = set()
    unresolved: set[str] = set()
    for raw in touches:
        canonical = ticket_rules._canonical_tier_touch(raw)
        rel = Path(canonical)
        if not canonical or rel.is_absolute() or '..' in rel.parts:
            unresolved.add(canonical or raw)
            continue
        target = repo / rel
        is_directory = raw.rstrip().endswith(('/', '\\')) or target.is_dir()
        if not is_directory:
            paths.add(rel.as_posix())
            continue
        if not target.is_dir():
            unresolved.add(rel.as_posix())
            continue
        try:
            owned = _list_owned_touch_files(repo, rel, list_owned)
        except Exception as exc:
            unresolved.add(rel.as_posix())
            continue
        prefix = rel.as_posix().rstrip('/') + '/'
        for item in owned:
            candidate = Path(item).as_posix()
            if candidate.startswith(prefix) and (not (repo / candidate).is_dir()):
                paths.add(candidate)
    return ticket_rules.TouchFileExpansion(tuple(sorted(paths)), tuple(sorted(unresolved)))

def _shared_tool_code(tools_dir: Path, *, list_owned=None) -> tuple[str, ...]:
    """tools/ import 구문을 AST로 스캔해 2개+ 도구가 import하는 파일을 산출.

    목록은 파일명으로 굳히지 않고 현재 tools 트리와 import/import-from
    구문만을 입력으로 삼는다. 파싱 불가 파일은 보조 신호에서만 제외한다.
    """
    repo = tools_dir.parent.parent
    subtree = tools_dir.relative_to(repo)
    prefix = subtree.as_posix().rstrip('/') + '/'
    python_files = sorted({repo / Path(path) for path in _list_owned_touch_files(repo, subtree, list_owned) if Path(path).as_posix().startswith(prefix) and Path(path).suffix == '.py' and (not ticket_rules._is_tool_test_file(Path(path).relative_to(subtree).as_posix()))})
    by_stem: dict[str, list[Path]] = {}
    for path in python_files:
        by_stem.setdefault(path.stem, []).append(path)
    importers: dict[Path, set[Path]] = {path: set() for path in python_files}
    for importer in python_files:
        try:
            tree = ast.parse(fs.read_text_shared(importer, encoding='utf-8'), filename=str(importer))
        except (OSError, UnicodeError, SyntaxError):
            continue
        names: set[str] = set()
        package_targets: set[Path] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update((alias.name.rsplit('.', 1)[-1] for alias in node.names))
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.split('.')[0] == ticket_rules._ENGINE_PACKAGE:
                    package_targets.update((tools_dir.joinpath(*node.module.split('.'), f'{alias.name}.py') for alias in node.names))
                elif node.module:
                    names.add(node.module.rsplit('.', 1)[-1])
                elif node.level:
                    names.update((alias.name.rsplit('.', 1)[-1] for alias in node.names))
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and (node.func.id == '_load_module_from_path'):
                literal = None
                if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                    literal = node.args[1].value
                if literal is None:
                    for keyword in node.keywords:
                        if keyword.arg == 'expected_filename' and isinstance(keyword.value, ast.Constant):
                            literal = keyword.value.value
                            break
                if isinstance(literal, str) and literal.endswith('.py'):
                    names.add(Path(literal).stem)
        for name in names:
            candidates = by_stem.get(name, ())
            if len(candidates) == 1 and candidates[0] != importer:
                importers[candidates[0]].add(importer)
        for target in package_targets:
            if target in importers and target != importer:
                importers[target].add(importer)
    prefix = '.project_manager/tools'
    return tuple((f'{prefix}/{path.relative_to(tools_dir).as_posix()}' for path in python_files if len(importers[path]) >= 2))

def tier_signals(touches: Sequence[str], tools_dir: Path, *, list_owned=None) -> ticket_rules.TierSignals:
    """PM 판정을 대체하지 않는 h1·h2·문서-전용 기계 신호."""
    repo = tools_dir.parent.parent
    subtree = Path('.')
    try:
        owned = tuple(_list_owned_touch_files(repo, subtree, list_owned))
        owned_error = None
    except Exception as exc:
        owned = ()
        owned_error = exc

    def inventory(_repo, _subtree):
        if owned_error is not None:
            raise owned_error
        return owned
    expanded = expand_owned_touch_files(repo, touches, list_owned=inventory)
    modules = ticket_rules._tier_tool_module_touches(expanded.paths)
    shared = set(_shared_tool_code(tools_dir, list_owned=inventory)) if owned_error is None else set()
    canonical_touches = tuple((ticket_rules._canonical_tier_touch(path) for path in touches))
    touched_shared = tuple(sorted((path for path in shared if any((path == touch or path.startswith(touch + '/') for touch in canonical_touches if touch)))))
    return ticket_rules.TierSignals(modules, touched_shared, bool(expanded.paths) and (not expanded.unresolved_directories) and all((ticket_rules._is_tier_document(path) for path in expanded.paths)), tuple((path for path in expanded.unresolved_directories if path == '.project_manager/tools' or path.startswith('.project_manager/tools/'))))
