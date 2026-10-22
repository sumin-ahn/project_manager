"""PM 홈 초기화 명령 (층: cli)."""
from __future__ import annotations

from pm_engine.app import areas_registry
from pm_engine.app import board_sync
from pm_engine.app import conf_lint
from pm_engine.cli import task_args
from pm_engine.domain import areas_rules
from pm_engine.domain import ticket_rules
from pm_engine.infra import areas_store
from pm_engine.infra import conf_store
from pm_engine.infra import fs
from pm_engine.infra import git
from pm_engine.infra import lease_store
from pm_engine.infra import pm_paths
from pm_engine.infra import pre_push_hook
import argparse
import datetime
import re
import subprocess
import sys


def _configure_board_submodule(home, ) -> bool:
    """board submodule 의 `ignore = all` 을 자동 설정 (누출 0). 멱등·fail-soft.

    board 가 submodule 로 분리(`.project_manager/board/.git` 존재)된 형상에서만 동작한다 —
    superproject(design·코드 git)에서 `submodule.<path>.ignore all` 을 켜면, board(submodule)가
    PM 운영 commit 으로 전진해도 design 의 `git status`/`git diff` 가 그 gitlink drift 를 숨겨
    routine `git add -A` 가 board 포인터 bump 를 *우발 stage* 하지 않는다(board↔design 누출 0).

    fail-soft: git 바이너리 부재·git repo 아님·submodule 미분리(`.../board/.git` 없음·
    legacy)면 아무 것도 하지 않고 False 반환(미마이그 adopter 100% 무영향). 멱등:
    `git config` 는 같은 키를 덮어쓰므로 재실행 안전. 반환 True = 설정 적용.

    config 키 = `submodule.<.gitmodules-path>.ignore`. board
    의 `.gitmodules` 서브섹션 *이름*을 권위로 읽어(표준 `git submodule add` 는 name==path) 키를
    구성한다 — 이름이 path 와 달라도 정확한 키로 set.
    """
    board_repo = (home.root) / ".project_manager" / "board" / ".git"
    if not board_repo.exists():
        return False  # submodule 미분리(legacy) — no-op
    # `.gitmodules` 에서 이 board path 에 대응하는 submodule 서브섹션 *이름*을 찾는다.
    # 출력 예: `submodule.<name>.path .project_manager/board` — 표준은 name == path.
    name = _board_submodule_name(home)
    if name is None:
        return False  # .gitmodules 부재/미등록·git 부재 — fail-soft
    r = subprocess.run(
        ["git", "-C", str((home.root)), "config", f"submodule.{name}.ignore", "all"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode == 0


def _board_submodule_name(home, ) -> str | None:
    """`.gitmodules` 에서 `.project_manager/board` path 의 submodule 서브섹션 이름 (없으면 None).

    `git config -f .gitmodules --get-regexp '^submodule\\..*\\.path$'` 행을 파싱해 값이
    `.project_manager/board` 인 항목의 키 `submodule.<name>.path` 에서 `<name>` 을 추출한다.
    git 부재·.gitmodules 부재·매칭 없음 → None (fail-soft·_configure_board_submodule 가 no-op).
    """
    gitmodules = (home.root) / ".gitmodules"
    if not gitmodules.exists():
        return None
    r = subprocess.run(
        ["git", "-C", str((home.root)), "config", "-f", str(gitmodules),
         "--get-regexp", r"^submodule\..*\.path$"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        return None
    want = ".project_manager/board"
    for line in r.stdout.splitlines():
        key, _, value = line.partition(" ")
        if value.strip() != want:
            continue
        # key = `submodule.<name>.path` → 가운데 <name>(점 포함 가능) 추출.
        if key.startswith("submodule.") and key.endswith(".path"):
            return key[len("submodule."):-len(".path")]
    return None


INIT_GUIDE = """\
─ init 완료 — 이 clone setup 끝 ({mode}) ─
  3계층: 엔진(upstream) / 공유상태(main: board·status·log·ADR) / per-clone 로컬(pm_state·local.conf · git-ignored)
  규칙: 내구 진실은 공유 채널에만 · pm_state 는 버려도 되는 로컬 · 공유 파일 직접 난편집 금지
  ID:   {id_line}
"""


_INIT_REPO_NAME_CANONICAL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def _init_repo_name(home, args: argparse.Namespace) -> str | None:
    """init 이 areas.md 에 등록할 **repo 이름** — `--repo` 명시 > clone 루트 폴더명.

    repo 칼럼은 *이 clone 의 정체성*이고 prefix 칼럼은 *작업 카테고리*다 — 두 축은 섞이지
    않는다. prefix 를 repo 이름으로 승격하면 (a) 무prefix init 이 이미 만든 이 clone 의 행을
    못 찾아 사용자가 승인까지 지불한 `--prefix` 가 조용히 버려지고 (b) 이름이 다르면 한 물리
    repo 가 areas 2행이 되며 (c) 명시 `--repo` 가 조용히 무시된다.

    표를 깨는 문자(`|`·개행)나 빈 이름이면 **아무것도 쓰지 않고** None 을 돌려준다(호출부가 rc 1)
    — 그 경우의 처방은 `--repo <repo>` 명시다. canonical 형식(영숫자 시작·이후 영숫자/`_`/`-`)을
    벗어난 폴더명은 등록은 하되 1줄로 알린다(폴더 이름 `<repo>_<N>` 가독성·`repo add` 와의 정합).
    """
    name = (getattr(args, "repo", None) or (home.root).name or "").strip()
    if not name or any(ch in name for ch in areas_rules._AREAS_CELL_BREAKERS):
        print(f"[중단] areas.md 에 등록할 repo 이름을 유도할 수 없다 (후보 {name!r}) — "
              "`--repo <repo>` 으로 명시하라 (등록·conf 어떤 것도 쓰지 않았다).",
              file=sys.stderr)
        return None
    if not _INIT_REPO_NAME_CANONICAL.match(name):
        print(f"  ⚠ 등록 repo 이름 {name!r} 이 canonical 형식(`[A-Za-z0-9][A-Za-z0-9_-]*`)을 "
              "벗어난다 — 그대로 등록하지만, 폴더 이름(`<repo>_<N>`)과 `pm-config repo` 조작이 "
              "읽기 어려워진다. `board.py init --repo <repo>` 으로 등록명을 지정할 수 있다.")
    return name


def cmd_init(home, args: argparse.Namespace) -> int:
    """clone 당 1회 setup — areas.md repo 행 등록 + local.conf + pm_state + pre-push 게이트 훅.

    multi-PM = N 세션 × M repo 한 개념 — *수가 1이냐 더냐*의 문제이고 N=1·M=1 도 그 부분집합이다.
    그래서 등록 경로가 갈리지 않는다: **`--prefix` 유무와 무관하게** 이 clone 의 repo 행을
    areas.md 에 항상 등록한다(등록 0 형상 없음 → 해소 체인이 레지스트리에서 끝난다).

    `--prefix` 는 협업(다중-사람)용이 아니라 **작업 카테고리** ID 네임스페이스다. 주면 그
    카테고리로 `T-<PREFIX>-NNN` 을, 안 주면 prefix 칼럼이 빈 채(=`none` 카테고리) `T-NNNN` 을
    발행한다 — 둘 다 1급 형상이라 `--prefix` 는 필수가 아니다(비대화형 온보딩이 인자 0으로 돈다).

    **등록 축은 둘이 분리돼 있다** — repo 칼럼 = 이 clone 의 정체성(`--repo` 명시 > 루트 폴더명),
    prefix 칼럼 = 작업 카테고리(`--prefix`). 멱등 키는 **이 clone 의 repo 행**이다:
      - 행 없음            → 행 1개 append(prefix 칼럼은 `--prefix` 또는 빈 칸).
      - 행 있음·`--prefix` 무 → no-op(행 추가 없음).
      - 행 있음·같은 카테고리 → no-op.
      - 행 있음·카테고리 비었음 → 그 행의 **prefix 셀만 갱신**(행 추가 아님·스키마 불변).
      - 행 있음·다른 카테고리 → 부작용 0 fail-loud(카테고리 변경은 `board.py prefix rename` 소관).
    `--area <설명>` 은 새 행을 만들며 카테고리를 함께 등록할 때만 필요하다.
    """
    requested_prefix = args.prefix
    prefix = requested_prefix
    prefix_precheck: str | None = None
    # 명시 --prefix sanity (예약어 `none`·형식 [a-z0-9_]+) — 위반이면 areas 등록·
    # local.conf write 어떤 부작용보다 앞에서 부작용 0 으로 거부(cmd_init 첫 문장 뒤).
    if prefix:
        reason = ticket_rules._validate_prefix(prefix)
        if reason:
            print(f"[중단] {reason}", file=sys.stderr)
            return 1
        canonical = areas_registry.require_prefix_user_ack(
            home, prefix,
            getattr(args, "user_ack", None),
            surface="board init --prefix",
        )
        if canonical is None:
            return 1
        prefix = canonical
        prefix_precheck = canonical
    # owner 기본값은 `--task` 다(환경변수·단일 대여에서 유도하지 않는다). `--repo` 는 등록명이다.
    owner_task = task_args._actor_session_override(home, args)
    repo_name = _init_repo_name(home, args)
    if repo_name is None:
        return 1
    area_message: str | None = None
    wrote_areas = False
    # areas 등록은 board_lock 안에서 fresh 3소스 판정을 선판정과 대조한 뒤 수행한다. 캐시된
    # 선판정 뒤 다른 세션이 다른 case를 만들었으면 areas/local.conf 모두 쓰기 전에 중단한다
    # (TOCTOU·부분 init 0).
    with fs.board_lock(home):
        if prefix:
            fresh = areas_registry.revalidate_prefix_user_ack(
                home, requested_prefix,
                getattr(args, "user_ack", None),
                prefix_precheck,
                surface="board init --prefix",
            )
            if fresh is None:
                return 1
            prefix = fresh
        # 멱등 키 = **이 clone 의 repo 행**. prefix 존재만으로 등록을 생략하면 사용자가 승인까지
        # 지불한 카테고리가 조용히 버려지고, prefix 를 repo 이름으로 쓰면 한 물리 repo 가 2행이 된다.
        _header, _rows = areas_store._parse_areas(home)
        own_rows = [row for row in _rows if row.get("repo") == repo_name]
        if len(own_rows) > 1:
            print(f"[중단] areas.md 에 repo {repo_name!r} 행이 {len(own_rows)}개다(중복) — 어느 행이 "
                  "이 clone 인지 기계가 정할 수 없다. 한 행만 남기고 수동 정리한 뒤 다시 실행하라 "
                  f"({pm_paths._rel_to_repo(home, pm_paths.areas_file(home))}·`board.py lint` 의 areas-duplicate-repo 참조). "
                  "등록·conf 어떤 것도 쓰지 않았다.", file=sys.stderr)
            return 1
        # 다른 repo 행이 이미 그 카테고리를 쓰고 있으면 ID 네임스페이스가 겹친다 — 두 repo 가
        # 같은 `T-<PFX>-NNN` 시리즈를 나눠 쓰면 순번이 충돌한다(부작용 0 fail-loud).
        if prefix:
            taken = [row for row in _rows
                     if (row.get("prefix") or "").lower() == prefix.lower()
                     and row.get("repo") != repo_name]
            if taken:
                print(f"[중단] 카테고리 {prefix!r} 는 이미 repo {taken[0].get('repo')!r} 행에 "
                      "등록돼 있다 — 두 repo 가 같은 ID 네임스페이스를 나눠 쓰면 순번이 충돌한다. "
                      "다른 카테고리를 쓰거나 `board.py prefix rename` 으로 먼저 정리하라 "
                      "(등록·conf 어떤 것도 쓰지 않았다).", file=sys.stderr)
                return 1
        own_row = own_rows[0] if own_rows else None
        registered_cell = (own_row.get("prefix") or "").strip() if own_row else ""
        if own_row is None:
            if prefix and not args.area:
                print(f"새 prefix {prefix!r} 등록엔 --area <설명> 필요.", file=sys.stderr)
                return 1
            # owner = areas.md 등록 식별자(registrant) — 협업 소유자가 아니라 등록 출처 표식.
            # `--owner` > `--task` > 빈 칼럼(미상) 순이고 환경변수·단일 대여에서 유도하지 않는다.
            owner = args.owner or owner_task or ""
            area_owner = lease_store.user_name(home, getattr(args, "user", None))
            # 자기 행(폴더 이름 등록)이면 git·base 를 함께 싣는다 — 안내 명령(`pm-config worktree
            # add <repo>`)이 base 와 clone 원 주소를 이 행에서 읽는다. `--repo` 로 폴더와 다른 이름을
            # 등록하면 싣지 않는다(PM 홈 주소가 제품 행에 적히는 일 방지).
            self_row = repo_name == (home.root).name
            coordinates = areas_registry.self_repo_coordinates(home) if self_row else None
            git, base = coordinates or ("", "")
            areas_store._areas_append_locked(home, prefix or "", owner, repo=repo_name, area_owner=area_owner,
                                 git=git, base=base)
            wrote_areas = True
            ao_surface = (area_owner if area_owner else
                          "(미상 — local.conf identity.user= / git user.email 미설정)")
            area_message = (f"✓ areas.md 등록: repo={repo_name} | "
                            f"prefix={prefix or '(빈 칼럼 · none 카테고리)'} | "
                            f"owner={owner or '(미상 — --owner·--task 미지정)'} | "
                            f"area_owner={ao_surface} | git={git or '(없음)'} | base={base or '(없음)'}")
            if self_row and coordinates is None:
                print(areas_rules._SELF_COORDINATES_UNKNOWN, file=sys.stderr)
        elif not prefix:
            area_message = (f"repo {repo_name!r} 이미 등록됨 (areas.md) — 행 추가 없음 "
                            f"(카테고리 {registered_cell or '(빈 칼럼 · none)'}).")
        elif registered_cell.lower() == prefix.lower():
            area_message = (f"repo {repo_name!r} 이 이미 카테고리 {registered_cell!r} 로 "
                            "등록됨 (areas.md) — 변경 없음.")
        elif registered_cell:
            print(f"[중단] repo {repo_name!r} 행은 이미 카테고리 {registered_cell!r} 다 — "
                  f"{prefix!r} 로 바꾸려면 기발행 ID 까지 옮기는 `board.py prefix rename "
                  f"{registered_cell} {prefix}` 를 쓰라(init 은 카테고리를 갈아끼우지 않는다). "
                  "등록·conf 어떤 것도 쓰지 않았다.", file=sys.stderr)
            return 1
        else:
            # 이 clone 의 행은 있는데 카테고리 칼럼만 비었다(무prefix init 뒤 카테고리 지정) —
            # **행 추가가 아니라 셀 갱신**이다(스키마 불변·이미 board_lock 보유 → locked 변형).
            areas_store._areas_set_cell_locked(home, repo_name, "prefix", prefix)
            wrote_areas = True
            area_message = (f"✓ areas.md 카테고리 등록: repo={repo_name} | "
                            f"prefix={prefix} (기존 행의 prefix 칼럼 갱신·행 추가 없음)")
        conf_store._write_init_local_conf(home)
    areas_registry.invalidate_known_prefixes_cache()
    if wrote_areas:
        # 등록 행은 board git 의 공유 파일이다 — 파일만 쓰고 두면 board 가 dirty 로 남아
        # (`M areas.md`) 온보딩 다음 단계가 그 잔여를 사용자 편집으로 만난다. `pm-config repo
        # add`/`repo protected` 와 **같은 스코프 채널**로 이 mutation 이 만진 경로만 기록한다
        # (board 가 별도 git 이 아니면 no-op·best-effort 라 실패해도 init 을 되돌리지 않는다).
        # board_lock 밖에서 부른다(락 순서: board_git_lock → board_lock).
        board_sync._board_git_sync_best_effort(home, "init areas 등록", (pm_paths.areas_file(home),))
    print(area_message)
    print("✓ local.conf: py·test_cmd·ctx 비율 (세션·prefix 는 conf 키가 아니다 — "
          "세션=lease 장부 · prefix=areas.md)")
    # 병합 경로가 손대지 않은 기존 키(오타·폐기)도 같은 목록·같은 함수로 표면화한다 —
    # `board.py lint` 를 따로 실행하지 않아도 온보딩 시점에 1줄로 보이게(사본 없음).
    for _, _, detail in conf_lint.lint_local_conf_keys(home):
        print(f"  ⚠ {detail}")
    if not (pm_paths.wiki_dir(home) / "pm_state.md").exists() and (pm_paths.wiki_dir(home) / "pm_state.template.md").exists():
        # `{{DATE}}` 의 소유자는 **생성 시점**이다 — 템플릿은 채택자 디스크에 토큰-form
        #   으로 남아야 pm_update 의 manifest byte-copy 와 진동하지 않는다. 그러니 그 템플릿으로
        #   산출물을 만드는 이 지점이 오늘 날짜를 채운다(task_store.ensure_task_pm_state 의
        #   task pm_state 렌더와 같은 규칙·소유 선언은 pm_import.CONSUMPTION_TIME_TOKENS).
        seed = fs.read_text_shared((pm_paths.wiki_dir(home) / "pm_state.template.md"), encoding="utf-8").replace(
            "{{DATE}}", datetime.date.today().isoformat())
        (pm_paths.wiki_dir(home) / "pm_state.md").write_text(seed, encoding="utf-8", newline="\n")
        print(f"✓ pm_state.md 생성 ({pm_paths._rel_to_repo(home, (pm_paths.wiki_dir(home) / "pm_state.template.md"))} 에서)")
    # 미설치면 사유를 `install_pre_push_hook` 이 stderr 에 남긴다 — 여기서는 설치한 자리를 밝힌다.
    if pre_push_hook.install_pre_push_hook(home):
        print("✓ pre-push 게이트 훅 설치 — 자기 저장소의 .git/hooks "
              "(lint 게이트는 항상 · 회귀 게이트는 tests/ 있는 트리만)")
    # board submodule 이 분리된 형상이면 ignore=all 자동 설정 — design(코드) git 이
    # board PM-commit 으로 오염되지 않게(누출 0). 미분리/git 부재면 no-op(fail-soft·무영향).
    if _configure_board_submodule(home):
        print("✓ board submodule ignore=all 설정 (코드 git 누출 0)")
    # areas.md `merge=union` 배포는 여기서 하지 않는다 — init 의 board git 기록은
    # `_board_git_sync_best_effort(..., (areas_file(),))` 로 **areas.md 경로만** stage/commit 하는
    # scoped 채널이라 `.gitattributes` 는 그 범위 밖이다. 여기서 파일만 쓰면 board 가
    # dirty(`?? .gitattributes`)로 남아 다음 `claim` 이 STRICT dirty 가드에 막힌다(엔진이 만든
    # 파일을 사용자 편집으로 오인·clone→init→claim 온보딩 직격) — 미배포 근거는 그대로 유효하다.
    # 배포는 `_board_git_stage_and_commit`(write→stage→commit 이 한 호출에 닫힘) **단일 채널**로 한다.
    # 완료 안내의 ID 포맷은 **이 clone 에서 `board.py new` 가 실제로 발행할 값**이다 —
    # 등록 결과가 아니라 해소 체인(`cmd_new` 와 같은 `id_prefix`)에서 뽑는다.
    # 등록이 생략/거부된 형상에서 발행되지 않을 포맷을 성공 메시지로 내던 거짓 안내 차단.
    issued = areas_store.id_prefix(home, None)
    # `id_prefix(None)` 의 미해소(None)는 두 형상을 가린다 — (a) 등록 ≤1(count-based 로 곧
    # 무prefix `T-NNNN` 이 발행) (b) 등록 ≥2(모호). `cmd_new` 는 후자에서
    # 자신의 ≥2 가드로 fail-loud 하므로 무명시 `T-NNNN` 은 실제로 발행되지 않는다 — 완료 안내가
    # 그 포맷을 성공 메시지로 내면 거짓이다(같은 `registered_prefixes()` 로 `cmd_new` 와 lockstep).
    ambiguous = issued is None and len(areas_store.registered_prefixes(home)) >= 2
    if ambiguous:
        mode = f"repo {repo_name} · 카테고리 모호(등록 카테고리 ≥2)"
        id_line = ("무명시 `board.py new` 발행 불가 — 등록 카테고리가 여럿이다. "
                   "`board.py new --prefix <PFX> --user-ack <PFX>` 로 "
                   "명시하거나 task prefix 를 지정하라(`pm-config task prefix <task> <PFX>`).")
    else:
        mode = f"repo {repo_name} · 카테고리 {issued or 'none(무prefix)'}"
        idfmt = f"T-{issued}-NNN" if issued else "T-NNNN (none 카테고리)"
        id_line = f"`board.py new` 로 {idfmt} 발행"
    print(INIT_GUIDE.format(mode=mode, id_line=id_line))
    # 이 도구는 lease 장부의 writer 가 아니다(단일 writer 규약) — 작업 폴더를 만드는 것은
    # `pm-config worktree add` 다. 채택자가 그 다음 순서를 모른 채 남지 않도록 상태만 알린다.
    if not lease_store.leased_sessions((pm_paths.leases_file(home))):
        print(
            "  ⚠ 이 홈은 아직 작업 폴더가 없다 — 귀속 조작(claim·complete)이 세션 미해소로 "
            "중단된다. `pm-config worktree add <repo> --user-ack <repo>` 로 먼저 만들어라.",
            file=sys.stderr,
        )
    return 0
