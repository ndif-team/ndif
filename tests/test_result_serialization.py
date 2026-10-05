"""The result path's pickler, exercised without a stack.

Requests travel *into* the server by value — nnsight's cloudpickle-based
serializer ships a registered or unimportable class with its code — but results
travel *out* through ``torch.save(..., pickle_module=cpu_pickle_module())``.
These tests round-trip exactly that hop. A subprocess plays the client and
ships classes by value the way nnsight does (``register_pickle_by_value``);
this process plays the server, where deserializing the request populates
cloudpickle's dynamic-class tracker — just as it does in the model actor, the
sandbox runner, and the TP rank-0 actor, all of which serialize results in the
process that deserialized the request — and further subprocesses play the
client reading the result back with a stock ``torch.load``.

No GPUs, no model, no server: the contract under test is pure serialization.
A class the client sent comes back as the client's *own* class (``isinstance``
against the user's import holds); a class born inside the traced block, which
never existed client-side, comes back as a working twin; everything importable
on both sides stays plain by-reference pickle; CUDA tensors relocate to CPU.
"""

from __future__ import annotations

import io
import pickle
import subprocess
import sys
import textwrap

import pytest
import torch

from ndif.services.ray.deployments.modeling.util import cpu_pickle_module

# A name nothing in this environment provides, so the test process genuinely
# cannot import the "user's library" — the condition the server is in.
USERLIB = "userlib_remote_only"

USERLIB_SOURCE = f"""
import enum
from dataclasses import dataclass


class Custom:
    def __init__(self, x):
        self.x = x


@dataclass
class Record:
    y: int


class Color(enum.Enum):
    RED = 1
    BLUE = 2


class Outer:
    class Inner:
        def __init__(self, z):
            self.z = z
"""


