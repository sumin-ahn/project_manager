"""구 진입 문서·설정 키의 안전 이관 (층: app)."""
from __future__ import annotations

from pathlib import Path
import base64
import datetime
import re
import shutil
import zlib
from pm_engine.app import update_render
from pm_engine.domain import conf_schema
from pm_engine.domain import jsonc
from pm_engine.domain import render_rules
from pm_engine.infra import conf_store
from pm_engine.infra import fs
from pm_engine.infra import install_conf
from pm_engine.infra import install_files


# ── 진입 doc 세대 마이그레이션 ────────────────────────────────
# 기존 채택자의 구형 진입 doc(자족 매뉴얼형 opencode `AGENTS.md`·~22KiB)을 신형(harness-neutral
# 공통 코어 + `.opencode/pm-instructions.md` + `opencode.jsonc` `instructions` 배열)으로 수렴시킨다
# — 2세대 영구 공존 차단(사용자 발의 "신형 전환 선택제=관리 분기"). self-update 흡수 경로 한정이며
# `--target` 엔진 export 는 비발화(skew/selfheal 와 동일 경계).
#
# 판정 = **미수정 여부 단 하나**(mechanize·추측 0). @render 치환(operational 토큰)·manual-fill TODO
# 마커가 채택자본을 세대 원본에서 벌려 놓으므로 순수 해시 대조는 불가능하다(ticket 열린 질문). →
# **치환-불변 정규화**로 판정한다: (1) manual-fill 마커(import_fill._mark_todos)를 벗겨 정규화하고,
# (2) 세대 원본에서 operational 토큰을 줄-경계 wildcard 로, free-form 토큰(`{{PROJECT_CONSTRAINTS}}`)
# 을 *리터럴*(미채움=pristine 요구)로 둔 패턴에 re.fullmatch 한다. operational=출하 렌더(전 채택자
# 결정적)라 wildcard(=미수정), free-form=채택자 FILL 영역이라 리터럴 요구(채웠으면 커스텀 흔적→무손
# loud). 이로써 local.conf 가 tagline/date 를 보존하지 않아도(board.py init 은 `runtime.py`·
# `test.cmd`·`project.name` 만 기록) 세대 판정이 성립한다 — 재렌더 대조(local.conf 미보유 토큰서 실패)보다 강건.
# 매칭 시 operational 값을 *포획*해 신형 재렌더에 재사용(채택자 tagline 보존).

# import_fill._mark_todos 가 manual-fill 시 free-form placeholder 줄 끝에 덧붙이는 마커 — 정규화로
# 벗겨낸다(세대 원본엔 없음). pm_import 리터럴과 동일해야 한다(단일 진실·거기서 바뀌면 여기도).
_ENTRY_DOC_MANUAL_TODO_MARKER = " <!-- TODO: 손으로 채우세요 -->"


# 구형 opencode AGENTS.md 의 H1 판별자 — 신형 title 은 "PM 어댑터 공통 코어"(이 문자열 부재). 세대
# clean-match 실패 시 "구형이나 수정됨(→loud) vs 신형/무관(→no-op)" 을 가르는 기계 신호.
_ENTRY_DOC_OLD_GEN_MARKER = "# AGENTS.md — opencode PM 어댑터"


# opencode.jsonc `instructions` 배열에 idempotent 추가할 신형 지침 경로(@source 전파).
_ENTRY_DOC_PM_INSTRUCTIONS_REL = ".opencode/pm-instructions.md"


# 중앙 백업 디렉토리 — pm_import 백업 채널 재사용(BACKUP_DIR_NAME 미러·relpath 미러링). 자동 전환
# 시 원본 AGENTS.md·opencode.jsonc 를 `<dest>/.pm_import_backups/<DATE>/<relpath>` 로 보존한다.
_ENTRY_DOC_BACKUP_DIR = ".pm_import_backups"


