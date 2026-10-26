"""하네스 압축 한도 검증·네이티브 조회·계획·적용 (층: app)."""
from __future__ import annotations

from pathlib import Path
from pm_engine.domain import conf_schema
from pm_engine.domain import install_rules
from pm_engine.domain import jsonc
from pm_engine.infra import adapter_baseline
from pm_engine.infra import conf_store
from pm_engine.infra import fs
from pm_engine.infra.harness import contract
from typing import Callable, NamedTuple
import datetime
import json
import os
import re
import subprocess
import sys
import tomllib


# ── 하네스 사용 한도·압축 비율 반영 (local.conf → 하네스 설정 파일·압축 한도 기록) ─────────
# 적용 경로(pm-config apply·설치·add-harness·pm_update·어댑터 수용)가 local.conf 쓰기 락 안에서 도구마다
# 모델 한도 C 를 한 번 조회하고 적용 한도 E = min(W, C)·압축 지점 P = floor(E × compact_at / 100) 를
# 하네스 설정 파일과 압축 한도 기록(`conf_store.CTX_BUDGET_RELPATH`)에 쓴다. 런타임은 기록만 읽는다.
#   claude   `.claude/settings.json`     autoCompactWindow = P + d (d = 모델 창 − 기본 압축 기준) ·
#            idleCompaction = false (유휴 압축 끔 — 압축이 압축 지점에서만 나야 압축 전 안내가 성립한다)
#   codex    `.codex/config.toml`        model_auto_compact_token_limit = P
#   opencode `.opencode/opencode.jsonc`  대상 모델마다 limit.context(원본)·limit.input = E ·
#            limit.output(있으면 유지, 없으면 32000) · 최상위 compaction.reserved = E − P
# 쓰는 바이트는 이 관리 키의 값 자리(없어서 넣는 멤버·구분 쉼표 포함)뿐이다 — 재직렬화하지 않는다.
# opencode 전역 설정과 모델 목록 캐시는 읽기만 한다.
class _HarnessCtxRule(NamedTuple):
    """설정 파일 하나의 반영 규칙 — `keys` 는 (관리 키 경로, 값 식(도구 기록 항목)), `model_limits` 는 모델마다
    `limit` 을 쓰는 형식인가, `capacity_reader` 는 모델 한도 C 조회, `check` 는 쓰기 전 설정 형식 확인이다."""
    label: str
    keys: tuple[tuple[tuple[str, ...], Callable[[dict], int | bool]], ...]
    model_limits: bool
    capacity_reader: Callable[[Path, list, dict], list]
    check: Callable[[Path, dict], None]


# 설정 파일 relpath → 반영 규칙. 어느 하네스가 그 파일을 갖는지는 어댑터 config 채널
# (`ADAPTER_CONFIG_CHANNEL`)이 정한다 — 하네스 이름 표를 여기 두 번째로 두지 않는다.
HARNESS_CTX_RULES: dict[str, _HarnessCtxRule] = {
    ".claude/settings.json": _HarnessCtxRule(
        "Claude Code", ((("autoCompactWindow",), lambda entry: _ctx_claude_window(entry)),
                        (("idleCompaction",), lambda entry: False)),
        False, lambda *args: _ctx_claude_capacity(*args), lambda *args: _ctx_claude_check(*args)),
    ".codex/config.toml": _HarnessCtxRule(
        "Codex", ((("model_auto_compact_token_limit",), lambda entry: entry["P"]),),
        False, lambda *args: _ctx_codex_capacity(*args), lambda *args: _ctx_codex_check(*args)),
    # opencode 1.18.33: `limit.input` 이 있으면 `input − compaction.reserved` 에서 압축한다.
    ".opencode/opencode.jsonc": _HarnessCtxRule(
        "opencode", ((("compaction", "reserved"), lambda entry: entry["E"] - entry["P"]),),
        True, lambda *args: _ctx_opencode_capacity(*args), lambda root, entry: None),
}


# 하네스 이름 → 반영 대상 설정 파일(규칙 표 순서).
HARNESS_CTX_FILES: dict[str, str] = {
    harness: relpath
    for relpath in HARNESS_CTX_RULES
    for harness, channel in install_rules.ADAPTER_CONFIG_CHANNEL.items() if relpath in channel}


# Claude Code `autoCompactWindow` 가 받는 범위(`claude --help` 의 "100k~1M tokens").
CLAUDE_AUTO_COMPACT_WINDOW_RANGE = (100_000, 1_000_000)


# Codex 압축 지점 상한 — 실효 창 대비 %.
CODEX_COMPACT_LIMIT_PCT = 90


# 무턴 조회 응답 대기 상한(초) — 실측 Claude Code 1.6초·Codex 0.2초.
_CTX_PROTOCOL_TIMEOUT_SECONDS = 30


# opencode 전역 설정 — `<설정 기준 폴더>/opencode` 아래 세 이름을 이 순서로 읽어 깊은 병합한다
# (뒤가 이긴다 · opencode 1.18.33 실측).
OPENCODE_GLOBAL_CONFIG_SUBDIR = "opencode"


OPENCODE_GLOBAL_CONFIG_NAMES = ("config.json", "opencode.json", "opencode.jsonc")


# 어느 설정 파일에도 `limit.output` 이 없는 대상 모델에 쓰는 값이자 opencode 의 출력 예약 상한
# (OUTPUT_TOKEN_MAX·`OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX` 가 있으면 그 값). 설치본 규격이 파일마다
# `limit` 안의 context·output 을 함께 요구해 비워 둘 수 없다(어기면 opencode 가 기동하지 못한다).
OPENCODE_LIMIT_OUTPUT_DEFAULT = 32000


# opencode 가 설정 공급자에 패키지가 없을 때 쓰는 패키지(opencode 1.18.34 실측) — OpenAI 호환 주소 조회의
# 조건이자, 주소가 없으면 공급자 연결 설정이 없는 것으로 보는 패키지다(`_ctx_opencode_unconnected`).
OPENCODE_DEFAULT_PROVIDER_NPM = "@ai-sdk/openai-compatible"


# 모델마다 쓰는 `limit` 세 키 — context 는 원본 창, input 은 적용 한도, output 은 모델별 값이다.
_MODEL_LIMIT_KEYS = ("context", "input", "output")


_HARNESS_CTX_RATIO_KEYS = ("ctx.checkpoint_at_pct", "ctx.precompact_at_pct", "ctx.compact_at_pct")


_HARNESS_CTX_WINDOW_SUFFIX = "ctx_window_tokens"


# TOML 표 머리 줄(`[표]`·`[[표 배열]]`) — 최상위 키가 들어갈 구간의 끝이다. 괄호 안은 TOML 키
# 문법(맨 키·큰따옴표 키·작은따옴표 키를 점으로 이은 것)이고, 줄 끝 앞의 CR 은 CRLF 파일의 개행이다.
# 여러 줄 배열의 원소 줄(`[1, 2],`)은 쉼표 때문에 이 문법에 맞지 않는다.
_TOML_SIMPLE_KEY = r"""(?:[A-Za-z0-9_-]+|"(?:[^"\\\r\n]|\\.)*"|'[^'\r\n]*')"""


_TOML_DOTTED_KEY = rf"{_TOML_SIMPLE_KEY}(?:[ \t]*\.[ \t]*{_TOML_SIMPLE_KEY})*"


_TOML_TABLE_HEADER_RE = re.compile(
    rf"^[ \t]*(?:\[[ \t]*{_TOML_DOTTED_KEY}[ \t]*\]|\[\[[ \t]*{_TOML_DOTTED_KEY}[ \t]*\]\])"
    r"[ \t]*(?:#[^\r\n]*)?\r?$", re.MULTILINE)


