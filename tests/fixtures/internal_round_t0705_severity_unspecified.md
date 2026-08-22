## 리뷰 (code-reviewer · 2026-08-18)

검토 범위: 격리 스냅샷 `dev-T0705`(commit e4fce07)의 `git diff HEAD~1` — canonical `.project_manager/tools/pm_delegate.py`(`_portable_exclusive_write` 롤백 unlink 경계 6줄) + `tests/test_pm_delegate.py`(신규 3건 · `_FailingWriteHandle`) + templates 3본 전파. 회귀는 지정 범위만 실행(전체 회귀는 PM 단계).

## 실측
- `tests/test_pm_delegate.py` 전체: 704 passed · 2 skipped (신규 3건 포함).
- `tests/test_engine_rev_failsoft_guard.py`: 60 passed — fail-soft 경계 ratchet(222) 불변. 새 흡수 0(핸들러 신설 없음·무음 `except OSError:` 7 → 6).
- 예외 전파 직접 probe: 롤백 unlink 실패 시 전파 객체가 주입한 쓰기 예외 **그 객체**(`is` 동일)이고 `__context__`·`__cause__` 모두 None. 원래 예외를 정리 실패로 바꿔치기하지 않는다.
- 경고 문구 실측: `경고: opencode 쓰기 실패 롤백 삭제 실패 — 잔존 가능 경로: <path> · 오류: <원인>` — 잔여 경로·원인 모두 포함. 기존 `_warn_transport_cleanup_failure` 재사용이라 접두·형식이 형제 경고 3종과 동일.
- 충돌 경로 불변: `os.open` O_EXCL 실패는 `created=False` 라 롤백·경고를 타지 않는다(FileExistsError 회귀 green).
- 민감도 1: 롤백 핸들러를 `except OSError: pass` 로 되돌림 → (b) 1건 red → 복원 → 4건 green.
- 민감도 2: `raise unlink_exc` 로 예외 바꿔치기 → (b) 1건 red → 복원 → 4건 green.
- 민감도 3: `raise write_exc from unlink_exc`(chaining 오염) → 4건 모두 green(F-001 근거). 복원 후 sha256 일치·`git status` clean 확인.
- 전파본: `templates/{claude_code,codex,opencode}/.project_manager/tools/pm_delegate.py` 3본 모두 canonical 과 byte 동일(drift 0).
- `except OSError:` 전수 판정 독립 대조: 변경 전 무음 7곳(`:1238`·`:5131`·`:5221`·`:7525`·롤백 지점·`:8313`·`:9727`) 중 사유 있음 5, 사유 없음 2(`_posix_mode_supported` probe unlink·`_prompt_file_denylist_pattern` 후보 resolve) — developer 판정표와 일치하며 [[T-0734]] 대상 2곳과 같다. tuple 형(`(OSError, ...)`) 흡수 8곳도 AST 로 훑었으나 전부 loud 하거나 사유 주석이 있어 같은 부류의 누락은 없다.

## must-fix
- 없음

## should-fix
- F-001 — 테스트 (b)가 예외 chaining 축을 고정하지 않는다(민감도 3에서 오염 주입이 green 통과). 구현은 정상이므로 가드 1~2줄 보강 문제다.
- F-002 — 롤백 unlink 실패 시 잔여 prompt 는 남는데 자기-은닉 `.gitignore` 는 제거돼 민감 사본이 untracked 로 노출된다. 이번 diff 가 만든 결함이 아니고 T-0705 §인터페이스 밖이라 후속 티켓 분리 권고.

## suggestion
- 경고 문구 `쓰기 실패 롤백 삭제 실패` 는 '실패'가 두 번 겹친다. `action="부분 쓰기 롤백 삭제"` 정도면 형제 문구(`합성 프롬프트 삭제`·`자기-은닉 ignore 삭제`·`전달 디렉터리 정리`)와 결이 맞는다.
- `_warn_transport_cleanup_failure` 의 `opencode` 접두는 현 호출자 2곳이 모두 opencode transport 라 정확하다. `_portable_exclusive_write` 는 범용 이름이므로 비-opencode 호출자가 생기면 접두가 오귀속된다(현 시점 문제 아님).
- 테스트가 `monkeypatch.setattr(pd.os, "fdopen", ...)` 로 프로세스 전역 `os.fdopen` 을 패치한다. capsys 라 현재는 무해하지만, 대상 호출 구간으로 좁히거나 사유를 주석에 남기면 capfd 계열 전환 때 상호작용을 막는다.
- templates 3본은 ticket `touches` 밖 변경이다. canonical 과 byte 동일이라 실질 위험은 없고 DoD 의 전파 항목과도 부합하지만, PM 전파 단계와 중복 수행이라는 점만 기록한다.

## 판정
판정: 통과

```pm-review-v1
{"version":1,"findings":[{"id":"F-001","class":"implementation-defect","authority":"[[T-0705]] §인터페이스 — \"원래 예외를 삼키거나 정리 실패로 바꿔치기하지 않는다(예외 chaining 유지)\"","evidence":"민감도 실측: `_portable_exclusive_write` 를 `except BaseException as write_exc:` + `raise write_exc from unlink_exc` 로 임시 변경(원 객체는 같지만 `__cause__` 가 롤백 예외로 오염)해도 `tests/test_pm_delegate.py -k portable_exclusive_write` 4건 전부 green. 테스트 (b)(tests/test_pm_delegate.py:551)는 `pytest.raises(OSError, match=\"의도한 쓰기 실패\")` 로 메시지만 고정해 chaining 축을 못 본다. 실 구현 자체는 정상(직접 probe: 전파 객체 identity 동일·`__context__`/`__cause__` 모두 None).","recommendation":"테스트 (b)에서 주입 예외를 sentinel 로 잡아 `excinfo.value is sentinel` 과 `excinfo.value.__cause__ is None and excinfo.value.__context__ is None` 을 함께 단언한다(가드 시야를 인터페이스 표면과 일치시킴).","design_change":false},{"id":"F-002","class":"implementation-defect","authority":"`.project_manager/tools/pm_delegate.py:7535` `_cleanup_attempt_transport` docstring — \"prompt 삭제가 실패하면 자기-은닉 ignore를 보존해 민감 사본이 untracked로 노출되지 않게 한다\" · [[T-0689]] §결정(정리 실패는 loud·주 결과 보존)","evidence":"실측 probe(prompt.md 쓰기 실패 + 롤백 unlink 거부): 잔여 `…/delegate/pm_delegate_<pid>_<uuid>/prompt.md` 는 남고 같은 디렉터리의 자기-은닉 `.gitignore` 는 제거된다. `_portable_exclusive_write` 실패 시 `transport.prompt_created` 가 False 로 남아 `_cleanup_attempt_transport` 가 prompt 잔여를 모른 채 ignore 만 지우기 때문(pm_delegate.py:7547·7554). 이번 변경 이전에도 같았고(무음이라 안 보였을 뿐) 이 diff 가 만든 결함은 아니다 — 경고 덕에 관측 가능해졌다.","recommendation":"T-0705 범위 밖이므로 후속 티켓으로 분리한다. 잔여 사실을 호출자에 되돌려(예: 롤백 실패 시 `prompt_created` 상당의 잔여 표식을 세워) cleanup 이 자기-은닉 ignore 를 보존하도록 한다. 버전 귀속은 사용자 결정.","design_change":false}],"confirmations":[]}
```
