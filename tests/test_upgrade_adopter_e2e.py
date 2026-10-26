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

from pm_engine.app import import_fill
from pm_engine.domain import install_rules

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from _manager_home import _known_ctx_installation
from _git_fixture import init_git_repo

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
from _fake_opencode import OLLAMA_LISTING, install_fake_opencode, path_with
from _engine_tree import engine_python_files

import ast

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def t0927_upstream(tmp_path_factory):
    from _manager_home import make_committed_source
    return make_committed_source(tmp_path_factory.mktemp("t0927-current"))


def _t0927_direct_update(dest, *extra):
    from _manager_home import known_ctx_env
    return subprocess.run(
        [sys.executable, str(dest / ".project_manager/tools/pm_config.py"), "update", *extra],
        cwd=dest, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300, env=known_ctx_env({**os.environ, "PM_NONINTERACTIVE": "1"},
                                       native_bin=dest.parent / "known-ctx-native"))


def _t0956_self_row(dest: Path) -> tuple[str, str]:
    """채택자 areas.md 폴더 이름 행의 (git, base) — 정확히 1행이어야 한다."""
    lines = [line.strip() for line in
             (dest / ".project_manager/areas.md").read_text(encoding="utf-8").splitlines()
             if line.strip().startswith("|") and not set(line.strip()) <= set("|- ")]
    header = [cell.strip() for cell in lines[0].strip("|").split("|")]
    rows = [dict(zip(header, (cell.strip() for cell in line.strip("|").split("|"))))
            for line in lines[1:]]
    own = [row for row in rows if row.get("repo") == dest.name]
    assert len(own) == 1, own
    return own[0].get("git", ""), own[0].get("base", "")


# ── OpenCode 위임 카드 `variant` — 갱신의 추론 값 정리와 옛 갱신기 첫 실행 (T-0946) ─────────────
# 모델 목록 조회는 PATH 앞의 가짜 opencode(`_fake_opencode`, 실측 출력 형식)가 답한다 — 설치도 같은
# PATH 로 돌려 실제 opencode 를 부르지 않는다.
_T0946_RESEARCHER_CARD = Path(".opencode/agents/researcher.md")


def _t0946_profile(model: str) -> str:
    return ("delegate.researcher.harness=opencode\n"
            f"delegate.researcher.model={model}\n"
            "delegate.researcher.reasoning=high\n")


def _t0946_append_conf(dest: Path, text: str) -> Path:
    conf = dest / ".project_manager/local.conf"
    conf.write_text(conf.read_text(encoding="utf-8") + text, encoding="utf-8", newline="\n")
    return conf


@pytest.fixture
def t0946_fake_opencode(tmp_path, monkeypatch):
    """PATH 앞에 `OLLAMA_LISTING` 을 내는 가짜 opencode 를 둔다 — 이 테스트의 자식 프로세스 전부가 쓴다."""
    if not posix_bash_supported():
        pytest.skip("가짜 opencode 는 POSIX sh 스크립트다")
    fakebin = install_fake_opencode(tmp_path / "fakebin", OLLAMA_LISTING)
    monkeypatch.setenv("PATH", path_with(fakebin))
    return fakebin


def _t0946_install(source: Path, dest: Path, upstream: Path) -> Path:
    """`source` 의 pm_import 로 opencode 채택자를 설치한다. 설치는 모델 한도를 조회하므로
    고정 한도(`known_ctx_env`)로 돌린다."""
    from _manager_home import _run, known_ctx_env
    argv = [sys.executable, str(source / ".project_manager/tools/pm_import.py"),
            "--new", str(dest), "--harness", "opencode", "--fill", "manual",
            "--from", str(source), "--upstream", str(upstream)]
    _run(argv, cwd=source, env=known_ctx_env(native_bin=dest.parent / "known-ctx-native"))
    return dest


@pytest.fixture
def t0946_current_opencode_adopter(tmp_path, t0927_upstream, t0946_fake_opencode):
    """현재 소스로 설치한 opencode 채택자 — 새 갱신기가 이미 설치된 형상."""
    return _t0946_install(t0927_upstream, tmp_path / "current-opencode", t0927_upstream)


def test_t0946_update_prunes_reasoning_before_rendering(t0946_current_opencode_adopter):
    """새 갱신기는 그 모델이 받지 않는 추론 값을 렌더 **전에** 지우고 알린다 — 렌더가 멈추지 않는다."""
    dest = t0946_current_opencode_adopter
    conf = _t0946_append_conf(dest, _t0946_profile("ollama/glm-5.2:cloud"))

    run = _t0927_direct_update(dest)

    assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"
    assert "delegate.researcher.reasoning=high" in run.stdout, run.stdout
    assert "delegate.researcher.reasoning=" not in conf.read_text(encoding="utf-8")
    lines = (dest / _T0946_RESEARCHER_CARD).read_text(encoding="utf-8").splitlines()
    assert lines[3] == 'model: "ollama/glm-5.2:cloud"', "\n".join(lines[:6])
    assert lines[4].startswith("# variant:"), "\n".join(lines[:6])


# 다음 세대 렌더러만 아는 토큰 — 설치된 렌더러는 이 토큰을 미해소로 보고 렌더를 거부한다.
_NEXT_GEN_TOKEN = "{{NEXT_GEN_MARK}}"


def _teach_renderer_next_gen_token(pm_render_path: Path) -> None:
    """`render_adapter` 본문 첫 문장으로 다음 세대 토큰 치환을 넣는다(docstring 바로 뒤)."""
    import ast
    text = pm_render_path.read_text(encoding="utf-8")
    function = next(node for node in ast.walk(ast.parse(text))
                    if isinstance(node, ast.FunctionDef) and node.name == "render_adapter")
    lines = text.splitlines(keepends=True)
    indent = " " * function.body[1].col_offset
    lines.insert(function.body[0].end_lineno,
                 f'{indent}template_text = template_text.replace("{_NEXT_GEN_TOKEN}", "next-gen")\n')
    pm_render_path.write_text("".join(lines), encoding="utf-8")