class HarnessCtxError(ValueError):
    """사용 한도·압축 비율을 하네스 설정 파일에 반영할 수 없다 — 위반 전부를 한국어 줄로 담는다.
    검사·조회 단계에서 나면 어느 파일도 쓰지 않았고, 쓰기 단계에서 나면 `written` 이 교체를 마친 파일이다."""

    def __init__(self, violations, *, written=()) -> None:
        self.written = tuple(written)
        self.violations = tuple(violations)
        lead = (f"사용 한도·압축 비율을 {', '.join(written)} 에 반영한 뒤 멈췄습니다" if written
                else "사용 한도·압축 비율을 하네스 설정 파일에 반영하지 않았습니다")
        super().__init__("\n".join((
            f"{lead} — 아래를 고친 뒤 다시 실행하십시오.",
            *(f"  - {violation}" for violation in self.violations))))


def opencode_global_config_dir(env, home: Path) -> Path:
    """opencode 전역 설정 디렉터리 — `XDG_CONFIG_HOME` 이 비어 있지 않으면 그 아래, 아니면 `<home>/.config`.

    opencode 1.18.33 설치본의 식(`join(XDG_CONFIG_HOME || join(homedir, ".config"), "opencode")`)과
    같다 — 빈 문자열은 없는 것과 같다. `OPENCODE_CONFIG_DIR` 은 보지 않는다(opencode 도 전역 파일
    위치를 그것으로 바꾸지 않는다). 운영 호출자는 `(os.environ, Path.home())` 를 넘긴다."""
    configured = env.get("XDG_CONFIG_HOME")
    base = Path(configured) if configured else Path(home) / ".config"
    return base / OPENCODE_GLOBAL_CONFIG_SUBDIR


def is_harness_ctx_input_key(key: str) -> bool:
    """반영의 입력 키인가 — 비율 3개와 등록 하네스의 사용 한도(`harness.<harness>.ctx_window_tokens`)."""
    if key in _HARNESS_CTX_RATIO_KEYS:
        return True
    parts = key.split(".")
    return (len(parts) == 3 and parts[0] == "harness" and parts[1] in HARNESS_CTX_FILES
            and parts[2] == _HARNESS_CTX_WINDOW_SUFFIX)


def _harness_ctx_window_key(harness: str) -> str:
    return f"harness.{harness}.{_HARNESS_CTX_WINDOW_SUFFIX}"


def _harness_ctx_value(values, key: str) -> str:
    """conf 값 — 비었거나 없으면 레지스트리 기본값(conf 표기 그대로)."""
    raw = values.get(key)
    if raw is None or not str(raw).strip():
        return conf_schema.default_for(key)
    return str(raw).strip()


def _harness_ctx_integer(text: str) -> int | None:
    """정수 표기(부호 허용·ASCII 숫자)면 그 값, 아니면 None."""
    return int(text) if re.fullmatch(r"[+-]?[0-9]+", text) else None


def validate_harness_ctx(values, present: frozenset) -> None:
    """반영 전 검사 — 위반을 모두 모아 `HarnessCtxError` 한 번으로 올린다.

    `present` 는 설정 파일이 있는 하네스 이름 집합이다. 규칙:
      (1) 비율 3개는 정수이고 `0 < checkpoint_at < precompact_at < compact_at < 100`
      (2) 있는 하네스의 사용 한도는 양의 정수
    빈 키는 레지스트리 기본값이다. 설정 형식이 받는 값의 범위는 모델 한도를 적용한 뒤 본다."""
    violations: list[str] = []
    ratios: dict[str, int] = {}
    for key in _HARNESS_CTX_RATIO_KEYS:
        text = _harness_ctx_value(values, key)
        number = _harness_ctx_integer(text)
        if number is None:
            violations.append(f"`{key}={text}` 는 정수가 아닙니다.")
            continue
        ratios[key] = number
        if not 0 < number < 100:
            violations.append(f"`{key}={number}` 는 0 보다 크고 100 보다 작아야 합니다.")
    for low_key, high_key in zip(_HARNESS_CTX_RATIO_KEYS, _HARNESS_CTX_RATIO_KEYS[1:]):
        low, high = ratios.get(low_key), ratios.get(high_key)
        if low is not None and high is not None and not low < high:
            violations.append(
                f"`{low_key}={low}` 는 `{high_key}={high}` 보다 작아야 합니다"
                " (중간 기록 권고 < 압축 임박 경고 < 압축).")
    for harness in HARNESS_CTX_FILES:
        if harness not in present:
            continue
        key = _harness_ctx_window_key(harness)
        text = _harness_ctx_value(values, key)
        window = _harness_ctx_integer(text)
        if window is None or window <= 0:
            violations.append(f"`{key}={text}` 는 양의 정수여야 합니다.")
    if violations:
        raise HarnessCtxError(violations)


def harness_ctx_targets(opencode_models, entries) -> dict[str, dict[tuple[str, ...], int | bool]]:
    """설정 파일 relpath → {관리 키 경로 조각 튜플: 값} — 도구 기록 항목(`entries`)에서 만든다.

    opencode 는 기록 항목의 모델 행마다 `limit` 을 쓰고, `limit.output` 은 `opencode_models`(대상 모델
    `(provider, 모델 id) → limit.output`)에서 읽는다. 모델 id 는 문서 키 그대로 쓴다(점·슬래시·콜론에서
    가르지 않는다)."""
    targets: dict[str, dict[tuple[str, ...], int | bool]] = {}
    for harness, relpath in HARNESS_CTX_FILES.items():
        if harness not in entries:
            continue
        rule, entry = HARNESS_CTX_RULES[relpath], entries[harness]
        values: dict[tuple[str, ...], int | bool] = {}
        if rule.model_limits:
            for row in entry["models"]:
                provider, model = row["provider"], row["id"]
                limit = {"context": row["basis"]["context"], "input": entry["E"],
                         "output": opencode_models[(provider, model)]}
                for name in _MODEL_LIMIT_KEYS:
                    values[("provider", provider, "models", model, "limit", name)] = limit[name]
        for path, formula in rule.keys:
            values[path] = formula(entry)
        targets[relpath] = values
    return targets


def apply_harness_ctx(dest_root: Path, *, write: bool, opencode_config_dir: Path) -> tuple[str, ...]:
    """local.conf 의 사용 한도·압축 비율을 설정 파일이 있는 하네스에 반영한다 — 바뀐 줄 목록.

    순서: 검사(옛 키면 멈춤) → 설정 파일·opencode 전역 설정 읽기 → 도구별 모델 한도 조회·계산·설정 형식
    확인 → 새 내용 재판독 확인 → (`write`) 원자 쓰기 → 원장 → 압축 한도 기록. 앞 단계의 `HarnessCtxError`
    는 어떤 파일도 쓰지 않았다는 뜻이다. 반환 줄은 "<relpath> <경로> <옛 값> → <새 값>"(없던 키는 `(없음)`,
    점이 든 경로 조각은 큰따옴표: `provider.ollama.models."glm-5.2:cloud"…`).

    원장 규칙: 반영 직전 파일을 원장이 증언하면(`_baseline_attests_dest`) 그 항목의 `sha256` 만 새
    내용 해시로 바꾸고 `recorded_at`·`template_rev` 는 둔다 — 무편집 판정이 반영 뒤에도 유지된다.
    편집됨·원장 없음은 원장을 건드리지 않는다. 원장이 상위 schema 면 파일을 쓰지 않고 멈춘다.
    `harness.opencode.pro_model` 은 읽지 않는다 — 대상 모델은 설정 파일에 적힌 모델 전부와 선택 모델이되,
    공급자 연결 설정이 없는 모델(`_ctx_opencode_unconnected`)은 빼고 반영마다 표준오류로 알린다. 남은
    모델이 없으면 opencode 는 반영하지 않는다(설정 파일 그대로·기록에 opencode 항목 없음)."""
    dest_root = Path(dest_root)
    conf_path = dest_root / ".project_manager" / "local.conf"
    if not write:
        return _harness_ctx_plan(dest_root, _conf_values(conf_path, None), opencode_config_dir)[1]
    with fs.local_conf_write_lock(conf_path):
        values = _conf_values(conf_path, None)
        changed, lines, entries = _harness_ctx_plan(dest_root, values, opencode_config_dir)
        if changed:
            _harness_ctx_baseline_writable(dest_root)
        _harness_ctx_commit(dest_root, values, changed, entries)
        return lines


