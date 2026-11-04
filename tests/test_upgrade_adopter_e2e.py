"""업그레이드-채택자 e2e 검사 — 옛 버전 디스크 + add-harness 구성 (T-0582 · 기계층).

## 이 파일이 있는 이유
v1.6.1 채택자 제보 중 네 건(추가 하네스 절 소실·어댑터 동결·토큰 회귀·reconcile 파괴)은 **기존 채택자가
업그레이드할 때만** 발현한다 — 디스크에 옛 버전 데이터(옛 추가 하네스 마커·add-harness 이력·설치 시점에
굳은 치환 결과)가 있어야 성립하는 조건이라, 깨끗한 디렉토리만 보는 fresh-adopter 검사
(`test_fresh_adopter_e2e.py`)는 이 클래스를 구조적으로 못 본다. 프레임워크 자신(adopter#0)도
add-harness 를 안 쓰는 구성이라 도그푸딩으로도 안 걸린다([[dogfooding-blind-spot-adopter-shape]]).

## 픽스처 모델 — 현행 엔진 + 옛 버전 데이터
옛 태그를 checkout 해 설치하는 대신 **현행 설치 + 상태 주입**을 쓴다(T-0582 결정): 발현 조건은
"디스크에 남은 옛 데이터" 지 "옛 코드" 가 아니고, 주입 방식이라야 픽스처가 코드로 자명하다.
`_build_upgraded_adopter` 가 셋을 주입한다 — ① 옛 버전 추가 하네스 절(옛 마커 리터럴 + 엔진 행 부재)
② add-harness 추가 하네스 어댑터의 설치-시점 동결 사본 ③ 설치 시 값으로 굳은 치환 결과.

채택자 트리의 **엔진 사본은 canonical 로 먼저 승격**한다(`_prime_engine_to_canonical`). pm_import 는
`templates/<variant>/` 사본에서 복사하는데 그 사본은 묶음 중 stale 할 수 있어, 승격이 없으면 이
검사가 "현행 엔진이 옛 디스크를 어떻게 다루나" 가 아니라 "templates 가 언제 동기됐나" 를 재는
측정기가 된다.

## 시나리오
- **S1** 추가 하네스 절 생존 — 옛 마커 채택자가 동기 후에도 절을 갖고, 마커가 현행 리터럴로 일치하며,
  다음 동기가 멱등이다.
- **S2** frozen 가시성 — 추가 하네스 어댑터 엔진 파일의 동결이 **조용히** 지나가지 않는다(상류와 일치
  하거나, 최소한 loud·actionable 하게 드러난다).
- **S3** 토큰 안정성 — 동기 후 채택자 가시 문서에 render 토큰이 없고, 소비 시점 소유 템플릿은
  토큰을 유지하며, 재동기가 진동 0 이다.
- **S4** reconcile 절차 안전 — 스킬이 지시하는 manifest 선-cp 절차와 엔진 단독 경로 둘 다에서
  추가 하네스 절이 조용히 사라지지 않는다.

**기계층이다** — 로컬 파일시스템 + subprocess(pm_import·pm_update)만 구동한다(라이브 LLM·네트워크
0·결정적). 실 LLM 이 문서를 읽고 운영하는 런타임 검사는 이 파일의 관심사가 아니다.
"""
from __future__ import annotations

from pm_engine.app import import_fill
from pm_engine.domain import install_rules

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from _manager_home import _known_ctx_installation
from _git_fixture import init_git_repo

from _harness_matrix import HARNESSES
# fresh-adopter 검사의 헬퍼를 그대로 쓴다(판정 사본 금지) — `_snapshot_tree` 는 산출물 트리
#   컴포넌트(`__pycache__`·`.git`·`.pm_import_backups`) 제외까지 이미 담고 있다.
from test_fresh_adopter_e2e import _load_pm_import, _snapshot_tree
# 픽스처 기능 삽입 규약은 디스패처 검사가 소유한다(사본 금지) — 이 파일은 그것을 상류에
#   심어 "코드 변경만으로 전파되나" 를 본다.
from test_codex_hook_dispatch import (
    FIXTURE_FEATURE_ENVELOPE, dispatcher_source_with_fixture_feature,
)
from _win_skip import posix_bash_supported
from _fake_opencode import OLLAMA_LISTING, install_fake_opencode, path_with
from _engine_tree import engine_python_files

import ast
from pm_engine.app import add_harness
from pm_engine.app import hook_sets
from pm_engine.domain import adapter_specs
from pm_engine.infra import pm_paths
from pm_engine.cli import import_cmd

pytestmark = pytest.mark.usefixtures("known_ctx_io")

REPO = Path(__file__).resolve().parents[1]
# 설치·추가를 실행하는 엔진 Home(이 checkout).
ENGINE_HOME = pm_paths.home_at(REPO)


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
    """현재 소스로 설치한 opencode 채택자 — 새 갱신기가 이미 설치된 경우."""
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


# 다음 버전 렌더러만 아는 토큰 — 설치된 렌더러는 이 토큰을 미해소로 보고 렌더를 거부한다.
_NEXT_GEN_TOKEN = "{{NEXT_GEN_MARK}}"


