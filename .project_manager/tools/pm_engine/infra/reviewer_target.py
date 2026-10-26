"""추가 리뷰어 대상과 프로필 해소 (층: infra)."""
from __future__ import annotations

from typing import Callable
from typing import NamedTuple
from pathlib import Path
import argparse
from pm_engine.domain import conf_schema
from pm_engine.infra import conf_store
from pm_engine.infra.harness import contract
from pm_engine.infra.harness import delegate_env
from pm_engine.domain import delegate_errors
from pm_engine.infra import process
import re
from pm_engine.domain import reviewer_budget
from pm_engine.domain import reviewer_result
import shlex
import sys
from pm_engine.infra.harness import watchdog



class ReviewerTarget(NamedTuple):
    """이번 실행이 실제로 부를 추가 리뷰어 — 세 표면이 공유하는 단일 해소 결과.

    `command` = 세 표면(dry-run·stderr provenance·raw 헤더)이 **같은 문자열로** 말하는 정체이며
    구조화 tuple 의 argv 를 그대로 렌더한 값이다(다른 출처는 없다).
    """

    source: str
    command: str
    harness: str | None = None
    model: str | None = None
    reasoning: str | None = None

    @property
    def structured(self) -> bool:
        """구조화 transport(하네스 argv 조립)를 태울 수 있는가.

        출처 라벨만으로 판정하면 **해소를 지나지 않아 harness 가 None 인** 대상(부분 구성)이
        구조화로 읽혀 argv 조립 단계에서 `harness=None` 으로 죽는다. 조립에 필요한 축이 실제로
        해소됐는지를 함께 본다.
        """
        return self.source == reviewer_result.REVIEWER_SOURCE_STRUCTURED and self.harness is not None

    @property
    def name(self) -> str:
        """시간 프로필·진행신호·raw 파일명이 공유하는 정규화 키."""
        return reviewer_name(self.command)

    @property
    def ledger_model(self) -> str:
        """이 실행의 **모델 정체** — 네 표면(dry-run·stderr·raw 헤더·raw 장부)이 공유하는 단일 값.

        **명시 모델 그대로**다. 커맨드 문자열에서 역추론하지 않는다(그러면 명시 모델이 argv 표기
        규칙에 따라 `default` 로 퇴화할 수 있다)."""
        return self.model or ""

    @property
    def profile_tail(self) -> str:
        """해소 tuple 의 출처/모델 축 — provenance 문자열의 공통 꼬리.

        모델 축은 장부·raw 헤더와 **같은 정체 값**(`ledger_model`)을 쓴다."""
        return (f"source={self.source}, harness={self.harness}, "
                f"model={self.model}, reasoning={self.reasoning}")


def _structured_reviewer_argv(
    harness: str, model: str, reasoning: str | None, *,
    cwd: str, prompt_file: str,
) -> list[str]:
    """구조화 대상의 argv — 공용 드라이버 계약(`contract`)만으로 조립한다.

    모델·reasoning 은 **항상 argv 에 명시**되고 권한축은 code-reviewer(읽기)로 고정된다. 사용자
    config 기본값(모델·effort)이 실제 수신자를 바꿀 수 없는 이유가 이것이다 — 명시 플래그가
    config 기본값을 이긴다.

    codex/claude 는 프로세스 cwd 를 그대로 쓰므로 실행마다 달라지는 토큰이 argv 에
    없다. opencode 만 subprocess cwd 를 무시해 `--dir` 이 필요하고 프롬프트도 `--file` 첨부라,
    그 두 자리는 자리표시자로 렌더한 뒤 실행 시점에 실 경로로 조립한다.
    """
    if harness == "codex":
        return contract.build_codex_argv(model, reasoning, reviewer_result.REVIEWER_ROLE)
    if harness == "claude":
        return contract.build_claude_argv(model, reasoning, reviewer_result.REVIEWER_ROLE)
    return contract.build_opencode_argv(model, reasoning, reviewer_result.REVIEWER_ROLE, cwd, prompt_file)


