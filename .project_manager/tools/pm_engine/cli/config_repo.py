"""repo 등록·보호목록·목록 CLI."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from pm_engine.domain import areas_rules
from pm_engine.infra import areas_store, console, fs, lease_store, pm_paths
from pm_engine.app import repo_protection
from pm_engine.infra import git, protected_hooks
from pm_engine.infra import worktree_git
from pm_engine.domain import worktree_records
from pm_engine.app import areas_registry
from pm_engine.app import board_sync


# ── base 브랜치 해소 ─────────────────────────────────────────────────

# `--base` 검증 실패(없는 브랜치)를 빈문자열("미해소")과 구별하는 sentinel. None 폴백
# (bare HEAD 해소 실패)은 빈 문자열로 surface 하지만, *명시 base 가 검증 실패*하면 등록을
# 막아야 하므로(잘못된 base 기록 방지) 이 sentinel 로 호출부에 신호한다.
_BASE_INVALID = object()


def _resolve_base(base_arg: str | None, bare_path: Path, *, runner: worktree_records.GitRunner):
    """repo add base 브랜치를 해소한다. bare(`.repos/<name>.git`)는 존재 전제.

    git 호출은 주입된 clone `runner` 를 `-C <bare>` 로 재사용한다(별도 DI seam 안 만듦·
    `_real_clone_runner` 가 `git <argv>` 형태라 `-C` 를 argv 로 넣으면 그 repo 컨텍스트).

      - `base_arg` 미지정(None) → bare HEAD 해소(`git -C <bare> symbolic-ref HEAD` full ref
        = 원격 default 브랜치)를 base 로 명시값화한다. 해소 실패(rc≠0/비-브랜치 ref)는 빈 문자열
        ("미해소"·worktree add 가 bare HEAD 폴백·현행 동작) — repo 등록 자체는 막지 않는다.
      - `base_arg` 지정 → **로컬 브랜치** 검증(`git -C <bare> show-ref --verify --quiet
        refs/heads/<b>` rc==0). `show-ref --verify` 는 exact-ref primitive(revision 문법
        미적용)라 태그·SHA·`HEAD`·원격 ref 는 물론 `main~0`·`main^{}` 같은 revision 표현도
        통과하지 못한다. 통과면
        반환값은 **기존대로 bare 브랜치명(`base_arg`)**(areas.md base 칼럼 규격 불변), 실패면
        `_BASE_INVALID`(호출부가 명확한 에러 rc 1 로 surface·등록 차단).

    반환: 해소된 base 문자열(빈 문자열 = 미해소·None 동등) 또는 `_BASE_INVALID`(검증 실패).
    """
    if base_arg is None:
        # full ref(`--short` 아님) → `refs/heads/` 접두 정확 제거 (동명 태그 존재 시
        # `--short` 가 `heads/<name>` 로 오염되던 모호성 회피). 실패·비-브랜치 ref → 미해소("").
        rc, out = runner(["-C", str(bare_path), "symbolic-ref", "HEAD"])
        ref = out.strip()
        if rc != 0 or not ref.startswith(worktree_git._SYMREF_BRANCH_PREFIX):
            return ""  # bare HEAD 해소 실패 → 미해소(worktree add 가 bare HEAD 폴백·현행).
        return ref[len(worktree_git._SYMREF_BRANCH_PREFIX):]
    # refs/heads/<b> exact-ref 검증 — 태그·SHA·HEAD·원격 ref·revision 문법(main~0·main^{}) 거부,
    # 로컬 브랜치만 통과. show-ref --verify 는 revision 문법을 적용 안 하는 exact-ref primitive.
    rc, _out = runner(
        ["-C", str(bare_path), "show-ref", "--verify", "--quiet", f"refs/heads/{base_arg}"]
    )
    if rc != 0:
        return _BASE_INVALID  # 비-로컬-브랜치(태그/SHA/HEAD/부재) → 등록 차단(잘못된 base 기록 방지).
    return base_arg


# ── bare clone fetch refspec 보정 ───────────────────────────────────

# `git clone --bare` 가 설정하지 않는 fetch refspec — 일반(non-bare) clone 은 이 줄을
# remote.origin.fetch 에 박지만 `--bare` 는 생략한다. 그 결과 bare 에 origin/* remote-tracking
# ref(origin/main 등)가 안 생겨, 그 bare 를 공유하는 worktree 슬롯이 핸드오프 라이브-게이트
#의 baseline(@{upstream}/origin/main)을 해소 못 한다. multi-PM 패밀리 공통 결함.
_BARE_FETCH_REFSPEC = "+refs/heads/*:refs/remotes/origin/*"


def _set_bare_fetch_refspec(bare_path: Path, *, runner: worktree_records.GitRunner) -> None:
    """bare repo 에 fetch refspec 을 박고 origin/* remote-tracking ref 를 채운다 (fail-soft).

    `git clone --bare` 는 일반 clone 과 달리 `remote.origin.fetch` 를 설정하지 않아 bare 에
    `refs/remotes/origin/*` 가 영영 안 생긴다 → 그 bare 를 공유하는 worktree 슬롯이 핸드오프
    라이브-게이트의 baseline(origin/main)을 해소 못 한다(ambiguous→surface). 이를:
      1. `git -C <bare> config remote.origin.fetch '+refs/heads/*:refs/remotes/origin/*'`
         (set=덮어쓰기·멱등 — 재실행/refspec-없는 과거 bare 보정 안전).
      2. `git -C <bare> fetch origin` (origin/* remote-tracking ref 채움 → origin/main 생성).
    로 근절한다(`--mirror` 가 아니라 명시 refspec — 국소·안전·push --mirror 부작용 없음·결정).

    git 호출은 주입된 clone `runner` 를 `-C <bare>` 로 재사용한다(별도 DI seam 안 만듦·base
    해소가 symbolic-ref/show-ref 를 `-C <bare>` 로 호출하는 패턴과 동일). **fail-soft**:
    refspec set 실패·fetch 실패(네트워크)는 경고를 surface 하되 repo add 자체는 막지 않는다
    (refspec 은 박혔으니 이후 fetch 로 복구 가능). bare 는 존재 전제(clone 성공/재사용 후 호출).
    """
    rc, out = runner(
        ["-C", str(bare_path), "config", "remote.origin.fetch", _BARE_FETCH_REFSPEC]
    )
    if rc != 0:
        print(
            f"[경고] bare fetch refspec 설정 실패 (rc={rc}): {out.strip()[:200]}\n"
            f"  수동으로 `git -C {bare_path} config remote.origin.fetch '{_BARE_FETCH_REFSPEC}'` "
            "후 `git -C <bare> fetch origin` 하라 (origin/* remote-tracking ref·baseline 해소).",
            file=sys.stderr,
        )
        return  # refspec 미설정이면 fetch 해도 origin/* 안 채워짐 — skip.
    rc, out = runner(["-C", str(bare_path), "fetch", "origin"])
    if rc != 0:
        print(
            f"[경고] bare `git -C {bare_path} fetch origin` 실패 (rc={rc}): {out.strip()[:200]}\n"
            "  refspec 은 설정됨 — 네트워크 복구 후 `git -C <bare> fetch origin` 으로 origin/* 채우면 "
            "라이브-게이트 baseline(origin/main)이 해소된다.",
            file=sys.stderr,
        )
        return
    print("✓ bare fetch refspec 설정 + fetch origin — origin/* remote-tracking ref 채움.")


def _ensure_bare_branch_tracking(bare_path: Path, *, runner: worktree_records.GitRunner) -> None:
    """bare repo 의 기본 브랜치에 origin tracking(branch.<d>.remote/merge)을 박는다 (fail-soft).

    `git clone --bare` 는 일반(non-bare) clone 과 달리 로컬 브랜치 tracking config
    (`branch.<n>.remote`/`branch.<n>.merge`)를 설정하지 않는다 → 그 bare 를 공유하는 worktree
    슬롯의 **기본 브랜치가 `@{upstream}`(origin/<default>)을 해소 못 한다**(livegate baseline·
    handoff freshness·`git status` ahead/behind·`git pull` 무인자 영향).
    (`_set_bare_fetch_refspec`)가 tracking 의 한 절반(origin/* remote-tracking ref)을 채웠고,
    이 함수가 나머지 절반(로컬 `branch.<d>.*` tracking config)을 채운다. bare 에 설정하면
    worktree 가 공유 common config(`branch.*`)로 **상속**한다(worktree-local config.worktree 아님).

    동작:
      1. 기본 브랜치 = bare HEAD 해소(`git -C <bare> symbolic-ref HEAD` full ref, 실패·
         비-브랜치 ref 시 `git -C <bare> rev-parse --abbrev-ref HEAD` 폴백).
      2. `git -C <bare> config branch.<d>.remote origin` (set=덮어쓰기·멱등).
      3. `git -C <bare> config branch.<d>.merge refs/heads/<d>`.
    비-bare `git clone` 이 checked-out 기본 브랜치만 tracking 거는 동작을 미러 — **기본 브랜치만**
    설정한다(전 브랜치 일괄 tracking 아님·로컬-전용 의도 브랜치를 잘못 tracking 걸지 않기 위해).

    git 호출은 주입된 clone `runner` 를 `-C <bare>` 로 재사용한다(별도 DI seam 안 만듦·
    `_set_bare_fetch_refspec`·base 해소 동형). **fail-soft**: bare HEAD 미해소·config
    실패는 경고를 surface 하되 repo add 자체는 막지 않는다(tracking 은 *추가 가드*이지 repo add
    핵심 부작용 아님). bare 는 존재 전제(clone 성공/재사용 후 호출).
    """
    # full ref(`--short` 아님) → `refs/heads/` 접두 정확 제거 (동명 태그 모호성 회피).
    rc, out = runner(["-C", str(bare_path), "symbolic-ref", "HEAD"])
    ref = out.strip()
    if rc == 0 and ref.startswith(worktree_git._SYMREF_BRANCH_PREFIX):
        default_branch = ref[len(worktree_git._SYMREF_BRANCH_PREFIX):]
    else:
        # detached·비-symbolic HEAD(비-브랜치 ref) 폴백 — 현재 브랜치명을 rev-parse 로 시도.
        rc, out = runner(["-C", str(bare_path), "rev-parse", "--abbrev-ref", "HEAD"])
        default_branch = out.strip()
    if rc != 0 or not default_branch or default_branch == "HEAD":
        print(
            f"[경고] bare 기본 브랜치 해소 실패 (rc={rc}): {out.strip()[:200]}\n"
            f"  수동으로 `git -C {bare_path} config branch.<d>.remote origin` + "
            f"`git -C {bare_path} config branch.<d>.merge refs/heads/<d>` 하라 "
            "(기본 브랜치 @{upstream} 해소).",
            file=sys.stderr,
        )
        return  # 기본 브랜치 미해소면 tracking 을 걸 대상이 없음 — skip(fail-soft).
    rc, out = runner(
        ["-C", str(bare_path), "config", f"branch.{default_branch}.remote", "origin"]
    )
    if rc != 0:
        print(
            f"[경고] bare `branch.{default_branch}.remote` 설정 실패 (rc={rc}): {out.strip()[:200]}\n"
            f"  수동으로 `git -C {bare_path} config branch.{default_branch}.remote origin` + "
            f"`git -C {bare_path} config branch.{default_branch}.merge refs/heads/{default_branch}` "
            "하라 (기본 브랜치 @{upstream} 해소).",
            file=sys.stderr,
        )
        return
    rc, out = runner(
        ["-C", str(bare_path), "config",
         f"branch.{default_branch}.merge", f"refs/heads/{default_branch}"]
    )
    if rc != 0:
        print(
            f"[경고] bare `branch.{default_branch}.merge` 설정 실패 (rc={rc}): {out.strip()[:200]}\n"
            f"  remote 는 설정됨 — 수동으로 `git -C {bare_path} config "
            f"branch.{default_branch}.merge refs/heads/{default_branch}` 로 보완하라.",
            file=sys.stderr,
        )
        return
    print(
        f"✓ bare 기본 브랜치 origin tracking 설정: branch.{default_branch}.remote=origin + "
        f"merge=refs/heads/{default_branch} (슬롯 @{{upstream}} 해소)."
    )


# ── 풀 슬롯 사본 루트 ignore 규칙 ───────────────────────────────────

# 위임 사본 루트(run-dir)를 숨기는 정본 규칙 — 두-git 풀 슬롯의 정본은 그 풀 bare 의
# `info/exclude` 다. bare 의 exclude 는 `$GIT_COMMON_DIR/info/exclude` 라 그 풀의 모든 linked
# worktree 가 공유하므로, 앱 저장소에 프레임워크 파일을 두거나 사람이 exclude 를 손으로
# 고치는 절차가 사라진다. 값은 `delegate_rounds.TICKET_COPY_POOL_IGNORE_RULE` 과 같은
# 문자열이어야 한다(판정하는 쪽과 설치하는 쪽의 불변식은 테스트가 고정). 루트 앵커 디렉터리
# 규칙이라 저장소가 추적하는 `.project_manager/tools`·`wiki` 는 숨기지 않는다.
TICKET_COPY_POOL_IGNORE_RULE = "/.project_manager/.local/"


_TICKET_COPY_EXCLUDE_COMMENT = "# pm_delegate ticket copy run-dir (engine-owned)"


def _ensure_bare_ticket_copy_exclude(bare_path: Path) -> bool:
    """bare `info/exclude` 에 사본 루트 ignore 규칙을 멱등 설치한다 (순수 파일 IO·fail-soft).

    규칙 줄이 이미 있으면 아무것도 쓰지 않는다(두 번 불러도 줄은 하나). 없을 때만 주석 1줄 +
    규칙 1줄을 append 한다 — **남이 넣은 줄은 건드리지 않는다**(옛 손 편집 줄 제거는 사람이
    1회 하는 데이터 정리이지 이 함수의 일이 아니다).

    bare 부재는 no-op + 경고다. 없는 자리에 `info/exclude` 를 만들면 `<repo>.git` 디렉터리가
    생겨 나중 `git clone --bare` 가 "destination path already exists" 로 죽는다.

    **fail-soft**: 쓰기 실패는 경고를 surface 하되 repo 등록·슬롯 생성 자체는 막지 않는다
    (옆 두 헬퍼 `_set_bare_fetch_refspec`·`_ensure_bare_branch_tracking` 과 같은 계약). 조용한
    생략이 아니다 — 진짜 게이트는 `delegate_copy_store`의 사본 루트 검사(fail-loud)이고, 여기서 rc 를
    1 로 만들면 exclude 쓰기 실패가 repo 등록 자체를 막는다. 반환 = 규칙이 설치된 상태인지.
    """
    exclude = bare_path / "info" / "exclude"
    if not bare_path.is_dir():
        print(
            f"[경고] 사본 루트 ignore 규칙을 설치하지 못했다 — bare 부재: {bare_path}. "
            f"이 풀의 슬롯에서 위임 라운드 준비(`ticket prepare`)가 거부된다 — bare 복구 후 "
            f"`{repo_protection._FACADE_PROG} repo add <repo>` 로 재설치하라 (멱등).",
            file=sys.stderr,
        )
        return False
    try:
        existing = (
            fs.read_text_shared(exclude, encoding="utf-8")
            if exclude.is_file() else ""
        )
        if any(line.strip() == TICKET_COPY_POOL_IGNORE_RULE
               for line in existing.splitlines()):
            return True
        exclude.parent.mkdir(parents=True, exist_ok=True)
        lead = "" if (not existing or existing.endswith("\n")) else "\n"
        with exclude.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(
                f"{lead}{_TICKET_COPY_EXCLUDE_COMMENT}\n{TICKET_COPY_POOL_IGNORE_RULE}\n"
            )
    except OSError as exc:
        print(
            f"[경고] 사본 루트 ignore 규칙 설치 실패: {exclude} — {exc}. 이 풀의 슬롯에서 위임 "
            f"라운드 준비(`ticket prepare`)가 거부된다 — 수동으로 그 파일에 "
            f"`{TICKET_COPY_POOL_IGNORE_RULE}` 줄을 넣거나 권한 복구 후 "
            f"`{repo_protection._FACADE_PROG} repo add <repo>` 를 재실행하라 (멱등).",
            file=sys.stderr,
        )
        return False
    print(f"✓ 사본 루트 ignore 규칙 설치: {exclude} (`{TICKET_COPY_POOL_IGNORE_RULE}`).")
    return True


# ── 서브커맨드 핸들러 ─────────────────────────────────────────────────────────


def _resolve_clone_git_url(home, name, cli_git, already_registered):
    """bare clone 소스 git URL 을 해소한다 (multi-user hydrate). 실패 → None(fail-loud 신호).

    `--git` 미제공 시 areas.md `git` 칼럼(레지스트리·git-tracked·공유)에서 URL 을 읽어,
    already-registered repo 의 `.repos/<repo>.git` bare mirror 를 재제공 없이 hydrate 한다 —
    하나의 채택 폴더를 여러 사람이 clone 할 때 mirror(`.repos/`·gitignore·per-clone)가 없는
    2번째 사용자 시나리오. 반환 `None` 은 호출자(cmd_repo_add)가 rc 1 로 바꾸는 fail-loud
    신호다(에러/경고 문구는 이 함수가 이미 print — 부작용 0·clone 전에 호출됨).

      - already_registered:
          - `--git` 미제공          → areas URL 로 해소(hydrate). areas 에도 URL 없으면 fail-loud.
          - `--git` 제공·areas 값과 다름 → 경고 + **areas 값 우선**(등록=단일 진실·mirror origin 은
                                        등록과 일치해야). CLI URL 로 바꾸려면 areas.md 를 직접 수정.
          - `--git` 제공·일치/areas 빔  → `--git` 값.
      - 미등록:
          - `--git` 제공            → 그 값(신규 repo 등록).
          - `--git` 미제공          → fail-loud(신규 repo 는 clone 원 URL 을 areas 에서 해소 불가).
    """
    areas_url = areas_store._areas_git_url(home, name) if already_registered else None

    if cli_git:
        if already_registered and areas_url and areas_url != cli_git:
            print(
                f"[경고] `--git {cli_git}` 가 areas.md 등록 URL({areas_url}) 과 다르다 — "
                "등록이 단일 진실이므로 areas URL 로 bare mirror 를 만든다(mirror origin 은 "
                "등록과 일치해야 한다). CLI URL 로 바꾸려면 areas.md 를 직접 수정하라.",
                file=sys.stderr,
            )
            return areas_url
        return cli_git

    # `--git` 미제공
    if already_registered:
        if areas_url:
            print(f"✓ `--git` 미제공 — areas.md 등록 URL 로 hydrate: {areas_url} (2번째 사용자).")
            return areas_url
        print(
            f"[중단] repo {name!r} 는 areas.md 등록됐으나 `git` 칼럼이 비어 clone 원 URL 을 "
            "해소할 수 없다 — `--git <url>` 로 명시하라 (clone/등록 전혀 하지 않았다).",
            file=sys.stderr,
        )
        return None
    print(
        f"[중단] `--git <url>` 필수 — repo {name!r} 는 areas.md 미등록이라 clone 원 URL 을 "
        "areas 에서 해소할 수 없다(신규 repo 등록). multi-user 2번째 사용자 hydrate 라면 먼저 "
        "등록(areas.md)을 공유받아야 한다 (clone/등록 전혀 하지 않았다).",
        file=sys.stderr,
    )
    return None


# `git ls-remote --exit-code <원본> HEAD` 가 일치 ref 없음(HEAD 없음 = 커밋 0개 원본)에 내는 rc.
_LS_REMOTE_NO_MATCHING_REF = 2


def cmd_repo_add(
    home,
    args: argparse.Namespace,
    *,
    clone_runner: worktree_records.GitRunner | None = None,
    repos_dir: Path | None = None,
) -> int:
    """`repo add <name> [--git <url>] [--test "<cmd>"] [--base <branch>]` — 패밀리에 repo 등록.

    **`--git` 은 optional**: 미제공 시 이미 등록된 repo 는 areas.md `git` 칼럼 URL 로
    bare mirror 를 hydrate 한다(하나의 채택 폴더를 여러 사람이 clone 할 때 `.repos/` mirror 가
    없는 2번째 사용자 시나리오·재제공 없이 재사용). 미등록 신규 repo 는 `--git` 필수(부작용 0
    fail-loud). clone 소스 URL 해소는 `_resolve_clone_git_url` 이 담당한다(불일치 시 areas 우선).

    1. areas.md 레지스트리에 per-repo 줄을 기록한다(areas_registry.areas_append — repo/prefix/
       git/test_cmd/owner/base 칼럼). **prefix 는 빈 값으로 등록**한다 —
       prefix=작업 카테고리이지 repo명이 아님.
       카테고리가 필요하면 `board.py new --prefix <cat>` 로 티켓별 명시하거나 `board.py
       prefix` 로 사후 관리한다. owner = 등록 식별자(registrant·협업 소유자 아님) —
       기본 현 세션.
    2. `.repos/<name>.git` 로 bare clone 한다 — worktree 풀이 공유하는 .git 원(
       worktree add 가 이 bare 를 base 로 슬롯을 만든다).

    **base 브랜치 해소** — clone 후, areas 등록 *전*(등록 줄에 base 를 박아야 하므로):
      - `--base` 미지정 → bare HEAD(`git -C .repos/<name>.git symbolic-ref HEAD` full ref
        = 원격 default 브랜치)를 해소해 명시값화·기록. worktree add 가 슬롯 브랜치를 그
        base 에서 파생한다. HEAD 해소 실패 시 base 빈 값(현행 bare HEAD 동작 폴백).
      - `--base <b>` 지정 → 로컬 브랜치 검증(`git -C .repos/<name>.git show-ref --verify --quiet refs/heads/<b>` rc==0).
        없으면 명확한 에러 rc 1(clone 은 됐으나 areas 등록은 막아 잘못된 base 기록 방지).

    **멱등·재시도 가능** (두 부작용 — bare clone + areas 등록 — 이 부분 성공할 수 있음):
      - 이미 등록 + bare 존재     → 완전 no-op rc0 (둘 다 이미 됨·친절 메시지).
      - 이미 등록 + bare 부재     → clone *진행*(재시도) 후 등록 건너뜀(append-only).
                                    첫 실행이 등록만 남기고 clone 실패한 경우의 복구 경로다.
      - 미등록                    → clone → base 해소·검증 → areas_append.
    clone 직전에 원본을 `git ls-remote --exit-code <url> HEAD` 로 조회한다 — 커밋 0개(rc 2)나
    조회 실패면 rc 1 로 멈추고 빈/부분 bare 를 만들지 않는다.
    base 해소가 clone 에 의존하므로(bare HEAD 해소/존재 검증) **clone 을 먼저** 한다 —
    미등록 경로는 clone→base 해소→areas_append. 이미 등록 경로는 base 가 이미 박혀 있어
    재해소하지 않는다(append-only·중복 등록 금지).
    clone 실패해도 등록이 남았지만, 이제 clone 성공 후에만 등록한다(재실행이 둘 다 다시 함).

    clone_runner / repos_dir 주입으로 hermetic 테스트(실 등록·clone 없이 배선 검증).
    base 해소 git 호출(symbolic-ref/show-ref)은 같은 `clone_runner` 를 `-C <bare>` 로 재사용한다.
    """

    # 명시 repos_dir가 없으면 전달받은 Home의 bare 저장소 디렉터리를 사용한다.
    repos_dir = repos_dir if repos_dir is not None else (home.root / ".repos")

    name = args.name

    # name 가드 — **어떤 부작용(bare clone·areas_append·훅 설치)보다 앞에서** 검증한다.
    # `../x` 폴더탈출·슬래시/공백/`.` 의 areas.md 줄 corruption·leading `-`·빈 문자열을 입구에서
    # 막는다(fail-closed·부분 부작용 0). CLI·콘솔 두 경로가 결국 이 sink 로 수렴하므로 여기 한 곳.
    if not areas_rules._validate_repo_name(name):
        print(
            f"[중단] repo 이름 {name!r} 형식 위반 — 허용: 영숫자로 시작, 이후 영숫자/`_`/`-` "
            "(정규식 `^[A-Za-z0-9][A-Za-z0-9_-]*$`). 경로분리자(`/`)·`.`·공백·leading `-`·빈 "
            "이름은 금지(폴더탈출·areas.md 줄 corruption 방지). clone/등록/훅 전혀 하지 않았다.",
            file=sys.stderr,
        )
        return 1

    # 보호목록 형식 검증 — **어떤 부작용(bare clone·areas_append·훅 설치)보다
    # 앞에서**. 미지정(None)이면 빈 칼럼 = `_repo_protected` 의 DEFAULT_PROTECTED 폴백(현행).
    # 명시했으면 토큰 형식만 본다(브랜치 실재는 검증 안 함 — 아직 없는 `release` 를 미리 보호하는
    # 게 정상). 빈 문자열은 "보호 없음" 을 뜻할 수 없으므로 fail-loud(플래그 생략으로 안내).
    protected_arg = getattr(args, "protected", None)
    protected_cell = ""
    protected_tokens: list[str] = []
    if protected_arg is not None:
        tokens, reason = areas_rules._validate_protected_tokens(protected_arg)
        if tokens is None:
            print(
                f"[중단] --protected {protected_arg!r} 거부 — {reason} "
                "(clone/등록/훅 전혀 하지 않았다·부작용 0). 기본값(main/master/develop)이면 "
                "`--protected` 를 생략하라.",
                file=sys.stderr,
            )
            return 1
        protected_tokens = tokens
        protected_cell = ",".join(tokens)

    # case-only 중복 거부 (repo명=prefix 동일성=case-insensitive fold) — 정확-case 는
    # 아래 already_registered 재사용(멱등) 경로로 빠지지만, case 만 다른 근접 중복(`svc` vs 등록
    # `SVC`)은 레지스트리에 fold-충돌하는 두 행을 만든다(네임스페이스 분할). **어떤 부작용(clone·
    # mkdir·등록·훅)보다 앞에서** 등록 canonical case 로 안내하고 fail-loud(부작용 0).
    registered_names = areas_store.registered_repos(home)
    if name not in registered_names:
        fold = name.lower()
        conflict = next((r for r in registered_names if r.lower() == fold), None)
        if conflict is not None:
            print(
                f"[중단] repo {name!r} 은 이미 등록된 {conflict!r} 과 대소문자만 다르다 "
                f"(repo명·prefix 동일성은 case-insensitive). 등록된 case {conflict!r} 를 "
                "그대로 쓰라 (clone/등록/훅 전혀 하지 않았다·부작용 0).",
                file=sys.stderr,
            )
            return 1

    # owner = areas.md 등록 식별자(registrant) — **사람** 차원이다. 슬롯 세션에서 유도하지
    # 않는다: 등록은 어느 슬롯에서 쳤는지와 무관하고, 작업 폴더가 아직 없는 홈(첫 등록)에서는
    # 세션이 아예 없다. 값의 단일 진실은 `identity.user`(없으면 git user.email)이고 그 선언을
    # 공용 `lease_store.user_name(home)`으로 읽는다. `--owner` 명시는 종전대로 이긴다.
    # 미해소면 fail-loud — **어떤 부작용(bare clone·areas_append·훅 설치)보다 앞에서** 차단한다
    # (그대로 넘기면 registry가 owner를 문자열 "None"으로 areas.md에 기록한다).
    owner = args.owner or lease_store.user_name(home)
    if not owner:
        print(
            "[중단] 등록 owner 미해소 — `identity.user` 도 git user.email 도 없다. "
            "`--owner <id>` 로 등록 식별자를 명시하거나 `local.conf` 에 `identity.user=` 를 "
            "적어라 (clone/등록/훅 전혀 하지 않았다).",
            file=sys.stderr,
        )
        return 1
    # area_owner = 그 area 의 *user* 소유(`--mine` 풀 입력) — registrant
    # `owner`(등록한 사람)와 별개 칼럼(overload 금지·codex sug). `--user` 명시 > local.conf identity.user=
    # > git config user.email > None(빈 칼럼·소비자가 None 으로 읽음·현행 동작).
    area_owner = getattr(args, "user", None) or lease_store.user_name(home)
    base_arg = getattr(args, "base", None)
    bare_path = repos_dir / f"{name}.git"
    # 멱등 재등록 판별은 **repo명**으로 한다(prefix 로 세지 않는다) — 자동시드 폐지
    # 후 prefix 칼럼이 비므로 `registered_prefixes()` 는 이 repo 를 못 센다. `registered_repos()`
    # 는 repo 칼럼을 직접 세어 중복 append(같은 repo 두 줄)를 막는다. 위 case-only 가드에서 이미
    # 조회한 `registered_names` 를 재사용한다(같은 areas 스냅샷·중복 조회 dedupe).
    already_registered = name in registered_names
    runner = clone_runner or git._real_clone_runner()

    # bare 는 *경로 존재*(exists)만이 아닌 *실 bare git repo* 인지로 판정한다 — 중단된
    # `git clone --bare`(하네스 타임아웃·Ctrl-C)가 남긴 부분/빈/깨진 `.repos/<name>.git` 도
    # exists()=True 라, 경로 존재만 보면 "존재 → 재사용"으로 조용히 통과시켜 무효 bare 를 재사용한다
    # (repo add 는 rc0 인데 invariant 깨진 채·나중 worktree add 가 날 git 에러로 죽음·audit #1→#4).
    # `_is_valid_bare`(rev-parse --is-bare-repository·worktree_pool)로 실 bare 를 판정해 그 통과를
    # fail-loud 로 닫는다. **파괴적 재clone 신중(§결정)**: 깨진 bare 자동삭제는 위험(사용자 데이터
    # 오판)이라 하지 않고, 정확 진단 + 수동 삭제 위임으로 안내한다(삭제는 사용자 위임 원칙). fail-soft:

    bare_present = bare_path.exists()
    if bare_present and (not worktree_git._is_valid_bare(bare_path, runner=runner)):
        print(
            f"[중단] `.repos/{name}.git` 경로는 있으나 유효한 bare git repo 가 아니다 — 부분/깨진 "
            f"bare (중단된 `git clone --bare` 잔존 가능성·하네스 타임아웃/Ctrl-C). 경로 존재만 보면 "
            f"재사용으로 조용히 통과하지만 `git worktree add` 의 base 로 못 써 나중 날 git 에러로 죽는다. "
            f"자동 삭제는 하지 않는다(사용자 데이터 오판 위험·삭제는 사용자 위임) — `.repos/{name}.git` 를 "
            f"수동 삭제 후 `pm-config repo add {name}`(--git 불요·areas 등록 URL 로 재hydrate·미등록이면 "
            f"`--git <url>` 재제공)로 재생성하라 (부작용 0·clone/등록/훅 전혀 하지 않았다).",
            file=sys.stderr,
        )
        return 1
    # 여기 도달 = bare 부재이거나 유효 bare (무효는 위에서 return 1) — 유효 bare 존재면 clone 건너뛰고
    # 재사용, 부재면 clone (재)시도. 종전 `bare_exists` 의미(존재 → 재사용)를 실 bare 로 좁힌 것.
    bare_exists = bare_present

    # clone 소스 git URL 해소 — `--git` 미제공 시 areas.md `git` 칼럼에서 해소해
    # multi-user 2번째 사용자의 bare mirror hydrate 를 완성한다(재제공 없이). None 이면
    # fail-loud(미등록+`--git` 없음 등) — 어떤 부작용(clone·mkdir·등록)보다 앞에서 걸러
    # 부작용 0 을 보장한다. 해소된 URL 을 이하 clone/areas_append 가 일관되게 쓴다.
    # URL 은 **실제로 필요할 때만** 해소한다 — clone(bare 부재) 또는 신규 등록(미등록)일 때. 이미
    # 등록 + bare 존재면 순수 no-op(refspec/tracking/보호훅 자가치유·아래 이미 등록 분기)라 URL 이
    # 불필요하니, areas `git` 칼럼이 빈 레거시/부분 등록이어도 fail-loud 하지 않는다(codex 지적·
    # `repo add` 재실행=훅 자가치유 경로 보존·`--git` optional 계약 완결).
    git_url = None
    if (not bare_exists) or (not already_registered):
        git_url = _resolve_clone_git_url(home, name, args.git, already_registered)
        if git_url is None:
            return 1

    # 두 부작용(bare clone + areas 등록)을 멱등화한다 — base 해소가 bare 에 의존
    # 하므로(bare HEAD/존재 검증) **clone 을 먼저**, 그 다음 base 해소·areas 등록 순서다.

    # 1) bare clone → .repos/<name>.git (worktree 풀 공유 .git 원). bare 가 이미
    #    있으면 건너뛴다(재사용·중복 clone/클로버 방지). bare 부재면 clone(재)시도.
    if bare_exists:
        print(f"✓ .repos/{name}.git 이미 존재 — clone 건너뜀 (재사용).")
    else:
        # 원본 조회 — 원본 HEAD 가 해소되지 않으면 clone 이 HEAD 없는 bare 를 만들고 엔진 명령으로는
        # 되돌릴 수 없다. `ls-remote --exit-code` 는 HEAD 가 없으면 rc 2 다(커밋 0개, 또는 원본 HEAD
        # 가 없는 브랜치를 가리킴). 그 밖의 실패(없는 경로·네트워크·권한)도 clone 전에 멈춘다 — 두
        # 경우 모두 mkdir·clone·등록·훅 0.
        rc, out = runner(["ls-remote", "--exit-code", git_url, "HEAD"])
        if rc == _LS_REMOTE_NO_MATCHING_REF:
            print(
                f"[중단] 원본 {git_url} 에 커밋이 없다(또는 원본 HEAD 가 없는 브랜치를 가리킨다). 빈 "
                "bare 를 만들지 않는다. 원본 저장소에서 먼저 커밋하거나 원본 HEAD 를 있는 브랜치로 "
                "바꾼 뒤 같은 명령을 다시 실행하라 (clone/등록/훅 전혀 하지 않았다).",
                file=sys.stderr,
            )
            return 1
        if rc != 0:
            print(
                f"[중단] 원본 {git_url} 을 조회하지 못했다 (git ls-remote rc={rc}): {out.strip()}. "
                "주소·네트워크·권한을 확인한 뒤 다시 실행하라 (clone/등록/훅 전혀 하지 않았다).",
                file=sys.stderr,
            )
            return 1
        repos_dir.mkdir(parents=True, exist_ok=True)
        rc, out = runner(["clone", "--bare", git_url, str(bare_path)])
        if rc != 0:
            print(
                f"[경고] `git clone --bare {git_url}` 실패 (rc={rc}):\n{out}\n"
                f"  네트워크/URL 확인 후 수동으로 `git clone --bare {git_url} {bare_path}` 하거나 "
                "재시도하라(등록은 clone 성공 후·멱등).",
                file=sys.stderr,
            )
            return 1
        print(f"✓ .repos/{name}.git bare clone 완료.")

    # 1b) bare fetch refspec 보정 — clone 성공/기존 bare 재사용 *둘 다* 에서 수행한다.
    #     `git clone --bare` 가 remote.origin.fetch 를 설정하지 않아 origin/* remote-tracking
    #     ref(origin/main)가 안 생기는 결함을 근절한다(멱등 — refspec-없는 과거 bare 도 보정).
    #     clone 실패 시엔 위에서 이미 return 1 했으므로 여기 도달 = bare 존재 전제(fail-soft).
    _set_bare_fetch_refspec(bare_path, runner=runner)

    # 1c) bare 기본 브랜치 origin tracking 보정. refspec 이 origin/*
    #     remote-tracking ref 를 채웠고, 이 헬퍼가 로컬 branch.<d>.remote/merge tracking config 를
    #     박아 그 bare 슬롯 기본 브랜치의 @{upstream}(origin/<default>) 해소를 닫는다. refspec 보정과
    #     같은 위치(clone 성공/기존 bare 재사용 둘 다·already_registered early-return 이전)라 과거
    #     tracking-없는 bare 도 다음 repo add 에 자가치유된다(멱등·fail-soft).
    _ensure_bare_branch_tracking(bare_path, runner=runner)

    # 1d) 사본 루트 ignore 규칙 설치 — 이 풀의 모든 슬롯이 공유하는 bare `info/exclude` 에
    #     위임 run-dir 규칙을 멱등으로 박는다. refspec/tracking 보정과 같은 자리
    #     (already_registered early-return 이전)라 기존 풀도 다음 `repo add` 에 규칙을 얻는다.
    _ensure_bare_ticket_copy_exclude(bare_path)

    # 2) 이미 등록돼 있으면 base 재해소/등록을 건너뛴다(append-only·중복 등록 금지). base 는
    #    첫 등록 때 박힌 값 그대로(clone 만 실패했던 재시도 경로는 위 clone 으로 복구됨).
    #    보호 훅은 멱등 자가치유라 *재등록 경로에서도* (재)설치한다 — 엔진 update 후
    #    기존 repo 도 다음 repo add/worktree add 에 훅을 얻는다(별도 명령 불요).
    if already_registered:
        print(f"✓ repo {name!r} 이미 areas.md 등록됨 — 등록 건너뜀.")
        # `--protected` 는 *등록 줄*에 실리는 값이라 등록을 건너뛰는 이 경로에선 반영되지 않는다.
        # 조용히 삼키면 사용자는 보호목록을 바꿨다고 믿는데 areas 는 그대로다(값-연결 끊김과 같은
        # 클래스) → loud 안내 + 정확한 대체 커맨드.
        if protected_arg is not None:
            print(
                f"[경고] `--protected {protected_arg!r}` 는 이미 등록된 repo 라 **반영되지 않았다** "
                "(등록 줄은 append-only·이 경로는 등록을 건너뛴다). 기존 행의 보호목록을 바꾸려면: "
                f"`{repo_protection._FACADE_PROG} repo protected {name} \"{protected_cell}\"`.",
                file=sys.stderr,
            )
        repo_protection._install_protected_hook_reporting(home,
            name, action="(재)설치")
        return 0

    # 3) base 브랜치 해소 — bare 가 존재하는 지금 시점에 해소·검증한다.
    #    --base 지정 → 존재 검증(없으면 rc 1·areas 등록 막음). 미지정 → bare HEAD 명시값화.
    base = _resolve_base(base_arg, bare_path, runner=runner)
    if base is _BASE_INVALID:
        print(
            f"[중단] --base {base_arg!r} 가 `.repos/{name}.git` 에 없다 "
            f"(`git -C {bare_path} show-ref --verify --quiet refs/heads/{base_arg}` 실패). "
            "브랜치명을 확인하거나 `--base` 를 생략(기본 브랜치 사용)하라.",
            file=sys.stderr,
        )
        return 1

    # 4) areas.md 등록 — repo/prefix/git/test_cmd/owner/base/protected 칼럼.
    #    prefix(2번째 positional)는 **빈 값**으로 등록 — repo명 자동시드 폐지: repo명은
    #    작업 카테고리가 아니다.
    #    으로 튀게 했다. protected 는 `--protected` 인자— 미지정이면 빈 값이라
    #    `_repo_protected` 가 DEFAULT_PROTECTED(main/master/develop) 폴백하고, 사후 변경은
    #    `pm-config repo protected <name> <목록>`(areas → sidecar 정합화)로 한다.
    areas_registry.areas_append(
        home, "", "", owner, repo=name, git=git_url, test_cmd=args.test, base=base,
        protected=protected_cell, area_owner=area_owner,
    )
    # --test 미지정(None) 이면 areas test_cmd 빈 값 — 해소 체인이 슬롯/local.conf 로
    # 폴백한다. 빌드명령은 worktree add 프롬프트·콘솔 [b] 에서 채울 수 있다.
    test_surface = args.test if args.test else "(미지정 — worktree add/콘솔 [b] 에서 설정)"
    base_surface = base if base else "(미해소 — worktree add 가 bare HEAD 사용)"
    area_owner_surface = area_owner if area_owner else "(미상 — local.conf identity.user= / git user.email 미설정)"
    protected_surface = protected_cell if protected_cell else "(미지정 — main/master/develop 기본값)"
    print(
        f"✓ areas.md 등록: {name} | git={git_url} | test_cmd={test_surface} | "
        f"owner={owner} | base={base_surface} | protected={protected_surface} | "
        f"area_owner={area_owner_surface}"
    )
    # 5) 보호 브랜치 pre-push 훅 설치 (멱등 자가치유) — 보호목록(areas protected→
    #    default) sidecar + bare core.hooksPath wiring. 회사 repo 무영향(.project_manager/.local).
    #    설치 실패는 조용히 넘기지 않는다(공용 깔때기 — 훅 미설치 침묵 = 보호 가드 무력화).
    repo_protection._install_protected_hook_reporting(home,
        name, action="설치")
    # 6) 브랜치 실재 경고 1줄 — `repo protected` set 경로와 **같은 공용 깔때기**.
    #    거부 아님(미래 브랜치 선-보호가 정상)·오타(`mian`)가 기본 보호목록을 덮으며 조용히
    #    통과하는 것만 막는다. bare 는 방금 clone/재사용돼 존재하므로 조회 가능(불가면 생략).
    repo_protection._warn_missing_protected_branches(home,
        name, protected_tokens, clone_runner=runner, repos_dir=repos_dir)
    return 0


def _print_protected_status(
    home,
    name: str,
    *,
    clone_runner: worktree_records.GitRunner | None = None,
) -> int:
    """`repo protected <name>` 조회 — 실효값 + 출처 + sidecar 정합/drift 3줄.

    "빈 값이라 기본값 폴백 중" 과 "이 clone 의 훅은 아직 옛 값으로 동작" 두 사실이 안 보이면
    사용자가 계속 헷갈린다 — 그래서 **실효값**(areas_store가 해소한 목록)·**출처**(명시/기본값
    폴백/미등록)·**sidecar 정합**(훅이 실제로 읽는 파일과 같은가)을 함께 낸다. 조회는 순수
    읽기라 어떤 파일도 쓰지 않는다(rc 0).

    **sidecar 줄은 두 축을 본다(should-fix)**:  내용 정합(sidecar == 실효값)  **배선**
    (`core.hooksPath` 가 우리 훅 디렉토리를 가리키나·`protected_hook_wired`).
    "sidecar 는 최신인데 훅이 아예 안 걸린" 부분성공(`install_protected_hook` 3단계 실패)이
    `✓ 정합` 으로 보인다 — 보호가 꺼져 있는데 정합하다고 거짓 보고하는 것이다.
    """
    effective = areas_store._repo_protected(home, name)
    print(f"{name} · protected = {', '.join(effective)}")

    cell = repo_protection._areas_protected_cell(home, name)
    if cell is None:
        print("  출처: 미등록 repo (areas.md 에 그 행이 없음) — DEFAULT_PROTECTED 기본값 폴백")
    elif cell.strip():
        print("  출처: 명시 (areas.md `protected` 칼럼)")
    else:
        print("  출처: 기본값 폴백 (areas.md `protected` 칼럼 비어 있음 — DEFAULT_PROTECTED)")

    path = protected_hooks._protected_sidecar_path(home, name)
    sidecar = protected_hooks._read_protected_sidecar(home, name)
    if sidecar is None:
        print(f"  훅 sidecar: {path} → (미설치) "
              f"⚠ 이 clone 엔 보호 훅이 아직 없다")
        print(f"    → 설치:  {repo_protection._FACADE_PROG} repo add {name}  (멱등·bare 존재 전제)")
        return 0
    wired = protected_hooks.protected_hook_wired(home,
        name, git_runner=clone_runner)
    listed = ", ".join(sidecar) or "(빈 파일)"
    # 재실행 안내는 **현재 상태**(명시/폴백/미등록)에 맞춰야 한다 — 폴백 상태에 명시 커맨드를
    # 안내하면 사용자가 그대로 실행했을 때 출처가 조용히 "명시" 로 바뀐다(단일 분기 헬퍼).
    retry = repo_protection.protected_retry_command(home, name)
    if sidecar != effective:
        # 내용 drift 가 지배적 — 배선 여부와 무관하게 재설치가 답이다(재설치가 둘 다 고친다).
        print(f"  훅 sidecar: {path} → {listed}  "
              f"⚠ 옛 목록({', '.join(sidecar) or '없음'}) — 이 clone 의 훅은 아직 옛 값으로 동작")
        print(f"    → 정합화:  {retry}"
              f"   (또는 `{lease_store._runtime_skill_entry('pm-bootstrap')}` 이 세션 시작에 자동 정합화)")
        return 0
    if wired is False:
        # 내용은 최신인데 배선이 끊긴 부분성공 — sidecar 만 보면 `✓ 정합` 으로 보이는 그 상태.
        print(f"  훅 sidecar: {path} → {listed}  "
              "⚠ 훅 미배선 — 목록은 최신이나 bare `core.hooksPath` 가 이 디렉토리를 가리키지 "
              "않아 pre-push·pre-commit 훅이 아예 발화하지 않는다(보호 꺼짐)")
        print(f"    → 재배선:  {retry}   (멱등·hooksPath 재설정)")
        return 0
    if wired is None:
        print(f"  훅 sidecar: {path} → {listed}  "
              "✓ 목록 정합 (배선 확인 불가 — bare 부재/git 미해소)")
        return 0
    print(f"  훅 sidecar: {path} → {listed}  ✓ 정합 (목록·hooksPath 배선)")
    return 0


def cmd_repo_protected(
    home,
    args: argparse.Namespace,
    *,
    clone_runner: worktree_records.GitRunner | None = None,
    repos_dir: Path | None = None,
) -> int:
    """`repo protected <name> [<목록>|default]` — 보호 브랜치 목록 조회/설정.

    값 인자 유무로 get/set 이 갈린다(`upstream show|set`·`task prefix <name> <p|none>` family
    동형). `default`(대소문자 무관)는 **칼럼 비움** = `DEFAULT_PROTECTED`
    (main/master/develop) 폴백이지 "보호 해제" 가 아니다 — 빈 문자열 지정은 fail-loud 한다.

    설정 순서(**areas → sidecar 고정**·역순 금지):
      1. 형식 검증(토큰만·브랜치 실재는 검증 안 함) — 실패면 부작용 0 rc1.
      2. `areas_registry.areas_set_cell(name, "protected", …)` — `board_lock()` 하 비파괴 in-place 재기록.
         중복 repo 행이면 fail-loud(부작용 0·수동 정리 안내), 미등록이면 `repo add` 안내.
      3. 훅 sidecar 재설치(`_install_protected_hook` 재사용·멱등). 실패는 loud 경고 + 재실행
         안내 — areas 는 이미 비준됐으므로 rc 0(다음 `repo add`/`/pm-bootstrap` 이 자가치유).
      4. board-git best-effort 동기(공유 정책 변경은 즉시 공유돼야 값-연결이 산다).
      5. bare 에 없는 브랜치가 있으면 경고 1줄(거부 아님 — 미래 브랜치 선-보호가 정상).

    clone_runner / repos_dir 주입으로 hermetic 테스트.
    """

    name = args.name
    if not areas_rules._validate_repo_name(name):
        print(
            f"[중단] repo 이름 {name!r} 형식 위반 — 허용: 영숫자로 시작, 이후 영숫자/`_`/`-` "
            "(정규식 `^[A-Za-z0-9][A-Za-z0-9_-]*$`). areas.md 는 전혀 건드리지 않았다.",
            file=sys.stderr,
        )
        return 1

    value = getattr(args, "value", None)
    if value is None:
        return _print_protected_status(home,
            name,
            clone_runner=clone_runner)

    # ── set ──────────────────────────────────────────────────────────────
    if value.strip().lower() == areas_rules._PROTECTED_DEFAULT_LITERAL:
        tokens: list[str] = []
        new_cell = ""            # 칼럼 비움 → `_repo_protected` 가 DEFAULT_PROTECTED 폴백.
    else:
        parsed, reason = areas_rules._validate_protected_tokens(value)
        if parsed is None:
            print(f"[중단] 보호목록 거부 (areas.md 미변경) — {reason}", file=sys.stderr)
            return 1
        tokens = parsed
        new_cell = ",".join(tokens)

    # 2) areas.md 셀 변경 — 단일 진실 먼저(역순이면 훅이 비준되지 않은 목록을 강제한다).
    try:
        old_cell, _new = areas_registry.areas_set_cell(home, name, "protected", new_cell)
    except (areas_rules.AreasDuplicateRepo, areas_rules.AreasRepoNotFound, OSError, UnicodeError) as exc:
        # 종류별 안내로 번역(부작용 0·rc 1).
        if isinstance(exc, areas_rules.AreasDuplicateRepo):
            print(
                f"[중단] areas.md 에 repo {name!r} 행이 {getattr(exc, 'count', 2)}개 있다(중복) — "
                "어느 행을 고쳐야 하는지 기계가 정할 수 없다(리졸버는 first-match). 추측해서 한쪽만 "
                "고치지 않는다(areas.md 미변경·부작용 0). 한 행만 남기고 수동 정리한 뒤 다시 실행하라 "
                "(`board.py lint` 의 `areas-duplicate-repo` 권고가 같은 상태를 상시 표면화한다).",
                file=sys.stderr,
            )
            return 1
        if isinstance(exc, areas_rules.AreasRepoNotFound):
            print(
                f"[중단] repo {name!r} 이(가) areas.md 에 등록돼 있지 않다 — 보호목록 설정은 *기존* "
                "행의 셀을 고칠 뿐 등록을 만들지 않는다. 먼저 등록하라: "
                f"`{repo_protection._FACADE_PROG} repo add {name} --git <url> --protected \"{value}\"`.",
                file=sys.stderr,
            )
            return 1
        print(f"[중단] areas.md 기록 실패 (미변경) — {exc}", file=sys.stderr)
        return 1

    old_surface = old_cell.strip() or "(빈 값 — 기본값 폴백)"
    new_surface = new_cell or "(빈 값 — 기본값 폴백 main/master/develop)"
    print(f"✓ areas.md `protected` 갱신: {name}  {old_surface} → {new_surface}")

    # 3) 파생 sidecar 정합화 — 훅이 *실제로 읽는* 파일. 멱등 재설치(`repo add`·`worktree add` 공용).
    # areas 는 이미 비준됐으므로 sidecar 실패는 rc 를 바꾸지 않는다(다음 repo add/`/pm-bootstrap`
    # 이 자가치유) — 대신 공용 깔때기가 실패를 loud 하게 낸다(침묵 금지).
    # 재실행 안내는 방금 비준한 상태 기준 — `default` 로 되돌린 경우엔 `default` 를 안내해야
    # 정확하다(`tokens` 가 빈 리스트라 falsy → 옛 코드는 `repo add` 로 잘못 떨어졌다).
    repo_protection._install_protected_hook_reporting(home,
        name, action="정합화",
        retry=repo_protection.protected_retry_command(home, name))

    # 4) board-git best-effort 동기 — 공유 정책 변경은 즉시 공유돼야 값-연결이 산다.
    #    **경로 스코프 필수**: paths 없이 부르면 board 전체가 "repo protected"
    #    커밋에 쓸려 들어가 *남의 미커밋 편집*까지 대신 커밋한다. claim 이 dirty 를 안 막게 된
    #    지금은 board 가 상시 dirty 라 그 노출이 더 크다. areas.md 만 이 mutation 의 산출물이다.
    board_sync._board_git_sync_best_effort(home, "repo protected", (pm_paths.areas_file(home),))

    # 5) 브랜치 실재 경고 1줄 — `repo add --protected` 와 **같은 공용 깔때기**(검증 비대칭 방지).
    #    거부 아님(미래 브랜치 선-보호가 정상·오타 가시화용).
    repo_protection._warn_missing_protected_branches(home,
        name, tokens, clone_runner=clone_runner, repos_dir=repos_dir)
    return 0


# `repo list` 표 칼럼 — areas.md 레지스트리에서 PM 이 조정하는 값 + 그 repo 가 어느 원격에
# 붙어 있는지(`git`). owner 는 등록 시점 메타라 제외. 헤더 라벨 = areas.md 칼럼명 그대로.
_REPO_LIST_COLUMNS = ("repo", "prefix", "git", "base", "protected", "test_cmd", "area_owner")


def cmd_repo_list(home, args: argparse.Namespace) -> int:
    """`repo list` — 등록 repo 표(repo·prefix·base·protected·test_cmd·area_owner).

    areas.md 레지스트리의 조회 표면이다 — 보호목록을 고치려면 *지금 뭐가 등록돼 있는지* 부터
    보여야 한다. 빈 셀은 그 칼럼의 폴백을 괄호로 밝힌다(특히 `protected` 빈 값 = 기본값 폴백 —
    "빈 칸" 을 "보호 없음" 으로 오독하지 않게). 순수 읽기(rc 0·미등록이어도 안내 후 0).
    """
    try:
        _header, rows = areas_store._parse_areas(home)
    except (OSError, UnicodeError, ValueError) as exc:  # 파싱 실패는 명시 에러(조용한 빈 표 금지).
        print(f"[중단] areas.md 파싱 실패 — {exc}", file=sys.stderr)
        return 1
    rows = [row for row in rows if row.get("repo")]
    if not rows:
        print(f"등록된 repo 없음 — `{repo_protection._FACADE_PROG} repo add <name> --git <url>` 로 등록하라.")
        return 0
    default_protected = ",".join(areas_rules.DEFAULT_PROTECTED)
    table: list[list[str]] = [list(_REPO_LIST_COLUMNS)]
    for row in sorted(rows, key=lambda r: r.get("repo", "")):
        cells = []
        for column in _REPO_LIST_COLUMNS:
            val = (row.get(column) or "").strip()
            if val:
                cells.append(val)
            elif column == "protected":
                cells.append(f"({default_protected} · 기본값)")
            else:
                cells.append("-")
        table.append(cells)
    # 폭은 **표시 폭** 기준(전각 2칸) — `len()` 이면 한글 셀(예 test_cmd·area_owner)에서 정렬이
    # 깨진다. 구분선도 같은 기준이라 헤더와 자리가 맞는다.
    widths = [max(console._display_width(r[i]) for r in table)
              for i in range(len(_REPO_LIST_COLUMNS))]
    for i, cells in enumerate(table):
        print("  ".join(console._pad_display(c, w) for c, w in zip(cells, widths)).rstrip())
        if i == 0:
            print("  ".join("-" * w for w in widths))
    print(f"\n보호목록 조회/변경: `{repo_protection._FACADE_PROG} repo protected <repo> [<목록>|default]`")
    return 0
