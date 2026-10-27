"""설치 공통 install_board (app)."""
from __future__ import annotations

from pathlib import Path
from pm_engine.infra import install_conf
from pm_engine.infra import install_files
from pm_engine.infra import raw_output
from typing import Callable
import functools
import shutil
import subprocess
import sys
import tempfile
from pm_engine.domain import install_rules
from pm_engine.infra import fs
import os

# 신규 공유 board 에 스캐폴드하는 상태 디렉토리 — `ticket_rules.STATUS_DIRS` 와 같은 집합(리터럴 · 동치는
# tests/test_status_dirs_single_truth.py 가 값으로 고정한다).
_SEEDED_STATUS_DIRS: tuple[str, ...] = ("open", "claimed", "blocked", "done", "discarded")


# ── board submodule 셋업 ─────────────────────────────────
# `--new --board-submodule --board-remote <url>` 은 board(tickets+areas)를 superproject inline 이
# 아니라 **별도 git submodule**(`.project_manager/board`)로 세운다(board 전용 git·공유
# remote·`ignore=all`). inline 기본(플래그 없음)은 이 경로를 전혀 타지 않아 완전 무변경(현행 --new
# 회귀). 공유 remote 의 호스팅/권한 생성은 사용자 게이트 — 엔진은 URL 을 받아 배선만 한다.

_BOARD_SUBMODULE_PATH = ".project_manager/board"  # 표준 `git submodule add` 은 name == path.


_BOARD_SETUP_GIT_TIMEOUT_SECONDS = 600  # remote clone/submodule add (네트워크·큰 board 여유).


# board submodule areas.md 스캐폴드 canonical 칼럼 — areas_rules._AREAS_COLUMNS 를 미러한다(pm_import 는
# stdlib-only·board import 회피). drift 는 test 가 board 를 로드해 대조한다(가드).
_BOARD_AREAS_COLUMNS = ("repo", "prefix", "git", "test_cmd", "owner", "base",
                        "protected", "area_owner")


# board submodule 의 `.gitattributes` seed — areas.md 동시 등록 안전(양쪽 행 보존)의 배포처.
# board 는 **별도 git** 이라 superproject 루트 `.gitattributes` 의 `.project_manager/areas.md
# merge=union` 선언이 닿지 않는다(`check-attr` = unspecified·실측). 신규 board 는 여기서 seed 하고,
# 이미 만들어진 board 는 board.py `_ensure_board_gitattributes` 가 멱등 backfill 한다(seed 재실행 없음).
# 내용은 board._BOARD_GITATTRIBUTES_BLOCK 을 미러한다 — drift 는 test 가 board 를 로드해 대조한다.
_BOARD_GITATTRIBUTES_SCAFFOLD = (
    "# areas.md = 멀티-PM prefix 레지스트리 — 동시 등록(행 append)이 merge 에서 충돌하지 않도록\n"
    "# git 내장 union merge 드라이버로 양쪽 행을 모두 보존한다.\n"
    "# board 는 별도 git 이라 superproject 루트의 같은 선언이 닿지 않는다 — 여기가 그 배포처다.\n"
    "areas.md merge=union\n"
    "# Windows checkout에서도 엔진-소유 텍스트의 논리 개행을 LF로 유지한다(라운드 회수 bytes·\n"
    "# 재작성 byte 판정). 엔진의 표기 보존이 근본이고 이 선언은 그 위의 방어층이다.\n"
    "*.md text eol=lf\n"
    "*.json text eol=lf\n"
    "*.txt text eol=lf\n"
    "*.jsonl text eol=lf\n"
)


