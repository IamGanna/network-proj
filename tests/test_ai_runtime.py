import threading
import time
import unittest
from unittest.mock import Mock
from ai_runtime import AiRuntime


class RuntimeTests(unittest.TestCase):
    def test_bounded_running_jobs_and_capacity_after_exception(self):
        gate, entered = threading.Event(), [threading.Event(), threading.Event()]
        runtime = AiRuntime(Mock())
        try:
            def job(i):
                entered[i].set()
                gate.wait(2)
                if i == 0: raise ValueError('private error')
            self.assertTrue(runtime.try_submit(lambda: job(0)))
            self.assertTrue(runtime.try_submit(lambda: job(1)))
            self.assertTrue(all(e.wait(1) for e in entered))
            self.assertFalse(runtime.try_submit(lambda: None))
            gate.set()
            done = threading.Event()
            deadline = time.monotonic()+2
            while not runtime.try_submit(done.set) and time.monotonic()<deadline:
                time.sleep(.005)
            self.assertTrue(done.wait(1))
        finally:
            gate.set()
            runtime.close()
        self.assertFalse(runtime.try_submit(lambda: None))
        runtime.close()
        runtime.service.close.assert_called_once()
