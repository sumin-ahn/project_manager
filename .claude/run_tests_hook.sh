#!/usr/bin/env bash
# PostToolUse hook: 프로젝트 소스 파일이 Write/Edit 되면 회귀 테스트를 자동 실행한다.
# stdin: Claude Code hook JSON. stdout: 선택적 systemMessage JSON.
#
# 도입 시 교체할 것:
#   {{PROJECT_ROOT}} — 프로젝트 루트 절대경로
#   소스 확장자 패턴 (*.py) — 프로젝트 언어에 맞게
#   테스트 명령 — pytest 가정. 다른 언어면 해당 러너로 교체.
set -u

# hook stdin 에서 편집된 파일 경로 해소
f=$(jq -r '.tool_input.file_path // .tool_response.filePath // empty')

# 이 프로젝트 안의 소스 파일에만 반응 (확장자는 프로젝트 언어에 맞게 조정)
case "$f" in
  {{PROJECT_ROOT}}/*.py) ;;
  *) exit 0 ;;
esac

cd {{PROJECT_ROOT}} || exit 0

# 테스트 러너가 없으면 조용히 통과 — hook 은 정상 작업을 절대 막지 않는다.
command -v {{PY}} >/dev/null 2>&1 || exit 0

result=$({{TEST_CMD}} --no-header 2>&1 | tail -1)
jq -n --arg msg "tests: $result" '{systemMessage: $msg}'
