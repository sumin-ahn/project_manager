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
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

# 리터럴 `?`/`[]` 파일명은 Windows 에서 불법이라 실 git 이스케이프 테스트는 POSIX 전용 (codex R7).
_posix_only = pytest.mark.skipif(os.name != "posix",
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
                 title="페이지") -> Path:
    domain_dir.mkdir(parents=True, exist_ok=True)
    fm = [f"title: {title}", "type: concept"]
    if covers:
        fm.append("covers:")
        # covers 글롭을 따옴표로 감싼다 — `**/*.py` 처럼 `*` 로 시작하면 YAML 이 alias 로 오파싱한다
        # (실제 도메인 페이지도 leading-`*` 글롭은 quote 필요·엔진 이슈 아님).
        fm.extend(f'  - "{c}"' for c in covers)
    if verified_at is not None:
        fm.append(f"verified_at: {verified_at}")
    path = domain_dir / name
    path.write_text("---\n" + "\n".join(fm) + "\n---\n\nbody\n", encoding="utf-8")
    return path


def _wire(board, domain, monkeypatch, repo: Path):
    domain_dir = repo / "domain"
    monkeypatch.setattr(board, "REPO", repo)
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


def _ls(root: Path, spec: str) -> str:
    return subprocess.run(["git", "-C", str(root), "ls-files", "--", spec],
                          capture_output=True, text=True).stdout


@_posix_only
def test_literal_question_mark_not_treated_as_wildcard(domain, tmp_path):
    # 리터럴 `?` 든 covers 는 다른 파일(fooX.py)에 오매칭 안 함.
    _init_repo(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "fooX.py").write_text("x\n")   # ? 자리 다른 문자 — 오매칭 후보
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
    (tmp_path / "src" / "a.py").write_text("x\n")   # [ab] char-class 라면 a.py 오매칭
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
    (tmp_path / "src" / "q?.py").write_text("x\n")   # 리터럴 ? 파일 실재(POSIX)
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
        p.write_text("x\n")
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
        p.write_text("x\n")
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
    (tmp_path / "src" / "b.py").write_text("x\n")
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
    (tmp_path / "src" / "nested" / "deep.py").write_text("x\n")   # 중첩 .py
    (tmp_path / "top.py").write_text("x\n")
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
    (tmp_path / "src" / "committed.py").write_text("x\n")
    pin = _commit(tmp_path, "init")
    (tmp_path / "src" / "staged.py").write_text("n\n")
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
    (tmp_path / "src" / "keep.py").write_text("x\n")
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
    (tmp_path / "src" / "committed.py").write_text("x\n")
    pin = _commit(tmp_path, "init")
    # tracked(HEAD 트리) → present.
    present, absent, _u = domain.covers_pathspecs(
        ["src/committed.py"], repo=tmp_path, verified_at=pin)
    assert present == ["src/committed.py"] and absent == []
    # staged-add → HEAD 미포함 → absent (staged 무관·HEAD-tree 일관).
    (tmp_path / "src" / "staged.py").write_text("n\n")
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
