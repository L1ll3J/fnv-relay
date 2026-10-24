"""Managed menu lifecycle with fake processes; no relay or external services."""
from unittest.mock import Mock
from fnv_relay import managed
from fnv_relay.store import Store


def test_start_is_nonblocking_uses_private_log_and_binds_menu_owner(tmp_path,monkeypatch):
    store=Store(tmp_path);child=Mock();child.pid=987654321;child.poll.return_value=None
    popen=Mock(return_value=child);monkeypatch.setattr(managed.subprocess,'Popen',popen)
    worker=managed.MenuWorker(store,lambda _:0,lambda _:None)
    worker.start()
    args,kwargs=popen.call_args
    assert args[0][:4]==[managed.sys.executable,'-u','-m','fnv_relay.cli']
    assert '--menu-owner-pid' in args[0] and '--menu-owner-start-time' in args[0]
    assert kwargs['start_new_session'] is True
    assert kwargs['stdin']==managed.subprocess.DEVNULL
    assert (tmp_path/'worker.log').stat().st_mode&0o777==0o600
    assert worker.running()
    worker.start()
    assert popen.call_count==1
    store.close()


def test_existing_controller_is_used_without_starting_a_second(tmp_path,monkeypatch):
    store=Store(tmp_path);popen=Mock();monkeypatch.setattr(managed.subprocess,'Popen',popen)
    worker=managed.MenuWorker(store,lambda _:0,lambda _:None)
    with store.lock('serve'):worker.start()
    popen.assert_not_called()
    store.close()


def test_stop_requests_graceful_cleanup_of_known_child(tmp_path):
    store=Store(tmp_path);child=Mock();child.pid=987654321;child.poll.return_value=None;calls=[]
    worker=managed.MenuWorker(store,lambda args:calls.append(args) or 0,lambda _:None);worker.process=child
    assert worker.stop()
    child.send_signal.assert_called_once_with(managed.signal.SIGTERM)
    child.wait.assert_called_once_with(timeout=1)
    assert calls==[['stop','--timeout','30']]
    store.close()



def test_exit_still_requests_cleanup_when_only_uncertain_operations_remain(tmp_path):
    store=Store(tmp_path);calls=[]
    try:store.operation('claim:lane:1',{},lambda:(_ for _ in ()).throw(RuntimeError('unknown')))
    except RuntimeError:pass
    worker=managed.MenuWorker(store,lambda args:calls.append(args) or 1,lambda _:None)
    assert worker.stop() is False
    assert calls==[['stop','--timeout','30']]
    store.close()



def test_separate_menu_does_not_start_duplicate_during_startup(tmp_path,monkeypatch):
    store=Store(tmp_path);child=Mock();child.pid=12345;child.poll.return_value=None
    popen=Mock(return_value=child);monkeypatch.setattr(managed.subprocess,'Popen',popen)
    monkeypatch.setattr(managed,'process_identity',lambda pid=None:{'pid':pid or 23456,'start_time':'synthetic'})
    monkeypatch.setattr(managed,'owner_alive',lambda owner:owner=={'pid':12345,'start_time':'synthetic'})
    first=managed.MenuWorker(store,lambda _:0,lambda _:None);second=managed.MenuWorker(store,lambda _:0,lambda _:None)
    first.start();second.start()
    assert popen.call_count==1
    store.close()



def test_terminal_termination_unwinds_menu_and_stops_worker(tmp_path):
    import pytest,signal
    from fnv_relay import interactive
    store=Store(tmp_path);worker=Mock();worker.running.return_value=True
    def terminated(_):signal.getsignal(signal.SIGTERM)(signal.SIGTERM,None)
    with pytest.raises(SystemExit):
        with managed.menu_signals():
            interactive.run(store,lambda _:0,ask=terminated,show=lambda _:None,worker=worker)
    worker.stop.assert_called_once_with()
    store.close()
