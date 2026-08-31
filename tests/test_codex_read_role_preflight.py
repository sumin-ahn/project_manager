"""T-0844 — codex read 역할 exec-root preflight.

배경 — codex read 역할(researcher·code-reviewer)만 `-C`(실행 root)를 격리 tmp 로 재앵커한다
(`_READ_TMP_REANCHOR_EXEC_ROOT_BY_HARNESS["codex"]`). 그 tmp 는 매 attempt 새로 만드는 빈
디렉터리라 절대 git 저장소가 아니고, 모델은 프롬프트 지시로 `--cwd` 복귀를 기대받을 뿐이다 —
그 준수를 기계가 보장할 수는 없다(`codex exec --help` 실측: `-C` 는 항상 암묵적 쓰기 가능
root·`--add-dir` 는 추가만 한다 — "주 워크스페이스는 read-only·별도 root만 write" 조합이 CLI 에
없다). 실측 3건(T-0778 r05·T-0841 r02·T-0823 r04)에서 리뷰어가 재앵커된 빈 tmp 에서 되짚어가지
못해 "저장소가 아니다/테스트가 없다"로 반려했다 — 세 라운드 모두 `--cwd` 자신은 정상이었다.

이 preflight(`_preflight_codex_read_exec_root`)는 재앵커 **이전**의 `--cwd` 가 리뷰 가능한
형상인지(저장소인가·toplevel 이 `--cwd` 자신인가·code-reviewer 는 staged 가 있는가)를 스폰 전에
기계로 확정해, 불량 입력으로 인한 무의미한 유료 라운드를 과금 전에 끊는다.
"""
from __future__ import annotations

import importlib.util
import json as _json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pd():
    return _load("pm_delegate", TOOLS / "pm_delegate.py")


def _codex_stdout(reply: str = "DONE") -> str:
    return "\n".join([
        _json.dumps({"type": "thread.started", "thread_id": "th1"}),
        _json.dumps({"type": "item.completed",
                     "item": {"type": "agent_message", "text": reply}}),
        _json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10}}),
    ])


def _claude_stdout(reply: str = "DONE") -> str:
    return _json.dumps({"type": "result", "result": reply, "session_id": "s1"})


def _opencode_stdout(reply: str = "DONE") -> str:
    return _json.dumps({"type": "text", "sessionID": "ses_1",
                        "part": {"type": "text", "text": reply}})


def _init_repo(repo: Path, *, staged: bool) -> None:
    """git 저장소를 `repo` 자신을 toplevel 로 세운다 — `staged` 면 독립 staged 변경 하나를 얹는다."""
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "seed.txt"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "base"],
        cwd=repo, check=True, capture_output=True,
    )
    if staged:
        (repo / "staged.txt").write_text("staged\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "staged.txt"], cwd=repo, check=True, capture_output=True,
        )


def _fail_if_called(*args, **kwargs):
    pytest.fail("preflight 가 막았어야 하는데 run_fn 이 호출됨 — 호출 전 차단 위반")


def _run_attempt(pd, *, harness, role, cwd, tmp_path, run_fn):
    return pd._execute_attempt(
        harness=harness, model="gpt-x" if harness == "codex" else (
            "opus" if harness == "claude" else "prov/m"
        ),
        reasoning=None, role=role, cwd=cwd, prompt="role preamble + task",
        timeout=30, output_dir=tmp_path / "raw", run_fn=run_fn, attempt="primary",
    )


# ── (a) 세 형상 — 각각 호출 전 rc≠0 차단 ─────────────────────────────────

