"""설치 공통 install_files (infra)."""
from __future__ import annotations

import errno
from pathlib import Path
from pm_engine.domain import install_rules
from pm_engine.infra import fs
from pm_engine.infra import install_source
from typing import NamedTuple
from typing import NoReturn
import os
import shutil
import stat
import sys

# ── plan 액션 ──────────────────────────────────────────────────────────────
# 결정적 단계(복사·백업·치환)를 액션 리스트로 모은 뒤 apply 에서 실행한다.
# plan/apply 분리 = dry-run 결정론(파일시스템 미변경) + 테스트 용이성 (pm_update 패턴).

class CopyAction:
    """src 파일을 dst 로 복사. 기존 dst 가 있으면 중앙 디렉토리에 백업 후 덮음(--into 비파괴).

    backup:
      - None  = 백업 안 함 — 신규 파일이거나 git-safe(추적&미변경, git 이 복원 가능).
      - Path  = `<dest>/.pm_import_backups/<DATE>/<relpath>` (중앙화·relpath 미러링).
                대상 디렉토리는 run() 이 mkdir(parents) 로 만든다.
    """

    def __init__(self, src: Path, dst: Path, backup: Path | None,
                 dest_root: Path | None = None, *,
                 existed: bool | None = None, is_symlink: bool | None = None,
                 backup_dir_name: str):
        self.backup_dir_name = backup_dir_name
        self.src = src
        self.dst = dst
        self.backup = backup  # None = 백업 안 함(신규 또는 git-safe)
        # dest 루트 바인딩 — `run()` 이 dest 상대 fd 순회로 쓰기 위해 필요하다(plan_copy 가 넣는다).
        #   계획 전용으로 손-구성한 액션(dry-run 미리보기 등)은 None 이고 `run()` 이 fail-loud 한다.
        self.dest_root = dest_root
        # **계획 시점 관측 상태**를 액션에 박아 둔다 — `run()` 이 적용 시점에 다시 보면 그 사이의
        #   생성·삭제가 조용히 다른 동작으로 바뀐다(계획은 "신규" 라 백업이 없는데 그 사이 생긴
        #   사용자 파일을 무백업으로 덮거나, 계획은 "기존" 인데 그 사이 지워진 파일을 되살린다).
        #   인자 생략(손-구성 액션)이면 **생성 시점**을 계획 시점으로 보고 여기서 관측한다.
        self.planned_symlink = dst.is_symlink() if is_symlink is None else is_symlink
        self.planned_existed = (
            (self.planned_symlink or dst.exists()) if existed is None else existed)

    def describe(self) -> str:
        rel = self.dst
        # lite 배치: src 가 `X.lite.md` 인데 dst 가 `X.md` 면 이름이 치환됐다 —
        #   어느 변종이 어디로 가는지 보이게 한다("CLAUDE.lite.md → CLAUDE.md (lite)").
        lite_note = ""
        if self.src.name.endswith(install_rules.LITE_SUFFIX) and self.src.name != self.dst.name:
            lite_note = f"  ({self.src.name} → {self.dst.name}, lite)"
        # 3분기: 신규([copy]) · 충돌 git-safe(백업 생략 [copy · git-safe]) ·
        #   충돌 비-safe([backup+copy] → 중앙 디렉토리 상대경로 `.pm_import_backups/<DATE>/<rel>`).
        if self.backup is not None:
            tail = self.backup.as_posix()
            idx = tail.find(self.backup_dir_name)
            shown = tail[idx:] if idx != -1 else self.backup.name
            return f"  [backup+copy] {rel}  (→ {shown}){lite_note}"
        if self._git_safe_skip:
            return f"  [copy · git-safe] {rel}{lite_note}"
        return f"  [copy] {rel}{lite_note}"

    # describe() 가 신규([copy])와 git-safe skip([copy · git-safe])을 구분하려면 충돌 여부를
    # 알아야 한다 — plan_copy 가 충돌&git-safe 인 액션에 표시한다(기본 False = 신규).
    _git_safe_skip: bool = False

    def run(self, root_identity: tuple | None = None) -> None:
        """복사를 적용한다 — 목적지 쓰기는 **dest 상대 fd 순회**(symlink 미추종)로만 한다.

        옛 코드는 `dst.parent.mkdir(parents=True)` + `shutil.copy2(src, dst)` 로 경로를 열어,
        계획과 복사 사이에 조상·목적지·**dest 루트 자체**가 symlink 로 바뀌면 저장소 밖 트리에
        먼저 썼다(재렌더 채널만 닫아서는 남는 구멍). 이제 디렉토리 생성과 파일 쓰기가 모두
        `_open_dest_relative_nofollow`(컴포넌트별 `O_NOFOLLOW` + 루트 신원 대조)를 탄다.
        원본(src)은 신뢰 소스(템플릿)라 경로로 읽는다."""
        if self.dest_root is None:
            raise ValueError(
                "CopyAction.run 은 dest_root 바인딩이 필요합니다 — 실행 대상은 plan_copy 산출뿐"
                "입니다(손-구성 액션은 계획 미리보기 전용).")
        rel = self.dst.relative_to(self.dest_root)
        # MF1: 기존 dst 가 symlink 이면 shutil.copy2 가 링크를 *따라가* 링크 대상(프로젝트
        #      밖일 수 있음) 파일을 백업/덮어쓴다 — 비파괴 보장 위반 + 외부 파일 변조 위험.
        #      따라서 symlink 는 *링크 자체*를 처리한다: 링크를 그대로 백업(follow 안 함) →
        #      링크 unlink → 일반 파일로 src 복사. 링크 대상 파일은 절대 건드리지 않는다.
        # 계획 뒤 상태가 달라졌으면 **그 파일은 건드리지 않는다** — 적용 시점 관측으로 동작을
        #   갈아타면 (a) 계획엔 없던 새 파일(백업 계획도 없음)을 무백업으로 덮고 (b) 그 사이
        #   사용자가 지운 파일을 되살린다. 둘 다 비파괴 위반이라 호출부가 loud 로 제외한다.
        dst_is_symlink = self.dst.is_symlink()
        dst_existed = dst_is_symlink or self.dst.exists()
        if (dst_existed, dst_is_symlink) != (self.planned_existed, self.planned_symlink):
            was = "있었" if self.planned_existed else "없었"
            now = "생겼" if dst_existed else "사라졌"
            raise PlanStateChangedError(
                f"계획 시점과 상태가 다릅니다({rel.as_posix()}: 계획 때는 {was}고 지금은 "
                f"{now}습니다) — 계획에 없던 처리를 하지 않기 위해 이 파일을 건너뜁니다.")
        # 링크 교체 축(아래 unlink)에서 쓸 leaf 신원 — 상태 확인과 **같은 시점** 값이어야 한다.
        leaf_identity = (
            _dest_leaf_identity(self.dest_root, rel, root_identity=root_identity)
            if dst_is_symlink else None)
        # 백업·삭제는 **dest 만 만지는** 단계다 — 상태 확인 뒤에도 삭제 경쟁이 남으므로
        #   `FileNotFoundError` 를 상태 변화 클래스로 정규화한다(그대로 새면 적용 루프가 죽는다).
        #   소스 쪽 오류는 쓰기 단계가 별도로 가른다(진짜 누락은 그대로 올린다).
        try:
            if dst_existed and not dst_is_symlink:
                # **기존 일반 파일: fd 하나로 백업 읽기와 덮어쓰기를 모두 한다.** 옛 흐름은 백업과
                #   쓰기가 각각 leaf 를 다시 열어, 그 사이 같은 자리가 다른 파일로 바뀌면 백업 없는
                #   새 파일을 잘랐다. 열린 fd 는 계획이 본 inode 에 묶이므로 그 창 자체가 없다
                #   (재검사로 좁히는 게 아니라 구조적 폐쇄 — 루트 신원 고정과 같은 원리).
                _refresh_existing_dest_file(
                    self.dest_root, rel, self.src,
                    backup_base_rel=(
                        self.backup.relative_to(self.dest_root)
                        if self.backup is not None else None),
                    root_identity=root_identity)
                return
            if self.backup is not None and dst_existed:
                # SF1: 백업 경로가 이미 존재하면(같은 날 재실행 등) 덮지 말고 순번 부여 —
                #      가장 오래된 원본(=진짜 사용자 파일)이 살아남게 한다(`_free_backup_path`).
                # 링크 *자체* 백업은 fd 스트리밍으로 표현할 수 없다(내용이 아니라 대상 문자열을
                #   보존해야 한다). 그래서 양쪽 **부모 디렉토리 fd** 안에서 `readlink`/`symlink`
                #   만 한다 — 옛 `copy2(follow_symlinks=False)` 는 원본·백업 경로를 다시 해소해,
                #   부모가 그 사이 교체되면 밖의 링크를 읽거나 밖에 링크를 만들었다.
                _backup_symlink_nofollow(
                    self.dest_root, rel, self.backup.relative_to(self.dest_root),
                    root_identity=root_identity)
            if dst_is_symlink:
                # 링크는 fd 로 붙들 수 없으므로(열면 대상이 열린다) **백업 시점 신원(lstat)** 을
                #   재대조한 뒤 지운다 — 백업한 그 링크가 아니면 건드리지 않는다. 재대조와 unlink
                #   사이 한 syscall 창은 남는다(구조적 폐쇄가 아님·명시).
                _unlink_dest_leaf_if_unchanged(
                    self.dest_root, rel, leaf_identity, root_identity=root_identity)
        except (FileExistsError, FileNotFoundError) as exc:
            raise PlanStateChangedError(
                f"계획 뒤 대상 상태가 달라져 백업·정리를 멈춥니다({rel.as_posix()}: {exc}) — "
                "이 파일을 건너뜁니다(원본·백업 모두 불변).") from exc
        _write_dest_file_from_source_nofollow(
            self.dest_root, rel, self.src, overwrite=False,
            root_identity=root_identity)


