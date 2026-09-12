"""Test CrispASR unified backend end-to-end."""
import asyncio, os, sys
os.environ['FLUXER_WORKSPACE'] = '/home/agent/workspace/fluxer-local'

from hermes_omni import register_builtin_backends, register_backend, resolve_profile
from hermes_omni.types import Part
from hermes_omni.profiles.cascade import duplex_session

register_builtin_backends()
from hermes_omni.backends.crispasr import CrispAsrMiniOmni2Backend
register_backend('local.crispasr_miniomni2', CrispAsrMiniOmni2Backend, kind='duplex')

async def main():
    cfg = {
        'default_profile': 'omni-unified',
        'profiles': {
            'omni-unified': {
                'mode': 'unified', 'tempo': 'realtime',
                'backend': {'backend': 'local.crispasr_miniomni2'},
                'senses': ['text', 'audio', 'image', 'video'],
            }
        }
    }
    profile = resolve_profile(cfg, name='omni-unified')
    session = await duplex_session(profile)
    print(f'Session opened: {type(session).__name__}')

    test_wav = '/home/agent/workspace/fluxer-local/models/jfk.wav'
    with open(test_wav, 'rb') as f:
        wav = f.read()
    print(f'Sending audio ({len(wav)} bytes)...')
    t0 = asyncio.get_event_loop().time()
    await session.send(Part.audio(wav))
    parts = []
    async for p in session.receive():
        parts.append(p)
    dt = asyncio.get_event_loop().time() - t0
    print(f'Done in {dt:.1f}s, {len(parts)} parts')
    for p in parts:
        if p.is_text:
            print(f'Text: "{p.text_of()[:120].strip()}"')
    print('✅ CrispASR unified backend works')

asyncio.run(main())