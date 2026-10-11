"""갱신 탭 브리지 — `pm-config update` 미리보기·적용 (T-0912 AT-002).

실 `pm_import --new` PM 홈, 로컬 clone 상류, 서브프로세스로 갱신과 소스 보존을 검증한다.
T-0927의 구버전 경계는 고정 v1.7.13 원본으로 검증한다. 호출 상한과 스폰·timeout 등
실패 분기는 대역으로 주입해 실행 횟수와 출력 보존을 확인한다.

축 여섯:
  1. 미리보기는 엔진 출력 원문을 한 글자도 바꾸지 않고 넘긴다(요약·파싱 0).
  2. 소스는 사본이 자기 `upstream.path` 로 해소한다 — 관리자는 `--from` 을 주지 않는다.
  3. 미리보기는 프로젝트 폴더를 한 바이트도 건드리지 않는다.
  4. 적용은 그 사본의 `engine_rev` 를 전진시킨다.
  5. 실패는 rc 와 엔진 원문 그대로 오고, 목록의 마지막 성공값은 살아 있다.
  6. 관리자 코드에는 엔진 파일을 복사하는 자리가 없다(쓰기는 등록부 하나뿐).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from _manager_home import (
    git_required, install_old_generation_engine, make_committed_source, make_pm_home,
    make_upstream_checkout, set_conf_key, tree_snapshot,
)

from manager import engine_rev
from manager.bridge import Api
from manager.bridge_update import update_argv
from manager import CallError, bridge_update
from test_upgrade_adopter_e2e import (
    t0927_legacy_source, t0927_upstream, t0927_installed_adopter, t0927_old_adopter,
)

REPO = Path(__file__).resolve().parents[1]

pytestmark = git_required

# 상류가 다는 릴리즈 태그 = 그 상류 엔진의 rev. 갱신 축 전체가 이 한 값으로 묶인다.
UPSTREAM_REV = "v9.9.9"
# 관리자 패키지에서 파일을 만들거나 복사하는 API — 등록부(`registry.py`) 밖에 있으면 red.
FILE_WRITING_APIS = ("copy", "copy2", "copyfile", "copytree", "copyfileobj",
                     "write_text", "write_bytes", "mkdir", "unlink")
REGISTRY_OWNED_MODULE = "registry.py"
# 판정 대상은 **번들에서 도는 코드**다. `build.py` 는 실행 파일을 만드는 자리(빌드 시각·번들 밖)라
# 각인 모듈 `manager/_build_info.py` 를 쓴다 — 그 파일이 번들에 실리지 않는 것은
# tests/test_manager_bundle_layout.py 가 잰다.
BUILD_TIME_MODULE = "build.py"
# 엔진이 소유한 per-clone 스크래치(락 파일·임시 트리) — git-ignored 이고 명령마다 생겼다 사라진다.
# "갱신이 무엇을 남겼는가" 는 이 축을 걷어낸 트리에서만 말할 수 있다.
ENGINE_SCRATCH_ROOT = ".project_manager/.local"
# 엔진이 갱신을 예고하는 두 채널 — byte-copy 와 토큰 치환(@render). 둘 다 예고다.
ANNOUNCE_PREFIXES = ("[update] ", "[render] ")


@pytest.fixture(scope="module")
def source(tmp_path_factory) -> Path:
    """이 트리의 커밋본 — 홈 설치와 상류 clone 이 같은 커밋에서 출발한다."""
    return make_committed_source(tmp_path_factory.mktemp("update-source"))


@pytest.fixture(scope="module")
def upstream(tmp_path_factory, source) -> Path:
    """엔진 rev 를 올려 둔 실 clone — 갱신할 것이 있는 상류다."""
    return make_upstream_checkout(tmp_path_factory.mktemp("update-upstream"), source=source,
                                  rev=UPSTREAM_REV)


@pytest.fixture(scope="module")
def base_home(tmp_path_factory, source, upstream) -> Path:
    """실 엔진이 만든 PM 홈 한 벌 — 테스트마다 복사해 쓴다(원본은 읽기만)."""
    return make_pm_home(tmp_path_factory.mktemp("update-base") / "demo", source=source,
                        upstream=upstream)


@pytest.fixture
def pm_home(base_home, tmp_path) -> Path:
    home = tmp_path / "demo"
    shutil.copytree(base_home, home, symlinks=True)
    return home


@pytest.fixture
def api(tmp_path, monkeypatch) -> Api:
    from _manager_home import use_known_ctx_env
    use_known_ctx_env(monkeypatch, tmp_path / "known-ctx-native")
    return Api(folder_picker=lambda: None, registry_file=tmp_path / "config" / "projects.json")


def _durable(snapshot: dict[str, str]) -> dict[str, str]:
    """엔진 스크래치를 걷어낸 트리 스냅샷."""
    return {name: digest for name, digest in snapshot.items()
            if not name.startswith(ENGINE_SCRATCH_ROOT)}


def _direct_update(pm_home: Path, *args: str) -> subprocess.CompletedProcess:
    """관리자를 거치지 않고 같은 명령을 직접 부른다 — 대조할 두 번째 눈."""
    from _manager_home import known_ctx_env
    return subprocess.run(
        [sys.executable, str(pm_home / ".project_manager" / "tools" / "pm_config.py"),
         "update", *args],
        cwd=str(pm_home), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=known_ctx_env(native_bin=pm_home.parent / "known-ctx-native"))


# ── ① 미리보기는 엔진 출력 원문이다 ──────────────────────────────────────────


def test_update_preview_hands_over_the_engine_output_unchanged(api, pm_home):
    """요약하거나 구조로 바꾸지 않는다 — 직접 부른 것과 세 값이 모두 같아야 한다."""
    direct = _direct_update(pm_home, "--dry-run")

    response = api.update_preview(str(pm_home))

    assert response["rc"] == direct.returncode
    assert response["stdout"] == direct.stdout
    assert response["stderr"] == direct.stderr
    assert set(response) == {"rc", "stdout", "stderr"}


def test_update_preview_shows_the_pending_engine_files_in_the_engines_own_words(api, pm_home):
    direct = _direct_update(pm_home, "--dry-run")
    assert "[dry-run]" in direct.stdout, direct.stdout      # 상류가 앞서 있는 형상인지 먼저 확인

    stdout = api.update_preview(str(pm_home))["stdout"]

    assert "[update] .project_manager/tools/engine_rev.py" in stdout
    assert "변경 예정" in stdout


# ── ② 소스는 사본이 해소한다 (`--from` 미부착) ───────────────────────────────


def test_the_update_command_never_carries_a_from_flag(pm_home):
    """`--from` 을 붙이면 관리자 번들이 소스가 되어 사본의 upstream 이 무의미해진다."""
    for dry_run in (True, False):
        argv = update_argv(pm_home, dry_run=dry_run)
        assert "--from" not in argv
        assert argv[-1] == ("--dry-run" if dry_run else "update")


def test_the_source_is_the_copys_own_upstream_not_the_manager_bundle(api, pm_home):
    """번들(= 이 트리)이 소스였다면 rev 가 같아 '변경 없음' 이 나온다 — 나오면 red 다."""
    assert engine_rev() != UPSTREAM_REV, "픽스처 전제 붕괴 — 상류와 번들의 rev 가 같다"

    stdout = api.update_preview(str(pm_home))["stdout"]

    assert "최신 — 변경 없음" not in stdout
    assert "[update] .project_manager/tools/board.py" in stdout


# ── ③ 미리보기는 아무것도 쓰지 않는다 ────────────────────────────────────────


def test_update_preview_touches_no_file_in_the_project(api, pm_home):
    api.status(str(pm_home))                                   # 공유 읽기 seam 을 먼저 세운다
    before = _durable(tree_snapshot(pm_home))

    api.update_preview(str(pm_home))

    assert _durable(tree_snapshot(pm_home)) == before


# ── ④ 적용은 engine_rev 를 전진시킨다 ────────────────────────────────────────


def test_update_apply_moves_the_copys_engine_rev_forward(api, pm_home):
    assert api.status(str(pm_home))["engine_rev"] == engine_rev()

    response = api.update_apply(str(pm_home))

    assert response["rc"] == 0, response["stderr"]
    assert "파일 동기화" in response["stdout"]
    assert api.status(str(pm_home))["engine_rev"] == UPSTREAM_REV


def test_the_latest_tag_axis_reads_the_same_upstream(api, pm_home, upstream):
    """"갱신 있음" 판정의 두 입력(사본 rev · 상류 최신 태그)이 같은 상류에서 온다."""
    api.add_project(str(pm_home))

    tags = api.latest_tags()

    assert tags["projects"][str(pm_home)]["upstream"] == str(upstream)
    assert tags["upstreams"][str(upstream)] == {"tag": UPSTREAM_REV}


# ── ⑤ 실패는 원문 그대로 · 마지막 값은 살아 있다 ─────────────────────────────


def test_a_failed_update_comes_back_word_for_word(api, pm_home, tmp_path):
    missing = tmp_path / "no-such-checkout"
    set_conf_key(pm_home, "upstream.path", str(missing))

    response = api.update_apply(str(pm_home))

    assert response["rc"] == 1
    assert response["stderr"].startswith(
        f"오류: upstream 경로가 디렉토리가 아니거나 존재하지 않음: {missing}")


def test_a_failed_update_keeps_the_last_status_value(api, pm_home, tmp_path):
    """갱신이 실패해도 목록이 비지 않는다 — 현황은 여전히 그 사본이 답한다."""
    set_conf_key(pm_home, "upstream.path", str(tmp_path / "no-such-checkout"))

    assert api.update_apply(str(pm_home))["rc"] != 0

    assert api.status(str(pm_home))["engine_rev"] == engine_rev()


def test_an_old_generation_copy_answers_with_its_own_refusal(api, pm_home):
    """엔진이 `update` 를 모르면 그 rc 와 문구가 그대로 온다(관리자가 다시 쓰지 않는다)."""
    install_old_generation_engine(pm_home)

    response = api.update_preview(str(pm_home))

    assert response["rc"] == 2
    assert "update" in response["stderr"]


def test_a_path_that_is_not_a_pm_home_is_an_error_dict(api, tmp_path):
    response = api.update_apply(str(tmp_path / "nowhere"))

    assert response["error"]["kind"] == "invalid-path"


# ── ⑥ 관리자는 엔진 파일을 복사하지 않는다 ───────────────────────────────────


def test_the_manager_package_has_no_file_copying_code():
    """갱신도 적용도 전부 서브프로세스다 — 파일을 만드는 자리는 등록부 하나뿐이다."""
    offenders: dict[str, list[str]] = {}
    for module in sorted((REPO / "manager").glob("*.py")):
        if module.name in (REGISTRY_OWNED_MODULE, BUILD_TIME_MODULE):
            continue
        source = module.read_text(encoding="utf-8")
        found = [f"{api_name}(" for api_name in FILE_WRITING_APIS
                 if f".{api_name}(" in source]
        if found:
            offenders[module.name] = found

    assert offenders == {}


def test_the_update_call_only_changes_what_the_engine_announced(api, pm_home):
    """적용이 바꾸는 것은 엔진이 바꾼다고 말한 그 파일들이다(관리자가 더 손대지 않는다)."""
    announced = {line.split(prefix, 1)[1].strip()
                 for line in api.update_preview(str(pm_home))["stdout"].splitlines()
                 for prefix in ANNOUNCE_PREFIXES if prefix in line}
    assert announced, "상류가 앞서 있지 않다 — 픽스처 전제 붕괴"
    before = _durable(tree_snapshot(pm_home))

    assert api.update_apply(str(pm_home))["rc"] == 0

    after = _durable(tree_snapshot(pm_home))
    changed = {name for name in set(before) | set(after) if before.get(name) != after.get(name)}
    # 엔진이 예고한 파일 + 엔진 자신이 기록하는 baseline(local.conf) 밖은 없어야 한다.
    assert changed <= announced | {".project_manager/local.conf"}, changed


@pytest.mark.parametrize("preview, count", [(True, 1), (False, 2)])
def test_t0927_apply_finalizes_fixed_process_count(api, tmp_path, monkeypatch, preview, count):
    (tmp_path / ".project_manager").mkdir()
    calls = []
    interpreters = []

    def interpreter(home):
        interpreters.append(home)
        return [f"python-{len(interpreters)}"]

    def run(home, argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, f"출력 {len(calls)}\n", "")

    monkeypatch.setattr(bridge_update.runner, "interpreter", interpreter)
    monkeypatch.setattr(bridge_update, "_run", run)
    response = (api.update_preview if preview else api.update_apply)(str(tmp_path))
    assert response["rc"] == 0
    assert len(calls) == len(interpreters) == count
    assert all("--from" not in argv for argv in calls)
    assert all(("--dry-run" in argv) == preview for argv in calls)
    if not preview:
        assert [step["name"] for step in response["steps"]] == ["RUN1", "RUN2"]
        assert calls[1][0] == "python-2"


def test_t0927_apply_finalizes_actual_v1713(api, t0927_old_adopter, monkeypatch):
    original_run = bridge_update._run
    calls = []

    def run(home, argv, **kwargs):
        calls.append(argv)
        return original_run(home, argv, **kwargs)

    monkeypatch.setattr(bridge_update, "_run", run)
    response = api.update_apply(str(t0927_old_adopter))
    assert response["rc"] == 0, response
    text = (t0927_old_adopter / ".project_manager/local.conf").read_text(encoding="utf-8")
    assert "ctx.stop_pct=" not in text
    assert "ctx.precompact_at_pct=90" in text
    assert len(calls) == 2
    assert all("--from" not in argv for argv in calls)
    assert [step["name"] for step in response["steps"]] == ["RUN1", "RUN2"]


def test_t0927_apply_finalizes_v1713_original_defaults(api, t0927_installed_adopter):
    conf = t0927_installed_adopter / ".project_manager/local.conf"
    original = conf.read_text(encoding="utf-8")
    assert "ctx.nudge_pct=30" in original
    assert "ctx.stop_pct=20" in original
    assert "ctx.compact_at_pct=" not in original
    response = api.update_apply(str(t0927_installed_adopter))
    assert response["rc"] == 0, response
    assert len(response["steps"]) == 2
    text = conf.read_text(encoding="utf-8")
    assert "ctx.stop_pct=" not in text
    assert "ctx.nudge_pct=" not in text
    assert "ctx.checkpoint_at_pct=70" in text
    assert "ctx.precompact_at_pct=80" in text


def test_t0927_failure_actual_run2_keeps_run1_baseline(api, t0927_old_adopter,
                                                     t0927_upstream, monkeypatch):
    from _manager_home import _git
    home = t0927_old_adopter
    set_conf_key(home, "ctx.compact_at_pct", "90")
    original_run = bridge_update._run
    observed = []

    def run(home, argv, **kwargs):
        result = original_run(home, argv, **kwargs)
        observed.append((result.returncode,
                         (home / ".project_manager/local.conf").read_text(encoding="utf-8")))
        return result

    monkeypatch.setattr(bridge_update, "_run", run)
    response = api.update_apply(str(home))
    assert len(observed) == 2
    assert observed[0][0] == 0
    assert response["rc"] != 0
    assert response["failed_step"] == "RUN2"
    assert "ctx.stop_pct=10" in observed[0][1]
    assert "ctx.precompact_at_pct=90" in observed[1][1]
    baseline = _git("-C", str(t0927_upstream), "rev-parse", "HEAD").stdout.strip()
    assert f"upstream.rev={baseline}" in observed[0][1]
    assert f"upstream.rev={baseline}" in observed[1][1]
    assert "ctx.compact_at_pct=90" in response["steps"][1]["stderr"]


@pytest.mark.parametrize("failed_run", [1, 2])
@pytest.mark.parametrize("failure", ["rc", "spawn", "timeout", "skew", "remaining-key"])
def test_t0927_failure_keeps_stage_and_original_output(api, tmp_path, monkeypatch,
                                                     failed_run, failure):
    (tmp_path / ".project_manager").mkdir()
    calls = []

    def run(home, argv, **kwargs):
        calls.append(argv)
        stage = len(calls)
        if stage == failed_run and failure in ("spawn", "timeout"):
            raise CallError(failure, "실행 실패", stderr="실패 원문", rc=None)
        failed = stage == failed_run
        stderr = {"skew": "엔진 사본 rev 혼합", "remaining-key": "ctx.stop_pct 잔류"}.get(
            failure, "실패 원문") if failed else f"경고 {stage}\n"
        return subprocess.CompletedProcess(argv, 7 if failed else 0, f"출력 {stage}\n", stderr)

    monkeypatch.setattr(bridge_update, "_run", run)
    response = api.update_apply(str(tmp_path))
    assert len(calls) == failed_run
    assert response["failed_step"] == f"RUN{failed_run}"
    if failure in ("spawn", "timeout"):
        assert response["error"]["kind"] == failure
        assert response["error"]["stderr"] == "실패 원문"
        assert len(response["steps"]) == failed_run - 1
    else:
        assert response["rc"] == 7
        assert len(response["steps"]) == failed_run
        assert response["steps"][-1]["stdout"] == f"출력 {failed_run}\n"
        assert response["steps"][-1]["stderr"] in response["stderr"]
    if failed_run == 2:
        assert response["steps"][0] == {
            "name": "RUN1", "rc": 0, "stdout": "출력 1\n", "stderr": "경고 1\n"}


def test_t0927_failure_timeout_retains_partial_run2_output(api, tmp_path, monkeypatch):
    (tmp_path / ".project_manager").mkdir()
    calls = []

    def run(home, argv, **kwargs):
        calls.append(argv)
        if len(calls) == 1:
            return subprocess.CompletedProcess(argv, 0, "배달 완료\n", "경고 원문\n")
        cause = subprocess.TimeoutExpired(argv, 1, output="중간 출력\n".encode(),
                                          stderr="중간 오류\n".encode())
        raise CallError("timeout", "응답 없음") from cause

    monkeypatch.setattr(bridge_update, "_run", run)
    response = api.update_apply(str(tmp_path))
    assert len(calls) == 2
    assert response["failed_step"] == "RUN2"
    assert response["steps"][0]["stdout"] == "배달 완료\n"
    assert response["steps"][1] == {
        "name": "RUN2", "rc": None, "stdout": "중간 출력\n", "stderr": "중간 오류\n"}
