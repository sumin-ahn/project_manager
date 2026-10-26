"""어댑터 훅 선언 값 형식·선언 데이터와 보존 필드·토큰 규칙 (층: domain)."""
from __future__ import annotations

from typing import NamedTuple


# ── 어댑터 훅 세트 (세대 정합 판정의 데이터 축) ─────────────────────────────────
# 훅 세트 = "instance-owned config 가 선언한 훅 호출" + "그 호출이 실행하는 manifest 소유
# 래퍼/드라이버". 두 축은 **갱신 주체가 다르다** — config 는 채택자 소유라 pm_update 가 못 덮고,
# 래퍼/드라이버는 manifest 등재라 pm_update 가 덮는다. 그래서 한쪽만 새 세대인 창이 구조적으로
# 열리고, 그중 **config 가 앞선 조합만** 락아웃이다(v1.7.0 흡수 실측: 신 settings.json 의
# `--git-anchor-hook` 을 구 pm_orch_claude.py 가 argparse rc2 로 거부 → PreToolUse rc2 = 도구 차단
# → Bash 전면 락아웃). 반대 방향(구 config + 신 드라이버)은 훅 미발화라 무해하다
# (tests/test_git_anchor_guard.py 의 unknown-flag rc0 관용).
#
# 판정을 하드코딩 claude 전용으로 두지 않기 위해 검사 대상은 전부 이 표에서만 온다. 등록 하네스
# 전수가 키를 가져야 하며(가드가 강제), 훅 커맨드를 선언하는 config 가 없는 하네스는 빈
# `config_relpath` 로 그 사실을 명시한다 — 미선언과 "없음" 이 구분되지 않으면 네 번째 하네스가
# 조용히 미검사로 떨어진다.

# 훅 커맨드에서 스크립트 경로를 뽑을 때 벗겨내는 접두 토큰(하네스 런타임이 해소하는 루트 변수).
_HOOK_COMMAND_ROOT_TOKENS = ("${CLAUDE_PROJECT_DIR}/", "$CLAUDE_PROJECT_DIR/", "./")


class AdapterHookEntrypoint(NamedTuple):
    """이 엔진 세대가 채택자 config 에 **있어야 한다**고 보는 훅 진입점 하나 (역방향 축).

    `flag_support` 와 방향이 반대다 — 저쪽은 "config 가 요구하는 것을 설치본이 감당하나"(config →
    엔진)이고, 이쪽은 "이 엔진 세대가 기대하는 진입점이 config 에 있나"(엔진 → config)다. 진입점이
    빠진 채택자에서는 **앞으로 등록될** 가드가 발화 자체를 안 한다(옛 직결 배선이 남아 있으면 그
    시점의 기능은 계속 돈다) — 그 상태가 조용한 통과로 남지 않게 한다.

    event      훅 이벤트 이름(config `hooks` 의 키).
    matcher    그 이벤트의 값 공간을 전부 덮는 matcher 리터럴. `None` 은 matcher 키 없음
               (claude 처럼 matcher 를 쓰지 않는 이벤트).
    dispatcher 진입점이 실행하는 manifest 소유 파일(dest 기준 POSIX relpath·`live_files` 안).
               판정은 훅 커맨드 문자열이 이 relpath 를 담는가다 — 출하 커맨드가 인터프리터
               해소를 포함한 셸 한 줄이라 argv 분해로는 스크립트를 뽑을 수 없다.
    flag       그 디스패처가 지원해야 하는 플래그 리터럴(없으면 `None`). config 는 갱신됐는데
               디스패처가 구세대면 훅이 매번 폴백으로 빠진다 — 그 창을 판정하는 축이다.
               플래그가 있으면 판정은 **호출 값**(`<flag> <event>`)까지 내려간다: 같은
               커맨드가 디스패처 경로와 그 이벤트 호출을 함께 담아야 진입점으로 세고, 설치된
               디스패처도 같은 두 값을 감당해야 세대 정합이다. 경로만 보면 실행하지 않는
               문자열이나 다른 이벤트를 부르는 커맨드가 false-green 으로 통과한다.
    """
    event: str
    matcher: str | None
    dispatcher: str
    flag: str | None = None


