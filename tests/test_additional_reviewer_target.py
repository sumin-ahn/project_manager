"""추가 리뷰어 원자 tuple 대상 — 해소·argv·wire·provenance·egress 계약 (T-0590).

추가 리뷰어(external_review)가 자유 문자열 `reviewer_cmd` 대신 위임과 **동형의 원자 tuple**
(`additional_reviewer.{harness,model,reasoning}`)로 대상을 정하고, 세 하네스 CLI 를 위임과 **같은
공용 드라이버 계약**(pm_relay)으로 스폰하는 절의 회귀다. 이 파일이 닫는 축:

1. 원자 해소 — 키 존재 기준 선언 판정(빈 값도 선언)·harness/model 동반 필수·reasoning 선택·
   미지원 값 fail-loud·legacy 이중 선언 차단. 모든 거부는 output-dir·raw·라운드·격리·스폰·과금
   문구 **어느 것도 만들기 전**에 성립한다.
2. 세 구조화 argv — 모델/reasoning 명시 + 읽기 권위(code-reviewer) 권한축 불변.
3. opencode `--file` 프롬프트 파일 0600 + 성공·실패·예외 전 경로 정리.
4. 구조화 wire 회신 추출 — 판정은 최종 회신만 보고 raw 는 wire 원문을 보존, 추출 실패는 fail-loud.
5. legacy `reviewer_cmd` 실행 형상 바이트 동형 + `unpinned-model` loud 라벨.
6. provenance 동일성 — dry-run·stderr 첫 줄·raw 헤더·raw 장부가 **같은 문자열**을 말한다.
7. Codex egress 게이트(network-off 안전 경계) — 증명 없는 실행은 스폰 전에 끊고 `--force` 로도
   못 넘으며, dry-run 은 부작용 0 으로 처방만 낸다.
8. 모듈 경계 — pm_relay 는 cycle-free(다른 엔진 표면을 읽지 않는다)·external_review 는
   pm_delegate 를 import 하지 않는다·위임 공개 wrapper 는 T-0592 행동을 그대로 보존한다.
9. 문구 규율 — 사람 역할 이름은 **추가 리뷰어**, 상한은 anti-loop PM 자율 ack, 기계 식별자·전송
   문구는 불변.

hermetic: 외부 프로세스 스폰 0. 실행 경로 테스트는 tmp repo 를 REPO/PM 홈으로 주입하고
`_watchdog_reviewer_run`(기본 러너 seam)을 캡처 스텁으로 갈아 끼워 **실 argv/stdin** 을 본다 —
`run_review` 를 통째로 스텁하면 이 절이 소유한 argv·wire·raw 계약이 통과 없이 green 이 된다.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import shlex
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

_EGRESS_MARKER = "CODEX_SANDBOX_NETWORK_DISABLED"


def _load(name: str):
    """도구 모듈을 (패키지 아님) importlib 로 경로 로드 — sibling 테스트 동일 규약."""
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def external():
    return _load("external_review")


@pytest.fixture
def relay():
    return _load("pm_relay")


@pytest.fixture(autouse=True)
def _neutral_codex_egress_marker(monkeypatch):
    """ambient Codex egress 마커 중화 — 승격 명령에서도 마커는 남는 실측(T-0592).

    Codex 세션에서 회귀를 돌리면 이 마커가 pytest env 로 새어 실행 경로 테스트가 통째로 승격
    게이트에 걸린다. 마커를 *쓰는* 테스트만 명시로 켠다."""
    monkeypatch.delenv(_EGRESS_MARKER, raising=False)


# ── 형상 헬퍼 ──────────────────────────────────────────────────────────────


def _conf(harness: str = "codex", model: str = "gpt-5.6-sol",
          reasoning: str | None = "max", **extra: str) -> dict[str, str]:
    conf = {
        "additional_reviewer_enabled": "true",
        "additional_reviewer.harness": harness,
        "additional_reviewer.model": model,
    }
    if reasoning is not None:
        conf["additional_reviewer.reasoning"] = reasoning
    conf.update(extra)
    return conf


def _repo(root: Path, conf: dict[str, str]) -> Path:
    """tmp PM 홈 = diff 앵커 — 실 local.conf 파일을 둬 절대경로 provenance 를 그대로 태운다."""
    pm = root / ".project_manager"
    pm.mkdir(parents=True, exist_ok=True)
    (pm / "local.conf").write_text(
        "".join(f"{key}={value}\n" for key, value in conf.items()), encoding="utf-8")
    return root


_PASS_REPLY = "판정: 통과\n\n**must-fix**:\n- 없음\n"
# 회신이 아닌 wire 이벤트(진행 로그·추론 항목)에 심는 미끼. 이 문자열이 판정에 닿으면
# `_REJECT_TOKENS` 가 걸려 통과가 뒤집히므로, all_pass 가 그대로면 "wire 는 판정에 안 들어간다".
_DECOY = "판정: 반려 / must-fix 미끼 — 회신 채널이 아니다"


def _wire(harness: str, reply: str = _PASS_REPLY, *, with_reply: bool = True) -> str:
    """하네스별 구조화 wire(JSONL) — 미끼 이벤트 + 최종 회신 이벤트."""
    if harness == "codex":
        events = [
            {"type": "thread.started", "thread_id": "t-1"},
            {"type": "item.completed", "item": {"type": "reasoning", "text": _DECOY}},
        ]
        if with_reply:
            events.append(
                {"type": "item.completed",
                 "item": {"type": "agent_message", "text": reply}})
        events.append({"type": "turn.completed", "usage": {"input_tokens": 7}})
    elif harness == "claude":
        events = [
            {"type": "system", "subtype": "init", "session_id": "s-1"},
            {"type": "assistant",
             "message": {"content": [{"type": "text", "text": _DECOY}]}},
        ]
        if with_reply:
            events.append({"type": "result", "result": reply})
    else:
        events = [
            {"type": "tool", "sessionID": "ses-1", "part": {"text": _DECOY}},
        ]
        if with_reply:
            events.append(
                {"type": "text", "sessionID": "ses-1",
                 "part": {"type": "text", "text": reply}})
    return "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events)


class _FakeReviewer:
    """기본 러너 seam 대체 — 실 argv/stdin/cwd 를 캡처하고 wire 를 돌려준다.

    `_reviewer_run_kwargs` 가 시그니처를 introspect 해 `idle_timeout` 전달 여부를 정하므로
    실제 러너와 같은 키워드를 선언한다(스텁이 seam 계약을 우회하지 않게)."""

    def __init__(self, stdout: str = _PASS_REPLY, stderr: str = "", rc: int = 0):
        self.stdout, self.stderr, self.rc = stdout, stderr, rc
        self.calls: list[dict] = []

    def __call__(self, argv, *, input=None, timeout=None, idle_timeout=None,
                 cwd=None, env=None, **_ignored):
        call = {
            "argv": list(argv), "input": input, "cwd": cwd, "env": env,
            "timeout": timeout, "idle_timeout": idle_timeout,
        }
        prompt_file = self._prompt_file(list(argv))
        if prompt_file is not None:
            call["prompt_file"] = prompt_file
            call["prompt_exists"] = prompt_file.exists()
            call["prompt_text"] = (
                prompt_file.read_text(encoding="utf-8") if prompt_file.exists() else None)
            call["prompt_mode"] = (
                stat.S_IMODE(prompt_file.stat().st_mode) if prompt_file.exists() else None)
        self.calls.append(call)
        return subprocess.CompletedProcess(argv, self.rc, self.stdout, self.stderr)

    @staticmethod
    def _prompt_file(argv: list[str]) -> Path | None:
        return Path(argv[argv.index("--file") + 1]) if "--file" in argv else None


def _wire_main(external, monkeypatch, repo: Path, reviewer: _FakeReviewer | None = None):
    """main() 을 tmp repo 로 격리 배선한다 — 앵커·diff·거울만 스텁하고 그 아래는 실 경로."""
    monkeypatch.setattr(external, "REPO", repo)
    monkeypatch.setattr(
        external, "resolve_pm_home_for_repo", lambda anchor, **kwargs: repo)
    monkeypatch.setattr(external, "_resolve_diff_root", lambda *a, **k: repo)
    monkeypatch.setattr(
        external, "extract_diff",
        lambda *a, **k: ("diff --git a/x.py b/x.py\n-old\n+new\n", []))
    monkeypatch.setattr(
        external, "create_reviewer_workspace",
        lambda diff_root, *, base_dir=None, conf=None, source_home=None, denylist=():
        external.ReviewerWorkspace(
            root=Path(tempfile.mkdtemp(prefix="stub_reviewer_mirror_")),
            tree=Path(tempfile.mkdtemp(prefix="stub_reviewer_tree_")),
            home=Path(tempfile.mkdtemp(prefix="stub_reviewer_home_")),
            files=1, skipped_unsafe=0, git_repo=True,
        ))
    if reviewer is not None:
        monkeypatch.setattr(external, "_watchdog_reviewer_run", reviewer)
    return reviewer


def _count_isolation(external, monkeypatch) -> list[Path]:
    """격리 seam **진입**을 센다 — 실 `reviewer_visibility_scope` 를 그대로 감싼 카운터.

    거울 생성 스텁(`_wire_main`)만 세면 "무엇을 만들었나"는 보이지만 "seam 에 들어갔나"는 안
    보인다. 진입 자체가 컨테이너 생성·정리 왕복의 시작이라 여기서 센다."""
    entered: list[Path] = []
    real_scope = external.reviewer_visibility_scope

    def _counting(diff_root, **kwargs):
        entered.append(Path(diff_root))
        return real_scope(diff_root, **kwargs)

    monkeypatch.setattr(external, "reviewer_visibility_scope", _counting)
    return entered


def _round_ledger(repo: Path) -> dict:
    path = repo / ".project_manager" / ".local" / "review_rounds.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _raw_ledger(repo: Path) -> list[dict]:
    path = repo / ".project_manager" / ".local" / "raw_outputs.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))["records"]


def _raw_text(repo: Path) -> str:
    review_dir = repo / ".project_manager" / ".local" / "review"
    files = sorted(review_dir.glob("external_review_*.txt"))
    assert len(files) == 1, files
    return files[0].read_text(encoding="utf-8")


# ══ ① 원자 해소 (부작용 전 fail-loud) ═══════════════════════════════════════


def test_no_structured_key_keeps_the_legacy_target(external):
    """구조화 키가 **하나도 없으면** 종전 경로 — 기본 커맨드/설정 자유 문자열 그대로."""
    default = external.resolve_reviewer_target({})
    assert (default.source, default.command) == (
        external.REVIEWER_SOURCE_LEGACY, external.DEFAULT_REVIEWER_CMD)
    configured = external.resolve_reviewer_target({"reviewer_cmd": "codex exec -m m1"})
    assert (configured.source, configured.command) == (
        external.REVIEWER_SOURCE_LEGACY, "codex exec -m m1")
    assert configured.structured is False


@pytest.mark.parametrize("declared", [
    {"additional_reviewer.harness": ""},
    {"additional_reviewer.model": ""},
    {"additional_reviewer.reasoning": ""},
    {"additional_reviewer.harness": "", "additional_reviewer.model": ""},
])
def test_blank_declared_key_never_falls_back_to_legacy(external, declared):
    """선언 판정 기준은 **키 존재**다 — 비운 채 선언한 부분 tuple 은 legacy 로 떨어지지 않는다.

    truthiness 로 판정하면 `additional_reviewer.harness=` 가 조용히 기본 커맨드로 나가, 사용자가
    지정한 것과 다른 대상이 코드를 받는다."""
    with pytest.raises(external.ReviewerTargetError) as caught:
        external.resolve_reviewer_target(declared)
    assert "불완전" in str(caught.value)


@pytest.mark.parametrize("missing", [
    "additional_reviewer.harness", "additional_reviewer.model",
])
def test_partial_tuple_names_the_missing_key(external, missing):
    """harness/model 은 동반 필수 — 빠진 키를 이름으로 지목하고 조용한 기본값을 쓰지 않는다."""
    conf = {"additional_reviewer.harness": "codex",
            "additional_reviewer.model": "gpt-5.6-sol"}
    conf[missing] = ""
    with pytest.raises(external.ReviewerTargetError) as caught:
        external.resolve_reviewer_target(conf)
    message = str(caught.value)
    assert missing in message
    assert external.ADDITIONAL_REVIEWER_REASONING_KEY in message  # 선택 축 안내


def test_reasoning_is_optional_and_validated(external):
    """reasoning 은 선택(생략=플래그 없음)이고, 주면 드라이버 허용집합으로 검증한다."""
    without = external.resolve_reviewer_target(
        {"additional_reviewer.harness": "codex", "additional_reviewer.model": "m"})
    assert without.reasoning is None
    assert "model_reasoning_effort" not in without.command

    blank = external.resolve_reviewer_target(
        {"additional_reviewer.harness": "codex", "additional_reviewer.model": "m",
         "additional_reviewer.reasoning": "  "})
    assert blank.reasoning is None                      # 빈 reasoning 은 축 생략(부분 tuple 아님)

    valid = external.resolve_reviewer_target(
        {"additional_reviewer.harness": "codex", "additional_reviewer.model": "m",
         "additional_reviewer.reasoning": "max"})
    assert valid.reasoning == "max"
    assert "-c model_reasoning_effort=max" in valid.command


@pytest.mark.parametrize("harness,reasoning,fragment", [
    ("gemini", None, "미지원 harness"),
    ("codex", "ultra", "허용집합"),
    ("opencode", "xhigh", "허용집합"),
    ("claude", "minimal", "허용집합"),
])
def test_unsupported_harness_or_reasoning_is_fail_loud(
        external, relay, harness, reasoning, fragment):
    """허용집합 밖 값은 조용한 무시/강등 없이 중단하고, 문구는 공용 계약(pm_relay)이 소유한다."""
    conf = {"additional_reviewer.harness": harness, "additional_reviewer.model": "m"}
    if reasoning is not None:
        conf["additional_reviewer.reasoning"] = reasoning
    with pytest.raises(external.ReviewerTargetError) as caught:
        external.resolve_reviewer_target(conf)
    message = str(caught.value)
    assert fragment in message
    # 같은 오설정이 두 표면에서 다르게 설명되지 않는다 — relay 문구를 그대로 감싼다.
    with pytest.raises(relay.HarnessContractError) as contract:
        if reasoning is None:
            relay.validate_harness(harness)
        else:
            relay.validate_reasoning(harness, reasoning)
    assert str(contract.value) in message


@pytest.mark.parametrize("model", [
    "default", "unpinned-model", "DEFAULT", "Unpinned-Model", " default ",
])
def test_reserved_sentinel_model_is_not_a_pinned_model(external, model):
    """예약 sentinel 은 "모델 미고정"을 뜻하는 낱말이라 **고정 선언의 값**이 될 수 없다.

    비어있지 않다는 것만 보면 `additional_reviewer.model=default` 가 통과해, "이 실행은 모델을
    고정했다"는 선언과 함께 미고정 라벨이 장부·raw 헤더·stderr 에 박힌다 — legacy 미고정 실행과
    글자 단위로 구분되지 않아 '어느 모델이 이 판정을 냈는가'가 사후에 닫히지 않는다."""
    with pytest.raises(external.ReviewerTargetError) as caught:
        external.resolve_reviewer_target(
            {"additional_reviewer.harness": "codex",
             "additional_reviewer.model": model})
    message = str(caught.value)
    assert "예약 sentinel" in message
    assert external.ADDITIONAL_REVIEWER_MODEL_KEY in message      # 어느 키를 고칠지
    assert external.LEGACY_REVIEWER_CMD_KEY in message            # 미고정을 원하면 갈 곳
    # 예약 집합은 엔진 자신이 쓰는 낱말들이다(따로 관리되는 사본이 아니다).
    assert external.RESERVED_MODEL_VALUES == {
        external.LEGACY_UNSPECIFIED_MODEL, external.UNPINNED_MODEL_LABEL}


@pytest.mark.parametrize("model", [
    "gpt-5.6-sol", "claude-opus-5", "zai/glm-4.6",
    "default-v2", "gpt-5-default", "unpinned-model-x",
])
def test_legitimate_model_names_are_not_swept_up_by_the_reserved_check(external, model):
    """거부 대상은 예약 낱말 **자체**뿐이다 — 그 문자열을 품은 실제 모델 이름은 그대로 통과한다."""
    target = external.resolve_reviewer_target(
        {"additional_reviewer.harness": "codex", "additional_reviewer.model": model})
    assert target.model == model
    assert target.ledger_model == model
    assert shlex.split(target.command)[
        shlex.split(target.command).index("-m") + 1] == model


def test_reserved_sentinel_model_is_refused_before_output_dir_raw_and_spawn(
        external, monkeypatch, tmp_path, capsys):
    """sentinel 거부는 output-dir 생성·raw 예약·격리·스폰 **어느 것보다도 앞**에서 성립한다."""
    repo = _repo(tmp_path / "repo", _conf(model="default"))
    reviewer = _FakeReviewer()
    _wire_main(external, monkeypatch, repo, reviewer)
    isolations: list[str] = []
    monkeypatch.setattr(external, "reviewer_visibility_scope",
                        lambda *a, **k: isolations.append("called"))
    outdir = tmp_path / "raw-out"

    rc = external.main(["--gate", "T-0590", "--paths", "x.py",
                        "--output-dir", str(outdir)])
    captured = capsys.readouterr()

    assert rc == 1
    assert reviewer.calls == []                                   # 외부 스폰 0
    assert isolations == []                                       # 격리 거울 0
    assert not outdir.exists()                                    # output-dir 생성 0
    assert not (repo / ".project_manager" / ".local").exists()    # raw·라운드 장부 0
    assert "추가 리뷰어 실행 중" not in captured.err                # 과금 문구 앞에서 끊긴다
    assert "예약 sentinel" in captured.err
    assert "추가 리뷰어 대상 해소 실패" in captured.err


def test_structured_and_nonblank_legacy_command_conflict(external):
    """대상이 둘이면 어느 쪽이 이기는지 추측해 외부로 보내지 않는다(빈 legacy 값은 선언 아님)."""
    structured = {"additional_reviewer.harness": "codex",
                  "additional_reviewer.model": "gpt-5.6-sol"}
    with pytest.raises(external.ReviewerTargetError) as caught:
        external.resolve_reviewer_target({**structured, "reviewer_cmd": "codex exec"})
    assert "대상이 둘" in str(caught.value)
    # 비어 있는 legacy 값은 선언이 아니다 — 구조화 tuple 이 정상 해소된다.
    assert external.resolve_reviewer_target(
        {**structured, "reviewer_cmd": "  "}).structured is True
    # 구조화 키가 비어 있어도 legacy 와의 이중 선언 판정은 같다.
    with pytest.raises(external.ReviewerTargetError):
        external.resolve_reviewer_target(
            {"additional_reviewer.harness": "", "reviewer_cmd": "codex exec"})


def test_resolution_failure_precedes_every_side_effect(
        external, monkeypatch, tmp_path, capsys):
    """잘못된 프로필은 output-dir·raw·라운드·격리 거울·스폰·과금 문구 **전부보다 앞**에서 끊긴다."""
    repo = _repo(tmp_path / "repo", _conf(reasoning="ultra"))
    reviewer = _FakeReviewer()
    _wire_main(external, monkeypatch, repo, reviewer)
    isolations: list[Path] = []
    monkeypatch.setattr(
        external, "reviewer_visibility_scope",
        lambda *a, **k: isolations.append(Path("called")))
    outdir = tmp_path / "raw-out"

    rc = external.main(["--gate", "T-0590", "--paths", "x.py",
                        "--output-dir", str(outdir)])
    err = capsys.readouterr().err

    assert rc == 1
    assert reviewer.calls == []                                  # 스폰 0
    assert isolations == []                                      # 격리 거울 0
    assert not outdir.exists()                                   # output-dir 생성 0
    assert not (repo / ".project_manager" / ".local").exists()   # raw·라운드 장부 0
    assert "추가 리뷰어 실행 중" not in err                        # 과금 문구 앞에서 끊긴다
    assert "과금" not in err
    assert "추가 리뷰어 대상 해소 실패" in err
    assert str(repo / ".project_manager" / "local.conf") in err   # 어느 conf 가 골랐나


# ══ ② 세 구조화 argv (명시 모델/reasoning · 읽기 권위 불변) ═════════════════


@pytest.mark.parametrize("harness,model,reasoning,expected", [
    ("codex", "gpt-5.6-sol", "max", [
        "codex", "-a", "never", "-s", "read-only", "exec", "--json",
        "--skip-git-repo-check", "-m", "gpt-5.6-sol",
        "-c", "model_reasoning_effort=max",
    ]),
    ("claude", "claude-opus-5", "high", [
        "claude", "-p", "--output-format", "stream-json", "--verbose",
        "--model", "claude-opus-5", "--tools", "Read,Glob,Grep,Bash",
        "--effort", "high",
    ]),
    ("opencode", "zai/glm-4.6", "medium", [
        "opencode", "run", "<msg>", "--file", "<prompt-file>",
        "--agent", "plan", "--format", "json", "--dir", "<isolated-cwd>",
        "-m", "zai/glm-4.6", "--variant", "medium",
    ]),
])
def test_structured_argv_pins_model_and_reasoning(
        external, relay, harness, model, reasoning, expected):
    """세 하네스 모두 모델·reasoning 을 **argv 에 명시**한다(기본값에 맡기지 않는다)."""
    expected = [relay.OPENCODE_ATTACHED_MSG if token == "<msg>" else token
                for token in expected]
    argv = external._structured_reviewer_argv(
        harness, model, reasoning,
        cwd=external.REVIEWER_CWD_PLACEHOLDER,
        prompt_file=external.REVIEWER_PROMPT_FILE_PLACEHOLDER,
    )
    assert argv == expected
    # 해소 결과의 `command` 는 이 argv 를 그대로 렌더한 문자열이다(세 표면 공통 정체).
    target = external.resolve_reviewer_target(
        {"additional_reviewer.harness": harness,
         "additional_reviewer.model": model,
         "additional_reviewer.reasoning": reasoning})
    assert target.command == shlex.join(expected)


@pytest.mark.parametrize("harness,forbidden,required", [
    ("codex", ("workspace-write",), ("-a", "never", "-s", "read-only")),
    ("claude", ("Write", "Edit", "--permission-mode", "acceptEdits"),
     ("--tools", "Read,Glob,Grep,Bash")),
    ("opencode", ("build",), ("--agent", "plan")),
])
def test_reviewer_permission_axis_is_immutable_read(
        external, harness, forbidden, required):
    """추가 리뷰어의 권한축은 읽기 권위(code-reviewer) 고정 — 설정으로 올릴 수 없다."""
    assert external.REVIEWER_ROLE == "code-reviewer"
    conf = {"additional_reviewer.harness": harness, "additional_reviewer.model": "m",
            # 역할/권한을 올리려는 설정 시도는 해소에 아무 영향이 없다(축이 코드 상수다).
            "additional_reviewer.role": "developer",
            "additional_reviewer.permission-mode": "acceptEdits"}
    argv = shlex.split(external.resolve_reviewer_target(conf).command)
    for token in forbidden:
        assert token not in argv, token
    for token in required:
        assert token in argv, token


def test_codex_max_reasoning_is_available_to_the_reviewer_profile(external, relay):
    """추가 리뷰어 기본 프로필이 쓰는 ladder 상단(codex `max`)이 허용집합에 등재돼 있다."""
    assert "max" in relay.REASONING_ALLOWED["codex"]
    target = external.resolve_reviewer_target(
        {"additional_reviewer.harness": "codex",
         "additional_reviewer.model": "gpt-5.6-sol",
         "additional_reviewer.reasoning": "max"})
    assert target.command.endswith("-c model_reasoning_effort=max")


def test_copied_home_config_defaults_cannot_change_argv_or_ledger(
        external, monkeypatch, tmp_path, capsys):
    """격리 홈에 복제된 사용자 config 기본값은 argv·장부를 못 바꾸고, tuple 모델은 둘 다 바꾼다.

    명시 플래그가 config 기본값을 이기는 구조라, 거울 홈에 어떤 `model=` 기본값이 복제돼도 이번
    실행의 수신자와 장부 기록은 tuple 이 정한 값 하나다."""
    def _run(model: str) -> tuple[list[str], dict]:
        repo = _repo(tmp_path / f"repo-{model}", _conf(model=model))
        reviewer = _FakeReviewer(stdout=_wire("codex"))
        with pytest.MonkeyPatch.context() as patch:
            _wire_main(external, patch, repo, reviewer)
            # 거울 홈에 다른 모델 기본값이 복제된 형상을 그대로 재현한다.
            home = repo / "mirror-home"
            (home / ".codex").mkdir(parents=True)
            (home / ".codex" / "config.toml").write_text(
                'model = "someone-elses-default"\n', encoding="utf-8")
            patch.setattr(
                external, "create_reviewer_workspace",
                lambda diff_root, *, base_dir=None, conf=None, source_home=None,
                denylist=(): external.ReviewerWorkspace(
                    root=repo / "mirror", tree=repo / "mirror-tree", home=home,
                    files=1, skipped_unsafe=0, git_repo=True))
            (repo / "mirror-tree").mkdir(parents=True, exist_ok=True)
            assert external.main(["--paths", "x.py"]) == 0
            capsys.readouterr()
        rows = _raw_ledger(repo)
        assert len(rows) == 1
        return reviewer.calls[0]["argv"], rows[0]

    first_argv, first_row = _run("gpt-5.6-sol")
    assert "someone-elses-default" not in first_argv
    assert first_argv[first_argv.index("-m") + 1] == "gpt-5.6-sol"
    assert first_row["model"] == "gpt-5.6-sol"

    second_argv, second_row = _run("gpt-5.6-terra")
    assert second_argv[second_argv.index("-m") + 1] == "gpt-5.6-terra"
    assert second_row["model"] == "gpt-5.6-terra"
    assert first_argv != second_argv and first_row["model"] != second_row["model"]


def test_structured_ledger_model_is_the_explicit_tuple_not_a_command_reading(
        external, monkeypatch):
    """구조화 모델은 커맨드 문자열에서 역추론하지 않는다 — 명시 모델이 `default` 로 퇴화 불가."""
    monkeypatch.setattr(external, "_reviewer_model", lambda cmd: "default")
    structured = external.resolve_reviewer_target(
        {"additional_reviewer.harness": "codex",
         "additional_reviewer.model": "gpt-5.6-sol"})
    assert structured.ledger_model == "gpt-5.6-sol"


def test_legacy_ledger_model_is_the_unpinned_identity_not_the_placeholder(external):
    """legacy 정체는 argv의 model처럼 보이는 토큰 유무와 무관하게 `unpinned-model`이다.

    임의 실행기의 옵션 스키마는 엔진이 보증하지 않는다. exact command는 별도 기록하므로 정보는
    보존하고, 모델 정체 보증은 구조화 tuple에만 부여한다."""
    unpinned = external.legacy_reviewer_target({})
    assert external._reviewer_model(unpinned.command) == "default"   # argv 표기 관측은 그대로
    assert unpinned.ledger_model == external.UNPINNED_MODEL_LABEL    # 정체는 미고정 라벨
    # 네 표면의 모델 축이 한 값에서 나온다.
    assert f"model={unpinned.ledger_model}" in unpinned.profile_tail
    assert (f"# model: {unpinned.ledger_model}"
            in external._review_raw_content("본문", None, None, unpinned, None))
    # `--model`처럼 보이는 토큰이 있어도 legacy command는 opaque하다.
    pinned = external.ReviewerTarget(
        external.REVIEWER_SOURCE_LEGACY, "codex exec --model gpt-x")
    assert external._reviewer_model(pinned.command) == "gpt-x"  # 표기 관측만 가능
    assert pinned.ledger_model == external.UNPINNED_MODEL_LABEL
    assert f"model={external.UNPINNED_MODEL_LABEL}" in pinned.profile_tail


# ══ ③ opencode 프롬프트 파일 (0600 · 전 경로 정리) ══════════════════════════


@pytest.mark.parametrize("rc", [0, 1])
def test_opencode_prompt_file_is_0600_and_removed_after_success_and_failure(
        external, monkeypatch, tmp_path, capsys, rc):
    """`--file` 프롬프트에는 diff 원문이 들어간다 — 0600 으로 만들고 rc 무관 지운다."""
    repo = _repo(tmp_path / "repo", _conf(harness="opencode", model="zai/glm-4.6",
                                          reasoning="medium"))
    reviewer = _FakeReviewer(stdout=_wire("opencode"), rc=rc)
    _wire_main(external, monkeypatch, repo, reviewer)

    external.main(["--paths", "x.py"])
    err = capsys.readouterr().err

    call = reviewer.calls[0]
    assert call["prompt_exists"] is True                  # 실행 시점엔 존재
    assert call["prompt_mode"] == 0o600                   # 다른 사용자에게 읽히지 않는다
    assert "diff --git a/x.py b/x.py" in call["prompt_text"]
    assert call["input"] == ""                            # 지시는 파일에 있다(stdin 아님)
    assert not call["prompt_file"].exists()               # 실행 후 정리(성공·실패 무관)
    assert "프롬프트 파일 정리 실패" not in err             # 정상 정리는 조용하다


def _failing_prompt_unlink(monkeypatch, reason: str = "정리 거부"):
    """프롬프트 임시 파일의 unlink 만 실패시킨다(다른 경로의 정리는 그대로 둔다)."""
    original = Path.unlink

    def _unlink(self, *args, **kwargs):
        if self.name.startswith("external_review_prompt_"):
            raise OSError(reason)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _unlink)


def test_prompt_file_cleanup_failure_is_loud_and_does_not_mask_the_result(
        external, monkeypatch, tmp_path, capsys):
    """정리 실패는 크게 알린다 — 주 결과는 그대로 두고, 남은 파일의 경로를 지목한다.

    이 임시 파일에는 검토 대상 diff 원문이 들어 있다. 조용히 삼키면 잔존 사실이 어느 표면에도
    남지 않아, 누출 흔적을 사람이 알 방법이 없다."""
    repo = _repo(tmp_path / "repo", _conf(harness="opencode", model="zai/glm-4.6",
                                          reasoning="medium"))
    reviewer = _FakeReviewer(stdout=_wire("opencode"))
    _wire_main(external, monkeypatch, repo, reviewer)
    _failing_prompt_unlink(monkeypatch)

    rc = external.main(["--paths", "x.py"])
    captured = capsys.readouterr()

    # 주 결과 보존 — 판정·종료코드·raw 박제 어느 것도 정리 실패에 오염되지 않는다.
    assert rc == 0
    assert "종합 판정: 통과" in captured.out
    assert _wire("opencode") in _raw_text(repo)
    # 정리 실패는 loud 하고, 남은 파일을 경로로 지목한다.
    leftover = reviewer.calls[0]["prompt_file"]
    assert leftover.exists()                              # 실제로 남았다(주입한 형상 그대로)
    assert "프롬프트 파일 정리 실패" in captured.err
    assert str(leftover) in captured.err
    assert "OSError" in captured.err and "정리 거부" in captured.err
    monkeypatch.undo()                                    # 주입 해제 후 tmp 잔존물 회수
    leftover.unlink()


def test_prompt_file_cleanup_failure_still_propagates_the_primary_exception(
        external, monkeypatch, tmp_path, capsys):
    """정리 실패가 주 예외를 가리지 않는다 — 진단만 더하고 원인은 그대로 올라간다."""
    target = external.resolve_reviewer_target(
        {"additional_reviewer.harness": "opencode",
         "additional_reviewer.model": "zai/glm-4.6"})
    _failing_prompt_unlink(monkeypatch)
    seen: dict[str, Path] = {}

    with pytest.raises(RuntimeError, match="스폰 중 폭발"):
        with external._structured_transport(target, "프롬프트 본문", tmp_path) as (argv, _):
            seen["path"] = Path(argv[argv.index("--file") + 1])
            raise RuntimeError("스폰 중 폭발")

    assert "프롬프트 파일 정리 실패" in capsys.readouterr().err
    monkeypatch.undo()
    seen["path"].unlink()


def test_opencode_prompt_file_is_removed_when_the_run_raises(external, tmp_path):
    """예외로 빠져나가도 프롬프트 파일은 남지 않는다(정리는 finally 소유)."""
    target = external.resolve_reviewer_target(
        {"additional_reviewer.harness": "opencode",
         "additional_reviewer.model": "zai/glm-4.6"})
    seen: dict[str, Path] = {}
    with pytest.raises(RuntimeError, match="스폰 중 폭발"):
        with external._structured_transport(target, "프롬프트 본문", tmp_path) as (argv, _):
            seen["path"] = Path(argv[argv.index("--file") + 1])
            assert seen["path"].exists()
            raise RuntimeError("스폰 중 폭발")
    assert not seen["path"].exists()


@pytest.mark.parametrize("harness", ["codex", "claude"])
def test_stdin_harnesses_prepare_no_prompt_file(external, tmp_path, harness):
    """codex/claude 는 프롬프트를 stdin 으로 받는다 — 준비할 임시 자원이 없다."""
    target = external.resolve_reviewer_target(
        {"additional_reviewer.harness": harness, "additional_reviewer.model": "m"})
    with external._structured_transport(target, "프롬프트", tmp_path) as (argv, stdin_text):
        assert "--file" not in argv
        assert stdin_text is None          # None = 프롬프트를 그대로 stdin 주입


# ══ ④ 구조화 wire 회신 추출 (판정 경계) ════════════════════════════════════


@pytest.mark.parametrize("harness", ["codex", "claude", "opencode"])
def test_verdict_sees_only_the_final_reply_while_raw_keeps_the_whole_wire(
        external, tmp_path, harness):
    """판정은 추출된 최종 회신만 보고, raw 는 stdout+stderr wire 원문을 그대로 보존한다."""
    target = external.resolve_reviewer_target(
        {"additional_reviewer.harness": harness, "additional_reviewer.model": "m"})
    wire = _wire(harness)
    result = external.run_review(
        "프롬프트", timeout=30, output_dir=tmp_path, target=target,
        run_fn=_FakeReviewer(stdout=wire, stderr="진행 로그: " + _DECOY),
        cwd=tmp_path, env={"PATH": os.environ.get("PATH", "")},
    )

    assert result["answer"] == _PASS_REPLY                 # 판정 입력 = 최종 회신
    assert result["all_pass"] is True                      # 미끼 반려가 판정에 닿지 않았다
    assert result["any_must_fix"] is False
    assert result["reply_extraction_failed"] is False
    assert wire in result["output"] and _DECOY in result["output"]   # wire 원문 보존
    raw = result["file"].read_text(encoding="utf-8")
    assert wire in raw and "진행 로그: " + _DECOY in raw


@pytest.mark.parametrize("harness", ["codex", "claude", "opencode"])
def test_reply_extraction_failure_never_feeds_the_wire_to_the_verdict(
        external, tmp_path, harness):
    """회신을 못 뽑으면 wire 를 판정에 흘리지 않는다 — 판정 불명확(보수적)이고 raw 로 진단한다."""
    target = external.resolve_reviewer_target(
        {"additional_reviewer.harness": harness, "additional_reviewer.model": "m"})
    # 회신 이벤트가 없는 wire. 본문에는 통과 문구가 섞여 있어 wire-as-verdict 면 통과가 나온다.
    wire = _wire(harness, with_reply=False) + json.dumps(
        {"type": "noise", "text": "판정: 통과"}, ensure_ascii=False) + "\n"
    result = external.run_review(
        "프롬프트", timeout=30, output_dir=tmp_path, target=target,
        run_fn=_FakeReviewer(stdout=wire),
        cwd=tmp_path, env={"PATH": os.environ.get("PATH", "")},
    )

    assert result["reply_extraction_failed"] is True
    assert result["answer"] == ""
    assert result["all_pass"] is False and result["any_must_fix"] is False
    assert result["failed"] is True                          # 회신 없음 = 리뷰 미수신
    assert external.determine_exit_code(result) == 1         # 보수적 종료코드
    assert wire in result["file"].read_text(encoding="utf-8")


# 형식이 붕괴한 wire — 최종 회신 자리에 **텍스트가 아닌 값**이 오거나 라인 자체가 깨진 형상이다.
# claude `result` 필드는 스키마상 임의 JSON 값이 올 수 있는 자리라 파서가 그대로 통과시킨다.
_MALFORMED_WIRES: tuple[tuple[str, str, str], ...] = (
    ("claude", "dict", '{"type":"result","result":{"not":"text"}}'),
    ("claude", "list", '{"type":"result","result":["not","text"]}'),
    ("claude", "number", '{"type":"result","result":42}'),
    ("claude", "null", '{"type":"result","result":null}'),
    ("claude", "empty", '{"type":"result","result":""}'),
    ("claude", "truncated", '{"type":"result","result":'),
    ("codex", "dict",
     '{"type":"item.completed","item":{"type":"agent_message","text":{"n":1}}}'),
    ("opencode", "dict",
     '{"type":"text","sessionID":"ses-1","part":{"type":"text","text":{"n":1}}}'),
)


def _malformed_wire(line: str) -> str:
    """형식 붕괴 라인 + 통과 미끼 — wire 가 판정에 흘러들면 가짜 통과가 나오게 심어 둔다."""
    return line + "\n" + json.dumps(
        {"type": "noise", "text": "판정: 통과"}, ensure_ascii=False) + "\n"


@pytest.mark.parametrize("harness,shape,line", _MALFORMED_WIRES)
def test_non_string_final_reply_is_not_a_reply_at_the_shared_seam(
        relay, harness, shape, line):
    """최종 회신의 타입 계약은 공용 seam 이 세운다 — 텍스트가 아니면 회신이 아니다(None).

    여기서 세우지 않으면 dict/list 가 소비 표면까지 흘러가 판정 파싱에서 AttributeError 로 터지고,
    그 시점은 raw 박제·장부 마감 **전**이라 원문도 장부도 남지 않는다."""
    reply = relay.extract_harness_reply(harness, _malformed_wire(line))
    assert reply is None, (shape, reply)


# 공백만 있는 최종 회신 — 형식은 멀쩡하고 회신 이벤트도 있는데 **말한 내용이 없는** 형상이다.
# 스트리밍 하네스가 빈 turn 을 닫을 때 실제로 나온다(개행만 실린 마지막 이벤트).
_BLANK_REPLIES: tuple[tuple[str, str], ...] = (
    ("space", " "),
    ("newline", "\n"),
    ("mixed", " \t\r\n  \n"),
    ("ideographic", "　"),          # 전각 공백도 공백이다(str.strip 대상)
)


@pytest.mark.parametrize("harness", ["claude", "codex", "opencode"])
@pytest.mark.parametrize("shape,blank", _BLANK_REPLIES)
def test_a_whitespace_only_final_reply_is_not_a_reply_at_the_shared_seam(
        relay, harness, shape, blank):
    """공백만 있는 최종 회신은 **회신 없음**이다 — 참/거짓이 아니라 내용으로 판정한다.

    참/거짓만 보면 `" "`·`"\\n"` 이 회신으로 통과해, 리뷰어가 아무 말도 하지 않은 실행이 '회신
    있음'으로 기록된다. 그 뒤 판정 파싱은 아무 토큰도 못 찾아 판정 불명확으로만 끝나고, 폴백
    신호가 서지 않아 호출자가 내부 리뷰어로 갈아탈 근거를 잃는다."""
    assert relay.extract_harness_reply(harness, _wire(harness, reply=blank)) is None, shape


@pytest.mark.parametrize("harness", ["claude", "codex", "opencode"])
def test_a_nonblank_reply_keeps_its_original_bytes_at_the_shared_seam(relay, harness):
    """내용이 있는 회신은 앞뒤 공백까지 **원문 그대로** 나온다 — 존재 판정만 strip 으로 한다.

    돌려주는 값을 strip 하면 판정·전사 표면이 wire 원문과 어긋나, raw 로 재구성한 회신과 실제
    판정에 들어간 회신이 다른 문자열이 된다."""
    padded = "  \n" + _PASS_REPLY + "  \n\n"
    assert relay.extract_harness_reply(harness, _wire(harness, reply=padded)) == padded


@pytest.mark.parametrize("harness", ["claude", "codex", "opencode"])
def test_a_whitespace_only_reply_takes_the_fail_loud_extraction_path(
        external, monkeypatch, tmp_path, capsys, harness):
    """공백만 있는 회신은 기존 `reply_extraction_failed` 경로를 그대로 탄다 (rc=1·폴백 신호).

    '성공 → 판정 불명확'으로 두면 rc 는 1 이어도 FALLBACK_INTERNAL 이 없어, PM 이 내부
    code-reviewer 로 갈아탈 근거를 못 받는다. wire 원문은 raw 에 그대로 남고 장부는 닫힌다."""
    repo = _repo(tmp_path / "repo", _conf(harness, "m", None))
    wire = _wire(harness, reply=" \n\t ")
    reviewer = _FakeReviewer(stdout=wire, stderr="진행 로그: " + _DECOY)
    _wire_main(external, monkeypatch, repo, reviewer)

    rc = external.main(["--gate", "T-0590", "--paths", "x.py"])
    captured = capsys.readouterr()

    assert rc == 1
    assert len(reviewer.calls) == 1                          # 스폰은 됐다(전송·과금 있음)
    assert "FALLBACK_INTERNAL" in captured.out               # 내부 리뷰어 폴백 신호
    assert "회신 추출 실패" in captured.out
    assert "종합 판정: 통과" not in captured.out               # 미끼가 판정이 되지 않았다
    assert "인증 입력이 빠져 실패했을 수" not in captured.err   # rc=0 실행 — 인증 탓이 아니다
    raw = _raw_text(repo)
    assert wire in raw                                       # 원문(공백 회신 포함) 보존
    assert "진행 로그: " + _DECOY in raw
    row = _raw_ledger(repo)[0]
    assert row["rc"] == 0 and row["finished_at"]             # 프로세스 자체는 정상 종료
    # 라운드는 전송이 있었으므로 환불하지 않는다 — 판정 없는 라운드로 마감된다.
    assert _round_ledger(repo)["T-0590"]["count"] == 1


@pytest.mark.parametrize("harness,shape,line", _MALFORMED_WIRES)
def test_malformed_wire_fails_loudly_and_still_closes_raw_and_ledger(
        external, monkeypatch, tmp_path, capsys, harness, shape, line):
    """형식 붕괴 wire 는 예외 없이 rc=1·FALLBACK_INTERNAL 로 끝나고 원문·장부를 그대로 닫는다.

    실 러너 seam(`_watchdog_reviewer_run`)을 통과시켜 `run_review` 본체(추출→판정→raw 박제→장부
    마감)를 그대로 태운다 — 어느 단계에서 터져도 raw 가 비고 장부가 미마감으로 남는다."""
    repo = _repo(tmp_path / "repo", _conf(harness, "m", None))
    wire = _malformed_wire(line)
    reviewer = _FakeReviewer(stdout=wire, stderr="진행 로그: " + _DECOY)
    _wire_main(external, monkeypatch, repo, reviewer)

    rc = external.main(["--gate", "T-0590", "--paths", "x.py"])
    captured = capsys.readouterr()

    assert rc == 1                                          # 조용한 통과 없음
    assert len(reviewer.calls) == 1                          # 실제로 스폰은 됐다(전송·과금 있음)
    assert "FALLBACK_INTERNAL" in captured.out               # 내부 리뷰어 폴백 신호
    assert "회신 추출 실패" in captured.out                    # 사람이 읽는 진단
    assert "종합 판정: 통과" not in captured.out               # 미끼가 판정이 되지 않았다
    # raw 는 wire 전문(stdout+stderr)을 그대로 보존한다 — 원인은 여기서만 보인다.
    raw = _raw_text(repo)
    assert wire in raw
    assert "진행 로그: " + _DECOY in raw
    assert f"# harness: {harness}" in raw
    # 장부 레코드가 **마감**돼 있다(미완으로 남으면 다음 실행의 재시도 예산을 갉아먹는다).
    row = _raw_ledger(repo)[0]
    assert row["finished_at"]
    assert row["rc"] == 0                                    # 프로세스 자체는 정상 종료였다
    assert row["elapsed_sec"] >= 0
    assert Path(row["raw_path"]).read_text(encoding="utf-8") == raw
    assert external._load_relay().unfinished_raw_records(
        repo / ".project_manager" / ".local" / "raw_outputs.json") == []


def test_malformed_wire_does_not_send_the_operator_down_the_auth_path(
        external, monkeypatch, tmp_path, capsys):
    """회신 추출 실패에는 격리-인증 힌트를 내지 않는다 — rc=0 으로 끝난 실행이라 원인이 아니다."""
    repo = _repo(tmp_path / "repo", _conf("claude", "claude-opus-5", None))
    reviewer = _FakeReviewer(stdout=_malformed_wire('{"type":"result","result":{"a":1}}'))
    _wire_main(external, monkeypatch, repo, reviewer)

    assert external.main(["--paths", "x.py"]) == 1
    captured = capsys.readouterr()

    assert "인증 입력이 빠져 실패했을 수" not in captured.err
    assert "최종 회신 텍스트를 추출하지 못했습니다" in captured.out   # 사유는 정확히 말한다


# ══ ⑤ legacy 호환 (실행 형상 동형 + unpinned-model loud) ═══════════════════


def test_legacy_command_stdin_and_output_are_byte_identical_to_the_old_path(
        external, tmp_path):
    """legacy 대상은 `shlex.split(reviewer_cmd)` + 프롬프트 stdin + 출력 원문 판정 그대로다."""
    reviewer = _FakeReviewer(stdout=_PASS_REPLY)
    result = external.run_review(
        "프롬프트 본문", reviewer_cmd="codex exec --sandbox read-only",
        timeout=30, output_dir=tmp_path, run_fn=reviewer,
    )
    call = reviewer.calls[0]
    assert call["argv"] == ["codex", "exec", "--sandbox", "read-only"]
    assert call["input"] == "프롬프트 본문"          # 프롬프트 stdin 주입(파일 첨부 없음)
    assert "--file" not in call["argv"] and "-C" not in call["argv"]
    assert result["answer"] == _PASS_REPLY           # wire 파서를 태우지 않는다
    assert result["reply_extraction_failed"] is False
    assert result["all_pass"] is True


def test_legacy_unpinned_model_is_loud_in_every_provenance_surface(
        external, monkeypatch, tmp_path, capsys):
    """legacy는 model 표기에도 dry-run·stderr·raw 헤더·장부 모두 unpinned로 라벨링된다."""
    repo = _repo(tmp_path / "repo", {"additional_reviewer_enabled": "true",
                                     "reviewer_cmd": "codex exec --model gpt-x --sandbox read-only"})
    reviewer = _FakeReviewer(stdout=_PASS_REPLY)
    _wire_main(external, monkeypatch, repo, reviewer)

    assert external.main(["--paths", "x.py", "--dry-run"]) == 0
    preview = capsys.readouterr()
    assert f"model={external.UNPINNED_MODEL_LABEL}" in preview.out
    assert "command: codex exec --model gpt-x --sandbox read-only" in preview.out

    assert external.main(["--paths", "x.py"]) == 0
    err = capsys.readouterr().err
    first = err.splitlines()[0]
    assert first.startswith("[external-review] config provenance:")
    assert f"source={external.REVIEWER_SOURCE_LEGACY}" in first
    assert f"model={external.UNPINNED_MODEL_LABEL}" in first

    raw = _raw_text(repo)
    assert f"# reviewer_source: {external.REVIEWER_SOURCE_LEGACY}" in raw
    assert f"# model: {external.UNPINNED_MODEL_LABEL}" in raw
    assert "# command: codex exec --model gpt-x --sandbox read-only" in raw
    # 장부의 **모델 필드 자체**가 같은 정체를 말한다 — raw 파일을 한 홉 더 읽어야 알 수 있으면
    # raw 가 지워진 뒤(보존기간 만료·격리 실행) 장부만으로는 모델 축이 닫히지 않고, 그 사이 장부는
    # `default` 라는 이름의 모델이 봤다고 말한다.
    row = _raw_ledger(repo)[0]
    assert row["model"] == external.UNPINNED_MODEL_LABEL
    assert row["model"] != "default"
    assert row["reviewer_source"] == external.REVIEWER_SOURCE_LEGACY
    assert row["command"] == "codex exec --model gpt-x --sandbox read-only"
    assert row["local_conf"] == str((repo / ".project_manager" / "local.conf").resolve())
    assert Path(row["raw_path"]).read_text(encoding="utf-8") == raw
    assert reviewer.calls[0]["argv"] == [
        "codex", "exec", "--model", "gpt-x", "--sandbox", "read-only"]


# ══ ⑥ provenance 동일성 (dry-run · stderr · raw 헤더 · raw 장부) ═══════════


@pytest.mark.parametrize("harness,model,reasoning", [
    ("codex", "gpt-5.6-sol", "max"),
    ("claude", "claude-opus-5", "high"),
    ("opencode", "zai/glm-4.6", None),
])
def test_structured_provenance_is_one_string_across_four_surfaces(
        external, monkeypatch, tmp_path, capsys, harness, model, reasoning):
    """네 표면이 **같은 문자열**로 대상을 말한다 — 다르면 대조 자체가 불가능하다."""
    repo = _repo(tmp_path / "repo", _conf(harness, model, reasoning))
    conf_path = (repo / ".project_manager" / "local.conf").resolve()
    reviewer = _FakeReviewer(stdout=_wire(harness))
    _wire_main(external, monkeypatch, repo, reviewer)
    target = external.resolve_reviewer_target(external.local_config(repo))

    assert external.main(["--paths", "x.py", "--dry-run"]) == 0
    preview = capsys.readouterr().out
    assert f"local_conf: {conf_path}" in preview
    assert f"command: {target.command}" in preview
    assert f"source={external.REVIEWER_SOURCE_STRUCTURED}" in preview
    assert f"harness={harness}, model={model}, reasoning={reasoning}" in preview

    assert external.main(["--paths", "x.py"]) == 0
    first = capsys.readouterr().err.splitlines()[0]
    assert f"local_conf={conf_path}" in first
    assert (f"source={external.REVIEWER_SOURCE_STRUCTURED}, harness={harness}, "
            f"model={model}, reasoning={reasoning}") in first

    raw = _raw_text(repo)
    assert f"# local_conf: {conf_path}" in raw
    assert f"# reviewer_source: {external.REVIEWER_SOURCE_STRUCTURED}" in raw
    assert f"# harness: {harness}" in raw
    assert f"# model: {model}" in raw
    assert f"# reasoning: {reasoning}" in raw
    assert f"# command: {target.command}" in raw

    row = _raw_ledger(repo)[0]
    assert row["local_conf"] == str(conf_path)          # 절대경로 앵커
    assert Path(row["local_conf"]).is_absolute()
    assert row["surface"] == "external-review"
    assert row["harness"] == harness                    # 정규화 실행 키
    assert row["model"] == model                        # 명시 모델(구조화는 `default` 불가)
    assert row["model"] != "default"
    assert row["reasoning"] == reasoning
    assert row["reviewer_source"] == external.REVIEWER_SOURCE_STRUCTURED
    assert row["command"] == target.command
    assert row["role"] == external.REVIEWER_ROLE


def test_dry_run_and_execution_render_the_same_command_for_opencode_placeholders(
        external, monkeypatch, tmp_path, capsys):
    """실행 시점에만 정해지는 경로(거울 cwd·프롬프트 파일)는 세 표면이 같은 자리표시자로 말한다."""
    repo = _repo(tmp_path / "repo", _conf("opencode", "zai/glm-4.6", None))
    reviewer = _FakeReviewer(stdout=_wire("opencode"))
    _wire_main(external, monkeypatch, repo, reviewer)
    target = external.resolve_reviewer_target(external.local_config(repo))
    assert external.REVIEWER_CWD_PLACEHOLDER in target.command
    assert external.REVIEWER_PROMPT_FILE_PLACEHOLDER in target.command

    assert external.main(["--paths", "x.py"]) == 0
    capsys.readouterr()
    argv = reviewer.calls[0]["argv"]
    # 실 argv 는 같은 자리에 실제 값이 들어가고, 나머지 토큰 순서/내용은 정체 문자열과 같다.
    rendered = shlex.split(target.command)
    assert len(argv) == len(rendered)
    for actual, declared in zip(argv, rendered):
        if declared in (external.REVIEWER_CWD_PLACEHOLDER,
                        external.REVIEWER_PROMPT_FILE_PLACEHOLDER):
            assert Path(actual).is_absolute()
        else:
            assert actual == declared


# ══ ⑦ Codex egress 게이트 (network-off 안전 경계) ══════════════════════════


def test_network_off_without_attestation_fails_before_round_raw_isolation_and_spawn(
        external, monkeypatch, tmp_path, capsys):
    """증명 없는 network-off 실행은 라운드 예약·raw·격리·스폰 전에 rc=1 로 끝난다."""
    monkeypatch.setenv(_EGRESS_MARKER, "1")
    repo = _repo(tmp_path / "repo", _conf())
    reviewer = _FakeReviewer(stdout=_wire("codex"))
    _wire_main(external, monkeypatch, repo, reviewer)
    isolations: list[str] = []
    monkeypatch.setattr(external, "reviewer_visibility_scope",
                        lambda *a, **k: isolations.append("called"))
    outdir = tmp_path / "raw-out"

    rc = external.main(["--gate", "T-0590", "--paths", "x.py",
                        "--output-dir", str(outdir)])
    err = capsys.readouterr().err

    assert rc == 1
    assert reviewer.calls == []                                   # 외부 스폰 0
    assert isolations == []                                       # 격리 거울 0
    # 요청한 산출 디렉토리 **자체가 생기지 않는다** — "비어 있다"로는 부족하다: 차단된 실행이
    # 남긴 빈 디렉토리는 파일시스템에서 "아무 일도 없었다"를 거짓으로 만들고, 사람이 산출물
    # 위치로 오독한다.
    assert not outdir.exists()                                    # raw 예약·디렉토리 생성 0
    assert not (repo / ".project_manager" / ".local").exists()    # 라운드 장부 0
    assert "추가 리뷰어 실행 중" not in err                         # 과금 문구 전
    for expected in (
        'sandbox_permissions="require_escalated"',
        external.CODEX_EGRESS_FLAG,
        "--dry-run",
        f"{_EGRESS_MARKER}=true",
        "sandbox_workspace_write.network_access=true",
        f"{external.ADDITIONAL_REVIEWER_ENABLED_KEY}=true",
        "후속 호출마다 비용을 다시 묻지 마세요",
    ):
        assert expected in err, expected


def test_force_cannot_bypass_the_egress_gate(external, monkeypatch, tmp_path, capsys):
    """`--force` 는 opt-in 게이트용이다 — 안전 경계(egress)를 여는 우회로가 아니다."""
    monkeypatch.setenv(_EGRESS_MARKER, "1")
    repo = _repo(tmp_path / "repo", _conf(**{"additional_reviewer_enabled": "false"}))
    reviewer = _FakeReviewer(stdout=_wire("codex"))
    _wire_main(external, monkeypatch, repo, reviewer)
    outdir = tmp_path / "raw-out"

    assert external.main(["--paths", "x.py", "--force",
                          "--output-dir", str(outdir)]) == 1
    assert reviewer.calls == []
    # 강제 실행도 게이트 **앞**에서 끝난다 — 부작용(산출 디렉토리)도 만들지 않는다.
    assert not outdir.exists()
    assert not (repo / ".project_manager" / ".local").exists()
    assert external.CODEX_EGRESS_FLAG in capsys.readouterr().err


def test_opt_in_off_is_a_no_op_that_creates_no_output_dir(
        external, monkeypatch, tmp_path, capsys):
    """비활성 no-op(rc=0)도 부작용 0 이다 — 요청한 산출 디렉토리를 만들지 않는다."""
    repo = _repo(tmp_path / "repo", _conf(**{"additional_reviewer_enabled": "false"}))
    reviewer = _FakeReviewer(stdout=_wire("codex"))
    _wire_main(external, monkeypatch, repo, reviewer)
    outdir = tmp_path / "raw-out"

    assert external.main(["--paths", "x.py", "--output-dir", str(outdir)]) == 0
    assert reviewer.calls == []
    assert not outdir.exists()
    assert "추가 리뷰어 비활성" in capsys.readouterr().err


def test_disabled_notice_on_a_legacy_only_conf_says_undecided_and_names_the_old_key(
        external, monkeypatch, tmp_path, capsys):
    """구키만 있는 채택자는 "결정 없음" + 구키 감지 1줄을 함께 받는다 (T-0600·T-0614).

    구키는 더 이상 결정을 공급하지 않으므로 비활성 안내가 그 줄을 인용하면 거짓말이 된다(읽지도
    않는 줄을 현재 상태로 제시하는 셈). 대신 별도 안내가 그 키를 지목하고, 처방은 그대로 신키다.
    """
    conf = _conf()
    del conf[external.ADDITIONAL_REVIEWER_ENABLED_KEY]
    conf[external.LEGACY_EXTERNAL_REVIEW_ENABLED_KEY] = "false"
    repo = _repo(tmp_path / "repo", conf)
    _wire_main(external, monkeypatch, repo, _FakeReviewer(stdout=_wire("codex")))

    assert external.main(["--paths", "x.py"]) == 0
    err = capsys.readouterr().err
    assert "local.conf 에 opt-in 결정 없음" in err
    assert external.LEGACY_ENABLED_KEY_REMOVED in err        # 구키는 별도 1줄이 지목
    assert f"`{external.ADDITIONAL_REVIEWER_ENABLED_KEY}=true`" in err   # 처방은 신키


def test_round_limit_refusal_enters_no_isolation_and_creates_no_output_dir(
        external, monkeypatch, tmp_path, capsys):
    """전송 전 거부(라운드 상한)도 같은 규율이다 — 격리 진입·산출 디렉토리·장부 변화 모두 0.

    예산 확인·예약이 격리 **뒤**에 서면 이미 상한에 닿은 호출도 거울과 임시 홈을 한 번 만들었다
    지운다. 정리가 성공했다는 것(=`남은 게 없다`)은 seam 에 들어간 적 없다는 것과 다른 진술이다 —
    거부된 호출은 저장소 tracked 사본·홈 인증 사본 복제를 아예 시작하지 않아야 한다."""
    repo = _repo(tmp_path / "repo", _conf(additional_reviewer_round_limit="1"))
    reviewer = _FakeReviewer(stdout=_wire("codex"))
    _wire_main(external, monkeypatch, repo, reviewer)
    entered = _count_isolation(external, monkeypatch)
    first_out, second_out = tmp_path / "raw-1", tmp_path / "raw-2"

    assert external.main(["--gate", "T-0590", "--paths", "x.py",
                          "--output-dir", str(first_out)]) == 0
    after_first = _round_ledger(repo)
    capsys.readouterr()
    rc = external.main(["--gate", "T-0590", "--paths", "x.py",
                        "--output-dir", str(second_out)])
    err = capsys.readouterr().err

    assert rc == external.EXIT_ROUND_LIMIT_EXCEEDED
    assert first_out.is_dir()                       # 실제로 나간 실행은 산출을 남긴다
    assert not second_out.exists()                  # 거부된 실행은 자리도 만들지 않는다
    assert len(entered) == 1                        # 격리 seam 진입은 나간 실행에서만
    assert len(reviewer.calls) == 1                 # 스폰도 그 실행에서만
    # 장부는 첫 라운드 상태 그대로다 — 거부는 예약도 환불도 하지 않는다(왕복 흔적 없음).
    assert _round_ledger(repo) == after_first
    assert after_first["T-0590"]["count"] == 1
    assert "라운드 상한 도달" in err
    assert "추가 리뷰어 실행 중" not in err            # 과금 문구 앞에서 끊긴다


def test_isolation_failure_after_reservation_refunds_that_reservation(
        external, monkeypatch, tmp_path, capsys):
    """예약 뒤 격리가 실패하면 그 예약 하나만 원자 환불한다 — 전송 0 인데 상한이 줄면 안 된다.

    예산 게이트를 격리 앞으로 옮긴 대가가 이 경로다: 스폰이 확실히 없었다는 점에서 마감 시점의
    `started=False` 환불과 같은 조건이라, 같은 락·같은 환불 기계를 그대로 쓴다. 되돌린 슬롯은
    다음 정상 실행이 그대로 소비한다(상한이 조용히 깎이지 않는다)."""
    repo = _repo(tmp_path / "repo", _conf(additional_reviewer_round_limit="1"))
    reviewer = _FakeReviewer(stdout=_wire("codex"))
    _wire_main(external, monkeypatch, repo, reviewer)
    workspace_stub = external.create_reviewer_workspace     # _wire_main 이 심은 거울 스텁
    outdir = tmp_path / "raw-out"

    def _fail(*args, **kwargs):
        raise external.ReviewerWorkspaceError("거울 생성 실패(주입)")

    monkeypatch.setattr(external, "create_reviewer_workspace", _fail)
    rc = external.main(["--gate", "T-0590", "--paths", "x.py",
                        "--output-dir", str(outdir)])
    err = capsys.readouterr().err

    assert rc == 1
    assert reviewer.calls == []                             # 스폰 0
    assert not outdir.exists()                              # raw 예약·디렉토리 0
    ledger = _round_ledger(repo)
    assert (ledger["T-0590"]["count"], ledger["T-0590"]["records"]) == (0, [])
    assert ledger[external.WAVE_SECTION_KEY]["spent"] == 0   # 두 축을 같은 조건으로 되돌린다
    assert ledger["T-0590"]["rounds"] == []                  # 산출 이력도 남기지 않는다
    assert err.count("라운드 예약 환불") == 1                 # 조용히도, 두 번도 되돌리지 않는다
    assert external.UNISOLATED_REVIEWER_FLAG in err          # 중단 사유는 격리 실패 그대로

    # 환불한 슬롯은 그대로 살아 있다 — limit=1 인데 다음 실행이 정상 전송된다.
    monkeypatch.setattr(external, "create_reviewer_workspace", workspace_stub)
    assert external.main(["--gate", "T-0590", "--paths", "x.py",
                          "--output-dir", str(outdir)]) == 0
    capsys.readouterr()
    assert len(reviewer.calls) == 1
    assert _round_ledger(repo)["T-0590"]["count"] == 1


def test_early_refund_never_charges_a_wave_that_was_reset_meanwhile(
        external, monkeypatch, tmp_path, capsys):
    """격리 실패 환불도 **예약 시점 세대**만 깎는다 (마감 경로와 같은 규칙).

    예약 구간 락은 이미 풀린 뒤라, 거울 생성 중 다른 실행이 `--ack-wave` 로 새 wave 를 여는 순서는
    실제 동시 실행과 같다. 세대 확인이 없으면 옛 실행의 실패가 새 예산을 깎아 승인 1회로 예산이
    늘어난다."""
    repo = _repo(tmp_path / "repo", _conf())
    reviewer = _FakeReviewer(stdout=_wire("codex"))
    _wire_main(external, monkeypatch, repo, reviewer)

    def _reset_wave_then_fail(*args, **kwargs):
        with external._round_ledger_lock():          # 다른 실행의 `--ack-wave` 대역
            ledger = external._load_round_ledger()
            external._spend_wave_round(external._reset_wave(ledger))
            external._save_round_ledger(ledger)
        raise external.ReviewerWorkspaceError("거울 생성 실패(주입)")

    monkeypatch.setattr(external, "create_reviewer_workspace", _reset_wave_then_fail)
    assert external.main(["--gate", "T-0590", "--paths", "x.py"]) == 1
    err = capsys.readouterr().err

    ledger = _round_ledger(repo)
    assert reviewer.calls == []
    assert ledger["T-0590"]["count"] == 0                        # 라운드 count 는 환불
    assert ledger[external.WAVE_SECTION_KEY]["spent"] == 1        # 새 wave 소비는 그대로
    assert "wave 가 이미 리셋" in err                              # 갈린 축을 조용히 두지 않는다


# 예약~스폰 사이에서 나갈 수 있는 실패의 세 부류 — 일반 예외 하나와 인터프리터가 던지는
# `BaseException` 둘. 환불 조건이 예외 **종류**가 아니라 **구간**이라면 셋이 같은 결과를 낸다.
_PRE_SPAWN_FAILURES = (RuntimeError, KeyboardInterrupt, SystemExit)


def _round_records(repo: Path, gate: str = "T-0590") -> list[dict]:
    return _round_ledger(repo)[gate]["records"]


@pytest.mark.parametrize("failure", _PRE_SPAWN_FAILURES)
def test_unexpected_failure_entering_isolation_refunds_and_propagates(
        external, monkeypatch, tmp_path, capsys, failure):
    """격리 진입에서 **알려지지 않은** 예외가 나도 예약은 환불되고, 예외는 그대로 전파된다.

    알려진 실패(`ReviewerWorkspaceError`)만 되돌리면 나머지 예외는 스폰도 전송도 없이 끝난
    실행의 예약을 미완 레코드로 남긴다 — `incomplete_limit=1` 이면 다음 **정상** 호출이 곧바로
    rc=4 로 막힌다(전송 0인 실패가 예산을 먹는다). 그래서 환불 조건은 예외 종류가 아니라
    '스폰 전 구간'이다. 반대로 rc 로 삼켜서도 안 된다 — 예상 못 한 예외를 격리 실패와 같은
    rc=1 로 바꾸면 진단이 사라진다."""
    repo = _repo(tmp_path / "repo", _conf(
        additional_reviewer_round_limit="1", additional_reviewer_incomplete_round_limit="1"))
    reviewer = _FakeReviewer(stdout=_wire("codex"))
    _wire_main(external, monkeypatch, repo, reviewer)
    workspace_stub = external.create_reviewer_workspace   # _wire_main 이 심은 거울 스텁
    outdir = tmp_path / "raw-out"

    def _boom(*args, **kwargs):
        raise failure("격리 진입 실패(주입)")

    monkeypatch.setattr(external, "create_reviewer_workspace", _boom)
    with pytest.raises(failure):
        external.main(["--gate", "T-0590", "--paths", "x.py",
                       "--output-dir", str(outdir)])
    err = capsys.readouterr().err

    assert reviewer.calls == []                              # 스폰 0
    assert not outdir.exists()                               # raw 예약·디렉토리 0
    ledger = _round_ledger(repo)
    assert (ledger["T-0590"]["count"], ledger["T-0590"]["records"]) == (0, [])
    assert ledger[external.WAVE_SECTION_KEY]["spent"] == 0    # 두 축을 같은 조건으로 되돌린다
    assert ledger["T-0590"]["rounds"] == []
    assert err.count("라운드 예약 환불") == 1                  # 이중 환불도 아니다
    assert "추가 리뷰어 실행 중" not in err                     # 과금 문구 앞에서 끊긴다

    # 환불한 슬롯은 살아 있다 — 상한 1·미완 상한 1 인데 다음 정상 실행이 그대로 전송된다.
    monkeypatch.setattr(external, "create_reviewer_workspace", workspace_stub)
    assert external.main(["--gate", "T-0590", "--paths", "x.py",
                          "--output-dir", str(outdir)]) == 0
    capsys.readouterr()
    assert len(reviewer.calls) == 1
    assert _round_ledger(repo)["T-0590"]["count"] == 1


@pytest.mark.parametrize("failure", _PRE_SPAWN_FAILURES)
def test_unexpected_failure_preparing_the_reviewer_environment_refunds_too(
        external, monkeypatch, tmp_path, capsys, failure):
    """격리 진입 **뒤**·스폰 **전**의 준비 구간(리뷰어 환경 구성)도 같은 seam 이 소유한다.

    격리 컨테이너는 이미 섰지만 리뷰어 프로세스는 아직 없는 구간이라, 여기서 죽어도 외부 전송은
    확실히 0 이다. 진입 지점만 막으면 예약이 이 한 칸 뒤에서 그대로 새어나간다."""
    repo = _repo(tmp_path / "repo", _conf(
        additional_reviewer_round_limit="1", additional_reviewer_incomplete_round_limit="1"))
    reviewer = _FakeReviewer(stdout=_wire("codex"))
    _wire_main(external, monkeypatch, repo, reviewer)
    outdir = tmp_path / "raw-out"

    def _boom(*args, **kwargs):
        raise failure("리뷰어 환경 구성 실패(주입)")

    monkeypatch.setattr(external, "reviewer_env", _boom)
    with pytest.raises(failure):
        external.main(["--gate", "T-0590", "--paths", "x.py",
                       "--output-dir", str(outdir)])
    err = capsys.readouterr().err

    assert reviewer.calls == []                              # 스폰 0
    assert not outdir.exists()                               # raw 예약·디렉토리 0
    ledger = _round_ledger(repo)
    assert (ledger["T-0590"]["count"], ledger["T-0590"]["records"]) == (0, [])
    assert ledger[external.WAVE_SECTION_KEY]["spent"] == 0
    assert err.count("라운드 예약 환불") == 1
    assert "추가 리뷰어 실행 중" not in err


def test_unusable_output_dir_fails_before_any_spawn_and_refunds(
        external, monkeypatch, tmp_path, capsys):
    """`--output-dir` 자리에 일반 파일이 있으면 raw 선점에서 끊긴다 — 스폰 0 이므로 환불한다.

    소유권을 `run_review` **진입**에서 넘기면 이 실행이 반례가 된다: 자식은 뜬 적이 없는데
    (raw 선점의 mkdir 이 먼저 터진다) 예약은 finished_at 없는 미완 레코드로 남는다. 상한 1·미완
    상한 1·wave 예산 1 형상에서는 다음 **정상** 호출이 곧바로 rc=4 로 막혀, 전송 0·과금 0 인
    실패가 다음 라운드를 먹는다. 이전 시점이 러너 호출 직전이면 이 구간은 그대로 환불 대상이다."""
    repo = _repo(tmp_path / "repo", _conf(
        additional_reviewer_round_limit="1", additional_reviewer_incomplete_round_limit="1",
        additional_reviewer_wave_budget="1"))
    reviewer = _FakeReviewer(stdout=_wire("codex"))
    _wire_main(external, monkeypatch, repo, reviewer)
    blocker = tmp_path / "raw-out"                   # 디렉토리 자리에 놓인 일반 파일
    blocker.write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        external.main(["--gate", "T-0590", "--paths", "x.py",
                       "--output-dir", str(blocker)])
    err = capsys.readouterr().err

    assert reviewer.calls == []                                  # 스폰 0
    assert blocker.read_text(encoding="utf-8") == "not a directory\n"  # 남의 파일 불변
    ledger = _round_ledger(repo)
    assert (ledger["T-0590"]["count"], ledger["T-0590"]["records"]) == (0, [])
    assert ledger[external.WAVE_SECTION_KEY]["spent"] == 0        # 두 축을 같은 조건으로
    assert err.count("라운드 예약 환불") == 1                      # 조용히도, 두 번도 아니다
    assert _raw_ledger(repo) == []                               # raw 장부에 남긴 것도 없다

    # 환불한 슬롯은 살아 있다 — 세 예산이 모두 1 인데 다음 정상 호출이 그대로 전송된다.
    outdir = tmp_path / "raw-out-ok"
    assert external.main(["--gate", "T-0590", "--paths", "x.py",
                          "--output-dir", str(outdir)]) == 0
    capsys.readouterr()
    assert len(reviewer.calls) == 1
    assert _round_ledger(repo)["T-0590"]["count"] == 1


@pytest.mark.parametrize("failure", _PRE_SPAWN_FAILURES)
def test_pre_spawn_failure_after_the_raw_reservation_refunds_and_closes_the_record(
        external, monkeypatch, tmp_path, capsys, failure):
    """raw 선점·장부 시작 레코드 **뒤**, 러너 호출 **앞** 구간도 같은 스폰 전 규칙을 탄다.

    이 구간에서 죽으면 되돌릴 게 둘이다. (1) 라운드 예약 — 전송이 확실히 없었으니 환불한다.
    (2) raw 장부의 미마감 레코드 — 그 상태는 "떠 있을지 모르는 자식"(고아 조회면 `--unfinished`
    의 입력)이라는 뜻이라, 스폰이 없었으면 장부가 거짓말을 하는 것이다. 레코드는 실패 축으로
    마감하고 중단 사유는 그 레코드가 가리키는 raw 파일에 박제한다(0바이트 파일도 남기지 않는다)."""
    repo = _repo(tmp_path / "repo", _conf(
        additional_reviewer_round_limit="1", additional_reviewer_incomplete_round_limit="1"))
    reviewer = _FakeReviewer(stdout=_wire("codex"))
    _wire_main(external, monkeypatch, repo, reviewer)
    real_transport = external._structured_transport

    def _boom(*args, **kwargs):
        raise failure("구조화 transport 준비 실패(주입)")

    monkeypatch.setattr(external, "_structured_transport", _boom)
    with pytest.raises(failure):
        external.main(["--gate", "T-0590", "--paths", "x.py"])
    err = capsys.readouterr().err

    assert reviewer.calls == []                                  # 스폰 0
    ledger = _round_ledger(repo)
    assert (ledger["T-0590"]["count"], ledger["T-0590"]["records"]) == (0, [])
    assert ledger[external.WAVE_SECTION_KEY]["spent"] == 0
    assert ledger["T-0590"]["rounds"] == []
    assert err.count("라운드 예약 환불") == 1

    records = _raw_ledger(repo)
    assert len(records) == 1
    assert records[0]["finished_at"] is not None                 # 미마감으로 두지 않는다
    assert records[0]["rc"] == 1                    # 리뷰를 하나도 못 받은 실행 = 실패 축
    raw = _raw_text(repo)
    assert Path(records[0]["raw_path"]).name.startswith("external_review_")
    assert "스폰 전 중단" in raw and "전송 0·과금 0" in raw
    assert failure.__name__ in raw                               # 무엇으로 끊겼는지까지

    # 환불한 슬롯은 살아 있다 — 상한 1·미완 1 인데 다음 정상 호출이 그대로 전송된다.
    monkeypatch.setattr(external, "_structured_transport", real_transport)
    assert external.main(["--gate", "T-0590", "--paths", "x.py"]) == 0
    capsys.readouterr()
    assert len(reviewer.calls) == 1
    assert _round_ledger(repo)["T-0590"]["count"] == 1


def _runner_that_spawns_then_dies(reviewer: _FakeReviewer, failure: type):
    """실제 러너 자리 대역 — 스폰(=전송)을 한 번 하고 그 자리에서 죽는다.

    `_FakeReviewer` 와 **같은 시그니처**를 선언한다: `_reviewer_run_kwargs` 가 러너 시그니처를
    introspect 해 `idle_timeout` 전달 여부와 bind 검증을 하므로, 대역이 `**kwargs` 로 뭉개면 이
    테스트만 다른 seam 계약으로 돌게 된다."""
    def _run(argv, *, input=None, timeout=None, idle_timeout=None,
             cwd=None, env=None, **_ignored):
        reviewer(argv, input=input, timeout=timeout, idle_timeout=idle_timeout,
                 cwd=cwd, env=env)
        raise failure("스폰 직후 실패(주입)")
    return _run


@pytest.mark.parametrize("seam", ["runner", "raw-write"])
def test_failure_after_the_spawn_attempt_keeps_the_reservation(
        external, monkeypatch, tmp_path, capsys, seam):
    """러너 호출로 소유권이 넘어간 뒤의 실패는 **환불하지 않는다** — 이미 전송됐을 수 있다.

    환불 seam 을 실행 전체로 넓히면 타임아웃·비정상 종료로 죽은 라운드까지 되돌아가, 프롬프트가
    이미 나가 과금된 호출이 상한을 소비하지 않는다(MF-A 가 막으려던 무한 우회). 넘어간 예약은
    finished_at 없는 미완 레코드로 남아 다음 실행의 재시도 예산에서 보수적으로 세어진다.
    raw 장부의 미마감 레코드도 그대로 둔다 — 그쪽은 실제로 "확인이 필요한 실행"이다.

    두 자리를 같이 본다: 러너 호출 안에서 죽는 경우(사용자 중단·드라이버 예외)와 러너가 돌아온
    뒤 수합에서 죽는 경우. `run_review` 를 통째로 대역으로 바꾸면 정작 이 경계(러너 호출 직전
    이전)가 검사되지 않으므로 실 러너 seam 을 건다."""
    repo = _repo(tmp_path / "repo", _conf())
    reviewer = _FakeReviewer(stdout=_wire("codex"))
    _wire_main(external, monkeypatch, repo, reviewer)
    if seam == "runner":
        # 러너 호출 **안**에서 나가는 BaseException — 전송은 이미 일어났다.
        expected = KeyboardInterrupt
        monkeypatch.setattr(
            external, "_watchdog_reviewer_run",
            _runner_that_spawns_then_dies(reviewer, expected))
    else:
        # 러너가 돌아온 **뒤**의 수합(raw 박제) 실패 — 회신까지 받은 실행이다.
        expected = RuntimeError

        def _boom(*args, **kwargs):
            raise expected("raw 박제 실패(주입)")

        monkeypatch.setattr(external, "_write_reserved_output", _boom)

    with pytest.raises(expected):
        external.main(["--gate", "T-0590", "--paths", "x.py"])
    err = capsys.readouterr().err

    assert len(reviewer.calls) == 1                           # 스폰은 실제로 일어났다
    ledger = _round_ledger(repo)
    assert ledger["T-0590"]["count"] == 1                     # 예약은 그대로 소비
    assert ledger[external.WAVE_SECTION_KEY]["spent"] == 1
    assert [row.get("finished_at") for row in _round_records(repo)] == [None]  # 미완
    assert "라운드 예약 환불" not in err
    assert "스폰 전 중단" not in err                            # 스폰 전 롤백 창은 이미 닫혔다
    assert [row.get("finished_at") for row in _raw_ledger(repo)] == [None]
    assert "추가 리뷰어 실행 중" in err                          # 이미 과금 구간에 들어갔다


def test_missing_reviewer_binary_refunds_exactly_once_through_finalization(
        external, monkeypatch, tmp_path, capsys):
    """러너 호출까지 갔지만 exec 이 실패한 실행(started=False)은 **마감 경로**가 한 번 환불한다.

    스폰 없음이 판명되면 소유권은 스폰 전 seam 으로 되돌아오지만, 되돌림 **자체**는 여전히 마감
    경로 하나가 한다 — 두 seam 이 같은 예약을 각각 되돌리면 상한이 조용히 늘어난다. 마감이 환불을
    저장까지 끝내면 소유권도 그 자리에서 반납되므로, 정상 복귀 경로의 표면은 종전과 완전히 같다
    (스폰 전 환불 문구가 나오지 않는다)."""
    repo = _repo(tmp_path / "repo", _conf())
    reviewer = _FakeReviewer(stdout=_wire("codex"))
    _wire_main(external, monkeypatch, repo, reviewer)

    def _missing(argv, *, input=None, timeout=None, idle_timeout=None,
                 cwd=None, env=None, **_ignored):
        raise FileNotFoundError(argv[0])              # 실행 파일 부재 — 전송 0

    monkeypatch.setattr(external, "_watchdog_reviewer_run", _missing)
    assert external.main(["--gate", "T-0590", "--paths", "x.py"]) == 1
    err = capsys.readouterr().err

    ledger = _round_ledger(repo)
    assert (ledger["T-0590"]["count"], ledger["T-0590"]["records"]) == (0, [])
    assert ledger[external.WAVE_SECTION_KEY]["spent"] == 0
    assert "라운드 예약 환불" not in err          # 스폰 전 seam 은 이 환불의 소유자가 아니다
    # raw 는 스폰 전 중단이 아니라 **정상 마감**이다(리뷰어를 부르러 갔다 실패한 실행).
    records = _raw_ledger(repo)
    assert len(records) == 1 and records[0]["rc"] == 127
    assert "스폰 전 중단" not in _raw_text(repo)


# ── 스폰 경계 표식의 예산 귀결 (T-0590 R4) ─────────────────────────────────
#
# 환불 판정의 입력은 예외 **종류**가 아니라 relay 가 스폰 경계에서 붙인 표식이다. 아래 세 축이
# 그 귀결을 예산 장부로 못박는다: (1) 재시도 중 자식이 한 번이라도 떴으면 뒤 시도의 기동 실패는
# 환불하지 않는다, (2) `Popen` 이 fork 전에 거절한 실행(argv NUL)은 전송 0 이라 환불한다,
# (3) `Popen` 성공 뒤의 같은 종류 예외는 환불하지 않는다.


class _FakeClock:
    """단조 fake clock — sleep 이 advance 해 relay 폴 루프를 결정적으로 전진시킨다."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class _StallingProc:
    """첫 이벤트가 영원히 오지 않는 fake 자식 — relay 의 startup stall 축을 태운다."""

    def __init__(self) -> None:
        self.kill_count = 0
        self._killed = False

    def first_event_ready(self) -> bool:
        return False

    def poll(self):
        return -9 if self._killed else None

    def kill(self) -> None:
        self.kill_count += 1
        self._killed = True

    def communicate(self, timeout=None):
        return ("", "")

    def last_event_at(self):
        return None

    def partial_output(self):
        return ("", "")

    @property
    def returncode(self):
        return self.poll()


