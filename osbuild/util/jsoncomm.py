"""JSON Communication

This module implements a client/server communication method based on JSON
serialization. It uses unix-domain-datagram-sockets and provides a simple
unicast message transmission.
"""


import array
import contextlib
import errno
import json
import os
import socket
from typing import Any
from typing import Optional


class FdSet():
    """File-Descriptor Set

    This object wraps an array of file-descriptors. Unlike a normal integer
    array, this object owns the file-descriptors and therefore closes them once
    the object is released.

    File-descriptor sets are initialized once. From then one, the only allowed
    operation is to query it for information, or steal file-descriptors from
    it. If you close a set, all remaining file-descriptors are closed and
    removed from the set. It will then be an empty set.
    """

    _fds = array.array("i")

    def __init__(self, *, rawfds):
        for i in rawfds:
            if not isinstance(i, int) or i < 0:
                raise ValueError()

        self._fds = rawfds

    def __del__(self):
        self.close()

    def close(self):
        """Close All Entries

        This closes all stored file-descriptors and clears the set. Once this
        returns, the set will be empty. It is safe to call this multiple times.
        Note that a set is automatically closed when it is garbage collected.
        """

        for i in self._fds:
            if i >= 0:
                os.close(i)

        self._fds = array.array("i")

    @classmethod
    def from_list(cls, l: list):
        """Create new Set from List

        This creates a new file-descriptor set initialized to the same entries
        as in the given list. This consumes the file-descriptors. The caller
        must not assume ownership anymore.
        """

        fds = array.array("i")
        fds.fromlist(l)
        return cls(rawfds=fds)

    def __len__(self):
        return len(self._fds)

    def __getitem__(self, key: Any):
        if self._fds[key] < 0:
            raise IndexError
        return self._fds[key]

    def steal(self, key: Any):
        """Steal Entry

        Retrieve the entry at the given position, but drop it from the internal
        file-descriptor set. The caller will now own the file-descriptor and it
        can no longer be accessed through the set.

        Note that this does not reshuffle the set. All indices stay constant.
        """

        v = self[key]
        self._fds[key] = -1
        return v


