#!/usr/bin/env python3
"""사설 참조 판정식 — 출하 산문의 사설 작업-항목 참조를 기계 판정하는 공용 seam.

Python 소스는 표준 tokenizer 로 본다. 주석과 문서로 쓰인 단독 문자열 표현식만 산문으로
판정하고, 보통 문자열 리터럴과 식별자는 일부러 건드리지 않는다. 데이터 리터럴 표식이 붙은
구간은 채택자 디스크에 기록되는 wire 문자열이라 판정에서 뺀다 — 스트립하는 쪽과 재유입을
막는 쪽이 이 한 벌을 함께 소비하므로 한쪽만 아는 판정이 생기지 않는다.

표면 술어 ``shipping_paths`` 는 repo-owned 열거 결과를 출하 python/markdown 축으로 분류한다.
경로별 예외를 두지 않고 git ignore/소유 판정으로 갈라, 새 파생 파일이 생겨도 같은 규칙이
적용된다.

``ENGINE_REV`` 는 baked 리터럴이다(형제 사본 skew fail-loud · ``engine_rev.py --bump`` 대상).
stdlib-only 이며 형제는 ``repo_owned_files``(repo 소유 파일 열거 seam) 하나만 경로-앵커로
지연 로드한다.
"""

from __future__ import annotations

import ast
import io
import os
import re
import sys
import tokenize
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

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
                    # 런타임에 만든 형제 모듈(중앙 로더 선복구가 방금 복사한 seam 등)을
                    # 이름으로 import 한다 — FileFinder 는 디렉터리 목록을 mtime 으로 캐시하고
                    # 인터프리터 시작 뒤 생긴 파일은 invalidate 없이는 인식이 보장되지 않는다
                    # (Python 문서 `importlib.invalidate_caches` · Windows 실측 간헐
                    # ModuleNotFoundError). 블록은 stdlib-only 라 지역 import 로 두되 sys.path 에
                    # parent 를 넣기 전에 가져와 그 트리의 동명 파일이 stdlib 를 가리지 않게 한다.
                    import importlib as _bootstrap_importlib
                    added = not sys.path or sys.path[0] != parent
                    if added:
                        sys.path.insert(0, parent)
                    try:
                        _bootstrap_importlib.invalidate_caches()
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


# ── 엔진 사본 rev 스탬프 (형제 사본 skew fail-loud) ──────────────────────
# baked 리터럴 — `local_conf.py`·`file_lock.py` 와 같은 규약이다. 릴리즈 bump 는
# `engine_rev.py --bump vX.Y.Z` 가 전 stamped 모듈을 기계 일괄 재작성한다.
ENGINE_REV = "v1.7.8"


def _verify_engine_rev(sibling_module, sibling_filename):
    """로드한 형제의 baked ENGINE_REV 를 이 사본의 것과 대조한다(skew 만 fail-loud)."""
    got = getattr(sibling_module, "ENGINE_REV", None)
    if got != ENGINE_REV:
        err = RuntimeError(
            f"엔진 사본 버전 불일치 — 로더 {Path(__file__).name}(rev={ENGINE_REV!r})가 "
            f"형제 {sibling_filename}(rev={got!r})를 로드했다 (사본 skew: 부분/수동 복사 또는 "
            f"구형 사본). `pm-update`(또는 pm_update.py)로 .project_manager/tools/ 전체를 재동기하라."
        )
        err._engine_rev_skew = True
        raise err


# 판정 앵커 — 이 사본이 속한 트리의 루트(``.project_manager/tools/`` 의 두 단계 위).
REPO = Path(__file__).resolve().parents[2]


RAW_REF_RE = re.compile(r"(?:T|ADR)-\d{4}")
SPECIFIC_REF_RE = re.compile(r"(?<![A-Za-z0-9_])(?:T|ADR)-\d{4}(?!\d)")
_INLINE_CODE_RE = re.compile(r"(?<!`)({})(?!`)(.*?)\1(?!`)".format(r"`+"))
# 채택자 디스크에 기록되는 wire 문자열은 산문이 아니다 — 리터럴 옆 소스 주석으로 그 사실을
# 밝히면 스트립과 재유입 가드가 함께 제외한다. 표식을 리터럴 옆에 두는 이유는 새 마커를
# 추가하는 사람이 같은 자리에서 보고 붙이게 하기 위해서다(별도 allow-list 파일은 drift 한다).
# 두 패턴은 **주석 토큰 본문**에만 맞춘다 — 문자열 리터럴 안의 같은 글자는 데이터지 표식이
# 아니라서 원문 정규식으로 보면 리터럴 한 줄로 재유입 검사를 우회할 수 있다.
_DATA_LITERAL_MARKER = "pm:data-literal"
_DATA_LITERAL_PREFIX_RE = re.compile(r"^#[ \t]*" + re.escape(_DATA_LITERAL_MARKER))
_DATA_LITERAL_LINE_RE = re.compile(
    r"^#[ \t]*" + re.escape(_DATA_LITERAL_MARKER) + r"(?![:\w-])"
)
_DATA_LITERAL_BLOCK_RE = re.compile(
    r"^#[ \t]*" + re.escape(_DATA_LITERAL_MARKER) + r":(?P<edge>begin|end)[ \t]*$"
)
_LONE_CARRIAGE_RETURN_RE = re.compile(r"\r(?!\n)")
# 문장 경계를 세는 데 쓰지 않는 토큰 — 라인 표식이 가리키는 문장 범위 계산용.
_NON_STATEMENT_TOKENS = frozenset(
    {tokenize.NL, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER}
)
_REFERENCE_ONLY_WRAPPER_RE = re.compile(
    r"\([ \t]*"
    + SPECIFIC_REF_RE.pattern
    + r"(?:[ \t]*·[ \t]*"
    + SPECIFIC_REF_RE.pattern
    + r")*[ \t]*\)"
)
_DELTA_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("separator-space", re.compile(r"· ")),
    ("double-separator", re.compile(r"··")),
    ("open-separator", re.compile(r"\(·")),
    ("separator-close", re.compile(r"·\)")),
    ("space-close", re.compile(r" \)")),
    ("open-space", re.compile(r"\( ")),
    ("trailing-separator", re.compile(r"·$")),
    ("leading-close", re.compile(r"^[ \t]*\)")),
    ("double-space", re.compile(r"(?= {2})")),
    (
        "orphan-marker-after-separator",
        re.compile(
            r"·[ \t]*(?:[\u2460-\u2473\u3251-\u325f\u32b1-\u32bf]"
            r"|[A-Za-z]\d+|\d+[A-Za-z])(?=$|[ \t·,.;:)\]])"
        ),
    ),
    ("empty-paren", re.compile(r"\(\s*\)")),
    # 설계 문서의 결정 항목·형상 번호로 쓰는 유니코드 원문자 — 경계(·) 유무와 무관하게 라인
    # 어디든 나타나면 사설 표식이다.
    ("circled-marker", re.compile(r"[\u2460-\u2473\u3251-\u325f\u32b1-\u32bf]")),
    # `F` + 숫자 형태로 쓰는 사설 fault 라벨 — 앞뒤가 식별자 문자(영숫자·`_`)나 base64 문자
    # (`+`·`/`)가 아닌 단독 토큰만 잡는다. 앞에 다른 알파벳이 붙은 리뷰-라운드 인용 표기, 뒤에
    # 알파벳이나 `_` 가 이어지는 정상 파이썬 식별자, base64 임베드 블롭 안의 우연 일치는 앞뒤
    # 인접 문자로 제외된다.
    ("design-label", re.compile(r"(?<![A-Za-z0-9_+/])F\d+(?![A-Za-z0-9_+/])")),
)


