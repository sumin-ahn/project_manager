"""진입 doc 세대 마이그레이션 단위 테스트 (T-0409·ADR-0069).

구형 채택자의 자족 매뉴얼형 opencode `AGENTS.md` → 신형(공통 코어 + `.opencode/pm-instructions.md`
+ `opencode.jsonc` `instructions` 배열) 조건부 자동 마이그레이션. 판정 = 미수정 여부(치환-불변
정규화·해시 대조). plan/apply 분리라 tmp 디렉토리만으로 외부 의존 없이 검증한다.

시나리오(DoD):
  - 미수정 구형 사본 → 자동 전환 + 백업 + jsonc idempotent 추가.
  - 수정 구형 사본 → 무손 + loud 안내(무 write).
  - 신형/재실행 → no-op 멱등(부분 전환 시 jsonc 만 복구).
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
NEW_TEMPLATE = REPO / "templates" / "opencode" / "AGENTS.md"

# 시뮬레이션용 operational 값(출하 import 시 채택자별 치환) — tagline 은 local.conf 미보유분이라
# 포획 경로를 태우기 위해 고유 문자열을 쓴다(신형 재렌더 보존 검증).
_SIM_OP = {
    "PY": "python3",
    "PROJECT_NAME": "myproj",
    "PROJECT_TAGLINE": "UNIQUE_TAGLINE_XYZ_12345",
    "TEST_CMD": "pytest tests/ -q",
}
_MANUAL_MARKER = " <!-- TODO: 손으로 채우세요 -->"

_MINIMAL_OLD_JSONC = '{\n  "$schema": "https://opencode.ai/config.json",\n  "compaction": {\n    "auto": false\n  }\n}\n'


def _load_pm_update():
    spec = importlib.util.spec_from_file_location("pm_update", TOOLS / "pm_update.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# 임베드 세대 수 + 세대 index → git 출하 blob ref (provenance lock). parametrize collection 시점에
# 필요해 모듈 로드 시 1회 계산. 새 세대 추가 시 _GEN_GIT_REFS 에 그 세대 ref 를 등록해야
# test_all_generations_have_provenance_ref 가 통과한다(lock 자동 강제).
_NUM_GENERATIONS = len(_load_pm_update()._OLD_OPENCODE_AGENTS_GENERATIONS)
_GEN_GIT_REFS = {0: "0ccc025:templates/opencode/AGENTS.md"}


@pytest.fixture(scope="module")
def pm_update():
    return _load_pm_update()


@pytest.fixture(scope="module")
def old_gen_text(pm_update):
    """임베드 세대 원본(구형 opencode AGENTS.md·토큰 form) 텍스트."""
    return pm_update._decode_entry_doc_generation(
        pm_update._OLD_OPENCODE_AGENTS_GENERATIONS[0])


def _sim_old_adopter(gen_text: str, op: dict, *, manual_markers: bool = True) -> str:
    """세대 원본 → import 렌더 채택자본 시뮬레이션.

    pm_import 의 두 변환을 복제한다: (1) substitute_placeholders — operational 토큰을 값으로 치환,
    (2) _mark_todos(manual·기본) — free-form placeholder 줄 끝에 TODO 마커 덧붙임."""
    text = gen_text
    for key, val in op.items():
        text = text.replace("{{" + key + "}}", val)
    if not manual_markers:
        return text
    out = []
    for line in text.splitlines(keepends=True):
        if "{{PROJECT_CONSTRAINTS}}" in line and "TODO" not in line:
            eol = "\n" if line.endswith("\n") else ""
            out.append(line.rstrip("\n") + _MANUAL_MARKER + eol)
        else:
            out.append(line)
    return "".join(out)


def _make_dest(tmp_path: Path, agents: str, jsonc: str,
               conf: str = "py=python3\nproject_name=myproj\ntest_cmd=pytest tests/ -q\n") -> Path:
    """채택자 dest 트리(AGENTS.md·opencode.jsonc·local.conf) 구성."""
    dest = tmp_path / "dest"
    (dest / ".opencode").mkdir(parents=True)
    (dest / ".project_manager").mkdir(parents=True)
    (dest / "AGENTS.md").write_text(agents, encoding="utf-8")
    (dest / ".opencode" / "opencode.jsonc").write_text(jsonc, encoding="utf-8")
    (dest / ".project_manager" / "local.conf").write_text(conf, encoding="utf-8")
    return dest


def _make_source(tmp_path: Path, *, new_template: bool = True) -> Path:
    """upstream source 트리 — 신형 AGENTS.md 템플릿(마이그레이션 목적지)."""
    src = tmp_path / "src"
    tdir = src / "templates" / "opencode"
    tdir.mkdir(parents=True)
    if new_template:
        (tdir / "AGENTS.md").write_text(
            NEW_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    return src


def _make_selfupdate_pair(tmp_path: Path, agents: str) -> tuple[Path, Path]:
    """main() self-update 가 apply(changes) 에 도달하는 최소 source/dest 쌍(시퀀싱 테스트용).

    manifest 1엔트리(`engine_file.txt`)를 source 는 보유·dest 는 부재 → plan 이 'new' change 를
    내어 apply(changes) 경로에 진입한다. dest 는 구형 opencode 채택자(AGENTS.md·opencode.jsonc·
    local.conf)."""
    src = tmp_path / "src"
    (src / ".project_manager").mkdir(parents=True)
    (src / ".project_manager" / "engine.manifest").write_text(
        "engine_file.txt\n", encoding="utf-8")
    (src / "engine_file.txt").write_text("NEW ENGINE CONTENT\n", encoding="utf-8")
    (src / "templates" / "opencode").mkdir(parents=True)
    (src / "templates" / "opencode" / "AGENTS.md").write_text(
        NEW_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    dest = tmp_path / "dest"
    (dest / ".opencode").mkdir(parents=True)
    (dest / ".project_manager").mkdir(parents=True)
    (dest / ".project_manager" / "engine.manifest").write_text(
        "engine_file.txt\n", encoding="utf-8")  # engine_file.txt 는 dest 부재 → 'new' change
    (dest / "AGENTS.md").write_text(agents, encoding="utf-8")
    (dest / ".opencode" / "opencode.jsonc").write_text(_MINIMAL_OLD_JSONC, encoding="utf-8")
    (dest / ".project_manager" / "local.conf").write_text(
        "py=python3\nproject_name=myproj\ntest_cmd=pytest tests/ -q\n"
        "additional_reviewer_enabled=false\n", encoding="utf-8")
    return dest, src


# ── fingerprint 자산 provenance (전 세대 순회·lock 자동 강제) ────────────────

@pytest.mark.parametrize("idx", range(_NUM_GENERATIONS))
def test_generation_decodes_and_structure(pm_update, idx):
    """각 임베드 세대(zlib+base64)가 온전히 디코드되고 구형 세대 구조를 갖는다."""
    text = pm_update._decode_entry_doc_generation(
        pm_update._OLD_OPENCODE_AGENTS_GENERATIONS[idx])
    assert pm_update._ENTRY_DOC_OLD_GEN_MARKER in text  # 구형 세대 판별자
    assert "{{PROJECT_CONSTRAINTS}}" in text            # free-form 토큰 form 보존


@pytest.mark.parametrize("idx,git_ref", sorted(_GEN_GIT_REFS.items()))
def test_generation_provenance_matches_git(pm_update, idx, git_ref):
    """각 임베드 세대 == git 출하 blob(무결성 lock·역대 출하본 진위)."""
    try:
        git = subprocess.run(
            ["git", "-C", str(REPO), "show", git_ref],
            capture_output=True, text=True, encoding="utf-8", check=True, timeout=15,
        ).stdout
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        pytest.skip("git 미가용 — provenance 대조 skip (자산 무결성은 decode 로만 검증)")
    decoded = pm_update._decode_entry_doc_generation(
        pm_update._OLD_OPENCODE_AGENTS_GENERATIONS[idx])
    assert decoded == git, f"임베드 세대 #{idx} 가 git {git_ref} 와 다르다(자산 stale/손상)."


def test_all_generations_have_provenance_ref():
    """모든 임베드 세대가 git provenance ref 를 갖는다 — 새 세대 추가 시 lock 등록을 강제."""
    assert set(_GEN_GIT_REFS) == set(range(_NUM_GENERATIONS)), (
        "새 세대를 _OLD_OPENCODE_AGENTS_GENERATIONS 에 추가하면 _GEN_GIT_REFS 에 그 세대의 "
        "git blob ref 를 등록해 provenance lock 을 강제하라.")


# ── _ensure_jsonc_instructions (idempotent·comment-preserving) ──────────────

def test_jsonc_insert_when_absent(pm_update):
    new, changed = pm_update._ensure_jsonc_instructions(_MINIMAL_OLD_JSONC)
    assert changed
    assert '".opencode/pm-instructions.md"' in new
    assert '"instructions"' in new
    assert '"compaction"' in new  # 기존 키 보존


def test_jsonc_noop_when_present(pm_update):
    src = '{\n  "instructions": [\n    ".opencode/pm-instructions.md"\n  ],\n  "x": 1\n}\n'
    new, changed = pm_update._ensure_jsonc_instructions(src)
    assert not changed
    assert new == src


def test_jsonc_append_when_array_without_path(pm_update):
    src = '{\n  "instructions": [\n    ".opencode/other.md"\n  ]\n}\n'
    new, changed = pm_update._ensure_jsonc_instructions(src)
    assert changed
    assert ".opencode/pm-instructions.md" in new
    assert ".opencode/other.md" in new  # 기존 원소 보존


def test_jsonc_empty_array_insert_parses_strict(pm_update):
    """빈 `instructions` 배열 삽입 산출이 strict JSON 으로 파싱된다 — 후행 쉼표를 남기지 않는다.

    앞머리에 `"…",` 를 넣던 옛 삽입은 이을 원소가 없어 `[ "…",]` 가 됐고 opencode 가 config 를
    못 읽었다(실측)."""
    src = '{\n  "instructions": []\n}\n'
    new, changed = pm_update._ensure_jsonc_instructions(src)
    assert changed
    assert json.loads(new) == {"instructions": [".opencode/pm-instructions.md"]}, \
        f"빈 배열 삽입 산출이 strict JSON 이 아니다: {new!r}"


def test_jsonc_comment_only_array_insert_parses_masked(pm_update):
    """주석만 있는 배열도 '원소 0' — 쉼표 없이 삽입되고 주석은 보존된다(마스킹 기준 parse 통과)."""
    src = '{\n  "instructions": [\n    // 아직 없음\n  ]\n}\n'
    new, changed = pm_update._ensure_jsonc_instructions(src)
    assert changed
    assert "// 아직 없음" in new, "주석이 소실됐다(비파괴 위반)"
    masked = pm_update._mask_jsonc_comments(new)
    assert json.loads(masked) == {"instructions": [".opencode/pm-instructions.md"]}, \
        f"주석-마스킹 기준으로도 파싱 불가(후행 쉼표 잔존?): {new!r}"


def test_jsonc_nonempty_array_insert_keeps_comma_and_parses(pm_update):
    """비어있지 않은 배열은 현행 동작 무변경 — 앞머리 쉼표 삽입·기존 원소 보존·재실행 멱등."""
    src = '{\n  "instructions": [\n    ".opencode/other.md"\n  ]\n}\n'
    new, changed = pm_update._ensure_jsonc_instructions(src)
    assert changed
    assert json.loads(new) == {
        "instructions": [".opencode/pm-instructions.md", ".opencode/other.md"]}
    again, changed_again = pm_update._ensure_jsonc_instructions(new)
    assert not changed_again and again == new


def test_jsonc_nonstring_element_array_keeps_comma(pm_update):
    """비-문자열 원소 배열(`[123]`)도 '비어있지 않음' — 쉼표 삽입으로 strict JSON 을 유지한다.

    "문자열 원소가 있나" 로 좁히면 이런 배열을 빈 배열로 오인해 쉼표를 빼고, `[ "…"  123]` 처럼
    구분자 없는 산출이 된다(같은 결함의 다른 모양). 판정을 '비-공백 바디' 로 두어 클래스를 닫는다."""
    src = '{\n  "instructions": [123]\n}\n'
    new, changed = pm_update._ensure_jsonc_instructions(src)
    assert changed
    assert json.loads(new) == {"instructions": [".opencode/pm-instructions.md", 123]}, \
        f"비-문자열 원소 배열 삽입 산출이 strict JSON 이 아니다: {new!r}"


def test_jsonc_empty_array_insert_is_idempotent(pm_update):
    """빈 배열에 삽입한 산출을 다시 통과시켜도 무변경(멱등) — 세대 왕복에서 중복 등록 없음."""
    once, _ = pm_update._ensure_jsonc_instructions('{\n  "instructions": []\n}\n')
    twice, changed = pm_update._ensure_jsonc_instructions(once)
    assert not changed and twice == once


def test_jsonc_idempotent_double_run(pm_update):
    once, _ = pm_update._ensure_jsonc_instructions(_MINIMAL_OLD_JSONC)
    twice, changed = pm_update._ensure_jsonc_instructions(once)
    assert not changed
    assert twice == once


def test_jsonc_comment_mention_not_false_positive(pm_update):
    """주석의 'instructions 배열' 서술은 코드 배열이 아니라 신설 블록이 삽입된다(quote+colon+bracket 판별)."""
    src = '{\n  // instructions 배열로 로드한다\n  "x": 1\n}\n'
    new, changed = pm_update._ensure_jsonc_instructions(src)
    assert changed
    assert '"instructions"' in new


def test_jsonc_commented_out_path_not_idempotent(pm_update):
    """주석-아웃된 경로는 '등록'이 아니다 (codex R2) — 실제 (비-주석) 배열 원소로 추가돼야 한다."""
    src = ('{\n  "instructions": [\n    // ".opencode/pm-instructions.md"\n'
           '    ".other.md"\n  ]\n}\n')
    new, changed = pm_update._ensure_jsonc_instructions(src)
    assert changed, "주석-아웃 경로를 이미 등록으로 오인하면 안 된다."
    # 비-주석 배열 원소로 실제 등록됐는지 (마스킹본에서 확인).
    masked = pm_update._mask_jsonc_comments(new)
    body = re.search(r'"instructions"\s*:\s*\[(.*?)\]', masked, re.DOTALL).group(1)
    assert ".opencode/pm-instructions.md" in re.findall(r'"([^"]*)"', body)


def test_jsonc_suffix_string_not_idempotent(pm_update):
    """suffix 문자열(`.bak`)은 다른 경로 (codex R2) — substring 오인 없이 정확 경로가 추가된다."""
    src = '{\n  "instructions": [\n    ".opencode/pm-instructions.md.bak"\n  ]\n}\n'
    new, changed = pm_update._ensure_jsonc_instructions(src)
    assert changed, "`.bak` suffix 를 이미 등록으로 오인하면 안 된다."
    masked = pm_update._mask_jsonc_comments(new)
    body = re.search(r'"instructions"\s*:\s*\[(.*?)\]', masked, re.DOTALL).group(1)
    elements = re.findall(r'"([^"]*)"', body)
    assert ".opencode/pm-instructions.md" in elements      # 정확 경로 추가
    assert ".opencode/pm-instructions.md.bak" in elements  # 기존 원소 보존


def test_jsonc_ignores_comment_inside_string(pm_update):
    """문자열 내부 `//`($schema URL)는 주석이 아니다 — 마스킹이 문자열을 존중(오프셋 보존)."""
    src = ('{\n  "$schema": "https://opencode.ai/config.json",\n'
           '  "instructions": [\n    ".opencode/pm-instructions.md"\n  ]\n}\n')
    new, changed = pm_update._ensure_jsonc_instructions(src)
    assert not changed  # 이미 등록 — idempotent
    assert new == src


def _toplevel_elements(pm_update, jsonc_text):
    """결과 jsonc 의 최상위 instructions 배열 원소 목록(테스트 단언용)."""
    masked = pm_update._mask_jsonc_comments(jsonc_text)
    bs, be, _ = pm_update._find_toplevel_instructions(masked)
    assert bs is not None, "최상위 instructions 배열이 없다."
    return re.findall(r'"([^"]*)"', masked[bs:be])


def test_jsonc_nested_instructions_ignored(pm_update):
    """중첩 객체의 "instructions" 는 대상 아님 (codex R2) — 최상위 신설·중첩 배열 보존."""
    src = '{\n  "agent": {\n    "x": {\n      "instructions": ["x.md"]\n    }\n  }\n}\n'
    new, changed = pm_update._ensure_jsonc_instructions(src)
    assert changed
    assert '"x.md"' in new  # 중첩 배열 보존
    top = _toplevel_elements(pm_update, new)
    assert ".opencode/pm-instructions.md" in top  # 최상위(depth==1)에 신설
    assert "x.md" not in top                       # 중첩 원소가 최상위로 새지 않음


def test_jsonc_nested_and_toplevel_append_toplevel_only(pm_update):
    """중첩+최상위 공존 시 최상위에만 append (중첩 배열 무변경·정확 1회 등록)."""
    src = ('{\n  "agent": {\n    "x": { "instructions": ["nested.md"] }\n  },\n'
           '  "instructions": [\n    "top.md"\n  ]\n}\n')
    new, changed = pm_update._ensure_jsonc_instructions(src)
    assert changed
    top = _toplevel_elements(pm_update, new)
    assert ".opencode/pm-instructions.md" in top  # 최상위에 등록
    assert "top.md" in top                         # 기존 최상위 원소 보존
    assert "nested.md" not in top                  # 중첩 원소는 최상위 밖
    assert '"nested.md"' in new                    # 중첩 배열 보존
    assert new.count(".opencode/pm-instructions.md") == 1  # 중첩엔 미삽입


def test_jsonc_nested_before_toplevel_idempotent(pm_update):
    """중첩 "instructions" 가 먼저 나와도 최상위(이미 등록)를 찾아 no-op (오삽입 안 함)."""
    src = ('{\n  "agent": { "instructions": ["nested.md"] },\n'
           '  "instructions": [\n    ".opencode/pm-instructions.md"\n  ]\n}\n')
    new, changed = pm_update._ensure_jsonc_instructions(src)
    assert not changed
    assert new == src


# ── _match_entry_doc_generation (치환-불변 정규화) ──────────────────────────

def test_match_rendered_no_markers(pm_update, old_gen_text):
    adopter = _sim_old_adopter(old_gen_text, _SIM_OP, manual_markers=False)
    keys = pm_update._entry_doc_operational_keys()
    captured = pm_update._match_entry_doc_generation(old_gen_text, adopter, keys)
    assert captured is not None
    assert captured["PY"] == "python3"
    assert captured["PROJECT_TAGLINE"] == "UNIQUE_TAGLINE_XYZ_12345"


def test_match_rendered_with_manual_markers(pm_update, old_gen_text):
    """manual-fill TODO 마커가 붙어도 정규화로 벗겨 매칭(pristine 판정)."""
    adopter = _sim_old_adopter(old_gen_text, _SIM_OP, manual_markers=True)
    assert _MANUAL_MARKER in adopter
    keys = pm_update._entry_doc_operational_keys()
    captured = pm_update._match_entry_doc_generation(old_gen_text, adopter, keys)
    assert captured is not None
    assert captured["PROJECT_TAGLINE"] == "UNIQUE_TAGLINE_XYZ_12345"


def test_match_none_when_freeform_filled(pm_update, old_gen_text):
    """free-form 채움(커스텀) → 리터럴 토큰 부재 → 불일치(None)."""
    adopter = _sim_old_adopter(old_gen_text, _SIM_OP).replace(
        "{{PROJECT_CONSTRAINTS}}", "MY CUSTOM CONSTRAINT")
    keys = pm_update._entry_doc_operational_keys()
    assert pm_update._match_entry_doc_generation(old_gen_text, adopter, keys) is None


def test_match_none_when_structural_edit(pm_update, old_gen_text):
    """구조 손편집 → 불일치(None)."""
    adopter = _sim_old_adopter(old_gen_text, _SIM_OP) + "\n\n## 채택자가 덧붙인 절\n"
    keys = pm_update._entry_doc_operational_keys()
    assert pm_update._match_entry_doc_generation(old_gen_text, adopter, keys) is None


def test_match_none_when_inconsistent_operational(pm_update, old_gen_text):
    """같은 operational 토큰 occurrence 가 값 불일치(손편집) → None(안전 낙하)."""
    adopter = _sim_old_adopter(old_gen_text, _SIM_OP, manual_markers=False)
    # 첫 `python3 .project_manager` 를 다른 인터프리터로 손편집 → occurrence 불일관.
    adopter = adopter.replace("python3 .project_manager", "py -3.12 .project_manager", 1)
    keys = pm_update._entry_doc_operational_keys()
    assert pm_update._match_entry_doc_generation(old_gen_text, adopter, keys) is None


# ── migrate_entry_doc 시나리오 (DoD) ───────────────────────────────────────

def test_scenario_unmodified_auto_migrate(pm_update, old_gen_text, tmp_path):
    """미수정 구형 사본: pm_update → 자동 전환 + 백업 + jsonc idempotent (DoD 1)."""
    old_agents = _sim_old_adopter(old_gen_text, _SIM_OP)
    dest = _make_dest(tmp_path, old_agents, _MINIMAL_OLD_JSONC)
    src = _make_source(tmp_path)

    result = pm_update.migrate_entry_doc(dest, src, write=True)

    assert result["status"] == "migrated"
    assert result["agents_replaced"] is True
    assert result["jsonc_updated"] is True
    assert result["matched_generation"] == 0

    new = (dest / "AGENTS.md").read_text(encoding="utf-8")
    # 신형 교체 — 구형 marker 소거·신형 title.
    assert pm_update._ENTRY_DOC_OLD_GEN_MARKER not in new
    assert new.splitlines()[0] == "# AGENTS.md — PM 어댑터 공통 코어"
    # operational 해소(leak 0) + 채택자 tagline 보존.
    assert "{{PY}}" not in new and "{{PROJECT_NAME}}" not in new
    assert "UNIQUE_TAGLINE_XYZ_12345" in new
    # free-form 은 pristine 유지(신선 import --fill manual 동형).
    assert "{{PROJECT_CONSTRAINTS}}" in new

    # 백업 — 원본 AGENTS.md 를 중앙 백업 채널에 보존.
    backups = list((dest / ".pm_import_backups").rglob("AGENTS.md"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == old_agents

    # jsonc — instructions 배열에 신형 지침 등록.
    jsonc = (dest / ".opencode" / "opencode.jsonc").read_text(encoding="utf-8")
    assert ".opencode/pm-instructions.md" in jsonc


def test_scenario_unmodified_auto_migrate_no_markers(pm_update, old_gen_text, tmp_path):
    """마커 없는(raw) 미수정 사본도 자동 전환."""
    old_agents = _sim_old_adopter(old_gen_text, _SIM_OP, manual_markers=False)
    dest = _make_dest(tmp_path, old_agents, _MINIMAL_OLD_JSONC)
    src = _make_source(tmp_path)
    result = pm_update.migrate_entry_doc(dest, src, write=True)
    assert result["status"] == "migrated"
    assert pm_update._ENTRY_DOC_OLD_GEN_MARKER not in (
        dest / "AGENTS.md"
    ).read_text(encoding="utf-8")


def test_scenario_modified_loud_no_touch(pm_update, old_gen_text, tmp_path):
    """수정 구형 사본(free-form 채움): 무손 + loud 안내(DoD 2)."""
    modified = _sim_old_adopter(old_gen_text, _SIM_OP).replace(
        "{{PROJECT_CONSTRAINTS}}", "MY CUSTOM CONSTRAINT")
    dest = _make_dest(tmp_path, modified, _MINIMAL_OLD_JSONC)
    src = _make_source(tmp_path)

    result = pm_update.migrate_entry_doc(dest, src, write=True)

    assert result["status"] == "loud_manual"
    assert result["agents_replaced"] is False
    # 무손 — AGENTS.md·opencode.jsonc 미터치.
    assert (dest / "AGENTS.md").read_text(encoding="utf-8") == modified
    assert (dest / ".opencode" / "opencode.jsonc").read_text(encoding="utf-8") == _MINIMAL_OLD_JSONC
    assert not (dest / ".pm_import_backups").exists()


def test_scenario_modified_structural_loud(pm_update, old_gen_text, tmp_path):
    """구조 손편집 구형 사본도 loud(무손)."""
    modified = _sim_old_adopter(old_gen_text, _SIM_OP) + "\n## 손편집 절\n"
    dest = _make_dest(tmp_path, modified, _MINIMAL_OLD_JSONC)
    src = _make_source(tmp_path)
    result = pm_update.migrate_entry_doc(dest, src, write=True)
    assert result["status"] == "loud_manual"
    assert (dest / "AGENTS.md").read_text(encoding="utf-8") == modified


def test_scenario_rerun_noop_idempotent(pm_update, old_gen_text, tmp_path):
    """전환 후 재실행 → no-op 멱등(DoD 3)."""
    old_agents = _sim_old_adopter(old_gen_text, _SIM_OP)
    dest = _make_dest(tmp_path, old_agents, _MINIMAL_OLD_JSONC)
    src = _make_source(tmp_path)

    first = pm_update.migrate_entry_doc(dest, src, write=True)
    assert first["status"] == "migrated"

    agents_after = (dest / "AGENTS.md").read_text(encoding="utf-8")
    jsonc_after = (dest / ".opencode" / "opencode.jsonc").read_text(encoding="utf-8")

    second = pm_update.migrate_entry_doc(dest, src, write=True)
    assert second["status"] == "noop"
    assert second["agents_replaced"] is False
    assert second["jsonc_updated"] is False
    assert (dest / "AGENTS.md").read_text(encoding="utf-8") == agents_after
    assert (dest / ".opencode" / "opencode.jsonc").read_text(encoding="utf-8") == jsonc_after


def test_scenario_fresh_newgen_noop(pm_update, tmp_path):
    """신형 채택자(신형 AGENTS.md + jsonc instructions 보유) → no-op."""
    new_rendered = NEW_TEMPLATE.read_text(encoding="utf-8").replace(
        "{{PY}}", "python3").replace("{{PROJECT_NAME}}", "myproj").replace(
        "{{PROJECT_TAGLINE}}", "tag").replace("{{TEST_CMD}}", "pytest -q")
    jsonc = '{\n  "instructions": [\n    ".opencode/pm-instructions.md"\n  ],\n  "x": 1\n}\n'
    dest = _make_dest(tmp_path, new_rendered, jsonc)
    src = _make_source(tmp_path)
    result = pm_update.migrate_entry_doc(dest, src, write=True)
    assert result["status"] == "noop"
    assert result["jsonc_updated"] is False


def test_scenario_partial_recovery(pm_update, tmp_path):
    """부분 전환(신형 AGENTS.md 이나 jsonc instructions 부재) → jsonc 만 idempotent 복구."""
    new_rendered = NEW_TEMPLATE.read_text(encoding="utf-8").replace(
        "{{PY}}", "python3").replace("{{PROJECT_NAME}}", "myproj").replace(
        "{{PROJECT_TAGLINE}}", "tag").replace("{{TEST_CMD}}", "pytest -q")
    dest = _make_dest(tmp_path, new_rendered, _MINIMAL_OLD_JSONC)
    src = _make_source(tmp_path)
    result = pm_update.migrate_entry_doc(dest, src, write=True)
    assert result["status"] == "recovered"
    assert result["jsonc_updated"] is True
    assert ".opencode/pm-instructions.md" in (
        dest / ".opencode" / "opencode.jsonc").read_text(encoding="utf-8")


def test_gate_not_opencode(pm_update, tmp_path):
    """opencode.jsonc 부재(비-opencode 채택자) → 비발화(not_opencode)."""
    dest = tmp_path / "dest"
    (dest / ".project_manager").mkdir(parents=True)
    (dest / "AGENTS.md").write_text("whatever", encoding="utf-8")
    src = _make_source(tmp_path)
    result = pm_update.migrate_entry_doc(dest, src, write=True)
    assert result["status"] == "not_opencode"


def test_gate_no_agents(pm_update, tmp_path):
    """AGENTS.md 부재 → no_agents(무동작)."""
    dest = tmp_path / "dest"
    (dest / ".opencode").mkdir(parents=True)
    (dest / ".opencode" / "opencode.jsonc").write_text(_MINIMAL_OLD_JSONC, encoding="utf-8")
    src = _make_source(tmp_path)
    result = pm_update.migrate_entry_doc(dest, src, write=True)
    assert result["status"] == "no_agents"


def test_no_new_template_failsoft(pm_update, old_gen_text, tmp_path):
    """신형 목적지(source 템플릿) 부재 → fail-soft(no_new_template·무손)."""
    old_agents = _sim_old_adopter(old_gen_text, _SIM_OP)
    dest = _make_dest(tmp_path, old_agents, _MINIMAL_OLD_JSONC)
    src = _make_source(tmp_path, new_template=False)
    result = pm_update.migrate_entry_doc(dest, src, write=True)
    assert result["status"] == "no_new_template"
    assert (dest / "AGENTS.md").read_text(encoding="utf-8") == old_agents


def test_dry_run_no_write(pm_update, old_gen_text, tmp_path):
    """write=False(dry-run) → 판정만·무 write."""
    old_agents = _sim_old_adopter(old_gen_text, _SIM_OP)
    dest = _make_dest(tmp_path, old_agents, _MINIMAL_OLD_JSONC)
    src = _make_source(tmp_path)
    result = pm_update.migrate_entry_doc(dest, src, write=False)
    assert result["status"] == "migrated"
    assert result["backup_rel"] is None
    # 미 write — 원본 그대로.
    assert (dest / "AGENTS.md").read_text(encoding="utf-8") == old_agents
    assert not (dest / ".pm_import_backups").exists()


# ── --target 비발화 (self-update 경로 한정 게이트) ─────────────────────────

def test_target_export_does_not_migrate(pm_update, monkeypatch, capsys):
    """`--target`(엔진 export)은 진입 doc 마이그레이션을 발화하지 않는다(skew/selfheal 동일 경계)."""
    calls = []
    orig = pm_update.migrate_entry_doc

    def _spy(*a, **k):
        calls.append((a, k))
        return orig(*a, **k)

    monkeypatch.setattr(pm_update, "migrate_entry_doc", _spy)
    rc = pm_update.main(["--target", "opencode", "--from", str(REPO), "--dry-run"])
    assert rc == 0
    assert calls == [], "--target 모드에서 migrate_entry_doc 가 호출됐다(비발화 위반)."


def test_self_update_invokes_migration(pm_update, monkeypatch):
    """self-update(--target 없음) 경로는 migrate_entry_doc 를 발화한다(--target 비발화의 대칭 증거).

    REPO 를 source 이자 self-dest 로 한 dry-run — source==dest 라 엔진 변경 0·부작용 0. REPO 루트엔
    opencode.jsonc 부재라 migrate 는 not_opencode(무해)지만, *호출됐다*는 사실이 wiring 을 입증한다."""
    calls = []
    orig = pm_update.migrate_entry_doc

    def _spy(*a, **k):
        calls.append(k.get("write"))
        return orig(*a, **k)

    monkeypatch.setattr(pm_update, "migrate_entry_doc", _spy)
    rc = pm_update.main(["--from", str(REPO), "--dry-run"])
    assert rc == 0
    assert calls == [False], "self-update dry-run 이 migrate_entry_doc(write=False)를 1회 호출해야 한다."


def test_migrate_without_local_conf(pm_update, old_gen_text, tmp_path):
    """local.conf 부재여도 포획 operational 값만으로 자족 전환(재렌더 leak 0·tagline 보존)."""
    old_agents = _sim_old_adopter(old_gen_text, _SIM_OP)
    dest = tmp_path / "dest"
    (dest / ".opencode").mkdir(parents=True)
    (dest / "AGENTS.md").write_text(old_agents, encoding="utf-8")
    (dest / ".opencode" / "opencode.jsonc").write_text(_MINIMAL_OLD_JSONC, encoding="utf-8")
    # local.conf 없음(.project_manager 미생성).
    src = _make_source(tmp_path)
    result = pm_update.migrate_entry_doc(dest, src, write=True)
    assert result["status"] == "migrated"
    new = (dest / "AGENTS.md").read_text(encoding="utf-8")
    assert "{{PY}}" not in new and "{{TEST_CMD}}" not in new
    assert "UNIQUE_TAGLINE_XYZ_12345" in new  # 포획 tagline 보존


# ── 시퀀싱 (codex R1·전환 write 는 apply(changes) 성공 이후) ────────────────

def test_sequencing_migration_after_apply_success(pm_update, old_gen_text, tmp_path, monkeypatch):
    """self-update(changes 有) 성공 경로: migrate 전환이 apply(changes) *이후* 발화한다."""
    dest, src = _make_selfupdate_pair(tmp_path, _sim_old_adopter(old_gen_text, _SIM_OP))
    order = []
    real_migrate = pm_update.migrate_entry_doc

    def spy_apply(changes, **_kwargs):  # 호출부가 훅 세트 판정자를 함께 넘긴다(T-0606).
        order.append("apply")

    def spy_migrate(*a, **k):
        order.append(("migrate", k.get("write")))
        return real_migrate(*a, **k)

    monkeypatch.setattr(pm_update, "REPO", dest)      # self-location dest = tmp
    monkeypatch.setattr(pm_update, "apply", spy_apply)
    monkeypatch.setattr(pm_update, "migrate_entry_doc", spy_migrate)
    rc = pm_update.main(["--from", str(src)])
    assert rc == 0
    assert order == ["apply", ("migrate", True)], order


def test_sequencing_apply_failure_leaves_old_gen(pm_update, old_gen_text, tmp_path, monkeypatch):
    """apply(changes) 실패 시 전환 write 미발화 — 채택자는 완전한 구형에 남는다(반쪽 상태 방지·R1)."""
    old_agents = _sim_old_adopter(old_gen_text, _SIM_OP)
    dest, src = _make_selfupdate_pair(tmp_path, old_agents)
    migrate_writes = []
    real_migrate = pm_update.migrate_entry_doc

    class ExpectedApplyFailure(RuntimeError):
        pass

    def boom_apply(changes, **_kwargs):  # 호출부가 훅 세트 판정자를 함께 넘긴다(T-0606).
        raise ExpectedApplyFailure("apply failed (render/IO)")

    def spy_migrate(*a, **k):
        migrate_writes.append(k.get("write"))
        return real_migrate(*a, **k)

    monkeypatch.setattr(pm_update, "REPO", dest)
    monkeypatch.setattr(pm_update, "apply", boom_apply)
    monkeypatch.setattr(pm_update, "migrate_entry_doc", spy_migrate)
    with pytest.raises(ExpectedApplyFailure, match="apply failed"):
        pm_update.main(["--from", str(src)])
    # 전환 write(write=True) 미발화 — apply 뒤라 도달 못 함.
    assert True not in migrate_writes, "apply 실패 후 migrate(write=True) 가 발화됐다(반쪽 상태 위험)."
    # 구형 무손 — AGENTS.md·opencode.jsonc 원본 그대로.
    assert (dest / "AGENTS.md").read_text(encoding="utf-8") == old_agents
    assert (dest / ".opencode" / "opencode.jsonc").read_text(encoding="utf-8") == _MINIMAL_OLD_JSONC
    assert not (dest / ".pm_import_backups").exists()


def test_finding_printer_loud_manual(pm_update, capsys):
    """loud_manual finding 이 안내 + 커맨드를 출력한다."""
    pm_update._print_entry_doc_migration_finding(
        {"status": "loud_manual"}, dry_run=False)
    out = capsys.readouterr().out
    assert "자동 전환하지 않는다" in out
    assert ".opencode/pm-instructions.md" in out


def test_finding_printer_migrated_backup(pm_update, capsys):
    pm_update._print_entry_doc_migration_finding(
        {"status": "migrated", "matched_generation": 0, "jsonc_updated": True,
         "backup_rel": ".pm_import_backups/2026-07-21"}, dry_run=False)
    out = capsys.readouterr().out
    assert "세대 마이그레이션 전환" in out
    assert ".pm_import_backups/2026-07-21" in out
