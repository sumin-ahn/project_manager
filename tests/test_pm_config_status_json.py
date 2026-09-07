"""`pm-config status --json [--with-board] [--with-adapters]` — 관리자 읽기 계약 (T-0907).

판정은 **실물**로 한다: tmp PM 홈에 실 `worktree-leases.json`·실 `local.conf`·실 `install.json`·
실 `adapter_baseline.json`·실 git 저장소와 실 git worktree 슬롯을 만들고, 그 홈의 엔진 사본을
서브프로세스로 돌려 stdout 을 `json.loads` 로 파싱해 필드별로 단언한다. 어댑터 기능 표는 이
저장소의 **실 `templates/<harness>/`** 를 `--from` 소스로 읽어 실 파일끼리 대조한다(수기 dict·
가짜 runner 로 흉내 낸 판정은 근거로 쓰지 않는다).

커버:
  - 가벼운 절 전수(schema·engine_rev·pm_home·generated_at·slots·tasks·git·repos·harnesses·conf).
  - `alive` 세 형태 — 살아 있는 pid(이 테스트 프로세스)·죽은 pid·readonly 슬롯 pid 0. 값은
    `worktree_pool._pid_alive` 와 같아야 한다(판정 재구현 0).
  - `role` 정규화(장부에 키가 없는 행 → `work` · readonly 행 → `readonly`)와 task 귀속.
  - `conf` 다섯 목록(필수 누락·폐지·미지·표준과 다름·최신 기준 누락 — 마지막은 소스 레지스트리).
  - `--with-board`: `STATUS_DIRS` 다섯 축 counts(초안 미집계)·open 목록·lint 목록(이슈가 있어도
    JSON 산출 계속)·회귀/라이브게이트 sidecar 그대로.
  - `--with-adapters`: 파일별 `mode`/`status` 가 엔진 어휘 그대로(네 상태 각 1)·문서 파일은
    검사 없음 한 줄·기능 표(없음/다름/폐지됨)와 `distance` 정의·기준/최신 diff 둘.
  - 소스 해소: `--from` 없이 local.conf `upstream.path=file://…` 이 T-0908 캐시로 해소되고
    그 결과가 `--from <캐시>` 와 같은 JSON 이다(네트워크 0). 미해소는 rc≠0 이고 stdout 에
    부분 JSON 이 0바이트.
  - JSON 경로가 `.project_manager/` 아래에 아무것도 쓰지 않는다(트리 해시 전후 동일).
  - 텍스트 경로 바이트 불변(골든) · 기본 호출이 board 를 로드하지 않음 · `whoami --json` 없음.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from _git_fixture import remove_git_tree
# T-0908 의 URL 캐시 픽스처(로컬 bare + `file://` · 네트워크 0)를 그대로 쓴다 — 캐시 자리·
# 형상 판정을 두 벌로 만들지 않는다.
from test_pm_update_url_cache import (  # noqa: E402
    _make_bare, _make_framework_repo, _only_cache,
)

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
TEMPLATES = REPO / "templates"

_GIT = shutil.which("git")
_git_required = pytest.mark.skipif(_GIT is None, reason="git 바이너리 없음")

# 이 pid 는 죽어 있어야 한다 — 테스트가 그 사실을 엔진 seam 으로 먼저 확인한다(우연 통과 차단).
_DEAD_PID = 999999


# ── 모듈 로드 ────────────────────────────────────────────────────────────────


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pc():
    return _load("pm_config_status_json", "pm_config.py")


@pytest.fixture(scope="module")
def wp():
    return _load("worktree_pool_status_json", "worktree_pool.py")


# ── 실 git 헬퍼 (test_task_cycle_e2e 동형) ────────────────────────────────────


def _git(cwd, *argv):
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.invalid",
    })
    return subprocess.run([_GIT, "-C", str(cwd), *argv], check=True,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env)


# ── tmp PM 홈 (실 엔진 사본 + 실 장부) ────────────────────────────────────────


def _engine_home(root: Path) -> Path:
    """엔진 사본을 담은 tmp PM 홈 — 실 CLI 를 이 홈에서 돌린다(REPO 앵커 = 이 홈).

    홈을 **자기 git 저장소로 선언**한다(`tests/_git_fixture` 규약). pytest 임시 루트가 이
    저장소 work tree 안이라, 선언이 없으면 홈 안에서 도는 git 질문(`git config core.hooksPath`·
    `rev-parse`)이 이 저장소의 답을 받아 실 PM 홈의 sidecar 를 읽는다(실측: livegate 값이
    픽스처가 아니라 실 홈 것으로 나왔다).
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / ".project_manager" / ".local").mkdir(parents=True, exist_ok=True)
    shutil.copytree(TOOLS, root / ".project_manager" / "tools",
                    ignore=shutil.ignore_patterns("__pycache__"), dirs_exist_ok=True)
    if _GIT is not None:
        _git(root, "init", "-q", "-b", "main")
        _git(root, "commit", "-q", "--allow-empty", "-m", "init")
    return root


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _write_json(path: Path, document) -> Path:
    return _write(path, json.dumps(document, ensure_ascii=False, indent=2) + "\n")


