"""Bounded real TCP fixtures; these exercise handlers, not server.main admission."""
import gc
import json
import queue
import socket
import threading
import time


def frame(message):
    return (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")


def shutdown(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    sock.close()


class LoopbackServer:
    def __init__(self, handler, port=0):
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("Create loopback fixtures on the main test thread")
        # Destroyed Tk roots can remain in unreachable Python widget/callback
        # cycles after a prior test. Collect them on their owner thread before
        # network workers can trigger GC and finalize Tcl on the wrong thread.
        gc.collect()
        self.handler = handler
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", port))
        self.port = self.listener.getsockname()[1]
        self.listener.listen()
        self.listener.settimeout(.1)
        self.stopped = threading.Event()
        self.sockets = []
        self.workers = []
        self.errors = queue.Queue()
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self):
        while not self.stopped.is_set():
            try:
                conn, addr = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.sockets.append(conn)
            def run(conn=conn, addr=addr):
                try:
                    self.handler(conn, addr)
                except Exception as exc:
                    self.errors.put(exc)
                finally:
                    shutdown(conn)
            worker = threading.Thread(target=run, daemon=True)
            self.workers.append(worker)
            worker.start()

    def close(self):
        self.stopped.set()
        self.listener.close()
        self.thread.join(1)
        for sock in self.sockets:
            shutdown(sock)
        for worker in self.workers:
            worker.join(2)
        if self.thread.is_alive() or any(w.is_alive() for w in self.workers):
            raise AssertionError("Fixture TCP thread failed to stop")
        if not self.errors.empty():
            raise AssertionError(f"Fixture handler failed: {self.errors.get()!r}")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class Events:
    def __init__(self):
        self.queue = queue.Queue()
        self.seen = []

    def put(self, msg):
        self.queue.put(msg)

    def wait(self, predicate, timeout=2):
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"Missing event; received {self.seen!r}")
            try:
                msg = self.queue.get(timeout=remaining)
            except queue.Empty:
                raise AssertionError(f"Missing event; received {self.seen!r}") from None
            self.seen.append(msg)
            if predicate(msg):
                return msg

    def drain(self):
        while not self.queue.empty():
            self.seen.append(self.queue.get_nowait())
        return self.seen


def pump(root, predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError("Tk condition did not complete before deadline")