def _relay_runner(relay, popen, clock, *, retries: int):
    """기본 러너(`_watchdog_reviewer_run`)와 같은 형상의 러너 — 자식 생성만 대역화한다.

    `popen` seam 하나만 갈아 끼우고 재시도·kill·표식은 **실 relay 코드**가 그대로 한다. 러너를
    통째로 대역화하면 이 절이 소유한 스폰 경계 표식 계약이 통과 없이 green 이 된다.
    `on_spawn_attempt` 를 이름으로 선언해 소유권 이전 seam 이 relay 안까지 내려가는 실 배선을
    유지한다."""
    def _run(argv, *, input=None, timeout=None, idle_timeout=None,
             cwd=None, env=None, on_spawn_attempt=None, **_ignored):
        return relay.run_with_first_event_watchdog(
            argv, first_event_timeout=5.0, overall_timeout=600.0, retries=retries,
            idle_timeout=None, popen=popen, clock=clock, sleep=clock.advance,
            log=[].append, poll_interval=1.0, on_spawn_attempt=on_spawn_attempt,
        )
    return _run


def test_a_second_attempt_launch_failure_still_charges_the_first_spawn(
        external, relay, monkeypatch, tmp_path, capsys):
    """1회차 스폰 + 2회차 기동 실패 = 정확히 두 시도 — 예약은 소비된 채로 남는다(환불 0).

    stall 재시도는 자식을 여러 번 띄운다. 2회차가 exec 에 실패했다고 실행 전체를 '스폰 없음'으로
    읽으면, **1회차에 이미 나갔을 수 있는 전송**이 라운드·wave 예산에서 사라진다(같은 형상을
    반복하면 상한이 무한히 우회된다). 종류(FileNotFoundError)가 아니라 실행 단위 표식이 이긴다."""
    repo = _repo(tmp_path / "repo", _conf())
    _wire_main(external, monkeypatch, repo)          # 러너 자리는 아래에서 실 relay 로 건다
    clock = _FakeClock()
    first = _StallingProc()
    attempts: list[str] = []

    def popen(argv):
        if not attempts:
            attempts.append("spawned")
            return first
        attempts.append("launch-failed")
        raise FileNotFoundError(argv[0])             # 2회차: 자식이 뜨지 못했다

    monkeypatch.setattr(
        external, "_watchdog_reviewer_run", _relay_runner(relay, popen, clock, retries=1))

    assert external.main(["--gate", "T-0590", "--paths", "x.py"]) == 1
    err = capsys.readouterr().err

    assert attempts == ["spawned", "launch-failed"]  # 정확히 2회 시도
    assert first.kill_count == 1                     # 1회차 자식은 실제로 떴다가 kill 됐다
    ledger = _round_ledger(repo)
    assert ledger["T-0590"]["count"] == 1, "이미 전송됐을 수 있는 실행이 환불됐다"
    assert ledger[external.WAVE_SECTION_KEY]["spent"] == 1
    assert "라운드 예약 환불" not in err
    assert "전송은 일어나지 않았습니다" not in err     # 확정 기동 실패 문구를 쓰면 안 된다


