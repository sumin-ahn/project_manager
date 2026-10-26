"""설정 값 기반 렌더·비교와 계획 산출 적용 (층: app)."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple
import contextlib
import shutil
from pm_engine.app import adapter_render
from pm_engine.app import hook_sets
from pm_engine.domain import manifest_rules
from pm_engine.domain import render_rules
from pm_engine.infra import engine_transfer
from pm_engine.infra import fs
from pm_engine.infra import install_conf
from pm_engine.infra import manifest_store
from pm_engine.infra import render_io
from pm_engine.infra import skill_inventory


# local.conf key → operational token key(uppercase·render_rules). board.py init 은
# runtime.py·test.cmd·project.name 만 기록 — 나머지(project.root·project.tagline·project.date)는
# local.conf 에 없으므로 매핑 부재 시 빈값(render 시 그 토큰이 남아있으면 leak assertion 이 잡는다·
# 그러나 출하 어댑터의 operational 토큰은 import sed 로 이미 리터럴이라 render 시점엔 보통 부재 → no-op).
_LOCAL_CONF_TO_OPERATIONAL = {
    "project.name": "PROJECT_NAME",
    "project.tagline": "PROJECT_TAGLINE",
    "project.root": "PROJECT_ROOT",
    "runtime.py": "PY",
    "test.cmd": "TEST_CMD",
    "project.date": "DATE",
    # opencode 어댑터 전용 — pm_import 가 import 시 local.conf 에 기록(모델 해소 시만).
    # self-update 의 @source 재렌더가 `.opencode/agents` 를 렌더할 때 이 매핑으로
    # local.conf 재유도. **미해소**(opencode 없이 import 한 채택자·local.conf 에 harness.opencode.pro_model
    # 부재)면 render_adapter 가 leak 으로 rc-fail 하지 않고 intentional-TODO 로 graceful 중화한다
    # (render_rules.neutralize_model_todo·import 대칭) — 한 토큰 미해소가 엔진/타 어댑터 update
    # 전체를 막지 않는다(부분-graceful). claude tree 엔 토큰 부재 → no-op.
    "harness.opencode.pro_model": "OPENCODE_PRO_MODEL",
    # 위임 모델/추론 토큰(`delegate.<role>[.<tier>].{model,reasoning}`)은 여기 손으로 적지 않는다 —
    # render_rules.DELEGATE_MODEL_CONF_KEYS 한 표를 역전해 얹는다(`_local_conf_operational_map`).
}


def _local_conf_operational_map() -> dict[str, str]:
    """local.conf key → operational token key — 고정 배선 + **위임 토큰(파생)**.

    위임 토큰의 단일 표는 `render_rules.DELEGATE_MODEL_CONF_KEYS`(token→conf key)다. 여기에 사본을
    두면 새 역할/티어가 한쪽에만 늘어 그 토큰이 조용히 미배선된다(카드가 영영 TODO 로 중화). 표를
    역전해 쓰므로 표가 자라면 배선도 같이 자란다.

    실행 패키지의 선언을 직접 소비하며, 적재 오류는 실행 실패로 전파한다.
    """
    delegate = render_rules.DELEGATE_MODEL_CONF_KEYS
    return {
        **_LOCAL_CONF_TO_OPERATIONAL,
        **{conf_key: token_key for token_key, conf_key in delegate.items()},
    }


def _delegate_harness_from_local_conf(conf: Mapping[str, str]) -> dict[str, str]:
    """위임 token-key → 그 역할/티어의 conf 하네스 — 카드의 **미사용 프로필** 판정 입력.

    카드가 선언하는 하네스(렌더 소스 경로)와 이 값이 다르면 그 카드는 이번 형상에서 스폰되지
    않는다 — render 가 값 대신 사유를 중화로 남긴다(`render_rules.unused_delegate_profiles`).
    키 부재/빈값은 담지 않는다(판정 불가 = 현행 거동 유지).
    """
    harness_keys = render_rules.DELEGATE_HARNESS_CONF_KEYS
    return {
        token_key: conf[conf_key]
        for token_key, conf_key in harness_keys.items()
        if conf.get(conf_key)
    }


def _operational_from_local_conf(conf: Mapping[str, str]) -> tuple[dict[str, str], list[str]]:
    """local.conf 값(`conf`)의 operational 해소값을 adapter_render의 token-key dict 로 변환.

    local.conf 키(lowercase) → operational token key(uppercase). board.py init 이 안 쓴 키는
    포함하지 않는다(빈값 강제 안 함). 출하 어댑터의 operational 토큰은 import sed 로 이미
    리터럴이라 render 시점엔 보통 부재 — 이 매핑은 재렌더가 그 토큰을 만났을 때 local.conf
    단일 진실로 재유도하기 위한 것.

    **값이 빈 문자열인 키도 dict 에서 제외**한다(부재와 동일 취급) — 빈값을 그대로
    넘기면 렌더가 토큰을 빈 문자열로 silent 치환해(예: `project.name=` 빈값 → description 이
    " 프로젝트") 탐지 신호가 사라진다. 제외하면 토큰이 잔존해
    render 의 _assert_no_leak 가 leak 으로 잡는다(silent-empty = leak 클래스). 제외된 빈값
    token-key 목록을 함께 반환해 render_adapter 가 leak 힌트("값을 채우라")에 싣게 한다.

    반환: (operational dict, 빈값이라 제외된 token-key 목록). 디스크 읽기는 호출 경계에서 실행당 한
    번이다 — 저장(`apply_conf`)은 쓰기 전의 제안 값을, 갱신은 dest local.conf 를 넘긴다.
    """
    operational: dict[str, str] = {}
    empty_keys: list[str] = []
    for conf_key, token_key in _local_conf_operational_map().items():
        if conf_key not in conf:
            continue
        if conf[conf_key] == "":
            empty_keys.append(token_key)
            continue
        operational[token_key] = conf[conf_key]
    return operational, empty_keys


def _render_text(
    home,
    source_path: Path,
    conf: Mapping[str, str],
    entry_notation_template: str | tuple[str, ...] | list[str] | None = None,
    flat_command_skill: str | None = None,
    codex_operational_skill: str | None = None,
    card_path: str | None = None,
) -> str:
    """source 템플릿을 local.conf 값(`conf`)으로 렌더한 텍스트 — `@render` 렌더 규칙의 유일한 구현.

    local.conf 의 operational 값을 plain replace 로 채운다(free-form 은 pm_import FILL 채널이
    canonical home 에서 전담). 결과는 자족(잔여 `{{...}}` 0·assertion).
    호출부(plan·apply·저장 `apply_conf`)가 dst 와 비교/기록한다.

    `app.adapter_render` 를 실행 Home(`home`, 카드 렌더의 코드 inventory·조회 위치)으로 직접 호출하며
    RenderLeakError를 그대로 전파한다.

    `card_path` 는 이 산출물의 **인스턴스 상대 좌표**(계획의 dest 경로)다 — 카드 하네스 판정
    (미사용 프로필 중화)의 입력이라, 상류 절대경로가 아니라 dest 좌표를 넘겨야 체크아웃 위치가
    판정을 흔들지 않는다.
    """
    operational, empty_keys = _operational_from_local_conf(conf)
    text = fs.read_text_shared(Path(source_path), encoding="utf-8")
    rendered = adapter_render.render_adapter(
        home,
        text,
        operational=operational,
        empty_keys=empty_keys,
        template_dir=entry_notation_template,
        source=str(source_path),
        delegate_harness=_delegate_harness_from_local_conf(conf),
        card_path=card_path,
    )
    if flat_command_skill is not None:
        rendered = _render_flat_command_reference(rendered, flat_command_skill)
    if codex_operational_skill is not None:
        rendered = _render_codex_operational_detail(
            rendered, codex_operational_skill
        )
    return rendered


def render_skill_entry_notation(
    home,
    text: str,
    template_dir: str | tuple[str, ...] | list[str],
    *,
    source: str | None = None,
) -> str:
    """호출 표기 최소 렌더 — board mirror 판정도 같은 경로를 쓴다. 스킬 이름은 실행 Home 의 inventory 다."""
    return render_rules.render_skill_entry_notation(
        text, template_dir, source=source,
        skill_names=skill_inventory._installed_skill_entry_names(home),
    )


def _render_skill_entry_text(
    home,
    source_path: Path,
    template_dir: str | tuple[str, ...] | list[str],
) -> str:
    """--target export용 파일 wrapper — operational은 건드리지 않고 호출 표기만 치환."""
    text = fs.read_text_shared(Path(source_path), encoding="utf-8")
    return render_skill_entry_notation(
        home, text, template_dir, source=str(source_path)
    )


_FLAT_COMMAND_DETAIL_LINK = (
    "[references/operational-details.md](references/operational-details.md)"
)


def _flat_command_skill_name(relpath: str) -> str | None:
    """OpenCode 사람 command의 평탄 dest 좌표에서 skill 이름을 파생한다."""
    path = Path(str(relpath).replace("\\", "/"))
    if path.parent.as_posix() != ".opencode/command" or path.suffix != ".md":
        return None
    return path.stem


_FLAT_COMMAND_ARGUMENTS_SECTION = (
    "\n## 이번 호출 인자\n\n"
    "`$ARGUMENTS` — 사용자가 슬래시 명령 뒤에 준 값이다(하네스가 치환한다). 비어 있으면 인자 없이"
    " 부른 것이다. 이 카드가 인자를 받는 진입이면 그 값을 그대로 backbone 에 넘기고, 인자를 받지"
    " 않는 진입이면 값이 붙어 온 것을 사용자에게 알리고 인자 없이 다시 부르게 안내한다.\n"
)


def _render_flat_command_reference(text: str, skill_name: str) -> str:
    """평탄 command의 canonical detail 링크를 skill별 생성 경로로 rewrite하고 인자 절을 붙인다.

    모델 skill은 ``<skill>/SKILL.md`` 옆 ``references/``를 쓰지만 사람 command는
    ``command/<skill>.md``라 같은 상대 링크가 한 공용 파일로 충돌한다. command 산출에만
    skill 이름을 끼워 넣고 원본 카드와 모델 표면은 그대로 둔다.

    **인자 전달**: opencode 커맨드 파일은 본문이 곧 프롬프트라 `$ARGUMENTS` 를 적지 않으면
    사용자가 `/pm-bootstrap --task X` 로 준 인자가 카드에 닿지 않는다(실측). 모델 skill 표면은
    하네스가 호출문을 그대로 보여주므로 이 절은 command 사본에만 붙인다.
    """
    count = text.count(_FLAT_COMMAND_DETAIL_LINK)
    if count != 1:
        raise ValueError(
            f"OpenCode flat command {skill_name!r} operational detail 링크가 "
            f"정확히 1개가 아니다: {count}"
        )
    target = (
        "[references/operational-details.md]"
        f"(../../.claude/skills/{skill_name}/references/operational-details.md)"
    )
    return text.replace(_FLAT_COMMAND_DETAIL_LINK, target) + _FLAT_COMMAND_ARGUMENTS_SECTION


_CODEX_OPERATIONAL_PATHS = {
    "pm-dev-delegate": (
        ".agents/skills/pm-dev-delegate/references/operational-details.md"
    ),
    "pm-review": ".agents/skills/pm-review/references/operational-details.md",
}


def _codex_operational_skill_name(relpath: str) -> str | None:
    normalized = str(relpath).replace("\\", "/").strip("/")
    return next(
        (skill for skill, path in _CODEX_OPERATIONAL_PATHS.items()
         if normalized == path),
        None,
    )


def _replace_exactly_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise ValueError(f"{label} rewrite 입력이 정확히 1개가 아니다: {count}")
    return text.replace(old, new)


def _render_codex_operational_detail(text: str, skill_name: str) -> str:
    """공유 operational detail을 Codex override 계약으로 최소 변환한다."""
    if skill_name == "pm-dev-delegate":
        text = _replace_exactly_once(
            text,
            "> 아래 절은 상시 카드에서 분리한 원문이다. 해당 상황에서만 읽는다.",
            "> 아래 절은 Codex 상시 카드에서 분리한 원문이다. 해당 상황에서만 읽는다.",
            label=skill_name,
        )
        text = _replace_exactly_once(
            text, ".claude/agents/developer.md", ".codex/agents/developer.toml",
            label=skill_name,
        )
        return _replace_exactly_once(
            text, ".claude/agents/code-reviewer.md",
            ".codex/agents/code-reviewer.toml", label=skill_name,
        )
    if skill_name == "pm-review":
        old = (
            "**Claude PM은 이 문서의 `additional_reviewer.py` 실 실행 커맨드를 Bash 툴로 호출할 때\n"
            "`timeout: 29300000`(ms)을 반드시 명시한다.** 이는 CLI `--timeout`(리뷰어 벽시계)이 아니라\n"
            "호출층 Bash 툴 파라미터다. Windows 진입 규약은 상시 `SKILL.md`에 남아 있다."
        )
        return _replace_exactly_once(
            text, old,
            "Codex 전용 실행 규율과 환경별 명령 문법은 상시 `SKILL.md`가 단일 진실이다.",
            label=skill_name,
        )
    raise ValueError(f"미등록 Codex operational detail: {skill_name}")


def _is_text_source(source_path: Path) -> bool:
    """source 가 UTF-8 텍스트로 읽히는가 — render 대상 판정의 유일한 형식 조건.

    옛 `.md` 확장자 열거를 대체한다: 확장자는 열린 집합(하니스가 새 형식을 들여온다)이라 열거하면
    새 형식이 조용히 미커버로 남는다(codex `.toml`). render 가 실제로 요구하는 건 "텍스트로 읽어
    plain replace 할 수 있는가" 뿐이므로 그것만 본다 — 바이너리 리소스는 False → byte-copy.
    IO 실패도 보수적으로 False(byte-copy·기존 동작).
    """
    try:
        fs.read_text_shared(Path(source_path), encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return False
    return True


def _rendered_text_matches_dest(rendered: str, dst) -> bool:
    """렌더 산출물이 dest 현재 내용과 같은가 — **표기만 다르면 같고, 1자라도 다르면 다르다**.

    render 계열 '변경 없음' 판정의 **단일 대조 규칙**이다: 전면 렌더(`@render`)와 호출 표기·flat
    command·codex operational 최소 렌더가 같은 축을 쓴다. 한쪽만 raw bytes 로 보면 CRLF
    체크아웃(Windows)에서 그 분기가 **수렴을 보고하지 못한다** — 대조는 raw 인데 쓰기
    (`_write_rendered_text`)는 dest 표기를 보존하므로, 내용이 같은 파일이 매 계획에서 `update` 로
    올라오고 재기록 후에도 bytes 가 그대로다. 내용이 상하진 않지만 dry-run 이 상시 오탐이고
    mtime/diff 가 흔들리며 실제 drift 가 그 소음에 묻힌다. 실측 두 계층: `--target`
    /`--all-targets` 전파(`render_enabled=False` → `@render` 선언도 이 분기로 온다·flat command
    사본 14건)와 채택자 `--from` 경로의 비-render 엔트리(공유 wiki 4건·zero-change 경계 붕괴).

    정규화는 **개행 표기에만** 적용한다. 그 밖의 차이는 그대로 살아 있어 한 글자 변경도 update 다
    (판정을 무디게 만들지 않는다). 읽기 실패·비-UTF8 dest 는 보수적으로 '다름'(False) — plan 이
    그 path 를 change 로 띄워 apply 가 명확히 실패하게 한다(침묵 폴백 금지)."""
    try:
        current = fs.read_bytes_shared(dst).decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return render_io._normalize_newlines(rendered) == render_io._normalize_newlines(current)


def _dest_relative_path(dst, dest_root) -> str | None:
    """dst 의 **인스턴스 상대 좌표** — 관계가 성립하지 않으면 None(판정 불가는 무판정).

    계획(`apply`)은 manifest 좌표(`_r`)를 그대로 들고 있으나 변경검출(`_render_eq_dst`)은 dst 만
    받으므로, 같은 좌표를 여기서 파생해 두 경로가 같은 판정 입력을 쓰게 한다."""
    try:
        return Path(dst).relative_to(Path(dest_root)).as_posix()
    except (ValueError, TypeError):
        return None


def _render_eq_dst(
    home,
    sp: Path,
    dst: Path,
    dest_root: Path,
    conf: Mapping[str, str],
    entry_notation_template: str | tuple[str, ...] | list[str] | None = None,
    flat_command_skill: str | None = None,
    codex_operational_skill: str | None = None,
) -> bool:
    """render path 의 '변경 없음' 정직 판정 — 렌더 산출물 == dst 현재 내용.

    filecmp.cmp(템플릿, dst) 는 render path 에 *틀림*(템플릿은 렌더 산출물과 byte-equal 일 수
    없어 항상 update 오보). 대신 source 를 local.conf 값(`conf`)으로 렌더해 dst 와 비교한다.
    렌더 실패는 그대로 올라간다 — 계획의 렌더러는 템플릿과 같은 세대다(갱신의 완료 단계는 엔진 코드를
    받은 뒤 새 프로세스로 돈다). 대조 규칙 자체는 `_rendered_text_matches_dest` 하나뿐이다(최소 렌더
    분기와 판정 사본 0).
    """
    rendered = _render_text(
        home, sp, conf, entry_notation_template, flat_command_skill,
        codex_operational_skill, _dest_relative_path(dst, dest_root),
    )
    return _rendered_text_matches_dest(rendered, dst)


class _RenderOutput(NamedTuple):
    """메모리에서 다 그린 `@render` 산출물 하나 — 쓰기는 전부 그린 뒤에만 한다."""
    relpath: str
    dst: Path
    source: Path
    text: str


def _render_outputs(home, changes: list[tuple], conf: Mapping[str, str]) -> list[_RenderOutput]:
    """render change 전부를 `_render_text` 로 메모리에서 그린다 — 하나라도 렌더가 거부되면 그 예외가
    그대로 올라가고 아무 파일도 쓰지 않는다(뒤 카드의 실패가 앞 카드를 바꾼 채 남기지 않는다)."""
    outputs: list[_RenderOutput] = []
    for rel, sp, dst, _kind in changes:
        relpath = str(rel).replace("\\", "/")
        outputs.append(_RenderOutput(relpath, Path(dst), Path(sp), _render_text(
            home, Path(sp), conf, getattr(dst, "entry_notation_template", None),
            getattr(dst, "flat_command_skill", None),
            getattr(dst, "codex_operational_skill", None),
            card_path=relpath,
        )))
    return outputs


def _write_render_outputs(outputs) -> None:
    """다 그린 산출물을 차례로 쓴다 — dest 의 현재 개행 표기를 지킨다(`_write_rendered_text`)."""
    for output in outputs:
        output.dst.parent.mkdir(parents=True, exist_ok=True)
        render_io._write_rendered_text(output.dst, output.source, output.text)


def apply(home, changes: list[tuple], *, is_hook_set_path=None, package_plan=None) -> None:
    """change 적용 — render 아닌 change 를 계획 순서대로 쓴 뒤, render change 전부를 그리고 나서 쓴다.

    dst 가 `_RenderDst`(render 플래그 운반·plan 산출)면 그 플래그로 분기한다. 평문 Path dst
    (레거시 직접 호출)는 render 비대상 → copy2(후방호환·현 pm_update 동작 불변).

    엔진 패키지 교체 행(`replace-dir`)은 파일 복사가 아니다. 그 행이 있으면 `package_plan`(계획이 낸
    `EnginePackagePlan`)이 반드시 함께 와야 하고, 전달 프로세스와 같은 교체 구간
    (`engine_transfer.engine_package_delivery`)에서 패키지를 먼저 게시한 뒤 나머지 render 아닌 change 를
    쓰고, 그 묶음이 성공하면 이전 패키지 backup 을 정리한다. 계획 값 없이 교체 행만 오거나 둘이 맞지
    않으면 쓰기 전에 실패한다. 복사한 패키지를 이 프로세스가 다시 적재하지 않는다.

    render가 아닌 change를 쓴 뒤 실행 Home(`home`)으로 render change 전부를 메모리에서 그린다.
    렌더가 전부 성공한 뒤 `_write_render_outputs`로 기록한다. 하나라도 거부되면 render 파일은
    바뀌지 않는다(앞 단계의 파일·게시한 패키지는 이미 쓰였을 수 있고 되돌리지 않는다). 렌더 값은 dest
    local.conf를 한 번 읽는다.

    engine.manifest self-prop overwrite 는 add-harness guest 절을 재부착한다(위 헬퍼). 그 재부착에
    함께 기록할 파생 엔진 행은 dst(`_RenderDst.guest_backfill_lines`)가 실어 나른다 — `entry_
    notation_template` 과 같은 운반 방식이다.

    **훅 세트 파일만 원자 교체**한다 — 하네스가 실행 중에 읽는 파일을 copy2 로 덮으면
    truncate→채움 창에 부분 파일이 실행된다. 그 밖의 파일은 현행 copy2 그대로다(전 파일 확대는
    이 클래스가 요구하지 않는다). `is_hook_set_path` 는 호출부가 **상류 세대**로 해소해 넘긴다
    (`resolve_hook_set_predicate`) — 미지정이면 로컬 세대로 폴백하는데, 그 폴백은 이번 실행이
    새로 추가하는 훅 경로를 모른다(구세대 선언).
    """
    hook_set_path = (is_hook_set_path if is_hook_set_path is not None
                     else hook_sets.resolve_hook_set_predicate(home))
    engine_transfer.change_file_count(changes, package_plan)  # 교체 행 ↔ 계획 값 대조(쓰기 전).
    file_changes = [change for change in changes if change[3] != "replace-dir"]
    render_changes = [change for change in file_changes if getattr(change[2], "render", False)]
    with (engine_transfer.engine_package_delivery(package_plan)
          if len(file_changes) < len(changes) else contextlib.nullcontext()):
        for _r, sp, dst, _kind in file_changes:
            if getattr(dst, "render", False):
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            if (
                getattr(dst, "entry_notation_template", None) is not None
                or getattr(dst, "flat_command_skill", None) is not None
                or getattr(dst, "codex_operational_skill", None) is not None
            ):

                text = fs.read_text_shared(Path(sp), encoding="utf-8")
                rendered = text
                if getattr(dst, "entry_notation_template", None) is not None:
                    rendered = render_rules.render_skill_entry_notation(
                        rendered,
                        dst.entry_notation_template,
                        source=str(sp), skill_names=skill_inventory._installed_skill_entry_names(home),
                    )
                if getattr(dst, "flat_command_skill", None) is not None:
                    rendered = _render_flat_command_reference(
                        rendered, dst.flat_command_skill
                    )
                if getattr(dst, "codex_operational_skill", None) is not None:
                    rendered = _render_codex_operational_detail(
                        rendered, dst.codex_operational_skill
                    )
                render_io._write_rendered_text(Path(dst), Path(sp), rendered)
            elif str(_r).replace("\\", "/") == manifest_rules._MANIFEST_SELF_REL:
                # engine.manifest self-prop — upstream 사본으로 덮되 guest 절 보존(+파생 행 기록).
                manifest_store._copy_manifest_preserving_guest(
                    sp, Path(dst), getattr(dst, "guest_backfill_lines", None))
            elif hook_set_path(_r):
                engine_transfer._atomic_copy2(Path(sp), Path(dst))
            else:
                shutil.copy2(sp, dst)
    if not render_changes:
        return

    first_rel, _sp, first_dst, _kind = render_changes[0]
    conf = install_conf._read_local_conf(_dest_root_for(first_dst, first_rel) / ".project_manager" / "local.conf")
    _write_render_outputs(_render_outputs(home, render_changes, conf))


def _dest_root_for(dst: Path, rel: str) -> Path:
    """change 의 dst 절대경로와 그 repo-기준 relpath 로 dest_root 를 역산한다.

    dst = dest_root / rel 이므로 dst 에서 rel 컴포넌트 수만큼 거슬러 올라가면 dest_root.
    plan 이 dst 를 effective_dest/r 로 만들었으므로 정확히 복원된다(render path 의 local.conf
    조회 기준).
    """
    parts = Path(rel).parts
    root = Path(dst)
    for _ in parts:
        root = root.parent
    return root
