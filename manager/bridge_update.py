"""갱신 탭·새 프로젝트 적용의 브리지 메서드 — `pm-config update` 와 `pm_import.py --new`.

경계 세 줄:
  - 관리자는 엔진 파일을 복사하지 않는다. 갱신도 적용도 전부 엔진 명령의 서브프로세스다.
  - `pm-config update` 에 `--from` 을 주지 않는다. 사본이 자기 `upstream.path` 로 소스를
    해소하고, 그 값이 URL 이면 엔진이 자기 캐시로 clone/fetch 한다(관리자 캐시 0).
  - `pm_import.py --new` 에는 **`--upstream` 을 항상 명시**한다. 생략하면 엔진이 `--from` 이
    git checkout 인지로 값을 가르는데 번들은 checkout 이 아니라서, 프로세스와 함께 사라지는
    `/tmp/_MEIxxxxxx` 가 그 프로젝트의 upstream 으로 박힌다(그 프로젝트는 갱신 경로를 잃는다).

`pm_config.py update` 의 산출은 사람이 읽는 여러 줄 텍스트다(JSON 없음). `update_preview` 는
rc·stdout·stderr 를 가공 없이 넘기고, 어댑터 drift 줄 강조는 화면의 정규식이다 — 출력을 파싱해
구조를 만드는 자리는 없다.

인터프리터: 프로젝트 사본을 부를 때는 그 사본 `local.conf` 의 `runtime.py`(빈값이면 OS 기본)를
쓴다. 아직 프로젝트가 없는 `pm_import.py --new` 도 같은 함수를 지나는데, dest 에 conf 가 없으니
그 규칙의 OS 기본이 나온다 — 규칙을 두 번 쓰지 않는다. `sys.executable` 은 쓰지 않는다: frozen
에서 그것은 관리자 실행 파일 자신이고, `pm_import` 가 `board.py init` 을 `sys.executable` 로
부르므로 관리자가 자기 자신을 다시 띄운다.
"""
from __future__ import annotations

from pm_engine.domain import install_rules
from pm_engine.infra import upstream_git

import importlib
import importlib.util
import subprocess
import sys
from pathlib import Path

from . import CallError, ENGINE_LOCK, REPO_ROOT
from . import registry, runner
from .bridge_settings import _completed, _pm_home, _run, _tool

# 갱신 한 번의 상한 — URL upstream 이면 엔진이 캐시 clone 을 돌리고 그 상한이 600초다
# (`upstream_git.UPSTREAM_CLONE_TIMEOUT_SECONDS`). 그 위에 파일 복사·훅 재설치 몫을 얹는다.
UPDATE_TIMEOUT_SECONDS = 900
# 적용 한 번의 상한 — 로컬 파일 복사 + `board.py init` 이다(실측 1.0초 · 네트워크 0).
APPLY_TIMEOUT_SECONDS = 300

# 빌드가 만드는 각인 모듈(git-ignored·개발 트리에는 없다). 있으면 `UPSTREAM_URL` 을 든다.
BUILD_INFO_MODULE = f"{__package__}._build_info"
# 적용 성공의 두 번째 조건 — 엔진이 남기는 설치 기록. rc 0 만으로 등록하지 않는다.
INSTALL_RECEIPT = Path(".project_manager") / "install.json"


def bundle_root() -> Path:
    """번들에 실린 프레임워크 트리의 루트 — frozen 이면 `sys._MEIPASS`, 아니면 repo 루트.

    `.project_manager/tools/` 와 `templates/` 가 이 아래에 소스 트리와 같은 상대 위치로 들어간다
    (번들 배치는 패키징 티켓의 spec 파일이 소유한다).
    """
    unpacked = getattr(sys, "_MEIPASS", None)
    return Path(unpacked) if unpacked is not None else REPO_ROOT


def imprinted_upstream() -> str:
    """새 프로젝트에 박을 upstream 기본값 — 빌드 각인, 없으면 번들 트리의 origin URL.

    개발 트리에는 각인 모듈이 없으므로 `derive_origin_url(bundle_root())` 로 간다 —
    `pm_import.py` 가 `--upstream` 생략 시 쓰는 바로 그 함수(`infra.upstream_git`)라 새 판정이 아니다. 둘 다 없으면 빈
    문자열이고, 화면이 사람에게 값을 묻는다(관리자가 URL 상수를 손으로 박지 않는다).
    """
    if importlib.util.find_spec(BUILD_INFO_MODULE) is not None:
        return importlib.import_module(BUILD_INFO_MODULE).UPSTREAM_URL
    with ENGINE_LOCK:
        derived = upstream_git.derive_origin_url(bundle_root())
    return derived or ""


