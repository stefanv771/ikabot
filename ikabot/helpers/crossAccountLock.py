#! /usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Cross-account activity lock, with fair (FIFO) queueing.

Unlike ikabot's existing per-account shipping lock (which is scoped to a
single {server}_{username} pair, so different accounts never compete for
it), this lock is intentionally scoped to the DEVICE, not the account: the
lock file path never includes a server or username, so every ikabot
instance running from this same machine (e.g. 6 separate Termux sessions,
each logged into a different account, possibly on different servers)
shares the exact same lock file.

Purpose: when running several automated accounts from one device (and
therefore one IP), avoid having more than one account mid-action (sending
a building upgrade request, or dispatching a resource shipment) at the
same instant. Serializing these short bursts - rather than letting them
fire simultaneously - avoids the specific, narrow pattern of multiple
accounts' request bursts lining up in time, without slowing down the
long, normal waits (construction time, ship travel time) that make up
the vast majority of each account's actual runtime.

Fairness: a bare "whichever process notices the lock is free first wins"
approach can starve some accounts indefinitely under normal OS
scheduling variance - one account can keep winning the race repeatedly
while others wait, until enough of them hit their timeout at nearly the
same moment and all act at once, which is exactly the outcome this
module exists to prevent. To avoid that, every waiting account takes a
numbered ticket (with its arrival time) in a shared queue file; the lock
is only ever granted to whichever waiting ticket is oldest.

Portability: rather than OS-specific file locking (fcntl on POSIX,
msvcrt on Windows), this module protects the shared queue file with a
small mutex implemented via atomic file creation (os.O_CREAT|os.O_EXCL),
which behaves identically on both platforms through Python's os module.
This means the exact same code path can be tested on Windows (e.g. in
VS Code) and will behave identically once deployed to Termux.

Defense in depth: the actual lock file is ALSO created with
os.O_CREAT|os.O_EXCL (atomic at the OS level), on top of the queue's
FIFO check. This guarantees two accounts can never simultaneously
believe they hold the lock, even if there were ever a bug in the
FIFO/mutex bookkeeping above it.

Crash recovery: when releasing, the enforced cooldown window is written
into the lock file itself (as `available_at`), rather than just held in
the releasing process's memory. If that process crashes mid-cooldown,
other waiting accounts can see exactly when the lock becomes available
from the file itself, instead of having to wait out the full 10-minute
stale-lock timeout.

