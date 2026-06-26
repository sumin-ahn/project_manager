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
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
BOARD_PY = TOOLS / "board.py"


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
    ])
    # --gate: domain 은 종료코드에 기여 안 함 → 0.
    assert board.cmd_lint(SimpleNamespace(gate=True)) == 0
    # 무인자(full): 같은 입력에서 1 (보고는 함·현행 계약).
    assert board.cmd_lint(SimpleNamespace(gate=False)) == 1


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


# ── architecture.md freshness lint (T-0101·ADR-0022·advisory) ─────────────────

def _write_adr_dated(decisions_dir, num, *, created, updated=None):
    """hermetic ADR md fixture — date frontmatter 만 의미 있음 (unquoted yaml date)."""
    fm = ["title: t", "type: decision", "status: accepted", f"created: {created}"]
    if updated is not None:
        fm.append(f"updated: {updated}")
    (decisions_dir / f"{num}-slug.md").write_text(
        "---\n" + "\n".join(fm) + "\n---\n\n# ADR-" + num + "\n\nbody\n",
        encoding="utf-8",
    )


def _write_architecture(arch_file, *, updated):
    """hermetic architecture.md fixture — frontmatter updated 만 의미 있음."""
    arch_file.write_text(
        f"---\ntitle: Architecture\ntype: architecture\nupdated: {updated}\n---\n\n# Architecture\n",
        encoding="utf-8",
    )


@pytest.fixture
def arch_wiring(board, monkeypatch, tmp_path):
    """decisions/ + architecture.md 를 tmp 로 monkeypatch. (decisions_dir, arch_file) 반환."""
    d = tmp_path / "decisions"
    d.mkdir()
    arch = tmp_path / "architecture.md"
    monkeypatch.setattr(board, "DECISIONS_DIR", d)
    monkeypatch.setattr(board, "ARCHITECTURE_FILE", arch)
    return d, arch


def test_architecture_freshness_flags_newer_adr(board, arch_wiring):
    # ① 최신 ADR date > architecture updated → finding 1.
    decisions_dir, arch = arch_wiring
    _write_architecture(arch, updated="2026-06-10")
    _write_adr_dated(decisions_dir, "0001", created="2026-06-05")
    _write_adr_dated(decisions_dir, "0002", created="2026-06-15")  # 최신·arch 보다 뒤
    findings = board.lint_architecture_freshness()
    assert len(findings) == 1
    label, kind, detail = findings[0]
    assert label == "architecture.md"
    assert kind == "architecture-stale"
    assert "ADR-0002" in detail and "2026-06-15" in detail and "2026-06-10" in detail


def test_architecture_freshness_uses_updated_over_created(board, arch_wiring):
    # updated 우선 — created 는 옛날이어도 updated 가 최신이면 그 date 로 비교.
    decisions_dir, arch = arch_wiring
    _write_architecture(arch, updated="2026-06-10")
    _write_adr_dated(decisions_dir, "0001", created="2026-06-01", updated="2026-06-20")
    findings = board.lint_architecture_freshness()
    assert len(findings) == 1
    assert "2026-06-20" in findings[0][2]


def test_architecture_freshness_clean_when_architecture_newer(board, arch_wiring):
    # ② architecture 가 최신(또는 동일) → finding 0.
    decisions_dir, arch = arch_wiring
    _write_architecture(arch, updated="2026-06-20")
    _write_adr_dated(decisions_dir, "0001", created="2026-06-05")
    _write_adr_dated(decisions_dir, "0002", created="2026-06-20")  # 동일 date → not >
    assert board.lint_architecture_freshness() == []


def test_architecture_freshness_graceful_when_architecture_absent(board, arch_wiring):
    # ③ architecture.md 부재 → graceful [] (fail-soft).
    decisions_dir, arch = arch_wiring  # arch 파일은 만들지 않음
    _write_adr_dated(decisions_dir, "0001", created="2026-06-15")
    assert not arch.exists()
    assert board.lint_architecture_freshness() == []


def test_architecture_freshness_graceful_no_frontmatter(board, arch_wiring):
    # architecture.md 가 frontmatter 없음 → graceful [].
    decisions_dir, arch = arch_wiring
    arch.write_text("# Architecture\n\nno frontmatter\n", encoding="utf-8")
    _write_adr_dated(decisions_dir, "0001", created="2026-06-15")
    assert board.lint_architecture_freshness() == []


def test_architecture_freshness_graceful_unparseable_date(board, arch_wiring):
    # architecture updated 가 date 로 파싱 불가 → graceful [] (비교 불가).
    decisions_dir, arch = arch_wiring
    arch.write_text(
        "---\ntitle: Architecture\nupdated: 'not-a-date'\n---\n\n# Architecture\n",
        encoding="utf-8",
    )
    _write_adr_dated(decisions_dir, "0001", created="2026-06-15")
    assert board.lint_architecture_freshness() == []


def test_architecture_freshness_skips_non_adr_files(board, arch_wiring):
    # decisions/README.md 류 비-ADR(NNNN-slug 아님) 파일은 글롭으로 제외.
    decisions_dir, arch = arch_wiring
    _write_architecture(arch, updated="2026-06-10")
    # README.md 가 미래 date 여도 NNNN-slug 글롭에 안 잡혀 무시.
    (decisions_dir / "README.md").write_text(
        "---\ntitle: r\nupdated: 2026-12-31\n---\n\n# README\n", encoding="utf-8")
    _write_adr_dated(decisions_dir, "0001", created="2026-06-05")  # arch 보다 이전
    assert board.lint_architecture_freshness() == []