def _board_setup_git(argv: list[str], cwd: Path | None) -> tuple[int, str]:
    """board submodule 셋업용 git 실행 → (rc, stdout+stderr). 실행 예외는 전파한다.

    `-c protocol.file.allow=always` 를 상시 얹어 로컬/`file://` remote(hermetic 테스트·self-split
    로컬 board) 의 submodule 전송을 허용한다 — ssh/https remote 는 그 transport 만 제어하므로 무영향
    (CVE-2022-39253 이후 file transport 는 submodule 재귀에서 기본 차단). git identity·credential 은
    사용자 환경 상속(공유 board=사용자-신뢰·credential helper/ssh-agent 그대로 작동). URL 은 호출부가
    `validate_upstream_value` 로 형태-안전 검증(credential-in-url·leading-dash·비허용 scheme 거부)한다.
    """
    git_binary = shutil.which("git")
    if git_binary is None:
        return 1, "git 바이너리를 찾을 수 없음 (PATH)."
    result = subprocess.run(
        [git_binary, "-c", "protocol.file.allow=always", *argv],
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=_BOARD_SETUP_GIT_TIMEOUT_SECONDS)
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def _board_areas_scaffold() -> str:
    """신규 공유 board 의 초기 areas.md (canonical 8칼럼 헤더만·데이터 행 0).

    board.py `_areas_header_line()`/`_areas_separator_line()` 와 같은 형식이다. 등록 repo 0 →
    `registered_prefixes()` 빈 set → 무prefix `T-NNNN`(합류/멀티-repo 등록 전). 이후 `board.py init
    --prefix` / `pm-config repo add` 가 데이터 행을 append 한다(append-only 레지스트리).
    """
    header = "| " + " | ".join(_BOARD_AREAS_COLUMNS) + " |"
    separator = "|" + "|".join("---" for _ in _BOARD_AREAS_COLUMNS) + "|"
    return (
        "# Area Registry\n\n"
        "> per-repo 레지스트리. `board.py init --prefix` / "
        "`pm-config repo add` 가 등록.\n\n"
        f"{header}\n{separator}\n"
    )


def _cleanup_partial_board(dest_root: Path) -> None:
    """submodule add 실패 후 부분 board 잔재 정리 (best-effort).

    board working tree·staged gitlink·`.git/modules` 캐시·(우리가 만든) `.gitmodules` 를 최대한
    원복한다(실패는 삼킴 — 이미 fail-loud 중). --new 는 빈 dest 보장이라 이번 run 이 만든 것뿐 →
    통째 되돌려도 사용자 자산 위험 0. 남은 트리 복사분(wiki/tickets 등)은 유지한다.

    **재시도는 fresh/비운 dest 로** — `setup_board_submodule` 첫 게이트가 `board/` 존재 시 중단하고
    `main` 의 `--new` 가드가 비-빈 dest 를 거부하므로, *같은* dest 재실행은 막힌다. 남은 트리를
    수동 정리(또는 새 경로)한 뒤 재-import 한다.
    """
    board_dir = dest_root / ".project_manager" / "board"
    _board_setup_git(["rm", "-f", "--cached", _BOARD_SUBMODULE_PATH], cwd=dest_root)
    _board_setup_git(["submodule", "deinit", "-f", _BOARD_SUBMODULE_PATH], cwd=dest_root)
    # `.git/modules` 캐시는 git object·packfile 이라 read-only 다 — 맨 rmtree 는 Windows 에서
    #   실패하고, 그 실패를 삼키면 잔재 때문에 fresh dest 재시도까지 막힌다(실측). 공용 seam 이
    #   속성을 풀고 재시도하며, 그래도 남으면 자리를 알린다(정리 실패로 죽지는 않는다 —
    #   호출부는 이미 fail-loud 중이다).
    for leftover in (board_dir,
                     dest_root / ".git" / "modules" / ".project_manager" / "board"):
        try:
            install_conf._force_rmtree(leftover)
        except OSError as exc:
            print(f"경고: 부분 board 정리 실패 — 직접 지우세요: {leftover} "
                  f"({type(exc).__name__}: {exc})", file=sys.stderr)
    gitmodules = dest_root / ".gitmodules"
    if gitmodules.exists():
        gitmodules.unlink()


