"""
A federated learning server for personalized FL.
"""
import logging
import os
import pickle
import sys
import time
from plato.config import Config
from plato.servers import fedavg
from plato.utils import csv_processor


class Server(fedavg.Server):
    """A federated learning server for personalized FL."""
    def __init__(self, model=None, algorithm=None, trainer=None):
        super().__init__(model=model, algorithm=algorithm, trainer=trainer)
        self.do_personalization_test = False

        # A list to store accuracy of clients' personalized models
        self.personalization_test_updates = []
        self.personalization_accuracy = 0

    async def select_testing_clients(self):
        """Select a subset of the clients to test personalization."""

        logging.info("\n[Server #%d] Starting testing personalization.",
                     os.getpid())

        if hasattr(Config().clients, 'simulation') and Config(
        ).clients.simulation and not Config().is_central_server:
            # In the client simulation mode, the client pool for client selection contains
            # all the virtual clients to be simulated
            self.clients_pool = list(range(1, 1 + self.total_clients))

        else:
            # If no clients are simulated, the client pool for client selection consists of
            # the current set of clients that have contacted the server
            self.clients_pool = list(self.clients)

        self.selected_clients = self.choose_clients(self.clients_pool,
                                                    self.clients_per_round)

        if len(self.selected_clients) > 0:
            for i, selected_client_id in enumerate(self.selected_clients):
                if hasattr(Config().clients, 'simulation') and Config(
                ).clients.simulation and not Config().is_central_server:
                    client_id = i + 1
                else:
                    client_id = selected_client_id

                sid = self.clients[client_id]['sid']

                logging.info(
                    "[Server #%d] Selecting client #%d for personalization testing.",
                    os.getpid(), selected_client_id)

                server_response = {
                    'id': selected_client_id,
                    'personalization_test': True
                }

                # Sending the server response as metadata to the clients (payload to follow)
                await self.sio.emit('payload_to_arrive',
                                    {'response': server_response},
                                    room=sid)

                payload = self.algorithm.extract_weights()
                payload = self.customize_server_payload(payload)

                # Sending the server payload to the client
                logging.info(
                    "[Server #%d] Sending the meta model to client #%d.",
                    os.getpid(), selected_client_id)
                await self.send(sid, payload, selected_client_id)

    async def process_reports(self):
        """Process the client reports by aggregating their weights."""
        if self.do_personalization_test:
            self.compute_personalization_accuracy()
            await self.wrap_up_processing_reports()
        else:
            await super().process_reports()

    def compute_personalization_accuracy(self):
        """"Average accuracy of clients' personalized models."""
        accuracy = 0
        for report in self.personalization_test_updates:
            accuracy += report
        self.personalization_accuracy = accuracy / len(
            self.personalization_test_updates)

    async def wrap_up_processing_reports(self):
        """Wrap up processing the reports with any additional work."""
        if self.do_personalization_test:

            if hasattr(Config(), 'results'):
                new_row = []
                for item in self.recorded_items:
                    item_value = {
                        'round':
                        self.current_round,
                        'accuracy':
                        self.accuracy * 100,
                        'personalization_accuracy':
                        self.personalization_accuracy * 100,
                        'training_time':
                        max([
                            report.training_time
                            for (report, __) in self.updates
                        ]),
                        'round_time':
                        time.perf_counter() - self.round_start_time
                    }[item]
                    new_row.append(item_value)

                result_csv_file = Config().result_dir + 'result.csv'

                csv_processor.write_csv(result_csv_file, new_row)

            self.do_personalization_test = False

    async def client_payload_done(self, sid, client_id, object_key):
        """ Upon receiving all the payload from a client, eithe via S3 or socket.io. """
        if object_key is None:
            assert self.client_payload[sid] is not None

            payload_size = 0
            if isinstance(self.client_payload[sid], list):
                for _data in self.client_payload[sid]:
                    payload_size += sys.getsizeof(pickle.dumps(_data))
            else:
                payload_size = sys.getsizeof(
                    pickle.dumps(self.client_payload[sid]))
        else:
            self.client_payload[sid] = self.s3_client.receive_from_s3(
                object_key)
            payload_size = sys.getsizeof(pickle.dumps(
                self.client_payload[sid]))

        logging.info(
            "[Server #%d] Received %s MB of payload data from client #%d.",
            os.getpid(), round(payload_size / 1024**2, 2), client_id)

        if self.client_payload[sid] == 'personalization_accuracy':
            self.personalization_test_updates.append(self.reports[sid])
        else:
            self.updates.append((self.reports[sid], self.client_payload[sid]))

        if len(self.personalization_test_updates) == 0:
            if len(self.updates) > 0 and len(self.updates) >= len(
                    self.selected_clients):
                logging.info(
                    "[Server #%d] All %d client reports received. Processing.",
                    os.getpid(), len(self.updates))
                self.do_personalization_test = False
                await self.process_reports()

                # Start testing the global meta model w.r.t. personalization
                await self.select_testing_clients()

        if len(self.personalization_test_updates) > 0 and len(
                self.personalization_test_updates) >= len(
                    self.selected_clients):
            logging.info(
                "[Server #%d] All %d personalization test results received.",
                os.getpid(), len(self.personalization_test_updates))

            self.do_personalization_test = True
            await self.process_reports()

            await self.wrap_up()
            self.personalization_test_updates = []

            # Start a new round of FL training
            await self.select_clients()
