"""스킬 md ↔ CLI 커맨드 **존재** 정합 가드 (T-0347 · spike §F13 · 결정 ⑱ · [[ADR-0062]]).

배경 — **실패 모드가 기능적이다**: 스킬 문서(`.claude/skills/**/SKILL.md` — 양 하네스가
opencode skill/command 표면 포함)의 ```bash 블록은 채택자·PM 이 그대로 복붙해 도는 실행 지시다. 거기 적힌
서브커맨드·플래그가 CLI 파서에서 사라지거나 이름이 바뀌면(예 v1.2.0 이 `--session` 제거·ADR-0057)
그 지시는 런타임에 **깨진다**. 살아있는 실사례 [[T-0346]](pm-handoff 가이드 `--tickets` 형식이 CLI
실형식과 어긋난 live drift)·물린 전례 [[T-0324]](구 `--session` 하드코딩이 두 가드 사각을 통과해
라이브 tier 에서야 릴리즈 블로커로 잡힘)가 이 클래스다. task 모델(v1.3.0)이 가이드 표면을 대폭
바꾸므로 같은 drift 가 재발 예약돼 있다.

이 가드는 그 표면을 lock-in 한다 — 스킬 md 의 각 커맨드가 실제 파서에 **존재**하는지를 도구의
`build_parser()`(기존 `importlib.util.spec_from_file_location` 관례) introspection 으로 대조한다.
값이 canonical 인지(예 `--slot` 값이 옛 형식인지)는 **별개 표면**(T-0262/0263·값 가드)이고, 도구
자체 `--help` usage ↔ subparser 정합도 **별개 표면**([[T-0348]]) — 이 파일은 스킬 md ↔ CLU **존재**
정합만 본다.

세 가지 커맨드 모양을 인식한다:
  - `python3 .project_manager/tools/<X>.py <sub> --flag …` → 그 도구 파서를 로드해 서브커맨드
    (subparsers·positional choices)·플래그 존재 확인. placeholder(`<repo>`·`T-NNNN` 등)는 값 무시·
    구조만 검사.
  - `./pm-config.sh <sub> …`(및 pm-update/pm-import 파사드) → 디스패처 대상 도구 파서로 동일 검사.
  - `/pm-<skill> …`(슬래시) → 그 스킬(또는 파사드 진입점)이 실재하는지 확인. 슬래시는 prose 상호
    참조로 살기에 md 전문에서 추출한다(bash 블록 밖).

**sensitivity 필수** — 추출된 스캔 대상이 0 이면 test 실패(공허 가드 재발 방지). v1.2.0 이 `--session`
을 제거하자 그 값만 보던 가드([[test_adapter_session_identity]])가 스캔 대상 0 = green 이지만 아무것도
안 지키는 상태가 됐다 — 그 공허 함정을 이 가드는 대상 수·표면 커버리지·분류기 자기검증으로 봉쇄한다.
(그 공허해진 `--session` 값 가드는 이 존재 가드가 흡수한다 — 없는 플래그를 쓰면 존재 검사에서 먼저
걸린다. `--session` 자체의 재유입은 `test_flag_unification_parity` group 2 가 넓은 .md 표면에서 백스톱.)
"""
from __future__ import annotations

import argparse
import importlib.util
import re
import shlex
from pathlib import Path

from _repo_owned_inventory import OWNED, repo_owned_paths

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

# 스캔 표면 — canonical 스킬 + 양 하네스 templates 미러 + opencode 슬래시 command 사본.
# command는 canonical에서 기계 생성되지만 채택자가 실제로 실행하는 독립 표면이므로 포함한다.
# templates/* 는 root `.claude/skills` 의 pm_update --target 동기 미러라, 미러가 drift 하면
# (전파 누락) 거기서 잡히는 것이 곧 파리티 가드다.
_SCAN_DIRS = (
    ".claude/skills",
    "templates/claude_code/.claude/skills",
    "templates/opencode/.claude/skills",
    "templates/opencode/.opencode/command",
)

