"""pm_import.py 단위 테스트 — 기계 단계(복사·sed·board init·백업·dry-run·idempotent).

plan/apply 분리 설계 덕에 임시 디렉토리만으로 외부 의존 없이 테스트한다. board.py init 은
복사된 트리의 board.py 를 동일 인터프리터로 subprocess 호출 — local.conf·pm_state 산출을
실제로 검증한다(LLM·네트워크 0 = 토큰 0).
"""
from __future__ import annotations

import datetime
import importlib.util
import io
import os
import re
from pathlib import Path

import pytest
import yaml

from _win_skip import _can_symlink

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

# symlink 생성 불가 환경(권한 없는 Windows 등)에서 symlink 의존 테스트를 skip.
requires_symlink = pytest.mark.skipif(
    not _can_symlink(),
    reason="Windows: symlink requires Developer Mode/admin",
)

# operational placeholder 치환에서 제외하는 엔진 문서 (pm_import.SED_EXCLUDE_RELPATHS 와 동치).
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


@pytest.fixture(scope="module")
def pm_import():
    return _load_pm_import()


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


def _grep_token_files(root: Path, token: str, *, exclude_engine_docs: bool = False) -> list[Path]:
    """root 하위에서 token 을 포함한 파일 목록. node_modules 제외."""
    hits: list[Path] = []
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if any(part == "node_modules" for part in rel.parts):
            continue
        if not path.is_file():
            continue
        if exclude_engine_docs:
            relp = rel.as_posix()
            # 엔진 문서/소스/생성-config 는 placeholder 대상이 아니라 *토큰명을 문서화*한다 — verbatim.
            #   - pm_role.md·pm_playbook.md (방법론 문서·기존)
            #   - .project_manager/tools/* (엔진 소스 .py — 주석/docstring 이 토큰 메커니즘 설명·T-0133)
            #   - local.conf (board init 헤더 주석이 해소 키를 `{{PY}}·{{PROJECT_NAME}}` 로 설명)
            if (relp in ENGINE_DOCS_KEEP_LITERAL
                    or relp.startswith(".project_manager/tools/")
                    or relp == ".project_manager/local.conf"):
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
    assert pm_import.HARNESS_CHOICES == ("claude", "opencode", "both")


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


def test_new_substitutes_operational_placeholders(pm_import, tmp_path):
    """엔진 문서(pm_role·pm_playbook) 외에는 잔여 operational {{ 0."""
    dest = tmp_path / "p"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "P"])
    assert rc == 0

    for token in OPERATIONAL_TOKENS:
        hits = _grep_token_files(dest, token, exclude_engine_docs=True)
        assert hits == [], f"{token} 잔존(엔진 문서 제외): {hits}"


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


# ── 엔진 문서는 리터럴 보존 (local.conf 가 런타임 해소) ──────────────────────

def test_engine_docs_keep_literal_placeholders(pm_import, tmp_path):
    """pm_role.md·pm_playbook.md 는 {{PY}}·{{TEST_CMD}}·{{PROJECT_NAME}} 리터럴을 유지한다."""
    dest = tmp_path / "p"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "P"])
    assert rc == 0
    for rel in ENGINE_DOCS_KEEP_LITERAL:
        text = (dest / rel).read_text(encoding="utf-8")
        assert "{{PY}}" in text, f"{rel} 에서 {{PY}} 가 치환됨 — 리터럴 유지여야 한다."


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
    """operational 값 동기화가 board.py init 이 쓴 다른 키(session 등)·주석을 보존한다."""
    dest = tmp_path / "confpreserve"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "Keep"])
    assert rc == 0

    local_conf = dest / ".project_manager" / "local.conf"
    conf = _parse_conf(local_conf)
    # board.py init 솔로가 쓰는 session 키가 살아있어야 한다(clobber 아님 — 키 단위 갱신).
    assert conf.get("session") == "pm", \
        f"session 키가 동기화로 손실됨: {conf.get('session')!r}"
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
    assert "upstream=/new/checkout" in text
    assert "upstream=/old/path" not in text
    assert "session=pm" in text  # 타 키 보존
    assert text.startswith("# header")  # 주석 보존
    # 제자리 갱신이므로 upstream 한 줄만 (upstream_rev 등 다른 키는 이 호출이 안 씀).
    assert sum(
        1 for line in text.splitlines()
        if line.split("=", 1)[0].strip() == "upstream"
    ) == 1


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


