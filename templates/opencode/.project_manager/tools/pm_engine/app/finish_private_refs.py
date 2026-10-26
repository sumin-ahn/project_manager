"""완료 기록 사설 참조 측정 — 출하 표면·추가 줄·통합 도달 여부 분류 (층: app)."""
from __future__ import annotations

import sys
import tokenize
from collections.abc import Sequence
from pathlib import Path

from pm_engine.domain import finish_rules, private_refs
from pm_engine.infra import finish_git, fs, private_surface, review_diff


# ── 사설 참조 완료 기록 preflight ────────────────────────────────────────
# 판정식은 domain.private_refs, 표면 열거는 infra.private_surface 소유다. 측정 폭은 리뷰 측정과 같은
# 앵커 seam(`TicketFinisher._measurement_anchor`)을 재사용한다 — 리뷰가 본 표면과 이 게이트가 보는
# 표면이 갈리지 않는다. rc 정책의 축은 **그 줄이 통합 브랜치에 이미 있는가**다: 통합
# 브랜치에 없는 줄(도입 커밋이 없거나, 커밋은 됐지만 그 커밋이 통합 브랜치의 조상이
# 아니다)의 참조는 이 작업이 새로 들인 유입이라 넓은 recall 의 raw 축 전부를 차단하고,
# 안전 삭제 단위가 잡히는 actionable 은 그와 무관하게 차단한다. 이미 통합 브랜치에
# 도달한 줄의 raw 는 흡수분·허용분 재작성이 실측상 걸리는 갈래라 blame sha 를 실은
# 경고로 남긴다.
#
# 이 기준은 **커밋·재배치 순서에 흔들리지 않는다** — 같은 변경이 작업트리에만 있든, 이미
# 커밋됐든, 재배치 전이든 후든 판정이 같다. 순서를 운영 규칙으로 지키게 하던 자리를 판정
# 기준 자체가 대체한다. 통합 브랜치를 해소하지 못하면(선언 부재·이 트리에 없음) 이 판정은
# 다른 기준으로 접지 않고 멈춘다.


def _private_ref_surface_paths(
    engine_home, code_tree: Path, touches: Sequence[str],
) -> list[Path]:
    """터치 선언 ∩ 사설 참조 출하 python 표면 ∖ 기계 mirror — 절대경로 목록(표면 해소).

    표면 정의는 `private_surface.shipping_paths` 가, 측정 제외는
    `review_diff.is_machine_mirror_path` 가 소유한다(정의 사본 0 · markdown 절반은
    이 게이트 밖 — 비목표). 기계 mirror 를 빼는 이유는 `templates/<타깃>/` 아래 엔진
    사본이 canonical 을 그대로 복사한 결과이기 때문이다 — 빼지 않으면 같은 유입 한 건이
    canonical 1 + 사본 3 으로 네 번 지목되고, 사람이 고칠 자리가 어디인지 흐려진다."""
    python_paths, _markdown_paths = private_surface.shipping_paths(code_tree)
    surface: list[Path] = []
    for path in python_paths:
        relative = path.relative_to(code_tree).as_posix()
        if not finish_rules.scope_covers(touches, relative):
            continue
        if review_diff.is_machine_mirror_path(engine_home, relative):
            continue
        surface.append(path)
    return surface


