"""Backend registry + the thin builtin backends (spec §6, wave 4).

The registry is the "nothing downstream cares whether one model or five serve
the senses" seam.  A profile names a backend per slot (see
:mod:`fluxer.omni.profile`); the cascade/callers ask the registry for the
built backend and only ever call :meth:`process` (turn-based) or
:meth:`open_session` (duplex).

Builtin backends are deliberately **thin wrappers** around the things that are
actually verified on this box (``docs/omni-models-feasibility.md`` §2/§3):

===============  ==========================================================
slot             backend
===============  ==========================================================
``text_*``       ``agent`` (marker — serviced by the adapter core),
                 ``local.llama_server`` (HTTP client for a llama-server)
``audio_in``     ``local.whispercpp`` (CPU, verified), ``local.qwen3asr``
                 (GPU via llama-mtmd-cli, verified; optional llama-server
                 ``input_audio`` transport documented on the class)
``audio_out``    ``local.piper`` (CPU, verified), ``local.qwen3tts``
                 (GPU via ``llama-tts``, verified; honours the ``-c 1024``
                 gotcha from the feasibility doc §3.4)
``image_in``     ``local.smolvlm`` (llama-mtmd-cli caption)
``video_in``     ``local.smolvlm_video`` (native ``--video`` path first,
                 frame-loop fallback — wraps ``fluxer.video.LocalVideoDuplexer``)
``video_out``    ``local.render`` (ffmpeg card/annotate — wraps
                 ``fluxer.video.render``)
any              ``null`` (honest gap placeholder; raises with a clear message)
duplex           ``null_duplex`` (no unified model fits 6 GB tonight; raises
                 with the config shape for the future — see
                 :data:`fluxer.omni.types.UNIFIED_CONFIG_EXAMPLE`)
===============  ==========================================================

Import discipline: stdlib only at module import.  The video package is
imported lazily *inside* the video factories, and the GPU env is applied per
call, so importing the engine never touches a subprocess or a model file.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib import error as urllib_error
from urllib import request as urllib_request

from .types import (
    UNIFIED_CONFIG_EXAMPLE,
    BackendError,
    BackendNotConfigured,
    HostRequired,
    OmniError,
    Part,
    SenseBinding,
)

__all__ = [
    "BackendSpec",
    "register_backend",
    "unregister_backend",
    "get_backend",
    "list_backends",
    "describe_backends",
    "backend_catalog",
    "register_builtin_backends",
    "run_command",
    "CommandResult",
    "apply_gpu_env",
    "default_root",
    # argv builders (pure; unit-tested + reused by the classes)
    "whisper_argv",
    "qwen3asr_argv",
    "piper_argv",
    "qwen3tts_argv",
    "smolvlm_argv",
    "smolvlm_video_argv",
    # backends
    "AgentBackend",
    "LlamaServerBackend",
    "WhisperCppBackend",
    "Qwen3ASRBackend",
    "PiperBackend",
    "Qwen3TTSBackend",
    "SmolVLMBackend",
    "SmolVLMVideoBackend",
    "LocalRenderBackend",
    "NullBackend",
    "NullDuplexBackend",
]

DEFAULT_PROMPT_IMAGE = "Describe what you see."
DEFAULT_PROMPT_VIDEO = "Describe what you see."
DEFAULT_PROMPT_ASR = "Transcribe the speech in this audio."

logger = logging.getLogger(__name__)

#: ``nice -n 10`` is a house rule for every subprocess on this box.
RUN_NICE: tuple[str, ...] = ("nice", "-n", "10")

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_WHISPER_TS_RE = re.compile(r"\[\d{1,2}:\d{2}:\d{2}\.\d{3}\s*-->\s*\d{1,2}:\d{2}:\d{2}\.\d{3}\]\s*")

_MIME_SUFFIX = {
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/flac": ".flac",
    "audio/ogg": ".ogg",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
}


def default_root() -> Path:
    """Workspace root (``FLUXER_WORKSPACE`` override; default is this repo)."""
    return Path(os.environ.get("FLUXER_WORKSPACE", "/home/agent/workspace/fluxer"))


def apply_gpu_env(env: Mapping[str, str] | None = None, *, root: str | Path | None = None) -> dict[str, str]:
    """Return ``env`` with the workspace Vulkan fix applied (mirrors gpu/gpu-env.sh)."""
    merged = dict(os.environ if env is None else env)
    gpu = Path(root or default_root()) / "gpu"
    merged.setdefault("VK_DRIVER_FILES", str(gpu / "nvidia_icd_egl.json"))
    egl = str(gpu / "extract-egl/usr/lib/x86_64-linux-gnu")
    existing = merged.get("LD_LIBRARY_PATH")
    merged["LD_LIBRARY_PATH"] = f"{egl}:{existing}" if existing else egl
    return merged


def _fmt_num(value: float | int) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _suffix_for(mime: str | None, fallback: str) -> str:
    key = str(mime or "").split(";")[0].strip().lower()
    return _MIME_SUFFIX.get(key, fallback)


def _materialize(part: Part, *, fallback_suffix: str, what: str) -> Path:
    """Resolve a part's ``data`` to an existing file path (bytes go via /tmp)."""
    data = part.data
    if isinstance(data, Path):
        path = data
    elif isinstance(data, str):
        path = Path(data)
    elif isinstance(data, (bytes, bytearray, memoryview)):
        tmp_dir = Path(tempfile.mkdtemp(prefix="fluxer-omni-"))
        path = tmp_dir / f"input{_suffix_for(part.mime, fallback_suffix)}"
        path.write_bytes(bytes(data))
    else:
        raise BackendError(f"{what}: unsupported part data type {type(data).__name__}")
    if not path.exists():
        raise BackendError(f"{what}: input not found: {path}")
    return path


