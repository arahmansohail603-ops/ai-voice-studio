"""Device selection for the two voice-cloning engines.

Both engines must use the GPU when the machine has one and fall back to the CPU
when it does not. That was not true of XTTS: the installed ``coqui-tts`` defaults
its ``gpu`` argument to ``False``, not ``None``, so simply omitting it pinned
*every* machine to the CPU -- including ones with a perfectly good NVIDIA GPU.
The user waited minutes with the hardware sitting idle and no way to tell why.

The rule pinned here: never pass ``gpu=True`` unless CUDA is actually available.
``True`` without CUDA raises inside Coqui instead of falling back, which would
turn a working CPU install into a hard failure.

These run in a child process rather than in-process. Both engines do
``import torch`` while loading, and on Windows that import only succeeds if
CTranslate2 claimed the OpenMP runtime first -- the same DLL base-name collision
``app.core.native_runtime`` exists to prevent. A test module that imported torch
directly passed alone and then failed the whole suite, purely because an earlier
test had pulled in Qt without the preload. Each child establishes the runtime
itself, so these results do not depend on the order the suite happens to run in.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# Reports which device and dtype each engine asked for, with the two libraries
# stubbed so no 2 GB download is attempted.
_PROBE = """
import json, sys, types
sys.path.insert(0, {root!r})

from app.core.native_runtime import preload_native_runtime
assert preload_native_runtime(), "native runtime preload failed"

cuda = {cuda}
seen = {{}}

if {engine!r} == "xtts":
    class FakeTTS:
        def __init__(self, **kwargs):
            seen.update(kwargs)
    TTS = types.ModuleType("TTS")
    api = types.ModuleType("TTS.api")
    api.TTS = FakeTTS
    TTS.api = api
    sys.modules["TTS"] = TTS
    sys.modules["TTS.api"] = api
    sys.modules["torch"] = types.ModuleType("torch")
    sys.modules["torch"].cuda = types.SimpleNamespace(
        is_available=lambda: cuda
    )
    from app.core.voice_cloner import VoiceCloner
    cloner = VoiceCloner()
    cloner._cuda_available = staticmethod(lambda: cuda)
    messages = []
    cloner.load_background(lambda state, msg: messages.append(msg))
    cloner._load_thread.join(timeout=60)
    out = {{"state": cloner.state.value, "device": cloner.device,
           "messages": messages, "seen": {{k: str(v) for k, v in seen.items()}}}}
else:
    import torch
    class FakeModel:
        @staticmethod
        def from_pretrained(*args, **kwargs):
            seen["model_name"] = args[0] if args else None
            seen.update({{k: str(v) for k, v in kwargs.items()}})
            return object()
    stub = types.ModuleType("qwen_tts")
    stub.Qwen3TTSModel = FakeModel
    sys.modules["qwen_tts"] = stub
    from app.core.qwen_cloner import QwenVoiceCloner
    cloner = QwenVoiceCloner()
    cloner._cuda_available = staticmethod(lambda: cuda)
    messages = []
    cloner.load_background(lambda state, msg: messages.append(msg))
    cloner._load_thread.join(timeout=60)
    out = {{"state": cloner.state.value, "device": cloner.device,
           "messages": messages, "seen": {{k: str(v) for k, v in seen.items()}},
           "bfloat16": str(torch.bfloat16), "float32": str(torch.float32)}}

print("PROBE_JSON:" + json.dumps(out))
"""


def _probe(engine: str, cuda: bool) -> dict:
    import json

    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _PROBE.format(root=str(_ROOT), engine=engine, cuda=cuda),
        ],
        capture_output=True,
        text=True,
        timeout=600,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    for line in result.stdout.splitlines():
        if line.startswith("PROBE_JSON:"):
            return json.loads(line.split("PROBE_JSON:", 1)[1])
    raise AssertionError(
        f"probe produced no result\nrc={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr[-2000:]!r}"
    )


class XttsDeviceSelectionTests(unittest.TestCase):
    """XTTS: GPU when there is one, CPU otherwise, and say which."""

    def test_a_gpu_machine_asks_coqui_for_the_gpu(self) -> None:
        seen = _probe("xtts", True)["seen"]
        self.assertEqual(
            seen.get("gpu"),
            "True",
            "a CUDA-capable machine was left on the CPU because the argument was "
            "omitted and this coqui build defaults it to False",
        )

    def test_a_cpu_only_machine_asks_for_the_cpu(self) -> None:
        seen = _probe("xtts", False)["seen"]
        self.assertEqual(
            seen.get("gpu"),
            "False",
            "gpu was left unset, so the machine silently inherited the default",
        )

    def test_the_load_completes_and_the_device_is_reported(self) -> None:
        for cuda, expected in ((True, "GPU"), (False, "CPU")):
            with self.subTest(cuda=cuda):
                out = _probe("xtts", cuda)
                self.assertEqual(out["state"], "ready", " ".join(out["messages"]))
                self.assertEqual(out["device"], "cuda" if cuda else "cpu")
                # A slow CPU run that looks hung is indistinguishable from a bug.
                self.assertIn(expected, " ".join(out["messages"]))


class QwenDeviceSelectionTests(unittest.TestCase):
    """Qwen already chose GPU-then-CPU; these lock that in against regressions.

    Qwen switches dtype with the device -- bfloat16 on CUDA, float32 on CPU --
    which is what makes the CPU path merely slow instead of failing to fit.
    """

    def test_gpu_is_used_with_bfloat16(self) -> None:
        out = _probe("qwen", True)
        self.assertEqual(out["state"], "ready", " ".join(out["messages"]))
        self.assertEqual(out["seen"].get("device_map"), "cuda")
        self.assertEqual(out["seen"].get("dtype"), out["bfloat16"])
        self.assertEqual(out["device"], "cuda")

    def test_cpu_falls_back_with_float32(self) -> None:
        out = _probe("qwen", False)
        self.assertEqual(out["state"], "ready", " ".join(out["messages"]))
        self.assertEqual(out["seen"].get("device_map"), "cpu")
        self.assertEqual(out["seen"].get("dtype"), out["float32"])
        self.assertEqual(out["device"], "cpu")
        # The user has to be warned: 1.7B on a CPU takes minutes a line.
        self.assertIn("CPU", " ".join(out["messages"]))

    def test_a_cpu_machine_is_not_blocked_by_the_gpu_only_flag(self) -> None:
        """``QWEN_GPU_ONLY`` is False, so CPU has to stay a working path."""
        from app import config

        self.assertFalse(
            config.QWEN_GPU_ONLY,
            "with this set, CPU-only machines cannot clone at all",
        )


if __name__ == "__main__":
    unittest.main()