class FileVsDirConflict(Exception):
    """dst 위치에 기존 *디렉토리* 가 있어 파일 복사가 불가능.

    src 는 파일인데 dst 가 디렉토리면 shutil.copy2 가 IsADirectoryError 로 터지고, 백업도
    안 된다(디렉토리는 copy2 대상 아님). 비파괴 보장상 사용자 디렉토리를 자동 삭제할 수 없으니
    plan 단계에서 명시적으로 거부한다(apply 부분 복사 전 차단).
    """


class AncestorConflict(Exception):
    """dst 의 *조상* 경로(dest_root 하위)에 symlink·비-디렉토리 파일이 있어
    안전하게 디렉토리를 만들 수 없다.

    위험 둘:
      - 조상이 symlink(프로젝트 밖 가리킴)면 `dst.parent.mkdir(exist_ok=True)`+`shutil.copy2`
        가 링크를 따라가 **프로젝트 밖**에 쓴다 — 비파괴 위반.
      - 조상이 일반 파일이면 plan 은 통과한 뒤 apply 중 `mkdir` 가 터져 **부분 복사** 잔존.
    CopyAction.run 의 dst-자체 symlink 처리(MF1)는 조상은 못 막으므로 plan 단계에서 조상
    컴포넌트를 따로 거부한다(dry-run·apply 모두 안전). dest_root 자신은 --into/--new 가드가
    처리하므로 그 *하위* 조상에만 집중한다.
    """


class DestRootSwappedError(RuntimeError):
    """계획 시점에 고정한 **dest 루트 자체**가 다른 디렉토리로 바뀌었다.

    파일 단위 경로 교체(`UnsafeDestPathError`)와 **의도적으로 다른 클래스**다 — 그쪽은 그 파일만
    빼고 진행하지만, 루트가 바뀐 뒤의 계속 진행은 남은 단계(치환·fill·board init 등 우리가 fd 로
    감싸지 못하는 외부 단계 포함)가 **교체된 트리에 쓰는** 것을 뜻한다. 그래서 즉시 전체 중단이다.

    이 시점의 중단은 "부분 적용을 남긴다"가 아니라 **오염 차단**이다: 대상 트리는 이미 공격자가
    바꿔치기한 것이라 거기서 무엇을 더 하든 원래 인스턴스를 완성하지 못한다. 남은 작업을 계속해
    저장소 밖에 쓰는 것보다, 멈추고 사람에게 알리는 쪽이 항상 낫다."""


class PlanStateChangedError(RuntimeError):
    """계획 시점에 관측한 대상 상태(존재/부재·symlink 여부)가 적용 시점에 달라졌다.

    적용 시점 재관측으로 동작을 갈아타면 두 방향 모두 비파괴를 깬다 — 계획이 "신규"(백업 없음)인
    자리에 그 사이 생긴 사용자 파일을 무백업으로 덮거나, 계획이 "기존"인 자리에서 그 사이 지워진
    파일을 되살린다. **파일 단위 제외 + loud** 클래스다(루트 교체와 달리 전체 중단 아님)."""


class UnsafeDestPathError(RuntimeError):
    """dest 안이어야 할 경로가 symlink·조상 symlink·`..` 로 저장소 밖을 가리켜 거부됐다.

    읽기/쓰기 전에 `_is_safe_dest_path` 로 판정하고 **작업 시작 전** 던진다(부분 적용 0·외부
    파일 불변). RuntimeError 를 상속해 옛 호출부 처리를 유지하면서, CLI 경계
    (`add_harness_cli`)가 이 타입만 골라 친화 메시지 + rc 1 로 번역한다(traceback 0)."""


def _free_backup_path(backup: Path) -> Path:
    """backup 경로가 비었으면 그대로, 점유됐으면 .1·.2… 순번을 붙여 빈 경로 반환.

    점유 판정은 **링크를 따라가지 않는다**(`os.path.lexists`) — `exists()` 는 **깨진 symlink**
    에 False 를 주므로, 그 자리를 "비었다"고 보고 그대로 쓰면 뒤이은 `shutil.copy2` 가 링크를
    따라가 **저장소 밖에 파일을 만든다**. 링크 자체가 있으면 점유로 보고 다음 순번을 고른다
    (원본 보존 + 외부 쓰기 차단)."""
    if not os.path.lexists(backup):
        return backup
    n = 1
    while True:
        candidate = backup.with_name(f"{backup.name}.{n}")
        if not os.path.lexists(candidate):
            return candidate
        n += 1


def read_text_keeping_newlines(path: Path) -> str:
    """줄끝을 **번역하지 않고** 읽는다(`\\r\\n` 은 `\\r\\n` 그대로).

    기본 텍스트 모드는 universal-newlines 라 `read_text`→`write_text` 왕복만으로 CRLF 가 LF 로
    바뀐다. 렌더 범위가 *인스턴스 소유* 문서까지 넓어진 뒤에는 그게 표기와 무관한 바이트 변경
    (Windows 채택자 트리 전체 줄끝 뒤집기·git diff 오염)이므로 원본 줄끝을 보존한다."""
    with fs.open_shared(path, binary=False, encoding="utf-8", newline="") as handle:
        return handle.read()


def write_text_keeping_newlines(path: Path, text: str) -> None:
    """줄끝을 **번역하지 않고** 쓴다 — `read_text_keeping_newlines` 와 쌍(왕복 byte 보존)."""
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)


# ── 개행 표기 보존 왕복 ───────────────────────────────────────────────────────
# 엔진이 자기 소유 파일을 **읽어서 다시 쓰는** 경로(engine.manifest guest 절 동기 등)는 본문을
# LF 로 다뤄야 판정이 단순한데, 그대로 되쓰면 CRLF 체크아웃 채택자의 파일 전체가 LF 로 뒤집힌다
# (engine.manifest 의 append-only byte 불변식 위반·손대지 않은 줄까지 전면 diff).
# 그래서 **판정은 LF 정규화 후, 쓰기는 원본 표기 복원**으로 두 축을 분리한다.
# 표기의 진실은 **그 파일의 현재 내용**이다 — 플랫폼(os.linesep)이 아니다. "Windows 면 CRLF" 로
# 분기하면 `core.autocrlf=false` 로 LF 체크아웃한 Windows 채택자를 반대로 깨뜨린다.
DEFAULT_TEXT_NEWLINE = "\n"


def dominant_newline(text: str) -> str:
    """번역 없이 읽은 본문의 지배 개행 (`"\\r\\n"` 또는 `"\\n"`).

    다수결로 정하고 동수면 **첫 등장** 표기를 쓴다. 개행이 하나도 없으면 `DEFAULT_TEXT_NEWLINE`.
    """
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    if crlf == 0 and lf == 0:
        return DEFAULT_TEXT_NEWLINE
    if crlf != lf:
        return "\r\n" if crlf > lf else "\n"
    first = text.find("\n")
    return "\r\n" if first > 0 and text[first - 1] == "\r" else "\n"


def read_text_preserving_newline(path: Path) -> tuple[str, str]:
    """`(LF 정규화 본문, 지배 개행)` — 번역 없이 읽고 판정용 본문만 LF 로 접는다.

    쌍인 `write_text_preserving_newline` 에 그 지배 개행을 그대로 넘기면 우리가 바꾸지 않은
    줄의 bytes 가 보존된다(왕복 무변경)."""
    raw = read_text_keeping_newlines(path)
    return raw.replace("\r\n", "\n"), dominant_newline(raw)


def write_text_preserving_newline(path: Path, text: str, newline: str) -> None:
    """LF 본문을 `newline` 표기로 되돌려 쓴다 (파일 부재 등 미해소 표기는 LF).

    `text` 는 LF 정규화 본문이어야 한다 — 삽입하는 새 블록도 같은 표기로 렌더돼 혼재가 생기지
    않는다(호출부가 블록을 LF 로 만들어 붙이고 여기서 한 번에 변환)."""
    newline = newline or DEFAULT_TEXT_NEWLINE
    write_text_keeping_newlines(
        path, text if newline == "\n" else text.replace("\n", newline))


# ── 계획-적용 사이 TOCTOU 창 ─────────────────────────────────────────────────
# 경로 안전은 `_is_safe_dest_path` 가 **계획 시점**에 판정한다(symlink·조상 symlink·`..` 거부).
# 그 판정과 실제 쓰기 사이에 대상 경로가 symlink 로 교체되면 판정이 무력해지고, 경로로 다시 여는
# 쓰기가 링크를 따라 **저장소 밖 파일**을 고친다. 그래서 아래 **여섯 채널**의 실 IO 는 경로가 아니라
# **fd** 로 한다 — dest 루트 fd 에서 각 컴포넌트를 `O_NOFOLLOW` 로 열고 마지막 컴포넌트도
# `O_NOFOLLOW` 로 연다. 열린 fd 는 그 시점 inode 에 묶이므로 이후 경로가 바뀌어도 우리가 읽고 쓰는
# 대상은 변하지 않는다(창을 좁히는 게 아니라 없앤다).
#   (1) 복사(`CopyAction.run` — 디렉토리 생성 포함) (2) 중앙 백업 (3) 공유 문서 재렌더
#   (4) placeholder 치환 (5) opencode 모델 해소 (6) 자유서술 fill(TODO 표시·토큰 판정).
#   원본(template)은 신뢰 소스라 경로로 읽는다.
#   ⚠ **여기 없는 dest 쓰기는 여전히 경로 기반이다** — engine.manifest 병합/guest 절 갱신 ·
#     `.gitignore` 보강 · local.conf 채널(백업·키 기록) · pm_playbook.local 스텁 생성 ·
#     board submodule scaffold · `--new` 최초 `mkdir`. 이들은 **정적 형상에서 안전**하다(계획 대상이
#     아니거나·인스턴스 소유라 계획-적용 창 밖이거나·존재하면 덮지 않는다). 그러나 **동적 교체 창
#     자체는 남는다** — 이 티켓의 범위가 계획-적용 창(위 6채널)이라 그렇게 한정했고, 확장은 별도
#     판단거리다(조용한 축소가 아니라 명시된 경계).
#   ⚠ dest 루트 **자체** 교체는 파일 단위가 아니라 전체 중단 클래스다(`DestRootSwappedError`) —
#     고정한 루트 신원(`dest_root_identity`)을 매 열기에서 fd 로 대조한다.
#   ⚠ 보장하는 축은 **symlink** 다 — 저장소 안에 미리 만들어 둔 **하드링크**는 같은 inode 라 fd 열기로
#     구분되지 않으므로 "저장소 밖 파일 불변" 문구는 symlink·경로 탈출 축에 한정된다(범위 밖).
_DEST_FD_WALK_SUPPORTED = (
    hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
    and os.open in getattr(os, "supports_dir_fd", frozenset())
)


