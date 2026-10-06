"""Force IPv4 for outbound HTTP.

Observed on a dev box: IPv6 resolves but is unroutable (WinError 10051), making
every request hang through retries. IPv4-only is harmless on EC2. Disable with
T105_ALLOW_IPV6=1.
"""

import os
import socket

import urllib3.util.connection as _conn

if os.environ.get("T105_ALLOW_IPV6") != "1":
    _conn.allowed_gai_family = lambda: socket.AF_INET