# 어댑터 `{{TOKEN}}` placeholder 스캔 — 세대 패턴 빌드용(operational=wildcard·그 외=리터럴 분류).
_ENTRY_DOC_TOKEN_RE = re.compile(r"\{\{([A-Z_]+)\}\}")


# 역대 출하 opencode AGENTS.md 세대(구형·자족 매뉴얼형)의 *원본* 텍스트 = fingerprint 자산. 채택자
# 사이트(프레임워크 git history 없음)에서도 자족적으로 세대 판정을 하려면 원본 텍스트가 필요하므로
# (해시만으론 치환-불변 대조 불가·위 근거) 엔진에 임베드한다. zlib+base64 인코딩 — 이스케이프 함정
# (triple-quote·backslash) 회피 + 콤팩트. 원본 = git `0ccc025`(v1.3.5 출하 세대). provenance 는
# tests/test_entry_doc_migration.py 가 git blob 과 기계 대조(무결성 lock). 세대 추가 시 아래 튜플에
# append("역대 출하본" 확장 지점·해시 목록의 텍스트 판).
_OLD_OPENCODE_AGENTS_V1_3_5_B64 = (
    "eNqtXHtzU0eW/9+fohdmZyUFSTavJF7MFgF2ll1eFZjdmWIpXyFdGw2ypJVkCDXMlgGZNdjZ2BMbZLA98sbEJuNUZCOw2JDa"
    "Kn8U/tS9+g77O+d034dsT0hqqATs++g+ffo8fufRd7868avT5y9fSoxk1LuxWVUo2vl0IWOri+eU+7jpTM10qo2enuP+Dbfa"
    "csdr7mJTuZML7tKMM11XztyUcpemnS/mlbO84Hy54EzXnEezyl2tukvjbn06oWIxeqUzNeUuvqWfLp6LXCwVfmenK+pcKp8a"
    "tktR5T5turW77mIN82E4d3nDrd9VzuqK87DpPqbXnMmVRCymTuZSoyDlJNHjvKniDXe5Gab/7NlzEdDifr+uhu2RkZRKqs7T"
    "KffeusK0USYf/zvrLbe64KxO0YyLP7Rf1pX7aAX/4VVmwWJVOa/HOo9auIa/naUX21tCZmdugajpOY5Xnakxd3FGtZtj7ZfV"
    "fuWvVA2EaO3cH2u/uQ9ylXXy7IlfnzoNtlsJ5UyugT/trRbGoYmdzQn3ybdM41zVmZxwFt/GYqE9mFzpPH6onG/WnM9bkWuj"
    "2VxGFUvZkVTptvpAOdUVvNuZbDitKWVVUuUblqoUCjnlLmA7qlH3yTSYutTepE1bx/rdiXrn8QStRkVOnPo03tvbe1RtbxED"
    "PnYba9Genv37VWe2SvysL4INCotX7kq1p+f3v7/46YV/Pn3y8uDlE786e+b86T/8oefY38Tj6vKFUxf6lX/7/IlzuKfajTEw"
    "vek++S9ibWdOxASCBPaOL7iLLXd1TPX950HemaXnCRWPH+fpexN7LF9FQKbzota5P6GvgN44+AkJK5TS1+1ypZSqFCBeA8oq"
    "jlgeoyBz+YqK9NEKabVhNkY6XzSdxkYU4tajFI1HD3aNAVGMWAlDV5JHLCeLI7StNKQ1gsv95mkrmlAlO5e6bZj8MWSJtYlF"
    "s17FREqVR4t26Wa2XChtb8lzB3vBtgXnm/EocY+kUqtgFVRl7IpdGsnms+VKNk0MpW0tF1O38iBO89eIHakPa4nlsbI0mgeL"
    "hRW0MHfhLUmgW1t3GrPO8xaTpOWMFGd7q/PHsfbrKQgAiJ+bANW0p1B53tnGXGdpKkgiVGxhnSWLuaKcly3a2cUxVbmezTMx"
    "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"
)


_OLD_OPENCODE_AGENTS_GENERATIONS = (_OLD_OPENCODE_AGENTS_V1_3_5_B64,)


