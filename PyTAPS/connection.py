import asyncio
import json
import sys
import ssl
from .endpoint import LocalEndpoint, RemoteEndpoint
from .transportProperties import *
from .utility import *
from .transports import *
from .multicast import do_join
color = "green"


class ConnectionState(Enum):
    ESTABLISHING = 0
    ESTABLISHED = 1
    CLOSING = 2
    CLOSED = 3

class Connection():
    """The TAPS connection class.

    Attributes:
        preconnection (Preconnection, required):
                Preconnection object from which this Connection
                object was created.
    """
    def __init__(self, preconnection):
                # Initializations
                self.local_endpoint = preconnection.local_endpoint
                self.remote_endpoint = preconnection.remote_endpoint
                self.transport_properties = preconnection.transport_properties
                self.security_parameters = preconnection.security_parameters
                self.loop = preconnection.loop
                self.active = preconnection.active
                self.framer = preconnection.framer
                self.set_callbacks(preconnection)
                self.pending = []
                # Security Context for SSL
                self.security_context = None
                # Current state of the connection object
                self.state = ConnectionState.ESTABLISHING

                self.transports = []
                """
                if self.protocol == "udp" and not self.active:
                    self.handler = preconnection.handler"""

    async def race(self):
        # This is an active connection attempt
        self.active = True

        # Create the set of possible protocol candidates
        candidate_set = self.create_candidates()
        # If security_parameters were given, initialize ssl context
        if self.security_parameters:
            self.security_context = ssl.create_default_context(
                                                ssl.Purpose.SERVER_AUTH)
            if self.security_parameters.identity:
                print_time("Identity: " +
                           str(self.security_parameters.identity))
                self.security_context.load_cert_chain(
                                        self.security_parameters.identity)
            for cert in self.security_parameters.trustedCA:
                self.security_context.load_verify_locations(cert)
        # Resolve address
        remote_info = await self.loop.getaddrinfo(
            self.remote_endpoint.host_name, self.remote_endpoint.port)
        self.remote_endpoint.address = remote_info[0][4][0]
        for candidate in candidate_set:

            if self.state == ConnectionState.ESTABLISHED:
                break

            if candidate[0] == 'udp':
                self.protocol = 'udp'
                print_time("Creating UDP connect task.", color)
                task = asyncio.create_task(self.loop.create_datagram_endpoint(
                                    lambda: UdpTransport(connection=self, remote_endpoint=self.remote_endpoint),
                                    remote_addr=(self.remote_endpoint.address,
                                                 self.remote_endpoint.port)))

            elif candidate[0] == 'tcp':
                self.protocol = 'tcp'
                print_time("Creating TCP connect task.", color)
                task = asyncio.create_task(self.loop.create_connection(
                                    lambda: TcpTransport(connection=self, remote_endpoint=self.remote_endpoint),
                                    self.remote_endpoint.address,
                                    self.remote_endpoint.port,
                                    ssl=self.security_context,
                                    server_hostname=(self.remote_endpoint.host_name if self.security_context else None)))
        # self.pending.append(task)
        # await asyncio.sleep(1)
        # Wait until the correct connection object has been set
        # await self.await_connection()

    async def send_message(self, data):
        """ Attempts to send data on the connection.
            Attributes:
                data (string, required):
                    Data to be send.
        """
        return self.transports[0].send(data)

    async def receive(self, min_incomplete_length=float("inf"), max_length=-1):
        self.transports[0].receive(min_incomplete_length, max_length)

    def close(self):
        """ Attempts to close the connection, issues a closed event
        on success.
        """
        self.loop.create_task(self.transports[0].close())
        self.state = ConnectionState.CLOSING

    def create_candidates(self):
        """ Decides which protocols are candidates and then orders them
        according to the TAPS interface draft
        """
        # Get the protocols know to the implementation from transportProperties
        available_protocols = get_protocols()

        # At the beginning, all protocols are candidates
        candidate_protocols = dict([(row["name"], list((0, 0)))
                                   for row in available_protocols])

        # Iterate over all available protocols and over all properties
        for protocol in available_protocols:
            for transport_property in self.transport_properties.properties:
                # If a protocol has a prohibited property remove it
                if (self.transport_properties.properties[transport_property]
                        is PreferenceLevel.PROHIBIT):
                    if (protocol[transport_property] is True and
                            protocol["name"] in candidate_protocols):
                        del candidate_protocols[protocol["name"]]
                # If a protocol doesnt have a required property remove it
                if (self.transport_properties.properties[transport_property]
                        is PreferenceLevel.REQUIRE):
                    if (protocol[transport_property] is False and
                            protocol["name"] in candidate_protocols):
                        del candidate_protocols[protocol["name"]]
                # Count how many PREFER properties each protocol has
                if (self.transport_properties.properties[transport_property]
                        is PreferenceLevel.PREFER):
                    if (protocol[transport_property] is True and
                            protocol["name"] in candidate_protocols):
                        candidate_protocols[protocol["name"]][0] += 1
                # Count how many AVOID properties each protocol has
                if (self.transport_properties.properties[transport_property]
                        is PreferenceLevel.AVOID):
                    if (protocol[transport_property] is True and
                            protocol["name"] in candidate_protocols):
                        candidate_protocols[protocol["name"]][1] -= 1

        # Sort candidates by number of PREFERs and then by AVOIDs on ties
        sorted_candidates = sorted(candidate_protocols.items(),
                                   key=lambda value: (value[1][0],
                                   value[1][1]), reverse=True)

        return sorted_candidates

    def set_callbacks(self, preconnection):
            self.ready = preconnection.ready
            self.initiate_error = preconnection.initiate_error
            self.connection_received = preconnection.connection_received
            self.listen_error = preconnection.listen_error
            self.stopped = preconnection.stopped
            self.sent = None
            self.send_error = None
            self.expired = None
            self.connection_error = None
            self.received = None
            self.received_partial = None
            self.receive_error = None
            self.closed = None
            self.reader = None
            self.writer = None

    # Events for active open
    def on_ready(self, callback):
        """ Set callback for ready events that get thrown once the connection is ready
        to send and receive data.

        Attributes:
            callback (callback, required): Function that implements the
                callback.
        """
        self.ready = callback

    def on_initiate_error(self, callback):
        """ Set callback for initiate error events that get thrown if an error occurs
        during initiation.

        Attributes:
            callback (callback, required): Function that implements the
                callback.
        """
        self.initiate_error = callback

    # Events for sending messages
    def on_sent(self, callback):
        """ Set callback for sent events that get thrown if a message has been
        succesfully sent.

        Attributes:
            callback (callback, required): Function that implements the
                callback.
        """
        self.sent = callback

    def on_send_error(self, callback):
        """ Set callback for send error events that get thrown if an error occurs
        during sending of a message.

        Attributes:
            callback (callback, required): Function that implements the
                callback.
        """
        self.send_error = callback

    def on_expired(self, callback):
        """ Set callback for expired events that get thrown if a message expires.

        Attributes:
            callback (callback, required): Function that implements the
                callback.
        """
        self.expired = callback

    # Events for receiving messages
    def on_received(self, callback):
        """ Set callback for received events that get thrown if a new message
        has been received.

        Attributes:
            callback (callback, required): Function that implements the
                callback.
        """
        self.received = callback

    def on_received_partial(self, callback):
        """ Set callback for partial received events that get thrown if a new partial
        message has been received.

        Attributes:
            callback (callback, required): Function that implements the
                callback.
        """
        self.received_partial = callback

    def on_receive_error(self, callback):
        """ Set callback for receive error events that get thrown if an error occurs
        during reception of a message.

        Attributes:
            callback (callback, required): Function that implements the
                callback.
        """
        self.receive_error = callback

    def on_connection_error(self, callback):
        """ Set callback for connection error events that get thrown if an error occurs
        while the connection is open.

        Attributes:
            callback (callback, required): Function that implements the
                callback.
        """
        self.connection_error = callback

    # Events for closing a connection
    def on_closed(self, callback):
        """ Set callback for on closed events that get thrown if the
        connection has been closed succesfully.

        Attributes:
            callback (callback, required): Function that implements the
                callback.
        """
        self.closed = callback


""" ASYNCIO function that receives data from multicast flows
"""
async def do_multicast_receive():
    if multicast.do_receive():
        asyncio.create_task(do_multicast_receive())

