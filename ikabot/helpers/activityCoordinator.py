"""Cross-process action queue, with foreground web actions taking priority.

SQLite stores tickets and cooldowns; an OS file lock protects the actual action.
The OS releases the lock on process death. A live action is never evicted by age.
All instances must use the same directory (default: ~/.ikabot/activity).
"""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import inspect
import os
from pathlib import Path
import random
import sqlite3
import threading
import time
from urllib.parse import parse_qsl, urlsplit
import uuid
import weakref

import psutil

from ikabot import config


_active = ContextVar("ikabot_active_operation", default=None)
_foreground = ContextVar("ikabot_foreground_request", default=False)
_mutexes = weakref.WeakSet()


class ActivityTimeout(TimeoutError):
    """The action was not started because coordination could not be acquired."""


def operation_active():
    owner = _active.get()
    return owner is not None and owner == (os.getpid(), threading.get_ident())


class _FileMutex:
    def __init__(self, path):
        self.file = open(path, "a+b")
        if os.fstat(self.file.fileno()).st_size == 0:
            self.file.write(b"0")
            self.file.flush()
        self.locked = False
        _mutexes.add(self)

    def acquire(self):
        try:
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.locked = True
            return True
        except (BlockingIOError, PermissionError):
            return False
        except OSError as exc:
            if exc.errno in (11, 13, 36):
                return False
            raise

    def release(self):
        if self.locked:
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            self.locked = False

    def close(self):
        try:
            self.release()
        finally:
            self.file.close()


def _after_fork():
    # Closing a duplicated descriptor is safe; explicitly unlocking it here
    # would also unlock the parent's flock. Spawn-based Windows needs no hook.
    for mutex in list(_mutexes):
        mutex.locked = False
        mutex.file.close()
    _active.set(None)
    _foreground.set(False)


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


