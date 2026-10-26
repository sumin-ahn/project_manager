"""정확 티켓·귀속 창·묶음 장부 조회, 원문 복구, 이 실행의 산출물 열거 (층: infra)."""
from __future__ import annotations

from pathlib import Path

from pm_engine.domain import cluster_rules, finish_rules, round_rules, ticket_rules
from pm_engine.infra import board_git, cluster_store, fs, pm_paths, round_store, ticket_store


def count_board_done(home) -> int:
    """done 티켓 수를 반환한다 (명시 Home 의 티켓 디렉터리).

    실패 시 -1 반환.
    """
    try:
        # board 루트 추종 — board/ 분리시 ticket 이 board/tickets 로 빠진다(Home 의 board 루트).
        done_dir = pm_paths.tickets_dir(home) / "done"
        return len(list(done_dir.glob("T-*.md")))
    except Exception:
        return -1


def get_ticket_title(home, ticket_id: str) -> str:
    """ticket_id 의 title 을 명시 Home 의 티켓에서 읽어온다 (실패 시 빈 문자열).

    조회는 `_ticket_frontmatter` 한 지점을 쓴다 — 제목과 touches 가 서로 다른 파일을
    보면 마감 로그가 가리키는 티켓과 게이트가 판정한 티켓이 갈린다.
    """
    title = _ticket_frontmatter(home, ticket_id).get("title")
    return title if isinstance(title, str) and title else ""


def _ticket_frontmatter_snapshot(
    home, ticket_id: str,
) -> finish_rules.TicketFrontmatterSnapshot:
    """ticket frontmatter 와 파일 읽기 실패 진단을 함께 돌려준다.

    touches 소비처의 단일 해소 지점 — 같은 티켓을 두 번 다르게 찾지 않는다.
    조회는 공용 정확-일치 seam(`ticket_store.find_ticket_exact`)이다: `{id}-*.md` prefix glob 의
    첫 매칭을 믿으면 `T-NNNN` 과 `T-NNNN-001` 공존 시 **다른 티켓의 touches** 로 완료
    게이트가 판정한다(조용한 오판). 정확 일치가 없으면 폴백 없이 {} 다 — 판정 입력이 없는 것과
    무관 티켓으로 판정하는 것 중 전자가 안전하다(게이트는 off 로 떨어질 뿐이다)."""
    try:
        found = ticket_store.find_ticket_exact(home, ticket_id)
        if found is None:
            return finish_rules.TicketFrontmatterSnapshot({}, None)
        fm, _body = ticket_store.load_ticket(found[1])
    except Exception as exc:
        detail = " ".join(str(exc).split()) or type(exc).__name__
        return finish_rules.TicketFrontmatterSnapshot({}, f"{pm_paths.tickets_dir(home)}: {detail}")
    return finish_rules.TicketFrontmatterSnapshot(fm if isinstance(fm, dict) else {}, None)


def _ticket_frontmatter(home, ticket_id: str) -> dict:
    """기존 fail-soft 조회 표면 — 상세 실패는 diff 전용 snapshot 소비자가 보존한다."""
    return _ticket_frontmatter_snapshot(home, ticket_id).frontmatter


def get_ticket_claimed_rev(home, ticket_id: str) -> str | None:
    """ticket_id 의 frontmatter `claimed_rev` (없으면 None) — 자기 축 회귀 baseline 입력.

    `board.py claim` 이 claim 시점 코드 트리 HEAD 를 박제한 값이다. 구 티켓(박제 이전)엔
    없으므로 None 이 정상 형상이고, 그때 그 축을 접는 판정은 소비자가 소유한다."""
    claimed_rev = _ticket_frontmatter(home, ticket_id).get("claimed_rev")
    return claimed_rev.strip() or None if isinstance(claimed_rev, str) else None


