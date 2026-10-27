"""설치 공통 import_fill (app)."""
from __future__ import annotations

from pathlib import Path
from pm_engine.domain import conf_schema
from pm_engine.domain import home
from pm_engine.domain import install_rules
from pm_engine.domain import render_rules
from pm_engine.infra import fs
from pm_engine.infra import install_files
from pm_engine.infra import install_source
from pm_engine.infra import repo_files
from pm_engine.infra.harness import contract
from pm_engine.infra.harness import watchdog
from typing import Callable
import datetime
import functools
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import shlex

# pm_playbook.local.md 스텁 본문.
# 단일 소스 = 이 인라인 상수. 루트 pm_playbook.local.md는 manifest 밖이라 템플릿
# 트리에 안 끼어 *복사로 안 따라온다* → pm_import 가 import 시 직접 *생성*한다. 별도 `_template`
# 파일을 두지 않는 이유: 그 파일 자체가 manifest/복사 경로에 다시 얽혀 .local 분리 취지와
# 충돌한다 — stdlib-only 인라인 상수가 가장 단순·일관(루트 스텁 형식과 정합: 프런트매터
# type: playbook-local + 인스턴스 소유·manifest 밖 안내 + [[pm_playbook]] 역참조 + TODO 절).
PM_PLAYBOOK_LOCAL_STUB = """\
---
title: PM Playbook — 프로젝트별 누적 학습 (instance)
type: playbook-local
---

# PM Playbook — 프로젝트 누적 학습·도메인 사례

> [[pm_playbook]] (엔진 · `pm_update` 가 자동 갱신하는 **순수 방법론**)의 **프로젝트별 칸**.
> 이 파일은 **인스턴스 소유** — 프레임워크 갱신이 안 건드린다(manifest 밖·tracked).
> 이 프로젝트의 **누적 wave 학습·도메인 사례**를 여기 적는다 (방법론 일반론은 [[pm_playbook]]).

## 누적 wave 학습 (이 프로젝트 고유)

<!-- TODO: 이 프로젝트에서 정착한 wave 운영 학습·도메인 특수 패턴을 누적한다.
  실시간 학습 trail 은 log/current.md entry 가 매체 — 여기엔 *정착된* 패턴만 흡수. -->

## 도메인 사례

<!-- TODO: 이 프로젝트 도메인에 특화된 ticket/wave 사례. 없으면 절 삭제. -->
"""


# 스텁 대상 경로 (dest_root 기준 relpath). 루트 seam과 동일 위치.
PM_PLAYBOOK_LOCAL_RELPATH = Path(".project_manager") / "wiki" / "pm_playbook.local.md"


# ── opencode 모델 결정적 해소 단계 (LLM 아님) ──────────────────────
# board init·conf sync 직후·fill *이전* 의 결정적 단계(sync_local_conf 와 같은 결). opencode
# 어댑터 token({{OPENCODE_PRO_MODEL}})이 이번 복사본에 잔존할 때만 동작한다.
# 해소 순서: 1) --opencode-model 명시 → 치환  2) 없고 stdin tty → `opencode models` 번호목록·선택
# → 치환  3) 없고 비-tty 또는 opencode 부재 → 치환 안 함·TODO 마커(가용목록 인라인)+stderr 경고.
# `opencode models` 가 실제 가용 모델의 단일 진실 — LLM 추측(fill) 대신 결정적 조회를 쓴다.

# `opencode models` 조회 seam — `()` → (성공 여부, provider/model 목록). 테스트가 stub 주입.
ModelsRunner = Callable[[], "tuple[bool, list[str]]"]


class ModelResolveResult:
    """opencode 모델 해소 산출 — 어느 경로로 갔는지·결정된 값·치환 파일 수.

    필드:
      active    : 해소 단계가 동작했는가 (opencode 토큰 잔존 시만 True; claude-only 면 False).
      path      : 'flag' | 'interactive' | 'todo' | 'inactive' — 해소 경로.
      model     : 치환에 쓴 모델 ID (PROVIDER/MODEL) 또는 None(미치환).
      changed   : {{OPENCODE_PRO_MODEL}} 을 치환한 파일 수 (0 = 미치환·TODO 폴백).
      available : `opencode models` 조회 성공 시 가용 모델 목록 (실패/미조회 시 빈 리스트).
      tty       : 해소 시점 stdin 이 tty 였는가 (대화형 가능 여부).
      todos     : TODO 폴백에서 마커를 추가한 토큰 목록.
      note      : 사람 대상 메모 (경고·경로 사유).

    plain class (dataclass 아님): FillResult 와 같은 이유 — 테스트가 spec_from_file_location
    동적 로드 시 dataclass 의 문자열 annotation 해석이 깨진다.
    """

    def __init__(
        self,
        active: bool = False,
        path: str = "inactive",
        model: str | None = None,
        changed: int = 0,
        available: list | None = None,
        tty: bool = False,
        todos: list | None = None,
        note: str = "",
    ):
        self.active = active
        self.path = path
        self.model = model
        self.changed = changed
        self.available = available if available is not None else []
        self.tty = tty
        self.todos = todos if todos is not None else []
        self.note = note

    def __repr__(self) -> str:
        return (f"ModelResolveResult(active={self.active!r}, path={self.path!r}, "
                f"model={self.model!r}, changed={self.changed!r})")


def _real_models_runner(source) -> tuple[bool, list[str]]:
    """설치 대조·콘솔 `[m]` 용 모델 목록 — `contract.model_listing` 을 감싼다. 반환: (성공 여부, 모델 목록).

    조회 실행·판독·시간 상한의 주인은 `contract` 하나다(이 함수에 조회 코드 0). 조회 실패
    (`HarnessContractError` — PATH 부재·rc·timeout·판독 실패·모델 0개)는 그 사유를 stderr 한 줄로 남기고
    (False, []) 로 돌린다 — 설치를 깨지 않고 형식 안내로 강등하는 이 두 소비자의 계약이다. 그 밖의
    예외는 잡지 않는다.
    """
    try:
        listing = contract.model_listing(source, cwd=Path.cwd(), env=dict(os.environ))
    except contract.HarnessContractError as exc:
        print(f"{exc} — 모델 자동해소 skip", file=sys.stderr)
        return False, []
    return True, list(listing.models)


def _substitute_model_token(
    dest_root: Path,
    model: str,
    copied_relpaths: set[Path],
    root_identity: tuple | None = None,
) -> int:
    """{{OPENCODE_PRO_MODEL}} 을 복사 파일 전역에서 결정적 치환. 변경 파일 수 반환.

    substitute_placeholders 와 동일한 copied_relpaths 비파괴 범위·동일 _should_substitute
    제외-판정(새 파일 형식이 자동 편입된다).
    이번 import 가 복사한 파일만 — 복사 안 한 사용자 파일은 절대 안 건드린다.
    대상 = `.opencode/agents/*.md` 의 `model:` 필드·AGENTS.md 잔존분.
    """
    changed = 0
    swapped: list[str] = []
    vanished: list[str] = []
    sed_exclude = install_source._dest_sed_exclude(dest_root)  # 치환 시점·dest manifest 기준(codex must-fix)
    for rel in sorted(copied_relpaths):
        if any(part in install_source.COPY_EXCLUDE_DIR_NAMES for part in rel.parts):
            continue
        if not install_source._should_substitute(rel, sed_exclude):
            continue
        anomaly = install_files._copied_scope_anomaly(dest_root, rel)
        if anomaly == "vanished":
            vanished.append(rel.as_posix())
            continue
        if anomaly == "swapped":
            swapped.append(rel.as_posix())
            continue
        try:
            text = install_files.read_dest_text(dest_root, rel, root_identity=root_identity)
        except install_files.UnsafeDestPathError:
            swapped.append(rel.as_posix())
            continue
        except FileNotFoundError:
            vanished.append(rel.as_posix())  # 읽기 전 삭제도 loud 제외(쓰기 쪽과 동형).
            continue
        except (UnicodeDecodeError, OSError):
            continue
        if install_rules.OPENCODE_MODEL_TOKEN not in text:
            continue
        try:
            install_files.write_dest_text(dest_root, rel, text.replace(install_rules.OPENCODE_MODEL_TOKEN, model),
                            root_identity=root_identity)
        except install_files.UnsafeDestPathError:
            swapped.append(rel.as_posix())
            continue
        except FileNotFoundError:
            vanished.append(rel.as_posix())
            continue
        changed += 1
    install_files._report_copied_scope_anomalies("모델 토큰 치환", swapped, vanished)
    return changed


