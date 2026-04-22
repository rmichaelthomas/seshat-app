"""
scanner.py — detects processes currently listening on TCP ports.
"""

import subprocess

import psutil


def _scan_via_lsof() -> dict:
    """macOS fallback: parse `lsof` for listening TCP sockets.

    macOS 12+ restricts psutil.net_connections(kind="inet") without a special
    entitlement — lsof works without sudo and lists every user-visible socket.
    Using -F (field) mode gives stable, parseable output keyed by p/c/n lines.
    """
    try:
        r = subprocess.run(
            ["lsof", "-iTCP", "-sTCP:LISTEN", "-n", "-P", "-F", "pcnL"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    if r.returncode not in (0, 1):  # lsof returns 1 when no matches
        return {}

    result: dict = {}
    pid = name = None
    for line in r.stdout.splitlines():
        if not line:
            continue
        tag, rest = line[0], line[1:]
        if tag == "p":
            pid = int(rest) if rest.isdigit() else None
            name = None
        elif tag == "c":
            name = rest
        elif tag == "n" and pid is not None:
            # rest looks like "*:7777" or "127.0.0.1:9000" or "[::1]:5000"
            if ":" not in rest:
                continue
            port_str = rest.rsplit(":", 1)[1]
            if not port_str.isdigit():
                continue
            port = int(port_str)
            if port in result:
                continue  # first listener wins (IPv4 before IPv6 in lsof output)
            try:
                proc = psutil.Process(pid)
                cmdline = " ".join(proc.cmdline())
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                cmdline = ""
            result[port] = {"pid": pid, "name": name or "unknown", "cmdline": cmdline}
    return result


class Scanner:
    def scan(self) -> dict:
        """
        Return {port: {pid, name, cmdline}} for every TCP port
        currently in LISTEN state on this machine.
        """
        result = {}

        try:
            connections = psutil.net_connections(kind="inet")
        except (psutil.AccessDenied, PermissionError):
            # macOS 12+ denies this without a special entitlement. Fall back to lsof.
            return _scan_via_lsof()

        for conn in connections:
            if conn.status != psutil.CONN_LISTEN:
                continue
            if not conn.laddr:
                continue

            port = conn.laddr.port
            pid  = conn.pid
            if pid is None:
                continue

            try:
                proc = psutil.Process(pid)
                result[port] = {
                    "pid":     pid,
                    "name":    proc.name(),
                    "cmdline": " ".join(proc.cmdline()),
                }
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                result[port] = {"pid": pid, "name": "unknown", "cmdline": ""}

        # If psutil succeeded but returned nothing, try lsof as a safety net
        # (some macOS setups return empty instead of raising).
        if not result:
            return _scan_via_lsof()
        return result

    def is_port_in_use(self, port: int) -> bool:
        return port in self.scan()
