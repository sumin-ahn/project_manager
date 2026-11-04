## 리뷰 (code-reviewer · 2026-08-22)

## must-fix
- F-001 — 정상 open 티켓의 canonical `claimed_by: null` 형상이 신규 lint 테스트에 없다. 값 검사(`fm.get`)를 키 존재 검사(`"claimed_by" in fm`)로 퇴행시킨 격리 변이에서도 `tests/test_board_lint.py` 전체가 216 passed여서, 명세가 요구한 정상 보드 오탐 0을 잠그지 못한다.

## 판정
판정: 반려 · finding 2건(must-fix 1건)

```pm-review-v1
{"version":2,"findings":[{"id":"F-001","class":"spec-violation","severity":"must-fix","authority":"spec.md 완료 조건의 ‘정상 보드에서 오탐 0(픽스처로 검증)’ 및 PM 지정 테스트 품질 축의 값 단언 요구","evidence":"tests/test_board_lint.py:3243의 _healthy_ticket_text는 claimed_by 키를 아예 생략하지만 test_lint_claim_identity_no_false_positive_on_healthy_shapes(:3741)는 이를 open+null이라고 설명한다. /tmp 격리 사본에서 board.py:13927의 조건을 status == open and fm.get(claimed_by)에서 status == open and claimed_by in fm으로 바꾼 뒤 허용 범위 tests/test_board_lint.py를 실행해도 216 passed였다. 실제 템플릿/정상 frontmatter의 claimed_by: null을 존재 기반 오탐으로 바꾸는 퇴행이 green이다.","recommendation":"정상 open 픽스처에 claimed_by: null과 claimed_at: null을 명시하고 lint_claim_identity()가 finding 0임을 단언하라. claimed_by 키 존재 검사로 바꾼 변이가 red가 되는지 재확인하라.","design_change":false},{"id":"F-002","class":"implementation-defect","severity":"should-fix","authority":"architect 불변식 I1 및 lint_claim_identity docstring의 claimed_by/claimed_at/claimed_rev 전부 null 계약","evidence":"board.py:13927은 status == open and fm.get(claimed_by)만 검사한다. 독립 probe에서 open+claimed_at 값만 잔존한 형상과 open+claimed_rev 값만 잔존한 형상은 모두 []였지만, 함수 docstring은 I1 전체 위반을 가시화한다고 서술한다.","recommendation":"open 상태에서 claimed_by·claimed_at·claimed_rev 중 하나라도 non-null 값이면 같은 advisory를 내도록 판정과 detail을 맞추고, 각 단독 잔존 픽스처를 추가하라. 범위를 claimed_by만으로 유지한다면 I1 전체를 검사한다는 docstring/테스트 설명을 좁혀 실제 계약과 일치시켜라.","design_change":false}],"confirmations":[]}
```
