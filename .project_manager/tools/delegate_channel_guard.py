#!/usr/bin/env python3
"""Harness-neutral native delegation-channel decision core and Claude hook.

``decide`` is the single decision truth shared by harness adapters.  The
argument-based ``decide`` CLI always emits one JSON line and exits successfully,
including usage and infrastructure failures.  With no arguments, the historical
Claude ``PreToolUse`` stdin/stdout hook protocol remains intact.
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import re
import shlex
import stat
import sys
import time
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
ENGINE_REV = "v1.7.4"


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
DELEGATE_TIERS = frozenset({"normal", "hard"})
_WORKTREE_PLACEHOLDER = "<worktree>"
_CODEX_OBSERVATION_RELATIVE_PATH = Path(
    ".project_manager/.local/delegate-channel/codex-observations.jsonl"
)
_CODEX_OBSERVATION_MAX_BYTES = 256 * 1024
_CODEX_OBSERVATION_MAX_FILES = 4
_CODEX_PROMPT_DIR = Path(".project_manager/.local/delegate")

# codex-cli 0.147.0 live hook payload: native delegation exposes this exact
# separator-free tool name and carries the selected custom-agent name in
# ``tool_input.task_name``.  ``SubagentStart.agent_type`` is merely ``default``;
# it is never used as a delegation role or correlation value.
CODEX_SPAWN_TOOL_NAME = "collaborationspawn_agent"
CODEX_ROLE_INPUT_FIELD = "task_name"
CODEX_CORRELATION_FIELDS = ("session_id", "turn_id", "agent_id")

# Agent names are harness-owned surface literals.  Keep their translation in
# this Python truth rather than duplicating the role set in JavaScript adapters.
# OpenCode/Claude currently expose the base names; Codex additionally encodes
# the hard developer tier in its agent name.
AGENT_NAME_PROFILES: dict[str, dict[str, tuple[str, str]]] = {
    "claude": {role: (role, "normal") for role in DELEGATE_ROLES},
    "opencode": {role: (role, "normal") for role in DELEGATE_ROLES},
    "codex": {
        **{role: (role, "normal") for role in DELEGATE_ROLES},
        "developer-hard": ("developer", "hard"),
    },
}

# Claude's native normal-agent surface is finite.  Keep explicit paths instead
# of deriving a filename from hook input: an unknown role/tier must never make
# this guard read an attacker-selected or merely guessed card.
_ENGINE_ROOT = Path(__file__).resolve().parents[2]
CLAUDE_NATIVE_AGENT_CARDS: dict[tuple[str, str], Path] = {
    ("developer", "normal"): Path(".claude/agents/developer.md"),
    ("code-reviewer", "normal"): Path(".claude/agents/code-reviewer.md"),
    ("researcher", "normal"): Path(".claude/agents/researcher.md"),
    ("architect", "normal"): Path(".claude/agents/architect.md"),
}
_FRONTMATTER_MODEL_RE = re.compile(r"^model\s*:\s*(.*)$")


# 사본 불일치를 **의도적으로 흡수**하는 경계의 등록부 (경계 이름 → 사유). 등록되지 않은 경계는
# 흡수 자격이 없다 — 기본 규율은 여전히 "marked skew 는 재-raise" 다.
_ENGINE_REV_SKEW_RECOVERY_REASONS = {
    "hook_fail_open": (
        "PreToolUse/decide 공용 경계는 가드 자신의 고장(사본 skew 포함)으로 정상 위임을 막지 "
        "않는 것이 계약이다 — 훅은 skew 진단(pm-update 처방이 담긴 예외 문구)을 stderr 한 줄로 "
        "남기고, CLI 는 allow JSON 사유로 남겨 rc0 통과한다. 여기서 fail-loud 로 올리면 부분 "
        "동기 하나가 native 위임을 락아웃한다"
    ),
    "observation_append_fail_open": (
        "관측 append 는 부기일 뿐 차단 판정이 아니다 — 장부 쓰기 실패(사본 skew 포함)로 이미 "
        "내려진 allow/deny 를 뒤집거나 훅을 죽이면 정상 위임이 막힌다. 흡수하되 조용하지 않게 "
        "경고 문구(matcher drift 관측 불완전)를 결과 envelope 에 실어 표면화한다"
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


def _load_pm_delegate():
    """Load the delegation engine whose resolver is the configuration truth."""
    return _load_module_from_path(
        Path(__file__).resolve().with_name("pm_delegate.py"),
        "pm_delegate.py",
        verifier=_verify_engine_rev,
        cache=True,
    )


def load_local_config() -> dict[str, str]:
    """Read config through pm_delegate's existing parser/provenance seam."""
    pm_delegate = _load_pm_delegate()
    return pm_delegate.local_config()


def _load_pm_relay():
    """Load the relay that owns harness capabilities and skill entry notation."""
    return _load_module_from_path(
        Path(__file__).resolve().with_name("pm_relay.py"),
        "pm_relay.py",
        verifier=_verify_engine_rev,
        cache=True,
    )


def _load_file_lock():
    """Load the shared O_APPEND and exclusive-lock primitives."""
    return _load_module_from_path(
        Path(__file__).resolve().with_name("file_lock.py"),
        "file_lock.py",
        verifier=_verify_engine_rev,
        cache=True,
    )


def _known_harnesses() -> frozenset[str]:
    """Return the declared harness set from pm_relay's shared capability table."""
    relay = _load_pm_relay()
    markers = getattr(relay, "HARNESS_SESSION_MARKERS", None)
    if not isinstance(markers, Mapping):
        raise TypeError("pm_relay.HARNESS_SESSION_MARKERS must be a mapping")
    return frozenset(str(name).strip() for name in markers if str(name).strip())


