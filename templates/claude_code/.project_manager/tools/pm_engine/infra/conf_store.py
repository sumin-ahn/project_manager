"""local.conf·압축 한도 기록 읽기 (층: infra).

파싱·판정·레지스트리는 `pm_engine.domain.conf_schema` 가 소유하고, 이 모듈은 파일을 읽어 그쪽에
넘긴다. 파싱 계약과 `load` 가 raise 하지 않는 이유는 `conf_schema` 모듈 docstring 에 있다.

`local.conf` 의 키 갱신은 설치 key writer(`install_conf._write_conf_keys`)가 **원자 교체**로 하고,
init(`_write_init_local_conf`)은 같은 락 안에서 직접 기록한다. 두 writer 의 텍스트 규칙은
`conf_schema._set_conf_keys` 하나다. 일반 `open` 리더가 하나라도 잡고 있으면 Windows 는 원자 교체를
WinError 32 로 막으므로 판독도 공용 seam 의 공유 읽기(`fs.read_text_shared`)를 지난다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from pm_engine.domain import conf_schema
from pm_engine.infra import fs

from pm_engine.infra import python_env


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


def _conf_default_int(key: str) -> int:
    """이 conf 키의 엔진 기본값(정수) — 정본은 `conf_schema.KEY_REGISTRY` 다.

    기본값이 선언되지 않은 키를 여기로 부르면 `TypeError` 로 멈춘다 — 엔진이 없는 값을 지어내지
    않는다(하네스별 표가 정본인 키는 그 표를 읽는 자리가 따로 있다).
    """
    return int(conf_schema.default_for(key))


def _local_conf_lock_path(conf_path: Path) -> Path:
    """local.conf writer 직렬화 락 경로 — 공용 seam 이 대상 conf 에서 유도한다(사본 0).

    유도 규칙이 도구마다 복제되면 한 사본만 어긋나도 배타가 조용히 사라지므로
    `fs.conf_lock_path` 한 곳이 소유한다. 이름은 호출부/테스트 호환으로 남긴 얇은 별칭이다.
    """
    return fs.conf_lock_path(conf_path)


def _local_conf_write_lock(conf_path: Path):
    """이 conf 를 쓰는 구간의 배타락 — **전 writer 공용**(전체 write·병합 교체).

    board 의 conf writer 는 둘이다(init 최초 생성 · init 비파괴 병합). 둘이 같은 락을 잡아야
    "커밋 전에 읽고 나중에 통째 교체하는" writer 가 그사이의 쓰기를 지우지 않는다.
    재진입 금지 — 이 구간 안에서 다른 conf writer 를 부르지 않는다.
    """
    return fs.local_conf_write_lock(conf_path)


def _write_init_local_conf(home) -> None:
    """init 의 local.conf 쓰기 — 존재 판정·읽기·병합·쓰기를 **한 배타 구간**에 닫는다.

    **세션·prefix 키는 쓰지 않는다.** 둘 다 slot·task 종속 값이라 프로젝트 공용 per-clone
    conf 의 범위가 아니다 — 세션은 lease 장부, prefix 는 areas.md 칼럼이 단일 진실이고 이
    함수는 `runtime.py`·`test.cmd`·ctx 비율 같은 per-clone operational 키만 심는다.

    구간을 나누면 두 곳이 새는데 둘 다 남의 결정을 지운다: (a) 존재 판정과 최초 생성 사이에
    다른 writer 가 만든 conf 를 통째 write 로 덮고, (b) 병합 경로가 읽은 뒤 교체하는 사이에 붙은
    opt-in append(그 자체는 원자였어도)가 교체본에 없어 사라진다. 그래서 락의 단위는 "쓰기" 가
    아니라 "읽고 쓰는 구간" 이고, 같은 락을 conf 의 **모든** writer 가 공유한다.

    온보딩 질문(추가 리뷰어 opt-in)은 이 구간 **밖**에서 한다 — 그 커밋이 같은 락을 다시
    잡으므로 안에서 부르면 재진입(정의되지 않은 동작)이다.
    """
    # 인터프리터 탐지는 conf 내용과 무관한 **외부 프로브**(subprocess)다 — 락 밖에서 미리 푼다
    # (임계 구간을 프로브 시간만큼 늘리지 않는다).
    detected_py = python_env._detect_py()
    detected_test_cmd = python_env.default_pytest_cmd(detected_py)
    with _local_conf_write_lock(home.local_conf):
        if not home.local_conf.exists():
            # 부재 시(첫 생성) — 현행 그대로 전체 default conf write. 회귀 0.
            # 주석은 **값을 재진술하지 않는다** — 손으로 옮겨 적은 값은 반드시 drift 한다.
            # 키가 무엇을 하는지와 전체 카탈로그는 출하 문서 한 곳이 소유한다.
            conf = (
                "# per-clone 설정 (git-ignored). board.py init 생성. clone 마다 다름.\n"
                "# 여기엔 실값만 둔다 — 키 목록·설명은 `local_conf.py --keys` 참조.\n"
                f"runtime.py={detected_py}\n"
                f"test.cmd={detected_test_cmd}\n"
                "project.name=\n"
                f"ctx.checkpoint_at_pct={_conf_default_int('ctx.checkpoint_at_pct')}\n"
                f"ctx.precompact_at_pct={_conf_default_int('ctx.precompact_at_pct')}\n"
                f"ctx.compact_at_pct={_conf_default_int('ctx.compact_at_pct')}\n"
            )
            home.local_conf.write_text(conf, encoding="utf-8", newline="\n")
            return
        # 존재 시 — 비파괴 병합. init 이 안 쓰는 사용자/operational 키
        # (additional_reviewer.*·delegate.*·upstream.*·harness.*·identity.user 등)를 절대
        # 삭제/변경하지 않는다. 통째 write 금지.
        text = fs.read_text_shared(home.local_conf, encoding="utf-8")
        existing = load_checked_readable(home.local_conf)
        updates: dict[str, str] = {}
        # init 기본키는 *없을 때만* 추가 — 기존 값(커스텀 `ctx.compact_at_pct` 등)은 보존.
        defaults = {
            "runtime.py": detected_py,
            "test.cmd": detected_test_cmd,
            "project.name": "",
            "ctx.checkpoint_at_pct": str(_conf_default_int("ctx.checkpoint_at_pct")),
            "ctx.precompact_at_pct": str(_conf_default_int("ctx.precompact_at_pct")),
            "ctx.compact_at_pct": str(_conf_default_int("ctx.compact_at_pct")),
        }
        for key, value in defaults.items():
            if key not in existing:
                updates[key] = value
        # session·prefix 는 어느 경로에서도 쓰지 않는다 — 이미 파일에 남아 있는 그 키도 건드리지
        # 않는다(비파괴 병합·엔진은 채택자 conf 를 대신 고쳐 쓰지 않는다). 읽는 코드가 없으므로
        # 남아 있어도 동작은 같다.
        merged = conf_schema._set_conf_keys(text, updates)
        # trailing newline 보장 — updates 가 비어(default 키 전부 존재) `_set_conf_keys` 가
        # 원문을 그대로 반환하면 마지막 줄이 개행 없이 끝날 수 있고, 그 conf 에 뒤이어 붙는
        # 어떤 append 도 마지막 키에 그대로 이어져 기존 키를 변질시킨다.
        if merged and not merged.endswith("\n"):
            merged += "\n"
        home.local_conf.write_text(merged, encoding="utf-8", newline="\n")