# ── ② --harness both: 두 어댑터 공존 ─────────────────────────────────────────

def test_both_harness_coexists(pm_import, tmp_path):
    dest = tmp_path / "dual"
    rc = pm_import.main(["--new", str(dest), "--harness", "both", "--name", "Dual"])
    assert rc == 0
    # claude 어댑터.
    assert (dest / ".claude").is_dir()
    assert (dest / "CLAUDE.md").is_file()
    # opencode 어댑터.
    assert (dest / ".opencode").is_dir()
    assert (dest / "AGENTS.md").is_file()
    # 공유 엔진은 한 벌.
    assert (dest / ".project_manager" / "tools" / "board.py").is_file()


def test_both_excludes_node_modules(pm_import, tmp_path):
    """opencode 의 node_modules 는 무겁고 재설치 대상 — 복사 제외."""
    dest = tmp_path / "dual2"
    rc = pm_import.main(["--new", str(dest), "--harness", "both", "--name", "D2"])
    assert rc == 0
    assert not (dest / ".opencode" / "node_modules").exists()


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
FULL_AGENTS_MARKER = "opencode 세션이 시작될 때 자동 로드"  # full AGENTS.md 만의 문구.


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


def test_weight_lite_both_places_both_lite_entries(pm_import, tmp_path, capsys):
    """--weight lite (both): CLAUDE.md·AGENTS.md 둘 다 lite · 어떤 *.lite.md 도 dst 부재."""
    dest = tmp_path / "liteb"
    rc = pm_import.main(["--new", str(dest), "--harness", "both", "--weight", "lite",
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
    # both 중복-relpath 충돌 경고가 없어야 한다. (가드 제거 시 lite 와 full 이 같은 dst X.md 로
    # 충돌해 "내용 불일치" 경고가 새어나온다.) engine.manifest·README.md 의 충돌 경고는 lite 와
    # 무관한 기존 both 동작이므로 진입 파일명으로 한정해 검사한다.
    conflict_lines = [ln for ln in capsys.readouterr().err.splitlines()
                      if "중복 relpath 내용 불일치" in ln]
    assert not any("CLAUDE.md" in ln or "AGENTS.md" in ln for ln in conflict_lines), \
        f"진입 파일 충돌 경고 누출 — (c) 가드 회귀: {conflict_lines}"


def test_weight_full_excludes_lite_variants(pm_import, tmp_path):
    """--weight full(기본): CLAUDE.md = full 진입 · dst 에 *.lite.md 없음(lite 변종 제외)."""
    dest = tmp_path / "fullp"
    rc = pm_import.main(["--new", str(dest), "--harness", "both", "--weight", "full",
                         "--name", "FP"])
    assert rc == 0
    claude_text = (dest / "CLAUDE.md").read_text(encoding="utf-8")
    agents_text = (dest / "AGENTS.md").read_text(encoding="utf-8")
    # full 진입 = full 고유 마커 존재 · lite 마커 부재.
    assert FULL_CLAUDE_MARKER in claude_text and LITE_MARKER not in claude_text
    assert FULL_AGENTS_MARKER in agents_text and LITE_MARKER not in agents_text
    # full 모드도 lite 변종은 배포에 끼면 안 됨.
    assert _lite_md_files(dest) == [], f"full 배포에 *.lite.md 잔존: {_lite_md_files(dest)}"


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


# ── MF3: both 중복 relpath 내용 불일치 — 경고 + claude_code 우선 ──────────────

def test_both_conflicting_relpath_warns_and_prefers_claude(pm_import, tmp_path, capsys):
    """engine.manifest 는 두 어댑터에서 내용이 달라(claude_code 가 상위집합) — both 에서
    claude_code 것이 채택되고 stderr 경고가 남는다. 조용한 정책 손실 금지."""
    dest = tmp_path / "bothconflict"
    rc = pm_import.main(["--new", str(dest), "--harness", "both", "--name", "BC"])
    assert rc == 0

    captured = capsys.readouterr()
    assert "engine.manifest" in captured.err, "내용 다른 중복 relpath 경고가 stderr 에 없음."
    assert "claude_code" in captured.err, "채택 트리(claude_code 우선)가 경고에 명시되지 않음."

    # 채택된 engine.manifest 는 claude_code 것 — .claude/agents·regression.yml 을 sync 범위에 포함.
    src_claude = REPO / "templates" / "claude_code" / ".project_manager" / "engine.manifest"
    dest_manifest = dest / ".project_manager" / "engine.manifest"
    assert dest_manifest.read_text(encoding="utf-8") == src_claude.read_text(encoding="utf-8"), \
        "both 의 engine.manifest 가 claude_code(우선) 것이 아님 — MF3 정책 위반."


def test_both_identical_relpath_silent(pm_import, tmp_path, capsys):
    """byte-identical 한 공유 엔진(board.py 등)은 경고 없이 조용히 한 번만 복사."""
    dest = tmp_path / "bothsilent"
    rc = pm_import.main(["--new", str(dest), "--harness", "both", "--name", "BS"])
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
    이 통째로 덮으면 external_review_enabled·reviewer_cmd·session 등이 무백업 손실된다.

    T-0021 메모 — external_review_enabled 보존(아래 assert)은 **T-0017 의 board.py
    EOF/비대화 가드**에 의존한다: board init 은 pm_import 가 stdin=DEVNULL 로 호출하므로
    `prompt_external_review_optin` 은 비대화(isatty=False/EOF)로 판정해 **아무것도 쓰지
    않고 반환**해야 한다. 그래야 reapply_preserved_conf_keys 가 백업의 사용자값('true')을
    그대로 재병합한다. board.py 가 pre-fix(가드 없음)면 init 이 `external_review_enabled=false`
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
        "external_review_enabled=true\n"
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
    assert conf.get("external_review_enabled") == "true", \
        f"external_review_enabled 가 보존되지 않음: {conf.get('external_review_enabled')!r}"
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
    사용자 키(external_review_enabled)도 보존된다.

    T-0184 이전엔 board init 이 local.conf 를 통째 덮어 session 이 init 솔로 기본('pm')으로
    리셋됐고 기존 'mine' 은 백업에만 남았다(데이터 손실 버그). 이제 cmd_init 은 local.conf
    존재 시 병합하며 session·prefix 는 *명시 인자일 때만* 교체한다 — pm_import 의
    run_board_init 은 --session 을 넘기지 않으므로 기존 session 이 보존된다."""
    dest = tmp_path / "precedence"
    dest.mkdir()
    pm_dir = dest / ".project_manager"
    pm_dir.mkdir()
    (pm_dir / "local.conf").write_text(
        "session=mine\nexternal_review_enabled=false\n", encoding="utf-8"
    )

    rc = pm_import.main(["--into", str(dest), "--harness", "claude", "--name", "Prec"])
    assert rc == 0

    conf = _parse_conf(pm_dir / "local.conf")
    # 명시 --session 이 없으므로 기존 session 이 보존된다(T-0184 비파괴 병합).
    assert conf.get("session") == "mine", \
        f"재-import 가 기존 session 을 보존하지 않음(T-0184 비파괴 병합 기대): {conf.get('session')!r}"
    # board init 이 안 쓰는 사용자 키는 보존.
    assert conf.get("external_review_enabled") == "false"


# ── T-0071: run_board_init 이 subprocess env 에 PM_NONINTERACTIVE=1 명시 전달 ──

def test_run_board_init_passes_pm_noninteractive_env(pm_import, tmp_path, monkeypatch):
    """run_board_init 이 board init subprocess env 에 PM_NONINTERACTIVE=1 을 넣는지.

    Windows DEVNULL stdin 의 isatty() 신뢰불가 함정 회피(T-0071) — stdin=DEVNULL 와 함께
    env 명시 신호로 external_review opt-in 프롬프트를 결정적 skip. 실 board init 을 돌리지
    않고 subprocess.run 을 가로채(부작용 0) 전달된 env/stdin 만 친다.
    """
    # board.py 존재 가드를 통과시킬 더미 트리.
    board = tmp_path / ".project_manager" / "tools" / "board.py"
    board.parent.mkdir(parents=True)
    board.write_text("# stub\n", encoding="utf-8")

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
    # 기존 원본 — session 은 다른 값('mine'), 추가로 external_review_enabled 보유.
    original = "session=mine\nexternal_review_enabled=true\nreviewer_cmd=bar\n"
    changed = pm_import.reapply_preserved_conf_keys(dest, original)
    assert changed is True

    conf = _parse_conf(pm_dir / "local.conf")
    # 현재 파일에 있던 키는 불변(init 값 우선).
    assert conf["session"] == "pm"
    assert conf["project_name"] == "New"
    # 현재 파일에 없던 기존 키만 재병합.
    assert conf["external_review_enabled"] == "true"
    assert conf["reviewer_cmd"] == "bar"


# ── SF3: __pycache__ / .pyc 복사 제외 ────────────────────────────────────────

def test_import_excludes_pycache(pm_import, tmp_path):
    """__pycache__/*.pyc(stale 바이트코드)는 새 프로젝트로 복사되지 않는다."""
    dest = tmp_path / "nopyc"
    rc = pm_import.main(["--new", str(dest), "--harness", "both", "--name", "NP"])
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
    assert os.readlink(backup) == str(link_target), "백업 링크가 원래 대상을 가리키지 않음."

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
    dev = dest / ".opencode" / "agents" / "developer.md"
    assert "ollama/qwen3.6:27b" in dev.read_text(encoding="utf-8")


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
    dev = dest / ".opencode" / "agents" / "developer.md"
    assert "opencode/big-pickle" in dev.read_text(encoding="utf-8")
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
    dev = dest / ".opencode" / "agents" / "developer.md"
    dev_text = dev.read_text(encoding="utf-8")
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
    dev_text = (dest / ".opencode" / "agents" / "developer.md").read_text(encoding="utf-8")
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


def test_opencode_model_fallback_only_comments_model_field_not_prose(pm_import, tmp_path):
    """폴백 주석화는 agent 의 `model:` 필드 줄만 — README 산문/헤더의 토큰은 안 건드린다(T-0077 PM 게이트).

    README 는 placeholder 를 *문서화* 하는 산문(`…placeholder {{OPENCODE_PRO_MODEL}} 로 출하된다`)·
    헤더(`### 모델 선택 (\`{{OPENCODE_PRO_MODEL}}\` …)`)에 토큰을 담는다. 마커가 `토큰 in line` 만 보고
    `# ` prepend 하면 그 산문이 markdown H1 로 깨진다 → 마커는 `model:` 필드 줄로 한정해야 한다.
    """
    dest = tmp_path / "readmesafe"
    rc = pm_import.main(["--new", str(dest), "--harness", "opencode", "--name", "ReadmeSafe"])
    assert rc == 0
    readme = (dest / "README.md").read_text(encoding="utf-8")
    # README 는 폴백 후에도 placeholder 토큰을 *문서화* 형태로 보존(치환/주석화 안 됨).
    assert OPENCODE_MODEL_TOKEN in readme
    # 폴백 TODO 마커는 `model:` 필드 줄에만 — README 엔 그 필드가 없으니 마커 0(산문 무손상).
    assert "# TODO: opencode 모델 ID" not in readme, "README 산문/헤더 토큰에 폴백 마커가 붙어 깨짐"
    # 토큰 든 산문 줄이 `# ` prepend 로 H1 화되지 않았다(원 산문/헤더 그대로).
    for line in readme.splitlines():
        if OPENCODE_MODEL_TOKEN in line and not line.lstrip().startswith("###"):
            assert not line.lstrip().startswith("# "), f"README 산문 줄이 # prepend 로 깨짐: {line!r}"


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
    가 _real_models_runner 를 (False, []) 로 고정 → 폴백)로 import 한 3개 subagent frontmatter 가드.
    """
    dest = tmp_path / "fmvalid"
    rc = pm_import.main(["--new", str(dest), "--harness", "opencode", "--name", "FmValid"])
    assert rc == 0
    agents_dir = dest / ".opencode" / "agents"
    for name in ("developer.md", "code-reviewer.md", "architect.md"):
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
        assert OPENCODE_MODEL_TOKEN not in text, \
            f"{name}: @render 경로 agent 에 리터럴 모델 토큰 잔존 → render leak"
        # 그래도 발견경로는 보존: 주석 model: 줄 + 형식 힌트(<provider/model>) + TODO 안내.
        assert "<provider/model>" in text, f"{name}: 폴백 형식 힌트(<provider/model>) 소실"
        assert re.search(r"^#\s*model:", text[: end + 5], re.MULTILINE), \
            f"{name}: model 줄이 `# model:` 로 주석화되지 않음"
        # frontmatter 영역에 HTML 주석 잔류 0 (YAML 깨짐 방지 — T-0033 codex must-fix 회귀 가드).
        assert "<!--" not in text[: end + 5], f"{name}: frontmatter 에 HTML 주석 잔류"


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
    dev = dest / ".opencode" / "agents" / "developer.md"
    assert "ollama/qwen3.6:27b" in dev.read_text(encoding="utf-8")


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
    (dest / "README.md").write_text("user readme\n", encoding="utf-8")
    today = datetime.date.today().isoformat()
    # 실 import — tmp 디렉토리는 git repo 가 아니므로 git_safe_relpaths → None → 전부 백업.
    rc = pm_import.main(["--into", str(dest), "--harness", "claude", "--name", "NonGit"])
    assert rc == 0
    backup_root = dest / pm_import.BACKUP_DIR_NAME / today
    assert (backup_root / "CLAUDE.md").is_file(), "비-git 충돌 CLAUDE.md 가 중앙 백업되지 않음."
    assert (backup_root / "README.md").is_file(), "비-git 충돌 README.md 가 중앙 백업되지 않음."
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


def test_opencode_import_substitutes_operational_tokens(pm_import, tmp_path):
    """opencode import 후 신규 스캐폴드의 operational {{토큰}} 잔존 0(엔진 문서 제외).

    claude `test_new_substitutes_operational_placeholders` 의 opencode 대칭 — 새 스캐폴드
    (status.md·log/current.md·README 등)의 `{{DATE}}`/`{{PROJECT_NAME}}`/`{{PY}}` 가 import 시
    실제 치환되는지 자동 단언(dangling 토큰=onboarding 깨짐). 양 harness 동일 sed 경로지만
    대칭 단언으로 사각을 닫는다(reviewer should-fix·T-0051).
    """
    dest = tmp_path / "oc_tokens"
    rc = pm_import.main(["--new", str(dest), "--harness", "opencode", "--name", "OcTokens"])
    assert rc == 0
    for token in OPERATIONAL_TOKENS:
        hits = _grep_token_files(dest, token, exclude_engine_docs=True)
        assert hits == [], f"{token} 잔존(엔진 문서 제외): {hits}"


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
    `--session` 1순위 안내로 적응했다. 그 한 줄 외 나머지 본문은 claude 와 동일해야 파리티가 의미를 가진다.
    """
    oc_readme = (
        REPO / "templates" / "opencode" / ".project_manager" / "wiki" / "tickets" / "README.md"
    ).read_text(encoding="utf-8")
    # harness-무관 1순위 안내가 들어있어야 한다.
    assert "--session" in oc_readme
    assert "harness-무관" in oc_readme, "세션 식별 절이 harness-무관 안내로 적응되지 않음."
    # claude 원본의 'CLAUDE_SESSION_NAME 환경변수\n1.' 단독 우선 형태가 그대로 남아있지 않아야 한다.
    assert "1. `CLAUDE_SESSION_NAME` 환경변수" not in oc_readme, \
        "claude-특화 세션 env 안내가 그대로 잔존 — opencode 적응 누락."
