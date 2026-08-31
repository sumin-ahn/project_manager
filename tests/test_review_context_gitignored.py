"""T-0608 — additional_reviewer 인스턴스 overlay `review_context.local.md` 의 ignore 등재 가드.

실측한 결함(2026-08-09·PM 33차 릴리즈): additional_reviewer 가 읽는 인스턴스 소유 overlay
`.project_manager/review_context.local.md` 가 어느 ignore 파일에도 없어(`git check-ignore` rc=1)
PM 홈에 untracked 로 표면화했다. per-clone 파일이 커밋에 유입되면 인스턴스마다 다른 추가 리뷰어
프롬프트 보강이 공유 히스토리에 박힌다.

`local.conf`·`.local/` 과 같은 per-clone 클래스라 엔진 `.project_manager/.gitignore` 에 등재하고
manifest 전파로 출하 템플릿 전 타깃(=채택자가 받는 사본)에 내린다. 가드는 세 축이다:

  ① 생성 주체와 ignore 규칙이 같은 파일을 가리킨다 — `additional_reviewer.REVIEW_CONTEXT_FILE` 에서
     이름·디렉토리를 파생해 단언한다(상수 rename 시 ignore 규칙 미갱신을 잡는다).
  ② 엔진 canonical + 출하 템플릿 전 타깃에 규칙이 등재돼 있다 — 엔진만 고치면 채택자에게 닿는
     채널(pm_import 트리 복사·pm_update manifest 전파)이 비어 출하 누락으로 남는다([[T-0473]] 선례).
  ③ `git check-ignore` 실판정 — 임시 채택자 repo 에 실 파일을 만들어 rc=0 과 `git status` 무표면화를
     확인한다. 등재 문자열만 보면 상위 절의 부정 패턴·구문 오류로 규칙이 죽어 있어도 통과한다.

과일반화 금지(ticket 결정)도 함께 못박는다 — 패턴은 정확명이라 채택자 dev-state
`wiki/pm_role.local.md`·`wiki/pm_playbook.local.md`(추적 여부를 채택자가 결정하는 별개 클래스)는
무시되지 않아야 한다.

hermetic — stdlib + `git` 서브프로세스만 (라이브 하니스·네트워크 0).
"""
from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
TEMPLATES_DIR = REPO / "templates"
ENGINE_GITIGNORE = REPO / ".project_manager" / ".gitignore"

# additional_reviewer 가 읽는 인스턴스 overlay 파일명 = ignore 규칙의 단일 대상 (정확명·와일드카드 아님).
OVERLAY_FILE_NAME = "review_context.local.md"

# 정확명 등재의 반대급부 — 이 dev-state 문서들은 채택자 소유라 엔진이 추적 여부를 대신 결정하지 않는다.
ADOPTER_OWNED_LOCAL_DOCS = ("wiki/pm_role.local.md", "wiki/pm_playbook.local.md")

_GIT = shutil.which("git")

requires_git_binary = pytest.mark.skipif(
    _GIT is None, reason="git 바이너리 부재 — check-ignore 실판정 실행 불가.")


