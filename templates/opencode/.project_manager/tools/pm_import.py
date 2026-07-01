#!/usr/bin/env python3
"""pm_import — PM 프레임워크 import 단일 진입 커맨드 (--new = PM 홈 생성 / --into = 기존 프로젝트 임베드).

현행 채택 플로우(루트 README §3.2 의 수동 longhand: `cp -r` + sed +
`board.py init` + 손)의 **기계 단계**(결정적·무LLM)를 1 커맨드로 대체하고(T-0007), 그 위에
sed 로 못 채우는 **자유서술 placeholder** 채움(하니스 헤드리스 구동·opt-in)을 얹는다(T-0009).

사용:
    pm_import.py (--into <기존프로젝트> | --new <프로젝트>)   # 모드 택1
                 --harness {claude,opencode,both}            # 어댑터 (default: claude)
                 --weight  {full,lite}                        # 무게축 (default: full)
                 [--from <프레임워크-checkout>]               # 소스 (default: 이 repo 루트)
                 [--name <표시이름>]                          # {{PROJECT_NAME}} (default: 디렉토리명)
                 [--fill {auto,manual}]                       # 자유서술 채움 (default: manual)
                 [--fill-harness {claude,opencode}]           # 구동 하니스 (default: --harness)
                 [--dry-run]                                  # 적용 없이 계획만 출력(fill 미호출)

동작:
  소스 = <--from>/templates/<harness>/ 트리(엔진 + 어댑터)를 대상으로 복사한다.
  `both` 면 두 어댑터 트리를 병합 복사(엔진 동일·어댑터 디렉토리/파일명 안 겹쳐 충돌 0).
  복사 후 operational placeholder 를 sed 치환하고 `board.py init`(solo)을 호출한다.
    - sed 대상 = {{PROJECT_NAME}}·{{PROJECT_TAGLINE}}·{{PROJECT_ROOT}}·{{PY}}·{{TEST_CMD}}·{{DATE}}.
    - 엔진 문서(wiki/pm_role.md·pm_playbook.md)는 sed 제외 — local.conf 가 런타임 해소.
    - 자유서술 3종({{PROJECT_CONSTRAINTS}}·{{PROTECTED_PATHS}}·{{USER_GATE_ITEMS}})은 보존(아래 fill).
  board init·local.conf 동기화 직후 **fill 단계**(T-0009)가 자유서술 placeholder 를 처리한다:
    - --fill manual(기본): 하니스 미구동, placeholder 를 `<!-- TODO: ... -->` 로 표시(채택자가 손으로).
    - --fill auto: 대상 repo 분석 프롬프트로 하니스(claude -p / opencode run --format json)를
      헤드리스 구동해 placeholder 값 + (해당 시) CLAUDE.md/pm_role.local.md 초안을 *제안*한다.
      생성물은 제안일 뿐 — 적용은 사용자 리뷰 전제(비가역 회피). --dry-run 이면 실 하니스를
      호출하지 않고 fill *계획*(채울 대상 토큰·결정된 harness·opt-in 게이트 상태)만 출력한다
      (파일 미변경·비용 0 — opt-in 게이트상 dry-run 에서 실호출 금지).
  --into: 기존 충돌 파일은 중앙 디렉토리 .pm_import_backups/<DATE>/<relpath> 에 백업 후 덮음
          (비파괴·T-0034). 단 git 이 추적 중이고 미변경인 파일은 백업 생략(git 이 복원). --new:
          디렉토리 생성 + git init. (이전 형제 *.backup.<DATE> 분산 방식은 폐기.)

결정:
  - 독립 pm_import.py (board.py 비대화). stdlib only.
  - idempotent — 재실행 시 백업하고 안전. --dry-run 은 파일시스템 미변경(plan/apply 분리).
  - --weight lite 는 진입 파일 선택만 영향(T-0010). 어댑터의 `X.lite.md`(예 CLAUDE.lite.md·
    AGENTS.lite.md)를 dst `X.md` 로 rename 배치하고 full `X.md`·원본 `*.lite.md` 는 제외한다.
    full(기본)은 모든 `*.lite.md` 를 제외하고 full 진입(X.md)만 깐다.
  - fill opt-in 게이트(external_review 선례): 하니스 실구동은 토큰·외부모델 비용 → 기본 OFF.
    **실호출은 환경변수 PM_IMPORT_LIVE_HARNESS=1 AND --fill auto 동시 충족 시만.** 둘 중 하나라도
    없으면 실 runner 를 호출하지 않는다(CI·기본 테스트는 stub). 회사 배포(claude code 없음)는
    opencode 구동 경로 1급 — `both`/혼합이면 claude 우선·부재 시 opencode 폴백.

opt-in 실 e2e (CI 비포함 — 토큰·외부모델 비용 발생):
    1) 대상 하니스 바이너리 설치 확인 (`claude` 또는 `opencode` 가 PATH 에 있어야 함).
    2) 환경변수와 플래그를 *동시* 지정해 실구동:
           PM_IMPORT_LIVE_HARNESS=1 pm_import.py --into <repo> --fill auto [--fill-harness opencode]
       - 둘 중 하나만 주면 실호출이 차단되고 stub/manual 로 폴백한다(안전).
    3) 출력된 자유서술 placeholder 값·초안은 *제안*이다 — 사람이 검토 후 손으로 반영한다
       (pm_import 는 자유서술 채움을 자동 확정하지 않는다). --dry-run 은 실 하니스를 호출하지
       않고 *fill 계획*(채울 토큰·harness·게이트 상태)만 미리 보여준다(파일 미변경).
"""

from __future__ import annotations

import argparse
import datetime
import functools
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

REPO = Path(__file__).resolve().parents[2]

HARNESS_CHOICES = ("claude", "opencode", "both")
WEIGHT_CHOICES = ("full", "lite")

FILL_CHOICES = ("auto", "manual")
FILL_HARNESS_CHOICES = ("claude", "opencode")

# fill 단계가 채우는 자유서술 placeholder 3종 (sed 로 못 채움 — repo 분석 필요).
# operational(보존) 토큰과 달리 board init 후 하니스 구동(auto) 또는 TODO 표시(manual)로 처리.
FREE_FORM_TOKENS = (
    "{{PROJECT_CONSTRAINTS}}",
    "{{PROTECTED_PATHS}}",
    "{{USER_GATE_ITEMS}}",
)

# opencode 어댑터 고유 harness 설정값 — operational/자유서술 어디에도 안 속하는 모델 ID.
# `.opencode/agents/*.md` 의 `model:` 필드로 등장(T-0032 후 주 타깃). T-0033 부터 LLM fill 이
# *아니라* `opencode models` 결정적 조회로 해소한다(resolve_opencode_model) — 따라서 fill
# 후보에서 분리됐다(중복·환각 제거). opencode 트리가 복사됐을 때만 해소 단계를 탄다.
OPENCODE_MODEL_TOKEN = "{{OPENCODE_PRO_MODEL}}"

# `opencode models` 조회 명령 — 가용 모델의 단일 진실(LLM 추측 아님·T-0033). 줄당 `provider/model`.
OPENCODE_MODELS_CMD = ("opencode", "models")

# fill 실 하니스 구동 opt-in 게이트 환경변수 (external_review 선례). PM_IMPORT_LIVE_HARNESS=1
# AND --fill auto 동시 충족 시만 실 runner 호출 — 둘 중 하나라도 없으면 stub/manual 강제.
LIVE_HARNESS_ENV = "PM_IMPORT_LIVE_HARNESS"

# 하니스 헤드리스 구동 명령 (fill auto). claude 는 stdout 캡처, opencode 는 --format json 파싱.
CLAUDE_FILL_CMD = ("claude", "-p")
OPENCODE_FILL_CMD = ("opencode", "run")

# 하니스 호출 타임아웃 (초) — repo 분석 1회.
FILL_TIMEOUT_SECONDS = 300

# `opencode models` 조회 타임아웃 (초). 모델 목록 나열은 빠른 로컬 명령 가정이나, 회사 Pro/원격
# 게이트웨이는 cold 콜 지연이 커 15s 로는 부족(T-0127 회사 실사용 — 자동해소 실패→수동 폴백).
# 기본을 60 으로 올리고, env override 로 환경별 재조정(T-0070 의 PM_SUBMODULE_TIMEOUT 동형).
# (FILL_TIMEOUT 300 은 LLM 헤드리스 구동용 — 모델 조회엔 과대. --opencode-model 명시 경로의
# 대조-조회가 import UX 를 길게 막지 않도록 fail-soft + 적당한 상한. T-0033 codex suggestion.)
OPENCODE_MODELS_TIMEOUT_SECONDS = 60


# env override (T-0127·T-0070 PM_SUBMODULE_TIMEOUT 동형): 회사 Pro·느린 원격에서 60s 도 모자라면
#   코드 수정 없이 `PM_OPENCODE_MODELS_TIMEOUT`(초)로 늘린다. 양의 정수만 채택 — 미설정/비숫자/≤0
#   은 기본 OPENCODE_MODELS_TIMEOUT_SECONDS(60) 로 폴백(무해). 빠른 로컬 조회라 무제한은 두지 않는다.
def _opencode_models_timeout() -> int:
    raw = os.environ.get("PM_OPENCODE_MODELS_TIMEOUT")
    if raw is None:
        return OPENCODE_MODELS_TIMEOUT_SECONDS
    try:
        val = int(raw.strip())
    except (ValueError, AttributeError):
        return OPENCODE_MODELS_TIMEOUT_SECONDS
    return val if val > 0 else OPENCODE_MODELS_TIMEOUT_SECONDS

# --harness 값 → templates/ 하위 어댑터 트리 디렉토리명.
HARNESS_TEMPLATE_DIRS = {
    "claude": ("claude_code",),
    "opencode": ("opencode",),
    "both": ("claude_code", "opencode"),
}

# sed 치환 대상 operational placeholder (루트 README §4 표). 자유서술 3종은 여기 없음(보존).
OPERATIONAL_TOKENS = (
    "{{PROJECT_NAME}}",
    "{{PROJECT_TAGLINE}}",
    "{{PROJECT_ROOT}}",
    "{{PY}}",
    "{{TEST_CMD}}",
    "{{DATE}}",
)

# 치환 대상 파일 확장자 (루트 README §3.2 sed 의 --include 와 동일).
SUBSTITUTE_SUFFIXES = (".md", ".json", ".sh", ".py")

# operational placeholder 치환에서 *제외*하는 엔진 문서 (repo 기준 relpath).
# local.conf 가 런타임 해소 + pm_update 동기화 대상이라 리터럴 유지(루트 README §4·D11).
SED_EXCLUDE_RELPATHS = frozenset({
    ".project_manager/wiki/pm_role.md",
    ".project_manager/wiki/pm_playbook.md",
})

# 복사/스캔 제외 디렉토리명 (무겁고 재설치 대상 / stale 산출물 / VCS 메타).
#   node_modules — opencode 의존성(재설치 대상). __pycache__ — stale 바이트코드(.pyc).
#   .git — VCS 메타(템플릿엔 없어 복사목록엔 안 끼지만, fill 폴백 전체 스캔이 대형 repo
#          .git 을 텍스트 read 하지 않도록 명시 제외 — 낭비 방지·결정론).
COPY_EXCLUDE_DIR_NAMES = frozenset({"node_modules", "__pycache__", ".git"})

# 복사 제외 파일 (정확 dst relpath) — adopter 에게 출하하지 않을 프레임워크-repo 내부 문서.
#   README.md(최상위) — 템플릿 트리의 "어댑터 타깃" 설명서다(프레임워크 상대링크 `../../README.md`·
#   `../opencode/README.md` 를 담아 adopter 트리에선 dangling·오해 소지). 채택자는 자기 프로젝트
#   README 를 쓴다 → 프레임워크-내부 doc 를 adopter README 로 박제하지 않는다(both 도 이 충돌 소거).
#   하위 `.project_manager/wiki/*/README.md`(wiki 구조 안내)는 유지 — 정확 relpath `README.md` 만 제외.
COPY_EXCLUDE_RELPATHS = frozenset({"README.md"})

# --into 백업 중앙화 디렉토리 (T-0034). 충돌 파일별 형제 `*.backup.<DATE>` 를 트리 전역에
# 흩뿌리는 대신, 무백업 덮기 불가(미추적·dirty·비-git)인 파일만 단일 디렉토리
# `<dest>/.pm_import_backups/<DATE>/` 에 relpath 미러링으로 모은다. git 이 추적 중이고
# 미변경인 파일은 git 이 내용을 보존하므로 백업 없이 덮는다(git-safe skip).
BACKUP_DIR_NAME = ".pm_import_backups"

# `git` 호출 seam — argv(list) → (returncode, stdout). 테스트가 stub 주입(라이브 git 미실행).
# git_safe_relpaths 가 work tree 판별·추적집합 조회에 사용한다(_real_models_runner 류 결정적 seam).
GitRunner = Callable[[list], "tuple[int, str]"]

# git 호출 타임아웃 (초) — ls-files/status 는 빠른 로컬 명령이라 짧게(과대 대기 방지·fail-soft 상한).
GIT_SAFE_TIMEOUT_SECONDS = 15

# upstream git 호출(ls-remote·remote get-url·rev-parse) 타임아웃 (초·T-0145). ls-remote 는
# 네트워크라 ls-files 보다 넉넉히 — pm_config.GIT_TIMEOUT_SECONDS(clone·600)보단 짧게(도달성
# 체크는 clone 만큼 길 필요 없음·과대 대기 방지).
UPSTREAM_GIT_TIMEOUT_SECONDS = 60

# upstream(네트워크-facing) git 호출의 config 격리 키 (codex MF4·worktree_pool GIT_CONFIG_*
# 선례). untrusted URL 의 ls-remote/rev-parse 에 사용자/global git config 의 `insteadOf`
# rewrite·credential helper 가 끼어드는 것을 막는다(defense-in-depth). GIT_CONFIG_GLOBAL/
# SYSTEM=/dev/null 로 global·system config 를 통째 무력화하고, GIT_CONFIG_COUNT 패턴으로
# protocol allowlist(https/ssh/file 만 always·나머지 never) + credential.helper=(빈값·helper
# 미경유)를 강제한다. (분류 검증[validate]은 *형태* 안전, 이 env 는 *실행* 격리 — 이중 방어.)
_UPSTREAM_GIT_CONFIG_KV = (
    ("credential.helper", ""),          # credential helper 미경유(자격증명 자동주입 차단).
    ("protocol.allow", "never"),        # 기본 거부 — 아래 명시 protocol 만 허용(allowlist).
    ("protocol.https.allow", "always"),
    ("protocol.ssh.allow", "always"),
    ("protocol.file.allow", "always"),
    ("http.followRedirects", "false"),  # redirect 추적 차단(codex hardening·D5 잔여 SSRF 표면).
)

# 인터프리터 탐지는 board.py 의 _detect_py() 가 단일 진실(T-0019/C5). pm_import 가 자체
# 탐지를 신설하지 않고 board.py 를 재사용한다 — 플랫폼별 python3/python 해석을 한 곳에 둔다.
# board.py import 가 실패하면(예: yaml 부재) 리눅스 현행과 동치인 "python3" 로 폴백.
_DEFAULT_PY_FALLBACK = "python3"


def _detected_py() -> str:
    """{{PY}} 치환·local.conf py= 기본값으로 쓸 인터프리터 명령을 board.py 에서 탐지한다.

    board.py 의 _detect_py() 를 import 해 재사용(단일 진실). board.py 와 같은 디렉토리에
    있으므로 spec_from_file_location 으로 직접 로드 — sys.path 오염 없이 호출 가능.
    어떤 이유로든 로드/호출이 실패하면 "python3" 폴백(리눅스 현행 동치).
    """
    board_py = Path(__file__).resolve().parent / "board.py"
    try:
        spec = importlib.util.spec_from_file_location("_board_for_detect_py", board_py)
        if spec is None or spec.loader is None:
            return _DEFAULT_PY_FALLBACK
        board_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(board_mod)
        return board_mod._detect_py()
    except Exception:  # noqa: BLE001 — 탐지 실패는 폴백, import 를 깨지 않는다.
        return _DEFAULT_PY_FALLBACK


def _default_test_cmd() -> str:
    """기본 test_cmd — 탐지된 인터프리터로 pytest 실행 (Windows 에선 `python`, POSIX 는 python3).

    상수 하드코딩(`python3 -m pytest`)은 Windows 에서 깨진다(`python3`=비기능 shim 또는
    엉뚱한 Store Python). `_detected_py()` 를 경유해 board.py `_detect_py()` 의 실행검증된
    인터프리터를 쓴다 — local.conf `py=` 와 동일 소스라 일관(T-0022).
    """
    return f"{_detected_py()} -m pytest tests/ -q"


DEFAULT_TAGLINE = "한 줄 프로젝트 설명"


# pm_playbook.local.md 스텁 본문 (ADR-0007 / T-0028).
# 단일 소스 = 이 인라인 상수. 루트 pm_playbook.local.md(T-0027)는 manifest 밖이라 템플릿
# 트리에 안 끼어 *복사로 안 따라온다* → pm_import 가 import 시 직접 *생성*한다. 별도 `_template`
# 파일을 두지 않는 이유: 그 파일 자체가 manifest/복사 경로에 다시 얽혀 .local 분리 취지와
# 충돌한다 — stdlib-only 인라인 상수가 가장 단순·일관(루트 스텁 형식과 정합: 프런트매터
# type: playbook-local + 인스턴스 소유·manifest 밖 안내 + [[pm_playbook]] 역참조 + TODO 절).
PM_PLAYBOOK_LOCAL_STUB = """\
---
title: PM Playbook — 프로젝트별 누적 학습 (instance)
type: playbook-local
---

# PM Playbook — 프로젝트 누적 학습·도메인 사례

> [[pm_playbook]] (엔진 · `pm_update` 가 자동 갱신하는 **순수 방법론**)의 **프로젝트별 칸**.
> 이 파일은 **인스턴스 소유** — 프레임워크 갱신이 안 건드린다(manifest 밖·tracked).
> 이 프로젝트의 **누적 wave 학습·도메인 사례**를 여기 적는다 (방법론 일반론은 [[pm_playbook]]).

## 누적 wave 학습 (이 프로젝트 고유)

<!-- TODO: 이 프로젝트에서 정착한 wave 운영 학습·도메인 특수 패턴을 누적한다.
  실시간 학습 trail 은 log/current.md entry 가 매체 — 여기엔 *정착된* 패턴만 흡수. -->

## 도메인 사례

<!-- TODO: 이 프로젝트 도메인에 특화된 ticket/wave 사례. 없으면 절 삭제. -->
"""

