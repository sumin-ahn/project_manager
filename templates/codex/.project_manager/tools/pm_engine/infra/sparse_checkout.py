"""sparse 폴더·gitlink 검증과 checkout."""
from __future__ import annotations

import re
from pathlib import Path

from pm_engine.domain import worktree_records
from pm_engine.infra import pm_paths, worktree_git


SPARSE_CHECKOUT_MIN_GIT = (2, 36)
_GIT_VERSION_RE = re.compile(r"git version (\d+)\.(\d+)")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_TREE_OBJECT_TYPE = "tree"          # `ls-tree` 의 폴더 항목 종류.
_GITLINK_OBJECT_TYPE = "commit"     # `ls-tree` 의 submodule 항목 종류.
_GITLINK_MODE = "160000"            # 인덱스의 submodule(gitlink) 항목 모드.
_SKIP_WORKTREE_TAG = "S"            # `ls-files -t` 태그. 추적하지만 폴더 제한으로 꺼내지 않은 항목.


def _normalize_folders(folders: "list[str]", *, target: str) -> "list[str]":
    """폴더 목록 형식 검사(부작용 전). 끝 `/` 하나를 떼고 나머지는 그대로 git 에 넘긴다.

    저장소 루트 기준 상대 폴더만 받는다. 빈 목록·빈 항목·절대경로(`/` 시작·드라이브 문자)·역슬래시·
    `.`/`..`/빈 조각은 거부한다. 폴더 실재(파일·submodule 아님)는 기준 커밋 트리로 따로 본다
    (`_require_tree_folders`). git 은 없는 폴더도 그대로 받는다.
    """
    if not folders:
        raise worktree_records.SlotFoldersRefused(target, "폴더 목록이 비어 있다. 꺼낼 폴더를 하나 이상 준다")
    normalized: list[str] = []
    for raw in folders:
        folder = raw[:-1] if raw.endswith("/") else raw
        if raw.startswith("/") or _WINDOWS_DRIVE_RE.match(raw):
            reason = "절대경로다(저장소 루트 기준 상대 경로만 받는다)"
        elif "\0" in raw:
            reason = "NUL 문자가 든 경로다"
        elif "\\" in raw:
            reason = "역슬래시 구분자다(`/` 만 받는다)"
        elif not folder:
            reason = "빈 항목이다"
        elif any(part in ("", ".", "..") for part in folder.split("/")):
            reason = "`.`·`..`·빈 조각이 든 경로다"
        else:
            normalized.append(folder)
            continue
        raise worktree_records.SlotFoldersRefused(target, f"{raw!r}: {reason}")
    return normalized


def _require_sparse_git(runner: worktree_records.GitRunner, *, target: str) -> None:
    """git 이 2.36 이상인지. 미만이면 bare 공용 저장소의 슬롯에 폴더 제한을 걸지 않는다."""
    rc, out = runner(["version"])
    match = _GIT_VERSION_RE.search(str(out)) if rc == 0 else None
    if match is None:
        raise worktree_records.SlotFoldersRefused(
            target, f"git 버전을 읽지 못했다 (rc={rc}): {str(out).strip()[:200]}")
    found = (int(match.group(1)), int(match.group(2)))
    if found < SPARSE_CHECKOUT_MIN_GIT:
        need = ".".join(str(part) for part in SPARSE_CHECKOUT_MIN_GIT)
        raise worktree_records.SlotFoldersRefused(
            target,
            f"git {found[0]}.{found[1]} 은 bare 공용 저장소의 슬롯에 폴더 제한을 바르게 걸지 못한다. "
            f"git {need} 이상이 필요하다",
        )