def resolve_reviewer_target(conf: dict[str, str]) -> ReviewerTarget:
    """`additional_reviewer.{harness,model,reasoning}` 을 **원자로** 해소한다.

    · 구조화 키가 **하나도 없으면** 대상이 없다 — fail-loud 다(엔진 기본 커맨드로 조용히 나가지
      않는다·모델 미고정 실행 경로 폐지).
    · 구조화 키가 하나라도 **있으면**(값이 비어 있어도 선언이다) harness/model 동반 필수 —
      부분 지정은 fail-loud 다(조용한 폴백·절반만 반영된 대상 금지). 선언 판정을 값의 truthiness
      로 하면 `additional_reviewer.harness=` 처럼 **비운 채 선언한 부분 tuple** 이 "미설정" 으로
      읽혀 진단이 엉뚱해진다. 그래서 기준은 **키 존재**다.
      `reasoning` 만 빈 값이 허용되고(선택 축·플래그 생략), 그것도 harness/model 이 온전할 때다.
    · model 이 **예약 sentinel**(`RESERVED_MODEL_VALUES` — 엔진이 '모델 미고정'을 뜻하는 낱말)이면
      거부한다. 값이 비어있지 않다는 것만으로 통과시키면 `additional_reviewer.model=default` 가
      "고정했다"는 선언과 함께 미고정 라벨을 장부에 박아, 감사가 자기 모순을 기록한다.
    · harness 값 검증은 공용 드라이버 계약(`contract`)이 소유한다. reasoning 은 정리만 하고, 그 모델이
      받는 값인지는 실행 직전 `check_reviewer_reasoning` 이 실행 cwd·env 로 확인한다(이 함수는
      비교용 `reviewer_profile_config` 에서도 불린다 — 거기서 모델 목록을 조회하지 않는다).

    모든 거부는 **호출·라운드 예약·raw 예약 전**에 성립한다 — 호출부가 이 함수를 그 게이트들
    앞에서 부르고, 이 함수 자체는 부작용이 없다.
    """
    present = tuple(key for key in reviewer_result.ADDITIONAL_REVIEWER_KEYS if key in conf)
    declared = {
        key: (conf.get(key) or "").strip()
        for key in reviewer_result.ADDITIONAL_REVIEWER_KEYS
    }
    if not present:
        raise delegate_errors.ReviewerTargetError(
            "추가 리뷰어 대상이 없습니다 — local.conf 에 "
            f"`{reviewer_result.ADDITIONAL_REVIEWER_HARNESS_KEY}`·`{reviewer_result.ADDITIONAL_REVIEWER_MODEL_KEY}` 를 "
            f"설정하세요(선택: `{reviewer_result.ADDITIONAL_REVIEWER_REASONING_KEY}`). 엔진 기본 커맨드로 "
            "조용히 나가지 않습니다 — 모델을 고정하지 않은 실행은 '어느 모델이 판정했는가'를 "
            "사후에 확정할 수 없습니다."
        )

    harness = declared[reviewer_result.ADDITIONAL_REVIEWER_HARNESS_KEY]
    model = declared[reviewer_result.ADDITIONAL_REVIEWER_MODEL_KEY]
    if not harness or not model:
        missing = ", ".join(
            key for key in (reviewer_result.ADDITIONAL_REVIEWER_HARNESS_KEY, reviewer_result.ADDITIONAL_REVIEWER_MODEL_KEY)
            if not declared[key]
        )
        raise delegate_errors.ReviewerTargetError(
            f"추가 리뷰어 프로필이 불완전합니다({missing} 부재/빈 값) — harness/model 은 동반 "
            "필수인 원자 tuple 입니다. 부분 설정으로 조용한 기본값을 쓰지 않습니다. local.conf 에 "
            f"`{reviewer_result.ADDITIONAL_REVIEWER_HARNESS_KEY}`·`{reviewer_result.ADDITIONAL_REVIEWER_MODEL_KEY}` 를 함께 "
            f"설정하세요(선택: `{reviewer_result.ADDITIONAL_REVIEWER_REASONING_KEY}`)."
        )
    if model.lower() in reviewer_result.RESERVED_MODEL_VALUES:
        raise delegate_errors.ReviewerTargetError(
            f"`{reviewer_result.ADDITIONAL_REVIEWER_MODEL_KEY}={model}` 은 예약 sentinel 입니다 — 엔진이 '모델 "
            f"미고정'을 표시하는 낱말이라(예약: {', '.join(sorted(reviewer_result.RESERVED_MODEL_VALUES))}) "
            "구조화 프로필의 모델 값이 될 수 없습니다. 구조화 프로필은 정의상 모델을 고정한 "
            "tuple 인데 이 값을 받으면 raw 장부·raw 헤더·stderr provenance 가 legacy 미고정 실행과 "
            "구분되지 않아 '어느 모델이 이 판정을 냈는가'를 사후에 확정할 수 없습니다.\n"
            f"  · 실제 모델 이름을 적으세요(예: `{reviewer_result.ADDITIONAL_REVIEWER_MODEL_KEY}=gpt-5.6-sol`).\n"
            "  · 모델 미고정 실행 경로는 폐지됐습니다 — 실행하려면 모델을 고정해야 합니다."
        )

    try:
        harness = contract.validate_harness(harness)
    except contract.HarnessContractError as exc:
        raise delegate_errors.ReviewerTargetError(
            f"추가 리뷰어 프로필 값 오류 — {exc}"
        ) from exc
    reasoning = contract.normalize_reasoning(declared[reviewer_result.ADDITIONAL_REVIEWER_REASONING_KEY])

    command = shlex.join(_structured_reviewer_argv(
        harness, model, reasoning,
        cwd=reviewer_result.REVIEWER_CWD_PLACEHOLDER, prompt_file=reviewer_result.REVIEWER_PROMPT_FILE_PLACEHOLDER,
    ))
    return ReviewerTarget(
        source=reviewer_result.REVIEWER_SOURCE_STRUCTURED, command=command,
        harness=harness, model=model, reasoning=reasoning,
    )


