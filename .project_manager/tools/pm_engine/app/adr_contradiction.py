"""결정 개정 잔여 모순 lint (층: app).

개정 대상 ADR 을 wikilink 로 참조하는 문서를 모으고, 발췌와 LLM 프롬프트·후보 판독·advisory 렌더를 소유한다.
기본은 LLM 을 부르지 않는다.
"""
from __future__ import annotations

import re
from pathlib import Path
from collections.abc import Callable, Iterable
from typing import NamedTuple

from pm_engine.infra import fs
from pm_engine.app import wikilink_lint


# ADR wikilink name 정규화.
_ADR_NAME_RE = re.compile(r"ADR-(\d+)$")


class ScopeHit(NamedTuple):
    """개정 대상을 참조하는 문서 하나 — 상대경로 + 매칭된 대상 id(들) + LLM 대조용 본문 excerpt.

    excerpt 는 *참조 줄만*이 아니라 문서 본문(또는 상한 초과 시 참조 주변 윈도)이다 — 잔여 모순은
    참조 줄이 아니라 같은 문서의 **다른 문단/절**에 있으므로 참조 줄만으론 근거가 없어 탐지 불능이다
    (codex must-fix). ref_lines 는 advisory 표시용 참조 라인 스니펫(경량)."""
    path: str
    matched_ids: tuple[str, ...]
    excerpt: str
    ref_lines: tuple[str, ...]


# 프롬프트 크기 상한 근거 (박제·codex must-fix) — 잔여 모순은 참조 줄이 아니라 *같은 문서의 다른
# 문단/절*에 있을 수 있어 참조 줄만으론 탐지 불능이다. 문서 전체를 넣는 게 이상적이나 무한 비용이라
# 상한을 둔다:
#   - 파일이 `_WHOLE_FILE_LINE_CAP` 이하 → **전체 본문** 포함(ADR·wiki·spike 대부분이 수백 줄 이내라
#     "다른 절의 모순"까지 온전히 담긴다·잔여 모순 탐지의 기본 케이스).
#   - 초과 → 각 참조 줄 주변 ±`_EXCERPT_CONTEXT_LINES` 윈도를 병합한 excerpt(생략 구간은 마커)로,
#     총 `_MAX_EXCERPT_LINES` 상한. 참조 인접 문단의 잔여 모순을 담되 프롬프트 토큰을 bound.
_WHOLE_FILE_LINE_CAP = 200
_EXCERPT_CONTEXT_LINES = 15
_MAX_EXCERPT_LINES = 200
_EXCERPT_GAP = "  … (생략) …"


def _build_excerpt(file_lines: list[str], ref_idxs: list[int]) -> str:
    """스코프 파일 본문에서 LLM 대조용 excerpt 를 만든다 — 작은 파일은 전체, 큰 파일은 참조 주변 윈도 병합.

    큰 파일은 각 참조 줄 ±`_EXCERPT_CONTEXT_LINES` 를 keep 하고 인접 윈도를 병합, 생략 구간은
    `_EXCERPT_GAP` 마커로 표시한다(총 `_MAX_EXCERPT_LINES` 상한). 참조 인접 문단의 잔여 모순을 담는다."""
    n = len(file_lines)
    if n <= _WHOLE_FILE_LINE_CAP:
        return "\n".join(file_lines)
    keep: set[int] = set()
    for i in ref_idxs:
        for j in range(max(0, i - _EXCERPT_CONTEXT_LINES), min(n, i + _EXCERPT_CONTEXT_LINES + 1)):
            keep.add(j)
    ordered = sorted(keep)[:_MAX_EXCERPT_LINES]
    out: list[str] = []
    prev: int | None = None
    for idx in ordered:
        if prev is not None and idx != prev + 1:
            out.append(_EXCERPT_GAP)
        out.append(file_lines[idx])
        prev = idx
    if ordered and ordered[-1] < n - 1:
        out.append(_EXCERPT_GAP)
    return "\n".join(out)


def _default_scope_files(home) -> list[Path]:
    """기본 스코프 파일 집합 — `wikilink_lint._collect_wikilink_files`(wiki/·root doc·어댑터 scaffold) 재사용.

    읽기 실패면 빈 목록(graceful — advisory 는 스코프 0 으로 조용히 no-op)."""
    try:
        return list(wikilink_lint._collect_wikilink_files(home))
    except (OSError, UnicodeError):
        return []


def _adr_num(name: str) -> int | None:
    """wikilink name(`ADR-NNNN`) → 정수(선행 0 무관). ADR 참조가 아니면 None."""
    m = _ADR_NAME_RE.fullmatch(name.strip())
    return int(m.group(1)) if m else None


