"""현재-진실 문서 `verified_at` 핀 쓰기 (층: app).

architecture.md·status.md·covers 보유 domain 페이지 가운데 핀 대상을 고르고 frontmatter 의
`verified_at`·`repo` 를 원자적으로 고쳐 쓴다.
"""
from __future__ import annotations

import contextlib
import os
import re
import tempfile
from pathlib import Path
from collections.abc import Sequence

import yaml

from pm_engine.domain import doc_pages, ticket_rules
from pm_engine.infra import doc_page_store, fs, pm_paths


_FRONTMATTER_FENCE_RE = re.compile(r"^---\s*$")


def _verified_at_backfill_targets(home) -> list[Path]:
    """repin 전체 대상 — architecture.md·status.md + covers 보유 domain 페이지.

    validate-all-first 열거다. frontmatter로 시작한 domain 문서 하나라도 읽기/파싱 실패하면
    예외를 전파해, 깨진 문서를 대상에서 조용히 제외한 채 나머지만 쓰는 부분 성공을 금지한다.
    """
    targets: list[Path] = []
    if pm_paths.architecture_file(home).exists():
        targets.append(pm_paths.architecture_file(home))
    if pm_paths.status_file(home).exists():
        targets.append(pm_paths.status_file(home))
    for page in doc_page_store.load_pages(pm_paths.domain_dir(home), strict=True):
        if page.get("covers"):
            targets.append(Path(page["path"]))
    return targets


class _VerifiedAtPageSelectionError(ValueError):
    """`--page`가 canonical 현재-진실 문서를 가리키지 않을 때의 명시 실패."""

    def __init__(self, root: Path, page: str, reason: str):
        self.path = root / page if page and not Path(page).is_absolute() else root
        super().__init__(f"--page {page!r}: {reason}")


def _verified_at_targets(home, pages: Sequence[str] | None) -> list[Path]:
    """전체 또는 `--page`로 고른 현재-진실 문서 대상을 반환한다.

    선택자 미지정(`None`)은 기존 전체 열거를 그대로 호출한다. 선택자를 지정하면
    validate-all-first의 "all"은 **선택 집합 전체**로 좁아진다. 이때 선택 밖 문서의 파싱
    오류가 쓰기를 막지 않는 것은 의도된 계약이다. 그 문서들의 핀은 한 바이트도 바뀌지 않아
    새 green 주장이 생기지 않기 때문이다. 대신 빈/비존재/비대상 선택을 오류로 거부해
    "0개를 검증하고 성공"하는 false-green을 막고, domain 문서는 실제 소비 파서로 읽어
    covers 보유·비-draft인 canonical 대상인지 확인한다.
    """
    if pages is None:
        return _verified_at_backfill_targets(home)
    if not pages:
        raise _VerifiedAtPageSelectionError(home.root, "", "선택 문서가 비어 있음")

    repo_root = home.root.resolve()
    fixed_targets = {Path(pm_paths.architecture_file(home)).resolve(), Path(pm_paths.status_file(home)).resolve()}
    targets: list[Path] = []
    seen: set[Path] = set()
    for supplied in pages:
        raw = str(supplied).strip()
        relative = Path(raw)
        if not raw:
            raise _VerifiedAtPageSelectionError(home.root, raw, "빈 경로")
        if relative.is_absolute() or ".." in relative.parts:
            raise _VerifiedAtPageSelectionError(home.root, raw, "REPO 상대 경로만 허용")
        unresolved = home.root / relative
        try:
            candidate = unresolved.resolve()
            candidate.relative_to(repo_root)
        except (OSError, ValueError):
            raise _VerifiedAtPageSelectionError(home.root, raw, "REPO 밖 경로") from None
        except RuntimeError:
            # Python 3.12 의 `Path.resolve()` 는 symlink loop 에 `RuntimeError`("Symlink loop
            # from …") 를 낸다 — 3.13+ 는 같은 상황을 `OSError`(ELOOP) 로 낸다. 어느 하한에서도
            # 통제되지 않은 traceback 이 나지 않게 두 축을 함께 선택 오류로 정규화한다
            # (실측: 3.12.3 상호/자기 루프 모두 RuntimeError).
            raise _VerifiedAtPageSelectionError(home.root, raw, "경로 해소 실패(symlink loop)") from None
        if not candidate.is_file():
            raise _VerifiedAtPageSelectionError(home.root, raw, "문서가 존재하지 않음")

        if candidate not in fixed_targets:
            domain_dir = pm_paths.domain_dir(home).resolve()
            try:
                candidate.relative_to(domain_dir)
            except ValueError:
                raise _VerifiedAtPageSelectionError(
                    home.root, raw, "architecture.md/status.md/covers domain 문서가 아님") from None
            if candidate.suffix != ".md" or candidate.name in doc_pages._NON_PAGE_FILES:
                raise _VerifiedAtPageSelectionError(home.root, raw, "canonical domain 문서가 아님")
            try:
                page = doc_page_store.parse_page(candidate)
            except Exception as exc:  # noqa: BLE001 — domain 소비 파서 오류를 선택 오류로 정규화.
                raise _VerifiedAtPageSelectionError(
                    home.root, raw, f"domain 문서 실소비 파싱 실패:{exc}") from exc
            if page.get("status") == doc_pages.DRAFT_STATUS or not page.get("covers"):
                raise _VerifiedAtPageSelectionError(
                    home.root, raw, "covers 보유·비-draft domain 문서가 아님")

        if candidate not in seen:
            targets.append(candidate)
            seen.add(candidate)
    return targets


