"""
scanner.py — detects processes currently listening on TCP ports.
"""

import psutil


class Scanner:
    def scan(self) -> dict:
        """
        Return {port: {pid, name, cmdline}} for every TCP port
        currently in LISTEN state on this machine.
        """
        result = {}

        try:
            connections = psutil.net_connections(kind="inet")
        except psutil.AccessDenied:
            # macOS may require Full Disk Access for net_connections.
            # Fail gracefully — the dashboard will show all projects as "stopped".
            return result

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

        return result

    def is_port_in_use(self, port: int) -> bool:
        return port in self.scan()
