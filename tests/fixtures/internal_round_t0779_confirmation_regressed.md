## 리뷰 (code-reviewer · 2026-08-21)

확인 전용 라운드 3에서 fix 라운드 2의 두 대상만 재검증했다. F-001 잔여는 해소됐다. 반면
F-007의 요청된 세 변형은 해소됐지만, 개발자가 추가한 `JoinedStr` 서브트리 전체 제외가 실제
conf 줄을 중첩 f-string 표현식으로 조립하는 새 미탐 통로를 열어 F-007은 퇴행이다.

- **F-001 — 해소.** 06 라운드와 같은 `pay` 형상(무prefix init → 다른 repo의 `ops` 행 →
  이 repo에 `pay` prefix, 세션 미바인딩) 실측은 init rc=0,
  `registered_repos()=['other','pay']`, `registered_prefixes()=['ops','pay']`,
  `id_prefix(None)=None`, `_next_id(None)='T-0001'`이었다. 완료 안내에는
  `T-NNNN (none 카테고리)`가 없고
  ``board.py new --prefix <PFX> --user-ack <PFX>``가 나온다. 직후 무명시 `cmd_new`는
  rc=1, 안내한 명시 `--prefix pay --user-ack pay`는 rc=0으로 `T-pay-001`을 실제 생성했다.
  `board.py:11323-11334`의 `registered_prefixes() >= 2` 판정과
  `board.py:11709-11720`의 `cmd_new` 가드가 실측상 lockstep이다.
- **F-001 역방향 — 정상.** 단일-repo·무prefix는 안내
  ``board.py new` 로 T-NNNN (none 카테고리) 발행` 뒤 무명시 new rc=0,
  `T-0001` 생성이었다. 단일-prefix `acct`는 안내
  ``board.py new` 로 T-acct-NNN 발행` 뒤 명시 `--prefix acct --user-ack acct` new rc=0,
  `T-acct-001` 생성이었다.
- **F-007 요청 세 변형 — 개별 해소.** 직접 probe 결과는
  `conf_identity_reads('value = local_config()["session"]') ==
  [(1, 'conf dict["session"] 인덱싱')]`, annotated 두 문장 조회는
  `[(2, 'conf dict 에서 "session" 조회')]`,
  `conf_identity_writes('LOCAL_CONF.write_text("prefix=pay")') ==
  [(1, "conf 줄 리터럴 'prefix=pay'")]`였다. 세 소스 모두
  `_RETIRED_READ_SAMPLES`/`_RETIRED_WRITE_SAMPLES`에 실재한다
  (`tests/test_local_conf_identity_keys_retired.py:264-285`).
- **F-007 `JoinedStr` 판단 — 퇴행.** 단순
  `LOCAL_CONF.write_text(f"prefix={prefix}")`는
  `[(1, "conf 줄 리터럴 'prefix=\\x00'")]`로 잡지만, 실행 결과가 같은 실제 conf write인
  `LOCAL_CONF.write_text(f"{'prefix='}{prefix}")`와
  `LOCAL_CONF.write_text(f"{'session='}{session}\\n")`는 둘 다 `[]`였다.
  `tests/test_local_conf_identity_keys_retired.py:170-180`이 `JoinedStr`의 모든 descendant를
  제외하고, `:150-157`이 각 `FormattedValue`를 무조건 placeholder 하나로 뭉개므로 그 안의
  정적 `prefix=`/`session=` 조각이 부모 결합 텍스트에서도 사라진다. 즉 오탐 제거는 됐지만
  실제 write를 f-string 표현식 안에 숨기는 미탐 통로가 생겼다.
- **F-007 오탐 역방향 — 해소 확인.** 세 template의 `board.py:11206`은 현재 모두 hit 0이었다.
  각 template 전체 write scan은 기존 stale 네 줄(11056, 11058, 11099, 11101)만 잡아 개발자가
  보고한 새 11206 오탐은 실측대로 사라졌다. 다만 위 양성 미탐 때문에 이 처방 전체를 수용할 수는
  없다.
- **지정 회귀.** pyc의 원 경로 메타데이터까지 배제하도록 `__pycache__` 없이 만든 writable 복사본
  `/tmp/pm_delegate_read_3644691_46dacc27bddb44cfa28990419bf73341/regression-clean`에서 실행해
  **305 passed / 2 failed**였다. 두 실패는
  `test_no_engine_source_reads_the_local_conf_identity_keys`와
  `test_no_engine_source_writes_the_local_conf_identity_keys`이며 offender는 모두 PM 소유 stale
  `templates/{claude_code,codex,opencode}` 사본뿐이다. canonical offender는 0이므로 지시된 면책
  항목이고 별도 반려 사유가 아니다.

```sh
cd /tmp/pm_delegate_read_3644691_46dacc27bddb44cfa28990419bf73341/regression-clean
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider \
  --basetemp "$TMPDIR/pytest-clean2" \
  tests/test_board_multipm.py tests/test_local_conf_identity_keys_retired.py \
  tests/test_board_identity.py tests/test_board_per_repo.py
```

신규 발견(NEW)은 없다. 위 f-string 미탐은 요청 항목 3의 F-007 판단 검증에서 확인된 같은 finding의
퇴행으로 분류했다. staged 타 티켓 diff와 template 파리티는 지시대로 finding으로 세지 않았다.

## must-fix
- F-007 — `JoinedStr` descendant 전체 제외가 실제 conf write
  (`f"{'prefix='}{prefix}"`, `f"{'session='}{session}\\n"`)를 놓친다. 자식 노드를 독립 hit로
  세지 않되, 부모 f-string을 문맥 보존 방식으로 평탄화하여 formatted expression 안의 정적 문자열
  조각과 동적 placeholder를 함께 반영하라. 위 두 양성 표본과 기존 `templates/*/board.py:11206`
  음성 표본을 자기검증에 같이 추가해 미탐·오탐 양쪽을 고정해야 한다.

## 판정
판정: 반려 · finding 1건(must-fix 1건)

```pm-review-v1
{"version":2,"findings":[],"confirmations":[{"id":"F-007","status":"regressed","evidence":"요청된 직접 subscript·AnnAssign 조회·종결 개행 없는 literal write는 각각 비어 있지 않은 hit를 반환하고 자기검증 표본에도 실재한다. 그러나 이번 JoinedStr 제외가 동일 실제 conf 내용을 중첩 formatted expression으로 만든 두 write를 []로 놓쳐 새 false-negative 통로를 만들었다."},{"id":"F-001","status":"resolved","evidence":"06 형상에서 안내는 T-NNNN을 제거하고 명시 --prefix 커맨드를 냈다. 무명시 new rc=1, 안내한 --prefix pay new rc=0/T-pay-001이었으며, 정상 무prefix와 단일-prefix도 각각 T-0001과 T-acct-001을 실제 발행했다."},{"id":"F-002","status":"resolved","evidence":"직전 확인 라운드의 resolved 처분 유지; 이번 확인 범위 밖이며 퇴행 증거 없음."},{"id":"F-003","status":"resolved","evidence":"직전 확인 라운드의 resolved 처분 유지; 이번 확인 범위 밖이며 퇴행 증거 없음."},{"id":"F-004","status":"resolved","evidence":"직전 확인 라운드의 resolved 처분 유지. 이번 F-007 JoinedStr 퇴행은 별도 확인 ID F-007에 귀속했다."}]}
```
