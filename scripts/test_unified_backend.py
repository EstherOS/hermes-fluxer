"""E2E test of the unified Mini-Omni2 backend through the DuplexSession protocol."""
import asyncio
import os
import sys

sys.path.insert(0, '/home/agent/workspace/fluxer/packages/hermes-omni/src')
os.environ['FLUXER_WORKSPACE'] = '/home/agent/workspace/fluxer-local'

from hermes_omni import register_builtin_backends, register_backend, resolve_profile
from hermes_omni.types import Part
from hermes_omni.profiles.cascade import duplex_session

register_builtin_backends()
from hermes_omni.backends.miniomni2 import MiniOmni2DuplexBackend
register_backend('local.miniomni2_duplex', MiniOmni2DuplexBackend, kind='duplex')


async def main():
    profile = resolve_profile({
        'default_profile': 'omni-unified',
        'profiles': {
            'omni-unified': {
                'mode': 'unified',
                'tempo': 'realtime',
                'backend': {'backend': 'local.miniomni2_duplex', 'options': {'max_tokens': 2048}},
                'senses': ['text', 'audio', 'image', 'video'],
            }
        }
    }, name='omni-unified')

    print('=== E2E TEST: Unified Mini-Omni2 Backend ===')
    session = await duplex_session(profile)
    print(f'Session opened: {type(session).__name__}')

    test_wav = '/home/agent/workspace/fluxer-local/models/jfk.wav'
    with open(test_wav, 'rb') as f:
        wav_bytes = f.read()

    print(f'Sending audio ({len(wav_bytes)} bytes)...')
    t0 = asyncio.get_event_loop().time()
    await session.send(Part.audio(wav_bytes))

    print('Receiving...')
    parts = []
    async for part in session.receive():
        parts.append(part)
        data_size = len(part.data) if isinstance(part.data, (bytes, str)) else 0
        print(f'  Got part: kind={part.kind}, size={data_size} bytes')

    dt = asyncio.get_event_loop().time() - t0
    print(f'\nTotal: {dt:.1f}s, {len(parts)} parts: {[p.kind for p in parts]}')
    for p in parts:
        if p.is_text:
            print(f'  Text: "{p.text_of()[:120].strip()}"')
        elif p.kind == 'audio':
            print(f'  Audio: {len(p.data)} bytes')
    print()
    print('✅ Unified Mini-Omni2 backend works end-to-end')


if __name__ == '__main__':
    asyncio.run(main())