def test_a_nul_argv_rejected_before_the_child_refunds_and_the_next_call_still_runs(
        external, monkeypatch, tmp_path, capsys):
    """argv NUL 은 `Popen` 이 fork 전에 거절한다(전송 0) — 상한 1 에서도 슬롯이 살아남는다.

    실 러너·실 relay·실 `_WatchedPopen` 을 그대로 태운다(프로세스는 하나도 뜨지 않는다 — 거절이
    fork 앞이라 hermetic 하다). 종류가 `ValueError` 라 확정 기동 실패 종류표에는 없다: 표식이
    없으면 보수적으로 '전송됐을 수 있음'이 되어, 상한 1·미완 1 인 채택자는 **한 번도 전송하지
    못한 채** 다음 호출이 막힌다."""
    repo = _repo(tmp_path / "repo", {
        "additional_reviewer_enabled": "true",
        "reviewer_cmd": "my-reviewer --flag\x00bad",     # 인자에 NUL — fork 전 거절
        "additional_reviewer_round_limit": "1",
        "additional_reviewer_incomplete_round_limit": "1",
    })
    _wire_main(external, monkeypatch, repo)              # 러너는 실 워치독 그대로

    assert external.main(["--gate", "T-0590", "--paths", "x.py"]) == 1
    capsys.readouterr()

    ledger = _round_ledger(repo)
    assert (ledger["T-0590"]["count"], ledger["T-0590"]["records"]) == (0, [])
    assert ledger[external.WAVE_SECTION_KEY]["spent"] == 0
    raw = _raw_text(repo)
    assert "실행할 수 없음" in raw and "외부 전송은 일어나지 않았습니다" in raw

    # 환불한 슬롯은 살아 있다 — 상한 1·미완 1 인데 다음 정상 호출이 그대로 전송된다.
    reviewer = _FakeReviewer(stdout=_PASS_REPLY)
    monkeypatch.setattr(external, "_watchdog_reviewer_run", reviewer)
    assert external.main(["--gate", "T-0590", "--paths", "x.py"]) == 0
    capsys.readouterr()
    assert len(reviewer.calls) == 1
    assert _round_ledger(repo)["T-0590"]["count"] == 1


