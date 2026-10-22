"""클러스터 식별자 및 장부 규칙 (층: domain)."""
from __future__ import annotations

from typing import Any
from collections.abc import Sequence
import re
from pm_engine.domain import ticket_rules


CLUSTER_ID_PREFIX = 'C-'

CLUSTER_BRANCH_PREFIX = 'task/'

CLUSTER_STATUS_OPEN = 'open'

CLUSTER_STATUSES: tuple[str, ...] = ('open', 'review', 'fix', 'closing', 'closed')

CLUSTER_BUDGET_DEFAULT: dict[str, int] = {'architect': 1, 'developer_per_ticket': 1, 'code-reviewer': 1, 'fix': 1}

CLUSTER_LEDGER_KEYS: tuple[str, ...] = ('id', 'tickets', 'base_branch', 'branch', 'spike', 'budget', 'status')

_CLUSTER_NAME_RE = re.compile('\\A[A-Za-z0-9][A-Za-z0-9._-]*\\Z')

_CLUSTER_NAME_MAX = 40

_CLUSTER_NAME_DOUBLE_DOT = '..'

_CLUSTER_NAME_LOCK_SUFFIX = '.lock'

_CLUSTER_NAME_DOT_SUFFIX = '.'

_CLUSTER_MEMBER_REJECT_STATUSES: tuple[str, ...] = ('done', 'discarded')

_CLUSTER_INTERNAL_OVERLAP_PATH_LIMIT = 3

def cluster_id_for_name(name: str) -> str:
    """사람이 준 이름 → 장부 id. 접두를 이미 쓴 입력은 그대로 둔다(입력 표기 둘 다 수용)."""
    text = str(name or '').strip()
    return text if text.startswith(CLUSTER_ID_PREFIX) else f'{CLUSTER_ID_PREFIX}{text}'

def cluster_name_of(cluster: str) -> str:
    """장부 id → 이름(접두 제거) — 브랜치 이름과 진단 문구가 쓰는 사람 표기."""
    text = str(cluster or '').strip()
    return text[len(CLUSTER_ID_PREFIX):] if text.startswith(CLUSTER_ID_PREFIX) else text

def cluster_branch_name(cluster: str) -> str:
    """통합 브랜치 이름 — `task/<ticket-group>`(장부 id 접두는 브랜치에 실리지 않는다)."""
    return f'{CLUSTER_BRANCH_PREFIX}{cluster_name_of(cluster)}'

def is_auto_cluster_id(cluster: str) -> str | None:
    """자동 생성 크기 1 장부인가 — 맞으면 그 티켓 ID, 아니면 None.

    이름 자리가 티켓 ID 인 장부는 발행이 만든 크기 1 묶음이다. 이 모양은 **자동 귀속의 자석이
    아니다** — 그렇지 않으면 먼저 발행된 티켓의 자동 장부가 뒤따르는 발행을 전부 빨아들여
    선언하지 않은 묶음이 생긴다. 사람이 선언하는 이름은 이 모양을 쓸 수 없다(아래 sanity).
    """
    name = cluster_name_of(cluster)
    return name if ticket_rules._is_valid_ticket_id(name) else None

def _validate_cluster_name(name: str) -> str | None:
    """`cluster new <ticket-group>` 입력 sanity — 문제면 사유 문자열, 정상이면 None."""
    text = cluster_name_of(name)
    if not text:
        return '클러스터 이름이 비었다'
    if is_auto_cluster_id(text):
        return f'티켓 ID 모양의 이름은 예약이다: {text!r} — 그 모양은 발행이 만드는 크기 1 장부의 자리다(사람이 선언하는 묶음은 다른 이름을 쓴다)'
    if len(text) > _CLUSTER_NAME_MAX:
        return f'클러스터 이름이 너무 길다({len(text)}자 · 상한 {_CLUSTER_NAME_MAX})'
    if not _CLUSTER_NAME_RE.match(text):
        return f'클러스터 이름 형식 위반: {text!r} — 영숫자로 시작하고 영숫자·`.`·`_`·`-` 만 쓴다(파일명과 git 브랜치 이름에 그대로 들어간다)'
    if _CLUSTER_NAME_DOUBLE_DOT in text or text.endswith(_CLUSTER_NAME_LOCK_SUFFIX) or text.endswith(_CLUSTER_NAME_DOT_SUFFIX):
        return f'git 브랜치 이름 금칙: {text!r} — `{cluster_branch_name(text)}` 는 만들 수 없다(이중 점 · `{_CLUSTER_NAME_LOCK_SUFFIX}` 끝 · 점 끝). 장부만 남고 통합 브랜치는 영원히 생기지 않으므로 첫 쓰기 앞에서 거부한다.'
    return None

def ticket_cluster(tid: str, fm: dict[str, Any] | None=None) -> str:
    """그 티켓이 속한 클러스터 id — **필드 부재는 크기 1**(`C-` + 티켓 ID)로 읽는다.

    구세대 티켓(필드도 장부도 없음)을 마이그레이션 없이 같은 코드 경로에 태우는 규칙이다.
    """
    value = str((fm or {}).get('cluster') or '').strip()
    return value or f'{CLUSTER_ID_PREFIX}{tid}'

def _new_cluster_fm(cluster: str, tickets: Sequence[str], *, base_branch: str | None=None, branch: str | None=None, spike: str | None=None) -> dict[str, Any]:
    """새 장부 frontmatter — 고정 단계별 1회 예산을 박는다."""
    return {'id': cluster_id_for_name(cluster), 'tickets': list(tickets), 'base_branch': base_branch, 'branch': branch, 'spike': spike, 'budget': dict(CLUSTER_BUDGET_DEFAULT), 'status': CLUSTER_STATUS_OPEN}
