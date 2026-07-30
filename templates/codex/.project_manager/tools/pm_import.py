#!/usr/bin/env python3
"""pm_import — PM 프레임워크 import 단일 진입 커맨드 (--new = PM 홈 생성 / --into = 기존 프로젝트 임베드).

현행 채택 플로우(`docs/manual-import.md` 의 수동 longhand: `cp -r` + sed +
`board.py init` + 손)의 **기계 단계**(결정적·무LLM)를 1 커맨드로 대체하고, 그 위에
sed 로 못 채우는 **자유서술 placeholder** 채움(하니스 헤드리스 구동·opt-in)을 얹는다.

사용:
    pm_import.py (--into <기존프로젝트> | --new <프로젝트>)   # 모드 택1
                 --harness {claude,opencode,both,codex}      # 어댑터 (default: claude)
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
  board init·local.conf 동기화 직후 **fill 단계**가 자유서술 placeholder 를 처리한다:
    - --fill manual(기본): 하니스 미구동, placeholder 를 `<!-- TODO: ... -->` 로 표시(채택자가 손으로).
    - --fill auto: 대상 repo 분석 프롬프트로 하니스(claude -p / opencode run --format json /
      codex exec --json)를
      헤드리스 구동해 placeholder 값 + (해당 시) CLAUDE.md/pm_role.local.md 초안을 *제안*한다.
      생성물은 제안일 뿐 — 적용은 사용자 리뷰 전제(비가역 회피). --dry-run 이면 실 하니스를
      호출하지 않고 fill *계획*(채울 대상 토큰·결정된 harness·opt-in 게이트 상태)만 출력한다
      (파일 미변경·비용 0 — opt-in 게이트상 dry-run 에서 실호출 금지).
  --into: 기존 충돌 파일은 중앙 디렉토리 .pm_import_backups/<DATE>/<relpath> 에 백업 후 덮음
          (비파괴). 단 git 이 추적 중이고 미변경인 파일은 백업 생략(git 이 복원). --new:
          디렉토리 생성 + git init.

결정:
  - 독립 pm_import.py (board.py 비대화). stdlib only.
  - idempotent — 재실행 시 백업하고 안전. --dry-run 은 파일시스템 미변경(plan/apply 분리).
  - --weight lite 는 진입 파일 선택만 영향. 어댑터의 `X.lite.md`(예 CLAUDE.lite.md·
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
import stat
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path
from typing import Callable

# ── 엔진 사본 rev 스탬프 (형제 사본 skew fail-loud) ──────────────────────
# Python 하한 probe보다 먼저 평가되므로 3.10에서도 파싱 가능한 문법만 쓴다.
ENGINE_REV = "v1.5.0"


def _verify_engine_rev(sibling_module, sibling_filename):
    """로드한 형제의 baked ENGINE_REV를 이 사본과 대조한다(skew만 fail-loud)."""
    got = getattr(sibling_module, "ENGINE_REV", None)
    if got != ENGINE_REV:
        err = RuntimeError(
            "엔진 사본 버전 불일치 — 로더 %s(rev=%r)가 형제 %s(rev=%r)를 로드했다 "
            "(사본 skew: 부분/수동 복사 또는 구형 사본). `pm-update`(또는 pm_update.py)로 "
            ".project_manager/tools/ 전체를 재동기하라."
            % (Path(__file__).name, ENGINE_REV, sibling_filename, got)
        )
        err._engine_rev_skew = True
        raise err


def _is_engine_rev_skew(exc):
    """fail-soft 소비 지점에서 rev skew만 재-raise하기 위한 구조화 판정."""
    return getattr(exc, "_engine_rev_skew", False)


def _load_min_python() -> tuple[int, int] | None:
    """engine_rev.py 의 하한을 읽되 불완전/구형 사본이면 확인을 건너뛴다."""
    try:
        path = Path(__file__).resolve().with_name("engine_rev.py")
        spec = importlib.util.spec_from_file_location("_engine_rev_python_floor", path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return tuple(mod.MIN_PYTHON)
    except Exception:
        # 부분/구형 사본에서 새 선행검사가 import 자체를 깨지 않게 한다. 정상 배포에서는
        # engine_rev.MIN_PYTHON을 읽고 아래 명시 오류를 유지한다(board._detect_py 동형 fail-soft).
        return None


def _require_python(version_info=None) -> None:
    """tomllib import 전에 현재 인터프리터 하한을 명시적으로 검증한다."""
    current = sys.version_info if version_info is None else version_info
    minimum = _load_min_python()
    if minimum is None:
        return
    if tuple(current[:2]) < minimum:
        major, minor = minimum
        raise SystemExit(
            f"Python {major}.{minor}+ 필요 · 현재 {current[0]}.{current[1]}"
        )


# 반드시 tomllib 보다 먼저 실행한다. 이 파일은 Python 3.10 문법으로 전체 parse 가능하며
# future annotations 덕분에 아래 list[...] / X | None 표기도 체크 전 평가되지 않는다.
_require_python()

import tomllib

REPO = Path(__file__).resolve().parents[2]

# import 어댑터 선택. codex = 세 번째 하네스. `both`(claude+opencode)는 legacy 조합 키로
# 유지하되 **신규 조합 키는 만들지 않는다**(claude+codex 등) — N번째 하네스 공존은 add-harness 채널로
# 통일한다(조합 폭발[7키] 회피·uniform 규칙).
HARNESS_CHOICES = ("claude", "opencode", "both", "codex")
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
# `.opencode/agents/*.md` 의 `model:` 필드로 등장.
# *아니라* `opencode models` 결정적 조회로 해소한다(resolve_opencode_model) — 따라서 fill
# 후보에서 분리됐다(중복·환각 제거). opencode 트리가 복사됐을 때만 해소 단계를 탄다.
OPENCODE_MODEL_TOKEN = "{{OPENCODE_PRO_MODEL}}"

# `opencode models` 조회 명령 — 가용 모델의 단일 진실(LLM 추측 아님). 줄당 `provider/model`.
OPENCODE_MODELS_CMD = ("opencode", "models")

# fill 실 하니스 구동 opt-in 게이트 환경변수 (external_review 선례). PM_IMPORT_LIVE_HARNESS=1
# AND --fill auto 동시 충족 시만 실 runner 호출 — 둘 중 하나라도 없으면 stub/manual 강제.
LIVE_HARNESS_ENV = "PM_IMPORT_LIVE_HARNESS"

# 하니스 헤드리스 구동 명령 (fill auto). claude 는 stdout 캡처, opencode 는 --format json 파싱,
# codex 는 `exec --json` JSONL 파싱(최종 agent_message). codex 는 stdin 미닫힘 시 무기한 대기(실측·
# spike )라 _real_harness_runner 가 stdin=DEVNULL 을 부여하고, -s workspace-write·--skip-git-repo-check·
# -C <dest> 는 _build_runner_argv 가 붙인다. codex 는 `model` 생략=사용자 config 상속(harness-특수 분기 0).
CLAUDE_FILL_CMD = ("claude", "-p")
OPENCODE_FILL_CMD = ("opencode", "run")
CODEX_FILL_CMD = ("codex", "exec")

# 하니스 호출 타임아웃 (초) — repo 분석 1회.
FILL_TIMEOUT_SECONDS = 300

# `opencode models` 조회 타임아웃 (초). 모델 목록 나열은 빠른 로컬 명령 가정이나, 회사 Pro/원격
# 게이트웨이는 cold 콜 지연이 커 15s 로는 부족.
# 기본을 60 으로 올리고, env override 로 환경별 재조정.
# (FILL_TIMEOUT 300 은 LLM 헤드리스 구동용 — 모델 조회엔 과대. --opencode-model 명시 경로의
# 대조-조회가 import UX 를 길게 막지 않도록 fail-soft + 적당한 상한.
OPENCODE_MODELS_TIMEOUT_SECONDS = 60


# env override: 회사 Pro·느린 원격에서 60s 도 모자라면
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
    "codex": ("codex",),
}

# add_harness어댑터 네임스페이스 = {adapter dir(들), root doc}. 라이브 인스턴스에 두 번째
# harness 를 *비파괴로 추가*할 때 복사 스코프 = 이 네임스페이스 ∪ **guest flavor 가 `@render` 로 선언한
# 경로**(cross-ns 의존물 포함) − **host 실소유**. 그 밖(엔진·wiki dev-state·타 harness·
# 설정·파사드·flavor 미선언)은 plan 에 애초에 안 들어와 clobber 가 불가능(Decision 2·5). cross-ns 예:
# opencode 를 codex host 에 추가하면 opencode 가 네이티브 소비하는 `.claude/skills`(`.opencode` 밖·
# flavor `@render` 선언이라 복사·등재된다. host 가 이미 소유한 경로(dest engine.manifest
# core·`_dest_manifest_core_paths`)는 스코프 안이라도 제외한다(중복 레이다운 방지). 단일 harness
# (claude|opencode|codex)만 추가한다('both' 는 최초 import 소관).
#
# 값 shape = **`(adapter_dirs: tuple, root_doc)`**. claude/opencode 는
# 어댑터 dir 가 하나라 단일-원소 튜플(`(".claude",)`)이고, codex 는 네임스페이스가 **둘**로 갈린다 —
# `.codex/`(agents·config·hooks·relay) + `.agents/`(skills·remap 강제) — 이 dual-namespace 가
# 값 shape 일반화를 기계적으로 강제했다(2-튜플 → dirs-튜플). 소비처(_in_adapter_namespace·add_harness
# unpack)는 dirs 를 iterate 한다([[cross-cutting-breaking-blast-radius]] — shape 변경 소비처 선-스코프).
ADD_HARNESS_ADAPTER = {
    "claude": ((".claude",), "CLAUDE.md"),
    "opencode": ((".opencode",), "AGENTS.md"),
    "codex": ((".codex", ".agents"), "AGENTS.md"),
}

# add-harness가 절대 merge/clobber하지 않는 adopter-owned adapter 설정의 단일 정책 지점.
# 값은 template-relative POSIX relpath다. 새 하네스가 사용자 권한·trust·machine-local 설정을
# 추가하면 여기에 명시해 create-if-absent 정책을 함께 받는다. 엔진/어댑터 코드 전체를 보존하는
# broad 예외가 아니라, 권한 경계인 개별 config 파일만 좁게 보호한다.
ADD_HARNESS_CREATE_IF_ABSENT = {
    "claude": frozenset(),
    "opencode": frozenset(),
    "codex": frozenset({"AGENTS.md", ".codex/config.toml", ".codex/hooks.json"}),
}

# 위 정적 경로와 같은 instance-owned 정책 섹션의 조건부 보호 규칙. Codex template은 model을
# 생략해 사용자 기본값을 상속하지만, adopter가 agent별 model/reasoning을 명시하면 그 *파일 전체*가
# override overlay다. 자동 TOML merge 대신 byte 보존한다. pattern/필드 추가는 이 선언만 고친다.
ADD_HARNESS_PRESERVE_EXISTING_TOML_FIELDS = {
    "claude": {},
    "opencode": {},
    "codex": {
        ".codex/agents/*.toml": frozenset({"model", "model_reasoning_effort"}),
    },
}

# sed 치환 대상 operational placeholder (`docs/placeholders.md` 표). 자유서술 3종은 여기 없음(보존).
OPERATIONAL_TOKENS = (
    "{{PROJECT_NAME}}",
    "{{PROJECT_TAGLINE}}",
    "{{PROJECT_ROOT}}",
    "{{PY}}",
    "{{TEST_CMD}}",
    "{{DATE}}",
)

# 치환 대상 판정은 **제외 사유 기반**이다 — 옛 확장자 allowlist(`(".md", ".json", ".sh", ".py")`)는
# allowlist 는 "규칙이 적용될 지점을 사람이 열거" 하는 형상이라, 새 하니스가 새 파일
# 형식을 들여오면 조용히 미커버로 남는다 — codex(세 번째 하니스)의 `.codex/agents/*.toml` 이 정확히
# 그렇게 `{{PROJECT_NAME}}` 을 리터럴로 출하했다. 이제 판정은 뒤집혀 "제외 사유가 있는가" 만 본다
# (`_should_substitute`) → `.yaml`·`.jsonc` 같은 네 번째 하니스의 새 형식도 자동 편입된다.
#
# 제외 사유는 **닫힌 집합**(프레임워크가 소유한 엔진 자산)이라 열거해도 안전하다 — 열거가 위험했던 건
# *열린* 쪽(채택자/하니스가 계속 늘리는 파일 형식)을 열거했기 때문이다:
#   엔진 소스 `.project_manager/tools/**` (`_is_engine_source` — 주석의 토큰은 *설명*·verbatim)
#   엔진 메타데이터 `.project_manager/engine.manifest` (`_is_engine_metadata` — 아래)
#   manifest 파생 방법론 문서 제외집합 (`_dest_sed_exclude` — pm_role·pm_playbook)
#   텍스트로 못 읽는 파일 — 각 치환 루프의 `read_text` UnicodeDecodeError 가 걸러낸다.

# 엔진 메타데이터. engine.manifest 는 채택자에게 *개인화되어* 출하되는
# 산출물이 아니라 엔진이 읽는 기계 설정이고, 그 주석은 placeholder 메커니즘을 *설명*하며 토큰을 담는다
# (예 codex manifest 의 "`.codex/agents` 는 developer_instructions 에 {{PROJECT_NAME}} 토큰 보유 →
# @render"). 치환하면 설명이 concrete 값으로 변질된다 — `.project_manager/tools/**` 주석과 같은 클래스
# (_is_engine_source docstring 참조)이나, tools/ prefix 밖이라 별도 판정이 필요하다.
ENGINE_METADATA_RELPATHS = frozenset({
    ".project_manager/engine.manifest",
})

# operational placeholder 치환에서 *제외*하는 방법론 문서 (repo 기준 relpath) — engine.manifest 파생.
# 하드코딩 목록(과거 pm_role.md·pm_playbook.md 리터럴 frozenset) 대신 manifest 의 `.project_manager/wiki/`
# 직속 비-템플릿 `.md`(= 방법론 문서 절)에서 결정적으로 유도한다. 이 문서들은 `{{PROJECT_NAME}}`·
# `{{DATE}}` 토큰을 *메커니즘 설명*으로 담아(placeholder 아님·local.conf 가 런타임 해소·`docs/placeholders.md`)
# 치환하면 문서가 concrete 값으로 변질되므로 제외한다. 파생이라 신규 방법론 .md 가 manifest 에 추가되면
# 자동 편입 — "목록 수동 추가 잊음 → 조용한 placeholder 오치환" 클래스 종결.
#
# ⚠️ 파생 기준 manifest 는 **모듈-import 시점(실행 checkout)이 아니라 치환 시점의 dest 인스턴스**
# (`dest_root/.project_manager/engine.manifest`)다. pm_import 는
# `--from <다른 framework checkout>` 에서 복사할 수 있어(구버전 로컬 도구가 신버전 upstream 을 흡수하는
# 실 운영 경로), *실제 복사되는 쪽* manifest 가 기준이어야 upstream 진화(신규 직속 방법론 문서)가 자동
# 편입된다 — 모듈-시점 상수는 실행 checkout 의 manifest 에 묶여 이 보장을 잃는다. 치환은 복사 *이후*
# 단계라 dest manifest 는 그 시점에 실재한다(manifest = self-prop 복사 대상). 각 치환 call site 는 아래
# `_dest_sed_exclude(dest_root)` 로 dest 기준 제외 집합을 산출해 `_should_substitute` 에 넘긴다.

# manifest 부재·로드 실패(broken-manifest)에서도 기존 제외를 조용히 잃지 않게 하는 리터럴 floor
# (should-fix). 정상 경로(manifest 파싱 성공)는 파생 결과를, 실패 경로만 이 floor 를 반환한다.
SED_EXCLUDE_FLOOR = frozenset({
    ".project_manager/wiki/pm_role.md",
    ".project_manager/wiki/pm_playbook.md",
})


def _is_template_scaffold(filename: str) -> bool:
    """파일명이 템플릿 스캐폴드 관례(`*.template.md`·`_template.md`·`*_template.md`)인가.

    템플릿(예: pm_state.template.md)은 `{{DATE}}` 등 토큰을 *렌더 대상*(도구·skill 이 새 산출물
    생성 시 채움)으로 담아, 토큰을 *설명*으로 담는 방법론 문서(pm_role·pm_playbook)와 성격이 다르다.
    치환-제외 집합엔 넣지 않는다(현행 동작 보존 — 템플릿의 operational 토큰은 import 치환 대상)."""
    stem = filename[:-len(".md")] if filename.endswith(".md") else filename
    return stem.endswith(".template") or stem == "_template" or stem.endswith("_template")


def _derive_sed_exclude_relpaths(manifest_path: Path) -> frozenset:
    """치환-제외 방법론 문서(repo 기준 relpath·POSIX) 집합을 engine.manifest 에서 파생한다.

    규칙 = manifest 엔트리 중 `.project_manager/wiki/` **직속** `.md` 파일 중 *템플릿이 아닌* 것.
    서브디렉토리(`tickets/_template.md`·`raw/spikes/_template.md`·`domain/_template.md`)는 "직속"
    조건으로, 직속 템플릿(`pm_state.template.md`)은 `_is_template_scaffold` 로 제외된다 — 현재
    산출은 정확히 {pm_role.md, pm_playbook.md}.

    manifest 파싱은 pm_update.read_manifest 재사용(새 파서 신설 없음·_render_managed_relpaths 동형).
    manifest 부재·로드 실패 → 리터럴 floor SED_EXCLUDE_FLOOR (fail-soft — 빈 집합이면 broken-manifest
    엣지에서 pm_role·pm_playbook 이 조용히 오치환되므로 기존 제외를 floor 로 보장·should-fix). 정상
    파싱은 파생 결과를 그대로 반환한다(manifest 가 방법론 문서를 명시적으로 뺐다면 그 판단을 존중)."""
    wiki_prefix = ".project_manager/wiki/"
    if not manifest_path.is_file():
        return SED_EXCLUDE_FLOOR
    pm_update_py = Path(__file__).resolve().parent / "pm_update.py"
    try:
        spec = importlib.util.spec_from_file_location("pm_update", pm_update_py)
        if spec is None or spec.loader is None:
            return SED_EXCLUDE_FLOOR
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        entries = mod.read_manifest(manifest_path)
    except Exception:  # noqa: BLE001 — 로드/파싱 실패는 floor 로 폴백(기존 제외 보존·should-fix).
        return SED_EXCLUDE_FLOOR
    out: set = set()
    for entry in entries:
        rel = str(entry).replace("\\", "/")
        if not rel.startswith(wiki_prefix):
            continue
        remainder = rel[len(wiki_prefix):]
        if "/" in remainder:  # 서브디렉토리 — 방법론 문서 절이 아님(직속만)
            continue
        if not remainder.endswith(".md"):
            continue
        if _is_template_scaffold(remainder):  # 템플릿 스캐폴드 — 치환 대상(제외 아님)
            continue
        out.add(rel)
    return frozenset(out)


def _dest_sed_exclude(dest_root: Path) -> frozenset:
    """dest 인스턴스의 engine.manifest 기준 치환-제외 집합 (치환 시점 산출·codex must-fix).

    모듈-import 시점(실행 checkout)이 아니라 *복사가 끝난 dest* 의 manifest 에서 파생한다 — pm_import
    는 `--from <다른 checkout>` 에서 복사할 수 있어, 실제 dest 에 실린 manifest 가 기준이어야 upstream
    진화(신규 직속 방법론 문서)가 자동 편입된다. manifest 는 self-prop 복사 대상이라 치환(복사 이후)
    단계엔 dest 에 실재한다. 부재/실패는 SED_EXCLUDE_FLOOR(fail-soft)."""
    return _derive_sed_exclude_relpaths(dest_root / ".project_manager" / "engine.manifest")

# tracked-only 출하 목록 위에서도 복사 정책과 fill 스캔의 공통 방어를 보존하려고 유지한다.
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

# --into 백업 중앙화 디렉토리. 충돌 파일별 형제 `*.backup.<DATE>` 를 트리 전역에
# 흩뿌리는 대신, 무백업 덮기 불가(미추적·dirty·비-git)인 파일만 단일 디렉토리
# `<dest>/.pm_import_backups/<DATE>/` 에 relpath 미러링으로 모은다. git 이 추적 중이고
# 미변경인 파일은 git 이 내용을 보존하므로 백업 없이 덮는다(git-safe skip).
BACKUP_DIR_NAME = ".pm_import_backups"

# `git` 호출 seam — argv(list) → (returncode, stdout). 테스트가 stub 주입(라이브 git 미실행).
# git_safe_relpaths 가 work tree 판별·추적집합 조회에 사용한다(_real_models_runner 류 결정적 seam).
GitRunner = Callable[[list], "tuple[int, str]"]

# git 호출 타임아웃 (초) — ls-files/status 는 빠른 로컬 명령이라 짧게(과대 대기 방지·fail-soft 상한).
GIT_SAFE_TIMEOUT_SECONDS = 15

# upstream git 호출(ls-remote·remote get-url·rev-parse) 타임아웃 (초). ls-remote 는
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
    ("http.followRedirects", "false"),  # redirect 추적 차단(잔여 SSRF 표면).
)

# 인터프리터 탐지는 board.py 의 _detect_py() 가 단일 진실. pm_import 가 자체
# 탐지를 신설하지 않고 board.py 를 재사용한다 — 플랫폼별 python3/python 해석을 한 곳에 둔다.
# board.py import 가 실패하면(예: yaml 부재) 리눅스 현행과 동치인 "python3" 로 폴백.
_DEFAULT_PY_FALLBACK = "python3"


def _detected_py() -> str:
    """{{PY}} 치환·local.conf py= 기본값으로 쓸 인터프리터 명령을 board.py 에서 탐지한다.

    board.py 의 _detect_py() 를 import 해 재사용(단일 진실). board.py 와 같은 디렉토리에
    있으므로 spec_from_file_location 으로 직접 로드 — sys.path 오염 없이 호출 가능.
    어떤 이유로든 로드/호출이 실패하면 "python3" 폴백(리눅스 현행 동치).
    """
    # 같은 canonical tools 디렉토리의 board.py라도 부분/수동 복사로 rev가 어긋날 수 있으므로
    # 로드 직후 verify한다. 일반 로드 실패는 폴백하되 marked skew만 아래에서 재-raise한다.
    board_py = Path(__file__).resolve().parent / "board.py"
    try:
        spec = importlib.util.spec_from_file_location("_board_for_detect_py", board_py)
        if spec is None or spec.loader is None:
            return _DEFAULT_PY_FALLBACK
        board_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(board_mod)
        _verify_engine_rev(board_mod, board_py.name)
        return board_mod._detect_py()
    except Exception as exc:  # noqa: BLE001 — 탐지 실패는 폴백, skew만 fail-loud.
        if _is_engine_rev_skew(exc):
            raise
        return _DEFAULT_PY_FALLBACK


def _default_test_cmd() -> str:
    """기본 test_cmd — 탐지된 인터프리터로 pytest 실행 (Windows 에선 `python`, POSIX 는 python3).

    상수 하드코딩(`python3 -m pytest`)은 Windows 에서 깨진다(`python3`=비기능 shim 또는
    엉뚱한 Store Python). `_detected_py()` 를 경유해 board.py `_detect_py()` 의 실행검증된
    인터프리터를 쓴다 — local.conf `py=` 와 동일 소스라 일관.
    """
    return f"{_detected_py()} -m pytest tests/ -q"


DEFAULT_TAGLINE = "한 줄 프로젝트 설명"


# pm_playbook.local.md 스텁 본문.
# 단일 소스 = 이 인라인 상수. 루트 pm_playbook.local.md는 manifest 밖이라 템플릿
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

# 스텁 대상 경로 (dest_root 기준 relpath). 루트 seam과 동일 위치.
PM_PLAYBOOK_LOCAL_RELPATH = Path(".project_manager") / "wiki" / "pm_playbook.local.md"


# ── git-safe 판정 (LLM 아님·결정적) ──────────────────────────────
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


# ── upstream URL 안전 계약 + self-describing 분류 ──────────
# upstream 값은 git URL *또는* 로컬 경로다 — self-describing(모양으로 분기). git 을
# 호출하는 모든 경로(ls-remote·remote get-url·rev-parse)가 이 계약을 지킨다:
#   - argv-list(no shell·_real_git_runner 가 항상 list 전달) · leading-dash 거부(옵션 오인)
#   - protocol allowlist(https/ssh/file 명시) · credential-in-URL 거부(SSRF·자격증명 누출)
#   - scp-style(user@host:path)/Windows(C:\)/상대/모호 colon 분기로 URL↔경로 정확 판별
# 비대화 auth(GIT_TERMINAL_PROMPT=0)·timeout 은 git 호출 runner(_real_upstream_git_runner)가
# 강제한다. 이 검증 자체는 *순수 함수*(네트워크 0) — 도달성은 ls-remote 호출부가 따로 본다.

# URL scheme allowlist — https/ssh/file *만*. http(평문)·git://(비인증
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
    """upstream 값을 self-describing 으로 분류한다 — 'url' | 'path'.

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
    """upstream 값의 *순수* 안전 검증 (네트워크 0·도달성은 별도). (ok, reason).

    git 을 호출하기 *전* 입구 가드 — fail-closed(나쁜 값은 거부, silently 기록 금지
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
    """upstream git 호출(ls-remote·remote get-url·rev-parse)용 GitRunner (fail-soft).

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
    """로컬 git checkout 의 `git remote get-url origin` 을 읽어 URL 을 도출한다.

    로컬 clone 을 `--from` 으로 받았을 때, future update 기록(`--upstream` 생략 시)을 *그
    checkout 경로* 대신 origin URL 로 자동도출하는 데 쓴다(릴리스 추적 기본).
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
    """로컬 git checkout 의 `git rev-parse HEAD` 를 읽는다 — drift baseline.

    `upstream_rev=<commit>` baseline 기록의 입력이다. checkout_root 가 가리키는
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
    테스트 결정적(`shutil.which("opencode")`·`_real_models_runner` 동일 seam 철학).

    ⚠️ 경로 기준 정규화: `git -C <dest> ls-files` 는 **cwd(=dest_root) 상대**,
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

    backup:
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
        # lite 배치: src 가 `X.lite.md` 인데 dst 가 `X.md` 면 이름이 치환됐다 —
        #   어느 변종이 어디로 가는지 보이게 한다("CLAUDE.lite.md → CLAUDE.md (lite)").
        lite_note = ""
        if self.src.name.endswith(LITE_SUFFIX) and self.src.name != self.dst.name:
            lite_note = f"  ({self.src.name} → {self.dst.name}, lite)"
        # 3분기: 신규([copy]) · 충돌 git-safe(백업 생략 [copy · git-safe]) ·
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
        #      밖일 수 있음) 파일을 백업/덮어쓴다 — 비파괴 보장 위반 + 외부 파일 변조 위험.
        #      따라서 symlink 는 *링크 자체*를 처리한다: 링크를 그대로 백업(follow 안 함) →
        #      링크 unlink → 일반 파일로 src 복사. 링크 대상 파일은 절대 건드리지 않는다.
        dst_is_symlink = self.dst.is_symlink()
        if self.backup is not None and (self.dst.exists() or dst_is_symlink):
            # SF1: 백업 경로가 이미 존재하면(같은 날 재실행 등) 덮지 말고 순번 부여 —
            #      가장 오래된 원본(=진짜 사용자 파일)이 살아남게 한다.
            target = _free_backup_path(self.backup)
            # 백업이 중앙 디렉토리(relpath 미러)이므로 부모 디렉토리를 먼저 만든다.
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.dst, target, follow_symlinks=False)
        if dst_is_symlink:
            # 링크 자체를 제거(대상 파일 불변) — 이후 일반 파일로 덮어쓴다.
            os.unlink(self.dst)
        self.dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.src, self.dst)


