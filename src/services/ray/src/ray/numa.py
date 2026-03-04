"""NUMA topology discovery and affinity utilities.

Best-effort NUMA awareness for Ray ModelActors. All functions gracefully
fall back to no-ops when NUMA information is unavailable (non-Linux,
containers without sysfs, missing libnuma).
"""

import ctypes
import ctypes.util
import logging
import os
import platform
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

logger = logging.getLogger("ndif")

IS_LINUX = platform.system() == "Linux"

SYSFS_NODE_BASE = Path("/sys/devices/system/node")
SYSFS_PCI_BASE = Path("/sys/bus/pci/devices")


@dataclass
class NumaNode:
    node_id: int
    cpu_ids: Set[int] = field(default_factory=set)
    gpu_indices: Set[int] = field(default_factory=set)
    memory_bytes: int = 0


def _parse_cpu_list(cpu_list_str: str) -> Set[int]:
    """Parse a Linux cpulist string (e.g. '0-3,8-11') into a set of CPU IDs."""
    cpus = set()
    for part in cpu_list_str.strip().split(","):
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            cpus.update(range(int(start), int(end) + 1))
        else:
            cpus.add(int(part))
    return cpus


def _read_sysfs(path: Path) -> Optional[str]:
    """Read a sysfs file, returning None on any failure."""
    try:
        return path.read_text().strip()
    except (OSError, IOError):
        return None


def gpu_to_numa_node(gpu_index: int) -> Optional[int]:
    """Map a GPU index to its NUMA node ID via pynvml and sysfs.

    Returns None if the mapping cannot be determined.
    """
    if not IS_LINUX:
        return None

    try:
        import pynvml

        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(gpu_index)
        pci_info = pynvml.nvmlDeviceGetPciInfo(handle)

        # PCI bus ID format from pynvml: "00000000:XX:YY.Z"
        pci_bus_id = pci_info.busId
        if isinstance(pci_bus_id, bytes):
            pci_bus_id = pci_bus_id.decode("utf-8")

        # Normalize to lowercase for sysfs lookup
        pci_bus_id = pci_bus_id.lower()

        numa_path = SYSFS_PCI_BASE / pci_bus_id / "numa_node"
        content = _read_sysfs(numa_path)
        if content is not None:
            node_id = int(content)
            # -1 means NUMA info not available
            return node_id if node_id >= 0 else None

    except Exception:
        logger.debug(f"Could not determine NUMA node for GPU {gpu_index}", exc_info=True)

    return None


def discover_topology() -> Optional[Dict[int, NumaNode]]:
    """Discover NUMA topology from sysfs.

    Returns a dict mapping node_id -> NumaNode, or None if topology
    cannot be determined.
    """
    if not IS_LINUX:
        return None

    if not SYSFS_NODE_BASE.exists():
        return None

    nodes: Dict[int, NumaNode] = {}

    try:
        for node_dir in sorted(SYSFS_NODE_BASE.iterdir()):
            match = re.match(r"node(\d+)", node_dir.name)
            if not match:
                continue

            node_id = int(match.group(1))
            node = NumaNode(node_id=node_id)

            # Parse CPU list
            cpulist = _read_sysfs(node_dir / "cpulist")
            if cpulist:
                node.cpu_ids = _parse_cpu_list(cpulist)

            # Parse memory info
            meminfo = _read_sysfs(node_dir / "meminfo")
            if meminfo:
                for line in meminfo.splitlines():
                    if "MemTotal" in line:
                        # Format: "Node X MemTotal:    12345 kB"
                        parts = line.split()
                        for i, part in enumerate(parts):
                            if part.isdigit() and i + 1 < len(parts) and parts[i + 1] == "kB":
                                node.memory_bytes = int(part) * 1024
                                break
                        break

            nodes[node_id] = node

    except (OSError, IOError):
        logger.debug("Failed to discover NUMA topology", exc_info=True)
        return None

    return nodes if nodes else None


def get_numa_nodes_for_gpus(gpu_indices: List[int]) -> Set[int]:
    """Map a list of GPU indices to their NUMA node IDs.

    Returns an empty set if no mappings can be determined.
    """
    numa_nodes = set()
    for idx in gpu_indices:
        node_id = gpu_to_numa_node(idx)
        if node_id is not None:
            numa_nodes.add(node_id)
    return numa_nodes


