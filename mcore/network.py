"""Validated, deterministic BitTorrent network settings (separate from the API)."""
from ipaddress import ip_address

DEFAULTS = {
    'listen_port': 51413,
    'listen_addresses': ['0.0.0.0'],
    'enable_dht': True,
    'enable_pex': True,
    'enable_utp': True,
    'enable_lsd': False,
    'enable_upnp': False,
    'enable_natpmp': False,
    'connections_limit': 200,
    'download_rate_limit': 0,
    'upload_rate_limit': 0,
    'outgoing_port_first': 0,
    'outgoing_port_last': 0,
}


def validate_torrent(value, api_port):
    if not isinstance(value, dict):
        raise ValueError('torrent must be a TOML table')
    unknown = value.keys() - DEFAULTS.keys()
    if unknown:
        raise ValueError('Unknown torrent settings: ' + ', '.join(sorted(unknown)))
    result = DEFAULTS | value
    for name, default in DEFAULTS.items():
        if isinstance(default, bool):
            if type(result[name]) is not bool:
                raise ValueError(f'torrent.{name} must be a boolean')
        elif isinstance(default, int):
            if type(result[name]) is not int or not 0 <= result[name] <= 2147483647:
                raise ValueError(f'torrent.{name} must be a non-negative 32-bit integer')
    port = result['listen_port']
    if not 1024 <= port <= 65535 or port == api_port:
        raise ValueError('torrent.listen_port must be 1024..65535 and differ from the API port')
    if not 1 <= result['connections_limit'] <= 100000:
        raise ValueError('torrent.connections_limit must be 1..100000')
    addresses = result['listen_addresses']
    if not isinstance(addresses, list) or not addresses or not all(isinstance(a, str) for a in addresses):
        raise ValueError('torrent.listen_addresses must be a non-empty array of IP addresses')
    result['listen_addresses'] = list(dict.fromkeys(str(ip_address(a)) for a in addresses))
    first, last = result['outgoing_port_first'], result['outgoing_port_last']
    if (first, last) != (0, 0):
        if not 1024 <= first <= last <= 65535:
            raise ValueError('Outgoing port range must be 0/0 or 1024 <= first <= last <= 65535')
        if first <= port <= last or first <= api_port <= last:
            raise ValueError('Outgoing port range must not overlap listening ports')
    return result


def session_settings(network):
    port = network['listen_port']
    interfaces = ','.join(f'[{a}]:{port}' if ':' in a else f'{a}:{port}' for a in network['listen_addresses'])
    first, last = network['outgoing_port_first'], network['outgoing_port_last']
    return {
        'listen_interfaces': interfaces,
        # A busy port is an error, not permission to silently use an unforwarded port.
        'max_retry_port_bind': 0,
        'listen_system_port_fallback': False,
        'enable_incoming_tcp': True,
        'enable_outgoing_tcp': True,
        'enable_incoming_utp': network['enable_utp'],
        'enable_outgoing_utp': network['enable_utp'],
        **{k: network[k] for k in ('enable_dht', 'enable_lsd', 'enable_upnp', 'enable_natpmp',
                                  'connections_limit', 'download_rate_limit', 'upload_rate_limit')},
        'outgoing_port': first,
        'num_outgoing_ports': last-first+1 if first else 0,
    }
