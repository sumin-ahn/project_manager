# pm-ticket 상황별 운영 상세

> 아래 절은 상시 카드에서 분리한 원문이다. 해당 상황에서만 읽는다.

## 3. 설계 단계 (`design: required` 티켓만)

설계 결함이 코드 리뷰 라운드로 전가되는 것을 막는 단계다. 엔진은 **frontmatter 값과 설계 절 존재만** 판정하고, 설계 내용의 옳고 그름은 PM·리뷰어가 본다.

| `design:` 값 | 의미 | promote / claim |
| --- | --- | --- |
| `required` | 설계 미완 (발행 기본값: `--estimate large`) | 차단 |
| `done` | 설계 절 완성 + 설계 검토 종료 (PM 수동 기입) | 통과 |
| `"waived: <사유>"` | 사유를 남기고 면제. YAML상 따옴표 필수(콜론 포함) | 통과 |
| `n/a` | 설계 단계 비대상 (발행 기본값: small/medium). **필드 부재도 `n/a`** | 통과 |

설계 절 4항목: **경계 실측**(인터페이스 가정을 무엇으로 실측했는지 — 실행한 명령·관측 결과) / **불변식** / **표면 상한**(입력 공간이 유한한지 — 무한 표면이면 설계 기각·범위 축소) / **테스트 전략**.

운영 규칙:

- **작성은 PM 인라인이 기본**이다. 발행 시점의 warm한 메인 세션에서 그대로 이어 쓴다(설계용 별도 세션을 띄우지 않는다 — 설계 오버헤드가 새 비용이 되면 역류한다).
- **architect 위임은 estimate=large + 신규 표면**일 때만. 기존 표면의 변경은 PM 인라인으로 끝낸다.
- **설계 검토는 리뷰어 세션이 겸임**한다(설계 전용 리뷰 세션 없음). **상한 2라운드** — 2R에 수렴하지 않으면 티켓을 분할하거나 사용자에게 에스컬레이션한다(라운드를 더 돌리지 않는다).
- 검토가 끝나면 PM이 frontmatter를 `design: done`으로 **수동 기입**한다. 면제는 `design: "waived: <사유>"`로 사유를 남긴다.
- 설계 절이 미충전이거나 필드가 `required`로 남아 있으면 `promote`·`claim`이 rc=1로 거부하고, 전역 `lint`는 `design-pending` 경고 1줄(never-block)로 보여준다.

## 규칙

- 발행·격리·검증·승격은 모두 `board.py`가 처리한다.
- `promote`는 `board.py new`와 같은 placeholder 검사(`_body_lint_issues`)를 재사용하며 목표/인터페이스/결정/DoD/참고 전부에 placeholder 0을 강제한다. 설계 절 판정(`_design_issues`)도 같은 깔때기를 탄다.
- 설계 단계 승격(`design: done`)은 사람 판정이라 엔진이 자동으로 올리지 않는다. PM이 frontmatter를 직접 고친다.
- 승격 전 draft는 로컬 `board.py show <id>`로만 조회된다.

## 참고

- `.project_manager/tools/board.py`: backbone CLI (`new`/`lint`/`promote`).
- `.project_manager/wiki/tickets/_template.md`: 7절 티켓 뼈대(목표/인터페이스/결정/설계/DoD/참고/메모) + `design:` 필드.
