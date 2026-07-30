"""플래그 통일 — parity guard class-closer (T-0319 · ADR-0057).

공용 헬퍼(`identity_args.py`·T-0322) 채택 정합 + 재발 방지를 잠그는 두 그룹의 기계 가드:

  1. **semantics parity** — 정체성 인자를 파싱하는 전 도구(board·pm_bootstrap·pm_handoff·
     ticket_finish·pm_config·pm_relay·worktree_pool)의 argparse 가 `--repo`/`--slot` 를 동형
     수용하고, `--slot` 단독(=`--repo` 누락)이면 fail-loud 동일하게 거부하는지 단언한다. 7 도구
     중 5 개(board·pm_bootstrap·pm_handoff·ticket_finish·worktree_pool)는 공용
     `identity_args.add_identity_args`/`parse_identity` 를 실제 CLI 레이어로 채택했고(실코드
     감사), 나머지 2 개는 **설계상 CLI 표면이 다르다** — 그 사실도 이 파일이 함께 못박는다:
       - `pm_config` — identity 를 cwd/리스에서 암묵 해소(`_default_session`)하고 `--repo`/
         `--slot` 명시 override CLI 를 노출하지 않는다(T-0317 실코드 확인).
       - `pm_relay`(엔진 core) — 정체성 CLI 자체가 없다(argparse 조차 import 안 함·T-0318 감사
         결론 — 하니스별 CLI 는 어댑터 `pm_orch_*.py` 몫).
     "동형"을 두 아키텍처 예외에 억지로 강제하지 않는다 — 실제로 그런 표면이 없다면 그 부재
     자체를 잠그는 것이 정확한 class-closer 다(허위 green 금지).

  2. **old-flag 부재 스캔** — shipped doc/skill/command-card `.md` 표면 + 하니스 어댑터 런처
     `.py`(`pm_orch_*.py`)에 actor `--session`·`--worktree-slot`·`--session-num` 이 재등장하면
     red. 뷰-무관 `--session-seq`(차수·pm_handoff)·하니스 대화-연속성 `--session-id` 는
     allow-list. 엔진 도구 `.py`(board·pm_handoff 등)는 스캔 대상 밖 — 거긴 구 플래그가 마이그레
     이션 *설명* 주석으로 정당히 남고, identity CLI 정합은 그룹 1(semantics parity)이 잠근다.
     이 그룹은 T-0320 sweep 으로 green 이 된 **steady-state 재발-방지 가드**다(이후 재등장 시
     red·assert 약화·skip·xfail 금지).

패턴 참고: 카드↔CLI parse 단언(T-0315 `test_pm_bootstrap_card.py`) · 서브파서 옵션 재귀 수집
(`test_board_list_scope.py::_subparsers_action`) · 엔진 core CLI 부재 감사
(`test_pm_relay.py::test_engine_core_exposes_no_identity_cli_surface`).
"""
from __future__ import annotations

import argparse
import importlib.util
import re
from pathlib import Path
from typing import NamedTuple

import pytest