class FileVsDirConflict(Exception):
    """dst 위치에 기존 *디렉토리* 가 있어 파일 복사가 불가능.

    src 는 파일인데 dst 가 디렉토리면 shutil.copy2 가 IsADirectoryError 로 터지고, 백업도
    안 된다(디렉토리는 copy2 대상 아님). 비파괴 보장상 사용자 디렉토리를 자동 삭제할 수 없으니
    plan 단계에서 명시적으로 거부한다(apply 부분 복사 전 차단).
    """


class AncestorConflict(Exception):
    """dst 의 *조상* 경로(dest_root 하위)에 symlink·비-디렉토리 파일이 있어
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


# lite 진입 파일 관례: `X.lite.md` 는 진입 `X.md` 의 lite 변종이다.
# (예: CLAUDE.lite.md → CLAUDE.md, AGENTS.lite.md → AGENTS.md.) 임의의 `*.lite.md` 에 일반화.
LITE_SUFFIX = ".lite.md"


class SkippedTemplateShippingEntryWarning(RuntimeWarning):
    """pm-import가 byte-copy할 수 없는 템플릿 엔트리를 명시적으로 제외했다는 신호."""


class EmptyTemplateShippingInventoryError(RuntimeError):
    """존재하는 출하 템플릿의 tracked 인벤토리가 0건인 결함."""

    def __init__(
            self, checkout: Path, subtree: str = ".",
            *, filesystem_fallback: bool = False) -> None:
        self.checkout = Path(checkout)
        self.subtree = subtree
        self.filesystem_fallback = filesystem_fallback
        diagnosis = (
            "filesystem 강등 상태이므로 소스 디렉토리가 비었는지와 checkout 루트가 "
            "올바른지 확인하라"
            if filesystem_fallback
            else "checkout 루트가 올바른지와 git index에 출하 파일이 등재됐는지 확인하라"
        )
        super().__init__(
            "pm-import 출하 인벤토리가 0건임 "
            f"(checkout={self.checkout}, subtree={subtree!r}); "
            + diagnosis
        )


def _full_relpath_for_lite(rel: Path) -> Path:
    """lite 변종 relpath `X.lite.md` → full 진입 relpath `X.md` (이름 치환).

    `X.lite.md` 만 매핑한다(이름이 정확히 `.lite.md` 로 끝나는 경우). 반환 relpath 는
    같은 디렉토리의 `X.md` 다 — lite 배치 시 dst 가 full 진입 이름으로 들어가게 한다.
    """
    base = rel.name[: -len(LITE_SUFFIX)]  # 'X.lite.md' → 'X'
    return rel.with_name(base + ".md")


def _shippable_template_files(repo_files, template_root: Path) -> list[Path]:
    """repo 출하 엔트리를 링크를 추종하지 않는 일반 파일 목록으로 좁힌다.

    git 인벤토리는 index mode가 진실이므로 일반 파일 mode만 허용한다. 비-git 폴백은 mode를
    얻을 수 없어 그 경로에서만 lstat()으로 등가 판정한다. 두 판정을 한 엔트리에 함께 적용하면
    index와 working tree 중 어느 쪽이 출하 계약인지 갈리므로 의도적으로 섞지 않는다.

    0건 판정은 복사 계획 수가 아니라 이 원시 열거 수에 둔다. ``plan_copy``는 목적지 byte를
    비교하지 않아 동일 트리 재-import도 모든 출하 파일을 다시 계획하므로 정당한 zero-action
    no-op이 없고, 빈 원시 인벤토리만 출하 결함이다.
    """
    accepted: list[Path] = []
    skipped: dict[str, list[str]] = {
        "symlink(index mode 120000, 링크 대상 내용을 복사하지 않음)": [],
        "gitlink(index mode 160000, 파일 byte-copy 대상 아님)": [],
        "지원하지 않는 git index mode": [],
        "filesystem 폴백 symlink(lstat, 링크 대상 내용을 복사하지 않음)": [],
        "filesystem 폴백 일반 파일이 아닌 엔트리(lstat)": [],
        "filesystem 폴백 lstat 실패": [],
    }
    entries = repo_files.list_repo_owned_entries(
        template_root, ".", mode=repo_files.TRACKED_ONLY
    )
    if not entries:
        runner = repo_files._real_git_runner(template_root)
        probe_rc, inside = runner(["rev-parse", "--is-inside-work-tree"])
        filesystem_fallback = not (
            probe_rc == 0 and inside.strip() == "true")
        # seam의 빈 결과는 부분 subtree 질의에는 유효할 수 있다. 그러나 선택된 템플릿은
        # 프레임워크 출하 단위이므로 소비 지점에서 직접 막아 warning의 조건부 발화에 기대지 않는다.
        raise EmptyTemplateShippingInventoryError(
            template_root, ".", filesystem_fallback=filesystem_fallback)
    for entry in entries:
        rel = entry.path
        source = template_root / rel
        index_mode = entry.index_mode
        if index_mode in {"100644", "100755"}:
            accepted.append(source)
            continue
        if index_mode == "120000":
            skipped["symlink(index mode 120000, 링크 대상 내용을 복사하지 않음)"].append(
                rel.as_posix()
            )
            continue
        if index_mode == "160000":
            skipped["gitlink(index mode 160000, 파일 byte-copy 대상 아님)"].append(
                rel.as_posix()
            )
            continue
        if index_mode is not None:
            skipped["지원하지 않는 git index mode"].append(
                f"{rel.as_posix()} ({index_mode})"
            )
            continue

        # filesystem 폴백에는 index mode가 없으므로 여기서만 링크 비추종 lstat 판정을 쓴다.
        try:
            fallback_mode = source.lstat().st_mode
        except OSError as exc:
            skipped["filesystem 폴백 lstat 실패"].append(
                f"{rel.as_posix()} ({exc})"
            )
            continue
        if stat.S_ISREG(fallback_mode):
            accepted.append(source)
        elif stat.S_ISLNK(fallback_mode):
            skipped[
                "filesystem 폴백 symlink(lstat, 링크 대상 내용을 복사하지 않음)"
            ].append(rel.as_posix())
        else:
            skipped["filesystem 폴백 일반 파일이 아닌 엔트리(lstat)"].append(
                rel.as_posix()
            )

    for reason, paths in skipped.items():
        if paths:
            warnings.warn(
                f"pm-import 템플릿 출하 엔트리 {len(paths)}건 제외 — {reason}: "
                + ", ".join(paths),
                SkippedTemplateShippingEntryWarning,
                stacklevel=2,
            )
    return accepted


def _iter_source_files(template_root: Path, weight: str = "full"):
    """template_root의 repo 추적 파일을 (dst relpath, 절대경로)로. 정책 경로 추가 제외.

    weight 관례 (`*.lite.md` = `*.md` 의 lite 변종):
      - full(기본): 모든 `*.lite.md` 를 복사 대상에서 *제외*한다(lite 변종이 full 배포에
        끼면 안 됨). full `X.md` 는 그대로 복사.
      - lite: 각 `X.lite.md` 를 dst relpath `X.md` 로 복사(이름 치환). 동시에 (a) 같은
        트리의 full `X.md` 는 복사 제외(lite 가 그 자리를 차지), (b) 원본 이름 `X.lite.md`
        도 그대로는 복사 제외(dst 에 `*.lite.md` 잔존 금지).
    yield 하는 relpath 는 *dst* 기준이므로 lite 모드에선 `X.md` 로 치환돼 나간다 —
    placeholder 치환(copied_relpaths 기준)·both 충돌 판정이 이 dst relpath 위에서 돈다.

    출하 인벤토리는 공용 repo_owned_files seam의 TRACKED_ONLY 의미를 쓴다. 따라서 유지보수자
    checkout의 미추적·machine-local 파일은 복사 후보가 아니며, 비-git 소스에서 filesystem
    폴백할 때는 seam이 RepoFilesFallbackWarning으로 추적 보장 소실을 loud하게 알린다.
    COPY_EXCLUDE_*는 추적 여부와 별개인 출하 정책(node_modules·stale bytecode·내부 README)과
    fill 스캔 공통 방어를 위해 그 위에 계속 적용한다.
    """
    repo_files = _load_repo_owned_files()
    files = [
        path
        for path in _shippable_template_files(repo_files, template_root)
        if not any(
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

    이미 존재하는 조상 컴포넌트가 symlink 이거나 비-디렉토리 파일이면
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
    skip_existing_relpaths: set[Path] | None = None,
    include_relpath: Callable[[Path], bool] | None = None,
) -> list[CopyAction]:
    """어댑터 트리들 → dest 복사 액션. both 면 여러 트리 병합(relpath 유일하면 충돌 0).

    backup_root: None 이면 백업 안 함(--new — 빈 디렉토리 보장). 비-None 이면 기존 충돌
    파일을 *중앙 디렉토리* `backup_root/<relpath>` 로 백업(--into). 형제 `*.backup.<DATE>`(트리
    전역 분산) 대신 단일 디렉토리로 모은다.

    git_safe: git_safe_relpaths 의 반환 — '추적 중 & 미변경' relpath(posix) 집합 또는
    None. None 이면 git 판정 불가(비-git·오류) → 모든 충돌을 백업(보수적). 집합이면 그 안의
    relpath 는 git 이 복원 가능하므로 백업 없이 덮는다(git-safe skip — 액션 _git_safe_skip 표시).

    weight: 'full'(기본) 이면 `*.lite.md` 를 제외, 'lite' 면 `X.lite.md` 를
    dst `X.md` 로 rename 복사(같은 트리 full `X.md` 제외). _iter_source_files 가 이 관례를
    적용해 dst relpath 를 산출하므로, 아래 both 중복 판정·치환 범위는 모두 *dst relpath*
    위에서 일관되게 돈다(lite 모드에선 `X.md` 가 dst — both 양 트리가 각자 lite 변종을 깐다).

    MF3: 여러 선택 트리에서 같은 relpath 가 중복되면(예: 공유 엔진), **내용이 같을
    때만** 조용히 skip 한다. 내용이 *다르면*(예: engine.manifest·README.md — claude_code 는
    .claude/agents·skills·regression.yml 을 sync 범위에 포함, opencode 는 제외) 첫 트리
    (template_roots의 CLI 선언 순서)를 우선하되 stderr 경고를 남긴다. 이 우선순위는 결정적
    충돌 해소 정책이지 첫 트리가 나머지의 상위집합이라는 전제를 두지 않는다. 단,
    engine.manifest는 복사 뒤 ``_install_selected_manifest_union``이 선택 트리 선언의 합집합으로
    다시 쓴다. (lite 진입 CLAUDE.md / AGENTS.md 는 트리별로 dst relpath 가
    달라 — claude→CLAUDE.md, opencode→AGENTS.md — 충돌하지 않는다.)
    """
    seen: dict[Path, tuple[Path, str]] = {}  # relpath → (채택된 src, 채택 트리명)
    skip_existing_relpaths = skip_existing_relpaths or set()
    actions: list[CopyAction] = []
    checked_ancestors: set[Path] = set()  # 검증 완료 조상 캐시(중복 검사 회피).
    for template_root in template_roots:
        for rel, src in _iter_source_files(template_root, weight):
            # add_harness처럼 전체 template 중 일부 namespace만 배포할 때는, 범위 밖 파일의
            # conflict/backup/ancestor safety 검증도 만들지 않는다. 최종 action 필터보다 앞에 둬
            # 무관한 engine 파일이 backup root 검증을 발화시키지 않게 한다.
            if include_relpath is not None and not include_relpath(rel):
                continue
            if rel in seen:
                prev_src, prev_tree = seen[rel]
                if _same_bytes(prev_src, src):
                    # 공유 엔진 등 byte-identical — 한 번만 복사(정상).
                    continue
                # 내용이 다른 중복 — 첫 트리(우선) 채택을 명시적으로 경고.
                print(
                    f"경고: 선택 트리 중복 relpath 내용 불일치 — '{rel.as_posix()}' 는 "
                    f"선언 순서상 첫 트리 '{prev_tree}' 것을 우선함. "
                    f"후순위 트리: '{template_root.name}'.",
                    file=sys.stderr,
                )
                continue
            seen[rel] = (src, template_root.name)
            dst = dest_root / rel
            # caller가 create-if-absent로 선언한 기존 파일은 일반 backup/action/ancestor safety
            # 계산 전에 action 자체를 만들지 않는다. instance-owned config는 backup이 있어도
            # overwrite 권한이 생기지 않으며, 손상된 backup root가 그 보존 분기까지 막아서도 안 된다.
            if rel in skip_existing_relpaths and (dst.exists() or dst.is_symlink()):
                continue
            # dst 조상(dest_root 하위)이 symlink·비-디렉토리 파일이면 거부 —
            #      링크 follow 로 프로젝트 밖 쓰기 / apply 중 mkdir 실패 부분복사 방지.
            _check_ancestor_safe(dest_root, dst, checked_ancestors)
            # dst 가 (symlink 아닌) 디렉토리면 파일 복사·백업 불가 — plan 에서
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
                # git 이 추적 중이고 미변경인 파일은 git 이 복원 가능 → 백업 생략(덮기만).
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
                    # 백업 target 의 *전체* 조상 체인
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


def _prepare_selected_manifest_union(template_roots: list[Path]) -> dict | None:
    """선택 template tree의 manifest 합집합을 읽기 전용으로 사전 검증한다.

    ``plan_copy``와 동일한 ``template_roots`` 순서를 단일 선택 트리로 소비한다. 경로/하네스별
    예외는 없고 각 트리의 manifest 선언을 합치므로, 향후 임의 조합도 같은 집합 의미를 그대로
    재사용할 수 있다. 복사 apply 전에 호출해 후순위 manifest 부재/파싱·병합 실패가 부분 설치를
    남기지 않게 한다. 단일 트리는 합집합 write가 불필요하므로 None을 반환한다.
    """
    manifest_paths = [
        root / ".project_manager" / "engine.manifest"
        for root in template_roots
    ]
    if len(manifest_paths) < 2:
        return None
    pu = _load_pm_update()
    if pu is None:
        raise RuntimeError("pm_update.py를 로드할 수 없어 선택 manifest 합집합을 만들 수 없습니다.")
    return pu.merge_manifest_sources(manifest_paths)


def _install_selected_manifest_union(merged: dict | None, dest_root: Path) -> int:
    """사전 검증된 선택 manifest 합집합을 설치한다(여기서는 source를 다시 읽지 않는다)."""
    if merged is None:
        return 0
    target = Path(dest_root) / ".project_manager" / "engine.manifest"
    target.write_text(merged["text"], encoding="utf-8")
    return len(merged["entries"])


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
    제외해 verbatim 으로 둔다 (pm_render/pm_update 가 토큰을 문서화하며 표면화).

    rel 은 dest-rel(`​.project_manager/tools/board.py`) 또는 절대/소스 경로(dry-run plan 의
    `action.dst`/`action.src`) 둘 다 올 수 있어 substring 매칭으로 통일한다(`tools/` 트레일링
    슬래시로 `tools_backup` 등 오탐 방지)."""
    p = rel.as_posix()
    return p.startswith(".project_manager/tools/") or "/.project_manager/tools/" in p