@dataclass(frozen=True)
class TokenSpan:
    start: int
    end: int
    line: int
    text: str


@dataclass(frozen=True)
class RemovalSpan:
    start: int
    end: int
    references: int


class DataLiteralMarkerError(ValueError):
    """데이터 리터럴 블록 표식의 짝이 맞지 않을 때 낸다."""


def _load_repo_owned_files():
    """공용 repo 소유 파일 열거 seam을 canonical checkout에서 로드한다."""
    path = REPO / ".project_manager" / "tools" / "repo_owned_files.py"
    return _load_module_from_path(
        path,
        "repo_owned_files.py",
        verifier=_verify_engine_rev,
        cache=True,
        cache_key=f"_private_context_repo_owned_files:{path.resolve()}",
    )


def repo_owned_paths(root: Path) -> list[Path]:
    """현재 tree에 존재하는 repo-owned 파일을 공용 ``OWNED`` 규칙으로 반환한다.

    추적 파일과 미추적·비-ignore 파일만 포함한다. git 없는 소스 아카이브에서는 공용
    seam이 loud warning과 함께 filesystem 폴백한다.
    """
    repo_files = _load_repo_owned_files()
    root = root.resolve()
    return [
        path
        for relative in repo_files.list_repo_owned_files(
            root, Path("."), mode=repo_files.OWNED
        )
        if (path := root / relative).is_file()
    ]


def _load_pm_update():
    """``pm_update`` 를 같은 tools/ 에서 형제 로드한다.

    ``pm_update.py`` 는 stamped 모듈이 아니라(``ENGINE_REV`` 미보유) skew 검증을 걸지 않는다 —
    ``pm_update.py`` 자신이 ``pm_render``/``pm_import`` 를 로드할 때 쓰는 것과 같은
    ``allow_unverified=True`` 관례다. 이 함수는 manifest 주석 경계(``_parse_manifest_line``)와
    루트 목적지 사본 판정(``manifest_entry_shipping_inventory``)에 쓰인다 — 판정 사본을 새로
    쓰지 않고 엔진의 manifest 파서·전개식을 그대로 호출한다.
    """
    path = REPO / ".project_manager" / "tools" / "pm_update.py"
    return _load_module_from_path(
        path,
        "pm_update.py",
        allow_unverified=True,
        cache=True,
        cache_key=f"_private_context_pm_update:{path.resolve()}",
    )


# ── 언어 축 ────────────────────────────────────────────────────────────────
# python·markdown 을 뺀 출하 표면 — 셸·JS·TOML·manifest 등 산문 경계가 언어마다 다른 축.
LANGUAGE_NOEXT = "noext"
LANGUAGE_SH = "sh"
LANGUAGE_CMD = "cmd"
LANGUAGE_TOML = "toml"
LANGUAGE_JS = "js"
LANGUAGE_MANIFEST = "manifest"
LANGUAGE_JSON = "json"
LANGUAGE_JSONC = "jsonc"
LANGUAGE_RULES = "rules"
LANGUAGE_TXT = "txt"

# 확장자 → 언어. 여기 없는 확장자는 ``language_of`` 가 예외를 낸다(fail-loud·조용한 통과 0) —
# "아직 안 덮는 축" 목록을 두지 않는다(시야 정의가 두 벌이 되는 것을 막는다).
_LANGUAGE_BY_SUFFIX: dict[str, str] = {
    "": LANGUAGE_NOEXT,
    ".sh": LANGUAGE_SH,
    ".cmd": LANGUAGE_CMD,
    ".toml": LANGUAGE_TOML,
    ".cjs": LANGUAGE_JS,
    ".js": LANGUAGE_JS,
    ".manifest": LANGUAGE_MANIFEST,
    ".json": LANGUAGE_JSON,
    ".jsonc": LANGUAGE_JSONC,
    ".rules": LANGUAGE_RULES,
    ".txt": LANGUAGE_TXT,
}

# 루트가 자기 자신을 dest 로 두는 manifest 항목 전개로는 안 잡히는, manifest 미등재
# 루트 목적지 사본. ``.claude/run_tests_hook.sh`` 는 제거 예정 잔재(PM 결정)라 정식
# manifest 항목으로 등재하지 않되, 판정 대상에는 넣는다 — ``shipping_paths`` 의
# ``CLAUDE.md`` 단일 경로 특례와 같은 자리다.
_UNREGISTERED_ROOT_DESTINATIONS: tuple[Path, ...] = (Path(".claude/run_tests_hook.sh"),)