from _repo_owned_inventory import OWNED, repo_owned_paths

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load_tool(filename: str, *, name: str | None = None):
    """`.project_manager/tools/<filename>` 를 importlib 로 로드한다 (도구는 패키지가 아님 —
    `test_identity_args.py`/`test_board_identity.py` 등의 `_load` 관용구와 동형)."""
    module_name = name or Path(filename).stem
    spec = importlib.util.spec_from_file_location(module_name, TOOLS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ════════════════════════════════════════════════════════════════════════════
# 그룹 1 — semantics parity (정체성 인자 파싱 동형 + `--slot` 단독 fail-loud)
# ════════════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def ia_mod():
    return _load_tool("identity_args.py", name="identity_args")


@pytest.fixture(scope="module")
def tool_parsers():
    """`build_parser()` 를 보유한 4 개 CLI-플래그 도구의 실 파서 + 정체성 플래그 앞에 필요한
    prefix 토큰(서브커맨드·필수 positional).

    board.py 는 서브커맨드 CLI 라 `claim`(actor 대표 — 정체성이 곧바로 귀속 쓰기로 흐르는
    서브)을 대표로 고른다. ADR-0057 은 **도구 간** 동형이지 도구 **내** 全서브 동형까지 요구하지
    않는다(board 의 list/init/migrate-identity/regression/livegate 서브도 각자
    `identity_args.add_identity_args` 를 쓰지만, 대표 1개로 도구-간 파리티를 검증하면 충분 —
    도구 내 서브간 drift 는 `test_board_identity.py`/`test_board_list_scope.py` 몫).
    ticket_finish 는 위치 인자 `ticket_id` 가 필수라 더미 `T-0001` 을 둔다.
    """
    board = _load_tool("board.py")
    pm_bootstrap = _load_tool("pm_bootstrap.py")
    pm_handoff = _load_tool("pm_handoff.py")
    ticket_finish = _load_tool("ticket_finish.py")
    return {
        "board.py": (board.build_parser(), ["claim", "T-0001"]),
        "pm_bootstrap.py": (pm_bootstrap.build_parser(), []),
        "pm_handoff.py": (pm_handoff.build_parser(), []),
        "ticket_finish.py": (ticket_finish.build_parser(), ["T-0001"]),
    }


_FLAG_PARSER_TOOLS = ("board.py", "pm_bootstrap.py", "pm_handoff.py", "ticket_finish.py")


@pytest.mark.parametrize("tool_key", _FLAG_PARSER_TOOLS)
def test_flag_parser_tool_accepts_repo_and_slot_homomorphically(tool_key, tool_parsers, ia_mod):
    """`--repo X --slot N` 이 4 도구 모두 같은 `Identity(kind="slot", session="X_N")` 로 해소된다
    (실 CLI 파서로 parse → 공용 `identity_args.parse_identity` 로 판정 — 두 층 다 실물)."""
    parser, prefix = tool_parsers[tool_key]
    ns = parser.parse_args([*prefix, "--repo", "proj", "--slot", "3"])
    assert ns.repo == "proj"
    assert ns.slot == 3  # type=int(identity_args canonical) — 문자열로 새지 않는다.
    identity = ia_mod.parse_identity(ns)
    assert identity.kind == "slot"
    assert identity.repo == "proj"
    assert identity.slot == 3
    assert identity.session == "proj_3"


@pytest.mark.parametrize("tool_key", _FLAG_PARSER_TOOLS)
def test_flag_parser_tool_bare_slot_fails_loud_homomorphically(tool_key, tool_parsers, ia_mod):
    """`--slot N` 단독(`--repo` 없음) — 4 도구 모두 공용 `parse_identity` 가 동일하게
    `ValueError`(--repo 안내)로 거부한다(ADR-0057 결정 2 · uniform · solo 예외 없음)."""
    parser, prefix = tool_parsers[tool_key]
    ns = parser.parse_args([*prefix, "--slot", "3"])
    with pytest.raises(ValueError, match=r"--repo"):
        ia_mod.parse_identity(ns)


@pytest.fixture(scope="module")
def wp_mod():
    return _load_tool("worktree_pool.py")


def test_worktree_pool_accepts_repo_and_slot_homomorphically(wp_mod, monkeypatch):
    """worktree_pool 은 `build_parser()` 를 따로 노출하지 않으므로(파서가 `main()` 내부 인라인)
    `main()` 진입점으로 동형성을 검증한다 — kind="slot" 경로는 `_normalize_slot`(포맷 검증만)
    으로 리스 파일 IO 없이 안전하다(worktree_pool.py 소스 확인·hermetic 불요)."""
    captured: dict[str, str] = {}
    monkeypatch.setattr(
        wp_mod, "sync", lambda slot, *, git_runner=None: captured.setdefault("slot", slot)
    )
    rc = wp_mod.main(["sync", "--repo", "proj", "--slot", "3"])
    assert rc == 0
    assert captured["slot"] == "work/proj_3"


def test_worktree_pool_bare_slot_fails_loud(wp_mod, monkeypatch, capsys):
    """`--slot N` 단독 — `parse_identity` 의 `ValueError` 를 `main` 이 rc 1 + `--repo` 안내로
    surface(다른 4 도구와 동일 실패 형태 — SystemExit 아닌 rc 반환이라는 관례 차이는 각 CLI
    래핑 스타일일 뿐, 근본 판정은 공용 `parse_identity` 로 동일)."""
    monkeypatch.setattr(
        wp_mod, "sync", lambda *a, **k: pytest.fail("bare --slot 인데 sync 가 호출됨")
    )
    rc = wp_mod.main(["sync", "--slot", "3"])
    assert rc == 1
    assert "--repo" in capsys.readouterr().err


def _all_option_strings(parser: argparse.ArgumentParser) -> set[str]:
    """parser + 모든 서브파서(재귀)의 optional 플래그 문자열 전체
    (`test_board_list_scope.py::_subparsers_action` 관용구 확장)."""
    opts: set[str] = set()
    for action in parser._actions:
        opts.update(action.option_strings)
        if isinstance(action, argparse._SubParsersAction):
            for sub_parser in action.choices.values():
                opts.update(_all_option_strings(sub_parser))
    return opts


@pytest.fixture(scope="module")
def pm_config_mod():
    return _load_tool("pm_config.py")


def test_pm_config_has_no_explicit_repo_slot_or_session_cli_flags(pm_config_mod):
    """pm_config 는 나머지 6 도구와 달리 `--repo`/`--slot` CLI 플래그를 노출하지 않는다 —
    설계상 예외(실 코드 감사로 확인, T-0317). identity_args 는 리스 IO 층(`leased_sessions`)만
    `_default_session` 내부에서 재사용하고(B-1), CLI 인자 레이어(`add_identity_args`)는
    채택하지 않았다 — `repo add`/`worktree add`/`release`/`status`/`whoami` 전부 cwd·리스
    유도 암묵 해소만 쓰고 명시 override 플래그가 없다(구조 자체가 identity CLI 무표면). 구
    `--session` 도 아무 서브에도 등록돼 있지 않다(T-0317 BREAKING 제거·grep 잔여 0 lock).
    이 사실을 고정해 향후 누군가 이 도구에만 부분적으로 `--repo`/`--slot` 를 추가(다른 5 도구와
    비일관 drift)하는 것을 막는다 — 추가한다면 이 가드를 의식적으로 갱신해야 한다.
    """
    opts = _all_option_strings(pm_config_mod.build_parser())
    assert "--repo" not in opts
    assert "--slot" not in opts
    # 구 alias 전수 부재 — 이 설계-예외 도구에 옛 정체성 플래그가 부분 재유입되는 것까지 잠근다.
    for legacy in ("--session", "--worktree-slot", "--session-num"):
        assert legacy not in opts, f"pm_config 에 구 정체성 플래그 {legacy!r} 재유입"


_PM_RELAY_PY = TOOLS / "pm_relay.py"


def test_pm_relay_engine_core_exposes_no_identity_cli_surface():
    """pm_relay.py(엔진 core)는 나머지 6 도구와 달리 정체성 CLI 표면이 아예 없다 — argparse
    자체를 import 하지 않는 순수 라이브러리다(T-0318 감사 결론 — `test_pm_relay.py` 의 전용
    가드와 동형이나, 이 class-closer 파일에도 포함해 7-도구 전수를 한 자리에서 감사한다). 하니스
    별 CLI(`--cwd`/`--model`)는 어댑터(`templates/*/.claude/pm_orch_claude.py` 등) 몫이고,
    엔진은 `Supervisor.run_loop(cwd, ...)` 로 호출자가 이미 해소한 cwd 를 받는다.

    `--session-id`(claude/opencode 대화-연속성 id)는 multi-PM 정체성과 무관한 별개 개념이라
    이 감사에서 명시 제외(음의 lookahead) — `--session-seq`/`--worktree-slot`/`--session-num`
    은 애초에 대상 자체가 아니다(정체성 CLI 자체가 없으므로 제거할 alias 도 없다).
    """
    text = _PM_RELAY_PY.read_text(encoding="utf-8")
    assert "import argparse" not in text, "엔진 core 에 argparse CLI 가 생기면 어댑터/엔진 경계가 흐려진다."
    assert re.search(r"--session(?!-id)\b", text) is None, "정체성 --session 플래그가 있으면 안 된다."
    for legacy in ("--worktree-slot", "--session-num"):
        assert legacy not in text, f"엔진 core 에 구 alias {legacy!r} 가 있으면 안 된다."


_ADOPTING_TOOL_FILES = (
    "board.py", "pm_bootstrap.py", "pm_handoff.py", "ticket_finish.py", "worktree_pool.py",
)


@pytest.mark.parametrize("filename", _ADOPTING_TOOL_FILES)
def test_flag_bearing_tool_source_adopts_shared_identity_args_module(filename):
    """공용 헬퍼 채택 정합(T-0322) 잠금 — 5 개 CLI-플래그 도구가 각자 로컬로 `--repo`/`--slot`
    파서를 재구현하지 않고 canonical `identity_args.add_identity_args(...)` 를 호출한다는 사실을
    소스 텍스트로 못박는다. 어느 도구가 향후 로컬 재구현으로 drift 하면(공용 모듈 미채택) 여기서
    red — semantics parity(위 두 그룹)가 "우연히" 같은 결과를 내더라도, 그 근거가 공용 모듈
    단일 진실인지까지 이 테스트가 확인한다.
    """
    text = (TOOLS / filename).read_text(encoding="utf-8")
    assert re.search(r"\.add_identity_args\(", text), (
        f"{filename} 이 공용 identity_args.add_identity_args 를 호출하지 않는다(로컬 재구현 의심)."
    )


# ════════════════════════════════════════════════════════════════════════════
# 그룹 2 — old-flag 부재 스캔 (shipped doc/skill/command-card `.md` + 어댑터 런처 `.py`)
#
# T-0320 sweep 으로 shipped 표면의 구 플래그가 새 표기(`--repo <repo> --slot <N>`)로 정리돼
# 이 그룹은 green(steady-state)이다 — 이후 문서/카드/어댑터 drift 로 구 플래그가 재등장하면 red.
# ════════════════════════════════════════════════════════════════════════════


class _OldFlagHit(NamedTuple):
    path: Path
    line_no: int
    token: str
    text: str


# `--session(-<suffix>)?` 또는 `--worktree-slot` 토큰 전체를 잡는다(부분매칭 방지 — `\b` 는
# `--session` 뒤에 `-num` 처럼 비-단어문자 아닌 하이픈이 와도 경계로 인식하므로, 그룹으로 접미사
# 전체를 함께 소비해 토큰 단위로 분류한다).
_SESSION_TOKEN_RE = re.compile(r"--session(-[a-zA-Z]+)?|--worktree-slot")

# 허용 접미사 — "seq"(차수·뷰-무관·pm_handoff 정식 인자) · "id"(하니스 대화-연속성 session-id·
# multi-PM 정체성과 무관한 별개 개념·pm_relay 감사와 동형). 그 외(특히 "num" = 구 alias)는 위반.
_ALLOWED_SESSION_SUFFIXES = frozenset({"seq", "id"})


def _is_old_flag_violation(token: str) -> bool:
    if token == "--worktree-slot":
        return True
    if token == "--session":
        return True  # actor bare --session(구 alias·ADR-0057 BREAKING 제거 대상).
    suffix = token[len("--session-"):]
    return suffix not in _ALLOWED_SESSION_SUFFIXES


def _scan_file_for_old_flags(path: Path) -> list[_OldFlagHit]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    hits: list[_OldFlagHit] = []
    for line_no, line in enumerate(lines, start=1):
        for m in _SESSION_TOKEN_RE.finditer(line):
            token = m.group(0)
            if _is_old_flag_violation(token):
                hits.append(_OldFlagHit(path, line_no, token, line.strip()))
    return hits


def _scan_paths_for_old_flags(paths: list[Path]) -> list[_OldFlagHit]:
    """`paths` 의 파일은 그 파일을, 디렉토리는 하위 전 `*.md` 를 스캔한다. 존재하지 않는 경로는
    조용히 skip(예: 이 worktree 엔 아직 `.project_manager/wiki/_template` 가 없다 — 생기면
    자동으로 스캔 대상에 편입되는 glob 이지 하드 의존 아니다)."""
    hits: list[_OldFlagHit] = []
    for base in paths:
        if base.is_file():
            hits.extend(_scan_file_for_old_flags(base))
        elif base.is_dir():
            for md in repo_owned_paths(
                REPO, base.relative_to(REPO), mode=OWNED
            ):
                if md.suffix != ".md":
                    continue
                hits.extend(_scan_file_for_old_flags(md))
    return hits


def _format_hits(hits: list[_OldFlagHit]) -> str:
    """(path, line) 로 그룹화해 같은 줄의 중복 토큰을 한 줄로 압축 — 실패 메시지 가독성."""
    grouped: dict[tuple[Path, int], tuple[set[str], str]] = {}
    for h in hits:
        key = (h.path, h.line_no)
        tokens, text = grouped.setdefault(key, (set(), h.text))
        tokens.add(h.token)
    lines = []
    for (path, line_no), (tokens, text) in sorted(grouped.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])):
        rel = path.relative_to(REPO)
        lines.append(f"  {rel}:{line_no}: {sorted(tokens)} :: {text}")
    return "\n".join(lines)


