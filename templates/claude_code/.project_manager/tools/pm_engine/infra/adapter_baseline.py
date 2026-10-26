"""어댑터 원장·내용 해시·대상 파일 원자 쓰기 (층: infra)."""
from __future__ import annotations

from pathlib import Path
from pm_engine.domain import install_rules
from pm_engine.infra import fs
from pm_engine.infra import install_files
from typing import NamedTuple
import hashlib
import json
import os
import sys


# ── instance-owned 어댑터 config 의 상류 도달 채널 (3-way 원장) ───────────────────
# 닫는 결함 클래스: 이 파일들은 어느 manifest 에도 없어 pm_update 가 안 덮고, add-harness 재실행도
# 기존 값이 template 과 다르면 byte 보존한다 — 상류의 *동작* fix(훅 차단→비차단 같은)가 기존
# 채택자에 도달할 채널이 0 이다(채택자 실측: 두 릴리스 전 차단판을 들고 운영).
#
# 해법은 3-way 대조다. 설치가 내려놓은 template 해시를 원장에 남기면 다음 동기에서 "채택자가
# 손댔는가" 를 판정할 수 있다 — dest 해시 == 원장 해시면 무편집이므로 백업 후 갱신해도 잃을 값이
# 없고, 다르거나 원장이 없으면 무조건 보존한다(**하한선: 채택자 커스텀은 절대 안 덮는다**).
#
# 원장을 install.json 에 넣지 않는 이유: 그 기록은 문서를 통째 재작성해 구 엔진이 새 키를 지우고,
# schema bump 는 구 엔진의 설치 판정을 추론으로 강등시킨다. 별도 소파일이 두 위험을 다 피한다.
# 원장은 flavor manifest 에 **등재하지 않는다** — 등재하면 byte-copy 가 채택자 원장을 상류 값으로
# 덮어 판정 전체가 거짓이 된다(인스턴스 상태 파일·출하 템플릿 트리에도 없다).
ADAPTER_BASELINE_RELPATH = Path(".project_manager") / "adapter_baseline.json"


ADAPTER_BASELINE_SCHEMA = 1


class AdapterConfigJudgment(NamedTuple):
    """instance-owned config 한 개의 현재 판정 (읽기 전용·write 경로가 이걸 소비한다).

    status:
      `in-sync`    dest == template (할 일 없음·원장만 backfill 대상)
      `unedited`   dest != template 인데 원장이 지금 dest 를 기록한 값 → 상류가 바뀐 것(무편집)
      `edited`     dest != template 이고 원장이 다른 내용을 기록 → 채택자 편집(보존)
      `unrecorded` dest != template 이고 원장 항목 없음 → 판정 불가(보존·안전 기본값)

    `==`/`!=` 와 두 해시 필드는 전부 **내용**(`content_sha256` — 개행 정규화 후) 축이다. 체크아웃이
    개행만 바꾼 파일은 같은 내용이므로 `in-sync` 다. 원장 대조만 예외로 구 축(raw bytes)을 함께
    인정한다(`_baseline_attests_dest` — 무편집 증거가 더 강한 축이고, 기록은 현재 축으로 수렴).
    """
    relpath: str
    harness: str
    mode: str
    status: str
    template: Path
    dest_sha256: str
    baseline_sha256: str | None


class AdapterConfigChannelUnavailable(RuntimeError):
    """기존 managed dest를 판정할 source/dest byte 기준이 없어 완료를 증명할 수 없음."""


def _adapter_config_path_exists(path: Path) -> bool:
    """경로 엔트리가 존재하는가 — broken symlink/비-regular도 ``True``.

    managed config의 완료 게이트는 "읽을 수 있는 regular file인가"와 "그 이름이 트리에
    존재하는가"를 분리해야 한다. ``Path.is_file``/``exists``는 디렉터리와 broken symlink를
    모두 부재로 접어 판정 대상 0 false-green을 만든다. 존재 여부 자체를 확인할 수 없는 IO 오류도
    완료를 증명하지 못하므로 unavailable로 보수 처리한다.
    """
    try:
        Path(path).lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise AdapterConfigChannelUnavailable(
            f"managed 어댑터 config 경로 존재 여부 unavailable: {path} ({exc})") from exc
    return True


def _existing_managed_adapter_harnesses(dest_root: Path) -> list[str]:
    """설치 추론과 별개로 기존 managed 경로가 지목하는 하네스(정규 registry 순서)."""
    found = set()
    for harness, channel in install_rules.ADAPTER_CONFIG_CHANNEL.items():
        for relpath, mode in channel.items():
            if (mode == install_rules.ADAPTER_CONFIG_MANAGED
                    and _adapter_config_path_exists(Path(dest_root) / relpath)):
                found.add(harness)
                break
    return [name for name in install_rules.REGISTERED_HARNESSES if name in found]