def _teach_renderer_next_gen_token(pm_render_path: Path) -> None:
    """`render_adapter` 본문 첫 문장으로 다음 버전 토큰 치환을 넣는다(docstring 바로 뒤)."""
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
    도는 완료 프로세스의 새 렌더러가 하므로, 설치 렌더러가 모르는 다음 버전 토큰도 첫 실행에서
    그려진다. 두 번째 실행은 변경 0 확인이다 — 첫 실행이 완료 단계를 끝내지 못했다면 여기서 변경이
    난다 (T-0951 AT-002)."""
    from _manager_home import _git, make_committed_source, make_pm_home
    source = make_committed_source(tmp_path / "source")
    home = make_pm_home(tmp_path / "home", upstream=source, source=source)
    card_rel = Path(".claude") / "agents" / "architect.md"
    _teach_renderer_next_gen_token(source / ".project_manager" / "tools" / "pm_engine" / "app" / "adapter_render.py")
    template = source / card_rel
    template.write_text(template.read_text(encoding="utf-8")
                        + f"\n다음 버전 표시: {_NEXT_GEN_TOKEN}\n", encoding="utf-8")
    _git("-C", str(source), "commit", "-qam", "next-gen renderer token")
    assert "next-gen" not in (home / card_rel).read_text(encoding="utf-8")

    run = _t0927_direct_update(home)

    assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"
    rendered = (home / card_rel).read_text(encoding="utf-8")
    assert "다음 버전 표시: next-gen" in rendered and _NEXT_GEN_TOKEN not in rendered

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

# 제보 채택자 구성(claude host + add-harness 추가 하네스)을 그대로 재현한다. host 쪽을 곱하지 않는 이유:
#   이 파일이 검증하는 건 *업그레이드 상태 처리*(마커 버전·치환 잔재·절차 안전)라 host 하네스와
#   독립이고, 추가 하네스 방향별 동기 채널 검사는 T-0574 통과 조건(`test_fresh_adopter_e2e`)가 전 순서쌍으로
#   이미 돈다. 추가 하네스 쪽만 파생으로 곱해 하네스별 판 shape 차이(엔진 행 1건 vs 디렉토리 포함 4건)를 태운다.
_HOST_HARNESS = "claude"
_ADDED_HARNESS_HARNESSES = tuple(harness for harness in HARNESSES if harness != _HOST_HARNESS)
# S1·S3·S4 는 단일 구성으로 충분하다(조합 곱셈 비용 대비 판정력 증가 0) — 제보 그대로의 추가 하네스.
_REPORTED_ADDED_HARNESS = "codex"

assert _ADDED_HARNESS_HARNESSES, "추가 하네스 후보 0 — HARNESSES 파생이 퇴화했다(공허 파라미터)"
assert _REPORTED_ADDED_HARNESS in _ADDED_HARNESS_HARNESSES, (
    f"제보 구성 추가 하네스({_REPORTED_ADDED_HARNESS})가 파생 목록에 없다: {_ADDED_HARNESS_HARNESSES}")

# pm:data-literal:begin
# 아래 세 리터럴은 **채택자 디스크에 기록된 데이터**다 — engine.manifest 안에 실제로 적힌 바이트이지
# 이 소스의 산문이 아니다. 엔진 상수(`manifest_rules._ADDED_HARNESS_MANIFEST_BEGIN` 등)를 참조하면 리터럴이 통째로
# 바뀌어도 이 검사가 green 이라(참조-only 테스트가 추가 하네스 절 소실을 30 여 릴리즈 살린 직접 원인)
# 여기엔 문자열을 직접 박는다. 마커 버전이 또 바뀌면 이 파일이 red 로 알린다.
_LEGACY_ADDED_HARNESS_BEGIN = "# >>> pm add-harness guest @render (local·pm_update-preserved·T-0456) >>>"
_PRE_RENAME_ADDED_HARNESS_BEGIN = "# >>> pm add-harness guest @render (local·pm_update-preserved) >>>"
_OLD_ADDED_HARNESS_END = "# <<< pm add-harness guest @render (local) <<<"
_CURRENT_ADDED_HARNESS_BEGIN = "# >>> pm add-harness @render (local·pm_update-preserved) >>>"
_ADDED_HARNESS_END_MARKER = "# <<< pm add-harness @render (local) <<<"
# pm:data-literal:end

_ADOPTER_NAME = "Upgrade Adopter"
_ADOPTER_MANIFEST_REL = ".project_manager/engine.manifest"

# 설치 시점 사본으로 굳은 추가 하네스 어댑터 엔진 파일의 본문 — 동기가 실제로 상류 값을 덮는지 판정하는 표시.
_STALE_ADDED_HARNESS_BODY = "# 설치 시점 사본으로 동결된 채택자 파일 (업그레이드 검사 픽스처)\n"
# 설치 시 `{{DATE}}` 가 값으로 굳었던 경우(T-0578 착지 전 pm_import 거동)의 재현값.
_FROZEN_INSTALL_DATE = "2025-11-03"
# 채택자가 손으로 유지하던 치환 결과(제보 구성) — 상류 산문과 겹치지 않는 픽스처 소유 문장.
_SUBSTITUTED_README_LINE = f"{_ADOPTER_NAME} 프로젝트의 비-코드 산출물 모음 (설치 시 치환된 옛 본문)."

# 갱신이 옛 표시를 옮길 때 내는 안내(조용한 변환 금지)의 고정 부분.
_MARKER_MIGRATION_NOTICE = "추가 하네스 구획 표시"

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


def _added_harness_manifest_block(dest: Path) -> str | None:
    """engine.manifest 의 add-harness 추가 하네스 절(마커 포함) — 어느 버전 마커든 인식, 부재면 None.

    엔진 파서를 부르지 않고 리터럴로 직접 훑는다 — 엔진과 같은 함수를 쓰면 마커 리터럴이 통째로
    바뀌어도 검사가 함께 따라가 결함을 못 본다(이 검사가 존재하는 이유)."""
    text = _manifest_text(dest)
    for begin, end_marker in ((_CURRENT_ADDED_HARNESS_BEGIN, _ADDED_HARNESS_END_MARKER),
                              (_PRE_RENAME_ADDED_HARNESS_BEGIN, _OLD_ADDED_HARNESS_END),
                              (_LEGACY_ADDED_HARNESS_BEGIN, _OLD_ADDED_HARNESS_END)):
        if begin in text and end_marker in text:
            start = text.index(begin)
            end = text.index(end_marker, start) + len(end_marker)
            return text[start:end]
    return None


def _added_harness_rows(block: str) -> list[str]:
    """추가 하네스 절의 등재 행(주석·빈 줄 제외)."""
    return [line.strip() for line in block.splitlines()
            if line.strip() and not line.strip().startswith("#")]


def _added_harness_engine_rows(block: str) -> list[str]:
    """추가 하네스 절의 **엔진 행** — `@render` 없는 행(update 채널 소유·byte-copy 대상)."""
    return [row for row in _added_harness_rows(block) if "@render" not in row]


def _added_harness_render_rows(block: str) -> list[str]:
    """추가 하네스 절의 **어댑터 렌더물 행** — add-harness refresh 소유(update 는 안 건드린다)."""
    return [row for row in _added_harness_rows(block) if "@render" in row]


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
    공용 열거 함수 우회(재귀 tree-walk) 없이 같은 판정을 낸다."""
    return sorted(path for path in snapshot if path == rel or path.startswith(rel + "/"))


def _frozen_added_harness_files(dest: Path) -> list[str]:
    """설치 시점 사본으로 동결된 채로 남은 파일의 relpath — 픽스처 표시로 판정한다.

    복사본이 백업 디렉토리(`.pm_import_backups`)를 이미 뺀다 — add-harness 가 남긴 백업 사본까지
    세면 동기가 끝난 뒤에도 동결이 남은 것처럼 보인다."""
    frozen_body = _STALE_ADDED_HARNESS_BODY.encode("utf-8")
    return sorted(rel for rel, payload in _snapshot_tree(dest).items() if payload == frozen_body)


def _prime_engine_to_canonical(dest: Path) -> None:
    """채택자 엔진 사본을 canonical(루트) 판으로 승격 — 이 검사의 전제 조건.

    pm_import 는 `templates/<variant>/` 사본을 깔고 그 사본은 묶음 중 stale 할 수 있다. 승격이 없으면
    시나리오가 구동하는 pm_update 가 **옛 엔진**이라, 검사가 현행 엔진의 업그레이드 처리 대신
    templates 동기 시점을 측정하게 된다. 엔진 도구는 manifest bare 등재(root-sourced)라 정상 동기
    한 번으로 전 도구가 **함께** canonical 이 된다(부분 승격에 따른 형제 모듈 skew 없음)."""
    _run_pm_update(dest)
    for rel in (".project_manager/tools/pm_update.py", ".project_manager/tools/board.py"):
        assert (dest / rel).read_bytes() == (REPO / rel).read_bytes(), (
            f"엔진 승격 실패 — {rel} 가 canonical 과 다르다(검사가 옛 엔진을 검증하게 된다)")


def _inject_old_added_harness_section(dest: Path, begin: str) -> None:
    """현행 추가 하네스 절을 **옛 표시 절**로 되돌린다 — 주어진 옛 시작 표시와 옛 끝 표시.

    v1.4.5 시작 표시(`_LEGACY_ADDED_HARNESS_BEGIN`)의 절은 그때 없던 엔진 행(비-`@render`)도 지운다. 그 결과가
    곧 제보 채택자의 디스크 경우이고, 표시 이관과 엔진 행 파생 백필을 함께 태운다.
    옛 표시 글 `pm add-harness guest` 를 `pm add-harness` 로 바꾸기 전 시작 표시(`_PRE_RENAME_ADDED_HARNESS_BEGIN`)의
    절은 표시만 옛 글자다."""
    block = _added_harness_manifest_block(dest)
    assert block is not None, "픽스처 오류 — add-harness 직후인데 추가 하네스 절이 없다"
    rows = _added_harness_render_rows(block) if begin == _LEGACY_ADDED_HARNESS_BEGIN else _added_harness_rows(block)
    old_block = "\n".join([begin, *rows, _OLD_ADDED_HARNESS_END])
    text = _manifest_text(dest)
    _manifest_path(dest).write_text(text.replace(block, old_block), encoding="utf-8", newline="\n")
    assert begin in _manifest_text(dest)
    assert _CURRENT_ADDED_HARNESS_BEGIN not in _manifest_text(dest)


def _freeze_added_harness_engine_files(dest: Path, block: str) -> list[str]:
    """추가 하네스 어댑터 **엔진 파일**을 설치 시점 사본으로 동결한다 — 동기 채널 검증의 대조군.

    렌더물(`@render` 행)은 add-harness refresh 소유라 동결 대상이 아니다(update 가 안 건드리는 게
    정상). 동결 좌표는 현행 절의 엔진 행에서 파생한다 — 하네스별 판마다 손 열거하지 않는다."""
    snapshot = _snapshot_tree(dest)
    frozen = []
    for row in _added_harness_engine_rows(block):
        for rel in _files_under(snapshot, _row_path(row)):
            (dest / rel).write_text(_STALE_ADDED_HARNESS_BODY, encoding="utf-8", newline="\n")
            frozen.append(rel)
    return sorted(frozen)