# 파사드 셸 디스패처(`./pm-config.sh …`)는 인자를 verbatim 으로 대상 도구에 forward 한다 —
# 서브커맨드/플래그 존재 검사는 그 도구 파서로 동일하게 한다.
_FACADE_TO_TOOL = {
    "pm-config.sh": "pm_config", "pm-config.cmd": "pm_config",
    "pm-update.sh": "pm_update", "pm-update.cmd": "pm_update",
    "pm-import.sh": "pm_import", "pm-import.cmd": "pm_import",
}
# 슬래시 참조 유효 집합에 더할 파사드 진입점 이름(`/pm-import` = pm-import.sh 가이드 참조 —
# 스킬 디렉토리는 아니지만 실재하는 문서화된 진입점이다).
_FACADE_STEMS = frozenset(name.rsplit(".", 1)[0] for name in _FACADE_TO_TOOL)

_BASH_BLOCK = re.compile(r"```bash\n(.*?)```", re.DOTALL)
# `python3 …/tools/X.py <rest>` — 앞에 경로 prefix(`\S*`)·런처(python3/py -3.12)를 허용.
_PY_TOOL = re.compile(
    r"(?:python3?|py(?:\s+-[\d.]+)?)\s+\S*\.project_manager/tools/(\w+)\.py\b(.*)"
)
# `./pm-config.sh <rest>` — 파사드 디스패처.
_FACADE = re.compile(r"\./(pm-\w+\.(?:sh|cmd))\b(.*)")
# `/pm-<skill>`/`/spike-<skill>` 슬래시 — 파일 경로(`./pm-config.sh`·`pm-import.py`)의 `/pm-…` 부분
# 오매치 방지: 앞이 `.`/단어문자가 아니고, 뒤가 `.`(확장자)/단어문자가 아닐 때만.
_SLASH = re.compile(r"(?<![.\w])/((?:pm|spike)-[a-z0-9-]+)(?![.\w])")

# 파서 로드 캐시(도구당 1회). 도구는 패키지가 아니므로 importlib 경로 로드.
_parser_cache: "dict[str, argparse.ArgumentParser | None]" = {}


class _ParserCaptured(Exception):
    """`main()` 인라인 파서 캡처용 sentinel — parse_args 직전에 파서를 낚아채 short-circuit."""