def _root_destination_language_paths(root: Path) -> set[Path]:
    """루트 자신을 dest 로 삼는 manifest 항목을 전개해 언어 축 루트 목적지 사본을 찾는다.

    ``shipping_paths`` 의 python 축은 templates 상대경로 일치로 루트 사본(``ctx_guard.py`` 등)을
    찾지만, 그 트릭을 확장자 전체로 일반화하면 우연히 같은 상대경로를 쓰는 인스턴스-소유 파일
    (루트 ``.gitignore``·``.claude/settings.json`` — engine.manifest 가 "미등록·전파 안 함"이라
    문서화한 예외)까지 오분류한다. 대신 ``pm_update`` 가 실제 update 계획에 쓰는
    ``manifest_entry_shipping_inventory`` 를 dest=root=source 로 호출해 **루트 자신의 manifest 가
    실제로 선언한** 항목만 전개한다(판정 사본 0) — 인스턴스-소유 예외는애초에 manifest 미등재라
    자동으로 빠진다.
    """
    pm_update = _load_pm_update()
    resolved_root = root.resolve()
    manifest_path = pm_update.resolve_manifest_for_dest(resolved_root, resolved_root)
    manifest = pm_update.read_manifest(manifest_path)
    found: set[Path] = set()
    for entry_index in range(len(manifest)):
        shipped, _missing, _target_owned = pm_update.manifest_entry_shipping_inventory(
            resolved_root, manifest, entry_index, resolved_root
        )
        for dest_relative, _source in shipped:
            destination = resolved_root / dest_relative
            if destination.suffix in (".py", ".md"):
                continue
            if destination.is_file():
                found.add(destination)
    for relative in _UNREGISTERED_ROOT_DESTINATIONS:
        candidate = resolved_root / relative
        if candidate.is_file():
            found.add(candidate)
    return found


def language_paths(root: Path) -> list[Path]:
    """출하 표면 − python − markdown. ``shipping_paths`` 와 같은 ``repo_owned_paths`` 열거에서
    파생한다(두 번째 열거 함수를 신설하지 않는다).

    templates 트리 아래 python·markdown 이 아닌 모든 파일 + 그 상대경로를 실제로 manifest 가
    루트 목적지로 선언한 사본(``_root_destination_language_paths``). 확장자 등록 여부는 여기서
    보지 않는다 — 미등록 확장자는 ``language_of``/``language_prose_spans`` 호출 시점에 fail-loud
    한다(시야를 조용히 좁히지 않는다).
    """
    resolved_root = root.resolve()
    python_paths, markdown_paths = shipping_paths(root)
    excluded = set(python_paths) | set(markdown_paths)
    found: set[Path] = set()
    for path in repo_owned_paths(root):
        if path in excluded:
            continue
        relative = path.relative_to(resolved_root)
        parts = relative.parts
        if len(parts) < 3 or parts[0] != "templates":
            continue
        found.add(path)
    found |= _root_destination_language_paths(root) - excluded
    return sorted(found)


def language_of(path: Path) -> str:
    """확장자 → 언어. 등록되지 않은 확장자는 예외를 낸다(조용한 통과 0)."""
    suffix = Path(path).suffix
    try:
        return _LANGUAGE_BY_SUFFIX[suffix]
    except KeyError:
        raise ValueError(
            f"language_of: 미등록 확장자 {suffix!r} ({path}) — "
            "_LANGUAGE_BY_SUFFIX 에 언어와 산문 경계를 등록하라"
        ) from None


def _noext_prose_spans(source: str) -> list[tuple[int, int]]:
    """라인 선두 ``#`` 만 — gitignore/gitattributes/gitkeep 은 인라인 주석 문법이 없다."""
    return [
        (start, end)
        for start, end, text in _line_records(source)
        if text.lstrip().startswith("#")
    ]


def _cmd_prose_spans(source: str) -> list[tuple[int, int]]:
    """``rem``(대소문자 무관·단어 경계) 또는 ``::`` 로 시작하는 라인 전체."""
    spans: list[tuple[int, int]] = []
    for start, end, text in _line_records(source):
        stripped = text.lstrip()
        if stripped.startswith("::"):
            spans.append((start, end))
            continue
        lower = stripped[:3].lower()
        if lower == "rem" and (len(stripped) == 3 or stripped[3] in " \t"):
            spans.append((start, end))
    return spans


def _quote_aware_hash_comment_spans(source: str) -> list[tuple[int, int]]:
    """따옴표(``'``·``"``) 안이 아니고 **단어 경계**(라인 선두·공백 뒤)인 ``#`` 부터 줄 끝까지.

    ``.sh``·``.rules`` 공용 — 둘 다 ``#`` 한 줄 주석 + 따옴표 문자열 구문이다. 단어 경계 규칙이
    ``${VAR#pat}``(파라미터 확장)·``pattern#literal`` 같은 임베디드 ``#`` 를 주석에서 제외한다.
    따옴표 안(작은따옴표는 리터럴·큰따옴표는 백슬래시 이스케이프)의 ``#`` 는 애초에 후보에서 뺀다
    — ``$(py -c '...')`` 안 임베디드 python 주석은 문자열 데이터로 본다(설계 §회색지대).
    """
    spans: list[tuple[int, int]] = []
    length = len(source)
    index = 0
    quote: str | None = None
    at_boundary = True
    comment_start: int | None = None
    while index < length:
        char = source[index]
        if comment_start is not None:
            if char == "\n":
                spans.append((comment_start, index))
                comment_start = None
                at_boundary = True
            index += 1
            continue
        if quote == "'":
            if char == "'":
                quote = None
            index += 1
            at_boundary = False
            continue
        if quote == '"':
            if char == "\\" and index + 1 < length:
                index += 2
            else:
                if char == '"':
                    quote = None
                index += 1
            at_boundary = False
            continue
        if char == "'" or char == '"':
            quote = char
            index += 1
            at_boundary = False
            continue
        if char == "\\" and index + 1 < length:
            index += 2
            at_boundary = False
            continue
        if char == "#" and at_boundary:
            comment_start = index
            index += 1
            continue
        if char in " \t":
            index += 1
            continue
        if char == "\n":
            at_boundary = True
            index += 1
            continue
        at_boundary = False
        index += 1
    if comment_start is not None:
        spans.append((comment_start, length))
    return spans


