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
`delegate.enabled=true` 로 monkeypatch 하고 `--harness/--model` CLI override 로 타깃을 지정한다 —
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
import inspect
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import (
    codex_auth_available, current_branch, drop_codex_auth, make_codex_home,
    write_cluster_ledger,
)

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

# T-0685 선택 cross 3경로의 실제 target×role 티켓 영속성. main 차원은 cross CLI의 argv/driver를
# 바꾸지 않으므로 기존 27셀 기계표가 맡고, live는 사용 경로별 실제 target transport를 1회씩 친다.
_CROSS_MAIN_FOR_TARGET = {"codex": "claude", "opencode": "claude", "claude": "codex"}
_GROWTH_PIPELINE = (
    ("architect", "LIVE_CROSS_ARCHITECT_PERSISTED"),
    ("developer", "LIVE_CROSS_DEVELOPER_PERSISTED"),
    ("code-reviewer", "LIVE_CROSS_REVIEWER_PERSISTED"),
    ("developer", "LIVE_CROSS_FINAL_FIX_PERSISTED"),
)
_CROSS_TEST_FILE = "tests/test_live_cross_growth.py"
_CROSS_TEST_COMMAND = f"python3 -m pytest {_CROSS_TEST_FILE} -q -n auto"
_CROSS_FULL_COMMAND = "python3 -m pytest tests/ -q -n auto"

# 이 파일 release 마커 수(file-local pin·마커 소실/개명 방어). 전역 커플드-pin 등재는 orchestrator 소유.
_EXPECTED_RELEASE_TESTS = 4

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
              reasoning: str | None, output_dir: Path, timeout: int, *,
              role: str = "researcher", ticket: str | None = None,
              resume_from: str | None = None) -> tuple[int, str, str]:
    """pm_delegate.main() 을 in-process 로 호출(local_config=enabled monkeypatch·실 run_fn 스폰).

    reply 는 stdout(print)로 나오므로 capsys 로 회수한다. rc·reply·stderr 반환."""
    monkeypatch.setattr(pd, "local_config", lambda: {"delegate.enabled": "true"})
    argv = ["--role", role, "--prompt-file", str(prompt), "--cwd", str(repo),
            "--harness", harness, "--model", model, "--output-dir", str(output_dir),
            "--timeout", str(timeout)]
    if reasoning:
        argv += ["--reasoning", reasoning]
    if ticket:
        argv += ["--ticket", ticket]
    if resume_from:
        argv += ["--resume-from", resume_from]
    rc = pd.main(argv)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _growth_ticket_text(ticket: str) -> str:
    """라운드 모델의 명세 파일 — 역할 산출 자리가 본문에 없다(라운드는 prepare 가 예약한다)."""
    text = (
        "---\n"
        f"id: {ticket}\n"
        "title: live cross growth\n"
        "status: claimed\n"
        "created: '2026-08-14'\n"
        "created_by: live\n"
        "claimed_by: live/slot\n"
        "claimed_at: '2026-08-14T00:00:00+00:00'\n"
        "completed_at: null\n"
        "depends_on: []\nblocks: []\ntouches: []\nestimate: small\n"
        "design: done\ntags: []\n---\n"
        f"# {ticket} — live cross growth\n\n## 목표\nrole round persistence\n\n"
        "## 설계\n"
        "- **경계 실측**: 라이브 전송 픽스처\n"
        "- **불변식**: 이 파일의 축 밖\n"
        "- **표면 상한**: 픽스처 1건\n"
        "- **테스트 전략**: 정상·실패 경로\n"
        "\nOUTSIDE_MARKER_MUST_STAY\n"
    )
    return text


