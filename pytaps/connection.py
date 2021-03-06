import asyncio
import json
import sys
import ssl
import netifaces
from .endpoint import LocalEndpoint, RemoteEndpoint
from .transportProperties import *
from .utility import *
from .transports import *
import ipaddress
import socket
color = "green"

# Wait for 100 ms between connection attempts when racing
RACING_DELAY = 0.1


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
        self.sleeper_for_racing = SleepClassForRacing()
        self.pending = []
        # Security Context for SSL
        self.security_context = None
        # Current state of the connection object
        self.state = ConnectionState.ESTABLISHING
        # List of possible underlying transports
        self.transports = []
        self.multicast_open = False

    async def race(self):
        # This is an active connection attempt
        self.active = True

        # Create the set of possible protocol candidates
        protocol_candidates = self.create_candidates()

        if len(protocol_candidates) == 0:
            print_time("Candidate set is empty, aborting", color)
            if self.initiate_error is not None:
                self.loop.create_task(self.initiate_error())
            return
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

        if self.remote_endpoint.host_name is not None:
            # Resolve address
            # FIXME: Unfortunately, asyncio getaddrinfo does not
            # FIXME: allow to resolve on specific interfaces
            # FIXME: Consider migrating to something better, e.g., getdns
            remote_info = await self.loop.getaddrinfo(
                self.remote_endpoint.host_name, self.remote_endpoint.port)
            # Concat v6 and v4 address lists, making sure we try v6 first
            remote_addrs_v6 = list(
                    set([
                        info[4][0] for info in remote_info
                        if info[0] == socket.AddressFamily.AF_INET6]
                        )
                )
            remote_addrs_v4 = list(
                    set([
                        info[4][0] for info in remote_info
                        if info[0] == socket.AddressFamily.AF_INET]
                        )
                )
            remote_addrs = remote_addrs_v6 + remote_addrs_v4
            print_time("Resolved " + str(self.remote_endpoint.host_name) +
                       " to " + str(remote_addrs), color)

        else:
            remote_addrs = self.remote_endpoint.address
            print_time("Not resolving - using address " +
                       str(self.remote_endpoint.address) + " --> " +
                       str(remote_addrs), color)

        if self.local_endpoint is not None:
            # Local interface specified -->
            # try local addresses on that interface
            for local_interface in self.local_endpoint.interface:
                try:
                    # Unfortunately, link-local IPv6 addresses don't work
                    # because they're broken in
                    # asyncio: https://bugs.python.org/issue35545
                    local_v6_addrs = [entry['addr']
                                      for entry in netifaces.ifaddresses
                                      (local_interface)[netifaces.AF_INET6]
                                      if entry['addr'][:4] != "fe80"]
                    local_v4_addrs = [entry['addr']
                                      for entry in netifaces.ifaddresses
                                      (local_interface)[netifaces.AF_INET]]
                    print_time("Trying addresses of local interface " +
                               str(self.local_endpoint.interface) + " --> " +
                               str(local_v6_addrs) + ", " +
                               str(local_v4_addrs), color)
                except ValueError as err:
                    print_time("Cannot get IP addresses for " +
                               str(self.local_endpoint.interface) + ": " +
                               str(err), color)
                    # TODO throw error
            # Build candidate set for racing
            # based on combinations of protocol, local and remote IP address
            candidate_set = [protocol + (remote_address,) + (local_address,)
                             for remote_address in remote_addrs_v6
                             for protocol in protocol_candidates
                             for local_address in local_v6_addrs]
            candidate_set += [protocol + (remote_address,) + (local_address,)
                              for remote_address in remote_addrs_v4
                              for protocol in protocol_candidates
                              for local_address in local_v4_addrs]
            print_time("Final Candidates: " + str(candidate_set))

        else:
            # Build candidate set for racing
            # based on combinations of protocol and remote IP address
            candidate_set = [protocol + (address,)
                             for address in remote_addrs
                             for protocol in protocol_candidates]

        # Attempt to establish a connection with each candidate
        for candidate in candidate_set:

            if self.state == ConnectionState.ESTABLISHED:
                print_time("Connection established -- stop racing", color)
                break

            print_time("Trying candidate protocol: " + str(candidate[0]) +
                       " and remote address: " + str(candidate[2]) +
                       (" and local address: " + str(candidate[3])
                       if len(candidate) > 3 else ""), color)
            if len(candidate) > 3:
                # bind to a specific local address
                local_address_to_use = (candidate[3], None)
                self.local_endpoint.address = candidate[3]
            else:
                local_address_to_use = None

            if candidate[0] == 'udp':
                self.protocol = 'udp'
                print_time("Creating UDP connect task with remote addr " +
                           str(candidate[2]) + ", port " +
                           str(self.remote_endpoint.port), color)
                self.remote_endpoint.address = candidate[2]
                if not self.local_endpoint:
                    if self.initiate_error:
                        self.loop.create_task(self.initiate_error(self))

                # Create a datagram endpoint
                task = self.loop.create_task(
                    self.loop.create_datagram_endpoint(
                        lambda: UdpTransport(
                                    connection=self,
                                    remote_endpoint=self.remote_endpoint),
                        remote_addr=(self.remote_endpoint.address,
                                     self.remote_endpoint.port),
                        local_addr=local_address_to_use))

                print_time("Not racing multiple addrs for UDP" +
                           " -- stop racing", color)
                break

            elif candidate[0] == 'tcp':
                self.protocol = 'tcp'
                print_time("Creating TCP connect task to " + candidate[2] +
                           ".", color)
                self.remote_endpoint.address = candidate[2]
                # If the protocol is tcp, create a asyncio connection
                task = self.loop.create_task(
                        self.loop.create_connection(
                            lambda: TcpTransport(
                                connection=self,
                                remote_endpoint=self.remote_endpoint),
                            self.remote_endpoint.address,
                            self.remote_endpoint.port,
                            ssl=self.security_context,
                            server_hostname=(
                                self.remote_endpoint.host_name
                                if self.security_context else None),
                            local_addr=local_address_to_use))
                # Wait before starting next connection attempt
                await self.sleeper_for_racing.sleep(RACING_DELAY)

    async def send_message(self, data):
        """ Attempts to send data on the connection.
            Attributes:
                data (string, required):
                    Data to be send.
        """
        if isinstance(data, str):
            data = data.encode()
        return self.transports[0].send(data)

    async def receive(self, min_incomplete_length=float("inf"), max_length=-1):
        """ Queues the reception of a message.
        Attributes:
            min_incomplete_length (integer, optional):
                The minimum length an incomplete message
                needs to have.
            max_length (integer, optional):
                The maximum length a message can have.
        """
        self.transports[0].receive(min_incomplete_length, max_length)

    def close(self):
        """ Attempts to close the connection, issues a closed event
        on success.
        """
        if self.multicast_open:
            self.loop.create_task(self.multicast_leave())
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

    def parse(self, min_incomplete_length=0, max_length=0):
        """ Returns the message buffer of the
            connection.

        Attributes:
            connection (connection, required):
                The connection object from which the
                buffer should be returned.
        """
        return self.transports[0].recv_buffer, None, False

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
        """ Set callback for ready events that
            get thrown once the connection is ready
            to send and receive data.

        Attributes:
            callback (callback, required): Function that implements the
                callback.
        """
        self.ready = callback

    def on_initiate_error(self, callback):
        """ Set callback for initiate error events that
            get thrown if an error occurs
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
        """ Set callback for send error events
            that get thrown if an error occurs
            during sending of a message.

        Attributes:
            callback (callback, required): Function that implements the
                callback.
        """
        self.send_error = callback

    def on_expired(self, callback):
        """ Set callback for expired events that
            get thrown if a message expires.

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
        """ Set callback for partial received events that
            get thrown if a new partial
            message has been received.

        Attributes:
            callback (callback, required): Function that implements the
                callback.
        """
        self.received_partial = callback

    def on_receive_error(self, callback):
        """ Set callback for receive error events that
            get thrown if an error occurs
            during reception of a message.

        Attributes:
            callback (callback, required): Function that implements the
                callback.
        """
        self.receive_error = callback

    def on_connection_error(self, callback):
        """ Set callback for connection error events that
            get thrown if an error occurs
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
                callback.  Callback signature should accept a connection
                as its parameter.
        """
        self.closed = callback