def _runtime_skill_entry(skill: str) -> str:
    """Render the project skill entry with the current harness notation."""
    helper = getattr(_load_pm_relay(), "_runtime_skill_entry", None)
    if not callable(helper):
        raise TypeError("pm_relay._runtime_skill_entry must be callable")
    return helper(skill)


def _result(
    verdict: str,
    reason: str,
    *,
    harness: str = "",
    model: str = "",
) -> dict[str, str]:
    """Build the stable one-line-CLI payload shape."""
    return {
        "verdict": verdict,
        "reason": reason,
        "harness": harness,
        "model": model,
    }


def normalize_agent_name(
    agent_name: object,
    self_harness: object,
    tier: object | None = None,
) -> tuple[str, str] | None:
    """Normalize a harness agent literal to ``(role, tier)``.

    An explicit tier is accepted for adapters whose tool surface carries it in
    a separate field.  Conflicts with an encoded tier fail open at the caller.
    """
    if not isinstance(agent_name, str) or not isinstance(self_harness, str):
        return None
    name = agent_name.strip()
    harness = self_harness.strip().lower()
    profile = AGENT_NAME_PROFILES.get(harness, {})
    normalized = profile.get(name)
    if normalized is None:
        return None

    if tier is None:
        return normalized
    if not isinstance(tier, str):
        return None
    explicit_tier = tier.strip().lower()
    if explicit_tier not in DELEGATE_TIERS:
        return None
    encoded_role, encoded_tier = normalized
    if encoded_tier != "normal" and explicit_tier != encoded_tier:
        return None
    if explicit_tier == "hard" and encoded_role != "developer":
        return None
    return encoded_role, explicit_tier


def _resolved_mapping(
    role: str,
    tier: str,
    conf: Mapping[str, object],
) -> tuple[str, str, str]:
    """Resolve through ``pm_delegate.resolve_delegate`` without a second policy.

    The engine owns the exact key contract: normal reads only
    ``delegate.<role>.*`` and hard reads only ``delegate.<role>.hard.*``;
    tuples are atomic and hard never inherits normal.  A configuration error is
    returned as data so this guard can stay fail-open without inventing a
    runnable redirect that the engine itself would reject.
    """
    pm_delegate = _load_pm_delegate()
    try:
        harness, model, _reasoning = pm_delegate.resolve_delegate(
            dict(conf), role, tier, None, None, None
        )
    except pm_delegate.DelegateError as exc:
        return "", "", str(exc)
    return harness, model, ""


def _remediation(role: str, tier: str) -> str:
    tier_arg = " --tier hard" if tier == "hard" else ""
    prompt_file = _CODEX_PROMPT_DIR / f"manual-{role}-{tier}-prompt.md"
    return (
        f"{_runtime_skill_entry('pm-dev-delegate')} 스킬로 위임하라 "
        f"(실행형 처방: 1) 프롬프트를 파일로 저장한 뒤"
        f"(경로: `{prompt_file.as_posix()}`) "
        f"2) backbone `python3 .project_manager/tools/pm_delegate.py "
        f"--role {role}{tier_arg} --prompt-file {prompt_file.as_posix()} "
        f"--cwd {_WORKTREE_PLACEHOLDER}`)"
    )


def _mapping_is_unset(
    role: str,
    tier: str,
    conf: Mapping[str, object],
) -> bool:
    """Whether the engine-owned atomic harness/model tuple is wholly absent."""
    key = f"delegate.{role}" + (".hard" if tier == "hard" else "")
    return not any(
        str(conf.get(f"{key}.{field}") or "").strip()
        for field in ("harness", "model")
    )


def _delegate_mapping_config_is_absent(conf: Mapping[str, object]) -> bool:
    """Whether this clone has no primary/fallback delegation mapping at all."""
    return not any(str(key).startswith("delegate.") for key in conf)


def _frontmatter_model_scalar(value: str) -> str:
    """Parse one model scalar with the project's PyYAML frontmatter grammar."""
    # Hook interpreters may lack the optional runtime dependency even though
    # project management commands normally install it.  Keep module/CLI/hook
    # startup alive; the caller converts this local import failure to the same
    # allow+loud warning as any other native-card inspection failure.
    import yaml

    parsed = yaml.safe_load(f"model: {value}\n")
    if not isinstance(parsed, dict) or set(parsed) != {"model"}:
        raise ValueError("model frontmatter 파싱 결과가 mapping 하나가 아님")
    candidate = parsed["model"]
    if candidate is None:
        raise ValueError("model 값이 비어 있음")
    if not isinstance(candidate, str):
        raise ValueError("model 값이 string scalar가 아님")
    candidate = candidate.strip()
    if not candidate:
        raise ValueError("model 값이 비어 있음")
    return candidate


def _metadata_is_linklike(metadata: os.stat_result) -> bool:
    """Recognize POSIX symlinks and Windows reparse points without following."""
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)


def _secure_dir_fd_supported() -> bool:
    """Whether the interpreter exposes every primitive used by the strong path."""
    supports_dir_fd = getattr(os, "supports_dir_fd", set())
    supports_follow = getattr(os, "supports_follow_symlinks", set())
    return (
        os.open in supports_dir_fd
        and os.stat in supports_dir_fd
        and os.stat in supports_follow
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
    )


def _read_fd_bytes(file_fd: int) -> str:
    """Decode only after the caller has completed every metadata check."""
    chunks: list[bytes] = []
    while True:
        chunk = os.read(file_fd, 64 * 1024)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8")