def get_ticket_claimed_at(home, ticket_id: str) -> str | None:
    """ticket_id 의 frontmatter `claimed_at` (없으면 None) — 귀속 **창**의 시작점.

    `board.py claim` 이 박제하는 UTC ISO 시각이다. 같은 wave 에서 이 시각 이후 완료된 티켓은
    `done/` 으로 옮겨졌어도 아직 같은 트리에 자기 diff 를 남기고 있으므로 귀속 창 안이다.
    구 티켓(박제 이전)엔 없으므로 None 이 정상 형상이고, 그때 창을 못 정하는 판정은 소비자가
    소유한다(종전 `claimed/` 만 보는 창으로 접는다)."""
    claimed_at = _ticket_frontmatter(home, ticket_id).get("claimed_at")
    return claimed_at.strip() or None if isinstance(claimed_at, str) else None


def get_ticket_touches(home, ticket_id: str) -> list[str]:
    """ticket_id 의 frontmatter `touches`(파일/디렉토리 경로 목록)를 명시 Home 에서 읽는다.

    문자열 원소만 취한다(비-문자열 오기는 버림). ticket 부재/깨짐 →
    [](graceful·crash 0 — soft 알림은 막지 않는다). domain soft 알림 step 이 쓴다.
    """
    # --touches CLI 와 동형: 각 원소 strip·빈 값/비-문자열 drop (silent-miss 방어).
    return finish_rules._clean_touches(_ticket_frontmatter(home, ticket_id).get("touches"))


def _done_paths_in_window(home, since: str | None) -> list[Path]:
    """`done/` 중 `completed_at >= since` 인 티켓 경로 (창 밖·판정 불능은 제외).

    판독은 frontmatter **머리**의 스칼라 한 개뿐이다 — `done/` 은 수백 건이라 완료 기록마다
    전문 YAML 파싱을 돌리지 않는다. 부수 효과로 창 **밖** 구 티켓의 손상 frontmatter 가 귀속
    보정을 깨뜨리지 않는다(파싱하지 않으므로).
    시각 비교는 domain.finish_rules의 writer-형식 파서(`_parse_utc_iso`)를 사용한다.
    `since`가 형식이 아니면 창을 정할 수 없으므로 `done/`을 보지
    않는다 — 제외 근거를 줄이는 쪽(잔여 과다 판정)으로 접는 쪽이 안전하다.
    """
    if since is None:
        return []
    window_start = finish_rules._parse_utc_iso(since)
    if window_start is None:
        return []
    done_dir = pm_paths.tickets_dir(home) / "done"
    if not done_dir.is_dir():
        return []
    read_text_shared = fs.read_text_shared
    in_window: list[Path] = []
    for path in sorted(done_dir.glob("T-*.md")):
        try:
            head = finish_rules._frontmatter_head(
                read_text_shared(path, encoding="utf-8", errors="replace")
            )
        except OSError:
            continue  # 읽기 실패는 창 밖으로 접는다(제외 근거를 못 쓰니 과다 판정 유지).
        completed_at = finish_rules._parse_utc_iso(
            finish_rules._fallback_frontmatter_scalar(head, "completed_at")
        )
        if completed_at is not None and completed_at >= window_start:
            in_window.append(path)
    return in_window


