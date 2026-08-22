"""T-0790 — 공유 트리 wave 의 티켓별 스코프 측정 (귀속 창 + 디렉터리 양보).

같은 worktree 에서 여러 티켓이 동시에 돌면 완료 기록 서킷브레이커가 티켓별 구현 스코프를
재지 못했다. 원인은 둘이다.
  ① 창: 타 티켓 스냅샷이 `claimed/` 뿐이라 같은 wave 에서 **먼저 완료된** 티켓이 안 보이고,
     그 전용 diff 가 뒤 티켓 몫으로 전부 흡수된다.
  ② 디렉터리: `tests` 같은 디렉터리 touches 가 그 아래 전 wave 변경을 자기 스코프로 주장한다.
이 파일은 두 원인을 실 git 저장소 + 실 board 엔진 사본 + 실 티켓 파일로 재현하고, 보정 뒤
숫자가 실 스코프에 근사하는지, 그리고 보정이 **가드를 약화하지 않는지**를 함께 고정한다.
board/diff 대역은 쓰지 않는다 (실 보드 상태도 읽거나 바꾸지 않는다).
"""
from __future__ import annotations

import argparse
import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

requires_git = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git 바이너리 부재 — 실 git 통합 케이스 skip.",
)

# 창 판정 입력 — board writer 형식(UTC ISO·인용 스칼라)과 같은 표기다.
_CLAIMED_AT = "2026-08-21T16:29:57+00:00"
_COMPLETED_IN_WINDOW = "2026-08-21T16:31:40+00:00"    # claim 이후 완료 — 창 안
_COMPLETED_BEFORE_WINDOW = "2026-08-21T15:00:00+00:00"  # claim 이전 완료 — 창 밖

_MEDIUM_CAP = 1000  # external_review.DEFAULT_DIFF_CAPS["medium"] — 재현 형상의 상한


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True,
        text=True, encoding="utf-8",
    )


def _ticket(touches: list[str], *, status: str = "claimed", estimate: str = "small",
            claimed_at: str | None = None, completed_at: str | None = None,
            claimed_rev: str | None = None, broken: bool = False) -> dict:
    """티켓 한 건의 형상 선언 — 창 축(status·시각)과 표기 축(touches)을 함께 준다."""
    return {
        "touches": touches, "status": status, "estimate": estimate,
        "claimed_at": claimed_at, "completed_at": completed_at,
        "claimed_rev": claimed_rev, "broken": broken,
    }


def _ticket_text(ticket_id: str, spec: dict) -> str:
    fields = [f"id: {ticket_id}", f"title: {ticket_id} shape",
              f"status: {spec['status']}"]
    for key in ("claimed_at", "completed_at", "claimed_rev"):
        if spec.get(key):
            # board writer 와 같은 인용 스칼라 — 인용이 없으면 YAML 이 datetime 으로 접는다.
            fields.append(f"{key}: '{spec[key]}'")
    if spec["broken"]:
        # 실측 손상 형상(인용 없는 콜론) — 전문 파싱이 닿으면 여기서 터진다.
        fields.append("design: waived: 인용 없는 콜론")
    fields.append("touches:")
    fields += [f"- {touch}" for touch in spec["touches"]]
    fields.append(f"estimate: {spec['estimate']}")
    return "---\n" + "\n".join(fields) + "\n---\n\n# hermetic wave shape\n"