# 거부로 승격하는 errno 와 **그 errno 가 실제로 말하는 것** — ENOTDIR 을 symlink 교체로 단정하지
# 않는다(일반 파일 컴포넌트·구조 변경일 수도 있다). 판정은 같아도(거부) 진단 문구는 갈라 쓴다.
_DEST_PATH_REFUSAL_REASONS = {
    getattr(errno, name): reason
    for name, reason in (
        ("ELOOP", "symlink 교체 의심"),
        ("EMLINK", "symlink 교체 의심(일부 BSD 의 O_NOFOLLOW errno)"),
        ("ENOTDIR", "경로 컴포넌트가 디렉토리가 아님 — 교체·구조 변경 의심"),
        ("ENXIO", "FIFO·디바이스 등 비-일반 파일 — 교체 의심"),
    )
    if hasattr(errno, name)
}


# 비-일반 파일(FIFO·디바이스)에서 **열기 자체가 멈추는** 창을 닫는 플래그. `O_NOFOLLOW` 는 symlink
# 만 거른다 — 계획 통과 뒤 대상이 FIFO 로 바뀌면 `O_RDONLY` 열기가 writer 를 기다리며 무기한 블록해
# 설치가 그 자리에 선다(교체 축의 남은 파생: 유출이 아니라 정지). 일반 파일에는 읽기·쓰기 의미가
# 없으므로(POSIX) 내용 IO 채널에 상시 얹고, 연 fd 를 `fstat` 해 일반 파일이 아니면 거부한다 —
# 검사 대상이 **연 fd 자신**이라 재검사 창이 없다. 쓰기 쪽은 열기가 ENXIO 로 먼저 실패한다(위 표).
_DEST_NONBLOCK_FLAG = getattr(os, "O_NONBLOCK", 0)


def _raise_dest_path_refusal(rel: Path, exc: OSError) -> NoReturn:
    """열기 거부로 볼 OSError 는 `UnsafeDestPathError` 로 승격해 던지고, 그 밖은 원본 재-raise.

    호출부는 승격된 예외만 "계획 뒤 교체" 로 다루고(해당 파일 제외 + loud), 나머지 OSError 는
    기존 처리(권한·부재 등)를 그대로 탄다 — 진단을 뭉개지 않는다."""
    reason = _DEST_PATH_REFUSAL_REASONS.get(exc.errno)
    if reason is None:
        raise exc
    raise UnsafeDestPathError(
        f"계획 검증 뒤 경로를 안전하게 열 수 없습니다({reason}): {Path(rel).as_posix()} — "
        "저장소 밖 파일을 고치지 않기 위해 이 파일을 건너뜁니다.") from exc


def dest_root_identity(dest_root: Path) -> tuple:
    """dest 루트를 **한 번** 해소해 그 디렉토리의 `(st_dev, st_ino)` 신원을 고정한다.

    계획 시점(복사 전)에 잡아 적용까지 넘긴다. 이후 매 IO 는 자기가 **연 fd** 를 `fstat` 해 이
    신원과 대조하므로(검사 대상 = 사용할 fd 자신), 계획 뒤 dest 루트 자체가 다른 디렉토리·저장소
    밖 symlink 로 교체돼도 그 트리를 따라가지 않고 거부한다 — 루트 재해소가 조용히 다른 inode 를
    고르던 창이 닫힌다.

    획득 실패는 **fail-loud** 다(옛 None 폴백 폐기): None 을 돌려주면 루트 검사가 통째로 꺼지므로,
    "획득 순간만 해소 불가로 만들고 그 뒤 교체" 라는 우회가 성립한다. 루트를 못 잡으면 무엇도
    안전하게 열 수 없으니 **적용 전에** 중단한다(호출부가 rc1 로 번역)."""
    try:
        st = os.stat(Path(dest_root).resolve(strict=True))
    except OSError as exc:
        raise UnsafeDestPathError(
            f"dest 루트 신원을 확정할 수 없습니다: {dest_root} ({exc}) — 루트 교체 검사를 끄고 "
            "진행하지 않고 적용 전에 중단합니다.") from exc
    if not stat.S_ISDIR(st.st_mode):
        raise UnsafeDestPathError(f"dest 루트가 디렉토리가 아닙니다: {dest_root}")
    return (st.st_dev, st.st_ino)


