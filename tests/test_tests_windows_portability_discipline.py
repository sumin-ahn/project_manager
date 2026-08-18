"""tests/ 안 Windows 잠복 함정 재발 방지 가드 (T-0741).

두 클래스를 tests/**/*.py AST 로 전수 스캔한다:

  (a) 전역 `os.name` 변이 — `setattr(os, "name", ...)`/`monkeypatch.setattr(<mod>.os,
      "name", ...)`/`os.name = ...` 는 Windows 에서 pathlib 이 그 값으로 flavour 를
      고르므로, 변이 중 `PosixPath`/`WindowsPath` 생성이 `NotImplementedError` 로 죽는다.
      모듈 seam(`_probe_os_name()` 류)을 patch 해야 한다.
  (b) python 자식 subprocess 를 `text=True`/`universal_newlines=True` 만으로 읽고
      `encoding=` 미명시 — Windows cp949 콘솔 코덱의 `_readerthread` 가 UTF-8 산출을
      디코드하다 죽어 `stdout=None` 이 된다.
"""
from __future__ import annotations

import ast
from pathlib import Path


TESTS = Path(__file__).resolve().parent
_PYTHON_INTERPRETER_LITERALS = {"py", "python3"}
_TEXT_MODE_KEYWORDS = {"text", "universal_newlines"}
_SUBPROCESS_ATTRS = {"run", "Popen", "check_output"}


def _is_os_name_string(node: ast.expr) -> bool:
    """두 번째 인자가 문자열 리터럴 ``"name"`` 인지."""
    return isinstance(node, ast.Constant) and node.value == "name"


def _is_os_module_reference(node: ast.expr) -> bool:
    """``os`` 자체 또는 ``<무언가>.os`` (로드된 모듈 별칭) 참조인지."""
    if isinstance(node, ast.Name):
        return node.id == "os"
    return isinstance(node, ast.Attribute) and node.attr == "os"


def _os_name_mutation_lines(source: str, *, filename: str) -> list[int]:
    """전역 `os.name` 변이(setattr 호출·직접 대입) 행을 찾는다."""
    offenders: list[int] = []
    tree = ast.parse(source, filename=filename)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            is_setattr_call = (
                (isinstance(node.func, ast.Name) and node.func.id == "setattr")
                or (isinstance(node.func, ast.Attribute) and node.func.attr == "setattr")
            )
            if (
                is_setattr_call
                and len(node.args) >= 2
                and _is_os_module_reference(node.args[0])
                and _is_os_name_string(node.args[1])
            ):
                offenders.append(node.lineno)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "name"
                    and _is_os_module_reference(target.value)
                ):
                    offenders.append(node.lineno)
    return offenders


def _is_python_interpreter_first_element(node: ast.expr) -> bool:
    """argv 첫 요소가 `sys.executable`/`"py"`/`"python3"` 인지 (문자 그대로만·심볼릭 해석 없음)."""
    if isinstance(node, ast.Attribute):
        return (
            isinstance(node.value, ast.Name)
            and node.value.id == "sys"
            and node.attr == "executable"
        )
    return isinstance(node, ast.Constant) and node.value in _PYTHON_INTERPRETER_LITERALS


def _python_child_missing_encoding_lines(source: str, *, filename: str) -> list[int]:
    """python 자식 subprocess 호출이 text 모드만 켜고 encoding 을 안 밝히는 행을 찾는다."""
    offenders: list[int] = []
    tree = ast.parse(source, filename=filename)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in _SUBPROCESS_ATTRS:
            continue
        if not (isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess"):
            continue
        if not node.args:
            continue
        argv = node.args[0]
        if not isinstance(argv, (ast.List, ast.Tuple)) or not argv.elts:
            continue
        if not _is_python_interpreter_first_element(argv.elts[0]):
            continue

        keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg is not None}
        text_mode_on = any(
            isinstance(keywords.get(name), ast.Constant) and keywords[name].value is True
            for name in _TEXT_MODE_KEYWORDS
        )
        if text_mode_on and "encoding" not in keywords:
            offenders.append(node.lineno)
    return offenders


def test_tests_do_not_mutate_the_global_os_name():
    """tests/ 어디에도 전역 `os.name` 변이가 없다 — 모듈 seam patch 로만 분기를 태운다."""
    offenders: list[str] = []
    for path in sorted(TESTS.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        offenders.extend(
            f"{path.relative_to(TESTS.parent)}:{lineno}"
            for lineno in _os_name_mutation_lines(source, filename=str(path))
        )
    assert offenders == [], "전역 os.name 변이(Windows 에서 pathlib 붕괴):\n" + "\n".join(
        offenders
    )


def test_os_name_mutation_guard_is_sensitive_to_a_planted_violation():
    monkeypatch_call = 'monkeypatch.setattr(mod.os, "name", "nt")\n'
    assert _os_name_mutation_lines(
        f"import mod\n{monkeypatch_call}", filename="synthetic_violation.py"
    ) == [2]
    bare_setattr = 'setattr(os, "name", "posix")\n'
    assert _os_name_mutation_lines(
        f"import os\n{bare_setattr}", filename="synthetic_violation.py"
    ) == [2]
    direct_assign = "os.name = \"nt\"\n"
    assert _os_name_mutation_lines(
        f"import os\n{direct_assign}", filename="synthetic_violation.py"
    ) == [2]


def test_tests_python_child_subprocess_calls_declare_encoding():
    """python 자식(`sys.executable`/`py`/`python3`)을 text 모드로 읽는 호출은 encoding 명시."""
    offenders: list[str] = []
    for path in sorted(TESTS.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        offenders.extend(
            f"{path.relative_to(TESTS.parent)}:{lineno}"
            for lineno in _python_child_missing_encoding_lines(source, filename=str(path))
        )
    assert offenders == [], (
        "python 자식 subprocess 호출에 encoding 명시가 없습니다(Windows cp949 리더 죽음):\n"
        + "\n".join(offenders)
    )


def test_python_child_encoding_guard_is_sensitive_to_a_planted_violation():
    source = (
        "import subprocess, sys\n"
        "subprocess.run([sys.executable, '-c', 'pass'], capture_output=True, text=True)\n"
    )
    assert _python_child_missing_encoding_lines(
        source, filename="synthetic_violation.py"
    ) == [2]
    # encoding 을 밝히면 더 이상 걸리지 않는다.
    fixed = (
        "import subprocess, sys\n"
        "subprocess.run([sys.executable, '-c', 'pass'], capture_output=True, text=True, "
        "encoding='utf-8')\n"
    )
    assert _python_child_missing_encoding_lines(
        fixed, filename="synthetic_violation.py"
    ) == []
    # python 자식이 아닌 명령(git 등)은 text=True 만 있어도 이 가드 대상이 아니다.
    non_python_child = (
        "import subprocess\n"
        "subprocess.run(['git', 'status'], capture_output=True, text=True)\n"
    )
    assert _python_child_missing_encoding_lines(
        non_python_child, filename="synthetic_violation.py"
    ) == []
