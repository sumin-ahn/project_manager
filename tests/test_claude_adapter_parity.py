"""`.claude/` ↔ `templates/claude_code/.claude/` 어댑터 전파 parity 가드 (T-0089·[[T-0088]]).

루트 도그푸딩 `.claude/` 어댑터는 채택 프로젝트용 `templates/claude_code/.claude/` 로
동기화된다(pm_update 전파). 드리프트 시 채택 프로젝트가 옛/다른 동작을 받는다
([[verify-engine-template-propagation]]). v2 머지 전 codex 제안 — 핵심 어댑터 산출물의
양 트리 parity 를 자동 검증한다:
  - `settings.json`(ADR-0038 D3 비대칭): root=PreCompact breadcrumb(auto-compact ON) /
    template=PreCompact 없음 + autoCompactEnabled:false(auto-compact OFF·hard-stop 단일 게이트).
  - `precompact_capture_hook.sh`: root 전용(template 엔 없음) — byte-identical 대상 아님.
  - `skills/pm-handoff/SKILL.md`·`skills/pm-dev-delegate/SKILL.md`: byte-identical.
  - `agents/*.md`: byte-identical(4파일).

**의도된 차이 허용**: 루트 settings 는 ctx 훅(PreToolUse/UserPromptSubmit/statusLine·
ctx_stop_hook·ctx_statusline)을 *싣지 않는다* — 루트 도그푸딩은 그 훅을 안 쓴다. 그래서
settings 는 byte-identical 을 강제하지 않고 **PreCompact 전파만** 양쪽 강제한다(채택
프로젝트 폴백 보장·codex must-fix 맥락). stdlib + json. 파일 iterate(hermetic).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from _settings_portability import (
    SUBST_TOKEN,
    absolute_path_hits,
    portability_failures,
    referenced_hook_paths,
)

REPO = Path(__file__).resolve().parents[1]
ROOT_CLAUDE = REPO / ".claude"
TEMPLATE_CLAUDE = REPO / "templates" / "claude_code" / ".claude"

# 양 트리에서 byte-identical 이어야 하는 어댑터 산출물 (의도된 차이 없음).
IDENTICAL_RELPATHS = [
    "skills/pm-handoff/SKILL.md",
    "skills/pm-dev-delegate/SKILL.md",
    "skills/pm-wave-claim/SKILL.md",
    "agents/architect.md",
    "agents/code-reviewer.md",
    "agents/developer.md",
    "agents/researcher.md",
]


def _precompact_block(settings_path: Path):
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    return data.get("hooks", {}).get("PreCompact")


# ── settings.json: PreCompact 전파 (양쪽 존재 + 동일 명령) ────────────────────

def test_settings_present_both_trees():
    """settings.json 이 양 트리에 존재 + 유효 JSON."""
    for tree in (ROOT_CLAUDE, TEMPLATE_CLAUDE):
        path = tree / "settings.json"
        assert path.exists(), f"settings.json 없음: {path}"
        json.loads(path.read_text(encoding="utf-8"))  # 파싱 실패 시 raise


def test_precompact_asymmetric_root_breadcrumb_template_autocompact_off():
    """ADR-0038 D3 — precompact 는 이제 **원리적 비대칭**(압축 가능한 곳에만 breadcrumb).

    - root(도그푸딩·ctx hard-stop 훅 부재·auto-compact **ON**): PreCompact breadcrumb 존재
      → `precompact_capture_hook.sh` 를 가리킴 (압축이 수동 핸드오프를 선점할 수 있는 유일한
      net-less tree 라 1줄 신호 보존).
    - template(auto-compact **OFF**·hard-stop 단일 게이트): PreCompact **없음** +
      `autoCompactEnabled:false` + env `DISABLE_AUTO_COMPACT` (압축이 hard-stop 을 선점 못 하니
      백스톱 불요). ctx-훅-템플릿-전용의 거울상 = precompact-root-전용.
    """
    root = json.loads((ROOT_CLAUDE / "settings.json").read_text(encoding="utf-8"))
    tmpl = json.loads((TEMPLATE_CLAUDE / "settings.json").read_text(encoding="utf-8"))
    # root: PreCompact breadcrumb 존재 + auto-compact 유지(비활성 아님).
    root_block = root.get("hooks", {}).get("PreCompact")
    assert root_block, "root settings.json 에 PreCompact breadcrumb 누락 (auto-compact ON tree)"
    assert "precompact_capture_hook.sh" in json.dumps(root_block), (
        f"root PreCompact 가 precompact 훅을 안 가리킴: {root_block}"
    )
    assert root.get("autoCompactEnabled") is not False, "root 는 auto-compact 유지여야 함"
    # template: PreCompact 제거 + auto-compact 이중 비활성.
    assert tmpl.get("hooks", {}).get("PreCompact") is None, (
        "template settings.json 에 PreCompact 잔존 — auto-compact off 라 제거됐어야 함 (ADR-0038 D3)"
    )
    assert tmpl.get("autoCompactEnabled") is False, "template autoCompactEnabled:false 누락"
    assert tmpl.get("env", {}).get("DISABLE_AUTO_COMPACT") == "1", (
        "template env DISABLE_AUTO_COMPACT 누락 (이중 kill-switch)"
    )


# ── byte-identical 어댑터 산출물 (hook·skills·agents) ─────────────────────────

def test_adapter_artifacts_byte_identical():
    """precompact 훅·pm-handoff/pm-dev-delegate skill·agents 4파일이 양 트리 byte-identical.

    각 파일에 대해 양 트리 존재 + 바이트 동일 검증 (pm_update 전파 무드리프트).
    """
    for relpath in IDENTICAL_RELPATHS:
        root_path = ROOT_CLAUDE / relpath
        tmpl_path = TEMPLATE_CLAUDE / relpath
        assert root_path.exists(), f"root 어댑터 산출물 없음: {root_path}"
        assert tmpl_path.exists(), f"templates 어댑터 산출물 없음: {tmpl_path}"
        assert root_path.read_bytes() == tmpl_path.read_bytes(), (
            f"{relpath} 가 root↔templates byte-identical 아님 (전파 드리프트) — "
            "엔진/어댑터 변경 후 pm_update 로 전파 필요"
        )


# ── T-0202: settings.json portable-by-construction 가드 (manifest-out·render 미탑승) ──
# 결정 A(사용자 2026-07-02): settings.json + 훅 래퍼(.sh)·훅 스크립트(.py)는 engine.manifest
# **밖**(인스턴스 소유·pm_update 미갱신)이라 import/update 의 토큰 치환 파이프라인을 안 탄다.
# 대신 파일 자체가 portable-by-construction — 치환 토큰·머신-특정 절대경로 0(${CLAUDE_PROJECT_DIR}
# +상대경로+래퍼 self-resolve)이라 어느 머신/프로젝트로 verbatim 복사돼도 그대로 동작한다. 이
# 가드들이 그 성질을 못박는다(render 채널 부재의 백스톱·Windows JSON invalid-escape 원천 차단).
# 절대경로 판정 로직(구조적 JSON 순회)은 _settings_portability 헬퍼에 통합(fresh-adopter e2e 와 공유).

# settings.json 경로 → 그 파일을 담은 프로젝트 루트(훅 상대경로 해소 기준).
_SETTINGS = {
    "template": (TEMPLATE_CLAUDE / "settings.json", REPO / "templates" / "claude_code"),
    "root": (ROOT_CLAUDE / "settings.json", REPO),
}

# hook/statusLine 경로가 써야 하는 인터프리터 wildcard(PM 도구 호출 커버).
_REQUIRED_ALLOW = ("Bash(python3 *)", "Bash(py *)")

# 신규 래퍼(.sh) → self-resolve 로 exec 하는 자기 디렉토리 대응 .py.
_WRAPPERS = {
    "ctx_stop_hook.sh": "ctx_stop_hook.py",
    "ctx_statusline.sh": "ctx_statusline.py",
}


def _executable_lines(shell_text: str) -> str:
    """shell 스크립트에서 주석(#…)·shebang 을 제외한 실행 라인만. 래퍼 주석은 `{{PY}}` 폐기를
    *문서화* 하느라 언급하므로, 토큰 검사는 실행부(=실제 portable 성질)만 대상으로 한다."""
    return "\n".join(
        line for line in shell_text.splitlines() if not line.lstrip().startswith("#")
    )


@pytest.mark.parametrize("tree", list(_SETTINGS))
def test_settings_portable_by_construction(tree):
    """settings.json 이 유효 JSON·치환 토큰 0·머신-특정 절대경로 0 (템플릿 + ① 루트 각각).

    - 유효 JSON: Windows 절대경로의 invalid escape(`\\w`·`\\c` 등)를 원천 차단하는 게이트.
    - 치환 토큰 0: manifest-out 이라 렌더 안 됨 → `{{...}}` 가 있으면 리터럴 잔존해 훅이 깨진다.
    - 절대경로 0: 운영 문자열값(hooks/statusLine·env·permissions)을 구조적 순회해 POSIX '/…'
      (`/tmp`·`/opt` 등 일반 경로 포함)·드라이브 'C:\\…' 를 잡는다 → git 공유·재-import 시 고착 차단.
    """
    path, _base = _SETTINGS[tree]
    text = path.read_text(encoding="utf-8")
    json.loads(text)  # 파싱 실패 시 raise (invalid-escape 포함)
    failures = portability_failures(text)
    assert not failures, f"{tree} settings.json portable-by-construction 위반: {failures}"


@pytest.mark.parametrize("tree", list(_SETTINGS))
def test_settings_hook_files_exist_and_executable(tree):
    """settings.json 이 가리키는 훅/statusLine 파일이 그 트리에 실재 + `.sh`는 실행비트 (양 트리 대칭).

    참조 경로를 settings.json 파싱으로 동적 추출(하드코딩 목록 아님) — 배선된 훅 파일이 누락되거나
    실행비트를 잃으면(chmod 소실) 여기서 터진다. 템플릿은 import 가 트리를 verbatim 복사하므로 트리
    정합이 곧 adopter 정합, 루트(①)는 프레임워크 dev 홈 자체의 훅 배선 정합(portable 가드와 같은
    `_SETTINGS` parametrize 대칭 — reviewer suggestion).
    """
    text, base = _SETTINGS[tree][0].read_text(encoding="utf-8"), _SETTINGS[tree][1]
    refs = referenced_hook_paths(text)
    assert refs, f"{tree} settings.json 에서 훅/statusLine 참조 경로를 하나도 못 뽑음"
    for rel in refs:
        target = base / rel
        assert target.is_file(), f"{tree} settings.json 이 가리키는 훅 파일 부재: {rel} ({target})"
        if rel.endswith(".sh"):
            assert os.access(target, os.X_OK), f"{tree} {rel} 실행비트 없음 (os.X_OK) — 훅 실행 불가"


@pytest.mark.parametrize("value,should_hit", [
    # 절대경로 = FAIL 케이스 (셸 연산자 경계 포함 — codex T-0202 r3 필수 커버)
    ("cmd >/tmp/log", True),
    ("cmd 2>/opt/log", True),
    ("VAR=/workspace/x cmd", True),
    ("Bash(cat /etc/hosts)", True),
    ("Bash(/usr/bin/foo)", True),
    ("C:\\proj\\x", True),
    ("C:/proj/x", True),
    # 정상 = PASS 케이스 (false-fire 경계 고정)
    ("${CLAUDE_PROJECT_DIR}/.claude/ctx_stop_hook.sh", False),
    (".claude/ctx_statusline.sh", False),
    ("https://github.com/foo/bar", False),
    ("git@host:/repo", False),
    ("Bash(mkdir -p ~/.ssh)", False),
    ("Bash(git ls-remote *)", False),
])
def test_absolute_path_hits_boundaries(value, should_hit):
    """절대경로 판정의 경계 고정 회귀 — 셸 연산자(> < | ; &) 직후 절대경로를 잡고(`cmd >/tmp/log`
    류·codex r3), URL·scp·`~`홈·런타임 var·상대경로는 false-fire 하지 않는다."""
    hits = absolute_path_hits(value)
    if should_hit:
        assert hits, f"절대경로 미탐지(가드 갭): {value!r}"
    else:
        assert not hits, f"false-fire: {value!r} → {hits}"


@pytest.mark.parametrize("wrapper,sibling", list(_WRAPPERS.items()))
def test_new_wrappers_self_contained(wrapper, sibling):
    """신규 래퍼(.sh)가 실행부 치환 토큰 0 + 자기 디렉토리 대응 .py 를 참조 (self-resolve).

    래퍼는 인터프리터를 python3→python 로 self-resolve 하고 대응 .py 를 exec 한다 — 치환 토큰
    없이 모든 머신 byte-identical. (주석의 `{{PY}}` 언급은 '폐기' 문서화라 실행부만 검사한다.)
    """
    path = TEMPLATE_CLAUDE / wrapper
    assert path.is_file(), f"신규 래퍼 부재: {wrapper}"
    text = path.read_text(encoding="utf-8")
    executable = _executable_lines(text)
    leaked = SUBST_TOKEN.findall(executable)
    assert not leaked, f"{wrapper} 실행부에 치환 토큰 {set(leaked)} 잔존 — 래퍼는 self-resolve 라 렌더 불요"
    # 실행부에서 검사 — 주석의 sibling 언급만으로 통과하면 exec 대상이 틀려도 가드가 침묵한다(codex).
    assert sibling in executable, f"{wrapper} 실행부가 대응 {sibling} 를 exec 하지 않음 (self-resolve 대상 부재)"
    assert (TEMPLATE_CLAUDE / sibling).is_file(), f"대응 {sibling} 파일이 트리에 부재"


@pytest.mark.parametrize("tree", list(_SETTINGS))
def test_settings_permissions_cover_pm_interpreters(tree):
    """permissions.allow 가 `Bash(python3 *)`·`Bash(py *)` 를 포함 (PM 도구 호출 커버·양 트리)."""
    data = json.loads(_SETTINGS[tree][0].read_text(encoding="utf-8"))
    allow = data.get("permissions", {}).get("allow", [])
    for entry in _REQUIRED_ALLOW:
        assert entry in allow, (
            f"{tree} settings.json permissions.allow 에 {entry} 누락 (PM 도구 인터프리터 호출 미커버)")