def _inject_substituted_documents(dest: Path) -> None:
    """설치 시 값으로 굳은 치환 결과를 주입한다 — T-0578 착지 전 채택자 디스크 경우.

    두 방향을 함께 만든다: 채택자 가시 문서(`wiki/README.md`)에 프로젝트명이 박힌 옛 본문,
    소비 시점 소유 템플릿 2종(`pm_state.template.md`·`domain/_template.md`)에 설치일로 굳은
    `{{DATE}}`. 전자는 상류가 토큰을 지워 일치해야 하고, 후자는 토큰-form 으로 되돌아와야 한다."""
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
        tmp_path: Path, *, added_harness_marker_generation: str, substituted: bool,
        host: str = _HOST_HARNESS, added_harness: str = _REPORTED_ADDED_HARNESS) -> Path:
    """옛 버전 상태를 주입한 업그레이드 채택자 트리를 만들고 그 경로를 낸다.

    ``added_harness_marker_generation``
      ``"legacy"`` — v1.4.5 버전 추가 하네스 절(옛 마커 리터럴 + 엔진 행 부재).
      ``"pre_rename"`` — 옛 표시 글 `pm add-harness guest` 를 `pm add-harness` 로 바꾸기 전(v1.8.3 까지)의 절(옛 표시 + 엔진 행).
      ``"current"`` — 현행 add-harness 가 쓴 그대로의 절(대조군·멱등 기준).
    ``substituted`` — 설치 시 값으로 굳은 치환 결과(README 프로젝트명·template 실날짜) 주입 여부.

    추가 하네스 어댑터 엔진 파일은 두 버전 모두 설치 시점 사본으로 동결한다 — 제보의 실제 증상이
    "설치 이후 상류 변경을 한 번도 못 받았다" 이기 때문이다.
    """
    assert added_harness_marker_generation in ("legacy", "pre_rename", "current"), added_harness_marker_generation
    dest = tmp_path / f"upgraded-{host}-{added_harness}-{added_harness_marker_generation}"

    # ① 현행 엔진으로 설치 + 엔진 사본 canonical 승격(검사 전제).
    assert import_cmd.main(
        pm_paths.home_at(_PM_IMPORT.REPO),
        ["--new", str(dest), "--harness", host, "--name", _ADOPTER_NAME, "--fill", "manual"],
    ) == 0, f"{host} import 실패"
    _prime_engine_to_canonical(dest)

    # ② add-harness 이력 — 추가 하네스 절 등재 + 추가 하네스 어댑터 실파일.
    plan = add_harness.add_harness(ENGINE_HOME, dest, added_harness, dry_run=False, source_root=REPO)
    assert plan, f"{host}→{added_harness}: add-harness plan 이 비어 있다(픽스처 무효)"
    block = _added_harness_manifest_block(dest)
    assert block is not None, f"{host}→{added_harness}: add-harness 가 추가 하네스 절을 등재하지 않았다"

    # ③ 추가 하네스 어댑터 엔진 파일 동결(설치 시점 사본).
    frozen = _freeze_added_harness_engine_files(dest, block)
    assert frozen, f"{host}→{added_harness}: 동결할 추가 하네스 엔진 파일 0(공허 픽스처)"

    # ④ 옛 버전 추가 하네스 절로 되돌리기.
    if added_harness_marker_generation == "legacy":
        _inject_old_added_harness_section(dest, _LEGACY_ADDED_HARNESS_BEGIN)
    elif added_harness_marker_generation == "pre_rename":
        _inject_old_added_harness_section(dest, _PRE_RENAME_ADDED_HARNESS_BEGIN)

    # ⑤ 설치 시 굳은 치환 결과.
    if substituted:
        _inject_substituted_documents(dest)
    return dest


# ── S1 추가 하네스 절 생존 — 옛 마커 채택자의 동기 ──────────────────────────────────


def test_upgrade_adopter_added_harness_section_survives_sync_and_marker_converges(tmp_path):
    """옛 마커 채택자가 동기 후에도 추가 하네스 절을 갖고, 마커가 현행 리터럴로 일치하며, 재동기가 멱등이다.

    결함 구성: 추가 하네스 절 경계 마커 리터럴이 v1.5.0 구간에 바뀌어, 그 전에 `add-harness` 를 쓴
    채택자가 `pm_update` 를 돌리면 읽기 쪽이 절을 못 알아보고 **경고 없이** 통째로 지웠다
    (렌더/overlay 스캔 커버리지가 그 시점에 끊긴다). 옛 버전(대조군: 현행 버전) 절을 디스크에 두고
    실 CLI 동기를 태워 절 생존·마커 일치·멱등을 못박는다. 옛 표시 두 가지(v1.4.5, 옛 표시 글 `pm add-harness guest` 를 `pm add-harness` 로 바꾸기 전)와 현행 표시를 한 시험 안에서 차례로 돈다.
    """
    for generation in ("legacy", "pre_rename", "current"):
        generation_root = tmp_path / generation
        generation_root.mkdir()
        _check_added_harness_section_generation(generation_root, generation)


def _check_added_harness_section_generation(tmp_path: Path, generation: str) -> None:
    dest = _build_upgraded_adopter(
        tmp_path, added_harness_marker_generation=generation, substituted=True)
    before = _added_harness_manifest_block(dest)
    assert before is not None
    render_rows_before = _added_harness_render_rows(before)
    assert render_rows_before, "픽스처 오류 — 추가 하네스 렌더물 행 0"

    result = _run_pm_update(dest)

    after = _added_harness_manifest_block(dest)
    assert after is not None, (
        f"{generation} 버전 추가 하네스 절이 동기 후 사라졌다(조용한 소실) — 절 안의 `@render`·엔진 행이 "
        f"어느 채널에도 안 남는다.\n--- stdout ---\n{result.stdout}")
    # 쓰기는 항상 현행 리터럴 단일 버전이다 — 옛 리터럴이 남으면 다음 버전에서 같은 사고가 반복된다.
    assert _CURRENT_ADDED_HARNESS_BEGIN in after, f"추가 하네스 절 시작 마커가 현행 리터럴이 아니다:\n{after}"
    for old_marker in (_LEGACY_ADDED_HARNESS_BEGIN, _PRE_RENAME_ADDED_HARNESS_BEGIN, _OLD_ADDED_HARNESS_END):
        assert old_marker not in _manifest_text(dest), (
            f"옛 표시가 manifest 에 잔존 — 1 회 이관이 안 일어났다: {old_marker}")
    # 렌더물 행은 add-harness refresh 소유라 동기가 내용을 바꾸지 않는다(경로 집합 보존).
    assert {_row_path(row) for row in _added_harness_render_rows(after)} == \
        {_row_path(row) for row in render_rows_before}, (
            f"추가 하네스 렌더물 행 집합이 동기로 변형됐다\n이전: {render_rows_before}\n이후: {after}")

    if generation in ("legacy", "pre_rename"):
        assert _MARKER_MIGRATION_NOTICE in result.stdout, (
            "옛 리터럴을 현행으로 바꿨는데 표기가 없다(조용한 변환 금지)\n"
            f"--- stdout ---\n{result.stdout}")
    else:
        assert _MARKER_MIGRATION_NOTICE not in result.stdout, (
            "현행 버전인데 마이그레이션 표기가 났다(멱등 위반·헛 변환)\n"
            f"--- stdout ---\n{result.stdout}")

    # 다음 동기는 멱등 — manifest 가 바이트로 안정(매 sync 재기록 churn 0).
    manifest_after_first = _manifest_text(dest)
    _run_pm_update(dest)
    assert _manifest_text(dest) == manifest_after_first, (
        "재동기가 engine.manifest 를 또 고쳤다(멱등 위반·영구 churn)")


