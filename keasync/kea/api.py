import json
import logging
import requests

from .exceptions import KeaServerError, KeaCmdError


class FileAPI:
    """ Fake Kea DHCP4 API that keep configuration in memory and file """

    def __init__(self, uri):
        self.config_file = uri
        if self.config_file:
            try:
                with open(self.config_file, 'rb') as f:
                    self.conf = json.load(f)
            except FileNotFoundError:
                self.conf = {}
        else:
            self.conf = {}

    def get_conf(self):
        return self.conf.get('Dhcp4', {})

    def raise_conf_error(self, config):
        json.dumps(config)

    def set_conf(self, config):
        self.raise_conf_error(config)
        self.conf['Dhcp4'] = config

    def write_conf(self):
        if self.config_file:
            with open(self.config_file, 'w') as f:
                json.dump(self.conf, f, indent=4)

    def del_lease4(self, ip_address):
        """ No-op for file-based API (no lease database) """
        pass


class DHCP4API:
    def __init__(self, url, username=None, password=None, timeout=15):
        self.url = url
        # Bounded requests (ddns.py convention): the lease pollers run
        # this client in unsupervised daemon threads, where a hung socket
        # would otherwise stall reflection forever without a log line.
        self.timeout = timeout
        self.session = requests.Session()
        if username:
            # session-level HTTP basic auth (requests: s.auth = tuple);
            # Kea rejects unauthenticated commands with result 403 when
            # the control socket has an authentication clause.
            self.session.auth = (username, password or '')

    def _request_kea(self, command, arguments={}):
        """ Send command to Kea APP """

        payload = {'command': command, 'service': ['dhcp4']}
        if arguments:
            payload['arguments'] = arguments
        try:
            r = self.session.post(self.url, json=payload,
                                  timeout=self.timeout)
            r.raise_for_status()
            rj = r.json()
        except requests.exceptions.RequestException as e:
            raise KeaServerError(f'API error: {e}')
        # One single command should return a list with one single item
        assert len(rj) == 1
        rj = rj.pop(0)
        result, text = rj['result'], rj.get('text')
        if result != 0:
            raise KeaCmdError(f'command "{command}" returns "{text}"')
        else:
            logging.debug(f'command "{command}" OK (text: {text})')
            return rj.get('arguments')

    def get_conf(self):
        """ Return configuration from Kea """

        return self._request_kea('config-get')['Dhcp4']

    def raise_conf_error(self, config):
        """ Test configuration and raise errors """

        self._request_kea('config-test', {'Dhcp4': config})

    def set_conf(self, config):
        """ Set configuration on DHCP server """

        self._request_kea('config-set', {'Dhcp4': config})

    def write_conf(self):
        """ On DHCP server write configuration to persistent storage """

        self._request_kea('config-write')

    def add_reservation(self, resa):
        """Create a host reservation in the hosts database (host_cmds).

        resa is the reservation map (subnet-id, ip-address, hw-address,
        hostname, user-context, ...). operation-target passed explicitly:
        the ARM documents 'database' as the write commands' effective
        default but the JSON specs disagree ('alternate'), so don't rely
        on it."""

        self._request_kea('reservation-add', {
            'reservation': resa, 'operation-target': 'database'})

    def del_reservation(self, subnet_id, ip_address):
        """Delete a host reservation by (subnet-id, ip-address) from the
        hosts database. Not-found is tolerated: the reconcile caller only
        cares that the reservation is gone ('Host not deleted (not
        found).' comes back as result 1, same code as real errors, so the
        text is the only discriminator the ARM documents)."""

        payload = {'command': 'reservation-del', 'service': ['dhcp4'],
                   'arguments': {'subnet-id': subnet_id,
                                 'ip-address': ip_address,
                                 'operation-target': 'database'}}
        try:
            r = self.session.post(self.url, json=payload,
                                  timeout=self.timeout)
            r.raise_for_status()
            rj = r.json()
        except requests.exceptions.RequestException as e:
            raise KeaServerError(f'API error: {e}')
        assert len(rj) == 1
        rj = rj.pop(0)
        result, text = rj['result'], rj.get('text')
        if result == 0:
            logging.info(f'reservation-del {ip_address}: {text}')
        elif result == 1 and 'not found' in (text or ''):
            logging.debug(f'reservation-del {ip_address}: {text}')
        else:
            raise KeaCmdError(f'command "reservation-del" returns "{text}"')

    def get_leases_page(self, from_, limit):
        """One page of leases (lease4-get-page; the ARM warns lease4-get-all
        can hang the server on large databases). from_ is 'start' for the
        first page, then the last address of the previous page. Returns []
        on result 3 ('empty'). The page is the last one when fewer than
        limit leases come back."""

        payload = {'command': 'lease4-get-page', 'service': ['dhcp4'],
                   'arguments': {'from': from_, 'limit': limit}}
        try:
            r = self.session.post(self.url, json=payload,
                                  timeout=self.timeout)
            r.raise_for_status()
            rj = r.json()
        except requests.exceptions.RequestException as e:
            raise KeaServerError(f'API error: {e}')
        assert len(rj) == 1
        rj = rj.pop(0)
        result = rj['result']
        if result == 0:
            return rj.get('arguments', {}).get('leases', [])
        if result == 3:
            return []
        raise KeaCmdError(
            f'command "lease4-get-page" returns "{rj.get("text")}"')

    def del_lease4(self, ip_address):
        """ Delete an IPv4 lease. Ignores non-existent leases (result 3). """

        payload = {'command': 'lease4-del', 'service': ['dhcp4'],
                   'arguments': {'ip-address': ip_address}}
        try:
            r = self.session.post(self.url, json=payload,
                                  timeout=self.timeout)
            r.raise_for_status()
            rj = r.json()
        except requests.exceptions.RequestException as e:
            raise KeaServerError(f'API error: {e}')
        assert len(rj) == 1
        rj = rj.pop(0)
        result, text = rj['result'], rj.get('text')
        if result == 0:
            logging.info(f'lease4-del {ip_address}: {text}')
        elif result == 3:
            logging.debug(f'lease4-del {ip_address}: {text}')
        else:
            raise KeaCmdError(f'command "lease4-del" returns "{text}"')