def test_non_repo_cwd_blocks_before_spawn(pd, tmp_path, monkeypatch):
    """--cwd 가 git 저장소가 아니면 codex 를 스폰하지 않고 DelegateError 로 끊는다.

    "이 `--cwd` 는 어느 checkout 도 아니다"가 입력이다 — 픽스처 위치가 그 답을 정하지 않도록
    그 한 질문(`rev-parse --show-toplevel`)에만 비-repo(rc 128)를 명시하고 나머지 git 호출은
    실제 git 에 그대로 위임한다.
    """
    cwd = tmp_path / "worktree"
    cwd.mkdir()
    real_run = pd.subprocess.run

    def run(argv, **kwargs):
        if list(argv[:3]) == ["git", "-C", str(cwd.resolve())] and "--show-toplevel" in argv:
            return subprocess.CompletedProcess(
                argv, 128, "", "fatal: not a git repository")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(pd.subprocess, "run", run)

    with pytest.raises(pd.DelegateError, match="git 저장소가 아님"):
        _run_attempt(
            pd, harness="codex", role="code-reviewer", cwd=cwd, tmp_path=tmp_path,
            run_fn=_fail_if_called,
        )

    rows = pd._load_relay().raw_records(tmp_path / "raw" / "raw_outputs.json")
    assert len(rows) == 1
    assert rows[0]["pre_spawn_rejected"] is True
    assert rows[0]["rc"] == 1


def test_subdirectory_cwd_toplevel_mismatch_blocks(pd, tmp_path):
    """--cwd 가 저장소 하위 디렉터리(toplevel 아님)면 실행 root 불일치로 끊는다."""
    repo = tmp_path / "repo"
    _init_repo(repo, staged=True)
    cwd = repo / "sub"
    cwd.mkdir()

    with pytest.raises(pd.DelegateError, match="실행 root 불일치"):
        _run_attempt(
            pd, harness="codex", role="code-reviewer", cwd=cwd, tmp_path=tmp_path,
            run_fn=_fail_if_called,
        )


def test_zero_staged_blocks_code_reviewer(pd, tmp_path):
    """저장소·toplevel 은 정상이어도 staged 변경 0 이면 code-reviewer 를 끊는다."""
    repo = tmp_path / "repo"
    _init_repo(repo, staged=False)

    with pytest.raises(pd.DelegateError, match="staged 변경 0"):
        _run_attempt(
            pd, harness="codex", role="code-reviewer", cwd=repo, tmp_path=tmp_path,
            run_fn=_fail_if_called,
        )


# ── (b) 오차단 0 — 정상 형상은 막히지 않는다 ──────────────────────────────────

def test_normal_shape_code_reviewer_passes(pd, tmp_path):
    """저장소·toplevel 일치·staged 존재 — codex code-reviewer 는 정상 스폰된다."""
    repo = tmp_path / "repo"
    _init_repo(repo, staged=True)
    calls = []

    def _capture(argv, *, stdin_text, cwd, env, timeout, harness):
        calls.append(argv)
        return {"returncode": 0, "stdout": _codex_stdout("검토 완료"),
                "stderr": "", "timed_out": False}

    _run_attempt(
        pd, harness="codex", role="code-reviewer", cwd=repo, tmp_path=tmp_path,
        run_fn=_capture,
    )
    assert len(calls) == 1


def test_researcher_role_is_exempt_from_staged_requirement(pd, tmp_path):
    """researcher 는 '조사·분석만' 계약이라 staged 0 이어도 오차단되지 않는다."""
    repo = tmp_path / "repo"
    _init_repo(repo, staged=False)
    calls = []

    def _capture(argv, *, stdin_text, cwd, env, timeout, harness):
        calls.append(argv)
        return {"returncode": 0, "stdout": _codex_stdout("조사 완료"),
                "stderr": "", "timed_out": False}

    _run_attempt(
        pd, harness="codex", role="researcher", cwd=repo, tmp_path=tmp_path,
        run_fn=_capture,
    )
    assert len(calls) == 1


def test_developer_role_never_reaches_preflight(pd, tmp_path):
    """write 역할(developer)은 read_tmp 가 없어 이 preflight 대상이 아니다 — 비-저장소 cwd 도 통과."""
    cwd = tmp_path / "worktree"
    cwd.mkdir()
    calls = []

    def _capture(argv, *, stdin_text, cwd, env, timeout, harness):
        calls.append(argv)
        return {"returncode": 0, "stdout": _codex_stdout("작업 완료"),
                "stderr": "", "timed_out": False}

    _run_attempt(
        pd, harness="codex", role="developer", cwd=cwd, tmp_path=tmp_path,
        run_fn=_capture,
    )
    assert len(calls) == 1


# ── (c) 클래스 전수 — claude·opencode 는 exec root 를 재앵커하지 않아 이 클래스가 없다 ──

