# Evidence — llama-server HTTP audio-in test (Qwen3-ASR-0.6B), 2026-09-11 ~08:37 UTC
# Excerpt from the tracked server session output (session proc_8c70114181ef) and the client run.
# Raw full capture: re-runnable via the commands at the bottom.

## Server (llama-server, Vulkan, GTX 1060 6GB) — selected lines from stdout:

    [...] srv  llama_server: initializing, n_slots = 4, n_ctx_slot = 2048, kv_unified = 'true'
    0.02.355.540 I srv  llama_server: model loaded
    0.02.355.553 I srv  llama_server: listening on http://127.0.0.1:18099
    0.11.893.331 I slot get_availabl: id  3 | task -1 | selected slot by LRU, t_last = -1
    0.11.893.404 I slot launch_slot_: id  3 | task 0 | processing task, is_child = 0
    0.15.928.388 I slot print_timing: id  3 | task 0 | prompt eval time =    3795.63 ms /   171 tokens (   22.20 ms per token,    45.05 tokens per second)
    0.15.928.394 I slot print_timing: id  3 | task 0 |        eval time =     239.31 ms /    30 tokens (    8.25 ms per token,   121.18 tokens per second)
    0.15.928.396 I slot print_timing: id  3 | task 0 |       total time =    4034.94 ms /   201 tokens
    0.15.928.401 I slot print_timing: id  3 | task 0 |    graphs reused =         29

## Client (OpenAI-compatible /v1/chat/completions with base64 input_audio):

    POST http://127.0.0.1:18099/v1/chat/completions
    body: {"messages":[{"role":"user","content":[
             {"type":"input_audio","input_audio":{"data":"<base64 of models/jfk.wav>"}},
             {"type":"text","text":"Transcribe this audio."}]}], "max_tokens":128}

    response (4.1 s wall):
      language English<asr_text>And so, my fellow Americans, ask not what your country can do for you; ask what you can do for your country.

## Capability advertisement:

    GET /v1/models  ->  ... "capabilities":["completion","multimodal"] ...

## Re-run recipe:

    . gpu/gpu-env.sh
    nice -n 10 gpu/tools/llama-b10903/llama-server \
      -m models/omni/qwen3-asr-0.6b/Qwen3-ASR-0.6B-Q8_0.gguf \
      --mmproj models/omni/qwen3-asr-0.6b/mmproj-Qwen3-ASR-0.6B-Q8_0.gguf \
      -c 2048 --port 18099 --host 127.0.0.1 -ub 64 -b 128
    # then POST the JSON above (base64 of a wav/mp3/flac)
