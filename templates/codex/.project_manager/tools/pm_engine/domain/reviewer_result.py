"""추가 리뷰어 결과와 설정 값 (층: domain)."""
from __future__ import annotations

from typing import NamedTuple
from pathlib import Path
from typing import Sequence
from pm_engine.domain import conf_schema
import re
from pm_engine.domain import review_verdict



# 프롬프트 형식에서 must-fix 섹션 헤더를 인식하는 정규식
_MUST_FIX_SECTION_RE = re.compile(
    r"\*{0,2}must[- ]fix\*{0,2}(?:\s*\([^)]*\))?\s*:", re.IGNORECASE,
)
# must-fix 섹션 내 "없음/N/A/none" 항목 패턴
_NONE_ITEM_RE = re.compile(r"^(?:없음|n/?a|none)\s*$", re.IGNORECASE)

# ── 설정 ──────────────────────────────────────────────────────────────────


def _conf_default(key: str) -> str | None:
    """이 conf 키의 엔진 기본값 — 정본은 `conf_schema.KEY_REGISTRY` 다(선언이 없으면 None)."""
    return conf_schema.default_for(key)


def _conf_default_int(key: str) -> int:
    """정수 기본값 — 선언이 없는 키를 부르면 `TypeError` 로 멈춘다(값을 지어내지 않는다)."""
    return int(_conf_default(key))


def knob_value_key(conf: dict[str, str], key: str) -> str | None:
    """노브 값을 공급하는 키 — **신키뿐**. 공급이 없으면 None.

    빈 값은 종전부터 미설정으로 읽어 기본값으로 fail-soft 했다. 그 의미를 그대로 승계해
    "비어 있지 않은 값"만 공급으로 본다.

    공급 판정은 값의 **존재**이지 형식이 아니다 — 신키가 깨진 값(비정수)을 공급하면 해소는
    엔진 기본값으로 간다(구키로 내려가지 않는다).
    """
    return key if conf.get(key, "").strip() else None


def _knob_raw(conf: dict[str, str], key: str) -> str:
    """공급 키의 원문 값 — 공급이 없으면 빈 문자열(호출부가 기본값으로 간다)."""
    supplier = knob_value_key(conf, key)
    return conf.get(supplier, "").strip() if supplier else ""


# ── 추가 리뷰어 대상 해소 (원자 tuple) ──────────────────────────────────────
#
# 사람 역할 이름은 **추가 리뷰어(additional reviewer)** 다 — 팀에 한 명 더 붙는 리뷰어라는 뜻이고,
# 우리가 띄우는 행위자를 `외부` 로 부르지 않는다 — 설정 키는 대상·상한 `additional_reviewer.*`
# 뿐이고, raw 파일 접두·모듈 파일 이름 같은 **이미 기록된 산출물에 박힌 기계 식별자만 그대로**
# 유지한다.
#
# 대상은 위임과 동형의 **원자 tuple**(harness+model 동반 필수·reasoning 선택) 하나뿐이다.
# 모델을 고정하지 않는 자유 문자열 경로는 "어느 모델이 봤는지"를 사후에 알 수 없어 라운드 장부·
# raw 감사가 거짓말을 하게 되므로 폐지했다 — tuple 이 없으면 호출하지 않고 fail-loud 다.
ADDITIONAL_REVIEWER_PREFIX = "additional_reviewer"
ADDITIONAL_REVIEWER_HARNESS_KEY = f"{ADDITIONAL_REVIEWER_PREFIX}.harness"
ADDITIONAL_REVIEWER_MODEL_KEY = f"{ADDITIONAL_REVIEWER_PREFIX}.model"
ADDITIONAL_REVIEWER_REASONING_KEY = f"{ADDITIONAL_REVIEWER_PREFIX}.reasoning"
ADDITIONAL_REVIEWER_KEYS: tuple[str, ...] = (
    ADDITIONAL_REVIEWER_HARNESS_KEY,
    ADDITIONAL_REVIEWER_MODEL_KEY,
    ADDITIONAL_REVIEWER_REASONING_KEY,
)