@pytest.mark.parametrize(
    ("harness", "stdout_fn"),
    [("claude", _claude_stdout), ("opencode", _opencode_stdout)],
)
def test_claude_and_opencode_reviewer_not_subject_to_this_class(
        pd, tmp_path, harness, stdout_fn):
    """claude·opencode 는 `_READ_TMP_REANCHOR_EXEC_ROOT_BY_HARNESS` 가 False 라 -C/--dir 을 건드리지
    않는다 — 같은 비-저장소·staged-0 cwd 로도 이 preflight 가 아예 발동하지 않고 정상 스폰된다."""
    assert pd._READ_TMP_REANCHOR_EXEC_ROOT_BY_HARNESS[harness] is False
    cwd = tmp_path / "worktree"   # 의도적으로 git 저장소가 아님 · staged 도 없음
    cwd.mkdir()
    calls = []

    def _capture(argv, *, stdin_text, cwd, env, timeout, harness):
        calls.append(argv)
        return {"returncode": 0, "stdout": stdout_fn("검토 완료"),
                "stderr": "", "timed_out": False}

    _run_attempt(
        pd, harness=harness, role="code-reviewer", cwd=cwd, tmp_path=tmp_path,
        run_fn=_capture,
    )
    assert len(calls) == 1


# ── T-0844 라운드 2 — 재앵커된 read 실행의 3-절대경로 preamble(엔진 소유) ──────────

def test_reanchor_preamble_carries_three_absolute_values_and_declaration(pd, tmp_path):
    """`_reanchor_exec_root_preamble`(순수 함수) 산출에 (a)(b)(c) 세 절대경로와 '대상 아님'
    선언·절대경로 git 명령 예시가 실린다."""
    cwd = tmp_path / "repo"
    exec_root = tmp_path / "isolated-tmp"
    writable = exec_root
    text = pd._reanchor_exec_root_preamble(cwd, exec_root, writable)

    assert str(cwd) in text                       # (a) 검토 대상 절대경로
    assert str(exec_root) in text                  # (b) 실행 root 절대경로
    assert "검토 대상이 아니다" in text             # (b) 대상 아님 선언
    assert str(writable) in text                   # (c) 쓰기 가능 임시 경로
    assert f"git -C {pd.render_shell_token(str(cwd))} diff --cached" in text
    assert f"git -C {pd.render_shell_token(str(cwd))} rev-parse HEAD" in text
    assert f"git -C {pd.render_shell_token(str(cwd))} status --short" in text
    pytest_line = [line for line in text.splitlines() if "pytest" in line and "-m" in line][0]
    assert str(cwd) in pytest_line  # pytest 예시도 절대 target


def test_predict_read_tmp_paths_has_no_side_effects(pd):
    """dry-run 예측 경로는 실제 mkdir을 하지 않는다 — 이름 규칙만 계산."""
    predicted_path, predicted_writable = pd._predict_read_tmp_paths("codex")
    assert predicted_path.name.startswith(pd._READ_TMP_PREFIX)
    assert not predicted_path.exists()
    assert not predicted_writable.exists()


def test_real_execution_prompt_carries_reanchor_preamble(pd, tmp_path):
    """codex code-reviewer 실행의 실제 outgoing prompt(stdin)에도 같은 3-절대경로 블록이 실린다."""
    repo = tmp_path / "repo"
    _init_repo(repo, staged=True)
    seen = {}

    def _capture(argv, *, stdin_text, cwd, env, timeout, harness):
        seen["prompt"] = stdin_text
        return {"returncode": 0, "stdout": _codex_stdout("검토 완료"),
                "stderr": "", "timed_out": False}

    _run_attempt(
        pd, harness="codex", role="code-reviewer", cwd=repo, tmp_path=tmp_path,
        run_fn=_capture,
    )
    prompt = seen["prompt"]
    assert str(repo) in prompt
    assert "검토 대상이 아니다" in prompt
    assert f"git -C {pd.render_shell_token(str(repo))} diff --cached" in prompt