# 스텁 대상 경로 (dest_root 기준 relpath). 루트 seam(T-0027)과 동일 위치.
PM_PLAYBOOK_LOCAL_RELPATH = Path(".project_manager") / "wiki" / "pm_playbook.local.md"


# ── git-safe 판정 (LLM 아님·결정적 · T-0034) ──────────────────────────────
# --into 백업 노이즈를 줄이려, git 이 *추적 중이고 미변경*인 파일은 백업 없이 덮는다(git 이
# 내용을 갖고 있어 복원 가능). 그 외(미추적·dirty·비-git)만 중앙 디렉토리에 백업한다.
# git 호출은 LLM 아님 — git_runner 주입으로 테스트 결정적(_real_models_runner 류 seam 철학).


def _real_git_runner(dest_root: Path) -> GitRunner:
    """실 git 을 dest_root 컨텍스트로 호출하는 GitRunner 를 만든다(fail-soft).

    반환 callable: argv(list) → (returncode, stdout). git 바이너리 부재(shutil.which) 또는
    어떤 예외든 (1, "") 로 감싼다 — git_safe_relpaths 가 이를 None(전부 백업) 폴백으로 본다.
    `git -C <dest> <argv...>` 형태로 항상 dest_root work tree 에 묶는다(_real_models_runner 선례).
    """
    git_binary = shutil.which("git")

    def runner(argv: list) -> tuple:
        if git_binary is None:
            return 1, ""
        try:
            result = subprocess.run(
                [git_binary, "-C", str(dest_root), *argv],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=GIT_SAFE_TIMEOUT_SECONDS,
            )
            return result.returncode, result.stdout or ""
        except Exception:  # noqa: BLE001 — fail-soft: 어떤 예외도 import 를 깨지 않는다.
            return 1, ""

    return runner


# ── upstream URL 안전 계약 + self-describing 분류 (T-0145·ADR-0032 D4) ──────────
# upstream 값은 git URL *또는* 로컬 경로다 — self-describing(모양으로 분기). git 을
# 호출하는 모든 경로(ls-remote·remote get-url·rev-parse)가 이 계약을 지킨다:
#   - argv-list(no shell·_real_git_runner 가 항상 list 전달) · leading-dash 거부(옵션 오인)
#   - protocol allowlist(https/ssh/file 명시) · credential-in-URL 거부(SSRF·자격증명 누출)
#   - scp-style(user@host:path)/Windows(C:\)/상대/모호 colon 분기로 URL↔경로 정확 판별
# 비대화 auth(GIT_TERMINAL_PROMPT=0)·timeout 은 git 호출 runner(_real_upstream_git_runner)가
# 강제한다. 이 검증 자체는 *순수 함수*(네트워크 0) — 도달성은 ls-remote 호출부가 따로 본다.

# URL scheme allowlist — https/ssh/file *만*(ADR-0032 D4 명시). http(평문)·git://(비인증
# 평문·MITM 취약)·ftp·ext::<cmd>(임의명령)·임의 transport 는 전부 거부(SSRF·중간자·원격 코드
# 실행 회피). git:// 는 ssh 위 전송이 아니라 *비인증 평문*이라 allowlist 에서 뺀다(codex MF2).
_UPSTREAM_URL_SCHEMES = ("https://", "ssh://", "file://")

# scp-style URL(`user@host:path`·`host:path`) 판별 — colon 앞에 슬래시가 없고(경로 아님)
# Windows 드라이브(`C:`)가 아니어야 한다. git 의 scp 문법은 이 모양을 SSH 로 해석한다.
_SCP_LIKE_RE = re.compile(r"^[^/\\:]+@[^/\\:]+:.+$|^[A-Za-z][A-Za-z0-9_.-]*:.+$")

# Windows 드라이브 경로(`C:\...`·`C:/...`) — 단일 알파벳 + colon + 슬래시. scp-style 과
# 구분해 *경로*로 취급(콜론 모호성 해소). 단일문자 호스트의 scp 와 충돌하지 않게 슬래시 요구.
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


def classify_upstream(value: str) -> str:
    """upstream 값을 self-describing 으로 분류한다 — 'url' | 'path' (T-0145·ADR-0032 D4).

    스킬층이 freshness 분기(URL→clone/fetch · 경로→pull)에 쓰는 것과 *동일 규칙*을 엔진이
    공유한다(값 모양만 본다·네트워크 0). 판정 순서:
      1. 허용 scheme(`https://`·`ssh://`·`file://`) prefix → 'url'.
      2. Windows 드라이브(`C:\\`·`C:/`) → 'path'(콜론 모호성 우선 해소).
      3. scp-style(`user@host:path`·`host:path`) → 'url'(git SSH 문법).
      4. 그 외 → 'path'(상대/절대 로컬 경로).
    빈 문자열은 'path'(호출부 검증이 별도로 거른다 — 여기선 모양 분류만). scheme allowlist
    밖(git://·http:// 등)은 prefix 매칭이 안 돼 scp-style 분기로 빠질 수 있으나, 안전 거부는
    `validate_upstream_value`(`://` allowlist-밖 명시 reject)가 전담한다(분류 ≠ 허가).
    """
    if any(value.startswith(s) for s in _UPSTREAM_URL_SCHEMES):
        return "url"
    if _WINDOWS_DRIVE_RE.match(value):
        return "path"
    if _SCP_LIKE_RE.match(value):
        return "url"
    return "path"


def validate_upstream_value(value: str) -> tuple[bool, str]:
    """upstream 값의 *순수* 안전 검증 (T-0145·네트워크 0·도달성은 별도). (ok, reason).

    git 을 호출하기 *전* 입구 가드 — fail-closed(나쁜 값은 거부, silently 기록 금지·T-0078
    동형). 검사:
      - 빈/공백 거부.
      - leading-dash 거부 — `--upload-pack=...` 류 옵션 오인(argv 첫 위치라도 안전).
      - URL 이면 scheme allowlist(https/ssh/file·git://·http:// 거부) 강제 + credential-in-URL
        (`user:pass@`) 거부(자격증명 누출·SSRF) + **authority(host[:port]) leading-dash 거부**
        — `ssh://-oProxyCommand=...`·`ssh://git@-oProxyCommand=...` 류 ssh 옵션 주입 차단
        (codex MF3·defense-in-depth·git 자체 방어에만 의존하지 않음). scp-style(`user@host:
        path`)의 user 부는 허용(비밀 아님)이나 `user:pass@host` 의 password·transport helper
        (`ext::cmd`)는 거부.
    도달성(ls-remote)·경로 존재는 호출부(검증 후속)가 본다 — 여기선 *형태 안전*만.
    """
    if not value or not value.strip():
        return False, "upstream 값이 비어 있다."
    if value.startswith("-"):
        return False, f"upstream 값이 '-' 로 시작한다(옵션 오인·argv 안전): {value!r}"
    # scheme-form(`X://...`)인데 allowlist 밖이면 명시 거부 — http(평문)·git://(비인증 평문·MITM)·
    # ftp·ext::cmd(임의명령)·임의 transport 차단(SSRF·중간자·원격 코드 실행). scp-style(콜론만·
    # `://` 없음)은 아래에서 분류.
    if "://" in value and not value.startswith(_UPSTREAM_URL_SCHEMES):
        scheme = value.split("://", 1)[0]
        return False, (
            f"upstream URL scheme {scheme!r} 비허용 — 허용: https/ssh/file "
            "(http·git:// 평문·ftp·ext::cmd 등 거부·SSRF/중간자/MITM 방지)."
        )
    kind = classify_upstream(value)
    if kind == "url":
        if value.startswith(_UPSTREAM_URL_SCHEMES) and not value.startswith("file://"):
            # scheme-form URL — authority(userinfo@host[:port]) 부를 분리해 검사.
            after_scheme = value.split("://", 1)[1] if "://" in value else value
            authority = after_scheme.split("/", 1)[0]
            userinfo = authority.split("@", 1)[0] if "@" in authority else ""
            host = authority.split("@", 1)[1] if "@" in authority else authority
            # credential-in-URL(`user:pass@`) 거부(자격증명 누출).
            if userinfo and ":" in userinfo:
                return False, (
                    "upstream URL 에 자격증명(user:pass@)이 박혀 있다 — 거부 "
                    "(누출 위험·credential helper/SSH 키를 쓰라)."
                )
            # MF3: host(또는 userinfo)가 `-` 로 시작하면 ssh 옵션 주입(`-oProxyCommand=...`)으로
            # 해석될 수 있다 — defense-in-depth 로 거부(git 자체 방어에만 의존하지 않음).
            if host.startswith("-") or userinfo.startswith("-"):
                return False, (
                    f"upstream URL 의 host/userinfo 가 '-' 로 시작한다 — 거부 "
                    f"(ssh 옵션 주입·-oProxyCommand 류 차단): {value!r}"
                )
        elif not value.startswith("file://"):
            # scp-style(`user@host:path`) — git transport helper(`ext::cmd`·`fd::N` 등 double-
            # colon)는 임의명령 실행이라 거부(`://` 없이 `::` 가 있는 형태). 정상 scp 는 single
            # colon(`host:path`)뿐이다.
            if "::" in value:
                return False, (
                    f"upstream 값에 git transport helper(`::`)가 있다 — 거부 "
                    f"(ext::cmd 등 임의명령 실행 회피): {value!r}"
                )
            # scp authority 분리 (codex round-2 정정) — git scp 문법은 `[user@]host:path`.
            # **먼저 첫 `:` 로 lhs(`[user@]host`) ↔ path 를 나눈다.** path(첫 `:` 뒤)는 자유 —
            # `@`·`:` 포함 정상(`host:path@v1.git`·`host:sub/dir@ref`)이라 거기서 credential/
            # leading-dash 를 보면 false-reject 한다. authority 해석은 **lhs 안에서만** 한다.
            #
            scp_lhs = value.split(":", 1)[0]
            if "@" in scp_lhs:
                scp_userinfo, scp_host = scp_lhs.split("@", 1)
            else:
                scp_userinfo, scp_host = "", scp_lhs
            # `user:pass@host` 형태의 password 박힘 거부(자격증명 누출) — **lhs 의 userinfo 한정**.
            # scp 문법은 password 를 지원하지 않으므로(그건 scheme URL `https://user:pass@` 형식)
            # path 의 `:`·`@`(첫 `:` 뒤)는 자유다 — `host:path@with:colon`·`user:pass@host:path`
            # (git 은 host=`user`·path=`pass@host:path` 로 본다)는 정상 scp 로 통과한다(codex 알고리즘:
            # authority 해석은 lhs[첫 `:` 앞] 안에서만). 진짜 credential 박힘은 scheme-form 에서 거부.
            if ":" in scp_userinfo:
                return False, (
                    "upstream URL 에 자격증명(user:pass@)이 박혀 있다 — 거부 "
                    "(누출 위험·SSH 키를 쓰라)."
                )
            # MF3: scp host/userinfo leading-dash 거부(ssh 옵션 주입 차단·defense-in-depth).
            if scp_host.startswith("-") or scp_userinfo.startswith("-"):
                return False, (
                    f"upstream URL 의 host/userinfo 가 '-' 로 시작한다 — 거부 "
                    f"(ssh 옵션 주입 차단): {value!r}"
                )
        return True, ""
    # 경로 — 형태 안전(leading-dash 는 위에서 이미 거름). 존재 검증은 호출부.
    return True, ""


def _real_upstream_git_runner() -> GitRunner:
    """upstream git 호출(ls-remote·remote get-url·rev-parse)용 GitRunner (T-0145·fail-soft).

    `_real_git_runner` 와 달리 `-C <dest>` 로 고정하지 않는다 — 호출부가 `-C <checkout>`·
    `ls-remote <url>` 등 컨텍스트를 argv 로 직접 준다. URL 안전 계약 강제:
      - argv-list(no shell) — subprocess 가 리스트로 받아 셸 해석 0.
      - `GIT_TERMINAL_PROMPT=0` — 비대화 auth(자격증명 프롬프트로 멈추지 않게·CI 안전).
      - **config 격리(MF4)** — `GIT_CONFIG_GLOBAL/SYSTEM=os.devnull` 로 global·system config 를
        무력화(사용자 `insteadOf` rewrite·credential helper 가 untrusted URL 호출에 끼는 것 차단)
        + `GIT_CONFIG_COUNT` 패턴으로 protocol allowlist(https/ssh/file 만)·credential.helper=
        (빈값) 강제. `_real_git_runner`(로컬 추적집합 조회)와 달리 *네트워크-facing* 이라 격리한다.
      - timeout — ls-remote 는 네트워크라 GIT_TIMEOUT 류 상한.
    git 바이너리 부재·예외는 (1, stderr) 로 감싼다(호출부가 rc 로 판정).
    """
    git_binary = shutil.which("git")

    def runner(argv: list) -> tuple:
        if git_binary is None:
            return 1, "git 바이너리를 찾을 수 없음 (PATH)."
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"  # 비대화 auth — 자격증명 프롬프트로 멈추지 않는다.
        # MF4: global/system git config 무력화 — insteadOf rewrite·credential helper 가
        # untrusted URL 의 ls-remote/rev-parse 에 끼는 것을 막는다(os.devnull = Win/POSIX 공통).
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_SYSTEM"] = os.devnull
        # hardening(codex): 상속된 protocol 우회 env 제거(방어층) — GIT_ALLOW_PROTOCOL 은
        # protocol.allow allowlist 를 우회하고, GIT_PROTOCOL_FROM_USER 는 user-given protocol
        # 게이트를 푼다. 둘 다 pop 해 우리 GIT_CONFIG allowlist 가 단일 권위가 되게 한다.
        env.pop("GIT_ALLOW_PROTOCOL", None)
        env.pop("GIT_PROTOCOL_FROM_USER", None)
        # GIT_CONFIG_COUNT 패턴으로 protocol allowlist·credential.helper=(빈값) 주입(`-c` 동치·
        # worktree_pool 선례). 기존 env 의 GIT_CONFIG_* 잔여는 우리 카운트로 덮어 결정론 보장
        # (env 격리는 sub-process[ssh transport]까지 전파돼 argv `-c` 보다 안전 — env 단일 채널).
        for idx, (key, val) in enumerate(_UPSTREAM_GIT_CONFIG_KV):
            env[f"GIT_CONFIG_KEY_{idx}"] = key
            env[f"GIT_CONFIG_VALUE_{idx}"] = val
        env["GIT_CONFIG_COUNT"] = str(len(_UPSTREAM_GIT_CONFIG_KV))
        try:
            result = subprocess.run(
                [git_binary, *argv],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=UPSTREAM_GIT_TIMEOUT_SECONDS,
                env=env,
            )
            return result.returncode, (result.stdout or "") + (result.stderr or "")
        except Exception as exc:  # noqa: BLE001 — fail-soft: rc!=0 로 호출부 위임.
            return 1, str(exc)

    return runner


def derive_origin_url(checkout_root: Path, *, git_runner: GitRunner | None = None) -> str | None:
    """로컬 git checkout 의 `git remote get-url origin` 을 읽어 URL 을 도출한다 (T-0145).

    로컬 clone 을 `--from` 으로 받았을 때, future update 기록(`--upstream` 생략 시)을 *그
    checkout 경로* 대신 origin URL 로 자동도출하는 데 쓴다(릴리스 추적 기본·ADR-0032 D4).
    rev-parse 와 동일 안전 계약(argv-list·timeout·GIT_TERMINAL_PROMPT=0)을 `_real_upstream_
    git_runner` 가 강제한다. git repo 아님·origin 부재·도출 URL 이 검증 실패 → None(graceful·
    호출부가 경로 fallback). 도출 URL 도 `validate_upstream_value` 로 fail-closed 검증.
    """
    runner = git_runner if git_runner is not None else _real_upstream_git_runner()
    rc, out = runner(["-C", str(checkout_root), "remote", "get-url", "origin"])
    if rc != 0:
        return None
    url = out.strip().splitlines()[0].strip() if out.strip() else ""
    if not url:
        return None
    ok, _reason = validate_upstream_value(url)
    if not ok:
        return None
    return url


def read_upstream_rev(checkout_root: Path, *, git_runner: GitRunner | None = None) -> str | None:
    """로컬 git checkout 의 `git rev-parse HEAD` 를 읽는다 — drift baseline (T-0145·T-0141 입력).

    `upstream_rev=<commit>` baseline 기록의 입력이다(ADR-0032 D2). checkout_root 가 가리키는
    로컬 git work tree 의 현재 HEAD commit 을 읽는다 — git repo 아님·HEAD 해소 실패는 None
    (graceful·기록 생략). URL upstream(로컬 checkout 없음)은 baseline 을 못 읽으므로 호출부가
    경로 upstream 에 한해 호출한다(스킬층이 URL 의 seen-rev 를 별도 기록·`upstream_seen_rev`).
    안전 계약은 `_real_upstream_git_runner`(argv-list·timeout·GIT_TERMINAL_PROMPT=0).
    """
    runner = git_runner if git_runner is not None else _real_upstream_git_runner()
    rc, out = runner(["-C", str(checkout_root), "rev-parse", "HEAD"])
    if rc != 0:
        return None
    rev = out.strip().splitlines()[0].strip() if out.strip() else ""
    return rev or None


def _parse_status_dirty(porcelain_z: str) -> set:
    """`git status --porcelain -z` 출력 → dirty·untracked relpath(posix) 집합.

    NUL(`\\0`) 구분. 각 엔트리는 `XY <path>`(2-char 상태 + 공백 + 경로). rename(R/C)은
    `<new>\\0<old>` 로 *두 NUL 필드*를 쓰므로(상태 2칸이 R/C 로 시작), old-path 필드를 건너뛴다.
    untracked(`??`)·수정·staged 전부 "git-safe 아님"으로 본다(보수적 — 중앙 백업 대상).
    """
    dirty: set = set()
    parts = porcelain_z.split("\0")
    i = 0
    while i < len(parts):
        entry = parts[i]
        if not entry:
            i += 1
            continue
        # 엔트리 형식: 2-char XY 상태 + 공백 + 경로. 경로 추출(상태 3칸 이후).
        status = entry[:2]
        path = entry[3:] if len(entry) > 3 else ""
        if path:
            dirty.add(path)
        # rename/copy 는 다음 필드가 old-path — 건너뛴다(경로로 오해 방지).
        if status and status[0] in ("R", "C"):
            i += 2
        else:
            i += 1
    return dirty


