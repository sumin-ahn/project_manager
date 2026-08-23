"""위임 모델·추론 토큰의 카드 배선 가드 — 생산(local.conf)과 소비(agent 카드)를 한 진실로 묶는다.

배경(T-0766). 세 하네스의 카드가 **상보적으로 반쪽씩** 비어 있었다: claude·opencode 는 4 역할
카드에 모델 토큰을 실었지만 hard 티어 카드 자체가 없었고, codex 는 hard 카드만 토큰을 싣고 역할
카드 4장은 모델 키를 생략했다. 두 빈칸이 서로를 가려서 어느 쪽에서 봐도 "대부분 되네" 로 보였고,
실제로는 어느 타깃도 5개 프로필 전부를 `local.conf` 로 제어하지 못했다.

이 모듈이 그 클래스를 닫는다:

  (1) **양방향 대조** — 토큰 표(`pm_render.DELEGATE_MODEL_CONF_KEYS`)가 소비를 기대하는데 카드에
      없으면 red, 카드가 쓰는데 표에 없으면 red. 기대는 손열거 표가 아니라 **카드 형식**에서
      파생한다(`pm_render.CARD_FIELD_TOKEN_PREFIXES` + 트리별 지원 필드) — 모델 토큰은 세 타깃
      전부, 추론 토큰은 codex TOML 만. claude·opencode frontmatter 에 추론을 실을 필드가 없는 건
      하네스 예외가 아니라 형식 차이다.
  (2) **실 해소 경로** — 문자열 존재 검사가 아니라 `local.conf` → `_operational_from_local_conf`
      → `render_adapter` 를 그대로 태워 렌더된 필드 값이 conf 값과 같은지 본다. 카드와 conf 가
      어긋나면 그 자리에서 red 다.
  (3) **민감도** — 카드 한 장에서 토큰 한 줄을 지우면 (1) 이 실제로 red 를 내는지 실측한다.
      가드가 자기 표면을 덮는지 스스로 증명하지 못하면 시야는 언제든 표면보다 좁아진다.
  (4) **손열거 표면 시야** — 카드 집합을 손으로 적어 둔 다섯 자리(다른 테스트 모듈의 상수 + 가드
      카드맵)가 실제 카드 집합과 등호인지 본다. 이 다섯은 카드를 늘려도 red 를 내지 않아 무가드
      출하를 만드는 자리다.
  (5) **전파 전제** — 새 카드가 git tracked 여야 출하 인벤토리(`TRACKED_ONLY`)에 들어간다.

stdlib + tomllib(3.11+). CLI 미실행·파일 iterate(hermetic).
"""
from __future__ import annotations

import importlib
import importlib.util
import re
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

DELEGATE_TOKEN_RE = re.compile(r"\{\{(DELEGATE_[A-Z0-9_]+)\}\}")


