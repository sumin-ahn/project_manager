#!/usr/bin/env python3
"""판단 원칙 레지스트리 로더 + 행동 직전 recall 판정.

`.project_manager/wiki/pm_principles.md`(출하층·canonical)와 PM 홈 로컬층
`.project_manager/wiki/pm_principles.local.md`(미출하·manifest 미등재)를 같은 스키마로 파싱한다.
형식 = 단일 markdown, 한 규칙 = 한 목록 항목(`- ...`, 줄바꿈 없음). 항목 머리가 코드 span
`` `[on: match]` ``(`on` ∈ shell|edit|delegate|prompt · `match` = 정규식)로 시작하면 RECALL(행동
직전 훅이 매칭 시 본문을 주입) 항목이고, 태그가 없으면 JUDGMENT(기계 판정 지점 없음) 항목이다.
태그 유무가 곧 분류다 — 분류를 별도 필드로 적지 않는다(파생 가능한 것은 기계).

공개 API 2개:
  - `load(root)`            — 출하층 + 로컬층 합성 규칙 튜플(같은 `match` 면 로컬층이 이긴다).
  - `judge_recall(root, *, on, text, seen)` — 클라우드/코덱스/opencode 세 어댑터가 공유하는
    단일 판정. 매칭 규칙 본문을 합본해 반환하고, `seen`(이미 이번 세션에 주입된 규칙 key 집합)에
    있는 규칙은 다시 넣지 않는다. 파일 부재·파손 항목은 조용히 skip 하지 않고 판정 dict 의
    `broken` 필드로 건수를 낸다 — 판정 불능이 통과와 같은 출력이면 안 된다. 어떤 입력에도 도구
    실행을 막지 않는다(비차단).

CLI(`judge-recall`/`count`/`rearm`)는 opencode plugin 이 python 자식으로 부르는 진입점이다 —
claude·codex in-process 핸들러는 이 모듈을 직접 import 해 `judge_recall`/`load_seen_marker`/
`record_seen_marker`를 부른다.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import NamedTuple

# ── 엔진 사본 rev 스탬프 (형제 사본 skew fail-loud · engine_rev.py --bump 대상) ──────────
ENGINE_REV = "v1.7.12"

# ── 엔진 중앙 로더 부트스트랩 (형제 로드는 이 한 경로만 · `repo_owned_files.load_module`) ──
# 파싱 자체는 정규식 하나로 끝나지만 이 모듈도 형제 둘을 지연 로드한다 — 레지스트리·marker 판독은
# 공용 읽기 seam(`file_lock`)을, CLI 진입(`main`)의 콘솔 인코딩은 `console_encoding` 을 쓴다. 엔진
# 전체가 `spec_from_file_location` 을 중앙 로더 한 곳에서만 부르는 불변식(deep-import 가드)이라
# 두 로드 모두 이 경로를 지난다.
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


def _verify_engine_rev(sibling_module, sibling_filename):
    """로드한 형제 모듈의 baked ENGINE_REV 를 이 사본의 것과 대조한다(fail-loud·skew→명시 에러)."""
    got = getattr(sibling_module, "ENGINE_REV", None)
    if got != ENGINE_REV:
        err = RuntimeError(
            f"엔진 사본 버전 불일치 — 로더 {Path(__file__).name}(rev={ENGINE_REV!r})가 "
            f"형제 {sibling_filename}(rev={got!r})를 로드했다 (사본 skew: 부분/수동 복사 또는 "
            "구형 사본). `pm_update.py`로 .project_manager/tools/ 전체를 재동기하라."
        )
        err._engine_rev_skew = True
        raise err


def _load_file_lock():
    """공용 파일 프리미티브 seam(`file_lock.py`)을 같은 tools/ 에서 경로 로드한다.

    원자 교체 대상 파일을 읽는 지점은 이 seam 의 공유 읽기를 지난다 — 일반 `open` 리더가 하나라도
    잡고 있으면 Windows 는 그 파일의 원자 교체를 WinError 32 로 막는다. 부재/손상/rev 불일치는
    엔진 사본 손상이므로 흡수하지 않는다(fail-loud·재동기 안내).
    """
    return _load_module_from_path(
        Path(__file__).resolve().with_name("file_lock.py"), "file_lock.py",
        verifier=_verify_engine_rev, cache=True,
    )


# ── 스키마 상수 ────────────────────────────────────────────────────────────
ON_VALUES = ("shell", "edit", "delegate", "prompt")

_SHIPPED_REL = Path(".project_manager") / "wiki" / "pm_principles.md"
_LOCAL_REL = Path(".project_manager") / "wiki" / "pm_principles.local.md"
_MARKER_DIR_REL = Path(".project_manager") / ".local" / "principle-recall"

_BUNDLE_RE = re.compile(r"^#{2,4}\s+(.+?)\s*$")
_ITEM_RE = re.compile(r"^-\s+(.*)$")
_TAG_RE = re.compile(
    r"^`\[(?P<on>shell|edit|delegate|prompt):\s+(?P<match>.+?)\]`\s+(?P<body>.+)$"
)

# additionalContext 상한(claude 계약과 같은 값 — ctx_stop_hook.py 주석 참고). 초과 시 잘라내지
# 않고 매칭 수만 값으로 싣는다.
_MAX_INJECT_CHARS = 10_000
_INJECT_PREFIX = "[principle-recall]"


class Rule(NamedTuple):
    """레지스트리 항목 하나. `on is None` 이면 JUDGMENT(주입 대상 아님)."""

    bundle: str
    on: str | None
    match: str | None
    text: str
    layer: str  # "shipped" | "local"


def _parse_text(text: str, *, layer: str) -> tuple[tuple[Rule, ...], int]:
    """마크다운 본문 → (규칙 튜플, 파손 항목 수). 파손은 조용히 skip 하지 않고 건수를 센다."""
    bundle = ""
    rules: list[Rule] = []
    broken = 0
    for line in text.splitlines():
        heading = _BUNDLE_RE.match(line)
        if heading:
            bundle = heading.group(1)
            continue
        item = _ITEM_RE.match(line)
        if not item:
            continue
        body = item.group(1).strip()
        if not body:
            continue
        if body.startswith("`["):
            tag = _TAG_RE.match(body)
            if not tag:
                broken += 1
                continue
            match_pattern = tag.group("match")
            try:
                re.compile(match_pattern)
            except re.error:
                broken += 1
                continue
            rules.append(Rule(
                bundle=bundle, on=tag.group("on"), match=match_pattern,
                text=tag.group("body"), layer=layer,
            ))
        else:
            rules.append(Rule(bundle=bundle, on=None, match=None, text=body, layer=layer))
    return tuple(rules), broken


def _read_text(path: Path) -> str:
    """레지스트리·marker 판독 한 지점(공용 읽기 seam 위임). 부재·판독 실패는 빈 본문."""
    try:
        return _load_file_lock().read_text_shared(path, encoding="utf-8")
    except OSError:
        return ""


def _dedup_key(rule: Rule):
    if rule.on is not None:
        return ("recall", rule.on, rule.match)
    return ("judgment", rule.text)


def _load_all(root) -> tuple[tuple[Rule, ...], int]:
    """출하층 + 로컬층 합성(같은 key 면 로컬층이 이긴다) + 합산 파손 건수.

    파일 부재는 파손이 아니다(채택자 형상 — 로컬층 없음이 정상). 읽기 실패·존재하지 않는 경로는
    빈 본문으로 접혀 규칙 0건을 낼 뿐 예외를 던지지 않는다(비차단 계약)."""
    root = Path(root)
    shipped_rules, shipped_broken = _parse_text(
        _read_text(root / _SHIPPED_REL), layer="shipped",
    )
    local_path = root / _LOCAL_REL
    if local_path.is_file():
        local_rules, local_broken = _parse_text(_read_text(local_path), layer="local")
    else:
        local_rules, local_broken = (), 0
    merged: dict[object, Rule] = {}
    order: list[object] = []
    for rule in (*shipped_rules, *local_rules):
        key = _dedup_key(rule)
        if key not in merged:
            order.append(key)
        merged[key] = rule  # 로컬층이 나중에 와서 같은 key 를 덮는다.
    return tuple(merged[key] for key in order), shipped_broken + local_broken


def load(root) -> tuple[Rule, ...]:
    """출하층 + 로컬층 합성 규칙(층 우선순위 반영). 파손 항목은 조용히 제외된다 —
    파손 건수가 필요하면 `judge_recall` 의 반환 dict `broken` 필드를 쓴다."""
    rules, _broken = _load_all(root)
    return rules


def judge_recall(root, *, on: str, text: str, seen=None) -> dict | None:
    """`on` 축에서 `text` 에 매칭하는 RECALL 규칙을 합본해 반환한다(없으면 None).

    `seen` 은 이번 세션에 이미 주입된 규칙 key 집합(caller 관리) — 그 안의 규칙은 다시 매칭돼도
    본문에 넣지 않는다. 반환 dict:
      - `count`  이번 호출에서 새로 매칭된 규칙 수.
      - `keys`   새로 매칭된 규칙의 key 목록(caller 가 `seen` 갱신에 쓴다).
      - `text`   `[principle-recall] ...` 접두 주입 문안(매칭 0·파손 0 이면 빈 문자열). 파손이
                 있으면 매칭 문안 뒤(매칭 0 이면 단독으로) 경고 줄이 같은 문자열에 이어붙는다 —
                 세 어댑터 모두 이 필드 하나만 주입하므로 파손 경고가 문안 없이 사라지지 않는다.
      - `broken` (있으면) 레지스트리 파손 항목 수 — 판정 불능은 침묵하지 않는다.
    어떤 입력에도 예외를 던지지 않는다(비차단) — 판독 실패·파손 항목은 값으로 접힌다. 형제 seam
    부재/손상/rev 불일치만 다른 엔진 모듈과 같은 규칙으로 올라가며, 그 경우도 어댑터 3종의
    fail-open 이 도구 실행을 막지 않는다."""
    if on not in ON_VALUES:
        return None
    rules, broken = _load_all(root)
    seen_keys = set(seen) if seen else set()
    matched: list[tuple[str, str]] = []
    for rule in rules:
        if rule.on != on:
            continue
        key = f"{rule.on}:{rule.match}"
        if key in seen_keys:
            continue
        try:
            hit = re.search(rule.match, text or "")
        except re.error:
            broken += 1
            continue
        if hit:
            matched.append((key, rule.text))
    if not matched and not broken:
        return None
    result: dict = {"count": len(matched), "keys": [key for key, _ in matched], "text": ""}
    segments: list[str] = []
    if matched:
        body = "\n".join(f"- {rule_text}" for _, rule_text in matched)
        rendered = f"{_INJECT_PREFIX} {body}"
        if len(rendered) > _MAX_INJECT_CHARS:
            rendered = f"{_INJECT_PREFIX} 매칭 {len(matched)}건 — 문안 상한 초과로 값만 표시"
        segments.append(rendered)
    if broken:
        result["broken"] = broken
        # 파손 항목은 판정 불능이지 통과가 아니다 — 매칭 문안이 없어도(또는 있어도) 같은
        # `text` 필드에 실어 세 어댑터가 별도 배선 없이 그대로 주입하게 한다.
        segments.append(f"{_INJECT_PREFIX} 레지스트리 파손 항목 {broken}건 — 판정에서 제외")
    result["text"] = "\n".join(segments)
    return result


# ── (세션, 규칙) 1회 marker — ctx_stop_hook `_nudge_marker_path` 관례와 동형 ─────────────
def _safe_session_id(session_id) -> str:
    text = str(session_id or "unknown").strip()
    safe = "".join(c for c in text if c.isalnum() or c in "-_")[:64]
    return safe or "unknown"


def _marker_path(root, session_id) -> Path:
    return Path(root) / _MARKER_DIR_REL / f"{_safe_session_id(session_id)}.json"


def load_seen_marker(root, session_id) -> set[str]:
    """이번 세션에 이미 주입한 규칙 key 집합(marker 부재·파손은 빈 집합 — 비차단)."""
    path = _marker_path(root, session_id)
    try:
        data = json.loads(_read_text(path))
    except ValueError:
        return set()
    if isinstance(data, list):
        return {str(k) for k in data}
    return set()


def record_seen_marker(root, session_id, keys) -> None:
    """새로 주입한 규칙 key 를 marker 에 합친다(best-effort — 쓰기 실패는 삼킨다)."""
    keys = [str(k) for k in (keys or ())]
    if not keys:
        return
    path = _marker_path(root, session_id)
    existing = load_seen_marker(root, session_id)
    existing.update(keys)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(existing)), encoding="utf-8", newline="\n")
    except OSError:
        pass  # marker 쓰기 실패는 소음(재주입)일 뿐 — 도구 실행을 막지 않는다.


def rearm_seen_marker(root, session_id) -> None:
    """PostCompact 경계에서 marker 를 지워 다음 사이클에 규칙을 다시 주입 가능하게 한다."""
    try:
        _marker_path(root, session_id).unlink(missing_ok=True)
    except OSError:
        pass


def _write_json(payload: dict) -> None:
    """기계 판독 한 줄(JSON)을 콘솔 코덱 전환과 무관하게 UTF-8 로 내보낸다.

    부모(opencode plugin)가 stdout 을 파싱하므로 콘솔 codepage 강등은 되돌릴 수 없는 손실이다 —
    `pm_log._write_machine_line` 과 같은 공용 seam 을 쓰고, 형제 부재/손상/rev 불일치는 삼키지
    않는다(엔진 사본 손상은 fail-loud). 부모는 비영 rc 를 이미 fail-open 으로 다룬다.
    """
    console_encoding = _load_module_from_path(
        Path(__file__).resolve().with_name("console_encoding.py"),
        "console_encoding.py",
        verifier=_verify_engine_rev,
        cache=True,
    )
    console_encoding.write_machine_line(json.dumps(payload, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    _console_encoding = _load_module_from_path(
        Path(__file__).resolve().with_name("console_encoding.py"),
        "console_encoding.py",
        verifier=_verify_engine_rev,
    )
    _console_encoding.configure_console_utf8()
    parser = argparse.ArgumentParser(
        description="판단 원칙 레지스트리 — 로더 + recall 판정 CLI(opencode subprocess 진입점).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_judge = sub.add_parser("judge-recall", help="한 호출을 판정하고 marker 를 갱신한다.")
    p_judge.add_argument("--root", default=".")
    p_judge.add_argument("--on", required=True, choices=ON_VALUES)
    p_judge.add_argument("--text", default="")
    p_judge.add_argument("--session", default="unknown")

    p_count = sub.add_parser("count", help="로드된 규칙 수(RECALL/JUDGMENT/파손)를 낸다.")
    p_count.add_argument("--root", default=".")

    p_rearm = sub.add_parser("rearm", help="세션 marker 를 지워 다음 사이클을 재무장한다.")
    p_rearm.add_argument("--root", default=".")
    p_rearm.add_argument("--session", required=True)

    args = parser.parse_args(argv)
    root = Path(args.root).resolve()

    if args.command == "judge-recall":
        seen = load_seen_marker(root, args.session)
        result = judge_recall(root, on=args.on, text=args.text, seen=seen)
        if result is None:
            result = {"count": 0, "keys": [], "text": ""}
        elif result.get("keys"):
            record_seen_marker(root, args.session, result["keys"])
        _write_json(result)
        return 0

    if args.command == "count":
        rules, broken = _load_all(root)
        recall = sum(1 for rule in rules if rule.on is not None)
        _write_json({
            "rules": len(rules), "recall": recall,
            "judgment": len(rules) - recall, "broken": broken,
        })
        return 0

    if args.command == "rearm":
        rearm_seen_marker(root, args.session)
        _write_json({"rearmed": True})
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