def set_cpu_affinity(cpu_ids: Set[int]) -> bool:
    """Set CPU scheduling affinity for the current process.

    Returns True on success, False on failure.
    """
    if not cpu_ids:
        return False

    try:
        os.sched_setaffinity(0, cpu_ids)
        logger.info(f"Set CPU affinity to {sorted(cpu_ids)}")
        return True
    except (OSError, AttributeError):
        logger.debug("Failed to set CPU affinity", exc_info=True)
        return False


def _load_libnuma() -> Optional[ctypes.CDLL]:
    """Load libnuma shared library via ctypes."""
    try:
        lib_path = ctypes.util.find_library("numa")
        if lib_path is None:
            return None
        lib = ctypes.CDLL(lib_path)
        # Check that libnuma is available by calling numa_available()
        if lib.numa_available() < 0:
            return None
        return lib
    except (OSError, AttributeError):
        return None


_libnuma: Optional[ctypes.CDLL] = None
_libnuma_loaded: bool = False


def _get_libnuma() -> Optional[ctypes.CDLL]:
    """Get cached libnuma handle."""
    global _libnuma, _libnuma_loaded
    if not _libnuma_loaded:
        _libnuma = _load_libnuma()
        _libnuma_loaded = True
    return _libnuma


def set_memory_policy_preferred(numa_node_id: int) -> bool:
    """Set memory allocation policy to prefer the given NUMA node.

    Uses libnuma's numa_set_preferred(). Returns True on success.
    """
    lib = _get_libnuma()
    if lib is None:
        return False

    try:
        lib.numa_set_preferred(ctypes.c_int(numa_node_id))
        logger.info(f"Set memory policy to prefer NUMA node {numa_node_id}")
        return True
    except Exception:
        logger.debug("Failed to set preferred memory policy", exc_info=True)
        return False


def set_memory_policy_interleave(numa_node_ids: Set[int]) -> bool:
    """Set memory allocation policy to interleave across NUMA nodes.

    Uses libnuma's numa_set_interleave_mask(). Returns True on success.
    """
    if not numa_node_ids:
        return False

    lib = _get_libnuma()
    if lib is None:
        return False

    try:
        # Allocate a nodemask bitmask via libnuma
        lib.numa_allocate_nodemask.restype = ctypes.c_void_p
        mask = lib.numa_allocate_nodemask()
        if not mask:
            return False

        try:
            lib.numa_bitmask_clearall(ctypes.c_void_p(mask))
            for node_id in numa_node_ids:
                lib.numa_bitmask_setbit(ctypes.c_void_p(mask), ctypes.c_uint(node_id))

            lib.numa_set_interleave_mask(ctypes.c_void_p(mask))
            logger.info(f"Set memory policy to interleave across NUMA nodes {sorted(numa_node_ids)}")
            return True
        finally:
            lib.numa_bitmask_free(ctypes.c_void_p(mask))

    except Exception:
        logger.debug("Failed to set interleave memory policy", exc_info=True)
        return False


def apply_numa_affinity(
    gpu_indices: List[int],
    topology: Optional[Dict[int, NumaNode]] = None,
) -> tuple[Optional[Set[int]], Optional[Dict[int, NumaNode]]]:
    """Apply best-effort NUMA affinity for the given GPU indices.

    Sets CPU affinity and memory policy based on GPU-to-NUMA mapping.

    Returns:
        (numa_node_ids, topology) — the resolved NUMA node IDs and topology,
        stored by callers for reuse. Both may be None on failure.
    """
    numa_node_ids = get_numa_nodes_for_gpus(gpu_indices)
    if not numa_node_ids:
        logger.debug("No NUMA node mapping found for GPUs, skipping affinity")
        return None, topology

    if topology is None:
        topology = discover_topology()

    if topology is None:
        logger.debug("Could not discover NUMA topology, skipping affinity")
        return numa_node_ids, None

    # Collect CPU IDs from target NUMA nodes
    target_cpus: Set[int] = set()
    for node_id in numa_node_ids:
        node = topology.get(node_id)
        if node:
            target_cpus.update(node.cpu_ids)

    set_cpu_affinity(target_cpus)

    # Set memory policy
    if len(numa_node_ids) == 1:
        set_memory_policy_preferred(next(iter(numa_node_ids)))
    else:
        set_memory_policy_interleave(numa_node_ids)

    return numa_node_ids, topology


def get_gpu_numa_mapping(gpu_count: int) -> Dict[int, int]:
    """Build a mapping of GPU index -> NUMA node ID for all GPUs.

    Returns a dict (possibly empty) of gpu_index -> numa_node_id.
    """
    mapping = {}
    for i in range(gpu_count):
        node_id = gpu_to_numa_node(i)
        if node_id is not None:
            mapping[i] = node_id
    return mapping