def test_update_renders_cards_whose_tokens_only_the_new_renderer_knows(tmp_path):
    """렌더러가 바뀌는 갱신이 한 번의 `pm-config update` 로 끝난다 (T-0945 AT-005 · 불변식 I7).

    전달 프로세스는 엔진 코드만 옮기고 렌더하지 않는다. `@render` 파일의 계획과 렌더는 받은 엔진으로
    도는 완료 프로세스의 새 렌더러가 하므로, 설치 렌더러가 모르는 다음 세대 토큰도 첫 실행에서
    그려진다. 두 번째 실행은 변경 0 확인이다 — 첫 실행이 완료 단계를 끝내지 못했다면 여기서 변경이
    난다 (T-0951 AT-002)."""
    from _manager_home import _git, make_committed_source, make_pm_home
    source = make_committed_source(tmp_path / "source")
    home = make_pm_home(tmp_path / "home", upstream=source, source=source)
    card_rel = Path(".claude") / "agents" / "architect.md"
    _teach_renderer_next_gen_token(source / ".project_manager" / "tools" / "pm_engine" / "app" / "adapter_render.py")
    template = source / card_rel
    template.write_text(template.read_text(encoding="utf-8")
                        + f"\n다음 세대 표식: {_NEXT_GEN_TOKEN}\n", encoding="utf-8")
    _git("-C", str(source), "commit", "-qam", "next-gen renderer token")
    assert "next-gen" not in (home / card_rel).read_text(encoding="utf-8")

    run = _t0927_direct_update(home)

    assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"
    rendered = (home / card_rel).read_text(encoding="utf-8")
    assert "다음 세대 표식: next-gen" in rendered and _NEXT_GEN_TOKEN not in rendered

    before = _snapshot_tree(home)
    again = _t0927_direct_update(home)
    assert again.returncode == 0, f"{again.stdout}\n{again.stderr}"
    assert "변경 없음" in again.stdout, again.stdout
    assert _snapshot_tree(home) == before




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
# 이 소스의 산문이 아니다. 엔진 상수(`manifest_rules._GUEST_MANIFEST_BEGIN` 등)를 참조하면 리터럴이 통째로
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

    모델 조회 대역은 autouse fixture가 테스트마다 설정하고 복원한다."""
    module = _load_pm_import()
    return module


_PM_IMPORT = _engine_pm_import()


# ── 채택자 트리 조작 헬퍼 ──────────────────────────────────────────────────────


def _run_pm_update(dest: Path, *args: str) -> subprocess.CompletedProcess:
    """채택자 트리의 pm_update.py 를 실제 CLI 로 구동한다(cwd=dest·비대화형·capture).

    upstream 은 명시 `--from REPO` 다 — fresh 인스턴스의 local.conf `upstream.path=` 은 URL 이라
    엔진이 자동 진행하지 않는다(로컬 checkout 명시가 채택자의 실제 절차)."""
    from _manager_home import known_ctx_env
    proc = subprocess.run(
        [sys.executable, str(dest / ".project_manager" / "tools" / "pm_update.py"),
         "--from", str(REPO), *args],
        cwd=str(dest),
        capture_output=True,
        text=True,
        # 엔진 출력은 UTF-8(한글 포함) — 부모 콘솔 로케일로 디코드하면 캡처가 깨진다.
        encoding="utf-8",
        errors="replace",
        env=known_ctx_env({**os.environ, "PM_NONINTERACTIVE": "1"},
                          native_bin=dest.parent / "known-ctx-native"),
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
    (template_dir,) = install_rules.HARNESS_TEMPLATE_DIRS[host]
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
    from _manager_home import known_ctx_env
    return subprocess.run(
        [sys.executable, str(dest / ".project_manager" / "tools" / tool), *args],
        cwd=str(dest), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=known_ctx_env({**os.environ, "PM_NONINTERACTIVE": "1"},
                          native_bin=dest.parent / "known-ctx-native"), timeout=300,
    )


# T-0590 재현행: 추가 리뷰어 온보딩(기본 프로필 상수 + maybe_prompt_additional_reviewer)이
#   updater 본문에 들어오면서 whole-file SHA 가 이동했다. 역적용 delta(T-0591 어댑터 수취 채널)는
#   그대로 적용되므로 실 배달 경계는 불변이고, RUN1 fixture 는 새 온보딩을 포함한 실 updater 다
#   (이 경로는 PM_NONINTERACTIVE=1 로 돌아 질문·conf write 가 발화하지 않는다).
#   T-0590 R2 에서 한 번 더 이동 — 온보딩의 "이미 결정됨" 판정면을 conf raw 텍스트 substring 에서
#   `_read_local_conf` 파싱 키 존재로 바꿨다(주석 한 줄이 결정을 가로채던 결함). 재검토 결과 이동
#   범위는 maybe_prompt_additional_reviewer 본문뿐이고, 이 fixture 가 ratchet 하는 배달 경계
#   (source/manifest planning → apply → self-update 순서)와 역적용 delta 구간에는 겹침이 없다.
#   T-0590 R3 에서 또 한 번 이동 — `_main` 의 변경 0 수렴 지점이 추가 리뷰어 opt-in 도 호출하게
#   됐다(has-changes 경로에서만 부르던 결함). 이동분은 `print("최신 — 변경 없음.")` 뒤의
#   수렴 블록뿐이라 역적용 delta 구간(어댑터 config 게이트·sync_adapter_configs 본문)과 겹치지
#   않고, 이 테스트의 RUN1 은 has-changes·RUN2 는 red rc1(프롬프트 이전 중단)이며 두 실행 모두
#   PM_NONINTERACTIVE=1 이라 질문·conf write 는 발화하지 않는다.
#   T-0590 R3 후속에서 또 이동 — 온보딩이 기존 대상(레거시 `reviewer_cmd`·구조화 튜플)을 덮지
#   않도록 대상 판정(classify_additional_reviewer_target)과 활성 플래그 전용 블록이 들어왔고,
#   EOF 응답이 false 를 박제하지 않게 바뀌었다. 이동 범위는 온보딩 상수/헬퍼와
#   maybe_prompt_additional_reviewer 본문뿐이라 역적용 delta 구간·배달 경계와 겹치지 않는다.
#   T-0590 R4 에서 또 이동 — 온보딩 응답의 기록 시점 판정이 질문 **전** 판정에서 커밋 시점
#   재읽기·재판정(배타락 + 단일 O_APPEND)으로 바뀌었다. 들어온 것은 온보딩 상수/헬퍼
#   (`_load_file_lock`·`_local_conf_lock_path`·`_local_conf_write_lock`·
#   `_append_local_conf_atomic`·`_commit_additional_reviewer_optin`)와
#   maybe_prompt_additional_reviewer 본문, 그리고 `contextlib` import 한 줄뿐이다. 역적용 delta 의
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
#   T-0597 에서 또 이동 — opt-in 게이트 키가 `additional_reviewer.enabled` 로 개칭되고 개칭 전
#   구키가 1릴리즈 fallback 이 됐다. 들어온 것은 온보딩 상수
#   (`ADDITIONAL_REVIEWER_ENABLED_KEY`·`LEGACY_EXTERNAL_REVIEW_ENABLED_KEY`·
#   `LEGACY_ENABLED_KEY_DEPRECATION`)와 판정 헬퍼(`additional_reviewer_decision_key`),
#   그 헬퍼를 쓰는 `_commit_additional_reviewer_optin`·`maybe_prompt_additional_reviewer` 의 결정 분기,
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
#   T-0767 이 local.conf 키 표기를 점 표기로 통일하며 또 이동했다. pm_update 쪽 변경은
#   운영값 키 표 갱신(`project.name`·`upstream.path` 등)·`upstream.rev`/`upstream.seen_rev`
#   상수와 그 산문·공용 로더(`local_conf.py`) 경로 로드뿐이라 문자열·읽기 키만 바뀌었다.
#   역적용 delta 의 anchor 중 baseline 절 marker 하나가 그 산문(`upstream_rev` →
#   `upstream.rev`)이라 marker 문자열을 함께 갱신했고, 나머지 anchor 는 그대로 유일
#   해소된다. 배달 경계(planning → apply → self-update 순서)와 배달 파일 집합은 불변이다.
#   같은 절의 리뷰 재작업에서 한 번 더 이동했다 — 교체 안내 호출이 수렴 뒤 두 자리에서
#   dest/source 해소 입구 한 자리로 옮겨갔다(안내가 성공 반환·dry-run·해소 실패보다 앞서게).
#   이동한 것은 그 호출 위치뿐이고 역적용 delta 의 네 anchor 는 전부 다른 구간이라 그대로 유일
#   해소된다. 배달 경계와 배달 파일 집합은 여전히 불변이다 — 현재화한 것은 기대 SHA 하나뿐이다.
#   통합 브랜치 위로 rebase 하며 한 번 더 이동했다 — 흡수한 커밋들이 pm_update 의 배달 대상
#   목록이 아니라 그 밖의 코드·산문을 바꿨고, 이 절의 역적용 delta anchor 네 자리는 전부
#   그 구간 밖이라 그대로 유일 해소된다. 배달 경계(planning → apply → self-update 순서)와
#   배달 파일 집합은 여전히 불변이고, 현재화한 것은 기대 SHA 하나뿐이다.
#   T-0887 에서 또 이동했다 — 추가 리뷰어 온보딩 opt-in 일습(상수·판정·커밋·질문 함수와 두
#   호출부)이 통째로 사라졌다. 역적용 delta 의 네 anchor 는 전부 그 절 밖이라 그대로 유일
#   해소되고, 배달 경계(planning → apply → self-update 순서)·배달 파일 집합도 불변이다: 이
#   fixture 의 두 실행은 `PM_NONINTERACTIVE=1` 이라 삭제된 질문이 애초에 발화하지 않았고, 삭제로
#   줄어든 것은 그 실행이 부르지 않던 코드뿐이다. 현재화한 것은 기대 SHA 하나뿐이다.
#   같은 티켓의 수정 라운드에서 한 번 더 이동했다 — `plan()` docstring 의 낱말 하나("모듈 밖
#   호출부")뿐이고 실행 코드·배달 경계는 무편집이다.
#   T-0956 수정 라운드(리뷰 F-005)에서 한 번 더 이동했다 — 주석 한 줄의 자리 표기
#   (`harness.<이름>` → `harness.<harness>`)뿐이고 실행 코드·배달 경계·anchor 는 무편집이다.
#   T-0964 에서 또 이동했다 — 설정 레지스트리가 패키지(`pm_engine.domain.conf_schema`·
#   `pm_engine.infra.conf_store`)로 옮겨 가며 `_load_local_conf` 경로 적재가 사라지고 호출이 모듈
#   속성 호출로 바뀌었다. 역적용 delta 의 네 anchor 는 전부 그 구간 밖이라 그대로 유일 해소되고,
#   배달 경계(planning → apply → self-update 순서)와 배달 파일 집합도 불변이다 — 현재화한 것은
#   기대 SHA 하나뿐이다.
#   같은 묶음의 수정 라운드(T-0963 리뷰 F-002)에서 한 번 더 이동했다 — conf 쓰기 락 래퍼가 쓰는 곳 없는
#   `yield` 를 버리고 공용 seam 을 그대로 돌려주게 됐고 `contextlib` import 가 빠졌다. 잠금 구간·배달
#   경계·anchor 는 무편집이다.
#   T-0967 에서 또 이동했다 — 하네스 계약이 패키지(`pm_engine.infra.harness.contract`)로 옮겨 가며
#   `_load_relay` 경로 적재가 사라지고 호출이 모듈 속성 호출로 바뀌었다. 역적용 delta 의 네 anchor 는
#   전부 그 구간 밖이라 그대로 유일 해소되고, 배달 경계와 배달 파일 집합도 불변이다 — 현재화한 것은
#   기대 SHA 하나뿐이다.
#   T-0968 에서 또 이동했다 — manifest 문법·판독이 패키지(`pm_engine.domain.manifest_rules`·
#   `pm_engine.infra.manifest_store`)로, 상류 분류가 `pm_engine.domain.upstream_rules` 로 옮겨 가며
#   pm_update 에서 그 정의가 빠지고 호출이 모듈 속성 호출로 바뀌었다. 역적용 delta 의 네 anchor 는
#   전부 그 구간 밖이라 그대로 유일 해소되고, 배달 경계와 배달 파일 집합도 불변이다 — 현재화한 것은
#   기대 SHA 하나뿐이다.
#   T-0971 에서 또 이동했다 — 등록 repo 조회가 패키지(`pm_engine.infra.areas_store`)로 옮겨 가며
#   `reinstall_protected_hooks` 의 dest board 호출 한 줄이 모듈 속성 호출로 바뀌고 머리 import 한 줄이
#   늘었다. 역적용 delta 의 네 anchor 는 전부 그 구간 밖이라 그대로 유일 해소되고, 배달 경계와 배달
#   파일 집합도 불변이다 — 현재화한 것은 기대 SHA 하나뿐이다.
_T0585_PM_UPDATE_SHA256 = "85e1a9263e13eadf66ef2f6f434c83fa4dd3e5d3828842995aae6925d5916efd"
# 재핀(2026-09-16): 핸드오프 슬롯 모드 삭제로 pm_update 본문이 바뀌었다 — 옛 대여 행(`<repo>_<N>`
# session)을 idle 로 옮기는 흡수 말미 1회 마이그레이션이 더해졌을 뿐, 이 테스트가 재는 배달 경계
# (어댑터 수령·리타이어 delta)는 그대로다. 두 시대착오 부재 단언이 먼저 통과했으므로 세대 재유입이
# 아니다.

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
        managed = judgment.mode == install_rules.ADAPTER_CONFIG_MANAGED
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
                "note": install_rules.ADAPTER_CONFIG_REAPPROVAL_NOTE.get(judgment.relpath),
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


# T-0923 의 하네스 사용 한도 반영 단계(`apply_harness_ctx_limits`)는 두 동결 세대 updater 에 없던
# 개념이다 — 세 종료 경로의 호출을 역적용해 합성본이 그 세대의 동작만 갖게 한다(함수 정의는
# 호출되지 않으므로 남아도 동작 축이 아니다 · 다른 역델타와 같은 규칙).
_HARNESS_CTX_APPLY_REVERSALS = (
    ("            # 하네스 사용 한도·압축 비율 반영 — 채널이 템플릿 갱신·원장 기록을 끝낸 파일 위에 쓴다.\n"
     "            if not apply_harness_ctx_limits(effective_dest, write=not args.dry_run):\n"
     "                return 1\n", ""),
    ("            if not apply_harness_ctx_limits(effective_dest, write=False):\n"
     "                return 1\n", ""),
    ("        if not apply_harness_ctx_limits(effective_dest, write=True):\n"
     "            return 1\n", ""),
)


def _reverse_harness_ctx_apply(source: str) -> str:
    """T-0923 반영 단계 역적용 — marker 는 각각 정확히 한 번 해소돼야 한다."""
    for current, legacy in _HARNESS_CTX_APPLY_REVERSALS:
        assert source.count(current) == 1, current
        source = source.replace(current, legacy, 1)
    assert "not apply_harness_ctx_limits(" not in source      # 호출 지점 0(정의는 동작 축이 아니다)
    return source


# T-0951 의 전달/완료 두 프로세스 분리는 두 동결 세대 updater 에 없던 개념이다 — 전달 분기를
# 역적용하면 인계 값이 없는 실행이 종전처럼 한 프로세스 안에서 해소·계획·적용을 끝낸다(복사한
# 엔진 모듈을 같은 프로세스가 다시 불러오던 그 세대의 동작이다). `_transfer_engine_code` 정의는
# 호출되지 않으므로 남아도 동작 축이 아니다(다른 역델타와 같은 규칙).
# 같은 티켓의 `--paths` 거부(엔진 코드가 상류와 다르면 부분 전파 거부)도 그 세대엔 없었다 — 구
# updater 의 `--paths` 단독 자기 배달이 바로 그 세대의 동작이다.
_TWO_PROCESS_UPDATE_REVERSALS = (
    "    if handoff is None and not args.target:\n"
    "        return _transfer_engine_code(args, raw_argv, scope_paths)\n",
    "    # 내보내기의 `--paths` 도 같은 규칙이다 — 엔진 코드는 세대 단위로만 옮긴다.\n"
    "    if scope_paths:\n"
    "        rc = _refuse_scope_with_pending_engine_code(changes)\n"
    "        if rc:\n"
    "            return rc\n",
)


# 같은 티켓이 계획 렌더 실패 흡수(`_render_eq_dst` 의 try/except → '다름')를 지웠다. 두 동결 세대
# updater 는 그 흡수를 가졌고, 그 세대가 다음 세대 템플릿을 그리지 못하는 실행(구 rewrite 대상이
# 상류에서 사라진 경우)은 흡수 덕에 계획까지 갔다 — 역적용해 그 세대 동작을 되살린다.
_RENDER_EQ_DST_RAISE = (
    "    rendered = _render_text(\n"
    "        sp, conf, entry_notation_template, flat_command_skill,\n"
    "        codex_operational_skill, _dest_relative_path(dst, dest_root),\n"
    "    )\n"
    "    return _rendered_text_matches_dest(rendered, dst)\n"
)
_RENDER_EQ_DST_ABSORB = (
    "    try:\n"
    "        rendered = _render_text(\n"
    "            sp, conf, entry_notation_template, flat_command_skill,\n"
    "            codex_operational_skill, _dest_relative_path(dst, dest_root),\n"
    "        )\n"
    "    except Exception:  # noqa: BLE001 — 렌더 실패는 '다름'으로 보수 처리.\n"
    "        return False\n"
    "    return _rendered_text_matches_dest(rendered, dst)\n"
)


def _reverse_render_eq_dst_raise(source: str) -> str:
    """T-0951 렌더 실패 흡수 삭제 역적용 — marker 는 정확히 한 번 해소돼야 한다."""
    assert source.count(_RENDER_EQ_DST_RAISE) == 1, _RENDER_EQ_DST_RAISE
    return source.replace(_RENDER_EQ_DST_RAISE, _RENDER_EQ_DST_ABSORB, 1)


def _reverse_two_process_update(source: str) -> str:
    """T-0951 전달 분기·`--paths` 거부 역적용 — marker 는 각각 정확히 한 번 해소돼야 한다."""
    for current in _TWO_PROCESS_UPDATE_REVERSALS:
        assert source.count(current) == 1, current
        source = source.replace(current, "", 1)
    assert "return _transfer_engine_code(" not in source   # 호출 지점 0
    return source


def _slice_replace(source: str, start_marker: str, end_marker: str, replacement: str) -> str:
    """동결 세대 복원용 exact structural replace — marker 모호/부재면 loud 실패."""
    assert source.count(start_marker) == 1, start_marker
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[:start] + replacement + source[end:]


# 보호 패키지 이동 역적용. 배달 경계의 역사적 source bytes와 SHA를 그대로 유지한다.
_PROTECTION_PACKAGE_REVERSALS = (
    (
        'from pm_engine.app import repo_protection\n'
        'from pm_engine.infra import branch_store, protected_hooks\n',
        '',
    ),
    (
        '# 완료 프로세스에서 실행 트리의 보호 패키지를 import하고 대상 Home의 데이터를 읽는다.\n'
        '# --target export는 이 완료 경로를 실행하지 않는다.\n',
        '# 배선은 **기존 계약을 그대로 탄다**(신규 seam 0): dest 의 `pm_config._install_protected_hook_\n'
        '# reporting` → `_resolve_repo_protected`(areas 권위) →\n'
        '# `worktree_pool.install_protected_hook`(훅·sidecar·hooksPath). pm_update 는 목록 해소도 훅\n'
        '# 본문도 재구현하지 않는다.\n'
        '#\n'
        '# **dest 의 엔진**을 로드한다(source 아님) — sync 로 방금 갱신된 사본이 새 훅 본문을 들고 있고,\n'
        '# 등록 repo 레지스트리(areas.md)·훅 디렉토리도 dest(PM 홈) 소유다. `--target`(루트→templates\n'
        '# 엔진 export)은 비발화 — templates/<name> 은 PM 홈이 아니라 출하 스캐폴드라 등록 repo 가 없다\n'
        '# (selfheal/skew/진입 doc 마이그레이션과 같은 경계).\n',
    ),
    (
        'def _load_dest_pm_config(dest_root: Path):\n'
        '    """자기 repo 행 채움에 필요한 대상 pm_config를 적재한다.\n'
        '\n'
        '    자기 행 채움의 평면 board 소비가 남아 있어 이 로더를 유지한다.\n'
        '    보호 훅 재설치는 완료 프로세스의 패키지와 대상 Home을 사용한다.\n'
        '    부재는 None, 적재 예외는 호출부로 전파한다.\n'
        '    """\n'
        '    pm_config_py = Path(dest_root) / ".project_manager" / "tools" / "pm_config.py"\n'
        '    if not pm_config_py.exists():\n'
        '        return None\n'
        '    return _load_module_from_path(\n'
        '        pm_config_py, "pm_config.py", allow_unverified=True,\n'
        '    )',
        'def _load_dest_pm_config(dest_root: Path):\n'
        '    """dest(방금 동기된) `.project_manager/tools/pm_config.py` 를 로드 (_load_pm_import 동형).\n'
        '\n'
        '    실행 중인 pm_update 프로세스는 **sync 이전** 코드를 메모리에 들고 있으므로, 재설치는 반드시\n'
        '    디스크의 *새* 사본을 로드해서 돌려야 신 훅 본문이 배포된다. 부재(구형 dest·엔진 미배치)면\n'
        '    None — 호출부가 fail-soft 로 보고한다. 로드 예외는 전파(호출부가 잡아 loud 보고)."""\n'
        '    pm_config_py = Path(dest_root) / ".project_manager" / "tools" / "pm_config.py"\n'
        '    if not pm_config_py.exists():\n'
        '        return None\n'
        '    return _load_module_from_path(\n'
        '        pm_config_py, "pm_config.py", allow_unverified=True,\n'
        '    )',
    ),
    (
        'def _protected_hook_in_sync(home, repo: str) -> bool:\n'
        '    try:\n'
        '        expected = branch_store._resolve_protected(home, repo)\n'
        '        mode, test_cmd = repo_protection._protected_push_gate_config(home, repo, report_downgrade=False)\n'
        '        for artifact in protected_hooks.protected_hook_artifacts(\n'
        '                home, repo, expected, gate_mode=mode, test_cmd=test_cmd):\n'
        '            if _read_hook_artifact(artifact.path) != artifact.content:\n'
        '                return False\n'
        '            if artifact.executable and not _hook_artifact_executable(artifact.path):\n'
        '                return False\n'
        '        return protected_hooks.protected_hook_wired(home, repo) is True\n'
        '    except Exception:\n'
        '        return False',
        'def _protected_hook_in_sync(repo: str, *, pm_config, worktree_pool, board) -> bool:\n'
        '    """이 repo 의 설치된 보호 훅이 **현재 엔진과 정합**인가 — drift 판정.\n'
        '\n'
        '    정합이면 재설치를 건너뛴다(매 sync 반복 출력 회피). "비교 우선·정합이면 조용" 은\n'
        '    sidecar reconcile 과 같은 패턴이다.\n'
        '\n'
        '    **축을 열거하지 않고 유도한다** — 봐야 할 것은 정의상 "`install_protected_hook` 이 쓰는 것"\n'
        '    이므로, 그 함수와 **같은 명세**(`worktree_pool.protected_hook_artifacts`)를 읽어 산출물마다\n'
        '    내용 실행권한(필요한 것만)을 대조하고, 파일이 아닌 bare `core.hooksPath` **배선**은\n'
        '    `pm_config.protected_hook_wired()`로 본다. 판정이 자체 목록을 들면 설치가\n'
        '    자랄 때 조용히 갈라진다 — 실제로 그 클래스가 연달아 났다(읽기 실패 축·실행 비트 축).\n'
        '\n'
        '    **모르면 재설치 쪽으로 기운다**(fail-safe): 파일 부재·**읽기 실패**(깨진 본문·권한·IO)·\n'
        '    실행권한 상실·배선 판정 불가(`None`)는 전부 drift 로 수렴한다 —\n'
        '    재설치는 멱등이라 비용이 낮고, 반대 방향(조용히 stale 유지)은 보호가 꺼진 채 침묵하는\n'
        '    실패모드다.\n'
        '\n'
        '    **일반 판정 예외는 밖으로 내지 않는 게 계약**이다(fail-safe False). 단 엔진 사본\n'
        '    불일치 marker는 계속 실행할 수 없는 상태라 재전파한다. 호출부의\n'
        '    `unavailable` 은 "dest 엔진/모듈을 못 불렀다"(=판정 자체가 불가능)를 위한 상태지 "파일이\n'
        '    깨졌다"가 아니다 — 후자를 unavailable 로 흘리면 재설치가 안 돌아 복구 경로가 사라진다."""\n'
        '    try:\n'
        '        # 설치가 받는 것과 **같은 입력**(areas 실효 보호목록)으로 기대 산출물을 유도한다.\n'
        '        expected_list = list(pm_config._resolve_repo_protected(repo))\n'
        '        # read-only drift 판정은 steady-state 무출력 계약을 지킨다. 강등 경고는 실제 설치\n'
        '        # 깔때기에서 1회 loud하게 나가며, 여기서는 같은 resolver 결과만 소비한다.\n'
        '        gate_mode, test_cmd = pm_config._protected_push_gate_config(\n'
        '            repo, board=board, report_downgrade=False)\n'
        '        for artifact in worktree_pool.protected_hook_artifacts(\n'
        '                repo, expected_list, gate_mode=gate_mode, test_cmd=test_cmd):\n'
        '            if _read_hook_artifact(artifact.path) != artifact.content:\n'
        '                return False\n'
        '            if artifact.executable and not _hook_artifact_executable(artifact.path):\n'
        '                return False\n'
        '        # 배선 축 — `False`(hooksPath 가 우리 디렉토리를 안 가리킴)면 훅이 아예 발화하지 않는다.\n'
        '        # `None`(bare 부재·git 실패)은 판정 불가 → 재설치 시도(install 이 결과를 loud 보고).\n'
        '        return pm_config.protected_hook_wired(repo, worktree_pool=worktree_pool) is True\n'
        '    except Exception as exc:  # noqa: BLE001 — 일반 판정 실패만 drift로 수렴(단 skew 재전파).\n'
        '        if _is_engine_rev_skew(exc):\n'
        '            raise\n'
        '        return False',
    ),
    (
        'def reinstall_protected_hooks(dest_root: Path, *, write: bool) -> dict:\n'
        '    result = {"status": "unavailable", "targets": [], "in_sync": [],\n'
        '              "drifted": [], "failed": [], "no_bare": [], "reason": None}\n'
        '    home = pm_paths.home_at(Path(dest_root).resolve())\n'
        '    try:\n'
        '        repos = sorted(areas_store.registered_repos(home))\n'
        '        if not repos:\n'
        '            result["status"] = "no_repos"\n'
        '            return result\n'
        '        for repo in repos:\n'
        '            if not pm_paths.bare_repo_path(home, repo).exists():\n'
        '                result["no_bare"].append(repo)\n'
        '                continue\n'
        '            result["targets"].append(repo)\n'
        '            if _protected_hook_in_sync(home, repo):\n'
        '                result["in_sync"].append(repo)\n'
        '                continue\n'
        '            result["drifted"].append(repo)\n'
        '            if write and not repo_protection._install_protected_hook_reporting(\n'
        '                    home, repo, action="(재)설치"):\n'
        '                result["failed"].append(repo)\n'
        '        result["status"] = "done"\n'
        '        return result\n'
        '    except Exception as exc:\n'
        '        result["reason"] = f"{type(exc).__name__}: {exc}"\n'
        '        return result',
        'def reinstall_protected_hooks(dest_root: Path, *, write: bool) -> dict:\n'
        '    """등록 repo 전수 보호 훅 정합 확인 + drift 재설치 — 엔진 업그레이드 배포 트리거.\n'
        '\n'
        '    **매 sync 실행마다** 돈다(changes 유무 무관 — 위 모듈 주석의 RUN1/RUN2 실측 참조).\n'
        '    `write=False`(dry-run)면 판정만 하고 아무것도 쓰지 않는다(migrate_entry_doc 의 write 플래그\n'
        '    동형). 반환 dict:\n'
        '      - `status` — "done" / "no_repos"(등록 repo 0) / "unavailable"(엔진/레지스트리 미해소)\n'
        '      - `targets` — 판정 대상 repo(= bare 미러 보유) · `in_sync` — 정합이라 건너뛴 repo(조용)\n'
        '      - `drifted` — 재설치가 필요한 repo(⊆ targets) · `failed` — 그중 설치 실패\n'
        '      - `no_bare` — 등록됐지만 `.repos/<repo>.git` 이 없어 게이트할 대상이 없는 repo\n'
        '      - `reason` — unavailable 사유(사람이 읽는 1줄)\n'
        '\n'
        '    **bare 부재는 실패가 아니다** — 게이트할 미러가 없으면 훅도 무의미하다(install 이 no-op\n'
        '    False). 매 sync 마다 경고를 울리는 대신 `no_bare` 로 분리해 요약 1줄로만 surface 한다\n'
        '    (침묵 아님·`_print_protected_hook_reinstall_finding`).\n'
        '\n'
        '    **fail-soft** — sync 는 이미 성공했다. 일반 엔진 로드 실패(구형 dest)나 레지스트리\n'
        '    파싱 실패가 update rc 를 바꾸면 안 된다 → 예외를 "unavailable"+사유로 강등하고 호출부가\n'
        '    경고로 낸다(훅은 추가 가드·`_install_protected_hook` 의 fail-soft 계약과 동형). stamped\n'
        '    sibling의 marked rev skew 는 그대로 올린다 — 완료 단계는 같은 세대 사본만 적재하므로 그 skew 는\n'
        '    동기 대상 트리 자체의 결함이다.\n'
        '\n'
        '    ⚠ **"unavailable" 은 판정 자체가 불가능한 경우만**이다 — dest 엔진/레지스트리를 못 불러\n'
        '    *어느 repo 도* 손댈 수 없는 상태. **개별 repo 의 훅 파일이 깨진 것은 unavailable 이 아니라\n'
        '    drift** 다(`_protected_hook_in_sync` 가 읽기 실패를 False 로 수렴). 그 구분이 무너지면\n'
        '    "깨진 훅을 발견했는데 재설치는 안 하는" 경로가 생겨 복구 채널이 사라진다."""\n'
        '    result: dict = {"status": "unavailable", "targets": [], "in_sync": [],\n'
        '                    "drifted": [], "failed": [], "no_bare": [], "reason": None}\n'
        '    try:\n'
        '        pm_config = _load_dest_pm_config(dest_root)\n'
        '        if pm_config is None:\n'
        '            result["reason"] = (\n'
        '                f"{dest_root}/.project_manager/tools/pm_config.py 부재 — 로드 불가")\n'
        '            return result\n'
        '        board = pm_config._load_module("board", "board.py")\n'
        '        worktree_pool = pm_config._load_module("worktree_pool", "worktree_pool.py")\n'
        '        if board is None or worktree_pool is None:\n'
        '            missing = "board.py" if board is None else "worktree_pool.py"\n'
        '            result["reason"] = f"dest 엔진 {missing} 부재/로드 실패"\n'
        '            return result\n'
        '        repos = sorted(areas_store.registered_repos(pm_paths.home_at(Path(dest_root).resolve())))\n'
        '        if not repos:\n'
        '            result["status"] = "no_repos"\n'
        '            return result\n'
        '        for repo in repos:\n'
        '            # bare 미러가 있어야 `core.hooksPath` 를 걸 대상이 있다(install 과 같은 가드).\n'
        '            if not pm_paths.bare_repo_path(pm_paths.home_at(Path(dest_root)), repo).exists():\n'
        '                result["no_bare"].append(repo)\n'
        '                continue\n'
        '            result["targets"].append(repo)\n'
        '            if _protected_hook_in_sync(repo, pm_config=pm_config,\n'
        '                                       worktree_pool=worktree_pool, board=board):\n'
        '                result["in_sync"].append(repo)\n'
        '                continue\n'
        '            result["drifted"].append(repo)\n'
        '            if not write:\n'
        '                continue\n'
        '            ok = pm_config._install_protected_hook_reporting(\n'
        '                repo, board=board, worktree_pool=worktree_pool, action="(재)설치")\n'
        '            if not ok:\n'
        '                result["failed"].append(repo)\n'
        '        result["status"] = "done"\n'
        '        return result\n'
        '    except Exception as exc:  # noqa: BLE001 — 재설치 실패가 성공한 sync 를 무효화하면 안 됨(표식 skew 는 올린다).\n'
        '        if _is_engine_rev_skew(exc):\n'
        '            raise\n'
        '        result["status"] = "unavailable"\n'
        '        result["reason"] = f"{type(exc).__name__}: {exc}"\n'
        '        return result',
    ),
    (
        'per-repo 성공/실패 줄은 repo_protection 깔때기 소관',
        'per-repo 성공/실패 줄은 pm_config 깔때기 소관',
    ),
)