class AdapterHookSetSpec(NamedTuple):
    """한 하네스의 훅 세트 선언 — 판정에 필요한 데이터 전부.

    config_relpath : 훅 호출을 선언하는 instance-owned config(dest 루트 기준 POSIX relpath).
    live_files     : 실행 중 하네스가 읽는 manifest 소유 훅 파일. `/` 로 끝나면 디렉토리 접두
                     (manifest 의 파일/디렉토리 semantics 와 같다). 판정 범위이자 동기 시
                     원자 write 대상이다 — 채택자 자작 훅 스크립트는 여기 없으므로 검사하지
                     않는다(엔진 소관 밖·거짓 처방 금지).
    flag_support   : 훅 커맨드 플래그 → 그 플래그를 **실제로 지원해야 하는** dest 파일들.
                     래퍼가 다른 파일로 dispatch 하면(claude: ctx_stop_hook.sh → pm_orch_
                     claude.py) 그 체인 전부를 적는다. 지원 판정은 파일 본문의 리터럴 보유다
                     (실행 없이 판정·훅 실행은 그 자체가 부작용).
    coupled_groups : **한 세대로 함께 옮겨야** 하는 묶음(로드 시점 결합). 부분 전파가 묶음의
                     일부만 갱신하면 그 자리에서 세대가 갈린다. `live_files` 와 관심사가 다르다 —
                     저쪽은 "원자 write 대상"(파일 단위 torn read)이고 이쪽은 "함께 움직여야
                     하는 단위"다. 결합이 없는 파일(독립 relay 드라이버·위임 채널 가드)은 어느
                     묶음에도 들지 않아 단건 전파가 정당하다.
    entrypoints    : 이 엔진 세대가 config 에 기대하는 **범용 진입점**(`AdapterHookEntrypoint`).
                     역방향 축이라 판정은 loud advisory 다 — config 는 채택자 소유이고 그 파일엔
                     실 노브가 있어, 무편집을 강제하는 red 는 소유 원칙과 충돌한다.
    """
    config_relpath: str
    live_files: tuple[str, ...]
    flag_support: dict[str, tuple[str, ...]]
    coupled_groups: tuple[tuple[str, ...], ...] = ()
    entrypoints: tuple[AdapterHookEntrypoint, ...] = ()


ADAPTER_HOOK_SET = {
    "claude": AdapterHookSetSpec(
        config_relpath=".claude/settings.json",
        live_files=(
            ".claude/ctx_guard.py",
            ".claude/ctx_stop_hook.py",
            ".claude/ctx_stop_hook.sh",
            ".claude/ctx_statusline.py",
            ".claude/ctx_statusline.sh",
            ".claude/delegate_channel_guard_hook.sh",
            ".claude/pm_orch_claude.py",
        ),
        flag_support={
            # 세대 결합 — settings.json 의 Bash 매처가 이 플래그를 넘기고,
            #   래퍼가 pm_orch_claude.py 로 dispatch 한다. 둘 중 하나라도 구 세대면 락아웃.
            "--git-anchor-hook": (
                ".claude/ctx_stop_hook.sh", ".claude/pm_orch_claude.py"),
        },
        coupled_groups=(
            # 훅 체인 + 공유 코어: 래퍼가 플래그로 두 파이썬 진입 중 하나를 고르고, 그 둘이
            #   ctx_guard 를 import 한다. 일부만 옮기면 미지원 플래그·import 불일치가 난다.
            (".claude/ctx_stop_hook.sh", ".claude/ctx_stop_hook.py",
             ".claude/pm_orch_claude.py", ".claude/ctx_guard.py"),
            # statusline 래퍼/구현 쌍(같은 근거·독립 축).
            (".claude/ctx_statusline.sh", ".claude/ctx_statusline.py"),
        ),
        entrypoints=(
            # claude 는 범용 진입점을 이미 갖고 있다 — 이 선언은 값을 바꾸지 않고 선언만
            #   한다(진입점이 사라진 채택자에서 ctx 가드가 조용히 무발화하는 상태를 표면화).
            #   래퍼가 인자로 분기하므로 플래그 축은 `flag_support` 가 이미 본다(중복 선언 0).
            AdapterHookEntrypoint("PreToolUse", "*", ".claude/ctx_stop_hook.sh"),
        ),
    ),
    "codex": AdapterHookSetSpec(
        # `.codex/hooks.json` 의 훅 커맨드는 인터프리터 해소를 포함한 **셸 한 줄**이라 argv 분해로
        #   스크립트를 못 뽑는다(`_hook_script_and_arguments` 의 첫 토큰이 `if`) — 그래서
        #   `flag_support` 는 여기서 구조적으로 공허하고, 같은 세대 결합은 아래 `entrypoints` 의
        #   `flag` 축이 커맨드 파싱 없이 판정한다(공허한 선언을 두지 않는다).
        config_relpath=".codex/hooks.json",
        live_files=(".codex/pm_orch_codex.py",),
        flag_support={},
        entrypoints=tuple(
            # 이벤트당 진입점 하나가 manifest 등재 디스패처를 부른다. PreToolUse는 실제
            #   안전 판정 대상(Bash·apply_patch·collaborationspawn_agent)만 native matcher에서 열고,
            #   나머지 이벤트는 값 공간 전체(`.*`)를 연다.
            #   "어떤 가드를 돌릴지" 는 그 코드 안에서 갈린다. 그래서 가드 **기능** 추가는 이제
            #   엔진 코드 변경뿐이고 채택자 config·`/hooks` 재승인을 다시 요구하지 않는다.
            #   이 집합은 릴리즈 간 불변이다 — 늘리려면 채택자 config 재승인을 동반한 1회
            #   마이그레이션이다. 그래서 codex 가 발화시키는 **모든** 이벤트를 한 번에 담는다:
            #   일부만 담으면 남은 이벤트가 다음 기능에서 재승인을 다시 부른다.
            AdapterHookEntrypoint(
                event,
                "^(Bash|apply_patch|collaborationspawn_agent)$" if event == "PreToolUse" else ".*",
                ".codex/pm_orch_codex.py",
                "--hook-dispatch",
            )
            for event in ("PreToolUse", "UserPromptSubmit", "PostToolUse",
                          "SubagentStart", "PreCompact", "PostCompact")
        ),
    ),
    # opencode 는 훅 커맨드를 선언하는 config 가 없다 — 플러그인은 `.opencode/plugins/`
    #   autoload 라 config 가 호출 형태를 담지 않는다. 판정 대상은 없지만 실행 중 하네스가 읽는
    #   파일이라 원자 write 대상에는 든다. `lib/` 도 함께다 — 플러그인 3종이 로드 시점에
    #   `../lib/*-core.cjs` 를 즉시 import 하므로(plugins/ctx-guard.js·git-anchor.js·
    #   safe-write.js 실측) 코어가 부분 파일이면 플러그인 로드 자체가 깨진다. claude 축이 공유
    #   코어 `ctx_guard.py` 를 넣은 것과 같은 이유다(하네스 간 비대칭 금지).
    "opencode": AdapterHookSetSpec(
        config_relpath="",
        live_files=(".opencode/lib/", ".opencode/plugins/",
                    ".opencode/pm_orch_opencode.py"),
        flag_support={},
        # 진입점 선언도 비어 있다 — 배선이 `plugins/` **디렉토리 스캔**이라 파일을 더하는 것이
        #   곧 배선이고 대조할 config 항목 자체가 없다(codex 진입점의 참고 모델).
        entrypoints=(),
        # 플러그인이 로드 시점에 코어를 import 한다 — 한쪽만 옮기면 그 자리에서 세대가 갈린다.
        #   relay 드라이버는 결합이 없어 묶음 밖이다(단건 전파 정당).
        coupled_groups=((".opencode/plugins/", ".opencode/lib/"),),
    ),
}


