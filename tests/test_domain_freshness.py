"""domain freshness 관찰불가 사각 닫기 — anchor 판정 + pin-경계 관찰가능성 + 글롭 충실도 (T-0421·codex MF/R1-R10).

domain 페이지 `verified_at` sha 기반 freshness 판정(ADR-0063)이 두-git 형상(ADR-0027)에서 거짓
green 이 되던 축들을 advisory/stale 로 정직히 표면화하는지 검증한다.

anchor 판정(`board._sha_anchor_status`) — 유효 anchor = 고정 hex + **고정 SHA 해소**(hex-이름 ref 아님·R5)
  + **HEAD 선조**(R4-α). 실패는 non-sha/unresolved/non-ancestor verdict 로 구분(env-error→unknown silent).
covers 관찰가능성(`domain.covers_pathspecs`) — 경계는 **verified_at**(R4-β)·매핑은 **`:(glob)` magic
  pathspec 직접**(R6·손실 접두사 폐기·`?`/`[]` 이스케이프 R7·지원 문법만 R8): **HEAD 트리**(커밋·`git diff
  <빈-트리> HEAD`·index/staged 무관·R9·object-format 중립 R10) 에 있음 OR pin 이후 델타 → present,
  미추적+pin 이후 델타 0 → absent, 빈/공백 → skip·미지원 형태/repo-밖 경로 → unmappable(advisory·R8/R10).

git 은 argv 를 분기하는 hermetic runner 로 대역(rev-parse/merge-base/diff/log)해 실 subprocess 미사용.
`:(glob)`·SHA-256·staged 실 동작은 별도 throwaway 실 git 테스트로 대역과 정합 확인.
"""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from _textio import write_lf
from _win_skip import posix_filenames_supported

# 리터럴 `?`/`[]` 파일명은 Windows 에서 불법이라 실 git 이스케이프 테스트는 POSIX 전용 (codex R7).
_posix_only = pytest.mark.skipif(not posix_filenames_supported(),
                                 reason="리터럴 ?/[] 파일명은 POSIX 전용(Windows 비호환)")


def _sha256_supported() -> bool:
    """git 이 `--object-format=sha256` 을 지원하나 (SHA-256 회귀 테스트 게이트·codex R10)."""
    probe = tempfile.mkdtemp(prefix="sha256probe_")
    try:
        return subprocess.run(["git", "-C", probe, "init", "--object-format=sha256", "-q"],
                              capture_output=True).returncode == 0
    finally:
        shutil.rmtree(probe, ignore_errors=True)


_sha256_only = pytest.mark.skipif(not _sha256_supported(),
                                  reason="git 이 --object-format=sha256 미지원")

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

_GLOB_MAGIC = ":(glob)"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _windows_expanduser(path: Path) -> Path:
    """Windows `Path.expanduser()` 대역 — **없는 사용자도** `C:/Users/<name>` 으로 조립한다.

    POSIX 는 `pwd` 조회 실패를 RuntimeError 로 올리지만 Windows 는 프로필 루트에 이름을 붙여
    돌려준다(실재 확인 없음). 이 차이가 `~user` 미확장을 플랫폼마다 다른 판정으로 갈랐다.
    """
    first = path.parts[0] if path.parts else ""
    if not first.startswith("~"):
        return path
    return Path("C:/Users") / (first[1:] or "pmuser") / Path(*path.parts[1:])


def _use_windows_expanduser(monkeypatch) -> None:
    """`Path.expanduser` **자체**에 Windows 동작을 주입한다 — 엔진 seam 유무와 무관한 경계.

    엔진 쪽 확장 seam 에 주입하면 "seam 이 있는 구현"만 태우게 된다(그 seam 이 곧 이번 수정의
    일부라 수정 전 red 가 성립하지 않는다). 플랫폼 API 를 직접 갈아끼워 Windows 분기를 Linux
    에서 있는 그대로 태운다.
    """
    monkeypatch.setattr(Path, "expanduser", _windows_expanduser)


@pytest.fixture
def board():
    return _load_module("board", TOOLS / "board.py")


@pytest.fixture
def domain():
    return _load_module("domain", TOOLS / "domain.py")


def _unmagic(spec: str) -> str:
    """git pathspec 에서 `:(glob)` magic 을 벗겨 원본 covers 글롭을 얻는다(대역 매칭용·codex R6)."""
    return spec[len(_GLOB_MAGIC):] if spec.startswith(_GLOB_MAGIC) else spec


# ── hermetic git 대역 (argv 분기: rev-parse / merge-base / diff / log) ─────────

def _git(*, resolves: bool = True, rev_rc: int | None = None,
         ancestor: bool = True, mb_rc: int | None = None,
         tracked=None, post_pin=(), resolved_oid=None):
    """anchor 3조건 + covers 관찰가능성을 재현하는 hermetic git runner.

    rev-parse             : `rev_rc` 명시 시 그 rc / 아니면 resolves→(0|1). 해소 OID 는
                            `resolved_oid`(hex-이름 ref 재현) 또는 입력 sha prefix(진짜 SHA·R5).
    merge-base --is-ancestor: `mb_rc` 명시 시 그 rc / 아니면 ancestor→(0|1).  [R4-α]
    diff(HEAD-tree presence) / log(pin 델타) : pathspec 의 `:(glob)` 를 벗긴 **원본 글롭**으로
                            `tracked`(HEAD 트리에 있음·codex R9)·`post_pin`(pin 이후 델타) 집합 매칭.
    """
    track = None if tracked is None else set(tracked)
    pin = set(post_pin)

    def runner(argv):
        cmd = argv[0] if argv else ""
        if cmd == "rev-parse":
            if "--show-object-format" in argv:
                return (0, "sha1\n")   # 빈 트리 OID 산출용(codex R10 MF-1)
            if rev_rc is not None:
                return (rev_rc, "")
            if not resolves:
                return (1, "")
            target = argv[-1].split("^")[0]
            oid = resolved_oid if resolved_oid is not None else target.ljust(40, "0")
            return (0, oid + "\n")
        if cmd == "merge-base":
            if mb_rc is not None:
                return (mb_rc, "")
            return (0, "") if ancestor else (1, "")
        if cmd == "diff":   # HEAD 트리 presence (codex R9·index/staged 무관)
            g = _unmagic(argv[-1])
            present = True if track is None else (g in track)
            return (0, f"{g}/file\n" if present else "")
        if cmd == "log" and "--" in argv:
            specs = [_unmagic(s) for s in argv[argv.index("--") + 1:]]
            return (0, "abc1234 commit\n") if any(s in pin for s in specs) else (0, "")
        return (0, "")
    return runner


def _raising_git(argv):
    """주입 runner 가 raise → covers_pathspecs/anchor 판정이 분류 불가(skip/unknown)."""
    raise RuntimeError("git unavailable")


def _fs_git(*, tracked=(), post_pin=()):
    """covers_pathspecs 관찰가능성 대역 — diff(HEAD 트리 presence·codex R9)와 log(pin 이후 델타)
    분리 제어. pathspec 의 `:(glob)` 를 벗긴 원본 글롭으로 집합 매칭."""
    tr, pin = set(tracked), set(post_pin)

    def runner(argv):
        cmd = argv[0] if argv else ""
        if cmd == "rev-parse":
            return (0, "sha1\n")   # 빈 트리 OID 산출용(codex R10 MF-1)
        g = _unmagic(argv[-1])
        if cmd == "diff":
            return (0, f"{g}/file\n") if g in tr else (0, "")
        if cmd == "log":
            return (0, f"abc del {g}\n") if g in pin else (0, "")
        return (0, "")
    return runner


# ── domain 페이지 fixture 와이어링 ────────────────────────────────────────────

def _domain_page(domain_dir: Path, name: str, *, covers, verified_at=None,
                 title="페이지", repo=None) -> Path:
    domain_dir.mkdir(parents=True, exist_ok=True)
    fm = [f"title: {title}", "type: concept"]
    if covers:
        fm.append("covers:")
        # covers 글롭을 따옴표로 감싼다 — `**/*.py` 처럼 `*` 로 시작하면 YAML 이 alias 로 오파싱한다
        # (실제 도메인 페이지도 leading-`*` 글롭은 quote 필요·엔진 이슈 아님).
        fm.extend(f'  - "{c}"' for c in covers)
    if verified_at is not None:
        fm.append(f"verified_at: {verified_at}")
    if repo is not None:
        fm.append(f"repo: {repo}")
    path = domain_dir / name
    path.write_text("---\n" + "\n".join(fm) + "\n---\n\nbody\n", encoding="utf-8")
    return path


def _wire(board, domain, monkeypatch, repo: Path):
    domain_dir = repo / "domain"
    monkeypatch.setattr(board, "REPO", repo)
    monkeypatch.setattr(board, "LOCAL_CONF", repo / ".project_manager" / "local.conf")
    monkeypatch.setattr(domain, "DOMAIN_DIR", domain_dir)
    monkeypatch.setattr(board, "_load_domain_module", lambda: domain)
    return domain_dir


# ── 핵심 판정 (anchor OK + covers 관찰가능성) ────────────────────────────────

# (1) 현재 tracked + pin 이후 델타 → domain-stale (양성 대조·회귀 0).
def test_present_path_with_delta_flags_stale(board, domain, monkeypatch, tmp_path):
    domain_dir = _wire(board, domain, monkeypatch, tmp_path)
    _domain_page(domain_dir, "p.md", covers=["src/mod/**"], verified_at="cafe0001",
                 title="모듈")
    findings = board.lint_domain_freshness(
        runner=_git(tracked={"src/mod/**"}, post_pin={"src/mod/**"}))
    assert len(findings) == 1
    label, kind, detail = findings[0]
    assert (label, kind) == ("모듈", "domain-stale")
    assert "cafe0001" in detail


# (2) 현재 tracked + pin 이후 델타 없음 → clean.
def test_present_path_no_delta_is_clean(board, domain, monkeypatch, tmp_path):
    domain_dir = _wire(board, domain, monkeypatch, tmp_path)
    _domain_page(domain_dir, "p.md", covers=["src/mod/**"], verified_at="cafe0001")
    assert board.lint_domain_freshness(
        runner=_git(tracked={"src/mod/**"}, post_pin=())) == []


# (3) 미추적 + pin 이후 델타 0(never-tracked) → domain-unverifiable "관찰 불가".
def test_never_tracked_path_flags_unverifiable(board, domain, monkeypatch, tmp_path):
    domain_dir = _wire(board, domain, monkeypatch, tmp_path)
    _domain_page(domain_dir, "codex.md", covers=["templates/codex/**"],
                 verified_at="cafe0002", title="codex 어댑터")
    findings = board.lint_domain_freshness(runner=_git(tracked=(), post_pin=()))
    assert len(findings) == 1
    label, kind, detail = findings[0]
    assert (label, kind) == ("codex 어댑터", "domain-unverifiable")
    assert "관찰 불가" in detail and "templates/codex/**" in detail
    assert not any(k == "domain-stale" for _l, k, _d in findings)


# ── anchor 실패 축 (모두 unverifiable·사유 구분) ─────────────────────────────

def test_unresolved_sha_flags_unverifiable(board, domain, monkeypatch, tmp_path):
    domain_dir = _wire(board, domain, monkeypatch, tmp_path)
    _domain_page(domain_dir, "p.md", covers=["src/mod/**"], verified_at="deadbeef",
                 title="모듈")
    findings = board.lint_domain_freshness(
        runner=_git(resolves=False, tracked={"src/mod/**"}, post_pin={"src/mod/**"}))
    assert len(findings) == 1
    label, kind, detail = findings[0]
    assert (label, kind) == ("모듈", "domain-unverifiable")
    assert "deadbeef" in detail and "해소 안 됨" in detail


def test_non_ancestor_sha_flags_unverifiable(board, domain, monkeypatch, tmp_path):
    domain_dir = _wire(board, domain, monkeypatch, tmp_path)
    _domain_page(domain_dir, "p.md", covers=["src/mod/**"], verified_at="beef0042",
                 title="모듈")
    findings = board.lint_domain_freshness(runner=_git(
        resolves=True, ancestor=False, tracked={"src/mod/**"}, post_pin={"src/mod/**"}))
    assert len(findings) == 1
    _label, kind, detail = findings[0]
    assert kind == "domain-unverifiable" and "선조 아님" in detail


def test_ambiguous_abbrev_sha_flags_unverifiable(board, domain, monkeypatch, tmp_path):
    # codex R22: 축약 sha 모호(repo 성장 다중 매칭) → env silent skip 아니라 unverifiable advisory.
    # --quiet rev-parse 는 rc1 로 stderr 억제, non-quiet 진단이 모호 stderr → ambiguous verdict.
    domain_dir = _wire(board, domain, monkeypatch, tmp_path)
    _domain_page(domain_dir, "p.md", covers=["src/mod/**"], verified_at="25b0abc", title="모호핀")

    def runner(argv):
        if argv and argv[0] == "rev-parse":
            if "--quiet" in argv:
                return (1, "")                                  # --quiet 억제(rc1)
            return (128, "", _REAL_AMBIGUOUS_STDERR)            # non-quiet 진단 → 모호 stderr
        return (0, "")

    findings = board.lint_domain_freshness(runner=runner)
    assert len(findings) == 1
    _label, kind, detail = findings[0]
    assert kind == "domain-unverifiable" and "모호" in detail and "재핀" in detail
    assert not any(k == "domain-stale" for _l, k, _d in findings)