Scope reminder: acquire this lock ONLY immediately around the
short-lived action itself (e.g. the POST that requests a building
upgrade, or the executeRoutes() call that dispatches a shipment) - never
around the long waits (waitForConstruction, ship travel) that follow.
Holding it longer than that would serialize accounts' entire timelines
instead of just their brief moments of active traffic, which defeats the
purpose.
"""

import json
import os
import random
import time
import uuid


LOCK_FILE_NAME = ".ikabot_cross_account_activity.lock"
QUEUE_FILE_NAME = ".ikabot_cross_account_queue.json"
QUEUE_MUTEX_NAME = ".ikabot_cross_account_queue.mutex"
STALE_LOCK_SECONDS = 10 * 60  # 10 minutes - a lock (or a queued ticket)
                               # older than this, with no `available_at`
                               # telling us otherwise, is assumed to
                               # belong to a crashed process
POST_RELEASE_COOLDOWN_RANGE = (10, 30)  # seconds - enforced gap after a
                                          # release before the NEXT account
                                          # may acquire the lock again
MUTEX_TIMEOUT_SECONDS = 10   # how long to wait for the (very short-lived)
                              # queue mutex before giving up on one attempt
MUTEX_RETRY_SECONDS = 0.05   # how often to retry while waiting for the
                              # queue mutex


def _home_dir():
    """Return the effective home directory for this process.

    Prefer environment variables (HOME / USERPROFILE), since they are
    inherited by spawned child processes. This keeps multiprocessing tests
    and real multi-session installations pointed at the same shared device
    directory.
    """
    for env_name in ("HOME", "USERPROFILE"):
        value = os.environ.get(env_name)
        if value:
            return value
    return os.path.expanduser("~")


def _lock_file_path():
    """
    Returns
    -------
    path : str
        Fixed path in the user's home directory. Deliberately excludes
        server/username, so it's the same path for every account running
        on this device.
    """
    return os.path.join(_home_dir(), LOCK_FILE_NAME)


def _queue_file_path():
    """
    Returns
    -------
    path : str
        Fixed path to the shared FIFO queue file (same directory as the
        lock file, same device-wide scope).
    """
    return os.path.join(_home_dir(), QUEUE_FILE_NAME)


def _replace_with_retry(src, dst, attempts=20, delay=0.05):
    """
    Parameters
    ----------
    src : str
    dst : str
    attempts : int
    delay : float

    os.replace() is atomic on POSIX and essentially never fails there.
    On Windows, it can transiently raise PermissionError (WinError 5)
    if another process happens to have `dst` open (even just for a
    quick read) at that exact instant - very likely with several real
    separate processes (not just threads) hammering the same shared
    file. This is self-resolving within milliseconds once the other
    process closes its handle, so a short retry loop is the standard
    fix rather than treating it as a real failure.
    """
    last_exc = None
    for _ in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError as e:
            last_exc = e
            time.sleep(delay)
    raise last_exc


def _remove_with_retry(path, attempts=20, delay=0.05):
    """
    Best-effort deletion that retries transient Windows PermissionError
    races, which can happen when another process is still closing a
    file handle after a successful release.
    """
    last_exc = None
    for _ in range(attempts):
        try:
            os.remove(path)
            return
        except FileNotFoundError:
            return
        except PermissionError as e:
            last_exc = e
            time.sleep(delay)
    if last_exc is not None:
        raise last_exc


def _queue_mutex_path():
    """
    Returns
    -------
    path : str
        Fixed path to the small mutex marker file that protects reads/
        writes of the queue file. Separate from the queue file itself so
        the mutex is a simple "does this file exist" check, regardless
        of the queue's actual (variable-length) JSON content.
    """
    return os.path.join(_home_dir(), QUEUE_MUTEX_NAME)


def _acquire_queue_mutex(timeout=MUTEX_TIMEOUT_SECONDS):
    """
    Parameters
    ----------
    timeout : float

    Returns
    -------
    acquired : bool
        True once the mutex marker file was atomically created by this
        process (os.O_CREAT|os.O_EXCL fails with FileExistsError if
        another process already holds it - this works identically on
        Windows and POSIX). False if we gave up after `timeout` seconds
        (should be rare - the mutex is only ever held very briefly).
    """
    mutex_path = _queue_mutex_path()
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            fd = os.open(mutex_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return True
        except (FileExistsError, PermissionError):
            # On Windows, a transient PermissionError can be raised when
            # another process is still closing a handle on the same mutex
            # file, even though the file is effectively "already there".
            # Treat that as "not yet available" and retry rather than
            # dying out of the contention window.
            try:
                age = time.time() - os.path.getmtime(mutex_path)
                if age > STALE_LOCK_SECONDS:
                    try:
                        _remove_with_retry(mutex_path)
                        continue
                    except PermissionError:
                        pass
            except OSError:
                pass
            time.sleep(MUTEX_RETRY_SECONDS)
    return False


def _release_queue_mutex():
    """
    Removes the mutex marker file, if present.
    """
    mutex_path = _queue_mutex_path()
    try:
        _remove_with_retry(mutex_path)
    except (OSError, PermissionError):
        pass


def _read_queue():
    """
    Returns
    -------
    tickets : list[dict]
        parsed queue contents, or an empty list if the file is missing,
        empty, or unreadable. Caller is responsible for holding the
        queue mutex before calling this.
    """
    queue_file = _queue_file_path()
    if not os.path.exists(queue_file):
        return []
    try:
        with open(queue_file, "r") as f:
            content = f.read()
        if not content.strip():
            return []
        return json.loads(content)
    except Exception:
        return []


def _write_queue(tickets):
    """
    Parameters
    ----------
    tickets : list[dict]

    Caller is responsible for holding the queue mutex before calling
    this. Writes via a temp file + atomic replace, so a reader can never
    observe a half-written file.
    """
    queue_file = _queue_file_path()
    tmp_path = f"{queue_file}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(tickets, f)
            f.flush()
        _replace_with_retry(tmp_path, queue_file)
    finally:
        try:
            _remove_with_retry(tmp_path)
        except (FileNotFoundError, PermissionError):
            pass


def _write_lock_data(lock_data):
    """
    Parameters
    ----------
    lock_data : dict

    Overwrites the lock file's content via temp file + atomic replace,
    so a reader can never observe a half-written file. Only meant to be
    called by whichever process already owns the lock (e.g. to stamp
    `available_at` onto it during the release cooldown).
    """
    lock_file = _lock_file_path()
    tmp_path = f"{lock_file}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(lock_data, f)
            f.flush()
        _replace_with_retry(tmp_path, lock_file)
    finally:
        try:
            _remove_with_retry(tmp_path)
        except (FileNotFoundError, PermissionError):
            pass


def _prune_stale_tickets(tickets):
    """
    Parameters
    ----------
    tickets : list[dict]

    Returns
    -------
    tickets : list[dict]
        the same list with stale entries removed, so a ticket left
        behind by a crashed process doesn't block the queue forever.
    """
    now = time.time()
    pruned = []
    for ticket in tickets:
        pid = ticket.get("pid")
        pid_is_alive = False
        if isinstance(pid, int):
            try:
                os.kill(pid, 0)
                pid_is_alive = True
            except (OSError, ValueError):
                pid_is_alive = False

        if pid_is_alive:
            pruned.append(ticket)
            continue

        if pid is None and now - ticket.get("timestamp", 0) >= STALE_LOCK_SECONDS:
            continue

        if pid is not None and not pid_is_alive:
            continue

        pruned.append(ticket)
    return pruned


def _join_queue(ticket_id, username):
    """
    Adds this ticket to the shared queue file.
    """
    if not _acquire_queue_mutex():
        return  # extremely unlikely; if it happens, this account simply
                 # never shows up in the queue and will time out cleanly
                 # rather than deadlock anyone else
    try:
        tickets = _prune_stale_tickets(_read_queue())
        tickets.append({
            "ticket_id": ticket_id,
            "username": username,
            "pid": os.getpid(),
            "timestamp": time.time(),
        })
        _write_queue(tickets)
    finally:
        _release_queue_mutex()


def _leave_queue(ticket_id):
    """
    Removes this ticket from the shared queue file, if present. Safe to
    call even if the ticket was already removed (e.g. on the success
    path) or the queue file doesn't exist.
    """
    if not _acquire_queue_mutex():
        return
    try:
        tickets = _read_queue()
        tickets = [t for t in tickets if t.get("ticket_id") != ticket_id]
        _write_queue(tickets)
    finally:
        _release_queue_mutex()


def _remove_stale_lock_if_any(lock_file):
    """
    Parameters
    ----------
    lock_file : str

    Removes the lock file if it looks abandoned: either its
    `available_at` cooldown timestamp has already passed (the owner
    finished, possibly crashed during its own cooldown, but told us
    exactly when it's safe to take over), or - if no `available_at` is
    present, meaning it's still meant to be actively held - it is older
    than STALE_LOCK_SECONDS or its recorded PID is no longer alive.
    """
    try:
        with open(lock_file, "r") as lf:
            lock_data = json.load(lf)
        now = time.time()
        available_at = lock_data.get("available_at")
        pid = lock_data.get("pid")

        pid_is_alive = False
        if isinstance(pid, int):
            try:
                os.kill(pid, 0)
                pid_is_alive = True
            except (OSError, ValueError):
                pid_is_alive = False

        if available_at is not None:
            if now >= available_at:
                try:
                    _remove_with_retry(lock_file)
                except (FileNotFoundError, PermissionError):
                    pass
        elif (not pid_is_alive) or (now - lock_data.get("timestamp", 0) > STALE_LOCK_SECONDS):
            try:
                _remove_with_retry(lock_file)
            except (FileNotFoundError, PermissionError):
                pass
    except Exception:
        # unreadable/corrupted lock file - fall back to its plain file
        # age instead of trusting its (unparseable) content
        try:
            if time.time() - os.path.getmtime(lock_file) > STALE_LOCK_SECONDS:
                try:
                    _remove_with_retry(lock_file)
                except (FileNotFoundError, PermissionError):
                    pass
        except (FileNotFoundError, OSError):
            pass


def acquire_activity_lock(username, timeout=600, poll_interval=5):
    """
    Blocks until the cross-account activity lock is acquired, or timeout
    is reached. Fair: waits its turn in a FIFO queue rather than racing
    other waiting accounts on every poll.

    Parameters
    ----------
    username : str
        identifies which account is acquiring the lock, stored in the
        lock file for visibility/debugging only.
    timeout : int
        maximum seconds to wait for the lock before giving up.
    poll_interval : int
        seconds between checks while waiting.

    Returns
    -------
    acquired : bool
        True if the lock was acquired, False on timeout (in which case
        this account's ticket has already been removed from the queue).
    """
    lock_file = _lock_file_path()
    ticket_id = str(uuid.uuid4())
    _join_queue(ticket_id, username)

    start_time = time.time()
    while time.time() - start_time < timeout:
        if _acquire_queue_mutex():
            try:
                # everything below runs while holding the queue mutex,
                # so "checking whether it's my turn" and "actually
                # taking the lock" happen as one atomic step - no other
                # process can slip in between them
                original_tickets = _read_queue()
                tickets = _prune_stale_tickets(original_tickets)
                if len(tickets) != len(original_tickets):
                    _write_queue(tickets)  # only write if pruning changed something
                is_next = bool(tickets) and tickets[0].get("ticket_id") == ticket_id

                if is_next and not os.path.exists(lock_file):
                    # belt-and-suspenders: even though the queue mutex
                    # already serializes this whole step, also create
                    # the lock file itself atomically (O_EXCL), so two
                    # accounts can never simultaneously believe they
                    # hold the lock even if the queue bookkeeping above
                    # ever had a bug
                    try:
                        fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    except FileExistsError:
                        fd = None

                    if fd is not None:
                        with os.fdopen(fd, "w") as f:
                            json.dump({
                                "username": username,
                                "pid": os.getpid(),
                                "timestamp": time.time(),
                            }, f)
                        tickets = [t for t in tickets if t.get("ticket_id") != ticket_id]
                        _write_queue(tickets)
                        return True

                if os.path.exists(lock_file):
                    _remove_stale_lock_if_any(lock_file)
            finally:
                _release_queue_mutex()

        time.sleep(poll_interval)

    # timed out - make sure our ticket doesn't stick around blocking
    # everyone else
    _leave_queue(ticket_id)
    return False


def release_activity_lock():
    """
    Stamps the lock file with an `available_at` timestamp (now + a
    randomized 10-30 second cooldown), then waits out that same window
    before removing the file.

    Writing `available_at` into the file (rather than just sleeping in
    memory) means that if THIS process crashes mid-cooldown, any other
    account polling for the lock can see exactly when it becomes
    available from the file itself, instead of having to wait out the
    full 10-minute stale-lock timeout.
    """
    lock_file = _lock_file_path()
    try:
        if not os.path.exists(lock_file):
            return
        with open(lock_file, "r") as f:
            lock_data = json.load(f)
        if lock_data.get("pid") != os.getpid():
            return  # not ours to release

        available_at = time.time() + random.randint(*POST_RELEASE_COOLDOWN_RANGE)
        lock_data["available_at"] = available_at
        _write_lock_data(lock_data)

        while time.time() < available_at:
            time.sleep(min(1, available_at - time.time()))

        try:
            _remove_with_retry(lock_file)
        except (FileNotFoundError, PermissionError):
            pass
    except Exception:
        pass
