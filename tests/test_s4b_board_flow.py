import pytest
from pm_engine.infra import pm_paths, cluster_store
from pm_engine.domain import cluster_rules, board_command_rules
from pm_engine.app import engine_guard

def test_cluster_record_does_not_cross_homes(tmp_path):
    a = pm_paths.home_at(tmp_path / 'a'); b = pm_paths.home_at(tmp_path / 'b')
    cluster_store.dump_cluster(a, {'id':'C-unit','tickets':['T-0042'],'status':'open'})
    assert cluster_store.load_cluster(a, 'unit')['tickets'] == ['T-0042']
    assert cluster_store.load_cluster(b, 'unit') is None

def test_strict_cluster_read_does_not_turn_corruption_into_empty(tmp_path):
    h = pm_paths.home_at(tmp_path / 'a')
    p = cluster_store.cluster_ledger_path(h, 'unit'); p.parent.mkdir(parents=True)
    p.write_text('---\nid: [unterminated\n---\n', encoding='utf-8')
    rows, error = cluster_store.all_clusters_strict(h)
    assert rows == [] and error and '전수 확인' in error

def test_command_classifications_stay_disjoint():
    m=board_command_rules._MUTATION_SUBCOMMANDS
    r=board_command_rules._READ_SUBCOMMANDS
    s=board_command_rules._SIDECAR_SUBCOMMANDS
    assert not (m&r or m&s or r&s)
    assert 'claim' in m and 'show' in r and 'git-anchor' in s

def test_no_todo_warning_without_a_ledger_keeps_no_judgment(tmp_path):
    h=pm_paths.home_at(tmp_path / 'a')
    assert engine_guard._no_open_todo_judgment(h, 'session') is None
