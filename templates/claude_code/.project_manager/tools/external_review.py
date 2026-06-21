#!/usr/bin/env python3
"""외부 코드리뷰 래퍼 — 외부 리뷰어 어댑터 CLI (ADR-0004).

사용:
    python3 .project_manager/tools/external_review.py [옵션]

동작:
  ① git diff <base> -- <paths> 추출 (시크릿 denylist 경로 자동 제외)
  ② (프로젝트 맥락 헤더 +) diff 결합 → 표준 프롬프트 생성
  ③ 외부 리뷰어 실행 (reviewer_cmd, stdin 으로 프롬프트 주입, read-only 권장)
  ④ 출력에서 판정(통과/반려)·must-fix 파싱
  ⑤ 결과 요약 stdout + 원문 파일 저장 (/tmp 또는 --output-dir)

기본 비활성 (ADR-0004):
  - 코드 diff 가 *외부로 전송*되므로 기본 OFF. local.conf `external_review_enabled=true`
    또는 `board.py init` / `pm_update` 시 opt-in 으로 켠다. 비활성 시 actual 호출은
    no-op(exit 0)이고 `--dry-run` 은 항상 허용(로컬 미리보기·미전송), `--force` 로 1회 강제.

종료 코드/신호:
  - 리뷰어 실패(인증/한도/네트워크/타임아웃) → exit 1 + stdout 에 FALLBACK_INTERNAL
    (= 내부 code-reviewer 서브에이전트로 폴백하라는 신호)
  - must-fix 감지 → exit 1
  - 통과 → exit 0

설계 (ADR-0004):
  - 어댑터 seam: 외부 도구를 `reviewer_cmd`(local.conf) 뒤로 격리 → codex 외 교체 가능.
    기본 `codex exec --sandbox read-only --skip-git-repo-check` (stdin 으로 프롬프트).
  - 도메인 외부화: 프로젝트 맥락은 `.project_manager/review_context.local.md`(인스턴스 소유)
    가 있으면 주입, 없으면 generic 헤더. 엔진 도구엔 도메인 콘텐츠 0.
  - subprocess DI (run_fn 매개변수) — 테스트에서 mock 주입 가능.
  - 외부 호출은 코드를 수정하지 않는다 (read-only 인자 사용 권장).
  - 시크릿 denylist (.env·*secret*·*credential*·*.key·*token*·*.pem 등) 파일은 diff 에서
    자동 제외하고 stderr 경고. local.conf `review_denylist_extra` 로 추가 가능.
"""

from __future__ import annotations

import argparse
import datetime
import fnmatch
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

REPO = Path(__file__).resolve().parents[2]
TICKETS_DIR = REPO / ".project_manager" / "wiki" / "tickets"
LOCAL_CONF = REPO / ".project_manager" / "local.conf"  # per-clone (git-ignored)
REVIEW_CONTEXT_FILE = REPO / ".project_manager" / "review_context.local.md"  # 인스턴스 소유 overlay
STATUS_DIRS: tuple[str, ...] = ("open", "claimed", "blocked", "done")

# 기본 검토 경로 (--paths·local.conf review_paths 미지정 시)
DEFAULT_PATHS: list[str] = ["src/", "tests/", "scripts/", ".project_manager/tools/"]

# 외부 리뷰어 기본 명령 (local.conf reviewer_cmd 로 교체 가능)
DEFAULT_REVIEWER_CMD = "codex exec --sandbox read-only --skip-git-repo-check"

# 외부 호출 타임아웃 (초)
EXTERNAL_TIMEOUT_SECONDS = 180

# 시크릿 denylist — 이 패턴에 매칭되는 파일은 diff 에서 강제 제외하고 stderr 에 경고.
# 보수적으로 유지: 오탐 허용 (누락 금지). 프로젝트 고유 경로는 local.conf review_denylist_extra 로.
_SECRET_DENYLIST_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*secret*",
    "*credential*",
    "*.key",
    "*token*",
    "*.pem",
    "*.p12",
    "*.pfx",
)