def test_moving_ref_verified_at_is_unverifiable(board, domain, monkeypatch, tmp_path):
    domain_dir = _wire(board, domain, monkeypatch, tmp_path)
    _domain_page(domain_dir, "p.md", covers=["src/mod/**"], verified_at="HEAD",
                 title="모듈")
    findings = board.lint_domain_freshness(
        runner=_git(tracked={"src/mod/**"}, post_pin={"src/mod/**"}))
    assert len(findings) == 1
    _label, kind, detail = findings[0]
    assert kind == "domain-unverifiable" and "고정 sha 아님" in detail


def test_hex_named_ref_verified_at_is_unverifiable(board, domain, monkeypatch, tmp_path):
    # codex R5: hex-이름 branch/tag 는 rev-parse 해소하나 OID 가 입력과 무관 → non-sha.
    domain_dir = _wire(board, domain, monkeypatch, tmp_path)
    _domain_page(domain_dir, "p.md", covers=["src/mod/**"], verified_at="deadbeef",
                 title="모듈")
    findings = board.lint_domain_freshness(runner=_git(
        resolved_oid="1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b",
        tracked={"src/mod/**"}, post_pin={"src/mod/**"}))
    assert len(findings) == 1
    _label, kind, detail = findings[0]
    assert kind == "domain-unverifiable" and "고정 sha 아님" in detail


def test_unverifiable_sha_skips_covers_judgment(board, domain, monkeypatch, tmp_path):
    # 전제(codex R4): anchor 실패 페이지는 covers 관찰가능성 판정에 안 온다 — 사유는 sha 만.
    domain_dir = _wire(board, domain, monkeypatch, tmp_path)
    _domain_page(domain_dir, "codex.md", covers=["templates/codex/**"],
                 verified_at="3cf0f731", title="codex 어댑터")
    findings = board.lint_domain_freshness(runner=_git(resolves=False, tracked=(), post_pin=()))
    assert len(findings) == 1
    _label, kind, detail = findings[0]
    assert kind == "domain-unverifiable"
    assert "해소 안 됨" in detail and "관찰 불가" not in detail


# ── 삭제/rename 경계 (pin 기준·codex R2/R4-β) ────────────────────────────────

def test_deleted_after_pin_flags_stale(board, domain, monkeypatch, tmp_path):
    domain_dir = _wire(board, domain, monkeypatch, tmp_path)
    _domain_page(domain_dir, "p.md", covers=["src/old/**"], verified_at="cafe0007",
                 title="삭제된 모듈")
    findings = board.lint_domain_freshness(
        runner=_git(tracked=(), post_pin={"src/old/**"}))   # 미추적 + pin 이후 삭제 델타
    assert len(findings) == 1
    label, kind, _detail = findings[0]
    assert (label, kind) == ("삭제된 모듈", "domain-stale")
    assert not any(k == "domain-unverifiable" for _l, k, _d in findings)


def test_deleted_before_pin_flags_unverifiable(board, domain, monkeypatch, tmp_path):
    domain_dir = _wire(board, domain, monkeypatch, tmp_path)
    _domain_page(domain_dir, "p.md", covers=["src/gone/**"], verified_at="cafe0008",
                 title="이전-삭제 모듈")
    findings = board.lint_domain_freshness(runner=_git(tracked=(), post_pin=()))
    assert len(findings) == 1
    label, kind, detail = findings[0]
    assert (label, kind) == ("이전-삭제 모듈", "domain-unverifiable")
    assert "관찰 불가" in detail
    assert not any(k == "domain-stale" for _l, k, _d in findings)


# ── 혼합: 존재 covers(stale) + 부재 covers → 두 finding 병존 ─────────────────

def test_mixed_present_stale_and_absent_yields_both_findings(
        board, domain, monkeypatch, tmp_path):
    domain_dir = _wire(board, domain, monkeypatch, tmp_path)
    _domain_page(domain_dir, "p.md",
                 covers=["src/mod/**", "templates/codex/**"],
                 verified_at="cafe0003", title="혼합")
    kinds = sorted(k for _l, k, _d in board.lint_domain_freshness(
        runner=_git(tracked={"src/mod/**"}, post_pin={"src/mod/**"})))
    assert kinds == ["domain-stale", "domain-unverifiable"]


# ── codex R6: 글롭 충실도 (접두사-없는 글롭·세그먼트 글롭) ────────────────────

# (a) `**/*.py` 접두사-없는 글롭 → 종전 covers_to_pathspec None 으로 통째 skip(false-green) 이던
#     것을 :(glob) 로 직접 판정 → pin 이후 델타 있으면 domain-stale (red-첫-재현).
def test_prefixless_glob_is_judged_not_skipped(board, domain, monkeypatch, tmp_path):
    domain_dir = _wire(board, domain, monkeypatch, tmp_path)
    _domain_page(domain_dir, "p.md", covers=["**/*.py"], verified_at="cafe0009",
                 title="전역 파이썬")
    findings = board.lint_domain_freshness(
        runner=_git(tracked={"**/*.py"}, post_pin={"**/*.py"}))
    assert len(findings) == 1
    label, kind, _detail = findings[0]
    assert (label, kind) == ("전역 파이썬", "domain-stale")   # 종전엔 finding 0(조용한 green)


# (b) `src/*.py` 는 접두사 `src/` 로 넓히지 않고 **원본 글롭을 :(glob) 로** 넘긴다 —
#     git 이 세그먼트 매칭(무관 src/nested/x.txt 배제)하게. 대역은 넘어온 pathspec 을 포착 검증.
def test_covers_pathspecs_passes_original_glob_as_magic_pathspec(domain, tmp_path):
    seen: list[list] = []

    def capture(argv):
        seen.append(list(argv))
        if argv and argv[0] == "rev-parse":
            return (0, "sha1\n")   # 빈 트리 OID 산출(codex R10) — 없으면 diff 미호출
        return (0, "")   # 미추적·델타 0 (분류는 무관·pathspec 형태만 본다)

    domain.covers_pathspecs(["src/*.py"], repo=tmp_path, git_runner=capture,
                            verified_at="cafe0001")
    specs = [a[-1] for a in seen]
    assert ":(glob)src/*.py" in specs               # 원본 글롭이 magic 으로 전달
    assert "src" not in specs and "src/" not in specs  # 손실 접두사 아님


# ── fail-soft / 감도 (silent skip 유지 케이스) ────────────────────────────────

def test_env_error_rev_parse_is_silent_skip(board, domain, monkeypatch, tmp_path):
    domain_dir = _wire(board, domain, monkeypatch, tmp_path)
    _domain_page(domain_dir, "p.md", covers=["src/mod/**"], verified_at="cafe0006")
    assert board.lint_domain_freshness(
        runner=_git(rev_rc=128, tracked={"src/mod/**"})) == []


def test_env_error_merge_base_is_silent_skip(board, domain, monkeypatch, tmp_path):
    domain_dir = _wire(board, domain, monkeypatch, tmp_path)
    _domain_page(domain_dir, "p.md", covers=["src/mod/**"], verified_at="cafe0006")
    assert board.lint_domain_freshness(
        runner=_git(resolves=True, mb_rc=128, tracked={"src/mod/**"})) == []


def test_git_unavailable_is_silent_skip(board, domain, monkeypatch, tmp_path):
    domain_dir = _wire(board, domain, monkeypatch, tmp_path)
    _domain_page(domain_dir, "codex.md", covers=["templates/codex/**"],
                 verified_at="cafe0004")
    assert board.lint_domain_freshness(runner=_raising_git) == []


def test_verified_at_absent_is_skip(board, domain, monkeypatch, tmp_path):
    domain_dir = _wire(board, domain, monkeypatch, tmp_path)
    _domain_page(domain_dir, "codex.md", covers=["templates/codex/**"], verified_at=None)
    assert board.lint_domain_freshness(runner=_git(tracked=(), post_pin=())) == []


def test_no_covers_is_skip(board, domain, monkeypatch, tmp_path):
    domain_dir = _wire(board, domain, monkeypatch, tmp_path)
    _domain_page(domain_dir, "concept.md", covers=None, verified_at="cafe0005")
    assert board.lint_domain_freshness(runner=_git(resolves=False)) == []


def test_all_blank_covers_is_skip(board, domain, monkeypatch, tmp_path):
    # covers 가 전부 공백 글롭 → 매핑 가능 글롭 0 → 코드-무관 skip(false-green 아님).
    domain_dir = _wire(board, domain, monkeypatch, tmp_path)
    _domain_page(domain_dir, "concept.md", covers=["   "], verified_at="cafe0005")
    assert board.lint_domain_freshness(runner=_git(resolves=False)) == []


def test_domain_absent_is_graceful(board, monkeypatch):
    monkeypatch.setattr(board, "_load_domain_module", lambda: None)
    assert board.lint_domain_freshness(runner=_git(resolves=False)) == []


# ── advisory / never-block ────────────────────────────────────────────────────

def test_unverifiable_in_advisory_kinds(board):
    assert "domain-unverifiable" in board._ADVISORY_LINT_KINDS


def test_unverifiable_never_blocks_gate(board, monkeypatch):
    for fn in ("lint_dependencies", "lint_bodies", "lint_ideas", "lint_status",
               "lint_wikilinks", "lint_unstable_refs", "lint_scopes", "lint_domain",
               "lint_adr_lifecycle", "lint_architecture_freshness",
               "lint_status_freshness", "lint_adapter_drift", "lint_render_leak",
               "_run_lint_hooks"):
        monkeypatch.setattr(board, fn, lambda: [])
    monkeypatch.setattr(board, "lint_domain_freshness", lambda: [
        ("codex 어댑터", "domain-unverifiable", "이 저장소에서 freshness 검증 불가")])
    assert board.cmd_lint(SimpleNamespace(gate=True)) == 0
    assert board.cmd_lint(SimpleNamespace(gate=False)) == 1


# ── 유닛: domain.covers_glob_pathspec / covers_pathspecs (R6 :(glob) 매핑) ────

def test_covers_glob_pathspec_wraps_original_glob(domain):
    assert domain.covers_glob_pathspec("src/*.py") == ":(glob)src/*.py"
    assert domain.covers_glob_pathspec("**/x.py") == ":(glob)**/x.py"
    assert domain.covers_glob_pathspec(".project_manager/tools/domain.py") == \
        ":(glob).project_manager/tools/domain.py"


def test_covers_glob_pathspec_blank_is_none(domain):
    assert domain.covers_glob_pathspec("") is None
    assert domain.covers_glob_pathspec("   ") is None


def test_covers_glob_pathspec_escapes_glob_specials(domain):
    # `*`/`**` 는 와일드카드로 보존, git glob 특수문자 `?`/`[`/`]`/백슬래시는 리터럴 이스케이프(codex R7).
    assert domain.covers_glob_pathspec("src/*.py") == ":(glob)src/*.py"          # * 보존
    assert domain.covers_glob_pathspec("a/**/b") == ":(glob)a/**/b"              # ** 보존
    assert domain.covers_glob_pathspec("src/foo?.py") == ":(glob)src/foo\\?.py"  # ? 리터럴
    assert domain.covers_glob_pathspec("src/[ab].py") == ":(glob)src/\\[ab\\].py"  # [] 리터럴
    assert domain.covers_glob_pathspec("a\\b") == ":(glob)a\\\\b"                # 백슬래시 먼저


# ── 실 git: 리터럴 특수문자 covers 정확 매칭 (codex R7·POSIX 전용·throwaway repo) ──
# 대역(mock)은 git glob 시맨틱을 구현하지 않아 이 축을 못 잡는다 — 실 git subprocess 로 검증한다.

def _init_repo(root: Path, *, object_format: str | None = None) -> None:
    init = ["init", "-q"] + ([f"--object-format={object_format}"] if object_format else [])
    for args in (init, ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(root), *args], check=True)


def _commit(root: Path, msg: str) -> str:
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", msg], check=True)
    return subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


