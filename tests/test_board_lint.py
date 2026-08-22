"""board.py 파일명-무관 참조 lint + push 게이트 단위테스트 (T-0036).

`lint_unstable_refs()` 와 `lint --gate` 종료코드 분기, pre-push 훅 본문을 검증한다.

  - lint 함수는 모듈-레벨 경로 상수(REPO·TICKETS_DIR·DECISIONS_DIR·IDEAS_DIR)를 tmp_path
    로 monkeypatch 해 구동한다 — **실 .project_manager/wiki/ 미접촉**. `_collect_wikilink_files`
    가 `REPO/.project_manager/wiki` 를 직접 계산하므로 tmp 트리를 그 레이아웃으로 깐다.
  - 훅 테스트는 `install_pre_push_hook` 의 `_hooks_dir`/`_detect_py` 를 stub 해 tmp 에 쓴다.

도구는 패키지가 아니므로 importlib 동적 로드 (test_pm_log 의 _load_module 관용구).
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from _win_skip import _can_symlink

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
BOARD_PY = TOOLS / "board.py"

# symlink 생성 불가 환경(권한 없는 Windows 등)에서 symlink 의존 테스트를 skip (test_pm_import 동형).
requires_symlink = pytest.mark.skipif(
    not _can_symlink(),
    reason="Windows: symlink requires Developer Mode/admin",
)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_board():
    return _load_module("board", BOARD_PY)


@pytest.fixture
def board():
    return _load_board()


# ── tmp 와이어링 ──────────────────────────────────────────────────────────

def _wire_repo(board, monkeypatch, root: Path) -> Path:
    """모듈-레벨 경로 상수를 tmp 루트로 갈아끼우고 빈 구조화 트리를 만든다.

    `_collect_wikilink_files` 가 `REPO/.project_manager/wiki` 를 직접 계산하므로
    tmp 루트는 반드시 그 레이아웃을 따라야 한다. 반환값 = wiki 디렉토리.
    """
    wiki = root / ".project_manager" / "wiki"
    tickets = wiki / "tickets"
    ideas = wiki / "ideas"
    decisions = wiki / "decisions"
    for status in ("open", "claimed", "blocked", "done"):
        (tickets / status).mkdir(parents=True, exist_ok=True)
    for status in ("open", "promoted", "killed"):
        (ideas / status).mkdir(parents=True, exist_ok=True)
    decisions.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(board, "REPO", root)
    monkeypatch.setattr(board, "TICKETS_DIR", tickets)
    monkeypatch.setattr(board, "IDEAS_DIR", ideas)
    monkeypatch.setattr(board, "DECISIONS_DIR", decisions)
    return wiki


def _adr(wiki: Path, num: str, slug: str) -> Path:
    """decisions/<num>-<slug>.md 실재 ADR 파일을 만든다."""
    p = wiki / "decisions" / f"{num}-{slug}.md"
    p.write_text(f"---\nid: ADR-{num}\n---\n# ADR {num}\n", encoding="utf-8")
    return p


def _doc(wiki: Path, relname: str, text: str) -> Path:
    """wiki/ 아래 임의 .md 문서를 만든다 (참조를 담는 source)."""
    p = wiki / relname
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _kinds(issues) -> list[str]:
    return [kind for _name, kind, _detail in issues]


# ── ① 생파일명 markdown 경로 링크 dangling 포착 ──────────────────────────────

def test_md_path_link_to_missing_decision_is_dangling(board, monkeypatch, tmp_path):
    wiki = _wire_repo(board, monkeypatch, tmp_path)
    # 실재 ADR 은 0006, 링크는 환각한 0006-...adapter.md (실재 파일명과 다름).
    _adr(wiki, "0006", "opencode-adapter-target")
    _doc(wiki, "note.md",
         "see [target](../../decisions/0006-opencode-something-adapter.md) for detail")
    issues = board.lint_unstable_refs()
    assert any(
        kind == "unstable-ref"
        and "0006-opencode-something-adapter.md" in name
        and "실재 안 함" in detail
        and "[[ADR-0006]]" in detail
        for name, kind, detail in issues
    ), issues


def test_md_path_link_without_leading_slash_is_caught(board, monkeypatch, tmp_path):
    """앞 경로 없는 wiki-루트 상대 링크 `](decisions/<slug>.md)` 도 포착 (codex T-0036 must-fix).

    수정 전 정규식 `[^)]*?/decisions/` 는 `decisions/` 앞 `/` 를 요구해 이 형을 놓쳤다.
    `(?:[^)]*?/)?decisions/` 로 앞 경로를 선택화해 false-negative 를 막는다.
    """
    wiki = _wire_repo(board, monkeypatch, tmp_path)
    # wiki 루트 문서가 앞 `/` 없이 `decisions/...` 로 직접 가리킴 (실재 안 하는 슬러그).
    _doc(wiki, "status.md", "결정은 [link](decisions/0007-ghost-overlay.md) 참고")
    issues = board.lint_unstable_refs()
    assert any(
        kind == "unstable-ref"
        and name == "0007-ghost-overlay.md"
        and "[[ADR-0007]]" in detail
        for name, kind, detail in issues
    ), issues


def test_md_link_with_fragment_or_title_is_caught(board, monkeypatch, tmp_path):
    """`.md#sec` fragment·`.md "title"` 달린 링크도 포착 (codex T-0036 must-fix).

    `.md)` 로 끝나는 형만 잡으면 fragment/title 달린 dangling 링크가 게이트를 우회한다.
    target 추출 후 fragment/query 를 떼고 매칭해 둘 다 잡는다.
    """
    wiki = _wire_repo(board, monkeypatch, tmp_path)
    _doc(wiki, "a.md", "anchor [x](decisions/0007-ghost.md#section) 참고")
    _doc(wiki, "b.md", 'title [y](../../decisions/0007-ghost.md "ADR 7") 참고')
    issues = board.lint_unstable_refs()
    dangling = [i for i in issues if i[0] == "0007-ghost.md" and i[1] == "unstable-ref"]
    assert dangling, issues
    # a.md(fragment)·b.md(title) 둘 다 사용처로 잡혀야 한다.
    assert "a.md" in dangling[0][2] and "b.md" in dangling[0][2], dangling


def test_external_url_decision_path_not_flagged(board, monkeypatch, tmp_path):
    """외부 URL `https://…/decisions/<x>.md` 는 로컬 구조 참조로 오탐(오차단)하지 않는다 (codex suggestion).

    스킴이 있으면 로컬 파일이 아니므로 dangling 으로 막으면 거짓 차단이 된다.
    """
    wiki = _wire_repo(board, monkeypatch, tmp_path)
    _doc(wiki, "note.md",
         "외부 [link](https://example.com/wiki/decisions/0007-ghost.md) 참고")
    # protocol-relative URL `//host/…` 도 외부 — 오차단 금지 (codex T-0036).
    _doc(wiki, "note2.md",
         "protocol-relative [l](//example.com/wiki/decisions/0008-ghost.md) 참고")
    issues = board.lint_unstable_refs()
    assert not any("0007-ghost.md" in name for name, _k, _d in issues), issues
    assert not any("0008-ghost.md" in name for name, _k, _d in issues), issues


def test_code_span_and_fence_examples_not_flagged(board, monkeypatch, tmp_path):
    """코드 span/fence 안의 *예시* 링크는 실제 참조가 아니므로 차단 안 함 (codex T-0036·오탐 0).

    문서가 "나쁜 예시"로 `[x](decisions/NNNN-...)` 를 코드로 보여줘도 push 게이트를 막으면 안 된다."""
    wiki = _wire_repo(board, monkeypatch, tmp_path)
    _doc(wiki, "doc.md",
         "inline 나쁜 예시 `[bad](decisions/9999-ghost.md)` 는 무시해야 한다.\n\n"
         "```\n"
         "fenced 예시: [also bad](decisions/9998-ghost.md)\n"
         "```\n")
    issues = board.lint_unstable_refs()
    flagged = [n for n, _k, _d in issues if n in ("9999-ghost.md", "9998-ghost.md")]
    assert flagged == [], f"코드 안 예시 링크가 차단됨(오탐): {flagged}"


def test_md_link_single_quote_and_paren_title_is_caught(board, monkeypatch, tmp_path):
    """CommonMark single-quote `'title'`·괄호 `(title)` title 링크도 포착 (codex T-0036 must-fix)."""
    wiki = _wire_repo(board, monkeypatch, tmp_path)
    _doc(wiki, "a.md", "single [x](decisions/9999-ghost.md 'a title') 참고")
    _doc(wiki, "b.md", "paren [y](decisions/9998-ghost.md (a title)) 참고")
    issues = board.lint_unstable_refs()
    names = {name for name, _k, _d in issues}
    assert "9999-ghost.md" in names and "9998-ghost.md" in names, issues


def test_num_lead_wikilink_alias_deduped_to_base(board, monkeypatch, tmp_path):
    """`[[0003-slug|표시명]]` 의 alias 는 dedupe 키에서 제거 — 같은 대상이 1 issue (codex suggestion)."""
    wiki = _wire_repo(board, monkeypatch, tmp_path)
    _adr(wiki, "0003", "overlay-seam")
    _doc(wiki, "note.md",
         "alias 둘 [[0003-overlay|첫째]] 그리고 [[0003-overlay|둘째]] 참고")
    issues = board.lint_unstable_refs()
    match = [i for i in issues if i[0] == "0003-overlay"]
    assert len(match) == 1, issues          # alias 달라도 1건
    assert "|" not in match[0][0]           # 키에 alias 없음


# ── ② 슬러그 자유어휘 [[NNNN-x]] — 권고(resolve) + dangling(미resolve) ─────────

def test_num_lead_wikilink_resolving_is_advice(board, monkeypatch, tmp_path):
    wiki = _wire_repo(board, monkeypatch, tmp_path)
    _adr(wiki, "0003", "wikilink-philosophy")
    _doc(wiki, "note.md", "we follow [[0003-wikilink-philosophy]] here")
    issues = board.lint_unstable_refs()
    match = [i for i in issues if i[0] == "0003-wikilink-philosophy"]
    assert match, issues
    name, kind, detail = match[0]
    assert kind == "unstable-ref-advice"
    assert "[[ADR-0003]]" in detail


def test_num_lead_wikilink_unresolved_is_untouched(board, monkeypatch, tmp_path):
    """ADR/idea 로 resolve 안 되는 숫자선두 wikilink 는 자유어휘로 간주·불검사 (codex T-0036 must-fix).

    `[[9999-ghost]]`·`[[2026-roadmap]]` 같은 숫자선두 메모리 링크를 dangling 으로 hard-block 하면
    ADR-0003 "자유어휘 불검사·오탐 0" 계약을 깬다 → 어떤 issue 도 내지 않는다(차단은 명시적 구조 경로만).
    """
    wiki = _wire_repo(board, monkeypatch, tmp_path)
    _doc(wiki, "note.md", "메모 [[9999-ghost-decision]] 및 [[2026-roadmap]] 참고")
    issues = board.lint_unstable_refs()
    assert not any(i[0] in ("9999-ghost-decision", "2026-roadmap") for i in issues), issues


def test_num_lead_wikilink_hangul_slug_is_caught(board, monkeypatch, tmp_path):
    """한글 slug 숫자선두 wikilink `[[NNNN-한글]]` 도 포착 (codex T-0036 must-fix).

    `_slugify` 가 한글 slug 를 허용하므로 slug 부 정규식이 ASCII 전용이면 false-negative.
    `[^\\]|]+` 로 넓혀 비-ASCII slug 도 잡는다.
    """
    wiki = _wire_repo(board, monkeypatch, tmp_path)
    _adr(wiki, "0003", "overlay-seam")
    _doc(wiki, "note.md", "참조 [[0003-오버레이-심]] 형으로 적음")
    issues = board.lint_unstable_refs()
    match = [i for i in issues if i[0] == "0003-오버레이-심"]
    assert match, issues
    name, kind, detail = match[0]
    assert kind == "unstable-ref-advice"          # 0003 ADR 실재 → 권고
    assert "[[ADR-0003]]" in detail


# ── ③ 실재 슬러그 경로 링크 = 경고(차단 아님) ─────────────────────────────────

def test_md_path_link_to_existing_decision_is_advice_not_block(board, monkeypatch, tmp_path):
    wiki = _wire_repo(board, monkeypatch, tmp_path)
    _adr(wiki, "0007", "overlay-doc")
    _doc(wiki, "note.md", "background: [adr](../../decisions/0007-overlay-doc.md)")
    issues = board.lint_unstable_refs()
    match = [i for i in issues if i[0] == "0007-overlay-doc.md"]
    assert match, issues
    name, kind, detail = match[0]
    assert kind == "unstable-ref-advice"          # 작동은 함 → 권고만
    assert kind in board._ADVISORY_LINT_KINDS     # gate 가 막지 않음
    assert "[[ADR-0007]]" in detail


def test_raw_snapshot_slug_link_advice_suppressed(board, monkeypatch, tmp_path):
    """raw/ 스냅샷(ADR-0010 — sealed 면 immutable)의 실재-슬러그 *권고*는 면제한다.

    봉인된 스냅샷의 링크는 고칠 수 없고(immutable) 역사적 인용이라 ID-wikilink 권고가
    비실행적이다. 같은 링크가 비-raw 문서에 있으면 권고는 유지된다(면제는 raw source 한정).
    """
    wiki = _wire_repo(board, monkeypatch, tmp_path)
    _adr(wiki, "0008", "lean-handoff")
    link = "background: [adr](../../decisions/0008-lean-handoff.md)"
    _doc(wiki, "raw/spikes/snap.md", link)   # raw/ → 면제
    _doc(wiki, "note.md", link)              # 비-raw → 권고 유지
    issues = board.lint_unstable_refs()
    advice = [d for n, k, d in issues
              if n == "0008-lean-handoff.md" and k == "unstable-ref-advice"]
    assert advice, issues                    # 비-raw 문서에서는 여전히 권고
    assert "note.md" in advice[0]            # 비-raw source 는 사용처에 남고
    assert "raw/spikes/snap.md" not in advice[0]   # raw source 는 면제


def test_raw_snapshot_dangling_still_blocks(board, monkeypatch, tmp_path):
    """raw/ 라도 dangling(환각·차단)은 유지 — 면제는 advice 레벨만(깨진 구조 링크는 surface)."""
    wiki = _wire_repo(board, monkeypatch, tmp_path)
    # 0009 ADR 미생성 → 슬러그 경로가 실재 안 함 = dangling(차단).
    _doc(wiki, "raw/spikes/snap.md", "ref: [x](../../decisions/0009-missing.md)")
    issues = board.lint_unstable_refs()
    dang = [(n, k, d) for n, k, d in issues if n == "0009-missing.md"]
    assert dang, issues
    assert dang[0][1] == "unstable-ref"      # 차단 kind (advice 아님·raw 라도 유지)


# ── ③b lint_wikilinks 의 code span/fence 제외 (T-0043·오탐 0) ─────────────────
# dangling-wikilink 도 `lint_unstable_refs` 처럼 `_strip_code` 로 code 영역을 빼야 한다.
# 규약 문서(pm_playbook.md)가 backtick 으로 보여주는 예시 `[[ADR-NNNN]]` 이 어댑터
# fresh-clone(그 ADR 없음)에서 dangling 으로 오탐돼 bootstrap 을 abort 시키던 버그.

def test_wikilink_in_inline_code_span_not_flagged_dangling(board, monkeypatch, tmp_path):
    """inline code span 안의 dangling 후보 wikilink 는 flag 안 함 (예시 보존)."""
    wiki = _wire_repo(board, monkeypatch, tmp_path)
    # ADR 트리는 비어 있음 → backtick 밖이면 [[ADR-0006]] 은 dangling 일 것.
    _doc(wiki, "doc.md", "규약 예시: ✅ `[[ADR-0006]]` 처럼 ID-wikilink 를 쓴다.")
    issues = board.lint_wikilinks()
    assert not any(name == "ADR-0006" for name, _k, _d in issues), issues


def test_wikilink_in_fenced_block_not_flagged_dangling(board, monkeypatch, tmp_path):
    """fenced(``` ```·~~~ ~~~) 안의 dangling 후보 wikilink 도 flag 안 함."""
    wiki = _wire_repo(board, monkeypatch, tmp_path)
    _doc(wiki, "doc.md",
         "fenced 예시:\n\n```\n참조는 [[ADR-0006]] · [[T-0099]] 처럼.\n```\n\n"
         "~~~\n또 [[idea-0042]] 도.\n~~~\n")
    issues = board.lint_wikilinks()
    flagged = [n for n, _k, _d in issues if n in ("ADR-0006", "T-0099", "idea-0042")]
    assert flagged == [], f"fenced 예시 wikilink 가 dangling 으로 차단됨(오탐): {flagged}"


def test_real_dangling_wikilink_outside_code_still_flagged(board, monkeypatch, tmp_path):
    """code span *밖*의 진짜 dangling wikilink 는 여전히 flag (누락 경계).

    code 제외가 실 참조까지 놓치면(false-negative) 환각 ref 가 게이트를 우회한다.
    같은 문서에 backtick 예시(보존)와 산문 dangling(차단)을 함께 둬 둘을 분리 검증.
    """
    wiki = _wire_repo(board, monkeypatch, tmp_path)
    _doc(wiki, "doc.md",
         "예시는 `[[ADR-0006]]` 로 보여주되, 본문 산문 참조 [[ADR-0007]] 는 실재해야 한다.")
    issues = board.lint_wikilinks()
    # 산문 밖의 [[ADR-0007]] 은 dangling 으로 잡혀야 하고,
    assert any(name == "ADR-0007" and kind == "dangling-wikilink"
               for name, kind, _d in issues), issues
    # backtick 예시 [[ADR-0006]] 은 잡히면 안 된다.
    assert not any(name == "ADR-0006" for name, _k, _d in issues), issues


# ── ③c 어댑터 scaffold 스캔 (T-0118·fresh-adopter scaffold dangling 가드) ──────
# `_collect_wikilink_files` 가 wiki/·루트 docs 뿐 아니라 출하 어댑터 scaffold
# (`.claude/{agents,skills}`·`.opencode/{agents,command}`)도 봐야 한다 — fresh adopter 엔
# framework ADR 이 없으니 scaffold 의 `[[ADR-NNNN]]` 가 새면 dangling. 가드가 wiki/ 만 보던
# 동안 이 dangling 은 구조적으로 안 잡혔다(T-0116 이 scaffold ref 를 늘림). 이 테스트들이
# scaffold 스캔의 sensitivity — 확장 전이면 모두 false-negative 로 fail 한다.

def _scaffold_doc(root: Path, relpath: str, text: str) -> Path:
    """root 아래 어댑터 scaffold .md 를 만든다 (예: .claude/agents/x.md)."""
    p = root / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


@pytest.mark.parametrize("relpath", [
    ".claude/agents/orchestrator.md",
    ".claude/skills/spike-new.md",
    ".opencode/agents/orchestrator.md",
    ".opencode/command/pm-dev-delegate.md",
])
def test_scaffold_dangling_wikilink_is_flagged(board, monkeypatch, tmp_path, relpath):
    """출하 scaffold 의 dangling framework [[ADR-NNNN]] 이 lint 에 잡힌다 (scaffold 스캔).

    T-0129 이후 scaffold-only framework ADR/idea dangling 은 advisory kind
    `dangling-wikilink-scaffold` 로 분류된다(여전 보고되되 `--gate` 미차단).
    """
    _wire_repo(board, monkeypatch, tmp_path)  # ADR 트리 비어 있음 → ADR-9999 는 부재.
    _scaffold_doc(tmp_path, relpath,
                  "이 에이전트는 [[ADR-9999]] 결정을 따른다.")
    issues = board.lint_wikilinks()
    assert any(name == "ADR-9999" and kind == "dangling-wikilink-scaffold"
               for name, kind, _d in issues), (
        f"scaffold {relpath} 의 dangling [[ADR-9999]] 가 안 잡힘 — "
        f"_collect_wikilink_files 가 scaffold 를 스캔하지 않음:\n{issues}")


def test_scaffold_resolving_wikilink_is_clean(board, monkeypatch, tmp_path):
    """scaffold ref 가 실재 ADR 을 가리키면 clean (오탐 0 경계)."""
    wiki = _wire_repo(board, monkeypatch, tmp_path)
    _adr(wiki, "0018", "domain-pages")
    _scaffold_doc(tmp_path, ".claude/agents/orchestrator.md",
                  "domain 갱신은 [[ADR-0018]] 을 따른다.")
    issues = board.lint_wikilinks()
    assert not any(name == "ADR-0018" for name, _k, _d in issues), issues


def test_scaffold_absent_harness_dir_skipped(board, monkeypatch, tmp_path):
    """부재 harness scaffold dir 은 skip — 없는 디렉토리에서 터지지 않는다.

    claude 채택자엔 `.opencode` 가, opencode 채택자엔 `.claude` 가 없다. `.is_dir()`
    가드 덕에 부재 dir 은 조용히 건너뛰고 존재하는 scaffold 만 스캔한다.
    """
    _wire_repo(board, monkeypatch, tmp_path)  # 어떤 scaffold dir 도 안 만든다.
    # scaffold 부재 + wiki/ 비어 있음 → dangling 없음, 예외 없이 clean.
    issues = board.lint_wikilinks()
    assert issues == [], issues
    # 한쪽(.claude)만 두고 dangling → 잡히되 부재한 .opencode 는 무영향.
    # scaffold-only framework ADR dangling → advisory kind (T-0129).
    _scaffold_doc(tmp_path, ".claude/agents/x.md", "참조 [[ADR-9999]].")
    issues = board.lint_wikilinks()
    assert any(name == "ADR-9999" and kind == "dangling-wikilink-scaffold"
               for name, kind, _d in issues), issues


# ── ③d scaffold framework ADR/idea dangling = advisory · push 미차단 (T-0129) ──
# T-0118 이 scaffold dangling 을 blocking 으로 만들면서 framework ADR 부재 다운스트림
# 채택자의 push 를 막는 부작용이 생겼다. T-0129 이 scaffold-only framework ADR/idea
# dangling 을 `dangling-wikilink-scaffold`(advisory·`_ADVISORY_LINT_KINDS`) 로 강등한다 —
# signal(visibility) 은 유지하되 false push-block 만 제거. wiki/·root-doc·ticket dangling 은
# 여전히 `dangling-wikilink`(blocking).

def test_scaffold_kind_is_advisory(board):
    """`dangling-wikilink-scaffold` 는 `_ADVISORY_LINT_KINDS` 에 등재 (gate 가 안 막음)."""
    assert "dangling-wikilink-scaffold" in board._ADVISORY_LINT_KINDS
    # blocking kind 는 advisory 가 아니다 (대칭 회귀 — 본 dangling 은 여전 차단).
    assert "dangling-wikilink" not in board._ADVISORY_LINT_KINDS


def test_scaffold_dangling_gate_passes(board, monkeypatch, tmp_path):
    """scaffold-only framework ADR dangling 만 있으면 `lint --gate` 종료코드 0 (미차단)."""
    _wire_repo(board, monkeypatch, tmp_path)  # ADR 트리 비어 있음 → ADR-9999 부재.
    _scaffold_doc(tmp_path, ".opencode/agents/orchestrator.md",
                  "이 에이전트는 [[ADR-9999]] 와 [[idea-9999]] 를 따른다.")
    # 다른 lint 표면은 비워 scaffold dangling 만 게이트에 반영.
    monkeypatch.setattr(board, "lint_dependencies", lambda: [])
    monkeypatch.setattr(board, "lint_bodies", lambda: [])
    monkeypatch.setattr(board, "lint_ideas", lambda: [])
    monkeypatch.setattr(board, "lint_status", lambda: [])
    monkeypatch.setattr(board, "lint_unstable_refs", lambda: [])
    monkeypatch.setattr(board, "_run_lint_hooks", lambda: [])
    # 분류는 advisory.
    issues = board.lint_wikilinks()
    assert all(kind == "dangling-wikilink-scaffold"
               for name, kind, _d in issues
               if name in ("ADR-9999", "idea-9999")), issues
    # gate 는 통과(0), full 은 advisory 라도 1 (현행 계약: full 은 모든 finding 에서 1).
    assert board.cmd_lint(SimpleNamespace(gate=True)) == 0
    assert board.cmd_lint(SimpleNamespace(gate=False)) == 1


def test_wiki_dangling_still_blocks(board, monkeypatch, tmp_path):
    """wiki/ 의 framework ADR dangling 은 여전히 `dangling-wikilink`·gate 차단."""
    wiki = _wire_repo(board, monkeypatch, tmp_path)  # ADR 트리 비어 있음.
    _doc(wiki, "note.md", "본문 산문 참조 [[ADR-9999]] 는 실재해야 한다.")
    monkeypatch.setattr(board, "lint_dependencies", lambda: [])
    monkeypatch.setattr(board, "lint_bodies", lambda: [])
    monkeypatch.setattr(board, "lint_ideas", lambda: [])
    monkeypatch.setattr(board, "lint_status", lambda: [])
    monkeypatch.setattr(board, "lint_unstable_refs", lambda: [])
    monkeypatch.setattr(board, "_run_lint_hooks", lambda: [])
    issues = board.lint_wikilinks()
    assert any(name == "ADR-9999" and kind == "dangling-wikilink"
               for name, kind, _d in issues), issues
    assert board.cmd_lint(SimpleNamespace(gate=True)) == 1  # 차단 유지.


def test_scaffold_resolving_wikilink_clean_no_advisory(board, monkeypatch, tmp_path):
    """scaffold ref 가 실재 ADR 을 가리키면 clean — advisory 도 안 남는다 (오탐 0)."""
    wiki = _wire_repo(board, monkeypatch, tmp_path)
    _adr(wiki, "0018", "domain-pages")
    _scaffold_doc(tmp_path, ".claude/agents/orchestrator.md",
                  "domain 갱신은 [[ADR-0018]] 을 따른다.")
    issues = board.lint_wikilinks()
    assert not any(name == "ADR-0018" for name, _k, _d in issues), issues
    assert not any(kind == "dangling-wikilink-scaffold"
                   for _n, kind, _d in issues), issues


def test_scaffold_ticket_dangling_still_blocks(board, monkeypatch, tmp_path):
    """scaffold 안의 ticket(`[[T-...]]`) dangling 은 scaffold 여도 항상 blocking."""
    _wire_repo(board, monkeypatch, tmp_path)  # ticket 트리 비어 있음 → T-9999 부재.
    _scaffold_doc(tmp_path, ".claude/agents/orchestrator.md",
                  "이전 결정 [[T-9999]] 참조.")
    issues = board.lint_wikilinks()
    assert any(name == "T-9999" and kind == "dangling-wikilink"
               for name, kind, _d in issues), (
        f"scaffold 의 ticket dangling 은 항상 blocking 이어야 함:\n{issues}")
    assert not any(name == "T-9999" and kind == "dangling-wikilink-scaffold"
                   for name, kind, _d in issues), issues


def test_prefixed_ticket_wikilink_resolves(board, monkeypatch, tmp_path):
    """prefixed ticket(`[[T-PAY-001]]`·`[[T-service-a-001]]`·`[[T-P0-001]]`) wikilink 가
    실재 ticket 으로 resolve 돼 dangling 으로 오탐되지 않는다 (T-0164 감사·multi-repo).

    구 정규식 `T-(?:[A-Za-z]+-)?\\d+` 는 `P0`(숫자)·`service-a`(하이픈 2개) prefix 를 ticket
    으로 인식조차 못 해(`continue`·자유어휘 처리) lint 가 침묵했다. 같은 grammar(`_TICKET_ID_BODY`)
    로 prefixed ID 도 ticket 으로 보고 `ticket_ids` 멤버십을 확인해야 valid resolve 가 된다."""
    wiki = _wire_repo(board, monkeypatch, tmp_path)
    for tid in ("T-PAY-001", "T-service-a-001", "T-P0-001"):
        p = wiki / "tickets" / "open" / f"{tid}-x.md"
        p.write_text(f"---\nid: {tid}\n---\n# {tid}\n", encoding="utf-8")
    _doc(wiki, "note.md", "참조: [[T-PAY-001]] · [[T-service-a-001]] · [[T-P0-001]].")
    issues = board.lint_wikilinks()
    assert not any(name in ("T-PAY-001", "T-service-a-001", "T-P0-001")
                   for name, _k, _d in issues), (
        f"실재 prefixed ticket wikilink 는 dangling 아님:\n{issues}")


def test_prefixed_ticket_wikilink_dangling_blocks(board, monkeypatch, tmp_path):
    """부재 prefixed ticket(`[[T-PAY-999]]`) wikilink 는 dangling 으로 잡혀 차단된다.

    grammar 가 prefixed ID 를 ticket 으로 인식해야 *부재* 시에도 dangling-wikilink(blocking)
    로 surface 한다 — 인식 못 하면 자유어휘로 새 침묵(T-0164 감사 round-3 클래스 방지)."""
    wiki = _wire_repo(board, monkeypatch, tmp_path)  # ticket 트리 비어 있음.
    _doc(wiki, "note.md", "없는 ticket 참조 [[T-PAY-999]].")
    issues = board.lint_wikilinks()
    assert any(name == "T-PAY-999" and kind == "dangling-wikilink"
               for name, kind, _d in issues), (
        f"부재 prefixed ticket wikilink 는 blocking dangling 이어야 함:\n{issues}")


def test_finance_dev_d4_prefixed_ticket_resolves_and_dangling(board, monkeypatch, tmp_path):
    """finance_dev 제보 D4 회귀-lock: `[[T-finance-011]]`(실존) clean · `[[T-nope-999]]`(부재)
    는 blocking dangling (T-0240·[[ADR-0042]]).

    제보 D4 는 구버전 fork 관찰 — `[[T-<prefix>-NNN]]` 을 실존인데도 dangling 으로 오탐. 현 엔진은
    `_TICKET_ID_BODY` 공유 grammar(board.py:640) + `ticket_ids` 멤버십 단일 경로(board.py:3926~3929)
    로 이미 정합해소한다. 이 테스트가 finance_dev 가 보고한 정확한 케이스로 그 정합을 못박아
    grammar/해소 drift(T-0164 클래스) 재발 시 빨간불이 되게 한다."""
    wiki = _wire_repo(board, monkeypatch, tmp_path)
    # (a) 실존 prefixed ticket — dangling 으로 오탐되면 안 된다 (오탐 제거).
    p = wiki / "tickets" / "open" / "T-finance-011-x.md"
    p.write_text("---\nid: T-finance-011\n---\n# T-finance-011\n", encoding="utf-8")
    # 실존 링크와 부재 링크를 같은 산문에 둬 정합·검출력을 함께 검증.
    _doc(wiki, "note.md",
         "실존 [[T-finance-011]] · 부재 [[T-nope-999]] 참조.")
    issues = board.lint_wikilinks()
    # (a) 실존 prefixed → clean (결과에 그 name 이 없음).
    assert not any(name == "T-finance-011" for name, _k, _d in issues), (
        f"실존 prefixed ticket wikilink 는 dangling 아님(오탐):\n{issues}")
    # (b) 부재 prefixed → is_ticket blocking(`dangling-wikilink`·advisory 강등 아님)·검출력 유지.
    assert any(name == "T-nope-999" and kind == "dangling-wikilink"
               for name, kind, _d in issues), (
        f"부재 prefixed ticket wikilink 는 blocking dangling 이어야 함:\n{issues}")
    assert not any(name == "T-nope-999" and kind == "dangling-wikilink-scaffold"
                   for name, kind, _d in issues), issues


def test_same_ref_in_scaffold_and_wiki_blocks(board, monkeypatch, tmp_path):
    """같은 framework ADR 이 scaffold + wiki/ 양쪽에서 dangle 하면 blocking (자기문서 dangle 금지)."""
    wiki = _wire_repo(board, monkeypatch, tmp_path)  # ADR 트리 비어 있음.
    _scaffold_doc(tmp_path, ".claude/agents/orchestrator.md", "scaffold 참조 [[ADR-9999]].")
    _doc(wiki, "note.md", "wiki 산문 참조 [[ADR-9999]] 도 있다.")
    issues = board.lint_wikilinks()
    # 사용처 하나라도 wiki/root-doc 이면 advisory 강등 불가 → blocking.
    assert any(name == "ADR-9999" and kind == "dangling-wikilink"
               for name, kind, _d in issues), issues
    assert not any(name == "ADR-9999" and kind == "dangling-wikilink-scaffold"
                   for name, kind, _d in issues), issues


# ── ④ 자유어휘 일반 무탐 (오탐 0 회귀) ────────────────────────────────────────

def test_freeform_non_numeric_wikilink_untouched(board, monkeypatch, tmp_path):
    wiki = _wire_repo(board, monkeypatch, tmp_path)
    _adr(wiki, "0003", "wikilink-philosophy")
    # 숫자선두가 아닌 자유어휘·산문·canonical ID 는 unstable-ref 가 건드리지 않는다.
    _doc(wiki, "note.md",
         "free [[some-memory-slug]] and canonical [[ADR-0003]] and prose: "
         "the decisions/ dir holds ADRs.")
    issues = board.lint_unstable_refs()
    assert issues == [], issues


def test_prose_path_mention_not_a_link_untouched(board, monkeypatch, tmp_path):
    """경로가 markdown 링크 `](...)` 형이 아니면(산문 언급) 건드리지 않는다."""
    wiki = _wire_repo(board, monkeypatch, tmp_path)
    _doc(wiki, "note.md",
         "edit the file at decisions/0006-opencode-adapter-target.md by hand.")
    issues = board.lint_unstable_refs()
    assert issues == [], issues


# ── ⑤ --gate 종료코드 분기 ────────────────────────────────────────────────────

def test_gate_zero_on_status_drift_only(board, monkeypatch, tmp_path, capsys):
    """status drift(자문성)만 있으면 --gate 종료코드 0 — never blocks 계약 보존."""
    wiki = _wire_repo(board, monkeypatch, tmp_path)
    monkeypatch.setattr(board, "lint_dependencies", lambda: [])
    monkeypatch.setattr(board, "lint_bodies", lambda: [])
    monkeypatch.setattr(board, "lint_ideas", lambda: [])
    monkeypatch.setattr(board, "lint_wikilinks", lambda: [])
    monkeypatch.setattr(board, "lint_unstable_refs", lambda: [])
    monkeypatch.setattr(board, "lint_status", lambda: [
        ("status.md", "status-done-accum", "활성 매트릭스 ✅ 행 누적"),
    ])
    monkeypatch.setattr(board, "_run_lint_hooks", lambda: [])
    rc = board.cmd_lint(SimpleNamespace(gate=True))
    assert rc == 0
    # 무인자(full) 는 같은 입력에서 1 (현행 계약 유지).
    rc_full = board.cmd_lint(SimpleNamespace(gate=False))
    assert rc_full == 1


def test_gate_one_on_unstable_ref_dangling(board, monkeypatch, tmp_path):
    """dangling unstable-ref 가 있으면 --gate 종료코드 1 (차단)."""
    wiki = _wire_repo(board, monkeypatch, tmp_path)
    monkeypatch.setattr(board, "lint_dependencies", lambda: [])
    monkeypatch.setattr(board, "lint_bodies", lambda: [])
    monkeypatch.setattr(board, "lint_ideas", lambda: [])
    monkeypatch.setattr(board, "lint_wikilinks", lambda: [])
    monkeypatch.setattr(board, "lint_status", lambda: [])
    monkeypatch.setattr(board, "lint_unstable_refs", lambda: [
        ("9999-ghost.md", "unstable-ref", "실재 안 함 → [[ADR-9999]]"),
    ])
    monkeypatch.setattr(board, "_run_lint_hooks", lambda: [])
    rc = board.cmd_lint(SimpleNamespace(gate=True))
    assert rc == 1


def test_gate_zero_on_unstable_ref_advice_only(board, monkeypatch, tmp_path):
    """실재 슬러그 권고(unstable-ref-advice)만 있으면 --gate 0 — 차단은 dangling 만."""
    wiki = _wire_repo(board, monkeypatch, tmp_path)
    monkeypatch.setattr(board, "lint_dependencies", lambda: [])
    monkeypatch.setattr(board, "lint_bodies", lambda: [])
    monkeypatch.setattr(board, "lint_ideas", lambda: [])
    monkeypatch.setattr(board, "lint_wikilinks", lambda: [])
    monkeypatch.setattr(board, "lint_status", lambda: [])
    monkeypatch.setattr(board, "lint_unstable_refs", lambda: [
        ("0007-overlay.md", "unstable-ref-advice", "실재 → [[ADR-0007]] 권고"),
    ])
    monkeypatch.setattr(board, "_run_lint_hooks", lambda: [])
    assert board.cmd_lint(SimpleNamespace(gate=True)) == 0
    assert board.cmd_lint(SimpleNamespace(gate=False)) == 1


# ── ⑥ pre-push 훅 본문에 lint --gate 단계 포함 ────────────────────────────────

def test_pre_push_hook_includes_lint_gate(board, monkeypatch, tmp_path):
    hooks = tmp_path / "hooks"
    monkeypatch.setattr(board, "_hooks_dir", lambda: hooks)
    monkeypatch.setattr(board, "_detect_py", lambda: "python3")
    assert board.install_pre_push_hook() is True
    text = (hooks / "pre-push").read_text(encoding="utf-8")
    assert "board.py lint --gate" in text
    # 회귀 단계도 여전히 있어야 한다 (AND).
    assert "regression check" in text
    assert "regression run" in text


def test_pre_push_hook_idempotent(board, monkeypatch, tmp_path):
    """재설치 안전 — 두 번 설치해도 동일 본문 (board.py init 가 재설치)."""
    hooks = tmp_path / "hooks"
    monkeypatch.setattr(board, "_hooks_dir", lambda: hooks)
    monkeypatch.setattr(board, "_detect_py", lambda: "python3")
    board.install_pre_push_hook()
    first = (hooks / "pre-push").read_text(encoding="utf-8")
    board.install_pre_push_hook()
    second = (hooks / "pre-push").read_text(encoding="utf-8")
    assert first == second
    assert second.count("board.py lint --gate") == 1


# ── lint_tickets 합류 (kind 노출 회귀) ────────────────────────────────────────

def test_lint_tickets_includes_unstable_refs(board, monkeypatch, tmp_path):
    wiki = _wire_repo(board, monkeypatch, tmp_path)
    # 명시적 구조 경로 링크(실재 안 함) → dangling unstable-ref (차단 가능 카테고리).
    _doc(wiki, "note.md", "broken [x](decisions/9999-ghost.md) ref")
    # 다른 lint 표면은 비워 unstable-ref 만 본다.
    monkeypatch.setattr(board, "lint_dependencies", lambda: [])
    monkeypatch.setattr(board, "lint_bodies", lambda: [])
    monkeypatch.setattr(board, "lint_ideas", lambda: [])
    monkeypatch.setattr(board, "lint_status", lambda: [])
    monkeypatch.setattr(board, "lint_wikilinks", lambda: [])
    kinds = _kinds(board.lint_tickets())
    assert "unstable-ref" in kinds


# ── status judgment-only — scalar-anchor lint 제거 (ADR-0023 a안) ──────────────
# status.md 헤더 scalar·회귀 실측 라인이 제거돼(judgment-only) ticket_finish 가 더 이상
# status.md 를 안 건드린다 → 그 앵커 무결성 가드(`lint_status_anchors`·`scalar-anchor-broken`)도
# 제거됐다. ✅ 누적 권고(`status-done-accum`)와 architecture freshness 는 유지된다.

def test_scalar_anchor_lint_removed(board):
    """`lint_status_anchors`·`scalar-anchor-broken` kind 는 더 이상 존재하지 않는다(ADR-0023)."""
    assert not hasattr(board, "lint_status_anchors")
    assert "scalar-anchor-broken" not in board._ADVISORY_LINT_KINDS  # advisory 도 아님(애초 부재)
    assert "status-header-bloat" not in board._ADVISORY_LINT_KINDS    # header-bloat 가드도 제거
    # judgment-only 가드는 보존: ✅ 누적 권고 kind 는 advisory 로 남는다.
    assert "status-done-accum" in board._ADVISORY_LINT_KINDS


def test_lint_status_only_done_accum_now(board, monkeypatch, tmp_path):
    """lint_status 는 ✅ 누적(status-done-accum)만 낸다 — 헤더 scalar 검사 제거 후에도 동작.

    임계 초과 ✅ 행을 넣은 tmp status.md 에서 status-done-accum 이 나오고, 그 외 kind 는 없다.
    """
    status = tmp_path / "status.md"
    rows = "".join(
        f"| | mod{i} | f{i}.py | ✅ | done |\n"
        for i in range(board.STATUS_DONE_ROW_WARN + 1)
    )
    status.write_text(f"# 현재 진행 상태\n\n{rows}", encoding="utf-8")
    monkeypatch.setattr(board, "STATUS_FILE", status)
    issues = board.lint_status()
    kinds = {k for _n, k, _d in issues}
    assert kinds == {"status-done-accum"}, issues


# ── family wiki scope 태그 + 승격 (ADR-0015) ──────────────────────────────────
# `family_scope(fm)` 파싱(shared 기본·명시값)·`lint_scopes()` 인지(오탐 0·advisory)·
# `cmd_promote_scope` retag 를 검증한다. scope-aware dir = decisions/·specs/ — `_wire_repo`
# 가 SPECS_DIR 을 monkeypatch 하지 않으므로 scope 테스트는 그 위에 SPECS_DIR 을 더한다.
# (실 .project_manager/wiki/ 미접촉 — 전부 tmp_path.)

def _wire_scope_repo(board, monkeypatch, root):
    """`_wire_repo` + SPECS_DIR(tmp) wiring + areas.md 미등록(솔로) 기본.

    반환 = wiki 디렉토리. `lint_scopes`/`registered_prefixes` 가 읽는 SPECS_DIR·AREAS_FILE 을
    tmp 로 갈아끼워 hermetic 하게 한다. areas.md 는 만들지 않음(솔로 — 등록 대조 생략).
    """
    wiki = _wire_repo(board, monkeypatch, root)
    specs = wiki / "specs"
    specs.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(board, "SPECS_DIR", specs)
    monkeypatch.setattr(board, "_SCOPE_AWARE_DIRS", (board.DECISIONS_DIR, specs))
    monkeypatch.setattr(board, "AREAS_FILE", root / ".project_manager" / "areas.md")
    return wiki


def _adr_with_scope(wiki, num, slug, scope_line=""):
    """decisions/<num>-<slug>.md — 선택적 frontmatter 라인(`family_scope: …`) 포함."""
    p = wiki / "decisions" / f"{num}-{slug}.md"
    p.write_text(f"---\nid: ADR-{num}\n{scope_line}---\n# ADR {num}\n", encoding="utf-8")
    return p


# ── family_scope() 파싱 — shared 기본·명시값·부재·비문자열 ──────────────────────

def test_family_scope_defaults_to_shared_when_absent(board):
    """family_scope 키 부재 → shared 기본 (ADR-0015 "부재 시 shared 로 간주")."""
    assert board.family_scope({}) == "shared"
    assert board.family_scope({"id": "ADR-0001"}) == "shared"


def test_family_scope_returns_explicit_value(board):
    """명시 family_scope 값을 strip 해 반환."""
    assert board.family_scope({"family_scope": "payments"}) == "payments"
    assert board.family_scope({"family_scope": "  shared  "}) == "shared"


def test_family_scope_empty_string_falls_back_to_shared(board):
    """빈/공백 family_scope → shared 기본 (부재와 동일 취급)."""
    assert board.family_scope({"family_scope": ""}) == "shared"
    assert board.family_scope({"family_scope": "   "}) == "shared"


def test_family_scope_non_string_falls_back_to_shared(board):
    """비-문자열(잘못 적힌 list/숫자) → shared 안전 폴백 (파싱 예외 0)."""
    assert board.family_scope({"family_scope": ["a", "b"]}) == "shared"
    assert board.family_scope({"family_scope": 42}) == "shared"
    assert board.family_scope({"family_scope": None}) == "shared"


# ── lint_scopes() 인지 — 솔로 오탐 0·형식 권고·미등록 권고·shared 무탐 ──────────

def test_lint_scopes_no_issue_when_scope_absent(board, monkeypatch, tmp_path):
    """family_scope 부재(솔로 현 문서) → scope 이슈 0 (회귀 0)."""
    wiki = _wire_scope_repo(board, monkeypatch, tmp_path)
    _adr_with_scope(wiki, "0001", "alpha")              # scope 라인 없음
    _adr_with_scope(wiki, "0002", "beta", "scope: mission\n")  # 기존 의미 scope: — family 아님
    assert board.lint_scopes() == []


def test_lint_scopes_no_issue_on_shared(board, monkeypatch, tmp_path):
    """family_scope: shared (명시) → 정상·이슈 0 (기본값을 명시한 것)."""
    wiki = _wire_scope_repo(board, monkeypatch, tmp_path)
    _adr_with_scope(wiki, "0001", "alpha", "family_scope: shared\n")
    assert board.lint_scopes() == []


def test_lint_scopes_advice_on_broken_format(board, monkeypatch, tmp_path):
    """형식이 깨진 family_scope(공백 포함 등) → scope-advice (자문성·차단 아님)."""
    wiki = _wire_scope_repo(board, monkeypatch, tmp_path)
    _adr_with_scope(wiki, "0001", "alpha", "family_scope: 'has space'\n")
    issues = board.lint_scopes()
    assert any(kind == "scope-advice" and "형식이 깨짐" in detail
               for _name, kind, detail in issues), issues


@pytest.mark.parametrize("scope_yaml", [
    "family_scope:\n  - a\n  - b\n",      # list
    "family_scope:\n  k: v\n",            # dict
    "family_scope: 42\n",                 # int
])
def test_lint_scopes_advice_on_non_string(board, monkeypatch, tmp_path, scope_yaml):
    """비문자열 family_scope(list/dict/int) → scope-advice (형식 오류·자문성·ADR-0015).

    `family_scope()` 헬퍼는 shared 로 fail-soft 폴백하지만, lint 는 그 형식 오류를
    조용히 삼키지 않고 원본을 검사해 권고한다.
    """
    wiki = _wire_scope_repo(board, monkeypatch, tmp_path)
    adr = wiki / "decisions" / "0001-alpha.md"
    adr.write_text(f"---\nid: ADR-0001\n{scope_yaml}---\n# ADR\n", encoding="utf-8")
    issues = board.lint_scopes()
    assert any(kind == "scope-advice" and "비문자열" in detail
               for _name, kind, detail in issues), issues


def test_lint_scopes_non_string_is_advisory_not_blocking(board, monkeypatch, tmp_path):
    """비문자열 family_scope 권고는 advisory — --gate 종료코드 0 유지 (차단 0)."""
    wiki = _wire_scope_repo(board, monkeypatch, tmp_path)
    adr = wiki / "decisions" / "0001-alpha.md"
    adr.write_text("---\nid: ADR-0001\nfamily_scope:\n  - a\n---\n# ADR\n",
                   encoding="utf-8")
    monkeypatch.setattr(board, "lint_dependencies", lambda: [])
    monkeypatch.setattr(board, "lint_bodies", lambda: [])
    monkeypatch.setattr(board, "lint_ideas", lambda: [])
    monkeypatch.setattr(board, "lint_status", lambda: [])
    monkeypatch.setattr(board, "lint_wikilinks", lambda: [])
    monkeypatch.setattr(board, "lint_unstable_refs", lambda: [])
    monkeypatch.setattr(board, "_run_lint_hooks", lambda: [])
    # advice 는 떠야 하고(가드 무력화 시 비게 됨), gate 는 0 유지.
    assert "scope-advice" in _kinds(board.lint_tickets())
    assert board.cmd_lint(SimpleNamespace(gate=True)) == 0


def test_lint_scopes_advice_on_unregistered_repo_when_areas_exists(
        board, monkeypatch, tmp_path):
    """areas.md 에 prefix 등록이 있는데 미등록 repo scope → scope-advice (오타 신호)."""
    wiki = _wire_scope_repo(board, monkeypatch, tmp_path)
    # areas.md 에 PAY 만 등록 — `ghost` scope 는 미등록.
    board.AREAS_FILE.write_text(
        "| repo | prefix | git | test_cmd | owner |\n"
        "|---|---|---|---|---|\n"
        "| pay | PAY | | pytest | pay-pm |\n", encoding="utf-8")
    _adr_with_scope(wiki, "0001", "alpha", "family_scope: ghost\n")
    issues = board.lint_scopes()
    assert any(kind == "scope-advice" and "등록된 repo prefix 아님" in detail
               for _name, kind, detail in issues), issues


def test_lint_scopes_no_unregistered_advice_in_solo(board, monkeypatch, tmp_path):
    """areas.md 부재(솔로)면 repo scope 미등록 대조를 건너뜀 — 미래값일 뿐 (오탐 0)."""
    wiki = _wire_scope_repo(board, monkeypatch, tmp_path)  # areas.md 안 만듦
    _adr_with_scope(wiki, "0001", "alpha", "family_scope: payments\n")
    assert board.lint_scopes() == []


def test_lint_scopes_reads_specs_dir(board, monkeypatch, tmp_path):
    """specs/ 문서의 family_scope 도 인지한다 (decisions/ 만이 아니라)."""
    wiki = _wire_scope_repo(board, monkeypatch, tmp_path)
    spec = wiki / "specs" / "format.md"
    spec.write_text("---\nfamily_scope: 'bad scope'\n---\n# spec\n", encoding="utf-8")
    issues = board.lint_scopes()
    assert any(kind == "scope-advice" and "format.md" in name
               for name, kind, _detail in issues), issues


def test_lint_scopes_is_advisory_not_blocking_in_gate(board, monkeypatch, tmp_path):
    """scope-advice 만 있으면 --gate 종료코드 0 (ADR-0015 "차단은 최소·advisory 우선")."""
    _wire_repo(board, monkeypatch, tmp_path)
    monkeypatch.setattr(board, "lint_dependencies", lambda: [])
    monkeypatch.setattr(board, "lint_bodies", lambda: [])
    monkeypatch.setattr(board, "lint_ideas", lambda: [])
    monkeypatch.setattr(board, "lint_wikilinks", lambda: [])
    monkeypatch.setattr(board, "lint_unstable_refs", lambda: [])
    monkeypatch.setattr(board, "lint_status", lambda: [])
    monkeypatch.setattr(board, "lint_scopes", lambda: [
        ("decisions/0001-x.md", "scope-advice", "family_scope='ghost' 미등록"),
    ])
    monkeypatch.setattr(board, "_run_lint_hooks", lambda: [])
    assert board.cmd_lint(SimpleNamespace(gate=True)) == 0
    assert board.cmd_lint(SimpleNamespace(gate=False)) == 1


def test_scope_advice_in_advisory_kinds(board):
    """scope-advice 가 advisory(never-blocks) 카테고리에 등록돼 있다 — sensitivity 가드."""
    assert "scope-advice" in board._ADVISORY_LINT_KINDS


def test_lint_tickets_includes_scopes(board, monkeypatch, tmp_path):
    """lint_scopes 가 lint_tickets 합류에 포함된다 (kind 노출 회귀)."""
    wiki = _wire_scope_repo(board, monkeypatch, tmp_path)
    _adr_with_scope(wiki, "0001", "alpha", "family_scope: 'bad scope'\n")
    # 다른 lint 표면은 비워 scope-advice 만 본다.
    monkeypatch.setattr(board, "lint_dependencies", lambda: [])
    monkeypatch.setattr(board, "lint_bodies", lambda: [])
    monkeypatch.setattr(board, "lint_ideas", lambda: [])
    monkeypatch.setattr(board, "lint_status", lambda: [])
    monkeypatch.setattr(board, "lint_wikilinks", lambda: [])
    monkeypatch.setattr(board, "lint_unstable_refs", lambda: [])
    assert "scope-advice" in _kinds(board.lint_tickets())


# ── cmd_promote_scope — retag (idea-promote 동형) ─────────────────────────────

def test_promote_scope_retags_frontmatter(board, monkeypatch, tmp_path):
    """promote-scope = family_scope 값 교체 (repoA → shared retag·ADR-0015)."""
    wiki = _wire_scope_repo(board, monkeypatch, tmp_path)
    adr = _adr_with_scope(wiki, "0001", "alpha", "family_scope: payments\n")
    rc = board.cmd_promote_scope(SimpleNamespace(file=str(adr), to="shared"))
    assert rc == 0
    fm, _body = board.load_ticket(adr)
    assert fm["family_scope"] == "shared"


def test_promote_scope_adds_scope_when_absent(board, monkeypatch, tmp_path):
    """family_scope 부재(=shared 묵시) 문서에 명시 scope 를 기록한다."""
    wiki = _wire_scope_repo(board, monkeypatch, tmp_path)
    adr = _adr_with_scope(wiki, "0001", "alpha")  # scope 라인 없음
    rc = board.cmd_promote_scope(SimpleNamespace(file=str(adr), to="payments"))
    assert rc == 0
    fm, _body = board.load_ticket(adr)
    assert fm["family_scope"] == "payments"


def test_promote_scope_rejects_invalid_to(board, monkeypatch, tmp_path):
    """깨진 --to(공백 포함 등) → rc 1·파일 무변경 (형식 검증)."""
    wiki = _wire_scope_repo(board, monkeypatch, tmp_path)
    adr = _adr_with_scope(wiki, "0001", "alpha", "family_scope: payments\n")
    rc = board.cmd_promote_scope(SimpleNamespace(file=str(adr), to="bad scope"))
    assert rc == 1
    fm, _body = board.load_ticket(adr)
    assert fm["family_scope"] == "payments"  # 무변경


def test_promote_scope_missing_file(board, monkeypatch, tmp_path):
    """scope-aware dir 안인데 존재하지 않는 파일 → rc 2 (범위 가드 통과 후 존재 검사)."""
    wiki = _wire_scope_repo(board, monkeypatch, tmp_path)
    rc = board.cmd_promote_scope(
        SimpleNamespace(file=str(wiki / "decisions" / "nope.md"), to="shared"))
    assert rc == 2


def test_promote_scope_noop_when_already_target(board, monkeypatch, tmp_path):
    """이미 목표 scope 면 no-op rc 0 (멱등)."""
    wiki = _wire_scope_repo(board, monkeypatch, tmp_path)
    adr = _adr_with_scope(wiki, "0001", "alpha", "family_scope: shared\n")
    rc = board.cmd_promote_scope(SimpleNamespace(file=str(adr), to="shared"))
    assert rc == 0
    fm, _body = board.load_ticket(adr)
    assert fm["family_scope"] == "shared"


def test_promote_scope_rejects_file_outside_scope_aware_dirs(
        board, monkeypatch, tmp_path):
    """scope-aware dir(decisions/·specs/) 밖 문서 → rc 1·무변경 (ADR-0015).

    promote-scope 는 ADR/spec scope 승격 명령 — 임의 frontmatter 문서 retag 를 막는다.
    """
    _wire_scope_repo(board, monkeypatch, tmp_path)
    outside = tmp_path / "loose.md"  # decisions/·specs/ 어느 쪽도 아님
    outside.write_text("---\nid: X\nfamily_scope: payments\n---\n# loose\n",
                       encoding="utf-8")
    rc = board.cmd_promote_scope(SimpleNamespace(file=str(outside), to="shared"))
    assert rc == 1
    fm, _body = board.load_ticket(outside)
    assert fm["family_scope"] == "payments"  # 무변경


def test_promote_scope_accepts_file_in_specs_dir(board, monkeypatch, tmp_path):
    """scope-aware dir 안(specs/) 문서는 정상 retag (가드가 ADR/spec 은 허용)."""
    wiki = _wire_scope_repo(board, monkeypatch, tmp_path)
    spec = wiki / "specs" / "format.md"
    spec.write_text("---\nid: SPEC-1\nfamily_scope: payments\n---\n# spec\n",
                    encoding="utf-8")
    rc = board.cmd_promote_scope(SimpleNamespace(file=str(spec), to="shared"))
    assert rc == 0
    fm, _body = board.load_ticket(spec)
    assert fm["family_scope"] == "shared"


# ── domain lint 배선 (T-0094 · advisory·never-block·deep-import seam) ──────────
# board.lint_domain() 은 domain.py 를 deep-import(순환 회피)해 freshness finding 을 board
# lint 에 표면화한다. 테스트는 *실 domain.py* 를 로드하되 DOMAIN_DIR 을 tmp 로·git_runner 를
# 고정 대역으로 갈아끼워 hermetic 하게 stale 을 강제한다(실 .project_manager/wiki/domain 미접촉).

DOMAIN_PY = TOOLS / "domain.py"


def _load_domain():
    return _load_module("domain", DOMAIN_PY)


def _domain_page(domain_dir: Path, name: str, *, frontmatter: str, body: str) -> Path:
    """tmp domain/ 에 frontmatter md 페이지를 쓴다(test_domain._write_page 동형)."""
    domain_dir.mkdir(parents=True, exist_ok=True)
    path = domain_dir / name
    path.write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")
    return path


def _wire_domain(board, monkeypatch, domain_dir: Path, *,
                 git_runner=None, page_stale=None):
    """board._load_domain_module 이 *실 domain.py* 를 tmp DOMAIN_DIR 로 묶어 돌려주게 한다.

    git_runner 주입(per-page git 호출 hermetic 대역) 또는 page_stale 직접 대역으로
    stale 판정을 결정적으로 만든다. board.lint_domain 은 domain._real_git_runner 로 runner 를
    만들므로, 그것도 주입 runner 를 돌려주게 갈아끼운다(실 git subprocess 미사용).
    """
    domain = _load_domain()
    monkeypatch.setattr(domain, "DOMAIN_DIR", domain_dir)
    if git_runner is not None:
        monkeypatch.setattr(domain, "_real_git_runner", lambda cwd: git_runner)
    if page_stale is not None:
        monkeypatch.setattr(domain, "page_stale", page_stale)
    monkeypatch.setattr(board, "_load_domain_module", lambda: domain)
    return domain


def _fixed_git(out: str, rc: int = 0):
    """고정 (rc, out) 을 돌려주는 hermetic git_runner 대역 (test_domain 동형)."""
    return lambda argv: (rc, out)


def test_lint_domain_surfaces_stale_as_advisory(board, monkeypatch, tmp_path):
    """stale 페이지 fixture → board lint_domain 이 advisory finding 으로 표면화."""
    domain_dir = tmp_path / "domain"
    # 상호 인링크로 orphan 회피 — stale 만 본다.
    _domain_page(
        domain_dir, "stale.md",
        frontmatter="title: 낡음\ntype: concept\ncovers:\n  - src/x/**\nupdated: 2026-06-19",
        body="\n[[peer]]\n",
    )
    _domain_page(
        domain_dir, "peer.md",
        frontmatter="title: 동료\ntype: concept",
        body="\n[[stale]]\n",
    )
    # covers 커밋(2026-06-20) > updated(2026-06-19) → stale.
    _wire_domain(board, monkeypatch, domain_dir,
                 git_runner=_fixed_git("2026-06-20T00:00:00Z\n"))
    findings = board.lint_domain()
    # board 관례 순서 (label, kind, detail) — kind 는 domain 의 stale 보존.
    kinds = [kind for _label, kind, _detail in findings]
    assert "stale" in kinds
    stale = next(f for f in findings if f[1] == "stale")
    assert stale[0] == "낡음"               # label = 페이지 title
    # advisory — push 차단 kind 에 안 들어간다.
    assert "stale" in board._ADVISORY_LINT_KINDS


def test_lint_domain_surfaced_in_full_report(board, monkeypatch, tmp_path):
    """lint_tickets/cmd_lint 무인자 보고가 domain finding 을 포함(흐름 합류)."""
    wiki = _wire_repo(board, monkeypatch, tmp_path)
    domain_dir = wiki / "domain"
    _domain_page(
        domain_dir, "lonely.md",
        frontmatter="title: 고립\ntype: concept",
        body="\nno inlink\n",
    )
    # peer 페이지로 ≥2 (T-0097 single-page orphan skip 회피) — 서로 안 링크라 lonely 는 orphan.
    _domain_page(
        domain_dir, "hub.md",
        frontmatter="title: 허브\ntype: concept",
        body="\nlonely 를 안 가리킨다\n",
    )
    _wire_domain(board, monkeypatch, domain_dir,
                 page_stale=lambda page, **kw: None)
    kinds = _kinds(board.lint_tickets())
    assert "orphan" in kinds            # domain finding 이 합류 흐름에 노출


def test_gate_excludes_domain_findings(board, monkeypatch, tmp_path):
    """domain finding(stale)만 있으면 --gate 종료코드 0 — advisory/never-block."""
    _wire_repo(board, monkeypatch, tmp_path)
    # 다른 lint 표면은 비우고 domain 만 stale 을 낸다.
    monkeypatch.setattr(board, "lint_dependencies", lambda: [])
    monkeypatch.setattr(board, "lint_bodies", lambda: [])
    monkeypatch.setattr(board, "lint_ideas", lambda: [])
    monkeypatch.setattr(board, "lint_status", lambda: [])
    monkeypatch.setattr(board, "lint_wikilinks", lambda: [])
    monkeypatch.setattr(board, "lint_unstable_refs", lambda: [])
    monkeypatch.setattr(board, "lint_scopes", lambda: [])
    monkeypatch.setattr(board, "_run_lint_hooks", lambda: [])
    monkeypatch.setattr(board, "lint_domain", lambda: [
        ("낡음", "stale", "covers 코드가 updated(2026-06-19) 후 커밋됨"),
        ("고립", "orphan", "다른 domain 페이지에서 인링크 0 (고립)"),
        ("거대", "oversized", "본문 250줄 > 200"),
        ("이력쌓임", "history", "변경 이력 항목 3개 — …"),
    ])
    # --gate: domain 은 종료코드에 기여 안 함 → 0.
    assert board.cmd_lint(SimpleNamespace(gate=True)) == 0
    # 무인자(full): 같은 입력에서 1 (보고는 함·현행 계약).
    assert board.cmd_lint(SimpleNamespace(gate=False)) == 1


def test_domain_lint_kinds_are_registered_advisory(board):
    """domain 이 내는 4 kind 전부 `_ADVISORY_LINT_KINDS` 등재 — visibility>enforcement.

    신규 kind(history·T-0503)가 등재를 빠뜨리면 `--gate` 가 push 를 막는다 — domain lint 의
    never-block 계약 위반이라 이 축을 kind 목록째 고정한다.
    """
    for kind in ("stale", "orphan", "oversized", "history"):
        assert kind in board._ADVISORY_LINT_KINDS


def test_lint_domain_graceful_when_domain_absent(board, monkeypatch, tmp_path):
    """domain.py 부재(deep-import None) → domain finding 0·board lint 정상 진행."""
    monkeypatch.setattr(board, "_load_domain_module", lambda: None)
    assert board.lint_domain() == []
    # board lint 자체는 막히지 않는다(solo/domain 미사용 무영향).
    _wire_repo(board, monkeypatch, tmp_path)
    monkeypatch.setattr(board, "lint_dependencies", lambda: [])
    monkeypatch.setattr(board, "lint_bodies", lambda: [])
    monkeypatch.setattr(board, "lint_ideas", lambda: [])
    monkeypatch.setattr(board, "lint_status", lambda: [])
    monkeypatch.setattr(board, "lint_wikilinks", lambda: [])
    monkeypatch.setattr(board, "lint_unstable_refs", lambda: [])
    monkeypatch.setattr(board, "lint_scopes", lambda: [])
    # areas 레지스트리 축은 이 테스트의 주제가 아니다 — 등록 0 이관 안내(advisory)를 격리한다.
    monkeypatch.setattr(board, "lint_areas_repo_unregistered", lambda: [])
    monkeypatch.setattr(board, "_run_lint_hooks", lambda: [])
    assert board.cmd_lint(SimpleNamespace(gate=True)) == 0
    assert board.cmd_lint(SimpleNamespace(gate=False)) == 0


def test_lint_domain_graceful_when_dir_missing(board, monkeypatch, tmp_path):
    """domain/ 디렉토리 부재(load_pages → []) → finding 0 (실 domain.py 경유)."""
    domain_dir = tmp_path / "nope"   # 만들지 않음 → load_pages 가 [] 반환
    _wire_domain(board, monkeypatch, domain_dir,
                 git_runner=_fixed_git("2026-06-20T00:00:00Z\n"))
    assert board.lint_domain() == []


def test_lint_domain_absorbs_exceptions(board, monkeypatch, tmp_path):
    """domain 호출이 예외를 던져도 [] 로 흡수 — board lint 정상 진행(비차단 계약)."""
    class Boom:
        def load_pages(self):
            raise RuntimeError("boom")
    monkeypatch.setattr(board, "_load_domain_module", lambda: Boom())
    assert board.lint_domain() == []


def test_lint_domain_no_circular_import_on_load():
    """board 와 domain 모듈 로드가 순환 없이 끝난다(deep-import seam 검증)."""
    board_mod = _load_board()
    domain_mod = _load_domain()
    # domain 은 board.load_ticket 을 쓰지만, board 는 domain 을 최상단 import 하지 않는다.
    assert callable(board_mod.lint_domain)
    assert callable(domain_mod.load_pages)


# ── ADR lifecycle lint (T-0099·ADR-0021·advisory) ────────────────────────────

def _write_adr(decisions_dir, num, *, status="accepted", amends=None, amended_by=None,
               supersedes=None, superseded_by=None, title="제목"):
    """hermetic ADR md fixture (frontmatter 만 의미 있음·본문 placeholder)."""
    fm = ["title: " + title, "type: decision", "status: " + status]
    if amends is not None:
        fm.append("amends: [" + ", ".join(amends) + "]")
    if amended_by is not None:
        fm.append("amended_by: [" + ", ".join(amended_by) + "]")
    if supersedes is not None:
        fm.append("supersedes: " + supersedes)
    if superseded_by is not None:
        fm.append("superseded_by: [" + ", ".join(superseded_by) + "]")
    (decisions_dir / f"{num}-slug.md").write_text(
        "---\n" + "\n".join(fm) + "\n---\n\n# ADR-" + num + " — " + title + "\n\nbody\n",
        encoding="utf-8",
    )


@pytest.fixture
def decisions_dir(board, monkeypatch, tmp_path):
    d = tmp_path / "decisions"
    d.mkdir()
    monkeypatch.setattr(board, "DECISIONS_DIR", d)
    return d


def test_adr_lifecycle_consistent_no_findings(board, decisions_dir):
    # 0002 amends 0001 · 0001 amended_by 0002 + status amended → 정합·finding 0.
    _write_adr(decisions_dir, "0001", status="amended", amended_by=["ADR-0002"])
    _write_adr(decisions_dir, "0002", status="accepted", amends=["ADR-0001"])
    assert board.lint_adr_lifecycle() == []


def test_adr_lifecycle_missing_backref(board, decisions_dir):
    # 0002 amends 0001 인데 0001 에 amended_by 없음 → adr-lifecycle finding.
    _write_adr(decisions_dir, "0001", status="accepted")  # back-ref·status 둘 다 누락
    _write_adr(decisions_dir, "0002", status="accepted", amends=["ADR-0001"])
    findings = board.lint_adr_lifecycle()
    kinds = {k for _l, k, _d in findings}
    assert kinds == {"adr-lifecycle"}
    # back-ref 누락 + status 불일치(accepted≠amended) 둘 다 잡힌다.
    detail = " ".join(d for _l, _k, d in findings)
    assert "amended_by" in detail and "status" in detail


def test_adr_lifecycle_missing_target(board, decisions_dir):
    # 0002 amends 0099(부재) → finding.
    _write_adr(decisions_dir, "0002", status="accepted", amends=["ADR-0099"])
    findings = board.lint_adr_lifecycle()
    assert any(k == "adr-lifecycle" and "없음" in d for _l, k, d in findings)


def test_adr_lifecycle_self_consistency(board, decisions_dir):
    # status: amended 인데 amended_by 없음 → finding (자가일관).
    _write_adr(decisions_dir, "0005", status="amended")
    findings = board.lint_adr_lifecycle()
    assert any(k == "adr-lifecycle" and "amended_by 없음" in d for _l, k, d in findings)


def test_adr_lifecycle_supersede(board, decisions_dir):
    # supersede 경로: 정합이면 0 · status 누락이면 finding.
    _write_adr(decisions_dir, "0010", status="superseded", superseded_by=["ADR-0021"])
    _write_adr(decisions_dir, "0021", status="accepted", supersedes="ADR-0010")
    assert board.lint_adr_lifecycle() == []
    # 대상 status 를 accepted 로 깨면 finding.
    _write_adr(decisions_dir, "0010", status="accepted", superseded_by=["ADR-0021"])
    assert any(k == "adr-lifecycle" for _l, k, _d in board.lint_adr_lifecycle())


def test_adr_lifecycle_supersession_closes_later_amendment(board, decisions_dir):
    """superseded는 종결 상태라 후속 amends가 있어도 amended로 되돌릴 필요가 없다."""
    _write_adr(
        decisions_dir,
        "0038",
        status="superseded",
        amended_by=["ADR-0076", "ADR-0080"],
        superseded_by=["ADR-0081"],
    )
    _write_adr(decisions_dir, "0076", amends=["ADR-0038"])
    _write_adr(decisions_dir, "0080", amends=["ADR-0038"])
    _write_adr(decisions_dir, "0081", supersedes="ADR-0038")

    assert board.lint_adr_lifecycle() == []


def test_adr_lifecycle_refines_not_checked(board, decisions_dir):
    # refines(추가·대상 불변)는 검사 안 함 — 0009 refines 0006, 0006 은 accepted·back-ref 없어도 0 finding.
    _write_adr(decisions_dir, "0006", status="accepted")
    (decisions_dir / "0009-slug.md").write_text(
        "---\ntitle: t\ntype: decision\nstatus: accepted\nrefines: ADR-0006, ADR-0008\n---\n\n# ADR-0009\n\nbody\n",
        encoding="utf-8",
    )
    assert board.lint_adr_lifecycle() == []


def test_adr_lifecycle_is_advisory_never_blocks(board):
    # adr-lifecycle 은 advisory — --gate 종료코드 비기여.
    assert "adr-lifecycle" in board._ADVISORY_LINT_KINDS


def test_adr_lifecycle_graceful_no_decisions(board, monkeypatch, tmp_path):
    # decisions/ 부재 → [] (솔로/신규 clone 무영향).
    monkeypatch.setattr(board, "DECISIONS_DIR", tmp_path / "nope")
    assert board.lint_adr_lifecycle() == []


# ── ADR author provenance lint (T-0165·ADR-0033 ③·advisory) ──────────────────

def _write_adr_author(decisions_dir, num, *, author=None, title="제목"):
    """hermetic ADR md fixture — author frontmatter 만 의미 있음.

    author=None → author 키 자체 부재(구 ADR), author="" → 빈값(부재로 취급),
    그 외 → `author: <값>` 박힘.
    """
    fm = ["title: " + title, "type: decision", "status: accepted"]
    if author is not None:
        fm.append("author: " + author)
    (decisions_dir / f"{num}-slug.md").write_text(
        "---\n" + "\n".join(fm) + "\n---\n\n# ADR-" + num + " — " + title + "\n\nbody\n",
        encoding="utf-8",
    )


def test_parse_adr_author_valid(board):
    # `<user>/<pm-slot>` → (user, slot) · 마지막 `/` 분리(slot=마지막 토큰).
    assert board._parse_adr_author("alice/pm-1") == ("alice", "pm-1")
    assert board._parse_adr_author("  alice/pm-1  ") == ("alice", "pm-1")


def test_parse_adr_author_invalid_or_absent(board):
    # `/` 없음·한쪽 빈값·None/빈값 → None (형식 어긋남 또는 부재).
    assert board._parse_adr_author("alice") is None          # `/` 없음 (slot-only)
    assert board._parse_adr_author("/pm-1") is None           # user 빈값
    assert board._parse_adr_author("alice/") is None          # slot 빈값
    assert board._parse_adr_author(None) is None
    assert board._parse_adr_author("") is None
    assert board._parse_adr_author("   ") is None


def test_adr_author_present_valid_no_findings(board, decisions_dir):
    # author 가 `<user>/<pm-slot>` 형식이면 finding 0.
    _write_adr_author(decisions_dir, "0001", author="alice/pm-1")
    assert board.lint_adr_author() == []


def test_adr_author_absent_is_advised(board, decisions_dir):
    # author 부재 → adr-author 권고 finding.
    _write_adr_author(decisions_dir, "0001", author=None)
    findings = board.lint_adr_author()
    assert len(findings) == 1
    name, kind, detail = findings[0]
    assert name == "ADR-0001"
    assert kind == "adr-author"
    assert "author 권고" in detail


def test_adr_author_empty_is_advised(board, decisions_dir):
    # author 빈값도 부재로 취급 → 권고.
    _write_adr_author(decisions_dir, "0001", author='""')
    findings = board.lint_adr_author()
    assert any(k == "adr-author" and "author 권고" in d for _l, k, d in findings)


def test_adr_author_malformed_is_advised(board, decisions_dir):
    # author 있으나 `<user>/<pm-slot>` 아님 → 형식 권고 finding.
    _write_adr_author(decisions_dir, "0001", author="alice")  # `/` 없음 (slot-only)
    findings = board.lint_adr_author()
    assert len(findings) == 1
    name, kind, detail = findings[0]
    assert kind == "adr-author"
    assert "형식 권고" in detail and "alice" in detail


def test_adr_author_is_advisory_never_blocks(board):
    # adr-author 은 advisory — --gate 종료코드 비기여(_ADVISORY_LINT_KINDS 등재).
    assert "adr-author" in board._ADVISORY_LINT_KINDS


def test_adr_author_gate_does_not_block(board, monkeypatch, tmp_path):
    # author 부재 ADR 만 있어도 `lint --gate` 종료코드 0 (never-block 보증·end-to-end).
    wiki = _wire_repo(board, monkeypatch, tmp_path)
    _write_adr_author(wiki / "decisions", "0001", author=None)
    # adr-author finding 이 실제로 발생함을 먼저 확인.
    assert any(k == "adr-author" for _l, k, _d in board.lint_adr_author())
    # 그럼에도 gate 는 0 (차단 카테고리 0), full 보고는 1 (issue 존재).
    assert board.cmd_lint(SimpleNamespace(gate=True)) == 0
    assert board.cmd_lint(SimpleNamespace(gate=False)) == 1


def test_adr_author_graceful_no_decisions(board, monkeypatch, tmp_path):
    # decisions/ 부재 → [] (솔로/신규 clone·구 ADR author 부재 무영향).
    monkeypatch.setattr(board, "DECISIONS_DIR", tmp_path / "nope")
    assert board.lint_adr_author() == []


# ── 현재-진실 문서 freshness = verified_at sha 판정 (T-0363·ADR-0063·advisory) ─────
# lint_architecture_freshness·lint_status_freshness·lint_domain_freshness 는 문서 frontmatter
# `verified_at: <sha>` *이후* 매핑 경로에 커밋이 있으면 `*-stale` finding 을 낸다(date 비교
# 대체·ADR-0063). git 은 주입 runner(argv→(rc, stdout)) 로 hermetic 하게 대역한다.

def _oid_for(argv):
    """rev-parse `<sha>^{commit}` argv 에서 sha 를 뽑아 full-length OID(입력 prefix)로 만든다.

    codex R5: `_sha_anchor_status` 가 해소 OID 의 입력-prefix 여부로 고정 SHA↔hex-이름 ref 를
    가르므로, 진짜 SHA 해소를 재현하려면 OID 가 입력을 prefix 로 가져야 한다."""
    return argv[-1].split("^")[0].ljust(40, "0") + "\n"


def _stale_git(argv):
    """sha 해소(rev-parse rc0·OID=입력 prefix)·covers tracked·매핑 경로 커밋 있음(stale).

    존재/해소/stale 을 동시에 재현한다(arch/status 는 diff/log 미사용). rev-parse 는 입력-prefix
    OID(진짜 SHA·R5), merge-base 는 rc0(선조), 그 외(diff·log)는 비지 않은 출력."""
    if argv and argv[0] == "rev-parse":
        if "--show-object-format" in argv:
            return (0, "sha1\n")   # 빈 트리 OID 산출용(codex R10)
        return (0, _oid_for(argv))
    return (0, "abc1234 some commit\n")


def _clean_git(argv):
    """sha 해소(rev-parse rc0·OID=입력 prefix)·covers HEAD 트리 presence·매핑 경로 커밋 없음(clean).

    domain covers 존재 판정이 HEAD-tree 기준(codex R9·`git diff <empty> HEAD`)이라 `diff` 는
    present 로, merge-base 는 rc0(선조·default 빈 출력), log 만 빈 출력(델타 없음=clean) — arch/status
    는 diff/log 미사용(엔진 경로 트리 pathspec 은 `_git_commits_between` 만·log)."""
    if argv and argv[0] == "rev-parse":
        if "--show-object-format" in argv:
            return (0, "sha1\n")   # 빈 트리 OID 산출용(codex R10)
        return (0, _oid_for(argv))
    if argv and argv[0] == "diff":
        return (0, "tracked/file\n")
    return (0, "")


def _unknown_sha_git(argv):
    """미지/타-git sha — rev-parse rc==1(순수 미해소) → unverifiable advisory (T-0454)."""
    return (1, "")


def _env_error_git(argv):
    """환경 오류(non-repo·safe.directory·권한) — rev-parse rc==128 fatal → None(silent skip·MF1).

    순수 미해소(rc1)와 달리 환경 오류는 "타-git SHA" advisory 로 오인하면 안 된다(거짓 신호)."""
    return (128, "fatal: not a git repository")


def _descendant_sha_git(argv):
    """해소되나 HEAD 선조 아님 — rev-parse rc0(OID=입력 prefix)·`merge-base --is-ancestor` rc1 (R4-α).

    object store 공유 형상에서 descendant/딴 브랜치 sha 는 (고정 SHA 로) rev-parse 통과하나 선조
    아님 → `<sha>..HEAD` 가 비어 영구 false-clean 이던 것을 non-ancestor unverifiable 로 잡는다."""
    if argv and argv[0] == "merge-base":
        return (1, "")            # --is-ancestor 실패 = 선조 아님
    if argv and argv[0] == "rev-parse":
        return (0, _oid_for(argv))  # 고정 SHA 로 해소(입력 prefix·R5)
    return (0, "")


def _hex_named_ref_git(argv):
    """hex 로 명명된 branch/tag — rev-parse 해소(rc0)하나 OID 가 입력과 무관(ref 로 해소·codex R5).

    형식 게이트(`_is_hex_sha`)·rev-parse 를 다 통과해도 해소 OID 가 입력 prefix 가 아니라 non-sha
    (움직이는 ref) 로 잡힌다 — hex-이름 ref 를 고정 SHA 로 오인하던 false-green 을 막는다."""
    if argv and argv[0] == "rev-parse":
        return (0, "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b\n")   # 입력과 무관한 OID
    return (0, "")


def _write_verified_doc(path, *, verified_at=None, extra="type: architecture"):
    """hermetic 현재-진실 문서 fixture — verified_at 유무를 제어한다."""
    fm = ["title: Doc", extra]
    if verified_at is not None:
        fm.append(f"verified_at: {verified_at}")
    path.write_text("---\n" + "\n".join(fm) + "\n---\n\n# Doc\n", encoding="utf-8")


@pytest.fixture
def arch_file(board, monkeypatch, tmp_path):
    """ARCHITECTURE_FILE 을 tmp 로 monkeypatch 하고 그 경로를 반환한다."""
    p = tmp_path / "architecture.md"
    monkeypatch.setattr(board, "ARCHITECTURE_FILE", p)
    return p


# (a) verified_at 이후 매핑 경로 커밋 있으면 architecture-stale 표면 (sensitivity 양성 대조).
def test_architecture_freshness_flags_when_commits_after_sha(board, arch_file):
    _write_verified_doc(arch_file, verified_at="deadbeef")
    findings = board.lint_architecture_freshness(runner=_stale_git)
    assert len(findings) == 1
    label, kind, detail = findings[0]
    assert label == "architecture.md"
    assert kind == "architecture-stale"
    assert "deadbeef" in detail


# (b) sha 이후 매핑 경로 커밋 없으면 clean.
def test_architecture_freshness_clean_when_no_commits_after_sha(board, arch_file):
    _write_verified_doc(arch_file, verified_at="deadbeef")
    assert board.lint_architecture_freshness(runner=_clean_git) == []


# (c) sensitivity — verified_at 부재면 stale runner 라도 조용히 skip(false-green 아님).
def test_architecture_freshness_skip_when_verified_at_absent(board, arch_file):
    _write_verified_doc(arch_file, verified_at=None)   # verified_at 없음
    # 매핑 경로에 커밋이 *있다고* 답하는 runner 여도, verified_at 이 없으면 판정 대상 아님 → [].
    assert board.lint_architecture_freshness(runner=_stale_git) == []


def test_architecture_freshness_skip_when_doc_absent(board, arch_file):
    # 문서 파일 자체가 없음 → graceful [] (솔로/신규 clone·fail-soft).
    assert not arch_file.exists()
    assert board.lint_architecture_freshness(runner=_stale_git) == []


def test_architecture_freshness_skip_when_no_frontmatter(board, arch_file):
    arch_file.write_text("# Architecture\n\nno frontmatter\n", encoding="utf-8")
    assert board.lint_architecture_freshness(runner=_stale_git) == []


def test_architecture_freshness_flags_unverifiable_when_sha_unknown(board, arch_file):
    # 미지/타-git sha(rev-parse rc==1·순수 미해소) → 종전 silent skip 대신 unverifiable advisory
    # (T-0454). 서로 다른 git 의 SHA 를 range 양끝으로 결합하지 않고 판정 불가를 정직히 표면화.
    _write_verified_doc(arch_file, verified_at="deadbeef")
    findings = board.lint_architecture_freshness(runner=_unknown_sha_git)
    assert len(findings) == 1
    label, kind, detail = findings[0]
    assert label == "architecture.md"
    assert kind == "architecture-unverifiable"
    assert "deadbeef" in detail and "해소 안 됨" in detail


def test_architecture_freshness_env_error_is_silent_skip(board, arch_file):
    # codex MF1: rev-parse rc==128(non-repo·권한 등 fatal)=환경 오류 → None → silent skip.
    # 순수 미해소(rc1)와 달리 advisory 로 오인하지 않는다(거짓 신호 방지).
    _write_verified_doc(arch_file, verified_at="deadbeef")
    assert board.lint_architecture_freshness(runner=_env_error_git) == []


def test_architecture_freshness_moving_ref_is_unverifiable(board, arch_file):
    # codex MF2: verified_at 이 HEAD/브랜치 등 움직이는 ref → 형식 게이트로 unverifiable
    # ("고정 sha 아님"). HEAD 는 rev-parse rc0 이라도 `HEAD..HEAD` 가 항상 비어 false-green.
    _write_verified_doc(arch_file, verified_at="HEAD")
    findings = board.lint_architecture_freshness(runner=_stale_git)  # runner 무관(게이트 선차단)
    assert len(findings) == 1
    label, kind, detail = findings[0]
    assert label == "architecture.md"
    assert kind == "architecture-unverifiable"
    assert "고정 sha 아님" in detail


def test_architecture_freshness_non_ancestor_sha_is_unverifiable(board, arch_file):
    # codex R4-α: verified_at 이 해소되나 HEAD 선조 아님(descendant/딴 브랜치) → unverifiable
    # ("HEAD 선조 아님"). object store 공유 형상서 `<sha>..HEAD` 가 비어 영구 false-clean 이던 축.
    _write_verified_doc(arch_file, verified_at="beef0042")
    findings = board.lint_architecture_freshness(runner=_descendant_sha_git)
    assert len(findings) == 1
    label, kind, detail = findings[0]
    assert label == "architecture.md"
    assert kind == "architecture-unverifiable"
    assert "선조 아님" in detail


def test_architecture_freshness_hex_named_ref_is_unverifiable(board, arch_file):
    # codex R5: verified_at="deadbeef" 가 hex-이름 branch/tag 라 rev-parse 해소(rc0)하나 OID 가
    # 입력 prefix 아님 → 형식 게이트 통과했어도 non-sha unverifiable("고정 sha 아님").
    _write_verified_doc(arch_file, verified_at="deadbeef")
    findings = board.lint_architecture_freshness(runner=_hex_named_ref_git)
    assert len(findings) == 1
    label, kind, detail = findings[0]
    assert label == "architecture.md"
    assert kind == "architecture-unverifiable"
    assert "고정 sha 아님" in detail


def test_architecture_freshness_in_advisory_kinds(board):
    assert "architecture-stale" in board._ADVISORY_LINT_KINDS
    assert "architecture-unverifiable" in board._ADVISORY_LINT_KINDS   # T-0454·never-block


# ── status.md freshness (동일 규칙·status-stale) ──────────────────────────────

@pytest.fixture
def status_file(board, monkeypatch, tmp_path):
    p = tmp_path / "status.md"
    monkeypatch.setattr(board, "STATUS_FILE", p)
    return p


def test_status_freshness_flags_when_commits_after_sha(board, status_file):
    _write_verified_doc(status_file, verified_at="cafe0001", extra="type: status")
    findings = board.lint_status_freshness(runner=_stale_git)
    assert len(findings) == 1
    label, kind, detail = findings[0]
    assert label == "status.md"
    assert kind == "status-stale"
    assert "cafe0001" in detail


def test_status_freshness_clean_when_no_commits(board, status_file):
    _write_verified_doc(status_file, verified_at="cafe0001", extra="type: status")
    assert board.lint_status_freshness(runner=_clean_git) == []


def test_status_freshness_skip_when_verified_at_absent(board, status_file):
    _write_verified_doc(status_file, verified_at=None, extra="type: status")
    assert board.lint_status_freshness(runner=_stale_git) == []


def test_status_freshness_flags_unverifiable_when_sha_unknown(board, status_file):
    # status.md 도 동일 축 — 타-git sha 미해소(rc1) → status-unverifiable advisory (T-0454).
    _write_verified_doc(status_file, verified_at="beefcafe", extra="type: status")
    findings = board.lint_status_freshness(runner=_unknown_sha_git)
    assert len(findings) == 1
    label, kind, detail = findings[0]
    assert label == "status.md"
    assert kind == "status-unverifiable"
    assert "beefcafe" in detail and "해소 안 됨" in detail


def test_status_freshness_moving_ref_is_unverifiable(board, status_file):
    # codex MF2: status.md verified_at 이 브랜치명 → 형식 게이트로 status-unverifiable("고정 sha 아님").
    _write_verified_doc(status_file, verified_at="main", extra="type: status")
    findings = board.lint_status_freshness(runner=_stale_git)
    assert len(findings) == 1
    label, kind, detail = findings[0]
    assert label == "status.md"
    assert kind == "status-unverifiable"
    assert "고정 sha 아님" in detail


def test_status_freshness_env_error_is_silent_skip(board, status_file):
    # codex MF1: rc128 환경 오류 → None → silent skip(advisory 아님).
    _write_verified_doc(status_file, verified_at="beefcafe", extra="type: status")
    assert board.lint_status_freshness(runner=_env_error_git) == []


def test_status_freshness_non_ancestor_sha_is_unverifiable(board, status_file):
    # codex R4-α: 해소되나 HEAD 선조 아님 → status-unverifiable("HEAD 선조 아님").
    _write_verified_doc(status_file, verified_at="beef0042", extra="type: status")
    findings = board.lint_status_freshness(runner=_descendant_sha_git)
    assert len(findings) == 1
    label, kind, detail = findings[0]
    assert label == "status.md"
    assert kind == "status-unverifiable"
    assert "선조 아님" in detail


def test_status_freshness_hex_named_ref_is_unverifiable(board, status_file):
    # codex R5: hex-이름 ref 로 해소되는 verified_at → status-unverifiable("고정 sha 아님").
    _write_verified_doc(status_file, verified_at="deadbeef", extra="type: status")
    findings = board.lint_status_freshness(runner=_hex_named_ref_git)
    assert len(findings) == 1
    label, kind, detail = findings[0]
    assert label == "status.md"
    assert kind == "status-unverifiable"
    assert "고정 sha 아님" in detail


def test_status_freshness_in_advisory_kinds(board):
    assert "status-stale" in board._ADVISORY_LINT_KINDS
    assert "status-unverifiable" in board._ADVISORY_LINT_KINDS   # T-0454·never-block


def test_gate_zero_on_unverifiable_only(board, monkeypatch, tmp_path):
    """-unverifiable finding 만 있으면 --gate 종료코드 0 — 파생 kind 의 never-block 을
    gate 경로로 직접 잠근다(T-0454 reviewer suggestion). `*_in_advisory_kinds` 는 리터럴
    멤버십만 봐서, 파생(`kind.replace("-stale","-unverifiable")`)이 미등재 kind 를 내면
    못 잡는다 — 실 finding 을 gate 로 흘려 그 갭을 덮는다."""
    _wire_repo(board, monkeypatch, tmp_path)
    monkeypatch.setattr(board, "lint_dependencies", lambda: [])
    monkeypatch.setattr(board, "lint_bodies", lambda: [])
    monkeypatch.setattr(board, "lint_ideas", lambda: [])
    monkeypatch.setattr(board, "lint_wikilinks", lambda: [])
    monkeypatch.setattr(board, "lint_unstable_refs", lambda: [])
    monkeypatch.setattr(board, "lint_status", lambda: [
        ("architecture.md", "architecture-unverifiable", "verified_at 해소 안 됨(타 git SHA?)"),
        ("status.md", "status-unverifiable", "verified_at 해소 안 됨(타 git SHA?)"),
        ("codex-adapter.md", "domain-unverifiable", "covers 부재 + sha 미해소"),
    ])
    monkeypatch.setattr(board, "_run_lint_hooks", lambda: [])
    assert board.cmd_lint(SimpleNamespace(gate=True)) == 0
    # 파생→등재 불변식 — 현 호출부(architecture/status-stale)의 replace 파생 산출이 전부 등재.
    for stale_kind in ("architecture-stale", "status-stale"):
        derived = stale_kind.replace("-stale", "-unverifiable")
        assert derived in board._ADVISORY_LINT_KINDS, derived


# ── domain 페이지 freshness (covers→pathspec 재사용·domain-stale) ─────────────

def test_domain_freshness_flags_when_commits_after_sha(board, monkeypatch, tmp_path):
    # verified_at 보유·covers 있는 domain 페이지 + stale runner → domain-stale 1.
    domain_dir = tmp_path / "domain"
    _domain_page(
        domain_dir, "engine.md",
        frontmatter="title: 엔진\ntype: concept\ncovers:\n  - .project_manager/tools/**\n"
                    "verified_at: beef0002",
        body="\nbody\n",
    )
    _wire_domain(board, monkeypatch, domain_dir)
    findings = board.lint_domain_freshness(runner=_stale_git)
    assert len(findings) == 1
    label, kind, detail = findings[0]
    assert label == "엔진"
    assert kind == "domain-stale"
    assert "beef0002" in detail


def test_domain_freshness_clean_when_no_commits(board, monkeypatch, tmp_path):
    domain_dir = tmp_path / "domain"
    _domain_page(
        domain_dir, "engine.md",
        frontmatter="title: 엔진\ntype: concept\ncovers:\n  - .project_manager/tools/**\n"
                    "verified_at: beef0002",
        body="\nbody\n",
    )
    _wire_domain(board, monkeypatch, domain_dir)
    assert board.lint_domain_freshness(runner=_clean_git) == []


def test_domain_freshness_skip_when_verified_at_absent(board, monkeypatch, tmp_path):
    # sensitivity — covers 는 있으나 verified_at 없는 페이지는 stale runner 라도 skip.
    domain_dir = tmp_path / "domain"
    _domain_page(
        domain_dir, "engine.md",
        frontmatter="title: 엔진\ntype: concept\ncovers:\n  - .project_manager/tools/**",
        body="\nbody\n",
    )
    _wire_domain(board, monkeypatch, domain_dir)
    assert board.lint_domain_freshness(runner=_stale_git) == []


def test_domain_freshness_skip_when_no_covers(board, monkeypatch, tmp_path):
    # covers 없는(코드-무관) 페이지는 verified_at 이 있어도 매핑 경로 0 → skip.
    domain_dir = tmp_path / "domain"
    _domain_page(
        domain_dir, "concept.md",
        frontmatter="title: 개념\ntype: concept\nverified_at: beef0002",
        body="\nbody\n",
    )
    _wire_domain(board, monkeypatch, domain_dir)
    assert board.lint_domain_freshness(runner=_stale_git) == []


def test_domain_freshness_graceful_when_domain_absent(board, monkeypatch):
    monkeypatch.setattr(board, "_load_domain_module", lambda: None)
    assert board.lint_domain_freshness(runner=_stale_git) == []


def test_domain_freshness_in_advisory_kinds(board):
    assert "domain-stale" in board._ADVISORY_LINT_KINDS


# ── never-block + lint_tickets 통합 ───────────────────────────────────────────

def test_freshness_findings_are_advisory_never_block(board, monkeypatch):
    # architecture/status/domain-stale finding 만 있으면 --gate 종료코드 0 (never-block).
    monkeypatch.setattr(board, "lint_dependencies", lambda: [])
    monkeypatch.setattr(board, "lint_bodies", lambda: [])
    monkeypatch.setattr(board, "lint_ideas", lambda: [])
    monkeypatch.setattr(board, "lint_status", lambda: [])
    monkeypatch.setattr(board, "lint_wikilinks", lambda: [])
    monkeypatch.setattr(board, "lint_unstable_refs", lambda: [])
    monkeypatch.setattr(board, "lint_scopes", lambda: [])
    monkeypatch.setattr(board, "lint_domain", lambda: [])
    monkeypatch.setattr(board, "lint_adr_lifecycle", lambda: [])
    monkeypatch.setattr(board, "lint_adapter_drift", lambda: [])
    monkeypatch.setattr(board, "lint_render_leak", lambda: [])
    monkeypatch.setattr(board, "_run_lint_hooks", lambda: [])
    monkeypatch.setattr(board, "lint_architecture_freshness", lambda: [
        ("architecture.md", "architecture-stale", "sha 이후 커밋 있음")])
    monkeypatch.setattr(board, "lint_status_freshness", lambda: [
        ("status.md", "status-stale", "sha 이후 커밋 있음")])
    monkeypatch.setattr(board, "lint_domain_freshness", lambda: [
        ("엔진", "domain-stale", "sha 이후 covers 커밋 있음")])
    assert board.cmd_lint(SimpleNamespace(gate=True)) == 0    # 차단 0
    assert board.cmd_lint(SimpleNamespace(gate=False)) == 1   # finding 표면화


def test_lint_tickets_includes_freshness_lints(board, monkeypatch):
    # lint_tickets 통합 — 세 freshness finding 이 전체 보고에 포함된다.
    arch = ("architecture.md", "architecture-stale", "arch")
    status = ("status.md", "status-stale", "status")
    dom = ("엔진", "domain-stale", "domain")
    monkeypatch.setattr(board, "lint_dependencies", lambda: [])
    monkeypatch.setattr(board, "lint_bodies", lambda: [])
    monkeypatch.setattr(board, "lint_ideas", lambda: [])
    monkeypatch.setattr(board, "lint_status", lambda: [])
    monkeypatch.setattr(board, "lint_wikilinks", lambda: [])
    monkeypatch.setattr(board, "lint_unstable_refs", lambda: [])
    monkeypatch.setattr(board, "lint_scopes", lambda: [])
    monkeypatch.setattr(board, "lint_domain", lambda: [])
    monkeypatch.setattr(board, "lint_adr_lifecycle", lambda: [])
    monkeypatch.setattr(board, "lint_architecture_freshness", lambda: [arch])
    monkeypatch.setattr(board, "lint_status_freshness", lambda: [status])
    monkeypatch.setattr(board, "lint_domain_freshness", lambda: [dom])
    result = board.lint_tickets()
    assert arch in result and status in result and dom in result


# ── verified-at-backfill (1회 데이터 마이그레이션·ADR-0063 Decision 5) ─────────

def test_insert_verified_at_inserts_before_closing_fence(board):
    text = "---\ntitle: Doc\ntype: architecture\n---\n\n# Doc\n"
    out = board._insert_verified_at(text, "abc123")
    assert 'verified_at: "abc123"\n' in out   # 따옴표 친 문자열(codex R19)
    # 닫는 --- 앞·본문 보존.
    assert out.index('verified_at: "abc123"') < out.index("---\n\n# Doc")
    assert out.endswith("# Doc\n")


def test_insert_verified_at_idempotent_when_present(board):
    text = "---\ntitle: Doc\nverified_at: old\n---\n\n# Doc\n"
    assert board._insert_verified_at(text, "new") is None  # 이미 있음 → no-op


def test_insert_verified_at_none_when_no_frontmatter(board):
    assert board._insert_verified_at("# Doc\n\nno fm\n", "abc") is None


def test_backfill_verified_at_fills_targets(board, monkeypatch, tmp_path):
    arch = tmp_path / "architecture.md"
    status = tmp_path / "status.md"
    _write_verified_doc(arch, verified_at=None)
    _write_verified_doc(status, verified_at=None, extra="type: status")
    monkeypatch.setattr(board, "ARCHITECTURE_FILE", arch)
    monkeypatch.setattr(board, "STATUS_FILE", status)
    monkeypatch.setattr(board, "_load_domain_module", lambda: None)  # domain 무관 격리
    results = board.backfill_verified_at("sha9999")
    states = {p.name: state for p, state in results}
    assert states == {"architecture.md": "added", "status.md": "added"}
    assert 'verified_at: "sha9999"' in arch.read_text(encoding="utf-8")     # quoted·R19
    assert 'verified_at: "sha9999"' in status.read_text(encoding="utf-8")


def test_backfill_verified_at_dry_run_writes_nothing(board, monkeypatch, tmp_path):
    arch = tmp_path / "architecture.md"
    _write_verified_doc(arch, verified_at=None)
    monkeypatch.setattr(board, "ARCHITECTURE_FILE", arch)
    monkeypatch.setattr(board, "STATUS_FILE", tmp_path / "nope-status.md")
    monkeypatch.setattr(board, "_load_domain_module", lambda: None)
    results = board.backfill_verified_at("sha9999", dry_run=True)
    assert ("added" in [s for _p, s in results])
    assert "verified_at" not in arch.read_text(encoding="utf-8")  # dry-run 미쓰기


def test_backfill_verified_at_skips_already_present(board, monkeypatch, tmp_path):
    arch = tmp_path / "architecture.md"
    _write_verified_doc(arch, verified_at="existing")
    monkeypatch.setattr(board, "ARCHITECTURE_FILE", arch)
    monkeypatch.setattr(board, "STATUS_FILE", tmp_path / "nope-status.md")
    monkeypatch.setattr(board, "_load_domain_module", lambda: None)
    results = board.backfill_verified_at("sha9999")
    assert results == [(arch, "skip:already")]
    assert "verified_at: existing" in arch.read_text(encoding="utf-8")  # 원본 sha 보존


def test_backfill_page_selector_changes_only_selected_document_bytes(
        board, monkeypatch, tmp_path, capsys):
    """형제 backfill도 같은 선택자를 받고 선택 밖 문서는 byte-for-byte 보존한다."""
    wiki = tmp_path / ".project_manager" / "wiki"
    wiki.mkdir(parents=True)
    arch = wiki / "architecture.md"
    status = wiki / "status.md"
    _write_verified_doc(arch, verified_at=None)
    _write_verified_doc(status, verified_at=None, extra="type: status")
    before_arch = arch.read_bytes()
    before_status = status.read_bytes()
    full_oid = "cafe0001" + "0" * 32
    monkeypatch.setattr(board, "REPO", tmp_path)
    monkeypatch.setattr(board, "ARCHITECTURE_FILE", arch)
    monkeypatch.setattr(board, "STATUS_FILE", status)
    monkeypatch.setattr(board, "_canonical_commit_oid", lambda _sha: full_oid)
    monkeypatch.setattr(board, "_guard_worktree_misanchor", lambda _action: False)

    rc = board.main([
        "verified-at-backfill", "--sha", "cafe0001",
        "--page", ".project_manager/wiki/architecture.md",
    ])

    assert rc == 0
    assert arch.read_bytes() != before_arch
    assert f'verified_at: "{full_oid}"' in arch.read_text(encoding="utf-8")
    assert status.read_bytes() == before_status
    assert "1개 문서에 verified_at 삽입" in capsys.readouterr().out


def test_cmd_backfill_rejects_unverifiable_explicit_sha(board, monkeypatch, tmp_path, capsys):
    """명시 --sha 는 commit 실존 검증 후에만 기록 (codex must-fix) — 오타/비존재 sha 가 영속되면
    `_git_commits_between` fail-soft(None)에 흡수돼 그 문서 freshness 가 영구 조용 skip(false-green)."""
    arch = tmp_path / "architecture.md"
    _write_verified_doc(arch, verified_at=None)
    monkeypatch.setattr(board, "ARCHITECTURE_FILE", arch)
    monkeypatch.setattr(board, "STATUS_FILE", tmp_path / "nope-status.md")
    monkeypatch.setattr(board, "_load_domain_module", lambda: None)
    monkeypatch.setattr(board, "_canonical_commit_oid", lambda sha: None)   # 검증 실패
    rc = board.cmd_verified_at_backfill(SimpleNamespace(sha="deadbeef", dry_run=False))
    err = capsys.readouterr().err
    assert rc == 1
    assert "검증되지 않는다" in err
    assert "verified_at" not in arch.read_text(encoding="utf-8")  # abort — 미기록


def test_cmd_backfill_records_canonical_full_oid_quoted(board, monkeypatch, tmp_path, capsys):
    """검증 통과 시 **입력 축약이 아니라 canonical full OID 를 따옴표 쳐 기록**한다 (codex R16/R19).

    red-첫: 전부-숫자(octal) full OID 를 unquoted 로 쓰면 YAML 정수 파싱돼 재-lint 서 깨진다."""
    arch = tmp_path / "architecture.md"
    _write_verified_doc(arch, verified_at=None)
    monkeypatch.setattr(board, "ARCHITECTURE_FILE", arch)
    monkeypatch.setattr(board, "STATUS_FILE", tmp_path / "nope-status.md")
    monkeypatch.setattr(board, "_load_domain_module", lambda: None)
    full_oid = "01234567" * 5   # 40자 전부-숫자(leading 0=octal) canonical OID
    monkeypatch.setattr(board, "_canonical_commit_oid", lambda sha: full_oid)
    rc = board.cmd_verified_at_backfill(SimpleNamespace(sha="0123456", dry_run=False))
    capsys.readouterr()
    assert rc == 0
    text = arch.read_text(encoding="utf-8")
    assert f'verified_at: "{full_oid}"' in text          # 입력 "0123456" 아닌 full OID·quoted
    assert "0123456\n" not in text                        # 입력 축약 그대로 기록 안 함
    # 재-파싱(load_ticket=YAML)에서 정수화 없이 문자열 보존 → 재-lint 정상.
    fm, _ = board.load_ticket(arch)
    assert fm["verified_at"] == full_oid                  # int 아님·leading zero 보존


# ── render-leak 트리 성격 게이트 (local.conf 부재=소스 트리 무발화 · T-0170·ADR-0028) ──
# render-leak 은 *렌더 산출물*(operational 토큰 치환 어댑터)의 미해소 토큰을 잡는다. 토큰-form
# 소스 트리(① canonical worktree·local.conf 부재)는 산출물이 아니라 토큰이 정상이므로 검사 밖이다
# (`_render_managed_relpaths` 의 local.conf 게이트). 루트 manifest 가 `.claude/* @render` 여도 소스
# 트리에선 무발화·채택 인스턴스(local.conf 보유)에선 실 leak 을 잡는다 — 양방향 박제.

def _write_root_manifest(root: Path, body: str) -> None:
    """tmp REPO 에 .project_manager/engine.manifest 를 쓴다."""
    mf = root / ".project_manager" / "engine.manifest"
    mf.parent.mkdir(parents=True, exist_ok=True)
    mf.write_text(body, encoding="utf-8")


def _write_claude_adapter(root: Path, text: str) -> Path:
    """tmp REPO 에 .claude/agents/architect.md 어댑터를 쓴다(토큰-form 산출물 시뮬)."""
    p = root / ".claude" / "agents" / "architect.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_render_leak_skips_source_tree_no_localconf(board, monkeypatch, tmp_path):
    """① canonical 소스 트리(local.conf 부재) → 루트 manifest @render 여도 render-leak 무발화.

    sensitivity: 루트 manifest 가 `.claude/agents @render` 이고 어댑터에 미해소 토큰이 잔존해도,
    local.conf 가 없으면 토큰-form 소스 트리이므로 검사 대상 0(빈 결과). 이게 ① worktree 에서
    `lint --gate` 가 rc=0 으로 통과하는 근거(T-0170 A·이전엔 rc=1 로 깨졌던 지점).
    """
    fake_repo = tmp_path / "source_tree"
    _write_root_manifest(fake_repo, ".claude/agents    @render\n")
    _write_claude_adapter(fake_repo, "# {{PROJECT_NAME}} architect · {{PY}} · {{TEST_CMD}}\n")
    # local.conf 미생성 → 소스 트리.
    assert not (fake_repo / ".project_manager" / "local.conf").exists()

    monkeypatch.setattr(board, "REPO", fake_repo)

    assert board.lint_render_leak() == [], (
        "토큰-form 소스 트리(local.conf 부재)인데 render-leak 이 발화 — 트리 게이트 미작동."
    )
    assert board._render_managed_relpaths() == set(), (
        "소스 트리는 검사 대상 0 이어야 한다(local.conf 게이트)."
    )


def test_render_leak_fires_on_rendered_tree_with_localconf(board, monkeypatch, tmp_path):
    """채택 인스턴스(local.conf 존재·render 산출물) → 미해소 토큰 잔존 시 render-leak 발화(blocking).

    specificity: 같은 manifest·같은 토큰-form 어댑터라도 local.conf 가 있으면 렌더 산출물 트리이므로
    실 leak 을 잡는다. 트리 게이트가 *소스 트리만* 면제하고 산출물 트리의 보안 의미론은 보존함을 박제.
    """
    fake_repo = tmp_path / "rendered_tree"
    _write_root_manifest(fake_repo, ".claude/agents    @render\n")
    adapter = _write_claude_adapter(fake_repo, "# {{PROJECT_NAME}} architect\n본문\n")
    # local.conf 존재 → 채택 인스턴스(render 산출물 트리).
    local_conf = fake_repo / ".project_manager" / "local.conf"
    local_conf.write_text("project_name=acme\npy=python3\n", encoding="utf-8")

    monkeypatch.setattr(board, "REPO", fake_repo)

    issues = board.lint_render_leak()
    rel = str(adapter.relative_to(fake_repo)).replace("\\", "/")
    assert any(
        name == rel and kind == "render-leak" and "{{PROJECT_NAME}}" in detail
        for name, kind, detail in issues
    ), f"산출물 트리(local.conf 존재)에서 미해소 토큰 leak 을 잡지 못함: {issues}"


# ── 출하 템플릿 mirror 면제 (도그푸딩 worktree 보완 · T-0463) ──────────────────
# local.conf 트리 게이트는 도그푸딩 worktree(adopter#0·local.conf 보유)를 못 가른다. 그 트리의
# 토큰-form 출하 원본은 `templates/<harness>/` 사본과 byte-identical 인지로 파일 단위 판정한다
# (`_template_mirror_state`). 후보는 손-열거가 아니라 세 겹 파생 —
# `pm_update.discover_target_names()`(이름) + `resolve_target_root`(symlink 탈출 거부) +
# 그 템플릿 manifest 의 `@render` 선언 범위(성격·범위). 무관 `templates/<이름>/` 우연 일치·
# 빈 manifest 흉내·`templates/alias -> ..` 자기참조 면제를 전부 봉쇄한다.

def _write_shipping_template(root: Path, harness: str, rel_posix: str, text: str,
                             manifest_body: str | None = ".claude/agents    @render\n") -> Path:
    """tmp REPO 의 `templates/<harness>/<rel_posix>` 사본을 쓴다.

    `manifest_body` 가 None 이 아니면 `templates/<harness>/.project_manager/engine.manifest` 를
    그 내용으로 만들어 **출하 템플릿 성격 + @render 범위**를 부여한다
    (`_shipping_template_render_scopes` 의 성격·범위 게이트). None 이면 디렉토리 이름만 같은
    무관 트리(엔진 사본 없음)를 시뮬한다. `@render` 없는 manifest 본문을 주면 "manifest 는 있지만
    그 경로를 렌더-관리한다고 선언하지 않은" 트리가 된다.
    """
    harness_root = root / "templates" / harness
    if manifest_body is not None:
        manifest = harness_root / ".project_manager" / "engine.manifest"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(manifest_body, encoding="utf-8")
    p = harness_root / rel_posix
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_render_leak_skips_only_byte_identical_template_source(board, monkeypatch, tmp_path):
    """공개 루트의 token-form mirror만 면제하고, 달라진 산출물 leak은 계속 잡는다.

    local.conf는 도그푸딩 canonical root에도 있으므로 단독 성격 표지가 될 수 없다. manifest
    @render의 상대경로가 출하 템플릿 트리에 byte-identical로 존재할 때만 출하 원본으로 판정한다.
    템플릿 사본이 있어도 산출물이 한 바이트 달라지면(실 leak) 면제하면 안 된다.
    """
    fake_repo = tmp_path / "canonical_root"
    _write_root_manifest(fake_repo, ".claude/agents @render\n")
    adapter = _write_claude_adapter(fake_repo, "# {{PROJECT_NAME}} architect\n")
    (fake_repo / ".project_manager" / "local.conf").write_text(
        "project_name=project_manager\n", encoding="utf-8")
    _write_shipping_template(fake_repo, "claude_code", ".claude/agents/architect.md",
                             adapter.read_text(encoding="utf-8"))

    monkeypatch.setattr(board, "REPO", fake_repo)

    assert board.lint_render_leak() == [], "byte-identical 출하 템플릿 원본은 leak이 아니다."

    adapter.write_text("# {{PROJECT_NAME}} architect\nrender가 누락된 변경\n", encoding="utf-8")
    findings = board.lint_render_leak()
    assert any(kind == "render-leak" and name.endswith("architect.md")
               for name, kind, _ in findings), (
        "templates/* 원본이 존재해도 달라진 실제 render leak은 반드시 검출해야 한다."
    )


def test_render_leak_template_mirror_requires_shipping_tree_manifest(board, monkeypatch, tmp_path):
    """면제는 *출하 템플릿 트리*에서만 — 무관 `templates/<이름>/` 우연 일치는 leak 그대로.

    sensitivity(ⓐ): 채택자가 `templates/emails/` 같은 무관 트리에 우연히 같은 상대경로·같은 바이트
    파일을 두면(엔진 사본=engine.manifest 없음) 면제가 오발생하면 안 된다 — 디렉토리 이름 + byte
    동일성만으로 키잉하던 1차 구현의 결함(합성 채택자 실증).
    specificity(ⓑ): 같은 트리에 `@render` 선언 manifest 를 놓아 출하 템플릿 성격이 서면
    (=`--all-targets` 가 타깃으로 발견하는 그 트리) 같은 바이트가 면제된다. 두 단언의 차이는
    manifest 하나뿐.
    """
    fake_repo = tmp_path / "adopter_root"
    _write_root_manifest(fake_repo, ".claude/agents @render\n")
    adapter = _write_claude_adapter(fake_repo, "# {{PROJECT_NAME}} architect\n")
    (fake_repo / ".project_manager" / "local.conf").write_text(
        "project_name=acme\n", encoding="utf-8")
    body = adapter.read_text(encoding="utf-8")
    _write_shipping_template(fake_repo, "emails", ".claude/agents/architect.md", body,
                             manifest_body=None)

    monkeypatch.setattr(board, "REPO", fake_repo)

    assert board._shipping_template_render_scopes() == [], (
        "engine.manifest 없는 무관 templates/<이름>/ 이 출하 템플릿으로 인정됐다."
    )
    findings = board.lint_render_leak()
    assert any(name == ".claude/agents/architect.md" and kind == "render-leak"
               for name, kind, _ in findings), (
        f"무관 templates/ 트리의 byte 일치로 면제가 오발생 — 실 leak 을 놓쳤다: {findings}"
    )

    # ⓑ 같은 바이트 그대로, @render 선언 manifest 만 추가 → 출하 템플릿 트리로 인정 → 면제.
    manifest = fake_repo / "templates" / "emails" / ".project_manager" / "engine.manifest"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(".claude/agents    @render\n", encoding="utf-8")
    assert board._shipping_template_render_scopes() == [
        ((fake_repo / "templates" / "emails").resolve(), {".claude/agents"})
    ]
    assert board.lint_render_leak() == [], (
        "@render 를 선언한 정당 출하 템플릿 트리의 byte-identical 원본은 면제돼야 한다."
    )


def test_render_leak_template_mirror_requires_render_declaration(board, monkeypatch, tmp_path):
    """manifest 가 *있어도* 그 경로를 `@render` 로 선언하지 않았으면 면제 없음 (codex R2 must-fix).

    sensitivity(ⓐ): 엔진 사본 흉내(빈/무관 manifest)만으로 면제가 나면, 같은 바이트 파일 하나로
    실 leak 을 통째로 가릴 수 있다. 면제 근거는 "그 템플릿이 이 경로를 렌더-관리한다고 선언했다"
    여야 한다.
    specificity(ⓑ): 같은 트리·같은 바이트에 `@render` 선언만 추가하면 면제된다 — 두 단언의 차이는
    manifest 의 `@render` 한 줄뿐.
    """
    fake_repo = tmp_path / "unrelated_manifest_root"
    _write_root_manifest(fake_repo, ".claude/agents @render\n")
    adapter = _write_claude_adapter(fake_repo, "# {{PROJECT_NAME}} architect\n")
    (fake_repo / ".project_manager" / "local.conf").write_text(
        "project_name=acme\n", encoding="utf-8")
    # manifest 는 있으나 @render 선언 0(무관 경로만) — 성격 흉내.
    _write_shipping_template(fake_repo, "claude_code", ".claude/agents/architect.md",
                             adapter.read_text(encoding="utf-8"),
                             manifest_body="# 주석\n.project_manager/tools/board.py\n")

    monkeypatch.setattr(board, "REPO", fake_repo)

    assert board._shipping_template_render_scopes() == [], (
        "@render 선언이 없는 manifest 인데 면제 후보로 인정됐다."
    )
    findings = board.lint_render_leak()
    assert any(name == ".claude/agents/architect.md" and kind == "render-leak"
               for name, kind, _ in findings), (
        f"@render 미선언 템플릿의 byte 일치로 면제가 오발생 — 실 leak 을 놓쳤다: {findings}"
    )

    # 다른 경로만 @render 인 manifest 도 마찬가지(범위 밖).
    manifest = fake_repo / "templates" / "claude_code" / ".project_manager" / "engine.manifest"
    manifest.write_text(".opencode/agents    @render\n", encoding="utf-8")
    assert not board._is_token_form_template_mirror(adapter, ".claude/agents/architect.md"), (
        "@render 범위 밖 경로인데 면제가 났다."
    )

    # ⓑ 그 경로를 @render 로 선언 → 면제.
    manifest.write_text(".claude/agents    @render\n", encoding="utf-8")
    assert board.lint_render_leak() == [], (
        "@render 로 선언된 경로의 byte-identical 출하 원본은 면제돼야 한다."
    )


@requires_symlink
def test_render_leak_template_mirror_rejects_symlink_escape(board, monkeypatch, tmp_path):
    """`templates/<alias> -> ..` 자기참조 symlink 는 면제 후보가 아니다 (codex R2 must-fix).

    링크를 허용하면 candidate 가 루트 산출물 *자기 자신*으로 해소돼 byte-identical 이 자명
    성립한다 — 한 줄 symlink 로 render-leak 백스톱 전체가 무력화된다. 후보 트리는
    `pm_update.resolve_target_root`(resolve 후 parent 가 `templates/` 실경로) 로 걸러낸다.
    """
    fake_repo = tmp_path / "symlink_root"
    _write_root_manifest(fake_repo, ".claude/agents @render\n")
    adapter = _write_claude_adapter(fake_repo, "# {{PROJECT_NAME}} architect\n")
    (fake_repo / ".project_manager" / "local.conf").write_text(
        "project_name=acme\n", encoding="utf-8")
    templates = fake_repo / "templates"
    templates.mkdir(parents=True, exist_ok=True)
    # templates/alias -> 루트 자신(= .claude/agents/architect.md 가 그대로 비쳐 보인다).
    os.symlink(fake_repo, templates / "alias", target_is_directory=True)

    monkeypatch.setattr(board, "REPO", fake_repo)

    assert (templates / "alias" / ".claude" / "agents" / "architect.md").read_bytes() \
        == adapter.read_bytes(), "전제 확인: 링크 경유로 루트 파일이 그대로 보인다(자명 일치)."
    assert board._shipping_template_render_scopes() == [], (
        "templates/ 밖으로 탈출하는 symlink 트리가 면제 후보로 인정됐다."
    )
    findings = board.lint_render_leak()
    assert any(name == ".claude/agents/architect.md" and kind == "render-leak"
               for name, kind, _ in findings), (
        f"symlink 자기참조로 면제가 자명 성립 — 실 leak 을 놓쳤다: {findings}"
    )


@requires_symlink
def test_template_mirror_rejects_symlinked_candidate_file(board, monkeypatch, tmp_path):
    """정당 템플릿 트리 안이라도 **후보 파일**이 링크로 트리를 벗어나면 면제하지 않는다.

    트리 루트 containment 만 보면 `templates/claude_code/.claude/agents/architect.md ->
    ../../../../.claude/agents/architect.md` 같은 파일-단위 자기참조가 남는다(같은 자명 일치).
    루트 산출물 *자기 자신*으로 해소되는 링크는 비교가 무의미하므로 판정 비참여(ABSENT)다 —
    drift 로 세면 "전파하라"는 잘못된 지시를 낳는다.
    """
    fake_repo = tmp_path / "linked_file_root"
    _write_root_manifest(fake_repo, ".claude/agents @render\n")
    adapter = _write_claude_adapter(fake_repo, "# {{PROJECT_NAME}} architect\n")
    (fake_repo / ".project_manager" / "local.conf").write_text(
        "project_name=acme\n", encoding="utf-8")
    _write_shipping_template(fake_repo, "claude_code", ".claude/agents/architect.md", "placeholder\n")
    linked = (fake_repo / "templates" / "claude_code" / ".claude" / "agents" / "architect.md")
    linked.unlink()
    os.symlink(adapter, linked)

    monkeypatch.setattr(board, "REPO", fake_repo)

    assert linked.read_bytes() == adapter.read_bytes(), "전제 확인: 링크가 루트 파일을 가리킨다."
    assert board._template_mirror_state(adapter, ".claude/agents/architect.md") \
        == board._TEMPLATE_MIRROR_ABSENT, "루트 자기참조 링크는 판정 비참여여야 한다."
    assert not board._is_token_form_template_mirror(adapter, ".claude/agents/architect.md"), (
        "템플릿 트리를 벗어나는 링크 후보로 면제가 성립했다."
    )
    assert any(name == ".claude/agents/architect.md" and kind == "render-leak"
               for name, kind, _ in board.lint_render_leak())


@requires_symlink
def test_template_mirror_offtree_symlink_counts_as_drift(board, monkeypatch, tmp_path):
    """자기참조가 아닌 **트리 밖 링크**는 비참여가 아니라 drift 로 집계한다(내부 리뷰 should-fix).

    조용히 빠지면 "그 타깃엔 사본이 있다"는 착각이 남는다 — 링크 너머가 무엇이든 그 타깃의 전파
    상태는 확인된 바 없으므로 미전파와 같이 취급(면제 없음·전파 힌트 finding)한다.
    """
    fake_repo = tmp_path / "offtree_link_root"
    _write_root_manifest(fake_repo, ".claude/agents @render\n")
    adapter = _write_claude_adapter(fake_repo, "# {{PROJECT_NAME}} architect\n")
    (fake_repo / ".project_manager" / "local.conf").write_text(
        "project_name=acme\n", encoding="utf-8")
    outside = tmp_path / "outside" / "architect.md"
    outside.parent.mkdir(parents=True)
    outside.write_text("# {{PROJECT_NAME}} architect\n", encoding="utf-8")  # 내용은 같지만 트리 밖.
    _write_shipping_template(fake_repo, "claude_code", ".claude/agents/architect.md", "placeholder\n")
    linked = (fake_repo / "templates" / "claude_code" / ".claude" / "agents" / "architect.md")
    linked.unlink()
    os.symlink(outside, linked)

    monkeypatch.setattr(board, "REPO", fake_repo)

    state, drifted = board._template_mirror_report(adapter, ".claude/agents/architect.md")
    assert (state, drifted) == (board._TEMPLATE_MIRROR_DIFFERS, ["claude_code"]), (
        f"트리 밖 링크가 drift 로 집계되지 않았다: {(state, drifted)}"
    )
    detail = next(d for name, kind, d in board.lint_render_leak()
                  if name == ".claude/agents/architect.md" and kind == "render-leak")
    assert "claude_code" in detail and "pm_update --all-targets" in detail


def test_template_mirror_skips_source_remapped_render_entries(board, monkeypatch, tmp_path):
    """`@source=` remap 항목은 면제 판정 범위 밖 (내부 리뷰 should-fix).

    후보 조립(`template_root / rel_posix`)이 "사본은 dest 와 같은 상대경로"를 전제하는데 source-remap
    (ADR-0054·`ManifestEntry.source_rel`)은 그 전제를 깬다 — 전제가 안 서는 항목은 면제하지 않는다
    (오면제 대신 leak 보고·보수). 실재 예: codex `.agents/skills @render @source=.claude/skills`.
    """
    fake_repo = tmp_path / "remap_root"
    _write_root_manifest(fake_repo, ".claude/agents @render\n")
    adapter = _write_claude_adapter(fake_repo, "# {{PROJECT_NAME}} architect\n")
    (fake_repo / ".project_manager" / "local.conf").write_text(
        "project_name=acme\n", encoding="utf-8")
    _write_shipping_template(
        fake_repo, "claude_code", ".claude/agents/architect.md",
        adapter.read_text(encoding="utf-8"),
        manifest_body=".claude/agents    @render @source=templates/claude_code/.claude/agents\n")

    monkeypatch.setattr(board, "REPO", fake_repo)

    assert board._shipping_template_render_scopes() == [], (
        "@source remap 항목이 면제 범위에 편입됐다 — 경로 전제가 깨진 항목이다."
    )
    assert any(name == ".claude/agents/architect.md" and kind == "render-leak"
               for name, kind, _ in board.lint_render_leak())

    # remap 마커를 떼면(경로 전제 성립) 정상 면제 — 차이는 `@source=` 하나뿐.
    (fake_repo / "templates" / "claude_code" / ".project_manager" / "engine.manifest").write_text(
        ".claude/agents    @render\n", encoding="utf-8")
    assert board.lint_render_leak() == []


def test_template_mirror_multi_target_partial_drift_is_not_exempt(board, monkeypatch, tmp_path):
    """여러 타깃이 같은 경로를 @render 로 선언하면 **전수 집계** — 하나만 drift 해도 면제 없음.

    실재 형상: `.claude/skills` 는 claude_code 와 opencode 양쪽 manifest 의 @render 범위다. first-match
    조기 반환이면 한 타깃만 갱신해도 면제가 나 나머지의 `--all-targets` 미전파가 숨는다(codex R3
    must-fix). sensitivity: 한쪽 identical + 한쪽 differs → DIFFERS 우선(전파 힌트 finding·blocking).
    specificity: 양쪽 identical → 면제.
    """
    fake_repo = tmp_path / "multi_target_root"
    _write_root_manifest(fake_repo, ".claude/skills @render\n")
    (fake_repo / ".project_manager" / "local.conf").write_text(
        "project_name=acme\n", encoding="utf-8")
    skill = fake_repo / ".claude" / "skills" / "pm-adr" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# {{PROJECT_NAME}} adr\n루트 갱신\n", encoding="utf-8")
    rel = ".claude/skills/pm-adr/SKILL.md"
    # claude_code 만 전파됨(identical), opencode 는 구버전 잔존(differs).
    _write_shipping_template(fake_repo, "claude_code", rel, skill.read_text(encoding="utf-8"),
                             manifest_body=".claude/skills    @render\n")
    stale = _write_shipping_template(fake_repo, "opencode", rel, "# {{PROJECT_NAME}} adr\n",
                                     manifest_body=".claude/skills    @render\n")

    monkeypatch.setattr(board, "REPO", fake_repo)

    assert len(board._shipping_template_render_scopes()) == 2, "전제: 두 타깃 모두 후보여야 한다."
    assert board._template_mirror_report(skill, rel) == (board._TEMPLATE_MIRROR_DIFFERS,
                                                         ["opencode"]), (
        "한 타깃이 identical 이라고 조기 반환 — 다른 타깃의 미전파 drift 가 숨었다."
    )
    detail = next(d for name, kind, d in board.lint_render_leak()
                  if name == rel and kind == "render-leak")
    assert "pm_update --all-targets" in detail, (
        f"부분 전파 상태인데 전파 누락 힌트가 없다: {detail}"
    )
    # 어긋난 타깃만 지목한다 — 전파된 쪽(claude_code)은 문구에 안 나온다(오지목 방지).
    assert "opencode" in detail and "claude_code" not in detail, (
        f"drift 타깃 지목이 부정확하다: {detail}"
    )

    # 남은 타깃까지 전파 → 전 후보 identical → 면제.
    stale.write_text(skill.read_text(encoding="utf-8"), encoding="utf-8")
    assert board._template_mirror_state(skill, rel) == board._TEMPLATE_MIRROR_IDENTICAL
    assert board.lint_render_leak() == [], (
        "전 타깃이 byte-identical 인데 면제되지 않았다."
    )


def test_template_mirror_missing_copy_in_scope_is_drift(board, monkeypatch, tmp_path):
    """선언 범위 안인데 타깃 **사본이 없으면** 면제 아님 — 신규 파일 미전파 (codex R4 must-fix).

    한 타깃만 전파된 신규 어댑터: claude_code 는 identical, opencode 는 같은 경로를 @render 로
    선언했는데 파일이 없다. "없으니 비참여"로 넘기면 남은 후보가 전부 identical 이라 면제가 나고
    `--all-targets` 누락이 숨는다. 부재는 drift 로 집계해야 한다(전파 힌트 finding·blocking).
    전파 후엔 면제로 돌아온다(specificity).
    """
    fake_repo = tmp_path / "missing_copy_root"
    _write_root_manifest(fake_repo, ".claude/skills @render\n")
    (fake_repo / ".project_manager" / "local.conf").write_text(
        "project_name=acme\n", encoding="utf-8")
    skill = fake_repo / ".claude" / "skills" / "pm-new" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# {{PROJECT_NAME}} 신규 skill\n", encoding="utf-8")
    rel = ".claude/skills/pm-new/SKILL.md"
    body = skill.read_text(encoding="utf-8")
    _write_shipping_template(fake_repo, "claude_code", rel, body,
                             manifest_body=".claude/skills    @render\n")
    # opencode 는 `.claude/skills` 를 @render 로 선언하되 신규 파일 사본이 없다(다른 파일만 보유).
    _write_shipping_template(fake_repo, "opencode", ".claude/skills/pm-old/SKILL.md", body,
                             manifest_body=".claude/skills    @render\n")
    assert not (fake_repo / "templates" / "opencode" / rel).exists(), "전제: 한 타깃엔 사본이 없다."

    monkeypatch.setattr(board, "REPO", fake_repo)

    assert board._template_mirror_report(skill, rel) == (board._TEMPLATE_MIRROR_DIFFERS,
                                                         ["opencode"]), (
        "선언 범위 안 사본 부재가 비참여 처리돼 면제가 났다 — 신규 파일 미전파가 숨는다."
    )
    detail = next(d for name, kind, d in board.lint_render_leak()
                  if name == rel and kind == "render-leak")
    assert "pm_update --all-targets" in detail, f"미전파 힌트가 없다: {detail}"
    assert "opencode" in detail, f"사본이 없는 타깃을 지목하지 않았다: {detail}"

    # 남은 타깃까지 전파 → 전 후보 identical → 면제.
    dest = fake_repo / "templates" / "opencode" / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(body, encoding="utf-8")
    assert board._template_mirror_state(skill, rel) == board._TEMPLATE_MIRROR_IDENTICAL
    assert board.lint_render_leak() == [], "전 타깃 전파 후에도 면제되지 않았다."


def test_template_mirror_no_exemption_without_pm_update_seam(board, monkeypatch, tmp_path):
    """pm_update 로드 실패 → 면제 없음(보수 방향·면제는 특권이지 기본값이 아니다).

    판정원(`discover_target_names`·`resolve_target_root`·`read_manifest`)이 없으면 후보 0 →
    byte-identical 사본이 있어도 mirror 로 인정하지 않는다. lint 경유가 아니라 술어를 직접
    단언한다 — 로드 실패는 `lint_render_leak` 을 조기 반환(검사 대상 0)시켜 lint 단언이 자명
    통과가 되기 때문(non-vacuous 유지).
    """
    fake_repo = tmp_path / "seam_root"
    _write_root_manifest(fake_repo, ".claude/agents @render\n")
    adapter = _write_claude_adapter(fake_repo, "# {{PROJECT_NAME}} architect\n")
    _write_shipping_template(fake_repo, "claude_code", ".claude/agents/architect.md",
                             adapter.read_text(encoding="utf-8"))
    monkeypatch.setattr(board, "REPO", fake_repo)

    assert board._is_token_form_template_mirror(adapter, ".claude/agents/architect.md"), (
        "정상 seam 에서는 byte-identical 출하 원본이 mirror 로 인정돼야 한다(대조군)."
    )

    monkeypatch.setattr(board, "_load_pm_update_module", lambda: None)
    assert board._shipping_template_render_scopes() == []
    assert not board._is_token_form_template_mirror(adapter, ".claude/agents/architect.md"), (
        "pm_update 로드 실패인데 면제가 났다 — 판정원 부재 시엔 면제하지 않아야 한다."
    )


def test_render_leak_differing_mirror_message_hints_propagation(board, monkeypatch, tmp_path):
    """전파-대상(적용 후보 있음)인데 drift 면 finding 에 전파 누락 힌트를 붙인다.

    현행 문구("overlay/local.conf 채널 누락")만으론 `pm_update --all-targets` 미전파 케이스를
    채널 오진단으로 몰고 간다. 반대로 **적용 후보 자체가 없는** 일반 채택 인스턴스(출하 템플릿
    트리 없음)에선 그 힌트가 없어야 한다 — 거긴 진짜로 overlay/local.conf 채널 문제다.
    """
    fake_repo = tmp_path / "drifted_root"
    _write_root_manifest(fake_repo, ".claude/agents @render\n")
    adapter = _write_claude_adapter(fake_repo, "# {{PROJECT_NAME}} architect\n루트만 수정\n")
    (fake_repo / ".project_manager" / "local.conf").write_text(
        "project_name=acme\n", encoding="utf-8")
    _write_shipping_template(fake_repo, "claude_code", ".claude/agents/architect.md",
                             "# {{PROJECT_NAME}} architect\n")
    monkeypatch.setattr(board, "REPO", fake_repo)

    detail = next(d for name, kind, d in board.lint_render_leak()
                  if name == ".claude/agents/architect.md" and kind == "render-leak")
    assert "pm_update --all-targets" in detail, (
        f"템플릿 사본과 내용 불일치인데 전파 누락 힌트가 없다: {detail}"
    )

    # 출하 템플릿 트리 자체가 없는 일반 채택 인스턴스 → 적용 후보 0(ABSENT): 채널 문구만.
    # (사본만 지우는 건 이제 "선언 범위 안 부재"=미전파 drift 라 힌트가 정상이다 — codex R4.)
    shutil.rmtree(fake_repo / "templates")
    plain = next(d for name, kind, d in board.lint_render_leak()
                 if name == ".claude/agents/architect.md" and kind == "render-leak")
    assert "pm_update --all-targets" not in plain, (
        f"출하 템플릿 트리가 없는데 전파 누락 힌트가 붙었다(오진단 유도): {plain}"
    )


# T-0463 재발 가드 대상 — 공개 루트의 token-form 어댑터 12파일(agents 4 + skills 8).
# 이 열거는 `tests/test_claude_adapter_parity.py::IDENTICAL_RELPATHS`(root↔templates byte-identical
# 8파일)와 같은 불변식의 *부분 중복*이다: 저쪽은 전파 드리프트 0, 이쪽은 그 동일성이 render-leak
# 면제의 근거라는 축. 한쪽을 고치면 다른 쪽도 같이 본다.
_T0463_TOKEN_FORM_MIRRORS = (
    ".claude/agents/architect.md",
    ".claude/agents/code-reviewer.md",
    ".claude/agents/developer.md",
    ".claude/agents/researcher.md",
    ".claude/skills/pm-adr/SKILL.md",
    ".claude/skills/pm-bootstrap/SKILL.md",
    ".claude/skills/pm-dev-delegate/SKILL.md",
    ".claude/skills/pm-handoff/SKILL.md",
    ".claude/skills/pm-ticket/SKILL.md",
    ".claude/skills/pm-wave-claim/SKILL.md",
    ".claude/skills/pm-wave-finish/SKILL.md",
    ".claude/skills/pm-worktree/SKILL.md",
)


def test_render_leak_public_claude_token_form_mirrors_are_clean(board, monkeypatch, tmp_path):
    """T-0463 재발 가드 — 공개 루트의 12개 token-form 어댑터는 render-leak 0 (hermetic).

    lint 단언을 실 REPO 에서 돌리면 local.conf 가 없는 CI/fresh clone 에서 `lint_render_leak()`
    이 조기 반환(검사 대상 0)해 **자명 통과**한다(변이 주입해도 green). 그래서 실 파일 바이트를
    tmp 사본 트리(local.conf + 루트 manifest + 출하 템플릿 사본)로 옮겨 hermetic 하게 돌린다 —
    어느 환경에서도 lint 가 실제로 이 12파일을 스캔한다. 실 트리에 대해서는 (a) 12파일이 실재하고
    (b) templates 사본과 byte-identical 이며 (c) 토큰을 실제로 담고 있음을 별도로 단언한다.
    """
    real_repo = board.REPO
    fake_repo = tmp_path / "adopter0_worktree"
    _write_root_manifest(fake_repo, ".claude/agents    @render\n.claude/skills    @render\n")
    (fake_repo / ".project_manager" / "local.conf").write_text(
        "project_name=project_manager\npy=python3\n", encoding="utf-8")
    template_manifest = (fake_repo / "templates" / "claude_code"
                         / ".project_manager" / "engine.manifest")
    template_manifest.parent.mkdir(parents=True, exist_ok=True)
    template_manifest.write_text(".claude/agents    @render\n.claude/skills    @render\n",
                                 encoding="utf-8")

    token_bearing = 0
    for rel in _T0463_TOKEN_FORM_MIRRORS:
        root_file = real_repo / rel
        template_file = real_repo / "templates" / "claude_code" / rel
        assert root_file.is_file(), f"공개 루트 어댑터 없음: {rel}"
        assert template_file.is_file(), f"출하 템플릿 사본 없음: templates/claude_code/{rel}"
        payload = root_file.read_bytes()
        assert payload == template_file.read_bytes(), (
            f"{rel} 이 root↔templates byte-identical 아님 — 전파 드리프트 "
            "(면제 근거가 무너진다·pm_update --all-targets 필요)"
        )
        if board._RENDER_TOKEN_RE.search(root_file.read_text(encoding="utf-8")):
            token_bearing += 1
        for dest_root in (fake_repo, fake_repo / "templates" / "claude_code"):
            dest = dest_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(payload)
    assert token_bearing, (
        "12파일 중 미해소 토큰을 가진 게 하나도 없다 — 이 가드가 자명 통과 상태다."
    )

    monkeypatch.setattr(board, "REPO", fake_repo)
    assert board._render_managed_relpaths(), "tmp 트리에서 검사 대상이 0 — 가드가 자명 통과한다."
    assert [issue for issue in board.lint_render_leak()
            if issue[0] in _T0463_TOKEN_FORM_MIRRORS] == [], (
        "T-0463 의 token-form 출하 원본 12파일에서 render-leak 이 재발했다."
    )

    # 변이 실증: 루트 사본만 바꾸면(=전파 안 된 실 leak) 같은 lint 가 잡는다 → 위 green 은 자명하지 않다.
    mutated = fake_repo / _T0463_TOKEN_FORM_MIRRORS[0]
    mutated.write_text(mutated.read_text(encoding="utf-8") + "\n{{PROJECT_NAME}} 미전파 수정\n",
                       encoding="utf-8")
    assert any(name == _T0463_TOKEN_FORM_MIRRORS[0] and kind == "render-leak"
               for name, kind, _ in board.lint_render_leak()), (
        "루트 사본이 템플릿과 달라졌는데 leak 을 못 잡았다 — 면제가 과잉이다."
    )


# ── render-leak 이 @render 하위 *모든 텍스트 파일*을 스캔 (T-0427·확장자 열거 세 번째 지점 마감) ──
# 옛 구현은 `rglob("*.md")` 로만 순회해 .toml·.json·.yaml·확장자 없는 텍스트의 미해소 토큰을
# 놓쳤다(codex `.codex/agents/*.toml` 이 정확히 이 갭을 통과) — blocking 백스톱이 조용히 반쪽.
# 텍스트 판정은 pm_update._is_text_source 를 **공유**하고(세 번째 사본 신설 0), 바이너리는 크래시
# 없이 건너뛴다(`except (UnicodeDecodeError, OSError)`). fixture 는 성공 입력만 고르지 않도록
# 실 바이너리·비-ASCII 파일명/내용·빈 디렉토리·부재 경로를 직접 심는다.

def _write_managed_file(root: Path, relpath: str, *, text: str | None = None,
                        data: bytes | None = None) -> Path:
    """@render path 하위에 임의 확장자/바이너리 파일을 쓴다 (T-0427 fixture)."""
    p = root / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    if data is not None:
        p.write_bytes(data)
    else:
        p.write_text(text or "", encoding="utf-8")
    return p


def _rendered_tree(root: Path) -> None:
    """`.claude/agents @render` 루트 manifest + local.conf(채택 인스턴스=산출물 트리)."""
    _write_root_manifest(root, ".claude/agents @render\n")
    (root / ".project_manager" / "local.conf").write_text(
        "project_name=acme\n", encoding="utf-8")


def test_render_leak_flags_token_in_non_md_toml(board, monkeypatch, tmp_path):
    """sensitivity(핵심 DoD): @render 경로의 `.toml` 에 미해소 토큰 → render-leak 발화(blocking).

    이 티켓의 목적 자체 — 옛 `rglob("*.md")` 는 `.toml` 을 스캔하지 않아 leak 을 통과시켰다.
    """
    fake_repo = tmp_path / "repo"
    _rendered_tree(fake_repo)
    leak = _write_managed_file(
        fake_repo, ".claude/agents/config.toml",
        text='name = "{{PROJECT_NAME}}"\n')
    monkeypatch.setattr(board, "REPO", fake_repo)

    findings = board.lint_render_leak()
    rel = str(leak.relative_to(fake_repo)).replace("\\", "/")
    assert any(
        name == rel and kind == "render-leak" and "{{PROJECT_NAME}}" in detail
        for name, kind, detail in findings
    ), f".toml 산출물의 미해소 토큰을 잡지 못함(rglob 미확장?): {findings}"


def test_render_leak_flags_tokens_across_non_md_extensions(board, monkeypatch, tmp_path):
    """.json·.yaml·확장자 없는 텍스트 각각의 미해소 토큰을 모두 잡는다 (확장자 무관 텍스트 판정)."""
    fake_repo = tmp_path / "repo"
    _rendered_tree(fake_repo)
    _write_managed_file(fake_repo, ".claude/agents/data.json",
                        text='{"name": "{{PROJECT_NAME}}"}\n')
    _write_managed_file(fake_repo, ".claude/agents/conf.yaml",
                        text="root: {{PROTECTED_PATHS}}\n")
    _write_managed_file(fake_repo, ".claude/agents/nested/deep.toml",
                        text="x = '{{TEST_CMD}}'\n")  # rglob 재귀
    _write_managed_file(fake_repo, ".claude/agents/Makefile",
                        text="run:\n\t{{PY}} run\n")  # 확장자 없는 텍스트
    monkeypatch.setattr(board, "REPO", fake_repo)

    flagged = {name for name, kind, _ in board.lint_render_leak() if kind == "render-leak"}
    for expected in (".claude/agents/data.json", ".claude/agents/conf.yaml",
                     ".claude/agents/nested/deep.toml", ".claude/agents/Makefile"):
        assert expected in flagged, (
            f"{expected} 의 미해소 토큰을 못 잡음 — 확장자 열거로 회귀? flagged={flagged}")


def test_render_leak_skips_real_binary_no_crash(board, monkeypatch, tmp_path):
    """실 바이너리(무효 UTF-8)가 @render 하위에 섞여도 크래시 0·오탐 0, 인접 텍스트 leak 은 잡는다.

    `\\x89PNG…` 는 유효 UTF-8 이 아니라 _is_text_source(공유 판정)가 False → 스캔 제외. 파일 안에
    `{{PROJECT_NAME}}` 바이트가 있어도 finding 0 이어야 하고, 스캔 루프는 바이너리 다음 텍스트
    파일까지 계속 돌아 leak 을 잡아야 한다(OSError 만 잡던 옛 코드는 UnicodeDecodeError 로 죽었다).
    """
    fake_repo = tmp_path / "repo"
    _rendered_tree(fake_repo)
    binary = _write_managed_file(
        fake_repo, ".claude/agents/logo.png",
        data=b"\x89PNG\r\n\x1a\n{{PROJECT_NAME}}\xff\xd8\xff\xe0binary")
    _write_managed_file(fake_repo, ".claude/agents/real.md",
                        text="# {{PROJECT_NAME}}\n")
    monkeypatch.setattr(board, "REPO", fake_repo)

    findings = board.lint_render_leak()  # 크래시하면 여기서 예외
    names = {name for name, kind, _ in findings if kind == "render-leak"}
    bin_rel = str(binary.relative_to(fake_repo)).replace("\\", "/")
    assert bin_rel not in names, f"바이너리를 텍스트로 오판해 스캔함: {findings}"
    assert ".claude/agents/real.md" in names, (
        f"바이너리 다음 텍스트 파일의 leak 을 놓침(루프 중단?): {findings}")


def test_render_leak_binary_survives_when_text_judgment_lies(board, monkeypatch, tmp_path):
    """넓힌 except 안전판: _is_text_source 가 바이너리를 True 로 오판해도 read 에서 크래시 0.

    TOCTOU/폴백 방어(`except (UnicodeDecodeError, OSError)`) 를 직접 태운다 — 텍스트 판정을
    통과한 뒤 read_text 가 UnicodeDecodeError 를 던져도(rglob("*") 로 바이너리가 스캔 대상에 든다)
    OSError 만 잡던 옛 코드처럼 죽지 않는다. _is_text_source=True 강제가 유일한 도달 경로.
    """
    fake_repo = tmp_path / "repo"
    _rendered_tree(fake_repo)
    _write_managed_file(fake_repo, ".claude/agents/logo.png",
                        data=b"\xff\xfe\x00\x01{{PROJECT_NAME}}\x80\x81")
    monkeypatch.setattr(board, "REPO", fake_repo)

    real = board._load_pm_update_module()
    lying = SimpleNamespace(read_manifest=real.read_manifest,
                            _is_text_source=lambda p: True)  # 바이너리도 '텍스트'라 우김
    monkeypatch.setattr(board, "_load_pm_update_module", lambda: lying)

    board.lint_render_leak()  # 예외 없이 반환해야 함(넓힌 except 가 UnicodeDecodeError 흡수)


def test_render_leak_delegates_text_judgment_to_pm_update(board, monkeypatch, tmp_path):
    """판정 공유(DoD): 텍스트 판정을 pm_update._is_text_source 에 **위임**함을 박제(세 번째 사본 0).

    render 채널이 쓰는 그 함수를 호출한다 — 스텁이 False 를 주면(그 파일 텍스트 아님) 스캔에서
    빠져야 한다. board 가 확장자 재열거 등 자체 인라인 판정으로 회귀하면 스텁이 무시돼 red.
    """
    fake_repo = tmp_path / "repo"
    _rendered_tree(fake_repo)
    _write_managed_file(fake_repo, ".claude/agents/config.toml",
                        text='name = "{{PROJECT_NAME}}"\n')
    monkeypatch.setattr(board, "REPO", fake_repo)

    calls: list[str] = []

    def _stub_is_text_source(p):
        calls.append(str(p))
        return False  # 전부 '텍스트 아님' 강제 → 스캔에서 빠져야

    real = board._load_pm_update_module()
    stub = SimpleNamespace(read_manifest=real.read_manifest,
                           _is_text_source=_stub_is_text_source)
    monkeypatch.setattr(board, "_load_pm_update_module", lambda: stub)

    issues = board.lint_render_leak()
    assert calls, "_is_text_source 미호출 — 판정이 공유되지 않고 인라인 사본일 가능성(세 번째 지점)"
    assert issues == [], (
        f"_is_text_source=False 인데 스캔됨 — 텍스트 판정 위임 안 됨: {issues}")


def test_render_leak_non_ascii_filename_and_content(board, monkeypatch, tmp_path):
    """비-ASCII 파일명·내용(UTF-8)의 미해소 토큰도 잡는다 (파일명/내용 인코딩 무관)."""
    fake_repo = tmp_path / "repo"
    _rendered_tree(fake_repo)
    leak = _write_managed_file(
        fake_repo, ".claude/agents/설정.toml",
        text='설명 = "{{PROJECT_NAME}} 프로젝트 · 한글 본문"\n')
    monkeypatch.setattr(board, "REPO", fake_repo)

    findings = board.lint_render_leak()
    rel = str(leak.relative_to(fake_repo)).replace("\\", "/")
    assert any(name == rel and kind == "render-leak" and "{{PROJECT_NAME}}" in detail
               for name, kind, detail in findings), (
        f"비-ASCII 파일명/내용의 leak 을 못 잡음: {findings}")


def test_render_leak_empty_and_missing_render_paths_no_crash(board, monkeypatch, tmp_path):
    """@render path 가 빈 디렉토리·부재 경로여도 크래시 0·finding 0 (성공-입력-only fixture 방지).

    `.claude/agents` = 부재(is_dir·is_file 둘 다 False), `.opencode/agents` = 빈 디렉토리(파일 0).
    """
    fake_repo = tmp_path / "repo"
    _write_root_manifest(fake_repo, ".claude/agents @render\n.opencode/agents @render\n")
    (fake_repo / ".project_manager" / "local.conf").write_text(
        "project_name=acme\n", encoding="utf-8")
    (fake_repo / ".opencode" / "agents").mkdir(parents=True)  # 빈 디렉토리
    # .claude/agents 는 만들지 않는다 → 부재 경로.
    monkeypatch.setattr(board, "REPO", fake_repo)

    assert board.lint_render_leak() == [], "빈/부재 @render 경로에서 크래시 또는 오탐"


def test_render_leak_clean_non_md_text_no_false_positive(board, monkeypatch, tmp_path):
    """specificity: 토큰 없는 `.toml`(완전 렌더)은 finding 0 — 넓힌 스캔이 오탐을 내지 않는다."""
    fake_repo = tmp_path / "repo"
    _rendered_tree(fake_repo)
    _write_managed_file(fake_repo, ".claude/agents/config.toml",
                        text='name = "acme"\nroot = "core/**"\n')
    monkeypatch.setattr(board, "REPO", fake_repo)

    assert board.lint_render_leak() == []


def test_claude_adapter_manifest_is_render_not_target_owned(board):
    """루트 canonical manifest `.claude/agents`·`.claude/skills` 가 @render·**non**-@target-owned (T-0170 B).

    `@render`: bare 행이면 채택 인스턴스 self-update 가 토큰-form 어댑터를 *치환된* 라이브 위에
    byte-copy 해 de-substitute 한다 — render 로 치환 경로를 탄다(de-substitution 근절).
    **non-@target-owned (의도적·안전판)**: `.claude/*` 는 루트 upstream 에 실재하는 엔진 리소스(4 md)
    라 source 부재 시 rc2(잘못된 --from·불완전 source 탐지)가 *옳다*. `@target-owned` 를 달면 그
    안전판이 꺼져 엔진 누락을 은폐한다(pm_update.py:985). `.opencode/*`(루트 부재=진짜 target-owned)
    와 비대칭. 선례 [[T-0154]]·hermetic·git-network 0.
    """
    pm_update = board._load_pm_update_module()
    assert pm_update is not None, "pm_update 모듈 로드 실패"
    root_manifest = board.REPO / ".project_manager" / "engine.manifest"
    assert root_manifest.is_file(), "루트 .project_manager/engine.manifest 없음"

    by_path = {str(e): e for e in pm_update.read_manifest(root_manifest)}
    for adapter in (".claude/agents", ".claude/skills"):
        assert adapter in by_path, f"루트 manifest 에 {adapter} 항목 없음"
        entry = by_path[adapter]
        assert entry.render is True, (
            f"{adapter} 가 @render 아님 — bare 행이면 self-update 가 de-substitute 한다(T-0170)."
        )
        assert entry.target_owned is False, (
            f"{adapter} 가 @target-owned 임 — 루트 실재 엔진 리소스라 source 부재 시 rc2 가 옳다 "
            "(@target-owned 는 엔진 누락 은폐·pm_update.py:985)."
        )


# ── un-migrated overlay 검출 (advisory · T-0132·§3.6·ADR-0031) ─────────────
# 어댑터 .md 에 리터럴 free-form 토큰(`_UNMIGRATED_FREEFORM_KEYS` 로컬 튜플·ADR-0031 디커플)
# 잔존 = canonical home(root doc·pm_role.local.md) 마이그레이션 미완 신호. advisory
# (`_ADVISORY_LINT_KINDS`·`--gate` 미차단). operational 토큰·code-fence 예시는 검사 제외
# (오탐 0). 어댑터 부재 tree finding 0(graceful). overlay 파일 부재 조건은 ADR-0031 로 제거됐다
# — free-form value-fill 기계(overlay.local.yaml)가 없어졌으므로 리터럴 토큰 잔존만으로 advisory.

def _register_render_dir(root: Path, render_dir: str) -> None:
    """tmp 어댑터 트리의 engine.manifest 에 `<render_dir>    @render` 를 멱등 등록 (T-0431).

    un-migrated 스캔은 engine.manifest `@render` dest 경로에서 스코프를 파생하므로(런타임/설정 파일
    제외), 테스트도 실 채택자 형상처럼 manifest 를 갖춰야 한다."""
    manifest = root / ".project_manager" / "engine.manifest"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    lines = manifest.read_text(encoding="utf-8").splitlines() if manifest.is_file() else []
    already = any(
        ln.split()[:1] == [render_dir]
        for ln in lines if ln.strip() and not ln.lstrip().startswith("#"))
    if not already:
        lines.append(f"{render_dir}    @render")
        manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _adapter_doc(root: Path, relpath: str, text: str) -> Path:
    """root 아래 어댑터 스캐폴드 본문을 만들고 그 `@render` 어댑터-본문 dir 를 manifest 에 등록한다.

    예: `.claude/agents/developer.md` → 파일 생성 + `.claude/agents @render` 등재. 어댑터-본문 dir =
    relpath 앞 2세그먼트(`.claude/agents`·`.codex/agents`·`.agents/skills` 등). root 문서(1세그먼트·
    CLAUDE.md/AGENTS.md)는 `@render` 아님(instance-owned·T-0133)이라 미등재 → 스캔 스코프 밖."""
    p = root / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    parts = relpath.replace("\\", "/").split("/")
    if len(parts) >= 2:  # 어댑터-본문 dir 만 등록 (root 문서=1세그먼트는 @render 아님).
        _register_render_dir(root, "/".join(parts[:2]))
    return p


def test_unmigrated_literal_token_is_advisory_hit(board, monkeypatch, tmp_path):
    """(a) 리터럴 free-form 토큰 잔존 어댑터 → `un-migrated-overlay` advisory finding."""
    monkeypatch.setattr(board, "REPO", tmp_path)
    _adapter_doc(tmp_path, ".claude/agents/developer.md",
                 "## 프로젝트 제약\n\n{{PROJECT_CONSTRAINTS}}\n")
    issues = board.lint_unmigrated_overlay()
    assert any(name == ".claude/agents/developer.md"
               and kind == "un-migrated-overlay"
               and "{{PROJECT_CONSTRAINTS}}" in detail
               for name, kind, detail in issues), issues


def test_unmigrated_kind_is_advisory_gate_excluded(board, monkeypatch, tmp_path):
    """(a) `un-migrated-overlay` 는 `_ADVISORY_LINT_KINDS` 등재 → `--gate` 종료코드 0(미차단)."""
    assert "un-migrated-overlay" in board._ADVISORY_LINT_KINDS
    monkeypatch.setattr(board, "REPO", tmp_path)
    _adapter_doc(tmp_path, ".opencode/agents/architect.md",
                 "## 보호 영역\n\n{{PROTECTED_PATHS}}\n")
    # 다른 lint 표면은 비워 un-migrated finding 만 게이트에 반영.
    for fn in ("lint_dependencies", "lint_bodies", "lint_ideas", "lint_status",
               "lint_wikilinks", "lint_unstable_refs", "lint_scopes",
               "lint_domain", "lint_adr_lifecycle", "lint_architecture_freshness",
               "lint_status_freshness", "lint_domain_freshness",
               "lint_adapter_drift", "lint_render_leak", "_run_lint_hooks"):
        monkeypatch.setattr(board, fn, lambda: [])
    issues = board.lint_unmigrated_overlay()
    assert issues, "un-migrated finding 이 있어야 한다."
    assert all(kind == "un-migrated-overlay" for _n, kind, _d in issues), issues
    # advisory 라 gate 는 통과(0), full 은 finding 있으면 1(현행 계약).
    assert board.cmd_lint(SimpleNamespace(gate=True)) == 0
    assert board.cmd_lint(SimpleNamespace(gate=False)) == 1


def test_unmigrated_no_tokens_is_clean(board, monkeypatch, tmp_path):
    """(b) 리터럴 free-form 토큰 0(마이그레이션 완료 = canonical home) → finding 0(clean)."""
    monkeypatch.setattr(board, "REPO", tmp_path)
    # 마이그레이션 후 — free-form 토큰이 canonical home 으로 옮겨져 어댑터엔 토큰 0.
    _adapter_doc(tmp_path, ".claude/agents/developer.md",
                 "## 프로젝트 제약\n\n- 핵심 결정 로직 = 순수 코드.\n")
    _adapter_doc(tmp_path, ".claude/skills/pm-wave-claim/SKILL.md",
                 "## 보호 영역\n\nconfig/limits.py\n")
    assert board.lint_unmigrated_overlay() == []


def test_unmigrated_token_finding_per_file(board, monkeypatch, tmp_path):
    """(c) 리터럴 토큰 잔존 → 파일별 finding 1건(잔존 토큰 합산·ADR-0031 디커플 후 토큰 finding 만)."""
    monkeypatch.setattr(board, "REPO", tmp_path)
    _adapter_doc(tmp_path, ".claude/agents/developer.md",
                 "## 제약\n\n{{PROJECT_CONSTRAINTS}}\n\n## 보호\n\n{{PROTECTED_PATHS}}\n")
    issues = board.lint_unmigrated_overlay()
    # 토큰 finding (파일별 1건·잔존 토큰 합산).
    match = [i for i in issues if i[0] == ".claude/agents/developer.md"]
    assert len(match) == 1, issues
    _name, kind, detail = match[0]
    assert kind == "un-migrated-overlay"
    assert "{{PROJECT_CONSTRAINTS}}" in detail and "{{PROTECTED_PATHS}}" in detail
    # overlay 파일 부재 조건은 ADR-0031 로 제거 — overlay-부재 finding 은 없다(토큰 finding 만).
    assert len(issues) == 1, issues


def test_unmigrated_operational_token_not_flagged(board, monkeypatch, tmp_path):
    """(d) operational 토큰(`{{PROJECT_NAME}}` 등)은 검사 대상 아님 — 오탐 0."""
    monkeypatch.setattr(board, "REPO", tmp_path)
    _adapter_doc(tmp_path, ".claude/agents/developer.md",
                 "너는 {{PROJECT_NAME}} 의 developer 다. {{PY}} {{TEST_CMD}} 로 검증.\n")
    assert board.lint_unmigrated_overlay() == []


def test_unmigrated_code_fence_example_not_flagged(board, monkeypatch, tmp_path):
    """(d) code span/fence 안 *예시* free-form 토큰은 `_strip_code` 로 제거 → 오탐 0."""
    monkeypatch.setattr(board, "REPO", tmp_path)
    _adapter_doc(tmp_path, ".claude/agents/developer.md",
                 "토큰 예시: `{{PROTECTED_PATHS}}` 는 canonical home 이 채운다.\n\n"
                 "```yaml\nPROTECTED_PATHS: |\n  {{PROJECT_CONSTRAINTS}}\n```\n")
    assert board.lint_unmigrated_overlay() == []


def test_unmigrated_absent_adapter_tree_is_clean(board, monkeypatch, tmp_path):
    """graceful: 어댑터 파일/디렉토리 부재(솔로·non-adopter tree) → finding 0."""
    monkeypatch.setattr(board, "REPO", tmp_path)
    # 어떤 어댑터 스캐폴드도 만들지 않는다 — 빈 tree.
    assert board.lint_unmigrated_overlay() == []


def test_unmigrated_root_doc_token_not_flagged(board, monkeypatch, tmp_path):
    """root 어댑터 doc(CLAUDE.md/AGENTS.md)의 리터럴 free-form 토큰은 *미-flag* (T-0133).

    root 문서는 채택자가 통째로 손편집하는 instance-owned scaffold 라 render-overlay 관리
    대상이 아니다(manifest 제외·omit-marker 0). 거기의 raw 토큰은 "미마이그레이션"이 아니라
    "채택자가 아직 안 채움"이라 lint 가 오분류하면 안 된다 — root doc 만 두면 clean.
    """
    monkeypatch.setattr(board, "REPO", tmp_path)
    _adapter_doc(tmp_path, "AGENTS.md", "## 사용자 게이트\n\n{{USER_GATE_ITEMS}}\n")
    _adapter_doc(tmp_path, "CLAUDE.md", "## 보호 영역\n\n{{PROTECTED_PATHS}}\n")
    assert board.lint_unmigrated_overlay() == []


def test_unmigrated_adapter_dir_still_flagged_when_root_doc_present(
        board, monkeypatch, tmp_path):
    """root doc 은 미-flag 하되 어댑터 디렉토리 토큰은 여전히 flag (root-doc 제외가 디렉토리 스캔 무영향)."""
    monkeypatch.setattr(board, "REPO", tmp_path)
    # root doc 토큰은 무시돼야 한다.
    _adapter_doc(tmp_path, "CLAUDE.md", "## 보호\n\n{{PROTECTED_PATHS}}\n")
    # 어댑터 디렉토리 토큰은 여전히 flag.
    _adapter_doc(tmp_path, ".claude/agents/developer.md",
                 "## 제약\n\n{{PROJECT_CONSTRAINTS}}\n")
    issues = board.lint_unmigrated_overlay()
    # 어댑터 finding 은 있다.
    assert any(name == ".claude/agents/developer.md" for name, _k, _d in issues), issues
    # root doc 은 어떤 finding 도 만들지 않는다(스캔 대상 아님).
    assert not any(name in ("CLAUDE.md", "AGENTS.md") for name, _k, _d in issues), issues


def test_unmigrated_skill_nested_scanned(board, monkeypatch, tmp_path):
    """`.claude/skills/**/SKILL.md` 는 중첩(rglob)으로 스캔된다(직속 *.md 아님)."""
    monkeypatch.setattr(board, "REPO", tmp_path)
    _adapter_doc(tmp_path, ".claude/skills/pm-dev-delegate/SKILL.md",
                 "## 제약\n\n{{PROJECT_CONSTRAINTS}}\n")
    issues = board.lint_unmigrated_overlay()
    assert any(name == ".claude/skills/pm-dev-delegate/SKILL.md"
               for name, _k, _d in issues), issues


# ── 두 축 파생 — codex(세 번째 하네스) 편입 (T-0431·[[T-0429]] 엔진 대칭) ─────────
# 옛 `_OVERLAY_ADAPTER_GLOBS`(`.claude`/`.opencode` × `*.md`/SKILL.md)는 harness 축·확장자 축이
# 둘 다 손-열거라 codex `.codex/agents/*.toml` 을 구조적으로 못 봤다. 디렉토리 축은
# pm_import.ADD_HARNESS_ADAPTER 에서, 파일 필터 축은 pm_update._is_text_source 로 파생해 역전한다.

def test_unmigrated_codex_toml_agent_scanned(board, monkeypatch, tmp_path):
    """codex 형상: `.codex/agents/*.toml` 의 리터럴 free-form 토큰이 잡힌다 (T-0431 핵심 DoD).

    수정 전(손-열거)엔 `.codex` 하네스 축도, `.toml` 확장자 축도 스캔 밖이라 미검출이었다 — 두 축
    파생 후 편입. `.toml` 은 markdown 코드 마커가 없어 `_strip_code` 통과 후 토큰이 매칭된다."""
    monkeypatch.setattr(board, "REPO", tmp_path)
    _adapter_doc(tmp_path, ".codex/agents/developer.toml",
                 'developer_instructions = "## 제약: {{PROJECT_CONSTRAINTS}}"\n')
    issues = board.lint_unmigrated_overlay()
    assert any(name == ".codex/agents/developer.toml"
               and kind == "un-migrated-overlay"
               and "{{PROJECT_CONSTRAINTS}}" in detail
               for name, kind, detail in issues), issues


def test_unmigrated_codex_dual_namespace_agents_skill_scanned(board, monkeypatch, tmp_path):
    """codex dual-namespace: `.agents/skills/**/SKILL.md`(ADR-0070 D5 ①)도 스캔된다 —
    ADD_HARNESS_ADAPTER["codex"] 의 두 번째 adapter dir(`.agents`)이 파생 축에 편입되기 때문."""
    monkeypatch.setattr(board, "REPO", tmp_path)
    _adapter_doc(tmp_path, ".agents/skills/pm-x/SKILL.md",
                 "## 보호 영역\n\n{{PROTECTED_PATHS}}\n")
    issues = board.lint_unmigrated_overlay()
    assert any(name == ".agents/skills/pm-x/SKILL.md"
               for name, _k, _d in issues), issues


def test_overlay_scope_is_manifest_render_derived(board, monkeypatch, tmp_path):
    """스코프가 engine.manifest `@render` 파생임을 직접 단언 — render-leak 과 공유하는
    `_manifest_render_relpaths` 단일 소스(신규 독립 사본 0·T-0431 DoD). 같은 파일 형식이라도
    manifest 미등재 형제 dir 는 스캔되지 않는다(스코프가 manifest 로만 열림)."""
    monkeypatch.setattr(board, "REPO", tmp_path)
    _adapter_doc(tmp_path, ".claude/agents/developer.md", "{{PROJECT_CONSTRAINTS}}\n")  # @render 등재
    unlisted = tmp_path / ".claude" / "unlisted" / "x.md"  # 미등재 형제 dir
    unlisted.parent.mkdir(parents=True, exist_ok=True)
    unlisted.write_text("{{PROTECTED_PATHS}}\n", encoding="utf-8")
    managed = board._manifest_render_relpaths()
    assert ".claude/agents" in managed and ".claude/unlisted" not in managed, managed
    scanned = {board._rel_to_repo(p).replace("\\", "/")
               for p in board._collect_overlay_adapter_files()}
    assert ".claude/agents/developer.md" in scanned, scanned
    assert ".claude/unlisted/x.md" not in scanned, scanned


def test_overlay_scope_excludes_runtime_and_config_files(board, monkeypatch, tmp_path):
    """스캔 스코프가 `@render`(어댑터 본문)로 조여져 런타임/설정 파일은 제외된다 (T-0431 codex MF·
    codex suggestion 회귀 박제).

    옛 네임스페이스-전체 rglob 은 `.opencode/node_modules`(플러그인 deps)·adopter-owned
    `.codex/config.toml`·`.claude/settings.json` 까지 읽어 대량 이중 read + 무관 파일 토큰 오탐
    리스크였다. 이들 비-`@render` 경로에 리터럴 토큰을 심어도 스캔·finding 0 이어야 한다."""
    monkeypatch.setattr(board, "REPO", tmp_path)
    # @render 어댑터 본문 (스캔 O) — _adapter_doc 가 manifest 에 .opencode/agents 등록.
    _adapter_doc(tmp_path, ".opencode/agents/architect.md",
                 "## 제약\n\n{{PROJECT_CONSTRAINTS}}\n")
    # 비-@render 런타임/설정 (스캔 X·manifest 미등재) — 토큰을 심어도 미검사.
    for rel, body in (
        (".opencode/node_modules/pkg/readme.md", "{{PROTECTED_PATHS}}\n"),
        (".codex/config.toml", "model = '{{USER_GATE_ITEMS}}'\n"),
        (".codex/hooks.json", '{"x": "{{PROTECTED_PATHS}}"}\n'),
        (".claude/settings.json", '{"y": "{{PROJECT_CONSTRAINTS}}"}\n'),
    ):
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body, encoding="utf-8")

    scanned = {board._rel_to_repo(p).replace("\\", "/")
               for p in board._collect_overlay_adapter_files()}
    assert ".opencode/agents/architect.md" in scanned, scanned
    assert not any("node_modules" in s for s in scanned), scanned
    assert ".codex/config.toml" not in scanned and ".codex/hooks.json" not in scanned, scanned
    assert ".claude/settings.json" not in scanned, scanned

    finds = board.lint_unmigrated_overlay()
    assert any(n == ".opencode/agents/architect.md" for n, _k, _d in finds), finds
    assert not any(("node_modules" in n or n in (
        ".codex/config.toml", ".codex/hooks.json", ".claude/settings.json"))
        for n, _k, _d in finds), finds


def test_unmigrated_retired_opencode_command_still_scanned(board, monkeypatch, tmp_path):
    """은퇴 채널(ADR-0065·`.opencode/command`) 잔존 파일의 un-migrated 토큰이 여전히 잡힌다
    (T-0431 codex R3 MF). 이 채널은 현행 manifest 에 미등재(은퇴)지만 pm_update 가 채택자 파일을
    삭제하지 않아 구 채택자 트리에 잔존할 수 있다 — @render 파생 스코프의 보완(`_RETIRED_OVERLAY_GLOBS`).

    manifest 를 만들지 않고 파일만 심어, 은퇴 글롭이 **manifest 와 독립**으로 커버함을 단언한다
    (@render-only 스코프였다면 미검출·red-첫-재현의 durable 박제)."""
    monkeypatch.setattr(board, "REPO", tmp_path)
    p = tmp_path / ".opencode" / "command" / "foo.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("## 보호 영역\n\n{{PROTECTED_PATHS}}\n", encoding="utf-8")
    # manifest 미생성 = @render 스코프 0 — 그래도 은퇴 채널이 잡아야 한다.
    assert board._manifest_render_relpaths() == set(), "@render 미등재 상태여야(은퇴 채널 독립 검증)."
    assert (".opencode/command", "*.md") in board._RETIRED_OVERLAY_GLOBS
    issues = board.lint_unmigrated_overlay()
    assert any(name == ".opencode/command/foo.md"
               and kind == "un-migrated-overlay"
               and "{{PROTECTED_PATHS}}" in detail
               for name, kind, detail in issues), issues


def test_adopter_shape_guest_scanned_via_dest_manifest(tmp_path):
    """adopter 형상(templates/ 없음)에서 guest 어댑터가 dest engine.manifest `@render` 등재로 스캔된다
    (T-0456·codex R12).

    codex R12 비판: 기존 guest 테스트가 worktree board.py + REPO monkeypatch 라 `__file__` 기준
    templates/ 가 worktree 것이라 인스턴스 형상(templates/ 부재)을 **가렸다**(옛 flavor-manifest 보강이
    거기선 항상 ∅인데 테스트는 green). 여기선 **templates/ 없는 tools/ 사본으로 board.py 를 로드**해
    실 인스턴스를 재현한다 — guest 커버는 오직 dest manifest 등재(add_harness·T-0456)로만 성립한다.

    red-첫: dest manifest 에 guest `@render` 미등재면 미검출 → 등재(add_harness 가 하는 것) 후 검출."""
    inst = tmp_path / "adopter"
    (inst / ".project_manager").mkdir(parents=True)
    shutil.copytree(TOOLS, inst / ".project_manager" / "tools")
    assert not (inst / "templates").exists()  # adopter 형상 = templates/ 부재
    # host=claude 만 등재 + guest opencode 어댑터(add-harness 레이다운) + free-form 토큰 잔존
    manifest = inst / ".project_manager" / "engine.manifest"
    manifest.write_text(".claude/agents    @render\n", encoding="utf-8")
    guest = inst / ".opencode" / "agents" / "architect.md"
    guest.parent.mkdir(parents=True)
    guest.write_text("## 보호\n\n{{PROTECTED_PATHS}}\n", encoding="utf-8")

    board_inst = _load_module("board_adopter_shape", inst / ".project_manager" / "tools" / "board.py")
    assert board_inst.REPO == inst.resolve()  # __file__ 기준 자동 해소 (REPO monkeypatch 아님)
    assert not hasattr(board_inst, "_all_harness_body_relpaths")  # templates/ 보강 제거됨(판정원 단일)
    # red: dest manifest 에 guest @render 미등재 → 미검출 (인스턴스엔 templates/ 도 없음)
    assert not any(n == ".opencode/agents/architect.md"
                   for n, _k, _d in board_inst.lint_unmigrated_overlay())
    # green: add_harness 가 등재하는 것과 동형으로 dest manifest 에 guest @render 추가 → 검출
    manifest.write_text(
        ".claude/agents    @render\n"
        ".opencode/agents    @render @source=templates/opencode/.opencode/agents\n",
        encoding="utf-8")
    issues = board_inst.lint_unmigrated_overlay()
    assert any(n == ".opencode/agents/architect.md"
               and "{{PROTECTED_PATHS}}" in d
               for n, _k, d in issues), issues


def test_unmigrated_codex_missed_under_old_hardcoded_collection(board, monkeypatch, tmp_path):
    """sensitivity — 옛 손-열거 collection(harness `.claude`/`.opencode` + 확장자 `*.md`/SKILL.md)을
    복원하면 codex `.toml`·`.agents` 가 다시 구조적으로 미검출(red). 두 축 파생이 load-bearing 임을
    증명하고 red-첫-재현을 durable 로 박제한다 (T-0431 DoD)."""
    monkeypatch.setattr(board, "REPO", tmp_path)
    _adapter_doc(tmp_path, ".codex/agents/developer.toml",
                 'x = "{{PROJECT_CONSTRAINTS}}"\n')
    _adapter_doc(tmp_path, ".agents/skills/pm-x/SKILL.md", "{{PROTECTED_PATHS}}\n")
    _adapter_doc(tmp_path, ".claude/agents/developer.md", "{{USER_GATE_ITEMS}}\n")

    # (a) 현행 파생 → codex 두 네임스페이스 모두 잡힌다(대조군: claude .md 도).
    finds = board.lint_unmigrated_overlay()
    assert any(n == ".codex/agents/developer.toml" for n, _k, _d in finds), finds
    assert any(n == ".agents/skills/pm-x/SKILL.md" for n, _k, _d in finds), finds

    # (b) 옛 손-열거 collection 복원 → codex 미검출, claude .md 만 남는다(red).
    _old_globs = (
        (".claude/agents", "*.md"), (".claude/skills", "SKILL.md"),
        (".opencode/agents", "*.md"), (".opencode/command", "*.md"),
    )

    def _old_collect():
        out: list[Path] = []
        for rel, pat in _old_globs:
            d = board.REPO / rel
            if not d.is_dir():
                continue
            out.extend(d.rglob(pat) if pat == "SKILL.md" else d.glob(pat))
        return out

    monkeypatch.setattr(board, "_collect_overlay_adapter_files", _old_collect)
    reverted = board.lint_unmigrated_overlay()
    assert not any(n == ".codex/agents/developer.toml" for n, _k, _d in reverted), reverted
    assert not any(n == ".agents/skills/pm-x/SKILL.md" for n, _k, _d in reverted), reverted
    assert any(n == ".claude/agents/developer.md" for n, _k, _d in reverted), reverted


def test_lint_tickets_includes_unmigrated_overlay(board, monkeypatch):
    """lint_tickets 통합 — un-migrated finding 이 전체 보고에 포함된다."""
    sentinel = [(".claude/agents/x.md", "un-migrated-overlay", "sentinel")]
    for fn in ("lint_dependencies", "lint_bodies", "lint_ideas", "lint_status",
               "lint_wikilinks", "lint_unstable_refs", "lint_scopes",
               "lint_domain", "lint_adr_lifecycle", "lint_architecture_freshness",
               "lint_status_freshness", "lint_domain_freshness",
               "lint_adapter_drift", "lint_render_leak"):
        monkeypatch.setattr(board, fn, lambda: [])
    monkeypatch.setattr(board, "lint_unmigrated_overlay", lambda: sentinel)
    assert sentinel[0] in board.lint_tickets()


# ── adapter-layer drift lint (T-0141·ADR-0032 Decision 2·advisory·baseline B) ─────
# `lint_adapter_drift` 는 git network 0 — `local.conf` 의 2키
# (`upstream_rev` baseline ↔ `upstream_seen_rev` 현재 관찰값)만 비교한다. 둘 다 존재하고
# *다르면* drift 1 finding(baseline 이후 upstream 변경). 한쪽이라도 부재·upstream 미설정·
# 같은 rev 면 graceful 0(fail-soft). 테스트는 `local_config` 를 stub 해 hermetic 하게 2키를
# 주입한다(파일 IO·network 0). scope 제외(instance-state)는 lint 가 파일 diff 를 안 하므로
# 자동 충족 — 여기선 rev 비교 sensitivity 와 advisory never-block 만 검증한다.

def _wire_conf(board, monkeypatch, conf: dict) -> None:
    """`local_config()` 를 고정 dict 로 stub — local.conf 파일/network 없이 2키 주입."""
    monkeypatch.setattr(board, "local_config", lambda: dict(conf))


def test_adapter_drift_flags_when_baseline_differs_from_seen(board, monkeypatch):
    # 정상 baseline 이후 upstream 이 앞섬(seen≠baseline) → 인위 drift → finding 1.
    _wire_conf(board, monkeypatch, {
        "upstream": "https://github.com/example/project_manager",
        "upstream_rev": "aaaaaaaaaaaa1111",
        "upstream_seen_rev": "bbbbbbbbbbbb2222",
    })
    findings = board.lint_adapter_drift()
    assert len(findings) == 1
    label, kind, detail = findings[0]
    assert label == "adapter-layer"
    assert kind == "adapter-drift"
    # baseline·seen 양쪽 rev 와 pm-update 안내가 메시지에 노출.
    assert "aaaaaaaaaaaa" in detail and "bbbbbbbbbbbb" in detail
    assert "pm-update" in detail


def test_adapter_drift_clean_when_baseline_equals_seen(board, monkeypatch):
    # baseline == seen → 마지막 동기 이후 upstream 변경 없음 → finding 0 (정상→0).
    _wire_conf(board, monkeypatch, {
        "upstream": "https://github.com/example/project_manager",
        "upstream_rev": "cccccccccccc3333",
        "upstream_seen_rev": "cccccccccccc3333",
    })
    assert board.lint_adapter_drift() == []


def test_adapter_drift_graceful_when_upstream_absent(board, monkeypatch):
    # 솔로·non-adopter — upstream 자체 부재 → graceful 0 (fail-soft).
    _wire_conf(board, monkeypatch, {
        "upstream_rev": "aaaaaaaaaaaa1111",
        "upstream_seen_rev": "bbbbbbbbbbbb2222",
    })
    assert board.lint_adapter_drift() == []


def test_adapter_drift_graceful_when_baseline_unrecorded(board, monkeypatch):
    # baseline(`upstream_rev`) 미기록(구 import·revision 추적 전) → graceful 0.
    _wire_conf(board, monkeypatch, {
        "upstream": "/some/path/project_manager_1",
        "upstream_seen_rev": "bbbbbbbbbbbb2222",
    })
    assert board.lint_adapter_drift() == []


def test_adapter_drift_observability_advisory_when_seen_unrecorded(board, monkeypatch):
    # baseline 은 있으나 seen(`upstream_seen_rev`) 미기록 → 관찰불가 advisory (T-0305·ADR-0032 Q3).
    # 과거엔 조용한 [](silent skip)였으나, safety-critical 잔여가 *관찰 없이* 낡는 "green 인데 고장"을
    # 막으려 관찰불가 자체를 표면화한다(never-block·1줄이라 flood 아님).
    _wire_conf(board, monkeypatch, {
        "upstream": "https://github.com/example/project_manager",
        "upstream_rev": "aaaaaaaaaaaa1111",
    })
    findings = board.lint_adapter_drift()
    assert len(findings) == 1
    label, kind, detail = findings[0]
    assert label == "adapter-layer" and kind == "adapter-drift"
    assert "관찰불가" in detail and "upstream_seen_rev" in detail and "pm-update" in detail


def test_adapter_drift_graceful_when_conf_empty(board, monkeypatch):
    # local.conf 부재(빈 dict·솔로/신규 clone) → graceful 0.
    _wire_conf(board, monkeypatch, {})
    assert board.lint_adapter_drift() == []


def test_adapter_drift_blank_seen_treated_as_unrecorded(board, monkeypatch):
    # seen 키는 있으나 빈 값(`upstream_seen_rev=   `) → strip 후 미기록과 동치 → 관찰불가 advisory
    # (T-0305·never-block). baseline 빈값이면 여전히 graceful [](관찰 기준점 부재).
    _wire_conf(board, monkeypatch, {
        "upstream": "https://github.com/example/project_manager",
        "upstream_rev": "aaaaaaaaaaaa1111",
        "upstream_seen_rev": "   ",
    })
    findings = board.lint_adapter_drift()
    assert len(findings) == 1 and findings[0][1] == "adapter-drift"
    assert "관찰불가" in findings[0][2]


def test_adapter_drift_message_is_direction_neutral(board, monkeypatch):
    # T-0413: lint 는 git 을 호출하지 않아(ADR-0032 D5·rev 문자열 비교뿐) 두 rev 의 선후를 모른다.
    # 따라서 "upstream 이 baseline 이후 변경됨" 같은 방향 단정을 하면 안 된다 — 불일치 사실만 알린다.
    _wire_conf(board, monkeypatch, {
        "upstream": "/w/project_manager_1",
        "upstream_rev": "aaaaaaaaaaaa1111",
        "upstream_seen_rev": "bbbbbbbbbbbb2222",
    })
    findings = board.lint_adapter_drift()
    assert len(findings) == 1
    detail = findings[0][2]
    assert "불일치" in detail
    assert "이후 변경됨" not in detail, f"방향 단정 잔존(거짓 경보 원인): {detail!r}"
    assert "aaaaaaaaaaaa" in detail and "bbbbbbbbbbbb" in detail


def test_adapter_drift_neutral_when_seen_is_older_than_baseline(board, monkeypatch):
    # ② adopter#0 실측(PM 4차): seen(0ccc0251…·v1.3.5)이 baseline(ddf6f484…·v1.4.0)의 *조상*.
    # finding 개수·kind 는 불변(advisory 1)이되, 메시지가 "upstream 이 앞섰다"고 단정하면 거짓이다.
    _wire_conf(board, monkeypatch, {
        "upstream": "/w/project_manager_1",
        "upstream_rev": "ddf6f4842653",
        "upstream_seen_rev": "0ccc02513a7f",
    })
    findings = board.lint_adapter_drift()
    assert len(findings) == 1 and findings[0][1] == "adapter-drift"
    detail = findings[0][2]
    assert "이후 변경됨" not in detail, f"조상 관찰값에 방향 단정(거짓 주장): {detail!r}"
    assert "ddf6f4842653" in detail and "0ccc02513a7f" in detail


def test_path_upstream_sync_converges_two_keys_to_drift_clean(board, monkeypatch, tmp_path):
    """연결 검증(T-0413) — 경로 upstream sync 1회 후 lint 의 adapter-drift 가 실제로 0이 된다.

    엔진 기록(pm_update.record_upstream_rev_baseline)과 판정(board.lint_adapter_drift)을 실
    local.conf 파일로 이어 붙인다 — 두 쪽 단위테스트가 각자 green 이어도 배선이 끊기면 거짓
    경보가 남으므로, 어긋난 conf(baseline 신·seen 구)가 sync 로 수렴함을 파일 왕복으로 본다.
    """
    pm_update = _load_module("pm_update", TOOLS / "pm_update.py")
    dest = tmp_path / "adopter"
    source = tmp_path / "upstream_checkout"
    source.mkdir()
    local_conf = dest / ".project_manager" / "local.conf"
    local_conf.parent.mkdir(parents=True, exist_ok=True)
    local_conf.write_text(
        f"upstream={source}\n"
        "upstream_rev=ddf6f4842653\n"
        "upstream_seen_rev=0ccc02513a7f\n",  # 조상 — 흡수 직후에도 거짓 drift 를 내던 형상.
        encoding="utf-8",
    )
    monkeypatch.setattr(board, "LOCAL_CONF", local_conf)

    # 어긋난 채로는 advisory 1건(회귀 sensitivity — 이 테스트가 vacuous 하지 않음).
    assert len(board.lint_adapter_drift()) == 1

    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: "converged9999")
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)
    assert pm_update.record_upstream_rev_baseline(dest, source) is True

    assert board.lint_adapter_drift() == [], \
        f"경로 sync 후에도 drift advisory 잔존: {local_conf.read_text(encoding='utf-8')!r}"


def test_adapter_drift_uses_two_distinct_keys(board, monkeypatch):
    # 한 키 2역 금지(race/자기비교 회피·codex round-3 NEW-2) — baseline 키와 seen 키가 분리돼야.
    assert board._DRIFT_BASELINE_KEY == "upstream_rev"
    assert board._DRIFT_SEEN_KEY == "upstream_seen_rev"
    assert board._DRIFT_BASELINE_KEY != board._DRIFT_SEEN_KEY


def test_adapter_drift_kind_is_advisory(board):
    # adapter-drift 는 `_ADVISORY_LINT_KINDS` 등재 → --gate 종료코드 비기여(never-block).
    assert "adapter-drift" in board._ADVISORY_LINT_KINDS


def test_adapter_drift_is_advisory_never_blocks(board, monkeypatch):
    # 인위 drift finding 만 있어도 --gate 는 0(미차단)·무인자는 표면화로 1 (sensitivity).
    for fn in ("lint_dependencies", "lint_bodies", "lint_ideas", "lint_status",
               "lint_wikilinks", "lint_unstable_refs", "lint_scopes",
               "lint_domain", "lint_adr_lifecycle", "lint_architecture_freshness",
               "lint_render_leak", "lint_unmigrated_overlay", "_run_lint_hooks"):
        monkeypatch.setattr(board, fn, lambda: [])
    _wire_conf(board, monkeypatch, {
        "upstream": "https://github.com/example/project_manager",
        "upstream_rev": "aaaaaaaaaaaa1111",
        "upstream_seen_rev": "bbbbbbbbbbbb2222",
    })
    issues = board.lint_adapter_drift()
    assert issues and all(k == "adapter-drift" for _n, k, _d in issues)
    assert board.cmd_lint(SimpleNamespace(gate=True)) == 0
    assert board.cmd_lint(SimpleNamespace(gate=False)) == 1


def test_lint_tickets_includes_adapter_drift(board, monkeypatch):
    # lint_tickets 통합 — adapter-drift finding 이 전체 보고에 포함된다.
    sentinel = [("adapter-layer", "adapter-drift", "sentinel")]
    for fn in ("lint_dependencies", "lint_bodies", "lint_ideas", "lint_status",
               "lint_wikilinks", "lint_unstable_refs", "lint_scopes",
               "lint_domain", "lint_adr_lifecycle", "lint_architecture_freshness",
               "lint_render_leak", "lint_unmigrated_overlay"):
        monkeypatch.setattr(board, fn, lambda: [])
    monkeypatch.setattr(board, "lint_adapter_drift", lambda: sentinel)
    assert sentinel[0] in board.lint_tickets()


# ── areas-duplicate-repo (ADR-0072·T-0417·advisory·never-block) ───────────────

def test_gate_zero_on_areas_duplicate_repo_only(board, monkeypatch, tmp_path):
    """중복 repo 행만 있으면 `--gate` 종료코드 0 — 레지스트리 정리는 push 결함이 아니다.

    실 areas.md(tmp) 를 중복 행으로 깔아 `lint_areas_duplicate_repo` 를 *실제로* 구동한다
    (sentinel 주입 아님) — advisory 등재 누락이면 여기서 1 로 뒤집힌다.
    """
    _wire_repo(board, monkeypatch, tmp_path)
    (tmp_path / ".project_manager" / ".local").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".project_manager" / "areas.md").write_text(
        "| repo | prefix | git | test_cmd | owner | base | protected | area_owner |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| svc | P | g | t | o | main | main | u |\n"
        "| svc | P | g | t | o | main | develop | u |\n",
        encoding="utf-8")
    # inline 형상의 union 배포는 정상으로 둔다 — 이 tmp 홈에 루트 선언이 없으면
    # `areas-merge-union`(T-0418) advisory 가 같이 잡혀 이 테스트의 kind 단언이 흐려진다.
    (tmp_path / ".gitattributes").write_text(
        ".project_manager/areas.md merge=union\n", encoding="utf-8")
    for fn in ("lint_dependencies", "lint_bodies", "lint_ideas", "lint_status",
               "lint_wikilinks", "lint_unstable_refs", "_run_lint_hooks"):
        monkeypatch.setattr(board, fn, lambda: [])
    issues = board.lint_tickets()
    assert [k for _n, k, _d in issues] == ["areas-duplicate-repo"]
    assert board.cmd_lint(SimpleNamespace(gate=True)) == 0
    assert board.cmd_lint(SimpleNamespace(gate=False)) == 1


def test_lint_tickets_includes_areas_duplicate_repo(board, monkeypatch):
    """lint_tickets 통합 — areas-duplicate-repo finding 이 전체 보고에 포함된다."""
    sentinel = [("svc", "areas-duplicate-repo", "sentinel")]
    for fn in ("lint_dependencies", "lint_bodies", "lint_ideas", "lint_status",
               "lint_wikilinks", "lint_unstable_refs", "lint_scopes",
               "lint_domain", "lint_adr_lifecycle", "lint_architecture_freshness",
               "lint_render_leak", "lint_unmigrated_overlay", "lint_adapter_drift"):
        monkeypatch.setattr(board, fn, lambda: [])
    monkeypatch.setattr(board, "lint_areas_duplicate_repo", lambda: sentinel)
    assert sentinel[0] in board.lint_tickets()


# ════════════════════════════════════════════════════════════════════════
# 손상 frontmatter fail-soft (T-0601 ⑤)
# ════════════════════════════════════════════════════════════════════════
# 한 티켓의 YAML 이 깨지면(실측: `design: waived: 사유` — 콜론 포함 스칼라를 인용 없이 씀) 그 파일을
# 읽는 순간 예외가 나고, 순회 소비자(`list`·`lint`·`refresh`)가 통째로 traceback 으로 죽었다.
# 순회는 그 티켓만 건너뛰되 **조용히 넘기지 않는다**(경고 1줄 + 경로 + 사유). 지정 대상 mutation 은
# 그대로 fail-loud — 고치라고 지목받은 파일이 조용히 무시되면 안 된다.

_BROKEN_FRONTMATTER = (
    "---\n"
    "id: T-0002\n"
    "title: 손상 티켓\n"
    "design: waived: 인용 없는 콜론\n"          # ← yaml.safe_load 가 여기서 터진다
    "---\n"
    "## 목표\n본문\n"
)


def _ticket(board, status: str, tid: str, text: str) -> Path:
    p = board.TICKETS_DIR / status / f"{tid}-seed.md"
    p.write_text(text, encoding="utf-8")
    return p


def _healthy_ticket_text(tid: str) -> str:
    return (f"---\nid: {tid}\ntitle: 정상 티켓\nstatus: open\ndepends_on: []\n---\n"
            "## 목표\n실값\n\n## 완료 조건\n- [x] 끝\n\n## 참고\n- 없음\n")


def test_broken_frontmatter_is_unparseable_by_the_strict_loader(board, monkeypatch, tmp_path):
    """전제 고정 — 이 픽스처는 실제로 파싱 불능이다(테스트가 가짜 손상을 쓰지 않게)."""
    _wire_repo(board, monkeypatch, tmp_path)
    path = _ticket(board, "open", "T-0002", _BROKEN_FRONTMATTER)
    with pytest.raises(Exception):
        board.load_ticket(path)


def test_scan_loader_skips_a_broken_ticket_loudly(board, monkeypatch, tmp_path, capsys):
    """순회 로더는 None + 경고 1줄 — 경로와 사유, 그리고 실제 처방(콜론 인용)을 함께 낸다."""
    _wire_repo(board, monkeypatch, tmp_path)
    path = _ticket(board, "open", "T-0002", _BROKEN_FRONTMATTER)

    assert board.load_ticket_soft(path) is None
    err = capsys.readouterr().err
    assert "건너뜁니다" in err and "T-0002" in err
    assert "인용" in err, "처방(콜론 포함 값은 인용)이 경고에 없다"


def test_scan_loader_rejects_non_mapping_frontmatter(board, monkeypatch, tmp_path, capsys):
    """frontmatter 가 매핑이 아닌 형상도 같은 축으로 접는다 — 뒤따르는 `fm.get` 이 안 터지게."""
    _wire_repo(board, monkeypatch, tmp_path)
    path = _ticket(board, "open", "T-0003", "---\n- 리스트\n- 형상\n---\n# 본문\n")
    assert board.load_ticket_soft(path) is None
    assert "매핑이 아니라" in capsys.readouterr().err


def test_lint_survives_a_broken_ticket(board, monkeypatch, tmp_path, capsys):
    """`lint` 는 손상 1건을 건너뛰고 나머지를 계속 본다 (traceback 0)."""
    _wire_repo(board, monkeypatch, tmp_path)
    _ticket(board, "open", "T-0002", _BROKEN_FRONTMATTER)
    _ticket(board, "open", "T-0001", _healthy_ticket_text("T-0001"))

    assert board.lint_bodies() == []                    # 정상 티켓은 그대로 판정된다
    assert board._all_tickets() == [("open", {
        "id": "T-0001", "title": "정상 티켓", "status": "open", "depends_on": []})]
    assert "T-0002" in capsys.readouterr().err


def test_list_survives_a_broken_ticket(board, monkeypatch, tmp_path, capsys):
    """`list` 도 손상 1건을 건너뛰고 나머지를 출력한다 (보드 전체 조회 불능 폐쇄)."""
    _wire_repo(board, monkeypatch, tmp_path)
    monkeypatch.setattr(board, "LOCAL_CONF", tmp_path / ".project_manager" / "local.conf")
    monkeypatch.setattr(board, "_git_config_email", lambda: None)
    _ticket(board, "open", "T-0002", _BROKEN_FRONTMATTER)
    _ticket(board, "open", "T-0001", _healthy_ticket_text("T-0001"))

    rc = board.cmd_list(SimpleNamespace(
        status=None, tag=None, mine=False, all=True, task=None, repo=None, slot=None))
    out, err = capsys.readouterr()
    assert rc == 0
    assert "T-0001" in out and "T-0002" not in out
    assert "T-0002" in err                              # 조용한 드롭이 아니다


def test_board_refresh_survives_a_broken_ticket(board, monkeypatch, tmp_path):
    """claim/complete 가 부르는 board.md 재생성도 손상 1건에 죽지 않는다."""
    wiki = _wire_repo(board, monkeypatch, tmp_path)
    monkeypatch.setattr(board, "BOARD_FILE", wiki / "board.md")
    _ticket(board, "open", "T-0002", _BROKEN_FRONTMATTER)
    _ticket(board, "open", "T-0001", _healthy_ticket_text("T-0001"))

    board._refresh_board_locked()
    rendered = (wiki / "board.md").read_text(encoding="utf-8")
    assert "T-0001" in rendered and "T-0002" not in rendered


def test_designated_mutation_stays_fail_loud(board, monkeypatch, tmp_path):
    """지정 대상 mutation 은 fail-soft 하지 않는다 — 조용한 무시는 금지다.

    `claim T-0002` 처럼 그 파일을 고치라고 지목한 실행이 skip 되면, 사용자는 아무 일도
    일어나지 않은 이유를 모른 채 티켓이 open/ 에 남은 것을 나중에 발견한다."""
    _wire_repo(board, monkeypatch, tmp_path)
    pm = tmp_path / ".project_manager"
    (pm / ".local").mkdir(parents=True, exist_ok=True)
    for name, value in (("LOCAL_CONF", pm / "local.conf"), ("LOCAL_DIR", pm / ".local"),
                        ("BOARD_LOCK", pm / ".local" / "board.lock"),
                        ("AREAS_FILE", pm / "areas.md"),
                        ("LEASES_FILE", pm / ".local" / "worktree-leases.json")):
        monkeypatch.setattr(board, name, value)
    (pm / "local.conf").write_text("user=tester\n", encoding="utf-8")
    # 세션은 env 명시로 바인딩한다(per-clone conf `session=` 폴백 폐지·T-0779) — 미바인딩이면
    # 이 테스트가 보려는 실패(손상 frontmatter fail-loud) 대신 세션 미해소로 먼저 죽는다.
    monkeypatch.setenv("PM_SESSION_NAME", "pm-1")
    monkeypatch.setattr(board, "_git_config_email", lambda: None)
    ticket = _ticket(board, "open", "T-0002", _BROKEN_FRONTMATTER)

    with pytest.raises(Exception):
        board.cmd_claim(SimpleNamespace(id="T-0002", repo=None, slot=None, user=None))
    assert ticket.exists(), "차단된 claim 이 티켓을 옮겼다"


# ── 순회 소비자 전수 soft 화 (T-0602 ⑦) ──────────────────────────────────────
# codex R2 지적: soft 로더가 **실제 전체 순회**에 일관되게 연결되지 않았다 — YAML 스칼라·리스트
# frontmatter(파싱은 성공하나 dict 가 아님)는 자체 except 목록을 통과해 뒤따르는 `fm.get` 에서
# AttributeError 로 죽고, 기본 `list` 가 그 경로(`_distinct_ticket_users`)를 **먼저** 부른다.
# 아래는 그 형상 그대로 재현하고, 순회 소비자들이 공용 로더로 접히는지 본다.

_NON_MAPPING_FRONTMATTER = "---\n- 리스트\n- 형상\n---\n# 본문\n"
_SCALAR_FRONTMATTER = "---\n그냥 스칼라 한 줄\n---\n# 본문\n"


def _claimed_ticket_text(tid: str, *, claimed_by: str) -> str:
    return (f"---\nid: {tid}\ntitle: 정상 티켓\nstatus: claimed\n"
            f"created_by: tester/{claimed_by}\nclaimed_by: tester/{claimed_by}\n"
            "depends_on: []\n---\n## 목표\n실값\n\n## 완료 조건\n- [x] 끝\n\n## 참고\n- 없음\n")


@pytest.mark.parametrize("text, shape", [
    (_NON_MAPPING_FRONTMATTER, "리스트"),
    (_SCALAR_FRONTMATTER, "스칼라"),
])
def test_distinct_ticket_users_skips_non_mapping_frontmatter(
        board, monkeypatch, tmp_path, capsys, text, shape):
    """재현: 비-dict frontmatter 는 파싱에 성공해 옛 except 목록을 통과했다 — `fm.get` 이 터졌다."""
    _wire_repo(board, monkeypatch, tmp_path)
    _ticket(board, "open", "T-0002", text)
    _ticket(board, "claimed", "T-0001", _claimed_ticket_text("T-0001", claimed_by="pm_1"))

    assert board._distinct_ticket_users() == 1, f"{shape} 형상에서 사용자 신호가 깨졌다"
    assert "T-0002" in capsys.readouterr().err       # 조용한 드롭이 아니다


def test_default_list_view_survives_a_non_mapping_frontmatter_ticket(
        board, monkeypatch, tmp_path, capsys):
    """**기본 `list`** e2e — 그 경로가 먼저 부르는 사용자 신호 산정에서 죽지 않는다 (traceback 0).

    무인자 `list`(세션 뷰)는 `_distinct_ticket_users` 를 먼저 부른다 — 비-dict 티켓 1건이 거기서
    터지면 보드 조회 자체가 불능이 된다(`--all` 경로는 그 산정을 지나지 않아 재현되지 않는다)."""
    _wire_repo(board, monkeypatch, tmp_path)
    pm = tmp_path / ".project_manager"
    (pm / ".local").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(board, "LOCAL_CONF", pm / "local.conf")
    monkeypatch.setattr(board, "AREAS_FILE", pm / "areas.md")
    monkeypatch.setattr(board, "LEASES_FILE", pm / ".local" / "worktree-leases.json")
    (pm / "local.conf").write_text("session=pm_1\nuser=tester\n", encoding="utf-8")
    monkeypatch.setattr(board, "_git_config_email", lambda: None)
    _ticket(board, "open", "T-0002", _NON_MAPPING_FRONTMATTER)
    _ticket(board, "open", "T-0001", _healthy_ticket_text("T-0001"))

    rc = board.cmd_list(SimpleNamespace(
        status=None, tag=None, mine=False, all=False, task=None, repo=None, slot=None))

    out, err = capsys.readouterr()
    assert rc == 0
    assert "T-0002" not in out
    assert "T-0002" in err


def test_scan_task_tickets_skips_broken_and_non_mapping(board, monkeypatch, tmp_path, capsys):
    """`scan_task_tickets`(task end 소진 게이트)도 같은 공용 로더를 탄다 — 정상분은 그대로 모은다."""
    _wire_repo(board, monkeypatch, tmp_path)
    _ticket(board, "claimed", "T-0003", _NON_MAPPING_FRONTMATTER)
    _ticket(board, "claimed", "T-0002", _BROKEN_FRONTMATTER)
    _ticket(board, "claimed", "T-0001", _claimed_ticket_text("T-0001", claimed_by="wave-x"))

    scanned = board.scan_task_tickets("tester", "wave-x")

    assert [row["id"] for row in scanned["claimed"]] == ["T-0001"]
    err = capsys.readouterr().err
    assert "T-0002" in err and "T-0003" in err        # 두 손상 형상 모두 표면화


# 티켓 **순회**에서 strict 로더를 그대로 쓰는 것이 정당한 지점 — 지정 대상 mutation 은 fail-loud
# 다(T-0601 결정). migrate-identity 는 스캔한 티켓을 *고치는* op 이라, 못 읽은 티켓을 건너뛰면
# 그 티켓만 마이그레이션에서 조용히 빠진다.
_STRICT_TICKET_SCAN_FUNCTIONS = frozenset({
    "_migrate_identity_preview", "_migrate_tickets_apply",
})


def test_ticket_iteration_consumers_use_the_shared_soft_loader(board):
    """사본 0 — `T-*.md` 를 순회하면서 strict `load_ticket` 을 직접 부르는 함수가 남으면 red.

    사본은 이번 클래스(비-dict frontmatter 미포섭·자체 except 목록)를 그대로 되살린다. mutation
    op 만 예외이고 그 목록은 위 상수가 소유한다(무언의 예외 금지)."""
    import ast

    source = (TOOLS / "board.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in _STRICT_TICKET_SCAN_FUNCTIONS:
            continue
        body = ast.get_source_segment(source, node) or ""
        if 'glob("T-*.md")' not in body:
            continue
        calls = {
            call.func.id for call in ast.walk(node)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
        }
        if "load_ticket" in calls:
            offenders.append(f"{node.name}:{node.lineno}")
    assert not offenders, f"티켓 순회가 공용 soft 로더를 안 쓴다: {offenders}"

def test_fallback_lookup_warns_when_it_answers_with_a_different_ticket(
    tmp_path, monkeypatch, capsys
):
    """정확 후보가 없을 때의 폴백 반환은 무음이 아니다 — canonical 불일치를 stderr 로 알린다."""
    board = _load_board()

    tickets = tmp_path / "tickets"
    (tickets / "open").mkdir(parents=True)
    (tickets / "open" / "T-7777-001-other.md").write_text(
        "---\nid: T-7777-001\ntitle: t\nstatus: open\n---\n\n# T-7777-001\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(board, "tickets_dir", lambda: tickets)
    monkeypatch.setattr(board, "drafts_dir", lambda: tickets / ".drafts")
    status, path = board.find_ticket("T-7777")
    assert path.name == "T-7777-001-other.md"
    err = capsys.readouterr().err
    assert "정확 일치 티켓 없음" in err and "T-7777-001" in err


# ── 라운드 사이드카 lint (advisory) + 구 역할 절 잔존 (blocking) ──────────────
#
# 라운드 판정의 단일 진실은 사이드카 seam 이고 board 는 그것을 lint 표면으로만 올린다
# (차단 소비자는 완료 게이트). 반대로 명세 본문에 남은 구 역할 절은 마이그레이션 명령 1회로
# 해소되는 이행 잔재라 blocking 이다.

def _lint_ticket(board, status: str, tid: str, *, body: str = "") -> Path:
    directory = board.tickets_dir() / status
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{tid}-fixture.md"
    path.write_text(
        "---\n"
        f"id: {tid}\n"
        "title: 픽스처\n"
        f"status: {'open' if status == '.drafts' else status}\n"
        "created: '2026-01-02'\n"
        "created_by: t\n"
        "claimed_by: null\n"
        "depends_on: []\n"
        "blocks: []\n"
        "touches: []\n"
        "estimate: small\n"
        "tags: []\n"
        "---\n\n"
        f"# {tid} — 픽스처\n\n## 목표\n판정 입력.\n\n"
        "## 완료 조건 (Definition of Done)\n- [x] 없음\n\n## 참고\n- 없음\n"
        + body,
        encoding="utf-8",
    )
    return path


def _lint_round(board, tid: str, name: str, text: str) -> Path:
    path = board._load_ticket_rounds().rounds_dir_for_ticket(tid, board.tickets_dir()) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")
    return path


def test_round_problem_kinds_are_the_seam_codes_and_never_block(board):
    """lint kind = seam 판정 코드 그대로 · 전부 advisory (차단은 완료 게이트 하나)."""
    rounds = board._load_ticket_rounds()
    for code in (rounds.PROBLEM_NAME, rounds.PROBLEM_GAP,
                 rounds.PROBLEM_DUPLICATE, rounds.PROBLEM_PENDING):
        assert code in board._ADVISORY_LINT_KINDS, code
    for kind in (board._ROUND_TEMPORARY_LINT_KIND, board._ROUND_STRAY_LINT_KIND,
                 board._ROUND_UNREADABLE_LINT_KIND):
        assert kind in board._ADVISORY_LINT_KINDS, kind


def test_lint_rounds_surfaces_gap_and_pending(board, monkeypatch, tmp_path):
    _wire_repo(board, monkeypatch, tmp_path)
    path = _lint_ticket(board, "claimed", "T-3001")
    rounds = board._load_ticket_rounds()
    _lint_round(board, "T-3001", "01-developer.md",
                "## 구현 보충 (developer · 2026-01-02)\n\n실제 산출.\n")
    _lint_round(board, "T-3001", "03-code-reviewer.md",
                rounds.render_round_seed(
                    "code-reviewer", path.read_text(encoding="utf-8"), today="2026-01-02"))

    issues = board.lint_rounds()

    kinds = {kind for _name, kind, _detail in issues}
    assert kinds == {rounds.PROBLEM_GAP, rounds.PROBLEM_PENDING}, issues
    assert all(name == "T-3001" for name, _kind, _detail in issues)
    assert any("02" in detail for _n, kind, detail in issues if kind == rounds.PROBLEM_GAP)


def test_lint_rounds_reports_engine_temporary_leftovers(board, monkeypatch, tmp_path):
    """점-접두 잔여(교체 중 크래시)는 라운드가 아니므로 seam 판정 밖이다 — 여기서 보이게 한다."""
    _wire_repo(board, monkeypatch, tmp_path)
    _lint_ticket(board, "claimed", "T-3002")
    rounds = board._load_ticket_rounds()
    _lint_round(board, "T-3002", "01-developer.md",
                "## 구현 보충 (developer · 2026-01-02)\n\n실제 산출.\n")
    leftover = (f"{rounds.ROUND_TEMPORARY_PREFIX}01-developer.md.1234.deadbeef"
                f"{rounds.ROUND_TEMPORARY_SUFFIX}")
    _lint_round(board, "T-3002", leftover, "부분 쓰기\n")

    issues = board.lint_rounds()

    assert [(name, kind) for name, kind, _detail in issues] == [
        ("T-3002", board._ROUND_TEMPORARY_LINT_KIND)]
    assert leftover in issues[0][2]


def test_lint_rounds_is_clean_for_a_consistent_board(board, monkeypatch, tmp_path):
    _wire_repo(board, monkeypatch, tmp_path)
    _lint_ticket(board, "claimed", "T-3003")
    _lint_round(board, "T-3003", "01-developer.md",
                "## 구현 보충 (developer · 2026-01-02)\n\n실제 산출.\n")
    _lint_round(board, "T-3003", "02-code-reviewer.md",
                "## 리뷰 (code-reviewer · 2026-01-02)\n\n실제 리뷰.\n")

    assert board.lint_rounds() == []


def test_lint_rounds_without_a_rounds_directory_is_empty(board, monkeypatch, tmp_path):
    _wire_repo(board, monkeypatch, tmp_path)
    _lint_ticket(board, "open", "T-3004")
    assert board.lint_rounds() == []


def test_lint_rounds_keeps_reporting_a_round_without_output_after_a_sibling_lands(
    board, monkeypatch, tmp_path,
):
    """같은 역할 병렬 2라운드 — 01 회수가 02 의 미회수 판정을 지우지 않는다."""
    _wire_repo(board, monkeypatch, tmp_path)
    path = _lint_ticket(board, "claimed", "T-3011")
    rounds = board._load_ticket_rounds()
    spec_text = path.read_text(encoding="utf-8")
    block = json.dumps({
        "version": 2,
        "findings": [{
            "id": "F-001", "class": "implementation-defect", "severity": "must-fix",
            "authority": "설계 §경계", "evidence": "probe rc=1",
            "recommendation": "F-001 수정", "design_change": False,
        }],
        "confirmations": [],
    }, ensure_ascii=False)
    _lint_round(
        board, "T-3011", "01-code-reviewer.md",
        "## 리뷰 (code-reviewer · 2026-01-03)\n\n## must-fix\n- F-001\n\n"
        "## 판정\n판정: 반려 · finding 1건(must-fix 1건)\n\n"
        f"```pm-review-v1\n{block}\n```\n",
    )
    _lint_round(
        board, "T-3011", "02-code-reviewer.md",
        rounds.render_round_seed("code-reviewer", spec_text, today="2026-01-02"),
    )

    issues = board.lint_rounds()

    assert [(name, kind) for name, kind, _detail in issues] == [
        ("T-3011", rounds.PROBLEM_PENDING)]
    assert "02-code-reviewer.md" in issues[0][2]


def test_lint_rounds_surfaces_a_file_sitting_where_a_ticket_directory_belongs(
    board, monkeypatch, tmp_path,
):
    """`tickets/rounds/` 직계는 티켓별 디렉터리 자리다 — 파일 항목은 잔여로 보인다."""
    _wire_repo(board, monkeypatch, tmp_path)
    _lint_ticket(board, "claimed", "T-3012")
    _lint_round(board, "T-3012", "01-developer.md",
                "## 구현 보충 (developer · 2026-01-02)\n\n실제 산출.\n")
    stray = board._load_ticket_rounds().rounds_dir(board.tickets_dir()) / "NOTES.md"
    stray.write_text("라운드 디렉터리가 아니다\n", encoding="utf-8")

    issues = board.lint_rounds()

    assert [(name, kind) for name, kind, _detail in issues] == [
        ("NOTES.md", board._ROUND_STRAY_LINT_KIND)]
    assert "NOTES.md" in issues[0][2]


def test_lint_rounds_fail_soft_boundary_is_one_ticket(board, monkeypatch, tmp_path):
    """한 티켓의 판정 실패가 다른 티켓의 판정을 통째로 버리지 않는다."""
    _wire_repo(board, monkeypatch, tmp_path)
    path = _lint_ticket(board, "claimed", "T-3013")
    rounds = board._load_ticket_rounds()
    _lint_round(board, "T-3013", "01-developer.md",
                rounds.render_round_seed(
                    "developer", path.read_text(encoding="utf-8"), today="2026-01-02"))
    # 판정 자체가 불가능한 이름(플랫폼 예약 장치명) — seam 이 loud 하게 거부한다.
    unreadable = rounds.rounds_dir(board.tickets_dir()) / "CON"
    unreadable.mkdir(parents=True, exist_ok=True)

    issues = board.lint_rounds()

    assert sorted((name, kind) for name, kind, _detail in issues) == [
        ("CON", board._ROUND_UNREADABLE_LINT_KIND),
        ("T-3013", rounds.PROBLEM_PENDING),
    ]


_LEGACY_SECTION = (
    "\n<!-- pm-ticket-section:start role=developer -->\n"
    "## 구현 보충 (developer · 2026-01-02)\n\n옛 컨테이너 산출.\n"
    "<!-- pm-ticket-section:end role=developer -->\n"
)


def test_legacy_growth_section_blocks_and_names_the_migration(board, monkeypatch, tmp_path):
    """구 역할 절이 남은 티켓은 red — 처방(마이그레이션 명령 1회)을 문구에 담는다."""
    _wire_repo(board, monkeypatch, tmp_path)
    _lint_ticket(board, "done", "T-3005", body=_LEGACY_SECTION)

    issues = board.lint_legacy_growth_sections()

    assert [(name, kind) for name, kind, _detail in issues] == [
        ("T-3005", "legacy-growth-section")]
    assert "legacy-growth-section" not in board._ADVISORY_LINT_KINDS, "차단 축이어야 한다"
    assert "rounds migrate" in issues[0][2]


def test_legacy_growth_section_covers_every_status_and_drafts(board, monkeypatch, tmp_path):
    """변환 대상은 완료 티켓에 몰려 있다 — done 과 draft 도 순회한다."""
    _wire_repo(board, monkeypatch, tmp_path)
    _lint_ticket(board, "done", "T-3006", body=_LEGACY_SECTION)
    _lint_ticket(board, ".drafts", "T-3007", body=_LEGACY_SECTION)
    _lint_ticket(board, "open", "T-3008")

    names = sorted(name for name, _kind, _detail in board.lint_legacy_growth_sections())
    assert names == ["T-3006", "T-3007"]


def test_legacy_seal_comment_alone_is_enough_to_flag(board, monkeypatch, tmp_path):
    """marker 를 손으로 지우고 봉인 주석만 남긴 형상도 미변환이다."""
    _wire_repo(board, monkeypatch, tmp_path)
    _lint_ticket(board, "done", "T-3009",
                 body="\n<!-- pm-ticket-seal role=developer ordinal=0 sha256=x by=harvest -->\n")

    assert [kind for _n, kind, _d in board.lint_legacy_growth_sections()] == [
        "legacy-growth-section"]


def test_legacy_growth_section_ignores_fenced_syntax_examples(board, monkeypatch, tmp_path):
    """``` 로 감싼 marker 문법 예시(문서화 티켓)는 변환 대상이 아니라 lint 도 red 를 안 낸다."""
    _wire_repo(board, monkeypatch, tmp_path)
    fenced = "\n```\n" + _LEGACY_SECTION.strip("\n") + "\n```\n"
    _lint_ticket(board, "done", "T-3011", body=fenced)

    assert board.lint_legacy_growth_sections() == []


def test_migrated_board_has_no_legacy_findings(board, monkeypatch, tmp_path):
    _wire_repo(board, monkeypatch, tmp_path)
    _lint_ticket(board, "done", "T-3010")
    _lint_round(board, "T-3010", "01-developer.md",
                "## 구현 보충 (developer · 2026-01-02)\n\n실제 산출.\n")

    assert board.lint_legacy_growth_sections() == []
    assert board.lint_rounds() == []


# ════════════════════════════════════════════════════════════════════════
# lint_claim_identity — open + claimed_by 모순 advisory (T-0783)
# ════════════════════════════════════════════════════════════════════════
# 옛 `cmd_unblock`(T-0783 이전)이 claimed_by 를 무접촉으로 둔 채 무조건 open 으로 옮기던 결함의
# 잔재를 가시화한다(I1: status=open 인 티켓은 claimed_by/claimed_at/claimed_rev 전부 null).
# `blocked` + claimed_by 는 (a)안(claimed-origin blocked 의 정상 형상)이라 대상이 **아니다**.

def _open_with_claimed_by_text(tid: str, *, claimed_by: str) -> str:
    return (f"---\nid: {tid}\ntitle: 모순 티켓\nstatus: open\n"
            f"claimed_by: {claimed_by}\nclaimed_at: '2026-08-01T00:00:00+00:00'\n"
            "depends_on: []\n---\n## 목표\n실값\n\n## 완료 조건\n- [x] 끝\n\n## 참고\n- 없음\n")


def _blocked_with_claimed_by_text(tid: str, *, claimed_by: str) -> str:
    return (f"---\nid: {tid}\ntitle: 정상 blocked 티켓(소유 보유)\nstatus: blocked\n"
            f"claimed_by: {claimed_by}\nclaimed_at: '2026-08-01T00:00:00+00:00'\n"
            "depends_on: []\n---\n## 목표\n실값\n\n## 완료 조건\n- [x] 끝\n\n## 참고\n- 없음\n")


def _blocked_without_claimed_by_text(tid: str) -> str:
    return (f"---\nid: {tid}\ntitle: 정상 blocked 티켓(무소유)\nstatus: blocked\n"
            "depends_on: []\n---\n## 목표\n실값\n\n## 완료 조건\n- [x] 끝\n\n## 참고\n- 없음\n")


def test_open_claimed_contradiction_kind_is_advisory_never_block(board):
    """kind 등록 확인 — `--gate` 종료코드에 기여하지 않는다."""
    assert "open-claimed-contradiction" in board._ADVISORY_LINT_KINDS


def test_lint_claim_identity_flags_open_with_claimed_by(board, monkeypatch, tmp_path):
    """open + claimed_by 잔존 형상 1건을 정확히 잡는다."""
    _wire_repo(board, monkeypatch, tmp_path)
    _ticket(board, "open", "T-0001",
            _open_with_claimed_by_text("T-0001", claimed_by="alice/pm-1"))

    issues = board.lint_claim_identity()
    assert [(tid, kind) for tid, kind, _detail in issues] == [
        ("T-0001", "open-claimed-contradiction")]


def test_lint_claim_identity_no_false_positive_on_healthy_shapes(board, monkeypatch, tmp_path):
    """정상 형상(open+null·claimed+set·blocked+set·blocked+null) 전부 오탐 0
    (adopter#0 실측 대상 0건 재현 — DoD)."""
    _wire_repo(board, monkeypatch, tmp_path)
    _ticket(board, "open", "T-0001", _healthy_ticket_text("T-0001"))
    _ticket(board, "claimed", "T-0002", _claimed_ticket_text("T-0002", claimed_by="pm_1"))
    _ticket(board, "blocked", "T-0003",
            _blocked_with_claimed_by_text("T-0003", claimed_by="alice/pm-1"))
    _ticket(board, "blocked", "T-0004", _blocked_without_claimed_by_text("T-0004"))

    assert board.lint_claim_identity() == []


def test_lint_tickets_surfaces_claim_identity_contradiction(board, monkeypatch, tmp_path):
    """전체 집계 `lint_tickets()` 도 이 판정을 포함한다."""
    _wire_repo(board, monkeypatch, tmp_path)
    _ticket(board, "open", "T-0001",
            _open_with_claimed_by_text("T-0001", claimed_by="alice/pm-1"))

    kinds = {kind for _tid, kind, _detail in board.lint_tickets()}
    assert "open-claimed-contradiction" in kinds


# ── 필드별 단독 잔존 (F-002 · I1 세 필드 전부 검사) ──────────────────────────
# claimed_by 만 잔존하는 형상은 위 `_open_with_claimed_by_text` 가 이미 커버하지만 claimed_at
# 도 같이 채워 시나리오를 겸한다 — 여기 셋은 **한 필드만** 남기고 나머지 둘은 완전히 비워
# "그 필드 하나만으로도 걸리는가"를 각각 독립 격리해 확인한다.

def _open_claimed_by_only_text(tid: str, *, claimed_by: str) -> str:
    return (f"---\nid: {tid}\ntitle: claimed_by 단독 잔존\nstatus: open\n"
            f"claimed_by: {claimed_by}\nclaimed_at: null\ndepends_on: []\n---\n"
            "## 목표\n실값\n\n## 완료 조건\n- [x] 끝\n\n## 참고\n- 없음\n")


def _open_claimed_at_only_text(tid: str, *, claimed_at: str) -> str:
    return (f"---\nid: {tid}\ntitle: claimed_at 단독 잔존\nstatus: open\n"
            f"claimed_by: null\nclaimed_at: '{claimed_at}'\ndepends_on: []\n---\n"
            "## 목표\n실값\n\n## 완료 조건\n- [x] 끝\n\n## 참고\n- 없음\n")


def _open_claimed_rev_only_text(tid: str, *, claimed_rev: str) -> str:
    return (f"---\nid: {tid}\ntitle: claimed_rev 단독 잔존\nstatus: open\n"
            f"claimed_by: null\nclaimed_rev: {claimed_rev}\ndepends_on: []\n---\n"
            "## 목표\n실값\n\n## 완료 조건\n- [x] 끝\n\n## 참고\n- 없음\n")


def test_lint_claim_identity_flags_claimed_by_alone(board, monkeypatch, tmp_path):
    """claimed_by 만 잔존(claimed_at/claimed_rev 없음)해도 잡는다 — detail 에 필드명이 보인다."""
    _wire_repo(board, monkeypatch, tmp_path)
    _ticket(board, "open", "T-0001",
            _open_claimed_by_only_text("T-0001", claimed_by="alice/pm-1"))

    issues = board.lint_claim_identity()
    assert [(tid, kind) for tid, kind, _detail in issues] == [
        ("T-0001", "open-claimed-contradiction")]
    assert "claimed_by" in issues[0][2]


def test_lint_claim_identity_flags_claimed_at_alone(board, monkeypatch, tmp_path):
    """claimed_at 만 잔존(claimed_by 없음)해도 잡는다 — F-002 이전엔 [] 였다(reviewer 관측 재현)."""
    _wire_repo(board, monkeypatch, tmp_path)
    _ticket(board, "open", "T-0001",
            _open_claimed_at_only_text("T-0001", claimed_at="2026-08-01T00:00:00+00:00"))

    issues = board.lint_claim_identity()
    assert [(tid, kind) for tid, kind, _detail in issues] == [
        ("T-0001", "open-claimed-contradiction")]
    assert "claimed_at" in issues[0][2]


def test_lint_claim_identity_flags_claimed_rev_alone(board, monkeypatch, tmp_path):
    """claimed_rev 만 잔존(claimed_by 없음)해도 잡는다 — F-002 이전엔 [] 였다(reviewer 관측 재현)."""
    _wire_repo(board, monkeypatch, tmp_path)
    _ticket(board, "open", "T-0001",
            _open_claimed_rev_only_text("T-0001", claimed_rev="abc123"))

    issues = board.lint_claim_identity()
    assert [(tid, kind) for tid, kind, _detail in issues] == [
        ("T-0001", "open-claimed-contradiction")]
    assert "claimed_rev" in issues[0][2]


def test_lint_claim_identity_kind_stays_advisory_after_widening(board):
    """F-002 로 판정을 넓혀도 kind 는 그대로 advisory(비차단) — 차단으로 승격되지 않았다."""
    assert "open-claimed-contradiction" in board._ADVISORY_LINT_KINDS


# ── F-001 (라운드 6) — 실제 open 티켓 frontmatter 형상 전용 픽스처 ────────────
# `_healthy_ticket_text`(공유 헬퍼·4개 다른 테스트가 exact-equality 로 의존)는 건드리지 않는다
# (라운드 5 빈틈 보고 수용 — PM 대안 1 채택). 대신 실 board(`.project_manager/board/tickets/
# open/`) 의 실제 frontmatter 형상을 그대로 재현한 **전용** 픽스처를 쓴다 — claimed_by/
# claimed_at/completed_at 을 값으로 명시(null)해, "필드 부재"가 아니라 "필드가 null 값으로
# 존재"하는 정상 open 티켓에서도 오탐 0 임을 값 단언으로 잠근다.