REVIEWER_SOURCE_STRUCTURED = "structured"
# 모델 축의 **예약 sentinel** — 엔진이 "모델이 고정되지 않았다"를 뜻하던 낱말들이다. 구조화
# 프로필은 정의상 모델을 고정한 tuple 이므로 이 값을 받을 수 없다: 받으면 장부·raw 헤더·stderr 가
# "고정했다는 선언"과 "미고정"을 같은 글자로 적어 감사가 자기 모순을 기록한다.
RESERVED_MODEL_VALUES: frozenset[str] = frozenset({"default", "unpinned-model"})

# 추가 리뷰어의 권한축은 **불변**이다 — 읽기 권위(code-reviewer)로 고정하고 설정으로 올릴 수 없다.
# 리뷰는 저장소를 고치지 않는다(고치는 것은 위임 developer 축의 일이다).
REVIEWER_ROLE = "code-reviewer"

# 실행 시점에만 정해지는 경로는 세 표면(dry-run·stderr provenance·raw)이 **같은 자리표시자**로
# 표시한다. 값이 실행마다 달라지는 토큰을 정체 문자열에 넣으면 세 표면이 서로 다른 문자열을
# 말하게 되고, 그러면 "같은 대상을 말했는가"를 기계가 대조할 수 없다. 실 argv 에는 그 자리에
# 실제 cwd/프롬프트 파일이 들어간다(opencode 만 해당).
REVIEWER_CWD_PLACEHOLDER = "<review-cwd>"
REVIEWER_PROMPT_FILE_PLACEHOLDER = "<prompt-file>"


# ── 결과 파싱 ─────────────────────────────────────────────────────────────


def _extract_must_fix_items(output: str) -> list[str]:
    """must-fix 섹션 헤더 이후의 항목 라인을 추출한다 (표기 편차 처리)."""
    items: list[str] = []
    in_section = False
    for line in output.splitlines():
        if not in_section:
            if _MUST_FIX_SECTION_RE.search(line):
                in_section = True
                after_header = _MUST_FIX_SECTION_RE.sub("", line, count=1).strip()
                if after_header:
                    items.append(after_header)
        else:
            stripped = line.strip()
            if stripped.startswith("**") and stripped.endswith(":"):
                break
            if stripped.startswith("**") and "**:" in stripped:
                break
            if stripped.startswith("- ") or stripped.startswith("* "):
                items.append(stripped.lstrip("-* ").strip())
            elif stripped and not stripped.startswith("#"):
                items.append(stripped)
    return items


def _is_none_items(items: list[str]) -> bool:
    """항목 목록이 "없음/N/A/none" 만으로 구성됐는지 확인한다."""
    if not items:
        return True
    return all(_NONE_ITEM_RE.match(item) for item in items if item)


def parse_verdict(output: str) -> dict[str, bool]:
    """모델 출력에서 판정(통과/반려)·must-fix 존재를 파싱한다.

    반환: {"has_must_fix": bool, "has_pass": bool}. 보수적: 판정 라인 없이 must-fix/반려
    토큰만 있어도 has_must_fix=True. 예외: must-fix 섹션이 "없음/N/A/none" 만이면 False.

    **통과 인정은 `verdict_words` 가 거른 명시 판정선에서만** 나온다. 본문 아무 곳의 '통과' 토큰을
    통과 근거로 세면 두 가지가 깨진다. (a) 코드펜스에 든 `판정: 통과`(= 검토 대상 diff 나 인용물) +
    본문 `must-fix: 없음` 조합이 파서만 통과시키고 오염 검출은 그 라인을 안 세는 **비대칭**이 생긴다.
    (b) 형식을 아예 안 지킨 산문("회귀 통과 확인. 문제 없음.")이 통과로 접힌다 — 실측된 형상이다.
    판정선이 하나도 없으면 통과가 아니라 '판정 불명확'(보수적 exit 1)이며, 그 성질은 파서 단독
    회귀가 소유한다(별도 폴백 분기 없음 — 실측상 no-op 이라 두지 않는다).
    """
    must_fix_items = _extract_must_fix_items(output)
    section_found = bool(_MUST_FIX_SECTION_RE.search(output))

    if section_found and _is_none_items(must_fix_items):
        has_must_fix = False
    elif section_found and must_fix_items:
        has_must_fix = True
    else:
        has_must_fix = any(token in output for token in review_verdict._REJECT_TOKENS)

    declared_verdicts = review_verdict.verdict_words(output)
    kind = review_verdict.verdict_kind(declared_verdicts[0]) if declared_verdicts else review_verdict.VERDICT_UNKNOWN
    has_pass = kind == review_verdict.VERDICT_PASS
    if has_pass:
        if not must_fix_items or _is_none_items(must_fix_items):
            has_must_fix = False
    elif kind == review_verdict.VERDICT_REJECT:
        has_must_fix = True

    return {"has_must_fix": has_must_fix, "has_pass": has_pass}


