#!/usr/bin/env python3
"""Claude native Agent delegation channel guard.

Claude ``PreToolUse`` hook JSON is read from stdin.  Native ``Agent`` calls for
the four delegation roles are allowed only when ``local.conf`` maps the role to
Claude (or has no harness mapping).  A cross-harness mapping is denied with a
remediation that points back to ``pm_delegate.py``.

This hook deliberately fails open when its own input/config/decision machinery
breaks: normal delegation must not be wedged by a broken guard installation.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TextIO


_TOOLS_BOOTSTRAP = os.path.dirname(os.path.abspath(__file__))
_TOOLS_BOOTSTRAP_FILE = os.path.realpath(
    os.path.join(_TOOLS_BOOTSTRAP, "repo_owned_files.py")
)
_TOOLS_BOOTSTRAP_KEY = f"_project_manager_repo_owned_files_bootstrap:{_TOOLS_BOOTSTRAP_FILE}"
_TOOLS_BOOTSTRAP_MODULE = sys.modules.get(_TOOLS_BOOTSTRAP_KEY)
_TOOLS_BOOTSTRAP_SENTINEL = object()
try:
    if (
        _TOOLS_BOOTSTRAP_MODULE is not None
        and os.path.realpath(getattr(_TOOLS_BOOTSTRAP_MODULE, "__file__", ""))
        != _TOOLS_BOOTSTRAP_FILE
    ):
        sys.modules.pop(_TOOLS_BOOTSTRAP_KEY, None)
        _TOOLS_BOOTSTRAP_MODULE = None
    if _TOOLS_BOOTSTRAP_MODULE is None:
        _TOOLS_BOOTSTRAP_PREVIOUS = sys.modules.pop(
            "repo_owned_files", _TOOLS_BOOTSTRAP_SENTINEL
        )
        _TOOLS_BOOTSTRAP_ADDED = not sys.path or sys.path[0] != _TOOLS_BOOTSTRAP
        if _TOOLS_BOOTSTRAP_ADDED:
            sys.path.insert(0, _TOOLS_BOOTSTRAP)
        try:
            import repo_owned_files as _TOOLS_BOOTSTRAP_MODULE
            if (
                os.path.realpath(getattr(_TOOLS_BOOTSTRAP_MODULE, "__file__", ""))
                != _TOOLS_BOOTSTRAP_FILE
            ):
                raise ImportError(
                    "repo_owned_files 형제 경로 불일치: "
                    f"{getattr(_TOOLS_BOOTSTRAP_MODULE, '__file__', None)!r}"
                )
            sys.modules[_TOOLS_BOOTSTRAP_KEY] = _TOOLS_BOOTSTRAP_MODULE
        finally:
            # 엔진 import bootstrap은 메인 스레드 전용이다. 그래도 위치를 가정한 pop(0)은
            # 피하고, 우리가 넣은 값이 남아 있을 때 그 값만 제거한다.
            if _TOOLS_BOOTSTRAP_ADDED:
                try:
                    sys.path.remove(_TOOLS_BOOTSTRAP)
                except ValueError:
                    pass
            if sys.modules.get("repo_owned_files") is _TOOLS_BOOTSTRAP_MODULE:
                sys.modules.pop("repo_owned_files", None)
            if _TOOLS_BOOTSTRAP_PREVIOUS is not _TOOLS_BOOTSTRAP_SENTINEL:
                sys.modules["repo_owned_files"] = _TOOLS_BOOTSTRAP_PREVIOUS
    _load_module_from_path = _TOOLS_BOOTSTRAP_MODULE.load_module
except Exception as _TOOLS_BOOTSTRAP_ERROR:
    if sys.modules.get(_TOOLS_BOOTSTRAP_KEY) is _TOOLS_BOOTSTRAP_MODULE:
        sys.modules.pop(_TOOLS_BOOTSTRAP_KEY, None)

    def _load_module_from_path(
        path,
        expected_filename,
        *,
        verifier=None,
        allow_unverified=False,
        cache=False,
        cache_key=None,
    ):
        """구형/손상 중앙 seam에서 복구 명령까지 띄우는 import-by-name 폴백."""
        target = os.path.realpath(os.fspath(path))
        if os.path.basename(target) != expected_filename:
            raise ValueError(
                f"module filename mismatch: expected {expected_filename!r}, "
                f"got {os.path.basename(target)!r}"
            )
        if verifier is not None and allow_unverified:
            raise ValueError("choose verifier or allow_unverified=True, not both")
        if verifier is None and not allow_unverified:
            raise ValueError(
                "module load requires verifier or explicit allow_unverified=True"
            )
        module_key = cache_key or f"_project_manager_legacy_loaded:{target}"
        module = sys.modules.get(module_key) if cache else None
        inserted = False
        try:
            if module is None:
                if (
                    target == _TOOLS_BOOTSTRAP_FILE
                    and _TOOLS_BOOTSTRAP_MODULE is not None
                ):
                    module = _TOOLS_BOOTSTRAP_MODULE
                else:
                    import_name = os.path.splitext(expected_filename)[0]
                    previous = sys.modules.pop(
                        import_name, _TOOLS_BOOTSTRAP_SENTINEL
                    )
                    parent = os.path.dirname(target)
                    added = not sys.path or sys.path[0] != parent
                    if added:
                        sys.path.insert(0, parent)
                    try:
                        module = __import__(import_name)
                        if os.path.realpath(getattr(module, "__file__", "")) != target:
                            raise ImportError(
                                f"{expected_filename} 형제 경로 불일치"
                            )
                    finally:
                        if added:
                            try:
                                sys.path.remove(parent)
                            except ValueError:
                                pass
                        if sys.modules.get(import_name) is module:
                            sys.modules.pop(import_name, None)
                        if previous is not _TOOLS_BOOTSTRAP_SENTINEL:
                            sys.modules[import_name] = previous
                if cache:
                    sys.modules[module_key] = module
                    inserted = True
            if verifier is not None:
                verifier(module, expected_filename)
            return module
        except Exception as exc:
            if cache and (inserted or sys.modules.get(module_key) is module):
                sys.modules.pop(module_key, None)
            if target == _TOOLS_BOOTSTRAP_FILE:
                raise RuntimeError(
                    f"엔진 공용 로더 {target}를 불러올 수 없음; "
                    "pm-update로 .project_manager/tools 전체를 재동기화하라."
                ) from exc
            raise


# baked stamp. 소비처는 이 값을 자기 rev와 대조해 부분 동기된 구 사본을 사용하기 전에
# 명시적인 sibling-skew 오류로 막는다.
ENGINE_REV = "v1.7.3"


def _verify_engine_rev(sibling_module, sibling_filename):
    """로드한 형제의 baked ENGINE_REV를 이 사본과 대조한다(skew만 fail-loud)."""
    got = getattr(sibling_module, "ENGINE_REV", None)
    if got != ENGINE_REV:
        err = RuntimeError(
            f"엔진 사본 버전 불일치 — 로더 {Path(__file__).name}(rev={ENGINE_REV!r})가 "
            f"형제 {sibling_filename}(rev={got!r})를 로드했다 (사본 skew: 부분/수동 복사 또는 "
            f"구형 사본). `pm-update`(또는 pm_update.py)로 .project_manager/tools/ 전체를 재동기하라."
        )
        err._engine_rev_skew = True
        raise err


DELEGATE_ROLES = frozenset(
    {"developer", "code-reviewer", "researcher", "architect"}
)


# 사본 불일치를 **의도적으로 흡수**하는 경계의 등록부 (경계 이름 → 사유). 등록되지 않은 경계는
# 흡수 자격이 없다 — 기본 규율은 여전히 "marked skew 는 재-raise" 다.
_ENGINE_REV_SKEW_RECOVERY_REASONS = {
    "hook_fail_open": (
        "PreToolUse 훅 경계는 가드 자신의 고장(사본 skew 포함)으로 정상 위임을 막지 않는 것이 "
        "계약이다 — skew 진단(pm-update 처방이 담긴 예외 문구)은 stderr 한 줄로 남기고 "
        "통과(rc0)한다. 여기서 fail-loud 로 올리면 부분 동기 하나가 모든 Agent 호출을 막는다"
    ),
}


def _is_engine_rev_skew(exc) -> bool:
    """예외가 rev-스탬프 skew(EngineRevSkew·불완전 복사) 유래인지 (fail-soft 흡수 식별)."""
    return getattr(exc, "_engine_rev_skew", False)


def _absorb_engine_rev_skew_for_recovery(exc, boundary: str) -> bool:
    """훅 fail-open 경계가 marked skew 를 의도적으로 흡수했음을 표시한다 (사유 등록 필수).

    반환값으로 일반 실패와 사본 불일치를 구분한다 — 흡수는 하되 조용하지는 않다
    (external_review 동형·self-contained 복제)."""
    reason = _ENGINE_REV_SKEW_RECOVERY_REASONS.get(boundary, "").strip()
    if not reason:
        raise ValueError(f"등록되지 않았거나 사유가 빈 복구 경계: {boundary!r}")
    return _is_engine_rev_skew(exc)


def load_local_config() -> dict[str, str]:
    """Read config through pm_delegate's existing parser/provenance seam."""
    pm_delegate = _load_module_from_path(
        Path(__file__).resolve().with_name("pm_delegate.py"),
        "pm_delegate.py",
        verifier=_verify_engine_rev,
        cache=True,
    )
    return pm_delegate.local_config()


