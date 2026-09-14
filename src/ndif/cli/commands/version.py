"""``ndif version`` — the versions that decide how this server behaves.

``ndif --version`` prints only the ndif package version. This prints the
resolved stack: ndif, nnsight, torch (with its CUDA line), transformers, ray and
Python. In the published image the same information is captured at build time
(``ndif version --write /etc/ndif/build.json``, see docker/Dockerfile) so a
tagged image can always say exactly what it carries.
"""

import importlib.metadata
import json
import platform
from pathlib import Path

import click

PACKAGES = ("ndif", "nnsight", "torch", "transformers", "ray", "peft", "accelerate")


def collect() -> dict:
    """Versions of every package that matters, plus torch's CUDA build."""
    info: dict = {"python": platform.python_version()}
    for pkg in PACKAGES:
        try:
            info[pkg] = importlib.metadata.version(pkg)
        except importlib.metadata.PackageNotFoundError:
            info[pkg] = None
    if info.get("torch"):
        try:
            import torch  # heavy, so only when installed

            info["cuda"] = torch.version.cuda
        except Exception:  # pragma: no cover - a broken torch is reported, not fatal
            info["cuda"] = None
    return info


@click.command()
@click.option("--json-output", "json_flag", is_flag=True, help="Output as JSON.")
@click.option("--write", "write_path", type=click.Path(dir_okay=False),
              help="Write the JSON to this file instead of printing (used at image build).")
def version(json_flag, write_path):
    """Show the versions of ndif, nnsight, torch (+CUDA), transformers and ray."""
    info = collect()
    if write_path:
        path = Path(write_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(info, indent=2) + "\n")
        click.echo(f"wrote {path}")
        return
    if json_flag:
        click.echo(json.dumps(info, indent=2))
        return
    width = max(len(k) for k in info)
    for key, value in info.items():
        click.echo(f"{key:<{width}}  {value if value is not None else '(not installed)'}")