class Socket(contextlib.AbstractContextManager):
    """Communication Socket

    This socket object represents a communication channel. It allows sending
    and receiving JSON-encoded messages. It uses unix-domain-datagram sockets
    as underlying transport.
    """

    _socket = None
    _unlink = None

    def __init__(self, sock, unlink):
        self._socket = sock
        self._unlink = unlink

    def __del__(self):
        self.close()

    def __exit__(self, exc_type, exc_value, exc_tb):
        self.close()
        return False

    def close(self):
        """Close Socket

        Close the socket and all underlying resources. This can be called
        multiple times.
        """

        # close the socket if it is set
        if self._socket is not None:
            self._socket.close()
            self._socket = None

        # unlink the file-system entry, if pinned
        if self._unlink is not None:
            try:
                os.unlink(self._unlink[1], dir_fd=self._unlink[0])
            except OSError as e:
                if e.errno != errno.ENOENT:
                    raise

            os.close(self._unlink[0])
            self._unlink = None

    @classmethod
    def new_client(cls, connect_to: Optional[str] = None):
        """Create Client

        Create a new client socket.

        Parameters
        ----------
        connect_to
            If not `None`, the client will use the specified address as the
            default destination for all send operations.
        """

        sock = None

        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)

            # Trigger an auto-bind. If you do not do this, you might end up with
            # an unbound unix socket, which cannot receive messages.
            # Alternatively, you can also set `SO_PASSCRED`, but this has
            # side-effects.
            sock.bind("")

            # Connect the socket. This has no effect other than specifying the
            # default destination for send operations.
            if connect_to is not None:
                sock.connect(connect_to)
        except:
            if sock is not None:
                sock.close()
            raise

        return cls(sock, None)

    @classmethod
    def new_server(cls, bind_to: str):
        """Create Server

        Create a new listener socket.

        Parameters
        ----------
        bind_to
            The socket-address to listen on for incoming client requests.
        """

        sock = None
        unlink = None
        path = os.path.split(bind_to)

        try:
            # We bind the socket and then open a directory-fd on the target
            # socket. This allows us to properly unlink the socket when the
            # server is closed. Note that sockets are never automatically
            # cleaned up on linux, nor can you bind to existing sockets.
            # We use a dirfd to guarantee this works even when you change
            # your mount points in-between.
            # Yeah, this is racy when mount-points change between the socket
            # creation and open. But then your entire socket creation is racy
            # as well. We do not guarantee atomicity, so you better make sure
            # you do not rely on it.
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            sock.bind(bind_to)
            unlink = os.open(os.path.join(".", path[0]), os.O_CLOEXEC | os.O_PATH)
        except:
            if unlink is not None:
                os.close(unlink)
            if sock is not None:
                sock.close()
            raise

        return cls(sock, (unlink, path[1]))

    def fileno(self) -> int:
        assert self._socket is not None
        return self._socket.fileno()

    def recv(self):
        """Receive a Message

        This receives the next pending message from the socket. This operation
        is synchronous.

        A tuple consisting of the deserialized message payload, the auxiliary
        file-descriptor set, and the socket-address of the sender is returned.
        """

        # On `SOCK_DGRAM`, packets might be arbitrarily sized. There is no
        # hard-coded upper limit, since it is only restricted by the size of
        # the kernel write buffer on sockets (which itself can be modified via
        # sysctl). The only real maximum is probably something like 2^31-1,
        # since that is the maximum of that sysctl datatype.
        # Anyway, `MSG_TRUNC+MSG_PEEK` usually allows us to easily peek at the
        # incoming buffer. Unfortunately, the python `recvmsg()` wrapper
        # discards the return code and we cannot use that. Instead, we simply
        # loop until we know the size. This is slightly awkward, but seems fine
        # as long as you do not put this into a hot-path.
        size = 4096
        while True:
            peek = self._socket.recvmsg(size, 0, socket.MSG_PEEK)
            if not (peek[2] & socket.MSG_TRUNC):
                break
            size *= 2

        # Fetch a packet from the socket. On linux, the maximum SCM_RIGHTS array
        # size is hard-coded to 253. This allows us to size the ancillary buffer
        # big enough to receive any possible message.
        fds = array.array("i")
        msg = self._socket.recvmsg(size, socket.CMSG_LEN(253 * fds.itemsize))

        # First thing we do is always to fetch the CMSG FDs into an FdSet. This
        # guarantees that we do not leak FDs in case the message handling fails
        # for other reasons.
        for level, ty, data in msg[1]:
            if level == socket.SOL_SOCKET and ty == socket.SCM_RIGHTS:
                assert len(data) % fds.itemsize == 0
                fds.frombytes(data)
        fdset = FdSet(rawfds=fds)

        # Check the returned message flags. If the message was truncated, we
        # have to discard it. This shouldn't happen, but there is no harm in
        # handling it. However, `CTRUNC` can happen, since it is also triggered
        # when LSMs reject FD transmission. Treat it the same as a parser error.
        flags = msg[2]
        if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
            raise BufferError

        try:
            payload = json.loads(msg[0])
        except json.JSONDecodeError:
            raise BufferError

        return (payload, fdset, msg[3])

    def send(self, payload: object, *, destination: Optional[str] = None, fds: Optional[list] = None):
        """Send Message

        Send a new message via this socket. This operation is synchronous. The
        maximum message size depends on the configured send-buffer on the
        socket. An `OSError` with `EMSGSIZE` is raised when it is exceeded.

        Parameters
        ----------
        payload
            A python object to serialize as JSON and send via this socket. See
            `json.dump()` for details about the serialization involved.
        destination
            The destination to send to. If `None`, the default destination is
            used (if none is set, this will raise an `OSError`).
        fds
            A list of file-descriptors to send with the message.

        Raises
        ------
        OSError
            If the socket cannot be written, a matching `OSError` is raised.
        TypeError
            If the payload cannot be serialized, a type error is raised.
        """

        serialized = json.dumps(payload).encode()
        cmsg = []
        if fds:
            cmsg.append((socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds)))

        n = self._socket.sendmsg([serialized], cmsg, 0, destination)
        assert n == len(serialized)
