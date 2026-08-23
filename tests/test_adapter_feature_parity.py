"""어댑터 기능 세트 파리티 가드.

파일 축(경로·내용·facade) 교차 대조는 `test_manifest_template_parity.py`(`OPENCODE_ONLY_PATHS` 등)가
이미 한다. 그 축이 못 보는 것은 **기능 축**이다 — 가드/훅이 하네스마다 배선됐는가, 역할 카드가
전 하네스에 출하됐는가, 상태줄 같은 부가 표면이 어디에 있는가. 파일이 전파되는지는 보면서 그
파일이 구현하는 기능이 실제로 걸려 있는지는 아무 게이트도 안 물었다 — 이 파일은 **등록 하네스
전수**(`_harness_matrix.HARNESSES` 파생)에 대해 명시 표를 실 트리와 대조한다.

## 표 형태
각 행 = 기능 하나. 선언은 둘 중 하나다.
  - `"all"` — 등록 하네스 전수가 보유해야 한다.
  - `subset(하네스집합, "intent:<사유>" | "defect:<티켓 ID>")` — 그 집합만 보유하고, 사유가
    반드시 달린다. 사유 없는 비대칭은 그 자체로 red 다.

3단 단언(`_violations_for`):
  ① 트리 실측 보유 집합 == 선언 집합(하네스 단위로 어긋나면 red).
  ② `subset` 행의 분류가 `intent:`/`defect:` 접두 없이 비어 있으면 red.
  ③ 표에 없는 기능이 **어느 하네스에서든** 발견되면 red(신규/미분류 기능이 조용히 통과하지 않는다).

## 기능 ID 열거 축 (하네스별 — 이미 노출된 경로만 소비, 새 파서를 만들지 않는다)
  codex    — `.codex/pm_orch_codex.py` 의 `hook_feature_registry()`(디스패처가 기계 소비용으로
             낸다). raw `feature_id` 는 이벤트별로 갈라져(`ctx-nudge-pretooluse` 등) 있어
             `_CODEX_FEATURE_TO_ROW` 로 표의 기능 단위로 묶는다. 매핑에 없는 raw id 는 그대로
             행 이름 취급되어(폴백 identity) 표에 없으면 단언③에 자동으로 걸린다 — "모르는 기능은
             통과" 가 성립하지 않는다.
  opencode — `.opencode/plugins/*.js` 파일명(확장자 제거) 자체가 곧 표의 행 이름이다(플러그인
             1개 = 기능 1개인 이 하네스의 배선 방식 그대로).
  claude   — `.claude/settings.json` 의 훅 커맨드. 파싱은 `pm_import.py` 가 이미 갖고 있는
             `_hook_commands`/`_split_hook_command`/`_hook_script_and_arguments`(어댑터 훅 세트
             세대 판정이 쓰는 것과 같은 함수)를 그대로 재사용한다 — 커맨드 파서를 이 파일이
             두 번째로 쓰지 않는다. (스크립트명, 플래그 집합) 쌍을 `_CLAUDE_HOOK_COMMAND_TO_ROWS`
             로 표의 기능 단위에 매핑한다.
  역할 카드 — `templates/<하네스>/<1차 어댑터 디렉토리>/agents/*` 파일명(확장자 제거) 그 자체가
             행 이름이다.
  lite 루트 문서 — `_harness_matrix.entry_docs(harness)[1]`(root 문서의 `.lite.md` 변형) 존재.
  상태줄   — claude 만 판정 대상이다(`settings.json.statusLine` 키 존재). opencode·codex 는
             기계로 확인해도(아래 결정 근거) 사용자 스크립트로 상태줄을 대체하는 설정 표면 자체가
             없어 raw enumeration 이 애초에 나오지 않는다 — 그래서 opencode/codex 는 이 축에서
             "found" 가 생기지 않고, "found" 가 없는 축은 단언③이 물을 대상도 없다(비대칭이
             표 밖으로 새는 경로가 아니다).

## opencode·codex 상태줄 등가 표면 실측 (착수 시 조사 — intent 분류 근거)
  - codex: 프로젝트 `templates/codex/.codex/config.toml` 은 `[tui]` 섹션을 싣지 않는다(그 키는
    사용자 홈 `~/.codex/config.toml` 전용). 홈 설정에 `[tui].status_line` 이 있어도 값은
    `model-with-reasoning`·`current-dir`·`context-used`·`weekly-limit` 같은 **고정 토큰**의
    나열이지 임의 커맨드를 실행하는 커스텀 스크립트 훅이 아니다(`codex --help`/`codex features`
    에 커스텀 상태줄 커맨드 기능 없음 — 실측).
  - opencode: 바이너리 문자열에 있는 `statusline` 참조는 내장 TUI 상태표시줄의 표시 토글
    (`showActivityMeta`·`showModel`·`showCommandHint` 등)이며 외부 커맨드를 실행하는 훅이 아니다
    (`opencode --help` 에도 상태줄 커스터마이즈 서브커맨드 없음).
  결론 — 두 하네스 모두 claude `statusLine.command`(임의 스크립트 실행) 같은 **사용자 스크립트
  훅**을 제공하지 않는다. 만들 수 있는데 안 만든 결함이 아니라 하네스 자체에 그 표면이 없다 →
  `intent` 분류.

## ADAPTER_HOOK_SET 확장 대신 이 파일 하나로 두는 판단
`pm_import.ADAPTER_HOOK_SET`(`AdapterHookSetSpec`)의 관심사는 **세대 정합**이다 — "채택자
config 가 부르는 파일이 지금 이 엔진 세대가 기대하는 버전인가"(`flag_support`·`coupled_groups`).
그 표는 파일이 함께 움직여야 하는 **결합 묶음** 단위로 조직돼 있어(예: claude 의 한 결합 묶음이
ctx 넛지 + precompact 캡처를 이미 한 데 묶는다), 이 티켓이 원하는 **기능 존재 단위**(ctx-guard와
principle-recall을 별개 행으로 보는 것)와 분할선이 다르다. 거기에 기능 축 필드를 얹으면 "세대
정합에 쓰는 분할"과 "기능 존재 판정에 쓰는 분할"이 같은 스키마에서 충돌한다(같은 규칙 두 벌은
반드시 drift 한다). 대신 하네스마다 이미 노출된 기능 열거 경로(`hook_feature_registry`·plugins
파일명·settings.json 훅 커맨드)를 그대로 소비하면 새 스키마 없이 이 파일 하나로 충분하다 —
`pm_import.py` 비수정.

hermetic — 실 파일 읽기/JSON 파싱만 한다(하네스 실행·LLM 진입 없음). 민감도는 합성 픽스처로
증명한다(`test_facade_guard_is_sensitive_to_missing_facade` 류 선례와 동형).
"""
from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path
from typing import NamedTuple