def _ledger(home: Path, leases: list[dict], tasks: list[dict] | None = None) -> Path:
    return _write_json(home / ".project_manager" / ".local" / "worktree-leases.json",
                       {"leases": leases, "tasks": tasks or []})


def _run_cli(home: Path, *argv: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(home / ".project_manager" / "tools" / "pm_config.py"), *argv],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(home), timeout=300, env=env,
    )


def _status_json(home: Path, *argv: str) -> dict:
    """`status --json …` 을 실 서브프로세스로 돌리고 stdout 을 파싱한다(rc 0 강제)."""
    done = _run_cli(home, "status", "--json", *argv)
    assert done.returncode == 0, f"rc={done.returncode}\nout={done.stdout}\nerr={done.stderr}"
    lines = [line for line in done.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"기계 출력은 한 줄이어야 한다: {done.stdout!r}"
    return json.loads(lines[0])


def _make_slot(home: Path, repo: str, number: int, branch: str) -> str:
    """실 bare + 실 `git worktree add` 로 만든 슬롯 — 슬롯 git 요약이 실물에서 나온다."""
    origin = home / ".origin"
    if not origin.exists():
        origin.mkdir(parents=True)
        _git(origin, "init", "-q", "-b", "trunk")
        _write(origin / "README.md", "seed\n")
        _git(origin, "add", "README.md")
        _git(origin, "commit", "-q", "-m", "init")
    bare = home / ".repos" / f"{repo}.git"
    if not bare.exists():
        bare.parent.mkdir(parents=True, exist_ok=True)
        _git(home, "clone", "--bare", "-q", str(origin), str(bare))
    slot = f"work/{repo}_{number}"
    _git(bare, "worktree", "add", "-q", "-b", branch, str(home / slot))
    return slot


@pytest.fixture
def home(tmp_path):
    return _engine_home(tmp_path / "home")


# ── ① 가벼운 절 — 실 장부·실 git 슬롯 ────────────────────────────────────────


@_git_required
def test_status_json_light_sections_from_real_ledger(home, pc, wp):
    """가벼운 절 전수 — 실 장부/실 슬롯에서 나온 값이 필드마다 실린다."""
    slot = _make_slot(home, "app", 1, "feat/x")
    _write(home / ".project_manager" / "local.conf",
           "project.name=demo\ntest.cmd=pytest -q\nruntime.py=python3\n")
    _write(home / ".project_manager" / "areas.md",
           "| repo | prefix | git | test_cmd | owner | base | protected | area_owner |\n"
           "|---|---|---|---|---|---|---|---|\n"
           "| app |  | git@example.invalid:app.git | pytest -q | me | trunk | main | me |\n")
    _write_json(home / ".project_manager" / "install.json",
                {"harnesses": ["claude"], "schema": 2})
    _write(home / "CLAUDE.md", "# demo\n")
    (home / ".claude").mkdir(exist_ok=True)
    _ledger(home, [{
        "slot": slot, "repo": "app", "session": "main", "pid": os.getpid(),
        "started": "2026-09-07T00:00:00+00:00", "state": "leased",
        "test_cmd": "pytest -q",
    }], [{"name": "main", "prefix": "app", "pid": os.getpid(),
          "started": "2026-09-07T00:00:00+00:00"}])

    document = _status_json(home)

    assert document["schema"] == pc.STATUS_JSON_SCHEMA == 1
    assert document["engine_rev"] == pc.ENGINE_REV
    assert document["pm_home"] == str(home.resolve())
    assert document["generated_at"].endswith("+00:00")
    assert document["board"] is None and document["adapters"] is None

    assert [row["slot"] for row in document["slots"]] == [slot]
    row = document["slots"][0]
    assert row["repo"] == "app" and row["state"] == "leased" and row["session"] == "main"
    assert row["pid"] == os.getpid() and row["alive"] is True
    assert row["role"] == "work" and row["task"] == "main"
    assert row["branch"] == "feat/x"
    assert row["head"] == _git(home / slot, "rev-parse", "HEAD").stdout.strip()
    assert row["base"] is None and row["behind"] is None and row["dirty"] is False

    assert document["tasks"] == [{
        "name": "main", "prefix": "app", "pid": os.getpid(), "alive": True,
        "started": "2026-09-07T00:00:00+00:00", "slots": [slot],
    }]
    assert document["harnesses"] == ["claude"]
    assert document["repos"] == [{
        "repo": "app", "prefix": None, "base": "trunk", "protected": "main",
        "test_cmd": "pytest -q", "area_owner": "me",
    }]


@_git_required
def test_status_json_alive_matches_pid_seam(home, wp):
    """`alive` 는 `_pid_alive` 와 같은 값이다 — 살아 있는 pid·죽은 pid·readonly pid 0 셋 다."""
    assert wp._pid_alive(_DEAD_PID) is False, "픽스처 전제: 이 pid 는 죽어 있어야 한다"
    slot_live = _make_slot(home, "app", 1, "live")
    slot_dead = _make_slot(home, "app", 2, "dead")
    slot_readonly = _make_slot(home, "app", 3, "readonly")
    _ledger(home, [
        {"slot": slot_live, "repo": "app", "session": "main", "pid": os.getpid(),
         "started": "s", "state": "leased", "test_cmd": None},
        {"slot": slot_dead, "repo": "app", "session": "gone", "pid": _DEAD_PID,
         "started": "s", "state": "leased", "test_cmd": None},
        {"slot": slot_readonly, "repo": "app", "session": "", "pid": 0,
         "started": "s", "state": "leased", "test_cmd": None, "role": "readonly"},
    ])

    rows = {row["slot"]: row for row in _status_json(home)["slots"]}

    assert rows[slot_live]["alive"] is True
    assert rows[slot_dead]["alive"] is False
    assert rows[slot_readonly]["alive"] is False
    for slot, row in rows.items():
        assert row["alive"] == wp._pid_alive(row["pid"]), slot


@_git_required
def test_status_json_normalizes_role_and_task_attribution(home):
    """장부에 `role` 키가 없는 행은 `work` · readonly 는 그대로 · task 귀속은 session 축."""
    work_slot = _make_slot(home, "app", 1, "w")
    readonly_slot = _make_slot(home, "app", 2, "r")
    _ledger(home, [
        {"slot": work_slot, "repo": "app", "session": "solo", "pid": 0,
         "started": "s", "state": "idle", "test_cmd": None},
        {"slot": readonly_slot, "repo": "app", "session": "", "pid": 0,
         "started": "s", "state": "leased", "test_cmd": None, "role": "readonly"},
    ], [{"name": "other", "prefix": None, "pid": 0, "started": "s"}])

    rows = {row["slot"]: row for row in _status_json(home)["slots"]}

    assert rows[work_slot]["role"] == "work"      # 장부 키 부재 → 기본값 정규화
    assert rows[readonly_slot]["role"] == "readonly"
    assert rows[work_slot]["task"] is None        # session 이 task 명이 아니다
    assert rows[readonly_slot]["session"] is None


@_git_required
def test_status_json_git_section_tracks_pm_home_checkout(home):
    """`git` 절은 PM 홈 checkout 자신 — clean 이면 dirty False, 파일 하나 놓으면 True."""
    _ledger(home, [])
    _write(home / ".gitignore", ".project_manager/\nwork/\n.repos/\n.origin/\n")
    _git(home, "add", "-A")
    _git(home, "commit", "-q", "-m", "tree")

    clean = _status_json(home)["git"]
    assert clean["branch"] == "main"
    assert clean["head"] == _git(home, "rev-parse", "HEAD").stdout.strip()
    assert clean["dirty"] is False

    _write(home / "README.md", "dirty\n")
    assert _status_json(home)["git"]["dirty"] is True


@_git_required
def test_status_json_git_section_is_null_outside_git(home):
    """git checkout 이 아니면 세 값 모두 null — 그 사실이 곧 신호다(추측 금지).

    상위 저장소로 탐색이 새면 남의 브랜치를 이 사본의 것으로 보고하게 되므로, `.git` 을 지운
    홈에 경계 env 를 주어 "조상도 없다" 형상을 만든다.
    """
    _ledger(home, [])
    remove_git_tree(home / ".git")
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(home.parent))

    done = _run_cli(home, "status", "--json", env=env)

    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["git"] == {"branch": None, "head": None, "dirty": None}