def git_safe_relpaths(
    dest_root: Path,
    *,
    git_runner: GitRunner | None = None,
) -> set | None:
    """dest_root 가 git work tree 면 '추적 중 & 미변경' relpath(posix) 집합 반환.

    git 아님 / 바이너리 부재 / 오류 → None (= 전부 백업·보수적 fail-soft·안전).

    구현: `git rev-parse --is-inside-work-tree`(work tree 판별) → `ls-files -z`(추적 집합)
    − `status --porcelain -z`(dirty·untracked). 차집합 = 추적&미변경. git_runner 주입으로
    테스트 결정적(`shutil.which("opencode")`·`_real_models_runner` 동일 seam 철학·T-0033).

    ⚠️ 경로 기준 정규화(codex T-0034 must-fix): `git -C <dest> ls-files` 는 **cwd(=dest_root) 상대**,
    `status --porcelain` 은 **repo-root 상대** 경로를 낸다. dest_root 가 repo 루트가 아닌 *하위
    디렉토리*면 두 기준이 달라 dirty 가 `tracked − dirty` 에서 안 빠진다 → dirty 를 git-safe 로
    오판해 무백업 덮을 위험. `rev-parse --show-prefix` 로 dirty(repo-root 상대)를 dest_root 상대로
    환산(prefix 하위만·prefix 제거)해 tracked(dest_root 상대)와 기준을 맞춘다. 반환 relpath 는
    plan_copy 의 `rel.as_posix()` 비교 기준(dest_root 상대 posix)과 일치한다.
    """
    runner = git_runner if git_runner is not None else _real_git_runner(dest_root)

    rc, out = runner(["rev-parse", "--is-inside-work-tree"])
    if rc != 0 or out.strip() != "true":
        return None  # git work tree 아님(또는 호출 실패) — 전부 백업.

    rc_pref, prefix_out = runner(["rev-parse", "--show-prefix"])
    if rc_pref != 0:
        return None  # prefix 조회 실패 — 기준 정규화 불가 → 보수적으로 전부 백업.
    prefix = prefix_out.strip()  # 하위 디렉토리면 'sub/dir/'(posix·trailing slash), repo 루트면 ''.

    rc_tracked, tracked_out = runner(["ls-files", "-z"])
    if rc_tracked != 0:
        return None  # 추적 집합 조회 실패 — 보수적으로 전부 백업.
    tracked = {p for p in tracked_out.split("\0") if p}  # cwd(=dest_root) 상대.

    rc_status, status_out = runner(["status", "--porcelain", "-z"])
    if rc_status != 0:
        return None
    dirty_repo = _parse_status_dirty(status_out)  # repo-root 상대.
    # repo-root 상대 dirty → dest_root 상대(prefix 하위만 남기고 prefix 제거). prefix='' 면 그대로.
    dirty = ({p[len(prefix):] for p in dirty_repo if p.startswith(prefix)}
             if prefix else dirty_repo)

    # 추적 중 & 미변경 = 추적 집합 − dirty/untracked. git 이 이 내용을 복원할 수 있다.
    return tracked - dirty


# ── plan 액션 ──────────────────────────────────────────────────────────────
# 결정적 단계(복사·백업·치환)를 액션 리스트로 모은 뒤 apply 에서 실행한다.
# plan/apply 분리 = dry-run 결정론(파일시스템 미변경) + 테스트 용이성 (pm_update 패턴).

class CopyAction:
    """src 파일을 dst 로 복사. 기존 dst 가 있으면 중앙 디렉토리에 백업 후 덮음(--into 비파괴).

    backup (T-0034):
      - None  = 백업 안 함 — 신규 파일이거나 git-safe(추적&미변경, git 이 복원 가능).
      - Path  = `<dest>/.pm_import_backups/<DATE>/<relpath>` (중앙화·relpath 미러링).
                대상 디렉토리는 run() 이 mkdir(parents) 로 만든다.
    """

    def __init__(self, src: Path, dst: Path, backup: Path | None):
        self.src = src
        self.dst = dst
        self.backup = backup  # None = 백업 안 함(신규 또는 git-safe)

    def describe(self) -> str:
        rel = self.dst
        # lite 배치(T-0010): src 가 `X.lite.md` 인데 dst 가 `X.md` 면 이름이 치환됐다 —
        #   어느 변종이 어디로 가는지 보이게 한다("CLAUDE.lite.md → CLAUDE.md (lite)").
        lite_note = ""
        if self.src.name.endswith(LITE_SUFFIX) and self.src.name != self.dst.name:
            lite_note = f"  ({self.src.name} → {self.dst.name}, lite)"
        # T-0034 3분기: 신규([copy]) · 충돌 git-safe(백업 생략 [copy · git-safe]) ·
        #   충돌 비-safe([backup+copy] → 중앙 디렉토리 상대경로 `.pm_import_backups/<DATE>/<rel>`).
        if self.backup is not None:
            tail = self.backup.as_posix()
            idx = tail.find(BACKUP_DIR_NAME)
            shown = tail[idx:] if idx != -1 else self.backup.name
            return f"  [backup+copy] {rel}  (→ {shown}){lite_note}"
        if self._git_safe_skip:
            return f"  [copy · git-safe] {rel}{lite_note}"
        return f"  [copy] {rel}{lite_note}"

    # describe() 가 신규([copy])와 git-safe skip([copy · git-safe])을 구분하려면 충돌 여부를
    # 알아야 한다 — plan_copy 가 충돌&git-safe 인 액션에 표시한다(기본 False = 신규).
    _git_safe_skip: bool = False

    def run(self) -> None:
        # MF1: 기존 dst 가 symlink 이면 shutil.copy2 가 링크를 *따라가* 링크 대상(프로젝트
        #      밖일 수 있음) 파일을 백업/덮어쓴다 — 비파괴 계약 위반 + 외부 파일 변조 위험.
        #      따라서 symlink 는 *링크 자체*를 처리한다: 링크를 그대로 백업(follow 안 함) →
        #      링크 unlink → 일반 파일로 src 복사. 링크 대상 파일은 절대 건드리지 않는다.
        dst_is_symlink = self.dst.is_symlink()
        if self.backup is not None and (self.dst.exists() or dst_is_symlink):
            # SF1: 백업 경로가 이미 존재하면(같은 날 재실행 등) 덮지 말고 순번 부여 —
            #      가장 오래된 원본(=진짜 사용자 파일)이 살아남게 한다.
            target = _free_backup_path(self.backup)
            # T-0034: 백업이 중앙 디렉토리(relpath 미러)이므로 부모 디렉토리를 먼저 만든다.
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.dst, target, follow_symlinks=False)
        if dst_is_symlink:
            # 링크 자체를 제거(대상 파일 불변) — 이후 일반 파일로 덮어쓴다.
            os.unlink(self.dst)
        self.dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.src, self.dst)


class FileVsDirConflict(Exception):
    """SF(codex 4차 suggestion): dst 위치에 기존 *디렉토리* 가 있어 파일 복사가 불가능.

    src 는 파일인데 dst 가 디렉토리면 shutil.copy2 가 IsADirectoryError 로 터지고, 백업도
    안 된다(디렉토리는 copy2 대상 아님). 비파괴 계약상 사용자 디렉토리를 자동 삭제할 수 없으니
    plan 단계에서 명시적으로 거부한다(apply 부분 복사 전 차단).
    """


class AncestorConflict(Exception):
    """MF(codex 5차): dst 의 *조상* 경로(dest_root 하위)에 symlink·비-디렉토리 파일이 있어
    안전하게 디렉토리를 만들 수 없다.

    위험 둘:
      - 조상이 symlink(프로젝트 밖 가리킴)면 `dst.parent.mkdir(exist_ok=True)`+`shutil.copy2`
        가 링크를 따라가 **프로젝트 밖**에 쓴다 — 비파괴 위반.
      - 조상이 일반 파일이면 plan 은 통과한 뒤 apply 중 `mkdir` 가 터져 **부분 복사** 잔존.
    CopyAction.run 의 dst-자체 symlink 처리(MF1)는 조상은 못 막으므로 plan 단계에서 조상
    컴포넌트를 따로 거부한다(dry-run·apply 모두 안전). dest_root 자신은 --into/--new 가드가
    처리하므로 그 *하위* 조상에만 집중한다.
    """


def _free_backup_path(backup: Path) -> Path:
    """backup 경로가 비었으면 그대로, 점유됐으면 .1·.2… 순번을 붙여 빈 경로 반환."""
    if not backup.exists():
        return backup
    n = 1
    while True:
        candidate = backup.with_name(f"{backup.name}.{n}")
        if not candidate.exists():
            return candidate
        n += 1


# lite 진입 파일 규약 (T-0010): `X.lite.md` 는 진입 `X.md` 의 lite 변종이다.
# (예: CLAUDE.lite.md → CLAUDE.md, AGENTS.lite.md → AGENTS.md.) 임의의 `*.lite.md` 에 일반화.
LITE_SUFFIX = ".lite.md"


def _full_relpath_for_lite(rel: Path) -> Path:
    """lite 변종 relpath `X.lite.md` → full 진입 relpath `X.md` (이름 치환).

    `X.lite.md` 만 매핑한다(이름이 정확히 `.lite.md` 로 끝나는 경우). 반환 relpath 는
    같은 디렉토리의 `X.md` 다 — lite 배치 시 dst 가 full 진입 이름으로 들어가게 한다.
    """
    base = rel.name[: -len(LITE_SUFFIX)]  # 'X.lite.md' → 'X'
    return rel.with_name(base + ".md")


def _iter_source_files(template_root: Path, weight: str = "full"):
    """template_root 하위 파일을 (dst relpath, 절대경로)로. node_modules 등 제외.

    weight 규약 (T-0010 — `*.lite.md` = `*.md` 의 lite 변종):
      - full(기본): 모든 `*.lite.md` 를 복사 대상에서 *제외*한다(lite 변종이 full 배포에
        끼면 안 됨). full `X.md` 는 그대로 복사.
      - lite: 각 `X.lite.md` 를 dst relpath `X.md` 로 복사(이름 치환). 동시에 (a) 같은
        트리의 full `X.md` 는 복사 제외(lite 가 그 자리를 차지), (b) 원본 이름 `X.lite.md`
        도 그대로는 복사 제외(dst 에 `*.lite.md` 잔존 금지).
    yield 하는 relpath 는 *dst* 기준이므로 lite 모드에선 `X.md` 로 치환돼 나간다 —
    placeholder 치환(copied_relpaths 기준)·both 충돌 판정이 이 dst relpath 위에서 돈다.
    """
    files = [
        path
        for path in sorted(template_root.rglob("*"))
        if path.is_file()
        and not any(
            part in COPY_EXCLUDE_DIR_NAMES
            for part in path.relative_to(template_root).parts
        )
        # 프레임워크-내부 doc(최상위 README.md 등)은 adopter 로 출하하지 않는다.
        and path.relative_to(template_root).as_posix() not in COPY_EXCLUDE_RELPATHS
    ]
    # lite 모드: 이 트리에서 lite 가 대체할 full 진입 relpath 집합을 먼저 모은다.
    lite_overridden: set[Path] = set()
    if weight == "lite":
        for path in files:
            rel = path.relative_to(template_root)
            if rel.name.endswith(LITE_SUFFIX):
                lite_overridden.add(_full_relpath_for_lite(rel))

    for path in files:
        rel = path.relative_to(template_root)
        is_lite = rel.name.endswith(LITE_SUFFIX)
        if weight == "lite":
            if is_lite:
                # X.lite.md → dst X.md (이름 치환). 원본 lite 이름은 dst 에 안 남는다.
                yield _full_relpath_for_lite(rel), path
                continue
            if rel in lite_overridden:
                # full X.md 는 같은 트리의 lite 변종이 그 자리를 차지하므로 제외.
                continue
            yield rel, path
        else:  # full
            if is_lite:
                # lite 변종은 full 배포에 끼면 안 됨 — 제외.
                continue
            yield rel, path


def _check_ancestor_safe(dest_root: Path, dst: Path, checked: set[Path]) -> None:
    """dest_root 와 dst 사이의 조상 경로 컴포넌트가 안전하게 디렉토리화 가능한지 검증.

    MF(codex 5차): 이미 존재하는 조상 컴포넌트가 symlink 이거나 비-디렉토리 파일이면
    AncestorConflict 로 거부한다(plan 단계 — apply 부분 복사·외부 쓰기 전 차단). dest_root
    자신은 상위 가드가 처리하므로 *하위* 조상만 본다. checked 는 이미 검증한 조상 캐시
    (같은 디렉토리를 매 파일마다 재검사하지 않게 — 결정론·성능).
    """
    for ancestor in reversed(dst.parents):
        # dest_root 자신과 그 상위는 --into/--new 가드 소관 — 하위 조상만 검사.
        if ancestor == dest_root or dest_root not in ancestor.parents:
            continue
        if ancestor in checked:
            continue
        if ancestor.is_symlink():
            raise AncestorConflict(
                f"dst 조상 경로가 symlink 입니다: {ancestor}. 링크를 따라가면 프로젝트 밖에 "
                f"쓸 수 있어 거부합니다(비파괴). 해당 링크를 직접 옮기거나 제거한 뒤 다시 "
                f"시도하세요."
            )
        if ancestor.exists() and not ancestor.is_dir():
            raise AncestorConflict(
                f"dst 조상 경로에 디렉토리가 아닌 파일이 있습니다: {ancestor}. 그 안에 "
                f"디렉토리를 만들 수 없어 거부합니다(부분 복사 방지). 해당 파일을 직접 "
                f"옮기거나 제거한 뒤 다시 시도하세요(비파괴 — 자동 삭제하지 않습니다)."
            )
        checked.add(ancestor)


def plan_copy(
    template_roots: list[Path],
    dest_root: Path,
    backup_root: Path | None,
    weight: str = "full",
    *,
    git_safe: set | None = None,
) -> list[CopyAction]:
    """어댑터 트리들 → dest 복사 액션. both 면 여러 트리 병합(relpath 유일하면 충돌 0).

    backup_root (T-0034): None 이면 백업 안 함(--new — 빈 디렉토리 보장). 비-None 이면 기존 충돌
    파일을 *중앙 디렉토리* `backup_root/<relpath>` 로 백업(--into). 형제 `*.backup.<DATE>`(트리
    전역 분산) 대신 단일 디렉토리로 모은다.

    git_safe (T-0034): git_safe_relpaths 의 반환 — '추적 중 & 미변경' relpath(posix) 집합 또는
    None. None 이면 git 판정 불가(비-git·오류) → 모든 충돌을 백업(보수적). 집합이면 그 안의
    relpath 는 git 이 복원 가능하므로 백업 없이 덮는다(git-safe skip — 액션 _git_safe_skip 표시).

    weight (T-0010): 'full'(기본) 이면 `*.lite.md` 를 제외, 'lite' 면 `X.lite.md` 를
    dst `X.md` 로 rename 복사(같은 트리 full `X.md` 제외). _iter_source_files 가 이 규약을
    적용해 dst relpath 를 산출하므로, 아래 both 중복 판정·치환 범위는 모두 *dst relpath*
    위에서 일관되게 돈다(lite 모드에선 `X.md` 가 dst — both 양 트리가 각자 lite 변종을 깐다).

    MF3: both 에서 같은 relpath 가 두 트리에 모두 존재하면(예: 공유 엔진), **내용이 같을
    때만** 조용히 skip 한다. 내용이 *다르면*(예: engine.manifest·README.md — claude_code 는
    .claude/agents·skills·regression.yml 을 sync 범위에 포함, opencode 는 제외) 첫 트리
    (template_roots 순서상 claude_code = 상위집합 어댑터)를 우선하되 stderr 경고를 남긴다.
    조용한 정책 손실 금지. (lite 진입 CLAUDE.md / AGENTS.md 는 트리별로 dst relpath 가
    달라 — claude→CLAUDE.md, opencode→AGENTS.md — 충돌하지 않는다.)
    """
    seen: dict[Path, tuple[Path, str]] = {}  # relpath → (채택된 src, 채택 트리명)
    actions: list[CopyAction] = []
    checked_ancestors: set[Path] = set()  # 검증 완료 조상 캐시(중복 검사 회피).
    for template_root in template_roots:
        for rel, src in _iter_source_files(template_root, weight):
            if rel in seen:
                prev_src, prev_tree = seen[rel]
                if _same_bytes(prev_src, src):
                    # 공유 엔진 등 byte-identical — 한 번만 복사(정상).
                    continue
                # 내용이 다른 중복 — 첫 트리(우선) 채택을 명시적으로 경고.
                print(
                    f"경고: both 중복 relpath 내용 불일치 — '{rel.as_posix()}' 는 "
                    f"'{prev_tree}'(우선) 것으로 정함. 무시된 트리: '{template_root.name}'.",
                    file=sys.stderr,
                )
                continue
            seen[rel] = (src, template_root.name)
            dst = dest_root / rel
            # MF(codex 5차): dst 조상(dest_root 하위)이 symlink·비-디렉토리 파일이면 거부 —
            #      링크 follow 로 프로젝트 밖 쓰기 / apply 중 mkdir 실패 부분복사 방지.
            _check_ancestor_safe(dest_root, dst, checked_ancestors)
            # SF(codex 4차): dst 가 (symlink 아닌) 디렉토리면 파일 복사·백업 불가 — plan 에서
            #      거부(apply 부분 복사 전 차단). symlink 는 run() 이 링크 자체로 처리하므로 제외.
            if dst.is_dir() and not dst.is_symlink():
                raise FileVsDirConflict(
                    f"dst 위치에 기존 디렉토리가 있어 파일을 쓸 수 없습니다: {dst}. "
                    f"충돌하는 디렉토리를 직접 옮기거나 제거한 뒤 다시 시도하세요 "
                    f"(비파괴 — 사용자 디렉토리를 자동 삭제하지 않습니다)."
                )
            backup: Path | None = None
            git_safe_skip = False
            # MF1: symlink 도 충돌이다(깨진 링크면 dst.exists() 가 False 라 별도 검사). 충돌
            #      symlink 는 run() 이 링크 자체를 백업하고 일반 파일로 교체한다(대상 불변).
            is_conflict = dst.exists() or dst.is_symlink()
            if backup_root is not None and is_conflict:
                # T-0034: git 이 추적 중이고 미변경인 파일은 git 이 복원 가능 → 백업 생략(덮기만).
                #   그 외(미추적·dirty·비-git·git 판정불가=git_safe None)는 중앙 디렉토리에 백업.
                #   ⚠️ symlink 충돌은 git_safe 와 무관하게 항상 백업한다 — ls-files 가 symlink 를
                #   추적 중이어도 run() 이 백업하는 것은 *링크 자체*(대상 파일 복제 아님)이고,
                #   git-safe skip 으로 무백업 덮으면 사용자 symlink 구성이 무흔적 손실되기 때문.
                if (git_safe is not None
                        and not dst.is_symlink()
                        and rel.as_posix() in git_safe):
                    git_safe_skip = True  # 백업 None — git 이 복원.
                else:
                    backup = backup_root / rel  # 중앙 디렉토리·relpath 미러링.
                    # MF(codex T-0034): 백업 target 의 *전체* 조상 체인
                    #   (`.pm_import_backups/<DATE>/<rel-parents>`) 이 안전한지 plan 단계 검증 —
                    #   일부 조상이 일반 파일/symlink 면 apply 중 mkdir 실패로 부분 복사가 잔존한다.
                    #   dst 조상 가드와 동일 helper·캐시 재사용(중앙 백업 자리 점유까지 한 번에 포착).
                    _check_ancestor_safe(dest_root, backup, checked_ancestors)
            action = CopyAction(src, dst, backup)
            action._git_safe_skip = git_safe_skip
            actions.append(action)
    return actions