def _decode_entry_doc_generation(b64: str) -> str:
    """임베드된 세대 원본(zlib+base64) → 텍스트."""
    return zlib.decompress(base64.b64decode(b64)).decode("utf-8")


def _entry_doc_operational_keys() -> frozenset:
    """세대 대조에서 wildcard(=출하 렌더)로 볼 operational 토큰 집합 = render_rules.OPERATIONAL_KEYS.
    그 밖의 `{{...}}`(free-form·`{{PROJECT_CONSTRAINTS}}`)은 리터럴(pristine 요구)이다.
    실행 패키지의 토큰 선언을 직접 소비하므로 새 토큰이 소비자마다 누락되지 않는다.
    """
    return frozenset(render_rules.OPERATIONAL_KEYS)


def _build_entry_doc_pattern(generation_text: str, operational_keys) -> tuple[str, dict]:
    """세대 원본 → (re.fullmatch 패턴, group→token 맵). operational 토큰은 줄-경계 wildcard 캡처
    그룹(`[^\\n]*`), 그 외 `{{...}}`(free-form)는 리터럴, 나머지 텍스트는 re.escape.

    operational 값은 출하 렌더라 채택자마다 다르나 미수정 신호(줄 내 값)이므로 `[^\\n]*`. free-form
    토큰은 채택자 FILL 영역이라 리터럴로 둬 미채움(pristine)일 때만 매칭한다(채웠으면 불일치→loud)."""
    parts: list[str] = []
    group_token: dict[str, str] = {}
    last = 0
    gi = 0
    for m in _ENTRY_DOC_TOKEN_RE.finditer(generation_text):
        parts.append(re.escape(generation_text[last:m.start()]))
        tok = m.group(1)
        if tok in operational_keys:
            name = f"op{gi}"
            gi += 1
            parts.append(f"(?P<{name}>[^\\n]*)")
            group_token[name] = tok
        else:
            parts.append(re.escape(m.group(0)))  # free-form/미상 토큰 = 리터럴(pristine 요구)
        last = m.end()
    parts.append(re.escape(generation_text[last:]))
    return "".join(parts), group_token


def _match_entry_doc_generation(
    generation_text: str, adopter_text: str, operational_keys
) -> dict | None:
    """정규화한 채택자 AGENTS.md 가 세대 원본 구조와 byte-match 하면 포획 operational 값 dict, 아니면 None.

    정규화 = manual-fill 마커 제거(import_fill._mark_todos). operational 토큰의 복수 occurrence 는
    같은 값이어야 한다(출하 시 uniform 치환) — 불일치면 손편집 신호로 None(안전·loud 로 낙하)."""
    normalized = adopter_text.replace(_ENTRY_DOC_MANUAL_TODO_MARKER, "")
    pattern, group_token = _build_entry_doc_pattern(generation_text, operational_keys)
    m = re.fullmatch(pattern, normalized)
    if m is None:
        return None
    values: dict[str, str] = {}
    for name, tok in group_token.items():
        v = m.group(name)
        if tok in values:
            if values[tok] != v:
                return None  # 같은 토큰 occurrence 값 불일치 → 비-uniform(손편집)·안전 낙하
        else:
            values[tok] = v
    return values


def _render_new_entry_doc(
    new_template_text: str, operational: dict, operational_keys
) -> str | None:
    """신형 AGENTS.md 템플릿 → operational 치환 산출물(free-form 리터럴 유지). operational leak
    잔존(값 미보유) 시 None — 미완 렌더 파일을 쓰지 않는다(안전·loud 로 낙하).

    render_adapter(assert_no_leak)는 free-form `{{PROJECT_CONSTRAINTS}}` 에서 raise 하므로 쓰지
    않는다 — operational 만 채우고 free-form 은 pristine 유지(신선 import --fill manual 과 동형)."""
    text = new_template_text
    for key, val in operational.items():
        if val:
            text = text.replace("{{" + key + "}}", str(val))
    for key in operational_keys:
        if "{{" + key + "}}" in text:
            return None  # operational 미해소 잔존 — 자족 위반 방지(free-form 은 허용)
    return text