def _root_directory_identity(
    root: Path,
    expected: tuple[int, int] | None = None,
) -> tuple[int, int]:
    """Bind a lexical engine root to one non-link directory identity."""
    metadata = root.lstat()
    if _metadata_is_linklike(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("agent card engine root symlink/reparse/비-directory 거부")
    identity = (metadata.st_dev, metadata.st_ino)
    if expected is not None and identity != expected:
        raise ValueError("agent card engine root 교체 거부")
    return identity


def _read_known_regular_file_dir_fd(
    root: Path,
    relative: Path,
    root_identity: tuple[int, int],
) -> str:
    """Strong POSIX path: component-wise dir-fd traversal with ``O_NOFOLLOW``."""
    directory_flag = os.O_DIRECTORY
    nofollow_flag = os.O_NOFOLLOW
    nonblock_flag = getattr(os, "O_NONBLOCK", 0)
    cloexec_flag = getattr(os, "O_CLOEXEC", 0)
    current_fd = os.open(
        os.fspath(root), os.O_RDONLY | directory_flag | nofollow_flag
    )
    try:
        opened_root = os.fstat(current_fd)
        if (
            not stat.S_ISDIR(opened_root.st_mode)
            or (opened_root.st_dev, opened_root.st_ino) != root_identity
        ):
            raise ValueError("agent card engine root 교체 거부")
        for part in relative.parts[:-1]:
            before = os.stat(part, dir_fd=current_fd, follow_symlinks=False)
            if _metadata_is_linklike(before) or not stat.S_ISDIR(before.st_mode):
                raise ValueError(
                    f"agent card 경로 symlink/reparse/비-directory 거부: {relative.as_posix()}"
                )
            child_fd = os.open(
                part,
                os.O_RDONLY | directory_flag | nofollow_flag | cloexec_flag,
                dir_fd=current_fd,
            )
            after = os.fstat(child_fd)
            if (
                not stat.S_ISDIR(after.st_mode)
                or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            ):
                os.close(child_fd)
                raise ValueError(
                    f"agent card 경로 교체 거부: {relative.as_posix()}"
                )
            os.close(current_fd)
            current_fd = child_fd

        leaf = relative.parts[-1]
        before = os.stat(leaf, dir_fd=current_fd, follow_symlinks=False)
        if (
            _metadata_is_linklike(before)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
        ):
            raise ValueError(
                f"agent card symlink/reparse/hardlink/비-regular 거부: {relative.as_posix()}"
            )
        file_fd = os.open(
            leaf,
            os.O_RDONLY | nofollow_flag | nonblock_flag | cloexec_flag,
            dir_fd=current_fd,
        )
        try:
            after = os.fstat(file_fd)
            if (
                not stat.S_ISREG(after.st_mode)
                or after.st_nlink != 1
                or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            ):
                raise ValueError(
                    f"agent card inode 교체/비-regular 거부: {relative.as_posix()}"
                )
            return _read_fd_bytes(file_fd)
        finally:
            os.close(file_fd)
    finally:
        os.close(current_fd)


def _read_known_regular_file_portable(
    root: Path,
    relative: Path,
    lexical_root: Path,
    root_identity: tuple[int, int],
) -> str:
    """Portable path for Windows/interpreters without dir-fd traversal.

    All components are lstat'd before open.  Parent identities and the leaf's
    regular/single-link identity are checked again after open but before the
    first content read, so a path replacement cannot redirect bytes into the
    warning channel.
    """
    _root_directory_identity(lexical_root, root_identity)
    _root_directory_identity(root, root_identity)
    parent_identities: list[tuple[Path, tuple[int, int]]] = []
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        observed = current.lstat()
        if _metadata_is_linklike(observed) or not stat.S_ISDIR(observed.st_mode):
            raise ValueError(
                f"agent card 경로 symlink/reparse/비-directory 거부: {relative.as_posix()}"
            )
        parent_identities.append((current, (observed.st_dev, observed.st_ino)))

    target = root / relative
    before = target.lstat()
    if (
        _metadata_is_linklike(before)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
    ):
        raise ValueError(
            f"agent card symlink/reparse/hardlink/비-regular 거부: {relative.as_posix()}"
        )
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    file_fd = os.open(os.fspath(target), flags)
    try:
        after = os.fstat(file_fd)
        if (
            not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        ):
            raise ValueError(
                f"agent card inode 교체/비-regular 거부: {relative.as_posix()}"
            )
        for parent, identity in parent_identities:
            observed = parent.lstat()
            if (
                _metadata_is_linklike(observed)
                or not stat.S_ISDIR(observed.st_mode)
                or (observed.st_dev, observed.st_ino) != identity
            ):
                raise ValueError(
                    f"agent card 경로 교체 거부: {relative.as_posix()}"
                )
        current_leaf = target.lstat()
        if (
            _metadata_is_linklike(current_leaf)
            or not stat.S_ISREG(current_leaf.st_mode)
            or current_leaf.st_nlink != 1
            or (current_leaf.st_dev, current_leaf.st_ino)
            != (after.st_dev, after.st_ino)
        ):
            raise ValueError(
                f"agent card inode 교체/비-regular 거부: {relative.as_posix()}"
            )
        _root_directory_identity(lexical_root, root_identity)
        _root_directory_identity(root, root_identity)
        return _read_fd_bytes(file_fd)
    finally:
        os.close(file_fd)


def _read_known_regular_file(root: Path, relative: Path) -> str:
    """Read a known repo-relative regular file without following links.

    Capable POSIX interpreters use component-wise dir-fd traversal.  Portable
    interpreters lstat every lexical component and recheck parent/leaf identity
    after open.  Both paths require a single-link regular leaf and complete all
    checks before the first content read.
    """
    if relative.is_absolute() or not relative.parts or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise ValueError(f"agent card 상대경로 거부: {relative.as_posix()}")
    lexical_root = Path(os.path.abspath(os.fspath(root)))
    root_identity = _root_directory_identity(lexical_root)
    resolved_root = lexical_root.resolve(strict=True)
    _root_directory_identity(resolved_root, root_identity)
    _root_directory_identity(lexical_root, root_identity)
    target = resolved_root.joinpath(*relative.parts)
    try:
        target.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(
            f"agent card repo containment 밖: {relative.as_posix()}"
        ) from exc

    if _secure_dir_fd_supported():
        return _read_known_regular_file_dir_fd(
            resolved_root, relative, root_identity
        )
    return _read_known_regular_file_portable(
        resolved_root, relative, lexical_root, root_identity
    )


def _read_claude_native_agent_model(
    role: str,
    tier: str,
) -> tuple[str, Path]:
    """Read one explicitly mapped Claude card and return its strict model scalar."""
    relative = CLAUDE_NATIVE_AGENT_CARDS.get((role, tier))
    if relative is None:
        raise ValueError(f"명시 agent card 매핑 없음({role}/{tier})")
    text = _read_known_regular_file(_ENGINE_ROOT, relative)
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("frontmatter 시작 fence 없음")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError("frontmatter 종료 fence 없음") from exc

    values: list[str] = []
    for line in lines[1:end]:
        match = _FRONTMATTER_MODEL_RE.fullmatch(line)
        if match is not None:
            values.append(match.group(1))
    if not values:
        raise ValueError("frontmatter model 없음")
    if len(values) != 1:
        raise ValueError("frontmatter model 중복")
    return _frontmatter_model_scalar(values[0]), relative


def _claude_native_model_warning(role: str, tier: str, model: str) -> str:
    """Return an allow+loud warning for card drift, otherwise an empty string."""
    relative = CLAUDE_NATIVE_AGENT_CARDS.get((role, tier))
    shown_path = relative.as_posix() if relative is not None else "(명시 매핑 없음)"
    try:
        card_model, relative = _read_claude_native_agent_model(role, tier)
    except Exception as exc:
        detail = " ".join(str(exc).split()) or type(exc).__name__
        return (
            f"[delegate-channel/warn] Claude native model 검사 실패"
            f"(role={role}, tier={tier}, conf_model={model}, card={shown_path}, "
            f"error={type(exc).__name__}: {detail}) — native 통과(fail-open)"
        )
    if card_model != model:
        return (
            f"[delegate-channel/warn] Claude native model 불일치"
            f"(role={role}, tier={tier}, conf_model={model}, "
            f"card={relative.as_posix()}, card_model={card_model}) — "
            "native 통과(fail-open)"
        )
    return ""


def decide(
    role: object,
    tier: object,
    conf: Mapping[str, object],
    self_harness: object,
) -> dict[str, str]:
    """Apply decision-table rows 0 through 5 without a hook-specific envelope."""
    known_harnesses = _known_harnesses()
    self_name = self_harness.strip().lower() if isinstance(self_harness, str) else ""

    # Row 0: a broken/misidentified adapter must never false-deny delegation.
    if not self_name or self_name not in known_harnesses:
        return _result(
            "allow",
            f"[delegate-channel/warn] self_harness={self_name or '(empty)'} 미상 — 통과(fail-open)",
        )

    role_name = role.strip() if isinstance(role, str) else ""
    tier_name = tier.strip().lower() if isinstance(tier, str) else ""

    # Row 1: unknown names are recorded instead of becoming a silent no-op.
    # ``record`` is intentionally not ``warn``: adapters reserve user-facing
    # warnings/system-context injection for decision infrastructure failures.
    if role_name not in DELEGATE_ROLES or tier_name not in DELEGATE_TIERS:
        return _result(
            "allow",
            f"[delegate-channel/record] 역할/티어 정규화 실패({role_name or '(empty)'}/{tier_name or '(empty)'}) — 통과(fail-open)",
        )
    if tier_name == "hard" and role_name != "developer":
        return _result(
            "allow",
            f"[delegate-channel/record] 미지원 역할/티어({role_name}/{tier_name}) — 통과(fail-open)",
        )
    if not isinstance(conf, Mapping):
        raise TypeError("delegate config must be a mapping")

    # Mapping is the truth for both native and cross delegation.  Resolve it
    # independently of the cross-only opt-in so native card drift remains
    # observable even when external sending is disabled.
    enabled = str(conf.get("delegate_enabled", "false")).strip().lower() in (
        "true", "1", "yes", "on",
    )

    if _delegate_mapping_config_is_absent(conf):
        return _result("allow", "[delegate-channel/allow] 역할 매핑 미설정")
    mapping_unset = _mapping_is_unset(role_name, tier_name, conf)
    harness, model, resolution_error = _resolved_mapping(role_name, tier_name, conf)

    # The engine rejects an absent/incomplete profile (hard included) instead
    # of inheriting another tier.  Keep native available because this guard's
    # infrastructure boundary is fail-open, but surface the same engine fact
    # and never prescribe an execution-impossible ``--tier hard`` redirect.
    if resolution_error:
        channel = "record" if mapping_unset else "warn"
        return _result(
            "allow",
            f"[delegate-channel/{channel}] 설정 해소 실패"
            f"({resolution_error}) — native 통과(fail-open)",
        )

    # Row 3: no configured destination leaves native delegation available.
    if not harness:
        return _result("allow", "[delegate-channel/allow] 역할 harness 미설정")

    # Row 4: the native surface matches configuration.
    if harness == self_name:
        if self_name == "claude":
            warning = _claude_native_model_warning(role_name, tier_name, model)
            if warning:
                return _result(
                    "allow", warning, harness=harness, model=model
                )
        return _result(
            "allow",
            f"[delegate-channel/allow] conf 와 native harness 일치({harness})",
            harness=harness,
            model=model,
        )

    # Row 5: delegate_enabled gates only cross-harness external sending.  With
    # opt-in off the native call remains available; redirecting it to an rc3
    # command would deadlock the remediation path.
    if not enabled:
        return _result(
            "allow",
            "[delegate-channel/allow] cross 매핑이나 delegate_enabled off",
            harness=harness,
            model=model,
        )

    # Cross-harness native spawn redirects to the ledgered CLI.
    shown_model = model or "(model 미설정)"
    return _result(
        "deny",
        f"[delegate-channel/deny] conf 는 {harness}/{shown_model} — "
        f"{_remediation(role_name, tier_name)}",
        harness=harness,
        model=model,
    )


def _materialize_cwd(result: Mapping[str, str], cwd: object | None) -> dict[str, str]:
    """Replace only the known worktree placeholder with a concrete adapter cwd."""
    materialized = dict(result)
    if not isinstance(cwd, str) or not cwd.strip():
        return materialized
    value = cwd.strip()
    rendered = shlex.quote(value)
    materialized["reason"] = materialized["reason"].replace(
        _WORKTREE_PLACEHOLDER, rendered
    )
    return materialized


def evaluate_hook(
    payload: Mapping[str, object],
    *,
    config_loader: Callable[[], Mapping[str, str]] = load_local_config,
) -> dict[str, object] | None:
    """Return a Claude deny/warning envelope, or ``None`` for a quiet allow."""
    tool_name = payload.get("tool_name")
    if tool_name != "Agent":
        return None

    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, Mapping):
        return None
    normalized = normalize_agent_name(tool_input.get("subagent_type"), "claude")
    if normalized is None:
        return None
    role, tier = normalized

    conf = config_loader()
    result = decide(role, tier, conf, "claude")
    if result["verdict"] != "deny" and not result["reason"].startswith(
        "[delegate-channel/warn]"
    ):
        return None
    result = _materialize_cwd(result, payload.get("cwd"))
    if result["verdict"] != "deny":
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": result["reason"],
            }
        }
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": result["reason"],
        }
    }