def _is_engine_metadata(rel: Path) -> bool:
    """엔진 메타데이터(`.project_manager/engine.manifest`)인가 — 치환 제외 대상.

    _is_engine_source 와 같은 클래스(엔진 소유·토큰이 *설명*)이나 `.project_manager/tools/` prefix
    밖이라 별도 판정이다. rel 은 dest-rel 또는 절대/소스 경로 둘 다 올 수 있어 `_is_engine_source`
    와 같은 suffix 매칭으로 통일한다(경로 구분자 정규화)."""
    p = rel.as_posix()
    return any(p == m or p.endswith("/" + m) for m in ENGINE_METADATA_RELPATHS)


def _should_substitute(rel: Path, exclude_relpaths: frozenset) -> bool:
    """이 파일이 operational placeholder 치환 대상인가 — **제외 사유가 없으면 대상**.

    옛 판정은 확장자 allowlist(`SUBSTITUTE_SUFFIXES`) gate 였다: 열린 집합(하니스가 계속 늘리는
    파일 형식)을 사람이 열거한 형상이라, codex 가 들여온 `.codex/agents/*.toml` 이 어느 채널도
    못 타고 `{{PROJECT_NAME}}` 을 리터럴로 출하했다. 판정을 뒤집어 **닫힌 집합(엔진 소유 자산)의
    제외 사유**만 본다 → 네 번째 하니스의 새 형식(`.yaml`·`.jsonc`)도 자동 편입된다.

    제외 사유(모듈 상단 주석과 동기):
      방법론 문서 — exclude_relpaths = dest 인스턴스 manifest 파생 치환-제외 집합
         (`_dest_sed_exclude`). 호출부가 치환 시점에 dest 기준으로 산출해 넘긴다(모듈-시점 상수
         아님).
      엔진 소스(`.project_manager/tools/**`) — verbatim.
      엔진 메타데이터(`engine.manifest`) — 주석이 토큰 메커니즘을 *설명*.
      텍스트로 못 읽는 파일 — 이 판정이 아니라 호출부 `read_text` 의 UnicodeDecodeError 가
         걸러낸다(파일 내용 없이는 판정 불가·현 구조 유지).

    범위(어떤 파일이 이 판정에 오는가)는 넓히지 않는다 — 호출부는 전부 `copied_relpaths`
    (이번 run 이 실제 복사한 파일)로 한정된다(`substitute_placeholders` docstring MF1 비파괴)."""
    if rel.as_posix() in exclude_relpaths:
        return False
    if _is_engine_source(rel):  # 엔진 소스(.py)는 verbatim — 주석의 토큰-문서는 placeholder 아님
        return False
    if _is_engine_metadata(rel):  # engine.manifest 주석의 토큰-문서도 placeholder 아님
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
    무백업 치환되어 --into 비파괴 보장을 위반한다. 따라서 범위를 copied_relpaths(plan_copy
    가 만든 actions 의 dst relpath)로 엄격히 한정한다. 복사된 파일은 충돌 시 이미 백업됐으므로
    치환해도 안전하고, 복사 안 한 사용자 파일은 절대 건드리지 않는다.

    값이 빈 문자열(`""`/`None`)인 subs 는 치환하지 않는다 — `replace(token, "")` 로 토큰을
    silent 로 비우면(예: 빈 project_name → " 프로젝트") 미해소 탐지 신호가 사라진다(잔여 토큰보다
    나쁨). 토큰을 남기면 @render path 는 이후 render_managed_files 의 _assert_no_leak 가 leak 으로
    잡고(같은 subs 를 render 채널에도 넘겨 빈값 힌트까지 표면화), 비-@render path 는 리터럴 토큰이
    가시적으로 남아(침묵 비움 아님) 사람이 즉시 알아챈다. 이 함수는 render *이전* 단계라
    빈값이 render 가드 도달 전에 이미 지워졌다(codex must-fix — 최초 import 경로 사각).
    """
    changed = 0
    sed_exclude = _dest_sed_exclude(dest_root)  # 치환 시점·dest manifest 기준(codex must-fix)
    for rel in sorted(copied_relpaths):
        if any(part in COPY_EXCLUDE_DIR_NAMES for part in rel.parts):
            continue
        if not _should_substitute(rel, sed_exclude):
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
            if value is None or value == "":
                # 빈값 subs 는 치환하지 않는다 — 토큰을 그대로 남겨 이후 render 단계
                # (render_managed_files)의 _assert_no_leak 가 leak 으로 잡게 한다(silent-empty =
                # leak 클래스). `replace(token, "")` 는 미해소를 *침묵 비움*(예:
                # `{{PROJECT_NAME}}`→"" → description 이 " 프로젝트")으로 박제해 탐지 신호 자체를
                # 없앤다 — 잔여 토큰보다 더 나쁘다. subs 는 이후
                # render_managed_files 에도 그대로 전달돼 render 채널이 같은 빈값을 힌트로 표면화한다.
                continue
            if token in new_text:
                new_text = new_text.replace(token, value)
        if new_text != text:
            path.write_text(new_text, encoding="utf-8")
            changed += 1
    return changed


# ── render 단계 ─────────────────────────────────
# @render manifest path 의 어댑터 파일은 import 도 pm_update 와 같은 render 경로를 탄다 —
# 복사 후 operational 토큰(local.conf — PROJECT_NAME·TEST_CMD 등)을 render_adapter 로 치환한다
# (pm_render 공유).
# free-form 은 canonical home(root doc·
# pm_role.local.md)의 FILL 채널이 전담. substitute(operational) *이후* 에 둬 일관 처리.

def _load_pm_render_module():
    """pm_render 모듈을 같은 tools/ 디렉토리에서 로드 (board.py 로더 패턴 동형·sys.path 무오염).

    실패 시 None 반환 — **호출부마다 처리가 다르다**: `_mark_model_todos`(모델 토큰 중화는
    안전 출하의 load-bearing 계약)는 중화 불가를 조용히 넘기지 않고 **fail-loud**(raise) 하고,
    `render_managed_files`/@render 처리는 skip(검사 대상 0·무동작·render path 토큰 잔존 시 board
    render-leak lint 가 backstop). pm_render 는 co-located 엔진이라 정상 설치에선 항상 로드된다 —
    로드 실패는 broken install 신호다."""
    render_py = Path(__file__).resolve().parent / "pm_render.py"
    try:
        spec = importlib.util.spec_from_file_location("pm_render", render_py)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _verify_engine_rev(mod, render_py.name)
        return mod
    except Exception as exc:  # noqa: BLE001 — 일반 로드 실패만 render 단계 skip(무동작).
        if _is_engine_rev_skew(exc):
            raise
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


def _engine_render_relpaths(root: Path) -> set[str]:
    """트리의 engine.manifest 에서 add_harness 가 *복사 제외*할 native-@render 엔진 리소스 relpath 집합.

    add_harness 스코프 제외용. `@render` 만 있고 `@target-owned`·
    `@source` 가 *둘 다 없는* path = 루트 upstream 에 manifest-경로 그대로 실재하는 native 엔진 리소스
    (예 `.claude/agents`·`.claude/skills`) — pm_update 소관이라 어댑터 추가 시 오적재하면 안 된다.
    반대로 어댑터-소유/전파 경로는 add_harness 의 *복사 대상*이라 이 집합에서 뺀다:
      - `@target-owned`(예 진짜 target-owned) — pm_update ManifestEntry.target_owned 판별자.
      - `@source=<path>`(예 `.opencode/agents`·`.opencode/command` — framework-owned·guest 하네스·
        canonical 소스가 `templates/opencode/.opencode/*`) — pm_update 는
        source-remap 으로 전파하나 add_harness 는 이 guest 어댑터 파일을 *레이다운*해야 한다
        manifest-경로 = 어댑터 네임스페이스라
        add_harness 소스(templates/opencode)에 실재 → 복사.
    manifest 부재·로드 실패 → 빈 set(무동작).
    """
    pm_update_py = Path(__file__).resolve().parent / "pm_update.py"
    manifest = root / ".project_manager" / "engine.manifest"
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
            and not getattr(e, "target_owned", False)
            and not getattr(e, "source_rel", None)
        }
    except Exception:  # noqa: BLE001 — 로드/파싱 실패는 제외집합 0(무동작·전부 복사 대상).
        return set()


def render_managed_files(
    dest_root: Path,
    subs: dict[str, str],
    copied_relpaths: set[Path],
) -> int:
    """이번 run 이 복사한 @render path 파일을 render_adapter 산출물로 다시 쓴다. 변경 수 반환.

    범위 = copied_relpaths(비파괴·substitute_placeholders 와 동일 보장). 대상 판정은 **@render
    manifest 선언(`_is_render_managed`) 단독**이다 — 옛 `.md` 확장자 하드 필터는 제거했다.
    확장자 조건은 manifest 선언을 덮는 중복·모순 판정이었고, `.codex/agents @render`(TOML)처럼
    선언은 맞는데 코드가 안 따라가는 형상을 만들었다. 텍스트로 못 읽는 파일은 아래 read_text 의
    UnicodeDecodeError 가 걸러낸다(byte-copy 그대로 남음).

    operational 은 이번 import 의 subs(이미 substitute 가 리터럴로 박았으므로 보통 no-op).
    free-form 은 pm_import 의 FILL 채널이 canonical home 에서 전담하므로 render-overlay 가
    관여하지 않는다.

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
        if not _is_render_managed(rel_posix, managed):
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
    stdin=DEVNULL 의 isatty() 가 Windows 서 신뢰불가라, env 로
    `PM_NONINTERACTIVE=1` 을 명시 전달해 결정적으로 skip 시킨다 (isatty 폴백 보조).

    `PYTHONDONTWRITEBYTECODE=1` 도 함께 전달한다— board.py 는 이제 같은
    tools/ 디렉토리의 `identity_args.py` 를 `importlib.util.spec_from_file_location` 으로
    동적 로드하는데, 이 실행이 갓 복사된 새 프로젝트 트리 안(`dest_root`)에서 일어나므로 표준
    바이트코드 캐싱이 `dest_root/.project_manager/tools/__pycache__/` 를 새로 만든다 — fresh
    import 직후 빌드 산출물이 섞이는 것을 막는다(`test_import_excludes_pycache`).
    """
    board = dest_root / ".project_manager" / "tools" / "board.py"
    if not board.exists():
        print(f"경고: board.py 없음 ({board}) — init 건너뜀.", file=sys.stderr)
        return 1
    result = subprocess.run(
        [sys.executable, str(board), "init"],
        cwd=str(dest_root),
        stdin=subprocess.DEVNULL,
        env={**os.environ, "PM_NONINTERACTIVE": "1", "PYTHONDONTWRITEBYTECODE": "1"},
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

    board.py init 은 project_name 빈값·test_cmd=`pytest -q` 를 하드코딩하므로(seam
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

    백업은 형제 `*.backup.<DATE>` 가 아니라 중앙 디렉토리
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
    """upstream 값(URL 또는 로컬 경로)을 dest local.conf 에 `upstream=` 로 기록한다.

    `--upstream`(future update 기록·URL 선호)↔`--from`(이번 import 파일 소스) 디커플:
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
    """upstream baseline revision 을 dest local.conf 에 `upstream_rev=<commit>` 로 기록한다.

    drift-lint의 baseline 입력 — "마지막 동기 이후 upstream 변경분"을 재는 기준점이다
    import 시(이 함수)와 pm_update 매 sync 시 갱신된다. `upstream_seen_rev`(현재
    관찰값·pm-update 스킬 기록)는 **별개 키** — 한 키 2역 금지(race/자기비교 회피). rev 가
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


# ── opencode 모델 결정적 해소 단계 (LLM 아님) ──────────────────────
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

    fail-soft 는 유지(import 안 깸)하되 *침묵*은 제거한다 — 각 실패 분기에서 stderr 로
    사유 1줄을 surface 해 사용자가 다음 실행 때 왜 자동해소가 실패했는지(PATH/rc/timeout/parse)를
    본다. 타임아웃은 _opencode_models_timeout()
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
    제외-판정(새 파일 형식이 자동 편입된다).
    이번 import 가 복사한 파일만 — 복사 안 한 사용자 파일은 절대 안 건드린다.
    대상 = `.opencode/agents/*.md` 의 `model:` 필드·AGENTS.md 잔존분.
    """
    changed = 0
    sed_exclude = _dest_sed_exclude(dest_root)  # 치환 시점·dest manifest 기준(codex must-fix)
    for rel in sorted(copied_relpaths):
        if any(part in COPY_EXCLUDE_DIR_NAMES for part in rel.parts):
            continue
        if not _should_substitute(rel, sed_exclude):
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

    _mark_todos 폴백을 흡수— 모델 토큰만 대상. 조회 성공 시 가용 모델 목록을 마커에
    인라인해 사람이 바로 고를 수 있게 한다. 비파괴 규칙(이미 TODO/주석인 줄은 건너뜀·
    copied_relpaths 범위 한정). 마크한 토큰([OPENCODE_MODEL_TOKEN] 또는 [])을 반환.

    미해소 시 `model:` 값을 활성으로 남기면(`model: "…"  # TODO`) opencode 가
    "configured model … is not valid" 로 agent 자체를 거부한다(실 파일럿 블로커). → 줄 *전체*를
    주석화해 YAML frontmatter 에서 `model` 키를 *부재*시킨다 → opencode 가 기본 모델로 agent 를
    구동(graceful degradation).

    미해소 폴백이 주석 줄에 리터럴 `{{OPENCODE_PRO_MODEL}}` 을
    남기면 render `_assert_no_leak` 가 hard-fail 한다(@render path 산출물에 토큰 0 이어야 함). 그래서
    주석화하면서 토큰을 **형식 힌트 `<provider/model>` 로 중화**한다 → `# model: "<provider/model>"
    # TODO: …`. 채택자는 주석을 해제하고 provider/model 을 채우거나 `--opencode-model` 재import.

    실 줄-중화 로직은 `pm_render.neutralize_model_todo`
    로 옮겨 import(여기·render *이전* 폴백)와 self-update(pm_update 의 @render 재렌더·render_adapter
    가 같은 함수 호출) 둘 다 **같은 산출**을 내게 한다(byte-동일·drift 0). 옛 opencode `@source`
    비대칭(update 가 미해소 토큰을 leak 으로 rc-fail)의 근본 fix. 렌더러 로드 실패 시엔 **fail-loud**
    (raise) — 이 폴백의 계약은 "미해소 `model:` 줄을 *반드시* 중화해 안전 출하"이므로, 중화
    못 하면 조용히 활성 `{{OPENCODE_PRO_MODEL}}` 을 출하(opencode 가 agent 거부)하는 대신 크게
    터뜨린다. pm_render 는 co-located 엔진이라 정상 설치에선 항상 로드된다(미발화) — 로드 실패는
    broken install 신호이므로 loud 가 옳다(silent-degrade 근절·robustness 값-연결 assert).
    """
    render_mod = _load_pm_render_module()
    if render_mod is None:
        # fail-loud: 렌더러(공유 중화 단일 진실)를 못 실으면 중화 불가 — 조용히 활성 토큰을 출하하는
        # 대신 raise. 정상 설치에선 미발화(co-located 엔진). 로드 실패 = broken install → loud.
        raise RuntimeError(
            "pm_render 모듈 로드 실패 — opencode 모델 토큰 {{OPENCODE_PRO_MODEL}} 을 중화할 수 "
            "없습니다. 활성 토큰을 출하하면 opencode 가 agent 를 거부합니다. 엔진 설치를 "
            "확인하세요(.project_manager/tools/pm_render.py)."
        )
    marked = False
    for _rel, path in _iter_copied_files(dest_root, copied_relpaths):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        # 공유 중화(pm_render): agent frontmatter 의 `model:` 필드 줄만 주석화·토큰 중화한다 —
        # 산문/헤더(README 의 `{{OPENCODE_PRO_MODEL}}` 예시 등)는 건드리지 않는다(모듈 함수가
        # `model:` 시작 줄로 한정·YAML 주석 안전). 비파괴 멱등도 그쪽이 보장.
        new_text, changed = render_mod.neutralize_model_todo(text, available)
        if changed and new_text != text:
            path.write_text(new_text, encoding="utf-8")
            marked = True
    return [OPENCODE_MODEL_TOKEN] if marked else []


def resolve_opencode_model(
    dest_root: Path,
    copied_relpaths: set[Path],
    *,
    model_arg: str | None,
    models_runner: ModelsRunner | None = None,
    stdin=None,
) -> ModelResolveResult:
    """{{OPENCODE_PRO_MODEL}} 을 결정적으로 해소. board init·conf sync 직후·fill 이전.

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