def test_architecture_freshness_graceful_no_decisions(board, monkeypatch, tmp_path):
    # decisions/ 부재 → [] (솔로/신규 clone 무영향).
    arch = tmp_path / "architecture.md"
    _write_architecture(arch, updated="2026-06-10")
    monkeypatch.setattr(board, "DECISIONS_DIR", tmp_path / "nope")
    monkeypatch.setattr(board, "ARCHITECTURE_FILE", arch)
    assert board.lint_architecture_freshness() == []


def test_architecture_freshness_in_advisory_kinds(board):
    # architecture-stale 은 advisory — --gate 종료코드 비기여.
    assert "architecture-stale" in board._ADVISORY_LINT_KINDS


def test_architecture_freshness_is_advisory_never_blocks(board, monkeypatch, tmp_path):
    # ④ architecture-stale finding 만 있으면 --gate 종료코드 0 (never-block) sensitivity.
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
    monkeypatch.setattr(board, "_run_lint_hooks", lambda: [])
    monkeypatch.setattr(board, "lint_architecture_freshness", lambda: [
        ("architecture.md", "architecture-stale", "최신 ADR > architecture updated")])
    # advisory: --gate 는 0(차단 0), 무인자는 finding 표면화로 1.
    assert board.cmd_lint(SimpleNamespace(gate=True)) == 0
    assert board.cmd_lint(SimpleNamespace(gate=False)) == 1


def test_lint_tickets_includes_architecture_freshness(board, monkeypatch):
    # lint_tickets 통합 — architecture freshness finding 이 전체 보고에 포함된다.
    sentinel = [("architecture.md", "architecture-stale", "sentinel")]
    monkeypatch.setattr(board, "lint_dependencies", lambda: [])
    monkeypatch.setattr(board, "lint_bodies", lambda: [])
    monkeypatch.setattr(board, "lint_ideas", lambda: [])
    monkeypatch.setattr(board, "lint_status", lambda: [])
    monkeypatch.setattr(board, "lint_wikilinks", lambda: [])
    monkeypatch.setattr(board, "lint_unstable_refs", lambda: [])
    monkeypatch.setattr(board, "lint_scopes", lambda: [])
    monkeypatch.setattr(board, "lint_domain", lambda: [])
    monkeypatch.setattr(board, "lint_adr_lifecycle", lambda: [])
    monkeypatch.setattr(board, "lint_architecture_freshness", lambda: sentinel)
    assert sentinel[0] in board.lint_tickets()


# ── un-migrated overlay 검출 (advisory · T-0132·§3.6·ADR-0031) ─────────────
# 어댑터 .md 에 리터럴 free-form 토큰(`_UNMIGRATED_FREEFORM_KEYS` 로컬 튜플·ADR-0031 디커플)
# 잔존 = canonical home(root doc·pm_role.local.md) 마이그레이션 미완 신호. advisory
# (`_ADVISORY_LINT_KINDS`·`--gate` 미차단). operational 토큰·code-fence 예시는 검사 제외
# (오탐 0). 어댑터 부재 tree finding 0(graceful). overlay 파일 부재 조건은 ADR-0031 로 제거됐다
# — free-form value-fill 기계(overlay.local.yaml)가 없어졌으므로 리터럴 토큰 잔존만으로 advisory.

def _adapter_doc(root: Path, relpath: str, text: str) -> Path:
    """root 아래 어댑터 스캐폴드 .md 를 만든다 (예: .claude/agents/developer.md)."""
    p = root / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
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


def test_lint_tickets_includes_unmigrated_overlay(board, monkeypatch):
    """lint_tickets 통합 — un-migrated finding 이 전체 보고에 포함된다."""
    sentinel = [(".claude/agents/x.md", "un-migrated-overlay", "sentinel")]
    for fn in ("lint_dependencies", "lint_bodies", "lint_ideas", "lint_status",
               "lint_wikilinks", "lint_unstable_refs", "lint_scopes",
               "lint_domain", "lint_adr_lifecycle", "lint_architecture_freshness",
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


def test_adapter_drift_graceful_when_seen_unrecorded(board, monkeypatch):
    # seen(`upstream_seen_rev`) 미기록(cache 부재 URL·pm-update 미실행) → graceful 0
    # (관찰값 없으면 비교 불가 → flood 회피·flag 안 함).
    _wire_conf(board, monkeypatch, {
        "upstream": "https://github.com/example/project_manager",
        "upstream_rev": "aaaaaaaaaaaa1111",
    })
    assert board.lint_adapter_drift() == []


def test_adapter_drift_graceful_when_conf_empty(board, monkeypatch):
    # local.conf 부재(빈 dict·솔로/신규 clone) → graceful 0.
    _wire_conf(board, monkeypatch, {})
    assert board.lint_adapter_drift() == []


def test_adapter_drift_blank_values_treated_as_absent(board, monkeypatch):
    # 키는 있으나 빈 값(`upstream_seen_rev=`) → 미기록과 동치 → graceful 0.
    _wire_conf(board, monkeypatch, {
        "upstream": "https://github.com/example/project_manager",
        "upstream_rev": "aaaaaaaaaaaa1111",
        "upstream_seen_rev": "   ",
    })
    assert board.lint_adapter_drift() == []


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
