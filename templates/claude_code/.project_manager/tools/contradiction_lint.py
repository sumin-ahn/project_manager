#!/usr/bin/env python3
"""모순 lint — "결정을 바꾼 순간의 잔여" 탐지.

결정을 개정(amends/supersedes)하면 옛 결정을 전제로 쓰인 다른 서술(타 ADR·wiki·spike·프롬프트)이
새 결정과 **모순된 채 남는다**. 기계 lint 전 구간이 clean 인데도 이 잔여는 안 잡힌다.
병목은 *가드 부재*가 아니라 **인지 시점** — 재정의를 하는 바로 그 순간에 점검해야 놓치지 않는다. 이 lint 는
그 순간(ADR 발행/개정 명령·pm_adr.py)에 배선돼 발화한다.

역할 분업:
  - **탐지 = LLM** — prose 의미 모순은 정규식으로 못 잡는다(LLM 필요). 단 호출은 **DI seam + 기본 dry
    (미호출)** — 기본은 스코프를 기계로 수집하고 LLM 프롬프트를 산출물로 표면화한다(비용 없음·hermetic).
    실 LLM 배선이 필요하면 `run_fn` 을 주입한다(external_review DI 동형).
  - **판정 = 사람** — 실제 모순인지 판정·해소는 사람(generate≠evaluate). **차단 아님**(advisory·
    후보 표면화까지가 lint 역할).

대상 스코프:
  - 개정된 결정(`target_ids`)을 `[[ADR-NNNN]]` **wikilink 로 참조하는 문서**(back-ref 범위). 전-코퍼스
    무차별이 아니다. wikilink 문법·파일 수집은 board.py 의 것을 재사용한다(자체 regex/매핑 신설 0).

설계:
  - `collect_reference_scope` — 순수 함수(파일 목록 주입). 기본 파일 수집은 board `_collect_wikilink_files`
    (REPO-anchored·pm_adr 와 같은 self-location) 재사용, 실패 시 graceful 빈 스코프.
  - `ContradictionLinter` — DI(files_fn·run_fn) 로 hermetic 테스트. `run_fn=None`(기본)이면 dry — 프롬프트만
    산출·LLM 미호출.
  - 이 lint 는 **advisory** — 어떤 경로에서도 예외로 발행을 막지 않는다(fail-soft·pm_adr 트리거는 감싸 호출).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Callable, Iterable, NamedTuple

_TOOLS_BOOTSTRAP = os.path.dirname(os.path.abspath(__file__))
_TOOLS_BOOTSTRAP_FILE = os.path.realpath(
    os.path.join(_TOOLS_BOOTSTRAP, "repo_owned_files.py")
)
_TOOLS_BOOTSTRAP_KEY = f"_project_manager_repo_owned_files_bootstrap:{_TOOLS_BOOTSTRAP_FILE}"
_TOOLS_BOOTSTRAP_MODULE = sys.modules.get(_TOOLS_BOOTSTRAP_KEY)
_TOOLS_BOOTSTRAP_SENTINEL = object()
try:
    if (
        _TOOLS_BOOTSTRAP_MODULE is not None
        and os.path.realpath(getattr(_TOOLS_BOOTSTRAP_MODULE, "__file__", ""))
        != _TOOLS_BOOTSTRAP_FILE
    ):
        sys.modules.pop(_TOOLS_BOOTSTRAP_KEY, None)
        _TOOLS_BOOTSTRAP_MODULE = None
    if _TOOLS_BOOTSTRAP_MODULE is None:
        _TOOLS_BOOTSTRAP_PREVIOUS = sys.modules.pop(
            "repo_owned_files", _TOOLS_BOOTSTRAP_SENTINEL
        )
        _TOOLS_BOOTSTRAP_ADDED = not sys.path or sys.path[0] != _TOOLS_BOOTSTRAP
        if _TOOLS_BOOTSTRAP_ADDED:
            sys.path.insert(0, _TOOLS_BOOTSTRAP)
        try:
            import repo_owned_files as _TOOLS_BOOTSTRAP_MODULE
            if (
                os.path.realpath(getattr(_TOOLS_BOOTSTRAP_MODULE, "__file__", ""))
                != _TOOLS_BOOTSTRAP_FILE
            ):
                raise ImportError(
                    "repo_owned_files 형제 경로 불일치: "
                    f"{getattr(_TOOLS_BOOTSTRAP_MODULE, '__file__', None)!r}"
                )
            sys.modules[_TOOLS_BOOTSTRAP_KEY] = _TOOLS_BOOTSTRAP_MODULE
        finally:
            # 엔진 import bootstrap은 메인 스레드 전용이다. 그래도 위치를 가정한 pop(0)은
            # 피하고, 우리가 넣은 값이 남아 있을 때 그 값만 제거한다.
            if _TOOLS_BOOTSTRAP_ADDED:
                try:
                    sys.path.remove(_TOOLS_BOOTSTRAP)
                except ValueError:
                    pass
            if sys.modules.get("repo_owned_files") is _TOOLS_BOOTSTRAP_MODULE:
                sys.modules.pop("repo_owned_files", None)
            if _TOOLS_BOOTSTRAP_PREVIOUS is not _TOOLS_BOOTSTRAP_SENTINEL:
                sys.modules["repo_owned_files"] = _TOOLS_BOOTSTRAP_PREVIOUS
    _load_module_from_path = _TOOLS_BOOTSTRAP_MODULE.load_module
except Exception as _TOOLS_BOOTSTRAP_ERROR:
    if sys.modules.get(_TOOLS_BOOTSTRAP_KEY) is _TOOLS_BOOTSTRAP_MODULE:
        sys.modules.pop(_TOOLS_BOOTSTRAP_KEY, None)

    def _load_module_from_path(
        path,
        expected_filename,
        *,
        verifier=None,
        allow_unverified=False,
        cache=False,
        cache_key=None,
    ):
        """구형/손상 중앙 seam에서 복구 명령까지 띄우는 import-by-name 폴백."""
        target = os.path.realpath(os.fspath(path))
        if os.path.basename(target) != expected_filename:
            raise ValueError(
                f"module filename mismatch: expected {expected_filename!r}, "
                f"got {os.path.basename(target)!r}"
            )
        if verifier is not None and allow_unverified:
            raise ValueError("choose verifier or allow_unverified=True, not both")
        if verifier is None and not allow_unverified:
            raise ValueError(
                "module load requires verifier or explicit allow_unverified=True"
            )
        module_key = cache_key or f"_project_manager_legacy_loaded:{target}"
        module = sys.modules.get(module_key) if cache else None
        inserted = False
        try:
            if module is None:
                if (
                    target == _TOOLS_BOOTSTRAP_FILE
                    and _TOOLS_BOOTSTRAP_MODULE is not None
                ):
                    module = _TOOLS_BOOTSTRAP_MODULE
                else:
                    import_name = os.path.splitext(expected_filename)[0]
                    previous = sys.modules.pop(
                        import_name, _TOOLS_BOOTSTRAP_SENTINEL
                    )
                    parent = os.path.dirname(target)
                    added = not sys.path or sys.path[0] != parent
                    if added:
                        sys.path.insert(0, parent)
                    try:
                        module = __import__(import_name)
                        if os.path.realpath(getattr(module, "__file__", "")) != target:
                            raise ImportError(
                                f"{expected_filename} 형제 경로 불일치"
                            )
                    finally:
                        if added:
                            try:
                                sys.path.remove(parent)
                            except ValueError:
                                pass
                        if sys.modules.get(import_name) is module:
                            sys.modules.pop(import_name, None)
                        if previous is not _TOOLS_BOOTSTRAP_SENTINEL:
                            sys.modules[import_name] = previous
                if cache:
                    sys.modules[module_key] = module
                    inserted = True
            if verifier is not None:
                verifier(module, expected_filename)
            return module
        except Exception as exc:
            if cache and (inserted or sys.modules.get(module_key) is module):
                sys.modules.pop(module_key, None)
            if target == _TOOLS_BOOTSTRAP_FILE:
                raise RuntimeError(
                    f"엔진 공용 로더 {target}를 불러올 수 없음; "
                    "pm-update로 .project_manager/tools 전체를 재동기화하라."
                ) from exc
            raise


# ── REPO 앵커 (external_review `_find_repo_root` 동형·adopter self-location) ──────

def _find_repo_root() -> Path:
    """스크립트 위치에서 부모 체인을 상향 탐색해 `.project_manager` 를 품은 첫 조상을 반환한다.

    worktree/PM 홈 등 다른 깊이여도 마커로 견고 해소한다. 못 찾으면 `parents[2]` 폴백(graceful·회귀 0)."""
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        if (ancestor / ".project_manager").is_dir():
            return ancestor
    return here.parents[2]


REPO = _find_repo_root()
DECISIONS_DIR = REPO / ".project_manager" / "wiki" / "decisions"
BOARD_PY = Path(__file__).resolve().parent / "board.py"

# board `_WIKILINK_RE` 와 동일 문법(자체 regex 금지·재사용 실패 시 폴백용 동일 복제). name 만 캡처.
_FALLBACK_WIKILINK_RE = re.compile(r"\[\[([A-Za-z0-9_\s.\-]+?)(?:\|[^\]]+)?\]\]")
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


# ── 엔진 사본 rev 스탬프 (형제 사본 skew fail-loud) ──────────────────────
# baked 리터럴 — 이 값은 이 파일 코드 안에 고정된다(engine_rev.py 런타임 읽기 아님). 부분/수동
# 복사로 신 로더 + 구 형제가 섞이면 각자 새/옛 리터럴을 지녀 대조에서 skew 로 검출된다(런타임
# 공유-읽기였다면 같은 디렉토리 안 자기-일치라 미검출). 릴리즈 bump 는 `engine_rev.py --bump
# vX.Y.Z` 가 전 stamped 모듈 리터럴을 기계 일괄 재작성한다(사람 N곳 편집 0). 평시 회귀 가드
# (test_engine_rev_stamp)가 전 모듈 리터럴 == engine_rev.ENGINE_REV 를 강제한다.
ENGINE_REV = "v1.6.3"


def _verify_engine_rev(sibling_module, sibling_filename):
    """로드한 형제 모듈의 baked ENGINE_REV 를 이 사본의 것과 대조한다 (fail-loud·skew→명시 에러).

    불일치/부재(구형 형제는 리터럴 부재=None)면 사본 skew → 명시 에러(어느 파일이 어떤 rev 인지
    지목 + pm-update 안내). self-contained(engine_rev.py 런타임 의존 0)라 부분복사도 정확 검출한다.
    """
    got = getattr(sibling_module, "ENGINE_REV", None)
    if got != ENGINE_REV:
        err = RuntimeError(
            f"엔진 사본 버전 불일치 — 로더 {Path(__file__).name}(rev={ENGINE_REV!r})가 "
            f"형제 {sibling_filename}(rev={got!r})를 로드했다 (사본 skew: 부분/수동 복사 또는 "
            f"구형 사본). `pm-update`(또는 pm_update.py)로 .project_manager/tools/ 전체를 재동기하라."
        )
        err._engine_rev_skew = True  # fail-soft 로더가 재-raise 식별
        raise err


def _is_engine_rev_skew(exc) -> bool:
    """예외가 rev-스탬프 skew(EngineRevSkew·불완전 복사) 유래인지 (fail-soft 재-raise 식별)."""
    return getattr(exc, "_engine_rev_skew", False)


# ── board 재사용 (wikilink 문법·파일 수집·신설 매핑 0) ────────────────────────────

def _load_board():
    """board.py 를 sibling import 한다 (ticket_finish 관용구·실패 시 None·graceful)."""
    try:
        mod = _load_module_from_path(
            BOARD_PY, "board.py", verifier=_verify_engine_rev,
        )
    except Exception as exc:
        if _is_engine_rev_skew(exc):
            raise  # board 사본 skew 는 fail-loud(삼키지 않는다).
        return None
    return mod


def _wikilink_re() -> re.Pattern[str]:
    """wikilink 문법 — board `_WIKILINK_RE` 재사용(자체 regex 금지), 실패 시 동일 폴백 복제."""
    board = _load_board()
    return getattr(board, "_WIKILINK_RE", _FALLBACK_WIKILINK_RE) if board else _FALLBACK_WIKILINK_RE


def _default_scope_files() -> list[Path]:
    """기본 스코프 파일 집합 — board `_collect_wikilink_files`(wiki/·root doc·어댑터 scaffold) 재사용.

    board 미로드/실패면 빈 목록(graceful — advisory 는 스코프 0 으로 조용히 no-op)."""
    board = _load_board()
    if board is None or not hasattr(board, "_collect_wikilink_files"):
        return []
    try:
        return list(board._collect_wikilink_files())
    except Exception:
        return []


def _adr_num(name: str) -> int | None:
    """wikilink name(`ADR-0061`) → 정수 61. ADR 참조가 아니면 None."""
    m = _ADR_NAME_RE.fullmatch(name.strip())
    return int(m.group(1)) if m else None


def _target_num_set(target_ids: Iterable[str]) -> set[int]:
    """target id 목록(`['ADR-0061', 'ADR-62']`)을 정수 집합으로 정규화한다(선행 0 무관)."""
    nums: set[int] = set()
    for tid in target_ids:
        n = _adr_num(str(tid))
        if n is not None:
            nums.add(n)
    return nums


# ── 스코프 수집 (기계) ────────────────────────────────────────────────────────────

def collect_reference_scope(
    target_ids: Iterable[str],
    files: Iterable[Path],
    *,
    wikilink_re: re.Pattern[str] | None = None,
    read: Callable[[Path], str] | None = None,
    repo: Path | None = None,
) -> list[ScopeHit]:
    """`target_ids`(개정된 ADR)를 `[[ADR-NNNN]]` 로 참조하는 파일을 스코프로 수집한다(back-ref 범위).

    각 파일에서 wikilink 를 훑어 대상 번호(선행 0 무관)와 매칭되면 그 파일 + 매칭 id + 참조 라인 스니펫을
    담는다. 대상을 안 참조하는 파일은 제외(전-코퍼스 무차별 아님). wikilink 문법은 board 것을 재사용한다.

    files/read/repo 를 주입해 hermetic 테스트(실 wiki 미접촉). 반환은 파일 경로 정렬."""
    targets = _target_num_set(target_ids)
    if not targets:
        return []
    wl_re = wikilink_re or _wikilink_re()
    _read = read or (lambda p: p.read_text(encoding="utf-8"))
    base = repo or REPO

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

    DI: `files_fn`(스코프 파일 수집·기본 board 재사용)·`run_fn`(LLM 호출·기본 None=dry·미호출).
    dry(run_fn=None)면 스코프+프롬프트만 산출하고 LLM 을 호출하지 않는다(비용 0·hermetic)."""

    def __init__(
        self,
        *,
        files_fn: Callable[[], Iterable[Path]] | None = None,
        run_fn: Callable[[str], str] | None = None,
        repo: Path | None = None,
    ) -> None:
        self._files_fn = files_fn or _default_scope_files
        self._run_fn = run_fn
        self._repo = repo or REPO

    def lint(self, *, new_adr_id: str, new_adr_title: str, new_adr_text: str,
             target_ids: Iterable[str]) -> LintResult:
        """개정된 결정을 참조하는 스코프를 수집하고, LLM 탐지(주입 시)를 수행하거나 프롬프트만 산출한다."""
        scope = collect_reference_scope(
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


# ── CLI (standalone 재검사·pm_adr 는 API 로 직접 호출) ──────────────────────────────

def _load_adr(decisions_dir: Path, num: int) -> tuple[str, str]:
    """decisions/ 에서 ADR 번호 → (title, 전체 텍스트). 파일/frontmatter 부재면 (빈 title, 텍스트)."""
    matches = sorted(decisions_dir.glob(f"{num:04d}-*.md"))
    if not matches:
        return "", ""
    text = matches[0].read_text(encoding="utf-8")
    m = re.search(r"^title:\s*(.+)$", text, re.MULTILINE)
    title = m.group(1).strip().strip("'\"") if m else ""
    return title, text


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="contradiction_lint.py",
        description="모순 lint — 개정된 결정을 참조하는 문서의 잔여 모순 후보 표면화(advisory).",
    )
    p.add_argument("--new-adr", required=True, metavar="ADR-NNNN",
                   help="방금 발행/개정한 새 결정 id (decisions/ 에서 title·본문 로드)")
    p.add_argument("--amends", action="append", default=[], metavar="ADR-NNNN",
                   help="개정(amends) 대상 — 참조 스코프 수집 (반복 가능)")
    p.add_argument("--supersedes", action="append", default=[], metavar="ADR-NNNN",
                   help="대체(supersedes) 대상 — 참조 스코프 수집 (반복 가능)")
    p.add_argument("--show-prompt", action="store_true", help="LLM 탐지 프롬프트도 출력")
    return p