def _open_dest_root_fd(dest_root: Path, root_identity: tuple | None) -> int:
    """dest 루트를 열고 **그 fd 자신**으로 고정 신원을 대조한다 — 실패는 전부 루트 교체 클래스.

    루트 해소·열기 실패(삭제·깨진 링크·일반 파일로 교체)를 `UnsafeDestPathError` 나 raw `OSError`
    로 흘리면 **파일 단위** 핸들러가 그것을 흡수해 실행이 계속되고, 남은 단계가 교체된 트리에
    쓴다(루트 교체는 파일 하나를 빼는 상황이 아니라 대상 트리가 통째로 바뀐 상황이다). 그래서
    고정 신원이 있는 실행에서는 그 계열을 전부 `DestRootSwappedError`(즉시 전체 중단)로 번역한다.
    고정이 없는 호출(질의성 읽기 등)은 대조 기준이 없으므로 옛 경로 오류를 유지한다."""
    try:
        fd = os.open(str(Path(dest_root).resolve(strict=True)),
                     os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        if root_identity is not None:
            raise DestRootSwappedError(
                f"계획 검증 뒤 dest 루트를 열 수 없습니다: {dest_root} ({exc}) — 삭제·깨진 링크·"
                "비-디렉토리로 바뀐 형상이므로 즉시 전체 중단합니다.") from exc
        raise UnsafeDestPathError(f"dest 루트를 해소할 수 없습니다: {dest_root}") from exc
    if root_identity is None:
        return fd
    try:
        # 검사 대상이 **지금 연 fd 자신**이라 검사-사용 사이 창이 없다(경로 재검사와 다른 점).
        root_stat = os.fstat(fd)
        if (root_stat.st_dev, root_stat.st_ino) != root_identity:
            raise DestRootSwappedError(
                f"계획 검증 뒤 dest 루트가 다른 디렉토리로 바뀌었습니다: {dest_root} — "
                "저장소 밖 트리를 건드리지 않기 위해 즉시 중단합니다.")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_dest_dir_nofollow(dest_root: Path, rel_dir: Path,
                            root_identity: tuple | None = None) -> int:
    """dest 하위 디렉토리를 **컴포넌트마다 symlink 를 거부하며** 열어 dir fd 를 반환한다.

    빈 rel(= dest 루트 자신)은 루트를 직접 연다. "부모 디렉토리 fd 를 잡고 그 안에서만 조작"
    해야 하는 채널(링크 자체 백업·링크 삭제)이 쓴다 — 그쪽은 내용 IO 가 아니라 디렉토리 엔트리
    조작이라 경로로 다시 열면 부모 교체 창이 그대로 남는다."""
    parts = Path(rel_dir).parts
    if not parts:
        return _open_dest_root_fd(dest_root, root_identity)
    return _open_dest_relative_nofollow(
        dest_root, Path(*parts), os.O_RDONLY | os.O_DIRECTORY,
        root_identity=root_identity)


def _open_dest_relative_nofollow(
        dest_root: Path, rel: Path, flags: int, mode: int = 0o666,
        create_parents: bool = False, root_identity: tuple | None = None,
        create_leaf_dir: bool = False, regular_only: bool = False) -> int:
    """`dest_root/rel` 을 **컴포넌트마다 symlink 를 거부하며** 열어 fd 를 반환한다.

    `create_parents` 는 부재 중간 디렉토리를 dir_fd 상대로 만든다(백업 target 용) — 경로 문자열
    `mkdir(parents=True)` 는 조상 symlink 를 따라가 저장소 밖에 쓰기 때문이다.

    `root_identity`(= `dest_root_identity()` 산출)를 주면 **루트 자체 교체**도 거부한다. 컴포넌트
    순회는 rel 안의 교체만 막으므로, 루트가 통째로 바뀌면 안전한 순회가 엉뚱한 트리에서 일어난다.

    `regular_only` 는 **파일 내용 IO 채널** 전용이다: `O_NONBLOCK` 을 얹어 FIFO·디바이스에서 열기가
    무기한 멈추는 것을 막고, 연 fd 를 `fstat` 해 일반 파일이 아니면 거부한다(디렉토리를 여는 호출은
    이 옵션을 쓰지 않는다). 판정 대상이 fd 자신이라 경로 재검사와 달리 검사-사용 창이 없다.

    dir_fd/`O_NOFOLLOW` 미지원 플랫폼(주로 Windows)은 **열기 직전 `_is_safe_dest_path` 재검사**로
    폴백한다 — 창을 syscall 하나로 좁히지만 구조적 폐쇄는 아니다. 그 플랫폼은 symlink 생성 자체가
    관리자/개발자 모드 권한이라 노출면이 다르며, 폴백임을 여기 명시해 둔다."""
    rel = Path(rel)
    parts = rel.parts
    # 절대경로·드라이브·anchor 는 `dir_fd` 를 **무시**하고 그대로 열린다
    #   (`os.open("/etc/hostname", dir_fd=…)` 실측) — 컴포넌트 순회를 통째로 우회하므로 입구에서
    #   거부한다. 현 호출부는 relpath 만 넘기지만 containment 는 호출부 규율이 아니라 여기서
    #   성립해야 한다. `drive`/`root` 는 Windows `C:x`·UNC·POSIX anchor 까지 한 식으로 덮는다.
    if (rel.is_absolute() or rel.drive or rel.root or not parts
            or any(part in ("", ".", "..") for part in parts)):
        raise UnsafeDestPathError(f"dest 상대경로가 안전하지 않습니다: {rel.as_posix()}")
    binary = getattr(os, "O_BINARY", 0)  # Windows 텍스트 모드 줄끝 번역 차단(줄끝 보존).
    if regular_only:
        flags |= _DEST_NONBLOCK_FLAG

    def _regular_or_refuse(fd: int) -> int:
        """연 fd 가 일반 파일인가 — 아니면 fd 를 닫고 거부한다(내용 IO 채널 공통 출구)."""
        if not regular_only:
            return fd
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise UnsafeDestPathError(
                    f"경로가 일반 파일이 아닙니다(FIFO·디바이스·교체 의심): {rel.as_posix()}")
        except BaseException:
            os.close(fd)
            raise
        return fd

    if not _DEST_FD_WALK_SUPPORTED:
        if not _is_safe_dest_path(dest_root, rel):
            raise UnsafeDestPathError(
                f"경로가 안전하지 않습니다(symlink·조상 symlink·repo 밖): {rel.as_posix()}")
        # **루트 관문은 고정 신원 유무와 무관하다.** fd 순회 플랫폼은 `_open_dest_root_fd` 가 매
        #   호출 루트를 strict 해소하므로(부재·깨진 링크·비-디렉토리 = `UnsafeDestPathError`),
        #   이 폴백만 관문을 건너뛰면 같은 상황에서 raw `FileNotFoundError` 가 새 나가 호출부의
        #   흡수 클래스가 플랫폼마다 갈린다(Windows 실측·`_is_safe_dest_path` 는 부재 경로를
        #   lexical 로 통과시킨다). 신원이 있으면 그 계열을 루트 교체 클래스로 승격하고, 없으면
        #   옛 경로 오류 클래스를 그대로 유지한다(질의성 호출 규약).
        try:
            current_identity = dest_root_identity(dest_root)
        except UnsafeDestPathError as exc:  # 루트 자체 이상.
            if root_identity is None:
                raise
            raise DestRootSwappedError(str(exc)) from exc
        if root_identity is not None and current_identity != root_identity:
            raise DestRootSwappedError(
                f"계획 검증 뒤 dest 루트가 다른 디렉토리로 바뀌었습니다: {dest_root}")
        target = Path(dest_root) / rel
        if create_parents:
            target.parent.mkdir(parents=True, exist_ok=True)
        return _regular_or_refuse(os.open(str(target), flags | binary, mode))
    # 루트 열기 + 신원 대조는 한 지점(`_open_dest_root_fd`)이 전담한다 — 해소 실패·비-디렉토리·
    #   불일치를 전부 **전체 중단 클래스**로 번역해, 파일 단위 핸들러가 루트 교체를 흡수하는 창을
    #   남기지 않는다.
    dir_flags = os.O_RDONLY | os.O_DIRECTORY
    current = _open_dest_root_fd(dest_root, root_identity)
    try:
        for part in parts[:-1]:
            try:
                nxt = os.open(part, dir_flags | os.O_NOFOLLOW, dir_fd=current)
            except OSError as exc:
                if not (create_parents and isinstance(exc, FileNotFoundError)):
                    _raise_dest_path_refusal(rel, exc)
                try:
                    os.mkdir(part, dir_fd=current)
                except FileExistsError:
                    pass  # 경쟁 생성 — 아래 재-open 이 판정한다(symlink 로 선점됐으면 거부).
                try:
                    nxt = os.open(part, dir_flags | os.O_NOFOLLOW, dir_fd=current)
                except OSError as retry_exc:
                    # 부모 생성 경쟁도 같은 정규화를 탄다 — raw OSError 로 새 나가지 않는다.
                    _raise_dest_path_refusal(rel, retry_exc)
            os.close(current)
            current = nxt
        try:
            return _regular_or_refuse(
                os.open(parts[-1], flags | os.O_NOFOLLOW | binary, mode, dir_fd=current))
        except OSError as exc:
            if not (create_leaf_dir and isinstance(exc, FileNotFoundError)):
                _raise_dest_path_refusal(rel, exc)
            try:
                os.mkdir(parts[-1], dir_fd=current)
            except FileExistsError:
                pass  # 경쟁 생성 — 아래 재-open 이 판정한다(symlink 로 선점됐으면 거부).
            try:
                return _regular_or_refuse(os.open(
                    parts[-1], flags | os.O_NOFOLLOW | binary, mode, dir_fd=current))
            except OSError as retry_exc:
                _raise_dest_path_refusal(rel, retry_exc)
    finally:
        os.close(current)


def _fdopen_text(fd: int, mode: str, newline: str | None = ""):
    """fd 소유권을 넘긴 텍스트 핸들 — 실패 시 fd 를 흘리지 않는다.

    `newline=""` = 줄끝 미번역(재렌더 채널·왕복 byte 보존), `newline=None` = 읽기 전용
    universal newline(`Path.read_text` 와 동일 의미)."""
    try:
        return os.fdopen(fd, mode, encoding="utf-8", newline=newline)
    except BaseException:
        os.close(fd)
        raise


def _fdopen_binary(fd: int, mode: str):
    """fd 소유권을 넘긴 바이너리 핸들 — 실패 시 fd 를 흘리지 않는다(백업 스트리밍용)."""
    try:
        return os.fdopen(fd, mode)
    except BaseException:
        os.close(fd)
        raise


def read_dest_text_keeping_newlines(
        dest_root: Path, rel: Path, root_identity: tuple | None = None) -> str:
    """`read_text_keeping_newlines` 의 TOCTOU 안전 짝 — symlink 미추종 fd 로 읽는다."""
    with _fdopen_text(
            _open_dest_relative_nofollow(
                dest_root, rel, os.O_RDONLY, root_identity=root_identity,
                regular_only=True), "r") as handle:
        return handle.read()


def write_dest_text_keeping_newlines(
        dest_root: Path, rel: Path, text: str,
        root_identity: tuple | None = None) -> None:
    """`write_text_keeping_newlines` 의 TOCTOU 안전 짝 — symlink 미추종 fd 로 제자리 덮어쓴다.

    `O_CREAT` 를 주지 않는다: 제자리 편집 전용이라 대상이 사라졌으면(경쟁 삭제) 만들지 않고
    실패해야 한다."""
    with _fdopen_text(
            _open_dest_relative_nofollow(
                dest_root, rel, os.O_WRONLY | os.O_TRUNC,
                root_identity=root_identity, regular_only=True), "w", newline="") as handle:
        handle.write(text)


def read_dest_text(dest_root: Path, rel: Path, root_identity: tuple | None = None) -> str:
    """`Path.read_text(encoding="utf-8")` 의 TOCTOU 안전 짝 — symlink 미추종 fd + universal newline.

    복사분 채널(치환·모델 해소·fill)용이다. 줄끝 의미를 옛 경로 IO 와 **동일하게** 두고 여는
    방식만 바꾼다 — 여기서 줄끝 보존으로 바꾸면 `_mark_todos` 의 줄 단위 마커 삽입이 CRLF 를
    깨뜨린다(의도치 않은 동작 변경)."""
    with _fdopen_text(
            _open_dest_relative_nofollow(
                dest_root, rel, os.O_RDONLY, root_identity=root_identity,
                regular_only=True),
            "r", newline=None) as handle:
        return handle.read()


def write_dest_text(dest_root: Path, rel: Path, text: str,
                    root_identity: tuple | None = None) -> None:
    """LF 고정 텍스트 쓰기의 TOCTOU 안전 짝 — 제자리 덮어쓰기 전용(생성 안 함)."""
    with _fdopen_text(
            _open_dest_relative_nofollow(
                dest_root, rel, os.O_WRONLY | os.O_TRUNC, root_identity=root_identity,
                regular_only=True),
            "w", newline="\n") as handle:
        handle.write(text)


def _ensure_dest_dir_nofollow(dest_root: Path, rel_dir: Path,
                              root_identity: tuple | None = None) -> None:
    """`dest_root/rel_dir` 디렉토리 체인을 **컴포넌트별 `O_NOFOLLOW` 로** 만든다(경로 mkdir 금지).

    `mkdir(parents=True)` 는 조상 symlink 를 따라가 저장소 밖에 디렉토리를 만든다 — 복사 목적지와
    백업 자리 양쪽에서 그게 첫 외부 쓰기였다. 빈 rel(=dest 루트)은 무동작."""
    parts = Path(rel_dir).parts
    if not parts:
        return
    rel_path = Path(*parts)
    if not _DEST_FD_WALK_SUPPORTED:
        # 폴백(dir_fd 미지원·주로 Windows): 만들기 직전 재검사 — 창을 좁힐 뿐 구조적 폐쇄가 아니다.
        if not _is_safe_dest_path(dest_root, rel_path):
            raise UnsafeDestPathError(
                f"디렉토리 경로가 안전하지 않습니다(symlink·조상 symlink·repo 밖): "
                f"{rel_path.as_posix()}")
        assert_dest_root_unchanged(dest_root, root_identity)
        (Path(dest_root) / rel_path).mkdir(parents=True, exist_ok=True)
        return
    fd = _open_dest_relative_nofollow(
        dest_root, rel_path, os.O_RDONLY | os.O_DIRECTORY,
        create_parents=True, root_identity=root_identity, create_leaf_dir=True)
    os.close(fd)


def _stream_fd_into(src_fd: int, dst_handle) -> None:
    """열린 fd 의 내용을 처음부터 스트리밍해 핸들에 쓴다(경로 재열기 0·메모리 적재 0)."""
    os.lseek(src_fd, 0, os.SEEK_SET)
    chunk_size = 1 << 20
    while True:
        data = os.read(src_fd, chunk_size)
        if not data:
            return
        dst_handle.write(data)


def _dest_leaf_identity(dest_root: Path, rel: Path,
                        root_identity: tuple | None = None) -> tuple:
    """dest leaf 의 `(st_dev, st_ino, 파일 종류)` — **부모 fd 안 `lstat`** 이라 링크도 링크 자신을 잰다.

    링크는 fd 로 붙들 수 없다(열면 대상이 열린다). 그래서 백업 시점 신원을 재 두고 삭제 직전에
    재대조한다 — 그 사이 같은 자리가 다른 파일로 바뀌면 백업 없는 남의 파일을 지우게 되므로.

    **파일 종류(`S_IFMT`)를 신원에 포함**한다: 지우고 곧바로 만들면 커널이 같은 inode 번호를
    재사용할 수 있어 `(dev, ino)` 만으로는 교체를 못 가른다(실측). 종류가 바뀌는 교체(링크→일반
    파일 등)는 이걸로 확실히 걸린다. 같은 종류로 교체 + inode 재사용은 남는 잔여인데, 그 경우
    지워지는 것도 링크뿐이라 사용자 데이터 손실 클래스가 아니다(명시된 경계)."""
    rel = Path(rel)
    if not _DEST_FD_WALK_SUPPORTED:
        st = os.lstat(Path(dest_root) / rel)  # 폴백 — 창을 좁힐 뿐(다른 폴백과 동형).
        return (st.st_dev, st.st_ino, stat.S_IFMT(st.st_mode))
    dir_fd = _open_dest_dir_nofollow(dest_root, rel.parent, root_identity=root_identity)
    try:
        st = os.lstat(rel.name, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)
    return (st.st_dev, st.st_ino, stat.S_IFMT(st.st_mode))


def _unlink_dest_leaf_if_unchanged(dest_root: Path, rel: Path, leaf_identity: tuple | None,
                                   root_identity: tuple | None = None) -> None:
    """백업한 그 leaf 가 맞을 때만 지운다 — 신원이 다르면 파일 단위 제외로 올린다."""
    if leaf_identity is not None:
        current = _dest_leaf_identity(dest_root, rel, root_identity=root_identity)
        if current != leaf_identity:
            raise PlanStateChangedError(
                f"백업 뒤 대상이 다른 파일로 바뀌었습니다({Path(rel).as_posix()}) — 백업하지 "
                "않은 파일을 지우지 않기 위해 이 파일을 건너뜁니다.")
    _unlink_dest_relative_nofollow(dest_root, rel, root_identity=root_identity)


def _refresh_existing_dest_file(
        dest_root: Path, rel: Path, src: Path, *, backup_base_rel: Path | None,
        root_identity: tuple | None = None) -> None:
    """기존 일반 파일을 **fd 하나로** 백업하고 그 자리에서 덮어쓴다(백업↔쓰기 창 폐쇄).

    옛 흐름은 백업과 쓰기가 leaf 를 각각 다시 열어, 그 사이 같은 자리가 다른 파일로 교체되면
    **백업 없는 새 파일을 truncate** 했다. 여기서는 `O_RDWR` fd 하나를 계획이 본 inode 에 묶어
    백업 읽기와 덮어쓰기를 모두 처리하므로 그 창이 존재하지 않는다(재검사가 아니라 구조적 폐쇄).

    소스는 **dest 를 건드리기 전에** 연다 — 소스 열기가 실패하면 dest 는 자르지도 않은 원본 그대로다.
    dest 경쟁(ENOENT)은 `PlanStateChangedError` 로 정규화한다(파일 단위 제외)."""
    with fs.open_shared(src, binary=True) as src_handle:  # 소스 먼저 — 실패 시 dest 불변.
        src_stat = os.fstat(src_handle.fileno())
        try:
            leaf_fd = _open_dest_relative_nofollow(
                dest_root, rel, os.O_RDWR, root_identity=root_identity, regular_only=True)
        except (FileExistsError, FileNotFoundError) as exc:
            raise PlanStateChangedError(
                f"계획 뒤 대상 상태가 달라져 복사를 멈춥니다({Path(rel).as_posix()}: {exc}) — "
                "계획에 없던 처리를 하지 않기 위해 이 파일을 건너뜁니다.") from exc
        try:
            if backup_base_rel is not None:
                _backup_open_fd_nofollow(
                    dest_root, leaf_fd, backup_base_rel, root_identity=root_identity)
            os.ftruncate(leaf_fd, 0)
            os.lseek(leaf_fd, 0, os.SEEK_SET)
            while True:
                data = src_handle.read(1 << 20)
                if not data:
                    break
                os.write(leaf_fd, data)
            if hasattr(os, "fchmod"):
                os.fchmod(leaf_fd, stat.S_IMODE(src_stat.st_mode))
            if os.utime in getattr(os, "supports_fd", frozenset()):
                os.utime(leaf_fd, ns=(src_stat.st_atime_ns, src_stat.st_mtime_ns))
            # 쓰기 자체는 fd(=계획이 본 inode)로 안전하게 끝났다. 다만 그 사이 **경로**가 다른
            #   파일로 바뀌었다면 우리가 쓴 내용은 그 자리에 보이지 않는다 — 성공으로 보고하면
            #   사람이 없는 결과를 믿는다. 손상은 0이지만 제외로 올려 요약에 싣는다.
            fd_stat = os.fstat(leaf_fd)
            current = _dest_leaf_identity(dest_root, rel, root_identity=root_identity)
            if current != (fd_stat.st_dev, fd_stat.st_ino, stat.S_IFMT(fd_stat.st_mode)):
                raise PlanStateChangedError(
                    f"쓰기 중 대상 경로가 다른 파일로 바뀌었습니다({Path(rel).as_posix()}) — "
                    "저장한 내용이 그 자리에 보이지 않으므로 이 파일을 제외로 보고합니다"
                    "(교체된 파일은 건드리지 않았습니다).")
        finally:
            os.close(leaf_fd)


def _write_dest_file_from_source_nofollow(
        dest_root: Path, rel: Path, src: Path, *, overwrite: bool,
        root_identity: tuple | None = None) -> None:
    """신뢰 소스 `src` 를 `dest_root/rel` 로 **fd 스트리밍** 복사한다(모드·타임스탬프 보존).

    `overwrite=False`(신규)는 `O_CREAT|O_EXCL` 로 만들어 계획 뒤 생긴 남의 파일을 덮지 않고,
    `True`(refresh)는 `O_WRONLY|O_TRUNC` 로 기존 파일만 덮는다(기존 일반 파일 갱신은 백업까지 한
    fd 로 묶는 `_refresh_existing_dest_file` 이 쓰므로, 이 경로의 `True` 는 직접 호출·테스트용이다).
    어느 쪽이든 `O_NOFOLLOW` 라 그 자리에 심어진 symlink 는 거부된다. `shutil.copy2` 가 보존하던
    실행 비트(출하 `.sh`)와 mtime 은 fd 기반으로 유지한다.

    **소스를 먼저 연다** — dest 를 `O_TRUNC` 로 연 뒤 소스 열기가 실패하면 dest 는 비워진 채 남고
    그 fd 는 주인이 없다(누수). 소스가 읽히는 것을 확인한 뒤에만 dest 를 건드린다.

    두 플래그 조합이 **계획 뒤 dest 경쟁**을 그대로 표면화한다: 신규인데 그 사이 생겼으면 EEXIST,
    기존인데 그 사이 지워졌으면 ENOENT 다. 둘 다 `PlanStateChangedError`(파일 단위 제외)로
    정규화한다 — 그대로 새면 적용 루프가 통째로 죽어 rc 정책이 깨진다. **소스(template) 쪽**
    오류는 정규화하지 않는다(진짜 누락 신호라 그대로 올린다·`src.open` 은 try 밖)."""
    flags = os.O_WRONLY | (os.O_TRUNC if overwrite else os.O_CREAT | os.O_EXCL)
    with fs.open_shared(src, binary=True) as src_handle:  # 소스 먼저 — 실패 시 dest 미접촉·fd 누수 0.
        src_stat = os.fstat(src_handle.fileno())
        try:
            # 디렉토리 체인 생성도 dest 조작이라 같은 정규화 안에 둔다 — 부모가 그 사이 생겼다
            #   사라지면 재-open 이 ENOENT 로 새어 적용 루프를 죽인다(인접 창).
            _ensure_dest_dir_nofollow(dest_root, Path(rel).parent, root_identity=root_identity)
            dst_fd = _open_dest_relative_nofollow(
                dest_root, rel, flags, stat.S_IMODE(src_stat.st_mode),
                root_identity=root_identity, regular_only=True)
        except (FileExistsError, FileNotFoundError) as exc:
            raise PlanStateChangedError(
                f"계획 뒤 대상 상태가 달라져 복사를 멈춥니다({Path(rel).as_posix()}: {exc}) — "
                "계획에 없던 처리를 하지 않기 위해 이 파일을 건너뜁니다.") from exc
        with _fdopen_binary(dst_fd, "wb") as dst_handle:
            shutil.copyfileobj(src_handle, dst_handle)
            dst_handle.flush()
            if hasattr(os, "fchmod"):
                os.fchmod(dst_handle.fileno(), stat.S_IMODE(src_stat.st_mode))
            if os.utime in getattr(os, "supports_fd", frozenset()):
                os.utime(dst_handle.fileno(),
                         ns=(src_stat.st_atime_ns, src_stat.st_mtime_ns))


class CopyApplyOutcome(NamedTuple):
    """복사 plan 적용 결과 — 실제로 쓴 것과 **건드리지 않은 것**을 사유별로 가른다."""

    copied: list[Path]    # 실제로 복사된 dest relpath(후속 채널의 범위 = 이것뿐).
    refused: list[str]    # 계획 뒤 경로 교체(symlink·비-디렉토리 컴포넌트).
    changed: list[str]    # 계획 뒤 대상 상태 변화(신규 생성·삭제).


def apply_copy_plan(plan: list, dest_root: Path,
                    root_identity: tuple | None = None) -> CopyApplyOutcome:
    """복사 plan 을 적용하고 **파일 단위 제외**를 사유별로 돌려준다.

    적용 단계의 파일 단위 위반(경로 교체·계획 뒤 상태 변화)은 그 파일만 빼고 계속한다 — 모듈
    docstring 이 명문화한 rc 정책(적용 단계 제외 = rc 0 + stderr 요약)과 같은 규칙이다. 여기서
    전체를 중단하면 이미 복사된 파일이 남아 오히려 부분 설치가 된다.

    반환 `copied` 는 **성공한 액션만** 담는다 — 후속 채널(치환·모델 해소·fill·설치 기록)의 범위가
    곧 이 집합이라, 제외된 파일이 섞이면 없는 파일을 처리 대상으로 세거나 교체된 경로를 범위로
    들인다. dest 루트 교체(`DestRootSwappedError`)만 예외로 그대로 올라간다(전체 중단 클래스)."""
    copied: list[Path] = []
    refused: list[str] = []
    changed: list[str] = []
    for action in plan:
        rel = action.dst.relative_to(dest_root)
        try:
            action.run(root_identity=root_identity)
        except UnsafeDestPathError:
            refused.append(rel.as_posix())
            continue
        except PlanStateChangedError:
            changed.append(rel.as_posix())
            continue
        copied.append(rel)
    return CopyApplyOutcome(copied, refused, changed)


def report_copy_apply_anomalies(outcome: CopyApplyOutcome) -> None:
    """복사 단계 파일 단위 제외를 loud 로 알린다(조용한 degrade 금지)."""
    if outcome.refused:
        print(f"  ⚠️ 복사 대상 {len(outcome.refused)}건: 계획 검증 뒤 경로를 안전하게 열 수 없어"
              f"(symlink 교체·비-디렉토리 컴포넌트) 복사를 건너뜁니다(저장소 밖 파일 불변): "
              f"{', '.join(outcome.refused)}", file=sys.stderr)
    if outcome.changed:
        print(f"  ⚠️ 복사 대상 {len(outcome.changed)}건: 계획 뒤 대상 상태가 달라져(그 사이 생성·"
              f"삭제) 복사를 건너뜁니다(무백업 덮기·삭제분 재생성 금지): "
              f"{', '.join(outcome.changed)}", file=sys.stderr)


def _copied_scope_anomaly(dest_root: Path, rel: Path) -> str | None:
    """복사분 채널의 선검사 탈락 사유 — `'vanished'`·`'swapped'`·`None`(정상 대상).

    이 채널들의 범위는 **이번 run 이 방금 복사한 파일**이라, 대상이 없다는 것은 "원래 대상이 아님"
    이 아니라 복사 뒤 삭제다. 조용히 건너뛰면 그 사실이 어디에도 안 남는다(적용 단계의 유일한
    신호가 stderr 요약이므로). 부재는 `vanished`, 실재하는데 편집 대상이 아니면(디렉토리·FIFO 등
    형상 변화) `swapped` 로 갈라 호출부가 같은 요약 채널에 싣는다."""
    if _is_inplace_edit_candidate(dest_root, rel):
        return None
    return "swapped" if os.path.lexists(Path(dest_root) / rel) else "vanished"


def _report_copied_scope_anomalies(
        channel: str, swapped: list[str], vanished: list[str]) -> None:
    """복사분 채널의 파일 단위 제외를 loud 로 알린다(조용한 degrade 금지·채널명 포함).

    루트 교체(`DestRootSwappedError`)는 여기 오지 않는다 — 그건 흡수 대상이 아니라 전체 중단이다."""
    if swapped:
        print(f"  ⚠️ {channel} 대상 {len(swapped)}건: 복사 뒤 경로를 안전하게 열 수 없어(symlink "
              f"교체·비-디렉토리 컴포넌트) 처리를 건너뜁니다(저장소 밖 파일 불변): "
              f"{', '.join(swapped)}", file=sys.stderr)
    if vanished:
        print(f"  ⚠️ {channel} 대상 {len(vanished)}건: 복사 뒤 대상이 사라져(경쟁 삭제) 처리를 "
              f"건너뜁니다(새로 만들지 않습니다): {', '.join(vanished)}", file=sys.stderr)


def assert_dest_root_unchanged(dest_root: Path, root_identity: tuple | None) -> None:
    """고정한 루트 신원을 **외부 단계 직전에** 재확인한다(board init·실 하니스 fill 등).

    그 단계들은 subprocess/자식 프로세스라 우리 fd 를 물려줄 수 없다 — 구조적 폐쇄가 불가능한
    구간이므로 직전 재확인으로 창을 좁힌다(검사-사용 사이 gap 은 남는다·명시). 불일치는 파일
    단위가 아니라 **전체 중단** 클래스다."""
    if root_identity is None:
        return
    try:
        current = dest_root_identity(dest_root)
    except UnsafeDestPathError as exc:
        raise DestRootSwappedError(str(exc)) from exc
    if current != root_identity:
        raise DestRootSwappedError(
            f"dest 루트가 실행 중에 다른 디렉토리로 바뀌었습니다: {dest_root} — 남은 단계가 "
            "교체된 트리에 쓰지 않도록 즉시 중단합니다.")


def _is_inplace_edit_candidate(dest_root: Path, rel: Path) -> bool:
    """제자리 편집(백업·재렌더·복사분 채널) 선검사 — **링크를 따라가지 않는다**(`lstat`).

    옛 `is_file()` 선검사는 링크를 따라가므로 **깨진 symlink** 로 교체된 대상을 False 로 보고
    조용히 건너뛰었다 — 요구된 "파일 단위 loud 제외" 가 그 경로에서만 침묵으로 바뀐다. 그래서
    여기서는 symlink 를 **후보로 통과**시키고, 실제 판정(거부 + loud)은 fd 가드 한 지점에 맡긴다.

    `lstat` 실패도 errno 로 가른다 — 삼키면 같은 침묵이 조상 축으로 되살아난다:
      - `ENOENT` + **조상에 symlink 없음** = 진짜 부재 → 조용한 제외(원래 대상이 아니다).
      - `ENOENT` + **조상이 symlink**(깨진 링크로 교체) 또는 `ELOOP`/`ENOTDIR` 류 = 안전 거부
        대상 → 후보로 통과시켜 fd 가드가 loud 로 거른다.
    조상 순회는 dest 루트 **하위**로 한정한다(루트 자신이 symlink 를 거쳐 있는 정상 형상 —
    예 `/tmp`→`/private/tmp` — 을 매 부재 파일마다 거부로 오판하지 않기 위해)."""
    try:
        st = os.lstat(Path(dest_root) / rel)
    except OSError as exc:
        if exc.errno in _DEST_PATH_REFUSAL_REASONS:
            return True
        return isinstance(exc, FileNotFoundError) and _has_symlink_ancestor(dest_root, rel)
    return stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)