def test_downstream_omits_absent_upstream_only_hierarchy(
        board, domain, monkeypatch, tmp_path):
    """형상 A: downstream에 없는 templates 계층은 영구 unverifiable로 올리지 않는다."""
    owner = tmp_path / "owner"
    downstream = tmp_path / "downstream"
    owner.mkdir()
    downstream.mkdir()
    _init_repo(owner)
    _init_repo(downstream)
    (owner / "seed.txt").write_text("owner\n", encoding="utf-8")
    pin = _commit(owner, "owner pin")

    # 라이브 재현처럼 소유 checkout에는 파일이 실재하지만 아직 HEAD 관찰 대상은 아니다.
    covered = owner / "templates" / "opencode" / "plugin.js"
    covered.parent.mkdir(parents=True)
    covered.write_text("plugin\n", encoding="utf-8")
    domain_dir = _wire(board, domain, monkeypatch, downstream)
    monkeypatch.setattr(domain, "REPO", downstream)
    _domain_page(domain_dir, "adapter.md", covers=["templates/opencode/plugin.js"],
                 verified_at=pin, title="upstream adapter", repo="upstream")
    local_conf = downstream / ".project_manager" / "local.conf"
    local_conf.parent.mkdir(parents=True, exist_ok=True)
    local_conf.write_text(f"upstream={owner}\n", encoding="utf-8")
    _commit(downstream, "downstream page")

    assert covered.exists()
    assert not (downstream / "templates").exists()
    assert board.lint_domain_freshness() == []


def test_owner_repo_keeps_full_covers_validation(
        board, domain, monkeypatch, tmp_path):
    """형상 B: 소유 repo에서는 실재 경로가 clean이고 삭제 commit은 stale로 잡힌다."""
    owner = tmp_path / "owner"
    owner.mkdir()
    _init_repo(owner)
    covered = owner / "templates" / "opencode" / "plugin.js"
    covered.parent.mkdir(parents=True)
    covered.write_text("plugin\n", encoding="utf-8")
    pin = _commit(owner, "owner pin")

    domain_dir = _wire(board, domain, monkeypatch, owner)
    monkeypatch.setattr(domain, "REPO", owner)
    _domain_page(domain_dir, "adapter.md", covers=["templates/opencode/plugin.js"],
                 verified_at=pin, title="owner adapter", repo="upstream")
    local_conf = owner / ".project_manager" / "local.conf"
    local_conf.parent.mkdir(parents=True, exist_ok=True)
    local_conf.write_text(f"upstream={owner}\n", encoding="utf-8")
    _commit(owner, "owner page")

    assert board.lint_domain_freshness() == []
    covered.unlink()
    _commit(owner, "delete covered path")
    findings = board.lint_domain_freshness()
    assert [kind for _label, kind, _detail in findings] == ["domain-stale"]


def test_downstream_existing_hierarchy_keeps_absent_finding(
        board, domain, monkeypatch, tmp_path):
    """형상 C: downstream에도 있는 계층의 사라진 covers는 완화하지 않는다."""
    owner = tmp_path / "owner"
    downstream = tmp_path / "downstream"
    owner.mkdir()
    downstream.mkdir()
    _init_repo(owner)
    _init_repo(downstream)
    (owner / "seed.txt").write_text("owner\n", encoding="utf-8")
    pin = _commit(owner, "owner pin")
    existing = downstream / "src" / "existing.py"
    existing.parent.mkdir()
    existing.write_text("present hierarchy\n", encoding="utf-8")

    domain_dir = _wire(board, domain, monkeypatch, downstream)
    monkeypatch.setattr(domain, "REPO", downstream)
    _domain_page(domain_dir, "source.md", covers=["src/removed.py"],
                 verified_at=pin, title="upstream source", repo="upstream")
    local_conf = downstream / ".project_manager" / "local.conf"
    local_conf.parent.mkdir(parents=True, exist_ok=True)
    local_conf.write_text(f"upstream={owner}\n", encoding="utf-8")
    _commit(downstream, "downstream source page")

    findings = board.lint_domain_freshness()
    assert len(findings) == 1
    label, kind, detail = findings[0]
    assert (label, kind) == ("upstream source", "domain-unverifiable")
    assert "src/removed.py" in detail


def test_two_repo_owner_change_closes_unsynced_copy_window(
        board, domain, monkeypatch, tmp_path):
    """2-repo 재현: 원본 변경·PM import 사본 미동기여도 세 freshness 축이 원본 시계로 stale.

    PM 홈은 owner initial commit을 clone해 두 저장소가 같은 pin을 공유한다. 이후 PM 홈에는
    문서만 commit하고 import 사본은 그대로, owner에서만 tools 경로를 바꾼다. 종전 REPO 시계
    질의가 clean임을 음성 대조한 뒤 새 owner 시계의 domain/architecture/status 발화를 확인한다.
    """
    owner = tmp_path / "owner"
    owner.mkdir()
    _init_repo(owner)
    engine = owner / ".project_manager" / "tools" / "engine.py"
    engine.parent.mkdir(parents=True)
    engine.write_text("VERSION = 1\n", encoding="utf-8")
    pin = _commit(owner, "owner initial")

    pm_home = tmp_path / "pm-home"
    subprocess.run(["git", "clone", "-q", str(owner), str(pm_home)], check=True)
    subprocess.run(["git", "-C", str(pm_home), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(pm_home), "config", "user.name", "t"], check=True)
    wiki = pm_home / ".project_manager" / "wiki"
    domain_dir = wiki / "domain"
    _domain_page(domain_dir, "engine.md", covers=[".project_manager/tools/**"],
                 verified_at=pin, title="엔진", repo="upstream")
    for name, doc_type in (("architecture.md", "architecture"), ("status.md", "status")):
        (wiki / name).write_text(
            "---\n"
            f"title: {name}\ntype: {doc_type}\nrepo: upstream\nverified_at: {pin}\n"
            "---\n\nbody\n", encoding="utf-8")
    local_conf = pm_home / ".project_manager" / "local.conf"
    local_conf.write_text(f"upstream={owner}\n", encoding="utf-8")
    _commit(pm_home, "PM docs only")

    # Canonical owner만 변경. PM import copy는 VERSION=1 그대로다.
    engine.write_text("VERSION = 2\n", encoding="utf-8")
    _commit(owner, "owner engine change")
    assert (pm_home / ".project_manager" / "tools" / "engine.py").read_text(
        encoding="utf-8"
    ) == "VERSION = 1\n"

    monkeypatch.setattr(board, "REPO", pm_home)
    monkeypatch.setattr(board, "LOCAL_CONF", local_conf)
    monkeypatch.setattr(board, "ARCHITECTURE_FILE", wiki / "architecture.md")
    monkeypatch.setattr(board, "STATUS_FILE", wiki / "status.md")
    monkeypatch.setattr(domain, "DOMAIN_DIR", domain_dir)
    monkeypatch.setattr(board, "_load_domain_module", lambda: domain)

    # 종전 PM-home 시계는 import 사본 경로 변경이 없어 clean이었다(조용한 오답 창의 대조군).
    assert board._git_commits_between(
        pin, [".project_manager/tools"], repo=pm_home) is False
    assert [kind for _label, kind, _detail in board.lint_domain_freshness()] == [
        "domain-stale"]
    assert [kind for _label, kind, _detail in board.lint_architecture_freshness()] == [
        "architecture-stale"]
    assert [kind for _label, kind, _detail in board.lint_status_freshness()] == [
        "status-stale"]


def test_upstream_owner_unresolved_stays_advisory(
        board, domain, monkeypatch, tmp_path):
    domain_dir = _wire(board, domain, monkeypatch, tmp_path)
    _domain_page(domain_dir, "p.md", covers=["src/**"], verified_at="cafe0001",
                 title="외부소유", repo="upstream")
    # local.conf/upstream 부재 — runner를 호출해 self로 green 흡수하면 안 된다.
    findings = board.lint_domain_freshness(runner=_raising_git)
    assert len(findings) == 1
    label, kind, detail = findings[0]
    assert (label, kind) == ("외부소유", "domain-unverifiable")
    assert "upstream 미설정" in detail and "소유 repo" in detail


def test_explicit_null_owner_is_unverifiable_not_self(
        board, domain, monkeypatch, tmp_path):
    """`repo: null`은 키 부재가 아니다 — self 시계로 흡수하지 않고 advisory."""
    domain_dir = _wire(board, domain, monkeypatch, tmp_path)
    domain_dir.mkdir(parents=True)
    page = domain_dir / "null-owner.md"
    page.write_text(
        "---\ntitle: null소유\ntype: concept\ncovers: [src/**]\n"
        "verified_at: cafe0001\nrepo: null\n---\n\nbody\n",
        encoding="utf-8",
    )
    findings = board.lint_domain_freshness(runner=_raising_git)
    assert len(findings) == 1
    label, kind, detail = findings[0]
    assert (label, kind) == ("null소유", "domain-unverifiable")
    assert "repo(null) 미지원" in detail


@pytest.mark.parametrize("repo_literal", ['""', "false", "0", "[]", "unsupported"])
def test_unsupported_repo_values_including_falsy_stay_advisory(
        board, domain, monkeypatch, tmp_path, repo_literal):
    domain_dir = _wire(board, domain, monkeypatch, tmp_path)
    _domain_page(domain_dir, "p.md", covers=["src/**"], verified_at="cafe0001",
                 title="잘못된소유", repo=repo_literal)
    # false/0/[]를 `owner or "self"`로 흡수하면 runner가 호출돼 조용한 green이 된다.
    findings = board.lint_domain_freshness(runner=_raising_git)
    assert len(findings) == 1
    label, kind, detail = findings[0]
    assert (label, kind) == ("잘못된소유", "domain-unverifiable")
    assert "repo(" in detail and "미지원" in detail


def test_date_freshness_lint_uses_owner_repo_runner(
        board, domain, monkeypatch, tmp_path):
    """updated/date 축도 upstream runner를 써 self의 false stale/green을 모두 배제한다."""
    self_repo = tmp_path / "self"
    owner_repo = tmp_path / "owner"
    self_repo.mkdir()
    owner_repo.mkdir()
    domain_dir = self_repo / "domain"
    _domain_page(domain_dir, "p.md", covers=["src/**"], title="소유페이지",
                 repo="upstream")
    # helper의 기본 updated가 없으므로 date 축 대상이 되도록 명시 교체한다.
    page_path = domain_dir / "p.md"
    page_path.write_text(
        page_path.read_text(encoding="utf-8").replace(
            "type: concept\n", "type: concept\nupdated: 2026-06-19\n"),
        encoding="utf-8")
    monkeypatch.setattr(domain, "DOMAIN_DIR", domain_dir)
    monkeypatch.setattr(board, "_load_domain_module", lambda: domain)
    monkeypatch.setattr(
        board, "_freshness_owner_repo",
        lambda owner: (owner_repo, None) if owner == "upstream" else (self_repo, None))

    dates = {
        self_repo: "2026-06-20T00:00:00Z\n",   # 잘못된 self 시계면 false stale
        owner_repo: "2026-06-18T00:00:00Z\n",  # 실제 owner 시계는 fresh
    }
    seen: list[Path] = []

    def runner_for(repo):
        seen.append(Path(repo))
        return lambda _argv: (0, dates[Path(repo)])

    monkeypatch.setattr(domain, "_real_git_runner", runner_for)
    assert not any(kind == "stale" for _label, kind, _detail in board.lint_domain())
    assert seen == [owner_repo]

    dates[self_repo] = "2026-06-18T00:00:00Z\n"   # 잘못된 self 시계면 false green
    dates[owner_repo] = "2026-06-20T00:00:00Z\n"  # 실제 owner 시계는 stale
    seen.clear()
    assert [kind for _label, kind, _detail in board.lint_domain()] == ["stale"]
    assert seen == [owner_repo]


@pytest.mark.parametrize(
    ("case", "detail_fragment"),
    [
        ("url", "URL"),
        ("moved", "부재/이동"),
        ("non_git", "git checkout 아님"),
        ("damaged_git_marker", "git checkout 검증 실패"),
    ],
)
def test_unusable_upstream_paths_stay_advisory(
        board, domain, monkeypatch, tmp_path, case, detail_fragment):
    domain_dir = _wire(board, domain, monkeypatch, tmp_path)
    _domain_page(domain_dir, "p.md", covers=["src/**"], verified_at="cafe0001",
                 title="외부소유", repo="upstream")
    local_conf = tmp_path / ".project_manager" / "local.conf"
    local_conf.parent.mkdir(parents=True)
    candidate = tmp_path / "owner"
    if case == "url":
        upstream = "https://example.invalid/owner.git"
    elif case == "moved":
        upstream = str(candidate)  # 존재하지 않음 = 이동/삭제된 checkout.
    else:
        candidate.mkdir()
        if case == "damaged_git_marker":
            (candidate / ".git").mkdir()  # 표식만 있고 rev-parse는 실패하는 손상 경로.
        upstream = str(candidate)
    local_conf.write_text(f"upstream={upstream}\n", encoding="utf-8")

    findings = board.lint_domain_freshness(runner=_raising_git)
    assert len(findings) == 1
    _label, kind, detail = findings[0]
    assert kind == "domain-unverifiable"
    assert detail_fragment in detail