def _runner_that_spawns_then_raises_marked(reviewer: _FakeReviewer, relay, failure: type):
    """스폰(=전송)한 뒤 `Popen` 성공 뒤 실패로 죽는 러너 — relay 가 붙이는 표식까지 재현한다."""
    def _run(argv, *, input=None, timeout=None, idle_timeout=None,
             cwd=None, env=None, **_ignored):
        reviewer(argv, input=input, timeout=timeout, idle_timeout=idle_timeout,
                 cwd=cwd, env=env)
        exc = failure("Popen 성공 뒤 초기화 실패(주입)")
        relay._mark_spawn_failed(exc, False)      # `_WatchedPopen` 이 붙이는 그 표식
        raise exc
    return _run


@pytest.mark.parametrize("failure", [ValueError, PermissionError])
def test_a_post_spawn_failure_is_never_refunded_whatever_its_type(
        external, relay, monkeypatch, tmp_path, capsys, failure):
    """`Popen` 성공 뒤의 실패는 종류와 무관하게 환불하지 않는다 — 자식이 있었던 실행이다.

    NUL 거절과 **같은 종류**(`ValueError`)를 반대편으로 세워, 환불을 종류로 정하는 회귀를 막는다.
    `PermissionError` 는 확정 기동 실패 종류표에 있는데도 표식이 False 면 환불 대상이 아니다."""
    repo = _repo(tmp_path / "repo", _conf())
    reviewer = _FakeReviewer(stdout=_wire("codex"))
    _wire_main(external, monkeypatch, repo, reviewer)
    monkeypatch.setattr(
        external, "_watchdog_reviewer_run",
        _runner_that_spawns_then_raises_marked(reviewer, relay, failure))

    assert external.main(["--gate", "T-0590", "--paths", "x.py"]) == 1
    err = capsys.readouterr().err

    assert len(reviewer.calls) == 1                       # 전송은 실제로 일어났다
    ledger = _round_ledger(repo)
    assert ledger["T-0590"]["count"] == 1
    assert ledger[external.WAVE_SECTION_KEY]["spent"] == 1
    assert "라운드 예약 환불" not in err