def _prompt_model_choice(models: list[str], stdin) -> str | None:
    """`opencode models` 번호 목록을 출력하고 stdin 에서 선택을 읽어 모델 ID 를 반환.

    1-based 번호 목록을 stdout 에 출력하고 stdin 한 줄을 읽는다. 유효한 번호면 해당 모델,
    빈 입력·범위 밖·비숫자·EOF 면 None(미선택 → 호출자가 TODO 폴백). 테스트는 io.StringIO 를
    stdin 으로 주입해 결정적으로 검증한다(라이브 입력 없음).
    """
    if not models:
        return None
    print("opencode 가용 모델 ({{OPENCODE_PRO_MODEL}} 에 쓸 모델 선택):")
    for i, m in enumerate(models, start=1):
        print(f"  {i}) {m}")
    print(f"  번호 입력 (1-{len(models)}, 빈 입력 = 건너뜀): ", end="")
    line = stdin.readline()
    if not line:  # EOF
        return None
    choice = line.strip()
    if not choice:
        return None
    try:
        idx = int(choice)
    except ValueError:
        return None
    if 1 <= idx <= len(models):
        return models[idx - 1]
    return None


def _mark_model_todos(
    home: home.Home,
    dest_root: Path,
    copied_relpaths: set[Path],
    available: list[str],
    root_identity: tuple | None = None,
) -> list[str]:
    """비-tty/opencode 부재 폴백: `model:` 줄을 주석화하고 그 안의 모델 토큰을 중화한다.

    _mark_todos 폴백을 흡수— 모델 토큰만 대상. 조회 성공 시 가용 모델 목록을 마커에
    인라인해 사람이 바로 고를 수 있게 한다. 비파괴 규칙(이미 TODO/주석인 줄은 건너뜀·
    copied_relpaths 범위 한정). 마크한 토큰([OPENCODE_MODEL_TOKEN] 또는 [])을 반환.

    미해소 시 `model:` 값을 활성으로 남기면(`model: "…"  # TODO`) opencode 가
    "configured model … is not valid" 로 agent 자체를 거부한다(실 파일럿 블로커). → 줄 *전체*를
    주석화해 YAML frontmatter 에서 `model` 키를 *부재*시킨다 → opencode 가 기본 모델로 agent 를
    구동(graceful degradation).

    미해소 폴백이 주석 줄에 리터럴 `{{OPENCODE_PRO_MODEL}}` 을
    남기면 render `_assert_no_leak` 가 hard-fail 한다(@render path 산출물에 토큰 0 이어야 함). 그래서
    주석화하면서 토큰을 **형식 힌트 `<provider/model>` 로 중화**한다 → `# model: "<provider/model>"
    # TODO: …`. 채택자는 주석을 해제하고 provider/model 을 채우거나 `--opencode-model` 재import.

    실 줄-중화 로직은 `render_rules.neutralize_model_todo`
    로 옮겨 import(여기·render *이전* 폴백)와 self-update(pm_update 의 @render 재렌더·render_adapter
    가 같은 함수 호출) 둘 다 **같은 산출**을 내게 한다(byte-동일·drift 0). 옛 opencode `@source`
    비대칭(update가 미해소 토큰을 leak으로 rc-fail)을 방지한다. 실행 패키지를 직접 호출하므로
    중화 함수를 사용할 수 없으면 실행 실패로 전파한다. 미해소 model 줄을 활성 상태로 출하하지 않는다.
    """
    marked = False
    swapped: list[str] = []
    vanished: list[str] = []
    for _rel, _path in _iter_copied_files(
            dest_root, copied_relpaths, swapped=swapped, vanished=vanished):
        try:
            text = install_files.read_dest_text(dest_root, _rel, root_identity=root_identity)
        except install_files.UnsafeDestPathError:
            swapped.append(_rel.as_posix())
            continue
        except FileNotFoundError:
            vanished.append(_rel.as_posix())  # 읽기 전 삭제도 loud 제외(쓰기 쪽과 동형).
            continue
        except (UnicodeDecodeError, OSError):
            continue
        # 공유 중화(render_rules): agent frontmatter 의 `model:` 필드 줄만 주석화·토큰 중화한다 —
        # 산문/헤더(README 의 `{{OPENCODE_PRO_MODEL}}` 예시 등)는 건드리지 않는다(모듈 함수가
        # `model:` 시작 줄로 한정·YAML 주석 안전). 비파괴 멱등도 그쪽이 보장.
        new_text, changed = render_rules.neutralize_model_todo(text, available)
        if changed and new_text != text:
            try:
                install_files.write_dest_text(dest_root, _rel, new_text, root_identity=root_identity)
            except install_files.UnsafeDestPathError:
                swapped.append(_rel.as_posix())
                continue
            except FileNotFoundError:
                vanished.append(_rel.as_posix())
                continue
            marked = True
    install_files._report_copied_scope_anomalies("모델 TODO 표시", swapped, vanished)
    return [install_rules.OPENCODE_MODEL_TOKEN] if marked else []