def _require_tree_folders(folders: "list[str]", ref: str, runner: worktree_records.GitRunner, *,
                          target: str) -> None:
    """폴더마다 커밋 `ref` 트리에 그 경로 그대로의 폴더(tree)가 있는지. 없음·파일·submodule 거부."""
    for folder in folders:
        rc, out = runner(["ls-tree", "-z", ref, "--", folder])
        if rc != 0:
            raise worktree_records.SlotFoldersRefused(
                target, f"`git ls-tree {ref} -- {folder}` 실패 (rc={rc}): {str(out).strip()[:200]}")
        kinds: dict[str, str] = {}
        for record in str(out).split("\0"):
            meta, tab, path = record.partition("\t")
            fields = meta.split()
            if tab and len(fields) == 3:
                kinds[path] = fields[1]
        kind = kinds.get(folder)
        if kind == _TREE_OBJECT_TYPE:
            continue
        if kind is None:
            reason = f"{ref} 에 없는 폴더다"
        elif kind == _GITLINK_OBJECT_TYPE:
            reason = "submodule 경로다(폴더만 받는다)"
        else:
            reason = "파일 경로다(폴더만 받는다)"
        raise worktree_records.SlotFoldersRefused(target, f"{folder!r}: {reason}")


def _config_bool(runner: worktree_records.GitRunner, key: str, *, target: str) -> "bool | None":
    """`git config --type=bool --get <key>`. 값이 없으면 None. 조회 실패는 거부로 올린다."""
    rc, out = runner(["config", "--type=bool", "--get", key])
    if rc == worktree_records._GIT_CONFIG_MISSING_RC:
        return None
    if rc != 0:
        raise worktree_records.SlotFoldersRefused(
            target, f"`git config --get {key}` 실패 (rc={rc}): {str(out).strip()[:200]}")
    return str(out).split("\n", 1)[0].strip() == "true"


def _run_sparse_checkout(runner: worktree_records.GitRunner, argv: "list[str]", *, target: str) -> None:
    """`git sparse-checkout <argv>`. rc≠0 이면 거부로 올린다(호출부의 롤백·idle 유지를 탄다)."""
    rc, out = runner(["sparse-checkout", *argv])
    if rc != 0:
        raise worktree_records.SlotFoldersRefused(
            target,
            f"`git sparse-checkout {' '.join(argv)}` 실패 (rc={rc}): {str(out).strip()[:200]}",
        )


def _require_clean_for_folders(home, slot: str, runner: worktree_records.GitRunner) -> None:
    """폴더 규칙을 바꾸는 대여는 커밋 안 한 변경이 없는 슬롯에서만. git 은 변경이 있어도 rc 0
    경고만 내고 그 파일만 남기는 반쪽 적용을 한다."""
    if worktree_git._is_dirty(pm_paths.slot_path(home, slot), git_runner=runner):
        raise worktree_records.SlotFoldersRefused(
            slot, "커밋 안 한 변경이 있어 꺼낼 폴더를 바꾸지 않는다. 커밋한 뒤 다시 대여한다")


def _checkout_folders(slot: str, path: Path, folders: "list[str]", *,
                      git_runner: worktree_records.GitRunner | None) -> None:
    """`--no-checkout` 으로 만든 슬롯에 폴더 규칙을 걸고 그 폴더만 꺼낸다. 전체를 디스크에 쓰지 않는다.

    파일을 쓰는 단계(`checkout`)는 worktree add 와 같은 콘솔 표시 러너·`GIT_TIMEOUT_SECONDS` 로 돈다
    (대형 저장소의 checkout 이 이 단계로 옮겨 왔다). `--no-recurse-submodules` 는 submodule 을
    건드리지 않는다. 초기화는 뒤의 submodule 단계가 꺼낸 것만 한다."""
    _run_sparse_checkout(git_runner or worktree_git._real_git_runner(path),
                         ["set", "--cone", "--", *folders], target=slot)
    checkout_runner = git_runner or worktree_git._real_git_runner_interactive(path, timeout=worktree_git.GIT_TIMEOUT_SECONDS)
    rc, out = checkout_runner(["checkout", "--no-recurse-submodules"])
    if rc != 0:
        raise worktree_records.SlotFoldersRefused(
            slot, f"`git checkout` 실패 (rc={rc}): {str(out).strip()[:200]}")


