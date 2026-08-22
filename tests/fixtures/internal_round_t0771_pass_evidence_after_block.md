## 리뷰 (code-reviewer · 2026-08-22)

## must-fix
- 없음

## 판정
판정: 통과 · finding 0건(must-fix 0건)

```pm-review-v1
{"version":2,"findings":[],"confirmations":[]}
```

## 검증 근거

- 제거 전수성: 현재 tracked grep의 비-CHANGELOG 11줄은 삭제 대기 4파일에만 있고, 네 파일을 처음부터
  제외해 만든 임시 스냅샷에서는 `run_tests_hook` 잔존이 `CHANGELOG.md` 7줄뿐이었다. 그 7줄은 이번
  업그레이드/Removed 안내 6줄과 보존 대상 과거 릴리즈 기록 1줄이다. 출하 코드·manifest·settings·README·
  docs·skills·나머지 tests에는 이름 및 관련 산문 잔존이 0이다.
- 래퍼 산문: `ctx_statusline.sh`와 `ctx_stop_hook.sh`는 실제로 같은 `for _cand in python3 python` 순회와
  `"$_cand" --version` 실행검증 블록을 공유한다. `git diff -U0`의 내용 변경은 추가 2·삭제 2줄뿐이고
  모두 `#` 주석이며, 빈 괄호·dangling 지시어가 없다. 양 파일은 `bash -n` 통과, HEAD/index 모드 모두
  `100755`다.
- 설정/manifest 정합: 루트·claude_code template `settings.json`은 JSON 파싱에 성공하고
  `run_tests_hook` 0건이며 권한 행과 `PostToolUse` 배선이 함께 제거됐다. 남은 이벤트는 각각
  `{PreToolUse, PreCompact}`와 `{PreToolUse, UserPromptSubmit, PreCompact, PostCompact}`이고 지정 회귀가
  ctx 래퍼 발화, compaction 경로, 위임 채널 가드 및 fresh-adopter 훅 실재/실행비트를 통과했다. 양
  `engine.manifest`와 `ADAPTER_HOOK_SET`에도 제거 대상은 0건이다.
- 미러/어댑터 경계: 루트와 templates 3타깃의 `pm_import.py` 4본은 SHA-256이 모두
  `09f1c64a6c539c22ff58fe6d8f3f8a1af9dde00eb6e1d9b9e98f18ce8a8208fc`로 동일하다. HEAD/index tracked
  blob 전수 대조에서 codex 105개·opencode 132개 중 변경은 각 `pm_import.py` 1개뿐이고,
  `.codex/`·`.opencode/`·각 manifest·AGENTS.md 변경은 0이다.
- 회귀: 지정된 15파일은 `1246 passed, 7 skipped`였다. 삭제 예정 테스트 두 파일은 독립 수집 결과
  14+61=75개이고, 그 밖의 변경 테스트 9파일은 HEAD와 현재 모두 902개가 수집되어 다른 증감이 없다.
  삭제 전 shipped-path 선별 가드는 예상대로 유일한 미등재 파일 `.claude/run_tests_hook.sh`를 지목해
  1 failed였고, 네 파일을 제외한 임시 스냅샷에서는 같은 가드가 1 passed로 전환됐다.
- 위생: `git diff --check`와 원본의 unstaged diff가 모두 깨끗하다. 테스트 cacheprovider는 끄고 모든
  `basetemp` 및 삭제 후 합성 스냅샷을 지정된 격리 임시 디렉터리 아래에만 만들었다.