# ── subprocess / HTTP plumbing ───────────────────────────────────────────────


@dataclass
class CommandResult:
    """Outcome of one bounded subprocess run (text stdout/stderr captured)."""

    argv: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


async def run_command(
    argv: Sequence[str],
    *,
    timeout: float = 60.0,
    stdin: str | bytes | None = None,
    env: Mapping[str, str] | None = None,
    nice: bool = True,
) -> CommandResult:
    """Run ``argv`` (nice'd, bounded) and capture its output.

    Raises :class:`BackendError` on timeout or spawn failure; a non-zero
    return code is returned in the result for the caller to judge.
    """
    argv = [str(a) for a in argv]
    full = [*RUN_NICE, *argv] if nice else list(argv)
    payload = stdin.encode("utf-8") if isinstance(stdin, str) else stdin
    try:
        proc = await asyncio.create_subprocess_exec(
            *full,
            stdin=asyncio.subprocess.PIPE if payload is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=dict(env) if env is not None else None,
        )
    except OSError as exc:
        raise BackendError(f"failed to spawn {full[0]!r}: {exc}") from exc
    try:
        out, err = await asyncio.wait_for(proc.communicate(input=payload), timeout=timeout)
    except asyncio.TimeoutError:
        with_exc = BackendError(f"command timed out after {timeout}s: {' '.join(argv)}")
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        raise with_exc from None
    return CommandResult(
        argv=full,
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=(out or b"").decode("utf-8", "replace"),
        stderr=(err or b"").decode("utf-8", "replace"),
    )


