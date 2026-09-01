"""pm_principles.py 로더 + recall 판정 단위 테스트.

레지스트리(`.project_manager/wiki/pm_principles.md` + PM 홈 로컬층 `pm_principles.local.md`)를
importlib 로 직접 로드해 검증한다. 실 canonical 레지스트리 파일도 형태(태그 유무 → 분류)를
값으로 확인한다 — 검증 축:
  1. 파서 정합 — 태그 항목/무태그 항목/파손 태그가 RECALL/JUDGMENT/broken 으로 갈린다. 로더
     경계값(빈 파일·CRLF·같은 파일 안 중복·실제 10,000자 임계)도 값으로 확인한다.
  2. 층 합성 — 로컬층이 뒤에 붙고 같은 (on, match) 면 로컬층이 이긴다 · 로컬층 부재(채택자 형상)에서도 출하층만으로 동작.
  3. 비차단·상한 — `judge_recall` 은 어떤 입력에도 예외를 던지지 않고, 매칭 다수 시 상한 안으로 접히며 매칭 수를 값으로 싣는다.
  4. opencode plugin core 순수함수 자가검증(node) — `require` 성공 + `toolSignal`/`extractPromptText` 값 +
     실 subprocess 발화-억제-재무장 사이클 + 파손 경고 표면화.
  5. codex 어댑터 — 실 캡처 fixture(`tests/fixtures/codex_0_147_0_live_hook_payloads.json`)로 도구
     이름 매핑·파손 경고·최종 합본 상한을 값으로 확인한다.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
PM_PRINCIPLES_PY = TOOLS / "pm_principles.py"
# 로더가 실행 중 지연 로드하는 형제 seam — 중앙 로더(repo_owned_files)·공용 읽기(file_lock)·
# 기계 출력(console_encoding). 채택자 트리엔 항상 함께 있고, 로더는 이들을 rev 검증으로만 부르므로
# 사본 fixture 도 같은 집합을 깔아야 실제 형상과 같아진다.
ENGINE_SIBLING_PY = ("repo_owned_files.py", "file_lock.py", "console_encoding.py")
CANONICAL_REGISTRY = REPO / ".project_manager" / "wiki" / "pm_principles.md"
CODEX_DISPATCHER_PY = REPO / "templates" / "codex" / ".codex" / "pm_orch_codex.py"
CODEX_LIVE_HOOK_FIXTURE = REPO / "tests" / "fixtures" / "codex_0_147_0_live_hook_payloads.json"


def _write_engine_tools(tools_dir: Path) -> None:
    """tmp 채택자 트리의 `.project_manager/tools/` 에 로더와 그 형제 seam 을 배치한다."""
    tools_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(PM_PRINCIPLES_PY, tools_dir / "pm_principles.py")
    for name in ENGINE_SIBLING_PY:
        shutil.copyfile(TOOLS / name, tools_dir / name)


def _load_module():
    spec = importlib.util.spec_from_file_location("pm_principles_under_test", PM_PRINCIPLES_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def m():
    return _load_module()


def _load_codex_dispatcher():
    spec = importlib.util.spec_from_file_location("codex_dispatcher_under_test", CODEX_DISPATCHER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def codex():
    return _load_codex_dispatcher()


def _codex_live_bash_event() -> dict:
    """라이브 캡처 fixture(codex-cli 0.147.0)의 유일한 Bash PreToolUse 이벤트."""
    data = json.loads(CODEX_LIVE_HOOK_FIXTURE.read_text(encoding="utf-8"))
    events = [event for event in data["events"] if event.get("tool_name") == "Bash"]
    assert len(events) == 1, events
    return dict(events[0])


def _codex_live_delegate_event() -> dict:
    """라이브 캡처 fixture의 `collaborationspawn_agent`(delegate 축) PreToolUse 이벤트."""
    data = json.loads(CODEX_LIVE_HOOK_FIXTURE.read_text(encoding="utf-8"))
    events = [event for event in data["events"] if event.get("tool_name") == "collaborationspawn_agent"]
    assert len(events) == 1, events
    return dict(events[0])


def _write_registry(root: Path, text: str, *, local: str | None = None) -> None:
    wiki = root / ".project_manager" / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / "pm_principles.md").write_text(text, encoding="utf-8")
    if local is not None:
        (wiki / "pm_principles.local.md").write_text(local, encoding="utf-8")


# ── 1. 파서 정합 ──────────────────────────────────────────────────────────

def test_tagged_item_parses_as_recall_with_on_and_match(m, tmp_path):
    _write_registry(tmp_path, (
        "#### 번들\n"
        "- `[shell: git\\s+push]` 규칙 본문. 어기면 깨진다.\n"
    ))
    rules = m.load(tmp_path)
    assert len(rules) == 1
    rule = rules[0]
    assert rule.on == "shell"
    assert rule.match == "git\\s+push"
    assert rule.text == "규칙 본문. 어기면 깨진다."


def test_untagged_item_parses_as_judgment(m, tmp_path):
    _write_registry(tmp_path, (
        "#### 번들\n"
        "- 태그 없는 판단 원칙. 어기면 깨진다.\n"
    ))
    rules = m.load(tmp_path)
    assert len(rules) == 1
    assert rules[0].on is None
    assert rules[0].match is None


@pytest.mark.parametrize("broken_line", [
    "- `[unknown-on: x]` 잘못된 on 값.",       # on 이 닫힌 4어휘 밖
    "- `[shell x]` 콜론 없음.",                 # 태그 문법 파손
    "- `[shell: (unclosed]` 정규식 오류.",      # 정규식 컴파일 실패
])
def test_broken_tag_is_excluded_and_not_silently_a_pass(m, tmp_path, broken_line):
    """파손 항목은 규칙 목록에서 빠지고(judge_recall 이 아무것도 매칭 안 함), 판정 불능은
    `judge_recall` 의 `broken` 필드로 표면화된다 — 조용한 통과가 아니다."""
    _write_registry(tmp_path, f"#### 번들\n{broken_line}\n")
    rules = m.load(tmp_path)
    assert rules == ()
    result = m.judge_recall(tmp_path, on="shell", text="git push", seen=set())
    assert result is not None
    assert result.get("broken", 0) >= 1
    assert result["count"] == 0


def test_blank_and_non_item_lines_are_ignored(m, tmp_path):
    _write_registry(tmp_path, (
        "# 표제\n\n"
        "산문 한 줄(항목 아님).\n\n"
        "#### 번들\n"
        "- 유효한 판단 원칙. 어기면 깨진다.\n"
    ))
    rules = m.load(tmp_path)
    assert len(rules) == 1


def test_missing_registry_file_yields_zero_rules_not_an_error(m, tmp_path):
    """레지스트리 파일 자체가 없어도(신선한 트리) load 는 예외 없이 빈 튜플."""
    rules = m.load(tmp_path)
    assert rules == ()
    assert m.judge_recall(tmp_path, on="shell", text="git push", seen=set()) is None


def test_empty_registry_file_yields_zero_rules(m, tmp_path):
    """파일은 존재하되 내용이 빈 문자열이면(부재와 다른 경계) 규칙 0건."""
    _write_registry(tmp_path, "")
    assert (tmp_path / ".project_manager" / "wiki" / "pm_principles.md").is_file()
    rules = m.load(tmp_path)
    assert rules == ()
    assert m.judge_recall(tmp_path, on="shell", text="git push", seen=set()) is None


def test_crlf_line_endings_parse_the_same_as_lf(m, tmp_path):
    """CRLF 로 저장된 레지스트리도 LF 와 같은 규칙을 낸다(universal newline 읽기)."""
    text = "#### 번들\r\n- `[shell: git\\s+push]` 규칙 본문. 어기면 깨진다.\r\n"
    _write_registry(tmp_path, text)
    raw = (tmp_path / ".project_manager" / "wiki" / "pm_principles.md").read_bytes()
    assert b"\r\n" in raw  # 실제로 CRLF 로 기록됐다(테스트 전제 확인)
    rules = m.load(tmp_path)
    assert len(rules) == 1
    assert rules[0].on == "shell"
    assert rules[0].text == "규칙 본문. 어기면 깨진다."


def test_duplicate_item_within_the_same_file_last_one_wins(m, tmp_path):
    """같은 (on, match) 항목이 한 파일 안에서 두 번 나오면(오타 재등록 등) 나중 것이 이긴다 —
    로컬층이 출하층을 덮는 것과 같은 last-wins 규칙이 같은 층 안에서도 성립한다."""
    _write_registry(tmp_path, (
        "#### 번들\n"
        "- `[shell: git\\s+push]` 첫 번째 문구.\n"
        "- `[shell: git\\s+push]` 두 번째 문구(이게 이긴다).\n"
    ))
    rules = m.load(tmp_path)
    assert len(rules) == 1
    assert rules[0].text == "두 번째 문구(이게 이긴다)."


def test_judge_recall_text_at_exact_10000_char_cap_is_not_summarized(m, tmp_path):
    """실제 `_MAX_INJECT_CHARS`(10,000) 경계 — 정확히 상한이면 원문 그대로다(monkeypatch 아님)."""
    fixed_len = len(m._INJECT_PREFIX) + 3  # f"{prefix} - {rule_text}" 의 고정 부분(공백 1 + "- ").
    body = "x" * (m._MAX_INJECT_CHARS - fixed_len)
    _write_registry(tmp_path, f"#### 번들\n- `[shell: git]` {body}\n")
    result = m.judge_recall(tmp_path, on="shell", text="git", seen=set())
    assert result is not None
    assert len(result["text"]) == m._MAX_INJECT_CHARS
    assert "매칭" not in result["text"]  # 요약으로 접히지 않았다.


def test_judge_recall_text_at_10001_chars_is_summarized_not_truncated(m, tmp_path):
    """상한을 딱 1자 넘기면(10,001) 원문을 자르지 않고 매칭 수 요약으로 접힌다."""
    fixed_len = len(m._INJECT_PREFIX) + 3
    body = "x" * (m._MAX_INJECT_CHARS - fixed_len + 1)
    _write_registry(tmp_path, f"#### 번들\n- `[shell: git]` {body}\n")
    result = m.judge_recall(tmp_path, on="shell", text="git", seen=set())
    assert result is not None
    assert "매칭 1건" in result["text"]
    assert len(result["text"]) < m._MAX_INJECT_CHARS
    assert "x" * 50 not in result["text"]  # 원문이 절단된 채로 남지 않았다(요약으로 대체).


# ── 2. 층 합성 ────────────────────────────────────────────────────────────

def test_local_layer_overrides_shipped_rule_with_same_match(m, tmp_path):
    _write_registry(
        tmp_path,
        "#### 공유\n- `[shell: git\\s+push]` 출하층 문구.\n",
        local="#### 로컬\n- `[shell: git\\s+push]` 로컬층 문구.\n",
    )
    rules = m.load(tmp_path)
    assert len(rules) == 1
    assert rules[0].text == "로컬층 문구."
    assert rules[0].layer == "local"


def test_local_layer_adds_distinct_rule_without_dropping_shipped(m, tmp_path):
    _write_registry(
        tmp_path,
        "#### 공유\n- `[shell: git\\s+push]` 출하층 문구.\n",
        local="#### 로컬\n- 로컬 전용 판단 원칙.\n",
    )
    rules = m.load(tmp_path)
    assert len(rules) == 2
    texts = {rule.text for rule in rules}
    assert texts == {"출하층 문구.", "로컬 전용 판단 원칙."}


def test_adopter_shape_without_local_file_uses_shipped_only(m, tmp_path):
    """로컬층 파일이 아예 없는 채택자 형상에서도 출하층만으로 정상 동작한다."""
    _write_registry(tmp_path, "#### 공유\n- `[edit: tests/]` 출하 문구.\n")
    assert (tmp_path / ".project_manager" / "wiki" / "pm_principles.local.md").exists() is False
    rules = m.load(tmp_path)
    assert len(rules) == 1
    result = m.judge_recall(tmp_path, on="edit", text="tests/test_x.py", seen=set())
    assert result is not None and result["count"] == 1


# ── 3. 비차단 · 상한 ──────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text", ["", "\x00\x01binary-ish\xff", "x" * 100_000],
    ids=("empty", "binary", "long-100k"),
)
def test_judge_recall_never_raises_on_hostile_input(m, tmp_path, text):
    _write_registry(tmp_path, "#### 공유\n- `[shell: git]` 문구. 어기면 깨진다.\n")
    # 예외를 던지면 pytest 가 실패로 잡는다 — 호출 자체가 단언이다.
    m.judge_recall(tmp_path, on="shell", text=text, seen=set())


def test_judge_recall_unknown_on_axis_returns_none(m, tmp_path):
    _write_registry(tmp_path, "#### 공유\n- `[shell: git]` 문구.\n")
    assert m.judge_recall(tmp_path, on="not-a-real-axis", text="git", seen=set()) is None


def test_judge_recall_dedups_against_seen(m, tmp_path):
    _write_registry(tmp_path, "#### 공유\n- `[shell: git\\s+push]` 문구. 어기면 깨진다.\n")
    first = m.judge_recall(tmp_path, on="shell", text="git push", seen=set())
    assert first["count"] == 1
    seen = set(first["keys"])
    second = m.judge_recall(tmp_path, on="shell", text="git push", seen=seen)
    assert second is None or second["count"] == 0


def test_judge_recall_caps_rendered_text_and_reports_match_count(m, tmp_path, monkeypatch):
    """상한을 넘길 매칭이면 잘라내지 말고 매칭 수를 값으로 싣는다."""
    monkeypatch.setattr(m, "_MAX_INJECT_CHARS", 10)
    _write_registry(tmp_path, (
        "#### 공유\n"
        "- `[shell: git]` 아주 긴 규칙 본문 하나 — 상한을 넘기기에 충분한 길이로 채운다. 어기면 깨진다.\n"
    ))
    result = m.judge_recall(tmp_path, on="shell", text="git push", seen=set())
    assert result is not None
    assert result["count"] == 1
    assert "매칭 1건" in result["text"]
    assert len(result["text"]) <= 200  # 원문 그대로가 아니라 요약으로 접혔다.


def test_broken_registry_pattern_is_undecidable_not_a_pass(m, tmp_path):
    """파손 항목이 섞여도 정상 항목 판정은 그대로 살아 있고, broken 건수가 함께 표면화된다."""
    _write_registry(tmp_path, (
        "#### 공유\n"
        "- `[shell: (unclosed]` 파손 항목.\n"
        "- `[shell: git\\s+push]` 정상 항목. 어기면 깨진다.\n"
    ))
    result = m.judge_recall(tmp_path, on="shell", text="git push", seen=set())
    assert result is not None
    assert result["count"] == 1
    assert result.get("broken", 0) == 1


def test_broken_registry_warning_is_carried_in_the_single_text_field_all_adapters_inject(m, tmp_path):
    """매칭 0 이어도 파손이 있으면 `text` 가 비지 않는다 — 세 어댑터 모두 `result.text` 하나만
    보고 주입하므로(F-004), 파손 경고가 이 필드에 없으면 무출력으로 사라진다."""
    _write_registry(tmp_path, "#### 공유\n- `[shell: (unclosed]` 파손 항목.\n")
    result = m.judge_recall(tmp_path, on="shell", text="아무 텍스트나", seen=set())
    assert result is not None
    assert result["count"] == 0
    assert result.get("broken", 0) == 1
    assert result["text"] != ""
    assert "[principle-recall]" in result["text"]
    assert "파손" in result["text"]


# ── canonical 레지스트리 형태 값 확인 ────────────────────────────────────

def test_canonical_registry_recall_and_judgment_counts(m):
    """실 출하 레지스트리의 분류 건수 — RECALL 13(태그) · JUDGMENT 12(무태그)."""
    rules = m.load(REPO)
    recall = [rule for rule in rules if rule.on is not None]
    judgment = [rule for rule in rules if rule.on is None]
    assert len(recall) == 13, [rule.match for rule in recall]
    assert len(judgment) == 12
    for rule in recall:
        assert rule.on in m.ON_VALUES


def test_canonical_registry_recall_matches_have_no_private_reference(m):
    """레지스트리 문구에 티켓/세션/날짜 좌표를 담지 않는다(사설 참조 가드의 보조 값 확인)."""
    import re
    rules = m.load(REPO)
    forbidden = re.compile(r"T-\d{4}|PM\s*\d+차|20\d\d-\d\d-\d\d")
    offenders = [rule.text for rule in rules if forbidden.search(rule.text)]
    assert offenders == []


def test_canonical_delegate_rule_requires_contracts_for_every_finding():
    """v3 parser와 원칙 문구가 severity 무관 계약 범위에서 갈리지 않는다."""
    text = CANONICAL_REGISTRY.read_text(encoding="utf-8")
    rule = next(
        line for line in text.splitlines()
        if line.startswith("- `[delegate: architect|developer|code-reviewer]`")
    )
    assert "severity와 무관하게 모든 finding마다" in rule
    assert "reviewer는 must-fix마다" not in rule
    assert "완전성·실행 가능성만 read-only로 검증하고" in rule


def test_delegator_grants_equal_authority_is_registered(m):
    """위임 권한 동등 규칙이 레지스트리 항목으로 실려 있고 위임 축에서 회상된다 (T-0887).

    카드 산문만으로는 하네스가 바뀌면 규칙이 사라진다 — 세 하네스가 공유하는 이 표에 있어야
    코덱스·오픈코드가 PM 일 때도 같은 규칙을 받는다.
    """
    rules = m.load(REPO)
    matched = [rule for rule in rules
               if "위임자는 피위임자에게 자신과 같은 권한을 준다" in rule.text]
    assert len(matched) == 1, [rule.text[:40] for rule in rules]
    rule = matched[0]
    assert "위임 방향·하네스 조합 무관" in rule.text
    assert rule.on == "edit"
    # 위임을 말하는 편집에서 실제로 회상된다(태그가 하네스 이름만 보면 이 축을 놓친다).
    for probe in ("delegate 경로를 고친다", "위임 경로를 고친다", "codex argv"):
        recalled = m.judge_recall(REPO, on="edit", text=probe)
        assert recalled is not None, probe
        assert "같은 권한을 준다" in recalled["text"], probe


def test_canonical_registry_loads_from_each_shipped_target(m):
    """3타깃 사본 각자의 트리에서 로더가 항목을 읽는다(값으로 확인 — 채택자 시야)."""
    for target in ("claude_code", "codex", "opencode"):
        target_root = REPO / "templates" / target
        rules = m.load(target_root)
        assert len(rules) == 25, target


# ── engine.manifest 정합 ──────────────────────────────────────────────────

_MANIFEST_PATHS = (
    REPO / ".project_manager" / "engine.manifest",
    REPO / "templates" / "claude_code" / ".project_manager" / "engine.manifest",
    REPO / "templates" / "codex" / ".project_manager" / "engine.manifest",
    REPO / "templates" / "opencode" / ".project_manager" / "engine.manifest",
)


@pytest.mark.parametrize("manifest_path", _MANIFEST_PATHS, ids=lambda p: p.relative_to(REPO).as_posix())
def test_manifest_bare_entries_are_a_superset_of_the_pre_regression_baseline(manifest_path):
    """rebase 충돌 해소가 manifest 를 통째로 교체하면서 기존 등재(`private_refs.py` 등)를
    지운 회귀를 막는다 — `task/main` 커밋 시점의 행 집합이 이 브랜치 manifest 행 집합의
    부분집합이어야 한다(누락 0). `task/main` 참조가 이 환경에 없으면(fresh clone·다른 워크트리)
    비교 기준이 없다는 뜻이라 skip 한다 — 이 검증은 이 저장소의 이 시점(rebase 직후)을
    겨냥한 값 확인이다."""
    rel = manifest_path.relative_to(REPO).as_posix()
    baseline = subprocess.run(
        ["git", "show", f"task/main:{rel}"],
        cwd=str(REPO), capture_output=True, text=True, timeout=10,
    )
    if baseline.returncode != 0:
        pytest.skip(f"task/main 참조 없음 — 이 환경에선 기준선 비교 불가: {baseline.stderr.strip()}")
    baseline_lines = {line for line in baseline.stdout.splitlines() if line.strip()}
    current_lines = {
        line for line in manifest_path.read_text(encoding="utf-8").splitlines() if line.strip()
    }
    missing = baseline_lines - current_lines
    # 선언된 retire는 기존 bare 파일과 그 이름을 설명하던 주석의 의도적 제거다. 그 밖의
    # baseline 행은 계속 부분집합이어야 하므로 rename이 unrelated manifest 누락을 가리지 않는다.
    retired_stems = {
        Path(line.split(":", 1)[1].split("->", 1)[0].strip()).stem
        for line in current_lines
        if line.startswith("# pm-retired-path:")
    }
    missing = {line for line in missing
               if not any(stem in line for stem in retired_stems)}
    assert not missing, f"{rel} 에서 task/main 대비 누락된 행: {sorted(missing)}"
    # 이번 라운드의 두 신규 bare 등재도 값으로 함께 확인한다(F-001 이 지운 두 항목).
    assert ".project_manager/tools/private_refs.py" in current_lines
    assert ".project_manager/tools/pm_principles.py" in current_lines
    assert ".project_manager/wiki/pm_principles.md" in current_lines


# ── 4. opencode plugin core 순수함수 자가검증(node) ──────────────────────

_OPENCODE_LIB = REPO / "templates" / "opencode" / ".opencode" / "lib"
_PRINCIPLE_RECALL_CORE = _OPENCODE_LIB / "principle-recall-core.cjs"
_NODE = shutil.which("node")


def _run_node_check(script: str) -> str:
    return subprocess.run(
        [_NODE, "-e", script],
        cwd=str(_OPENCODE_LIB),
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout


def _install_recall_core(root: Path) -> Path:
    """core 사본을 픽스처의 `<root>/.opencode/lib/` 에 둔다 — 설치 형상 그대로.

    core 는 엔진 루트를 자기 위치(`path.resolve(__dirname, "..", "..")`)에서 내므로, 소스
    트리에서 require 하면 judge-recall 이 `templates/opencode` 의 registry 를 읽고 그 트리에
    marker 를 쓴다. 팩토리를 구동하는 검증은 이 사본을 cwd 로 돌린다.
    """
    lib = root / ".opencode" / "lib"
    lib.mkdir(parents=True, exist_ok=True)
    for name in ("principle-recall-core.cjs", "warning-channel-core.cjs"):
        shutil.copyfile(_OPENCODE_LIB / name, lib / name)
    return lib


def test_opencode_principle_recall_core_requires_cleanly_in_node():
    if _NODE is None:
        pytest.skip("node 없음 — require 검증 skip")
    out = _run_node_check(
        'require("./principle-recall-core.cjs"); console.log("REQUIRE_OK");'
    )
    assert "REQUIRE_OK" in out, f"core 모듈 require 실패: {out!r}"


def test_opencode_principle_recall_core_pure_functions_selfcheck():
    """toolSignal/extractPromptText 순수함수를 opencode 런타임 없이 검증(node)."""
    if _NODE is None:
        pytest.skip("node 없음 — 순수 로직 자가검증 skip(정적 검증만 적용)")
    script = r"""