@_git_required
def test_status_json_git_section_handles_zero_commit_clone(home):
    """커밋 0 저장소(fresh clone 직후)는 브랜치만 있고 head 는 null — 정상 형상이다."""
    _ledger(home, [])
    remove_git_tree(home / ".git")
    _git(home, "init", "-q", "-b", "main")

    section = _status_json(home)["git"]

    assert section["branch"] == "main" and section["head"] is None
    assert section["dirty"] is True


# ── ② conf 다섯 목록 ─────────────────────────────────────────────────────────


@_git_required
def test_status_json_conf_five_lists_from_real_local_conf(home, pc):
    """필수 누락·폐지 키·미지 키·표준과 다른 값 — 실 local.conf 를 읽어 판정한다."""
    local_conf = _load("local_conf_status_json", "local_conf.py")
    required = local_conf.required_keys()
    assert required, "픽스처 전제: 레지스트리에 필수 키가 있어야 한다"
    legacy_key = next(iter(local_conf.LEGACY_KEY_MAP))
    standard_key, standard_spec = next(
        (key, spec) for key, spec in local_conf.KEY_REGISTRY.items()
        if spec.default is not None)
    _ledger(home, [])
    _write(home / ".project_manager" / "local.conf",
           f"{legacy_key}=x\n"
           f"{standard_key}={standard_spec.default}9\n"
           "no.such.key=1\n")

    conf = _status_json(home)["conf"]

    assert set(conf["missing_required"]) == set(required)
    assert {"key": legacy_key,
            "replacement": local_conf.LEGACY_KEY_MAP[legacy_key]} in conf["legacy"]
    assert "no.such.key" in conf["unknown"]
    assert {"key": standard_key, "value": f"{standard_spec.default}9",
            "default": standard_spec.default} in conf["nonstandard"]
    assert conf["missing_for_latest"] is None, "소스가 없으면 최신 기준 판정도 없다"


