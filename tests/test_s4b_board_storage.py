from pathlib import Path
import contextlib
import pytest
from pm_engine.infra import pm_paths, fs, git
from pm_engine.app import areas_registry
from pm_engine.domain import areas_rules

def make_home(root, rows):
    root.mkdir(parents=True)
    home = pm_paths.home_at(root)
    home.board_root.mkdir(parents=True)
    (root / '.project_manager/areas.md').write_text('| repo | prefix | area_owner | git | base |\n|---|---|---|---|---|\n'+rows, encoding='utf-8')
    return home

def test_self_row_uses_target_data_without_flat_engine(tmp_path, monkeypatch):
    home = make_home(tmp_path / 'target', '| target | x | | | |\n')
    def run(repo, argv):
        assert repo == home.root
        if argv[0] == 'rev-parse': return 0, str(home.root)
        if argv[0] == 'symbolic-ref': return 0, 'refs/heads/main'
        return 0, 'git@example.test:target.git'
    monkeypatch.setattr(git, '_git_scope_run', run)
    assert not (home.root / '.project_manager/tools/board.py').exists()
    before = (home.root / '.project_manager/areas.md').read_bytes()
    assert 'base=main' in areas_registry.fill_self_repo_row(home, write=False)
    assert not home.local_dir.exists()
    assert (home.root / '.project_manager/areas.md').read_bytes() == before
    assert 'base=main' in areas_registry.fill_self_repo_row(home, write=True)
    assert 'git@example.test:target.git' in (home.root / '.project_manager/areas.md').read_text(encoding='utf-8')

def test_self_row_keeps_partly_filled_row(tmp_path, monkeypatch):
    home = make_home(tmp_path / 'target', '| target | x | | supplied | |\n')
    before = (home.root / '.project_manager/areas.md').read_bytes()
    monkeypatch.setattr(git, '_git_scope_run', lambda *args: pytest.fail('already filled'))
    assert areas_registry.fill_self_repo_row(home, write=True) is None
    assert (home.root / '.project_manager/areas.md').read_bytes() == before

def test_self_row_duplicate_is_loud(tmp_path):
    home = make_home(tmp_path / 'target', '| target | x | | | |\n| target | y | | | |\n')
    with pytest.raises(areas_rules.AreasDuplicateRepo):
        areas_registry.fill_self_repo_row(home, write=True)

def test_board_lock_uses_home_local_dir(tmp_path, monkeypatch):
    a = pm_paths.home_at(tmp_path / 'a'); b = pm_paths.home_at(tmp_path / 'b'); seen=[]
    @contextlib.contextmanager
    def lock(path):
        seen.append(path)
        yield
    monkeypatch.setattr(fs, 'exclusive_file_lock', lock)
    with fs.board_lock(a): pass
    with fs.board_lock(b): pass
    assert seen == [a.local_dir / 'board.lock', b.local_dir / 'board.lock']

def test_freshness_formatter_is_independent_of_flat_engine():
    from pm_engine.domain import git_freshness
    assert git_freshness.parse_git_ahead_behind('2\t3') == (2,3)
    assert git_freshness.parse_git_ahead_behind('broken') is None
    assert '최신' in git_freshness._format_freshness({'fetched':True,'detached':False,'dirty':False,'ahead':0,'behind':0,'note':None})
