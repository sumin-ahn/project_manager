"""커맨드 카드 ↔ CLI 정합 가드 (T-0362 · spike §F12 · 결정 ⑰).

커맨드 카드(ADR-0045)의 커맨드 토큰은 **공용 정의서**(`pm_bootstrap._CARD_MODE_CLI` ·
`_CardCmd` records) 단일 진실에서 온다 — 손 문자열 하드코딩을 제거해 "가이드가 실제 옵션과
다른" drift 를 구조적으로 불가능하게 만든다(⑰·⑭ PM 실수 기계 차단). task 모델(`--task`·alloc/
release/task end/prefix·readonly refresh)이 표면을 대폭 바꾸는데 카드를 손 갱신하면 정확히 그
drift 가 재발한다 — 이 가드가 **양방향**으로 못박는다:

  - **정의서 → 파서** (카드 토큰이 파서에 있나): 각 record 의 서브커맨드 leaf(`subpath`)가 그
    도구 `build_parser()` 의 실 등록 leaf 이고, 비-정체성 flag 가 그 leaf 에 실 등록됐는지
    introspection 으로 검증(T-0348 `_registered_leaves` 방식 재사용). 카드가 옵션 rename·삭제로
    어긋나면 red.
  - **정의서 → 카드** (정의서 커맨드가 카드에 렌더되나): 각 모드 카드에서 CLI 줄을 추출해
    그 모드 정의서와 **(도구, base-render) 집합**이 **정확히 일치**하는지(누락·잉여 둘 다 red).
    granularity = (tool, render) — leaf-set 은 같은 leaf 의 render 변종 제거를 통과시켜(reviewer
    should-fix·mutation 실증) render 급으로 올렸다(정체성 실값만 벗기고 base render 정확 대조).
  - **파서 → 정의서** (신규 커맨드가 카드에 표면화됐나): task/readonly 신규 parser-backed 커맨드
    (alloc·release·task end·task prefix·status)가 정의서에 covered 됐는지 **curated spot-check** —
    완전 generic(모든 파서 leaf 강제)은 부적절(카드=curated 표면·전부 뿌리면 컨텍스트 잠식). 한계·
    근거는 `test_task_mode_covers_new_task_commands` docstring. worktree_pool refresh/rebase 는
    build_parser 없는 스킬-렌더라 이 required 대상 아님.

**sensitivity**(공허 가드 방지·PM 21 T-0112·F13 `--session` 공허 통과 실증): 대상 record 수>0 +
합성 drift(없는 flag·없는 leaf)를 comparator 가 실제로 잡는지 별도 단언.

**templates 사본 전수**: 엔진 canonical(루트) + 출하 템플릿(claude_code·opencode)의 pm_bootstrap
사본이 *각자의* board/pm_config 파서와 정합하는지 트리별 parametrize(사본 stale=drift 클래스).

skill(`/pm-…`) 줄은 CLI 가 아니라 대상 밖(backbone 강등 `python3 …` 줄만 파서-검증). worktree_pool
(build_parser 없음)·external_review(build_parser 없음)는 파서-backed 도구가 아니라 leaf/flag 강제
검증에서 제외하되, 정의서 record 로는 카드↔정의서 대조에 남는다(카드가 지어낸 도구 방지).
"""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# 엔진 canonical + 출하 템플릿 사본 (templates 사본 전수·drift-0 검증).
_TREES = {
    "root": REPO / ".project_manager" / "tools",
    "claude_code": REPO / "templates" / "claude_code" / ".project_manager" / "tools",
    "opencode": REPO / "templates" / "opencode" / ".project_manager" / "tools",
}

# 파서-backed 도구 (build_parser 존재) — 정의서 leaf/flag 를 실 argparse 로 검증할 대상.
_PARSER_TOOLS = ("board.py", "pm_config.py", "pm_handoff.py",
                 "ticket_finish.py", "pm_log.py", "domain.py")

# 정체성 축 flag (ADR-0057) — leaf-등록 여부와 무관하게 허용(identity_args grammar 로 별도 검증됨).
_IDENTITY_FLAGS = frozenset({"--repo", "--slot", "--task"})


