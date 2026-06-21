"""scripts/bench_weight.py 순수 함수 단위 테스트 (T-0008).

라이브 호출 0 — claude/opencode subprocess 를 일절 부르지 않고, 캡처 stub JSON
과 격리된 tmp 파일만으로 파싱·계측·표 생성 로직을 검증한다.

검증 축:
  - parse_claude_usage — 실제 캡처 JSON 정규화 정확성.
  - parse_opencode_usage — JSONL 이벤트 스트림 파싱·합산·방어성(누락 키 KeyError 안 남).
  - count_entry_files — byte/char/per-file breakdown·합계·누락 파일 처리.
  - render_table — 표 구조(헤더·full/lite 행·placeholder).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def bench():
    return _load("bench_weight")


# ── 실제 캡처 stub ────────────────────────────────────────────────────────────

# claude `-p --output-format json` 실제 캡처 (T-0008 본문 제공).
_CLAUDE_STUB = (
    '{"type":"result","subtype":"success","is_error":false,"duration_ms":1723,'
    '"num_turns":1,"result":"ok","total_cost_usd":0.07335799999999999,'
    '"usage":{"input_tokens":4441,"cache_creation_input_tokens":4651,'
    '"cache_read_input_tokens":7946,"output_tokens":4},'
    '"modelUsage":{"claude-opus-4-8":{"inputTokens":4441,"outputTokens":4,'
    '"cacheReadInputTokens":7946,"cacheCreationInputTokens":4651,'
    '"costUSD":0.07278799999999999,"contextWindow":1000000}}}'
)

# opencode `run --format json` 실제 캡처 (라이브 검증 — JSONL 이벤트 스트림).
# step_finish.part.tokens.{total,input,output,cache.{write,read}} + part.cost.
_OPENCODE_STUB = "\n".join(
    [
        '{"type":"step_start","timestamp":1,"sessionID":"ses_x",'
        '"part":{"id":"prt_a","type":"step-start"}}',
        '{"type":"text","timestamp":2,"sessionID":"ses_x",'
        '"part":{"id":"prt_b","type":"text","text":"done"}}',
        '{"type":"step_finish","timestamp":3,"sessionID":"ses_x",'
        '"part":{"id":"prt_c","type":"step-finish","reason":"stop",'
        '"tokens":{"total":6983,"input":6977,"output":6,"reasoning":0,'
        '"cache":{"write":0,"read":0}},"cost":0.0012}}',
    ]
)


# ── 1. parse_claude_usage ─────────────────────────────────────────────────────

def test_parse_claude_usage_normalizes(bench):
    u = bench.parse_claude_usage(_CLAUDE_STUB)
    assert u["input"] == 4441
    assert u["output"] == 4
    assert u["cache_read"] == 7946
    assert u["cache_creation"] == 4651
    assert u["cost"] == pytest.approx(0.07335799999999999)
    assert u["num_turns"] == 1
    assert u["result"] == "ok"
    assert u["model"] == "claude-opus-4-8"
    assert u["context_window"] == 1000000


def test_parse_claude_usage_missing_keys_safe(bench):
    """usage·modelUsage 누락 시 KeyError 없이 0/None fallback."""
    u = bench.parse_claude_usage('{"total_cost_usd":0.5}')
    assert u["input"] == 0
    assert u["output"] == 0
    assert u["cache_read"] == 0
    assert u["cache_creation"] == 0
    assert u["cost"] == pytest.approx(0.5)
    assert u["model"] is None
    assert u["context_window"] is None


# ── 2. parse_opencode_usage ───────────────────────────────────────────────────

def test_parse_opencode_usage_jsonl_stream(bench):
    u = bench.parse_opencode_usage(_OPENCODE_STUB)
    assert u["input"] == 6977
    assert u["output"] == 6
    assert u["total"] == 6983
    assert u["cache_read"] == 0
    assert u["cache_creation"] == 0
    assert u["cost"] == pytest.approx(0.0012)
    assert u["result"] == "done"


def test_parse_opencode_usage_aggregates_multistep(bench):
    """멀티스텝(tool 사용)이면 step_finish 토큰·비용을 합산하고 text 를 이어붙인다."""
    multi = "\n".join(
        [
            '{"type":"text","part":{"type":"text","text":"a"}}',
            '{"type":"step_finish","part":{"type":"step-finish",'
            '"tokens":{"total":100,"input":80,"output":20,"cache":{"read":5,"write":3}},'
            '"cost":0.001}}',
            '{"type":"text","part":{"type":"text","text":"b"}}',
            '{"type":"step_finish","part":{"type":"step-finish",'
            '"tokens":{"total":50,"input":40,"output":10,"cache":{"read":2,"write":1}},'
            '"cost":0.002}}',
        ]
    )
    u = bench.parse_opencode_usage(multi)
    assert u["input"] == 120
    assert u["output"] == 30
    assert u["total"] == 150
    assert u["cache_read"] == 7
    assert u["cache_creation"] == 4
    assert u["cost"] == pytest.approx(0.003)
    assert u["result"] == "ab"


def test_parse_opencode_usage_missing_keys_safe(bench):
    """토큰·cost 없는 이벤트·빈 입력에 KeyError 안 남(방어성)."""
    assert bench.parse_opencode_usage("") == {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_creation": 0,
        "total": 0,
        "cost": 0.0,
        "result": "",
    }
    # 깨진 줄·part 없는 이벤트 혼재 — 건너뛰고 0 반환.
    messy = '\n'.join(['{not json}', '{"type":"step_finish"}', '{"type":"text"}'])
    u = bench.parse_opencode_usage(messy)
    assert u["input"] == 0 and u["cost"] == 0.0


def test_parse_opencode_usage_array_variant(bench):
    """단일 JSON 배열 변종도 관용적으로 흡수한다(스키마 불확실성 방어)."""
    arr = (
        '[{"type":"step_finish","part":{"type":"step-finish",'
        '"tokens":{"total":10,"input":8,"output":2},"cost":0.0}}]'
    )
    u = bench.parse_opencode_usage(arr)
    assert u["input"] == 8 and u["output"] == 2 and u["total"] == 10


# ── 3. count_entry_files ──────────────────────────────────────────────────────

def test_count_entry_files_breakdown_and_totals(bench, tmp_path):
    (tmp_path / "a.md").write_text("hello", encoding="utf-8")  # 5 bytes/chars
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.md").write_text("worldworld", encoding="utf-8")  # 10
    counts = bench.count_entry_files(["a.md", "sub/b.md"], tmp_path)

    assert counts["total_bytes"] == 15
    assert counts["total_chars"] == 15
    assert counts["total_approx_tokens_char4"] == 15 // 4
    assert len(counts["files"]) == 2

    by_path = {f["path"]: f for f in counts["files"]}
    assert by_path["a.md"]["bytes"] == 5
    assert by_path["a.md"]["chars"] == 5
    assert by_path["a.md"]["approx_tokens_char4"] == 1
    assert by_path["a.md"]["exists"] is True
    assert by_path["sub/b.md"]["bytes"] == 10


def test_count_entry_files_missing_file(bench, tmp_path):
    """누락 파일은 exists=False·0 으로 기록(raise 안 함) — plan 모드 가시화용."""
    counts = bench.count_entry_files(["nope.md"], tmp_path)
    f = counts["files"][0]
    assert f["exists"] is False
    assert f["bytes"] == 0 and f["chars"] == 0
    assert counts["total_bytes"] == 0


def test_count_entry_files_multibyte_chars(bench, tmp_path):
    """멀티바이트(한글)는 bytes != chars — 둘 다 정확히 잰다."""
    (tmp_path / "k.md").write_text("가나", encoding="utf-8")  # 2 chars / 6 bytes
    counts = bench.count_entry_files(["k.md"], tmp_path)
    f = counts["files"][0]
    assert f["chars"] == 2
    assert f["bytes"] == 6


# ── 4. render_table ───────────────────────────────────────────────────────────

def test_render_table_structure(bench):
    rows = [
        {"weight": "full", "entry_tokens": 5000, "scenario_tokens": 12000, "self_sufficiency": "yes"},
        {"weight": "lite", "entry_tokens": 2000, "scenario_tokens": None, "self_sufficiency": "PM-manual-pending"},
    ]
    table = bench.render_table(rows)
    lines = table.splitlines()
    # 헤더 + 구분선 + 2 데이터 행.
    assert len(lines) == 4
    assert lines[0].startswith("|") and "weight" in lines[0]
    assert set(lines[1].replace(" ", "").strip("|").split("|")) == {"---"}
    # full·lite 행이 존재한다.
    assert any(c.strip() == "full" for c in lines[2].split("|"))
    assert any(c.strip() == "lite" for c in lines[3].split("|"))
    # 누락 값(None)은 "—" placeholder.
    assert "—" in lines[3]


def test_render_table_empty_rows(bench):
    """행이 없어도 헤더·구분선은 나온다(빈 표 안전)."""
    table = bench.render_table([])
    assert len(table.splitlines()) == 2


# ── 5. plan(dry) 모드 — 라이브 호출 없이 진입셋·프롬프트 출력 ──────────────────

def test_lite_fileset_excludes_methodology_docs(bench):
    """lite 프록시는 full 에서 pm_role·pm_playbook 만 뺀다(= lite-A 가설)."""
    assert bench.LITE_FILESET == (
        "CLAUDE.md",
        ".project_manager/wiki/pm_state.md",
        ".project_manager/wiki/status.md",
    )
    assert ".project_manager/wiki/pm_role.md" in bench.FULL_FILESET
    assert ".project_manager/wiki/pm_role.md" not in bench.LITE_FILESET
    assert ".project_manager/wiki/pm_playbook.md" not in bench.LITE_FILESET


def test_render_plan_no_measurement(bench, tmp_path):
    """plan 모드는 측정 없이 진입셋·프롬프트·sandbox 계획만 출력한다(토큰 0)."""
    (tmp_path / "CLAUDE.md").write_text("x" * 40, encoding="utf-8")
    plan = bench.render_plan("opencode", "lite", "boot-1", tmp_path)
    assert "dry — no measurement, 0 tokens" in plan
    assert "harness: opencode" in plan
    assert "weight: lite" in plan
    assert "Read these files:" in plan
    assert "XDG_" in plan


def test_build_entry_probe_prompt_lists_fileset(bench):
    prompt = bench.build_entry_probe_prompt(["CLAUDE.md", "x.md"])
    assert "CLAUDE.md" in prompt and "x.md" in prompt
    assert prompt.endswith(f"reply exactly: {bench.ENTRY_PROBE_REPLY}.")
