"""
session.py: defines the Session class used by the core-daemon daemon program
that manages a CORE session.
"""

import logging
import os
import pwd
import random
import shutil
import subprocess
import tempfile
import threading
import time

from core import constants, utils
from core.emane.emanemanager import EmaneManager
from core.emane.nodes import EmaneNet
from core.emulator.data import EventData, ExceptionData, NodeData
from core.emulator.distributed import DistributedController
from core.emulator.emudata import (
    IdGen,
    LinkOptions,
    NodeOptions,
    create_interface,
    link_config,
)
from core.emulator.enumerations import EventTypes, ExceptionLevels, LinkTypes, NodeTypes
from core.emulator.sessionconfig import SessionConfig
from core.errors import CoreError
from core.location.corelocation import CoreLocation
from core.location.event import EventLoop
from core.location.mobility import BasicRangeModel, MobilityManager
from core.nodes.base import CoreNetworkBase, CoreNode, CoreNodeBase
from core.nodes.docker import DockerNode
from core.nodes.ipaddress import MacAddress
from core.nodes.lxd import LxcNode
from core.nodes.network import (
    CtrlNet,
    GreTapBridge,
    HubNode,
    PtpNet,
    SwitchNode,
    TunnelNode,
    WlanNode,
)
from core.nodes.physical import PhysicalNode, Rj45Node
from core.plugins.sdt import Sdt
from core.services.coreservices import CoreServices
from core.xml import corexml, corexmldeployment
from core.xml.corexml import CoreXmlReader, CoreXmlWriter

# maps for converting from API call node type values to classes and vice versa
NODES = {
    NodeTypes.DEFAULT: CoreNode,
    NodeTypes.PHYSICAL: PhysicalNode,
    NodeTypes.SWITCH: SwitchNode,
    NodeTypes.HUB: HubNode,
    NodeTypes.WIRELESS_LAN: WlanNode,
    NodeTypes.RJ45: Rj45Node,
    NodeTypes.TUNNEL: TunnelNode,
    NodeTypes.EMANE: EmaneNet,
    NodeTypes.TAP_BRIDGE: GreTapBridge,
    NodeTypes.PEER_TO_PEER: PtpNet,
    NodeTypes.CONTROL_NET: CtrlNet,
    NodeTypes.DOCKER: DockerNode,
    NodeTypes.LXC: LxcNode,
}
NODES_TYPE = {NODES[x]: x for x in NODES}
CTRL_NET_ID = 9001