# ── S2 frozen 가시성 — 추가 하네스 어댑터 엔진 파일 ────────────────────────────────


@pytest.mark.parametrize("added_harness", _ADDED_HARNESS_HARNESSES)
def test_upgrade_adopter_added_harness_adapter_is_never_silently_frozen(tmp_path, added_harness):
    """설치 시점 사본으로 동결된 추가 하네스 어댑터 엔진 파일이 **조용히** 동결로 남지 않는다.

    두 결과 중 하나만 인정한다 — (a) 동기 채널이 상류 값으로 일치시키거나, (b) 일치가 없다면
    출력이 동결 좌표를 짚고 복구 채널(`add-harness`)을 안내한다. 금지되는 건 셋째 경우, 즉 계획
    0 건·경고 0 건으로 지나가 채택자가 자기 어댑터가 몇 달째 옛 사본인 걸 모르는 상태다(제보의
    실제 증상: 설치 시점 `.codex/**` 가 상류 6 커밋을 못 받은 채 조용히 동결).

    추가 하네스 절 자체는 어느 쪽 결과에서도 생존해야 한다 — 절이 사라지면 다음 동기부터는 동결 사실을
    드러낼 근거조차 없다.
    """
    dest = _build_upgraded_adopter(
        tmp_path, added_harness_marker_generation="legacy", substituted=True, added_harness=added_harness)
    frozen_before = _frozen_added_harness_files(dest)
    assert frozen_before, f"{added_harness}: 픽스처가 동결 사본을 안 만들었다(공허 검사)"

    result = _run_pm_update(dest)
    output = _combined_output(result)

    block = _added_harness_manifest_block(dest)
    assert block is not None, (
        f"{added_harness}: 동기가 추가 하네스 절을 지웠다 — 동결 드러냄의 근거가 사라진다\n{output}")

    frozen_after = _frozen_added_harness_files(dest)
    if frozen_after:
        # (b) 아직 동기 채널이 없다면 최소한 loud·actionable 이어야 한다.
        assert any(rel in output for rel in frozen_after), (
            f"{added_harness}: 동결 파일 {frozen_after} 이 그대로인데 출력이 좌표를 짚지 않는다"
            f"(조용한 동결)\n{output}")
        assert "add-harness" in output, (
            f"{added_harness}: 동결을 알렸으나 복구 채널 안내가 없다(actionable 아님)\n{output}")
        return

    # (a) 일치했다면 상류 값과 **byte 일치** 여야 한다 — 표시만 지운 헛 동기를 배제한다.
    engine_rows = _added_harness_engine_rows(block)
    assert engine_rows, (
        f"{added_harness}: 파일은 갱신됐는데 추가 하네스 절에 엔진 행이 없다 — 다음 동기의 소유 근거가 없다\n{block}")
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
            assert source.is_file(), f"{added_harness}: 상류 대응 부재 — {rel} → {source}"
            assert snapshot[rel] == source.read_bytes(), (
                f"{added_harness}: {rel} 이 상류와 byte 불일치(동기가 값을 안 가져왔다)")
            compared += 1
    assert compared, f"{added_harness}: 상류와 대조한 파일 0(공허 통과)"


# ── S3 토큰 안정성 — 치환 소유권과 진동 ──────────────────────────────────────


@pytest.mark.parametrize("substituted", (True, False))
def test_upgrade_adopter_render_tokens_settle_without_oscillation(tmp_path, substituted):
    """동기가 채택자 가시 문서에 토큰을 되돌리지 않고, 소비 시점 소유 템플릿은 토큰을 유지한다.

    결함 구성: render 토큰을 본문에 담은 wiki 파일이 manifest 에 bare 등재돼, byte-copy 동기가
    채택자의 치환된 문서를 미치환 토큰으로 되돌렸다(실측: 동기 후 `wiki/README.md` 가
    `{{PROJECT_NAME}}` 로 회귀). 반대편 반쪽은 설치가 값으로 굳혀 버린 템플릿이다 —
    `pm_state.template.md`·`domain/_template.md` 의 `{{DATE}}` 는 **소비 시점** 소유라 채택자
    디스크에 토큰-form 으로 있어야 정상이고, 굳어 있으면 새 산출물이 설치일을 물려받는다.

    두 방향을 한 트리에서 함께 보고, 재동기 진동 0(같은 upstream 을 두 번 받으면 트리가 바이트로
    안정)까지 확인한다 — 진동은 소유권이 두 주체로 갈렸을 때의 증상이라 1 회 관측으로는 안 보인다.
    """
    dest = _build_upgraded_adopter(
        tmp_path, added_harness_marker_generation="legacy", substituted=substituted)
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


def test_upgrade_adopter_skill_manifest_reconcile_never_silently_drops_added_harness(tmp_path):
    """스킬이 지시하는 manifest reconcile 절차를 재연해도 추가 하네스 절이 **조용히** 사라지지 않는다.

    `pm-update` 스킬은 동기 전에 채택자 manifest 를 상류 하네스별 판 manifest 로 덮는 선-cp 를 지시한다
    (새 등재 항목이 채택자에 도달하게 하는 목적). 그 cp 는 파일 통째 덮기라 add-harness 채택자의
    추가 하네스 절도 함께 지운다 — 절이 사라진 뒤엔 마이그레이션(T-0571)도 살릴 대상이 없다.

    인정하는 결과는 둘이다 — (a) 절차를 그대로 밟아도 추가 하네스 절이 생존하거나, (b) 절이 사라졌다면
    엔진이 그 사실을 loud 하게 짚고 복구(`add-harness` 재실행)를 안내하고, 그 복구가 실제로 절을
    되살린다. 금지되는 건 절이 조용히 사라져 채택자가 커버리지 상실을 모르는 상태다. **(b) 로
    green 인 동안은 스킬 본문이 개선 대상**이라는 신호다(추가 하네스 절이 있는 채택자에겐 cp 대신 엔진
    reconcile 경로 안내 — 그 경로가 실재함은 아래 테스트가 실측한다).
    """
    dest = _build_upgraded_adopter(
        tmp_path, added_harness_marker_generation="legacy", substituted=True)
    added_harness_paths_before = [_row_path(row) for row in _added_harness_rows(_added_harness_manifest_block(dest))]
    assert added_harness_paths_before, "픽스처 오류 — 추가 하네스 절 등재 행 0"

    # 채택자가 실제로 읽는 사본(설치된 스킬)에서 절차를 뽑는다 — 소스 트리가 아니라 출하물이 진실.
    skill = dest / ".claude" / "skills" / "pm-update" / "SKILL.md"
    assert skill.is_file(), f"채택자 트리에 pm-update 스킬 부재: {skill}"
    copy_source = _skill_manifest_copy_source(skill.read_text(encoding="utf-8"), host=_HOST_HARNESS)
    if copy_source is not None:
        _manifest_path(dest).write_bytes(copy_source.read_bytes())

    result = _run_pm_update(dest)
    output = _combined_output(result)

    if _added_harness_manifest_block(dest) is not None:
        return  # (a) 절차가 절을 보존한다 — 더 볼 것 없음.

    # (b) 절이 사라졌다면 loud·actionable 이어야 하고, 안내된 복구가 실제로 작동해야 한다.
    assert any(path in output for path in added_harness_paths_before), (
        f"추가 하네스 절이 절차 중 사라졌는데 출력이 그 좌표({added_harness_paths_before})를 짚지 않는다"
        f"(조용한 소실)\n{output}")
    assert "add-harness" in output, (
        f"추가 하네스 절 소실을 알렸으나 복구 채널 안내가 없다(actionable 아님)\n{output}")
    add_harness.add_harness(ENGINE_HOME, dest, _REPORTED_ADDED_HARNESS, dry_run=False, source_root=REPO)
    assert _added_harness_manifest_block(dest) is not None, (
        "안내대로 add-harness 를 재실행했는데 추가 하네스 절이 복구되지 않는다 — 안내가 거짓이다")