def resolve_opencode_model(
    home: home.Home,
    dest_root: Path,
    copied_relpaths: set[Path],
    *,
    model_arg: str | None,
    models_runner: ModelsRunner | None = None,
    stdin=None,
    root_identity: tuple | None = None,
) -> ModelResolveResult:
    """{{OPENCODE_PRO_MODEL}} 을 결정적으로 해소. board init·conf sync 직후·fill 이전.

    opencode 어댑터 token 이 이번 복사본(copied_relpaths)에 잔존할 때만 동작 — 없으면 inactive.
    해소 순서:
      1) model_arg 명시 → 치환. (조회 가능하면 목록 대조해 *경고만*; 목록에 없어도 사용자 의도
         존중·치환 — 회사 사설 모델 등.)
      2) 없고 stdin tty → `opencode models` 번호목록 출력·선택 입력 → 치환. (선택 안 하면 TODO 폴백.)
      3) 없고 비-tty(CI·파이프) 또는 opencode 바이너리 부재 → 치환 안 함·TODO 마커(조회 성공 시
         가용목록 인라인)+stderr 경고.

    models_runner: `opencode models` 조회 seam — 테스트 stub 주입(라이브 CLI 미실행). None 이면
                   실 _real_models_runner. stdin: 대화형 선택 입력 seam — None 이면 sys.stdin.
    치환은 substitute_placeholders 와 동일한 copied_relpaths 비파괴 범위·_should_substitute 규칙.
    """
    # opencode 토큰이 이번 복사본에 없으면 단계 자체가 무의미(claude-only) — inactive.
    model_scan_swapped: list[str] = []
    model_scan_vanished: list[str] = []
    token_present = _token_present(
        dest_root, install_rules.OPENCODE_MODEL_TOKEN, copied_relpaths,
        root_identity=root_identity, swapped=model_scan_swapped,
        vanished=model_scan_vanished)
    # 교체됐거나 사라져 못 읽은 파일은 "토큰 없음"이 아니다 — 판정에서 빼되 조용히 넘기지 않는다.
    install_files._report_copied_scope_anomalies(
        "모델 토큰 판정", model_scan_swapped, model_scan_vanished)
    if not token_present:
        return ModelResolveResult(active=False, path="inactive",
                                  note="opencode 모델 토큰 미잔존 — 해소 단계 비활성(claude-only).")

    runner = models_runner if models_runner is not None else (
        lambda: _real_models_runner(contract.MODEL_SOURCE[install_rules.OPENCODE_MODEL_SOURCE_HARNESS]))
    stream = stdin if stdin is not None else sys.stdin
    is_tty = bool(getattr(stream, "isatty", lambda: False)())

    # 1) --opencode-model 명시 → 치환(사용자 의도 우선). 조회 가능하면 목록 대조 경고만.
    if model_arg:
        # 명시값을 **먼저 확정**(치환) — 외부 `opencode models` 조회가 명시-플래그 경로의 import
        # 를 막지 않게(codex suggestion). 목록 대조는 그 *뒤* best-effort 경고만(짧은 timeout·
        # fail-soft — 조회 실패/지연이 치환 결과를 바꾸지 않는다).
        changed = _substitute_model_token(dest_root, model_arg, copied_relpaths,
                                          root_identity=root_identity)
        ok, available = runner()
        if ok and available and model_arg not in available:
            print(
                f"경고: --opencode-model '{model_arg}' 가 `opencode models` 가용 목록에 없습니다 "
                f"(사용자 의도 존중·그대로 치환됨). 가용: {', '.join(available)}.",
                file=sys.stderr,
            )
        return ModelResolveResult(
            active=True, path="flag", model=model_arg, changed=changed,
            available=available if ok else [], tty=is_tty,
            note=f"--opencode-model 명시값으로 치환({changed} 파일).",
        )

    # 플래그 없음 → `opencode models` 조회(2·3 공통 전제).
    ok, available = runner()

    # 2) stdin tty + 조회 성공 → 번호목록·대화형 선택 → 치환.
    if is_tty and ok and available:
        choice = _prompt_model_choice(available, stream)
        if choice:
            changed = _substitute_model_token(dest_root, choice, copied_relpaths,
                                              root_identity=root_identity)
            return ModelResolveResult(
                active=True, path="interactive", model=choice, changed=changed,
                available=available, tty=True,
                note=f"대화형 선택 '{choice}' 로 치환({changed} 파일).",
            )
        # 선택 안 함(빈 입력·범위 밖) → TODO 폴백.
        todos = _mark_model_todos(home, dest_root, copied_relpaths, available,
                                  root_identity=root_identity)
        print("경고: opencode 모델 미선택 — {{OPENCODE_PRO_MODEL}} 을 TODO 로 표시(손으로 채우세요).",
              file=sys.stderr)
        return ModelResolveResult(
            active=True, path="todo", model=None, changed=0,
            available=available, tty=True, todos=todos,
            note="대화형 선택 건너뜀 — TODO 폴백.",
        )

    # 3) 비-tty / opencode 부재·조회 실패 → 치환 안 함·TODO 마커(가용목록 인라인 시도)+경고.
    todos = _mark_model_todos(home, dest_root, copied_relpaths, available if ok else [],
                              root_identity=root_identity)
    if not ok:
        reason = "opencode 바이너리 부재 또는 `opencode models` 조회 실패"
    elif not is_tty:
        reason = "비대화형(CI·파이프) — 블로킹 프롬프트 회피"
    else:
        reason = "가용 모델 없음"
    print(
        f"경고: {{{{OPENCODE_PRO_MODEL}}}} 미치환({reason}) — TODO 로 표시했습니다. "
        f"--opencode-model PROVIDER/MODEL 로 명시하거나 손으로 채우세요.",
        file=sys.stderr,
    )
    return ModelResolveResult(
        active=True, path="todo", model=None, changed=0,
        available=available if ok else [], tty=is_tty, todos=todos,
        note=f"치환 안 함({reason}) — TODO 폴백.",
    )


# ── fill 단계 (자유서술 placeholder · 하니스 구동 · opt-in) ──────────────────
# board init·local.conf 동기화 직후의 hook 지점. sed 로 못 채우는 자유서술 placeholder 를
# 대상 하니스를 헤드리스 구동해 *제안* 한다(auto) / TODO 로 표시한다(manual). 실구동은 토큰·
# 모델 비용이므로 opt-in 게이트(LIVE_HARNESS_ENV + --fill auto) 뒤로 격리한다.

# 하니스 호출 seam — (argv, prompt) → (성공 여부, stdout). 테스트가 stub 주입(토큰 0).
HarnessRunner = Callable[[list[str], str], "tuple[bool, str]"]


class FillResult:
    """fill 단계 산출 — 자유서술 placeholder 값 + 초안 제안 (확정 아님, 사람 리뷰 전제).

    필드:
      mode        : 'auto' (하니스 구동) | 'manual' (TODO 표시).
      harness     : 실제 구동 하니스 ('claude' | 'opencode' | None=manual).
      live        : 실 하니스를 호출했는가 (opt-in 게이트 통과 시만 True).
      values      : placeholder token → 채운 값 (auto·stub 모두 채움 — manual 은 빈 dict).
      drafts      : 라벨 → 초안 텍스트 제안 (CLAUDE.md·pm_role.local.md·harness-output 등).
      todos       : manual 에서 TODO 로 남긴 placeholder token 목록.
      runner_calls: 하니스에 보낸 argv 리스트 (명령 조립 검증·로깅용 — stub 도 기록).
      note        : 사람 대상 메모 (게이트 차단 이유 등).

    plain class (dataclass 아님): 테스트가 spec_from_file_location 로 동적 로드하는데,
    그 경로에선 모듈이 sys.modules 에 없어 dataclass 의 문자열 annotation 해석이 깨진다.
    """

    def __init__(
        self,
        mode: str,
        harness: str | None = None,
        live: bool = False,
        values: dict | None = None,
        drafts: dict | None = None,
        todos: list | None = None,
        runner_calls: list | None = None,
        note: str = "",
    ):
        self.mode = mode
        self.harness = harness
        self.live = live
        self.values = values if values is not None else {}
        self.drafts = drafts if drafts is not None else {}
        self.todos = todos if todos is not None else []
        self.runner_calls = runner_calls if runner_calls is not None else []
        self.note = note

    def __repr__(self) -> str:
        return (f"FillResult(mode={self.mode!r}, harness={self.harness!r}, live={self.live!r}, "
                f"values={list(self.values)!r}, todos={self.todos!r})")


def _fill_driver(argv: list[str]) -> tuple[str, bool, str | None]:
    """실제 fill argv를 (하네스·증분 신호·프롬프트 전달 방식) 선언으로 해소한다."""
    for command, driver in install_rules.FILL_DRIVER_BY_CMD.items():
        if tuple(argv[:len(command)]) != command:
            continue
        # 실행 중 이벤트를 내는 **실제 출력 형식**까지 argv에서 확인한다. 바이너리 접두사만
        # 맞고 JSON 플래그가 빠진 주입 호출은 평문/종료시 blob일 수 있으므로 증분 신호 축으로
        # 올리지 않는다(모르는 형식 = idle 판정 없음·가장 관대한 wall).
        if command == install_rules.OPENCODE_FILL_CMD:
            has_json_format = any(
                token == "--format=json"
                or (token == "--format" and index + 1 < len(argv)
                    and argv[index + 1] == "json")
                for index, token in enumerate(argv)
            )
            if not has_json_format:
                break
        elif command == install_rules.CODEX_FILL_CMD and "--json" not in argv[len(command):]:
            # 출력이 평문이면 증분 신호/프로필은 보수적으로 미지 축에 두되, codex의 프롬프트 전달
            # (표준 입력 후 EOF)은 출력 형식과 무관하다.
            return "", False, driver[2]
        return driver
    # 내부 빌더 밖의 주입 호출은 미지 프로필로 보수 처리한다: 신호·프롬프트 전달을 추정하지 않고
    # 가장 관대한 wall만 적용한다. basename이 알려진 이름이어도 argv 계약이 다르면 미지 축이다.
    return "", False, None


def _fill_local_config(cwd: Path | str | None) -> dict[str, str]:
    """fill 대상 repo의 배포별 하네스 timeout override를 읽는다(fail-soft)."""
    if cwd is None:
        return {}
    path = Path(cwd) / ".project_manager" / "local.conf"
    try:
        return conf_schema.parse(fs.read_text_shared(path, encoding="utf-8")) if path.is_file() else {}
    except (OSError, UnicodeError) as exc:
        print(f"경고: fill timeout 설정을 읽을 수 없음({path}): {exc} — 프로필 기본값 사용.",
              file=sys.stderr)
        return {}