def _missing_binary_runner(argv, *, input=None, timeout=None, idle_timeout=None,
                           cwd=None, env=None, **_ignored):
    """실행 파일 부재 — exec 이 실패해 자식이 **뜬 적 없는** 실 러너 결과(started=False)."""
    raise FileNotFoundError(argv[0])


def _runner_that_spawns_then_times_out(reviewer: _FakeReviewer):
    """스폰(=전송)한 뒤 타임아웃으로 끝나는 러너 — started=True 의 대표 축(과금 가능)."""
    def _run(argv, *, input=None, timeout=None, idle_timeout=None,
             cwd=None, env=None, **_ignored):
        reviewer(argv, input=input, timeout=timeout, idle_timeout=idle_timeout,
                 cwd=cwd, env=env)
        raise subprocess.TimeoutExpired(argv, timeout or 1)
    return _run


class _RelayWithBrokenFinish:
    """실 relay 에 위임하되 raw 레코드 **마감만** 실패시키는 대역.

    `run_review` 의 정상 마감과 스폰 전 보상 경로가 **같은 relay 객체**를 쓰므로, 이 대역 하나면
    "정상 마감도 보상 마감도 실패"라는 가장 나쁜 형상이 선다."""

    def __init__(self, real, failure: type):
        self._real, self._failure = real, failure

    def __getattr__(self, name):
        return getattr(self._real, name)

    def finish_raw_record(self, *args, **kwargs):
        raise self._failure("raw 레코드 마감 실패(주입)")