def test_upgrade_adopter_engine_reconciles_stale_manifest_without_manual_copy(tmp_path):
    """엔진 단독 동기가 옛 manifest 를 상류 구성으로 되맞추고 그 항목까지 같은 실행에서 전파한다.

    스킬의 선-cp 는 "새 항목이 채택자에 도달하려면 dest manifest 가 먼저 알아야 한다" 를 근거로
    한다. 그 전제가 아직 참인지 실측한다 — 등재 행 하나를 지우고 그 파일을 옛 내용으로 되돌린
    채택자에서 `pm_update` **한 번**이 (a) 행 복구 (b) 파일 전파 (c) 추가 하네스 절 보존을 함께 해내면,
    추가 하네스 절이 있는 채택자에게 선-cp 는 불필요하며(수동 복사 없이 같은 결과) 위험만 남는다.

    이 테스트가 red 로 바뀌면 선-cp 지시는 다시 필요해진 것이다 — 그때는 위 재연 테스트의 판정과
    함께 스킬 본문을 재검토해야 한다.
    """
    dest = _build_upgraded_adopter(
        tmp_path, added_harness_marker_generation="legacy", substituted=True)

    # 옛 manifest 재현 — core 등재 행 하나를 지우고 그 문서를 옛 내용으로 되돌린다. 대상은 엔진이
    #   실행 중 import 하는 도구가 아니라 문서 행으로 고른다(도구를 부수면 판정 대신 크래시를 본다).
    assert _added_harness_manifest_block(dest) is not None, "픽스처 오류 — 추가 하네스 절 부재"
    core_text = _manifest_text(dest).split(_LEGACY_ADDED_HARNESS_BEGIN)[0]
    victim = next(
        (line.strip() for line in core_text.splitlines()
         if line.strip().endswith(".md") and "@" not in line and (dest / line.strip()).is_file()),
        None)
    assert victim, "core 등재 문서 행을 못 찾았다(픽스처 전제 붕괴)"
    text = _manifest_text(dest)
    dropped = "\n".join(line for line in text.splitlines() if line.strip() != victim) + "\n"
    assert dropped != text, f"{victim} 행 제거가 무효(공허 픽스처)"
    _manifest_path(dest).write_text(dropped, encoding="utf-8", newline="\n")
    (dest / victim).write_text(_STALE_ADDED_HARNESS_BODY, encoding="utf-8", newline="\n")

    _run_pm_update(dest)

    restored = [line.strip() for line in _manifest_text(dest).splitlines()]
    assert victim in restored, (
        f"엔진 단독 동기가 옛 manifest 의 누락 행({victim})을 복구하지 못했다 — 선-cp 없이는 "
        "새 등재 항목이 채택자에 도달하지 않는다")
    assert (dest / victim).read_text(encoding="utf-8") != _STALE_ADDED_HARNESS_BODY, (
        f"{victim} 이 같은 실행에서 전파되지 않았다 — 채택자는 동기를 두 번 돌려야 한다")
    assert _added_harness_manifest_block(dest) is not None, (
        "엔진 단독 경로가 추가 하네스 절을 지웠다 — 안전한 대안 경로가 없다")


# ── S5 instance-owned config RUN1/RUN2 완료 통과 조건 (T-0591) ────────────────

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


def test_upgrade_adopter_zero_change_run_blocks_until_adapter_accept(tmp_path):
    """엔진이 최신인 사본의 zero-change 실행이 기록 없는 어댑터 설정에서 red → accept → check green 전체 경로."""
    dest = tmp_path / "legacy-adapter-adopter"
    assert import_cmd.main(pm_paths.home_at(_PM_IMPORT.REPO), [
        "--new", str(dest), "--harness", "codex", "--name", _ADOPTER_NAME,
        "--fill", "manual",
    ]) == 0
    # 먼저 나머지 엔진을 canonical과 맞춰 RUN2의 엔진 change를 정확히 0으로 만든다.
    _prime_engine_to_canonical(dest)

    hooks = dest / _ADAPTER_HOOKS_REL
    hooks.write_text(_LEGACY_BLOCKING_HOOKS, encoding="utf-8", newline="\n")
    records = dest / ".project_manager" / "adapter_baseline.json"
    records.unlink(missing_ok=True)  # 옛 버전 설치: durable 기록 부재.

    run2 = _run_adopter_tool(dest, "pm_update.py", "--from", str(REPO))
    assert run2.returncode == 1, (
        f"기록 부재인데 zero-change RUN2가 green\n{run2.stdout}\n{run2.stderr}")
    assert "managed 어댑터 config가 아직 일치하지 않는다" in run2.stderr
    assert "엔진 manifest 변경은 0건" in run2.stderr, \
        "RUN2가 zero-change 경계를 실제로 거치지 않음"
    assert "최신 — 변경 없음" not in run2.stdout
    assert hooks.read_text(encoding="utf-8") == _LEGACY_BLOCKING_HOOKS, \
        "RUN2가 기록 부재 파일을 자동 덮음(비파괴 계약 위반)"

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
    assert "일치 확인" in green_check.stdout


# ── S6 CRLF 표기 dest 의 zero-change 일치 (T-0723) ────────────────────────────
# Windows 채택자(`core.autocrlf=true`)의 dest 는 내용이 상류와 같아도 표기가 CRLF 다. 계획의 최소
# 렌더 분기가 raw bytes 로 대조하던 동안 그 파일들은 매 실행 `update` 로 올라왔다 — 재기록 후에도
# bytes 는 그대로라(쓰기가 dest 표기 보존) 내용이 상하진 않지만, dry-run 이 상시 오탐이고 실제
# drift 가 그 소음에 묻히며 zero-change 경계(RUN2 완료 통과 조건)를 아예 통과하지 못한다.
# LF 환경(Linux)에서 그 경우를 태우려면 dest 표기를 직접 뒤집어야 한다 — 아래 헬퍼가 그 자리다.

_NOTATION_MANAGED_WIKI = (
    ".project_manager/wiki/pm_role.md",
    ".project_manager/wiki/pm_playbook.md",
    ".project_manager/wiki/README.md",
    ".project_manager/wiki/pm_state.template.md",
)