def _toml_prose_spans(source: str) -> list[tuple[int, int]]:
    """따옴표 인식 ``#`` 주석 + 문자열 값(단일·삼중 따옴표 모두). 키/값을 가리지 않는다.

    에이전트 카드의 ``description``·프롬프트 문자열은 채택자가 읽는 출하 산문이라 데이터로
    면제하지 않는다(python 축의 argparse ``help=`` 문자열과 같은 클래스).
    """
    spans: list[tuple[int, int]] = []
    length = len(source)
    index = 0
    while index < length:
        char = source[index]
        if char == "#":
            newline = source.find("\n", index)
            end = newline if newline != -1 else length
            spans.append((index, end))
            index = end
            continue
        if source.startswith("'''", index) or source.startswith('"""', index):
            delimiter = source[index:index + 3]
            search_from = index + 3
            end = length
            while True:
                found = source.find(delimiter, search_from)
                if found == -1:
                    end = length
                    break
                if delimiter[0] == "'":
                    end = found + 3
                    break
                backslashes = 0
                cursor = found - 1
                while cursor >= 0 and source[cursor] == "\\":
                    backslashes += 1
                    cursor -= 1
                if backslashes % 2 == 0:
                    end = found + 3
                    break
                search_from = found + 3
            spans.append((index, end))
            index = end
            continue
        if char == "'":
            found = source.find("'", index + 1)
            end = found + 1 if found != -1 else length
            spans.append((index, end))
            index = end
            continue
        if char == '"':
            cursor = index + 1
            while cursor < length:
                if source[cursor] == "\\":
                    cursor += 2
                    continue
                if source[cursor] == '"':
                    cursor += 1
                    break
                cursor += 1
            spans.append((index, cursor))
            index = cursor
            continue
        index += 1
    return spans


