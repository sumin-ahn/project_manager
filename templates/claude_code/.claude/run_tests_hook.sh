#!/usr/bin/env bash
# PostToolUse hook: 프로젝트 소스 파일이 Write/Edit 되면 회귀 테스트를 자동 실행한다.
# stdin: Claude Code hook JSON. stdout: 선택적 systemMessage JSON.
#
# 멀티-유저 안전(clone-and-go): 프로젝트 루트를 스크립트 위치에서 self-resolve 하고(절대경로
# 박제 금지 — 다른 PC 에서 재-import 불필요), 인터프리터는 python3→python 런타임 폴백으로
# OS 무관하게 고른다. 이 파일은 치환 토큰이 없어 모든 머신에서 byte-identical 하다.
# 다른 언어 프로젝트면 소스 확장자 패턴(*.py)·테스트 러너 줄만 프로젝트에 맞게 교체.
#
# stdin JSON 파싱·systemMessage 출력은 외부 JSON 프로세서(과거 의존) 없이 python 으로 한다 —
# 그 프로세서는 Windows 에 기본 부재라(command not found → 파일 경로 빈값 → case 불일치 → rc0
# silent no-op, 회귀 자동실행이 조용히 죽었다). 훅은 어차피 python(pytest) 필수라 별도 의존이 아니다.
set -u

# 스크립트 위치(.claude/)에서 프로젝트 루트 self-resolve (precompact_capture_hook.sh 와 동일 패턴).
hook_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 0
repo_root=$(CDPATH= cd -- "$hook_dir/.." && pwd) || exit 0

# 인터프리터 선택 — 후보를 순회하며 *실행검증*(--version rc)으로 채택(엔진 _detect_py·T-0022 시맨틱과
# 동형·python3 → python). 존재검증(command -v)만으론 Windows WindowsApps 가짜 shim(command -v 통과·
# 실행 시 Permission denied rc126)을 못 거른다. 전부 실패 시 rc0 조용 통과(훅은 정상 작업을 막지 않음).
# stdin JSON 파싱에도 이 인터프리터를 쓰므로 파일 상단에서 먼저 고른다(외부 프로세서 제거 — T-0210).
py=""
for _cand in python3 python; do
    if command -v "$_cand" >/dev/null 2>&1 && "$_cand" --version >/dev/null 2>&1; then
        py="$_cand"
        break
    fi
done
[ -n "$py" ] || exit 0

# hook stdin 에서 편집된 파일 경로를 해소하고, 이 프로젝트 안의 .py 인지까지 python 안에서 판정한다.
# '.tool_input.file_path // .tool_response.filePath // empty' 필터 시맨틱을 python 으로 옮기고
# (부재·비-dict·malformed JSON 은 빈 출력으로 graceful), 경로 형식 정규화 + containment + .py 게이트도
# python 이 한다 — repo_root 를 argv 로 넘긴다. bash case 의 리터럴 접두 매칭은 경로 *형식* 에
# 민감해서, 하네스가 native Windows 경로(C:\...)를 보내면 Git Bash pwd 형(/c/...)인 repo_root 와
# 불일치→rc0 silent skip 했다(T-0210 must-fix). python 이 세 형식(native C:\ · 드라이브 C:/ · mount
# /c/)을 단일 canonical 로 수렴시켜 판정하고, 통과 시에만 경로를 emit → bash 는 비어있음만 검사한다.
# stdin 은 여기서 한 번만 소비하며(이전 구현과 동일 위치), 이후 단계(pytest)는 stdin 을 안 쓴다.
target=$("$py" -c 'import json, os, re, sys

repo_root = sys.argv[1] if len(sys.argv) > 1 else ""

try:
    data = json.load(sys.stdin)
except Exception:
    data = None


def _pick(container, key):
    if isinstance(container, dict):
        value = container.get(key)
        if isinstance(value, str):
            return value
    return None


def _canon(p):
    # 경로를 플랫폼 canonical 형으로 수렴. Windows 는 Git Bash mount(/c/...)를 드라이브형으로
    # 바꾼 뒤 normcase/normpath(대소문자 무시)로, POSIX 는 normpath(대소문자 유지)로 정규화한다.
    # mount 변환·normcase 는 os.name == "nt" 에서만 적용 → POSIX 실경로(/c/... 존재 가능)에 항등.
    if os.name == "nt":
        m = re.match(r"^/([A-Za-z])/", p)
        if m:
            p = m.group(1) + ":/" + p[3:]
        return os.path.normcase(os.path.normpath(p))
    return os.path.normpath(p)


path = None
if isinstance(data, dict):
    path = _pick(data.get("tool_input"), "file_path")
    if path is None:
        path = _pick(data.get("tool_response"), "filePath")

emit = ""
if path and repo_root:
    canon_path = _canon(path)
    canon_root = _canon(repo_root)
    inside = canon_path == canon_root or canon_path.startswith(canon_root + os.sep)
    if inside and canon_path.endswith(".py"):
        emit = path
sys.stdout.write(emit)' "$repo_root")

# 프로젝트 안의 .py 로 판정된 경우에만 target 이 비어있지 않다(위 python 이 형식정규화+게이트 수행).
[ -n "$target" ] || exit 0

cd "$repo_root" || exit 0

result=$("$py" -m pytest tests/ -q --no-header 2>&1 | tail -1)

# systemMessage JSON 출력 — 외부 프로세서 대체(json.dumps 가 escape 안전 보장·result 는 argv 로 전달).
"$py" -c 'import json, sys; sys.stdout.write(json.dumps({"systemMessage": "tests: " + sys.argv[1]}))' "$result"