@pytest.mark.parametrize("expansion", ["posix", "windows"])
def test_unexpandable_upstream_user_stays_advisory(
        board, domain, monkeypatch, tmp_path, expansion):
    """없는 `~user` upstream 은 **두 플랫폼 모두** '경로 해소 실패' 로 수렴한다.

    POSIX `expanduser()` 는 `pwd` 조회 실패를 RuntimeError 로 올리지만 Windows 는 없는 사용자도
    `C:\\Users\\<name>` 으로 조립해 준다 — 그러면 해소가 성공한 척 다음 단계로 내려가 "경로
    부재/이동" 이라는 **다른 사유**로 갈렸다(Windows 실측). 확장 seam 에 Windows 동작을 주입해
    그 분기를 Linux 에서 태운다."""
    if expansion == "windows":
        _use_windows_expanduser(monkeypatch)
    domain_dir = _wire(board, domain, monkeypatch, tmp_path)
    _domain_page(domain_dir, "p.md", covers=["src/**"], verified_at="cafe0001",
                 title="외부소유", repo="upstream")
    local_conf = tmp_path / ".project_manager" / "local.conf"
    local_conf.parent.mkdir(parents=True)
    local_conf.write_text(
        "upstream=~codex_user_that_must_not_exist_0470/owner\n", encoding="utf-8")

    findings = board.lint_domain_freshness(runner=_raising_git)
    assert len(findings) == 1
    _label, kind, detail = findings[0]
    assert kind == "domain-unverifiable"
    assert "경로 해소 실패" in detail


def test_current_user_home_expansion_keeps_resolving(
        board, domain, monkeypatch, tmp_path):
    """`~`(현재 사용자)에는 실재 검사를 걸지 않는다 — `~user` 판정이 정상 형상을 막지 않는다.

    홈 디렉터리가 아직 없는 형상까지 해소 실패로 접으면 판정이 과차단이다. `~` 확장 결과는
    그대로 다음 단계(경로 실재)로 내려가고, 사유도 그 단계의 것이어야 한다."""
    _use_windows_expanduser(monkeypatch)
    domain_dir = _wire(board, domain, monkeypatch, tmp_path)
    _domain_page(domain_dir, "p.md", covers=["src/**"], verified_at="cafe0001",
                 title="외부소유", repo="upstream")
    local_conf = tmp_path / ".project_manager" / "local.conf"
    local_conf.parent.mkdir(parents=True)
    local_conf.write_text("upstream=~/owner\n", encoding="utf-8")

    findings = board.lint_domain_freshness(runner=_raising_git)
    assert len(findings) == 1
    _label, kind, detail = findings[0]
    assert kind == "domain-unverifiable"
    assert "경로 해소 실패" not in detail        # 확장 자체는 성립
    assert "부재/이동" in detail                # 다음 단계(경로 실재)의 사유


def test_solo_self_owner_naturally_uses_same_repo(
        board, domain, monkeypatch, tmp_path):
    _init_repo(tmp_path)
    src = tmp_path / "src" / "engine.py"
    src.parent.mkdir()
    src.write_text("v1\n", encoding="utf-8")
    pin = _commit(tmp_path, "initial")
    domain_dir = _wire(board, domain, monkeypatch, tmp_path)
    _domain_page(domain_dir, "p.md", covers=["src/**"], verified_at=pin, title="solo")
    _commit(tmp_path, "docs")
    src.write_text("v2\n", encoding="utf-8")
    _commit(tmp_path, "engine change")
    findings = board.lint_domain_freshness()
    assert [kind for _label, kind, _detail in findings] == ["domain-stale"]


def test_repin_migration_replaces_owner_and_anchor_for_all_current_truth_docs(
        board, domain, monkeypatch, tmp_path):
    wiki = tmp_path / ".project_manager" / "wiki"
    domain_dir = wiki / "domain"
    domain_dir.mkdir(parents=True)
    for name, doc_type in (("architecture.md", "architecture"), ("status.md", "status")):
        (wiki / name).write_text(
            f"---\ntitle: {name}\ntype: {doc_type}\nverified_at: \"deadbeef\"\n---\n",
            encoding="utf-8")
    page = _domain_page(domain_dir, "p.md", covers=["src/**"],
                        verified_at="deadbeef", title="page")
    monkeypatch.setattr(board, "REPO", tmp_path)
    monkeypatch.setattr(board, "ARCHITECTURE_FILE", wiki / "architecture.md")
    monkeypatch.setattr(board, "STATUS_FILE", wiki / "status.md")
    monkeypatch.setattr(domain, "DOMAIN_DIR", domain_dir)
    monkeypatch.setattr(board, "_load_domain_module", lambda: domain)
    full = "cafe0001" + "0" * 32
    results = board.repin_verified_at(full, "upstream")
    assert [state for _path, state in results] == ["updated", "updated", "updated"]
    for path in (wiki / "architecture.md", wiki / "status.md", page):
        text = path.read_text(encoding="utf-8")
        assert "repo: upstream" in text
        assert f'verified_at: "{full}"' in text
        assert "deadbeef" not in text


def test_repin_page_selector_changes_only_selected_document_bytes(
        board, domain, monkeypatch, tmp_path, capsys):
    """선택 문서는 실제 갱신되고 나머지 현재-진실 문서는 byte-for-byte 불변이다."""
    _init_repo(tmp_path)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("v1\n", encoding="utf-8")
    pin = _commit(tmp_path, "initial")
    wiki = tmp_path / ".project_manager" / "wiki"
    domain_dir = wiki / "domain"
    domain_dir.mkdir(parents=True)
    architecture = wiki / "architecture.md"
    status = wiki / "status.md"
    architecture.write_text(
        "---\ntitle: architecture\nrepo: upstream\nverified_at: deadbeef\n---\n\narch body\n",
        encoding="utf-8")
    status.write_text(
        "---\ntitle: status\nrepo: upstream\nverified_at: deadbeef\n---\n\nstatus body\n",
        encoding="utf-8")
    page = _domain_page(
        domain_dir, "engine.md", covers=["src/**"], verified_at="deadbeef",
        title="engine", repo="upstream")
    before = {path: path.read_bytes() for path in (architecture, status, page)}
    monkeypatch.setattr(board, "REPO", tmp_path)
    monkeypatch.setattr(board, "ARCHITECTURE_FILE", architecture)
    monkeypatch.setattr(board, "STATUS_FILE", status)
    monkeypatch.setattr(domain, "DOMAIN_DIR", domain_dir)
    monkeypatch.setattr(board, "_load_domain_module", lambda: domain)
    monkeypatch.setattr(board, "_guard_worktree_misanchor", lambda _action: False)

    rc = board.main([
        "verified-at-repin", "--repo", "self", "--sha", pin,
        "--page", ".project_manager/wiki/architecture.md",
    ])

    assert rc == 0
    assert architecture.read_bytes() != before[architecture]
    assert f'verified_at: "{pin}"' in architecture.read_text(encoding="utf-8")
    assert status.read_bytes() == before[status]
    assert page.read_bytes() == before[page]
    assert "1개 문서 재핀" in capsys.readouterr().out


def test_repin_page_selector_validates_entire_selected_set_before_writing(
        board, monkeypatch, tmp_path):
    """선택 집합 하나가 invalid면 같은 선택 집합의 valid 문서도 쓰지 않는다."""
    wiki = tmp_path / ".project_manager" / "wiki"
    wiki.mkdir(parents=True)
    architecture = wiki / "architecture.md"
    status = wiki / "status.md"
    architecture_original = (
        "---\ntitle: architecture\nrepo: self\nverified_at: deadbeef\n---\n")
    status_original = "frontmatter 없음\n"
    architecture.write_text(architecture_original, encoding="utf-8")
    status.write_text(status_original, encoding="utf-8")
    monkeypatch.setattr(board, "REPO", tmp_path)
    monkeypatch.setattr(board, "ARCHITECTURE_FILE", architecture)
    monkeypatch.setattr(board, "STATUS_FILE", status)

    full = "cafe0001" + "0" * 32
    results = dict(board.repin_verified_at(
        full,
        "upstream",
        pages=[
            ".project_manager/wiki/architecture.md",
            ".project_manager/wiki/status.md",
        ],
    ))

    assert results[architecture] == "not-written:validation-failed"
    assert results[status] == "error:no-frontmatter"
    assert architecture.read_text(encoding="utf-8") == architecture_original
    assert status.read_text(encoding="utf-8") == status_original


@pytest.mark.parametrize(
    "selected",
    ["", ".project_manager/wiki/domain/does-not-exist.md"],
    ids=["empty", "missing"],
)
def test_repin_page_selector_rejects_vacuous_inputs_without_changes(
        board, monkeypatch, tmp_path, capsys, selected):
    """빈/비존재 선택자가 0개 검증 성공으로 퇴화하지 않고 rc!=0·무변경을 보장한다."""
    _init_repo(tmp_path)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("v1\n", encoding="utf-8")
    pin = _commit(tmp_path, "initial")
    wiki = tmp_path / ".project_manager" / "wiki"
    wiki.mkdir(parents=True)
    architecture = wiki / "architecture.md"
    status = wiki / "status.md"
    original = "---\ntitle: page\nrepo: upstream\nverified_at: deadbeef\n---\n"
    architecture.write_text(original, encoding="utf-8")
    status.write_text(original, encoding="utf-8")
    before = {path: path.read_bytes() for path in (architecture, status)}
    monkeypatch.setattr(board, "REPO", tmp_path)
    monkeypatch.setattr(board, "ARCHITECTURE_FILE", architecture)
    monkeypatch.setattr(board, "STATUS_FILE", status)
    monkeypatch.setattr(board, "_guard_worktree_misanchor", lambda _action: False)

    rc = board.main([
        "verified-at-repin", "--repo", "self", "--sha", pin,
        "--page", selected,
    ])

    assert rc != 0
    assert {path: path.read_bytes() for path in before} == before
    err = capsys.readouterr().err
    assert "--page" in err and "무변경" in err


@pytest.mark.parametrize(
    "case",
    [
        "no-covers",
        "draft",
        "readme",
        "parent-path",
        "absolute-path",
        "outside-domain",
    ],
)
def test_repin_page_selector_rejects_noncanonical_targets_without_any_write(
        board, domain, monkeypatch, tmp_path, capsys, case):
    """각 거부 가드는 rc!=0뿐 아니라 모든 현재-진실 문서의 byte 불변까지 보장한다."""
    _init_repo(tmp_path)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("v1\n", encoding="utf-8")
    pin = _commit(tmp_path, "initial")
    wiki = tmp_path / ".project_manager" / "wiki"
    domain_dir = wiki / "domain"
    domain_dir.mkdir(parents=True)
    architecture = wiki / "architecture.md"
    status = wiki / "status.md"
    for path in (architecture, status):
        path.write_text(
            "---\ntitle: fixed\nrepo: self\nverified_at: deadbeef\n---\n",
            encoding="utf-8",
        )
    valid = _domain_page(
        domain_dir, "valid.md", covers=["src/**"], verified_at="deadbeef",
        title="valid", repo="self")
    outside = wiki / "outside.md"
    outside.write_text(
        "---\ntitle: outside\ncovers:\n  - \"src/**\"\n"
        "repo: self\nverified_at: deadbeef\n---\n",
        encoding="utf-8",
    )
    documents = [architecture, status, valid, outside]
    selected_path = domain_dir / f"{case}.md"
    if case == "no-covers":
        selected_path.write_text(
            "---\ntitle: no covers\nrepo: self\nverified_at: deadbeef\n---\n",
            encoding="utf-8",
        )
        documents.append(selected_path)
        selected = selected_path.relative_to(tmp_path).as_posix()
    elif case == "draft":
        selected_path.write_text(
            "---\ntitle: draft\nstatus: draft\ncovers:\n  - \"src/**\"\n"
            "repo: self\nverified_at: deadbeef\n---\n",
            encoding="utf-8",
        )
        documents.append(selected_path)
        selected = selected_path.relative_to(tmp_path).as_posix()
    elif case == "readme":
        selected_path = domain_dir / "README.md"
        selected_path.write_text(
            "---\ntitle: readme\ncovers:\n  - \"src/**\"\n"
            "repo: self\nverified_at: deadbeef\n---\n",
            encoding="utf-8",
        )
        documents.append(selected_path)
        selected = selected_path.relative_to(tmp_path).as_posix()
    elif case == "parent-path":
        selected = ".project_manager/wiki/domain/../domain/valid.md"
    elif case == "absolute-path":
        selected = str(valid.resolve())
    else:
        selected = outside.relative_to(tmp_path).as_posix()

    monkeypatch.setattr(board, "REPO", tmp_path)
    monkeypatch.setattr(board, "ARCHITECTURE_FILE", architecture)
    monkeypatch.setattr(board, "STATUS_FILE", status)
    monkeypatch.setattr(domain, "DOMAIN_DIR", domain_dir)
    monkeypatch.setattr(board, "_load_domain_module", lambda: domain)
    monkeypatch.setattr(board, "_guard_worktree_misanchor", lambda _action: False)
    before = {path: path.read_bytes() for path in documents}

    rc = board.main([
        "verified-at-repin", "--repo", "self", "--sha", pin,
        "--page", selected,
    ])

    assert rc != 0
    assert {path: path.read_bytes() for path in before} == before
    err = capsys.readouterr().err
    assert "--page" in err and "무변경" in err