@_git_required
def test_status_json_missing_for_latest_reads_source_registry(home, tmp_path):
    """`--from` 소스의 레지스트리가 요구하는 필수 키 — "갱신하면 필요" 축."""
    source = _engine_home(tmp_path / "source")
    source_conf = source / ".project_manager" / "tools" / "local_conf.py"
    source_conf.write_text(
        source_conf.read_text(encoding="utf-8")
        + '\nKEY_REGISTRY["future.knob"] = KeySpec(\n'
          '    "다음 세대가 요구하는 키", default=None, required=True, group=KEY_GROUPS[0])\n',
        encoding="utf-8")
    _ledger(home, [])
    _write(home / ".project_manager" / "local.conf", "project.name=demo\n")

    conf = _status_json(home, "--from", str(source))["conf"]

    assert "future.knob" in conf["missing_for_latest"]
    assert "future.knob" not in conf["missing_required"], "이 사본의 선언은 그대로다"


# ── ③ --with-board ───────────────────────────────────────────────────────────


def _ticket(home: Path, status: str, ticket_id: str, title: str, **fields) -> Path:
    front = {
        "id": ticket_id, "title": title, "status": status, "created": "2026-09-07",
        "created_by": "me", "claimed_by": None, "claimed_at": None,
        "completed_at": None, "depends_on": [], "blocks": [], "touches": [],
        "estimate": "small", **fields,
    }
    lines = ["---"]
    for key, value in front.items():
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines += ["---", "", f"# {ticket_id} — {title}", "", "## 목표", "본문", ""]
    return _write(
        home / ".project_manager" / "wiki" / "tickets" / status / f"{ticket_id}.md",
        "\n".join(lines))