def get_in_window_ticket_touches(
    home, *, since: str | None = None,
) -> finish_rules.ClaimedTouchesSnapshot:
    """귀속 창 안 티켓별 touches 와 읽기 실패 진단.

    창 = `claimed/` ∪ (`done/` 중 `completed_at >= since`)이고 `since` 는 현재 티켓의
    `claimed_at` 이다. `claimed/` 만 보면 같은 wave 에서 **먼저 완료된** 티켓이 `done/` 으로
    옮겨져 보이지 않고, 그 전용 diff 가 뒤 티켓 몫으로 전부 흡수된다(실측: v1.7.8 wave 6장 —
    마지막 티켓이 잴 때 나머지 넷은 이미 done). `since` 가 없으면 창을 정할 수 없으므로
    `claimed/` 만 보는 종전 동작으로 접는다.

    잔여 판정의 다른 멤버 경로(`_sibling_member_scopes`) 입력은 이 스냅샷뿐이다. 새 장부나 설정을 만들지 않고, 보드가 권위 있게 정한
    `tickets_dir()/{claimed,done}` 과 각 티켓 frontmatter `touches` 만 읽는다. 디렉터리와
    frontmatter status 가 모두 같은 행만 인정한다. 정상적인 빈 touches/빈 창은 `error=None`이고,
    모듈·디렉터리·티켓 읽기/파싱 실패는 error 에 담아 호출부가 loud 하게 보정 skip 을 알린다.
    """
    try:
        claimed_dir = pm_paths.tickets_dir(home) / "claimed"
        if not claimed_dir.is_dir():
            raise FileNotFoundError(f"claimed 티켓 디렉터리 부재: {claimed_dir}")
        window_paths = [("claimed", path)
                        for path in sorted(claimed_dir.glob("T-*.md"))]
        window_paths += [("done", path)
                         for path in _done_paths_in_window(home, since)]
    except Exception as exc:  # noqa: BLE001 — 보드 미로드면 타 티켓 제외 없이 과다 판정 쪽 유지.
        detail = " ".join(str(exc).split()) or type(exc).__name__
        return finish_rules.ClaimedTouchesSnapshot({}, f"{pm_paths.tickets_dir(home)}: {detail}")

    in_window: dict[str, list[str]] = {}
    errors: list[str] = []
    for status, path in window_paths:
        try:
            fm, _body = ticket_store.load_ticket(path)
        except Exception as exc:  # noqa: BLE001 — 호출부가 전체 보정을 보수적으로 skip 한다.
            detail = " ".join(str(exc).split()) or type(exc).__name__
            errors.append(f"{path}: {detail}")
            continue
        ticket_id = fm.get("id") if isinstance(fm, dict) else None
        if (not isinstance(ticket_id, str) or not ticket_id.strip()
                or fm.get("status") != status):
            errors.append(f"{path}: {status} frontmatter(id/status) 손상")
            continue
        in_window[ticket_id.strip()] = finish_rules._clean_touches(fm.get("touches"))
    return finish_rules.ClaimedTouchesSnapshot(in_window, "; ".join(errors) or None)