def _reverse_board_provider_move(source: str) -> str:
    """현행 provider 이동을 역적용해 동결 updater의 기존 bytes와 SHA를 보존한다."""
    replacements = [('\nfrom pm_engine.app import areas_registry\n', ''), ('from typing import NamedTuple\n', 'from types import SimpleNamespace\nfrom typing import NamedTuple\n'), ('def fill_self_repo_row(effective_dest: Path, *, write: bool) -> None:\n    """대상 Home의 빈 자기 행을 채우고 기존 write/dry-run 결과를 출력한다."""\n    filled = areas_registry.fill_self_repo_row(pm_paths.home_at(Path(effective_dest)), write=write)\n    if filled is None:\n        return\n    if write:\n        print(f"  ✓ areas.md 자기 행 git·base 기록: {filled}")\n    else:\n        print(f"[dry-run] areas.md 자기 행 git·base 기록 예정: {filled}")', 'def fill_self_repo_row(effective_dest: Path, *, write: bool) -> None:\n    """흡수 말미 1회 — areas.md 자기 행(폴더 이름 행)의 `git`·`base` 가 둘 다 비었으면 채운다.\n\n    설치가 자기 행에 두 칸을 기록하기 전의 채택자 데이터 이관이다. 규칙(대상 행·값·덮어쓰지\n    않음)은 dest 의 board `fill_self_repo_row` 가 소유한다 — 이 함수는 방금 동기된 dest 엔진을\n    로드해 부르고 결과 한 줄만 낸다. dest 에 pm_config.py 가 없으면(엔진 미배치 dest) 무동작\n    (`reinstall_protected_hooks` 와 같은 판정). `write=False`(dry-run)는 판정만 한다.\n    """\n    pm_config = _load_dest_pm_config(effective_dest)\n    if pm_config is None:\n        return\n    board = pm_config._load_module("board", "board.py")\n    if board is None:\n        raise RuntimeError(\n            f"{effective_dest}/.project_manager/tools/board.py 를 로드하지 못했다 — areas.md 자기 행 "\n            "이관 불가. 엔진 사본을 다시 동기한 뒤 pm-update 를 재실행하라.")\n    filled = board.fill_self_repo_row(write=write)\n    if filled is None:\n        return\n    if write:\n        print(f"  ✓ areas.md 자기 행 git·base 기록: {filled}")\n    else:\n        print(f"[dry-run] areas.md 자기 행 git·base 기록 예정: {filled}")'), ('def _read_hook_artifact(', 'def _load_dest_pm_config(dest_root: Path):\n    """자기 repo 행 채움에 필요한 대상 pm_config를 적재한다.\n\n    자기 행 채움의 평면 board 소비가 남아 있어 이 로더를 유지한다.\n    보호 훅 재설치는 완료 프로세스의 패키지와 대상 Home을 사용한다.\n    부재는 None, 적재 예외는 호출부로 전파한다.\n    """\n    pm_config_py = Path(dest_root) / ".project_manager" / "tools" / "pm_config.py"\n    if not pm_config_py.exists():\n        return None\n    return _load_module_from_path(\n        pm_config_py, "pm_config.py", allow_unverified=True,\n    )\n\n\ndef _read_hook_artifact(')]
    for current, previous in replacements:
        assert source.count(current) == 1, current
        source = source.replace(current, previous, 1)
    return source