def _realistic_open_ticket_text(tid: str) -> str:
    return (f"---\nid: {tid}\ntitle: 실측 형상 정상 티켓\nstatus: open\n"
            "created: '2026-08-01'\ncreated_by: tester/pm-1\n"
            "claimed_by: null\nclaimed_at: null\ncompleted_at: null\n"
            "depends_on: []\nblocks: []\ntouches: []\nestimate: small\ndesign: n/a\ntags: []\n"
            "---\n## 목표\n실값\n\n## 완료 조건\n- [x] 끝\n\n## 참고\n- 없음\n")


def test_lint_claim_identity_no_false_positive_on_realistic_open_ticket_shape(
        board, monkeypatch, tmp_path):
    """실 board 의 open 티켓 frontmatter 형상(claimed_by/claimed_at/completed_at 이 **값으로**
    null)에서 오탐 0 — `_healthy_ticket_text`(필드 부재)와 달리 필드가 명시적으로 존재해도
    값이 null 이면 걸리지 않는다는 것을 값 단언으로 잠근다(F-001)."""
    _wire_repo(board, monkeypatch, tmp_path)
    _ticket(board, "open", "T-0001", _realistic_open_ticket_text("T-0001"))

    assert board.lint_claim_identity() == []


def test_lint_claim_identity_still_flags_contradiction_alongside_realistic_healthy_ticket(
        board, monkeypatch, tmp_path):
    """역방향 확인 — 새 오탐-0 픽스처가 실제 정탐(claimed_by 잔존)까지 함께 삼켜버리지 않는다.
    같은 스캔에 실측 형상 정상 티켓과 모순 티켓을 같이 두고, 모순 티켓만 정확히 잡히는지 본다."""
    _wire_repo(board, monkeypatch, tmp_path)
    _ticket(board, "open", "T-0001", _realistic_open_ticket_text("T-0001"))
    _ticket(board, "open", "T-0002",
            _open_with_claimed_by_text("T-0002", claimed_by="alice/pm-1"))

    issues = board.lint_claim_identity()
    assert [(tid, kind) for tid, kind, _detail in issues] == [
        ("T-0002", "open-claimed-contradiction")]