def check_reviewer_reasoning(target: ReviewerTarget, *, cwd: Path) -> None:
    """실행 직전 확인 — 대상의 reasoning 이 그 하네스·모델의 허용집합 원소인가(밖이면 ReviewerTargetError).

    허용집합은 실행과 같은 cwd·env(`delegate_env.build_env`)로 해소한다 — opencode 는 그 조건의 모델
    목록 조회 결과가 정한다. 추론 값이 없으면 조회하지 않는다."""
    if target.reasoning is None:
        return
    try:
        contract.validate_reasoning(
            target.harness, target.reasoning, model=target.model, cwd=cwd,
            env=delegate_env.build_env(target.harness))
    except contract.HarnessContractError as exc:
        raise delegate_errors.ReviewerTargetError(f"추가 리뷰어 프로필 값 오류 — {exc}") from exc


def _split_reviewer_argv(reviewer_cmd: str) -> list[str]:
    """`reviewer_cmd` 를 **실행 플랫폼 규칙**으로 argv 분해한다 (분해 규칙은 `pm_engine.infra.process` 소유).

    `shlex.split` 를 직접 부르면 POSIX 규칙이라 Windows 실행 경로의 ``\\`` 가 escape 로 소비된다 —
    ``C:\\Users\\pm\\codex.exe`` 가 ``C:Userspmcodex.exe`` 로 뭉개져 확정 기동 실패로 끝나고, 그
    실행은 "리뷰어를 찾을 수 없음"으로 흡수돼 교차검증이 조용히 환불된다. 분해 규칙 사본을 여기
    만들지 않고 `pm_engine.infra.process.split_command_argv` 하나를 쓴다(같은 보호 규칙·같은 복원).
    """
    return process.split_command_argv(
        reviewer_cmd, windows=contract.running_on_windows(),
    )


