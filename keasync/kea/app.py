import logging
from copy import deepcopy
from ipaddress import ip_interface, ip_network

from .api import DHCP4API, FileAPI
from .exceptions import (DuplicateValue, KeaCmdError, SubnetNotEqual,
                         SubnetNotFound)

# Kea configuration keys
SUBNETS = 'subnet4'
USR_CTX = 'user-context'
POOLS = 'pools'
RESAS = 'reservations'
PREFIX = 'id'
IP_RANGE = 'netbox_ip_range_id'
IP_ADDR = 'netbox_ip_address_id'


def _autocommit(func):
    """ Decorator to autocommit changes after method execution """

    def wrapper(self, *args, **kwargs):
        res = func(self, *args, **kwargs)
        commit_arg = kwargs.get('commit')
        if commit_arg is True or (commit_arg is None and self.auto_commit):
            self.commit()
        return res
    return wrapper


def _boundaries(ip_range):
    """ Return a tuple of first and last ip_interface of the pool """

    # pool may be expressed by a "-" seperated range or by a network
    try:
        start, end = ip_range.split('-')
    except ValueError:
        net = ip_network(ip_range)
        return ip_interface(net.network_address), ip_interface(
            net.broadcast_address)
    else:
        return ip_interface(start), ip_interface(end)