def _same_bytes(a: Path, b: Path) -> bool:
    """두 파일의 바이트 내용이 동일한가 (중복 relpath 충돌 판정용)."""
    try:
        return a.read_bytes() == b.read_bytes()
    except OSError:
        return False


# ── placeholder 치환 ───────────────────────────────────────────────────────

def _substitution_map(project_name: str, project_root: Path, today: str) -> dict[str, str]:
    return {
        "{{PROJECT_NAME}}": project_name,
        "{{PROJECT_TAGLINE}}": DEFAULT_TAGLINE,
        "{{PROJECT_ROOT}}": str(project_root),
        "{{PY}}": _detected_py(),
        "{{TEST_CMD}}": _default_test_cmd(),
        "{{DATE}}": today,
    }


def _is_engine_source(rel: Path) -> bool:
    """엔진 소스 코드(`.project_manager/tools/`)인가 — placeholder 처리에서 전면 제외 대상.

    엔진 도구(.py)는 verbatim canonical 사본이다: 코드는 런타임에 local.conf 에서 project_name·
    py·test_cmd 를 읽지, baked placeholder 를 쓰지 않는다. 그런데 그 *주석·docstring·예시 문자열*
    엔 `{{PROJECT_NAME}}`·`{{OPENCODE_PRO_MODEL}}`·`{{PROJECT_CONSTRAINTS}}` 같은 토큰이 문서로
    등장한다(엔진이 placeholder 메커니즘을 설명하므로). 이 문자열들은 *placeholder 가 아니라
    문서*다 — substitute/fill/token-scan 이 건드리면 (a) 주석이 concrete 값으로 변질 (b) free-form
    토큰에 `<!-- TODO -->` 주입 (c) `{{OPENCODE_PRO_MODEL}}` 이 claude 트리의 모델-해소 게이트를
    오발(_token_present True → resolve active). 따라서 엔진 소스는 placeholder 처리 전 범위에서
    제외해 verbatim 으로 둔다 (D17 의 pm_render/pm_update 가 토큰을 문서화하며 표면화·T-0133).

    rel 은 dest-rel(`​.project_manager/tools/board.py`) 또는 절대/소스 경로(dry-run plan 의
    `action.dst`/`action.src`) 둘 다 올 수 있어 substring 매칭으로 통일한다(`tools/` 트레일링
    슬래시로 `tools_backup` 등 오탐 방지)."""
    p = rel.as_posix()
    return p.startswith(".project_manager/tools/") or "/.project_manager/tools/" in p


def _should_substitute(rel: Path) -> bool:
    """이 파일이 operational placeholder 치환 대상인가."""
    if rel.suffix not in SUBSTITUTE_SUFFIXES:
        return False
    if rel.as_posix() in SED_EXCLUDE_RELPATHS:
        return False
    if _is_engine_source(rel):  # 엔진 소스(.py)는 verbatim — 주석의 토큰-문서는 placeholder 아님
        return False
    return True


def substitute_placeholders(
    dest_root: Path,
    subs: dict[str, str],
    copied_relpaths: set[Path],
) -> int:
    """**이번 run 이 복사한 파일만** 대상으로 operational placeholder 치환. 변경 파일 수 반환.

    apply 단계 전용 — 복사가 끝난 dest 트리를 in-place 수정한다.

    MF1: dest 트리 전체를 rglob 하면 이번 import 가 복사하지 *않은* 기존 사용자 파일까지
    무백업 치환되어 --into 비파괴 계약을 위반한다. 따라서 범위를 copied_relpaths(plan_copy
    가 만든 actions 의 dst relpath)로 엄격히 한정한다. 복사된 파일은 충돌 시 이미 백업됐으므로
    치환해도 안전하고, 복사 안 한 사용자 파일은 절대 건드리지 않는다.
    """
    changed = 0
    for rel in sorted(copied_relpaths):
        if any(part in COPY_EXCLUDE_DIR_NAMES for part in rel.parts):
            continue
        if not _should_substitute(rel):
            continue
        path = dest_root / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        new_text = text
        for token, value in subs.items():
            if token in new_text:
                new_text = new_text.replace(token, value)
        if new_text != text:
            path.write_text(new_text, encoding="utf-8")
            changed += 1
    return changed


# ── render 단계 (T-0131·ADR-0028·ADR-0031) ─────────────────────────────────
# @render manifest path 의 어댑터 파일은 import 도 pm_update 와 같은 render 경로를 탄다 —
# 복사 후 operational 토큰(local.conf — PROJECT_NAME·TEST_CMD 등)을 render_adapter 로 치환한다
# (pm_render 공유). @render 는 T-0133 으로 활성(.claude/agents·skills·.opencode/agents·command).
# free-form value-fill 기계는 ADR-0031 로 제거됨 — free-form 은 canonical home(root doc·
# pm_role.local.md)의 FILL 채널이 전담. substitute(operational) *이후* 에 둬 일관 처리.

def _load_pm_render_module():
    """pm_render 모듈을 같은 tools/ 디렉토리에서 로드 (board.py 로더 패턴 동형·sys.path 무오염).

    실패 시 None → 호출부가 render 단계 skip(검사 대상 0·무동작). render path 가 있는데
    렌더러 부재면 토큰이 잔존하나, 그건 board.py render-leak lint 가 backstop 으로 잡는다."""
    render_py = Path(__file__).resolve().parent / "pm_render.py"
    try:
        spec = importlib.util.spec_from_file_location("pm_render", render_py)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:  # noqa: BLE001 — 로드 실패는 render 단계 skip(무동작).
        return None


def _render_managed_relpaths(dest_root: Path) -> set[str]:
    """복사된 트리의 engine.manifest 에서 `@render` path(repo 기준 relpath·POSIX) 집합.

    pm_update.read_manifest 를 재사용해 `.render` True 항목만 모은다. manifest 부재·로드 실패
    → 빈 set(render 대상 0·무동작). 디렉토리 path 는 하위 어댑터 산출물의 prefix 매칭에 쓴다."""
    pm_update_py = Path(__file__).resolve().parent / "pm_update.py"
    manifest = dest_root / ".project_manager" / "engine.manifest"
    if not manifest.is_file():
        return set()
    try:
        spec = importlib.util.spec_from_file_location("pm_update", pm_update_py)
        if spec is None or spec.loader is None:
            return set()
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return {
            str(e).replace("\\", "/")
            for e in mod.read_manifest(manifest)
            if getattr(e, "render", False)
        }
    except Exception:  # noqa: BLE001 — 로드/파싱 실패는 render 대상 0(무동작).
        return set()


def _is_render_managed(rel_posix: str, managed: set[str]) -> bool:
    """rel_posix 가 @render manifest path(파일 정확일치 OR 디렉토리 prefix) 하위인지."""
    for m in managed:
        if rel_posix == m or rel_posix.startswith(m.rstrip("/") + "/"):
            return True
    return False


def render_managed_files(
    dest_root: Path,
    subs: dict[str, str],
    copied_relpaths: set[Path],
) -> int:
    """이번 run 이 복사한 @render path 파일을 render_adapter 산출물로 다시 쓴다. 변경 수 반환.

    범위 = copied_relpaths(비파괴·substitute_placeholders 와 동일 계약). @render manifest path
    하위 .md 만 대상. operational 은 이번 import 의 subs(이미 substitute 가 리터럴로 박았으므로
    보통 no-op). free-form 은 pm_import 의 FILL 채널이 canonical home 에서 전담하므로 render-overlay
    가 관여하지 않는다(ADR-0030·ADR-0031). 현 트리는 @render 0 → 무동작.

    subs(중괄호 포함 token→value)를 pm_render 의 bare-key operational dict 로 변환해 넘긴다."""
    managed = _render_managed_relpaths(dest_root)
    if not managed:
        return 0
    render_mod = _load_pm_render_module()
    if render_mod is None:
        return 0
    # subs 는 `{{KEY}}`→value — pm_render 는 bare KEY 를 기대하므로 변환.
    operational = {
        token.strip("{}"): value for token, value in subs.items()
    }
    changed = 0
    for rel in sorted(copied_relpaths):
        rel_posix = rel.as_posix()
        if not rel_posix.endswith(".md") or not _is_render_managed(rel_posix, managed):
            continue
        path = dest_root / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rendered = render_mod.render_adapter(text, operational=operational)
        if rendered != text:
            path.write_text(rendered, encoding="utf-8")
            changed += 1
    return changed


# ── board.py init 호출 ─────────────────────────────────────────────────────

def run_board_init(dest_root: Path) -> int:
    """복사된 트리의 board.py init(solo)을 호출 — local.conf·pm_state·pre-push 훅 생성.

    같은 인터프리터(sys.executable)로 호출 — board.py 는 pyyaml 의존이라 venv 보존 필요.
    비대화형(stdin 비-tty)이면 external_review opt-in 은 board.py 가 안전쪽(OFF)으로 건너뛴다.
    stdin=DEVNULL 의 isatty() 가 Windows 서 신뢰불가([[T-0068]] 류 함정)라, env 로
    `PM_NONINTERACTIVE=1` 을 명시 전달해 결정적으로 skip 시킨다 (T-0071·isatty 폴백 보조).
    """
    board = dest_root / ".project_manager" / "tools" / "board.py"
    if not board.exists():
        print(f"경고: board.py 없음 ({board}) — init 건너뜀.", file=sys.stderr)
        return 1
    result = subprocess.run(
        [sys.executable, str(board), "init"],
        cwd=str(dest_root),
        stdin=subprocess.DEVNULL,
        env={**os.environ, "PM_NONINTERACTIVE": "1"},
        encoding="utf-8",
        errors="replace",
    )
    return result.returncode


def _set_conf_keys(text: str, updates: dict[str, str]) -> str:
    """local.conf 텍스트에서 지정 키만 set-or-replace. 나머지 줄·주석은 보존.

    있으면 그 자리에서 `key=value` 로 교체(첫 등장만), 없으면 끝에 추가. stdlib only.
    """
    remaining = dict(updates)
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    for line in lines:
        stripped = line.lstrip()
        if not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                newline = "\n" if line.endswith("\n") else ""
                out.append(f"{key}={remaining.pop(key)}{newline}")
                continue
        out.append(line)
    if remaining:
        if out and not out[-1].endswith("\n"):
            out[-1] = out[-1] + "\n"
        for key, value in updates.items():
            if key in remaining:
                out.append(f"{key}={value}\n")
    return "".join(out)


def sync_local_conf(dest_root: Path, project_name: str) -> bool:
    """board.py init 직후 local.conf 의 operational 해소값을 pm_import 치환값과 일치시킨다.

    board.py init 은 project_name 빈값·test_cmd=`pytest -q` 를 하드코딩하므로(D11 seam
    불완전), 엔진 문서(local.conf 해소)와 CLAUDE.md(sed 치환)가 *다른 값*을 보게 된다.
    project_name·test_cmd·py 3개 키만 키 단위 갱신해 정렬한다. board.py init 이 쓴 다른 키
    (session·prefix·external_review 등)와 주석은 보존. clobber 금지. 파일 변경 시 True.
    """
    local_conf = dest_root / ".project_manager" / "local.conf"
    if not local_conf.is_file():
        print(f"경고: local.conf 없음 ({local_conf}) — operational 값 동기화 건너뜀.",
              file=sys.stderr)
        return False
    text = local_conf.read_text(encoding="utf-8")
    updates = {
        "project_name": project_name,
        "test_cmd": _default_test_cmd(),
        "py": _detected_py(),
    }
    new_text = _set_conf_keys(text, updates)
    if new_text != text:
        local_conf.write_text(new_text, encoding="utf-8")
        return True
    return False


def _parse_conf_keys(text: str) -> dict[str, str]:
    """local.conf 텍스트를 key=value dict 로 파싱(주석·빈 줄 제외). board.local_config 와 동치."""
    conf: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        conf[key.strip()] = value.strip()
    return conf


def backup_existing_local_conf(dest_root: Path, backup_root: Path | None) -> str | None:
    """--into 재-import 전, 기존 local.conf 가 있으면 백업하고 원본 텍스트를 반환한다.

    MF1: board.py init 은 local.conf 를 무조건 write_text 로 덮으므로, 이미 프레임워크를
    쓰던 프로젝트(재-import/업그레이드)면 기존 per-clone 설정(external_review_enabled·
    reviewer_cmd·session·prefix 등)이 무백업 손실된다. local.conf 는 pm_import 의
    copy/backup 대상 트리 밖이라 CopyAction 의 백업 로직을 안 탄다 — init 호출 전 여기서
    명시적으로 백업한다.

    T-0034: 백업은 형제 `*.backup.<DATE>` 가 아니라 중앙 디렉토리
    `backup_root/.project_manager/local.conf` 로 라우팅한다(한 곳 원칙). local.conf 는
    보통 git-ignored(미추적)라 git-safe 아님 — 중앙 백업 유지(내용을 git 이 복원 못 함).
    backup_root=None(--new)이면 빈 디렉토리 보장이라 기존 local.conf 가 없으므로 호출되지 않음.
    중앙 경로 충돌은 _free_backup_path 로 순번 부여(원본 보존). 반환값(원본 텍스트)은
    reapply_preserved_conf_keys 가 사용자 키 재병합에 쓴다. None = 기존 local.conf 없음.
    """
    local_conf = dest_root / ".project_manager" / "local.conf"
    if not local_conf.is_file():
        return None
    original_text = local_conf.read_text(encoding="utf-8")
    if backup_root is None:
        return original_text  # --new 빈 디렉토리 — 보존만(백업 위치 없음·실질 도달 안 함).
    backup = _free_backup_path(backup_root / local_conf.relative_to(dest_root))
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(local_conf, backup)
    rel = backup.relative_to(dest_root).as_posix()
    print(f"✓ 기존 local.conf 백업: {rel}")
    return original_text


def reapply_preserved_conf_keys(dest_root: Path, original_text: str) -> bool:
    """board.py init 이 새로 쓴 local.conf 위에, 기존 파일의 사용자 키를 재병합한다.

    MF1: board.py init 은 local.conf 를 통째로 덮으므로, init 이 *안 쓴* 사용자 키
    (external_review_enabled·reviewer_cmd·prefix 등)는 init 후 사라진다. 따라서 init 산출
    local.conf 에 *없는* 기존 키만 _set_conf_keys 로 다시 얹는다. init 이 쓴 키
    (session·py·test_cmd·project_name·솔로 init 이 채운 prefix 등)는 init/operational sync
    값을 우선해 덮지 않는다. 결과: import 후 local.conf = board init 기본 + operational sync
    + 사용자 기존 설정 보존. 재병합으로 변경 시 True.
    """
    local_conf = dest_root / ".project_manager" / "local.conf"
    if not local_conf.is_file():
        return False
    current_text = local_conf.read_text(encoding="utf-8")
    current_keys = _parse_conf_keys(current_text)
    original_keys = _parse_conf_keys(original_text)
    # board init 이 새로 쓴 local.conf 에 *없는* 기존 사용자 키만 복원(init 값 우선).
    preserved = {
        key: value
        for key, value in original_keys.items()
        if key not in current_keys
    }
    if not preserved:
        return False
    new_text = _set_conf_keys(current_text, preserved)
    if new_text != current_text:
        local_conf.write_text(new_text, encoding="utf-8")
        kept = "·".join(sorted(preserved))
        print(f"✓ 기존 local.conf 사용자 키 보존: {kept}")
        return True
    return False


def record_upstream(dest_root: Path, upstream_value) -> bool:
    """upstream 값(URL 또는 로컬 경로)을 dest local.conf 에 `upstream=` 로 기록한다(T-0053·T-0145).

    `--upstream`(future update 기록·URL 선호)↔`--from`(이번 import 파일 소스) 디커플(T-0145):
    이 함수는 *기록할 upstream 값*을 받아 그대로 박는다. `--upstream` 생략 시 호출부가
    `--from`(=source_root)을 넘겨 **기존 동작(경로 기록)을 회귀 보존**한다 — `Path` 를 받으면
    `str()` 로 직렬화하므로 옛 `record_upstream(dest_root, source_root)` 호출 형태도 그대로 동작.

    이후 pm_update 가 --from 생략 시 이 값을 기본 upstream 으로 쓴다. _set_conf_keys 의 키 단위
    set-or-replace 라 기존 줄이 있으면 *제자리 갱신*, 없으면 끝에 추가한다 — 따라서 재-import(--into)
    는 reapply_preserved_conf_keys 가 백업의 *stale upstream 을 되살려도*(board init 은 upstream 을
    쓰지 않으므로 preserve 가 옛 값을 복원한다) 마지막에 *현재 값* 으로 제자리 확정 갱신된다(stale
    보존 아님). 바로 그 때문에 board init·conf sync·preserve 단계 *이후* 에 호출해야 갱신이 보장된다. 변경 시 True.
    """
    local_conf = dest_root / ".project_manager" / "local.conf"
    if not local_conf.is_file():
        print(f"경고: local.conf 없음 ({local_conf}) — upstream 기록 건너뜀.", file=sys.stderr)
        return False
    text = local_conf.read_text(encoding="utf-8")
    new_text = _set_conf_keys(text, {"upstream": str(upstream_value)})
    if new_text != text:
        local_conf.write_text(new_text, encoding="utf-8")
        return True
    return False


