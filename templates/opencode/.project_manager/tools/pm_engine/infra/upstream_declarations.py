"""상류 package snapshot 과 훅 선언 하위 프로세스 조회 (층: infra).

상류 트리의 훅 세트 선언(`pm_engine.domain.adapter_specs.ADAPTER_HOOK_SET`)을 읽는 유일한 경로다.
선언은 상류 package 의 배송 Python 파일 전부를 한 번 읽은 bytes(snapshot)로 결속한다. 그 bytes 만
실행 clone 의 임시 트리에 새로 내려놓고 별도 `python -I -B -c` 프로세스에서 선언만 직렬화해 받는다.
부모는 받은 JSON 을 엄격히 검사한 뒤 자기 `adapter_specs` 값 형식으로 재구성한다 — 다른 세대의 module·
클래스 객체를 이 프로세스에 들이지 않는다.

같은 snapshot helper 가 파일 집합·해시 알고리즘을 소유한다. 조회(자식 실행)와 수용 직전 재대조가
같은 helper 의 digest 를 쓰므로 두 경로의 결속 기준이 갈리지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

from pm_engine.domain import adapter_specs
from pm_engine.domain import manifest_rules
from pm_engine.infra import fs
from pm_engine.infra import install_conf
from pm_engine.infra import manifest_store
from pm_engine.infra import raw_output
from pm_engine.infra import repo_files

# 상류 checkout 안의 엔진 package 좌표(repo 기준 POSIX relpath). snapshot 키는 이 아래 상대 경로다.
_PACKAGE_RELDIR = ".project_manager/tools/pm_engine"

# 선언 원천 — package 기준 상대 경로.
_DECLARATION_MEMBER = "domain/adapter_specs.py"

# 선언 import 가 성립하려면 반드시 있어야 하는 package 파일. 하나라도 없으면 빈 snapshot 을 정상
#   선언으로 해석하지 않고 명시 실패로 올린다.
_REQUIRED_MEMBERS = ("__init__.py", "domain/__init__.py", _DECLARATION_MEMBER)

# snapshot digest 의 도메인 구분자와 길이 필드 폭(바이트). 경로·길이·내용을 함께 묶는다.
_SNAPSHOT_DOMAIN = b"pm-hook-set-v1\0"
_LENGTH_FIELD_BYTES = 8

# 선언 자식 프로세스의 상한(초). 선언 import 와 JSON 직렬화만 하므로 넉넉한 고정값이다.
DECLARATION_TIMEOUT_SECONDS = 30

# 자식 → 부모 wire 형식.
_WIRE_VERSION = 1
_ENVELOPE_KEYS = frozenset({"version", "declarations"})
_SPEC_KEYS = frozenset({"config_relpath", "live_files", "flag_support", "coupled_groups", "entrypoints"})
_ENTRYPOINT_KEYS = frozenset({"event", "matcher", "dispatcher", "flag"})

# 임시 트리 이름 접두 — 실행 clone 의 `raw_output.temp_root` 아래에 만든다.
_STAGING_PREFIX = ".pm_hook_set_gen."

# 자식 프로그램 — argv[1] 의 임시 tools 경로에서 선언 모듈만 import 해 wire 형식으로 직렬화한다.
_DECLARATION_PROGRAM = (
    "import json, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from pm_engine.domain import adapter_specs\n"
    "def entrypoint(item):\n"
    "    return {'event': item.event, 'matcher': item.matcher,\n"
    "            'dispatcher': item.dispatcher, 'flag': item.flag}\n"
    "def spec(item):\n"
    "    return {'config_relpath': item.config_relpath,\n"
    "            'live_files': list(item.live_files),\n"
    "            'flag_support': {key: list(value) for key, value in item.flag_support.items()},\n"
    "            'coupled_groups': [list(group) for group in item.coupled_groups],\n"
    "            'entrypoints': [entrypoint(entry) for entry in item.entrypoints]}\n"
    "payload = {'version': 1, 'declarations': {name: spec(value) for name, value\n"
    "           in adapter_specs.ADAPTER_HOOK_SET.items()}}\n"
    "sys.stdout.buffer.write(json.dumps(payload).encode('utf-8'))\n"
)


# snapshot 판독의 알려진 실패(열거·읽기·필수 파일 부재). 조회와 수용 재대조가 같은 집합으로 사유를 만든다.
SNAPSHOT_READ_ERRORS = (OSError, repo_files.RepoFilesGitError, manifest_rules.EmptyShippingInventoryError)


class DeclarationWireError(ValueError):
    """자식이 낸 선언 JSON 이 wire 형식과 다르다(키·타입·중복·잔여 출력)."""


def _snapshot_digest(payloads: dict[str, bytes]) -> str:
    """경로 정렬 순서로 (경로 길이, 경로, 내용 길이, 내용)을 이어 붙인 SHA-256."""
    digest = hashlib.sha256(_SNAPSHOT_DOMAIN)
    for member in sorted(payloads):
        path_bytes = member.encode("utf-8")
        payload = payloads[member]
        digest.update(len(path_bytes).to_bytes(_LENGTH_FIELD_BYTES, "big"))
        digest.update(path_bytes)
        digest.update(len(payload).to_bytes(_LENGTH_FIELD_BYTES, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _read_declaration_snapshot(source_root) -> tuple[dict[str, bytes], str]:
    """상류 package 의 배송 Python 파일 bytes 와 그 집합의 digest.

    열거는 기존 출하 경로(`manifest_store._iter_files` — tracked-only·일반 파일·비-git 사본 경고)를
    그대로 쓴다. package 상대 경로가 `.py` 인 파일만 담고 `__pycache__` 아래는 제외한다. 각 파일은 한
    번만 읽으며, 호출자는 이 bytes 를 그대로 실행에 쓴다(다시 읽어 staging 하지 않는다).
    열거·읽기 오류와 필수 파일 부재는 구체 예외로 올린다."""
    root = Path(source_root)
    prefix = _PACKAGE_RELDIR + "/"
    payloads: dict[str, bytes] = {}
    if (root / _PACKAGE_RELDIR).is_dir():
        for relative, source in manifest_store._iter_files(root, _PACKAGE_RELDIR):
            member = PurePosixPath(relative[len(prefix):])
            if member.suffix != ".py" or "__pycache__" in member.parts:
                continue
            payloads[member.as_posix()] = fs.read_bytes_shared(source)
    missing = [member for member in _REQUIRED_MEMBERS if member not in payloads]
    if missing:
        raise FileNotFoundError(
            f"상류 엔진 package 필수 파일 부재({root / _PACKAGE_RELDIR}: {', '.join(missing)})")
    return payloads, _snapshot_digest(payloads)


def _reject_duplicate_keys(pairs: list) -> dict:
    keys = [key for key, _value in pairs]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise DeclarationWireError(f"중복 키 {', '.join(duplicates)}")
    return dict(pairs)


def _reject_constant(token: str):
    raise DeclarationWireError(f"허용하지 않는 수 표기 {token}")


def _exact_keys(value, keys: frozenset, label: str) -> dict:
    if not isinstance(value, dict):
        raise DeclarationWireError(f"{label} 가 객체가 아니다")
    if set(value) != keys:
        raise DeclarationWireError(
            f"{label} 키 불일치(기대 {sorted(keys)} · 실제 {sorted(value)})")
    return value


def _string(value, label: str) -> str:
    if not isinstance(value, str):
        raise DeclarationWireError(f"{label} 가 문자열이 아니다")
    return value


def _optional_string(value, label: str) -> str | None:
    return None if value is None else _string(value, label)


def _string_tuple(value, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise DeclarationWireError(f"{label} 가 목록이 아니다")
    return tuple(_string(item, label) for item in value)


def _decode_entrypoint(value, label: str) -> adapter_specs.AdapterHookEntrypoint:
    fields = _exact_keys(value, _ENTRYPOINT_KEYS, label)
    return adapter_specs.AdapterHookEntrypoint(
        event=_string(fields["event"], f"{label}.event"),
        matcher=_optional_string(fields["matcher"], f"{label}.matcher"),
        dispatcher=_string(fields["dispatcher"], f"{label}.dispatcher"),
        flag=_optional_string(fields["flag"], f"{label}.flag"),
    )


def _decode_spec(value, label: str) -> adapter_specs.AdapterHookSetSpec:
    fields = _exact_keys(value, _SPEC_KEYS, label)
    flag_support = fields["flag_support"]
    if not isinstance(flag_support, dict):
        raise DeclarationWireError(f"{label}.flag_support 가 객체가 아니다")
    groups = fields["coupled_groups"]
    if not isinstance(groups, list):
        raise DeclarationWireError(f"{label}.coupled_groups 가 목록이 아니다")
    entrypoints = fields["entrypoints"]
    if not isinstance(entrypoints, list):
        raise DeclarationWireError(f"{label}.entrypoints 가 목록이 아니다")
    return adapter_specs.AdapterHookSetSpec(
        config_relpath=_string(fields["config_relpath"], f"{label}.config_relpath"),
        live_files=_string_tuple(fields["live_files"], f"{label}.live_files"),
        flag_support={
            _string(flag, f"{label}.flag_support"): _string_tuple(paths, f"{label}.flag_support.{flag}")
            for flag, paths in flag_support.items()
        },
        coupled_groups=tuple(
            _string_tuple(group, f"{label}.coupled_groups") for group in groups),
        entrypoints=tuple(
            _decode_entrypoint(entry, f"{label}.entrypoints[{index}]")
            for index, entry in enumerate(entrypoints)),
    )


def _decode_declarations(stdout: bytes) -> dict:
    """자식 stdout(UTF-8 JSON 한 객체) → `{하네스: AdapterHookSetSpec}`."""
    text = stdout.decode("utf-8")
    document = json.loads(text, object_pairs_hook=_reject_duplicate_keys,
                          parse_constant=_reject_constant)
    envelope = _exact_keys(document, _ENVELOPE_KEYS, "envelope")
    version = envelope["version"]
    if type(version) is not int or version != _WIRE_VERSION:
        raise DeclarationWireError(f"지원하지 않는 wire version {version!r}")
    declarations = envelope["declarations"]
    if not isinstance(declarations, dict) or not declarations:
        raise DeclarationWireError("declarations 가 비어 있지 않은 객체가 아니다")
    return {
        _string(harness, "하네스 이름"): _decode_spec(spec, f"declarations.{harness}")
        for harness, spec in declarations.items()
    }


def _run_declaration_child(home, staged_tools: Path) -> bytes:
    """임시 tools 경로에서 선언 프로그램을 실행해 stdout bytes 를 돌려준다(rc≠0 은 예외)."""
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", _DECLARATION_PROGRAM, str(staged_tools)],
        cwd=str(home.root), capture_output=True, timeout=DECLARATION_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        raise subprocess.CalledProcessError(
            completed.returncode, completed.args, completed.stdout, completed.stderr)
    return completed.stdout


def _stage_snapshot(home, payloads: dict[str, bytes]) -> Path:
    """실행 clone 의 임시 루트 아래 새 디렉터리에 snapshot bytes 만 `pm_engine/` 로 내려놓는다."""
    staging = Path(tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=raw_output.temp_root(home.root)))
    package_root = staging / "staged_tools" / "pm_engine"
    for member, payload in payloads.items():
        target = package_root / member
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    return staging


def _child_failure_reason(exc: BaseException) -> str:
    if isinstance(exc, subprocess.TimeoutExpired):
        stderr = (exc.stderr or b"").decode("utf-8", "replace").strip()
        return f"상류 선언 프로세스 timeout({DECLARATION_TIMEOUT_SECONDS}초) {stderr}".rstrip()
    if isinstance(exc, subprocess.CalledProcessError):
        stderr = (exc.stderr or b"").decode("utf-8", "replace").strip()
        return f"상류 선언 프로세스 실패(rc={exc.returncode}) {stderr}".rstrip()
    return f"상류 선언 판독 실패({type(exc).__name__}: {exc})"


def _upstream_hook_set_declarations(
        home, source_root) -> tuple[dict | None, str | None, str | None]:
    """상류 package 의 선언 테이블 — (테이블, 실패 사유, snapshot digest).

    **해시와 실행되는 선언 코드는 한 벌이다.** snapshot 으로 읽은 bytes 만 실행 Home 의 임시 트리에
    새로 쓰고(바이트코드 캐시 없음) 거기서 자식을 띄운다. 해시 뒤 원천이 바뀌어도 실행 bytes 는 해시한
    그대로다. 상류가 실행 중인 바로 그 경로여도 같은 절차를 밟는다 — 메모리에 적재된 선언은 적재
    시점의 코드라 지금 디스크 bytes 의 해시와 짝이 맞지 않을 수 있다.

    상류 트리에는 아무것도 쓰지 않는다. 임시 트리 정리 실패는 경고와 남은 경로를 출력하고 이미 얻은
    선언·digest 를 유지한다. 알려진 읽기·프로세스·형식 실패는 사유가 있는 미해소 결과다."""
    try:
        payloads, digest = _read_declaration_snapshot(source_root)
    except SNAPSHOT_READ_ERRORS as exc:
        return None, f"상류 선언 package 판독 실패({type(exc).__name__}: {exc})", None
    try:
        staging = _stage_snapshot(home, payloads)
    except OSError as exc:
        return None, f"상류 선언 임시 트리 생성 실패({type(exc).__name__}: {exc})", None
    try:
        try:
            stdout = _run_declaration_child(home, staging / "staged_tools")
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, OSError) as exc:
            return None, _child_failure_reason(exc), None
        try:
            table = _decode_declarations(stdout)
        except (UnicodeDecodeError, ValueError) as exc:
            return None, _child_failure_reason(exc), None
    finally:
        try:
            install_conf._force_rmtree(staging)
        except OSError as exc:
            print(f"경고: 훅 세트 임시 트리 정리 실패 — 임시 디렉토리가 남았습니다: "
                  f"{staging} ({type(exc).__name__}: {exc})", file=sys.stderr)
    return table, None, digest