import pytest

from _harness_matrix import HARNESS_ADAPTER_DIRS, HARNESSES, TEMPLATES, _PM_IMPORT, entry_docs

REPO = Path(__file__).resolve().parents[1]


class SubsetExpectation(NamedTuple):
    """`all` 이 아닌 행의 선언 — 보유해야 할 하네스 집합 + 분류(`intent:`/`defect:` 접두 필수)."""
    harnesses: frozenset[str]
    classification: str


def subset(harnesses, classification: str) -> SubsetExpectation:
    return SubsetExpectation(frozenset(harnesses), classification)


ALL = "all"  # 등록 하네스 전수 보유 선언 sentinel.

# ── codex: raw feature_id → 표의 기능 행 ────────────────────────────────────
# `hook_feature_registry()` 가 내는 feature_id 는 이벤트별로 갈라진다(예: ctx 넛지가
# PreToolUse/UserPromptSubmit 둘로 등록). 여기서 기능 단위로 접는다. 매핑에 없는 새 feature_id는
# 이 표에 그대로 행 이름 취급되어(fallback identity — 아래 `_codex_hook_rows`) 표 밖으로 새지
# 않는다.
_CODEX_FEATURE_TO_ROW = {
    "delegate-channel": "delegate-channel",
    "delegate-channel-subagent": "delegate-channel",
    "ctx-nudge-pretooluse": "ctx-guard",
    "ctx-nudge-userpromptsubmit": "ctx-guard",
    "compaction-checkpoint-pre": "ctx-guard",
    "compaction-guidance": "ctx-guard",
    "compaction-checkpoint-post": "ctx-guard",
    "compaction-snapshot": "ctx-guard",
    "principle-recall-pretooluse": "principle-recall",
    "principle-recall-userpromptsubmit": "principle-recall",
    "principle-recall-rearm": "principle-recall",
    "git-anchor": "git-anchor",
}

# ── claude: (스크립트명, 정렬된 플래그 튜플) → 표의 기능 행(들) ─────────────────
# ctx_stop_hook.sh(무플래그)는 ctx 넛지와 판단 원칙 recall 을 한 스크립트 안에서 함께 처리한다
# (claude 는 codex/opencode 처럼 기능별 파일을 안 가른다) — 그래서 두 행에 동시에 기여한다.
_CLAUDE_HOOK_COMMAND_TO_ROWS = {
    ("ctx_stop_hook.sh", ()): ("ctx-guard", "principle-recall"),
    ("ctx_stop_hook.sh", ("--git-anchor-hook",)): ("git-anchor",),
    ("delegate_channel_guard_hook.sh", ()): ("delegate-channel",),
    ("precompact_capture_hook.sh", ()): ("ctx-guard",),
}