def _codex_decision_envelope(result: Mapping[str, str]) -> dict[str, object]:
    """Render the exact host-verified Codex 0.147.0 PreToolUse deny envelope.

    Isolated ``CODEX_HOME`` + ``--dangerously-bypass-hook-trust`` evidence:
    the five-field object below (including ``systemMessage`` and
    ``suppressOutput:false``) produced ``PreToolUse=4``, ``error=2``, and
    ``SubagentStart=0``; the same isolated control with an empty allow object
    produced ``SubagentStart=1``.  These exact bytes are shipped and asserted
    by the wrapper regression test.  A 2026-08-12 rerun of both
    cases was blocked before hook execution by the executor network sandbox
    (WebSocket/HTTPS ``Operation not permitted``; rc=1, ``PreToolUse=0`` and
    ``SubagentStart=0`` for each), so those zeroes are not enforcement evidence.
    The current hooks documentation says unsupported PreToolUse
    ``suppressOutput`` should fail the hook and continue the tool, but the
    measured 0.147.0 host applied the deny before reporting those errors.

    2026-08-12 three-cell re-measurement (codex-cli 0.147.0, isolated
    ``CODEX_HOME`` + ``--dangerously-bypass-hook-trust`` + tee hook, one live
    spawn attempt per cell) settles the repeated review objection:

    ==========================================  ================  ==============
    variant                                     PreToolUse spawn  SubagentStart
    ==========================================  ================  ==============
    deny WITH ``suppressOutput: false``                        1               0
    deny WITHOUT ``suppressOutput``                            1               0
    allow control (hook returns nothing)                       1               1
    ==========================================  ================  ==============

    The field is irrelevant to enforcement: the deny blocks the spawn either
    way, and only the allow control reaches ``SubagentStart``.  The shipped
    five-field object is therefore kept because those exact bytes are the
    measured ones; dropping a field would ship bytes no probe has covered.
    Re-probe end to end on a newer host before changing this; stdout schema
    inspection alone is not evidence of enforcement.
    """
    if result["verdict"] != "deny":
        return {}
    reason = result["reason"]
    return {
        "decision": "block",
        "reason": reason,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
        "systemMessage": reason,
        "suppressOutput": False,
    }