def _renotate_dest_to_crlf(dest: Path, relpaths) -> list[str]:
    """dest 사본을 **내용 그대로** CRLF 표기로 되쓴다 — 조건이 실제로 실렸는지 함께 단언한다.

    선-단언은 "bytes 가 바뀌었나" 가 아니라 **"지금 dest 가 CRLF 인가"** 다. CRLF 체크아웃
    (Windows)에서는 설치 사본이 이미 CRLF 라 변환이 no-op 이고, 그때도 이 검사가 재려는 조건
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
            f"{rel}: dest 가 LF 렌더층과 byte 동일하다 — 이 조건이 시험되지 않는다"
        converted.append(rel)
    assert len(converted) == len(tuple(relpaths)) and converted, "변환 대상 0(공허 픽스처)"
    return converted


def test_upgrade_adopter_crlf_dest_reaches_zero_change_and_keeps_notation(tmp_path):
    """표기만 CRLF 인 dest 는 zero-change 로 일치하고 표기가 보존된다 — 내용 차이는 여전히 update."""
    dest = tmp_path / "crlf-dest-adopter"
    assert import_cmd.main(pm_paths.home_at(_PM_IMPORT.REPO), [
        "--new", str(dest), "--harness", "codex", "--name", _ADOPTER_NAME,
        "--fill", "manual",
    ]) == 0
    _prime_engine_to_canonical(dest)
    # 대조군: 승격 직후는 변경 0 이어야 이 검사가 **CRLF 변환분만** 재는 측정기가 된다.
    settled = _run_pm_update(dest)
    assert "최신 — 변경 없음." in settled.stdout, (
        f"픽스처 전제 붕괴 — 승격 직후 변경이 남아 있다\n{settled.stdout}")

    converted = _renotate_dest_to_crlf(dest, _NOTATION_MANAGED_WIKI)
    before = {rel: (dest / rel).read_bytes() for rel in converted}

    run = _run_pm_update(dest)

    assert "최신 — 변경 없음." in run.stdout, (
        "표기만 CRLF 인 dest 를 변경으로 계획했다 — 내용 무변경 churn 이라 일치를 영영 보고하지 "
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


def test_update_release_skill_cards_pin_zero_change_and_adapter_check_contract():
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
        assert "live-test/main push/tag/GitHub Release" in text, card


# ── S7 진입점이 열린 채택자에 가드 기능이 **코드만으로** 도달하나 (T-0777) ────────
# 이 티켓의 주장은 "이후 기능 추가는 엔진 코드 변경뿐이고 채택자 config 는 다시 안 건드린다" 다.
# 그 주장은 업그레이드 경우에서만 확인된다 — fresh 경우는 진입점이 처음부터 있는지만 본다.

_CODEX_DISPATCHER_REL = ".codex/pm_orch_codex.py"


def _build_codex_framework(tmp_path: Path) -> Path:
    """REPO 로부터 **변경 가능한** codex 프레임워크 소스를 만든다(import + self-update 소스).

    codex 채택자 manifest 가 참조하는 root-상대 경로 전부를 담는다 — 엔진(`.project_manager/`)·
    root `.claude/skills`(`.agents/skills` @source 원본)·`templates/codex/`(어댑터 @source 원본)·
    `.gitattributes`. REPO 를 안 건드리려고 사본에 엔진 mutate 를 가한다(fresh 검사의 opencode
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
    """진입점이 열린 채택자 + 상류 기능 추가 → pm_update 1회로 돈다(채택자 config 무변경)."""
    framework = _build_codex_framework(tmp_path)
    dest = tmp_path / "codex-adopter"
    assert import_cmd.main(pm_paths.home_at(_PM_IMPORT.REPO), [
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
                     in adapter_specs.ADAPTER_HOOK_SET["codex"].entrypoints)
    assert set(installed) == set(declared), "설치 config 와 엔진 선언의 이벤트 집합이 갈렸다"
    for event in declared:
        groups = installed[event]
        expected = "^(Bash|apply_patch|collaborationspawn_agent)$" if event == "PreToolUse" else ".*"
        assert len(groups) == 1 and groups[0]["matcher"] == expected, event
        for key in ("command", "commandWindows"):
            assert _CODEX_DISPATCHER_REL in groups[0]["hooks"][0][key], (event, key)
            assert f"--hook-dispatch {event}" in groups[0]["hooks"][0][key], (event, key)
    assert (dest / _CODEX_DISPATCHER_REL).is_file(), "디스패처가 설치되지 않았다"
    assert hook_sets.judge_adapter_hook_entrypoints(dest, framework, ["codex"]) == [], \
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
    ) == FIXTURE_FEATURE_ENVELOPE, "전파는 됐는데 진입점에서 돌지 않는다"


# ── S8 진입점 집합이 늘어난 릴리즈의 갱신 (T-0806) ────────────────────────────
# 이관 3 이벤트가 진입점 밖에 있던 버전(T-0777)의 채택자가 이 릴리즈를 받는 기간이다.
# 그 창에서 **무편집 채택자는 advisory 0줄**이어야 한다 — pm_update 는 같은 실행에서
# `sync_adapter_configs(write=True)` 로 config 를 교체한 뒤 진입점을 판정하므로, 판정 시점의
# config 는 이미 새 버전이다. 이 순서가 뒤집히면 정상 갱신마다 거짓 소견 3줄이 나간다.

_T0806_MIGRATED_EVENTS = {"SubagentStart": (".*",),
                          "PreCompact": ("^auto$", "^manual$"),
                          "PostCompact": ("^auto$", "^manual$")}


def _pre_t0806_hooks_document(framework: Path) -> dict:
    """T-0806 이전 버전 경우 — 이관 3 이벤트가 진입점 밖 직결 등록으로 남은 config."""
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
    """진입점 집합이 늘어난 릴리즈를 무편집 채택자가 갱신 1회로 일치한다(소견 0줄)."""
    framework = _build_codex_framework(tmp_path)
    hooks_rel = ".codex/hooks.json"
    upstream_hooks = framework / "templates" / "codex" / hooks_rel
    current_bytes = upstream_hooks.read_bytes()
    upstream_hooks.write_text(
        json.dumps(_pre_t0806_hooks_document(framework), ensure_ascii=False, indent=2)
        + "\n", encoding="utf-8", newline="\n")

    dest = tmp_path / "codex-adopter"
    assert import_cmd.main(pm_paths.home_at(_PM_IMPORT.REPO), [
        "--new", str(dest), "--harness", "codex", "--name", _ADOPTER_NAME,
        "--fill", "manual", "--from", str(framework),
    ]) == 0
    # 전제: 이 버전 채택자에는 진입점이 3건 빠져 있다(소견이 뜰 수 있는 상태).
    before = hook_sets.judge_adapter_hook_entrypoints(dest, framework, ["codex"])
    assert {finding.event for finding in before} == set(_T0806_MIGRATED_EVENTS), before

    upstream_hooks.write_bytes(current_bytes)  # 상류를 이 릴리즈 버전으로 되돌린다.
    run = _run_adopter_tool(dest, "pm_update.py", "--from", str(framework))

    assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"
    assert (dest / hooks_rel).read_bytes() == current_bytes, \
        "무편집 managed config 가 새 버전으로 교체되지 않았다"
    assert "훅 진입점 누락" not in run.stderr, \
        f"같은 실행에서 교체된 config 에 거짓 소견이 났다\n{run.stderr}"
    assert hook_sets.judge_adapter_hook_entrypoints(dest, framework, ["codex"]) == []
    # 갱신 뒤 압축 진입점이 실제로 등록 기능을 돌린다(형태 일치 + 동작 확인).
    envelope = _run_shipped_entrypoint(
        dest, "PreCompact",
        {"hook_event_name": "PreCompact", "cwd": str(dest), "model": "m",
         "session_id": "s", "transcript_path": None, "trigger": "auto", "turn_id": "t"})
    assert envelope and "adapter-fallback" not in json.dumps(envelope, ensure_ascii=False)

# ── S8 구표기 conf 채택자의 전환 (dry-run → RUN1 → RUN2 → 교체 → check) ───────
#
# 표기 통일은 채택자 conf 를 깨뜨리는 전환이라, 채택자가 **무엇을 어떻게 고치는지**를 실 CLI 흐름
# 에서 듣지 못하면 전환이 성립하지 않는다. 특히 위험한 창은 두 곳이다:
#   (1) 구표기 `upstream=` 만 있는 채택자의 미리보기는 키를 옮기지 않아 upstream 미해소로 rc=1 로
#       끝난다(쓰는 실행은 키 이름을 먼저 옮긴다) — 그 실행이 안내를 내지 않으면 채택자는 무엇이
#       문제인지 모른다.
#   (2) 엔진을 배달하는 RUN1 은 **구 updater** 가 돈다(새 안내 코드가 아직 그 프로세스에 없다).
#       그래서 공개 절차는 변경 0 RUN2 를 필수로 적고, 그 실행이 안내를 낸다.
# 여기서는 그 흐름을 실 facade/CLI 로 돌려 값으로 잠근다(helper 수동 호출이 아니다).

# 부분 이주 경우 — 렌더 채널이 쓰는 operational 키는 이미 신표기이고, 위임·ctx·upstream 쪽만
# 구표기로 남았다(실 채택자가 안내를 받기 전 상태). 렌더 쪽까지 구표기면 자족 산출물 토큰 가드가
# 먼저 서서 이 시나리오가 재는 **안내 시점**이 가려진다.
_OPERATIONAL_CONF = (
    "project.name=Upgrade Adopter\n"
    "runtime.py=python3\n"
    "test.cmd=python -c \"print('adopter suite')\"\n"
)
# 사용 한도 543210 은 Claude Code 압축 지점 범위(100000~1000000) 안이다 — 옮긴 값이 반영 검사를
#   통과해야 (5) 의 교체 뒤 실행이 설정 파일까지 반영한다(T-0923).
_LEGACY_KEYS_CONF = (
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
    """엔진 파일 하나를 옛 버전으로 되돌린다 — RUN1(배달)/RUN2(변경 0)를 실제로 가르기 위해."""
    stale = dest / ".project_manager" / "tools" / "domain.py"
    stale.write_text(stale.read_text(encoding="utf-8") + "\n# 옛 버전 사본 표시\n",
                     encoding="utf-8")
    return stale


def test_upgrade_adopter_legacy_conf_is_guided_then_replaced_then_checks_run(tmp_path):
    """구표기 conf 채택자: 안내 → 교체 → 검사 동작이 실 CLI 로 닫힌다."""
    dest = tmp_path / "legacy-conf-adopter"
    assert import_cmd.main(pm_paths.home_at(_PM_IMPORT.REPO), [
        "--new", str(dest), "--harness", "claude", "--name", _ADOPTER_NAME,
        "--fill", "manual",
    ]) == 0
    _prime_engine_to_canonical(dest)
    conf = dest / ".project_manager" / "local.conf"
    conf.write_text(f"upstream={REPO}\n" + _OPERATIONAL_CONF + _LEGACY_KEYS_CONF,
                    encoding="utf-8")

    # (1) 구표기 upstream 만 있는 경우의 미리보기 — 쓰지 않는 실행은 키를 옮기지 않아 해소는 실패해도
    #     **안내는 나간다**(쓰는 실행은 갱신 시작 때 키 이름을 먼저 옮긴다).
    unresolved = _raw_pm_update(dest, "--dry-run")
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

    # (4) 교체 전 검사는 멈춘다 — 조용히 엔진 기본값으로 돌지 않는다.
    blocked = _run_adopter_tool(dest, "board.py", "list")
    assert blocked.returncode == 1, blocked.stdout
    assert "구표기 키가 남아 있습니다" in blocked.stderr + blocked.stdout

    # (5) 교체 후 같은 검사가 돈다.
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


def test_public_update_procedure_documents_the_manager_apply_and_into():
    """공개 절차(README)가 관리자 [적용]은 사본 판과 관계없이 최신판까지 올리고, 명령줄에서는 최신판
    갱신기의 `--into <PM 홈>` 으로 옛 사본을 올린다고 적는다. 절차는 미리보기·갱신·변경 0 확인 세 줄이다."""
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    section = readme.split("엔진 갱신은 채택자 루트에서 받는다", 1)[1][:900]

    assert "사본의 판과 관계없이 최신판까지 올린다" in section
    assert "pm_update.py --into <PM 홈>" in section
    assert "변경 0" in section
    assert section.count("./pm-update.sh") >= 3      # dry-run + 갱신 + 변경 0 확인


# ── 옛 홈 자신 행 제거 (T-0919 마이그레이션) ──────────────────────────────────
# 옛 엔진은 홈 자신을 첫 슬롯 행(`slot="."`)으로 등재했다. 그 경로를 지우면서 이미 박힌 행도
# 갱신이 지운다 — 지우기만 하면 관리자 화면에 작업 폴더 `.` 로 계속 보인다(사용자 발견 증상).


def _seed_legacy_home_row(dest: Path) -> Path:
    """옛 홈 구성 — 홈 자신 행(`.`) + 작업 폴더 행 1개."""
    records = dest / ".project_manager" / ".local" / "worktree-leases.json"
    records.parent.mkdir(parents=True, exist_ok=True)
    records.write_text(json.dumps({"leases": [
        {"slot": ".", "repo": "demo", "session": "demo_1", "started": "2026-08-22T00:00:00+00:00", "state": "leased", "bound": True},
        {"slot": "work/demo_1", "repo": "demo", "session": "demo_1", "state": "leased"},
    ], "tasks": []}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return records


def _lease_slots(records: Path) -> list[str]:
    return [row["slot"]
            for row in json.loads(records.read_text(encoding="utf-8"))["leases"]]


def test_upgrade_adopter_drops_the_legacy_home_row_on_sync(tmp_path):
    """갱신이 옛 홈 자신 행을 지운다 — 작업 폴더 행은 그대로 · 재실행은 byte 불변(멱등)."""
    dest = tmp_path / "legacy-home-row"
    assert import_cmd.main(pm_paths.home_at(_PM_IMPORT.REPO),
        ["--new", str(dest), "--harness", "claude", "--name", _ADOPTER_NAME, "--fill", "manual"]
    ) == 0
    _prime_engine_to_canonical(dest)
    records = _seed_legacy_home_row(dest)

    # dry-run 은 판정만 — 기록 byte 불변.
    before = records.read_bytes()
    preview = _run_pm_update(dest, "--dry-run")
    assert records.read_bytes() == before, "dry-run 이 기록을 고쳤다"
    assert "PM 홈 자신 행" in _combined_output(preview), _combined_output(preview)

    _run_pm_update(dest)
    assert _lease_slots(records) == ["work/demo_1"], _lease_slots(records)

    after = records.read_bytes()
    _run_pm_update(dest)
    assert records.read_bytes() == after, "지울 행이 없는데 다시 실행한 갱신이 기록을 다시 썼다(비멱등)"


def test_upgrade_adopter_moves_the_delegate_round_key_on_sync(tmp_path):
    """갱신이 위임 라운드 기록 파일 행의 옛 키를 collected_at 으로 옮긴다 · 재실행은 바이트 불변."""
    dest = tmp_path / "legacy-delegate-round-key"
    assert import_cmd.main(pm_paths.home_at(_PM_IMPORT.REPO),
        ["--new", str(dest), "--harness", "claude", "--name", _ADOPTER_NAME, "--fill", "manual"]
    ) == 0
    _prime_engine_to_canonical(dest)
    record_file = dest / ".project_manager" / ".local" / "delegate-rounds.jsonl"
    record_file.parent.mkdir(parents=True, exist_ok=True)
    record_file.write_text(
        '{"board_rel":"tickets/rounds/T-0001/01-developer.md","copy":"/tmp/c/01-developer.md",'
        '"harvested_at":null,"ordinal":1,"prepared_at":"2026-10-01T00:00:00.000000+00:00",'
        '"role":"developer","run_id":"' + "a" * 32 + '","ticket":"T-0001"}\n',
        encoding="utf-8", newline="",
    )

    # dry-run 은 출력만 한다.
    before = record_file.read_bytes()
    preview = _run_pm_update(dest, "--dry-run")
    assert record_file.read_bytes() == before, "dry-run 이 기록 파일을 고쳤다"
    assert "collected_at" in _combined_output(preview), _combined_output(preview)

    _run_pm_update(dest)
    row = json.loads(record_file.read_text(encoding="utf-8"))
    assert "collected_at" in row and "harvested_at" not in row, row

    after = record_file.read_bytes()
    _run_pm_update(dest)
    assert record_file.read_bytes() == after, "옮길 행이 없는데 다시 갱신이 기록 파일을 다시 썼다"


# ── 전달/완료 두 프로세스 갱신 (T-0951) ─────────────────────────────────────────────────────────────
# 실제 git 상류(`make_committed_source`)·실제 설치(`pm_import --new`)·서브프로세스 실행만 쓴다.

@pytest.fixture
def t0951_opencode_path(request, tmp_path, monkeypatch):
    """opencode 구성만 가짜 opencode 를 PATH 앞에 둔다(POSIX 아닌 플랫폼은 그 구성만 skip)."""
    if request.node.callspec.params["harness"] != "opencode":
        return None
    return request.getfixturevalue("t0946_fake_opencode")


# 패키지 트리 첫 채택의 고정 기점 — 태그 조회나 최신 함수 합성으로 대체하지 않는다.
_PINNED_V18_COMMITS = {
    "v1.8.0": "9612e2ec19c5742d27f5fa78f930cfbed40ee384",
    "v1.8.1": "298ffe604ab951146b0acc4d70b57a16a563a1bc",
}
# 엔진 패키지로 옮겨 평면 위치에서 은퇴한 모듈(manifest `# pm-retired-path:`).
_PACKAGE_RETIRED_FLAT_MODULES = ("file_lock.py", "console_encoding.py", "repo_owned_files.py", "engine_rev.py")


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
    assert (tools / "file_lock.py").is_file(), "고정 기점 설치가 옛 평면 경우가 아니다"

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



# 관리자 [적용] 한 번으로 최신판에 닿아야 하는 옛 공개 릴리즈의 고정 커밋.
_PINNED_V175_COMMIT = "49a322dba3d03e35dde8e03080e9949272e4a8e8"
_PINNED_V102_COMMIT = "fd7f7e1940ecfc4c090b866a7c979542688d9725"


def _install_pinned_copy(source: Path, dest: Path, harness: str, upstream: str,
                         added: tuple[str, ...] = ()) -> None:
    """고정 커밋 소스의 설치 명령으로 사본을 만들고 `added` 하네스를 그 판의 add-harness 로 얹는다."""
    from _manager_home import _run, known_ctx_env
    env = known_ctx_env(native_bin=dest.parent / "known-ctx-native")
    _run([sys.executable, str(source / ".project_manager/tools/pm_import.py"),
          "--new", str(dest), "--harness", harness, "--fill", "manual",
          "--from", str(source), "--upstream", upstream], cwd=source, env=env)
    for extra in added:
        _run([sys.executable, str(dest / ".project_manager/tools/pm_config.py"),
              "add-harness", extra, "--from", str(source)], cwd=dest, env=env)


def _apply_by_the_manager(dest: Path, monkeypatch) -> dict:
    """관리자 브리지의 [적용] — 번들(이 트리) 갱신기 `--into` 뒤 사본 자신의 갱신."""
    from _manager_home import use_known_ctx_env
    from manager.bridge import Api
    use_known_ctx_env(monkeypatch, dest.parent / "known-ctx-native")
    api = Api(folder_picker=lambda: None, registry_file=dest.parent / "config" / "projects.json")
    return api.update_apply(str(dest))


def _assert_applied_to_latest(response: dict) -> None:
    steps = response.get("steps", [])
    transcript = "\n".join(f"[{step['name']} rc={step['rc']}]\n{step['stdout']}\n{step['stderr']}"
                           for step in steps)
    assert [step["name"] for step in steps] == ["RUN1", "RUN2"], response
    assert all(step["rc"] == 0 for step in steps), transcript
    assert "최신 — 변경 없음" in steps[1]["stdout"], transcript


def _self_prop_rows(dest: Path) -> list[str]:
    """사본 engine.manifest 에서 자기 자신(`.project_manager/engine.manifest`)을 등재한 행."""
    return [line for line in _manifest_text(dest).splitlines()
            if line.split("#", 1)[0].split()[:1] == [_ADOPTER_MANIFEST_REL]]


def _adapter_config_check(dest: Path) -> subprocess.CompletedProcess:
    from _manager_home import known_ctx_env
    return subprocess.run(
        [sys.executable, str(dest / ".project_manager/tools/pm_config.py"), "sync-adapter-config", "--check"],
        cwd=dest, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
        env=known_ctx_env({**os.environ, "PM_NONINTERACTIVE": "1"}, native_bin=dest.parent / "known-ctx-native"))


def test_pinned_v175_three_harness_adopter_reaches_latest_by_the_manager(
        tmp_path, t0927_upstream, t0946_fake_opencode, monkeypatch):
    """v1.7.5 claude+opencode+codex 사본(URL upstream)이 관리자 [적용] 한 번으로 최신판에 닿는다.

    첫 실행은 번들 갱신기가 엔진 코드를 옮기고, 사본에만 있던 manifest 행은 관리에서 빠지며 파일은 남는다.
    추가 하네스 구획은 출하 manifest 에서 다시 만들어져 이름 바뀐 opencode 명령이 생긴다."""
    source = _pinned_source(tmp_path, _PINNED_V175_COMMIT)
    dest = tmp_path / "v175-three"
    _install_pinned_copy(source, dest, "claude", t0927_upstream.as_uri(), ("opencode", "codex"))
    assert (dest / ".claude/run_tests_hook.sh").is_file(), "고정 기점 설치가 옛 경우가 아니다"

    response = _apply_by_the_manager(dest, monkeypatch)

    _assert_applied_to_latest(response)
    check = _adapter_config_check(dest)
    assert check.returncode == 0, f"{check.stdout}\n{check.stderr}"
    added_harness_paths = [_row_path(row) for row in _added_harness_rows(_added_harness_manifest_block(dest) or "")]
    for name in ("pm-cluster-claim", "pm-cluster-finish"):
        rel = f".opencode/command/{name}.md"
        assert rel in added_harness_paths, added_harness_paths
        assert (dest / rel).is_file(), rel
    assert (dest / ".claude/run_tests_hook.sh").is_file()
    assert ".claude/run_tests_hook.sh" not in _manifest_text(dest)
    run1 = response["steps"][0]["stdout"]
    assert "관리에서 빠지고 파일은 그대로 둔다" in run1, run1
    assert "    - .claude/run_tests_hook.sh" in run1, run1
    conf_lines = (dest / ".project_manager/local.conf").read_text(encoding="utf-8").splitlines()
    assert any(line.startswith("upstream.path=") for line in conf_lines), conf_lines
    assert not any(line.startswith("upstream=") for line in conf_lines), conf_lines


def test_pinned_v102_opencode_adopter_reaches_latest_by_the_manager(
        tmp_path, t0927_upstream, t0946_fake_opencode, monkeypatch):
    """v1.0.2 opencode 사본(self-prop 행 없는 manifest·옛 키 `upstream=`)이 관리자 [적용] 한 번으로 최신판에 닿는다.

    판은 manifest 의 opencode 고유 경로로 정해지고(루트 manifest 로 계획하지 않는다), 옛 키 이름은 상류
    해소보다 먼저 옮겨진다."""
    source = _pinned_source(tmp_path, _PINNED_V102_COMMIT)
    dest = tmp_path / "v102-opencode"
    _install_pinned_copy(source, dest, "opencode", t0927_upstream.as_uri())
    assert not _self_prop_rows(dest), "고정 기점 설치가 옛 경우가 아니다"

    response = _apply_by_the_manager(dest, monkeypatch)

    _assert_applied_to_latest(response)
    assert not (dest / ".claude/agents").exists()
    self_rows = _self_prop_rows(dest)
    assert self_rows and "@source=templates/opencode/.project_manager/engine.manifest" in self_rows[0], self_rows
    assert (dest / ".opencode/command/pm-adr.md").is_file()

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
        '    print("완료 단계 표시 출력")\n'
        '    print("완료 단계 표시 오류", file=sys.stderr)\n'
        '    raise SystemExit(7)\n')
    _git("-C", str(source), "commit", "-qam", "completion marker")

    run = subprocess.run(
        [sys.executable, str(home / ".project_manager/tools/pm_update.py"), "--from", str(source)],
        cwd=home, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300, env=known_ctx_env({**os.environ, "PM_NONINTERACTIVE": "1"},
                                       native_bin=home.parent / "known-ctx-native"))

    assert run.returncode == 7, f"{run.stdout}\n{run.stderr}"
    assert "[update] .project_manager/tools/pm_update.py" in run.stdout, run.stdout
    assert "완료 단계 표시 출력" in run.stdout, run.stdout
    assert "완료 단계 표시 오류" in run.stderr, run.stderr


def _t0951_pm_update(home: Path, source: Path) -> subprocess.CompletedProcess:
    from _manager_home import known_ctx_env
    return subprocess.run(
        [sys.executable, str(home / ".project_manager/tools/pm_update.py"), "--from", str(source)],
        cwd=home, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300, env=known_ctx_env({**os.environ, "PM_NONINTERACTIVE": "1"},
                                       native_bin=home.parent / "known-ctx-native"))


@pytest.fixture(autouse=True)
def _stub_install_model_listing(monkeypatch):
    """공유 package의 조회 대역을 테스트마다 복원한다."""
    monkeypatch.setattr(import_fill, "_real_models_runner", lambda source=None: (False, []))