def _load_module(stem: str):
    spec = importlib.util.spec_from_file_location(stem, TOOLS / f"{stem}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_parser(stem: str) -> "argparse.ArgumentParser | None":
    """도구 `stem` 의 argparse 파서를 얻는다 — `build_parser()` 우선, 없으면 `main()` 캡처.

    대부분 도구는 `build_parser()` 를 노출한다(test_flag_unification_parity 등 관례). 일부
    (worktree_pool·pm_update)는 파서를 `main()` 내부에 인라인으로 짓는다 — 그 경우
    `parse_args` 를 일시 후킹해 완성된 파서를 낚아챈다(부작용 없음: 두 도구 모두 parse_args
    이전에 네트워크/쓰기가 없음을 소스 확인). 얻지 못하면 None → 호출측이 fail-loud 로 표면화
    (조용한 skip = 공허 가드라 금지).
    """
    if stem in _parser_cache:
        return _parser_cache[stem]
    parser: "argparse.ArgumentParser | None"
    mod = _load_module(stem)
    if hasattr(mod, "build_parser"):
        parser = mod.build_parser()
    else:
        captured: "dict[str, argparse.ArgumentParser]" = {}
        original = argparse.ArgumentParser.parse_args

        def _spy(self, *args, **kwargs):  # noqa: ANN001
            captured["parser"] = self
            raise _ParserCaptured

        argparse.ArgumentParser.parse_args = _spy  # type: ignore[assignment]
        try:
            mod.main([])
        except _ParserCaptured:
            pass
        except SystemExit:
            pass
        finally:
            argparse.ArgumentParser.parse_args = original  # type: ignore[assignment]
        parser = captured.get("parser")
    _parser_cache[stem] = parser
    return parser


def _subparsers_action(parser: argparse.ArgumentParser):
    """parser 의 `_SubParsersAction`(있으면) — `test_board_list_scope.py` 관용구."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _positional_choices(parser: argparse.ArgumentParser) -> "set[str] | None":
    """parser 의 첫 constrained positional(choices 보유)의 선택지 — 없으면 None.

    board `regression` 은 서브파서가 아니라 positional `action`(`choices=["run","check"]`)으로
    구조가 갈린다 — 이걸 검사해야 `regression runn`(오타/제거)을 잡는다.
    """
    for action in parser._actions:
        if (not action.option_strings
                and not isinstance(action, argparse._SubParsersAction)
                and action.choices):
            return {str(choice) for choice in action.choices}
    return None


def _own_option_strings(parser: argparse.ArgumentParser) -> "set[str]":
    """parser 자신이 직접 정의한 옵션 플래그(서브파서 항 제외)."""
    opts: "set[str]" = set()
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            opts.update(action.option_strings)
    return opts


def _is_placeholder(token: str) -> bool:
    """서브커맨드 자리의 토큰이 값/자리표시(검사 skip)인지 — `<repo>`·`T-NNNN`·셸 구분자."""
    return token.startswith("<") or token.startswith("T-") or token in ("|", "||", "&&")


def _flag_of(token: str) -> "str | None":
    """토큰이 플래그면 정규화한 플래그명(`--foo`) 반환, 아니면 None.

    `[--force]`(문서 optional 표기) → `--force`, `--slot=<N>`(등호형) → `--slot`. 값 자리표시
    `<...>` 는 플래그 아님.
    """
    tok = token
    if tok.startswith("[") and tok.endswith("]"):
        tok = tok[1:-1]
    if tok.startswith("-") and not tok.startswith("<"):
        flag = tok.split("=", 1)[0]
        if flag not in ("-", "--"):
            return flag
    return None


def _join_line_continuations(block: str) -> "list[str]":
    """`\\` 로 끝나는 줄을 다음 줄과 합쳐 논리 커맨드 단위로(멀티라인 pm_handoff 등)."""
    lines: "list[str]" = []
    buffer = ""
    for raw in block.splitlines():
        if raw.rstrip().endswith("\\"):
            buffer += raw.rstrip()[:-1] + " "
        else:
            buffer += raw
            lines.append(buffer)
            buffer = ""
    if buffer:
        lines.append(buffer)
    return lines


def _tokenize(rest: str) -> "list[str]":
    """커맨드 나머지를 **quote-aware** 로 토큰화(shlex·인라인 주석 제거).

    순진한 공백 split 은 quoted 값 안의 부분을 실 플래그로 **오탐**한다 —
    `--test "pytest --tb=short"` 는 `--tb` 를 미등록 플래그로 잘못 잡는다(reviewer 실측). shlex 는
    인용을 존중해 `pytest --tb=short` 를 한 토큰(=`--test` 의 값)으로 유지하고 `#…` 인라인 주석을
    떼어낸다. 비셸 문법(불균형 인용 등)은 조용히 skip 하지 않고 `ValueError` 로 **fail-loud** 한다
    (그 커맨드를 검사 못 함을 감춰 공허 가드가 되는 것을 금지 — 문서를 고쳐야 한다는 신호).
    """
    return shlex.split(rest, comments=True)


def check_command(parser: argparse.ArgumentParser, tokens: "list[str]") -> "list[str]":
    """`tokens`(도구/파사드 뒤 인자)를 `parser` 로 존재 검사 → offender 사유 목록(빈=정합).

    서브커맨드(중첩 subparsers)·constrained positional(choices)을 앞에서부터 walk 하며 대조하고,
    남은 토큰의 플래그를 walk 경로(root→최심 서브)의 옵션 합집합과 대조한다. 값·자유 positional
    은 존재 검사 대상 아님(placeholder 는 값 무시·구조만).
    """
    offenders: "list[str]" = []
    path = [parser]
    current = parser
    idx = 0
    while idx < len(tokens):
        token = tokens[idx]
        if _flag_of(token) or token.startswith("-"):
            break  # 플래그 구간 시작 — 서브커맨드 walk 종료.
        sub_action = _subparsers_action(current)
        if sub_action is not None:
            if _is_placeholder(token):
                break  # 서브커맨드 자리에 placeholder — 구조만, 값 검사 skip.
            if token in sub_action.choices:
                current = sub_action.choices[token]
                path.append(current)
                idx += 1
                continue
            offenders.append(f"미등록 서브커맨드 {token!r}")
            return offenders
        pos_choices = _positional_choices(current)
        if pos_choices is not None:
            if _is_placeholder(token):
                idx += 1
                break
            if token in pos_choices:
                idx += 1
                break
            offenders.append(
                f"미등록 action/값 {token!r} (choices {sorted(pos_choices)})"
            )
            return offenders
        break  # 자유 positional 구간 — 구조 검사 종료.
    valid = set()
    for node in path:
        valid |= _own_option_strings(node)
    for token in tokens[idx:]:
        flag = _flag_of(token)
        if flag and flag not in valid:
            offenders.append(f"미등록 플래그 {flag!r}")
    return offenders


def _iter_md_files() -> "list[Path]":
    files: "list[Path]" = []
    for rel in _SCAN_DIRS:
        base = REPO / rel
        if base.is_dir():
            files.extend(
                path
                for path in repo_owned_paths(REPO, rel, mode=OWNED)
                if path.suffix == ".md"
            )
    return files


def _extract_bash_commands(text: str) -> "list[tuple[str, list[str]]]":
    """텍스트의 ```bash 블록에서 (도구stem, 토큰) 커맨드 목록 — python 도구 + 파사드."""
    commands: "list[tuple[str, list[str]]]" = []
    for block in _BASH_BLOCK.findall(text):
        for line in _join_line_continuations(block):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            py_match = _PY_TOOL.search(stripped)
            facade_match = _FACADE.match(stripped)
            if py_match:
                commands.append((py_match.group(1), _tokenize(py_match.group(2))))
            elif facade_match:
                stem = _FACADE_TO_TOOL.get(facade_match.group(1))
                if stem:
                    commands.append((stem, _tokenize(facade_match.group(2))))
    return commands


def _canonical_skill_names() -> "set[str]":
    """실재하는 스킬/슬래시 이름 집합 ∪ 파사드 진입점."""
    names: "set[str]" = set(_FACADE_STEMS)
    for rel in (".claude/skills", "templates/claude_code/.claude/skills",
                "templates/opencode/.claude/skills"):
        base = REPO / rel
        if base.is_dir():
            for sub in base.iterdir():
                if sub.is_dir() and (sub / "SKILL.md").is_file():
                    names.add(sub.name)
    command_dir = REPO / "templates/opencode/.opencode/command"
    if command_dir.is_dir():
        names.update(md.stem for md in command_dir.glob("*.md"))
    return names


# ════════════════════════════════════════════════════════════════════════════
# 실 스캔 — 스킬 md ↔ CLI 존재 정합
# ════════════════════════════════════════════════════════════════════════════


def test_skill_bash_commands_reference_existing_cli():
    """모든 스킬 md ```bash 커맨드의 서브커맨드·플래그가 실 파서에 존재하는지 (T-0347).

    실패 모드는 기능적 — 사라진/개명된 서브커맨드·플래그를 지시하는 스킬 md 는 채택자가 복붙하면
    런타임에 깨진다(T-0346 클래스). 파서 introspection 으로 존재를 대조한다.
    """
    files = _iter_md_files()
    assert files, (
        "scope sanity: 스캔 대상 스킬 md 를 0개 찾음 — _SCAN_DIRS 가 stale 이다. 실 트리에 맞춰 갱신."
    )
    total = 0
    offenders: "list[str]" = []
    surfaces_with_targets: "set[str]" = set()
    for md in files:
        try:
            surface = next(s for s in _SCAN_DIRS if (REPO / s) in md.parents)
        except StopIteration:  # pragma: no cover - _iter_md_files 가 _SCAN_DIRS 하위만 냄
            surface = md.as_posix()
        for stem, tokens in _extract_bash_commands(md.read_text(encoding="utf-8")):
            total += 1
            surfaces_with_targets.add(surface)
            parser = _load_parser(stem)
            if parser is None:
                offenders.append(
                    f"{md.relative_to(REPO).as_posix()}: {stem}.py 파서를 얻지 못함"
                    " (build_parser 부재·main 캡처 실패) — 존재 검사 불가"
                )
                continue
            for reason in check_command(parser, tokens):
                offenders.append(f"{md.relative_to(REPO).as_posix()}: {stem} — {reason}")

    # sensitivity: 스캔 대상 0 = 공허 가드 → 실패.
    assert total > 0, (
        "sensitivity: 스킬 md ```bash 에서 커맨드를 0개 추출 — 추출 정규식/스캔 경로가 stale 하거나"
        " 스킬 md 구조가 바뀌었다. 공허 통과(green 이지만 무보증)를 막기 위해 실패시킨다."
    )
    # templates 전수: 네 표면(canonical·claude_code·opencode skill·opencode command)이
    # 모두 실 커맨드를 냈는지 — 한 표면이라도 0 이면 파리티 커버리지 구멍.
    assert surfaces_with_targets == set(_SCAN_DIRS), (
        "sensitivity: 커맨드를 낸 표면이 전수(_SCAN_DIRS)가 아님 — "
        f"{sorted(set(_SCAN_DIRS) - surfaces_with_targets)} 표면이 스캔에서 비었다(파리티 구멍)."
    )
    assert not offenders, (
        "스킬 md 가 실 파서에 없는 서브커맨드/플래그를 지시한다 — 채택자 복붙 시 런타임에 깨진다.\n"
        "CLI 실형식에 맞춰 스킬 md 를 정정하라(값 canonical 은 별개 표면·T-0262/0263):\n  "
        + "\n  ".join(offenders)
    )


def test_existence_classifier_catches_drift():
    """존재 검사 분류기 자기검증 — 실 트리와 무관한 합성 입력으로(가짜 게이트 방지).

    이게 없으면 `check_command` 가 *무엇이든* 통과시켜도(offender 항상 빈) 위 스캔이 green 이라
    공허해진다. 미등록 서브커맨드·미등록 action·미등록 플래그를 실제로 잡고, 정상형은 통과함을
    실 파서로 못박는다.
    """
    board = _load_parser("board")
    handoff = _load_parser("pm_handoff")
    worktree = _load_parser("worktree_pool")
    config = _load_parser("pm_config")
    assert board is not None and handoff is not None
    assert worktree is not None and config is not None

    # catch — 미등록 서브커맨드.
    assert check_command(board, _tokenize("claimm T-1 --repo <repo>")) == [
        "미등록 서브커맨드 'claimm'"
    ]
    # catch — 미등록 플래그.
    assert check_command(board, _tokenize("claim T-1 --nonexistent x")) == [
        "미등록 플래그 '--nonexistent'"
    ]
    # catch — constrained positional(regression action) 오타.
    assert check_command(board, _tokenize("regression runn --ticket T-1")) == [
        "미등록 action/값 'runn' (choices ['check', 'run'])"
    ]
    # catch — 인라인 파서(worktree_pool·main 캡처) 미등록 서브커맨드.
    assert check_command(worktree, _tokenize("devv <s> <b> --slot work/<repo>_<N>")) == [
        "미등록 서브커맨드 'devv'"
    ]
    # catch — 중첩 파사드 서브커맨드 오타.
    assert check_command(config, _tokenize("reo add <name>")) == [
        "미등록 서브커맨드 'reo'"
    ]

    # pass — 정상형(placeholder 값 무시·구조만·중첩·positional choices·bracket optional).
    assert check_command(board, _tokenize("claim T-NNNN --repo <repo> --slot <N>")) == []
    assert check_command(board, _tokenize("regression run --ticket T-pay-001")) == []
    assert check_command(board, _tokenize("show T-NNNN")) == []
    assert check_command(handoff, _tokenize("--session-seq <N> --wave-summary x")) == []
    assert check_command(worktree, _tokenize("sync --slot work/<repo>_<N>")) == []
    assert check_command(config, _tokenize("repo add <name> --git <url> --test x")) == []
    assert check_command(config, _tokenize("worktree remove <slot> [--force]")) == []


def test_tokenizer_is_quote_aware_no_false_flag():
    """토크나이저 자기검증 — quoted 값 안의 플래그를 오탐하지 않는다 (reviewer 실측 수렴).

    순진한 공백 split 은 `--test "pytest --tb=short"` 에서 `--tb` 를 미등록 플래그로 오탐한다
    (`repo add` 는 실제 `--test "<cmd>"` 를 받으므로 라이브 false positive). shlex 로 인용을
    존중하면 `pytest --tb=short` 는 `--test` 의 값 한 토큰이라 플래그로 새지 않는다. 파사드
    `pm-config repo add` 실 파서로 offender 0 임을 못박고, malformed(비셸 문법)는 fail-loud.
    """
    # quoted 값 안의 `-q`/`--tb=short` 는 값이지 플래그가 아니다 — 별개 토큰으로 새지 않는다.
    assert _tokenize('repo add <name> --git <url> --test "pytest -q --tb=short"') == [
        "repo", "add", "<name>", "--git", "<url>", "--test", "pytest -q --tb=short",
    ]
    config = _load_parser("pm_config")
    assert config is not None
    # 실 파서로 존재 검사 — 오탐(`--tb`/`-q` 미등록 플래그) 이 나오면 안 된다.
    assert check_command(
        config, _tokenize('repo add <name> --git <url> --test "pytest -q --tb=short"')
    ) == []
    # fail-loud — 불균형 인용(비셸 문법)은 조용히 skip 하지 않고 ValueError 로 표면화.
    import pytest

    with pytest.raises(ValueError):
        _tokenize('--test "unterminated value')


def test_referenced_tools_all_expose_a_parser():
    """스킬 md 가 지시하는 모든 도구가 introspect 가능한 파서를 실제로 노출하는지 (sensitivity).

    파서를 못 얻는 도구가 있으면 그 커맨드의 존재 검사가 조용히 skip 돼 가드가 부분 공허해진다 —
    그 상황을 fail-loud 로 못박는다(build_parser 부재 시 main 캡처 폴백이 살아있음을 실증).
    """
    referenced: "set[str]" = set()
    for md in _iter_md_files():
        for stem, _tokens in _extract_bash_commands(md.read_text(encoding="utf-8")):
            referenced.add(stem)
    assert referenced, "sensitivity: 참조 도구 0 — 추출이 공허하다."
    unparseable = sorted(stem for stem in referenced if _load_parser(stem) is None)
    assert not unparseable, (
        "다음 도구의 파서를 introspect 하지 못함(build_parser 부재 + main 캡처 실패) — 존재 검사가"
        f" 조용히 skip 된다: {unparseable}"
    )


# ════════════════════════════════════════════════════════════════════════════
# 슬래시 커맨드 존재 — `/pm-<skill>` 상호참조가 실재 스킬을 가리키는지
# ════════════════════════════════════════════════════════════════════════════


def _slash_refs(text: str) -> "set[str]":
    return {m.group(1) for m in _SLASH.finditer(text)}


def test_slash_skill_references_exist():
    """스킬 md 의 `/pm-<skill>`/`/spike-<skill>` 상호참조가 실재 스킬/진입점을 가리키는지 (T-0347).

    슬래시는 prose 상호참조(예 "`/pm-wave-finish` 호출 전")로 사는 커맨드 모양이라 md 전문에서
    추출한다. 제거·개명된 스킬을 가리키는 dangling 참조를 잡는다.
    """
    valid = _canonical_skill_names()
    total = 0
    offenders: "list[str]" = []
    for md in _iter_md_files():
        for name in _slash_refs(md.read_text(encoding="utf-8")):
            total += 1
            if name not in valid:
                offenders.append(f"{md.relative_to(REPO).as_posix()}: /{name}")
    assert total > 0, (
        "sensitivity: 슬래시 참조를 0개 추출 — _SLASH 정규식/스캔 경로가 stale. 공허 통과 방지 실패."
    )
    assert not offenders, (
        "실재하지 않는 스킬을 가리키는 슬래시 참조(dangling) — 스킬 개명/제거 후 참조 미갱신.\n"
        "실 스킬명으로 정정하라:\n  " + "\n  ".join(offenders)
    )


def test_slash_classifier_catches_unknown_skill():
    """슬래시 존재 판별 자기검증 — 합성 입력으로(가짜 게이트 방지).

    실재 스킬은 유효 집합에 있고, 없는 스킬(`/pm-nonexistent`)은 걸리고, 파일 경로의
    `/pm-…`(`./pm-config.sh`·`pm-import.py`) 오매치는 슬래시로 안 잡히는지 못박는다.
    """
    valid = _canonical_skill_names()
    assert "pm-wave-finish" in valid
    assert "pm-bootstrap" in valid
    assert "pm-import" in valid  # 파사드 진입점도 유효.
    assert "pm-nonexistent" not in valid

    # 실 슬래시 참조는 추출된다.
    assert _slash_refs("`/pm-wave-finish` 호출 전 baseline 확인") == {"pm-wave-finish"}
    assert _slash_refs("세션 시작은 `/pm-bootstrap`") == {"pm-bootstrap"}
    # 없는 스킬은 추출되나 유효 집합에 없음 → offender.
    bogus = _slash_refs("`/pm-nonexistent` 로 위임")
    assert bogus == {"pm-nonexistent"}
    assert not (bogus <= valid)
    # 파일 경로의 `/pm-…` 는 슬래시 커맨드가 아니다 → 오매치 안 됨.
    assert _slash_refs("`./pm-config.sh status` 로 확인") == set()
    assert _slash_refs("`.project_manager/tools/pm_import.py`") == set()