def _http_json(
    url: str,
    *,
    method: str = "GET",
    payload: Mapping[str, Any] | None = None,
    timeout: float = 30.0,
    headers: Mapping[str, str] | None = None,
) -> Any:
    """Minimal JSON HTTP call (sync; callers wrap in ``asyncio.to_thread``)."""
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib_request.Request(url, data=body, method=method)
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib_request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (local/LAN endpoints)
            raw = response.read().decode("utf-8", "replace")
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300] if exc.fp else ""
        raise BackendError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except urllib_error.URLError as exc:
        raise BackendError(f"cannot reach {url}: {exc.reason}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BackendError(f"non-JSON reply from {url}: {raw[:200]!r}") from exc


# ── output parsers (llama.cpp writes the reply to stdout, logs to stderr) ────


def parse_mtmd_stdout(stdout: str, *, prompt: str | None = None) -> str:
    """Strip ANSI + echo of ``prompt`` from a llama-mtmd-cli reply."""
    text = _ANSI_RE.sub("", stdout or "").strip()
    if prompt and prompt in text:
        text = text.rsplit(prompt, 1)[-1]
    return " ".join(text.split())


def parse_whisper_stdout(stdout: str) -> str:
    """whisper-cli may print ``[ts --> ts]`` lines even with ``-nt``; strip both."""
    text = _ANSI_RE.sub("", stdout or "")
    text = _WHISPER_TS_RE.sub("", text)
    return " ".join(text.split())


def parse_asr_stdout(stdout: str) -> str:
    """Qwen3-ASR emits ``language English<asr_text>…``; keep the transcript."""
    text = _ANSI_RE.sub("", stdout or "").strip()
    if "<asr_text>" in text:
        text = text.rsplit("<asr_text>", 1)[-1]
    return " ".join(text.split())


# ── registry core ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BackendSpec:
    """One registered backend: how to build it and what it is for."""

    name: str
    kind: str  # "sense" | "duplex"
    factory: Callable[..., Any]
    description: str = ""

    def build(self, **options: Any) -> Any:
        return self.factory(**options)


_REGISTRY: dict[str, BackendSpec] = {}


def register_backend(
    name: str,
    factory: Callable[..., Any],
    *,
    kind: str = "sense",
    description: str = "",
    replace: bool = False,
) -> BackendSpec:
    """Register ``factory`` under ``name`` (kind ``"sense"`` or ``"duplex"``)."""
    name = str(name).strip()
    if not name:
        raise ValueError("backend name must be non-empty")
    if kind not in ("sense", "duplex"):
        raise ValueError(f"backend kind must be 'sense' or 'duplex', got {kind!r}")
    if name in _REGISTRY and not replace:
        raise ValueError(f"backend {name!r} already registered (pass replace=True to override)")
    spec = BackendSpec(name=name, kind=kind, factory=factory, description=description)
    _REGISTRY[name] = spec
    return spec


def unregister_backend(name: str) -> None:
    _REGISTRY.pop(str(name), None)


def get_backend(name: str, **options: Any) -> Any:
    """Build the backend registered as ``name`` (factory gets ``**options``)."""
    spec = _REGISTRY.get(str(name))
    if spec is None:
        known = ", ".join(sorted(_REGISTRY)) or "<none>"
        raise BackendNotConfigured(f"unknown backend {name!r}; registered: {known}")
    try:
        return spec.factory(**options)
    except TypeError as exc:
        raise BackendError(f"cannot build backend {name!r}: {exc}") from exc


def list_backends(kind: str | None = None) -> list[str]:
    """Registered backend names (optionally filtered by kind), sorted."""
    names = [n for n, spec in _REGISTRY.items() if kind is None or spec.kind == kind]
    return sorted(names)


def describe_backends(kind: str | None = None) -> list[BackendSpec]:
    return [spec for _n, spec in sorted(_REGISTRY.items()) if kind is None or spec.kind == kind]


def backend_catalog() -> dict[str, str]:
    """``{name: kind}`` — the shape :func:`fluxer.omni.profile.resolve_profile` validates against."""
    return {spec.name: spec.kind for spec in _REGISTRY.values()}


# ── argv builders (pure, reused by the classes and the tests) ────────────────


def whisper_argv(binary: Path, model: Path, audio: Path, *, threads: int = 4, nots: bool = True, extra: Sequence[str] = ()) -> list[str]:
    argv = [str(binary), "-m", str(model), "-f", str(audio), "-t", str(threads)]
    if nots:
        argv.append("-nt")
    return [*argv, *extra]


def qwen3asr_argv(
    llama_dir: Path,
    model: Path,
    mmproj: Path,
    audio: Path,
    *,
    prompt: str = DEFAULT_PROMPT_ASR,
    n_predict: int = 256,
    ctx: int = 1024,  # verified-safe 6 GB profile — see feasibility doc §3.4
    ub: int = 64,
    b: int = 128,
    extra: Sequence[str] = (),
) -> list[str]:
    return [
        str(Path(llama_dir) / "llama-mtmd-cli"),
        "-m", str(model), "--mmproj", str(mmproj),
        "--audio", str(audio),
        "-p", prompt,
        "-n", str(n_predict), "-ub", str(ub), "-b", str(b), "-c", str(ctx),
        *extra,
    ]


def piper_argv(binary: Path, model: Path, out: Path, *, extra: Sequence[str] = ()) -> list[str]:
    return [str(binary), "--model", str(model), "--output_file", str(out), *extra]


def qwen3tts_argv(
    llama_dir: Path,
    model: Path,
    mmproj: Path,
    text: str,
    out: Path,
    *,
    lang: str = "en",
    speaker_file: Path | None = None,
    frames: int = 300,
    ctx: int = 1024,  # the -c 1024 gotcha is load-bearing (feasibility doc §3.4)
    ub: int = 64,
    b: int = 128,
    extra: Sequence[str] = (),
) -> list[str]:
    argv = [
        str(Path(llama_dir) / "llama-tts"),
        "-m", str(model), "-mm", str(mmproj),
        "-p", text,
        "--tts-lang", str(lang),
    ]
    if speaker_file is not None:
        argv += ["--tts-speaker-file", str(speaker_file)]
    argv += ["-o", str(out), "-n", str(frames), "-ub", str(ub), "-b", str(b), "-c", str(ctx), *extra]
    return argv


def smolvlm_argv(
    llama_dir: Path,
    model: Path,
    mmproj: Path,
    image: Path,
    *,
    prompt: str = DEFAULT_PROMPT_IMAGE,
    n_predict: int = 64,
    ctx: int = 2048,
    ub: int = 64,
    b: int = 128,
    extra: Sequence[str] = (),
) -> list[str]:
    return [
        str(Path(llama_dir) / "llama-mtmd-cli"),
        "-m", str(model), "--mmproj", str(mmproj),
        "--image", str(image),
        "-p", prompt,
        "-n", str(n_predict), "-ub", str(ub), "-b", str(b), "-c", str(ctx),
        *extra,
    ]


def smolvlm_video_argv(
    llama_dir: Path,
    model: Path,
    mmproj: Path,
    video: Path,
    *,
    fps: float = 2.0,
    prompt: str = DEFAULT_PROMPT_VIDEO,
    n_predict: int = 48,
    ctx: int = 8192,  # video needs ctx ~ frames; 2 s @ 2 fps needed 8192 (§3.4)
    ub: int = 64,
    b: int = 128,
    extra: Sequence[str] = (),
) -> list[str]:
    return [
        str(Path(llama_dir) / "llama-mtmd-cli"),
        "-m", str(model), "--mmproj", str(mmproj),
        "--video", str(video), "--video-fps", _fmt_num(fps),
        "-p", prompt,
        "-n", str(n_predict), "-ub", str(ub), "-b", str(b), "-c", str(ctx),
        *extra,
    ]


def _build_audio_chat_payload(text: str, audio_b64: str) -> dict[str, Any]:
    """OpenAI-compatible ``input_audio`` payload (llama-server, verified path)."""
    return {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "input_audio", "input_audio": {"data": audio_b64}},
                    {"type": "text", "text": text},
                ],
            }
        ],
        "temperature": 0.0,
    }


