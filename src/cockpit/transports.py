# This file is part of Cockpit.
#
# Copyright (C) 2022 Red Hat, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Bi-directional asyncio.Transport implementations based on file descriptors."""

import asyncio
import collections
import fcntl
import logging
import os
import select
import socket
import struct
import subprocess
import termios

from typing import Any, ClassVar, Dict, Optional, Tuple


logger = logging.getLogger(__name__)
IOV_MAX = 1024  # man 2 writev


class _Transport(asyncio.Transport):
    BLOCK_SIZE: ClassVar[int] = 1024 * 1024

    # A transport always has a loop and a protocol
    _loop: asyncio.AbstractEventLoop
    _protocol: asyncio.Protocol

    _queue: Optional[collections.deque[bytes]]
    _in_fd: int
    _out_fd: int
    _closing: bool
    _is_reading: bool
    _eof: bool
    _eio_is_eof: bool = False

    def __init__(self,
                 loop: asyncio.AbstractEventLoop,
                 protocol: asyncio.Protocol,
                 in_fd: int = -1, out_fd: int = -1,
                 extra: Optional[Dict[str, object]] = None):
        super().__init__(extra)

        self._loop = loop
        self._protocol = protocol

        logger.debug('Created transport %s for protocol %s, fds %d %d', self, protocol, in_fd, out_fd)

        self._queue = None
        self._is_reading = False
        self._eof = False
        self._closing = False

        self._in_fd = in_fd
        self._out_fd = out_fd

        os.set_blocking(in_fd, False)
        if out_fd != in_fd:
            os.set_blocking(out_fd, False)

        self._protocol.connection_made(self)
        self.resume_reading()

    def _read_ready(self) -> None:
        logger.debug('Read ready on %s %s %d', self, self._protocol, self._in_fd)
        try:
            data = os.read(self._in_fd, _Transport.BLOCK_SIZE)
        except IOError:
            # PTY devices return EIO to mean "EOF"
            if not self._eio_is_eof:
                raise  # pragma: no cover
            data = b''
        except BlockingIOError:  # pragma: no cover
            return
        except OSError as exc:
            # Other errors: terminate the connection
            self.abort(exc)
            return

        if data != b'':
            logger.debug('  read %d bytes', len(data))
            self._protocol.data_received(data)
        else:
            logger.debug('  got EOF')
            self._close_reader()
            keep_open = self._protocol.eof_received()
            if not keep_open:
                self.close()

    def is_reading(self) -> bool:
        return self._is_reading

    def _close_reader(self) -> None:
        self.pause_reading()
        self._in_fd = -1

    def pause_reading(self) -> None:
        if self._is_reading:
            self._loop.remove_reader(self._in_fd)
            self._is_reading = False

    def resume_reading(self) -> None:
        # It's possible that the Protocol could decide to attempt to unpause
        # reading after _close_reader() got called.  Check that the fd is != -1
        # before actually resuming.
        if not self._is_reading and self._in_fd != -1:
            self._loop.add_reader(self._in_fd, self._read_ready)
            self._is_reading = True

    def _close(self) -> None:
        pass

    def abort(self, exc: Optional[Exception] = None) -> None:
        self._closing = True
        self._close_reader()
        self._remove_write_queue()
        self._protocol.connection_lost(exc)
        self._close()

    def can_write_eof(self) -> bool:
        raise NotImplementedError

    def write_eof(self) -> None:
        assert not self._eof
        self._eof = True
        if self._queue is None:
            self._write_eof_now()

    def get_write_buffer_size(self) -> int:
        if self._queue is None:
            return 0
        return sum(len(block) for block in self._queue)

    def get_write_buffer_limits(self) -> Tuple[int, int]:
        return (0, 0)

    def set_write_buffer_limits(self, high: Optional[int] = None, low: Optional[int] = None) -> None:
        assert high is None or high == 0
        assert low is None or low == 0

    def _write_eof_now(self) -> None:
        raise NotImplementedError

    def _write_ready(self) -> None:
        assert self._queue is not None

        try:
            n_bytes = os.writev(self._out_fd, self._queue)
        except BlockingIOError:  # pragma: no cover
            n_bytes = 0
        except OSError as exc:
            self.abort(exc)
            return

        while n_bytes:
            block = self._queue.popleft()
            if len(block) > n_bytes:
                # This block wasn't completely written.
                self._queue.appendleft(block[n_bytes:])
                break
            n_bytes -= len(block)
        else:
            self._remove_write_queue()
            if self._eof:
                self._write_eof_now()
            if self._closing:
                self.abort()

    def _remove_write_queue(self) -> None:
        if self._queue is not None:
            self._protocol.resume_writing()
            self._loop.remove_writer(self._out_fd)
            self._queue = None

    def _create_write_queue(self, data: bytes) -> None:
        assert self._queue is None
        self._loop.add_writer(self._out_fd, self._write_ready)
        self._queue = collections.deque((data,))
        self._protocol.pause_writing()

    def write(self, data: bytes) -> None:
        assert not self._closing
        assert not self._eof

        if self._queue is not None:
            self._queue.append(data)

            # writev() will complain if the queue is too long.  Consolidate it.
            if len(self._queue) > IOV_MAX:
                all_data = b''.join(self._queue)
                self._queue.clear()
                self._queue.append(all_data)

            return

        try:
            n_bytes = os.write(self._out_fd, data)
        except BlockingIOError:
            n_bytes = 0
        except OSError as exc:
            self.abort(exc)
            return

        if n_bytes != len(data):
            self._create_write_queue(data[n_bytes:])

    def close(self) -> None:
        if self._closing:
            return

        self._closing = True
        self._close_reader()

        if self._queue is not None:
            # abort() will be called from _write_ready() when it's done
            return

        self.abort()

    def get_protocol(self) -> asyncio.BaseProtocol:
        return self._protocol

    def is_closing(self) -> bool:
        return self._closing

    def set_protocol(self, protocol: asyncio.BaseProtocol) -> None:
        raise NotImplementedError

    def __del__(self) -> None:
        self._close()


