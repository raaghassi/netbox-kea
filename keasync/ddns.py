"""kea-dhcp-ddns (D2) zone manager.

Derives D2's forward/reverse-ddns domains from NetBox DNS zones flagged
``ddns_enabled`` and config-sets them into kea-dhcp-ddns over its HTTP control
socket (kea-ddns-control Service). Forward vs reverse is split by the ``.arpa``
suffix. The TSIG key-name and the dns-server (the netbox-dns bridge) are reused
from D2's existing (bootstrap) config via config-get, so those stay defined in
one place (the kea chart) rather than duplicated here.

This is the DDNS analogue of the prefix->CB path: a netbox change (a zone's
ddns_enabled toggle) drives kea config, dynamically, with no restart.
"""

import logging

import requests

REVERSE_SUFFIXES = ('in-addr.arpa', 'ip6.arpa')


class DdnsManager:

    def __init__(self, netbox_url, netbox_token, d2_url, timeout=15,
                 username=None, password=None, zone_source=None):
        self.zones_url = (
            netbox_url.rstrip('/') + '/api/plugins/netbox-dns/zones/')
        self.headers = {
            'Authorization': 'Token ' + netbox_token,
            'Accept': 'application/json'}
        self.d2_url = d2_url
        self.timeout = timeout
        # HTTP basic auth toward D2's control socket (None = no auth)
        self.auth = (username, password or '') if username else None
        # Injectable zone-name source (callable -> list of names). The
        # in-process plugin daemon supplies an ORM query; default is the
        # NetBox REST API via netbox_url/netbox_token.
        self.zone_source = zone_source

    def _zone_names(self):
        """Active netbox-dns zones flagged ddns_enabled (handles pagination)."""

        if self.zone_source is not None:
            return self.zone_source()

        names = []
        url = (self.zones_url +
               '?cf_ddns_enabled=true&status=active&limit=0')
        while url:
            r = requests.get(url, headers=self.headers, timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
            names += [z['name'] for z in data.get('results', [])]
            url = data.get('next')
        return names

    def _d2(self, command, arguments=None):
        body = {'command': command}
        if arguments is not None:
            body['arguments'] = arguments
        r = requests.post(self.d2_url, json=body, timeout=self.timeout,
                          auth=self.auth)
        r.raise_for_status()
        res = r.json()
        if isinstance(res, list):
            res = res[0]
        if res.get('result') != 0:
            raise RuntimeError(
                'D2 {} failed: {}'.format(command, res.get('text')))
        return res

    def sync(self):
        """Reconcile D2's forward/reverse-ddns domains to the ddns_enabled zones."""

        cfg = self._d2('config-get')['arguments']['DhcpDdns']
        # Reuse the TSIG key-name + dns-server(s) from a bootstrap domain (kea
        # chart), so they aren't duplicated here.
        bootstrap = next(iter(
            (cfg.get('forward-ddns', {}).get('ddns-domains') or [])
            + (cfg.get('reverse-ddns', {}).get('ddns-domains') or [])
            + [{}]))
        key = bootstrap.get('key-name')
        servers = bootstrap.get('dns-servers', [])

        def domain(name):
            return {'name': name.rstrip('.') + '.', 'key-name': key,
                    'dns-servers': servers}

        forward, reverse = [], []
        for name in self._zone_names():
            bucket = reverse if name.rstrip('.').endswith(
                REVERSE_SUFFIXES) else forward
            bucket.append(domain(name))

        cfg.setdefault('forward-ddns', {})['ddns-domains'] = forward
        cfg.setdefault('reverse-ddns', {})['ddns-domains'] = reverse
        self._d2('config-set', {'DhcpDdns': cfg})
        logging.info(
            'D2 ddns sync: %d forward + %d reverse zone(s)',
            len(forward), len(reverse))
