"""Forces IPv4-only DNS resolution process-wide.

Some networks have IPv6 routes that silently black-hole outbound
connections. Python's resolver returns IPv6 addresses first and each one
has to time out before falling back to IPv4, making every request (yt-dlp,
the Gemini SDK, anything using sockets) take tens of seconds or hang
outright. Importing this module once patches socket.getaddrinfo for the
whole process, so every module that talks to the network benefits
regardless of import order.
"""

import socket

_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


socket.getaddrinfo = _ipv4_only_getaddrinfo
