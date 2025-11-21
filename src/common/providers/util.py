import socket


def verify_connection(ip: str, port: int, timeout: float = 2) -> bool:
    """Verify if a connection can be established to the given IP and port."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception as e:
        return False
