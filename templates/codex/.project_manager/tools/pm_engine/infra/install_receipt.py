"""설치 기록(install receipt)의 schema·권위·손상 보존·하네스 추론 (층: infra)."""
from __future__ import annotations

from pathlib import Path
from pm_engine.domain import install_rules
from pm_engine.infra import install_files
from pm_engine.infra import install_source
from typing import NamedTuple
import json
import os
import sys


def _pm_install_evidence(source_root: Path) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """`(하네스 → PM 설치 증거 후보, 하네스 → 전체 출하 relpath)`.

    증거 후보 = 그 하네스 **어댑터 네임스페이스 안**의 PM 관례 자산(경로 컴포넌트가
    `_PM_ASSET_NAME_PREFIXES` 로 시작 — `skills/pm-bootstrap/`·`pm_orch_claude.py`·
    `pm-instructions.md`). 네임스페이스 밖(공유 wiki·엔진 backbone)은 하네스를 못 가르므로 제외.

    두 번째 dict(전체 출하)는 **귀속 판정**용이다 — 어떤 증거가 다른 하네스에서도 나오는지
    (예 `.claude/skills/pm-*` 는 claude 네임스페이스지만 opencode template 도 출하) 알아야
    거짓 양성을 막는다. 소스 열거 실패는 그 하네스 빈 집합(호출부가 구조 판정으로 강등)."""
    evidence: dict[str, set[str]] = {}
    shipped_all: dict[str, set[str]] = {}
    for name in install_rules.REGISTERED_HARNESSES:
        adapter_dirs = install_rules.ADD_HARNESS_ADAPTER.get(name, ((), ""))[0]
        rels: set[str] = set()
        every: set[str] = set()
        for dirname in install_rules.HARNESS_TEMPLATE_DIRS.get(name, ()):
            root = Path(source_root) / "templates" / dirname
            if not root.is_dir():
                continue
            try:
                shipped = list(install_source._iter_source_files(root, "full"))
            except Exception as exc:  # noqa: BLE001 — 열거 실패는 판별자 0(구조 판정으로 강등).
                if getattr(exc, "_engine_rev_skew", False):
                    raise
                continue
            for rel, _src in shipped:
                rel_posix = rel.as_posix()
                every.add(rel_posix)
                if not any(rel_posix == d.rstrip("/") or rel_posix.startswith(d.rstrip("/") + "/")
                           for d in adapter_dirs):
                    continue
                if any(part.startswith(install_source._PM_ASSET_NAME_PREFIXES)
                       for part in rel_posix.split("/")):
                    rels.add(rel_posix)
        evidence[name] = rels
        shipped_all[name] = every
    return evidence, shipped_all


# ── 영속 설치 기록(install receipt) ──────────────────────────────────────────
# 이 인스턴스에 PM 어댑터를 **실제로 설치한** 하네스 목록을 인스턴스 메타에 박제한다. 증거 추론
# (`_pm_install_evidence`)은 구조상 두 방향으로 틀릴 수 있다 — 증거가 둘뿐인 하네스는 그 파일이
# 함께 사라지면 미검출(표기 유실)이고, 채택자 자작 진입문서는 거짓 양성(표기 소음)이다. 기록이
# 있으면 그 추론을 아예 안 탄다.
#
# 위치를 local.conf 키가 아니라 **별도 소파일**로 둔 근거:
#   - git 추적: local.conf 는 per-clone 이라 엔진 `.gitignore` 가 무시한다. 그런데 기록이 서술하는
#     대상(어댑터 파일 `.claude/**`·`.codex/**`)은 추적물이라 clone 마다 실재한다 — 기록만 빠지면
#     그 clone 은 다시 추론으로 내려가 이 기록이 닫으려는 한계를 그대로 맞는다. 소파일은 기본
#     추적이라 기록과 대상이 같은 채널로 함께 이동한다(다중 사용자 clone 공유).
#   - clobber 면역: manifest 미등재라 pm_update 동기 대상이 아니고, `board.py init` 이 통째로 다시
#     쓰는 local.conf 의 백업·재병합 왕복도 타지 않는다(재-import 마다 값이 되살아나는 창 없음).
# 형식은 JSON 한 객체 — `harnesses`와 실제로 레이다운한 instance-owned template 좌표만 의미를
# 갖는다. 좌표는 dst relpath → `{weight, source}`이고, source는 checkout 기준 POSIX 경로다. 특히
# lite의 `X.lite.md → X.md` rename을 나중 관측이 full `X.md`로 되짚지 않게 설치 순간 선택을 박제한다.
# 날짜 등 변하는 값은 넣지 않아 같은 설치 형상이면 매 실행 byte 동일이다(re-import churn 0).
INSTALL_RECEIPT_RELPATH = Path(".project_manager") / "install.json"


