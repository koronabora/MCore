"""Opt-in live adapter check. Fetches metadata only, without starting payload downloads."""
import asyncio
import libtorrent as lt
from mcore.config import load
from mcore.jackett import Jackett


async def main():
    adapter = Jackett(load()['jackett'])
    try:
        for provider in ('rutracker', 'kinozal'):
            rows = await adapter.search(provider, 'Интерстеллар')
            assert rows, provider + ': no video releases'
            row = rows[0]
            source = await adapter.download(row['download']) if row['download'] else row['Magnet']
            if isinstance(source, bytes):
                info = lt.torrent_info(source)
                kind = 'torrent file; files=' + str(info.num_files())
            else:
                params = lt.parse_magnet_uri(source)
                assert params.info_hashes.has_v1() or params.info_hashes.has_v2()
                kind = 'magnet'
            print(provider, 'video releases:', len(rows), 'metadata:', kind, flush=True)
    finally:
        await adapter.client.aclose()


asyncio.run(main())