# must-fix / 반려 판정 토큰 (보수적: 하나라도 감지 → 비-통과)
_REJECT_TOKENS: tuple[str, ...] = (
    "must-fix", "must fix", "MUST-FIX", "MUST FIX", "반려", "REJECT", "reject",
)
_PASS_TOKENS: tuple[str, ...] = (
    "통과", "PASS", "pass", "승인", "APPROVE", "approve", "lgtm", "LGTM",
)

# 프롬프트 형식에서 must-fix 섹션 헤더를 인식하는 정규식
_MUST_FIX_SECTION_RE = re.compile(
    r"\*{0,2}must[- ]fix\*{0,2}(?:\s*\([^)]*\))?\s*:", re.IGNORECASE,
)
# must-fix 섹션 내 "없음/N/A/none" 항목 패턴
_NONE_ITEM_RE = re.compile(r"^(?:없음|n/?a|none)\s*$", re.IGNORECASE)

# generic 맥락 헤더 (review_context.local.md 부재 시)
_DEFAULT_CONTEXT_HEADER = """\
## 리뷰 맥락

아래 diff 를 코드리뷰하라. 프로젝트 고유 맥락(`.project_manager/review_context.local.md`)이
설정돼 있으면 그 기준을 우선한다.
"""

# 출력 형식 블록 (parse_verdict 가 의존 — 리뷰어 무관 공통)
_OUTPUT_FORMAT_BLOCK = """\
### 출력 형식 (필수)
아래 형식으로 응답하라:

판정: [통과 | 반려]

**must-fix** (반드시 수정):
- (없으면 "없음"으로 표기)

**suggestion** (권장):
- (없으면 "없음"으로 표기)

"""


# ── 설정 ──────────────────────────────────────────────────────────────────


def local_config() -> dict[str, str]:
    """per-clone local.conf 를 KEY=value 로 읽는다 (없으면 빈 dict). board.py 와 동일 포맷."""
    conf: dict[str, str] = {}
    if not LOCAL_CONF.exists():
        return conf
    for line in LOCAL_CONF.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        conf[key.strip()] = value.strip()
    return conf


def _is_enabled(conf: dict[str, str]) -> bool:
    return conf.get("external_review_enabled", "false").strip().lower() in ("true", "1", "yes", "on")


def _reviewer_cmd(conf: dict[str, str]) -> str:
    return conf.get("reviewer_cmd", "").strip() or DEFAULT_REVIEWER_CMD


def _configured_paths(conf: dict[str, str]) -> list[str]:
    raw = conf.get("review_paths", "").strip()
    return [p for p in re.split(r"[,\s]+", raw) if p] if raw else list(DEFAULT_PATHS)


def _denylist_patterns(conf: dict[str, str]) -> tuple[str, ...]:
    extra = conf.get("review_denylist_extra", "").strip()
    extras = tuple(p for p in re.split(r"[,\s]+", extra) if p) if extra else ()
    return _SECRET_DENYLIST_PATTERNS + extras


# ── 시크릿 필터링 ────────────────────────────────────────────────────────


