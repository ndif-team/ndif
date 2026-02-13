from typing import Dict, Optional

def gib(bytes_value: Optional[int]) -> Optional[float]:
    if bytes_value is None:
        return None
    return bytes_value / (1024 ** 3)

def gib_map(bytes_by_id: Dict[int, int]) -> Dict[int, float]:
    return {gpu_id: gib(bytes_value) for gpu_id, bytes_value in bytes_by_id.items()}