def _scan_array_end(masked: str, body_start: int) -> int:
    """배열 `[` 직후 body_start 부터 매칭되는 `]` 위치를 문자열/중첩 존중으로 찾는다(배열 body 끝).

    문자열 리터럴 내 `]`·중첩 `[...]` 은 건너뛴다. 닫는 `]` 부재(비정상)면 끝(len) 반환."""
    i, n = body_start, len(masked)
    depth = 0
    in_str = False
    while i < n:
        c = masked[i]
        if in_str:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
        elif c == "[":
            depth += 1
        elif c == "]":
            if depth == 0:
                return i
            depth -= 1
        i += 1
    return n


def _find_toplevel_instructions(masked: str) -> tuple[int | None, int | None, int | None]:
    """주석-마스킹된 jsonc 에서 **최상위(depth==1)** `"instructions"` 배열을 brace-depth 추적으로 찾는다.

    중첩 객체(agent/provider 블록 등)의 `"instructions"` 는 무시한다 — opencode 가 읽는
    진입 지침 배열은 최상위 키 하나다. 문자열 리터럴 내 brace/bracket 은 세지 않는다(문자열 상태 추적).

    반환 (body_start, body_end, root_end):
      - 최상위 instructions 배열 존재 → (배열 `[` 직후, 닫는 `]` 위치, None): 검사/append 용.
      - 부재 → (None, None, 최상위 여는 `{` 직후 오프셋): 신설 블록 삽입 위치.
      - 최상위 `{` 부재(비정상) → (None, None, None)."""
    i, n = 0, len(masked)
    depth = 0
    root_end: int | None = None
    in_str = False
    while i < n:
        c = masked[i]
        if in_str:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            # 최상위(depth==1)의 "instructions" 키만 후보 — 중첩(depth>1)은 무시.
            if depth == 1:
                m = jsonc._INSTR_KEY_RE.match(masked, i)
                if m:
                    body_start = m.end()  # 여는 `[` 직후
                    return body_start, _scan_array_end(masked, body_start), None
            in_str = True
            i += 1
            continue
        if c == "{":
            depth += 1
            if depth == 1 and root_end is None:
                root_end = i + 1  # 최상위 여는 `{` 직후
            i += 1
            continue
        if c == "}":
            depth -= 1
            i += 1
            continue
        i += 1
    return None, None, root_end