def evaluate_hook(
    payload: Mapping[str, object],
    *,
    config_loader: Callable[[], Mapping[str, str]] = load_local_config,
) -> dict[str, object] | None:
    """Return a Claude deny response, or ``None`` for an unblocked call.

    티어(`delegate.<role>.hard.*`) 축은 판정하지 않는다 — Agent 훅 입력에는 티어 정보가
    없어 base 매핑(`delegate.<role>.harness`)만이 판정 가능한 표면이다.
    """
    tool_name = payload.get("tool_name")
    if tool_name != "Agent":
        return None

    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, Mapping):
        return None
    role = tool_input.get("subagent_type")
    if role not in DELEGATE_ROLES:
        return None

    conf = config_loader()
    if not isinstance(conf, Mapping):
        raise TypeError("pm_delegate.local_config() returned a non-mapping")

    # cross 위임 opt-in(delegate_enabled)이 꺼진 형상에서는 pm_delegate 가 rc3 로 거부라
    # deny 처방이 실행 불가능한 교착이 된다 — native 만 가능한 형상이므로 통과시킨다.
    enabled = str(conf.get("delegate_enabled", "false")).strip().lower() in (
        "true", "1", "yes", "on",
    )
    if not enabled:
        return None

    prefix = f"delegate.{role}"
    harness_value = conf.get(f"{prefix}.harness")
    if harness_value is None:
        return None
    if not isinstance(harness_value, str):
        raise TypeError(f"{prefix}.harness must be a string")
    harness = harness_value.strip()
    if not harness or harness == "claude":
        return None

    model_value = conf.get(f"{prefix}.model")
    if model_value is not None and not isinstance(model_value, str):
        raise TypeError(f"{prefix}.model must be a string")
    model = (model_value or "").strip() or "(model 미설정)"
    reason = (
        f"[delegate-channel/deny] conf 는 {harness}/{model} — "
        f"`pm_delegate.py --role {role}` 로 위임하라"
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _report_fail_open(exc: BaseException, stderr: TextIO) -> None:
    detail = " ".join(str(exc).splitlines()).strip() or type(exc).__name__
    message = (
        f"[delegate-channel/warn] 가드 판정 실패({type(exc).__name__}: {detail}) "
        "— 통과(fail-open)"
    )
    # fail-open 보고 자체가 죽으면 안 된다 — 콘솔 인코딩(CP949 등)이 UTF-8 재설정 전에
    # 깨진 경로에서도 ASCII 폴백으로 강등하고, 그마저 실패하면 조용히 삼킨다.
    try:
        print(message, file=stderr)
    except Exception:
        try:
            print(message.encode("ascii", "replace").decode("ascii"), file=stderr)
        except Exception:
            pass


def main(
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    config_loader: Callable[[], Mapping[str, str]] = load_local_config,
) -> int:
    """Run the stdin/stdout hook protocol; all internal failures pass with rc 0."""
    try:
        _console_encoding = _load_module_from_path(
            Path(__file__).resolve().with_name("console_encoding.py"),
            "console_encoding.py",
            verifier=_verify_engine_rev,
        )
        _console_encoding.configure_console_utf8()
        input_stream = sys.stdin if stdin is None else stdin
        output_stream = sys.stdout if stdout is None else stdout
        payload = json.loads(input_stream.read())
        if not isinstance(payload, Mapping):
            raise TypeError("hook input JSON must be an object")
        result = evaluate_hook(payload, config_loader=config_loader)
        if result is not None:
            json.dump(result, output_stream, ensure_ascii=False)
        return 0
    except BaseException as exc:
        # marked engine skew 도 이 경계는 의도적으로 흡수한다 — 사유는
        # _ENGINE_REV_SKEW_RECOVERY_REASONS["hook_fail_open"] 에 등록돼 있고, skew 예외
        # 문구(pm-update 처방 포함)는 아래 보고가 그대로 stderr 로 내보낸다.
        _absorb_engine_rev_skew_for_recovery(exc, "hook_fail_open")
        _report_fail_open(exc, sys.stderr if stderr is None else stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