INSTALL_RECEIPT_SCHEMA = 2


INSTALL_RECEIPT_TEMPLATE_COORDINATES_KEY = "instance_owned_templates"


def _install_receipt_fix_hint(dest_root: Path) -> str:
    """기록을 사람이 고칠 위치를 실값 경로로 알려 주는 꼬리말(수정·복구 채널 노출).

    기록은 관측(증거 추론)을 덮으므로, 잘못 박힌 기록은 사람이 고치지 않으면 계속 그 판정을
    강제한다 — 제거·수정 채널이 없는 채로 두면 유령 하네스가 영구히 독자로 남는다."""
    return (f"수정 경로: {Path(dest_root) / INSTALL_RECEIPT_RELPATH} 의 "
            "`harnesses` 배열을 직접 편집하세요")


class InstallReceiptDocument(NamedTuple):
    """기록 파일 판독 결과 — 문서와 **왜 못 읽었는지**를 함께 나른다.

    `status`: `ok`(문서 유효) · `absent`(파일 없음) · `unreadable`(경로 교체·권한 등 열기 실패) ·
    `corrupt`(열렸는데 JSON·형식이 깨짐). 부재와 손상을 같은 `None` 으로 접으면 기록 갱신이
    손상 파일을 그냥 덮어써 원본이 영구 소실된다 — 그래서 사유를 남긴다."""

    document: dict | None
    status: str


def _load_install_receipt_document(dest_root: Path, *,
                                   quiet: bool = False) -> InstallReceiptDocument:
    """기록 파일을 파싱한 **원본 문서 + 판독 사유**.

    schema 판정을 읽기(해석)와 쓰기(덮어쓰기) 양쪽이 봐야 해서 문서 단계를 따로 둔다. `quiet` 는
    같은 실행에서 두 번 읽을 때(판정 → 기록) 경고가 겹치지 않게 하는 스위치다."""
    dest_root = Path(dest_root)
    if not dest_root.is_dir():
        # 아직 만들어지지 않은 dest(--new 계획 단계) — 기록 없음이 정상.
        return InstallReceiptDocument(None, "absent")
    try:
        text = install_files.read_dest_text(dest_root, INSTALL_RECEIPT_RELPATH)
    except FileNotFoundError:
        # 기록 미도입 인스턴스(구 설치·수기 설치) — 호출부가 추론으로 폴백.
        return InstallReceiptDocument(None, "absent")
    except (OSError, install_files.UnsafeDestPathError, UnicodeDecodeError) as exc:
        if not quiet:
            print(f"경고: 설치 기록을 읽을 수 없어 증거 추론으로 판정합니다 "
                  f"({Path(dest_root) / INSTALL_RECEIPT_RELPATH}: {exc}). "
                  f"{_install_receipt_fix_hint(dest_root)}.", file=sys.stderr)
        return InstallReceiptDocument(None, "unreadable")
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise TypeError("기록 최상위는 객체여야 합니다")
    except (ValueError, TypeError) as exc:
        if not quiet:
            print(f"경고: 설치 기록 형식이 올바르지 않아 증거 추론으로 판정합니다 "
                  f"({Path(dest_root) / INSTALL_RECEIPT_RELPATH}: {exc}). "
                  f"{_install_receipt_fix_hint(dest_root)}.", file=sys.stderr)
        return InstallReceiptDocument(None, "corrupt")
    return InstallReceiptDocument(data, "ok")


def _install_receipt_is_newer_schema(document: dict | None) -> bool:
    """이 엔진이 해석할 수 없는 **상위 schema** 기록인가(읽기 거부·쓰기 거부 공통 판정)."""
    schema = (document or {}).get("schema")
    return isinstance(schema, int) and schema > INSTALL_RECEIPT_SCHEMA