def test_page_selector_reuses_domain_module_selection_contract(
        board, monkeypatch, tmp_path):
    """DOMAIN_DIR·비페이지 집합·parse_page가 선택자의 단일 규칙 원천이다."""
    custom_domain_dir = tmp_path / "custom-domain"
    custom_domain_dir.mkdir()
    page = custom_domain_dir / "page.md"
    page.write_text("parser owns this contract\n", encoding="utf-8")
    non_page = custom_domain_dir / "INDEX.md"
    non_page.write_text("not a page\n", encoding="utf-8")
    parse_calls = []

    class FakeDomain:
        DOMAIN_DIR = custom_domain_dir
        _NON_PAGE_FILES = frozenset({"INDEX.md"})
        DRAFT_STATUS = "unapproved"

        @staticmethod
        def parse_page(path):
            parse_calls.append(path)
            return {"path": path, "covers": ["src/**"], "status": None}

    monkeypatch.setattr(board, "REPO", tmp_path)
    monkeypatch.setattr(board, "ARCHITECTURE_FILE", tmp_path / "architecture.md")
    monkeypatch.setattr(board, "STATUS_FILE", tmp_path / "status.md")
    monkeypatch.setattr(board, "_load_domain_module", lambda: FakeDomain)

    selected = page.relative_to(tmp_path).as_posix()
    assert board._verified_at_targets([selected]) == [page.resolve()]
    assert parse_calls == [page.resolve()]
    with pytest.raises(board._VerifiedAtPageSelectionError, match="canonical domain"):
        board._verified_at_targets([non_page.relative_to(tmp_path).as_posix()])
    assert parse_calls == [page.resolve()]


def test_page_selector_rejects_domain_symlink_to_repo_internal_non_domain_document(
        board, domain, monkeypatch, tmp_path):
    """domain 링크의 해소 대상이 domain 밖이면 유효 문서여도 선택·쓰기를 거부한다."""
    wiki = tmp_path / ".project_manager" / "wiki"
    domain_dir = wiki / "domain"
    domain_dir.mkdir(parents=True)
    outside = wiki / "outside.md"
    original = (
        "---\ntitle: outside\ncovers:\n  - \"src/**\"\n"
        "repo: self\nverified_at: deadbeef\n---\n\nbody\n"
    )
    outside.write_text(original, encoding="utf-8")
    link = domain_dir / "link.md"
    link.symlink_to(outside)
    monkeypatch.setattr(board, "REPO", tmp_path)
    monkeypatch.setattr(board, "ARCHITECTURE_FILE", wiki / "architecture.md")
    monkeypatch.setattr(board, "STATUS_FILE", wiki / "status.md")
    monkeypatch.setattr(domain, "DOMAIN_DIR", domain_dir)
    monkeypatch.setattr(board, "_load_domain_module", lambda: domain)
    selected = ".project_manager/wiki/domain/link.md"

    with pytest.raises(board._VerifiedAtPageSelectionError, match="domain 문서가 아님"):
        board.backfill_verified_at("cafe0001", pages=[selected])

    results = board.repin_verified_at(
        "cafe0001" + "0" * 32, "self", pages=[selected])
    assert len(results) == 1
    assert results[0][1].startswith("error:enumeration:")
    assert outside.read_text(encoding="utf-8") == original


def test_page_selector_rejects_symlink_loop_as_controlled_selection_error(
        board, domain, monkeypatch, tmp_path):
    """symlink loop 는 traceback 이 아니라 통제된 선택 오류로 정규화되고 아무 문서도 안 바뀐다.

    Python 3.12 의 `Path.resolve()` 는 loop 에 `RuntimeError`("Symlink loop from …") 를 내고
    3.13+ 는 `OSError`(ELOOP) 를 낸다 — 둘 중 어느 하한에서도 CLI 가 traceback 으로 죽지 않아야
    한다(실측: 3.12.3 상호/자기 루프 모두 RuntimeError)."""
    wiki = tmp_path / ".project_manager" / "wiki"
    domain_dir = wiki / "domain"
    page = _domain_page(
        domain_dir, "engine.md", covers=["src/**"], verified_at="deadbeef",
        title="engine", repo="self")
    untouched = page.read_text(encoding="utf-8")
    left = domain_dir / "loop-a.md"
    right = domain_dir / "loop-b.md"
    left.symlink_to(right)
    right.symlink_to(left)
    monkeypatch.setattr(board, "REPO", tmp_path)
    monkeypatch.setattr(board, "ARCHITECTURE_FILE", wiki / "architecture.md")
    monkeypatch.setattr(board, "STATUS_FILE", wiki / "status.md")
    monkeypatch.setattr(domain, "DOMAIN_DIR", domain_dir)
    monkeypatch.setattr(board, "_load_domain_module", lambda: domain)
    selected = ".project_manager/wiki/domain/loop-a.md"

    with pytest.raises(board._VerifiedAtPageSelectionError, match="symlink loop"):
        board._verified_at_targets([selected])
    with pytest.raises(board._VerifiedAtPageSelectionError, match="symlink loop"):
        board.backfill_verified_at("cafe0001", pages=[selected])

    results = board.repin_verified_at(
        "cafe0001" + "0" * 32, "self", pages=[selected])
    assert len(results) == 1
    assert results[0][1].startswith("error:enumeration:")
    assert page.read_text(encoding="utf-8") == untouched


def test_page_selector_resolves_normal_domain_path_and_deduplicates_symlink_alias(
        board, domain, monkeypatch, tmp_path):
    """정상 상대경로와 같은 파일의 링크 별칭은 해소 경로 하나로만 검증·쓴다."""
    wiki = tmp_path / ".project_manager" / "wiki"
    domain_dir = wiki / "domain"
    page = _domain_page(
        domain_dir, "engine.md", covers=["src/**"], verified_at="deadbeef",
        title="engine", repo="self")
    alias = domain_dir / "engine-alias.md"
    alias.symlink_to(page)
    monkeypatch.setattr(board, "REPO", tmp_path)
    monkeypatch.setattr(board, "ARCHITECTURE_FILE", wiki / "architecture.md")
    monkeypatch.setattr(board, "STATUS_FILE", wiki / "status.md")
    monkeypatch.setattr(domain, "DOMAIN_DIR", domain_dir)
    monkeypatch.setattr(board, "_load_domain_module", lambda: domain)
    selected = [
        ".project_manager/wiki/domain/engine.md",
        ".project_manager/wiki/domain/engine-alias.md",
    ]

    assert board._verified_at_targets(selected) == [page.resolve()]
    results = board.repin_verified_at(
        "cafe0001" + "0" * 32, "upstream", pages=selected)

    assert results == [(page.resolve(), "updated")]
    assert 'verified_at: "cafe0001' in page.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("frontmatter", "parsed_type"),
    [
        ("- covers\n- src/**", list),
        ("scalar-frontmatter", str),
    ],
    ids=["top-level-list", "top-level-scalar"],
)
def test_page_selector_converts_non_mapping_frontmatter_to_controlled_selection_error(
        board, domain, monkeypatch, tmp_path, capsys, frontmatter, parsed_type):
    """공용 파서 반환 계약은 유지하되 선택 경계가 non-mapping을 파일 지목 오류로 바꾼다."""
    wiki = tmp_path / ".project_manager" / "wiki"
    domain_dir = wiki / "domain"
    domain_dir.mkdir(parents=True)
    page = domain_dir / "malformed.md"
    original = f"---\n{frontmatter}\n---\n\nbody\n"
    page.write_text(original, encoding="utf-8")
    monkeypatch.setattr(board, "REPO", tmp_path)
    monkeypatch.setattr(board, "ARCHITECTURE_FILE", wiki / "architecture.md")
    monkeypatch.setattr(board, "STATUS_FILE", wiki / "status.md")
    monkeypatch.setattr(domain, "DOMAIN_DIR", domain_dir)
    monkeypatch.setattr(board, "_load_domain_module", lambda: domain)
    full = "cafe0001" + "0" * 32
    monkeypatch.setattr(board, "_canonical_commit_oid", lambda *_args, **_kwargs: full)
    selected = ".project_manager/wiki/domain/malformed.md"

    parsed, _body = board.load_ticket(page)
    assert isinstance(parsed, parsed_type)
    rc = board.cmd_verified_at_backfill(
        SimpleNamespace(sha="cafe0001", dry_run=False, pages=[selected]))

    assert rc != 0
    err = capsys.readouterr().err
    assert selected in err
    assert "domain 문서 실소비 파싱 실패" in err
    results = board.repin_verified_at(full, "self", pages=[selected])
    assert len(results) == 1
    assert "error:enumeration:" in results[0][1]
    assert "domain 문서 실소비 파싱 실패" in results[0][1]
    assert page.read_text(encoding="utf-8") == original


def test_repin_only_replaces_or_inserts_column_zero_keys(board):
    old = (
        "---\n"
        "title: fixture\n"
        "metadata:\n"
        "  repo: nested-owner\n"
        "  verified_at: nested-pin\n"
        "notes: |\n"
        "  repo: prose-owner\n"
        "  verified_at: prose-pin\n"
        "---\n"
        "\nbody\n"
    )
    full = "cafe0001" + "0" * 32
    replaced = board._replace_freshness_pin(old, full, "upstream")
    assert replaced is not None
    # 중첩 mapping/block scalar 내용은 byte-for-byte 보존되고, 최상위 두 키가 별도로 삽입된다.
    assert "  repo: nested-owner\n  verified_at: nested-pin\n" in replaced
    assert "  repo: prose-owner\n  verified_at: prose-pin\n" in replaced
    assert replaced.count("\nrepo: upstream\n") == 1
    assert replaced.count(f'\nverified_at: "{full}"\n') == 1


def test_repin_indented_fence_in_block_scalar_preserves_content(
        board, monkeypatch, tmp_path):
    """들여쓴 `---`는 block scalar 본문이며 frontmatter 종료로 오인하지 않는다."""
    path = tmp_path / "block.md"
    original = (
        "---\n"
        "title: fixture\n"
        "notes: |\n"
        "  first line\n"
        "  ---\n"
        "  repo: prose-owner\n"
        "repo: self\n"
        "verified_at: deadbeef\n"
        "---\n"
        "\nbody\n"
    )
    path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(
        board, "_verified_at_backfill_targets", lambda **_kwargs: [path])

    full = "cafe0001" + "0" * 32
    assert board.repin_verified_at(full, "upstream") == [(path, "updated")]
    replaced = path.read_text(encoding="utf-8")
    assert "notes: |\n  first line\n  ---\n  repo: prose-owner\n" in replaced
    assert board._frontmatter_mapping(replaced)["notes"] == (
        "first line\n---\nrepo: prose-owner\n")


def test_repin_quoted_freshness_keys_do_not_create_duplicates(board):
    original = (
        "---\n"
        "title: quoted\n"
        "'repo': self\n"
        '"verified_at": deadbeef\n'
        "---\n"
    )
    full = "cafe0001" + "0" * 32
    replaced = board._replace_freshness_pin(original, full, "upstream")
    assert replaced is not None
    key_lines = replaced.splitlines()
    assert sum(bool(re.match(r"""^(?:repo|["']repo["'])\s*:""", line))
               for line in key_lines) == 1
    assert sum(bool(re.match(r"""^(?:verified_at|["']verified_at["'])\s*:""", line))
               for line in key_lines) == 1
    assert board._frontmatter_mapping(replaced)["repo"] == "upstream"
    assert board._frontmatter_mapping(replaced)["verified_at"] == full


def test_repin_multiline_yaml_value_aborts_all_without_damage(
        board, monkeypatch, tmp_path):
    """다중행 scalar 첫 줄만 바꾸는 변환은 YAML 의미 검증에서 거부하고 전 파일 무변경."""
    good = tmp_path / "good.md"
    multiline = tmp_path / "multiline.md"
    good_original = "---\ntitle: good\nrepo: self\nverified_at: deadbeef\n---\n"
    multiline_original = (
        "---\ntitle: folded\nrepo: >\n  upstream\n"
        "verified_at: deadbeef\n---\n\nbody\n"
    )
    good.write_text(good_original, encoding="utf-8")
    multiline.write_text(multiline_original, encoding="utf-8")
    monkeypatch.setattr(
        board, "_verified_at_backfill_targets",
        lambda **_kwargs: [good, multiline],
    )

    full = "cafe0001" + "0" * 32
    results = board.repin_verified_at(full, "upstream")
    states = {path: state for path, state in results}
    assert states[good] == "not-written:validation-failed"
    assert states[multiline].startswith("error:yaml:")
    assert "안전 교체 불가" in states[multiline]
    assert good.read_text(encoding="utf-8") == good_original
    assert multiline.read_text(encoding="utf-8") == multiline_original