def _break_raw_finish(external, monkeypatch, failure: type) -> None:
    monkeypatch.setattr(
        external, "_load_relay",
        lambda real=_RelayWithBrokenFinish(_load("pm_relay"), failure): real)


# 예약 seam 의 소유권 상태 기계 — (거쳐 온 전이, 남은 환불 횟수). 전이 이름이 곧 사실이다:
# hand_off=스폰 구간이 판정한다 · reclaim_no_spawn=자식 없음이 판명됐다 · settle_refunded=마감이
# 이미 되돌렸다. 어떤 순서로 와도 환불은 **최대 한 번**이어야 한다(두 번이면 상한이 조용히 늘어난다).
_OWNERSHIP_TRANSITIONS = [
    ((), 1),                                                  # 스폰 전 구간에서 그대로 이탈
    (("hand_off",), 0),                                       # 스폰됐(을 수 있)다 — 되돌리지 않는다
    (("hand_off", "reclaim_no_spawn"), 1),                    # 자식 없음 판명 — 권리 회복
    (("hand_off", "reclaim_no_spawn", "settle_refunded"), 0),  # 마감이 이미 되돌렸다
    (("hand_off", "reclaim_no_spawn", "hand_off"), 0),        # 회복 뒤 다시 넘김(started=True 대역)
]


@pytest.mark.parametrize("transitions,refunds", _OWNERSHIP_TRANSITIONS)
def test_the_reservation_seam_refunds_at_most_once_in_every_order(
        external, monkeypatch, transitions, refunds):
    """소유권 전이가 어떤 순서로 와도 이 seam 의 환불은 0 또는 1 회다.

    `reclaim_no_spawn` 은 `hand_off` 의 대칭이라 권리를 되돌려 놓지만, 되돌린 권리도 여전히 한 번만
    쓰인다 — `release` 와 `__exit__` 이 겹쳐 호출돼도(여러 층이 같은 예외를 잡는 형상) 두 번째는
    no-op 이어야 한다."""
    released: list[str] = []
    monkeypatch.setattr(
        external, "_release_round_reservation",
        lambda budget, *, reason: released.append(reason))
    budget = external.RoundBudget(
        gate="T-0590", round_id="round-1", sequence=1, wave_id="wave-1")

    with pytest.raises(RuntimeError):
        with external._PreSpawnReservation(budget) as reservation:
            for transition in transitions:
                getattr(reservation, transition)()
            raise RuntimeError("구간 이탈(주입)")
    reservation.release(reason="중복 호출")          # 겹친 층의 두 번째 환불 시도

    assert len(released) == refunds


@pytest.mark.parametrize("machine_refunded", [True, False])
def test_the_release_seam_reports_whether_it_actually_refunded(
        external, monkeypatch, machine_refunded):
    """`release` 는 **이번 호출이 실제로 되돌렸는지**를 돌려준다 (no-op·환불 실패는 False).

    예외로 나가는 구간은 이 값을 쓰지 않지만, 정상 return 하는 마감 저장 실패 경로는 안내 문구를
    이 사실에 맞춰야 한다 — 되돌리지 못한 예약을 "되돌렸다"고 말하면 장부와 표면이 갈린다."""
    reasons: list[str] = []

    def _machine(budget, *, reason: str) -> bool:
        reasons.append(reason)
        return machine_refunded

    monkeypatch.setattr(external, "_release_round_reservation", _machine)
    reservation = external._PreSpawnReservation(external.RoundBudget(
        gate="T-0590", round_id="round-1", sequence=1, wave_id="wave-1"))
    reservation.reclaim_no_spawn()                   # 자식 없음 판명 — 권리 회복

    assert reservation.release(reason="마감 실패") is machine_refunded
    assert reservation.release(reason="두 번째") is False    # 정산 뒤에는 no-op
    assert reasons == ["마감 실패"]                           # 환불 기계는 한 번만 돈다


# 판명된 no-spawn 뒤의 수합 구간에서 나갈 수 있는 실패. `BrokenPipeError` 는 실측 축이다 — 닫힌
# stdout 파이프(`| head`·죽은 상위 프로세스)로 요약을 쓰면 여기서 터진다.
_POST_NO_SPAWN_FAILURES = (BrokenPipeError, RuntimeError)


