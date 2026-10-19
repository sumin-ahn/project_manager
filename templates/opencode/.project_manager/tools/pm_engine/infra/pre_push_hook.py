"""pre-push 회귀 게이트 훅 (층: infra).

훅 본문과 세대 표기, git 이 해소한 훅 폴더, 설치와 세대 불일치 차단 안내를 소유한다.
"""
from __future__ import annotations

import contextlib
import os
import re
import subprocess
import sys
from pathlib import Path

from pm_engine.infra import fs, pm_paths, python_env


# ── 회귀 게이트 ──────────────────────────────────────────────────────
# 회귀 단위 ≡ **코드 repo** push 단위 · green 인 것만 push. 회귀 게이트가 붙는 자리는 `tests/` 가
# 있는 트리뿐이다 — 코드가 worktree 슬롯에 사는 분리 형상의 PM 홈(dev-state·board·wiki)에는 돌릴
# 스위트가 없어 회귀를 요구하지 않는다. 그 판정은 훅 본문이 push 시점에 직접 한다
# (`pre_push_hook_body` 의 `[ -d tests ]` 자기 가림 · lint 게이트는 두 형상 모두 항상).
# `regression run` 이 측정·기록(per-clone 로컬 플래그), pre-push 훅이 `regression check` 로 HEAD
# green 을 검증. 비차단 pre-warm 은 PM 이 `run_in_background` 로 `regression run` 을 돌리는
# 워크플로(하니스 background).