def _codex_spawn_role(payload: Mapping[str, object]) -> str:
    tool_input = payload.get("tool_input")
    value = (
        tool_input.get(CODEX_ROLE_INPUT_FIELD)
        if isinstance(tool_input, Mapping)
        else None
    )
    return value.strip() if isinstance(value, str) else ""


def _codex_audit_path(state_dir: Path | None = None) -> Path:
    if state_dir is not None:
        return Path(state_dir) / _CODEX_OBSERVATION_RELATIVE_PATH.name
    return Path(__file__).resolve().parents[2] / _CODEX_OBSERVATION_RELATIVE_PATH


def _codex_audit_lock_path(target: Path) -> Path:
    return target.with_name(f"{target.name}.lock")


def _codex_rotated_audit_paths(target: Path) -> list[Path]:
    candidates = [
        path
        for path in target.parent.glob(f"{target.stem}.*{target.suffix}")
        if path != target
    ]

    def sort_key(path: Path) -> tuple[int, str]:
        try:
            return path.stat().st_mtime_ns, path.name
        except OSError:
            return 0, path.name

    return sorted(candidates, key=sort_key)


def _codex_audit_paths(target: Path) -> list[Path]:
    paths = _codex_rotated_audit_paths(target)
    if target.is_file():
        paths.append(target)
    return paths


