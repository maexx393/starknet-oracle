from datetime import datetime
import os
import zmq
import sys
import json
import threading
from time import sleep, time
from pickle import dumps, loads
import signal

from starkware.cairo.common.hash_state import compute_hash_on_elements
from starkware.crypto.signature.signature import sign, verify

from classes.report_class import Report
import helpers.helpers as h
from leader import LeaderState

# ? ===========================================================================
file_path = os.path.join(
    os.path.normpath(os.getcwd() + os.sep + os.pardir + os.sep + os.pardir),
    "tests/dummy_data/dummy_keys.json")
f = open(file_path, 'r')
keys = json.load(f)
f.close()

public_keys = keys["keys"]["public_keys"]
private_keys = keys["keys"]["private_keys"]
# ? ===========================================================================

T_ROUND = 70
T_GRACE = 3


class LeaderNode(LeaderState):
    '''
    This node is only initialized for one instance of the report-generation protocol,
    when the current node is selected to be leader and is destroyed when it ends.
    It runs in a separate thread, alongside the follower_node and is responsible for
    cordenating other nodes until the end of the epoch.
    @arguments:
        - index: the index of the current node (to identify participants in the network)
        - epoch: the epoch number
        - publisher: the publisher socket (see below)
        - num_nodes: the number of nodes in the network
        - max_round: the maximum number of rounds leader is allowed to run before choosing a new one
    '''

    def __init__(self, index, epoch, publisher, num_nodes, max_round):
        super().__init__(index, epoch, num_nodes, max_round)
        self.context = zmq.Context()
        # * This is the socket from which the follower will brodcast messages to other oracles
        self.publisher = publisher
        # * These sockets are used to receive messages from other oracles
        self.subscriptions = h.subscribe_to_other_nodes_leader(self.context)
        # * Poller is used to reduce the cpu strain
        self.poller = zmq.Poller()
        for sub in self.subscriptions:
            self.poller.register(sub, zmq.POLLIN)
        # * round_timer is used to start a new round after T_ROUND seconds
        self.round_timer = h.ResettingTimer(
            T_ROUND, self.emit_new_round_event, self.publisher)
        # * grace_timer is used to give slow nodes T_GRACE seconds to send their observations
        self.grace_timer = h.ResettingTimer(
            T_GRACE, self.assemble_report, self.publisher)
        self.stop_event = threading.Event()

    def run_(self):
        while True:

            try:
                socks = dict(self.poller.poll())
            except KeyboardInterrupt:
                break
            except Exception as e:
                print("Exception: {}".format(e))
                continue

            for sub in self.subscriptions:

                if self.stop_event.is_set():
                    print(f"Stopping leader_node {self.index}")
                    return

                if sub in socks:
                    try:
                        msg = sub.recv_multipart()
                        # ? ==========================================================================
                        # SECTION Start a new round
                        if msg[0] == b'START-EPOCH' or msg[0] == b'NEW-ROUND':
                            self.start_round()
                            self.round_timer.start()
                            self.publisher.send_multipart(
                                [b"OBSERVE-REQ", dumps({"round_n": self.round_num})])
                        # _ !SECTION
                        # ? ==========================================================================
                        # SECTION Recieve an observation
                        if msg[0] == b'OBSERVE':
                            round_n, observation, signature = loads(msg[1])["round_n"], loads(msg[1])[
                                "observation"], loads(msg[1])["signature"]
                            node_idx = int(sub.get(zmq.IDENTITY).decode())

                            if round_n != self.round_num:
                                print("ERROR: Round number mismatch in OBSERVE\n")
                                continue
                            if not (self.phase == "OBSERVE" or self.phase == "GRACE"):
                                print("ERROR: Phase should be OBSERVE or GRACE")
                                continue

                            if self.observations[node_idx]:
                                print(
                                    "ERROR: Observation already received from this node for this round")
                                print("\n ", (observation, signature, node_idx))
                                continue

                            msg_hash = compute_hash_on_elements(
                                [self.epoch, round_n, observation])
                            if verify(msg_hash, signature[0], signature[1], public_keys[node_idx]):

                                self.observations[node_idx] = (
                                    (observation, signature, node_idx))

                                if len([1 for x in self.observations if x]) == 2*self.F + 1:
                                    if self.phase != 'OBSERVE':
                                        print(
                                            'ERROR: Phase must be OBSERVE')
                                        continue

                                    # print("GRACE TIMER STARTED")
                                    self.phase = "GRACE"
                                    self.grace_timer.start()
                        # _ !SECTION
                        # ? ===========================================================================
                        # SECTION Recieve a Report
                        if msg[0] == b'REPORT':
                            round_n, report, signature = loads(msg[1])["round_n"], loads(msg[1])[
                                "report"], loads(msg[1])["signature"]

                            if self.current_report.msg_hash() != report.msg_hash():
                                print("ERROR: Report mismatch")
                                continue

                            node_idx = int(sub.get(zmq.IDENTITY).decode())
                            public_key = public_keys[node_idx]

                            if self.current_report.verify_report_signature(public_key, signature):

                                self.reports.append(
                                    (report, signature, node_idx))

                                if len(self.reports) > self.F:
                                    self.finalize_report(
                                        report, self.publisher)

                        # _ !SECTION
                        # ? ===========================================================================

                    except Exception as e:
                        print("Exception: {}".format(e))
                        continue

    def run(self):
        '''
        This function starts a thread so it can run in parallel with the follower_node
        '''
        thread = threading.Thread(target=self.run_)
        thread.start()

    def stop(self):
        '''
        This function stops the running thread so it can be removed for garbage collection
        '''
        self.round_timer.cancel()
        self.stop_event.set()
        self.context.destroy(linger=0)