class Session:
    """
    CORE session manager.
    """

    def __init__(self, _id, config=None, mkdir=True):
        """
        Create a Session instance.

        :param int _id: session id
        :param dict config: session configuration
        :param bool mkdir: flag to determine if a directory should be made
        """
        self.id = _id

        # define and create session directory when desired
        self.session_dir = os.path.join(tempfile.gettempdir(), f"pycore.{self.id}")
        if mkdir:
            os.mkdir(self.session_dir)

        self.name = None
        self.file_name = None
        self.thumbnail = None
        self.user = None
        self.event_loop = EventLoop()

        # dict of nodes: all nodes and nets
        self.node_id_gen = IdGen()
        self.nodes = {}
        self._nodes_lock = threading.Lock()

        # TODO: should the default state be definition?
        self.state = EventTypes.NONE.value
        self._state_time = time.monotonic()
        self._state_file = os.path.join(self.session_dir, "state")

        # hooks handlers
        self._hooks = {}
        self._state_hooks = {}
        self.add_state_hook(
            state=EventTypes.RUNTIME_STATE.value, hook=self.runtime_state_hook
        )

        # handlers for broadcasting information
        self.event_handlers = []
        self.exception_handlers = []
        self.node_handlers = []
        self.link_handlers = []
        self.file_handlers = []
        self.config_handlers = []
        self.shutdown_handlers = []

        # session options/metadata
        self.options = SessionConfig()
        if not config:
            config = {}
        for key in config:
            value = config[key]
            self.options.set_config(key, value)
        self.metadata = {}

        # distributed support and logic
        self.distributed = DistributedController(self)

        # initialize session feature helpers
        self.location = CoreLocation()
        self.mobility = MobilityManager(session=self)
        self.services = CoreServices(session=self)
        self.emane = EmaneManager(session=self)
        self.sdt = Sdt(session=self)

        # initialize default node services
        self.services.default_services = {
            "mdr": ("zebra", "OSPFv3MDR", "IPForward"),
            "PC": ("DefaultRoute",),
            "prouter": (),
            "router": ("zebra", "OSPFv2", "OSPFv3", "IPForward"),
            "host": ("DefaultRoute", "SSH"),
        }

    @classmethod
    def get_node_class(cls, _type):
        """
        Retrieve the class for a given node type.

        :param core.emulator.enumerations.NodeTypes _type: node type to get class for
        :return: node class
        """
        node_class = NODES.get(_type)
        if node_class is None:
            raise CoreError(f"invalid node type: {_type}")
        return node_class

    @classmethod
    def get_node_type(cls, _class):
        """
        Retrieve node type for a given node class.

        :param _class: node class to get a node type for
        :return: node type
        :rtype: core.emulator.enumerations.NodeTypes
        """
        node_type = NODES_TYPE.get(_class)
        if node_type is None:
            raise CoreError(f"invalid node class: {_class}")
        return node_type

    def _link_nodes(self, node_one_id, node_two_id):
        """
        Convenience method for retrieving nodes within link data.

        :param int node_one_id: node one id
        :param int node_two_id: node two id
        :return: nodes, network nodes if present, and tunnel if present
        :rtype: tuple
        """
        logging.debug(
            "link message between node1(%s) and node2(%s)", node_one_id, node_two_id
        )

        # values to fill
        net_one = None
        net_two = None

        # retrieve node one
        node_one = self.get_node(node_one_id)
        node_two = self.get_node(node_two_id)

        # both node ids are provided
        tunnel = self.distributed.get_tunnel(node_one_id, node_two_id)
        logging.debug("tunnel between nodes: %s", tunnel)
        if isinstance(tunnel, GreTapBridge):
            net_one = tunnel
            if tunnel.remotenum == node_one_id:
                node_one = None
            else:
                node_two = None
        # physical node connected via gre tap tunnel
        elif tunnel:
            if tunnel.remotenum == node_one_id:
                node_one = None
            else:
                node_two = None

        if isinstance(node_one, CoreNetworkBase):
            if not net_one:
                net_one = node_one
            else:
                net_two = node_one
            node_one = None

        if isinstance(node_two, CoreNetworkBase):
            if not net_one:
                net_one = node_two
            else:
                net_two = node_two
            node_two = None

        logging.debug(
            "link node types n1(%s) n2(%s) net1(%s) net2(%s) tunnel(%s)",
            node_one,
            node_two,
            net_one,
            net_two,
            tunnel,
        )
        return node_one, node_two, net_one, net_two, tunnel

    def _link_wireless(self, objects, connect):
        """
        Objects to deal with when connecting/disconnecting wireless links.

        :param list objects: possible objects to deal with
        :param bool connect: link interfaces if True, unlink otherwise
        :return: nothing
        :raises core.CoreError: when objects to link is less than 2, or no common networks are found
        """
        objects = [x for x in objects if x]
        if len(objects) < 2:
            raise CoreError(f"wireless link failure: {objects}")
        logging.debug(
            "handling wireless linking objects(%s) connect(%s)", objects, connect
        )
        common_networks = objects[0].commonnets(objects[1])
        if not common_networks:
            raise CoreError("no common network found for wireless link/unlink")

        for common_network, interface_one, interface_two in common_networks:
            if not isinstance(common_network, (WlanNode, EmaneNet)):
                logging.info(
                    "skipping common network that is not wireless/emane: %s",
                    common_network,
                )
                continue

            logging.info(
                "wireless linking connect(%s): %s - %s",
                connect,
                interface_one,
                interface_two,
            )
            if connect:
                common_network.link(interface_one, interface_two)
            else:
                common_network.unlink(interface_one, interface_two)

    def add_link(
        self,
        node_one_id,
        node_two_id,
        interface_one=None,
        interface_two=None,
        link_options=None,
    ):
        """
        Add a link between nodes.

        :param int node_one_id: node one id
        :param int node_two_id: node two id
        :param core.emulator.emudata.InterfaceData interface_one: node one interface data, defaults to none
        :param core.emulator.emudata.InterfaceData interface_two: node two interface data, defaults to none
        :param core.emulator.emudata.LinkOptions link_options: data for creating link, defaults to no options
        :return: nothing
        """
        if not link_options:
            link_options = LinkOptions()

        # get node objects identified by link data
        node_one, node_two, net_one, net_two, tunnel = self._link_nodes(
            node_one_id, node_two_id
        )

        if node_one:
            node_one.lock.acquire()
        if node_two:
            node_two.lock.acquire()

        try:
            # wireless link
            if link_options.type == LinkTypes.WIRELESS:
                objects = [node_one, node_two, net_one, net_two]
                self._link_wireless(objects, connect=True)
            # wired link
            else:
                # 2 nodes being linked, ptp network
                if all([node_one, node_two]) and not net_one:
                    logging.info(
                        "adding link for peer to peer nodes: %s - %s",
                        node_one.name,
                        node_two.name,
                    )
                    start = self.state > EventTypes.DEFINITION_STATE.value
                    net_one = self.create_node(cls=PtpNet, start=start)

                # node to network
                if node_one and net_one:
                    logging.info(
                        "adding link from node to network: %s - %s",
                        node_one.name,
                        net_one.name,
                    )
                    interface = create_interface(node_one, net_one, interface_one)
                    link_config(net_one, interface, link_options)

                # network to node
                if node_two and net_one:
                    logging.info(
                        "adding link from network to node: %s - %s",
                        node_two.name,
                        net_one.name,
                    )
                    interface = create_interface(node_two, net_one, interface_two)
                    if not link_options.unidirectional:
                        link_config(net_one, interface, link_options)

                # network to network
                if net_one and net_two:
                    logging.info(
                        "adding link from network to network: %s - %s",
                        net_one.name,
                        net_two.name,
                    )
                    interface = net_one.linknet(net_two)
                    link_config(net_one, interface, link_options)

                    if not link_options.unidirectional:
                        interface.swapparams("_params_up")
                        link_config(
                            net_two, interface, link_options, devname=interface.name
                        )
                        interface.swapparams("_params_up")

                # a tunnel node was found for the nodes
                addresses = []
                if not node_one and all([net_one, interface_one]):
                    addresses.extend(interface_one.get_addresses())

                if not node_two and all([net_two, interface_two]):
                    addresses.extend(interface_two.get_addresses())

                # tunnel node logic
                key = link_options.key
                if key and isinstance(net_one, TunnelNode):
                    logging.info("setting tunnel key for: %s", net_one.name)
                    net_one.setkey(key)
                    if addresses:
                        net_one.addrconfig(addresses)
                if key and isinstance(net_two, TunnelNode):
                    logging.info("setting tunnel key for: %s", net_two.name)
                    net_two.setkey(key)
                    if addresses:
                        net_two.addrconfig(addresses)

                # physical node connected with tunnel
                if not net_one and not net_two and (node_one or node_two):
                    if node_one and isinstance(node_one, PhysicalNode):
                        logging.info("adding link for physical node: %s", node_one.name)
                        addresses = interface_one.get_addresses()
                        node_one.adoptnetif(
                            tunnel, interface_one.id, interface_one.mac, addresses
                        )
                        link_config(node_one, tunnel, link_options)
                    elif node_two and isinstance(node_two, PhysicalNode):
                        logging.info("adding link for physical node: %s", node_two.name)
                        addresses = interface_two.get_addresses()
                        node_two.adoptnetif(
                            tunnel, interface_two.id, interface_two.mac, addresses
                        )
                        link_config(node_two, tunnel, link_options)
        finally:
            if node_one:
                node_one.lock.release()
            if node_two:
                node_two.lock.release()

    def delete_link(
        self,
        node_one_id,
        node_two_id,
        interface_one_id,
        interface_two_id,
        link_type=LinkTypes.WIRED,
    ):
        """
        Delete a link between nodes.

        :param int node_one_id: node one id
        :param int node_two_id: node two id
        :param int interface_one_id: interface id for node one
        :param int interface_two_id: interface id for node two
        :param core.emulator.enumerations.LinkTypes link_type: link type to delete
        :return: nothing
        :raises core.CoreError: when no common network is found for link being deleted
        """
        # get node objects identified by link data
        node_one, node_two, net_one, net_two, _tunnel = self._link_nodes(
            node_one_id, node_two_id
        )

        if node_one:
            node_one.lock.acquire()
        if node_two:
            node_two.lock.acquire()

        try:
            # wireless link
            if link_type == LinkTypes.WIRELESS:
                objects = [node_one, node_two, net_one, net_two]
                self._link_wireless(objects, connect=False)
            # wired link
            else:
                if all([node_one, node_two]):
                    # TODO: fix this for the case where ifindex[1,2] are not specified
                    # a wired unlink event, delete the connecting bridge
                    interface_one = node_one.netif(interface_one_id)
                    interface_two = node_two.netif(interface_two_id)

                    # get interfaces from common network, if no network node
                    # otherwise get interfaces between a node and network
                    if not interface_one and not interface_two:
                        common_networks = node_one.commonnets(node_two)
                        for (
                            network,
                            common_interface_one,
                            common_interface_two,
                        ) in common_networks:
                            if (net_one and network == net_one) or not net_one:
                                interface_one = common_interface_one
                                interface_two = common_interface_two
                                break

                    if all([interface_one, interface_two]) and any(
                        [interface_one.net, interface_two.net]
                    ):
                        if interface_one.net != interface_two.net and all(
                            [interface_one.up, interface_two.up]
                        ):
                            raise CoreError("no common network found")

                        logging.info(
                            "deleting link node(%s):interface(%s) node(%s):interface(%s)",
                            node_one.name,
                            interface_one.name,
                            node_two.name,
                            interface_two.name,
                        )
                        net_one = interface_one.net
                        interface_one.detachnet()
                        interface_two.detachnet()
                        if net_one.numnetif() == 0:
                            self.delete_node(net_one.id)
                        node_one.delnetif(interface_one.netindex)
                        node_two.delnetif(interface_two.netindex)
                elif node_one and net_one:
                    interface = node_one.netif(interface_one_id)
                    if interface:
                        logging.info(
                            "deleting link node(%s):interface(%s) node(%s)",
                            node_one.name,
                            interface.name,
                            net_one.name,
                        )
                        interface.detachnet()
                        node_one.delnetif(interface.netindex)
                elif node_two and net_one:
                    interface = node_two.netif(interface_two_id)
                    if interface:
                        logging.info(
                            "deleting link node(%s):interface(%s) node(%s)",
                            node_two.name,
                            interface.name,
                            net_one.name,
                        )
                        interface.detachnet()
                        node_two.delnetif(interface.netindex)
        finally:
            if node_one:
                node_one.lock.release()
            if node_two:
                node_two.lock.release()

    def update_link(
        self,
        node_one_id,
        node_two_id,
        interface_one_id=None,
        interface_two_id=None,
        link_options=None,
    ):
        """
        Update link information between nodes.

        :param int node_one_id: node one id
        :param int node_two_id: node two id
        :param int interface_one_id: interface id for node one
        :param int interface_two_id: interface id for node two
        :param core.emulator.emudata.LinkOptions link_options: data to update link with
        :return: nothing
        :raises core.CoreError: when updating a wireless type link, when there is a unknown
            link between networks
        """
        if not link_options:
            link_options = LinkOptions()

        # get node objects identified by link data
        node_one, node_two, net_one, net_two, _tunnel = self._link_nodes(
            node_one_id, node_two_id
        )

        if node_one:
            node_one.lock.acquire()
        if node_two:
            node_two.lock.acquire()

        try:
            # wireless link
            if link_options.type == LinkTypes.WIRELESS.value:
                raise CoreError("cannot update wireless link")
            else:
                if not node_one and not node_two:
                    if net_one and net_two:
                        # modify link between nets
                        interface = net_one.getlinknetif(net_two)
                        upstream = False

                        if not interface:
                            upstream = True
                            interface = net_two.getlinknetif(net_one)

                        if not interface:
                            raise CoreError("modify unknown link between nets")

                        if upstream:
                            interface.swapparams("_params_up")
                            link_config(
                                net_one, interface, link_options, devname=interface.name
                            )
                            interface.swapparams("_params_up")
                        else:
                            link_config(net_one, interface, link_options)

                        if not link_options.unidirectional:
                            if upstream:
                                link_config(net_two, interface, link_options)
                            else:
                                interface.swapparams("_params_up")
                                link_config(
                                    net_two,
                                    interface,
                                    link_options,
                                    devname=interface.name,
                                )
                                interface.swapparams("_params_up")
                    else:
                        raise CoreError("modify link for unknown nodes")
                elif not node_one:
                    # node1 = layer 2node, node2 = layer3 node
                    interface = node_two.netif(interface_two_id)
                    link_config(net_one, interface, link_options)
                elif not node_two:
                    # node2 = layer 2node, node1 = layer3 node
                    interface = node_one.netif(interface_one_id)
                    link_config(net_one, interface, link_options)
                else:
                    common_networks = node_one.commonnets(node_two)
                    if not common_networks:
                        raise CoreError("no common network found")

                    for net_one, interface_one, interface_two in common_networks:
                        if (
                            interface_one_id is not None
                            and interface_one_id != node_one.getifindex(interface_one)
                        ):
                            continue

                        link_config(
                            net_one,
                            interface_one,
                            link_options,
                            interface_two=interface_two,
                        )
                        if not link_options.unidirectional:
                            link_config(
                                net_one,
                                interface_two,
                                link_options,
                                interface_two=interface_one,
                            )
        finally:
            if node_one:
                node_one.lock.release()
            if node_two:
                node_two.lock.release()

    def add_node(self, _type=NodeTypes.DEFAULT, _id=None, options=None, _cls=None):
        """
        Add a node to the session, based on the provided node data.

        :param core.emulator.enumerations.NodeTypes _type: type of node to create
        :param int _id: id for node, defaults to None for generated id
        :param core.emulator.emudata.NodeOptions options: data to create node with
        :param class _cls: optional custom class to use for a created node
        :return: created node
        :raises core.CoreError: when an invalid node type is given
        """
        # validate node type, get class, or throw error
        if _cls is None:
            node_class = self.get_node_class(_type)
        else:
            node_class = _cls

        # set node start based on current session state, override and check when rj45
        start = self.state > EventTypes.DEFINITION_STATE.value
        enable_rj45 = self.options.get_config("enablerj45") == "1"
        if _type == NodeTypes.RJ45 and not enable_rj45:
            start = False

        # determine node id
        if not _id:
            while True:
                _id = self.node_id_gen.next()
                if _id not in self.nodes:
                    break

        # generate name if not provided
        if not options:
            options = NodeOptions()
        name = options.name
        if not name:
            name = f"{node_class.__name__}{_id}"

        # verify distributed server
        server = self.distributed.servers.get(options.server)
        if options.server is not None and server is None:
            raise CoreError(f"invalid distributed server: {options.server}")

        # create node
        logging.info(
            "creating node(%s) id(%s) name(%s) start(%s)",
            node_class.__name__,
            _id,
            name,
            start,
        )
        if _type in [NodeTypes.DOCKER, NodeTypes.LXC]:
            node = self.create_node(
                cls=node_class,
                _id=_id,
                name=name,
                start=start,
                image=options.image,
                server=server,
            )
        else:
            node = self.create_node(
                cls=node_class, _id=_id, name=name, start=start, server=server
            )

        # set node attributes
        node.icon = options.icon
        node.canvas = options.canvas
        node.opaque = options.opaque

        # set node position and broadcast it
        self.set_node_position(node, options)

        # add services to needed nodes
        if isinstance(node, (CoreNode, PhysicalNode, DockerNode, LxcNode)):
            node.type = options.model
            logging.debug("set node type: %s", node.type)
            self.services.add_services(node, node.type, options.services)

        # ensure default emane configuration
        if isinstance(node, EmaneNet) and options.emane:
            self.emane.set_model_config(_id, options.emane)
        # set default wlan config if needed
        if isinstance(node, WlanNode):
            self.mobility.set_model_config(_id, BasicRangeModel.name)

        # boot nodes after runtime, CoreNodes, Physical, and RJ45 are all nodes
        is_boot_node = isinstance(node, CoreNodeBase) and not isinstance(node, Rj45Node)
        if self.state == EventTypes.RUNTIME_STATE.value and is_boot_node:
            self.write_nodes()
            self.add_remove_control_interface(node=node, remove=False)
            self.services.boot_services(node)

        return node

    def edit_node(self, node_id, options):
        """
        Edit node information.

        :param int node_id: id of node to update
        :param core.emulator.emudata.NodeOptions options: data to update node with
        :return: True if node updated, False otherwise
        :rtype: bool
        :raises core.CoreError: when node to update does not exist
        """
        # get node to update
        node = self.get_node(node_id)

        # set node position and broadcast it
        self.set_node_position(node, options)

        # update attributes
        node.canvas = options.canvas
        node.icon = options.icon

    def set_node_position(self, node, options):
        """
        Set position for a node, use lat/lon/alt if needed.

        :param node: node to set position for
        :param core.emulator.emudata.NodeOptions options: data for node
        :return: nothing
        """
        # extract location values
        x = options.x
        y = options.y
        lat = options.lat
        lon = options.lon
        alt = options.alt

        # check if we need to generate position from lat/lon/alt
        has_empty_position = all(i is None for i in [x, y])
        has_lat_lon_alt = all(i is not None for i in [lat, lon, alt])
        using_lat_lon_alt = has_empty_position and has_lat_lon_alt
        if using_lat_lon_alt:
            x, y, _ = self.location.getxyz(lat, lon, alt)

        # set position and broadcast
        if None not in [x, y]:
            node.setposition(x, y, None)

        # broadcast updated location when using lat/lon/alt
        if using_lat_lon_alt:
            self.broadcast_node_location(node)

    def broadcast_node_location(self, node):
        """
        Broadcast node location to all listeners.

        :param core.nodes.base.NodeBase node: node to broadcast location for
        :return: nothing
        """
        node_data = NodeData(
            message_type=0,
            id=node.id,
            x_position=node.position.x,
            y_position=node.position.y,
        )
        self.broadcast_node(node_data)

    def start_mobility(self, node_ids=None):
        """
        Start mobility for the provided node ids.

        :param list[int] node_ids: nodes to start mobility for
        :return: nothing
        """
        self.mobility.startup(node_ids)

    def is_active(self):
        """
        Determine if this session is considered to be active. (Runtime or Data collect states)

        :return: True if active, False otherwise
        """
        result = self.state in {
            EventTypes.RUNTIME_STATE.value,
            EventTypes.DATACOLLECT_STATE.value,
        }
        logging.info("session(%s) checking if active: %s", self.id, result)
        return result

    def open_xml(self, file_name, start=False):
        """
        Import a session from the EmulationScript XML format.

        :param str file_name: xml file to load session from
        :param bool start: instantiate session if true, false otherwise
        :return: nothing
        """
        logging.info("opening xml: %s", file_name)

        # clear out existing session
        self.clear()

        if start:
            state = EventTypes.CONFIGURATION_STATE
        else:
            state = EventTypes.DEFINITION_STATE
        self.set_state(state)
        self.name = os.path.basename(file_name)
        self.file_name = file_name

        # write out xml file
        CoreXmlReader(self).read(file_name)

        # start session if needed
        if start:
            self.instantiate()

    def save_xml(self, file_name):
        """
        Export a session to the EmulationScript XML format.

        :param str file_name: file name to write session xml to
        :return: nothing
        """
        CoreXmlWriter(self).write(file_name)

    def add_hook(self, state, file_name, source_name, data):
        """
        Store a hook from a received file message.

        :param int state: when to run hook
        :param str file_name: file name for hook
        :param str source_name: source name
        :param data: hook data
        :return: nothing
        """
        # hack to conform with old logic until updated
        state = f":{state}"
        self.set_hook(state, file_name, source_name, data)

    def add_node_file(self, node_id, source_name, file_name, data):
        """
        Add a file to a node.

        :param int node_id: node to add file to
        :param str source_name: source file name
        :param str file_name: file name to add
        :param str data: file data
        :return: nothing
        """

        node = self.get_node(node_id)

        if source_name is not None:
            node.addfile(source_name, file_name)
        elif data is not None:
            node.nodefile(file_name, data)

    def clear(self):
        """
        Clear all CORE session data. (nodes, hooks, etc)

        :return: nothing
        """
        self.emane.shutdown()
        self.delete_nodes()
        self.distributed.shutdown()
        self.del_hooks()
        self.emane.reset()
        self.emane.config_reset()
        self.location.reset()
        self.services.reset()
        self.mobility.config_reset()

    def start_events(self):
        """
        Start event loop.

        :return: nothing
        """
        self.event_loop.run()

    def mobility_event(self, event_data):
        """
        Handle a mobility event.

        :param core.emulator.data.EventData event_data: event data to handle
        :return: nothing
        """
        self.mobility.handleevent(event_data)

    def set_location(self, lat, lon, alt, scale):
        """
        Set session geospatial location.

        :param float lat: latitude
        :param float lon: longitude
        :param float alt: altitude
        :param float scale: reference scale
        :return: nothing
        """
        self.location.setrefgeo(lat, lon, alt)
        self.location.refscale = scale

    def shutdown(self):
        """
        Shutdown all session nodes and remove the session directory.
        """
        logging.info("session(%s) shutting down", self.id)
        self.set_state(EventTypes.DATACOLLECT_STATE, send_event=True)
        self.set_state(EventTypes.SHUTDOWN_STATE, send_event=True)

        # clear out current core session
        self.clear()

        # shutdown sdt
        self.sdt.shutdown()

        # remove this sessions working directory
        preserve = self.options.get_config("preservedir") == "1"
        if not preserve:
            shutil.rmtree(self.session_dir, ignore_errors=True)

        # call session shutdown handlers
        for handler in self.shutdown_handlers:
            handler(self)

    def broadcast_event(self, event_data):
        """
        Handle event data that should be provided to event handler.

        :param core.data.EventData event_data: event data to send out
        :return: nothing
        """

        for handler in self.event_handlers:
            handler(event_data)

    def broadcast_exception(self, exception_data):
        """
        Handle exception data that should be provided to exception handlers.

        :param core.emulator.data.ExceptionData exception_data: exception data to send out
        :return: nothing
        """

        for handler in self.exception_handlers:
            handler(exception_data)

    def broadcast_node(self, node_data):
        """
        Handle node data that should be provided to node handlers.

        :param core.emulator.data.ExceptionData node_data: node data to send out
        :return: nothing
        """

        for handler in self.node_handlers:
            handler(node_data)

    def broadcast_file(self, file_data):
        """
        Handle file data that should be provided to file handlers.

        :param core.data.FileData file_data: file data to send out
        :return: nothing
        """

        for handler in self.file_handlers:
            handler(file_data)

    def broadcast_config(self, config_data):
        """
        Handle config data that should be provided to config handlers.

        :param core.emulator.data.ConfigData config_data: config data to send out
        :return: nothing
        """

        for handler in self.config_handlers:
            handler(config_data)

    def broadcast_link(self, link_data):
        """
        Handle link data that should be provided to link handlers.

        :param core.emulator.data.ExceptionData link_data: link data to send out
        :return: nothing
        """

        for handler in self.link_handlers:
            handler(link_data)

    def set_state(self, state, send_event=False):
        """
        Set the session's current state.

        :param core.enumerations.EventTypes state: state to set to
        :param send_event: if true, generate core API event messages
        :return: nothing
        """
        state_value = state.value
        state_name = state.name

        if self.state == state_value:
            logging.info(
                "session(%s) is already in state: %s, skipping change",
                self.id,
                state_name,
            )
            return

        self.state = state_value
        self._state_time = time.monotonic()
        logging.info("changing session(%s) to state %s", self.id, state_name)

        self.write_state(state_value)
        self.run_hooks(state_value)
        self.run_state_hooks(state_value)

        if send_event:
            event_data = EventData(event_type=state_value, time=str(time.monotonic()))
            self.broadcast_event(event_data)

    def write_state(self, state):
        """
        Write the current state to a state file in the session dir.

        :param int state: state to write to file
        :return: nothing
        """
        try:
            state_file = open(self._state_file, "w")
            state_file.write(f"{state} {EventTypes(self.state).name}\n")
            state_file.close()
        except IOError:
            logging.exception("error writing state file: %s", state)

    def run_hooks(self, state):
        """
        Run hook scripts upon changing states. If hooks is not specified, run all hooks in the given state.

        :param int state: state to run hooks for
        :return: nothing
        """

        # check that state change hooks exist
        if state not in self._hooks:
            return

        # retrieve all state hooks
        hooks = self._hooks.get(state, [])

        # execute all state hooks
        if hooks:
            for hook in hooks:
                self.run_hook(hook)
        else:
            logging.info("no state hooks for %s", state)

    def set_hook(self, hook_type, file_name, source_name, data):
        """
        Store a hook from a received file message.

        :param str hook_type: hook type
        :param str file_name: file name for hook
        :param str source_name: source name
        :param str data: hook data
        :return: nothing
        """
        logging.info(
            "setting state hook: %s - %s from %s", hook_type, file_name, source_name
        )

        _hook_id, state = hook_type.split(":")[:2]
        if not state.isdigit():
            logging.error("error setting hook having state '%s'", state)
            return

        state = int(state)
        hook = file_name, data

        # append hook to current state hooks
        state_hooks = self._hooks.setdefault(state, [])
        state_hooks.append(hook)

        # immediately run a hook if it is in the current state
        # (this allows hooks in the definition and configuration states)
        if self.state == state:
            logging.info("immediately running new state hook")
            self.run_hook(hook)

    def del_hooks(self):
        """
        Clear the hook scripts dict.
        """
        self._hooks.clear()

    def run_hook(self, hook):
        """
        Run a hook.

        :param tuple hook: hook to run
        :return: nothing
        """
        file_name, data = hook
        logging.info("running hook %s", file_name)

        # write data to hook file
        try:
            hook_file = open(os.path.join(self.session_dir, file_name), "w")
            hook_file.write(data)
            hook_file.close()
        except IOError:
            logging.exception("error writing hook '%s'", file_name)

        # setup hook stdout and stderr
        try:
            stdout = open(os.path.join(self.session_dir, file_name + ".log"), "w")
            stderr = subprocess.STDOUT
        except IOError:
            logging.exception("error setting up hook stderr and stdout")
            stdout = None
            stderr = None

        # execute hook file
        try:
            args = ["/bin/sh", file_name]
            subprocess.check_call(
                args,
                stdout=stdout,
                stderr=stderr,
                close_fds=True,
                cwd=self.session_dir,
                env=self.get_environment(),
            )
        except (OSError, subprocess.CalledProcessError):
            logging.exception("error running hook: %s", file_name)

    def run_state_hooks(self, state):
        """
        Run state hooks.

        :param int state: state to run hooks for
        :return: nothing
        """
        for hook in self._state_hooks.get(state, []):
            try:
                hook(state)
            except Exception:
                state_name = EventTypes(self.state).name
                message = (
                    f"exception occured when running {state_name} state hook: {hook}"
                )
                logging.exception(message)
                self.exception(
                    ExceptionLevels.ERROR, "Session.run_state_hooks", None, message
                )

    def add_state_hook(self, state, hook):
        """
        Add a state hook.

        :param int state: state to add hook for
        :param func hook: hook callback for the state
        :return: nothing
        """
        hooks = self._state_hooks.setdefault(state, [])
        if hook in hooks:
            raise CoreError("attempting to add duplicate state hook")
        hooks.append(hook)

        if self.state == state:
            hook(state)

    def del_state_hook(self, state, hook):
        """
        Delete a state hook.

        :param int state: state to delete hook for
        :param func hook: hook to delete
        :return:
        """
        hooks = self._state_hooks.setdefault(state, [])
        hooks.remove(hook)

    def runtime_state_hook(self, state):
        """
        Runtime state hook check.

        :param int state: state to check
        :return: nothing
        """
        if state == EventTypes.RUNTIME_STATE.value:
            self.emane.poststartup()

            # create session deployed xml
            xml_file_name = os.path.join(self.session_dir, "session-deployed.xml")
            xml_writer = corexml.CoreXmlWriter(self)
            corexmldeployment.CoreXmlDeployment(self, xml_writer.scenario)
            xml_writer.write(xml_file_name)

    def get_environment(self, state=True):
        """
        Get an environment suitable for a subprocess.Popen call.
        This is the current process environment with some session-specific
        variables.

        :param bool state: flag to determine if session state should be included
        :return: environment variables
        :rtype: dict
        """
        env = os.environ.copy()
        env["SESSION"] = str(self.id)
        env["SESSION_SHORT"] = self.short_session_id()
        env["SESSION_DIR"] = self.session_dir
        env["SESSION_NAME"] = str(self.name)
        env["SESSION_FILENAME"] = str(self.file_name)
        env["SESSION_USER"] = str(self.user)
        env["SESSION_NODE_COUNT"] = str(self.get_node_count())

        if state:
            env["SESSION_STATE"] = str(self.state)

        # attempt to read and add environment config file
        environment_config_file = os.path.join(constants.CORE_CONF_DIR, "environment")
        try:
            if os.path.isfile(environment_config_file):
                utils.load_config(environment_config_file, env)
        except IOError:
            logging.warning(
                "environment configuration file does not exist: %s",
                environment_config_file,
            )

        # attempt to read and add user environment file
        if self.user:
            environment_user_file = os.path.join(
                "/home", self.user, ".core", "environment"
            )
            try:
                utils.load_config(environment_user_file, env)
            except IOError:
                logging.debug(
                    "user core environment settings file not present: %s",
                    environment_user_file,
                )

        return env

    def set_thumbnail(self, thumb_file):
        """
        Set the thumbnail filename. Move files from /tmp to session dir.

        :param str thumb_file: tumbnail file to set for session
        :return: nothing
        """
        if not os.path.exists(thumb_file):
            logging.error("thumbnail file to set does not exist: %s", thumb_file)
            self.thumbnail = None
            return

        destination_file = os.path.join(self.session_dir, os.path.basename(thumb_file))
        shutil.copy(thumb_file, destination_file)
        self.thumbnail = destination_file

    def set_user(self, user):
        """
        Set the username for this session. Update the permissions of the
        session dir to allow the user write access.

        :param str user: user to give write permissions to for the session directory
        :return: nothing
        """
        if user:
            try:
                uid = pwd.getpwnam(user).pw_uid
                gid = os.stat(self.session_dir).st_gid
                os.chown(self.session_dir, uid, gid)
            except IOError:
                logging.exception("failed to set permission on %s", self.session_dir)

        self.user = user

    def get_node_id(self):
        """
        Return a unique, new node id.
        """
        with self._nodes_lock:
            while True:
                node_id = random.randint(1, 0xFFFF)
                if node_id not in self.nodes:
                    break

        return node_id

    def create_node(self, cls, *args, **kwargs):
        """
        Create an emulation node.

        :param class cls: node class to create
        :param list args: list of arguments for the class to create
        :param dict kwargs: dictionary of arguments for the class to create
        :return: the created node instance
        :raises core.CoreError: when id of the node to create already exists
        """
        node = cls(self, *args, **kwargs)

        with self._nodes_lock:
            if node.id in self.nodes:
                node.shutdown()
                raise CoreError(f"duplicate node id {node.id} for {node.name}")
            self.nodes[node.id] = node

        return node

    def get_node(self, _id):
        """
        Get a session node.

        :param int _id: node id to retrieve
        :return: node for the given id
        :rtype: core.nodes.base.CoreNode
        :raises core.CoreError: when node does not exist
        """
        if _id not in self.nodes:
            raise CoreError(f"unknown node id {_id}")
        return self.nodes[_id]

    def delete_node(self, _id):
        """
        Delete a node from the session and check if session should shutdown, if no nodes are left.

        :param int _id: id of node to delete
        :return: True if node deleted, False otherwise
        :rtype: bool
        """
        # delete node and check for session shutdown if a node was removed
        logging.info("deleting node(%s)", _id)
        node = None
        with self._nodes_lock:
            if _id in self.nodes:
                node = self.nodes.pop(_id)

        if node:
            node.shutdown()
            self.check_shutdown()

        return node is not None

    def delete_nodes(self):
        """
        Clear the nodes dictionary, and call shutdown for each node.
        """
        with self._nodes_lock:
            funcs = []
            while self.nodes:
                _, node = self.nodes.popitem()
                funcs.append((node.shutdown, [], {}))
            utils.threadpool(funcs)
        self.node_id_gen.id = 0

    def write_nodes(self):
        """
        Write nodes to a 'nodes' file in the session dir.
        The 'nodes' file lists: number, name, api-type, class-type
        """
        try:
            with self._nodes_lock:
                file_path = os.path.join(self.session_dir, "nodes")
                with open(file_path, "w") as f:
                    for _id in self.nodes.keys():
                        node = self.nodes[_id]
                        f.write(f"{_id} {node.name} {node.apitype} {type(node)}\n")
        except IOError:
            logging.exception("error writing nodes file")

    def dump_session(self):
        """
        Log information about the session in its current state.
        """
        logging.info("session id=%s name=%s state=%s", self.id, self.name, self.state)
        logging.info(
            "file=%s thumbnail=%s node_count=%s/%s",
            self.file_name,
            self.thumbnail,
            self.get_node_count(),
            len(self.nodes),
        )

    def exception(self, level, source, node_id, text):
        """
        Generate and broadcast an exception event.

        :param core.emulator.enumerations.ExceptionLevel level: exception level
        :param str source: source name
        :param int node_id: node related to exception
        :param str text: exception message
        :return: nothing
        """
        exception_data = ExceptionData(
            node=node_id,
            session=str(self.id),
            level=level,
            source=source,
            date=time.ctime(),
            text=text,
        )
        self.broadcast_exception(exception_data)

    def instantiate(self):
        """
        We have entered the instantiation state, invoke startup methods
        of various managers and boot the nodes. Validate nodes and check
        for transition to the runtime state.
        """

        # write current nodes out to session directory file
        self.write_nodes()

        # create control net interfaces and network tunnels
        # which need to exist for emane to sync on location events
        # in distributed scenarios
        self.add_remove_control_interface(node=None, remove=False)

        # initialize distributed tunnels
        self.distributed.start()

        # instantiate will be invoked again upon Emane configure
        if self.emane.startup() == self.emane.NOT_READY:
            return

        # boot node services and then start mobility
        exceptions = self.boot_nodes()
        if not exceptions:
            self.mobility.startup()

            # notify listeners that instantiation is complete
            event = EventData(event_type=EventTypes.INSTANTIATION_COMPLETE.value)
            self.broadcast_event(event)

            # assume either all nodes have booted already, or there are some
            # nodes on slave servers that will be booted and those servers will
            # send a node status response message
            self.check_runtime()
        return exceptions

    def get_node_count(self):
        """
        Returns the number of CoreNodes and CoreNets, except for those
        that are not considered in the GUI's node count.
        """

        with self._nodes_lock:
            count = 0
            for node_id in self.nodes:
                node = self.nodes[node_id]
                is_p2p_ctrlnet = isinstance(node, (PtpNet, CtrlNet))
                is_tap = isinstance(node, GreTapBridge) and not isinstance(
                    node, TunnelNode
                )
                if is_p2p_ctrlnet or is_tap:
                    continue

                count += 1

        return count

    def check_runtime(self):
        """
        Check if we have entered the runtime state, that all nodes have been
        started and the emulation is running. Start the event loop once we
        have entered runtime (time=0).
        """
        # this is called from instantiate() after receiving an event message
        # for the instantiation state
        logging.debug(
            "session(%s) checking if not in runtime state, current state: %s",
            self.id,
            EventTypes(self.state).name,
        )
        if self.state == EventTypes.RUNTIME_STATE.value:
            logging.info("valid runtime state found, returning")
            return

        # start event loop and set to runtime
        self.event_loop.run()
        self.set_state(EventTypes.RUNTIME_STATE, send_event=True)

    def data_collect(self):
        """
        Tear down a running session. Stop the event loop and any running
        nodes, and perform clean-up.
        """
        # stop event loop
        self.event_loop.stop()

        # stop node services
        with self._nodes_lock:
            funcs = []
            for node_id in self.nodes:
                node = self.nodes[node_id]
                if isinstance(node, CoreNodeBase):
                    args = (node,)
                    funcs.append((self.services.stop_services, args, {}))
            utils.threadpool(funcs)

        # shutdown emane
        self.emane.shutdown()

        # update control interface hosts
        self.update_control_interface_hosts(remove=True)

        # remove all four possible control networks. Does nothing if ctrlnet is not
        # installed.
        self.add_remove_control_interface(node=None, net_index=0, remove=True)
        self.add_remove_control_interface(node=None, net_index=1, remove=True)
        self.add_remove_control_interface(node=None, net_index=2, remove=True)
        self.add_remove_control_interface(node=None, net_index=3, remove=True)

    def check_shutdown(self):
        """
        Check if we have entered the shutdown state, when no running nodes
        and links remain.
        """
        node_count = self.get_node_count()
        logging.debug(
            "session(%s) checking shutdown: %s nodes remaining", self.id, node_count
        )

        shutdown = False
        if node_count == 0:
            shutdown = True
            self.set_state(EventTypes.SHUTDOWN_STATE)

        return shutdown

    def short_session_id(self):
        """
        Return a shorter version of the session ID, appropriate for
        interface names, where length may be limited.
        """
        ssid = (self.id >> 8) ^ (self.id & ((1 << 8) - 1))
        return f"{ssid:x}"

    def boot_node(self, node):
        """
        Boot node by adding a control interface when necessary and starting
        node services.

        :param core.nodes.base.CoreNodeBase node: node to boot
        :return: nothing
        """
        logging.info("booting node(%s): %s", node.name, [x.name for x in node.services])
        self.add_remove_control_interface(node=node, remove=False)
        self.services.boot_services(node)

    def boot_nodes(self):
        """
        Invoke the boot() procedure for all nodes and send back node
        messages to the GUI for node messages that had the status
        request flag.

        :return: service boot exceptions
        :rtype: list[core.services.coreservices.ServiceBootError]
        """
        with self._nodes_lock:
            funcs = []
            start = time.monotonic()
            for _id in self.nodes:
                node = self.nodes[_id]
                if isinstance(node, CoreNodeBase) and not isinstance(node, Rj45Node):
                    args = (node,)
                    funcs.append((self.boot_node, args, {}))
            results, exceptions = utils.threadpool(funcs)
            total = time.monotonic() - start
            logging.debug("boot run time: %s", total)
        if not exceptions:
            self.update_control_interface_hosts()
        return exceptions

    def get_control_net_prefixes(self):
        """
        Retrieve control net prefixes.

        :return: control net prefix list
        :rtype: list
        """
        p = self.options.get_config("controlnet")
        p0 = self.options.get_config("controlnet0")
        p1 = self.options.get_config("controlnet1")
        p2 = self.options.get_config("controlnet2")
        p3 = self.options.get_config("controlnet3")

        if not p0 and p:
            p0 = p

        return [p0, p1, p2, p3]

    def get_control_net_server_interfaces(self):
        """
        Retrieve control net server interfaces.

        :return: list of control net server interfaces
        :rtype: list
        """
        d0 = self.options.get_config("controlnetif0")
        if d0:
            logging.error("controlnet0 cannot be assigned with a host interface")
        d1 = self.options.get_config("controlnetif1")
        d2 = self.options.get_config("controlnetif2")
        d3 = self.options.get_config("controlnetif3")
        return [None, d1, d2, d3]

    def get_control_net_index(self, dev):
        """
        Retrieve control net index.

        :param str dev: device to get control net index for
        :return: control net index, -1 otherwise
        :rtype: int
        """
        if dev[0:4] == "ctrl" and int(dev[4]) in [0, 1, 2, 3]:
            index = int(dev[4])
            if index == 0:
                return index
            if index < 4 and self.get_control_net_prefixes()[index] is not None:
                return index
        return -1

    def get_control_net(self, net_index):
        return self.get_node(CTRL_NET_ID + net_index)

    def add_remove_control_net(self, net_index, remove=False, conf_required=True):
        """
        Create a control network bridge as necessary.
        When the remove flag is True, remove the bridge that connects control
        interfaces. The conf_reqd flag, when False, causes a control network
        bridge to be added even if one has not been configured.

        :param int net_index: network index
        :param bool remove: flag to check if it should be removed
        :param bool conf_required: flag to check if conf is required
        :return: control net node
        :rtype: core.nodes.network.CtrlNet
        """
        logging.debug(
            "add/remove control net: index(%s) remove(%s) conf_required(%s)",
            net_index,
            remove,
            conf_required,
        )
        prefix_spec_list = self.get_control_net_prefixes()
        prefix_spec = prefix_spec_list[net_index]
        if not prefix_spec:
            if conf_required:
                # no controlnet needed
                return None
            else:
                prefix_spec = CtrlNet.DEFAULT_PREFIX_LIST[net_index]
        logging.debug("prefix spec: %s", prefix_spec)

        server_interface = self.get_control_net_server_interfaces()[net_index]

        # return any existing controlnet bridge
        try:
            control_net = self.get_control_net(net_index)

            if remove:
                self.delete_node(control_net.id)
                return None

            return control_net
        except CoreError:
            if remove:
                return None

        # build a new controlnet bridge
        _id = CTRL_NET_ID + net_index

        # use the updown script for control net 0 only.
        updown_script = None

        if net_index == 0:
            updown_script = self.options.get_config("controlnet_updown_script")
            if not updown_script:
                logging.debug("controlnet updown script not configured")

        prefixes = prefix_spec.split()
        if len(prefixes) > 1:
            # a list of per-host prefixes is provided
            try:
                # split first (master) entry into server and prefix
                prefix = prefixes[0].split(":", 1)[1]
            except IndexError:
                # no server name. possibly only one server
                prefix = prefixes[0]
        else:
            prefix = prefixes[0]

        logging.info(
            "controlnet(%s) prefix(%s) updown(%s) serverintf(%s)",
            _id,
            prefix,
            updown_script,
            server_interface,
        )
        control_net = self.create_node(
            cls=CtrlNet,
            _id=_id,
            prefix=prefix,
            assign_address=True,
            updown_script=updown_script,
            serverintf=server_interface,
        )

        return control_net

    def add_remove_control_interface(
        self, node, net_index=0, remove=False, conf_required=True
    ):
        """
        Add a control interface to a node when a 'controlnet' prefix is
        listed in the config file or session options. Uses
        addremovectrlnet() to build or remove the control bridge.
        If conf_reqd is False, the control network may be built even
        when the user has not configured one (e.g. for EMANE.)

        :param core.nodes.base.CoreNodeBase node: node to add or remove control interface
        :param int net_index: network index
        :param bool remove: flag to check if it should be removed
        :param bool conf_required: flag to check if conf is required
        :return: nothing
        """
        control_net = self.add_remove_control_net(net_index, remove, conf_required)
        if not control_net:
            return

        if not node:
            return

        # ctrl# already exists
        if node.netif(control_net.CTRLIF_IDX_BASE + net_index):
            return

        control_ip = node.id

        try:
            address = control_net.prefix.addr(control_ip)
            prefix = control_net.prefix.prefixlen
            addrlist = [f"{address}/{prefix}"]
        except ValueError:
            msg = f"Control interface not added to node {node.id}. "
            msg += f"Invalid control network prefix ({control_net.prefix}). "
            msg += "A longer prefix length may be required for this many nodes."
            logging.exception(msg)
            return

        interface1 = node.newnetif(
            net=control_net,
            ifindex=control_net.CTRLIF_IDX_BASE + net_index,
            ifname=f"ctrl{net_index}",
            hwaddr=MacAddress.random(),
            addrlist=addrlist,
        )
        node.netif(interface1).control = True

    def update_control_interface_hosts(self, net_index=0, remove=False):
        """
        Add the IP addresses of control interfaces to the /etc/hosts file.

        :param int net_index: network index to update
        :param bool remove: flag to check if it should be removed
        :return: nothing
        """
        if not self.options.get_config_bool("update_etc_hosts", default=False):
            return

        try:
            control_net = self.get_control_net(net_index)
        except CoreError:
            logging.exception("error retrieving control net node")
            return

        header = f"CORE session {self.id} host entries"
        if remove:
            logging.info("Removing /etc/hosts file entries.")
            utils.file_demunge("/etc/hosts", header)
            return

        entries = []
        for interface in control_net.netifs():
            name = interface.node.name
            for address in interface.addrlist:
                address = address.split("/")[0]
                entries.append(f"{address} {name}")

        logging.info("Adding %d /etc/hosts file entries.", len(entries))

        utils.file_munge("/etc/hosts", header, "\n".join(entries) + "\n")

    def runtime(self):
        """
        Return the current time we have been in the runtime state, or zero
        if not in runtime.
        """
        if self.state == EventTypes.RUNTIME_STATE.value:
            return time.monotonic() - self._state_time
        else:
            return 0.0

    def add_event(self, event_time, node=None, name=None, data=None):
        """
        Add an event to the event queue, with a start time relative to the
        start of the runtime state.

        :param event_time: event time
        :param core.nodes.base.CoreNode node: node to add event for
        :param str name: name of event
        :param data: data for event
        :return: nothing
        """
        event_time = float(event_time)
        current_time = self.runtime()

        if current_time > 0:
            if event_time <= current_time:
                logging.warning(
                    "could not schedule past event for time %s (run time is now %s)",
                    event_time,
                    current_time,
                )
                return
            event_time = event_time - current_time

        self.event_loop.add_event(
            event_time, self.run_event, node=node, name=name, data=data
        )

        if not name:
            name = ""
        logging.info(
            "scheduled event %s at time %s data=%s",
            name,
            event_time + current_time,
            data,
        )

    # TODO: if data is None, this blows up, but this ties into how event functions
    #  are ran, need to clean that up
    def run_event(self, node_id=None, name=None, data=None):
        """
        Run a scheduled event, executing commands in the data string.

        :param int node_id: node id to run event
        :param str name: event name
        :param str data: event data
        :return: nothing
        """
        now = self.runtime()
        if not name:
            name = ""

        logging.info("running event %s at time %s cmd=%s", name, now, data)
        if not node_id:
            utils.mute_detach(data)
        else:
            node = self.get_node(node_id)
            node.cmd(data, wait=False)