def _replace_freshness_pin(text: str, sha: str, owner: str) -> str | None:
    """frontmatter 의 `verified_at`·`repo`를 한 소유 시계로 insert-or-replace한다.

    전체 YAML 재-dump 없이 두 scalar 줄만 바꿔 hand-authored 순서/날짜/본문을 보존한다.
    frontmatter가 없거나 닫히지 않으면 None. 같은 값이면 원문을 그대로 반환(호출부가 same 구분).
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None
    close = next((i for i in range(1, len(lines))
                  if _FRONTMATTER_FENCE_RE.match(lines[i])), None)
    if close is None:
        return None
    wanted = {"repo": owner, "verified_at": f'"{sha}"'}
    seen: set[str] = set()
    for i in range(1, close):
        # 컬럼 0 키만 freshness scalar 로 취급한다. 들여쓴 동명 키는 중첩 mapping 또는
        # block scalar 본문일 수 있으므로 건드리지 않고, 최상위 키가 없으면 아래에서 새로 넣는다.
        match = re.match(
            r"""^(?P<quote>["']?)(?P<key>repo|verified_at)(?P=quote)\s*:.*?(\r?\n)?$""",
            lines[i],
        )
        if not match:
            continue
        key, newline = match.group("key"), match.group(3) or ""
        lines[i] = f"{key}: {wanted[key]}{newline}"
        seen.add(key)
    missing = [key for key in ("repo", "verified_at") if key not in seen]
    if missing:
        if close > 0 and lines[close - 1] and not lines[close - 1].endswith(("\n", "\r")):
            lines[close - 1] += "\n"
        for key in missing:
            lines.insert(close, f"{key}: {wanted[key]}\n")
            close += 1
    return "".join(lines)


def _frontmatter_mapping(text: str) -> dict:
    """문서 frontmatter를 실제 YAML로 파싱해 mapping을 반환한다(repin 검증 전용).

    문자열 치환 성공과 YAML 안전은 별개다. 특히 `repo: >` 뒤 continuation 줄은 첫 줄만
    바꾸면 YAML이 깨지거나 `upstream <옛값>`으로 의미가 합쳐질 수 있다. 원본과 변환 결과
    모두 이 함수로 `yaml.safe_load`하고 mapping이 아니어도 안전 교체 불가로 본다.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ValueError("frontmatter 시작 구분자 없음")
    close = next((i for i in range(1, len(lines))
                  if _FRONTMATTER_FENCE_RE.match(lines[i])), None)
    if close is None:
        raise ValueError("frontmatter 종료 구분자 없음")
    parsed = yaml.safe_load("".join(lines[1:close]))
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ValueError(f"frontmatter가 mapping 아님({type(parsed).__name__})")
    return parsed


def _freshness_replacement_error(
        original: str, replaced: str, sha: str, owner: str) -> str | None:
    """repin 변환 전후 YAML 의미와 실소비 파서 정합을 검증한다.

    `_frontmatter_mapping`은 의미 보존 검사용이라 공백 fence/EOF fence도 읽지만, 실제 freshness
    소비자는 `load_ticket`의 엄격한 fence 문법을 쓴다. 최종 결과는 그 파서의 문자열 진입점으로
    다시 읽어, repin 성공 뒤 lint가 문서를 조용히 skip하는 false-green을 쓰기 전에 차단한다.
    """
    try:
        before = _frontmatter_mapping(original)
    except (ValueError, yaml.YAMLError) as exc:
        return f"원본 YAML 파싱 실패:{exc}"
    try:
        after = _frontmatter_mapping(replaced)
    except (ValueError, yaml.YAMLError) as exc:
        return f"변환 YAML 파싱 실패:{exc}"
    try:
        consumed, _body = ticket_rules._parse_ticket_text(replaced, "<repin 변환 결과>")
    except (ValueError, yaml.YAMLError) as exc:
        return f"변환 결과 실소비 파서 파싱 실패:{exc}"
    if not isinstance(consumed, dict):
        return f"변환 결과 실소비 frontmatter가 mapping 아님({type(consumed).__name__})"
    if after.get("repo") != owner or after.get("verified_at") != sha:
        return (
            "다중행/복합 freshness 값 안전 교체 불가"
            f"(변환값 repo={after.get('repo')!r}, verified_at={after.get('verified_at')!r})"
        )
    before_other = {key: value for key, value in before.items()
                    if key not in {"repo", "verified_at"}}
    after_other = {key: value for key, value in after.items()
                   if key not in {"repo", "verified_at"}}
    if after_other != before_other:
        return "freshness 외 frontmatter 의미 변경 감지"
    return None


def _atomic_write_text(path: Path, text: str) -> None:
    """같은 디렉토리의 임시 파일을 완전히 flush한 뒤 `fs.atomic_replace`로 원자 교체한다.

    임시 파일 쓰기/flush/fsync 또는 replace가 실패하면 임시 파일만 정리하고 원본은 건드리지
    않는다. 기존 파일 mode도 임시 파일에 복제해 교체가 권한을 바꾸지 않게 한다.
    """
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        os.chmod(tmp_path, path.stat().st_mode & 0o7777)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            fd = -1  # fd 소유권은 handle로 이전됐다.
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        fs.atomic_replace(tmp_path, path)
    except BaseException:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def repin_verified_at(
        home,
        sha: str, owner: str, *, dry_run: bool = False,
        pages: Sequence[str] | None = None) -> list[tuple[Path, str]]:
    """architecture/status + covers domain 페이지를 validate-all-first 후 재핀한다.

    선택자 미지정이면 기존 일괄 대상 전부, 지정이면 선택 집합 전부를 1단계에서 읽고
    frontmatter 변환 가능 여부를 검증한다. 하나라도 읽기/frontmatter 오류면 **그 집합의 어느
    파일도 쓰지 않고** `error:*` + `not-written:validation-failed` 상태를 돌려준다. 전부
    유효할 때만 2단계 원자 교체를 시작한다. 쓰기 오류는 앞서 교체된 파일은 `updated`, 원본이
    보존된 실패 파일은 `error:write`, 뒤 대상은 `not-written:write-failed`로 명시해 호출부가
    실제 변경 범위를 정확히 보고할 수 있게 한다. dry-run은 검증까지만 하고 변경 예정 대상을
    `updated`로 표시한다.
    """
    prepared: list[tuple[Path, str, str | None]] = []
    try:
        targets = _verified_at_targets(home, pages)
    except Exception as exc:  # noqa: BLE001 — 대상 열거 실패도 validate-all-first 중단.
        error_path = Path(getattr(exc, "path", pm_paths.domain_dir(home)))
        return [(error_path, f"error:enumeration:{exc}")]
    for path in targets:
        try:
            text = fs.read_text_shared(path, encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            prepared.append((path, f"error:read:{exc}", None))
            continue
        new_text = _replace_freshness_pin(text, sha, owner)
        if new_text is None:
            prepared.append((path, "error:no-frontmatter", None))
            continue
        replacement_error = _freshness_replacement_error(text, new_text, sha, owner)
        if replacement_error is not None:
            prepared.append((path, f"error:yaml:{replacement_error}", None))
            continue
        if new_text == text:
            prepared.append((path, "same", None))
            continue
        prepared.append((path, "ready", new_text))

    if any(state.startswith("error:") for _path, state, _text in prepared):
        return [
            (path, "not-written:validation-failed" if state == "ready" else state)
            for path, state, _text in prepared
        ]

    if dry_run:
        return [(path, "updated" if state == "ready" else state)
                for path, state, _text in prepared]

    results: list[tuple[Path, str]] = []
    for index, (path, state, new_text) in enumerate(prepared):
        if state == "same":
            results.append((path, state))
            continue
        try:
            _atomic_write_text(path, new_text)
        except (OSError, UnicodeError) as exc:
            results.append((path, f"error:write:{exc}"))
            results.extend(
                (later_path, "not-written:write-failed" if later_state == "ready" else later_state)
                for later_path, later_state, _later_text in prepared[index + 1:]
            )
            break
        results.append((path, "updated"))
    return results