# ── 회신 품질 검출 (판정 확정 가능성) ─────────────────────────────────────
#
# "이번 판정이 이 리뷰의 것인가"를 회신 본문의 증거로 판정한다: 회신에 옛 리뷰 raw 파일명이나
# 하네스 세션 전사 경로가 인용돼 있으면 그 판정은 이번 라운드의 산출이 아닐 수 있고, 옛 판정
# 블록을 통째로 옮겨 오면 판정 라인이 여러 개가 되어 파서가 고른 한 줄이 이번 판정이라는 보장이
# 사라진다. 이것은 **출력 품질** 검사이지 접근 권한이 아니다 — 입력은 회신 텍스트 하나뿐이고
# 하네스로 갈리지 않는다.
# 검사 대상은 **회신 채널만**이다(`ReviewerOutput.answer`) — 진행 로그에는 프롬프트와 diff 원문이
# 그대로 실려 있어 그것까지 세면 정상 실행이 전부 걸린다(라이브 실측).

# 이 엔진이 만드는 raw 산출물 파일명(`_reserve_output`·pm_delegate 동형). 타임스탬프/pid 자리가
# 있어 diff 본문에서 우연히 나오지 않는다 — 인용됐다면 로컬 산출물 디렉터리를 읽었다는 증거다.
# reviewer/harness 이름에는 `_` 가 들어갈 수 있다(`additional_reviewer_<reviewer>_<타임스탬프>_…`) —
# 이름 문자에서 `_` 를 빼면 그런 배포의 인용을 통째로 놓친다.
_RAW_ARTIFACT_RE = re.compile(
    r"\b(?:additional_reviewer|external_review|pm_delegate)_"
    r"[A-Za-z0-9._-]+_\d{4,}[A-Za-z0-9_.-]*\.txt\b"
)
# 하네스 세션 전사 저장소(실측 경로만 선언 — 추정 경로는 넣지 않는다). 구분자는 `/`·`\` 양쪽을
# 받는다 — Windows 형상(`C:\Users\u\.claude\projects\…`)을 못 보면 그 플랫폼에서 백스톱이 없다.
_SESSION_TRANSCRIPT_RE = re.compile(
    r"[\w~.:/\\-]*(?:\.claude[/\\]projects|\.codex[/\\]sessions)[/\\][\w.:@/\\-]*"
)


