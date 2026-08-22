"""multi-PM 세션 뷰 격리 — composite 게이트 (2 user × 2 slot·실 생성→뷰·T-0304·ADR-0053).

이 파일은 multi-PM/multi-USER 격리 불변식(ADR-0053)을 **실제로 태우는** durable 기계 게이트다.
`test_board_multipm.py` 의 세션-격리 코어(T-0302)는 `_ticket_is_mine` 을 *hand-seed 한 YAML* 로
단위 검증하지만, 이 파일은 **각 슬롯/유저가 실 board API 로 티켓을 생성**(`cmd_new`+`cmd_claim`)해
`created_by`/`claimed_by` 를 실제 스탬프한 뒤 **뷰가 서로 섞이는지**를 검증한다 — 라이브 composite
(opencode·release tier)와 *동형의 create→view 경로*를 무-LLM 으로 못박는다(사용자 발의·PM 64).

검증하는 불변식 (ADR-0053·spike §0·[[ADR-0059]] Decision 10):
  세션 뷰(`--mine`/`--session`/`--slot`/`--task`) 멤버십 = (내 claim) ∪ (내 소유 open).
  타 사용자 미claim open 은 절대 미포함. degrade("전체 open=mine")는 solo(distinct user ≤1)에서만.
  task 렌즈(`--task <이름>`·T-0365)는 claim 축을 그 task 바인딩(`claimed_by==<user>/<task>`·⑲)으로
  좁힌 task-aware 대응 — slot 세션값 `<user>/<repo>_<N>` 과 ⑥ 예약으로 기계 판별(추가 필드 0).

명시 cross-contamination 단언 (PM 64 필수 형상):
  (a) alice 세션이 bob 미claim open **미열람**              — session/mine 뷰
  (b) slot1 세션이 slot2 전용 티켓 **미열람**               — session/slot 뷰
  (c) 각자 자기 open+claim **열람**                          — session/mine 뷰
  (d) solo(distinct ≤1) fallback 전체 open **보존**          — 회귀 0

전 surface: `list --mine`·`list --session`·`list --slot`·`list --task`(T-0365)·bootstrap
`_collect_board` 카운트(`pm_bootstrap.py` **코드변경 0**·`_ticket_is_mine` 상속 검증) + RED-증명(git
금지 하에서 monkeypatch 로 pre-fix degrade 를 시뮬해 동일 데이터에서 섞임이 실제로 감지됨을 durable 박제).

**hermetic 필수**: board.py 경로 전역을 tmp 프로젝트로 monkeypatch 재지정하고 git 폴백은 stub —
실 루트의 areas.md·tickets/·local.conf 를 절대 건드리지 않는다(test_board_multipm.py 동형).
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import re
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
REAL_AREAS = REPO / ".project_manager" / "areas.md"


def _load(name: str):
    """엔진 모듈을 (패키지 아님) 경로 로드 — 경로 전역이 매번 fresh (test_pm_bootstrap 동형)."""
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_project(root: Path) -> None:
    """tmp 프로젝트 골격 — tickets/{open,claimed,blocked,done}/ + cmd_new 용 _template.md."""
    tickets = root / ".project_manager" / "wiki" / "tickets"
    for status in ("open", "claimed", "blocked", "done"):
        (tickets / status).mkdir(parents=True, exist_ok=True)
    (tickets / "_template.md").write_text(
        "---\n"
        "id: T-NNNN\n"
        "title: <제목>\n"
        "status: open\n"
        "created: YYYY-MM-DD\n"
        "created_by:\n"
        "claimed_by:\n"
        "claimed_at:\n"
        "completed_at:\n"
        "depends_on: []\n"
        "blocks: []\n"
        "touches: []\n"
        "estimate: small\n"
        "tags: []\n"
        "---\n\n"
        "# T-NNNN — <제목>\n\n## 목표\n채운 목표.\n\n## 완료 조건\n- [ ] 채운 DoD.\n\n## 참고\n채운 참고.\n",
        encoding="utf-8",
    )


@pytest.fixture
def board(tmp_path, monkeypatch):
    """fresh board 모듈 + 모든 IO 전역을 tmp 프로젝트로 재지정한 hermetic 인스턴스."""
    proj = tmp_path / "proj"
    _make_project(proj)
    mod = _load("board")
    pm = proj / ".project_manager"
    wiki = pm / "wiki"
    local = pm / ".local"
    overrides = {
        "REPO": proj,
        "TICKETS_DIR": wiki / "tickets",
        "TEMPLATE_FILE": wiki / "tickets" / "_template.md",
        "BOARD_FILE": wiki / "board.md",
        "LOG_FILE": wiki / "log" / "current.md",
        "STATUS_FILE": wiki / "status.md",
        "LOCAL_CONF": pm / "local.conf",
        "AREAS_FILE": pm / "areas.md",
        "LOCAL_DIR": local,
        "LEASES_FILE": local / "worktree-leases.json",
        "BOARD_LOCK": local / "board.lock",
        "PM_STATE_FILE": wiki / "pm_state.md",
        "PM_STATE_TEMPLATE": wiki / "pm_state.template.md",
    }
    for name, val in overrides.items():
        monkeypatch.setattr(mod, name, val)
    local.mkdir(parents=True, exist_ok=True)
    # 실 PM 세션 env·git config 가 hermetic 테스트로 새지 않게 격리(장부/local.conf 만이 세션·user 소스).
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    monkeypatch.setattr(mod, "_git_config_email", lambda: None)
    return mod


# ── composite 매트릭스: repo alpha(prefix al·area_owner alice) · beta(prefix be·bob) ──
# 세션 alpha_1/alpha_2(alice)·beta_1/beta_2(bob). user-first(ADR-0056): querying identity 는 조회
# 시점 현재 사용자(local.conf user=)·open 소유는 area_owner 로 해소된다(area_owner=open 소유 정의).
# prefix 는 소문자(`_validate_prefix` 게이트) — repo 명(alpha/beta)과 ID prefix(al/be)는 별개 축.
_COMPOSITE_AREAS = (
    "# Area Registry\n\n"
    "| repo | prefix | git | test_cmd | owner | base | protected | area_owner |\n"
    "|---|---|---|---|---|---|---|---|\n"
    "| alpha | al | g:a | pytest -q | reg | develop | main | alice |\n"
    "| beta | be | g:b | pytest -q | reg | develop | main | bob |\n"
)

_CREATED_RE = re.compile(r"created (T-\S+)")


# 테스트 kwargs 는 python 식별자라 dot 표기를 담을 수 없다 — conf 키로 옮겨 쓴다
# (`user=` → `identity.user=`). 구표기로 쓰면 conf 를 읽는 지점이 fail-loud 로 멈춘다.
_CONF_KEY_ALIASES = {"user": "identity.user", "py": "runtime.py",
                     "test_cmd": "test.cmd", "upstream": "upstream.path",
                     "project_name": "project.name"}


def _conf_key(name: str) -> str:
    return _CONF_KEY_ALIASES.get(name, name)


def _write_conf(board, **kv) -> None:
    board.LOCAL_CONF.write_text(
        "".join(f"{_conf_key(k)}={v}\n" for k, v in kv.items()),
        encoding="utf-8")


def _new(board, capsys, *, prefix, session, user, title) -> str:
    """실 `cmd_new` 로 티켓을 발행하고(created_by = <user>/<repo>_<N> 스탬프) 발행 ID 를 캡처한다.

    ADR-0067: `cmd_new` 의 `--repo`/`--slot`(decomposed 세션 기록)으로 created_by 에 생성-세션을
    박아야 세션 기본 뷰 스트림 판정(`_created_by_session`)이 그 세션 open 을 잡는다."""
    repo, slot = _split_session(session)
    rc = board.cmd_new(argparse.Namespace(
        prefix=prefix, repo=repo, slot=slot, task=None, user=user, title=title,
        touches=None, depends=None, tag=None, estimate="small"))
    out = capsys.readouterr().out
    assert rc == 0, f"cmd_new rc={rc}: {out}"
    m = _CREATED_RE.search(out)
    assert m, f"cmd_new stdout 에 created ID 없음: {out!r}"
    return m.group(1)


def _split_session(session: str) -> tuple[str, int]:
    """세션 문자열 `<repo>_<N>` 을 (repo, slot 정수) 로 분해 — 테스트 helper 전용(ADR-0057
    decomposed CLI 를 흉내: `cmd_claim`/`cmd_list` 는 이제 `args.repo`/`args.slot` 만 읽는다)."""
    repo, _, num = session.rpartition("_")
    return repo, int(num)


def _claim(board, capsys, tid, *, session, user) -> None:
    """실 `cmd_claim` 으로 claim(claimed_by=<user>/<slot> 스탬프) — `--repo`/`--slot`(ADR-0057)
    로 호출한다. board-git 비활성이라 즉시 확정."""
    repo, slot = _split_session(session)
    rc = board.cmd_claim(argparse.Namespace(id=tid, repo=repo, slot=slot, user=user))
    out = capsys.readouterr().out
    assert rc == 0, f"cmd_claim rc={rc}: {out}"


def _view(board, capsys, **flags) -> list[str]:
    """실 `cmd_list` 렌즈(mine/repo/slot/task)를 돌려 출력에서 ticket ID 목록을 추출한다.

    `session="<repo>_<N>"` 편의 kwarg 는 내부에서 repo/slot(exact 완전일치) 으로 분해한다(호출부
    가독성 보존 — ADR-0057 이후 `cmd_list` 는 `args.repo`/`args.slot` 만 읽는다). `repo=`(단독=
    repo-scope 뷰)/`slot=`(단독이면 `--repo` 없어 fail-loud)/`task=`(task 바인딩 렌즈·T-0365) 직접
    지정도 지원한다.
    """
    repo = flags.get("repo")
    slot = flags.get("slot")
    if flags.get("session") is not None:
        repo, slot = _split_session(flags["session"])
    rc = board.cmd_list(argparse.Namespace(
        status=flags.get("status"), tag=None, mine=flags.get("mine", False),
        all=flags.get("all", False), repo=repo, slot=slot, task=flags.get("task")))
    out = capsys.readouterr().out
    assert rc == 0, f"cmd_list rc={rc}: {out}"
    ids: list[str] = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("[") and "]" in line:
            ids.append(line.split("]", 1)[1].split()[0])
    return ids


def _seed_composite(board, capsys, *, areas: bool = True) -> types.SimpleNamespace:
    """2 user(alice/bob) × 2 slot 각자 open+claim 을 **실 board API** 로 생성한다.

    `areas=True` → per-user area 등록(area_owner 운영·소유 1차 = area_owner) · prefix al/be.
    `areas=False` → 레지스트리 부재(미마이그 채택자) · legacy `T-NNNN` · 소유는 created_by 2차 폴백.
    각 정체성 전환은 `cmd_new`/`cmd_claim` 의 `--session`/`--user` override 로만(local.conf 무관).
    """
    if areas:
        board.AREAS_FILE.write_text(_COMPOSITE_AREAS, encoding="utf-8")
    al_pfx, be_pfx = ("al", "be") if areas else (None, None)

    def slot(pfx, session, user, tag):
        opened = _new(board, capsys, prefix=pfx, session=session, user=user,
                      title=f"{tag} open")
        wip = _new(board, capsys, prefix=pfx, session=session, user=user,
                   title=f"{tag} wip")
        _claim(board, capsys, wip, session=session, user=user)
        return opened, wip

    al1_open, al1_claim = slot(al_pfx, "alpha_1", "alice", "alice s1")
    al2_open, al2_claim = slot(al_pfx, "alpha_2", "alice", "alice s2")
    be1_open, be1_claim = slot(be_pfx, "beta_1", "bob", "bob s1")
    be2_open, be2_claim = slot(be_pfx, "beta_2", "bob", "bob s2")

    return types.SimpleNamespace(
        al1_open=al1_open, al1_claim=al1_claim, al2_open=al2_open, al2_claim=al2_claim,
        be1_open=be1_open, be1_claim=be1_claim, be2_open=be2_open, be2_claim=be2_claim,
        alice_all={al1_open, al1_claim, al2_open, al2_claim},
        bob_all={be1_open, be1_claim, be2_open, be2_claim},
        bob_open={be1_open, be2_open}, bob_claim={be1_claim, be2_claim})


# ════════════════════════════════════════════════════════════════════════
# surface 1 — `list --session` : (a) 타 user open 미열람 · (c) 자기 open+claim 열람
# ════════════════════════════════════════════════════════════════════════

def test_session_view_isolates_users_real_create_to_view(board, capsys):
    """실 생성 composite 에서 `--repo X --slot N` = **그 세션 스트림**(생성 open + 그 세션 claim·ADR-0067).

    open 은 `created_by` 세션이 alpha_1 인 것만(같은 사용자 타 슬롯 alpha_2 생성 open 은 제외 — PM 77
    누출의 fix)·claim 은 그 슬롯. (a) 타 사용자 무유출 · (b) 타 슬롯의 내 claim·타 슬롯 생성 open 제외.
    """
    comp = _seed_composite(board, capsys)

    # alice 정체성으로 alpha_1 슬롯 조회 — alpha_1 생성 open + alpha_1 claim 만(생성-세션 스트림).
    _write_conf(board, user="alice", session="alpha_1")
    alice_s1 = set(_view(board, capsys, session="alpha_1"))
    assert not (comp.bob_all & alice_s1), "alice 세션에 bob 티켓 유출(ADR-0056 위반)"
    # 생성 open(alpha_1)만·타 슬롯 생성 open(al2_open)·타 슬롯 claim(al2_claim) 제외(ADR-0067).
    assert alice_s1 == {comp.al1_open, comp.al1_claim}
    assert comp.al2_open not in alice_s1 and comp.al2_claim not in alice_s1

    # 같은 alice 의 --mine = 내 것 **전 슬롯**(양 슬롯 claim + open·의미론 불변·ADR-0067).
    alice_mine = set(_view(board, capsys, mine=True))
    assert not (comp.bob_all & alice_mine), "alice --mine 에 bob 티켓 유출"
    assert alice_mine == comp.alice_all

    # bob 정체성으로 beta_2 슬롯 조회 — 대칭. alice 티켓 무유출·생성 open+claim 은 beta_2 만.
    _write_conf(board, user="bob", session="beta_2")
    bob_s2 = set(_view(board, capsys, session="beta_2"))
    assert not (comp.alice_all & bob_s2), "bob 세션에 alice 티켓 유출"
    assert bob_s2 == {comp.be2_open, comp.be2_claim}
    assert comp.be1_open not in bob_s2 and comp.be1_claim not in bob_s2


# ════════════════════════════════════════════════════════════════════════
# surface 2 — `list --mine` : (a) 타 user open 미열람 · (c) 자기 open+claim 열람
# ════════════════════════════════════════════════════════════════════════

def test_mine_view_isolates_users_real_create_to_view(board, capsys):
    """`--mine`(local.conf identity)도 동형 — alice 의 --mine 은 bob 소유 open/claim 을 제외한다."""
    comp = _seed_composite(board, capsys)
    _write_conf(board, user="alice", session="alpha_1")

    alice_ids = set(_view(board, capsys, mine=True))
    assert not (comp.bob_all & alice_ids), "alice --mine 에 bob 티켓 유출"
    assert alice_ids == comp.alice_all


# ════════════════════════════════════════════════════════════════════════
# surface 3 — `list --repo X`(kind=repo·신규 repo-scope 뷰) + bare `--slot N` fail-loud (ADR-0057
# 결정 2/3·T-0314 — 구 bare `--slot N`[repo 불문 cross-repo suffix 매칭]을 대체)
# ════════════════════════════════════════════════════════════════════════

def test_bare_slot_without_repo_fails_loud_in_composite(board, capsys):
    """구 bare `--slot N`(repo 불문 cross-repo 매칭)은 ADR-0057 로 제거됐다 — composite
    다중슬롯 맥락에서도 `--repo` 없는 `--slot` 은 여전히 fail-loud(uniform·solo 예외 없음)."""
    _seed_composite(board, capsys)
    _write_conf(board, user="alice", session="alpha_1")
    with pytest.raises(SystemExit) as exc:
        board.cmd_list(argparse.Namespace(status=None, tag=None, mine=False,
                                          repo=None, slot=1))
    assert "--repo" in str(exc.value)


def test_repo_alone_view_shows_all_my_slots_in_repo(board, capsys):
    """`--repo alpha` 단독(kind=repo) = 현재 사용자(alice) ∩ **그 repo 의 내 슬롯 전체**
    (user-first·ADR-0056·신규 repo-scope 뷰 — spike §3.1).

    **타 사용자 무유출(codex leak 가드 계승)**: alice 의 alpha_1+alpha_2 claim 을 모두 보이고
    (repo-prefix 매칭), bob(beta·타 사용자·타 repo) 은 어느 쪽도 안 섞인다."""
    comp = _seed_composite(board, capsys)
    _write_conf(board, user="alice", session="alpha_1")

    alpha_ids = set(_view(board, capsys, repo="alpha"))
    assert comp.al1_claim in alpha_ids                # 내 alpha_1 claim
    assert comp.al2_claim in alpha_ids                # 신규: 내 alpha_2 claim 도 포함(그 repo 전체)
    assert not (comp.bob_all & alpha_ids)             # bob(타 사용자·타 repo) 무유출

    beta_ids = set(_view(board, capsys, repo="beta"))
    assert comp.al1_claim not in beta_ids and comp.al2_claim not in beta_ids  # repo 불일치 → 제외
    assert not (comp.bob_all & beta_ids)              # bob(타 사용자) claim/open 전부 무유출
    # open 은 슬롯무관 backlog(ADR-0056 #3) — repo 뷰와 무관하게 내 open 은 그대로 보인다.
    assert beta_ids == {comp.al1_open, comp.al2_open}


def test_session_excludes_other_users_slot_exclusive_claim(board, capsys):
    """(b) 명시: alice(현재 사용자) slot1 세션(alpha_1)이 bob 의 어떤 claim 도 미열람한다.

    타 user claim 은 user 불일치(cb_user=bob≠alice)로 제외 — bob 의 slot2 전용 claim(beta_2)도,
    slot1 claim(beta_1)도 모두. 비공허: alice 자기 alpha_1 claim(al1_claim)은 보여 뷰가 안 빈다.
    """
    comp = _seed_composite(board, capsys)
    _write_conf(board, user="alice", session="alpha_1")
    alice_ids = set(_view(board, capsys, session="alpha_1"))
    assert comp.al1_claim in alice_ids                # 비공허 — 내 alpha_1 claim 은 보임
    assert comp.be2_claim not in alice_ids
    assert not (comp.bob_all & alice_ids)             # bob 티켓(claim·open) 전부 무유출


# ════════════════════════════════════════════════════════════════════════
# surface 5 — `list --task <이름>` : task 바인딩 렌즈 (T-0365·[[ADR-0059]] Decision 10)
#   멤버십 = claimed_by==<user>/<task> claim ∪ 내 소유 open backlog. 타 user·타 task·slot claim
#   미열람. ⑥ 예약(task 명 ≠ `<repo>_<N>`) 으로 slot 세션값과 기계 판별(claimed_by 재사용·추가 필드 0).
# ════════════════════════════════════════════════════════════════════════

def _claim_task(board, capsys, tid, *, task, user) -> None:
    """실 `cmd_claim --task` 로 task-mode claim (claimed_by=<user>/<task> 스탬프·⑲ claimed_by 재사용).

    `_claim`(slot-mode·`--repo`/`--slot`)의 task 짝 — `--task <이름>` 이 정체성 축이라 claimed_by 의
    slot 토큰 자리에 task 이름이 들어간다(`_actor_session_override` 가 task 를 세션 override 로 반환).
    """
    rc = board.cmd_claim(argparse.Namespace(
        id=tid, repo=None, slot=None, task=task, user=user))
    out = capsys.readouterr().out
    assert rc == 0, f"cmd_claim --task rc={rc}: {out}"


def _seed_task_composite(board, capsys):
    """slot claim composite 위에 task-mode claim 을 얹어 slot/task 두 축을 한 보드에서 섞는다.

    alice: slot claim(alpha_1/alpha_2·from _seed_composite) + task 'refactor' claim + open backlog.
    bob:   slot claim(beta_1/beta_2) + task 'docs' claim + open backlog. 두 축이 공존해야 렌즈가
    서로 안 섞임(기계 판별·⑥)을 실 데이터로 검증할 수 있다.
    """
    comp = _seed_composite(board, capsys)
    al_task = _new(board, capsys, prefix="al", session="alpha_1", user="alice",
                   title="alice refactor wip")
    _claim_task(board, capsys, al_task, task="refactor", user="alice")
    be_task = _new(board, capsys, prefix="be", session="beta_1", user="bob",
                   title="bob docs wip")
    _claim_task(board, capsys, be_task, task="docs", user="bob")
    comp.al_refactor = al_task
    comp.be_docs = be_task
    return comp


def test_task_lens_isolates_by_binding(board, capsys):
    """`--task refactor`(alice) = 이 task 명의 claim + 내 소유 open backlog · 타 user·타 task·slot 미열람.

    (b) claim 축은 claimed_by==<user>/<task> 로 좁혀진다 — alice 의 slot claim(alpha_1/alpha_2)은
    같은 user 라도 task 토큰 불일치(⑥ 판별)라 제외, bob(타 user)·bob task(docs)도 제외. (a) open 은
    ADR-0053 task 대응으로 내 소유 backlog(슬롯무관) 포함.
    """
    comp = _seed_task_composite(board, capsys)
    _write_conf(board, user="alice", session="alpha_1")

    ids = set(_view(board, capsys, task="refactor"))
    assert comp.al_refactor in ids                       # (b) 이 task 명의 claim
    assert comp.al1_claim not in ids                     # 내 slot claim = 타 task 토큰 → 제외(⑥)
    assert comp.al2_claim not in ids
    assert comp.be_docs not in ids                       # 타 user·타 task claim 제외
    assert not (comp.bob_all & ids), "task 렌즈에 bob 티켓 유출"
    assert comp.al1_open in ids and comp.al2_open in ids  # (a) 내 소유 open backlog(슬롯무관)
    assert ids == {comp.al_refactor, comp.al1_open, comp.al2_open}


def test_task_and_slot_lenses_machine_discriminate(board, capsys):
    """⑥ 예약 규칙 기계 판별: slot claim `<user>/<repo>_<N>` 은 --task 렌즈에, task claim
    `<user>/<task>` 는 --slot 렌즈에 서로 안 섞인다(claimed_by 재사용·추가 필드 0·§F5b).

    같은 user(alice)의 두 claim 형태를 두 렌즈로 교차 조회해 각 렌즈가 자기 축의 claim 만 매칭하고
    타 축 claim 을 걸러냄을 실 데이터로 못박는다(DoD: slot 값 vs task 값 판별).
    """
    comp = _seed_task_composite(board, capsys)
    _write_conf(board, user="alice", session="alpha_1")

    # task 렌즈: task claim O · slot claim X.
    task_ids = set(_view(board, capsys, task="refactor"))
    assert comp.al_refactor in task_ids
    assert comp.al1_claim not in task_ids, "slot claim <alice>/<alpha_1> 이 --task 렌즈에 섞임(⑥ 위반)"

    # slot 렌즈: slot claim O · task claim X.
    slot_ids = set(_view(board, capsys, session="alpha_1"))
    assert comp.al1_claim in slot_ids
    assert comp.al_refactor not in slot_ids, "task claim <alice>/<refactor> 가 --slot 렌즈에 섞임(⑥ 위반)"


def test_list_task_flag_is_consumed_not_silent_noop(board, capsys):
    """(이월 ②) 파서-수용/핸들러-소비: cmd_list 가 `--task` 를 실제 소비해 필터가 좁혀진다.

    `--task` 는 list 파서에 등록돼(add_identity_args) *수용*되지만, 핸들러가 무시하면(옛 동작·silent
    no-op) 무필터 전체 보드가 나와 타 user·타 task claim 이 섞인다 — 그 부재를 assert 로 못박는다
    (`test_pm_bootstrap_card_parity` 등록-only 가드의 behavior 짝). 존재하지 않는 task 도 whole-board
    와 달라야(claim 0 → 내 open 만) 필터가 실제로 걸렸음이 증명된다.
    """
    comp = _seed_task_composite(board, capsys)
    _write_conf(board, user="alice", session="alpha_1")

    task_ids = set(_view(board, capsys, task="refactor"))
    whole = set(_view(board, capsys, all=True))          # 무필터 전체 보드(모든 user·claim·ADR-0066 `--all`)
    # 소비됐다면 task 렌즈 ⊊ 전체(bob·타 task 제외)·미소비(no-op)면 전체와 동일.
    assert task_ids < whole, "--task 가 silent no-op — 렌즈가 전체 보드와 동일(핸들러 미소비)"
    assert not (comp.bob_all & task_ids), "무필터라면 섞였을 bob 티켓이 task 렌즈에 없다(필터 실동작)"

    # 존재하지 않는 task → claim 0 → 내 소유 open backlog 만(전체 보드 아님·필터 실동작 재확인).
    none_ids = set(_view(board, capsys, task="nonexistent"))
    assert comp.al_refactor not in none_ids and comp.al1_claim not in none_ids
    assert none_ids == {comp.al1_open, comp.al2_open}


def test_task_lens_rejects_reserved_slot_pattern(board, capsys):
    """read 경로 깔때기 소비(codex must-fix·T-0355 게이트): `--task alpha_1`(슬롯 예약 패턴·⑥) fail-loud.

    cmd_list 가 `_validate_actor_task_or_exit` 를 안 태우면 예약 패턴 task 명이 `_slot_matches` exact
    에서 slot claim `<user>/alpha_1` 을 task claim 처럼 매칭해 ⑥ 기계 판별이 깨진다 — 거부가 그 유출
    경로 자체를 닫는다(부작용 0 조회도 소비 지점 폐쇄에 합류·전 surface 단일 규칙).
    """
    _seed_composite(board, capsys)
    _write_conf(board, user="alice", session="alpha_1")
    with pytest.raises(SystemExit) as exc:
        _view(board, capsys, task="alpha_1")
    assert "예약" in str(exc.value) or "--task" in str(exc.value)


def test_task_lens_mutually_exclusive_with_other_scopes(board, capsys):
    """`--task` 는 --mine/--repo/--slot 과 상호 배타(뷰 스코프는 하나·모호 방지) — 함께 주면 fail-loud."""
    _seed_composite(board, capsys)
    _write_conf(board, user="alice", session="alpha_1")
    for extra in ({"mine": True}, {"repo": "alpha"}, {"repo": "alpha", "slot": 1}):
        with pytest.raises(SystemExit) as exc:
            _view(board, capsys, task="refactor", **extra)
        assert "--task" in str(exc.value)


# ════════════════════════════════════════════════════════════════════════
# surface 4 — bootstrap `_collect_board` 카운트 격리 (A-bootstrap 흡수·코드변경 0)
# ════════════════════════════════════════════════════════════════════════

def _make_board_runner(board):
    """pm_bootstrap `run_board_fn` seam 을 **실 board.cmd_list** 로 배선한다(subprocess 없이).

    `_collect_board` 이 부르는 `["list","--mine"]`·`["list","--status","done","--mine"]`·
    `["lint","--gate"]` 를 dispatch — 격리는 `--mine`(=`_ticket_is_mine`) 가 담당하므로 이 러너가
    bootstrap 카운트가 그 predicate 를 그대로 상속함을 태운다(pm_bootstrap.py 무변경·ADR-0067 로 옛
    `["list","--all"]` 재조회는 폐기됐으나 러너는 방어적으로 여전히 수용).
    """
    def run(argv):
        if argv[:1] == ["lint"]:
            return 0, "✓ no lint issues\n"
        status = argv[argv.index("--status") + 1] if "--status" in argv else None
        args = argparse.Namespace(status=status, tag=None, mine="--mine" in argv,
                                  all="--all" in argv, task=None, repo=None, slot=None)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = board.cmd_list(args)
        return rc, buf.getvalue()
    return run


def test_bootstrap_collect_board_inherits_isolation(board, capsys):
    """부트스트랩 카운트(`_collect_board`=`list --mine`/`--status done --mine`)가 격리를 상속한다.

    `pm_bootstrap.py` 코드변경 0 — `_ticket_is_mine` 상속 검증(A-bootstrap). alice 정체성에서
    수집한 open_tickets·counts 는 bob 의 미claim open·claim 을 반영하지 않는다(카운트 오염 0).
    """
    comp = _seed_composite(board, capsys)
    _write_conf(board, user="alice", session="alpha_1")
    pm_bootstrap = _load("pm_bootstrap")

    bs = pm_bootstrap.PmBootstrap(
        run_board_fn=_make_board_runner(board),
        log_file=board.LOG_FILE, areas_file=board.AREAS_FILE)
    result = bs._collect_board()

    open_tickets = set(result["open_tickets"])
    assert not (comp.bob_open & open_tickets), "bootstrap open_tickets(--mine 리스트) 에 bob open 유출"
    assert open_tickets == {comp.al1_open, comp.al2_open}
    # claimed/done 은 `--mine`(=`_ticket_is_mine`) 격리 상속 — alice 것만(claimed 2·done 0).
    assert result["counts"]["claimed"] == 2
    assert result["counts"]["done"] == 0
    # open 카운트도 이제 `--mine` 스코프(ADR-0067 — 옛 `--all` 전량 모수·접힘 카운트 폐기)라 alice
    # 소유 open 2건. `_collect_board` 는 `--all` 재조회를 안 하므로 stream_open/other_open_count 키도 없다.
    assert result["counts"]["open"] == 2
    assert "stream_open" not in result and "other_open_count" not in result


# ════════════════════════════════════════════════════════════════════════
# RED-증명 (git 금지·durable no-git) — pre-fix degrade 를 monkeypatch 로 시뮬해
# 동일 생성 데이터에서 격리 단언이 실제로 섞임을 잡음을 박제한다. 실 predicate 로는 green
# (위 스위트). ADR-0053 fix(T-0302)는 이미 머지돼 green 이라 git revert 대신 이 시뮬로 catch 검증.
# ════════════════════════════════════════════════════════════════════════

def _make_pre_fix_is_mine(board):
    """ADR-0053(T-0302) fix **이전**의 `_ticket_is_mine` 재현 — multi_user 게이트 없이
    `my_user is None ∨ not area_owner_in_use` 면 전체 open=mine 으로 degrade(=유출·spike §1)."""
    def _is_mine(status, fm, my_user, my_slot, area_owner_in_use, multi_user,
                 *, slot_mode="exact", slot_scoped=False):
        cb = fm.get("claimed_by") or ""
        if cb:
            cb_user = board._claimed_by_user(cb)
            if (my_user is not None and cb_user == my_user) or \
                    board._slot_matches(cb, my_slot, mode=slot_mode):
                return True
        if status == "open":
            if my_user is None or not area_owner_in_use:
                return True   # 옛 degrade — 다중사용자 미claim open 유출
            return board._ticket_area_owner(fm.get("id") or "") == my_user
        return False
    return _is_mine


def test_pre_fix_session_scoping_leaks_red_proof(board, capsys, monkeypatch):
    """RED-증명(ADR-0067·`--repo/--slot` 세션 뷰): 옛 세션 뷰는 open 을 user-단위(`_ticket_is_mine`·
    슬롯무관 backlog)로 보여 같은 사용자의 **타 슬롯 생성 open**(al2_open·alpha_2)을 alpha_1 뷰에
    유출했다(PM 77 발단). 생성-세션 스트림 fix 는 이를 제외한다.

    green 스위트의 `assert al2_open not in alice_s1` 가 바로 이 유출을 잡는다(catch 검증·
    [[verify-real-output-not-just-review]])."""
    comp = _seed_composite(board, capsys)   # 실 predicate(생성-세션 스트림)로는 격리(위 green 테스트가 확증)
    _write_conf(board, user="alice", session="alpha_1")
    # 옛 동작 재현: 세션 뷰가 생성-세션 스트림 대신 user-단위(`_ticket_is_mine`·slot-scoped)로 판정 —
    # open 은 슬롯무관이라 alice 소유의 alpha_2 생성 open 도 alpha_1 뷰에 섞인다(ADR-0067 이 닫은 누출).
    def _pre_fix_in_view(status, fm, my_user, my_session, area_owner_in_use, multi_user):
        return board._ticket_is_mine(status, fm, my_user, my_session or "", area_owner_in_use,
                                     multi_user, slot_mode="exact", slot_scoped=True)

    monkeypatch.setattr(board, "_in_default_view", _pre_fix_in_view)

    leaked = set(_view(board, capsys, session="alpha_1"))
    # RED: pre-fix user-단위 open 은 alice 의 타 슬롯(alpha_2) 생성 open 을 alpha_1 세션 뷰에 섞는다.
    assert comp.al2_open in leaked, "pre-fix user-단위 open 이 유출 재현 못함 — RED-proof 무효(sim 오류)"


def test_pre_fix_mine_unmigrated_leaks_red_proof(board, capsys, monkeypatch):
    """RED-증명(T2·`--mine`·미마이그): area_owner 미운영이면 옛 코드는 `not area_owner_in_use →
    return True` 로 전체 open 유출 — 레지스트리 부재 composite 에서 bob open 이 alice --mine 에 섞인다.

    실 predicate(created_by 2차 폴백·multi_user strict)로는 제외됨을 먼저 확인(green)한 뒤, 시뮬로 유출 박제.
    """
    comp = _seed_composite(board, capsys, areas=False)   # 레지스트리 부재 → area_owner 미운영
    _write_conf(board, user="alice", session="alpha_1")

    # green: 실 predicate 는 created_by.user 2차 폴백 + 다중사용자 strict 로 bob open 제외.
    assert not (comp.bob_open & set(_view(board, capsys, mine=True)))

    # RED: pre-fix degrade(area_owner 미운영 → 전체 open=mine)는 bob open 을 alice --mine 에 유출.
    monkeypatch.setattr(board, "_ticket_is_mine", _make_pre_fix_is_mine(board))
    leaked = set(_view(board, capsys, mine=True))
    assert comp.bob_open <= leaked, "pre-fix T2 degrade 가 유출을 재현 못함 — RED-proof 무효"


# ════════════════════════════════════════════════════════════════════════
# (d) solo(distinct user ≤1) fallback — 전체 open 보존(degrade 정당·회귀 0)
# ════════════════════════════════════════════════════════════════════════

def test_solo_single_user_fallback_preserves_all_open(board, capsys):
    """solo(단일 user·distinct ≤1) — 실 생성 데이터에서 `--mine` 이 전체 open 을 보존한다(빈 보드 금지).

    한 user(alice)만 생성하면 `_distinct_ticket_users()==1` → solo → 소유 미해소 open 도 degrade 로
    표시. 다중사용자 strict-exclude 가 solo 로 잘못 확장되지 않음을 못박는다(ADR-0053 (d)·회귀 0).
    """
    # 레지스트리 부재 solo — legacy T-NNNN, area_owner 미운영. 단일 user 라 소유 미해소 open 보존.
    o1 = _new(board, capsys, prefix=None, session="home_1", user="alice", title="o1")
    o2 = _new(board, capsys, prefix=None, session="home_1", user="alice", title="o2")
    c1 = _new(board, capsys, prefix=None, session="home_1", user="alice", title="c1")
    _claim(board, capsys, c1, session="home_1", user="alice")
    _write_conf(board, user="alice", session="home_1")

    ids = set(_view(board, capsys, mine=True))
    # distinct user = {alice} = 1 → solo → 전체 open(o1·o2) + 내 claim(c1) 보존.
    assert ids == {o1, o2, c1}


# ════════════════════════════════════════════════════════════════════════
# codex must-fix (cmd_new --repo/--slot 경로) — 생성-세션 기록 + prefix 유도
# ════════════════════════════════════════════════════════════════════════

def test_new_repo_slot_user_unresolved_open_visible_in_own_session_view(board, capsys):
    """**codex must-fix 1**: user 미해소 발행이면 `identity_tag` 가 슬롯-only(slash 없음)를 낸다.

    `_created_by_session` 이 slash 없는 값을 전부 None(세션 부재)으로 봤으면 그 open 이 자기 slot
    세션 뷰(`--repo alpha --slot 1`)에서 즉시 소실됐다(ADR-0067 "생성 세션 open" 위반). ⑥ 슬롯 세션
    예약 패턴 판별로 session 토큰으로 취급 → 자기 생성 open 이 보여야 한다.
    """
    board.areas_append("al", "alpha", "owner", repo="alpha")
    # conf user= 없음 + git stub None → user 미해소 → created_by = 슬롯-only "alpha_1"(slash 없음).
    tid = _new(board, capsys, prefix="al", session="alpha_1", user=None, title="unresolved user open")
    path = next((board.TICKETS_DIR / "open").glob(f"{tid}-*.md"))
    fm, _ = board.load_ticket(path)
    assert fm["created_by"] == "alpha_1", (
        f"user 미해소 발행 created_by 가 슬롯-only(모호 값) 아님 — sim 무효: {fm['created_by']}")
    # alpha_1 세션 뷰에 그 open 이 보인다(생성-세션 스트림 소실 회귀 방지).
    ids = set(_view(board, capsys, session="alpha_1"))
    assert tid in ids, f"user 미해소 슬롯-발행 open 이 자기 세션 뷰에서 소실(must-fix 1 미해소): {ids}"


def test_new_repo_slot_derives_prefix_multi_repo(board, capsys):
    """**codex must-fix 2**: 등록 repo ≥2 에서 `new --repo alpha --slot 1`(무 --prefix)이 해소한
    세션으로 prefix 를 유도(alpha→al)해 발행 성공한다.

    `id_prefix(override, session=task_session)` 로 세션을 안 넘기면 전역 재해소(모호 None)로 유도 실패 →
    "prefix 필요"(등록 repo ≥2 가드)로 거부되던 회귀."""
    board.AREAS_FILE.write_text(_COMPOSITE_AREAS, encoding="utf-8")   # alpha=al · beta=be (2 등록)
    rc = board.cmd_new(argparse.Namespace(
        prefix=None, repo="alpha", slot=1, task=None, user="alice",
        title="multi repo derive", touches=None, depends=None, tag=None, estimate="small"))
    out = capsys.readouterr().out
    assert rc == 0, f"multi-repo(등록 2)에서 `--repo/--slot` prefix 유도 실패(거부됨): {out}"
    m = _CREATED_RE.search(out)
    assert m, f"created ID 없음: {out}"
    assert m.group(1).startswith("T-al-"), f"alpha→al prefix 유도 실패(전역 재해소 fallback?): {m.group(1)}"


def test_claim_repo_slot_querying_user_unresolved_visible_in_own_session_view(board, capsys):
    """user-qualified claim(`alice/alpha_1`)이 있는데 **조회 user 가 미해소**(git
    email 부재)면, 옛 세션 뷰(user-first `_ticket_is_mine` 재사용)는 my_user None 이라 자기 세션 claim
    을 숨겼다(cb_user!=None·my_user None → user 분기 실패·legacy 분기도 미발동). 세션 뷰 claim
    축은 session 라벨로 통일해 조회 user 미해소여도 세션(alpha_1)이 일치하면 보인다.
    """
    board.areas_append("al", "alpha", "owner", repo="alpha")
    tid = _new(board, capsys, prefix="al", session="alpha_1", user="alice", title="wip")
    _claim(board, capsys, tid, session="alpha_1", user="alice")
    cpath = next((board.TICKETS_DIR / "claimed").glob(f"{tid}-*.md"))
    fm, _ = board.load_ticket(cpath)
    assert fm["claimed_by"] == "alice/alpha_1", f"claim 스탬프 예상 밖 — sim 무효: {fm['claimed_by']}"
    # 조회 user 미해소(conf user= 없음·git stub None) — 세션만 alpha_1 로 조회.
    ids = set(_view(board, capsys, session="alpha_1"))
    assert tid in ids, f"조회 user 미해소인데 자기 세션 claim 이 세션 뷰에서 소실(R2 must-fix 미해소): {ids}"


def test_slot_view_claim_same_session_isolated_by_user_real_create(board, capsys):
    """실 create/claim 경로에서도 동명 세션의 타 user claim은 기본 뷰에서 제외된다.

    바로 위 user 미해소 solo 가시성은 유지하되, user 해소 시에는 user ∧ session을
    strict 적용한다."""
    board.areas_append("al", "alpha", "owner", repo="alpha")
    # alice·bob 둘 다 alpha_1 세션으로 실 claim (동명 세션·user 상이).
    a = _new(board, capsys, prefix="al", session="alpha_1", user="alice", title="alice wip")
    _claim(board, capsys, a, session="alpha_1", user="alice")
    b = _new(board, capsys, prefix="al", session="alpha_1", user="bob", title="bob wip")
    _claim(board, capsys, b, session="alpha_1", user="bob")
    _write_conf(board, user="alice", session="alpha_1")
    ids = set(_view(board, capsys, session="alpha_1"))
    assert a in ids
    assert b not in ids, f"타 user(bob)의 동명 세션(alpha_1) claim 누출: {ids}"


# ════════════════════════════════════════════════════════════════════════
# hermetic 입증 — 실 루트 areas.md 미오염
# ════════════════════════════════════════════════════════════════════════

def test_real_root_areas_md_untouched(board):
    """이 모듈 실행이 실 루트 areas.md 를 만들지 않았음을 입증(hermetic 격리 회귀 가드)."""
    assert not REAL_AREAS.exists(), (
        f"실 루트 areas.md 가 생성됨 ({REAL_AREAS}) — hermetic 격리 위반")