def slot_folders(home, slot: str, *, git_runner: worktree_records.GitRunner | None = None) -> "list[str] | None":
    """슬롯에 꺼낸 폴더 목록. git 이 가진 cone 목록 그대로(git 이 정규화한 뒤). 제한이 없으면 None.

    제한 여부·cone 여부는 슬롯 git 설정(`core.sparseCheckout`·`core.sparseCheckoutCone`)으로 읽고,
    목록은 `-c core.quotePath=false sparse-checkout list` 로 읽는다(한글 이름 원문). git 에러 문구에는
    기대지 않는다(로케일마다 다르다). cone 이 아닌 제한(패턴 지정)은 폴더로 보고하지 않고 거부한다.
    빈 목록은 최상위 파일만 꺼낸 상태다."""
    runner = git_runner or worktree_git._real_git_runner(pm_paths.slot_path(home, slot))
    if not _config_bool(runner, "core.sparseCheckout", target=slot):
        return None
    if not _config_bool(runner, "core.sparseCheckoutCone", target=slot):
        raise worktree_records.SlotFoldersRefused(
            slot,
            "cone 이 아닌 폴더 제한(패턴 지정)이라 폴더 목록으로 보고하지 않는다. 슬롯에서 "
            "`git sparse-checkout set --cone <폴더…>` 로 바꾸거나 `git sparse-checkout disable` 로 푼다",
        )
    rc, out = runner(["-c", "core.quotePath=false", "sparse-checkout", "list"])
    if rc != 0:
        raise worktree_records.SlotFoldersRefused(
            slot, f"`git sparse-checkout list` 실패 (rc={rc}): {str(out).strip()[:200]}")
    # Git C 인용은 바이트 단위 8진 escape를 포함한다. UTF-8 바이트를 복원한 뒤 해석한다.
    escapes = {'a': 7, 'b': 8, 't': 9, 'n': 10, 'v': 11, 'f': 12, 'r': 13,
               '"': 34, '\\': 92}
    folders: list[str] = []
    for line in str(out).split("\n"):
        if not line:
            continue
        if not line.startswith('"'):
            folders.append(line)
            continue
        try:
            if len(line) < 2 or not line.endswith('"'):
                raise ValueError("닫히지 않은 인용")
            value = line[1:-1]
            decoded = bytearray()
            index = 0
            while index < len(value):
                char = value[index]
                if char != "\\":
                    decoded.extend(char.encode("utf-8"))
                    index += 1
                    continue
                index += 1
                escaped = value[index]
                if escaped in escapes:
                    decoded.append(escapes[escaped])
                    index += 1
                elif escaped in "01234567":
                    octal = value[index:index + 3]
                    if len(octal) != 3 or any(digit not in "01234567" for digit in octal):
                        raise ValueError("잘못된 8진 escape")
                    decoded.append(int(octal, 8))
                    index += 3
                else:
                    raise ValueError("알 수 없는 escape")
            folders.append(decoded.decode("utf-8"))
        except (ValueError, IndexError) as exc:
            raise worktree_records.SlotFoldersRefused(slot, f"Git 폴더 인용 해석 실패: {line!r}") from exc
    return folders


def _index_gitlink_tags(runner: worktree_records.GitRunner, *, target: str,
                        paths: "list[str] | None" = None) -> "dict[str, str]":
    """인덱스의 submodule(gitlink) 항목 `{경로: ls-files -t 태그}`. `S` 는 폴더 제한으로 꺼내지 않은 것.

    `paths` 를 주면 그 경로만 literal pathspec 으로 묻는다(빈 목록으로는 부르지 않는다. 호출부 몫)."""
    argv = ["ls-files", "-t", "-s", "-z"]
    if paths is not None:
        argv += ["--", *(f":(literal){path}" for path in paths)]
    rc, out = runner(argv)
    if rc != 0:
        raise worktree_records.SlotFoldersRefused(
            target, f"`git ls-files -t -s` 실패 (rc={rc}): {str(out).strip()[:200]}")
    tags: dict[str, str] = {}
    for record in str(out).split("\0"):
        meta, tab, path = record.partition("\t")
        fields = meta.split()
        if tab and len(fields) == 4 and fields[1] == _GITLINK_MODE:
            tags[path] = fields[0]
    return tags