def _ensure_jsonc_instructions(jsonc_text: str) -> tuple[str, bool]:
    """opencode.jsonc **최상위** `instructions` 배열에 신형 지침 경로를 idempotent 추가(comment-preserving).

    반환 (new_text, changed). 최상위 배열에 이미 (비-주석) 원소로 있으면 무변경. 최상위 배열이
    있으나 경로가 없으면 배열 앞에 삽입. 최상위 배열이 없으면 최상위 `{` 직후 신설 블록 삽입.
    JSONC(주석)라 json.load 불가 — 주석을 **오프셋 보존 마스킹**한 사본 위에서 **brace-depth 추적**
    으로 위치를 구하고 원본에 같은 오프셋으로 write 한다(비파괴·주석·타 키·provider 보존).

    **최상위(depth==1) 한정**: 중첩 객체(agent/provider)의 `"instructions"` 가 파일에서
    먼저 나와도 그 중첩 배열에 삽입하지 않는다 — opencode 가 로드하는 진입 지침은 최상위 키다.
    등록-확인은 **quoted-string 원소 정확 대조**(substring 오인 방지·주석-아웃/`.bak` suffix)."""
    rel = _ENTRY_DOC_PM_INSTRUCTIONS_REL
    masked = jsonc._mask_jsonc_comments(jsonc_text)  # 주석 blank(오프셋 == 원본)
    body_start, body_end, root_end = _find_toplevel_instructions(masked)
    if body_start is not None:
        elements = jsonc._JSONC_STRING_RE.findall(masked[body_start:body_end])  # 주석-아웃 원소 제외
        if rel in elements:
            return jsonc_text, False  # idempotent — 최상위 배열에 이미 등록
        # **빈 배열**에 `"…",` 를 넣으면 뒤에 이을 원소가 없어 후행 쉼표만 남는다
        #   (`[\n    "…",]` — strict JSON parse 실패·실측). 비어 있으면 쉼표 없이 단독 원소 형태로
        #   (닫는 괄호 앞 개행 정렬·신설 블록과 같은 모양) 넣고, 아니면 현행대로 앞머리에 쉼표를 달아
        #   넣는다. 판정은 **마스킹본 배열 본문에 비-공백이 있는가** — 문자열 원소 유무로 좁히면
        #   비-문자열 원소(`[123]`)를 "빈 배열" 로 오인해 같은 결함이 다른 모양으로 재발한다(클래스
        #   폐쇄). 주석은 마스킹으로 공백이 되므로 주석-only 배열은 자연히 "비어 있음" 이다.
        insertion = (
            f'\n    "{rel}",' if masked[body_start:body_end].strip()
            else f'\n    "{rel}"\n  ')
        return jsonc_text[:body_start] + insertion + jsonc_text[body_start:], True
    if root_end is None:
        return jsonc_text, False  # 최상위 `{` 없음 — 비정상 config·무변경(안전)
    block = f'\n  "instructions": [\n    "{rel}"\n  ],'
    return jsonc_text[:root_end] + block + jsonc_text[root_end:], True


def _entry_doc_backup_root(dest_root: Path) -> Path:
    """중앙 백업 루트 `<dest>/.pm_import_backups/<DATE>/` (pm_import 채널 재사용·relpath 미러)."""
    return Path(dest_root) / _ENTRY_DOC_BACKUP_DIR / datetime.date.today().isoformat()


def _entry_doc_backup(dest_root: Path, rel: str, backup_root: Path) -> None:
    """`dest_root/rel` 을 `backup_root/rel` 로 복사(중앙 백업·relpath 미러링). 부재면 무동작."""
    src = Path(dest_root) / rel
    if not src.is_file():
        return
    install_files.ensure_backup_dir_ignored(dest_root)
    dst = Path(backup_root) / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def migrate_renamed_conf_keys(effective_dest: Path, *, write: bool) -> dict[str, str]:
    """local.conf 이름 이동의 읽기·계획·쓰기·후조건을 공용 락 안에서 수행한다.

    새 키의 사용자 값과 대체 없는 폐지 키를 보존한다. dry-run은 읽기만 한다.
    이동 가능한 옛 키 잔류는 완료 실패다. 이미 배달한 엔진 파일은 보존하며 호출부의 완료
    baseline 기록에는 도달하지 않는다. 설정 판독·기록 실패는 그대로 올라간다.
    """
    conf_path = Path(effective_dest) / ".project_manager" / "local.conf"
    if not conf_path.is_file():
        return {}
    if not write:
        result = conf_store.load(conf_path)
        updates = conf_schema.rename_updates(result)
        if not updates:
            return {}
        moved = {old: new for old, new in result.legacy.items()
                 if new is not None and old in updates}
    else:
        with fs.local_conf_write_lock(conf_path):
            result = conf_store.load(conf_path)
            updates = conf_schema.rename_updates(result)
            if not updates:
                return {}
            moved = {old: new for old, new in result.legacy.items()
                     if new is not None and old in updates}
            install_conf._write_conf_keys_locked(conf_path, updates)
            remaining = conf_schema.rename_updates(conf_store.load(conf_path))
            if remaining:
                raise RuntimeError(
                    f"local.conf 키 이동 실패: 잔존 키={', '.join(remaining)}; 파일={conf_path}")
    prefix = "[dry-run] " if not write else "  ✓ "
    print(f"{prefix}local.conf 키 이름 {len(moved)}개 이동{' 예정' if not write else ''} — "
          "갱신이 바꾼 이름이라 갱신이 옮긴다(남은 % 는 쓴 % 로, 공통 사용 한도는 "
          "하네스마다 같은 값으로).")
    for line in conf_schema.migration_lines(moved):
        print(f"  {line}")
    return moved