def _load_tool(name: str):
    spec = importlib.util.spec_from_file_location(
        f"t0766_{name}", TOOLS / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pm_render():
    return _load_tool("pm_render")


@pytest.fixture(scope="module")
def pm_update():
    return _load_tool("pm_update")


@pytest.fixture(scope="module")
def guard():
    return _load_tool("delegate_channel_guard")


# ── 카드 트리 선언 — "어떤 필드를 실을 수 있는가" 는 카드 형식이 정한다 ──────────
# 값을 손으로 적지 않는다: 기대 소비는 이 형식 선언과 토큰 표에서 파생하고, 카드 stem 도 토큰
# 표에서 파생한다. 새 역할/티어가 표에 늘면 네 트리 전부가 자동으로 검사 대상이 된다.

@dataclass(frozen=True)
class CardTree:
    """한 어댑터 네임스페이스의 카드 묶음."""

    label: str
    relative_dir: str          # 인스턴스 상대 — render 의 card_path 좌표이기도 하다.
    suffix: str
    assignment: str            # 필드 대입 표기(YAML frontmatter `: ` vs TOML ` = `).
    harness: str
    # 이 형식이 실을 수 있는 필드. 없는 필드에 토큰을 넣으면 소비처 없는 배선이다.
    fields: tuple[str, ...]
    # 위임 역할이 아닌 카드(=이 가드의 대상 밖). 사유를 함께 둔다.
    exempt_stems: tuple[tuple[str, str], ...] = ()

    def directory(self, root: Path) -> Path:
        return root / self.relative_dir

    def card_path(self, stem: str) -> str:
        return f"{self.relative_dir}/{stem}{self.suffix}"

    def field_line(self, field: str, token: str) -> str:
        return f'{field}{self.assignment}"{{{{{token}}}}}"'


_MD_FIELDS = ("model",)                                # claude·opencode frontmatter
_TOML_FIELDS = ("model", "model_reasoning_effort")     # codex custom agent TOML

CARD_TREES = (
    CardTree("claude(root)", ".claude/agents", ".md", ": ", "claude", _MD_FIELDS),
    CardTree(
        "claude(templates)", "templates/claude_code/.claude/agents", ".md", ": ",
        "claude", _MD_FIELDS,
    ),
    CardTree(
        "codex(templates)", "templates/codex/.codex/agents", ".toml", " = ",
        "codex", _TOML_FIELDS,
    ),
    CardTree(
        "opencode(templates)", "templates/opencode/.opencode/agents", ".md", ": ",
        "opencode", _MD_FIELDS,
        exempt_stems=(
            ("pm", "PM primary 카드 — 위임 역할이 아니라 PM 자신의 모델(설치 모델 pin)"),
        ),
    ),
)

# 렌더 시점의 인스턴스 상대 좌표는 어댑터 네임스페이스부터다(templates/ 접두는 소스 트리 사정).
_INSTANCE_PREFIX = re.compile(r"^templates/[^/]+/")


def _instance_card_path(tree: CardTree, stem: str) -> str:
    return _INSTANCE_PREFIX.sub("", tree.card_path(stem))


def _token_stem(token: str, prefix: str) -> str:
    """`DELEGATE_MODEL_CODE_REVIEWER` → `code-reviewer` (표에서 카드 stem 파생·사본 0)."""
    return token[len(prefix):].lower().replace("_", "-")


def _expected_stems(pm_render_module) -> tuple[str, ...]:
    """토큰 표가 요구하는 카드 stem 집합 — 4 역할 축 + developer hard 티어."""
    stems = {
        _token_stem(token, "DELEGATE_MODEL_")
        for token in pm_render_module.DELEGATE_MODEL_CONF_KEYS
        if token.startswith("DELEGATE_MODEL_")
    }
    return tuple(sorted(stems))


def _expected_tokens(pm_render_module, tree: CardTree, stem: str) -> dict[str, str]:
    """이 트리의 이 카드가 소비해야 하는 {필드: 토큰} — 형식 선언에서 파생."""
    wanted: dict[str, str] = {}
    suffix = stem.upper().replace("-", "_")
    for prefix, field in pm_render_module.CARD_FIELD_TOKEN_PREFIXES.items():
        if field not in tree.fields:
            continue
        token = f"{prefix}{suffix}"
        if token in pm_render_module.DELEGATE_MODEL_CONF_KEYS:
            wanted[field] = token
    return wanted


def _wiring_problems(pm_render_module, root: Path) -> list[str]:
    """(토큰, 타깃) 기대 소비 표와 실제 카드의 **양방향** 대조 결과.

    빈 리스트가 아니면 어느 한쪽이 조용히 반쪽이 된 것이다. 테스트가 이 함수를 REPO 와 변이
    사본 양쪽에 태워 가드 자신의 민감도까지 판정한다.
    """
    problems: list[str] = []
    stems = _expected_stems(pm_render_module)
    for tree in CARD_TREES:
        directory = tree.directory(root)
        if not directory.is_dir():
            problems.append(f"{tree.label}: 카드 디렉터리 부재 ({tree.relative_dir})")
            continue
        exempt = {stem for stem, _reason in tree.exempt_stems}

        # (a) 표 → 카드. 기대하는데 없으면 그 프로필은 conf 로 제어되지 않는다.
        for stem in stems:
            path = directory / f"{stem}{tree.suffix}"
            if not path.is_file():
                problems.append(f"{tree.label}: 카드 부재 {tree.card_path(stem)}")
                continue
            text = path.read_text(encoding="utf-8")
            for field, token in _expected_tokens(pm_render_module, tree, stem).items():
                if tree.field_line(field, token) not in text:
                    problems.append(
                        f"{tree.label}: {tree.card_path(stem)} 에 "
                        f"{tree.field_line(field, token)!r} 없음 — 토큰 미소비"
                    )

        # (b) 카드 → 표. 표에 없는 토큰이나 형식이 못 싣는 필드는 해소를 검증할 수 없다.
        for path in sorted(directory.glob(f"*{tree.suffix}")):
            stem = path.stem
            text = path.read_text(encoding="utf-8")
            found = set(DELEGATE_TOKEN_RE.findall(text))
            if stem in exempt:
                if found:
                    problems.append(
                        f"{tree.label}: 면제 카드 {tree.card_path(stem)} 가 위임 토큰 "
                        f"{sorted(found)} 를 쓴다 — 면제 사유와 어긋난다"
                    )
                continue
            wanted = _expected_tokens(pm_render_module, tree, stem)
            for token in sorted(found):
                if token not in pm_render_module.DELEGATE_MODEL_CONF_KEYS:
                    problems.append(
                        f"{tree.label}: {tree.card_path(stem)} 의 {token} 이 토큰 표에 없다"
                    )
                elif token not in wanted.values():
                    problems.append(
                        f"{tree.label}: {tree.card_path(stem)} 이 자기 것이 아닌 토큰 "
                        f"{token} 을 쓴다(형식 미지원 또는 다른 역할)"
                    )
    return problems


# ── (1) 양방향 대조 ──────────────────────────────────────────────────────────

def test_delegate_tokens_wired_in_every_card_tree_both_ways(pm_render):
    """표가 기대하는 소비 == 카드가 실제로 하는 소비 (미소비 0 · 표 밖 토큰 0)."""
    problems = _wiring_problems(pm_render, REPO)
    assert problems == [], "위임 토큰 배선이 반쪽이다:\n" + "\n".join(problems)


def test_expected_consumption_covers_the_whole_token_table(pm_render):
    """가드가 표의 **모든** 토큰을 실제로 요구하는지 — 자명 통과 방지(시야 == 표면).

    (a) 는 카드 stem 을 돌기 때문에, 표에만 있고 아무 stem 에도 안 붙는 토큰이 생기면 조용히
    검사 밖으로 빠진다. 표 전량이 어느 트리에선가 기대 대상인지 뒤집어 센다.
    """
    demanded: set[str] = set()
    for tree in CARD_TREES:
        for stem in _expected_stems(pm_render):
            demanded.update(_expected_tokens(pm_render, tree, stem).values())
    assert demanded == set(pm_render.DELEGATE_MODEL_CONF_KEYS), (
        "토큰 표와 가드 시야가 갈렸다 — 미요구: "
        f"{sorted(set(pm_render.DELEGATE_MODEL_CONF_KEYS) - demanded)}"
    )


def test_reasoning_tokens_are_expected_only_where_the_format_carries_them(pm_render):
    """추론 토큰 기대는 codex TOML 트리에만 — 형식 차이지 하네스 예외가 아니다.

    claude·opencode frontmatter 에 추론 필드가 없는데 기대를 걸면 영구 red 가 되고, 반대로
    codex 에서 기대를 빼면 그 카드의 추론이 conf 밖으로 샌다.
    """
    for tree in CARD_TREES:
        reasoning = {
            token
            for stem in _expected_stems(pm_render)
            for field, token in _expected_tokens(pm_render, tree, stem).items()
            if field == "model_reasoning_effort"
        }
        if tree.harness == "codex":
            assert reasoning == {
                token for token in pm_render.DELEGATE_MODEL_CONF_KEYS
                if token.startswith("DELEGATE_REASONING_")
            }, f"{tree.label}: 추론 토큰 기대가 표와 다르다"
        else:
            assert reasoning == set(), (
                f"{tree.label}: 추론 필드가 없는 형식인데 추론 토큰을 기대한다"
            )


# ── (3) 민감도 — 한 타깃에서 토큰 하나를 빼면 red ─────────────────────────────

@pytest.mark.parametrize(
    ("tree_label", "stem", "field"),
    [
        ("claude(root)", "developer-hard", "model"),
        ("claude(templates)", "developer-hard", "model"),
        ("codex(templates)", "developer", "model"),
        ("codex(templates)", "architect", "model_reasoning_effort"),
        ("opencode(templates)", "developer-hard", "model"),
    ],
)
def test_guard_is_sensitive_to_one_missing_token(
    pm_render, tmp_path, tree_label, stem, field
):
    """실 카드 트리를 복사해 토큰 한 줄을 지우면 같은 가드가 red 를 낸다.

    존재가 아니라 값을 재는 축임을 실측한다 — 민감도 없는 가드는 카드가 늘 때 한 타깃이 빠져도
    영원히 green 이다(이 티켓이 닫는 재발 클래스 그 자체).
    """
    tree = next(t for t in CARD_TREES if t.label == tree_label)
    root = tmp_path / "mutated"
    for candidate in CARD_TREES:
        source = candidate.directory(REPO)
        destination = candidate.directory(root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination)

    assert _wiring_problems(pm_render, root) == [], "대조군: 무변이 사본이 이미 red 다"

    token = _expected_tokens(pm_render, tree, stem)[field]
    target = tree.directory(root) / f"{stem}{tree.suffix}"
    text = target.read_text(encoding="utf-8")
    line = tree.field_line(field, token)
    assert line in text, f"변이 전제 실패: {line!r} 가 없다"
    target.write_text(text.replace(line + "\n", ""), encoding="utf-8")

    problems = _wiring_problems(pm_render, root)
    assert any(token in problem for problem in problems), (
        f"{tree_label}/{stem} 의 {field} 를 지웠는데 가드가 잡지 못했다: {problems}"
    )


def test_guard_rejects_a_token_the_table_does_not_declare(pm_render, tmp_path):
    """카드가 표에 없는 위임 토큰을 쓰면 red — 역방향(카드 → 표)도 실제로 잡는지."""
    root = tmp_path / "mutated"
    for candidate in CARD_TREES:
        destination = candidate.directory(root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(candidate.directory(REPO), destination)

    tree = next(t for t in CARD_TREES if t.label == "claude(root)")
    target = tree.directory(root) / "researcher.md"
    target.write_text(
        target.read_text(encoding="utf-8")
        + '\nstray: "{{DELEGATE_MODEL_NONEXISTENT_ROLE}}"\n',
        encoding="utf-8",
    )

    problems = _wiring_problems(pm_render, root)
    assert any("DELEGATE_MODEL_NONEXISTENT_ROLE" in problem for problem in problems), (
        f"표에 없는 토큰을 카드가 쓰는데 통과했다: {problems}"
    )


# ── (2) 실 해소 경로 — local.conf 를 읽어 렌더된 값으로 단언 ──────────────────

def _write_local_conf(dest_root: Path, text: str) -> Path:
    path = dest_root / ".project_manager" / "local.conf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _conf_text(pm_render_module, harness: str) -> tuple[str, dict[str, str]]:
    """모든 프로필을 `harness` 로 보내는 local.conf 본문 + 기대 해소값(token-key → 값).

    값은 프로필마다 다르게 준다 — 같은 값이면 토큰이 서로 뒤바뀌어도 통과한다.
    """
    # 카드가 함께 담는 operational 토큰(프로젝트 이름)도 실 채널로 해소해야 렌더가 자족한다.
    lines: list[str] = ["project.name=Acme"]
    expected: dict[str, str] = {}
    for token, conf_key in sorted(pm_render_module.DELEGATE_MODEL_CONF_KEYS.items()):
        value = token.lower().replace("delegate_", "")
        lines.append(f"{conf_key}={value}")
        expected[token] = value
    for conf_key in sorted({
        pm_render_module.DELEGATE_HARNESS_CONF_KEYS[token]
        for token in pm_render_module.DELEGATE_MODEL_CONF_KEYS
    }):
        lines.append(f"{conf_key}={harness}")
    return "\n".join(lines) + "\n", expected


@pytest.mark.parametrize("tree", CARD_TREES, ids=lambda tree: tree.label)
def test_cards_render_local_conf_values_through_the_real_path(
    pm_render, pm_update, tmp_path, tree
):
    """`local.conf` → 해소 → 렌더까지 실 경로를 태워 카드 필드가 conf 값이 되는지 본다.

    문자열 존재 검사가 아니라 **반환값**을 본다: 토큰이 있어도 채널 배선이 빠지면 여기서 red 다
    (선언이 실행면에 닿지 않는 상태를 그 자리에서 표면화한다).
    """
    dest = tmp_path / f"dest-{tree.harness}"
    conf_text, expected = _conf_text(pm_render, tree.harness)
    _write_local_conf(dest, conf_text)

    operational, empty_keys = pm_update._operational_from_local_conf(dest)
    delegate_harness = pm_update._delegate_harness_from_local_conf(dest)
    assert empty_keys == []

    for stem in _expected_stems(pm_render):
        relative = _instance_card_path(tree, stem)
        source = tree.directory(REPO) / f"{stem}{tree.suffix}"
        rendered = pm_render.render_adapter(
            source.read_text(encoding="utf-8"),
            operational,
            source=relative,
            empty_keys=empty_keys,
            delegate_harness=delegate_harness,
            card_path=relative,
        )
        assert "{{" not in rendered, f"{relative}: 미해소 토큰 잔존"
        for field, token in _expected_tokens(pm_render, tree, stem).items():
            line = f"{field}{tree.assignment}\"{expected[token]}\""
            assert line in rendered, (
                f"{relative}: {line!r} 로 해소되지 않았다 — conf 선언이 카드에 닿지 않는다"
            )
        if tree.suffix == ".toml":
            data = tomllib.loads(rendered)
            for field, token in _expected_tokens(pm_render, tree, stem).items():
                assert data[field] == expected[token]


@pytest.mark.parametrize("tree", CARD_TREES, ids=lambda tree: tree.label)
def test_card_conf_mismatch_is_red_on_the_resolution_path(
    pm_render, pm_update, tmp_path, tree
):
    """카드가 conf 와 어긋나면(토큰이 다른 역할 것으로 바뀌면) 해소 결과가 달라져 red.

    민감도 축 — 위 테스트가 값을 실제로 재는지 확인한다.
    """
    dest = tmp_path / f"dest-{tree.harness}"
    conf_text, expected = _conf_text(pm_render, tree.harness)
    _write_local_conf(dest, conf_text)
    operational, empty_keys = pm_update._operational_from_local_conf(dest)
    delegate_harness = pm_update._delegate_harness_from_local_conf(dest)

    relative = _instance_card_path(tree, "developer")
    source = tree.directory(REPO) / f"developer{tree.suffix}"
    swapped = source.read_text(encoding="utf-8").replace(
        tree.field_line("model", "DELEGATE_MODEL_DEVELOPER"),
        tree.field_line("model", "DELEGATE_MODEL_ARCHITECT"),
    )
    rendered = pm_render.render_adapter(
        swapped, operational, source=relative, empty_keys=empty_keys,
        delegate_harness=delegate_harness, card_path=relative,
    )
    developer_line = f"model{tree.assignment}\"{expected['DELEGATE_MODEL_DEVELOPER']}\""
    assert developer_line not in rendered, (
        "카드 토큰을 다른 역할 것으로 바꿨는데 해소 결과가 그대로다 — 값을 재지 않고 있다"
    )


def test_unused_profile_neutralization_keeps_codex_toml_valid(
    pm_render, pm_update, tmp_path
):
    """conf 하네스 ≠ codex 면 codex 카드 5장이 intentional-TODO 로 중화되고 TOML 이 유효하다.

    adopter#0 처럼 전 프로필을 다른 하네스로 보내는 형상에서, 새로 배선한 역할 카드가 렌더를
    깨지 않는지(rc 불변) 본다. 중화는 값을 채우는 대신 사유를 남기는 경로다.
    """
    tree = next(t for t in CARD_TREES if t.harness == "codex")
    dest = tmp_path / "dest-claude-profiles"
    conf_text, _expected = _conf_text(pm_render, "claude")
    _write_local_conf(dest, conf_text)
    operational, empty_keys = pm_update._operational_from_local_conf(dest)
    delegate_harness = pm_update._delegate_harness_from_local_conf(dest)

    for stem in _expected_stems(pm_render):
        relative = _instance_card_path(tree, stem)
        source = tree.directory(REPO) / f"{stem}{tree.suffix}"
        rendered = pm_render.render_adapter(
            source.read_text(encoding="utf-8"), operational, source=relative,
            empty_keys=empty_keys, delegate_harness=delegate_harness,
            card_path=relative,
        )
        assert "{{" not in rendered, f"{relative}: 중화 후에도 토큰 잔존"
        data = tomllib.loads(rendered)
        assert "model" not in data and "model_reasoning_effort" not in data, (
            f"{relative}: 미사용 프로필인데 모델 키가 살아 있다"
        )
        assert "TODO" in rendered, f"{relative}: 중화 사유가 남지 않았다"


# ── claude hard 카드는 model 만 싣는다 ────────────────────────────────────────

def test_claude_hard_card_carries_model_only(pm_render):
    """claude hard 카드에 추론 토큰이 없다 — frontmatter 에 그 값을 실을 필드가 없다."""
    for tree in CARD_TREES:
        if tree.harness != "claude":
            continue
        text = (tree.directory(REPO) / "developer-hard.md").read_text(encoding="utf-8")
        found = set(DELEGATE_TOKEN_RE.findall(text))
        assert found == {"DELEGATE_MODEL_DEVELOPER_HARD"}, (
            f"{tree.label}: hard 카드의 위임 토큰 집합이 모델 하나가 아니다 — {sorted(found)}"
        )


def test_reasoning_token_in_a_claude_card_fails_loud(pm_render, pm_update, tmp_path):
    """소비 필드가 없는 자리에 추론 토큰을 넣으면 렌더가 `RenderLeakError` 로 막는다.

    "claude 도 추론을 실어 주자" 는 변경이 조용히 통과하면, 카드에 아무도 안 읽는 필드가 남고
    그 값이 실행면에 닿는 것처럼 보인다. 중화 대상 줄(`model`/`model_reasoning_effort`)이 아닌
    자리라 leak 으로 표면화되는 것이 옳다.
    """
    dest = tmp_path / "dest"
    # hard 프로필 모델만 선언 — 추론 키는 없다(그 토큰이 해소될 곳이 없는 형상).
    _write_local_conf(dest, "delegate.developer.hard.model=opus\n")
    operational, empty_keys = pm_update._operational_from_local_conf(dest)

    source = (REPO / ".claude/agents/developer-hard.md").read_text(encoding="utf-8")
    injected = source.replace(
        'model: "{{DELEGATE_MODEL_DEVELOPER_HARD}}"',
        'model: "{{DELEGATE_MODEL_DEVELOPER_HARD}}"\n'
        'reasoning: "{{DELEGATE_REASONING_DEVELOPER_HARD}}"',
    )
    with pytest.raises(pm_render.RenderLeakError) as excinfo:
        pm_render.render_adapter(
            injected, operational, source=".claude/agents/developer-hard.md",
            empty_keys=empty_keys, card_path=".claude/agents/developer-hard.md",
        )
    assert "DELEGATE_REASONING_DEVELOPER_HARD" in str(excinfo.value)


def test_empty_hard_model_fails_loud_with_the_conf_key(pm_render, pm_update, tmp_path):
    """`delegate.developer.hard.model=` (빈값)은 중화가 아니라 fail-loud 다 — 오설정 신호.

    부재는 정상 형상(중화)이지만 빈값은 손-편집·손상 신호라 조용히 넘기면 hard 위임이 모델 없이
    돈다. 메시지가 채울 conf 키를 이름으로 지목하는지까지 단언한다.
    """
    dest = tmp_path / "dest"
    _write_local_conf(dest, "delegate.developer.hard.model=\n")
    operational, empty_keys = pm_update._operational_from_local_conf(dest)
    assert empty_keys == ["DELEGATE_MODEL_DEVELOPER_HARD"]

    source = (REPO / ".claude/agents/developer-hard.md").read_text(encoding="utf-8")
    with pytest.raises(pm_render.RenderLeakError) as excinfo:
        pm_render.render_adapter(
            source, operational, source=".claude/agents/developer-hard.md",
            empty_keys=empty_keys, card_path=".claude/agents/developer-hard.md",
        )
    message = str(excinfo.value)
    assert "delegate.developer.hard.model" in message, message
    assert "{{DELEGATE_MODEL_DEVELOPER_HARD}}" in message, message


# ── (4) 손열거 표면 시야 == 카드 표면 ─────────────────────────────────────────

def _claude_card_stems() -> set[str]:
    tree = next(t for t in CARD_TREES if t.label == "claude(root)")
    return {path.stem for path in tree.directory(REPO).glob("*.md")}


def _developer_axis_surfaces() -> set[str]:
    """네 트리의 developer 축 카드 상대경로(REPO 기준) — hard 티어 포함."""
    surfaces: set[str] = set()
    for tree in CARD_TREES:
        for path in tree.directory(REPO).glob(f"*{tree.suffix}"):
            if path.stem == "developer" or path.stem.startswith("developer-"):
                surfaces.add(path.relative_to(REPO).as_posix())
    return surfaces


def _hand_enumerations() -> dict[str, tuple[object, object]]:
    """손열거 표면 → (실제 열거에서 뽑은 시야, 실 카드 표면). 등호가 불변식이다."""
    frontmatter = importlib.import_module("test_agents_frontmatter")
    parity = importlib.import_module("test_claude_adapter_parity")
    board_lint = importlib.import_module("test_board_lint")
    fix_scope = importlib.import_module("test_fix_round_prescription_scope")
    large_output = importlib.import_module("test_large_output_file_delivery_convention")

    claude_stems = _claude_card_stems()
    developer_axis = {stem for stem in claude_stems if stem.startswith("developer")}
    return {
        "test_agents_frontmatter.AGENT_NAMES": (
            {Path(name).stem for name in frontmatter.AGENT_NAMES},
            claude_stems,
        ),
        "test_claude_adapter_parity.IDENTICAL_RELPATHS": (
            {
                Path(rel).stem for rel in parity.IDENTICAL_RELPATHS
                if rel.startswith("agents/")
            },
            claude_stems,
        ),
        "test_board_lint._T0463_TOKEN_FORM_MIRRORS": (
            {
                Path(rel).stem for rel in board_lint._T0463_TOKEN_FORM_MIRRORS
                if rel.startswith(".claude/agents/")
            },
            claude_stems,
        ),
        "test_fix_round_prescription_scope.DEVELOPER_CARD_SURFACES": (
            {
                path.relative_to(REPO).as_posix()
                for path in fix_scope.DEVELOPER_CARD_SURFACES
            },
            _developer_axis_surfaces(),
        ),
        "test_large_output_file_delivery_convention._ROLES": (
            {role for role in large_output._ROLES if role.startswith("developer")},
            developer_axis,
        ),
    }


@pytest.mark.parametrize("surface", sorted(_hand_enumerations()))
def test_hand_enumerated_surfaces_see_the_whole_card_set(surface):
    """카드 집합을 손으로 적어 둔 자리가 실 카드 표면과 등호인지 (시야 == 표면).

    이 다섯은 카드를 늘려도 스스로 red 를 내지 않는다 — 그래서 새 카드가 가드 없이 출하되는
    자리다. 여기서 등호를 강제해 "한 곳이라도 빠지면 red" 를 만든다.
    """
    view, actual = _hand_enumerations()[surface]
    assert view == actual, (
        f"{surface} 의 시야가 실 카드 표면과 갈렸다 — 누락 {sorted(actual - view)} / "
        f"유령 {sorted(view - actual)}"
    )


def test_guard_card_map_sees_the_whole_claude_card_set(guard):
    """가드 카드맵(6번째 손열거 표면)도 claude 카드 전량을 본다."""
    view = {path.stem for path in guard.CLAUDE_NATIVE_AGENT_CARDS.values()}
    assert view == _claude_card_stems(), (
        f"가드 카드맵 시야가 갈렸다 — 누락 {sorted(_claude_card_stems() - view)}"
    )


@pytest.mark.parametrize("surface", sorted(_hand_enumerations()))
def test_hand_enumeration_check_is_sensitive_to_a_dropped_entry(surface):
    """민감도 probe — 시야에서 hard 항목 하나를 빼면 등호가 실제로 깨진다."""
    view, actual = _hand_enumerations()[surface]
    dropped = {item for item in view if "developer-hard" in str(item)}
    assert dropped, f"{surface}: hard 항목이 시야에 없다(전제 실패)"
    assert (view - dropped) != actual, (
        f"{surface}: hard 항목을 빼도 등호가 유지된다 — 이 가드는 자명 통과다"
    )


# ── (5) 전파 전제 — 새 카드는 git tracked ─────────────────────────────────────

def test_hard_cards_are_git_tracked(pm_update):
    """hard 카드가 tracked 여야 출하 인벤토리(`TRACKED_ONLY`)에 들어간다.

    untracked 파일은 조용히 전파에서 빠진다 — 카드를 만들고 add 하지 않으면 채택자 트리엔
    영원히 안 나가고, 그 사실은 어느 렌더 가드에도 걸리지 않는다.
    """
    repo_files = pm_update._load_repo_owned_files()
    for tree in CARD_TREES:
        relative = Path(tree.relative_dir)
        tracked = {
            path.name
            for path in repo_files.list_repo_owned_files(
                REPO, relative, mode=repo_files.TRACKED_ONLY
            )
        }
        assert f"developer-hard{tree.suffix}" in tracked, (
            f"{tree.label}: developer-hard 카드가 git tracked 가 아니다 — 전파되지 않는다 "
            f"(tracked={sorted(tracked)})"
        )
