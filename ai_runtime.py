"""Fixed workers and nonblocking admission, bounded across running and queued jobs."""
import queue
import threading
import time


class AiRuntime:
    def __init__(self, service, *, max_jobs=2, workers=2):
        self.service = service
        self._queue = queue.Queue(maxsize=max_jobs)
        self._slots = threading.BoundedSemaphore(max_jobs)
        self._lock = threading.Lock()
        self._closed = False
        self.stopped = threading.Event()
        self._running = {}
        self._next_id = 0
        self._workers = [threading.Thread(target=self._run, daemon=True, name='chat-ai')
                         for _ in range(workers)]
        for worker in self._workers:
            worker.start()

    def try_submit(self, job, *, on_cancel=None):
        with self._lock:
            if self._closed or not self._slots.acquire(blocking=False):
                return False
            self._next_id += 1
            self._queue.put_nowait((self._next_id, job, on_cancel))
            return True

    def _run(self):
        while True:
            with self._lock:
                if self._closed:
                    return
                try:
                    item = self._queue.get_nowait()
                    self._running[item[0]] = item[2]
                except queue.Empty:
                    item = None
            if item is None:
                time.sleep(.02)
                continue
            try:
                item[1]()
            except Exception:
                # Jobs own safe user-facing errors. Never log provider contents.
                pass
            finally:
                with self._lock:
                    self._running.pop(item[0], None)
                self._slots.release()

    def close(self):
        deadline = time.monotonic() + 2
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self.stopped.set()
            cancelled = list(self._running.values())
            while True:
                try:
                    cancelled.append(self._queue.get_nowait()[2])
                    self._slots.release()
                except queue.Empty:
                    break
        for callback in cancelled:
            if callback is not None:
                callback()
        self.service.close()  # Transport interrupts sockets; worker owns buffered close.
        for worker in self._workers:
            worker.join(max(0, deadline - time.monotonic()))
