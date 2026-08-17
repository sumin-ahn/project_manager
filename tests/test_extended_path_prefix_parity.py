r"""경로 표기 직렬화 규칙의 **사본 대조** — 확장 길이 prefix 축은 pm_import 단일 (T-0712).

경로를 값(비교·기록·표시)으로 내보내는 자리는 지금 셋이다:

  `pm_import._path_notation_text`      — Windows 확장 길이 prefix(`\\?\`) 제거. 이 축의 유일한 입구는
                                          `os.readlink`(Windows 커널이 `\??\` 로 저장한 symlink 대상)이고
                                          그 호출은 pm_import 에만 있다.
  `pm_handoff._lease_slot_path_text`   — lease 슬롯 경로를 POSIX 표기로 직렬화(장부 키 산출·repo 상대화).
  `pm_bootstrap._display_path_text`    — 표시/대조 경로를 POSIX 표기로 직렬화.

셋은 "플랫폼 표기를 값에 남기지 않는다" 는 같은 목적을 갖지만 축이 다르다 — 뒤 둘은 **구분자**(`\` →
`/`), 앞 하나는 **prefix** 다. `as_posix()` 는 prefix 를 벗기지 않으므로(아래 값 단언) 한 함수로 합칠
수 없고, 반대로 prefix 규칙이 모듈마다 복제되면 반드시 갈라진다. 그래서 이 파일이 (a) prefix 규칙의
구현이 pm_import 하나뿐임을 지키고 (b) 세 직렬화가 같은 슬롯 경로에서 같은 문자열을 낸다는 것을
못박는다. 호출로 합칠 수는 없다 — pm_import 는 설치 진입점이라 다른 엔진 모듈을 import 하지 않는다
(자기 사본을 깔면서 도는 코드다).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path, PureWindowsPath

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

# 확장 길이 prefix 의 두 표기 — 소스 스캔과 값 단언이 같은 상수를 쓴다(손 재타이핑 금지).
EXTENDED_PREFIX = "\\\\?\\"
EXTENDED_PREFIX_SLASH = "//?/"
# 이 규칙을 구현해도 되는 유일한 모듈.
RULE_OWNER = "pm_import.py"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def modules():
    return _load("pm_import"), _load("pm_handoff"), _load("pm_bootstrap")


def _lines_with_prefix_literal(source: str) -> list[str]:
    """확장 prefix 표기를 담은 **코드 줄** — 순수 주석(`#`)은 규칙 사본이 아니므로 제외."""
    return [line for line in source.splitlines()
            if (EXTENDED_PREFIX in line or EXTENDED_PREFIX_SLASH in line)
            and not line.lstrip().startswith("#")]


def test_the_extended_prefix_rule_has_exactly_one_implementation():
    """prefix 규칙의 구현은 pm_import 하나다 — 사본이 생기면 여기서 걸린다.

    소유 모듈에 실제로 그 규칙이 **있다는** 값 단언을 함께 둔다. 없으면 "어디에도 없다" 가 통과해
    스캔이 무의미해진다([[guard-must-cover-its-own-surface]]).
    """
    owners = {}
    for path in sorted(TOOLS.glob("*.py")):
        lines = _lines_with_prefix_literal(path.read_text(encoding="utf-8"))
        if lines:
            owners[path.name] = lines

    assert RULE_OWNER in owners, "규칙 소유 모듈에서 확장 prefix 표기가 사라졌다(스캔 무효)"
    assert set(owners) == {RULE_OWNER}, (
        "확장 길이 prefix 규칙 사본: "
        f"{sorted(set(owners) - {RULE_OWNER})} — pm_import._path_notation_text 를 부르라")


def test_the_owner_module_strips_the_prefix_on_both_spellings(modules, monkeypatch):
    """소유 모듈의 규칙 값 단언 — 백슬래시 표기와 `as_posix()` 표기 둘 다 벗긴다."""
    pm_import, _handoff, _bootstrap = modules
    monkeypatch.setattr(pm_import, "_WINDOWS_PATH_NOTATION", True)

    assert pm_import._path_notation_text("\\\\?\\C:\\pm\\a.md") == "C:\\pm\\a.md"
    assert pm_import._path_notation_text("//?/C:/pm/a.md") == "C:/pm/a.md"


def test_posix_serialization_alone_does_not_close_the_prefix_axis(modules, monkeypatch):
    """`as_posix()` 는 prefix 를 남긴다 — 뒤 둘의 규칙을 재사용해서는 이 축이 안 닫힌다(분리 근거).

    이 값이 바뀌면(=구분자 직렬화가 prefix 까지 벗기게 되면) 세 번째 규칙을 둘 이유가 사라진다.
    """
    pm_import, _handoff, bootstrap = modules
    extended = PureWindowsPath("\\\\?\\C:\\pm\\a.md")

    assert extended.as_posix().startswith(EXTENDED_PREFIX_SLASH)
    assert bootstrap._display_path_text(extended).startswith(EXTENDED_PREFIX_SLASH)
    monkeypatch.setattr(pm_import, "_WINDOWS_PATH_NOTATION", True)
    assert not pm_import._path_notation_text(extended.as_posix()).startswith(
        EXTENDED_PREFIX_SLASH)


def test_the_three_serializers_agree_on_the_same_slot_path(modules, tmp_path):
    """같은 슬롯 경로를 셋이 같은 문자열로 직렬화한다 — 표기가 모듈마다 갈리지 않는다.

    prefix 가 없는 정상 입력이 세 함수의 공통 정의역이다(pm_import 는 prefix 축만 다루므로 구분자
    단일화는 `as_posix()` 와 조합해 비교한다).
    """
    pm_import, handoff, bootstrap = modules
    repo = tmp_path / "pm_home"
    slot = repo / "work" / "proj_1"
    slot.mkdir(parents=True)
    relative = Path("work/proj_1")

    texts = {
        "pm_handoff": handoff._lease_slot_path_text(slot, repo),
        "pm_bootstrap": bootstrap._display_path_text(relative),
        "pm_import": Path(pm_import._path_notation_text(relative)).as_posix(),
    }

    assert set(texts.values()) == {"work/proj_1"}, texts