def run(script: str, *, path: str | None = None) -> str:
    """Run a client-side script in a fresh interpreter; fail with its stderr."""
    prelude = f"import sys; sys.path.insert(0, {path!r})\n" if path else ""
    proc = subprocess.run(
        [sys.executable, "-c", prelude + textwrap.dedent(script)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.fixture(scope="module")
def libdir(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("client_site")
    (path / f"{USERLIB}.py").write_text(USERLIB_SOURCE)
    return str(path)


@pytest.fixture(scope="module")
def saved(libdir, tmp_path_factory) -> dict:
    """What the block "saved", after arriving by value — the server's view.

    The client subprocess registers its library by value and pickles instances
    with cloudpickle, exactly nnsight's request-side mechanism; loading that
    blob here reconstructs the classes and registers them in cloudpickle's
    dynamic-class tracker, which is the state the result pickler keys off.
    """
    try:
        __import__(USERLIB)
        pytest.fail(f"{USERLIB} importable in the test process; test is void")
    except ImportError:
        pass

    blob_path = tmp_path_factory.mktemp("wire") / "request.pkl"
    run(
        f"""
        import cloudpickle, {USERLIB} as userlib
        cloudpickle.register_pickle_by_value(userlib)
        payload = {{
            "custom": userlib.Custom(3),
            "record": userlib.Record(4),
            "color": userlib.Color.RED,
            "inner": userlib.Outer.Inner(7),
        }}
        open({str(blob_path)!r}, "wb").write(cloudpickle.dumps(payload))
        """,
        path=libdir,
    )
    payload = pickle.loads(blob_path.read_bytes())
    assert type(payload["custom"]).__module__ == USERLIB

    # And two things born inside the traced block itself: blocks cross as
    # source and are exec'd server-side, so these never existed client-side.
    block_ns: dict = {}
    exec(
        "class BlockCls:\n"
        "    def __init__(self, w):\n"
        "        self.w = w\n"
        "def scale(x):\n"
        "    return x * 2\n",
        block_ns,
    )
    payload["block"] = block_ns["BlockCls"](9)
    payload["fn"] = block_ns["scale"]
    payload["tensor"] = torch.arange(4.0)
    return payload


@pytest.fixture(scope="module")
def result_path(saved, tmp_path_factory) -> str:
    """The result blob, serialized the way both execution paths serialize it."""
    buffer = io.BytesIO()
    torch.save(saved, buffer, pickle_module=cpu_pickle_module())
    path = tmp_path_factory.mktemp("wire") / "result.pt"
    path.write_bytes(buffer.getvalue())
    return str(path)


def test_plain_pickle_refuses_the_same_payload(saved):
    # The condition the pickler exists for: a by-value class has no importable
    # home here, so an unassisted torch.save dies verifying the reference.
    with pytest.raises(pickle.PicklingError):
        torch.save(saved, io.BytesIO())


def test_client_classes_come_back_as_the_real_classes(result_path, libdir):
    run(
        f"""
        import torch, {USERLIB} as userlib
        obj = torch.load({result_path!r}, map_location="cpu", weights_only=False)
        assert type(obj["custom"]) is userlib.Custom, "identity lost"
        assert isinstance(obj["custom"], userlib.Custom) and obj["custom"].x == 3
        assert type(obj["record"]) is userlib.Record and obj["record"].y == 4
        assert obj["color"] is userlib.Color.RED, "enum identity lost"
        assert obj["tensor"].sum().item() == 6.0
        """,
        path=libdir,
    )


def test_block_borns_come_back_as_working_twins(saved, tmp_path_factory):
    # No library on the path at all: the block-defined class and function can
    # only come back by value, and they do — state intact, callable. Their own
    # blob, because a pointered class *requires* its module at load time (the
    # client that registered it has it; this loader deliberately has nothing).
    buffer = io.BytesIO()
    torch.save(
        {"block": saved["block"], "fn": saved["fn"]},
        buffer,
        pickle_module=cpu_pickle_module(),
    )
    path = tmp_path_factory.mktemp("wire") / "block_only.pt"
    path.write_bytes(buffer.getvalue())
    run(
        f"""
        import torch
        obj = torch.load({str(path)!r}, map_location="cpu", weights_only=False)
        assert type(obj["block"]).__name__ == "BlockCls" and obj["block"].w == 9
        assert obj["fn"](3) == 6
        """
    )


def test_unresolvable_pointer_degrades_to_a_twin(result_path, libdir):
    # cloudpickle flattens a nested class's qualname on the by-value trip in,
    # so the pointer's name ("Inner") resolves nowhere in the client's library.
    # The getattr default then hands back the by-value twin instead of erroring.
    run(
        f"""
        import torch, {USERLIB} as userlib
        obj = torch.load({result_path!r}, map_location="cpu", weights_only=False)
        inner = obj["inner"]
        assert type(inner) is not userlib.Outer.Inner
        assert type(inner).__name__ == "Inner" and inner.z == 7
        """,
        path=libdir,
    )


def test_importable_values_stay_plain_by_reference():
    # Nothing by-value crept into a payload of ordinary types: the stream must
    # stay loadable with no cloudpickle at all, so the fast path can't have
    # regressed into blobbing everything.
    import fractions

    buffer = io.BytesIO()
    torch.save(
        {"t": torch.ones(2), "f": fractions.Fraction(1, 2)},
        buffer,
        pickle_module=cpu_pickle_module(),
    )
    assert b"cloudpickle" not in buffer.getvalue()
    out = torch.load(io.BytesIO(buffer.getvalue()), weights_only=False)
    assert out["f"] == fractions.Fraction(1, 2)


@pytest.mark.skipif(not torch.cuda.is_available(), reason="needs a GPU")
def test_cuda_tensors_relocate_to_cpu():
    buffer = io.BytesIO()
    torch.save(
        {"t": torch.ones(2, device="cuda")}, buffer, pickle_module=cpu_pickle_module()
    )
    out = torch.load(io.BytesIO(buffer.getvalue()), weights_only=False)
    assert out["t"].device.type == "cpu" and out["t"].sum().item() == 2.0
