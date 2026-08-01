"""출하 엔진 domain coverage 집계·README claim·advisory 경계 (T-0495)."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def domain():
    return _load("domain_coverage_test", TOOLS / "domain.py")


def _coverage_tree(
        tmp_path: Path,
        domain,
        *,
        shipped=("alpha.py", "beta.py"),
        manifest_entries=None,
        with_page=True,
):
    root = tmp_path / "adopter"
    tools_dir = root / ".project_manager" / "tools"
    domain_dir = root / ".project_manager" / "wiki" / "domain"
    tools_dir.mkdir(parents=True)
    domain_dir.mkdir(parents=True)
    for name in shipped:
        (tools_dir / name).write_text("# tool\n", encoding="utf-8")
    manifest = root / ".project_manager" / "engine.manifest"
    entries = (
        [
            (Path(".project_manager") / "tools" / name).as_posix()
            for name in shipped
        ]
        if manifest_entries is None else list(manifest_entries)
    )
    manifest.write_text("\n".join(entries) + ("\n" if entries else ""), encoding="utf-8")
    if with_page:
        page = domain_dir / "engine.md"
        page.write_text(
            "---\n"
            "title: 엔진\n"
            "type: concept\n"
            "covers:\n"
            "  - .project_manager/tools/alpha.py\n"
            "---\n"
            "# 엔진\n",
            encoding="utf-8",
        )
    readme = domain_dir / "README.md"
    subprocess.run(
        ["git", "-C", str(root), "init", "-q"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "add", "-f", "-A"],
        check=True,
        capture_output=True,
        text=True,
    )
    return root, tools_dir, domain_dir, manifest, readme


def _readme(domain, readme: Path, claim: str) -> None:
    readme.write_text(
        "# Domain coverage\n\n"
        f"`{domain.COVERAGE_COMMAND}`\n\n"
        f"<!-- domain-coverage-claim: {claim} -->\n",
        encoding="utf-8",
    )


def _findings(domain, domain_dir, manifest, *, exemptions=None):
    pages = domain.load_pages(domain_dir)
    return domain.coverage_findings(
        pages,
        domain_dir=domain_dir,
        manifest_path=manifest,
        exemptions={} if exemptions is None else exemptions,
    )


def test_false_readme_complete_claim_is_finding(domain, tmp_path):
    """거짓 complete 주장 → 실측 incomplete와 대조되어 red."""
    _root, _tools, domain_dir, manifest, readme = _coverage_tree(tmp_path, domain)
    _readme(domain, readme, "complete")

    findings = _findings(domain, domain_dir, manifest)

    assert any("claim=complete, 실측=incomplete" in detail
               for _kind, _label, detail in findings)


def test_new_unowned_shipped_tool_is_finding(domain, tmp_path):
    """기존 complete fixture에 manifest 출하 파일을 추가하면 coverage gap으로 red."""
    root, tools_dir, domain_dir, manifest, readme = _coverage_tree(
        tmp_path, domain, shipped=("alpha.py",)
    )
    _readme(domain, readme, "complete")
    assert _findings(domain, domain_dir, manifest) == []

    (tools_dir / "new_tool.py").write_text("# new\n", encoding="utf-8")
    with manifest.open("a", encoding="utf-8") as fh:
        fh.write((Path(".project_manager") / "tools" / "new_tool.py").as_posix() + "\n")
    subprocess.run(
        ["git", "-C", str(root), "add", "-f", ".project_manager/tools/new_tool.py"],
        check=True,
    )

    findings = _findings(domain, domain_dir, manifest)
    assert any(label == "new_tool.py" and "담당 covers" in detail
               for _kind, label, detail in findings)


@pytest.mark.parametrize(
    ("shape", "reason"),
    [
        ("missing", "디렉토리 없음"),
        ("empty", "디렉토리 비어 있음"),
        ("template", "_template.md만 존재"),
    ],
)
def test_page_zero_shapes_skip_with_visible_reason(
        domain, tmp_path, shape, reason, capsys):
    """채택자 wiki 부재 3형상은 crash/조용한 green 없이 사유가 있는 graceful skip."""
    domain_dir = tmp_path / ".project_manager" / "wiki" / "domain"
    if shape != "missing":
        domain_dir.mkdir(parents=True)
    if shape == "template":
        (domain_dir / "_template.md").write_text("# template\n", encoding="utf-8")

    findings = domain.coverage_findings([], domain_dir=domain_dir)

    assert findings == []
    err = capsys.readouterr().err
    assert "coverage 검사 skip" in err and reason in err


def test_complete_claim_with_zero_pages_is_finding(domain, tmp_path):
    """complete claim이 활성인 트리는 page-0이어도 skip으로 공짜 통과하지 않는다."""
    _root, _tools, domain_dir, manifest, readme = _coverage_tree(
        tmp_path, domain, shipped=("alpha.py",), with_page=False
    )
    _readme(domain, readme, "complete")

    report = domain.coverage_report(
        [], domain_dir=domain_dir, manifest_path=manifest, exemptions={}
    )
    findings = domain.lint_pages(
        [], domain_dir=domain_dir, manifest_path=manifest
    )

    assert report["status"] == "error"
    assert findings
    assert any("claim 활성인데 coverage 집계 불가" in detail
               for _kind, _label, detail in findings)


def test_unclaimed_zero_page_tree_has_no_coverage_finding(domain, tmp_path):
    """marker 없는 채택자의 page-0은 기존 opt-out 경계대로 finding 0."""
    _root, _tools, domain_dir, manifest, readme = _coverage_tree(
        tmp_path, domain, shipped=("alpha.py",), with_page=False
    )
    readme.write_text("# 우리 프로젝트 domain\n", encoding="utf-8")

    report = domain.coverage_report(
        [], domain_dir=domain_dir, manifest_path=manifest, exemptions={}
    )

    assert report["status"] == "skipped"
    assert domain.lint_pages(
        [], domain_dir=domain_dir, manifest_path=manifest
    ) == []


def test_active_claim_turns_any_future_skipped_report_into_finding(
        domain, tmp_path, monkeypatch):
    """새 skip 사유가 추가돼도 claim 활성 + skipped → finding 불변식을 지킨다."""
    _root, _tools, domain_dir, manifest, readme = _coverage_tree(
        tmp_path, domain, shipped=("alpha.py",)
    )
    _readme(domain, readme, "complete")
    monkeypatch.setattr(
        domain,
        "coverage_report",
        lambda *args, **kwargs: {
            "status": "skipped",
            "reason": "미래에 추가된 임의 skip 사유",
        },
    )

    findings = _findings(domain, domain_dir, manifest)

    assert findings
    assert any("미래에 추가된 임의 skip 사유" in detail
               for _kind, _label, detail in findings)


def test_exemption_requires_nonempty_reason_and_must_be_current_gap(domain, tmp_path):
    """면제 schema는 {출하 경로: 비어 있지 않은 사유}; 빈/stale 등재는 red."""
    _root, _tools, domain_dir, manifest, readme = _coverage_tree(tmp_path, domain)
    _readme(domain, readme, "complete")
    beta = (Path(".project_manager") / "tools" / "beta.py").as_posix()
    alpha = (Path(".project_manager") / "tools" / "alpha.py").as_posix()

    assert _findings(
        domain, domain_dir, manifest, exemptions={beta: "순수 생성기 상수 모듈"}
    ) == []
    empty = _findings(domain, domain_dir, manifest, exemptions={beta: "  "})
    stale = _findings(domain, domain_dir, manifest, exemptions={alpha: "과거 사유"})

    assert any("면제 사유가 비어 있음" in detail for _k, _l, detail in empty)
    assert any("stale 면제" in detail for _k, _l, detail in stale)


def test_exemption_outside_manifest_is_finding(domain, tmp_path):
    """manifest 밖 면제를 허용하는 mutation은 명시 finding 단언으로 red."""
    _root, _tools, domain_dir, manifest, readme = _coverage_tree(tmp_path, domain)
    _readme(domain, readme, "incomplete")

    findings = _findings(
        domain,
        domain_dir,
        manifest,
        exemptions={".project_manager/tools/not_shipped.py": "로컬 도구"},
    )

    assert any("engine.manifest 출하 도구가 아님" in detail
               for _kind, _label, detail in findings)


def test_invalid_exemption_never_hides_its_gap(domain, tmp_path):
    """valid_exemptions 대신 declared를 빼는 mutation은 beta gap 부재로 red."""
    _root, _tools, domain_dir, manifest, readme = _coverage_tree(tmp_path, domain)
    _readme(domain, readme, "incomplete")
    beta = ".project_manager/tools/beta.py"

    report = domain.coverage_report(
        domain.load_pages(domain_dir),
        domain_dir=domain_dir,
        manifest_path=manifest,
        exemptions={beta: ""},
    )

    assert beta in report["gaps"]
    assert any("면제 사유가 비어 있음" in detail
               for detail in report["exemption_errors"])


def test_manifest_is_denominator_not_unshipped_tools_directory(domain, tmp_path):
    """tools/에만 있고 manifest 밖인 파일은 출하 분모가 아니다."""
    _root, tools_dir, domain_dir, manifest, readme = _coverage_tree(
        tmp_path, domain, shipped=("alpha.py",)
    )
    (tools_dir / "local_only.py").write_text("# adopter local\n", encoding="utf-8")
    _readme(domain, readme, "complete")

    report = domain.coverage_report(
        domain.load_pages(domain_dir),
        domain_dir=domain_dir,
        manifest_path=manifest,
        exemptions={},
    )

    assert report["status"] == "complete"
    assert all(not path.endswith("local_only.py") for path in report["tools"])


def test_manifest_nested_python_entry_is_not_engine_tool(domain, tmp_path):
    """tools_parent 직계 필터를 제거하는 mutation은 nested.py가 분모에 들어와 red."""
    root, _tools, domain_dir, manifest, readme = _coverage_tree(
        tmp_path,
        domain,
        shipped=("alpha.py",),
        manifest_entries=(
            ".project_manager/tools/alpha.py",
            ".project_manager/tools/nested/helper.py",
        ),
    )
    nested = root / ".project_manager" / "tools" / "nested"
    nested.mkdir()
    (nested / "helper.py").write_text("# helper\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(root), "add", "-f", ".project_manager/tools/nested/helper.py"],
        check=True,
    )
    _readme(domain, readme, "complete")

    report = domain.coverage_report(
        domain.load_pages(domain_dir),
        domain_dir=domain_dir,
        manifest_path=manifest,
        exemptions={},
    )

    assert report["status"] == "complete"
    assert report["tools"] == [".project_manager/tools/alpha.py"]


def test_empty_manifest_denominator_is_error_not_complete(domain, tmp_path):
    """빈/해석불가 manifest 분모는 공허한 complete가 아니라 error."""
    _root, _tools, domain_dir, manifest, readme = _coverage_tree(
        tmp_path, domain, shipped=("alpha.py",), manifest_entries=()
    )
    _readme(domain, readme, "complete")

    report = domain.coverage_report(
        domain.load_pages(domain_dir),
        domain_dir=domain_dir,
        manifest_path=manifest,
        exemptions={},
    )

    assert report["status"] == "error"
    assert "분모가 0" in report["reason"]


def test_empty_tracked_directory_inventory_is_error_not_complete(domain, tmp_path):
    """디렉토리 선언이 있어도 tracked 출하 파일 0이면 공허한 complete가 아니다."""
    _root, _tools, domain_dir, manifest, readme = _coverage_tree(
        tmp_path,
        domain,
        shipped=(),
        manifest_entries=(".project_manager/tools/",),
    )
    _readme(domain, readme, "complete")

    report = domain.coverage_report(
        domain.load_pages(domain_dir),
        domain_dir=domain_dir,
        manifest_path=manifest,
        exemptions={},
    )

    assert report["status"] == "error"
    assert "출하 인벤토리가 0건" in report["reason"]


def test_manifest_tools_directory_expands_to_python_denominator(domain, tmp_path):
    """manifest 디렉토리 엔트리는 직계 Python 도구로 전개되고 gap을 숨기지 않는다."""
    _root, _tools, domain_dir, manifest, readme = _coverage_tree(
        tmp_path,
        domain,
        manifest_entries=(".project_manager/tools/",),
    )
    _readme(domain, readme, "incomplete")

    report = domain.coverage_report(
        domain.load_pages(domain_dir),
        domain_dir=domain_dir,
        manifest_path=manifest,
        exemptions={},
    )

    assert report["tools"] == [
        ".project_manager/tools/alpha.py",
        ".project_manager/tools/beta.py",
    ]
    assert report["status"] == "incomplete"
    assert report["gaps"] == [".project_manager/tools/beta.py"]


def test_manifest_directory_excludes_untracked_and_ignored_python_files(
        domain, tmp_path):
    """디렉토리 분모는 update의 tracked 출하 목록이라 disk-only Python을 포함하지 않는다."""
    root, tools_dir, domain_dir, manifest, readme = _coverage_tree(
        tmp_path,
        domain,
        shipped=("alpha.py",),
        manifest_entries=(".project_manager/tools/",),
    )
    (tools_dir / "untracked.py").write_text("# local\n", encoding="utf-8")
    (tools_dir / "ignored.py").write_text("# ignored\n", encoding="utf-8")
    (root / ".gitignore").write_text(
        ".project_manager/tools/ignored.py\n", encoding="utf-8"
    )
    subprocess.run(
        ["git", "-C", str(root), "add", ".gitignore"],
        check=True,
    )
    _readme(domain, readme, "complete")

    report = domain.coverage_report(
        domain.load_pages(domain_dir),
        domain_dir=domain_dir,
        manifest_path=manifest,
        exemptions={},
    )

    assert report["status"] == "complete"
    assert report["tools"] == [".project_manager/tools/alpha.py"]


def test_manifest_directory_uses_source_remap_shipping_inventory(domain, tmp_path):
    """@source 디렉토리는 canonical source를 열거하고 manifest 목적지로 분모를 리매핑한다."""
    root, _tools, domain_dir, manifest, readme = _coverage_tree(
        tmp_path,
        domain,
        shipped=("alpha.py",),
        manifest_entries=(
            ".project_manager/tools/ @source=canonical/project-tools",
        ),
    )
    source = root / "canonical" / "project-tools"
    source.mkdir(parents=True)
    (source / "alpha.py").write_text("# canonical\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(root), "add", "canonical/project-tools/alpha.py"],
        check=True,
    )
    _readme(domain, readme, "complete")

    report = domain.coverage_report(
        domain.load_pages(domain_dir),
        domain_dir=domain_dir,
        manifest_path=manifest,
        exemptions={},
    )

    assert report["status"] == "complete"
    assert report["tools"] == [".project_manager/tools/alpha.py"]


@pytest.mark.parametrize("flavor", ["claude_code", "codex", "opencode"])
def test_flavor_manifest_engine_tool_paths_ignore_unrelated_upstream_sources(
        domain, flavor):
    """채택자 flavor의 upstream-only @source가 도구 분모 해석을 깨지 않는다."""
    manifest = REPO / "templates" / flavor / ".project_manager" / "engine.manifest"

    assert domain.engine_tool_paths(manifest) == domain.engine_tool_paths(
        REPO / ".project_manager" / "engine.manifest"
    )


def test_missing_target_owned_tool_is_not_shipping_denominator(domain, tmp_path):
    """source가 없는 @target-owned 항목은 실제 update처럼 전파 제외되고 분모에도 없다."""
    _root, _tools, domain_dir, manifest, readme = _coverage_tree(
        tmp_path,
        domain,
        shipped=("alpha.py",),
        manifest_entries=(
            ".project_manager/tools/alpha.py",
            ".project_manager/tools/adopter_only.py @target-owned",
        ),
    )
    _readme(domain, readme, "complete")

    report = domain.coverage_report(
        domain.load_pages(domain_dir),
        domain_dir=domain_dir,
        manifest_path=manifest,
        exemptions={},
    )

    assert report["status"] == "complete"
    assert report["tools"] == [".project_manager/tools/alpha.py"]


def test_missing_engine_source_makes_denominator_error(domain, tmp_path):
    """non-target-owned source 누락은 update 중단과 같이 coverage도 해석불가 error다."""
    _root, _tools, domain_dir, manifest, readme = _coverage_tree(
        tmp_path,
        domain,
        shipped=("alpha.py",),
        manifest_entries=(
            ".project_manager/tools/alpha.py",
            ".project_manager/tools/missing.py",
        ),
    )
    _readme(domain, readme, "complete")

    report = domain.coverage_report(
        domain.load_pages(domain_dir),
        domain_dir=domain_dir,
        manifest_path=manifest,
        exemptions={},
    )

    assert report["status"] == "error"
    assert "non-@target-owned manifest source" in report["reason"]


def test_manifest_paths_are_parsed_by_canonical_pm_update_reader(domain, tmp_path):
    """공백을 포함한 path도 pm_update.read_manifest 의미론 그대로 분모에 들어간다."""
    _root, _tools, domain_dir, manifest, readme = _coverage_tree(
        tmp_path,
        domain,
        shipped=("alpha tool.py",),
    )
    page = domain_dir / "engine.md"
    page.write_text(
        page.read_text(encoding="utf-8").replace("alpha.py", "alpha tool.py"),
        encoding="utf-8",
    )
    _readme(domain, readme, "complete")

    report = domain.coverage_report(
        domain.load_pages(domain_dir),
        domain_dir=domain_dir,
        manifest_path=manifest,
        exemptions={},
    )

    assert report["status"] == "complete"
    assert report["tools"] == [".project_manager/tools/alpha tool.py"]


def test_engine_tool_denominator_consumes_manifest_reader_seam(
        domain, tmp_path, monkeypatch):
    """분모 계산이 자체 line parser로 회귀하지 않고 canonical reader seam을 소비한다."""
    _root, _tools, domain_dir, manifest, readme = _coverage_tree(
        tmp_path,
        domain,
        shipped=("alpha.py", "decoy.py"),
        manifest_entries=(".project_manager/tools/decoy.py",),
    )
    _readme(domain, readme, "complete")
    calls = []

    def fake_read_manifest(path):
        calls.append(path)
        return [".project_manager/tools/alpha.py"]

    monkeypatch.setattr(domain, "_load_manifest_reader", lambda: fake_read_manifest)

    report = domain.coverage_report(
        domain.load_pages(domain_dir),
        domain_dir=domain_dir,
        manifest_path=manifest,
        exemptions={},
    )

    assert calls == [manifest]
    assert report["status"] == "complete"
    assert report["tools"] == [".project_manager/tools/alpha.py"]


@pytest.mark.parametrize("command_shape", ["missing", "duplicate"])
def test_coverage_command_reference_must_appear_once(
        domain, tmp_path, command_shape):
    """README 커맨드 1회 검사를 지우는 mutation은 두 형상 모두 red."""
    _root, _tools, domain_dir, manifest, readme = _coverage_tree(
        tmp_path, domain, shipped=("alpha.py",)
    )
    _readme(domain, readme, "complete")
    body = readme.read_text(encoding="utf-8")
    if command_shape == "missing":
        body = body.replace(f"`{domain.COVERAGE_COMMAND}`\n\n", "")
    else:
        body += f"\n`{domain.COVERAGE_COMMAND}`\n"
    readme.write_text(body, encoding="utf-8")

    findings = _findings(domain, domain_dir, manifest)

    assert any("같은 표기를 반복하지 말 것" in detail
               for _kind, _label, detail in findings)


def test_windows_and_posix_coverage_commands_may_be_documented_together(
        domain, tmp_path):
    """서로 다른 허용 launcher 병기는 반복으로 오인하지 않는다."""
    _root, _tools, domain_dir, manifest, readme = _coverage_tree(
        tmp_path, domain, shipped=("alpha.py",)
    )
    _readme(domain, readme, "complete")
    with readme.open("a", encoding="utf-8") as fh:
        fh.write(
            f"\nPOSIX: `{domain.COVERAGE_COMMAND_ALTERNATES[0]}`\n"
            f"venv: `{domain.COVERAGE_COMMAND_ALTERNATES[1]}`\n"
        )

    assert _findings(domain, domain_dir, manifest) == []


@pytest.mark.parametrize("claim_shape", ["missing", "duplicate", "typo"])
def test_claim_marker_must_be_exactly_one_valid_value(
        domain, tmp_path, claim_shape):
    """marker 개수·값 검사를 지우는 mutation은 부재·중복·오타 각각 red."""
    _root, _tools, domain_dir, manifest, readme = _coverage_tree(
        tmp_path, domain, shipped=("alpha.py",)
    )
    _readme(domain, readme, "complete")
    body = readme.read_text(encoding="utf-8")
    marker = "<!-- domain-coverage-claim: complete -->"
    if claim_shape == "missing":
        body = body.replace(marker, "")
    elif claim_shape == "duplicate":
        body += f"\n{marker}\n"
    else:
        body = body.replace("complete -->", "complet -->")
    readme.write_text(body, encoding="utf-8")

    findings = _findings(domain, domain_dir, manifest)

    assert any("claim marker를 정확히 1개" in detail
               for _kind, _label, detail in findings)


@pytest.mark.parametrize(
    ("markers", "clean"),
    [
        (
            "<!-- domain-coverage-claim: complete -->\n"
            "<!-- domain-coverage-claim: complet -->",
            False,
        ),
        (
            "<!-- domain-coverage-claim: complete -->\n"
            "<!-- domain-coverage-claim: complete -->",
            False,
        ),
        ("<!-- domain-coverage-claim: complete -->", True),
        ("<!-- domain-coverage-claim: complet -->", False),
    ],
    ids=("valid-plus-typo", "two-valid", "one-valid", "typo-only"),
)
def test_claim_marker_counts_all_intent_before_validating_value(
        domain, tmp_path, markers, clean):
    """유효 marker 하나에 무효 intent를 섞어 claim 검사를 우회할 수 없다."""
    _root, _tools, domain_dir, manifest, readme = _coverage_tree(
        tmp_path, domain, shipped=("alpha.py",)
    )
    _readme(domain, readme, "complete")
    body = readme.read_text(encoding="utf-8")
    body = body.replace("<!-- domain-coverage-claim: complete -->", markers)
    readme.write_text(body, encoding="utf-8")

    findings = _findings(domain, domain_dir, manifest)

    if clean:
        assert findings == []
    else:
        assert any("claim marker를 정확히 1개" in detail
                   for _kind, _label, detail in findings)


def test_marker_and_command_inside_fence_are_examples_not_claim(domain, tmp_path, monkeypatch):
    """fenced 예시는 opt-in도 command 충족도 하지 않아 inline/fence 판정이 대칭이다."""
    root, _tools, domain_dir, manifest, readme = _coverage_tree(
        tmp_path, domain, shipped=("alpha.py",)
    )
    readme.write_text(
        "# 예시\n\n"
        "```md\n"
        f"`{domain.COVERAGE_COMMAND}`\n"
        "<!-- domain-coverage-claim: complete -->\n"
        "```\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(domain, "REPO", root)
    monkeypatch.setattr(domain, "DOMAIN_DIR", domain_dir)
    monkeypatch.setattr(domain, "ENGINE_MANIFEST", manifest)
    monkeypatch.setattr(domain, "page_stale", lambda _page, **_kwargs: None)

    pages = domain.load_pages(domain_dir)
    assert domain.lint_pages(pages) == []
    explicit = domain.coverage_findings(
        pages, domain_dir=domain_dir, manifest_path=manifest, exemptions={}
    )
    assert any("inline-code로 하나 이상" in detail
               for _kind, _label, detail in explicit)
    assert any("claim marker를 정확히 1개" in detail
               for _kind, _label, detail in explicit)


@pytest.mark.parametrize("marker_shape", ["deleted", "fenced"])
def test_numeric_prose_lint_cannot_be_disabled_by_marker_shape(
        domain, tmp_path, monkeypatch, marker_shape):
    """marker 삭제/예시 fence 이동 모두 원 결함 수치 산문 lint를 끄지 못한다."""
    root, _tools, domain_dir, manifest, readme = _coverage_tree(
        tmp_path, domain, shipped=("alpha.py",)
    )
    _readme(domain, readme, "complete")
    marker = "<!-- domain-coverage-claim: complete -->"
    body = readme.read_text(encoding="utf-8").replace(marker, "")
    if marker_shape == "fenced":
        body += f"\n```md\n{marker}\n```\n"
    body += "\n엔진 17 도구 전부 담당 페이지가 있다. 면제 = 0.\n"
    readme.write_text(body, encoding="utf-8")
    monkeypatch.setattr(domain, "REPO", root)
    monkeypatch.setattr(domain, "DOMAIN_DIR", domain_dir)
    monkeypatch.setattr(domain, "ENGINE_MANIFEST", manifest)
    monkeypatch.setattr(domain, "page_stale", lambda _page, **_kwargs: None)

    findings = domain.lint_pages(domain.load_pages(domain_dir))

    assert findings == [(
        "coverage",
        "README.md",
        "수치형 coverage 산문 금지 — 도구·면제·gap 수는 집계 커맨드 결과를 가리킬 것",
    )]


def test_markerless_adopter_domain_does_not_join_engine_coverage(
        domain, tmp_path, monkeypatch):
    """자기 코드 page가 있는 fresh adopter도 marker 미주장이면 coverage finding 0."""
    root, _tools, domain_dir, manifest, readme = _coverage_tree(tmp_path, domain)
    readme.write_text(
        "# 우리 프로젝트 domain\n\n"
        "우리 결제 도구는 외부 승인과 내부 원장을 함께 기록합니다.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(domain, "REPO", root)
    monkeypatch.setattr(domain, "DOMAIN_DIR", domain_dir)
    monkeypatch.setattr(domain, "ENGINE_MANIFEST", manifest)
    monkeypatch.setattr(domain, "page_stale", lambda _page, **_kwargs: None)

    findings = domain.lint_pages(domain.load_pages(domain_dir))

    assert not any(kind == "coverage" for kind, _label, _detail in findings)


@pytest.mark.parametrize(
    "adopter_prose",
    [
        "보험료 면제는 0.5%입니다.",
        "수수료 면제 = 0원입니다.",
        "커버리지 목표는 12 = 8 + 4입니다.",
    ],
)
def test_markerless_adopter_numeric_domain_prose_is_not_coverage_finding(
        domain, tmp_path, monkeypatch, adopter_prose):
    """marker 없는 채택자의 일반 도메인 수치 산문은 엔진 coverage advisory가 아니다."""
    root, _tools, domain_dir, manifest, readme = _coverage_tree(tmp_path, domain)
    readme.write_text(
        f"# 우리 프로젝트 domain\n\n{adopter_prose}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(domain, "REPO", root)
    monkeypatch.setattr(domain, "DOMAIN_DIR", domain_dir)
    monkeypatch.setattr(domain, "ENGINE_MANIFEST", manifest)
    monkeypatch.setattr(domain, "page_stale", lambda _page, **_kwargs: None)

    findings = domain.lint_pages(domain.load_pages(domain_dir))

    assert not any(kind == "coverage" for kind, _label, _detail in findings)


def test_mixed_page_and_manifest_trees_skip_coverage_with_reason(
        domain, tmp_path, capsys):
    """claim 트리와 다른 출처 pages를 섞으면 skip 대신 명시 finding이다."""
    _root_a, _tools_a, domain_a, manifest_a, readme_a = _coverage_tree(
        tmp_path / "a", domain, shipped=("alpha.py",)
    )
    _readme(domain, readme_a, "complete")
    _root_b, _tools_b, domain_b, _manifest_b, _readme_b = _coverage_tree(
        tmp_path / "b", domain, shipped=("alpha.py",)
    )

    findings = domain.coverage_findings(
        domain.load_pages(domain_b),
        domain_dir=domain_a,
        manifest_path=manifest_a,
        exemptions={},
    )

    assert any("pages 출처가 domain 트리와 불일치" in detail
               for _kind, _label, detail in findings)
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "numeric_claim",
    [
        "엔진 17 도구 전부 커버합니다.",
        "면제 = 0건입니다.",
        "coverage 합계는 17 = 17 + 0 입니다.",
        "면제는 0건이다.",
        "엔진 도구 17개 전부 커버합니다.",
        "**면제(exemption) = 0.**",
        "10 = 9 concept/guide + 1 research",
    ],
)
def test_numeric_coverage_prose_is_finding(
        domain, tmp_path, numeric_claim):
    """사람이 읽는 하드코딩 수치 주장은 marker가 맞아도 red."""
    _root, _tools, domain_dir, manifest, readme = _coverage_tree(
        tmp_path, domain, shipped=("alpha.py",)
    )
    _readme(domain, readme, "complete")
    with readme.open("a", encoding="utf-8") as fh:
        fh.write(f"\n{numeric_claim}\n")

    findings = _findings(domain, domain_dir, manifest)

    assert any("수치형 coverage 산문 금지" in detail
               for _kind, _label, detail in findings)


@pytest.mark.parametrize(
    "allowed_prose",
    [
        "| 면제 | 0 |",
        "엔진 열일곱 도구 전부 커버합니다.",
        "면제 수는 다음 문장에서 밝힙니다.\n0건입니다.",
        "우선순위 계산은 3 = 1 + 2 규칙을 쓴다.",
    ],
)
def test_numeric_prose_lint_documented_boundaries_are_not_findings(
        domain, tmp_path, allowed_prose):
    """표·한글 수사·문장 분할은 좁은 lint 경계이며 일반 산술식은 오탐하지 않는다."""
    _root, _tools, domain_dir, manifest, readme = _coverage_tree(
        tmp_path, domain, shipped=("alpha.py",)
    )
    _readme(domain, readme, "complete")
    with readme.open("a", encoding="utf-8") as fh:
        fh.write(f"\n{allowed_prose}\n")

    assert _findings(domain, domain_dir, manifest) == []


def test_qualitative_coverage_prose_has_no_false_positive(domain, tmp_path):
    """숫자를 복제하지 않는 정상 설명은 산문 lint 오탐 0."""
    _root, _tools, domain_dir, manifest, readme = _coverage_tree(
        tmp_path, domain, shipped=("alpha.py",)
    )
    _readme(domain, readme, "complete")
    with readme.open("a", encoding="utf-8") as fh:
        fh.write("\n엔진 도구와 면제 여부는 위 집계 커맨드 결과로 확인합니다.\n")

    assert _findings(domain, domain_dir, manifest) == []


def test_coverage_command_output_is_machine_derived(domain, tmp_path, monkeypatch, capsys):
    _root, _tools_dir, domain_dir, manifest, _readme_path = _coverage_tree(tmp_path, domain)
    monkeypatch.setattr(domain, "DOMAIN_DIR", domain_dir)
    monkeypatch.setattr(domain, "ENGINE_MANIFEST", manifest)
    monkeypatch.setattr(domain, "DOMAIN_COVERAGE_EXEMPTIONS", {})

    assert domain.main(["coverage"]) == 0

    assert capsys.readouterr().out == (
        "domain coverage (denominator: engine.manifest shipped Python tools)\n"
        "  claim: none (이 트리는 coverage 미주장; 아래 집계는 진단 전용)\n"
        "  tools: 2\n"
        "  covered: 1\n"
        "  exempt: 0\n"
        "  gaps: 1\n"
        "  status: incomplete\n"
        "  coverage gaps:\n"
        "    .project_manager/tools/beta.py\n"
    )


def test_coverage_finding_flows_through_board_and_gate_stays_zero(
        domain, tmp_path, monkeypatch):
    """실 domain fixture finding이 board deep-import seam을 지나도 --gate는 rc 0."""
    root, _tools, domain_dir, manifest, readme = _coverage_tree(tmp_path, domain)
    _readme(domain, readme, "complete")
    monkeypatch.setattr(domain, "REPO", root)
    monkeypatch.setattr(domain, "DOMAIN_DIR", domain_dir)
    monkeypatch.setattr(domain, "ENGINE_MANIFEST", manifest)
    monkeypatch.setattr(domain, "DOMAIN_COVERAGE_EXEMPTIONS", {})
    monkeypatch.setattr(domain, "page_stale", lambda _page, **_kwargs: None)

    board = _load("board_coverage_gate_test", TOOLS / "board.py")
    monkeypatch.setattr(board, "_load_domain_module", lambda: domain)
    findings = board.lint_domain()
    assert any(label == "beta.py" and kind == "coverage"
               for label, kind, _detail in findings)
    monkeypatch.setattr(board, "lint_tickets", lambda: findings)
    monkeypatch.setattr(board, "_run_lint_hooks", lambda: [])

    assert "coverage" in board._ADVISORY_LINT_KINDS
    assert board.cmd_lint(SimpleNamespace(gate=True)) == 0
    assert board.cmd_lint(SimpleNamespace(gate=False)) == 1