def _load(tools_dir: Path, name: str):
    spec = importlib.util.spec_from_file_location(f"{tools_dir.parent.parent.name}_{name}",
                                                  tools_dir / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _leaf_options(parser: argparse.ArgumentParser) -> dict[tuple, set]:
    """build_parser() subparser 트리 → {leaf 경로: 그 leaf 의 option_strings 집합}.

    subparser 없는 파서가 leaf(예: `list`·`prefix rename`·flag-only 도구는 leaf `()`). leaf 의
    옵션은 그 leaf 파서 `_actions` 의 option_strings 합집합(-h/--help 제외).
    """
    out: dict[tuple, set] = {}

    def opts(node) -> set:
        return {o for a in node._actions for o in a.option_strings if o not in ("-h", "--help")}

    def walk(node, prefix: list) -> None:
        subs = [a for a in node._actions if isinstance(a, argparse._SubParsersAction)]
        if not subs:
            out[tuple(prefix)] = opts(node)
            return
        for sub in subs:
            for name, subparser in sub.choices.items():
                walk(subparser, prefix + [name])

    walk(parser, [])
    return out


def _leaves_by_tool(tools_dir: Path) -> dict[str, dict[tuple, set]]:
    """파서-backed 도구별 {leaf: options}. 도구명(파일명) → leaf-options 맵."""
    result: dict[str, dict[tuple, set]] = {}
    for tool in _PARSER_TOOLS:
        mod = _load(tools_dir, tool[:-3])  # strip ".py"
        result[tool] = _leaf_options(mod.build_parser())
    return result


def _resolve_leaf(leaf_options: dict[tuple, set], tokens: list[str]) -> tuple:
    """카드 커맨드 토큰열 → 등록 leaf 경로(subparser 선두 토큰 greedy 소비).

    flag(`-`)·placeholder(`<`)·미등록 토큰에서 멈춘다. 파서-backed 도구의 leaf 집합 기준.
    """
    known_first = {leaf[0] for leaf in leaf_options if leaf}
    path: list[str] = []
    for tok in tokens:
        candidate = tuple(path + [tok])
        # 다음 토큰이 leaf 확장(또는 그 prefix)일 때만 소비.
        if any(leaf[:len(candidate)] == candidate for leaf in leaf_options if leaf):
            path.append(tok)
            continue
        break
    return tuple(path)


# ── 카드 CLI 줄 추출 ─────────────────────────────────────────────────────────

_CARD_PREFIX = "python3 .project_manager/tools/"


def _card_cli_commands(card: str) -> list[tuple[str, list[str]]]:
    """카드에서 `python3 .project_manager/tools/<tool> …` 줄만 (tool, arg-tokens) 로 추출.

    engine 강등 줄(2-스페이스 들여쓰기)·주석(`# …`)을 벗겨 CLI 인자만. skill(`/pm-…`) 줄·평문
    note(`↳ …`)는 python3 로 시작 안 해 제외(파서-검증 대상은 backbone python3 줄뿐).
    """
    out: list[tuple[str, list[str]]] = []
    for raw in card.splitlines():
        s = raw.strip()
        if not s.startswith(_CARD_PREFIX):
            continue
        rest = s[len(_CARD_PREFIX):]
        rest = rest.split("  #", 1)[0].rstrip()
        parts = rest.split()
        if not parts:
            continue
        out.append((parts[0], parts[1:]))
    return out


def _render_mode_card(bootstrap, mode: str) -> str:
    """모드별 카드 렌더 — slot / task / readonly."""
    inst = bootstrap.PmBootstrap.__new__(bootstrap.PmBootstrap)
    if mode == "slot":
        return inst._build_command_card_markdown(
            {"repo": "project_manager", "session": "project_manager_1", "branch": "b"})
    if mode == "task":
        inst._task_name = "refactor"
        return inst._build_command_card_markdown(
            {"repo": "project_manager", "session": "project_manager_1", "branch": "b"})
    if mode == "readonly":
        return inst._build_command_card_markdown(
            {"role": "readonly", "slot": "work/project_manager_3"})
    raise AssertionError(mode)


# ── 1. 정의서 → 파서 (카드 토큰이 실 CLI 에 있나·양방향 forward) ────────────────


@pytest.mark.parametrize("tree", list(_TREES))
def test_definition_leaves_and_flags_exist_in_parser(tree):
    """모든 정의서 record 의 leaf(`subpath`)가 실 등록 leaf 이고 flag 가 그 leaf 에 실 등록됐다.

    파서-backed 도구만 강제(worktree_pool/external_review 는 build_parser 없음 → 강제 제외·카드가
    지어낸 도구 방지는 카드↔정의서 대조가 커버). 정의서가 옵션 rename·삭제로 어긋나면 red.

    구조 메모: 이 가드는 flag 이 파서에 *등록*됐는지만 본다 — 파서는 수용하나 핸들러가 무시하는
    flag 는 여기선 green 이다(등록≠동작). flag 의 *실 동작*(핸들러가 실제로 소비하나)까지 검증하는
    behavior test 는 `test_board_scoping_isolation.test_list_task_flag_is_consumed_not_silent_noop`
    (T-0365 이월 ②·`--task` 소비 지점)이 별도로 못박는다.
    """
    tools_dir = _TREES[tree]
    bootstrap = _load(tools_dir, "pm_bootstrap")
    leaves = _leaves_by_tool(tools_dir)

    checked = 0
    for mode, records in bootstrap._CARD_MODE_CLI.items():
        for rec in records:
            if rec.tool not in leaves:
                continue  # 파서 없는 도구(external_review 등) — 강제 제외.
            leaf_opts = leaves[rec.tool]
            assert rec.subpath in leaf_opts, (
                f"[{tree}/{mode}] 정의서 record {rec.tool} {rec.subpath} 가 실 등록 leaf 가 아님 "
                f"(등록 leaf: {sorted(leaf_opts)})"
            )
            for flag in rec.flags:
                assert flag in leaf_opts[rec.subpath], (
                    f"[{tree}/{mode}] 정의서 flag {flag!r}({rec.tool} {rec.subpath})가 파서에 없음 "
                    f"(등록 옵션: {sorted(leaf_opts[rec.subpath])})"
                )
            checked += 1
    # sensitivity — 실제로 record 를 검증했다(파서 없는 도구만 남아 공허 통과 방지).
    assert checked >= 15, f"[{tree}] 파서-검증한 정의서 record 가 너무 적음({checked})"


# ── 2. 정의서 ↔ 카드 (모드별 정확 일치·양방향) ───────────────────────────────


def _strip_identity(tokens: list[str]) -> str:
    """카드 커맨드 토큰에서 정체성(`--repo/--slot/--task` + 값)을 제거해 base render 문자열 복원.

    정체성은 실값 보간(ADR-0057)이라 도구별 base render(placeholder 형)와 대조하려면 벗겨야 한다.
    task end/prefix 의 name 위치인자는 정체성 중간삽입 문제로 카드에서 `<이름>` placeholder 유지
    (render 에 포함)이므로 여기서 벗길 게 없다(정의서 record 와 그대로 일치).
    """
    out: list[str] = []
    i = 0
    while i < len(tokens):
        if tokens[i] in _IDENTITY_FLAGS and i + 1 < len(tokens):
            i += 2
            continue
        out.append(tokens[i])
        i += 1
    return " ".join(out)


@pytest.mark.parametrize("tree", list(_TREES))
@pytest.mark.parametrize("mode", ["slot", "task", "readonly"])
def test_card_render_matches_definition(tree, mode):
    """각 모드 카드가 렌더한 **(도구, base-render)** 집합 == 그 모드 정의서 집합 (누락·잉여 둘 다 red).

    카드→정의서: 카드가 정의서 밖 커맨드를 지어내지 않음. 정의서→카드: 정의서 커맨드가 전부
    렌더됨(신규 task/readonly 커맨드 누락 방지).

    **granularity = (tool, render)** — leaf-set 대조(reviewer should-fix)는 같은 leaf 의 render
    변종(예: `list --mine` 삭제·`list --task`→`list --mine` 변경)을 통과시켰다. render 급으로 올려
    정체성만 벗기고 base render 문자열까지 정확 대조해 렌더 줄 제거/변형이 잡히게 한다.
    """
    tools_dir = _TREES[tree]
    bootstrap = _load(tools_dir, "pm_bootstrap")
    records = bootstrap._CARD_MODE_CLI[mode]

    # 정의서 측 (tool, render) 집합.
    declared = {(rec.tool, rec.render) for rec in records}

    # 카드 측 (tool, base-render) 집합 — 정체성 실값만 벗기고 나머지 인자 문자열 정확 대조.
    card = _render_mode_card(bootstrap, mode)
    rendered = {(tool, _strip_identity(tokens)) for tool, tokens in _card_cli_commands(card)}

    missing = declared - rendered  # 정의서엔 있으나 카드 미렌더/변형.
    extra = rendered - declared    # 카드엔 있으나 정의서 밖(지어낸/변형 커맨드).
    assert not missing and not extra, (
        f"[{tree}/{mode}] 카드 ↔ 정의서 drift.\n"
        f"  정의서엔 있으나 카드 미렌더/변형: {sorted(missing)}\n"
        f"  카드엔 있으나 정의서 밖(지어냄/변형): {sorted(extra)}\n"
        f"  → pm_bootstrap 의 _CARD_{mode.upper()}_CLI 와 {mode} 카드 렌더를 정합시켜라."
    )
    # sensitivity — 카드가 실제로 CLI 줄을 렌더했다(추출 0 이면 대조 공허).
    assert rendered, f"[{tree}/{mode}] 카드에서 추출된 CLI 커맨드가 0 (렌더 회귀?)"


@pytest.mark.parametrize("tree", list(_TREES))
@pytest.mark.parametrize("mode", ["slot", "task", "readonly"])
def test_card_rendered_flags_exist_in_parser(tree, mode):
    """카드가 렌더한 각 CLI 줄의 비-정체성 flag 가 그 leaf 에 실 등록됐다(카드→파서 flag 방향).

    카드 텍스트에서 직접 뽑은 `--flag`(정체성 `--repo/--slot/--task` 제외)가 파서 leaf 옵션에
    없으면 red — 카드가 없는 옵션을 손으로 적는 클래스(정의서 우회 하드코딩)를 잡는다.
    """
    tools_dir = _TREES[tree]
    bootstrap = _load(tools_dir, "pm_bootstrap")
    leaves = _leaves_by_tool(tools_dir)
    card = _render_mode_card(bootstrap, mode)

    for tool, tokens in _card_cli_commands(card):
        if tool not in leaves:
            continue
        leaf = _resolve_leaf(leaves[tool], tokens)
        leaf_opts = leaves[tool].get(leaf, set())
        for tok in tokens:
            if tok.startswith("--") and tok not in _IDENTITY_FLAGS:
                assert tok in leaf_opts, (
                    f"[{tree}/{mode}] 카드가 렌더한 flag {tok!r}({tool} {leaf})가 파서에 없음 "
                    f"(등록 옵션: {sorted(leaf_opts)})"
                )


# ── 3. 파서 → 정의서 (신규 task/readonly 커맨드가 카드에 표면화됐나) ───────────


def test_task_mode_covers_new_task_commands():
    """task 모드 정의서가 F1~F7 신규 task 커맨드(alloc·release·task end·task prefix)를 covered.

    **한계 (spot-check·not generic 자동검출)**: 파서→정의서 방향을 완전 generic 으로(모든 파서 leaf
    가 카드에 있나) 강제하면 잘못이다 — 카드는 **curated** 표면(모드-스코프·신호 대 잡음·⑰)이라
    board/pm_config 의 모든 서브커맨드를 뿌리지 않는다(전부 뿌리면 컨텍스트 잠식). 그래서 이 wave
    가 실제로 추가한 **parser-backed** task 관리 leaf 만 명시 required 로 못박는다 — 새 서브커맨드가
    카드에서 조용히 빠지는 클래스의 teeth. (worktree_pool refresh/rebase 는 build_parser 없는
    스킬-렌더 backbone 이라 parser-backed required 대상이 아니다 — readonly 카드의 status 는 별도
    test 로 covered. board `list --task` 스코프 렌즈(T-0365)는 이제 cmd_list 가 소비하며 task 카드에
    `list`(+`--task` 정체성 suffix·base render `list`)로 렌더된다.)
    """
    bootstrap = _load(_TREES["root"], "pm_bootstrap")
    declared = {(rec.tool, rec.subpath) for rec in bootstrap._CARD_MODE_CLI["task"]}
    required = {
        ("pm_config.py", ("alloc",)),
        ("pm_config.py", ("release",)),
        ("pm_config.py", ("task", "end")),
        ("pm_config.py", ("task", "prefix")),
    }
    missing = required - declared
    assert not missing, f"task 카드가 신규 task 커맨드 미표면화: {sorted(missing)}"


def test_readonly_mode_covers_query_commands():
    """readonly 모드 정의서가 조회 커맨드(status)를 covered (⑬ 조회 전용)."""
    bootstrap = _load(_TREES["root"], "pm_bootstrap")
    declared = {(rec.tool, rec.subpath) for rec in bootstrap._CARD_MODE_CLI["readonly"]}
    assert ("pm_config.py", ("status",)) in declared, "readonly 카드가 status 조회 미표면화"


# ── 4. sensitivity (합성 drift 를 comparator 가 실제로 잡나·공허 가드 방지) ──────


def test_comparator_detects_bogus_flag_and_leaf():
    """없는 flag·없는 leaf 를 leaf-options 검증이 실제로 잡는지(가드 로직 non-vacuous)."""
    bootstrap = _load(_TREES["root"], "pm_bootstrap")
    leaves = _leaves_by_tool(_TREES["root"])
    board_leaves = leaves["board.py"]

    # 없는 flag — 실 leaf 옵션에 없다(정의서에 넣으면 test 1 이 red).
    assert "--no-such-flag" not in board_leaves[("list",)]
    # 없는 leaf — 등록 leaf 집합에 없다(정의서에 넣으면 test 1 이 red).
    assert ("no-such-subcommand",) not in board_leaves
    # 실재 확인(대조 대상이 실제로 존재·역-공허 방지).
    assert ("list",) in board_leaves and "--mine" in board_leaves[("list",)]


def test_render_granularity_detects_same_leaf_variant_removal():
    """(tool, render) 대조가 같은 leaf 의 render 변종 제거를 실제로 잡는지 (reviewer mutation·non-vacuous).

    leaf-set 대조는 `list --mine`(leaf `list`)를 삭제해도 `list`(전체 보드·같은 leaf)가 남아 통과했다.
    render 급은 `(board.py, "list --mine")` ≠ `(board.py, "list")` 라 삭제가 mismatch 로 잡힌다.
    """
    bootstrap = _load(_TREES["root"], "pm_bootstrap")
    card = _render_mode_card(bootstrap, "slot")
    rendered = {(tool, _strip_identity(tokens)) for tool, tokens in _card_cli_commands(card)}
    declared = {(rec.tool, rec.render) for rec in bootstrap._CARD_MODE_CLI["slot"]}
    assert declared == rendered  # baseline green.
    # `list --mine` 줄을 제거한 카드는 leaf `list`(전체 보드)가 남아도 render 대조가 red.
    variant = ("board.py", "list --mine")
    assert variant in rendered and variant in declared
    mutated = rendered - {variant}
    assert declared - mutated == {variant}, "render 급 대조가 같은-leaf 변종 제거를 못 잡음(공허)"


def test_mode_definitions_non_empty():
    """모드별 정의서가 비어있지 않다(공허 스코프 → 카드↔정의서 대조 무의미)."""
    bootstrap = _load(_TREES["root"], "pm_bootstrap")
    for mode, records in bootstrap._CARD_MODE_CLI.items():
        assert records, f"{mode} 모드 정의서가 비었다(가드 vacuous)"
    assert set(bootstrap._CARD_MODE_CLI) == {"slot", "task", "readonly"}


# ── 5. ㉓ 하네스 파리티 (템플릿 사본이 같은 정의서·같은 파서와 정합) ────────────


def test_template_copies_share_engine_definition():
    """출하 템플릿(claude_code·opencode) pm_bootstrap 사본이 루트와 **같은** 카드 정의서를 갖는다.

    ㉓ — 카드 렌더는 하네스-무관 Python 엔진이라 opencode 도 같은 정의를 소비(같은 부트스트랩 dump·
    같은 카드) → 어댑터 md 분기 0. 사본이 stale 하면 여기서 red(drift-0 은 pm_update 가 강제하나
    이 가드가 카드-표면 관점에서 이중화).
    """
    root = _load(_TREES["root"], "pm_bootstrap")
    root_decl = {m: {(r.tool, r.subpath, r.render, r.flags) for r in recs}
                 for m, recs in root._CARD_MODE_CLI.items()}
    for tree in ("claude_code", "opencode"):
        mod = _load(_TREES[tree], "pm_bootstrap")
        decl = {m: {(r.tool, r.subpath, r.render, r.flags) for r in recs}
                for m, recs in mod._CARD_MODE_CLI.items()}
        assert decl == root_decl, f"{tree} 템플릿 카드 정의서가 루트와 drift(사본 stale)"