# ── builtin backends ─────────────────────────────────────────────────────────


class _LocalBackend:
    """Shared plumbing: workspace root, timeout, per-call GPU env."""

    name = "local.backend"
    default_timeout = 120.0

    def __init__(self, *, root: str | Path | None = None, timeout: float | None = None, gpu: bool = True) -> None:
        self.root = Path(root) if root is not None else default_root()
        self.timeout = float(timeout if timeout is not None else self.default_timeout)
        self.gpu = gpu

    def _env(self) -> dict[str, str] | None:
        return apply_gpu_env(root=self.root) if self.gpu else None

    def _llama_dir(self, override: str | Path | None = None) -> Path:
        return Path(override) if override is not None else self.root / "gpu/tools/llama-b10903"


class AgentBackend:
    """Marker for the thinker seam: the adapter core runs this slot.

    Binding ``text_out: agent`` documents *where thinking happens* so a
    stitched profile reads completely.  The engine cannot run the Hermes agent
    turn itself — provide a ``think`` hook to :class:`fluxer.omni.cascade.Cascade`
    (or service the slot in the caller) instead of calling ``process``.
    """

    name = "agent"

    async def process(self, part: Part) -> list[Part]:
        raise HostRequired(
            "backend 'agent' is a marker for the thinker seam: the fluxer adapter core "
            "services this slot. Use Cascade(think=...) or handle the text step in the caller."
        )