def _read_install_receipt_raw(dest_root: Path) -> list | None:
    """기록의 `harnesses` 값을 **거르지 않고** 반환 — 부재·해독 불가·형식 오류면 `None`.

    등록 필터는 소비 지점(`read_install_receipt`)이 하고, 기록 갱신(`record_install_receipt`)은
    이 원본을 봐야 한다: 신 엔진이 남긴 미등록 하네스 이름을 구 엔진의 재기록이 영구 삭제하지
    않으려면 원본 목록이 필요하다. 경고는 여기서 한 번만 낸다(중복 출력 방지)."""
    document = _load_install_receipt_document(dest_root).document
    if document is None:
        return None
    # 전방 호환 가드: 상위 schema 기록은 이 엔진이 모르는 의미(예 항목별 조건·제외 규칙)를 담을 수
    #   있다 — 그걸 목록으로만 읽으면 신 엔진의 의도를 조용히 왜곡한다. 알리고 추론으로 내려간다.
    if _install_receipt_is_newer_schema(document):
        print(f"경고: 설치 기록 schema {document.get('schema')} 는 이 엔진(지원 상한 "
              f"{INSTALL_RECEIPT_SCHEMA})보다 새롭습니다 — 해석하지 않고 증거 추론으로 판정합니다 "
              f"({Path(dest_root) / INSTALL_RECEIPT_RELPATH}). 엔진을 갱신하세요.",
              file=sys.stderr)
        return None
    recorded = document.get("harnesses")
    if not isinstance(recorded, list):
        print(f"경고: 설치 기록 형식이 올바르지 않아 증거 추론으로 판정합니다 "
              f"({Path(dest_root) / INSTALL_RECEIPT_RELPATH}: harnesses 는 목록이어야 합니다). "
              f"{_install_receipt_fix_hint(dest_root)}.", file=sys.stderr)
        return None
    return recorded


def read_install_receipt(dest_root: Path) -> list[str] | None:
    """인스턴스에 기록된 설치 하네스(registry 정규 순서) — 기록 부재·해독 불가면 `None`.

    `None` 은 "기록 없음"이고 빈 목록과 다르다: 호출부는 `None` 일 때만 증거 추론으로 내려간다.
    깨진 기록(JSON 오류·`harnesses` 부재·상위 schema·등록 하네스 0)도 `None` 로 강등하되 **조용히
    하지 않는다** — 기록을 진실로 쓰던 판정이 침묵으로 추론으로 바뀌면 그 전환을 아무도 못 본다.
    다만 dest 가 아직 없거나(`--new` 계획 단계) `.project_manager/` 가 없는 트리는 정상이라 무음이다.

    읽기는 symlink 미추종 fd 경로다 — 기록 경로가 저장소 밖 지향 링크로 바뀌어도 따라가지 않는다."""
    dest_root = Path(dest_root)
    recorded = _read_install_receipt_raw(dest_root)
    if recorded is None:
        return None
    unknown = [str(name) for name in recorded if name not in install_rules.REGISTERED_HARNESSES]
    if unknown:
        # 신 엔진이 쓴 미래 하네스일 수 있다 — 판정에서 빼되 알린다(등록 밖 이름은 registry 조회
        #   에서 터지므로 통과시킬 수 없고, 조용히 버리면 표기 독자가 말없이 준다). 기록 자체에는
        #   보존된다(`record_install_receipt`) — 구 엔진이 신 엔진의 값을 지우지 않게.
        print(f"경고: 설치 기록에 미등록 하네스가 있어 판정에서 제외합니다: "
              f"{', '.join(unknown)} "
              f"({Path(dest_root) / INSTALL_RECEIPT_RELPATH}).", file=sys.stderr)
    known = [name for name in install_rules.REGISTERED_HARNESSES if name in recorded]
    if not known:
        # 유효 항목 0 = 기록 없음과 같게 다루되(표기 유실보다 소음이 낫다) **조용히 하지 않는다** —
        #   빈 `harnesses` 는 정상 상태가 아니라 잘린·손상된 기록이다.
        print(f"경고: 설치 기록에 유효한 하네스가 없어 증거 추론으로 판정합니다 "
              f"({Path(dest_root) / INSTALL_RECEIPT_RELPATH}). "
              f"{_install_receipt_fix_hint(dest_root)}.", file=sys.stderr)
        return None
    return known