def file_sha256(path: Path) -> str | None:
    """파일 **raw bytes** 의 sha256 hex — 읽을 수 없으면 None(fail-soft).

    bytes 결속(판정한 그 bytes 를 쓰기 직전에 재확인)·쓰기 검증 전용이다. **내용 동일성 판정은
    `content_sha256`** 을 쓴다 — 이 다이제스트는 개행 표기까지 내용으로 세기 때문이다."""
    digest = hashlib.sha256()
    try:
        with fs.open_shared(Path(path), binary=True) as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


# ── 내용 동일성 판정 다이제스트 ───────────────────────────────────────────────
# 어댑터 config 원장은 "엔진이 이 **내용**을 깔았다" 를 기록한다. 그 판정을 raw bytes 해시로 하면
# `core.autocrlf=true` 체크아웃이 개행만 바꾼 파일을 채택자 손편집으로 오판하고, Windows 채택자는
# 어댑터 config 전량이 `edited` 로 분류돼 자동 갱신 궤도에서 이탈한다(`--check` 영구 red).
# 그래서 **판정은 개행 정규화 후**, **쓰기는 bytes verbatim**(`_atomic_write_dest_bytes`)으로 축을
# 나눈다 — 위 "개행 표기 보존 왕복" 절의 판정/쓰기 축 분리와 같은 원칙이다.
def content_sha256(path_or_bytes) -> str | None:
    """내용 동일성 판정용 sha256 hex — 텍스트는 개행을 LF 로 접은 뒤 해시한다.

    인자는 경로 또는 이미 읽은 bytes 다(설치할 payload 를 그대로 넘길 수 있다). 경로를 읽을 수
    없으면 None(`file_sha256` 과 같은 fail-soft). UTF-8 로 디코딩되지 않는 바이너리는 정규화
    대상이 아니므로 raw bytes 를 해시한다(`\\r\\n` 이 개행이 아니라 데이터일 수 있다).

    LF 파일에서는 값이 `file_sha256` 과 같다(유효 UTF-8 은 디코드/인코드 왕복이 bytes 보존).
    구 원장 항목이 그대로 유효한 이유이자, 달라진 항목이 무변경 backfill 로 재기록되는 근거다.
    대상은 config 크기 파일이라 통째로 읽는다(스트리밍이 필요한 큰 파일은 `file_sha256`)."""
    if isinstance(path_or_bytes, (bytes, bytearray)):
        raw = bytes(path_or_bytes)
    else:
        try:
            raw = fs.read_bytes_shared(Path(path_or_bytes))
        except OSError:
            return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return hashlib.sha256(raw).hexdigest()
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def _baseline_attests_dest(recorded, dest_path: Path,
                           dest_content_sha256: str) -> bool:
    """원장 값이 **지금 dest 그대로**를 기록한 것인가 (= 기록 시점 이후 무편집).

    인정하는 축은 둘이고 둘 다 "편집이 없었다" 의 증거다:
      · **현재 축**(`content_sha256`) — 개행 표기와 무관한 내용 동일.
      · **구 축**(`file_sha256` — raw bytes) — 판정을 내용 축으로 옮기기 전 엔진이 기록한 값.
        저장 값이 dest **raw** 해시와 같다는 건 그 파일의 bytes 가 기록 시점 그대로라는 뜻이라,
        내용 동일성보다 오히려 강한 증거다(채택자가 손댔다면 raw 해시부터 달라진다).

    구 축이 살아 있는 형상은 **상류 체크아웃까지 CRLF 인 Windows manager 로 설치한 채택자**다 —
    그 원장에는 CRLF raw 다이제스트가 들어갔고, 그 파일의 template 이 그 뒤 바뀌면 dest 가
    template 과 달라 in-sync backfill 이 닿지 않는다. 이 판정이 없으면 설치 이후 아무것도 안 한
    채택자가 영구히 `edited`(채택자 편집 — 보존)로 오판돼 상류 동작 fix 를 못 받는다.

    `record_adapter_baseline` 이 **같은 판정**으로 그 항목을 현재 축으로 재기록한다(무변경
    마이그레이션·판정 사본 0). 판정만 두 축을 보고 기록은 한 축으로 수렴하므로 구 축은 원장에
    누적되지 않는다."""
    if not isinstance(recorded, str):
        return False
    if recorded == dest_content_sha256:
        return True
    return recorded == file_sha256(dest_path)


class AdapterBaselineDocument(NamedTuple):
    """원장 파싱 결과 + 판독 사유 — 읽기(해석)와 쓰기(덮어쓰기)가 **같은 판정**을 본다.

    설치 기록(`InstallReceiptDocument`)과 같은 형상이다: schema 판정을 문서 단계로 올려야
    "해석 못 함" 과 "덮어써도 됨" 이 갈리지 않는다(해석 불가를 빈 값으로 접고 이 엔진 형식으로
    다시 쓰면 미래 형식 데이터가 파괴된다)."""
    document: dict | None
    status: str  # ok | absent | unreadable | corrupt | newer-schema


