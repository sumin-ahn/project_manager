"""출하 문서의 PM 스킬 진입 표기를 하네스별 실제 동작과 맞춘다.

격리 프로브의 스킬 본문에만 둔 마커로 확인한 진입 표기는 다음과 같다.

* claude: 하네스 슬래시 커맨드 ``/pm-bootstrap``
* codex: 엔진 멘션 확장 ``$pm-bootstrap``
* opencode: ``.claude/skills`` 소비 경로에서 동작하는 ``/pm-bootstrap`` 표기

opencode 측정은 비대화형 ``opencode run`` 기준이다. 대화형 TUI의 팝업 거동까지
확인했다는 뜻으로 넓히지 않는다. 다만 ``/pm-*``가 opencode 슬래시 커맨드가
아니라는 점은 별도 미존재 커맨드 프로브로 확인됐다.

조건부 렌더 출하 표면은 다음 한 축이다. 파일 집합은 하네스별 실제 skills root에서
``*/SKILL.md``로 파생하며 현재 각 15개다. 이름 접두사와 무관하게 모든 카드를 포함한다.

================  ========================================  =========================
template dir      render 대상                               호출 토큰 산출
================  ========================================  =========================
claude_code       ``.claude/skills/*/SKILL.md``             ``/<card>``
codex             ``.agents/skills/*/SKILL.md``             ``$<card>``
opencode          ``.claude/skills/*/SKILL.md``             ``/<card>``
================  ========================================  =========================

본문 전체를 별도 템플릿으로 소유하지 않는다. canonical 카드의 호출 문맥 ``/pm-*``만
표기 토큰으로 렌더하고, 경로 문자열과 operational placeholder는 그대로 복사한다.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from _harness_matrix import (
    HARNESSES,
    REPO,
    TEMPLATES,
    _PM_IMPORT,
    entry_docs,
)
from _repo_owned_inventory import OWNED, repo_owned_paths


def _load_pm_render():
    path = REPO / ".project_manager" / "tools" / "pm_render.py"
    spec = importlib.util.spec_from_file_location("skill_entry_pm_render", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_PM_RENDER = _load_pm_render()


def _load_pm_update():
    path = REPO / ".project_manager" / "tools" / "pm_update.py"
    spec = importlib.util.spec_from_file_location("skill_entry_pm_update", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_PM_UPDATE = _load_pm_update()


def _template_dir(harness: str) -> str:
    (dirname,) = _PM_IMPORT.HARNESS_TEMPLATE_DIRS[harness]
    return dirname


# 실행 프로브로 확정한 독립 oracle. production registry가 잘못 바뀌어도 함께 green이 되지 않는다.
_ENTRY_PREFIX = {"claude": "/", "codex": "$", "opencode": "/"}
_ENTRY_NAME_ALT = "|".join(
    re.escape(name)
    for name in sorted(_PM_RENDER.SKILL_ENTRY_NAMES, key=len, reverse=True)
)
_PM_ENTRY_NAME_ALT = "|".join(
    re.escape(name)
    for name in sorted(
        (name for name in _PM_RENDER.SKILL_ENTRY_NAMES if name.startswith("pm-")),
        key=len,
        reverse=True,
    )
)


def _wrong_prefix(harness: str) -> str:
    return "/" if _ENTRY_PREFIX[harness] == "$" else "$"

# 경로의 ``/pm-*``는 호출 표기가 아니다. 앞 문자가 경로 구성 문자인 경우를
# 제외해 ``.claude/skills/pm-*``·``<manager>/pm-import.sh``를 허용한다.
_PREFIXED_ENTRY = re.compile(
    rf"(?<![\w.>/=\-])(?<!\]\()(?P<prefix>[/\$])(?P<entry>{_ENTRY_NAME_ALT})"
    # renderer의 경계식을 재사용하지 않는 독립 oracle. ``.`` 뒤에 식별자 문자가 오면
    # 확장자(`/pm-bootstrap.md`)라 제외하지만, 문장 끝 ``/pm-handoff.``의 마침표는
    # 호출 뒤 문장부호라 오표기로 잡는다(B1).
    rf"(?![A-Za-z0-9_>/\-]|\.[A-Za-z0-9_])"
)
_BARE_ENTRY_LINE = re.compile(
    # bare 비-pm 이름은 산문 목록의 줄바꿈(``spike-new … 전체``)과 구분할 문법이 없다.
    # 모든 카드의 잘못된 /$는 PREFIXED + heading 가드가 맡고, legacy bare 규율은 PM 명령만 유지한다.
    rf"(?m)^[ \t]*(?:[#>`*-]+[ \t]*)?(?P<entry>{_PM_ENTRY_NAME_ALT})"
    r"(?=$|[ \t`])"
)
_BARE_ENTRY_INLINE = re.compile(
    rf"(?<![A-Za-z0-9_.>/\-])`(?P<entry>{_PM_ENTRY_NAME_ALT})(?=`|[ \t])"
)


def _template_root(harness: str) -> Path:
    return TEMPLATES / _template_dir(harness)


def _shipping_texts(harness: str) -> list[Path]:
    root = _template_root(harness)
    return sorted(
        path
        for path in repo_owned_paths(REPO, root.relative_to(REPO), mode=OWNED)
        # 확장자 목록을 만들지 않고 board render-leak과 같은 production 판정을 재사용한다.
        if _PM_UPDATE._is_text_source(path)
        # tools 사본은 모든 하네스에 byte-identical인 엔진 backbone이다. 하네스별 어댑터 진입
        # 표면이 아니며 per-harness 치환 대상이 아니므로 제외한다. hook/config/JS/driver는 포함한다.
        if ".project_manager/tools" not in path.as_posix()
    )


def _shared_docs(root: Path = REPO / ".project_manager" / "wiki") -> list[Path]:
    return sorted(
        path
        for path in repo_owned_paths(REPO, root.relative_to(REPO), mode=OWNED)
        if _PM_UPDATE._is_text_source(path)
    )


def _has_render_channel(harness: str, path: Path) -> bool:
    root = _template_root(harness)
    rel = path.relative_to(root).as_posix()
    entries = {
        str(entry).replace("\\", "/")
        for entry in _PM_UPDATE.read_manifest(
            root / ".project_manager" / "engine.manifest"
        )
    }
    return any(
        rel == entry or rel.startswith(entry.rstrip("/") + "/")
        for entry in entries
    )


def _notation_issues(harness: str, text: str) -> list[str]:
    expected = _ENTRY_PREFIX[harness]
    issues = []
    for match in _PREFIXED_ENTRY.finditer(text):
        if match.group("prefix") == expected:
            continue
        line_start = text.rfind("\n", 0, match.start()) + 1
        line_end = text.find("\n", match.end())
        line = text[line_start : len(text) if line_end < 0 else line_end]
        issues.append(match.group(0))
    if expected in {"/", "$"}:
        issues.extend(match.group("entry") for match in _BARE_ENTRY_LINE.finditer(text))
        issues.extend(match.group("entry") for match in _BARE_ENTRY_INLINE.finditer(text))
    return issues


def _skill_cards(harness: str) -> list[Path]:
    root = _template_root(harness)
    # opencode는 .opencode가 실행 어댑터지만 스킬은 .claude/skills를 단일 소비한다.
    # 하네스별 디렉터리를 다시 손으로 열거하지 않고 실제 출하 카드 root를 찾는다.
    return sorted(root.glob(".*/skills/*/SKILL.md"))


def test_measured_entry_notation_table_covers_derived_harness_axis():
    assert set(_ENTRY_PREFIX) == set(HARNESSES)
    derived_template_dirs = {_template_dir(harness) for harness in HARNESSES}
    assert set(_PM_RENDER.SKILL_ENTRY_PREFIX_BY_TEMPLATE_DIR) == derived_template_dirs
    assert {
        harness: _PM_RENDER.SKILL_ENTRY_PREFIX_BY_TEMPLATE_DIR[_template_dir(harness)]
        for harness in HARNESSES
    } == _ENTRY_PREFIX
    canonical_names = {
        path.parent.name
        for path in (REPO / ".claude" / "skills").glob("*/SKILL.md")
    }
    assert set(_PM_RENDER.SKILL_ENTRY_NAMES) == canonical_names
    assert canonical_names
    assert {
        harness: {path.parent.name for path in _skill_cards(harness)}
        for harness in HARNESSES
    } == {harness: canonical_names for harness in HARNESSES}


def test_measured_oracle_is_independent_from_registry_mutation(monkeypatch):
    monkeypatch.setitem(
        _PM_RENDER.SKILL_ENTRY_PREFIX_BY_TEMPLATE_DIR, "opencode", ""
    )
    derived = {
        harness: _PM_RENDER.SKILL_ENTRY_PREFIX_BY_TEMPLATE_DIR[_template_dir(harness)]
        for harness in HARNESSES
    }
    assert derived != _ENTRY_PREFIX, "production registry 오염이 실측 oracle과 함께 움직임"


@pytest.mark.parametrize("harness", HARNESSES)
def test_renderer_changes_only_canonical_call_tokens(harness):
    prefix = _ENTRY_PREFIX[harness]
    source = (
        "`/pm-bootstrap --task main` "
        ".claude/skills/pm-bootstrap/SKILL.md "
        "codex example `$pm-handoff` "
        "python3 .project_manager/tools/pm_update.py\n"
    )
    expected = (
        f"`{prefix}pm-bootstrap --task main` "
        ".claude/skills/pm-bootstrap/SKILL.md "
        "codex example `$pm-handoff` "
        "python3 .project_manager/tools/pm_update.py\n"
    )
    rendered = _PM_RENDER.render_skill_entry_notation(
        source, _template_dir(harness)
    )
    assert rendered == expected
    assert _PM_RENDER.render_skill_entry_notation(
        rendered, _template_dir(harness)
    ) == rendered


def test_renderer_combines_only_installed_harnesses_on_shared_path():
    source = "run `/pm-bootstrap --task main`\n"
    rendered = _PM_RENDER.render_skill_entry_notation(
        source, ("codex", "opencode")
    )
    assert rendered == "run `$pm-bootstrap --task main`(codex) / `/pm-bootstrap --task main`(opencode)\n"
    assert _PM_RENDER.render_skill_entry_notation(
        rendered, ("codex", "opencode")
    ) == rendered
    assert _PM_RENDER.render_skill_entry_notation(
        source, ("claude_code", "codex", "opencode")
    ) == (
        "run `/pm-bootstrap --task main`(claude·opencode) / "
        "`$pm-bootstrap --task main`(codex)\n"
    )
    assert _PM_RENDER.render_skill_entry_notation(
        rendered, ("claude_code", "codex", "opencode")
    ) == (
        "run `/pm-bootstrap --task main`(claude·opencode) / "
        "`$pm-bootstrap --task main`(codex)\n"
    )


@pytest.mark.parametrize(
    "non_call",
    (
        "/pm-bootstrap.md",
        "/pm-bootstrap/path",
        "/pm-bootstrap-extra",
        "prefix-/pm-bootstrap",
        ".claude/skills/pm-bootstrap/SKILL.md",
    ),
)
def test_renderer_excludes_extensions_paths_and_hyphen_identifiers(non_call):
    assert _PM_RENDER.render_skill_entry_notation(non_call, "codex") == non_call


@pytest.mark.parametrize(
    ("source", "expected"),
    (
        ("`/pm-handoff`.", "`$pm-handoff`."),
        ("`/pm-wave-claim ISSUE-1234`.", "`$pm-wave-claim ISSUE-1234`."),
        ("`/pm-handoff.`", "`/pm-handoff.`"),
        ("/pm-handoff.", "$pm-handoff."),
    ),
)
def test_renderer_limits_path_boundary_to_inside_inline_code(source, expected):
    assert _PM_RENDER.render_skill_entry_notation(source, "codex") == expected


def test_renderer_fails_loud_for_unregistered_template_with_call_token():
    with pytest.raises(_PM_RENDER.RenderLeakError, match="미등록"):
        _PM_RENDER.render_skill_entry_notation("/pm-bootstrap\n", "fourth")


def test_renderer_unknown_template_without_call_token_is_noop():
    text = ".claude/skills/pm-bootstrap/SKILL.md and `$pm-bootstrap`\n"
    assert _PM_RENDER.render_skill_entry_notation(text, "fourth") == text


def test_renderer_includes_non_pm_skill_discovered_from_card_inventory():
    assert _PM_RENDER.render_skill_entry_notation(
        "run `/spike-new topic`\n", "codex"
    ) == "run `$spike-new topic`\n"


@pytest.mark.parametrize(
    "non_call",
    (
        "https://x.dev/a?p=/pm-handoff",
        "실행/pm-handoff",
        "[x](/pm-handoff)",
    ),
)
def test_renderer_excludes_url_korean_adjacency_and_markdown_link_target(non_call):
    assert _PM_RENDER.render_skill_entry_notation(non_call, "codex") == non_call
    assert not _notation_issues("codex", non_call)


def test_shared_canonical_keeps_slash_skill_entries():
    docs = _shared_docs()
    assert docs, "shared wiki inventory is empty"
    failures = {
        path.relative_to(REPO).as_posix(): _notation_issues(
            "claude", path.read_text(encoding="utf-8")
        )
        for path in docs
        if _notation_issues("claude", path.read_text(encoding="utf-8"))
    }
    assert not failures, f"shared wiki canonical slash notation drift: {failures}"


@pytest.mark.parametrize("harness", HARNESSES)
def test_every_shipping_text_rejects_other_harness_prefixes(harness):
    docs = _shipping_texts(harness)
    assert docs, f"{harness} shipping text inventory is empty"
    failures = {}
    for path in docs:
        text = path.read_text(encoding="utf-8")
        # template wiki는 canonical slash source다. 실제 import/self-update와 같은 하네스
        # context로 렌더한 **산출**을 독립 guard에 넣어 wiki를 검사 범위에서 빼지 않는다.
        if (
            ".project_manager/wiki/" in path.as_posix()
            and _has_render_channel(harness, path)
        ):
            text = _PM_RENDER.render_skill_entry_notation(
                text, _template_dir(harness), source=path.as_posix()
            )
        if issues := _notation_issues(harness, text):
            failures[path.relative_to(REPO).as_posix()] = issues
    assert not failures, f"wrong PM skill entry notation for {harness}: {failures}"


@pytest.mark.parametrize("harness", HARNESSES)
def test_every_skill_card_heading_uses_native_entry_notation(harness):
    cards = _skill_cards(harness)
    assert cards, f"{harness} skill card inventory is empty"
    failures = {}
    for path in cards:
        skill_name = path.parent.name
        heading = re.compile(
            rf"^# {re.escape(_ENTRY_PREFIX[harness] + skill_name)}(?:\s+.*?)? —",
            re.MULTILINE,
        )
        if not heading.search(path.read_text(encoding="utf-8")):
            failures[path.relative_to(REPO).as_posix()] = heading.pattern
    assert not failures, f"skill card heading entry notation mismatch: {failures}"


@pytest.mark.parametrize("harness", HARNESSES)
def test_root_entry_docs_surface_native_bootstrap_and_handoff(harness):
    root = _template_root(harness)
    docs = [root / name for name in entry_docs(harness) if (root / name).is_file()]
    assert docs, f"{harness} root entry document inventory is empty"
    full = docs[0].read_text(encoding="utf-8")
    prefix = _ENTRY_PREFIX[harness]
    assert f"`{prefix}pm-bootstrap" in full


@pytest.mark.parametrize("harness", HARNESSES)
def test_guard_is_sensitive_to_wrong_prefix_in_real_entry_document(harness):
    root = _template_root(harness)
    path = root / entry_docs(harness)[0]
    original = path.read_text(encoding="utf-8")
    native = f"{_ENTRY_PREFIX[harness]}pm-bootstrap"
    wrong = f"{_wrong_prefix(harness)}pm-bootstrap"
    assert f"`{native}" in original, f"sensitivity fixture absent from {path}"
    poisoned = original.replace(f"`{native}", f"`{wrong}", 1)
    assert wrong in _notation_issues(harness, poisoned)


@pytest.mark.parametrize("harness", HARNESSES)
def test_fenced_examples_are_not_a_wrong_prefix_escape_hatch(harness):
    prefix = _ENTRY_PREFIX[harness]
    valid = f"```text\n{prefix}pm-bootstrap --task main\n```"
    wrong = f"```text\n{_wrong_prefix(harness)}pm-bootstrap --task main\n```"
    assert not _notation_issues(harness, valid)
    assert _notation_issues(harness, wrong)


@pytest.mark.parametrize("harness", HARNESSES)
def test_bare_command_line_is_not_a_prefix_escape_hatch(harness):
    assert _notation_issues(harness, "```text\npm-bootstrap --task main\n```")


@pytest.mark.parametrize("harness", HARNESSES)
def test_mid_line_inline_bare_entry_is_not_a_prefix_escape_hatch(harness):
    issues = _notation_issues(
        harness, "설명 중간의 `pm-bootstrap --task main` 호출도 잡는다."
    )
    assert issues == ["pm-bootstrap"]


def test_shipping_inventory_includes_non_markdown_hook_config_js_and_python():
    expected = {
        "templates/codex/.codex/hooks.json",
        "templates/codex/.codex/config.toml",
        "templates/codex/.codex/pm_orch_codex.py",
        "templates/opencode/.opencode/lib/ctx-guard-core.cjs",
        "templates/opencode/.opencode/pm_orch_opencode.py",
    }
    actual = {
        path.relative_to(REPO).as_posix()
        for harness in HARNESSES
        for path in _shipping_texts(harness)
    }
    assert expected <= actual


@pytest.mark.parametrize("harness", HARNESSES)
def test_every_shipped_wiki_with_skill_entry_has_render_channel(harness):
    if _ENTRY_PREFIX[harness] == "/":
        pytest.skip("canonical slash와 native 표기가 같아 render channel이 필요하지 않음")
    root = _template_root(harness)
    uncovered = []
    for path in _shipping_texts(harness):
        rel = path.relative_to(root).as_posix()
        if not rel.startswith(".project_manager/wiki/"):
            continue
        text = path.read_text(encoding="utf-8")
        if not _PREFIXED_ENTRY.search(text):
            continue
        if not _has_render_channel(harness, path):
            uncovered.append(rel)
    assert not uncovered, f"skill entry가 있으나 render channel 밖인 wiki: {uncovered}"


def test_non_markdown_injected_message_poisoning_is_detected():
    valid = '{"systemMessage":"run $pm-handoff now"}'
    poisoned = valid.replace("$pm-handoff", "/pm-handoff")
    assert not _notation_issues("codex", valid)
    assert _notation_issues("codex", poisoned) == ["/pm-handoff"]


def test_guard_detects_bare_wrong_prefix_followed_by_sentence_period():
    assert _notation_issues("codex", "다음은 /pm-handoff. 이후 종료한다.") == [
        "/pm-handoff"
    ]


def test_opencode_entry_doc_states_both_true_notation_facts():
    """slash 표기는 동작하지만 OpenCode 자체 slash command라는 거짓 설명은 금지한다."""
    text = (TEMPLATES / "opencode" / "AGENTS.md").read_text(encoding="utf-8")
    assert "`/pm-bootstrap" in text
    assert "자체 slash command를 뜻하지 않는다" in text


def test_paths_and_python_backbones_are_not_classified_as_skill_entries():
    prose = " ".join(
        (
            ".claude/skills/pm-bootstrap/SKILL.md",
            ".opencode/pm-instructions.md",
            "<manager>/pm-import.sh",
            "./pm-update.sh",
            "python3 .project_manager/tools/pm_bootstrap.py",
        )
    )
    assert not _PREFIXED_ENTRY.search(prose)


def test_guard_excludes_extensions_paths_and_hyphen_identifiers():
    prose = " ".join(
        (
            "/pm-bootstrap.md",
            "/pm-bootstrap/path",
            "/pm-bootstrap-extra",
            "prefix-/pm-bootstrap",
            "pm-bootstrap.md",
            "pm-bootstrap/path",
            "pm-bootstrap-extra",
        )
    )
    assert not _notation_issues("codex", prose)
