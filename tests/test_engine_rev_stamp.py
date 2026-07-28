"""T-0397 — 엔진 rev 스탬프 정합 평시 가드 (baked 리터럴 == 단일 진실 값).

각 stamped 엔진 모듈은 자기 소스에 `ENGINE_REV = "vX.Y.Z"` baked 리터럴을 지닌다(부분복사 skew
검출용). 이 가드는 **평시 회귀**에서 전 모듈 리터럴 == `engine_rev.ENGINE_REV`(단일 진실)를
강제해, bump 누락·부분 편집(일부 모듈만 갱신)을 즉시 red 로 세운다 — 릴리즈 시 `engine_rev.py
--bump vX.Y.Z` 한 커맨드로만 갱신하라는 규율을 기계로 못박는다.

완결성 가드: tools/ 에서 baked 리터럴을 지닌 모든 모듈이 `STAMPED_MODULES` 에 등재됐는지 본다
(새 stamped 모듈을 목록에서 빠뜨리면 bump/가드가 조용히 놓치는 갭 차단). bump CLI 의 형식 검증·
멱등·전-파일 재작성도 확인한다.
"""
from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load_engine_rev(tools: Path = TOOLS):
    spec = importlib.util.spec_from_file_location("engine_rev", tools / "engine_rev.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_all_stamped_module_literals_match_single_source():
    """전 STAMPED_MODULES 의 baked ENGINE_REV 리터럴 == engine_rev.ENGINE_REV (bump 누락/부분편집 red)."""
    er = _load_engine_rev()
    mismatched = {
        fn: er.read_literal(TOOLS / fn)
        for fn in er.STAMPED_MODULES
        if er.read_literal(TOOLS / fn) != er.ENGINE_REV
    }
    assert not mismatched, (
        f"baked 리터럴 ≠ engine_rev.ENGINE_REV({er.ENGINE_REV!r}) — bump 누락/부분편집: {mismatched}. "
        f"`python3 .project_manager/tools/engine_rev.py --bump {er.ENGINE_REV}` 로 일괄 재작성하라."
    )


def test_stamped_modules_list_is_complete():
    """tools/ 에서 baked 리터럴을 지닌 모든 모듈이 STAMPED_MODULES 에 등재됐다 (등록 누락 갭 차단).

    engine_rev.py(단일 진실 소스)는 제외 — 그 자체는 대조 대상이 아니라 기준값이다."""
    er = _load_engine_rev()
    found = {
        p.name for p in TOOLS.glob("*.py")
        if p.name != "engine_rev.py" and er.read_literal(p) is not None
    }
    assert found == set(er.STAMPED_MODULES), (
        f"baked 리터럴 보유 모듈 ≠ STAMPED_MODULES — "
        f"목록에만: {sorted(set(er.STAMPED_MODULES) - found)} / "
        f"파일에만(미등재): {sorted(found - set(er.STAMPED_MODULES))}"
    )


def test_bump_is_idempotent_and_covers_all_files():
    """bump(현재값·dry-run) → 변경 0(멱등). bump(신값·dry-run) → engine_rev + 전 STAMPED 재작성 대상."""
    er = _load_engine_rev()
    assert er.bump(er.ENGINE_REV, dry_run=True) == []          # 같은 값 → 변경 없음
    would_change = er.bump("v0.0.0", dry_run=True)             # 다른 값 → 전 파일
    assert set(would_change) == {"engine_rev.py", *er.STAMPED_MODULES}
    # dry-run 이라 실제 파일은 그대로 (부작용 0).
    assert er.read_literal(TOOLS / "board.py") == er.ENGINE_REV


def test_bump_recovers_after_engine_rev_was_rewritten_first(tmp_path):
    """중단된 bump(engine_rev=신 rev·console_encoding=구 rev)도 재실행하면 전 파일 정합을 복구한다."""
    current = _load_engine_rev()
    major, minor, patch = map(int, current.ENGINE_REV.removeprefix("v").split("."))
    new_rev = f"v{major}.{minor}.{patch + 1}"

    tools = tmp_path / ".project_manager" / "tools"
    tools.mkdir(parents=True)
    for filename in ("engine_rev.py", *current.STAMPED_MODULES):
        shutil.copy2(TOOLS / filename, tools / filename)

    # bump()의 실제 쓰기 순서상 첫 파일만 반영되고 프로세스가 중단된 순간을 재현한다.
    engine_path = tools / "engine_rev.py"
    old_literal = f'ENGINE_REV = "{current.ENGINE_REV}"'
    text = engine_path.read_text(encoding="utf-8")
    assert text.count(old_literal) == 1
    engine_path.write_text(text.replace(old_literal, f'ENGINE_REV = "{new_rev}"', 1),
                           encoding="utf-8")

    interrupted = _load_engine_rev(tools)
    assert interrupted.ENGINE_REV == new_rev
    assert interrupted.read_literal(tools / "console_encoding.py") == current.ENGINE_REV

    # engine_rev.main()은 복구 경로이므로 stale console helper를 verify하지 않고 bump까지 도달한다.
    assert interrupted.main(["--bump", new_rev]) == 0
    assert {
        filename: interrupted.read_literal(tools / filename)
        for filename in ("engine_rev.py", *interrupted.STAMPED_MODULES)
    } == {
        filename: new_rev
        for filename in ("engine_rev.py", *interrupted.STAMPED_MODULES)
    }


def test_bump_round_trip_rewrites_delegate_modules(tmp_path):
    """--bump 신 rev→원 rev 왕복이 위임 모듈 2개의 baked 리터럴도 함께 재작성한다."""
    current = _load_engine_rev()
    major, minor, patch = map(int, current.ENGINE_REV.removeprefix("v").split("."))
    next_rev = f"v{major}.{minor}.{patch + 1}"
    delegate_modules = ("pm_delegate.py", "delegate_scope.py")
    assert set(delegate_modules) <= set(current.STAMPED_MODULES)

    tools = tmp_path / ".project_manager" / "tools"
    tools.mkdir(parents=True)
    for filename in ("engine_rev.py", *current.STAMPED_MODULES):
        shutil.copy2(TOOLS / filename, tools / filename)

    isolated = _load_engine_rev(tools)
    assert isolated.main(["--bump", next_rev]) == 0
    assert {
        filename: isolated.read_literal(tools / filename)
        for filename in delegate_modules
    } == dict.fromkeys(delegate_modules, next_rev)

    bumped = _load_engine_rev(tools)
    assert bumped.main(["--bump", current.ENGINE_REV]) == 0
    assert {
        filename: bumped.read_literal(tools / filename)
        for filename in delegate_modules
    } == dict.fromkeys(delegate_modules, current.ENGINE_REV)


def test_bump_rejects_bad_format():
    """bump 은 vX.Y.Z 형식만 수용 (오형식 → SystemExit·fail-loud)."""
    er = _load_engine_rev()
    with pytest.raises(SystemExit):
        er.bump("1.2.3", dry_run=True)     # v 접두 없음
    with pytest.raises(SystemExit):
        er.bump("v1.2", dry_run=True)      # 3-파트 아님