def migrate_entry_doc(effective_dest: Path, source_root: Path, *, write: bool) -> dict:
    """진입 doc 세대 마이그레이션 — self-update 흡수 경로 한정(호출부가 --target 게이트).

    구형 미수정 opencode `AGENTS.md`(세대 fingerprint clean-match) → 신형 공통 코어로 자동 교체
    (+백업·`opencode.jsonc` instructions 배열 idempotent 추가). 커스텀 흔적(FILL·손편집) → 무손·
    loud 안내. 이미 신형/재실행 → no-op 멱등(부분 전환 시 jsonc instructions 만 복구). `write=False`
    (dry-run)면 판정만 하고 파일을 쓰지 않는다. 반환 dict 는 finding 출력·테스트 단언 공용.

    status ∈ {'not_opencode','no_agents','no_new_template','migrated','loud_manual','noop','recovered'}.
    """
    dest = Path(effective_dest)
    jsonc_path = dest / ".opencode" / "opencode.jsonc"
    agents_path = dest / "AGENTS.md"
    result: dict = {
        "status": "not_opencode", "agents_replaced": False, "jsonc_updated": False,
        "backup_rel": None, "matched_generation": None,
    }
    # opencode 채택자 게이트 — opencode.jsonc 부재면 비-opencode(claude 등)·비발화.
    if not jsonc_path.is_file():
        return result
    if not agents_path.is_file():
        result["status"] = "no_agents"
        return result

    adopter_agents = fs.read_text_shared(agents_path, encoding="utf-8")
    operational_keys = _entry_doc_operational_keys()

    # 세대 clean-match 탐색 (구형 미수정?).
    captured = None
    matched_gen = None
    for idx, b64 in enumerate(_OLD_OPENCODE_AGENTS_GENERATIONS):
        gen_text = _decode_entry_doc_generation(b64)
        captured = _match_entry_doc_generation(gen_text, adopter_agents, operational_keys)
        if captured is not None:
            matched_gen = idx
            break

    if captured is not None:
        # ── 구형 미수정 → 자동 전환 ─────────────────────────────────────────
        new_tmpl_path = source_root / "templates" / "opencode" / "AGENTS.md"
        if not new_tmpl_path.is_file():
            # 신형 목적지(source) 부재 — fail-soft(무손·loud 아님·비정상 source 신호는 타 게이트).
            result["status"] = "no_new_template"
            return result
        # operational: 포획값(tagline 등 local.conf 미보유분) + local.conf(현재 진실·py/name/test_cmd 우선).
        local_op, _empty = update_render._operational_from_local_conf(
            install_conf._read_local_conf(dest / ".project_manager" / "local.conf"))
        operational = {**captured, **local_op}
        new_text = _render_new_entry_doc(
            fs.read_text_shared(new_tmpl_path, encoding="utf-8"), operational, operational_keys)
        if new_text is None:
            # operational 재렌더 미완 — 무손·loud 로 낙하(미완 파일을 쓰지 않는다).
            result["status"] = "loud_manual"
            result["matched_generation"] = matched_gen
            return result
        adopter_jsonc = fs.read_text_shared(jsonc_path, encoding="utf-8")
        new_jsonc, jsonc_changed = _ensure_jsonc_instructions(adopter_jsonc)
        result.update(status="migrated", matched_generation=matched_gen,
                      agents_replaced=True, jsonc_updated=jsonc_changed)
        if write:
            backup_root = _entry_doc_backup_root(dest)
            _entry_doc_backup(dest, "AGENTS.md", backup_root)
            if jsonc_changed:
                _entry_doc_backup(dest, ".opencode/opencode.jsonc", backup_root)
            agents_path.write_text(new_text, encoding="utf-8", newline="\n")
            if jsonc_changed:
                jsonc_path.write_text(new_jsonc, encoding="utf-8", newline="\n")
            result["backup_rel"] = (
                f"{_ENTRY_DOC_BACKUP_DIR}/{datetime.date.today().isoformat()}")
        return result

    # ── clean-match 실패 ────────────────────────────────────────────────────
    if _ENTRY_DOC_OLD_GEN_MARKER in adopter_agents:
        # 구형 세대이나 수정됨(FILL·손편집) → 무손·loud 안내(수동 병합·커스텀 보존).
        result["status"] = "loud_manual"
        return result
    # 신형/무관 — AGENTS.md 미터치. opencode.jsonc instructions 만 idempotent 보장(부분 전환 복구·멱등).
    adopter_jsonc = fs.read_text_shared(jsonc_path, encoding="utf-8")
    new_jsonc, jsonc_changed = _ensure_jsonc_instructions(adopter_jsonc)
    result["jsonc_updated"] = jsonc_changed
    result["status"] = "recovered" if jsonc_changed else "noop"
    if jsonc_changed and write:
        backup_root = _entry_doc_backup_root(dest)
        _entry_doc_backup(dest, ".opencode/opencode.jsonc", backup_root)
        jsonc_path.write_text(new_jsonc, encoding="utf-8", newline="\n")
        result["backup_rel"] = f"{_ENTRY_DOC_BACKUP_DIR}/{datetime.date.today().isoformat()}"
    return result