def _readme_files() -> list[Path]:
    return sorted(p for p in REPO.glob("README*") if p.is_file())


SURFACE_GROUPS: dict[str, list[Path]] = {
    "claude_skills": [REPO / ".claude" / "skills"],
    "claude_agents": [REPO / ".claude" / "agents"],
    "templates_claude_code": [REPO / "templates" / "claude_code"],
    "templates_opencode": [REPO / "templates" / "opencode"],
    "root_docs": [REPO / "CLAUDE.md", *_readme_files()],
    "wiki_methodology": [
        REPO / ".project_manager" / "wiki" / "pm_role.md",
        REPO / ".project_manager" / "wiki" / "pm_playbook.md",
        # canonical identity-teaching 표면 — sweep 이 실제 고친 methodology 파일(구 `--session`→
        # `--repo/--slot`). template 사본은 templates_* dir-glob 이 잡지만, canonical 직접 커버로
        # "편집→미전파" 구간까지 닫는다(reviewer should-fix·codex 스코프).
        REPO / ".project_manager" / "wiki" / "tickets" / "README.md",
    ],
    "wiki_template": [REPO / ".project_manager" / "wiki" / "_template"],
    # 하니스 어댑터 런처 `.py`(board/relay 를 호출하는 shipped 어댑터 표면) — 엔진 도구 `.py` 와
    # 달리 마이그레이션 설명 주석이 없어 명시 스캔 안전. 여기에 구 플래그가 생기면 실 usage 재유입.
    "adapter_launchers": [
        REPO / "templates" / "claude_code" / ".claude" / "pm_orch_claude.py",
        REPO / "templates" / "opencode" / ".opencode" / "pm_orch_opencode.py",
    ],
}