def established_harnesses(dest_root: Path, candidates, preexisting) -> tuple[list, list]:
    """기록에 올릴 자격이 있는 하네스 — `(성립분, 제외분)`.

    "이번 run 이 선택했다" 와 "그 어댑터가 실제로 트리에 섰다" 는 다르다: 적용 단계에서 그 하네스
    파일이 전부 제외되면(경로 교체·계획 뒤 상태 변화) 어댑터는 서지 않는데 기록만 "설치됨" 이 된다
    — 그 순간 기록은 유령을 진실로 만든다(기록이 관측을 덮으므로 스스로 교정되지도 않는다).

    판정은 구조 증거(`_has_adapter_shape`)다. 단 **이미 설치돼 있던 하네스**(`preexisting`)는 그대로
    통과시킨다 — 기록은 설치 사실이라 사용자가 파일을 지웠다고 이번 run 이 임의로 철회하지 않는다
    (철회는 명시 편집 채널)."""
    preexisting_set = set(preexisting)
    established: list = []
    dropped: list = []
    for name in candidates:
        if name in preexisting_set or _has_adapter_shape(dest_root, name):
            established.append(name)
        else:
            dropped.append(name)
    return established, dropped


INSTALL_RECEIPT_CORRUPT_SUFFIX = ".corrupt"


def _preserve_corrupt_install_receipt(dest_root: Path,
                                      root_identity: tuple | None = None) -> bool:
    """손상된 기록을 `install.json.corrupt`(순번)로 **백업**하고 재기록을 허용한다 — 성공 시 True.

    손상을 부재와 같게 다루면 갱신이 그 파일을 `O_TRUNC` 로 덮어 원본이 영구 소실된다(사람이 나중에
    무엇이 깨졌는지 볼 수 없다). 그렇다고 기록을 영영 거부하면 그 인스턴스는 계속 추론 판정에
    머문다 — 그래서 **백업 후 재기록**을 택한다(엔진의 "백업하고 바꾼다" 규율과 같은 결). 백업에
    실패하면 재기록도 하지 않는다(백업 못 하는 파일은 고치지 않는다)."""
    try:
        target = install_files._copy_dest_file_nofollow(
            dest_root, INSTALL_RECEIPT_RELPATH,
            INSTALL_RECEIPT_RELPATH.with_name(
                INSTALL_RECEIPT_RELPATH.name + INSTALL_RECEIPT_CORRUPT_SUFFIX),
            root_identity=root_identity)
    except (OSError, install_files.UnsafeDestPathError) as exc:
        print(f"경고: 손상된 설치 기록을 백업하지 못해 갱신하지 않습니다 "
              f"({Path(dest_root) / INSTALL_RECEIPT_RELPATH}: {exc}) — 원본을 보존합니다.",
              file=sys.stderr)
        return False
    print(f"  ⚠️ 손상된 설치 기록을 백업하고 다시 씁니다: {target.relative_to(dest_root).as_posix()}",
          file=sys.stderr)
    return True


