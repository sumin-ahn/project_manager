"""추가 리뷰어 회신 품질 검사 (T-0563 → T-0887).

리뷰어 회신 본문에 옛 리뷰 raw 파일명·세션 전사 경로·엇갈린 판정 라인이 섞이면 "이번 판정이 이
리뷰의 것인가"를 기계가 확정할 수 없다. 실측 형상(T-0544 raw)은 같은 세션 내부 reviewer 보고
verbatim + 옛 리뷰 raw 재인용이었고, 파서는 앞 블록만 보고 통과를 냈다. 그래서 검출은 loud 진단
으로 올리고 판정은 '판정 불명확'(보수적 exit 1)으로 강등한다.

이 검사는 **출력 품질**이지 접근 권한이 아니다 — 입력은 회신 텍스트 하나뿐이고 하네스로 갈리지
않는다. 추가 리뷰어의 실행 조건(cwd·env)은 위임 채널과 같은 seam 이 소유한다(T-0887).

hermetic: 실 리뷰어 프로세스를 스폰하지 않는다(호출 0). `run_fn` 주입으로 판정 파이프라인만 본다.
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / ".project_manager" / "tools"


def _load(name: str = "additional_reviewer"):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def external():
    return _load("additional_reviewer")


# ── 회신 품질 검출 ────────────────────────────────────────────────────────


def test_detects_old_raw_artifact_citation(external):
    output = (
        "판정: 통과\n\n**must-fix**:\n- 없음\n\n"
        "참고: .project_manager/.local/review/"
        "additional_reviewer_codex_20260806_040406_11_ab.txt 의 지적과 동일하다.\n"
    )
    contamination = external.detect_output_contamination(output)
    assert contamination.raw_artifacts == (
        "additional_reviewer_codex_20260806_040406_11_ab.txt",
    )
    assert any("raw 파일명 인용" in marker for marker in contamination.markers)


def test_detects_session_transcript_citation(external):
    output = "판정: 통과\n\n/home/user/.claude/projects/-home-user-repo/9f2.jsonl 를 확인했다.\n"
    contamination = external.detect_output_contamination(output)
    assert contamination.transcripts and any(
        "세션 전사" in marker for marker in contamination.markers)


def test_detects_conflicting_verdict_blocks(external):
    """옛 판정 블록이 앞에 echo 되면 파서가 그걸 이번 판정으로 읽는다 — 다중/불일치를 세운다."""
    output = (
        "이전 라운드 원문:\n판정: 반려\n\n**must-fix**:\n- 옛 지적\n\n"
        "---\n이번 리뷰:\n판정: 통과\n\n**must-fix**:\n- 없음\n"
    )
    contamination = external.detect_output_contamination(output)
    assert contamination.verdicts == ("반려", "통과")
    assert contamination.verdict_conflict is True


def test_repeated_same_verdict_is_not_flagged(external):
    """같은 판정의 재진술은 위험이 없다 — false-red 를 만들지 않는다."""
    output = "판정: 통과\n\n**must-fix**:\n- 없음\n\n## 요약\n판정: 통과\n"
    contamination = external.detect_output_contamination(output)
    assert len(contamination.verdicts) == 2
    assert contamination.verdict_conflict is False
    assert contamination.markers == ()


def test_detector_sees_every_verdict_line_the_parser_can_pick(external):
    """검출기가 파서보다 좁으면 파서가 집어 든 echo 라인을 못 본다 — 같은 함수를 쓰는지 단언한다.

    파서는 선언 목록의 **첫 줄**을 판정으로 쓰고 검출기는 **전량**을 본다(= 검출기 ⊇ 파서).
    """
    output = "판정: 반려\n\n**must-fix**:\n- 옛 지적\n\n판정: 통과\n"
    words = external.verdict_words(output)
    assert words == ("반려", "통과")
    assert external.parse_verdict(output)["has_must_fix"] is True  # 파서는 첫 줄(반려)을 집는다.
    contamination = external.detect_output_contamination(output)
    assert contamination.verdicts == words
    assert contamination.verdict_conflict is True


def test_prose_verdict_inside_a_sentence_is_not_a_declaration(external):
    """행 선두 앵커의 **판별** 축 — 산문 안 인용 판정은 선언이 아니고 오탐도 만들지 않는다.

    앵커를 풀면(문서 어디의 `판정:` 이든 세면) 같은 입력이 '판정 라인 다중/불일치'가 된다.
    """
    output = (
        "참고: 이전 라운드에서는 판정: 반려 였다.\n\n"
        "판정: 통과\n\n**must-fix** (반드시 수정):\n- 없음\n"
    )
    assert external.verdict_words(output) == ("통과",)
    assert external.detect_output_contamination(output).verdict_conflict is False
    assert external.parse_verdict(output) == {"has_must_fix": False, "has_pass": True}


@pytest.mark.parametrize("prose", (
    # 실측 형상: 판정선이 아예 없고 **본문에만** '통과' 가 있다 — 본문 전역 스캔이면 통과가 된다.
    "회귀 통과 확인. 문제 없음.\n\n**must-fix** (반드시 수정):\n- 없음\n",
    "이번 리뷰의 판정: 통과 이다.\n\n**must-fix** (반드시 수정):\n- 없음\n",
    "이번 리뷰의 판정: 반려 이다.\n",
))
def test_format_noncompliant_prose_verdict_is_never_a_pass(external, tmp_path, prose):
    """형식 미준수 출력은 통과로 접지 않는다 — 앵커 판정선 0개면 '판정 불명확'(exit 1)."""
    assert external.verdict_words(prose) == ()
    assert external.parse_verdict(prose)["has_pass"] is False
    result = external.run_review(
        "p", target=_direct_target(external), output_dir=tmp_path,
        run_fn=lambda *a, **k: subprocess.CompletedProcess(["codex"], 0, prose, ""),
    )
    assert result["all_pass"] is False
    assert external.determine_exit_code(result) == 1


def test_prose_fallback_stays_off_when_a_declaration_exists(external):
    """앵커 선언이 하나라도 있으면 폴백은 비활성 — 정상 형식의 통과가 오탐으로 막히지 않는다."""
    output = (
        "덧붙임: 이전 라운드의 판정: 반려 는 해소됐다.\n\n"
        "판정: 통과\n\n**must-fix** (반드시 수정):\n- 없음\n"
    )
    assert external.parse_verdict(output) == {"has_must_fix": False, "has_pass": True}


@pytest.mark.parametrize("word", ("[통과", "비통과", "PASS/REJECT", "통과|반려", "미정"))
def test_only_exact_verdict_tokens_count_as_a_verdict(external, word):
    """부분 문자열이면 템플릿 echo·부정형·선택지 나열이 전부 '통과'로 읽힌다(false-green 실측)."""
    assert external.verdict_kind(word) == external.VERDICT_UNKNOWN


@pytest.mark.parametrize("word,kind", (
    ("통과", "pass"), ("**통과**", "pass"), ("통과.", "pass"), ("PASS", "pass"),
    ("반려", "reject"), ("*반려*", "reject"), ("REJECT", "reject"),
))
def test_exact_tokens_survive_emphasis_and_sentence_punctuation(external, word, kind):
    """강조/문장부호만 벗긴다 — 정상 형식이 정확일치 때문에 불명확이 되면 안 된다."""
    assert external.verdict_kind(word) == kind


def test_prompt_template_echo_is_not_a_pass(external, tmp_path):
    """프롬프트 출력 형식이 회신에 그대로 실려도 통과가 아니다 — `판정: [통과 | 반려]`."""
    echoed = (
        "판정: [통과 | 반려]\n\n**must-fix** (반드시 수정):\n- 없음\n\n"
        "**suggestion** (권장):\n- 없음\n"
    )
    assert external.verdict_words(echoed) == ("[통과",)
    assert external.parse_verdict(echoed)["has_pass"] is False
    result = external.run_review(
        "p", target=_direct_target(external), output_dir=tmp_path,
        run_fn=lambda *a, **k: subprocess.CompletedProcess(["codex"], 0, echoed, ""),
    )
    assert result["all_pass"] is False
    assert external.determine_exit_code(result) == 1


def test_negated_verdict_is_not_a_pass(external, tmp_path):
    """`판정: 비통과` 는 부분 문자열로는 통과였다 — 정확일치에서는 불명확이다."""
    output = "판정: 비통과\n\n**must-fix** (반드시 수정):\n- 없음\n"
    assert external.parse_verdict(output)["has_pass"] is False
    result = external.run_review(
        "p", target=_direct_target(external), output_dir=tmp_path,
        run_fn=lambda *a, **k: subprocess.CompletedProcess(["codex"], 0, output, ""),
    )
    assert result["all_pass"] is False


def test_unknown_verdict_line_beside_a_real_one_is_ambiguous(external):
    """허용 토큰이 아닌 판정선이 진짜 판정선과 섞이면 어느 게 이번 판정인지 모른다."""
    output = "판정: [통과 | 반려]\n\n판정: 통과\n\n**must-fix** (반드시 수정):\n- 없음\n"
    assert external.detect_output_contamination(output).verdict_conflict is True


def test_raw_artifact_regex_accepts_underscored_reviewer_names(external):
    """reviewer 이름에 `_` 가 있는 배포(`additional_reviewer_my_reviewer_…`)의 인용도 잡는다."""
    output = (
        "판정: 통과\n\n**must-fix** (반드시 수정):\n- 없음\n\n**suggestion** (권장):\n"
        "- additional_reviewer_my_reviewer_20260806_040406_11_ab.txt 의 지적과 같다.\n"
    )
    contamination = external.detect_output_contamination(output)
    assert contamination.raw_artifacts == (
        "additional_reviewer_my_reviewer_20260806_040406_11_ab.txt",
    )


def test_transcript_regex_detects_windows_paths(external):
    """Windows 형상 전사 경로를 못 보면 그 플랫폼에서는 백스톱이 없는 것과 같다."""
    output = (
        "판정: 통과\n\n**must-fix** (반드시 수정):\n- 없음\n\n**suggestion** (권장):\n"
        "- C:\\Users\\u\\.claude\\projects\\-repo\\9f2.jsonl 에서 확인했다.\n"
    )
    contamination = external.detect_output_contamination(output)
    assert contamination.transcripts
    assert any("9f2.jsonl" in hit for hit in contamination.transcripts)
    assert any("세션 전사" in marker for marker in contamination.markers)


def test_clean_output_has_no_markers(external):
    contamination = external.detect_output_contamination(
        "판정: 통과\n\n**must-fix**:\n- 없음\n\n**suggestion**:\n- 없음\n")
    assert contamination.markers == ()


# 실측 오염 형상 그대로의 **판별 픽스처**: 리뷰어 자신의 블록(통과·must-fix 없음)이 먼저 오고 옛
# 라운드 블록(반려)이 뒤에 echo 된다. 파서는 앞 블록만 보고 통과를 내므로, 이 입력에서 all_pass 를
# 막는 것은 오직 오염 강등뿐이다(앞뒤가 바뀐 입력은 파서 단독으로 이미 비-통과라 판별력이 0이다).
_ECHOED_OLD_BLOCK_OUTPUT = (
    "판정: 통과\n\n"
    "**must-fix** (반드시 수정):\n- 없음\n\n"
    "**suggestion** (권장):\n- 없음\n\n"
    "--- 참고: 이전 라운드 원문 ---\n"
    "판정: 반려\n\n"
    "**must-fix** (반드시 수정):\n- 옛 지적\n"
)


def test_parser_alone_reads_the_echoed_pass_as_a_clean_pass(external):
    """판별력 확인 — 이 입력은 파서 단독으로는 '통과'다(강등 절이 유일한 방어선)."""
    verdict = external.parse_verdict(_ECHOED_OLD_BLOCK_OUTPUT)
    assert verdict == {"has_must_fix": False, "has_pass": True}



def _direct_target(external, command: str = "codex"):
    """직접 호출용 대상 — 이 축의 테스트는 transport 아래(회신/로그 채널·판정·오염)를 본다.

    `run_review` 는 대상을 **필수**로 받고 커맨드 문자열 입구가 없다. 여기서는 하네스 argv 조립을
    태우지 않는 대상 하나로 고정해 판정 파이프라인만 본다 — 엔진 CLI 가 넘기는 대상은 언제나
    `resolve_reviewer_target` 이 harness·model 을 채운 구조화 tuple 이다."""
    return external.ReviewerTarget(external.REVIEWER_SOURCE_STRUCTURED, command)

def _run_kwargs(root: Path) -> dict:
    """실행 조건 DI 헬퍼 — 정상 경로가 넘기는 cwd·env 와 같은 모양이다."""
    return {"cwd": root, "env": {"PWD": str(root)}}


def test_contaminated_reject_is_not_recorded_as_a_reject(external, tmp_path):
    """옛 반려 블록 echo 를 '이번 리뷰의 반려'로 기록하면 리뷰어가 안 한 지적으로 일이 돈다."""
    output = (
        "판정: 반려\n\n**must-fix** (반드시 수정):\n- 옛 지적\n\n"
        "**suggestion** (권장):\n- 없음\n\n--- 이전 라운드 원문 ---\n판정: 통과\n"
    )
    result = external.run_review(
        "p", target=_direct_target(external), output_dir=tmp_path,
        run_fn=lambda *a, **k: subprocess.CompletedProcess(["codex"], 0, output, ""),
        **_run_kwargs(tmp_path),
    )
    assert result["contamination"]
    assert result["any_must_fix"] is False and result["all_pass"] is False
    assert external._round_has_verdict(result) is False   # 장부에서도 판정이 아니다.
    assert external.determine_exit_code(result) == 1      # 여전히 비-통과(보수적).


def test_clean_reject_is_still_recorded_as_a_verdict(external, tmp_path):
    """오염 없는 반려는 종전대로 반려다 — 무효화가 정상 판정까지 삼키면 안 된다."""
    output = "판정: 반려\n\n**must-fix** (반드시 수정):\n- 실제 지적\n"
    result = external.run_review(
        "p", target=_direct_target(external), output_dir=tmp_path,
        run_fn=lambda *a, **k: subprocess.CompletedProcess(["codex"], 0, output, ""),
        **_run_kwargs(tmp_path),
    )
    assert result["contamination"] == ()
    assert result["any_must_fix"] is True
    assert external._round_has_verdict(result) is True


def test_run_review_downgrades_conflicting_verdict_to_unclear(external, tmp_path):
    """오염된 출력에서 '통과'가 그대로 나가면 false-green — 보수적으로 판정 불명확이어야 한다."""
    result = external.run_review(
        "p", target=_direct_target(external), output_dir=tmp_path,
        run_fn=lambda *a, **k: subprocess.CompletedProcess(
            ["codex"], 0, _ECHOED_OLD_BLOCK_OUTPUT, ""),
    )
    assert result["all_pass"] is False
    assert result["contamination"] and any(
        "판정 라인" in marker for marker in result["contamination"])
    assert external.determine_exit_code(result) == 1


def test_parser_and_detector_read_the_same_text(external, tmp_path):
    """파서만 진행 로그를 보면 로그의 옛 판정 블록이 판정에 반영되고 검출은 못 잡는다."""
    answer = "판정: 통과\n\n**must-fix** (반드시 수정):\n- 없음\n"
    log = "이전 라운드 원문:\n판정: 반려\n\n**must-fix** (반드시 수정):\n- 옛 지적\n"
    result = external.run_review(
        "p", target=_direct_target(external), output_dir=tmp_path,
        run_fn=lambda *a, **k: subprocess.CompletedProcess(["codex"], 0, answer, log),
    )
    # 회신 구간만 본 판정 = 통과 · 오염 신호 없음 → 진행 로그가 판정에도 검출에도 안 샌다.
    assert result["verdict"] == {"has_must_fix": False, "has_pass": True}
    assert result["contamination"] == () and result["all_pass"] is True


@pytest.mark.parametrize("wrapped", (
    "```\n판정: 반려\n```\n",
    "> 판정: 반려\n",
))
def test_quoted_and_fenced_verdicts_count_for_neither_parser_nor_detector(external, wrapped):
    """인용/코드펜스 안의 판정 문구는 리뷰어 선언이 아니다 — 두 표면이 같이 무시해야 한다.

    검토 대상 diff 에 든 판정 문안(이 저장소의 테스트 픽스처가 그렇다)이 이 축의 상시 오탐 원천이라,
    좁히는 규칙을 한쪽에만 걸면 오탐이 남거나 "검출기 ⊇ 파서" 성질이 깨진다.
    """
    output = wrapped + "\n판정: 통과\n\n**must-fix** (반드시 수정):\n- 없음\n"
    assert external.verdict_words(output) == ("통과",)
    assert external.parse_verdict(output) == {"has_must_fix": False, "has_pass": True}
    assert external.detect_output_contamination(output).verdict_conflict is False


@pytest.mark.parametrize("citation", (
    "- additional_reviewer_codex_20260806_040406_11_ab.txt 의 지적과 같다.\n",
    "- /home/user/.claude/projects/-home-user-repo/9f2.jsonl 에서 확인했다.\n",
))
def test_run_review_downgrades_any_contaminated_pass(external, tmp_path, citation):
    """판정이 하나여도 옛 raw·전사를 인용했으면 그 통과는 리뷰어 자신의 판정이라는 보장이 없다."""
    output = ("판정: 통과\n\n**must-fix**:\n- 없음\n\n**suggestion**:\n" + citation)
    result = external.run_review(
        "p", target=_direct_target(external), output_dir=tmp_path,
        run_fn=lambda *a, **k: subprocess.CompletedProcess(["codex"], 0, output, ""),
    )
    assert result["contamination"]
    assert result["all_pass"] is False
    assert external.determine_exit_code(result) == 1


def test_reviewer_output_keeps_channels_structurally_separate(external):
    """회신/로그 경계는 **필드**다 — 표시 문자열을 되파싱하지 않는다.

    구분자 파싱이던 시절의 반례를 그대로 넣는다: 회신 안에 표시 구분자와 옛 판정 블록이 섞여
    들어와도 이번 판정(반려)이 '깨끗한 통과'로 잘리면 안 된다.
    """
    poisoned_answer = (
        "인용: 옛 라운드 원문\n판정: 통과\n" + external._STDERR_SECTION_MARKER
        + "판정: 반려\n\n**must-fix** (반드시 수정):\n- 이번 지적\n"
    )
    output = external.ReviewerOutput(poisoned_answer, "workdir: /tmp/ws\n")
    assert output.answer == poisoned_answer          # 잘리지 않는다.
    assert output.log == "workdir: /tmp/ws\n"
    assert output.combined.endswith("workdir: /tmp/ws\n")
    contamination = external.detect_output_contamination(output.answer)
    assert contamination.verdict_conflict is True    # 두 판정선이 모두 보인다.


def test_reviewer_output_normalizes_plain_string_runners(external):
    """문자열만 돌려주는 주입 러너/스텁은 회신 채널로 정규화한다(로그 없음)."""
    normalized = external._as_reviewer_output("판정: 통과\n")
    assert normalized == external.ReviewerOutput("판정: 통과\n", "")
    assert external._as_reviewer_output(normalized) is normalized


def test_progress_log_prompt_echo_is_not_contamination(external, tmp_path):
    """라이브 실측 형상 회귀: codex 진행 로그는 프롬프트 템플릿과 diff 원문을 그대로 싣는다.

    거기까지 오염으로 세면 (a) 모든 실행이 '판정 라인 다중'이 되고 (b) 검토 대상 코드에 든
    판정 문안/raw 파일명이 오염으로 둔갑해 정상 통과가 false-red 가 된다.
    """
    # 라이브 회신 형상 그대로 — 프롬프트가 강제하는 두 섹션이 다 있다.
    answer = ("판정: 통과\n\n**must-fix** (반드시 수정):\n- 없음\n\n"
              "**suggestion** (권장):\n- 없음\n")
    progress_log = (
        "Reading prompt from stdin...\nworkdir: /tmp/pm_review_workspace_x\n"
        "판정: [통과 | 반려]\n"                     # 프롬프트 출력 형식 템플릿 echo
        "+    output = \"판정: 반려\\n\"\n"          # 검토 대상 diff 안의 판정 문안
        "+    raw = \"additional_reviewer_codex_20260806_040406_11_ab.txt\"\n"
    )
    result = external.run_review(
        "p", target=_direct_target(external), output_dir=tmp_path,
        run_fn=lambda *a, **k: subprocess.CompletedProcess(
            ["codex"], 0, answer, progress_log),
    )
    assert result["contamination"] == ()
    assert result["all_pass"] is True
    assert result["log"] == progress_log        # 로그는 버리지 않고 따로 보관한다.
    assert progress_log in result["output"]     # raw 박제 표시에는 그대로 남는다.


def test_print_summary_surfaces_contamination(external, capsys):
    external.print_summary({
        "reviewer": "codex", "ok": True, "output": "판정: 통과",
        "verdict": {"has_must_fix": False, "has_pass": True},
        "contamination": ("옛 리뷰/위임 raw 파일명 인용: additional_reviewer_codex_1.txt",),
        "file": None, "failed": False, "started": True,
        "any_must_fix": False, "all_pass": True,
    })
    out = capsys.readouterr().out
    assert "회신 품질 의심" in out and "additional_reviewer_codex_1.txt" in out


def test_print_summary_unchanged_without_contamination(external, capsys):
    """오염 0건이면 출력은 종전과 동일하다."""
    external.print_summary({
        "reviewer": "codex", "ok": True, "output": "판정: 통과",
        "verdict": {"has_must_fix": False, "has_pass": True},
        "file": None, "failed": False, "started": True,
        "any_must_fix": False, "all_pass": True,
    })
    assert "오염 의심" not in capsys.readouterr().out