def _load_adapter_baseline_document(dest_root: Path) -> AdapterBaselineDocument:
    """원장 파일을 파싱한 **원본 문서 + 판독 사유** (부작용 0)."""
    try:
        text = install_files.read_dest_text(Path(dest_root), ADAPTER_BASELINE_RELPATH)
    except FileNotFoundError:
        return AdapterBaselineDocument(None, "absent")
    except (OSError, install_files.UnsafeDestPathError, UnicodeDecodeError):
        return AdapterBaselineDocument(None, "unreadable")
    try:
        data = json.loads(text)
    except ValueError:
        return AdapterBaselineDocument(None, "corrupt")
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        return AdapterBaselineDocument(None, "corrupt")
    schema = data.get("schema")
    if isinstance(schema, int) and schema > ADAPTER_BASELINE_SCHEMA:
        return AdapterBaselineDocument(data, "newer-schema")
    return AdapterBaselineDocument(data, "ok")


def read_adapter_baseline(dest_root: Path) -> dict:
    """판정용 원장 — 해석할 수 없는 상태(부재·손상·상위 schema)는 빈 원장으로 본다.

    fail-soft 가 안전한 이유: 원장은 *갱신해도 되는가* 를 여는 열쇠라, 못 읽으면 아무것도 갱신하지
    않는 쪽(보존 + 보고)으로 떨어진다. ⚠ **빈 원장은 '덮어써도 된다' 는 뜻이 아니다** — 쓰기 판정은
    `_load_adapter_baseline_document` 의 사유를 직접 본다(상위 schema 는 쓰기 거부)."""
    loaded = _load_adapter_baseline_document(dest_root)
    if loaded.status != "ok" or loaded.document is None:
        return {"schema": ADAPTER_BASELINE_SCHEMA, "files": {}}
    return loaded.document


def _write_adapter_baseline(dest_root: Path, document: dict,
                            root_identity: tuple | None = None) -> bool:
    """원장을 dest 안전 쓰기로 기록 — 내용이 바뀌었으면 True.

    **상위 schema 기록은 덮지 않는다**(설치 기록과 동형): 해석할 수 없다고 판정에서 뺀 문서를 이
    엔진 형식으로 다시 쓰면 신 엔진의 원장이 통째로 파괴된다(읽기 거부와 쓰기 거부는 짝이어야
    한다). 그 밖의 기록 실패는 경고 후 False 다 — 호출부가 성공을 주장하지 않도록 결과를 확인한다."""
    if _load_adapter_baseline_document(dest_root).status == "newer-schema":
        print(f"경고: 어댑터 config 원장 schema 가 이 엔진(지원 상한 {ADAPTER_BASELINE_SCHEMA})보다 "
              f"새로워 갱신하지 않습니다 — 기존 원장을 보존합니다 "
              f"({Path(dest_root) / ADAPTER_BASELINE_RELPATH}). 엔진을 갱신한 뒤 다시 실행하세요.",
              file=sys.stderr)
        return False
    text = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        if install_files.read_dest_text(Path(dest_root), ADAPTER_BASELINE_RELPATH,
                          root_identity=root_identity) == text:
            return False  # 멱등 — 같은 내용 재기록은 byte churn 0.
    except (OSError, install_files.UnsafeDestPathError, UnicodeDecodeError):
        pass  # 부재·해독 불가는 아래에서 새로 쓴다.
    try:
        with install_files._fdopen_text(
                install_files._open_dest_relative_nofollow(
                    Path(dest_root), ADAPTER_BASELINE_RELPATH,
                    os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                    root_identity=root_identity, regular_only=True),
                "w", newline="\n") as handle:
            handle.write(text)
    except (OSError, install_files.UnsafeDestPathError) as exc:
        print(f"경고: 어댑터 config 원장을 남기지 못했습니다 "
              f"({Path(dest_root) / ADAPTER_BASELINE_RELPATH}: {exc}) — 다음 동기가 보고 모드로 "
              f"내려갑니다(파일은 그대로).", file=sys.stderr)
        return False
    return True


def _atomic_write_dest_bytes(dest_root: Path, rel: Path, payload: bytes, *,
                             root_identity: tuple | None = None) -> bool:
    """같은 디렉토리 임시 파일 → fsync → `fs.atomic_replace` 로 dest 파일을 원자 교체한다.

    임시 파일도 **symlink 미추종 fd**(`_open_dest_relative_nofollow`)로 열어 조상 검증을 그대로
    받는다. 교체는 이름 바꾸기라 대상이 symlink 여도 그 링크 자체를 대체한다(링크 너머 쓰기 없음).
    실패하면 임시 파일을 치우고 False — 원본은 손대지 않은 상태 그대로다."""
    tmp_rel = rel.with_name(rel.name + ".pm-accept.tmp")
    tmp_path = Path(dest_root) / tmp_rel
    try:
        with install_files._fdopen_binary(
                install_files._open_dest_relative_nofollow(
                    Path(dest_root), tmp_rel, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                    root_identity=root_identity, regular_only=True),
                "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        fs.atomic_replace(tmp_path, Path(dest_root) / rel)
    except (OSError, install_files.UnsafeDestPathError) as exc:
        print(f"경고: 어댑터 config 교체 실패 ({rel.as_posix()}: {exc}) — 기존 내용을 보존합니다.",
              file=sys.stderr)
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True
