"""Unit tests for the omni engine seams (wave 4): registry, profiles, cascade.

Conventions match the rest of the fluxer suite: no pytest-asyncio — async
paths run via ``asyncio.run`` inside sync test functions.  Subprocess work is
mocked at ``fluxer.omni.registry.run_command`` (except one real, bounded
timeout test of the runner itself).
"""

from __future__ import annotations

import asyncio
import base64
import time
from pathlib import Path

import pytest

from fluxer.omni import cascade, profile, registry, types
from fluxer.omni.registry import (
    AgentBackend,
    CommandResult,
    LlamaServerBackend,
    NullBackend,
    NullDuplexBackend,
    PiperBackend,
    Qwen3ASRBackend,
    Qwen3TTSBackend,
    SmolVLMBackend,
    WhisperCppBackend,
    parse_asr_stdout,
    parse_mtmd_stdout,
    parse_whisper_stdout,
    piper_argv,
    qwen3asr_argv,
    qwen3tts_argv,
    smolvlm_argv,
    smolvlm_video_argv,
    whisper_argv,
)

RUN = asyncio.run

REG = registry


# ── helpers ──────────────────────────────────────────────────────────────────


class RecordingRunner:
    """Async stand-in for registry.run_command: records calls, replays results."""

    def __init__(self, result: CommandResult | None = None, *, writes: Path | None = None):
        self.calls: list[dict] = []
        self.result = result
        self.writes = writes  # file the fake command should create (argv[-2] for -o)

    async def __call__(self, argv, *, timeout=60.0, stdin=None, env=None, nice=True):
        self.calls.append({"argv": [str(a) for a in argv], "timeout": timeout, "stdin": stdin, "env": env, "nice": nice})
        if self.writes is not None:
            argv_list = [str(a) for a in argv]
            if "-o" in argv_list:
                Path(argv_list[argv_list.index("-o") + 1]).parent.mkdir(parents=True, exist_ok=True)
                Path(argv_list[argv_list.index("-o") + 1]).write_bytes(b"RIFF-fake")
            else:
                self.writes.write_bytes(b"RIFF-fake")
        if self.result is not None:
            return self.result
        return CommandResult(argv=[str(a) for a in argv], returncode=0, stdout="", stderr="")


@pytest.fixture
def tmp_wav(tmp_path: Path) -> Path:
    path = tmp_path / "input.wav"
    path.write_bytes(b"RIFF....WAVEfmt ")
    return path


# ── registry core ────────────────────────────────────────────────────────────


def test_registry_register_get_list_describe_unregister():
    class Thing:
        def __init__(self, **opts):
            self.opts = opts

    spec = REG.register_backend("t.thing", Thing, kind="sense", description="a thing")
    try:
        assert spec.name == "t.thing"
        built = REG.get_backend("t.thing", answer=42)
        assert isinstance(built, Thing) and built.opts == {"answer": 42}
        assert "t.thing" in REG.list_backends()
        assert "t.thing" in REG.list_backends(kind="sense")
        assert "t.thing" not in REG.list_backends(kind="duplex")
        described = {s.name: s for s in REG.describe_backends()}
        assert described["t.thing"].description == "a thing"
        assert REG.backend_catalog()["t.thing"] == "sense"
        with pytest.raises(ValueError, match="already registered"):
            REG.register_backend("t.thing", Thing)
        REG.register_backend("t.thing", Thing, replace=True)  # explicit replace works
    finally:
        REG.unregister_backend("t.thing")
    assert "t.thing" not in REG.list_backends()


def test_registry_get_unknown_and_kind_validation():
    with pytest.raises(types.BackendNotConfigured, match="unknown backend"):
        REG.get_backend("no.such.backend")
    with pytest.raises(ValueError, match="kind"):
        REG.register_backend("t.badkind", lambda: None, kind="wat")
    with pytest.raises(ValueError, match="non-empty"):
        REG.register_backend("", lambda: None)


def test_builtin_backends_registered_and_idempotent():
    names = REG.list_backends()
    for expected in (
        "agent",
        "local.llama_server",
        "local.whispercpp",
        "local.qwen3asr",
        "local.piper",
        "local.qwen3tts",
        "local.smolvlm",
        "local.smolvlm_video",
        "local.render",
        "null",
    ):
        assert expected in names, expected
    assert REG.list_backends(kind="duplex") == ["null_duplex"]
    REG.register_builtin_backends()  # idempotent (replace=True)
    assert REG.list_backends() == names