class OutputContamination(NamedTuple):
    """리뷰어 출력에 남은 저장소 밖 탐색 흔적."""

    verdicts: tuple[str, ...]
    raw_artifacts: tuple[str, ...]
    transcripts: tuple[str, ...]

    @property
    def verdict_conflict(self) -> bool:
        """판정 라인이 여러 개면서 서로 다른 판정을 말한다 = 어느 게 이번 판정인지 모른다.

        같은 판정의 재진술은 위험이 없어 잡지 않는다(false-red 억제). 서로 다르면 파서가 고른
        한 줄이 이번 리뷰의 판정이라는 보장이 사라지므로 보수적으로 '불명확'이어야 한다.
        """
        return len(self._verdict_kinds()) > 1

    def _verdict_kinds(self) -> set[str]:
        """판정 라인 낱말을 파서와 **같은 함수**(`verdict_kind`)로 접는다.

        허용 토큰이 아닌 판정선(`[통과`·`PASS/REJECT` 같은 템플릿/선택지 echo)은 `unknown` 이라
        고유한 종류로 센다 — 그런 줄이 진짜 판정선과 섞여 있으면 어느 게 이번 판정인지 모른다.
        """
        return {review_verdict.verdict_kind(word) for word in self.verdicts}

    @property
    def contaminated(self) -> bool:
        """오염 신호가 하나라도 있으면 이 판정은 리뷰어 자신의 것이라는 보장이 없다."""
        return bool(self.markers)

    @property
    def markers(self) -> tuple[str, ...]:
        """PM 이 읽을 오염 진단 — 비어 있으면 오염 신호 없음."""
        markers: list[str] = []
        if self.verdict_conflict:
            markers.append(
                f"판정 라인 {len(self.verdicts)}개가 서로 다름 "
                f"({', '.join(self.verdicts)}) — 옛 판정 블록 echo 가능"
            )
        if self.raw_artifacts:
            markers.append(
                "옛 리뷰/위임 raw 파일명 인용: " + ", ".join(self.raw_artifacts)
            )
        if self.transcripts:
            markers.append(
                "하네스 세션 전사 경로 인용: " + ", ".join(self.transcripts)
            )
        return tuple(markers)


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    """등장 순서를 유지한 중복 제거 + 진단 상한."""
    seen: list[str] = []
    for value in values:
        if value not in seen:
            seen.append(value)
        if len(seen) >= 5:      # 표시용 상한(종전 기본값)
            break
    return tuple(seen)


def detect_output_contamination(output: str) -> OutputContamination:
    """리뷰어 출력에서 저장소 밖 탐색 흔적을 찾는다(판정 무관·순수 함수).

    판정 라인은 파서와 같은 `verdict_words` 로 센다 — 검출 범위가 파서보다 좁으면 파서가 집어 든
    echo 라인이 검출을 빠져나간다.
    """
    return OutputContamination(
        verdicts=review_verdict.verdict_words(output),
        raw_artifacts=_unique(_RAW_ARTIFACT_RE.findall(output)),
        transcripts=_unique(_SESSION_TRANSCRIPT_RE.findall(output)),
    )


class LocalConfDivergence(NamedTuple):
    """엔진 conf와 호출 대상 repo conf의 실제 호출 프로필 차이."""

    engine_repo: Path
    engine_conf_path: Path
    target_repo: Path
    target_conf_path: Path
    differences: tuple[str, ...]


class TargetLocalConfReadError(RuntimeError):
    """존재하는 비교 대상 local.conf를 안전하게 읽지 못해 호출을 중단해야 함."""

    def __init__(self, path: Path, cause: BaseException):
        self.path = path
        self.cause = cause
        super().__init__(
            f"대상 local.conf 읽기 실패 — 호출 전에 중단합니다: {path} "
            f"({type(cause).__name__}: {cause})"
        )


def format_local_conf_divergence(
    divergence: LocalConfDivergence, *, surface: str, cwd_label: str,
    resolution_note: str | None = None, source_label: str = "실행 엔진",
) -> str:
    """두 호출 표면 공용 never-block loud 경고 문구."""
    details = "\n".join(f"  · {item}" for item in divergence.differences)
    resolution = resolution_note or (
        f"이번 실행에서는 {source_label} conf가 이깁니다: {divergence.engine_conf_path}\n"
        f"  차단하지 않고 계속합니다. 같은 프로필을 원하면 {cwd_label} conf의 위 키 값을 "
        f"{source_label} "
        "conf와 맞추거나, 의도한 local.conf를 가진 엔진 사본을 실행하세요. 실행 결과 raw 헤더의 "
        "# local_conf/# resolved_profile에도 승자 provenance를 기록합니다."
    )
    return (
        f"경고: local.conf 프로필 분기 감지 ({surface}) — {cwd_label} repo와 {source_label} "
        "REPO가 다르고 호출 프로필/내용 값이 실제로 갈립니다.\n"
        f"  · {source_label} REPO: {divergence.engine_repo}\n"
        f"  · {source_label} conf: {divergence.engine_conf_path}\n"
        f"  · {cwd_label} repo: {divergence.target_repo}\n"
        f"  · {cwd_label} conf: {divergence.target_conf_path}\n"
        f"{details}\n"
        f"  {resolution}"
    )
