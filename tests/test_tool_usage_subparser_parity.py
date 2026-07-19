"""도구 자기-`--help` usage 블록 ↔ 등록 subparser 정합 가드 (T-0348 · spike §F13 · 결정 ⑱).

엔진 CLI 중 일부는 모듈 docstring 에 **손-작성 `사용:` 블록**(서브커맨드 표)을 둔다. 이 손-작성
표는 `build_parser()` 가 실제 등록하는 subparser 집합과 *어긋날 수 있다*: 티켓이 새 서브커맨드를
추가하며 usage 표 갱신을 빠뜨리면 도구가 *자기 자신을 틀리게 설명*한다(스킬 md 가 엔진 자기
설명보다 정확한 역전 상태). 실측(T-0333/T-0295): `pm-config --help` usage 에 `worktree remove`·
`worktree prune-stale` 이 빠져 있었다(subparser 엔 있음).

이 `사용:` 블록이 `--help` 에 *실제로 뜨는지*는 도구마다 다르다 — 가드는 두 경우 다 지킨다:
  - **pm_config**: `ArgumentParser(description=__doc__, formatter_class=RawDescriptionHelpFormatter)`
    라 모듈 docstring(=`사용:` 블록 포함)이 `--help` 에 verbatim 표면화된다. drift = 문자 그대로
    *틀린 `--help`*.
  - **pm_log**: `build_parser()`(pm_log.py:233)가 짧은 하드코딩 `description=` 을 써서 `--help` 에
    모듈 docstring 이 안 뜬다. 여기선 가드가 "`--help` 표면"이 아니라 *소스-doc `사용:` 블록*
    (사람이 읽는 소스 문서)이 subparser 와 일관되게 유지되도록 지킨다.

이 가드는 그 drift 클래스를 상시 회귀로 닫는다 — 각 대상 도구에서:
  - **declared**: docstring `사용:` 블록의 각 줄에서 prog 뒤 서브커맨드 단어(literal)만 파싱해
    leaf 명령 경로 집합을 만든다(`|` 대안 확장·`<...>`/`[...]`/`--flag`/`#` 주석에서 멈춤).
  - **registered**: 그 도구 `build_parser()` 의 subparser 트리를 재귀 순회해 leaf 경로 집합.
  - 두 집합이 **양방향 일치**해야 한다. usage 에 있는데 파서에 없거나(오탈자) 파서에 있는데
    usage 에 없으면(누락) fail.

스킬 md↔CLI 가드(T-0347)와 **별개 표면**이다 — 이건 도구가 *자기 자신*을 설명하는 손-작성
help 이고, 저건 스킬 문서가 CLI 를 설명하는 표면이다.

대상 도구(스캔 판단·근거는 아래 `_TOOLS` 주석):
  - `pm_config` — 손-작성 `사용:` 블록 + subparsers. **drift 해소 대상**(T-0348).
  - `pm_log`    — 손-작성 `사용:` 블록 + subparsers. 현행 정합(가드가 지속 검증).
  - board.py/pm_update.py/domain.py/worktree_pool.py 는 **비대상** — board/domain/worktree_pool 은
    docstring 에 손-작성 서브커맨드 `사용:` 블록이 없다(usage=argparse 자동생성이라 원리적 drift
    불가), pm_update 는 `사용:` 블록이 있으나 flag 기반(subparsers 0)이라 대조 대상이 없다.

sensitivity(T-0347 동형): 스캔 대상 0·leaf 집합 空 이면 vacuous green — 이를 fail 로 못박고,
합성 drift 를 comparator 가 실제로 잡는지도 별도 단언한다(가드 no-op 방지).

registry completeness: `_TOOLS` 는 수동 하드코딩이라, 미래에 새 도구(또는 flag-only→subparsers
전환)가 `사용:` 블록 + `add_subparsers` 를 갖게 되어도 조용히 미스캔될 수 있다. 이를 막으려
`tools/*.py` 를 소스-텍스트로 전수 스캔(ast 파싱·**import 미실행**·부작용 0)해 두 조건을 만족하는
도구가 전부 `_TOOLS` 에 등재됐는지 별도 단언한다.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

# (module 이름, usage 블록에서 prog 으로 등장하는 토큰). prog 은 파서의 `parser.prog`
# 와 다를 수 있다(pm_log 는 usage 에 `python3 .../pm_log.py`, prog 은 `pm_log.py`) — usage
# 줄에서 이 prog 로 끝나는 토큰 뒤를 서브커맨드로 읽는다.
_TOOLS = [
    ("pm_config", "pm-config"),
    ("pm_log", "pm_log.py"),
    ("pm_adr", "pm_adr.py"),
]

# literal 서브커맨드 단어(소문자·숫자·하이픈; add-harness/prune-stale 포함). placeholder
# (`<...>`·`[...]`)·flag(`--...`)·주석(`#`)·CJK 는 매칭 안 됨 → 거기서 파싱을 멈춘다.
_CMD_WORD = re.compile(r"^[a-z][a-z0-9-]*$")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _registered_leaves(parser: argparse.ArgumentParser) -> set[tuple[str, ...]]:
    """build_parser() subparser 트리를 재귀 순회 → leaf 명령 경로 집합.

    subparser 가 없는 파서가 leaf 다(예: `status`·`repo add`·`worktree remove`). 상위
    그룹(`repo`·`worktree`·`upstream`)은 그 자체론 leaf 가 아니다.
    """
    leaves: set[tuple[str, ...]] = set()

    def walk(node: argparse.ArgumentParser, prefix: list[str]) -> None:
        subactions = [a for a in node._actions
                      if isinstance(a, argparse._SubParsersAction)]
        if not subactions:
            if prefix:
                leaves.add(tuple(prefix))
            return
        for sub in subactions:
            for name, subparser in sub.choices.items():
                walk(subparser, prefix + [name])

    walk(parser, [])
    return leaves


def _usage_command_tokens(doc: str, prog: str) -> list[list[str]]:
    """docstring `사용:` 블록에서 각 usage 줄의 *prog 뒤* 토큰 리스트를 뽑는다.

    블록 = `사용:` 줄 다음부터 첫 빈 줄 전까지. prog 으로 끝나는 토큰이 없는 줄(블록 내
    주석 등)은 건너뛴다.
    """
    out: list[list[str]] = []
    in_block = False
    for line in (doc or "").splitlines():
        stripped = line.strip()
        if not in_block:
            if stripped == "사용:":
                in_block = True
            continue
        if stripped == "":
            break
        tokens = stripped.split()
        anchor = None
        for i, token in enumerate(tokens):
            if token == prog or token.endswith("/" + prog):
                anchor = i
                break
        if anchor is None:
            continue
        out.append(tokens[anchor + 1:])
    return out


def _expand_alternation(words: list[str]) -> set[tuple[str, ...]]:
    """`|` 대안을 leaf 경로들로 확장.

    `status | whoami` → {(status,), (whoami,)} · `upstream show | set` →
    {(upstream, show), (upstream, set)}. `|` 없으면 단일 leaf.
    """
    if "|" not in words:
        return {tuple(words)}
    segments: list[list[str]] = []
    current: list[str] = []
    for word in words:
        if word == "|":
            segments.append(current)
            current = []
        else:
            current.append(word)
    segments.append(current)
    first = segments[0]
    prefix = first[:-1]
    leaves = {tuple(first)}
    for segment in segments[1:]:
        if segment:
            leaves.add(tuple(prefix) + tuple(segment))
    return leaves


def _declared_leaves(doc: str, prog: str) -> set[tuple[str, ...]]:
    """usage 블록 → 선언된 leaf 명령 경로 집합."""
    leaves: set[tuple[str, ...]] = set()
    for tokens in _usage_command_tokens(doc, prog):
        words: list[str] = []
        for token in tokens:
            if token == "|":
                words.append("|")
            elif _CMD_WORD.match(token):
                words.append(token)
            else:
                break  # placeholder·flag·주석 → 서브커맨드 나열 종료.
        if words:
            leaves |= _expand_alternation(words)
    return leaves


@pytest.mark.parametrize("name, prog", _TOOLS)
def test_usage_block_matches_subparsers(name: str, prog: str) -> None:
    """손-작성 `사용:` 블록의 서브커맨드 나열 ↔ 등록 subparser 집합이 양방향 일치."""
    module = _load(name)
    registered = _registered_leaves(module.build_parser())
    declared = _declared_leaves(module.__doc__, prog)

    # sensitivity: 대상 도구가 실제로 서브커맨드를 갖는다(파싱 실패로 인한 vacuous 방지).
    assert registered, f"{name}: build_parser() 에 등록된 subparser leaf 가 없다 (가드 vacuous)"
    assert declared, f"{name}: `사용:` 블록에서 파싱된 서브커맨드가 없다 (블록 헤더/prog 파싱 회귀?)"

    missing_in_usage = registered - declared
    unknown_in_usage = declared - registered
    assert not missing_in_usage and not unknown_in_usage, (
        f"{name}: usage 블록 ↔ subparser drift.\n"
        f"  subparser 엔 있으나 usage 누락: {sorted(missing_in_usage)}\n"
        f"  usage 엔 있으나 subparser 부재(오탈자): {sorted(unknown_in_usage)}\n"
        f"  → {name}.py 의 `사용:` docstring 블록과 build_parser() 를 정합시켜라."
    )


def test_scan_target_registry_non_vacuous() -> None:
    """스캔 대상 0 이면 이 가드는 아무것도 검증 안 한다 → 대상 목록 non-empty 못박기."""
    assert _TOOLS, "usage↔subparser 가드의 스캔 대상 도구 목록이 비었다 (guard no-op)"


def _tools_with_usage_block_and_subparsers() -> set[str]:
    """`tools/*.py` 중 top-level `사용:` 블록 + `add_subparsers` 를 둘 다 가진 모듈 stem 집합.

    탐지는 **소스 텍스트만** — `ast.parse`(import 미실행·부작용 0)로 모듈 docstring 을 뽑아
    독립 `사용:` 줄 유무를 보고, subparser 여부는 `add_subparsers` 소스 등장으로 판정한다.
    가벼운 발견 휴리스틱(should-fix·registry drift 방지)이다."""
    found: set[str] = set()
    for path in sorted(TOOLS.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        docstring = ast.get_docstring(ast.parse(source)) or ""
        has_usage_block = any(line.strip() == "사용:" for line in docstring.splitlines())
        if has_usage_block and "add_subparsers" in source:
            found.add(path.stem)
    return found


def test_all_usage_block_subparser_tools_are_registered() -> None:
    """`사용:` 블록 + subparsers 를 가진 도구가 전부 `_TOOLS` 에 등재됐는지 (수동 registry drift 가드).

    `_TOOLS` 는 손 하드코딩이라 새 도구(또는 flag-only→subparsers 전환)가 조용히 미스캔될 수 있다.
    `tools/*.py` 를 전수 소스-스캔(import 미실행)해 발견 집합과 등재 집합을 대조한다."""
    registered = {name for name, _ in _TOOLS}
    discovered = _tools_with_usage_block_and_subparsers()

    # sensitivity: 발견이 실제로 도구를 봤는지(glob 空/파싱 회귀로 vacuous 방지).
    assert discovered, "소스 스캔이 `사용:`+subparsers 도구를 하나도 못 찾음 (탐지 회귀?)"

    unregistered = discovered - registered
    assert not unregistered, (
        f"`사용:` 블록 + subparsers 를 가진 도구가 _TOOLS 미등재: {sorted(unregistered)} — "
        "usage↔subparser 가드 스캔 대상(_TOOLS)에 `(모듈명, prog)` 로 추가하라."
    )
    # 등재됐으나 더는 조건을 만족 안 하면(리팩터로 usage/subparser 제거) registry stale — 정리 신호.
    stale = registered - discovered
    assert not stale, (
        f"_TOOLS 에 등재됐으나 `사용:` 블록+subparsers 조건을 더는 안 만족: {sorted(stale)} — "
        "리팩터로 대상에서 빠졌으면 _TOOLS 에서 제거하라(registry stale)."
    )


def test_comparator_detects_synthetic_drift() -> None:
    """comparator 가 실제 drift 를 잡는지(가드 로직 non-vacuous) — 합성 파서/usage 로 확증.

    subparser 엔 있으나 usage 에서 누락한 leaf, usage 엔 있으나 파서에 없는 leaf 를
    각각 만들어 두 방향 모두 감지되는지 본다.
    """
    parser = argparse.ArgumentParser(prog="synthetic")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("alpha")
    grp = sub.add_parser("beta")
    grp_sub = grp.add_subparsers(dest="beta_command")
    grp_sub.add_parser("one")
    grp_sub.add_parser("two")

    registered = _registered_leaves(parser)
    assert registered == {("alpha",), ("beta", "one"), ("beta", "two")}

    # usage: alpha·beta one 만 나열(beta two 누락) + gamma 오탈자(파서에 없음).
    doc = (
        "사용:\n"
        "    synthetic alpha\n"
        "    synthetic beta one <x>\n"
        "    synthetic gamma\n"
        "\n"
        "명령:\n"
    )
    declared = _declared_leaves(doc, "synthetic")
    assert declared == {("alpha",), ("beta", "one"), ("gamma",)}

    assert registered - declared == {("beta", "two")}   # usage 누락 감지
    assert declared - registered == {("gamma",)}          # 오탈자 감지