def record_upstream_rev(dest_root: Path, rev: str) -> bool:
    """upstream baseline revision 을 dest local.conf 에 `upstream_rev=<commit>` 로 기록한다(T-0145).

    drift-lint(T-0141)의 baseline 입력 — "마지막 동기 이후 upstream 변경분"을 재는 기준점이다
    (ADR-0032 D2). import 시(이 함수)와 pm_update 매 sync 시 갱신된다. `upstream_seen_rev`(현재
    관찰값·pm-update 스킬 기록·T-0142)는 **별개 키** — 한 키 2역 금지(race/자기비교 회피). rev 가
    빈 값(git repo 아님·HEAD 해소 실패)이면 호출부가 이 함수를 부르지 않는다(기록 생략·graceful).
    _set_conf_keys 키 단위 set-or-replace 라 다른 키·주석 보존. 변경 시 True.
    """
    local_conf = dest_root / ".project_manager" / "local.conf"
    if not local_conf.is_file():
        print(f"경고: local.conf 없음 ({local_conf}) — upstream_rev 기록 건너뜀.", file=sys.stderr)
        return False
    text = local_conf.read_text(encoding="utf-8")
    new_text = _set_conf_keys(text, {"upstream_rev": rev})
    if new_text != text:
        local_conf.write_text(new_text, encoding="utf-8")
        return True
    return False


def record_opencode_model(dest_root: Path, model: str) -> bool:
    """해소된 opencode 모델을 dest local.conf 에 `opencode_pro_model=` 로 기록한다.

    {{OPENCODE_PRO_MODEL}} 가 import 때 파일에 직접 치환되지만, local.conf 엔 안 들어가
    pm_update 의 @render 가 그 토큰을 local.conf 에서 재유도할 때(`opencode_pro_model` →
    OPENCODE_PRO_MODEL · pm_update._LOCAL_CONF_TO_OPERATIONAL) 키 부재로 leak assertion 에
    걸려 채택자 렌더가 crash 한다. 따라서 *실제로 모델이 해소된* 경로(flag·interactive)에서만
    그 값을 local.conf 에 박아 둔다 — todo(미해소)는 토큰이 YAML 주석으로 남아 렌더 leak 이
    없으므로 기록하지 않는다(호출부 게이트). _set_conf_keys 키 단위 set-or-replace 라 다른
    키·주석은 보존. local.conf 부재면 graceful skip. 변경 시 True.
    """
    local_conf = dest_root / ".project_manager" / "local.conf"
    if not local_conf.is_file():
        print(f"경고: local.conf 없음 ({local_conf}) — opencode 모델 기록 건너뜀.",
              file=sys.stderr)
        return False
    text = local_conf.read_text(encoding="utf-8")
    new_text = _set_conf_keys(text, {"opencode_pro_model": model})
    if new_text != text:
        local_conf.write_text(new_text, encoding="utf-8")
        return True
    return False


# ── opencode 모델 결정적 해소 단계 (LLM 아님 · T-0033) ──────────────────────
# board init·conf sync 직후·fill *이전* 의 결정적 단계(sync_local_conf 와 같은 결). opencode
# 어댑터 token({{OPENCODE_PRO_MODEL}})이 이번 복사본에 잔존할 때만 동작한다.
# 해소 순서: ①--opencode-model 명시 → 치환  ②없고 stdin tty → `opencode models` 번호목록·선택
# → 치환  ③없고 비-tty 또는 opencode 부재 → 치환 안 함·TODO 마커(가용목록 인라인)+stderr 경고.
# `opencode models` 가 실제 가용 모델의 단일 진실 — LLM 추측(fill) 대신 결정적 조회를 쓴다.

# `opencode models` 조회 seam — () → (성공 여부, provider/model 목록). 테스트가 stub 주입.
ModelsRunner = Callable[[], "tuple[bool, list[str]]"]


class ModelResolveResult:
    """opencode 모델 해소 산출 — 어느 경로로 갔는지·결정된 값·치환 파일 수.

    필드:
      active    : 해소 단계가 동작했는가 (opencode 토큰 잔존 시만 True; claude-only 면 False).
      path      : 'flag' | 'interactive' | 'todo' | 'inactive' — 해소 경로.
      model     : 치환에 쓴 모델 ID (PROVIDER/MODEL) 또는 None(미치환).
      changed   : {{OPENCODE_PRO_MODEL}} 을 치환한 파일 수 (0 = 미치환·TODO 폴백).
      available : `opencode models` 조회 성공 시 가용 모델 목록 (실패/미조회 시 빈 리스트).
      tty       : 해소 시점 stdin 이 tty 였는가 (대화형 가능 여부).
      todos     : TODO 폴백에서 마커를 추가한 토큰 목록.
      note      : 사람 대상 메모 (경고·경로 사유).

    plain class (dataclass 아님): FillResult 와 같은 이유 — 테스트가 spec_from_file_location
    동적 로드 시 dataclass 의 문자열 annotation 해석이 깨진다.
    """

    def __init__(
        self,
        active: bool = False,
        path: str = "inactive",
        model: str | None = None,
        changed: int = 0,
        available: list | None = None,
        tty: bool = False,
        todos: list | None = None,
        note: str = "",
    ):
        self.active = active
        self.path = path
        self.model = model
        self.changed = changed
        self.available = available if available is not None else []
        self.tty = tty
        self.todos = todos if todos is not None else []
        self.note = note

    def __repr__(self) -> str:
        return (f"ModelResolveResult(active={self.active!r}, path={self.path!r}, "
                f"model={self.model!r}, changed={self.changed!r})")


def _real_models_runner() -> tuple[bool, list[str]]:
    """실 `opencode models` 를 subprocess 로 조회(fail-soft). 반환: (성공 여부, 모델 목록).

    _real_harness_runner 선례 — 예외를 raise 하지 않고 (False, []) 로 감싼다. opencode 바이너리가
    PATH 에 없으면(shutil.which 부재) subprocess 도 안 띄우고 즉시 (False, []) — fail-soft.
    stdout 은 _parse_opencode_models 로 줄단위 provider/model 파싱한다.

    T-0127: fail-soft 는 유지(import 안 깸)하되 *침묵*은 제거한다 — 각 실패 분기에서 stderr 로
    사유 1줄을 surface 해 사용자가 다음 실행 때 왜 자동해소가 실패했는지(PATH/rc/timeout/parse)를
    본다(T-0070 _real_git_runner stderr surface 선례 동형). 타임아웃은 _opencode_models_timeout()
    (env PM_OPENCODE_MODELS_TIMEOUT > 기본 60)으로 해소한다.
    """
    if shutil.which("opencode") is None:
        print("opencode 바이너리 PATH 부재 — 모델 자동해소 skip", file=sys.stderr)
        return False, []
    timeout = _opencode_models_timeout()
    try:
        result = subprocess.run(
            list(OPENCODE_MODELS_CMD),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        if result.returncode != 0:
            print(
                f"opencode models 실패 rc={result.returncode}: "
                f"{(result.stderr or '').strip()[:200]}",
                file=sys.stderr,
            )
            return False, []
        models = _parse_opencode_models(result.stdout or "")
        if not models:
            print("opencode models 출력에서 모델 0개 파싱 — 형식 확인", file=sys.stderr)
        return True, models
    except subprocess.TimeoutExpired:
        print(
            f"opencode models {timeout}s timeout 초과 — "
            "PM_OPENCODE_MODELS_TIMEOUT 로 늘리세요",
            file=sys.stderr,
        )
        return False, []
    except FileNotFoundError as exc:
        print(f"opencode models 예외: {exc}", file=sys.stderr)
        return False, []
    except Exception as exc:  # noqa: BLE001 — fail-soft: 어떤 예외도 import 를 깨지 않는다.
        print(f"opencode models 예외: {exc}", file=sys.stderr)
        return False, []


def _parse_opencode_models(output: str) -> list[str]:
    """`opencode models` stdout → provider/model 목록. 빈 줄·배너 제외.

    실측 형식: 줄당 `provider/model`(예 'ollama/gemma4:26b'·'opencode/big-pickle'). 슬래시가
    있는 줄만 모델로 본다(배너·헤더 줄 제외). 앞뒤 공백 strip, 순서·중복은 입력대로 보존.
    """
    models: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or "/" not in stripped:
            continue
        models.append(stripped)
    return models


def _substitute_model_token(
    dest_root: Path,
    model: str,
    copied_relpaths: set[Path],
) -> int:
    """{{OPENCODE_PRO_MODEL}} 을 복사 파일 전역에서 결정적 치환. 변경 파일 수 반환.

    substitute_placeholders 와 동일한 copied_relpaths 비파괴 범위·동일 _should_substitute
    확장자 규칙. 이번 import 가 복사한 파일만 — 복사 안 한 사용자 파일은 절대 안 건드린다.
    대상 = `.opencode/agents/*.md` 의 `model:` 필드(T-0032 후 주 타깃)·AGENTS.md 잔존분.
    """
    changed = 0
    for rel in sorted(copied_relpaths):
        if any(part in COPY_EXCLUDE_DIR_NAMES for part in rel.parts):
            continue
        if not _should_substitute(rel):
            continue
        path = dest_root / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if OPENCODE_MODEL_TOKEN not in text:
            continue
        path.write_text(text.replace(OPENCODE_MODEL_TOKEN, model), encoding="utf-8")
        changed += 1
    return changed


def _prompt_model_choice(models: list[str], stdin) -> str | None:
    """`opencode models` 번호 목록을 출력하고 stdin 에서 선택을 읽어 모델 ID 를 반환.

    1-based 번호 목록을 stdout 에 출력하고 stdin 한 줄을 읽는다. 유효한 번호면 해당 모델,
    빈 입력·범위 밖·비숫자·EOF 면 None(미선택 → 호출자가 TODO 폴백). 테스트는 io.StringIO 를
    stdin 으로 주입해 결정적으로 검증한다(라이브 입력 없음).
    """
    if not models:
        return None
    print("opencode 가용 모델 ({{OPENCODE_PRO_MODEL}} 에 쓸 모델 선택):")
    for i, m in enumerate(models, start=1):
        print(f"  {i}) {m}")
    print(f"  번호 입력 (1-{len(models)}, 빈 입력 = 건너뜀): ", end="")
    try:
        line = stdin.readline()
    except Exception:  # noqa: BLE001 — 입력 실패는 미선택 폴백.
        return None
    if not line:  # EOF
        return None
    choice = line.strip()
    if not choice:
        return None
    try:
        idx = int(choice)
    except ValueError:
        return None
    if 1 <= idx <= len(models):
        return models[idx - 1]
    return None


def _mark_model_todos(
    dest_root: Path,
    copied_relpaths: set[Path],
    available: list[str],
) -> list[str]:
    """비-tty/opencode 부재 폴백: `model:` 줄을 주석화하고 그 안의 모델 토큰을 중화한다.

    _mark_todos 폴백을 흡수(T-0033) — 모델 토큰만 대상. 조회 성공 시 가용 모델 목록을 마커에
    인라인해 사람이 바로 고를 수 있게 한다. _mark_todos 와 같은 비파괴 규칙(이미 TODO/주석인 줄은
    건너뜀·copied_relpaths 범위 한정). 마크한 토큰([OPENCODE_MODEL_TOKEN] 또는 [])을 반환.

    T-0077: 미해소 시 `model:` 값을 활성으로 남기면(`model: "…"  # TODO`) opencode 가
    "configured model … is not valid" 로 agent 자체를 거부한다(실 파일럿 블로커). → 줄 *전체*를
    주석화해 YAML frontmatter 에서 `model` 키를 *부재*시킨다 → opencode 가 기본 모델로 agent 를
    구동(graceful degradation).

    T-0133(@render 활성화·동작 변경): 미해소 폴백이 주석 줄에 리터럴 `{{OPENCODE_PRO_MODEL}}` 을
    남기면 render `_assert_no_leak` 가 hard-fail 한다(@render path 산출물에 토큰 0 이어야 함). 그래서
    주석화하면서 토큰을 **형식 힌트 `<provider/model>` 로 중화**한다 → `# model: "<provider/model>"
    # TODO: …`. (이전엔 리터럴 토큰을 그대로 보존했으나, 활성화가 그 동작과 양립 불가.) 채택자는
    주석을 해제하고 `<provider/model>` 자리에 provider/model 을 채우거나 `--opencode-model` 재import.
    """
    # ⚠️ 토큰은 (T-0032 후) 전부 `.opencode/agents/*.md` 의 YAML frontmatter `model:` 줄에만
    # 있다 — 주석은 반드시 **YAML 주석(`#`)** 이어야 한다. HTML 주석(`<!-- -->`)을 붙이면
    # `# model: ... <!-- ... -->` 가 되어 frontmatter 파싱이 깨진다(T-0033 codex must-fix). `#` 뒤는
    # 줄 끝까지 주석이라 가용목록의 `/`·`,` 도 안전. 줄 머리 `# ` 로 키 전체를 비활성화한다.
    if available:
        tail = f"  # TODO: opencode 모델 ID 를 넣으려면 이 줄 주석 해제 후 provider/model 로 치환 (가용: {', '.join(available)})"
    else:
        tail = "  # TODO: opencode 모델 ID 를 넣으려면 이 줄 주석 해제 후 provider/model(예: ollama/qwen3:8b) 로 치환"
    marked = False
    for _rel, path in _iter_copied_files(dest_root, copied_relpaths):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        new_text = text
        changed = False
        for line in text.splitlines(keepends=True):
            # **YAML `model:` 필드 줄만** 주석화한다 — 토큰이 산문/헤더(예: README 의
            # "placeholder `{{OPENCODE_PRO_MODEL}}` 로 출하된다"·`### 모델 선택`)에도 있어,
            # `토큰 in line` 만 보면 그 줄까지 `# ` prepend 돼 마크다운이 깨진다(산문→H1). agent
            # frontmatter 의 `model: "{{…}}"` 만 비활성 대상이므로 `model:` 시작 줄로 한정한다.
            # 비파괴 멱등: 이미 주석(`#` 머리)이거나 TODO 붙은 줄은 재처리 안 함(`# model:` 은
            # lstrip 이 `model:` 로 시작 안 해 자연히 skip).
            if OPENCODE_MODEL_TOKEN in line and "TODO" not in line \
                    and line.lstrip().startswith("model:"):
                eol = "\n" if line.endswith("\n") else ""
                # 토큰 중화 (T-0133·@render leak-safety): 줄을 주석화하면서 {{OPENCODE_PRO_MODEL}}
                # 을 <provider/model> 로 치환해 *토큰을 제거*한다. @render 활성화로 .opencode/agents
                # 가 render 대상이 됐고 render 의 _assert_no_leak 는 주석 안의 토큰도 잡으므로, 미해소
                # 폴백이 토큰을 남기면 RenderLeakError. 중화하면 토큰 0(자족) + 채택자 발견경로(주석
                # +TODO·fill 형식 힌트) 유지. (resolve 가 render 이전으로 이동했어도 이 줄은 필요 —
                # 주석화한 줄에 토큰을 남기면 뒤따르는 render 가 여전히 leak.)
                body = line.rstrip("\n").replace(OPENCODE_MODEL_TOKEN, "<provider/model>")
                replacement = "# " + body + tail + eol
                new_text = new_text.replace(line, replacement, 1)
                marked = True
                changed = True
        if changed and new_text != text:
            path.write_text(new_text, encoding="utf-8")
    return [OPENCODE_MODEL_TOKEN] if marked else []


def resolve_opencode_model(
    dest_root: Path,
    copied_relpaths: set[Path],
    *,
    model_arg: str | None,
    models_runner: ModelsRunner | None = None,
    stdin=None,
) -> ModelResolveResult:
    """{{OPENCODE_PRO_MODEL}} 을 결정적으로 해소(T-0033). board init·conf sync 직후·fill 이전.

    opencode 어댑터 token 이 이번 복사본(copied_relpaths)에 잔존할 때만 동작 — 없으면 inactive.
    해소 순서:
      ① model_arg 명시 → 치환. (조회 가능하면 목록 대조해 *경고만*; 목록에 없어도 사용자 의도
         존중·치환 — 회사 사설 모델 등.)
      ② 없고 stdin tty → `opencode models` 번호목록 출력·선택 입력 → 치환. (선택 안 하면 TODO 폴백.)
      ③ 없고 비-tty(CI·파이프) 또는 opencode 바이너리 부재 → 치환 안 함·TODO 마커(조회 성공 시
         가용목록 인라인)+stderr 경고.

    models_runner: `opencode models` 조회 seam — 테스트 stub 주입(라이브 CLI 미실행). None 이면
                   실 _real_models_runner. stdin: 대화형 선택 입력 seam — None 이면 sys.stdin.
    치환은 substitute_placeholders 와 동일한 copied_relpaths 비파괴 범위·_should_substitute 규칙.
    """
    # opencode 토큰이 이번 복사본에 없으면 단계 자체가 무의미(claude-only) — inactive.
    if not _token_present(dest_root, OPENCODE_MODEL_TOKEN, copied_relpaths):
        return ModelResolveResult(active=False, path="inactive",
                                  note="opencode 모델 토큰 미잔존 — 해소 단계 비활성(claude-only).")

    runner = models_runner if models_runner is not None else _real_models_runner
    stream = stdin if stdin is not None else sys.stdin
    is_tty = bool(getattr(stream, "isatty", lambda: False)())

    # ① --opencode-model 명시 → 치환(사용자 의도 우선). 조회 가능하면 목록 대조 경고만.
    if model_arg:
        # 명시값을 **먼저 확정**(치환) — 외부 `opencode models` 조회가 명시-플래그 경로의 import
        # 를 막지 않게(codex suggestion). 목록 대조는 그 *뒤* best-effort 경고만(짧은 timeout·
        # fail-soft — 조회 실패/지연이 치환 결과를 바꾸지 않는다).
        changed = _substitute_model_token(dest_root, model_arg, copied_relpaths)
        ok, available = runner()
        if ok and available and model_arg not in available:
            print(
                f"경고: --opencode-model '{model_arg}' 가 `opencode models` 가용 목록에 없습니다 "
                f"(사용자 의도 존중·그대로 치환됨). 가용: {', '.join(available)}.",
                file=sys.stderr,
            )
        return ModelResolveResult(
            active=True, path="flag", model=model_arg, changed=changed,
            available=available if ok else [], tty=is_tty,
            note=f"--opencode-model 명시값으로 치환({changed} 파일).",
        )

    # 플래그 없음 → `opencode models` 조회(②③ 공통 전제).
    ok, available = runner()

    # ② stdin tty + 조회 성공 → 번호목록·대화형 선택 → 치환.
    if is_tty and ok and available:
        choice = _prompt_model_choice(available, stream)
        if choice:
            changed = _substitute_model_token(dest_root, choice, copied_relpaths)
            return ModelResolveResult(
                active=True, path="interactive", model=choice, changed=changed,
                available=available, tty=True,
                note=f"대화형 선택 '{choice}' 로 치환({changed} 파일).",
            )
        # 선택 안 함(빈 입력·범위 밖) → TODO 폴백.
        todos = _mark_model_todos(dest_root, copied_relpaths, available)
        print("경고: opencode 모델 미선택 — {{OPENCODE_PRO_MODEL}} 을 TODO 로 표시(손으로 채우세요).",
              file=sys.stderr)
        return ModelResolveResult(
            active=True, path="todo", model=None, changed=0,
            available=available, tty=True, todos=todos,
            note="대화형 선택 건너뜀 — TODO 폴백.",
        )

    # ③ 비-tty / opencode 부재·조회 실패 → 치환 안 함·TODO 마커(가용목록 인라인 시도)+경고.
    todos = _mark_model_todos(dest_root, copied_relpaths, available if ok else [])
    if not ok:
        reason = "opencode 바이너리 부재 또는 `opencode models` 조회 실패"
    elif not is_tty:
        reason = "비대화형(CI·파이프) — 블로킹 프롬프트 회피"
    else:
        reason = "가용 모델 없음"
    print(
        f"경고: {{{{OPENCODE_PRO_MODEL}}}} 미치환({reason}) — TODO 로 표시했습니다. "
        f"--opencode-model PROVIDER/MODEL 로 명시하거나 손으로 채우세요.",
        file=sys.stderr,
    )
    return ModelResolveResult(
        active=True, path="todo", model=None, changed=0,
        available=available if ok else [], tty=is_tty, todos=todos,
        note=f"치환 안 함({reason}) — TODO 폴백.",
    )


# ── fill 단계 (자유서술 placeholder · 하니스 구동 · opt-in) ──────────────────
# board init·local.conf 동기화 직후의 hook 지점. sed 로 못 채우는 자유서술 placeholder 를
# 대상 하니스를 헤드리스 구동해 *제안* 한다(auto) / TODO 로 표시한다(manual). 실구동은 토큰·
# 외부모델 비용이므로 opt-in 게이트(LIVE_HARNESS_ENV + --fill auto) 뒤로 격리한다.

# 하니스 호출 seam — (argv, prompt) → (성공 여부, stdout). 테스트가 stub 주입(토큰 0).
HarnessRunner = Callable[[list[str], str], "tuple[bool, str]"]


class FillResult:
    """fill 단계 산출 — 자유서술 placeholder 값 + 초안 제안 (확정 아님, 사람 리뷰 전제).

    필드:
      mode        : 'auto' (하니스 구동) | 'manual' (TODO 표시).
      harness     : 실제 구동 하니스 ('claude' | 'opencode' | None=manual).
      live        : 실 하니스를 호출했는가 (opt-in 게이트 통과 시만 True).
      values      : placeholder token → 채운 값 (auto·stub 모두 채움 — manual 은 빈 dict).
      drafts      : 라벨 → 초안 텍스트 제안 (CLAUDE.md·pm_role.local.md·harness-output 등).
      todos       : manual 에서 TODO 로 남긴 placeholder token 목록.
      runner_calls: 하니스에 보낸 argv 리스트 (명령 조립 검증·로깅용 — stub 도 기록).
      note        : 사람 대상 메모 (게이트 차단 이유 등).

    plain class (dataclass 아님): 테스트가 spec_from_file_location 로 동적 로드하는데,
    그 경로에선 모듈이 sys.modules 에 없어 dataclass 의 문자열 annotation 해석이 깨진다.
    """

    def __init__(
        self,
        mode: str,
        harness: str | None = None,
        live: bool = False,
        values: dict | None = None,
        drafts: dict | None = None,
        todos: list | None = None,
        runner_calls: list | None = None,
        note: str = "",
    ):
        self.mode = mode
        self.harness = harness
        self.live = live
        self.values = values if values is not None else {}
        self.drafts = drafts if drafts is not None else {}
        self.todos = todos if todos is not None else []
        self.runner_calls = runner_calls if runner_calls is not None else []
        self.note = note

    def __repr__(self) -> str:
        return (f"FillResult(mode={self.mode!r}, harness={self.harness!r}, live={self.live!r}, "
                f"values={list(self.values)!r}, todos={self.todos!r})")


def _real_harness_runner(
    argv: list[str], prompt: str, cwd: Path | str | None = None
) -> tuple[bool, str]:
    """실 하니스 바이너리를 subprocess 로 구동(fail-soft). 반환: (성공 여부, stdout).

    external_review.run_reviewer 선례 — 예외를 raise 하지 않고 (False, 에러텍스트)로 감싼다.
    프롬프트는 argv 마지막 인자로 전달한다(claude -p "<prompt>" / opencode run "<prompt>" ...).

    SF: cwd 가 주어지면 *대상 repo* 에서 구동한다(run_fill 이 dest_root 를 바인딩). 호출자
    cwd 가 아니라 import 대상에서 돌아야 하니스의 작업 디렉토리·파일 접근이 분석 대상과 맞는다.
    """
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=FILL_TIMEOUT_SECONDS,
            cwd=str(cwd) if cwd is not None else None,
        )
        output = result.stdout or ""
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, f"[하니스 타임아웃 — {FILL_TIMEOUT_SECONDS}초 초과]"
    except FileNotFoundError:
        return False, f"[하니스 명령 '{argv[0] if argv else '?'}' 를 찾을 수 없음 — 설치/PATH 확인]"
    except Exception as exc:  # noqa: BLE001 — fail-soft: 어떤 예외도 import 를 깨지 않는다.
        return False, f"[하니스 실행 오류: {exc}]"