def _load_additional_reviewer():
    """도구 모듈을 (패키지 아님) importlib 로 경로 로드 — tests/ 공통 규약."""
    spec = importlib.util.spec_from_file_location(
        "additional_reviewer", TOOLS / "additional_reviewer.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _target_names() -> set[str]:
    """존재하는 출하 타깃 디렉토리명 (런타임 discover_target_names 와 동일 규칙·숨김 제외)."""
    return {path.name for path in TEMPLATES_DIR.iterdir()
            if path.is_dir() and not path.name.startswith(".")}


def _shipped_gitignore_paths() -> tuple[Path, ...]:
    """엔진 canonical + 출하 타깃별 `.project_manager/.gitignore` (하드코딩 아닌 디렉토리 파생)."""
    targets = sorted(TEMPLATES_DIR / name for name in _target_names())
    return (ENGINE_GITIGNORE,
            *(target / ".project_manager" / ".gitignore" for target in targets))


GITIGNORE_PATHS = _shipped_gitignore_paths()


def _identify(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


def _rules(text: str) -> set[str]:
    """주석·빈 줄을 뺀 ignore 규칙 집합."""
    return {line.strip() for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")}


# ── 임시 채택자 repo 픽스처 (실 판정용) ────────────────────────────────────────


def _instance_repo(root: Path, gitignore_body: str, excludes_file: Path) -> Path:
    """`.project_manager/.gitignore` 본문만 반영한 임시 채택자 repo.

    유지보수자 머신의 global excludes / custom git-init template 이 판정에 섞이면 규칙이 없어도
    rc=0 이 나와 가드가 공허해진다 — 빈 excludes 파일 + 빈 `.git/info/exclude` 로 차단한다.
    """
    root.mkdir(parents=True)
    subprocess.run([_GIT, "init", "-q"], cwd=str(root), check=True, capture_output=True)
    excludes_file.write_text("", encoding="utf-8")
    (root / ".git" / "info" / "exclude").write_text("", encoding="utf-8")
    project_dir = root / ".project_manager"
    project_dir.mkdir()
    (project_dir / ".gitignore").write_text(gitignore_body, encoding="utf-8")
    return root


def _check_ignore(repo: Path, relpath: str, excludes_file: Path) -> int:
    """`git check-ignore` 실판정 rc (0=무시됨·1=추적 대상). 그 외 rc 는 판정 실패로 즉시 fail."""
    proc = subprocess.run(
        [_GIT, "-c", f"core.excludesFile={excludes_file}", "check-ignore", "-q", "--", relpath],
        cwd=str(repo), capture_output=True, text=True, encoding="utf-8",
    )
    assert proc.returncode in (0, 1), (
        f"git check-ignore 판정 실패(rc={proc.returncode}): {proc.stderr.strip()}")
    return proc.returncode


def _untracked_status(repo: Path, excludes_file: Path) -> str:
    proc = subprocess.run(
        [_GIT, "-c", f"core.excludesFile={excludes_file}",
         "status", "--porcelain", "--untracked-files=all"],
        cwd=str(repo), capture_output=True, text=True, encoding="utf-8",
    )
    assert proc.returncode == 0, f"git status 실패: {proc.stderr.strip()}"
    return proc.stdout


def _write(repo: Path, relpath: str, body: str = "본문\n") -> None:
    path = repo / ".project_manager" / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


# ── ① 생성 주체 ↔ ignore 규칙 동일 대상 ────────────────────────────────────────


def test_ignore_rule_targets_the_additional_reviewer_overlay_constant():
    """ignore 규칙 대상이 additional_reviewer 가 실제로 읽는 overlay 파일과 같다.

    상수를 rename 하면 규칙이 죽은 이름을 무시하게 되고 새 이름이 다시 untracked 로 샌다. 위치
    단언(부모 디렉토리 `.project_manager`)도 함께 건다 — bare-name 패턴은 규칙 파일이 있는
    디렉토리 하위에서만 유효하므로, overlay 가 다른 곳으로 옮겨지면 이 등재는 무력해진다.
    """
    overlay = _load_additional_reviewer().REVIEW_CONTEXT_FILE
    assert overlay.name == OVERLAY_FILE_NAME, (
        "additional_reviewer overlay 파일명이 바뀌었는데 ignore 규칙이 옛 이름에 남아 있다 — "
        f"규칙 대상을 {overlay.name!r} 로 갱신하라")
    assert overlay.parent.name == ".project_manager", (
        f"overlay 가 .project_manager/ 밖({overlay.parent}) 으로 이동 — "
        ".project_manager/.gitignore 의 bare-name 등재가 더 이상 그 경로를 덮지 않는다")


def test_every_overlay_literal_in_source_matches_the_registered_name():
    """additional_reviewer.py 안의 overlay 파일명 리터럴 전 지점이 등재명과 같다.

    모듈 로드 시점 바인딩 외에 PM 홈 재바인딩 지점이 같은 리터럴을 중복 보유한다 — 재바인딩
    쪽만 rename 되면 위 상수 단언은 green 인 채 `--pm-home` 해소 실행이 읽는 파일명이 갈린다.
    소스의 `*.local.md` 파일명 리터럴을 전수 스캔해 등재명 하나로 고정한다 (지점 수 하한 2).
    """
    source = (TOOLS / "additional_reviewer.py").read_text(encoding="utf-8")
    literals = re.findall(r'"([A-Za-z0-9_.-]+\.local\.md)"', source)
    assert len(literals) >= 2, (
        "overlay 파일명 리터럴이 2지점(모듈 바인딩 + PM 홈 재바인딩) 미만으로 줄었다 — "
        "경로 조립이 바뀌었으면 이 스캔 패턴을 실 조립 형태로 갱신하라")
    stray = [lit for lit in literals if lit != OVERLAY_FILE_NAME]
    assert not stray, (
        f"additional_reviewer.py 의 overlay 리터럴 {stray!r} 가 등재명 {OVERLAY_FILE_NAME!r} 와 "
        "다르다 — 전 지점을 같은 이름으로 맞추고 ignore 등재를 갱신하라")


# ── ② 엔진 + 출하 타깃 전량 등재 ──────────────────────────────────────────────


def test_guard_covers_engine_and_every_shipped_target():
    """파라미터 집합이 엔진 canonical + 존재하는 모든 출하 타깃을 덮는다 (열거 누락=조용한 약화)."""
    target_names = _target_names()
    assert target_names, "templates/ 에 출하 타깃이 없다 — 열거 전제 붕괴"
    assert GITIGNORE_PATHS[0] == ENGINE_GITIGNORE
    covered = {path.parents[1].name for path in GITIGNORE_PATHS[1:]}
    assert covered == target_names, (
        f"출하 타깃 커버리지 불일치 — 덮은 타깃 {sorted(covered)} vs 실재 {sorted(target_names)}")


@pytest.mark.parametrize("gitignore_path", GITIGNORE_PATHS, ids=_identify)
def test_shipped_gitignore_registers_review_context_overlay(gitignore_path: Path):
    """엔진과 출하 템플릿 사본의 `.project_manager/.gitignore` 가 overlay 를 등재한다.

    엔진만 고치고 전파를 빠뜨리면 채택자는 규칙 없는 사본을 받는다 —
    `pm_update.py --all-targets` 로 내보내야 한다.
    """
    assert gitignore_path.is_file(), f"ignore 규칙 파일 부재: {_identify(gitignore_path)}"
    rules = _rules(gitignore_path.read_text(encoding="utf-8"))
    assert OVERLAY_FILE_NAME in rules, (
        f"{_identify(gitignore_path)} 에 {OVERLAY_FILE_NAME} 미등재 — 인스턴스 overlay 가 "
        "커밋에 유입된다(T-0608). 엔진 파일에 추가한 뒤 pm_update.py --all-targets 로 전파하라")


# ── ③ git check-ignore 실판정 ─────────────────────────────────────────────────


@requires_git_binary
@pytest.mark.parametrize("gitignore_path", GITIGNORE_PATHS, ids=_identify)
def test_check_ignore_treats_overlay_as_ignored(gitignore_path: Path, tmp_path: Path):
    """임시 채택자 repo 에 실 overlay 를 만들면 `git check-ignore` rc=0 이고 status 에 안 뜬다.

    등재 문자열 단언(가드 ②)만으로는 규칙이 실제로 발효하는지 알 수 없다 — 뒤따르는 부정 패턴이나
    잘못된 구문이면 문자열은 있어도 파일은 계속 커밋 후보로 남는다.
    """
    excludes_file = tmp_path / "empty-global-excludes"
    repo = _instance_repo(
        tmp_path / "instance", gitignore_path.read_text(encoding="utf-8"), excludes_file)
    _write(repo, OVERLAY_FILE_NAME, "# 리뷰 컨텍스트 overlay\n")

    relpath = f".project_manager/{OVERLAY_FILE_NAME}"
    assert _check_ignore(repo, relpath, excludes_file) == 0, (
        f"{_identify(gitignore_path)} 적용 repo 에서 {relpath} 가 무시되지 않는다 "
        "— 규칙이 등재만 되고 발효하지 않음(T-0608)")
    assert OVERLAY_FILE_NAME not in _untracked_status(repo, excludes_file), (
        "overlay 가 git status 에 미추적으로 표면화 — 커밋 유입 경로가 열려 있다")


@requires_git_binary
@pytest.mark.parametrize("gitignore_path", GITIGNORE_PATHS, ids=_identify)
def test_check_ignore_leaves_adopter_owned_local_docs_trackable(
        gitignore_path: Path, tmp_path: Path):
    """채택자 소유 dev-state `wiki/*.local.md` 는 계속 추적 가능하다 (과일반화 금지·ticket 결정).

    `*.local.md` 와일드카드로 승격하면 채택자가 추적하기로 결정한 `pm_role.local.md`·
    `pm_playbook.local.md` 까지 엔진이 일방적으로 무시 처리한다 — 별개 클래스라 정확명으로 둔다.
    """
    excludes_file = tmp_path / "empty-global-excludes"
    repo = _instance_repo(
        tmp_path / "instance", gitignore_path.read_text(encoding="utf-8"), excludes_file)

    for relpath in ADOPTER_OWNED_LOCAL_DOCS:
        _write(repo, relpath)
        assert _check_ignore(repo, f".project_manager/{relpath}", excludes_file) == 1, (
            f"{_identify(gitignore_path)} 이 채택자 소유 {relpath} 까지 무시 — "
            "정확명 등재가 와일드카드로 번졌다(T-0608 결정 위반)")


# ── sensitivity — 가드가 실제로 도는지 (가짜 게이트 방지) ──────────────────────


@requires_git_binary
def test_check_ignore_verdict_comes_from_the_added_rule(tmp_path: Path):
    """규칙 줄을 빼면 판정이 rc=1 로 뒤집힌다 (rc=0 이 다른 패턴 덕이 아님을 증명).

    이 반전이 없으면 가드 ③ 은 어떤 본문에도 통과하는 공허한 단언이다.
    """
    body = ENGINE_GITIGNORE.read_text(encoding="utf-8")
    without_rule = "".join(
        f"{line}\n" for line in body.splitlines() if line.strip() != OVERLAY_FILE_NAME)
    assert OVERLAY_FILE_NAME not in _rules(without_rule), \
        "픽스처가 규칙 줄을 못 뺐다 — sensitivity 전제 붕괴"

    excludes_file = tmp_path / "empty-global-excludes"
    repo = _instance_repo(tmp_path / "instance", without_rule, excludes_file)
    _write(repo, OVERLAY_FILE_NAME, "# 리뷰 컨텍스트 overlay\n")

    relpath = f".project_manager/{OVERLAY_FILE_NAME}"
    assert _check_ignore(repo, relpath, excludes_file) == 1, \
        "규칙을 빼도 무시됨 — 판정이 다른 패턴에 기대고 있어 가드가 공허하다"
    assert OVERLAY_FILE_NAME in _untracked_status(repo, excludes_file), \
        "규칙 없이도 status 에 안 뜸 — 결함 재현 전제 붕괴(판정 환경 오염)"