@pytest.mark.parametrize("group", sorted(SURFACE_GROUPS))
def test_shipped_surface_has_no_old_identity_flags(group):
    """shipped 표면에 actor `--session`/`--worktree-slot`/`--session-num` 재발 부재 단언
    (allow `--session-seq`/`--session-id`) — T-0319/T-0320 class-closer 가드.

    T-0320 sweep 으로 green(steady-state) — 이후 문서/카드/어댑터 drift 로 구 플래그가 재등장하면
    다시 red 로 잡는다(재발 방지 — [[feature-ship-needs-fresh-adopter-gate]] 패턴). 실패 메시지의
    (경로:줄) 목록이 정정 work-list 다.
    """
    hits = _scan_paths_for_old_flags(SURFACE_GROUPS[group])
    assert not hits, (
        f"[{group}] old-flag(actor --session/--worktree-slot/--session-num) 잔존 "
        f"{len(hits)}건(파일 {len({h.path for h in hits})}개):\n{_format_hits(hits)}"
    )


# ── 스캐너 자체 정밀도 가드 (vacuous-pass 방지 — 검출력·allow-list 정확성을 별도로 lock) ──


def test_old_flag_scanner_flags_synthetic_violations(tmp_path):
    """스캐너 sensitivity — 합성 old-flag 라인 3종을 실제로 잡는지(T-0320 이후에도 재발 감지력
    보존 확인용 — 위 그룹 테스트가 전부 green 이어도 이 가드는 계속 유효해야 한다)."""
    doc = tmp_path / "fake.md"
    doc.write_text(
        "예시: `board.py claim T-0001 --session myproj_1`\n"
        "구형 `--worktree-slot work/myproj_1` 도 동작\n"
        "구형 `--session-num 19` 지원\n",
        encoding="utf-8",
    )
    hits = _scan_paths_for_old_flags([doc])
    assert {h.token for h in hits} == {"--session", "--worktree-slot", "--session-num"}