def _has_symlink_ancestor(dest_root: Path, rel: Path) -> bool:
    """`rel` 의 조상 컴포넌트(dest 루트 하위)에 symlink 가 있는가 — 부재 사유 판별용."""
    current = Path(dest_root)
    for part in Path(rel).parts[:-1]:
        current = current / part
        try:
            if stat.S_ISLNK(os.lstat(current).st_mode):
                return True
        except OSError:
            return False  # 조상 자체가 부재 — 진짜 부재 경로다.
    return False


def _is_notation_fallback_scope(rel_posix: str) -> bool:
    """manifest 소유자가 없을 때 **설치 하네스 전체 표기**로 폴백해도 되는 relpath 인가.

    폴백 대상 = 인스턴스 wiki 하위 전부(설치 하네스 전원이 같은 물리 문서를 읽는 표면).
    엔진 backbone(`tools/**`)·어댑터 네임스페이스·루트 진입 문서는 독자가 특정 하네스라
    여기 들어오지 않는다(전체 집합으로 렌더하면 오히려 오표기가 된다)."""
    return rel_posix.startswith(install_rules.INSTANCE_SHARED_WIKI_PREFIX)


def _is_safe_dest_path(dest_root: Path, rel: Path) -> bool:
    """`dest_root/rel` 이 dest 루트 하위이고 rel 경로·조상에 symlink 가 없는가.

    조작된 경로가 링크 follow·`..` 탈출로 repo 밖을 순회/치환하는 것을 막는다(순회·처리 양쪽):
      - `..`/빈 컴포넌트 거부.
      - rel 각 컴포넌트(`dest_root/…`)가 symlink 면 거부(**symlink 및 symlink 조상**).
      - 최종 resolve 후 dest_root 하위 containment(밖이면 거부).
    위반 시 False → 호출부가 skip(비파괴)."""
    try:
        droot = dest_root.resolve()
    except OSError:
        return False
    cur = dest_root
    for part in rel.parts:
        if part in ("..", "", "."):
            return False
        cur = cur / part
        try:
            if cur.is_symlink():
                return False
        except OSError:
            return False
    try:
        cur.resolve().relative_to(droot)
    except (ValueError, OSError):
        return False
    return True