def _build_fill_prompt(dest_root: Path, tokens: list[str]) -> str:
    """대상 repo 분석 → 자유서술 placeholder 추출 프롬프트(하니스 구동용).

    프롬프트 품질이 산출을 좌우하나 초안일 뿐(확정 아님). 토큰별로 무엇을 채울지 지시한다.
    """
    token_lines = "\n".join(f"  - {t}" for t in tokens)
    return (
        f"이 저장소({dest_root})를 분석해 PM 프레임워크 자유서술 placeholder 를 채울 초안을 제안하라.\n"
        f"확정이 아니라 사람이 검토할 *초안*이다. 다음 placeholder 각각에 대해 한국어로 제안하라:\n"
        f"{token_lines}\n\n"
        f"  - {{{{PROJECT_CONSTRAINTS}}}}: 이 프로젝트의 아키텍처 불변식·금지(핵심 결정 경계 등).\n"
        f"  - {{{{PROTECTED_PATHS}}}}: code author + ADR 없이 건드리면 안 되는 파일/디렉토리.\n"
        f"  - {{{{USER_GATE_ITEMS}}}}: PM 자율 결정 밖 — 사용자 사전 동의가 필요한 행위.\n"
        f"불확실하면 빈 항목으로 두고 사람이 채우도록 TODO 를 남겨라."
    )


def _build_runner_argv(harness: str, prompt: str) -> list[str]:
    """하니스별 헤드리스 구동 명령 조립. claude → `claude -p "<p>"`,
    opencode → `opencode run "<p>" --format json` (token/cost 파싱 위해 json 출력)."""
    if harness == "opencode":
        return [*OPENCODE_FILL_CMD, prompt, "--format", "json"]
    # claude (기본·both→claude)
    return [*CLAUDE_FILL_CMD, prompt]