@pytest.mark.parametrize("failure", _POST_NO_SPAWN_FAILURES)
def test_a_proven_no_spawn_refunds_even_if_the_summary_dies(
        external, monkeypatch, tmp_path, capsys, failure):
    """스폰이 **없었다고 판명된** 뒤 요약 출력이 죽어도 예약은 되돌아간다.

    소유권 이전은 "스폰할 수도 있다"에 대한 것이고, 러너가 `started=False` 로 돌아온 순간 그 전제는
    사라진다. 그 뒤로도 소유권을 넘긴 채 두면 요약·진단·마감 중 어느 하나만 죽어도(닫힌 stdout
    파이프의 `BrokenPipeError`) 자식이 뜬 적 없는 실행이 finished_at 없는 미완 레코드로 남아,
    상한 1·미완 상한 1·wave 1 형상에서 다음 **정상** 호출이 곧바로 rc=4 로 막힌다. 예외는 그대로
    전파되고(진단은 주 예외가 소유한다) raw 레코드는 이미 정직하게 닫혀 있어야 한다."""
    repo = _repo(tmp_path / "repo", _conf(
        additional_reviewer_round_limit="1", additional_reviewer_incomplete_round_limit="1",
        additional_reviewer_wave_budget="1"))
    reviewer = _FakeReviewer(stdout=_wire("codex"))
    _wire_main(external, monkeypatch, repo, reviewer)
    real_summary = external.print_summary

    def _boom(*args, **kwargs):
        raise failure("요약 출력 실패(주입)")

    monkeypatch.setattr(external, "_watchdog_reviewer_run", _missing_binary_runner)
    monkeypatch.setattr(external, "print_summary", _boom)
    with pytest.raises(failure):
        external.main(["--gate", "T-0590", "--paths", "x.py"])
    err = capsys.readouterr().err

    assert reviewer.calls == []                                   # 자식 0
    ledger = _round_ledger(repo)
    assert (ledger["T-0590"]["count"], ledger["T-0590"]["records"]) == (0, [])
    assert ledger[external.WAVE_SECTION_KEY]["spent"] == 0         # 두 축을 같은 조건으로
    assert ledger["T-0590"]["rounds"] == []                        # 산출도 남기지 않는다
    assert err.count("라운드 예약 환불: 게이트") == 1               # 조용히도, 두 번도 아니다
    # raw 는 `run_review` 가 이미 정직하게 닫았다 — 보상 경로가 닫힌 레코드를 다시 덮지 않는다.
    records = _raw_ledger(repo)
    assert len(records) == 1 and records[0]["rc"] == 127
    assert records[0]["finished_at"] is not None
    assert "스폰 전 중단" not in _raw_text(repo)

    # 되돌린 슬롯은 살아 있다 — 세 예산이 모두 1 인데 다음 정상 호출이 그대로 전송된다.
    monkeypatch.setattr(external, "_watchdog_reviewer_run", reviewer)
    monkeypatch.setattr(external, "print_summary", real_summary)
    assert external.main(["--gate", "T-0590", "--paths", "x.py"]) == 0
    capsys.readouterr()
    assert len(reviewer.calls) == 1
    assert _round_ledger(repo)["T-0590"]["count"] == 1


@pytest.mark.parametrize("seam", ["raw-write", "raw-finish"])
def test_a_proven_no_spawn_refunds_and_stays_loud_when_raw_bookkeeping_dies(
        external, monkeypatch, tmp_path, capsys, seam):
    """판명된 no-spawn 뒤의 raw 박제·레코드 마감이 죽어도 라운드는 되돌아가고 장부는 loud 하다.

    이 구간은 두 되돌림을 동시에 요구한다. (1) 라운드 예약 — 자식이 없었으니 환불한다. (2) raw
    선점/미마감 레코드 — 스폰 전 중단과 **같은 보상 경로**로 닫는다. 보상 자체가 또 실패해도
    주 예외를 덮지 않고(중단 사유는 주 예외가 소유한다) 실패 사실만 경고로 남긴다 — 조용히
    성공한 척하는 표면이 없어야 사후에 장부를 믿을 수 있다."""
    repo = _repo(tmp_path / "repo", _conf(
        additional_reviewer_round_limit="1", additional_reviewer_incomplete_round_limit="1"))
    reviewer = _FakeReviewer(stdout=_wire("codex"))
    _wire_main(external, monkeypatch, repo, reviewer)
    monkeypatch.setattr(external, "_watchdog_reviewer_run", _missing_binary_runner)
    if seam == "raw-write":
        def _boom(*args, **kwargs):
            raise RuntimeError("raw 박제 실패(주입)")

        monkeypatch.setattr(external, "_write_reserved_output", _boom)
    else:
        _break_raw_finish(external, monkeypatch, RuntimeError)

    with pytest.raises(RuntimeError):                 # 주 예외가 1순위 그대로
        external.main(["--gate", "T-0590", "--paths", "x.py"])
    err = capsys.readouterr().err

    assert reviewer.calls == []                                   # 자식 0
    ledger = _round_ledger(repo)
    assert (ledger["T-0590"]["count"], ledger["T-0590"]["records"]) == (0, [])
    assert ledger[external.WAVE_SECTION_KEY]["spent"] == 0
    assert err.count("라운드 예약 환불: 게이트") == 1               # 정확히 한 번
    records = _raw_ledger(repo)
    assert len(records) == 1
    if seam == "raw-write":
        # 박제만 실패했다 — 레코드는 실패 축으로 닫고, 빈 파일이 남았다고 말한다.
        assert records[0]["rc"] == external._PRE_SPAWN_ABORT_RC
        assert records[0]["finished_at"] is not None
        assert "스폰 전 중단 사유의 raw 박제 실패" in err
    else:
        # 마감이 두 번 다 실패했다 — 미마감으로 남되 "실제로는 스폰 0"을 함께 말한다.
        assert records[0].get("finished_at") is None
        assert "스폰 전 중단 레코드 마감 실패" in err
        assert "실제로는 스폰 0" in err
        raw = _raw_text(repo)
        assert "스폰 전 중단" in raw and "전송 0·과금 0" in raw


def _flaky_ledger_save(external, monkeypatch, *, fail_from: int, fail_to: int | None = None):
    """`_save_round_ledger` 를 `fail_from`~`fail_to` 번째 호출에서만 실패시키는 대역.

    호출 순서가 곧 구간이다: 1=예약 저장 · 2=마감 저장 · 3=마감 실패 뒤의 보상 환불 저장.
    락 경합(Windows `msvcrt.locking` 재시도 소진)이 실 유입원이라 예외는 `OSError` 다.
    반환 dict 로 실제 호출 횟수를 본다 — 보상 경로가 돌았는지를 문구가 아니라 횟수로 확인한다."""
    real_save = external._save_round_ledger
    saves = {"n": 0}

    def _save(ledger):
        saves["n"] += 1
        if saves["n"] >= fail_from and (fail_to is None or saves["n"] <= fail_to):
            raise OSError(11, "resource temporarily unavailable")
        return real_save(ledger)

    monkeypatch.setattr(external, "_save_round_ledger", _save)
    return saves


def test_a_proven_no_spawn_refunds_when_the_finalization_save_fails(
        external, monkeypatch, tmp_path, capsys):
    """마감 **저장**이 실패해도 판명된 no-spawn 의 예약은 되돌아간다 — 정상 return 경로의 소유.

    이 경로는 예외를 삼키고 판정 rc 를 그대로 돌려주므로 스폰 전 seam 의 `__exit__` 가 소유를
    보지 못한다. 되돌리지 않으면 자식이 뜬 적 없는(전송 0·과금 0) 실행이 라운드 count·wave 예산을
    먹은 채 미완으로 남아, 상한 1·미완 상한 1·wave 1 형상에서 다음 **정상** 호출이 곧바로 rc=4 로
    막힌다. 마감 저장은 실패했지만 환불 저장은 성공하는 형상이 실측 축이다(락 경합은 지나간다)."""
    repo = _repo(tmp_path / "repo", _conf(
        additional_reviewer_round_limit="1", additional_reviewer_incomplete_round_limit="1",
        additional_reviewer_wave_budget="1"))
    reviewer = _FakeReviewer(stdout=_wire("codex"))
    _wire_main(external, monkeypatch, repo, reviewer)
    monkeypatch.setattr(external, "_watchdog_reviewer_run", _missing_binary_runner)
    saves = _flaky_ledger_save(external, monkeypatch, fail_from=2, fail_to=2)

    assert external.main(["--gate", "T-0590", "--paths", "x.py"]) == 1   # 판정 rc 보존
    err = capsys.readouterr().err

    assert reviewer.calls == []                                   # 자식 0 (전송·과금 0)
    assert saves["n"] == 3                        # 예약 · 마감(실패) · 보상 환불
    ledger = _round_ledger(repo)
    assert (ledger["T-0590"]["count"], ledger["T-0590"]["records"]) == (0, [])
    assert ledger[external.WAVE_SECTION_KEY]["spent"] == 0         # 두 축을 같은 조건으로
    assert ledger["T-0590"]["rounds"] == []                        # 산출도 남기지 않는다
    assert "라운드 장부 마감 실패" in err                            # 실패는 여전히 loud
    assert err.count("라운드 예약 환불: 게이트") == 1                # 조용히도, 두 번도 아니다
    assert "미완으로 남아" not in err              # 되돌린 라운드를 미완이라고 말하지 않는다

    # 되돌린 슬롯은 살아 있다 — 세 예산이 모두 1 인데 다음 정상 호출이 그대로 스폰·성공한다.
    monkeypatch.setattr(external, "_watchdog_reviewer_run", reviewer)
    assert external.main(["--gate", "T-0590", "--paths", "x.py"]) == 0
    capsys.readouterr()
    assert len(reviewer.calls) == 1                                # 재시도는 실제로 스폰됐다
    ledger = _round_ledger(repo)
    assert ledger["T-0590"]["count"] == 1
    assert [row.get("finished_at") is not None for row in _round_records(repo)] == [True]


def test_a_failing_compensation_after_no_spawn_stays_loud_and_conservative(
        external, monkeypatch, tmp_path, capsys):
    """보상 환불까지 실패하면 예약은 미완으로 남되 두 실패가 모두 loud 하다 (보수적 방향).

    장부가 실제보다 헐거워지는 방향(전송한 라운드를 안 셈)으로 틀리지 않는 대신, 되돌리지 못한
    사실은 조용히 숨기지 않는다 — 사후에 장부를 믿으려면 실패 표면이 남아야 한다."""
    repo = _repo(tmp_path / "repo", _conf(
        additional_reviewer_round_limit="1", additional_reviewer_incomplete_round_limit="1"))
    reviewer = _FakeReviewer(stdout=_wire("codex"))
    _wire_main(external, monkeypatch, repo, reviewer)
    monkeypatch.setattr(external, "_watchdog_reviewer_run", _missing_binary_runner)
    saves = _flaky_ledger_save(external, monkeypatch, fail_from=2)   # 마감·보상 모두 실패

    assert external.main(["--gate", "T-0590", "--paths", "x.py"]) == 1   # 판정 rc 보존
    err = capsys.readouterr().err

    assert reviewer.calls == []                                   # 자식 0
    assert saves["n"] == 3                                        # 보상은 시도했다
    ledger = _round_ledger(repo)
    assert ledger["T-0590"]["count"] == 1                         # 되돌리지 못한 예약
    assert [row.get("finished_at") for row in _round_records(repo)] == [None]  # 미완
    assert "라운드 장부 마감 실패" in err
    assert "라운드 예약 환불 실패" in err                           # 보상 실패도 말한다
    assert "미완으로 남아" in err
    assert "라운드 예약 환불: 게이트" not in err                     # 성공을 사칭하지 않는다


def test_a_spawned_run_is_not_refunded_when_the_finalization_save_fails(
        external, monkeypatch, tmp_path, capsys):
    """같은 마감 저장 실패라도 **스폰된** 실행은 되돌리지 않는다 — 프롬프트가 이미 나갔다.

    보상 경로의 조건은 판명된 `started=False` 하나뿐이다. 저장 실패를 조건으로 넓히면 타임아웃·
    비정상 종료로 죽은 과금 라운드까지 되돌아가 상한 무한 우회가 열린다(MF-A)."""
    repo = _repo(tmp_path / "repo", _conf())
    reviewer = _FakeReviewer(stdout=_wire("codex"))
    _wire_main(external, monkeypatch, repo, reviewer)
    saves = _flaky_ledger_save(external, monkeypatch, fail_from=2, fail_to=2)

    assert external.main(["--gate", "T-0590", "--paths", "x.py"]) == 0   # 통과 판정 그대로
    err = capsys.readouterr().err

    assert len(reviewer.calls) == 1                               # 스폰은 실제로 일어났다
    assert saves["n"] == 2                                        # 보상 저장은 없다
    ledger = _round_ledger(repo)
    assert ledger["T-0590"]["count"] == 1                         # 예약은 그대로 소비
    assert ledger[external.WAVE_SECTION_KEY]["spent"] == 1
    assert [row.get("finished_at") for row in _round_records(repo)] == [None]  # 미완
    assert "라운드 장부 마감 실패" in err and "미완으로 남아" in err
    assert "라운드 예약 환불" not in err


@pytest.mark.parametrize("runner_kind,seam", [
    ("완주", "summary"), ("타임아웃", "summary"), ("완주", "raw-finish"),
])
def test_a_spawned_run_is_never_refunded_by_the_same_failures(
        external, monkeypatch, tmp_path, capsys, runner_kind, seam):
    """같은 실패라도 **스폰된** 실행은 되돌리지 않는다 — 프롬프트가 이미 나갔을 수 있다.

    반납 조건은 판명된 `started=False` 하나뿐이다. 타임아웃까지 되돌리면 과금된 호출이 상한을
    소비하지 않아 반복 타임아웃으로 무한 우회가 열린다(MF-A). 넘어간 예약은 finished_at 없는 미완
    레코드로 남아 다음 실행의 재시도 예산에서 보수적으로 세어진다."""
    repo = _repo(tmp_path / "repo", _conf())
    reviewer = _FakeReviewer(stdout=_wire("codex"))
    _wire_main(external, monkeypatch, repo, reviewer)
    if runner_kind == "타임아웃":
        monkeypatch.setattr(external, "_watchdog_reviewer_run",
                            _runner_that_spawns_then_times_out(reviewer))
    if seam == "summary":
        def _boom(*args, **kwargs):
            raise RuntimeError("요약 출력 실패(주입)")

        monkeypatch.setattr(external, "print_summary", _boom)
    else:
        _break_raw_finish(external, monkeypatch, RuntimeError)

    with pytest.raises(RuntimeError):
        external.main(["--gate", "T-0590", "--paths", "x.py"])
    err = capsys.readouterr().err

    assert len(reviewer.calls) == 1                               # 스폰은 실제로 일어났다
    ledger = _round_ledger(repo)
    assert ledger["T-0590"]["count"] == 1                         # 예약은 그대로 소비
    assert ledger[external.WAVE_SECTION_KEY]["spent"] == 1
    assert [row.get("finished_at") for row in _round_records(repo)] == [None]  # 미완
    assert "라운드 예약 환불" not in err
    assert "스폰 전 중단" not in err                                # 보상 경로도 타지 않는다


@pytest.mark.parametrize("failure", _PRE_SPAWN_FAILURES)
def test_a_failing_refund_never_masks_the_original_pre_spawn_failure(
        external, monkeypatch, tmp_path, capsys, failure):
    """환불이 **평범한 정리 실패**(락 경합·장부 IO)로 끝나도 주 예외가 그대로 1순위로 나간다.

    중단 사유는 주 예외가 소유한다 — 정리 실패가 그것을 덮으면 이 실행이 왜 죽었는지가 어느
    표면에도 남지 않는다. 되돌리지 못한 예약은 loud 경고 + 미완 레코드로 남아 다음 실행의 재시도
    예산에서 보수적으로 세어진다(장부가 실제보다 헐거워지는 방향으로는 틀리지 않는다)."""
    repo = _repo(tmp_path / "repo", _conf())
    reviewer = _FakeReviewer(stdout=_wire("codex"))
    _wire_main(external, monkeypatch, repo, reviewer)
    real_lock = external._round_ledger_lock
    lock_broken: list[bool] = []

    def _lock(*args, **kwargs):
        if lock_broken:                              # 예약은 끝났고 환불만 락을 못 잡는다
            raise OSError("장부 락 실패(주입)")
        return real_lock(*args, **kwargs)

    def _boom(*args, **kwargs):
        lock_broken.append(True)
        raise failure("구조화 transport 준비 실패(주입)")

    monkeypatch.setattr(external, "_round_ledger_lock", _lock)
    monkeypatch.setattr(external, "_structured_transport", _boom)
    with pytest.raises(failure):                     # 주 예외가 1순위 그대로
        external.main(["--gate", "T-0590", "--paths", "x.py"])
    err = capsys.readouterr().err

    assert reviewer.calls == []
    assert "라운드 예약 환불 실패" in err              # 정리 실패는 조용히 넘어가지 않는다
    assert "라운드 예약 환불: 게이트" not in err        # 되돌리지 못했으면 그렇게 말하지 않는다
    assert _round_ledger(repo)["T-0590"]["count"] == 1
    assert [row.get("finished_at") for row in _round_records(repo)] == [None]


