"""One end of the conversation between a host and a runner.

A `Link` wraps a channel (or a `Fanout` over several) and receives by type: the
caller names the messages it is waiting for. Each end spells an exchange once,
here, and a message of the wrong kind is an error at the point it arrives.
"""

from __future__ import annotations


class ProtocolError(Exception):
    """A message arrived that the receiver was not waiting for.

    The two ends have fallen out of step, and every later message would be read
    as something it is not.
    """


class Link:
    """Sends messages over ``channel`` and receives them by type.

    Args:
        channel: anything with ``send`` and ``recv``: a ``Channel``, or a
            ``Fanout`` over several.
        always: ``{type: handler}`` for messages that answer nothing and can
            arrive at any point. Each is handed to its handler wherever this end
            happens to be waiting.
    """

    def __init__(self, channel, always=None) -> None:
        self.channel = channel
        self.always = always or {}

    def send(self, message) -> None:
        self.channel.send(message)

    def receive(self, *expect):
        """The next message that is one of ``expect``.

        Raises:
            ProtocolError: a message arrived that is neither expected nor one of
                ``always``.
        """
        while True:
            message = self.channel.recv()
            if type(message) in self.always:
                self.always[type(message)](message)
            elif isinstance(message, expect):
                return message
            else:
                wanted = " or ".join(kind.__name__ for kind in expect)
                raise ProtocolError(
                    f"got {type(message).__name__} while waiting for {wanted}"
                )

    def request(self, message, *expect):
        """Send ``message`` and return the reply, one of ``expect``."""
        self.send(message)
        return self.receive(*expect)
