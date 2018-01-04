#
# Copyright 2012-2017 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301  USA
#
# Refer to the README and COPYING files for full details of the license
#
from __future__ import absolute_import
import os
import io

import six

from nose.plugins.attrib import attr

from vdsm.network import ipwrapper
from vdsm.network import sysctl
from vdsm.network.ip.address import prefix2netmask
from vdsm.network.link import nic
from vdsm.network.link.bond import Bond
from vdsm.network.link.bond.sysfs_driver import BONDING_MASTERS
from vdsm.network.link.iface import random_iface_name
from vdsm.network.netinfo import addresses, bonding, misc, nics, routes
from vdsm.network.netinfo.cache import get
from vdsm.network.netlink import waitfor

from modprobe import RequireBondingMod
from testlib import mock
from testlib import VdsmTestCase as TestCaseBase
from testValidation import ValidateRunningAsRoot
from testValidation import broken_on_ci

from .nettestlib import dnsmasq_run, dummy_device, veth_pair, wait_for_ipv6


# speeds defined in ethtool
ETHTOOL_SPEEDS = set([10, 100, 1000, 2500, 10000])


@attr(type='unit')
class TestNetinfo(TestCaseBase):

    def test_netmask_conversions(self):
        path = os.path.join(os.path.dirname(__file__), "netmaskconversions")
        with open(path) as netmaskFile:
            for line in netmaskFile:
                if line.startswith('#'):
                    continue
                bitmask, address = [value.strip() for value in line.split()]
                self.assertEqual(prefix2netmask(int(bitmask)), address)
        self.assertRaises(ValueError, prefix2netmask, -1)
        self.assertRaises(ValueError, prefix2netmask, 33)

    def test_speed_on_an_iface_that_does_not_support_speed(self):
        self.assertEqual(nic.speed('lo'), 0)

    def test_speed_in_range(self):
        for d in nics.nics():
            s = nic.speed(d)
            self.assertFalse(s < 0)
            self.assertTrue(s in ETHTOOL_SPEEDS or s == 0)

    @mock.patch.object(nic, 'iface')
    @mock.patch.object(nics.io, 'open')
    def test_valid_nic_speed(self, mock_io_open, mock_iface):
        IS_UP = True
        values = ((b'0', IS_UP, 0),
                  (b'-10', IS_UP, 0),
                  (six.b(str(2 ** 16 - 1)), IS_UP, 0),
                  (six.b(str(2 ** 32 - 1)), IS_UP, 0),
                  (b'123', IS_UP, 123),
                  (b'', IS_UP, 0),
                  (b'', not IS_UP, 0),
                  (b'123', not IS_UP, 0))

        for passed, is_nic_up, expected in values:
            mock_io_open.return_value = io.BytesIO(passed)
            mock_iface.return_value.is_oper_up.return_value = is_nic_up

            self.assertEqual(nic.speed('fake_nic'), expected)

    def test_dpdk_device_speed(self):
        self.assertEqual(nic.speed('dpdk0'), 0)

    def test_dpdk_operstate_always_up(self):
        self.assertEqual(nics.operstate('dpdk0'), nics.OPERSTATE_UP)

    @mock.patch.object(bonding, 'permanent_address', lambda: {})
    @mock.patch('vdsm.network.netinfo.cache.RunningConfig')
    def test_get_non_existing_bridge_info(self, mock_runningconfig):
        # Getting info of non existing bridge should not raise an exception,
        # just log a traceback. If it raises an exception the test will fail as
        # it should.
        mock_runningconfig.return_value.networks = {'fake': {'bridged': True}}
        get()

    @mock.patch.object(bonding, 'permanent_address', lambda: {})
    @mock.patch('vdsm.network.netinfo.cache.getLinks')
    @mock.patch('vdsm.network.netinfo.cache.RunningConfig')
    def test_get_empty(self, mock_networks, mock_getLinks):
        result = {}
        result.update(get())
        self.assertEqual(result['networks'], {})
        self.assertEqual(result['bridges'], {})
        self.assertEqual(result['nics'], {})
        self.assertEqual(result['bondings'], {})
        self.assertEqual(result['vlans'], {})

    def test_ipv4_to_mapped(self):
        self.assertEqual('::ffff:127.0.0.1',
                         addresses.IPv4toMapped('127.0.0.1'))

    def test_get_device_by_ip(self):
        NL_ADDRESS4 = {'label': 'iface0',
                       'address': '127.0.0.1/32',
                       'family': 'inet'}
        NL_ADDRESS6 = {'label': 'iface1',
                       'address': '2001::1:1:1/48',
                       'family': 'inet6'}
        NL_ADDRESSES = [NL_ADDRESS4, NL_ADDRESS6]

        with mock.patch.object(addresses.nl_addr, 'iter_addrs',
                               lambda: NL_ADDRESSES):
            for nl_addr in NL_ADDRESSES:
                self.assertEqual(
                    nl_addr['label'],
                    addresses.getDeviceByIP(nl_addr['address'].split('/')[0]))

    @mock.patch.object(ipwrapper.Link, '_hiddenNics', ['hid*'])
    @mock.patch.object(ipwrapper.Link, '_hiddenBonds', ['jb*'])
    @mock.patch.object(ipwrapper.Link, '_fakeNics', ['fake*'])
    @mock.patch.object(ipwrapper.Link, '_detectType', lambda x: None)
    @mock.patch.object(ipwrapper, '_bondExists', lambda x: x == 'jbond')
    @mock.patch.object(misc, 'getLinks')
    def test_nics(self, mock_getLinks):
        """
        managed by vdsm: em, me, fake0, fake1
        not managed due to hidden bond (jbond) enslavement: me0, me1
        not managed due to being hidden nics: hid0, hideous
        """
        mock_getLinks.return_value = self._LINKS_REPORT

        self.assertEqual(set(nics.nics()), set(['em', 'me', 'fake', 'fake0']))

    # Creates a test fixture so that nics() reports:
    # physical nics: em, me, me0, me1, hid0 and hideous
    # dummies: fake and fake0
    # bonds: jbond (over me0 and me1)
    _LINKS_REPORT = [
        ipwrapper.Link(address='f0:de:f1:da:aa:e7', index=2,
                       linkType=ipwrapper.LinkType.NIC, mtu=1500,
                       name='em', qdisc='pfifo_fast', state='up'),
        ipwrapper.Link(address='ff:de:f1:da:aa:e7', index=3,
                       linkType=ipwrapper.LinkType.NIC, mtu=1500,
                       name='me', qdisc='pfifo_fast', state='up'),
        ipwrapper.Link(address='ff:de:fa:da:aa:e7', index=4,
                       linkType=ipwrapper.LinkType.NIC, mtu=1500,
                       name='hid0', qdisc='pfifo_fast', state='up'),
        ipwrapper.Link(address='ff:de:11:da:aa:e7', index=5,
                       linkType=ipwrapper.LinkType.NIC, mtu=1500,
                       name='hideous', qdisc='pfifo_fast', state='up'),
        ipwrapper.Link(address='66:de:f1:da:aa:e7', index=6,
                       linkType=ipwrapper.LinkType.NIC, mtu=1500,
                       name='me0', qdisc='pfifo_fast', state='up',
                       master='jbond'),
        ipwrapper.Link(address='66:de:f1:da:aa:e7', index=7,
                       linkType=ipwrapper.LinkType.NIC, mtu=1500,
                       name='me1', qdisc='pfifo_fast', state='up',
                       master='jbond'),
        ipwrapper.Link(address='ff:aa:f1:da:aa:e7', index=34,
                       linkType=ipwrapper.LinkType.DUMMY, mtu=1500,
                       name='fake0', qdisc='pfifo_fast', state='up'),
        ipwrapper.Link(address='ff:aa:f1:da:bb:e7', index=35,
                       linkType=ipwrapper.LinkType.DUMMY, mtu=1500,
                       name='fake', qdisc='pfifo_fast', state='up'),
        ipwrapper.Link(address='66:de:f1:da:aa:e7', index=419,
                       linkType=ipwrapper.LinkType.BOND, mtu=1500,
                       name='jbond', qdisc='pfifo_fast', state='up')
    ]

    @attr(type='integration')
    @ValidateRunningAsRoot
    @mock.patch.object(ipwrapper.Link, '_fakeNics', ['veth_*', 'dummy_*'])
    def test_fake_nics(self):
        with veth_pair() as (v1a, v1b):
            with dummy_device() as d1:
                fakes = set([d1, v1a, v1b])
                _nics = nics.nics()
                self.assertTrue(fakes.issubset(_nics),
                                'Fake devices %s are not listed in nics '
                                '%s' % (fakes, _nics))

        with veth_pair(prefix='mehv_') as (v2a, v2b):
            with dummy_device(prefix='mehd_') as d2:
                hiddens = set([d2, v2a, v2b])
                _nics = nics.nics()
                self.assertFalse(hiddens.intersection(_nics), 'Some of '
                                 'hidden devices %s is shown in nics %s' %
                                 (hiddens, _nics))

    @mock.patch.object(misc, 'open', create=True)
    def test_get_ifcfg(self, mock_open):
        gateway = '1.1.1.1'
        netmask = '255.255.0.0'

        ifcfg = "GATEWAY0={}\nNETMASK={}\n".format(gateway, netmask)
        ifcfg_stream = six.StringIO(ifcfg)
        mock_open.return_value.__enter__.return_value = ifcfg_stream

        resulted_ifcfg = misc.getIfaceCfg('eth0')

        self.assertEqual(resulted_ifcfg['GATEWAY'], gateway)
        self.assertEqual(resulted_ifcfg['NETMASK'], netmask)

    @mock.patch.object(misc, 'open', create=True)
    def test_missing_ifcfg_file(self, mock_open):
        mock_open.return_value.__enter__.side_effect = IOError()

        ifcfg = misc.getIfaceCfg('eth0')

        self.assertEqual(ifcfg, {})

    @broken_on_ci('Bond options scanning is fragile on CI',
                  exception=AssertionError)
    @attr(type='integration')
    @ValidateRunningAsRoot
    @RequireBondingMod
    def test_get_bonding_options(self):
        INTERVAL = '12345'
        bondName = random_iface_name()

        with open(BONDING_MASTERS, 'w') as bonds:
            bonds.write('+' + bondName)
            bonds.flush()

            try:  # no error is anticipated but let's make sure we can clean up
                self.assertEqual(
                    self._bond_opts_without_mode(bondName), {},
                    'This test fails when a new bonding option is added to '
                    'the kernel. Please run vdsm-tool dump-bonding-options` '
                    'and retest.')

                with open(bonding.BONDING_OPT % (bondName, 'miimon'),
                          'w') as opt:
                    opt.write(INTERVAL)

                self.assertEqual(self._bond_opts_without_mode(bondName),
                                 {'miimon': INTERVAL})

            finally:
                bonds.write('-' + bondName)

    @staticmethod
    def _bond_opts_without_mode(bond_name):
        opts = Bond(bond_name).options
        opts.pop('mode')
        return opts

    def test_get_gateway(self):
        TEST_IFACE = 'test_iface'
        # different tables but the gateway is the same so it should be reported
        DUPLICATED_GATEWAY = {TEST_IFACE: [
            {
                'destination': 'none',
                'family': 'inet',
                'gateway': '12.34.56.1',
                'oif': TEST_IFACE,
                'oif_index': 8,
                'scope': 'global',
                'source': None,
                'table': 203569230,  # lucky us, we got the address 12.34.56.78
            }, {
                'destination': 'none',
                'family': 'inet',
                'gateway': '12.34.56.1',
                'oif': TEST_IFACE,
                'oif_index': 8,
                'scope': 'global',
                'source': None,
                'table': 254,
            }]}
        SINGLE_GATEWAY = {TEST_IFACE: [DUPLICATED_GATEWAY[TEST_IFACE][0]]}

        gateway = routes.get_gateway(SINGLE_GATEWAY, TEST_IFACE)
        self.assertEqual(gateway, '12.34.56.1')
        gateway = routes.get_gateway(DUPLICATED_GATEWAY, TEST_IFACE)
        self.assertEqual(gateway, '12.34.56.1')

    @broken_on_ci('IPv6 not supported on travis', name='TRAVIS_CI')
    @attr(type='integration')
    @ValidateRunningAsRoot
    def test_ip_info(self):
        IPV4_ADDR1 = '192.168.99.2'
        IPV4_GATEWAY1 = '192.168.99.1'
        IPV4_ADDR2 = '192.168.199.2'
        IPV4_GATEWAY2 = '192.168.199.1'
        IPV4_ADDR3 = '192.168.200.2'
        IPV4_NETMASK = '255.255.255.0'
        IPV4_PREFIX_LENGTH = 24
        IPV6_ADDR = '2607:f0d0:1002:51::4'
        IPV6_PREFIX_LENGTH = 64

        IPV4_ADDR1_CIDR = self._cidr_form(IPV4_ADDR1, IPV4_PREFIX_LENGTH)
        IPV4_ADDR2_CIDR = self._cidr_form(IPV4_ADDR2, IPV4_PREFIX_LENGTH)
        IPV4_ADDR3_CIDR = self._cidr_form(IPV4_ADDR3, 32)
        IPV6_ADDR_CIDR = self._cidr_form(IPV6_ADDR, IPV6_PREFIX_LENGTH)

        with dummy_device() as device:
            with waitfor.waitfor_ipv4_addr(device, address=IPV4_ADDR1_CIDR):
                ipwrapper.addrAdd(device, IPV4_ADDR1, IPV4_PREFIX_LENGTH)
            with waitfor.waitfor_ipv4_addr(device, address=IPV4_ADDR2_CIDR):
                ipwrapper.addrAdd(device, IPV4_ADDR2, IPV4_PREFIX_LENGTH)
            with waitfor.waitfor_ipv6_addr(device, address=IPV6_ADDR_CIDR):
                ipwrapper.addrAdd(
                    device, IPV6_ADDR, IPV6_PREFIX_LENGTH, family=6)

            # 32 bit addresses are reported slashless by netlink
            with waitfor.waitfor_ipv4_addr(device, address=IPV4_ADDR3):
                ipwrapper.addrAdd(device, IPV4_ADDR3, 32)

            self.assertEqual(
                addresses.getIpInfo(device),
                (IPV4_ADDR1, IPV4_NETMASK,
                 [IPV4_ADDR1_CIDR, IPV4_ADDR2_CIDR, IPV4_ADDR3_CIDR],
                 [IPV6_ADDR_CIDR]))
            self.assertEqual(
                addresses.getIpInfo(device, ipv4_gateway=IPV4_GATEWAY1),
                (IPV4_ADDR1, IPV4_NETMASK,
                 [IPV4_ADDR1_CIDR, IPV4_ADDR2_CIDR, IPV4_ADDR3_CIDR],
                 [IPV6_ADDR_CIDR]))
            self.assertEqual(
                addresses.getIpInfo(device, ipv4_gateway=IPV4_GATEWAY2),
                (IPV4_ADDR2, IPV4_NETMASK,
                 [IPV4_ADDR1_CIDR, IPV4_ADDR2_CIDR, IPV4_ADDR3_CIDR],
                 [IPV6_ADDR_CIDR]))

    def test_netinfo_ignoring_link_scope_ip(self):
        v4_link = {'family': 'inet', 'address': '169.254.0.0/16',
                   'scope': 'link', 'prefixlen': 16, 'flags': ['permanent']}
        v4_global = {'family': 'inet', 'address': '192.0.2.2/24',
                     'scope': 'global', 'prefixlen': 24,
                     'flags': ['permanent']}
        v6_link = {'family': 'inet6', 'address': 'fe80::5054:ff:fea3:f9f3/64',
                   'scope': 'link', 'prefixlen': 64, 'flags': ['permanent']}
        v6_global = {'family': 'inet6',
                     'address': 'ee80::5054:ff:fea3:f9f3/64',
                     'scope': 'global', 'prefixlen': 64,
                     'flags': ['permanent']}
        ipaddrs = {'eth0': (v4_link, v4_global, v6_link, v6_global)}
        ipv4addr, ipv4netmask, ipv4addrs, ipv6addrs = \
            addresses.getIpInfo('eth0', ipaddrs=ipaddrs)
        self.assertEqual(ipv4addrs, ['192.0.2.2/24'])
        self.assertEqual(ipv6addrs, ['ee80::5054:ff:fea3:f9f3/64'])

    def _cidr_form(self, ip_addr, prefix_length):
        return '{}/{}'.format(ip_addr, prefix_length)

    def test_parse_bond_options(self):
        self.assertEqual(bonding.parse_bond_options('mode=4 custom=foo:bar'),
                         {'custom': {'foo': 'bar'}, 'mode': '4'})