def test_an_unexpected_refund_failure_stays_fatal_and_keeps_the_original_context(
        external, monkeypatch, tmp_path):
    """환불 경로가 삼키는 예외는 **OSError 하나뿐**이다 — 그 밖은 엔진 결함이라 그대로 뜬다.

    이 계층이 하중을 받는다: 락 경합·장부 IO 실패는 이 실행의 결과가 아니라 다음 실행의 미완
    예산으로 흡수되지만(경고만 남기고 삼킨다), 환불 기계 자체의 결함까지 정리 경로가 삼키면
    장부가 조용히 틀린 채로 계속 돈다. 주 예외는 `__context__` 로 그대로 따라온다(진단 유실 없음).
    """
    repo = _repo(tmp_path / "repo", _conf())
    reviewer = _FakeReviewer(stdout=_wire("codex"))
    _wire_main(external, monkeypatch, repo, reviewer)

    def _refund_boom(*args, **kwargs):
        raise ValueError("환불 기계 결함(주입)")       # OSError 가 아니다 = 예상 밖

    def _boom(*args, **kwargs):
        raise RuntimeError("구조화 transport 준비 실패(주입)")

    monkeypatch.setattr(external, "_refund_reserved_round", _refund_boom)
    monkeypatch.setattr(external, "_structured_transport", _boom)
    with pytest.raises(ValueError) as caught:
        external.main(["--gate", "T-0590", "--paths", "x.py"])

    assert isinstance(caught.value.__context__, RuntimeError)
    assert reviewer.calls == []


def test_permitted_run_still_creates_the_requested_output_dir(
        external, monkeypatch, tmp_path, capsys):
    """게이트를 모두 통과한 실행에서는 `--output-dir` 이 그대로 산출 위치가 된다(기능 보존)."""
    repo = _repo(tmp_path / "repo", _conf())
    reviewer = _FakeReviewer(stdout=_wire("codex"))
    _wire_main(external, monkeypatch, repo, reviewer)
    outdir = tmp_path / "raw-out"

    assert external.main(["--paths", "x.py", "--output-dir", str(outdir)]) == 0
    capsys.readouterr()

    assert outdir.is_dir()
    assert len(list(outdir.glob("external_review_*.txt"))) == 1
    assert (outdir / "raw_outputs.json").is_file()
    # 격리 산출은 PM 홈 기본 위치를 쓰지 않는다(종전 격리 계약 보존).
    assert not (repo / ".project_manager" / ".local" / "review").exists()


def test_dry_run_under_network_off_is_side_effect_free_and_prints_the_prefix_rule(
        external, monkeypatch, tmp_path, capsys):
    """미리보기는 차단 환경에서도 그대로 돈다(외부 송신 0) — 처방만 정확히 낸다."""
    monkeypatch.setenv(_EGRESS_MARKER, "1")
    repo = _repo(tmp_path / "repo", _conf())
    reviewer = _FakeReviewer(stdout=_wire("codex"))
    _wire_main(external, monkeypatch, repo, reviewer)
    outdir = tmp_path / "raw-out"

    rc = external.main(["--gate", "T-0590", "--paths", "x.py", "--dry-run",
                        "--output-dir", str(outdir)])
    out = capsys.readouterr().out

    assert rc == 0
    assert reviewer.calls == []
    # 미리보기는 로컬에서 그대로 쓸모 있어야 하지만(대상·처방·프롬프트를 다 보여준다), 요청한
    # 산출 디렉토리를 **만들지는 않는다** — 부작용 0 은 파일시스템에서도 참이어야 한다.
    assert not outdir.exists()
    assert not (repo / ".project_manager" / ".local").exists()
    assert "=== [dry-run] 프롬프트 미리보기 (외부 전송 없음) ===" in out
    assert "diff --git a/x.py b/x.py" in out                  # 실제 나갈 내용 그대로
    assert f"Codex egress: escalation required ({_EGRESS_MARKER}=true)" in out
    assert ('prefix_rule=["python3", ".project_manager/tools/external_review.py"]'
            in out)
    assert f"실 실행은 {external.CODEX_EGRESS_FLAG} 없이는 스폰 전 rc=1 로 중단됩니다." in out


def test_attested_run_proceeds_and_labels_the_boundary_everywhere(
        external, monkeypatch, tmp_path, capsys):
    """증명 실행은 그대로 돌고, egress 라벨이 stderr·raw 헤더·장부에 같은 값으로 남는다."""
    monkeypatch.setenv(_EGRESS_MARKER, "1")
    repo = _repo(tmp_path / "repo", _conf())
    reviewer = _FakeReviewer(stdout=_wire("codex"))
    _wire_main(external, monkeypatch, repo, reviewer)

    assert external.main(["--paths", "x.py", external.CODEX_EGRESS_FLAG]) == 0
    err = capsys.readouterr().err

    assert len(reviewer.calls) == 1                       # 실행됨(sandbox 완화 시도 없음)
    label = external._load_relay().CODEX_EGRESS_ESCALATED_ATTESTED
    assert f"codex_egress={label}" in err
    assert f"# codex_egress: {label}" in _raw_text(repo)
    assert _raw_ledger(repo)[0]["codex_egress"] == label
    # 엔진은 마커를 지우지 않는다 — 자식에게도 그대로 전달되는 실측 형상 보존.
    assert os.environ[_EGRESS_MARKER] == "1"


@pytest.mark.parametrize("windows,expected_prefix", [
    (False, "python3 .project_manager/tools/external_review.py"),
    (True, "py .project_manager/tools/external_review.py"),
])
def test_retry_command_starts_with_this_surface_entrypoint(
        external, relay, monkeypatch, windows, expected_prefix):
    """재실행 안내는 승인 prefix 와 **같은 2-token** 으로 시작한다(재승인 유발 금지)."""
    monkeypatch.setattr(external, "_running_on_windows", lambda: windows)
    message = relay.codex_egress_block_message(
        ["--gate", "T-0590", "--paths", "x.py"], "codex", "gpt-5.6-sol",
        script=relay.EXTERNAL_REVIEW_ENTRYPOINT,
        consent_key=external.ADDITIONAL_REVIEWER_ENABLED_KEY,
        subject="추가 리뷰어 외부 전송",
        windows=windows,
    )
    retry = next(line for line in message.splitlines() if "재실행: " in line)
    assert retry.strip().startswith(f"· 재실행: {expected_prefix} ")
    # 증명 플래그는 같은 호출에 하나만 더해진다(다른 수신자로 갈아타지 않는다). Windows 는 첫
    # 2 token 만 그대로 두고 나머지를 PowerShell literal 로 인용하므로 표기만 다르다.
    assert retry.rstrip().endswith(
        f"'{external.CODEX_EGRESS_FLAG}'" if windows else external.CODEX_EGRESS_FLAG)
    assert "--gate" in retry and "T-0590" in retry
    assert "powershell.exe" not in retry
    assert relay.codex_egress_prefix_rule_text(
        relay.EXTERNAL_REVIEW_ENTRYPOINT, windows=windows) in message


# ══ ⑧ 모듈 경계 (cycle-free 공용 계약) ═════════════════════════════════════


def _sibling_loads(name: str) -> set[str]:
    """모듈이 실제로 **로드**하는 형제 엔진 파일명 집합.

    문자열 상수·주석의 언급(진입점 경로 표기 등)은 import 가 아니다 — 로더 호출
    (`_require_engine_sibling`/`_load_module_from_path`)의 파일명 인자와 `import` 문만 센다."""
    tree = ast.parse((TOOLS / name).read_text(encoding="utf-8"))
    loaded: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            loaded.update(f"{alias.name}.py" for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            loaded.add(f"{node.module}.py")
        elif isinstance(node, ast.Call):
            called = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if called not in ("_require_engine_sibling", "_load_module_from_path"):
                continue
            loaded.update(
                argument.value for argument in node.args
                if isinstance(argument, ast.Constant)
                and isinstance(argument.value, str) and argument.value.endswith(".py")
            )
    return loaded


def test_pm_relay_is_cycle_free(relay):
    """공용 계약 모듈은 다른 엔진 표면을 읽지 않는다 — 양쪽이 안전하게 deep-import 한다."""
    loads = {name for name in _sibling_loads("pm_relay.py") if (TOOLS / name).is_file()}
    assert "pm_delegate.py" not in loads
    assert "external_review.py" not in loads
    # 형제는 부트스트랩 로더와 공용 파일락뿐이고, 그 둘은 아무것도 되로드하지 않는 leaf 다 —
    # 어느 표면이 relay 를 deep-import 해도 순환이 생기지 않는다.
    assert loads == {"file_lock.py", "repo_owned_files.py"}
    for leaf in sorted(loads):
        assert {name for name in _sibling_loads(leaf) if (TOOLS / name).is_file()} == set()
    assert Path(relay._load_file_lock().__file__).name == "file_lock.py"


def test_external_review_never_imports_pm_delegate(external):
    """역방향 deep-import 가 이미 있어 여기서 되부르면 순환이 된다."""
    assert "pm_delegate.py" not in _sibling_loads("external_review.py")
    assert not hasattr(external, "pm_delegate")
    assert Path(external._load_relay().__file__).name == "pm_relay.py"
    # 위임 쪽은 반대 방향 deep-import 를 계속 보유한다(순환이 되려면 이 짝이 필요하다).
    assert "external_review.py" in _sibling_loads("pm_delegate.py")


def test_delegate_public_wrappers_still_speak_the_shared_contract():
    """위임 공개 API 는 시그니처·값 모두 공용 계약을 그대로 통과시킨다(T-0592 행동 보존)."""
    delegate = _load("pm_delegate")
    shared = delegate._load_relay()
    assert delegate.build_codex_argv("m", "high", "developer", "/w") == \
        shared.build_codex_argv("m", "high", "developer", "/w")
    assert delegate.build_claude_argv("m", "high", "developer") == \
        shared.build_claude_argv("m", "high", "developer")
    assert delegate.build_opencode_argv("m", "high", "developer", "/w", "/p") == \
        shared.build_opencode_argv("m", "high", "developer", "/w", "/p")
    # codex `max` 는 두 표면에서 같은 판정을 받는다(테이블이 하나라서).
    assert delegate._validate_reasoning("codex", "max") == "max"
    # 회신 파서 계약 — 같은 wire 를 같은 텍스트로 읽는다.
    for harness in ("codex", "claude", "opencode"):
        wire = _wire(harness)
        assert delegate.extract_reply(harness, wire) == _PASS_REPLY
        assert shared.extract_harness_reply(harness, wire) == _PASS_REPLY
    # egress 브리지는 위임 진입점을 유지한다(표면별 인자는 진입점·동의 키뿐).
    assert delegate._codex_egress_entrypoint()[1] == shared.DELEGATE_ENTRYPOINT
    assert shared.DELEGATE_ENTRYPOINT != shared.EXTERNAL_REVIEW_ENTRYPOINT


def test_delegate_and_reviewer_reject_the_same_misconfiguration_wording():
    """두 표면의 오설정 진단은 문구까지 같은 소유자에게서 나온다."""
    delegate = _load("pm_delegate")
    external = _load("external_review")
    with pytest.raises(delegate.DelegateError) as delegate_error:
        delegate._validate_reasoning("opencode", "xhigh")
    with pytest.raises(external.ReviewerTargetError) as reviewer_error:
        external.resolve_reviewer_target(
            {"additional_reviewer.harness": "opencode",
             "additional_reviewer.model": "m",
             "additional_reviewer.reasoning": "xhigh"})
    assert str(delegate_error.value) in str(reviewer_error.value)


# ══ ⑨ 문구 규율 (사람 이름 · anti-loop ack · 기계 식별자 불변) ═════════════

# 실행 세 도구 + 채택자 온보딩 표면(board.py·pm_update.py). 온보딩은 T-0590 시점엔 병렬 작업의
# 소유라 빠져 있었고, 게이트 키 개칭(T-0597)으로 그쪽이 수렴하면서 같은 규율로 확장됐다.
_OWNED_TOOLS = (
    "external_review.py", "pm_delegate.py", "pm_relay.py", "board.py", "pm_update.py",
)


@pytest.mark.parametrize("name", _OWNED_TOOLS)
def test_owned_tools_call_the_person_an_additional_reviewer(name):
    """사람 역할 이름은 **추가 리뷰어** 다 — 팀에 한 명 더 붙는 리뷰어다."""
    source = (TOOLS / name).read_text(encoding="utf-8")
    assert "외부 리뷰어" not in source


def test_reviewer_surface_states_the_name_and_keeps_the_transport_axis(external):
    """`external` 은 전송/격리/과금 축과 기계 식별자에만 남는다."""
    source = (TOOLS / "external_review.py").read_text(encoding="utf-8")
    assert "추가 리뷰어" in source
    assert "외부 전송" in source and "과금" in source          # 전송 축 문구는 유지
    # 설정 키는 역할 이름과 같은 축으로 통일됐다(T-0597) — 구키는 fallback 으로만 남는다.
    assert external.ADDITIONAL_REVIEWER_ENABLED_KEY == "additional_reviewer_enabled"
    assert external.LEGACY_EXTERNAL_REVIEW_ENABLED_KEY == "external_review_enabled"
    assert external.LEGACY_REVIEWER_CMD_KEY == "reviewer_cmd"
    assert external.ADDITIONAL_REVIEWER_PREFIX == "additional_reviewer"
    # 파일 이름은 개칭하지 않는다 — 동기가 상류 부재 파일을 지우지 않아 채택자 PM 홈에 구 사본이
    # 남고(두 진입점 공존), 이미 기록된 raw 감사물의 접두와도 어긋난다. 이력은 docstring 1줄.
    assert (TOOLS / "external_review.py").is_file()
    assert not (TOOLS / "additional_reviewer.py").exists()
    assert "명칭 이력:" in source
    assert "external_review.py" in source.split('"""')[1]     # 모듈 docstring 안


def test_round_and_wave_caps_are_anti_loop_without_round_extension(external):
    """상한의 성격은 무한 루프 차단이다 — 비용 재승인 요구가 아니고, 라운드 연장은 폐지됐다."""
    for guidance in (external._ROUND_LIMIT_GUIDANCE, external._CONVERGENCE_GUIDANCE,
                     external._WAVE_BUDGET_GUIDANCE):
        assert "--rounds-report" in guidance                # 먼저 볼 조회면
        assert "대기" not in guidance                        # '사용자 승인 대기' 규율 삭제
        assert "승인 후" not in guidance
    # 라운드 축의 출구는 재설계·분할뿐이다 (연장 승인 없음).
    for guidance in (external._ROUND_LIMIT_GUIDANCE, external._CONVERGENCE_GUIDANCE):
        assert "재설계" in guidance and "분할" in guidance
    # wave 축만 승인으로 재개한다 (별개 비용 축).
    assert "--ack-wave" in external._WAVE_BUDGET_GUIDANCE
    assert "자율" in external._WAVE_BUDGET_GUIDANCE
    # 지속 동의 = 한 번 켜면 호출마다 비용을 다시 묻지 않는다.
    assert "지속 동의" in external._is_enabled.__doc__


def test_reviewer_source_comments_match_structured_target_and_durable_consent():
    """능동 source 설명이 legacy-only·사용자 비용 재승인 계약으로 되돌아가지 않는다."""
    source = (TOOLS / "external_review.py").read_text(encoding="utf-8")
    stale = (
        "reviewer_cmd 의 하네스 프로필",
        "리뷰어 커맨드의 하네스 프로필",
        "read-only 인자 사용 권장",
        "반쯤에서 한 번 사용자 확인",
        "승인 판단은 사용자/카드",
        "사용자가 준 승인",
        "같은 승인을 다시 받아야",
    )
    assert [phrase for phrase in stale if phrase in source] == []