def test_dry_run_prompt_carries_three_absolute_values(pd, tmp_path, capsys):
    """PM 실측 재현: `--dry-run` 출력의 합성 프롬프트에 (a)(b)(c) 세 절대경로가 박혀 있다."""
    repo = tmp_path / "repo"
    _init_repo(repo, staged=True)
    prompt_file = repo / "prompt.md"     # --prompt-file 은 --cwd 하위여야 containment 를 통과
    prompt_file.write_text("임의 프롬프트 본문", encoding="utf-8")

    rc = pd.main([
        "--role", "code-reviewer", "--harness", "codex", "--model", "gpt-x",
        "--ticket", "T-0844", "--prompt-file", str(prompt_file),
        "--cwd", str(repo), "--dry-run",
    ])
    out = capsys.readouterr().out

    assert rc == 0
    assert str(repo) in out                        # (a) 검토 대상 절대경로
    assert "검토 대상이 아니다" in out               # (b) 실행 root·대상 아님 선언
    assert "이 프로세스의 실행 root(-C):" in out
    assert "쓰기 가능 임시 경로:" in out              # (c)
    assert f"git -C {pd.render_shell_token(str(repo))} diff --cached" in out
    assert f"git -C {pd.render_shell_token(str(repo))} rev-parse HEAD" in out


def test_dry_run_developer_role_has_no_reanchor_preamble(pd, tmp_path, capsys):
    """write 역할(developer)은 read_tmp/재앵커 대상이 아니므로 이 preamble 이 없다."""
    repo = tmp_path / "repo"
    _init_repo(repo, staged=True)
    prompt_file = repo / "prompt.md"
    prompt_file.write_text("임의 프롬프트 본문", encoding="utf-8")

    rc = pd.main([
        "--role", "developer", "--harness", "codex", "--model", "gpt-x",
        "--prompt-file", str(prompt_file), "--cwd", str(repo), "--dry-run",
    ])
    out = capsys.readouterr().out

    assert rc == 0
    assert "검토 대상이 아니다" not in out
    assert "[격리 실행 좌표 — 엔진 값]" not in out


# ── T-0844 라운드 5 (F-001) — dry-run 예측은 실행과 같은 술어를 소비해야 한다 ────────

def test_dry_run_no_reanchor_when_read_tmp_strategy_unavailable(
        pd, tmp_path, monkeypatch, capsys):
    """read_tmp 전략이 없으면(fd 결속도 소유자 ACL 도 없음) 실행도 재앵커를 안 하므로(`read_tmp
    =None` → `_apply_read_tmp_argv` 가 argv 를 그대로 반환) dry-run 도 존재하지 않을 좌표를
    예고하지 않는다 — 존재하지 않을 좌표를 안내하는 건 안내가 없는 것보다 나쁘다(PM 판정).
    dry-run 과 실행의 `-C` 의미(둘 다 `--cwd`)·preamble 유무가 일치함을 한 테스트에서 교차 확인."""
    monkeypatch.setattr(pd, "_READ_TMP_FD_SUPPORTED", False)
    monkeypatch.setattr(pd, "_read_tmp_owner_acl_platform", lambda: False)
    assert pd._read_tmp_strategy() is None

    repo = tmp_path / "repo"
    _init_repo(repo, staged=True)
    prompt_file = repo / "prompt.md"
    prompt_file.write_text("임의 프롬프트 본문", encoding="utf-8")

    rc = pd.main([
        "--role", "code-reviewer", "--harness", "codex", "--model", "gpt-x",
        "--ticket", "T-0844", "--prompt-file", str(prompt_file),
        "--cwd", str(repo), "--dry-run",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "검토 대상이 아니다" not in out
    assert "[격리 실행 좌표 — 엔진 값]" not in out
    assert f"-C {repo}" in out   # dry-run argv 의 -C 의미 = --cwd (재앵커 없음)

    seen = {}

    def _capture(argv, *, stdin_text, cwd, env, timeout, harness):
        seen.update(argv=argv, prompt=stdin_text, launch_cwd=cwd)
        return {"returncode": 0, "stdout": _codex_stdout("완료"),
                "stderr": "", "timed_out": False}

    _run_attempt(
        pd, harness="codex", role="code-reviewer", cwd=repo, tmp_path=tmp_path,
        run_fn=_capture,
    )
    assert "검토 대상이 아니다" not in seen["prompt"]
    assert seen["argv"][seen["argv"].index("-C") + 1] == str(repo)   # 실행측 -C 의미도 --cwd
    assert seen["launch_cwd"] == str(repo)