class SubprocessProtocol(asyncio.Protocol):
    """An extension to asyncio.Protocol for use with SubprocessTransport."""
    def process_exited(self) -> None:
        """Called when subprocess has exited."""
        raise NotImplementedError


class SubprocessTransport(_Transport, asyncio.SubprocessTransport):
    """A bi-directional transport speaking with stdin/out of a subprocess.

    Note: this is not really a normal SubprocessTransport.  Although it
    implements the entire API of asyncio.SubprocessTransport, it is not
    designed to be used with asyncio.SubprocessProtocol objects.  Instead, it
    pair with normal Protocol objects which also implement the
    SubprocessProtocol defined in this module (which only has a
    process_exited() method).  Whatever the protocol writes is sent to stdin,
    and whatever comes from stdout is given to the Protocol via the
    .data_received() function.

    If stderr is configured as a pipe, the transport will separately collect
    data from it, making it available via the .get_stderr() method.
    """

    _returncode: Optional[int] = None

    _sock: Optional[socket.socket] = None
    _pty_fd: Optional[int] = None
    _process: subprocess.Popen[bytes]
    _stderr: Optional['Spooler']

    @staticmethod
    def _create_watcher() -> asyncio.AbstractChildWatcher:
        try:
            os.close(os.pidfd_open(os.getpid(), 0))  # check for kernel support
            return asyncio.PidfdChildWatcher()
        except (AttributeError, OSError):
            pass

        return asyncio.SafeChildWatcher()

    @staticmethod
    def _get_watcher(loop: asyncio.AbstractEventLoop) -> asyncio.AbstractChildWatcher:
        quark = '_cockpit_transports_child_watcher'
        watcher = getattr(loop, quark, None)

        if watcher is None:
            watcher = SubprocessTransport._create_watcher()
            watcher.attach_loop(loop)
            setattr(loop, quark, watcher)

        return watcher

    def get_stderr(self) -> Optional[bytes]:
        if self._stderr is not None:
            return self._stderr.get()
        else:
            return None

    def _exited(self, pid: int, code: int) -> None:
        # NB: per AbstractChildWatcher API, this handler should be thread-safe,
        # but we only ever use non-threaded child watcher implementations, so
        # we can assume we'll always be called in the main thread.

        # NB: the subprocess is going to want to waitpid() itself as well, but
        # will get ECHILD since we already reaped it.  Fortunately, since
        # Python 3.2 this is supported, and process gets a return status of
        # zero.  For that reason, we need to store our own copy of the return
        # status.  See https://github.com/python/cpython/issues/59960
        assert isinstance(self._protocol, SubprocessProtocol)
        assert self._process.pid == pid
        self._returncode = code
        logger.debug('Process exited with status %d', self._returncode)
        if not self._closing:
            self._protocol.process_exited()

    def __init__(self,
                 loop: asyncio.AbstractEventLoop,
                 protocol: SubprocessProtocol,
                 args: list[str],
                 pty: bool = False,
                 window: Optional[Dict[str, int]] = None,
                 **kwargs: Any):
        if pty:
            our_fd, session_fd = os.openpty()
            kwargs['stderr'] = session_fd
            self._eio_is_eof = True
            self._pty_fd = our_fd

            if window is not None:
                self.set_window_size(**window)
        else:
            self._sock, sock = socket.socketpair()
            our_fd = self._sock.fileno()
            session_fd = sock.detach()

        try:
            self._process = subprocess.Popen(args,
                                             stdin=session_fd, stdout=session_fd,
                                             start_new_session=True, **kwargs)
        finally:
            os.close(session_fd)

        if self._process.stderr is not None:
            self._stderr = Spooler(loop, self._process.stderr.fileno())
        else:
            self._stderr = None

        super().__init__(loop, protocol, our_fd, our_fd)

        self._get_watcher(loop).add_child_handler(self._process.pid, self._exited)

    def set_window_size(self, rows: int, cols: int) -> None:
        assert self._pty_fd is not None
        fcntl.ioctl(self._pty_fd, termios.TIOCSWINSZ, struct.pack('2H4x', rows, cols))

    def can_write_eof(self) -> bool:
        return self._sock is not None

    def _write_eof_now(self) -> None:
        assert self._sock is not None
        self._sock.shutdown(socket.SHUT_WR)

    def get_pid(self) -> int:
        return self._process.pid

    def get_returncode(self) -> Optional[int]:
        return self._returncode

    def get_pipe_transport(self, fd: int) -> asyncio.Transport:
        raise NotImplementedError

    def send_signal(self, signal: int) -> None:  # type: ignore # https://github.com/python/mypy/issues/13885
        self._process.send_signal(signal)

    def terminate(self) -> None:
        self._process.terminate()

    def kill(self) -> None:
        self._process.kill()

    def _close(self) -> None:
        if self._pty_fd is not None:
            os.close(self._pty_fd)
            self._pty_fd = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None