# 위 정적 경로와 같은 instance-owned 정책 섹션의 조건부 보호 규칙 — 선언한 최상위 TOML 키를
# adopter 가 갖고 있으면 그 *파일 전체*를 byte 보존한다(자동 TOML merge 대신). pattern/필드
# 추가는 이 선언만 고친다.
#
# **agent 카드의 `model`/`model_reasoning_effort` 는 여기 없다**: 카드의 모델·추론은 local.conf
# `delegate.<role>[.<tier>].{model,reasoning}` 의 렌더 파생물이라 단일 진실이 conf 다. 그 두 필드를
# 여기서 보존하면 add-harness 는 손편집을 지키고 다음 pm-update 는 conf 로 되돌려, 두 표면이 같은
# 필드에 반대 규칙을 선언하게 된다. 모델을 바꾸는 자리는 카드가 아니라 local.conf 다.
ADD_HARNESS_PRESERVE_EXISTING_TOML_FIELDS = {
    "claude": {},
    "opencode": {},
    "codex": {},
}


# sed 치환 대상 operational placeholder (`docs/placeholders.md` 표). 자유서술 3종은 여기 없음(보존).
OPERATIONAL_TOKENS = (
    "{{PROJECT_NAME}}",
    "{{PROJECT_TAGLINE}}",
    "{{PROJECT_ROOT}}",
    "{{PY}}",
    "{{TEST_CMD}}",
    "{{DATE}}",
)


class HookSetGeneration(NamedTuple):
    """세대 선언 해소 결과. `declarations` 가 None 이면 판정할 근거가 없다(게이트는 멈춘다).

    `source_sha256` 은 **선언을 읽어 온 상류 package 의 배송 Python 파일 전체 snapshot digest** 다
    (`infra.upstream_declarations._read_declaration_snapshot`). 게이트가 판정에 쓴 세대 자체를 쓰기
    직전에 재검증하는 결속 축이다(template 해시만 묶으면 "구 선언 판정 + 신 template" 조합이
    통과한다). template raw 해시·원장 내용 해시와는 별개 축이다."""
    declarations: dict | None
    origin: str
    reasons: tuple[str, ...] = ()
    source_sha256: str | None = None