class DHCP4App:

    def __init__(self, url=None, username=None, password=None):
        if url.startswith('http://') or url.startswith('https://'):
            self.api = DHCP4API(url, username=username, password=password)
        elif url.startswith('file://'):
            self.api = FileAPI(url.removeprefix('file://'))
        else:
            raise ValueError(
                'Kea URL must starts either with "http(s)://" or "file://"')
        self.conf = None
        self.commit_conf = None
        self._has_commit = False
        self.auto_commit = True

    def pull(self):
        """ Fetch configuration from DHCP server  """

        logging.info('pull running config from DHCP server')
        self.conf = self.api.get_conf()
        # Set minimal expected keys
        self.conf.setdefault(SUBNETS, [])
        for s in self.conf[SUBNETS]:
            for r in s.setdefault(RESAS, []):
                r.setdefault(USR_CTX, {}).setdefault(IP_ADDR, None)
            for p in s.setdefault(POOLS, []):
                p.setdefault(USR_CTX, {}).setdefault(IP_RANGE, None)

        self.commit_conf = deepcopy(self.conf)
        # A pull resynchronizes from the server: any staged-but-unpushed
        # commit is now stale, so discard it (pull acts as rollback).
        self._has_commit = False
        self.ip_uniqueness = self.conf.get('ip-reservations-unique', True)

    def commit(self):
        """ Record changes to the configuration. Return True if success """

        try:
            logging.debug('check configuration')
            self.api.raise_conf_error(self.conf)
        except KeaCmdError:
            # Drop current working config
            logging.error('config check failed, drop uncommited changes')
            self.conf = deepcopy(self.commit_conf)
            raise
        else:
            logging.debug('commit configuration')
            self.commit_conf = deepcopy(self.conf)
            self._has_commit = True
            return True

    def push(self):
        """ Update DHCP server configuration """

        if self._has_commit:
            logging.info('push configuration to runtime DHCP server')
            try:
                self.api.set_conf(self.commit_conf)
                logging.info('write configuration to permanent file')
                self.api.write_conf()
            except KeaCmdError as e:
                logging.error(f'config push or write rejected: {e}')

            self._has_commit = None
        else:
            logging.debug('no commit to push')

    def _check_commit(self, commit=None):
        """ Commit conf if required by argument or instance attribute """

        if commit is True or (commit is None and self.auto_commit):
            self.commit()

    @_autocommit
    def set_subnet(self, prefix_id, subnet_item):
        """ Replace subnet with prefix ID or append a new one """

        self._set_subnet(prefix_id, subnet_item, only_update_options=False)

    @_autocommit
    def update_subnet(self, prefix_id, subnet_item):
        """
        Update subnet options (preserve current reservations and pools). Raise
        SubnetNotEqual if network address differs, or SubnetNotFound if no
        subnet prefix ID matches.
        """

        self._set_subnet(prefix_id, subnet_item, only_update_options=True)

    def _set_subnet(self, prefix_id, subnet_item, only_update_options):
        """ Update subnet options, replace subnet or append a new one """

        try:
            subnet = subnet_item['subnet']
        except KeyError as e:
            raise TypeError(f'Missing mandatory subnet key: {e}')

        sfound = None
        for s in self.conf[SUBNETS]:
            if s[PREFIX] == prefix_id:
                sfound = s
                if s['subnet'] == subnet:
                    # No network addr change, no need to inspect other subnets
                    break
                elif only_update_options:
                    raise SubnetNotEqual(f'subnet {s["subnet"]} ≠ {subnet}')

            # Continue in order to check duplicates
            elif s['subnet'] == subnet:
                raise DuplicateValue(f'duplicate subnet {subnet}')

        subnet_item[PREFIX] = prefix_id
        if sfound:
            if only_update_options:
                logging.info(f'subnet {subnet}: update with {subnet_item}')
                # Preserve reservations and pools
                subnet_item[RESAS] = sfound[RESAS]
                subnet_item[POOLS] = sfound[POOLS]
            else:
                subnet_item.setdefault(RESAS, [])
                subnet_item.setdefault(POOLS, [])
                logging.info(f'subnet ID {prefix_id}: replace with {subnet}')
            # Clear current subnet (except reservations and pools) in order to
            # drop Kea default options, as they may conflict with our new
            # settings (like min/max-valid-lifetime against valid-lifetime).
            sfound.clear()
            sfound.update(subnet_item)
        elif only_update_options:
            raise SubnetNotFound(f'subnet ID {prefix_id}')
        else:
            subnet_item.setdefault(RESAS, [])
            subnet_item.setdefault(POOLS, [])
            logging.info(f'subnets: add {subnet}, ID {prefix_id}')
            self.conf[SUBNETS].append(subnet_item)

    @_autocommit
    def del_subnet(self, prefix_id, commit=None):
        logging.info(f'subnets: remove subnet {prefix_id} if it exists')
        self.conf[SUBNETS] = [
            s for s in self.conf[SUBNETS] if s[PREFIX] != prefix_id]

    @_autocommit
    def del_all_subnets(self):
        logging.info('delete all current subnets')
        self.conf[SUBNETS].clear()

    @_autocommit
    def set_pool(self, prefix_id, iprange_id, pool_item):
        """ Replace pool or append a new one """

        try:
            start, end = pool_item['pool'].split('-')
        except KeyError as e:
            raise TypeError(f'Missing mandatory pool key: {e}')

        pool_item.setdefault(USR_CTX, {})[IP_RANGE] = iprange_id
        ip_start, ip_end = ip_interface(start), ip_interface(end)

        def raise_conflict(p):
            pl = p.get('pool')
            if pl:
                s, e = _boundaries(pl)
                if s <= ip_start <= e or s <= ip_end <= e:
                    raise DuplicateValue(f'overlaps existing pool {pl}')

        self._set_subnet_item(
            prefix_id, POOLS, IP_RANGE, iprange_id, pool_item, raise_conflict,
            pool_item['pool'])

    @_autocommit
    def del_pool(self, iprange_id):
        self._del_prefix_item(POOLS, IP_RANGE, iprange_id)

    @_autocommit
    def set_reservation(self, prefix_id, ipaddr_id, resa_item):
        """ Replace host reservation or append a new one """

        for k in ('ip-address', 'hw-address'):
            if k not in resa_item:
                raise TypeError(f'Missing mandatory reservation key: {k}')

        resa_item.setdefault(USR_CTX, {})[IP_ADDR] = ipaddr_id

        # If replacing a reservation whose MAC changed, delete the stale
        # lease so Kea can assign the reserved IP to the new MAC.
        for s in self.conf[SUBNETS]:
            if s[PREFIX] == prefix_id:
                for r in s[RESAS]:
                    if (r[USR_CTX][IP_ADDR] == ipaddr_id and
                            r.get('hw-address') != resa_item['hw-address']):
                        ip = r.get('ip-address')
                        if ip:
                            logging.info(
                                f'reservation {ipaddr_id}: MAC changed, '
                                f'deleting stale lease for {ip}')
                            self._evict_lease(ip)
                break

        def raise_conflict(r):
            if r.get('hw-address') == resa_item['hw-address']:
                raise DuplicateValue(
                    f'duplicate hw-address={r["hw-address"]}')
            elif (self.ip_uniqueness and r.get('ip-address') ==
                    resa_item['ip-address']):
                raise DuplicateValue(f'duplicate address={r["ip-address"]}')

        self._set_subnet_item(
            prefix_id, RESAS, IP_ADDR, ipaddr_id, resa_item, raise_conflict,
            resa_item['hw-address'])

    @_autocommit
    def del_resa(self, ipaddr_id):
        # Delete any lease for the reserved IP before removing the
        # reservation, so stale leases don't block future reservations.
        for s in self.conf[SUBNETS]:
            for r in s[RESAS]:
                if r[USR_CTX][IP_ADDR] == ipaddr_id:
                    ip = r.get('ip-address')
                    if ip:
                        self._evict_lease(ip)
        self._del_prefix_item(RESAS, IP_ADDR, ipaddr_id)

    def _evict_lease(self, ip):
        """Delete the lease for a reserved IP so Kea can hand it to the new
        MAC. Applied immediately here; subclasses with a staged push phase
        override this to defer the live mutation until push."""

        self.api.del_lease4(ip)

    def _set_subnet_item(self, prefix_id, item_list, item_key, item_id, new,
                         raise_conflict, display):
        """ Replace either a pool or a host reservation """

        for s in self.conf[SUBNETS]:
            found = None
            if s[PREFIX] == prefix_id:
                # Prefix found
                for i in s[item_list]:
                    if i[USR_CTX][item_key] == item_id:
                        found = i
                    # Continue in order to check duplicates
                    else:
                        raise_conflict(i)

                if found:
                    logging.info(
                        f'subnet {prefix_id} > {item_list} > ID {item_id}: '
                        f'replace with {display}')
                    found.clear()
                    found.update(new)
                else:
                    logging.info(
                        f'subnet {prefix_id} > {item_list}: add {display}, '
                        f'ID {item_id}')
                    s[item_list].append(new)
                break
        else:
            raise SubnetNotFound(f'subnet {prefix_id}')

    def _del_prefix_item(self, item_list, item_key, item_id):
        """ Delete item from all subnets. Silently ignore non-existent item """

        logging.info(f'{item_list}: delete resa {item_id} if it exists')
        for s in self.conf[SUBNETS]:
            s[item_list] = [
                i for i in s[item_list] if i[USR_CTX][item_key] != item_id]