def setup_board_submodule(dest_root: Path, remote_url: str) -> int:
    """--new --board-submodule: board(tickets+areas)를 `.project_manager/board` submodule 로 셋업.

    (board 전용 git·공유 remote·`ignore=all`) 반환 rc(0=성공·비0=fail-loud).

    순서(부작용 원자성·부분 홈 최소화):
      1. remote 를 temp 로 clone. **비었으면**(신규 공유 board) 복사된 wiki/tickets 스캐폴드(치환
         완료·open/claimed/blocked/done + _template + README) + canonical areas.md +
         `.gitattributes`(areas.md merge=union) 를 seed → commit → push. **내용 있으면**
         (2번째 유저 합류) skip(기존 board 재사용 — `.gitattributes` 는 board.py 가 backfill).
         (빈 remote 를 *직접* `submodule add` 하면 git 이 checkout 할 커밋이 없어 rc128 로 거부한다 —
          실측. 그래서 add *전* 에 remote 를 non-empty 로 만들어 두 경로를 수렴시킨다.)
      2. `git submodule add <url> .project_manager/board` — remote 가 non-empty 라 checkout 성공.
      3. `.gitmodules` 에 `ignore = all`(committed·공유 default) 설정 — board PM-commit 이 design(코드)
         git status 를 오염시키지 않게(누출 0). board.py init 이 추가로 `.git/config`
         ignore=all 도 설정한다(`_configure_board_submodule` 재사용·per-clone).
      4. 복사된 dormant `wiki/tickets` 제거 — board 는 이제 submodule 에 산다(board 루트가
         `board/tickets` 존재로 board/ 로 해소되므로 wiki/tickets 는 잉여).
    """
    board_dir = dest_root / ".project_manager" / "board"
    copied_tickets = dest_root / ".project_manager" / "wiki" / "tickets"
    if board_dir.exists():
        print(f"오류: {_BOARD_SUBMODULE_PATH} 가 이미 존재 — board submodule 셋업 중단.",
              file=sys.stderr)
        return 1
    tmp_clone = Path(tempfile.mkdtemp(prefix="pm_board_seed_", dir=raw_output.temp_root(dest_root)))
    try:
        # ── 1. clone remote → temp; 비었으면 스캐폴드 seed ──
        rc, out = _board_setup_git(["clone", remote_url, str(tmp_clone)], cwd=None)
        if rc != 0:
            print(f"오류: board remote clone 실패 (rc={rc}) — {remote_url}\n{out.strip()}",
                  file=sys.stderr)
            return rc or 1
        rc_head, _ = _board_setup_git(["rev-parse", "--verify", "HEAD"], cwd=tmp_clone)
        seeded = rc_head != 0  # HEAD 없음 = 커밋 0 = 빈 remote(신규 공유 board).
        if seeded:
            if not copied_tickets.is_dir():
                print("오류: 복사된 wiki/tickets 부재 — board 스캐폴드 불가(트리 복사 확인).",
                      file=sys.stderr)
                return 1
            shutil.copytree(copied_tickets, tmp_clone / "tickets")
            # ticket_rules.STATUS_DIRS 와 같은 집합 — 처분 종결 `discarded` 포함. 신규 공유
            # board 는 빈 상태 디렉토리도 추적돼야 다른 clone 이 checkout 에서 받는다.
            for status in _SEEDED_STATUS_DIRS:
                sd = tmp_clone / "tickets" / status
                sd.mkdir(parents=True, exist_ok=True)
                (sd / ".gitkeep").touch(exist_ok=True)  # 빈 status dir git 추적(합류 유저 checkout).
            (tmp_clone / "areas.md").write_text(
                _board_areas_scaffold(), encoding="utf-8", newline="\n")
            # areas.md merge=union 은 **이 git**(board)에 선언돼야 유효하다 — 루트 선언은 다른
            # git 이라 닿지 않는다. 신규 clone 이라 기존 파일 없음(비파괴 판단 불요).
            (tmp_clone / ".gitattributes").write_text(
                _BOARD_GITATTRIBUTES_SCAFFOLD, encoding="utf-8", newline="\n")
            for step in (["add", "-A"],
                         ["commit", "-m", "board scaffold (pm-import --new --board-submodule)"],
                         ["push", "origin", "HEAD"]):
                rc, out = _board_setup_git(step, cwd=tmp_clone)
                if rc != 0:
                    print(f"오류: board seed `git {step[0]}` 실패 (rc={rc})\n{out.strip()}",
                          file=sys.stderr)
                    return rc or 1
        else:
            # 내용 있는 remote(합류) — **실제 board 인지 검증**(codex+reviewer must-fix). 오타/비-board
            #   URL 을 주면 clone 은 성공하고 seeded=False 로 흘러 아래(try 밖)에서 submodule add + step 4
            #   가 dormant wiki/tickets 를 무조건 제거 → board/tickets 부재인데 폴백처(wiki/tickets)도
            #   삭제 = 깨진 dual-inert board 를 rc0 "완료"로 낸다. add·dest mutation *전*(부작용 0·finally
            #   가 temp 정리)에 tickets/·areas.md 존재로 board 형태를 확인해 명확 실패시킨다.
            if not (tmp_clone / "tickets").is_dir() or not (tmp_clone / "areas.md").exists():
                print(
                    f"오류: --board-remote {remote_url!r} 은 유효한 board repo 가 아닙니다 "
                    f"(tickets/·areas.md 부재). 신규면 *빈* remote 를, 합류면 기존 board remote 를 "
                    f"주세요 (오타·비-board URL 확인).",
                    file=sys.stderr)
                return 1
    finally:
        try:
            install_conf._force_rmtree(tmp_clone)
        except OSError as exc:
            # clone 사본(read-only git object 포함)이 tempdir 에 남는다 — 셋업 결과를 뒤집지
            #   않지만 침묵하지 않는다.
            print(f"경고: board seed 임시 clone 정리 실패 — 직접 지우세요: {tmp_clone} "
                  f"({type(exc).__name__}: {exc})", file=sys.stderr)

    # ── 2. submodule add (remote 는 이제 non-empty — checkout 성공) ──
    rc, out = _board_setup_git(
        ["submodule", "add", remote_url, _BOARD_SUBMODULE_PATH], cwd=dest_root)
    if rc != 0:
        print(f"오류: git submodule add 실패 (rc={rc}) — {remote_url}\n{out.strip()}",
              file=sys.stderr)
        if seeded:
            # 방금 우리가 remote 를 scaffold+push 했으므로 remote 는 이제 non-empty 다 — fresh dest 로
            #   재시도하면 합류(재사용) 경로를 탄다(빈 remote 초기화 아님·중복 scaffold 없음).
            print("  (remote 는 이미 scaffold 됨 — fresh dest 로 재시도 시 기존 board 합류 경로).",
                  file=sys.stderr)
        _cleanup_partial_board(dest_root)
        return rc or 1

    # ── 3. .gitmodules ignore=all (committed·공유 default) — 표준 submodule add 는 name==path ──
    rc, out = _board_setup_git(
        ["config", "-f", str(dest_root / ".gitmodules"),
         f"submodule.{_BOARD_SUBMODULE_PATH}.ignore", "all"], cwd=dest_root)
    if rc != 0:
        # 비차단 — board.py init 의 _configure_board_submodule 이 .git/config 로 보완한다.
        print(f"경고: .gitmodules ignore=all 설정 실패 (rc={rc}) — "
              f".git/config 로 보완(board.py init).\n{out.strip()}", file=sys.stderr)

    # ── 4. dormant wiki/tickets 제거 (board 는 submodule 에 산다) ──
    if copied_tickets.is_dir():
        try:
            install_conf._force_rmtree(copied_tickets)
        except OSError as exc:
            # 남으면 board 는 submodule 로 정상 동작하지만 잉여 트리가 채택자를 헷갈리게 한다 —
            #   셋업을 실패로 뒤집지는 않되 자리를 알린다.
            print(f"경고: dormant wiki/tickets 제거 실패 — 직접 지우세요: {copied_tickets} "
                  f"({type(exc).__name__}: {exc})", file=sys.stderr)

    label = "신규 스캐폴드 seed+push" if seeded else "기존 board 재사용(합류)"
    print(f"✓ board submodule 셋업: {_BOARD_SUBMODULE_PATH} "
          f"(remote={remote_url} · {label} · ignore=all)")
    return 0