@pytest.mark.parametrize(
    ("name", "invalid_original"),
    [
        (
            "closing-fence-without-newline.md",
            "---\ntitle: invalid\nrepo: self\nverified_at: deadbeef\n---",
        ),
        (
            "closing-fence-with-spaces.md",
            "---\ntitle: invalid\nrepo: self\nverified_at: deadbeef\n---   \n\nbody\n",
        ),
    ],
)
def test_repin_aborts_all_when_result_is_unreadable_by_consumer_parser(
        board, monkeypatch, tmp_path, name, invalid_original):
    """repin 자체 YAML 검증만 통과해도 load_ticket 문법이 못 읽으면 전 파일 사전 차단."""
    good = tmp_path / "good.md"
    invalid = tmp_path / name
    good_original = "---\ntitle: good\nrepo: self\nverified_at: deadbeef\n---\n"
    good.write_text(good_original, encoding="utf-8")
    invalid.write_text(invalid_original, encoding="utf-8")
    monkeypatch.setattr(
        board, "_verified_at_backfill_targets",
        lambda **_kwargs: [good, invalid],
    )

    with pytest.raises(ValueError):
        board.load_ticket(invalid)
    full = "cafe0001" + "0" * 32
    results = dict(board.repin_verified_at(full, "upstream"))

    assert results[good] == "not-written:validation-failed"
    assert results[invalid].startswith("error:yaml:")
    assert "실소비 파서 파싱 실패" in results[invalid]
    assert good.read_text(encoding="utf-8") == good_original
    assert invalid.read_text(encoding="utf-8") == invalid_original


def test_repin_cli_dry_run_writes_nothing(
        board, monkeypatch, tmp_path):
    _init_repo(tmp_path)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("v1\n", encoding="utf-8")
    pin = _commit(tmp_path, "initial")
    wiki = tmp_path / ".project_manager" / "wiki"
    wiki.mkdir(parents=True)
    architecture = wiki / "architecture.md"
    original = "---\ntitle: architecture\ntype: architecture\nverified_at: deadbeef\n---\n"
    architecture.write_text(original, encoding="utf-8")
    monkeypatch.setattr(board, "REPO", tmp_path)
    monkeypatch.setattr(board, "ARCHITECTURE_FILE", architecture)
    monkeypatch.setattr(board, "STATUS_FILE", wiki / "missing-status.md")
    monkeypatch.setattr(board, "DOMAIN_PY", tmp_path / "missing-domain.py")
    monkeypatch.setattr(board, "_load_domain_module", lambda: None)
    monkeypatch.setattr(board, "_guard_worktree_misanchor", lambda _action: False)

    rc = board.main([
        "verified-at-repin", "--repo", "self", "--sha", pin, "--dry-run"])
    assert rc == 0
    assert architecture.read_text(encoding="utf-8") == original


def test_repin_cli_invalid_sha_fails_loud_without_writing(
        board, monkeypatch, tmp_path, capsys):
    _init_repo(tmp_path)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("v1\n", encoding="utf-8")
    _commit(tmp_path, "initial")
    wiki = tmp_path / ".project_manager" / "wiki"
    wiki.mkdir(parents=True)
    architecture = wiki / "architecture.md"
    original = "---\ntitle: architecture\ntype: architecture\nverified_at: deadbeef\n---\n"
    architecture.write_text(original, encoding="utf-8")
    monkeypatch.setattr(board, "REPO", tmp_path)
    monkeypatch.setattr(board, "ARCHITECTURE_FILE", architecture)
    monkeypatch.setattr(board, "STATUS_FILE", wiki / "missing-status.md")
    monkeypatch.setattr(board, "_load_domain_module", lambda: None)
    monkeypatch.setattr(board, "_guard_worktree_misanchor", lambda _action: False)

    rc = board.main([
        "verified-at-repin", "--repo", "self", "--sha", "definitely-not-a-sha"])
    assert rc == 1
    assert "검증되지 않는다" in capsys.readouterr().err
    assert architecture.read_text(encoding="utf-8") == original


def test_repin_validation_failure_is_nonzero_and_changes_nothing(
        board, monkeypatch, tmp_path, capsys):
    """한 대상 frontmatter 오류면 valid 앞 대상도 쓰지 않는 validate-all-first 계약."""
    _init_repo(tmp_path)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("v1\n", encoding="utf-8")
    pin = _commit(tmp_path, "initial")
    good = tmp_path / "good.md"
    broken = tmp_path / "broken.md"
    good_original = "---\ntitle: good\nverified_at: deadbeef\n---\n"
    broken_original = "frontmatter 없음\n"
    good.write_text(good_original, encoding="utf-8")
    broken.write_text(broken_original, encoding="utf-8")
    monkeypatch.setattr(board, "REPO", tmp_path)
    monkeypatch.setattr(board, "_verified_at_backfill_targets",
                        lambda **_kwargs: [good, broken])
    monkeypatch.setattr(board, "_guard_worktree_misanchor", lambda _action: False)

    rc = board.main(["verified-at-repin", "--repo", "self", "--sha", pin])
    assert rc != 0
    assert good.read_text(encoding="utf-8") == good_original
    assert broken.read_text(encoding="utf-8") == broken_original
    err = capsys.readouterr().err
    assert "전 대상 검증 실패" in err and "무변경" in err


def test_atomic_write_text_flush_failure_preserves_original_bytes_and_cleans_temp(
        board, monkeypatch, tmp_path):
    """임시 파일 flush 실패는 대상 원본 byte와 디렉토리에 어떤 흔적도 남기지 않는다."""
    path = tmp_path / "page.md"
    original = b"---\r\ntitle: original\r\n---\r\n"
    path.write_bytes(original)

    def fail_flush(_fd):
        raise OSError("injected flush failure")

    monkeypatch.setattr(board.os, "fsync", fail_flush)
    with pytest.raises(OSError, match="injected flush failure"):
        board._atomic_write_text(path, "---\ntitle: replacement\n---\n")

    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


def test_repin_write_failure_reports_changed_prefix_and_stops(
        board, monkeypatch, tmp_path):
    """두 번째 임시 파일 flush 실패 시 원본 보존 + 앞 변경/뒤 미변경 보고가 정확하다."""
    paths = [tmp_path / name for name in ("a.md", "b.md", "c.md")]
    original = "---\ntitle: page\nverified_at: deadbeef\n---\n"
    for path in paths:
        write_lf(path, original)
    monkeypatch.setattr(board, "_verified_at_backfill_targets",
                        lambda **_kwargs: paths)
    real_fsync = board.os.fsync
    fsync_calls = 0

    def fail_second(_fd):
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 2:
            raise OSError("injected write failure")
        return real_fsync(_fd)

    monkeypatch.setattr(board.os, "fsync", fail_second)
    full = "cafe0001" + "0" * 32
    results = board.repin_verified_at(full, "self")
    assert [state.split(":", 2)[0] for _path, state in results] == [
        "updated", "error", "not-written"]
    assert f'verified_at: "{full}"' in paths[0].read_text(encoding="utf-8")
    assert paths[1].read_bytes() == original.encode()
    assert paths[2].read_bytes() == original.encode()
    assert not list(tmp_path.glob(".*.tmp"))


def test_repin_cli_write_failure_is_nonzero_and_names_changed_files(
        board, monkeypatch, tmp_path, capsys):
    """쓰기 실패 CLI는 rc≠0과 실제 교체된 prefix만 명시한다(실패 파일 원본 보존)."""
    paths = [tmp_path / name for name in ("a.md", "b.md", "c.md")]
    original = "---\ntitle: page\nverified_at: deadbeef\n---\n"
    for path in paths:
        write_lf(path, original)
    full = "cafe0001" + "0" * 32
    monkeypatch.setattr(board, "REPO", tmp_path)
    monkeypatch.setattr(board, "_repo_head_sha", lambda _repo=None: full)
    monkeypatch.setattr(board, "_verified_at_backfill_targets",
                        lambda **_kwargs: paths)
    real_fsync = board.os.fsync
    fsync_calls = 0

    def fail_second(_fd):
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 2:
            raise OSError("injected write failure")
        return real_fsync(_fd)

    monkeypatch.setattr(board.os, "fsync", fail_second)
    rc = board.cmd_verified_at_repin(
        SimpleNamespace(repo="self", sha=None, dry_run=False))
    assert rc != 0
    captured = capsys.readouterr()
    assert "쓰기 실패" in captured.err
    assert "이미 변경된 파일: a.md" in captured.err
    assert "b.md" in captured.err
    assert "이미 변경된 파일: a.md, b.md" not in captured.err
    assert "not-written:write-failed: c.md" in captured.out
    assert paths[1].read_bytes() == original.encode()


@pytest.mark.parametrize("expansion", ["posix", "windows"])
def test_repin_cli_unexpandable_upstream_user_fails_before_writing(
        board, monkeypatch, tmp_path, capsys, expansion):
    """`~없는사용자` upstream은 CLI에서 명시 실패하고 대상 파일을 건드리지 않는다.

    Windows 확장(`C:\\Users\\<없는사용자>` 조립)을 주입해도 **같은 판정**에 도달해야 한다 —
    종전엔 phantom 홈 경로로 내려가 다른 사유로 갈렸다(Windows 실측)."""
    if expansion == "windows":
        _use_windows_expanduser(monkeypatch)
    wiki = tmp_path / ".project_manager" / "wiki"
    wiki.mkdir(parents=True)
    architecture = wiki / "architecture.md"
    original = "---\ntitle: architecture\nverified_at: deadbeef\n---\n"
    architecture.write_text(original, encoding="utf-8")
    local_conf = tmp_path / ".project_manager" / "local.conf"
    local_conf.write_text(
        "upstream=~codex_user_that_must_not_exist_0470/owner\n", encoding="utf-8")
    monkeypatch.setattr(board, "REPO", tmp_path)
    monkeypatch.setattr(board, "LOCAL_CONF", local_conf)
    monkeypatch.setattr(board, "ARCHITECTURE_FILE", architecture)
    monkeypatch.setattr(board, "_guard_worktree_misanchor", lambda _action: False)

    rc = board.main(["verified-at-repin", "--repo", "upstream"])
    assert rc != 0
    assert architecture.read_text(encoding="utf-8") == original
    assert "경로 해소 실패" in capsys.readouterr().err


def test_repin_domain_parse_error_aborts_enumeration_and_changes_nothing(
        board, domain, monkeypatch, tmp_path, capsys):
    """대상 domain 문서 파싱 실패도 rc!=0·전 파일 무변경인 열거 검증 실패다."""
    _init_repo(tmp_path)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("v1\n", encoding="utf-8")
    pin = _commit(tmp_path, "initial")
    wiki = tmp_path / ".project_manager" / "wiki"
    domain_dir = wiki / "domain"
    domain_dir.mkdir(parents=True)
    architecture = wiki / "architecture.md"
    original = "---\ntitle: architecture\nrepo: self\nverified_at: deadbeef\n---\n"
    architecture.write_text(original, encoding="utf-8")
    broken = domain_dir / "broken.md"
    broken_original = "---\ntitle: [invalid\n---\n"
    broken.write_text(broken_original, encoding="utf-8")
    monkeypatch.setattr(board, "REPO", tmp_path)
    monkeypatch.setattr(board, "ARCHITECTURE_FILE", architecture)
    monkeypatch.setattr(board, "STATUS_FILE", wiki / "missing-status.md")
    monkeypatch.setattr(domain, "DOMAIN_DIR", domain_dir)
    monkeypatch.setattr(board, "_load_domain_module", lambda: domain)
    monkeypatch.setattr(board, "_guard_worktree_misanchor", lambda _action: False)

    rc = board.main(["verified-at-repin", "--repo", "self", "--sha", pin])
    assert rc != 0
    assert architecture.read_text(encoding="utf-8") == original
    assert broken.read_text(encoding="utf-8") == broken_original
    err = capsys.readouterr().err
    assert "error:enumeration:" in err
    assert "broken.md" in err
    assert "전 대상 검증 실패" in err and "무변경" in err