def fill_harness_cap_advisory(harness: str, cwd: Path | str | None) -> str | None:
    """fill도 위임·리뷰와 같은 호출층 상한 판정을 소비한다(never-block advisory)."""
    profile = watchdog.resolve_harness_profile(harness, _fill_local_config(cwd))
    first_event_timeout = (
        watchdog.first_event_timeout_default() if profile.startup_watchdog else None
    )
    retries = watchdog.stall_retries_default() if profile.startup_watchdog else 0
    execution_budget = watchdog.watchdog_execution_budget(
        profile.wall_timeout,
        first_event_timeout=first_event_timeout,
        retries=retries,
    )
    return watchdog.harness_cap_advisory(
        os.environ, execution_budget=execution_budget,
        session_markers=watchdog.HARNESS_SESSION_MARKERS,
        cap_env=watchdog.HARNESS_CAP_ENV,
        render_missing=lambda _harness, cap_key, required: (
            f"[fill auto] 경고: 호출층 상한 {cap_key} 미해소 — "
            f"실행+정리+부분 산출물 보존 여유 {required}s 이상을 설정하세요."
        ),
        render_invalid=lambda _harness, cap_key, raw, required: (
            f"[fill auto] 경고: 호출층 상한 {cap_key}={raw!r} 해석 불가 — "
            f"실행+정리+부분 산출물 보존 여유 {required}s 이상을 설정하세요."
        ),
        render_low=lambda _harness, cap_key, cap_seconds, required: (
            f"[fill auto] 경고: 호출층 상한 {cap_key}={cap_seconds:g}s < "
            f"실행+정리+부분 산출물 보존 여유 {required}s — 엔진 진단/부분 산출물 보존 전에 "
            f"하네스가 kill할 수 있습니다. {cap_key}를 상향하세요."
        ),
    )


def _save_fill_failure_output(dest_root: Path, harness: str, output: str) -> Path:
    """fill 실패 원문을 repo 안 private raw 파일로 이식 가능하게 박제한다."""
    return _write_fill_private_file(
        dest_root, f"pm_import_fill_{harness}_{datetime.datetime.now():%Y%m%d_%H%M%S}_", ".txt", output)


def _write_fill_private_file(dest_root: Path, prefix: str, suffix: str, text: str) -> Path:
    """`<dest>/.project_manager/.local/fill/<prefix><무작위><suffix>` 에 0600·O_EXCL 로 쓴다(fill 실패 원문·
    opencode 프롬프트 파일). 경로 조상이 repo 밖을 가리키거나 부모가 symlink 면 `OSError` 다. 반환 경로는
    `dest_root` 기준 lexical 경로다(opencode `--dir <dest>` 와 같은 접두)."""
    dest_root = Path(dest_root)
    repo_root = dest_root.resolve(strict=True)
    if not repo_root.is_dir():
        raise NotADirectoryError(f"fill raw 대상 repo가 디렉터리가 아님: {dest_root}")
    raw_parts = (".project_manager", ".local", "fill")
    raw_dir = repo_root.joinpath(*raw_parts)

    def _assert_raw_dir_contained() -> None:
        try:
            raw_dir.resolve(strict=False).relative_to(repo_root)
        except ValueError as exc:
            raise OSError(
                f"fill raw 경로가 대상 repo 밖을 가리킴: {raw_dir}"
            ) from exc

    _assert_raw_dir_contained()

    os.makedirs(raw_dir, mode=0o700, exist_ok=True)
    _assert_raw_dir_contained()
    raw_dir_stat = os.lstat(raw_dir)
    if stat.S_ISLNK(raw_dir_stat.st_mode):
        raise OSError(f"fill raw 부모가 symlink라 거부: {raw_dir}")
    if not stat.S_ISDIR(raw_dir_stat.st_mode):
        raise NotADirectoryError(f"fill raw 부모가 디렉터리가 아님: {raw_dir}")
    os.chmod(raw_dir, 0o700)

    file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    file_flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = None
    raw_basename = ""
    for _attempt in range(100):
        raw_basename = f"{prefix}{secrets.token_hex(4)}{suffix}"
        try:
            fd = os.open(raw_dir / raw_basename, file_flags, 0o600)
            break
        except FileExistsError:
            continue
    if fd is None:
        raise FileExistsError("fill raw 고유 파일명 생성 100회 충돌")
    raw_path = raw_dir / raw_basename
    try:
        _assert_raw_dir_contained()
    except BaseException:
        os.close(fd)
        try:
            os.unlink(raw_path)
        except OSError:
            pass
        raise
    with os.fdopen(
            fd, "w", encoding="utf-8", errors="replace", newline="\n") as handle:
        handle.write(text)
    return dest_root.joinpath(*raw_parts, raw_basename)


def _real_harness_runner(
    argv: list[str], prompt: str, cwd: Path | str | None = None
) -> tuple[bool, str]:
    """실 하니스 바이너리를 subprocess 로 구동한다. 반환: (성공 여부, stdout).

    하니스 실행 실패(실행 파일 부재·정체·정리 실패·기동 OSError)는 (False, 에러텍스트)로 감싼다.
    프로필 준비·결과 정규화·부분 출력 서식 예외는 전파한다.
    프롬프트는 FILL_DRIVER_BY_CMD 의 전달 방식대로 넘긴다 — 표준 입력이면 여기서 전문을 쓰고 닫는다,
    `--file` 이면 argv 가 이미 run_fill 이 쓴 파일을 가리킨다. 실행 인자에는 싣지 않는다.

    SF: cwd 가 주어지면 *대상 repo* 에서 구동한다(run_fill 이 dest_root 를 바인딩). 호출자
    cwd 가 아니라 import 대상에서 돌아야 하니스의 작업 디렉토리·파일 접근이 분석 대상과 맞는다.

    세 하네스 모두 `watchdog` 공용 워치독을 경유한다. 하네스별 차이는 코드 분기가 아니라
    HARNESS_PROFILES 선언(startup 감시 여부·진행 신호·idle/wall 상한)이며 대상 repo local.conf
    override도 같은 해소기를 탄다. startup stall만 선언된 유한 재시도를 하고, idle/wall kill은
    중복 과금·중복 호출을 피하려 자동 재시도하지 않은 채 부분 산출물과 함께 fail-soft 한다.

    표준 입력 전달은 전문을 쓴 뒤 **stdin 을 닫아 EOF를 전달**한다 — codex 는 stdin 미닫힘 시
    "Reading additional input from stdin..." 로 무기한 대기. 전달 방식·진행 신호 정책은 실제 argv와
    함께 FILL_DRIVER_BY_CMD에 선언돼 하네스 판정 코드와 분리된다."""
    harness, emits_progress, prompt_transport = _fill_driver(argv)
    input_text = prompt if prompt_transport == install_rules.FILL_PROMPT_STDIN else None
    profile = watchdog.resolve_harness_profile(harness, _fill_local_config(cwd))
    progress_signal = (
        profile.progress_signal if emits_progress else conf_schema.PROGRESS_SIGNAL_NONE
    )
    idle_timeout = watchdog.idle_timeout_for_signal(
        progress_signal, profile.idle_timeout)
    first_event_timeout = (
        watchdog.first_event_timeout_default() if profile.startup_watchdog else None
    )
    retries = watchdog.stall_retries_default() if profile.startup_watchdog else 0
    idle_label = f"{idle_timeout:.0f}초" if idle_timeout is not None else "비활성(증분 신호 없음)"
    print(
        f"[fill auto] 실행 상한: idle={idle_label} · wall={profile.wall_timeout:.0f}초 "
        "(중단: Ctrl-C)",
        file=sys.stderr,
    )

    try:
        result = watchdog.run_with_first_event_watchdog(
            argv,
            first_event_timeout=first_event_timeout,
            overall_timeout=profile.wall_timeout,
            retries=retries,
            idle_timeout=idle_timeout,
            cwd=str(cwd) if cwd is not None else None,
            input_text=input_text,
        )
    except subprocess.TimeoutExpired as exc:
        wall_axis = watchdog.TIMEOUT_AXIS_WALL
        idle_axis = watchdog.TIMEOUT_AXIS_IDLE
        axis = getattr(exc, "timeout_axis", wall_axis)
        threshold = float(getattr(exc, "threshold_seconds", exc.timeout))
        silence = getattr(exc, "silence_seconds", None)
        silence_label = f" · 실측 침묵 {silence:.0f}초" if silence is not None else ""
        kind = "무진행 임계" if axis == idle_axis else "벽시계 백스톱"
        return False, watchdog.format_partial_output(
            f"[하니스 타임아웃 — {kind} {threshold:.0f}초 발화{silence_label}; "
            "자동 재시도 안 함 — 출력 확인 후 수동 재시도]",
            exc,
        )
    except FileNotFoundError:
        return False, f"[하니스 명령 '{argv[0] if argv else '?'}' 를 찾을 수 없음 — 설치/PATH 확인]"
    except (watchdog.StallWatchdogError, watchdog.ProcessCleanupError, OSError) as exc:  # 하니스 실행 실패만 fill 실패 결과.
        if isinstance(exc, watchdog.StallWatchdogError):
            axis = getattr(exc, "timeout_axis", None)
            threshold = getattr(exc, "threshold_seconds", None)
            threshold_label = f" {float(threshold):.0f}초" if threshold is not None else ""
            first_event_axis = watchdog.TIMEOUT_AXIS_FIRST_EVENT
            idle_axis = watchdog.TIMEOUT_AXIS_IDLE
            if axis == first_event_axis:
                kind = f"첫-이벤트 stall{threshold_label}"
            elif axis == idle_axis:
                kind = f"무진행 임계{threshold_label}"
            else:
                kind = f"벽시계 백스톱{threshold_label}"
            return False, watchdog.format_partial_output(
                f"[하니스 {kind} — 재시도 소진(fail-soft): {exc}]", exc)
        return False, watchdog.format_partial_output(f"[하니스 실행 오류: {exc}]", exc)

    output = result.stdout or ""
    if result.stderr:
        output += f"\n[stderr]\n{result.stderr}"
    return result.returncode == 0, output