@_git_required
def test_with_board_counts_open_lint_and_gate_records(home):
    """counts 다섯 축·open 목록·lint 목록·회귀/라이브게이트 sidecar — 실 파일에서."""
    _ledger(home, [])
    _ticket(home, "open", "T-0001", "열린 것")
    _ticket(home, "done", "T-0002", "끝난 것", completed_at="2026-09-07")
    _ticket(home, "discarded", "T-0003", "처분된 것", disposition="dropped")
    _write(home / ".project_manager" / "wiki" / "tickets" / ".drafts" / "T-0004.md",
           "---\nid: T-0004\n---\n\n# 초안\n")
    local = home / ".project_manager" / ".local"
    _write_json(local / "regression.json",
                {"head": "abc123", "status": "pass", "rc": 0, "collected": 12345})
    _write_json(local / "livegate.json", {"head": "abc123", "status": "pass", "n": 18})

    board = _status_json(home, "--with-board")["board"]

    assert board["counts"] == {"open": 1, "claimed": 0, "blocked": 0,
                               "done": 1, "discarded": 1}, "초안은 세지 않는다"
    assert board["open"] == [{"id": "T-0001", "title": "열린 것", "claimed_by": None}]
    assert board["regression"]["collected"] == 12345
    assert board["livegate"]["n"] == 18
    assert isinstance(board["lint"], list)
    assert all({"kind", "message"} == set(row) for row in board["lint"])


@_git_required
def test_with_board_keeps_emitting_json_when_lint_has_issues(home):
    """lint 이슈(=`board lint` rc 1)가 있어도 JSON 산출은 계속된다 — 목록으로만 실린다."""
    _ledger(home, [])
    _ticket(home, "open", "T-0010", "의존성 깨진 것", depends_on=["T-9999"])

    board = _status_json(home, "--with-board")["board"]

    assert any(row["kind"] == "unknown" and "T-9999" in row["message"]
               for row in board["lint"]), f"lint 판정이 실려야 한다: {board['lint']}"
    assert board["counts"]["open"] == 1


def test_default_call_does_not_load_board_engine(pc, monkeypatch, tmp_path, capsys):
    """기본 호출은 board 를 **로드하지 않는다**(티켓 전수 스캔 비용을 지지 않는다)."""
    loaded: list[str] = []
    real_load = pc._load_module

    def spy(name, filename):
        loaded.append(name)
        return real_load(name, filename)

    monkeypatch.setattr(pc, "_load_module", spy)
    monkeypatch.setattr(pc, "_write_machine_line", lambda text: None)

    class _Wp:
        def list_leases(self):
            return []

        def list_tasks(self):
            return []

        def _pid_alive(self, pid):
            return False

    monkeypatch.setattr(pc, "_status_json_git",
                        lambda runner: {"branch": None, "head": None, "dirty": None})
    args = pc.build_parser().parse_args(["status", "--json"])
    assert pc.cmd_status(args, worktree_pool=_Wp()) == 0
    assert "board" not in loaded, f"기본 호출이 board 를 로드했다: {loaded}"


# ── ④ --with-adapters (실 템플릿 대조) ───────────────────────────────────────


def _adapter_home(root: Path) -> Path:
    """세 하네스 어댑터가 설치된 tmp 인스턴스 — config 4파일이 판정 대상이 된다."""
    home = _engine_home(root)
    _ledger(home, [])
    _write(home / "CLAUDE.md", "# 인스턴스 소유 문서\n")
    _write(home / "AGENTS.md", "# 인스턴스 소유 문서\n")
    (home / ".agents").mkdir(exist_ok=True)
    for relpath in (".claude/settings.json", ".opencode/opencode.jsonc",
                    ".codex/config.toml", ".codex/hooks.json"):
        source = TEMPLATES / _TEMPLATE_DIR[relpath] / relpath
        _write(home / relpath, source.read_text(encoding="utf-8"))
    _write_json(home / ".project_manager" / "install.json", {
        "harnesses": ["claude", "opencode", "codex"],
        "schema": 2,
        "instance_owned_templates": {
            relpath: {"source": f"templates/{_TEMPLATE_DIR[relpath]}/{relpath}",
                      "weight": "full"}
            for relpath in _TEMPLATE_DIR
        },
    })
    return home


_TEMPLATE_DIR = {
    ".claude/settings.json": "claude_code",
    ".opencode/opencode.jsonc": "opencode",
    ".codex/config.toml": "codex",
    ".codex/hooks.json": "codex",
    "CLAUDE.md": "claude_code",
    "AGENTS.md": "codex",
}