def record_install_receipt(dest_root: Path, harnesses,
                           root_identity: tuple | None = None, *,
                           template_coordinates: dict | None = None) -> bool:
    """설치 하네스를 인스턴스 설치 기록에 박제한다 — 내용이 바뀌었으면 True.

    기록 시점은 **실제 설치·추가가 일어난 run**(`--new`·`--into`·add-harness 의 적용 경로)뿐이다.
    판정 함수(`installed_harnesses`)는 읽기만 한다 — 거기서 쓰면 dry-run·read-only 갱신 확인 같은
    무변경 명령이 인스턴스를 고치고, 비-PM 트리에도 기록을 남긴다. 기록이 없던 구 인스턴스는 그래서
    다음 설치 행위에서 backfill 된다(그 시점의 추론 산출 ∪ 이번 선택을 그대로 박제 — 폴백을 계속
    쌓는 대신 한 번 원천으로 옮긴다). 엔진 동기(pm_update)는 기록 채널이 아니다: 그쪽 dest 는 채택자
    인스턴스일 수도, 출하 템플릿 트리(`--target`)나 프레임워크 checkout 자신일 수도 있어 "설치했다"
    는 사실을 만들 자격이 없다(설치 행위를 한 쪽만 기록한다).

    `template_coordinates`는 이번 복사에서 실제 성공한 instance-owned dst만 받는다. 기존 좌표와
    합쳐 쓰므로 add-harness가 create-if-absent로 보존한 진입문서나 파일 단위 적용 제외분의 과거
    좌표를 현재 full 좌표로 바꾸지 않는다. 인자를 생략한 기존 호출도 이미 기록된 좌표를 보존한다.

    기존 기록의 **미등록 이름은 보존한다**: 그 값은 이 엔진보다 새로운 엔진이 남긴 하네스일 수
    있어, 구 엔진의 재기록이 지우면 신 엔진의 설치 사실이 영구 소실된다(판정에서 빼는 것과 기록에서
    지우는 것은 다른 일이다). 보존분은 registry 순서 뒤에 원래 순서로 붙는다.

    쓰기 실패(경로 교체·권한·`.project_manager/` 부재)는 경고 후 False — 기록은 판정을 더 정확하게
    할 뿐 설치의 성공 조건이 아니므로 설치 전체를 되돌리지 않는다. dest 루트 교체(전체 중단
    클래스)만 예외로 그대로 올라간다."""
    ordered = [name for name in install_rules.REGISTERED_HARNESSES if name in set(harnesses)]
    if not ordered:
        return False
    # **상위 schema 기록은 덮지 않는다** — 해석할 수 없다고 판정에서 뺀 문서를 이 엔진 형식으로
    #   다시 쓰면 신 엔진의 기록이 통째로 파괴된다(읽기 거부와 쓰기 거부는 짝이어야 한다).
    existing = _load_install_receipt_document(dest_root, quiet=True)
    if _install_receipt_is_newer_schema(existing.document):
        print(f"경고: 설치 기록 schema {existing.document.get('schema')} 가 이 엔진(지원 상한 "
              f"{INSTALL_RECEIPT_SCHEMA})보다 새로워 갱신하지 않습니다 — 기존 기록을 보존합니다 "
              f"({Path(dest_root) / INSTALL_RECEIPT_RELPATH}). 엔진을 갱신한 뒤 다시 실행하세요.",
              file=sys.stderr)
        return False
    if existing.status == "unreadable":
        # 경로 자체를 안전하게 열 수 없다(교체·권한) — 새로 쓰면 그 자리를 건드리는 셈이라 멈춘다.
        print(f"경고: 설치 기록 경로를 읽을 수 없어 갱신하지 않습니다 "
              f"({Path(dest_root) / INSTALL_RECEIPT_RELPATH}). "
              f"{_install_receipt_fix_hint(dest_root)}.", file=sys.stderr)
        return False
    if existing.status == "corrupt" and not _preserve_corrupt_install_receipt(
            dest_root, root_identity=root_identity):
        return False
    # 보존 대상은 **이미 읽은 문서**에서 뽑는다 — 여기서 다시 읽으면 판정용 경고("증거 추론으로
    #   판정합니다")가 기록 갱신 실행에서 오도성으로 한 번 더 나간다(이 실행은 판정이 아니다).
    existing_names = (existing.document or {}).get("harnesses")
    preserved = [
        str(name) for name in (existing_names if isinstance(existing_names, list) else [])
        if name not in install_rules.REGISTERED_HARNESSES and str(name) not in ordered
    ]
    seen_preserved: list[str] = []
    for name in preserved:  # 중복 접기(원래 순서 유지·byte 안정).
        if name not in seen_preserved:
            seen_preserved.append(name)
    existing_coordinates = (existing.document or {}).get(
        INSTALL_RECEIPT_TEMPLATE_COORDINATES_KEY)
    coordinates = dict(existing_coordinates) if isinstance(existing_coordinates, dict) else {}
    if template_coordinates:
        for relpath, coordinate in template_coordinates.items():
            if not isinstance(relpath, str) or not isinstance(coordinate, dict):
                continue
            weight = coordinate.get("weight")
            source = coordinate.get("source")
            if weight not in install_rules.WEIGHT_CHOICES or not isinstance(source, str):
                continue
            coordinates[relpath] = {"weight": weight, "source": source}
    payload = {
        "schema": INSTALL_RECEIPT_SCHEMA,
        "harnesses": ordered + seen_preserved,
    }
    if coordinates:
        payload[INSTALL_RECEIPT_TEMPLATE_COORDINATES_KEY] = coordinates
    text = json.dumps(
        payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        if install_files.read_dest_text(dest_root, INSTALL_RECEIPT_RELPATH,
                          root_identity=root_identity) == text:
            return False  # 같은 집합 재기록 — 무변경(멱등·byte churn 0).
    except (OSError, install_files.UnsafeDestPathError, UnicodeDecodeError):
        pass  # 부재·해독 불가는 아래에서 새로 쓴다.
    try:
        with install_files._fdopen_text(
                install_files._open_dest_relative_nofollow(
                    dest_root, INSTALL_RECEIPT_RELPATH,
                    os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                    root_identity=root_identity, regular_only=True),
                "w", newline="\n") as handle:
            handle.write(text)
    except (OSError, install_files.UnsafeDestPathError) as exc:
        print(f"경고: 설치 기록을 남기지 못했습니다 "
              f"({Path(dest_root) / INSTALL_RECEIPT_RELPATH}: {exc}) — 판정이 증거 추론으로 "
              f"내려갑니다. {_install_receipt_fix_hint(dest_root)}.", file=sys.stderr)
        return False
    return True


def _copied_instance_owned_template_coordinates(
        plan: list[install_files.CopyAction], dest_root: Path, source_root: Path,
        copied_relpaths, weight: str) -> dict[str, dict[str, str]]:
    """실제 복사 성공분에서 instance-owned template 좌표를 추출한다.

    계획 전체나 설치 하네스 선언으로 재구성하지 않고 ``CopyAction.src``를 직접 읽는다. 그래야
    lite rename과 공존 중립 source override가 기록에 그대로 남고, 적용 단계에서 제외된 파일이나
    create-if-absent로 보존한 기존 파일은 현재 실행 좌표로 오염되지 않는다.
    """
    dest_root = Path(dest_root)
    source_root = Path(source_root)
    copied = {Path(rel) for rel in copied_relpaths}
    instance_owned = {
        relpath
        for relpaths in install_rules.INSTANCE_OWNED_ADAPTER_FILES.values()
        for relpath in relpaths
    }
    coordinates: dict[str, dict[str, str]] = {}
    for action in plan:
        try:
            dest_rel = action.dst.relative_to(dest_root)
            source_rel = action.src.relative_to(source_root)
        except ValueError:
            continue
        relpath = dest_rel.as_posix()
        if dest_rel not in copied or relpath not in instance_owned:
            continue
        coordinates[relpath] = {
            "weight": weight,
            "source": source_rel.as_posix(),
        }
    return coordinates


def _has_adapter_shape(dest_root: Path, harness: str) -> bool:
    """그 하네스 어댑터의 **구조 증거**(네임스페이스 디렉토리 전부 + 루트 진입문서)가 있는가.

    추론 판정의 1단이자 기록 보유 인스턴스의 유령 형상(기록엔 있는데 트리엔 없음) 판별에 함께
    쓴다 — 두 곳이 같은 판정을 봐야 "무엇을 설치로 보는가"가 갈리지 않는다."""
    adapter_dirs, root_doc = install_rules.ADD_HARNESS_ADAPTER[harness]
    return (all((Path(dest_root) / dirname).is_dir() for dirname in adapter_dirs)
            and (Path(dest_root) / root_doc).is_file())


def _installed_harnesses_with_authority(
        dest_root: Path, source_root: Path | None = None) -> tuple[list[str], str]:
    """dest 트리에 **PM 어댑터가 실제로 설치된** 하네스. registry 정규 순서.

    표기 렌더의 독자 집합은 "이번 run 이 고른 하네스"가 아니라 **그 인스턴스를 읽는 하네스
    전부**다 — codex 인스턴스에 `--into claude` 로 claude 를 얹으면 공유 wiki 는 두 하네스가
    함께 읽는다.

    판정 순서는 **기록 우선**이다: 영속 설치 기록(`read_install_receipt`)이 있으면 그것이 진실이고
    아래 증거 추론은 아예 타지 않는다 — 판별자 파일이 전부 지워져도 독자 집합이 유실되지 않는다.
    기록이 없는 인스턴스(기록 도입 전 설치·수기 설치)만 추론으로 폴백하며, 그 인스턴스는 다음
    설치 행위(`--into` 재-import·add-harness)가 기록을 backfill 한다.

    ⚠ 기록은 **설치 사실**이라 어댑터 파일을 지웠다고 자동 철회되지 않는다 — 제거는 기록을 고치는
    명시 행위여야 한다(현재 제거 채널 없음).

    폴백 판정 2단: (a) 어댑터 네임스페이스 + 루트 진입문서 실재(opencode 가 `.claude/skills` 를 함께
    깔기 때문에 claude 는 `CLAUDE.md` 까지 요구하고 codex 는 두 네임스페이스를 모두 요구한다)
    (b) **PM 자산 실재**(`_pm_install_evidence` 의 증거 집합). (b)가 없으면 일반 프로젝트가 자기
    용도로 가진 `.codex/`·`.agents/`·`AGENTS.md` 를 codex PM 설치로 오인해, PM 카드가 없는
    인스턴스에 codex 호출 표기(`$pm-bootstrap`)를 출하한다.

    (b)는 **전용·공유 증거를 가리지 않는다** — 구조 증거가 있는 하네스는 공유 자산(예 opencode
    도 함께 까는 `.claude/skills/pm-*`)만으로도 설치로 본다. 공유 증거를 다른 하네스에 귀속해
    빼면 공존 인스턴스에서 전용 판별자 하나가 빠졌을 때 그 하네스를 통째로 놓쳐 표기가 유실된다.
    반대 방향 오차(채택자 자작 `CLAUDE.md` + opencode 설치 → claude 도 독자로 셈)는 병기 표기가
    하나 늘 뿐이라 **유실보다 안전하다**(비대칭 판단·거짓 양성 허용).

    ⚠ 폴백의 한계: 증거가 적은 하네스는 그 파일들이 다 지워지면 미검출된다 — opencode 는 증거가
    `.opencode/pm-instructions.md`·`pm_orch_opencode.py`·`command/` 뿐이라 그게 다 사라지면 구조 증거가 있어도
    설치로 안 본다. 이 한계는 **기록이 있는 인스턴스에는 없다**(기록이 추론을 대체한다).

    source_root 미지정/증거 파생 실패는 (a)만으로 판정한다(옛 동작·호출부는 항상 소스를 준다)."""
    dest_root = Path(dest_root)
    recorded = read_install_receipt(dest_root)
    if recorded is not None:
        # 기록이 관측을 덮으므로 **유령 형상**(기록엔 있는데 어댑터 자체가 트리에 없음)은 알린다 —
        #   기록은 진실로 쓰되(판정 불변), 사람이 고칠 위치를 함께 준다. 안 알리면 잘못 박힌 기록이
        #   영구히 그 하네스를 독자로 붙든다(제거 채널이 편집뿐이라 더더욱 보여야 한다).
        ghosts = [name for name in recorded if not _has_adapter_shape(dest_root, name)]
        if ghosts:
            print(f"경고: 설치 기록의 {', '.join(ghosts)} 어댑터가 트리에 없습니다 — 기록을 진실로 "
                  f"쓰므로 표기 독자에는 그대로 남습니다. 제거하려면 "
                  f"{_install_receipt_fix_hint(dest_root)}.", file=sys.stderr)
        return recorded, "receipt"
    evidence, _shipped_all = (
        _pm_install_evidence(source_root) if source_root is not None else ({}, {}))
    structural = [
        candidate for candidate in install_rules.ADD_HARNESS_ADAPTER
        if _has_adapter_shape(dest_root, candidate)
    ]

    def _present(rels) -> bool:
        return any((dest_root / rel).is_file() for rel in rels)

    # 구조 증거(어댑터 네임스페이스 + 루트 진입문서)가 있는 하네스는 **공유 증거만으로도**
    #   인정한다 — 공유 자산을 다른 하네스에 귀속해 빼면 공존 인스턴스에서 전용 판별자 하나가
    #   사라졌을 때 그 하네스가 통째로 미검출된다(claude+opencode 공존에서 `pm_orch_claude.py`
    #   만 없어도 `.claude/skills/pm-*` 가 전부 opencode 몫이 돼 claude 유실·실측). 거짓 양성은
    #   표기 소음이고 거짓 음성은 표기 유실이라 보수적으로 포함한다(비대칭).
    #   판별자를 못 만든 하네스(소스 부재 등)는 구조 판정만으로 인정한다(옛 동작).
    found = [
        name for name in structural
        if not evidence.get(name) or _present(evidence[name])
    ]
    return [name for name in install_rules.REGISTERED_HARNESSES if name in found], "inferred"


def installed_harnesses(dest_root: Path, source_root: Path | None = None) -> list[str]:
    """dest 트리에 설치된 PM 하네스 목록 — 상세 판정은 authority helper의 단일 경로."""
    return _installed_harnesses_with_authority(dest_root, source_root)[0]