class SocketTransport(_Transport):
    """A Transport subclass that can wrap any socket"""
    _socket: socket.socket

    def __init__(self,
                 loop: asyncio.AbstractEventLoop,
                 protocol: asyncio.Protocol,
                 sock: socket.socket):
        super().__init__(loop, protocol, sock.fileno(), sock.fileno(), {'socket': sock})
        self._socket = sock

    def _close(self) -> None:
        self._socket.close()

    def can_write_eof(self) -> bool:
        return True

    def _write_eof_now(self) -> None:
        self._socket.shutdown(socket.SHUT_WR)


class StdioTransport(_Transport):
    """A bi-directional transport that corresponds to stdin/out.

    Can talk to just about anything:
        - files
        - pipes
        - character devices (including terminals)
        - sockets
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, protocol: asyncio.Protocol, stdin: int = 0, stdout: int = 1):
        super().__init__(loop, protocol, stdin, stdout)

    def can_write_eof(self) -> bool:
        return False

    def _write_eof_now(self) -> None:
        raise RuntimeError("Can't write EOF to stdout")


class Spooler:
    """Consumes data from an fd, storing it in a buffer.

    This makes a copy of the fd, so you don't have to worry about holding it
    open.
    """

    _loop: asyncio.AbstractEventLoop
    _fd: int
    _contents: list[bytes]

    def __init__(self, loop: asyncio.AbstractEventLoop, fd: int):
        self._loop = loop
        self._fd = -1  # in case dup() raises an exception
        self._contents = []

        self._fd = os.dup(fd)

        os.set_blocking(self._fd, False)
        loop.add_reader(self._fd, self._read_ready)

    def _read_ready(self) -> None:
        try:
            data = os.read(self._fd, 8192)
        except BlockingIOError:  # pragma: no cover
            return
        except OSError:
            # all other errors -> EOF
            data = b''

        if data != b'':
            self._contents.append(data)
        else:
            self.close()

    def _is_ready(self) -> bool:
        if self._fd == -1:
            return False
        return select.select([self._fd], [], [], 0) != ([], [], [])

    def get(self) -> bytes:
        while self._is_ready():
            self._read_ready()

        return b''.join(self._contents)

    def is_closed(self) -> bool:
        return self._fd == -1

    def close(self) -> None:
        if self._fd != -1:
            self._loop.remove_reader(self._fd)
            os.close(self._fd)
            self._fd = -1

    def __del__(self) -> None:
        self.close()
