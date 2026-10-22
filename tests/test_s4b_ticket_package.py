from pathlib import Path
import pytest
from pm_engine.infra import pm_paths, ticket_store, fs
from pm_engine.app import ticket_access, board_dashboard, completion_gate
from pm_engine.domain import ticket_rules

def make_home(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / '.project_manager/board/tickets/open').mkdir(parents=True)
    return pm_paths.home_at(root)

def test_ticket_write_preserves_crlf_and_body(tmp_path):
    path = tmp_path / 'T-0001.md'
    path.write_bytes(b'---\r\nid: T-0001\r\n---\r\nbody\r\n')
    fm, body = ticket_store.load_ticket(path)
    ticket_store.dump_ticket_atomic(path, fm, 'body\nnext\n')
    raw = path.read_bytes()
    assert b'\r\nbody\r\nnext\r\n' in raw
    assert b'\n' not in raw.replace(b'\r\n', b'')

def test_ticket_lookup_does_not_mix_homes(tmp_path):
    a = make_home(tmp_path / 'a'); b = make_home(tmp_path / 'b')
    pa = a.board_root / 'tickets/open/T-0042-a.md'
    pb = b.board_root / 'tickets/open/T-0042-b.md'
    ticket_store.dump_ticket(pa, {'id':'T-0042'}, 'A')
    ticket_store.dump_ticket(pb, {'id':'T-0042'}, 'B')
    assert ticket_store.find_ticket_for_mutation(a, 'T-0042')[1] == pa
    assert ticket_store.find_ticket_for_mutation(b, 'T-0042')[1] == pb

def test_unowned_ticket_keeps_deferred_task_validation(tmp_path):
    home = make_home(tmp_path / 'a')
    verdict = ticket_access._ticket_ownership(home, {}, task='../invalid', user=None)
    assert verdict.ok and not verdict.unresolved

def test_owned_ticket_without_a_task_is_rejected(tmp_path):
    home = make_home(tmp_path / 'a')
    verdict = ticket_access._ticket_ownership(home, {'claimed_by':'alice/job'}, task=None, user='alice')
    assert not verdict.ok and verdict.unresolved

def test_dod_distinguishes_checked_and_open_items():
    assert completion_gate._dod_open_items('## 완료 조건\n- [x] 완료\n') == []
    assert completion_gate._dod_open_items('## 완료 조건\n- [ ] 미완료\n')

def test_dashboard_write_uses_the_selected_home(tmp_path):
    a = make_home(tmp_path / 'a'); b = make_home(tmp_path / 'b')
    ticket_store.dump_ticket(a.board_root / 'tickets/open/T-0042-a.md', {'id':'T-0042','title':'only-a'}, '')
    code = tmp_path / 'code'; code.mkdir()
    read_home = pm_paths.home_at(code, a.root, a.board_root)
    board_dashboard.refresh_board(read_home)
    assert 'only-a' in (a.root / '.project_manager/wiki/board.md').read_text(encoding='utf-8')
    assert not (b.root / '.project_manager/wiki/board.md').exists()
    assert not (code / '.project_manager/wiki/board.md').exists()