def _target_num_set(target_ids: Iterable[str]) -> set[int]:
    """target id 목록(`ADR-NNNN`·선행 0 생략 표기 혼용)을 정수 집합으로 정규화한다(선행 0 무관)."""
    nums: set[int] = set()
    for tid in target_ids:
        n = _adr_num(str(tid))
        if n is not None:
            nums.add(n)
    return nums


# ── 스코프 수집 (기계) ────────────────────────────────────────────────────────────

def collect_reference_scope(
    home,
    target_ids: Iterable[str],
    files: Iterable[Path],
    *,
    repo: Path | None = None,
) -> list[ScopeHit]:
    """`target_ids`(개정된 ADR)를 `[[ADR-NNNN]]` 로 참조하는 파일을 스코프로 수집한다(back-ref 범위).

    각 파일에서 wikilink 를 훑어 대상 번호(선행 0 무관)와 매칭되면 그 파일 + 매칭 id + 참조 라인 스니펫을
    담는다. 대상을 안 참조하는 파일은 제외(전-코퍼스 무차별 아님). wikilink 문법은 `wikilink_lint` 것을 재사용한다.

    files/repo 를 주입해 hermetic 테스트(실 wiki 미접촉). 반환은 파일 경로 정렬."""
    targets = _target_num_set(target_ids)
    if not targets:
        return []
    wl_re = wikilink_lint._WIKILINK_RE

    def _read(p):
        return fs.read_text_shared(p, encoding="utf-8")
    base = repo or home.root

    hits: list[ScopeHit] = []
    for path in files:
        try:
            text = _read(path)
        except OSError:
            continue
        file_lines = text.splitlines()
        matched: set[int] = set()
        ref_idxs: list[int] = []
        ref_lines: list[str] = []
        for idx, line in enumerate(file_lines):
            line_hit = False
            for raw in wl_re.findall(line):
                n = _adr_num(raw)
                if n is not None and n in targets:
                    matched.add(n)
                    line_hit = True
            if line_hit:
                ref_idxs.append(idx)
                snippet = line.strip()
                if snippet and snippet not in ref_lines:
                    ref_lines.append(snippet)
        if matched:
            try:
                rel = str(Path(path).resolve().relative_to(base.resolve()))
            except ValueError:
                rel = str(path)
            hits.append(ScopeHit(
                path=rel.replace("\\", "/"),
                matched_ids=tuple(f"ADR-{n:04d}" for n in sorted(matched)),
                excerpt=_build_excerpt(file_lines, ref_idxs),
                ref_lines=tuple(ref_lines),
            ))
    hits.sort(key=lambda h: h.path)
    return hits


# ── LLM 프롬프트 (탐지 산출물) ─────────────────────────────────────────────────────

# LLM 출력 계약 — 후보 한 줄씩 `- [<파일>] ...` / 없으면 정확히 이 토큰.
NO_CONTRADICTIONS = "NO_CONTRADICTIONS"
_CANDIDATE_LINE_RE = re.compile(r"^\s*-\s*\[")


def build_prompt(new_adr_id: str, new_adr_title: str, new_adr_text: str, scope: list[ScopeHit]) -> str:
    """모순 탐지 LLM 프롬프트를 조립한다 — 새 결정 + 개정 대상을 참조하는 문서 스니펫 + 출력 계약.

    '옛 결정을 전제로 쓰인 서술이 새 결정과 모순되게 남았나'를 LLM 이 대조하도록 요청한다. 판정은
    사람이므로 후보 제시까지만 요구한다(false-positive 는 사람이 거른다)."""
    lines: list[str] = [
        "당신은 결정(ADR) 개정 직후의 **잔여 모순 서술**을 찾는 검토자다. 방금 아래 결정이 발행/개정됐다.",
        "옛 결정을 전제로 쓰인 다른 문서의 서술이 이 새 결정과 **모순되게 남았는지** 후보를 찾아라.",
        "판정은 사람이 하므로 확실치 않아도 후보로 제시하라(오탐은 사람이 거른다).",
        "",
        f"## 새 결정 — {new_adr_id}: {new_adr_title}",
        "",
        new_adr_text.strip(),
        "",
        "## 개정 대상을 참조하는 문서 (이 범위만 대조·잔여 모순은 참조 줄이 아닌 다른 문단일 수 있다)",
        "",
    ]
    for hit in scope:
        ref_hint = "; ".join(hit.ref_lines) if hit.ref_lines else ""
        lines.append(f"### {hit.path}  (참조: {', '.join(hit.matched_ids)})")
        if ref_hint:
            lines.append(f"참조 지점: {ref_hint}")
        lines.append("")
        lines.append(hit.excerpt.strip())
        lines.append("")
    lines += [
        "## 출력",
        "",
        "각 문서 excerpt 전체를 새 결정과 대조하라(참조 줄뿐 아니라 다른 문단·절의 잔여 모순도 찾아라).",
        "모순 후보를 다음 형식으로 한 줄씩 출력하라:",
        "  - [<파일경로>] <옛 결정을 전제로 남은 서술> — <새 결정과 어떻게 모순되나>",
        f"모순 후보가 하나도 없으면 정확히 `{NO_CONTRADICTIONS}` 한 줄만 출력하라.",
    ]
    return "\n".join(lines)


