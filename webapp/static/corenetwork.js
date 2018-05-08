const Ip4Prefix = '10.0.0.1/24';
const Ip6Prefix = '2001::/64';


class NodeHelper {
    constructor() {
        this.displays = {
            // default router
            0: {
                name: 'node',
                display: 'Default'
            },
            // switch
            4: {
                name: 'switch',
                display: 'Switch'
            },
            // hub
            5: {
                name: 'hub',
                display: 'Hub'
            },
            // wlan
            6: {
                name: 'wlan',
                display: 'WLAN'
            },
            12: {
                name: 'ptp',
                display: 'PTP'
            }
        };

        this.icons = {
            router: 'static/router.svg',
            host: 'static/host.gif',
            PC: 'static/pc.gif',
            mdr: 'static/mdr.svg',
            switch: 'static/lanswitch.svg',
            hub: 'static/hub.svg',
            wlan: 'static/wlan.gif'
        };

        this.defaultNode = 0;
        this.ptpNode = 12;
    }

    isNetworkNode(node) {
        return [4, 5, 6, 12].includes(node.type);
    }

    getDisplay(nodeType) {
        return this.displays[nodeType];
    }

    getIcon(node) {
        let iconName = this.getDisplay(node.type).name;
        if (node.type === 0) {
            iconName = node.model;
        }
        return this.icons[iconName];
    }
}
const CoreNodeHelper = new NodeHelper();

class CoreNode {
    constructor(id, type, name, x, y) {
        this.id = id;
        this.type = type;
        this.name = name;
        this.model = null;
        this.canvas = null;
        this.icon = null;
        this.opaque = null;
        this.services = [];
        this.x = x;
        this.y = y;
        this.lat = null;
        this.lon = null;
        this.alt = null;
        this.emulation_id = null;
        this.emulation_server = null;
        this.interfaces = {};
    }

    getNetworkNode() {
        const icon = CoreNodeHelper.getIcon(this);

        return {
            id: this.id,
            x: this.x,
            y: this.y,
            label: this.name,
            coreNode: this,
            //color: '#FFF',
            shape: 'image',
            image: icon
        };
    }

    json() {
        return {
            id: this.id,
            type: this.type,
            name: this.name,
            model: this.model,
            x: this.x,
            y: this.y,
            lat: this.lat,
            lon: this.lon,
            alt: this.alt
        }
    }
}

function setEdgeData(edge, linkId, fromNode, toNode, interfaceOne, interfaceTwo) {
    edge.core = linkId;
    edge.label = 'from: name\nto: name';
    edge.title = 'from: name\nto: name';
    edge.nodeOne = fromNode.name;
    edge.interfaceOne = interfaceOne;
    edge.nodeTwo = toNode.name;
    edge.interfaceTwo = interfaceTwo;
}

class CoreNetwork {
    constructor(elementId, coreRest) {
        this.coreRest = coreRest;
        this.nodeType = 0;
        this.nodeModel = 'router';
        this.nodeId = 0;
        this.container = document.getElementById(elementId);
        this.nodes = new vis.DataSet();
        this.edges = new vis.DataSet();
        this.links = {};
        this.networkData = {
            nodes: this.nodes,
            edges: this.edges
        };
        this.networkOptions = {
            height: '95%',
            physics: false,
            interaction: {
                selectConnectedEdges: false
            },
            edges: {
                shadow: true,
                width: 3,
                smooth: false,
                color: {
                    color: '#000000'
                }
            },
            nodes: {
                shadow: true
            }
        };
        this.network = new vis.Network(this.container, this.networkData, this.networkOptions);
        this.network.on('doubleClick', this.addNode.bind(this));
        this.edges.on('add', this.addEdge.bind(this));
        this.nodesEnabled = false;
    }

    enableNodeCreation(enabled) {
        console.log('node created enabled: ', enabled);
        this.nodesEnabled = enabled;
    }

    getCoreNodes() {
        const coreNodes = [];
        for (let node of this.nodes.get()) {
            coreNodes.push(node.coreNode.json());
        }
        return coreNodes;
    }

    addCoreNode(node) {
        const position = node.position;
        const coreNode = new CoreNode(node.id, node.type, node.name, position.x, position.y);
        coreNode.model = node.model;
        this.nodes.add(coreNode.getNetworkNode());
    }

    nextNodeId() {
        this.nodeId += 1;
        return this.nodeId;
    }

    joinedSessions(nodes) {
        const self = this;
        const nodeIds = [0];

        for (let node of nodes) {
            if (node.type === CoreNodeHelper.ptpNode) {
                continue;
            }

            nodeIds.push(node.id);
            this.addCoreNode(node);
        }

        for (let node of nodes) {
            if (!CoreNodeHelper.isNetworkNode(node)) {
                continue;
            }

            this.coreRest.getLinks(node.id)
                .then(function(response) {
                    console.log('link response: ', response);
                    for (let linkData of response.links) {
                        self.createEdgeFromLink(linkData);
                    }
                })
                .catch(function(err) {
                    console.log('get link error: ', err);
                });
        }

        if (nodes.length) {
            this.nodeId = Math.max.apply(Math, nodeIds) || 0;
        } else {
            this.nodeId = 0;
        }
    }