def _prune_codex_observation_logs(
    target: Path, *, max_bytes: int, max_files: int
) -> None:
    """Bound retained JSONL segments; the active append target is never pruned."""
    while True:
        paths = _codex_audit_paths(target)
        total_bytes = 0
        for path in paths:
            try:
                total_bytes += path.stat().st_size
            except OSError:
                pass
        if len(paths) <= max_files and total_bytes <= max_bytes * max_files:
            return
        rotated = [path for path in paths if path != target]
        if not rotated:
            return
        try:
            rotated[0].unlink()
        except FileNotFoundError:
            continue


def _bounded_observation_text(value: object, limit: int) -> str:
    text = value if isinstance(value, str) else ""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _append_codex_observation(
    payload: Mapping[str, object],
    *,
    hook_event_name: str,
    status: str,
    reason: str,
    state_dir: Path | None = None,
) -> None:
    """Append one observation and rotate bounded JSONL segments under a lock."""
    target = _codex_audit_path(state_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "hook_event_name": hook_event_name,
        "status": _bounded_observation_text(status, 128),
        "reason": _bounded_observation_text(reason, 4096),
        "session_id": _bounded_observation_text(payload.get("session_id"), 512),
        "turn_id": _bounded_observation_text(payload.get("turn_id"), 512),
        "agent_id": _bounded_observation_text(payload.get("agent_id"), 512),
        "role": _bounded_observation_text(
            _codex_spawn_role(payload) if hook_event_name == "PreToolUse" else "",
            256,
        ),
    }
    line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
    line_size = len(line.encode("utf-8"))
    max_bytes = _CODEX_OBSERVATION_MAX_BYTES
    max_files = _CODEX_OBSERVATION_MAX_FILES
    if max_bytes < 1 or max_files < 1:
        raise ValueError("Codex observation rotation bounds must be positive")
    if line_size > max_bytes:
        raise ValueError(
            f"Codex observation entry({line_size} bytes) exceeds segment bound({max_bytes})"
        )

    file_lock = _load_file_lock()
    with file_lock.exclusive_file_lock(_codex_audit_lock_path(target), mode=0o600):
        try:
            current_size = target.stat().st_size
        except FileNotFoundError:
            current_size = 0
        if current_size and current_size + line_size > max_bytes:
            rotated = target.with_name(
                f"{target.stem}.{time.time_ns()}.{os.getpid()}{target.suffix}"
            )
            os.replace(target, rotated)
        file_lock.append_atomic(target, line, mode=0o600)
        _prune_codex_observation_logs(
            target, max_bytes=max_bytes, max_files=max_files
        )


def _codex_observation_identity(
    entry: Mapping[str, object],
) -> tuple[str, str, str] | None:
    values = tuple(
        value.strip() if isinstance(value, str) else ""
        for value in (entry.get(field) for field in CODEX_CORRELATION_FIELDS)
    )
    if not all(values):
        return None
    return values


def scan_codex_observation_misses(
    *,
    observation_path: Path | str | None = None,
    state_dir: Path | None = None,
) -> list[dict[str, object]]:
    """Idempotently find SubagentStart rows lacking a prior allowed spawn row.

    Every retained JSONL segment participates.  The scan never renames, deletes,
    or marks an event, so repeated scans over the same bytes return the same rows.
    Live Codex payloads give the parent spawn and child start different turn IDs
    and omit ``agent_id`` from the spawn.  Pair them FIFO within ``session_id``;
    identify/deduplicate starts by the measured session/turn/agent triplet.
    """
    if observation_path is not None and state_dir is not None:
        raise ValueError("choose observation_path or state_dir, not both")
    target = (
        Path(observation_path)
        if observation_path is not None
        else _codex_audit_path(state_dir)
    )

    def scan_unlocked() -> list[dict[str, object]]:
        available_spawns: dict[str, int] = {}
        seen_starts: set[tuple[str, str, str]] = set()
        misses: list[dict[str, object]] = []
        for path in _codex_audit_paths(target):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (FileNotFoundError, OSError, UnicodeError):
                continue
            for raw in lines:
                try:
                    entry = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(entry, Mapping):
                    continue
                event = entry.get("hook_event_name")
                identity = _codex_observation_identity(entry)
                if event == "PreToolUse" and entry.get("status") == "decision_allow":
                    session_id = entry.get("session_id")
                    if isinstance(session_id, str) and session_id.strip():
                        session_id = session_id.strip()
                        available_spawns[session_id] = (
                            available_spawns.get(session_id, 0) + 1
                        )
                elif event == "SubagentStart":
                    if identity is not None and identity in seen_starts:
                        continue
                    if identity is not None:
                        seen_starts.add(identity)
                    session_id = identity[0] if identity is not None else ""
                    if session_id and available_spawns.get(session_id, 0):
                        available_spawns[session_id] -= 1
                    else:
                        misses.append(dict(entry))
        return misses

    # Normal hook writes create the persistent lock before the first JSONL row.
    # Avoid creating any file from a read-only lint scan when no hook has run.
    lock_path = _codex_audit_lock_path(target)
    if not lock_path.is_file():
        return scan_unlocked()
    file_lock = _load_file_lock()
    with file_lock.exclusive_file_lock(lock_path, mode=0o600):
        return scan_unlocked()


