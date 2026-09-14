import socket


def verify_connection(ip: str, port: int, timeout: float = 2) -> bool:
    """Whether a TCP connection can be established to ``ip:port``."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False
