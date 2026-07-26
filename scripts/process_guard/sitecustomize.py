from __future__ import annotations

import os


_GUARD_ERROR_PATH_ENV = "STATA_CODEX_TEST_GUARD_ERROR_PATH"


def _fatal_guard_startup() -> None:
    path = os.environ.get(_GUARD_ERROR_PATH_ENV)
    if path:
        descriptor: int | None = None
        try:
            flags = os.O_WRONLY | os.O_APPEND
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            payload = f"GUARD_INIT_ERROR {os.getpid()}\n".encode("ascii")
            os.write(descriptor, payload)
        except BaseException:
            pass
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
    os._exit(125)


try:
    import inspect
    import multiprocessing.util as multiprocessing_util
    import select
    import subprocess
    import threading

    from process_guard.authorization import detachment_is_authorized

    _TRACKER_FDS_ENV = "STATA_CODEX_TEST_TRACKER_FDS"
    _STOP_FDS_ENV = "STATA_CODEX_TEST_STOP_FDS"
    _TRACKER_FDS = tuple(
        int(value)
        for value in os.environ[_TRACKER_FDS_ENV].split(",")
    )
    _STOP_FDS = tuple(
        int(value) for value in os.environ[_STOP_FDS_ENV].split(",")
    )
    if not _TRACKER_FDS or not _STOP_FDS:
        raise RuntimeError("process-guard descriptor sets are empty")
    for _descriptor in _TRACKER_FDS + _STOP_FDS:
        os.set_inheritable(_descriptor, True)

    def _write_record(payload: bytes) -> None:
        if len(payload) > 512 or not payload.endswith(b"\n"):
            _fatal_guard_startup()
        for descriptor in _TRACKER_FDS:
            try:
                written = os.write(descriptor, payload)
            except OSError:
                _fatal_guard_startup()
            if written != len(payload):
                _fatal_guard_startup()

    def _start_stop_watcher() -> None:
        def stop_on_eof() -> None:
            while True:
                try:
                    readable, _, _ = select.select(_STOP_FDS, [], [])
                    for descriptor in readable:
                        if not os.read(descriptor, 1):
                            os._exit(125)
                except OSError:
                    os._exit(125)

        threading.Thread(
            target=stop_on_eof,
            name="test-process-stop-watcher",
            daemon=True,
        ).start()

    def _fork_child_stop_watcher() -> None:
        _start_stop_watcher()

    _original_popen = subprocess.Popen
    _popen_signature = inspect.signature(_original_popen)

    class _TrackedPopen(_original_popen):
        def __init__(self, *args, **kwargs):
            bound = _popen_signature.bind(*args, **kwargs)
            arguments = bound.arguments
            detaches = (
                bool(arguments.get("start_new_session", False))
                or arguments.get("process_group") is not None
                or arguments.get("preexec_fn") is not None
            )
            if detaches and not detachment_is_authorized():
                raise PermissionError(
                    "detached subprocess creation requires an explicit "
                    "process-guard authorization"
                )
            inherited = set(arguments.get("pass_fds", ()))
            inherited.update(_TRACKER_FDS)
            inherited.update(_STOP_FDS)
            arguments["pass_fds"] = tuple(sorted(inherited))
            arguments["close_fds"] = True
            _original_popen.__init__(
                self,
                *bound.args,
                **bound.kwargs,
            )

    subprocess.Popen = _TrackedPopen

    def _deny_detachment(name: str) -> None:
        original = getattr(os, name, None)
        if original is None:
            return

        def guarded(*args, **kwargs):
            if not detachment_is_authorized():
                raise PermissionError(
                    f"os.{name} requires an explicit process-guard "
                    "authorization"
                )
            return original(*args, **kwargs)

        setattr(os, name, guarded)

    for _function_name in ("setsid", "setpgid", "setpgrp"):
        _deny_detachment(_function_name)

    def _guard_posix_spawn(name: str) -> None:
        original = getattr(os, name, None)
        if original is None:
            return

        def guarded(*args, **kwargs):
            detaches = bool(kwargs.get("setsid", False)) or (
                "setpgroup" in kwargs
            )
            if detaches and not detachment_is_authorized():
                raise PermissionError(
                    f"os.{name} detachment requires an explicit "
                    "process-guard authorization"
                )
            return original(*args, **kwargs)

        setattr(os, name, guarded)

    for _function_name in ("posix_spawn", "posix_spawnp"):
        _guard_posix_spawn(_function_name)

    if hasattr(os, "fork"):
        _original_fork = os.fork

        def _tracked_fork():
            pid = _original_fork()
            if pid == 0:
                _fork_child_stop_watcher()
            return pid

        os.fork = _tracked_fork

    if hasattr(os, "forkpty"):
        _original_forkpty = os.forkpty

        def _tracked_forkpty():
            pid, descriptor = _original_forkpty()
            if pid == 0:
                _fork_child_stop_watcher()
            return pid, descriptor

        os.forkpty = _tracked_forkpty

    _original_spawnv_passfds = multiprocessing_util.spawnv_passfds

    def _tracked_spawnv_passfds(path, args, passfds):
        inherited = set(passfds)
        inherited.update(_TRACKER_FDS)
        inherited.update(_STOP_FDS)
        return _original_spawnv_passfds(
            path,
            args,
            tuple(sorted(inherited)),
        )

    multiprocessing_util.spawnv_passfds = _tracked_spawnv_passfds

    _start_stop_watcher()
    _write_record(f"READY {os.getpid()}\n".encode("ascii"))
except BaseException:
    _fatal_guard_startup()
