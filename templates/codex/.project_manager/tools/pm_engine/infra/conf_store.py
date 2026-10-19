"""local.conf·압축 한도 기록 읽기 (층: infra).

파싱·판정·레지스트리는 `pm_engine.domain.conf_schema` 가 소유하고, 이 모듈은 파일을 읽어 그쪽에
넘긴다. 파싱 계약과 `load` 가 raise 하지 않는 이유는 `conf_schema` 모듈 docstring 에 있다.

`local.conf` 는 키 갱신이 **원자 교체**로 이뤄지는 파일이다(`pm_import._write_conf_keys`). 일반
`open` 리더가 하나라도 잡고 있으면 Windows 는 그 교체를 WinError 32 로 막으므로 판독도 공용 seam 의
공유 읽기(`fs.read_text_shared`)를 지난다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from pm_engine.domain import conf_schema
from pm_engine.infra import fs


# 압축 한도 기록 — 적용 경로(pm-config apply·설치·add-harness·pm_update·어댑터 수용)가 조회한 모델 한도 C 와
# 계산 결과를 한 번 쓰고, 훅·상태줄·구동기·상태 조회는 읽기만 한다. 시간·도구 갱신·환경변수로
# 값이 무효가 되지 않는다 — 같은 모델의 한도가 바뀌면 사용자가 다시 적용한다. `applied_at` 은 지금
# 기록 값이 처음 쓰인 시각이다(값이 같은 재적용은 기록을 다시 쓰지 않는다).
CTX_BUDGET_RELPATH = Path(".project_manager/.local/ctx-budget.json")


def _ctx_record_entry(root, values, harness):
    """기록과 이 도구 항목 — 형식이 다르거나 현재 local.conf 입력과 다르면 `CtxBudgetError`."""
    path = Path(root) / CTX_BUDGET_RELPATH
    damaged = f"압축 한도 기록 손상 ({path}) — {conf_schema._CTX_RERUN}."
    try:
        record = json.loads(fs.read_text_shared(path, encoding="utf-8"))
    except FileNotFoundError:
        raise conf_schema.CtxBudgetError(f"압축 한도 기록이 없습니다 ({path}) — {conf_schema._CTX_RERUN}.") from None
    except ValueError:
        raise conf_schema.CtxBudgetError(damaged) from None
    if not isinstance(record, dict) or type(record.get("schema")) is not int:
        raise conf_schema.CtxBudgetError(damaged)
    if record["schema"] != conf_schema.CTX_BUDGET_SCHEMA:
        raise conf_schema.CtxBudgetError(f"이전 형식(schema {record['schema']}) 압축 한도 기록입니다 — {conf_schema._CTX_RERUN}.")
    inputs, harnesses = record.get("inputs"), record.get("harnesses")
    if (not isinstance(inputs, dict) or set(inputs) != set(conf_schema.CTX_INPUT_KEYS)
            or any(not isinstance(value, str) or not re.fullmatch(r"[+-]?[0-9]+", value) for value in inputs.values())
            or not isinstance(record.get("applied_at"), str) or not isinstance(harnesses, dict)):
        raise conf_schema.CtxBudgetError(damaged)
    if harness not in harnesses:
        raise conf_schema.CtxBudgetError(f"{harness} 압축 한도 기록이 없습니다 ({path}) — {conf_schema._CTX_RERUN}.")
    entry = harnesses[harness]
    models = entry.get("models") if isinstance(entry, dict) else None
    if (not isinstance(models, list) or not models or not isinstance(entry.get("settings"), dict)
            or any(not conf_schema._ctx_positive(entry.get(key)) for key in conf_schema._CTX_ENTRY_INTEGERS)
            or any(not isinstance(model, dict) or not isinstance(model.get("id"), str)
                   or not isinstance(model.get("provider"), str) or not conf_schema._ctx_positive(model.get("C"))
                   or not isinstance(model.get("basis"), dict) or set(model["basis"]) not in conf_schema._CTX_BASIS_FORMS
                   or not all(conf_schema._ctx_positive(value) or (key == "source" and isinstance(value, str)) or (value is None and key in (
                       "max_context_window", "model_context_window")) for key, value in model["basis"].items())
                   for model in models)):
        raise conf_schema.CtxBudgetError(damaged)
    current = conf_schema.ctx_input_values(values)
    changed = [key for key in (f"harness.{harness}.ctx_window_tokens", *conf_schema.CTX_INPUT_KEYS[-3:])
               if inputs[key] != current[key]]
    if changed:
        detail = ", ".join(f"{key} 기록 {inputs[key]} · 현재 {current[key]}" for key in changed)
        raise conf_schema.CtxBudgetError(f"local.conf 값이 적용 기록과 다릅니다 ({detail}) — {conf_schema._CTX_RERUN}.")
    return record, entry


def read_ctx_budget(root, values, harness, session_model=None) -> dict:
    """이 도구의 적용 한도 E·압축 지점 P·경고 비율 — 값 또는 `CtxBudgetError`.

    세션 모델이 기록 모델과 다르면 값은 그대로 두고 `warning` 에 다시 적용하라는 문구를 싣는다."""
    record, entry = _ctx_record_entry(root, values, harness)
    return conf_schema._ctx_budget_view(record, entry, harness, session_model)


def load(path: Path | str) -> conf_schema.ConfResult:
    """`path` 의 local.conf 를 읽어 `ConfResult` 로 돌려준다 — **raise 하지 않는다**.

    부재·`OSError`·`UnicodeError` 는 빈 결과다(conf 없는 트리는 정상 형상). 구표기 잔존 판정은
    호출부가 `assert_no_legacy` 로 명시한다.
    """
    conf_path = Path(path)
    try:
        text = fs.read_text_shared(conf_path, encoding="utf-8")
    except (OSError, UnicodeError):
        return conf_schema.ConfResult({}, {}, conf_path)
    values = conf_schema.parse(text)
    return conf_schema.ConfResult(values, conf_schema.legacy_keys(values), conf_path)


def load_strict(path: Path | str) -> conf_schema.ConfResult:
    """`load` 와 같지만 **판독 실패를 삼키지 않는다** — `OSError`·`UnicodeError` 를 그대로 올린다.

    "conf 가 없다" 와 "conf 를 읽지 못했다" 를 구분해야 하는 소비 지점용이다(예: 실행 프로필을
    conf 에서 해소하는 자리 — 못 읽었는데 빈 conf 로 진행하면 기본값으로 조용히 갈린다).
    부재도 예외(`FileNotFoundError`)이므로 존재 판정은 호출부가 자기 축으로 한다."""
    conf_path = Path(path)
    values = conf_schema.parse(fs.read_text_shared(conf_path, encoding="utf-8"))
    return conf_schema.ConfResult(values, conf_schema.legacy_keys(values), conf_path)


def load_checked(path: Path | str) -> dict[str, str]:
    """`load` + `assert_no_legacy` — **판독 실패까지 빈 결과**로 접는 소비 지점용.

    conf 를 못 읽어도 자기 기본값으로 계속 도는 것이 옳은 자리(부트스트랩 인터프리터 해소·
    `pm-config` 단일 키 조회)가 쓴다. 못 읽은 사실이 실행을 멈춰야 하는 자리는
    `load_checked_readable` 이다 — 두 정책을 한 함수에 섞으면 어느 호출부가 무엇을 기대하는지
    코드에서 사라진다."""
    result = load(path)
    conf_schema.assert_no_legacy(result)
    return result.values


def load_checked_readable(path: Path | str) -> dict[str, str]:
    """부재는 빈 결과, **존재하는데 판독 실패면 예외를 그대로 올린다** + 구표기 fail-loud.

    `local_config()` 계열(`conf_store.local_config`·ticket_finish·additional_reviewer)의 정책이다 — 그 호출부들은
    `OSError`/`UnicodeError` 를 잡아 "실행 전에 중단" 으로 닫는다. 여기서 빈 dict 로 강등하면
    설정 선언을 **확인하지 못한 채** 통과한 실행이 된다."""
    conf_path = Path(path)
    if not conf_path.exists():
        return {}
    result = load_strict(conf_path)
    conf_schema.assert_no_legacy(result)
    return result.values


def local_config(home, repo: Path | None = None) -> dict[str, str]:
    """Per-clone local config (`.project_manager/local.conf`, git-ignored).

    `repo` 를 주면 **그 트리의** conf 를 읽는다(`<repo>/.project_manager/local.conf`) —
    판정 대상 트리가 `home.root` 와 다른 소비처(회귀 수집 증감 — 스위트는 실행 cwd 트리의
    것이다)를 위한 인자. 미지정이면 `home.local_conf` 를 읽는다.
    `additional_reviewer.local_config(repo)` 와 동형 시그니처.

    파싱·판정은 공용 로더(`conf_store`·`conf_schema`)가 소유한다 — 이 함수는 경로만 정하고 값 소비
    지점의 계약(구표기 키가 남아 있으면 `LegacyConfKeyError` fail-loud)을 그대로 물려받는다.
    Missing → {}. per-clone 설정(`runtime.py`·`test.cmd`·`ctx.*`·`upstream.*`)은 git 으로
    공유하지 않는다. `session=`/`prefix=` 는 폐지된 키다 — 세션의 진실은 lease 장부,
    prefix 는 areas.md 칼럼이다(구표기 판정이 그 잔존을 지목한다).
    """
    path = (repo / ".project_manager" / "local.conf") if repo is not None else home.local_conf
    return load_checked_readable(path)