def test_repin_domain_load_failure_aborts_enumeration_and_changes_nothing(
        board, monkeypatch, tmp_path, capsys):
    """실재 domain.py 로드 실패는 strict 열거 오류이며 architecture도 쓰지 않는다."""
    _init_repo(tmp_path)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("v1\n", encoding="utf-8")
    pin = _commit(tmp_path, "initial")
    wiki = tmp_path / ".project_manager" / "wiki"
    wiki.mkdir(parents=True)
    architecture = wiki / "architecture.md"
    original = "---\ntitle: architecture\nrepo: self\nverified_at: deadbeef\n---\n"
    architecture.write_text(original, encoding="utf-8")
    domain_py = tmp_path / ".project_manager" / "tools" / "domain.py"
    domain_py.parent.mkdir(parents=True)
    domain_py.write_text("raise RuntimeError('injected load failure')\n", encoding="utf-8")
    monkeypatch.setattr(board, "REPO", tmp_path)
    monkeypatch.setattr(board, "ARCHITECTURE_FILE", architecture)
    monkeypatch.setattr(board, "STATUS_FILE", wiki / "missing-status.md")
    monkeypatch.setattr(board, "DOMAIN_PY", domain_py)
    monkeypatch.setattr(board, "_load_domain_module", lambda: None)
    monkeypatch.setattr(board, "_guard_worktree_misanchor", lambda _action: False)

    rc = board.main(["verified-at-repin", "--repo", "self", "--sha", pin])
    assert rc != 0
    assert architecture.read_text(encoding="utf-8") == original
    err = capsys.readouterr().err
    assert "error:enumeration:" in err
    assert "domain.py 로드 실패" in err
    assert "전 대상 검증 실패" in err and "무변경" in err


def _ls(root: Path, spec: str) -> str:
    return subprocess.run(["git", "-C", str(root), "ls-files", "--", spec],
                          capture_output=True, text=True).stdout


@_posix_only
def test_literal_question_mark_not_treated_as_wildcard(domain, tmp_path):
    # 리터럴 `?` 든 covers 는 다른 파일(fooX.py)에 오매칭 안 함.
    _init_repo(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "fooX.py").write_text("x\n", encoding="utf-8")
    pin = _commit(tmp_path, "init")
    # red-첫: escape 안 한 :(glob)src/foo?.py 는 fooX.py 에 오매칭(? 와일드카드).
    assert "src/fooX.py" in _ls(tmp_path, ":(glob)src/foo?.py")
    # escape 후(covers_glob_pathspec): 리터럴 파일(부재)만 → 매칭 0.
    assert _ls(tmp_path, domain.covers_glob_pathspec("src/foo?.py")).strip() == ""
    # covers_pathspecs 판정: 리터럴 파일 부재 → absent(정확·fooX.py 오매칭 아님).
    present, absent, _u = domain.covers_pathspecs(["src/foo?.py"], repo=tmp_path, verified_at=pin)
    assert present == [] and absent == ["src/foo?.py"]


