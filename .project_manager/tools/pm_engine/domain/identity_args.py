"""정체성 인자와 슬롯·task 값 판정 (층: domain).

`--task`·`--repo`/`--slot` 인자 정의와 해소(`add_task_arg`·`add_identity_args`·`parse_identity`), task 이름
검증, 경로 마디·슬롯 키 술어, 장부 판정값과 해소 결과 형식을 소유한다. 장부 파일을 읽는 해소는
`pm_engine.infra.lease_store` 가 한다.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path


# task pm_state 신규 세션 window의 단일 empty marker. state 생성(worktree_pool)과
# handoff 갱신(pm_handoff)이 같은 literal을 소비해야 하므로 공용 경계 모듈이 소유한다.
TASK_PM_STATE_EMPTY_MARKER = "  - (아직 완료된 task 세션 없음)"


class Identity:
    """`parse_identity` 의 discriminated 결과 — caller 는 `kind` 로 위치를 가른다.

    - `kind="slot"`: `--repo X --slot N` 둘 다 지정 — repo=X·slot=N(폴더 지목).
    - `kind="repo"`: `--repo X` 만 지정(슬롯 무) — repo=X·slot=None(그 repo 의 폴더 지목).
    - `kind="none"`: 위치 인자 전무 — repo/slot 둘 다 None.
    정체성은 `task`(`--task`) 하나다. 위치 인자는 정체성을 만들지 않는다.

    (평범한 클래스 — `@dataclass` 미사용: 엔진 도구는 `spec_from_file_location` 으로 로드되는데
    `from __future__ import annotations`(문자열 지연평가) 와 결합 시 모듈이 `sys.modules` 에
    등록 안 돼 있으면 dataclass 처리가 `AttributeError` 로 깨진다 — `worktree_pool.Lease`·
    `pm_engine.infra.harness.watchdog` 의 동일 회피 관용구를 따른다.)
    """

    def __init__(self, kind: str, repo: str | None, slot: int | None,
                 task: str | None = None):
        self.kind = kind
        self.repo = repo
        self.slot = slot
        # 정체성 축 — `--task` 명시 시 그 task 이름(없으면 None). 위치 축(kind)과 직교한다:
        # task 는 `--repo --slot` 과 함께(보유 폴더 지목) 또는 단독으로 온다.
        self.task = task

    def __repr__(self) -> str:
        return (f"Identity(kind={self.kind!r}, repo={self.repo!r}, "
                f"slot={self.slot!r}, task={self.task!r})")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Identity):
            return NotImplemented
        return (self.kind, self.repo, self.slot, self.task) == (
            other.kind, other.repo, other.slot, other.task)


def add_task_arg(parser: argparse.ArgumentParser) -> None:
    """정체성 인자 `--task` 만 parser 에 추가한다(폴더를 고르지 않는 명령용)."""
    parser.add_argument(
        "--task", metavar="이름", default=None,
        help="task 이름 — 정체성. `<등록 repo>_<N>` 폴더 이름 패턴은 거부.",
    )


def add_identity_args(parser: argparse.ArgumentParser) -> None:
    """`--task`·`--repo`·`--slot` 을 parser 에 추가 — 폴더를 고르는 명령(실행 소비자) 전용.

    `--slot` 단독(``--repo`` 없이) 금지 규칙은 여기(add 시점)가 아니라 parse 후 `parse_identity`
    가 검사한다 — argparse 만으로는 "A 있으면 B 도 필수"를 표현할 수단이 마땅치 않고(양방향
    `required` 조합 불가), 에러 메시지도 전 도구가 동일해야 하므로(카드↔CLI 정합) 검사를
    한 곳(`parse_identity`)에 모은다.
    """
    parser.add_argument(
        "--repo", metavar="이름", default=None,
        help="repo 이름 — --task 가 보유한 폴더를 고르는 위치 인자(정체성 아님). "
             "--task 와 함께만 쓴다.",
    )
    parser.add_argument(
        "--slot", metavar="N", type=int, default=None,
        help="폴더 번호 — --repo 필수(단독 사용 불가). --task 가 보유한 work/<repo>_<N> 을 고른다.",
    )
    parser.add_argument(
        "--task", metavar="이름", default=None,
        help="task 이름 — 작업 단위 정체성 축이며 정체성 해소 체인 "
             "전반이 소비한다. 포맷 자유(prefix 아님)·`<등록 repo>_<N>` 예약 패턴은 거부. "
             "repo/slot과의 조합 허용 여부는 소비 도구 계약을 따른다. pm_bootstrap/"
             "pm_handoff에서는 task 단독만 허용하고 보유 슬롯 집합을 자동 해소한다.",
    )


def parse_identity(args: argparse.Namespace) -> Identity:
    """parsed args(`.repo`·`.slot`)에서 discriminated `Identity` 로 해소한다.

    fail-loud(`ValueError`) 두 경우 — (1) `--slot` 있는데 `--repo` 없음, (2) `--slot < 1`(슬롯
    번호는 1부터·`work/<repo>_<N>` 정합). caller 가 `parser.error(str(exc))` 로 그대로 보인다.
    그 외 세 경우(slot/repo/none)는 성공한다.

    slot≥1 검증은 이 모듈이 단일 진실이다 — 여기서 빠뜨리면 채택시 회귀.
    """
    repo = getattr(args, "repo", None)
    slot = getattr(args, "slot", None)
    # task 축 — repo/slot 축과 직교. 여기선 값만 실어주고(kind 는
    # repo/slot 로 그대로 결정), `<등록 repo>_<N>` 예약 패턴 거부는 등록 repo 집합이 필요하므로
    # 순수 층 밖에서(`is_reserved_task_name`·caller 가 등록 repo 를 넘김) 검증한다(이 함수=파일 IO 0).
    task = getattr(args, "task", None)
    if slot is not None and repo is None:
        raise ValueError("--slot 은 --repo 필수 — --repo <name> --slot <N>")
    if slot is not None and slot < 1:
        raise ValueError("--slot 은 1 이상의 슬롯 번호여야 한다 (work/<repo>_<N>).")
    if repo is not None and slot is not None:
        return Identity(kind="slot", repo=repo, slot=slot, task=task)
    if repo is not None:
        return Identity(kind="repo", repo=repo, slot=None, task=task)
    return Identity(kind="none", repo=None, slot=None, task=task)


def is_reserved_task_name(name: str, registered_repos: "list[str] | set[str]") -> bool:
    """task 명이 `<등록 repo>_<N>` 슬롯 폴더 이름 패턴과 충돌하면 True — 예약(task 명 검증).

    순수 함수(파일 IO 0) — 등록 repo 집합을 caller 가 넘긴다(`parse_identity` 의 순수 층 규율
    보존·등록 repo 는 areas/leases 유래라 IO 층 밖). 등록된 각 repo `R` 에 대해 `^R_<정수>$` 를
    검사한다: task 이름이 그 형태면(예 `myproj_2`) 슬롯 폴더 이름(`work/<repo>_<N>`)과 섞여 폴더
    이름과 task 이름의 분리가 깨지므로 거부한다(task 는 자유 포맷이되 폴더 이름 형태는 예약). **등록 repo
    집합에 없는** repo 로 시작하는 `_N` 형태는 무관(자유 포맷 허용) — 실재 슬롯과만 충돌 방지한다.
    """
    for repo in registered_repos:
        if re.match(rf"^{re.escape(repo)}_\d+$", name):
            return True
    return False


class InvalidTaskName(ValueError):
    """CLI 정체성 깔때기의 task 명 검증 실패 — 공백/괄호/path/선행 `.`/예약패턴 (fail-loud·게이트).

    `worktree_pool._validate_task_name`(엔진 bind 층)과 **동형 규칙·독립 구현**이다 — board 는
    worktree_pool 을 import 하지 않으므로(격리 관성·`is_reserved_task_name` 이 예약 정규식을
    이미 같은 근거로 mirror) CLI 층 공유 validator 를 여기 둔다. board 의 정체성 깔때기
    (`_actor_session_override`·`cmd_new`)가 이 **하나**를 소비해, 무검증 task 명이 `created_by`/
    `claimed_by`/lease-session 으로 영속되는 클래스를 소비 지점 전체에서 한 번에 닫는다. `ValueError`
    서브클래스라 caller 의 기존 `except ValueError`(parse_identity fail-loud 관례)가 그대로 잡는다.
    """

    def __init__(self, name: str, reason: str):
        self.name = name
        self.reason = reason
        super().__init__(f"부적합 task 명 {name!r} — {reason}")


def validate_task_name(name: str, registered_repos: "list[str] | set[str] | None" = None) -> None:
    """CLI 층 task 명 검증 — 위반 시 `InvalidTaskName`(fail-loud) (worktree_pool._validate_task_name 동형).

    task 명 영속 지점(`created_by`/`claimed_by`·lease session)이 무검증 값을 저장하지 못하게 board
    정체성 깔때기가 **부작용 이전** 소비하는 공유 validator. 문자 도메인은 하류 구문 표면(CLI 인자
    경계·relay slash·log 태그 delimiter·path)에 맞춘 협소화로 worktree_pool 엔진 validator 와 규칙이
    동형이다(모듈 격리라 독립 구현·`is_reserved_task_name` 재사용). 거부: 빈/공백-only·whitespace·괄호
    `(`/`)`·path separator(`/`·`\\`)·선행 `.`(traversal)·단일 컴포넌트 아님·(registered_repos 주면)
    `<repo>_<N>` 슬롯 폴더 이름 패턴 예약. 한글·하이픈·언더스코어·숫자는 통과(어느 표면과도 무충돌).
    """
    if not name or not name.strip():
        raise InvalidTaskName(name, "빈 이름(공백 포함)")
    if any(ch.isspace() for ch in name):
        raise InvalidTaskName(
            name, "공백·탭 등 whitespace 불가 (CLI/relay `--task <task>` 인자 경계 파손 방지)")
    if "(" in name or ")" in name:
        raise InvalidTaskName(
            name, "괄호 `(`·`)` 불가 (log 헤더 태그 `(task:<task>)` delimiter 파손 방지)")
    if "/" in name or "\\" in name:
        raise InvalidTaskName(name, "path separator(`/`·`\\`) 불가 — 단일 이름이어야")
    if name.startswith("."):
        raise InvalidTaskName(name, "선행 `.` 불가(숨김/`.`/`..` 상대경로 traversal 방지)")
    if Path(name).name != name:
        raise InvalidTaskName(name, "단일 path 컴포넌트가 아님")
    if registered_repos and is_reserved_task_name(name, registered_repos):
        raise InvalidTaskName(name, "슬롯 폴더 이름 패턴(<repo>_<N>)")


# 장부에 행이 하나도 없는 홈(= 아직 작업 폴더가 없는 홈)이 정체성 해소에 실패했을 때 붙이는
# 처방 한 줄. 미해소를 조용한 폴백으로 덮지 않으므로, 사용자가 무엇을 하면 되는지는 메시지가
# 말해야 한다. 문구를 재타이핑하지 않도록 이 모듈이 단일 진실로 들고, 정체성을 해소하는
# 모듈들이 여기서 가져다 쓴다.
def no_work_folder_remedy() -> str:
    """작업 폴더가 없는 홈에 붙이는 처방 한 줄 — 모든 소비자가 이 한 문장을 쓴다.

    repo 를 알면 실 명령으로, 모르면 `<repo>` 자리표시자로 낸다. 부트스트랩·0단계 게이트·
    snapshot 경고·귀속 쓰기 거부가 같은 문장을 낸다(문구 사본 0).
    """
    name = "<repo>"
    return (f"작업 폴더가 없다 — `pm-config worktree add {name} --user-ack {name}` 로 "
            "먼저 만들어라.")


NO_WORK_FOLDER_REMEDY = "이 홈의 lease 장부에 행이 하나도 없으면 " + no_work_folder_remedy()


# 장부 읽기 축의 판정값 — "못 읽었다"를 **부재**와 **손상**으로 가른다(합치면 실제로 행을
# 보유한 홈도 장부가 깨지기만 하면 "행 없음"과 같은 취급을 받는다).
LEDGER_OK = "ok"                       # 정상 파싱 — rows 가 그 시점 진실.
LEDGER_MISSING = "ledger-missing"      # 장부 파일 자체가 없다(fresh 홈).
LEDGER_UNREADABLE = "ledger-unreadable"  # 파일은 있으나 읽기/파싱/스키마 실패(손상).


# ── 경로 마디 안전 술어 (POSIX·Windows 공통·두 축 공유) ────────────────────
# 슬롯 축의 두 값(장부 행의 session 키 · 슬롯 경로의 마디)은 **둘 다 파일 경로로 결합**된다 —
# 키는 `.local/slots/<키>/pm_state.md` 디렉토리명이 되고, 경로 마디는 `REPO / <슬롯 경로>` 가
# 된다. 그래서 두 축은 **같은 술어**를 소비해야 한다: 한쪽만 조이면 같은 값이 다른 축으로 들어와
# 상태 루트 밖을 가리킨다(리뷰 F-003 실측 — `C:_1` 이 session 키로 통과해
# `PureWindowsPath("D:/pm/.project_manager/.local/slots") / "C:_1"` 이 부모를 버리고 `C:_1` 을 냈다).
#
# 판정은 **실행 OS 와 무관**하다 — POSIX·Windows 양쪽에서 안전한 교집합만 통과시킨다. 같은 장부를
# 두 OS 가 공유하므로(POSIX 에서 적힌 값이 Windows 에서 읽힌다) 실행 OS 기준으로 재면 그 순간엔
# 안전해 보이는 값이 다른 OS 에서 탈출한다.
_PATH_COMPONENT_SEPARATORS = ("/", "\\")
# Windows 파일명 금지 문자 — 드라이브 표기 `C:` 의 콜론도 이 집합이 함께 막는다.
_PATH_COMPONENT_FORBIDDEN_CHARS = frozenset('<>:"|?*')
# Windows 예약 장치 이름 — 확장자를 붙여도(`NUL.txt`) 같은 장치라 stem 으로 판정한다.
_PATH_COMPONENT_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{n}" for n in range(1, 10)}
    | {f"LPT{n}" for n in range(1, 10)}
)
_PATH_COMPONENT_DOT_NAMES = (".", "..")


def path_component_rejection(value: object) -> str | None:
    """`value` 가 POSIX·Windows 공통 안전한 **단일 경로 마디**가 아니면 사유, 맞으면 `None`.

    거부 축: 빈 값/문자열 아님 · 앞뒤 공백 · 경로구분자(`/`·`\\`) · Windows 금지 문자
    (드라이브 표기 `C:` 의 콜론 포함) · 제어문자 · `.`/`..`(현재/상위 마디) · 끝의 `.`/공백
    (Windows 가 조용히 잘라 다른 마디가 된다) · 예약 장치 이름(`CON`·`NUL`·`COM1`…).

    슬롯 키 축(`is_slot_key`)과 슬롯 경로 축(`worktree_pool._normalize_slot`)이 이 한 함수를
    공유한다 — 사유 문자열을 돌려주므로 각 축이 자기 문맥의 에러 메시지에 그대로 실을 수 있다.
    """
    if not isinstance(value, str) or not value:
        return "빈 값(경로 마디가 아니다)"
    if value != value.strip():
        return f"{value!r} — 앞뒤 공백은 경로 마디로 안전하지 않다"
    for separator in _PATH_COMPONENT_SEPARATORS:
        if separator in value:
            return f"{value!r} — 경로구분자({separator!r})를 포함한 값은 단일 마디가 아니다"
    bad = sorted({ch for ch in value if ch in _PATH_COMPONENT_FORBIDDEN_CHARS})
    if bad:
        return (f"{value!r} — Windows 금지 문자({''.join(bad)})를 포함한다"
                "(드라이브 표기 `C:` 포함)")
    if any(ord(ch) < 32 for ch in value):
        return f"{value!r} — 제어문자를 포함한다"
    if value in _PATH_COMPONENT_DOT_NAMES:
        return f"{value!r} — 현재/상위 마디는 이름이 아니다"
    if value.endswith(".") or value.endswith(" "):
        return f"{value!r} — 끝의 `.`/공백은 Windows 가 잘라 다른 마디가 된다"
    if value.split(".", 1)[0].upper() in _PATH_COMPONENT_RESERVED_NAMES:
        return f"{value!r} — Windows 예약 장치 이름이다"
    return None


def is_safe_path_component(value: object) -> bool:
    """`value` 가 POSIX·Windows 공통 안전한 단일 경로 마디인가 (순수 값 술어·IO 0)."""
    return path_component_rejection(value) is None


# ── 슬롯 정체성 값 술어 (경로 형태 강제 아님) ──────────────────────────────
# 슬롯 정체성 키 = `<repo>_<N>`(N≥1). 이 술어는 **장부 값이 슬롯 축 정체성인지**를 가른다 —
# 경로가 어떤 모양이어야 한다는 강제가 아니라, 주어진 *값*(행의 session·경로 마지막 마디)이
# 슬롯 키로 쓸 수 있는지의 판정이다. 키는 `.local/slots/<키>/pm_state.md` 디렉토리명·log 헤더
# 태그 `(<키>)`·handoff ack 토큰으로 그대로 쓰이므로 **경로 마디 안전**(`path_component_
# rejection` — 두 축 공유)에 더해 태그 delimiter 안전 문자만 허용한다(공백·괄호 거부·선두
# alnum). task 이름(`main`)·`<host>-<pid>` 임시 명명·무소유 빈 session 은 이 형식이 아니라
# 슬롯 정체성이 아니다.
_SLOT_KEY_RE = re.compile(r"^[A-Za-z0-9][^\s()/\\]*_[1-9]\d*$")

# 경로 값의 마지막 마디를 뽑는 구분자 — POSIX/Windows 표기를 **OS 무관**하게 같은 값으로 읽는다
# (`Path(...).name` 은 실행 OS 의 flavour 를 타 같은 장부가 OS 마다 다르게 읽힌다).
_SLOT_PATH_SEPARATORS = re.compile(r"[\\/]")


def is_slot_key(value: object) -> bool:
    """`value` 가 슬롯 축 정체성 키(`<repo>_<N>`)인가 — 순수 값 술어(IO 0).

    두 관문을 모두 통과해야 한다: (1) **경로 마디 안전**(`path_component_rejection` — 슬롯 경로
    축과 공유하는 같은 술어), (2) 태그/키 문법(`<repo>_<N>`·선두 alnum·공백/괄호 없음). (1)이
    없으면 `C:_1` 같은 드라이브 표기가 키로 통과해 `.local/slots/<키>` 결합에서 상태 루트를
    벗어난다(리뷰 F-003).
    """
    return (isinstance(value, str)
            and is_safe_path_component(value)
            and _SLOT_KEY_RE.fullmatch(value) is not None)


def slot_number_for_repo(repo: str, value: object) -> int | None:
    """`<repo>_<N>` 값에서 슬롯 번호 N — 아니면 None (순수 값 술어).

    슬롯 **번호** 축(“다음 worktree 를 무슨 이름으로 만드나”)의 단일 규칙이다. `identity_args.
    repo_slot_numbers`·`worktree_pool._existing_slot_numbers` 가 이 한 함수를 공유해 번호 계상이
    갈리지 않게 한다.
    """
    m = re.fullmatch(rf"{re.escape(repo)}_(\d+)", str(value or ""))
    return int(m.group(1)) if m else None


def normalize_slot_value(value: object) -> str:
    """장부 슬롯 경로 값 표기 정규화 — POSIX 단일·후행 구분자 제거(대조 전용·IO 0).

    같은 슬롯이 `work/A_1`·`work/A_1/`·`work\\A_1` 로 적혀도 한 값으로 대조된다. 파일시스템을
    건드리지 않으며(resolve 없음) 경로 의미를 바꾸지 않는다 — 순전히 문자열 대조 키다.
    """
    text = str(value or "").strip().replace("\\", "/")
    while len(text) > 1 and text.endswith("/"):
        text = text[:-1]
    return text


def slot_path_name(value: object) -> str:
    """슬롯 경로 값의 마지막 마디(OS 무관) — `work/A_1`→`A_1` · `.`→`` · `D:/x/A_1`→`A_1`."""
    text = normalize_slot_value(value)
    if not text:
        return ""
    return _SLOT_PATH_SEPARATORS.split(text)[-1]


def _row_slot_numbers(repo: str, slot: object, session: object) -> set[int]:
    """장부 행 하나가 이 repo 에서 점유한 슬롯 번호 — 경로 값 ∪ session 값(공유 규칙).

    경로 축(`work/<repo>_<N>`)만 세면 경로에 번호가 없는 행(홈 N=1 행 `slot="."`)이 미계상돼
    다음 `alloc` 이 이미 쓰이는 이름(`<repo>_1`)을 다시 판다. 두 값 모두 **장부 행의 값**이라
    파일시스템/추측이 개입하지 않는다.
    """
    nums: set[int] = set()
    slot_text = normalize_slot_value(slot)
    if slot_text.startswith(f"work/{repo}_"):
        number = slot_number_for_repo(repo, slot_text[len("work/"):])
        if number is not None:
            nums.add(number)
    number = slot_number_for_repo(repo, session)
    if number is not None:
        nums.add(number)
    return nums


class SlotResolutionError(Exception):
    """`resolve_actor_workspace` 가 활성 슬롯을 자동 해소할 수 없을 때(≥2 개·모호) — fail-loud.

    `--repo`-단독 폴더 해소(bootstrap 회귀 cwd·worktree_pool dev/sync) 전용 규칙(결정
    3) — 원 `pm_bootstrap.SlotResolutionError`(session-entry 용·default-1 규칙 포함)와 이름은
    같으나(의미 보존·단일 진실로 수렴 예정) 이 모듈의 판정은 더 단순하다: **1개면 해소·0개/≥2개는
    각각 None/raise** — default-1 규칙(slot 1 우선)은 여기 적용하지 않는다(그건 session-entry
    전용 관심사).
    """


class SlotPathResolution:
    """번호→경로 해소 결과 — 경로와 **판정 사유**를 함께 나른다.

    `status` 는 `SLOT_PATH_OK`(경로 해소) / `SLOT_PATH_LEDGER_MISSING` / `SLOT_PATH_LEDGER_
    UNREADABLE` / `SLOT_PATH_ROW_ABSENT`(정상 장부에 그 번호 행 없음) / `SLOT_PATH_CONFLICT`
    (같은 번호를 서로 다른 경로가 주장 — 모호) 중 하나다. 부재/읽기불능/행 부재는 호출부의 기존
    canonical 폴백이 정당하지만 **모순은 폴백 대상이 아니다** — 그 구분이 없으면 "장부를 못
    읽었다"와 "장부가 두 답을 준다"가 같은 `None` 으로 접혀 모호가 조용히 추측으로 통과한다
    (리뷰 F-004).

    (dataclass 미사용 — `Identity`·`Workspace` 와 같은 로더 제약.)
    """

    def __init__(self, status: str, path: str | None = None, detail: str = ""):
        self.status = status
        self.path = path
        self.detail = detail

    def __repr__(self) -> str:
        return (f"SlotPathResolution(status={self.status!r}, path={self.path!r}, "
                f"detail={self.detail!r})")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SlotPathResolution):
            return NotImplemented
        return (self.status, self.path, self.detail) == (
            other.status, other.path, other.detail)


SLOT_PATH_OK = "ok"
SLOT_PATH_LEDGER_MISSING = LEDGER_MISSING
SLOT_PATH_LEDGER_UNREADABLE = LEDGER_UNREADABLE
SLOT_PATH_ROW_ABSENT = "row-absent"
SLOT_PATH_CONFLICT = "conflict"


# ── 슬롯 정체성 해소 (장부 값 1순위 · 경로 재구성 금지) ──────────
# 슬롯의 **정체성 키**(`.local/slots/<키>/pm_state.md` 디렉토리 · log 헤더 태그 `(<키>)` ·
# handoff ack 토큰)를 한 지점에서 해소한다. 지금까지는 소비 지점마다 경로 basename 을 각자
# 파싱했고(`pm_handoff._parse_worktree_slot`·`worktree_pool.slot_pm_state_file`), 그래서 경로에
# 이름이 없는 슬롯(경로 마디가 없는 행)은 어느 소비자도 해소하지 못했다.

IDENTITY_FROM_LEDGER_SESSION = "ledger-session"   # 장부 행 session 값이 정체성을 줬다.
IDENTITY_FROM_SLOT_PATH = "slot-path"             # 장부 정체성 부재 — pool 명명 규약(경로 마디).


class SlotIdentity:
    """슬롯 해소 결과 — 정체성 키(`key`)와 실제 경로(`slot`)를 함께 나른다.

    `key` = `<repo>_<N>` 상태/태그/ack 키 · `slot` = 장부/호출부가 준 경로 값 그대로 ·
    `repo`/`number` = **장부 행의 repo 와 그 repo 기준 행 번호**(장부에 행이 없으면 pool 명명
    규약대로 키 분해) · `session` = 장부 행 session 원값(경로 규약으로 해소했으면 None) ·
    `source` = 어느 층이 해소했는지(진단·테스트 단언).

    **`repo`/`number` 를 키에서 뜯지 않는 이유**: 키는 그 슬롯의 *정체성*이고 트리거
    `--repo <repo> --slot <N>` 은 그 슬롯을 다시 **찾는** 좌표다. 둘을 같은 문자열에서 뽑으면
    session 과 행 repo 가 어긋난 장부(`repo=A · slot=work/A_1 · session=B_7`)에서 `--repo B
    --slot 7` 이라는 **재접속 불가능한 지시**가 나온다(리뷰 F-002 실측 — 즉시 재해소가 세션↔repo
    조인 불일치로 거부된다).
    좌표는 장부 행에서 오고, 행이 좌표를 주지 못하면 `number=None`(명시 트리거를 만들지 않는다 —
    틀린 지시를 내느니 내지 않는다).

    (dataclass 미사용 — `Identity`·`Workspace` 와 같은 이유: `spec_from_file_location` 로드 +
    `from __future__ import annotations` 결합에서 forward-ref 해소가 깨진다.)
    """

    def __init__(self, key: str, slot: str, source: str, session: str | None = None,
                 repo: str | None = None, number: int | None = None):
        self.key = key
        self.slot = slot
        self.source = source
        self.session = session
        self.repo = repo
        self.number = number

    def __repr__(self) -> str:
        return (f"SlotIdentity(key={self.key!r}, slot={self.slot!r}, "
                f"source={self.source!r}, session={self.session!r}, "
                f"repo={self.repo!r}, number={self.number!r})")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SlotIdentity):
            return NotImplemented
        return (self.key, self.slot, self.source, self.session, self.repo, self.number) == (
            other.key, other.slot, other.source, other.session, other.repo, other.number)


class SlotIdentityResolution:
    """슬롯 정체성 해소 결과 — 정체성과 **판정 사유**를 함께 나른다.

    `status`:
      - `SLOT_IDENTITY_OK` — 해소됨(`identity` 비어 있지 않음·어느 층인지는 `identity.source`).
      - `SLOT_IDENTITY_CONFLICT` — 같은 슬롯을 서로 다른 슬롯 키 session 이 주장(**장부 모순**).
        `identity` 는 `None` 이다 — 판정 불능을 판정으로 위장하지 않는다.
      - `SLOT_IDENTITY_LEDGER_MISSING` / `..._UNREADABLE` / `..._ROW_ABSENT` — 장부가 답을 주지
        못한 세 사유(부재 / 손상 / 정상인데 그 슬롯 행 없음)이고 경로 규약도 키를 못 줬다.
      - `SLOT_IDENTITY_NO_SLOT` — 입력 슬롯 값 자체가 비었다.

    `fallback` = 모순/미해소일 때 **읽기 표시**가 쓸 수 있는 pool 명명 규약(경로 마디) 정체성.
    쓰기 진입은 이 값을 쓰지 않는다 — `status` 로 fail-loud 한다.
    """

    def __init__(self, status: str, identity: "SlotIdentity | None" = None,
                 detail: str = "", fallback: "SlotIdentity | None" = None):
        self.status = status
        self.identity = identity
        self.detail = detail
        self.fallback = fallback

    def __repr__(self) -> str:
        return (f"SlotIdentityResolution(status={self.status!r}, "
                f"identity={self.identity!r}, detail={self.detail!r})")


SLOT_IDENTITY_OK = "ok"
SLOT_IDENTITY_NO_SLOT = "no-slot"
SLOT_IDENTITY_LEDGER_MISSING = LEDGER_MISSING
SLOT_IDENTITY_LEDGER_UNREADABLE = LEDGER_UNREADABLE
SLOT_IDENTITY_ROW_ABSENT = "row-absent"
SLOT_IDENTITY_CONFLICT = "conflict"

# 쓰기 진입이 fail-loud 로 번역해야 하는 판정 — "장부가 두 답을 준다"는 폴백 대상이 아니다.
SLOT_IDENTITY_FAIL_LOUD_STATUSES = (SLOT_IDENTITY_CONFLICT,)


def _identity_coordinates(key: str, rows: "list[dict]") -> "tuple[str | None, int | None]":
    """정체성 키의 재접속 좌표 `(repo, number)` — **장부 행 값 1순위**, 행이 없으면 키 분해.

    행이 있으면 repo 는 행의 `repo` 값이고 번호는 그 repo 기준 행 번호(`_row_slot_numbers` —
    경로 값 ∪ session 값). 행이 아예 없는 슬롯은 pool 명명 규약이 유일한 진실이라 키를 분해한다
    (그 형상은 호출부의 canonical 폴백이 그대로 왕복한다). 행이 좌표를 주지 못하면 `(repo, None)`
    — 명시 트리거를 만들지 않는다는 신호다.
    """
    if not rows:
        head, separator, tail = key.rpartition("_")
        if separator and head and tail.isdigit():
            return head, int(tail)
        return None, None
    repos = {row.get("repo") for row in rows
             if isinstance(row.get("repo"), str) and row.get("repo")}
    if len(repos) != 1:
        return None, None   # 행이 repo 를 안 주거나 갈린다 — 좌표 없음(트리거 미생성).
    repo = str(next(iter(repos)))
    numbers: set[int] = set()
    for row in rows:
        numbers |= _row_slot_numbers(repo, row.get("slot"), row.get("session"))
    return repo, (next(iter(numbers)) if len(numbers) == 1 else None)


# ── 작업공간(slot) 2단 해소 — task-aware ──────────
# 실행 위치가 필요한 도구(regression run·livegate record·ticket_finish·dev-delegate)가
# **어느 worktree 에서 도는지**를 task 보유 슬롯 중에서 특정한다. 위 `resolve_actor_workspace`
# (`--repo` 단독·활성 lease 유일해소)와 판정 축이 다르다 — 이건 **task 축**(lease.session
# == task 이름)이라 "내 task 가 보유한 슬롯"을 본다. cwd 는 해소에 **비참여** —
# 순전히 리스 장부 + 명시 인자(`--repo`/`--slot`/`--task`)로만 판정한다.

_LEASE_ROLE_READONLY = "readonly"   # role="readonly" 공유 슬롯 — 무소유가 정상·소유검사 예외.


class Workspace:
    """작업공간 해소 결과 — 실행 위치(slot·절대경로 surface 소스) + 슬롯 메타.

    `resolve_task_workspace` 가 반환한다. `slot` = "work/<repo>_<N>"(worktree_pool.slot_path 와
    같은 상대형 — caller 가 `REPO / slot` 으로 **절대경로 surface**). `repo` = 그 repo. `session` =
    그 슬롯 lease.session(task-mode 는 task 이름·readonly 공유 슬롯은 무소유라 None 일 수 있음).
    `test_cmd` = 슬롯 바인딩 회귀명령(None=미바인딩·caller 가 다음 레이어 폴백). `readonly` =
    role="readonly" 공유 자산이라 소유검사를 우회했는지.

    (dataclass 미사용 — `Identity`·`worktree_pool.Lease` 와 동일: `spec_from_file_location` 로드 시
    `from __future__ import annotations` 결합으로 forward-ref 해소가 깨진다. 평범한 클래스로 회피.)
    """

    def __init__(self, slot: str, repo: str | None, session: str | None,
                 test_cmd: str | None = None, readonly: bool = False):
        self.slot = slot
        self.repo = repo
        self.session = session
        self.test_cmd = test_cmd
        self.readonly = readonly

    def __repr__(self) -> str:
        return (f"Workspace(slot={self.slot!r}, repo={self.repo!r}, session={self.session!r}, "
                f"test_cmd={self.test_cmd!r}, readonly={self.readonly!r})")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Workspace):
            return NotImplemented
        return (self.slot, self.repo, self.session, self.test_cmd, self.readonly) == (
            other.slot, other.repo, other.session, other.test_cmd, other.readonly)


class WorkspaceResolutionError(Exception):
    """작업공간 해소 실패 — 미보유·모호(≥2)·`--slot` 단독 등.

    같은 repo 슬롯 ≥2 를 허용하되 **유일=자동 해소·모호=에러**(첫번째/최근 암묵 선택
    금지·[[mechanize-dont-instruct-llm]]). caller(board·ticket_finish)가 자기 관례(`[중단]` 접두)로
    surface 한다. `SlotResolutionError`(slot-mode `--repo` 단독)와 **별개** — 이건 task-mode
    작업공간 전용이라 판정 축(내 task 보유)이 다르다.
    """