const m = require("./principle-recall-core.cjs");
const assert = require("node:assert");

assert.deepStrictEqual(
  m.toolSignal("Bash", {command: "git push"}),
  {on: "shell", text: "git push"},
);
assert.deepStrictEqual(
  m.toolSignal("edit", {filePath: "tests/test_x.py"}),
  {on: "edit", text: "tests/test_x.py"},
);
assert.deepStrictEqual(
  m.toolSignal("task", {subagent_type: "developer"}),
  {on: "delegate", text: "developer"},
);
assert.strictEqual(m.toolSignal("read", {}), null);
assert.strictEqual(m.extractPromptText({text: "direct"}), "direct");
assert.strictEqual(
  m.extractPromptText({parts: [{text: "a"}, {text: "b"}]}),
  "a\nb",
);
assert.strictEqual(m.extractPromptText({}), "");
assert.strictEqual(typeof m.makePrincipleRecallPlugin, "function");
console.log("SELFCHECK_OK");
"""
    out = _run_node_check(script)
    assert "SELFCHECK_OK" in out, out


def test_opencode_principle_recall_core_fires_suppresses_and_rearms_on_compaction(tmp_path):
    """실 python subprocess(judge-recall/rearm) 를 통한 값 사이클 — 발화 → 같은 세션 재호출
    억제(marker dedup) → `session.compacted` 재무장 → 재발화. 순수함수 자가검증과 달리 마커
    파일 IO 를 실제로 태운다(judge 인자를 mock 하지 않는다)."""
    if _NODE is None:
        pytest.skip("node 없음 — 재무장 사이클 skip")
    _write_engine_tools(tmp_path / ".project_manager" / "tools")
    wiki_dir = tmp_path / ".project_manager" / "wiki"
    wiki_dir.mkdir(parents=True)
    (wiki_dir / "pm_principles.md").write_text(
        "#### 번들\n- `[shell: git\\s+push]` 재무장 사이클 확인용 규칙 본문.\n",
        encoding="utf-8",
    )
    script = f"""