def _codex_observation_failure(
    result: Mapping[str, object], exc: BaseException
) -> dict[str, object]:
    detail = " ".join(str(exc).splitlines()).strip() or type(exc).__name__
    warning = (
        f"[delegate-channel/warn] Codex 관측 기록 실패({type(exc).__name__}: {detail}); "
        "차단 판정은 유지하고 matcher drift 관측은 불완전함"
    )
    visible = dict(result)
    hook_output = visible.get("hookSpecificOutput")
    if (
        visible.get("decision") == "block"
        and isinstance(hook_output, Mapping)
        and hook_output.get("permissionDecision") == "deny"
    ):
        combined = f"{visible.get('reason') or ''}; {warning}".lstrip("; ")
        hook = dict(hook_output)
        hook["permissionDecisionReason"] = combined
        visible["reason"] = combined
        visible["hookSpecificOutput"] = hook
        visible["systemMessage"] = combined
        visible["suppressOutput"] = False
        return visible
    visible["systemMessage"] = warning
    visible["suppressOutput"] = False
    return visible


def observe_codex_pretooluse(
    payload: Mapping[str, object],
    result: Mapping[str, object],
    *,
    decision: Mapping[str, str],
    state_dir: Path | None = None,
) -> dict[str, object]:
    """Append the internal decision while returning the host envelope unchanged.

    ``decision`` is required: the host envelope cannot express an allow verdict
    (allow emits ``{}`` so the tool proceeds untouched), so deriving the recorded
    status from the envelope silently degrades every allow row to
    ``decision_unknown`` and makes the spawn/start pairing scan report false
    matcher misses.
    """
    reason = str(decision.get("reason") or result.get("reason") or "")
    permission = str(decision.get("verdict") or "")
    if not permission:
        hook_output = result.get("hookSpecificOutput")
        if isinstance(hook_output, Mapping):
            permission = str(hook_output.get("permissionDecision") or "")
    try:
        _append_codex_observation(
            payload,
            hook_event_name="PreToolUse",
            status=f"decision_{permission or 'unknown'}",
            reason=reason,
            state_dir=state_dir,
        )
    except BaseException as exc:
        _absorb_engine_rev_skew_for_recovery(exc, "observation_append_fail_open")
        return _codex_observation_failure(result, exc)
    return dict(result)


def observe_codex_subagent_start(
    payload: Mapping[str, object], *, state_dir: Path | None = None
) -> dict[str, object]:
    """Append an actual start; correlation is deferred to ``board.py lint``."""
    reason = (
        "[delegate-channel/record] SubagentStart 관측 기록 — "
        "PreToolUse 대조는 board.py lint의 멱등 스캔이 수행"
    )
    result: dict[str, object] = {"suppressOutput": True}

    try:
        _append_codex_observation(
            payload,
            hook_event_name="SubagentStart",
            status="observed",
            reason=reason,
            state_dir=state_dir,
        )
    except BaseException as exc:
        _absorb_engine_rev_skew_for_recovery(exc, "observation_append_fail_open")
        return _codex_observation_failure(result, exc)
    return result


def _evaluate_codex_decision(
    payload: Mapping[str, object],
    *,
    config_loader: Callable[[], Mapping[str, str]] = load_local_config,
) -> dict[str, object] | None:
    """Adapt Codex native spawn input to the shared ``decide`` CLI seam."""
    if payload.get("tool_name") != CODEX_SPAWN_TOOL_NAME:
        return None

    tool_input = payload.get("tool_input") or {}
    agent_name = (
        tool_input.get(CODEX_ROLE_INPUT_FIELD)
        if isinstance(tool_input, Mapping)
        else None
    )
    cwd = payload.get("cwd")
    cli_argv = [
        "decide", "--role", agent_name if isinstance(agent_name, str) else "",
        "--harness", "codex",
    ]
    if isinstance(cwd, str) and cwd.strip() and Path(cwd).is_absolute():
        cli_argv.extend(("--cwd", cwd.strip()))
    cli_stdout = io.StringIO()
    _run_decide_cli(
        cli_argv,
        stdout=cli_stdout,
        config_loader=config_loader,
    )
    result = json.loads(cli_stdout.getvalue())

    if result["verdict"] == "deny":
        if not isinstance(cwd, str) or not cwd.strip() or not Path(cwd).is_absolute():
            result = _result(
                "allow",
                "[delegate-channel/warn] Codex PreToolUse cwd 절대경로 누락 — 통과(fail-open)",
            )
        else:
            result = _materialize_cwd(result, cwd)
    return result


def evaluate_codex_hook(
    payload: Mapping[str, object],
    *,
    config_loader: Callable[[], Mapping[str, str]] = load_local_config,
) -> dict[str, object] | None:
    """Return only the host envelope; keep allow decisions out of host output."""
    result = _evaluate_codex_decision(payload, config_loader=config_loader)
    return None if result is None else _codex_decision_envelope(result)


class _CliUsageError(Exception):
    """Internal argparse failure converted to the fail-open JSON contract."""


class _FailOpenArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _CliUsageError(message)


def _parse_decide_args(argv: list[str]) -> argparse.Namespace:
    parser = _FailOpenArgumentParser(prog="delegate_channel_guard.py", add_help=False)
    parser.add_argument("command", choices=("decide",))
    parser.add_argument("--role", required=True)
    parser.add_argument("--harness", required=True)
    parser.add_argument("--tier")
    parser.add_argument("--cwd")
    parser.add_argument("--help", action="store_true")
    args = parser.parse_args(argv)
    if args.help:
        raise _CliUsageError(
            "usage: delegate_channel_guard.py decide --role ROLE --harness HARNESS [--tier TIER] [--cwd ABS]"
        )
    if args.cwd and not Path(args.cwd).is_absolute():
        raise _CliUsageError("--cwd 는 절대경로여야 한다")
    return args