def _conf_values(conf_path: Path, updates) -> dict[str, str]:
    """local.conf 값에 `updates`(None·빈 값은 줄 삭제)를 덮은 제안 값 — 옛 키가 남았거나 들어오면 멈춘다.

    저장(`pm_update.apply_conf`)의 모든 반영 단계와 이 모듈의 반영이 같은 값 하나를 쓴다."""
    loaded = conf_store.load(conf_path)
    conf_schema.assert_no_legacy(loaded)
    values = {key: value for key, value in loaded.values.items() if key not in (updates or {})}
    if updates:
        values.update({key: value for key, value in updates.items() if value})
        conf_schema.assert_values_no_legacy(values, conf_path)
    return values


def _harness_ctx_commit(dest_root: Path, values, changed: dict[str, tuple[str, str]], entries) -> tuple[str, ...]:
    """계획(`_harness_ctx_plan`)대로 설정 파일과 압축 한도 기록을 쓴다 — 바꾼 파일 relpath.

    local.conf 쓰기 락 안에서 부른다(락을 다시 잡지 않는다). 쓰기 단계에서 멈추면 `HarnessCtxError.written`
    이 바꾼 파일을 적고 `write_stage` 가 참이다(바꾼 파일이 없어도 검사 거부가 아니다)."""
    record = {"schema": conf_schema.CTX_BUDGET_SCHEMA, "inputs": conf_schema.ctx_input_values(values),
              "applied_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
              "harnesses": entries}
    payload = (json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    record_relpath = conf_store.CTX_BUDGET_RELPATH.as_posix()
    try:
        if changed:
            _harness_ctx_write(dest_root, changed)
        if _ctx_record_unchanged(dest_root / conf_store.CTX_BUDGET_RELPATH, record):
            return tuple(changed)
        if not adapter_baseline._atomic_write_dest_bytes(dest_root, conf_store.CTX_BUDGET_RELPATH, payload):
            raise HarnessCtxError([f"압축 한도 기록({record_relpath})을 쓰지 못했습니다."], written=tuple(changed))
    except HarnessCtxError as exc:
        failure = HarnessCtxError(exc.violations, written=exc.written)
        failure.write_stage = True
        raise failure from exc
    return (*changed, record_relpath)


def _harness_ctx_plan(dest_root: Path, values, opencode_config_dir: Path):
    """제안 값(`_conf_values`) 검사·조회·계산 — (바뀔 설정 파일, 바뀐 줄, 기록 항목). 아무것도 쓰지 않는다."""
    present = frozenset(harness for harness, relpath in HARNESS_CTX_FILES.items()
                        if (dest_root / relpath).is_file())
    validate_harness_ctx(values, present)

    texts = {relpath: _harness_ctx_read_text(dest_root / relpath, relpath)
             for harness, relpath in HARNESS_CTX_FILES.items() if harness in present}
    documents = {relpath: _harness_ctx_parse(relpath, text) for relpath, text in texts.items()}
    sources: dict[str, list[dict]] = {}
    models: dict[tuple[str, str], int] = {}
    for harness, relpath in HARNESS_CTX_FILES.items():
        if harness not in present:
            continue
        sources[harness] = [documents[relpath]]
        if HARNESS_CTX_RULES[relpath].model_limits:
            global_documents = _opencode_global_documents(opencode_config_dir)
            models = _opencode_target_models(documents[relpath], relpath, global_documents)
            sources[harness] = [document for _label, document in global_documents] + [documents[relpath]]
    entries: dict[str, dict] = {}
    for harness, relpath in HARNESS_CTX_FILES.items():
        if harness not in present:
            continue
        rows = _read_harness_ctx_metadata(dest_root, harness, sources[harness], models)
        if HARNESS_CTX_RULES[relpath].model_limits and not _opencode_rows_left(models, rows):
            continue
        entries[harness] = _harness_ctx_entry(dest_root, harness, values, rows, models)
    targets = harness_ctx_targets(models, entries)
    lines: list[str] = []
    changed: dict[str, tuple[str, str]] = {}
    for relpath, text in texts.items():
        if relpath not in targets:  # 반영하지 않는 도구의 설정 파일은 그대로 둔다
            continue
        new_text, file_lines = _harness_ctx_edit(relpath, text, targets[relpath])
        lines.extend(file_lines)
        if new_text != text:
            changed[relpath] = (text, new_text)
    for harness, entry in entries.items():
        entry["settings"] = {_harness_ctx_dotted(path): value
                             for path, value in targets[HARNESS_CTX_FILES[harness]].items()}
    return changed, tuple(lines), entries


def _ctx_record_unchanged(path: Path, record: dict) -> bool:
    """기존 기록이 적용 시각 밖에서 같은가(같은 재적용은 트리를 바꾸지 않는다) — 없거나 손상이면 새로 쓴다."""
    try:
        previous = json.loads(fs.read_bytes_shared(path).decode("utf-8"))
    except (FileNotFoundError, ValueError):
        return False
    return isinstance(previous, dict) and {**previous, "applied_at": None} == {**record, "applied_at": None}


def _ctx_basis_text(model: dict) -> str:
    """모델 가용 한도의 출처 한 줄 (관리자 표시용) — 읽기가 확인한 출처 형식(`conf_schema._CTX_BASIS_FORMS`)으로 고른다."""
    basis = model["basis"]
    if "auto_compact_threshold" in basis:
        return (f"Claude Code 조회 · 모델 창 {basis['model_window']:,} − 압축 예약 "
                f"{basis['model_window'] - basis['auto_compact_threshold']:,}")
    if "percent" in basis:
        window = basis["model_context_window"] or basis["context_window"]
        maximum = f" · 최대 {basis['max_context_window']:,}" if basis["max_context_window"] else ""
        return f"Codex 모델 목록 · 창 {window:,}{maximum} × {basis['percent']}%"
    limit = f"입력 한도 {basis['input']:,} · " if basis.get("input") else ""
    return (f"{conf_schema._ctx_model_name(model)} · {limit}창 {basis['context']:,} − 출력 "
            f"{basis['output']:,} ({basis['source']})")


def ctx_budget_status(dest_root, values) -> dict:
    """상태 조회·관리자 표시 — 도구마다 `{"status": "ok", …}` 또는 `{"status": "error", "reason"}`.

    ok 항목의 `settings_mismatch` 는 기록 `settings` 중 설정 파일 값이 다른 경로의 `{"path", "file",
    "record"}` 목록이다(키가 없으면 `file` 은 None, 모두 같으면 빈 목록 · 순서는 기록 순서). 설정 파일은
    반영과 같은 판독으로 읽고 같음 규칙도 반영 재판독과 같다(`_harness_ctx_same`). 모델 한도를 조회하지
    않고 파일을 쓰지 않는다. 기록 오류는 설정 파일을 읽지 않은 error, 설정 파일을 읽거나 풀지 못하면
    그 도구만 error(사유는 그 파일과 원인)다."""

    result = {}
    for harness in conf_schema.CTX_HARNESSES:
        relpath = HARNESS_CTX_FILES[harness]
        try:
            record, entry = conf_store._ctx_record_entry(dest_root, values, harness)
        except conf_schema.CtxBudgetError as exc:
            result[harness] = {"status": "error", "reason": str(exc)}
            continue
        try:
            document = _harness_ctx_parse(
                relpath, _harness_ctx_read_text(Path(dest_root) / relpath, relpath))
        except HarnessCtxError as exc:
            result[harness] = {"status": "error", "reason": " ".join(exc.violations)}
            continue
        mismatch = []
        for dotted, value in entry["settings"].items():
            present, actual = _document_key_value_path(document, _harness_ctx_path(dotted))
            if not _harness_ctx_same(present, actual, value):
                mismatch.append({"path": dotted, "file": actual if present else None, "record": value})
        smallest = min(entry["models"], key=lambda model: model["C"])
        result[harness] = {"status": "ok", **conf_schema._ctx_budget_view(record, entry, harness, None),
                           "C": entry["C"], "basis_text": _ctx_basis_text(smallest),
                           "settings_mismatch": mismatch}
    return result


def _opencode_rows_left(models: dict, rows: list) -> bool:
    """opencode 조회 행이 남았는가 — 대상 중 행이 없는 모델을 표준오류 한 줄로 알리고, 남은 행이 없으면
    반영하지 않는다는 줄을 더한다. 행의 형식은 `_harness_ctx_entry` 가 본다.

    모델 목록 캐시 파일이 없으면 공급자 연결 여부를 알 수 없어 models.dev 공급자 모델도 빠진다 — 그때는
    원인을 공급자 연결 설정 없음 대신 캐시 부재와 그 경로로 적고, 미반영 줄에 opencode 를 한 번 실행해
    목록을 받으라는 처방을 더한다."""
    rerun = conf_schema._CTX_RERUN
    catalog_path = _opencode_model_catalog_path()
    catalog_missing = not catalog_path.is_file()
    listed = {(row.get("provider"), row.get("id")) for row in rows if isinstance(row, dict)}
    excluded = [f"{provider}/{model}" for provider, model in models if (provider, model) not in listed]
    if excluded:
        cause = f"모델 목록 캐시 없음: {catalog_path}" if catalog_missing else "공급자 연결 설정 없음"
        print(f"opencode 사용 한도 대상에서 뺀 모델({cause}): {', '.join(excluded)}", file=sys.stderr)
    if rows:
        return True
    catalog_hint = (f" 모델 목록 캐시가 없으니 opencode 를 한 번 실행해 모델 목록을 받은 뒤 {rerun}."
                    if catalog_missing else "")
    print(f"opencode 사용 한도를 반영하지 않았습니다 (쓸 수 있는 모델 없음). 공급자 연결 설정을 넣은 뒤 "
          f"{rerun}.{catalog_hint}", file=sys.stderr)
    return False


def _harness_ctx_entry(dest_root: Path, harness: str, values, found: list, models: dict) -> dict:
    """도구 기록 항목 — 모델 행·W·C·E·P·경고 지점. `found` 는 조회 행(`_read_harness_ctx_metadata`)이다.
    조회 행이 대상과 맞지 않거나(opencode 는 중복 없는 대상 모델의 부분집합, 그 밖은 정확히 1행) 설정
    형식이 받지 않는 값이면 `HarnessCtxError`."""
    rule = HARNESS_CTX_RULES[HARNESS_CTX_FILES[harness]]
    positive = conf_schema._ctx_positive
    if any(not isinstance(row, dict) or not isinstance(row.get("id"), str) or not isinstance(row.get("provider"), str)
           or not positive(row.get("C")) or not isinstance(row.get("basis"), dict)
           or (rule.model_limits and not positive(row["basis"].get("context"))) for row in found):
        raise HarnessCtxError([f"{rule.label} 모델 한도 조회 결과의 형식이 맞지 않습니다."])
    rows = [{key: row[key] for key in ("id", "provider", "C", "basis")} for row in found]
    identities = [(row["provider"], row["id"]) for row in rows]
    mismatched = (len(set(identities)) != len(rows) or not set(identities) <= set(models)) if rule.model_limits \
        else len(rows) != 1
    if mismatched:
        raise HarnessCtxError([f"{rule.label} 모델 한도 조회 결과가 대상 모델과 맞지 않습니다."])
    window = int(_harness_ctx_value(values, _harness_ctx_window_key(harness)))
    checkpoint_at, precompact_at, compact_at = (int(_harness_ctx_value(values, key)) for key in _HARNESS_CTX_RATIO_KEYS)
    capacity = min(row["C"] for row in rows)
    effective = min(window, capacity)
    entry = {"models": rows, "W": window, "C": capacity, "E": effective, "P": effective * compact_at // 100,
             "checkpoint": effective * checkpoint_at // 100, "precompact": effective * precompact_at // 100}
    rule.check(dest_root, entry)
    return entry


def _read_harness_ctx_metadata(dest_root, harness, documents, models):
    """도구의 모델 한도 조회 — 모델 행 `[{id, provider, C, basis}]` (테스트 교체 지점). `documents` 는 그 도구
    설정 문서(병합 순서), `models` 는 opencode 대상 모델이다. 조회하지 못하면 `HarnessCtxError`."""
    rule = HARNESS_CTX_RULES[HARNESS_CTX_FILES[harness]]
    try:
        return rule.capacity_reader(Path(dest_root), documents, models)
    except (OSError, ValueError) as exc:
        raise HarnessCtxError([f"{rule.label} 모델 한도를 확인하지 못했습니다 ({exc})."]) from exc


def _ctx_native_protocol(command, root, requests):
    """도구의 JSON 줄 제어 프로토콜로 (이름, 요청, 응답 뒤 알림 또는 None) 을 차례로 보내고 `{이름: 응답}` 을
    낸다 (테스트 교체 지점). 모델 답변 요청은 보내지 않으며 답변 흐름이 보이면 멈춘다."""
    import queue
    import threading
    import time
    executable = contract.resolve_executable(command[0], None)  # 없으면 FileNotFoundError(PATH 부재)
    incoming: queue.Queue = queue.Queue()
    proc = subprocess.Popen([executable, *command[1:]], cwd=root, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                            encoding="utf-8", bufsize=1)

    def receive():
        for line in proc.stdout:
            try:
                incoming.put(json.loads(line))
            except ValueError:
                incoming.put(None)
    worker = threading.Thread(target=receive, daemon=True)
    worker.start()
    responses = {}
    deadline = time.monotonic() + _CTX_PROTOCOL_TIMEOUT_SECONDS
    try:
        for name, message, notification in requests:
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
            expected = message.get("request_id", message.get("id"))
            while name not in responses:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError(f"{command[0]} 응답 시간 초과")
                try:
                    frame = incoming.get(timeout=min(remaining, 0.25))
                except queue.Empty:
                    if proc.poll() is not None:
                        raise ValueError(f"{command[0]} 가 응답 전에 종료됨") from None
                    continue
                if not isinstance(frame, dict):
                    raise ValueError(f"{command[0]} 응답 형식 손상")
                if frame.get("type") in ("user", "assistant", "result"):
                    raise ValueError(f"{command[0]} 조회에서 모델 답변이 시작됨")
                control = frame.get("response") if frame.get("type") == "control_response" else None
                identifier = control.get("request_id") if isinstance(control, dict) else frame.get("id")
                if identifier != expected:
                    continue
                if "error" in frame or (control is not None and control.get("subtype") != "success"):
                    raise ValueError(f"{command[0]} 가 {name} 요청을 거부함")
                response = control.get("response") if control is not None else frame.get("result")
                if not isinstance(response, dict):
                    raise ValueError(f"{command[0]} {name} 응답 형식 손상")
                responses[name] = response
            if notification:
                proc.stdin.write(json.dumps(notification) + "\n")
                proc.stdin.flush()
        return responses
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        worker.join(timeout=1)


# Claude Code 무턴 조회 — 설정 파일 세 층만 읽고 MCP·도구를 비운 채 제어 요청만 보낸다.
_CLAUDE_CONTEXT_QUERY = (
    "claude", "-p", "--verbose", "--input-format", "stream-json", "--output-format", "stream-json",
    "--no-session-persistence", "--safe-mode", "--setting-sources", "user,project,local",
    "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--permission-mode", "plan", "--tools", "")


def _ctx_claude_query(root, autocompact) -> dict:
    """`--autocompact <autocompact>` 로 연 Claude Code 의 initialize·get_context_usage 응답."""
    responses = _ctx_native_protocol([*_CLAUDE_CONTEXT_QUERY, "--autocompact", str(autocompact)], root, [
        ("initialize", {"type": "control_request", "request_id": "ctx-init",
                        "request": {"subtype": "initialize", "hooks": {}, "skills": []}}, None),
        ("context", {"type": "control_request", "request_id": "ctx-context",
                     "request": {"subtype": "get_context_usage", "detail": "summary"}}, None)])
    context, positive = responses["context"], conf_schema._ctx_positive
    if (not positive(context.get("rawMaxTokens")) or not positive(context.get("autoCompactThreshold"))
            or not context["autoCompactThreshold"] < context["rawMaxTokens"]):
        raise ValueError("Claude Code 조회 응답에 모델 창·압축 기준이 없습니다")
    return responses


def _ctx_claude_capacity(root, documents, models) -> list:
    """Claude C = 기본 창 조회의 압축 기준(autoCompactThreshold) — 모델 창에서 압축 예약 d 를 뺀 값."""
    responses = _ctx_claude_query(root, "auto")
    context, account = responses["context"], responses["initialize"].get("account")
    model = context.get("model")
    provider = account.get("apiProvider") if isinstance(account, dict) else None
    if not isinstance(model, str) or not model or not isinstance(provider, str) or not provider:
        raise ValueError("Claude Code 조회 응답에 모델·provider 가 없습니다")
    return [{"id": model, "provider": provider, "C": context["autoCompactThreshold"],
             "basis": {"model_window": context["rawMaxTokens"],
                       "auto_compact_threshold": context["autoCompactThreshold"]}}]


def _ctx_claude_threshold(root, window: int) -> int:
    """`autoCompactWindow = window` 일 때 Claude Code 의 압축 기준 — 확인 조회 (테스트 교체 지점)."""
    return _ctx_claude_query(root, window)["context"]["autoCompactThreshold"]


def _ctx_claude_window(entry: dict) -> int:
    """autoCompactWindow = P + d — d 는 기본 창 조회의 모델 창 − 압축 기준(창 크기와 무관·실측 33,000)."""
    basis = entry["models"][0]["basis"]
    return entry["P"] + basis["model_window"] - basis["auto_compact_threshold"]


def _ctx_claude_check(root, entry: dict) -> None:
    """autoCompactWindow 범위와 확인 조회(그 창의 압축 기준 = P)."""
    window = _ctx_claude_window(entry)
    low, high = CLAUDE_AUTO_COMPACT_WINDOW_RANGE
    if not low <= window <= high:
        raise HarnessCtxError([
            f"Claude Code autoCompactWindow {window}(압축 지점 {entry['P']} + 압축 예약 {window - entry['P']})은 "
            f"{low}~{high} 이어야 합니다 — 사용 한도나 압축 비율을 조정하십시오."])
    try:
        threshold = _ctx_claude_threshold(root, window)
    except (OSError, ValueError) as exc:
        raise HarnessCtxError([f"Claude Code 확인 조회 실패 ({exc})."]) from exc
    if threshold != entry["P"]:
        raise HarnessCtxError([
            f"Claude Code 확인 조회: autoCompactWindow {window} 의 압축 기준이 {threshold} 로 압축 지점 "
            f"{entry['P']} 와 다릅니다 (CLAUDE_AUTOCOMPACT_PCT_OVERRIDE 같은 환경 설정을 확인하십시오)."])


def _ctx_codex_window(basis: dict) -> int:
    """Codex 실효 창 — 설정 model_context_window(없으면 모델 창)를 모델 최대 창으로 자른 값."""
    window = basis["model_context_window"] or basis["context_window"]
    return window if basis["max_context_window"] is None else min(window, basis["max_context_window"])


# Codex 앱 서버(무턴 조회)와 opencode 모델 목록 캐시(`<캐시 기준 폴더>/opencode/models.json`).
_CODEX_APP_SERVER_QUERY = ("codex", "app-server", "--stdio")


_OPENCODE_MODEL_CATALOG = ("opencode", "models.json")


def _opencode_model_catalog_path() -> Path:
    """opencode 모델 목록 캐시 파일 — 조회(`_ctx_opencode_capacity`)와 제외 알림이 같은 경로를 본다."""
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache", *_OPENCODE_MODEL_CATALOG)


def _ctx_codex_capacity(root, documents, models) -> list:
    """Codex C = floor(실효 창 × 유효 비율 ÷ 100) — 앱 서버의 실효 설정과 Codex 모델 캐시의 그 모델 행."""
    responses = _ctx_native_protocol(_CODEX_APP_SERVER_QUERY, root, [
        ("initialize", {"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "pm-ctx", "version": "1"}, "capabilities": {"experimentalApi": True}}},
         {"method": "initialized"}),
        ("config", {"id": 2, "method": "config/read", "params": {"includeLayers": True, "cwd": str(root)}}, None),
        # 모델 목록 요청이 Codex 에게 지금 인증으로 모델 캐시를 받게 한다(창 크기는 캐시 파일에만 있다).
        ("models", {"id": 3, "method": "model/list", "params": {"includeHidden": True, "limit": 100}}, None)])
    config = responses["config"].get("config")
    model = config.get("model") if isinstance(config, dict) else None
    if not isinstance(model, str) or not model:
        raise ValueError("Codex 실효 설정에 모델이 없습니다")
    cache = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "models_cache.json"
    document = json.loads(fs.read_bytes_shared(cache).decode("utf-8"))
    listed = document.get("models") if isinstance(document, dict) else None
    rows = [row for row in listed if isinstance(row, dict) and row.get("slug") == model] if isinstance(listed, list) else []
    if len(rows) != 1:
        raise ValueError(f"Codex 모델 캐시({cache})에 {model} 행이 없거나 둘 이상입니다")
    basis = {"context_window": rows[0].get("context_window"), "max_context_window": rows[0].get("max_context_window"),
             "model_context_window": config.get("model_context_window"),
             "percent": rows[0].get("effective_context_window_percent")}
    positive = conf_schema._ctx_positive
    if (not positive(basis["context_window"]) or not positive(basis["percent"]) or basis["percent"] > 100
            or any(basis[key] is not None and not positive(basis[key])
                   for key in ("max_context_window", "model_context_window"))):
        raise ValueError(f"Codex 모델 캐시·설정의 {model} 창 크기 형식 오류")
    # model_provider 가 없으면 Codex 는 openai 를 쓴다(기록의 표시용 이름).
    return [{"id": model, "provider": config.get("model_provider") or "openai",
             "C": _ctx_codex_window(basis) * basis["percent"] // 100, "basis": basis}]


def _ctx_codex_check(root, entry: dict) -> None:
    """Codex 압축 지점은 실효 창의 `CODEX_COMPACT_LIMIT_PCT`% 이하."""
    limit = _ctx_codex_window(entry["models"][0]["basis"]) * CODEX_COMPACT_LIMIT_PCT // 100
    if entry["P"] > limit:
        raise HarnessCtxError([
            f"Codex 압축 지점 {entry['P']} 은 실효 창의 {CODEX_COMPACT_LIMIT_PCT}%({limit}) 이하여야 합니다 — "
            "압축 비율을 낮추십시오."])


def _ctx_opencode_output_max() -> int:
    """opencode 출력 예약 상한 — `OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX` 가 있으면 그 값."""
    configured = os.environ.get("OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX")
    if not configured:
        return OPENCODE_LIMIT_OUTPUT_DEFAULT
    if not re.fullmatch(r"[0-9]+", configured) or int(configured) <= 0:
        raise ValueError(f"OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX={configured} 는 양의 정수가 아닙니다")
    return int(configured)


def _ctx_opencode_route(documents: list, provider: str, model: str) -> tuple:
    """설정 문서(병합 순서·뒤가 이긴다)의 그 모델 경로 — (baseURL, npm, API 모델 id)."""
    origin, npm, api_model = None, None, model
    for document in documents:
        configured = (document.get("provider") or {}).get(provider) or {}
        origin = (configured.get("options") or {}).get("baseURL", origin)
        npm = configured.get("npm", npm)
        api_model = ((configured.get("models") or {}).get(model) or {}).get("id", api_model)
    return origin, npm, api_model


def _ctx_opencode_unconnected(documents: list, catalog: dict, provider: str, model: str) -> bool:
    """공급자 연결 설정이 없는 모델인가 — 셋 다 맞으면 참이다.

      (1) 모델 목록에 그 공급자 항목이 없다.
      (2) 설정 문서(병합 순서) 어디에도 공급자 `options.baseURL`(빈 문자열 제외)·공급자 `api`·모델
          `provider.api` 가 없다.
      (3) 패키지(모델 `provider.npm`, 없으면 공급자 `npm` · 뒤 문서가 이긴다)가 없거나
          `OPENCODE_DEFAULT_PROVIDER_NPM` 이다.
    opencode 1.18.34 는 이런 모델의 요청 주소를 빈 문자열로 두어 그 모델로 요청을 보내지 못한다."""
    if provider in catalog:
        return False
    provider_npm = model_npm = None
    for document in documents:
        configured = (document.get("provider") or {}).get(provider) or {}
        model_route = (((configured.get("models") or {}).get(model) or {}).get("provider") or {})
        if ((configured.get("options") or {}).get("baseURL") not in (None, "")
                or configured.get("api") is not None or model_route.get("api") is not None):
            return False
        provider_npm = configured.get("npm", provider_npm)
        model_npm = model_route.get("npm", model_npm)
    return (model_npm if model_npm is not None else provider_npm) in (None, OPENCODE_DEFAULT_PROVIDER_NPM)


def _ctx_opencode_capacity(root, documents, models) -> list:
    """opencode 대상 모델마다 C = min(원본 input, context − 실효 출력) — models.dev 캐시 행이 없으면
    OpenAI 호환(Ollama) 주소의 모델 정보. 공급자 연결 설정이 없는 모델(`_ctx_opencode_unconnected`)은
    조회하지 않고 행을 내지 않는다."""
    output_max, positive = _ctx_opencode_output_max(), conf_schema._ctx_positive
    catalog_path = _opencode_model_catalog_path()
    catalog = json.loads(fs.read_bytes_shared(catalog_path).decode("utf-8")) if catalog_path.is_file() else {}
    if not isinstance(catalog, dict):
        raise ValueError(f"opencode 모델 목록({catalog_path}) 형식 오류")
    rows = []
    for (provider, model), output in models.items():
        if _ctx_opencode_unconnected(documents, catalog, provider, model):
            continue
        origin, npm, api_model = _ctx_opencode_route(documents, provider, model)
        provider_row = catalog.get(provider) or {}
        listed = provider_row.get("models") if isinstance(provider_row, dict) else None
        listed = listed.get(model) if isinstance(listed, dict) else None
        if isinstance(listed, dict):
            if api_model != model or origin not in (None, provider_row.get("api")):
                raise ValueError(f"{provider}/{model} 의 설정 주소·모델 id 가 모델 목록과 다릅니다")
            limit = listed.get("limit") or {}
            context, input_limit = limit.get("context"), limit.get("input")
            if (not positive(context) or not positive(limit.get("output"))
                    or (input_limit is not None and not positive(input_limit))):
                raise ValueError(f"opencode 모델 목록의 {provider}/{model} limit 형식 오류")
            effective, source = min(limit["output"], output, output_max), "models.dev 목록"
        else:
            if npm != OPENCODE_DEFAULT_PROVIDER_NPM or not isinstance(origin, str) or not isinstance(api_model, str):
                raise ValueError(f"{provider}/{model} 이 모델 목록에 없고 OpenAI 호환 주소도 아닙니다")
            info = _ctx_ollama_show(origin, api_model).get("model_info")
            architecture = info.get("general.architecture") if isinstance(info, dict) else None
            context = info.get(f"{architecture}.context_length") if isinstance(architecture, str) else None
            if not positive(context):
                raise ValueError(f"{origin} 의 {api_model} 모델 정보에 context 길이가 없습니다")
            input_limit, effective, source = None, min(output, output_max), origin
        capacity = context - effective if input_limit is None else min(input_limit, context - effective)
        if capacity <= 0:
            raise ValueError(f"{provider}/{model} 의 출력 예약 {effective} 가 창 {context} 이상입니다")
        basis = {"context": context, "output": effective, "source": source}
        if input_limit is not None:
            basis["input"] = input_limit
        rows.append({"id": model, "provider": provider, "C": capacity, "basis": basis})
    return rows


def _ctx_ollama_show(origin, model):
    """Ollama `/api/show` 모델 정보 — OpenAI 호환 주소(`…/v1`)의 같은 서버에 묻는다(생성 요청 없음)."""
    from urllib import request, error
    from urllib.parse import urlsplit, urlunsplit
    address = urlsplit(origin)
    prefix = address.path.rstrip("/")
    if (address.scheme not in ("http", "https") or not address.hostname or address.username or address.password
            or address.query or address.fragment or not prefix.endswith("/v1")):
        raise ValueError(f"Ollama 주소 형식 미지원 ({origin})")
    endpoint = urlunsplit((address.scheme, address.netloc, prefix[:-3] + "/api/show", "", ""))
    payload = json.dumps({"model": model, "verbose": False}).encode("utf-8")
    message = request.Request(endpoint, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with request.urlopen(message, timeout=15) as response:
            raw = response.read(1024 * 1024 + 1)
            if response.status != 200 or len(raw) > 1024 * 1024:
                raise ValueError(f"Ollama 모델 정보 응답 거부/크기 초과 ({endpoint})")
    except error.URLError as exc:  # HTTPError 포함
        raise ValueError(f"Ollama 모델 정보 조회 실패 ({endpoint} {model}: {exc})") from None
    body = json.loads(raw.decode("utf-8"))
    if not isinstance(body, dict):
        raise ValueError(f"Ollama 모델 정보 형식 오류 ({endpoint})")
    return body


def _harness_ctx_read_text(path: Path, label: str) -> str:
    """설정 파일 텍스트(개행 표기 보존) — 못 읽으면 `HarnessCtxError`(보고 채널의 빈 문서 관용 없음)."""
    try:
        raw = fs.read_bytes_shared(path)
    except OSError as exc:
        raise HarnessCtxError([f"{label} 를 읽지 못했습니다 ({exc})."]) from exc
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HarnessCtxError([f"{label} 가 UTF-8 이 아닙니다 ({exc})."]) from exc


def _harness_ctx_parse(label: str, text: str) -> dict:
    """설정 텍스트 → 문서. 형식은 확장자가 정한다 — 풀지 못하거나 최상위가 표/객체가 아니면 멈춘다."""
    try:
        if label.endswith(".toml"):
            document = tomllib.loads(text)
        else:
            document = json.loads(
                jsonc._strip_jsonc_trailing_commas(jsonc._mask_jsonc_comments(text)))
    except ValueError as exc:
        raise HarnessCtxError([f"{label} 를 풀지 못했습니다 ({exc})."]) from exc
    if not isinstance(document, dict):
        raise HarnessCtxError([f"{label} 의 최상위가 객체가 아닙니다."])
    return document


def _harness_ctx_check_objects(label: str, document: dict) -> None:
    """opencode 문서에서 반영이 들어가 쓰거나 읽는 객체 자리가 객체인가 — `compaction`·`provider`·
    provider 항목·`models`·모델 항목·`limit`. 아니면 파일·키 경로를 지목해 멈춘다(그 값을 덮어써서
    지우지 않는다)."""
    def need_object(value, path: str) -> None:
        if not isinstance(value, dict):
            raise HarnessCtxError([f"{label} 의 `{path}` 가 객체가 아닙니다."])

    if "compaction" in document:
        need_object(document["compaction"], "compaction")
    if "provider" not in document:
        return
    need_object(document["provider"], "provider")
    for provider, entry in document["provider"].items():
        need_object(entry, f"provider.{provider}")
        if "models" not in entry:
            continue
        need_object(entry["models"], f"provider.{provider}.models")
        for model, model_entry in entry["models"].items():
            need_object(model_entry, f"provider.{provider}.models.{model}")
            if "limit" in model_entry:
                need_object(model_entry["limit"], f"provider.{provider}.models.{model}.limit")


def _opencode_global_documents(config_dir: Path) -> list[tuple[str, dict]]:
    """opencode 전역 설정 파일 중 있는 것을 병합 순서대로 — (경로 표기, 문서). 읽기만 한다."""
    documents: list[tuple[str, dict]] = []
    for name in OPENCODE_GLOBAL_CONFIG_NAMES:
        path = Path(config_dir) / name
        try:
            raw = fs.read_bytes_shared(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise HarnessCtxError(
                [f"opencode 전역 설정 {path} 를 읽지 못했습니다 ({exc})."]) from exc
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HarnessCtxError([f"opencode 전역 설정 {path} 가 UTF-8 이 아닙니다 ({exc})."]) from exc
        documents.append((str(path), _harness_ctx_parse(str(path), text)))
    return documents


def _opencode_target_models(project_document: dict, project_label: str,
                            global_documents: list) -> dict[tuple[str, str], int]:
    """opencode 대상 모델 `(provider, 모델 id) → limit.output` — 프로젝트 파일과 전역 설정의 합집합.

    순서는 프로젝트 파일 등장 순, 그다음 전역 파일을 병합 순서대로 본 등장 순이다. `limit.output` 은
    프로젝트 값, 없으면 전역 값(병합 순서상 뒤 파일이 이긴다), 둘 다 없으면 32000 이다. 가져다 쓸
    값이 정수가 아니면(bool 포함) 그 파일·키 경로를 지목해 멈춘다."""
    order: list[tuple[str, str]] = []
    project_outputs: dict[tuple[str, str], tuple[str, object]] = {}
    global_outputs: dict[tuple[str, str], tuple[str, object]] = {}
    sources = [(project_label, project_document, project_outputs),
               *((label, document, global_outputs) for label, document in global_documents)]
    for label, document, outputs in sources:
        _harness_ctx_check_objects(label, document)
        for provider, entry in (document.get("provider") or {}).items():
            for model, model_entry in (entry.get("models") or {}).items():
                if (provider, model) not in order:
                    order.append((provider, model))
                limit = model_entry.get("limit") or {}
                if "output" in limit:
                    outputs[(provider, model)] = (label, limit["output"])
    selected = None
    for _label, document, _outputs in [*sources[1:], sources[0]]:  # 프로젝트 파일이 전역 설정을 이긴다
        selected = document.get("model", selected)
    if selected is not None:
        if not isinstance(selected, str):
            raise HarnessCtxError(["opencode 실제 선택 모델 형식 불일치"])
        provider, separator, model = selected.partition("/")
        if not separator or not provider or not model:
            raise HarnessCtxError(["opencode 실제 선택 provider/model 확인 불가"])
        if (provider, model) not in order:
            order.append((provider, model))
    models: dict[tuple[str, str], int] = {}
    for provider, model in order:
        found = project_outputs.get((provider, model)) or global_outputs.get((provider, model))
        if found is None:
            models[(provider, model)] = OPENCODE_LIMIT_OUTPUT_DEFAULT
            continue
        label, value = found
        if isinstance(value, bool) or not isinstance(value, int):
            raise HarnessCtxError([
                f"{label} 의 `provider.{provider}.models.{_harness_ctx_path_segment(model)}"
                f".limit.output` 값 {value!r} 는 정수가 아닙니다."])
        models[(provider, model)] = value
    return models


def _harness_ctx_path_segment(segment: str) -> str:
    """경로 표기 조각 — 점이 든 조각은 큰따옴표로 감싼다(`"glm-5.2:cloud"`)."""
    return f'"{segment}"' if "." in segment else segment


def _harness_ctx_dotted(path: tuple[str, ...]) -> str:
    return ".".join(_harness_ctx_path_segment(segment) for segment in path)


def _harness_ctx_path(dotted: str) -> tuple[str, ...]:
    """`_harness_ctx_dotted` 의 역표기 — 큰따옴표로 감싼 조각은 점에서 가르지 않는다.
    큰따옴표가 든 조각은 표기 규칙이 구분하지 못해 되돌리지 못한다(모델 id 에 큰따옴표가 없다는 전제)."""
    return tuple(quoted or bare for quoted, bare in re.findall(r'"([^"]*)"|([^.]+)', dotted))


def _harness_ctx_edit(relpath: str, text: str, entries: dict[tuple[str, ...], int | bool]) -> tuple[str, list[str]]:
    """관리 키를 목표값으로 고친 텍스트와 바뀐 줄 — 결과를 다시 읽어 값을 확인한다."""
    lines: list[str] = []
    for path, value in entries.items():
        try:
            if relpath.endswith(".toml"):
                text, old = _toml_set_top_level_integer(text, path[0], value)
            else:
                text, old = jsonc._jsonc_set_value(text, path, value)
        except ValueError as exc:
            raise HarnessCtxError([f"{relpath}: {exc}"]) from exc
        literal = json.dumps(value)  # 문서에 쓰는 리터럴(정수는 str 과 같고 bool 은 false/true)
        if old != literal:
            lines.append(f"{relpath} {_harness_ctx_dotted(path)} "
                         f"{'(없음)' if old is None else old} → {literal}")
    document = _harness_ctx_parse(relpath, text)
    for path, value in entries.items():
        present, actual = _document_key_value_path(document, path)
        if not _harness_ctx_same(present, actual, value):
            raise HarnessCtxError([
                f"{relpath}: 반영 결과를 다시 읽었더니 `{_harness_ctx_dotted(path)}` 가 {json.dumps(value)} 가 "
                "아닙니다 — 파일을 쓰지 않았습니다."])
    return text, lines


def _harness_ctx_same(present: bool, actual: object, value: int | bool) -> bool:
    """설정 문서에서 찾은 값이 반영 목표와 같은가 — 정수 목표는 bool 이 아닌 같은 정수만, bool 목표는
    같은 bool 만 같다(bool 은 정수로 보지 않는다). 반영의 재판독(`_harness_ctx_edit`)과 상태 비교
    (`ctx_budget_status`)가 함께 쓴다."""
    return present and isinstance(actual, bool) == isinstance(value, bool) and actual == value


def _document_key_value_path(document: dict, path: tuple[str, ...]) -> tuple[bool, object]:
    """조각 튜플 경로로 문서 안쪽 값을 찾는다 — (있음, 값). 조각을 점에서 가르지 않는다."""
    node: object = document
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def _toml_first_table_header(text: str) -> "re.Match[str] | None":
    """첫 실제 표 머리 줄 — 여러 줄 문자열·배열 안의 `[이름]` 꼴 줄은 건너뛴다.

    머리 꼴 줄 앞까지의 텍스트가 TOML 로 풀리지 않으면 그 줄은 아직 열린 값(여러 줄 문자열·배열)
    안에 있다. 앞부분이 풀리는 첫 후보가 최상위 구간의 끝이다."""
    for match in _TOML_TABLE_HEADER_RE.finditer(text):
        try:
            tomllib.loads(text[:match.start()])
        except tomllib.TOMLDecodeError:
            continue
        return match
    return None


def _toml_set_top_level_integer(text: str, key: str, value: int) -> tuple[str, str | None]:
    """TOML 최상위 키 줄의 값 자리만 정수로 바꾼다 — 없으면 첫 `[표]` 머리 줄 앞에 한 줄 넣는다.

    반환 (새 텍스트, 옛 값 리터럴 또는 None). 최상위 구간은 첫 표 머리 줄 전까지다(그 뒤의 같은
    이름은 표 안 키다). 넣는 줄은 머리 줄 바로 위의 빈 줄들 앞에 둔다. 결과의 정합은 호출부가
    `tomllib` 로 다시 읽어 확인한다."""
    header = _toml_first_table_header(text)
    region_end = header.start() if header else len(text)
    key_re = re.compile(
        rf'^[ \t]*(?:{re.escape(key)}|"{re.escape(key)}")[ \t]*=[ \t]*(?P<value>[^\s#]+)',
        re.MULTILINE)
    found = list(key_re.finditer(text, 0, region_end))
    if found:
        match = found[-1]
        start, end = match.span("value")
        return text[:start] + str(value) + text[end:], match.group("value")
    newline = "\r\n" if "\r\n" in text else "\n"
    line = f"{key} = {value}{newline}"
    if header is None:
        if text and not text.endswith("\n"):
            line = newline + line
        return text + line, None
    insert_at = header.start()
    while True:
        previous_end = insert_at - 1 if insert_at > 0 and text[insert_at - 1] == "\n" else None
        if previous_end is None:
            break
        previous_start = text.rfind("\n", 0, previous_end) + 1
        if text[previous_start:previous_end].strip():
            break
        insert_at = previous_start
    return text[:insert_at] + line + text[insert_at:], None


def _harness_ctx_baseline_writable(dest_root: Path) -> None:
    """원장이 이 엔진보다 새 schema 면 `HarnessCtxError` — 반영이 어떤 파일도 쓰기 전에 본다."""
    if adapter_baseline._load_adapter_baseline_document(dest_root).status == "newer-schema":
        raise HarnessCtxError([
            f"어댑터 config 원장 schema 가 이 엔진(지원 상한 {adapter_baseline.ADAPTER_BASELINE_SCHEMA})보다 새로워 "
            "기록할 수 없어 설정 파일을 쓰지 않았습니다 — 엔진을 갱신한 뒤 다시 실행하십시오."])


def _harness_ctx_write(dest_root: Path, changed: dict[str, tuple[str, str]]) -> None:
    """바뀐 설정 파일을 원자 쓰기하고 원장 규칙을 적용한다(`apply_harness_ctx` docstring).

    파일마다 교체를 시도해 결과를 모은 뒤, 교체에 성공한 원장 증언 파일의 `sha256` 을 먼저 기록하고
    나서 쓰지 못한 파일이 있으면 멈춘다 — 이미 바뀐 파일이 원장의 옛 해시에 걸려 편집됨으로 판정되지
    않게 한다(편집됨 판정은 세트 수용에서도 그 파일을 뺀다)."""
    document = adapter_baseline.read_adapter_baseline(dest_root)
    files = dict(document.get("files") or {})
    attested: dict[str, str] = {}
    for relpath, (old_text, new_text) in changed.items():
        entry = files.get(relpath)
        recorded = entry.get("sha256") if isinstance(entry, dict) else None
        old_bytes = old_text.encode("utf-8")
        if recorded is not None and adapter_baseline._baseline_attests_dest(
                recorded, dest_root / relpath, adapter_baseline.content_sha256(old_bytes)):
            attested[relpath] = adapter_baseline.content_sha256(new_text.encode("utf-8"))
    written: list[str] = []
    failed: list[str] = []
    for relpath, (_old_text, new_text) in changed.items():
        if adapter_baseline._atomic_write_dest_bytes(dest_root, Path(relpath), new_text.encode("utf-8")):
            written.append(relpath)
        else:
            failed.append(relpath)
    violations: list[str] = []
    if failed:
        violations.append(
            f"{', '.join(failed)} 를 쓰지 못했습니다 — 그 파일의 기존 내용은 그대로입니다.")
    recorded = {relpath: attested[relpath] for relpath in written if relpath in attested}
    if recorded:
        for relpath, digest in recorded.items():
            files[relpath] = {**files[relpath], "sha256": digest}
        adapter_baseline._write_adapter_baseline(dest_root, {"schema": adapter_baseline.ADAPTER_BASELINE_SCHEMA, "files": files})
        recorded_now = adapter_baseline.read_adapter_baseline(dest_root).get("files", {})
        unrecorded = [relpath for relpath, digest in recorded.items()
                      if not isinstance(recorded_now.get(relpath), dict)
                      or recorded_now[relpath].get("sha256") != digest]
        if unrecorded:
            violations.append(
                f"{', '.join(unrecorded)} 는 반영했지만 원장 기록을 확인하지 못했습니다 — 원장 경로 "
                f"({adapter_baseline.ADAPTER_BASELINE_RELPATH.as_posix()})의 쓰기 권한을 확인한 뒤 다시 실행하십시오.")
    if violations:
        raise HarnessCtxError(violations, written=written)


def _print_harness_ctx_apply(dest_root: Path) -> None:
    """설치·add-harness·어댑터 config 수용 뒤의 반영 한 번 — 운영 환경의 opencode 전역 설정
    디렉터리로 쓰고 줄을 낸다.

    local.conf 에 값 공급을 잃는 옛 키가 남아 있으면 반영하지 않고 한 줄로 알린다 — 이 단계는
    파일을 다 깐 뒤의 후속이고, 이름 이동은 pm-update(`migrate_renamed_conf_keys`)가 하며 그 실행이
    이어서 반영한다(pm_update 의 반영 단계와 같은 처분)."""
    remaining = conf_schema.blocking_legacy(
        conf_store.load(Path(dest_root) / ".project_manager" / "local.conf").legacy)
    if remaining:
        print(f"  ⚠️ 하네스 설정 반영을 건너뜁니다 — local.conf 에 옛 키({', '.join(remaining)})가 "
              "남아 있습니다. pm-update 가 키 이름을 옮긴 뒤 반영합니다.")
        return
    for line in apply_harness_ctx(
            dest_root, write=True,
            opencode_config_dir=opencode_global_config_dir(os.environ, Path.home())):
        print(f"  ✓ 하네스 설정 반영: {line}")