# ── argv builders ────────────────────────────────────────────────────────────


def test_argv_builders_carry_the_verified_flags():
    whisper = whisper_argv(Path("/b/whisper-cli"), Path("/m/tiny.en.bin"), Path("/a.wav"), threads=4)
    assert whisper == ["/b/whisper-cli", "-m", "/m/tiny.en.bin", "-f", "/a.wav", "-t", "4", "-nt"]

    asr = qwen3asr_argv(Path("/llama"), Path("/asr.gguf"), Path("/mm.gguf"), Path("/a.wav"))
    assert asr[:1] == ["/llama/llama-mtmd-cli"]
    assert "--audio" in asr and asr[asr.index("--audio") + 1] == "/a.wav"
    assert asr[asr.index("-c") + 1] == "1024"  # the 6 GB gotcha
    assert asr[asr.index("-ub") + 1] == "64" and asr[asr.index("-b") + 1] == "128"

    tts = qwen3tts_argv(Path("/llama"), Path("/tts.gguf"), Path("/mm.gguf"), "hi", Path("/out.wav"), speaker_file=Path("/ref.wav"))
    assert tts[0] == "/llama/llama-tts"
    assert tts[tts.index("-c") + 1] == "1024"
    assert tts[tts.index("--tts-speaker-file") + 1] == "/ref.wav"
    assert tts[tts.index("--tts-lang") + 1] == "en"
    assert tts[tts.index("-o") + 1] == "/out.wav"
    no_speaker = qwen3tts_argv(Path("/l"), Path("/m"), Path("/p"), "hi", Path("/o.wav"))
    assert "--tts-speaker-file" not in no_speaker

    assert piper_argv(Path("/p/piper"), Path("/v.onnx"), Path("/o.wav")) == [
        "/p/piper", "--model", "/v.onnx", "--output_file", "/o.wav",
    ]

    image = smolvlm_argv(Path("/llama"), Path("/smol.gguf"), Path("/mm.gguf"), Path("/f.jpg"))
    assert image[image.index("--image") + 1] == "/f.jpg"
    assert image[image.index("-c") + 1] == "2048"

    video = smolvlm_video_argv(Path("/llama"), Path("/smol.gguf"), Path("/mm.gguf"), Path("/c.mp4"), fps=2)
    assert video[video.index("--video") + 1] == "/c.mp4"
    assert video[video.index("--video-fps") + 1] == "2"
    assert video[video.index("-c") + 1] == "8192"  # §3.4: video needs big ctx


def test_output_parsers():
    assert parse_mtmd_stdout("\x1b[1mDescribe what you see.\x1b[0m A black puppy.") == "Describe what you see. A black puppy."
    assert parse_mtmd_stdout("Describe what you see.  A black puppy.", prompt="Describe what you see.") == "A black puppy."
    assert parse_asr_stdout("language English<asr_text>And so, my fellow Americans.") == "And so, my fellow Americans."
    assert parse_asr_stdout("plain transcript") == "plain transcript"
    assert parse_whisper_stdout("[00:00:00.000 --> 00:00:05.000]  hello there\n[00:00:05.000 --> 00:00:09.000]  world") == "hello there world"


def test_audio_chat_payload_shape():
    payload = REG._build_audio_chat_payload("prompt", base64.b64encode(b"bytes").decode())
    content = payload["messages"][0]["content"]
    assert content[0]["type"] == "input_audio"
    assert content[0]["input_audio"]["data"] == base64.b64encode(b"bytes").decode()
    assert content[1] == {"type": "text", "text": "prompt"}


# ── subprocess backends (mocked runner: argv / timeout / env assertions) ─────


def test_whispercpp_backend_argv_timeout_and_parse(monkeypatch, tmp_wav):
    runner = RecordingRunner(CommandResult(argv=[], returncode=0, stdout="[00:00:00.000 --> 00:00:11.000]  And so my fellow Americans.", stderr=""))
    monkeypatch.setattr(REG, "run_command", runner)
    backend = WhisperCppBackend(root="/root", binary="/b/whisper-cli", model="/m/tiny.en.bin", threads=4, timeout=77.0)
    parts = RUN(backend.process(types.Part.audio(tmp_wav)))
    assert [p.kind for p in parts] == ["text"]
    assert "And so my fellow Americans." in parts[0].data
    call = runner.calls[0]
    assert call["argv"] == ["/b/whisper-cli", "-m", "/m/tiny.en.bin", "-f", str(tmp_wav), "-t", "4", "-nt"]
    assert call["timeout"] == 77.0