def _is_secret_path(file_path: str, patterns: tuple[str, ...] = _SECRET_DENYLIST_PATTERNS) -> bool:
    """파일 경로가 시크릿 denylist 패턴에 매칭되는지 확인한다."""
    normalized = file_path.strip()
    for prefix in ("a/", "b/"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
            break
    basename = Path(normalized).name
    for pattern in patterns:
        if fnmatch.fnmatch(normalized, pattern) or fnmatch.fnmatch(basename, pattern):
            return True
        if pattern.endswith("/*") or pattern.endswith("/"):
            dir_pattern = pattern.rstrip("*").rstrip("/")
            if normalized.startswith(dir_pattern + "/") or normalized == dir_pattern:
                return True
    return False


def filter_secret_hunks(
    diff_text: str, patterns: tuple[str, ...] = _SECRET_DENYLIST_PATTERNS,
) -> tuple[str, list[str]]:
    """diff 텍스트에서 시크릿 denylist 파일의 hunk 를 제거한다.

    반환: (필터링된 diff 텍스트, 제외된 파일 경로 목록)
    """
    if not diff_text:
        return diff_text, []
    excluded_files: list[str] = []
    output_blocks: list[str] = []
    current_block: list[str] = []
    current_is_secret = False
    for line in diff_text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current_block and not current_is_secret:
                output_blocks.extend(current_block)
            current_block = [line]
            match = re.match(r"diff --git a/(\S+)\s+b/(\S+)", line)
            if match:
                file_path = match.group(2)
                current_is_secret = _is_secret_path(file_path, patterns)
                if current_is_secret:
                    excluded_files.append(file_path)
            else:
                current_is_secret = False
        else:
            current_block.append(line)
    if current_block and not current_is_secret:
        output_blocks.extend(current_block)
    return "".join(output_blocks), excluded_files


# ── git diff 추출 ─────────────────────────────────────────────────────────


def extract_diff(
    base: str,
    paths: list[str],
    run_fn: Callable[..., subprocess.CompletedProcess] | None = None,
    denylist: tuple[str, ...] = _SECRET_DENYLIST_PATTERNS,
) -> str:
    """git diff <base> -- <paths> 를 추출해 반환한다 (시크릿 denylist 자동 제외).

    base 가 'HEAD' 이면 스테이징+언스테이징 변경분(없으면 HEAD~1..HEAD)을 추출한다.
    run_fn — subprocess.run 대체 주입 (테스트용).
    """
    _run = run_fn or subprocess.run
    if base == "HEAD":
        staged = _run(["git", "-C", str(REPO), "diff", "--cached", "--"] + paths,
                      capture_output=True, text=True, encoding="utf-8", errors="replace")
        unstaged = _run(["git", "-C", str(REPO), "diff", "--"] + paths,
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
        combined = (staged.stdout if staged.returncode == 0 else "") + \
                   (unstaged.stdout if unstaged.returncode == 0 else "")
        if not combined.strip():
            commit = _run(["git", "-C", str(REPO), "diff", "HEAD~1..HEAD", "--"] + paths,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
            if commit.returncode == 0:
                combined = commit.stdout
        raw_diff = combined
    else:
        result = _run(["git", "-C", str(REPO), "diff", base, "--"] + paths,
                      capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            raise RuntimeError(f"git diff 실패 (rc={result.returncode}): {result.stderr.strip()}")
        raw_diff = result.stdout

    filtered_diff, excluded = filter_secret_hunks(raw_diff, denylist)
    for excluded_path in excluded:
        print(f"경고: 시크릿 denylist 경로 '{excluded_path}' 를 diff 에서 제외했습니다.",
              file=sys.stderr)
    return filtered_diff


# ── ticket touches 파싱 ───────────────────────────────────────────────────


def parse_ticket_touches(ticket_id: str) -> list[str]:
    """board ticket frontmatter 의 touches 필드를 파싱해 경로 목록을 반환한다.

    YAML frontmatter 직접 파싱 (board.py 를 import 하지 않음). 못 찾으면 빈 목록.
    """
    for status_dir in STATUS_DIRS:
        dir_path = TICKETS_DIR / status_dir
        if not dir_path.exists():
            continue
        for ticket_file in dir_path.glob(f"{ticket_id}-*.md"):
            return _parse_touches_from_file(ticket_file)
        exact = dir_path / f"{ticket_id}.md"
        if exact.exists():
            return _parse_touches_from_file(exact)
    return []


def _parse_touches_from_file(path: Path) -> list[str]:
    """ticket 파일에서 frontmatter touches 를 추출한다."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return []
    after_open = text[4:]
    end = after_open.find("\n---\n")
    if end == -1:
        return []
    fm_text = after_open[:end]
    touches: list[str] = []
    in_touches = False
    for line in fm_text.splitlines():
        if re.match(r"^touches\s*:", line):
            inline_match = re.match(r"^touches\s*:\s*\[(.+)\]", line)
            if inline_match:
                items = [s.strip().strip("\"'") for s in inline_match.group(1).split(",")]
                touches = [i for i in items if i]
                in_touches = False
            elif re.match(r"^touches\s*:\s*$", line):
                in_touches = True
            else:
                val = re.sub(r"^touches\s*:\s*", "", line).strip().strip("\"'")
                if val:
                    touches = [val]
                in_touches = False
        elif in_touches:
            item_match = re.match(r"^\s*-\s+(.+)$", line)
            if item_match:
                touches.append(item_match.group(1).strip().strip("\"'"))
            elif line and not line[0].isspace():
                in_touches = False
    return touches


# ── 프롬프트 조립 ─────────────────────────────────────────────────────────


def _load_review_context() -> str:
    """review_context.local.md (인스턴스 overlay) 가 있으면 그 내용, 없으면 generic 헤더."""
    if REVIEW_CONTEXT_FILE.exists():
        try:
            return REVIEW_CONTEXT_FILE.read_text(encoding="utf-8").strip() + "\n"
        except OSError:
            pass
    return _DEFAULT_CONTEXT_HEADER


def build_prompt(
    diff: str,
    dod: str | None = None,
    adr_refs: list[str] | None = None,
    gate: str | None = None,
) -> str:
    """맥락 헤더 + 출력 형식 + diff 를 결합해 표준 리뷰 프롬프트를 생성한다."""
    parts: list[str] = [_load_review_context().rstrip() + "\n\n", _OUTPUT_FORMAT_BLOCK]
    if adr_refs:
        parts.append(f"관련 ADR: {', '.join(adr_refs)}\n\n")
    if gate:
        parts.append(f"게이트 ticket: {gate}\n\n")
    if dod:
        parts.append(f"### 완료 조건 (DoD)\n{dod}\n\n")
    parts.append("### 리뷰 대상 diff\n")
    if diff.strip():
        parts.append("```diff\n")
        parts.append(diff)
        parts.append("```\n")
    else:
        parts.append("(변경 diff 없음 — 경로에 해당하는 변경사항이 없거나 base 와 동일)\n")
    return "".join(parts)


# ── 외부 리뷰어 실행 ──────────────────────────────────────────────────────


def run_reviewer(
    prompt: str,
    reviewer_cmd: str = DEFAULT_REVIEWER_CMD,
    timeout: int = EXTERNAL_TIMEOUT_SECONDS,
    run_fn: Callable[..., subprocess.CompletedProcess] | None = None,
) -> tuple[bool, str]:
    """reviewer_cmd 를 stdin(=프롬프트)으로 실행한다. 반환: (성공 여부, 출력 텍스트)."""
    _run = run_fn or subprocess.run
    argv = shlex.split(reviewer_cmd)
    if not argv:
        return False, "[reviewer_cmd 가 비어 있음 — local.conf 확인]"
    try:
        result = _run(argv, input=prompt, capture_output=True, text=True,
                      encoding="utf-8", errors="replace", timeout=timeout)
        output = result.stdout or ""
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, f"[리뷰어 타임아웃 — {timeout}초 초과]"
    except FileNotFoundError:
        return False, f"[리뷰어 명령 '{argv[0]}' 를 찾을 수 없음 — 설치 또는 PATH 확인]"
    except Exception as exc:  # noqa: BLE001
        return False, f"[리뷰어 실행 오류: {exc}]"


def reviewer_name(reviewer_cmd: str) -> str:
    """reviewer_cmd 의 첫 토큰을 리뷰어 라벨로 (파일명/요약용)."""
    argv = shlex.split(reviewer_cmd)
    name = argv[0] if argv else "reviewer"
    return re.sub(r"[^A-Za-z0-9_.-]", "_", Path(name).name) or "reviewer"


# ── 결과 파싱 ─────────────────────────────────────────────────────────────


def _extract_must_fix_items(output: str) -> list[str]:
    """must-fix 섹션 헤더 이후의 항목 라인을 추출한다 (표기 편차 처리)."""
    items: list[str] = []
    in_section = False
    for line in output.splitlines():
        if not in_section:
            if _MUST_FIX_SECTION_RE.search(line):
                in_section = True
                after_header = _MUST_FIX_SECTION_RE.sub("", line, count=1).strip()
                if after_header:
                    items.append(after_header)
        else:
            stripped = line.strip()
            if stripped.startswith("**") and stripped.endswith(":"):
                break
            if stripped.startswith("**") and "**:" in stripped:
                break
            if stripped.startswith("- ") or stripped.startswith("* "):
                items.append(stripped.lstrip("-* ").strip())
            elif stripped and not stripped.startswith("#"):
                items.append(stripped)
    return items


def _is_none_items(items: list[str]) -> bool:
    """항목 목록이 "없음/N/A/none" 만으로 구성됐는지 확인한다."""
    if not items:
        return True
    return all(_NONE_ITEM_RE.match(item) for item in items if item)


def parse_verdict(output: str) -> dict[str, bool]:
    """모델 출력에서 판정(통과/반려)·must-fix 존재를 파싱한다.

    반환: {"has_must_fix": bool, "has_pass": bool}. 보수적: 판정 라인 없이 must-fix/반려
    토큰만 있어도 has_must_fix=True. 예외: must-fix 섹션이 "없음/N/A/none" 만이면 False.
    """
    must_fix_items = _extract_must_fix_items(output)
    section_found = bool(_MUST_FIX_SECTION_RE.search(output))

    if section_found and _is_none_items(must_fix_items):
        has_must_fix = False
    elif section_found and must_fix_items:
        has_must_fix = True
    else:
        has_must_fix = any(token in output for token in _REJECT_TOKENS)

    has_pass = any(token in output for token in _PASS_TOKENS)

    verdict_line_match = re.search(r"판정\s*:\s*(\S+)", output)
    if verdict_line_match:
        verdict_word = verdict_line_match.group(1).strip()
        if any(tok in verdict_word for tok in
               ("통과", "PASS", "pass", "승인", "APPROVE", "approve", "LGTM", "lgtm")):
            if not must_fix_items or _is_none_items(must_fix_items):
                has_must_fix = False
        elif any(tok in verdict_word for tok in ("반려", "REJECT", "reject")):
            has_must_fix = True

    return {"has_must_fix": has_must_fix, "has_pass": has_pass}


# ── 결과 저장 ─────────────────────────────────────────────────────────────


def save_output(reviewer: str, content: str, output_dir: Path | None = None) -> Path:
    """리뷰어 출력 원문을 파일로 저장하고 경로를 반환한다."""
    base_dir = output_dir or Path(tempfile.gettempdir())
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = base_dir / f"external_review_{reviewer}_{ts}.txt"
    dest.write_text(content, encoding="utf-8")
    return dest


# ── 실행 + 수합 ────────────────────────────────────────────────────────────


def run_review(
    prompt: str,
    reviewer_cmd: str = DEFAULT_REVIEWER_CMD,
    timeout: int = EXTERNAL_TIMEOUT_SECONDS,
    output_dir: Path | None = None,
    run_fn: Callable[..., subprocess.CompletedProcess] | None = None,
) -> dict:
    """외부 리뷰어를 실행하고 결과를 수합한다.

    반환 dict: reviewer / ok / output / verdict / file / failed / any_must_fix / all_pass.
    """
    name = reviewer_name(reviewer_cmd)
    ok, output = run_reviewer(prompt, reviewer_cmd, timeout, run_fn)
    verdict = parse_verdict(output)
    out_file: Path | None = None
    if ok or output:
        out_file = save_output(name, output, output_dir)
    return {
        "reviewer": name,
        "ok": ok,
        "output": output,
        "verdict": verdict,
        "file": out_file,
        "failed": not ok,
        "any_must_fix": ok and verdict["has_must_fix"],
        "all_pass": ok and verdict["has_pass"] and not verdict["has_must_fix"],
    }


# ── 결과 요약 출력 ────────────────────────────────────────────────────────


def _format_verdict(ok: bool, verdict: dict | None) -> str:
    if not ok:
        return "실패"
    if verdict is None:
        return "미실행"
    if verdict["has_must_fix"]:
        return "성공 → 반려 (must-fix 감지)"
    if verdict["has_pass"]:
        return "성공 → 통과"
    return "성공 → 판정 불명확 (PM 확인 필요)"


def print_summary(result: dict, gate: str | None = None) -> None:
    """결과 요약을 stdout 에 출력한다."""
    sep = "=" * 60
    name = result.get("reviewer", "reviewer")
    print(sep)
    print(f"외부 코드리뷰 결과 요약 [{name}]")
    if gate:
        print(f"게이트: {gate}")
    print(sep)
    print(f"\n[{name}] {_format_verdict(result['ok'], result.get('verdict'))}")
    if result.get("file"):
        print(f"  원문: {result['file']}")
    print()
    if result["failed"]:
        print(f"종합 판정: {name} 실패")
        print("FALLBACK_INTERNAL")  # 내부 code-reviewer 서브에이전트로 폴백 신호
    elif result["any_must_fix"]:
        print("종합 판정: 비-통과 (must-fix 감지 — PM 검토 필요)")
    elif result["all_pass"]:
        print("종합 판정: 통과")
    else:
        print("종합 판정: 판정 불명확 (PM 확인 필요)")
    print(sep)


# ── 종료 코드 결정 ────────────────────────────────────────────────────────


def determine_exit_code(result: dict) -> int:
    """failed→1(FALLBACK), any_must_fix→1, all_pass→0, 판정불명확→1(보수적)."""
    if result["failed"] or result["any_must_fix"]:
        return 1
    if result["all_pass"]:
        return 0
    return 1


# ── CLI ──────────────────────────────────────────────────────────────────


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="외부 코드리뷰 래퍼 — 외부 리뷰어 어댑터 CLI (ADR-0004, 기본 OFF)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  # 기본 (HEAD 기준 변경, local.conf review_paths/기본 경로) — 활성화돼 있어야 실제 호출
  python3 .project_manager/tools/external_review.py

  # ticket 의 touches 로 경로 결정
  python3 .project_manager/tools/external_review.py --ticket T-0259

  # 특정 base 와 경로 지정
  python3 .project_manager/tools/external_review.py --base main --paths src/ tests/

  # dry-run (diff·프롬프트만 출력, 외부 호출/전송 안 함 — 비활성이어도 허용)
  python3 .project_manager/tools/external_review.py --dry-run

  # 비활성 상태에서 1회 강제 실행
  python3 .project_manager/tools/external_review.py --force

활성화: local.conf 에 `external_review_enabled=true` (+ 필요 시 `reviewer_cmd`) ·
        또는 `board.py init` / `pm_update` 시 opt-in 프롬프트.
""",
    )
    parser.add_argument("--base", default="HEAD",
                        help="git diff 기준 ref (기본: HEAD — 스테이징+언스테이징)")
    parser.add_argument("--paths", nargs="+", default=None,
                        help="검토 대상 경로 (기본: local.conf review_paths / src tests scripts ...)")
    parser.add_argument("--ticket", default=None, metavar="T-NNNN",
                        help="ticket ID — touches 로 검토 경로 결정")
    parser.add_argument("--gate", default=None, metavar="T-NNNN",
                        help="게이트 ticket 표식 (로깅용)")
    parser.add_argument("--dry-run", action="store_true",
                        help="diff·프롬프트만 출력, 외부 호출/전송 안 함 (비활성이어도 허용)")
    parser.add_argument("--force", action="store_true",
                        help="external_review_enabled=false 여도 1회 강제 실행 (외부 전송 발생)")
    parser.add_argument("--output-dir", default=None, metavar="DIR",
                        help="리뷰 원문 저장 디렉토리 (기본: /tmp)")
    parser.add_argument("--timeout", type=int, default=EXTERNAL_TIMEOUT_SECONDS,
                        metavar="SEC", help=f"외부 호출 타임아웃(초) (기본: {EXTERNAL_TIMEOUT_SECONDS})")
    parser.add_argument("--adr", nargs="+", default=None, metavar="ADR-NNNN",
                        help="관련 ADR 목록 (프롬프트에 포함)")
    return parser


def _set_console_codepage_utf8() -> None:
    # Windows 한정 — 콘솔 코드페이지를 UTF-8(65001)로 맞춘다. cp949(한국어) 콘솔에서
    # stdout reconfigure(utf-8)만으로는 콘솔이 UTF-8 바이트를 cp949 로 디코드해 한글이
    # mojibake 되므로, 콘솔 입출력 codepage 자체를 65001 로 설정해 정합시킨다 (T-0068).
    # best-effort: 콘솔 핸들 없음·권한·예외 시 조용히 통과(reconfigure 와 동형 try/except).
    # idempotent — 이미 UTF-8 콘솔엔 65001 재설정이 무해. POSIX 는 진입하지 않는다.
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    # 콘솔/파이프 출력을 UTF-8 로 재설정 — cp949 콘솔이나 리다이렉트된 stdout 에서
    # 이모지·em-dash(—) print 가 UnicodeEncodeError 로 죽는 것을 막는다 (T-0017).
    # 먼저 Windows 콘솔 codepage 를 UTF-8 로 맞춘 뒤(T-0068) 스트림을 reconfigure 한다.
    # reconfigure 미지원 스트림(테스트 캡처 등)은 hasattr 가드로 건너뛴다.
    _set_console_codepage_utf8()
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    conf = local_config()

    output_dir: Path | None = None
    if args.output_dir:
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

    # 경로 결정: --paths > --ticket touches > local.conf review_paths > DEFAULT_PATHS
    if args.paths:
        paths = args.paths
    elif args.ticket:
        touches = parse_ticket_touches(args.ticket)
        if not touches:
            print(f"경고: ticket {args.ticket} 의 touches 미발견 — 기본 경로 사용", file=sys.stderr)
            paths = _configured_paths(conf)
        else:
            paths = touches
    else:
        paths = _configured_paths(conf)

    print(f"검토 경로: {paths}", file=sys.stderr)
    print(f"base: {args.base}", file=sys.stderr)

    # diff 추출 (시크릿 denylist 자동 제외)
    try:
        diff = extract_diff(args.base, paths, denylist=_denylist_patterns(conf))
    except RuntimeError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1

    prompt = build_prompt(diff=diff, adr_refs=args.adr, gate=args.gate)

    if args.dry_run:
        print("=== [dry-run] 프롬프트 미리보기 (외부 전송 없음) ===")
        print(prompt)
        print("=== [dry-run] 외부 호출 생략 ===")
        return 0

    # 활성화 게이트 (ADR-0004 — 외부 전송이므로 기본 OFF)
    if not _is_enabled(conf) and not args.force:
        print(
            "외부 리뷰 비활성 — 코드 diff 외부 전송이 꺼져 있습니다 "
            "(local.conf external_review_enabled=false).\n"
            "켜기: local.conf 에 `external_review_enabled=true` 추가, 또는 "
            "`board.py init` / `pm_update` 시 opt-in 프롬프트. "
            "미리보기는 `--dry-run`, 1회 강제는 `--force`.",
            file=sys.stderr,
        )
        return 0  # no-op — 실패 아님

    reviewer_cmd = _reviewer_cmd(conf)
    print(f"외부 리뷰어 실행 중: {reviewer_cmd}", file=sys.stderr)
    result = run_review(
        prompt=prompt, reviewer_cmd=reviewer_cmd,
        timeout=args.timeout, output_dir=output_dir,
    )
    print_summary(result, gate=args.gate)
    return determine_exit_code(result)


if __name__ == "__main__":
    sys.exit(main())