const assert = require("node:assert");
const m = require("./principle-recall-core.cjs");
const root = {json.dumps(str(tmp_path))};
const sessionID = "rearm-cycle-session";

async function fireCount() {{
  const plugin = m.makePrincipleRecallPlugin();
  const instance = await plugin({{ client: {{}}, directory: root }});
  const output = {{ system: [] }};
  await instance["tool.execute.before"](
    {{ tool: "Bash", sessionID }}, {{ args: {{ command: "git push" }} }},
  );
  instance["experimental.chat.system.transform"]({{ sessionID }}, output);
  return output.system.length;
}}

async function compact() {{
  const plugin = m.makePrincipleRecallPlugin();
  const instance = await plugin({{ client: {{}}, directory: root }});
  await instance.event({{ event: {{ type: "session.compacted", properties: {{ sessionID }} }} }});
}}

(async () => {{
  const first = await fireCount();
  assert.strictEqual(first, 1, "1차 발화 실패: " + first);
  const second = await fireCount();
  assert.strictEqual(second, 0, "같은 세션 재호출이 억제되지 않음: " + second);
  await compact();
  const third = await fireCount();
  assert.strictEqual(third, 1, "session.compacted 재무장 뒤 재발화 실패: " + third);
  console.log("REARM_CYCLE_OK");
}})().catch((error) => {{ console.error(error); process.exit(1); }});
"""
    result = subprocess.run(
        ["node", "-e", script], cwd=str(_install_recall_core(tmp_path)),
        capture_output=True, text=True, timeout=30,
    )
    assert "REARM_CYCLE_OK" in result.stdout, (result.stdout, result.stderr)
    assert not (_OPENCODE_LIB.parents[1] / ".project_manager" / ".local").exists(), (
        "팩토리 구동이 templates/opencode 소스 트리에 marker 를 남김"
    )


def test_opencode_principle_recall_core_surfaces_broken_registry_warning(tmp_path):
    """opencode 축도 파손 경고를 무출력으로 버리지 않는다 — `tool.execute.before` 가 예외 없이
    끝나면(node 프로세스 rc0 과 동형) system.transform 출력에 경고 문안이 실린다."""
    if _NODE is None:
        pytest.skip("node 없음 — 파손 경고 표면화 skip")
    _write_engine_tools(tmp_path / ".project_manager" / "tools")
    wiki_dir = tmp_path / ".project_manager" / "wiki"
    wiki_dir.mkdir(parents=True)
    (wiki_dir / "pm_principles.md").write_text(
        "#### 번들\n- `[shell: (unclosed]` 파손 항목.\n", encoding="utf-8",
    )
    script = f"""