def test_old_flag_scanner_allows_session_seq_and_session_id(tmp_path):
    """allow-list 정확성(과소 아님) — `--session-seq`/`--session-id` 만 있는 문서는 0 건이어야
    한다(허용 토큰까지 blanket 으로 잡으면 T-0320 sweep 후에도 영구 red 가 되는 회귀)."""
    doc = tmp_path / "fake.md"
    doc.write_text("--session-seq 19 그리고 --session-id abc123 은 무관 개념.\n", encoding="utf-8")
    assert _scan_paths_for_old_flags([doc]) == []


def test_old_flag_scanner_allow_list_is_precise_not_blanket(tmp_path):
    """allow-list 정밀도(과다 아님) — 같은 줄에 `--session-seq`(허용)와 `--session-num`/
    `--worktree-slot`(위반)이 인접해도 스캐너가 파일/줄 단위 blanket 이 아니라 **토큰 단위**로
    정확히 가른다. 합성 fixture 로 고정한다 — shipped 실파일은 T-0320 sweep 후 위반 토큰이 0개라
    (class-closer 목표) 실파일이 위반을 '포함해야 한다'는 단언은 sweep 목표와 자기모순이 된다
    (sweep-robust: 검출력은 이 테스트+`test_old_flag_scanner_flags_synthetic_violations` 가 합성으로 잠근다)."""
    doc = tmp_path / "mixed.md"
    doc.write_text(
        "pm_handoff.py `--session-seq 19` 는 허용, 구형 `--session-num 19`·`--worktree-slot X` 는 위반.\n",
        encoding="utf-8",
    )
    hits = _scan_file_for_old_flags(doc)
    tokens = {h.token for h in hits}
    assert "--session-seq" not in tokens, "허용 토큰 --session-seq 가 오탐됐다(blanket 과다검출)."
    assert tokens == {"--session-num", "--worktree-slot"}, (
        "같은 줄 인접 토큰을 토큰 단위로 정확히 가르지 못했다(allow `--session-seq` 는 통과·위반만 "
        f"검출해야 함): {sorted(tokens)}"
    )