def _seed_growth_repo(tmp_path: Path, ticket: str) -> tuple[Path, Path]:
    """실 cross target이 편집할 slot+canonical PM-home을 한 throwaway git repo로 세운다."""
    repo, _unused = _seed_repo(tmp_path)
    tools = repo / ".project_manager" / "tools"
    tools.mkdir(parents=True)
    for source in TOOLS.glob("*.py"):
        shutil.copy2(source, tools / source.name)
    tickets = repo / ".project_manager" / "wiki" / "tickets" / "claimed"
    tickets.mkdir(parents=True)
    (repo / ".project_manager" / ".local").mkdir(parents=True)
    (repo / ".project_manager" / "local.conf").write_text(
        f"runtime.py=python3\ntest.cmd={_CROSS_FULL_COMMAND}\n",
        encoding="utf-8",
    )
    # 사본 루트(.local/)는 tracked `.project_manager/.gitignore` 로 무시돼야 prepare 가 받는다
    # (채택자 출하 형상과 같게 — ignore 규칙 출처 검증).
    ignore = repo / ".project_manager" / ".gitignore"
    ignore.write_text(".local/\n", encoding="utf-8")
    if _GIT is not None:
        subprocess.run([_GIT, "-C", str(repo), "add", ".project_manager/.gitignore"], check=True)
        subprocess.run([_GIT, "-C", str(repo), "commit", "-q", "-m", "seed ignore"], check=True)
    source = tickets / f"{ticket}-live-cross-growth.md"
    source.write_text(_growth_ticket_text(ticket), encoding="utf-8")
    # 라운드 준비는 묶음 장부의 예산·기준 브랜치 선언만 읽는다 — 크기 1 장부를 함께 깐다.
    write_cluster_ledger(
        repo / ".project_manager" / "wiki", ticket, base_branch=current_branch(repo))
    return repo, source



def _stage_reviewable_change(repo: Path) -> None:
    """리뷰 라운드의 실 입력 — 리뷰할 staged 변경 1건(읽기 역할 preflight 가 요구하는 그 값)."""
    reviewed = repo / "reviewed.txt"
    reviewed.write_text("live cross review target\n", encoding="utf-8")
    subprocess.run([_GIT, "-C", str(repo), "add", "reviewed.txt"], check=True)


def _record_growth_review_disposition(pd, source: Path, ticket: str) -> str:
    """PM 역할을 fixture에서 수행해 zero/finding reviewer 산출을 04 입력으로 닫는다."""
    ticket_text = source.read_text(encoding="utf-8")
    rounds = pd._load_ticket_rounds().load_rounds(
        source.parent.parent, ticket, ticket_text=ticket_text,
    )
    rendered = pd.render_pm_review_disposition_template(ticket_text, rounds)
    lines = rendered.splitlines()
    payload = json.loads("\n".join(lines[1:-1]))
    for row in payload.get("dispositions", []):
        row.update({
            "decision": "accepted",
            "reason": "live reviewer가 찾은 현재 티켓 결함을 final fix에서 닫는다",
            "scope": "live_cross_probe.py와 tests/test_live_cross_growth.py",
            "prerequisite": "없음",
        })
    block = (
        f"```{pd.PM_REVIEW_DISPOSITION_BLOCK}\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n```\n"
    )
    source.write_text(ticket_text + "\n" + block, encoding="utf-8", newline="")
    updated = source.read_text(encoding="utf-8")
    updated_rounds = pd._load_ticket_rounds().load_rounds(
        source.parent.parent, ticket, ticket_text=updated,
    )
    return pd.render_pm_review_delta(
        ticket, pd.parse_pm_review_delta(updated, updated_rounds),
    )