    createEdgeFromLink(linkData) {
        const fromNode = this.nodes.get(linkData.node1_id).coreNode;
        const toNode = this.nodes.get(linkData.node2_id).coreNode;
        const linkId = `${fromNode.id}-${toNode.id}`;

        let interfaceOne = null;
        if (linkData.interface1_id !== null) {
            interfaceOne = {
                id: linkData.interface1_id,
                ip4: linkData.interface1_ip4,
                ip4mask: linkData.interface1_ip4_mask,
                ip6: linkData.interface1_ip6,
                ip6mask: linkData.interface1_ip6_mask
            };
            fromNode.interfaces[linkData.interface1_id] = interfaceOne;
        }

        let interfaceTwo = null;
        if (linkData.interface2_id !== null) {
            interfaceTwo = {
                id: linkData.interface2_id,
                ip4: linkData.interface2_ip4,
                ip4mask: linkData.interface2_ip4_mask,
                ip6: linkData.interface2_ip6,
                ip6mask: linkData.interface2_ip6_mask
            };
            toNode.interfaces[linkData.interface2_id] = interfaceTwo;
        }

        this.links[linkId] = {
            node_one: fromNode.id,
            node_two: toNode.id,
            interface_one: interfaceOne,
            interface_two: interfaceTwo
        };

        const edge = {
            recreated: true,
            from: fromNode.id,
            to: toNode.id
        };
        setEdgeData(edge, linkId, fromNode, toNode, interfaceOne, interfaceTwo);
        this.edges.add(edge);
    }

    async start() {
        const nodes = coreNetwork.getCoreNodes();
        for (let node of nodes) {
            const response = await coreRest.createNode(node);
            console.log('created node: ', response);
        }

        for (let linkId in this.links) {
            const link = this.links[linkId];
            const response = await coreRest.createLink(link);
            console.log('created link: ', response);
        }

        return await coreRest.setSessionState(SessionStates.instantiation);
    }

    addNode(properties) {
        if (!this.nodesEnabled) {
            console.log('node creation disabled');
            return;
        }

        console.log('add node event: ', properties);
        if (properties.nodes.length !== 0) {
            return;
        }

        const {x, y} = properties.pointer.canvas;
        const nodeId = this.nextNodeId();
        const nodeDisplay = CoreNodeHelper.getDisplay(this.nodeType);
        const name = `${nodeDisplay.name}${nodeId}`;
        const coreNode = new CoreNode(nodeId, this.nodeType, name, x, y);
        coreNode.model = this.nodeModel;
        this.nodes.add(coreNode.getNetworkNode());
        console.log('added node: ', coreNode.getNetworkNode());
    }

    addEdge(_, properties) {
        const edgeId = properties.items[0];
        const edge = this.edges.get(edgeId);
        if (edge.recreated) {
            console.log('ignoring recreated edge');
            return;
        }

        console.log('added edge: ', edgeId, edge);
        if (edge.from === edge.to) {
            console.log('removing cyclic edge');
            this.edges.remove(edge.id);
            setTimeout(() => this.network.addEdgeMode(), 250);
            return;
        }

        const fromNode = this.nodes.get(edge.from).coreNode;
        const toNode = this.nodes.get(edge.to).coreNode;

        this.addEdgeLink(edge, fromNode, toNode)
            .then(function() {
                console.log('create edge link success!');
            })
            .catch(function(err) {
                console.log('create link error: ', err);
            });

        setTimeout(() => this.network.addEdgeMode(), 250);
    }

    async addEdgeLink(edge, fromNode, toNode) {
        const linkId = `${fromNode.id}-${toNode.id}`;
        let interfaceOne = null;
        if (fromNode.type === CoreNodeHelper.defaultNode) {
            const fromIps = await this.coreRest.getNodeIps(fromNode.id, Ip4Prefix, Ip6Prefix);
            console.log('from ips: ', fromIps);
            const interfaceOneId = Object.keys(fromNode.interfaces).length;
            interfaceOne = {
                id: interfaceOneId,
                ip4: fromIps.ip4,
                ip4mask: fromIps.ip4mask,
                ip6: fromIps.ip6,
                ip6mask: fromIps.ip6mask
            };
            fromNode.interfaces[interfaceOneId] = interfaceOne;
        }

        let interfaceTwo = null;
        if (toNode.type === CoreNodeHelper.defaultNode) {
            const toIps = await this.coreRest.getNodeIps(toNode.id, Ip4Prefix, Ip6Prefix);
            console.log('to ips: ', toIps);
            const interfaceTwoId = Object.keys(toNode.interfaces).length;
            interfaceTwo = {
                id: interfaceTwoId,
                ip4: toIps.ip4,
                ip4mask: toIps.ip4mask,
                ip6: toIps.ip6,
                ip6mask: toIps.ip6mask
            };
            toNode.interfaces[interfaceTwoId] = interfaceTwo;
        }

        this.links[linkId] = {
            node_one: fromNode.id,
            node_two: toNode.id,
            interface_one: interfaceOne,
            interface_two: interfaceTwo
        };

        setEdgeData(edge, linkId, fromNode, toNode, interfaceOne, interfaceTwo);
        this.edges.update(edge);
    }

    linkMode(enabled) {
        console.log('link mode:', enabled);
        if (enabled) {
            this.network.addEdgeMode();
        } else {
            this.network.disableEditMode();
        }
    }

    setNodeMode(nodeType, model) {
        this.nodeType = nodeType;
        this.nodeModel = model || null;
    }
}