# ── main ───────────────────────────────────────────────────────────────────

def _translate_dest_safety_errors(func: Callable) -> Callable:
    """경로 안전 예외를 CLI 경계에서 rc 1 + 친화 메시지로 번역하는 데코레이터(traceback 0).

    두 클래스를 모두 잡는다:
      - `DestRootSwappedError` — dest 루트 자체 교체. 파일 단위 교체는 각 단계가 흡수하지만
        (그 파일만 제외 + loud) **루트 교체는 어느 단계도 흡수하면 안 된다**(흡수하면 남은
        단계가 교체된 트리에 계속 쓴다). 실행 전체를 감싸 한 번만 잡고 즉시 끝낸다.
      - `UnsafeDestPathError` — 계획 단계는 각 호출부가 이미 rc1 로 번역하지만, **복사 단계**의
        fd 가드(`CopyAction.run`)도 이 예외를 던질 수 있다. 백스톱이 없으면 그 경로만 traceback
        으로 끝나 `add_harness_cli` 와 비대칭이 된다.

    래핑 함수를 쪼개지 않고 데코레이터로 두는 이유: `main` 본문을 별도 함수로 옮기면 엔진 관용구
    가드(진입 `main()` 의 console helper 선행·하네스 이름 분기 면제)가 이름 기준이라 함께 깨진다."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs) -> int:
        try:
            return func(*args, **kwargs)
        except (install_files.DestRootSwappedError, install_files.UnsafeDestPathError) as exc:
            print(f"오류: {exc}", file=sys.stderr)
            return 1
    return wrapper


# ── board.py init 호출 ─────────────────────────────────────────────────────

def run_board_init(dest_root: Path) -> int:
    """복사된 트리의 셋업(`pm-config init`)을 인자 없이 호출 — areas repo 행·local.conf·
    pm_state·pre-push 훅 + 이 홈의 첫 슬롯 행 등록.

    인자 0 호출은 그대로 성립한다 — init 은 `--prefix` 없이도 이 clone 의 repo 행을 등록하고
    (prefix 칼럼은 빈 채 = 무prefix `T-NNNN` 카테고리), 그 등록에는 `--area`·사용자 승인이
    필요 없다(신규 카테고리 신설이 아니다).

    같은 인터프리터(sys.executable)로 호출 — board.py 는 pyyaml 의존이라 venv 보존 필요.
    비대화형(stdin 비-tty)이면 board.py 는 대화형 질문을 건너뛰고 아무것도 묻지 않는다.
    stdin=DEVNULL 의 isatty() 가 Windows 서 신뢰불가라, env 로
    `PM_NONINTERACTIVE=1` 을 명시 전달해 결정적으로 skip 시킨다 (isatty 폴백 보조).

    `PYTHONDONTWRITEBYTECODE=1` 도 함께 전달한다— board.py 는 같은 tools/ 디렉토리의
    `pm_engine` 패키지를 import 하는데, 이 실행이 갓 복사된 새 프로젝트 트리 안(`dest_root`)에서
    일어나므로 표준 바이트코드 캐싱이 `dest_root/.project_manager/tools/__pycache__/` 를 새로 만든다 — fresh
    import 직후 빌드 산출물이 섞이는 것을 막는다(`test_import_excludes_pycache`).
    """
    tools = dest_root / ".project_manager" / "tools"
    entry = tools / "pm_config.py"
    if not entry.exists():
        # 셋업 진입은 pm_config 가 소유한다(board init 위임 + 홈 슬롯 등록). 그 파일이 없으면
        # 복사가 반쪽이라는 뜻이라 여기서 멈춘다 — board 로 우회하면 등록이 조용히 빠진다.
        print(f"경고: pm_config.py 없음 ({entry}) — init 건너뜀.", file=sys.stderr)
        return 1
    result = subprocess.run(
        [sys.executable, str(entry), "init"],
        cwd=str(dest_root),
        stdin=subprocess.DEVNULL,
        env={**os.environ, "PM_NONINTERACTIVE": "1", "PYTHONDONTWRITEBYTECODE": "1"},
        encoding="utf-8",
        errors="replace",
    )
    return result.returncode


def git_init(dest_root: Path) -> int:
    """--new 대상에 git init. 이미 .git 있으면 skip(0). returncode 를 반환한다.

    MF2: git init 실패를 무시하면 git repo 없는 불완전 import 가 성공으로 끝난다 —
    board.py init 의 pre-push 훅이 git repo 에 의존하므로 명세상 치명적이다. returncode 를
    그대로 돌려주고, main 이 비0 이면 import 미완으로 판정한다.
    """
    if (dest_root / ".git").exists():
        return 0
    result = subprocess.run(
        ["git", "init", str(dest_root)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode


def ensure_backup_dir_gitignored(
    dest_root: Path, git_safe: set | None, copied_relpaths: set,
) -> str:
    """dest .gitignore 에 `.pm_import_backups/` 패턴을 보장한다 (git 위생).

    반환 상태: "added"(기존에 append) · "created"(신규 .gitignore 생성) ·
      "present"(이미 무시 중·멱등 skip) · "unsafe-skip"(사전 존재 unbacked 사용자 .gitignore —
      비파괴 위해 미변경) · "noop"(읽기 실패).

    **비파괴:** 기존 .gitignore 를 append 하려면 둘 중 하나여야 한다 —
      (a) **git-safe**(추적 중 & 미변경 → git 이 복원 가능), 또는
      (b) **이번 import 가 복사·관리한 파일**(`.gitignore` ∈ copied_relpaths) — 이 경우 사용자
          원본이 있었다면 CopyAction 이 이미 중앙 백업했으므로 append 가 안전하다.
    둘 다 아니면 *사전 존재하는 unbacked 사용자 파일*이므로(미추적/dirty·import 가 안 건드림)
    무백업 변조를 피해 "unsafe-skip" 으로 수동 추가를 안내한다(이 append 는 CopyAction 백업
    경로를 타지 않으므로 별도 가드 필요). .gitignore 가 없으면 새로 만든다(비파괴·신규 파일).
    """
    pattern = f"{install_rules.BACKUP_DIR_NAME}/"
    gitignore = dest_root / ".gitignore"
    # .gitignore 가 symlink 면 write_text 가 링크 대상(프로젝트 밖 가능)을
    #   따라가 변조한다 — git-safe 여도 링크 대상은 git 복원 대상이 아니다. CopyAction 의 symlink
    #   비파괴 정책(follow_symlinks=False)과 일관되게 자동 append 를 거부한다.
    if gitignore.is_symlink():
        return "unsafe-skip"
    if gitignore.is_file():
        try:
            text = fs.read_text_shared(gitignore, encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            return "noop"
        # 이미 무시 중이면(정확한 패턴 줄·앞뒤 공백 무시) skip — 멱등.
        existing_lines = {line.strip() for line in text.splitlines()}
        if pattern in existing_lines or install_rules.BACKUP_DIR_NAME in existing_lines:
            return "present"
        git_safe_ok = git_safe is not None and ".gitignore" in git_safe
        import_owned = Path(".gitignore") in copied_relpaths
        if not (git_safe_ok or import_owned):
            return "unsafe-skip"  # 사전 존재 unbacked 사용자 파일 — 무백업 변경 금지.
        prefix = "" if text.endswith("\n") or text == "" else "\n"
        new_text, status = f"{text}{prefix}{pattern}\n", "added"
    else:
        new_text, status = f"{pattern}\n", "created"
    # 방어: 위생 write 실패(권한 등)가 *복사·치환이 끝난* import 말미를
    #   깨뜨리지 않게 한다 — gitignore 위생은 should 부가단계라 실패해도 import 자체는 성공으로 둔다.
    try:
        gitignore.write_text(new_text, encoding="utf-8", newline="\n")
    except OSError:
        return "noop"
    return status
