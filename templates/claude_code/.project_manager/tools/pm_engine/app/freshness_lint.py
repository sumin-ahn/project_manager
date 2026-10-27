"""현재-진실 문서 freshness lint (층: app).

`verified_at` sha 의 해소·조상 판정과 그 sha 이후 매핑 경로 커밋 유무로 architecture.md·status.md 의
재검증 필요를 판정한다. 페이지 `repo:` 의 소유 저장소 해소를 함께 소유한다.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from pm_engine.infra import conf_store, git, pm_paths, ticket_store


# architecture.md·status.md 의 verified_at 매핑 경로 = 엔진 코드 트리. 이 문서들이 판정
# 대상으로 삼는 코드다. 디렉토리 전체를 pathspec 으로 잡아 over-approx(과경고 < 미경고·
# 리터럴 디렉토리 prefix) — 그 트리 어느 파일이 sha 이후 커밋돼도 "재검증해" 신호.
_CURRENT_TRUTH_ENGINE_PATHS: tuple[str, ...] = (".project_manager/tools",)


def _git_commits_between(home, sha: str, pathspecs: list[str], *,
                         runner=None, repo: Path | None = None) -> bool | None:
    """`<sha>..HEAD` 에 `pathspecs` 경로 커밋이 있으면 True·없으면 False·판정불가 None.

    verified_at 판정의 핵심 — `git log --oneline <sha>..HEAD -- <pathspecs>` 출력이 비지
    않으면 그 sha 이후 매핑 경로가 바뀐 것(stale). 판정불가(None):
      - sha 빈값·pathspec 전부 빈 → None (판정 대상 없음).
      - git 바이너리 부재·미지 sha(rc≠0·`bad revision`) → None (skip).
    실행 예외는 전파한다. None 은 "판정불가(unknown)"라 호출부가 finding 을 내지 않는다(현행 freshness fail-soft
    성격·신규 clone 무탈). `runner` 는 테스트 hermetic seam(argv→(rc, stdout))·미주입
    시 실 subprocess(`git -C repo`)를 쓴다. `repo` 부재는 REPO(기존 호출·자연 퇴화)."""
    sha = (sha or "").strip()
    pathspecs = [p for p in pathspecs if p]
    if not sha or not pathspecs:
        return None
    if runner is None:
        if shutil.which("git") is None:
            return None
        repo = Path(repo) if repo is not None else home.root

        def runner(argv: list[str]) -> tuple[int, str]:
            r = subprocess.run(
                ["git", "-C", str(repo), *argv],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=git._BOARD_GIT_TIMEOUT_SECONDS)
            return r.returncode, r.stdout or ""

    rc, out = runner(["log", "--oneline", f"{sha}..HEAD", "--", *pathspecs])
    if rc != 0:
        return None
    return bool(out.strip())


# 고정 16진 commit sha 형식 — abbrev 7자+ ~ full 40(SHA-1)/64(SHA-256). `HEAD`·브랜치·태그
# 처럼 **움직이는 ref** 를 freshness anchor 로 오인하지 않기 위한 게이트(codex MF2). 이들은
# rev-parse 를 통과(rc0)하지만 `<ref>..HEAD` 가 (거의) 항상 비어 영구 false-green 이다.
_HEX_SHA_RE = re.compile(r"[0-9a-fA-F]{7,64}")


def _is_hex_sha(value: str) -> bool:
    """`value` 가 고정 16진 commit sha 형식인가 (freshness anchor 자격·`_sha_anchor_status`/
    `cli.board_repin._canonical_commit_oid` 공유 형식 규칙·사본 금지). `HEAD`·브랜치명·태그·너무 짧은 값은 False —
    움직이는 ref 는 안정적 검증 기준이 못 된다(codex MF2)."""
    return bool(_HEX_SHA_RE.fullmatch((value or "").strip()))


# freshness anchor 판정 verdict. 사유 문구까지 한 곳에서 구분한다.
_ANCHOR_OK = "ok"                      # 고정 hex·해소·HEAD 선조 → 유효 backward anchor.
_ANCHOR_NON_SHA = "non-sha"            # 형식 아님(HEAD/브랜치/태그·움직이는 ref·MF2).
_ANCHOR_UNRESOLVED = "unresolved"      # hex 이나 이 저장소 commit 아님(rev-parse rc1·타-git SHA·MF1).
_ANCHOR_NON_ANCESTOR = "non-ancestor"  # 해소되나 HEAD 선조 아님(merge-base rc1·descendant/딴 브랜치).
_ANCHOR_AMBIGUOUS = "ambiguous"        # 축약 sha 가 다중 매칭(repo 성장) — pin 속성·unverifiable.
_ANCHOR_UNKNOWN = "unknown"            # 환경적 판정불가(git 부재·rc≥2 fatal) → silent skip.

# ambiguous 축약 sha 의 rev-parse stderr 신호 (실 git 실측·git 2.43: "error: short object ID <p> is
# ambiguous"). `--quiet` 는 이 stderr 를 억제하므로(실측) 진단은 non-quiet 재질의로 본다. 소문자
# 부분일치(버전 문구 변동 여유).
_AMBIGUOUS_STDERR_SIGNALS: tuple[str, ...] = ("ambiguous", "short object id")


def _git_rc(home, argv: list[str], *, runner=None, repo: Path | None = None) -> int | None:
    """git 명령의 rc 만 반환(stdout 무시)·git 부재 → None, 실행 예외는 전파 (`_git_run` 얇은 래퍼·사본 0)."""
    res = git._git_run(argv, runner=runner, repo=home.root if repo is None else repo)
    return res[0] if res is not None else None


def _rev_parse_ambiguous(home, sha: str, *, runner=None,
                         repo: Path | None = None) -> bool:
    """축약 `sha` 가 다중 매칭(모호)한가 — **non-quiet** rev-parse stderr 신호로 판정.

    `git rev-parse --verify --quiet <sha>^{commit}` 는 모호 시 stderr 를 **억제**(실측·rc1) 하므로,
    실패 경로에서 `--quiet` 없이 재질의해 stderr 의 `_AMBIGUOUS_STDERR_SIGNALS`(소문자 부분일치)를
    본다. 모호는 환경 오류가 아니라 **pin 의 속성**이라 호출부가 `_ANCHOR_AMBIGUOUS`(unverifiable)로
    표면화한다. git 부재 → False(모호로 단정 안 함·rc 기반 분류로 폴백). 실행 예외는 전파한다.

    `runner` 대역: `(rc, stdout)` 또는 `(rc, stdout, stderr)` — 3-tuple 이면 stderr 를 본다(hermetic
    테스트가 모호 stderr 를 주입)."""
    argv = ["rev-parse", "--verify", f"{sha}^{{commit}}"]   # non-quiet — stderr 억제 안 함
    if runner is None:
        if shutil.which("git") is None:
            return False
        repo = Path(repo) if repo is not None else home.root
        r = subprocess.run(
            ["git", "-C", str(repo), *argv], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=git._BOARD_GIT_TIMEOUT_SECONDS)
        stderr = r.stderr or ""
    else:
        result = runner(argv)
        stderr = result[2] if len(result) >= 3 else ""   # 2-tuple 대역 → stderr 없음.
    low = stderr.lower()
    return any(sig in low for sig in _AMBIGUOUS_STDERR_SIGNALS)


def _sha_anchor_status(home, sha: str, *, runner=None,
                       repo: Path | None = None) -> tuple[str, str | None]:
    """`sha` 가 유효한 **backward freshness anchor** 인가 — `(verdict, full_oid)`.

    유효 anchor(`_ANCHOR_OK`) = (1) 고정 hex 형식 + (2) **고정 SHA 로** 유일 commit 해소(ref 아님) +
    (3) **HEAD 의 선조**. 셋 다여야 `<oid>..HEAD` range 가 "pin 이후 변경"을 정직히 센다. OK 면
    `full_oid` = rev-parse 로 얻은 **canonical full OID**(gitrevisions 유일 해소) — 호출부는
    이후 모든 git 명령(merge-base·`<oid>..HEAD` range·domain pin-이후 질의)을 **이 full OID 로** 돌려
    원 입력 문자열 재해석(ref/모호 축약 여지)을 없앤다. 비-OK verdict 는 `full_oid=None`. 실패 모드:
      - `_ANCHOR_NON_SHA`      : 형식 아님(`HEAD`/브랜치/태그·MF2) **또는** hex 이나 hex-이름 ref 로
        해소됨(`deadbeef` 라는 branch/tag — 해소 OID 가 입력을 prefix 로 안 가짐).
      - `_ANCHOR_UNRESOLVED`   : hex 이나 이 저장소 commit 아님(rev-parse rc1·타-git SHA·MF1).
      - `_ANCHOR_NON_ANCESTOR` : 해소되나 HEAD 선조 아님(`merge-base --is-ancestor` rc1).
      - `_ANCHOR_AMBIGUOUS`    : 축약 sha 가 다중 매칭(repo 성장·`_rev_parse_ambiguous` stderr 신호·
        ). 모호는 환경이 아니라 pin 속성 → unverifiable(full OID 로 재핀 안내).
    환경적 판정불가 → `_ANCHOR_UNKNOWN`(silent skip): 빈 sha·git 부재·rev-parse/merge-base **rc≥2**
    (rc128 non-repo·safe.directory·권한 등 fatal). 환경 오류를 unverifiable 로 오인하지 않는다(MF1).
    쓰기 소비자 `cli.board_repin._canonical_commit_oid`(repin 검증)는 이 판정이 OK 면 full_oid 를
    기록한다(read/write 대칭·판정 알고리즘은 이 함수 하나).
    `runner` = 테스트 hermetic seam(argv→(rc, stdout[, stderr]))·미주입 시 실 subprocess
    (`git -C repo`; 부재=REPO)."""
    sha = (sha or "").strip()
    if not sha:
        return (_ANCHOR_UNKNOWN, None)
    if not _is_hex_sha(sha):
        return (_ANCHOR_NON_SHA, None)  # 움직이는 ref — rev-parse 시도 안 함.
    res = git._git_run(["rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"],
                       runner=runner, repo=home.root if repo is None else repo)
    if res is None:
        return (_ANCHOR_UNKNOWN, None)
    rc, resolved = res
    if rc != 0:
        # 실패 — `--quiet` 가 모호 stderr 를 억제하므로 non-quiet 재질의로 모호성 먼저 판정.
        # 모호(축약 다중 매칭)는 환경이 아니라 pin 속성 → unverifiable. 그 외 rc1=미해소·rc≥2=env.
        if _rev_parse_ambiguous(home, sha, runner=runner, repo=repo):
            return (_ANCHOR_AMBIGUOUS, None)
        if rc == 1:
            return (_ANCHOR_UNRESOLVED, None)  # 순수 미해소(repo 정상·sha 부재·타-git SHA).
        return (_ANCHOR_UNKNOWN, None)  # rc≥2 fatal(non-repo·safe.directory 등) — 환경적.
    full_oid = resolved.strip()
    # 해소 OID 가 입력 sha 를 실제 prefix 로 가져야 **고정 SHA** — 아니면 hex-이름 ref.
    if not full_oid.lower().startswith(sha.lower()):
        return (_ANCHOR_NON_SHA, None)
    # 이후 하류 명령은 **canonical full OID** 를 쓴다(원 입력 재해석 제거).
    rc = _git_rc(home, ["merge-base", "--is-ancestor", full_oid, "HEAD"],
                 runner=runner, repo=repo)
    if rc is None:
        return (_ANCHOR_UNKNOWN, None)
    if rc == 0:
        return (_ANCHOR_OK, full_oid)
    if rc == 1:
        return (_ANCHOR_NON_ANCESTOR, None)
    return (_ANCHOR_UNKNOWN, None)  # rc≥2 fatal — 환경적.


def _unverifiable_sha_reason(status: str, sha: str) -> str:
    """anchor verdict → advisory 사유 문구 (`_verified_at_finding`·`lint_domain_freshness` 공유·DRY).

    `_ANCHOR_OK`/`_ANCHOR_UNKNOWN` 은 unverifiable 이 아니라 호출부가 이 함수를 안 부른다."""
    if status == _ANCHOR_NON_SHA:
        return f"verified_at({sha}) 이 고정 sha 아님(HEAD/브랜치/태그?)"
    if status == _ANCHOR_NON_ANCESTOR:
        return f"verified_at({sha[:12]}) 이 HEAD 선조 아님(딴 브랜치/앞선 checkout?)"
    if status == _ANCHOR_AMBIGUOUS:
        return f"verified_at({sha}) 축약 sha 가 모호(repo 성장으로 다중 매칭) — full OID 로 재핀"
    return f"verified_at({sha[:12]}) 이 이 저장소 commit 으로 해소 안 됨(타 git SHA?)"  # UNRESOLVED


_FRESHNESS_REPO_SELF = "self"
_FRESHNESS_REPO_UPSTREAM = "upstream"
_SCP_STYLE_UPSTREAM_RE = re.compile(
    r"^[^/\\:]+@[^/\\:]+:.+$|^[A-Za-z][A-Za-z0-9_.-]*:.+$")
_WINDOWS_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _freshness_owner_repo(home, owner) -> tuple[Path | None, str | None]:
    """현재-진실 문서의 `repo:` 채널을 소유 git checkout 으로 해소한다.

    단일 규칙:
      - `self` → REPO. 키 부재는 파서/호출부가 `self`를 넘겨 자연 퇴화한다.
      - `upstream` → `local.conf upstream.path=` **경로형** checkout. 상대경로는 REPO 기준.

    URL upstream 은 lint 가 fetch/clone 하지 않는다는 기존 네트워크-0 경계를 지켜 미해소로
    남긴다. 경로 부재/이동·실 git checkout 검증 실패도 `(None, reason)` — 호출부가
    `*-unverifiable` advisory 로 표면화해 green 으로 흡수하지 않는다. 알 수 없는 `repo:`
    값(명시 null/빈 문자열·false·0·컨테이너 포함)도 동일하다.
    """
    if owner is None:
        return (None, "repo(null) 미지원 — 키 부재 또는 self/upstream 이어야 함")
    elif not isinstance(owner, str):
        return (None, f"repo({owner!r}) 미지원 — self 또는 upstream 이어야 함")
    else:
        value = owner.strip()
    if value == _FRESHNESS_REPO_SELF:
        return (home.root, None)
    if value != _FRESHNESS_REPO_UPSTREAM:
        shown = value if value else "<empty>"
        return (None, f"repo({shown}) 미지원 — self 또는 upstream 이어야 함")
    try:
        upstream = (conf_store.local_config(home).get("upstream.path") or "").strip()
    except (OSError, UnicodeError, RuntimeError):
        return (None, "local.conf 읽기 실패로 upstream 소유 repo 미해소")
    if not upstream:
        return (None, "local.conf upstream.path 미설정으로 소유 repo 미해소")
    try:
        # 문서 소유자의 로컬 checkout 판정이다(네트워크 0). Windows 드라이브를 빼고 `://` 포함 또는
        # scp 모양이면 모두 비로컬로 본다 — source 형태 분류(`upstream_rules.classify_upstream`)와는
        # 책임·판정식이 다르다(예: `./a://b` 는 그쪽에서 path, 여기서는 미해소).
        is_windows_path = bool(_WINDOWS_DRIVE_PATH_RE.match(upstream))
        is_url = (not is_windows_path and
                  ("://" in upstream or bool(_SCP_STYLE_UPSTREAM_RE.match(upstream))))
        if is_url:
            return (None, "local.conf upstream.path 이 URL이라 로컬 소유 repo 미해소")
        # `~`/`~user` 확장은 `_expanded_user_path` 단일 규칙 — 없는 `~user` 는 플랫폼 무관하게
        # 여기서 해소 실패로 끝난다. 종전 `Path.expanduser()` 는 POSIX 만 RuntimeError 였고
        # Windows 는 phantom `C:\\Users\\<name>` 을 조립해 아래 "경로 부재/이동" 으로 갈렸다.
        candidate = pm_paths._expanded_user_path(upstream)
        if not candidate.is_absolute():
            candidate = home.root / candidate
        candidate = candidate.resolve()
    except (OSError, RuntimeError, ValueError):
        return (None, f"upstream 경로 해소 실패({upstream})")
    if not candidate.is_dir():
        return (None, f"upstream 경로 부재/이동({candidate})")
    if not (candidate / ".git").exists():
        return (None, f"upstream 경로가 git checkout 아님({candidate})")
    checkout = git._git_run(["rev-parse", "--is-inside-work-tree"], repo=candidate)
    if checkout is None or checkout[0] != 0 or checkout[1].strip().lower() != "true":
        return (None, f"upstream 경로 git checkout 검증 실패({candidate})")
    return (candidate, None)


def _verified_at_finding(home, doc_file: Path, pathspecs, label: str, kind: str, *,
                         runner=None) -> list[tuple[str, str, str]]:
    """문서 `verified_at` sha 판정 → 유효 anchor 면 stale/`kind`·아니면 unverifiable advisory.

    fail-soft: 문서 부재·frontmatter 없음/깨짐·`verified_at` 부재·환경적 판정불가(`_ANCHOR_UNKNOWN`)
    → [](명시 skip — 신규 clone·아직 verified_at 미부여 문서 무영향·false-green 아님).

    **anchor 판정**: `verified_at` 이 유효 backward anchor
    (고정 hex + 해소 + HEAD 선조·`_sha_anchor_status`)가 아니면 종전엔 `_git_commits_between` 이
    rc≠0/빈 range 를 조용한 green 으로 흡수했다. 이제 `{kind 접두}-unverifiable` advisory 로
    표면화한다 — 형식 아님(움직이는 ref·MF2)/미해소(타-git SHA·MF1)/HEAD 선조 아님(descendant·
    딴 브랜치)를 사유 구분(never-block·억지 판정 금지). anchor OK 일 때만
    `<sha>..HEAD` stale 검사 → `(label, kind, detail)` 1개. (매핑 경로 `_CURRENT_TRUTH_ENGINE_PATHS`
    경로-관찰가능성 축은 여기 무해 — 그 축은 domain 전용.)"""
    if not doc_file.exists():
        return []
    try:
        fm, _ = ticket_store.load_ticket(doc_file)
    except ticket_store._load_ticket_soft_errors():  # frontmatter 없음/깨짐은 skip(비차단).
        return []
    sha = str((fm or {}).get("verified_at") or "").strip()
    if not sha:
        return []
    owner = fm["repo"] if "repo" in fm else _FRESHNESS_REPO_SELF
    owner_repo, owner_error = _freshness_owner_repo(home, owner)
    if owner_repo is None:
        return [(label, kind.replace("-stale", "-unverifiable"),
                 f"소유 repo freshness 검증 불가 — {owner_error} · architect 재검증 필요")]
    status, full_oid = _sha_anchor_status(home, sha, runner=runner, repo=owner_repo)
    if status == _ANCHOR_UNKNOWN:
        return []  # git 부재 등 환경적 판정불가 — 종전 fail-soft silent skip 유지.
    if status != _ANCHOR_OK:
        # 형식 아님/미해소/비-선조 — stale kind 와 대칭 "{x}-stale" → "{x}-unverifiable".
        return [(label, kind.replace("-stale", "-unverifiable"),
                 _unverifiable_sha_reason(status, sha)
                 + " — freshness 검증 불가·architect 재검증 필요")]
    # canonical full OID 로 stale 검사 (원 입력 재해석 제거). 표시는 원 sha(사용자 친화).
    if _git_commits_between(
            home,
            full_oid, list(pathspecs), runner=runner, repo=owner_repo) is not True:
        return []
    return [(label, kind,
             f"verified_at({sha[:12]}) 이후 매핑 경로에 커밋 있음 — architect 재검증 필요")]


def lint_architecture_freshness(home, *, runner=None) -> list[tuple[str, str, str]]:
    """architecture.md freshness advisory — `verified_at` sha 판정 (never-block).

    architecture.md frontmatter `verified_at: <sha>` *이후* 엔진 코드 트리
    (`_CURRENT_TRUTH_ENGINE_PATHS`)에 커밋이 있으면 "architect 재검증 필요" 권고를 낸다 —
    검증 기준 sha 이후 코드가 바뀌었다는 이진 기계 판정(date "최신이겠지" 해석 구멍 대체·
    ). kind=`architecture-stale`. verified_at 이 유효 anchor 아니면(타-git SHA·비-선조·
    움직이는 ref) `architecture-unverifiable` advisory(조용한 green 근절). 둘 다
    `_ADVISORY_LINT_KINDS` 등재·`--gate` 비기여. fail-soft: 문서 부재·frontmatter 없음·
    verified_at 부재·git 불가 → [](명시 skip)."""
    return _verified_at_finding(
        home,
        pm_paths.architecture_file(home), _CURRENT_TRUTH_ENGINE_PATHS,
        "architecture.md", "architecture-stale", runner=runner)


def lint_status_freshness(home, *, runner=None) -> list[tuple[str, str, str]]:
    """status.md freshness advisory — `verified_at` sha 판정 (never-block).

    status.md frontmatter `verified_at: <sha>` *이후* 엔진 코드 트리에 커밋이 있으면
    "모듈 진행 상태 재검증 필요" 권고. lint_architecture_freshness 와 동일 규칙·매핑 경로
    (status.md 는 엔진 모듈 진행 상태를 기록).
    kind=`status-stale`·verified_at 이 유효 anchor 아니면 `status-unverifiable`(advisory).
    fail-soft: 부재/verified_at 부재/git 불가 → []."""
    return _verified_at_finding(
        home,
        pm_paths.status_file(home), _CURRENT_TRUTH_ENGINE_PATHS,
        "status.md", "status-stale", runner=runner)
