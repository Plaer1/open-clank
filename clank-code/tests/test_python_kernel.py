"""One disposable owner-scoped kernel lifecycle case; no live application."""
import tempfile
import threading
import time
from src.openclank.python_kernel import KernelError, KernelManager, KernelScope


def test_disposable_kernel_lifecycle():
    owner = KernelScope('account', 'workspace', 'chat', 'engine', 'main', 'holder-1', 1)
    other = KernelScope('other', 'workspace', 'chat', 'engine', 'main', 'holder-1', 1)
    manager = KernelManager()
    with tempfile.TemporaryDirectory(prefix='openclank-kernel-fixture-') as directory:
        started = manager.start(owner, directory)
        kid, generation = started['kernel_id'], started['generation']
        try:
            assert manager.execute(owner, kid, generation, 'one', 'value = 41')['status'] == 'completed'
            assert manager.execute(owner, kid, generation, 'two', 'print(value + 1)')['stdout'] == '42\n'
            try:
                manager.status(other, kid)
                raise AssertionError('foreign scope accepted')
            except KernelError:
                pass
            result = []
            thread = threading.Thread(target=lambda: result.append(manager.execute(owner, kid, generation, 'long', 'while True: pass', timeout=10)))
            thread.start()
            for _ in range(100):
                if manager.status(owner, kid)['active_cell_id'] == 'long': break
                time.sleep(.01)
            assert manager.interrupt(owner, kid, generation, 'long')['status'] == 'termination_requested'
            thread.join(5)
            assert not thread.is_alive() and result[0]['status'] == 'unknown' and not result[0]['state_survives']
            reset = manager.reset(owner, kid, generation)
            generation = reset['generation']
            lost = manager.execute(owner, kid, generation, 'after-reset', 'print(value)')
            assert lost['status'] == 'error' and 'NameError' in lost['exception']
            manager._get(owner, kid)['process'].kill()
            manager._get(owner, kid)['process'].wait()
            assert manager.status(owner, kid)['status'] == 'lost'
        finally:
            manager.close_all()
        entered, release = threading.Event(), threading.Event()
        class DelayedManager(KernelManager):
            def _launch(self, *args):
                entered.set()
                assert release.wait(5)
                return super()._launch(*args)
        delayed = DelayedManager()
        outcomes = []
        def start_late():
            try:
                delayed.start(owner, directory)
                outcomes.append('unexpected')
            except KernelError:
                outcomes.append('retired')
        launch = threading.Thread(target=start_late); launch.start(); assert entered.wait(5)
        retirement = threading.Thread(target=lambda: delayed.retire_scope(owner)); retirement.start()
        for _ in range(100):
            with delayed._lock:
                if owner in delayed._retired: break
            time.sleep(.01)
        release.set(); launch.join(5); retirement.join(5)
        assert not launch.is_alive() and not retirement.is_alive() and outcomes == ['retired']
        assert not delayed._kernels
        delayed.close_all()