class LlamaServerBackend:
    """Thin HTTP client for a local ``llama-server`` (health + chat)."""

    name = "local.llama_server"
    default_timeout = 60.0

    def __init__(
        self,
        *,
        base_url: str,
        model: str | None = None,
        timeout: float | None = None,
        api_key: str | None = None,
    ) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.model = model
        self.timeout = float(timeout if timeout is not None else self.default_timeout)
        self.api_key = api_key

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    async def health(self) -> bool:
        """True when the server answers ``/health`` with ``status: ok`` (or legacy 200)."""
        try:
            payload = await asyncio.to_thread(
                _http_json, f"{self.base_url}/health", timeout=self.timeout, headers=self._headers()
            )
        except BackendError:
            return False
        if isinstance(payload, Mapping):
            return str(payload.get("status", "ok")).lower() in ("ok", "")
        return True

    async def chat(self, text: str, *, system: str | None = None, max_tokens: int | None = None) -> str:
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": text})
        payload: dict[str, Any] = {"messages": messages, "temperature": 0.0}
        if self.model:
            payload["model"] = self.model
        if max_tokens is not None:
            payload["max_tokens"] = int(max_tokens)
        reply = await asyncio.to_thread(
            _http_json,
            f"{self.base_url}/v1/chat/completions",
            method="POST",
            payload=payload,
            timeout=self.timeout,
            headers=self._headers(),
        )
        try:
            return str(reply["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise BackendError(f"unexpected chat reply from {self.base_url}: {str(reply)[:200]}") from exc

    async def process(self, part: Part) -> list[Part]:
        reply = await self.chat(part.text_of())
        return [Part.text(reply, backend=self.name)]


class WhisperCppBackend:
    """CPU speech→text via the installed whisper.cpp binary (verified path)."""

    name = "local.whispercpp"
    default_timeout = 120.0

    def __init__(
        self,
        *,
        root: str | Path | None = None,
        binary: str | Path | None = None,
        model: str | Path | None = None,
        threads: int = 4,
        timeout: float | None = None,
        gpu: bool = False,  # CPU path; kept for symmetry with the other backends
    ) -> None:
        self.root = Path(root) if root is not None else default_root()
        self.binary = Path(binary) if binary is not None else self.root / "gpu/tools/whisper-bin-ubuntu-x64/whisper-cli"
        self.model = Path(model) if model is not None else self.root / "models/ggml-tiny.en.bin"
        self.threads = int(threads)
        self.timeout = float(timeout if timeout is not None else self.default_timeout)

    async def process(self, part: Part) -> list[Part]:
        audio = _materialize(part, fallback_suffix=".wav", what=self.name)
        result = await run_command(
            whisper_argv(self.binary, self.model, audio, threads=self.threads),
            timeout=self.timeout,
        )
        if not result.ok:
            raise BackendError(f"whisper-cli rc={result.returncode}: {result.stderr.strip()[-300:]}")
        text = parse_whisper_stdout(result.stdout)
        return [Part.text(text, backend=self.name, audio=str(audio), source_language="en")]


class Qwen3ASRBackend:
    """GPU speech→text via llama-mtmd-cli + Qwen3-ASR GGUF (verified, §3.3).

    The other verified transport (``llama-server`` + ``input_audio`` base64,
    see ``docs/captures/omni-asr-server-evidence.md``) is implemented as the
    ``server_url`` option; it is *not* live-tested in wave 4 and requires a
    running server, so the CLI transport is the default.
    """

    name = "local.qwen3asr"
    default_timeout = 170.0

    def __init__(
        self,
        *,
        root: str | Path | None = None,
        llama_dir: str | Path | None = None,
        model: str | Path | None = None,
        mmproj: str | Path | None = None,
        prompt: str = DEFAULT_PROMPT_ASR,
        n_predict: int = 256,
        ctx: int = 1024,
        timeout: float | None = None,
        server_url: str | None = None,
        server_model: str | None = None,
    ) -> None:
        self.root = Path(root) if root is not None else default_root()
        self.llama_dir = Path(llama_dir) if llama_dir is not None else self.root / "gpu/tools/llama-b10903"
        base = self.root / "models/omni/qwen3-asr-0.6b"
        self.model = Path(model) if model is not None else base / "Qwen3-ASR-0.6B-Q8_0.gguf"
        self.mmproj = Path(mmproj) if mmproj is not None else base / "mmproj-Qwen3-ASR-0.6B-Q8_0.gguf"
        self.prompt = prompt
        self.n_predict = int(n_predict)
        self.ctx = int(ctx)
        self.timeout = float(timeout if timeout is not None else self.default_timeout)
        self.server_url = str(server_url).rstrip("/") if server_url else None
        self.server_model = server_model

    async def process(self, part: Part) -> list[Part]:
        audio = _materialize(part, fallback_suffix=".wav", what=self.name)
        meta_extra: dict[str, Any] = {}
        if self.server_url:
            text = await self._via_server(audio)
        else:
            argv = qwen3asr_argv(
                self.llama_dir, self.model, self.mmproj, audio,
                prompt=self.prompt, n_predict=self.n_predict, ctx=self.ctx,
            )
            result = await run_command(argv, timeout=self.timeout, env=apply_gpu_env(root=self.root))
            text = parse_asr_stdout(result.stdout)
            # The b10903 Vulkan build intermittently segfaults *after* printing
            # the reply (observed live: rc=-11 with a complete transcript on
            # stdout) — accept usable output, record the exit code.
            if not result.ok and not text:
                raise BackendError(f"llama-mtmd-cli rc={result.returncode}: {result.stderr.strip()[-300:]}")
            if not result.ok:
                meta_extra["exit_code"] = result.returncode
                meta_extra["warning"] = (
                    f"llama-mtmd-cli exited rc={result.returncode} after printing a transcript "
                    "(flaky teardown on this llama.cpp Vulkan build); output accepted"
                )
                logger.warning("qwen3asr: %s", meta_extra["warning"])
        return [Part.text(text, backend=self.name, audio=str(audio), language="English", **meta_extra)]

    async def _via_server(self, audio: Path) -> str:
        """Optional transport: llama-server + OpenAI-compatible ``input_audio``."""
        payload = _build_audio_chat_payload(self.prompt, base64.b64encode(audio.read_bytes()).decode("ascii"))
        url = f"{self.server_url}/v1/chat/completions"
        try:
            reply = await asyncio.to_thread(_http_json, url, method="POST", payload=payload, timeout=self.timeout)
            return str(reply["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise BackendError(f"unexpected ASR server reply: {str(reply)[:200]}") from exc


class PiperBackend:
    """CPU text→speech via the installed piper binary (verified; RTF ≈ 0.22)."""

    name = "local.piper"
    default_timeout = 60.0

    def __init__(
        self,
        *,
        root: str | Path | None = None,
        binary: str | Path | None = None,
        model: str | Path | None = None,
        timeout: float | None = None,
        gpu: bool = False,  # CPU path
    ) -> None:
        self.root = Path(root) if root is not None else default_root()
        self.binary = Path(binary) if binary is not None else self.root / "gpu/tools/piper/piper"
        self.model = Path(model) if model is not None else self.root / "models/en_US-lessac-medium.onnx"
        self.timeout = float(timeout if timeout is not None else self.default_timeout)

    async def process(self, part: Part) -> list[Part]:
        text = part.text_of()
        out_dir = Path(tempfile.mkdtemp(prefix="fluxer-omni-"))
        out = out_dir / "speech.wav"
        result = await run_command(
            piper_argv(self.binary, self.model, out),
            timeout=self.timeout,
            stdin=text + "\n",
        )
        if not result.ok:
            raise BackendError(f"piper rc={result.returncode}: {result.stderr.strip()[-300:]}")
        if not out.exists():
            raise BackendError("piper reported success but wrote no WAV")
        return [Part.audio(str(out), backend=self.name, voice=self.model.stem, text=text)]


class Qwen3TTSBackend:
    """GPU text→speech via ``llama-tts`` + Qwen3-TTS GGUF, voice cloning (§3.3).

    ``-c 1024`` is deliberate: the model's default context made the loader try
    a single ~917 MB Vulkan buffer and fail on the 6 GB card (doc §3.4).
    """

    name = "local.qwen3tts"
    default_timeout = 170.0

    def __init__(
        self,
        *,
        root: str | Path | None = None,
        llama_dir: str | Path | None = None,
        model: str | Path | None = None,
        mmproj: str | Path | None = None,
        speaker_file: str | Path | None = None,
        lang: str = "en",
        frames: int = 300,
        ctx: int = 1024,
        timeout: float | None = None,
    ) -> None:
        self.root = Path(root) if root is not None else default_root()
        self.llama_dir = Path(llama_dir) if llama_dir is not None else self.root / "gpu/tools/llama-b10903"
        base = self.root / "models/omni/qwen3-tts-1.7b"
        self.model = Path(model) if model is not None else base / "Qwen3-TTS-12Hz-1.7B-Base-Q4_K_M.gguf"
        self.mmproj = Path(mmproj) if mmproj is not None else base / "mmproj-Qwen3-TTS-12Hz-1.7B-Base-Q8_0.gguf"
        self.speaker_file = Path(speaker_file) if speaker_file is not None else None
        self.lang = lang
        self.frames = int(frames)
        self.ctx = int(ctx)
        self.timeout = float(timeout if timeout is not None else self.default_timeout)

    async def process(self, part: Part) -> list[Part]:
        text = part.text_of()
        out_dir = Path(tempfile.mkdtemp(prefix="fluxer-omni-"))
        out = out_dir / "speech.wav"
        argv = qwen3tts_argv(
            self.llama_dir, self.model, self.mmproj, text, out,
            lang=self.lang, speaker_file=self.speaker_file, frames=self.frames, ctx=self.ctx,
        )
        result = await run_command(argv, timeout=self.timeout, env=apply_gpu_env(root=self.root))
        # llama-tts on this Vulkan build occasionally segfaults at teardown
        # *after* writing a valid WAV (observed live: rc=-11, file complete and
        # re-transcribed correctly). Accept a written file, but record the exit
        # code so a caller can tell the two apart.
        wrote_output = out.exists() and out.stat().st_size > 44
        if not result.ok and not wrote_output:
            raise BackendError(f"llama-tts rc={result.returncode}: {result.stderr.strip()[-300:]}")
        if not wrote_output:
            raise BackendError("llama-tts reported success but wrote no WAV")
        meta: dict[str, Any] = {"text": text, "language": self.lang}
        if not result.ok:
            meta["exit_code"] = result.returncode
            meta["warning"] = (
                f"llama-tts exited rc={result.returncode} after writing a complete WAV "
                "(flaky teardown on this llama.cpp Vulkan build); output accepted"
            )
        return [Part.audio(str(out), backend=self.name, **meta)]


class SmolVLMBackend:
    """image→text captions via llama-mtmd-cli + SmolVLM2-256M (verified)."""

    name = "local.smolvlm"
    default_timeout = 150.0

    def __init__(
        self,
        *,
        root: str | Path | None = None,
        llama_dir: str | Path | None = None,
        model: str | Path | None = None,
        mmproj: str | Path | None = None,
        prompt: str = DEFAULT_PROMPT_IMAGE,
        n_predict: int = 64,
        ctx: int = 2048,
        timeout: float | None = None,
    ) -> None:
        self.root = Path(root) if root is not None else default_root()
        self.llama_dir = Path(llama_dir) if llama_dir is not None else self.root / "gpu/tools/llama-b10903"
        self.model = Path(model) if model is not None else self.root / "models/smolvlm2-256m.gguf"
        self.mmproj = Path(mmproj) if mmproj is not None else self.root / "models/mmproj-smolvlm2.gguf"
        self.prompt = prompt
        self.n_predict = int(n_predict)
        self.ctx = int(ctx)
        self.timeout = float(timeout if timeout is not None else self.default_timeout)

    async def caption_file(self, image: str | Path, prompt: str | None = None) -> str:
        """Caption one image file (the one reused by the video frame loop)."""
        argv = smolvlm_argv(
            self.llama_dir, self.model, self.mmproj, Path(image),
            prompt=prompt or self.prompt, n_predict=self.n_predict, ctx=self.ctx,
        )
        result = await run_command(argv, timeout=self.timeout, env=apply_gpu_env(root=self.root))
        text = parse_mtmd_stdout(result.stdout, prompt=prompt or self.prompt)
        if not result.ok and not text:
            raise BackendError(f"llama-mtmd-cli rc={result.returncode}: {result.stderr.strip()[-300:]}")
        if not result.ok:
            logger.warning(
                "smolvlm: llama-mtmd-cli exited rc=%s after printing a caption "
                "(flaky teardown on this llama.cpp Vulkan build); output accepted",
                result.returncode,
            )
        return text

    async def process(self, part: Part) -> list[Part]:
        image = _materialize(part, fallback_suffix=".jpg", what=self.name)
        caption = await self.caption_file(image, part.meta.get("prompt"))
        return [Part.text(caption, backend=self.name, image=str(image))]


class SmolVLMVideoBackend:
    """video→text via :class:`fluxer.video.LocalVideoDuplexer` (native ``--video`` first).

    The heavy lifting (ffmpeg sampling, timestamps, native-vs-frames choice)
    lives in ``fluxer.video.local_backend``; this class is the registry adapter
    and emits one text :class:`Part` per caption/summary event.
    """

    name = "local.smolvlm_video"
    default_timeout = 300.0

    def __init__(
        self,
        *,
        root: str | Path | None = None,
        fps: float = 2.0,
        mode: str = "auto",
        prompt: str = DEFAULT_PROMPT_VIDEO,
        max_frames: int = 12,
        width: int = 512,
        timeout: float | None = None,
    ) -> None:
        self.root = Path(root) if root is not None else default_root()
        self.fps = float(fps)
        self.mode = mode
        self.prompt = prompt
        self.max_frames = int(max_frames)
        self.width = int(width)
        self.timeout = float(timeout if timeout is not None else self.default_timeout)

    async def process(self, part: Part) -> list[Part]:
        from ..video.local_backend import LocalVideoDuplexer  # lazy: keep omni import-light

        video = _materialize(part, fallback_suffix=".mp4", what=self.name)
        duplexer = LocalVideoDuplexer(
            root=self.root, fps=self.fps, mode=self.mode, prompt=self.prompt,
            max_frames=self.max_frames, width=self.width, timeout=self.timeout,
        )
        parts: list[Part] = []
        async for event in duplexer.ingest_video(video, fps=self.fps, mode=self.mode, prompt=self.prompt):
            if event.kind in ("caption", "summary") and event.text:
                parts.append(
                    Part.text(event.text, backend=self.name, ts=event.ts, event_kind=event.kind, video=str(video))
                )
        await duplexer.close()
        return parts


class LocalRenderBackend:
    """text→video via :mod:`fluxer.video.render` (card slideshow / annotation).

    ``mode="card"`` (default) renders the text part as a slideshow;
    ``mode="annotate"`` consumes ``part.meta["src"]`` +
    ``part.meta["events"]`` and overlays caption events by timestamp.
    """

    name = "local.render"
    default_timeout = 300.0

    def __init__(
        self,
        *,
        root: str | Path | None = None,
        mode: str = "card",
        out_dir: str | Path | None = None,
        width: int = 640,
        height: int = 360,
        duration_per_line: float = 2.0,
        timeout: float | None = None,
    ) -> None:
        self.root = Path(root) if root is not None else default_root()
        self.mode = mode
        self.out_dir = Path(out_dir) if out_dir is not None else self.root / "models/omni/out"
        self.width = int(width)
        self.height = int(height)
        self.duration_per_line = float(duration_per_line)
        self.timeout = float(timeout if timeout is not None else self.default_timeout)

    async def process(self, part: Part) -> list[Part]:
        from ..video import render as video_render  # lazy

        self.out_dir.mkdir(parents=True, exist_ok=True)
        if self.mode == "annotate":
            src = part.meta.get("src")
            events = part.meta.get("events") or []
            if not src:
                raise BackendError("local.render annotate: part.meta['src'] is required")
            out = self.out_dir / f"annotated-{os.getpid()}-{int(time.time() * 1000)}.mp4"
            await video_render.annotate_video(src, events, out, gen_backend=part.meta.get("gen_backend"))
        elif self.mode == "card":
            out = self.out_dir / f"card-{os.getpid()}-{int(time.time() * 1000)}.mp4"
            await video_render.card_video(
                part.text_of().splitlines(),
                out,
                width=self.width,
                height=self.height,
                duration_per_line=self.duration_per_line,
                gen_backend=part.meta.get("gen_backend"),
            )
        else:
            raise BackendNotConfigured(f"local.render: unknown mode {self.mode!r}")
        return [Part.video(str(out), backend=self.name, mode=self.mode)]


class NullBackend:
    """Honest gap: a slot bound to ``null`` fails loudly with an explanation."""

    name = "null"

    def __init__(self, *, reason: str | None = None, **options: Any) -> None:
        self.reason = reason
        self.options = options

    async def process(self, part: Part) -> list[Part]:
        reason = self.reason or (
            "no backend is bound to this slot on this box (placeholder 'null'); "
            "see docs/omni-models-feasibility.md §2 for what is actually installed"
        )
        raise BackendNotConfigured(reason)


class NullDuplexBackend:
    """The unified (single-god-model) slot, honestly empty.

    No model that serves the senses in one process fits the 6 GB GTX 1060
    today (feasibility doc §1/§5).  This backend keeps profiles wireable: a
    ``mode: unified`` profile resolves, and the failure — with the exact config
    shape a future model should be configured with — happens when a session is
    opened rather than at config-parse time.
    """

    name = "null_duplex"

    def __init__(self, **options: Any) -> None:
        self.options = options

    async def open_session(self, **options: Any) -> Any:
        merged = {**self.options, **options}
        detail = "" if not merged else f" (configured options were ignored: {sorted(merged)})"
        raise BackendNotConfigured(
            "unified duplex session is not configured: no single-model omni that fits 6 GB is "
            "installed (Qwen2.5-Omni-3B download is deferred — docs/omni-models-feasibility.md §5)."
            f"{detail} To wire a future unified model, add a profile like "
            f"{json.dumps(UNIFIED_CONFIG_EXAMPLE['omni']['profiles']['unified-future'])} "
            "and register its backend with register_backend(name, factory, kind='duplex')."
        )


# ── builtin registration ─────────────────────────────────────────────────────

_BUILTIN_SPECS: tuple[tuple[str, Callable[..., Any], str, str], ...] = (
    ("agent", AgentBackend, "sense", "thinker seam marker — serviced by the adapter core"),
    ("local.llama_server", LlamaServerBackend, "sense", "llama-server HTTP client (health + chat)"),
    ("local.whispercpp", WhisperCppBackend, "sense", "whisper.cpp CPU speech→text"),
    ("local.qwen3asr", Qwen3ASRBackend, "sense", "Qwen3-ASR GGUF GPU speech→text (llama-mtmd-cli)"),
    ("local.piper", PiperBackend, "sense", "piper CPU text→speech"),
    ("local.qwen3tts", Qwen3TTSBackend, "sense", "Qwen3-TTS GGUF GPU text→speech (llama-tts)"),
    ("local.smolvlm", SmolVLMBackend, "sense", "SmolVLM2 image→text caption"),
    ("local.smolvlm_video", SmolVLMVideoBackend, "sense", "SmolVLM2 video→text (native --video / frames)"),
    ("local.render", LocalRenderBackend, "sense", "ffmpeg card/annotate video renderer"),
    ("null", NullBackend, "sense", "placeholder backend that fails loudly"),
    ("null_duplex", NullDuplexBackend, "duplex", "unified-model placeholder (not configured)"),
)


def register_builtin_backends() -> None:
    """(Re)register every builtin backend idempotently."""
    for name, factory, kind, description in _BUILTIN_SPECS:
        register_backend(name, factory, kind=kind, description=description, replace=True)


register_builtin_backends()