def _json_string_spans(source: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    length = len(source)
    index = 0
    while index < length:
        if source[index] == '"':
            cursor = index + 1
            while cursor < length:
                if source[cursor] == "\\":
                    cursor += 2
                    continue
                if source[cursor] == '"':
                    cursor += 1
                    break
                cursor += 1
            spans.append((index, cursor))
            index = cursor
            continue
        index += 1
    return spans


def _json_value_spans(source: str) -> list[tuple[int, int]]:
    """문자열 값(키가 아닌 문자열)만 — JSON 은 주석 문법이 없다.

    문자열이 끝난 뒤 첫 비-공백 문자가 ``:`` 면 키(판정면 아님), 그 외(``,``·``}``·``]``)면
    값이다.
    """
    spans: list[tuple[int, int]] = []
    length = len(source)
    for start, end in _json_string_spans(source):
        cursor = end
        while cursor < length and source[cursor] in " \t\r\n":
            cursor += 1
        is_key = cursor < length and source[cursor] == ":"
        if not is_key:
            spans.append((start, end))
    return spans


def _jsonc_prose_spans(source: str) -> list[tuple[int, int]]:
    """JSON 문자열 값 판정 + ``//`` 한 줄 주석(따옴표 안은 후보에서 제외)."""
    spans: list[tuple[int, int]] = []
    length = len(source)
    index = 0
    while index < length:
        char = source[index]
        if char == '"':
            cursor = index + 1
            while cursor < length:
                if source[cursor] == "\\":
                    cursor += 2
                    continue
                if source[cursor] == '"':
                    cursor += 1
                    break
                cursor += 1
            lookahead = cursor
            while lookahead < length and source[lookahead] in " \t\r\n":
                lookahead += 1
            is_key = lookahead < length and source[lookahead] == ":"
            if not is_key:
                spans.append((index, cursor))
            index = cursor
            continue
        if char == "/" and index + 1 < length and source[index + 1] == "/":
            newline = source.find("\n", index)
            end = newline if newline != -1 else length
            spans.append((index, end))
            index = end
            continue
        index += 1
    return spans


# JS/CJS 의 정규식-리터럴 판정 — 이 앞 유의미 토큰이 이 집합이면 ``/`` 는 나눗셈이 아니라
# 정규식 시작이다(직전 토큰이 없음=파일/블록 시작도 포함).
_JS_REGEX_PRECEDING_PUNCTUATION = set("([{,;:=!&|?+-*%^~<>\n")
_JS_REGEX_PRECEDING_KEYWORDS = {
    "return", "typeof", "instanceof", "in", "of", "new", "delete", "void",
    "throw", "case", "do", "else", "yield", "await",
}


def _js_skip_string(source: str, index: int, quote: str) -> int:
    length = len(source)
    cursor = index + 1
    while cursor < length:
        if source[cursor] == "\\":
            cursor += 2
            continue
        if source[cursor] == quote:
            return cursor + 1
        cursor += 1
    return length


def _js_skip_template(source: str, index: int) -> int:
    """백틱 문자열 하나를 건너뛴다. ``${...}`` 보간 안 중괄호 깊이만 추적한다(중첩 백틱은 단순
    처리 — 표면 상한 안에서 실 코퍼스가 요구하는 정밀도만 갖춘다)."""
    length = len(source)
    cursor = index + 1
    while cursor < length:
        char = source[cursor]
        if char == "\\":
            cursor += 2
            continue
        if char == "`":
            return cursor + 1
        if char == "$" and cursor + 1 < length and source[cursor + 1] == "{":
            cursor += 2
            depth = 1
            while cursor < length and depth > 0:
                inner = source[cursor]
                if inner == "{":
                    depth += 1
                    cursor += 1
                elif inner == "}":
                    depth -= 1
                    cursor += 1
                elif inner in "'\"":
                    cursor = _js_skip_string(source, cursor, inner)
                elif inner == "`":
                    cursor = _js_skip_template(source, cursor)
                elif inner == "/" and cursor + 1 < length and source[cursor + 1] == "/":
                    newline = source.find("\n", cursor)
                    cursor = newline if newline != -1 else length
                elif inner == "/" and cursor + 1 < length and source[cursor + 1] == "*":
                    end_block = source.find("*/", cursor + 2)
                    cursor = end_block + 2 if end_block != -1 else length
                else:
                    cursor += 1
            continue
        cursor += 1
    return length


def _js_prose_spans(source: str) -> list[tuple[int, int]]:
    """``//``·``/* */`` 주석 — 문자열·템플릿·정규식 리터럴을 인식해 그 안의 동형 글자를
    주석 시작으로 오판하지 않는다(``//`` 로 시작하는 문자열 리터럴·슬래시를 포함한 정규식 등)."""
    spans: list[tuple[int, int]] = []
    length = len(source)
    index = 0
    prev_token = ""
    while index < length:
        char = source[index]
        if char == "/" and index + 1 < length and source[index + 1] == "/":
            newline = source.find("\n", index)
            end = newline if newline != -1 else length
            spans.append((index, end))
            index = end
            prev_token = ""
            continue
        if char == "/" and index + 1 < length and source[index + 1] == "*":
            end_block = source.find("*/", index + 2)
            end = end_block + 2 if end_block != -1 else length
            spans.append((index, end))
            index = end
            prev_token = ""
            continue
        if char == "'" or char == '"':
            index = _js_skip_string(source, index, char)
            prev_token = "STRING"
            continue
        if char == "`":
            index = _js_skip_template(source, index)
            prev_token = "STRING"
            continue
        if char == "/":
            is_regex_context = (
                prev_token == ""
                or prev_token in _JS_REGEX_PRECEDING_KEYWORDS
                or (len(prev_token) == 1 and prev_token in _JS_REGEX_PRECEDING_PUNCTUATION)
            )
            if is_regex_context:
                cursor = index + 1
                in_class = False
                while cursor < length:
                    inner = source[cursor]
                    if inner == "\\":
                        cursor += 2
                        continue
                    if inner == "\n":
                        break
                    if inner == "[":
                        in_class = True
                    elif inner == "]":
                        in_class = False
                    elif inner == "/" and not in_class:
                        cursor += 1
                        break
                    cursor += 1
                while cursor < length and source[cursor].isalpha():
                    cursor += 1
                index = cursor
                prev_token = "REGEX"
                continue
            index += 1
            prev_token = "/"
            continue
        if char.isspace():
            index += 1
            continue
        if char.isalnum() or char in "_$":
            cursor = index
            while cursor < length and (source[cursor].isalnum() or source[cursor] in "_$"):
                cursor += 1
            prev_token = source[index:cursor]
            index = cursor
            continue
        prev_token = char
        index += 1
    return spans


def _manifest_prose_spans(source: str) -> list[tuple[int, int]]:
    """엔진 파서 ``pm_update._parse_manifest_line`` 이 ``None`` 을 내는 비-빈 라인 전체.

    새 정규식을 쓰지 않는다(판정 사본 금지) — 그 함수가 항상 ``None`` 을 내도록 바뀌면
    데이터 행까지 산문으로 잡혀 manifest 축 판정이 뒤집힌다(값 확인은 회귀가 한다).
    """
    pm_update = _load_pm_update()
    spans: list[tuple[int, int]] = []
    for start, end, text in _line_records(source):
        if not text.strip():
            continue
        if pm_update._parse_manifest_line(text) is None:
            spans.append((start, end))
    return spans


_LANGUAGE_PROSE_SCANNERS = {
    LANGUAGE_NOEXT: _noext_prose_spans,
    LANGUAGE_SH: _quote_aware_hash_comment_spans,
    LANGUAGE_CMD: _cmd_prose_spans,
    LANGUAGE_TOML: _toml_prose_spans,
    LANGUAGE_JS: _js_prose_spans,
    LANGUAGE_MANIFEST: _manifest_prose_spans,
    LANGUAGE_JSON: _json_value_spans,
    LANGUAGE_JSONC: _jsonc_prose_spans,
    LANGUAGE_RULES: _quote_aware_hash_comment_spans,
    LANGUAGE_TXT: lambda source: [(0, len(source))],
}


def language_prose_spans(path: Path, source: str) -> list[TokenSpan]:
    """언어별 산문 구간 — ``language_of`` 로 언어를 정하고 등록된 분류기를 호출한다."""
    language = language_of(path)
    scanner = _LANGUAGE_PROSE_SCANNERS[language]
    return [
        TokenSpan(
            start=start,
            end=end,
            line=1 + source.count("\n", 0, start),
            text=source[start:end],
        )
        for start, end in scanner(source)
    ]


def shipping_paths(root: Path) -> tuple[list[Path], list[Path]]:
    """private-context 출하 표면을 repo-owned 열거 결과에서 분류한다.

    ``dashboard.md``와 ``pm_state.md`` 같은 per-clone 파생 파일은 경로별 예외가
    아니라 git ignore/소유 판정으로 빠진다. 따라서 앞으로 다른 파생 파일이 생겨도
    같은 규칙이 적용되고, 비-ignore 신규 출하 파일은 계속 검사한다.

    python 축은 루트 canonical 엔진(``.project_manager/tools/*.py``) +
    ``templates/<harness>/`` 트리 전체의 모든 python(엔진 사본 + 어댑터, 깊이 무관) +
    templates canonical 과 같은 상대경로를 공유하는 루트 목적지 사본(예:
    ``.claude/ctx_guard.py`` — ``engine.manifest`` 의 ``@source=`` 목적지가 이 트리 안에
    실재하면 그것도 출하 표면이라는 규칙의 파생)이다. 마지막 항은 경로를 하드코딩하지
    않고 templates 열거 결과에서 상대경로 대조로 도출한다.
    """
    resolved_root = root.resolve()
    owned = repo_owned_paths(root)
    python_paths: set[Path] = set()
    markdown_paths: set[Path] = set()
    template_python_relpaths: set[Path] = set()
    for path in owned:
        relative = path.relative_to(resolved_root)
        parts = relative.parts
        if relative == Path("CLAUDE.md"):
            markdown_paths.add(path)
            continue
        if (
            path.suffix == ".py"
            and relative.parent == Path(".project_manager", "tools")
        ):
            python_paths.add(path)
            continue
        if (
            path.suffix == ".md"
            and len(parts) >= 3
            and parts[0] == ".project_manager"
            and parts[1] == "wiki"
        ):
            markdown_paths.add(path)
            continue
        if path.suffix == ".md" and parts and parts[0] == ".claude":
            markdown_paths.add(path)
            continue
        if len(parts) < 3 or parts[0] != "templates":
            continue
        if path.suffix == ".md":
            markdown_paths.add(path)
        elif path.suffix == ".py":
            python_paths.add(path)
            template_python_relpaths.add(Path(*parts[2:]))
    # 루트 목적지 사본 — templates canonical 과 상대경로가 일치하는 루트 python 만
    # 편입한다(파생·하드코딩 0). 오늘 값: `.claude/ctx_guard.py`·`.claude/ctx_stop_hook.py`.
    for path in owned:
        if path.suffix != ".py" or path in python_paths:
            continue
        relative = path.relative_to(resolved_root)
        if relative.parts and relative.parts[0] == "templates":
            continue
        if relative in template_python_relpaths:
            python_paths.add(path)
    return sorted(python_paths), sorted(markdown_paths)


def _line_offsets(text: str) -> list[int]:
    offsets = [0]
    for match in re.finditer("\n", text):
        offsets.append(match.end())
    return offsets


def _offset(offsets: list[int], position: tuple[int, int]) -> int:
    line, column = position
    return offsets[line - 1] + column


def _ast_column_to_character(source_line: str, byte_column: int) -> int:
    return len(source_line.encode("utf-8")[:byte_column].decode("utf-8"))


def _line_records(source: str) -> list[tuple[int, int, str]]:
    """라인마다 (시작 offset, 개행 포함 끝 offset, 본문) 을 낸다.

    ``tokenize`` 와 같은 줄 나눔(``\\n`` 기준)을 써서 토큰의 라인 번호를 그대로 색인으로
    쓸 수 있게 한다.
    """
    records: list[tuple[int, int, str]] = []
    position = 0
    for line in source.split("\n"):
        start = position
        position += len(line) + 1
        records.append((start, min(position, len(source)), line.rstrip("\r")))
    return records


def _merge_spans(spans: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _subtract_spans(
    start: int, end: int, protected: Iterable[tuple[int, int]]
) -> list[tuple[int, int]]:
    """``[start, end)`` 에서 보호 구간을 뺀 나머지를 낸다.

    여러 줄 토큰이 보호 구간과 일부만 겹칠 때 토큰 전체를 버리면 표식이 없는 나머지 줄까지
    스트립·검사에서 빠진다. 그래서 겹치는 부분만 도려낸다.
    """
    remaining = [(start, end)]
    for span_start, span_end in protected:
        pieces: list[tuple[int, int]] = []
        for piece_start, piece_end in remaining:
            if span_end <= piece_start or piece_end <= span_start:
                pieces.append((piece_start, piece_end))
                continue
            if piece_start < span_start:
                pieces.append((piece_start, span_start))
            if span_end < piece_end:
                pieces.append((span_end, piece_end))
        remaining = pieces
    return [(piece_start, piece_end) for piece_start, piece_end in remaining
            if piece_end > piece_start]


def _data_literal_markers(
    source: str, records: list[tuple[int, int, str]]
) -> tuple[set[int], list[tuple[int, str]]]:
    """표식 주석을 (라인 표식이 가리키는 라인, 블록 경계 목록) 으로 분류한다.

    표식은 **실제 주석 토큰**일 때만 인정한다 — 문자열 리터럴 안의 같은 글자는 그 자체가
    데이터지 표식이 아니다. 원문 정규식으로 보면 리터럴 한 줄로 스트립·재유입 검사를
    통째로 우회할 수 있고, 여러 줄 문자열 안의 경계 글자가 짝 불일치를 내기도 한다.
    """
    marked: set[int] = set()
    edges: list[tuple[int, str]] = []
    comments: list[tokenize.TokenInfo] = []
    statement_end: dict[int, int] = {}
    opened: int | None = None
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            comments.append(token)
        elif token.type == tokenize.NEWLINE:
            if opened is not None:
                statement_end[opened] = token.end[0]
                opened = None
        elif token.type not in _NON_STATEMENT_TOKENS and opened is None:
            opened = token.start[0]
    for token in comments:
        if not _DATA_LITERAL_PREFIX_RE.match(token.string):
            continue
        index = token.start[0] - 1
        standalone = not records[index][2][: token.start[1]].strip()
        edge = _DATA_LITERAL_BLOCK_RE.match(token.string)
        if edge:
            # 블록 경계는 단독 라인일 때만 구획을 연다 — 코드 뒤 표식은 경계가 아니다.
            if standalone:
                edges.append((index, edge.group("edge")))
            continue
        if not _DATA_LITERAL_LINE_RE.match(token.string):
            # 오탈자 표식은 보호를 만들지 못한 채 조용히 지나가 원래 사고를 재현한다.
            raise DataLiteralMarkerError(
                f"line {index + 1}: 표식으로 보이는 주석이 정확 형태가 아니다 "
                f"({token.string.strip()!r}) — 라인 표식은 '# {_DATA_LITERAL_MARKER}'"
                f"(설명은 그 뒤에), 블록 경계는 단독 라인의 "
                f"'# {_DATA_LITERAL_MARKER}:begin' / ':end' 만 인정한다"
            )
        marked.add(index)
        if not standalone:
            continue
        # 표식만 있는 라인은 다음 **문장 전체**를 가리킨다 — 여러 줄 선언 위에 붙였을 때
        # 첫 줄만 보호되면 나머지 줄이 조용히 정리된다.
        following = index + 2
        end_line = statement_end.get(following, following)
        marked.update(range(following - 1, min(end_line, len(records))))
    return marked, edges


def _data_literal_block_spans(
    records: list[tuple[int, int, str]], edges: list[tuple[int, str]]
) -> list[tuple[int, int]]:
    """블록 경계를 짝지어 구간으로 만든다 — 짝이 안 맞으면 즉시 실패한다.

    반만 남은 표식은 보호 구간이 사라졌다는 뜻인데 조용히 통과하면 아무도 모른다.
    """
    spans: list[tuple[int, int]] = []
    open_index: int | None = None
    for index, edge in edges:
        if edge == "begin":
            if open_index is not None:
                raise DataLiteralMarkerError(
                    f"line {index + 1}: {_DATA_LITERAL_MARKER}:begin 이 line "
                    f"{open_index + 1} 의 열린 블록 안에서 다시 열렸다 — 짝을 맞춰라"
                )
            open_index = index
        elif open_index is None:
            raise DataLiteralMarkerError(
                f"line {index + 1}: {_DATA_LITERAL_MARKER}:end 에 짝이 되는 "
                f"{_DATA_LITERAL_MARKER}:begin 이 없다"
            )
        else:
            spans.append((records[open_index][0], records[index][1]))
            open_index = None
    if open_index is not None:
        raise DataLiteralMarkerError(
            f"line {open_index + 1}: {_DATA_LITERAL_MARKER}:begin 이 닫히지 않았다 — "
            f"{_DATA_LITERAL_MARKER}:end 를 붙여라"
        )
    return spans


def _data_literal_spans(source: str) -> list[tuple[int, int]]:
    """소스 주석 표식으로 데이터 선언임을 밝힌 라인 구간을 낸다.

    두 형태를 데이터로 판정한다 — 같은 라인 또는 바로 앞 라인에 표식 주석이 붙은 리터럴
    라인 전체, 그리고 ``begin``/``end`` 표식으로 감싼 여러 줄 구간. 스트립과 재유입 가드가
    이 한 함수를 함께 소비하므로 한쪽만 아는 판정이 생기지 않는다.

    표식이 있는 소스는 개행이 LF 여야 한다 — 홀로 선 CR 은 라인 판정을 어긋나게 해 구간이
    의도한 라인 밖까지 번지고, 그 안의 사설 참조가 조용히 검사에서 빠진다.
    """
    if _DATA_LITERAL_MARKER not in source:
        return []
    lone_carriage_return = _LONE_CARRIAGE_RETURN_RE.search(source)
    if lone_carriage_return is not None:
        raise DataLiteralMarkerError(
            f"offset {lone_carriage_return.start()}: 표식이 있는 소스에 홀로 선 CR 이 "
            f"있다 — 라인 판정이 어긋나 보호 구간이 의도 밖까지 번진다. "
            f"표식·경계가 있는 파일은 개행을 LF 로 통일하라"
        )
    records = _line_records(source)
    marked, edges = _data_literal_markers(source, records)
    spans = _data_literal_block_spans(records, edges)
    spans.extend((records[index][0], records[index][1]) for index in sorted(marked))
    return _merge_spans(spans)


def _doc_expression_ranges(
    source: str,
) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    tree = ast.parse(source)
    source_lines = source.splitlines(keepends=True)
    return [
        (
            (
                node.value.lineno,
                _ast_column_to_character(
                    source_lines[node.value.lineno - 1], node.value.col_offset
                ),
            ),
            (
                node.value.end_lineno,
                _ast_column_to_character(
                    source_lines[node.value.end_lineno - 1],
                    node.value.end_col_offset,
                ),
            ),
        )
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        and node.value.end_lineno is not None
        and node.value.end_col_offset is not None
    ]


def _prose_token_ranges(source: str) -> list[tuple[int, int, int]]:
    """산문 토큰의 원본 (시작 offset, 끝 offset, 시작 라인) 목록."""
    offsets = _line_offsets(source)
    doc_ranges = _doc_expression_ranges(source)
    ranges: list[tuple[int, int, int]] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        is_prose = token.type == tokenize.COMMENT
        is_prose = is_prose or (
            token.type == tokenize.STRING
            and any(
                start <= token.start and token.end <= end
                for start, end in doc_ranges
            )
        )
        if is_prose:
            ranges.append(
                (
                    _offset(offsets, token.start),
                    _offset(offsets, token.end),
                    token.start[0],
                )
            )
    return ranges


def prose_context_spans(source: str) -> list[TokenSpan]:
    """분할하지 않은 산문 토큰 구간 — 여러 줄에 걸친 판정의 문맥이다.

    개행을 삼킬 수 있는 패턴(절 참조·세션 표기)은 데이터 구간에서 잘라낸 조각만 보면 경계를
    걸친 출현을 놓친다. 그래서 판정하는 쪽은 이 원본 문맥을 읽고, 면제는 데이터 구간에
    **완전히 포함된** 출현만으로 판단한다.
    """
    return [
        TokenSpan(start=start, end=end, line=line, text=source[start:end])
        for start, end, line in _prose_token_ranges(source)
    ]


def prose_token_spans(source: str) -> list[TokenSpan]:
    """Return comment and documentation-expression spans in Python source.

    데이터 구간의 주석·문서는 산문이 아니라 마커 선언의 일부다. 일부만 겹치는 여러 줄
    토큰은 겹친 라인만 빼고 나머지 라인은 계속 산문으로 본다. 고쳐 쓰는 쪽(스트립)이
    쓰는 뷰이며, 여러 줄 문맥이 필요한 판정은 ``prose_context_spans`` 를 쓴다.
    """
    data_spans = _data_literal_spans(source)
    spans: list[TokenSpan] = []
    for token_start, token_end, token_line in _prose_token_ranges(source):
        for start, end in _subtract_spans(token_start, token_end, data_spans):
            spans.append(
                TokenSpan(
                    start=start,
                    end=end,
                    # 라인 번호는 토큰 안 개행만 세어 구한다(소스 전체 재스캔 회피).
                    line=token_line + source.count("\n", token_start, start),
                    text=source[start:end],
                )
            )
    return spans


def _specific_matches(text: str) -> list[re.Match[str]]:
    protected = [
        (match.start(), match.end()) for match in _INLINE_CODE_RE.finditer(text)
    ]
    return [
        match
        for match in SPECIFIC_REF_RE.finditer(text)
        if not any(start <= match.start() < end for start, end in protected)
    ]


def _nearest_left_boundary(text: str, start: int) -> int:
    return max((text.rfind(char, 0, start) for char in "·(\"'#"), default=-1)


def _nearest_right_boundary(text: str, end: int) -> int:
    candidates = [
        position
        for char in "·)\"'"
        if (position := text.find(char, end)) >= 0
    ]
    return min(candidates, default=len(text))


def _is_dot_unit(text: str, match: re.Match[str]) -> bool:
    left = _nearest_left_boundary(text, match.start())
    right = _nearest_right_boundary(text, match.end())
    segment = text[left + 1:right].strip(" \t")
    has_dot_boundary = (
        (left >= 0 and text[left] == "·")
        or (right < len(text) and text[right] == "·")
    )
    return has_dot_boundary and segment == match.group()


def _actionable_matches(text: str) -> list[re.Match[str]]:
    """Return references in delimiter units that pass line safety checks."""
    actionable: list[re.Match[str]] = []
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        _, removed, spans = _line_rewrite_plan(body)
        if not removed:
            continue
        actionable.extend(
            match
            for match in _specific_matches(body)
            if any(span.start <= match.start() < span.end for span in spans)
        )
    return actionable


def line_delta_counts(line: str) -> dict[str, int]:
    """Count mechanically invalid boundary patterns on one line."""
    return {
        name: sum(1 for _ in pattern.finditer(line))
        for name, pattern in _DELTA_PATTERNS
    }


def line_delta(before: str, after: str) -> dict[str, dict[str, int]]:
    """Return before/after/delta counts for one changed line."""
    before_counts = line_delta_counts(before)
    after_counts = line_delta_counts(after)
    return {
        name: {
            "before": before_counts[name],
            "after": after_counts[name],
            "delta": after_counts[name] - before_counts[name],
        }
        for name, _ in _DELTA_PATTERNS
    }


def _expand_unit_span(text: str, start: int, end: int) -> tuple[int, int]:
    left = start
    while left > 0 and text[left - 1] in " \t":
        left -= 1
    if left < start and text[:left].strip(" \t"):
        return left, end

    right = end
    while right < len(text) and text[right] in " \t":
        right += 1
    if right > end and text[right:].strip(" \t"):
        return start, right
    return start, end


def _candidate_removal_spans(text: str) -> list[RemovalSpan]:
    protected = [
        (match.start(), match.end()) for match in _INLINE_CODE_RE.finditer(text)
    ]
    spans: list[RemovalSpan] = []
    covered_references: set[int] = set()

    for wrapper in _REFERENCE_ONLY_WRAPPER_RE.finditer(text):
        if any(
            start < wrapper.end() and wrapper.start() < end
            for start, end in protected
        ):
            continue
        matches = list(SPECIFIC_REF_RE.finditer(wrapper.group()))
        if not matches:
            continue
        start, end = _expand_unit_span(text, wrapper.start(), wrapper.end())
        spans.append(RemovalSpan(start, end, len(matches)))
        covered_references.update(
            range(wrapper.start(), wrapper.end())
        )

    for match in _specific_matches(text):
        if match.start() in covered_references or not _is_dot_unit(text, match):
            continue
        left = _nearest_left_boundary(text, match.start())
        right = _nearest_right_boundary(text, match.end())
        if right < len(text) and text[right] == "·":
            start, end = match.start(), right + 1
        elif left >= 0 and text[left] == "·":
            start, end = left, match.end()
        else:
            continue
        start, end = _expand_unit_span(text, start, end)
        spans.append(RemovalSpan(start, end, 1))

    spans.sort(key=lambda span: (span.start, span.end))
    merged: list[RemovalSpan] = []
    for span in spans:
        if merged and span.start < merged[-1].end:
            previous = merged[-1]
            merged[-1] = RemovalSpan(
                previous.start,
                max(previous.end, span.end),
                previous.references + span.references,
            )
        else:
            merged.append(span)
    return merged


def remove_matched_spans(
    text: str, spans: Iterable[RemovalSpan] | None = None
) -> str:
    """Delete only the exact matched unit spans from the original text."""
    if spans is None:
        spans = _candidate_removal_spans(text)
    cleaned = text
    for span in reversed(list(spans)):
        cleaned = cleaned[:span.start] + cleaned[span.end:]
    return cleaned


def _leading_whitespace(text: str) -> str:
    match = re.match(r"[ \t]*", text)
    assert match is not None
    return match.group()


def _result_payload(text: str) -> str:
    payload = text.lstrip(" \t")
    if payload.startswith("#"):
        payload = payload[1:].lstrip(" \t")
    quote = re.match(r"(?i:[rubf]*)(?:'''|\"\"\"|'|\")", payload)
    if quote:
        payload = payload[quote.end():].lstrip(" \t")
    return payload


def _unsafe_result(before: str, after: str) -> bool:
    if _leading_whitespace(before) != _leading_whitespace(after):
        return True
    payload = _result_payload(after)
    if not any(character.isalnum() or character == "_" for character in payload):
        return True
    spans = _candidate_removal_spans(before)
    prefix = before[:spans[0].start] if spans else before
    prefix_has_content = any(
        character.isalnum() or character == "_"
        for character in _result_payload(prefix)
    )
    return not prefix_has_content and payload.startswith(
        (".", ",", ";", ":", "!", "?", "·", "—", "–", "…", ")", "]", "}")
    )


def _line_rewrite_plan(text: str) -> tuple[str, int, list[RemovalSpan]]:
    spans = _candidate_removal_spans(text)
    if not spans:
        return text, 0, []
    cleaned = remove_matched_spans(text, spans)
    assert cleaned == remove_matched_spans(text)
    if _unsafe_result(text, cleaned):
        return text, 0, []
    if any(counts["delta"] > 0 for counts in line_delta(text, cleaned).values()):
        return text, 0, []
    return cleaned, sum(span.references for span in spans), spans


def _prose_count(source: str) -> int:
    return sum(
        len(_specific_matches(span.text))
        for span in prose_token_spans(source)
    )