def test_backend_nonzero_rc_raises_backenderror(monkeypatch, tmp_wav):
    runner = RecordingRunner(CommandResult(argv=[], returncode=1, stdout="", stderr="boom"))
    monkeypatch.setattr(REG, "run_command", runner)
    with pytest.raises(types.BackendError, match="rc=1"):
        RUN(WhisperCppBackend(root="/r", timeout=5).process(types.Part.audio(tmp_wav)))


def test_qwen3asr_backend_cli_transport(monkeypatch, tmp_wav):
    runner = RecordingRunner(CommandResult(argv=[], returncode=0, stdout="language English<asr_text>And so.", stderr=""))
    monkeypatch.setattr(REG, "run_command", runner)
    backend = Qwen3ASRBackend(root="/root", llama_dir="/llama", model="/m.gguf", mmproj="/mm.gguf")
    parts = RUN(backend.process(types.Part.audio(tmp_wav)))
    assert parts[0].data == "And so."
    call = runner.calls[0]
    assert call["argv"][0] == "/llama/llama-mtmd-cli"
    assert call["argv"][call["argv"].index("-c") + 1] == "1024"
    assert call["env"]["VK_DRIVER_FILES"].endswith("nvidia_icd_egl.json")  # GPU env applied
    assert call["env"]["LD_LIBRARY_PATH"]


def test_qwen3asr_backend_server_transport(monkeypatch, tmp_wav):
    captured = {}

    def fake_http(url, *, method="GET", payload=None, timeout=30.0, headers=None):
        captured.update(url=url, method=method, payload=payload)
        return {"choices": [{"message": {"content": "server transcript"}}]}

    monkeypatch.setattr(REG, "_http_json", fake_http)
    backend = Qwen3ASRBackend(root="/root", server_url="http://127.0.0.1:8080/")
    parts = RUN(backend.process(types.Part.audio(tmp_wav)))
    assert parts[0].data == "server transcript"
    assert captured["url"] == "http://127.0.0.1:8080/v1/chat/completions"
    blob = captured["payload"]["messages"][0]["content"][0]["input_audio"]["data"]
    assert base64.b64decode(blob) == tmp_wav.read_bytes()


def test_bytes_input_materializes_temp_file(monkeypatch):
    runner = RecordingRunner(CommandResult(argv=[], returncode=0, stdout="hello", stderr=""))
    monkeypatch.setattr(REG, "run_command", runner)
    parts = RUN(WhisperCppBackend(root="/r").process(types.Part.audio(b"RIFFdata", mime="audio/wav")))
    assert parts[0].data == "hello"
    played = runner.calls[0]["argv"][runner.calls[0]["argv"].index("-f") + 1]
    assert played.endswith(".wav") and Path(played).exists()


