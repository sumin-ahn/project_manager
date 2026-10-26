"""새 프로젝트 적용 브리지 — `pm_import --new` → `pm-config init` → 등록 (T-0912 AT-001).

전부 **실물**로 잰다: 번들 소스는 이 트리 자신이고, 만들어지는 PM 홈도 실 엔진 산출이며,
upstream 값은 실 로컬 bare 다(네트워크 0). 가짜 runner 로 rc 를 지어내지 않는다.

축 여섯:
  1. 세 단계가 순서대로 돌고, 성공한 단계까지의 로그가 항상 돌아온다.
  2. `--upstream` 은 항상 명시된다 — 그 값이 새 프로젝트의 갱신 경로로 그대로 박힌다.
  3. `--user-ack` 는 확인 대화의 반환값에서만 온다(관리자가 스스로 붙이는 자리 0).
  4. 등록은 적용이 rc 0 이고 설치 기록이 있을 때만 한다 — 실패는 등록부를 건드리지 않는다.
  5. 폴더·upstream 이 비면 서브프로세스를 띄우기 전에 거부한다.
  6. 대화의 기본값(하네스 목록·upstream)은 번들 엔진에서 온다(손으로 박은 상수 0).
"""
from __future__ import annotations

from pm_engine.domain import install_rules

import inspect
import json
import subprocess
import sys
from pathlib import Path

import pytest

from _manager_home import git_required, make_bare_with_tags

from manager import bridge_update, registry, runner
from pm_engine.infra import upstream_git
from manager.bridge import Api
from manager.bridge_update import bundle_root, import_argv, init_argv

pytestmark = git_required

REPO = Path(__file__).resolve().parents[1]

# 적용 한 번을 실 서브프로세스로 돌린다 — 화면(Api)이 부르는 것과 같은 진입이다.
_APPLY_SOURCE = '''\
import sys

sys.path.insert(0, sys.argv[1])          # 이 repo 루트 — `manager` 패키지가 여기 있다.
from manager.bridge import Api
from pathlib import Path

dest, upstream, registry_file = sys.argv[2], sys.argv[3], sys.argv[4]
api = Api(folder_picker=lambda: None, registry_file=registry_file)
response = api.apply_new(dest, "claude", "applied", "", "", "", upstream, "")
raise SystemExit(0 if response.get("ok") else 1)
'''


# 적용 한 번의 단계 이름 — 화면의 진행 로그가 이 순서를 그대로 그린다.
STEP_ORDER = ["import", "init", "register"]
# 확인 대화를 지난 prefix 하나 — 승인 토큰은 이 값과 정확히 같아야 한다.
PREFIX = "pay"


@pytest.fixture(scope="module")
def upstream(tmp_path_factory) -> Path:
    """실 로컬 bare — `--upstream` 으로 넘길 값이자 그 프로젝트의 갱신 경로다."""
    return make_bare_with_tags(tmp_path_factory.mktemp("apply-upstream"))


@pytest.fixture
def registry_file(tmp_path) -> Path:
    return tmp_path / "config" / "projects.json"


@pytest.fixture
def api(registry_file, tmp_path, monkeypatch) -> Api:
    from _manager_home import use_known_ctx_env
    use_known_ctx_env(monkeypatch, tmp_path / "known-ctx-native")
    return Api(folder_picker=lambda: None, registry_file=registry_file)


@pytest.fixture
def dest(tmp_path) -> Path:
    return tmp_path / "applied"


def _apply(api: Api, dest: Path, upstream: Path, *, harness: str = "claude",
           name: str = "applied", prefix: str = "", area: str = "", owner: str = "",
           prefix_ack: str = "") -> dict:
    """화면이 넘기는 일곱 값 + 확인 반환값 그대로 부른다."""
    return api.apply_new(str(dest), harness, name, prefix, area, owner, str(upstream), prefix_ack)


