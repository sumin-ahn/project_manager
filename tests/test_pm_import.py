"""pm_import.py 단위 테스트 — 기계 단계(복사·sed·board init·백업·dry-run·idempotent).

plan/apply 분리 설계 덕에 임시 디렉토리만으로 외부 의존 없이 테스트한다. board.py init 은
복사된 트리의 board.py 를 동일 인터프리터로 subprocess 호출 — local.conf·pm_state 산출을
실제로 검증한다(LLM·네트워크 0 = 토큰 0).
"""
from __future__ import annotations

import argparse
import datetime
import importlib.util
import io
import itertools
import json
import os
import re
import shutil
import subprocess
import warnings
from pathlib import Path

import pytest
import yaml

from _win_skip import _can_symlink
from _harness_matrix import HARNESSES, HARNESS_ADAPTER_DIRS, HARNESS_ROOT_DOC
from _textio import (
    dominant_newline_bytes, write_crlf, write_lf, write_matching_newlines,
)

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _track_source_tree(root: Path) -> None:
    """pm-update tracked-only 출하 fixture를 임시 checkout으로 만든다."""
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-f", "-A"], check=True)

# symlink 생성 불가 환경(권한 없는 Windows 등)에서 symlink 의존 테스트를 skip.
requires_symlink = pytest.mark.skipif(
    not _can_symlink(),
    reason="Windows: symlink requires Developer Mode/admin",
)

# operational placeholder 치환에서 제외하는 방법론 문서 (pm_import.SED_EXCLUDE_FLOOR·manifest 파생과 동치).
ENGINE_DOCS_KEEP_LITERAL = (
    ".project_manager/wiki/pm_role.md",
    ".project_manager/wiki/pm_playbook.md",
)

FREE_FORM_TOKENS = ("{{PROJECT_CONSTRAINTS}}", "{{PROTECTED_PATHS}}", "{{USER_GATE_ITEMS}}")

OPERATIONAL_TOKENS = (
    "{{PROJECT_NAME}}",
    "{{PROJECT_TAGLINE}}",
    "{{PROJECT_ROOT}}",
    "{{PY}}",
    "{{TEST_CMD}}",
    "{{DATE}}",
)


def _load_pm_import():
    spec = importlib.util.spec_from_file_location("pm_import", TOOLS / "pm_import.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_pm_config():
    spec = importlib.util.spec_from_file_location("pm_config", TOOLS / "pm_config.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_board():
    spec = importlib.util.spec_from_file_location("board_for_conf_writer_test", TOOLS / "board.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pm_import():
    return _load_pm_import()


def _patch_atomic_replace(monkeypatch, module, fake):
    """엔진이 원자 교체에 **실제로 부르는** seam(`file_lock.atomic_replace`)을 대역으로 바꾼다.

    `os.replace` 는 그 seam 의 POSIX 분기 구현 세부다 — Windows 분기는 Win32 rename 이라
    `os.replace` 에 건 주입을 지나지 않는다. pm_import 는 복구 채널이라 seam 을 import 시점이
    아니라 쓰기 경로에서 캐시 로더(`_load_file_lock()`·프로세스 1회)로 받으므로, 그 로더가
    돌려주는 모듈 객체에 건다.
    """
    seam = module._load_file_lock()
    monkeypatch.setattr(seam, "atomic_replace", fake)
    return seam


@pytest.fixture(scope="module")
def pm_config():
    return _load_pm_config()


@pytest.fixture(scope="module")
def pm_update():
    spec = importlib.util.spec_from_file_location("pm_update", TOOLS / "pm_update.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _hermetic_opencode_models(request, pm_import, monkeypatch):
    """T-0033: main() 의 opencode 경로(resolve_opencode_model)가 실제 `opencode models` CLI 를
    호출하지 않도록 `_real_models_runner` 를 (False, []) 로 고정한다.

    이게 없으면 opencode 가 설치된 개발/CI 환경에서 main(--harness opencode) 가 라이브 `opencode
    models` 를 호출해 테스트가 **비-hermetic**(설치 여부로 동작 분기)이 된다. (False, []) 고정 =
    "미설치" 동치 동작(TODO 폴백)으로 결정화한다. models_runner 를 직접 주입하는 resolve 단위
    테스트는 `_real_models_runner` 를 안 타므로 영향 없다. `_real_models_runner` 자체의 fail-soft
    를 검증하는 테스트만 opt-out 한다.
    """
    if request.function.__name__.startswith("test_real_models_runner"):
        return
    monkeypatch.setattr(pm_import, "_real_models_runner", lambda: (False, []))


def _consumption_time_token_owners(pm_import) -> dict[str, frozenset]:
    """채택자 트리에 **토큰-form 으로 남는 게 정상**인 (파일 → 토큰) 지도 (T-0578).

    소비 시점이 소유한 토큰이다 — 설치가 값으로 굳히면 manifest bare 등재의 byte-copy
    (`pm_update`)가 다음 흡수에서 토큰-form 을 되돌려 매 sync 마다 진동한다. 목록은 엔진 선언
    (`pm_import.CONSUMPTION_TIME_TOKENS`)에서 파생한다(손-열거 0 — 선언이 늘면 자동 추종).

    **템플릿이 만들어 내는 산출물은 빼지 않는다** — `pm_state.md` 같은 생성 산출물의 날짜는 dest
    트리의 엔진 사본이 채워야 하고, 그게 안 채워지면 채택자 실수령물에 리터럴 토큰이 남는다.
    산출물까지 면제하면 이 e2e 가 바로 그 결함(전파 누락·생성 지점 미배선)을 못 본다."""
    return dict(pm_import.CONSUMPTION_TIME_TOKENS)


def _grep_token_files(root: Path, token: str, *, exclude_engine_docs: bool = False,
                      token_owned_elsewhere: dict | None = None) -> list[Path]:
    """root 하위에서 token 을 포함한 파일 목록. node_modules 제외.

    `token_owned_elsewhere`(파일 → 토큰 집합)는 그 파일의 **그 토큰만** 제외한다 — 같은 파일의
    다른 operational 토큰이 새면 여전히 잡힌다(파일 통째 면제 아님)."""
    hits: list[Path] = []
    owned = token_owned_elsewhere or {}
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if any(part == "node_modules" for part in rel.parts):
            continue
        if not path.is_file():
            continue
        if token in owned.get(rel.as_posix(), frozenset()):
            continue
        if exclude_engine_docs:
            relp = rel.as_posix()
            # 엔진 문서/소스/생성-config 는 placeholder 대상이 아니라 *토큰명을 문서화*한다 — verbatim.
            #   - pm_role.md·pm_playbook.md (방법론 문서·기존)
            #   - .project_manager/tools/* (엔진 소스 .py — 주석/docstring 이 토큰 메커니즘 설명·T-0133)
            #   - local.conf (board init 헤더 주석이 해소 키를 `{{PY}}·{{PROJECT_NAME}}` 로 설명)
            #   - engine.manifest (엔진 메타데이터·verbatim copy — 주석이 토큰 메커니즘을 *설명*하며
            #     토큰을 담는다. pm_import.ENGINE_METADATA_RELPATHS 가 치환에서 제외하는 그 파일이고,
            #     codex flavor manifest 는 `.codex/agents … {{PROJECT_NAME}} 토큰 보유 → @render` 를
            #     문서화해 {{PROJECT_NAME}}/{{PROJECT_TAGLINE}} 를 리터럴로 담는다·헬퍼 제외집합이
            #     엔진 제외집합과 어긋나면 문서 토큰을 leak 으로 오탐한다).
            if (relp in ENGINE_DOCS_KEEP_LITERAL
                    or relp.startswith(".project_manager/tools/")
                    or relp == ".project_manager/local.conf"
                    or relp == ".project_manager/engine.manifest"):
                continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if token in text:
            hits.append(rel)
    return hits


# ── 심볼 존재 ──────────────────────────────────────────────────────────────

def test_exposes_symbols(pm_import):
    assert callable(pm_import.main)
    assert callable(pm_import.plan_copy)
    assert callable(pm_import.substitute_placeholders)
    assert callable(pm_import.resolve_template_roots)
    assert pm_import.REGISTERED_HARNESSES == tuple(pm_import.HARNESS_TEMPLATE_DIRS)
    assert pm_import.HARNESS_CHOICES == (
        *pm_import.REGISTERED_HARNESSES, "all")


# ── ① --new: 트리 존재 · board init 산출 · 잔여 operational {{ 0 ──────────────

def test_new_creates_tree_and_inits(pm_import, tmp_path):
    dest = tmp_path / "myproj"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "My Project"])
    assert rc == 0

    # 트리 존재 — 엔진 + claude 어댑터.
    assert (dest / ".project_manager" / "tools" / "board.py").is_file()
    assert (dest / ".project_manager" / "wiki" / "pm_role.md").is_file()
    assert (dest / ".claude" / "agents" / "developer.md").is_file()
    assert (dest / "CLAUDE.md").is_file()

    # board.py init 산출 — local.conf · pm_state.
    assert (dest / ".project_manager" / "local.conf").is_file()
    assert (dest / ".project_manager" / "wiki" / "pm_state.md").is_file()

    # --new 는 git init.
    assert (dest / ".git").exists()


@pytest.mark.parametrize("harness", HARNESSES)
def test_new_substitutes_operational_placeholders(pm_import, tmp_path, harness):
    """엔진 문서 외에는 잔여 operational {{ 0 — 채택자 트리를 **확장자 무관 전수 스캔**(전 하네스·
    codex 포함). 옛 형상은 claude(:135)/opencode(:2740) **손-복제** 두 벌이라 세 번째 하네스(codex)를
    못 봤다 — `.codex/agents/*.toml` 이 `{{PROJECT_NAME}}` 리터럴로 출하돼도 어느 게이트도 안 잡았다.
    파생 축(HARNESSES) 하나로 대체해 네 번째 하네스도 자동 편입시킨다(T-0429)."""
    dest = tmp_path / f"adopter-{harness}"
    rc = pm_import.main(["--new", str(dest), "--harness", harness, "--name", "P", "--fill", "manual"])
    assert rc == 0

    owned_elsewhere = _consumption_time_token_owners(pm_import)
    for token in OPERATIONAL_TOKENS:
        hits = _grep_token_files(dest, token, exclude_engine_docs=True,
                                 token_owned_elsewhere=owned_elsewhere)
        assert hits == [], f"{harness}: {token} 잔존(엔진 문서 제외): {hits}"


def test_new_project_name_applied(pm_import, tmp_path):
    """--name 값이 {{PROJECT_NAME}} 자리에 들어간다 (CLAUDE.md)."""
    dest = tmp_path / "namecheck"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "Banana Corp"])
    assert rc == 0
    claude_md = (dest / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Banana Corp" in claude_md
    assert "{{PROJECT_NAME}}" not in claude_md


def test_new_name_defaults_to_dirname(pm_import, tmp_path):
    """--name 생략 시 대상 디렉토리명이 {{PROJECT_NAME}} 로."""
    dest = tmp_path / "auto-named-proj"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude"])
    assert rc == 0
    claude_md = (dest / "CLAUDE.md").read_text(encoding="utf-8")
    assert "auto-named-proj" in claude_md


# ── 엔진 문서 토큰 계약 (T-0219: PY/TEST_CMD 폐기·PROJECT_NAME 리터럴 유지) ──

def test_engine_docs_keep_literal_placeholders(pm_import, tmp_path):
    """엔진 문서의 머신-가변 토큰은 폐기됐고({{PY}}·{{TEST_CMD}} 부재·T-0219 (c) 중립화 —
    문서 표기는 `python3` 관례 + 래퍼 self-resolve·test 명령은 local.conf 노브 지칭),
    project-truth 토큰({{PROJECT_NAME}})은 리터럴 유지된다(local.conf 런타임 해소 관례)."""
    dest = tmp_path / "p"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "P"])
    assert rc == 0
    for rel in ENGINE_DOCS_KEEP_LITERAL:
        text = (dest / rel).read_text(encoding="utf-8")
        assert "{{PY}}" not in text, f"{rel} 에 {{{{PY}}}} 잔존 — T-0219 로 폐기된 토큰."
        assert "{{TEST_CMD}}" not in text, f"{rel} 에 {{{{TEST_CMD}}}} 잔존 — T-0219 로 폐기된 토큰."
        if rel.endswith("pm_role.md"):  # PROJECT_NAME 리터럴은 pm_role 만 보유 (playbook 은 원래 없음)
            assert "{{PROJECT_NAME}}" in text, f"{rel} 에서 {{{{PROJECT_NAME}}}} 가 치환/유실 — 리터럴 유지여야 한다."


# ── A4: 치환-제외 집합은 dest engine.manifest 파생 (T-0329·codex must-fix 재작업) ──

def test_sed_exclude_floor_and_canonical_derivation(pm_import):
    """치환-제외가 하드코딩/모듈-시점 상수 대신 파생이고, 리터럴 floor·canonical manifest 파생 모두
    현행 방법론 문서 집합과 일치한다.

    - SED_EXCLUDE_FLOOR = broken-manifest fail-soft floor(should-fix) == {pm_role, pm_playbook}.
    - 이 repo canonical manifest 파생도 동일(직속 템플릿 pm_state.template.md·서브디렉토리
      _template.md 는 비편입).
    - 모듈-시점 상수 `SED_EXCLUDE_RELPATHS` 는 제거됐다(must-fix — dest 시점 산출로 대체)."""
    assert pm_import.SED_EXCLUDE_FLOOR == frozenset(ENGINE_DOCS_KEEP_LITERAL)
    canonical = pm_import._derive_sed_exclude_relpaths(
        REPO / ".project_manager" / "engine.manifest"
    )
    assert canonical == frozenset(ENGINE_DOCS_KEEP_LITERAL)
    assert not hasattr(pm_import, "SED_EXCLUDE_RELPATHS"), \
        "모듈-시점 상수는 제거돼야 한다(실행 checkout 이 아니라 dest 시점 산출·must-fix)"


def test_sed_exclude_auto_includes_new_methodology_doc(pm_import, tmp_path):
    """신규 방법론 .md 가 manifest 에 추가되면 파생 제외 집합에 자동 편입된다(수동 목록 불요).

    함께 못박는 비편입 경계: 직속 템플릿 스캐폴드(`pm_state.template.md`)·서브디렉토리
    템플릿(`tickets/_template.md`)·비-wiki 경로(`.claude/agents`)는 편입되지 않는다."""
    manifest = tmp_path / "engine.manifest"
    manifest.write_text(
        "# 방법론 문서 절\n"
        ".project_manager/tools/board.py\n"
        ".project_manager/wiki/pm_role.md\n"
        ".project_manager/wiki/pm_playbook.md\n"
        ".project_manager/wiki/pm_newdoc.md\n"            # 신규 방법론 문서 — 자동 편입 대상
        ".project_manager/wiki/pm_state.template.md\n"    # 직속 템플릿 스캐폴드 — 비편입
        ".project_manager/wiki/tickets/_template.md\n"    # 서브디렉토리 — 비편입(직속 아님)
        ".claude/agents  @render\n",                      # 비-wiki — 비편입
        encoding="utf-8",
    )
    derived = pm_import._derive_sed_exclude_relpaths(manifest)
    assert derived == frozenset({
        ".project_manager/wiki/pm_role.md",
        ".project_manager/wiki/pm_playbook.md",
        ".project_manager/wiki/pm_newdoc.md",
    })


def test_sed_exclude_missing_manifest_floors_not_empty(pm_import, tmp_path):
    """manifest 부재/로드 실패 시 빈 집합이 아니라 리터럴 floor 로 폴백 — broken-manifest 엣지에서도
    기존 제외(pm_role·pm_playbook)를 조용히 잃지 않는다(should-fix)."""
    derived = pm_import._derive_sed_exclude_relpaths(tmp_path / "absent.manifest")
    assert derived == pm_import.SED_EXCLUDE_FLOOR
    assert derived == frozenset(ENGINE_DOCS_KEEP_LITERAL)


def test_substitute_excludes_new_methodology_doc_via_dest_manifest(pm_import, tmp_path):
    """`--from <신 upstream>` 흡수 회귀 (codex must-fix): dest 인스턴스 manifest 에 신규 직속 방법론
    .md 가 실리면 substitute 가 그 문서를 치환에서 제외한다 — 제외 집합이 *실행 checkout* 이 아니라
    복사가 끝난 *dest* manifest 기준으로 산출되기 때문.

    pm_newdoc.md 는 이 repo(실행 checkout) manifest 엔 없다 — 모듈-시점 상수였다면 제외 못 해 그
    문서의 {{PROJECT_NAME}}(메커니즘 설명)가 오치환됐을 것이다. 대조로 scaffold(status.md)의 실
    placeholder 는 정상 치환된다."""
    dest = tmp_path / "inst"
    wiki = dest / ".project_manager" / "wiki"
    wiki.mkdir(parents=True)
    (dest / ".project_manager" / "engine.manifest").write_text(
        ".project_manager/wiki/pm_role.md\n"
        ".project_manager/wiki/pm_playbook.md\n"
        ".project_manager/wiki/pm_newdoc.md\n",   # 신 upstream 이 실은 신규 직속 방법론 문서
        encoding="utf-8",
    )
    # 방법론 문서: {{PROJECT_NAME}} 를 *메커니즘 설명*으로 담음(치환되면 오치환).
    (wiki / "pm_newdoc.md").write_text(
        "설명: {{PROJECT_NAME}} 는 local.conf 가 런타임 해소한다.\n", encoding="utf-8")
    # scaffold: {{PROJECT_NAME}} 를 *실 placeholder* 로 담음(치환 대상).
    (wiki / "status.md").write_text("# {{PROJECT_NAME}} 보드\n", encoding="utf-8")
    copied = {
        Path(".project_manager/wiki/pm_newdoc.md"),
        Path(".project_manager/wiki/status.md"),
    }
    n = pm_import.substitute_placeholders(dest, {"{{PROJECT_NAME}}": "Acme"}, copied)
    assert "{{PROJECT_NAME}}" in (wiki / "pm_newdoc.md").read_text(encoding="utf-8"), \
        "신규 방법론 문서가 오치환 — dest manifest 파생 제외 미적용(module-시점 상수 회귀)"
    assert "Acme" in (wiki / "status.md").read_text(encoding="utf-8")
    assert n == 1  # status.md 만 치환(pm_newdoc.md 는 제외)


# ── D11 seam: local.conf operational 값이 sed 치환값과 일치 ─────────────────

def _parse_conf(path: Path) -> dict[str, str]:
    """local.conf 를 key=value dict 로 파싱 (주석·빈 줄 제외)."""
    conf: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" in stripped:
            key, value = stripped.split("=", 1)
            conf[key.strip()] = value.strip()
    return conf


def test_local_conf_operational_values_synced(pm_import, tmp_path):
    """board.py init 후 local.conf 의 project_name·test_cmd·py 가 pm_import 치환값과 일치한다.

    D11 seam: board.py init 은 project_name 빈값·test_cmd=`pytest -q` 를 하드코딩하므로,
    pm_import 가 init 직후 local.conf operational 값을 sed 치환값(--name·DEFAULT_TEST_CMD·
    _detected_py())으로 동기화해야 한다. 파일 존재만 보는 기존 테스트로는 이 불일치를 못 잡는다.
    """
    dest = tmp_path / "confsync"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "Banana Corp"])
    assert rc == 0

    conf = _parse_conf(dest / ".project_manager" / "local.conf")
    assert conf.get("project_name") == "Banana Corp", \
        f"local.conf project_name 이 --name 과 불일치: {conf.get('project_name')!r}"
    assert conf.get("test_cmd") == pm_import._default_test_cmd(), \
        f"local.conf test_cmd 이 _default_test_cmd() 와 불일치: {conf.get('test_cmd')!r}"
    assert conf.get("py") == pm_import._detected_py(), \
        f"local.conf py 가 _detected_py() 와 불일치: {conf.get('py')!r}"


def test_local_conf_preserves_board_init_keys(pm_import, tmp_path):
    """operational 값 동기화가 board.py init 이 쓴 다른 키(ctx 예산 등)·주석을 보존한다.

    T-0779: session=/prefix= 는 board init 이 더는 쓰지 않는다(폐지 — 세션은 lease 장부,
    prefix 는 areas.md 가 단일 진실). "동기화가 sync 대상 밖 키를 clobber 하지 않는다" 는
    이 테스트의 계약은 유효하므로, init 이 실제로 쓰는 ctx 예산 키(sync_local_conf 가
    건드리지 않는 project_name/test_cmd/py 밖 키)로 갱신한다.
    """
    board = _load_board()
    dest = tmp_path / "confpreserve"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "Keep"])
    assert rc == 0

    local_conf = dest / ".project_manager" / "local.conf"
    conf = _parse_conf(local_conf)
    # board.py init 이 쓰는 ctx 예산 키가 살아있어야 한다(clobber 아님 — 키 단위 갱신).
    assert conf.get("ctx_nudge_pct") == str(board.CTX_NUDGE_PCT_DEFAULT), \
        f"ctx_nudge_pct 키가 동기화로 손실됨: {conf.get('ctx_nudge_pct')!r}"
    # session·prefix 는 폐지 키 — init 이 쓰지 않는다(T-0779).
    assert conf.get("session") is None, f"session 키가 폐지됐는데 존재: {conf.get('session')!r}"
    assert conf.get("prefix") is None, f"prefix 키가 폐지됐는데 존재: {conf.get('prefix')!r}"
    # 주석 줄도 보존(board.py init 의 헤더 주석).
    text = local_conf.read_text(encoding="utf-8")
    assert text.startswith("#"), "local.conf 머리 주석이 동기화로 사라짐."


def test_local_conf_sync_idempotent(pm_import, tmp_path):
    """--into 재실행 시 동기화가 멱등 — 키 중복 없이 같은 값 유지."""
    dest = tmp_path / "confidem"
    rc1 = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "Idem"])
    assert rc1 == 0
    rc2 = pm_import.main(["--into", str(dest), "--harness", "claude", "--name", "Idem"])
    assert rc2 == 0

    text = (dest / ".project_manager" / "local.conf").read_text(encoding="utf-8")
    # 동기화 키가 정확히 한 줄씩만 존재(중복 추가 없음).
    for key in ("project_name", "test_cmd", "py"):
        occurrences = [
            line for line in text.splitlines()
            if line.strip().split("=", 1)[0].strip() == key and not line.lstrip().startswith("#")
        ]
        assert len(occurrences) == 1, f"{key} 가 {len(occurrences)}회 등장 — 멱등 위반(중복)."


# ── T-0053: import 가 source(--from)를 local.conf 의 upstream= 으로 기록 ──────

# T-0145 디커플: --upstream 생략 시 --from 이 로컬 git clone 이면 origin URL 을 자동도출한다
# (ADR-0032 D4 릴리스 추적 기본). 아래 *기존 동작 회귀 보존* 테스트들은 derive_origin_url 을
# None(=origin 부재·non-git source)으로 monkeypatch 해 "--from 경로 그대로 기록" 의 기존 계약을
# 결정적으로 검증한다(REPO 는 실 git checkout 이라 patch 없으면 origin URL 이 도출됨). origin
# 도출·--upstream 명시 등 *신규* 디커플 경로는 별도 테스트(아래)가 검증한다.

def test_new_records_upstream_in_local_conf(pm_import, tmp_path, monkeypatch):
    """--new import 후 local.conf 에 upstream=<resolved --from> 이 기록된다(origin 부재 시·기존 동작).

    --from 생략 시 default=REPO 이고 origin 도출이 None(monkeypatch)이면, upstream 은 이 repo
    루트의 resolve() 절대경로여야 한다(경로 fallback·회귀 보존). 이후 pm_update 가 --from 없이
    이 값을 기본 upstream 으로 쓴다(T-0053).
    """
    monkeypatch.setattr(pm_import, "derive_origin_url", lambda *a, **k: None)
    dest = tmp_path / "up_new"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "U"])
    assert rc == 0

    conf = _parse_conf(dest / ".project_manager" / "local.conf")
    assert conf.get("upstream") == str(REPO), \
        f"upstream 이 default source(REPO)와 불일치: {conf.get('upstream')!r}"


def test_new_records_upstream_explicit_from(pm_import, tmp_path, monkeypatch):
    """명시 `--from` 이 그 source 의 resolve() 절대경로로 upstream= 에 기록되는 *배선*을 검증한다.

    실 import 은 source 가 유효 프레임워크 checkout(`templates/<harness>/`)이어야 하므로 여기선
    `--from REPO`(=기본값)로 배선만 확인한다(origin 도출 None patch·경로 fallback). *주어진
    source != 기본값* 일 때 그 값이 기록된다는 값-구분 계약은 `test_record_upstream_unit`이 강제한다.
    """
    monkeypatch.setattr(pm_import, "derive_origin_url", lambda *a, **k: None)
    rc = pm_import.main(["--new", str(tmp_path / "up_expl"), "--harness", "claude",
                         "--from", str(REPO), "--name", "E"])
    assert rc == 0
    conf = _parse_conf(tmp_path / "up_expl" / ".project_manager" / "local.conf")
    assert conf.get("upstream") == str(REPO), \
        f"명시 --from 이 upstream 으로 기록 안 됨: {conf.get('upstream')!r}"


def test_into_records_upstream_in_local_conf(pm_import, tmp_path, monkeypatch):
    """--into 재-import 후에도 local.conf 에 upstream= 이 기록된다(origin 부재 시·기존 동작)."""
    monkeypatch.setattr(pm_import, "derive_origin_url", lambda *a, **k: None)
    dest = tmp_path / "up_into"
    rc1 = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "I"])
    assert rc1 == 0
    rc2 = pm_import.main(["--into", str(dest), "--harness", "claude", "--name", "I"])
    assert rc2 == 0

    conf = _parse_conf(dest / ".project_manager" / "local.conf")
    assert conf.get("upstream") == str(REPO), \
        f"--into 후 upstream 불일치: {conf.get('upstream')!r}"


def test_reimport_updates_stale_upstream(pm_import, tmp_path, monkeypatch):
    """재-import 는 upstream 을 *현재 source 로 갱신*한다 — preserve 가 stale 값을 붙들지 않는다.

    1차 import 후 local.conf 의 upstream 을 가짜 stale 경로로 손수 바꾼 뒤 재-import 하면,
    upstream 이 현재 source(REPO)로 덮여야 한다(stale 보존 아님·origin 부재 시 경로 fallback).
    upstream 키 한 줄만 등장.
    """
    monkeypatch.setattr(pm_import, "derive_origin_url", lambda *a, **k: None)
    dest = tmp_path / "up_stale"
    rc1 = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "S"])
    assert rc1 == 0

    local_conf = dest / ".project_manager" / "local.conf"
    text = local_conf.read_text(encoding="utf-8")
    stale = "/nonexistent/old/checkout"
    text = re.sub(r"(?m)^upstream=.*$", f"upstream={stale}", text)
    assert stale in text  # 손수 stale 주입 확인
    local_conf.write_text(text, encoding="utf-8")

    rc2 = pm_import.main(["--into", str(dest), "--harness", "claude", "--name", "S"])
    assert rc2 == 0

    conf = _parse_conf(local_conf)
    assert conf.get("upstream") == str(REPO), \
        f"재-import 가 stale upstream 을 갱신하지 않음: {conf.get('upstream')!r}"
    # upstream 키가 정확히 한 줄(중복 추가 없음).
    occurrences = [
        line for line in local_conf.read_text(encoding="utf-8").splitlines()
        if line.strip().split("=", 1)[0].strip() == "upstream"
        and not line.lstrip().startswith("#")
    ]
    assert len(occurrences) == 1, f"upstream 이 {len(occurrences)}회 등장 — 갱신이 아니라 중복."


def test_record_upstream_unit(pm_import, tmp_path):
    """record_upstream: 기존 upstream 줄은 제자리 갱신, 없으면 추가. 다른 키·주석 보존."""
    local_conf = tmp_path / ".project_manager" / "local.conf"
    local_conf.parent.mkdir(parents=True)
    local_conf.write_text("# header\nsession=pm\nupstream=/old/path\n", encoding="utf-8")

    changed = pm_import.record_upstream(tmp_path, Path("/new/checkout"))
    assert changed is True
    text = local_conf.read_text(encoding="utf-8")
    # record_upstream 은 str(Path) 를 OS-네이티브로 기록(엔진 결정) — Windows 역슬래시를
    # 경로 구분자만 정규화해 비교(POSIX 무변경·os.sep="/").
    assert "upstream=/new/checkout" in text.replace(os.sep, "/")
    assert "upstream=/old/path" not in text
    assert "session=pm" in text  # 타 키 보존
    assert text.startswith("# header")  # 주석 보존
    # 제자리 갱신이므로 upstream 한 줄만 (upstream_rev 등 다른 키는 이 호출이 안 씀).
    assert sum(
        1 for line in text.splitlines()
        if line.split("=", 1)[0].strip() == "upstream"
    ) == 1


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("project_name", "Writer Project"),
        ("test_cmd", "python3 -m pytest tests/ -q"),
        ("py", "python3"),
        ("upstream", "https://github.com/acme/framework.git"),
        ("upstream_rev", "abc123"),
        ("upstream_seen_rev", "def456"),
        ("opencode_pro_model", "openai/gpt-5.6"),
    ],
)
def test_general_conf_writer_normalizes_every_key_atomically_for_all_readers(
        pm_import, pm_config, tmp_path, monkeypatch, key, value):
    """일반 writer는 키 종류와 무관하게 유일화·atomic 기록하고 세 reader와 일치한다."""
    local_conf = tmp_path / ".project_manager" / "local.conf"
    local_conf.parent.mkdir(parents=True)
    local_conf.write_text(
        f"# header\n{key}=first\nsession=keep\n{key}=stale\n{key}=\n",
        encoding="utf-8",
    )
    real_replace = pm_import._load_file_lock().atomic_replace
    replacements = []

    def recording_replace(src, dst):
        replacements.append((Path(src), Path(dst)))
        real_replace(src, dst)

    _patch_atomic_replace(monkeypatch, pm_import, recording_replace)
    assert pm_import._write_conf_keys(local_conf, {key: value}) is True

    text = local_conf.read_text(encoding="utf-8")
    active = [
        line for line in text.splitlines()
        if not line.lstrip().startswith("#")
        and "=" in line
        and line.split("=", 1)[0].strip() == key
    ]
    assert active == [f"{key}={value}"]
    assert replacements == [(local_conf.with_suffix(".conf.tmp"), local_conf)]
    assert "session=keep" in text and text.startswith("# header\n")

    assert pm_import._parse_conf_keys(text)[key] == value
    monkeypatch.setattr(pm_config, "REPO", tmp_path)
    assert pm_config._local_conf_value(key) == value
    board = _load_board()
    board.LOCAL_CONF = local_conf
    assert board.local_config()[key] == value


# ── T-0145: --from↔--upstream 디커플 + origin 자동도출 + upstream_rev baseline ──

def test_record_upstream_accepts_url_string(pm_import, tmp_path):
    """record_upstream 은 URL 문자열도 받아 그대로 기록한다(디커플·URL 선호·T-0145)."""
    local_conf = tmp_path / ".project_manager" / "local.conf"
    local_conf.parent.mkdir(parents=True)
    local_conf.write_text("session=pm\nupstream=/old\n", encoding="utf-8")

    changed = pm_import.record_upstream(tmp_path, "https://github.com/foo/bar.git")
    assert changed is True
    conf = _parse_conf(local_conf)
    assert conf["upstream"] == "https://github.com/foo/bar.git"
    assert conf["session"] == "pm"  # 타 키 보존


def test_explicit_upstream_recorded_distinct_from_source(pm_import, tmp_path):
    """--upstream 명시값은 --from(파일 소스)과 *독립적으로* upstream= 에 기록된다(디커플·T-0145)."""
    dest = tmp_path / "up_explicit"
    url = "https://github.com/acme/proj.git"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "X",
                         "--from", str(REPO), "--upstream", url])
    assert rc == 0
    conf = _parse_conf(dest / ".project_manager" / "local.conf")
    assert conf.get("upstream") == url, \
        f"--upstream 명시값이 기록 안 됨(파일 소스 --from 과 디커플 실패): {conf.get('upstream')!r}"


def test_bad_upstream_rejected_before_import(pm_import, tmp_path):
    """나쁜 --upstream(leading-dash·비허용 scheme·credential)은 부작용 전 fail-closed 거부(T-0145)."""
    dest = tmp_path / "bad_up"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "B",
                         "--upstream", "http://insecure/x"])
    assert rc == 1, "비허용 scheme upstream 이 거부되지 않음"
    # 부작용 전 거부 — dest 가 생성되지 않았어야(import 진행 안 함).
    assert not dest.exists(), "거부됐는데도 import 부작용이 발생(dest 생성됨)"


def test_origin_url_auto_derived_when_from_is_clone(pm_import, tmp_path, monkeypatch):
    """--upstream 생략 + --from 이 로컬 clone 이면 origin URL 을 자동도출해 기록(릴리스 추적·T-0145)."""
    derived_url = "git@github.com:owner/repo.git"
    monkeypatch.setattr(pm_import, "derive_origin_url", lambda *a, **k: derived_url)
    dest = tmp_path / "up_origin"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "O",
                         "--from", str(REPO)])
    assert rc == 0
    conf = _parse_conf(dest / ".project_manager" / "local.conf")
    assert conf.get("upstream") == derived_url, \
        f"origin URL 자동도출 실패 — upstream={conf.get('upstream')!r}"


def test_upstream_rev_baseline_recorded_on_import(pm_import, tmp_path, monkeypatch):
    """import 시 --from checkout 의 HEAD 가 upstream_rev= baseline 으로 기록된다(drift 입력·T-0145)."""
    monkeypatch.setattr(pm_import, "derive_origin_url", lambda *a, **k: None)
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: "deadbeefcafe")
    dest = tmp_path / "up_rev"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "R"])
    assert rc == 0
    conf = _parse_conf(dest / ".project_manager" / "local.conf")
    assert conf.get("upstream_rev") == "deadbeefcafe", \
        f"upstream_rev baseline 미기록: {conf.get('upstream_rev')!r}"


def test_upstream_rev_skipped_when_source_not_git(pm_import, tmp_path, monkeypatch):
    """--from 이 git checkout 이 아니면(read_upstream_rev=None) upstream_rev 를 graceful 생략(T-0145)."""
    monkeypatch.setattr(pm_import, "derive_origin_url", lambda *a, **k: None)
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: None)
    dest = tmp_path / "up_norev"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "N"])
    assert rc == 0
    conf = _parse_conf(dest / ".project_manager" / "local.conf")
    assert "upstream_rev" not in conf, \
        f"git repo 아닌데 upstream_rev 가 기록됨: {conf.get('upstream_rev')!r}"


def test_record_upstream_rev_preserves_other_keys(pm_import, tmp_path):
    """record_upstream_rev: upstream_rev 만 set-or-replace, 타 키·주석 보존(T-0145)."""
    local_conf = tmp_path / ".project_manager" / "local.conf"
    local_conf.parent.mkdir(parents=True)
    local_conf.write_text(
        "# h\nsession=pm\nupstream=/x\nupstream_rev=old\n", encoding="utf-8")

    changed = pm_import.record_upstream_rev(tmp_path, "newrev123")
    assert changed is True
    conf = _parse_conf(local_conf)
    assert conf["upstream_rev"] == "newrev123"
    assert conf["upstream"] == "/x"   # 별개 키 보존(한 키 2역 금지)
    assert conf["session"] == "pm"


# ── T-0145: URL 안전 계약 (순수 검증·네트워크 0) ──────────────────────────────

def test_classify_upstream_url_vs_path(pm_import):
    """self-describing 분류 — scheme/scp→url · 경로/Windows 드라이브→path (분류 ≠ 허가)."""
    assert pm_import.classify_upstream("https://github.com/x/y.git") == "url"
    assert pm_import.classify_upstream("ssh://git@h/x") == "url"
    assert pm_import.classify_upstream("file:///srv/r.git") == "url"
    assert pm_import.classify_upstream("git@github.com:x/y.git") == "url"  # scp-style
    assert pm_import.classify_upstream("/home/u/checkout") == "path"
    assert pm_import.classify_upstream("../rel/path") == "path"
    assert pm_import.classify_upstream("C:\\repo") == "path"   # Windows 드라이브
    assert pm_import.classify_upstream("C:/repo") == "path"


def test_validate_upstream_value_safety_contract(pm_import):
    """URL 안전 계약 — allowlist(https/ssh/file)·credential·leading-dash·transport·ssh-주입 거부."""
    # 허용 — allowlist scheme + scp + 경로 + **scp path edge**(path 의 `@`·`:` 는 자유·MF3
    # round-2 회귀 박제: scp 는 첫 `:` 로 lhs↔path 분리·authority 는 lhs 안에서만 해석).
    for ok_val in (
        "https://github.com/x/y.git", "ssh://git@h/x", "ssh://git@h:22/x",
        "file:///srv/r.git", "git@github.com:x/y.git", "/home/u/checkout", "../rel",
        "host:path@v1.git",          # path 에 `@`(ref) — 정상 scp
        "host:path@with:colon",      # path 에 `@`+`:` — 정상 scp(false-reject 금지)
        "git@host:path",             # 기본 scp
        "git@host:sub/dir@ref",      # path 에 `/`·`@` — 정상 scp
    ):
        assert pm_import.validate_upstream_value(ok_val)[0] is True, ok_val
    # 거부.
    for bad_val in (
        "", "   ",                              # 빈/공백
        "--upload-pack=evil",                   # leading-dash(옵션 오인)
        "http://insecure/x",                    # 평문 http(SSRF/중간자)
        "git://h/x.git",                        # MF2: git:// 비인증 평문(MITM)·allowlist 밖
        "ftp://h/x",                            # 비허용 scheme
        "ext::sh -c evil",                      # transport helper(임의명령)
        "fd::17",                               # transport helper
        "https://user:pass@github.com/x.git",   # credential-in-URL(scheme-form)
        "ssh://-oProxyCommand=sh/repo",         # MF3: ssh 옵션 주입(host leading-dash)
        "ssh://git@-oProxyCommand=sh/repo",     # MF3: ssh 옵션 주입(host leading-dash·userinfo 有)
        "git@-evil:x.git",                      # MF3: scp host leading-dash
    ):
        assert pm_import.validate_upstream_value(bad_val)[0] is False, bad_val


def test_derive_origin_url_unit(pm_import):
    """derive_origin_url: origin URL 도출·origin 부재 None·도출 URL 검증 실패 None(T-0145)."""
    assert pm_import.derive_origin_url(
        Path("/x"), git_runner=lambda a: (0, "git@github.com:o/r.git\n")
    ) == "git@github.com:o/r.git"
    # origin 부재(rc!=0) → None.
    assert pm_import.derive_origin_url(
        Path("/x"), git_runner=lambda a: (1, "no remote")) is None
    # 도출 URL 이 안전 검증 실패(비허용 scheme) → None(나쁜 값 자동기록 차단).
    assert pm_import.derive_origin_url(
        Path("/x"), git_runner=lambda a: (0, "http://insecure/x\n")) is None


def test_read_upstream_rev_unit(pm_import):
    """read_upstream_rev: HEAD commit 읽기·git repo 아님 None(T-0145)."""
    assert pm_import.read_upstream_rev(
        Path("/x"), git_runner=lambda a: (0, "abc123def\n")) == "abc123def"
    assert pm_import.read_upstream_rev(
        Path("/x"), git_runner=lambda a: (128, "not a git repo")) is None


def test_upstream_git_runner_isolates_global_config(pm_import, monkeypatch):
    """MF4: 네트워크-facing runner 가 global/system git config 를 격리한다(insteadOf·helper 차단).

    실 git 을 부르지 않고 subprocess.run 을 가로채 *전달된 env* 를 검사한다 — GIT_CONFIG_GLOBAL/
    SYSTEM=os.devnull 로 global·system config 무력화 + GIT_CONFIG_COUNT 패턴으로 credential.
    helper=(빈값)·protocol allowlist(https/ssh/file always·기본 never) 강제 + GIT_TERMINAL_PROMPT=0.
    """
    import os as _os
    captured = {}

    class _Result:
        returncode = 0
        stdout = "ok\n"
        stderr = ""

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env", {})
        return _Result()

    # hardening: 상속된 protocol 우회 env 가 *있어도* runner 가 중화(pop)하는지 검증.
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "ext")
    monkeypatch.setenv("GIT_PROTOCOL_FROM_USER", "1")
    monkeypatch.setattr(pm_import.shutil, "which", lambda b: "/usr/bin/git")
    monkeypatch.setattr(pm_import.subprocess, "run", fake_run)
    runner = pm_import._real_upstream_git_runner()
    rc, _out = runner(["ls-remote", "https://github.com/x/y.git"])
    assert rc == 0
    env = captured["env"]
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_GLOBAL"] == _os.devnull
    assert env["GIT_CONFIG_SYSTEM"] == _os.devnull
    # hardening 2: protocol 우회 env 가 중화(pop)됐는지 — 우리 allowlist 가 단일 권위.
    assert "GIT_ALLOW_PROTOCOL" not in env, "GIT_ALLOW_PROTOCOL 미중화(allowlist 우회 가능)"
    assert "GIT_PROTOCOL_FROM_USER" not in env, "GIT_PROTOCOL_FROM_USER 미중화"
    # GIT_CONFIG_COUNT 패턴 — credential.helper=(빈값)·protocol allowlist·followRedirects.
    count = int(env["GIT_CONFIG_COUNT"])
    kvs = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(count)}
    assert kvs.get("credential.helper") == "", "credential.helper 빈값 강제 안 됨"
    assert kvs.get("protocol.allow") == "never", "protocol 기본 거부 안 됨"
    assert kvs.get("protocol.https.allow") == "always"
    assert kvs.get("protocol.ssh.allow") == "always"
    assert kvs.get("protocol.file.allow") == "always"
    # hardening 1: redirect 추적 차단(D5 잔여 SSRF 표면).
    assert kvs.get("http.followRedirects") == "false", "http.followRedirects 차단 안 됨"
    # argv 에 shell 해석 없이 그대로 — no-shell(argv-list) 계약.
    assert captured["argv"][0] == "/usr/bin/git"
    assert "ls-remote" in captured["argv"]


def test_set_conf_keys_replaces_in_place(pm_import):
    """_set_conf_keys: 기존 키는 제자리 교체, 없는 키만 추가. 주석·타 키·순서 보존."""
    text = (
        "# header comment\n"
        "session=pm\n"
        "py=python3\n"
        "test_cmd=pytest -q\n"
        "project_name=\n"
    )
    out = pm_import._set_conf_keys(text, {
        "project_name": "X",
        "test_cmd": "python3 -m pytest tests/ -q",
        "py": "python3",
    })
    conf = {}
    for line in out.splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            conf[k.strip()] = v.strip()
    assert conf["project_name"] == "X"
    assert conf["test_cmd"] == "python3 -m pytest tests/ -q"
    assert conf["py"] == "python3"
    assert conf["session"] == "pm"  # 무관 키 보존.
    assert out.startswith("# header comment\n")  # 주석 보존.
    # 제자리 교체 — 새 줄 추가 없이 줄 수 동일.
    assert len(out.splitlines()) == len(text.splitlines())


def test_set_conf_keys_appends_missing(pm_import):
    """_set_conf_keys: 키가 없으면 끝에 추가한다."""
    text = "session=pm\n"
    out = pm_import._set_conf_keys(text, {"project_name": "Y"})
    assert "project_name=Y" in out
    assert "session=pm" in out


# ── ② --harness 집합: 어댑터 공존 ────────────────────────────────────────────

def test_harness_combination_is_order_independent_and_duplicate_safe(pm_import, tmp_path):
    dest = tmp_path / "dual"
    assert pm_import.parse_harness_selection("opencode,claude,opencode") == (
        "claude", "opencode")
    rc = pm_import.main([
        "--new", str(dest), "--harness", "opencode,claude,opencode", "--name", "Dual",
    ])
    assert rc == 0
    # claude 어댑터.
    assert (dest / ".claude").is_dir()
    assert (dest / "CLAUDE.md").is_file()
    # opencode 어댑터.
    assert (dest / ".opencode").is_dir()
    assert (dest / "AGENTS.md").is_file()
    # 공유 엔진은 한 벌.
    assert (dest / ".project_manager" / "tools" / "board.py").is_file()


def test_harness_combination_excludes_node_modules(pm_import, tmp_path):
    """opencode 의 node_modules 는 무겁고 재설치 대상 — 복사 제외."""
    dest = tmp_path / "dual2"
    rc = pm_import.main([
        "--new", str(dest), "--harness", "claude,opencode", "--name", "D2",
    ])
    assert rc == 0
    assert not (dest / ".opencode" / "node_modules").exists()


def test_all_is_derived_from_registry_and_auto_includes_fourth_harness(
        pm_import, tmp_path, monkeypatch):
    """``all``은 고정 3-tuple이 아니라 호출 시점 registry에서 파생된다(sensitivity)."""
    templates = tmp_path / "templates"
    fake_registry = {
        "claude": ("claude_code",),
        "opencode": ("opencode",),
        "codex": ("codex",),
        "fourth": ("fourth_tmpl",),
    }
    for (dirname,) in fake_registry.values():
        (templates / dirname).mkdir(parents=True)
    monkeypatch.setattr(pm_import, "HARNESS_TEMPLATE_DIRS", fake_registry)

    assert pm_import.parse_harness_selection("all") == (
        "claude", "opencode", "codex", "fourth")
    assert [root.name for root in pm_import.resolve_template_roots(tmp_path, "all")] == [
        "claude_code", "opencode", "codex", "fourth_tmpl",
    ]


def test_unknown_or_empty_harness_selection_fails_loud(pm_import, tmp_path, capsys):
    with pytest.raises(ValueError, match="미지원 harness.*unknown"):
        pm_import.parse_harness_selection("claude,unknown")
    with pytest.raises(ValueError, match="빈 항목"):
        pm_import.parse_harness_selection("claude,")
    dest = tmp_path / "must-not-exist"
    with pytest.raises(SystemExit) as exc_info:
        pm_import.main(["--new", str(dest), "--harness", "claude,unknown"])
    assert exc_info.value.code == 2
    assert "미지원 harness: unknown" in capsys.readouterr().err
    assert not dest.exists()


def test_legacy_both_alias_removed_and_rejected_as_unknown(pm_import):
    """v1.6.0 예약 제거 이후 `both` 는 전용 경로 없이 일반 unknown 으로 거부된다."""
    assert pm_import.LEGACY_HARNESS_ALIASES == {}
    with pytest.raises(ValueError, match="미지원 harness: both"):
        pm_import.parse_harness_selection("both")


def test_legacy_alias_seam_expands_registered_alias(pm_import, monkeypatch):
    """빈 seam 에 alias 를 등록하면 파서 확장 분기가 정식 튜플로 전개한다(범용 기계 생존 가드)."""
    monkeypatch.setattr(
        pm_import, "LEGACY_HARNESS_ALIASES", {"pair": ("claude", "codex")})
    assert pm_import.parse_harness_selection("pair") == \
        pm_import.parse_harness_selection("claude,codex")
    monkeypatch.setattr(
        pm_import, "LEGACY_HARNESS_ALIASES", {"ghost": ("claude", "nope")})
    with pytest.raises(ValueError, match="미등록 하네스"):
        pm_import.parse_harness_selection("ghost")


def test_legacy_both_alias_deadline_is_self_enforcing(pm_import):
    """v1.6.0 bump가 alias 제거 누락을 즉시 red로 만들고, 제거 뒤에는 통과한다."""
    pm_import._enforce_legacy_harness_alias_deadline(
        "v1.5.9", {"both": ("claude", "opencode")}
    )
    with pytest.raises(RuntimeError, match="legacy harness alias를 제거"):
        pm_import._enforce_legacy_harness_alias_deadline(
            "v1.6.0", {"both": ("claude", "opencode")}
        )
    pm_import._enforce_legacy_harness_alias_deadline("v1.6.0", {})


def test_import_ships_only_tracked_template_files_and_keeps_tracked_gitignore(
        pm_import, tmp_path):
    """출하 e2e: 임의 미추적 파일은 목적지 0, ignore되지만 추적된 .gitignore는 정상 출하."""
    source = tmp_path / "source"
    template = source / "templates" / "opencode"
    shutil.copytree(REPO / "templates" / "opencode", template)
    _track_source_tree(source)

    injected_rel = Path(".opencode/arbitrary-machine-local.json")
    (template / injected_rel).write_text('{"machine": "local"}\n', encoding="utf-8")
    tracked_gitignore = template / ".opencode" / ".gitignore"
    assert tracked_gitignore.is_file()
    tracked = subprocess.run(
        ["git", "-C", str(source), "ls-files", "--error-unmatch",
         "templates/opencode/.opencode/.gitignore"],
        capture_output=True,
        text=True,
    )
    assert tracked.returncode == 0, tracked.stderr

    dest = tmp_path / "tracked-only-adopter"
    rc = pm_import.main([
        "--new", str(dest), "--from", str(source), "--harness", "opencode",
        "--name", "Tracked Only",
    ])

    assert rc == 0
    assert not (dest / injected_rel).exists(), "미추적 template 파일이 adopter로 출하됨"
    assert (dest / ".opencode" / ".gitignore").read_bytes() == tracked_gitignore.read_bytes()


@requires_symlink
def test_import_skips_tracked_template_symlink_without_copying_external_payload(
        pm_import, tmp_path):
    """추적 symlink는 외부 machine-local 대상을 따라 복사하지 않고 제외 경로를 경고한다."""
    source = tmp_path / "source"
    template = source / "templates" / "opencode"
    shutil.copytree(REPO / "templates" / "opencode", template)
    external_payload = tmp_path / "machine-local-secret.json"
    marker = b"T0499-R2-MACHINE-LOCAL-PAYLOAD-MUST-NOT-SHIP"
    external_payload.write_bytes(marker)
    injected_rel = Path(".opencode/machine-local-link.json")
    (template / injected_rel).symlink_to(external_payload)
    _track_source_tree(source)

    dest = tmp_path / "symlink-safe-adopter"
    with pytest.warns(
        pm_import.SkippedTemplateShippingEntryWarning,
        match=r"symlink\(index mode 120000.*\.opencode/machine-local-link\.json",
    ):
        rc = pm_import.main([
            "--new", str(dest), "--from", str(source), "--harness", "opencode",
            "--name", "Symlink Safe",
        ])

    assert rc == 0
    shipped_link = dest / injected_rel
    assert not shipped_link.exists()
    assert not shipped_link.is_symlink()
    payload_hits = _grep_token_files(dest, marker.decode("ascii"))
    assert payload_hits == [], f"외부 symlink 대상 내용이 adopter에 복사됨: {payload_hits}"


@pytest.mark.parametrize(
    ("index_mode", "warning_fragment"),
    [
        ("120000", "symlink"),
        ("160000", "gitlink"),
    ],
)
def test_iter_source_files_rejects_special_index_modes_even_if_materialized_regular(
        pm_import, tmp_path, monkeypatch, index_mode, warning_fragment):
    """working tree 형상이 일반 파일이어도 index mode 120000/160000이면 출하하지 않는다."""
    template = tmp_path / "template"
    template.mkdir()
    relative = Path(f"mode-{index_mode}")
    (template / relative).write_text("must not ship\n", encoding="utf-8")
    real_repo_files = pm_import._load_repo_owned_files()

    class StubRepoFiles:
        TRACKED_ONLY = real_repo_files.TRACKED_ONLY

        @staticmethod
        def list_repo_owned_entries(_checkout, _subtree, *, mode):
            assert mode == real_repo_files.TRACKED_ONLY
            return [real_repo_files.RepoOwnedEntry(relative, index_mode)]

    monkeypatch.setattr(pm_import, "_load_repo_owned_files", lambda: StubRepoFiles)

    with pytest.warns(
        pm_import.SkippedTemplateShippingEntryWarning,
        match=rf"{warning_fragment}.*mode-{index_mode}",
    ):
        got = list(pm_import._iter_source_files(template))

    assert got == []


def test_iter_source_files_non_git_fallback_is_loud(pm_import, tmp_path):
    """비-git 배포 사본은 복사를 계속하되 tracked 보장 소실을 공용 seam 경고로 표면화."""
    template = tmp_path / "unpacked-template"
    template.mkdir()
    (template / "kept.txt").write_text("kept\n", encoding="utf-8")
    repo_files = pm_import._load_repo_owned_files()

    with pytest.warns(repo_files.RepoFilesFallbackWarning, match="filesystem 전수 순회"):
        got = list(pm_import._iter_source_files(template))

    assert [(rel.as_posix(), path.name) for rel, path in got] == [
        ("kept.txt", "kept.txt"),
    ]


@requires_symlink
def test_iter_source_files_non_git_fallback_lstat_skips_symlink_loudly(
        pm_import, tmp_path):
    """mode 없는 filesystem 폴백도 lstat으로 symlink를 거르고 폴백·제외를 모두 알린다."""
    template = tmp_path / "unpacked-template"
    template.mkdir()
    external_payload = tmp_path / "outside.txt"
    external_payload.write_text("outside\n", encoding="utf-8")
    (template / "outside-link.txt").symlink_to(external_payload)
    repo_files = pm_import._load_repo_owned_files()

    with pytest.warns(Warning) as caught:
        got = list(pm_import._iter_source_files(template))

    assert got == []
    assert any(
        issubclass(item.category, repo_files.RepoFilesFallbackWarning)
        and "filesystem 전수 순회" in str(item.message)
        for item in caught
    )
    assert any(
        issubclass(item.category, pm_import.SkippedTemplateShippingEntryWarning)
        and "filesystem 폴백 symlink(lstat" in str(item.message)
        and "outside-link.txt" in str(item.message)
        for item in caught
    )


def test_iter_source_files_empty_tracked_inventory_fails_and_keeps_seam_warning(
        pm_import, tmp_path):
    """실제 빈 index는 소비점에서 실패하고 디스크 비교 seam warning도 그대로 남는다."""
    template = tmp_path / "empty-tracked-template"
    template.mkdir()
    (template / "untracked-local.txt").write_text("local\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(template), "init", "-q"], check=True)
    repo_files = pm_import._load_repo_owned_files()
    assert subprocess.run(
        ["git", "-C", str(template), "ls-files"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout == ""

    with pytest.warns(
        repo_files.RepoFilesEmptyWarning,
        match="tracked_only 열거가 빈 결과",
    ):
        with pytest.raises(
            pm_import.EmptyTemplateShippingInventoryError,
            match=r"pm-import 출하 인벤토리가 0건임.*subtree='\.'",
        ) as caught:
            list(pm_import._iter_source_files(template))

    assert str(template) in str(caught.value)
    assert "git index" in str(caught.value)


def test_iter_source_files_empty_disk_inventory_fails_without_seam_warning(
        pm_import, tmp_path):
    """디스크도 빈 실제 git subtree는 warning 0이어도 소비점의 명시 판정으로 실패한다."""
    source = tmp_path / "source"
    template = source / "templates" / "empty-disk-template"
    template.mkdir(parents=True)
    subprocess.run(["git", "-C", str(source), "init", "-q"], check=True)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(pm_import.EmptyTemplateShippingInventoryError) as error:
            list(pm_import._iter_source_files(template))

    assert caught == []
    assert error.value.filesystem_fallback is False
    assert "git index" in str(error.value)


def test_main_empty_non_git_template_uses_filesystem_diagnostic(
        pm_import, tmp_path, capsys):
    """비-git filesystem 강등의 빈 템플릿은 git index 대신 소스 디렉토리 원인을 안내한다."""
    source = tmp_path / "unpacked-source"
    (source / "templates" / "claude_code").mkdir(parents=True)
    dest = tmp_path / "dest"
    dest.mkdir()

    with pytest.warns(
            pm_import._load_repo_owned_files().RepoFilesFallbackWarning,
            match="filesystem 전수 순회"):
        rc = pm_import.main([
            "--into", str(dest), "--from", str(source), "--harness", "claude",
        ])

    err = capsys.readouterr().err
    assert rc == 1
    assert "filesystem 강등 상태" in err
    assert "소스 디렉토리가 비었는지" in err
    assert "checkout 루트" in err
    assert "git index" not in err


@pytest.mark.parametrize("dry_run", [False, True])
def test_main_empty_tracked_inventory_is_nonzero_before_copy(
        pm_import, tmp_path, capsys, dry_run):
    """--into 실행과 dry-run 모두 실제 빈 index 템플릿을 복사 전에 같은 진단으로 막는다."""
    source = tmp_path / f"source-{dry_run}"
    template = source / "templates" / "claude_code"
    template.mkdir(parents=True)
    (template / "engine.manifest").write_text(
        ".project_manager/tools/board.py\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "init", "-q"], check=True)
    assert subprocess.run(
        ["git", "-C", str(source), "ls-files"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout == ""
    dest = tmp_path / f"dest-{dry_run}"
    dest.mkdir()
    argv = [
        "--into", str(dest), "--from", str(source), "--harness", "claude",
    ]
    if dry_run:
        argv.append("--dry-run")

    with pytest.warns(
        pm_import._load_repo_owned_files().RepoFilesEmptyWarning,
        match="tracked_only 열거가 빈 결과",
    ):
        rc = pm_import.main(argv)

    captured = capsys.readouterr()
    assert rc == 1
    assert "pm-import 출하 인벤토리가 0건임" in captured.err
    assert str(template) in captured.err
    assert "subtree='.'" in captured.err
    assert "git index" in captured.err
    assert not (dest / ".project_manager").exists()


def test_tracked_source_inventory_still_applies_policy_exclusions(
        pm_import, tmp_path):
    """tracked-only 위에도 재설치물·stale bytecode·내부 README 정책 제외는 유지."""
    template = tmp_path / "tracked-template"
    (template / "node_modules").mkdir(parents=True)
    (template / "__pycache__").mkdir()
    (template / "node_modules" / "pkg.js").write_text("tracked dependency\n", encoding="utf-8")
    (template / "__pycache__" / "stale.pyc").write_bytes(b"tracked stale bytecode")
    (template / "README.md").write_text("framework-only docs\n", encoding="utf-8")
    (template / "kept.txt").write_text("ship me\n", encoding="utf-8")
    _track_source_tree(template)

    got = {
        rel.as_posix()
        for rel, _path in pm_import._iter_source_files(template)
    }

    assert got == {"kept.txt"}
    # `.git`은 git 출하 인벤토리에 들어올 수 없지만, fill의 명시 copied_relpaths 소비자는
    # 호출자가 준 범위를 다시 거른다. 공유 상수에서 빼면 VCS 메타를 텍스트 스캔하게 된다.
    explicit_scope = {Path(".git/config"), Path("kept.txt")}
    scanned = {
        rel.as_posix()
        for rel, _path in pm_import._iter_copied_files(template, explicit_scope)
    }
    assert scanned == {"kept.txt"}


# ── ②b --harness codex: 세 번째 하네스 스캐폴드 (ADR-0070 D5·T-0403) ───────────

def test_harness_maps_include_codex(pm_import):
    """registry에는 단일 하네스만 있고 조합은 parser가 집합으로 표현한다."""
    assert "codex" in pm_import.HARNESS_CHOICES
    assert pm_import.HARNESS_TEMPLATE_DIRS["codex"] == ("codex",)
    assert set(pm_import.HARNESS_TEMPLATE_DIRS) == {"claude", "opencode", "codex"}
    assert "all" in pm_import.HARNESS_CHOICES
    assert "both" not in pm_import.HARNESS_TEMPLATE_DIRS


def test_codex_new_creates_tree_and_inits(pm_import, tmp_path):
    """`--new --harness codex`: codex 어댑터(AGENTS.md·.codex/agents/*.toml·.agents/skills) +
    공유 엔진 + board init 산출. CLAUDE.md 는 없다(codex 는 공통 코어 AGENTS.md 를 native 로드)."""
    dest = tmp_path / "cxproj"
    rc = pm_import.main(["--new", str(dest), "--harness", "codex", "--name", "Codex Proj"])
    assert rc == 0
    # codex 어댑터 (dual namespace: .codex agents/config/hooks + .agents skills).
    assert (dest / "AGENTS.md").is_file()
    assert (dest / ".codex" / "agents" / "developer.toml").is_file()
    assert (dest / ".codex" / "rules" / "default.rules").is_file()
    assert (dest / ".agents" / "skills" / "pm-adr" / "SKILL.md").is_file()
    # 공통 코어 전략(D3 C-v2): codex 는 CLAUDE.md 를 두지 않는다.
    assert not (dest / "CLAUDE.md").exists()
    # 공유 엔진 + board init 산출.
    assert (dest / ".project_manager" / "tools" / "board.py").is_file()
    assert (dest / ".project_manager" / "local.conf").is_file()
    assert (dest / ".project_manager" / "wiki" / "pm_state.md").is_file()
    assert (dest / ".git").exists()


def test_codex_scaffold_no_unresolved_token_leak(pm_import, tmp_path):
    """codex 어댑터 트리(.codex/·.agents/·AGENTS.md)에 미해소 토큰 잔존 0 — **확장자 무관 전수
    스캔**(`rglob("*")`·`.toml` 포함·트리·방식 모두 정답이었다).

    검사 토큰을 opencode 모델 토큰 하나에서 **OPERATIONAL_TOKENS 전체**로 넓힌다(T-0429 ③): 옛
    형상은 `{{OPENCODE_PRO_MODEL}}` 만 봐서, v1.4.0 codex 가 실제로 출하한 `.codex/agents/*.toml` 의
    `{{PROJECT_NAME}}` 리터럴을 놓쳤다 — 이 한 줄(토큰 집합 확장)이면 그 원 결함이 red 였다.
      - OPENCODE_MODEL_TOKEN: codex 는 모델 해소 분기가 없다(D5·gpt-5.5 상속) — 토큰 자체가
        없어야 한다(있으면 harness-특수 분기 필요 신호·codex-고유 검사).
      - OPERATIONAL_TOKENS: 전부 채택자 값으로 치환됐어야 한다(미치환 = onboarding 깨짐)."""
    dest = tmp_path / "cxproj_notoken"
    rc = pm_import.main(["--new", str(dest), "--harness", "codex", "--name", "NoModel"])
    assert rc == 0
    check_tokens = (pm_import.OPENCODE_MODEL_TOKEN, *OPERATIONAL_TOKENS)
    for sub in (dest / ".codex", dest / ".agents", dest / "AGENTS.md"):
        paths = [sub] if sub.is_file() else sub.rglob("*")
        for p in paths:
            if not p.is_file():
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue  # 바이너리·읽기불가 = 치환 대상 아님(제외 사유 ④)
            leaked = [t for t in check_tokens if t in text]
            assert not leaked, f"codex 어댑터 {p.relative_to(dest).as_posix()} 에 미해소 토큰 {leaked}"


def test_codex_import_prints_trust_guidance(pm_import, tmp_path, capsys):
    """codex import 완료 출력에 loud 2단계 trust 안내(대화형 trust + hook trust + trust_level 미작동).

    (안내 고유 prose 로 단언 — 스캐폴드 copy 로그의 `.project_manager/hooks/` 파일 경로와 충돌 회피.)"""
    dest = tmp_path / "cxproj_trust"
    rc = pm_import.main(["--new", str(dest), "--harness", "codex", "--name", "Trust"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "2단계 trust 승인" in out              # loud 헤더
    assert "hook trust" in out                   # ② /hooks 승인 단계
    assert "trust_level" in out                  # -c override 안 먹음 명시(실측)
    assert "codex" in out.lower()
    # T-0818 값 단언 — circled 단계 마커(①·②)가 실 CLI stdout 에 없다(안내는 ASCII 번호 매김
    # `1)`·`2)` 로 표시돼 채택자에게 뜻이 선다).
    assert "①" not in out
    assert "②" not in out


def test_non_codex_import_no_trust_guidance(pm_import, tmp_path, capsys):
    """claude import 는 codex trust 안내를 내지 않는다(하네스 게이트 — 오출력 방지)."""
    dest = tmp_path / "claude_notrust"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "NoTrust"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "2단계 trust 승인" not in out
    assert "hook trust" not in out


# ── Claude permissions.allow trust 안내 (T-0556) ─────────────────────────────

def test_claude_import_prints_permissions_allow_trust_guidance(pm_import, tmp_path, capsys):
    """claude import 완료 출력은 trust 미승인 시 degrade와 수락 방법을 loud 안내한다."""
    dest = tmp_path / "claude_permissions_trust"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "Trust"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "아직 trust 승인하지 않았다면" in out
    assert "permissions.allow" in out
    assert "전역 설정에 의존" in out
    assert "Ignoring N permissions.allow entries" in out
    assert "첫 대화형 `claude` 세션에서 trust 다이얼로그를 수락하면 적용" in out


def test_claude_import_into_prints_conditional_trust_guidance(pm_import, tmp_path, capsys):
    """--into 대상은 이미 trusted일 수 있으므로 trust 상태를 단정하지 않는다."""
    dest = tmp_path / "claude_permissions_trust_into"
    dest.mkdir()
    rc = pm_import.main(["--into", str(dest), "--harness", "claude", "--name", "TrustInto"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "이 디렉토리를 아직 trust 승인하지 않았다면" in out
    assert "첫 대화형 `claude` 세션에서 trust 다이얼로그를 수락하면 적용" in out
    assert "이 프로젝트는 아직 untrusted" not in out


@pytest.mark.parametrize("harness", ["opencode", "codex"])
def test_non_claude_import_omits_permissions_allow_trust_guidance(
        pm_import, tmp_path, capsys, harness):
    """타 하네스 import에는 Claude settings.json trust 안내를 출력하지 않는다."""
    dest = tmp_path / f"{harness}_no_claude_permissions_trust"
    rc = pm_import.main(["--new", str(dest), "--harness", harness, "--name", "NoClaudeTrust"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Claude Code permissions.allow 적용 조건 안내" not in out
    assert "아직 untrusted" not in out


# ── ③ 자유서술 placeholder 3종 보존 (T-0009 몫) ──────────────────────────────

def test_free_form_placeholders_preserved(pm_import, tmp_path):
    dest = tmp_path / "freeform"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "FF"])
    assert rc == 0
    for token in FREE_FORM_TOKENS:
        hits = _grep_token_files(dest, token)
        assert hits, f"{token} 가 보존되지 않음 — T-0009 가 채워야 한다."


# ── ④ --into: 기존 파일 백업 + 원본 보존 ─────────────────────────────────────

def test_into_backs_up_existing_files(pm_import, tmp_path):
    # T-0034: 비-git tmp 디렉토리 → git_safe=None → 충돌 전부 중앙 디렉토리 백업.
    dest = tmp_path / "existing"
    dest.mkdir()
    # 기존 충돌 파일 (CLAUDE.md 는 claude 어댑터가 복사하는 파일).
    original = dest / "CLAUDE.md"
    sentinel = "## 기존 사용자 내용 — 보존되어야 함\n"
    original.write_text(sentinel, encoding="utf-8")

    rc = pm_import.main(["--into", str(dest), "--harness", "claude", "--name", "Existing"])
    assert rc == 0

    today = datetime.date.today().isoformat()
    # T-0034: 형제 *.backup.<DATE> 가 아니라 중앙 디렉토리에 relpath 미러링으로 백업된다.
    backup = dest / pm_import.BACKUP_DIR_NAME / today / "CLAUDE.md"
    assert backup.is_file(), "기존 충돌 파일이 중앙 디렉토리에 백업되지 않음."
    assert backup.read_text(encoding="utf-8") == sentinel, "백업이 원본 내용을 보존하지 않음."
    # 형제 백업(트리 전역 분산)은 더 이상 만들지 않는다.
    assert not list(dest.glob("CLAUDE.md.backup.*")), "형제 *.backup.<DATE> 가 잔존 — 중앙화 위반."
    # 새 CLAUDE.md 는 템플릿으로 덮였다 (원본 sentinel 아님).
    assert (dest / "CLAUDE.md").read_text(encoding="utf-8") != sentinel


def test_into_backup_central_dir_date_layout(pm_import, tmp_path):
    """T-0034: 백업이 `<dest>/.pm_import_backups/<DATE>/<relpath>` 중앙 레이아웃을 따른다."""
    dest = tmp_path / "datecheck"
    dest.mkdir()
    (dest / "CLAUDE.md").write_text("x\n", encoding="utf-8")
    rc = pm_import.main(["--into", str(dest), "--harness", "claude"])
    assert rc == 0
    today = datetime.date.today().isoformat()
    backup_root = dest / pm_import.BACKUP_DIR_NAME / today
    assert backup_root.is_dir(), "중앙 백업 디렉토리가 <DATE> 하위에 만들어지지 않음."
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", backup_root.name)
    backups = list(backup_root.glob("CLAUDE.md"))
    assert len(backups) == 1, f"CLAUDE.md 백업이 중앙 디렉토리에 정확히 1개여야 함: {backups}"


# ── ⑤ --dry-run: 파일시스템 미변경 ──────────────────────────────────────────

def test_dry_run_does_not_touch_fs(pm_import, tmp_path):
    dest = tmp_path / "dryrun"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--dry-run"])
    assert rc == 0
    assert not dest.exists(), "--dry-run 인데 대상 디렉토리가 생성됨."


def test_dry_run_into_does_not_modify(pm_import, tmp_path):
    dest = tmp_path / "dryinto"
    dest.mkdir()
    original = dest / "CLAUDE.md"
    original.write_text("keep me\n", encoding="utf-8")
    rc = pm_import.main(["--into", str(dest), "--harness", "claude", "--dry-run"])
    assert rc == 0
    # 원본 그대로 · 백업 없음 · 트리 복사 안 됨.
    assert original.read_text(encoding="utf-8") == "keep me\n"
    assert not list(dest.glob("*.backup.*"))
    assert not (dest / ".project_manager").exists()


# ── ⑥ idempotent: 재실행 안전 ───────────────────────────────────────────────

def test_idempotent_rerun_into(pm_import, tmp_path):
    """--into 재실행은 안전 — 2회차에 자기 자신을 백업하고 덮음, 트리 온전 유지."""
    dest = tmp_path / "rerun"
    rc1 = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "Re"])
    assert rc1 == 0
    board_before = (dest / ".project_manager" / "tools" / "board.py").read_text(encoding="utf-8")

    # 2회차 — 이미 채워진 트리에 --into 로 재실행.
    rc2 = pm_import.main(["--into", str(dest), "--harness", "claude", "--name", "Re"])
    assert rc2 == 0

    # 트리는 여전히 온전.
    assert (dest / ".project_manager" / "tools" / "board.py").is_file()
    assert (dest / "CLAUDE.md").is_file()
    assert (dest / ".project_manager" / "local.conf").is_file()
    # 엔진 파일은 재실행 후에도 동일 내용(결정적).
    board_after = (dest / ".project_manager" / "tools" / "board.py").read_text(encoding="utf-8")
    assert board_before == board_after


# ── 에러 처리: 잘못된 --from ─────────────────────────────────────────────────

def test_bad_source_returns_nonzero(pm_import, tmp_path):
    dest = tmp_path / "p"
    bad_source = tmp_path / "not-a-framework"
    bad_source.mkdir()
    rc = pm_import.main(["--new", str(dest), "--from", str(bad_source)])
    assert rc == 1
    assert not dest.exists()


# ── --weight lite: lite 진입 배치 (T-0010) ──────────────────────────────────
# lite 변종 고유 마커 / full 진입 고유 마커. lite 파일 1행은 "# X.md — ... lite 진입 ...".
LITE_MARKER = "lite 진입"
FULL_CLAUDE_MARKER = "자동 로드되는 진입점"        # full CLAUDE.md 만의 문구.
FULL_AGENTS_MARKER = "harness-neutral 공통 코어"  # full AGENTS.md 만의 문구(ADR-0069·T-0401 공통 코어 헤더).


def _lite_md_files(root: Path) -> list[Path]:
    """dst 트리에 남은 `*.lite.md` 파일 목록(node_modules 제외). lite/full 모두 0 이어야."""
    return [
        path.relative_to(root)
        for path in root.rglob("*.lite.md")
        if path.is_file()
        and not any(part == "node_modules" for part in path.relative_to(root).parts)
    ]


def test_weight_lite_claude_places_lite_entry(pm_import, tmp_path):
    """--weight lite (claude): CLAUDE.md = lite 변종 · CLAUDE.lite.md 부재 · full 마커 부재."""
    dest = tmp_path / "litec"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--weight", "lite",
                         "--name", "LC"])
    assert rc == 0
    claude_md = dest / "CLAUDE.md"
    assert claude_md.is_file()
    text = claude_md.read_text(encoding="utf-8")
    assert LITE_MARKER in text, "CLAUDE.md 가 lite 변종이 아님 (lite 마커 부재)."
    assert FULL_CLAUDE_MARKER not in text, "lite 배치인데 full CLAUDE.md 고유 내용이 들어감."
    # 원본 lite 이름은 dst 에 남으면 안 됨.
    assert not (dest / "CLAUDE.lite.md").exists()
    assert _lite_md_files(dest) == [], f"dst 에 *.lite.md 잔존: {_lite_md_files(dest)}"


def test_weight_lite_opencode_places_lite_entry(pm_import, tmp_path):
    """--weight lite (opencode): AGENTS.md = lite 변종 · AGENTS.lite.md 부재."""
    dest = tmp_path / "liteo"
    rc = pm_import.main(["--new", str(dest), "--harness", "opencode", "--weight", "lite",
                         "--name", "LO"])
    assert rc == 0
    agents_md = dest / "AGENTS.md"
    assert agents_md.is_file()
    text = agents_md.read_text(encoding="utf-8")
    assert LITE_MARKER in text, "AGENTS.md 가 lite 변종이 아님 (lite 마커 부재)."
    assert FULL_AGENTS_MARKER not in text, "lite 배치인데 full AGENTS.md 고유 내용이 들어감."
    assert not (dest / "AGENTS.lite.md").exists()
    assert _lite_md_files(dest) == [], f"dst 에 *.lite.md 잔존: {_lite_md_files(dest)}"


def test_weight_lite_combination_places_both_lite_entries(pm_import, tmp_path, capsys):
    """--weight lite 조합: CLAUDE.md·AGENTS.md 둘 다 lite · 어떤 *.lite.md 도 dst 부재."""
    dest = tmp_path / "liteb"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude,opencode", "--weight", "lite",
                         "--name", "LB"])
    assert rc == 0
    claude_text = (dest / "CLAUDE.md").read_text(encoding="utf-8")
    agents_text = (dest / "AGENTS.md").read_text(encoding="utf-8")
    assert LITE_MARKER in claude_text and FULL_CLAUDE_MARKER not in claude_text
    assert LITE_MARKER in agents_text and FULL_AGENTS_MARKER not in agents_text
    # 어떤 *.lite.md 도 dst 에 남지 않음.
    assert _lite_md_files(dest) == [], f"dst 에 *.lite.md 잔존: {_lite_md_files(dest)}"
    # 공유 엔진은 그대로 한 벌.
    assert (dest / ".project_manager" / "tools" / "board.py").is_file()
    # lite 모드 full X.md 제외 가드(c) 고정 — 진입 파일(CLAUDE.md·AGENTS.md)에 대한 스퓨리어스
    # 다중-tree 중복-relpath 충돌 경고가 없어야 한다. (가드 제거 시 lite 와 full 이 같은 dst X.md 로
    # 충돌해 "내용 불일치" 경고가 새어나온다.) .gitignore 같은 일반 충돌 경고는 lite 와
    # 무관한 기존 다중 선택 동작이므로 진입 파일명으로 한정해 검사한다.
    conflict_lines = [ln for ln in capsys.readouterr().err.splitlines()
                      if "중복 relpath 내용 불일치" in ln]
    assert not any("CLAUDE.md" in ln or "AGENTS.md" in ln for ln in conflict_lines), \
        f"진입 파일 충돌 경고 누출 — (c) 가드 회귀: {conflict_lines}"


def test_weight_full_excludes_lite_variants(pm_import, tmp_path):
    """--weight full(기본): CLAUDE.md = full 진입 · dst 에 *.lite.md 없음(lite 변종 제외)."""
    dest = tmp_path / "fullp"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude,opencode", "--weight", "full",
                         "--name", "FP"])
    assert rc == 0
    claude_text = (dest / "CLAUDE.md").read_text(encoding="utf-8")
    agents_text = (dest / "AGENTS.md").read_text(encoding="utf-8")
    # full 진입 = full 고유 마커 존재 · lite 마커 부재.
    assert FULL_CLAUDE_MARKER in claude_text and LITE_MARKER not in claude_text
    assert FULL_AGENTS_MARKER in agents_text and LITE_MARKER not in agents_text
    # full 모드도 lite 변종은 배포에 끼면 안 됨.
    assert _lite_md_files(dest) == [], f"full 배포에 *.lite.md 잔존: {_lite_md_files(dest)}"


def test_neutral_shared_entry_members_are_derived_from_adapter_registry(pm_import):
    """중립 진입문서 멤버는 이름 손-열거 없이 add-harness 선언의 root doc에서 파생한다."""
    for rel, (members, neutral_harness) in pm_import.NEUTRAL_SHARED_ENTRY_DOCS.items():
        expected = frozenset(
            harness
            for harness, (_dirs, root_doc) in pm_import.ADD_HARNESS_ADAPTER.items()
            if root_doc == rel.as_posix()
        )
        assert members == expected
        assert neutral_harness in members


def test_neutral_shared_entry_override_auto_includes_real_fourth_tree(tmp_path):
    """실제 fourth template tree+registry 등록만으로 AGENTS.md 중립 override에 자동 편입된다."""
    checkout = tmp_path / "checkout"
    tools = checkout / ".project_manager" / "tools"
    tools.mkdir(parents=True)
    source = (TOOLS / "pm_import.py").read_text(encoding="utf-8")

    template_registry_anchor = '    "codex": ("codex",),\n}'
    adapter_registry_anchor = '    "codex": ((".codex", ".agents"), "AGENTS.md"),\n}'
    assert template_registry_anchor in source
    assert adapter_registry_anchor in source
    source = source.replace(
        template_registry_anchor,
        '    "codex": ("codex",),\n    "fourth": ("fourth",),\n}',
        1,
    ).replace(
        adapter_registry_anchor,
        '    "codex": ((".codex", ".agents"), "AGENTS.md"),\n'
        '    "fourth": ((".fourth",), "AGENTS.md"),\n}',
        1,
    )
    (tools / "pm_import.py").write_text(source, encoding="utf-8")

    for dirname in ("opencode", "codex", "fourth"):
        tree = checkout / "templates" / dirname
        tree.mkdir(parents=True)
        (tree / "AGENTS.md").write_text(f"{dirname} entry\n", encoding="utf-8")

    spec = importlib.util.spec_from_file_location(
        "pm_import_with_fourth_shared_entry", tools / "pm_import.py"
    )
    fourth_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fourth_module)

    members, _neutral = fourth_module.NEUTRAL_SHARED_ENTRY_DOCS[Path("AGENTS.md")]
    assert members == frozenset({"opencode", "codex", "fourth"})
    overrides = fourth_module._selected_entry_doc_source_overrides(
        checkout, ("opencode", "codex", "fourth"), "full"
    )
    fourth_key = (checkout / "templates" / "fourth", Path("AGENTS.md"))
    assert overrides[fourth_key] == checkout / "templates" / "codex" / "AGENTS.md"


@pytest.mark.parametrize("neutral_lite_exists", (True, False))
def test_neutral_shared_entry_source_honors_lite_with_full_fallback(
        pm_import, tmp_path, neutral_lite_exists):
    """공존 lite는 중립 lite 변종을 우선하고, 변종이 없는 현행 트리는 full로 호환 폴백한다."""
    source = tmp_path / "source"
    for dirname in ("opencode", "codex"):
        shutil.copytree(REPO / "templates" / dirname, source / "templates" / dirname)
    lite_source = source / "templates" / "codex" / "AGENTS.lite.md"
    lite_marker = "T-0501-NEUTRAL-LITE-SOURCE"
    if neutral_lite_exists:
        lite_source.write_text(f"# {lite_marker}\n", encoding="utf-8")
    _track_source_tree(source)

    dest = tmp_path / ("neutral-lite" if neutral_lite_exists else "neutral-full-fallback")
    assert pm_import.main([
        "--new", str(dest), "--from", str(source),
        "--harness", "opencode,codex", "--weight", "lite", "--name", "Neutral",
    ]) == 0
    installed = (dest / "AGENTS.md").read_text(encoding="utf-8")
    if neutral_lite_exists:
        assert lite_marker in installed
        assert FULL_AGENTS_MARKER not in installed
    else:
        assert FULL_AGENTS_MARKER in installed
        assert lite_marker not in installed
    assert not (dest / "AGENTS.lite.md").exists()


_ALL_NONEMPTY_HARNESS_SELECTIONS = tuple(
    ",".join(selection)
    for size in range(1, len(HARNESSES) + 1)
    for selection in itertools.combinations(HARNESSES, size)
)


@pytest.mark.parametrize(
    ("selection", "expected_labels", "expected_prefixes"),
    (
        ("claude,codex", {"claude", "codex"}, {"/", "$"}),
        ("codex,opencode", {"codex", "opencode"}, {"/", "$"}),
        ("all", {"claude", "codex", "opencode"}, {"/", "$"}),
    ),
)
def test_multi_harness_shared_wiki_lists_only_installed_native_entries(
        pm_import, tmp_path, selection, expected_labels, expected_prefixes):
    dest = tmp_path / f"shared-{selection.replace(',', '-')}"
    assert pm_import.main([
        "--new", str(dest), "--harness", selection, "--name", "Shared Entry",
    ]) == 0
    role = (dest / ".project_manager" / "wiki" / "pm_role.md").read_text(
        encoding="utf-8"
    )
    annotated = re.findall(
        r"`(?P<prefix>[/\$])pm-[^`]+`\((?P<labels>claude|codex|opencode|"
        r"claude·opencode|opencode·claude)\)",
        role,
    )
    assert annotated, "공유 wiki에 다중 하네스 진입 표기 병기가 없다"
    prefixes = {prefix for prefix, _labels in annotated}
    labels = {
        label
        for _prefix, grouped in annotated
        for label in grouped.split("·")
    }
    assert prefixes == expected_prefixes
    assert labels == expected_labels

    # 같은 자동-load root doc을 공유하는 조합도 침묵 first-wins가 아니라 같은 설치 집합을 밝힌다.
    if {"codex", "opencode"} <= expected_labels:
        agents = (dest / "AGENTS.md").read_text(encoding="utf-8")
        assert "(codex)" in agents and "(opencode)" in agents


@pytest.mark.parametrize("weight", ("full", "lite"))
@pytest.mark.parametrize("selection", _ALL_NONEMPTY_HARNESS_SELECTIONS)
def test_entry_document_execution_models_are_isolated_for_every_selection_and_weight(
        pm_import, tmp_path, selection, weight):
    """단일/2조합/3조합 × full/lite 설치 결과에서 자동-load 진입문서 오염이 없다.

    opencode+codex는 AGENTS.md를 함께 자동 로드하므로 공존 시 중립 코어만 허용한다. 각자의
    실행 모델은 namespace별 지침에 남아 있어야 한다. claude는 별도 CLAUDE.md를 자동 로드하므로
    opencode 단독 lite의 기존 자족형 AGENTS.md와 공존해도 교차 로드되지 않는다.
    """
    dest = tmp_path / f"entry-{selection.replace(',', '-')}-{weight}"
    assert pm_import.main([
        "--new", str(dest), "--harness", selection, "--weight", weight,
        "--name", "Entry Isolation",
    ]) == 0
    selected = set(pm_import.parse_harness_selection(selection))

    if "claude" in selected:
        assert (dest / "CLAUDE.md").is_file()
    if selected & {"opencode", "codex"}:
        agents_text = (dest / "AGENTS.md").read_text(encoding="utf-8")
        if {"opencode", "codex"} <= selected:
            assert FULL_AGENTS_MARKER in agents_text
            assert "## 0. opencode 실행 모델" not in agents_text
            assert ".opencode/agents/*.md" not in agents_text
            assert "네이티브 `task` tool" not in agents_text
            assert "spawn_agent" not in agents_text
        elif "codex" in selected:
            assert FULL_AGENTS_MARKER in agents_text
            assert "네이티브 `task` tool" not in agents_text
        elif "opencode" in selected and weight == "lite":
            assert "## 0. opencode 실행 모델" in agents_text

    if "opencode" in selected:
        opencode_instructions = (
            dest / ".opencode" / "pm-instructions.md").read_text(encoding="utf-8")
        assert "네이티브 `task` tool" in opencode_instructions
        assert ".opencode/agents/*.md" in opencode_instructions
    if "codex" in selected:
        codex_instructions = (
            dest / ".agents" / "skills" / "pm-dev-delegate" / "SKILL.md"
        ).read_text(encoding="utf-8")
        assert "spawn_agent" in codex_instructions


def test_weight_default_is_full_no_lite_variants(pm_import, tmp_path):
    """--weight 미지정(기본 full): claude 단독에서도 CLAUDE.lite.md 가 dst 에 안 깔린다."""
    dest = tmp_path / "defp"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "DP"])
    assert rc == 0
    assert not (dest / "CLAUDE.lite.md").exists()
    text = (dest / "CLAUDE.md").read_text(encoding="utf-8")
    assert FULL_CLAUDE_MARKER in text and LITE_MARKER not in text


def test_weight_lite_substitutes_operational_placeholders(pm_import, tmp_path):
    """lite 배치된 CLAUDE.md 안에 operational placeholder 잔여 0 — 자유서술만 보존.

    lite 파일이 dst CLAUDE.md 로 rename 복사돼도 copied_relpaths(=dst relpath)에 잡혀
    placeholder 치환이 정상 동작하는지(정합성) 확인. {{PY}}·{{TEST_CMD}} 등 operational
    토큰은 치환되고, 자유서술({{PROJECT_*}})·자유서술 3종은 보존된다.
    """
    dest = tmp_path / "litesub"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--weight", "lite",
                         "--name", "Lite Sub"])
    assert rc == 0
    text = (dest / "CLAUDE.md").read_text(encoding="utf-8")
    # operational 토큰은 lite CLAUDE.md 에서 치환됨(엔진 문서 아님 — 치환 대상).
    for token in OPERATIONAL_TOKENS:
        assert token not in text, f"lite CLAUDE.md 에 operational {token} 잔존(치환 안 됨)."
    # --name 값이 반영됨(치환 정합 증거).
    assert "Lite Sub" in text


def test_weight_lite_dry_run_shows_rename(pm_import, tmp_path, capsys):
    """lite --dry-run 출력에 'CLAUDE.lite.md → CLAUDE.md (lite)' rename 가 보인다 · 파일 미변경."""
    dest = tmp_path / "litedry"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--weight", "lite",
                         "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "CLAUDE.lite.md" in out and "CLAUDE.md" in out and "lite" in out
    # dry-run = 파일시스템 미변경.
    assert not dest.exists()


# ── MF1: --into 치환은 복사한 파일만 — 안 복사한 사용자 파일 불가침 ────────────

def test_into_does_not_substitute_untouched_user_files(pm_import, tmp_path):
    """import 가 복사하지 않는 사용자 파일에 operational 토큰이 있어도 치환·백업되지 않는다.

    MF1: substitute_placeholders 가 dest 트리 전체를 rglob 하면 비파괴 계약을 위반한다 —
    이번 run 이 복사한 파일로만 범위를 한정해야 한다.
    """
    dest = tmp_path / "withuser"
    dest.mkdir()
    # import 가 복사하지 않는 경로 + operational 토큰 텍스트 포함.
    user_src_dir = dest / "src"
    user_src_dir.mkdir()
    user_file = user_src_dir / "app.py"
    user_content = "# project: {{PROJECT_NAME}} root={{PROJECT_ROOT}} py={{PY}}\n"
    user_file.write_text(user_content, encoding="utf-8")

    rc = pm_import.main(["--into", str(dest), "--harness", "claude", "--name", "Untouched"])
    assert rc == 0

    # 사용자 파일은 글자 하나 안 바뀜 — 토큰 치환 0, 백업 0.
    assert user_file.read_text(encoding="utf-8") == user_content, \
        "안 복사한 사용자 파일의 operational 토큰이 치환됨 — MF1 위반."
    assert not list(user_src_dir.glob("*.backup.*")), \
        "안 복사한 사용자 파일이 백업됨 — 건드리면 안 됨."


# ── T-0218: 빈값 subs silent-empty 가드 (substitute → render 관통·codex must-fix) ──

def _seed_render_managed(dest_root: Path, rel_str: str, body: str) -> None:
    """@render manifest(.claude/agents) + 복사된 산출물 파일 — substitute→render 관통 대상 트리."""
    pm = dest_root / ".project_manager"
    pm.mkdir(parents=True, exist_ok=True)
    (pm / "engine.manifest").write_text(".claude/agents @render\n", encoding="utf-8")
    f = dest_root / rel_str
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body, encoding="utf-8")


def test_substitute_skips_empty_value_keeps_token(pm_import, tmp_path):
    """빈값 subs 는 치환하지 않고 토큰을 남긴다(silent 비움 금지)·정상값은 치환(T-0218 unit).

    `replace(token, "")` 로 토큰을 조용히 비우면 미해소 탐지 신호가 사라진다 — 빈값은 skip 해
    토큰을 남기고(이후 render 가 leak 으로 잡음), 정상값만 치환한다.
    """
    dest = tmp_path / "empty"
    rel = Path("CLAUDE.md")
    f = dest / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("name={{PROJECT_NAME}} py={{PY}}\n", encoding="utf-8")
    subs = {"{{PROJECT_NAME}}": "", "{{PY}}": "python3"}  # PROJECT_NAME 빈값·PY 정상값
    pm_import.substitute_placeholders(dest, subs, {rel})
    out = f.read_text(encoding="utf-8")
    assert out == "name={{PROJECT_NAME}} py=python3\n"  # 빈값 토큰 잔존·정상값만 치환


def test_import_order_empty_name_leaks_at_render_not_silently_emptied(pm_import, tmp_path):
    """최초 import 관통(substitute→render_managed_files)에서 빈 project_name → RenderLeakError+힌트.

    codex must-fix(T-0218): substitute_placeholders 가 render *이전* 에 돌아 빈값 subs 를
    `replace(token, "")` 로 지우면 `{{PROJECT_NAME}}` 이 render 가드 도달 전에 사라진다(최초 import
    사각). 이제 빈값은 substitute 에서 skip → 토큰 잔존 → render_managed_files 의 _assert_no_leak
    가 leak + 빈값 힌트로 표면화한다. 실제 import 호출 순서를 그대로 거쳐 회귀를 못박는다.
    """
    dest = tmp_path / "adopter"
    rel_str = ".claude/agents/developer.md"
    _seed_render_managed(dest, rel_str, "description: {{PROJECT_NAME}} 프로젝트\n")
    copied = {Path(rel_str)}
    # 실제 import 순서의 subs: PROJECT_NAME 빈값 (다른 operational 토큰은 정상값).
    subs = {"{{PROJECT_NAME}}": "", "{{PY}}": "python3", "{{DATE}}": "2026-07-03"}
    # 1) substitute_placeholders — 빈값 skip → 토큰 잔존(파일 미오염·silent 비움 없음).
    pm_import.substitute_placeholders(dest, subs, copied)
    assert (dest / rel_str).read_text(encoding="utf-8") == \
        "description: {{PROJECT_NAME}} 프로젝트\n"
    # 2) render_managed_files — 잔존 토큰 → RenderLeakError + 빈값 힌트. RenderLeakError 는
    #    RuntimeError 서브클래스 — pm_import 가 pm_render 를 격리 로드하므로 base+이름으로 잡는다.
    with pytest.raises(RuntimeError) as exc:
        pm_import.render_managed_files(dest, subs, copied)
    assert type(exc.value).__name__ == "RenderLeakError"
    msg = str(exc.value)
    assert "{{PROJECT_NAME}}" in msg
    assert "`project_name=`" in msg
    assert "빈값" in msg
    # render 실패 전이라 파일은 여전히 토큰 보존(silent 로 " 프로젝트" 안 기록됨).
    assert (dest / rel_str).read_text(encoding="utf-8") == \
        "description: {{PROJECT_NAME}} 프로젝트\n"


def test_import_order_normal_name_renders_clean(pm_import, tmp_path):
    """정상 project_name → substitute 가 치환·render 는 leak 0(정상 import 회귀 무변경·T-0218)."""
    dest = tmp_path / "adopter_ok"
    rel_str = ".claude/agents/developer.md"
    _seed_render_managed(dest, rel_str, "description: {{PROJECT_NAME}} 프로젝트\n")
    copied = {Path(rel_str)}
    subs = {"{{PROJECT_NAME}}": "Acme", "{{PY}}": "python3", "{{DATE}}": "2026-07-03"}
    # substitute 가 토큰을 리터럴로 치환(정상값).
    n = pm_import.substitute_placeholders(dest, subs, copied)
    assert n == 1
    assert (dest / rel_str).read_text(encoding="utf-8") == "description: Acme 프로젝트\n"
    # render_managed_files — 잔존 토큰 0 → 변경 0(leak 없음·idempotent).
    n_render = pm_import.render_managed_files(dest, subs, copied)
    assert n_render == 0
    assert "{{" not in (dest / rel_str).read_text(encoding="utf-8")


# ── MF2: --new 비어있지 않은 디렉토리 거부 (데이터 손실 가드) ──────────────────

def test_new_rejects_non_empty_dir(pm_import, tmp_path):
    """--new 가 기존 파일 든 디렉토리를 가리키면 비0 종료 · 기존 파일 불변 · 트리 미생성."""
    dest = tmp_path / "occupied"
    dest.mkdir()
    existing = dest / "important.txt"
    existing.write_text("user data\n", encoding="utf-8")

    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "Occ"])
    assert rc != 0, "--new 비어있지 않은 디렉토리인데 성공 반환 — MF2 위반."
    # 기존 파일 불변 · 백업 안 만들어짐 · 트리 미생성.
    assert existing.read_text(encoding="utf-8") == "user data\n"
    assert not list(dest.glob("*.backup.*"))
    assert not (dest / ".project_manager").exists()
    assert not (dest / "CLAUDE.md").exists()


def test_new_rejects_non_empty_dir_in_dry_run(pm_import, tmp_path):
    """dry-run 에서도 동일하게 비어있지 않은 --new 를 거부한다(계획 전 게이트)."""
    dest = tmp_path / "occupied_dry"
    dest.mkdir()
    (dest / "x.txt").write_text("x\n", encoding="utf-8")
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--dry-run"])
    assert rc != 0


def test_new_allows_empty_existing_dir(pm_import, tmp_path):
    """비어있는 기존 디렉토리는 --new 정상 진행(가드는 '비어있지 않을 때'만)."""
    dest = tmp_path / "emptydir"
    dest.mkdir()  # 존재하지만 비어있음.
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "E"])
    assert rc == 0
    assert (dest / "CLAUDE.md").is_file()


# ── MF2(codex 4차): --into 미존재 경로 거부 (기존 프로젝트 전용 가드) ──────────

def test_into_rejects_nonexistent_path(pm_import, tmp_path):
    """--into 가 존재하지 않는 경로면 비0 종료 · 디렉토리 미생성.

    codex 4차 MF2: --into 는 기존 프로젝트 가정이다. 미존재 경로면 복사가 디렉토리를 새로
    만들고 git init 없이 board.py init 이 성공해 pre-push 훅 없는 불완전 import 가 "완료"된다.
    --new 가드와 대칭으로 plan/dry-run 이전에 거부해야 한다.
    """
    dest = tmp_path / "does-not-exist"
    rc = pm_import.main(["--into", str(dest), "--harness", "claude", "--name", "Nope"])
    assert rc != 0, "--into 미존재 경로인데 성공 반환 — MF2 위반."
    assert not dest.exists(), "--into 미존재 경로가 거부됐는데 디렉토리가 생성됨."


def test_into_rejects_nonexistent_path_in_dry_run(pm_import, tmp_path):
    """dry-run 에서도 --into 미존재 경로를 거부한다(계획 전 게이트 — --new 가드와 대칭)."""
    dest = tmp_path / "does-not-exist-dry"
    rc = pm_import.main(["--into", str(dest), "--harness", "claude", "--dry-run"])
    assert rc != 0
    assert not dest.exists()


def test_into_rejects_file_path(pm_import, tmp_path):
    """--into 가 디렉토리가 아닌 *파일* 경로면 비0 종료(기존 디렉토리만 허용)."""
    dest = tmp_path / "a-file"
    dest.write_text("i am a file\n", encoding="utf-8")
    rc = pm_import.main(["--into", str(dest), "--harness", "claude", "--name", "F"])
    assert rc != 0, "--into 파일 경로인데 성공 반환 — MF2 위반."
    # 기존 파일 불변.
    assert dest.read_text(encoding="utf-8") == "i am a file\n"


def test_into_existing_dir_still_works(pm_import, tmp_path):
    """정상: --into 기존(빈) 디렉토리는 가드를 통과해 import 가 완주한다."""
    dest = tmp_path / "existing-empty"
    dest.mkdir()
    rc = pm_import.main(["--into", str(dest), "--harness", "claude", "--name", "OK"])
    assert rc == 0
    assert (dest / "CLAUDE.md").is_file()
    assert (dest / ".project_manager" / "local.conf").is_file()


# ── MF3: 다중-tree 중복 relpath 내용 불일치 — 경고 + registry 우선 ───────────

def test_combination_conflicting_relpath_warns_and_prefers_registry_first(
        pm_import, tmp_path, capsys):
    """일반 중복은 registry 우선 경고, manifest는 거짓 경고 없이 flavor 합집합.

    우선순위 근거는 ``claude_code가 상위집합``이 아니라 registry의 결정적 정규 순서다.
    """
    dest = tmp_path / "bothconflict"
    rc = pm_import.main([
        "--new", str(dest), "--harness", "opencode,claude", "--name", "BC",
    ])
    assert rc == 0

    captured = capsys.readouterr()
    assert "'.project_manager/engine.manifest' 는" not in captured.err, \
        "최종 합집합으로 교체되는 manifest에 첫-tree 우선이라는 거짓 경고가 남음."
    assert "선택 manifest 중복 경로의 marker/@source 불일치" not in captured.err, \
        "실제 flavor의 정상 합집합에서 marker 충돌 경고가 부풀려짐."
    normalized_relpaths = {
        ".gitignore",
        ".project_manager/wiki/tickets/README.md",
        ".project_manager/wiki/tickets/blocked/.gitkeep",
        ".project_manager/wiki/tickets/claimed/.gitkeep",
        ".project_manager/wiki/tickets/done/.gitkeep",
        ".project_manager/wiki/tickets/open/.gitkeep",
    }
    assert not any(f"'{relpath}'" in captured.err for relpath in normalized_relpaths), \
        "정규화한 템플릿 사본에서 첫 트리 충돌 경고가 남음."

    # 경고 채널 자체는 유지한다. 인위 충돌은 여전히 registry 첫 트리 우선 경고여야 한다.
    first = tmp_path / "first"
    second = tmp_path / "second"
    for root, payload in ((first, b"first\n"), (second, b"second\n")):
        target = root / "shared.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    pm_import.plan_copy([first, second], tmp_path / "planned", None)
    warning = capsys.readouterr().err
    assert "'shared.txt'" in warning
    assert "registry 정규 순서상 첫 트리 'first'" in warning

    # engine.manifest 자체의 self-prop 마커 충돌은 첫 트리(claude_code)가 이긴다.
    entries = pm_import._pm_update_read_manifest(
        dest / ".project_manager" / "engine.manifest")
    self_entry = [e for e in entries if str(e) == ".project_manager/engine.manifest"]
    assert len(self_entry) == 1
    assert self_entry[0].source_rel == \
        "templates/claude_code/.project_manager/engine.manifest"


def test_combination_install_manifest_is_selected_tree_union(pm_import, pm_update, tmp_path):
    """조합 설치 manifest가 각 flavor 선언을 전부 포함하고 opencode 관리 파일까지 plan한다.

    합집합 구현을 되돌려 claude manifest만 설치하면 opencode 선언 subset 단언에서 red가 된다.
    """
    dest = tmp_path / "both-union"
    assert pm_import.main([
        "--new", str(dest), "--harness", "claude,opencode", "--name", "Union",
    ]) == 0

    dest_manifest = dest / ".project_manager" / "engine.manifest"
    installed = pm_update.read_manifest(dest_manifest)
    installed_paths = {str(e) for e in installed}
    expected_paths: set[str] = set()
    for flavor in ("claude_code", "opencode"):
        source_entries = pm_update.read_manifest(
            REPO / "templates" / flavor / ".project_manager" / "engine.manifest")
        expected_paths.update(str(e) for e in source_entries)
        assert {str(e) for e in source_entries} <= installed_paths, \
            f"{flavor} manifest 선언이 조합 설치 합집합에서 누락"
    assert installed_paths == expected_paths, \
        "설치 manifest 수치/내용이 선택 flavor 합집합과 정확히 일치하지 않음"

    # 디렉터리 선언을 실제 파일로 펼친 관리 plan에도 opencode 산출물이 실린다.
    empty_dest = tmp_path / "empty-plan"
    changes, missing = pm_update.plan(REPO, installed, dest_root=empty_dest)
    opencode_files = {
        str(rel).replace("\\", "/")
        for rel, _src, _dst, _kind in changes
        if str(rel).replace("\\", "/").startswith(".opencode/")
    }
    assert not missing
    assert {
        ".opencode/agents/developer.md",
        ".opencode/lib/safe-write-core.cjs",
        ".opencode/plugins/safe-write.js",
        ".opencode/pm-instructions.md",
        ".opencode/pm_orch_opencode.py",
        ".opencode/.gitignore",
    } <= opencode_files


def test_selected_manifest_union_disjoint_paths_are_warning_silent(
        pm_import, pm_update, tmp_path, capsys):
    """서로 다른 관리 경로의 정상 합집합은 marker 충돌 경고를 1줄도 만들지 않는다."""
    first = tmp_path / "templates" / "first" / ".project_manager" / "engine.manifest"
    second = tmp_path / "templates" / "second" / ".project_manager" / "engine.manifest"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_text(".first/a    @render\n", encoding="utf-8")
    second.write_text(".second/b    @source=templates/second/.second/b\n", encoding="utf-8")

    merged = pm_update.merge_manifest_sources([first, second])
    assert merged["conflicts"] == []
    pm_import._warn_selected_manifest_union_conflicts(merged)
    assert capsys.readouterr().err == ""


def test_selected_manifest_union_real_marker_conflict_warns(
        pm_import, pm_update, tmp_path, capsys, monkeypatch):
    """같은 관리 경로의 marker 충돌을 main이 소비해 경로·우선순위와 함께 경고한다."""
    first = tmp_path / "templates" / "first" / ".project_manager" / "engine.manifest"
    second = tmp_path / "templates" / "second" / ".project_manager" / "engine.manifest"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_text(".shared/x    @render\n", encoding="utf-8")
    second.write_text(
        ".shared/x    @source=templates/second/.shared/x\n", encoding="utf-8")

    merged = pm_update.merge_manifest_sources([first, second])
    assert merged["conflicts"] == [".shared/x"]
    pm_import._warn_selected_manifest_union_conflicts(merged)
    err = capsys.readouterr().err
    assert "marker/@source 불일치" in err
    assert "registry 정규 순서상 첫 flavor" in err
    assert ".shared/x" in err

    # CLI 연결 회귀: prepare 결과의 conflicts가 helper에만 머물지 않고 main 경로에서 소비된다.
    monkeypatch.setattr(
        pm_import, "_prepare_selected_manifest_union", lambda _roots: merged)
    dest = tmp_path / "main-conflict-preview"
    assert pm_import.main([
        "--new", str(dest), "--harness", "claude,opencode", "--dry-run",
        "--name", "Conflict Preview",
    ]) == 0
    cli_err = capsys.readouterr().err
    assert "marker/@source 불일치" in cli_err
    assert ".shared/x" in cli_err


def test_combination_missing_second_manifest_fails_before_any_copy(
        pm_import, tmp_path, capsys):
    """조합의 후순위 manifest 부재는 apply 전에 loud 실패하고 부분 설치를 남기지 않는다."""
    source = tmp_path / "source"
    claude = source / "templates" / "claude_code"
    opencode = source / "templates" / "opencode"
    (claude / ".project_manager").mkdir(parents=True)
    opencode.mkdir(parents=True)
    (claude / ".project_manager" / "engine.manifest").write_text(
        ".project_manager/engine.manifest\n", encoding="utf-8")
    (claude / "CLAUDE.md").write_text("would copy\n", encoding="utf-8")
    (opencode / "AGENTS.md").write_text("would also copy\n", encoding="utf-8")
    # opencode/.project_manager/engine.manifest는 의도적으로 부재.
    dest = tmp_path / "partial-install-must-not-exist"

    rc = pm_import.main([
        "--new", str(dest), "--from", str(source), "--harness", "claude,opencode",
        "--name", "No Partial",
    ])

    captured = capsys.readouterr()
    assert rc != 0
    assert "manifest 합집합" in captured.err and "복사 전에 중단" in captured.err
    assert not dest.exists(), "후순위 manifest 부재인데 복사/git init 등 부분 설치가 남음"


def test_combination_install_then_update_repairs_stale_opencode_in_one_run(
        pm_import, pm_update, tmp_path, monkeypatch):
    """조합 신규 설치 후 opencode adapter가 낡아져도 pm_update 1회로 복구된다."""
    dest = tmp_path / "both-update"
    assert pm_import.main([
        "--new", str(dest), "--harness", "claude,opencode", "--name", "Combo Update",
    ]) == 0
    victim_rel = Path(".opencode/plugins/safe-write.js")
    victim = dest / victim_rel
    expected = (REPO / "templates" / "opencode" / victim_rel).read_bytes()
    victim.write_text("// stale adopter copy\n", encoding="utf-8")

    monkeypatch.setattr(pm_update, "REPO", dest)
    monkeypatch.setenv("PM_NONINTERACTIVE", "1")
    assert pm_update.main(["--from", str(REPO)]) == 0
    assert victim.read_bytes() == expected, \
        "조합 설치의 opencode adapter가 pm_update 1회로 복구되지 않음"


@pytest.mark.parametrize(
    ("selection", "victim_rels"),
    [
        (
            "claude,codex",
            (Path(".claude/ctx_guard.py"), Path(".codex/agents/developer.toml")),
        ),
        (
            "all",
            (
                Path(".claude/ctx_guard.py"),
                Path(".opencode/plugins/safe-write.js"),
                Path(".codex/agents/developer.toml"),
            ),
        ),
    ],
)
def test_arbitrary_selection_manifest_union_survives_pm_update(
        pm_import, pm_update, tmp_path, monkeypatch, selection, victim_rels):
    """claude+codex와 all이 각 flavor 전체를 manifest에 싣고 한 번의 update로 갱신된다."""
    dest = tmp_path / f"selection-{selection.replace(',', '-') }"
    assert pm_import.main([
        "--new", str(dest), "--harness", selection, "--name", "Set Selection",
    ]) == 0

    installed_paths = {
        str(entry)
        for entry in pm_update.read_manifest(
            dest / ".project_manager" / "engine.manifest")
    }
    selected = pm_import.parse_harness_selection(selection)
    for harness in selected:
        (flavor,) = pm_import.HARNESS_TEMPLATE_DIRS[harness]
        source_paths = {
            str(entry)
            for entry in pm_update.read_manifest(
                REPO / "templates" / flavor / ".project_manager" / "engine.manifest")
        }
        assert source_paths <= installed_paths, \
            f"{selection}: {flavor}의 manifest 고유 경로가 선택 합집합에서 누락"

    expected: dict[Path, bytes] = {}
    for rel in victim_rels:
        # @render 파일은 source token-form이 아니라 채택자 local.conf로 렌더된 byte가 정답이다.
        # 같은 conf의 pm_update 뒤에는 import 직후 byte로 되돌아와야 한다.
        expected[rel] = (dest / rel).read_bytes()
        # 손상은 **내용만** 바꾼다. 플랫폼 기본 텍스트 쓰기는 Windows에서 표기까지 CRLF로 바꾸고,
        # 엔진은 그 dest 표기를 충실히 보존하므로(T-0709) 되돌아온 byte가 표기만 달라 "갱신
        # 실패"로 보였다(T-0724 실측). 갱신 축을 표기 축과 섞지 않는다.
        write_matching_newlines(dest / rel, f"stale {rel}\n", like=expected[rel])
        stale = (dest / rel).read_bytes()
        assert stale != expected[rel], f"픽스처가 {rel} 손상을 만들지 못했다"
        assert dominant_newline_bytes(stale) == dominant_newline_bytes(expected[rel]), \
            f"픽스처가 {rel}의 개행 표기까지 바꿨다 — 손상 축이 표기 축과 섞인다"

    monkeypatch.setattr(pm_update, "REPO", dest)
    monkeypatch.setenv("PM_NONINTERACTIVE", "1")
    assert pm_update.main(["--from", str(REPO)]) == 0
    for rel, expected_bytes in expected.items():
        assert (dest / rel).read_bytes() == expected_bytes, \
            f"{selection}: {rel}가 pm_update 1회로 갱신되지 않음"


@pytest.mark.parametrize(
    "base_harness",
    ["claude", "opencode"],
)
def test_legacy_single_harness_without_stray_has_no_cross_flavor_warning(
        pm_import, pm_update, tmp_path, monkeypatch, capsys, base_harness):
    """순정 legacy 단일-harness는 자기 core 경로를 타 flavor evidence로 세지 않아 조용히 sync한다."""
    dest = tmp_path / f"legacy-clean-{base_harness}"
    assert pm_import.main([
        "--new", str(dest), "--harness", base_harness, "--name", "Legacy Clean",
    ]) == 0
    capsys.readouterr()
    manifest = dest / ".project_manager" / "engine.manifest"
    manifest.write_text(
        re.sub(r"\s+@source=\S+", "", manifest.read_text(encoding="utf-8")),
        encoding="utf-8",
    )

    monkeypatch.setattr(pm_update, "REPO", dest)
    monkeypatch.setenv("PM_NONINTERACTIVE", "1")
    assert pm_update.main(["--from", str(REPO)]) == 0

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "미등재 flavor 파일 관측" not in combined
    assert "타 flavor(" not in combined
    assert "./pm-config.sh add-harness" not in combined


@pytest.mark.parametrize(
    ("base_harness", "stray_flavor", "stray_rel"),
    [
        ("claude", "opencode", ".opencode/agents/developer.md"),
        ("opencode", "claude_code", ".claude/agents/developer.md"),
        ("claude", "codex", ".codex/agents/developer-hard.toml"),
    ],
)
def test_legacy_single_harness_partial_stray_never_promotes_adapter(
        pm_import, pm_update, tmp_path, monkeypatch, capsys,
        base_harness, stray_flavor, stray_rel):
    """legacy 단일 flavor + 타 flavor stray 1개는 loud 진단만 하고 승격/overwrite하지 않는다.

    `all(...)`을 `any(...)`로 바꾸는 mutant는 upstream_manifests에 stray flavor를 넣고 실제 sync가
    사용자 바이트를 덮으므로 이 matrix에서 red가 된다.
    """
    dest = tmp_path / f"{base_harness}-with-user-{stray_flavor}"
    assert pm_import.main([
        "--new", str(dest), "--harness", base_harness, "--name", "Legacy Single",
    ]) == 0
    capsys.readouterr()

    # 실제 legacy 형상: 선택 provenance가 전무한 구 manifest.
    dest_manifest = dest / ".project_manager" / "engine.manifest"
    legacy_text = re.sub(
        r"\s+@source=\S+", "", dest_manifest.read_text(encoding="utf-8")
    )
    write_lf(dest_manifest, legacy_text)

    stray = dest / stray_rel
    stray.parent.mkdir(parents=True, exist_ok=True)
    user_bytes = b"# user-owned stray; must survive\n"
    stray.write_bytes(user_bytes)

    selected = pm_update.resolve_manifest_selfheal(dest, REPO)
    selected_flavors = {
        path.parents[1].name
        for path in selected["upstream_manifests"]
        if len(path.parents) >= 2 and path.parents[2].name == "templates"
    }
    assert stray_flavor not in selected_flavors, \
        "부분 존재 flavor가 upstream_manifests에 무단 편입됨(all→any mutant)"
    assert not selected.get("multi_flavor_recovery"), \
        "부분 존재인데 multi_flavor_recovery가 발화함"
    capsys.readouterr()

    monkeypatch.setattr(pm_update, "REPO", dest)
    monkeypatch.setenv("PM_NONINTERACTIVE", "1")
    assert pm_update.main(["--from", str(REPO)]) == 0

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "설치된 다중 하네스" not in combined
    assert "@source 선언이 없는 legacy manifest" in combined
    assert f"타 flavor({stray_flavor}) 관리 경로 일부" in combined
    assert "어느 동기 채널도 선언하지 않은 출하 파일" in combined
    cli_flavor = "claude" if stray_flavor == "claude_code" else stray_flavor
    assert f"`add-harness {cli_flavor}`는 그 하네스 어댑터만 등재한다" in combined
    migration = re.search(
        r"<manager>/pm-import\.sh --into <project> --harness (\S+) --dry-run",
        combined,
    )
    assert migration, "frozen 진단에 실행 가능한 dry-run 마이그레이션 명령이 없음"
    guided_selection = pm_import.parse_harness_selection(migration.group(1))
    assert cli_flavor in guided_selection, \
        f"안내 명령이 실제 관측 flavor({cli_flavor})를 포함하지 않음: {guided_selection}"
    assert migration.group(1) == "all", \
        "불완전한 관측만으로 flavor 집합을 좁혀 frozen 복구를 누락할 수 있음"
    observed_rel = "/".join(Path(stray_rel).parts[:2])
    candidate_manifests = sorted(
        REPO.glob("templates/*/.project_manager/engine.manifest")
    )
    candidate_paths = {
        path: {
            str(entry).replace("\\", "/")
            for entry in pm_update.read_manifest(path)
        }
        for path in candidate_manifests
    }
    stray_manifest = next(
        path for path in candidate_manifests
        if path.parents[1].name == stray_flavor
    )
    local_core_paths = {
        str(entry).replace("\\", "/")
        for entry in pm_update.read_manifest(dest_manifest)
    }
    other_paths = set().union(*(
        paths for path, paths in candidate_paths.items()
        if path != stray_manifest
    ))
    evidence_paths = set(pm_update._flavor_exclusive_paths(
        pm_update.read_manifest(stray_manifest), other_paths
    )) - local_core_paths
    if stray_flavor == "opencode":
        assert ".claude/skills/pm-dev-delegate/SKILL.md" not in evidence_paths, (
            "공용 .claude/skills 아래 target override를 배타 opencode evidence로 오판함"
        )
    assert re.search(
        rf"\(1/{len(evidence_paths)}: {re.escape(observed_rel)}\)", combined
    ), f"진단에 관측 형상(n/m: 경로)이 없음: {combined!r}"
    assert "그 하네스 어댑터만 등재한다(엔진 전량 마이그레이션 아님)" in combined
    assert "stray라면 이 경고를 무시해도 된다" in combined
    assert stray.read_bytes() == user_bytes


def test_legacy_opencode_manifest_provenance_upgrade_avoids_bare_source_rc2(
        pm_import, pm_update, tmp_path, monkeypatch, capsys):
    """경로가 같아도 @source 전무 opencode manifest를 승격해 bare `.opencode/*` rc=2를 막는다."""
    dest = tmp_path / "legacy-opencode-provenance"
    assert pm_import.main([
        "--new", str(dest), "--harness", "opencode", "--name", "Legacy OpenCode",
    ]) == 0
    capsys.readouterr()
    manifest = dest / ".project_manager" / "engine.manifest"
    manifest.write_text(
        re.sub(r"\s+@source=\S+", "", manifest.read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    preview = pm_update.resolve_manifest_selfheal(dest, REPO)
    assert preview["status"] == "heal"
    assert preview["added"] == []
    assert preview["provenance_upgrade"] is True
    capsys.readouterr()

    monkeypatch.setattr(pm_update, "REPO", dest)
    monkeypatch.setenv("PM_NONINTERACTIVE", "1")
    assert pm_update.main(["--from", str(REPO)]) == 0

    out = capsys.readouterr().out
    assert "@source provenance 선언을 upstream 형식으로 승격" in out
    entries = pm_update.read_manifest(manifest)
    opencode_entries = [e for e in entries if str(e).startswith(".opencode/")]
    assert opencode_entries and all(entry.source_rel for entry in opencode_entries)


def test_legacy_opencode_primary_with_missing_file_heals_in_one_run(
        pm_import, pm_update, tmp_path, monkeypatch, capsys):
    """manifest 엔트리 경로 자체가 없어도 legacy primary를 선택해 provenance와 파일을 함께 복구한다."""
    dest = tmp_path / "legacy-opencode-missing"
    assert pm_import.main([
        "--new", str(dest), "--harness", "opencode", "--name", "Legacy Missing",
    ]) == 0
    capsys.readouterr()
    manifest = dest / ".project_manager" / "engine.manifest"
    manifest.write_text(
        re.sub(r"\s+@source=\S+", "", manifest.read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    victim_rel = Path(".opencode/.gitignore")
    victim = dest / victim_rel
    expected = (REPO / "templates" / "opencode" / victim_rel).read_bytes()
    victim.unlink()
    assert not victim.exists(), "가드 fixture가 manifest 엔트리 경로 자체를 제거하지 못함"

    preview = pm_update.resolve_manifest_selfheal(dest, REPO)
    assert preview["status"] == "heal"
    assert preview["provenance_upgrade"] is True
    assert preview["upstream_manifests"][0].parents[1].name == "opencode", \
        "누락 파일 때문에 legacy primary가 root manifest로 폴백함"
    capsys.readouterr()

    monkeypatch.setattr(pm_update, "REPO", dest)
    monkeypatch.setenv("PM_NONINTERACTIVE", "1")
    assert pm_update.main(["--from", str(REPO)]) == 0

    assert victim.read_bytes() == expected, "누락된 primary 관리 파일이 같은 sync에서 복구되지 않음"
    entries = pm_update.read_manifest(manifest)
    opencode_entries = [e for e in entries if str(e).startswith(".opencode/")]
    assert opencode_entries and all(entry.source_rel for entry in opencode_entries)


def test_declared_claude_manifest_with_opencode_tree_warns_without_promoting(
        pm_import, pm_update, tmp_path, monkeypatch, capsys):
    """선언 claude manifest + 실재 opencode 트리는 frozen 경고만 내고 선택 flavor를 승격하지 않는다."""
    dest = tmp_path / "declared-claude-with-frozen-opencode"
    assert pm_import.main([
        "--new", str(dest), "--harness", "claude,opencode", "--name", "Declared Frozen",
    ]) == 0
    capsys.readouterr()
    claude_manifest = (
        REPO / "templates" / "claude_code" / ".project_manager" / "engine.manifest"
    )
    dest_manifest = dest / ".project_manager" / "engine.manifest"
    dest_manifest.write_bytes(claude_manifest.read_bytes())

    preview = pm_update.resolve_manifest_selfheal(dest, REPO)
    assert preview["upstream_manifests"] == [claude_manifest]
    assert not preview.get("multi_flavor_recovery")
    diagnostic = capsys.readouterr().err
    assert "미등재 flavor 파일 관측" in diagnostic
    assert "선언되지 않은 타 flavor(opencode)" in diagnostic
    assert "`add-harness opencode`는 그 하네스 어댑터만 등재한다" in diagnostic
    assert "<manager>/pm-import.sh --into <project> --harness all" in diagnostic

    monkeypatch.setattr(pm_update, "REPO", dest)
    monkeypatch.setenv("PM_NONINTERACTIVE", "1")
    assert pm_update.main(["--from", str(REPO)]) == 0
    combined = capsys.readouterr()
    assert "미등재 flavor 파일 관측" in combined.err
    assert "설치된 다중 하네스의 manifest 누락" not in combined.out


def test_declared_clean_claude_adopter_has_no_frozen_warning(
        pm_import, pm_update, tmp_path, monkeypatch, capsys):
    """순정 단일-flavor 선언 채택자는 자기 upstream만 선택하고 frozen 경고 없이 갱신한다."""
    dest = tmp_path / "declared-clean-claude"
    assert pm_import.main([
        "--new", str(dest), "--harness", "claude", "--name", "Declared Clean",
    ]) == 0
    capsys.readouterr()
    claude_manifest = (
        REPO / "templates" / "claude_code" / ".project_manager" / "engine.manifest"
    )

    preview = pm_update.resolve_manifest_selfheal(dest, REPO)
    assert preview["upstream_manifests"] == [claude_manifest]
    assert "미등재 flavor 파일 관측" not in capsys.readouterr().err

    monkeypatch.setattr(pm_update, "REPO", dest)
    monkeypatch.setenv("PM_NONINTERACTIVE", "1")
    assert pm_update.main(["--from", str(REPO)]) == 0
    captured = capsys.readouterr()
    assert "미등재 flavor 파일 관측" not in captured.err
    assert "./pm-config.sh add-harness" not in captured.err


def test_declared_claude_with_opencode_guest_has_channel_not_frozen_warning(
        pm_import, pm_config, pm_update, tmp_path, monkeypatch, capsys):
    """add-harness guest 의 어댑터 엔진 파일은 **동기 채널을 갖는다** — frozen 경고 대신 전파 계획.

    옛 형상에선 flavor 의 `@render` 선언만 guest 로 등재돼 `.opencode/lib`·`plugins`·driver 같은
    비-@render 어댑터 엔진 파일이 어떤 동기 채널도 없이 동결됐고, T-0573 이 그 동결을 frozen 경고로
    표면화했다. T-0574 가 그 파일들에 update 채널을 주면서 **동결 케이스 자체가 소멸**한다 — 이제
    같은 형상에서 옳은 산출은 경고가 아니라 계획 편입이다(경고의 선언/legacy 축은
    `test_pm_update.py` 의 합성·수기 절 픽스처가 계속 지킨다).

    민감도: 절이 실제로 엔진 행을 담고 그 경로가 계획 좌표로 뜨는지 함께 단언해 "경고가 없다" 가
    "채널이 생겼다" 로만 성립하게 한다(경고만 지운 회귀는 red)."""
    dest = tmp_path / "declared-claude-opencode-guest"
    assert pm_import.main([
        "--new", str(dest), "--harness", "claude", "--name", "Declared Guest",
    ]) == 0
    capsys.readouterr()
    args = argparse.Namespace(
        harness="opencode", dry_run=False, source=REPO)
    assert pm_config.cmd_add_harness(
        args, pm_import=pm_import, dest_root=dest) == 0
    capsys.readouterr()

    # 절이 어댑터 엔진 파일을 담는다(채널 존재의 근거).
    guest_engine = {
        str(e).replace("\\", "/")
        for e in pm_update._dest_guest_manifest_entries(dest)
        if not getattr(e, "render", False)
    }
    assert ".opencode/pm_orch_opencode.py" in guest_engine, \
        f"어댑터 엔진 파일이 guest 절에 없다(채널 부재·영구 동결): {sorted(guest_engine)}"

    # 채택자 사본을 stale 로 만들어 이번 동기가 실제로 그 파일을 계획에 싣는지 본다.
    driver = dest / ".opencode" / "pm_orch_opencode.py"
    driver.write_text("# stale adopter copy\n", encoding="utf-8")

    monkeypatch.setattr(pm_update, "REPO", dest)
    monkeypatch.setenv("PM_NONINTERACTIVE", "1")
    assert pm_update.main(["--from", str(REPO), "--dry-run"]) == 0

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert ".opencode/pm_orch_opencode.py" in captured.out, \
        f"stale 어댑터 엔진 파일이 계획에 없다(동기 채널 미작동): {captured.out!r}"
    assert "미등재 flavor 파일 관측" not in combined, \
        f"채널이 생긴 파일에 여전히 frozen 경고(오탐): {combined!r}"


def test_codex_rules_converge_on_update_for_main_harness_and_guest(
        pm_import, pm_update, tmp_path, monkeypatch, capsys):
    """codex execpolicy rules 가 **주하네스·guest 두 형상 모두** 상류와 수렴한다 (T-0584 e2e).

    `.codex/rules/default.rules` 는 출하되면서 어느 manifest 에도 없어 두 형상 다 채널이 0 이었다 —
    주하네스 채택자는 pm_update 대상 밖이고, guest 절은 flavor manifest 파생이라 add-harness 도 못
    실었다(등재 유일분인 `@render` 행에도 안 들어간다). 등재 행 하나가 두 채널을 동시에 여는지
    실 install + 실 sync 로 확인한다.

    두 dest 의 채널 근거가 다르다 — 주하네스는 flavor manifest 등재 행, guest 는 그 행에서 파생된
    guest 절 엔진 행이다. 그래서 guest 쪽은 절 등재도 함께 단언한다(수렴만 보면 host 채널로
    우연히 맞은 경우와 구분되지 않는다)."""
    rules_rel = ".codex/rules/default.rules"
    upstream_bytes = (REPO / "templates" / "codex" / rules_rel).read_bytes()

    main_dest = tmp_path / "codex-main-harness"
    assert pm_import.main([
        "--new", str(main_dest), "--harness", "codex", "--name", "Rules Adopter",
    ]) == 0
    guest_dest = tmp_path / "claude-host-codex-guest"
    assert pm_import.main([
        "--new", str(guest_dest), "--harness", "claude", "--name", "Rules Adopter",
    ]) == 0
    pm_import.add_harness(guest_dest, "codex", dry_run=False, source_root=REPO)
    capsys.readouterr()

    guest_engine_rows = {
        str(entry).replace("\\", "/")
        for entry in pm_update._dest_guest_manifest_entries(guest_dest)
        if not getattr(entry, "render", False)
    }
    assert rules_rel in guest_engine_rows, (
        f"guest 절에 rules 엔진 행이 없다(guest 채택자 영구 동결): {sorted(guest_engine_rows)}")

    for label, dest in (("주하네스", main_dest), ("guest", guest_dest)):
        landed = dest / rules_rel
        assert landed.is_file(), f"{label}: {rules_rel} 미배포(픽스처 전제 붕괴)"
        landed.write_text("# stale adopter copy\n", encoding="utf-8")
        monkeypatch.setattr(pm_update, "REPO", dest)
        monkeypatch.setenv("PM_NONINTERACTIVE", "1")
        assert pm_update.main(["--from", str(REPO)]) == 0, f"{label}: self-update rc≠0"
        capsys.readouterr()
        assert landed.read_bytes() == upstream_bytes, (
            f"{label}: {rules_rel} 가 상류와 수렴하지 않음(동기 채널 미작동)")


def test_legacy_frozen_both_full_reimport_makes_next_update_refresh_both_flavors(
        pm_import, pm_update, tmp_path, monkeypatch, capsys):
    """문서 절차를 그대로 밟으면 declared union이 설치되고 다음 pm_update가 두 flavor 전체를 갱신한다."""
    dest = tmp_path / "legacy-both"
    assert pm_import.main([
        "--new", str(dest), "--harness", "claude,opencode", "--name", "Legacy Both",
    ]) == 0
    capsys.readouterr()

    # 구 설치 형상: adapter는 둘 다 있지만 manifest에는 선택-tree @source provenance가 없다.
    dest_manifest = dest / ".project_manager" / "engine.manifest"
    claude_manifest = (
        REPO / "templates" / "claude_code" / ".project_manager" / "engine.manifest")
    legacy_text = re.sub(r"\s+@source=\S+", "", claude_manifest.read_text(encoding="utf-8"))
    write_lf(dest_manifest, legacy_text)
    victim_rels = (
        Path(".claude/ctx_guard.py"),
        Path(".opencode/lib/safe-write-core.cjs"),
    )
    expected = {
        rel: (
            REPO
            / "templates"
            / ("claude_code" if rel.parts[0] == ".claude" else "opencode")
            / rel
        ).read_bytes()
        for rel in victim_rels
    }
    for rel in victim_rels:
        (dest / rel).write_text(f"# frozen before migration: {rel}\n", encoding="utf-8")
    expected_manifest = pm_update.merge_manifest_sources([
        REPO / "templates" / flavor / ".project_manager" / "engine.manifest"
        for dirs in pm_import.HARNESS_TEMPLATE_DIRS.values()
        for flavor in dirs
    ])["text"].encode("utf-8")

    preview = pm_update.resolve_manifest_selfheal(dest, REPO)
    assert preview["upstream_manifests"] == [claude_manifest]
    diagnostic = capsys.readouterr().err
    assert "미등재 flavor 파일 관측" in diagnostic
    assert "<manager>/pm-import.sh --into <project> --harness all --dry-run" in diagnostic

    before_dry_run = dest_manifest.read_bytes()
    assert pm_import.main([
        "--into", str(dest), "--harness", "all", "--name", "Legacy Both", "--dry-run",
    ]) == 0
    assert dest_manifest.read_bytes() == before_dry_run
    capsys.readouterr()
    assert pm_import.main([
        "--into", str(dest), "--harness", "all", "--name", "Legacy Both",
    ]) == 0
    assert dest_manifest.read_bytes() == expected_manifest

    # 절차 직후 다시 두 flavor를 낡게 만들어, "다음 pm_update부터 전부 갱신"을 분리해 실증한다.
    for rel in victim_rels:
        (dest / rel).write_text(f"# frozen after migration: {rel}\n", encoding="utf-8")
    capsys.readouterr()
    monkeypatch.setattr(pm_update, "REPO", dest)
    monkeypatch.setenv("PM_NONINTERACTIVE", "1")
    assert pm_update.main(["--from", str(REPO)]) == 0
    for rel in victim_rels:
        assert (dest / rel).read_bytes() == expected[rel]
    assert dest_manifest.read_bytes() == expected_manifest


def test_combination_identical_relpath_silent(pm_import, tmp_path, capsys):
    """byte-identical 한 공유 엔진(board.py 등)은 경고 없이 조용히 한 번만 복사."""
    dest = tmp_path / "bothsilent"
    rc = pm_import.main([
        "--new", str(dest), "--harness", "claude,opencode", "--name", "BS",
    ])
    assert rc == 0
    captured = capsys.readouterr()
    # board.py 는 두 트리에서 동일 — 이 파일에 대한 경고는 없어야 한다.
    assert "tools/board.py" not in captured.err


# ── SF1: 같은 날 --into 2회 — 1회차 원본 백업 보존 ───────────────────────────

def test_into_rerun_same_day_preserves_first_backup(pm_import, tmp_path):
    """T-0034: 같은 날 --into 2회차가 1회차 백업(=진짜 사용자 원본)을 덮지 않는다 — 중앙
    디렉토리 안에서 _free_backup_path 순번 부여(SF1 유지). 비-git tmp → 충돌 전부 백업."""
    dest = tmp_path / "samedaybackup"
    dest.mkdir()
    original_content = "## 진짜 사용자 원본 — 영구 보존되어야 함\n"
    (dest / "CLAUDE.md").write_text(original_content, encoding="utf-8")

    today = datetime.date.today().isoformat()
    backup_root = dest / pm_import.BACKUP_DIR_NAME / today

    # 1회차: 사용자 원본을 중앙 디렉토리에 백업하고 템플릿으로 덮음.
    rc1 = pm_import.main(["--into", str(dest), "--harness", "claude", "--name", "S1"])
    assert rc1 == 0
    backup1 = backup_root / "CLAUDE.md"
    assert backup1.read_text(encoding="utf-8") == original_content

    # 2회차 같은 날: 현 CLAUDE.md(=1회차 템플릿)를 백업하지만, 1회차 백업은 덮지 않는다.
    rc2 = pm_import.main(["--into", str(dest), "--harness", "claude", "--name", "S2"])
    assert rc2 == 0

    # 1회차 백업(진짜 원본)이 살아있어야 한다.
    assert backup1.read_text(encoding="utf-8") == original_content, \
        "같은 날 재실행이 1회차 원본 백업을 덮음 — SF1 위반(원본 영구 손실)."
    # 2회차는 중앙 디렉토리 안에서 순번 백업(CLAUDE.md.1)을 만든다.
    backup2 = backup_root / "CLAUDE.md.1"
    assert backup2.is_file(), "2회차 백업이 중앙 디렉토리 순번(CLAUDE.md.1)으로 보존되지 않음."


# ── MF1(codex 4차): --into 재-import 시 기존 local.conf 백업 + 사용자 키 보존 ──

def test_into_backs_up_and_preserves_existing_local_conf(pm_import, tmp_path):
    """이미 프레임워크를 쓰던 프로젝트(local.conf 존재)에 --into 재-import 하면, board.py
    init 의 무조건 덮어쓰기로 잃을 per-clone 설정을 ① *.backup.<DATE> 로 백업하고 ② 새
    local.conf 에 재병합한다 — operational sync(project_name·test_cmd)도 동시 충족.

    codex 4차 MF1: local.conf 는 pm_import 의 copy/backup 대상 트리 밖이라, board.py init
    이 통째로 덮으면 additional_reviewer_enabled·reviewer_cmd·session 등이 무백업 손실된다.

    T-0021 메모 — additional_reviewer_enabled 보존(아래 assert)은 **T-0017 의 board.py
    EOF/비대화 가드**에 의존한다: board init 은 pm_import 가 stdin=DEVNULL 로 호출하므로
    `prompt_external_review_optin` 은 비대화(isatty=False/EOF)로 판정해 **아무것도 쓰지
    않고 반환**해야 한다. 그래야 reapply_preserved_conf_keys 가 백업의 사용자값('true')을
    그대로 재병합한다. board.py 가 pre-fix(가드 없음)면 init 이 `additional_reviewer_enabled=false`
    를 먼저 써 버려 재병합이 스킵되고 이 테스트는 'false' 로 실패한다 — 정상(엔진 미수정 신호).
    이 ticket(tests-only)에서는 board.py 를 고치지 않으므로, 복사되는 엔진이 pre-fix 인 run
    에서는 이 테스트가 red 일 수 있다. 통합(T-0017 머지·pm_update 동기화) 후 green 이어야 한다.
    """
    dest = tmp_path / "reimport"
    dest.mkdir()
    pm_dir = dest / ".project_manager"
    pm_dir.mkdir()
    existing_conf = pm_dir / "local.conf"
    # 기존 프로젝트가 갖고 있던 per-clone 설정(board init 솔로가 안 쓰는 키 포함).
    existing_content = (
        "# 기존 사용자 local.conf — 보존되어야 함\n"
        "additional_reviewer_enabled=true\n"
        "reviewer_cmd=foo\n"
        "session=mine\n"
    )
    existing_conf.write_text(existing_content, encoding="utf-8")

    today = datetime.date.today().isoformat()
    rc = pm_import.main(["--into", str(dest), "--harness", "claude", "--name", "Reimport"])
    assert rc == 0

    # ① 기존 local.conf 가 중앙 디렉토리에 relpath 미러링으로 백업되고 원본 내용 보존(T-0034).
    backup = dest / pm_import.BACKUP_DIR_NAME / today / ".project_manager" / "local.conf"
    assert backup.is_file(), "기존 local.conf 가 중앙 디렉토리에 백업되지 않음 — MF1 위반."
    assert backup.read_text(encoding="utf-8") == existing_content, \
        "local.conf 백업이 원본 내용을 보존하지 않음."
    # 형제 백업(.project_manager/local.conf.backup.<DATE>)은 더 이상 만들지 않는다.
    assert not list(pm_dir.glob("local.conf.backup.*")), "형제 local.conf 백업 잔존 — 중앙화 위반."

    # ② 새 local.conf = board init 기본 + 사용자 키 보존 + operational sync 동시 충족.
    conf = _parse_conf(existing_conf)
    # board init 솔로가 안 쓰는 사용자 키 보존.
    assert conf.get("additional_reviewer_enabled") == "true", \
        f"additional_reviewer_enabled 가 보존되지 않음: {conf.get('additional_reviewer_enabled')!r}"
    assert conf.get("reviewer_cmd") == "foo", \
        f"reviewer_cmd 가 보존되지 않음: {conf.get('reviewer_cmd')!r}"
    # operational sync 동시 충족 (project_name·test_cmd 가 pm_import 치환값).
    assert conf.get("project_name") == "Reimport", \
        f"operational sync 미충족 — project_name: {conf.get('project_name')!r}"
    assert conf.get("test_cmd") == pm_import._default_test_cmd(), \
        f"operational sync 미충족 — test_cmd: {conf.get('test_cmd')!r}"


def test_into_local_conf_init_keys_take_precedence(pm_import, tmp_path):
    """재-import 는 기존 사용자 설정을 보존한다 — session 은 명시 인자가 없으므로 기존값
    ('mine')을 유지하고(T-0184 비파괴 병합·cmd_init 이 통째 덮지 않음), init 이 안 쓰는
    사용자 키(additional_reviewer_enabled)도 보존된다.

    T-0184 이전엔 board init 이 local.conf 를 통째 덮어 session 이 init 솔로 기본('pm')으로
    리셋됐고 기존 'mine' 은 백업에만 남았다(데이터 손실 버그). 이제 cmd_init 은 local.conf
    존재 시 병합하며 session·prefix 는 *명시 인자일 때만* 교체한다 — pm_import 의
    run_board_init 은 --session 을 넘기지 않으므로 기존 session 이 보존된다."""
    dest = tmp_path / "precedence"
    dest.mkdir()
    pm_dir = dest / ".project_manager"
    pm_dir.mkdir()
    (pm_dir / "local.conf").write_text(
        "session=mine\nadditional_reviewer_enabled=false\n", encoding="utf-8"
    )

    rc = pm_import.main(["--into", str(dest), "--harness", "claude", "--name", "Prec"])
    assert rc == 0

    conf = _parse_conf(pm_dir / "local.conf")
    # 명시 --session 이 없으므로 기존 session 이 보존된다(T-0184 비파괴 병합).
    assert conf.get("session") == "mine", \
        f"재-import 가 기존 session 을 보존하지 않음(T-0184 비파괴 병합 기대): {conf.get('session')!r}"
    # board init 이 안 쓰는 사용자 키는 보존.
    assert conf.get("additional_reviewer_enabled") == "false"


# ── T-0071: run_board_init 이 subprocess env 에 PM_NONINTERACTIVE=1 명시 전달 ──

def test_run_board_init_passes_pm_noninteractive_env(pm_import, tmp_path, monkeypatch):
    """run_board_init 이 셋업 subprocess env 에 PM_NONINTERACTIVE=1 을 넣는지.

    Windows DEVNULL stdin 의 isatty() 신뢰불가 함정 회피(T-0071) — stdin=DEVNULL 와 함께
    env 명시 신호로 external_review opt-in 프롬프트를 결정적 skip. 실 셋업을 돌리지
    않고 subprocess.run 을 가로채(부작용 0) 전달된 env/stdin 만 친다.
    """
    # 셋업 진입(pm_config init — board init 위임 + 홈 슬롯 등록) 존재 가드를 통과시킬 더미 트리.
    entry = tmp_path / ".project_manager" / "tools" / "pm_config.py"
    entry.parent.mkdir(parents=True)
    entry.write_text("# stub\n", encoding="utf-8")

    captured: dict = {}

    class _FakeCompleted:
        returncode = 0

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs.get("env")
        captured["stdin"] = kwargs.get("stdin")
        return _FakeCompleted()

    monkeypatch.setattr(pm_import.subprocess, "run", fake_run)

    rc = pm_import.run_board_init(tmp_path)
    assert rc == 0
    assert captured["env"] is not None, "env 미전달 — PM_NONINTERACTIVE 주입 누락."
    assert captured["env"].get("PM_NONINTERACTIVE") == "1", \
        f"PM_NONINTERACTIVE=1 미주입: {captured['env'].get('PM_NONINTERACTIVE')!r}"
    # 기존 ambient env 도 보존(전체 교체가 아니라 병합)·stdin=DEVNULL 유지.
    assert captured["env"].get("PATH") == os.environ.get("PATH")
    assert captured["stdin"] == pm_import.subprocess.DEVNULL


def test_into_no_existing_local_conf_no_backup(pm_import, tmp_path):
    """기존 local.conf 가 없는 --into(빈 디렉토리)는 local.conf 백업을 만들지 않는다."""
    dest = tmp_path / "freshinto"
    dest.mkdir()
    rc = pm_import.main(["--into", str(dest), "--harness", "claude", "--name", "Fresh"])
    assert rc == 0
    backups = list((dest / ".project_manager").glob("local.conf.backup.*"))
    assert backups == [], f"기존 local.conf 없는데 백업이 생성됨: {backups}"
    # 정상 local.conf 는 생성됨.
    assert (dest / ".project_manager" / "local.conf").is_file()


def test_backup_existing_local_conf_returns_none_when_absent(pm_import, tmp_path):
    """backup_existing_local_conf: local.conf 없으면 None 반환 · 백업 미생성(단위)."""
    dest = tmp_path / "unit_no_conf"
    (dest / ".project_manager").mkdir(parents=True)
    # 새 시그니처: backup_root 는 Path|None (이전 문자열 suffix 아님).
    backup_root = dest / pm_import.BACKUP_DIR_NAME / "2026-06-14"
    result = pm_import.backup_existing_local_conf(dest, backup_root)
    assert result is None
    assert not list((dest / ".project_manager").glob("local.conf.backup.*"))


def test_into_rejects_backup_dir_name_as_file(pm_import, tmp_path):
    """중앙 백업 디렉토리 자리(`.pm_import_backups`)에 일반 파일이 있으면 plan 단계 거부 (codex T-0034).

    backup target 의 mkdir(parents) 가 apply 중 터져 부분 복사가 남는 것을 사전 차단 — 비0·무변경."""
    dest = tmp_path / "bdirfile"
    dest.mkdir()
    (dest / "CLAUDE.md").write_text("user content\n", encoding="utf-8")   # 충돌(백업 경로 사용)
    occupied = dest / pm_import.BACKUP_DIR_NAME
    occupied.write_text("not a directory\n", encoding="utf-8")            # 백업 디렉토리 자리 점유
    rc = pm_import.main(["--into", str(dest), "--harness", "claude", "--name", "BDir"])
    assert rc == 1, "백업 디렉토리 자리가 일반 파일인데 거부하지 않음."
    # 부분 복사 없음 — 점유 파일은 여전히 파일(디렉토리로 안 바뀜)·내용 불변.
    assert occupied.is_file() and occupied.read_text(encoding="utf-8") == "not a directory\n"


def test_into_rejects_deep_backup_ancestor_file(pm_import, tmp_path):
    """중앙 백업 경로의 *깊은* 조상(`.pm_import_backups/<DATE>/.project_manager`)이 일반 파일이면
    plan 단계 거부 — local.conf 백업이 복사 일부 뒤 mkdir 로 터지는 부분 적용 방지 (codex T-0034 R4)."""
    dest = tmp_path / "deepanc"
    (dest / ".project_manager").mkdir(parents=True)
    (dest / ".project_manager" / "local.conf").write_text("prefix=x\n", encoding="utf-8")  # 백업 대상
    today = datetime.date.today().isoformat()
    # 백업 경로의 깊은 조상을 일반 파일로 점유 — local.conf 백업 target 의 .project_manager 조상.
    bdated = dest / pm_import.BACKUP_DIR_NAME / today
    bdated.mkdir(parents=True)
    (bdated / ".project_manager").write_text("blocker\n", encoding="utf-8")
    rc = pm_import.main(["--into", str(dest), "--harness", "claude", "--name", "Deep"])
    assert rc == 1, "깊은 백업 조상이 일반 파일인데 거부하지 않음(부분 적용 위험)."
    # 부분 적용 없음 — blocker 불변·import 산출물(.project_manager/tools 등) 미생성.
    assert (bdated / ".project_manager").is_file()
    assert not (dest / ".project_manager" / "tools").exists(), "부분 복사 잔존 — plan 거부 실패."


def test_into_rejects_file_vs_dir_conflict(pm_import, tmp_path):
    """SF(codex 4차): dst 위치에 기존 디렉토리가 있으면 IsADirectoryError 로 터지지 않고
    plan 단계에서 명시적 거부 — 비0 종료 · 부분 복사 없음 · 사용자 디렉토리 불변."""
    dest = tmp_path / "filedir"
    dest.mkdir()
    # CLAUDE.md(claude 어댑터가 파일로 복사하는 경로) 위치에 디렉토리를 둔다.
    clobber_dir = dest / "CLAUDE.md"
    clobber_dir.mkdir()
    (clobber_dir / "inner.txt").write_text("user dir content\n", encoding="utf-8")

    rc = pm_import.main(["--into", str(dest), "--harness", "claude", "--name", "FD"])
    assert rc != 0, "dst 가 디렉토리인데 성공/예외 — 명시적 거부여야 함."
    # 사용자 디렉토리 불변(자동 삭제 금지) · 부분 복사 흔적(board.py 등) 없음.
    assert (clobber_dir / "inner.txt").read_text(encoding="utf-8") == "user dir content\n"
    assert not (dest / ".project_manager" / "tools" / "board.py").exists(), \
        "file-vs-dir 거부인데 트리가 부분 복사됨."


def test_into_file_vs_dir_conflict_in_dry_run(pm_import, tmp_path):
    """dry-run 에서도 file-vs-dir 충돌을 거부한다(plan 단계 게이트 · 파일시스템 미변경)."""
    dest = tmp_path / "filedirdry"
    dest.mkdir()
    (dest / "CLAUDE.md").mkdir()
    rc = pm_import.main(["--into", str(dest), "--harness", "claude", "--dry-run"])
    assert rc != 0
    assert not (dest / ".project_manager").exists()


# ── MF(codex 5차): dst 조상 경로 검증 — 프로젝트 밖 쓰기·부분복사 방지 ─────────

@requires_symlink
def test_into_rejects_symlink_ancestor(pm_import, tmp_path):
    """dst 조상(dest_root 하위)이 외부를 가리키는 symlink 디렉토리면 비0 거부 · 외부 대상 불변.

    codex 5차 MF: 조상이 symlink 면 mkdir(exist_ok=True)+copy2 가 링크를 따라가 프로젝트
    밖에 쓴다(비파괴 위반). plan 단계에서 조상을 거부해야 한다.
    """
    dest = tmp_path / "symancestor"
    dest.mkdir()

    # 외부 디렉토리(프로젝트 밖 모사) — 절대 쓰여선 안 됨.
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    outside_sentinel = outside / "keep.txt"
    outside_sentinel.write_text("외부 — 불변\n", encoding="utf-8")

    # dest/.project_manager 를 외부 디렉토리로 가리키는 symlink (엔진 파일들의 조상).
    link = dest / ".project_manager"
    link.symlink_to(outside, target_is_directory=True)

    rc = pm_import.main(["--into", str(dest), "--harness", "claude", "--name", "SA"])
    assert rc != 0, "조상이 symlink 인데 성공 반환 — MF 위반(프로젝트 밖 쓰기 위험)."
    # 외부 디렉토리 내용 불변 · 외부에 엔진 파일이 안 쓰임.
    assert outside_sentinel.read_text(encoding="utf-8") == "외부 — 불변\n"
    assert not (outside / "tools").exists(), "조상 symlink 를 따라가 외부에 엔진 파일이 쓰임."
    # 링크 자체도 그대로(자동 삭제 금지).
    assert link.is_symlink()


@requires_symlink
def test_into_rejects_symlink_ancestor_in_dry_run(pm_import, tmp_path):
    """dry-run 에서도 조상 symlink 를 거부한다(plan 단계 게이트 · 파일시스템 미변경)."""
    dest = tmp_path / "symancestordry"
    dest.mkdir()
    outside = tmp_path / "outside_dry"
    outside.mkdir()
    (dest / ".project_manager").symlink_to(outside, target_is_directory=True)
    rc = pm_import.main(["--into", str(dest), "--harness", "claude", "--dry-run"])
    assert rc != 0
    assert not (outside / "tools").exists()


def test_into_rejects_file_ancestor(pm_import, tmp_path):
    """dst 조상(dest_root 하위)이 일반 파일이면 비0 거부 · 부분 복사 없음 · 파일 불변.

    codex 5차 MF: 조상이 파일이면 plan 통과 후 apply 중 mkdir 가 터져 부분 복사가 잔존한다.
    plan 단계에서 거부해 부분 복사를 막아야 한다.
    """
    dest = tmp_path / "fileancestor"
    dest.mkdir()
    # .project_manager 를 디렉토리가 아닌 일반 파일로 둔다(엔진 파일들의 조상).
    blocker = dest / ".project_manager"
    blocker.write_text("나는 파일이다\n", encoding="utf-8")

    rc = pm_import.main(["--into", str(dest), "--harness", "claude", "--name", "FA"])
    assert rc != 0, "조상이 일반 파일인데 성공 반환 — MF 위반(부분 복사 위험)."
    # 파일 불변(자동 삭제 금지) · 그 안에 디렉토리가 안 만들어짐 · 부분 복사 흔적 없음.
    assert blocker.is_file()
    assert blocker.read_text(encoding="utf-8") == "나는 파일이다\n"
    assert not (dest / ".claude").exists(), "file-ancestor 거부인데 다른 트리가 부분 복사됨."


def test_into_file_ancestor_in_dry_run(pm_import, tmp_path):
    """dry-run 에서도 조상 파일 충돌을 거부한다(plan 단계 게이트 · 파일시스템 미변경)."""
    dest = tmp_path / "fileancestordry"
    dest.mkdir()
    (dest / ".project_manager").write_text("file\n", encoding="utf-8")
    rc = pm_import.main(["--into", str(dest), "--harness", "claude", "--dry-run"])
    assert rc != 0
    assert not (dest / ".claude").exists()


# ── SF(codex 5차): --new 대상이 기존 파일 — 친화적 비0(iterdir 예외 방지) ────────

def test_new_rejects_existing_file_path(pm_import, tmp_path):
    """--new 대상이 디렉토리가 아닌 기존 파일이면 친화적 비0 거부(iterdir 예외 아님)."""
    dest = tmp_path / "iamafile"
    dest.write_text("user file\n", encoding="utf-8")
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "FF"])
    assert rc != 0, "--new 기존 파일 경로인데 성공/예외 — 친화적 거부여야 함."
    # 기존 파일 불변.
    assert dest.read_text(encoding="utf-8") == "user file\n"


def test_reapply_preserved_conf_keys_only_adds_missing(pm_import, tmp_path):
    """reapply_preserved_conf_keys: 현 local.conf 에 *없는* 기존 키만 재병합, 있는 키는 불변(단위)."""
    dest = tmp_path / "unit_reapply"
    pm_dir = dest / ".project_manager"
    pm_dir.mkdir(parents=True)
    # board init 이 새로 쓴 것을 모사 — session·project_name 보유.
    (pm_dir / "local.conf").write_text(
        "session=pm\nproject_name=New\n", encoding="utf-8"
    )
    # 기존 원본 — session 은 다른 값('mine'), 추가로 additional_reviewer_enabled 보유.
    original = "session=mine\nadditional_reviewer_enabled=true\nreviewer_cmd=bar\n"
    changed = pm_import.reapply_preserved_conf_keys(dest, original)
    assert changed is True

    conf = _parse_conf(pm_dir / "local.conf")
    # 현재 파일에 있던 키는 불변(init 값 우선).
    assert conf["session"] == "pm"
    assert conf["project_name"] == "New"
    # 현재 파일에 없던 기존 키만 재병합.
    assert conf["additional_reviewer_enabled"] == "true"
    assert conf["reviewer_cmd"] == "bar"


# ── T-0590: 추가 리뷰어 프로필 무손실 왕복 (레거시 reviewer_cmd · 구조적/커스텀 튜플) ──
# 온보딩이 심는 키는 `additional_reviewer.<축>` 점 표기다. 보존 경로가 키 이름을 열거하지
# 않는 일반 규칙(_parse_conf_keys 의 문자열 key=value)이라는 사실을 값으로 못박는다 — 열거로
# 바뀌면 커스텀 축을 쓰는 채택자가 재-import 에서 조용히 설정을 잃는다. 자동 마이그레이션도
# 하지 않는다: 레거시만 쓰던 채택자에게 튜플이 생기지 않고, 튜플을 쓰던 채택자의 값이
# 엔진 기본값으로 덮이지 않는다.

_LEGACY_ONLY_CONF = (
    "# 레거시 채택자 — 구조적 튜플 없이 reviewer_cmd 만 쓴다\n"
    "additional_reviewer_enabled=true\n"
    "reviewer_cmd=codex exec --sandbox read-only --skip-git-repo-check\n"
    "session=mine\n"
)

_CANONICAL_TUPLE_CONF = (
    "# 추가 리뷰어(additional reviewer) — ON.\n"
    "additional_reviewer_enabled=true\n"
    "additional_reviewer.harness=codex\n"
    "additional_reviewer.model=gpt-5.6-sol\n"
    "additional_reviewer.reasoning=max\n"
    "session=mine\n"
)

_CUSTOM_TUPLE_CONF = (
    "additional_reviewer_enabled=true\n"
    "additional_reviewer.harness=opencode\n"
    "additional_reviewer.model=qwen3-coder-next\n"
    "additional_reviewer.reasoning=low\n"
    "additional_reviewer.persona=security\n"   # 엔진이 모르는 커스텀 축
    "session=mine\n"
)


@pytest.mark.parametrize(
    "original,expected",
    [
        pytest.param(
            _LEGACY_ONLY_CONF,
            {
                "additional_reviewer_enabled": "true",
                "reviewer_cmd": "codex exec --sandbox read-only --skip-git-repo-check",
            },
            id="legacy-reviewer-cmd-only",
        ),
        pytest.param(
            _CANONICAL_TUPLE_CONF,
            {
                "additional_reviewer_enabled": "true",
                "additional_reviewer.harness": "codex",
                "additional_reviewer.model": "gpt-5.6-sol",
                "additional_reviewer.reasoning": "max",
            },
            id="canonical-tuple",
        ),
        pytest.param(
            _CUSTOM_TUPLE_CONF,
            {
                "additional_reviewer_enabled": "true",
                "additional_reviewer.harness": "opencode",
                "additional_reviewer.model": "qwen3-coder-next",
                "additional_reviewer.reasoning": "low",
                "additional_reviewer.persona": "security",
            },
            id="custom-tuple-values-and-axis",
        ),
    ],
)
def test_into_reimport_round_trips_additional_reviewer_profile(
    pm_import, tmp_path, original, expected
):
    """--into 재-import 가 추가 리뷰어 설정을 값 그대로 왕복시킨다(자동 마이그레이션 0)."""
    dest = tmp_path / "roundtrip"
    pm_dir = dest / ".project_manager"
    pm_dir.mkdir(parents=True)
    (pm_dir / "local.conf").write_text(original, encoding="utf-8")

    rc = pm_import.main(["--into", str(dest), "--harness", "claude", "--name", "RT"])
    assert rc == 0

    conf = _parse_conf(pm_dir / "local.conf")
    for key, value in expected.items():
        assert conf.get(key) == value, f"{key} 왕복 실패: {conf.get(key)!r} != {value!r}"
    # 레거시 채택자에게 구조적 튜플을 새로 만들어 주지 않는다(자동 마이그레이션 금지).
    if "additional_reviewer.harness" not in expected:
        assert not [k for k in conf if k.startswith("additional_reviewer.")], (
            f"레거시 conf 에 추가 리뷰어 튜플이 주입됨: {sorted(conf)}"
        )
    # 튜플을 쓰던 채택자에게 레거시 키를 새로 만들어 주지도 않는다.
    if "reviewer_cmd" not in expected:
        assert "reviewer_cmd" not in conf, "튜플 채택자에게 reviewer_cmd 가 주입됨"


def test_reapply_preserved_conf_keys_round_trips_dotted_and_custom_axes(
    pm_import, tmp_path
):
    """재병합 단위: 점 표기·커스텀 축이 키 열거 없이 그대로 복원된다.

    `_set_conf_keys`/`_parse_conf_keys` 가 정규식이 아니라 문자열 partition 이라 `.` 이
    특수문자로 취급되지 않는다 — 그 성질이 깨지면 여기서 값이 어긋난다.
    """
    dest = tmp_path / "unit_dotted"
    pm_dir = dest / ".project_manager"
    pm_dir.mkdir(parents=True)
    (pm_dir / "local.conf").write_text("session=pm\nproject_name=New\n", encoding="utf-8")

    assert pm_import.reapply_preserved_conf_keys(dest, _CUSTOM_TUPLE_CONF) is True

    conf = _parse_conf(pm_dir / "local.conf")
    assert conf["additional_reviewer.harness"] == "opencode"
    assert conf["additional_reviewer.model"] == "qwen3-coder-next"
    assert conf["additional_reviewer.reasoning"] == "low"
    assert conf["additional_reviewer.persona"] == "security"
    assert conf["session"] == "pm"           # init 값 우선(원본 'mine' 이 이기지 않는다)
    assert conf["project_name"] == "New"


# ── SF3: __pycache__ / .pyc 복사 제외 ────────────────────────────────────────

def test_import_excludes_pycache(pm_import, tmp_path):
    """__pycache__/*.pyc(stale 바이트코드)는 새 프로젝트로 복사되지 않는다."""
    dest = tmp_path / "nopyc"
    rc = pm_import.main([
        "--new", str(dest), "--harness", "claude,opencode", "--name", "NP",
    ])
    assert rc == 0
    pycache_dirs = [p for p in dest.rglob("__pycache__")]
    assert pycache_dirs == [], f"__pycache__ 가 복사됨: {pycache_dirs}"
    pyc_files = [p for p in dest.rglob("*.pyc")]
    assert pyc_files == [], f".pyc 가 복사됨: {pyc_files}"


# ── SF2: board.py init 비0 → main 비0 전파 (성공으로 묻히지 않음) ─────────────

def test_board_init_failure_propagates_nonzero(pm_import, tmp_path, monkeypatch):
    """board.py init 가 비0 종료하면(local.conf·pm_state 미생성 = import 미완) main 도 비0.

    init 실패를 monkeypatch 로 모사 — 복사·치환은 정상 끝났어도 import 미완으로 판정.
    """
    dest = tmp_path / "initfail"
    monkeypatch.setattr(pm_import, "run_board_init", lambda dest_root: 3)
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "IF"])
    assert rc == 3, "board.py init 비0 인데 main 이 0 반환 — SF2 위반(성공으로 묻힘)."
    # 복사 자체는 일어났음(init 만 실패) — 트리는 존재.
    assert (dest / "CLAUDE.md").is_file()


# ── MF1(codex 2차): --into 충돌 dst 가 symlink — 링크 대상 불변, 링크 자체 백업 ──

@requires_symlink
def test_into_symlink_conflict_does_not_follow_link(pm_import, tmp_path):
    """기존 dst 가 symlink 면 링크를 *따라가지 않는다* — 링크 대상 파일(프로젝트 밖일 수
    있음)은 글자 하나 안 바뀌고, 백업은 링크 자체, 새 dst 는 일반 파일(템플릿 내용).

    codex 2차 MF1: shutil.copy2 가 symlink 를 follow 하면 링크 대상 파일을 백업/덮어써
    비파괴 계약 위반 + 프로젝트 밖 파일 변조. run() 은 링크 자체를 처리해야 한다.
    """
    dest = tmp_path / "symconflict"
    dest.mkdir()

    # 링크 대상(프로젝트 밖을 모사) — 절대 건드리면 안 되는 외부 파일.
    outside = tmp_path / "outside_target"
    outside.mkdir()
    link_target = outside / "real_claude.md"
    target_content = "## 외부 링크 대상 — 절대 불변이어야 함\n"
    link_target.write_text(target_content, encoding="utf-8")

    # 기존 CLAUDE.md 가 외부 파일을 가리키는 symlink (claude 어댑터가 덮을 경로).
    link_path = dest / "CLAUDE.md"
    link_path.symlink_to(link_target)

    today = datetime.date.today().isoformat()
    rc = pm_import.main(["--into", str(dest), "--harness", "claude", "--name", "SymLink"])
    assert rc == 0

    # ① 링크 대상 파일은 절대 불변(백업도 덮어쓰기도 follow 안 함).
    assert link_target.read_text(encoding="utf-8") == target_content, \
        "symlink 충돌이 링크 대상 파일을 변조함 — MF1 위반(프로젝트 밖 파일 손상)."

    # ② 백업은 링크 *자체*(대상 파일 복제가 아님) — 중앙 디렉토리에 symlink 로 보존되고
    #    원래 대상을 가리킨다(T-0034: symlink 충돌은 git_safe 무관하게 항상 백업).
    backup = dest / pm_import.BACKUP_DIR_NAME / today / "CLAUDE.md"
    assert backup.is_symlink(), "백업이 링크 자체가 아님 — 링크를 따라가 대상을 복제함."
    # os.readlink 는 Windows 에서 링크 대상을 확장길이 접두형(\\?\C:\...)으로 반환할 수 있어
    #   평문 str 비교가 형식 차이로 깨진다(POSIX 는 평문·`\\?\` 는 resolve()도 보존해 무의미).
    #   계약은 "백업 링크가 원래 외부 대상 파일을 가리킨다"이지 링크 문자열의 정확한 형식이
    #   아니므로 same-file 동일성으로 형식-무관 비교한다(T-0211 (a) os-agnostic 패턴).
    assert Path(os.readlink(backup)).samefile(link_target), \
        "백업 링크가 원래 대상을 가리키지 않음."

    # ③ 새 dst 는 일반 파일(symlink 아님)이고 템플릿 내용(외부 대상 내용 아님).
    new_claude = dest / "CLAUDE.md"
    assert not new_claude.is_symlink(), "새 dst 가 여전히 symlink — 링크를 일반 파일로 교체 안 함."
    assert new_claude.is_file()
    assert new_claude.read_text(encoding="utf-8") != target_content, \
        "새 dst 가 외부 대상 내용 — 링크를 따라가 덮어씀."


# ── MF2(codex 2차): --new git init 실패 → main 비0 전파 ───────────────────────

def test_git_init_failure_propagates_nonzero(pm_import, tmp_path, monkeypatch):
    """git init 가 비0 종료하면(git repo 미생성 = pre-push 훅 불가) main 도 비0.

    codex 2차 MF2: git_init 가 returncode 를 무시하면 불완전 import 가 성공으로 끝난다.
    실제 실패 재현 대신 함수 교체로 전파 경로만 검증(SF2 패턴과 동일).
    """
    dest = tmp_path / "gitinitfail"
    monkeypatch.setattr(pm_import, "git_init", lambda dest_root: 128)
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "GF"])
    assert rc == 128, "git init 비0 인데 main 이 0 반환 — MF2 위반(불완전 import 가 묻힘)."


def test_git_init_failure_aborts_before_copy(pm_import, tmp_path, monkeypatch):
    """git init 실패 시 복사 전에 중단 — board.py init 도 안 돌아 미완 상태가 명확하다."""
    dest = tmp_path / "gitinitfail2"
    monkeypatch.setattr(pm_import, "git_init", lambda dest_root: 1)
    # board init 이 절대 호출되면 안 됨(git init 단계에서 이미 중단).
    monkeypatch.setattr(
        pm_import, "run_board_init",
        lambda dest_root: pytest.fail("git init 실패 후 board init 이 호출됨 — 중단 안 됨."),
    )
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "GF2"])
    assert rc == 1


# ── T-0033: opencode 모델 결정적 해소 (opencode models 조회·대화형·플래그·TODO 폴백) ──
# {{OPENCODE_PRO_MODEL}} 은 LLM fill 추측이 아니라 `opencode models` 결정적 조회로 해소한다.
# `opencode models` subprocess 와 stdin(대화형 선택)은 *주입 가능 seam* 으로 stub 한다 —
# 라이브 opencode CLI 는 절대 실행하지 않는다(기존 fill runner 주입 패턴과 동일).

OPENCODE_MODEL_TOKEN = "{{OPENCODE_PRO_MODEL}}"
# `opencode models` 실측 출력 형식(줄당 provider/model) — stub 가 흉내 낸다.
_FAKE_MODELS = ["ollama/gemma4:26b", "opencode/big-pickle", "anthropic/claude-x"]


def _stub_models_runner(ok=True, models=None):
    """`opencode models` 조회 seam stub — 라이브 CLI 미실행. (ok, models) 고정 반환."""
    payload = list(models) if models is not None else list(_FAKE_MODELS)

    def _runner():
        return ok, payload
    return _runner


def _opencode_dest_with_token(pm_import, tmp_path, name):
    """opencode 어댑터를 import 한 *fresh 토큰* 트리를 만든다(함수 단위 resolve 테스트용).

    main 의 기본(비-tty) 경로가 import 중 모델 토큰에 TODO 마커를 붙이므로, resolve_opencode_model
    을 함수 단위로 직접 검증하려면 import 가 남긴 마커를 벗겨 *치환 전 상태*(토큰만·TODO 없음)로
    되돌린다. 이렇게 해야 stub seam(models_runner·stdin)으로 각 경로(flag·interactive·todo)를
    멱등 간섭 없이 검증할 수 있다. 라이브 opencode CLI 는 main 도 함수도 절대 호출하지 않는다.
    """
    dest = tmp_path / name.lower()
    rc = pm_import.main(["--new", str(dest), "--harness", "opencode", "--name", name])
    assert rc == 0
    # import 가 비-tty 경로로 주석화한 모델-토큰 `# model:` 줄을 fresh 활성 토큰 줄로 환원.
    # (T-0077: 폴백이 `model:` 줄을 통째 주석화 — `# model: "..."  # TODO: ...` → `model: "..."`.
    #  T-0133: @render leak-safety 로 폴백이 토큰을 중화 — `# model: "<provider/model>"  # TODO:`
    #  → 활성 토큰 줄 `model: "{{OPENCODE_PRO_MODEL}}"` 로 환원: TODO 마커 제거·`# ` 주석 표식
    #  제거·중화 placeholder(<provider/model>)를 원 토큰으로 복원.)
    for path in dest.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        new_lines = []
        changed_any = False
        for line in text.splitlines(keepends=True):
            stripped = line.lstrip()
            # 폴백이 주석화한 model: 줄 (토큰 잔존 또는 <provider/model> 중화·둘 다 대응).
            is_commented_model = (
                stripped.startswith("#") and "model:" in stripped and "# TODO" in line
                and (OPENCODE_MODEL_TOKEN in line or "<provider/model>" in line)
            )
            if is_commented_model:
                eol = "\n" if line.endswith("\n") else ""
                body = line.rstrip("\n")
                # 줄 끝의 `  # TODO ...` 마커를 잘라내고…
                marker_idx = body.find("  # TODO")
                if marker_idx != -1:
                    body = body[:marker_idx]
                # …줄 머리의 `# ` 주석 표식을 벗기고…
                s = body.lstrip()
                indent = body[: len(body) - len(s)]
                if s.startswith("#"):
                    s = s[1:].lstrip(" ")
                body = indent + s
                # …중화 placeholder 를 원 토큰으로 복원해 활성 `model:` 줄로 환원한다.
                body = body.replace("<provider/model>", OPENCODE_MODEL_TOKEN)
                new_lines.append(body + eol)
                changed_any = True
            else:
                new_lines.append(line)
        if changed_any:
            path.write_text("".join(new_lines), encoding="utf-8")
    return dest


def _copied_relpaths_of(dest):
    """dest 트리의 모든 파일 relpath set — resolve_opencode_model 의 copied_relpaths 인자용.

    main 통합이 아닌 함수 단위 테스트에서, 이미 import 된 트리 전체를 복사 범위로 본다
    (실제 main 은 actions 의 dst relpath 를 넘긴다 — 여기선 동치로 트리 전체).
    """
    out = set()
    for path in dest.rglob("*"):
        if path.is_file():
            out.add(path.relative_to(dest))
    return out


# ── DoD ①: --opencode-model 플래그 → 결정적 치환 ─────────────────────────────

def test_opencode_model_flag_substitutes(pm_import, tmp_path):
    """--opencode-model PROVIDER/MODEL 명시 → {{OPENCODE_PRO_MODEL}} 결정적 치환(조회 불필요)."""
    dest = _opencode_dest_with_token(pm_import, tmp_path, "FlagSub")
    relpaths = _copied_relpaths_of(dest)
    # 토큰이 잔존(혹은 import 가 TODO 표시했어도 토큰 자체는 보존)함을 전제 확인.
    assert pm_import._token_present(dest, OPENCODE_MODEL_TOKEN, relpaths)

    result = pm_import.resolve_opencode_model(
        dest, relpaths, model_arg="ollama/qwen3.6:27b",
        models_runner=_stub_models_runner(ok=False, models=[]),  # 조회 실패해도 플래그 우선.
        stdin=io.StringIO(""),
    )
    assert result.active is True
    assert result.path == "flag"
    assert result.model == "ollama/qwen3.6:27b"
    assert result.changed >= 1, "플래그 명시인데 치환 파일이 0."
    # 토큰이 모두 치환되고 명시 모델로 바뀌었다.
    assert not pm_import._token_present(dest, OPENCODE_MODEL_TOKEN, relpaths), \
        "플래그 치환 후에도 모델 토큰이 잔존."
    # 설치 모델 토큰을 담는 카드는 primary `pm.md` 다 — 역할 카드는 `delegate.<role>.model`
    #   파생이라 이 해소 경로의 대상이 아니다.
    pm_card = dest / ".opencode" / "agents" / "pm.md"
    assert "ollama/qwen3.6:27b" in pm_card.read_text(encoding="utf-8")


def test_opencode_model_flag_warns_when_not_in_available(pm_import, tmp_path, capsys):
    """플래그 모델이 `opencode models` 목록에 없어도 *경고만* 하고 사용자 의도대로 치환(사설 모델)."""
    dest = _opencode_dest_with_token(pm_import, tmp_path, "FlagWarn")
    relpaths = _copied_relpaths_of(dest)
    result = pm_import.resolve_opencode_model(
        dest, relpaths, model_arg="company/secret-model",
        models_runner=_stub_models_runner(ok=True, models=_FAKE_MODELS),
        stdin=io.StringIO(""),
    )
    assert result.path == "flag" and result.model == "company/secret-model"
    assert result.changed >= 1, "목록 밖이어도 사용자 의도 존중·치환해야 함."
    err = capsys.readouterr().err
    assert "가용 목록에 없습니다" in err, "목록 밖 플래그에 대한 경고가 없음."


# ── DoD ②: 대화형 선택 (models stub + stdin stub) → 선택 모델로 치환 ───────────

def test_opencode_model_interactive_selection(pm_import, tmp_path):
    """stdin tty + `opencode models` 조회 성공 → 번호목록·선택 입력 → 선택 모델로 치환."""
    dest = _opencode_dest_with_token(pm_import, tmp_path, "Interactive")
    relpaths = _copied_relpaths_of(dest)

    class _TtyStdin(io.StringIO):
        def isatty(self):
            return True

    # 2번 선택 = _FAKE_MODELS[1] = 'opencode/big-pickle'.
    stdin = _TtyStdin("2\n")
    result = pm_import.resolve_opencode_model(
        dest, relpaths, model_arg=None,
        models_runner=_stub_models_runner(ok=True, models=_FAKE_MODELS),
        stdin=stdin,
    )
    assert result.active is True
    assert result.path == "interactive"
    assert result.model == "opencode/big-pickle", "번호 선택이 잘못 매핑됨."
    assert result.changed >= 1
    pm_card = dest / ".opencode" / "agents" / "pm.md"
    assert "opencode/big-pickle" in pm_card.read_text(encoding="utf-8")
    assert not pm_import._token_present(dest, OPENCODE_MODEL_TOKEN, relpaths)


def test_opencode_model_interactive_empty_falls_back_to_todo(pm_import, tmp_path):
    """대화형에서 빈 입력(미선택) → 치환 안 함·TODO 폴백(블로킹 금지·안전)."""
    dest = _opencode_dest_with_token(pm_import, tmp_path, "InteractiveEmpty")
    relpaths = _copied_relpaths_of(dest)

    class _TtyStdin(io.StringIO):
        def isatty(self):
            return True

    result = pm_import.resolve_opencode_model(
        dest, relpaths, model_arg=None,
        models_runner=_stub_models_runner(ok=True, models=_FAKE_MODELS),
        stdin=_TtyStdin("\n"),  # 빈 입력 = 건너뜀.
    )
    assert result.path == "todo"
    assert result.model is None
    assert result.changed == 0
    assert OPENCODE_MODEL_TOKEN in result.todos


# ── DoD ③: 비-tty → 치환 안 함·TODO 마커(가용목록 인라인) 폴백 ──────────────────

def test_opencode_model_non_tty_todo_with_available_list(pm_import, tmp_path, capsys):
    """비-tty + 조회 성공 → 치환 안 함·TODO 마커에 가용 모델 목록 인라인 + stderr 경고."""
    dest = _opencode_dest_with_token(pm_import, tmp_path, "NonTty")
    relpaths = _copied_relpaths_of(dest)
    # 비-tty stdin(StringIO 기본 isatty=False).
    result = pm_import.resolve_opencode_model(
        dest, relpaths, model_arg=None,
        models_runner=_stub_models_runner(ok=True, models=_FAKE_MODELS),
        stdin=io.StringIO(""),
    )
    assert result.active is True
    assert result.path == "todo"
    assert result.model is None
    assert result.changed == 0
    assert OPENCODE_MODEL_TOKEN in result.todos
    pm_card = dest / ".opencode" / "agents" / "pm.md"
    dev_text = pm_card.read_text(encoding="utf-8")
    # T-0133: TODO 폴백(모델 미해소)은 model: 줄을 주석화하며 토큰을 <provider/model> 로 *중화*한다
    # (실제 모델로 치환=채움이 아님). model 파일엔 리터럴 토큰이 남지 않는다(@render leak 회피) — 발견
    # 경로는 주석 model 줄 + 형식 힌트 + 가용목록 TODO 로 보존. (whole-tree 토큰 존재는 README 산문에서
    # 별도 검증되므로 여기선 model 줄 동작만 본다.)
    assert OPENCODE_MODEL_TOKEN not in dev_text, "TODO 폴백인데 model 파일에 리터럴 토큰 잔존(@render leak)."
    assert "<provider/model>" in dev_text, "TODO 폴백 형식 힌트(<provider/model>) 소실."
    assert "TODO" in dev_text, "비-tty 폴백인데 TODO 마커가 없음."
    assert "ollama/gemma4:26b" in dev_text, "TODO 마커에 가용 모델 목록이 인라인되지 않음."
    err = capsys.readouterr().err
    assert "미치환" in err, "비-tty 폴백 stderr 경고가 없음."


# ── DoD ④: opencode 바이너리 부재(조회 실패) → TODO 폴백(목록 없음) ────────────

def test_opencode_model_binary_absent_todo_fallback(pm_import, tmp_path, capsys):
    """opencode 바이너리 부재(조회 (False, [])) → 치환 안 함·일반 TODO 마커 + stderr 경고."""
    dest = _opencode_dest_with_token(pm_import, tmp_path, "BinAbsent")
    relpaths = _copied_relpaths_of(dest)
    # 바이너리 부재 = runner 가 (False, []) 반환(_real_models_runner 의 which 부재 동치).
    result = pm_import.resolve_opencode_model(
        dest, relpaths, model_arg=None,
        models_runner=_stub_models_runner(ok=False, models=[]),
        stdin=io.StringIO(""),
    )
    assert result.path == "todo"
    assert result.changed == 0
    assert result.available == [], "조회 실패인데 가용 목록이 비어있지 않음."
    dev_text = (dest / ".opencode" / "agents" / "pm.md").read_text(encoding="utf-8")
    # T-0133: TODO 폴백은 model: 줄 토큰을 <provider/model> 로 중화 — model 파일에 리터럴 토큰 0(@render leak 회피).
    assert OPENCODE_MODEL_TOKEN not in dev_text, "TODO 폴백인데 model 파일에 리터럴 토큰 잔존(@render leak)."
    assert "TODO" in dev_text
    # 목록 없으니 일반 TODO 안내(가용 목록 인라인 아님)·형식 힌트 <provider/model> 보존.
    assert "provider/model" in dev_text and "가용:" not in dev_text
    # T-0077: model 줄은 통째 주석화돼야 한다(`# model:` — 값 비활성 → opencode 기본 모델).
    assert re.search(r"^#\s*model:", dev_text, re.MULTILINE), \
        "바이너리 부재 폴백인데 model 줄이 주석화되지 않음(깨진 agent)."
    err = capsys.readouterr().err
    assert "미치환" in err


def test_opencode_model_render_load_failure_fails_loud(pm_import, tmp_path, monkeypatch):
    """렌더러(pm_render) 로드 실패 시 폴백은 조용히 활성 토큰을 출하하지 않고 fail-loud (T-0310 codex must-fix).

    회귀 방지: T-0310 이 줄-중화 로직을 pm_render.neutralize_model_todo 로 추출하며 import 폴백
    (`_mark_model_todos`)이 pm_render 로드에 *의존*하게 됐다. 로드 실패 시 조용히 `[]` 를 반환하면
    (초기 리팩터 동작) `model: "{{OPENCODE_PRO_MODEL}}"` 이 활성 상태로 출하돼 opencode 가 agent 를
    거부한다(T-0077). 이 폴백 계약은 "미해소 model: 줄을 *반드시* 중화" 이므로, 중화 못 하면 broken
    install 신호로 크게 터뜨려야 한다(silent-degrade 근절·robustness 값-연결 assert). pm_render 는
    co-located 엔진이라 정상 설치에선 절대 미발화 — 이 테스트는 로드 실패를 monkeypatch 로 강제한다.
    """
    dest = _opencode_dest_with_token(pm_import, tmp_path, "RenderLoadFail")
    relpaths = _copied_relpaths_of(dest)
    # pm_render 로드 실패 시뮬레이션(co-located 엔진 부재/손상 = broken install).
    monkeypatch.setattr(pm_import, "_load_pm_render_module", lambda: None)
    # 비-tty·바이너리 부재 → 폴백(_mark_model_todos) 경로 → 로드 실패로 raise.
    with pytest.raises(RuntimeError, match="pm_render"):
        pm_import.resolve_opencode_model(
            dest, relpaths, model_arg=None,
            models_runner=_stub_models_runner(ok=False, models=[]),
            stdin=io.StringIO(""),
        )


def test_opencode_model_fallback_only_comments_model_field_not_prose(pm_import, tmp_path):
    """폴백 주석화는 agent 의 `model:` 필드 줄만 — 엔진 사본 docstring 산문의 토큰은 안 건드린다(T-0077 PM 게이트).

    opencode 트리에도 그대로 복사되는 `.project_manager/tools/pm_import.py` 자신의 docstring 은
    placeholder 를 *문서화* 하는 산문(`{{OPENCODE_PRO_MODEL}} 가 import 때 파일에 직접 치환되지만…`)에
    토큰을 담는다(T-0192 #6 전 README 예시를 실 출하 파일로 repoint). 마커가 `토큰 in line` 만 보고
    `# ` prepend 하면 그 산문 줄이 깨진다 → 마커는 `model:` 필드 줄로 한정해야 한다.
    """
    dest = tmp_path / "readmesafe"
    rc = pm_import.main(["--new", str(dest), "--harness", "opencode", "--name", "ReadmeSafe"])
    assert rc == 0
    engine_copy = (dest / ".project_manager" / "tools" / "pm_import.py").read_text(encoding="utf-8")
    # 엔진 사본은 폴백 후에도 placeholder 토큰을 *문서화* 형태로 보존(치환/주석화 안 됨).
    assert OPENCODE_MODEL_TOKEN in engine_copy
    # 폴백 TODO 마커는 `model:` 필드 줄에만 — docstring 산문 줄은 그 필드가 아니므로 마커 0(산문 무손상).
    prose_line = "{{OPENCODE_PRO_MODEL}} 가 import 때 파일에 직접 치환되지만"
    assert any(prose_line in line for line in engine_copy.splitlines()), \
        "docstring 산문 줄 전제가 깨짐 — pm_import.py record_opencode_model 문구 확인 필요."
    for line in engine_copy.splitlines():
        if OPENCODE_MODEL_TOKEN in line and not line.lstrip().startswith("model:"):
            assert "# TODO: opencode 모델 ID" not in line, f"docstring 산문 줄에 폴백 마커가 붙어 깨짐: {line!r}"
            assert not line.lstrip().startswith("# TODO"), f"docstring 산문 줄이 마커로 오염됨: {line!r}"


def test_opencode_agent_frontmatter_valid_after_default_import(pm_import, tmp_path):
    """기본(--opencode-model 없는·비-tty) opencode import 후 agent frontmatter 가 유효한 YAML.

    T-0077: 미해소 폴백은 `model:` 줄을 *통째 주석화*한다 — frontmatter 에 `model` 키가 *부재*해야
    opencode 가 "configured model … is not valid" 로 agent 를 거부하지 않고 *기본 모델*로 띄운다
    (graceful·실 파일럿 블로커 fix). 주석은 반드시 YAML 주석(`#`)이어야 하며 (HTML 주석 `<!-- -->` 은
    frontmatter 파싱을 깬다 — T-0033 codex must-fix).

    T-0133(@render 활성화): `.opencode/agents` 가 render 대상이 되면서, 폴백 주석 줄에 리터럴
    `{{OPENCODE_PRO_MODEL}}` 을 남기면 render `_assert_no_leak` 가 hard-fail 한다. 그래서 폴백은
    토큰을 형식 힌트 `<provider/model>` 로 *중화* 하되 주석 `model:` 줄 + TODO 안내는 보존한다 —
    채택자 발견경로(주석 해제 후 provider/model 로 치환·`--opencode-model` 재import)는 유지하고,
    리터럴 토큰만 제거(이전 "토큰 보존" 계약을 활성화가 강제 변경). main 의 기본 경로(autouse fixture
    가 _real_models_runner 를 (False, []) 로 고정 → 폴백)로 import 한 subagent frontmatter 가드.

    카드마다 미해소 사유가 다르다 — primary `pm.md` 는 설치 모델(`{{OPENCODE_PRO_MODEL}}`),
    역할 카드는 위임 모델(`delegate.<role>.model`)이다. 둘 다 같은 중화 규약(주석 `model:` 줄 +
    형식 힌트 + TODO 안내)을 타되 형식 힌트와 안내가 그 사유를 가리켜야 한다.
    """
    dest = tmp_path / "fmvalid"
    rc = pm_import.main(["--new", str(dest), "--harness", "opencode", "--name", "FmValid"])
    assert rc == 0
    agents_dir = dest / ".opencode" / "agents"
    hints = {
        "pm.md": ("<provider/model>", OPENCODE_MODEL_TOKEN),
        "developer.md": ("<model>", "{{DELEGATE_MODEL_DEVELOPER}}"),
        "code-reviewer.md": ("<model>", "{{DELEGATE_MODEL_CODE_REVIEWER}}"),
        "architect.md": ("<model>", "{{DELEGATE_MODEL_ARCHITECT}}"),
    }
    for name, (hint, token) in hints.items():
        text = (agents_dir / name).read_text(encoding="utf-8")
        assert text.startswith("---\n"), f"{name}: frontmatter 시작 구분자 없음"
        end = text.find("\n---\n", 4)
        assert end != -1, f"{name}: frontmatter 종료 구분자 없음"
        fm = yaml.safe_load(text[4:end])  # 깨지면 YAMLError → 테스트 실패(회귀 포착)
        # 미해소 폴백 = model 키 *부재*(=opencode 기본 모델로 graceful 구동·깨진 agent 0).
        assert "model" not in fm, (
            f"{name}: 미해소 폴백인데 model 키가 활성(부재여야 opencode 기본 모델): {fm.get('model')!r}"
        )
        # T-0133: @render leak-safety — 리터럴 토큰은 *없어야* 한다(render _assert_no_leak hard-fail 회피).
        assert token not in text, \
            f"{name}: @render 경로 agent 에 리터럴 모델 토큰 잔존 → render leak"
        # 그래도 발견경로는 보존: 주석 model: 줄 + 사유에 맞는 형식 힌트 + TODO 안내.
        assert hint in text, f"{name}: 폴백 형식 힌트({hint}) 소실"
        assert re.search(r"^#\s*model:", text[: end + 5], re.MULTILINE), \
            f"{name}: model 줄이 `# model:` 로 주석화되지 않음"
        # frontmatter 영역에 HTML 주석 잔류 0 (YAML 깨짐 방지 — T-0033 codex must-fix 회귀 가드).
        assert "<!--" not in text[: end + 5], f"{name}: frontmatter 에 HTML 주석 잔류"
    # 역할 카드의 TODO 는 채울 conf 키를 지목한다(설치 모델 안내와 사유가 갈린다).
    dev_text = (agents_dir / "developer.md").read_text(encoding="utf-8")
    assert "delegate.developer.model=" in dev_text, \
        "역할 카드 TODO 가 채울 local.conf 키를 지목하지 않는다"


def test_resolve_inactive_when_token_absent(pm_import, tmp_path):
    """claude-only 트리(모델 토큰 미잔존) → 해소 단계 inactive(아무 것도 안 함)."""
    dest = tmp_path / "claudeonly"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "ClaudeOnly"])
    assert rc == 0
    relpaths = _copied_relpaths_of(dest)
    assert not pm_import._token_present(dest, OPENCODE_MODEL_TOKEN, relpaths)
    result = pm_import.resolve_opencode_model(
        dest, relpaths, model_arg="ollama/x",
        models_runner=_stub_models_runner(),
        stdin=io.StringIO(""),
    )
    assert result.active is False
    assert result.path == "inactive"
    assert result.changed == 0


# ── change4 (codex): 해소된 모델을 local.conf opencode_pro_model 로 기록 ──────────
#   pm_update @render 가 {{OPENCODE_PRO_MODEL}} 을 local.conf 에서 재유도(_LOCAL_CONF_TO_
#   OPERATIONAL["opencode_pro_model"])할 때 키 부재면 leak assertion crash. flag/interactive
#   해소 경로만 기록, todo(미해소·토큰이 YAML 주석)·claude(inactive)는 미기록.

def test_import_flag_records_opencode_model_in_local_conf(pm_import, tmp_path):
    """opencode import + --opencode-model(flag 해소) → local.conf 에 opencode_pro_model 기록."""
    dest = tmp_path / "modelconf"
    rc = pm_import.main(["--new", str(dest), "--harness", "opencode", "--name", "ModelConf",
                         "--opencode-model", "ollama/qwen3.6:27b"])
    assert rc == 0
    conf = _parse_conf(dest / ".project_manager" / "local.conf")
    assert conf.get("opencode_pro_model") == "ollama/qwen3.6:27b", \
        f"flag 해소인데 local.conf opencode_pro_model 부재/불일치: {conf.get('opencode_pro_model')!r}"


def test_import_flag_model_preserves_other_local_conf_keys(pm_import, tmp_path):
    """opencode_pro_model 기록이 board init·sync 가 쓴 다른 키(project_name·upstream)·주석을 보존."""
    dest = tmp_path / "modelpreserve"
    rc = pm_import.main(["--new", str(dest), "--harness", "opencode", "--name", "Keep It",
                         "--opencode-model", "ollama/qwen3.6:27b"])
    assert rc == 0
    local_conf = dest / ".project_manager" / "local.conf"
    conf = _parse_conf(local_conf)
    assert conf.get("opencode_pro_model") == "ollama/qwen3.6:27b"
    assert conf.get("project_name") == "Keep It", "모델 기록이 project_name 을 덮음."
    assert "upstream" in conf, "모델 기록이 upstream 키를 잃음."
    assert local_conf.read_text(encoding="utf-8").lstrip().startswith("#"), \
        "모델 기록이 local.conf 머리 주석을 지움."


def test_import_todo_does_not_record_opencode_model(pm_import, tmp_path):
    """opencode import + 플래그 없음(비-tty → todo 폴백·미해소) → opencode_pro_model 미기록.

    토큰이 YAML 주석(`# model: …`)으로 남아 @render leak 이 없으므로 local.conf 기록도 안 한다.
    (autouse _hermetic_opencode_models fixture 가 _real_models_runner 를 (False, []) 로 고정 →
    todo 경로.)
    """
    dest = tmp_path / "modeltodo"
    rc = pm_import.main(["--new", str(dest), "--harness", "opencode", "--name", "ModelTodo"])
    assert rc == 0
    conf = _parse_conf(dest / ".project_manager" / "local.conf")
    assert "opencode_pro_model" not in conf, \
        f"todo(미해소)인데 opencode_pro_model 이 기록됨: {conf.get('opencode_pro_model')!r}"


def test_import_claude_does_not_record_opencode_model(pm_import, tmp_path):
    """claude-only import(모델 토큰 미잔존·inactive) → opencode_pro_model 미기록."""
    dest = tmp_path / "modelclaude"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "ModelClaude"])
    assert rc == 0
    conf = _parse_conf(dest / ".project_manager" / "local.conf")
    assert "opencode_pro_model" not in conf, \
        "claude import 인데 opencode_pro_model 이 기록됨."


def test_record_opencode_model_set_or_replace_unit(pm_import, tmp_path):
    """record_opencode_model: 기존 키 제자리 교체, 없으면 추가. 다른 키·주석 보존."""
    local_conf = tmp_path / ".project_manager" / "local.conf"
    local_conf.parent.mkdir(parents=True)
    local_conf.write_text(
        "# header\nproject_name=Keep\nsession=pm\n", encoding="utf-8")
    # 신규 추가.
    assert pm_import.record_opencode_model(tmp_path, "ollama/a") is True
    conf = _parse_conf(local_conf)
    assert conf["opencode_pro_model"] == "ollama/a"
    assert conf["project_name"] == "Keep" and conf["session"] == "pm"
    assert local_conf.read_text(encoding="utf-8").startswith("# header"), "머리 주석 손실."
    # 제자리 교체(중복 줄 미생성).
    assert pm_import.record_opencode_model(tmp_path, "ollama/b") is True
    text = local_conf.read_text(encoding="utf-8")
    assert text.count("opencode_pro_model=") == 1, "교체 대신 중복 줄 생성."
    assert _parse_conf(local_conf)["opencode_pro_model"] == "ollama/b"
    # 동일값 재기록 → 변경 없음(False).
    assert pm_import.record_opencode_model(tmp_path, "ollama/b") is False


def test_record_opencode_model_graceful_when_conf_absent(pm_import, tmp_path):
    """record_opencode_model: local.conf 부재면 graceful skip(False·예외 없음)."""
    assert pm_import.record_opencode_model(tmp_path, "ollama/x") is False


# ── DoD ⑤: dry-run 계획 — 경로·플래그값·tty 여부만 출력, 파일변경·실호출 0 ───────

def test_dry_run_opencode_model_plan_flag(pm_import, tmp_path, capsys):
    """--dry-run + opencode + --opencode-model: flag 경로 계획 출력, 파일·`opencode models` 0."""
    dest = tmp_path / "dryflag"
    rc = pm_import.main(["--new", str(dest), "--harness", "opencode", "--name", "DryFlag",
                         "--opencode-model", "ollama/qwen3.6:27b", "--dry-run"])
    assert rc == 0
    assert not dest.exists(), "--dry-run 인데 대상 디렉토리가 생성됨."
    out = capsys.readouterr().out
    assert "opencode 모델 해소 계획" in out, "dry-run 모델 해소 계획이 없음."
    assert "경로: flag" in out, "플래그 명시인데 flag 경로 계획이 아님."
    assert "ollama/qwen3.6:27b" in out, "dry-run 계획에 플래그값이 안 나옴."
    assert "stdin tty:" in out, "dry-run 계획에 tty 여부가 없음."


def test_dry_run_opencode_model_plan_non_tty_todo(pm_import, tmp_path, capsys):
    """--dry-run + opencode + 플래그 없음 + 비-tty → todo 경로 계획(파일·실호출 0)."""
    dest = tmp_path / "dryplan"
    rc = pm_import.main(["--new", str(dest), "--harness", "opencode", "--name", "DryPlan",
                         "--dry-run"])
    assert rc == 0
    assert not dest.exists()
    out = capsys.readouterr().out
    assert "opencode 모델 해소 계획" in out
    # 테스트 stdin 은 비-tty → todo 경로.
    assert "경로: todo" in out, "비-tty(플래그 없음) 인데 todo 경로 계획이 아님."


def test_dry_run_claude_only_no_model_plan(pm_import, tmp_path, capsys):
    """--dry-run + claude-only: 모델 토큰이 없으니 모델 해소 계획이 출력되지 않는다."""
    dest = tmp_path / "dryclaude"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "DryClaude",
                         "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "opencode 모델 해소 계획" not in out, \
        "claude-only 인데 opencode 모델 해소 계획이 출력됨."


# ── seam·파싱 단위 ───────────────────────────────────────────────────────────

def test_parse_opencode_models_filters_blanks_and_banner(pm_import):
    """_parse_opencode_models: 빈 줄·슬래시 없는 배너 줄 제외, provider/model 만 추출."""
    raw = (
        "사용 가능한 모델:\n"      # 배너(슬래시 없음) → 제외.
        "\n"                       # 빈 줄 → 제외.
        "ollama/gemma4:26b\n"
        "  opencode/big-pickle  \n"  # 앞뒤 공백 strip.
        "anthropic/claude-x\n"
    )
    models = pm_import._parse_opencode_models(raw)
    assert models == ["ollama/gemma4:26b", "opencode/big-pickle", "anthropic/claude-x"]


def test_prompt_model_choice_out_of_range_returns_none(pm_import):
    """_prompt_model_choice: 범위 밖·비숫자·빈 입력 → None(미선택 → TODO 폴백)."""
    models = ["a/b", "c/d"]
    assert pm_import._prompt_model_choice(models, io.StringIO("9\n")) is None
    assert pm_import._prompt_model_choice(models, io.StringIO("xyz\n")) is None
    assert pm_import._prompt_model_choice(models, io.StringIO("\n")) is None
    assert pm_import._prompt_model_choice(models, io.StringIO("1\n")) == "a/b"


def test_substitute_model_token_scoped_to_copied(pm_import, tmp_path):
    """_substitute_model_token: copied_relpaths 밖 파일은 치환하지 않는다(비파괴)."""
    dest = tmp_path / "scope"
    (dest / "sub").mkdir(parents=True)
    copied = dest / "sub" / "agent.md"
    copied.write_text('model: "{{OPENCODE_PRO_MODEL}}"\n', encoding="utf-8")
    outside = dest / "outside.md"
    outside.write_text('model: "{{OPENCODE_PRO_MODEL}}"\n', encoding="utf-8")
    changed = pm_import._substitute_model_token(
        dest, "ollama/x", {Path("sub/agent.md")})
    assert changed == 1
    assert "ollama/x" in copied.read_text(encoding="utf-8")
    # 범위 밖 파일은 토큰이 그대로 보존(비파괴).
    assert OPENCODE_MODEL_TOKEN in outside.read_text(encoding="utf-8"), \
        "_substitute_model_token 이 copied_relpaths 밖 파일을 치환함(비파괴 위반)."


def test_real_models_runner_no_binary_fail_soft(pm_import, monkeypatch):
    """_real_models_runner: opencode 바이너리 부재(which None) → (False, []) fail-soft.

    subprocess 도 안 띄운다(라이브 CLI 미실행 보장) — which 가 None 이면 즉시 폴백.
    """
    monkeypatch.setattr(pm_import.shutil, "which", lambda b: None)
    monkeypatch.setattr(
        pm_import.subprocess, "run",
        lambda *a, **k: pytest.fail("which None 인데 subprocess 가 호출됨 — 라이브 CLI 위험."),
    )
    ok, models = pm_import._real_models_runner()
    assert ok is False and models == []


# ── T-0127: _real_models_runner 실패 사유 stderr surface (침묵 무력화 해소) ──────
# fail-soft 는 유지(반환 계약 불변)하되 *왜* 실패했는지 stderr 로 1줄 surface. monkeypatch 로
# subprocess.run 에 가짜 result/예외를 주입하고 capsys 로 stderr 를 캡처해 사유 출력을 단언한다.

class _FakeResult:
    """subprocess.run 반환 모사 — _real_models_runner 가 보는 필드만(returncode/stdout/stderr)."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_real_models_runner_no_binary_surfaces_reason(pm_import, monkeypatch, capsys):
    """which None → (False, []) 유지 + stderr 에 PATH 부재 사유 surface."""
    monkeypatch.setattr(pm_import.shutil, "which", lambda b: None)
    ok, models = pm_import._real_models_runner()
    assert ok is False and models == []
    err = capsys.readouterr().err
    assert "PATH 부재" in err


def test_real_models_runner_nonzero_rc_surfaces_reason(pm_import, monkeypatch, capsys):
    """rc≠0 → (False, []) 유지 + stderr 에 rc + stderr 앞부분 surface."""
    monkeypatch.setattr(pm_import.shutil, "which", lambda b: "/usr/bin/opencode")
    monkeypatch.setattr(
        pm_import.subprocess, "run",
        lambda *a, **k: _FakeResult(returncode=2, stdout="", stderr="boom failure detail"),
    )
    ok, models = pm_import._real_models_runner()
    assert ok is False and models == []
    err = capsys.readouterr().err
    assert "rc=2" in err
    assert "boom failure detail" in err


def test_real_models_runner_nonzero_rc_truncates_stderr(pm_import, monkeypatch, capsys):
    """rc≠0 의 stderr 는 앞 200자까지만 surface(로그 폭증 방지)."""
    monkeypatch.setattr(pm_import.shutil, "which", lambda b: "/usr/bin/opencode")
    long_detail = "x" * 500
    monkeypatch.setattr(
        pm_import.subprocess, "run",
        lambda *a, **k: _FakeResult(returncode=1, stdout="", stderr=long_detail),
    )
    pm_import._real_models_runner()
    err = capsys.readouterr().err
    assert "x" * 200 in err
    assert "x" * 201 not in err


def test_real_models_runner_timeout_surfaces_reason(pm_import, monkeypatch, capsys):
    """TimeoutExpired → (False, []) 유지 + stderr 에 timeout 값 + env override 안내 surface."""
    monkeypatch.setattr(pm_import.shutil, "which", lambda b: "/usr/bin/opencode")
    monkeypatch.delenv("PM_OPENCODE_MODELS_TIMEOUT", raising=False)

    def _raise_timeout(*a, **k):
        raise pm_import.subprocess.TimeoutExpired(cmd="opencode models", timeout=60)

    monkeypatch.setattr(pm_import.subprocess, "run", _raise_timeout)
    ok, models = pm_import._real_models_runner()
    assert ok is False and models == []
    err = capsys.readouterr().err
    assert "60s timeout 초과" in err
    assert "PM_OPENCODE_MODELS_TIMEOUT" in err


def test_real_models_runner_exception_surfaces_reason(pm_import, monkeypatch, capsys):
    """기타 예외 → (False, []) 유지 + stderr 에 예외 메시지 surface(import 안 깸)."""
    monkeypatch.setattr(pm_import.shutil, "which", lambda b: "/usr/bin/opencode")

    def _raise(*a, **k):
        raise RuntimeError("unexpected explosion")

    monkeypatch.setattr(pm_import.subprocess, "run", _raise)
    ok, models = pm_import._real_models_runner()
    assert ok is False and models == []
    err = capsys.readouterr().err
    assert "예외" in err
    assert "unexpected explosion" in err


def test_real_models_runner_parse_zero_surfaces_reason(pm_import, monkeypatch, capsys):
    """rc=0 이나 파싱 0개 → (True, []) 유지(호출부가 TODO 폴백) + stderr 에 형식 확인 안내 surface."""
    monkeypatch.setattr(pm_import.shutil, "which", lambda b: "/usr/bin/opencode")
    # 슬래시 없는 배너만 → _parse_opencode_models 가 빈 리스트.
    monkeypatch.setattr(
        pm_import.subprocess, "run",
        lambda *a, **k: _FakeResult(returncode=0, stdout="banner line\nno slash here\n"),
    )
    ok, models = pm_import._real_models_runner()
    assert ok is True and models == []
    err = capsys.readouterr().err
    assert "모델 0개 파싱" in err


def test_real_models_runner_success_no_reason(pm_import, monkeypatch, capsys):
    """정상(rc=0·모델 N개) → (True, [모델]) + stderr 무음(반환 계약·무사유 확인)."""
    monkeypatch.setattr(pm_import.shutil, "which", lambda b: "/usr/bin/opencode")
    monkeypatch.setattr(
        pm_import.subprocess, "run",
        lambda *a, **k: _FakeResult(
            returncode=0, stdout="ollama/gemma4:26b\nopencode/big-pickle\n"
        ),
    )
    ok, models = pm_import._real_models_runner()
    assert ok is True
    assert models == ["ollama/gemma4:26b", "opencode/big-pickle"]
    assert capsys.readouterr().err == ""


# ── T-0127: _opencode_models_timeout env override (T-0070 PM_SUBMODULE_TIMEOUT 동형) ──

def test_opencode_models_timeout_default_when_unset(pm_import, monkeypatch):
    """env 미설정 → 기본 60."""
    monkeypatch.delenv("PM_OPENCODE_MODELS_TIMEOUT", raising=False)
    assert pm_import._opencode_models_timeout() == 60


def test_opencode_models_timeout_env_override(pm_import, monkeypatch):
    """PM_OPENCODE_MODELS_TIMEOUT=120 → 120(양의 정수 채택)."""
    monkeypatch.setenv("PM_OPENCODE_MODELS_TIMEOUT", "120")
    assert pm_import._opencode_models_timeout() == 120


def test_opencode_models_timeout_strips_whitespace(pm_import, monkeypatch):
    """env 값 앞뒤 공백 strip 후 int 파싱."""
    monkeypatch.setenv("PM_OPENCODE_MODELS_TIMEOUT", "  90  ")
    assert pm_import._opencode_models_timeout() == 90


def test_opencode_models_timeout_non_numeric_falls_back(pm_import, monkeypatch):
    """비숫자 env → 기본 60 폴백(무해)."""
    monkeypatch.setenv("PM_OPENCODE_MODELS_TIMEOUT", "soon")
    assert pm_import._opencode_models_timeout() == 60


def test_opencode_models_timeout_non_positive_falls_back(pm_import, monkeypatch):
    """≤0 env(0·음수) → 기본 60 폴백(무제한 두지 않음 — 빠른 로컬 조회 가정)."""
    monkeypatch.setenv("PM_OPENCODE_MODELS_TIMEOUT", "0")
    assert pm_import._opencode_models_timeout() == 60
    monkeypatch.setenv("PM_OPENCODE_MODELS_TIMEOUT", "-5")
    assert pm_import._opencode_models_timeout() == 60


def test_real_models_runner_uses_resolved_timeout(pm_import, monkeypatch):
    """_real_models_runner 가 subprocess.run 의 timeout= 으로 _opencode_models_timeout() 값을 쓴다."""
    monkeypatch.setattr(pm_import.shutil, "which", lambda b: "/usr/bin/opencode")
    monkeypatch.setenv("PM_OPENCODE_MODELS_TIMEOUT", "200")
    seen = {}

    def _capture(*a, **k):
        seen["timeout"] = k.get("timeout")
        return _FakeResult(returncode=0, stdout="ollama/gemma4:26b\n")

    monkeypatch.setattr(pm_import.subprocess, "run", _capture)
    pm_import._real_models_runner()
    assert seen["timeout"] == 200


def test_main_opencode_flag_end_to_end(pm_import, tmp_path):
    """main --opencode-model: 통합 경로에서 모델 토큰이 명시값으로 치환된다(실 import)."""
    dest = tmp_path / "mainflag"
    rc = pm_import.main(["--new", str(dest), "--harness", "opencode", "--name", "MainFlag",
                         "--opencode-model", "ollama/qwen3.6:27b"])
    assert rc == 0
    relpaths = _copied_relpaths_of(dest)
    assert not pm_import._token_present(dest, OPENCODE_MODEL_TOKEN, relpaths), \
        "main --opencode-model 인데 토큰이 잔존."
    pm_card = dest / ".opencode" / "agents" / "pm.md"
    assert "ollama/qwen3.6:27b" in pm_card.read_text(encoding="utf-8")


# ── T-0034: --into 백업 — 파일별 git-인지 skip + 중앙화 디렉토리 ───────────────
# git 판정은 LLM 아님·결정적 — git_runner / git_safe 주입으로 라이브 git 없이 단위 검증한다
# (_real_models_runner 류 seam 철학). 통합 케이스 1개만 실 git init 으로 e2e 확인.

import shutil as _shutil_for_git  # noqa: E402 — 실 git 가용 여부 게이트(통합 케이스).

requires_git = pytest.mark.skipif(
    _shutil_for_git.which("git") is None,
    reason="git 바이너리 부재 — 실 git 통합 케이스 skip(단위 seam 테스트는 항상 실행).",
)


# ── ① git 추적&clean → 백업 0·덮기 (git-safe skip) ──────────────────────────

def test_git_safe_tracked_clean_skips_backup(pm_import, tmp_path):
    """git 이 추적 중이고 미변경인 충돌 파일은 백업 없이 덮는다(git 이 복원). plan_copy 단위."""
    dest = tmp_path / "gitclean"
    dest.mkdir()
    (dest / "CLAUDE.md").write_text("tracked clean\n", encoding="utf-8")
    template_roots = pm_import.resolve_template_roots(REPO, "claude")
    backup_root = dest / pm_import.BACKUP_DIR_NAME / "2026-01-01"
    # CLAUDE.md 가 추적&미변경(safe 집합에 포함) → 백업 생략.
    git_safe = {"CLAUDE.md"}
    actions = pm_import.plan_copy(template_roots, dest, backup_root, git_safe=git_safe)
    claude = next(a for a in actions if a.dst == dest / "CLAUDE.md")
    assert claude.backup is None, "git-safe(추적&미변경) 파일이 백업됨 — skip 미동작."
    assert claude._git_safe_skip is True, "git-safe skip 플래그가 표시되지 않음."
    assert "[copy · git-safe]" in claude.describe()


# ── ② git 추적&dirty → 중앙 백업 ─────────────────────────────────────────────

def test_git_dirty_file_gets_central_backup(pm_import, tmp_path):
    """git 이 추적 중이지만 dirty(미커밋 변경)인 충돌 파일은 중앙 디렉토리에 백업한다."""
    dest = tmp_path / "gitdirty"
    dest.mkdir()
    (dest / "CLAUDE.md").write_text("tracked dirty\n", encoding="utf-8")
    template_roots = pm_import.resolve_template_roots(REPO, "claude")
    backup_root = dest / pm_import.BACKUP_DIR_NAME / "2026-01-01"
    # git_safe 집합에 CLAUDE.md 가 *없다* → dirty/미추적 동치 → 중앙 백업.
    git_safe = {"README.md"}  # CLAUDE.md 는 미포함.
    actions = pm_import.plan_copy(template_roots, dest, backup_root, git_safe=git_safe)
    claude = next(a for a in actions if a.dst == dest / "CLAUDE.md")
    assert claude.backup == backup_root / Path("CLAUDE.md"), "dirty 파일이 중앙 백업되지 않음."
    assert claude._git_safe_skip is False
    assert "[backup+copy]" in claude.describe()
    assert pm_import.BACKUP_DIR_NAME in claude.describe()


# ── ③ git 미추적 → 중앙 백업 (git_safe_relpaths seam 통해) ────────────────────

def test_git_untracked_excluded_from_safe(pm_import, tmp_path):
    """git_safe_relpaths: 미추적(??) 파일은 추적집합에 없어 safe 에서 제외된다(중앙 백업 대상)."""
    # tracked = {a.md}; status: untracked.md 는 ?? → safe = 추적집합 − dirty = {a.md} 만.
    def runner(argv):
        if argv[:2] == ["rev-parse", "--is-inside-work-tree"]:
            return 0, "true\n"
        if argv[:2] == ["rev-parse", "--show-prefix"]:
            return 0, "\n"  # repo 루트 = 빈 prefix.
        if argv[:1] == ["ls-files"]:
            return 0, "a.md\0"
        if argv[:1] == ["status"]:
            return 0, "?? untracked.md\0"
        return 1, ""
    safe = pm_import.git_safe_relpaths(tmp_path, git_runner=runner)
    assert safe == {"a.md"}, f"미추적 파일이 safe 에 끼었거나 추적 clean 이 빠짐: {safe}"


def test_git_safe_relpaths_subdir_prefix_normalizes_dirty(pm_import, tmp_path):
    """하위 디렉토리 dest: ls-files(cwd 상대)와 status(repo-root 상대) 기준 차이를 --show-prefix 로
    정규화해 dirty 가 git-safe 에서 빠진다 (codex T-0034 must-fix·비파괴).

    prefix='sub/deep/' 일 때 status 의 'sub/deep/a.md'(repo-root 상대)를 'a.md'(dest 상대)로 환산해
    ls-files 의 'a.md' 와 같은 기준으로 빼야 한다. 정규화 없으면 a.md(dirty)가 safe 에 잘못 남아
    무백업 덮인다.
    """
    def runner(argv):
        if argv[:2] == ["rev-parse", "--is-inside-work-tree"]:
            return 0, "true\n"
        if argv[:2] == ["rev-parse", "--show-prefix"]:
            return 0, "sub/deep/\n"               # dest 는 repo 의 sub/deep 하위.
        if argv[:1] == ["ls-files"]:
            return 0, "a.md\0b.md\0"              # cwd(dest) 상대
        if argv[:1] == ["status"]:
            return 0, " M sub/deep/a.md\0"        # repo-root 상대 — a.md 가 dirty
        return 1, ""
    safe = pm_import.git_safe_relpaths(tmp_path, git_runner=runner)
    assert safe == {"b.md"}, f"dirty a.md 가 prefix 정규화로 제외되지 않음(무백업 덮임 위험): {safe}"


# ── ④ 비-git 대상 → 전부 중앙 백업 (형제 백업 0) ─────────────────────────────

def test_non_git_target_all_central_backup_no_siblings(pm_import, tmp_path):
    """비-git 대상(git_safe None)이면 모든 충돌을 중앙 디렉토리에 백업하고 형제 백업은 0."""
    dest = tmp_path / "nongit"
    dest.mkdir()
    (dest / "CLAUDE.md").write_text("user content\n", encoding="utf-8")
    (dest / "pm-config.sh").write_text("#!/bin/sh\necho user script\n", encoding="utf-8")
    today = datetime.date.today().isoformat()
    # 실 import — tmp 디렉토리는 git repo 가 아니므로 git_safe_relpaths → None → 전부 백업.
    rc = pm_import.main(["--into", str(dest), "--harness", "claude", "--name", "NonGit"])
    assert rc == 0
    backup_root = dest / pm_import.BACKUP_DIR_NAME / today
    assert (backup_root / "CLAUDE.md").is_file(), "비-git 충돌 CLAUDE.md 가 중앙 백업되지 않음."
    assert (backup_root / "pm-config.sh").is_file(), "비-git 충돌 pm-config.sh 가 중앙 백업되지 않음."
    # 형제 백업(트리 전역 분산)은 0 — 중앙화 계약.
    siblings = list(dest.rglob("*.backup.*"))
    assert siblings == [], f"형제 *.backup.<DATE> 잔존 — 중앙화 위반: {siblings}"


def test_git_safe_relpaths_non_git_returns_none(pm_import, tmp_path):
    """git work tree 가 아니면(rev-parse 비0) None 반환 — 보수적 전부 백업 폴백."""
    assert pm_import.git_safe_relpaths(tmp_path, git_runner=lambda a: (128, "")) is None


def test_git_safe_relpaths_binary_absent_fail_soft(pm_import, tmp_path, monkeypatch):
    """_real_git_runner: git 바이너리 부재면 subprocess 미실행·(1,'') → git_safe None(fail-soft)."""
    monkeypatch.setattr(pm_import.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        pm_import.subprocess, "run",
        lambda *a, **k: pytest.fail("git which None 인데 subprocess 호출 — 라이브 git 위험."),
    )
    assert pm_import.git_safe_relpaths(tmp_path) is None


# ── ⑤ 중앙 레이아웃 relpath 미러 + 동일자 재실행 _free_backup_path 순번 ────────

def test_central_backup_mirrors_nested_relpath(pm_import, tmp_path):
    """중앙 백업은 nested relpath 를 그대로 미러링한다(`.../<DATE>/.project_manager/...`)."""
    dest = tmp_path / "nested"
    dest.mkdir()
    # 어댑터가 복사하는 nested 파일(board.py)과 충돌하는 사용자 파일을 둔다.
    nested = dest / ".project_manager" / "tools" / "board.py"
    nested.parent.mkdir(parents=True)
    nested.write_text("# user board\n", encoding="utf-8")
    today = datetime.date.today().isoformat()
    rc = pm_import.main(["--into", str(dest), "--harness", "claude", "--name", "Nested"])
    assert rc == 0
    mirrored = dest / pm_import.BACKUP_DIR_NAME / today / ".project_manager" / "tools" / "board.py"
    assert mirrored.is_file(), "nested 충돌이 중앙 디렉토리에 relpath 미러링되지 않음."
    assert mirrored.read_text(encoding="utf-8") == "# user board\n", "미러 백업 내용 불일치."


# ── ⑥ symlink 충돌 follow_symlinks=False 유지 (중앙 디렉토리) — MF1 회귀 방지 ──
# (test_into_symlink_conflict_does_not_follow_link 가 중앙 디렉토리 백업 경로로 MF1 회귀를
#  검증한다 — 위쪽. 여기서는 plan_copy 가 symlink 충돌을 git_safe 와 무관하게 항상 백업으로
#  잡는지(무백업 덮기 금지)를 단위로 확증한다.)

@requires_symlink
def test_symlink_conflict_always_backed_up_even_if_tracked(pm_import, tmp_path):
    """symlink 충돌은 git_safe 집합에 들어 있어도 백업한다 — git-safe skip 으로 무백업 덮으면
    사용자 symlink 구성이 무흔적 손실(MF1). plan_copy 단위."""
    dest = tmp_path / "symtracked"
    dest.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("external\n", encoding="utf-8")
    link = dest / "CLAUDE.md"
    link.symlink_to(outside)
    template_roots = pm_import.resolve_template_roots(REPO, "claude")
    backup_root = dest / pm_import.BACKUP_DIR_NAME / "2026-01-01"
    # CLAUDE.md 가 git_safe 집합에 *있어도* symlink 라 백업해야 한다.
    git_safe = {"CLAUDE.md"}
    actions = pm_import.plan_copy(template_roots, dest, backup_root, git_safe=git_safe)
    claude = next(a for a in actions if a.dst == dest / "CLAUDE.md")
    assert claude.backup is not None, "symlink 충돌이 git-safe skip 으로 무백업 — MF1 위반."
    assert claude._git_safe_skip is False


# ── ⑦ dry-run 무변경 + 결정 출력 ────────────────────────────────────────────

def test_dry_run_into_no_change_and_shows_git_decision(pm_import, tmp_path, capsys):
    """--into --dry-run: 파일시스템 미변경(중앙 백업 디렉토리도 미생성) + git 판정·백업 위치 출력."""
    dest = tmp_path / "dryrundecide"
    dest.mkdir()
    (dest / "CLAUDE.md").write_text("keep\n", encoding="utf-8")
    rc = pm_import.main(["--into", str(dest), "--harness", "claude", "--dry-run"])
    assert rc == 0
    # 무변경: 원본 그대로 · 트리 미복사 · 중앙 백업 디렉토리 미생성.
    assert (dest / "CLAUDE.md").read_text(encoding="utf-8") == "keep\n"
    assert not (dest / ".project_manager").exists()
    assert not (dest / pm_import.BACKUP_DIR_NAME).exists(), "dry-run 인데 중앙 백업 디렉토리 생성됨."
    out = capsys.readouterr().out
    # 백업 위치·git 판정이 계획에 반영(비-git tmp → '비-git/판정불가').
    assert pm_import.BACKUP_DIR_NAME in out, "dry-run 계획에 백업 위치(중앙 디렉토리)가 안 보임."
    assert "백업 위치" in out
    # 충돌 CLAUDE.md 가 백업 대상으로 표시([backup+copy]).
    assert "[backup+copy]" in out


# ── 통합: 실 git repo 에서 추적&clean skip + dirty 백업 + .gitignore 위생 (e2e) ──

@requires_git
def test_into_real_git_repo_tracked_clean_skip_dirty_backup(pm_import, tmp_path):
    """실 git init 한 repo 에 --into: 추적&clean 충돌은 백업 생략, dirty 충돌은 중앙 백업,
    .gitignore 에 `.pm_import_backups/` 가 추가된다(should). 라이브 git e2e."""
    import subprocess
    dest = tmp_path / "realgit"
    dest.mkdir()
    # 두 충돌 파일: README.md 는 커밋(추적&clean), CLAUDE.md 는 커밋 후 수정(dirty).
    (dest / "CLAUDE.md").write_text("committed then dirty\n", encoding="utf-8")
    (dest / "README.md").write_text("committed clean\n", encoding="utf-8")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", str(dest)], check=True)
    subprocess.run(["git", "-C", str(dest), "add", "CLAUDE.md", "README.md"], check=True)
    subprocess.run(["git", "-C", str(dest), "commit", "-q", "-m", "init"], check=True, env=env)
    # CLAUDE.md 를 dirty 하게 만든다(README.md 는 추적&clean 유지).
    (dest / "CLAUDE.md").write_text("now modified — uncommitted\n", encoding="utf-8")

    today = datetime.date.today().isoformat()
    rc = pm_import.main(["--into", str(dest), "--harness", "claude", "--name", "RealGit"])
    assert rc == 0
    backup_root = dest / pm_import.BACKUP_DIR_NAME / today
    # 추적&clean(README.md)은 백업 생략 — git 이 복원 가능.
    assert not (backup_root / "README.md").exists(), \
        "추적&clean 충돌이 백업됨 — git-safe skip 미동작."
    # dirty(CLAUDE.md)는 중앙 백업되고 미커밋 변경 내용 보존.
    assert (backup_root / "CLAUDE.md").is_file(), "dirty 충돌이 중앙 백업되지 않음."
    assert (backup_root / "CLAUDE.md").read_text(encoding="utf-8") == \
        "now modified — uncommitted\n", "dirty 백업이 미커밋 내용을 보존하지 않음."
    # .gitignore 위생(should): 백업 디렉토리 패턴이 추가됐다.
    gitignore = dest / ".gitignore"
    assert gitignore.is_file(), ".gitignore 가 생성/갱신되지 않음(git repo·백업 생성됨)."
    assert f"{pm_import.BACKUP_DIR_NAME}/" in gitignore.read_text(encoding="utf-8")


def test_ensure_backup_dir_gitignored_create_then_idempotent(pm_import, tmp_path):
    """.gitignore 없으면 생성("created")·이미 패턴 있으면 멱등("present")·내용 불변."""
    dest = tmp_path / "gi"
    dest.mkdir()
    # 1회차: .gitignore 없음 → 패턴 1줄 신규 생성(비파괴·신규 파일).
    assert pm_import.ensure_backup_dir_gitignored(dest, set(), set()) == "created"
    text1 = (dest / ".gitignore").read_text(encoding="utf-8")
    assert text1 == f"{pm_import.BACKUP_DIR_NAME}/\n"
    # 2회차: 이미 있음 → 멱등 skip(git-safe 여부 무관)·내용 불변.
    assert pm_import.ensure_backup_dir_gitignored(dest, {".gitignore"}, set()) == "present"
    assert (dest / ".gitignore").read_text(encoding="utf-8") == text1


def test_ensure_backup_dir_gitignored_appends_when_git_safe(pm_import, tmp_path):
    """기존 .gitignore 가 git-safe(추적&미변경)면 기존 규칙 보존하고 패턴만 append("added")."""
    dest = tmp_path / "gi2"
    dest.mkdir()
    (dest / ".gitignore").write_text("node_modules/\n*.log\n", encoding="utf-8")
    assert pm_import.ensure_backup_dir_gitignored(dest, {".gitignore"}, set()) == "added"
    text = (dest / ".gitignore").read_text(encoding="utf-8")
    assert "node_modules/" in text and "*.log" in text, "기존 규칙이 손실됨 — 비파괴 위반."
    assert text.endswith(f"{pm_import.BACKUP_DIR_NAME}/\n")


def test_ensure_backup_dir_gitignored_import_owned_appends(pm_import, tmp_path):
    """import 가 복사·관리한 .gitignore(copied_relpaths)면 git-safe 아니어도 append("added").

    CopyAction 이 사용자 원본을 이미 중앙 백업했으므로 안전 (e2e 정상 경로 — 템플릿이 .gitignore 출하)."""
    dest = tmp_path / "gi4"
    dest.mkdir()
    (dest / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    from pathlib import Path as _P
    # git-safe 아님(set())·하지만 import 가 복사한 파일 → 안전 append.
    assert pm_import.ensure_backup_dir_gitignored(dest, set(), {_P(".gitignore")}) == "added"
    assert (dest / ".gitignore").read_text(encoding="utf-8").endswith(
        f"{pm_import.BACKUP_DIR_NAME}/\n")


def test_ensure_backup_dir_gitignored_unsafe_skips_unbacked_change(pm_import, tmp_path):
    """사전 존재 unbacked 사용자 .gitignore(git-safe 아님·import 미복사)면 무백업 변경 금지 (codex T-0034 must-fix).

    이 append 는 CopyAction 백업 경로를 안 타므로, git 이 복원 못 하고 import 도 안 건드린
    .gitignore 를 변조하면 비파괴 계약 위반이다 → "unsafe-skip" 으로 원본 불변 유지.
    """
    dest = tmp_path / "gi3"
    dest.mkdir()
    original = "node_modules/\n*.log\n"
    (dest / ".gitignore").write_text(original, encoding="utf-8")
    # git_safe 아님 + import 미복사(copied 빈) → 변경 금지.
    assert pm_import.ensure_backup_dir_gitignored(dest, set(), set()) == "unsafe-skip"
    assert (dest / ".gitignore").read_text(encoding="utf-8") == original
    # git_safe is None(비-git/판정 불가)도 동일하게 보수적 skip.
    assert pm_import.ensure_backup_dir_gitignored(dest, None, set()) == "unsafe-skip"
    assert (dest / ".gitignore").read_text(encoding="utf-8") == original


@requires_symlink
def test_ensure_backup_dir_gitignored_symlink_skips_no_follow(pm_import, tmp_path):
    """.gitignore 가 symlink 면 write_text 가 링크 대상을 따라가 변조 → skip (codex T-0034·MF1).

    git-safe·import-소유로 표시돼도 symlink 면 거부 — 링크 대상은 git 복원 대상이 아니다."""
    from pathlib import Path as _P
    dest = tmp_path / "gisym"
    dest.mkdir()
    target = tmp_path / "outside_gitignore"        # 프로젝트 '밖' 가리키는 대상
    target.write_text("external content\n", encoding="utf-8")
    os.symlink(target, dest / ".gitignore")
    # git-safe + import-소유로 표시해도 symlink 면 unsafe-skip(링크 follow 금지).
    status = pm_import.ensure_backup_dir_gitignored(dest, {".gitignore"}, {_P(".gitignore")})
    assert status == "unsafe-skip", status
    # 링크 대상(프로젝트 밖) 내용 불변 — 따라가 변조하지 않음.
    assert target.read_text(encoding="utf-8") == "external content\n"


def test_parse_status_dirty_handles_rename_old_path(pm_import):
    """_parse_status_dirty: rename(R) 엔트리의 old-path 필드를 경로로 오해하지 않는다."""
    # `R  new.md\0old.md\0 M z.md\0` — new.md·z.md 가 dirty, old.md 는 old-path 필드(skip).
    dirty = pm_import._parse_status_dirty("R  new.md\0old.md\0 M z.md\0")
    assert dirty == {"new.md", "z.md"}, f"rename old-path 가 잘못 dirty 로 잡힘: {dirty}"


# ── T-0051: 출하 wiki 스캐폴드 파리티 (fresh import 부트스트랩 계약 + 드리프트 가드) ──
# opencode 출하 템플릿이 claude 와 동형의 instance-owned wiki 스캐폴드를 ship 하는지 보증한다.
# 회귀: opencode 가 standalone(HARNESS_TEMPLATE_DIRS["opencode"]=("opencode",)) 인데 status.md·
# log/current.md·decisions/·ideas/·specs/·status_done.md·각 README·tickets 하위 placeholder 를
# 안 갖고 있어, `--new --harness opencode` 가 불완전 wiki 로 시작했다(첫 wave-finish/handoff 가
# write_text 전 mkdir 없이 status.md/log 에 쓰다 크래시). 이 파일들은 engine.manifest 가 "인스턴스
# 소유"로 전파 제외하므로 pm_update 가 안 채운다 — 각 템플릿이 스캐폴드로 직접 ship 해야 한다.

# 부트스트랩 계약 — opencode AGENTS.md/AGENTS.lite.md 가 읽으라 지시하는 wiki 타깃(= dangling 금지).
# 파일·디렉토리 둘 다 단언(import 결과 트리에 실제 존재해야 함).
_BOOTSTRAP_CONTRACT_FILES = (
    "wiki/status.md",
    "wiki/status_done.md",
    "wiki/architecture.md",
    "wiki/README.md",
    "wiki/log/current.md",
    "wiki/decisions/README.md",
    "wiki/ideas/README.md",
    "wiki/specs/README.md",
    "wiki/raw/README.md",
    "wiki/tickets/README.md",
    "wiki/pm_role.local.md",
)
# ticket_finish/pm_handoff 가 write_text 전 mkdir 없이 쓰는 디렉토리(부재 시 첫 finish 크래시) +
# board.py 가 lazy 생성하긴 하지만 출하 스캐폴드로 존재해야 하는 하위 디렉토리들.
_BOOTSTRAP_CONTRACT_DIRS = (
    "wiki/ideas",
    "wiki/log/archive",
    "wiki/tickets/open",
    "wiki/tickets/claimed",
    "wiki/tickets/done",
    "wiki/tickets/blocked",
)


def _assert_bootstrap_contract(dest: Path, harness: str) -> None:
    pm = dest / ".project_manager"
    for rel in _BOOTSTRAP_CONTRACT_FILES:
        assert (pm / rel).is_file(), \
            f"[{harness}] 부트스트랩 계약 파일 누락(dangling): {rel}"
    for rel in _BOOTSTRAP_CONTRACT_DIRS:
        assert (pm / rel).is_dir(), \
            f"[{harness}] 부트스트랩 계약 디렉토리 누락: {rel}"


def test_opencode_import_satisfies_bootstrap_contract(pm_import, tmp_path):
    """--new --harness opencode 결과가 부트스트랩 계약 wiki 타깃을 전부 갖춘다(hermetic).

    실 opencode CLI 무호출(_hermetic_opencode_models 가 (False,[]) 고정). 회귀 시 여기서
    status.md·log/current.md·decisions/·ideas/·tickets 하위 placeholder 누락을 즉시 FAIL.
    """
    dest = tmp_path / "oc_contract"
    rc = pm_import.main(["--new", str(dest), "--harness", "opencode", "--name", "OcContract"])
    assert rc == 0
    _assert_bootstrap_contract(dest, "opencode")


def test_claude_import_satisfies_bootstrap_contract(pm_import, tmp_path):
    """claude 동일 import 도 같은 부트스트랩 계약을 만족(양 하니스 대칭 — 동형 보증)."""
    dest = tmp_path / "cl_contract"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "ClContract"])
    assert rc == 0
    _assert_bootstrap_contract(dest, "claude")


# (T-0429) 옛 `test_opencode_import_substitutes_operational_tokens`(opencode 손-복제 대칭)는
#   위 파생-축 parametrize(`test_new_substitutes_operational_placeholders[opencode]`)에 흡수됐다 —
#   "claude 로 쓰고 opencode 대칭 복제" 라는 손-복제 패턴 자체가 세 번째 하네스를 못 따라온 뿌리였다.


# ── 드리프트 가드(핵심·자동 사각 해소): opencode 스캐폴드 집합 ⊇ claude 스캐폴드 ──────
# 미래에 claude 템플릿에 instance-owned wiki 스캐폴드가 추가되면 opencode 미반영을 FAIL 로 잡는다.
# 양 트리의 git-추적 wiki 상대경로 집합을 비교 — claude 에 있는데 opencode 에 없는 게 0 이어야 한다
# (문서화된 harness-특화 allowlist 만 차감). 엔진-동기 파일(pm_playbook·pm_role·pm_state·_template)은
# 이미 양쪽 존재하니 포함돼도 무방하다.

# harness-특화로 opencode 가 의도적으로 안 들고 가는 claude wiki 파일(현재 없음). 미래에 claude 전용
# 스캐폴드가 생기면(예: claude 고유 가이드) 여기 명시적으로 등록 — "조용한 드리프트"가 아니라 "문서화된
# 의도적 차이"로만 통과시킨다.
_HARNESS_SPECIFIC_CLAUDE_ONLY_WIKI: frozenset[str] = frozenset()


def _git_tracked_wiki_relpaths(harness_dir: str) -> set[str]:
    """templates/<harness_dir>/.project_manager/wiki/ 하위의 git-추적 파일 상대경로(wiki 기준) 집합.

    `git ls-files` 로 staged/추적 상태를 본다 — .gitkeep 이 실제 추적돼야(빈 dir 복사·커밋의 유일
    메커니즘) 집합에 들어온다. 미추적 .gitkeep 은 import 복사도 안 되므로 이 가드가 그것까지 잡는다.
    """
    import subprocess
    prefix = f"templates/{harness_dir}/.project_manager/wiki/"
    out = subprocess.run(
        ["git", "ls-files", prefix],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout
    return {
        line[len(prefix):]
        for line in out.splitlines()
        if line.startswith(prefix)
    }


def test_opencode_wiki_scaffold_superset_of_claude(pm_import):
    """드리프트 가드: opencode 출하 wiki 스캐폴드 ⊇ claude(harness-특화 allowlist 차감).

    claude 에 있는데 opencode 에 없는 추적 wiki 파일이 0 이어야 한다 — 미래에 claude 에 스캐폴드가
    추가되고 opencode 에 미반영되면 즉시 FAIL(T-0051 회귀 재발 차단·자동 사각 해소).
    """
    claude_set = _git_tracked_wiki_relpaths("claude_code")
    opencode_set = _git_tracked_wiki_relpaths("opencode")

    # 가드가 의미를 가지려면 claude 트리 자체가 비어있지 않아야 한다(경로 오타·트리 이동 방지).
    assert claude_set, "claude wiki 추적 파일이 비어있음 — 경로/트리 확인 필요."

    missing = (claude_set - opencode_set) - _HARNESS_SPECIFIC_CLAUDE_ONLY_WIKI
    assert not missing, (
        "opencode 출하 wiki 스캐폴드가 claude 를 누락(드리프트) — opencode 템플릿에 미러하거나 "
        f"_HARNESS_SPECIFIC_CLAUDE_ONLY_WIKI 에 의도적 차이로 등록하라: {sorted(missing)}"
    )


def test_opencode_wiki_gitkeep_placeholders_tracked(pm_import):
    """빈 디렉토리 placeholder(.gitkeep)가 실제로 git-추적되는지 — 빈 dir 복사·커밋 보장의 유일 메커니즘.

    git 은 빈 디렉토리를 추적하지 않으므로, .gitkeep 이 미추적이면 import 가 그 디렉토리를 복사도
    못 한다(부트스트랩 계약 디렉토리 누락으로 이어짐). 8 placeholder 가 전부 추적 집합에 있어야 한다.
    """
    tracked = _git_tracked_wiki_relpaths("opencode")
    expected_gitkeeps = {
        "ideas/open/.gitkeep",
        "ideas/killed/.gitkeep",
        "ideas/promoted/.gitkeep",
        "log/archive/.gitkeep",
        "tickets/open/.gitkeep",
        "tickets/claimed/.gitkeep",
        "tickets/done/.gitkeep",
        "tickets/blocked/.gitkeep",
    }
    missing = expected_gitkeeps - tracked
    assert not missing, f"opencode wiki .gitkeep placeholder 미추적(빈 dir 복사 불가): {sorted(missing)}"


def test_opencode_tickets_readme_no_claude_specific_session_env(pm_import):
    """tickets/README.md 의 세션 식별 절이 claude-특화 잔존 0 인지(드리프트 가드 allowlist 정합).

    claude 원본의 `CLAUDE_SESSION_NAME 환경변수` 단독 우선 안내(claude-특화)를 opencode 는 harness-무관
    `--repo`/`--slot` 1순위 안내로 적응했다(ADR-0057). 그 한 줄 외 나머지 본문은 claude 와 동일해야 파리티가 의미를 가진다.
    """
    oc_readme = (
        REPO / "templates" / "opencode" / ".project_manager" / "wiki" / "tickets" / "README.md"
    ).read_text(encoding="utf-8")
    # harness-무관 1순위 안내가 들어있어야 한다(ADR-0057: --repo/--slot 로 통일).
    assert "--repo" in oc_readme and "--slot" in oc_readme
    assert "harness-무관" in oc_readme, "세션 식별 절이 harness-무관 안내로 적응되지 않음."
    # claude 원본의 'CLAUDE_SESSION_NAME 환경변수\n1.' 단독 우선 형태가 그대로 남아있지 않아야 한다.
    assert "1. `CLAUDE_SESSION_NAME` 환경변수" not in oc_readme, \
        "claude-특화 세션 env 안내가 그대로 잔존 — opencode 적응 누락."


# ── add_harness — 라이브 인스턴스 harness 추가(어댑터 네임스페이스 스코프) · ADR-0048 (T-0269) ──
# scoped add-harness core: 라이브 인스턴스에 두 번째 harness 어댑터를 비파괴로 추가. 복사 스코프를
# *추가 harness 의 어댑터 네임스페이스*로 제한해 raw 재-import 의 wiki/엔진 clobber 를 구조적으로 차단.

# 어댑터 네임스페이스 판정(구현 predicate 와 독립·불변식을 진짜로 재는 참조 규칙).
#   opencode = `.opencode/**` + `AGENTS.md`  ·  claude = `.claude/**`(@render agents·skills 제외) + `CLAUDE.md`.
_ADD_HARNESS_NS = {
    "opencode": (".opencode", "AGENTS.md", ()),
    "claude": (".claude", "CLAUDE.md", (".claude/agents/", ".claude/skills/")),
}


def _build_live_instance(pm_import, dest: Path, harness: str) -> Path:
    """`--new` 로 라이브 인스턴스 트리를 만든다(board init 포함). add_harness 대상."""
    rc = pm_import.main(["--new", str(dest), "--harness", harness, "--name", "Live Inst"])
    assert rc == 0, f"라이브 인스턴스 셋업 실패(rc={rc}·harness={harness})."
    return dest


def _plan_relpaths(plan, dest: Path) -> list[str]:
    return sorted(a.dst.resolve().relative_to(dest.resolve()).as_posix() for a in plan)


def _rel_in_namespace(rel: str, added_harness: str) -> bool:
    """rel 이 *추가되는 harness* 의 어댑터 **구조적** 네임스페이스(adapter dir + root doc) 안인가.

    R17(T-0456): 복사 제외는 flavor-native 가 아니라 **host 실소유** 기준이라(claude-as-guest)
    `.claude/agents` 도 non-claude host 엔 복사된다 — 네임스페이스 *멤버십*(불변식 대상)은 render_excl
    차감과 무관하다(구조적 네임스페이스 = adapter dir + root doc). render_excl 은 copy-포함 판정이지
    멤버십 아님(옛 판정은 둘을 혼동했다)."""
    adapter_dir, root_doc, _render_excl = _ADD_HARNESS_NS[added_harness]
    return rel == root_doc or rel == adapter_dir or rel.startswith(adapter_dir + "/")


def test_add_harness_exposed(pm_import):
    assert callable(pm_import.add_harness)
    # ADR-0070 D5 ①: 값 shape = (adapter_dirs: tuple, root_doc). claude/opencode 는 단일-원소
    # 튜플, codex 는 이중(.codex agents/config/hooks + .agents skills·dual-namespace 강제).
    assert pm_import.ADD_HARNESS_ADAPTER["opencode"] == ((".opencode",), "AGENTS.md")
    assert pm_import.ADD_HARNESS_ADAPTER["claude"] == ((".claude",), "CLAUDE.md")
    assert pm_import.ADD_HARNESS_ADAPTER["codex"] == ((".codex", ".agents"), "AGENTS.md")
    assert pm_import.ADD_HARNESS_CREATE_IF_ABSENT["codex"] == {
        "AGENTS.md", ".codex/config.toml", ".codex/hooks.json",
    }
    # 카드의 model/reasoning 은 local.conf 파생물이라 보존 선언 대상이 아니다 — 여기 두면
    #   add-harness 는 손편집을 지키고 다음 pm-update 는 conf 로 되돌린다(두 표면 반대 규칙).
    assert pm_import.ADD_HARNESS_PRESERVE_EXISTING_TOML_FIELDS["codex"] == {}
    assert not any(pm_import.ADD_HARNESS_PRESERVE_EXISTING_TOML_FIELDS.values())
    assert not pm_import.ADD_HARNESS_CREATE_IF_ABSENT["claude"]
    assert not pm_import.ADD_HARNESS_CREATE_IF_ABSENT["opencode"]
    # shape 불변식: 모든 값 = (dirs:튜플[str], root_doc:str) — 소비처 iterate 전제.
    for harness, (dirs, root_doc) in pm_import.ADD_HARNESS_ADAPTER.items():
        assert isinstance(dirs, tuple) and dirs and all(isinstance(d, str) for d in dirs), harness
        assert isinstance(root_doc, str) and root_doc, harness


def test_add_harness_dry_run_opencode_scope(pm_import, tmp_path):
    """claude 인스턴스에 opencode add(dry-run): plan 은 `.opencode/**`+`AGENTS.md` 만·파일 미변경."""
    dest = _build_live_instance(pm_import, tmp_path / "claude_inst", "claude")
    plan = pm_import.add_harness(dest, "opencode", dry_run=True, source_root=REPO)
    rels = _plan_relpaths(plan, dest)
    assert rels, "plan 이 비어 있다 — opencode 어댑터 신규 집합이 잡혀야 한다."
    # 어댑터 신규 산출물이 실제로 포함(스코프가 맞는 트리를 잡았다는 sanity).
    assert "AGENTS.md" in rels
    assert ".opencode/agents/pm.md" in rels
    # 전부 opencode 네임스페이스 안.
    assert all(_rel_in_namespace(r, "opencode") for r in rels), rels
    # dry-run = 파일시스템 미변경(.opencode 미생성).
    assert not (dest / ".opencode").exists(), "dry-run 이 .opencode 를 생성했다(파일 변경)."
    assert not (dest / "AGENTS.md").exists(), "dry-run 이 AGENTS.md 를 생성했다(파일 변경)."


@pytest.mark.parametrize("base,added", [("claude", "opencode"), ("opencode", "claude")])
def test_add_harness_invariant_zero_outside_namespace(pm_import, tmp_path, base, added):
    """불변식 가드(ADR-0048 Decision 5): plan 이 어댑터 네임스페이스 밖 relpath 를 0개 포함.

    양 harness — claude 인스턴스에 opencode 추가 / opencode 인스턴스에 claude 추가 모두 검증한다.
    """
    dest = _build_live_instance(pm_import, tmp_path / f"{base}_inst", base)
    plan = pm_import.add_harness(dest, added, dry_run=True, source_root=REPO)
    rels = _plan_relpaths(plan, dest)
    assert rels, f"plan 이 비어 있다(base={base}·added={added})."
    outside = [r for r in rels if not _rel_in_namespace(r, added)]
    assert outside == [], (
        f"add_harness({added}) plan 에 어댑터 네임스페이스 밖 relpath 포함(불변식 위반): {outside}"
    )


@pytest.mark.parametrize("base,added,other_dir,other_doc", [
    ("claude", "opencode", ".claude/", "CLAUDE.md"),
    ("opencode", "claude", ".opencode/", "AGENTS.md"),
])
def test_add_harness_live_safe_excludes_wiki_engine_facades(
    pm_import, tmp_path, base, added, other_dir, other_doc,
):
    """라이브-안전 회귀: 기존 wiki/엔진/타 harness/설정/파사드 경로가 plan 에 절대 없다.

    raw 재-import 가 덮던 것들(`.project_manager/**` wiki dev-state·엔진·engine.manifest·
    .gitignore·.github·루트 파사드·다른 harness 어댑터)이 plan 에서 구조적으로 배제됨을 단언한다.
    """
    dest = _build_live_instance(pm_import, tmp_path / f"{base}_ls", base)
    plan = pm_import.add_harness(dest, added, dry_run=True, source_root=REPO)
    rels = _plan_relpaths(plan, dest)

    # 엔진 + wiki dev-state (라이브 파괴 원천) — 0개.
    assert not [r for r in rels if r.startswith(".project_manager/")], \
        f"plan 에 .project_manager/** (엔진+wiki) 포함: {[r for r in rels if r.startswith('.project_manager/')]}"
    # 공유 설정 / CI / 파사드 — 0개.
    forbidden_exact = {".gitignore", ".gitattributes", ".project_manager/engine.manifest",
                       "pm-config.sh", "pm-config.cmd", "pm-update.sh", "pm-update.cmd", "README.md"}
    assert not (set(rels) & forbidden_exact), f"plan 에 공유 설정/파사드 포함: {set(rels) & forbidden_exact}"
    assert not [r for r in rels if r.startswith(".github/")], "plan 에 .github/** 포함(공유 CI)."
    # 다른(기존) harness 어댑터 — 0개.
    assert not [r for r in rels if r.startswith(other_dir)], \
        f"plan 에 타 harness 어댑터({other_dir}) 포함: {[r for r in rels if r.startswith(other_dir)]}"
    assert other_doc not in rels, f"plan 에 타 harness root doc({other_doc}) 포함."


def test_add_harness_claude_guest_copies_agents_excludes_host_owned_skills(pm_import, tmp_path):
    """claude-as-guest on opencode host — **host 실소유 경로만 제외** (T-0456 R17·N×N 역방향 MF).

    opencode host 는 `.claude/skills`(ADR-0065 native 소비)를 이미 소유 → 제외한다. 그러나
    `.claude/agents` 는 opencode host 가 소유하지 않으므로 **복사한다** — 옛 flavor-native 판정은 claude
    flavor 관점이라 `.claude/agents`·`.claude/skills` 를 전부 native 로 오차감해 claude-as-guest 를
    놓쳤다(등재 0 → pm_update 영구 관리 불능·MF). `CLAUDE.md`·`.claude/settings.json` 도 포함.
    """
    dest = _build_live_instance(pm_import, tmp_path / "oc_inst", "opencode")
    plan = pm_import.add_harness(dest, "claude", dry_run=True, source_root=REPO)
    rels = _plan_relpaths(plan, dest)
    # host(opencode)가 소유한 `.claude/skills`(ADR-0065)는 제외.
    assert not [r for r in rels if r.startswith(".claude/skills/")], \
        f"opencode host 소유 `.claude/skills/**` 오적재: {[r for r in rels if r.startswith('.claude/skills/')]}"
    # host 미소유 `.claude/agents` 는 **복사**(claude-as-guest·MF 수정·옛엔 native 로 오차감).
    assert [r for r in rels if r.startswith(".claude/agents/")], \
        "claude-as-guest 의 `.claude/agents/**` 미복사(R17 MF 미해소)"
    # target-owned 어댑터 파일 + root doc 은 포함(스코프가 claude 어댑터를 실제로 잡았다는 sanity).
    assert "CLAUDE.md" in rels
    assert ".claude/settings.json" in rels
    # 전부 claude 구조적 네임스페이스 안(불변식 재확인).
    assert all(_rel_in_namespace(r, "claude") for r in rels), rels


def test_add_harness_apply_opencode_creates_adapter_and_preserves_devstate(pm_import, tmp_path):
    """apply(opencode→claude 인스턴스): `.opencode/**`+`AGENTS.md` 신규·wiki/엔진/타 harness 불변."""
    dest = _build_live_instance(pm_import, tmp_path / "claude_apply", "claude")
    # 라이브 dev-state·타 harness 파일의 apply 전 바이트 스냅샷.
    wiki_role = dest / ".project_manager" / "wiki" / "pm_role.md"
    engine_board = dest / ".project_manager" / "tools" / "board.py"
    claude_doc = dest / "CLAUDE.md"
    before = {p: p.read_bytes() for p in (wiki_role, engine_board, claude_doc)}

    plan = pm_import.add_harness(dest, "opencode", dry_run=False, source_root=REPO)
    assert plan, "apply plan 이 비어 있다."

    # 어댑터 파일이 실제로 생성됐다.
    assert (dest / "AGENTS.md").is_file()
    assert (dest / ".opencode" / "agents" / "pm.md").is_file()
    # operational 토큰이 치환됐다(어댑터 산출물에 raw 토큰 잔존 0).
    assert _grep_token_files(dest / ".opencode", "{{PROJECT_NAME}}") == [], \
        "apply 후 .opencode 에 {{PROJECT_NAME}} 미치환 잔존."
    assert _grep_token_files(dest / ".opencode", "{{OPENCODE_PRO_MODEL}}") == [], \
        "apply 후 .opencode 에 {{OPENCODE_PRO_MODEL}} 미해소 잔존(TODO 중화 실패)."
    # 인스턴스 project_name(Live Inst)이 어댑터에 반영됐다.
    assert "Live Inst" in (dest / "AGENTS.md").read_text(encoding="utf-8")

    # 라이브 dev-state·엔진·타 harness root doc 은 바이트 단위 불변(라이브-안전).
    for p, raw in before.items():
        assert p.read_bytes() == raw, f"add-harness 가 스코프 밖 파일을 변경했다: {p}"


def test_add_harness_apply_claude_creates_adapter_and_preserves_devstate(pm_import, tmp_path):
    """apply(claude→opencode 인스턴스·반대 방향): `.claude/**`(host 미소유분)+`CLAUDE.md`
    신규·기존 opencode 어댑터/wiki/엔진 불변. (DoD "양 harness apply" 문자 충족.)

    R17(T-0456·N×N 역방향): opencode host 가 소유하지 않는 `.claude/agents` 는 **복사된다**(claude-as-
    guest·옛 flavor-native 오차감 수정). opencode 가 이미 소유한 `.claude/skills` 는
    claude add-harness 가 재적재/변경하지 않는다(host 실소유 차감·byte-불변 preserve 로 확인).
    """
    dest = _build_live_instance(pm_import, tmp_path / "opencode_apply", "opencode")
    # 라이브 dev-state·엔진·타(기존) harness 파일의 apply 전 바이트 스냅샷.
    wiki_role = dest / ".project_manager" / "wiki" / "pm_role.md"
    engine_board = dest / ".project_manager" / "tools" / "board.py"
    opencode_doc = dest / "AGENTS.md"
    opencode_agent = dest / ".opencode" / "agents" / "pm.md"
    before = {p: p.read_bytes() for p in (wiki_role, engine_board, opencode_doc, opencode_agent)}
    # `.claude/skills`는 opencode 모델 skill tool 채널로 인스턴스에 **이미 존재** — add-harness 가
    #   건드리면 안 되므로(공유 canonical) apply 전 스냅샷에 넣어 byte-불변을 검증한다.
    skills_dir = dest / ".claude" / "skills"
    assert skills_dir.is_dir(), (
        "opencode 인스턴스에 `.claude/skills` 부재 — 모델 skill tool 채널 출하 누락.")
    for f in skills_dir.rglob("SKILL.md"):
        before[f] = f.read_bytes()

    plan = pm_import.add_harness(dest, "claude", dry_run=False, source_root=REPO)
    assert plan, "apply plan 이 비어 있다."

    # claude 어댑터 파일이 실제로 생성됐다(root doc + target-owned 어댑터 파일).
    assert (dest / "CLAUDE.md").is_file()
    assert (dest / ".claude" / "settings.json").is_file()
    # claude-as-guest: opencode host 미소유 `.claude/agents` 는 **복사된다**(R17·옛엔 native 로 오차감).
    #   `.claude/skills` 는 opencode 소유(ADR-0065)라 add-harness 가 재적재/변경하지 않는다(아래 byte-불변).
    assert (dest / ".claude" / "agents").is_dir() and any((dest / ".claude" / "agents").glob("*.md")), \
        "claude-as-guest 의 `.claude/agents` 미복사(R17 MF 미해소·pm_update 영구 관리 불능)."
    # operational 토큰이 치환됐다(claude 어댑터 산출물에 raw 토큰 잔존 0).
    assert _grep_token_files(dest / ".claude", "{{PROJECT_NAME}}", exclude_engine_docs=True) == [], \
        "apply 후 .claude 에 {{PROJECT_NAME}} 미치환 잔존."
    assert "{{PROJECT_NAME}}" not in (dest / "CLAUDE.md").read_text(encoding="utf-8"), \
        "apply 후 CLAUDE.md 에 {{PROJECT_NAME}} 미치환 잔존."
    # 인스턴스 project_name(Live Inst)이 어댑터에 반영됐다.
    assert "Live Inst" in (dest / "CLAUDE.md").read_text(encoding="utf-8")

    # 라이브 dev-state·엔진·기존 opencode 어댑터(root doc+agent)는 바이트 단위 불변(라이브-안전).
    for p, raw in before.items():
        assert p.read_bytes() == raw, f"add-harness 가 스코프 밖 파일을 변경했다: {p}"


def test_add_harness_claude_prints_only_claude_trust_guidance(pm_import, tmp_path, capsys):
    """claude add-harness 완료 출력은 Claude 안내만 내고 codex 안내는 내지 않는다."""
    dest = _build_live_instance(pm_import, tmp_path / "opencode_add_claude_trust", "opencode")
    capsys.readouterr()

    pm_import.add_harness(dest, "claude", dry_run=False, source_root=REPO)

    out = capsys.readouterr().out
    assert "Claude Code permissions.allow 적용 조건 안내" in out
    assert "Ignoring N permissions.allow entries" in out
    assert "2단계 trust 승인" not in out
    assert "hook trust" not in out


# 전 하네스쌍(base ≠ added) — 파생 축 HARNESSES 에서 유도(손-열거 아님·codex 포함 6쌍).
_ADD_HARNESS_APPLY_PAIRS = [(b, a) for b in HARNESSES for a in HARNESSES if b != a]


def _flavor_manifest_entries(pm_import, harness):
    """하네스 flavor manifest 의 ManifestEntry 목록 (테스트 공용 파생원)."""
    template_root = REPO / "templates" / pm_import.HARNESS_TEMPLATE_DIRS[harness][0]
    return pm_import._pm_update_read_manifest(
        template_root / ".project_manager" / "engine.manifest")


def _expected_guest_engine_paths(pm_import, added, dest_owned):
    """이 host 에 `added` 를 얹었을 때 등재돼야 할 **엔진 행** 경로 — 복사 술어 파생."""
    adapter_dirs, root_doc = pm_import.ADD_HARNESS_ADAPTER[added]
    entries = _flavor_manifest_entries(pm_import, added)
    render_paths = {str(e).replace("\\", "/") for e in entries if getattr(e, "render", False)}
    return {
        str(e).replace("\\", "/") for e in entries
        if not getattr(e, "render", False)
        and pm_import._in_adapter_namespace(
            Path(str(e).replace("\\", "/")), adapter_dirs, root_doc,
            dest_owned, render_paths)
    }


@pytest.mark.parametrize("base,added", _ADD_HARNESS_APPLY_PAIRS)
def test_add_harness_guest_registration_within_namespace_or_flavor_render(
        pm_import, pm_update, tmp_path, base, added):
    """**등재 ⊆ flavor@render ∪ (복사 술어 통과 flavor 비-@render)** 불변식 (전 N×(N−1) 순서쌍).

    **R18 지시가 R25 에서 기능 요건으로 반전**: R18 은 "flavor 가 무관 공유 경로도 `@render` 로 들 수
    있으니 등재를 복사 namespace 로 막자"였으나(옛 이름 `..._subset_of_added_namespace`), opencode 의
    `.claude/skills @render`(ADR-0065 네이티브 소비)는 `.opencode` namespace 밖이면서도 opencode 어댑터가
    반드시 소비하는 **cross-ns 의존물**이라 codex host(미소유)엔 복사·등재해야 한다(namespace cap 이 이걸
    놓쳐 PM 스킬 파손 = R25 MF). 새 경계 = **flavor `@render` 선언 자체** — 등재 경로는 (a) 추가 하네스
    namespace 안이거나 (b) `added` flavor 가 `@render` 로 선언한 경로다(그 밖 = flavor 미선언 유입 0).
    host 실소유 차감(R17)은 그 위에 얹혀 더 좁힐 뿐(claude host + opencode 는 `.claude/skills` host-소유라
    미등재 — codex↔claude host 대조는 `test_add_harness_opencode_guest_cross_ns_skills_by_host`).

    **T-0574 로 상한이 넓어졌다**: 절은 이제 렌더물(`@render`)뿐 아니라 **엔진 행**(비-`@render`·
    update 채널)도 담는다. 엔진 행 후보는 복사 술어(`_in_adapter_namespace`)를 그대로 태워 뽑으므로
    상한도 그 술어로 파생한다 — 손-열거가 아니라 "등재 ⊆ 복사" 가 불변식의 형태다."""
    dest = _build_live_instance(pm_import, tmp_path / f"{base}_add_{added}", base)
    dest_owned = pm_import._dest_manifest_core_paths(dest)
    pm_import.add_harness(dest, added, dry_run=False, source_root=REPO)
    reg = _guest_block_paths(pm_update, dest / ".project_manager" / "engine.manifest")
    adapter_dirs = HARNESS_ADAPTER_DIRS[added]
    flavor_render = pm_import._flavor_render_relpaths(
        REPO / "templates" / pm_import.HARNESS_TEMPLATE_DIRS[added][0])

    def _in_scope(r: str) -> bool:
        return (any(r == d or r.startswith(d + "/") for d in adapter_dirs)
                or any(r == fr or r.startswith(fr + "/") for fr in flavor_render))
    outside = sorted(r for r in reg if not _in_scope(r))
    assert outside == [], (
        f"{base}->{added}: guest 등재가 namespace ∪ flavor-@render 밖(flavor 미선언 유입): {outside}")
    # 등재 ⊆ flavor `@render` 선언 ∪ 복사 술어를 통과한 flavor 비-@render(엔진 행) — 두 채널 모두
    #   flavor manifest 파생이고 후자는 복사 스코프의 부분집합이다(등재⊆복사·미복사 경로 등재 0).
    allowed = flavor_render | _expected_guest_engine_paths(pm_import, added, dest_owned)
    assert reg <= allowed, (
        f"{base}->{added}: 등재에 flavor 미선언·미복사 경로 포함: {sorted(reg - allowed)}")


# 출하 flavor manifest 가 지금 들고 있는 **어댑터 네임스페이스 엔진 파일**(비-`@render`) 수 —
#   codex `.codex/{pm_orch_codex.py,rules/default.rules}` 2 · opencode `.opencode/{lib,plugins,
#   pm_orch_opencode.py,.gitignore}` 4 · claude ctx 가드 5종 + `pm_orch_claude.py` +
#   `run_tests_hook.sh` + `precompact_capture_hook.sh` 8. 구조 단언과 별개로 **수**를 못박아,
#   새로 얹을 때 그것이 guest 채널을 타는지 사람이 한 번 확인하게 만든다(등재 누락 = 그 하네스의
#   영구 동결). codex 의 2번째가 T-0584 로 편입된 execpolicy rules 다.
_GUEST_ENGINE_ROW_COUNT = {"codex": 2, "opencode": 4, "claude": 9}


@pytest.mark.parametrize("base,added", _ADD_HARNESS_APPLY_PAIRS)
def test_add_harness_registers_flavor_engine_rows(
        pm_import, pm_update, tmp_path, base, added):
    """add-harness 가 guest **엔진 행**(비-`@render`)을 dest manifest 에 정확히 등재한다 (T-0574 ②).

    엔진 행이 없으면 `.codex/pm_orch_codex.py`·`.opencode/lib`·claude ctx 가드처럼 `pm_relay` 코어와
    짝인 engine-mirror 가 설치 시점 사본으로 **영구 동결**된다(코어↔드라이버 skew) — add-harness 는
    복사만 하고 등재하지 않았고, 등재 유일분이던 `@render` 행은 update plan 에서 전량 차감됐다.

    단언 셋: (a) 등재 집합 == 복사 술어 파생 기대집합(host 실소유 차감 포함) · (b) 출하 수 일치
    (codex 2·opencode 4·claude 8) · (c) 각 엔진 행이 `@target-owned` + 비-`@render` 마커다."""
    dest = _build_live_instance(pm_import, tmp_path / f"{base}_eng_{added}", base)
    dest_owned = pm_import._dest_manifest_core_paths(dest)
    expected = _expected_guest_engine_paths(pm_import, added, dest_owned)
    assert len(expected) == _GUEST_ENGINE_ROW_COUNT[added], (
        f"{base}->{added}: 출하 flavor 의 엔진 행 수가 기대와 다름 — 상류가 엔진 파일을 "
        f"추가/제거했다면 _GUEST_ENGINE_ROW_COUNT 를 갱신하라: {sorted(expected)}")

    pm_import.add_harness(dest, added, dry_run=False, source_root=REPO)
    block_entries = pm_update._dest_guest_manifest_entries(dest)
    engine_rows = [e for e in block_entries if not getattr(e, "render", False)]
    got = {str(e).replace("\\", "/") for e in engine_rows}
    assert got == expected, (
        f"{base}->{added}: guest 엔진 행 등재가 기대와 다름 "
        f"(누락 {sorted(expected - got)} · 초과 {sorted(got - expected)})")
    assert all(getattr(e, "target_owned", False) for e in engine_rows), (
        f"{base}->{added}: 엔진 행에 @target-owned 부재 — upstream flavor 부재 시 rc2 로 전체가 "
        f"막힌다: {[str(e) for e in engine_rows if not getattr(e, 'target_owned', False)]}")
    # 등재 ⊆ 복사: 등재한 엔진 행은 실제로 dest 에 레이다운돼 있어야 한다(없는 파일 생성 금지).
    for path in sorted(got):
        assert (dest / path).exists(), (
            f"{base}->{added}: 등재했으나 복사되지 않은 엔진 경로(pm_update 가 새 파일을 만든다): {path}")


@pytest.mark.parametrize("added", sorted(_GUEST_ENGINE_ROW_COUNT))
def test_add_harness_engine_row_sync_is_idempotent_and_prunes_stale(
        pm_import, pm_update, tmp_path, added):
    """엔진 행 등재도 refresh 멱등(중복 0)이고 상류 폐기분은 제거된다 (T-0574 ③).

    엔진 행이 `@render` 행과 다른 축이라, 목표 계산이 한쪽만 보면 refresh 마다 등재 churn 이거나
    폐기 행이 영구 잔존한다(그 행은 매 sync 마다 `[skip]` 을 찍는 잔재가 된다)."""
    base = "claude" if added != "claude" else "opencode"
    dest = _build_live_instance(pm_import, tmp_path / f"idem_{added}", base)
    pm_import.add_harness(dest, added, dry_run=False, source_root=REPO)
    mani = dest / ".project_manager" / "engine.manifest"
    before = mani.read_text(encoding="utf-8")

    pm_import.add_harness(dest, added, dry_run=False, source_root=REPO)  # refresh(멱등).
    assert mani.read_text(encoding="utf-8") == before, \
        f"{added}: 엔진 행 포함 refresh 가 manifest 를 바꿈(등재 churn)"
    paths = [ln.split()[0] for ln in
             pm_update._extract_guest_manifest_block(before).splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    assert len(paths) == len(set(paths)), f"{added}: guest 절 경로 중복(멱등 위반): {paths}"

    # 상류 폐기분 제거 — 현행 flavor 에 없는 엔진 행을 심고 refresh.
    adapter_dir = pm_import.ADD_HARNESS_ADAPTER[added][0][0]
    stale = f"{adapter_dir}/obsolete_engine.py    @target-owned"
    end = pm_update._GUEST_MANIFEST_END
    mani.write_text(before.replace(end, stale + "\n" + end), encoding="utf-8")
    pm_import.add_harness(dest, added, dry_run=False, source_root=REPO)
    assert f"{adapter_dir}/obsolete_engine.py" not in _guest_block_paths(pm_update, mani), \
        f"{added}: 상류에서 폐기된 엔진 행이 guest 절에 잔존"


def test_render_managed_relpaths_excludes_guest_engine_rows(pm_import, pm_update, tmp_path):
    """guest 엔진 행은 `_render_managed_relpaths` 에 유입되지 않는다 (T-0574 ④).

    엔진 행은 byte-copy 채널이라 렌더 대상이 아니다 — 새면 add-harness/pm_update 가 바이너리성
    스크립트를 operational 치환 대상으로 삼아 내용을 훼손한다(`@render` 판정 단일 근거 유지)."""
    dest = _build_live_instance(pm_import, tmp_path / "render_managed", "claude")
    pm_import.add_harness(dest, "opencode", dry_run=False, source_root=REPO)
    managed = pm_import._render_managed_relpaths(dest)
    engine_paths = {
        str(e).replace("\\", "/")
        for e in pm_update._dest_guest_manifest_entries(dest)
        if not getattr(e, "render", False)
    }
    assert engine_paths, "픽스처 전제 붕괴 — guest 엔진 행이 하나도 등재되지 않았다"
    assert not (engine_paths & managed), \
        f"비-@render guest 엔진 행이 render 대상에 유입: {sorted(engine_paths & managed)}"
    assert ".opencode/agents" in managed, "렌더물 축이 함께 사라짐(공허 단언 방지)"


@pytest.mark.parametrize("base,added", _ADD_HARNESS_APPLY_PAIRS)
def test_add_harness_apply_zero_operational_token_leak(pm_import, tmp_path, base, added):
    """add_harness apply 후 **추가된** 어댑터 네임스페이스에 미해소 operational 토큰 0 (전 하네스쌍·
    codex 포함). 옛 apply 게이트(:2977/:3006)는 스캔 루트가 `dest/".opencode"`·`dest/".claude"`
    **하드코딩**이라 codex 대응 함수 자체가 없었다 — codex 추가 시 `.codex/agents/*.toml`(sed 치환
    대상·`.toml`)이 실제로 치환됐는지 아무도 안 봤다. 스캔 트리를 엔진 파생 HARNESS_ADAPTER_DIRS[added]
    (ADD_HARNESS_ADAPTER 유도)로 잡아 네 번째 하네스도 자동 편입한다(T-0429).

    dest 인스턴스 manifest 엔 added 하네스의 `@render` 항목이 없어(그건 최초 import 소관) render 는
    no-op — 그래서 **sed 채널이 반드시 치환해야** 하고(T-0424 확장자 열거 → 제외-판정 역전으로 `.toml`
    자동 편입), 이 게이트가 그 채널을 codex 까지 못박는다. **루트 doc(AGENTS.md/CLAUDE.md)도 스캔** —
    ADD_HARNESS_ADAPTER 값이 (네임스페이스 dirs, 루트 doc) 쌍이고 add_harness 가 루트 doc 도 배포하므로
    그 안 미치환이 네임스페이스-only 스캔을 통과하던 사각을 닫는다(MF2)."""
    dest = _build_live_instance(pm_import, tmp_path / f"{base}_add_{added}", base)
    pm_import.add_harness(dest, added, dry_run=False, source_root=REPO)
    # Ground truth (add_harness plan 실측 + `_engine_render_relpaths` 코드 근거·MF-B): 추가 하네스의
    #   **전** 어댑터 네임스페이스 dir 이 배포되고 각각 non-empty 다 — any-of(≥1) 아니라 **exact
    #   집합**(전 dir 실존+non-empty)을 단언한다.
    #     codex(dual .codex+.agents): .codex 7(agents .toml 4)·.agents 15(skills) **둘 다** 배포.
    #     opencode(.opencode): 15(agents 5). claude(.claude): 8 — 단 `.claude/agents`·`.claude/skills`
    #       는 **bare-@render** 엔진 리소스라 제외(target-owned 어댑터 파일만; @source·@target-owned 없는
    #       @render = _engine_render_relpaths 제외집합).
    #   codex `.codex/agents/*.toml`·`.agents/skills` 는 `@render **@source**` guest 어댑터라 제외집합에
    #   **없어** add_harness 가 레이다운한다(리뷰어 우려 지점 — 코드+실측으로 배포 확정, 미배포 아님).
    scan_roots = []
    for nd in HARNESS_ADAPTER_DIRS[added]:
        ns_root = dest / nd
        assert ns_root.is_dir(), f"add {added}(base={base}): 어댑터 네임스페이스 {nd} 미배포"
        ns_files = [p for p in ns_root.rglob("*") if p.is_file()]
        assert ns_files, f"add {added}(base={base}): 네임스페이스 {nd} 배포됐으나 파일 0(vacuous 방지)"
        scan_roots.append(ns_root)
    # codex dual 네임스페이스 핵심 산출물 landing 명시 — 리뷰어 우려(“@render @source 경로가 제외돼
    #   .codex/agents·.agents/skills 가 미배포될 수 있다”)를 exact landing 단언으로 반박·못박는다.
    if added == "codex":
        assert list((dest / ".codex" / "agents").glob("*.toml")), (
            f"add codex(base={base}): `.codex/agents/*.toml` 미landing — @render @source guest 어댑터 배포 실패")
        assert list((dest / ".agents" / "skills").glob("*/SKILL.md")), (
            f"add codex(base={base}): `.agents/skills/*/SKILL.md` 미landing — skill remap 배포 실패")
    # 스캔 = 배포된 전 네임스페이스 + 루트 doc(ADD_HARNESS_ADAPTER 쌍의 root doc·add_harness 도 배포).
    for ns_root in scan_roots:
        for token in OPERATIONAL_TOKENS:
            hits = _grep_token_files(ns_root, token, exclude_engine_docs=True)
            assert hits == [], (
                f"add {added}(base={base}): {ns_root.name}/ 에 {token} 미치환 잔존: {hits}")
    root_doc = dest / HARNESS_ROOT_DOC[added]
    assert root_doc.is_file(), f"add {added}(base={base}): 루트 doc {root_doc.name} 부재(배포 실패?)"
    leaked = [t for t in OPERATIONAL_TOKENS if t in root_doc.read_text(encoding="utf-8")]
    assert not leaked, f"add {added}(base={base}): 루트 doc {root_doc.name} 에 미치환 {leaked}"


def test_add_harness_apply_refresh_backs_up_and_stays_scoped(pm_import, tmp_path):
    """재실행(refresh): 네임스페이스 안 기존 어댑터는 백업 후 덮되, 스코프 밖은 여전히 불변."""
    dest = _build_live_instance(pm_import, tmp_path / "refresh_inst", "claude")
    pm_import.add_harness(dest, "opencode", dry_run=False, source_root=REPO)
    wiki_role = dest / ".project_manager" / "wiki" / "pm_role.md"
    role_before = wiki_role.read_bytes()

    # 로컬 어댑터 커스터마이즈 후 refresh — 백업 경로로 보존돼야 한다.
    agents_pm = dest / ".opencode" / "agents" / "pm.md"
    agents_pm.write_text("LOCAL CUSTOMIZE\n", encoding="utf-8")

    plan2 = pm_import.add_harness(dest, "opencode", dry_run=False, source_root=REPO)
    rels2 = _plan_relpaths(plan2, dest)
    # refresh 도 스코프 불변(네임스페이스 밖 0).
    assert all(_rel_in_namespace(r, "opencode") for r in rels2), rels2
    # 중앙 백업 디렉토리가 생기고 커스터마이즈가 그 안에 보존됐다.
    backups = list((dest / ".pm_import_backups").rglob("agents/pm.md"))
    assert backups, "refresh 가 기존 어댑터를 백업하지 않았다(.pm_import_backups)."
    assert any("LOCAL CUSTOMIZE" in b.read_text(encoding="utf-8") for b in backups), \
        "refresh 백업에 로컬 커스터마이즈 내용이 보존되지 않았다."
    # 스코프 밖 wiki 는 여전히 불변.
    assert wiki_role.read_bytes() == role_before, "refresh 가 wiki dev-state 를 건드렸다."


# ── guest @render 인스턴스 manifest 등재 (T-0456·:2726 no-op 해소) ──────────────

def test_add_harness_registers_guest_render_in_dest_manifest(pm_import, tmp_path):
    """add_harness(opencode→claude host) 가 guest `@render` 를 dest engine.manifest 에 **멱등 등재**
    (T-0456·pm_import:2726 no-op 해소). 등재로 render_managed_files·manifest-파생 overlay 스캔([[T-0431]])
    이 guest 를 커버한다. before=미등재 → after=등재 · 재실행 중복 0."""
    dest = _build_live_instance(pm_import, tmp_path / "guest_reg", "claude")
    manifest = dest / ".project_manager" / "engine.manifest"
    assert ".opencode/agents" not in manifest.read_text(encoding="utf-8")  # before: guest 미등재

    pm_import.add_harness(dest, "opencode", dry_run=False, source_root=REPO)
    managed = pm_import._render_managed_relpaths(dest)
    assert ".opencode/agents" in managed, "guest @render 미등재(render_managed 미포함)"
    assert ".opencode/pm-instructions.md" in managed
    # guest 는 host 인스턴스 소유 → @target-owned (pm_update 재렌더 skip·T-0456 MF-2).
    #   마커 **집합**으로 본다 — 직렬화는 `_manifest_entry_line` 소관이라 공백/순서를 문자로 박으면
    #   생성기 교체마다 무의미하게 깨진다(T-0574 에서 실제로 구분자가 바뀌었다).
    agents_entry = next(
        e for e in pm_import._load_pm_update().read_manifest(manifest)
        if str(e).replace("\\", "/") == ".opencode/agents")
    assert agents_entry.render and agents_entry.target_owned, \
        f"guest 렌더물 마커 누락: render={agents_entry.render} owned={agents_entry.target_owned}"
    # 멱등: 재실행(refresh)이 중복 등재 안 함(guest 라인·마커 절 각 1).
    begin = pm_import._load_pm_update()._GUEST_MANIFEST_BEGIN
    text1 = manifest.read_text(encoding="utf-8")
    pm_import.add_harness(dest, "opencode", dry_run=False, source_root=REPO)
    text2 = manifest.read_text(encoding="utf-8")
    assert [str(e) for e in pm_import._load_pm_update().read_manifest(manifest)].count(
        ".opencode/agents") == 1, "guest @render 라인 중복(멱등 위반)"
    assert text1.count(begin) == text2.count(begin) == 1, "guest 절(마커) 중복"


def test_add_harness_codex_registers_dual_namespace_guest_render(pm_import, tmp_path):
    """codex dual-namespace guest(`.codex/agents`·`.agents/skills`·ADR-0070) 둘 다 dest manifest 에
    `@render` 등재 (T-0456). **native 엔진 `.claude/skills`(host 소유)는 새로 등재하지 않는다** —
    before/after @render 델타로 native 경계를 조인다(reviewer suggestion·guest 파생이 host 를 삼키면 red)."""
    dest = _build_live_instance(pm_import, tmp_path / "codex_reg", "claude")
    before = pm_import._render_managed_relpaths(dest)
    pm_import.add_harness(dest, "codex", dry_run=False, source_root=REPO)
    after = pm_import._render_managed_relpaths(dest)
    assert ".codex/agents" in after and ".agents/skills" in after, sorted(after)
    # native `.claude/skills`(host @render)는 add-harness 델타에 **새로** 들어오지 않는다(host 소유).
    delta = after - before
    assert ".claude/skills" not in delta, f"codex 등재가 native .claude/skills 를 새로 추가: {sorted(delta)}"
    assert all(d.startswith(".codex/") or d.startswith(".agents/") for d in delta), \
        f"codex guest 델타가 codex 네임스페이스 밖을 포함: {sorted(delta)}"


@pytest.mark.parametrize("host,expect_registered", [("codex", True), ("claude", False)])
def test_add_harness_opencode_guest_cross_ns_skills_by_host(
        pm_import, pm_update, tmp_path, host, expect_registered):
    """opencode-as-guest 의 **cross-ns `.claude/skills`**(ADR-0065 네이티브 소비·`.opencode` 밖) 처리는
    host 실소유에 달렸다 (T-0456 R25 MF — codex R18 지시가 기능 요건으로 반전·red-첫).

    opencode flavor 는 `.claude/skills @render` 를 선언하지만 이는 `.opencode` namespace 밖이다. 옛
    namespace cap(R18)은 이 cross-ns 의존물을 복사·등재에서 빼, **codex host**(`.claude/skills` 미소유·
    `.agents/skills` 로 remap)에 opencode 를 추가하면 PM 스킬 채널이 통째로 파손됐다(기능 회귀). 수정
    전엔 codex host 에서 `.claude/skills/**` 미복사(SKILL.md 0)·guest 미등재였다.
      - codex host: `.claude/skills/**` 복사(SKILL.md 실재)·guest 절 등재·render_managed·토큰 치환·멱등.
      - claude host(대조군): `.claude/skills` host 소유(claude core `@render`)라 guest 미등재 —
        namespace cap 제거가 host-소유 경로까지 삼키지 않음을 못박는다(R17 차감 유지)."""
    dest = _build_live_instance(pm_import, tmp_path / f"{host}_add_oc", host)
    pm_import.add_harness(dest, "opencode", dry_run=False, source_root=REPO)
    mani = dest / ".project_manager" / "engine.manifest"
    block = _guest_block_paths(pm_update, mani)
    assert (".claude/skills" in block) is expect_registered, (
        f"host={host}: `.claude/skills` guest 등재={'.claude/skills' in block} (기대 {expect_registered})")
    if not expect_registered:
        return  # claude host 대조군 — host 소유 차감 유지(등재 X)면 충분.
    # codex host — cross-ns 스킬이 실제로 복사·렌더·관리되는지 전수 확인.
    skills_dir = dest / ".claude" / "skills"
    skill_files = list(skills_dir.rglob("SKILL.md"))
    assert skill_files, "codex host: cross-ns `.claude/skills/**` 미복사(SKILL.md 0·R25 MF 미해소)"
    assert ".claude/skills" in pm_import._render_managed_relpaths(dest), \
        "codex host: cross-ns `.claude/skills` render_managed 미커버(등재 실패)"
    leaked = _grep_token_files(skills_dir, "{{PROJECT_NAME}}")
    assert leaked == [], f"codex host: cross-ns 스킬 미렌더 토큰 잔존(복사만·렌더 누락): {leaked}"
    # guest = host 소유(add-harness 레이다운)라 @target-owned(pm_update 재렌더/재전파 skip).
    skills_entry = next(
        e for e in pm_update.read_manifest(mani)
        if str(e).replace("\\", "/") == ".claude/skills")
    assert skills_entry.render and skills_entry.target_owned, \
        f"cross-ns guest 마커 누락: render={skills_entry.render} owned={skills_entry.target_owned}"
    # 멱등: refresh 재실행이 manifest 를 안 바꾼다(cross-ns 항목이 매번 changed 로 churn 안 함·_this_ns R25).
    before = mani.read_text(encoding="utf-8")
    pm_import.add_harness(dest, "opencode", dry_run=False, source_root=REPO)
    assert mani.read_text(encoding="utf-8") == before, \
        "codex host: cross-ns 등재 멱등 위반(refresh 마다 manifest churn·_this_ns cross-ns 누락)"


def test_add_harness_two_guests_share_single_header(pm_import, tmp_path):
    """같은 host 에 guest 2종 순차 add(opencode→codex)면 **단일 guest 절**(마커 하나) 아래 두 하네스
    라인이 모인다 (reviewer suggestion·durable 박제·T-0456 MF-1 병합)."""
    dest = _build_live_instance(pm_import, tmp_path / "two_guest", "claude")
    pm_import.add_harness(dest, "opencode", dry_run=False, source_root=REPO)
    pm_import.add_harness(dest, "codex", dry_run=False, source_root=REPO)
    manifest = dest / ".project_manager" / "engine.manifest"
    pu = pm_import._load_pm_update()
    text = manifest.read_text(encoding="utf-8")
    assert text.count(pu._GUEST_MANIFEST_BEGIN) == 1, "guest 절이 하나가 아님(단일 헤더 위반)"
    block = pu._extract_guest_manifest_block(text)
    block_paths = {ln.split()[0] for ln in block.splitlines()
                   if ln.strip() and not ln.strip().startswith("#")}
    assert {".opencode/agents", ".codex/agents", ".agents/skills"} <= block_paths, block_paths


def test_add_harness_dry_run_preview_subtracts_existing(pm_import, tmp_path, capsys):
    """add-harness `--dry-run` preview 가 **dest 기존 등록분을 차감**해 실제 병합과 같은 diff 를 보인다
    (T-0456 R14 suggestion·`_guest_render_to_add` 공유). fresh=신규 등재분 표시, 이미 등재=0건(무표시)."""
    dest = _build_live_instance(pm_import, tmp_path / "preview", "claude")
    pm_import.add_harness(dest, "codex", dry_run=True, source_root=REPO)  # fresh dry-run
    fresh = capsys.readouterr().out
    assert "guest 절 등재 예정" in fresh and ".codex/agents" in fresh, fresh
    pm_import.add_harness(dest, "codex", dry_run=False, source_root=REPO)  # 실제 등재
    capsys.readouterr()
    pm_import.add_harness(dest, "codex", dry_run=True, source_root=REPO)  # refresh dry-run
    refresh = capsys.readouterr().out
    assert "등재 예정" not in refresh, f"기등록분 차감 안 됨(preview churn): {refresh}"


def test_add_harness_subtracts_core_owned_ancestor_paths(pm_import, pm_update, tmp_path):
    """add-측 대칭(T-0456 R16): core 가 이미 **상위** 경로(`.opencode` 네임스페이스)를 `@render` 로
    소유하면 add-harness 가 `.opencode/agents` 를 guest 등재하지 않는다(경로-포함 차감) — 안 그러면 더
    구체적인 guest `@target-owned` 가 core 를 가려 업데이트 중단(red-첫). R15 재부착 차감과 같은
    `_path_owned_by` 헬퍼 공유. 옛 정확-일치 차감은 상위 소유를 못 봐 guest 를 등재했다."""
    dest = _build_live_instance(pm_import, tmp_path / "core_owns", "claude")
    manifest = dest / ".project_manager" / "engine.manifest"
    # core 가 상위 `.opencode`(네임스페이스 전체)를 이미 @render 로 소유(선승격 시뮬).
    manifest.write_text(
        manifest.read_text(encoding="utf-8").rstrip()
        + "\n.opencode    @render @source=templates/opencode/.opencode\n", encoding="utf-8")
    # 유닛: guest 등재 차감 — `.opencode/*` 는 전부 core(`.opencode`) 소유라 added 0(정확-일치였으면 미차감).
    guest = [".opencode/agents    @render @target-owned",
             ".opencode/pm-instructions.md    @render @target-owned"]
    sync = pm_import._guest_render_sync_plan(dest, guest, (".opencode",))
    assert sync["added"] == [] and sync["removed"] == [], f"core 상위 소유 경로 차감 안 됨(R16): {sync}"
    # 롤아웃: add_harness → `.opencode` guest 미등재 + 이중 등재 0(core owner 보존).
    pm_import.add_harness(dest, "opencode", dry_run=False, source_root=REPO)
    block = pm_update._extract_guest_manifest_block(manifest.read_text(encoding="utf-8"))
    block_paths = {ln.split()[0] for ln in (block.splitlines() if block else [])
                   if ln.strip() and not ln.strip().startswith("#")}
    assert not any(p.startswith(".opencode") for p in block_paths), \
        f"core 가 `.opencode` 소유인데 guest `.opencode/*` 등재됨(shadow·이중 등재): {block_paths}"
    ents = [str(e) for e in pm_update.read_manifest(manifest)]
    assert ents.count(".opencode/agents") <= 1, "add-harness 가 `.opencode/agents` 이중 등재"


def _guest_block_paths(pm_update, manifest_path) -> set:
    block = pm_update._extract_guest_manifest_block(manifest_path.read_text(encoding="utf-8"))
    return {ln.split()[0] for ln in (block.splitlines() if block else [])
            if ln.strip() and not ln.strip().startswith("#")}


def test_add_harness_claude_as_guest_registered_on_nonclaude_hosts(pm_import, pm_update, tmp_path):
    """N×N 역방향(T-0456 R17 MF): claude-as-guest 가 non-claude host 에 dest manifest 등재된다.

    옛 flavor-native 차감은 claude 의 bare `@render` `.claude/*` 를 전부 native 로 봐 등재 0 이었다
    (pm_update 영구 관리 불능). **대조군**: opencode host 는 `.claude/skills`(ADR-0065 native 소비)를
    이미 소유 → guest 등재 안 함(정확히 그것만)·`.claude/agents` 만 등재. codex host 는 `.agents/skills`
    를 소유(`.claude/skills` 미소유) → `.claude/agents`·`.claude/skills` 둘 다 guest 등재."""
    # opencode host — .claude/agents 등재 O · .claude/skills 등재 X(host 소유).
    oc = _build_live_instance(pm_import, tmp_path / "oc", "opencode")
    pm_import.add_harness(oc, "claude", dry_run=False, source_root=REPO)
    oc_mani = oc / ".project_manager" / "engine.manifest"
    oc_render = {str(e) for e in pm_update.read_manifest(oc_mani) if getattr(e, "render", False)}
    assert ".claude/agents" in oc_render, "opencode host 에 claude-as-guest `.claude/agents` 미등재(R17 MF)"
    oc_block = _guest_block_paths(pm_update, oc_mani)
    assert ".claude/agents" in oc_block, oc_block
    assert ".claude/skills" not in oc_block, f"host 소유 `.claude/skills` 를 guest 로 오등재: {oc_block}"
    # .claude/agents 이중 등재 0 (guest 만·core 미소유).
    assert [str(e) for e in pm_update.read_manifest(oc_mani)].count(".claude/agents") == 1

    # codex host — .claude/skills 미소유(.agents/skills 소유) → .claude/skills 도 guest 등재.
    cx = _build_live_instance(pm_import, tmp_path / "cx", "codex")
    pm_import.add_harness(cx, "claude", dry_run=False, source_root=REPO)
    cx_block = _guest_block_paths(pm_update, cx / ".project_manager" / "engine.manifest")
    assert {".claude/agents", ".claude/skills"} <= cx_block, \
        f"codex host(.claude/skills 미소유)에 claude guest 미등재: {cx_block}"


def test_add_harness_claude_guest_survives_pm_update_roundtrip(pm_import, pm_update, tmp_path, monkeypatch):
    """claude-as-guest 가 opencode host self-update 라운드트립을 잔존(절 보존)·무churn·@render 유지
    (T-0456 R17 + R14/R15 대칭)."""
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: None)
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)
    oc = _build_live_instance(pm_import, tmp_path / "oc_rt", "opencode")
    pm_import.add_harness(oc, "claude", dry_run=False, source_root=REPO)
    mani = oc / ".project_manager" / "engine.manifest"
    assert ".claude/agents" in _guest_block_paths(pm_update, mani)

    monkeypatch.setattr(pm_update, "REPO", oc)
    assert pm_update.main(["--from", str(REPO)]) == 0, "opencode host self-update rc≠0"
    # 절 잔존 + @render 유지(render/scan 커버).
    assert ".claude/agents" in _guest_block_paths(pm_update, mani), "pm_update 가 claude guest 절 제거(R17 미해소)"
    assert ".claude/agents" in {str(e) for e in pm_update.read_manifest(mani) if getattr(e, "render", False)}
    # 2차 sync 무churn — engine.manifest 가 plan changes 에 없음.
    manifest = pm_update.read_manifest(pm_update.resolve_manifest_for_dest(oc, REPO))
    sh = pm_update.resolve_manifest_selfheal(oc, REPO)
    if sh["manifest"] is not None:
        manifest = sh["manifest"]
    changes, _m = pm_update.plan(REPO, manifest, dest_root=oc)
    assert not any(str(r).replace("\\", "/") == ".project_manager/engine.manifest"
                   for r, _s, _d, _k in changes), "claude guest 절로 engine.manifest 영구 churn"


def test_guest_manifest_lines_emits_all_flavor_render(pm_import, tmp_path):
    """`_guest_manifest_lines` 는 flavor manifest 의 `@render` **선언 전부**를 후보로 방출한다
    (T-0456 R25·옛 namespace cap R18 **제거**).

    **R18 지시가 R25 에서 기능 요건으로 반전**: R18(R19)은 등재 후보를 복사 namespace 로 제한해
    cross-ns 경로(`.other @render`)를 뺐으나, opencode 의 `.claude/skills @render`(ADR-0065·`.opencode`
    밖 네이티브 소비)가 그 cap 에 걸려 codex host 에서 PM 스킬이 파손됨을 R25 가 포착했다 — **flavor
    `@render` 선언 자체가 경계**다(flavor 는 자기 footprint 만 선언). 이제 namespace-레벨(`.fourth`)·
    하위(`.fourth/agents`)·cross-ns(`.other`) `@render` 를 **모두** 후보로 낸다(host 실소유 차감은
    downstream `_guest_render_sync_plan`).

    **T-0574**: 비-@render 는 더 이상 무조건 제외가 아니라 **복사 술어**(`_in_adapter_namespace`)로
    갈린다 — namespace 안이면 엔진 행으로 등재하고(`@source … @target-owned`), 밖이거나 host 실소유면
    제외한다. 가짜 4번째 하네스 fixture(T-0429)."""
    tmpl = tmp_path / "templates" / "fourth"
    mani = tmpl / ".project_manager" / "engine.manifest"
    mani.parent.mkdir(parents=True)
    # namespace-레벨 @render + 하위 @render + cross-ns @render
    #   + namespace 안 비-@render(엔진 행) + namespace 밖 비-@render(host 엔진·제외).
    mani.write_text(
        ".fourth    @render\n"
        ".fourth/agents    @render\n"
        ".other    @render\n"
        ".fourth/plain.txt    @source=templates/fourth/.fourth/plain.txt\n"
        ".project_manager/tools/board.py\n",
        encoding="utf-8")
    lines = pm_import._guest_manifest_lines(tmpl, (".fourth",), "FOURTH.md", set())
    by_path = {ln.split()[0]: ln for ln in lines}
    # @render 선언 전부 + namespace 안 엔진 행. namespace 밖 엔진 경로(host 소유)는 제외.
    assert set(by_path) == {".fourth", ".fourth/agents", ".other", ".fourth/plain.txt"}, by_path
    # cross-ns(`.other`)도 방출(R25 — 옛 namespace cap 이면 빠졌다·이게 `.claude/skills` 파손 클래스).
    assert "@render" in by_path[".other"] and "@target-owned" in by_path[".other"]
    # 엔진 행은 `@render` 없이 `@source` + `@target-owned` — 한 줄의 @render 유무가 채널을 가른다.
    engine_line = by_path[".fourth/plain.txt"]
    assert "@render" not in engine_line and "@target-owned" in engine_line, engine_line
    assert "@source=templates/fourth/.fourth/plain.txt" in engine_line, engine_line
    # host 가 이미 소유하면(`dest_owned`) 엔진 행은 빠진다 — 복사 술어와 같은 차감.
    owned = pm_import._guest_manifest_lines(
        tmpl, (".fourth",), "FOURTH.md", {".fourth/plain.txt"})
    assert ".fourth/plain.txt" not in {ln.split()[0] for ln in owned}, owned


def test_add_harness_refresh_syncs_removes_stale_guest(
        pm_import, pm_update, tmp_path, monkeypatch, capsys):
    """refresh 가 upstream flavor 에서 **폐기된 guest 라인을 제거**한다 (T-0456 R20 MF·add-only→sync).

    옛 refresh 는 추가만 해서, upstream 에서 삭제/`@render` 해제된 경로가 pm_update 보존으로 영구
    render/lint 관리로 남았다. red-첫: guest 절에 폐기 라인(현행 flavor 없음) 심고 refresh → 제거·현행
    잔존·**타 하네스 절 불변**·dry-run preview '제거 예정' 표시·roundtrip(pm_update) 정상."""
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: None)
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)
    dest = _build_live_instance(pm_import, tmp_path / "refresh_sync", "claude")
    pm_import.add_harness(dest, "opencode", dry_run=False, source_root=REPO)
    pm_import.add_harness(dest, "codex", dry_run=False, source_root=REPO)  # 타 하네스 guest.
    mani = dest / ".project_manager" / "engine.manifest"
    # 폐기 라인 주입 (opencode namespace·현행 flavor 에 없음).
    end = pm_update._GUEST_MANIFEST_END
    stale = ".opencode/obsolete    @render @target-owned"
    mani.write_text(mani.read_text(encoding="utf-8").replace(end, stale + "\n" + end),
                    encoding="utf-8")
    assert ".opencode/obsolete" in _guest_block_paths(pm_update, mani)  # 사전조건.
    capsys.readouterr()

    # dry-run preview 에 '제거 예정' 표시.
    pm_import.add_harness(dest, "opencode", dry_run=True, source_root=REPO)
    out = capsys.readouterr().out
    assert "제거 예정" in out and ".opencode/obsolete" in out, out

    # refresh opencode → stale 제거·현행 잔존·codex 불변.
    pm_import.add_harness(dest, "opencode", dry_run=False, source_root=REPO)
    after = _guest_block_paths(pm_update, mani)
    assert ".opencode/obsolete" not in after, f"폐기 guest 미제거(add-only 잔재·R20): {sorted(after)}"
    assert ".opencode/agents" in after, "현행 opencode guest 유실"
    assert {".codex/agents", ".agents/skills"} <= after, f"타 하네스 codex guest 변경(sync 오염): {sorted(after)}"

    # roundtrip: pm_update 후 절 잔존·stale 재출현 없음.
    monkeypatch.setattr(pm_update, "REPO", dest)
    assert pm_update.main(["--from", str(REPO)]) == 0
    assert ".opencode/obsolete" not in _guest_block_paths(pm_update, mani)
    assert pm_update._extract_guest_manifest_block(mani.read_text(encoding="utf-8")) is not None


def test_add_harness_refresh_corrects_stale_marker(pm_import, pm_update, tmp_path, monkeypatch):
    """refresh 가 같은 경로의 **마커 교정**을 감지·적용한다 (T-0456 R21 MF).

    경로 집합만 비교하면 `.opencode/agents @render` → 목표 `@render @target-owned` 교정이 added=[]·
    removed=[]·changed=False 로 스킵돼 pm_update 가 non-target-owned 누락으로 rc=2 실패할 수 있다.
    red-첫: 구 마커(@target-owned 제거) 심고 refresh → 목표 마커로 교체·changed 감지·pm_update rc0."""
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: None)
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)
    dest = _build_live_instance(pm_import, tmp_path / "marker_fix", "claude")
    pm_import.add_harness(dest, "opencode", dry_run=False, source_root=REPO)
    mani = dest / ".project_manager" / "engine.manifest"
    # 마커 손상: `.opencode/agents` 에서 @target-owned 제거(구 마커·경로 동일).
    damaged = [
        ".opencode/agents    @render" if line.split()[:1] == [".opencode/agents"] else line
        for line in mani.read_text(encoding="utf-8").splitlines()
    ]
    mani.write_text("\n".join(damaged) + "\n", encoding="utf-8")
    assert ".opencode/agents    @render\n" in mani.read_text(encoding="utf-8"), \
        "마커 손상 주입 실패(픽스처 전제 붕괴)"
    gl = pm_import._guest_manifest_lines(
        REPO / "templates" / "opencode", (".opencode",), "AGENTS.md",
        pm_import._dest_manifest_core_paths(dest))
    plan = pm_import._guest_render_sync_plan(dest, gl, (".opencode",))
    assert plan["changed"] and not plan["added"] and not plan["removed"], \
        f"마커 교정 미감지(경로 집합만 비교·R21): {plan}"

    pm_import.add_harness(dest, "opencode", dry_run=False, source_root=REPO)  # refresh → 교정.
    block = pm_update._extract_guest_manifest_block(mani.read_text(encoding="utf-8"))
    ags = [ln.strip() for ln in block.splitlines()
           if ln.strip() and not ln.strip().startswith("#") and ln.split()[0] == ".opencode/agents"]
    assert ags and all("@target-owned" in ln for ln in ags), f"마커 미교정: {ags}"
    monkeypatch.setattr(pm_update, "REPO", dest)
    assert pm_update.main(["--from", str(REPO)]) == 0, \
        "마커 교정 후 pm_update rc≠0(non-target-owned 누락?)"


def test_add_harness_guest_registration_warns_when_no_manifest(pm_import, tmp_path, capsys):
    """manifest 부재 dest 에서 guest 등재 생략 시 **명시 경고**(복사됐으나 render/lint 관리 밖·R21
    suggestion) — 조용한 생략 금지."""
    d = tmp_path / "nomani"
    (d / ".opencode" / "agents").mkdir(parents=True)
    res = pm_import._append_guest_render_to_manifest(
        d, [".opencode/agents    @render @target-owned"], (".opencode",))
    assert res == {"added": [], "removed": []}
    assert "render/lint 관리 밖" in capsys.readouterr().err


def test_pm_update_rerenders_claude_guest_card_from_conf(
        pm_import, pm_update, tmp_path, monkeypatch):
    """claude-as-guest 카드는 self-update 가 **local.conf 로 다시 렌더**한다(손편집은 되돌아간다).

    카드의 model 은 `delegate.<role>.model` 의 렌더 파생물이고 단일 진실은 local.conf 다 — guest 를
    계획에서 빼면 conf 를 바꿔도 카드가 설치 시점 값으로 남는다(채택자 실측: 카드 model ↔ conf 영구
    불일치). 그래서 이 축은 "손편집 보존" 이 아니라 "conf 로 수렴" 을 못박는다."""
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: None)
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)
    dest = _build_live_instance(pm_import, tmp_path / "guest_edit", "opencode")
    pm_import.add_harness(dest, "claude", dry_run=False, source_root=REPO)
    agent = dest / ".claude" / "agents" / "developer.md"
    assert agent.is_file(), "claude-as-guest `.claude/agents/developer.md` 미복사"
    conf = dest / ".project_manager" / "local.conf"
    conf.write_text(
        conf.read_text(encoding="utf-8")
        + "delegate.developer.harness=claude\n"
        + "delegate.developer.model=sonnet\n",
        encoding="utf-8", newline="\n")
    agent.write_text("LOCAL ADOPTER GUEST EDIT\n", encoding="utf-8")  # 채택자 로컬 수정.

    monkeypatch.setattr(pm_update, "REPO", dest)
    assert pm_update.main(["--from", str(REPO)]) == 0
    text = agent.read_text(encoding="utf-8")
    assert text.startswith("---\n"), \
        "guest 카드가 재렌더되지 않았다(손편집이 conf 를 이긴다·설치 시점 동결)"
    assert 'model: "sonnet"' in text, "재렌더 산출이 conf 해소값이 아니다"


def test_add_harness_renders_preexisting_token_form_guest_file(pm_import, tmp_path):
    """add_harness 가 **사전 배치된 token-form guest 파일**(copy plan 에서 byte-identical 로 skip)도
    렌더한다 (T-0456 R22 MF-3). 미포함이면 미치환(토큰 잔존)으로 남았다."""
    dest = _build_live_instance(pm_import, tmp_path / "preexist", "claude")
    # template source 와 byte-identical 한 token-form 파일 사전 배치 → copy plan 이 skip.
    src = REPO / "templates" / "opencode" / ".opencode" / "agents" / "architect.md"
    pre = dest / ".opencode" / "agents" / "architect.md"
    pre.parent.mkdir(parents=True)
    shutil.copy2(src, pre)
    assert "{{PROJECT_NAME}}" in pre.read_text(encoding="utf-8")  # token-form 사전조건.
    pm_import.add_harness(dest, "opencode", dry_run=False, source_root=REPO)
    assert "{{PROJECT_NAME}}" not in pre.read_text(encoding="utf-8"), \
        "사전 배치 guest 파일 미렌더(토큰 잔존·R22 MF-3)"


def test_add_harness_render_scope_excludes_other_guest_adopter_files(pm_import, tmp_path):
    """add-harness 렌더 범위가 **이번 하네스 byte-identical 미복사 파일**로 한정 (T-0456 R23 MF-2).

    R22 의 `_existing_files_under(현재 guest 절 전체)`는 타 하네스 guest·adopter 커스텀 파일(내용 상이·
    copied)까지 무백업 치환·렌더 대상으로 삼았다. red-첫: 타 하네스(codex) guest 를 adopter 가 커스텀한 뒤
    opencode add → 그 커스텀 파일이 **불변**(과확장 봉쇄)."""
    dest = _build_live_instance(pm_import, tmp_path / "noover", "claude")
    pm_import.add_harness(dest, "codex", dry_run=False, source_root=REPO)  # codex guest 먼저.
    cust = dest / ".codex" / "agents" / "developer.toml"
    cust.write_text("ADOPTER CUSTOM has {{PROJECT_NAME}} token\n", encoding="utf-8")  # 내용 상이.
    pm_import.add_harness(dest, "opencode", dry_run=False, source_root=REPO)  # opencode add.
    assert cust.read_text(encoding="utf-8") == "ADOPTER CUSTOM has {{PROJECT_NAME}} token\n", \
        "opencode add 가 타 하네스(codex) adopter 커스텀 파일을 과확장 치환(R23 MF-2 미해소)"


def test_byte_identical_skipped_rejects_unsafe_paths(pm_import, tmp_path):
    """`_is_safe_dest_path` 가 `..` 탈출·symlink(조상 포함) 경로를 거부한다 (T-0456 R23 MF-3·repo 밖
    순회/치환 방지). `_byte_identical_skipped` 는 그 위에서 조작 경로를 skip 한다."""
    dest = tmp_path / "inst"
    dest.mkdir()
    assert pm_import._is_safe_dest_path(dest, Path(".opencode/agents/x.md"))
    assert not pm_import._is_safe_dest_path(dest, Path("../etc/passwd"))
    # symlink 조상 → 거부(링크 follow 로 repo 밖 쓰기 방지).
    (dest / ".opencode").symlink_to(tmp_path / "outside")
    assert not pm_import._is_safe_dest_path(dest, Path(".opencode/agents/x.md"))


def test_add_harness_rejects_symlink_manifest_before_copy(pm_import, tmp_path):
    """add-harness 가 engine.manifest 가 **repo-밖 지향 symlink** 면 **복사 시작 전 거부**한다
    (T-0456 R24). symlink 를 따라 write 하면 repo 밖 파일을 덮으므로 fail-loud·부분 적용 0·외부 파일 불변.

    red-첫: manifest 를 외부 파일 지향 symlink 로 바꾼 fixture → add-harness RuntimeError·`.opencode` 미생성
    ·외부 파일 무변."""
    dest = _build_live_instance(pm_import, tmp_path / "symmani", "claude")
    ext = tmp_path / "OUTSIDE_SECRET.txt"
    ext.write_text("ORIGINAL EXTERNAL\n", encoding="utf-8")
    mani = dest / ".project_manager" / "engine.manifest"
    mani.unlink()
    mani.symlink_to(ext)  # manifest → repo-밖 파일.
    with pytest.raises(RuntimeError, match="안전"):
        pm_import.add_harness(dest, "opencode", dry_run=False, source_root=REPO)
    assert ext.read_text(encoding="utf-8") == "ORIGINAL EXTERNAL\n", "외부 파일이 덮여짐(R24 미해소)"
    assert not (dest / ".opencode").exists(), "복사 시작됨(부분 적용·복사 전 거부 위반)"


def test_append_guest_render_rejects_symlink_manifest(pm_import, tmp_path):
    """`_append_guest_render_to_manifest` 백스톱: symlink manifest 직접 write 거부 (T-0456 R24·TOCTOU/
    직접 호출). 외부 파일 불변."""
    dest = tmp_path / "inst"
    (dest / ".project_manager").mkdir(parents=True)
    ext = tmp_path / "outside.manifest"
    ext.write_text("EXTERNAL\n", encoding="utf-8")
    (dest / ".project_manager" / "engine.manifest").symlink_to(ext)
    with pytest.raises(RuntimeError, match="안전"):
        pm_import._append_guest_render_to_manifest(
            dest, [".opencode/agents    @render @target-owned"], (".opencode",))
    assert ext.read_text(encoding="utf-8") == "EXTERNAL\n"


def test_pm_update_updates_promoted_guest_path_in_single_run(
        pm_import, pm_update, tmp_path, monkeypatch):
    """guest 경로가 **후순위 flavor** upstream core로 승격돼도 1차 pm_update에서 갱신된다.

    guest 필터가 승격분까지 plan에서 제거해 첫 실행이 안 갱신(2회 필요)하던 것을, 실제 승격된
    선택 flavor 합집합 전체로 차감한다. primary만 보는 구현은 opencode 후순위의 승격분을 놓친다."""
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: None)
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)
    fw = tmp_path / "fw"
    fw.mkdir()
    for sub in (".project_manager", "templates", ".claude", ".github"):
        shutil.copytree(REPO / sub, fw / sub)
    shutil.copy2(REPO / ".gitattributes", fw / ".gitattributes")
    dest = tmp_path / "inst"
    assert pm_import.main(["--new", str(dest), "--harness", "claude,opencode", "--name", "X",
                           "--from", str(fw)]) == 0
    pm_import.add_harness(dest, "codex", dry_run=False, source_root=fw)  # codex guest 절.
    # 승격: 선택 순서상 후순위인 opencode flavor core에 `.codex/agents` 추가 + 소스 sentinel.
    fm = fw / "templates" / "opencode" / ".project_manager" / "engine.manifest"
    fm.write_text(fm.read_text(encoding="utf-8")
                  + "\n.codex/agents    @render @source=templates/codex/.codex/agents\n",
                  encoding="utf-8")
    csrc = fw / "templates" / "codex" / ".codex" / "agents" / "developer.toml"
    csrc.write_text(csrc.read_text(encoding="utf-8") + "\n# PROMOTED_SENTINEL_T0456\n",
                    encoding="utf-8")
    _track_source_tree(fw)

    monkeypatch.setattr(pm_update, "REPO", dest)
    assert pm_update.main(["--from", str(fw)]) == 0
    assert "PROMOTED_SENTINEL_T0456" in (
        dest / ".codex" / "agents" / "developer.toml").read_text(encoding="utf-8"), \
        "승격분이 1차 self-update 에서 미갱신(R23 MF-1·guest 필터 과적용)"


def test_pm_update_diverged_selfheal_keeps_local_only_core_declaration(
        pm_import, pm_update, tmp_path, monkeypatch, capsys):
    """diverged로 승격이 거부되면 로컬 전용 core 선언(과 그 파일)이 보존된다.

    guest 렌더물은 이 축이 아니다 — 절이 소유를 선언한 경로라 update 채널이 재렌더한다(아래 단언)."""
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: None)
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)
    fw = tmp_path / "fw-diverged"
    fw.mkdir()
    for sub in (".project_manager", "templates", ".claude", ".github"):
        shutil.copytree(REPO / sub, fw / sub)
    shutil.copy2(REPO / ".gitattributes", fw / ".gitattributes")
    # local-only core 선언이 missing(rc=2)이 되지 않도록 source에는 두되 선택 manifest에는 등재하지 않는다.
    custom_source = fw / ".local" / "custom-core"
    custom_source.parent.mkdir(parents=True)
    custom_source.write_text("custom core\n", encoding="utf-8")
    _track_source_tree(fw)

    dest = tmp_path / "diverged-inst"
    assert pm_import.main([
        "--new", str(dest), "--harness", "claude,opencode", "--name", "Diverged Guest",
        "--from", str(fw),
    ]) == 0
    capsys.readouterr()
    manifest = dest / ".project_manager" / "engine.manifest"
    begin, end = pm_update._GUEST_MANIFEST_BEGIN, pm_update._GUEST_MANIFEST_END
    manifest.write_text(
        manifest.read_text(encoding="utf-8").rstrip()
        + "\n.local/custom-core\n\n"
        + begin + "\n.claude/skills    @render @target-owned\n" + end + "\n",
        encoding="utf-8",
    )
    local_custom = dest / ".local" / "custom-core"
    local_custom.parent.mkdir(parents=True, exist_ok=True)
    local_custom.write_text("custom core\n", encoding="utf-8")
    guest_file = dest / ".claude" / "skills" / "pm-dev-delegate" / "SKILL.md"
    user_bytes = b"# USER GUEST OVERRIDE\n"
    guest_file.write_bytes(user_bytes)

    preview = pm_update.resolve_manifest_selfheal(dest, fw)
    assert preview["status"] == "diverged"
    assert ".local/custom-core" in preview["removed"]
    assert preview["manifest"] is None
    capsys.readouterr()

    monkeypatch.setattr(pm_update, "REPO", dest)
    monkeypatch.setenv("PM_NONINTERACTIVE", "1")
    assert pm_update.main(["--from", str(fw)]) == 0

    # guest 렌더물은 diverged 여부와 무관하게 update 채널이 재렌더한다(손편집은 되돌아간다).
    #   이 테스트가 지키는 축은 **local-only core 선언 보존**(승격 거부)이지 guest 파일 불가침이 아니다.
    assert guest_file.read_bytes() != user_bytes, \
        "guest 렌더물이 재렌더를 안 받았다(설치 시점 사본 동결)"
    assert (dest / ".local" / "custom-core").read_text(encoding="utf-8") == "custom core\n", \
        "diverged 로 승격을 거부했는데 로컬 전용 core 파일이 훼손됐다"


def test_add_harness_guest_render_survives_pm_update(pm_import, pm_update, tmp_path, monkeypatch):
    """MF-1 roundtrip: add_harness → pm_update self-update 후에도 guest 절이 잔존하고 manifest-파생
    render/scan 이 계속 guest 를 본다 (engine.manifest self-prop overwrite 보존·T-0456). 함께
    렌더물의 의도된 동작(손으로 박은 model override 는 다음 흡수가 conf 파생물로 되돌린다)도 본다."""
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: None)
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)
    dest = _build_live_instance(pm_import, tmp_path / "roundtrip", "claude")
    pm_import.add_harness(dest, "codex", dry_run=False, source_root=REPO)
    manifest = dest / ".project_manager" / "engine.manifest"
    pu = pm_import._load_pm_update()
    # 사용자 model override 심기 — 카드 model 의 단일 진실은 local.conf 라 흡수가 되돌린다.
    agent = dest / ".codex" / "agents" / "developer.toml"
    agent.write_text(agent.read_text(encoding="utf-8").rstrip() + '\nmodel = "USER/OVERRIDE"\n',
                     encoding="utf-8")
    assert pu._extract_guest_manifest_block(manifest.read_text(encoding="utf-8")) is not None
    role = dest / ".project_manager" / "wiki" / "pm_role.md"
    before_update = role.read_text(encoding="utf-8")
    assert "(claude)" in before_update and "(codex)" in before_update

    monkeypatch.setattr(pm_update, "REPO", dest)
    assert pm_update.main(["--from", str(REPO)]) == 0, "self-update rc≠0"

    # MF-1: guest 절 잔존 + @render 여전히 파싱(render/scan 커버 유지).
    after = manifest.read_text(encoding="utf-8")
    assert pu._extract_guest_manifest_block(after) is not None, "pm_update 가 guest 절을 지웠다(MF-1 미해소)"
    render_paths = {str(e) for e in pu.read_manifest(manifest) if getattr(e, "render", False)}
    assert {".codex/agents", ".agents/skills"} <= render_paths, sorted(render_paths)
    # 카드 model 의 단일 진실은 local.conf 다 — 손으로 박은 override 는 다음 흡수가 되돌린다
    #   (codex host 의 core `.codex/agents @render` 와 같은 거동·guest 라고 예외를 두지 않는다).
    assert "USER/OVERRIDE" not in agent.read_text(encoding="utf-8"), \
        "guest 렌더물이 재렌더를 안 받았다(카드 손편집이 conf 를 이긴다)"
    after_update = role.read_text(encoding="utf-8")
    assert "(claude)" in after_update and "(codex)" in after_update
    assert "`$pm-bootstrap`(codex)" in after_update
    assert pm_update.main(["--from", str(REPO)]) == 0, "2차 self-update rc≠0"
    assert role.read_text(encoding="utf-8") == after_update, \
        "2차 self-update가 guest 포함 공유 wiki 표기를 다시 바꿈"


def test_render_managed_files_covers_guest_when_registered(pm_import, tmp_path):
    """render_managed_files 가 dest manifest 에 등재된 guest `@render` 를 **실제 렌더**한다 (T-0456·
    :2726 no-op 해소·실측). 미등재면 no-op(0 변경·옛 동작), 등재면 토큰→값 렌더(≥1). add_harness 가
    이 등재를 하므로 guest 도 렌더된다."""
    dest = tmp_path / "inst"
    (dest / ".project_manager").mkdir(parents=True)
    (dest / ".project_manager" / "local.conf").write_text("project_name=X\n", encoding="utf-8")
    guest = dest / ".opencode" / "agents" / "x.md"
    guest.parent.mkdir(parents=True)
    guest.write_text("model: {{PROJECT_NAME}}\n", encoding="utf-8")
    copied = {Path(".opencode/agents/x.md")}
    subs = {"{{PROJECT_NAME}}": "Rendered"}

    # red: guest 미등재 manifest → render no-op(0·:2726 옛 동작), 토큰 미치환 잔존.
    manifest = dest / ".project_manager" / "engine.manifest"
    manifest.write_text(".claude/agents    @render\n", encoding="utf-8")
    assert pm_import.render_managed_files(dest, subs, copied) == 0
    assert "{{PROJECT_NAME}}" in guest.read_text(encoding="utf-8")

    # green: guest @render 등재 → 실제 렌더(변경 ≥1·토큰→값).
    manifest.write_text(".claude/agents    @render\n.opencode/agents    @render\n", encoding="utf-8")
    assert pm_import.render_managed_files(dest, subs, copied) >= 1, \
        "guest 등재 후에도 render_managed_files 가 guest 를 렌더 안 함(no-op 미해소)."
    rendered = guest.read_text(encoding="utf-8")
    assert "Rendered" in rendered and "{{PROJECT_NAME}}" not in rendered


@pytest.mark.parametrize("rel", (".codex/config.toml", ".codex/hooks.json"))
def test_add_harness_codex_seeds_absent_instance_owned_config(pm_import, tmp_path, rel):
    """첫 codex add 는 없는 adopter config/hook만 template seed 한다."""
    dest = _build_live_instance(pm_import, tmp_path / f"codex_seed_{Path(rel).stem}", "claude")

    plan = pm_import.add_harness(dest, "codex", dry_run=False, source_root=REPO)

    seeded = dest / rel
    assert seeded.read_bytes() == (REPO / "templates" / "codex" / rel).read_bytes()
    assert rel in _plan_relpaths(plan, dest)


@pytest.mark.parametrize("rel", (".codex/config.toml", ".codex/hooks.json"))
def test_add_harness_codex_refresh_quietly_preserves_identical_instance_config(
    pm_import, tmp_path, capsys, rel,
):
    """동일한 adopter config/hook은 refresh copy·backup·안내 없이 quiet skip 한다."""
    dest = _build_live_instance(pm_import, tmp_path / f"codex_same_{Path(rel).stem}", "claude")
    existing = dest / rel
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_bytes((REPO / "templates" / "codex" / rel).read_bytes())
    capsys.readouterr()

    plan = pm_import.add_harness(dest, "codex", dry_run=False, source_root=REPO)

    assert existing.read_bytes() == (REPO / "templates" / "codex" / rel).read_bytes()
    assert rel not in _plan_relpaths(plan, dest)
    assert not list((dest / ".pm_import_backups").rglob(Path(rel).name))
    assert "수동 반영" not in capsys.readouterr().out


@pytest.mark.parametrize("rel", (".codex/config.toml", ".codex/hooks.json"))
def test_add_harness_codex_refresh_preserves_different_instance_config_loudly(
    pm_import, tmp_path, capsys, rel,
):
    """다른 adopter config/hook은 byte 보존하고 수동 반영을 loud 안내한다.

    같은 refresh에서 engine-managed agent는 계속 template로 전파돼, instance-owned 예외가
    `.codex/**` 전체의 refresh 중단으로 넓어지지 않음을 함께 고정한다.
    """
    dest = _build_live_instance(pm_import, tmp_path / f"codex_diff_{Path(rel).stem}", "claude")
    pm_import.add_harness(dest, "codex", dry_run=False, source_root=REPO)
    instance_file = dest / rel
    local_bytes = b"# adopter-owned local policy\\n"
    instance_file.write_bytes(local_bytes)
    agent = dest / ".codex" / "agents" / "developer.toml"
    agent.write_text("LOCAL AGENT CUSTOMIZATION\\n", encoding="utf-8")
    capsys.readouterr()

    plan = pm_import.add_harness(dest, "codex", dry_run=False, source_root=REPO)

    assert instance_file.read_bytes() == local_bytes
    assert rel not in _plan_relpaths(plan, dest)
    assert "LOCAL AGENT CUSTOMIZATION" not in agent.read_text(encoding="utf-8")
    out = capsys.readouterr().out
    assert rel in out and "수동 반영" in out


@pytest.mark.parametrize("rel", (".codex/config.toml", ".codex/hooks.json"))
def test_add_harness_codex_instance_config_bypasses_broken_backup_root(
    pm_import, tmp_path, capsys, rel,
):
    """보호 파일은 backup action 전에 제외돼 broken backup root도 보존 분기를 막지 않는다."""
    dest = _build_live_instance(pm_import, tmp_path / f"codex_backup_root_{Path(rel).stem}", "claude")
    protected = dest / rel
    protected.parent.mkdir(parents=True, exist_ok=True)
    local_bytes = b"# adopter-owned local policy\n"
    protected.write_bytes(local_bytes)
    backup_root = dest / ".pm_import_backups"
    backup_root.write_text("not a directory", encoding="utf-8")
    capsys.readouterr()

    plan = pm_import.add_harness(dest, "codex", dry_run=False, source_root=REPO)

    assert protected.read_bytes() == local_bytes
    # CopyAction 자체가 0이므로 backup도 0 — backup root가 file이어도 apply가 도달한다.
    protected_actions = [a for a in plan if a.dst == protected]
    assert protected_actions == []
    assert backup_root.read_text(encoding="utf-8") == "not a directory"
    assert rel in capsys.readouterr().out


def test_add_harness_codex_broken_backup_root_is_sensitive_to_protection_policy(
    pm_import, tmp_path, monkeypatch,
):
    """보호 정책을 제거하면 일반 backup 안전 가드가 다시 red가 된다(non-vacuous seam)."""
    dest = _build_live_instance(pm_import, tmp_path / "codex_backup_sensitive", "claude")
    config = dest / ".codex" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("adopter config", encoding="utf-8")
    (dest / ".pm_import_backups").write_text("not a directory", encoding="utf-8")
    monkeypatch.setitem(pm_import.ADD_HARNESS_CREATE_IF_ABSENT, "codex", frozenset())

    with pytest.raises(pm_import.AncestorConflict, match=r"\.pm_import_backups"):
        pm_import.add_harness(dest, "codex", dry_run=False, source_root=REPO)


def test_add_harness_codex_refresh_preserves_agents_md_and_refreshes_agent_cards(
    pm_import, tmp_path, capsys,
):
    """Codex root doc 만 byte 보존하고 agent 카드는 손편집 여부와 무관하게 refresh 한다.

    카드의 model/reasoning 은 local.conf `delegate.*` 의 렌더 파생물이라, add-harness 가 손편집을
    보존하면 다음 pm-update 가 그것을 conf 값으로 되돌린다(두 표면이 같은 필드에 반대 규칙을
    선언). 모델을 바꾸는 자리는 카드가 아니라 local.conf 다."""
    dest = _build_live_instance(pm_import, tmp_path / "codex_model_overrides", "claude")
    pm_import.add_harness(dest, "codex", dry_run=False, source_root=REPO)
    agents_md = dest / "AGENTS.md"
    agents_md_bytes = b"# adopter-owned common core\n"
    agents_md.write_bytes(agents_md_bytes)
    cards = {}
    for name, local in (
        ("developer.toml", b'\nmodel = "adopter/dev"\n'),
        ("code-reviewer.toml", b'\nmodel_reasoning_effort = "high"\n'),
        ("architect.toml", None),
    ):
        card = dest / ".codex" / "agents" / name
        template_bytes = card.read_bytes()
        cards[name] = (card, template_bytes)
        card.write_bytes(template_bytes + local if local
                         else b"LOCAL ENGINE-MANAGED EDIT\n")
    capsys.readouterr()

    plan = pm_import.add_harness(dest, "codex", dry_run=False, source_root=REPO)

    assert agents_md.read_bytes() == agents_md_bytes, "instance-owned root doc 이 덮였다"
    planned = set(_plan_relpaths(plan, dest))
    for name, (card, template_bytes) in cards.items():
        assert card.read_bytes() == template_bytes, \
            f"{name} 이 refresh 되지 않았다(카드 손편집이 conf 단일 진실을 이긴다)"
        assert f".codex/agents/{name}" in planned, f"{name} 이 계획에서 빠졌다"
    assert "AGENTS.md" in capsys.readouterr().out


def test_add_harness_codex_root_doc_protection_bypasses_broken_backup_root(
    pm_import, tmp_path, capsys,
):
    """instance-owned root doc 은 backup action 전에 제외돼 broken backup root 에도 보존된다."""
    dest = _build_live_instance(pm_import, tmp_path / "codex_model_backup_root", "claude")
    agents_md = dest / "AGENTS.md"
    agents_md.write_text("LOCAL AGENTS\n", encoding="utf-8")
    backup_root = dest / ".pm_import_backups"
    backup_root.write_text("not a directory", encoding="utf-8")
    capsys.readouterr()

    plan = pm_import.add_harness(dest, "codex", dry_run=False, source_root=REPO)

    assert agents_md.read_text(encoding="utf-8") == "LOCAL AGENTS\n"
    assert "AGENTS.md" not in set(_plan_relpaths(plan, dest))
    assert backup_root.read_text(encoding="utf-8") == "not a directory"
    assert "AGENTS.md" in capsys.readouterr().out


def test_add_harness_codex_seeds_absent_and_quietly_skips_identical_agents_md(
    pm_import, tmp_path, capsys,
):
    """AGENTS.md 부재는 seed, template과 byte 동일한 기존 파일은 quiet skip이다."""
    seeded_dest = _build_live_instance(pm_import, tmp_path / "codex_agents_seed", "claude")
    seed_plan = pm_import.add_harness(seeded_dest, "codex", dry_run=False, source_root=REPO)
    assert "AGENTS.md" in _plan_relpaths(seed_plan, seeded_dest)
    assert "Live Inst" in (seeded_dest / "AGENTS.md").read_text(encoding="utf-8")

    same_dest = _build_live_instance(pm_import, tmp_path / "codex_agents_same", "claude")
    (same_dest / "AGENTS.md").write_bytes((REPO / "templates" / "codex" / "AGENTS.md").read_bytes())
    capsys.readouterr()
    same_plan = pm_import.add_harness(same_dest, "codex", dry_run=False, source_root=REPO)
    assert "AGENTS.md" not in _plan_relpaths(same_plan, same_dest)
    assert "instance-owned AGENTS.md" not in capsys.readouterr().out


def test_add_harness_rejects_both_and_unknown(pm_import, tmp_path):
    dest = _build_live_instance(pm_import, tmp_path / "reject_inst", "claude")
    with pytest.raises(ValueError):
        pm_import.add_harness(dest, "both", dry_run=True, source_root=REPO)
    with pytest.raises(ValueError):
        pm_import.add_harness(dest, "bogus", dry_run=True, source_root=REPO)


def test_add_harness_rejects_missing_dest(pm_import, tmp_path):
    with pytest.raises(FileNotFoundError):
        pm_import.add_harness(tmp_path / "does_not_exist", "opencode",
                              dry_run=True, source_root=REPO)


# ── instance-owned 어댑터 config 원장 (T-0585) ────────────────────────────────
# 기록 시점(설치·add-harness 레이다운 판정)과 수용 커맨드(pm_config)를 실 설치로 태운다. 채널의
# 판정 분기(무편집/편집/원장부재/보고전용)는 `test_pm_update.py` 의 합성 픽스처 절이 본다.


def _ledger_files(dest: Path) -> dict:
    path = dest / ".project_manager" / "adapter_baseline.json"
    return json.loads(path.read_text(encoding="utf-8"))["files"] if path.is_file() else {}


def test_instance_owned_declaration_and_channel_classification_agree(pm_import):
    """소유 선언 ↔ 채널 분류가 **양방향 전수 일치**한다 (선언 파생·분류 상수).

    한쪽만 늘면 조용히 깨진다 — 분류에만 있으면 소유 진실이 갈리고(그 파일을 pm_update 가 덮을
    수도 있다), 선언에만 있으면 새 config 가 채널 0 으로 떨어져 지금 닫는 결함이 재발한다."""
    declared = pm_import.INSTANCE_OWNED_ADAPTER_FILES
    channel = pm_import.ADAPTER_CONFIG_CHANNEL
    assert set(declared) == set(channel), "하네스 축이 어긋남(등록 하네스 누락)"
    modes = {pm_import.ADAPTER_CONFIG_MANAGED, pm_import.ADAPTER_CONFIG_REPORT,
             pm_import.ADAPTER_CONFIG_NO_CHANNEL}
    for harness in sorted(declared):
        assert set(declared[harness]) == set(channel[harness]), (
            f"{harness}: 선언 {sorted(declared[harness])} ↔ 분류 {sorted(channel[harness])} 불일치")
        unknown = {rel: mode for rel, mode in channel[harness].items() if mode not in modes}
        assert unknown == {}, f"{harness}: 알 수 없는 채널 분류 {unknown}"
    # 재승인 안내는 managed 대상에만 붙는다(보고-전용에 붙으면 안 하는 일을 안내하는 셈).
    managed = {rel for harness in channel for rel, mode in channel[harness].items()
               if mode == pm_import.ADAPTER_CONFIG_MANAGED}
    assert set(pm_import.ADAPTER_CONFIG_REAPPROVAL_NOTE) <= managed, (
        "재승인 안내가 managed 아닌 경로에 선언됨: "
        f"{sorted(set(pm_import.ADAPTER_CONFIG_REAPPROVAL_NOTE) - managed)}")


def test_install_records_adapter_baseline_for_laid_down_configs(pm_import, tmp_path):
    """최초 import 가 레이다운한 instance-owned config 의 template 해시를 원장에 기록한다.

    기록이 곧 다음 동기의 판정 기준이다 — 없으면 그 인스턴스는 영구 보고 모드(상류 동작 fix 미도달).
    루트 진입 문서(`none` 분류)는 채널이 없으므로 기록 대상이 아니다.

    대조 축은 **판정이 쓰는 축**(`content_sha256`)이다 — 여기서 raw 해시를 손으로 재타이핑하면
    CRLF 체크아웃(Windows manager)에서 정상 기록을 불일치로 읽는다(기록↔판정 축 분리)."""
    dest = tmp_path / "baseline-install"
    assert pm_import.main([
        "--new", str(dest), "--harness", "codex", "--name", "Ledger Adopter",
    ]) == 0

    files = _ledger_files(dest)
    assert set(files) == {".codex/config.toml", ".codex/hooks.json"}, (
        f"원장 기록 집합이 채널 대상과 다름: {sorted(files)}")
    template_root = REPO / "templates" / "codex"
    for rel, entry in files.items():
        assert entry["sha256"] == pm_import.content_sha256(template_root / rel), \
            f"{rel}: 원장 해시가 template 과 다름(레이다운 시점 기록이 아니다)"
        # 기록 축 == 판정 축: 같은 값이 레이다운한 dest 에서도 나와야 다음 동기가 수렴한다.
        assert entry["sha256"] == pm_import.content_sha256(dest / rel), \
            f"{rel}: 원장 해시가 레이다운한 dest 와 다름(판정 축이 갈렸다)"
        assert entry["recorded_at"] and "template_rev" in entry


def test_adapter_config_partial_managed_dest_is_judged_without_install_shape(
        pm_import, tmp_path):
    """managed 경로 자체는 설치 영수증/완전 adapter shape와 독립인 복구 증거다.

    구세대/부분 복구 트리에 ``.codex/hooks.json`` 하나만 남으면 installed_harnesses는 의도대로
    비어도 config 완료 게이트는 그 파일을 판정해야 한다. 빈 judgments는 rc0 false-green이다.
    """
    source = tmp_path / "source"
    template = source / "templates" / "codex" / ".codex" / "hooks.json"
    template.parent.mkdir(parents=True)
    template.write_text('{"hooks": {"PreCompact": ["new"]}}\n', encoding="utf-8")
    dest = tmp_path / "partial-dest"
    hooks = dest / ".codex" / "hooks.json"
    hooks.parent.mkdir(parents=True)
    hooks.write_bytes(template.read_bytes())

    assert pm_import.installed_harnesses(dest, source) == [], \
        "픽스처가 완전 설치 형상으로 잘못 올라갔다"
    judgments = pm_import.judge_adapter_configs(dest, source)

    assert [(item.relpath, item.harness, item.mode, item.status) for item in judgments] == [
        (".codex/hooks.json", "codex", pm_import.ADAPTER_CONFIG_MANAGED, "in-sync")
    ]


@pytest.mark.parametrize("kind", ("directory", "broken-symlink"))
def test_adapter_config_partial_managed_nonfile_is_unavailable(
        pm_import, tmp_path, kind):
    """managed 이름이 존재하지만 byte를 읽을 수 없으면 대상 0이 아니라 unavailable이다."""
    source = tmp_path / "source"
    template = source / "templates" / "codex" / ".codex" / "hooks.json"
    template.parent.mkdir(parents=True)
    template.write_text('{"hooks": {}}\n', encoding="utf-8")
    dest = tmp_path / f"partial-{kind}"
    candidate = dest / ".codex" / "hooks.json"
    candidate.parent.mkdir(parents=True)
    if kind == "directory":
        candidate.mkdir()
    else:
        candidate.symlink_to(dest / "missing-hooks-target.json")

    with pytest.raises(pm_import.AdapterConfigChannelUnavailable, match="dest를 읽을 수 없다"):
        pm_import.judge_adapter_configs(dest, source)


def test_adapter_config_partial_managed_regular_file_read_failure_is_unavailable(
        pm_import, tmp_path, monkeypatch):
    """regular managed 파일도 byte 판독 실패를 부재로 접지 않는다."""
    source = tmp_path / "source"
    template = source / "templates" / "codex" / ".codex" / "hooks.json"
    template.parent.mkdir(parents=True)
    template.write_text('{"hooks": {}}\n', encoding="utf-8")
    dest = tmp_path / "partial-unreadable"
    candidate = dest / ".codex" / "hooks.json"
    candidate.parent.mkdir(parents=True)
    candidate.write_text('{"hooks": {"old": true}}\n', encoding="utf-8")
    # 주입 지점은 **판정이 쓰는 다이제스트**다(T-0710 이후 `content_sha256` — 개행 정규화 축).
    original_sha = pm_import.content_sha256
    monkeypatch.setattr(
        pm_import, "content_sha256",
        lambda path: None if Path(path) == candidate else original_sha(path))

    with pytest.raises(pm_import.AdapterConfigChannelUnavailable, match="dest를 읽을 수 없다"):
        pm_import.judge_adapter_configs(dest, source)


def test_adapter_config_partial_report_only_does_not_infer_harness(pm_import, tmp_path):
    """report-only 파일만 있는 partial 트리는 기존처럼 blocking 판정 집합을 만들지 않는다."""
    source = tmp_path / "source"
    template = source / "templates" / "codex" / ".codex" / "config.toml"
    template.parent.mkdir(parents=True)
    template.write_text("upstream = true\n", encoding="utf-8")
    dest = tmp_path / "report-only"
    report = dest / ".codex" / "config.toml"
    report.parent.mkdir(parents=True)
    report.write_text("adopter = true\n", encoding="utf-8")

    assert pm_import.installed_harnesses(dest, source) == []
    assert pm_import.judge_adapter_configs(dest, source) == []


def test_adapter_config_explicit_claude_receipt_ignores_foreign_codex_managed_file(
        pm_import, tmp_path):
    """명시 receipt가 부정한 하네스를 foreign managed 파일 하나로 다시 설치 판정하지 않는다."""
    source = tmp_path / "source"
    codex_template = source / "templates" / "codex" / ".codex" / "hooks.json"
    codex_template.parent.mkdir(parents=True)
    codex_template.write_text('{"hooks": {"upstream": true}}\n', encoding="utf-8")
    claude_template = source / "templates" / "claude_code" / ".claude" / "settings.json"
    claude_template.parent.mkdir(parents=True)
    claude_template.write_text('{}\n', encoding="utf-8")
    dest = tmp_path / "claude-only"
    (dest / ".project_manager").mkdir(parents=True)
    (dest / ".project_manager" / "install.json").write_text(
        '{"schema": 1, "harnesses": ["claude"]}\n', encoding="utf-8")
    foreign = dest / ".codex" / "hooks.json"
    foreign.parent.mkdir(parents=True)
    foreign_body = '{"hooks": {"foreign": true}}\n'
    foreign.write_text(foreign_body, encoding="utf-8")

    judgments = pm_import.judge_adapter_configs(dest, source)

    assert all(item.harness != "codex" for item in judgments)
    with pytest.raises(ValueError, match="채널 대상이 아니다"):
        pm_import.accept_adapter_config(dest, source, ".codex/hooks.json")
    assert foreign.read_text(encoding="utf-8") == foreign_body


def test_add_harness_records_baseline_only_for_unpreserved_configs(pm_import, tmp_path):
    """add-harness 는 보존(기존 값 상이)한 config 를 원장에 올리지 않는다.

    보존분을 기록하면 그 값이 "상류가 준 그대로" 로 둔갑해 다음 동기가 채택자 편집을 덮는다
    (하한선 붕괴). 함께 내려놓은 무편집 파일은 정상 기록돼야 한다(공허 단언 방지)."""
    dest = _build_live_instance(pm_import, tmp_path / "baseline-add", "claude")
    local_hooks = '{"hooks": {"PreCompact": ["채택자 값"]}}\n'
    (dest / ".codex").mkdir(parents=True, exist_ok=True)
    (dest / ".codex" / "hooks.json").write_text(local_hooks, encoding="utf-8")

    pm_import.add_harness(dest, "codex", dry_run=False, source_root=REPO)

    files = _ledger_files(dest)
    assert ".codex/hooks.json" not in files, \
        "보존된(채택자 값) config 가 원장에 올라감 — 다음 동기가 그 값을 덮는다"
    assert ".codex/config.toml" in files, "함께 레이다운된 config 가 기록되지 않음(공허 단언)"
    assert (dest / ".codex" / "hooks.json").read_text(encoding="utf-8") == local_hooks


def test_sync_adapter_config_list_and_accept_via_pm_config(
        pm_import, pm_config, tmp_path, capsys):
    """`sync-adapter-config --list` 는 판정을 조회하고 `--accept` 는 백업 후 상류 값을 채택한다.

    구세대 채택자(원장 부재)가 보고 모드를 빠져나가는 **유일한 명시 채널**이다. 채널 밖 경로는
    rc1 로 거른다(조용한 무동작 금지)."""
    dest = tmp_path / "accept-cli"
    assert pm_import.main([
        "--new", str(dest), "--harness", "codex", "--name", "Accept Adopter",
    ]) == 0
    edited = '{"hooks": {"PreCompact": ["채택자 손편집"]}}\n'
    (dest / ".codex" / "hooks.json").write_text(edited, encoding="utf-8")
    capsys.readouterr()

    listed = argparse.Namespace(list=True, accept=None, source=str(REPO))
    assert pm_config.cmd_sync_adapter_config(
        listed, pm_import=pm_import, dest_root=dest) == 0
    listing = capsys.readouterr().out
    assert ".codex/hooks.json" in listing and "managed" in listing, listing

    accepted = argparse.Namespace(list=False, accept=".codex/hooks.json", source=str(REPO))
    assert pm_config.cmd_sync_adapter_config(
        accepted, pm_import=pm_import, dest_root=dest) == 0

    upstream_bytes = (REPO / "templates" / "codex" / ".codex" / "hooks.json").read_bytes()
    assert (dest / ".codex" / "hooks.json").read_bytes() == upstream_bytes
    # 원장은 **판정 축**(`content_sha256`)으로 기록한다 — raw 해시를 손으로 재타이핑하면 CRLF
    #   체크아웃에서 정상 수용을 불일치로 읽는다.
    assert _ledger_files(dest)[".codex/hooks.json"]["sha256"] == \
        pm_import.content_sha256(REPO / "templates" / "codex" / ".codex" / "hooks.json")
    backup = (dest / ".pm_import_backups" / datetime.date.today().isoformat()
              / ".codex" / "hooks.json")
    assert backup.is_file() and backup.read_text(encoding="utf-8") == edited, \
        "수용이 기존 값을 중앙 백업 자리에 남기지 않음(비파괴 위반)"
    out = capsys.readouterr().out
    assert "/hooks" in out, f"수용 후 재승인 안내 부재: {out!r}"

    bogus = argparse.Namespace(list=False, accept=".codex/nope.json", source=str(REPO))
    assert pm_config.cmd_sync_adapter_config(
        bogus, pm_import=pm_import, dest_root=dest) == 1


def test_pm_update_run_preserves_edited_adapter_config_and_reports(
        pm_import, pm_update, tmp_path, monkeypatch, capsys):
    """실 sync가 편집분을 보존·보고하고 managed 미수렴을 rc1 완료 게이트로 올린다.

    합성 호출이 아니라 `_main` 흐름을 태워, 채널이 실행 경로에 실제로 걸려 있는지(엔진 sync
    후단 배치)를 본다. 파일 보존과 명령 성공은 별개다 — T-0585의 non-destructive 기본값은
    유지하되 그 상태를 흡수 완료(rc0)로 접지 않는다."""
    dest = tmp_path / "sync-wiring"
    assert pm_import.main([
        "--new", str(dest), "--harness", "codex", "--name", "Wiring Adopter",
    ]) == 0
    edited = '{"hooks": {"PreCompact": ["채택자 손편집"]}}\n'
    (dest / ".codex" / "hooks.json").write_text(edited, encoding="utf-8")
    capsys.readouterr()

    monkeypatch.setattr(pm_update, "REPO", dest)
    monkeypatch.setenv("PM_NONINTERACTIVE", "1")
    assert pm_update.main(["--from", str(REPO)]) == 1

    captured = capsys.readouterr()
    assert (dest / ".codex" / "hooks.json").read_text(encoding="utf-8") == edited, \
        "실 sync 가 채택자 편집을 덮었다(하한선 위반)"
    assert "sync-adapter-config --accept" in captured.err, (
        f"실 sync 에 채널이 배선되지 않음(보고 부재): {captured.err!r}")
    assert "managed 어댑터 config가 미수렴" in captured.err, (
        f"보존 상태를 rc1 완료 게이트로 올리지 않음: {captured.err!r}")


def test_sync_adapter_config_list_label_reflects_mode_not_status_alone(
        pm_import, pm_config, tmp_path, capsys):
    """`--list` 라벨은 mode 와 함께 판정한다 — 보고-전용을 "자동 갱신" 으로 안내하지 않는다.

    status(`unedited`)만 보면 report 대상도 "다음 동기가 자동 갱신" 이 되는데, 그 파일은 어떤
    경우에도 갱신되지 않는다(거짓 안내). 두 mode 를 같은 상태로 만들어 라벨이 갈리는지 본다."""
    dest = tmp_path / "label-mode"
    assert pm_import.main([
        "--new", str(dest), "--harness", "codex", "--name", "Label Adopter",
    ]) == 0
    # 두 파일 모두 "무편집(원장 일치)인데 template 과 다름" 상태로 만든다 — mode 만 다르다.
    #   픽스처 bytes 는 `write_lf` 로 고정하고 원장은 **판정 축**(`content_sha256`)으로 심는다 —
    #   플랫폼 기본 개행 + raw 해시로 만들면 Windows 에서만 구 원장(raw 축) 상태가 되어, 이 게이트가
    #   재려는 라벨 축 대신 마이그레이션 축을 재게 된다(그 축은 아래 전용 회귀가 본다).
    ledger_path = dest / ".project_manager" / "adapter_baseline.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    for rel, body in ((".codex/hooks.json", '{"hooks": {}}\n'),
                      (".codex/config.toml", 'sandbox_mode = "read-only"\n')):
        write_lf(dest / rel, body)
        ledger["files"][rel]["sha256"] = pm_import.content_sha256(dest / rel)
    write_lf(ledger_path, json.dumps(ledger, indent=2) + "\n")
    capsys.readouterr()

    args = argparse.Namespace(list=True, accept=None, source=str(REPO))
    assert pm_config.cmd_sync_adapter_config(
        args, pm_import=pm_import, dest_root=dest) == 0

    lines = {line.split("·")[0].strip().lstrip("- "): line
             for line in capsys.readouterr().out.splitlines() if line.startswith("  - ")}
    assert "다음 동기가 자동 갱신" in lines[".codex/hooks.json"], lines[".codex/hooks.json"]
    assert "보고 전용" in lines[".codex/config.toml"], lines[".codex/config.toml"]
    assert "다음 동기가 자동 갱신" not in lines[".codex/config.toml"], \
        "보고-전용 대상에 자동 갱신 안내(거짓)"


# ── 구 원장(raw bytes 축) 무변경 마이그레이션 (T-0723) ────────────────────────
# 상류 체크아웃까지 CRLF 인 Windows manager 로 설치한 채택자는 원장에 **CRLF raw 다이제스트**가
# 들어갔다(판정을 내용 축으로 옮기기 전 엔진). 그 파일의 template 이 그 뒤 바뀌면 dest 가 template
# 과 달라 in-sync backfill 이 닿지 않고, 설치 이후 아무것도 안 한 채택자가 영구히 `edited`
# (채택자 편집 — 보존)로 오판된다. 아래 픽스처는 그 상류 CRLF 형상을 LF 환경(Linux)에서 그대로
# 재현한다 — `write_crlf` 가 없으면 두 축이 같은 값이라 가드가 시험조차 되지 않는다.

_LEGACY_CRLF_HOOKS = '{\n  "hooks": {\n    "PreCompact": ["레이다운 시점 값"]\n  }\n}\n'
_CURRENT_HOOKS = '{\n  "hooks": {\n    "PreCompact": ["상류 동작 fix"]\n  }\n}\n'


def _legacy_raw_digest_instance(pm_import, root: Path) -> tuple[Path, Path, Path]:
    """구 원장(raw 축) + 상류가 그 뒤 바뀐 형상 — (source, dest, ledger_path).

    dest 는 **레이다운 시점 bytes 그대로**(CRLF)이고 원장에는 그 bytes 의 raw 해시가 들어 있다.
    상류 template 만 새 내용으로 전진해 있다(= 그 채택자가 받아야 할 동작 fix).
    """
    source = root / "source"
    template = source / "templates" / "codex" / ".codex" / "hooks.json"
    template.parent.mkdir(parents=True)
    write_crlf(template, _CURRENT_HOOKS)
    dest = root / "legacy-ledger"
    hooks = dest / ".codex" / "hooks.json"
    hooks.parent.mkdir(parents=True)
    write_crlf(hooks, _LEGACY_CRLF_HOOKS)
    ledger_path = dest / ".project_manager" / "adapter_baseline.json"
    ledger_path.parent.mkdir(parents=True)
    write_lf(ledger_path, json.dumps({
        "schema": 1,
        "files": {
            ".codex/hooks.json": {
                "sha256": pm_import.file_sha256(hooks),
                "recorded_at": "2025-11-03T09:00:00+09:00",
                "template_rev": "0" * 40,
            },
        },
    }, indent=2) + "\n")
    # 주입이 실제로 걸렸는가 — 두 축이 갈린 상태여야 이 회귀가 마이그레이션을 시험한다.
    assert pm_import.file_sha256(hooks) != pm_import.content_sha256(hooks), \
        "픽스처가 CRLF 축을 싣지 못했다(구 원장 상태 재현 실패·공허 회귀)"
    assert pm_import.content_sha256(template) != pm_import.content_sha256(hooks), \
        "픽스처 상류가 전진하지 않았다(in-sync backfill 이 닿는 상태·공허 회귀)"
    return source, dest, ledger_path


def test_legacy_raw_digest_ledger_is_judged_unedited_not_adopter_edit(
        pm_import, tmp_path):
    """구 원장(raw 축) 항목은 `edited` 가 아니라 `unedited` 다 — 그 bytes 가 기록 그대로다.

    저장 값이 dest **raw** 해시와 같다는 건 기록 시점 이후 아무도 손대지 않았다는 뜻이라,
    내용 동일성보다 강한 무편집 증거다. `edited` 로 접으면 채택자에게 "네가 편집했다" 는 거짓을
    말하고 상류 동작 fix 를 `--accept` 1회 수동 조작 뒤로 미룬다."""
    source, dest, _ledger = _legacy_raw_digest_instance(pm_import, tmp_path)

    judgments = pm_import.judge_adapter_configs(dest, source)

    assert [(item.relpath, item.status) for item in judgments] == [
        (".codex/hooks.json", "unedited")]


def test_record_adapter_baseline_migrates_legacy_raw_digest_without_touching_file(
        pm_import, tmp_path):
    """구 원장 항목을 현재 축으로 재기록한다 — 채택자 파일 bytes 와 레이다운 메타는 불변.

    바뀌는 건 같은 사실의 표현(원장 값)뿐이다. `recorded_at`·`template_rev` 까지 현재로 밀면
    이번 실행이 관측하지 않은 세대를 주장하게 되고, 세대 델타 보고가 침묵한다."""
    source, dest, ledger_path = _legacy_raw_digest_instance(pm_import, tmp_path)
    hooks = dest / ".codex" / "hooks.json"
    before_bytes = hooks.read_bytes()
    before_entry = json.loads(ledger_path.read_text(encoding="utf-8"))["files"][
        ".codex/hooks.json"]

    assert pm_import.record_adapter_baseline(dest, source) == [".codex/hooks.json"]

    entry = json.loads(ledger_path.read_text(encoding="utf-8"))["files"][
        ".codex/hooks.json"]
    assert entry["sha256"] == pm_import.content_sha256(hooks), \
        "재기록 값이 판정 축(content_sha256)이 아니다"
    assert entry["recorded_at"] == before_entry["recorded_at"]
    assert entry["template_rev"] == before_entry["template_rev"], \
        "마이그레이션이 관측하지 않은 세대를 주장했다(세대 델타 침묵)"
    assert hooks.read_bytes() == before_bytes, \
        "무변경 마이그레이션이 채택자 파일 bytes 를 바꿨다"
    # 재기록 뒤에는 구 축 없이도 같은 판정이 나온다(마이그레이션 수렴 · 축 누적 0).
    judgment = pm_import.judge_adapter_configs(dest, source)[0]
    assert (judgment.status, judgment.baseline_sha256) == (
        "unedited", judgment.dest_sha256)


def test_record_adapter_baseline_records_content_digest_for_crlf_upstream_laydown(
        pm_import, tmp_path):
    """CRLF 상류를 그대로 내려놓은 직후의 기록 값도 **판정 축**이다 (기록↔판정 축 일치).

    레이다운 시점 기록이 raw 축으로 남으면 그 인스턴스는 설치 직후부터 구 원장 상태가 된다."""
    source = tmp_path / "source"
    template = source / "templates" / "codex" / ".codex" / "hooks.json"
    template.parent.mkdir(parents=True)
    write_crlf(template, _CURRENT_HOOKS)
    dest = tmp_path / "crlf-laydown"
    hooks = dest / ".codex" / "hooks.json"
    hooks.parent.mkdir(parents=True)
    hooks.write_bytes(template.read_bytes())  # 레이다운 = byte-copy.
    (dest / ".project_manager").mkdir()  # 원장이 앉을 자리(설치 트리 형상).
    assert pm_import.file_sha256(template) != pm_import.content_sha256(template), \
        "픽스처가 CRLF 축을 싣지 못했다(공허 회귀)"

    assert pm_import.record_adapter_baseline(dest, source) == [".codex/hooks.json"]

    entry = json.loads(
        (dest / ".project_manager" / "adapter_baseline.json").read_text(
            encoding="utf-8"))["files"][".codex/hooks.json"]
    assert entry["sha256"] == pm_import.content_sha256(template) \
        == pm_import.content_sha256(hooks), \
        "레이다운 시점 기록이 template 내용 다이제스트와 다르다"
    assert pm_import.judge_adapter_configs(dest, source)[0].status == "in-sync"


def test_sync_updates_legacy_raw_digest_managed_config_without_requiring_accept(
        pm_import, pm_update, tmp_path):
    """실 sync 가 구 원장 managed config 를 자동 갱신하고 원장을 수렴시킨다 — `--accept` 불요.

    합성 판정이 아니라 pm_update 의 동기 채널을 태운다. 이 경로가 `preserved`(채택자 편집)로
    떨어지면 채택자는 상류 동작 fix 를 받으려고 거짓 안내대로 수동 수용을 해야 한다."""
    dest = tmp_path / "legacy-sync"
    assert pm_import.main([
        "--new", str(dest), "--harness", "codex", "--name", "Legacy Ledger Adopter",
    ]) == 0
    hooks = dest / ".codex" / "hooks.json"
    write_crlf(hooks, _LEGACY_CRLF_HOOKS)
    ledger_path = dest / ".project_manager" / "adapter_baseline.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["files"][".codex/hooks.json"]["sha256"] = pm_import.file_sha256(hooks)
    write_lf(ledger_path, json.dumps(ledger, indent=2) + "\n")
    assert pm_import.file_sha256(hooks) != pm_import.content_sha256(hooks), \
        "픽스처가 CRLF 축을 싣지 못했다(구 원장 상태 재현 실패·공허 회귀)"

    result = pm_update.sync_adapter_configs(dest, REPO, write=True)

    assert result["status"] == "ok"
    assert [item["relpath"] for item in result["updated"]] == [".codex/hooks.json"], \
        f"구 원장 무편집 파일이 자동 갱신 대상이 아니다: {result}"
    assert result["preserved"] == [], \
        f"무편집 파일을 채택자 편집으로 보존했다(거짓 --accept 처방): {result['preserved']}"
    assert result["managed_converged"], f"동기 후에도 미수렴: {result['blocking']}"
    assert hooks.read_bytes() == (
        REPO / "templates" / "codex" / ".codex" / "hooks.json").read_bytes()


def test_build_parser_routes_sync_adapter_config(pm_config):
    """build_parser 가 `sync-adapter-config` 를 파싱하고 cmd_sync_adapter_config 로 라우팅한다.

    핸들러가 있어도 파서 배선이 없으면 채택자에겐 없는 커맨드다(배선 자체가 판정 대상)."""
    parser = pm_config.build_parser()
    listed = parser.parse_args(["sync-adapter-config", "--list"])
    assert listed.func is pm_config.cmd_sync_adapter_config
    assert listed.list is True and listed.accept is None and listed.source is None
    accepted = parser.parse_args(
        ["sync-adapter-config", "--accept", ".codex/hooks.json", "--from", "/tmp/fw"])
    assert accepted.func is pm_config.cmd_sync_adapter_config
    assert accepted.accept == ".codex/hooks.json" and accepted.source == "/tmp/fw"
    # 무인자 = 조회(기본 동작)로 떨어진다.
    bare = parser.parse_args(["sync-adapter-config"])
    assert bare.func is pm_config.cmd_sync_adapter_config and bare.accept is None


def test_sync_adapter_config_list_and_accept_are_mutually_exclusive(pm_config):
    """`--list` 와 `--accept` 를 함께 주면 argparse 가 거부한다(한쪽 조용한 무시 금지)."""
    parser = pm_config.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["sync-adapter-config", "--list", "--accept", ".codex/hooks.json"])
    # 각각 단독은 정상 파싱(가드가 정상 사용을 막지 않음).
    assert parser.parse_args(["sync-adapter-config", "--list"]).list is True
    assert parser.parse_args(
        ["sync-adapter-config", "--accept", ".codex/hooks.json"]).accept == ".codex/hooks.json"


def test_sync_adapter_config_accept_reports_failure_rc_without_success_claim(
        pm_import, pm_config, tmp_path, capsys):
    """수용이 비정상 종료하면 rc1 + 사유를 내고 "자동 갱신 궤도" 를 주장하지 않는다.

    원장 기록 실패를 무시하고 성공을 찍으면, 채택자는 다음 동기가 자동 갱신할 거라 믿는데 실제론
    영구 보고 모드다(거짓 안내)."""
    dest = tmp_path / "accept-failure"
    assert pm_import.main([
        "--new", str(dest), "--harness", "codex", "--name", "Failure Adopter",
    ]) == 0
    (dest / ".codex" / "hooks.json").write_text('{"hooks": {}}\n', encoding="utf-8")
    ledger_path = dest / ".project_manager" / "adapter_baseline.json"
    ledger_path.unlink()
    ledger_path.mkdir()  # 원장 자리를 디렉토리로 점유 → 기록 불가.
    capsys.readouterr()

    args = argparse.Namespace(list=False, accept=".codex/hooks.json", source=str(REPO))
    rc = pm_config.cmd_sync_adapter_config(args, pm_import=pm_import, dest_root=dest)

    captured = capsys.readouterr()
    assert rc == 1, "원장 기록 실패인데 성공 rc"
    assert "ledger-failed" in captured.err, captured.err
    assert "자동 갱신 궤도" not in captured.out, f"거짓 성공 안내: {captured.out!r}"