def _print_entry_doc_migration_finding(result: dict, *, dry_run: bool = False) -> None:
    """migrate_entry_doc 결과를 사람이 읽을 형태로 출력(loud 안내).

    'migrated'/'loud_manual'/'recovered' 만 출력 — 'noop'·'not_opencode'·'no_agents'·
    'no_new_template' 는 조용(정상/무관·노이즈 회피). 전환/복구 자체는 migrate_entry_doc 이 수행."""
    status = result.get("status")
    if status == "migrated":
        verb = "전환 예정" if dry_run else "전환"
        gen = result.get("matched_generation")
        tail = " + opencode.jsonc instructions 배열 추가" if result.get("jsonc_updated") else ""
        print(f"→ 진입 doc 세대 마이그레이션 {verb} — 구형 미수정 opencode AGENTS.md "
              f"(세대 #{gen})를 신형 공통 코어로 교체{tail}.")
        if dry_run:
            print("    (원본은 .pm_import_backups/<DATE>/ 에 백업 예정·적용 안 함)")
        elif result.get("backup_rel"):
            src = "AGENTS.md·opencode.jsonc" if result.get("jsonc_updated") else "AGENTS.md"
            print(f"    백업: {result['backup_rel']}/ (원본 {src})")
    elif status == "loud_manual":
        print("⚠️  진입 doc 세대 마이그레이션 — 구형 opencode AGENTS.md 를 감지했으나 커스텀 흔적"
              "(FILL·손편집)이 있어 자동 전환하지 않는다(무손).")
        print("    신형(공통 코어 + .opencode/pm-instructions.md + opencode.jsonc instructions)으로 "
              "수동 병합하려면:")
        print("      1) templates/opencode/AGENTS.md(신형 공통 코어)로 AGENTS.md 를 교체하고 "
              "커스텀(프로젝트 고유 제약 등)을 프로젝트 고유 제약으로 옮긴다.")
        print("      2) opencode.jsonc 최상위에 "
              '`"instructions": [".opencode/pm-instructions.md"]` 를 추가한다(기존 배열이면 경로 append).')
    elif status == "recovered":
        verb = "추가 예정" if dry_run else "추가"
        print("→ 진입 doc — opencode.jsonc `instructions` 배열에 .opencode/pm-instructions.md "
              f"{verb}(신형 정합·idempotent 복구).")