def registered_harnesses() -> list[str]:
    """하네스 라디오의 값 — 번들 엔진의 어댑터 registry(= template 디렉터리 표) 그대로."""
    with ENGINE_LOCK:
        return list(install_rules.REGISTERED_HARNESSES)


def update_argv(pm_home: Path, *, dry_run: bool) -> list[str]:
    """`pm-config update` 한 줄 — `--from` 은 붙이지 않는다(소스는 사본이 해소한다)."""
    argv = [*runner.interpreter(pm_home), _tool(pm_home, "pm_config.py"), "update"]
    if dry_run:
        argv.append("--dry-run")
    return argv


def import_argv(target: Path, harness: str, name: str, upstream: str) -> list[str]:
    """`pm_import.py --new` 한 줄 — 번들 소스에서 OS 기본 인터프리터로 돈다.

    `--upstream` 은 인자로 받은 값 그대로이고 생략 가지가 없다. `--fill manual` 고정 —
    `auto` 는 하네스를 헤드리스로 구동하므로 관리자에서 열지 않는다.
    """
    root = bundle_root()
    argv = [*runner.interpreter(target),
            str(root / ".project_manager" / "tools" / "pm_import.py"),
            "--new", str(target), "--harness", harness]
    if name:
        argv += ["--name", name]
    return [*argv, "--fill", "manual", "--from", str(root), "--upstream", upstream]


def init_argv(target: Path, prefix: str, area: str, owner: str, prefix_ack: str) -> list[str]:
    """`pm-config init` 한 줄 — 빈 값은 인자 자체를 넘기지 않는다.

    `--user-ack` 는 **확인 대화가 돌려준 값**(`prefix_ack`)에서만 온다. 관리자가 `prefix` 를
    복사해 붙이는 가지는 없다(값이 다르면 이 줄을 만들기 전에 거부된다).
    """
    argv = [*runner.interpreter(target), _tool(target, "pm_config.py"), "init"]
    if prefix:
        argv += ["--prefix", prefix, "--user-ack", prefix_ack]
    if area:
        argv += ["--area", area]
    if owner:
        argv += ["--owner", owner]
    return argv


def _target(dest: str) -> Path:
    """적용 대상 폴더 — 비어 있으면 거부. 비어 있지 않은 폴더의 거부는 엔진 문구가 낸다."""
    value = (dest or "").strip()
    if not value:
        raise CallError("invalid-path", "적용할 폴더를 고르지 않았다.", rc=2)
    return Path(value).expanduser().resolve()


def _checked_prefix_ack(prefix: str, prefix_ack: str) -> None:
    """prefix 승인 값 대조 — 다르면 서브프로세스를 띄우기 전에 거부한다.

    `--user-ack` 는 "사용자가 이 prefix 를 승인했다" 는 토큰이라 관리자가 만들어 낼 수 없다.
    화면에서 고칠 값이 있는 거부라 종류는 `invalid-path` 다(다섯 종류 중 값이 문제인 자리).
    """
    if prefix and prefix_ack != prefix:
        raise CallError(
            "invalid-path",
            f"prefix 승인 값이 다르다 — 확인 대화에서 {prefix!r} 를 그대로 승인해야 한다 "
            f"(받은 값: {prefix_ack!r}).", rc=2)


def _checked_upstream(upstream: str) -> str:
    """`--upstream` 에 넘길 값 — 비어 있으면 거부하고 몰래 채우지 않는다.

    화면이 각인값을 미리 채우므로 빈 값은 사람이 지운 것이다. 여기서 기본값을 되살리면 사람이
    지운 뜻과 다른 upstream 이 그 프로젝트에 영구히 박힌다.
    """
    value = (upstream or "").strip()
    if not value:
        raise CallError("invalid-path",
                        "upstream 이 비어 있다 — 이 값이 그 프로젝트의 갱신 경로다.", rc=2)
    return value


def _step(name: str, completed) -> dict:
    """단계 하나의 로그 — rc·stdout·stderr 는 엔진 원문 그대로다."""
    return {"name": name, "rc": completed.returncode,
            "stdout": completed.stdout, "stderr": completed.stderr}


def _note(name: str, rc: int, message: str) -> dict:
    """엔진 호출이 아닌 단계(등록)의 한 줄 — 관리자가 낸 말이라는 것이 이름으로 드러난다."""
    return {"name": name, "rc": rc, "stdout": message if rc == 0 else "",
            "stderr": "" if rc == 0 else message}


