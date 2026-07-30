"""tests/ 전역 conftest — codex ambient env 중화(autouse) + codex 라이브 하네스 공용 헬퍼 (ADR-0070 T-0407).

두 관심사를 담는다:

1. **codex ambient env 중화 (autouse·T-0405 리뷰 forward-note)** — 회귀를 codex 하네스로 돌리면
   (에이전트=codex 세션이 pytest 를 기동) 그 세션의 ambient `CODEX_THREAD_ID`/`CODEX_CI` 가 pytest
   프로세스 env 로 새어들어와 `pm_bootstrap._is_codex_harness()` 를 참으로 만든다 → 부트스트랩 카드
   codex 절이 주입돼 **카드-정확일치 단언 테스트**(자체 monkeypatch 미보유 파일)가 깨진다. 세션 전역
   autouse 로 기계 테스트에 codex-미감지 baseline 을 준다 — 어느 하네스로 회귀를 돌려도 동일 결과
   (hermetic). 마커를 *원하는* 테스트(codex-card 케이스 등)는 이 fixture 뒤에 자기 monkeypatch.setenv
   로 되살린다(setenv 가 delenv 를 override — 같은 MonkeyPatch 인스턴스, 나중이 이김).

2. **codex 라이브 공용 헬퍼** — `test_pm_adr_live.py`·`test_pm_ticket_live.py`·`test_pm_relay_codex.py`
   가 공유하는 격리 CODEX_HOME + auth 준비/정리 + `codex exec` 실행 조립. adopter import 헬퍼가 있는
   `test_fresh_adopter_runtime_smoke.py`(T-0408 병렬 소유)를 건드리지 않으려 codex-전용 라이브 인프라는
   여기 한곳에 둔다(중복 금지). 라이브 규율(세션 실측·ADR-0070 D7): 격리 CODEX_HOME(실 ~/.codex/auth.json
   복사 후 테스트 종료 시 삭제) + `codex exec … stdin=DEVNULL`(미닫힘 시 무기한 대기 실측·spike §D3).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

# 테스트가 제품의 output_dir 폴백을 밟으면 tempdir에 감사 raw가 영구 누적된다.
# 세션 기본 tempdir를 pytest 소유 디렉터리로 격리해 다른 checkout/PM 프로세스 출력과 섞지 않는다.
_PROJECT_TEMPDIR_GLOBS = (
    "pm_delegate_*",
    "external_review_*",
    "pm_board_seed_*",
    "safewrite-*",
    "safewrite-fac-*",
    "swroot-*",
    "swout-*",
)
_TEMPDIR_ENV_KEYS = ("TMPDIR", "TEMP", "TMP")


def _snapshot_project_temp_outputs(
        root: Path) -> dict[Path, tuple[int, int, int, int]]:
    """프로젝트가 지정 tempdir에 만드는 이름 있는 산출물의 경로와 파일 정체를 스냅샷한다.

    external_review 파일명은 초 단위라 기존 파일을 덮어쓸 수 있다. 경로 집합만 비교하지 않고
    inode/mtime/size도 함께 기록해 같은 이름 덮어쓰기도 신규 tempdir 쓰기로 판정한다.
    """
    snapshot: dict[Path, tuple[int, int, int, int]] = {}
    for pattern in _PROJECT_TEMPDIR_GLOBS:
        for path in root.glob(pattern):
            try:
                stat_result = path.stat()
            except FileNotFoundError:
                continue
            snapshot[path] = (
                stat_result.st_dev,
                stat_result.st_ino,
                stat_result.st_mtime_ns,
                stat_result.st_size,
            )
    return snapshot


@pytest.fixture(scope="session", autouse=True)
def _forbid_real_tempdir_project_outputs(tmp_path_factory):
    """세션 기본 tempdir를 격리하고 이 세션의 프로젝트 산출물만 금지한다.

    Python 현재 프로세스의 캐시와 자식 프로세스가 상속할 표준 환경변수를 함께 고정한다.
    감시 경로는 지역 변수로 보존하므로 테스트가 이후 env/gettempdir를 monkeypatch해도 바뀌지
    않는다. 다른 pytest/PM 프로세스는 이 디렉터리를 모르므로 전역 tempdir 동시 출력은 무관하다.
    """
    session_tempdir = tmp_path_factory.mktemp("project-temp-output-guard")
    before = _snapshot_project_temp_outputs(session_tempdir)
    previous_tempdir = tempfile.tempdir
    previous_env = {key: os.environ.get(key) for key in _TEMPDIR_ENV_KEYS}
    for key in _TEMPDIR_ENV_KEYS:
        os.environ[key] = str(session_tempdir)
    tempfile.tempdir = str(session_tempdir)
    try:
        yield
        after = _snapshot_project_temp_outputs(session_tempdir)
    finally:
        tempfile.tempdir = previous_tempdir
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    written = sorted(path for path, identity in after.items() if before.get(path) != identity)
    assert not written, (
        f"테스트 세션이 격리 tempdir({session_tempdir})에 프로젝트 산출물을 썼습니다. "
        "흐름 테스트에 pytest tmp_path 기반 --output-dir/output_dir를 명시하세요:\n"
        + "\n".join(f"  - {path}" for path in written)
    )

# ── ① codex ambient env 중화 (autouse) ────────────────────────────────────────

# 카드 감지 predicate 가 읽는 두 마커(pm_bootstrap._is_codex_harness)만 중화한다 —
# CODEX_SANDBOX_NETWORK_DISABLED 등 다른 codex 마커는 카드 감지와 무관이라 건드리지 않는다.
_CODEX_AMBIENT_MARKERS = ("CODEX_THREAD_ID", "CODEX_CI")


@pytest.fixture(autouse=True)
def _neutralize_codex_ambient_markers(request, monkeypatch):
    """모든 기계 테스트에 codex-미감지 env baseline 을 준다 (release-marked 라이브 게이트는 제외).

    라이브 게이트(`@pytest.mark.release`) 테스트는 실 codex 를 격리 CODEX_HOME env 로 명시 스폰하므로
    부모 프로세스의 ambient 마커에 의존/간섭하지 않는다 — 중화가 그 의도를 흐리지 않게 제외한다."""
    if request.node.get_closest_marker("release") is not None:
        return
    for key in _CODEX_AMBIENT_MARKERS:
        monkeypatch.delenv(key, raising=False)


# ── ② codex 라이브 공용 헬퍼 (adr/ticket live · relay smoke 공유) ──────────────────

_CODEX_AUTH = Path.home() / ".codex" / "auth.json"

# codex exec 공통 argv 접두 (fill 러너·relay driver 와 동일 핀·ADR-0070): --json JSONL · workspace-write
# (파일 편집/board.py 실행 필요) · --skip-git-repo-check(tmp 홈은 git repo 아님).
_CODEX_EXEC_PREFIX = ("codex", "exec", "--json", "-s", "workspace-write", "--skip-git-repo-check")

# 라이브 codex subprocess 로 통과시킬 env 화이트리스트 — 부모 env 통째 상속 대신 필수만
# (test_fresh_adopter_runtime_smoke._live_env 취지 동일). HOME 은 통과하되 CODEX_HOME 을 격리 dir 로
# 덮어 실 ~/.codex 오염(history/세션/trust)을 막는다.
_CODEX_ENV_PASSTHROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE")


def codex_auth_available() -> bool:
    """실 codex auth 토큰(`~/.codex/auth.json`)이 있나 — 라이브 skipif 게이트에 합류(없으면 graceful skip)."""
    return _CODEX_AUTH.is_file()


def make_codex_home(base: Path) -> Path:
    """격리 CODEX_HOME 을 만들고 실 auth.json 을 복사한다 (실 ~/.codex 미오염·trust 결정성).

    trust 는 여기서 강제하지 않는다 — adr/ticket/relay 라이브는 `-s workspace-write` CLI 핀으로
    쓰기가 열려 project trust 없이 동작(세션 실측). project config/hooks/skills 로드가 필요한
    측정은 호출부가 `config.toml [projects]` trust 를 이 홈에 추가한다(`-c` override 는 무효·실측 §D3)."""
    home = base / ".codex-home"
    home.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_CODEX_AUTH, home / "auth.json")
    return home


def drop_codex_auth(home: Path) -> None:
    """테스트 종료 시 복사해 둔 auth 토큰을 삭제한다 (scratch 에 자격증명 잔류 방지·라이브 규율)."""
    (home / "auth.json").unlink(missing_ok=True)


def codex_live_env(home: Path) -> dict[str, str]:
    """격리 codex 라이브 subprocess env — 화이트리스트 + CODEX_HOME 격리."""
    env = {k: os.environ[k] for k in _CODEX_ENV_PASSTHROUGH if k in os.environ}
    env["CODEX_HOME"] = str(home)
    env["PM_NONINTERACTIVE"] = "1"
    # 모델은 CLI `-m` 로 넘긴다(아래 run_codex_exec) — 기본은 codex 로컬 config 상속(gpt-5.5·D5).
    return env


def run_codex_exec(prompt: str, cwd: Path, home: Path, *, model: str | None = None,
                   timeout: int = 600) -> subprocess.CompletedProcess:
    """`codex exec` 한 turn 을 격리 홈으로 실행한다 (stdin=DEVNULL 필수·미닫힘 시 무기한 대기·실측).

    기본 모델 = codex 로컬 config 상속(`-m` 생략). `model` 명시(예 env `PM_ORCH_LIVE_CODEX_MODEL`)면
    `-m <model>` 로 override. cwd 는 `-C` 로 핀(child cwd 격리·PM repo root). resume 커맨드형이
    필요하면 driver(`pm_orch_codex.py`)를 쓴다 — 이 헬퍼는 단일-turn exec 전용."""
    cmd = list(_CODEX_EXEC_PREFIX)
    if model:
        cmd += ["-m", model]
    cmd += ["-C", str(cwd)]
    cmd.append(prompt)
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=codex_live_env(home), stdin=subprocess.DEVNULL, timeout=timeout,
    )
