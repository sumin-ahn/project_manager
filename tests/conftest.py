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
    # tempdir 폴백 장부(PM 홈 미해소 형상) + 그 잠금 파일. 기본 목적지가 repo 로 옮겨졌어도
    # 폴백 경로는 살아 있으므로 이 축도 세션 누출 대상이다.
    "pm_raw_outputs.json",
    "pm_raw_outputs.json.lock",
)
_TEMPDIR_ENV_KEYS = ("TMPDIR", "TEMP", "TMP")

# 위임·외부리뷰 raw 의 **기본 목적지**는 tempdir 가 아니라 해소된 repo 의
# `.project_manager/.local/` 하위다. 그래서 tempdir 격리만으로는 부족하다 — 기본 경로를 밟는
# 테스트는 tempdir 가 아니라 **실 작업 트리 안**에 raw 를 쓰고, 위 세션 스냅샷은 그것을 보지
# 못한다. 감시 축을 하나 더 세워 두 목적지를 함께 닫는다(tempdir 폴백 + repo 기본 경로).
# 이 경로는 *이 checkout* 소유라 다른 PM 프로세스(자기 PM 홈에 쓴다)의 동시 출력과 섞이지 않는다 —
# 전역 tempdir 감시가 flaky 했던 원인이 여기엔 없다.
_REPO_ROOT_FOR_RAW_GUARD = Path(__file__).resolve().parent.parent
_REPO_RAW_OUTPUT_DIRS = (
    Path(".project_manager") / ".local" / "delegate",
    Path(".project_manager") / ".local" / "review",
)
_REPO_RAW_OUTPUT_FILES = (
    Path(".project_manager") / ".local" / "raw_outputs.json",
    # 장부 잠금 파일도 세션이 남기면 누출이다 — 정상 종료면 잠금은 해제되지만 파일 자체가
    # 새로 생겼다는 사실이 "이 세션이 기본 경로를 밟았다"는 증거다.
    Path(".project_manager") / ".local" / "raw_outputs.json.lock",
)


def _snapshot_repo_raw_outputs(
        repo_root: Path) -> dict[Path, tuple[int, int, int, int]]:
    """repo 기본 raw 목적지(디렉토리 직계 파일 + 장부 파일)의 파일 정체를 스냅샷한다.

    디렉토리/파일 부재는 정상(빈 스냅샷) — 신규 생성 자체가 델타로 잡힌다.

    `delegate/` 디렉토리는 raw 외에 **쓰고-지우는 중간 산출**(opencode 프롬프트 파일)도 만들 수
    있다. 정상 경로는 unlink 로 지우므로 델타에 남지 않고, 남았다면 그것 자체가 누출 신호다 —
    특정 접두어로 좁히지 않고 디렉토리의 직계 파일 전부를 본다.

    직계 열거(`iterdir`)로 충분하다 — raw 박제는 목적지 디렉토리에 **평평하게** 파일을 만든다
    (`pm_delegate_<harness>_<pid>_<uuid>.txt`·`external_review_<reviewer>_<ts>.txt`). 재귀 walk 는
    공용 repo-owned 열거 seam 을 우회하는 형태가 되고, 감시 대상은 git-ignored 산출물이라 그 seam 의
    tracked-only 의미와도 맞지 않는다. 하위 디렉토리가 생기는 설계 변경이 오면 그때 seam 을 태운다.
    """
    snapshot: dict[Path, tuple[int, int, int, int]] = {}
    candidates: list[Path] = [repo_root / rel for rel in _REPO_RAW_OUTPUT_FILES]
    for rel in _REPO_RAW_OUTPUT_DIRS:
        base = repo_root / rel
        if base.is_dir():
            candidates.extend(p for p in base.iterdir() if p.is_file())
    for path in candidates:
        try:
            stat_result = path.stat()
        except (FileNotFoundError, NotADirectoryError):
            continue
        snapshot[path] = (
            stat_result.st_dev,
            stat_result.st_ino,
            stat_result.st_mtime_ns,
            stat_result.st_size,
        )
    return snapshot


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
    repo_before = _snapshot_repo_raw_outputs(_REPO_ROOT_FOR_RAW_GUARD)
    previous_tempdir = tempfile.tempdir
    previous_env = {key: os.environ.get(key) for key in _TEMPDIR_ENV_KEYS}
    for key in _TEMPDIR_ENV_KEYS:
        os.environ[key] = str(session_tempdir)
    tempfile.tempdir = str(session_tempdir)
    try:
        yield
        after = _snapshot_project_temp_outputs(session_tempdir)
        repo_after = _snapshot_repo_raw_outputs(_REPO_ROOT_FOR_RAW_GUARD)
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
    repo_written = sorted(
        path for path, identity in repo_after.items() if repo_before.get(path) != identity)
    assert not repo_written, (
        "테스트 세션이 실 작업 트리의 기본 raw 목적지"
        f"({_REPO_ROOT_FOR_RAW_GUARD}/.project_manager/.local/)에 산출물을 썼습니다 — "
        "기본 목적지가 여기이므로 흐름 테스트는 tmp_path 기반 "
        "--output-dir/output_dir 를 명시해야 합니다:\n"
        + "\n".join(f"  - {path}" for path in repo_written)
    )

# ── ① codex ambient env 중화 (autouse) ────────────────────────────────────────

# 카드 감지 predicate 의 두 마커와 T-0592 실송신 게이트의 network-off 마커를 중화한다.
# 후자는 승인형 비샌드박스 실행에서도 부모 Codex가 `1`로 유지하는 ambient 값이라, mock runner로
# 제품 흐름을 검증하는 기존 테스트가 호출층 attestation 부재로 차단되는 것을 막아야 한다. egress
# 전용 테스트는 이 fixture 뒤에 monkeypatch.setenv 로 다시 켜 두 축을 명시 검증한다.
_CODEX_AMBIENT_MARKERS = (
    "CODEX_THREAD_ID",
    "CODEX_CI",
    "CODEX_SANDBOX_NETWORK_DISABLED",
)


@pytest.fixture(autouse=True)
def _neutralize_codex_ambient_markers(request, monkeypatch):
    """모든 기계 테스트에 codex-미감지·egress-neutral env baseline 을 준다.

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
