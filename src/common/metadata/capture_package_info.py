"""Capture installed package version information."""

import importlib.metadata
from typing import Optional


def get_package_versions(
    package_filter: Optional[list[str]] = None,
) -> dict[str, str]:
    """Get versions of installed packages, optionally filtered by package names.

    Args:
        package_filter: Optional list of package names to include. If None, returns all packages.

    Returns:
        Dictionary mapping package names to version strings.
    """
    package_versions = {}

    if package_filter is None:
        # Get all installed packages
        for dist in importlib.metadata.distributions():
            package_versions[dist.name] = dist.version
    else:
        # Get only specified packages
        for package_name in package_filter:
            try:
                version = importlib.metadata.version(package_name)
                package_versions[package_name] = version
            except importlib.metadata.PackageNotFoundError:
                package_versions[package_name] = "not installed"

    return package_versions
