"""T-0492 — 출하 템플릿 트리의 ignore 규칙 파일이 *미추적*·*자기-은닉* 으로 남는 걸 잡는 가드.

실측한 결함(2026-07-28): `templates/opencode/.opencode/.gitignore` 가 자기 자신(`.gitignore`)을 무시
목록에 넣고 있어 `git status` 에 **한 번도 뜨지 않았고**, 그래서 아무도 `git add` 하지 않은 채 몇 달간
로컬 전용 파일로 남았다. `pm_import` 는 템플릿 트리를 *디스크에서* 통째 복사하므로 유지보수자 머신에선
멀쩡히 동작했지만, 공개 repo 를 fresh clone 한 채택자에겐 그 파일이 애초에 없다 → 무시 규칙 미출하.

이 결함 클래스는 **정의상 눈에 안 띈다**(자기-은닉이라 status·diff·리뷰 어디에도 안 뜬다). 지식이나
리뷰로는 못 막히므로 기계 판정으로 못박는다([[mechanize-dont-instruct-llm]]):

  ① 디스크에 있는 템플릿 ignore 파일은 전부 git 추적 대상이어야 한다 (미추적 = 출하 누락).
  ② 어떤 ignore 파일도 자기 자신을 무시하면 안 된다 (자기-은닉 = ① 을 영구히 은폐하는 원인).
  ③ opencode 어댑터 `.gitignore` 는 출하 경로(opencode flavor engine.manifest)에 실려야 한다 —
     추적만 하면 *신규* 채택자(pm_import 트리 복사)만 받고 *기존* 채택자(pm_update manifest 전파)는
     영영 못 받는다([[T-0473]] 출하 경로 미등재 클래스 선례).

**가드 자신의 sensitivity 도 검증한다**(안 돌려보면 가짜 게이트 — [[feature-ship-needs-fresh-adopter-gate]]):
판정 로직을 독립 헬퍼로 떼어 (a) opencode 원본 5줄 본문에 자기-은닉을 실제로 검출하고 (b) 임시 git
repo 에 미추적 ignore 파일을 심어 ① 이 실제로 잡아내는 걸 각각 단언한다.

hermetic — stdlib + `git` 서브프로세스만(라이브 하니스·네트워크 0).
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
OC_MANIFEST = REPO / "templates" / "opencode" / ".project_manager" / "engine.manifest"

# 가드 ① 스캔 대상 = 출하 누락 시 각 도구의 패키징/빌드 동작이 바뀌는 ignore 규칙 파일 전부.
IGNORE_FILE_NAMES = frozenset({".gitignore", ".npmignore", ".dockerignore"})

# 가드 ② 대상은 git 이 실제로 읽는 `.gitignore` 만이다. `.npmignore`/`.dockerignore` 의 패턴
# 의미론은 npm/Docker 소유라 `git check-ignore` 로 판정하면 근거 없는 오탐·누락을 만든다.
GIT_IGNORE_FILE_NAMES = frozenset({".gitignore"})

# 스캔 제외 디렉토리 — 재설치 산출물·바이트코드·VCS 메타. pm_import.COPY_EXCLUDE_DIR_NAMES 동형.
SCAN_EXCLUDE_DIR_NAMES = frozenset({"node_modules", "__pycache__", ".git"})

_GIT = shutil.which("git")


def _is_git_worktree(root: Path) -> bool:
    """`root` 가 실제 git work tree 인지 (능력 탐지·플랫폼/경로 하드코딩 아님).

    `git ls-files` 는 work tree 밖에서 **빈 출력 + 비정상 rc** 를 낸다 — 그걸 "전부 미추적" 으로
    읽으면 비-git 사본(예: 프레임워크 트리를 복사해 만든 e2e 픽스처)에서 가드가 헛발화한다.
    추적 상태를 *판정할 수 없는* 트리는 fail 이 아니라 skip 이 옳다(실 repo 에선 항상 판정 가능).
    """
    if _GIT is None:
        return False
    proc = subprocess.run(
        [_GIT, "rev-parse", "--is-inside-work-tree"],
        cwd=str(root), capture_output=True, text=True, encoding="utf-8",
    )
    return proc.returncode == 0 and proc.stdout.strip() == "true"


# 추적 상태 가드 = 이 트리가 git worktree 일 때만. 자기-은닉 판정과 sensitivity 는 임시 repo 를
# 만들어 `check-ignore` 하므로 git 바이너리만 있으면 된다(실 트리 성격과 무관).
requires_git_worktree = pytest.mark.skipif(
    not _is_git_worktree(REPO),
    reason="git 바이너리 부재 또는 비-git 트리 — 추적 상태 판정 불가.")
requires_git_binary = pytest.mark.skipif(
    _GIT is None,
    reason="git 바이너리 부재 — check-ignore 자기-은닉 판정·임시 repo sensitivity 실행 불가.")


# ── 순수 판정 헬퍼 (sensitivity 테스트가 같은 함수를 태운다) ──────────────────────


def _ondisk_ignore_relpaths(root: Path, subdir: str) -> set[str]:
    """`root/subdir` 하위 디스크의 ignore 규칙 파일 relpath 집합 (root 기준·POSIX)."""
    base = root / subdir
    if not base.is_dir():
        return set()
    found: set[str] = set()
    for path in base.rglob("*"):
        if not path.is_file() or path.name not in IGNORE_FILE_NAMES:
            continue
        rel = path.relative_to(root)
        if any(part in SCAN_EXCLUDE_DIR_NAMES for part in rel.parts):
            continue
        found.add(rel.as_posix())
    return found


def _tracked_relpaths(root: Path, subdir: str) -> set[str]:
    """`git ls-files <subdir>` 결과 relpath 집합 (root 기준·POSIX·비-ASCII 이름 보존)."""
    proc = subprocess.run(
        [_GIT, "-c", "core.quotepath=false", "ls-files", "--", subdir],
        cwd=str(root), capture_output=True, text=True, encoding="utf-8",
    )
    return {line for line in proc.stdout.splitlines() if line}


def _untracked_ignore_files(root: Path, subdir: str) -> list[str]:
    """`root/subdir` 하위 ignore 파일 중 **미추적**인 것 (출하 누락 후보)."""
    return sorted(_ondisk_ignore_relpaths(root, subdir) - _tracked_relpaths(root, subdir))


def _self_hiding_entries(text: str, file_name: str, tmp_path: Path) -> list[str]:
    """git 의미론으로 `.gitignore` 본문이 자기 파일을 무시하는지 판정해 마지막 매치 규칙을 반환.

    임시 worktree 로 본문만 격리해 실제 템플릿 트리의 상위 ignore 규칙이 판정에 섞이지 않게 한다.
    글롭·문자 클래스·이스케이프·디렉토리 전용 `/`·부정 패턴의 마지막 매치 의미론은 재구현하지 않고
    `git check-ignore -v --no-index` 에 전부 위임한다.

    worktree 자리는 호출한 테스트의 `tmp_path` 아래다 — 한 테스트가 본문 여러 벌을 판정하므로
    호출마다 새 이름이 필요하고(`mkdtemp` 유일성), 프로젝트 밖으로는 나가지 않는다.
    """
    if _GIT is None:
        raise RuntimeError("git 바이너리 부재 — 자기-은닉 의미론을 판정할 수 없음")
    if file_name not in GIT_IGNORE_FILE_NAMES:
        raise ValueError(
            f"{file_name} 은 git ignore 규칙 파일이 아님 — 해당 도구의 의미론으로 별도 판정해야 함")

    with tempfile.TemporaryDirectory(prefix="pm-self-hiding-", dir=tmp_path) as temp_dir:
        worktree = Path(temp_dir)
        subprocess.run(
            [_GIT, "init", "-q"], cwd=str(worktree), check=True, capture_output=True)
        # 유지보수자 머신의 global excludes / custom git-init template 이 판정에 섞이지 않게 비운다.
        global_excludes = worktree / "empty-global-excludes"
        global_excludes.write_text("", encoding="utf-8")
        (worktree / ".git" / "info" / "exclude").write_text("", encoding="utf-8")
        (worktree / file_name).write_text(text, encoding="utf-8")
        decision = subprocess.run(
            [_GIT, "-c", f"core.excludesFile={global_excludes}",
             "check-ignore", "-q", "--no-index", "--", file_name],
            cwd=str(worktree), capture_output=True, text=True, encoding="utf-8",
        )
        if decision.returncode == 1:
            return []
        assert decision.returncode == 0, (
            f"git check-ignore 판정 실패(rc={decision.returncode}): "
            f"{decision.stderr.strip()}")

        details = subprocess.run(
            [_GIT, "-c", f"core.excludesFile={global_excludes}",
             "check-ignore", "-v", "--no-index", "--", file_name],
            cwd=str(worktree), capture_output=True, text=True, encoding="utf-8",
        )

    assert details.returncode == 0, (
        f"git check-ignore 진단 실패(rc={details.returncode}): {details.stderr.strip()}")

    hits: list[str] = []
    for output_line in details.stdout.splitlines():
        metadata, separator, _queried_path = output_line.partition("\t")
        assert separator, f"git check-ignore -v 출력 형식 불명: {output_line!r}"
        _source, _line_number, pattern = metadata.split(":", 2)
        hits.append(pattern)
    return hits


def _load_pm_update():
    spec = importlib.util.spec_from_file_location("pm_update", TOOLS / "pm_update.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest_entry_source(manifest: Path, relpath: str) -> str | None:
    """manifest 에서 relpath 엔트리의 `@source=<rel>` 값. 미등재면 KeyError 대신 명시 sentinel."""
    pm_update = _load_pm_update()
    for entry in pm_update.read_manifest(manifest):
        if str(entry).replace("\\", "/") == relpath:
            return getattr(entry, "source_rel", None)
    raise AssertionError(f"{manifest.name} 에 {relpath} 미등재")


# ── 가드 ① 템플릿 ignore 파일 전량 추적 ────────────────────────────────────────


@requires_git_worktree
def test_template_ignore_files_are_all_tracked():
    """`templates/` 하위 디스크의 ignore 규칙 파일이 전부 git 추적된다 (출하 누락 차단·T-0492).

    미추적 ignore 파일은 fresh clone 에 존재하지 않는다 → `pm_import` 트리 복사가 못 싣고 채택자는
    무시 규칙 없이 어댑터를 받는다. `git add <경로>` 로 추적 전환하고, 그 파일이 자기를 무시하고
    있었다면 그 줄부터 제거해야 한다(가드 ② 참조).
    """
    untracked = _untracked_ignore_files(REPO, "templates")
    assert untracked == [], (
        "출하 template 트리에 미추적 ignore 파일 — 채택자에게 출하되지 않는다(T-0492). "
        f"`git add` 로 추적 전환: {untracked}"
    )


# ── 가드 ② 자기-은닉 금지 ──────────────────────────────────────────────────────


@requires_git_binary
def test_template_ignore_files_do_not_self_hide(tmp_path):
    """어떤 템플릿 ignore 파일도 자기 자신을 무시하지 않는다 (가드 ① 을 은폐하는 근본 원인).

    자기-은닉 규칙 파일은 *미추적인 어느 clone 에서도* `git status` 에 뜨지 않아 영영 커밋되지 않는다
    — 채택자 트리에도 그대로 전파되는 함정이라 출하 전 소거한다. 추적된 파일은 git 이 ignore 규칙을
    적용하지 않으므로 이 줄을 빼도 무시 *대상*(산출물)은 하나도 바뀌지 않는다.
    """
    offenders: list[str] = []
    for rel in sorted(_ondisk_ignore_relpaths(REPO, "templates")):
        path = REPO / rel
        if path.name not in GIT_IGNORE_FILE_NAMES:
            continue
        for line in _self_hiding_entries(path.read_text(encoding="utf-8"), path.name, tmp_path):
            offenders.append(f"{rel}: {line!r}")
    assert offenders == [], (
        "ignore 규칙 파일이 자기 자신을 무시 — 어느 clone 에서도 git status 에 안 떠 영영 미추적으로 "
        f"남는다(T-0492). 해당 줄을 제거하라(무시 대상은 안 바뀐다):\n" + "\n".join(offenders)
    )


# ── 가드 ③ opencode 어댑터 .gitignore 의 내용 + 출하 경로 ────────────────────────


@requires_git_worktree
def test_opencode_adapter_gitignore_tracked_and_keeps_artifact_rules(tmp_path):
    """opencode 어댑터 `.gitignore` 가 추적되고, opencode 생성 산출물 무시 규칙을 그대로 유지한다.

    추적 전환은 **additive** — 자기-은닉 줄만 빼고 산출물 4종(`node_modules`·`package.json`·
    `package-lock.json`·`bun.lock`)은 계속 무시한다. 이 4종은 opencode 가 `.opencode/` 마다 자동
    생성/설치하는 머신-로컬 재생성물이라(실행 중인 opencode 버전으로 `@opencode-ai/plugin` 설치)
    커밋·출하 대상이 아니다 — 핀된 lock 을 출하하면 채택자 런타임과 어긋난 버전을 박는다.
    """
    rel = "templates/opencode/.opencode/.gitignore"
    path = REPO / rel
    assert path.is_file(), f"opencode 어댑터 ignore 규칙 파일 부재: {rel}"
    assert rel in _tracked_relpaths(REPO, "templates"), (
        f"{rel} 미추적 — fresh clone 에 없어 pm_import 가 못 싣는다(T-0492 회귀)")

    body = path.read_text(encoding="utf-8")
    rules = {line.strip() for line in body.splitlines()
             if line.strip() and not line.strip().startswith("#")}
    missing = {"node_modules", "package.json", "package-lock.json", "bun.lock"} - rules
    assert not missing, (
        f"opencode 로컬 산출물 무시 규칙 누락 {sorted(missing)} — 추적 전환은 additive 여야 한다"
        "(무시되던 산출물은 계속 무시)")
    assert _self_hiding_entries(body, ".gitignore", tmp_path) == [], \
        "opencode 어댑터 .gitignore 가 자기 자신을 무시 — 자기-은닉 재발(T-0492)"


def test_opencode_manifest_registers_adapter_gitignore():
    """opencode flavor manifest 가 `.opencode/.gitignore` 를 `@source` 로 등록 (출하 경로·T-0492 DoD).

    추적만 하면 `pm_import`(템플릿 트리 통째 복사)로 **신규** 채택자만 받는다. **기존** 채택자에게
    닿는 채널은 manifest 기반 `pm_update` 전파뿐이라, 미등재면 이미 채택한 사람은 영영 못 받는다
    ([[T-0473]] 출하 경로 미등재 클래스). canonical 이 `templates/opencode/` 라 루트 `.opencode/`
    부재 비대칭을 잇는 `@source` remap 이 필수다(lib·plugins·pm_orch_opencode.py 동형).
    """
    assert _manifest_entry_source(OC_MANIFEST, ".opencode/.gitignore") == \
        "templates/opencode/.opencode/.gitignore", (
        "opencode manifest 의 .opencode/.gitignore @source remap 이 templates/opencode/ 를 "
        "가리키지 않음 — self-update 가 소스를 못 찾아 전파 실패(rc2)")


# ── 가드 ④ 제품 루트 opencode npm 산출물 재추적 방지 ───────────────────────────

# 루트 .opencode 추적 허용집합 — ignore 규칙 파일 + safe-write/stall-watchdog 인스턴스 사본.
# 인스턴스 사본(lib/plugins)은 fresh clone 에서 런타임이 즉시 참조할 수 있어야 하고,
# templates 정본과 같은 내용으로 관리되므로 추적돼야 한다.
# npm 재생성물(node_modules·package.json·package-lock.json·bun.lock)은 계속 금지다 — 집합 일치
# 비교라 허용집합 밖 신규 추적 전환은 즉시 발화한다.
ROOT_OPENCODE_TRACKED_ALLOWLIST = frozenset({
    ".opencode/.gitignore",
    ".opencode/lib/safe-write-core.cjs",
    ".opencode/lib/stall-watchdog-core.cjs",
    ".opencode/plugins/stall-watchdog.js",
})


@requires_git_worktree
def test_root_opencode_tracks_only_its_gitignore():
    """제품 루트 `.opencode` 추적 집합은 허용집합에 머문다 (T-0631 · OpenCode 묶음 갱신).

    opencode 런타임이 `node_modules`·`package*.json` 을 다시 만들어도 제품 소스가 아니므로 index 에
    들어오면 안 된다. 반대로 `.gitignore` 자체와 safe-write/워치독 인스턴스 사본 3건은
    fresh clone 에 있어야 설치된 OpenCode 런타임이 바로 쓸 수 있다.
    """
    rel = ".opencode/.gitignore"
    assert (REPO / rel).is_file(), f"루트 opencode ignore 규칙 파일 부재: {rel}"
    tracked = _tracked_relpaths(REPO, ".opencode")
    unexpected = sorted(tracked - ROOT_OPENCODE_TRACKED_ALLOWLIST)
    missing = sorted(ROOT_OPENCODE_TRACKED_ALLOWLIST - tracked)
    assert not unexpected and not missing, (
        "루트 .opencode 추적 집합이 허용집합에서 이탈했다 — npm 재생성물 재추적 회귀"
        f"(unexpected={unexpected}, missing={missing})")


@requires_git_binary
def test_root_opencode_gitignore_matches_template_and_ignores_runtime_artifacts():
    """루트 판은 템플릿과 동형이고, 런타임 npm 재생성물 4종을 실제 git 의미론으로 무시한다."""
    root_ignore = REPO / ".opencode" / ".gitignore"
    template_ignore = REPO / "templates" / "opencode" / ".opencode" / ".gitignore"
    assert root_ignore.read_bytes() == template_ignore.read_bytes(), \
        "루트 .opencode/.gitignore 가 canonical 템플릿 판과 다름"

    artifacts = (
        ".opencode/node_modules/runtime-regenerated.js",
        ".opencode/package.json",
        ".opencode/package-lock.json",
        ".opencode/bun.lock",
    )
    decision = subprocess.run(
        [_GIT, "check-ignore", "--no-index", "--", *artifacts],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8",
    )
    assert decision.returncode == 0, (
        f"opencode 런타임 재생성물이 ignore 되지 않음(rc={decision.returncode}): "
        f"{decision.stderr.strip()}")
    assert set(decision.stdout.splitlines()) == set(artifacts), (
        "opencode 런타임 재생성물 중 일부가 ignore 밖이라 다시 추적될 수 있음: "
        f"{decision.stdout.splitlines()}")


# ── sensitivity — 가드가 실제로 도는지 (가짜 게이트 방지) ────────────────────────


@requires_git_binary
def test_self_hiding_detector_catches_opencode_original_body(tmp_path):
    """자기-은닉 판정이 opencode 원본 5줄 본문을 실제로 잡고, 출하판(4줄)엔 무발화.

    원본 = opencode `Config.ensureGitignore` 가 써넣는 리터럴
    `["node_modules","package.json","package-lock.json","bun.lock",".gitignore"]`.
    """
    original = "node_modules\npackage.json\npackage-lock.json\nbun.lock\n.gitignore"
    assert _self_hiding_entries(original, ".gitignore", tmp_path) == [".gitignore"], \
        "판정이 opencode 원본의 자기-은닉 줄을 못 잡음(가드 무력)"

    shipped = "# 주석\nnode_modules\npackage.json\npackage-lock.json\nbun.lock\n"
    assert _self_hiding_entries(shipped, ".gitignore", tmp_path) == [], "출하판에 오탐"


@requires_git_binary
def test_self_hiding_detector_normalizes_equivalent_forms_and_skips_negation(tmp_path):
    """git 동치 표기는 잡고, 부정·주석·비동치 이름·선행 공백은 무발화."""
    for variant in ("/.gitignore", "**/.gitignore", ".gitignore  "):
        assert _self_hiding_entries(variant, ".gitignore", tmp_path), f"동치 표기 미검출: {variant!r}"
    for benign in ("!.gitignore", "# .gitignore", ".gitignore.bak", "gitignore", "  .gitignore"):
        assert _self_hiding_entries(benign, ".gitignore", tmp_path) == [], f"오탐: {benign!r}"


@requires_git_binary
@pytest.mark.parametrize(
    ("body", "expected"),
    (
        pytest.param("*", ["*"], id="catch-match-all"),
        pytest.param(".git*", [".git*"], id="catch-prefix-glob"),
        pytest.param("*.gitignore", ["*.gitignore"], id="catch-suffix-glob"),
        pytest.param("[.]gitignore", ["[.]gitignore"], id="catch-character-class"),
        pytest.param("/**/.gitignore", ["/**/.gitignore"], id="catch-leading-slash-double-star"),
        pytest.param(r"\.gitignore", [r"\.gitignore"], id="catch-escaped-literal"),
        pytest.param(".gitignore/", [], id="skip-directory-only-pattern"),
        pytest.param(".gitignore\n!.gitignore", [], id="honor-last-negation"),
    ),
)
def test_self_hiding_detector_follows_git_semantics(body, expected, tmp_path):
    """손-파서가 오판하던 8개 패턴을 git 실제 의미론대로 판정한다."""
    assert _self_hiding_entries(body, ".gitignore", tmp_path) == expected


@requires_git_binary
def test_untracked_ignore_guard_is_not_vacuous(tmp_path):
    """가드 ① 이 임시 repo 에 심은 미추적 ignore 파일을 실제로 잡는다 (공허 아님).

    실 결함 형상 그대로 재현 — 자기 자신을 무시하는 `.gitignore` 를 템플릿 트리에 두면 `git status`
    엔 안 뜨지만 이 가드는 디스크↔`ls-files` 차집합으로 본다. `git add` 후엔 무발화(정상 상태).
    """
    repo = tmp_path / "fake-repo"
    (repo / "templates" / "opencode" / ".opencode").mkdir(parents=True)
    subprocess.run([_GIT, "init", "-q"], cwd=str(repo), check=True, capture_output=True)
    hidden = repo / "templates" / "opencode" / ".opencode" / ".gitignore"
    hidden.write_text("node_modules\n.gitignore\n", encoding="utf-8")

    # 결함 재현 sanity: 자기-은닉이라 git status 엔 안 뜬다(사람 눈으로는 못 잡는 클래스).
    status = subprocess.run(
        [_GIT, "status", "--porcelain", "--untracked-files=all"],
        cwd=str(repo), capture_output=True, text=True, encoding="utf-8",
    ).stdout
    assert ".opencode/.gitignore" not in status, \
        "픽스처가 자기-은닉을 재현 못 함 — sensitivity 테스트 전제 붕괴"

    assert _untracked_ignore_files(repo, "templates") == \
        ["templates/opencode/.opencode/.gitignore"], "가드 ① 이 미추적 ignore 파일을 못 잡음(공허)"

    subprocess.run([_GIT, "add", "-f", "templates/opencode/.opencode/.gitignore"],
                   cwd=str(repo), check=True, capture_output=True)
    assert _untracked_ignore_files(repo, "templates") == [], "추적 전환 후에도 발화(오탐)"