def _content_sha256(path: Path) -> str:
    text = path.read_bytes().decode("utf-8").replace("\r\n", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@_git_required
def test_with_adapters_reports_engine_status_vocabulary(tmp_path):
    """네 상태(in-sync·unedited·edited·unrecorded)가 엔진 어휘 그대로 실린다 — 실 파일 대조."""
    home = _adapter_home(tmp_path / "home")
    head = _git(REPO, "rev-parse", "HEAD").stdout.strip()
    # in-sync = 템플릿과 같은 내용(`.codex/hooks.json` 은 그대로 둔다).
    # unedited = 내용이 template 과 다르고 원장이 **지금 dest** 를 기록 → 상류가 바뀐 것.
    _write(home / ".codex/config.toml", "sandbox_mode = \"workspace-write\"\n")
    # edited = 원장이 다른 내용을 기록 → 채택자 편집(보존).
    _write(home / ".opencode/opencode.jsonc", "{\n  // 손편집\n  \"instructions\": []\n}\n")
    # unrecorded = 원장 항목 없음(.claude/settings.json).
    settings = home / ".claude/settings.json"
    _write(settings, settings.read_text(encoding="utf-8").replace(
        '"autoCompactEnabled": true', '"autoCompactEnabled": false'))
    _write_json(home / ".project_manager" / "adapter_baseline.json", {
        "schema": 1,
        "files": {
            ".codex/hooks.json": {"sha256": _content_sha256(home / ".codex/hooks.json"),
                                  "template_rev": head, "recorded_at": "2026-09-07"},
            ".codex/config.toml": {"sha256": _content_sha256(home / ".codex/config.toml"),
                                   "template_rev": head, "recorded_at": "2026-09-07"},
            ".opencode/opencode.jsonc": {"sha256": "0" * 64, "template_rev": head,
                                         "recorded_at": "2026-09-07"},
        },
    })

    adapters = _status_json(home, "--with-adapters", "--from", str(REPO))["adapters"]

    assert adapters["source"] == str(REPO.resolve())
    rows = {row["path"]: row for row in adapters["files"]}
    assert rows[".codex/hooks.json"]["status"] == "in-sync"
    assert rows[".codex/hooks.json"]["mode"] == "managed"
    assert rows[".codex/config.toml"]["status"] == "unedited"
    assert rows[".opencode/opencode.jsonc"]["status"] == "edited"
    assert rows[".claude/settings.json"]["status"] == "unrecorded"
    assert {row["mode"] for row in adapters["files"] if "mode" in row} <= {
        "managed", "report"}
    # 문서 파일은 채널이 없다 — 검사 없음이라는 사실만 한 줄로.
    assert rows["CLAUDE.md"] == {"path": "CLAUDE.md", "ownership": "instance",
                                 "status": "unchecked"}
    assert rows["AGENTS.md"]["status"] == "unchecked"


@_git_required
def test_with_adapters_feature_table_and_distance(tmp_path):
    """기능 표 — 없음·다름·폐지됨 판정과 `distance` 정의(같음이 아닌 행 수)."""
    home = _adapter_home(tmp_path / "home")
    settings = json.loads((home / ".claude/settings.json").read_text(encoding="utf-8"))
    settings.pop("autoCompactEnabled")                       # 없음(missing)
    settings["permissions"]["allow"] = ["Bash(ls:*)"]        # 다름(differs)
    settings["hooks"] = {"PostToolUse": [{"matcher": "*", "hooks": [
        {"type": "command", "command": "${CLAUDE_PROJECT_DIR}/.claude/run_tests_hook.sh"}]}]}
    _write_json(home / ".claude/settings.json", settings)

    adapters = _status_json(home, "--with-adapters", "--from", str(REPO))["adapters"]
    row = next(item for item in adapters["files"]
               if item["path"] == ".claude/settings.json")
    features = {feature["name"]: feature for feature in row["features"]}

    assert features["autoCompactEnabled"]["verdict"] == "missing"
    assert features["autoCompactEnabled"]["project"] is None
    assert features["autoCompactEnabled"]["latest"] is True
    assert features["permissions.allow"]["verdict"] == "differs"
    assert features["permissions.allow"]["project"] == 1
    assert features["permissions.allow"]["latest"] > 1
    assert features["hooks.PostToolUse"]["verdict"] == "retired"
    assert features["hooks.PostToolUse"]["project"] == ".claude/run_tests_hook.sh"
    assert features["hooks.PostToolUse"]["latest"] is None
    assert features["hooks.PreToolUse"]["verdict"] == "missing"
    assert row["distance"] == sum(1 for feature in row["features"]
                                  if feature["verdict"] != "same")
    assert row["distance"] >= 4


@_git_required
def test_with_adapters_diffs_against_baseline_rev_and_latest(tmp_path):
    """diff 둘 — 설치 기준(`template_rev` 시점)↔현재, 현재↔최신 template."""
    home = _adapter_home(tmp_path / "home")
    head = _git(REPO, "rev-parse", "HEAD").stdout.strip()
    _write_json(home / ".project_manager" / "adapter_baseline.json", {
        "schema": 1,
        "files": {".claude/settings.json": {"sha256": "0" * 64, "template_rev": head,
                                            "recorded_at": "2026-09-07"}},
    })
    settings = home / ".claude/settings.json"
    _write(settings, settings.read_text(encoding="utf-8").replace(
        '"autoCompactEnabled": true', '"autoCompactEnabled": false'))

    row = next(item for item in
               _status_json(home, "--with-adapters", "--from", str(REPO))["adapters"]["files"]
               if item["path"] == ".claude/settings.json")

    assert row["template_rev"] == head
    assert row["diff_lines"] == {"added": 1, "removed": 1}
    assert '-  "autoCompactEnabled": true' in row["diff_vs_baseline"]
    assert '+  "autoCompactEnabled": false' in row["diff_vs_baseline"]
    assert '+  "autoCompactEnabled": true' in row["diff_vs_latest"]


@_git_required
def test_with_adapters_resolves_url_upstream_via_cache(tmp_path):
    """URL upstream 은 `--from` 없이 T-0908 캐시로 해소되고 결과가 `--from <캐시>` 와 같다.

    픽스처는 로컬 bare + `file://` 라 네트워크가 0 이다(T-0908 헬퍼 재사용). 캐시 checkout 이
    소스가 됐음을 `adapters.source` 절대경로로 단언하고, 같은 캐시를 `--from` 으로 준 산출과
    문서 전체를 대조한다(`generated_at` 만 시각이라 제외).
    """
    framework = _make_framework_repo(tmp_path / "fw")
    # 상류 checkout 에는 최신 설정 레지스트리가 있다(`conf.missing_for_latest` 의 비교 기준).
    shutil.copyfile(TOOLS / "local_conf.py",
                    framework / ".project_manager" / "tools" / "local_conf.py")
    subprocess.run(["git", "-C", str(framework), "add", "-A"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(framework), "commit", "-qm", "registry"],
                   check=True, capture_output=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t.invalid",
                            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t.invalid"))
    url = _make_bare(framework, tmp_path / "fw.git")

    home = _engine_home(tmp_path / "home")
    _ledger(home, [])
    _write(home / ".project_manager" / "local.conf", f"upstream.path={url}\n")
    _write(home / ".codex" / "hooks.json",
           (framework / "templates" / "codex" / ".codex" / "hooks.json").read_text(
               encoding="utf-8"))
    _write(home / ".codex" / "config.toml",
           (framework / "templates" / "codex" / ".codex" / "config.toml").read_text(
               encoding="utf-8"))
    _write(home / "AGENTS.md", "# adopter\n")
    (home / ".agents").mkdir(exist_ok=True)
    _write_json(home / ".project_manager" / "install.json",
                {"schema": 2, "harnesses": ["codex"]})

    cached = _status_json(home, "--with-adapters")
    cache = _only_cache(home)

    assert cached["adapters"]["source"] == str(cache.resolve())
    assert {row["path"] for row in cached["adapters"]["files"]} >= {
        ".codex/config.toml", ".codex/hooks.json"}
    assert cached["conf"]["missing_for_latest"] is not None, \
        "캐시가 최신 레지스트리 비교 기준으로도 쓰여야 한다"

    explicit = _status_json(home, "--with-adapters", "--from", str(cache))

    cached.pop("generated_at"), explicit.pop("generated_at")
    assert cached == explicit, "캐시 해소 산출이 `--from <캐시>` 와 다르다"


@_git_required
def test_with_adapters_source_unresolved_errors(home):
    """소스 미해소 — rc≠0 이고 stdout 에 부분 JSON 이 0바이트(AT-003)."""
    _ledger(home, [])
    _write(home / ".project_manager" / "local.conf", "project.name=demo\n")
    _write_json(home / ".project_manager" / "install.json",
                {"harnesses": ["claude"], "schema": 2})
    _write(home / "CLAUDE.md", "# demo\n")
    (home / ".claude").mkdir(exist_ok=True)

    done = _run_cli(home, "status", "--json", "--with-adapters")

    assert done.returncode != 0
    assert done.stdout == "", f"부분 산출이 있으면 안 된다: {done.stdout!r}"
    assert "어댑터 config 소스 미해소" in done.stderr or "[중단]" in done.stderr


# ── ⑤ 부작용 0 · 텍스트 불변 · 표면 상한 ─────────────────────────────────────


def _tree_digest(root: Path) -> str:
    """`.project_manager/` 트리의 경로+내용 해시 — 생성·수정·삭제를 모두 잡는다."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if "__pycache__" in path.parts:
            continue
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(b"\0")
        if path.is_file():
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


@_git_required
def test_json_path_writes_nothing(tmp_path):
    """JSON 경로가 `.project_manager/` 아래 어떤 파일도 만들지 않고 고치지 않는다(AT-004)."""
    home = _adapter_home(tmp_path / "home")
    slot = _make_slot(home, "app", 1, "feat")
    _ledger(home, [{"slot": slot, "repo": "app", "session": "main", "pid": os.getpid(),
                    "started": "s", "state": "leased", "test_cmd": None}])
    _ticket(home, "open", "T-0001", "열린 것")
    # 텍스트 경로를 먼저 한 번 돌려 공유 읽기 seam(리스 락 파일)을 만든 상태를 기준으로 삼는다 —
    # JSON 이 **텍스트 경로보다 더** 쓰지 않음을 재는 것이 이 단언의 축이다.
    assert _run_cli(home, "status").returncode == 0
    before = _tree_digest(home / ".project_manager")

    _status_json(home)
    _status_json(home, "--with-board")
    _status_json(home, "--with-adapters", "--from", str(REPO))

    assert _tree_digest(home / ".project_manager") == before, \
        "JSON 조회가 PM 홈 .project_manager/ 트리를 건드렸다"


@_git_required
def test_text_path_output_is_byte_identical(home):
    """텍스트 경로 골든 — `--json` 추가가 사람 출력의 바이트를 바꾸지 않는다(불변식 1)."""
    _ledger(home, [])
    _write(home / ".project_manager" / "local.conf", "identity.user=me@example.invalid\n")

    done = _run_cli(home, "status")

    assert done.returncode == 0
    assert done.stdout == (
        "# pm-config status — 세션: (비바인딩)\n"
        "## 정체성(user): me@example.invalid\n"
        "## 세션격리(registry/area_owner 기준): single-user (단일/미등록 registry) — 단, 티켓 "
        "귀속(created_by/claimed_by)이 다중이면 세션 뷰가 strict-exclude 될 수 있다. 실 격리는 "
        "`board list --mine`(strict-exclude loud-warn)이 authoritative.\n"
        "## 이 세션의 리스: (없음)\n"
        "## task 상황 (사람이 명명한 작업스트림·slot-모드 세션 제외):\n"
        "  (task 없음 — 명명한 task 미등록·세션은 아래 풀 축에만 나타남)\n"
        "## slot 풀 (풀 전체 리스 장부 · 슬롯별 state·보유 task·role + git 요약):\n"
        "  (리스 없음 — 아직 worktree 슬롯이 생성되지 않음)\n"
    )


@_git_required
@pytest.mark.parametrize("flag", ["--with-board", "--with-adapters"])
def test_scope_flags_without_json_are_usage_error(home, flag):
    """`--with-*` 를 `--json` 없이 주면 usage error(rc 2) — 조용히 무시하지 않는다."""
    _ledger(home, [])
    done = _run_cli(home, "status", flag)
    assert done.returncode == 2
    assert flag in done.stderr and "--json" in done.stderr
    assert done.stdout == ""


@_git_required
def test_from_without_json_is_usage_error(home):
    """`--from` 도 `--json` 전용이다 — 텍스트 경로엔 그 소스를 쓰는 절이 없다."""
    _ledger(home, [])
    done = _run_cli(home, "status", "--from", str(REPO))
    assert done.returncode == 2
    assert "--from" in done.stderr


def test_whoami_has_no_json_flag(pc):
    """`--json` 은 `status` 전용 — `whoami --json` 은 파서가 거부한다(계약 하나)."""
    with pytest.raises(SystemExit) as excinfo:
        pc.build_parser().parse_args(["whoami", "--json"])
    assert excinfo.value.code == 2
    assert pc.build_parser().parse_args(["status", "--json"]).json is True


def test_status_json_schema_constant_is_single_source(pc):
    """스키마 번호는 상수 하나다 — 문서의 값이 그 상수와 같아야 한다."""
    assert pc.STATUS_JSON_SCHEMA == 1
