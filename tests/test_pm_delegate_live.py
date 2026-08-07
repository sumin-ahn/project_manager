"""pm_delegate 라이브 하네스 테스트 — 실 CLI cross 위임 3방향 스모크 (T-0449 · sealed spike §9).

pm_delegate.py 는 **실 하네스 CLI subprocess** 를 스폰하는 채널이라 backbone 단위 테스트
(`test_pm_delegate.py`·전부 run_fn mock)로는 "argv/config 해소가 옳나"만 검증되고 "**실 codex/
opencode/claude 가 위임 프롬프트를 받아 완주하고 reply 를 회수하나**"는 미검증이다
([[harness-test-vs-machine-test]]). 이 파일이 그 갭을 `PM_DELEGATE_LIVE=1` 게이트로 메운다
(`PM_RELAY_LIVE`/`PM_ORCH_LIVE` 선례·기본 skip·CI green 불변) — 소형 read 태스크(README 요약)를
`--role researcher` 로 3방향 타깃(codex·opencode·claude)에 각 1회 실 위임하고, **reply 에 README
고유 marker 가 실제로 담겼는지** 단언한다.

**false-green 방지(side-effect/marker 기반·adr_live/release_wave 상속)**: 프롬프트는 README 의
교정 코드(`_MARKER`)를 *값으로 말하지 않고* "README 에 적힌 코드를 그대로 인용하라"고만 지시한다 —
빈 reply·프롬프트 에코·미실행은 marker 부재로 red(marker 는 오직 README 를 실제로 읽어야 나온다).
+ 위임 성공 = rc==0 + raw 박제 파일 존재까지 hard 단언.

**in-process(self-contained)**: `pm_delegate.main()` 을 직접 호출하되 `local_config` 를
`delegate_enabled=true` 로 monkeypatch 하고 `--harness/--model` CLI override 로 타깃을 지정한다 —
per-clone local.conf(worktree) 의존을 없애 fresh clone/CI/livegate 어디서든 자기-정합. run_fn 은
실제 `_default_run_fn`(실 subprocess 스폰)이다. codex 는 격리 CODEX_HOME(auth 복사·종료 시 삭제·실
~/.codex 미오염·conftest 선례)로 스폰한다.

**release tier 편입**: codex 방향 1건을 `@pytest.mark.release` + `PM_ORCH_LIVE_RELEASE` 게이트로
릴리즈 라이브 tier 에 편입한다(codex 라이브 = load-bearing 게이트·test_pm_relay_codex 선례). ⚠
**커플드 전역 pin**(board.LIVEGATE_RELEASE_PIN · test_release_wave `_RELEASE_TEST_FILES`/
`_EXPECTED_RELEASE_TESTS` · test_board_livegate · test_worktree_pool 미러 · templates/*/board.py)에
이 파일을 **+1 등재**하는 갱신은 **orchestrator 가 wave 종료 시 1회 수행**한다(touches 밖 공유 상수·
병렬 wave clobber 회피·test_pm_release_live 동형). 이 파일은 **file-local 마커 pin** 만 둔다.

always-run 가드(라이브 없이·매 회귀): backbone 존재·로드 + 실측 `_REASONING_ALLOWED` 집합 + marker
비-에코(false-green 가드가 vacuous 아님) + release 마커 수 pin.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import codex_auth_available, drop_codex_auth, make_codex_home

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

# ── 게이트 (기본 skip·CI green 불변) ────────────────────────────────────────────
PM_DELEGATE_LIVE = os.environ.get("PM_DELEGATE_LIVE") == "1"      # 3방향 on-demand 스모크.
_RELEASE_LIVE = os.environ.get("PM_ORCH_LIVE_RELEASE") == "1"     # release tier 트리거(livegate).

# 모델(env override) — 라이브 실측 매핑(researcher 중심·저비용) 단일 진실. codex=gpt-5.6-terra(T-0449
# 실측 유효)·claude=sonnet 계열 alias·opencode=ollama/glm-5.2:cloud([[opencode-live-model-glm52]]·$0).
CODEX_MODEL = os.environ.get("PM_DELEGATE_LIVE_CODEX_MODEL", "gpt-5.6-terra")
CLAUDE_MODEL = os.environ.get("PM_DELEGATE_LIVE_CLAUDE_MODEL", "claude-sonnet-4-6")
LIVE_MODEL = os.environ.get("PM_DELEGATE_LIVE_MODEL", "ollama/glm-5.2:cloud")
_CODEX_TIMEOUT = int(os.environ.get("PM_DELEGATE_LIVE_CODEX_TIMEOUT", "600"))
_CLAUDE_TIMEOUT = int(os.environ.get("PM_DELEGATE_LIVE_CLAUDE_TIMEOUT", "600"))
_OPENCODE_TIMEOUT = int(os.environ.get("PM_DELEGATE_LIVE_TIMEOUT", "1800"))

# README 에만 심는 고유 교정 코드 — 프롬프트는 이 값을 *말하지 않고* "README 의 코드를 인용하라"고만
# 지시한다. reply 에 이 marker 가 있으면 위임이 실제로 파일을 읽고 완주했다는 증거(에코/빈-reply 차단).
_MARKER = "QZX4242DELEGATE"

# 이 파일 release 마커 수(file-local pin·마커 소실/개명 방어). 전역 커플드-pin 등재는 orchestrator 소유.
_EXPECTED_RELEASE_TESTS = 1

_GIT = shutil.which("git")


# ── backbone 로드 ────────────────────────────────────────────────────────────
def _load_pd():
    """canonical pm_delegate.py 를 로드한다(실 위임 채널 = 이 backbone)."""
    spec = importlib.util.spec_from_file_location("pm_delegate_live", TOOLS / "pm_delegate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── fixture (git repo + marker README + 요약 프롬프트·containment 통과) ──────────
def _seed_repo(tmp_path: Path) -> tuple[Path, Path]:
    """위임 대상 작업공간 — git repo(_cwd_in_git_repo 통과) + marker README + prompt-file(cwd 하위).

    프롬프트는 `_MARKER` 를 값으로 말하지 않는다(README 만 안다) — false-green 가드의 근거."""
    repo = tmp_path / "delegate-live-repo"
    repo.mkdir()
    if _GIT is not None:
        subprocess.run([_GIT, "init", "-q"], cwd=repo, check=True)
        subprocess.run([_GIT, "-C", str(repo), "config", "user.email", "live@x"], check=True)
        subprocess.run([_GIT, "-C", str(repo), "config", "user.name", "live"], check=True)
    (repo / "README.md").write_text(
        "# Widget Tracker\n\n"
        "이 프로젝트는 창고의 위젯 재고를 추적하는 작은 도구다. 모든 재고 조정은 단일 "
        f"교정 코드(calibration code) `{_MARKER}` 로 서명되며, 어떤 요약이든 이 코드를 반드시 "
        "그대로 인용해야 한다.\n\n"
        "재고는 하루 한 번 스냅샷으로 집계되고, 불일치는 다음 스냅샷까지 이월된다.\n",
        encoding="utf-8",
    )
    prompt = repo / "summarize_prompt.txt"
    prompt.write_text(
        "이 저장소 루트의 README.md 를 읽고 핵심을 정확히 3줄로 요약하라. 요약 안에는 README 에 "
        "적혀 있는 정확한 교정 코드(calibration code)를 찾아 그대로(대소문자 유지) 반드시 인용하라. "
        "요약만 출력하고 다른 설명은 붙이지 마라.",
        encoding="utf-8",
    )
    return repo, prompt


# ── 위임 실행 (in-process main·실 subprocess 스폰) ────────────────────────────────
def _delegate(pd, monkeypatch, capsys, repo: Path, prompt: Path, harness: str, model: str,
              reasoning: str | None, output_dir: Path, timeout: int) -> tuple[int, str, str]:
    """pm_delegate.main() 을 in-process 로 호출(local_config=enabled monkeypatch·실 run_fn 스폰).

    reply 는 stdout(print)로 나오므로 capsys 로 회수한다. rc·reply·stderr 반환."""
    monkeypatch.setattr(pd, "local_config", lambda: {"delegate_enabled": "true"})
    argv = ["--role", "researcher", "--prompt-file", str(prompt), "--cwd", str(repo),
            "--harness", harness, "--model", model, "--output-dir", str(output_dir),
            "--timeout", str(timeout)]
    if reasoning:
        argv += ["--reasoning", reasoning]
    rc = pd.main(argv)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _assert_delegate_ok(rc: int, reply: str, err: str, output_dir: Path, harness: str) -> None:
    """위임 성공 강판정 — rc==0 + reply 에 README marker(실 읽기 증거) + raw 박제 파일 존재."""
    tail = f"\n--- stderr(tail) ---\n{err[-1500:]}\n--- reply(tail) ---\n{reply[-800:]}"
    assert rc == 0, f"{harness} 위임 rc={rc}(!=0·실패 loud)" + tail
    assert _MARKER in reply, (
        f"{harness} reply 에 README marker {_MARKER!r} 부재 — 빈/에코 reply 또는 미실행"
        f"(false-green 가드). marker 는 README 를 실제로 읽어야만 나온다." + tail
    )
    raws = list(Path(output_dir).glob(f"pm_delegate_{harness}_*.txt"))
    assert raws, f"{harness} raw 박제 파일 부재(§3.4 감사 산출물 없음)." + tail


# ── 라이브 3방향 (on-demand · PM_DELEGATE_LIVE=1) ──────────────────────────────────
@pytest.mark.skipif(
    not PM_DELEGATE_LIVE or not shutil.which("codex") or not codex_auth_available(),
    reason="pm_delegate 라이브(codex) — PM_DELEGATE_LIVE=1 + codex CLI(과금·~/.codex/auth.json) 필요. "
           "기본 skip·on-demand.",
)
def test_delegate_live_codex(tmp_path, monkeypatch, capsys):
    """claude/codex/opencode PM → **codex** researcher 위임 1회 — reply marker + rc0 + raw 박제.

    격리 CODEX_HOME(auth 복사·종료 시 삭제)·read-only sandbox(researcher=순수읽기·기계적)·reasoning=low
    (`-c model_reasoning_effort=low`·drive)."""
    pd = _load_pd()
    repo, prompt = _seed_repo(tmp_path)
    out_dir = tmp_path / "raw"
    home = make_codex_home(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(home))
    try:
        rc, reply, err = _delegate(pd, monkeypatch, capsys, repo, prompt, "codex",
                                   CODEX_MODEL, "low", out_dir, _CODEX_TIMEOUT)
    finally:
        drop_codex_auth(home)
    _assert_delegate_ok(rc, reply, err, out_dir, "codex")


@pytest.mark.skipif(
    not PM_DELEGATE_LIVE or not shutil.which("opencode"),
    reason="pm_delegate 라이브(opencode) — PM_DELEGATE_LIVE=1 + opencode CLI(+ollama 모델) 필요. "
           "기본 skip·on-demand.",
)
def test_delegate_live_opencode(tmp_path, monkeypatch, capsys):
    """claude/codex PM → **opencode**(glm-5.2:cloud·$0) researcher 위임 1회 — reply marker + rc0 + raw.

    `--file` 프롬프트 채널·`--dir` cwd 핀·`--agent plan`(read=쓰기차단)·`--variant high`(passthrough)."""
    pd = _load_pd()
    repo, prompt = _seed_repo(tmp_path)
    out_dir = tmp_path / "raw"
    rc, reply, err = _delegate(pd, monkeypatch, capsys, repo, prompt, "opencode",
                               LIVE_MODEL, "high", out_dir, _OPENCODE_TIMEOUT)
    _assert_delegate_ok(rc, reply, err, out_dir, "opencode")


@pytest.mark.skipif(
    not PM_DELEGATE_LIVE or not shutil.which("claude"),
    reason="pm_delegate 라이브(claude) — PM_DELEGATE_LIVE=1 + claude CLI(API 과금) 필요. "
           "기본 skip·on-demand.",
)
def test_delegate_live_claude(tmp_path, monkeypatch, capsys):
    """codex/opencode PM → **claude** researcher 위임 1회 — reply marker + rc0 + raw 박제.

    `--tools Read,Glob,Grep`(researcher=Bash/Write 제외·기계적)·`--effort low`(drive)·cwd 존중."""
    pd = _load_pd()
    repo, prompt = _seed_repo(tmp_path)
    out_dir = tmp_path / "raw"
    rc, reply, err = _delegate(pd, monkeypatch, capsys, repo, prompt, "claude",
                               CLAUDE_MODEL, "low", out_dir, _CLAUDE_TIMEOUT)
    _assert_delegate_ok(rc, reply, err, out_dir, "claude")


# ── release tier (livegate · PM_ORCH_LIVE_RELEASE=1) ──────────────────────────────
@pytest.mark.release
@pytest.mark.skipif(
    not _RELEASE_LIVE or not shutil.which("codex") or not codex_auth_available(),
    reason="pm_delegate 릴리즈 라이브(codex) — PM_ORCH_LIVE_RELEASE=1 + codex CLI(과금·auth) 필요. "
           "기본 skip·릴리즈 트리거.",
)
def test_delegate_live_codex_release(tmp_path, monkeypatch, capsys):
    """릴리즈 라이브 tier — codex cross 위임 green 없이 관련 버전 push 차단(spike §9·codex load-bearing).

    on-demand codex 스모크와 동일 본문·release tier 참여자. 커플드 전역 pin +1 등재는 orchestrator
    소유(파일 docstring ⚠ 참조)."""
    pd = _load_pd()
    repo, prompt = _seed_repo(tmp_path)
    out_dir = tmp_path / "raw"
    home = make_codex_home(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(home))
    try:
        rc, reply, err = _delegate(pd, monkeypatch, capsys, repo, prompt, "codex",
                                   CODEX_MODEL, "low", out_dir, _CODEX_TIMEOUT)
    finally:
        drop_codex_auth(home)
    _assert_delegate_ok(rc, reply, err, out_dir, "codex")


# ── always-run 가드 (라이브 없이·매 회귀) ─────────────────────────────────────────
def test_pm_delegate_backbone_shipped_and_loadable():
    """backbone pm_delegate.py 가 canonical 에 존재·로드 가능하고 위임 API 를 노출 — setup-rot pin."""
    pd = _load_pd()
    for attr in ("main", "resolve_delegate", "build_codex_argv", "build_claude_argv",
                 "build_opencode_argv", "_validate_reasoning"):
        assert hasattr(pd, attr), f"pm_delegate.py 에 {attr} 부재 — API rot."


def test_reasoning_allowed_measured_sets_pinned():
    """reasoning 허용집합이 라이브 실측값을 보유 — 빈 집합 회귀/실측값 소실을 red 로 잡는다.

    claude `--effort`=low/medium/high/xhigh/max(CLI-authoritative)·opencode `--variant`=minimal/low/
    medium/high/max(문서 ladder·passthrough typo-guard)·codex `-c model_reasoning_effort`=low/medium/
    high/xhigh/max(0.145.0 xhigh 실측 + T-0590 max 편입). 실측 전 '미확정 빈 집합'으로 되돌아가면
    (silent-무시 재발) 여기서 red.

    테이블 소유자는 위임/추가 리뷰어 공용 계약 모듈 `pm_relay` 다(T-0590) — 위임은 그 검증을 부르는
    wrapper 이므로 실효 판정을 `_validate_reasoning` 으로도 함께 못박는다."""
    pd = _load_pd()
    allowed = pd._load_relay().REASONING_ALLOWED
    assert allowed["codex"] == frozenset({"low", "medium", "high", "xhigh", "max"})
    assert allowed["claude"] == frozenset({"low", "medium", "high", "xhigh", "max"})
    assert allowed["opencode"] == frozenset({"minimal", "low", "medium", "high", "max"})
    assert pd._validate_reasoning("codex", "max") == "max"
    assert pd._validate_reasoning("opencode", "minimal") == "minimal"


def test_marker_absent_from_prompt_present_in_readme(tmp_path):
    """false-green 가드 비-vacuous — `_MARKER` 는 README 에만 있고 프롬프트엔 없다.

    reply marker 단언이 유효하려면 marker 가 프롬프트 에코로 나올 수 없어야 한다(오직 README 를 읽어야
    나옴). 프롬프트에 marker 가 있으면 에코만으로 통과하는 vacuous 단언이 되므로 여기서 못박는다."""
    repo, prompt = _seed_repo(tmp_path)
    assert _MARKER in (repo / "README.md").read_text(encoding="utf-8"), "README 에 marker 부재(fixture rot)."
    assert _MARKER not in prompt.read_text(encoding="utf-8"), (
        "프롬프트에 marker 가 있음 — reply marker 단언이 에코로 vacuous 통과 가능(false-green 가드 무력)."
    )


def test_release_markers_pinned():
    """이 파일 release 마커 수(=1·codex 릴리즈 스모크)를 pin — 마커 소실/개명 시 게이트 selection 누락 방어.

    `pytest -m release` 는 마커로 라이브 서브셋을 고른다. 데코레이터 삭제/개명 시 조용히 빠지는 것을
    file-local 로 잡는다(test_pm_release_live 동형). ⚠ 이 파일을 test_release_wave `_RELEASE_TEST_FILES`
    합산 + 전역 pin(board.LIVEGATE_RELEASE_PIN 등)에 등재하는 커플드-pin 갱신은 orchestrator 가 wave
    종료 시 수행(touches 밖 공유 상수). 라이브를 의도적으로 늘리면 `_EXPECTED_RELEASE_TESTS` 를 함께 갱신."""
    import ast
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    count = 0
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            target = deco.func if isinstance(deco, ast.Call) else deco
            if (isinstance(target, ast.Attribute) and target.attr == "release"
                    and isinstance(target.value, ast.Attribute) and target.value.attr == "mark"
                    and isinstance(target.value.value, ast.Name) and target.value.value.id == "pytest"):
                count += 1
    assert count == _EXPECTED_RELEASE_TESTS, (
        f"이 파일 release 마커 수={count} != 기대 {_EXPECTED_RELEASE_TESTS} — 마커 소실/개명 의심."
    )