def _run_cross_growth_route(pd, monkeypatch, capsys, tmp_path: Path, *,
                            target: str, model: str, reasoning: str,
                            timeout: int) -> None:
    """선택 main→target 한 경로에서 고정 4라운드를 실행하고 canonical 재조회를 단언한다."""
    ticket = {"codex": "T-9101", "opencode": "T-9102", "claude": "T-9103"}[target]
    repo, source = _seed_growth_repo(tmp_path, ticket)
    er = pd._load_additional_reviewer()
    monkeypatch.setattr(pd, "check_local_conf_divergence", lambda *_a, **_k: (repo, None, er))
    monkeypatch.setattr(pd, "_cwd_in_git_repo", lambda *_a, **_k: True)
    output_dir = tmp_path / "raw"
    rounds_dir = (
        repo / ".project_manager" / "wiki" / "tickets" / "rounds" / ticket
    )
    fix_delta = ""

    for stage, (role, sentinel) in enumerate(_GROWTH_PIPELINE, start=1):
        prompt = repo / f"growth-{target}-{stage:02d}-{role}.md"
        if role == "architect":
            payload = json.dumps({
                "version": pd.ARCHITECT_TEST_VERSION,
                "tests": [{
                    "id": "AT-001",
                    "target": _CROSS_TEST_FILE,
                    "command": _CROSS_TEST_COMMAND,
                    "expected": "passed",
                    "negative": "pipeline_roles에서 마지막 developer를 빼면 회귀가 실패해야 한다",
                }],
            }, ensure_ascii=False, separators=(",", ":"))
            architect_body = (
                "## 경계 실측\n- fixed release pipeline fixture\n\n"
                "## 불변식\n- architect then developer then code-reviewer then final developer\n\n"
                "## 표면 상한\n- one stable module and one stable regression test\n\n"
                "## 테스트 전략\n- positive sequence and missing-final-developer negative\n\n"
                f"```{pd.ARCHITECT_TEST_BLOCK}\n{payload}\n```\n\n"
                "검토 판정: 설계 통과\n"
                f"{sentinel}\n"
            )
            edit_contract = (
                "Use replacement/truncation, never append: preserve the first header line and make every "
                "byte from line 2 through EOF equal the exact body between BEGIN/END below.\n"
                f"BEGIN EXACT ARCHITECT BODY\n{architect_body}END EXACT ARCHITECT BODY\n"
                "Mandatory self-check before replying: reopen the file and verify the exact body equality, "
                f"exactly one ```{pd.ARCHITECT_TEST_BLOCK} block, exactly one `{sentinel}`, and zero `<...>` "
                "placeholder tokens. If the old skeleton follows the sentinel or any check fails, rewrite "
                "from line 2 through EOF and reread it again. Do not reply before every check passes. "
            )
            final_contract = "After the edit, reply exactly DONE.\n"
        elif role == "code-reviewer":
            # 리뷰 입력은 리뷰할 diff 가 있는 트리다 — developer 회수가 작업물을 커밋한 뒤라
            # 리뷰 직전에 staged 변경을 하나 둔다(codex read 역할 preflight 의 실 입력).
            _stage_reviewable_change(repo)
            zero_payload = json.dumps(
                {"version": pd.PM_REVIEW_VERSION, "findings": [], "confirmations": []},
                ensure_ascii=False, separators=(",", ":"),
            )
            edit_contract = (
                "Inspect live_cross_probe.py and tests/test_live_cross_growth.py and run the targeted "
                f"command `{_CROSS_TEST_COMMAND}`. Do not invent a finding. If the stable four-role "
                "contract is correct, replace everything after the first header with `## must-fix`, "
                "`- 없음`, `## 판정`, `판정: 통과 · finding 0건(must-fix 0건)`, then the exact "
                f"```{pd.PM_REVIEW_BLOCK} block payload `{zero_payload}`, and finally `{sentinel}`. "
                "If a real defect exists instead, fill the seeded F-001 v3 schema without adding keys: "
                "include code evidence and every fix_contract field; fix_contract.test must name "
                f"{_CROSS_TEST_FILE}, command must be `{_CROSS_TEST_COMMAND}`, expected must be `passed`. "
            )
            final_contract = "After the edit, reply with the same 판정 and must-fix summary.\n"
        elif stage == 2:
            edit_contract = (
                "Implement the architect contract using stable canonical files only. Create "
                "live_cross_probe.py with a function pipeline_roles() returning exactly "
                "('architect', 'developer', 'code-reviewer', 'developer'). Create "
                f"{_CROSS_TEST_FILE} which imports that function and asserts that exact tuple. "
                "Do not use a random value, run id, temporary delegate-copy path, or session hash. Run "
                f"`{_CROSS_TEST_COMMAND}` and then `{_CROSS_FULL_COMMAND}`. Only after both actually return "
                "rc=0, replace the round body after its first header with completed sections in this exact "
                "order: `## 변경 파일`, `## 신규 테스트`, `## 회귀`, `## DoD evidence`, `## 민감도`, "
                "then the sentinel. The `## 회귀` section must contain exactly two nonblank "
                f"rows: `- 커맨드: `{_CROSS_FULL_COMMAND}`` and `- 결과: rc=0 · <the actual pytest "
                "summary you just observed>`. Put targeted-command evidence only under `## DoD evidence`. "
                "Mandatory self-check before replying: reopen the file, extract `## 회귀` through the next "
                "`## ` heading, and refuse to finish unless it has exactly those two rows, contains the full "
                "command once, and contains neither the targeted command nor the sentinel. Also verify the "
                f"whole body contains `{sentinel}` exactly once. Rewrite and reread if any check fails. "
                "Never fabricate a result. "
            )
            final_contract = "After the edit and real green commands, reply exactly DONE.\n"
        else:
            edit_contract = (
                "This is terminal round 04, not another initial implementation. Apply every accepted-only "
                "delta below; if it is empty, make no code change. For each accepted finding, modify or add "
                f"{_CROSS_TEST_FILE} as its regression target and fill every preseeded pm-review-verify-v1 "
                "row with a real boolean and the exact executed command/expected/before values. Run "
                f"`{_CROSS_TEST_COMMAND}` and then `{_CROSS_FULL_COMMAND}`. Only after both actually return "
                "rc=0, replace all remaining developer skeleton placeholders and keep any verify block. "
                "Write one final body with the normal completed sections in this exact order: `## 변경 파일`, "
                "`## 신규 테스트`, `## 회귀`, `## DoD evidence`, `## 민감도`, then an existing "
                "verify block if one was seeded, then the sentinel. The `## 회귀` section itself must be exactly "
                f"`- 커맨드: `{_CROSS_FULL_COMMAND}`` followed by `- 결과: rc=0 · <the one actual full "
                "pytest summary>` and no third nonblank row. Put targeted-command evidence only under "
                "`## DoD evidence`. Mandatory self-check before replying: reopen the file, extract `## 회귀` "
                "through the next `## ` heading, and refuse to finish unless it has exactly those two rows, "
                f"contains `{_CROSS_FULL_COMMAND}` once, contains neither `{_CROSS_TEST_COMMAND}` nor any "
                "placeholder, and the whole body contains the sentinel exactly once. Rewrite and reread if "
                "any check fails. Append "
                f"`{sentinel}`. Never fabricate counts and never create another round or ticket.\n"
                "ACCEPTED-ONLY DELTA:\n" + (fix_delta or "(empty: reviewer finding zero accepted)\n")
            )
            final_contract = "After the edit, reply with exactly DONE.\n"
        prompt.write_text(
            "The delegation preamble gives one absolute writable round file path. Open that file, keep its "
            "first header line. " + edit_contract
            + "The spec.md and rounds/ directory next to it are read-only. "
            + final_contract,
            encoding="utf-8",
        )
        spec_before_stage = source.read_bytes()
        rc, reply, err = _delegate(
            pd, monkeypatch, capsys, repo, prompt, target, model, reasoning,
            output_dir, timeout, role=role, ticket=ticket,
            resume_from=ticket if stage == 4 and role == "developer" else None,
        )
        tail = f"\n--- stderr ---\n{err[-1800:]}\n--- reply ---\n{reply[-1000:]}"
        assert rc == 0, (
            f"{_CROSS_MAIN_FOR_TARGET[target]}→{target} {role} rc={rc}" + tail
        )
        if target == "opencode":
            assert "Falling back to default agent" not in err + reply, (
                "OpenCode custom 역할이 default agent로 강등됨" + tail
            )
        # 회수 대상은 `NN-<역할>.md` 하나다 — 이름 문법의 단일 진실은 엔진(ticket_rounds)이고
        # 여기서는 그 형식으로 찾아 내용만 단언한다.
        role_rounds = sorted(rounds_dir.glob(f"*-{role}.md"))
        assert role_rounds, (
            f"{_CROSS_MAIN_FOR_TARGET[target]}→{target} {role} 라운드 파일 부재: {rounds_dir}" + tail
        )
        texts = [path.read_text(encoding="utf-8") for path in role_rounds]
        assert any(sentinel in text for text in texts), (
            f"{_CROSS_MAIN_FOR_TARGET[target]}→{target} {role} harvest sentinel 부재" + tail
        )
        assert any(text.splitlines()[0].startswith("## ") and f"({role} · " in text.splitlines()[0]
                   for text in texts), (
            f"{_CROSS_MAIN_FOR_TARGET[target]}→{target} {role} 라운드 첫 줄 헤더 소실" + tail
        )
        # 각 subagent에게 명세는 읽기 전용이다. reviewer 뒤 PM fixture만 별도로 disposition을 쓴다.
        assert source.read_bytes() == spec_before_stage, (
            f"{_CROSS_MAIN_FOR_TARGET[target]}→{target} {role} 위임이 명세 파일을 변경함: {source}" + tail
        )
        assert list(output_dir.glob(f"pm_delegate_{target}_*.txt")), "raw 감사 산출물 부재"
        if role == "code-reviewer":
            fix_delta = _record_growth_review_disposition(pd, source, ticket)

    # 새 Python 프로세스가 고정 4라운드(동일 developer 02/04)를 다시 읽어야 영속 증거다.
    probe = subprocess.run(
        [shutil.which("python3") or "python3", "-c",
         "from pathlib import Path; "
         "print(''.join(p.read_text(encoding='utf-8') "
         "for p in sorted(Path(r'%s').iterdir())))" % rounds_dir],
        cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    assert probe.returncode == 0, probe.stderr
    assert all(sentinel in probe.stdout for _role, sentinel in _GROWTH_PIPELINE)
    round_roles = [path.stem.split("-", 1)[1] for path in sorted(rounds_dir.glob("*.md"))]
    assert round_roles == [role for role, _sentinel in _GROWTH_PIPELINE]
    # subagent는 명세를 건드리지 않고 PM disposition만 추가했으며 원래 marker는 보존된다.
    assert source.read_text(encoding="utf-8").count("OUTSIDE_MARKER_MUST_STAY") == 1


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

    `--file` 프롬프트 채널·`--dir` cwd 핀·`--agent researcher`(런타임 role config로
    edit/bash deny)·`--variant high`(passthrough)."""
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

    `--tools Read,Glob,Grep,Edit`(researcher=Bash/Write 제외·Edit 는 티켓 사본 자기 절
    기록 전용·T-0696)·`--effort low`(drive)·cwd 존중."""
    pd = _load_pd()
    repo, prompt = _seed_repo(tmp_path)
    out_dir = tmp_path / "raw"
    rc, reply, err = _delegate(pd, monkeypatch, capsys, repo, prompt, "claude",
                               CLAUDE_MODEL, "low", out_dir, _CLAUDE_TIMEOUT)
    _assert_delegate_ok(rc, reply, err, out_dir, "claude")


@pytest.mark.skipif(
    not PM_DELEGATE_LIVE or not shutil.which("claude"),
    reason="pm_delegate 세션 재사용 라이브(claude) — PM_DELEGATE_LIVE=1 + claude CLI(API 과금) 필요. "
           "기본 skip·on-demand.",
)
def test_delegate_live_claude_resume_round(tmp_path, monkeypatch, capsys):
    """실 경로 2라운드 — R1 fresh 스폰 → `--resume-from` 으로 **같은 세션** 이어받기(T-0595 DoD).

    R1 은 marker 인용까지 완주해 장부에 세션 id 를 남기고, R2 는 그 레코드 id 로 재개해 delta 만
    보낸다. 판정은 세 가지다: R2 회신 세션 id 가 R1 과 같은가(장부 `resume_matched`), delta 가
    실제로 나갔는가(R2 raw argv 에 재개 플래그), 그리고 **이어받은 컨텍스트로 답했는가**(R2
    프롬프트는 README 를 다시 읽으라고 하지 않는데 marker 를 그대로 말한다 — fresh 세션이면 못
    한다). 미일치면 엔진이 fresh + full payload 로 재실행하므로 라운드 수(장부 행)로도 갈린다."""
    pd = _load_pd()
    repo, prompt = _seed_repo(tmp_path)
    out_dir = tmp_path / "raw"
    rc, reply, err = _delegate(pd, monkeypatch, capsys, repo, prompt, "claude",
                               CLAUDE_MODEL, "low", out_dir, _CLAUDE_TIMEOUT)
    _assert_delegate_ok(rc, reply, err, out_dir, "claude")

    ledger = out_dir / "raw_outputs.json"
    rows = pd._load_relay().raw_records(ledger)
    first = rows[0]
    assert first.get("session_id"), f"R1 장부에 세션 id 부재 — 재개 불가.\n{first}"
    assert first.get("usage"), f"R1 장부에 usage 분해 부재.\n{first}"

    # R2 프롬프트는 README 를 다시 읽으라고 말하지 않는다 — 이어받은 세션만 답할 수 있는 질문이다.
    followup = repo / "resume_prompt.txt"
    followup.write_text(
        "방금 네가 요약한 그 저장소에 대해 한 줄만 더 답하라. 방금 인용했던 교정 코드"
        "(calibration code)를 그대로 다시 적고, 재고 스냅샷 주기를 한 단어로 덧붙여라. "
        "파일을 새로 읽지 말고 이미 아는 내용으로만 답하라.",
        encoding="utf-8",
    )
    monkeypatch.setattr(pd, "local_config", lambda: {"delegate.enabled": "true"})
    rc2 = pd.main([
        "--role", "researcher", "--prompt-file", str(followup), "--cwd", str(repo),
        "--harness", "claude", "--model", CLAUDE_MODEL, "--reasoning", "low",
        "--output-dir", str(out_dir), "--timeout", str(_CLAUDE_TIMEOUT),
        "--resume-from", first["id"],
    ])
    captured = capsys.readouterr()
    tail = (f"\n--- stderr(tail) ---\n{captured.err[-1500:]}"
            f"\n--- reply(tail) ---\n{captured.out[-800:]}")
    assert rc2 == 0, f"resume 라운드 rc={rc2}" + tail
    assert "세션 재사용 미적용" not in captured.err, "재개 시도 자체가 서지 않았다" + tail
    assert "세션 재사용 실패" not in captured.err, (
        "회신 세션 id 가 요청과 달라 fresh 로 폴백했다(재개 미성립)" + tail)
    assert _MARKER in captured.out, (
        "resume 회신에 marker 부재 — 이어받은 컨텍스트로 답하지 못했다(파일 재읽기 금지 지시)" + tail)

    resumed = [row for row in pd._load_relay().raw_records(ledger)
               if row.get("attempt") == pd.RESUME_ATTEMPT]
    assert len(resumed) == 1, f"재개 attempt 레코드 수 이상: {len(resumed)}" + tail
    assert resumed[0]["resume_matched"] is True, "장부가 세션 불일치를 기록" + tail
    assert resumed[0]["session_id"] == first["session_id"]
    raw_text = Path(resumed[0]["raw_path"]).read_text(encoding="utf-8")
    assert first["session_id"] in raw_text, "재개 argv 가 raw 감사 헤더에 없다" + tail


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


@pytest.mark.release
@pytest.mark.skipif(
    not _RELEASE_LIVE or not shutil.which("codex") or not codex_auth_available(),
    reason="선택 cross Claude→Codex 성장 — PM_ORCH_LIVE_RELEASE=1 + codex auth 필요.",
)
def test_ticket_growth_cross_claude_to_codex_release(tmp_path, monkeypatch, capsys):
    """Claude main이 쓰는 cross Codex transport에서 성장 역할 3종을 실제 왕복한다."""
    pd = _load_pd()
    home = make_codex_home(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(home))
    try:
        _run_cross_growth_route(
            pd, monkeypatch, capsys, tmp_path, target="codex", model=CODEX_MODEL,
            reasoning="low", timeout=_CODEX_TIMEOUT,
        )
    finally:
        drop_codex_auth(home)


@pytest.mark.release
@pytest.mark.skipif(
    not _RELEASE_LIVE or not shutil.which("opencode"),
    reason="선택 cross Claude→OpenCode 성장 — PM_ORCH_LIVE_RELEASE=1 + opencode 필요.",
)
def test_ticket_growth_cross_claude_to_opencode_release(tmp_path, monkeypatch, capsys):
    """Claude main이 쓰는 cross OpenCode transport에서 성장 역할 3종을 실제 왕복한다."""
    _run_cross_growth_route(
        _load_pd(), monkeypatch, capsys, tmp_path, target="opencode", model=LIVE_MODEL,
        reasoning="high", timeout=_OPENCODE_TIMEOUT,
    )


@pytest.mark.release
@pytest.mark.skipif(
    not _RELEASE_LIVE or not shutil.which("claude"),
    reason="선택 cross Codex→Claude 성장 — PM_ORCH_LIVE_RELEASE=1 + claude 필요.",
)
def test_ticket_growth_cross_codex_to_claude_release(tmp_path, monkeypatch, capsys):
    """Codex main이 쓰는 cross Claude transport에서 성장 역할 3종을 실제 왕복한다."""
    _run_cross_growth_route(
        _load_pd(), monkeypatch, capsys, tmp_path, target="claude", model=CLAUDE_MODEL,
        reasoning="low", timeout=_CLAUDE_TIMEOUT,
    )


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


def test_cross_growth_fixture_pins_fixed_pipeline_and_parallel_full_command(tmp_path):
    """세 cross route가 공유하는 fixture는 developer 02/04와 exact stage-exit 명령을 고정한다."""
    assert [role for role, _sentinel in _GROWTH_PIPELINE] == [
        "architect", "developer", "code-reviewer", "developer",
    ]
    assert len({sentinel for _role, sentinel in _GROWTH_PIPELINE}) == 4
    assert _CROSS_TEST_FILE.startswith("tests/") and _CROSS_TEST_FILE.endswith(".py")
    assert _CROSS_TEST_COMMAND == (
        "python3 -m pytest tests/test_live_cross_growth.py -q -n auto"
    )
    repo, _source = _seed_growth_repo(tmp_path, "T-9199")
    conf_lines = (repo / ".project_manager" / "local.conf").read_text(
        encoding="utf-8"
    ).splitlines()
    assert "runtime.py=python3" in conf_lines
    assert "py=python3" not in conf_lines
    assert f"test.cmd={_CROSS_FULL_COMMAND}" in conf_lines
    route_source = inspect.getsource(_run_cross_growth_route)
    assert 'resume_from=ticket if stage == 4 and role == "developer" else None' in route_source
    assert "BEGIN EXACT ARCHITECT BODY" in route_source
    assert "exact body equality" in route_source
    assert "old skeleton follows the sentinel" in route_source
    assert "exactly those two rows" in route_source
    assert "targeted-command evidence only under" in route_source
    assert "no third nonblank row" in route_source
    assert "extract `## 회귀`" in route_source
    assert "contains neither" in route_source


def test_cross_stage2_regression_section_has_exact_boundary_and_negative_guard():
    """02 developer sentinel/targeted 증거가 exact two-row 회귀 절에 흡수되지 않는다."""
    route_source = inspect.getsource(_run_cross_growth_route)
    stage2_source = route_source.split("elif stage == 2:", 1)[1].split(
        "        else:", 1,
    )[0]
    section_order = (
        "`## 변경 파일`, `## 신규 테스트`, `## 회귀`, `## DoD evidence`, `## 민감도`"
    )

    assert section_order in stage2_source
    assert "extract `## 회귀` through the next" in stage2_source
    assert "contains neither the targeted command nor the sentinel" in stage2_source
    assert "targeted-command evidence only under `## DoD evidence`" in stage2_source
    assert "whole body contains" in stage2_source and "exactly once" in stage2_source


def test_delegate_forwards_resume_from_without_fresh(tmp_path, monkeypatch, capsys):
    """final-fix 재주입은 ticket resume 결속만 쓰고 fresh 우회를 만들지 않는다."""
    seen = []

    class StubDelegate:
        local_config = None

        @staticmethod
        def main(argv):
            seen.append(argv)
            return 0

    prompt = tmp_path / "prompt.md"
    prompt.write_text("final fix\n", encoding="utf-8")
    rc, _reply, _err = _delegate(
        StubDelegate(), monkeypatch, capsys, tmp_path, prompt, "opencode", "test-model",
        "high", tmp_path / "raw", 30, role="developer", ticket="T-9199",
        resume_from="T-9199",
    )

    assert rc == 0
    assert seen[0][seen[0].index("--resume-from") + 1] == "T-9199"
    assert "--fresh" not in seen[0]


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