def _build_fill_prompt(dest_root: Path, tokens: list[str]) -> str:
    """대상 repo 분석 → 자유서술 placeholder 추출 프롬프트(하니스 구동용).

    프롬프트 품질이 산출을 좌우하나 초안일 뿐(확정 아님). 토큰별로 무엇을 채울지 지시한다.
    """
    token_lines = "\n".join(f"  - {t}" for t in tokens)
    return (
        f"이 저장소({dest_root})를 분석해 PM 프레임워크 자유서술 placeholder 를 채울 초안을 제안하라.\n"
        f"확정이 아니라 사람이 검토할 *초안*이다. 다음 placeholder 각각에 대해 한국어로 제안하라:\n"
        f"{token_lines}\n\n"
        f"  - {{{{PROJECT_CONSTRAINTS}}}}: 이 프로젝트의 아키텍처 불변식·금지(핵심 결정 경계 등).\n"
        f"  - {{{{PROTECTED_PATHS}}}}: code author + ADR 없이 건드리면 안 되는 파일/디렉토리.\n"
        f"  - {{{{USER_GATE_ITEMS}}}}: PM 자율 결정 밖 — 사용자 사전 동의가 필요한 행위.\n"
        f"불확실하면 빈 항목으로 두고 사람이 채우도록 TODO 를 남겨라."
    )


def _build_runner_argv(harness: str, dest_root: Path, prompt_file: Path | None) -> list[str]:
    """하니스별 헤드리스 구동 명령 조립(runner 매핑·명시 등록). 프롬프트는 실행 인자에 없다.

      claude   → `claude -p` (프롬프트 = 표준 입력)
      opencode → `opencode run "<첨부 안내>" --file <prompt_file> --format json --dir <dest>`
                 (cross 위임과 같은 `--file` 전달·안내 문구는 `contract` 의 한 줄 상수)
      codex    → `codex exec --json -s workspace-write --skip-git-repo-check -C <dest>` (표준 입력)

    `prompt_file` 은 `--file` 전달 하네스에 run_fill 이 `dest_root` 안에 쓴 프롬프트 파일이다(opencode
    `--dir` 하위). 표준 입력 전달 하네스는 None 이다.

    미지원 harness 는 **fail-loud**(ValueError). 과거 codex 가 이 매핑에 없어 조용히 `claude -p`
    로 폴백해 *잘못된 바이너리*를 호출하고 출력만 harness=codex 로 오표기하던 클래스를 닫는다 —
    모르는 harness 는 명시 등록을 강제(silent 폴백 금지)."""
    if harness == "claude":
        return [*install_rules.CLAUDE_FILL_CMD]
    if harness == "opencode":
        return [*install_rules.OPENCODE_FILL_CMD, contract.OPENCODE_ATTACHED_MSG, "--file", str(prompt_file),
                "--format", "json", "--dir", str(dest_root)]
    if harness == "codex":
        # workdir 핀(codex 는 -C 로 작업 디렉토리 고정).
        return [*install_rules.CODEX_FILL_CMD, "--json", "-s", "workspace-write", "--skip-git-repo-check",
                "-C", str(dest_root)]
    supported = "·".join(install_rules.FILL_CAPABLE_HARNESSES)
    raise ValueError(
        f"_build_runner_argv: 미지원 fill harness {harness!r} — 지원: {supported}. "
        f"silent 폴백 금지(잘못된 바이너리 호출·오표기 방지·runner 매핑에 명시 등록 필요)."
    )