def _parse_opencode_json(output: str) -> str:
    """opencode `--format json` 출력에서 결과 텍스트를 추출(token/cost 파싱은 부수적).

    opencode 출력 형태는 버전에 따라 다를 수 있어 보수적으로 흔한 모양을 훑는다:
      - 최상위 'result'/'text'/'output' 문자열
      - 메시지 parts 배열의 text part 들 (parts[].text)
    파싱 실패하면 원문을 그대로 반환(fail-soft — 사람이 읽을 수 있게)."""
    try:
        data = json.loads(output)
    except (json.JSONDecodeError, ValueError):
        return output
    if isinstance(data, dict):
        for key in ("result", "text", "output", "content"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value
        parts = data.get("parts")
        if isinstance(parts, list):
            texts = [p["text"] for p in parts
                     if isinstance(p, dict) and isinstance(p.get("text"), str)]
            if texts:
                return "\n".join(texts)
    return output


def _live_harness_allowed(mode: str) -> bool:
    """opt-in 게이트: 실 하니스 호출은 PM_IMPORT_LIVE_HARNESS=1 AND --fill auto 동시 충족 시만.

    둘 중 하나라도 없으면 False → run_fill 이 실 runner 를 호출하지 않고 stub/manual 로 폴백.
    """
    return mode == "auto" and os.environ.get(LIVE_HARNESS_ENV, "").strip() in ("1", "true", "yes", "on")


def _resolve_fill_scope(dest_root: Path, copied_relpaths: set[Path] | None) -> set[Path]:
    """fill 스캔 대상 relpath set 을 결정한다.

    copied_relpaths 가 주어지면(main 경로) 그걸 그대로 쓴다 — 이번 import 가 복사한 파일만
    스캔(비파괴). None 이면(run_fill/_run_manual_fill 직접 호출 — 테스트·디버그) dest 트리
    전체를 폴백 스캔한다. 단, --into main 경로는 *항상* copied_relpaths 를 넘기므로 사용자
    파일 오염은 발생하지 않는다(이 폴백은 직접 호출자 편의용). 폴백도 COPY_EXCLUDE_DIR_NAMES
    (node_modules·__pycache__·.git)는 제외한다.
    """
    if copied_relpaths is not None:
        return copied_relpaths
    scope: set[Path] = set()
    for path in dest_root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(dest_root)
        if any(part in COPY_EXCLUDE_DIR_NAMES for part in rel.parts):
            continue
        scope.add(rel)
    return scope


def _iter_copied_files(dest_root: Path, copied_relpaths: set[Path]):
    """이번 import 가 복사한 파일들만 (relpath, 절대경로)로 순회한다.

    MF(비파괴): fill 단계가 dest_root.rglob 로 *대상 프로젝트 전체* 를 훑으면, --into 에서
    이번 import 가 복사하지 *않은* 기존 사용자 파일(우연히 sentinel 포함)에도 TODO 마커가
    주입되어 T-0007 비파괴 계약(substitute_placeholders 가 copied_relpaths 로 한정)과
    충돌한다. 따라서 fill 도 substitute_placeholders 와 *동일한* copied_relpaths set 만
    대상으로 한다 — 복사 안 한 사용자 파일은 절대 스캔/수정하지 않는다. node_modules·
    __pycache__·.git 등은 애초에 복사 목록에 없어 자연히 제외된다.
    """
    for rel in sorted(copied_relpaths):
        if any(part in COPY_EXCLUDE_DIR_NAMES for part in rel.parts):
            continue
        path = dest_root / rel
        if not path.is_file():
            continue
        yield rel, path


def _fill_targets(dest_root: Path, copied_relpaths: set[Path] | None = None) -> list[str]:
    """이번 import 가 복사한 파일에 실제로 남아있는 자유서술 placeholder 토큰 목록.

    잔존 grep 으로 판정 — 트리에 없는 토큰은 채울 필요 없음. 스캔 범위는 copied_relpaths
    (이번 run 복사 파일)로 한정 — 사용자 파일 불가침(비파괴). None 이면 dest 트리 전체 폴백
    (직접 호출용 — COPY_EXCLUDE_DIR_NAMES 제외).

    T-0033: {{OPENCODE_PRO_MODEL}} 는 LLM fill 후보가 *아니다* — resolve_opencode_model 의
    결정적 `opencode models` 조회가 전담한다(환각·미가용 모델 추측 제거). 여기서는 자유서술
    3종(FREE_FORM_TOKENS)만 본다.
    """
    scan = _resolve_fill_scope(dest_root, copied_relpaths)
    present: list[str] = []
    for token in FREE_FORM_TOKENS:
        if _token_present(dest_root, token, scan):
            present.append(token)
    return present


def _plan_fill_targets(actions: list[CopyAction]) -> list[str]:
    """dry-run 계획용: 복사될 *소스* 파일들에 남아있는 자유서술 토큰 목록.

    MF2: --dry-run 은 파일을 복사하지 않으므로 dest 트리에 토큰이 없다. 그래서 복사 *예정*인
    src 파일(actions[].src)을 직접 읽어 무엇을 채우게 될지 계획을 만든다. 실 fill(_fill_targets)
    이 copied dest 파일에서 보는 것과 동일한 후보 토큰 집합을 source 측에서 미리보기한다.

    T-0033: {{OPENCODE_PRO_MODEL}} 는 fill 후보에서 분리(결정적 resolve_opencode_model 전담)
    되므로 여기서도 자유서술 3종만 본다. 모델 토큰 계획은 _plan_opencode_model_targets 가 별도.
    """
    present: list[str] = []
    for token in FREE_FORM_TOKENS:
        for action in actions:
            if _is_engine_source(action.dst):  # 엔진 소스 주석의 토큰-문서는 placeholder 아님 (T-0133)
                continue
            try:
                if token in action.src.read_text(encoding="utf-8"):
                    present.append(token)
                    break
            except (UnicodeDecodeError, OSError):
                continue
    return present


def _plan_opencode_model_targets(actions: list[CopyAction]) -> bool:
    """dry-run 계획용: 복사 *예정* src 에 {{OPENCODE_PRO_MODEL}} 토큰이 잔존하는가(opencode 트리).

    실 단계(resolve_opencode_model)가 dest 복사본에서 토큰 잔존을 보고 동작 여부를 정하는데,
    dry-run 은 복사를 안 하므로 src 측에서 미리 본다(_plan_fill_targets 와 같은 결).
    """
    for action in actions:
        if _is_engine_source(action.dst):  # 엔진 소스(.py) 주석의 모델-토큰 문서는 placeholder 아님 (T-0133)
            continue
        try:
            if OPENCODE_MODEL_TOKEN in action.src.read_text(encoding="utf-8"):
                return True
        except (UnicodeDecodeError, OSError):
            continue
    return False


def _token_present(
    dest_root: Path,
    token: str,
    copied_relpaths: set[Path] | None = None,
) -> bool:
    """이번 import 가 복사한 파일에 token 이 한 파일이라도 남아있는가(비파괴 범위 한정).

    copied_relpaths=None 이면 dest 트리 전체 폴백(COPY_EXCLUDE_DIR_NAMES 제외) — 직접 호출용.
    """
    scan = _resolve_fill_scope(dest_root, copied_relpaths)
    for _rel, path in _iter_copied_files(dest_root, scan):
        if _is_engine_source(_rel):  # 엔진 소스 주석의 토큰-문서는 placeholder 아님 (T-0133)
            continue
        try:
            if token in path.read_text(encoding="utf-8"):
                return True
        except (UnicodeDecodeError, OSError):
            continue
    return False


def _mark_todos(
    dest_root: Path,
    tokens: list[str],
    copied_relpaths: set[Path] | None = None,
) -> list[str]:
    """manual 모드: 자유서술 placeholder 옆에 `<!-- TODO -->` 가 없으면 표시한다.

    템플릿은 대개 이미 placeholder 아래에 TODO 주석을 둔다(T-0007 보존). 여기서는 토큰을
    `<!-- TODO: 손으로 채우세요 -->` 인라인으로 *치환*하지 않고, 토큰 줄에 TODO 마커가 없을
    때만 토큰 뒤에 인라인 마커를 덧붙여(비파괴) 채택자에게 손작업 지점을 명시한다.
    실제로 마커를 추가한 토큰 목록을 반환한다.

    스캔 범위는 copied_relpaths(이번 run 복사 파일)로 한정 — 복사하지 않은 사용자 파일에는
    절대 마커를 주입하지 않는다(비파괴·T-0007 계약). None 이면 dest 트리 전체 폴백(직접 호출용).
    """
    scan = _resolve_fill_scope(dest_root, copied_relpaths)
    marked: set[str] = set()
    marker = " <!-- TODO: 손으로 채우세요 -->"
    for _rel, path in _iter_copied_files(dest_root, scan):
        if _is_engine_source(_rel):  # 엔진 소스(.py)에 TODO 마커 주입 금지 — verbatim (T-0133)
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        new_text = text
        changed = False
        for line in text.splitlines(keepends=True):
            for token in tokens:
                if token in line and "TODO" not in line:
                    replacement = line.replace("\n", "") + marker + ("\n" if line.endswith("\n") else "")
                    new_text = new_text.replace(line, replacement, 1)
                    marked.add(token)
                    changed = True
        if changed and new_text != text:
            path.write_text(new_text, encoding="utf-8")
    return sorted(marked)


def run_fill(
    dest_root: Path,
    harness: str,
    *,
    live: bool,
    runner: HarnessRunner | None = None,
    copied_relpaths: set[Path] | None = None,
) -> FillResult:
    """자유서술 placeholder 채움 단계. board init 직후 hook (T-0009).

    dest_root: import 된 대상 트리. harness: 구동 하니스('claude'|'opencode').
    live=True  → 실 하니스 호출 시도(opt-in 게이트 통과 — main 이 _live_harness_allowed 로 판정).
    live=False → runner 미호출(stub 경로). runner 가 주입되면(테스트 stub) live=True 와 무관하게
                 그 runner 로 명령을 조립·호출해 *명령 조립* 만 검증한다(토큰 0).
    copied_relpaths: 이번 import 가 복사한 파일 relpath set — fill 스캔 범위를 이 파일들로
                 한정한다(비파괴·T-0007 계약). None 이면 dest 트리 전체를 스캔(직접 호출용
                 폴백) — main 은 항상 substitute_placeholders 와 동일 set 을 전달한다.

    계약(ticket §인터페이스): live=False 면 runner 를 호출하지 않고 stub/manual 경로로 간다.
    여기서 '하니스 미구동'은 *실 바이너리* 미구동을 뜻한다 — 주입 runner(stub)는 항상 안전.
    """
    scan = _resolve_fill_scope(dest_root, copied_relpaths)
    tokens = _fill_targets(dest_root, scan)

    # manual(또는 채울 토큰 없음): 하니스 미구동 — TODO 표시만.
    if not tokens:
        return FillResult(mode="manual", note="자유서술 placeholder 가 트리에 없음 — 처리 불필요.")

    prompt = _build_fill_prompt(dest_root, tokens)
    argv = _build_runner_argv(harness, prompt)

    # 실 runner 결정: 주입 stub 이 있으면 그걸(테스트). 없고 live 면 실 바이너리. 아니면 미구동.
    # SF: 실 바이너리는 대상 repo(dest_root)에서 구동되도록 cwd 를 바인딩한다(호출자 cwd 아님).
    effective_runner: HarnessRunner | None
    if runner is not None:
        effective_runner = runner
    elif live:
        effective_runner = functools.partial(_real_harness_runner, cwd=dest_root)
    else:
        effective_runner = None

    if effective_runner is None:
        # stub/실호출 모두 없음 → 자유서술 placeholder 값을 채우지 않고 제안만 비움.
        # (main 은 manual 또는 게이트 미통과 시 이 경로 대신 _run_manual_fill 을 부른다.)
        return FillResult(
            mode="auto",
            harness=harness,
            live=False,
            note="하니스 미구동(게이트 미통과·stub 없음) — 제안 없음. manual 폴백 권장.",
        )

    ok, output = effective_runner(argv, prompt)
    text = _parse_opencode_json(output) if harness == "opencode" else output

    result = FillResult(mode="auto", harness=harness, live=live)
    result.runner_calls.append(list(argv))
    if not ok:
        result.note = f"하니스 구동 실패(fail-soft) — 제안 없음. 출력: {text.strip()[:200]}"
        return result

    # 산출 텍스트 = 사람이 검토할 placeholder 값 제안. 각 토큰에 동일 출력을 후보로 매핑한다
    # (정밀 파싱은 모델 출력 형식에 의존 — 초안 전제라 통째로 제안하고 사람이 분배·편집).
    for token in tokens:
        result.values[token] = text
    result.drafts["(harness-output)"] = text
    result.note = "하니스 구동 제안 — 사람 검토 후 손으로 반영(자동 확정 아님)."
    return result


def _run_manual_fill(
    dest_root: Path,
    copied_relpaths: set[Path] | None = None,
) -> FillResult:
    """manual 모드(기본): 하니스 미구동. 자유서술 placeholder 에 TODO 마커 표시만.

    copied_relpaths: 이번 import 가 복사한 파일 relpath set — TODO 마킹 범위를 이 파일들로
    한정한다(비파괴). None 이면 dest 트리 전체 폴백(직접 호출용). main 은 항상 전달한다.
    """
    scan = _resolve_fill_scope(dest_root, copied_relpaths)
    tokens = _fill_targets(dest_root, scan)
    if not tokens:
        return FillResult(mode="manual", note="자유서술 placeholder 가 트리에 없음 — 처리 불필요.")
    marked = _mark_todos(dest_root, tokens, scan)
    return FillResult(
        mode="manual",
        todos=marked,
        note="자유서술 placeholder 를 TODO 로 표시 — 채택자가 손으로 채운다(하니스 미구동).",
    )


def ensure_pm_playbook_local_stub(dest_root: Path, backup_root: Path | None) -> str:
    """pm_playbook.local.md 스텁을 dest 에 생성한다 (ADR-0007 / T-0028).

    backup_root (T-0034): 중앙 백업 디렉토리(또는 None=--new). 이 함수는 기존 .local 을
    *덮지 않고 보존(skip)* 하므로 백업할 원본 변경이 없다 — backup_root 는 시그니처 일관성을
    위해 받지만 실제로 사용하지 않는다(미생성 = 백업 불요).

    fill 단계와 같은 자리(board init·conf sync 직후)에서 호출 — pm_role.local 초안 처리와
    같은 결의 인스턴스-소유 문서다. 루트 .local(T-0027)은 manifest 밖이라 템플릿 복사로 안 오니
    여기서 PM_PLAYBOOK_LOCAL_STUB(단일 소스 인라인 상수)로 *생성*한다.

    비파괴(재-import): 기존 pm_playbook.local.md 가 있으면 덮지 않는다 — 인스턴스가 누적한
    wave 학습이 손실되면 안 된다(local.conf 백업 철학 MF1·_backup_existing_local_conf 와 동일).
    구현은 *skip* — 기존 .local 은 manifest 밖·인스턴스 소유라 import 산출 백업과 별개로
    그대로 보존한다(누적 학습 trail 은 사용자 VCS 가 이력 관리). 백업하지 않는 이유: 백업은
    "import 가 덮는 파일"을 위한 것인데 여기선 애초에 덮지 않으므로 백업할 원본 변경이 없다.

    반환값(사람 대상 상태):
      "created" — 새 스텁 생성.
      "preserved" — 기존 .local 발견·비파괴 보존(미생성).
    """
    target = dest_root / PM_PLAYBOOK_LOCAL_RELPATH
    if target.exists():
        # 비파괴: 인스턴스 소유 누적 학습 보존(덮지 않음·skip).
        return "preserved"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(PM_PLAYBOOK_LOCAL_STUB, encoding="utf-8")
    return "created"


def _harness_binary_available(harness: str) -> bool:
    """fill 구동 하니스의 실 바이너리(claude/opencode)가 PATH 에 있는가.

    shutil.which 로 탐지한다. 테스트는 monkeypatch(pm_import.shutil.which) 또는 PATH 조작으로
    부재를 stub 한다. 알 수 없는 harness 면 보수적으로 False(폴백 유도).
    """
    binary = {"claude": "claude", "opencode": "opencode"}.get(harness)
    if binary is None:
        return False
    return shutil.which(binary) is not None


def _resolve_fill_harness(fill_harness_arg: str | None, harness: str) -> str:
    """fill 구동 하니스 결정. --fill-harness 명시값 우선, 없으면 --harness 따름.

    both(또는 fill-harness 미지정)에서는 claude 를 우선하되 **claude 바이너리가 없으면
    opencode 로 폴백**한다(MF1: claude code 없는 회사 배포에서 opencode 1급 구동 — opencode 도
    없으면 claude 를 그대로 반환해 상위 게이트/manual 폴백에 맡긴다). --fill-harness 명시값은
    바이너리 유무와 무관하게 그대로 존중한다(사용자 의도 우선).
    """
    if fill_harness_arg:
        return fill_harness_arg
    if harness == "both":
        # both → claude 우선. claude 바이너리 부재 시 opencode 폴백(회사 배포 1급 경로).
        if _harness_binary_available("claude"):
            return "claude"
        if _harness_binary_available("opencode"):
            return "opencode"
        return "claude"  # 둘 다 없음 — 상위 opt-in 게이트/manual 폴백이 처리.
    return harness


def _print_fill_result(result: FillResult, dry_run: bool) -> None:
    """fill 결과를 사람 대상으로 출력. auto 제안은 *적용 안 함* — 사람 검토 전제."""
    if result.mode == "manual":
        if result.todos:
            print(f"✓ 자유서술 placeholder TODO 표시: {'·'.join(result.todos)} "
                  f"(채택자가 손으로 채웁니다).")
        else:
            print(f"  fill(manual): {result.note}")
        return
    # auto
    print(f"[fill auto] harness={result.harness}  live={result.live}")
    if result.runner_calls:
        for call in result.runner_calls:
            print(f"  구동 명령: {shlex.join(call) if hasattr(shlex, 'join') else ' '.join(call)}")
    if result.values:
        print("  제안된 자유서술 placeholder 값 (검토 후 손으로 반영 — 자동 확정 아님):")
        for token, value in result.values.items():
            preview = value.strip().splitlines()[0][:80] if value.strip() else "(빈 제안)"
            print(f"    {token} → {preview}")
        if dry_run:
            print("  [dry-run] 제안만 출력 — 파일 미변경.")
    if result.note:
        print(f"  메모: {result.note}")


# ── 모드 준비 (--new / --into) ─────────────────────────────────────────────

def resolve_template_roots(source_root: Path, harness: str) -> list[Path]:
    """--from 의 templates/<harness>/ 어댑터 트리 경로들. 없으면 FileNotFoundError."""
    roots: list[Path] = []
    for name in HARNESS_TEMPLATE_DIRS[harness]:
        root = source_root / "templates" / name
        if not root.is_dir():
            raise FileNotFoundError(
                f"소스 어댑터 트리 없음: {root}. "
                f"--from 이 올바른 프레임워크 checkout 인지 확인하라 "
                f"(templates/{name}/ 필요)."
            )
        roots.append(root)
    return roots


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
    """dest .gitignore 에 `.pm_import_backups/` 패턴을 보장한다 (T-0034 should·git 위생).

    반환 상태: "added"(기존에 append) · "created"(신규 .gitignore 생성) ·
      "present"(이미 무시 중·멱등 skip) · "unsafe-skip"(사전 존재 unbacked 사용자 .gitignore —
      비파괴 위해 미변경) · "noop"(읽기 실패).

    **비파괴(codex T-0034 must-fix):** 기존 .gitignore 를 append 하려면 둘 중 하나여야 한다 —
      (a) **git-safe**(추적 중 & 미변경 → git 이 복원 가능), 또는
      (b) **이번 import 가 복사·관리한 파일**(`.gitignore` ∈ copied_relpaths) — 이 경우 사용자
          원본이 있었다면 CopyAction 이 이미 중앙 백업했으므로 append 가 안전하다.
    둘 다 아니면 *사전 존재하는 unbacked 사용자 파일*이므로(미추적/dirty·import 가 안 건드림)
    무백업 변조를 피해 "unsafe-skip" 으로 수동 추가를 안내한다(이 append 는 CopyAction 백업
    경로를 타지 않으므로 별도 가드 필요). .gitignore 가 없으면 새로 만든다(비파괴·신규 파일).
    """
    pattern = f"{BACKUP_DIR_NAME}/"
    gitignore = dest_root / ".gitignore"
    # MF1(codex T-0034): .gitignore 가 symlink 면 write_text 가 링크 대상(프로젝트 밖 가능)을
    #   따라가 변조한다 — git-safe 여도 링크 대상은 git 복원 대상이 아니다. CopyAction 의 symlink
    #   비파괴 정책(follow_symlinks=False)과 일관되게 자동 append 를 거부한다.
    if gitignore.is_symlink():
        return "unsafe-skip"
    if gitignore.is_file():
        try:
            text = gitignore.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            return "noop"
        # 이미 무시 중이면(정확한 패턴 줄·앞뒤 공백 무시) skip — 멱등.
        existing_lines = {line.strip() for line in text.splitlines()}
        if pattern in existing_lines or BACKUP_DIR_NAME in existing_lines:
            return "present"
        git_safe_ok = git_safe is not None and ".gitignore" in git_safe
        import_owned = Path(".gitignore") in copied_relpaths
        if not (git_safe_ok or import_owned):
            return "unsafe-skip"  # 사전 존재 unbacked 사용자 파일 — 무백업 변경 금지.
        prefix = "" if text.endswith("\n") or text == "" else "\n"
        new_text, status = f"{text}{prefix}{pattern}\n", "added"
    else:
        new_text, status = f"{pattern}\n", "created"
    # 방어(codex T-0034 suggestion): 위생 write 실패(권한 등)가 *복사·치환이 끝난* import 말미를
    #   깨뜨리지 않게 한다 — gitignore 위생은 should 부가단계라 실패해도 import 자체는 성공으로 둔다.
    try:
        gitignore.write_text(new_text, encoding="utf-8")
    except OSError:
        return "noop"
    return status


# ── main ───────────────────────────────────────────────────────────────────

def _set_console_codepage_utf8() -> None:
    # Windows 한정 — 콘솔 코드페이지를 UTF-8(65001)로 맞춘다. cp949(한국어) 콘솔에서
    # stdout reconfigure(utf-8)만으로는 콘솔이 UTF-8 바이트를 cp949 로 디코드해 한글이
    # mojibake 되므로, 콘솔 입출력 codepage 자체를 65001 로 설정해 정합시킨다 (T-0068).
    # best-effort: 콘솔 핸들 없음·권한·예외 시 조용히 통과(reconfigure 와 동형 try/except).
    # idempotent — 이미 UTF-8 콘솔엔 65001 재설정이 무해. POSIX 는 진입하지 않는다.
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    # 콘솔/파이프 출력을 UTF-8 로 재설정 — cp949 콘솔이나 리다이렉트된 stdout 에서
    # 이모지·em-dash(—) print 가 UnicodeEncodeError 로 죽는 것을 막는다 (T-0017).
    # 먼저 Windows 콘솔 codepage 를 UTF-8 로 맞춘 뒤(T-0068) 스트림을 reconfigure 한다.
    # reconfigure 미지원 스트림(테스트 캡처 등)은 hasattr 가드로 건너뛴다.
    _set_console_codepage_utf8()
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    ap = argparse.ArgumentParser(
        prog="pm_import.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "온보딩(fresh 채택자·T-0144): manager(project_manager) 경로/URL 만 있으면 자율 import — "
            "harness=자기 세션(claude|opencode), --new(빈 PM 홈·ADR-0026)/--into(기존 프로젝트 임베드) "
            "맥락 판단. 상세 가이드 = manager 루트 ADOPT.md. import 후 다음 단계: /pm-bootstrap → /pm-env.\n\n"
            "upstream 기록(T-0145): --from 은 *파일 소스*, --upstream 은 *future update 기록*으로 "
            "디커플된다. local.conf 에 `upstream=`(pm_update 가 --from 생략 시 사용) + "
            "`upstream_rev=<commit>`(drift baseline·--from 이 로컬 git checkout 일 때)이 기록된다. "
            "--upstream 생략 시 --from 으로 폴백하되, --from 이 로컬 clone 이면 origin URL 을 자동도출한다 "
            "(릴리스 추적 기본). 재-import 시 현재 값으로 갱신."
        ),
    )
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--into", metavar="PATH", help="기존 프로젝트에 임베드 import(비파괴·백업·특정 케이스)")
    mode.add_argument("--new", metavar="PATH", help="PM 홈 생성 + git init (코드 없는 홈·표준 채택·ADR-0026)")
    ap.add_argument("--harness", choices=HARNESS_CHOICES, default="claude",
                    help="어댑터 선택 (default: claude)")
    ap.add_argument("--weight", choices=WEIGHT_CHOICES, default="full",
                    help="무게축 (default: full)")
    ap.add_argument("--from", dest="source", default=str(REPO),
                    help="이번 import 의 *파일 소스* checkout 경로 (default: 이 repo 루트). "
                         "엔진/어댑터 파일을 여기서 복사한다.")
    ap.add_argument("--upstream", dest="upstream", default=None,
                    help="future update 기록값(URL 선호) — pm_update 가 --from 생략 시 쓸 upstream. "
                         "URL|경로 self-describing. 생략 시 --from 으로 폴백하되, --from 이 로컬 "
                         "git clone 이면 `git remote get-url origin` 으로 URL 자동도출(릴리스 추적·ADR-0032). "
                         "값은 안전 검증(scheme allowlist·credential 거부·leading-dash)을 통과해야 한다.")
    ap.add_argument("--name", help="{{PROJECT_NAME}} 값 (default: 대상 디렉토리명)")
    ap.add_argument("--fill", choices=FILL_CHOICES, default="manual",
                    help="자유서술 placeholder 채움 — auto: 하니스 구동 제안(opt-in), "
                         "manual: TODO 표시(default)")
    ap.add_argument("--fill-harness", choices=FILL_HARNESS_CHOICES, default=None,
                    help="fill 구동 하니스 (default: --harness; both→claude, claude 부재 시 opencode 폴백)")
    ap.add_argument("--opencode-model", dest="opencode_model", metavar="PROVIDER/MODEL",
                    default=None,
                    help="{{OPENCODE_PRO_MODEL}} 결정적 치환값 (비대화/CI). 예 'ollama/qwen3.6:27b'. "
                         "opencode 어댑터 미포함이면 무시(claude-only)")
    ap.add_argument("--dry-run", action="store_true",
                    help="적용 없이 fill 계획만 출력 (실 하니스 미호출·파일시스템 미변경)")
    args = ap.parse_args(argv)

    # --upstream 명시값은 *부작용 전* 입구에서 fail-closed 검증(T-0145·T-0078 동형). 나쁜 값
    # (빈/leading-dash/credential-in-URL/비허용 scheme)을 silently 기록하지 않게 입구에서 거른다.
    if args.upstream is not None:
        ok, reason = validate_upstream_value(args.upstream)
        if not ok:
            print(f"오류: --upstream 값 거부 — {reason}", file=sys.stderr)
            return 1

    is_new = args.new is not None
    dest_root = Path(args.into or args.new).resolve()
    source_root = Path(args.source).resolve()
    project_name = args.name or dest_root.name
    today = datetime.date.today().isoformat()

    try:
        template_roots = resolve_template_roots(source_root, args.harness)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    # SF(codex 5차): --new 대상이 기존 *파일* 이면 아래 iterdir() 가 NotADirectoryError 로
    #      터진다 — 디렉토리 여부를 먼저 검사해 친화적 비0 오류로 거부한다.
    if is_new and dest_root.exists() and not dest_root.is_dir():
        print(
            f"오류: --new 대상이 디렉토리가 아닌 기존 파일입니다: {dest_root}. "
            f"다른 경로를 지정하거나 해당 파일을 직접 옮긴 뒤 다시 시도하세요.",
            file=sys.stderr,
        )
        return 1

    # MF2: --new 는 백업 없이 복사하므로(아래 backup_root=None), 대상이 비어있지 않으면
    #      기존 파일을 무백업 덮을 위험 → 명세(비0=대상 비어있지 않은데 백업 불가)대로 거부.
    #      dry-run 에서도 동일 판정(계획 전 게이트).
    if is_new and dest_root.is_dir() and any(dest_root.iterdir()):
        print(
            f"오류: --new 대상이 비어있지 않습니다: {dest_root}. "
            f"기존 파일이 있는 디렉토리에는 비파괴 백업이 되는 --into 를 사용하세요.",
            file=sys.stderr,
        )
        return 1

    # MF2: --into 는 *기존 프로젝트* 가정이다. 미존재/비-디렉토리 경로면, 복사가 디렉토리를
    #      새로 만들고 git init 없이 board.py init 이 성공해 pre-push 훅 없는 불완전 import 가
    #      "완료"된다 → 거부. 새 프로젝트는 git init·디렉토리 생성을 하는 --new 로 안내.
    #      dry-run 에서도 동일 판정(계획 전 게이트 — --new 가드와 대칭).
    if not is_new and not dest_root.is_dir():
        print(
            f"오류: --into 대상이 존재하는 디렉토리가 아닙니다: {dest_root}. "
            f"--into 는 기존 프로젝트 전용입니다 — 새 프로젝트는 --new 를 사용하세요 "
            f"(디렉토리 생성 + git init).",
            file=sys.stderr,
        )
        return 1

    # T-0034: --into 백업을 중앙 디렉토리 `<dest>/.pm_import_backups/<DATE>/` 로 모은다.
    #   --new 는 빈 디렉토리 보장이라 백업 없음(backup_root=None). git_safe = '추적&미변경'
    #   relpath 집합(또는 None=비-git·판정불가). git 호출 실패는 None→전부 백업(보수적 폴백).
    backup_root = None if is_new else dest_root / BACKUP_DIR_NAME / today
    git_safe = None if is_new else git_safe_relpaths(dest_root)
    try:
        actions = plan_copy(template_roots, dest_root, backup_root, args.weight,
                            git_safe=git_safe)
        # codex T-0034: local.conf 백업 target 의 조상도 plan 단계에서 검증한다. 이 백업은
        #   plan_copy actions 밖(apply 후 backup_existing_local_conf)에서 일어나므로, 그 조상이
        #   일반 파일/symlink 면 *복사가 일부 끝난 뒤* mkdir 실패로 부분 적용이 남는다 → 사전 차단.
        if backup_root is not None and not is_new:
            _local_conf = dest_root / ".project_manager" / "local.conf"
            if _local_conf.is_file() or _local_conf.is_symlink():
                _check_ancestor_safe(
                    dest_root, backup_root / ".project_manager" / "local.conf", set())
    except (FileVsDirConflict, AncestorConflict) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1

    # ── 계획 출력 ──
    mode_label = f"--new {dest_root}" if is_new else f"--into {dest_root}"
    print(f"[pm_import] {mode_label}  harness={args.harness}  weight={args.weight}")
    print(f"  소스: {source_root}/templates/{'+'.join(HARNESS_TEMPLATE_DIRS[args.harness])}")
    n_copy = len(actions)
    n_backup = sum(1 for a in actions if a.backup is not None)
    n_git_safe = sum(1 for a in actions if a._git_safe_skip)
    for a in actions:
        print(a.describe())
    # T-0034: 백업이 중앙 디렉토리이고, git-safe(추적&미변경)는 백업 생략임을 한 줄로 요약한다.
    if not is_new:
        git_note = (
            f"git work tree (추적&미변경 {n_git_safe} 백업 생략)"
            if git_safe is not None
            else "비-git/판정불가 (충돌 전부 백업)"
        )
        print(f"  백업 위치: {BACKUP_DIR_NAME}/{today}/  · {git_note}")
    print(f"  → {n_copy} 파일 복사 ({n_backup} 백업), placeholder 치환, board.py init")

    # fill 단계 계획/게이트 미리보기 (dry-run·실행 공통). 실 하니스 호출 여부는 opt-in 게이트
    # (PM_IMPORT_LIVE_HARNESS=1 AND --fill auto)로 결정한다 — 여기서는 의도만 출력한다.
    fill_harness = _resolve_fill_harness(args.fill_harness, args.harness)
    live_allowed = _live_harness_allowed(args.fill)
    if args.fill == "auto":
        gate = "실구동(게이트 통과)" if live_allowed else "stub/미구동(게이트 미통과 — 안전)"
        print(f"  fill=auto  harness={fill_harness}  → {gate}")
    else:
        print("  fill=manual  → 자유서술 placeholder 를 TODO 로 표시(하니스 미구동).")

    if args.dry_run:
        # T-0033: opencode 모델 결정적 해소 계획(LLM 아님·fill 이전 단계). 복사 *예정* src 에
        #         {{OPENCODE_PRO_MODEL}} 가 잔존하면(opencode 트리) 어느 경로로 갈지·플래그값·
        #         tty 여부만 출력한다 — 프롬프트·파일변경·`opencode models` 실호출 0.
        if _plan_opencode_model_targets(actions):
            stdin_tty = bool(getattr(sys.stdin, "isatty", lambda: False)())
            print("  [dry-run] opencode 모델 해소 계획 (결정적·LLM 아님·파일 미변경):")
            print(f"    stdin tty: {stdin_tty}")
            if args.opencode_model:
                print(f"    경로: flag → --opencode-model '{args.opencode_model}' 로 치환.")
            elif stdin_tty:
                print("    경로: interactive → `opencode models` 번호목록·대화형 선택 후 치환.")
            else:
                print("    경로: todo → 비-tty(또는 opencode 부재) — 치환 안 함·TODO 마커 폴백.")
        # MF2: --dry-run = fill 계획 미리보기. 실 하니스 미호출·파일 미변경. auto 면 무엇을
        #      채울지(대상 토큰)·결정된 fill harness·게이트 상태(실구동/manual 폴백)를 출력한다.
        #      복사를 안 하므로 dest 가 아닌 복사 *예정* src(actions)에서 잔존 토큰을 스캔한다.
        if args.fill == "auto":
            plan_tokens = _plan_fill_targets(actions)
            print("  [dry-run] fill=auto 계획 (실 하니스 미호출·파일 미변경):")
            if plan_tokens:
                print(f"    채울 대상 토큰: {'·'.join(plan_tokens)}")
            else:
                print("    채울 대상 토큰: (트리에 자유서술 placeholder 없음 — 처리 불필요)")
            print(f"    fill harness: {fill_harness}")
            if live_allowed:
                print("    게이트: 통과 → 적용 시 실 하니스 구동(제안 — 사람 검토 전제).")
            else:
                print("    게이트: 미통과(PM_IMPORT_LIVE_HARNESS 미설정) → 적용 시 manual 폴백"
                      "(TODO 표시·하니스 미구동).")
        print("[dry-run] 적용 안 함 (파일시스템 미변경).")
        return 0

    # ── 적용 ──
    if is_new:
        dest_root.mkdir(parents=True, exist_ok=True)
        # MF2: git init 실패 시 git repo 없는 불완전 import — board.py init 의 pre-push 훅이
        #      git repo 에 의존하므로 비0 전파(복사 전에 중단).
        git_rc = git_init(dest_root)
        if git_rc != 0:
            print(
                f"오류: git init 비0 종료({git_rc}) — import 미완(git repo 없이는 "
                f"board.py init 의 pre-push 훅이 동작하지 않습니다).",
                file=sys.stderr,
            )
            return git_rc

    for a in actions:
        a.run()

    # MF1: 치환 범위 = 이번 run 이 복사한 파일만(복사 안 한 사용자 파일 불가침).
    copied_relpaths = {a.dst.relative_to(dest_root) for a in actions}
    subs = _substitution_map(project_name, dest_root, today)
    n_subst = substitute_placeholders(dest_root, subs, copied_relpaths)
    print(f"✓ {n_copy} 파일 복사 · {n_subst} 파일 placeholder 치환")

    # ── opencode 모델 결정적 해소 (T-0033): substitute *직후*·render *이전*. @render 활성화(T-0133)
    #    로 .opencode/agents 가 render 대상이 됐으므로, render_managed_files 가 model: 줄의
    #    {{OPENCODE_PRO_MODEL}} 을 만나기 *전* 에 해소해야 한다 — flag/interactive=토큰 치환,
    #    todo(미해소)=줄 주석화 + 토큰 중화(<provider/model>) → 어느 경로든 render 시점엔 토큰 0.
    #    (이 단계 이전엔 render 가 0 path 였어서 resolve 가 render *뒤*에 있었다 — 활성화가 그 순서
    #    가정을 깸·옛 "todo 토큰은 YAML 주석으로 남아 leak 없음" 전제 무효.) local.conf 기록
    #    (record_opencode_model)은 board init 이 local.conf 를 만든 *뒤* 로 분리(아래).
    #    LLM 추측(fill)이 아니라 `opencode models` 결정적 조회로 해소(환각·미가용 모델 제거).
    #    범위 = substitute_placeholders 와 동일한 copied_relpaths(비파괴). claude-only=inactive.
    model_result = resolve_opencode_model(
        dest_root, copied_relpaths, model_arg=args.opencode_model)
    if model_result.active:
        if model_result.path == "flag":
            print(f"✓ {OPENCODE_MODEL_TOKEN} 치환(--opencode-model "
                  f"'{model_result.model}', {model_result.changed} 파일)")
        elif model_result.path == "interactive":
            print(f"✓ {OPENCODE_MODEL_TOKEN} 치환(대화형 선택 "
                  f"'{model_result.model}', {model_result.changed} 파일)")
        elif model_result.path == "todo":
            print(f"  {OPENCODE_MODEL_TOKEN} TODO 표시 — {model_result.note}")

    # render 단계 (T-0131·ADR-0028·ADR-0031): @render manifest path 의 복사본을 render_adapter
    # 산출물로 다시 쓴다 — operational 토큰(subs·이미 sed) 치환. free-form value-fill 은 ADR-0031
    # 로 제거(FILL 채널이 canonical home 전담). substitute·모델해소 *직후*. 범위 = copied_relpaths(비파괴).
    n_render = render_managed_files(dest_root, subs, copied_relpaths)
    if n_render:
        print(f"✓ {n_render} 파일 render (operational 토큰 치환·ADR-0028·ADR-0031)")

    # MF1: board.py init 은 local.conf 를 무조건 덮으므로(local.conf 는 복사/백업 대상 트리
    #      밖), --into 재-import 면 기존 per-clone 설정(external_review·reviewer_cmd·prefix
    #      등)이 무백업 손실된다. init *호출 전*에 백업하고 원본 텍스트를 받아둔다(--new 는
    #      빈 디렉토리 보장이라 None — 보존할 것 없음).
    preserved_conf_text = backup_existing_local_conf(dest_root, backup_root) if not is_new else None

    # SF2: board.py init 비0 이면 local.conf·pm_state 미생성 = import 미완 → 비0 전파.
    rc = run_board_init(dest_root)
    if rc != 0:
        print(f"오류: board.py init 비0 종료({rc}) — import 미완(local.conf·pm_state 확인 필요).",
              file=sys.stderr)
        return rc

    # D11 seam: board.py init 은 project_name 빈값·test_cmd=`pytest -q` 를 하드코딩한다.
    # init 성공 직후 local.conf 의 operational 해소값(project_name·test_cmd·py)을 sed
    # 치환값과 정렬해 엔진 문서(local.conf 해소)와 CLAUDE.md(치환)가 같은 값을 보게 한다.
    if sync_local_conf(dest_root, project_name):
        print("✓ local.conf operational 값 동기화 (project_name·test_cmd·py)")

    # MF1: init 이 덮은 local.conf 위에, 백업해 둔 기존 사용자 키 중 init 이 *안 쓴* 것
    #      (external_review·reviewer_cmd·prefix 등)을 재병합. init/operational sync 값은 우선.
    if preserved_conf_text is not None:
        reapply_preserved_conf_keys(dest_root, preserved_conf_text)

    # T-0053·T-0145: upstream 값을 local.conf 에 upstream= 으로 기록한다. board init·conf
    #   sync·preserve 단계 *이후* 에 둬야 한다 — 그래야 재-import 에서도 preserve 가 stale 값을
    #   붙들지 않고 *현재 값* 으로 확정된다(_set_conf_keys 제자리 갱신). 이후 pm_update 가
    #   --from 생략 시 이 값을 기본 upstream 으로 쓴다(--new·--into 공통).
    #
    #   --from(파일 소스)↔--upstream(update 기록) 디커플(T-0145·ADR-0032 D4):
    #     ① --upstream 명시      → 그 값(URL|경로·이미 입구에서 검증됨).
    #     ② 생략 + --from 이 로컬 git clone → origin URL 자동도출(릴리스 추적 기본).
    #     ③ 생략 + 도출 실패(git repo 아님·origin 부재) → --from 경로(기존 동작 회귀 보존).
    upstream_value = args.upstream
    if upstream_value is None:
        derived = derive_origin_url(source_root)
        upstream_value = derived if derived is not None else str(source_root)
    if record_upstream(dest_root, upstream_value):
        print(f"✓ local.conf upstream 기록 (pm_update --from 기본값): {upstream_value}")

    # upstream_rev baseline 기록(T-0145·T-0141 입력·ADR-0032 D2) — --from 이 로컬 git checkout
    # 이면 그 HEAD commit 을 baseline 으로 박는다("마지막 동기 이후 변경" 의 기준점). git repo
    # 아님·HEAD 해소 실패면 graceful 생략(URL upstream 은 로컬 checkout 이 없어 baseline 없음 —
    # 스킬층이 fetch 후 upstream_seen_rev 를 별도 기록·별개 키).
    baseline_rev = read_upstream_rev(source_root)
    if baseline_rev and record_upstream_rev(dest_root, baseline_rev):
        print(f"✓ local.conf upstream_rev baseline 기록 (drift-lint 기준점): {baseline_rev}")

    # ── opencode 모델 local.conf 기록 (T-0033): board init·conf sync 가 local.conf 를 만든 *뒤*.
    #    실제 모델을 해소한 경로(flag·interactive)만 기록 — 이후 pm_update @render 가
    #    {{OPENCODE_PRO_MODEL}} 을 local.conf 에서 재유도할 때 키 부재로 leak assertion crash 하는
    #    걸 막는다. todo(미해소)는 위 resolve 가 토큰을 주석화+중화(<provider/model>)했으니 기록
    #    안 함(키 없어도 어댑터에 토큰 0 → leak 없음). claude import 는 active=False 라 자연 skip.
    #    (resolve_opencode_model 자체는 render 이전으로 이동·위 substitute 직후 블록 참조.)
    if model_result.active and model_result.path in ("flag", "interactive") \
            and model_result.model:
        if record_opencode_model(dest_root, model_result.model):
            print(f"✓ local.conf opencode_pro_model 기록 ({model_result.model})")

    # ── fill 단계 (T-0009): board init·conf sync 직후 hook. 자유서술 placeholder 처리.
    #    auto + opt-in 게이트 통과 → 하니스 구동 *제안*(파일 미변경, 사람 검토 전제).
    #    그 외(manual 또는 게이트 미통과) → TODO 표시(채택자 손작업 지점 명시).
    #    MF(비파괴): fill 스캔 범위 = substitute_placeholders 와 동일한 copied_relpaths —
    #    이번 import 가 복사한 파일만. --into 에서 복사 안 한 사용자 파일은 절대 스캔/수정 안 함.
    if args.fill == "auto" and live_allowed:
        fill_result = run_fill(dest_root, fill_harness, live=True,
                               copied_relpaths=copied_relpaths)
        if not fill_result.values:
            # 하니스 미구동/실패 → manual 폴백(자유서술이 빈 채로 남지 않게 TODO 표시).
            print(f"  fill=auto 제안 없음({fill_result.note}) — manual 폴백.")
            fill_result = _run_manual_fill(dest_root, copied_relpaths)
    else:
        # --fill auto 라도 게이트 미통과면 실호출 차단 → manual 강제(안전·토큰 0).
        fill_result = _run_manual_fill(dest_root, copied_relpaths)
    _print_fill_result(fill_result, dry_run=False)

    # pm_playbook.local 스텁 생성 (ADR-0007 / T-0028): fill 과 같은 자리 — 인스턴스 소유
    # 누적 학습 칸. 루트 .local 은 manifest 밖이라 복사로 안 오니 여기서 생성한다. 재-import
    # 에서 기존 .local 은 비파괴 보존(누적 학습 손실 방지·local.conf 백업 철학과 같은 결).
    playbook_status = ensure_pm_playbook_local_stub(dest_root, backup_root)
    if playbook_status == "created":
        print(f"✓ pm_playbook.local.md 스텁 생성 ({PM_PLAYBOOK_LOCAL_RELPATH})")
    else:
        print("  pm_playbook.local.md 기존 파일 비파괴 보존 (인스턴스 소유 — 덮지 않음).")

    # T-0034 (should): dest 가 git repo(git_safe is not None)이고 이번에 중앙 백업 디렉토리가
    #   실제로 만들어졌으면, .gitignore 가 `.pm_import_backups/` 를 무시하지 않을 때 1줄 append
    #   — 백업이 git status 를 오염시키지 않게 한다. 비-git/미생성/이미 무시 중이면 skip(멱등).
    if not is_new and git_safe is not None and backup_root is not None and backup_root.exists():
        gi_status = ensure_backup_dir_gitignored(dest_root, git_safe, copied_relpaths)
        if gi_status in ("added", "created"):
            print(f"✓ .gitignore 에 {BACKUP_DIR_NAME}/ 추가 (백업이 git status 오염 방지)")
        elif gi_status == "unsafe-skip":
            print(f"  ⚠️ .gitignore 가 미추적/변경 상태 — 비파괴 위해 자동 추가 생략. "
                  f"수동으로 `{BACKUP_DIR_NAME}/` 한 줄을 추가하세요.")

    print(f"✓ import 완료: {dest_root}")
    print("  다음: 자유서술 placeholder 제안 검토·반영(--fill auto 했으면) + 첫 ticket 발행.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