def _byte_identical_skipped(
        template_root: Path, dest_root: Path, copied_relpaths: set,
        adapter_dirs: tuple, root_identity: tuple | None = None) -> set:
    """이번 하네스 template 과 **byte-identical 이라 복사만 생략된** dest 파일 relpath 집합
    .

    이게 token-form 미렌더 잔존 문제의 **유일한 대상**이다 — 타 guest·adopter 자체 생성 파일(내용 상이·
    copied)
    조건: (a) 이 하네스 adapter namespace(`adapter_dirs`) 안, (b) 미-copied(copy plan 이 안 실음),
    (c) dest 실존 + template 과 byte-identical, (d) 경로 안전(`_is_safe_dest_path`). 경로는
    **template**(신뢰)에서 오고 manifest(조작 가능)에서 오지 않는다."""
    out: set = set()
    for rel, src in install_source._iter_source_files(template_root, "full"):
        rel_posix = rel.as_posix()
        if not any(rel_posix == d.rstrip("/") or rel_posix.startswith(d.rstrip("/") + "/")
                   for d in adapter_dirs):
            continue  # 이 하네스 namespace 밖.
        if rel in copied_relpaths:
            continue  # 이미 copy plan 이 실음 → 렌더 대상(중복 방지).
        if not _is_safe_dest_path(dest_root, rel):
            continue  # 조작 경로·symlink → skip(repo 밖 순회/치환 방지).
        # 비교는 **nofollow 로 한 번 연 fd** 에서 한다 — lstat 으로 걸러도 그 뒤 `_same_bytes` 가
        #   경로를 *다시 열면* 그 사이 교체된 링크의 대상을 읽어 byte-identical 로 보고, 그 경로가
        #   치환·렌더 대상 집합에 들어간다(경로 기반 소비처가 저장소 밖을 고칠 입구). 재열기 제거.
        try:
            # `regular_only` 가 일반 파일 판정(FIFO·디바이스 거부)까지 연 fd 에서 처리한다 —
            #   거부는 아래 UnsafeDestPathError 핸들러가 범위 밖으로 흘린다(옛 별도 S_ISREG 검사와
            #   같은 결과·판정 지점 하나).
            with _fdopen_binary(
                    _open_dest_relative_nofollow(
                        dest_root, rel, os.O_RDONLY,
                        root_identity=root_identity, regular_only=True), "rb") as dst_handle:
                if _same_bytes_fd(src, dst_handle):
                    out.add(rel)
        except UnsafeDestPathError:
            continue  # 교체된 경로는 범위 밖(위 `_is_safe_dest_path` 거부와 같은 결과).
        except OSError:
            continue
    return out


