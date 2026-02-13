#!/usr/bin/env python3
"""Minimal CUDA runtime tests for NDIF modeling base behavior.

Tests:
1) Changing CUDA_VISIBLE_DEVICES at runtime after CUDA init.
2) Using accelerate device_map to control placement with all devices visible.
3) Setting per-process memory fraction after CUDA initialization.
4) Device switching simulation (removing and re-dispatching accelerate hooks).
5) torch.cuda.set_device() controlling default tensor placement.

Run in a fresh Python process. Results depend on GPU availability.
"""

from __future__ import annotations

import os
import sys


def banner(title: str) -> None:
    print("\n" + "=" * 80)
    print(title)
    print("=" * 80)


def env_value(name: str) -> str:
    value = os.environ.get(name)
    return value if value is not None else "<unset>"


def main() -> int:
    try:
        import torch
    except Exception as exc:
        print(f"Failed to import torch: {exc}")
        return 1

    banner("Test 1: CUDA_VISIBLE_DEVICES runtime change")
    original_cvd = os.environ.get("CUDA_VISIBLE_DEVICES")
    print(f"CUDA_VISIBLE_DEVICES before init: {env_value('CUDA_VISIBLE_DEVICES')}")

    if not torch.cuda.is_available():
        print("CUDA not available; skipping Test 1.")
    else:
        # initialize the CUDA context
        torch.cuda.init()
        # First call initializes CUDA context in this process.
        count_before = torch.cuda.device_count()
        print(f"device_count before env change: {count_before}")

        # Try to change visible devices after init.
        new_value = "0"
        os.environ["CUDA_VISIBLE_DEVICES"] = new_value
        count_after = torch.cuda.device_count()
        print(f"CUDA_VISIBLE_DEVICES after change: {env_value('CUDA_VISIBLE_DEVICES')}")
        print(f"device_count after env change: {count_after}")

        if count_after == count_before:
            print("Result: device_count did not change after init (expected).")
        else:
            print("Result: device_count changed after init (unexpected; verify environment).")

    # Restore environment variable.
    if original_cvd is None:
        os.environ.pop("CUDA_VISIBLE_DEVICES", None)
    else:
        os.environ["CUDA_VISIBLE_DEVICES"] = original_cvd

    banner("Test 2: accelerate device_map placement")
    print(f"CUDA_VISIBLE_DEVICES at start of Test 2: {env_value('CUDA_VISIBLE_DEVICES')}")
    try:
        from accelerate import dispatch_model
    except Exception as exc:
        print(f"accelerate not available; skipping Test 2. ({exc})")
    else:
        if not torch.cuda.is_available():
            print("CUDA not available; skipping Test 2.")
        else:
            device_count = torch.cuda.device_count()
            if device_count == 0:
                print("No visible CUDA devices; skipping Test 2.")
            else:
                import torch.nn as nn

                class TinyModel(nn.Module):
                    def __init__(self) -> None:
                        super().__init__()
                        self.l0 = nn.Linear(4, 4)
                        self.l1 = nn.Linear(4, 4)

                    def forward(self, x):
                        return self.l1(self.l0(x))

                model = TinyModel()

                dev0 = "cuda:0"
                dev1 = "cuda:1" if device_count > 1 else "cuda:0"
                device_map = {"l0": dev0, "l1": dev1}
                print(f"device_count: {device_count}")
                print(f"device_map: {device_map}")

                dispatch_model(model, device_map=device_map)

                param_devices = {
                    name: str(param.device) for name, param in model.named_parameters()
                }
                unique_devices = sorted(set(param_devices.values()))

                print("Parameter devices:")
                for name, device in param_devices.items():
                    print(f"  {name}: {device}")
                print(f"Unique devices: {unique_devices}")
                print("Result: parameters should match the device_map entries above.")

    banner("Test 3: set_per_process_memory_fraction after init")
    if not torch.cuda.is_available():
        print("CUDA not available; skipping Test 3.")
    else:
        try:
            torch.cuda.set_device(0)
            _ = torch.empty(1, device="cuda")
            print("CUDA context initialized on device 0.")

            try:
                torch.cuda.set_per_process_memory_fraction(0.5, device=0)
                print("set_per_process_memory_fraction(0.5, device=0) succeeded.")
            except Exception as exc:
                print(f"set_per_process_memory_fraction failed: {exc}")

            try:
                # Large allocation to confirm the process still allocates on GPU.
                memory_required = 0.2 * torch.cuda.get_device_properties(0).total_memory 
                # compute the number of elements to allocate
                num_elements = int(memory_required // torch.finfo(torch.float32).bits // 8)
                _ = torch.empty((num_elements,), device="cuda", dtype=torch.float32)
                torch.cuda.synchronize()
                print("Large allocation succeeded after setting memory fraction.")
            except Exception as exc:
                print(f"Large allocation failed after setting memory fraction: {exc}")
            
            # change the memory fraction
            try: 
                torch.cuda.set_per_process_memory_fraction(0.1, device=0)
                print(f"set_per_process_memory_fraction to 0.1 on device 0 succeeded.")
            except Exception as exc:
                print(f"set_per_process_memory_fraction to 0.1 failed: {exc}")

            # try to allocate a large that exeeds 0.1 of the memory
            try:
                # compute the memory required for the allocation
                memory_required = 0.2 * torch.cuda.get_device_properties(0).total_memory 
                _ = torch.empty((memory_required // sizeof(torch.float32),), device="cuda", dtype=torch.float32)
                torch.cuda.synchronize()
                print("Large allocation succeeded after setting memory fraction. This means the memory fraction is not effective.")
            except Exception as exc:
                print(f"Large allocation failed after setting memory fraction. This means the memory fraction is effective.")

            # set another cap on device 1
            try: 
                torch.cuda.set_per_process_memory_fraction(0.1, device=1)
                print(f"set_per_process_memory_fraction to 0.1 on device 1 succeeded.")
            except Exception as exc:
                print(f"set_per_process_memory_fraction to 0.1 on device 1 failed: {exc}")

            print(f"memory_allocated: {torch.cuda.memory_allocated()} bytes")
            print(f"max_memory_allocated: {torch.cuda.max_memory_allocated()} bytes")
        except Exception as exc:
            print(f"Unexpected CUDA error in Test 3: {exc}")

    banner("Test 4: Device switching simulation (from_cache pattern)")
    try:
        from accelerate import dispatch_model
        from accelerate.hooks import remove_hook_from_module
    except Exception as exc:
        print(f"accelerate not available; skipping Test 4. ({exc})")
    else:
        if not torch.cuda.is_available():
            print("CUDA not available; skipping Test 4.")
        else:
            device_count = torch.cuda.device_count()
            if device_count == 0:
                print("No visible CUDA devices; skipping Test 4.")
            else:
                import torch.nn as nn

                class TinyModel(nn.Module):
                    def __init__(self) -> None:
                        super().__init__()
                        self.l0 = nn.Linear(4, 4)
                        self.l1 = nn.Linear(4, 4)

                    def forward(self, x):
                        return self.l1(self.l0(x))

                model = TinyModel()

                # First dispatch to cuda:0
                dev0 = "cuda:0"
                device_map = {"l0": dev0, "l1": dev0}
                print(f"device_count: {device_count}")
                print(f"Initial device_map: {device_map}")

                model = dispatch_model(model, device_map=device_map)

                param_devices = {
                    name: str(param.device) for name, param in model.named_parameters()
                }
                print("Initial parameter devices:")
                for name, device in param_devices.items():
                    print(f"  {name}: {device}")

                # Remove hooks and re-dispatch (like from_cache does)
                print("Removing accelerate hooks and re-dispatching...")
                for name, module in model.named_modules():
                    remove_hook_from_module(module, recurse=True)

                # Re-dispatch to different device(s)
                if device_count > 1:
                    new_device_map = {"l0": "cuda:1", "l1": "cuda:1"}
                else:
                    # Fall back to CPU if only one GPU
                    new_device_map = {"l0": "cuda:0", "l1": "cpu"}

                print(f"New device_map: {new_device_map}")
                model = dispatch_model(model, device_map=new_device_map)

                new_param_devices = {
                    name: str(param.device) for name, param in model.named_parameters()
                }
                print("New parameter devices:")
                for name, device in new_param_devices.items():
                    print(f"  {name}: {device}")

                if param_devices != new_param_devices:
                    print("Result: Device switch successful!")
                else:
                    print("Result: Device switch failed (devices unchanged).")

if __name__ == "__main__":
    sys.exit(main())
