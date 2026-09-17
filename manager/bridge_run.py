"""구성 탭·현황 탭 행 동작의 브리지 메서드 — 사본의 `pm-config` 를 부르는 자리 하나.

경계 네 줄:
  - 화면이 부를 수 있는 명령은 **선행 토큰 튜플 16개**(`ALLOWED_COMMANDS`)뿐이고, `run` 은 argv
    앞머리를 이 집합과 정확히 맞춘 뒤에만 서브프로세스를 띄운다. `argv[0]` 만 보면 `repo`·`task`
    가 통째로 열려 `repo remove` 같은 형제와 미래 확장이 자동으로 들어온다.
  - `--force` 는 **파괴적 제거**(`worktree remove`)에서만 거부한다(값 형태 `--force=…` 포함) —
    그 강제는 명령줄에서 사람이 직접 한다. 반면 열린 채 남은 기록을 닫는 강제
    (`release <slot> --force`·`task close`)는 사용자 결정("핸드오프 없이 끝난 세션은 사용자가
    강제로 닫는다")의 자리라 화면에서도 부를 수 있다.
  - `--user-ack <값>` 을 이 파일이 만들지 않는다. 화면의 확인 대화가 사용자 클릭으로 돌려준
    값이 argv 에 실려 올 뿐이라, 이 모듈에 그 문자열이 없다.
  - 관리자가 장부 파일(`worktree-leases.json`·`areas.md`·`install.json`)을 직접 여는 자리는
    없다. 장부 변화는 전부 이 서브프로세스의 부작용이다.

거부(허용 목록 밖·금지 플래그)는 **서브프로세스 0개**이고, 실패 형태는 `CallError` 가 아니다 —
바깥을 부르다 실패한 것이 아니라 부르기를 거절한 것이라 kind 축이 다르다(화면은 같은
`{"error": {...}}` 모양으로 받는다).
"""
from __future__ import annotations

from . import CallError
from . import runner
from .bridge_settings import _completed, _pm_home, _run, _tool

# 화면이 부르는 pm-config 명령 — 선행 토큰 튜플의 정확 일치. `whoami` 는 없다(현황 머리글은
# `status --json` 한 번으로 그리므로 사람용 텍스트 출력을 부르는 자리가 없다).
ALLOWED_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("status",),
    ("repo", "list"),
    ("repo", "add"),
    ("repo", "protected"),
    ("worktree", "add"),
    ("worktree", "remove"),
    ("worktree", "prune-stale"),
    ("add-harness",),
    ("alloc",),
    ("release",),
    ("task", "new"),
    ("task", "close"),
    ("task", "prefix"),
    ("task", "end"),
    ("task", "reopen"),
    # 열린 todo 등록·완료 — 화면에서 직접 다루는 목록이라 읽기(status)만으로는 부족하다.
    ("task", "todo"),
)

# 화면에 없는 플래그 — 값 형태(`--force=1`)도 같이 막는다.
FORBIDDEN_FLAG = "--force"
# 그 플래그를 막는 명령 — **파괴적 제거 하나**다. 강제 반납은 기록을 닫는 사용자 행위라 여기
# 없다(그 자리가 없으면 화면에서 끝난 세션을 닫을 방법이 사라진다).
FORCE_FORBIDDEN_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("worktree", "remove"),
)
# 거부의 rc — 엔진을 부르지 않았다는 뜻의 관리자 자신의 값이다(argparse 사용법 오류와 같은 2).
REJECT_RC = 2
# 명령 하나의 상한 — 가장 긴 것이 `repo add --git <URL>`(실 bare clone)과 `add-harness`
# (소스가 URL 이면 엔진 캐시 clone)라 엔진의 clone 상한(600초)에 맞춘다.
RUN_TIMEOUT_SECONDS = 600


def matched_command(argv: list[str]) -> tuple[str, ...] | None:
    """argv 의 선행 토큰이 정확히 맞는 허용 항목 — 없으면 None(= 서브프로세스 없음)."""
    for command in ALLOWED_COMMANDS:
        if tuple(argv[:len(command)]) == command:
            return command
    return None


def forbidden_flag(argv: list[str]) -> str | None:
    """`--force` 를 막는 명령에서만 그 토큰 — 값 형태(`--force=…`)를 포함해 찾는다."""
    if matched_command(argv) not in FORCE_FORBIDDEN_COMMANDS:
        return None
    for token in argv:
        if token == FORBIDDEN_FLAG or token.startswith(f"{FORBIDDEN_FLAG}="):
            return token
    return None


def _rejected(kind: str, message: str) -> dict:
    """부르기를 거절한 자리 — 화면은 `CallError` 와 같은 모양으로 받는다(stderr 는 없다)."""
    return {"error": {"kind": kind, "message": message, "stderr": "", "rc": REJECT_RC}}


def _command_list() -> str:
    return " · ".join(" ".join(command) for command in ALLOWED_COMMANDS)


class RunApi:
    """구성 탭(repo·슬롯·하네스)과 현황 탭 행 동작(대여·반납·task) 실행."""

    def run(self, path: str, argv: list[str]) -> dict:
        """그 사본의 `pm-config <argv…>` 한 번 — rc·stdout·stderr 를 원문 그대로 돌려준다.

        엔진의 거부(승인 토큰·소유검사·dirty·claimed 게이트)는 rc 와 문구 그대로 올라간다.
        관리자가 그 판정을 다시 하거나 문구를 고쳐 쓰는 자리는 없다.
        """
        tokens = list(argv)
        if matched_command(tokens) is None:
            return _rejected(
                "not-allowed",
                f"화면이 부르는 pm-config 명령이 아니다: {' '.join(tokens) or '(빈 인자)'} "
                f"(허용: {_command_list()})")
        flag = forbidden_flag(tokens)
        if flag is not None:
            return _rejected(
                "forbidden-flag",
                f"{FORBIDDEN_FLAG} 는 이 명령에서 화면에 없다 — 받은 토큰 {flag!r}. 강제 슬롯 "
                "제거는 명령줄에서 사람이 직접 한다.")
        try:
            home = _pm_home(path)
            command = [*runner.interpreter(home), _tool(home, "pm_config.py"), *tokens]
            return _completed(_run(home, command, timeout=RUN_TIMEOUT_SECONDS))
        except CallError as exc:
            return exc.as_error_dict()