def _fallback_ticket_frontmatter(
    home, ticket_id: str,
) -> dict:
    """정상 frontmatter 판독이 실패했을 때 현재 티켓의 최소 입력만 복구한다.

    정상 경로의 YAML 판정을 대체하지 않는다. 명시 Home 의 board 티켓 디렉터리에서
    frontmatter `id`가 정확히 같은 후보가 **하나**일 때만 쓴다. touches는
    round_store의 raw frontmatter parser를 재사용한다. 후보가 모호하거나 읽히지
    않으면 {}라 임의 스코프를 만들지 않는다.
    """
    tickets_dir = pm_paths.tickets_dir(home)
    candidates: list[tuple[Path, str]] = []
    for status in ("open", "claimed", "blocked", "done"):
        for path in sorted((tickets_dir / status).glob(f"{ticket_id}*.md")):
            try:
                text = fs.read_text_shared(path, encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            if finish_rules._fallback_frontmatter_scalar(text, "id") == ticket_id:
                candidates.append((path, text))
    if len(candidates) != 1:
        return {}
    path, text = candidates[0]
    # round_store의 원문 frontmatter parser는 손상된 opener를 `AnchorResolutionError`
    # 로 알린다. 이 fallback은 측정 가드용 입력 복구라 그 경우에도 가드 off로 접어 완료 기록을
    # 막지 않는다. 다른 예외는 삼키지 않는다.
    parser_errors = (OSError, UnicodeError, pm_paths.AnchorResolutionError)
    try:
        touches = round_store._parse_touches_from_file(path)
    except parser_errors:
        return {}
    return {"touches": touches}


def _round_stage_candidates(tickets_dir: Path, ticket_id: str) -> list[Path]:
    """티켓 라운드 디렉터리에 **실존하는 라운드 파일만** stage 후보로 낸다 (디렉터리 자체는
    넣지 않는다 — untracked 신규 라운드도 실 파일 목록이라야 `git_scope_stage_pathspec` 이
    add 한다).

    라운드 판정은 문법 단일 소유자 `parse_round_filename` 한 곳에 위임한다 — 접미/접두를
    이 함수가 다시 조합하면(`.md` 접미 + 점-접두 제외 등) 라운드가 아닌 `.md`(예: `notes.md`·
    `01-dev.md`)까지 후보에 실린다(reviewer 실측 — 9개 이름 배치 중 진짜 라운드 2건만
    통과해야 하는데 4건이 통과). 점-접두 임시 잔여(원자 교체 중간 산출)도 이 문법을 벗어나
    자연히 걸러지므로 별도 조건이 필요 없다.
    """
    directory = round_store.rounds_dir_for_ticket(ticket_id, tickets_dir)
    if not directory.is_dir():
        return []
    return sorted(
        entry for entry in directory.iterdir()
        if entry.is_file()
        and round_rules.parse_round_filename(entry.name) is not None
    )


def engine_written_paths(home, ticket_id: str, log_file: Path) -> list[Path]:
    """**이 실행이 실제로 쓴** 산출물 경로.

      - `log/current.md` — 선작성 entry가 있으면 append하지 않더라도 2단계의 stage 스코프에
        항상 포함한다.
      - 티켓 파일의 옛/새 경로 · 라운드 사이드카(`tickets/rounds/<id>/*.md`) — 3단계
        `board.py complete` 가 티켓을 `claimed/`→`done/` 로 옮기고, 라운드는
        `round_store.rounds_dir_for_ticket` 가 정하는 고정 위치라 상태 이동을 따라가지
        않지만 그 실행이 새로 쓴 파일이라 함께 stage 대상이다. **board-git 분리 형상에선
        티켓과 함께 라운드도 제외** 한다: 티켓이 서브모듈(`.project_manager/board/`) 안이라
        상위 repo 의 `git add` 가 `fatal: … is in submodule`(rc=128)로 죽고, 그 이동은
        board-git 이 자기 커밋으로 이미 기록한다(라운드도 같은 서브모듈 트리 안).
        legacy(board 미분리·**출하 템플릿 기본 형상**)에선 그 이동이 홈 git 에 떨어지므로
        반드시 실어야 한다 — 안 그러면 채택자가 매 finish 마다 손으로 `git add` 해야 한다
        (reviewer 실측).
      - 묶음 장부(`tickets/clusters/C-*.md`) — 종결이 소속 장부에 `closing`·`closed` 를 쓰고, 묶음
        선언이 흡수한 크기 1 장부는 지워진 채 남는다. 빠지면 종결 뒤 홈에 미커밋 장부가 남아 첫
        핸드오프가 멈춘다(legacy 만 · 분리 형상은 board-git 이 커밋한다).

    옛 경로는 상태 디렉토리를 몰라도 된다 — 후보(모든 STATUS_DIRS/같은 파일명)를 넣어두면
    실존/추적되지 않는 후보는 `git_scope_stageable` 이 거른다(추적 중인 *삭제* 경로만 남아
    이동이 커밋으로 완성된다). 라운드는 디렉터리 통째가 아니라 존재하는 파일만 넣는다(위
    `_round_stage_candidates`). 티켓 부재면 log만 포함한다.
    """
    paths: list[Path] = [Path(log_file)]
    if board_git._board_git_enabled(home):
        return paths
    try:
        _status, ticket_path = ticket_store.find_ticket(home, ticket_id)
    except FileNotFoundError:
        return paths        # 티켓 부재 — 이 실행이 옮긴 티켓이 없다(log 만).
    paths.append(ticket_path)
    tickets_dir = pm_paths.tickets_dir(home)
    paths.extend(tickets_dir / status / ticket_path.name
                 for status in ticket_rules.STATUS_DIRS)
    paths.extend(_round_stage_candidates(tickets_dir, ticket_id))
    # 묶음 장부 — 종결이 status 를 쓰는 소속 장부와, 발행이 만들고 묶음 선언이 흡수(삭제)한 크기 1
    # 장부. 실존·추적 판정은 `git_scope_stageable` 이 한다(흡수 장부는 추적 중일 때만 삭제로 남는다).
    frontmatter, _body = ticket_store.load_ticket(ticket_path)
    paths.append(cluster_store.cluster_ledger_path(home, cluster_rules.ticket_cluster(ticket_id, frontmatter)))
    paths.append(cluster_store.cluster_ledger_path(home, ticket_id))
    return paths


def ticket_cluster_ledger(home, ticket_id: str) -> tuple[str | None, dict | None]:
    """티켓과 명시 Home의 장부에서 묶음 ID와 frontmatter를 읽는다.

    ticket_cluster 규칙은 cluster_rules, 장부 IO는 cluster_store를 사용한다.
    조회 실패는 기존 무판정 (None, None)을 유지하며 장부 부재는 (묶음 ID, None)이다."""
    try:
        found = ticket_store.find_ticket_exact(home, ticket_id)
        frontmatter: dict = {}
        if found is not None:
            loaded, _body = ticket_store.load_ticket(found[1])
            frontmatter = loaded if isinstance(loaded, dict) else {}
        cluster = cluster_rules.ticket_cluster(ticket_id, frontmatter)
        return cluster, cluster_store.load_cluster(home, cluster)
    except Exception as exc:  # noqa: BLE001 — 조회 실패는 판정 불능(차단 아님).
        return None, None


def _cluster_integration_branch(home, ticket_id: str) -> str | None:
    """이 티켓이 속한 묶음의 **통합 브랜치** 이름 (선언 부재·조회 실패는 None).

    값의 소유자는 cluster_store의 묶음 장부(`base_branch`)다 — 이 도구는 그 선언을 읽기만 한다.
    값은 장부를 만들 때 박히므로(발행이 만드는 크기 1 장부 포함) 선언 부재는 판정 입력이
    없다는 사실이고, 이 판독을 쓰는 두 소비자(완료 기록 판정·리뷰 호출 폭)는 그 사실에
    **멈춘다** — 다른 기준으로 접는 갈래가 없다.
    """
    _cluster, ledger = ticket_cluster_ledger(home, ticket_id)
    # 기준 이름만 있는 옛 singleton은 제품 결속의 증거가 아니다.
    if not str((ledger or {}).get("branch") or "").strip():
        return None
    declared = (ledger or {}).get("base_branch")
    if not isinstance(declared, str):
        return None
    return declared.strip() or None


def _cluster_member_ids(home, ticket_id: str) -> tuple[str, ...]:
    """이 티켓이 속한 묶음의 멤버 티켓 ID (장부 부재·조회 실패는 빈 튜플).

    장부 해소는 `ticket_cluster_ledger` 한 벌이고 목록을 읽는 술어는 cluster_store 소유
    (`cluster_store.cluster_tickets`)다 — 이 도구는 그 둘을 잇기만 한다(멤버 규칙 사본 0).
    """
    _cluster, ledger = ticket_cluster_ledger(home, ticket_id)
    if not ledger:
        return ()
    try:
        members = cluster_store.cluster_tickets(ledger)
    except Exception:  # noqa: BLE001 — 목록 조회 실패는 제외 없이(인구 유지) 접는다.
        return ()
    return tuple(str(member).strip() for member in members if str(member).strip())