def test_piper_backend_stdin_and_output(monkeypatch, tmp_path):
    out_holder = {"path": None}

    async def runner(argv, *, timeout=60.0, stdin=None, env=None, nice=True):
        out_holder["path"] = Path(argv[argv.index("--output_file") + 1])
        out_holder["path"].write_bytes(b"RIFF-fake")
        out_holder["stdin"] = stdin
        return CommandResult(argv=[str(a) for a in argv], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(REG, "run_command", runner)
    backend = PiperBackend(root="/r", binary="/b/piper", model="/v.onnx")
    parts = RUN(backend.process(types.Part.text("Hello from Fluxer.")))
    assert parts[0].kind == "audio"
    assert out_holder["stdin"] == "Hello from Fluxer.\n"
    assert Path(parts[0].data).read_bytes() == b"RIFF-fake"


def test_piper_backend_missing_output_raises(monkeypatch):
    runner = RecordingRunner(CommandResult(argv=[], returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(REG, "run_command", runner)
    with pytest.raises(types.BackendError, match="wrote no WAV"):
        RUN(PiperBackend(root="/r").process(types.Part.text("hi")))


def test_qwen3tts_backend_flags_and_output(monkeypatch):
    runner = RecordingRunner(writes=None)

    async def write_runner(argv, *, timeout=60.0, stdin=None, env=None, nice=True):
        out = Path(argv[argv.index("-o") + 1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"RIFF" + b"\x00" * 200)
        runner.calls.append({"argv": [str(a) for a in argv], "timeout": timeout, "env": env})
        return CommandResult(argv=[str(a) for a in argv], returncode=0, stdout="generated 80 frames", stderr="")

    monkeypatch.setattr(REG, "run_command", write_runner)
    backend = Qwen3TTSBackend(root="/r", llama_dir="/l", model="/m.gguf", mmproj="/p.gguf", speaker_file="/ref.wav", frames=120)
    parts = RUN(backend.process(types.Part.text("Speech please.")))
    argv = runner.calls[0]["argv"]
    assert argv[argv.index("-c") + 1] == "1024" and argv[argv.index("-n") + 1] == "120"
    assert argv[argv.index("--tts-speaker-file") + 1] == "/ref.wav"
    assert runner.calls[0]["env"]["VK_DRIVER_FILES"]
    assert Path(parts[0].data).exists()


def test_qwen3tts_backend_accepts_written_wav_despite_teardown_crash(monkeypatch):
    """llama-tts can segfault at teardown after writing a valid WAV (observed live)."""

    async def crashy_runner(argv, *, timeout=60.0, stdin=None, env=None, nice=True):
        out = Path(argv[argv.index("-o") + 1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"RIFF" + b"\x00" * 200)
        return CommandResult(argv=[str(a) for a in argv], returncode=-11, stdout="", stderr="I wrote .../speech.wav")

    monkeypatch.setattr(REG, "run_command", crashy_runner)
    backend = Qwen3TTSBackend(root="/r", llama_dir="/l", model="/m.gguf", mmproj="/p.gguf")
    parts = RUN(backend.process(types.Part.text("hi")))
    assert parts[0].kind == "audio"
    assert parts[0].meta["exit_code"] == -11
    assert "warning" in parts[0].meta

    async def hard_fail_runner(argv, *, timeout=60.0, stdin=None, env=None, nice=True):
        return CommandResult(argv=[str(a) for a in argv], returncode=1, stdout="", stderr="ErrorOutOfDeviceMemory")

    monkeypatch.setattr(REG, "run_command", hard_fail_runner)
    with pytest.raises(types.BackendError, match="rc=1"):
        RUN(backend.process(types.Part.text("hi")))


def test_gpu_backends_accept_output_after_teardown_crash(monkeypatch, tmp_wav, tmp_path):
    """rc=-11 with usable output is accepted (with a warning); bare crashes still raise."""
    image = tmp_path / "f.jpg"
    image.write_bytes(b"\xff\xd8\xff")

    runner = RecordingRunner(CommandResult(argv=[], returncode=-11, stdout="language English<asr_text>And so.", stderr=""))
    monkeypatch.setattr(REG, "run_command", runner)
    parts = RUN(Qwen3ASRBackend(root="/r", llama_dir="/l").process(types.Part.audio(tmp_wav)))
    assert parts[0].data == "And so."
    assert parts[0].meta["exit_code"] == -11 and "warning" in parts[0].meta

    runner = RecordingRunner(CommandResult(argv=[], returncode=-11, stdout="Describe what you see. A puppy.", stderr=""))
    monkeypatch.setattr(REG, "run_command", runner)
    parts = RUN(SmolVLMBackend(root="/r", llama_dir="/l").process(types.Part.image(image)))
    assert parts[0].data == "A puppy."

    empty = RecordingRunner(CommandResult(argv=[], returncode=-11, stdout="", stderr="segfault"))
    monkeypatch.setattr(REG, "run_command", empty)
    with pytest.raises(types.BackendError, match="rc=-11"):
        RUN(Qwen3ASRBackend(root="/r", llama_dir="/l").process(types.Part.audio(tmp_wav)))
    with pytest.raises(types.BackendError, match="rc=-11"):
        RUN(SmolVLMBackend(root="/r", llama_dir="/l").process(types.Part.image(image)))


def test_smolvlm_backend_parses_and_sets_gpu_env(monkeypatch, tmp_path):
    image = tmp_path / "f.jpg"
    image.write_bytes(b"\xff\xd8\xff")
    runner = RecordingRunner(CommandResult(argv=[], returncode=0, stdout="\x1b[1mDescribe what you see.\x1b[0m A black puppy on a deck.", stderr=""))
    monkeypatch.setattr(REG, "run_command", runner)
    backend = SmolVLMBackend(root="/r", llama_dir="/l", model="/m.gguf", mmproj="/p.gguf")
    parts = RUN(backend.process(types.Part.image(image)))
    assert parts[0].data == "A black puppy on a deck."
    call = runner.calls[0]
    assert call["argv"][call["argv"].index("--image") + 1] == str(image)
    assert call["argv"][call["argv"].index("-c") + 1] == "2048"
    assert call["env"]["VK_DRIVER_FILES"]


def test_smolvlm_video_backend_delegates_to_video_package(monkeypatch, tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00\x00\x00")
    from fluxer.video import local_backend as video_local

    captured: dict = {}

    class FakeDuplexer:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def ingest_video(self, path, *, fps=None, mode=None, prompt=None):
            captured["ingest"] = {"path": str(path), "fps": fps, "mode": mode, "prompt": prompt}
            yield video_local.VideoEvent(0.0, "caption", "a dog", {"mode": "native"})
            yield video_local.VideoEvent(0.5, "caption", "a second caption", {})

        async def close(self):
            captured["closed"] = True

    monkeypatch.setattr(video_local, "LocalVideoDuplexer", FakeDuplexer)
    backend = REG.SmolVLMVideoBackend(root="/r", fps=1.5, mode="native", prompt="watch")
    parts = RUN(backend.process(types.Part.video(video)))
    assert [p.data for p in parts] == ["a dog", "a second caption"]
    assert captured["ingest"]["mode"] == "native"
    assert captured["fps"] == 1.5
    assert captured["closed"] is True


def test_null_and_agent_backends_fail_loudly():
    with pytest.raises(types.BackendNotConfigured, match="null"):
        RUN(NullBackend().process(types.Part.text("x")))
    with pytest.raises(types.HostRequired, match="adapter core"):
        RUN(AgentBackend().process(types.Part.text("x")))


def test_null_duplex_documents_unified_shape():
    backend = REG.get_backend("null_duplex")
    with pytest.raises(types.BackendNotConfigured) as excinfo:
        RUN(backend.open_session())
    message = str(excinfo.value)
    assert "unified" in message and "mode" in message and "register_backend" in message
    assert "not configured" in message


def test_llama_server_backend(monkeypatch):
    calls: list[dict] = []

    def fake_http(url, *, method="GET", payload=None, timeout=30.0, headers=None):
        calls.append({"url": url, "method": method, "payload": payload})
        if url.endswith("/health"):
            return {"status": "ok"}
        return {"choices": [{"message": {"content": "a reply"}}]}

    monkeypatch.setattr(REG, "_http_json", fake_http)
    backend = LlamaServerBackend(base_url="http://127.0.0.1:8081/", model="qwen")
    assert RUN(backend.health()) is True
    parts = RUN(backend.process(types.Part.text("hello")))
    assert parts[0].data == "a reply"
    assert calls[1]["payload"]["model"] == "qwen"

    def broken_http(url, *, method="GET", payload=None, timeout=30.0, headers=None):
        return {"unexpected": True}

    monkeypatch.setattr(REG, "_http_json", broken_http)
    with pytest.raises(types.BackendError, match="unexpected chat reply"):
        RUN(backend.chat("hello"))


def test_llama_server_health_false_on_transport_error(monkeypatch):
    def failing_http(url, *, method="GET", payload=None, timeout=30.0, headers=None):
        raise types.BackendError("cannot reach")

    monkeypatch.setattr(REG, "_http_json", failing_http)
    assert RUN(LlamaServerBackend(base_url="http://127.0.0.1:9").health()) is False


def test_run_command_real_subprocess_paths():
    # real /bin/echo round-trip
    result = RUN(REG.run_command(["/bin/echo", "hello"], timeout=10))
    assert result.ok and result.stdout.strip() == "hello"
    assert result.argv[0] == "nice"  # nice -n 10 prefix (house rule)
    # real, bounded timeout (sleep killed well before it would finish)
    started = time.monotonic()
    with pytest.raises(types.BackendError, match="timed out"):
        RUN(REG.run_command(["sleep", "5"], timeout=0.3))
    assert time.monotonic() - started < 4.0


def test_run_command_spawn_failure():
    # without the nice prefix a missing binary is a spawn failure (OSError)…
    with pytest.raises(types.BackendError, match="failed to spawn"):
        RUN(REG.run_command(["/definitely/not/a/binary"], timeout=5, nice=False))
    # …with it, `nice` reports the failure as rc=127 in the result
    result = RUN(REG.run_command(["/definitely/not/a/binary"], timeout=5))
    assert not result.ok and result.returncode == 127


# ── profile parsing ──────────────────────────────────────────────────────────


def test_default_fallback_profile():
    for cfg in (None, {}, {"omni": {}}, {"other": 1}):
        resolved = profile.resolve_profile(cfg)
        assert resolved.name == profile.DEFAULT_PROFILE_NAME
        assert resolved.mode == "stitched"
        assert resolved.source == "builtin"
        assert resolved.binding("audio_in").backend == "local.whispercpp"
        assert resolved.binding("audio_out").backend == "local.piper"
        assert resolved.binding("text_out").backend == "agent"


def test_parse_stitched_profile_string_and_mapping_bindings():
    cfg = {
        "omni": {
            "default_profile": "local",
            "profiles": {
                "local": {
                    "mode": "stitched",
                    "bindings": {
                        "audio_in": "local.whispercpp",
                        "audio_out": {"backend": "local.piper", "options": {"model": "/v.onnx"}},
                    },
                }
            },
        }
    }
    resolved = profile.resolve_profile(cfg)
    assert resolved.name == "local" and resolved.source == "config"
    assert resolved.binding("audio_in") == types.SenseBinding("local.whispercpp", {})
    assert resolved.binding("audio_out").options == {"model": "/v.onnx"}
    # named resolution works too
    assert profile.resolve_profile(cfg, "local").name == "local"


def test_parse_unified_profile():
    resolved = profile.resolve_profile(
        {"omni": {"profiles": {"grey": {"mode": "unified", "backend": "null_duplex", "senses": ["audio", "VIDEO", "text_in"], "options": {"model": "future"}}}}},
        "grey",
    )
    assert resolved.mode == "unified"
    assert resolved.senses == ("audio", "video", "text")  # normalized + deduped
    assert resolved.backend.backend == "null_duplex"
    assert resolved.backend.options["model"] == "future"
    assert resolved.bindings == {}


def test_profile_missing_name_lists_available():
    cfg = {"omni": {"profiles": {"a": {"mode": "stitched", "bindings": {"audio_in": "local.whispercpp"}}}}}
    with pytest.raises(types.ProfileError, match=r"available: \['a'\]"):
        profile.resolve_profile(cfg, "nope")


def test_profile_unknown_mode_and_empty_bindings():
    with pytest.raises(types.ProfileError, match="missing/unknown mode"):
        profile.parse_profile("x", {"mode": "godmode", "bindings": {"audio_in": "local.whispercpp"}}, backend_kinds=None)
    with pytest.raises(types.ProfileError, match="non-empty 'bindings'"):
        profile.parse_profile("x", {"mode": "stitched", "bindings": {}}, backend_kinds=None)


def test_profile_typo_slot_gets_did_you_mean():
    with pytest.raises(types.ProfileError) as excinfo:
        profile.parse_profile(
            "x", {"mode": "stitched", "bindings": {"audoi_in": "local.whispercpp"}}, backend_kinds=None
        )
    assert "did you mean 'audio_in'" in str(excinfo.value)


def test_profile_unknown_and_mismatched_backends():
    catalog = {"local.piper": "sense", "null_duplex": "duplex"}
    with pytest.raises(types.ProfileError, match="unknown backend"):
        profile.parse_profile("x", {"mode": "stitched", "bindings": {"audio_in": "local.nope"}}, backend_kinds=catalog)
    with pytest.raises(types.ProfileError, match="duplex backend"):
        profile.parse_profile("x", {"mode": "stitched", "bindings": {"audio_in": "null_duplex"}}, backend_kinds=catalog)
    with pytest.raises(types.ProfileError, match="sense backend"):
        profile.parse_profile("x", {"mode": "unified", "backend": "local.piper", "senses": ["audio"]}, backend_kinds=catalog)
    # resolution against the *real* registry catalog (lazy default) also validates
    with pytest.raises(types.ProfileError, match="unknown backend"):
        profile.resolve_profile(
            {"omni": {"profiles": {"x": {"mode": "stitched", "bindings": {"audio_in": "local.nope"}}}}}, "x"
        )


def test_profile_multiple_without_default_errors_and_single_is_implicit():
    cfg = {
        "omni": {
            "profiles": {
                "a": {"mode": "stitched", "bindings": {"audio_in": "local.whispercpp"}},
                "b": {"mode": "stitched", "bindings": {"audio_in": "local.qwen3asr"}},
            }
        }
    }
    with pytest.raises(types.ProfileError, match="multiple profiles"):
        profile.resolve_profile(cfg)
    single = {"omni": {"profiles": {"only": {"mode": "stitched", "bindings": {"audio_in": "local.whispercpp"}}}}}
    assert profile.resolve_profile(single).name == "only"


def test_profile_warnings_for_unknown_keys():
    resolved = profile.parse_profile(
        "x",
        {"mode": "stitched", "bindings": {"audio_in": "local.whispercpp"}, "surprise": 1},
        backend_kinds=None,
    )
    assert any("surprise" in w for w in resolved.warnings)
    # section-level unknown keys warn too but still resolve
    resolved2 = profile.resolve_profile(
        {"omni": {"profiles": {"x": {"mode": "stitched", "bindings": {"audio_in": "local.whispercpp"}}}, "extra_key": 1}},
        "x",
    )
    assert any("extra_key" in w for w in resolved2.warnings)


def test_binding_from_config_errors():
    with pytest.raises(types.ProfileError, match="backend"):
        types.SenseBinding.from_config({"options": {}})
    with pytest.raises(types.ProfileError, match="unknown binding key"):
        types.SenseBinding.from_config({"backend": "x", "wat": 1})
    with pytest.raises(types.ProfileError, match="string or mapping"):
        types.SenseBinding.from_config(42)


def test_require_binding_raises_with_context():
    resolved = profile.parse_profile("x", {"mode": "stitched", "bindings": {"audio_in": "local.whispercpp"}}, backend_kinds=None)
    with pytest.raises(types.BackendNotConfigured, match="audio_out"):
        resolved.require_binding("audio_out")


# ── cascade ──────────────────────────────────────────────────────────────────


class FakeASR:
    name = "fake.asr"

    async def process(self, part):
        return [types.Part.text("transcribed text", backend=self.name, audio=str(part.data))]


class FakeTTS:
    name = "fake.tts"

    def __init__(self):
        self.seen: list[str] = []

    async def process(self, part):
        self.seen.append(part.text_of())
        return [types.Part.audio(b"WAV:" + part.text_of().encode(), backend=self.name)]


def _fake_catalog(fakes: dict):
    def get(name, **opts):
        return fakes[name]

    return get


def _stitched(*slots: str) -> profile.ResolvedProfile:
    bindings = {slot: {"backend": f"fake.{slot.split('_')[0]}{slot.split('_')[1]}"} for slot in slots}
    return profile.parse_profile("fake", {"mode": "stitched", "bindings": bindings}, backend_kinds=None)


def test_cascade_audio_understand_only():
    asr = FakeASR()
    prof = _stitched("audio_in")
    c = cascade.Cascade(prof, get_backend=_fake_catalog({"fake.audioin": asr}))
    out = RUN(c.collect(types.Part.audio("input.wav")))
    assert [(p.kind, p.data) for p in out] == [("text", "transcribed text")]


def test_cascade_think_and_express_audio():
    asr, tts = FakeASR(), FakeTTS()

    async def think(text, parts):
        assert text == "transcribed text"
        return "agent reply"

    prof = _stitched("audio_in", "audio_out")
    c = cascade.Cascade(prof, get_backend=_fake_catalog({"fake.audioin": asr, "fake.audioout": tts}), think=think)
    out = RUN(c.collect(types.Part.audio("input.wav"), output_senses=("audio",)))
    assert [(p.kind, p.data) for p in out] == [
        ("text", "transcribed text"),
        ("text", "agent reply"),
        ("audio", b"WAV:agent reply"),
    ]
    assert tts.seen == ["agent reply"]


def test_cascade_text_input_not_echoed_and_str_think_normalization():
    prof = _stitched("audio_out")
    tts = FakeTTS()
    c = cascade.Cascade(prof, get_backend=_fake_catalog({"fake.audioout": tts}), think=lambda text, parts: "reply only")
    out = RUN(c.collect(types.Part.text("user message"), output_senses=("audio",)))
    assert [p.data for p in out] == ["reply only", b"WAV:reply only"]


def test_cascade_missing_bindings_raise():
    prof = _stitched("audio_out")
    c = cascade.Cascade(prof, get_backend=_fake_catalog({"fake.audioout": FakeTTS()}))
    with pytest.raises(types.BackendNotConfigured, match="audio_in"):
        RUN(c.collect(types.Part.audio("x.wav")))
    prof2 = _stitched("audio_in")
    c2 = cascade.Cascade(prof2, get_backend=_fake_catalog({"fake.audioin": FakeASR()}))
    with pytest.raises(types.BackendNotConfigured, match="audio_out"):
        RUN(c2.collect(types.Part.audio("x.wav"), output_senses=("audio",)))


def test_cascade_rejects_unified_profile():
    prof = profile.parse_profile("u", {"mode": "unified", "backend": "null_duplex", "senses": ["audio"]}, backend_kinds=None)
    with pytest.raises(types.OmniError, match="stitched-only"):
        cascade.Cascade(prof)


def test_cascade_session_fifo_and_close():
    prof = _stitched("audio_in")
    c = cascade.Cascade(prof, get_backend=_fake_catalog({"fake.audioin": FakeASR()}))

    async def main():
        session = c.session()
        await session.open()
        received = []

        async def collect():
            async for part in session.receive():
                received.append(part.data)

        drain = asyncio.ensure_future(collect())
        await session.send(types.Part.audio("one.wav"))
        await session.close()
        await asyncio.wait_for(drain, timeout=5)
        return session, received

    session, received = RUN(main())
    assert received == ["transcribed text"]
    assert session.sent == 1 and session.emitted == 1
    with pytest.raises(types.OmniError, match="closed"):
        RUN(session.send(types.Part.audio("two.wav")))


def test_duplex_session_helper_stitched_and_unified():
    prof = _stitched("audio_in")
    c = cascade.Cascade(prof, get_backend=_fake_catalog({"fake.audioin": FakeASR()}))

    async def main():
        session = await cascade.duplex_session(prof, get_backend=_fake_catalog({"fake.audioin": FakeASR()}))
        assert isinstance(session, cascade.CascadeSession)
        await session.close()

        unified = profile.parse_profile("u", {"mode": "unified", "backend": "null_duplex", "senses": ["audio"]}, backend_kinds=None)
        with pytest.raises(types.BackendNotConfigured, match="not configured"):
            await cascade.duplex_session(unified)

    RUN(main())


def test_cascade_from_config_uses_registry():
    registered = []
    try:
        REG.register_backend("t.cascade.asr", FakeASR, replace=True)
        registered.append("t.cascade.asr")
        cfg = {"omni": {"profiles": {"p": {"mode": "stitched", "bindings": {"audio_in": "t.cascade.asr"}}}}}
        c = cascade.Cascade.from_config(cfg, "p")
        out = RUN(c.collect(types.Part.audio("x.wav")))
        assert out[0].data == "transcribed text"
    finally:
        for name in registered:
            REG.unregister_backend(name)


# ── types ────────────────────────────────────────────────────────────────────


def test_part_helpers():
    part = types.Part.text("hi", source="test")
    assert part.sense is types.Sense.TEXT and part.is_text and part.meta["source"] == "test"
    assert types.Part.audio(b"x").mime == "audio/wav"
    with pytest.raises(types.BackendError, match="expected a text part"):
        types.Part.audio("x").text_of()
    assert types.slot_name(types.Sense.AUDIO, types.Direction.IN) == "audio_in"
    assert types.slot_name("video", "out") == "video_out"
    assert types.is_slot("audio_in") and not types.is_slot("audio_sideways")


def test_unified_config_example_shape():
    example = types.UNIFIED_CONFIG_EXAMPLE["omni"]["profiles"]["unified-future"]
    assert example["mode"] == "unified"
    assert "base_url" in example["options"] and "modalities" in example["options"]
