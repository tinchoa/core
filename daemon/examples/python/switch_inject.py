import logging

from core.emulator.emudata import IpPrefixes
from core.emulator.enumerations import EventTypes, NodeTypes


def example(nodes):
    # ip generator for example
    prefixes = IpPrefixes("10.83.0.0/16")

    # create emulator instance for creating sessions and utility methods
    coreemu = globals()["coreemu"]
    session = coreemu.create_session()

    # must be in configuration state for nodes to start, when using "node_add" below
    session.set_state(EventTypes.CONFIGURATION_STATE)

    # create switch network node
    switch = session.add_node(_type=NodeTypes.SWITCH)

    # create nodes
    for _ in range(nodes):
        node = session.add_node()
        interface = prefixes.create_interface(node)
        session.add_link(node.id, switch.id, interface_one=interface)

    # instantiate session
    session.instantiate()


if __name__ in {"__main__", "__builtin__"}:
    logging.basicConfig(level=logging.INFO)
    example(2)