const assert = require("node:assert");
const m = require("./principle-recall-core.cjs");
const root = {json.dumps(str(tmp_path))};
const sessionID = "broken-registry-session";

(async () => {{
  const plugin = m.makePrincipleRecallPlugin();
  const instance = await plugin({{ client: {{}}, directory: root }});
  const output = {{ system: [] }};
  await instance["tool.execute.before"](
    {{ tool: "Bash", sessionID }}, {{ args: {{ command: "ls -la" }} }},
  );
  instance["experimental.chat.system.transform"]({{ sessionID }}, output);
  assert.strictEqual(output.system.length, 1, "파손 경고가 표면화되지 않음: " + JSON.stringify(output));
  assert.ok(output.system[0].includes("[principle-recall]"), output.system[0]);
  assert.ok(output.system[0].includes("파손"), output.system[0]);
  console.log("BROKEN_WARNING_OK");
}})().catch((error) => {{ console.error(error); process.exit(1); }});
"""
    result = subprocess.run(
        ["node", "-e", script], cwd=str(_install_recall_core(tmp_path)),
        capture_output=True, text=True, timeout=30,
    )
    assert "BROKEN_WARNING_OK" in result.stdout, (result.stdout, result.stderr)


# ── 5. codex 어댑터 — 실 캡처 fixture 값 확인 ─────────────────────────────

def _write_codex_root(root: Path, registry_text: str) -> None:
    """codex `_load_principles(root)` 가 읽는 실 파일 트리를 tmp root 에 배치한다."""
    wiki = root / ".project_manager" / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / "pm_principles.md").write_text(registry_text, encoding="utf-8")
    _write_engine_tools(root / ".project_manager" / "tools")


def test_codex_bash_tool_name_maps_to_shell_axis_using_the_live_fixture(codex):
    """실 캡처 fixture 의 Bash PreToolUse 이벤트가 shell 축으로 판별된다 — 이전엔
    `tool == "shell"` 만 처리해 실 payload(tool_name=`Bash`)에서 무발화였다."""
    payload = _codex_live_bash_event()
    signal = codex._principle_recall_signal(payload)
    assert signal == ("shell", payload["tool_input"]["command"])


def test_codex_collaborationspawn_agent_still_maps_to_delegate_axis(codex):
    """Bash 매핑 수정이 delegate 축(다른 tool_name)을 건드리지 않는다(역방향 확인)."""
    payload = _codex_live_delegate_event()
    signal = codex._principle_recall_signal(payload)
    assert signal is not None
    assert signal[0] == "delegate"
    assert signal[1] == payload["tool_input"]["task_name"]


def test_codex_apply_patch_tool_name_is_not_misrouted_to_shell_axis(codex):
    """Bash→shell 매핑이 codex 의 다른 도구(apply_patch)를 shell 로 오판하지 않는다(역방향
    확인) — apply_patch 자체는 fixture 에 이벤트가 없어 필드 추정치인 edit 축으로 간다."""
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "apply_patch",
        "tool_input": {"file_path": "tests/test_x.py"},
    }
    signal = codex._principle_recall_signal(payload)
    assert signal == ("edit", "tests/test_x.py")


def test_codex_apply_patch_edit_signal_is_a_synthetic_field_estimate_not_fixture_verified(codex):
    """codex 캡처 fixture 에는 apply_patch PreToolUse 이벤트가 없다 — 이 테스트는 추정 필드명
    (file_path/path/input)의 합성 입력 확인이지 라이브 도달 증거가 아니다. fixture 가 그 이벤트를
    얻으면 이 테스트를 실 필드명으로 교체해야 한다(아래 guard 가 그 시점을 표시한다)."""
    data = json.loads(CODEX_LIVE_HOOK_FIXTURE.read_text(encoding="utf-8"))
    assert not [e for e in data["events"] if e.get("tool_name") == "apply_patch"], (
        "fixture 에 apply_patch 이벤트가 생겼다 — 이 테스트를 그 실 필드명으로 교체하라"
    )
    for field in ("file_path", "path", "input"):
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "apply_patch",
            "tool_input": {field: "tests/test_x.py"},
        }
        assert codex._principle_recall_signal(payload) == ("edit", "tests/test_x.py"), field


def test_codex_principle_recall_envelope_surfaces_broken_registry_warning(codex, tmp_path):
    """codex `principle_recall_envelope` 도 파손 경고를 무출력으로 버리지 않는다 — 비차단이라
    `decision` 키가 없다(codex 축의 "rc0" 과 동형: 판정을 막지 않는 엔벨로프 형태)."""
    _write_codex_root(tmp_path, "#### 공유\n- `[shell: (unclosed]` 파손 항목.\n")
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "ls -la"},
        "session_id": "sess-codex-broken",
    }
    envelope = codex.principle_recall_envelope(payload, tmp_path)
    assert envelope != {}
    assert "decision" not in envelope
    guidance = envelope["hookSpecificOutput"]["additionalContext"]
    assert "[principle-recall]" in guidance
    assert "파손" in guidance


def test_codex_cap_envelope_additional_context_at_exact_cap_is_untouched(codex):
    """실제 `CODEX_ADDITIONAL_CONTEXT_MAX_CHARS`(10,000) 경계 — 정확히 상한이면 원문 그대로다."""
    cap = codex.CODEX_ADDITIONAL_CONTEXT_MAX_CHARS
    text = "x" * cap
    envelope = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": text}}
    result = codex._cap_envelope_additional_context(envelope)
    assert result["hookSpecificOutput"]["additionalContext"] == text


def test_codex_cap_envelope_additional_context_over_by_one_char_is_summarized(codex):
    """상한을 딱 1자 넘기면(10,001) 원문을 자르지 않고 생략 표시로 접는다(원문 잔존 없음)."""
    cap = codex.CODEX_ADDITIONAL_CONTEXT_MAX_CHARS
    text = "x" * (cap + 1)
    envelope = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": text}}
    result = codex._cap_envelope_additional_context(envelope)
    capped = result["hookSpecificOutput"]["additionalContext"]
    assert len(capped) <= cap
    assert "x" * 50 not in capped
    assert "[principle-recall]" in capped


def test_codex_merge_hook_envelopes_caps_the_single_answer_early_return_path(codex):
    """`len(answered) == 1` 조기 반환 경로도 최종 상한을 강제한다(다중 합본을 안 거쳤다고 예외
    아님 — F-006 은 이 경로가 무제한이었다)."""
    cap = codex.CODEX_ADDITIONAL_CONTEXT_MAX_CHARS
    text = "x" * (cap + 1)
    merged = codex.merge_hook_envelopes([
        {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": text}},
    ])
    assert len(merged["hookSpecificOutput"]["additionalContext"]) <= cap


def test_codex_merge_hook_envelopes_caps_the_combined_multi_answer_path(codex):
    """짧은 응답(ctx-nudge) + 거의 상한까지 찬 응답(recall) 을 합치면 총합이 상한을 넘긴다
    (리뷰 실측 10,091 재현 — recall 단독 상한과 별개로 합본 뒤 재검사가 없었다). 원문을 그대로
    자르지 않고 생략 표시로 접는다(비차단 — `decision` 키 없음)."""
    cap = codex.CODEX_ADDITIONAL_CONTEXT_MAX_CHARS
    nudge = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "ctx-nudge 안내"}}
    recall = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "x" * cap}}
    merged = codex.merge_hook_envelopes([nudge, recall])
    combined = merged["hookSpecificOutput"]["additionalContext"]
    assert len(combined) <= cap
    assert "x" * 50 not in combined
    assert "decision" not in merged