class UpdateApi:
    """갱신 탭(`pm-config update`)·새 프로젝트 적용(`pm_import`) 동작."""

    # ── 갱신 ──────────────────────────────────────────────────────────────
    def update_preview(self, path: str) -> dict:
        """`pm-config update --dry-run` — 엔진 출력 원문 그대로(요약·파싱 없음)."""
        return self._update(path, dry_run=True)

    def update_apply(self, path: str) -> dict:
        """`pm-config update`의 RUN1 배달과 RUN2 완료를 새 프로세스로 실행한다."""
        return self._update(path, dry_run=False)

    def _update(self, path: str, *, dry_run: bool) -> dict:
        steps: list[dict] = []
        stage = "RUN1"
        try:
            home = _pm_home(path)
            if dry_run:
                return _completed(_run(home, update_argv(home, dry_run=True),
                                       timeout=UPDATE_TIMEOUT_SECONDS))
            # v1.8.0 이상 사본: RUN1 이 엔진 코드 전달과 완료 단계를 끝내고 RUN2 는 변경 0건 확인이다.
            # v1.7.13 이하 사본: RUN1 은 옛 갱신기가 배달만 끝낼 수 있고 RUN2 가 새 갱신기로 완료 단계를
            # 실행한다. 재시도는 없다.
            for stage in ("RUN1", "RUN2"):
                completed = _run(home, update_argv(home, dry_run=False),
                                 timeout=UPDATE_TIMEOUT_SECONDS)
                steps.append(_step(stage, completed))
                if completed.returncode != 0:
                    break
            response = {**_completed(completed), "steps": steps,
                        "stdout": "".join(step["stdout"] for step in steps),
                        "stderr": "".join(step["stderr"] for step in steps)}
            if completed.returncode != 0:
                response["failed_step"] = stage
            return response
        except CallError as exc:
            if dry_run or (not steps and exc.kind == "invalid-path"):
                return exc.as_error_dict()
            # _run의 timeout 원인은 캡처 도중의 bytes를 보유한다. 완료된 RUN1뿐 아니라
            # 중단된 RUN2 원문도 보존하며, rc가 없는 실행을 성공으로 바꾸지 않는다.
            if isinstance(exc.__cause__, subprocess.TimeoutExpired):
                partial = {}
                for field in ("stdout", "stderr"):
                    value = getattr(exc.__cause__, field) or ""
                    partial[field] = (value.decode("utf-8", errors="replace")
                                      if isinstance(value, bytes) else value)
                steps.append({"name": stage, "rc": None, **partial})
            return {**exc.as_error_dict(), "steps": steps, "failed_step": stage}

    # ── 새 프로젝트 적용 ──────────────────────────────────────────────────
    def apply_defaults(self) -> dict:
        """새 프로젝트 대화가 미리 채울 값 — 하네스 이름 목록과 각인 upstream."""
        return {"harnesses": registered_harnesses(), "upstream": imprinted_upstream()}

    def apply_new(self, dest: str, harness: str, name: str, prefix: str, area: str,
                  owner: str, upstream: str, prefix_ack: str) -> dict:
        """적용 → init → 등록. 성공한 단계까지의 로그를 항상 돌려준다.

        등록은 적용이 rc 0 이고 설치 기록(`install.json`)이 실재할 때만 한다 — 실패한 적용은
        등록부를 한 줄도 건드리지 않는다.
        """
        try:
            target = _target(dest)
            _checked_prefix_ack(prefix, prefix_ack)
            upstream_value = _checked_upstream(upstream)
        except CallError as exc:
            return exc.as_error_dict()

        steps: list[dict] = []
        try:
            steps.append(_step("import", _run(
                bundle_root(), import_argv(target, harness, name, upstream_value),
                timeout=APPLY_TIMEOUT_SECONDS)))
            if steps[-1]["rc"] == 0:
                steps.append(_step("init", _run(
                    target, init_argv(target, prefix, area, owner, prefix_ack),
                    timeout=APPLY_TIMEOUT_SECONDS)))
            if all(step["rc"] == 0 for step in steps):
                steps.append(self._register(target))
        except CallError as exc:
            # 스폰·응답 없음은 rc 가 없는 실패다. 여기까지의 로그를 얹어 화면이 잃지 않게 한다.
            return {**exc.as_error_dict(), "steps": steps}

        ok = all(step["rc"] == 0 for step in steps)
        return {"steps": steps, "ok": ok, "path": str(target) if ok else None}

    def _register(self, target: Path) -> dict:
        """등록부 한 줄 추가 — 설치 기록이 없으면 넣지 않는다(적용이 끝나지 않은 트리)."""
        if not (target / INSTALL_RECEIPT).is_file():
            return _note("register", 1,
                         f"설치 기록이 없어 등록하지 않았다: {target / INSTALL_RECEIPT}")
        try:
            registry.add(target, self._registry_file)
        except CallError as exc:
            return _note("register", 1, exc.message)
        return _note("register", 0, f"등록부에 추가: {target}")