def _parse_ids(tokens: list[str]) -> list[str]:
    out: list[str] = []
    for tok in tokens:
        for part in re.split(r"[,\s]+", tok):
            part = part.strip()
            if part:
                n = _adr_num(part) if part.upper().startswith("ADR-") else _adr_num(f"ADR-{part}")
                if n is not None:
                    out.append(f"ADR-{n:04d}")
    return out


def main(argv: list[str] | None = None) -> int:
    _console_encoding = _load_module_from_path(
        Path(__file__).resolve().with_name("console_encoding.py"),
        "console_encoding.py",
        verifier=_verify_engine_rev,
    )
    _console_encoding.configure_console_utf8()
    args = build_parser().parse_args(argv)
    targets = _parse_ids(args.amends) + _parse_ids(args.supersedes)
    if not targets:
        print("[모순 lint] 개정 대상(--amends/--supersedes) 없음 — 신규 plain 발행은 참조 스코프가 "
              "없어 no-op(개정만 잔여 모순을 만든다).")
        return 0
    new_id = _parse_ids([args.new_adr])
    new_adr_id = new_id[0] if new_id else args.new_adr
    num = _adr_num(new_adr_id) or 0
    title, text = _load_adr(DECISIONS_DIR, num)

    linter = ContradictionLinter()  # dry (LLM 미호출)
    result = linter.lint(
        new_adr_id=new_adr_id, new_adr_title=title, new_adr_text=text, target_ids=targets,
    )
    print(format_advisory(new_adr_id, targets, result))
    if args.show_prompt and result.prompt:
        print("\n── LLM 탐지 프롬프트 ──")
        print(result.prompt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
