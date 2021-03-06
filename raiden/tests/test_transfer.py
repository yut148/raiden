# -*- coding: utf8 -*-
from __future__ import print_function

import gevent
import pytest
from ethereum import slogging

from raiden.messages import decode, Ack, DirectTransfer, CancelTransfer
from raiden.tasks import MediatedTransferTask
from raiden.tests.utils.messages import setup_messages_cb, MessageLogger
from raiden.tests.utils.network import create_network, create_sequential_network
from raiden.tests.utils.transfer import assert_synched_channels, channel, direct_transfer, transfer
from raiden.utils import pex, sha3

# pylint: disable=too-many-locals,too-many-statements,line-too-long
slogging.configure(':debug')

# set shorter timeout for testing
MediatedTransferTask.timeout_per_hop = 0.3


def teardown_module(module):  # pylint: disable=unused-argument
    from raiden.tests.utils.tests import cleanup_tasks
    cleanup_tasks()


def test_transfer():
    apps = create_network(num_nodes=2, num_assets=1, channels_per_node=1)
    app0, app1 = apps  # pylint: disable=unbalanced-tuple-unpacking

    messages = setup_messages_cb()
    mlogger = MessageLogger()

    a0_address = pex(app0.raiden.address)
    a1_address = pex(app1.raiden.address)

    asset_manager0 = app0.raiden.assetmanagers.values()[0]
    asset_manager1 = app1.raiden.assetmanagers.values()[0]

    channel0 = asset_manager0.channels[app1.raiden.address]
    channel1 = asset_manager1.channels[app0.raiden.address]

    balance0 = channel0.balance
    balance1 = channel1.balance

    assert asset_manager0.asset_address == asset_manager1.asset_address
    assert app1.raiden.address in asset_manager0.channels

    amount = 10
    app0.raiden.api.transfer(
        asset_manager0.asset_address,
        amount,
        target=app1.raiden.address,
    )
    gevent.sleep(1)

    assert_synched_channels(
        channel0, balance0 - amount, [],
        channel1, balance1 + amount, []
    )

    assert len(messages) == 2  # DirectTransfer, Ack
    directtransfer_message = decode(messages[0])
    assert isinstance(directtransfer_message, DirectTransfer)
    assert directtransfer_message.transfered_amount == amount

    ack_message = decode(messages[1])
    assert isinstance(ack_message, Ack)
    assert ack_message.echo == directtransfer_message.hash

    a0_messages = mlogger.get_node_messages(a0_address)
    assert len(a0_messages) == 2
    assert isinstance(a0_messages[0], DirectTransfer)
    assert isinstance(a0_messages[1], Ack)

    a0_sent_messages = mlogger.get_node_messages(a0_address, only='sent')
    assert len(a0_sent_messages) == 1
    assert isinstance(a0_sent_messages[0], DirectTransfer)

    a0_recv_messages = mlogger.get_node_messages(a0_address, only='recv')
    assert len(a0_recv_messages) == 1
    assert isinstance(a0_recv_messages[0], Ack)

    a1_messages = mlogger.get_node_messages(a1_address)
    assert len(a1_messages) == 2
    assert isinstance(a1_messages[0], Ack)
    assert isinstance(a1_messages[1], DirectTransfer)

    a1_sent_messages = mlogger.get_node_messages(a1_address, only='sent')
    assert len(a1_sent_messages) == 1
    assert isinstance(a1_sent_messages[0], Ack)

    a1_recv_messages = mlogger.get_node_messages(a1_address, only='recv')
    assert len(a1_recv_messages) == 1
    assert isinstance(a1_recv_messages[0], DirectTransfer)


def test_mediated_transfer():
    app_list = create_network(num_nodes=10, num_assets=1, channels_per_node=2)
    app0 = app_list[0]
    setup_messages_cb()

    am0 = app0.raiden.assetmanagers.values()[0]

    # search for a path of length=2 A > B > C
    num_hops = 2
    source = app0.raiden.address

    path_list = am0.channelgraph.get_paths_of_length(source, num_hops)
    assert len(path_list)

    for path in path_list:
        assert len(path) == num_hops + 1
        assert path[0] == source

    path = path_list[0]
    target = path[-1]
    assert path in am0.channelgraph.get_shortest_paths(source, target)
    assert min(len(p) for p in am0.channelgraph.get_shortest_paths(source, target)) == num_hops + 1

    ams_by_address = dict(
        (app.raiden.address, app.raiden.assetmanagers)
        for app in app_list
    )

    # addresses
    hop1, hop2, hop3 = path

    # asset
    asset_address = am0.asset_address

    # channels
    c_ab = ams_by_address[hop1][asset_address].channels[hop2]
    c_ba = ams_by_address[hop2][asset_address].channels[hop1]
    c_bc = ams_by_address[hop2][asset_address].channels[hop3]
    c_cb = ams_by_address[hop3][asset_address].channels[hop2]

    # initial channel balances
    b_ab = c_ab.balance
    b_ba = c_ba.balance
    b_bc = c_bc.balance
    b_cb = c_cb.balance

    amount = 10

    app0.raiden.api.transfer(asset_address, amount, target)

    gevent.sleep(1.)

    # check
    assert b_ab - amount == c_ab.balance
    assert b_ba + amount == c_ba.balance
    assert b_bc - amount == c_bc.balance
    assert b_cb + amount == c_cb.balance


@pytest.mark.xfail(reason='not implemented')
def test_cancel_transfer():
    deposit = 100
    asset = sha3('test_cancel_transfer')[:20]

    # pylint: disable=unbalanced-tuple-unpacking
    app0, app1, app2 = create_sequential_network(num_nodes=3, deposit=deposit, asset=asset)

    messages = setup_messages_cb()
    mlogger = MessageLogger()

    assert_synched_channels(
        channel(app0, app1, asset), deposit, [],
        channel(app1, app0, asset), deposit, []
    )

    assert_synched_channels(
        channel(app1, app2, asset), deposit, [],
        channel(app2, app1, asset), deposit, []
    )

    # drain the channel app1 -> app2
    amount = 80
    direct_transfer(app1, app2, asset, amount)

    assert_synched_channels(
        channel(app0, app1, asset), deposit, [],
        channel(app1, app0, asset), deposit, []
    )

    assert_synched_channels(
        channel(app1, app2, asset), deposit - amount, [],
        channel(app2, app1, asset), deposit + amount, []
    )

    # app1 -> app2 is the only available path and doens't have resource, app1
    # needs to send CancelTransfer to app0
    transfer(app0, app2, asset, 50)

    assert_synched_channels(
        channel(app0, app1, asset), deposit, [],
        channel(app1, app0, asset), deposit, []
    )

    assert_synched_channels(
        channel(app1, app2, asset), deposit - amount, [],
        channel(app2, app1, asset), deposit + amount, []
    )

    assert len(messages) == 6  # DirectTransfer + MediatedTransfer + CancelTransfer + a Ack for each

    app1_messages = mlogger.get_node_messages(pex(app1.raiden.address), only='sent')

    assert isinstance(app1_messages[-1], CancelTransfer)