def _same_bytes_fd(src: Path, dst_handle) -> bool:
    """template 원본과 **이미 연 dest 핸들**의 내용이 같은가(dest 재열기 없음·스트리밍 비교)."""
    chunk_size = 1 << 20
    try:
        with fs.open_shared(src, binary=True) as src_handle:
            while True:
                src_chunk = src_handle.read(chunk_size)
                dst_chunk = dst_handle.read(chunk_size)
                if src_chunk != dst_chunk:
                    return False
                if not src_chunk:
                    return True
    except OSError:
        return False


def _copy_dest_file_nofollow(
        dest_root: Path, src_rel: Path, target_base_rel: Path,
        root_identity: tuple | None = None) -> Path:
    """dest 안 파일을 dest 안 다른 경로로 **fd↔fd 스트리밍** 복사하고 실제 경로 반환.

    옛 `shutil.copy2(src, target, follow_symlinks=False)` 는 두 창을 열어 뒀다:
      - 원본 경로가 검증 뒤 symlink 로 바뀌면 **링크 자체**를 백업해 백업이 원본 내용을 안
        담는다(그 상태로 재렌더가 진행되면 복원 불가).
      - target 의 조상이 symlink 로 바뀌면 `mkdir(parents=True, exist_ok=True)` 가 링크를 따라가
        **저장소 밖에 사용자 파일 내용을 쓴다**(외부 쓰기·내용 유출).
    양쪽 다 fd 열기로 닫는다. 원본이 일반 파일이 아니면(교체 의심) 거부한다. target 이 점유돼
    있으면 `_free_backup_path` 순번으로 비켜 간다(원본 보존 우선). `copy2` 가 보존하던 모드·
    타임스탬프는 fd 기반(`fchmod`/`utime`)으로 유지하고, 내용은 스트리밍이라 메모리 적재가 없다."""
    # 원본이 일반 파일이 아니면(symlink 교체 뒤 FIFO·디바이스 포함) `regular_only` 가 연 fd 에서
    #   거부한다. 연 fd 를 그대로 백업 함수에 넘긴다 — 열기와 읽기가 같은 inode 다.
    src_fd = _open_dest_relative_nofollow(
        dest_root, src_rel, os.O_RDONLY, root_identity=root_identity, regular_only=True)
    try:
        return _backup_open_fd_nofollow(
            dest_root, src_fd, target_base_rel, root_identity=root_identity,
            label=Path(src_rel).as_posix())
    finally:
        os.close(src_fd)


def _backup_open_fd_nofollow(
        dest_root: Path, src_fd: int, target_base_rel: Path,
        root_identity: tuple | None = None, label: str = "") -> Path:
    """**이미 연 dest fd** 의 내용을 dest 안 백업 자리로 스트리밍 복사하고 실제 경로 반환.

    호출부가 fd 를 쥔 채 넘기므로 백업 대상은 그 fd 의 inode 로 고정된다 — 백업과 그 뒤의 덮어쓰기가
    **같은 파일**임이 구조적으로 보장된다(경로 재열기 창 없음). target 이 점유돼 있으면
    `_free_backup_path` 순번으로 비켜 가고, 모드·타임스탬프는 fd 기반으로 유지한다."""
    src_stat = os.fstat(src_fd)
    for _attempt in range(100):
        target = _free_backup_path(Path(dest_root) / target_base_rel)
        try:
            target_rel = target.relative_to(dest_root)
        except ValueError as exc:
            raise UnsafeDestPathError(f"백업 위치가 dest 밖입니다: {target}") from exc
        try:
            dst_fd = _open_dest_relative_nofollow(
                dest_root, target_rel, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                stat.S_IMODE(src_stat.st_mode), create_parents=True,
                root_identity=root_identity)
        except FileExistsError:
            continue  # 순번을 고른 사이 선점됨 — 다음 순번(원본 보존 우선).
        with _fdopen_binary(dst_fd, "wb") as dst:
            _stream_fd_into(src_fd, dst)  # 순번 재시도 시 처음부터 다시 흘린다.
            dst.flush()
            if hasattr(os, "fchmod"):
                os.fchmod(dst.fileno(), stat.S_IMODE(src_stat.st_mode))
            if os.utime in getattr(os, "supports_fd", frozenset()):
                os.utime(dst.fileno(),
                         ns=(src_stat.st_atime_ns, src_stat.st_mtime_ns))
        return target
    raise OSError(f"백업 경로 순번 100회 충돌: {label or target_base_rel}")


# ── 경로 표기 정규화 (Windows 확장 길이 prefix) ───────────────────────────────
# Windows 의 `os.readlink`/`Path.resolve()` 는 확장 길이 경로 prefix `\\?\`(UNC 는 `\\?\UNC\`)를
# 붙여 돌려준다 — 커널이 symlink 대상을 `\??\C:\…` 로 저장하고 CPython 이 그것을 `\\?\C:\…` 로
# 번역하기 때문이다(`Path.resolve()` 는 입력에 그 prefix 가 있으면 산출에도 유지한다). 그 표기가
# **값**에 실리면 두 자리에서 샌다:
#   기록값 — 백업이 다시 심는 링크 대상. 원본 링크의 사용자-가시 대상은 `C:\…` 인데 백업만
#            `\\?\C:\…` 를 갖게 되어 채택자 백업 트리에 플랫폼 표기가 노출된다.
#   비교값 — "링크 대상 자체를 보존했는가" 판정. 같은 파일을 가리키는 두 경로가 문자열로 갈린다.
# 그래서 규칙을 **여기 한 곳**에 두고 두 자리가 같은 함수를 부른다 — 한쪽만 벗기면 같은 결함이
# 자리만 옮긴다.
#
# 정규화 대상은 **표기뿐**이다. 파일시스템 호출에 넘기는 경로(dest 루트·조상 순회·열기)는 `Path`
# 그대로 둔다 — MAX_PATH 를 넘는 경로에서 `\\?\` 는 표기가 아니라 *기능*이라 벗기면 열리지 않는다.
_EXTENDED_PATH_PREFIX = "\\\\?\\"          # 실제 문자열: \\?\


_EXTENDED_UNC_PREFIX = "\\\\?\\UNC\\"      # 실제 문자열: \\?\UNC\


# 같은 prefix 의 슬래시 표기 — `PurePath.as_posix()` 산출과 `//?/…` 로 주어진 입력이 이 형태다.
_EXTENDED_PATH_PREFIX_SLASH = "//?/"


_EXTENDED_UNC_PREFIX_SLASH = "//?/UNC/"


# 확장 prefix 없이 다룰 수 있는 경로 길이 상한(Windows MAX_PATH).
_WINDOWS_MAX_PATH_CHARS = 260


# 확장 prefix 제거는 **Windows 표기 축**이다 — POSIX 에서는 `\` 가 정당한 파일명 문자라 같은
# 문자열을 벗기면 실재하는 파일명을 망가뜨린다(링크 대상이라면 백업이 다른 곳을 가리킨다).
# 플랫폼 판정을 모듈 상수로 두어 테스트가 Windows 표기를 다른 플랫폼에서 **주입해** 규칙을 실제로
# 태울 수 있게 한다(능력 플래그 `_DEST_FD_WALK_SUPPORTED` 와 같은 주입 seam).
_WINDOWS_PATH_NOTATION = os.name == "nt"


def _strip_extended_path_prefix(text: str) -> str:
    """확장 길이 prefix 를 벗긴 경로 표기를 돌려준다(순수 문자열 규칙·플랫폼 판정 없음).

    UNC 변형은 루트 표기까지 되돌린다 — `\\\\?\\UNC\\server\\share` 에서 prefix 만 잘라내면
    `UNC\\server\\share` 라는 **다른 경로**가 되므로 `\\\\server\\share` 로 복원한다. 슬래시
    표기(`//?/…`)도 같은 규칙으로 받는다(`as_posix()` 산출이 그 형태다).
    """
    for extended, root in (
            (_EXTENDED_UNC_PREFIX, "\\\\"),
            (_EXTENDED_UNC_PREFIX_SLASH, "//"),
            (_EXTENDED_PATH_PREFIX, ""),
            (_EXTENDED_PATH_PREFIX_SLASH, "")):
        if text.startswith(extended):
            return root + text[len(extended):]
    return text


def _path_notation_text(path) -> str:
    """경로를 **비교·기록 표기**로 직렬화한다 — Windows 확장 길이 prefix 를 남기지 않는다.

    Windows 표기 축이 아닌 플랫폼에서는 입력 표기를 그대로 둔다(위 `_WINDOWS_PATH_NOTATION`).
    구분자는 건드리지 않는다 — 이 함수의 축은 prefix 하나이고, POSIX 단일 직렬화가 필요한 자리는
    `as_posix()` 를 함께 쓴다(장부 표기 축은 pm_handoff·pm_bootstrap 소관).
    """
    text = path if isinstance(path, str) else os.fspath(path)
    if not _WINDOWS_PATH_NOTATION:
        return text
    return _strip_extended_path_prefix(text)