def _reverse_pool_package_move(source: str) -> str:
    """패키지 이동 전 updater의 경로·잠금·호출 형태를 복원한다."""
    source = _reverse_board_provider_move(source)
    for current, previous in _PROTECTION_PACKAGE_REVERSALS:
        assert source.count(current) == 1, current
        source = source.replace(current, previous, 1)
    return (source.replace("from pm_engine.infra import pool_store\n", "")
            .replace("pool_store._lease_lock", "worktree_pool._lease_lock")
            .replace("with worktree_pool._lease_lock(pm_paths.home_at(Path(effective_dest))):",
                     'with fs.exclusive_file_lock(ledger.parent / "worktree-leases.lock"):')
            .replace("pm_paths.bare_repo_path(pm_paths.home_at(Path(dest_root)), repo).exists()",
                     "worktree_pool.bare_repo_path(repo).exists()")
            .replace("pm_config._resolve_repo_protected(repo)",
                     "pm_config._resolve_repo_protected(repo, board=board)"))


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

    T-0918 — 어댑터 설정 판독이 jsonc 후행 쉼표를 처리하도록 `pm_update` 에 문법 헬퍼
    `_strip_jsonc_trailing_commas` 를 **새로 더했다**(호출부는 `pm_import._load_adapter_config_document`
    하나). `_main` 의 배달 경계(source/manifest planning → apply → self-update 순서)와
    `sync_adapter_configs`·`_adapter_config_gate_failed`·`apply` 는 무편집이고, 이 fixture 의 역델타
    marker 4곳도 그대로 유일 해소됐다(marker assert 선통과) — 합성본에 새 함수 bytes 가 실려 온
    차이만 SHA 에 반영했다.

    T-0908 — URL upstream 을 `_resolve_dest_source` 가 로컬 캐시 checkout 으로 해소하게 바꾸고
    `upstream.seen_rev` 의 형상 분기를 지웠다. 바뀐 곳은 **소스 해소 입구**와 sync 종료 직전의
    rev 기록이며, 그 사이의 배달 경계(source/manifest planning → apply → self-update 순서)는 한 줄도
    건드리지 않았다. 이 fixture 의 upstream 은 로컬 경로라 캐시 분기 자체가 실행되지 않는다.
    marker 4곳 모두 그대로 유일 해소됐고 anachronism 부재 단언 2건도 통과한 상태에서 SHA 만 갱신했다.

    T-0848 — `drift_changes` 가 엔진 사본 rev skew 를 `None`(게이트 비적용) 으로 접던 분기를 없애고
    예외를 그대로 전파한다(livegate drift-0 게이트가 자기 양성 형상에서 false-green 이 되던 결함).
    바뀐 곳은 livegate 전용 질의 헬퍼이며 `_main` 의 source/manifest planning → apply →
    self-update 배달 순서는 한 줄도 건드리지 않는다. marker 4곳 모두 그대로 유일 해소됐고
    anachronism 부재 단언 2건도 통과한 상태에서 SHA 만 갱신했다.

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

    T-0919 — 홈 자신을 작업 폴더로 등재하던 경로를 지우면서, 흡수 말미의 그 등재 호출
    (홈 자신 등재)이 옛 행 제거 마이그레이션(`drop_self_lease_rows`)으로 **자리 그대로**
    바뀌었다(호출 3곳 = `_main` 의 `do_migrate` 세 분기 · 전부 기존 `migrate_entry_doc(...)` 바로
    다음). 새 함수는 dest 의 리스 장부에서 `slot="."` 행만 지우고, 지울 행이 없으면 읽기만 한다
    (멱등). 역적용 delta 의 네 anchor 는 전부 `sync_adapter_configs`/훅 세트 게이트 쪽이라 이
    치환과 겹치지 않고 그대로 유일 해소된다(marker assert 선통과). 배달 경계(source/manifest
    planning → apply → self-update 순서)도 불변이다 — 호출 자리 수·조건·순서가 그대로다.
    현재화한 것은 기대 SHA 하나뿐이다.

    T-0919 수정 라운드 — 같은 `drop_self_lease_rows` 의 읽기·판정·쓰기를 dest 의
    `worktree-leases.lock` 배타락 안으로 넣고 쓰기를 tmp → 원자 교체로 바꿨다(엔진
    `worktree_pool._lease_lock`·`_write_ledger_raw` 와 같은 직렬화 구간). 함수 하나의 내부만
    바뀌었고 호출 자리 수·조건·순서는 그대로다. 역적용 delta 의 marker 는 전부
    `sync_adapter_configs`/퇴역 경로 쪽이라 이 편집과 겹치지 않고 그대로 유일 해소된다
    (marker assert 선통과). 현재화한 것은 기대 SHA 하나뿐이다.

    T-0861 — 릴리즈 게이트 drift 판정(`board.py livegate record`)이 `pm_update.py` 의 self-update
    계획 계산을 재사용하도록, `_main()` 의 manifest 자기치유 → notation 템플릿 해소 → guest
    backfill 기록 판정 → `plan()` 호출 블록을 `_resolve_engine_sync_plan()` 단일 함수로 추출하고
    기존 `drift_changes()` 를 그 위의 얇은 래퍼로 재작성했다. notation 실패를 `plan()` 자신의
    다른 `RuntimeError`(예 `EmptyShippingInventoryError`)와 구분하는
    `_EngineSyncNotationContextError` 도 새로 추가했다. 위 역델타의 네 marker(`instance_owned_
    template_delta` 초입·`sync_adapter_configs` 블록·`do_adapter_config` 단순화·훅 세트 게이트
    제거)는 모두 그대로 유일 해소됐고(marker assert 선통과) anachronism 부재 단언 2건도 그대로
    통과했다 — 새 함수·클래스 둘 다 그 marker 들이 가리키는 어댑터 config/훅 세트 영역과 겹치지
    않는 `_main()` 바깥의 별도 위치에 있다(T-0585 세대 `_main()` 은 이 새 코드를 호출·참조하지
    않으므로 합성본에는 무호출(inert) 정의로만 실린다). 배달 경계(source/manifest planning →
    apply → self-update 순서)·`sync_adapter_configs`·`_adapter_config_gate_failed`·`apply` 는
    무편집이다 — 함수 추출은 그 안의 계산 순서(manifest 자기치유 → notation → guest-backfill
    기록 판정 → `plan()`)를 바이트 동일하게 보존하며 호출부만 함수 호출로 치환했다. 현재화한
    것은 기대 SHA 하나뿐이다.

    T-0876 — manifest가 선언한 퇴역 파일을 full self-sync 두 번째 실행에서 백업 이주하는 단계가
    apply 직후 추가됐다. 이 fixture의 fresh codex 대상에는 구 물리 파일이 없어 새 단계는
    idempotent no-op이고, 역델타가 검증하는 adapter config 판정·source/manifest planning·apply·
    self-update 순서는 그대로다. marker 4곳과 anachronism 부재 단언 2건이 선통과한 상태에서 새
    migration 정의/호출 bytes만 합성본 SHA에 반영했다.

    T-0878 — strict retired-path 선언의 검증된 역상만 legacy flavor exact-match에 허용하고 같은
    선언의 OLD를 self-heal 제거분에서 제외했다. 이 fixture는 현행 codex manifest와 신 reviewer만
    가진 fresh 대상이라 legacy 역상·removed 면제가 모두 비발화하고 기존 in-sync 계획으로 간다.
    adapter config 판정·apply·self-update 순서는 무편집이며 marker 4곳과 anachronism 부재 단언도
    선통과했으므로, 새 검증 helper/선택 판정 bytes만 합성본 SHA에 반영했다. 후속 수정의 no-local
    strict 검증과 OLD+NEW retired heal 조건도 이 fixture의 현행 manifest 형상에서는 비발화한다.

    T-0887 — 추가 리뷰어의 별도 동의 축을 없애면서 `pm_update.py` 에서 opt-in 온보딩 일체
    (`maybe_prompt_additional_reviewer`·`_commit_additional_reviewer_optin`·기본 프로필 상수·대상
    판정 helper·`_append_local_conf_atomic`·`_is_noninteractive`)와 `_main()` 의 호출 2곳을 지웠다.
    지운 코드는 **동기가 끝난 뒤** 도는 사후 질문 경로이고, 배달 경계(source/manifest planning →
    apply → self-update 순서)·`sync_adapter_configs`·`_adapter_config_gate_failed`·`apply` 는
    무편집이다 — `_main()` 에서 바뀐 것은 `converge_upstream_revs(...)` 의 반환을 조건으로 질문을
    던지던 두 자리가 그 호출 한 줄로 줄어든 것뿐이라 baseline 수렴의 순서·조건은 그대로다. 역델타
    marker 4곳이 모두 그대로 유일 해소됐고(marker assert 선통과) anachronism 부재 단언 2건도
    통과한 상태에서 SHA 만 갱신했다.

    T-0887 수정 라운드 — 같은 티켓의 용어 정정이 `plan()` docstring 한 줄의 낱말만 바꿨다
    ("모듈 밖 호출부"). 실행 코드·배달 경계(source/manifest planning → apply → self-update 순서)·
    `sync_adapter_configs`·`_adapter_config_gate_failed`·`apply` 는 무편집이고, 역델타 marker 4곳과
    anachronism 부재 단언 2건도 그대로 통과했다 — 현재화한 것은 기대 SHA 하나뿐이다.
    v1.8.0 — ctx 임계 두 키를 하는 일에 맞춰 개칭하고(`ctx.nudge_pct`→`ctx.checkpoint_pct` ·
    `ctx.stop_pct`→`ctx.precompact_pct`) 그것을 **갱신이 옮기는** 이관 단계
    (`migrate_renamed_conf_keys` · 흡수 사유 등재 1건)를 `pm_update.py` 에 더했다. 이 fixture 가
    역적용하는 T-0591 delta 와 겹치지 않아 exact-marker 는 모두 그대로 유일 해소됐고(marker
    assert 선통과), RUN1 이 검증하는 배달 경계(source/manifest planning → apply → self-update
    순서)는 무편집이다 — 합성본 bytes 차이만 SHA 에 반영했다.
    재핀(2026-09-18) — 삼킴형 예외 감사로 pm_update 본문의 pm_render 로드 폴백 세 곳을 지웠다
    (`_entry_doc_operational_keys`·`_local_conf_operational_map`·`_delegate_harness_from_local_conf`).
    세 소비처 모두 바로 앞줄에서 pm_render 를 무방비로 로드하므로 도달 불가 분기였고, 이 fixture 가
    역적용하는 delta 와도 겹치지 않는다 — 배달 경계와 배달 파일 집합은 무편집이다.
    재핀(2026-09-18, 2차) — 손잡이 감사로 `_path_newline` 의 `default` 인자를 지우고(어느 호출부도
    넘긴 적이 없다) 본문·docstring 이 `_DEFAULT_TEXT_NEWLINE` 상수를 직접 가리키게 했다. 이
    fixture 가 역적용하는 delta 와 겹치지 않고 배달 경계도 무편집이라, 합성본 bytes 차이만 SHA 에
    반영했다.
    재핀(2026-09-18, 3차) — 같은 감사로 `resolve_hook_set_generation` 의 `required` 인자를 지웠다
    (호출부 1곳이 넘긴 적이 없고, 값을 넘기던 안쪽 `_resolve_hook_set_generation_and_sibling` 의
    기본값이 바깥과 같은 False 라 동작이 보존된다). 역적용 marker·배달 경계 모두 무편집이다.
    재핀(T-0923) — 하네스 사용 한도 반영 단계가 들어왔다. 새로 들어온 것은 jsonc 경로 편집
    헬퍼(`_jsonc_set_value` 와 그 부품)·반영 채널(`apply_harness_ctx_limits` 와 그 rev 혼합 흡수 사유
    등재)·세 종료 경로의 그 호출과
    `migrate_renamed_conf_keys` 출력 문구다. 반영 호출 세 곳은 T-0585 세대에 없던 개념이라
    `_reverse_harness_ctx_apply` 로 역적용하고 호출 지점 부재를 anachronism 목록으로 단언한다. 나머지는
    역델타 anchor 밖이라 그대로 유일 해소되고, 배달 경계와 배달 파일 집합은 불변이다 — 현재화한 것은
    기대 SHA 하나뿐이다.
    T-0927의 이관 완료 계약을 반영했다. 이름 이동 helper의 계획을 락 안으로
    옮기고 잔존 키·형제 skew를 완료 실패로 처리했다. 역델타 marker와 시대착오 부재 단언은
    선통과했으며 _main·plan·apply·sync_adapter_configs·_adapter_config_gate_failed의 AST는
    기준 HEAD와 동일하다. 이 합성 시나리오에는 이동할 옛 키가 없어 새 실패 분기가 발화하지
    않는다. 실제 v1.7.13 프로세스 검증은 별도 고정 커밋과 blob을 사용한다.
    T-0945 — 설정 저장 반영(`apply_conf`·키 묶음 표·설치 세대 원본 확인)을 더하고, 렌더 값이 conf 값을
    받도록(`_render_text`·`_render_eq_dst`·`plan`·`_resolve_engine_sync_plan` 의 `conf`·렌더러 인자)
    바꿨으며, `apply` 가 render change 를 render 아닌 change 를 다 쓴 뒤 전부 그리고 나서 쓰게 했다.
    배달 파일 집합과 배달 경계(source/manifest planning → apply → self-update 순서)는 그대로다 —
    바뀐 것은 한 apply 안에서 render 파일 쓰기가 맨 뒤로 모인 순서뿐이고, 이 fixture 의 두 실행이
    단언하는 것(어댑터 수령·리타이어 delta)은 apply 안의 쓰기 순서에 기대지 않는다. 같은 티켓에서
    `_read_local_conf` 가 파일이 없으면 형제 로더를 읽지 않고 {} 를 내게 했다(갱신 입구가 계획용 conf 를
    한 번 읽게 되어, conf 없는 복구 트리가 그 읽기에서 멈추지 않게). 역델타 marker 와 시대착오 부재
    단언은 그대로 선통과했다 — 현재화한 것은 기대 SHA 하나뿐이다.
    T-0946 — 갱신의 추론 값 정리(`prune_unavailable_reasoning`·`ReasoningPruneError`·`_load_relay`)를
    더하고 `_main` 이 계획 직전에 conf 를 한 번 읽어 그 정리를 거친 값을 계획에 넘기게 했다. 정리는
    모델별 허용값 하네스(opencode) 프로필에 추론 값이 있을 때만 판정하고, 이 fixture 의 conf 에는 그런
    줄이 없어 아무것도 지우지 않는다. 배달 파일 집합과 배달 경계(source/manifest planning → apply →
    self-update 순서)는 그대로이고, 역델타 marker 와 시대착오 부재 단언은 선통과했다 — 현재화한 것은
    기대 SHA 하나뿐이다. 같은 티켓의 수정 라운드에서 저장(`apply_conf`)에 추론 값 묶음을 더하고 저장의
    계획 판정과 그리기가 렌더러 하나를 쓰게(`plan`·`_resolve_engine_sync_plan` 의 선택 인자 `render_mod`)
    했으며, 정리와 저장 확인이 추론 값 열거 하나(`_reasoning_values`)를 쓰게 했다. 갱신 실행은
    `render_mod` 를 넘기지 않아 계획 렌더러 로드가 그대로이고 배달 경계도 그대로다 — 현재화한 것은 기대
    SHA 하나뿐이다.

    T-0951 — 자기 갱신이 전달/완료 두 프로세스로 갈리고 흡수 원장·수렴 검사가 사라졌다. 사라진
    흡수 사유 표의 한 항목을 지우던 첫 역델타는 되돌릴 원문이 없어 함께 지웠고, 적용 분기의 끝
    marker 는 새 동기화 줄(`transferred + len(changes)`)로 옮겼다. 전달 분기는
    `_reverse_two_process_update` 로 역적용해 합성본이 그 세대처럼 한 프로세스 안에서 계획·적용을
    끝내게 했다(전달 분기를 남기면 RUN1 의 완료 단계가 신 updater 로 돌아 구 세대 RUN1 이 아니다).
    같은 함수가 `--paths` 거부도 걷어낸다(이 fixture 는 스코프 없이 돌아 무동작이다).
    인계 값이 없는 실행의 `transferred` 는 0 이라 동기화 줄·변경 없음 줄의 출력도 그 세대와 같다.
    계획 렌더 실패 흡수의 삭제도 `_reverse_render_eq_dst_raise` 로 역적용했다(그 세대는 흡수를 가졌다).
    같은 티켓이 conf 락의 구세대 `file_lock` 사본 호환(`_conf_lock_section`·`_local_conf_lock_path`)과
    rev 기록의 구 키 writer 대체 경로도 지웠다. 이 fixture 의 형제 사본은 현행이라 그 분기들은 두
    실행 모두에서 발화하지 않았다(역적용 없이 지문만 갱신).

    T-0956 — 흡수 말미 이관 `fill_self_repo_row`(areas.md 자기 행의 빈 git·base 채움)를 더하고
    `_main` 의 `do_migrate` 세 분기에서 기존 `migrate_renamed_conf_keys(...)` 바로 다음에 부르게 했다.
    역적용 delta 의 marker 는 전부 `sync_adapter_configs`/훅 세트 게이트·전달 분기 쪽이라 이 추가와
    겹치지 않고 그대로 유일 해소됐다(marker assert·anachronism 부재 단언 선통과). 배달 경계
    (source/manifest planning → apply → self-update 순서)와 배달 파일 집합은 무편집이다 — 이관이
    만지는 것은 manifest 밖의 areas.md 한 행뿐이다. 현재화한 것은 기대 SHA 하나뿐이다.

    T-0963 — 형제 적재 블록이 엔진 패키지 import(`from pm_engine.infra import ...`)로 바뀌고 형제 로더·
    강등 함수(`_load_repo_owned_files`·`_shared_read_api` 일습·`_atomic_replace_or_degrade`·중앙 로더
    선복구 `central_loader_needs_recovery`·`_predeploy_central_loader`)가 사라졌다. 역적용 delta 의
    marker 는 전부 `sync_adapter_configs`/훅 세트 게이트·전달 분기 쪽이라 그대로 유일 해소됐고
    (marker assert·anachronism 부재 단언 선통과), 배달 경계(source/manifest planning → apply →
    self-update 순서)와 배달 파일 집합의 계획 규칙은 그대로다 — manifest 의 디렉터리 행은 같은 계획이
    파일들로 펼친다. 현재화한 것은 기대 SHA 하나뿐이다.

    설치 공급자 이동 반영: 렌더·JSONC·등록부 호출을 실제 package 소유자로 전환했다.
    동결 fixture의 렌더 실패 흡수 anchor도 module 인자가 없는 현행 호출 형태로 맞췄다.
    배달 계획·적용·퇴역 순서와 역적용 delta는 유지하며, exact-marker와 시대착오 부재
    단언을 통과한 합성본의 지문을 재수집했다.
    """
    source = (REPO / ".project_manager" / "tools" / "pm_update.py").read_text(
        encoding="utf-8")
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
        "\n    # upstream.rev baseline 갱신",
        "",
    )
    source = _slice_replace(
        source,
        "    # 훅 세트 판정자는 **상류 세대**로 미리 해소해 넘긴다",
        "    print(f\"✓ {transferred + len(changes)} 파일 동기화\")",
        "    apply(changes)  # ← 실패 시 예외 전파 → 아래 전환 미도달(채택자 완전한 구형 유지).\n",
    )
    source = _reverse_harness_ctx_apply(source)
    source = _reverse_two_process_update(source)
    source = _reverse_render_eq_dst_raise(source)
    source = _reverse_pool_package_move(source)
    # 세대 시대착오 부재를 **기계로** 못박는다 — 전제(“codex 판정이 늘 빈 결과”)에 기대지 않는다.
    #   대조 대상은 `_main` 의 **호출 지점**이다(`apply` 시그니처의 기본값 파라미터는 호출하지
    #   않으면 발화하지 않으므로 이 합성본의 동작 축이 아니다).
    for anachronism in ("check_adapter_hook_sets(effective_dest",
                        "is_hook_set_path=resolve_hook_set_predicate(",
                        "not apply_harness_ctx_limits("):
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
    # 동결 source의 SHA는 그대로 확인한다. 제거된 target board 적재 구간만 현재 데이터
    # 공급자로 교체해 실제 adapter planning/apply/RUN2 게이트 검증을 유지한다.
    source = _t0585_pm_update_source()
    current = (REPO / ".project_manager/tools/pm_update.py").read_text(encoding="utf-8")
    old_node = next(n for n in ast.parse(source).body if getattr(n, "name", None) == "fill_self_repo_row")
    new_node = next(n for n in ast.parse(current).body if getattr(n, "name", None) == "fill_self_repo_row")
    source = source.replace(ast.get_source_segment(source, old_node),
                            ast.get_source_segment(current, new_node), 1)
    source = source.replace("_load_module_from_path = repo_files.load_module",
                            "from pm_engine.app import areas_registry\n"
                            "_load_module_from_path = repo_files.load_module", 1)
    (dest / ".project_manager" / "tools" / "pm_update.py").write_text(
        source, encoding="utf-8", newline="\n")


# T-0887: 온보딩 opt-in 일습 삭제로 역적용 delta 의 exact-marker 목록이 두 항목으로 줄고
# (되돌릴 원문이 사라졌다) 그만큼 historical 전문 SHA 도 이동했다. 퇴역 경로 delta 의 marker 는
# 전부 그 절 밖이라 그대로 유일 해소된다.
# T-0956 수정 라운드(리뷰 F-005): 주석 한 줄의 자리 표기(`harness.<이름>` → `harness.<harness>`)로
# 전문 SHA 만 이동했다. 실행 코드와 marker 는 무편집이다.
# T-0963: 형제 적재 블록의 패키지 import 전환과 형제 로더·강등·중앙 로더 선복구 삭제로 전문 SHA 가
# 이동했다. 퇴역 경로 delta 의 marker 는 전부 그 구간 밖이라 그대로 유일 해소된다.
# T-0964: 설정 레지스트리의 패키지 이동으로 `_load_local_conf` 경로 적재가 모듈 속성 호출
# (`conf_store`·`conf_schema`)로 바뀌어 전문 SHA 가 이동했다. 퇴역 경로 delta 의 marker 는 전부 그
# 구간 밖이라 그대로 유일 해소된다.
# T-0963 리뷰 F-002: conf 쓰기 락 래퍼의 `yield` 잔재와 `contextlib` import 삭제로 전문 SHA 가 이동했다.
# marker 는 무편집이다.
# T-0969: 구 역할 절 안내(판독 함수·호출) 삭제로 전문 SHA 가 이동했다. 퇴역 경로 delta 의 marker 는
# 전부 그 구간 밖이라 그대로 유일 해소된다.
# T-0967: 하네스 계약의 패키지 이동으로 `_load_relay` 경로 적재가 모듈 속성 호출(`contract`)로 바뀌어
# 전문 SHA 가 이동했다. 퇴역 경로 delta 의 marker 는 전부 그 구간 밖이라 그대로 유일 해소된다.
# T-0968: manifest 문법·판독의 패키지 이동으로 pm_update 에서 퇴역 경로 상수 뒤의 manifest 상수와
# `_parse_manifest_line` 이하 판독 함수가 빠졌다. 두 `_slice_replace` 의 끝 marker 가 가리키던 정의가
# 없어져 끝 marker 를 그 자리 다음 정의(`class _RenderDst:`·`def _read_local_conf(`)로 옮겼고, 지우는
# 구간(퇴역 경로 상수·`RetiredPathError` 부터 퇴역 이주 함수까지)은 같다. 전문 SHA 가 이동했다.
# T-0971: 등록 repo 조회의 패키지 이동으로 `reinstall_protected_hooks` 한 줄과 머리 import 한 줄이
# 바뀌어 전문 SHA 가 이동했다. 퇴역 경로 delta 의 marker 는 전부 그 구간 밖이라 그대로 유일 해소된다.
_PRE_T0876_PM_UPDATE_SHA256 = "bb59bac0de01920017020eff9d6eebc5f3417a01e7f035d2ec7151055d6600d5"


def _pre_t0876_pm_update_source() -> str:
    """현행 updater에서 T-0878·T-0876 delta를 exact-marker로 역적용한 동결 구 실행 파일.

    git history나 HEAD^가 없는 출하 트리에서도 RUN1을 실제 구 updater subprocess로 재현한다.
    whole-file SHA는 역적용 범위를 역사적 구 source bytes에 고정한다.

    재핀 이력:

    T-0908 — URL upstream 캐시 해소와 `upstream.seen_rev` 단일 writer 전환으로 `pm_update.py` 의
    소스 해소 입구·rev 기록 문구가 바뀌었다. 이 fixture 가 역적용하는 T-0878·T-0876 delta(retired-path
    선언 검증·legacy flavor 선택 판정)와 겹치지 않아 exact-marker 는 모두 그대로 유일 해소됐고
    (marker assert 선통과), RUN1 이 검증하는 배달 경계도 무편집이라 합성본 bytes 차이만 SHA 에
    반영했다. 이 fixture 의 upstream 도 로컬 경로라 캐시 분기는 실행되지 않는다.

    T-0918 — jsonc 후행 쉼표 처리 헬퍼 `_strip_jsonc_trailing_commas` 를 `pm_update.py` 에 더했다
    (어댑터 설정 판독 전용 · 호출부는 `pm_import` 하나). 이 fixture 가 역적용하는 T-0878·T-0876
    delta(retired-path 선언 검증·legacy flavor 선택 판정)와 겹치지 않아 exact-marker 는 모두 그대로
    유일 해소됐고(marker assert 선통과), RUN1 이 검증하는 배달 경계(신 reviewer 설치 → 두 번째 실행의
    구 파일 퇴역)도 무편집이라 합성본 bytes 차이만 SHA 에 반영했다.

    T-0887 — 추가 리뷰어 opt-in 온보딩 블록을 `pm_update.py` 에서 삭제했다. 이 fixture 가 역적용
    하는 T-0878·T-0876 delta 는 retired-path 선언 검증과 legacy flavor 선택 판정이라 삭제된 온보딩
    코드와 겹치지 않고, exact-marker 는 모두 그대로 유일 해소됐다(marker assert 선통과). RUN1 이
    검증하는 배달 경계(신 reviewer 설치 → 두 번째 실행의 구 파일 퇴역)도 무편집이므로 합성본
    bytes 차이만 SHA 에 반영했다. 같은 티켓의 수정 라운드에서 `plan()` docstring 한 줄의 낱말이
    바뀌어("모듈 밖 호출부") 다시 이동했다 — 역적용 marker 는 그 줄과 겹치지 않는다.
    v1.8.0 — 위와 같은 ctx 키 개칭과 conf 키 이관 단계 추가다. 이 fixture 가 역적용하는
    T-0878·T-0876 delta(retired-path 선언 검증·legacy flavor 선택 판정)와 겹치지 않아 exact-marker
    는 모두 그대로 유일 해소됐고(marker assert 선통과), RUN1 이 검증하는 배달 경계도 무편집이라
    합성본 bytes 차이만 SHA 에 반영했다.
    재핀(2026-09-18) — 삼킴형 예외 감사로 pm_update 본문의 pm_render 로드 폴백 세 곳을 지웠다
    (`_entry_doc_operational_keys`·`_local_conf_operational_map`·`_delegate_harness_from_local_conf`).
    세 소비처 모두 바로 앞줄에서 pm_render 를 무방비로 로드하므로 도달 불가 분기였고, 퇴역 경로
    delta 의 marker 도 전부 그 절 밖이라 그대로 유일 해소된다.
    재핀(2026-09-18, 2차) — 손잡이 감사로 `_path_newline` 의 `default` 인자를 지우고(어느 호출부도
    넘긴 적이 없다) 본문·docstring 이 `_DEFAULT_TEXT_NEWLINE` 상수를 직접 가리키게 했다. 이
    fixture 가 역적용하는 delta 와 겹치지 않고 배달 경계도 무편집이라, 합성본 bytes 차이만 SHA 에
    반영했다.
    재핀(2026-09-18, 3차) — 같은 감사로 `resolve_hook_set_generation` 의 `required` 인자를 지웠다
    (호출부 1곳이 넘긴 적이 없고, 값을 넘기던 안쪽 `_resolve_hook_set_generation_and_sibling` 의
    기본값이 바깥과 같은 False 라 동작이 보존된다). 역적용 marker·배달 경계 모두 무편집이다.
    재핀(T-0923) — 하네스 사용 한도 반영 단계가 들어왔다(jsonc 경로 편집 헬퍼·반영 채널과 그 흡수 사유·세 종료
    경로의 호출·이동 출력 문구). 반영 호출은 이 세대에 없던 개념이라 `_reverse_harness_ctx_apply` 로
    역적용한다. 퇴역 경로 delta 의 exact-marker 는 전부 그 절 밖이라 그대로 유일 해소되고, RUN1 이
    검증하는 배달 경계(신 reviewer 설치 → 두 번째 실행의 구 파일 퇴역)도 무편집이라 합성본 bytes 차이만
    SHA 에 반영했다.
    T-0927의 이름 이동 helper와 실패 진단 변경은 퇴역 경로 역델타와 겹치지 않는다.
    exact-marker가 선통과하고 _main·plan·apply·sync_adapter_configs·
    _adapter_config_gate_failed의 AST가 기준 HEAD와 동일함을 확인했다. 이 시나리오는 이동할
    옛 conf 키가 없어 새 실패 분기가 발화하지 않는다. 합성본 지문만 갱신했다.
    T-0945 — 설정 저장 반영(`apply_conf`)을 더하고 렌더 경로가 conf 값과 렌더러 인스턴스를 받게 했으며
    `apply` 가 render 파일을 전부 그린 뒤에 쓰게 했다. 퇴역 경로 delta(T-0878·T-0876)의 exact-marker 는
    그 절 밖이라 그대로 유일 해소되고, RUN1 이 검증하는 배달 경계(신 reviewer 설치 → 두 번째 실행의 구
    파일 퇴역)는 apply 안 render 쓰기 순서와 무관하다. `_read_local_conf` 의 파일 부재 처리(형제 로더를
    읽지 않고 {})도 이 fixture 의 배달 경계 밖이다. 합성본 지문만 갱신했다.
    T-0946 — 계획 직전의 추론 값 정리(`prune_unavailable_reasoning`)를 더했다. opencode 프로필의 추론
    값이 있을 때만 판정하며 이 fixture 의 conf 에는 그런 줄이 없다. 퇴역 경로 delta(T-0878·T-0876)의
    exact-marker 는 그 절 밖이라 그대로 유일 해소되고, RUN1 이 검증하는 배달 경계(신 reviewer 설치 →
    두 번째 실행의 구 파일 퇴역)는 무편집이다. 합성본 지문만 갱신했다. 같은 티켓의 수정 라운드에서
    저장의 추론 값 묶음과 렌더러 하나 공유(`plan` 의 선택 인자 `render_mod`)를 더했다. 퇴역 경로 delta 의
    exact-marker 와 배달 경계는 그대로다 — 합성본 지문만 갱신했다.
    T-0951 — 자기 갱신이 전달/완료 두 프로세스로 갈렸다. 전달 분기와 `--paths` 거부는
    `_reverse_two_process_update` 로 역적용해 합성본이 그 세대처럼 한 프로세스 안에서 계획·적용을
    끝내고 `--paths` 단독 자기 배달(bridge RUN1)을 하게 했다. 전달 함수 안의
    retired-path 자기치유 실패 처리도 그 세대엔 없었으므로 두 자리 모두 걷어낸다. 퇴역 경로 delta 의
    exact-marker 는 그대로 유일 해소되고 배달 경계(신 reviewer 설치 → 두 번째 실행의 구 파일 퇴역)는
    그대로다. 계획 렌더 실패 흡수의 삭제도 `_reverse_render_eq_dst_raise` 로 역적용했다 — 이 세대
    updater 의 rewrite 대상(`external_review.py` 실 실행 커맨드)은 상류 카드에 없어 렌더가 실패하고,
    그 세대는 그 실패를 '다름'으로 흡수해 source 부재 rc2 까지 갔다. conf 락의 구세대 사본 호환과
    rev 기록의 구 키 writer 대체 경로 삭제는 형제 사본이 현행인 이 fixture 에서 발화하지 않는다.
    합성본 지문만 갱신했다.
    T-0956 — 흡수 말미 이관 `fill_self_repo_row`(areas.md 자기 행의 빈 git·base 채움)를 더하고
    `do_migrate` 세 분기의 `migrate_renamed_conf_keys(...)` 다음에서 부르게 했다. 퇴역 경로
    delta(T-0878·T-0876)의 exact-marker 는 그 절 밖이라 그대로 유일 해소되고(marker assert 선통과),
    RUN1 이 검증하는 배달 경계(신 reviewer 설치 → 두 번째 실행의 구 파일 퇴역)도 무편집이다. 이관이
    만지는 것은 manifest 밖의 areas.md 한 행뿐이라 합성본 지문만 갱신했다.

    설치 공급자 이동 반영: 렌더·JSONC·등록부 호출을 실제 package 소유자로 전환했다.
    동결 fixture의 렌더 실패 흡수 anchor도 module 인자가 없는 현행 호출 형태로 맞췄다.
    배달 계획·적용·퇴역 순서와 역적용 delta는 유지하며, exact-marker와 시대착오 부재
    단언을 통과한 합성본의 지문을 재수집했다.
    """
    source = (REPO / ".project_manager" / "tools" / "pm_update.py").read_text(
        encoding="utf-8")
    # T-0878은 아래 T-0876 은퇴 기능 위에 쌓인 flavor 자기치유 보강이다. 먼저 그 delta만
    # exact-marker로 걷어내야 이어지는 T-0876 역적용이 역사적 구 source bytes로 수렴한다.
    source = _slice_replace(
        source,
        "def validated_retired_path_directives(\n",
        "def _path_is_link_or_reparse(",
        "",
    )
    source = source.replace(
        "    declarations = validated_retired_path_directives(\n"
        "        source_root, [Path(path) for path in manifest_paths]\n"
        "    )\n",
        "    declarations: list[tuple[str, str]] = []\n"
        "    seen: dict[str, str] = {}\n"
        "    for manifest_path in manifest_paths:\n"
        "        for old, new in parse_retired_path_directives(Path(manifest_path)):\n"
        "            previous = seen.get(old)\n"
        "            if previous is not None and previous != new:\n"
        "                raise RetiredPathError(\n"
        "                    f\"retired-path 선언 충돌: {old} -> {previous} / {new}\"\n"
        "                )\n"
        "            # 다중 harness 회복은 동일 지시문을 여러 flavor manifest에서\n"
        "            # 읽을 수 있다. 파일 내 중복은 parser가 거부하고, 파일 간 동일값만 접는다.\n"
        "            if previous is None:\n"
        "                seen[old] = new\n"
        "                declarations.append((old, new))\n",
        1,
    )
    source = source.replace(
        "완전 일치**할 때만 그 후보를 primary로 고른다. 단 strict retired-path 선언의 검증된 역상\n"
        "    ``(upstream - NEW) ∪ OLD``도 exact-match 후보로 인정한다. 부분집합, 존재 경로, 이름 추측,\n"
        "    최소 초과집합/tiebreak는 사용하지 않는다. 완전 일치가 아니면 로컬 manifest를 그대로 계획에\n",
        "완전 일치**할 때만 그 후보를 primary로 고른다. 부분집합, 존재 경로, 은퇴 행 추정, 최소\n"
        "    초과집합/tiebreak는 사용하지 않는다. 완전 일치가 아니면 로컬 manifest를 그대로 계획에\n",
        1,
    )
    source = source.replace(
        "    # provenance가 없는 legacy 후보를 고를 때만 후보별 선언을 검증한다. 현행 manifest가 flavor를\n"
        "    # 명시했다면 위에서 선택한 manifest만 후속 self-heal 검증 대상이며, 무관한 template flavor의\n"
        "    # 손상 선언이 그 정상 경로를 가로막아서는 안 된다.\n",
        "",
        1,
    )
    source = source.replace(
        "    candidate_retirements = {\n"
        "        candidate: validated_retired_path_directives(source_root, [candidate])\n"
        "        for candidate in candidate_entries\n"
        "    }\n",
        "",
        1,
    )
    source = _slice_replace(
        source,
        "    exact_matches = []\n",
        "    legacy_primary = (\n",
        "    exact_matches = [\n"
        "        candidate for candidate, paths in candidate_paths.items()\n"
        "        if paths == local_core_paths\n"
        "    ]\n",
    )
    source = source.replace(
        "      - retired_removed: strict retired-path 선언으로 검증되어 divergence에서 제외한 OLD 경로\n",
        "",
        1,
    )
    source = source.replace(
        "        validated_retired_path_directives(source_root, [root_manifest])\n",
        "",
        1,
    )
    source = source.replace('                "retired_removed": [],\n', "")
    source = source.replace(
        "        validated_retirements = validated_retired_path_directives(\n"
        "            source_root, upstream_manifests\n"
        "        )\n",
        "",
        1,
    )
    source = source.replace(
        "    validated_old = {old for old, _new in validated_retirements}\n"
        "    retired_removed = sorted(set(removed) & validated_old)\n"
        "    effective_removed = sorted(set(removed) - validated_old)\n",
        "",
        1,
    )
    source = source.replace("    if effective_removed or marker_divergent:\n",
                            "    if removed or marker_divergent:\n", 1)
    source = source.replace(
        "    if not added and not retired_removed and not (\n",
        "    if not added and not (\n",
        1,
    )
    source = source.replace(
        '        return {"status": "diverged", "added": added, "removed": effective_removed,\n'
        '                "retired_removed": retired_removed,\n',
        '        return {"status": "diverged", "added": added, "removed": removed,\n',
        1,
    )
    source = source.replace('            "retired_removed": retired_removed,\n', "", 1)
    source = source.replace(
        '    retired_removed = selfheal.get("retired_removed", [])\n', "", 1)
    source = source.replace(
        '        f"(선택 flavor 합집합·선언 순서 우선): 신규 등재 +{len(added)}"\n'
        '        + (f", retired-path OLD -{len(retired_removed)}" if retired_removed else "")\n',
        '        f"(선택 flavor 합집합·선언 순서 우선): 신규 등재 +{len(added)}"\n',
        1,
    )
    source = source.replace(
        "    for path in retired_removed:\n"
        "        print(f\"    - {path}  (검증된 retired-path OLD — apply 뒤 backup/퇴역)\")\n",
        "",
        1,
    )
    # 전달 프로세스(T-0951)도 같은 처리를 갖는다 — 두 자리 모두 걷어내야 클래스 정의를 지운
    #   합성본에 미정의 이름이 남지 않는다(전달 함수는 호출되지 않지만 정의 안의 참조도 지운다).
    retired_abort = (
        "    except RetiredPathError as exc:\n"
        "        print(f\"[중단] retired-path manifest 자기치유 실패 — {exc}\", file=sys.stderr)\n"
        "        return 1\n"
    )
    assert source.count(retired_abort) == 2, retired_abort
    source = source.replace(retired_abort, "")
    # T-0887 이 온보딩 opt-in 일습을 지워, 그 절을 되돌리던 역전 항목들도 함께 사라졌다
    # (되돌릴 원문이 없다 — 죽은 예외를 남기지 않는다). 남은 것은 개칭 축 하나뿐이다.
    exact_reversals = (
        ("import hashlib\n", ""),
        ("`additional_reviewer.py` 실 실행 커맨드", "`external_review.py` 실 실행 커맨드"),
    )
    for current, legacy in exact_reversals:
        assert current in source, current
        source = source.replace(current, legacy)
    source = _slice_replace(
        source,
        "# 구 updater도 무시하는 manifest 주석 지시문. 일반 상류 부재 파일을\n",
        "\n\nclass _RenderDst:",
        "",
    )
    source = _slice_replace(
        source, "class RetiredPathError(RuntimeError):\n", "def _read_local_conf(", "",
    )
    source = _slice_replace(
        source,
        "    # manifest가 명시한 engine-owned 구 경로 퇴역도 전량 self-sync에서만 돈다.\n",
        "    # ── 보호 훅 정합 확인 + drift 재설치",
        "\n",
    )
    for block in (
        "        if do_retire and not _run_retired_path_migration(\n"
        "            effective_dest, source_root, manifest, selfheal,\n"
        "            write=not args.dry_run,\n"
        "        ):\n"
        "            return 1\n",
        "        if do_retire and not _run_retired_path_migration(\n"
        "            effective_dest, source_root, manifest, selfheal, write=False,\n"
        "            changes=changes,\n"
        "        ):\n"
        "            return 1\n",
        "    # apply 완료를 관측한 뒤에만 구 경로를 옮긴다. 이 게이트가 실패하면\n"
        "    # 아래 instance migration·upstream.rev baseline 모두에 도달하지 않는다.\n"
        "    if do_retire and not _run_retired_path_migration(\n"
        "        effective_dest, source_root, manifest, selfheal, write=True,\n"
        "    ):\n"
        "        return 1\n\n",
    ):
        assert source.count(block) == 1, block
        source = source.replace(block, "", 1)
    source = _reverse_harness_ctx_apply(source)
    source = _reverse_two_process_update(source)
    source = _reverse_render_eq_dst_raise(source)
    source = _reverse_pool_package_move(source)
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    assert digest == _PRE_T0876_PM_UPDATE_SHA256, (
        f"pre-T0876 updater fixture drift: {digest}; retirement delta의 exact-marker와 "
        "historical whole-file SHA를 함께 재검토하라"
    )
    return source


def _make_pre_t0876_codex_framework(tmp_path: Path) -> Path:
    """현행 출하 tree를 구 reviewer 물리명/manifest/updater 세대로 되감은 설치 소스."""
    framework = _build_codex_framework(tmp_path)
    for tools in (
        framework / ".project_manager" / "tools",
        framework / "templates" / "codex" / ".project_manager" / "tools",
    ):
        (tools / "additional_reviewer.py").replace(tools / "external_review.py")
        (tools / "pm_update.py").write_text(
            _pre_t0876_pm_update_source(), encoding="utf-8", newline="\n")
    for manifest in (
        framework / ".project_manager" / "engine.manifest",
        framework / "templates" / "codex" / ".project_manager" / "engine.manifest",
    ):
        lines = [
            line for line in manifest.read_text(encoding="utf-8").splitlines()
            if not line.startswith("# pm-retired-path:")
        ]
        manifest.write_text(
            "\n".join(lines).replace("additional_reviewer", "external_review") + "\n",
            encoding="utf-8", newline="\n",
        )
    # 되감은 뒤의 트리가 출하 인벤토리다 — rename/재작성분을 index 에 현행화한다.
    init_git_repo(framework, commit="pre-T-0876 세대")
    return framework


def test_legacy_updater_run1_delivers_new_reviewer_and_run2_retires_old(tmp_path):
    """구 full rc2 → updater 단독 self-delivery → 신 full 1회가 rename을 수렴한다."""
    legacy_framework = _make_pre_t0876_codex_framework(tmp_path)
    dest = tmp_path / "legacy-reviewer-adopter"
    from _manager_home import known_ctx_env
    installed = subprocess.run(
        [
            sys.executable,
            str(legacy_framework / ".project_manager" / "tools" / "pm_import.py"),
            "--new", str(dest), "--harness", "codex", "--name", _ADOPTER_NAME,
            "--fill", "manual", "--from", str(legacy_framework),
        ],
        cwd=str(legacy_framework), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=300,
        env=known_ctx_env({**os.environ, "PM_NONINTERACTIVE": "1"}, native_bin=dest.parent / "known-ctx-native"),
    )
    assert installed.returncode == 0, f"{installed.stdout}\n{installed.stderr}"

    tools = dest / ".project_manager" / "tools"
    updater = tools / "pm_update.py"
    old_reviewer = tools / "external_review.py"
    new_reviewer = tools / "additional_reviewer.py"
    old_updater_bytes = _pre_t0876_pm_update_source().encode("utf-8")
    assert updater.read_bytes() == old_updater_bytes
    assert old_reviewer.is_file() and not new_reviewer.exists(), \
        "RUN1 전 구 물리명 단독 형상이 아니다"
    old_reviewer.write_bytes(old_reviewer.read_bytes() + b"\n# adopter-local legacy bytes\n")
    preserved = old_reviewer.read_bytes()
    if os.name != "nt":
        old_reviewer.chmod(0o751)
    manifest = dest / ".project_manager" / "engine.manifest"
    old_manifest_bytes = manifest.read_bytes()

    # 불가능한 one-shot 계약을 음성 증거로 고정한다. 구 updater는 source에서 사라진 OLD를
    # apply 전에 거부하며 updater/reviewer/manifest 어느 것도 바꾸지 않는다.
    impossible = _run_adopter_tool(dest, "pm_update.py", "--from", str(REPO))
    assert impossible.returncode == 2, f"{impossible.stdout}\n{impossible.stderr}"
    assert "source 에 없음" in impossible.stderr and "external_review.py" in impossible.stderr
    assert updater.read_bytes() == old_updater_bytes
    assert manifest.read_bytes() == old_manifest_bytes
    assert old_reviewer.read_bytes() == preserved and not new_reviewer.exists()

    # bridge RUN1: 구 manifest가 이미 소유하는 updater 하나만 self-delivery한다. manifest와
    # reviewer는 손복사/선전파하지 않으며 부분 실행이라 baseline도 전진하지 않는다.
    run1 = _run_adopter_tool(
        dest, "pm_update.py", "--from", str(REPO), "--paths",
        ".project_manager/tools/pm_update.py",
    )
    assert run1.returncode == 0, f"{run1.stdout}\n{run1.stderr}"
    assert "파일 동기화" in run1.stdout, "RUN1이 실제 planning/apply 경로를 타지 않았다"
    assert updater.read_bytes() == (REPO / ".project_manager/tools/pm_update.py").read_bytes(), \
        "구 updater가 신 updater를 self-deliver하지 못했다"
    assert updater.read_bytes() != old_updater_bytes
    assert manifest.read_bytes() == old_manifest_bytes, "scoped RUN1이 manifest를 손댔다"
    assert not new_reviewer.exists(), "scoped RUN1이 reviewer를 선전파했다"
    assert old_reviewer.read_bytes() == preserved, \
        "directive를 모르는 구 updater RUN1이 old를 조기 제거/수정했다"

    # bridge RUN2: RUN1이 놓은 신 updater의 첫 full sync 한 번이 manifest+NEW 적용 뒤 OLD를
    # backup으로 이동한다. 이 사이 manifest/reviewer 손복사는 없다.
    run2 = _run_adopter_tool(dest, "pm_update.py", "--from", str(REPO))
    assert run2.returncode == 0, f"{run2.stdout}\n{run2.stderr}"
    assert "파일 동기화" in run2.stdout, "신 updater full sync가 apply 경계를 타지 않았다"
    assert "engine.manifest 자기치유" in run2.stdout
    assert "retired-path OLD" in run2.stdout
    assert "[퇴역] .project_manager/tools/external_review.py" in run2.stdout
    assert manifest.read_bytes() == \
        (REPO / "templates/codex/.project_manager/engine.manifest").read_bytes()
    assert new_reviewer.read_bytes() == \
        (REPO / ".project_manager/tools/additional_reviewer.py").read_bytes()
    assert not old_reviewer.exists()
    backups = list((dest / ".pm_import_backups").glob(
        "*/.project_manager/tools/external_review.py"))
    assert len(backups) == 1, backups
    assert backups[0].read_bytes() == preserved, "RUN2 backup이 채택자 old bytes를 보존하지 않았다"
    if os.name != "nt":
        assert stat.S_IMODE(backups[0].stat().st_mode) == 0o751
    conf = {
        key.strip(): value.strip()
        for line in (dest / ".project_manager/local.conf").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
        for key, value in [line.split("=", 1)]
    }
    assert conf.get("upstream.rev") and conf["upstream.rev"] == conf.get("upstream.seen_rev")


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
    # `.local` 은 per-clone 스크래치(git-untracked·manifest 밖)다. pytest 임시 루트가
    # 그 아래 있으므로 제외하지 않으면 목적지를 다시 복사해 무한 재귀한다.
    ignore = shutil.ignore_patterns("__pycache__", ".git", "node_modules", ".local")
    shutil.copytree(REPO / ".project_manager", framework / ".project_manager", ignore=ignore)
    shutil.copytree(REPO / "templates" / "codex",
                    framework / "templates" / "codex", ignore=ignore)
    shutil.copytree(REPO / ".claude" / "skills", framework / ".claude" / "skills",
                    ignore=ignore)
    shutil.copy2(REPO / ".gitattributes", framework / ".gitattributes")
    # 출하 인벤토리는 `git ls-files` 가 낸다 — 합성 프레임워크 트리가 자기 checkout 이라고
    # 선언한다(선언이 없으면 이 저장소가 답해 인벤토리가 0건이 된다).
    init_git_repo(framework, commit="framework fixture")
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
    declared = tuple(entrypoint.event for entrypoint
                     in _PM_IMPORT.ADAPTER_HOOK_SET["codex"].entrypoints)
    assert set(installed) == set(declared), "설치 config 와 엔진 선언의 이벤트 집합이 갈렸다"
    for event in declared:
        groups = installed[event]
        expected = "^(Bash|apply_patch|collaborationspawn_agent)$" if event == "PreToolUse" else ".*"
        assert len(groups) == 1 and groups[0]["matcher"] == expected, event
        for key in ("command", "commandWindows"):
            assert _CODEX_DISPATCHER_REL in groups[0]["hooks"][0][key], (event, key)
            assert f"--hook-dispatch {event}" in groups[0]["hooks"][0][key], (event, key)
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


# ── S8 진입점 집합이 늘어난 릴리즈의 흡수 (T-0806) ────────────────────────────
# 이관 3 이벤트가 진입점 밖에 있던 세대(T-0777)의 채택자가 이 릴리즈를 흡수하는 창이다.
# 그 창에서 **무편집 채택자는 advisory 0줄**이어야 한다 — pm_update 는 같은 실행에서
# `sync_adapter_configs(write=True)` 로 config 를 교체한 뒤 진입점을 판정하므로, 판정 시점의
# config 는 이미 새 세대다. 이 순서가 뒤집히면 정상 흡수마다 거짓 소견 3줄이 나간다.

_T0806_MIGRATED_EVENTS = {"SubagentStart": (".*",),
                          "PreCompact": ("^auto$", "^manual$"),
                          "PostCompact": ("^auto$", "^manual$")}


def _pre_t0806_hooks_document(framework: Path) -> dict:
    """T-0806 이전 세대 형상 — 이관 3 이벤트가 진입점 밖 직결 배선으로 남은 config."""
    hooks_rel = "templates/codex/.codex/hooks.json"
    document = json.loads((framework / hooks_rel).read_text(encoding="utf-8"))
    for event, matchers in _T0806_MIGRATED_EVENTS.items():
        document["hooks"][event] = [
            {"matcher": matcher,
             "hooks": [{"type": "command", "timeout": 10,
                        "command": '"$py" .project_manager/tools/pm_log.py checkpoint',
                        "commandWindows":
                            "& $py '.project_manager/tools/pm_log.py' checkpoint"}]}
            for matcher in matchers]
    return document


@pytest.mark.skipif(not posix_bash_supported(),
                    reason="POSIX bash wrapper 실행 환경이 아님")
def test_upgrade_adopter_absorbs_the_widened_entrypoint_set_without_advisory_noise(
        tmp_path):
    """진입점 집합이 늘어난 릴리즈를 무편집 채택자가 흡수 1회로 수렴한다(소견 0줄)."""
    framework = _build_codex_framework(tmp_path)
    hooks_rel = ".codex/hooks.json"
    upstream_hooks = framework / "templates" / "codex" / hooks_rel
    current_bytes = upstream_hooks.read_bytes()
    upstream_hooks.write_text(
        json.dumps(_pre_t0806_hooks_document(framework), ensure_ascii=False, indent=2)
        + "\n", encoding="utf-8", newline="\n")

    dest = tmp_path / "codex-adopter"
    assert _PM_IMPORT.main([
        "--new", str(dest), "--harness", "codex", "--name", _ADOPTER_NAME,
        "--fill", "manual", "--from", str(framework),
    ]) == 0
    # 전제: 이 세대 채택자에는 진입점이 3건 빠져 있다(소견이 발화할 수 있는 상태).
    before = _PM_IMPORT.judge_adapter_hook_entrypoints(dest, framework, ["codex"])
    assert {finding.event for finding in before} == set(_T0806_MIGRATED_EVENTS), before

    upstream_hooks.write_bytes(current_bytes)  # 상류를 이 릴리즈 세대로 되돌린다.
    run = _run_adopter_tool(dest, "pm_update.py", "--from", str(framework))

    assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"
    assert (dest / hooks_rel).read_bytes() == current_bytes, \
        "무편집 managed config 가 새 세대로 교체되지 않았다"
    assert "훅 진입점 누락" not in run.stderr, \
        f"같은 실행에서 교체된 config 에 거짓 소견이 났다\n{run.stderr}"
    assert _PM_IMPORT.judge_adapter_hook_entrypoints(dest, framework, ["codex"]) == []
    # 흡수 뒤 압축 진입점이 실제로 등록 기능을 돌린다(형태 수렴 + 발화 확인).
    envelope = _run_shipped_entrypoint(
        dest, "PreCompact",
        {"hook_event_name": "PreCompact", "cwd": str(dest), "model": "m",
         "session_id": "s", "transcript_path": None, "trigger": "auto", "turn_id": "t"})
    assert envelope and "adapter-fallback" not in json.dumps(envelope, ensure_ascii=False)

# ── S8 구표기 conf 채택자의 전환 (dry-run → RUN1 → RUN2 → 교체 → gate) ───────
#
# 표기 통일은 채택자 conf 를 깨뜨리는 전환이라, 채택자가 **무엇을 어떻게 고치는지**를 실 CLI 흐름
# 에서 듣지 못하면 전환이 성립하지 않는다. 특히 위험한 창은 두 곳이다:
#   (1) 구표기 `upstream=` 만 있는 채택자는 upstream 미해소로 rc=1 로 끝난다 — 그 실행이 안내를
#       내지 않으면 채택자는 엔진을 받을 방법도, 무엇이 문제인지도 모른다.
#   (2) 엔진을 배달하는 RUN1 은 **구 updater** 가 돈다(새 안내 코드가 아직 그 프로세스에 없다).
#       그래서 공개 절차는 변경 0 RUN2 를 필수로 적고, 그 실행이 안내를 낸다.
# 여기서는 그 흐름을 실 facade/CLI 로 돌려 값으로 잠근다(helper 수동 호출이 아니다).

# 부분 이주 형상 — 렌더 채널이 쓰는 operational 키는 이미 신표기이고, 위임·ctx·upstream 축만
# 구표기로 남았다(실 채택자가 안내를 받기 전 상태). 렌더 축까지 구표기면 자족 산출물 토큰 가드가
# 먼저 서서 이 시나리오가 재는 **안내 시점**이 가려진다.
_OPERATIONAL_CONF = (
    "project.name=Upgrade Adopter\n"
    "runtime.py=python3\n"
    "test.cmd=python -c \"print('adopter suite')\"\n"
)
# 사용 한도 543210 은 Claude Code 압축 지점 범위(100000~1000000) 안이다 — 옮긴 값이 반영 검사를
#   통과해야 (5) 의 교체 뒤 실행이 설정 파일까지 반영한다(T-0923).
_LEGACY_AXES_CONF = (
    "delegate_enabled=true\n"
    "ctx_window_tokens_claude=543210\n"
    "regression_min_collected=40\n"
)


def _parse_conf(path: Path) -> dict[str, str]:
    """local.conf 활성 키만 (주석 제외·last-wins) — 이관 결과를 값으로 확인한다."""
    conf: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _, value = stripped.partition("=")
            conf[key.strip()] = value.strip()
    return conf


def _raw_pm_update(dest: Path, *args: str) -> subprocess.CompletedProcess:
    """rc 를 판정하지 않는 pm_update 실행 — 실패 경로의 출력까지 본다."""
    from _manager_home import known_ctx_env
    return subprocess.run(
        [sys.executable, str(dest / ".project_manager" / "tools" / "pm_update.py"), *args],
        cwd=str(dest), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=known_ctx_env({**os.environ, "PM_NONINTERACTIVE": "1"},
                          native_bin=dest.parent / "known-ctx-native"), timeout=300,
    )


def _make_engine_file_stale(dest: Path) -> Path:
    """엔진 파일 하나를 구세대로 되돌린다 — RUN1(배달)/RUN2(변경 0)를 실제로 가르기 위해."""
    stale = dest / ".project_manager" / "tools" / "domain.py"
    stale.write_text(stale.read_text(encoding="utf-8") + "\n# 구세대 사본 표식\n",
                     encoding="utf-8")
    return stale


def test_upgrade_adopter_legacy_conf_is_guided_then_replaced_then_gates_run(tmp_path):
    """구표기 conf 채택자: 안내 → 교체 → 게이트 동작이 실 CLI 로 닫힌다."""
    dest = tmp_path / "legacy-conf-adopter"
    assert _PM_IMPORT.main([
        "--new", str(dest), "--harness", "claude", "--name", _ADOPTER_NAME,
        "--fill", "manual",
    ]) == 0
    _prime_engine_to_canonical(dest)
    conf = dest / ".project_manager" / "local.conf"
    conf.write_text(f"upstream={REPO}\n" + _OPERATIONAL_CONF + _LEGACY_AXES_CONF,
                    encoding="utf-8")

    # (1) 구표기 upstream 만 있는 형상 — 해소는 실패해도 **안내는 나간다**.
    unresolved = _raw_pm_update(dest)
    assert unresolved.returncode == 1, unresolved.stdout
    assert "위임 기본값이 허용으로 바뀝니다" in unresolved.stdout
    assert "`upstream` → `upstream.path`" in unresolved.stdout
    assert "`delegate_enabled` → `delegate.enabled`" in unresolved.stdout
    # 이름이 바뀐 키는 갱신이 옮긴다 — 안내는 그 사실을 알리고, 사람에게 남기는 것은
    # 대체가 없어 옮길 자리가 없는 키뿐이다(옛 안내의 "모델 값은…" 변명은 지워졌다).
    assert "갱신이 새 이름으로 옮깁니다" in unresolved.stdout
    assert "제거(대체 키 없음)" in unresolved.stdout
    assert "모델 값은 자동으로 옮기지 않습니다" not in unresolved.stdout
    assert ".codex/config.toml" in unresolved.stdout          # 엔진이 못 고치는 파일 지목

    # (2) dry-run 도 성공 반환 전에 같은 안내를 낸다(무write 실행이라 더더욱 여기서 들어야 한다).
    before = conf.read_text(encoding="utf-8")
    planned = _raw_pm_update(dest, "--from", str(REPO), "--dry-run")
    assert planned.returncode == 0, planned.stderr
    assert "위임 기본값이 허용으로 바뀝니다" in planned.stdout
    assert "`regression_min_collected` → 제거(대체 키 없음)" in planned.stdout
    assert conf.read_text(encoding="utf-8") == before, "dry-run 이 채택자 conf 를 고쳤다"

    # (3) RUN1(배달) → RUN2(변경 0) — 공개 절차가 요구하는 두 실행 **모두** 안내를 낸다.
    stale = _make_engine_file_stale(dest)
    run1 = _raw_pm_update(dest, "--from", str(REPO))
    assert run1.returncode == 0, run1.stderr
    assert "파일 동기화" in run1.stdout and "위임 기본값이 허용으로 바뀝니다" in run1.stdout
    assert stale.read_bytes() == (REPO / ".project_manager/tools/domain.py").read_bytes()
    # 이름이 바뀐 키는 이 실행이 **옮긴다** — 지목하고 끝나지 않는다.
    assert "`ctx_window_tokens_claude` → `harness.claude.ctx_window_tokens`" in run1.stdout
    assert "개 이동 — 갱신이 바꾼 이름이라 갱신이 옮긴다" in run1.stdout
    assert _parse_conf(conf)["harness.claude.ctx_window_tokens"] == "543210"
    # 폐지 키가 남아 있는 동안 반영은 건너뛴다 — 배달은 계속된다(rc 0).
    assert "하네스 설정 반영을 건너뛴다" in run1.stdout and "regression_min_collected" in run1.stdout

    run2 = _raw_pm_update(dest, "--from", str(REPO))
    assert run2.returncode == 0, run2.stderr
    assert "최신 — 변경 없음." in run2.stdout
    assert "위임 기본값이 허용으로 바뀝니다" in run2.stdout
    # 옮긴 뒤엔 그 키를 다시 지목하지 않는다(멱등) — 남는 지목은 대체 없는 폐지 키뿐이다.
    assert "`ctx_window_tokens_claude`" not in run2.stdout
    assert "`regression_min_collected` → 제거(대체 키 없음)" in run2.stdout

    # (4) 교체 전 게이트는 멈춘다 — 조용히 엔진 기본값으로 돌지 않는다.
    blocked = _run_adopter_tool(dest, "board.py", "list")
    assert blocked.returncode == 1, blocked.stdout
    assert "구표기 키가 남아 있습니다" in blocked.stderr + blocked.stdout

    # (5) 교체 후 같은 게이트가 돈다.
    conf.write_text(f"upstream.path={REPO}\n" + _OPERATIONAL_CONF
                    + "delegate.enabled=true\nharness.claude.ctx_window_tokens=543210\n"
                      "regression.min_collected=40\n", encoding="utf-8")
    green = _run_adopter_tool(dest, "board.py", "list")
    assert green.returncode == 0, green.stderr
    settled = _raw_pm_update(dest, "--from", str(REPO))
    assert settled.returncode == 0, settled.stderr
    assert "→ `" not in settled.stdout, "교체 뒤에도 키 지목이 남았다"
    # 옛 키가 사라진 실행은 사용 한도를 설정 파일에 반영한다(543210 × 90% = 488889).
    ctx = json.loads((dest / '.project_manager/.local/ctx-budget.json').read_text(encoding='utf-8'))['harnesses']['claude']
    assert (ctx['W'], ctx['C'], ctx['E'], ctx['P']) == (543210, 200000, 200000, 180000)
    assert json.loads((dest / '.claude/settings.json').read_text(encoding='utf-8'))['autoCompactWindow'] == 180000


def test_public_update_procedure_documents_the_v1713_three_runs():
    """공개 절차(README)가 v1.7.13 이하 사본은 v1.8.0 을 먼저 거친다고 적는다 — 이 판으로의 직행은
    지원하지 않는다(T-0963). 절차는 미리보기·갱신·변경 0 확인 세 줄이다."""
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    section = readme.split("엔진 갱신은 채택자 루트에서 받는다", 1)[1][:900]

    assert "v1.8.0 을 먼저 거친다" in section
    assert "변경 0" in section
    assert section.count("./pm-update.sh") >= 3      # dry-run + 갱신 + 변경 0 확인


# ── 옛 홈 자신 행 제거 (T-0919 마이그레이션) ──────────────────────────────────
# 옛 엔진은 홈 자신을 첫 슬롯 행(`slot="."`)으로 등재했다. 그 경로를 지우면서 이미 박힌 행도
# 흡수가 지운다 — 지우기만 하면 관리자 화면에 작업 폴더 `.` 로 계속 보인다(사용자 발견 증상).


def _seed_legacy_home_row(dest: Path) -> Path:
    """옛 홈 형상 — 홈 자신 행(`.`) + 작업 폴더 행 1개."""
    ledger = dest / ".project_manager" / ".local" / "worktree-leases.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps({"leases": [
        {"slot": ".", "repo": "demo", "session": "demo_1", "started": "2026-08-22T00:00:00+00:00", "state": "leased", "bound": True},
        {"slot": "work/demo_1", "repo": "demo", "session": "demo_1", "state": "leased"},
    ], "tasks": []}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return ledger


def _lease_slots(ledger: Path) -> list[str]:
    return [row["slot"]
            for row in json.loads(ledger.read_text(encoding="utf-8"))["leases"]]


def test_upgrade_adopter_drops_the_legacy_home_row_on_sync(tmp_path):
    """흡수가 옛 홈 자신 행을 지운다 — 작업 폴더 행은 그대로 · 재실행은 byte 불변(멱등)."""
    dest = tmp_path / "legacy-home-row"
    assert _PM_IMPORT.main(
        ["--new", str(dest), "--harness", "claude", "--name", _ADOPTER_NAME, "--fill", "manual"]
    ) == 0
    _prime_engine_to_canonical(dest)
    ledger = _seed_legacy_home_row(dest)

    # dry-run 은 판정만 — 장부 byte 불변.
    before = ledger.read_bytes()
    preview = _run_pm_update(dest, "--dry-run")
    assert ledger.read_bytes() == before, "dry-run 이 장부를 고쳤다"
    assert "PM 홈 자신 행" in _combined_output(preview), _combined_output(preview)

    _run_pm_update(dest)
    assert _lease_slots(ledger) == ["work/demo_1"], _lease_slots(ledger)

    after = ledger.read_bytes()
    _run_pm_update(dest)
    assert ledger.read_bytes() == after, "지울 행이 없는데 재흡수가 장부를 다시 썼다(비멱등)"


# ── 전달/완료 두 프로세스 갱신 (T-0951) ─────────────────────────────────────────────────────────────
# 실제 git 상류(`make_committed_source`)·실제 설치(`pm_import --new`)·서브프로세스 실행만 쓴다.

_T0951_SKEW_NOTICE = "엔진 사본 버전 불일치"


@pytest.fixture
def t0951_opencode_path(request, tmp_path, monkeypatch):
    """opencode 형상만 가짜 opencode 를 PATH 앞에 둔다(POSIX 아닌 플랫폼은 그 형상만 skip)."""
    if request.node.callspec.params["harness"] != "opencode":
        return None
    return request.getfixturevalue("t0946_fake_opencode")


# 패키지 트리 첫 채택의 고정 기점 — 태그 조회나 최신 함수 합성으로 대체하지 않는다.
_PINNED_V18_COMMITS = {
    "v1.8.0": "9612e2ec19c5742d27f5fa78f930cfbed40ee384",
    "v1.8.1": "298ffe604ab951146b0acc4d70b57a16a563a1bc",
}
# 엔진 패키지로 옮겨 평면 위치에서 은퇴한 모듈(manifest `# pm-retired-path:`).
_PACKAGE_RETIRED_FLAT_MODULES = ("file_lock.py", "console_encoding.py", "repo_owned_files.py")


def _pinned_source(tmp_path: Path, commit: str) -> Path:
    """이 저장소를 고정 커밋으로 clone 한 설치 소스."""
    from _manager_home import _git
    root = tmp_path / "pinned-source"
    _git("clone", "--quiet", "--no-checkout", "--no-hardlinks", str(REPO), str(root))
    _git("-C", str(root), "checkout", "--quiet", "--detach", commit)
    assert _git("-C", str(root), "rev-parse", "HEAD").stdout.strip() == commit
    return root


@pytest.mark.parametrize("harness", ["claude", "codex", "opencode"])
@pytest.mark.parametrize("generation", sorted(_PINNED_V18_COMMITS))
def test_pinned_v18_adopter_updates_to_package_tree_in_one_run(
        generation, harness, tmp_path, t0927_upstream, t0951_opencode_path):
    """v1.8.0·v1.8.1 사본은 엔진 패키지 트리(`pm_engine/`)를 한 번의 갱신으로 받는다 (T-0963 AT-002).

    1회차는 설치된 옛 갱신기가 엔진 코드를 옮기고 받은 엔진의 완료 프로세스가 마친다. 다음 실행은
    변경 0 확인이다. 옛 평면 모듈 셋은 은퇴 지시로 치워지고 패키지 모듈은 상류와 바이트가 같다."""
    from _manager_home import _run, known_ctx_env
    source = _pinned_source(tmp_path, _PINNED_V18_COMMITS[generation])
    dest = tmp_path / f"{generation}-{harness}"
    _run([sys.executable, str(source / ".project_manager/tools/pm_import.py"),
          "--new", str(dest), "--harness", harness, "--fill", "manual",
          "--from", str(source), "--upstream", str(t0927_upstream)],
         cwd=source, env=known_ctx_env(native_bin=dest.parent / "known-ctx-native"))
    tools = dest / ".project_manager/tools"
    assert (tools / "file_lock.py").is_file(), "고정 기점 설치가 옛 평면 형상이 아니다"

    run1 = _t0927_direct_update(dest)
    assert run1.returncode == 0, f"RUN1\n{run1.stdout}\n{run1.stderr}"
    before = _snapshot_tree(dest)
    run2 = _t0927_direct_update(dest)
    assert run2.returncode == 0, f"RUN2\n{run2.stdout}\n{run2.stderr}"
    assert "변경 없음" in run2.stdout, run2.stdout
    assert _snapshot_tree(dest) == before, "RUN2 가 트리를 바꿨다"

    for name in _PACKAGE_RETIRED_FLAT_MODULES:
        assert not (tools / name).exists(), name
    package_fs = ".project_manager/tools/pm_engine/infra/fs.py"
    assert (dest / package_fs).read_bytes() == (t0927_upstream / package_fs).read_bytes()
    status = subprocess.run(
        [sys.executable, str(tools / "pm_config.py"), "status", "--json"],
        cwd=dest, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    assert status.returncode == 0, status.stderr
    # 설정 레지스트리는 패키지(`pm_engine/domain/conf_schema.py`)로 옮겨 갔고 `local_conf.py` 는
    # CLI 진입 파일로 남는다 — 관리자가 프로젝트 사본마다 부르는 `--keys --json` 이 갱신 뒤에도 돈다(T-0964).
    assert (tools / "pm_engine/domain/conf_schema.py").is_file()
    keys = subprocess.run(
        [sys.executable, str(tools / "local_conf.py"), "--keys", "--json"],
        cwd=dest, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    assert keys.returncode == 0, keys.stderr
    assert keys.stdout.startswith('{"schema"'), keys.stdout[:200]


def _t0951_engine_code_rows(source: Path) -> list[str]:
    """상류 manifest 의 엔진 코드 행(`.project_manager/tools/` 아래 `.py`) — 출하 순서 그대로.

    디렉터리 행(엔진 패키지 `pm_engine`)은 그 아래 `.py` 파일들로 펼친다."""
    rows = []
    for line in (source / ".project_manager/engine.manifest").read_text(encoding="utf-8").splitlines():
        rel = line.split("#", 1)[0].split()[0] if line.split("#", 1)[0].split() else ""
        if not rel.startswith(".project_manager/tools/"):
            continue
        if rel.endswith(".py"):
            rows.append(rel)
        elif (source / rel).is_dir():
            rows.extend(path.relative_to(source).as_posix()
                        for path in engine_python_files(source / rel))
    return rows


# 적재 기록 한 줄 — 이 줄이 붙은 모듈이 적재되면 적재한 프로세스의 pid 와 파일을 남긴다.
_T0951_LOAD_PROBE = (
    '__import__("os").environ.get("P1_LOAD_LOG") and open(__import__("os").environ["P1_LOAD_LOG"], '
    '"a", encoding="utf-8").write(f"{__import__(\'os\').getpid()} {__file__}\\n")\n')


def test_transferring_process_loads_no_copied_module(tmp_path):
    """전달 프로세스는 엔진 코드를 복사한 뒤 그 사본을 하나도 적재하지 않는다 (AT-003 · 불변식 I1).

    상류의 엔진 코드 전부에 적재 기록 줄을 붙인다. 사본에는 그 줄이 없으므로 기록은 복사 뒤 적재만
    잡는다. 전달 프로세스(`pm_config.py update` 자신)의 pid 로 남은 줄은 0, 완료 프로세스(다른 pid)의
    줄은 1개 이상이어야 한다."""
    from _manager_home import _git, known_ctx_env, make_committed_source, make_pm_home
    source = make_committed_source(tmp_path / "source")
    home = make_pm_home(tmp_path / "home", upstream=source, source=source)
    rows = _t0951_engine_code_rows(source)
    assert len(rows) >= 30, rows
    for rel in rows:
        path = source / rel
        path.write_text(path.read_text(encoding="utf-8") + "\n" + _T0951_LOAD_PROBE,
                        encoding="utf-8", newline="\n")
    _git("-C", str(source), "commit", "-qam", "load probe")
    log = tmp_path / "load.log"
    env = known_ctx_env({**os.environ, "PM_NONINTERACTIVE": "1", "P1_LOAD_LOG": str(log)},
                        native_bin=home.parent / "known-ctx-native")

    proc = subprocess.Popen(
        [sys.executable, str(home / ".project_manager/tools/pm_config.py"), "update"],
        cwd=home, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace", env=env)
    stdout, stderr = proc.communicate(timeout=300)

    assert proc.returncode == 0, f"{stdout}\n{stderr}"
    assert "파일 동기화" in stdout, stdout
    entries = [line.split(" ", 1) for line in log.read_text(encoding="utf-8").splitlines()]
    transfer_loads = [path for pid, path in entries if int(pid) == proc.pid]
    completion_loads = [path for pid, path in entries if int(pid) != proc.pid]
    assert transfer_loads == [], transfer_loads
    assert completion_loads, "완료 프로세스가 받은 엔진을 적재하지 않았다"


def _t0951_insert_after_imports(path: Path, block: str) -> None:
    """모듈 최상위의 docstring·import 묶음 바로 뒤에 `block` 을 넣는다."""
    import ast
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    last = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)) or (
                isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)):
            last = node.end_lineno
            continue
        break
    lines = text.splitlines(keepends=True)
    lines.insert(last, block)
    path.write_text("".join(lines), encoding="utf-8", newline="\n")


def test_completion_exit_code_and_output_are_relayed(tmp_path):
    """완료 프로세스의 종료 코드와 한글 출력이 전달 프로세스를 거쳐 그대로 나온다 (AT-006 · 불변식 I4)."""
    from _manager_home import _git, known_ctx_env, make_committed_source, make_pm_home
    source = make_committed_source(tmp_path / "source")
    home = make_pm_home(tmp_path / "home", upstream=source, source=source)
    _t0951_insert_after_imports(
        source / ".project_manager/tools/pm_update.py",
        'if os.environ.get("PM_UPDATE_COMPLETION"):\n'
        '    print("완료 단계 표식 출력")\n'
        '    print("완료 단계 표식 오류", file=sys.stderr)\n'
        '    raise SystemExit(7)\n')
    _git("-C", str(source), "commit", "-qam", "completion marker")

    run = subprocess.run(
        [sys.executable, str(home / ".project_manager/tools/pm_update.py"), "--from", str(source)],
        cwd=home, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300, env=known_ctx_env({**os.environ, "PM_NONINTERACTIVE": "1"},
                                       native_bin=home.parent / "known-ctx-native"))

    assert run.returncode == 7, f"{run.stdout}\n{run.stderr}"
    assert "[update] .project_manager/tools/pm_update.py" in run.stdout, run.stdout
    assert "완료 단계 표식 출력" in run.stdout, run.stdout
    assert "완료 단계 표식 오류" in run.stderr, run.stderr


def _t0951_set_engine_rev(path: Path, rev: str) -> None:
    text = path.read_text(encoding="utf-8")
    new_text, count = re.subn(r'^ENGINE_REV = "[^"]*"', f'ENGINE_REV = "{rev}"', text,
                              count=1, flags=re.MULTILINE)
    assert count == 1, path
    path.write_text(new_text, encoding="utf-8", newline="\n")


def _t0951_pm_update(home: Path, source: Path) -> subprocess.CompletedProcess:
    from _manager_home import known_ctx_env
    return subprocess.run(
        [sys.executable, str(home / ".project_manager/tools/pm_update.py"), "--from", str(source)],
        cwd=home, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300, env=known_ctx_env({**os.environ, "PM_NONINTERACTIVE": "1"},
                                       native_bin=home.parent / "known-ctx-native"))


def test_stale_sibling_copy_converges_in_one_update(tmp_path):
    """옛 rev 의 형제 사본 하나는 한 번의 갱신으로 상류와 같아지고 강등 알림이 없다 (AT-022).

    전달 프로세스는 엔진 모듈을 적재하지 않고 옮기며, 완료 프로세스는 이미 맞춰진 사본만 적재한다
    — 옛 사본을 검증 적재할 일이 없으므로 버전 불일치 알림도 없다."""
    from _manager_home import make_committed_source, make_pm_home
    source = make_committed_source(tmp_path / "source")
    home = make_pm_home(tmp_path / "home", upstream=source, source=source)
    stale = home / ".project_manager/tools/pm_import.py"
    _t0951_set_engine_rev(stale, "v1.7.13")

    run = _t0951_pm_update(home, source)

    assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"
    assert _T0951_SKEW_NOTICE not in run.stdout + run.stderr
    assert stale.read_bytes() == (source / ".project_manager/tools/pm_import.py").read_bytes()


@pytest.fixture(autouse=True)
def _stub_install_model_listing(monkeypatch):
    """공유 package의 조회 대역을 테스트마다 복원한다."""
    monkeypatch.setattr(import_fill, "_real_models_runner", lambda source=None: (False, []))