class ActivityCoordinator:
    def __init__(self, directory=None, manual_idle=None, gap=None, poll=0.2):
        self.directory = Path(directory or config.ACTIVITY_COORDINATOR_DIRECTORY).expanduser()
        self.manual_idle = config.MANUAL_ACTIVITY_IDLE_SECONDS if manual_idle is None else manual_idle
        self.gap = config.ACTIVITY_ACTION_GAP_SECONDS if gap is None else gap
        self.poll = poll
        self.directory.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, manual_until REAL NOT NULL, next_auto REAL NOT NULL)")
            db.execute("INSERT OR IGNORE INTO state VALUES (1, 0, 0)")
            db.execute("""CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ticket TEXT UNIQUE NOT NULL,
                pid INTEGER NOT NULL, born REAL NOT NULL, manual INTEGER NOT NULL,
                expires REAL NOT NULL)""")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(str(self.directory / "queue.sqlite3"), timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _alive(pid, born):
        try:
            return abs(psutil.Process(pid).create_time() - born) < 0.01
        except psutil.NoSuchProcess:
            return False
        except psutil.AccessDenied:
            # Inability to inspect an owner is not evidence that it died.
            return True

    def _prune(self, db, now):
        for ticket, pid, born, expires in db.execute("SELECT ticket, pid, born, expires FROM tickets").fetchall():
            if expires < now or not self._alive(pid, born):
                db.execute("DELETE FROM tickets WHERE ticket=?", (ticket,))

    @contextmanager
    def operation(self, manual=False, quiet=True, timeout=None, on_wait=None):
        if operation_active():
            yield
            return
        ticket = uuid.uuid4().hex
        now = time.time()
        born = psutil.Process().create_time()
        mutex = _FileMutex(self.directory / "action.lock")
        entered = False
        token = None
        started = time.monotonic()
        deadline = started + timeout if timeout is not None else float("inf")
        last_reason = None
        try:
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("INSERT INTO tickets(ticket,pid,born,manual,expires) VALUES (?,?,?,?,?)",
                           (ticket, os.getpid(), born, int(manual), now + timeout + 5 if timeout is not None else float("inf")))
                if manual and quiet:
                    db.execute("UPDATE state SET manual_until=MAX(manual_until, ?) WHERE id=1",
                               (now + self.manual_idle,))
            while time.monotonic() < deadline:
                reason = "Waiting for another game action to finish"
                if mutex.acquire():
                    try:
                        with self._db() as db:
                            db.execute("BEGIN IMMEDIATE")
                            now = time.time()
                            self._prune(db, now)
                            first = db.execute("SELECT ticket,manual FROM tickets ORDER BY manual DESC,id LIMIT 1").fetchone()
                            manual_until, next_auto = db.execute("SELECT manual_until,next_auto FROM state WHERE id=1").fetchone()
                            if not manual and (manual_until > now or (first and first[1])):
                                reason = "Waiting: manual activity in webserver"
                            elif not manual and next_auto > now:
                                reason = "Waiting: shared pause between game actions"
                            elif first and first[0] == ticket:
                                db.execute("DELETE FROM tickets WHERE ticket=?", (ticket,))
                                entered = True
                        if entered:
                            break
                    finally:
                        if not entered:
                            mutex.release()
                if on_wait and reason != last_reason:
                    on_wait(reason)
                    last_reason = reason
                time.sleep(min(self.poll, max(0, deadline - time.monotonic())))
            if not entered:
                raise ActivityTimeout("Action postponed: timed out waiting for the shared activity coordinator.")
            token = _active.set((os.getpid(), threading.get_ident()))
            yield
        finally:
            if token is not None:
                _active.reset(token)
            try:
                with self._db() as db:
                    db.execute("DELETE FROM tickets WHERE ticket=?", (ticket,))
                    if entered:
                        now = time.time()
                        if manual and quiet:
                            db.execute("UPDATE state SET manual_until=MAX(manual_until, ?) WHERE id=1",
                                       (now + self.manual_idle,))
                        elif not manual and quiet:
                            db.execute("UPDATE state SET next_auto=? WHERE id=1", (now + random.uniform(*self.gap),))
            finally:
                mutex.close()


def request_kind(url="", params=None, payload=None):
    """Views are reads, even over POST. Explicit actions are coordinated.

    Unknown actions default to mutation. Header navigation is serialized but
    does not extend the manual inactivity timer. No HTTP-method heuristic.
    """
    text = str(url or "")
    query = urlsplit(text).query if "?" in text else text
    pairs = parse_qsl(query, keep_blank_values=True)
    values = dict(pairs)
    actions = [value for key, value in pairs if key == "action" and value]
    for source in (payload, params):
        if isinstance(source, str):
            source = dict(parse_qsl(source, keep_blank_values=True))
        if source:
            values.update(source)
            action = source.get("action")
            actions.extend(value for value in (action if isinstance(action, (list, tuple)) else [action]) if value)
    if not actions:
        return "read"
    if all(action == "header" for action in actions) and values.get("function") == "changeCurrentCity":
        return "navigation"
    return "mutation"


@contextmanager
def foreground_requests():
    """Mark only this web request's thread/context as foreground."""
    token = _foreground.set(True)
    try:
        yield
    finally:
        _foreground.reset(token)


@contextmanager
def activity(session=None):
    if operation_active():
        yield
        return
    def report(reason):
        if session is not None:
            session.setStatus(reason)
    manual = _foreground.get()
    with ActivityCoordinator().operation(manual=manual, timeout=600 if manual else None, on_wait=report):
        yield


def coordinated_request(func):
    """Cover all Session actions, including actions encoded in GET URLs."""
    signature = inspect.signature(func)
    @wraps(func)
    def wrapper(self, *args, **kwargs):
        arguments = signature.bind(self, *args, **kwargs).arguments
        kind = request_kind(arguments.get("url", ""), arguments.get("params"), arguments.get("payloadPost"))
        if kind == "read" or operation_active():
            return func(self, *args, **kwargs)
        manual = _foreground.get()
        with ActivityCoordinator().operation(manual=manual, quiet=kind == "mutation",
                                             timeout=600 if manual else None, on_wait=self.setStatus):
            return func(self, *args, **kwargs)
    return wrapper
