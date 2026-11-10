"""Bounded NVIDIA device-access smoke test using only ctypes and the CUDA driver.

Allocates 16 MiB, fills device memory, and checks copied-back values for two
seconds. This checks real device access; it is not a GPU performance benchmark.
Submit with --gpu. No CUDA toolkit, compiler, NumPy, or PyTorch is required.
"""

import ctypes as ct
import json
import os
import sys
import time


def main() -> int:
    if os.environ.get("CUDA_VISIBLE_DEVICES") != "0":
        print("Submit this smoke test through rmgr with --gpu", file=sys.stderr)
        return 2
    try:
        cuda = ct.CDLL("libcuda.so.1")

        def bind(name, types):
            function = getattr(cuda, name)
            function.argtypes = types
            function.restype = ct.c_int

            def call(*args):
                code = function(*args)
                if code:
                    raise RuntimeError(f"{name} returned CUDA error {code}")
            return call

        init = bind("cuInit", [ct.c_uint])
        get_device = bind("cuDeviceGet", [ct.POINTER(ct.c_int), ct.c_int])
        retain = bind("cuDevicePrimaryCtxRetain", [ct.POINTER(ct.c_void_p), ct.c_int])
        release = bind("cuDevicePrimaryCtxRelease_v2", [ct.c_int])
        set_context = bind("cuCtxSetCurrent", [ct.c_void_p])
        allocate = bind("cuMemAlloc_v2", [ct.POINTER(ct.c_uint64), ct.c_size_t])
        free = bind("cuMemFree_v2", [ct.c_uint64])
        fill = bind("cuMemsetD32_v2", [ct.c_uint64, ct.c_uint, ct.c_size_t])
        copy = bind("cuMemcpyDtoH_v2", [ct.c_void_p, ct.c_uint64, ct.c_size_t])
        synchronize = bind("cuCtxSynchronize", [])
        init(0)
        device, context, address = ct.c_int(), ct.c_void_p(), ct.c_uint64()
        get_device(ct.byref(device), 0)
        retain(ct.byref(context), device)
        try:
            set_context(context)
            allocate(ct.byref(address), 16 * 1024**2)
            try:
                started = time.time()
                deadline = time.monotonic() + 2
                sample = (ct.c_uint * 16)()
                iterations = 0
                while time.monotonic() < deadline:
                    fill(address, 12345, 4 * 1024**2)
                    synchronize()
                    copy(sample, address, ct.sizeof(sample))
                    if any(value != 12345 for value in sample):
                        raise RuntimeError("Device memory verification failed")
                    iterations += 1
                    time.sleep(0.02)
                print(json.dumps({"started": started, "finished": time.time(), "iterations": iterations, "ok": True}))
            finally:
                free(address)
        finally:
            release(device)
    except (OSError, AttributeError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