def parse_candidates(output: str) -> list[str]:
    """LLM 출력에서 모순 후보 라인(`- [<파일>] ...`)을 파싱한다. `NO_CONTRADICTIONS` → 빈 목록."""
    out: list[str] = []
    for line in output.splitlines():
        if line.strip() == NO_CONTRADICTIONS:
            continue
        if _CANDIDATE_LINE_RE.match(line):
            out.append(line.strip())
    return out


# ── 오케스트레이션 ────────────────────────────────────────────────────────────────

class LintResult(NamedTuple):
    scope: list[ScopeHit]
    prompt: str | None       # 스코프 있을 때만
    candidates: list[str]    # LLM 호출(run_fn) 시에만 채워짐
    called: bool             # LLM 을 실제로 호출했나(dry 면 False)


class ContradictionLinter:
    """모순 lint 핵심 — 스코프 수집(기계) + 프롬프트 조립 + (opt-in) LLM 탐지.

    DI: `files_fn`(스코프 파일 수집·기본 `wikilink_lint` 재사용)·`run_fn`(LLM 호출·기본 None=dry·미호출).
    dry(run_fn=None)면 스코프+프롬프트만 산출하고 LLM 을 호출하지 않는다(비용 0·hermetic)."""

    def __init__(
        self,
        home,
        *,
        files_fn: Callable[[], Iterable[Path]] | None = None,
        run_fn: Callable[[str], str] | None = None,
        repo: Path | None = None,
    ) -> None:
        self._home = home
        self._files_fn = files_fn or (lambda: _default_scope_files(home))
        self._run_fn = run_fn
        self._repo = repo or home.root

    def lint(self, *, new_adr_id: str, new_adr_title: str, new_adr_text: str,
             target_ids: Iterable[str]) -> LintResult:
        """개정된 결정을 참조하는 스코프를 수집하고, LLM 탐지(주입 시)를 수행하거나 프롬프트만 산출한다."""
        scope = collect_reference_scope(
            self._home,
            target_ids, list(self._files_fn()), repo=self._repo,
        )
        if not scope:
            return LintResult(scope=[], prompt=None, candidates=[], called=False)
        prompt = build_prompt(new_adr_id, new_adr_title, new_adr_text, scope)
        if self._run_fn is None:
            return LintResult(scope=scope, prompt=prompt, candidates=[], called=False)
        output = self._run_fn(prompt)
        return LintResult(
            scope=scope, prompt=prompt, candidates=parse_candidates(output), called=True,
        )


def format_advisory(new_adr_id: str, target_ids: Iterable[str], result: LintResult) -> str:
    """모순 lint 결과를 사람이 읽는 advisory 블록으로 렌더한다(차단 아님·판정=사람)."""
    tids = ", ".join(str(t) for t in target_ids)
    if not result.scope:
        return (f"[모순 lint] {new_adr_id} 개정 대상({tids})을 참조하는 문서가 없음 "
                "— 잔여 모순 스코프 없음.")
    header = (f"[모순 lint] {new_adr_id} 가 개정한 결정({tids})을 참조하는 문서 "
              f"{len(result.scope)}개 — 새 결정과 모순되는 잔여 서술이 있는지 대조하라(판정=사람·차단 아님):")
    lines = [header]
    for hit in result.scope:
        lines.append(f"  · {hit.path}  (참조: {', '.join(hit.matched_ids)})")
    if result.called:
        if result.candidates:
            lines.append("  LLM 모순 후보(사람 판정 필요):")
            lines.extend(f"    {c}" for c in result.candidates)
        else:
            lines.append("  LLM 탐지: 모순 후보 없음(NO_CONTRADICTIONS).")
    else:
        # standalone 커맨드 전체 예시 — pm_adr 경로에선 `--show-prompt` 만으론 실행 불가라 재생성 명령을
        # 온전히 안내한다(codex suggestion·pm_adr 플래그 신설은 과설계로 회피). scope 는 verb 무관이라
        # 모든 대상을 --amends 로 넘겨도 동일 프롬프트가 재생성된다.
        amends_flags = " ".join(f"--amends {t}" for t in target_ids)
        lines.append("  탐지 = LLM(기본 dry·미호출). 위 문서를 새 결정과 대조해 잔여 모순을 확인하라.")
        lines.append(f"  프롬프트 재생성: python3 .project_manager/tools/contradiction_lint.py "
                     f"--new-adr {new_adr_id} {amends_flags} --show-prompt")
    return "\n".join(lines)