def _parse_opencode_json(output: str) -> str:
    """opencode `--format json` 출력에서 결과 텍스트를 추출(token/cost 파싱은 부수적).

    opencode 출력 형태는 버전에 따라 다를 수 있어 보수적으로 흔한 모양을 훑는다:
      - 최상위 'result'/'text'/'output' 문자열
      - 메시지 parts 배열의 text part 들 (parts[].text)
    파싱 실패하면 원문을 그대로 반환(fail-soft — 사람이 읽을 수 있게)."""
    try:
        data = json.loads(output)
    except (json.JSONDecodeError, ValueError):
        return output
    if isinstance(data, dict):
        for key in ("result", "text", "output", "content"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value
        parts = data.get("parts")
        if isinstance(parts, list):
            texts = [p["text"] for p in parts
                     if isinstance(p, dict) and isinstance(p.get("text"), str)]
            if texts:
                return "\n".join(texts)
    return output


def _parse_codex_json(output: str) -> str:
    """codex `exec --json` JSONL 출력에서 최종 응답 텍스트 추출(fail-soft·_parse_opencode_json 동형).

    codex 는 줄당 JSON 이벤트(thread.started·turn.started·item.*·turn.completed·error)를 낸다.
    응답 = `item.type == "agent_message"` 인 item 의 `.text`(마지막 것 = 최종 응답·엔진
    contract.parse_codex_json 규칙과 정합). JSON 파싱 불가·구조 상이·미발견이면 원문 그대로
    반환(사람이 읽게 — _parse_opencode_json 과 같은 fail-soft). pm_import 자족(형제 파서 미러·
    _parse_opencode_json 선례 + 시그니처/목적 상이 — `contract` 쪽은 orchestration 용 3-튜플
    (thread_id·reply·usage)·여긴 fill 초안용 str·fail-soft. 순환 import 는 어느 쪽이든 없음)."""
    reply = ""
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            evt = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(evt, dict):
            continue
        item = evt.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                reply = text  # 최종(마지막) agent_message 로 갱신.
    return reply if reply else output


def _live_harness_allowed(mode: str) -> bool:
    """opt-in 게이트: 실 하니스 호출은 PM_IMPORT_LIVE_HARNESS=1 AND --fill auto 동시 충족 시만.

    둘 중 하나라도 없으면 False → run_fill 이 실 runner 를 호출하지 않고 stub/manual 로 폴백.
    """
    return mode == "auto" and os.environ.get(install_rules.LIVE_HARNESS_ENV, "").strip() in ("1", "true", "yes", "on")


def _resolve_fill_scope(dest_root: Path, copied_relpaths: set[Path] | None) -> set[Path]:
    """fill 스캔 대상 relpath set 을 결정한다.

    copied_relpaths 가 주어지면(main 경로) 그걸 그대로 쓴다 — 이번 import 가 복사한 파일만
    스캔(비파괴). None 이면(run_fill/_run_manual_fill 직접 호출 — 테스트·디버그) dest 트리
    전체를 폴백 스캔한다. 단, --into main 경로는 *항상* copied_relpaths 를 넘기므로 사용자
    파일 오염은 발생하지 않는다(이 폴백은 직접 호출자 편의용). 폴백도 COPY_EXCLUDE_DIR_NAMES
    (node_modules·__pycache__·.git)는 제외한다.
    """
    if copied_relpaths is not None:
        return copied_relpaths
    # 실 import 경로는 항상 copied_relpaths를 넘기므로 여기의 dest 전수 열거는 직접 호출용 폴백에
    # 한정된다. adopter가 아직 git repo가 아니면 seam이 filesystem 강등 경고를 1회 표면화한다.
    # 정상 import에는 노이즈가 없고, 직접 호출자는 추적/ignore 보장 소실을 놓치지 않는다.
    # 이 소비점의 0건은 "채울 파일 없음"이라는 정당 결과이므로 출하용 빈 인벤토리 예외에서 제외한다.
    scope: set[Path] = set()
    for rel in repo_files.list_repo_owned_files(
            dest_root, ".", mode=repo_files.OWNED):
        if any(part in install_source.COPY_EXCLUDE_DIR_NAMES for part in rel.parts):
            continue
        # dest fallback은 텍스트 읽기/수정 소비처다. seam의 gap domain은 symlink/gitlink를
        # 보존하지만 여기서는 링크 추종(특히 repo 밖)과 디렉토리/gitlink·삭제 엔트리를 제외한다.
        path = dest_root / rel
        if path.is_symlink() or not path.is_file():
            continue
        scope.add(rel)
    return scope


def _iter_copied_files(dest_root: Path, copied_relpaths: set[Path],
                       swapped: list[str] | None = None,
                       vanished: list[str] | None = None):
    """이번 import 가 복사한 파일들만 (relpath, 절대경로)로 순회한다.

    MF(비파괴): fill 단계가 dest_root.rglob 로 *대상 프로젝트 전체* 를 훑으면, --into 에서
    이번 import 가 복사하지 *않은* 기존 사용자 파일(우연히 sentinel 포함)에도 TODO 마커가
    주입되어 비파괴 보장(substitute_placeholders 가 copied_relpaths 로 한정)과
    충돌한다. 따라서 fill 도 substitute_placeholders 와 *동일한* copied_relpaths set 만
    대상으로 한다 — 복사 안 한 사용자 파일은 절대 스캔/수정하지 않는다. node_modules·
    __pycache__·.git 등은 애초에 복사 목록에 없어 자연히 제외된다.
    """
    for rel in sorted(copied_relpaths):
        if any(part in install_source.COPY_EXCLUDE_DIR_NAMES for part in rel.parts):
            continue
        # 선검사는 `lstat` — symlink 를 후보로 통과시켜 소비처의 nofollow fd 가드가 판정하게 한다
        #   (`is_file()` 은 깨진 링크를 조용히 제외하고 정상 링크는 따라간다). 탈락 사유(복사 뒤
        #   삭제·형상 변화)는 호출부 목록에 실어 요약으로 나가게 한다(조용한 skip 0).
        anomaly = install_files._copied_scope_anomaly(dest_root, rel)
        if anomaly == "vanished":
            if vanished is not None and rel.as_posix() not in vanished:
                vanished.append(rel.as_posix())
            continue
        if anomaly == "swapped":
            if swapped is not None and rel.as_posix() not in swapped:
                swapped.append(rel.as_posix())
            continue
        yield rel, dest_root / rel


def _fill_targets(dest_root: Path, copied_relpaths: set[Path] | None = None,
                  root_identity: tuple | None = None) -> list[str]:
    """이번 import 가 복사한 파일에 실제로 남아있는 자유서술 placeholder 토큰 목록.

    잔존 grep 으로 판정 — 트리에 없는 토큰은 채울 필요 없음. 스캔 범위는 copied_relpaths
    (이번 run 복사 파일)로 한정 — 사용자 파일 불가침(비파괴). None 이면 dest 트리 전체 폴백
    (직접 호출용 — COPY_EXCLUDE_DIR_NAMES 제외).

    {{OPENCODE_PRO_MODEL}} 는 LLM fill 후보가 *아니다* — resolve_opencode_model 의
    결정적 `opencode models` 조회가 전담한다(환각·미가용 모델 추측 제거). 여기서는 자유서술
    3종(FREE_FORM_TOKENS)만 본다.
    """
    scan = _resolve_fill_scope(dest_root, copied_relpaths)
    present: list[str] = []
    swapped: list[str] = []
    vanished: list[str] = []
    for token in install_rules.FREE_FORM_TOKENS:
        if _token_present(dest_root, token, scan, root_identity=root_identity,
                          swapped=swapped, vanished=vanished):
            present.append(token)
    install_files._report_copied_scope_anomalies("자유서술 토큰 판정", swapped, vanished)
    return present


def _plan_fill_targets(actions: list[install_files.CopyAction]) -> list[str]:
    """dry-run 계획용: 복사될 *소스* 파일들에 남아있는 자유서술 토큰 목록.

    MF2: --dry-run 은 파일을 복사하지 않으므로 dest 트리에 토큰이 없다. 그래서 복사 *예정*인
    src 파일(actions[].src)을 직접 읽어 무엇을 채우게 될지 계획을 만든다. 실 fill(_fill_targets)
    이 copied dest 파일에서 보는 것과 동일한 후보 토큰 집합을 source 측에서 미리보기한다.

    {{OPENCODE_PRO_MODEL}} 는 fill 후보에서 분리(결정적 resolve_opencode_model 전담)
    되므로 여기서도 자유서술 3종만 본다. 모델 토큰 계획은 _plan_opencode_model_targets 가 별도.
    """
    present: list[str] = []
    for token in install_rules.FREE_FORM_TOKENS:
        for action in actions:
            if install_source._is_engine_source(action.dst):  # 엔진 소스 주석의 토큰-문서는 placeholder 아님
                continue
            try:
                if token in fs.read_text_shared(action.src, encoding="utf-8"):
                    present.append(token)
                    break
            except (UnicodeDecodeError, OSError):
                continue
    return present


def _plan_opencode_model_targets(actions: list[install_files.CopyAction]) -> bool:
    """dry-run 계획용: 복사 *예정* src 에 {{OPENCODE_PRO_MODEL}} 토큰이 잔존하는가(opencode 트리).

    실 단계(resolve_opencode_model)가 dest 복사본에서 토큰 잔존을 보고 동작 여부를 정하는데,
    dry-run 은 복사를 안 하므로 src 측에서 미리 본다(_plan_fill_targets 와 같은 결).
    """
    for action in actions:
        if install_source._is_engine_source(action.dst):  # 엔진 소스(.py) 주석의 모델-토큰 문서는 placeholder 아님
            continue
        try:
            if install_rules.OPENCODE_MODEL_TOKEN in fs.read_text_shared(action.src, encoding="utf-8"):
                return True
        except (UnicodeDecodeError, OSError):
            continue
    return False


def _token_present(
    dest_root: Path,
    token: str,
    copied_relpaths: set[Path] | None = None,
    root_identity: tuple | None = None,
    swapped: list[str] | None = None,
    vanished: list[str] | None = None,
) -> bool:
    """이번 import 가 복사한 파일에 token 이 한 파일이라도 남아있는가(비파괴 범위 한정).

    copied_relpaths=None 이면 dest 트리 전체 폴백(COPY_EXCLUDE_DIR_NAMES 제외) — 직접 호출용.

    swapped/vanished: 계획 뒤 교체됐거나 사라져 **읽지 못한** relpath 를 담을 리스트(호출부가
    loud 로 보고). 삼키면 "토큰 없음"으로 오판정해 모델 해소가 inactive 로 빠지거나 fill 대상이
    조용히 준다 — 판정 채널이라고 침묵하면 안 되는 이유다(제외는 하되 반드시 알린다). 삭제 경쟁을
    일반 OSError 로 묶으면 그 축만 무요약이 되므로 교체와 같은 격으로 가른다."""
    scan = _resolve_fill_scope(dest_root, copied_relpaths)
    for _rel, _path in _iter_copied_files(
            dest_root, scan, swapped=swapped, vanished=vanished):
        if install_source._is_engine_source(_rel):  # 엔진 소스 주석의 토큰-문서는 placeholder 아님
            continue
        try:
            if token in install_files.read_dest_text(dest_root, _rel, root_identity=root_identity):
                return True
        except install_files.UnsafeDestPathError:
            if swapped is not None and _rel.as_posix() not in swapped:
                swapped.append(_rel.as_posix())
            continue
        except FileNotFoundError:
            if vanished is not None and _rel.as_posix() not in vanished:
                vanished.append(_rel.as_posix())
            continue
        except (UnicodeDecodeError, OSError):
            continue
    return False


def _mark_todos(
    dest_root: Path,
    tokens: list[str],
    copied_relpaths: set[Path] | None = None,
    root_identity: tuple | None = None,
) -> list[str]:
    """manual 모드: 자유서술 placeholder 옆에 `<!-- TODO -->` 가 없으면 표시한다.

    템플릿은 대개 이미 placeholder 아래에 TODO 주석을 둔다. 여기서는 토큰을
    `<!-- TODO: 손으로 채우세요 -->` 인라인으로 *치환*하지 않고, 토큰 줄에 TODO 마커가 없을
    때만 토큰 뒤에 인라인 마커를 덧붙여(비파괴) 채택자에게 손작업 지점을 명시한다.
    실제로 마커를 추가한 토큰 목록을 반환한다.

    스캔 범위는 copied_relpaths(이번 run 복사 파일)로 한정 — 복사하지 않은 사용자 파일에는
    절대 마커를 주입하지 않는다(비파괴 보장). None 이면 dest 트리 전체 폴백(직접 호출용).
    """
    scan = _resolve_fill_scope(dest_root, copied_relpaths)
    marked: set[str] = set()
    swapped: list[str] = []
    vanished: list[str] = []
    marker = " <!-- TODO: 손으로 채우세요 -->"
    for _rel, _path in _iter_copied_files(
            dest_root, scan, swapped=swapped, vanished=vanished):
        if install_source._is_engine_source(_rel):  # 엔진 소스(.py)에 TODO 마커 주입 금지 — verbatim
            continue
        try:
            text = install_files.read_dest_text(dest_root, _rel, root_identity=root_identity)
        except install_files.UnsafeDestPathError:
            swapped.append(_rel.as_posix())
            continue
        except FileNotFoundError:
            vanished.append(_rel.as_posix())  # 읽기 전 삭제도 loud 제외(쓰기 쪽과 동형).
            continue
        except (UnicodeDecodeError, OSError):
            continue
        new_text = text
        changed = False
        # 이 파일에서 마킹한 토큰은 **쓰기 성공 뒤에** 반영한다 — 계획 시점에 더하면 교체·삭제로
        #   제외된 파일의 토큰이 "표시됨"으로 보고돼 사람이 없는 처리 결과를 믿는다.
        pending: set[str] = set()
        for line in text.splitlines(keepends=True):
            for token in tokens:
                if token in line and "TODO" not in line:
                    replacement = line.replace("\n", "") + marker + ("\n" if line.endswith("\n") else "")
                    new_text = new_text.replace(line, replacement, 1)
                    pending.add(token)
                    changed = True
        if changed and new_text != text:
            try:
                install_files.write_dest_text(dest_root, _rel, new_text, root_identity=root_identity)
            except install_files.UnsafeDestPathError:
                swapped.append(_rel.as_posix())
                continue
            except FileNotFoundError:
                vanished.append(_rel.as_posix())
                continue
            marked.update(pending)
    install_files._report_copied_scope_anomalies("자유서술 TODO 표시", swapped, vanished)
    return sorted(marked)


def run_fill(
    dest_root: Path,
    harness: str,
    *,
    live: bool,
    runner: HarnessRunner | None = None,
    copied_relpaths: set[Path] | None = None,
    root_identity: tuple | None = None,
) -> FillResult:
    """자유서술 placeholder 채움 단계. board init 직후 hook.

    dest_root: import 된 대상 트리. harness: 구동 하니스('claude'|'opencode').
    live=True  → 실 하니스 호출 시도(opt-in 게이트 통과 — main 이 _live_harness_allowed 로 판정).
    live=False → runner 미호출(stub 경로). runner 가 주입되면(테스트 stub) live=True 와 무관하게
                 그 runner 로 명령을 조립·호출해 *명령 조립* 만 검증한다(토큰 0).
    copied_relpaths: 이번 import 가 복사한 파일 relpath set — fill 스캔 범위를 이 파일들로
                 한정한다(비파괴 보장). None 이면 dest 트리 전체를 스캔(직접 호출용
                 폴백) — main 은 항상 substitute_placeholders 와 동일 set 을 전달한다.

    규격(ticket §인터페이스): live=False 면 runner 를 호출하지 않고 stub/manual 경로로 간다.
    여기서 '하니스 미구동'은 *실 바이너리* 미구동을 뜻한다 — 주입 runner(stub)는 항상 안전.
    """
    scan = _resolve_fill_scope(dest_root, copied_relpaths)
    tokens = _fill_targets(dest_root, scan, root_identity=root_identity)

    # manual(또는 채울 토큰 없음): 하니스 미구동 — TODO 표시만.
    if not tokens:
        return FillResult(mode="manual", note="자유서술 placeholder 가 트리에 없음 — 처리 불필요.")

    prompt = _build_fill_prompt(dest_root, tokens)

    # 실 runner 결정: 주입 stub 이 있으면 그걸(테스트). 없고 live 면 실 바이너리. 아니면 미구동.
    # SF: 실 바이너리는 대상 repo(dest_root)에서 구동되도록 cwd 를 바인딩한다(호출자 cwd 아님).
    effective_runner: HarnessRunner | None
    if runner is not None:
        effective_runner = runner
    elif live:
        effective_runner = functools.partial(_real_harness_runner, cwd=dest_root)
    else:
        effective_runner = None

    if effective_runner is None:
        # stub/실호출 모두 없음 → 자유서술 placeholder 값을 채우지 않고 제안만 비움.
        # (main 은 manual 또는 게이트 미통과 시 이 경로 대신 _run_manual_fill 을 부른다.)
        return FillResult(
            mode="auto",
            harness=harness,
            live=False,
            note="하니스 미구동(게이트 미통과·stub 없음) — 제안 없음. manual 폴백 권장.",
        )

    # 프롬프트 전달 방식은 FILL_DRIVER_BY_CMD 선언이 정한다. `--file` 전달이면 대상 폴더 안
    # (opencode `--dir` 하위·gitignore 된 `.local/fill`)에 프롬프트 파일을 쓰고 실행 뒤 지운다.
    prompt_transport = next(
        driver[2] for driver in install_rules.FILL_DRIVER_BY_CMD.values() if driver[0] == harness)
    prompt_file = (
        _write_fill_private_file(
            dest_root, f"pm_import_fill_prompt_{datetime.datetime.now():%Y%m%d_%H%M%S}_", ".md", prompt)
        if prompt_transport == install_rules.FILL_PROMPT_FILE else None)
    try:
        argv = _build_runner_argv(harness, dest_root, prompt_file)
        ok, output = effective_runner(argv, prompt)
    finally:
        if prompt_file is not None:
            prompt_file.unlink()
    result = FillResult(mode="auto", harness=harness, live=live)
    result.runner_calls.append(list(argv))
    if not ok:
        raw_path = _save_fill_failure_output(dest_root, harness, output)
        shown_path = raw_path.relative_to(dest_root)
        result.note = (
            "하니스 구동 실패(fail-soft) — 제안 없음. "
            f"부분/오류 출력 원문 보존: {shown_path}"
        )
        return result

    # 하니스별 응답 파싱: opencode=--format json·codex=exec --json JSONL(agent_message)·claude=평문.
    if harness == "opencode":
        text = _parse_opencode_json(output)
    elif harness == "codex":
        text = _parse_codex_json(output)
    else:
        text = output

    # 산출 텍스트 = 사람이 검토할 placeholder 값 제안. 각 토큰에 동일 출력을 후보로 매핑한다
    # (정밀 파싱은 모델 출력 형식에 의존 — 초안 전제라 통째로 제안하고 사람이 분배·편집).
    for token in tokens:
        result.values[token] = text
    result.drafts["(harness-output)"] = text
    result.note = "하니스 구동 제안 — 사람 검토 후 손으로 반영(자동 확정 아님)."
    return result


def _run_manual_fill(
    dest_root: Path,
    copied_relpaths: set[Path] | None = None,
    root_identity: tuple | None = None,
) -> FillResult:
    """manual 모드(기본): 하니스 미구동. 자유서술 placeholder 에 TODO 마커 표시만.

    copied_relpaths: 이번 import 가 복사한 파일 relpath set — TODO 마킹 범위를 이 파일들로
    한정한다(비파괴). None 이면 dest 트리 전체 폴백(직접 호출용). main 은 항상 전달한다.
    """
    scan = _resolve_fill_scope(dest_root, copied_relpaths)
    tokens = _fill_targets(dest_root, scan, root_identity=root_identity)
    if not tokens:
        return FillResult(mode="manual", note="자유서술 placeholder 가 트리에 없음 — 처리 불필요.")
    marked = _mark_todos(dest_root, tokens, scan, root_identity=root_identity)
    return FillResult(
        mode="manual",
        todos=marked,
        note="자유서술 placeholder 를 TODO 로 표시 — 채택자가 손으로 채운다(하니스 미구동).",
    )


def ensure_pm_playbook_local_stub(dest_root: Path, backup_root: Path | None,
                                  root_identity: tuple | None = None) -> str:
    """pm_playbook.local.md 스텁을 dest 에 생성한다.

    backup_root: 중앙 백업 디렉토리(또는 None=--new). 이 함수는 기존 .local 을
    *덮지 않고 보존(skip)* 하므로 백업할 원본 변경이 없다 — backup_root 는 시그니처 일관성을
    위해 받지만 실제로 사용하지 않는다(미생성 = 백업 불요).

    fill 단계와 같은 자리(board init·conf sync 직후)에서 호출 — pm_role.local 초안 처리와
    같은 결의 인스턴스-소유 문서다. 루트 .local은 manifest 밖이라 템플릿 복사로 안 오니
    여기서 PM_PLAYBOOK_LOCAL_STUB(단일 소스 인라인 상수)로 *생성*한다.

    비파괴(재-import): 기존 pm_playbook.local.md 가 있으면 덮지 않는다 — 인스턴스가 누적한
    wave 학습이 손실되면 안 된다(local.conf 백업 철학 MF1·_backup_existing_local_conf 와 동일).
    구현은 *skip* — 기존 .local 은 manifest 밖·인스턴스 소유라 import 산출 백업과 별개로
    그대로 보존한다(누적 학습 trail 은 사용자 VCS 가 이력 관리). 백업하지 않는 이유: 백업은
    "import 가 덮는 파일"을 위한 것인데 여기선 애초에 덮지 않으므로 백업할 원본 변경이 없다.

    쓰기는 **dest 상대 fd 순회**로 한다 — 이 스텁 자리의 조상(`.project_manager/wiki`)이 저장소 밖
    지향 링크로 바뀌어 있으면 경로 `mkdir`+`write_text` 가 링크를 따라 밖에 파일을 만든다. 복사
    단계가 파일 단위 제외로 **계속 진행**하게 된 뒤로는 그 형상에서도 이 지점에 도달하므로, 여기도
    같은 fd 규율을 탄다(생성 실패는 loud skip — 스텁은 설치 성공 조건이 아니다).

    반환값(사람 대상 상태):
      "created" — 새 스텁 생성.
      "preserved" — 기존 .local 발견·비파괴 보존(미생성).
      "unsafe-skip" — 경로가 안전하지 않아 생성하지 않음(저장소 밖 쓰기 차단).
    """
    rel = Path(PM_PLAYBOOK_LOCAL_RELPATH)
    if not install_files._is_safe_dest_path(dest_root, rel.parent):
        # 조상이 저장소 밖을 향하면 그 아래를 **보지도 만들지도 않는다** — 밖의 동명 파일을
        #   "기존 보존" 으로 오보고하지도, 밖에 스텁을 만들지도 않는다.
        print(f"  ⚠️ pm_playbook.local.md 스텁 자리의 조상 경로가 안전하지 않아 생략합니다 "
              f"({rel.as_posix()}) — 저장소 밖 쓰기를 피합니다.", file=sys.stderr)
        return "unsafe-skip"
    if os.path.lexists(dest_root / rel):
        # 비파괴: 인스턴스 소유 누적 학습 보존(덮지 않음·skip). 링크로 바뀌어 있어도 그 자리를
        #   새로 만들지 않는다(`lexists` — 깨진 링크도 점유로 본다).
        return "preserved"
    try:
        install_files._ensure_dest_dir_nofollow(dest_root, rel.parent, root_identity=root_identity)
        with install_files._fdopen_text(
                install_files._open_dest_relative_nofollow(
                    dest_root, rel, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    root_identity=root_identity, regular_only=True),
                "w", newline="\n") as handle:
            handle.write(PM_PLAYBOOK_LOCAL_STUB)
    except (OSError, install_files.UnsafeDestPathError) as exc:
        print(f"  ⚠️ pm_playbook.local.md 스텁을 만들지 못했습니다 "
              f"({rel.as_posix()}: {exc}) — 저장소 밖 쓰기를 피해 생략합니다.", file=sys.stderr)
        return "unsafe-skip"
    return "created"


def _harness_binary_available(harness: str) -> bool:
    """등록 fill 하니스와 동명의 실 바이너리가 PATH 에 있는가.

    shutil.which 로 탐지한다. 테스트는 monkeypatch(import_fill.shutil.which) 또는 PATH 조작으로
    부재를 stub 한다. registry에 없는 이름은 새 하네스의 등록 누락/호출부 오류이므로 조용히
    첫 하네스로 폴백하지 않고 fail-loud 한다. 등록됐지만 바이너리가 없는 것은 정상적인
    환경 가용성 판정(False)이며 ``_resolve_fill_harness``가 다음 등록 후보를 탐색한다.
    """
    if harness not in install_rules.HARNESS_TEMPLATE_DIRS:
        raise ValueError(
            f"미등록 fill harness의 바이너리를 판정할 수 없습니다: {harness!r} — "
            f"지원: {', '.join(install_rules.HARNESS_TEMPLATE_DIRS)}"
        )
    return shutil.which(harness) is not None


def _resolve_fill_harness(
    fill_harness_arg: str | None,
    harness: str | tuple[str, ...] | list[str],
) -> str:
    """fill 구동 하네스 결정. 명시값 우선, 없으면 선택 집합의 첫 가용 하네스를 쓴다.

    registry 정규 순서가 결정적 우선순위다(현재 claude→opencode→codex). 선택된 바이너리가 모두
    없으면 첫 하네스를 그대로 반환해 상위 opt-in 게이트/manual 폴백이 명확히 진단하게 한다.
    """
    if fill_harness_arg:
        return fill_harness_arg
    selected = (
        install_rules.parse_harness_selection(harness)
        if isinstance(harness, str)
        else tuple(harness)
    )
    if len(selected) == 1:
        return selected[0]
    for candidate in selected:
        if _harness_binary_available(candidate):
            return candidate
    return selected[0]


def _print_fill_result(result: FillResult, dry_run: bool) -> None:
    """fill 결과를 사람 대상으로 출력. auto 제안은 *적용 안 함* — 사람 검토 전제."""
    if result.mode == "manual":
        if result.todos:
            print(f"✓ 자유서술 placeholder TODO 표시: {'·'.join(result.todos)} "
                  f"(채택자가 손으로 채웁니다).")
        else:
            print(f"  fill(manual): {result.note}")
        return
    # auto
    print(f"[fill auto] harness={result.harness}  live={result.live}")
    if result.runner_calls:
        for call in result.runner_calls:
            print(f"  구동 명령: {shlex.join(call) if hasattr(shlex, 'join') else ' '.join(call)}")
    if result.values:
        print("  제안된 자유서술 placeholder 값 (검토 후 손으로 반영 — 자동 확정 아님):")
        for token, value in result.values.items():
            preview = value.strip().splitlines()[0][:80] if value.strip() else "(빈 제안)"
            print(f"    {token} → {preview}")
        if dry_run:
            print("  [dry-run] 제안만 출력 — 파일 미변경.")
    if result.note:
        print(f"  메모: {result.note}")