@_posix_only
def test_literal_brackets_not_treated_as_charclass(domain, tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x\n", encoding="utf-8")
    pin = _commit(tmp_path, "init")
    assert "src/a.py" in _ls(tmp_path, ":(glob)src/[ab].py")            # red-첫: 오매칭
    assert _ls(tmp_path, domain.covers_glob_pathspec("src/[ab].py")).strip() == ""
    present, absent, _u = domain.covers_pathspecs(["src/[ab].py"], repo=tmp_path, verified_at=pin)
    assert present == [] and absent == ["src/[ab].py"]


@_posix_only
def test_literal_special_char_file_matched_when_present(domain, tmp_path):
    # 과-이스케이프 아님 대조군: 리터럴 `?` 파일이 실재하면 escaped 글롭이 그 파일을 매칭 → present.
    _init_repo(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "q?.py").write_text("x\n", encoding="utf-8")
    pin = _commit(tmp_path, "init")
    present, absent, _u = domain.covers_pathspecs(["src/q?.py"], repo=tmp_path, verified_at=pin)
    assert present == ["src/q?.py"] and absent == []


# ── codex R8: 지원 covers 문법 = 두 방언(우리 matcher vs git :(glob)) 동일성 증명 ──

def test_is_supported_covers_glob_classification(domain):
    # 동등 증명된 형태만 True (정확 경로·단일 `*`·리터럴-prefix `/**`·leading/middle `**`·`..foo` 비-탈출).
    for g in ["src/b.py", "src/a/x.py", "src", "src/**", "src/nested/**", "src/*.py",
              "src/*", "*.py", "**/x.py", "src/**/x.py", "**/*.py", "..foo/bar"]:
        assert domain._is_supported_covers_glob(g), g
    for g in ["**.py", "src/**.py", "a**b", "x**", "**foo/bar", "a/***/b"]:
        assert not domain._is_supported_covers_glob(g), g
    # codex R10 MF-2: repo-밖/절대 경로 — git pathspec 이 rc128 silent skip 되므로 미지원.
    for g in ["/etc/passwd", "/abs/**", "../outside/**", "a/../b/**", "C:/Users/x", "C:\\win"]:
        assert not domain._is_supported_covers_glob(g), g
    # codex R20: wildcard-prefix + trailing `/**` — parent-포함 매칭이 git `…/**`(슬래시 필수)와 갈림.
    for g in ["*/**", "a*b/**", "src/*/**", "*/literal/**", "a*/**", "**/x/**"]:
        assert not domain._is_supported_covers_glob(g), g


def test_empty_tree_oid_object_format_neutral(domain):
    # codex R10 MF-1: object-format 감지 → 맞는 빈 트리 OID (SHA-1/SHA-256 중립).
    assert domain._empty_tree_oid(lambda a: (0, "sha1\n")) == \
        "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
    assert domain._empty_tree_oid(lambda a: (0, "sha256\n")) == \
        "6ef19b41225c5369f1c104d45d8d85efa9b057b53b14b4b9b939dd74decc5321"
    assert domain._empty_tree_oid(lambda a: (0, "sha999\n")) is None   # 미지 포맷
    assert domain._empty_tree_oid(lambda a: (128, "")) is None         # 감지 실패 → fail-soft


def test_repo_outside_glob_page_flags_unverifiable(board, domain, monkeypatch, tmp_path):
    # codex R10 MF-2: repo-밖/절대 경로 covers → 조용히 skip(rc128) 아니라 unverifiable advisory.
    domain_dir = _wire(board, domain, monkeypatch, tmp_path)
    _domain_page(domain_dir, "p.md", covers=["../outside/**"], verified_at="cafe0011",
                 title="repo밖")
    findings = board.lint_domain_freshness(runner=_git(tracked=set(), post_pin=set()))
    assert len(findings) == 1
    _label, kind, detail = findings[0]
    assert kind == "domain-unverifiable" and "../outside/**" in detail
    assert not any(k == "domain-stale" for _l, k, _d in findings)


def test_covers_pathspecs_repo_outside_is_unmappable(domain, tmp_path):
    present, absent, unmappable = domain.covers_pathspecs(
        ["/abs/**", "../up/**", "src/mod/**"], repo=tmp_path,
        git_runner=_fs_git(tracked={"src/mod/**"}), verified_at="cafe0001")
    assert present == ["src/mod/**"]
    assert sorted(unmappable) == ["../up/**", "/abs/**"]


# 실 git property: 지원 글롭은 우리 matcher 판정 == git :(glob) 판정(파일 집합 동일)임을 증명한다.
# **루트 파일·prefix-레벨 파일 포함**(codex R20) — wildcard-prefix+`/**` parent-포함 divergence 를
# property 가 실제로 노출하게(옛 파일 셋은 이 클래스를 못 잡았다). bare-dir `src` 는 별도 test.
_PROP_FILES = ["top.txt", "axb", "a.py", "src/b.py", "src/foo", "src/nested/c.py",
               "src/nested/deep/d.py", "pkg/e.py", "x.py", "src/x.py", "src/a/x.py",
               "src/sub.py.bak"]
_PROP_EQUIVALENT_GLOBS = ["src/b.py", "src/a/x.py", "src/**", "src/nested/**", "src/*.py",
                          "src/*", "*.py", "**/x.py", "src/**/x.py", "**/*.py"]


def test_supported_globs_match_git_equivalently(domain, tmp_path):
    _init_repo(tmp_path)
    for f in _PROP_FILES:
        p = tmp_path / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n", encoding="utf-8")
    _commit(tmp_path, "init")
    for glob in _PROP_EQUIVALENT_GLOBS:
        spec = domain.covers_glob_pathspec(glob)
        assert spec is not None, glob   # 지원 → 변환됨
        git_set = {l for l in _ls(tmp_path, spec).splitlines() if l}
        our_set = {f for f in _PROP_FILES if domain._path_matches_covers(f, [glob])}
        assert our_set == git_set, (glob, sorted(our_set), sorted(git_set))


# codex R20 red-첫: wildcard-prefix + trailing `/**` 는 우리 matcher(parent-포함)가 루트/prefix-레벨
# 실파일을 매칭하나 git `:(glob)…/**`(슬래시 필수)는 제외 → 두 방언 불일치. 오번역 대신 변환 거부.
_PROP_DIVERGENT_GLOBS = {
    "*/**": {"axb", "top.py"},        # 루트 파일 (git 제외)
    "a*b/**": {"axb"},                # `a*b` 매칭 루트 파일
    "src/*/**": {"src/foo", "src/b.py"},  # prefix-레벨 파일
}


def test_wildcard_prefix_trailing_starstar_divergence_refused(domain, tmp_path):
    _init_repo(tmp_path)
    for f in ["top.py", "axb", "src/foo", "src/b.py", "src/nested/c.py"]:
        p = tmp_path / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n", encoding="utf-8")
    files = ["top.py", "axb", "src/foo", "src/b.py", "src/nested/c.py"]
    _commit(tmp_path, "init")
    for glob, our_only_expected in _PROP_DIVERGENT_GLOBS.items():
        raw = ":(glob)" + domain._escape_glob_literals(glob)   # 검증 우회·git 실의미
        git_set = {l for l in _ls(tmp_path, raw).splitlines() if l}
        our_set = {f for f in files if domain._path_matches_covers(f, [glob])}
        assert our_set - git_set >= our_only_expected, (glob, sorted(our_set), sorted(git_set))
        assert domain.covers_glob_pathspec(glob) is None   # R20: 변환 거부 → unmappable


def test_wildcard_prefix_starstar_page_flags_unverifiable(board, domain, monkeypatch, tmp_path):
    # codex R20: `*/**` 페이지 → 조용한 false-green 아니라 domain-unverifiable advisory.
    domain_dir = _wire(board, domain, monkeypatch, tmp_path)
    _domain_page(domain_dir, "p.md", covers=["*/**"], verified_at="cafe0012", title="와일드prefix")
    findings = board.lint_domain_freshness(runner=_git(tracked=set(), post_pin=set()))
    assert len(findings) == 1
    _label, kind, detail = findings[0]
    assert kind == "domain-unverifiable" and "*/**" in detail
    assert not any(k == "domain-stale" for _l, k, _d in findings)


def test_bare_dir_exact_path_git_subtree(domain, tmp_path):
    # 정확 경로가 dir 이면 git :(glob) 은 subtree 매칭(우리 exact 매치보다 넓음) — freshness 는
    # over-warn 이라 안전(false-green 아님). 동작 명시(codex R8).
    _init_repo(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "b.py").write_text("x\n", encoding="utf-8")
    _commit(tmp_path, "init")
    git_set = {l for l in _ls(tmp_path, domain.covers_glob_pathspec("src")).splitlines() if l}
    assert git_set == {"src/b.py"}                                   # git: subtree
    assert not domain._path_matches_covers("src/b.py", ["src"])      # 우리 matcher: exact(불일치)


# 미지원 형태(비-경계 `**`) red-첫: `**.py` 는 우리 matcher 가 중첩 .py 를 매치하나 git :(glob) 은
# `**` 를 세그먼트 못 넘어 miss → 두 방언 불일치. 오번역 대신 변환 거부(None) → 호출부 unmappable.
# (특수문자 파일명 없음 → POSIX 게이트 불요·git wildmatch 는 OS 무관.)
def test_non_boundary_starstar_mistranslation_refused(domain, tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "src" / "nested").mkdir(parents=True)
    (tmp_path / "src" / "nested" / "deep.py").write_text("x\n", encoding="utf-8")
    (tmp_path / "top.py").write_text("x\n", encoding="utf-8")
    _commit(tmp_path, "init")
    # 우리 matcher: `**.py` ⊇ 중첩 파일.
    assert domain._path_matches_covers("src/nested/deep.py", ["**.py"])
    # git :(glob)**.py (검증 우회·직접): `**` 를 세그먼트 못 넘어 중첩 miss (R7 코드였다면 오번역).
    git_raw = {l for l in _ls(tmp_path, ":(glob)**.py").splitlines() if l}
    assert "src/nested/deep.py" not in git_raw   # git miss = 오번역 시 false-green
    # R8: 두 방언 불일치라 변환 거부 → unmappable(정직 보고).
    assert domain.covers_glob_pathspec("**.py") is None


def test_unsupported_glob_page_flags_unverifiable(board, domain, monkeypatch, tmp_path):
    # 미지원-only covers 페이지 → 조용히 skip(false-green) 아니라 domain-unverifiable advisory(codex R8).
    domain_dir = _wire(board, domain, monkeypatch, tmp_path)
    _domain_page(domain_dir, "p.md", covers=["**.py"], verified_at="cafe0010", title="비경계")
    findings = board.lint_domain_freshness(runner=_git(tracked={"**.py"}, post_pin={"**.py"}))
    assert len(findings) == 1
    _label, kind, detail = findings[0]
    assert kind == "domain-unverifiable" and "미지원" in detail and "**.py" in detail
    assert not any(k == "domain-stale" for _l, k, _d in findings)


# ── codex R9: 판정 축을 HEAD 트리 기준으로 (mutable index/staged 무관) ────────────

def test_staged_add_not_in_head_is_absent(domain, tmp_path):
    # staged-추가(HEAD 미포함)만으로는 present 아님 — 종전 ls-files 면 present=순간 false-green(red-첫).
    _init_repo(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "committed.py").write_text("x\n", encoding="utf-8")
    pin = _commit(tmp_path, "init")
    (tmp_path / "src" / "staged.py").write_text("n\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "src/staged.py"], check=True)  # stage only
    # 대조: ls-files(index)=staged.py 봄(종전 present 오판) / HEAD 트리는 안 봄.
    assert "src/staged.py" in _ls(tmp_path, ":(glob)src/*.py")
    present, absent, _u = domain.covers_pathspecs(
        ["src/staged.py"], repo=tmp_path, verified_at=pin)
    assert present == [] and absent == ["src/staged.py"]   # HEAD 트리 기준 → absent


def test_staged_delete_stays_present(domain, tmp_path):
    # staged-삭제(index 서 빠져도 HEAD 엔 있음) → present 유지(HEAD 트리 기준·codex R9).
    _init_repo(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "keep.py").write_text("x\n", encoding="utf-8")
    pin = _commit(tmp_path, "init")
    subprocess.run(["git", "-C", str(tmp_path), "rm", "-q", "--cached", "src/keep.py"], check=True)
    # 대조: ls-files(index)=keep.py 안 봄 / HEAD 트리는 있음.
    assert "src/keep.py" not in _ls(tmp_path, ":(glob)src/*.py")
    present, absent, _u = domain.covers_pathspecs(
        ["src/keep.py"], repo=tmp_path, verified_at=pin)
    assert present == ["src/keep.py"] and absent == []     # HEAD 트리 기준 → present 유지


# ── codex R10 MF-1: SHA-256 repo 에서도 판정 동작 (object-format 중립 빈 트리 OID) ──

@_sha256_only
def test_sha256_repo_head_tree_judgment(domain, tmp_path):
    # SHA-256 repo — 종전 SHA-1 하드코딩이면 diff rc≠0 → 전축 silent skip. R10 은 포맷 감지로 정상.
    _init_repo(tmp_path, object_format="sha256")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "committed.py").write_text("x\n", encoding="utf-8")
    pin = _commit(tmp_path, "init")
    # tracked(HEAD 트리) → present.
    present, absent, _u = domain.covers_pathspecs(
        ["src/committed.py"], repo=tmp_path, verified_at=pin)
    assert present == ["src/committed.py"] and absent == []
    # staged-add → HEAD 미포함 → absent (staged 무관·HEAD-tree 일관).
    (tmp_path / "src" / "staged.py").write_text("n\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "src/staged.py"], check=True)
    present, absent, _u = domain.covers_pathspecs(
        ["src/staged.py"], repo=tmp_path, verified_at=pin)
    assert present == [] and absent == ["src/staged.py"]


def test_covers_pathspecs_tracked_is_present(domain, tmp_path):
    present, absent, unmappable = domain.covers_pathspecs(
        ["src/mod/**"], repo=tmp_path, git_runner=_fs_git(tracked={"src/mod/**"}),
        verified_at="cafe0001")
    assert present == ["src/mod/**"] and absent == [] and unmappable == []


def test_covers_pathspecs_deleted_after_pin_is_present(domain, tmp_path):
    present, absent, unmappable = domain.covers_pathspecs(
        ["src/old/**"], repo=tmp_path,
        git_runner=_fs_git(tracked=(), post_pin={"src/old/**"}), verified_at="cafe0007")
    assert present == ["src/old/**"] and absent == [] and unmappable == []


def test_covers_pathspecs_never_or_pre_pin_is_absent(domain, tmp_path):
    present, absent, unmappable = domain.covers_pathspecs(
        ["templates/codex/**", "src/gone/**"], repo=tmp_path,
        git_runner=_fs_git(tracked=(), post_pin=()), verified_at="cafe0008")
    assert present == [] and sorted(absent) == ["src/gone/**", "templates/codex/**"]
    assert unmappable == []


def test_covers_pathspecs_distinguishes_present_from_absent(domain, tmp_path):
    present, absent, unmappable = domain.covers_pathspecs(
        ["src/mod/**", "templates/codex/**"], repo=tmp_path,
        git_runner=_fs_git(tracked={"src/mod/**"}, post_pin=()), verified_at="cafe0001")
    assert present == ["src/mod/**"] and absent == ["templates/codex/**"]
    assert unmappable == []


def test_covers_pathspecs_blank_glob_is_skipped(domain, tmp_path):
    # 빈/공백 글롭 = 패턴 아님 → 제외(unmappable 아님·codex R8). 미지원 형태만 unmappable.
    present, absent, unmappable = domain.covers_pathspecs(
        ["  ", "src/mod/**"], repo=tmp_path,
        git_runner=_fs_git(tracked={"src/mod/**"}), verified_at="cafe0001")
    assert present == ["src/mod/**"] and unmappable == []


def test_covers_pathspecs_unsupported_glob_is_unmappable(domain, tmp_path):
    # 비-경계 `**`(`**.py`) = 두 방언 의미 다름 → 오번역 대신 unmappable(codex R8).
    present, absent, unmappable = domain.covers_pathspecs(
        ["**.py", "src/mod/**"], repo=tmp_path,
        git_runner=_fs_git(tracked={"src/mod/**"}), verified_at="cafe0001")
    assert present == ["src/mod/**"] and unmappable == ["**.py"]


def test_covers_pathspecs_git_error_skips_pathspec(domain, tmp_path):
    present, absent, unmappable = domain.covers_pathspecs(
        ["src/mod/**"], repo=tmp_path,
        git_runner=lambda argv: (128, "fatal"), verified_at="cafe0001")
    assert present == [] and absent == [] and unmappable == []   # git 오류=skip(≠unmappable)


# ── 유닛: board._is_hex_sha / _sha_anchor_status (형식·해소·선조 3조건) ───────

def test_is_hex_sha_accepts_and_rejects(board):
    assert board._is_hex_sha("fa1c398") and board._is_hex_sha(
        "3cf0f731b11b3a0adc739116cd767cd6fee14558") and board._is_hex_sha("DEADBEEF")
    for ref in ("HEAD", "main", "master", "v1.4.2", "origin/main", "abc", ""):
        assert not board._is_hex_sha(ref), ref


# 실 git 2.43 실측 ambiguous stderr 형태 (codex R22·`_rev_parse_ambiguous` 소문자 부분일치 대상).
_REAL_AMBIGUOUS_STDERR = ("error: short object ID 25b0 is ambiguous\n힌트: The candidates are:\n"
                          "힌트:   25b02cb commit ...\n힌트:   25b0950 commit ...\n"
                          "fatal: Needed a single revision")


def _anchor_git(rev=0, mb=0, oid=None, ambig=False):
    def runner(argv):
        if argv and argv[0] == "rev-parse":
            if ambig and "--quiet" not in argv:
                # non-quiet 진단 재질의 — `--quiet` 가 억제한 모호 stderr(3-tuple·codex R22).
                return (128, "", _REAL_AMBIGUOUS_STDERR)
            if rev != 0:
                return (rev, "")
            target = argv[-1].split("^")[0]
            return (0, (oid if oid is not None else target.ljust(40, "0")) + "\n")
        if argv and argv[0] == "merge-base":
            return (mb, "")
        return (0, "")
    return runner


# `_sha_anchor_status` 는 `(verdict, full_oid)` 반환 — full_oid 는 OK 일 때만(codex R16).
def test_sha_anchor_status_ok_returns_full_oid(board):
    verdict, full_oid = board._sha_anchor_status("cafe0001", runner=_anchor_git(0, 0))
    assert verdict == board._ANCHOR_OK
    assert full_oid == "cafe0001".ljust(40, "0")   # rev-parse 해소 canonical full OID


def test_sha_anchor_status_unresolved(board):
    assert board._sha_anchor_status("deadbeef", runner=_anchor_git(1, 0)) == \
        (board._ANCHOR_UNRESOLVED, None)


def test_sha_anchor_status_non_ancestor(board):
    assert board._sha_anchor_status("beef0042", runner=_anchor_git(0, 1)) == \
        (board._ANCHOR_NON_ANCESTOR, None)


def test_sha_anchor_status_non_sha(board):
    assert board._sha_anchor_status("HEAD", runner=_anchor_git(0, 0)) == \
        (board._ANCHOR_NON_SHA, None)


def test_sha_anchor_status_hex_named_ref_is_non_sha(board):
    # codex R5: rev-parse 해소(rc0)하나 OID 가 입력 prefix 아님(ref 로 해소) → non-sha.
    ref_oid = "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b"
    assert board._sha_anchor_status(
        "deadbeef", runner=_anchor_git(0, 0, oid=ref_oid))[0] == board._ANCHOR_NON_SHA
    assert board._sha_anchor_status(
        "deadbeef", runner=_anchor_git(0, 0, oid="deadbeef" + "0" * 32))[0] == board._ANCHOR_OK


def test_sha_anchor_status_unknown_env_error(board):
    assert board._sha_anchor_status("deadbeef", runner=_anchor_git(128, 0))[0] == board._ANCHOR_UNKNOWN
    assert board._sha_anchor_status("deadbeef", runner=_anchor_git(0, 128))[0] == board._ANCHOR_UNKNOWN


def test_sha_anchor_status_ambiguous(board):
    # codex R22: --quiet rev-parse 실패(rc1·stderr 억제) + non-quiet 진단이 모호 stderr → ambiguous
    # (env/unresolved 아님). rc≥2 로 억제되는 git 버전도 진단이 잡게(양쪽 rev 확인).
    for rev in (1, 128):
        assert board._sha_anchor_status(
            "25b0abc", runner=_anchor_git(rev, 0, ambig=True))[0] == board._ANCHOR_AMBIGUOUS


def test_rev_parse_ambiguous_signal(board):
    # non-quiet stderr 3-tuple 에 모호 신호가 있으면 True, 2-tuple(대역 stderr 없음)·예외는 False.
    assert board._rev_parse_ambiguous(
        "25b0", runner=lambda a: (128, "", _REAL_AMBIGUOUS_STDERR)) is True
    assert board._rev_parse_ambiguous("25b0", runner=lambda a: (1, "")) is False
    assert board._rev_parse_ambiguous("25b0", runner=_raising_git) is False


def test_sha_anchor_status_unknown_empty_or_raise(board):
    assert board._sha_anchor_status("", runner=_anchor_git())[0] == board._ANCHOR_UNKNOWN
    assert board._sha_anchor_status("cafe0001", runner=_raising_git)[0] == board._ANCHOR_UNKNOWN


# codex R16: 검증 후 하류 명령이 **canonical full OID** 를 쓰는지 argv 로 단언 (원 입력 재해석 제거).
def test_downstream_commands_use_full_oid_not_input(board, domain, monkeypatch, tmp_path):
    domain_dir = _wire(board, domain, monkeypatch, tmp_path)
    _domain_page(domain_dir, "p.md", covers=["src/mod/**"], verified_at="cafe0001",
                 title="모듈")
    full = "cafe0001" + "f" * 32   # 입력 prefix 를 갖는 40자 canonical OID(입력과는 다름)
    seen: list[list] = []

    def runner(argv):
        seen.append(list(argv))
        if argv and argv[0] == "rev-parse":
            if "--show-object-format" in argv:
                return (0, "sha1\n")
            if "--verify" in argv:
                return (0, full + "\n")   # 해소 → full OID
        if argv and argv[0] == "merge-base":
            return (0, "")                # ancestor
        if argv and argv[0] == "diff":
            return (0, "src/mod/x.py\n")  # HEAD-tree present
        if argv and argv[0] == "log":
            return (0, "abc commit\n")    # delta → stale
        return (0, "")

    findings = board.lint_domain_freshness(runner=runner)
    assert any(k == "domain-stale" for _l, k, _d in findings)
    # merge-base·log range 가 full OID 를 쓰고, bare 입력("cafe0001" 단독)은 argv element 로 안 온다.
    mb = [a for a in seen if a and a[0] == "merge-base"]
    logs = [a for a in seen if a and a[0] == "log"]
    assert mb and full in mb[0]                              # merge-base full OID
    assert logs and any(f"{full}..HEAD" in a for a in logs)  # log range full OID
    # rev-parse 의 `cafe0001^{commit}` 외엔 bare 입력이 하류 argv element 로 재사용되지 않는다.
    downstream = [tok for a in (mb + logs) for tok in a]
    assert "cafe0001" not in downstream and "cafe0001..HEAD" not in downstream