def _preserved_link_target(link_target: str) -> str:
    """백업 링크가 **기록**할 대상 표기 — 표기 정규화 + 긴 경로 안전 가드.

    `os.readlink` 가 돌려준 `\\\\?\\C:\\…` 를 그대로 다시 심으면 백업 링크만 원본에 없던 표기를
    갖는다. 다만 MAX_PATH 를 넘는 대상에서는 그 prefix 가 있어야 링크 생성·해소가 되므로 그때는
    **벗기지 않는다** — 표기 통일보다 링크가 실제로 대상을 가리키는 것이 우선이다.
    """
    normalized = _path_notation_text(link_target)
    if normalized != link_target and len(normalized) >= _WINDOWS_MAX_PATH_CHARS:
        return link_target
    return normalized


def _backup_symlink_nofollow(
        dest_root: Path, src_rel: Path, target_base_rel: Path,
        root_identity: tuple | None = None) -> Path:
    """dest 안 **symlink 자체**를 dest 안 백업 자리에 재생성한다(경로 재해소 0).

    링크는 내용이 아니라 *대상 문자열*이 자산이라 fd 스트리밍 복사로 표현할 수 없다. 그렇다고
    `copy2(follow_symlinks=False)` 로 두면 원본·백업 경로를 다시 해소하므로, 그 사이 부모가 저장소
    밖 지향 링크로 바뀌면 밖의 링크를 읽거나 밖에 링크를 만든다. 그래서 양쪽 **부모 디렉토리 fd**
    를 symlink 미추종으로 열고 그 안에서 `readlink`/`symlink` 만 한다 — 우리가 쓰는 디렉토리는 연
    fd 에 묶여 이후 경로 교체와 무관하다. 점유 자리는 `_free_backup_path` 순번으로 비켜 간다.

    dir_fd 미지원 플랫폼은 다른 채널과 같은 폴백을 쓴다(직전 재검사 + 경로 조작·창을 좁힐 뿐)."""
    src_rel = Path(src_rel)
    if not _DEST_FD_WALK_SUPPORTED:
        # 검사 대상은 **조상**뿐이다 — 이 채널의 원본은 정의상 symlink 라(그 링크를 보존하러 왔다)
        #   leaf 까지 거부하면 정상 형상이 통째로 막힌다. 조상 링크만 밖을 향하게 만들 수 있다.
        if not _is_safe_dest_path(dest_root, src_rel.parent):
            raise UnsafeDestPathError(
                f"백업 원본 조상 경로가 안전하지 않습니다: {src_rel.as_posix()}")
        assert_dest_root_unchanged(dest_root, root_identity)
        target = _free_backup_path(Path(dest_root) / target_base_rel)
        _ensure_dest_dir_nofollow(
            dest_root, target.relative_to(dest_root).parent, root_identity=root_identity)
        # 옛 `shutil.copy2(follow_symlinks=False)` 와 같은 동작(대상 문자열 재심기 + copystat)을
        #   펼쳐 쓴다 — 그 안의 `os.readlink` 산출이 곧 기록값이라, 표기 정규화를 끼울 자리가
        #   copy2 안에는 없다(이 폴백 분기가 곧 Windows 경로다).
        source = Path(dest_root) / src_rel
        os.symlink(_preserved_link_target(os.readlink(source)), target)
        shutil.copystat(source, target, follow_symlinks=False)
        return target
    src_dir_fd = _open_dest_dir_nofollow(
        dest_root, src_rel.parent, root_identity=root_identity)
    try:
        try:
            raw_link_target = os.readlink(src_rel.name, dir_fd=src_dir_fd)
        except OSError as exc:
            _raise_dest_path_refusal(src_rel, exc)
        # 아래 `os.symlink` 가 심는 값이 곧 채택자가 보는 백업 링크의 대상 표기다 — 기록 직전에
        #   확장 길이 prefix 를 벗긴다(비교 표기와 같은 규칙·`_path_notation_text`).
        link_target = _preserved_link_target(raw_link_target)
        for _attempt in range(100):
            target = _free_backup_path(Path(dest_root) / target_base_rel)
            try:
                target_rel = target.relative_to(dest_root)
            except ValueError as exc:
                raise UnsafeDestPathError(f"백업 위치가 dest 밖입니다: {target}") from exc
            _ensure_dest_dir_nofollow(
                dest_root, target_rel.parent, root_identity=root_identity)
            dst_dir_fd = _open_dest_dir_nofollow(
                dest_root, target_rel.parent, root_identity=root_identity)
            try:
                os.symlink(link_target, target_rel.name, dir_fd=dst_dir_fd)
            except FileExistsError:
                continue  # 순번을 고른 사이 선점됨 — 다음 순번(원본 보존 우선).
            except OSError as exc:
                _raise_dest_path_refusal(target_rel, exc)
            finally:
                os.close(dst_dir_fd)
            return target
        raise OSError(f"백업 경로 순번 100회 충돌: {src_rel.as_posix()}")
    finally:
        os.close(src_dir_fd)


def _unlink_dest_relative_nofollow(
        dest_root: Path, rel: Path, root_identity: tuple | None = None) -> None:
    """dest 안 파일/링크를 **부모 디렉토리 fd 안에서** 지운다(경로 재해소 0).

    경로 `os.unlink` 은 삭제 직전 조상이 교체되면 저장소 밖 파일을 지운다 — 링크를 걷어내고 그
    자리에 일반 파일을 놓는 복사 경로가 그 창을 갖고 있었다."""
    rel = Path(rel)
    if not _DEST_FD_WALK_SUPPORTED:
        # 조상만 검사한다 — 삭제 대상 자신이 symlink 인 것이 이 채널의 정상 형상이고(`os.unlink` 은
        #   링크를 따라가지 않는다), 밖을 가리키게 만드는 건 조상 링크뿐이다.
        if not _is_safe_dest_path(dest_root, rel.parent):
            raise UnsafeDestPathError(
                f"삭제 대상 조상 경로가 안전하지 않습니다: {rel.as_posix()}")
        assert_dest_root_unchanged(dest_root, root_identity)
        os.unlink(Path(dest_root) / rel)
        return
    dir_fd = _open_dest_dir_nofollow(dest_root, rel.parent, root_identity=root_identity)
    try:
        try:
            os.unlink(rel.name, dir_fd=dir_fd)
        except OSError as exc:
            _raise_dest_path_refusal(rel, exc)
    finally:
        os.close(dir_fd)


def _backup_to_central_dir_nofollow(
        dest_root: Path, rel: Path, backup_root: Path,
        root_identity: tuple | None = None) -> Path:
    """`dest_root/rel` 을 `backup_root/<rel>` 로 symlink 미추종 복사(제자리 편집 백업 채널)."""
    try:
        target_base_rel = (Path(backup_root) / rel).relative_to(dest_root)
    except ValueError as exc:
        raise UnsafeDestPathError(
            f"백업 위치가 dest 밖입니다: {Path(backup_root) / rel}") from exc
    return _copy_dest_file_nofollow(
        dest_root, rel, target_base_rel, root_identity=root_identity)


class InplaceBackupOutcome(NamedTuple):
    """제자리 편집 백업 결과 — 성공분과 **고치면 안 되는 분**을 사유별로 가른다."""

    backed_up: list[str]
    refused: list[str]   # 계획 뒤 경로를 안전하게 열 수 없음(교체 의심).
    vanished: list[str]  # 선검사 뒤 대상이 사라짐(삭제 경쟁).


def _backup_before_inplace_edit(
        dest_root: Path, relpaths: list[Path], backup_root: Path | None,
        git_safe: set | None,
        root_identity: tuple | None = None) -> InplaceBackupOutcome:
    """복사 대상이 아닌 기존 파일을 **변경 직전에** 중앙 백업한다.

    `CopyAction` 백업 규칙과 동형: git 추적&미변경(git_safe)은 git 이 복원 가능하므로 생략하고,
    그 밖은 `backup_root/<relpath>`(경로 점유 시 `_free_backup_path` 순번). backup_root=None 이면
    백업 위치가 없어 무동작. 계획 시점의 symlink 는 `_shared_notation_rerender_plan` 이 이미
    거부했고, **계획 뒤 교체·삭제**는 여기서 각각의 목록으로 돌려준다 — 호출부가 재렌더 대상에서
    빼고 loud 하게 알린다(백업 못 하는 파일은 고치지 않는다).

    삭제 경쟁(`FileNotFoundError`)을 예외로 흘리지 않는 이유: 이 시점엔 복사·manifest 변경이 이미
    끝나 있어 예외가 곧 **부분 적용 잔존**이다. 렌더 쓰기의 같은 경쟁 처리와 동형으로 흡수한다."""
    if backup_root is None:
        return InplaceBackupOutcome([], [], [])
    done: list[str] = []
    refused: list[str] = []
    vanished: list[str] = []
    for rel in relpaths:
        # 이 목록은 계획이 **실재를 확인한** 대상이라, 부재·형상 변화는 계획 뒤 사고다 — 조용한
        #   skip 대신 사유별 목록에 실어 호출부가 재렌더에서 빼고 loud 로 알리게 한다.
        anomaly = _copied_scope_anomaly(dest_root, rel)
        if anomaly == "vanished":
            vanished.append(rel.as_posix())
            continue
        if anomaly == "swapped":
            refused.append(rel.as_posix())
            continue
        if git_safe is not None and rel.as_posix() in git_safe:
            continue  # git 이 복원 가능 — 백업 생략(plan_copy git-safe skip 과 동형).
        try:
            _backup_to_central_dir_nofollow(
                dest_root, rel, backup_root, root_identity=root_identity)
        except UnsafeDestPathError:
            refused.append(rel.as_posix())
            continue
        except FileNotFoundError:
            vanished.append(rel.as_posix())
            continue
        done.append(rel.as_posix())
    return InplaceBackupOutcome(done, refused, vanished)