@attr(type='integration')
class TestIPv6Addresses(TestCaseBase):
    @ValidateRunningAsRoot
    def test_local_auto_when_ipv6_is_disabled(self):
        with dummy_device() as dev:
            sysctl.disable_ipv6(dev)
            self.assertFalse(addresses.is_ipv6_local_auto(dev))

    @broken_on_ci('IPv6 not supported on travis', name='TRAVIS_CI')
    @ValidateRunningAsRoot
    def test_local_auto_without_router_advertisement_server(self):
        with dummy_device() as dev:
            self.assertTrue(addresses.is_ipv6_local_auto(dev))

    @broken_on_ci('IPv6 not supported on travis', name='TRAVIS_CI')
    @ValidateRunningAsRoot
    def test_local_auto_with_static_address_without_ra_server(self):
        with dummy_device() as dev:
            ipwrapper.addrAdd(dev, '2001::88', '64', family=6)
            ip_addrs = addresses.getIpAddrs()[dev]
            self.assertTrue(addresses.is_ipv6_local_auto(dev))
            self.assertEqual(2, len(ip_addrs))
            self.assertTrue(addresses.is_ipv6(ip_addrs[0]))
            self.assertTrue(not addresses.is_dynamic(ip_addrs[0]))

    @broken_on_ci('Using dnsmasq for ipv6 RA is unstable on CI',
                  name='OVIRT_CI')
    @broken_on_ci('IPv6 not supported on travis', name='TRAVIS_CI')
    @ValidateRunningAsRoot
    def test_local_auto_with_dynamic_address_from_ra(self):
        IPV6_NETADDRESS = '2001:1:1:1'
        IPV6_NETPREFIX_LEN = '64'
        with veth_pair() as (server, client):
            ipwrapper.addrAdd(server, IPV6_NETADDRESS + '::1',
                              IPV6_NETPREFIX_LEN, family=6)
            ipwrapper.linkSet(server, ['up'])
            with dnsmasq_run(server, ipv6_slaac_prefix=IPV6_NETADDRESS + '::'):
                with wait_for_ipv6(client):
                    ipwrapper.linkSet(client, ['up'])

                # Expecting link and global addresses on client iface
                # The addresses are given randomly, so we sort them
                ip_addrs = sorted(addresses.getIpAddrs()[client],
                                  key=lambda ip: ip['address'])
                self.assertEqual(2, len(ip_addrs))

                self.assertTrue(addresses.is_dynamic(ip_addrs[0]))
                self.assertEqual('global', ip_addrs[0]['scope'])
                self.assertEqual(IPV6_NETADDRESS,
                                 ip_addrs[0]['address'][:len(IPV6_NETADDRESS)])

                self.assertEqual('link', ip_addrs[1]['scope'])