def _hooks_dir_resolution(home) -> tuple[Path | None, str | None]:
    """git 이 해소한 훅 디렉토리와 **판정 불능 사유** — 훅 위치 질의의 단일 git 호출 지점.

    반환은 세 형상뿐이고, 사유 유무가 곧 호출부의 방향을 가른다:
      · `(Path, None)`  — 해소 성공.
      · `(None, 사유)`  — **git 은 있는데 답을 못 했다**(rc≠0). 이 저장소의 훅 위치를 확정할 수
        없다는 뜻이라 판정 불능이다. 실측 클래스: global config 에 Windows 경로가 이스케이프
        없이 적혀(`hooksPath = C:\\Users\\…`) git 이 파싱에 실패하는 형상 — 그 repo 의 **모든**
        git 호출이 rc=128 이 되고, 종전 침묵 None 은 이 형상에서 훅 세대 판정을 통째로 no-op 으로
        만들었다(가드가 없는 것과 같다).
      · `(None, None)` — **git 을 부르지 못했다**(바이너리 부재·spawn 실패). 이 트리에서는 훅
        자체가 실행되지 않으므로 판정 대상이 없다(종전 '위치 해소 불가 = 무영향'과 같은 결론).
    """
    # encoding 명시 — git path 출력(Korean 경로 가능)을 cp949 로 디코딩하지 않도록 utf-8 고정.
    try:
        r = subprocess.run(["git", "-C", str(home.root), "rev-parse", "--git-path", "hooks"],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    except OSError:
        return None, None                      # git 부재·spawn 실패 — 훅 실행도 없는 환경.
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip().splitlines()
        first = detail[0] if detail else "사유 출력 없음"
        return None, f"git rev-parse --git-path hooks rc={r.returncode}: {first}"
    d = Path(r.stdout.strip())
    return (d if d.is_absolute() else home.root / d), None


def _hooks_dir(home) -> Path | None:
    """훅 디렉토리 (해소 실패는 None) — 설치 경로(`install_pre_push_hook`)가 쓰는 얇은 표면.

    사유가 필요한 판정 경로는 `_hooks_dir_resolution` 을 직접 쓴다.
    """
    return _hooks_dir_resolution(home)[0]


# 엔진이 설치한 훅임을 알아보는 서명 — 세대 판정이 남의 pre-push 훅까지 막지 않게 한다.
_PM_HOOK_SIGNATURE = "pm pre-push gate"

# 본문 세대 스탬프 — "이 훅이 어느 세대인가"를 파일 자신이 말하게 한다. 게이트 명령을 바꿀 때
# 이 값을 올리면, 이미 깔린 옛 훅이 스스로 구세대임을 드러낸다.
PM_HOOK_REV = 3
_PM_HOOK_REV_PREFIX = "# pm-hook-rev: "

# 설치된 훅에서 인터프리터를 되읽는 규칙 — 채택자의 런처 선택(`py -3.12` 등)이 세대 판정을
# 뒤집지 않게 한다(본문 대조를 그 인터프리터로 조립한다).
# (회귀 줄은 `[ -d tests ]` 블록 안이라 들여쓰기가 붙는다 — 앞 공백을 캡처 밖에서 흘린다.)
_PM_HOOK_INTERPRETER_RE = re.compile(
    r"^[ \t]*(?P<py>\S.*?) \.project_manager/tools/board\.py regression check", re.M,
)


def pre_push_hook_body(py: str | None = None) -> str:
    """pre-push 훅 본문 **단일 진실** — 설치·세대 대조가 같은 문자열을 본다.

    두 표면이 각자 본문을 알면 "설치된 훅이 현행인가"를 물을 수 없다(그게 구버전 훅이 조용히
    사는 채널이었다). `py` 미지정이면 호출 시점 인터프리터를 탐지한다.

    본문은 **게이트 둘**을 싣고 앞의 것만 트리를 보고 스스로 가린다:
      1. **회귀 게이트** — `[ -d tests ]` 인 트리에서만 돈다. 훅은 push 되는 트리의 루트에서 실행
         되므로(git) 이 판정의 대상이 곧 push 단위 그 자체다. `tests/` 가 없는 트리(분리 형상의
         PM 홈)는 돌릴 스위트가 없으니 회귀를 요구하지 않는다 — 종전엔 그 자리에서 회귀 cwd 를
         활성 슬롯으로 우회해 **코드가 한 줄도 안 바뀐 board/wiki push 가 슬롯 스위트 전량**을
         요구했다(채택자 제보).
      2. **lint 게이트** — 대상이 그 트리 자신의 board/wiki 라 코드 유무와 무관하다. **항상** 돈다
         (차단을 강제하는 상시 호출자는 이 훅 하나뿐이다)."""
    interpreter = py if py is not None else python_env._detect_py()
    return (
        "#!/bin/sh\n"
        "# pm pre-push gate — 회귀 게이트(tests/ 있는 트리만) AND lint 게이트(항상). "
        "board.py init 이 설치.\n"
        f"{_PM_HOOK_REV_PREFIX}{PM_HOOK_REV}\n"
        "if [ -d tests ]; then\n"
        f"  {interpreter} .project_manager/tools/board.py regression check || \\\n"
        f"    {interpreter} .project_manager/tools/board.py regression run --final || exit 1\n"
        "else\n"
        '  echo "pre-push: 회귀 게이트 비대상 — 이 트리에 tests/ 없음(PM 홈) · lint 게이트만"\n'
        "fi\n"
        f"{interpreter} .project_manager/tools/board.py lint --gate || exit 1\n"
    )


def _legacy_pre_push_hook_bodies(py: str) -> tuple[str, ...]:
    """**알려진 구세대 본문** 전수 (rev 미표기·rev 2 두 세대) — 정확일치 registry.

    차단 여부는 이 registry 가 아니라 "현행 본문인가" 하나로 갈린다(비-현행은 전부 차단). registry
    가 가르는 것은 안내의 사유 낱말뿐이다 — 아는 세대면 "구버전", 모르는 본문이면 "엔진이 모르는
    본문"이라고 말해 채택자가 자기 훅에 무슨 일이 있었는지 구분할 수 있게 한다.
    """
    return (
        # rev 미표기 세대 — 회귀 게이트가 `--final` 없이 FULL 을 요구하던 본문. 활성 리뷰 사이클
        # 중에는 강등 실행의 rc0 을 push 허가로 읽어 게이트가 열린다(차단의 직접 대상).
        "#!/bin/sh\n"
        "# pm pre-push gate — green 회귀 AND lint 게이트만 push. board.py init 이 설치.\n"
        f"{py} .project_manager/tools/board.py regression check || \\\n"
        f"  {py} .project_manager/tools/board.py regression run || exit 1\n"
        f"{py} .project_manager/tools/board.py lint --gate || exit 1\n",
        # rev 2 — 회귀 게이트가 트리를 보지 않고 **항상** 돌던 본문. 코드가 없는 분리 형상 PM 홈
        # 에서도 회귀를 요구해, 슬롯 스위트 전량이 board/wiki push 의 조건이 됐다(채택자 제보).
        # 이 훅이 남아 있으면 push 가 막히고 처방은 `board.py init` 재실행 1회다.
        "#!/bin/sh\n"
        "# pm pre-push gate — green 회귀 AND lint 게이트만 push. board.py init 이 설치.\n"
        f"{_PM_HOOK_REV_PREFIX}2\n"
        f"{py} .project_manager/tools/board.py regression check || \\\n"
        f"  {py} .project_manager/tools/board.py regression run --final || exit 1\n"
        f"{py} .project_manager/tools/board.py lint --gate || exit 1\n",
    )


def install_pre_push_hook(home) -> bool:
    """Install the pre-push gate (회귀 + lint). Idempotent. 자기 저장소의 훅 폴더에만 쓴다.

    두 단계를 AND 로 묶는다:
      1. 회귀 게이트 — green 회귀만 push (`regression check` 실패 시 `regression run --final`).
         `--final` 은 활성 리뷰 사이클 중의 targeted 강등을 건너뛴다 — push 게이트는 언제나
         FULL 이어야 한다(강등된 결과는 게이트 플래그를 쓰지 않아 훅이 통과할 수 없다).
         **`tests/` 가 있는 트리에서만** 돈다 — 판정은 훅 본문이 push 시점에 한다.
      2. lint 게이트 — `lint --gate` 차단 카테고리(dangling/unstable-ref/
         dependency/thin) 발견 시 push 실패. status drift 자문성은 차단 안 함. 두 형상 모두 돈다.

    **쓰는 자리는 `REPO/.git` 아래 하나다.** git 은 훅 위치를 물으면 트리를 걸어올라가 답하므로
    (비-git 폴더에서 물으면 상위 저장소의 훅 폴더를, `core.hooksPath` 가 선언돼 있으면 그 폴더를
    돌려준다), 그 답을 그대로 쓰면 남의 저장소 pre-push 를 덮는다(실측: `pm_import --into <남의
    저장소의 비-git 하위>` 가 그 저장소의 훅을 교체 · 원본 소실). 판정 입력은 자기 `.git`
    디렉터리와 git 이 해소한 훅 폴더 두 값뿐이고, 셋 중 어느 관문에서 멈추든 사유가 stderr 에
    1줄 남는다(침묵 False 없음). `board.py init` 가 (재)설치하므로 멱등·재설치 안전.
    """
    # 자기 `.git` 이 디렉터리인 트리만 plain 훅의 대상이다 — 비-git 폴더·linked worktree
    # (`.git` 이 gitfile)는 여기서 멈춘다(슬롯의 보호훅은 worktree_pool 이 설치한다).
    own = home.root / ".git"
    if not own.is_dir():
        print(f"{PM_HOOK_NOTICE_PREFIX}설치 생략 — 자기 `.git` 디렉터리가 없습니다 "
              f"({own}) · 훅은 자기 저장소의 `.git/hooks` 에만 씁니다.", file=sys.stderr)
        return False
    hooks = _hooks_dir(home)
    if hooks is None:
        print(f"{PM_HOOK_NOTICE_PREFIX}설치 생략 — git 이 훅 디렉토리를 해소하지 못해 "
              "설치할 자리를 확정할 수 없습니다.", file=sys.stderr)
        return False
    # 대소문자·심볼릭 링크·`..` 를 걷어낸 뒤 비교한다(`Path.is_relative_to` 는 문자열 비교라
    # 정규화 전 경로로는 같은 자리를 다른 자리로 읽는다 · `pm_log._wip_probe_targets` 동형 관용구).
    own_resolved = Path(os.path.normcase(str(own.resolve(strict=False))))
    hooks_resolved = Path(os.path.normcase(str(hooks.resolve(strict=False))))
    if not hooks_resolved.is_relative_to(own_resolved):
        print(f"{PM_HOOK_NOTICE_PREFIX}설치 생략 — 해소된 훅 폴더가 자기 `.git` 밖입니다 "
              f"(훅 폴더 {hooks_resolved} · 자기 .git {own_resolved}).", file=sys.stderr)
        return False
    hooks.mkdir(parents=True, exist_ok=True)
    _write_hook_atomic(hooks / "pre-push", pre_push_hook_body())
    return True


def _write_hook_atomic(hook: Path, body: str) -> None:
    """훅 파일을 temp + `fs.atomic_replace` 로 **원자 교체**한다 (`fs.write_json_atomic` 동형).

    같은 inode 에 truncate-rewrite 하면 **실행 중인 훅을 자기 자신이 덮을 수 있다** — shell 은
    스크립트를 실행하며 읽으므로 파일 오프셋이 바뀐 바이트를 이어 읽어 구문이 깨진다(정렬에 따라
    fail-open 방향도 나온다). 새 inode 로 갈아끼우면 실행 중인 shell 은 옛 fd 를 그대로 읽어
    끝까지 온전하다.
    실행 권한은 **교체 전 tmp 에** 준다 — 교체 뒤에 chmod 하면 그 사이 훅이 실행 불가다.
    **어느 단계가 실패하든 tmp 를 지운다** — 쓰기·chmod·교체 세 단계가 모두 정리 범위다(교체만
    감싸면 앞 단계 실패에서 잔재가 남아 서술과 어긋난다). `pre-push.<pid>.tmp` 는 git 이 실행하지
    않는 이름이라 무해했지만, 실패마다 훅 디렉토리에 잔재가 쌓인다. 실패 자체는 그대로
    올린다(호출부가 판정)."""
    tmp = hook.with_name(f"{hook.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(body, encoding="utf-8", newline="\n")
        tmp.chmod(0o755)
        fs.atomic_replace(tmp, hook)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


# ── 구형 서명 훅 감지 (fail-closed) ─────────────────────────────────────────
# 훅 본문은 **설치 시점에 박제**되므로 엔진이 게이트 명령을 바꿔도 이미 설치된 훅은 옛 명령을
# 계속 돈다. 실측 클래스: 강등(`--final` 부재) 훅이 targeted 결과 rc0 을 push 허가로 해석해
# FULL 플래그 없이 push 가 열린다. 그래서 회귀 게이트가 진입할 때 세대를 대조하고, 현행이 아니면
# **차단**한다(고치지 않는다).
#
# 자동 교체(치유)를 두지 않는 이유: 교체는 *다음* 실행부터 유효한데 지금 돌고 있는 것은 교체 전
# 본문이라, 그 실행의 강등 rc0 을 옛 훅이 push 허가로 읽는다. 그 창을 닫으려면 "방금 고쳤다"를
# 별 프로세스(훅 `check || run` 2단계)에 전달해야 하고, 그건 공유 상태이며 공유 상태는 경합·TTL·
# 표식 탈취를 낳는다(4라운드 실증). 차단은 상태가 필요 없다 — 판정 한 번이 곧 결론이다.

# git config 순수 파서 — `core.hooksPath` 값 하나만 읽는다(subprocess 0). git 은 섹션·키 이름을
# **대소문자 무관**으로 다루므로 매칭도 그렇게 한다(`[CORE] HooksPath` 도 같은 키다).
_GIT_CONFIG_SECTION_RE = re.compile(r"^\s*\[(?P<name>[^\]\s]+)(?:\s+\"(?P<sub>[^\"]*)\")?\]")
_GIT_CONFIG_ENTRY_RE = re.compile(r"^\s*(?P<key>[A-Za-z][A-Za-z0-9-]*)\s*=\s*(?P<value>.*)$")

# 이 섹션이 있으면 값 해석을 포기한다 — 포함 파일의 선언이 최종 값을 뒤집을 수 있는데, 그 파일까지
# 따라가는 것은 git config 해소를 다시 구현하는 일이다(fail-open 이 안전한 방향).
_GIT_CONFIG_INCLUDE_SECTIONS = frozenset({"include", "includeif"})
_HOOKS_PATH_KEY = "hookspath"
_HOOKS_PATH_SECTION = "core"

# "선언은 있는데 git 없이 확정 불가" 를 "선언 없음"과 구분하는 표식.
_HOOKS_PATH_UNRESOLVED = object()


def _git_config_value(raw: str) -> str:
    """git config 값 한 줄에서 실제 값만 남긴다 — 인용 해제 + 인용 밖 주석(`#`/`;`) 절단.

    손편집 config 에 `hooksPath = .githooks  # 사내 표준` 같은 줄이 있으면 주석까지 경로로 읽는다
    (git 은 인용 밖 `#`/`;` 부터를 주석으로 본다)."""
    text = raw.strip()
    if text.startswith('"'):
        end = text.find('"', 1)
        return text[1:end] if end != -1 else text[1:]
    for comment in ("#", ";"):
        cut = text.find(comment)
        if cut != -1:
            text = text[:cut]
    return text.strip()


def _config_hooks_path(text: str) -> object | None:
    """git config 원문에서 `core.hooksPath` 선언 값을 순수 파싱한다.

    반환은 셋 중 하나다:
      · `str` — 선언된 값(마지막 선언이 이긴다 · git 의 last-wins 와 같다).
      · `None` — 선언 없음(훅은 기본 위치).
      · `_HOOKS_PATH_UNRESOLVED` — include/includeIf 가 있거나 값이 비어 git 없이 확정 불가.
    """
    section = ""
    value: object | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped[0] in "#;":
            continue
        header = _GIT_CONFIG_SECTION_RE.match(line)
        if header is not None:
            section = header.group("name").lower()
            if section in _GIT_CONFIG_INCLUDE_SECTIONS:
                return _HOOKS_PATH_UNRESOLVED
            continue
        if section != _HOOKS_PATH_SECTION:
            continue
        entry = _GIT_CONFIG_ENTRY_RE.match(line)
        if entry is None or entry.group("key").lower() != _HOOKS_PATH_KEY:
            continue
        resolved = _git_config_value(entry.group("value"))
        if not resolved:
            return _HOOKS_PATH_UNRESOLVED
        value = resolved
    return value


def _repo_git_dir(home) -> Path | None:
    """REPO 의 **공용 git 디렉토리** (`.git` 부재·해소 불가면 None = 비-repo 형상).

    규칙:
      · `REPO/.git` 디렉토리 → 그 경로.
      · `REPO/.git` 파일(linked worktree·`gitdir: <경로>`) → 그 gitdir 의 `commondir` 을 따라
        **공용 git 디렉토리**로 올라간다 (훅은 worktree 간 공유다).

    반환 None 은 "이 트리는 판정 대상이 아니다"라는 뜻이다 — 훅 세대 판정도 그 자리에서 끝내고
    git 조회조차 하지 않는다(REPO 가 repo 루트가 아닌 트리에서 조상 repo 의 훅을 끌어오지
    않는다·종전 무영향 형상 보존).
    """
    try:
        git_path = home.root / ".git"
        if git_path.is_dir():
            git_dir = git_path
        elif git_path.is_file():
            pointer = fs.read_text_shared(git_path, encoding="utf-8").strip()
            if not pointer.startswith("gitdir:"):
                return None
            target = Path(pointer[len("gitdir:"):].strip())
            git_dir = target if target.is_absolute() else (home.root / target)
        else:
            return None
        common = git_dir / "commondir"
        if common.is_file():
            relative = fs.read_text_shared(common, encoding="utf-8").strip()
            if relative:
                shared = Path(relative)
                git_dir = shared if shared.is_absolute() else (git_dir / shared)
        return git_dir
    except (OSError, UnicodeError, ValueError):
        return None


def _pure_hooks_dir(home) -> Path | None:
    """훅 디렉토리를 **git 호출 없이** 해소한다 (해소 불가면 None).

    회귀 게이트 진입은 hot path 라 여기에 subprocess 를 더하지 않는다 — `_hooks_dir()`(git
    `rev-parse --git-path`)는 설치 경로가 계속 쓰고, 세대 판정은 이 순수 해소를 먼저 쓴다
    (권위 해소는 이 자리에서 훅을 못 찾았을 때만·`_stale_pre_push_hook_refusal`). 규칙:
      · `REPO/.git`(디렉토리·linked worktree 파일) → 공용 git 디렉토리 아래 `hooks/`.
      · repo config 에 `core.hooksPath` 선언이 있으면 **그 값을 파싱해** 훅 위치로 쓴다. 상대
        경로는 git 과 같이 worktree 루트(`REPO`) 기준이고 `~` 는 확장한다(확장 규칙은
        `_expanded_user_path` — 없는 `~user` 는 플랫폼 무관 해소 실패). 존재-only 로 보고
        포기하면 *기본 위치를 가리키는 선언*까지 세대 관리 밖에 남는다.
      · include/includeIf 가 있거나 값이 비면 **None** — 포함 파일의 선언이 최종 값을 뒤집을 수
        있어 git 없이 확정할 수 없다(그 형상은 권위 해소가 받는다).
      · `~user` 미확장도 **None** — 이 순수 해소로는 위치를 확정할 수 없다는 뜻이라 권위 해소가
        그 자리를 받는다(POSIX 만 RuntimeError 로 터지던 비대칭 제거).
    """
    git_dir = _repo_git_dir(home)
    if git_dir is None:
        return None
    try:
        config = git_dir / "config"
        if config.is_file():
            declared = _config_hooks_path(fs.read_text_shared(config, encoding="utf-8"))
            if declared is _HOOKS_PATH_UNRESOLVED:
                return None
            if isinstance(declared, str):
                hooks = pm_paths._expanded_user_path(declared)
                # 상대 `core.hooksPath` 는 git 이 worktree 루트 기준으로 해소한다(프로세스 cwd 아님).
                return (hooks if hooks.is_absolute() else (home.root / hooks)).resolve()
        return (git_dir / "hooks").resolve()
    except (OSError, UnicodeError, ValueError, RuntimeError):
        return None


def _has_pre_push(hooks: Path) -> bool:
    """그 훅 디렉토리에 `pre-push` 파일이 실재하는가 (읽기 실패는 False·fail-soft)."""
    try:
        return (hooks / "pre-push").is_file()
    except OSError:
        return False


def _authoritative_hooks_resolution(home) -> tuple[Path | None, str | None]:
    """git 권위 해소 `(훅 디렉토리, 판정 불능 사유)` — `_hooks_dir_resolution` 의 판정용 표면.

    저장소 config 직접 파싱(`_pure_hooks_dir`)은 **repo config 의 선언만** 본다. `core.hooksPath`
    는 global(`~/.gitconfig`)·system·명령행(`-c`)·worktree 범위에서도 선언되고, 훅을 실행하는
    git 과 훅을 설치하는 엔진(`install_pre_push_hook` → `_hooks_dir`)은 그 해소를 따른다 —
    세대 판정만 `.git/hooks` 를 보면 사내 표준 hooksPath 형상에서 **구세대 훅이 계속 실행되는데
    차단은 서지 않는다**(우회). 이 함수가 그 자리를 메운다.

    subprocess 1회를 쓴다 — 회귀 진입은 hot path 지만 이 축은 push 게이트 차단이라 "위치를 잘못
    봐서 안 막는 것"이 비용보다 크다. 그래서 **언제 묻는지**는 호출부가 정한다
    (`_stale_pre_push_hook_refusal` — 순수 해소가 훅을 못 찾았을 때만).

    사유 유무가 호출부의 방향이다(`_hooks_dir_resolution` 서술과 같은 규칙): 사유가 있으면
    **git 이 답을 못 한 판정 불능**이라 차단 방향이고, 사유 없는 None(git 부재·대역 subprocess의
    예외)은 종전대로 순수 해소 결과를 그대로 쓴다.
    """
    try:
        return _hooks_dir_resolution(home)
    except Exception:  # noqa: BLE001 — 대역 subprocess/스텁 예외는 순수 해소로 폴백.
        return None, None


def _installed_hook_interpreter(body: str) -> str | None:
    """설치된 훅이 쓰는 인터프리터 (게이트 줄이 없으면 None)."""
    match = _PM_HOOK_INTERPRETER_RE.search(body)
    return match.group("py") if match else None


# 훅 세대 안내의 **호출 경로 중립 접두** — 이 판정은 `regression`(스위트 있는 트리)과
# `lint --gate`(두 형상 모두) 두 진입에서 나온다. 접두가 한 채널 이름(`regression:`)이면 다른
# 채널의 소비자가 그 출력을 자기 채널 산출로 읽다가 파싱에 실패한다(pm_bootstrap 의 lint dump
# 가 `gate error (unparsed)` 로 저하되던 자리). 접두를 여기서 소유해 소비자가 사본 없이 짚는다.
PM_HOOK_NOTICE_PREFIX = "pre-push 훅: "

# 차단 안내 — 처방은 `init` 재실행 하나뿐이라 두 사유(구세대·미상 본문)가 같은 문장을 쓴다.
# 인터프리터 표기는 채택자 문서와 같은 규칙이다(Linux/macOS `python3` · Windows 런처 `py -3`).
_STALE_HOOK_REFUSAL = (
    f"{PM_HOOK_NOTICE_PREFIX}설치된 pre-push 훅이 {{kind}}입니다 ({{hook}}) — "
    "`python3 .project_manager/tools/board.py init` 재실행 후 다시 시도"
    "(Windows 는 py -3)."
)
_UNREADABLE_HOOK_REFUSAL = (
    f"{PM_HOOK_NOTICE_PREFIX}설치된 pre-push 훅을 읽지 못해 세대를 판정할 수 없습니다 "
    "({hook}: {error}) — `python3 .project_manager/tools/board.py init` 재실행 후 "
    "다시 시도(Windows 는 py -3)."
)

# git 이 훅 위치 자체를 답하지 못한 경우 — **사유를 실어 알린다**(침묵 no-op 금지). 차단이
# 아니라 경고인 이유는 이 rc≠0 이 두 형상을 합치기 때문이다: (1) config 파싱 실패(실측 원인 —
# git config 는 값의 백슬래시를 이스케이프로 읽으므로 `C:\path\to\hooks` 는 파싱 실패고
# `C:/path/to/hooks` 또는 `\\` 이스케이프로 적어야 한다), (2) git 이 보기엔 저장소가 아닌 트리·dubious ownership
# 등 **환경적 판정불가**. 둘은 rc·문구(로케일 의존)로 갈리지 않는데, 후자를 차단하면 훅을 쓰지
# 않는 정상 형상의 `regression run` 까지 멈춘다. 두 형상 모두 그 git 으로는 push 자체가 서지
# 않으므로 push 게이트가 이 침묵으로 우회되지도 않는다 — 잃던 것은 **관측성**이었다(판정이 돌아
# 통과한 것인지 아예 안 돈 것인지 구분 불가). 그 관측성만 되돌린다.
# (`_sha_anchor_status` 의 rc≥2 fatal = 환경적 판정불가 규약과 같은 방향이다.)
_UNRESOLVED_HOOKS_NOTICE = (
    f"{PM_HOOK_NOTICE_PREFIX}훅 디렉토리를 해소하지 못해 세대 판정을 생략합니다 ({{reason}}) — "
    "훅을 쓰는 형상이면 git 설정(core.hooksPath 등)을 확인하십시오"
    "(Windows 경로는 `C:/path/to/hooks` 표기 또는 백슬래시 이스케이프)."
)


def _stale_pre_push_hook_refusal(home) -> str | None:
    """설치된 pre-push 훅이 **현행 세대가 아니면** 차단 안내 1줄, 아니면 None.

    차단 대상은 **pm 서명을 가진 비-현행 훅** 하나다(구세대 registry 일치 · 엔진이 모르는 본문
    둘 다). 아는 세대만 골라 다루던 옛 경계는 자동 교체가 있을 때의 것이었다 — 덮어쓰지 않는
    지금은 커스터마이즈 본문도 "이 게이트가 무엇을 강제하는지 확정할 수 없는 훅"이라 같은 결론이다.
    registry 는 남아서 안내의 사유 낱말만 가른다(구버전인지, 엔진이 모르는 본문인지).

    **무영향으로 두는 형상 셋**: 훅 미설치 · 서명 없는 남의 훅 · 훅 위치 해소 불가. 셋 다
    "이 트리에 엔진 push 게이트가 없다"는 뜻이라, 차단하면 훅을 안 쓰는 정상 형상까지 막는다.
    다만 셋째는 **조용히 두지 않는다** — git 이 훅 위치를 답하지 못하면(rc≠0) 사유를 stderr 에
    남기고 판정을 생략한다(`_UNRESOLVED_HOOKS_NOTICE`). 침묵하면 "판정이 돌아서 통과"와 "판정이
    아예 안 돎"이 호출부에서 같은 값(None)이라, 가드가 통째로 no-op 인 형상이 green 으로 보인다
    (Windows 실측: global config 의 `hooksPath = C:\\Users\\…` 미이스케이프 → 그 repo 의 모든
    git 호출 rc=128 → 세대 판정 전면 no-op).

    나머지 판정 실패는 **차단 방향**이다: 훅 파일이 있는데 읽지 못하면 세대를 확정할 수 없고,
    확정 못 한 훅을 통과시키는 것이 곧 옛 게이트가 조용히 사는 채널이다(git 도 읽지 못하는 훅은
    실행하지 못한다).

    인터프리터는 **설치된 훅의 것**으로 대조한다 — 채택자의 런처 선택(`py -3.12`)이 세대 판정을
    뒤집지 않게.

    위치 해소는 두 단계다:
      1. 순수 해소(`_pure_hooks_dir`)가 찾은 자리에 `pre-push` 가 **실재하면** 그 파일로 판정한다
         — 이미 판정 대상을 손에 쥐었으므로 git 을 부를 이유가 없다(회귀 진입에 subprocess 0).
      2. 그 자리에 훅이 없거나 순수 해소가 확정에 실패하면 **git 의 권위 해소**
         (`_authoritative_hooks_resolution`)를 묻는다. repo config 밖(global/system/worktree)에서
         `core.hooksPath` 가 선언된 형상이 정확히 이 경우다 — 훅은 그 경로에 설치돼 실행되는데
         순수 해소는 `.git/hooks` 를 보고 "미설치"로 읽어 세대 차단이 우회됐다. git 이 **답을
         못 하면 사유를 stderr 에 남기고**, 부르지 못하면(부재·대역) 조용히 — 어느 쪽이든 현행
         순수 해소 결과를 그대로 쓴다(기존 판정 유지).

    `REPO` 가 repo 루트가 아닌 트리(`.git` 부재)는 그 앞에서 끝낸다 — 조상 repo 의 훅을 이 트리의
    판정으로 끌어오지 않는다(종전 '위치 해소 불가 = 무영향'과 같은 결론·git 호출 0).

    남는 사각은 하나다: repo config 밖 `hooksPath` 형상에서 `.git/hooks/pre-push` 잔재가 남아
    있으면 1단계가 그 잔재를 판정한다(git 이 실행하지 않는 파일). 잔재가 구세대 엔진 훅이면
    차단(안전 방향)이고, 남의 훅/현행 본문이면 권위 경로를 못 본다 — 잔재 파일이 있어야 성립하는
    좁은 형상이라, 회귀 진입 비용(모든 실행에 git 1회)과 바꾸지 않는다."""
    if _repo_git_dir(home) is None:
        return None                                   # 비-repo 트리 — 판정 대상 밖(git 호출 0).
    hooks = _pure_hooks_dir(home)
    if hooks is None or not _has_pre_push(hooks):
        authoritative, unresolved_reason = _authoritative_hooks_resolution(home)
        if unresolved_reason is not None:
            # git 이 훅 위치를 답하지 못했다 — 판정을 생략하되 **사유를 남긴다**. 침묵하면
            # "판정이 돌아서 통과"와 "판정이 아예 안 돎"이 같은 결과로 보인다(Windows 실측에서
            # 가드가 통째로 no-op 이었는데도 green 이던 자리).
            print(_UNRESOLVED_HOOKS_NOTICE.format(reason=unresolved_reason), file=sys.stderr)
        hooks = authoritative or hooks
    if hooks is None:
        return None                                   # 위치 해소 불가 — 비-훅 형상 차단 금지.
    hook = hooks / "pre-push"
    try:
        if not hook.is_file():
            return None                               # 미설치 — 무영향.
        current = fs.read_text_shared(hook, encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return _UNREADABLE_HOOK_REFUSAL.format(hook=hook, error=exc)
    if _PM_HOOK_SIGNATURE not in current:
        return None                                   # 남의 훅 — 대상 아님.
    interpreter = _installed_hook_interpreter(current) or python_env._detect_py()
    if current == pre_push_hook_body(interpreter):
        return None                                   # 현행 세대 — 무소음 통과.
    kind = ("구버전" if current in _legacy_pre_push_hook_bodies(interpreter)
            else "엔진이 모르는 본문")
    return _STALE_HOOK_REFUSAL.format(kind=kind, hook=hook)