def _template_root(templates_dir: Path, harness: str) -> Path:
    """등록 하네스 → 그 하네스의 출하 template 트리 루트(엔진 레지스트리에서 파생·손-열거 아님)."""
    (dirname,) = _PM_IMPORT.HARNESS_TEMPLATE_DIRS[harness]
    return templates_dir / dirname


def _load_module(name: str, path: Path):
    if not path.is_file():
        raise RuntimeError(f"모듈 파일 없음: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _codex_hook_rows(templates_dir: Path) -> set[str]:
    dispatcher = _load_module(
        "pm_orch_codex_feature_parity",
        _template_root(templates_dir, "codex") / ".codex" / "pm_orch_codex.py",
    )
    registry = dispatcher.hook_feature_registry()
    return {
        _CODEX_FEATURE_TO_ROW.get(feature["feature_id"], feature["feature_id"])
        for feature in registry["features"]
    }


def _opencode_hook_rows(templates_dir: Path) -> set[str]:
    plugins_dir = _template_root(templates_dir, "opencode") / ".opencode" / "plugins"
    if not plugins_dir.is_dir():
        raise RuntimeError(f"opencode plugins 디렉토리 없음: {plugins_dir}")
    return {path.stem for path in plugins_dir.glob("*.js")}


def _claude_hook_rows(templates_dir: Path) -> set[str]:
    settings_path = _template_root(templates_dir, "claude") / ".claude" / "settings.json"
    if not settings_path.is_file():
        raise RuntimeError(f"claude settings.json 없음: {settings_path}")
    document = json.loads(settings_path.read_text(encoding="utf-8"))
    rows: set[str] = set()
    for command in _PM_IMPORT._hook_commands(document):
        tokens = _PM_IMPORT._split_hook_command(command)
        script_rel, arguments = _PM_IMPORT._hook_script_and_arguments(tokens)
        if script_rel is None:
            continue
        script_name = Path(script_rel).name
        flags = tuple(sorted(a for a in arguments if a.startswith("-")))
        mapped = _CLAUDE_HOOK_COMMAND_TO_ROWS.get((script_name, flags))
        if mapped is None:
            rows.add(f"{script_name} {' '.join(flags)}".strip())  # fallback identity(③이 잡는다)
        else:
            rows.update(mapped)
    return rows


def _claude_has_statusline(templates_dir: Path) -> bool:
    settings_path = _template_root(templates_dir, "claude") / ".claude" / "settings.json"
    if not settings_path.is_file():
        raise RuntimeError(f"claude settings.json 없음: {settings_path}")
    document = json.loads(settings_path.read_text(encoding="utf-8"))
    return bool(document.get("statusLine"))


def _role_cards(templates_dir: Path, harness: str) -> set[str]:
    (adapter_dir, *_rest) = HARNESS_ADAPTER_DIRS[harness]
    agents_dir = _template_root(templates_dir, harness) / adapter_dir / "agents"
    if not agents_dir.is_dir():
        raise RuntimeError(f"{harness} agents 디렉토리 없음: {agents_dir}")
    return {path.stem for path in agents_dir.iterdir() if path.is_file()}


def _lite_doc_present(templates_dir: Path, harness: str) -> bool:
    lite_name = entry_docs(harness)[1]
    return (_template_root(templates_dir, harness) / lite_name).is_file()


# 하네스별 훅/플러그인 raw 열거 함수 — 축 자체(HARNESSES)는 파생이지만, config 형식이 하네스마다
# 구조적으로 다르므로(codex=디스패처 노출·opencode=디렉토리 스캔·claude=JSON 훅 커맨드) 판독기는
# 하네스별로 갈린다(`pm_import.ADAPTER_HOOK_SET` 이 이미 같은 이유로 하네스별 키를 쓴다). 표에
# 없는 하네스가 오면(4번째 하네스가 이 판독기 확장 없이 등록되면) loud fail 한다 — 조용한 미검사
# 하네스를 만들지 않는다.
_HOOK_ROW_READERS = {
    "codex": _codex_hook_rows,
    "opencode": _opencode_hook_rows,
    "claude": _claude_hook_rows,
}


def _harness_actual_features(templates_dir: Path, harness: str) -> frozenset[str]:
    reader = _HOOK_ROW_READERS.get(harness)
    if reader is None:
        raise RuntimeError(
            f"{harness}: 훅/플러그인 열거 판독기가 없다 — 새 하네스는 _HOOK_ROW_READERS 확장 필요"
        )
    rows = set(reader(templates_dir))
    rows |= _role_cards(templates_dir, harness)
    if _lite_doc_present(templates_dir, harness):
        rows.add("lite-root-doc")
    if harness == "claude" and _claude_has_statusline(templates_dir):
        rows.add("statusline")
    return frozenset(rows)


# ── 기능 세트 명시 표 ────────────────────────────────────────────────────────
TABLE: dict[str, str | SubsetExpectation] = {
    # 가드/훅 — 세 하네스 모두 배선됐다.
    "ctx-guard": ALL,
    "principle-recall": ALL,
    "delegate-channel": ALL,
    "git-anchor": ALL,
    # 역할 카드 — 5개 카드가 세 하네스 모두에 출하됐다.
    "architect": ALL,
    "code-reviewer": ALL,
    "developer": ALL,
    "developer-hard": ALL,
    "researcher": ALL,
    # 의도된 비대칭 — opencode 전용.
    "safe-write": subset(
        {"opencode"},
        "intent:opencode 1.17~1.18 대용량 write/edit 자동절단 실측 대응 — 다른 하네스는 그 결함이 "
        "없어 무해",
    ),
    "pm": subset(
        {"opencode"},
        "intent:orchestrator 가 PM 세션을 spawn 하는 타깃 카드 — 그 spawn 형상이 opencode 에서만 "
        "동작(카드 frontmatter description 명문)",
    ),
    # 미분류였던 2건 — 착수 시 실측으로 확정.
    "lite-root-doc": subset(
        {"claude", "opencode"},
        "intent:codex 는 lite 무게축 루트문서를 출하하지 않는다 — lite 변종 부재는 폴백으로 "
        "선언된 정상 상태(full 무게축 수용)",
    ),
    "statusline": subset(
        {"claude"},
        "intent:opencode·codex 모두 사용자 스크립트로 상태줄을 대체하는 설정 표면이 없다(고정 "
        "토큰 나열 또는 내장 표시 토글뿐 — 커스텀 커맨드 훅 부재를 --help/공식 문서로 실측)",
    ),
}


def _violations_for(
    table: dict[str, str | SubsetExpectation],
    actual: dict[str, frozenset[str]],
    harnesses: tuple[str, ...],
) -> list[str]:
    """순수 함수 — 표 선언과 실측 보유 집합을 3단으로 대조한다(파일시스템 미접근·hermetic)."""
    violations: list[str] = []
    all_found: set[str] = set()
    for features in actual.values():
        all_found |= features

    for row, expectation in table.items():
        if expectation == ALL:
            expected = frozenset(harnesses)
        elif isinstance(expectation, SubsetExpectation):
            expected = expectation.harnesses
            classification = expectation.classification
            if not classification or not classification.startswith(("intent:", "defect:")):
                violations.append(
                    f"{row}: subset 분류가 'intent:<사유>' 또는 'defect:<티켓 ID>' 형식이 아니다 "
                    f"({classification!r})"
                )
        else:
            violations.append(f"{row}: 선언 형식 불명({expectation!r}) — 'all' 또는 subset(...)")
            continue
        found = frozenset(h for h in harnesses if row in actual.get(h, frozenset()))
        if found != expected:
            violations.append(f"{row}: 선언={sorted(expected)} 실측={sorted(found)}")

    undeclared = all_found - set(table)
    if undeclared:
        violations.append(f"표에 없는 기능이 트리에서 발견됨: {sorted(undeclared)}")

    return violations


def _collect_violations(templates_dir: Path, harnesses: tuple[str, ...] = HARNESSES) -> list[str]:
    actual = {h: _harness_actual_features(templates_dir, h) for h in harnesses}
    return _violations_for(TABLE, actual, harnesses)


# ── 1) 표 ↔ 실 출하 트리 대조 (DoD 1·2·3) ───────────────────────────────────

def test_feature_table_matches_shipped_trees():
    violations = _collect_violations(TEMPLATES)
    assert not violations, "기능 파리티 위반:\n" + "\n".join(violations)


def test_dod_required_rows_are_present_and_classified():
    """DoD 4 — 선행 티켓이 닫은 3건은 `all`, 의도 비대칭 2건과 신규 분류 2건은 intent/defect 분류를
    갖는다(현행 스냅샷 — 결함으로 남는 행은 없다)."""
    assert TABLE["git-anchor"] == ALL
    assert TABLE["developer-hard"] == ALL
    assert TABLE["ctx-guard"] == ALL
    for row in ("safe-write", "pm", "lite-root-doc", "statusline"):
        expectation = TABLE[row]
        assert isinstance(expectation, SubsetExpectation), row
        assert expectation.classification.startswith(("intent:", "defect:")), row


def test_known_intentional_asymmetries_do_not_leak_into_other_harnesses():
    """역방향 확인 — safe-write·pm 카드처럼 의도된 비대칭이 표 밖(다른 하네스)으로 새지 않는다."""
    actual = {h: _harness_actual_features(TEMPLATES, h) for h in HARNESSES}
    for row in ("safe-write", "pm"):
        expected = TABLE[row].harnesses
        for harness in HARNESSES:
            present = row in actual[harness]
            assert present == (harness in expected), (
                f"{row}: {harness} 보유={present}, 기대={harness in expected}"
            )


def test_missing_tree_is_loud_fail_not_silent_pass():
    """트리가 없으면(손상·오타 경로) RuntimeError — vacuous green 금지(민감도 선례와 동형)."""
    nonexistent = REPO / "templates" / "__nonexistent_for_feature_parity__"
    with pytest.raises(RuntimeError):
        _collect_violations(nonexistent)


def test_unregistered_harness_is_loud_fail():
    """`_HOOK_ROW_READERS` 에 없는 하네스가 축에 섞이면(합성) 조용히 통과하지 않는다."""
    with pytest.raises(RuntimeError):
        _harness_actual_features(TEMPLATES, "__fourth_harness__")


# ── 2) 민감도 — 합성 선언 픽스처(실 트리 미접근) ────────────────────────────

def test_sensitivity_flags_asymmetry_without_classification():
    table = {"foo": subset({"claude"}, "")}
    actual = {"claude": frozenset({"foo"}), "opencode": frozenset(), "codex": frozenset()}
    violations = _violations_for(table, actual, HARNESSES)
    assert any("분류" in v for v in violations), violations


def test_sensitivity_accepts_defect_classification():
    """의도(intent)뿐 아니라 defect:<티켓 ID> 분류도 유효한 형식으로 통과한다(스펙이 요구하는
    두 값 중 하나 — 현재 표엔 defect 행이 없어 이 경로를 합성으로 별도 증명한다)."""
    table = {"foo": subset({"claude"}, "defect:T-0001")}
    actual = {"claude": frozenset({"foo"}), "opencode": frozenset(), "codex": frozenset()}
    assert _violations_for(table, actual, HARNESSES) == []


def test_sensitivity_flags_undeclared_feature():
    table: dict[str, str | SubsetExpectation] = {}
    actual = {"claude": frozenset({"mystery-feature"}), "opencode": frozenset(), "codex": frozenset()}
    violations = _violations_for(table, actual, HARNESSES)
    assert any("mystery-feature" in v for v in violations), violations


def test_sensitivity_flags_all_row_missing_from_one_harness():
    table = {"bar": ALL}
    actual = {"claude": frozenset({"bar"}), "opencode": frozenset(), "codex": frozenset({"bar"})}
    violations = _violations_for(table, actual, HARNESSES)
    assert any(v.startswith("bar:") for v in violations), violations


def test_sensitivity_green_path_has_no_false_positive():
    """올바르게 선언·분류된 표는 위반 0(non-vacuous 반대쪽 — 과검출 없음도 증명)."""
    table = {
        "bar": ALL,
        "baz": subset({"opencode"}, "intent:테스트 전용 합성 사유"),
    }
    actual = {
        "claude": frozenset({"bar"}),
        "opencode": frozenset({"bar", "baz"}),
        "codex": frozenset({"bar"}),
    }
    assert _violations_for(table, actual, HARNESSES) == []


# ── 3) 변이 프로브 — 실 출하 트리 사본에서 기능 1개를 빼면 red (non-vacuous·실 데이터) ─────

def test_removing_a_shipped_opencode_plugin_trips_red(tmp_path):
    mutated = tmp_path / "templates"
    shutil.copytree(TEMPLATES, mutated)
    (mutated / "opencode" / ".opencode" / "plugins" / "git-anchor.js").unlink()
    violations = _collect_violations(mutated)
    assert any("git-anchor" in v for v in violations), violations


def test_removing_a_shipped_codex_card_trips_red(tmp_path):
    mutated = tmp_path / "templates"
    shutil.copytree(TEMPLATES, mutated)
    (mutated / "codex" / ".codex" / "agents" / "developer-hard.toml").unlink()
    violations = _collect_violations(mutated)
    assert any("developer-hard" in v for v in violations), violations
