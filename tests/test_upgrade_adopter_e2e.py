"""업그레이드-채택자 e2e 게이트 — 구세대 디스크 + add-harness 형상 (T-0582 · 기계층).

## 이 파일이 있는 이유
v1.6.1 채택자 제보 중 네 건(guest 절 소실·어댑터 동결·토큰 회귀·reconcile 파괴)은 **기존 채택자가
업그레이드할 때만** 발현한다 — 디스크에 옛 세대 데이터(옛 guest 마커·add-harness 이력·설치 시점에
굳은 치환 결과)가 있어야 성립하는 조건이라, 깨끗한 디렉토리만 보는 fresh-adopter 게이트
(`test_fresh_adopter_e2e.py`)는 이 클래스를 구조적으로 못 본다. 프레임워크 자신(adopter#0)도
add-harness 를 안 쓰는 형상이라 도그푸딩으로도 안 걸린다([[dogfooding-blind-spot-adopter-shape]]).

## 픽스처 모델 — 현행 엔진 + 구세대 데이터
옛 태그를 checkout 해 설치하는 대신 **현행 설치 + 상태 주입**을 쓴다(T-0582 결정): 발현 조건은
"디스크에 남은 옛 데이터" 지 "옛 코드" 가 아니고, 주입 방식이라야 픽스처가 코드로 자명하다.
`_build_upgraded_adopter` 가 셋을 주입한다 — ① 옛 세대 guest 절(옛 마커 리터럴 + 엔진 행 부재)
② add-harness guest 어댑터의 설치-시점 동결 사본 ③ 설치 시 값으로 굳은 치환 결과.

채택자 트리의 **엔진 사본은 canonical 로 먼저 승격**한다(`_prime_engine_to_canonical`). pm_import 는
`templates/<flavor>/` 사본에서 복사하는데 그 사본은 wave 중 stale 할 수 있어, 승격이 없으면 이
게이트가 "현행 엔진이 옛 디스크를 어떻게 다루나" 가 아니라 "templates 가 언제 동기됐나" 를 재는
측정기가 된다.

## 시나리오
- **S1** guest 절 생존 — 옛 마커 채택자가 동기 후에도 절을 갖고, 마커가 현행 리터럴로 수렴하며,
  다음 동기가 멱등이다.
- **S2** frozen 가시성 — guest 어댑터 엔진 파일의 동결이 **조용히** 지나가지 않는다(상류와 수렴
  하거나, 최소한 loud·actionable 하게 표면화된다).
- **S3** 토큰 안정성 — 동기 후 채택자 가시 문서에 render 토큰이 없고, 소비 시점 소유 템플릿은
  토큰을 유지하며, 재동기가 진동 0 이다.
- **S4** reconcile 절차 안전 — 스킬이 지시하는 manifest 선-cp 절차와 엔진 단독 경로 둘 다에서
  guest 절이 조용히 사라지지 않는다.

**기계층이다** — 로컬 파일시스템 + subprocess(pm_import·pm_update)만 구동한다(라이브 LLM·네트워크
0·결정적). 실 LLM 이 문서를 읽고 운영하는 런타임 축은 이 파일의 관심사가 아니다.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from _harness_matrix import HARNESSES
# fresh-adopter 게이트의 헬퍼를 그대로 쓴다(판정 사본 금지) — `_snapshot_tree` 는 산출물 트리
#   컴포넌트(`__pycache__`·`.git`·`.pm_import_backups`) 제외까지 이미 담고 있다.
from test_fresh_adopter_e2e import _load_pm_import, _snapshot_tree
# 픽스처 기능 삽입 규약은 디스패처 게이트가 소유한다(사본 금지) — 이 파일은 그것을 상류에
#   심어 "코드 변경만으로 전파되나" 를 본다.
from test_codex_hook_dispatch import (
    FIXTURE_FEATURE_ENVELOPE, dispatcher_source_with_fixture_feature,
)
from _win_skip import posix_bash_supported

REPO = Path(__file__).resolve().parents[1]


def _card_with_operational_details(path: Path) -> str:
    """상시 카드와 T-0678 sibling 상황별 참조를 함께 읽는다."""
    text = path.read_text(encoding="utf-8")
    details = path.parent / "references" / "operational-details.md"
    if details.is_file():
        text += "\n" + details.read_text(encoding="utf-8")
    return text

# 제보 채택자 형상(claude host + add-harness guest)을 그대로 재현한다. host 축을 곱하지 않는 이유:
#   이 파일이 검증하는 건 *업그레이드 상태 처리*(마커 세대·치환 잔재·절차 안전)라 host 하네스와
#   독립이고, guest 방향별 동기 채널 축은 T-0574 게이트(`test_fresh_adopter_e2e`)가 전 순서쌍으로
#   이미 돈다. guest 축만 파생으로 곱해 flavor shape 차이(엔진 행 1건 vs 디렉토리 포함 4건)를 태운다.
_HOST_HARNESS = "claude"
_GUEST_HARNESSES = tuple(harness for harness in HARNESSES if harness != _HOST_HARNESS)
# S1·S3·S4 는 단일 형상으로 충분하다(축 곱셈 비용 대비 판정력 증가 0) — 제보 그대로의 guest.
_REPORTED_GUEST = "codex"

assert _GUEST_HARNESSES, "guest 후보 0 — HARNESSES 파생이 퇴화했다(공허 파라미터)"
assert _REPORTED_GUEST in _GUEST_HARNESSES, (
    f"제보 형상 guest({_REPORTED_GUEST})가 파생 축에 없다: {_GUEST_HARNESSES}")

# pm:data-literal:begin
# 아래 세 리터럴은 **채택자 디스크에 기록된 데이터**다 — engine.manifest 안에 실제로 적힌 바이트이지
# 이 소스의 산문이 아니다. 엔진 상수(`pm_update._GUEST_MANIFEST_BEGIN` 등)를 참조하면 리터럴이 통째로
# 바뀌어도 이 게이트가 green 이라(참조-only 테스트가 guest 절 소실을 30 여 릴리즈 살린 직접 원인)
# 여기엔 문자열을 직접 박는다. 마커 세대가 또 바뀌면 이 파일이 red 로 알린다.
_LEGACY_GUEST_BEGIN = "# >>> pm add-harness guest @render (local·pm_update-preserved·T-0456) >>>"
_CURRENT_GUEST_BEGIN = "# >>> pm add-harness guest @render (local·pm_update-preserved) >>>"
_GUEST_END_MARKER = "# <<< pm add-harness guest @render (local) <<<"
# pm:data-literal:end

_ADOPTER_NAME = "Upgrade Adopter"
_ADOPTER_MANIFEST_REL = ".project_manager/engine.manifest"

# 설치 시점 사본으로 굳은 guest 어댑터 엔진 파일의 본문 — 동기가 실제로 상류 값을 덮는지 판정하는 표식.
_STALE_GUEST_BODY = "# 설치 시점 사본으로 동결된 채택자 파일 (업그레이드 게이트 픽스처)\n"
# 설치 시 `{{DATE}}` 가 값으로 굳었던 형상(T-0578 착지 전 pm_import 거동)의 재현값.
_FROZEN_INSTALL_DATE = "2025-11-03"
# 채택자가 손으로 유지하던 치환 결과(제보 형상) — 상류 산문과 겹치지 않는 픽스처 소유 문장.
_SUBSTITUTED_README_LINE = f"{_ADOPTER_NAME} 프로젝트의 비-코드 산출물 모음 (설치 시 치환된 옛 본문)."

# T-0571 이 마이그레이션 발생 시 내는 표기(조용한 변환 금지) — 티켓 인터페이스가 고정한 문구.
_MARKER_MIGRATION_NOTICE = "guest 절 마커 세대 마이그레이션"

_RENDER_TOKEN_RE = re.compile(r"\{\{[A-Z_]+\}\}")
# 스킬 §manifest reconcile 의 선-cp 지시. placeholder(`<cache-or-path>`·`<harness>`)를 포함한
#   한 줄 bash 명령이라 원본/대상 두 좌표만 뽑는다.
_SKILL_MANIFEST_COPY_RE = re.compile(
    r"^[ \t]*cp[ \t]+(?P<source>\S*templates/\S*engine\.manifest)"
    r"[ \t]+(?P<target>\S*engine\.manifest)[ \t]*$",
    re.MULTILINE,
)


def _engine_pm_import():
    """canonical pm_import 모듈 — 라이브 `opencode models` 조회를 막은 hermetic 인스턴스.

    `_load_pm_import` 는 호출마다 새 모듈 객체를 만드므로 이 stub 은 이 파일 안에만 남는다."""
    module = _load_pm_import()
    module._real_models_runner = lambda: (False, [])
    return module


_PM_IMPORT = _engine_pm_import()


# ── 채택자 트리 조작 헬퍼 ──────────────────────────────────────────────────────


def _run_pm_update(dest: Path, *args: str) -> subprocess.CompletedProcess:
    """채택자 트리의 pm_update.py 를 실제 CLI 로 구동한다(cwd=dest·비대화형·capture).

    upstream 은 명시 `--from REPO` 다 — fresh 인스턴스의 local.conf `upstream=` 은 URL 이라
    엔진이 자동 진행하지 않는다(로컬 checkout 명시가 채택자의 실제 절차)."""
    proc = subprocess.run(
        [sys.executable, str(dest / ".project_manager" / "tools" / "pm_update.py"),
         "--from", str(REPO), *args],
        cwd=str(dest),
        capture_output=True,
        text=True,
        # 엔진 출력은 UTF-8(한글 포함) — 부모 콘솔 로케일로 디코드하면 캡처가 깨진다.
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PM_NONINTERACTIVE": "1"},
        timeout=300,
    )
    assert proc.returncode == 0, (
        f"pm_update rc={proc.returncode}\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}")
    return proc


def _combined_output(proc: subprocess.CompletedProcess) -> str:
    """채택자가 화면에서 보는 전체 출력 — 경고 채널(stderr)과 진행 표기(stdout)를 함께 본다."""
    return f"{proc.stdout}\n{proc.stderr}"


def _manifest_path(dest: Path) -> Path:
    return dest / ".project_manager" / "engine.manifest"


def _manifest_text(dest: Path) -> str:
    return _manifest_path(dest).read_text(encoding="utf-8")


def _guest_manifest_block(dest: Path) -> str | None:
    """engine.manifest 의 add-harness guest 절(마커 포함) — 어느 세대 마커든 인식, 부재면 None.

    엔진 파서를 부르지 않고 리터럴로 직접 훑는다 — 엔진과 같은 함수를 쓰면 마커 리터럴이 통째로
    바뀌어도 게이트가 함께 따라가 결함을 못 본다(이 게이트가 존재하는 이유)."""
    text = _manifest_text(dest)
    for begin in (_CURRENT_GUEST_BEGIN, _LEGACY_GUEST_BEGIN):
        if begin in text and _GUEST_END_MARKER in text:
            start = text.index(begin)
            end = text.index(_GUEST_END_MARKER, start) + len(_GUEST_END_MARKER)
            return text[start:end]
    return None


def _guest_rows(block: str) -> list[str]:
    """guest 절의 등재 행(주석·빈 줄 제외)."""
    return [line.strip() for line in block.splitlines()
            if line.strip() and not line.strip().startswith("#")]


def _guest_engine_rows(block: str) -> list[str]:
    """guest 절의 **엔진 행** — `@render` 없는 행(update 채널 소유·byte-copy 대상)."""
    return [row for row in _guest_rows(block) if "@render" not in row]


def _guest_render_rows(block: str) -> list[str]:
    """guest 절의 **어댑터 렌더물 행** — add-harness refresh 소유(update 는 안 건드린다)."""
    return [row for row in _guest_rows(block) if "@render" in row]


def _row_path(row: str) -> str:
    return row.split()[0].replace("\\", "/")


def _row_source(row: str) -> str:
    """행의 `@source=<루트-상대>` — 없으면 dest 경로와 같은 좌표(root-sourced)."""
    for token in row.split()[1:]:
        if token.startswith("@source="):
            return token[len("@source="):].replace("\\", "/")
    return _row_path(row)


def _files_under(snapshot: dict[str, bytes], rel: str) -> list[str]:
    """등재 행 한 줄이 커버하는 실파일 relpath — 파일 행은 그 자신, 디렉토리 행은 하위 전체.

    트리 열거는 직접 재귀하지 않고 `_snapshot_tree` 결과를 접두사로 거른다 — repo-owned 열거
    seam 우회(재귀 tree-walk) 없이 같은 판정을 낸다."""
    return sorted(path for path in snapshot if path == rel or path.startswith(rel + "/"))


def _frozen_guest_files(dest: Path) -> list[str]:
    """설치 시점 사본으로 동결된 채로 남은 파일의 relpath — 픽스처 표식으로 판정한다.

    스냅샷이 백업 디렉토리(`.pm_import_backups`)를 이미 뺀다 — add-harness 가 남긴 백업 사본까지
    세면 동기가 끝난 뒤에도 동결이 남은 것처럼 보인다."""
    frozen_body = _STALE_GUEST_BODY.encode("utf-8")
    return sorted(rel for rel, payload in _snapshot_tree(dest).items() if payload == frozen_body)


def _prime_engine_to_canonical(dest: Path) -> None:
    """채택자 엔진 사본을 canonical(루트) 판으로 승격 — 이 게이트의 전제 조건.

    pm_import 는 `templates/<flavor>/` 사본을 깔고 그 사본은 wave 중 stale 할 수 있다. 승격이 없으면
    시나리오가 구동하는 pm_update 가 **옛 엔진**이라, 게이트가 현행 엔진의 업그레이드 처리 대신
    templates 동기 시점을 측정하게 된다. 엔진 도구는 manifest bare 등재(root-sourced)라 정상 동기
    한 번으로 전 도구가 **함께** canonical 이 된다(부분 승격에 따른 형제 모듈 skew 없음)."""
    _run_pm_update(dest)
    for rel in (".project_manager/tools/pm_update.py", ".project_manager/tools/board.py"):
        assert (dest / rel).read_bytes() == (REPO / rel).read_bytes(), (
            f"엔진 승격 실패 — {rel} 가 canonical 과 다르다(게이트가 옛 엔진을 검증하게 된다)")


def _inject_legacy_guest_section(dest: Path) -> None:
    """현행 guest 절을 **옛 세대 절**로 되돌린다 — 옛 마커 리터럴 + 엔진 행 부재.

    엔진 행(비-`@render`)은 그 세대에 존재하지 않던 채널이므로 함께 지운다. 그 결과가 곧 제보
    채택자의 디스크 형상이고, 마커 세대 인식(T-0571)과 엔진 행 파생 백필을 함께 태운다."""
    block = _guest_manifest_block(dest)
    assert block is not None, "픽스처 오류 — add-harness 직후인데 guest 절이 없다"
    legacy_block = "\n".join([_LEGACY_GUEST_BEGIN, *_guest_render_rows(block), _GUEST_END_MARKER])
    text = _manifest_text(dest)
    _manifest_path(dest).write_text(text.replace(block, legacy_block), encoding="utf-8", newline="\n")
    assert _LEGACY_GUEST_BEGIN in _manifest_text(dest)
    assert _CURRENT_GUEST_BEGIN not in _manifest_text(dest)


def _freeze_guest_engine_files(dest: Path, block: str) -> list[str]:
    """guest 어댑터 **엔진 파일**을 설치 시점 사본으로 동결한다 — 동기 채널 검증의 대조군.

    렌더물(`@render` 행)은 add-harness refresh 소유라 동결 대상이 아니다(update 가 안 건드리는 게
    정상). 동결 좌표는 현행 절의 엔진 행에서 파생한다 — flavor 별로 손 열거하지 않는다."""
    snapshot = _snapshot_tree(dest)
    frozen = []
    for row in _guest_engine_rows(block):
        for rel in _files_under(snapshot, _row_path(row)):
            (dest / rel).write_text(_STALE_GUEST_BODY, encoding="utf-8", newline="\n")
            frozen.append(rel)
    return sorted(frozen)


def _inject_substituted_documents(dest: Path) -> None:
    """설치 시 값으로 굳은 치환 결과를 주입한다 — T-0578 착지 전 채택자 디스크 형상.

    두 방향을 함께 만든다: 채택자 가시 문서(`wiki/README.md`)에 프로젝트명이 박힌 옛 본문,
    소비 시점 소유 템플릿 2종(`pm_state.template.md`·`domain/_template.md`)에 설치일로 굳은
    `{{DATE}}`. 전자는 상류가 토큰을 지워 수렴해야 하고, 후자는 토큰-form 으로 되돌아와야 한다."""
    readme = dest / ".project_manager" / "wiki" / "README.md"
    text = readme.read_text(encoding="utf-8")
    assert _SUBSTITUTED_README_LINE not in text, "픽스처 오류 — 주입 문장이 이미 상류 본문에 있다"
    lines = text.splitlines()
    readme.write_text(
        "\n".join([lines[0], "", _SUBSTITUTED_README_LINE, *lines[1:]]) + "\n", encoding="utf-8", newline="\n")

    for rel in ("pm_state.template.md", "domain/_template.md"):
        path = dest / ".project_manager" / "wiki" / rel
        text = path.read_text(encoding="utf-8")
        frozen = text.replace("{{DATE}}", _FROZEN_INSTALL_DATE)
        assert frozen != text, f"픽스처 오류 — {rel} 에 굳힐 {{{{DATE}}}} 가 없다(공허 주입)"
        path.write_text(frozen, encoding="utf-8", newline="\n")


def _build_upgraded_adopter(
        tmp_path: Path, *, guest_marker_generation: str, substituted: bool,
        host: str = _HOST_HARNESS, guest: str = _REPORTED_GUEST) -> Path:
    """구세대 상태를 주입한 업그레이드 채택자 트리를 만들고 그 경로를 낸다.

    ``guest_marker_generation``
      ``"legacy"`` — v1.4.5 세대 guest 절(옛 마커 리터럴 + 엔진 행 부재).
      ``"current"`` — 현행 add-harness 가 쓴 그대로의 절(대조군·멱등 축).
    ``substituted`` — 설치 시 값으로 굳은 치환 결과(README 프로젝트명·template 실날짜) 주입 여부.

    guest 어댑터 엔진 파일은 두 세대 모두 설치 시점 사본으로 동결한다 — 제보의 실제 증상이
    "설치 이후 상류 변경을 한 번도 못 받았다" 이기 때문이다.
    """
    assert guest_marker_generation in ("legacy", "current"), guest_marker_generation
    dest = tmp_path / f"upgraded-{host}-{guest}-{guest_marker_generation}"

    # ① 현행 엔진으로 설치 + 엔진 사본 canonical 승격(게이트 전제).
    assert _PM_IMPORT.main(
        ["--new", str(dest), "--harness", host, "--name", _ADOPTER_NAME, "--fill", "manual"]
    ) == 0, f"{host} import 실패"
    _prime_engine_to_canonical(dest)

    # ② add-harness 이력 — guest 절 등재 + guest 어댑터 실파일.
    plan = _PM_IMPORT.add_harness(dest, guest, dry_run=False, source_root=REPO)
    assert plan, f"{host}→{guest}: add-harness plan 이 비어 있다(픽스처 무효)"
    block = _guest_manifest_block(dest)
    assert block is not None, f"{host}→{guest}: add-harness 가 guest 절을 등재하지 않았다"

    # ③ guest 어댑터 엔진 파일 동결(설치 시점 사본).
    frozen = _freeze_guest_engine_files(dest, block)
    assert frozen, f"{host}→{guest}: 동결할 guest 엔진 파일 0(공허 픽스처)"

    # ④ 옛 세대 guest 절로 되돌리기.
    if guest_marker_generation == "legacy":
        _inject_legacy_guest_section(dest)

    # ⑤ 설치 시 굳은 치환 결과.
    if substituted:
        _inject_substituted_documents(dest)
    return dest


# ── S1 guest 절 생존 — 옛 마커 채택자의 동기 ──────────────────────────────────


@pytest.mark.parametrize("generation", ("legacy", "current"))
def test_upgrade_adopter_guest_section_survives_sync_and_marker_converges(tmp_path, generation):
    """옛 마커 채택자가 동기 후에도 guest 절을 갖고, 마커가 현행 리터럴로 수렴하며, 재동기가 멱등이다.

    결함 형상: guest 절 경계 마커 리터럴이 v1.5.0 구간에 바뀌어, 그 전에 `add-harness` 를 쓴
    채택자가 `pm_update` 를 돌리면 읽기 쪽이 절을 못 알아보고 **경고 없이** 통째로 지웠다
    (렌더/overlay 스캔 커버리지가 그 시점에 끊긴다). 옛 세대(대조군: 현행 세대) 절을 디스크에 두고
    실 CLI 동기를 태워 절 생존·마커 수렴·멱등을 못박는다.
    """
    dest = _build_upgraded_adopter(
        tmp_path, guest_marker_generation=generation, substituted=True)
    before = _guest_manifest_block(dest)
    assert before is not None
    render_rows_before = _guest_render_rows(before)
    assert render_rows_before, "픽스처 오류 — guest 렌더물 행 0"

    result = _run_pm_update(dest)

    after = _guest_manifest_block(dest)
    assert after is not None, (
        f"{generation} 세대 guest 절이 동기 후 사라졌다(조용한 소실) — 절 안의 `@render`·엔진 행이 "
        f"어느 채널에도 안 남는다.\n--- stdout ---\n{result.stdout}")
    # 쓰기는 항상 현행 리터럴 단일 세대다 — 옛 리터럴이 남으면 다음 세대에서 같은 사고가 반복된다.
    assert _CURRENT_GUEST_BEGIN in after, f"guest 절 시작 마커가 현행 리터럴이 아니다:\n{after}"
    assert _LEGACY_GUEST_BEGIN not in _manifest_text(dest), (
        "옛 세대 마커가 manifest 에 잔존 — 1 회 마이그레이션이 안 일어났다")
    # 렌더물 행은 add-harness refresh 소유라 동기가 내용을 바꾸지 않는다(경로 집합 보존).
    assert {_row_path(row) for row in _guest_render_rows(after)} == \
        {_row_path(row) for row in render_rows_before}, (
            f"guest 렌더물 행 집합이 동기로 변형됐다\n이전: {render_rows_before}\n이후: {after}")

    if generation == "legacy":
        assert _MARKER_MIGRATION_NOTICE in result.stdout, (
            "옛 리터럴을 현행으로 바꿨는데 표기가 없다(조용한 변환 금지)\n"
            f"--- stdout ---\n{result.stdout}")
    else:
        assert _MARKER_MIGRATION_NOTICE not in result.stdout, (
            "현행 세대인데 마이그레이션 표기가 났다(멱등 위반·헛 변환)\n"
            f"--- stdout ---\n{result.stdout}")

    # 다음 동기는 멱등 — manifest 가 바이트로 안정(매 sync 재기록 churn 0).
    manifest_after_first = _manifest_text(dest)
    _run_pm_update(dest)
    assert _manifest_text(dest) == manifest_after_first, (
        "재동기가 engine.manifest 를 또 고쳤다(멱등 위반·영구 churn)")


# ── S2 frozen 가시성 — guest 어댑터 엔진 파일 ────────────────────────────────


@pytest.mark.parametrize("guest", _GUEST_HARNESSES)
def test_upgrade_adopter_guest_adapter_is_never_silently_frozen(tmp_path, guest):
    """설치 시점 사본으로 동결된 guest 어댑터 엔진 파일이 **조용히** 동결로 남지 않는다.

    두 결과 중 하나만 인정한다 — (a) 동기 채널이 상류 값으로 수렴시키거나, (b) 수렴이 없다면
    출력이 동결 좌표를 짚고 복구 채널(`add-harness`)을 안내한다. 금지되는 건 셋째 경우, 즉 계획
    0 건·경고 0 건으로 지나가 채택자가 자기 어댑터가 몇 달째 옛 사본인 걸 모르는 상태다(제보의
    실제 증상: 설치 시점 `.codex/**` 가 상류 6 커밋을 못 받은 채 조용히 동결).

    guest 절 자체는 어느 쪽 결과에서도 생존해야 한다 — 절이 사라지면 다음 동기부터는 동결 사실을
    표면화할 근거조차 없다.
    """
    dest = _build_upgraded_adopter(
        tmp_path, guest_marker_generation="legacy", substituted=True, guest=guest)
    frozen_before = _frozen_guest_files(dest)
    assert frozen_before, f"{guest}: 픽스처가 동결 사본을 안 만들었다(공허 게이트)"

    result = _run_pm_update(dest)
    output = _combined_output(result)

    block = _guest_manifest_block(dest)
    assert block is not None, (
        f"{guest}: 동기가 guest 절을 지웠다 — 동결 표면화의 근거가 사라진다\n{output}")

    frozen_after = _frozen_guest_files(dest)
    if frozen_after:
        # (b) 아직 동기 채널이 없다면 최소한 loud·actionable 이어야 한다.
        assert any(rel in output for rel in frozen_after), (
            f"{guest}: 동결 파일 {frozen_after} 이 그대로인데 출력이 좌표를 짚지 않는다"
            f"(조용한 동결)\n{output}")
        assert "add-harness" in output, (
            f"{guest}: 동결을 알렸으나 복구 채널 안내가 없다(actionable 아님)\n{output}")
        return

    # (a) 수렴했다면 상류 값과 **byte 일치** 여야 한다 — 표식만 지운 헛 동기를 배제한다.
    engine_rows = _guest_engine_rows(block)
    assert engine_rows, (
        f"{guest}: 파일은 갱신됐는데 guest 절에 엔진 행이 없다 — 다음 동기의 소유 근거가 없다\n{block}")
    snapshot = _snapshot_tree(dest)
    compared = 0
    for row in engine_rows:
        source_root = REPO / _row_source(row)
        row_path = _row_path(row)
        for rel in _files_under(snapshot, row_path):
            if rel not in frozen_before:
                continue
            source = source_root if source_root.is_file() else \
                source_root / rel[len(row_path) + 1:]
            assert source.is_file(), f"{guest}: 상류 대응 부재 — {rel} → {source}"
            assert snapshot[rel] == source.read_bytes(), (
                f"{guest}: {rel} 이 상류와 byte 불일치(동기가 값을 안 가져왔다)")
            compared += 1
    assert compared, f"{guest}: 상류와 대조한 파일 0(공허 통과)"


# ── S3 토큰 안정성 — 치환 소유권과 진동 ──────────────────────────────────────


@pytest.mark.parametrize("substituted", (True, False))
def test_upgrade_adopter_render_tokens_settle_without_oscillation(tmp_path, substituted):
    """동기가 채택자 가시 문서에 토큰을 되돌리지 않고, 소비 시점 소유 템플릿은 토큰을 유지한다.

    결함 형상: render 토큰을 본문에 담은 wiki 파일이 manifest 에 bare 등재돼, byte-copy 동기가
    채택자의 치환된 문서를 미치환 토큰으로 되돌렸다(실측: 동기 후 `wiki/README.md` 가
    `{{PROJECT_NAME}}` 로 회귀). 반대편 반쪽은 설치가 값으로 굳혀 버린 템플릿이다 —
    `pm_state.template.md`·`domain/_template.md` 의 `{{DATE}}` 는 **소비 시점** 소유라 채택자
    디스크에 토큰-form 으로 있어야 정상이고, 굳어 있으면 새 산출물이 설치일을 물려받는다.

    두 방향을 한 트리에서 함께 보고, 재동기 진동 0(같은 upstream 을 두 번 받으면 트리가 바이트로
    안정)까지 확인한다 — 진동은 소유권이 두 주체로 갈렸을 때의 증상이라 1 회 관측으로는 안 보인다.
    """
    dest = _build_upgraded_adopter(
        tmp_path, guest_marker_generation="legacy", substituted=substituted)
    _run_pm_update(dest)

    readme = (dest / ".project_manager" / "wiki" / "README.md").read_text(encoding="utf-8")
    leaked = sorted(set(_RENDER_TOKEN_RE.findall(readme)))
    assert not leaked, (
        f"동기 후 채택자 가시 문서(wiki/README.md)에 render 토큰 {leaked} — byte-copy 가 치환된 "
        "문서를 토큰-form 으로 되돌렸다")
    assert _SUBSTITUTED_README_LINE not in readme, (
        "채택자가 손으로 유지하던 치환 본문이 남아 있다 — 상류 중립 문구가 안 내려왔다")

    for rel in ("pm_state.template.md", "domain/_template.md"):
        text = (dest / ".project_manager" / "wiki" / rel).read_text(encoding="utf-8")
        assert "{{DATE}}" in text, (
            f"{rel} 에 `{{{{DATE}}}}` 가 없다 — 소비 시점 소유 토큰이 값으로 굳었다"
            "(이 템플릿에서 나오는 산출물이 옛 날짜를 물려받는다)")
        assert _FROZEN_INSTALL_DATE not in text, (
            f"{rel} 에 설치 시점으로 굳은 날짜({_FROZEN_INSTALL_DATE})가 잔존 — 동기가 토큰-form 을 "
            "복구하지 못했다")

    # 진동 0 — 같은 upstream 을 한 번 더 받아도 트리가 바이트로 안정.
    before = _snapshot_tree(dest)
    _run_pm_update(dest)
    after = _snapshot_tree(dest)
    changed = sorted(rel for rel in set(before) | set(after) if before.get(rel) != after.get(rel))
    assert changed == [], f"재동기가 트리를 또 바꿨다(진동): {changed}"


# ── S4 reconcile 절차 안전 — 스킬 절차와 엔진 단독 경로 ──────────────────────


def _skill_manifest_copy_source(skill_text: str, *, host: str) -> Path | None:
    """스킬이 지시하는 manifest 선-cp 의 **원본 경로** — 지시가 없으면 None.

    placeholder 두 개(`<cache-or-path>`·`<harness>`)를 이 채택자의 실좌표로 해소한다. 지시가
    있는데 해소가 안 되면 재연이 조용히 무력화되므로 loud 하게 죽인다."""
    match = _SKILL_MANIFEST_COPY_RE.search(skill_text)
    if match is None:
        return None
    assert match.group("target") == _ADOPTER_MANIFEST_REL, (
        f"스킬 cp 대상이 채택자 manifest 가 아니다: {match.group('target')!r}")
    (template_dir,) = _PM_IMPORT.HARNESS_TEMPLATE_DIRS[host]
    resolved = Path(
        match.group("source")
        .replace("<cache-or-path>", str(REPO))
        .replace("<harness>", template_dir))
    assert resolved.is_file(), (
        f"스킬 cp 원본을 실좌표로 못 풀었다: {match.group('source')!r} → {resolved} "
        "(스킬 placeholder 어휘가 바뀌었다면 이 재연도 함께 고쳐야 한다)")
    return resolved


def test_upgrade_adopter_skill_manifest_reconcile_never_silently_drops_guest(tmp_path):
    """스킬이 지시하는 manifest reconcile 절차를 재연해도 guest 절이 **조용히** 사라지지 않는다.

    `pm-update` 스킬은 동기 전에 채택자 manifest 를 상류 flavor manifest 로 덮는 선-cp 를 지시한다
    (새 등재 항목이 채택자에 도달하게 하는 목적). 그 cp 는 파일 통째 덮기라 add-harness 채택자의
    guest 절도 함께 지운다 — 절이 사라진 뒤엔 마이그레이션(T-0571)도 살릴 대상이 없다.

    인정하는 결과는 둘이다 — (a) 절차를 그대로 밟아도 guest 절이 생존하거나, (b) 절이 사라졌다면
    엔진이 그 사실을 loud 하게 짚고 복구(`add-harness` 재실행)를 안내하고, 그 복구가 실제로 절을
    되살린다. 금지되는 건 절이 조용히 사라져 채택자가 커버리지 상실을 모르는 상태다. **(b) 로
    green 인 동안은 스킬 본문이 개선 대상**이라는 신호다(guest 절이 있는 채택자에겐 cp 대신 엔진
    reconcile 경로 안내 — 그 경로가 실재함은 아래 테스트가 실측한다).
    """
    dest = _build_upgraded_adopter(
        tmp_path, guest_marker_generation="legacy", substituted=True)
    guest_paths_before = [_row_path(row) for row in _guest_rows(_guest_manifest_block(dest))]
    assert guest_paths_before, "픽스처 오류 — guest 절 등재 행 0"

    # 채택자가 실제로 읽는 사본(설치된 스킬)에서 절차를 뽑는다 — 소스 트리가 아니라 출하물이 진실.
    skill = dest / ".claude" / "skills" / "pm-update" / "SKILL.md"
    assert skill.is_file(), f"채택자 트리에 pm-update 스킬 부재: {skill}"
    copy_source = _skill_manifest_copy_source(skill.read_text(encoding="utf-8"), host=_HOST_HARNESS)
    if copy_source is not None:
        _manifest_path(dest).write_bytes(copy_source.read_bytes())

    result = _run_pm_update(dest)
    output = _combined_output(result)

    if _guest_manifest_block(dest) is not None:
        return  # (a) 절차가 절을 보존한다 — 더 볼 것 없음.

    # (b) 절이 사라졌다면 loud·actionable 이어야 하고, 안내된 복구가 실제로 작동해야 한다.
    assert any(path in output for path in guest_paths_before), (
        f"guest 절이 절차 중 사라졌는데 출력이 그 좌표({guest_paths_before})를 짚지 않는다"
        f"(조용한 소실)\n{output}")
    assert "add-harness" in output, (
        f"guest 절 소실을 알렸으나 복구 채널 안내가 없다(actionable 아님)\n{output}")
    _PM_IMPORT.add_harness(dest, _REPORTED_GUEST, dry_run=False, source_root=REPO)
    assert _guest_manifest_block(dest) is not None, (
        "안내대로 add-harness 를 재실행했는데 guest 절이 복구되지 않는다 — 안내가 거짓이다")


def test_upgrade_adopter_engine_reconciles_stale_manifest_without_manual_copy(tmp_path):
    """엔진 단독 동기가 옛 manifest 를 상류 형상으로 되맞추고 그 항목까지 같은 실행에서 전파한다.

    스킬의 선-cp 는 "새 항목이 채택자에 도달하려면 dest manifest 가 먼저 알아야 한다" 를 근거로
    한다. 그 전제가 아직 참인지 실측한다 — 등재 행 하나를 지우고 그 파일을 옛 내용으로 되돌린
    채택자에서 `pm_update` **한 번**이 (a) 행 복구 (b) 파일 전파 (c) guest 절 보존을 함께 해내면,
    guest 절이 있는 채택자에게 선-cp 는 불필요하며(수동 복사 없이 같은 결과) 위험만 남는다.

    이 테스트가 red 로 바뀌면 선-cp 지시는 다시 필요해진 것이다 — 그때는 위 재연 테스트의 판정과
    함께 스킬 본문을 재검토해야 한다.
    """
    dest = _build_upgraded_adopter(
        tmp_path, guest_marker_generation="legacy", substituted=True)

    # 옛 manifest 재현 — core 등재 행 하나를 지우고 그 문서를 옛 내용으로 되돌린다. 대상은 엔진이
    #   실행 중 import 하는 도구가 아니라 문서 행으로 고른다(도구를 부수면 판정 대신 크래시를 본다).
    assert _guest_manifest_block(dest) is not None, "픽스처 오류 — guest 절 부재"
    core_text = _manifest_text(dest).split(_LEGACY_GUEST_BEGIN)[0]
    victim = next(
        (line.strip() for line in core_text.splitlines()
         if line.strip().endswith(".md") and "@" not in line and (dest / line.strip()).is_file()),
        None)
    assert victim, "core 등재 문서 행을 못 찾았다(픽스처 전제 붕괴)"
    text = _manifest_text(dest)
    dropped = "\n".join(line for line in text.splitlines() if line.strip() != victim) + "\n"
    assert dropped != text, f"{victim} 행 제거가 무효(공허 픽스처)"
    _manifest_path(dest).write_text(dropped, encoding="utf-8", newline="\n")
    (dest / victim).write_text(_STALE_GUEST_BODY, encoding="utf-8", newline="\n")

    _run_pm_update(dest)

    restored = [line.strip() for line in _manifest_text(dest).splitlines()]
    assert victim in restored, (
        f"엔진 단독 동기가 옛 manifest 의 누락 행({victim})을 복구하지 못했다 — 선-cp 없이는 "
        "새 등재 항목이 채택자에 도달하지 않는다")
    assert (dest / victim).read_text(encoding="utf-8") != _STALE_GUEST_BODY, (
        f"{victim} 이 같은 실행에서 전파되지 않았다 — 채택자는 동기를 두 번 돌려야 한다")
    assert _guest_manifest_block(dest) is not None, (
        "엔진 단독 경로가 guest 절을 지웠다 — 안전한 대안 경로가 없다")


# ── S5 instance-owned config RUN1/RUN2 완료 게이트 (T-0591) ────────────────

_ADAPTER_HOOKS_REL = ".codex/hooks.json"
_LEGACY_BLOCKING_HOOKS = '{"hooks": {"PreCompact": [{"command": "legacy-block"}]}}\n'


def _run_adopter_tool(dest: Path, tool: str, *args: str) -> subprocess.CompletedProcess:
    """채택자 사본 CLI를 hermetic subprocess로 실행 — 성공/실패 판정은 호출 테스트가 한다."""
    return subprocess.run(
        [sys.executable, str(dest / ".project_manager" / "tools" / tool), *args],
        cwd=str(dest), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PM_NONINTERACTIVE": "1"}, timeout=300,
    )


# T-0590 재현행: 추가 리뷰어 온보딩(기본 프로필 상수 + maybe_prompt_external_review)이
#   updater 본문에 들어오면서 whole-file SHA 가 이동했다. 역적용 delta(T-0591 어댑터 수취 채널)는
#   그대로 적용되므로 실 배달 경계는 불변이고, RUN1 fixture 는 새 온보딩을 포함한 실 updater 다
#   (이 경로는 PM_NONINTERACTIVE=1 로 돌아 질문·conf write 가 발화하지 않는다).
#   T-0590 R2 에서 한 번 더 이동 — 온보딩의 "이미 결정됨" 판정면을 conf raw 텍스트 substring 에서
#   `_read_local_conf` 파싱 키 존재로 바꿨다(주석 한 줄이 결정을 가로채던 결함). 재검토 결과 이동
#   범위는 maybe_prompt_external_review 본문뿐이고, 이 fixture 가 ratchet 하는 배달 경계
#   (source/manifest planning → apply → self-update 순서)와 역적용 delta 구간에는 겹침이 없다.
#   T-0590 R3 에서 또 한 번 이동 — `_main` 의 변경 0 수렴 지점이 추가 리뷰어 opt-in 도 호출하게
#   됐다(has-changes 경로에서만 부르던 결함). 이동분은 `print("최신 — 변경 없음.")` 뒤의
#   수렴 블록뿐이라 역적용 delta 구간(어댑터 config 게이트·sync_adapter_configs 본문)과 겹치지
#   않고, 이 테스트의 RUN1 은 has-changes·RUN2 는 red rc1(프롬프트 이전 중단)이며 두 실행 모두
#   PM_NONINTERACTIVE=1 이라 질문·conf write 는 발화하지 않는다.
#   T-0590 R3 후속에서 또 이동 — 온보딩이 기존 대상(레거시 `reviewer_cmd`·구조화 튜플)을 덮지
#   않도록 대상 판정(classify_additional_reviewer_target)과 활성 플래그 전용 블록이 들어왔고,
#   EOF 응답이 false 를 박제하지 않게 바뀌었다. 이동 범위는 온보딩 상수/헬퍼와
#   maybe_prompt_external_review 본문뿐이라 역적용 delta 구간·배달 경계와 겹치지 않는다.
#   T-0590 R4 에서 또 이동 — 온보딩 응답의 기록 시점 판정이 질문 **전** 판정에서 커밋 시점
#   재읽기·재판정(배타락 + 단일 O_APPEND)으로 바뀌었다. 들어온 것은 온보딩 상수/헬퍼
#   (`_load_file_lock`·`_local_conf_lock_path`·`_local_conf_write_lock`·
#   `_append_local_conf_atomic`·`_commit_additional_reviewer_optin`)와
#   maybe_prompt_external_review 본문, 그리고 `contextlib` import 한 줄뿐이다. 역적용 delta 의
#   4개 anchor(어댑터 config 게이트·sync_adapter_configs 본문·`_main` 수렴 블록)와 배달 경계
#   (source/manifest planning → apply → self-update 순서)에는 겹침이 없고, 이 테스트의 두 실행은
#   모두 PM_NONINTERACTIVE=1 이라 질문·conf write 자체가 발화하지 않는다.
#   T-0590 4차에서 또 이동 — conf 락의 단위가 "opt-in append" 에서 "conf 를 읽고 쓰는 구간"으로
#   넓어졌다. 들어온 것은 `_local_conf_lock_path` 의 공용 seam 위임(+손상 사본 폴백),
#   `_local_conf_write_lock` 의 `local_conf_write_lock` 호출, `maybe_prompt_delegate_optin` 의
#   기록부(락 안 재판정 + 단일 원자 추가)뿐이다. 셋 다 온보딩 질문 경로이고 역적용 delta 의 4개
#   anchor(어댑터 config 게이트·sync_adapter_configs 본문·`_main` 수렴 블록)와 배달 경계
#   (source/manifest planning → apply → self-update 순서)에는 겹치지 않는다 — 실제로 이번에도 네
#   anchor 가 모두 유일하게 해소됐다(`_slice_replace` 의 count==1 단언 통과). 두 실행이
#   PM_NONINTERACTIVE=1 이라 질문·conf write 는 여전히 발화하지 않으므로 배달 의미도 불변이고,
#   현재화한 것은 기대 SHA 하나뿐이다.
#   T-0590 R6 에서 또 이동 — conf 락 구간이 "쓰기" 에서 "읽기→계획→쓰기→검증" 으로 넓어졌다.
#   들어온 것은 `_conf_lock_section`(구세대 file_lock 사본 호환·518행대)과 rev 기록의 계획 분리
#   (`_upstream_rev_updates`·`_warn_missing_conf_for_rev`·`record_upstream_revs` 본문·2081행대)뿐이다.
#   역적용 delta 의 anchor 는 `sync_adapter_configs`(4488행대)·`_print_adapter_config_finding`·
#   `_main` 수렴 블록(5449행대)이라 겹치는 구간이 없고, 이번에도 네 anchor 가 모두 유일하게
#   해소됐다(`_slice_replace` 의 count==1 단언 통과). 배달 경계(source/manifest planning → apply →
#   self-update 순서)는 불변이며, 이 테스트의 두 실행은 PM_NONINTERACTIVE=1 이라 온보딩 질문·
#   conf write 가 발화하지 않는다. rev 기록은 두 실행 모두에서 종전과 같은 키를 같은 값으로
#   쓴다(락이 추가됐을 뿐 기록 의미 불변) — 현재화한 것은 기대 SHA 하나뿐이다.
#   T-0597 에서 또 이동 — opt-in 게이트 키가 `additional_reviewer_enabled` 로 개칭되고 개칭 전
#   구키가 1릴리즈 fallback 이 됐다. 들어온 것은 온보딩 상수
#   (`ADDITIONAL_REVIEWER_ENABLED_KEY`·`LEGACY_EXTERNAL_REVIEW_ENABLED_KEY`·
#   `LEGACY_ENABLED_KEY_DEPRECATION`)와 판정 헬퍼(`additional_reviewer_decision_key`),
#   그 헬퍼를 쓰는 `_commit_additional_reviewer_optin`·`maybe_prompt_external_review` 의 결정 분기,
#   그리고 블록/힌트 문자열의 키 이름뿐이다. 역적용 delta 의 anchor(어댑터 config 게이트·
#   `sync_adapter_configs` 본문·`_main` 수렴 블록)와 배달 경계(source/manifest planning → apply →
#   self-update 순서)에는 겹침이 없고, 이번에도 네 anchor 가 모두 유일하게 해소됐다
#   (`_slice_replace` 의 count==1 단언 통과). 두 실행은 PM_NONINTERACTIVE=1 이고 fixture 채택자
#   conf 에 구키가 없어 새 안내 1줄도 발화하지 않는다 — 현재화한 것은 기대 SHA 하나뿐이다.
#   T-0606 에서 또 이동 — 훅 세트 세대 정합 검사가 들어왔다. 새로 들어온 것은 판정 채널
#   (`check_adapter_hook_sets`·`_adapter_hook_set_gate_failed`·`_print_adapter_hook_set_finding`),
#   `_main` 두 지점의 그 채널 호출, 그리고 `apply` 의 훅 세트 원자 write 분기
#   (`resolve_hook_set_predicate` → `_atomic_copy2`)다. 역적용 delta 의 네 anchor 는 그대로
#   유일하게 해소된다(새 블록이 anchor **앞**에 들어가 슬라이스 범위를 건드리지 않는다). 배달
#   경계도 불변이다: 이 fixture 채택자는 codex 단독이라 훅 세트 판정이 항상 빈 결과고(게이트
#   미발화·출력 0줄), apply 분기도 등재 파일이 `.codex/pm_orch_codex.py` 뿐이라 이 fixture 의
#   변경 목록에 들지 않는다. 뒤이은 수렴 라운드가 더한 경로 스코프 반쪽 갱신 가드
#   (`refuse_partial_hook_set_scope`)도 `--paths` 전용이라 이 fixture(스코프 없음)에서 비발화다.
#   T-0610 이 세대 선언 해소를 단일 지점(`resolve_hook_set_generation`)으로 통일했으나 그 역시
#   같은 두 소비자(원자 write 판정자·훅 세대 채널)만 태우고, 이 fixture 채택자는 codex 단독이라
#   판정이 여전히 빈 결과다 — 현재화한 것은 기대 SHA 하나뿐이다.
#   T-0607 이 동기 실행 중 rev 혼합 흡수를 넣으며 또 이동했다. 배달 경계는 불변이다: 새 코드는
#   marked skew 가 실제로 났을 때만 분기하고(이 fixture 의 RUN1 은 rev 가 단일이라 어느 경계도
#   발화하지 않는다), 종료 시 수렴 검증도 혼합일 때만 출력한다(단일 rev → 무출력·rc 불변).
#   역적용 delta 의 anchor 도 그대로다 — 흡수 지점은 전부 anchor 바깥이고, 슬라이스로 교체되는
#   `sync_adapter_configs` 안의 변경은 정의상 digest 에 남지 않는다. 현재화한 것은 기대 SHA 하나뿐.
#   같은 티켓의 수렴 게이트(미수렴 시 baseline 억제·비영 rc)도 이 fixture 의 배달 경계를 바꾸지
#   않는다: RUN1 채택자 트리는 canonical 로 승격된 단일 rev 라 판정이 수렴이고, 그 경로의
#   baseline·rc 는 종전과 같다(미수렴에서만 갈라진다).
#   T-0611 이 강등 사다리·흡수 보고·수렴 결과 반환을 더하며 또 이동했다. 배달 경계는 여전히
#   불변이다: 구세대 형제 강등 3단은 **형제가 구세대일 때만** 갈라지고(이 fixture 의 형제는
#   canonical 사본이라 1단), 조회 축 강등 사유 표면화도 상류 선언 해소 실패에서만 발화하며
#   (여기선 성공), 부분 실행 흡수 보고는 `--paths` 전용이다. 수렴 결과 반환은 프롬프트 게이트만
#   좁히는데 이 RUN1 은 단일 rev(수렴)라 종전대로 프롬프트까지 간다. 역적용 delta 의 anchor 도
#   그대로다 — 현재화한 것은 기대 SHA 하나뿐이다.
#   T-0612 가 그 **전제 자체를 없앴다**: 역델타가 훅 세트 게이트(세대 검사 3지점 + 원자 write
#   판정자 주입)를 통째로 걷어내므로, 이제 이 합성본은 "codex 훅 세트 판정이 늘 빈 결과" 인지와
#   무관하다(T-0585 세대엔 그 개념이 없었다는 사실만 남는다). 부재는 아래 부재 단언이 기계로
#   지킨다 — 위 문단들의 전제 서술은 그 시점 근거의 기록이고, 현재 구속력은 그 단언에 있다.
#   T-0691 이 엔진 텍스트 쓰기에 `newline="\n"` 을 명시하며 또 이동했다. 배달 경계는 불변이다:
#   키워드 추가는 write 호출의 인자만 늘리고 anchor·슬라이스 범위·훅 세트 판정 어디에도 닿지
#   않으며, 채택자 트리에 배달되는 파일 집합도 그대로다 — 현재화한 것은 기대 SHA 하나뿐이다.
#   T-0691 R2가 Path.open 폴백에도 같은 키워드를 보강해 SHA만 다시 현재화했다.
#   T-0709 가 렌더 판정을 개행 정규화 후로, 렌더/manifest 쓰기를 dest 표기 보존으로 바꾸며 또
#   이동했다. 새로 들어온 것은 개행 원시 함수 3개와 쓰기 헬퍼(`_write_rendered_text`)이고,
#   `_render_is_unchanged`·`apply` 의 렌더 분기·`_copy_manifest_preserving_guest` 가 그것을 쓴다.
#   역적용 delta 의 네 anchor 는 그대로 유일 해소되고(새 함수는 anchor 밖), 배달 경계도 불변이다:
#   이 fixture 트리는 개행이 LF 단일이라 정규화 판정 == byte 판정이고 쓰기 표기도 LF 그대로다
#   (CRLF 체크아웃에서만 갈라진다) — 현재화한 것은 기대 SHA 하나뿐이다.
#   T-0723 이 T-0709 가 남긴 마지막 raw 대조를 같은 축으로 맞추며 또 이동했다. 들어온 것은 대조
#   헬퍼(`_rendered_text_matches_dest`) 하나와 그 호출 2곳(`_render_eq_dst`·`plan` 의 최소 렌더
#   분기)뿐이다. 역적용 delta 의 anchor 는 전부 `_main`/`sync_adapter_configs` 쪽이라 그대로
#   유일 해소되고, 배달 경계(planning → apply → self-update 순서)와 배달 파일 집합도 불변이다.
#   LF 단일 트리에서는 판정 결과가 종전과 같다 — 현재화한 것은 기대 SHA 하나뿐이다.
#   T-0729 가 판독을 공유 읽기 seam 으로 올리며 또 이동했다. 들어온 것은 **새 최상위 함수 넷**
#   (`_warn_shared_read_degraded`·`_shared_read_api`·`_read_text_shared`·`_read_bytes_shared`·
#   `_open_shared`)과 등록 사유 한 항목이고, 종전 판독 호출이 그 헬퍼 호출로 바뀌었다. 역적용
#   delta 의 anchor 는 전부 `_main`/`sync_adapter_configs` 쪽이라 그대로 유일 해소되고, 배달
#   경계(planning → apply → self-update 순서)와 배달 파일 집합도 불변이다. 헬퍼는 seam 이 있으면
#   그것을, 없으면 종전 읽기를 쓰므로 이 fixture 트리에서 읽는 바이트가 같다 — 현재화한 것은
#   기대 SHA 하나뿐이다.
#   T-0760 이 guest 절 렌더물을 core 와 같은 재렌더 채널에 합류시키며 또 이동했다. 바뀐 것은 계획
#   구성(`_split_guest_channels` → `_join_guest_channels`)과 위임 토큰 배선 헬퍼 셋이고, 죽은
#   헬퍼 둘(`_dest_guest_manifest_paths`·`_selected_upstream_core_paths`)이 사라졌다. 역적용
#   delta 의 네 anchor 는 전부 `_main`/`sync_adapter_configs` 쪽이라 그대로 유일 해소되고, 배달
#   경계(planning → apply → self-update 순서)와 배달 파일 집합도 불변이다 — 이 fixture 트리엔
#   guest 절이 없어 합류 자체가 무동작이다. 현재화한 것은 기대 SHA 하나뿐이다.
#   T-0760 R1 재작업이 카드 하네스 판정 입력을 상류 절대경로에서 **dest 상대 좌표**로 바꾸며 또
#   이동했다. 들어온 것은 좌표 파생 헬퍼(`_dest_relative_path`) 하나와 렌더 호출 3곳의 인자이고,
#   역적용 delta 의 네 anchor 는 전부 `_main`/`sync_adapter_configs` 쪽이라 그대로 유일 해소된다.
#   배달 경계와 배달 파일 집합도 불변이다 — 현재화한 것은 기대 SHA 하나뿐이다.
_T0585_PM_UPDATE_SHA256 = "c1d8670b2eb0b773b738e2807ae70b43e74319765c43579351eb676cb4f3f578"

_T0585_SYNC_ADAPTER_CONFIGS = '''def sync_adapter_configs(dest_root: Path, source_root: Path, *, write: bool) -> dict:
    """instance-owned 어댑터 config 채널을 1회 돌린다 — 판정 결과 dict(출력은 호출부).

    `write=False`(dry-run)는 판정만 한다(파일·원장 미변경). pm_import 로드/판정 실패는
    `status="unavailable"` 로 fail-soft 한다 — 이 채널이 엔진 sync 자체를 깨뜨리지 않는다."""
    result = {"status": "ok", "updated": [], "preserved": [], "drift": [],
              "backfilled": [], "degraded": []}
    try:
        # **로드도 try 안**이다 — 부분 전파로 pm_import 사본이 없는 트리에서 로더가 던지면
        #   그 예외가 CLI 를 통째로 죽인다(엔진 복구 실행이 바로 그 형상이다).
        pm_import = _load_pm_import()
        if pm_import is None:
            return {**result, "status": "unavailable", "reason": "pm_import 로드 실패"}
        judgments = pm_import.judge_adapter_configs(dest_root, source_root)
    except Exception as exc:  # noqa: BLE001 — 판정 실패는 sync 를 막지 않는다(skew 만 예외).
        if _is_engine_rev_skew(exc):
            raise
        return {**result, "status": "unavailable", "reason": str(exc)}

    for judgment in judgments:
        if judgment.status == "in-sync":
            # 이미 상류와 같다 — 원장만 뒤늦게 채운다(원장 도입 전 채택자가 손댄 적도 없이
            #   영구 보고 모드에 갇히는 걸 막는 유일한 자동 경로).
            if judgment.baseline_sha256 != judgment.dest_sha256:
                result["backfilled"].append(judgment.relpath)
            continue
        summary = pm_import.adapter_config_drift_summary(judgment, dest_root)
        managed = judgment.mode == pm_import.ADAPTER_CONFIG_MANAGED
        if managed and judgment.status == "unedited":
            backup_rel = None
            if write:
                try:
                    # 판정 시점 해시를 넘겨 **판정↔쓰기 사이 동시 편집**을 엔진이 재검증하게 한다
                    #   (raced 면 아무것도 안 덮고 돌아온다). 백업·원자 교체·원장 확인도 그 안이다.
                    accepted = pm_import.accept_adapter_config(
                        dest_root, source_root, judgment.relpath,
                        expected_sha256=judgment.dest_sha256)
                except (OSError, ValueError) as exc:
                    # 한 파일의 실패가 이미 끝난 엔진 sync 를 traceback 으로 덮지 않게 보존 쪽으로
                    #   내린다(다음 실행이 같은 판정으로 재시도). 경로 안전 거부·루트 교체는
                    #   의도적 hard abort 라 그대로 올라간다.
                    result["preserved"].append({
                        "relpath": judgment.relpath, "status": "update-failed",
                        "summary": str(exc)})
                    continue
                if accepted.status != "accepted":
                    # `ledger-failed` 만 성질이 다르다 — 파일은 이미 바뀌었으므로 "보존" 으로
                    #   묶으면 거짓 보고다. 별도 버킷으로 낸다(둘 다 loud).
                    bucket = ("degraded" if accepted.status == "ledger-failed"
                              else "preserved")
                    result[bucket].append({
                        "relpath": judgment.relpath, "status": accepted.status,
                        "summary": accepted.detail})
                    continue
                backup_rel = Path(accepted.backup).relative_to(Path(dest_root)).as_posix()
            result["updated"].append({
                "relpath": judgment.relpath,
                "backup_rel": backup_rel,
                "note": pm_import.ADAPTER_CONFIG_REAPPROVAL_NOTE.get(judgment.relpath),
            })
            continue
        bucket = "preserved" if managed else "drift"
        result[bucket].append({
            "relpath": judgment.relpath, "status": judgment.status, "summary": summary})

    if write and (result["updated"] or result["backfilled"]):
        # 갱신분·in-sync backfill 을 한 번에 기록한다(record 는 template 일치분만 담는다).
        pm_import.record_adapter_baseline(dest_root, source_root)
    return result


'''


def _slice_replace(source: str, start_marker: str, end_marker: str, replacement: str) -> str:
    """동결 세대 복원용 exact structural replace — marker 모호/부재면 loud 실패."""
    assert source.count(start_marker) == 1, start_marker
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[:start] + replacement + source[end:]


def _t0585_pm_update_source() -> str:
    """현행 전체 updater에서 T-0591 delta만 역적용해 실제 T-0585 source bytes를 복원한다.

    전체 5천 줄을 복제하지 않으면서도 결과 SHA를 결함 세대 실파일로 ratchet한다. 따라서 RUN1은
    mock driver가 아니라 source/manifest planning, apply, self-update 순서를 포함한 실제 updater다.
    이 순서 전체가 회귀의 load-bearing 범위라 whole-file SHA가 의도적이다. 향후 updater 변경은
    무관해 보여도 이 실제 배달 경계를 다시 검토한 뒤 reverse delta/기대 SHA를 함께 현재화한다.

    아래 각 `_slice_replace`/`.replace(...)` 호출의 marker 문자열은 현재 `pm_update.py` 소스와
    정확히 일치해야 한다. marker 가 사라지거나 모호해지면(다른 편집이 그 자리를 바꿔서) 그
    호출의 assert(`_slice_replace`의 `assert source.count(start_marker) == 1`)가 아래 whole-file
    SHA assert보다 먼저 AssertionError로 멈춘다 — 이건 SHA drift가 아니라 marker 자체가 낡았다는
    뜻이다. 그때는 SHA를 건드리지 말고, 새 `pm_update.py` 소스에서 그 marker가 가리키던 경계에
    해당하는 새 텍스트를 찾아 marker 문자열만 갱신한 뒤 이 함수를 처음부터 다시 돌린다.

    T-0748 재핀 이력(배달 경계가 그대로였던 근거는 여기 계속 쌓는다):

    T-0777 — codex 훅 범용 진입점의 역방향 축이 `check_adapter_hook_sets` 결과에 advisory 목록을
    더하고 `_print_adapter_hook_set_finding` 이 그 목록을 한 벌 더 출력한다. 두 함수 모두 이
    합성본에서는 `_main` 호출 지점이 위 역델타로 이미 제거돼 있어(아래 anachronism 부재 단언 2건이
    그것을 기계로 확인한다) T-0585 세대 실행 경로에 들지 않는다. 배달 경계
    (source/manifest planning → apply → self-update 순서)와 어댑터 config 채널 본문은 이번 변경에
    포함되지 않았다 — `sync_adapter_configs`·`_adapter_config_gate_failed`·`apply` 는 무편집이다.

    T-0811 — 출하 표면 사설 문맥 청소로 `pm_update.py` 의 주석 2줄(역방향 진입점 축 설명)에서
    티켓 ID 토큰만 걷어냈다. 변경은 주석 문자열 전용이라 배달 경계(source/manifest planning →
    apply → self-update 순서)·`sync_adapter_configs`·`_adapter_config_gate_failed`·`apply` 는
    무편집이고, 위 역델타의 marker 문자열도 그대로 매치했다(marker assert 선통과). 그래서 이
    drift 는 세대 시대착오가 아니라 합성본에 그대로 실려 온 주석 bytes 차이다.

    T-0810 — 사설 문맥 스트립이 엔진 산문에 남긴 빈 괄호 `()` 를 걷어낸 커밋 e0f07fb(머지
    db7e153)가 이 drift 를 만들었다. 직전 재핀(합성본 sha d8694ef5) 이후 `pm_update.py` 를 건드린
    커밋은 이것 하나다. 배달 경계는 불변이다: 변경 13줄이 전부 주석·docstring 텍스트라 COMMENT·
    docstring 을 제외한 토큰 스트림(31,578개)과 docstring 을 제외한 모듈 AST 가 구/신 세대 동일하고,
    어댑터 config 채널·배달 함수(`sync_adapter_configs`·`_adapter_config_gate_failed`·`apply`·
    `_print_adapter_config_finding`)는 byte 동일이다. `plan`·`_main` 만 docstring/주석에서 2·5 바이트
    줄었을 뿐 실행 코드는 그대로다. 채택자 관측면도 그대로다 — docstring 을 출력으로 내보내는
    유일한 지점은 argparse `description=__doc__`(모듈 docstring)인데 그건 무편집이고, 역적용 delta 의
    네 anchor 도 이번에 모두 유일하게 해소됐다(`_slice_replace` 의 count==1 단언 선통과). 구/신
    세대로 합성본을 각각 만들어 대조하면 6,603줄 중 13줄이 같은 빈 괄호 제거뿐이다 — 현재화한
    것은 기대 SHA 하나뿐이다.
    """
    source = (REPO / ".project_manager" / "tools" / "pm_update.py").read_text(
        encoding="utf-8")
    source = _slice_replace(
        source,
        '    "instance_owned_template_delta": (\n',
        '    "sync_adapter_configs.accept": (\n',
        "",
    )
    source = _slice_replace(
        source,
        "# pm_import 자체를 아직 못 불러오는 복구 RUN에서도 채널 적용 여부를 판정할 최소 좌표.\n",
        "def sync_adapter_configs",
        "\n\n",
    )
    source = _slice_replace(
        source, "def sync_adapter_configs", "def _print_adapter_config_finding",
        _T0585_SYNC_ADAPTER_CONFIGS,
    )
    source = _slice_replace(
        source,
        "    # in-sync byte지만 원장 backfill 실패 같은 상태는 preserved/degraded 버킷에 아직 없을 수 있다.\n",
        "    drift = result.get(\"drift\", [])\n",
        "",
    )
    source = source.replace(
        "    do_adapter_config = (\n"
        "        not args.target and not scope_paths\n"
        "        and _has_adapter_config_candidate(effective_dest)\n"
        "    )\n",
        "    do_adapter_config = not args.target and not scope_paths\n",
        1,
    )
    # T-0617 세대 관측은 T-0585 updater에 없었다. 판정 snapshot과 세 종료 표면 호출을 모두
    # 역적용해야 합성본에 undefined helper가 남지 않고 whole-file SHA가 실제 결함 세대를 가리킨다.
    source = _slice_replace(
        source,
        "    # instance-owned 세대 요약은 각 종료 분기에서 config 채널의 **최종 판정 뒤** 계산한다.\n",
        "\n    if not changes:\n",
        "",
    )
    source = source.replace(
        "        else:\n"
        "            _print_instance_owned_template_delta(instance_owned_delta_lines)\n",
        "",
        1,
    )
    source = source.replace(
        "    else:\n"
        "        _print_instance_owned_template_delta(instance_owned_delta_lines)\n",
        "",
        1,
    )
    source = source.replace(
        "            _print_instance_owned_template_delta(instance_owned_delta_lines)\n",
        "",
    )
    source = source.replace(
        "        _print_instance_owned_template_delta(instance_owned_delta_lines)\n",
        "",
    )
    # 훅 세트 게이트(T-0606 이후 세대)는 **역델타에서 통째로 걷어낸다** — T-0585 세대 updater 엔
    #   그 개념이 없었다. 남겨 두면 이 fixture 가 "codex 훅 세트 판정이 늘 빈 결과" 라는 전제에
    #   기대게 되고(그 전제가 깨지는 순간 조용히 다른 것을 검증한다), 합성본이 세대 시대착오를
    #   품는다. 지우면 전제 자체가 필요 없어진다(아래 부재 단언이 그걸 기계로 못박는다).
    source = _slice_replace(
        source,
        "            # 세대 정합은 config 채널과 **같은 경계·같은 자리**에서 본다(둘 다 manifest 밖\n",
        "        print(\"최신 — 변경 없음.\")\n",
        "",
    )
    source = _slice_replace(
        source,
        "            _print_adapter_hook_set_finding(\n",
        "        return 0\n",
        "",
    )
    assert source.count('        print("최신 — 변경 없음.")\n') == 1
    source = source.replace('        print("최신 — 변경 없음.")\n', "", 1)
    source = source.replace(
        "    if not changes:\n",
        '    if not changes:\n        print("최신 — 변경 없음.")\n',
        1,
    )
    source = _slice_replace(
        source,
        "        # apply 로 방금 착지한 **새 훅 세트**를 기준으로 판정한다",
        "\n    # upstream_rev baseline 갱신",
        "",
    )
    source = _slice_replace(
        source,
        "    # 훅 세트 판정자는 **상류 세대**로 미리 해소해 넘긴다",
        "    msg = f\"✓ {len(changes)} 파일 동기화\"",
        "    apply(changes)  # ← 실패 시 예외 전파 → 아래 전환 미도달(채택자 완전한 구형 유지).\n",
    )
    # 세대 시대착오 부재를 **기계로** 못박는다 — 전제(“codex 판정이 늘 빈 결과”)에 기대지 않는다.
    #   대조 대상은 `_main` 의 **호출 지점**이다(`apply` 시그니처의 기본값 파라미터는 호출하지
    #   않으면 발화하지 않으므로 이 합성본의 동작 축이 아니다).
    for anachronism in ("check_adapter_hook_sets(effective_dest",
                        "is_hook_set_path=resolve_hook_set_predicate("):
        assert anachronism not in source, (
            f"T-0585 세대 합성본에 훅 세트 게이트가 남았다: {anachronism}")
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    assert digest == _T0585_PM_UPDATE_SHA256, (
        f"T-0585 updater fixture drift: {digest}\n"
        "재핀 절차(T-0748): 이 assert에 도달했다는 것은 위 anachronism 부재 단언 2건이 이미 "
        "통과했다는 뜻이다(둘 중 하나라도 실패했다면 여기 오지 않는다) — 즉 이 drift는 세대 "
        "시대착오 재유입이 아니라 배달 경계 안의 다른 변경이다. _T0585_PM_UPDATE_SHA256 을 위 "
        "digest 값으로 갱신하고, 왜 배달 경계가 그대로인지 한 문단을 이 함수 docstring 에 "
        "덧붙인다. (marker 자체가 안 맞아 여기 도달하기 전에 `_slice_replace` 가 먼저 터졌다면 "
        "SHA 문제가 아니라 marker 갱신이 필요하다는 뜻이다 — 이 함수 docstring 참고.)")
    return source


def _install_t0585_updater(dest: Path) -> None:
    (dest / ".project_manager" / "tools" / "pm_update.py").write_text(
        _t0585_pm_update_source(), encoding="utf-8", newline="\n")


def test_upgrade_adopter_zero_change_run2_blocks_until_adapter_accept(tmp_path):
    """구 updater RUN1 → 신 zero-change RUN2 red → accept/backfill → check green 전체 경로."""
    dest = tmp_path / "legacy-adapter-adopter"
    assert _PM_IMPORT.main([
        "--new", str(dest), "--harness", "codex", "--name", _ADOPTER_NAME,
        "--fill", "manual",
    ]) == 0
    # 먼저 나머지 엔진을 canonical과 맞춰 RUN2의 엔진 change를 정확히 0으로 만든다.
    _prime_engine_to_canonical(dest)

    hooks = dest / _ADAPTER_HOOKS_REL
    hooks.write_text(_LEGACY_BLOCKING_HOOKS, encoding="utf-8", newline="\n")
    ledger = dest / ".project_manager" / "adapter_baseline.json"
    ledger.unlink(missing_ok=True)  # 구세대 설치: durable 원장 부재.
    _install_t0585_updater(dest)

    run1 = _run_adopter_tool(dest, "pm_update.py", "--from", str(REPO))
    assert run1.returncode == 0, run1.stderr
    assert "파일 동기화" in run1.stdout, "실 updater planning/apply 경로를 타지 않음"
    assert (dest / ".project_manager" / "tools" / "pm_update.py").read_bytes() == \
        (REPO / ".project_manager" / "tools" / "pm_update.py").read_bytes(), \
        "RUN1이 신 updater를 배달하지 못함"
    assert hooks.read_text(encoding="utf-8") == _LEGACY_BLOCKING_HOOKS

    run2 = _run_adopter_tool(dest, "pm_update.py", "--from", str(REPO))
    assert run2.returncode == 1, (
        f"원장 부재인데 zero-change RUN2가 green\n{run2.stdout}\n{run2.stderr}")
    assert "managed 어댑터 config가 미수렴" in run2.stderr
    assert "엔진 manifest 변경은 0건" in run2.stderr, \
        "RUN2가 zero-change 경계를 실제로 거치지 않음"
    assert "최신 — 변경 없음" not in run2.stdout
    assert hooks.read_text(encoding="utf-8") == _LEGACY_BLOCKING_HOOKS, \
        "RUN2가 원장 부재 파일을 자동 덮음(비파괴 계약 위반)"

    before_check = _snapshot_tree(dest)
    red_check = _run_adopter_tool(
        dest, "pm_config.py", "sync-adapter-config", "--check", "--from", str(REPO))
    assert red_check.returncode == 1 and "--accept .codex/hooks.json" in red_check.stderr
    assert _snapshot_tree(dest) == before_check, "--check가 채택자 트리를 변경함"

    accepted = _run_adopter_tool(
        dest, "pm_config.py", "sync-adapter-config", "--accept", _ADAPTER_HOOKS_REL,
        "--from", str(REPO))
    assert accepted.returncode == 0, accepted.stderr
    green_check = _run_adopter_tool(
        dest, "pm_config.py", "sync-adapter-config", "--check", "--from", str(REPO))
    assert green_check.returncode == 0, green_check.stderr
    assert "수렴 확인" in green_check.stdout


# ── S6 CRLF 표기 dest 의 zero-change 수렴 (T-0723) ────────────────────────────
# Windows 채택자(`core.autocrlf=true`)의 dest 는 내용이 상류와 같아도 표기가 CRLF 다. 계획의 최소
# 렌더 분기가 raw bytes 로 대조하던 동안 그 파일들은 매 실행 `update` 로 올라왔다 — 재기록 후에도
# bytes 는 그대로라(쓰기가 dest 표기 보존) 내용이 상하진 않지만, dry-run 이 상시 오탐이고 실제
# drift 가 그 소음에 묻히며 zero-change 경계(RUN2 완료 게이트)를 아예 통과하지 못한다.
# LF 환경(Linux)에서 그 형상을 태우려면 dest 표기를 직접 뒤집어야 한다 — 아래 헬퍼가 그 자리다.

_NOTATION_MANAGED_WIKI = (
    ".project_manager/wiki/pm_role.md",
    ".project_manager/wiki/pm_playbook.md",
    ".project_manager/wiki/README.md",
    ".project_manager/wiki/pm_state.template.md",
)


def _renotate_dest_to_crlf(dest: Path, relpaths) -> list[str]:
    """dest 사본을 **내용 그대로** CRLF 표기로 되쓴다 — 축이 실제로 실렸는지 함께 단언한다.

    선-단언은 "bytes 가 바뀌었나" 가 아니라 **"지금 dest 가 CRLF 인가"** 다. CRLF 체크아웃
    (Windows)에서는 설치 사본이 이미 CRLF 라 변환이 no-op 이고, 그때도 이 게이트가 재려는 축
    (LF 렌더 산출물 ↔ CRLF dest)은 그대로 실려 있다. 'bytes 가 바뀌었나' 로 단언하면 정작 결함이
    나는 그 플랫폼에서만 픽스처가 스스로 red 가 된다."""
    converted: list[str] = []
    for rel in relpaths:
        path = dest / rel
        raw = path.read_bytes()
        crlf = raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        assert crlf.replace(b"\r\n", b"\n") == raw.replace(b"\r\n", b"\n"), \
            f"{rel}: 표기 변환이 내용을 바꿨다"
        path.write_bytes(crlf)
        after = path.read_bytes()
        assert b"\r\n" in after, f"{rel}: CRLF 주입 실패(공허 픽스처)"
        assert after != after.replace(b"\r\n", b"\n"), \
            f"{rel}: dest 가 LF 렌더층과 byte 동일하다 — 이 축이 시험되지 않는다"
        converted.append(rel)
    assert len(converted) == len(tuple(relpaths)) and converted, "변환 대상 0(공허 픽스처)"
    return converted


def test_upgrade_adopter_crlf_dest_reaches_zero_change_and_keeps_notation(tmp_path):
    """표기만 CRLF 인 dest 는 zero-change 로 수렴하고 표기가 보존된다 — 내용 차이는 여전히 update."""
    dest = tmp_path / "crlf-dest-adopter"
    assert _PM_IMPORT.main([
        "--new", str(dest), "--harness", "codex", "--name", _ADOPTER_NAME,
        "--fill", "manual",
    ]) == 0
    _prime_engine_to_canonical(dest)
    # 대조군: 승격 직후는 변경 0 이어야 이 게이트가 **CRLF 변환분만** 재는 측정기가 된다.
    settled = _run_pm_update(dest)
    assert "최신 — 변경 없음." in settled.stdout, (
        f"픽스처 전제 붕괴 — 승격 직후 변경이 남아 있다\n{settled.stdout}")

    converted = _renotate_dest_to_crlf(dest, _NOTATION_MANAGED_WIKI)
    before = {rel: (dest / rel).read_bytes() for rel in converted}

    run = _run_pm_update(dest)

    assert "최신 — 변경 없음." in run.stdout, (
        "표기만 CRLF 인 dest 를 변경으로 계획했다 — 내용 무변경 churn 이라 수렴을 영영 보고하지 "
        f"못한다\n{run.stdout}")
    for rel in converted:
        assert (dest / rel).read_bytes() == before[rel], (
            f"{rel}: 동기가 채택자 체크아웃 표기를 LF 로 되썼다(표기 보존 위반)")
    # 멱등 — 다음 실행에서 되살아나지 않는다.
    again = _run_pm_update(dest)
    assert "최신 — 변경 없음." in again.stdout, again.stdout

    # 판정이 무뎌지지 않았음을 같은 자리에서 본다: CRLF dest 라도 내용이 다르면 update 다.
    victim = converted[0]
    (dest / victim).write_bytes(
        before[victim] + "<!-- 채택자 한 줄 -->\r\n".encode("utf-8"))
    dirty = _run_pm_update(dest, "--dry-run")
    assert victim in dirty.stdout and "최신 — 변경 없음." not in dirty.stdout, (
        f"CRLF dest 의 내용 차이를 놓쳤다(정규화가 판정을 무디게 만듦)\n{dirty.stdout}")


# ── S7 구 컨테이너 board 의 1회 변환 (T-0754) ────────────────────────────────
# 엔진을 흡수해도 **board 데이터**는 구세대 그대로다 — 역할 산출을 명세 본문 안에 marker 로
# 감싸 두던 형상은 `board.py rounds migrate` 한 번으로만 풀린다. 채택자 축에서 그 경로가
# 실제로 닫히는지(동기 → 안내 → 변환 → lint clean → 멱등)를 실 CLI 로 잰다.

# pm:data-literal:begin
# 아래 리터럴은 **채택자 디스크에 기록된 구세대 데이터**다(구 엔진이 실제로 쓴 줄) — 엔진 상수를
# 참조하면 문법이 통째로 바뀌어도 이 게이트가 green 이라 여기엔 문자열을 직접 박는다.
_LEGACY_SECTION_START = "<!-- pm-ticket-section:start role=developer -->"
_LEGACY_SECTION_END = "<!-- pm-ticket-section:end role=developer -->"
_LEGACY_SEAL = (
    "<!-- pm-ticket-seal role=developer ordinal=0 sha256="
    + "0" * 64 + " by=harvest -->"
)
# pm:data-literal:end

_LEGACY_ROUND_BODY = "## 구현 보충 (developer · 2026-08-17)\n\n채택자 board 의 옛 산출.\n"
_LEGACY_TICKET_ID = "T-0001"


def _install_legacy_board(dest: Path) -> tuple[Path, Path]:
    """채택자 board 에 구 컨테이너 형상(역할 절 + 성장 장부)을 깐다 → (티켓 경로, 장부 경로)."""
    tickets = dest / ".project_manager" / "wiki" / "tickets"
    assert tickets.is_dir(), f"채택자 board 스캐폴드 부재: {tickets}"
    ticket = tickets / "done" / f"{_LEGACY_TICKET_ID}-legacy.md"
    ticket.write_text(
        "---\n"
        f"id: {_LEGACY_TICKET_ID}\n"
        "title: 옛 컨테이너 티켓\n"
        "status: done\n"
        "created: '2026-08-17'\n"
        "created_by: adopter\n"
        "claimed_by: null\n"
        "claimed_at: null\n"
        "completed_at: '2026-08-17'\n"
        "depends_on: []\n"
        "blocks: []\n"
        "touches: []\n"
        "estimate: small\n"
        "design: n/a\n"
        "tags: []\n"
        "---\n"
        f"# {_LEGACY_TICKET_ID} — 옛 컨테이너 티켓\n\n## 메모\n채택자 산문.\n\n"
        f"{_LEGACY_SECTION_START}\n{_LEGACY_ROUND_BODY}{_LEGACY_SECTION_END}\n"
        f"{_LEGACY_SEAL}\n\n## PM 판정\n판정은 명세에 남는다.\n",
        encoding="utf-8", newline="\n")
    growth = tickets / ".growth"
    growth.mkdir(exist_ok=True)
    (growth / ".migrated").write_text("1\n", encoding="utf-8", newline="\n")
    (growth / f"{_LEGACY_TICKET_ID}.jsonl").write_text(
        '{"ticket": "' + _LEGACY_TICKET_ID + '", "role": "developer", "ordinal": 0}\n',
        encoding="utf-8", newline="\n")
    return ticket, growth


def test_upgrade_adopter_legacy_board_converges_after_update_and_migrate(tmp_path):
    """구 컨테이너 board 채택자: 동기가 변환을 안내하고, 명령 1회로 lint 가 clean 해진다."""
    dest = tmp_path / "legacy-container-adopter"
    assert _PM_IMPORT.main([
        "--new", str(dest), "--harness", "codex", "--name", _ADOPTER_NAME,
        "--fill", "manual",
    ]) == 0
    _prime_engine_to_canonical(dest)
    ticket, growth = _install_legacy_board(dest)

    synced = _run_pm_update(dest)
    assert "board.py rounds migrate" in synced.stderr, (
        f"흡수가 구 컨테이너 잔존을 알리지 않았다\n{synced.stdout}\n{synced.stderr}")
    assert ticket.read_text(encoding="utf-8").count(_LEGACY_SECTION_START) == 1, (
        "동기가 board 데이터를 자동 변환했다(안내만 해야 한다)")

    red = _run_adopter_tool(dest, "board.py", "lint")
    assert "legacy-growth-section" in (red.stdout + red.stderr), (
        f"변환 전 board 가 lint red 가 아니다\n{red.stdout}\n{red.stderr}")

    migrated = _run_adopter_tool(dest, "board.py", "rounds", "migrate")
    assert migrated.returncode == 0, f"{migrated.stdout}\n{migrated.stderr}"

    round_file = (dest / ".project_manager" / "wiki" / "tickets" / "rounds"
                  / _LEGACY_TICKET_ID / "01-developer.md")
    assert round_file.read_text(encoding="utf-8") == _LEGACY_ROUND_BODY
    spec = ticket.read_text(encoding="utf-8")
    assert "pm-ticket-section" not in spec and "pm-ticket-seal" not in spec
    assert "## PM 판정" in spec, "절 밖 PM 텍스트가 사라졌다"
    assert not growth.exists()

    green = _run_adopter_tool(dest, "board.py", "lint")
    assert "legacy-growth-section" not in (green.stdout + green.stderr), (
        f"변환 뒤에도 구 컨테이너 판정이 남았다\n{green.stdout}\n{green.stderr}")
    settled = _run_pm_update(dest)
    assert "board.py rounds migrate" not in settled.stderr, "변환 뒤에도 안내가 남았다"

    again = _run_adopter_tool(dest, "board.py", "rounds", "migrate")
    assert again.returncode == 0 and "변경 없음" in again.stdout, (
        f"재실행이 멱등이 아니다\n{again.stdout}\n{again.stderr}")


def test_update_release_skill_cards_pin_zero_change_and_adapter_gate_contract():
    """canonical + claude/codex/opencode 출하 카드가 changes=0 skip 문구를 재도입하지 않는다."""
    update_cards = [
        REPO / ".claude/skills/pm-update/SKILL.md",
        REPO / "templates/claude_code/.claude/skills/pm-update/SKILL.md",
        REPO / "templates/codex/.agents/skills/pm-update/SKILL.md",
        REPO / "templates/opencode/.claude/skills/pm-update/SKILL.md",
    ]
    release_cards = [
        REPO / ".claude/skills/pm-release/SKILL.md",
        REPO / "templates/claude_code/.claude/skills/pm-release/SKILL.md",
        REPO / "templates/codex/.agents/skills/pm-release/SKILL.md",
        REPO / "templates/opencode/.claude/skills/pm-release/SKILL.md",
    ]
    for card in update_cards:
        text = _card_with_operational_details(card)
        assert "`--changes`는 미리보기" in text, card
        assert "zero-change RUN2" in text, card
        assert "sync-adapter-config --check" in text, card
        assert "변경 0(최신)**: 동기 생략" not in text, card
    for card in release_cards:
        text = card.read_text(encoding="utf-8")
        assert text.count("sync-adapter-config --check") >= 2, card
        assert "livegate/main push/tag/GitHub Release" in text, card


# ── S7 진입점이 열린 채택자에 가드 기능이 **코드만으로** 도달하나 (T-0777) ────────
# 이 티켓의 주장은 "이후 기능 추가는 엔진 코드 변경뿐이고 채택자 config 는 다시 안 건드린다" 다.
# 그 주장은 업그레이드 축에서만 확인된다 — fresh 축은 진입점이 처음부터 있는지만 본다.

_CODEX_DISPATCHER_REL = ".codex/pm_orch_codex.py"


def _build_codex_framework(tmp_path: Path) -> Path:
    """REPO 로부터 **변경 가능한** codex 프레임워크 소스를 만든다(import + self-update 소스).

    codex 채택자 manifest 가 참조하는 root-상대 경로 전부를 담는다 — 엔진(`.project_manager/`)·
    root `.claude/skills`(`.agents/skills` @source 원본)·`templates/codex/`(어댑터 @source 원본)·
    `.gitattributes`. REPO 를 안 건드리려고 사본에 엔진 mutate 를 가한다(fresh 게이트의 opencode
    헬퍼와 같은 모델)."""
    framework = tmp_path / "framework"
    ignore = shutil.ignore_patterns("__pycache__", ".git", "node_modules")
    shutil.copytree(REPO / ".project_manager", framework / ".project_manager", ignore=ignore)
    shutil.copytree(REPO / "templates" / "codex",
                    framework / "templates" / "codex", ignore=ignore)
    shutil.copytree(REPO / ".claude" / "skills", framework / ".claude" / "skills",
                    ignore=ignore)
    shutil.copy2(REPO / ".gitattributes", framework / ".gitattributes")
    return framework


def _run_shipped_entrypoint(dest: Path, event: str, payload: dict) -> dict:
    """채택자에 설치된 hooks.json 의 그 진입점 커맨드를 **그대로** 실행한다."""
    hooks = json.loads((dest / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    handler = hooks["hooks"][event][0]["hooks"][0]
    completed = subprocess.run(
        ["bash", "-c", handler["command"]], cwd=dest, input=json.dumps(payload),
        text=True, capture_output=True, timeout=handler["timeout"] + 5, check=True)
    return json.loads(completed.stdout)


@pytest.mark.skipif(not posix_bash_supported(),
                    reason="POSIX bash wrapper 실행 환경이 아님")
def test_upgrade_adopter_gains_a_codex_hook_feature_by_engine_code_alone(tmp_path):
    """진입점이 열린 채택자 + 상류 기능 추가 → pm_update 1회로 발화(채택자 config 무변경)."""
    framework = _build_codex_framework(tmp_path)
    dest = tmp_path / "codex-adopter"
    assert _PM_IMPORT.main([
        "--new", str(dest), "--harness", "codex", "--name", _ADOPTER_NAME,
        "--fill", "manual", "--from", str(framework),
    ]) == 0

    hooks_rel = ".codex/hooks.json"
    hooks_before = (dest / hooks_rel).read_bytes()
    assert hooks_before == (framework / "templates" / "codex" / hooks_rel).read_bytes(), \
        "설치 직후 채택자 config 가 상류와 다르다(전제 붕괴)"
    # 만들어진 트리의 **파일 내용**으로 진입점을 단언한다(조립 문자열이 아니라 산출물).
    installed = json.loads((dest / hooks_rel).read_text(encoding="utf-8"))["hooks"]
    for event in ("PreToolUse", "UserPromptSubmit", "PostToolUse"):
        groups = installed[event]
        assert len(groups) == 1 and groups[0]["matcher"] == ".*", event
        for key in ("command", "commandWindows"):
            assert _CODEX_DISPATCHER_REL in groups[0]["hooks"][0][key], (event, key)
    assert (dest / _CODEX_DISPATCHER_REL).is_file(), "디스패처가 설치되지 않았다"
    assert _PM_IMPORT.judge_adapter_hook_entrypoints(dest, framework, ["codex"]) == [], \
        "갓 만든 채택자에 진입점 소견이 뜬다(거짓 red)"
    # 진입점이 열려 있으나 아직 이 이벤트에 등록된 기능은 없다.
    assert _run_shipped_entrypoint(
        dest, "PostToolUse", {"hook_event_name": "PostToolUse", "tool_name": "shell"}) == {}

    # 상류에 가드 기능 하나를 **코드로만** 더한다(hooks.json 무접촉).
    upstream_dispatcher = framework / "templates" / "codex" / _CODEX_DISPATCHER_REL
    upstream_dispatcher.write_text(dispatcher_source_with_fixture_feature(),
                                   encoding="utf-8", newline="\n")

    run = _run_adopter_tool(dest, "pm_update.py", "--from", str(framework))
    assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"
    assert "훅 진입점 누락" not in run.stderr, \
        f"진입점이 다 있는 채택자에 advisory 소음이 났다\n{run.stderr}"

    assert (dest / hooks_rel).read_bytes() == hooks_before, \
        "기능 추가가 채택자 config 를 건드렸다 — 이 티켓이 닫은 마찰이 그대로다"
    assert (dest / _CODEX_DISPATCHER_REL).read_bytes() == upstream_dispatcher.read_bytes(), \
        "디스패처가 전파되지 않았다(코드 채널 파손)"
    assert _run_shipped_entrypoint(
        dest, "PostToolUse",
        {"hook_event_name": "PostToolUse", "tool_name": "shell"}
    ) == FIXTURE_FEATURE_ENVELOPE, "전파는 됐는데 진입점에서 발화하지 않는다"