def _conf_values(pm_home: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    text = (pm_home / ".project_manager" / "local.conf").read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _, value = stripped.partition("=")
            values[key.strip()] = value.strip()
    return values


# ── ① 세 단계 · 순서 · 로그 ──────────────────────────────────────────────────


def test_apply_new_runs_import_then_init_then_register(api, dest, upstream, registry_file):
    response = _apply(api, dest, upstream)

    assert [step["name"] for step in response["steps"]] == STEP_ORDER
    assert [step["rc"] for step in response["steps"]] == [0, 0, 0]
    assert response["ok"] is True
    assert response["path"] == str(dest)
    assert set(response) == {"steps", "ok", "path"}   # 소비처 없는 필드를 두지 않는다
    assert (dest / ".project_manager" / "install.json").is_file()
    assert registry.load(registry_file) == [dest]


def test_the_step_log_carries_the_engines_own_lines(api, dest, upstream):
    response = _apply(api, dest, upstream)

    steps = {step["name"]: step for step in response["steps"]}
    assert "✓ import 완료" in steps["import"]["stdout"]
    assert "init 완료" in steps["init"]["stdout"]
    assert str(dest) in steps["register"]["stdout"]


def test_the_applied_project_answers_the_manager_and_joins_the_list(
        api, dest, upstream, tmp_path, registry_file):
    """적용 직후 프로젝트가 목록에 들어오고, 작업 폴더 목록은 **비어 있다**.

    적용은 작업 폴더를 만들지 않는다(T-0919 — 홈 자신을 작업 폴더로 등재하던 경로 삭제).
    폴더는 사람이 `pm-config worktree add` 로 만들고, 그 전까지 화면의 슬롯 표는 빈 표다.
    """
    applier = tmp_path / "apply.py"
    applier.write_text(_APPLY_SOURCE, encoding="utf-8")

    done = subprocess.run(
        [sys.executable, str(applier), str(REPO), str(dest), str(upstream), str(registry_file)],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert done.returncode == 0, done.stderr

    assert api.list_projects()["projects"] == [{"path": str(dest), "name": dest.name}]
    document = api.status(str(dest))
    assert document["slots"] == [], (
        f"적용이 작업 폴더 행을 지어냈다: {document['slots']}")


def test_the_harness_choice_reaches_the_engine(api, dest, upstream):
    _apply(api, dest, upstream, harness="codex")

    receipt = json.loads((dest / ".project_manager" / "install.json").read_text(encoding="utf-8"))
    assert receipt["harnesses"] == ["codex"]


# ── ② `--upstream` 은 항상 명시된다 ──────────────────────────────────────────


def test_the_recorded_upstream_is_the_value_we_passed(api, dest, upstream):
    """생략하면 엔진이 `--from`(번들)에서 값을 도출한다 — 번들에서는 그것이 임시 경로다."""
    _apply(api, dest, upstream)

    values = _conf_values(dest)
    assert values["upstream.path"] == str(upstream)
    assert values["upstream.path"] != str(bundle_root())


def test_the_import_command_always_carries_upstream_and_the_bundle_source(dest, upstream):
    argv = import_argv(dest, "claude", "applied", str(upstream))
    prefix = runner.interpreter(dest)

    assert argv[argv.index("--upstream") + 1] == str(upstream)
    assert argv[argv.index("--from") + 1] == str(bundle_root())
    assert argv[argv.index("--fill") + 1] == "manual"
    assert tuple(argv[:len(prefix)]) == prefix
    assert argv[len(prefix)] == str(bundle_root() / ".project_manager" / "tools" / "pm_import.py")
    assert argv[len(prefix) + 1] == "--new"


def test_there_is_no_second_place_that_builds_an_import_call():
    """`--upstream` 을 빠뜨릴 수 있는 두 번째 호출 경로가 없어야 한다."""
    source = Path(bridge_update.__file__).read_text(encoding="utf-8")

    assert source.count('"--new"') == 1
    assert '"--upstream"' in inspect.getsource(bridge_update.import_argv)


def test_an_empty_upstream_is_refused_before_any_subprocess(api, dest):
    response = api.apply_new(str(dest), "claude", "applied", "", "", "", "  ", "")

    assert response["error"]["kind"] == "invalid-path"
    assert response["error"]["rc"] == 2
    assert not dest.exists()


def test_an_empty_folder_choice_is_refused(api, upstream):
    response = api.apply_new("", "claude", "applied", "", "", "", str(upstream), "")

    assert response["error"]["rc"] == 2
    assert "폴더" in response["error"]["message"]


# ── ③ prefix 승인 토큰 ───────────────────────────────────────────────────────


def test_a_prefix_without_the_matching_confirmation_never_reaches_the_engine(api, dest, upstream):
    for ack in ("", "other", PREFIX.upper()):
        response = _apply(api, dest, upstream, prefix=PREFIX, prefix_ack=ack)

        assert response["error"]["kind"] == "invalid-path"
        assert response["error"]["rc"] == 2
        assert not dest.exists(), f"승인 값 {ack!r} 인데 서브프로세스가 떴다"


def test_the_init_command_carries_no_ack_when_there_is_no_prefix(dest):
    argv = init_argv(dest, "", "", "", "")

    assert "--prefix" not in argv and "--user-ack" not in argv
    assert argv[-1] == "init"


def test_the_ack_value_comes_from_the_confirmation_not_from_the_prefix_field(dest):
    argv = init_argv(dest, PREFIX, "결제", "me", PREFIX)

    assert argv[argv.index("--user-ack") + 1] == PREFIX
    assert argv[argv.index("--area") + 1] == "결제"
    assert argv[argv.index("--owner") + 1] == "me"


def test_the_engine_refuses_a_new_prefix_without_the_ack_in_its_own_words(api, dest, upstream):
    """관리자가 승인 토큰을 나르는 이유 — 그 게이트가 엔진에 실재한다(실 엔진 실행)."""
    _apply(api, dest, upstream)

    refused = subprocess.run(
        [sys.executable, str(dest / ".project_manager" / "tools" / "pm_config.py"),
         "init", "--prefix", PREFIX],
        cwd=str(dest), capture_output=True, text=True, encoding="utf-8", errors="replace")

    assert refused.returncode == 1
    assert refused.stderr.startswith(
        f"[중단] board init --prefix: 새 prefix '{PREFIX}' 신설에는 사용자 명시 승인이 필요하다.")
    assert f"--user-ack {PREFIX}" in refused.stderr


def test_a_confirmed_prefix_registers_the_category(api, dest, upstream):
    response = _apply(api, dest, upstream, prefix=PREFIX, area="결제", owner="me",
                      prefix_ack=PREFIX)

    assert response["ok"] is True, response["steps"]
    init_step = next(step for step in response["steps"] if step["name"] == "init")
    assert f"카테고리 {PREFIX}" in init_step["stdout"]
    assert "[승인 감사] board init --prefix" in init_step["stdout"]


# ── ④ 실패는 등록부를 건드리지 않는다 ────────────────────────────────────────


def test_a_failed_apply_leaves_the_registry_untouched(api, dest, upstream, registry_file):
    dest.mkdir(parents=True)
    (dest / "already-here.txt").write_text("사람이 쓰던 폴더\n", encoding="utf-8")

    response = _apply(api, dest, upstream)

    assert [step["name"] for step in response["steps"]] == ["import"]
    assert response["steps"][0]["rc"] != 0
    assert "비어있지 않습니다" in response["steps"][0]["stderr"]
    assert response["ok"] is False
    assert response["path"] is None
    assert set(response) == {"steps", "ok", "path"}
    assert not registry_file.exists()
    assert (dest / "already-here.txt").read_text(encoding="utf-8") == "사람이 쓰던 폴더\n"


def test_registration_needs_the_install_receipt(api, dest, registry_file):
    """rc 0 만으로 등록하지 않는다 — 설치 기록이 없으면 적용이 끝나지 않은 트리다."""
    (dest / ".project_manager").mkdir(parents=True)

    step = api._register(dest)

    receipt = dest / ".project_manager" / "install.json"
    assert step == {"name": "register", "rc": 1, "stdout": "",
                    "stderr": f"설치 기록이 없어 등록하지 않았다: {receipt}"}
    assert not registry_file.exists()


# ── ⑤ 대화의 기본값은 번들 엔진에서 온다 ────────────────────────────────────


def test_the_dialog_defaults_come_from_the_bundle_engine(api):
    defaults = api.apply_defaults()

    assert defaults["harnesses"] == list(install_rules.REGISTERED_HARNESSES)
    assert defaults["upstream"] == upstream_git.derive_origin_url(bundle_root())
    assert defaults["upstream"], "번들 트리의 origin 을 읽지 못했다 — 화면 필드가 빈다"