def _private_ref_diff_offenders(
    surface_paths: Sequence[Path], code_tree: Path, diff_text: str,
) -> tuple[list[tuple[str, int, str]], list[tuple[str, int, str]]]:
    """표면 파일 중 `diff_text` 가 새로 추가한 줄에서 (actionable, raw) offender 를 낸다.

    `(경로, 줄, 토큰)` 3-튜플 목록 두 개 — raw ⊇ actionable(같은 프로세 스팬·같은 정규식을
    재사용한다). 판정은 diff 조각이 아니라 **작업트리 파일 전문**에 건다(`prose_token_spans`
    는 완전한 파이썬 소스가 필요하다) — diff 는 추가 줄 **번호**를 뽑는 데만 쓴다.

    판정하지 못한 표면(비-UTF-8 · 구문 오류 · 경로 미확정 diff block)은 차단하지 않되
    **조용히 넘기지도 않는다** — 확정 사실에만 hard 차단을 걸되, 못 본 자리는 값으로 알린다."""
    added_by_path, unresolved = finish_rules._parsed_diff_added_lines(diff_text)
    for header in unresolved:
        print(
            f"  ⚠ 사설 참조 판정 불가(차단 아님) — diff 경로 미확정: {header}",
            file=sys.stderr,
        )
    actionable: list[tuple[str, int, str]] = []
    raw: list[tuple[str, int, str]] = []
    for path in surface_paths:
        relative = path.relative_to(code_tree).as_posix()
        added_lines = added_by_path.get(relative)
        if not added_lines:
            continue
        try:
            source = fs.read_text_shared(path, encoding="utf-8")
            spans = private_refs.prose_token_spans(source)
        # `TokenError` 는 3.12 미만 토크나이저가 미완성 문자열·괄호에 내는 형식이다
        # (3.12+ 는 같은 입력을 `SyntaxError` 로 낸다) — 지원 하한을 함께 덮는다.
        except (
            OSError, UnicodeError, SyntaxError, tokenize.TokenError,
            private_refs.DataLiteralMarkerError,
        ) as exc:
            print(
                f"  ⚠ 사설 참조 판정 불가(차단 아님) — {relative}: "
                f"{type(exc).__name__}",
                file=sys.stderr,
            )
            continue
        for span in spans:
            # raw 축은 span 원문맥에 건다 — `_specific_matches` 의 인라인 코드 보호(backtick
            # 쌍)는 여러 줄에 걸칠 수 있어 잘린 줄만 보면 보호가 풀린다. 이 축의 match
            # offset 은 span 기준이라 그대로 줄 번호로 환산된다.
            for match in private_refs._specific_matches(span.text):
                line = span.line + span.text[:match.start()].count("\n")
                if line in added_lines:
                    raw.append((relative, line, match.group()))
            # actionable 축은 **물리 줄 단위**로 건다 — `_actionable_matches` 는 줄마다 안전
            # 삭제 계획을 세우고 그 **줄 기준** offset 의 match 를 돌려준다. span 전체 offset
            # 으로 읽으면 2행 이후에 들어온 유입이 앞줄로 붙어 추가 줄 집합과 어긋나고,
            # 차단돼야 할 유입이 경고로 강등된다(여러 줄 docstring 실측).
            offset = 0
            for chunk in span.text.splitlines(keepends=True):
                line = span.line + span.text.count("\n", 0, offset)
                if line in added_lines:
                    for match in private_refs._actionable_matches(chunk):
                        actionable.append((relative, line, match.group()))
                offset += len(chunk)
    actionable.sort()
    raw.sort()
    return actionable, raw


def _line_reached_integration(
    code_tree: Path, sha: str, integration_tip: str, *, run_git_stdout_at,
) -> bool:
    """그 줄의 도입 커밋이 통합 브랜치에 이미 있는가 — 판정 불능은 False(신규 취급).

    `git merge-base --is-ancestor` 는 조상이면 rc 0, 아니면 rc 1 이고 그 밖의 rc 는 판정
    자체가 안 된 것이다. 도달했다는 증거가 없는 줄을 흡수분으로 접으면 이 게이트가 막으려는
    신규 유입이 경고로 강등돼 나간다 — 반대 방향의 오분류는 사람이 그 자리를 한 번 보는
    비용에서 끝난다."""
    try:
        rc, _out = run_git_stdout_at(
            code_tree, ["merge-base", "--is-ancestor", sha, integration_tip],
        )
    except Exception as exc:  # noqa: BLE001 — 판정 실패는 신규 취급(차단 방향).
        return False
    return rc == 0


def _private_ref_raw_by_novelty(
    code_tree: Path, raw_only: Sequence[tuple[str, int, str]],
    integration_tip: str, *, run_git_stdout_at,
) -> tuple[list[tuple[str, int, str, str]], list[tuple[str, int, str, str]]]:
    """raw-only offender 를 (신규 유입 + 사유 표기, 흡수분 + 그 줄의 blame sha)로 가른다.

    기준은 **그 줄이 통합 브랜치에 도달했는가** 다: 도입 커밋이 없는 줄(all-zero sha·조회
    불가)과 도입 커밋이 통합 브랜치의 조상이 아닌 줄은 이 작업이 새로 들인 유입이고, 조상인
    줄은 통합 브랜치가 이미 가진 흡수분·허용분이다. 재유입 가드의 대장(allowlist·ratchet)은
    여기서 읽지 않는다 — 채택자 트리에 그 파일이 없어 판정이 영구 강등되고, 대장 재생성이
    곧 게이트 통과가 된다.

    통합 브랜치는 항상 값이다(`_integration_tip` 이 해소하지 못하면 이 판정에 오지 않고
    멈춘다) — 도입 커밋 유무만 보는 기준으로 접는 갈래가 없다."""
    novel: list[tuple[str, int, str, str]] = []
    absorbed: list[tuple[str, int, str, str]] = []
    for path, line, token in raw_only:
        sha = finish_git._git_blame_short_sha(
            run_git_stdout_at, code_tree, path, line,
        )
        if sha is None:
            novel.append((path, line, token, finish_rules._PRIVATE_REF_UNCOMMITTED_NOTE))
        elif _line_reached_integration(code_tree, sha, integration_tip,
                                       run_git_stdout_at=run_git_stdout_at):
            absorbed.append((path, line, token, sha))
        else:
            novel.append(
                (path, line, token, finish_rules._PRIVATE_REF_UNMERGED_NOTE.format(sha=sha)))
    return novel, absorbed
