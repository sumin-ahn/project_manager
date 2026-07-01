#!/usr/bin/env bash
# PostToolUse hook: 프로젝트 소스 파일이 Write/Edit 되면 회귀 테스트를 자동 실행한다.
# stdin: Claude Code hook JSON. stdout: 선택적 systemMessage JSON.
#
# 멀티-유저 안전(clone-and-go): 프로젝트 루트를 스크립트 위치에서 self-resolve 하고(절대경로
# 박제 금지 — 다른 PC 에서 재-import 불필요), 인터프리터는 python3→python 런타임 폴백으로
# OS 무관하게 고른다. 이 파일은 치환 토큰이 없어 모든 머신에서 byte-identical 하다.
# 다른 언어 프로젝트면 소스 확장자 패턴(*.py)·테스트 러너 줄만 프로젝트에 맞게 교체.
set -u

# 스크립트 위치(.claude/)에서 프로젝트 루트 self-resolve (precompact_capture_hook.sh 와 동일 패턴).
hook_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 0
repo_root=$(CDPATH= cd -- "$hook_dir/.." && pwd) || exit 0

# hook stdin 에서 편집된 파일 경로 해소.
f=$(jq -r '.tool_input.file_path // .tool_response.filePath // empty')

# 이 프로젝트 안의 소스 파일에만 반응 (확장자는 프로젝트 언어에 맞게 조정).
case "$f" in
  "$repo_root"/*.py) ;;
  *) exit 0 ;;
esac

cd "$repo_root" || exit 0

# 인터프리터 런타임 선택 (python3 → python 폴백). 없으면 조용히 통과 — hook 은 정상 작업을 막지 않는다.
if command -v python3 >/dev/null 2>&1; then py=python3
elif command -v python >/dev/null 2>&1; then py=python
else exit 0
fi

result=$("$py" -m pytest tests/ -q --no-header 2>&1 | tail -1)
jq -n --arg msg "tests: $result" '{systemMessage: $msg}'