def _normalized_reviewer_key(argv: list[str]) -> str:
    """리뷰어 실행파일을 공유 프로필 키로 정규화한다.

    Windows 실행파일 접미사/대소문자/경로 구분자를 여기서 한 번만 처리한다. 진행신호 계약과 시간
    프로필이 이 키를 함께 써야 `codex.exe`의 진행은 codex로 보면서 시간만 미지 GPU 축으로 잡는
    비대칭이 생기지 않는다.
    """
    if not argv:
        return "reviewer"
    executable = argv[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    for suffix in (".exe", ".cmd", ".bat"):
        if executable.endswith(suffix):
            executable = executable[:-len(suffix)]
            break
    return re.sub(r"[^a-z0-9_.-]", "_", executable) or "reviewer"


def _reviewer_progress_signal(reviewer_cmd: str, conf: dict[str, str]) -> str:
    """실행 파일 basename과 그 CLI의 **옵션 계약** 또는 명시 설정으로 진행 신호를 판정한다.

    이름만 같거나 스트리밍처럼 보이는 플래그만 있다는 이유로 추론하지 않는다. 알려진 실행 파일의
    알려진 옵션 조합만 계약으로 인정한다. 미지 CLI/형식은 신호 없음으로 두며, 자유 문자열 커맨드는
    local.conf 명시 키로만 선언할 수 있다.
    """
    explicit = (conf.get(reviewer_budget.ADDITIONAL_REVIEWER_PROGRESS_SIGNAL_KEY) or "").strip()
    if explicit:
        if explicit in conf_schema.PROGRESS_SIGNAL_KINDS:
            return explicit
        print(
            f"경고: local.conf {reviewer_budget.ADDITIONAL_REVIEWER_PROGRESS_SIGNAL_KEY}={explicit!r} 은 "
            f"{sorted(conf_schema.PROGRESS_SIGNAL_KINDS)} 중 하나가 아님 — argv 형식 판정 사용.",
            file=sys.stderr,
        )
    try:
        argv = _split_reviewer_argv(reviewer_cmd)
    except ValueError:
        return conf_schema.PROGRESS_SIGNAL_NONE
    if not argv:
        return conf_schema.PROGRESS_SIGNAL_NONE

    executable = _normalized_reviewer_key(argv)
    contract = reviewer_budget._REVIEWER_PROGRESS_CONTRACTS.get(executable)
    if contract is None:
        return conf_schema.PROGRESS_SIGNAL_NONE

    def option_value(name: str) -> str | None:
        for index, token in enumerate(argv[1:], start=1):
            if token == name and index + 1 < len(argv):
                return argv[index + 1].strip().lower()
            prefix = name + "="
            if token.startswith(prefix):
                return token[len(prefix):].strip().lower()
        return None

    for flag, signal_attr in contract["flags"].items():
        if flag in argv[1:]:
            return getattr(conf_schema, signal_attr)
    for (option, expected), signal_attr in contract["options"].items():
        if option_value(option) == expected:
            return getattr(conf_schema, signal_attr)
    return getattr(conf_schema, contract["default"])


def reviewer_profile(home, reviewer_cmd: str, conf: dict[str, str] | None = None):
    """리뷰어 커맨드 → 시간값 프로필 + 출력형식 기반 진행신호 프로필.

    시간값은 실행 파일 축의 공유 테이블을 쓰되, 진행 신호는 전체 argv/명시 설정으로 독립 판정한다.
    conf 미지정 호출도 local.conf를 읽어 `delegate_env.harness_profile`과 대칭이다.
    """
    if conf is None:
        try:
            conf = conf_store.local_config(home)
        except OSError:
            conf = {}
    profile = watchdog.resolve_harness_profile(
        reviewer_name(reviewer_cmd), conf,
        fallback=watchdog.REVIEWER_FALLBACK_PROFILE,
        legacy_idle_key=reviewer_budget.ADDITIONAL_REVIEWER_IDLE_TIMEOUT_KEY,
        legacy_wall_key=reviewer_budget.ADDITIONAL_REVIEWER_TIMEOUT_KEY,
    )
    return profile._replace(
        progress_signal=_reviewer_progress_signal(reviewer_cmd, conf)
    )


def _resolve_timeout(home, args: argparse.Namespace, conf: dict[str, str],
                     reviewer_cmd: str) -> int:
    """추가 리뷰 **벽시계 백스톱**(초)을 `--timeout` > 리뷰어 프로필 순서로 해소한다.

    CLI 양수값은 argparse 검증을 통과한 명시 override다. 그 아래(legacy flat conf →
    `harness.<reviewer>.wall_timeout` → 선언 기본)는 `watchdog.resolve_harness_profile` 이 소유한다
    — 깨진 conf 값은 거기서 stderr 경고 후 fail-soft. delegate_env._resolve_timeout 과 같은 계약이다.
    """
    if args.timeout is not None:
        return args.timeout
    return int(reviewer_profile(home, reviewer_cmd, conf).wall_timeout)



def _timeout_seconds_arg(raw: str) -> int:
    """CLI timeout 을 `watchdog` 의 단일 정규화 경계로 파싱한다."""
    value = watchdog.normalize_timeout_seconds(raw)
    if value is None:
        raise argparse.ArgumentTypeError("유한한 정수 초(최소 1)여야 합니다")
    return value


def _configured_paths(conf: dict[str, str]) -> list[str]:
    """검토 경로 — 선언이 없으면 레지스트리 기본값을 **같은 문법**으로 나눈다(파서 하나)."""
    raw = conf.get("additional_reviewer.paths", "").strip() or reviewer_result._conf_default(
        "additional_reviewer.paths")
    return [p for p in re.split(r"[,\s]+", raw) if p]


def _incomplete_round_limit(conf: dict[str, str]) -> int:
    """판정 없는 호출의 별도 재시도 상한(미설정·손상은 레지스트리 기본값)."""
    default = reviewer_result._conf_default_int(reviewer_budget.ADDITIONAL_REVIEWER_INCOMPLETE_ROUND_LIMIT_KEY)
    raw = reviewer_result._knob_raw(conf, reviewer_budget.ADDITIONAL_REVIEWER_INCOMPLETE_ROUND_LIMIT_KEY)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def _wave_budget(conf: dict[str, str]) -> int:
    """wave 총 라운드 예산 (local.conf additional_reviewer.wave_budget·미설정은 레지스트리 기본값).

    비정수·음수는 기본값으로 fail-soft — 라운드 상한 노브와 같은 규칙이다(깨진 노브가 게이트를
    벽돌로 만들지 않는다)."""
    default = reviewer_result._conf_default_int(reviewer_budget.ADDITIONAL_REVIEWER_WAVE_BUDGET_KEY)
    raw = reviewer_result._knob_raw(conf, reviewer_budget.ADDITIONAL_REVIEWER_WAVE_BUDGET_KEY)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def _review_rounds_max(conf: dict[str, str]) -> int:
    """수렴-형상 라운드 상한 (local.conf `additional_reviewer.rounds_max`·미설정은 레지스트리 기본값).

    비정수·음수는 기본값으로 fail-soft — 다른 예산 노브와 같은 규칙이다(깨진 노브가 게이트를
    벽돌로 만들지 않는다)."""
    default = reviewer_result._conf_default_int(reviewer_budget.REVIEW_ROUNDS_MAX_KEY)
    raw = conf.get(reviewer_budget.REVIEW_ROUNDS_MAX_KEY, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default



def reviewer_name(reviewer_cmd: str) -> str:
    """reviewer_cmd 의 공유 정규화 키(시간 프로필·진행신호·파일명/요약 공통)."""
    argv = _split_reviewer_argv(reviewer_cmd)
    return _normalized_reviewer_key(argv)


def _read_local_config(path: Path) -> dict[str, str]:
    """존재가 확인된 비교 대상 local.conf를 공용 로더로 읽는다(구표기 잔존은 fail-loud).

    판독 실패는 **삼키지 않는다**(`load_strict`) — 호출부가 그 예외로 fail-closed 하므로, 빈 conf
    로 강등하면 보호 선언을 확인하지 못한 채 나간다."""
    result = conf_store.load_strict(path)
    conf_schema.assert_no_legacy(result)
    return result.values


def _review_raw_content(
    content: str, local_conf_path: Path | None, resolved_profile: str | None,
    target: ReviewerTarget | None = None,
) -> str:
    """pm_delegate와 같은 `# key: value` 감사 헤더를 additional_reviewer 원문에 붙인다.

    헤더는 **추가만** 한다(기존 두 줄의 위치·표기 불변). 해소 축(출처·harness·model·reasoning·
    실행 커맨드)을 한 줄씩 더해, raw 하나만 봐도 "무엇이 이 판정을 냈는가"가 닫힌다."""
    if local_conf_path is None and resolved_profile is None and target is None:
        return content
    header = ["# additional_reviewer raw 출력 (감사)"]
    if local_conf_path is not None:
        header.append(f"# local_conf: {local_conf_path}")
    if resolved_profile is not None:
        header.append(f"# resolved_profile: {resolved_profile}")
    if target is not None:
        header.append(f"# reviewer_source: {target.source}")
        if target.structured:
            header.extend([
                f"# harness: {target.harness}",
                f"# model: {target.model}",
                f"# reasoning: {target.reasoning}",
            ])
        else:
            # legacy 도 장부와 **같은 정체 값**을 쓴다(표기 없으면 `unpinned-model`).
            header.append(f"# model: {target.ledger_model}")
        header.append(f"# command: {target.command}")
    return "\n".join((*header, "", content))


def reviewer_profile_config(conf: dict[str, str]) -> dict[str, str]:
    """additional_reviewer의 실제 호출 대상 값. 미지정과 명시 default는 같은 값으로 정규화한다.

    비교 대상은 **해소된 대상**이다 — 한쪽이 구조화 tuple 이고 다른 쪽이 legacy 커맨드면 실제
    수신자가 다르므로 커맨드·출처 축이 모두 갈려 loud 하다. 해소 불가능한 대상 conf 는 값 자리에
    사유를 실어 조용히 같아 보이지 않게 한다(비교기는 절대 예외를 올리지 않는다)."""
    try:
        target = resolve_reviewer_target(conf)
    except delegate_errors.ReviewerTargetError as exc:
        return {"reviewer_cmd": f"<해소 실패: {exc}>"}
    values = {"reviewer_cmd": target.command, "reviewer_source": target.source}
    if target.structured:
        values.update({
            "reviewer_harness": str(target.harness),
            "reviewer_model": str(target.model),
            "reviewer_reasoning": str(target.reasoning),
        })
    return values


def _cross_repo_target_conf(
    engine_repo: Path, target_repo: Path | None,
) -> tuple[Path, Path, dict[str, str]] | None:
    """다른 repo conf를 읽되, 진짜 부재만 무소음이고 읽기 실패는 fail-closed로 올린다."""
    if target_repo is None:
        return None
    engine_repo = engine_repo.resolve()
    target_repo = target_repo.resolve()
    if target_repo == engine_repo:
        return None
    # `_local_conf_path()`는 provenance용으로 symlink까지 resolve한다. 존재 판정은 그보다 앞선
    # directory entry 자체를 봐야 dangling symlink를 정상 부재로 오인하지 않는다. target_repo는
    # 위에서 resolve했으므로 이 경로도 절대경로지만 마지막 local.conf symlink는 따라가지 않는다.
    target_conf_path = target_repo / ".project_manager" / "local.conf"
    try:
        # lstat은 dangling symlink도 "존재"로 구분한다. FileNotFoundError만 기존 정상·무소음
        # 대상 conf 부재 축이고, 그 밖의 stat/read/UTF-8 실패는 보호 선언을 확인할 수 없으므로
        # 호출 전에 fail-closed 해야 한다.
        target_conf_path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise reviewer_result.TargetLocalConfReadError(target_conf_path, exc) from exc
    try:
        target_conf = _read_local_config(target_conf_path)
    except (OSError, UnicodeError) as exc:
        raise reviewer_result.TargetLocalConfReadError(target_conf_path, exc) from exc
    return target_repo, target_conf_path, target_conf


def local_conf_divergence(
    *,
    engine_repo: Path,
    engine_conf: dict[str, str],
    target_repo: Path | None,
    selector: Callable[[dict[str, str]], dict[str, object]],
    engine_label: str = "engine",
    target_label: str = "cwd",
) -> reviewer_result.LocalConfDivergence | None:
    """두 repo가 다르고 대상 conf가 있으며 선택된 실제 값이 다를 때만 차이를 반환한다.

    값 동일·대상 conf 부재·cwd repo 미해소는 None(경고 인플레 금지). 비교는 selector가 허용한
    호출 프로필 키만 하므로 test_cmd 같은 무관 per-clone 차이는 loud 조건이 아니다.
    """
    loaded = _cross_repo_target_conf(engine_repo, target_repo)
    if loaded is None:
        return None
    target_repo, target_conf_path, target_conf = loaded
    engine_repo = engine_repo.resolve()

    engine_values = selector(engine_conf)
    target_values = selector(target_conf)
    # selector는 비교할 **유효 프로필**을 완전한 동일 키 집합으로 정규화한다. 해소 불가능한
    # 대상 프로필은 빈 dict라 skip되고, 정상 프로필의 생략 기본값(None 포함)은 공통 키로 비교된다.
    keys = sorted(set(engine_values) & set(target_values))
    differences = tuple(
        f"{key}: {engine_label}={engine_values[key]!r}, "
        f"{target_label}={target_values[key]!r}"
        for key in keys
        if engine_values[key] != target_values[key]
    )
    if not differences:
        return None
    return reviewer_result.LocalConfDivergence(
        engine_repo=engine_repo,
        engine_conf_path=(engine_repo / ".project_manager" / "local.conf").resolve(),
        target_repo=target_repo,
        target_conf_path=target_conf_path,
        differences=differences,
    )


def resolved_reviewer_profile(
    target: ReviewerTarget, timeout: int | None, idle_timeout: float | None,
) -> str:
    """stderr·dry-run·raw 가 **같은 문자열로** 공유하는 추가 리뷰어 해소 tuple.

    앞머리(커맨드·시간 예산)와 뒤의 해소 출처 축(`source=structured` + harness/model/reasoning)이
    **같은 검증된 대상 하나**에서 나온다(커맨드 문자열을 따로 받지 않는다)."""
    return (
        f"(reviewer_cmd={target.command}, wall_timeout_sec={timeout}, "
        f"idle_timeout_sec={idle_timeout}, {target.profile_tail})"
    )