def _load_watchdog():
    """엔진 pm_relay 의 첫-이벤트 워치독을 지연 로드한다 (deep-import seam·순환 회피).

    pm_import 와 pm_relay 는 형제(`.project_manager/tools/`) — importlib 로 직접 로드해
    PYTHONPATH 의존 없이(테스트 spec_from_file_location 경로 포함) run_with_first_event_watchdog·
    StallWatchdogError·env 노브 해소기를 빌려 쓴다(board._load_domain_module 선례 동형)."""
    engine_path = Path(__file__).resolve().parent / "pm_relay.py"
    spec = importlib.util.spec_from_file_location("pm_relay", engine_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _verify_engine_rev(module, engine_path.name)
    return module


def _real_harness_runner(
    argv: list[str], prompt: str, cwd: Path | str | None = None
) -> tuple[bool, str]:
    """실 하니스 바이너리를 subprocess 로 구동(fail-soft). 반환: (성공 여부, stdout).

    external_review.run_reviewer 선례 — 예외를 raise 하지 않고 (False, 에러텍스트)로 감싼다.
    프롬프트는 argv 마지막 인자로 전달한다(claude -p "<prompt>" / opencode run "<prompt>" ...).

    SF: cwd 가 주어지면 *대상 repo* 에서 구동한다(run_fill 이 dest_root 를 바인딩). 호출자
    cwd 가 아니라 import 대상에서 돌아야 하니스의 작업 디렉토리·파일 접근이 분석 대상과 맞는다.

    opencode 경로(`opencode run …`)는 엔진 첫-이벤트 워치독을 경유한다 — startup network
    fetch stall에 무한 hang 하지 않고 유한 재시도 후 fail-soft((False, stall 안내)). claude
    경로는 무변경(관측된 stall 클래스는 opencode 스타트업 고유).

    codex 경로(`codex exec …`)는 **stdin=DEVNULL 로 구동**한다 — stdin 미닫힘 시
    "Reading additional input from stdin..." 로 무기한 대기. claude/opencode 는
    argv 로 프롬프트를 받아 stdin 불요라 현행(None=상속) 유지."""
    use_watchdog = tuple(argv[:2]) == OPENCODE_FILL_CMD
    is_codex = tuple(argv[:2]) == CODEX_FILL_CMD
    engine = _load_watchdog() if use_watchdog else None
    try:
        if engine is not None:
            result = engine.run_with_first_event_watchdog(
                argv,
                first_event_timeout=engine.first_event_timeout_default(),
                overall_timeout=FILL_TIMEOUT_SECONDS,
                retries=engine.stall_retries_default(),
                cwd=str(cwd) if cwd is not None else None,
            )
        else:
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=FILL_TIMEOUT_SECONDS,
                cwd=str(cwd) if cwd is not None else None,
                # codex: stdin 미닫힘 시 무기한 대기(실측) → DEVNULL. claude 는 None(상속·현행).
                stdin=subprocess.DEVNULL if is_codex else None,
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
        if engine is not None and isinstance(exc, engine.StallWatchdogError):
            return False, f"[opencode 첫-이벤트 stall — 재시도 소진(fail-soft): {exc}]"
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


def _build_runner_argv(
    harness: str, prompt: str, dest_root: Path | None = None
) -> list[str]:
    """하니스별 헤드리스 구동 명령 조립(runner 매핑·명시 등록).

      claude   → `claude -p "<p>"`
      opencode → `opencode run "<p>" --format json` (token/cost 파싱 위해 json 출력)
      codex    → `codex exec --json -s workspace-write --skip-git-repo-check [-C <dest>] "<p>"`
                 (프롬프트=마지막 positional·stdin=DEVNULL 은 _real_harness_runner 가 부여)

    미지원 harness 는 **fail-loud**(ValueError). 과거 codex 가 이 매핑에 없어 조용히 `claude -p`
    로 폴백해 *잘못된 바이너리*를 호출하고 출력만 harness=codex 로 오표기하던 클래스를 닫는다 —
    모르는 harness 는 명시 등록을 강제(silent 폴백 금지)."""
    if harness == "claude":
        return [*CLAUDE_FILL_CMD, prompt]
    if harness == "opencode":
        return [*OPENCODE_FILL_CMD, prompt, "--format", "json"]
    if harness == "codex":
        argv = [*CODEX_FILL_CMD, "--json", "-s", "workspace-write", "--skip-git-repo-check"]
        if dest_root is not None:
            argv += ["-C", str(dest_root)]     # workdir 핀(codex 는 -C 로 작업 디렉토리 고정)
        argv.append(prompt)                    # 프롬프트 = 마지막 positional
        return argv
    raise ValueError(
        f"_build_runner_argv: 미지원 fill harness {harness!r} — 지원: claude·opencode·codex. "
        f"silent 폴백 금지(잘못된 바이너리 호출·오표기 방지·runner 매핑에 명시 등록 필요)."
    )


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


def _parse_codex_json(output: str) -> str:
    """codex `exec --json` JSONL 출력에서 최종 응답 텍스트 추출(fail-soft·_parse_opencode_json 동형).

    codex 는 줄당 JSON 이벤트(thread.started·turn.started·item.*·turn.completed·error)를 낸다.
    응답 = `item.type == "agent_message"` 인 item 의 `.text`(마지막 것 = 최종 응답·엔진
    pm_relay.parse_codex_json 규칙과 정합). JSON 파싱 불가·구조 상이·미발견이면 원문 그대로
    반환(사람이 읽게 — _parse_opencode_json 과 같은 fail-soft). pm_import 자족(형제 파서 미러·
    _parse_opencode_json 선례 + 시그니처/목적 상이 — pm_relay 쪽은 orchestration 용 3-튜플
    (thread_id·reply·usage)·여긴 fill 초안용 str·fail-soft. 순환 import 는 어느 쪽이든 없음)."""
    reply = ""
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            evt = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(evt, dict):
            continue
        item = evt.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                reply = text  # 최종(마지막) agent_message 로 갱신.
    return reply if reply else output


def _live_harness_allowed(mode: str) -> bool:
    """opt-in 게이트: 실 하니스 호출은 PM_IMPORT_LIVE_HARNESS=1 AND --fill auto 동시 충족 시만.

    둘 중 하나라도 없으면 False → run_fill 이 실 runner 를 호출하지 않고 stub/manual 로 폴백.
    """
    return mode == "auto" and os.environ.get(LIVE_HARNESS_ENV, "").strip() in ("1", "true", "yes", "on")


def _load_repo_owned_files():
    """공용 repo 소유 파일 열거 seam을 검증 소비자로 로드한다."""
    try:
        helper_path = Path(__file__).resolve().with_name("engine_rev.py")
        spec = importlib.util.spec_from_file_location(
            "_pm_import_repo_owned_loader", helper_path
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("공용 loader module spec/loader 부재")
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        return helper.load_repo_owned_files(
            Path(__file__).resolve().with_name("repo_owned_files.py"),
            verifier=_verify_engine_rev,
        )
    except Exception as exc:
        if _is_engine_rev_skew(exc):
            raise
        raise RuntimeError(
            "repo_owned_files.py를 로드할 수 없음 — 엔진 사본을 pm-update로 재동기화하라"
        ) from exc


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
    # 실 import 경로는 항상 copied_relpaths를 넘기므로 여기의 dest 전수 열거는 직접 호출용 폴백에
    # 한정된다. adopter가 아직 git repo가 아니면 seam이 filesystem 강등 경고를 1회 표면화한다.
    # 정상 import에는 노이즈가 없고, 직접 호출자는 추적/ignore 보장 소실을 놓치지 않는다.
    # 이 소비점의 0건은 "채울 파일 없음"이라는 정당 결과이므로 출하용 빈 인벤토리 예외에서 제외한다.
    repo_files = _load_repo_owned_files()
    scope: set[Path] = set()
    for rel in repo_files.list_repo_owned_files(
            dest_root, ".", mode=repo_files.OWNED):
        if any(part in COPY_EXCLUDE_DIR_NAMES for part in rel.parts):
            continue
        # dest fallback은 텍스트 읽기/수정 소비처다. seam의 gap domain은 symlink/gitlink를
        # 보존하지만 여기서는 링크 추종(특히 repo 밖)과 디렉토리/gitlink·삭제 엔트리를 제외한다.
        path = dest_root / rel
        if path.is_symlink() or not path.is_file():
            continue
        scope.add(rel)
    return scope


def _iter_copied_files(dest_root: Path, copied_relpaths: set[Path]):
    """이번 import 가 복사한 파일들만 (relpath, 절대경로)로 순회한다.

    MF(비파괴): fill 단계가 dest_root.rglob 로 *대상 프로젝트 전체* 를 훑으면, --into 에서
    이번 import 가 복사하지 *않은* 기존 사용자 파일(우연히 sentinel 포함)에도 TODO 마커가
    주입되어 비파괴 보장(substitute_placeholders 가 copied_relpaths 로 한정)과
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

    {{OPENCODE_PRO_MODEL}} 는 LLM fill 후보가 *아니다* — resolve_opencode_model 의
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

    {{OPENCODE_PRO_MODEL}} 는 fill 후보에서 분리(결정적 resolve_opencode_model 전담)
    되므로 여기서도 자유서술 3종만 본다. 모델 토큰 계획은 _plan_opencode_model_targets 가 별도.
    """
    present: list[str] = []
    for token in FREE_FORM_TOKENS:
        for action in actions:
            if _is_engine_source(action.dst):  # 엔진 소스 주석의 토큰-문서는 placeholder 아님
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
        if _is_engine_source(action.dst):  # 엔진 소스(.py) 주석의 모델-토큰 문서는 placeholder 아님
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
        if _is_engine_source(_rel):  # 엔진 소스 주석의 토큰-문서는 placeholder 아님
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

    템플릿은 대개 이미 placeholder 아래에 TODO 주석을 둔다. 여기서는 토큰을
    `<!-- TODO: 손으로 채우세요 -->` 인라인으로 *치환*하지 않고, 토큰 줄에 TODO 마커가 없을
    때만 토큰 뒤에 인라인 마커를 덧붙여(비파괴) 채택자에게 손작업 지점을 명시한다.
    실제로 마커를 추가한 토큰 목록을 반환한다.

    스캔 범위는 copied_relpaths(이번 run 복사 파일)로 한정 — 복사하지 않은 사용자 파일에는
    절대 마커를 주입하지 않는다(비파괴 보장). None 이면 dest 트리 전체 폴백(직접 호출용).
    """
    scan = _resolve_fill_scope(dest_root, copied_relpaths)
    marked: set[str] = set()
    marker = " <!-- TODO: 손으로 채우세요 -->"
    for _rel, path in _iter_copied_files(dest_root, scan):
        if _is_engine_source(_rel):  # 엔진 소스(.py)에 TODO 마커 주입 금지 — verbatim
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
    """자유서술 placeholder 채움 단계. board init 직후 hook.

    dest_root: import 된 대상 트리. harness: 구동 하니스('claude'|'opencode').
    live=True  → 실 하니스 호출 시도(opt-in 게이트 통과 — main 이 _live_harness_allowed 로 판정).
    live=False → runner 미호출(stub 경로). runner 가 주입되면(테스트 stub) live=True 와 무관하게
                 그 runner 로 명령을 조립·호출해 *명령 조립* 만 검증한다(토큰 0).
    copied_relpaths: 이번 import 가 복사한 파일 relpath set — fill 스캔 범위를 이 파일들로
                 한정한다(비파괴 보장). None 이면 dest 트리 전체를 스캔(직접 호출용
                 폴백) — main 은 항상 substitute_placeholders 와 동일 set 을 전달한다.

    규격(ticket §인터페이스): live=False 면 runner 를 호출하지 않고 stub/manual 경로로 간다.
    여기서 '하니스 미구동'은 *실 바이너리* 미구동을 뜻한다 — 주입 runner(stub)는 항상 안전.
    """
    scan = _resolve_fill_scope(dest_root, copied_relpaths)
    tokens = _fill_targets(dest_root, scan)

    # manual(또는 채울 토큰 없음): 하니스 미구동 — TODO 표시만.
    if not tokens:
        return FillResult(mode="manual", note="자유서술 placeholder 가 트리에 없음 — 처리 불필요.")

    prompt = _build_fill_prompt(dest_root, tokens)
    argv = _build_runner_argv(harness, prompt, dest_root)   # codex 는 -C <dest> workdir 핀 필요

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
    # 하니스별 응답 파싱: opencode=--format json·codex=exec --json JSONL(agent_message)·claude=평문.
    if harness == "opencode":
        text = _parse_opencode_json(output)
    elif harness == "codex":
        text = _parse_codex_json(output)
    else:
        text = output

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
    """pm_playbook.local.md 스텁을 dest 에 생성한다.

    backup_root: 중앙 백업 디렉토리(또는 None=--new). 이 함수는 기존 .local 을
    *덮지 않고 보존(skip)* 하므로 백업할 원본 변경이 없다 — backup_root 는 시그니처 일관성을
    위해 받지만 실제로 사용하지 않는다(미생성 = 백업 불요).

    fill 단계와 같은 자리(board init·conf sync 직후)에서 호출 — pm_role.local 초안 처리와
    같은 결의 인스턴스-소유 문서다. 루트 .local은 manifest 밖이라 템플릿 복사로 안 오니
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
    """fill 구동 하니스의 실 바이너리(claude/opencode/codex)가 PATH 에 있는가.

    shutil.which 로 탐지한다. 테스트는 monkeypatch(pm_import.shutil.which) 또는 PATH 조작으로
    부재를 stub 한다. 알 수 없는 harness 면 보수적으로 False(폴백 유도).
    """
    binary = {"claude": "claude", "opencode": "opencode", "codex": "codex"}.get(harness)
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


def _has_harness_templates(root: Path, harness: str) -> bool:
    """root 가 harness 의 어댑터 소스 트리(templates/<dir>/…)를 전부 보유하는가."""
    return all(
        (root / "templates" / name).is_dir()
        for name in HARNESS_TEMPLATE_DIRS[harness]
    )


def _resolve_add_harness_source(
    dest_root: Path, harness: str, explicit: Path | None,
) -> Path:
    """add_harness 의 어댑터 소스 checkout 을 해소한다.

    imported 인스턴스(scoped-core 사본·`templates/` 부재)는 dest 안에 어댑터
    소스 트리가 없다 — 소스는 그 인스턴스의 **upstream 프레임워크 checkout** 이다(add-harness 를
    *라이브 인스턴스*에 걸면 소스는 항상 그 인스턴스의 upstream 이다). 해소 우선순위:
      1. explicit(`--from`)      → 그대로 (기존 계약·override).
      2. dest local.conf upstream → classify_upstream=path 이고 그 경로에 templates/<harness>/
                                    가 있으면 소스.
      3. dest 자신              → dest 에 templates/ 가 있으면 dest (framework-checkout 자기전환·
                                    REPO 하드 기본이 맞던 유일 케이스·현행 회귀 보존).
      4. 전부 실패              → 친화 FileNotFoundError (actionable).

    URL upstream 은 이번 스코프 밖 — 엔진은 로컬 파일만 복사(git clone/fetch 안 함)
    하므로 path upstream 만 자동 해소하고 URL 은 skip 해 말단(dest 자기전환 또는 친화 에러)으로
    유도한다(명시 `--from` 요구). classify_upstream 으로 분기.
    """
    if explicit is not None:
        return Path(explicit).resolve()
    dest_root = Path(dest_root).resolve()

    # dest 는 엔진 사본(templates 부재) — 소스는 upstream 프레임워크 checkout.
    local_conf = dest_root / ".project_manager" / "local.conf"
    if local_conf.is_file():
        try:
            upstream = _parse_conf_keys(
                local_conf.read_text(encoding="utf-8")).get("upstream", "").strip()
        except (UnicodeDecodeError, OSError):
            upstream = ""
        # path upstream 만 자동 해소 — URL 은 로컬 파일 소스가 아니므로 skip(--from 요구).
        if upstream and classify_upstream(upstream) == "path":
            # 상대 경로는 인스턴스 루트(dest) 기준·절대면 그대로(pathlib: 절대 우변 승).
            candidate = (dest_root / upstream).resolve()
            if _has_harness_templates(candidate, harness):
                return candidate

    # framework-checkout 자기전환(dest 에 templates/ 보유) — 현행 REPO 하드 기본의 회귀 보존.
    if _has_harness_templates(dest_root, harness):
        return dest_root

    raise FileNotFoundError(
        f"add_harness 소스 미해소: {dest_root} 에 templates/ 가 없고, local.conf upstream 도 "
        f"templates/<harness> 를 가진 로컬 프레임워크 경로가 아니다. "
        f"`--from <프레임워크 checkout>` 를 주거나 local.conf 의 upstream= 을 로컬 프레임워크 "
        f"경로로 두라(URL upstream 은 자동 해소하지 않는다)."
    )


# ── add_harness (라이브 인스턴스에 두 번째 harness 어댑터 비파괴 추가) ──────
# raw `--into --harness both` 재-import 는 91파일 full 재-laydown 으로 라이브 wiki dev-state/엔진을
# 템플릿 starter 로 덮는다. add_harness 는 복사 스코프를 *추가되는 harness 의
# 어댑터 네임스페이스*(ADD_HARNESS_ADAPTER)로 제한해 그 파괴를 구조적으로 차단한다 — 기존 copy/
# render/backup 머신(plan_copy·substitute·resolve_opencode_model·_run_manual_fill)만 재사용한다(신규
# 복사 머신 0). 운영 진입(pm_config add-harness)이 이 core 로 verbatim 위임한다(Decision 3).


# codex 어댑터(`​.codex/agents/*.toml`·`config.toml`·hooks)는 **trusted project + hook trust 승인
# 후에만** 발화한다. import/add-harness 직후 신선 인스턴스는 이 2단계가
# 미승인 상태 — 조용히 두면 위임 subagent 스폰·PreCompact ctx tripwire 가 안 뜬다. `-c projects.<path>.
# trust_level` CLI override 는 **안 먹으므로** 대화형 승인이 유일 경로다.
def _print_codex_trust_guidance() -> None:
    """codex 어댑터 laydown 후 loud 2단계 trust 안내.

    import(`--harness codex`)·add-harness(기존 인스턴스에 codex 추가) 완료 출력 끝에 붙는다 —
    채택자가 첫 부트스트랩 전에 밟아야 할 trust 2단계 + 검증 커맨드를 눈에 띄게(loud) 안내한다.
    """
    print("")
    print("⚠️  codex 어댑터 활성화 전 2단계 trust 승인 필요 (미승인 시 위임/훅 미발화):")
    print("  ① 이 디렉토리에서 대화형 `codex` 를 1회 열어 프로젝트 trust 를 수락한다")
    print("     (`.codex/agents/*.toml`·`config.toml` 은 trusted project 한정 로드).")
    print("  ② codex 안에서 `/hooks` 로 hook trust 를 승인한다 (PreCompact ctx tripwire 발화 전제).")
    print("  검증: 대화형 codex 에서 위임 4축(architect/developer/code-reviewer/researcher)이 "
          "스폰 목록에 뜨는지 확인한다.")
    print("  ⚠️ `-c projects.<path>.trust_level=trusted` CLI override 는 안 먹는다(실측) — "
          "위 ① 대화형 승인이 필수다.")


def _in_adapter_namespace(
    rel: Path, adapter_dirs: tuple, root_doc: str, owned_paths: set[str],
    guest_render_paths: set[str],
) -> bool:
    """rel(dst relpath)이 추가 harness 의 복사 스코프 안인가.

    스코프 = ({adapter dir(들) 하위, root doc} ∪ **flavor `@render` 선언**(`guest_render_paths`·cross-ns
    의존물 포함)) − **host 실소유 경로**(`owned_paths`). adapter_dirs 중 하나의 하위/root doc 정확일치/
    flavor `@render` 선언(그 자체·하위) 중 하나여야 하고, 그 중 host(dest)가 이미 소유(pm_update 관리)하는
    경로 하위는 제외한다(경로-포함·`_is_render_managed`). **cross-ns 확장**: opencode 의
    `.claude/skills @render`(네이티브 소비)는 `.opencode` namespace 밖이나 flavor 가 선언한
    의존물이라 codex host(미소유)엔 복사해야 한다 — 옛 namespace-only 스코프는 이를 놓쳐 PM 스킬이
    host 실소유 차감은 그 위에 얹혀 opencode host 의 `.claude/skills`
    (host 소유)처럼 **host 가 이미 가진 것만** 정확히 뺀다(claude host + opencode 는 여전히 미복사). 옛엔
    flavor-native `@render`(guest flavor 관점)로 판정해 claude-as-guest 의 `.claude/agents`·`.claude/skills`
    adapter_dirs 는 튜플 —
    claude/opencode 는 단일, codex 는 이중(`.codex`+`.agents`). 이 밖(엔진·wiki·타 harness·
    설정·파사드)은 전부 False → plan 에 애초에 안 들어온다(구조적 안전·flavor 미선언 경로 유입 0).
    """
    rel_posix = rel.as_posix()
    # `rel == d` 포함(등재 경계와 동일 판정): namespace 자체 relpath 도 안으로 본다(파일 복사는
    # 하위만 나오지만 두 경계를 문자적으로 일치시켜). flavor `@render` 선언(cross-ns 의존물)은
    # `_is_render_managed`(경로-포함)로 그 하위 파일까지 스코프에 넣는다.
    in_scope = (rel_posix == root_doc
                or any(rel_posix == d or rel_posix.startswith(d + "/") for d in adapter_dirs)
                or _is_render_managed(rel_posix, guest_render_paths))
    if not in_scope:
        return False
    # host(dest)가 이미 소유(pm_update 관리)하는 경로는 스코프 안이라도 제외(중복 레이다운 방지).
    if _is_render_managed(rel_posix, owned_paths):
        return False
    return True


def _existing_create_if_absent_relpaths(
    template_root: Path,
    dest_root: Path,
    relpaths: frozenset[str],
    toml_override_fields: dict[str, frozenset[str]],
) -> tuple[set[Path], list[str]]:
    """기존 instance-owned config의 copy 제외 집합과 loud 안내 대상을 계산한다.

    이 helper는 add_harness가 ``plan_copy``를 호출하기 *전* 쓴다. 따라서 보호 파일은 일반
    CopyAction/backup/ancestor validation 경로에 전혀 들어가지 않는다. src가 없는 정책 항목은
    무시하지 않고 ValueError로 fail-loud — 선언과 template drift를 숨기지 않는다.
    """
    skip: set[Path] = set()
    different: list[str] = []
    for rel_str in sorted(relpaths):
        rel = Path(rel_str)
        src = template_root / rel
        if not src.is_file():
            raise ValueError(
                f"add-harness create-if-absent 정책 경로가 template에 없습니다: {rel_str}"
            )
        dst = dest_root / rel
        if not (dst.exists() or dst.is_symlink()):
            continue
        skip.add(rel)
        if not _same_bytes(src, dst):
            different.append(rel_str)
    for pattern, fields in sorted(toml_override_fields.items()):
        for src in sorted(template_root.glob(pattern)):
            rel = src.relative_to(template_root)
            dst = dest_root / rel
            if not (dst.exists() or dst.is_symlink()) or rel in skip:
                continue
            try:
                text = dst.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue  # 읽을 수 없는 agent 파일은 engine-managed refresh의 기존 안전 경로를 탄다.
            # TOML 문법으로 해석한 최상위 키만 본다. nested table·문자열·주석의 동일한
            # 단어는 instance override가 아니며, quoted key도 정상적으로 보호한다.
            try:
                parsed = tomllib.loads(text)
            except tomllib.TOMLDecodeError:
                continue  # 깨진 TOML은 기존 engine-managed refresh 경로를 탄다.
            if not any(field in parsed for field in fields):
                continue
            skip.add(rel)
            if not _same_bytes(src, dst):
                different.append(rel.as_posix())
    return skip, different


def _load_pm_update():
    """pm_update 모듈 지연 로드 (read_manifest + guest 절 마커/헬퍼 재사용·pm_import 은 pm_update 를
    top-level import 하지 않는다). `_engine_render_relpaths` 등이 쓰는 그 spec 로드 패턴. 실패 시 None."""
    pm_update_py = Path(__file__).resolve().parent / "pm_update.py"
    try:
        spec = importlib.util.spec_from_file_location("pm_update", pm_update_py)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:  # noqa: BLE001 — 로드 실패는 None(호출부가 무동작으로 흡수).
        return None


def _pm_update_read_manifest(manifest_path: Path) -> list:
    """pm_update.read_manifest 재사용 (부재·로드/파싱 실패는 빈 리스트·무동작). 새 파서 신설 없음."""
    if not manifest_path.is_file():
        return []
    mod = _load_pm_update()
    if mod is None:
        return []
    try:
        return list(mod.read_manifest(manifest_path))
    except Exception:  # noqa: BLE001 — 파싱 실패는 빈 리스트(무동작).
        return []


def _dest_manifest_core_paths(dest_root: Path) -> set[str]:
    """dest engine.manifest 의 **core**(guest 절 제외) 경로 집합 — add-harness 복사/등재 차감 기준
    host 가 pm_update 로 이미 관리(소유)하는 경로다.

    옛 flavor-native 판정(`_engine_render_relpaths`)은 *guest flavor* 관점이라 **claude-as-guest** 를
    놓쳤다 — dest 실소유로 대체한다. guest 절(add-harness 자기 산출·refresh 재복사 대상)은 stripped 라
    제외 → refresh 가 guest 를 재복사한다. manifest 부재·pm_update 로드 실패는 빈 set(무차감)."""
    manifest = dest_root / ".project_manager" / "engine.manifest"
    if not manifest.is_file():
        return set()
    pu = _load_pm_update()
    if pu is None:
        return set()
    return pu._core_manifest_paths(manifest.read_text(encoding="utf-8"))


def _flavor_render_relpaths(template_root: Path) -> set[str]:
    """guest flavor manifest 의 `@render` 선언 경로 **전부** (namespace 무관·host 실소유 미차감·손-열거 0).

    add-harness 복사/등재 후보의 **단일 출처**: flavor 가 `@render` 로 선언한 경로가 곧
    그 하네스가 관리하는 footprint 다. 여기엔 **cross-ns 의존물**도 포함된다 — opencode flavor 의
    `.claude/skills @render`(PM 스킬 채널·네이티브 소비)는 `.opencode` namespace 밖이지만
    opencode 어댑터가 반드시 소비한다. host 실소유 차감은 이 위에 downstream 으로 얹힌다(복사=
    `_in_adapter_namespace`·등재=`_guest_render_sync_plan`·둘 다 dest 실소유 `_path_owned_by` 기준). manifest
    부재·pm_update 로드/파싱 실패는 빈 set(무동작)."""
    manifest = template_root / ".project_manager" / "engine.manifest"
    if not manifest.is_file():
        return set()
    return {str(e).replace("\\", "/") for e in _pm_update_read_manifest(manifest)
            if getattr(e, "render", False)}


def _guest_render_manifest_lines(template_root: Path) -> list[str]:
    """add-harness 가 레이다운하는 guest 어댑터의 `@render` manifest **후보** 라인 (dest 등재용·손-열거 0).

    후보 = guest flavor manifest 의 `@render` **선언 전부**(`_flavor_render_relpaths`).
    "flavor 가 무관 공유 경로도 `@render` 로 들 수 있으니
    opencode 의 `.claude/skills @render`(네이티브
    소비)가 `.opencode` namespace 밖이라 **cross-ns 의존물이 등재·복사에서 빠져 codex host 에서 PM 스킬이
    flavor `@render` 선언 자체가 이미
    경계다 — flavor 는 자기가 관리하는 경로만 `@render` 로 선언하고("flavor 미선언 경로 유입 0" 불변식은
    이 구성으로 구조적 보장), host 가 이미 소유한 것은 downstream 차감(`_guest_render_sync_plan` 의
    `_path_owned_by`·기준 `_core_manifest_paths`)이 dest 기준으로 정확히 뺀다. guest 는 host 소유
    (add-harness 레이다운·upstream source 부재 정상)라 `@target-owned` 태깅(MF-2 재렌더 clobber 계약 —
    pm_update 재렌더/재전파 skip)."""
    return sorted(f"{rel}    @render @target-owned"
                  for rel in _flavor_render_relpaths(template_root))


def _is_safe_dest_path(dest_root: Path, rel: Path) -> bool:
    """`dest_root/rel` 이 dest 루트 하위이고 rel 경로·조상에 symlink 가 없는가.

    조작된 경로가 링크 follow·`..` 탈출로 repo 밖을 순회/치환하는 것을 막는다(순회·처리 양쪽):
      - `..`/빈 컴포넌트 거부.
      - rel 각 컴포넌트(`dest_root/…`)가 symlink 면 거부(**symlink 및 symlink 조상**).
      - 최종 resolve 후 dest_root 하위 containment(밖이면 거부).
    위반 시 False → 호출부가 skip(비파괴)."""
    try:
        droot = dest_root.resolve()
    except OSError:
        return False
    cur = dest_root
    for part in rel.parts:
        if part in ("..", "", "."):
            return False
        cur = cur / part
        try:
            if cur.is_symlink():
                return False
        except OSError:
            return False
    try:
        cur.resolve().relative_to(droot)
    except (ValueError, OSError):
        return False
    return True


def _byte_identical_skipped(
        template_root: Path, dest_root: Path, copied_relpaths: set,
        adapter_dirs: tuple) -> set:
    """이번 하네스 template 과 **byte-identical 이라 복사만 생략된** dest 파일 relpath 집합
    .

    이게 token-form 미렌더 잔존 문제의 **유일한 대상**이다 — 타 guest·adopter 자체 생성 파일(내용 상이·
    copied)
    조건: (a) 이 하네스 adapter namespace(`adapter_dirs`) 안, (b) 미-copied(copy plan 이 안 실음),
    (c) dest 실존 + template 과 byte-identical, (d) 경로 안전(`_is_safe_dest_path`). 경로는
    **template**(신뢰)에서 오고 manifest(조작 가능)에서 오지 않는다."""
    out: set = set()
    for rel, src in _iter_source_files(template_root, "full"):
        rel_posix = rel.as_posix()
        if not any(rel_posix == d.rstrip("/") or rel_posix.startswith(d.rstrip("/") + "/")
                   for d in adapter_dirs):
            continue  # 이 하네스 namespace 밖.
        if rel in copied_relpaths:
            continue  # 이미 copy plan 이 실음 → 렌더 대상(중복 방지).
        if not _is_safe_dest_path(dest_root, rel):
            continue  # 조작 경로·symlink → skip(repo 밖 순회/치환 방지).
        dst = dest_root / rel
        if dst.is_file() and _same_bytes(src, dst):
            out.add(rel)
    return out


def _guest_line_key(line: str) -> tuple:
    """guest manifest 라인의 **마커-무관 비교 키** = (path, frozenset(markers)).

    공백/마커 순서에 불변이라 `.opencode/agents    @render @target-owned` 와
    `.opencode/agents @target-owned @render` 를 같게 본다. 경로 집합만 비교하면 같은 경로의 **마커
    교정**(`@render` → `@render @target-owned`)을 놓쳐 pm_update 가 non-target-owned 누락으로 rc=2
    실패할 수 있다 — sync 의 changed 판정이 이 키 집합을 비교한다."""
    toks = line.split()
    return (toks[0], frozenset(toks[1:])) if toks else ("", frozenset())


def _guest_render_sync_plan(
        dest_root: Path, guest_lines: list[str], adapter_dirs: tuple) -> dict:
    """refresh 시 **이 하네스 namespace 의 guest 절 항목을 현재 flavor 와 동기화**한 계획.

    반환 `{"added": [라인], "removed": [경로], "new_block": str|None, "changed": bool}`.
    - **이 하네스 namespace(`adapter_dirs`) 항목만** 현행 목표로 교체 — 신규 추가 **+ upstream flavor 에서
      폐기/`@render` 해제된 stale 제거**.
    - **타 하네스 guest 항목은 불변**(다른 namespace — 순차 add 로 한 절에 공존).
    - 목표 = `guest_lines`(flavor·이미 namespace-limited) **−** host 실소유(경로-포함
      `_path_owned_by`·기준 `_core_manifest_paths`). add·refresh·dry-run preview 가 이 단일 계획을 공유
      (판정 사본 0). manifest 부재·pm_update 로드 실패는 무동작(changed=False)."""
    empty = {"added": [], "removed": [], "new_block": None, "changed": False}
    manifest = dest_root / ".project_manager" / "engine.manifest"
    if not manifest.is_file():
        return empty
    pu = _load_pm_update()
    if pu is None:
        return empty
    text = manifest.read_text(encoding="utf-8")
    block = pu._extract_guest_manifest_block(text)
    existing_lines = [
        ln.rstrip() for ln in (block.splitlines() if block else [])
        if ln.strip() and not ln.strip().startswith("#")]

    # 이 하네스가 관리하는 footprint = adapter namespace ∪ **flavor `@render` 선언**(cross-ns 포함).
    #   guest_lines 는 flavor `@render` 선언 전부(`_guest_render_manifest_lines`) — 그 경로 집합이 cross-ns
    #   의존물(opencode 의 `.claude/skills`)까지 이 하네스 소유로 판정하게 한다(경로-포함
    #   `_path_owned_by`). namespace-only 판정이면 cross-ns 항목이 target 엔 있는데 existing_this 엔 없어
    #   idempotent refresh 가 매번 changed=True(등재 churn)·타 하네스로 오분류된다(멱등 위반).
    this_flavor_paths = {ln.split()[0] for ln in guest_lines}

    def _this_ns(path: str) -> bool:
        return pu._path_owned_by(path, this_flavor_paths) or any(
            path == d.rstrip("/") or path.startswith(d.rstrip("/") + "/")
            for d in adapter_dirs)

    # 목표 = flavor 후보(namespace 무관·cross-ns 포함) − host 실소유. guest_lines 는 이미 flavor-declared.
    core_owned = pu._core_manifest_paths(text)
    target = sorted(
        ln for ln in guest_lines if not pu._path_owned_by(ln.split()[0], core_owned))
    target_paths = {ln.split()[0] for ln in target}
    existing_this_lines = [ln for ln in existing_lines if _this_ns(ln.split()[0])]
    existing_this = {ln.split()[0] for ln in existing_this_lines}
    other_ns = [ln for ln in existing_lines if not _this_ns(ln.split()[0])]  # 타 하네스 — 불변.
    added = [ln for ln in target if ln.split()[0] not in existing_this]
    removed = sorted(existing_this - target_paths)  # upstream 에서 폐기된 this-ns 경로.
    merged = sorted(set(other_ns + target), key=lambda ln: ln.split()[0])
    new_block = (pu._GUEST_MANIFEST_BEGIN + "\n" + "\n".join(merged) + "\n"
                 + pu._GUEST_MANIFEST_END) if merged else None
    # changed = 경로 추가/제거 **OR 마커 교정**(같은 경로·마커 상이) — 경로 집합만 보면 기존
    #   `.opencode/agents @render` → 목표 `@render @target-owned` 교정을 놓쳐 pm_update rc=2
    #   ). this-ns 기존 라인 ↔ 목표를 마커-무관 키 집합으로 비교한다(merged 는 이미 목표 마커 반영).
    changed = ({_guest_line_key(ln) for ln in existing_this_lines}
               != {_guest_line_key(ln) for ln in target})
    return {"added": added, "removed": removed, "new_block": new_block, "changed": changed}


def _append_guest_render_to_manifest(
        dest_root: Path, guest_lines: list[str], adapter_dirs: tuple) -> dict:
    """dest engine.manifest 의 guest 절을 이 하네스 namespace 에 대해 현재 flavor 와 **동기화**한다
    (신규 등재 **+ 폐기 제거**·타 하네스 불변). 반환 `{"added": [라인], "removed": [경로]}`.

    **단일 guest 절**(마커 하나) 아래 모든 하네스 라인이 모이고, pm_update 가 engine.manifest overwrite
    시 재부착한다(MF-1). refresh 가 add-only 였으면 upstream flavor 에서 사라진 경로가 영구 render/lint
    관리로 남았다(). write 후 `read_manifest` **왕복 검증**(조용한 미등재 금지·RuntimeError). dest
    manifest 부재·무변경(멱등)은 graceful skip. 계획은 `_guest_render_sync_plan`(preview 공유·판정 사본 0)."""
    manifest = dest_root / ".project_manager" / "engine.manifest"
    # 경로 안전: manifest(또는 조상)가 repo-밖 지향 symlink 면 아래 read/write 가 링크를
    #   따라가 외부 파일을 노출/덮는다 — **fail-loud**(조용한 skip 아님). 부분 적용 방지는 add_harness 의
    #   복사 시작 전 조기 가드가 맡고, 여기선 직접 호출·TOCTOU 백스톱.
    if not _is_safe_dest_path(dest_root, Path(".project_manager") / "engine.manifest"):
        raise RuntimeError(
            f"add-harness: engine.manifest 경로가 안전하지 않아 guest 등재를 거부합니다 ({manifest}) "
            "— symlink·조상 symlink·repo 밖. 링크를 옮기거나 제거한 뒤 다시 시도하세요(외부 파일 불변).")
    if not manifest.is_file():
        # 등재를 조용히 생략하지 않는다() — 복사됐지만 render/lint 관리 밖임을 명시.
        print("  ⚠️ engine.manifest 부재 — guest 어댑터가 복사됐으나 render/lint 관리 밖입니다 "
              "(manifest-파생 등재 채널 없음).", file=sys.stderr)
        return {"added": [], "removed": []}
    pu = _load_pm_update()
    if pu is None:
        print("  ⚠️ pm_update 로드 실패 — guest 어댑터가 복사됐으나 render/lint 관리 밖입니다 "
              "(guest @render 등재 생략).", file=sys.stderr)
        return {"added": [], "removed": []}
    plan = _guest_render_sync_plan(dest_root, guest_lines, adapter_dirs)
    if not plan["changed"]:
        return {"added": [], "removed": []}  # 멱등 — 이미 동기(재실행 refresh)
    text = manifest.read_text(encoding="utf-8")
    stripped = pu._strip_guest_manifest_block(text)
    if plan["new_block"]:
        if stripped and not stripped.endswith("\n"):
            stripped += "\n"
        manifest.write_text(stripped + "\n" + plan["new_block"] + "\n", encoding="utf-8")
    else:
        manifest.write_text(stripped, encoding="utf-8")  # this-ns guest 전량 폐기·타 하네스도 0 → 절 제거.
    # read_manifest 왕복 검증 (fail-loud·추가분 반영).
    after = {str(e).replace("\\", "/") for e in pu.read_manifest(manifest)
             if getattr(e, "render", False)}
    missing = [ln.split()[0] for ln in plan["added"] if ln.split()[0] not in after]
    if missing:
        raise RuntimeError(
            f"add-harness: guest @render 등재가 read_manifest 왕복에 미반영: {missing}")
    return {"added": plan["added"], "removed": plan["removed"]}


def add_harness(
    dest_root: Path,
    harness: str,
    *,
    dry_run: bool,
    source_root: Path | None = None,
) -> list[CopyAction]:
    """라이브 인스턴스에 두 번째 harness 어댑터를 비파괴로 추가한다.

    스코프 = *추가되는 harness 의 어댑터 네임스페이스 ∪ guest flavor `@render` 선언*(ADD_HARNESS_ADAPTER·
    host 실소유: opencode=`.opencode/**`+`AGENTS.md`(+codex host 엔
    cross-ns `.claude/skills` — opencode 네이티브 소비), claude=`.claude/**`(host-소유 제외)+
    `CLAUDE.md`. **제외**(plan 에 애초에 없음→clobber 불가): `.project_manager/**`(엔진+wiki dev-state)·
    `engine.manifest`·`.gitignore`·`.gitattributes`·`.github/**`·루트 파사드·다른 harness·flavor 미선언.

    구현: `plan_copy` 로 전체 어댑터 트리 plan 을 만든 뒤 `_in_adapter_namespace` predicate(네임스페이스
    ∪ flavor `@render` − host 실소유)로 **구조적으로 좁힌다** — 반환·적용 plan 에 flavor 미선언 relpath 가
    0개다(Decision 5 불변식·cross-ns 의존물은 flavor 선언이라 허용).
    첫 add=신규 복사(무손실)·재실행=refresh(엔진 관리 어댑터 파일은 중앙 백업 후 덮음·
    instance-owned config는 create-if-absent 보존·`--into` 백업 철학). fill(LLM) 불요 — operational 토큰 치환(substitute_placeholders)·opencode
    모델 결정적 해소(resolve_opencode_model)·자유서술 TODO 표시(_run_manual_fill·비-LLM)만.

    dry_run=True 면 plan 만 산출·출력(파일시스템 미변경). 반환값 = 스코프 제한된 CopyAction plan.
    source_root 생략 시 _resolve_add_harness_source 로 소스를 정한다: dest local.conf
    upstream(path·templates 보유) > dest 자신(templates 보유·framework-checkout 자기전환) > 친화
    에러. imported 인스턴스(templates 부재)도 upstream 에서 어댑터 소스를 해소한다.
    harness 는 단일('claude'|'opencode') — 'both'/미지원은 ValueError. dest 미존재는 FileNotFoundError.
    """
    if harness not in ADD_HARNESS_ADAPTER:
        raise ValueError(
            f"add_harness: harness 는 {tuple(ADD_HARNESS_ADAPTER)} 중 하나여야 한다 "
            f"(단일 harness 추가·'both' 는 최초 import 소관): {harness!r}"
        )
    dest_root = Path(dest_root).resolve()
    if not dest_root.is_dir():
        raise FileNotFoundError(
            f"add_harness: dest 가 존재하는 라이브 인스턴스 디렉토리가 아니다: {dest_root}"
        )
    # engine.manifest 경로 안전 검증 (**복사 시작 전** fail-loud): manifest(또는 조상)가
    #   repo-밖 지향 symlink 면 이후 read(`_dest_manifest_core_paths`)·write(`_append_guest_render_to_
    #   manifest`)가 링크를 따라가 외부 파일을 노출/덮는다. 불안전이면 어떤 복사·등재도 시작하지 않는다
    #   (부분 적용 0). 읽기·쓰기 지점이 같은 경로라 이 단일 가드가 양쪽을 덮는다.
    if not _is_safe_dest_path(dest_root, Path(".project_manager") / "engine.manifest"):
        raise RuntimeError(
            f"add-harness 거부: engine.manifest 경로가 안전하지 않습니다 "
            f"({dest_root / '.project_manager' / 'engine.manifest'}) — symlink·조상 symlink·repo 밖. "
            "링크를 직접 옮기거나 제거한 뒤 다시 시도하세요(비파괴 — 외부 파일을 건드리지 않습니다).")
    src_root = _resolve_add_harness_source(dest_root, harness, source_root)
    template_root = resolve_template_roots(src_root, harness)[0]

    adapter_dirs, root_doc = ADD_HARNESS_ADAPTER[harness]
    # 스코프 표시 문자열 — dirs 튜플을 `d/**` 로 합친다(codex=`.codex/** + .agents/**`·단일은 그대로).
    adapter_scope = " + ".join(f"{d}/**" for d in adapter_dirs)
    # 복사/등재 차감 기준 = **dest(host) 실소유 경로** (guest 절 제외).
    # 판정(`_engine_render_relpaths`)은 *guest flavor* 관점이라 bare `@render` 를 전부 native 로 봐
    # **claude-as-guest**(codex/opencode host 에 claude 추가)를 놓쳤다 — claude flavor 의 `.claude/agents`·
    # `.claude/skills` bare @render 가 native 로 차감돼 복사·등재 0 → pm_update 영구 관리 불능. host 가
    # *실제로* 소유(pm_update 관리)하는 경로만 빼고 나머지는 guest 로 레이다운/등재한다(경로-포함
    # `_path_owned_by`/`_is_render_managed`). opencode host 의 `.claude/skills`(native 소비)처럼
    # host 가 이미 가진 것만 정확히 빠진다.
    dest_owned = _dest_manifest_core_paths(dest_root)
    # guest flavor 가 `@render` 로 선언한 경로 전부 (cross-ns 의존물 포함) — 복사 스코프가
    # namespace 밖이라도 이걸 포함해야 opencode 의 `.claude/skills`(codex host 미소유)가 복사·
    # 렌더된다(그래야 등재된 guest @render 를 render_managed_files 가 실제 파일에 적용). host 실소유
    # (dest_owned)는 아래 `_in_adapter_namespace` 가 그 위에서 차감한다(claude host 는 미복사 유지).
    guest_render_paths = _flavor_render_relpaths(template_root)

    today = datetime.date.today().isoformat()
    # refresh(재실행)는 네임스페이스 안 기존 어댑터를 중앙 디렉토리에 백업 후 덮는다(--into 동형).
    # 첫 add 는 신규라 백업 없음. git 추적&미변경은 백업 생략(git 복원 가능).
    backup_root = dest_root / BACKUP_DIR_NAME / today
    git_safe = git_safe_relpaths(dest_root)

    # 전체 어댑터 트리 plan → (네임스페이스 ∪ flavor `@render` − host 실소유)로 구조적 제한(Decision 2·5·
    # ). 그 밖(엔진·wiki·타 harness·설정·파사드·flavor 미선언)은 필터로 제거돼 plan 에 0개다(불변식).
    create_if_absent = ADD_HARNESS_CREATE_IF_ABSENT[harness]
    skipped_existing, preserved_different = _existing_create_if_absent_relpaths(
        template_root, dest_root, create_if_absent,
        ADD_HARNESS_PRESERVE_EXISTING_TOML_FIELDS[harness],
    )
    plan = plan_copy(
        [template_root], dest_root, backup_root, "full", git_safe=git_safe,
        skip_existing_relpaths=skipped_existing,
        include_relpath=lambda rel: _in_adapter_namespace(
            rel, adapter_dirs, root_doc, dest_owned, guest_render_paths),
    )

    n_new = sum(1 for a in plan if a.backup is None and not a._git_safe_skip)
    n_refresh = len(plan) - n_new
    print(f"[pm_import add-harness] {harness} → {dest_root}")
    print(f"  소스: {src_root}/templates/{HARNESS_TEMPLATE_DIRS[harness][0]}")
    print(f"  스코프: 어댑터 네임스페이스 + flavor @render ({adapter_scope} + {root_doc} · "
          f"host 실소유 경로 제외)")
    for a in plan:
        print(a.describe())
    for rel in preserved_different:
        print(f"  ⚠️ instance-owned {rel}: 기존 값이 template과 다름 — byte 보존. "
              "template 변경은 수동 반영하세요.")
    print(f"  → {len(plan)} 파일 ({n_new} 신규 · {n_refresh} refresh)")

    if dry_run:
        # engine.manifest guest `@render` 동기 미리보기 — 추가/제거 예정 둘 다(실제 sync 와 같은 계획·
        #   `_guest_render_sync_plan` 공유·멱등이면 0건).
        gsync = _guest_render_sync_plan(
            dest_root, _guest_render_manifest_lines(template_root), adapter_dirs)
        if gsync["added"]:
            print(f"  engine.manifest guest @render 등재 예정 ({len(gsync['added'])}건):")
            for gl in gsync["added"]:
                print(f"    + {gl}")
        if gsync["removed"]:
            print(f"  engine.manifest guest @render 제거 예정 ({len(gsync['removed'])}건·폐기):")
            for gp in gsync["removed"]:
                print(f"    - {gp}")
        print("[dry-run] 적용 안 함 (파일시스템 미변경).")
        return plan

    # ── 적용 ── 스코프(네임스페이스 ∪ flavor `@render` − host 실소유) 안 파일만 복사·토큰 처리
    #   (스코프 밖은 plan 에 없어 불가침).
    for a in plan:
        a.run()
    copied_relpaths = {a.dst.relative_to(dest_root) for a in plan}
    # guest 어댑터 `@render` 를 dest engine.manifest 에 멱등 등재 —
    # 인스턴스 manifest 가 "이 인스턴스에서 framework-managed 인 것"의 단일 진실이 되어, 아래
    # render_managed_files 와 manifest-파생 overlay 스캔이 guest 를 자연 커버한다.
    # **render 전에** 등재해야 이번 run 의 render_managed_files 가 guest 를 집는다.
    guest_sync = _append_guest_render_to_manifest(
        dest_root, _guest_render_manifest_lines(template_root), adapter_dirs)
    if guest_sync["added"]:
        print(f"  ✓ engine.manifest guest @render {len(guest_sync['added'])}건 등재: "
              f"{', '.join(ln.split()[0] for ln in guest_sync['added'])}")
    if guest_sync["removed"]:
        print(f"  ✓ engine.manifest guest @render {len(guest_sync['removed'])}건 제거(폐기 동기): "
              f"{', '.join(guest_sync['removed'])}")
    # 이번 하네스 template 과 **byte-identical 이라 복사만 생략된**
    #   파일만 처리 대상에 추가한다 — token-form 그대로라 미렌더(토큰 잔존) 잔존의 유일 대상. 경로는
    #   template(신뢰)에서 오고 안전 검증(`_is_safe_dest_path`)을 거치며, 타 guest·adopter 자체
    #   생성 파일(내용 상이·copied)은 제외된다(과확장 봉쇄). 기존 렌더 파이프 재사용.
    proc_relpaths = copied_relpaths | _byte_identical_skipped(
        template_root, dest_root, copied_relpaths, adapter_dirs)
    # 라이브 인스턴스의 project_name 은 기존 local.conf 를 존중(없으면 디렉토리명 폴백).
    project_name = _instance_project_name(dest_root)
    subs = _substitution_map(project_name, dest_root, today)
    n_subst = substitute_placeholders(dest_root, subs, proc_relpaths)
    # opencode 모델 토큰 결정적 해소(claude-only 는 inactive) — main 흐름과 동일.
    resolve_opencode_model(dest_root, proc_relpaths, model_arg=None)
    # render_managed_files 는 dest 인스턴스 engine.manifest 의 @render path 만 렌더한다 — 위에서 guest
    # `@render` 를 dest manifest 에 등재했으므로 guest 어댑터도 렌더된다.
    render_managed_files(dest_root, subs, proc_relpaths)
    # 자유서술 placeholder 는 TODO 표시(비-LLM·main manual 흐름과 동일).
    _run_manual_fill(dest_root, proc_relpaths)
    # main import(:3289)와 **대칭** — 이번 add 가 실제로 중앙 백업을 만들었으면
    #   (backup_root 생성) .gitignore 가 `.pm_import_backups/` 를 무시하게 보장한다(채택자
    #   git status 오염 방지). 같은 헬퍼 재사용(신규 로직 0)·발화 조건은 git repo(git_safe
    #   판정 가능) + 백업 실생성 시에만 — 무백업 add 는 gitignore 무변(최소 변경). add-harness
    #   는 항상 라이브 인스턴스라 main 의 is_new 분기는 없다(backup_root 도 상시 non-None).
    if git_safe is not None and backup_root.exists():
        gi_status = ensure_backup_dir_gitignored(dest_root, git_safe, copied_relpaths)
        if gi_status in ("added", "created"):
            print(f"✓ .gitignore 에 {BACKUP_DIR_NAME}/ 추가 (백업이 git status 오염 방지)")
        elif gi_status == "unsafe-skip":
            print(f"  ⚠️ .gitignore 가 미추적/변경 상태 — 비파괴 위해 자동 추가 생략. "
                  f"수동으로 `{BACKUP_DIR_NAME}/` 한 줄을 추가하세요.")
    print(f"✓ add-harness 완료: {harness} 어댑터 {len(plan)} 파일 복사 · "
          f"{n_subst} 파일 토큰 치환 (스코프: {adapter_scope} + {root_doc})")
    # codex 는 laydown 만으로 활성화되지 않는다 — trusted project + hook trust 2단계 안내.
    if harness == "codex":
        _print_codex_trust_guidance()
    return plan


def add_harness_cli(
    dest_root: Path,
    harness: str,
    *,
    dry_run: bool,
    source_root: Path | None = None,
) -> int:
    """add_harness 의 main-style CLI 진입 — 인터페이스 예외를 친화 메시지 + rc 로 번역한다.

    운영 진입(`pm_config add-harness`·Decision 3)이 verbatim 위임하는 얇은 래퍼다. add_harness
    자체(확정 시그니처/로직)는 건드리지 않고, 그것이 던지는 인터페이스 예외만 CLI 경계에서
    잡아 `main()` 과 *동일하게* 처리한다(에러 처리의 단일 진실 = CLI contract owner = pm_import):
      - ValueError            : 미지원 harness('both'/오타·add_harness 입구 검증).
      - FileNotFoundError     : dest 부재/비-디렉토리·소스 템플릿 부재(resolve_template_roots).
      - FileVsDirConflict     : 어댑터 dst 위치에 기존 디렉토리(plan_copy·비파괴 거부).
      - AncestorConflict      : dst 조상에 symlink/비-디렉토리 파일(plan_copy·비파괴 거부).
    이 네 예외는 add_harness 가 *출력 전*(plan_copy·resolve_template_roots·입구 검증)에 던지므로
    부분 출력/부작용 없이 깨끗한 `오류: …`(stderr) + rc 1 로 끝난다(traceback 0·main 동형). 성공은
    add_harness 가 자체 plan/summary 를 출력하고 여기선 rc 0 만 돌려준다(위임 verbatim·중복 출력 0).

    dry_run/source_root 는 add_harness 로 그대로 전달한다(투명 위임). 반환: 0(성공)·1(인터페이스 예외).
    """
    try:
        add_harness(dest_root, harness, dry_run=dry_run, source_root=source_root)
    except (
        ValueError,
        FileNotFoundError,
        FileVsDirConflict,
        AncestorConflict,
        EmptyTemplateShippingInventoryError,
    ) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    return 0


def _instance_project_name(dest_root: Path) -> str:
    """라이브 인스턴스의 project_name 을 local.conf 에서 읽는다(없으면 디렉토리명 폴백).

    add_harness 의 operational 토큰 치환이 인스턴스의 실제 project_name 을 존중하도록 —
    최초 import 가 local.conf 에 박아 둔 값을 재사용한다(_parse_conf_keys). local.conf 부재·
    project_name 미설정이면 dest 디렉토리명(main 의 --name 기본값과 동형).
    """
    local_conf = dest_root / ".project_manager" / "local.conf"
    if local_conf.is_file():
        try:
            conf = _parse_conf_keys(local_conf.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, OSError):
            conf = {}
        name = conf.get("project_name", "").strip()
        if name:
            return name
    return dest_root.name


# ── board submodule 셋업 ─────────────────────────────────
# `--new --board-submodule --board-remote <url>` 은 board(tickets+areas)를 superproject inline 이
# 아니라 **별도 git submodule**(`.project_manager/board`)로 세운다(board 전용 git·공유
# remote·`ignore=all`). inline 기본(플래그 없음)은 이 경로를 전혀 타지 않아 완전 무변경(현행 --new
# 회귀). 공유 remote 의 호스팅/권한 생성은 사용자 게이트 — 엔진은 URL 을 받아 배선만 한다.

_BOARD_SUBMODULE_PATH = ".project_manager/board"  # 표준 `git submodule add` 은 name == path.
_BOARD_SETUP_GIT_TIMEOUT_SECONDS = 600  # remote clone/submodule add (네트워크·큰 board 여유).

# board submodule areas.md 스캐폴드 canonical 칼럼 — board._AREAS_COLUMNS 를 미러한다(pm_import 는
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
)


def _board_setup_git(argv: list[str], cwd: Path | None) -> tuple[int, str]:
    """board submodule 셋업용 git 실행 → (rc, stdout+stderr). fail-soft(예외→(1,msg)).

    `-c protocol.file.allow=always` 를 상시 얹어 로컬/`file://` remote(hermetic 테스트·self-split
    로컬 board) 의 submodule 전송을 허용한다 — ssh/https remote 는 그 transport 만 제어하므로 무영향
    (CVE-2022-39253 이후 file transport 는 submodule 재귀에서 기본 차단). git identity·credential 은
    사용자 환경 상속(공유 board=사용자-신뢰·credential helper/ssh-agent 그대로 작동). URL 은 호출부가
    `validate_upstream_value` 로 형태-안전 검증(credential-in-url·leading-dash·비허용 scheme 거부)한다.
    """
    git_binary = shutil.which("git")
    if git_binary is None:
        return 1, "git 바이너리를 찾을 수 없음 (PATH)."
    try:
        result = subprocess.run(
            [git_binary, "-c", "protocol.file.allow=always", *argv],
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_BOARD_SETUP_GIT_TIMEOUT_SECONDS)
        return result.returncode, (result.stdout or "") + (result.stderr or "")
    except Exception as exc:  # noqa: BLE001 — fail-soft: rc!=0 로 호출부 위임.
        return 1, str(exc)


def _board_areas_scaffold() -> str:
    """신규 공유 board 의 초기 areas.md (canonical 8칼럼 헤더만·데이터 행 0).

    board.py `_areas_header_line()`/`_areas_separator_line()` 와 같은 형식이다. 등록 repo 0 →
    `registered_prefixes()` 빈 set → 솔로 `T-NNNN`(합류/멀티-repo 등록 전). 이후 `board.py init
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
    shutil.rmtree(board_dir, ignore_errors=True)
    shutil.rmtree(dest_root / ".git" / "modules" / ".project_manager" / "board",
                  ignore_errors=True)
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
      4. 복사된 dormant `wiki/tickets` 제거 — board 는 이제 submodule 에 산다(`board_root()` 가
         `board/tickets` 존재로 board/ 로 해소하므로 wiki/tickets 는 잉여).
    """
    board_dir = dest_root / ".project_manager" / "board"
    copied_tickets = dest_root / ".project_manager" / "wiki" / "tickets"
    if board_dir.exists():
        print(f"오류: {_BOARD_SUBMODULE_PATH} 가 이미 존재 — board submodule 셋업 중단.",
              file=sys.stderr)
        return 1
    tmp_clone = Path(tempfile.mkdtemp(prefix="pm_board_seed_"))
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
            for status in ("open", "claimed", "blocked", "done"):
                sd = tmp_clone / "tickets" / status
                sd.mkdir(parents=True, exist_ok=True)
                (sd / ".gitkeep").touch(exist_ok=True)  # 빈 status dir git 추적(합류 유저 checkout).
            (tmp_clone / "areas.md").write_text(_board_areas_scaffold(), encoding="utf-8")
            # areas.md merge=union 은 **이 git**(board)에 선언돼야 유효하다 — 루트 선언은 다른
            # git 이라 닿지 않는다. 신규 clone 이라 기존 파일 없음(비파괴 판단 불요).
            (tmp_clone / ".gitattributes").write_text(
                _BOARD_GITATTRIBUTES_SCAFFOLD, encoding="utf-8")
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
        shutil.rmtree(tmp_clone, ignore_errors=True)

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
        shutil.rmtree(copied_tickets, ignore_errors=True)

    label = "신규 스캐폴드 seed+push" if seeded else "기존 board 재사용(합류)"
    print(f"✓ board submodule 셋업: {_BOARD_SUBMODULE_PATH} "
          f"(remote={remote_url} · {label} · ignore=all)")
    return 0


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
    pattern = f"{BACKUP_DIR_NAME}/"
    gitignore = dest_root / ".gitignore"
    # .gitignore 가 symlink 면 write_text 가 링크 대상(프로젝트 밖 가능)을
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
    # 방어: 위생 write 실패(권한 등)가 *복사·치환이 끝난* import 말미를
    #   깨뜨리지 않게 한다 — gitignore 위생은 should 부가단계라 실패해도 import 자체는 성공으로 둔다.
    try:
        gitignore.write_text(new_text, encoding="utf-8")
    except OSError:
        return "noop"
    return status


# ── main ───────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    _console_spec = importlib.util.spec_from_file_location(
        "_console_encoding", Path(__file__).resolve().with_name("console_encoding.py")
    )
    _console_encoding = importlib.util.module_from_spec(_console_spec)
    _console_spec.loader.exec_module(_console_encoding)
    _verify_engine_rev(_console_encoding, "console_encoding.py")
    _console_encoding.configure_console_utf8()
    ap = argparse.ArgumentParser(
        prog="pm_import.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "온보딩(fresh 채택자): manager(project_manager) 경로/URL 만 있으면 자율 import — "
            "harness=자기 세션(claude|opencode), --new(빈 PM 홈)/--into(기존 프로젝트 임베드) "
            "맥락 판단. 상세 가이드 = manager 루트 ADOPT.md. import 후 다음 단계: /pm-bootstrap → /pm-env.\n\n"
            "upstream 기록: --from 은 *파일 소스*, --upstream 은 *future update 기록*으로 "
            "디커플된다. local.conf 에 `upstream=`(pm_update 가 --from 생략 시 사용) + "
            "`upstream_rev=<commit>`(drift baseline·--from 이 로컬 git checkout 일 때)이 기록된다. "
            "--upstream 생략 시 --from 으로 폴백하되, --from 이 로컬 clone 이면 origin URL 을 자동도출한다 "
            "(릴리스 추적 기본). 재-import 시 현재 값으로 갱신."
        ),
    )
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--into", metavar="PATH", help="기존 프로젝트에 임베드 import(비파괴·백업·특정 케이스)")
    mode.add_argument("--new", metavar="PATH", help="PM 홈 생성 + git init (코드 없는 홈·표준 채택)")
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
                         "git clone 이면 `git remote get-url origin` 으로 URL 자동도출(릴리스 추적). "
                         "값은 안전 검증(scheme allowlist·credential 거부·leading-dash)을 통과해야 한다.")
    ap.add_argument("--name", help="{{PROJECT_NAME}} 값 (default: 대상 디렉토리명)")
    ap.add_argument("--fill", choices=FILL_CHOICES, default="manual",
                    help="자유서술 placeholder 채움 — auto: 하니스 구동 제안(opt-in), "
                         "manual: TODO 표시(default)")
    ap.add_argument("--fill-harness", choices=FILL_HARNESS_CHOICES, default=None,
                    help="fill 구동 하니스 (default: --harness; both→claude, claude 부재 시 opencode 폴백)")
    ap.add_argument("--opencode-model", dest="opencode_model", metavar="PROVIDER/MODEL",
                    default=None,
                    help="{{OPENCODE_PRO_MODEL}} 결정적 치환값 (비대화/CI). 예 'ollama/glm-5.2:cloud'. "
                         "opencode 어댑터 미포함이면 무시(claude-only)")
    ap.add_argument("--dry-run", action="store_true",
                    help="적용 없이 fill 계획만 출력 (실 하니스 미호출·파일시스템 미변경)")
    ap.add_argument("--board-submodule", action="store_true",
                    help="board(tickets+areas)를 별도 git submodule(.project_manager/board)로 셋업 "
                         "(공유 board). --new + --board-remote <url> 필수. "
                         "미지정(기본)=board 를 superproject inline(무변경).")
    ap.add_argument("--board-remote", dest="board_remote", metavar="URL", default=None,
                    help="공유 board 의 remote URL (--board-submodule 필수). 빈 remote 면 tickets "
                         "구조+areas.md 로 초기화 후 push, 내용 있으면 재사용(합류). URL 안전검증 "
                         "(credential-in-url·leading-dash·비허용 scheme 거부).")
    args = ap.parse_args(argv)

    # --upstream 명시값은 *부작용 전* 입구에서 fail-closed 검증. 나쁜 값
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

    # --board-submodule: board 를 별도 git submodule 로 셋업. --new + --board-remote
    #   <url> 필수(공유 board 의 본질=remote·미제공 fail-loud). --new 없이 단독은 거부(범위=신규 홈
    #   셋업). *부작용 전* 입구에서 fail-closed 검증(--upstream·MF2 git_init 규율 정합). inline 기본
    #   (플래그 없음)은 이 게이트를 통과해 완전 무변경.
    if args.board_submodule:
        if not is_new:
            print("오류: --board-submodule 은 --new 와 함께만 씁니다 (신규 PM 홈 셋업 범위). "
                  "기존 프로젝트엔 수동 `git submodule add` 를 쓰세요.", file=sys.stderr)
            return 1
        if not args.board_remote:
            print("오류: --board-submodule 은 --board-remote <url> 이 필수입니다 "
                  "(공유 board 의 본질은 remote). 예: --board-remote git@github.com:you/board.git",
                  file=sys.stderr)
            return 1
        ok, reason = validate_upstream_value(args.board_remote)
        if not ok:
            print(f"오류: --board-remote 값 거부 — {reason}", file=sys.stderr)
            return 1
    elif args.board_remote:
        print("오류: --board-remote 는 --board-submodule 과 함께 씁니다 "
              "(board submodule 셋업 없이는 무의미).", file=sys.stderr)
        return 1

    try:
        template_roots = resolve_template_roots(source_root, args.harness)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    # (): --new 대상이 기존 *파일* 이면 아래 iterdir() 가 NotADirectoryError 로
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

    # 다중 tree import의 manifest 합집합은 **어떤 복사보다 먼저** 읽고 병합 가능성을 검증한다.
    # 후순위 manifest 부재/병합 실패를 복사 뒤 발견하면 --new/--into 모두 부분 설치가 남는다.
    try:
        prepared_manifest_union = _prepare_selected_manifest_union(template_roots)
    except (OSError, ValueError, RuntimeError) as exc:
        print(
            "오류: 선택된 어댑터 manifest 합집합을 만들 수 없어 복사 전에 중단합니다 — "
            f"{exc}",
            file=sys.stderr,
        )
        return 1

    # --into 백업을 중앙 디렉토리 `<dest>/.pm_import_backups/<DATE>/` 로 모은다.
    #   --new 는 빈 디렉토리 보장이라 백업 없음(backup_root=None). git_safe = '추적&미변경'
    #   relpath 집합(또는 None=비-git·판정불가). git 호출 실패는 None→전부 백업(보수적 폴백).
    backup_root = None if is_new else dest_root / BACKUP_DIR_NAME / today
    git_safe = None if is_new else git_safe_relpaths(dest_root)
    try:
        actions = plan_copy(template_roots, dest_root, backup_root, args.weight,
                            git_safe=git_safe)
        # local.conf 백업 target 의 조상도 plan 단계에서 검증한다. 이 백업은
        #   plan_copy actions 밖(apply 후 backup_existing_local_conf)에서 일어나므로, 그 조상이
        #   일반 파일/symlink 면 *복사가 일부 끝난 뒤* mkdir 실패로 부분 적용이 남는다 → 사전 차단.
        if backup_root is not None and not is_new:
            _local_conf = dest_root / ".project_manager" / "local.conf"
            if _local_conf.is_file() or _local_conf.is_symlink():
                _check_ancestor_safe(
                    dest_root, backup_root / ".project_manager" / "local.conf", set())
    except (
        FileVsDirConflict,
        AncestorConflict,
        EmptyTemplateShippingInventoryError,
    ) as exc:
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
    # 백업이 중앙 디렉토리이고, git-safe(추적&미변경)는 백업 생략임을 한 줄로 요약한다.
    if not is_new:
        git_note = (
            f"git work tree (추적&미변경 {n_git_safe} 백업 생략)"
            if git_safe is not None
            else "비-git/판정불가 (충돌 전부 백업)"
        )
        print(f"  백업 위치: {BACKUP_DIR_NAME}/{today}/  · {git_note}")
    print(f"  → {n_copy} 파일 복사 ({n_backup} 백업), placeholder 치환, board.py init")
    if len(template_roots) > 1:
        print(
            f"  engine.manifest: 선택된 {len(template_roots)}개 트리 선언의 합집합 "
            "(중복 경로는 선언 순서상 첫 트리 우선)"
        )
    if args.board_submodule:
        print(f"  board submodule: {args.board_remote} → {_BOARD_SUBMODULE_PATH} "
              f"(빈 remote 면 구조 init+push·ignore=all)")

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
        # opencode 모델 결정적 해소 계획(LLM 아님·fill 이전 단계). 복사 *예정* src 에
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

    merged_manifest_entries = _install_selected_manifest_union(
        prepared_manifest_union, dest_root)
    if merged_manifest_entries:
        print(
            f"✓ engine.manifest 선택 트리 합집합 설치 "
            f"({len(template_roots)}개 flavor · {merged_manifest_entries}개 관리 경로)"
        )

    # MF1: 치환 범위 = 이번 run 이 복사한 파일만(복사 안 한 사용자 파일 불가침).
    copied_relpaths = {a.dst.relative_to(dest_root) for a in actions}
    subs = _substitution_map(project_name, dest_root, today)
    n_subst = substitute_placeholders(dest_root, subs, copied_relpaths)
    print(f"✓ {n_copy} 파일 복사 · {n_subst} 파일 placeholder 치환")

    # ── opencode 모델 결정적 해소: substitute *직후*·render *이전*. @render 활성화
    #    로 .opencode/agents 가 render 대상이 됐으므로, render_managed_files 가 model: 줄의
    #    {{OPENCODE_PRO_MODEL}} 을 만나기 *전* 에 해소해야 한다 — flag/interactive=토큰 치환,
    #    todo(미해소)=줄 주석화 + 토큰 중화(<provider/model>) → 어느 경로든 render 시점엔 토큰 0.
    #    (활성화가 그 순서
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

    # render 단계: @render manifest path 의 복사본을 render_adapter
    # 산출물로 다시 쓴다 — operational 토큰(subs·이미 sed) 치환. free-form value-fill 은 
    # 로 제거(FILL 채널이 canonical home 전담). substitute·모델해소 *직후*. 범위 = copied_relpaths(비파괴).
    n_render = render_managed_files(dest_root, subs, copied_relpaths)
    if n_render:
        print(f"✓ {n_render} 파일 render (operational 토큰 치환)")

    # ── board submodule 셋업: --new --board-submodule 일 때만. board.py init
    #    (아래 run_board_init) *이전* 에 둬야 (a) board_root() 가 board/ 로 해소되고 (b) board.py
    #    init 의 _configure_board_submodule() 이 .git/config ignore=all 을 설정한다. substitute·
    #    render *이후* — 복사·치환이 끝난 wiki/tickets 를 스캐폴드로 board 에 seed 하기 때문. inline
    #    기본(플래그 없음)은 이 블록을 건너뛰어 완전 무변경(현행 --new 회귀). 실패 시 fail-loud.
    if is_new and args.board_submodule:
        board_rc = setup_board_submodule(dest_root, args.board_remote)
        if board_rc != 0:
            print("오류: board submodule 셋업 실패 — import 미완(board 미배선·부분 홈).",
                  file=sys.stderr)
            return board_rc

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

    # board.py init 은 project_name 빈값·test_cmd=`pytest -q` 를 하드코딩한다.
    # init 성공 직후 local.conf 의 operational 해소값(project_name·test_cmd·py)을 sed
    # 치환값과 정렬해 엔진 문서(local.conf 해소)와 CLAUDE.md(치환)가 같은 값을 보게 한다.
    if sync_local_conf(dest_root, project_name):
        print("✓ local.conf operational 값 동기화 (project_name·test_cmd·py)")

    # MF1: init 이 덮은 local.conf 위에, 백업해 둔 기존 사용자 키 중 init 이 *안 쓴* 것
    #      (external_review·reviewer_cmd·prefix 등)을 재병합. init/operational sync 값은 우선.
    if preserved_conf_text is not None:
        reapply_preserved_conf_keys(dest_root, preserved_conf_text)

    # upstream 값을 local.conf 에 upstream= 으로 기록한다. board init·conf
    #   sync·preserve 단계 *이후* 에 둬야 한다 — 그래야 재-import 에서도 preserve 가 stale 값을
    #   붙들지 않고 *현재 값* 으로 확정된다(_set_conf_keys 제자리 갱신). 이후 pm_update 가
    #   --from 생략 시 이 값을 기본 upstream 으로 쓴다(--new·--into 공통).
    #
    #   --from(파일 소스)↔--upstream(update 기록) 디커플:
    #     --upstream 명시      → 그 값(URL|경로·이미 입구에서 검증됨).
    #     생략 + --from 이 로컬 git clone → origin URL 자동도출(릴리스 추적 기본).
    #     생략 + 도출 실패(git repo 아님·origin 부재) → --from 경로(기존 동작 회귀 보존).
    upstream_value = args.upstream
    if upstream_value is None:
        derived = derive_origin_url(source_root)
        upstream_value = derived if derived is not None else str(source_root)
    if record_upstream(dest_root, upstream_value):
        print(f"✓ local.conf upstream 기록 (pm_update --from 기본값): {upstream_value}")

    # upstream_rev baseline 기록 — --from 이 로컬 git checkout
    # 이면 그 HEAD commit 을 baseline 으로 박는다("마지막 동기 이후 변경" 의 기준점). git repo
    # 아님·HEAD 해소 실패면 graceful 생략(URL upstream 은 로컬 checkout 이 없어 baseline 없음 —
    # 스킬층이 fetch 후 upstream_seen_rev 를 별도 기록·별개 키).
    baseline_rev = read_upstream_rev(source_root)
    if baseline_rev and record_upstream_rev(dest_root, baseline_rev):
        print(f"✓ local.conf upstream_rev baseline 기록 (drift-lint 기준점): {baseline_rev}")

    # ── opencode 모델 local.conf 기록: board init·conf sync 가 local.conf 를 만든 *뒤*.
    #    실제 모델을 해소한 경로(flag·interactive)만 기록 — 이후 pm_update @render 가
    #    {{OPENCODE_PRO_MODEL}} 을 local.conf 에서 재유도할 때 키 부재로 leak assertion crash 하는
    #    걸 막는다. todo(미해소)는 위 resolve 가 토큰을 주석화+중화(<provider/model>)했으니 기록
    #    안 함(키 없어도 어댑터에 토큰 0 → leak 없음). claude import 는 active=False 라 자연 skip.
    #    (resolve_opencode_model 자체는 render 이전으로 이동·위 substitute 직후 블록 참조.)
    if model_result.active and model_result.path in ("flag", "interactive") \
            and model_result.model:
        if record_opencode_model(dest_root, model_result.model):
            print(f"✓ local.conf opencode_pro_model 기록 ({model_result.model})")

    # ── fill 단계: board init·conf sync 직후 hook. 자유서술 placeholder 처리.
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

    # pm_playbook.local 스텁 생성: fill 과 같은 자리 — 인스턴스 소유
    # 누적 학습 칸. 루트 .local 은 manifest 밖이라 복사로 안 오니 여기서 생성한다. 재-import
    # 에서 기존 .local 은 비파괴 보존(누적 학습 손실 방지·local.conf 백업 철학과 같은 결).
    playbook_status = ensure_pm_playbook_local_stub(dest_root, backup_root)
    if playbook_status == "created":
        print(f"✓ pm_playbook.local.md 스텁 생성 ({PM_PLAYBOOK_LOCAL_RELPATH})")
    else:
        print("  pm_playbook.local.md 기존 파일 비파괴 보존 (인스턴스 소유 — 덮지 않음).")

    # dest 가 git repo(git_safe is not None)이고 이번에 중앙 백업 디렉토리가
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
    # codex 어댑터는 laydown 후 trusted project + hook trust 2단계 승인이 있어야 발화.
    if args.harness == "codex":
        _print_codex_trust_guidance()
    return 0


if __name__ == "__main__":
    sys.exit(main())