def _wave_shape(tmp_path: Path, tickets: dict[str, dict], files: dict[str, int],
                *, commit_changes: bool = False):
    """실 board 엔진 + 실 창 티켓(claimed/done) + 실 git diff 를 가진 tmp repo.

    `commit_changes` 면 변경을 커밋하고 스코프 밖 전파 커밋을 하나 더 얹는다 — finish 시점
    트리가 clean 이고 마지막 커밋이 티켓 경로를 안 건드리는 wave 형상이라, 앵커 폭(claim 시점
    rev)이라야 그 누적이 보인다. 반환 rev 는 claim 앵커로 쓸 baseline 이다.
    """
    root = tmp_path / "repo"
    tools = root / ".project_manager" / "tools"
    tools.parent.mkdir(parents=True)
    shutil.copytree(TOOLS, tools)
    for ticket_id, spec in tickets.items():
        status_dir = root / ".project_manager" / "wiki" / "tickets" / spec["status"]
        status_dir.mkdir(parents=True, exist_ok=True)
        (status_dir / f"{ticket_id}-shape.md").write_text(
            _ticket_text(ticket_id, spec), encoding="utf-8")

    _git(root, "init", "-q")
    _git(root, "config", "user.email", "wave-attribution-test@example.invalid")
    _git(root, "config", "user.name", "wave attribution test")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for relative in files:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
    # 엔진 사본도 baseline 에 넣어 하네스 복사본이 미커밋 작업물로 안 보이게 한다.
    _git(root, "add", "seed.txt", ".project_manager", *files)
    _git(root, "commit", "-q", "-m", "seed")
    anchor = _git(root, "rev-parse", "HEAD").stdout.strip()
    for relative, lines in files.items():
        (root / relative).write_text("x\n" * lines, encoding="utf-8")
    if commit_changes:
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "wave 구현")
        (root / "docs" / "propagation.md").parent.mkdir(parents=True, exist_ok=True)
        (root / "docs" / "propagation.md").write_text("prop\n", encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "전파")

    spec = importlib.util.spec_from_file_location(
        f"ticket_finish_t0790_{tmp_path.name}", tools / "ticket_finish.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return root, module, anchor


def _finisher(root: Path, tf):
    return tf.TicketFinisher(
        board_py=root / ".project_manager" / "tools" / "board.py",
        regression_cwd=root,
        log_file=root / "log.md",
    )


def _measure(root: Path, tf, ticket_id: str, *, claimed_rev: str | None = None,
             run_fn=None):
    """한 티켓의 측정 — (finisher, external, touches, attribution)."""
    finisher = _finisher(root, tf)
    external = tf._load_external_review()
    assert external is not None
    touches = finisher._measured_touches(ticket_id)
    assert touches is not None
    kwargs = {"claimed_rev": claimed_rev} if claimed_rev else {}
    if run_fn is not None:
        kwargs["run_fn"] = run_fn
    return finisher, external, touches, finisher._ticket_diff_attribution(
        ticket_id, external, touches, **kwargs)


def _wave_rename_shape(tmp_path: Path, tickets: dict[str, dict], *, edited: bool,
                       source_lines: int = 400):
    """`src/old.py` → `dst/new.py` staged rename 을 가진 wave 형상.

    rename 은 측정 폭이 pathspec 에 따라 달라지는 유일한 형상이다 — 한 endpoint 만 주면 삭제
    /추가 전체를, 둘 다 주면 delta 만 센다. 창 합집합 측정은 항상 두 endpoint 를 넣으므로,
    단독 claim 폭 복원이 빠지면 `source_lines` 줄이 0 으로 접힌다.
    """
    root, tf, _anchor = _wave_shape(tmp_path, tickets, {"src/old.py": source_lines})
    _git(root, "add", "src/old.py")
    _git(root, "commit", "-q", "-m", f"{source_lines}-line source")
    (root / "dst").mkdir()
    _git(root, "mv", "src/old.py", "dst/new.py")
    if edited:
        with (root / "dst/new.py").open("a", encoding="utf-8") as stream:
            stream.write("edited\n" * 10)
    _git(root, "add", "dst/new.py")
    return root, tf


# ══ 형상 E — 디렉터리 선언은 정확-파일 claim 에 양보한다 ══════════════════════


@requires_git
@pytest.mark.parametrize(
    "directory_touches",
    [
        pytest.param(["tests"], id="bare"),
        pytest.param(["tests/"], id="trailing-slash"),
        pytest.param(["./tests"], id="dot-prefix"),
        pytest.param(["tests", "tests/"], id="duplicate-notation"),
    ],
)
def test_shape_e_directory_claim_yields_to_the_exact_file_claim(
    tmp_path, directory_touches,
):
    """디렉터리로만 claim 한 파일을 창 안 타 티켓이 정확 claim 하면 내 몫에서 뺀다.

    뺀 양은 상대 티켓 몫(`excluded_total`)이 아니라 귀속 불명(`unattributed_total`)이다 —
    같은 파일의 hunks 를 티켓별로 가를 증거가 없기 때문이다. 표기 변형(후행 슬래시·`./`
    접두·중복 선언)은 같은 판정으로 접힌다.
    """
    root, tf, _anchor = _wave_shape(
        tmp_path,
        {"T-1101": _ticket(directory_touches, claimed_at=_CLAIMED_AT),
         "T-1102": _ticket(["tests/test_b.py"], claimed_at=_CLAIMED_AT)},
        {"tests/test_a.py": 11, "tests/test_b.py": 17, "tests/test_c.py": 13},
    )

    _f, _e, _t, directory_ticket = _measure(root, tf, "T-1101")
    _f2, _e2, _t2, exact_ticket = _measure(root, tf, "T-1102")

    assert directory_ticket == tf.DiffAttribution(11 + 13, 0, (), 17)
    assert exact_ticket == tf.DiffAttribution(17, 11 + 13, ("T-1101",), 0)


@requires_git
def test_exact_claim_beside_the_directory_claim_blocks_the_yield(tmp_path):
    """같은 파일을 디렉터리와 **정확 파일**로 함께 선언했으면 양보하지 않는다.

    양보의 조건은 "내 주장이 디렉터리 선언뿐"이다 — 정확 지목은 그 파일이 내 스코프라는 선언
    이므로 겹쳐도 종전대로 유지(과다 측정)한다.
    """
    root, tf, _anchor = _wave_shape(
        tmp_path,
        {"T-1111": _ticket(["tests", "tests/test_b.py"], claimed_at=_CLAIMED_AT),
         "T-1112": _ticket(["tests/test_b.py"], claimed_at=_CLAIMED_AT)},
        {"tests/test_a.py": 11, "tests/test_b.py": 17, "tests/test_c.py": 13},
    )

    _f, _e, _t, measured = _measure(root, tf, "T-1111")

    assert measured == tf.DiffAttribution(41, 0, (), 0)


@requires_git
def test_shape_e_prime_both_directory_claims_report_the_whole_yield(tmp_path, capsys):
    """양쪽이 같은 디렉터리만 claim 하면 양쪽에서 빠지되 **전량이 보고된다**.

    조용히 줄어드는 것이 이 축의 금지 사항이다 — 통과할 때도 뺀 양이 stderr 에 남는다.
    """
    root, tf, _anchor = _wave_shape(
        tmp_path,
        {"T-1201": _ticket(["tests"], claimed_at=_CLAIMED_AT),
         "T-1202": _ticket(["tests"], claimed_at=_CLAIMED_AT)},
        {"tests/test_a.py": 11, "tests/test_b.py": 17},
    )

    finisher, _e, _t, measured = _measure(root, tf, "T-1201")
    block = finisher._default_diff_cap_block("T-1201")
    err = capsys.readouterr().err

    assert measured == tf.DiffAttribution(0, 0, (), 28)
    assert block is None                       # 0줄 — 상한 판정은 통과다
    assert "디렉터리 양보" in err and "28줄" in err   # 그러나 조용하지 않다


# ══ 형상 F — 귀속 창(claimed ∪ 창 안 done) ═════════════════════════════════


@requires_git
def test_window_includes_done_tickets_completed_after_my_claim(tmp_path):
    """내 claim 이후 완료된 티켓은 아직 같은 트리에 diff 를 남기고 있다 — 창 안이다."""
    root, tf, _anchor = _wave_shape(
        tmp_path,
        {"T-1301": _ticket(["tests"], claimed_at=_CLAIMED_AT),
         "T-1302": _ticket(["tests/test_b.py"], status="done",
                           claimed_at=_CLAIMED_AT,
                           completed_at=_COMPLETED_IN_WINDOW)},
        {"tests/test_a.py": 11, "tests/test_b.py": 17, "tests/test_c.py": 13},
    )

    _f, _e, _t, measured = _measure(root, tf, "T-1301")

    assert measured == tf.DiffAttribution(11 + 13, 0, (), 17)


@requires_git
def test_window_excludes_done_tickets_completed_before_my_claim(tmp_path):
    """내 claim 이전에 끝난 티켓은 이 wave 의 참여자가 아니다 — 종전대로 과다 측정."""
    root, tf, _anchor = _wave_shape(
        tmp_path,
        {"T-1311": _ticket(["tests"], claimed_at=_CLAIMED_AT),
         "T-1312": _ticket(["tests/test_b.py"], status="done",
                           completed_at=_COMPLETED_BEFORE_WINDOW)},
        {"tests/test_a.py": 11, "tests/test_b.py": 17, "tests/test_c.py": 13},
    )

    _f, _e, _t, measured = _measure(root, tf, "T-1311")

    assert measured == tf.DiffAttribution(41, 0, (), 0)


@requires_git
def test_missing_claimed_at_folds_to_the_claimed_only_window(tmp_path):
    """`claimed_at` 이 없으면 창을 정할 수 없다 — `claimed/` 만 보는 종전 폭(과다 측정)."""
    root, tf, _anchor = _wave_shape(
        tmp_path,
        {"T-1321": _ticket(["tests"]),
         "T-1322": _ticket(["tests/test_b.py"], status="done",
                           completed_at=_COMPLETED_IN_WINDOW)},
        {"tests/test_a.py": 11, "tests/test_b.py": 17, "tests/test_c.py": 13},
    )

    _f, _e, _t, measured = _measure(root, tf, "T-1321")

    assert measured == tf.DiffAttribution(41, 0, (), 0)


@requires_git
def test_out_of_window_broken_ticket_is_never_fully_parsed(tmp_path, capsys):
    """창 밖 done 티켓은 frontmatter 머리만 읽는다 — 손상 YAML 이 보정을 깨지 않는다."""
    root, tf, _anchor = _wave_shape(
        tmp_path,
        {"T-1331": _ticket(["tests"], claimed_at=_CLAIMED_AT),
         "T-1332": _ticket(["tests/test_b.py"], status="done", broken=True,
                           completed_at=_COMPLETED_BEFORE_WINDOW)},
        {"tests/test_a.py": 11, "tests/test_b.py": 17},
    )

    _f, _e, _t, measured = _measure(root, tf, "T-1331")

    assert measured == tf.DiffAttribution(28, 0, (), 0)
    assert "보드 읽기 실패" not in capsys.readouterr().err


@requires_git
def test_in_window_broken_ticket_is_loud_and_keeps_the_breaker(tmp_path, capsys):
    """창 **안** 티켓이 손상되면 조용히 넘기지 않는다 — 경고 뒤 과다 측정으로 판정 유지."""
    root, tf, _anchor = _wave_shape(
        tmp_path,
        {"T-1341": _ticket(["tests"], claimed_at=_CLAIMED_AT),
         "T-1342": _ticket(["tests/test_b.py"], status="done", broken=True,
                           completed_at=_COMPLETED_IN_WINDOW)},
        {"tests/test_a.py": 11, "tests/test_b.py": 17},
    )

    _f, _e, _t, measured = _measure(root, tf, "T-1341")

    assert measured == tf.DiffAttribution(28, 0, (), 0)
    assert "귀속 보정 skip — claimed 보드 읽기 실패" in capsys.readouterr().err


# ══ 형상 G — 공유 정확-파일 claim 은 union 을 유지한다 ═══════════════════════


@requires_git
def test_shape_g_shared_exact_file_claim_keeps_the_union(tmp_path):
    """두 티켓이 같은 파일을 정확 claim 하면 양쪽 다 전량 유지한다(hunk 증거 없음)."""
    root, tf, _anchor = _wave_shape(
        tmp_path,
        {"T-1401": _ticket(["src/shared.py"], claimed_at=_CLAIMED_AT),
         "T-1402": _ticket(["src/shared.py"], status="done",
                           completed_at=_COMPLETED_IN_WINDOW)},
        {"src/shared.py": 13},
    )

    _f, _e, _t, measured = _measure(root, tf, "T-1401")

    assert measured == tf.DiffAttribution(13, 0, (), 0)


# ══ 형상 H — 양보의 상대 owner 는 증명 가능한 선언만 센다 ═══════════════════
#
# 유지 판정(`_touch_claims_path`)은 해소 불능한 magic/glob 을 일치로 접는다 — 그쪽 오차는 과다
# 측정이다. 같은 술어를 **양보**의 상대 owner 산출에 재사용하면 오차 방향이 뒤집혀, 무관한 타
# 티켓 선언 하나로 내 몫이 통째로 사라진다. 그래서 양보 전용 술어(`_touch_owns_path`)는 반대로
# 접는다.

_GLOB_RIVAL_CELLS = [
    pytest.param(["docs/*.md"], id="unrelated-glob"),
    pytest.param(["tests/*.py"], id="matching-glob"),
    pytest.param([":(glob)tests/**/*.py"], id="pathspec-magic"),
]


@requires_git
@pytest.mark.parametrize("rival_touches", _GLOB_RIVAL_CELLS)
def test_rival_glob_declaration_never_triggers_the_directory_yield(
    tmp_path, rival_touches,
):
    """창 안 타 티켓의 glob/pathspec 선언은 양보 근거가 아니다 — 전량 내 몫으로 남고 차단된다.

    무관한 glob(`docs/*.md`)이 양보를 유발하면 내 400줄이 지워지고 상한 300줄을 통과한다
    (해소 불능 술어를 owner 로 쓴 형상의 실측값: `total=0 · unattributed=400 · block=None`).
    실제로 그 파일에 걸리는 glob 과 pathspec magic 도 같은 방향으로 접는다 — 이 귀속기의 자체
    glob 해석을 owner 근거로 믿으면 Git 과의 해석 차이가 곧바로 가드 약화가 되기 때문이다.
    """
    root, tf, _anchor = _wave_shape(
        tmp_path,
        {"T-1901": _ticket(["tests"], claimed_at=_CLAIMED_AT),
         "T-1902": _ticket(rival_touches, claimed_at=_CLAIMED_AT)},
        {"tests/test_big.py": 400},
    )

    finisher, external, touches, measured = _measure(root, tf, "T-1901")
    block = finisher._default_diff_cap_block("T-1901")

    assert external.diff_line_total(root, "HEAD", touches) == 400
    assert measured == tf.DiffAttribution(400, 0, (), 0)
    assert block is not None and "diff 400줄 > 상한 300줄" in block


@requires_git
def test_plain_path_rival_still_yields_in_the_same_shape(tmp_path, capsys):
    """대조군 — 같은 형상에서 **평문 경로** rival 은 종전대로 양보를 발동시킨다.

    조임이 정상 사용(실재하는 정확-claim 양보)까지 막지 않는다는 역방향 단언이다.
    """
    root, tf, _anchor = _wave_shape(
        tmp_path,
        {"T-1911": _ticket(["tests"], claimed_at=_CLAIMED_AT),
         "T-1912": _ticket(["tests/test_big.py"], claimed_at=_CLAIMED_AT)},
        {"tests/test_big.py": 400},
    )

    finisher, _e, _t, measured = _measure(root, tf, "T-1911")
    block = finisher._default_diff_cap_block("T-1911")
    err = capsys.readouterr().err

    assert measured == tf.DiffAttribution(0, 0, (), 400)
    assert block is None
    assert "디렉터리 양보 — T-1911" in err and "400줄" in err


# ══ 형상 I — 양보도 rename endpoint 폭을 복원해 전량 보고한다 ═══════════════
#
# 양보가 폭 복원보다 먼저 실행되면 rename 전량이 `total` 에도 `unattributed_total` 에도 안 남고
# 사라진다. 유실은 과다 측정보다 나쁘다 — 사용자가 사라진 줄을 알 방법이 없다.

_RENAME_DIRECTORY_CELLS = [
    pytest.param(["src"], ["dst/new.py"], "source", id="source-directory"),
    pytest.param(["dst"], ["src/old.py"], "destination", id="destination-directory"),
]


@requires_git
@pytest.mark.parametrize("edited", [False, True],
                         ids=["exact-rename", "edited-rename"])
@pytest.mark.parametrize(("directory_touches", "rival_touches", "endpoint"),
                         _RENAME_DIRECTORY_CELLS)
def test_directory_only_rename_yield_reports_the_restored_width(
    tmp_path, edited, directory_touches, rival_touches, endpoint, capsys,
):
    """디렉터리로만 claim 한 rename endpoint 를 양보할 때도 **단독 claim 폭**을 보고한다.

    합집합 numstat 은 두 endpoint 를 다 넣어 `0/0`(순수 rename)이나 편집분만 센다. 복원이
    없으면 그 값이 그대로 양보량이 되어 400줄이 `total=0 · unattributed=0` 으로 사라진다
    (복원 전 실측). 복원 뒤에는 내 touches 단독 측정과 정확히 같은 폭이 보고되며, 그 폭을
    양쪽에 이중 계상하지도 않는다.
    """
    root, tf = _wave_rename_shape(
        tmp_path,
        {"T-1921": _ticket(directory_touches, claimed_at=_CLAIMED_AT),
         "T-1922": _ticket(rival_touches, claimed_at=_CLAIMED_AT)},
        edited=edited,
    )

    finisher, external, touches, measured = _measure(root, tf, "T-1921")
    block = finisher._default_diff_cap_block("T-1921")
    err = capsys.readouterr().err

    standalone = external.diff_line_total(root, "HEAD", touches)
    expected = 400 if endpoint == "source" or not edited else 410
    assert standalone == expected
    assert measured == tf.DiffAttribution(0, 0, (), expected)
    # 이중 계상 금지 — 양보량은 단독 측정 폭과 같고, 내 몫은 0 이다.
    assert measured.total + measured.unattributed_total == standalone
    assert block is None                       # 귀속값 0줄 — 상한 판정은 통과
    assert "디렉터리 양보 — T-1921" in err and f"{expected}줄" in err


@requires_git
@pytest.mark.parametrize("edited", [False, True],
                         ids=["exact-rename", "edited-rename"])
@pytest.mark.parametrize(
    ("exact_touches", "rival_touches", "endpoint"),
    [
        pytest.param(["src/old.py"], ["dst/new.py"], "source", id="source-exact"),
        pytest.param(["dst/new.py"], ["src/old.py"], "destination",
                     id="destination-exact"),
    ],
)
def test_exact_endpoint_claim_keeps_the_rename_eight_cell_rule(
    tmp_path, edited, exact_touches, rival_touches, endpoint,
):
    """정확 claim 한 endpoint 는 창 안 rival 이 있어도 종전 8셀 규칙 그대로다.

    어느 endpoint 든 현재 티켓이 **정확 지목**하면 양보하지 않고 단독 claim 폭을 내 몫으로
    센다 — 창 확장·양보가 이 규칙을 바꾸지 않는다는 불변 단언이다.
    """
    root, tf = _wave_rename_shape(
        tmp_path,
        {"T-1931": _ticket(exact_touches, claimed_at=_CLAIMED_AT),
         "T-1932": _ticket(rival_touches, claimed_at=_CLAIMED_AT)},
        edited=edited,
    )

    _f, external, touches, measured = _measure(root, tf, "T-1931")

    expected = 400 if endpoint == "source" or not edited else 410
    assert external.diff_line_total(root, "HEAD", touches) == expected
    assert measured == tf.DiffAttribution(expected, 0, (), 0)


# ══ 역방향 — 좁힌 폭이 큰 스코프를 놓치지 않는다 ════════════════════════════


@requires_git
def test_directory_claim_without_a_rival_keeps_the_whole_scope(tmp_path):
    """창 안 타 티켓이 그 경로를 주장하지 않으면 디렉터리 선언분은 전량 내 몫이다.

    양보는 **겹칠 때만** 발동한다 — 겹치지 않는 대형 스코프는 그대로 차단된다.
    """
    root, tf, _anchor = _wave_shape(
        tmp_path,
        {"T-1501": _ticket(["tests"], claimed_at=_CLAIMED_AT),
         "T-1502": _ticket(["src/other.py"], claimed_at=_CLAIMED_AT)},
        {"tests/test_a.py": 320, "src/other.py": 40},
    )

    finisher, _e, _t, measured = _measure(root, tf, "T-1501")
    block = finisher._default_diff_cap_block("T-1501")

    assert measured == tf.DiffAttribution(320, 40, ("T-1502",), 0)
    assert block is not None and "diff 320줄 > 상한 300줄" in block


@requires_git
def test_single_ticket_measurement_is_unchanged_by_the_window(tmp_path):
    """비-wave(겹침 0) 측정은 창이 생겨도 종전 `diff_line_total` 과 같다."""
    root, tf, _anchor = _wave_shape(
        tmp_path,
        {"T-1601": _ticket(["src/", "tests"], claimed_at=_CLAIMED_AT),
         "T-1602": _ticket(["docs/notes.md"], status="done",
                           completed_at=_COMPLETED_IN_WINDOW)},
        {"src/a.py": 11, "tests/test_a.py": 17, "docs/notes.md": 5},
    )

    _f, external, touches, measured = _measure(root, tf, "T-1601")

    assert external.diff_line_total(root, "HEAD", touches) == 28
    assert measured.total == 28 and measured.unattributed_total == 0


# ══ v1.7.8 wave 축소 재현 ═══════════════════════════════════════════════════
#
# 실측(architect 재측정)의 구조를 그대로 줄인 형상 — 디렉터리 2 · 공유 정확-파일 1 · 전용 파일
# 여럿. 현행 union 과 신규 귀속값을 **둘 다** 단언해 회귀 시 어느 쪽이 깨졌는지 바로 보이게 한다.

_V178_FILES = {
    "src/pm_delegate.py": 775,          # 세 티켓이 정확 claim 한 공유 파일
    "docs/pm_playbook.md": 24,          # T-1701 전용
    "tests/test_review_verify.py": 836,  # 아무도 정확 claim 하지 않은 디렉터리 안
    "tests/test_identity_keys.py": 636,  # 〃
    "src/ticket_rounds.py": 300,        # 먼저 완료된 티켓 전용
    "src/review_rounds.py": 530,        # T-1703 전용
}
_V178_TESTS_DIR_TOTAL = 836 + 636


def _v178_shape(tmp_path: Path, **overrides):
    tickets = {
        # T-0785 형상 — 디렉터리(tests) + 공유 정확-파일 + 전용 파일
        "T-1701": _ticket(["src/pm_delegate.py", "docs/pm_playbook.md", "tests"],
                          estimate="medium", claimed_at=_CLAIMED_AT),
        # T-0786 형상 — 같은 wave 에서 **먼저 완료**(done·창 안)
        "T-1702": _ticket(["src/pm_delegate.py", "src/ticket_rounds.py"],
                          estimate="medium", status="done",
                          claimed_at=_CLAIMED_AT,
                          completed_at=_COMPLETED_IN_WINDOW),
        # T-0787 형상 — 디렉터리 + 공유 정확-파일 + 큰 전용 파일
        "T-1703": _ticket(["src/pm_delegate.py", "tests", "src/review_rounds.py"],
                          estimate="medium", claimed_at=_CLAIMED_AT),
    }
    return _wave_shape(tmp_path, tickets, dict(_V178_FILES), **overrides)


@requires_git
def test_v178_shape_drops_under_the_cap_after_the_directory_yield(tmp_path, capsys):
    """T-0785 형상: 현행 union 2,271줄(상한 1,000 초과) → 보정 뒤 799줄로 통과한다."""
    root, tf, _anchor = _v178_shape(tmp_path)

    finisher, external, touches, measured = _measure(root, tf, "T-1701")
    union = external.diff_line_total(root, "HEAD", touches)
    block = finisher._default_diff_cap_block("T-1701")
    err = capsys.readouterr().err

    assert union == 775 + 24 + _V178_TESTS_DIR_TOTAL == 2271   # 현행 측정(오답)
    assert union > _MEDIUM_CAP                                  # 옛 규칙이면 차단이었다
    assert measured == tf.DiffAttribution(
        799, 300 + 530, ("T-1702", "T-1703"), _V178_TESTS_DIR_TOTAL)
    assert block is None                                        # 신규 규칙은 통과
    assert "디렉터리 양보 — T-1701" in err and f"{_V178_TESTS_DIR_TOTAL}줄" in err


@requires_git
def test_v178_shared_exact_file_residual_still_blocks(tmp_path):
    """T-0787 형상: 공유 정확-파일 union 은 유지되므로 보정 뒤에도 1,305줄로 차단된다.

    잔여는 의도된 것이다 — 세 티켓이 **정확 claim** 한 파일의 hunk 증거가 없으므로 과다 측정을
    유지하는 쪽이 안전하다(안분·hunk 귀속은 비목표).
    """
    root, tf, _anchor = _v178_shape(tmp_path)

    finisher, _e, _t, measured = _measure(root, tf, "T-1703")
    block = finisher._default_diff_cap_block("T-1703")

    assert measured == tf.DiffAttribution(
        775 + 530, 24 + 300, ("T-1701", "T-1702"), _V178_TESTS_DIR_TOTAL)
    assert block is not None
    assert "diff 1305줄 > 상한 1000줄" in block
    assert "타 claimed 티켓 귀속 제외: 324줄 · 티켓 T-1701, T-1702" in block
    assert f"디렉터리 양보 보류: {_V178_TESTS_DIR_TOTAL}줄" in block


@requires_git
def test_anchor_width_gets_the_same_yield(tmp_path):
    """앵커 폭(claim 시점 rev → 작업트리)에서도 같은 귀속이 나온다.

    폭 축(앵커 유무)과 귀속 축(창·양보)은 직교한다 — 커밋으로 흡수된 wave 도 같은 규칙이다.
    """
    root, tf, anchor = _v178_shape(tmp_path, commit_changes=True)

    _f, external, touches, anchored = _measure(root, tf, "T-1701",
                                               claimed_rev=anchor)
    _f2, _e2, _t2, unanchored = _measure(root, tf, "T-1701")

    assert external.diff_line_total(root, "HEAD", touches, claimed_rev=anchor) == 2271
    assert anchored == tf.DiffAttribution(
        799, 300 + 530, ("T-1702", "T-1703"), _V178_TESTS_DIR_TOTAL)
    assert unanchored.total == 0   # 옛 폭은 전파 커밋 한 칸만 본다(폭 축은 앵커 회귀 소관)


# ══ 두 진입점 — 같은 폭, 다른 질문 ══════════════════════════════════════════


@requires_git
def test_both_seams_share_one_width_and_answer_two_questions(tmp_path, capsys):
    """리뷰 진입점은 전송량(union)을, 완료 진입점은 티켓 귀속값을 낸다 — 폭은 하나다.

    폭이 같다는 것은 내 touches 위에서 두 seam 의 합이 정확히 같다는 뜻이다(`총량 = 귀속 +
    양보`). 총량이 다른 것은 결함이 아니라 질문이 다르기 때문이다 — 리뷰어에게 실제로 가는
    diff 는 union 이고, 완료 게이트가 묻는 것은 이 티켓 구현 스코프다. 이 비대칭을 없애려고
    한쪽에 다른 쪽 규칙을 이식하면 리뷰 전송량을 실제보다 작게 재게 된다.
    """
    root, tf, _anchor = _v178_shape(tmp_path)
    finisher, external, touches, measured = _measure(root, tf, "T-1701")

    union = external.diff_line_total(root, "HEAD", touches)
    review_block = external._diff_cap_refusal(
        argparse.Namespace(ticket="T-1701", gate=None, base="HEAD"), {},
        root=root, paths=touches, pm_home=root)
    finish_block = finisher._default_diff_cap_block("T-1701")
    capsys.readouterr()

    # 같은 폭 — 내 touches 위 union 은 귀속값과 양보분의 합이다(빠지는 줄이 없다).
    assert measured.total + measured.unattributed_total == union == 2271
    # 다른 총량 — 리뷰는 전송량으로 차단하고, 완료는 귀속값으로 통과한다.
    assert review_block is not None and "diff 2271줄 > 상한 1000줄" in review_block
    assert finish_block is None


# ══ 비용 — 창이 넓어져도 git 호출은 안 늘어난다 ═════════════════════════════


@requires_git
@pytest.mark.parametrize("done_count", [0, 5, 20])
def test_git_call_count_does_not_scale_with_the_window(tmp_path, done_count):
    """창 안 done 티켓 수와 무관하게 numstat 한 벌(실 git 3호출)만 소비한다."""
    tickets = {"T-1801": _ticket(["src/mine.py"], claimed_at=_CLAIMED_AT)}
    files = {"src/mine.py": 7}
    for index in range(done_count):
        tickets[f"T-{1900 + index}"] = _ticket(
            [f"src/done_{index}.py"], status="done",
            completed_at=_COMPLETED_IN_WINDOW)
        files[f"src/done_{index}.py"] = 1
    root, tf, _anchor = _wave_shape(tmp_path, tickets, files)
    git_calls: list[list[str]] = []

    def counting_real_git(args, **kwargs):
        git_calls.append(list(args))
        return subprocess.run(args, **kwargs)

    _f, _e, _t, measured = _measure(root, tf, "T-1801", run_fn=counting_real_git)

    assert measured.total == 7
    assert measured.excluded_total == done_count
    assert len(git_calls) == 3