def _cli_fail_open(reason: str) -> dict[str, str]:
    detail = " ".join(str(reason).splitlines()).strip() or "unknown error"
    return _result(
        "allow",
        f"[delegate-channel/warn] decide CLI 실패({detail}) — 통과(fail-open)",
    )


def _run_decide_cli(
    argv: list[str],
    *,
    stdout: TextIO,
    config_loader: Callable[[], Mapping[str, str]],
) -> int:
    """Run the non-stdin CLI; every path writes exactly one JSON line and rc0."""
    args = _parse_decide_args(argv)
    normalized = normalize_agent_name(args.role, args.harness, args.tier)
    if normalized is None:
        # Preserve row 0 precedence for an unknown self harness.
        if args.harness.strip().lower() not in _known_harnesses():
            result = decide(args.role, args.tier or "normal", {}, args.harness)
        else:
            result = _result(
                "allow",
                f"[delegate-channel/record] 에이전트명 정규화 실패({args.harness}/{args.role}) — 통과(fail-open)",
            )
    else:
        role, tier = normalized
        result = decide(role, tier, config_loader(), args.harness)
    result = _materialize_cwd(result, args.cwd)
    json.dump(result, stdout, ensure_ascii=False, separators=(",", ":"))
    stdout.write("\n")
    return 0


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


def _codex_hook_fail_open(
    exc: BaseException, hook_event_name: str = "PreToolUse"
) -> dict[str, object]:
    detail = " ".join(str(exc).splitlines()).strip() or type(exc).__name__
    reason = (
        f"[delegate-channel/warn] Codex 가드 판정 실패({type(exc).__name__}: {detail}) "
        "— 통과(fail-open)"
    )
    if hook_event_name == "SubagentStart":
        return {"systemMessage": reason, "suppressOutput": False}
    return {"systemMessage": reason, "suppressOutput": False}


def main(
    argv: list[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    config_loader: Callable[[], Mapping[str, str]] = load_local_config,
    state_dir: Path | None = None,
) -> int:
    """Run decide CLI when argv is non-empty, otherwise the legacy Claude hook."""
    try:
        _console_encoding = _load_module_from_path(
            Path(__file__).resolve().with_name("console_encoding.py"),
            "console_encoding.py",
            verifier=_verify_engine_rev,
        )
        _console_encoding.configure_console_utf8()
        output_stream = sys.stdout if stdout is None else stdout
        # ``None`` preserves the historical direct-call hook seam used by tests
        # and wrappers; the module entry point passes process argv explicitly.
        cli_argv = list(argv or [])
        if cli_argv and cli_argv[0] == "codex-hook":
            if cli_argv != ["codex-hook"]:
                raise _CliUsageError("codex-hook 는 추가 인자를 받지 않는다")
            input_stream = sys.stdin if stdin is None else stdin
            payload = json.loads(input_stream.read())
            if not isinstance(payload, Mapping):
                raise TypeError("hook input JSON must be an object")
            decision = _evaluate_codex_decision(
                payload, config_loader=config_loader
            )
            if decision is not None:
                result = _codex_decision_envelope(decision)
                result = observe_codex_pretooluse(
                    payload, result, decision=decision, state_dir=state_dir
                )
                json.dump(
                    result,
                    output_stream,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                output_stream.write("\n")
            return 0
        if cli_argv and cli_argv[0] == "codex-subagent-observe":
            if cli_argv != ["codex-subagent-observe"]:
                raise _CliUsageError("codex-subagent-observe 는 추가 인자를 받지 않는다")
            input_stream = sys.stdin if stdin is None else stdin
            payload = json.loads(input_stream.read())
            if not isinstance(payload, Mapping):
                raise TypeError("hook input JSON must be an object")
            result = observe_codex_subagent_start(payload, state_dir=state_dir)
            json.dump(result, output_stream, ensure_ascii=False)
            output_stream.write("\n")
            return 0
        if cli_argv:
            return _run_decide_cli(
                cli_argv, stdout=output_stream, config_loader=config_loader
            )

        input_stream = sys.stdin if stdin is None else stdin
        payload = json.loads(input_stream.read())
        if not isinstance(payload, Mapping):
            raise TypeError("hook input JSON must be an object")
        result = evaluate_hook(payload, config_loader=config_loader)
        if result is not None:
            json.dump(result, output_stream, ensure_ascii=False)
        return 0
    except BaseException as exc:
        # The shared hook/CLI boundary intentionally absorbs marked engine skew:
        # a broken guard must not lock out either native delegation surface.
        _absorb_engine_rev_skew_for_recovery(exc, "hook_fail_open")
        output_stream = sys.stdout if stdout is None else stdout
        cli_argv = list(argv or [])
        if cli_argv and cli_argv[0] in {"codex-hook", "codex-subagent-observe"}:
            hook_event_name = (
                "SubagentStart"
                if cli_argv[0] == "codex-subagent-observe"
                else "PreToolUse"
            )
            json.dump(
                _codex_hook_fail_open(exc, hook_event_name),
                output_stream,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            output_stream.write("\n")
            _report_fail_open(exc, sys.stderr if stderr is None else stderr)
            return 0
        if cli_argv:
            json.dump(
                _cli_fail_open(f"{type(exc).__name__}: {exc}"),
                output_stream,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            output_stream.write("\n")
            return 0
        _report_fail_open(exc, sys.stderr if stderr is None else stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
