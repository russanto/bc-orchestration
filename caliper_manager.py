import docker
import json
import logging
import os
import queue
import sys
from threading import Thread
import time
from web3 import Web3, HTTPProvider
import yaml

from caliper_manager_adapter import CaliperManagerAdapter
from deploy_manager import DeployManager
from host_manager import HostManager
from parity_manager import ParityManager

class CaliperManager(DeployManager):

    remote_caliper_dir = "/home/ubuntu/caliper"
    remote_network_conf_file = os.path.join(remote_caliper_dir, "benchmark.json")
    docker_container_server_name = "caliper"
    docker_container_client_name = "zookeeper-client"
    docker_container_datadir = "/root/caliper"
    reports_dir = "/home/ubuntu/reports"
    tmp_dir = "tmp"

    def __init__(self, manager_adapter, workload_file):
        if not isinstance(manager_adapter, CaliperManagerAdapter):
            raise Exception("CaliperManager requires a CaliperManagerAdapter. Given %s" % type(manager_adapter).__name__)
        super().__init__(manager_adapter.hosts)
        self.logger = logging.getLogger("CaliperManager")
        self.manager_adapter = manager_adapter
        self.workload_file = workload_file
    
    def parse_conf(self, conf_as_dict): # TODO integrate this function inside kwargs on __init__
        if "REPORTS_DIR" in conf_as_dict:
            self.reports_dir = conf_as_dict["REPORTS_DIR"]

    def _init(self):
        self.hosts_connections = HostManager.get_hosts_connections(self.hosts)
        self.local_connections = HostManager.get_local_connections()
        if "docker" in self.local_connections:
            local_docker = self.local_connections["docker"]["client"]
            try:
                local_zookeeper = local_docker.containers.get("zookeeper")
                local_zookeeper.stop()
                local_zookeeper.remove()
                self.logger.info("Previous execution Zookeeper server found, stopped and removed")
            except docker.errors.NotFound:
                self.logger.info("Zookeeper server not found")
            except:
                raise
            try:
                self.logger.info("Deploying Zookeeper server")
                self.local_connections["docker"]["containers"]["zookeeper"] = local_docker.containers.run(
                    self.dinr.resolve("zookeeper"),
                    detach=True,
                    name="zookeeper",
                    network=self.manager_adapter.manager.docker_network_name,
                    ports={
                        '2181/tcp': 2181,
                        '2888/tcp': 2888,
                        '3888/tcp': 3888
                    })
                self.logger.info("Zookeeper server deployed")
            except docker.errors.APIError as error:
                if error.status_code == 409:
                    self.logger.warning("Zookeeper port is already being used")
                else:
                    self.logger.error(error)
            self.manager_adapter.init()
            return True
        else:
            self.logger.error("Can't initialize: error with local docker client")
            return False

    def _start_loop(self, host):
        network_conf_file = self.manager_adapter.get_network_conf_file(host)
        docker = self.hosts_connections[host]["docker"]
        try:
            self.hosts_connections[host]["ssh"].put(network_conf_file, remote=self.remote_network_conf_file)
            docker["containers"][self.docker_container_client_name] = docker["client"].containers.run(
                self.dinr.resolve("caliper-client"),
                name=self.docker_container_client_name,
                detach=True,
                network=self.manager_adapter.manager.docker_network_name,
                environment={
                    "ZOO_SERVER": self.local_connections["ip"],
                    "BLOCKCHAIN": "benchmark",
                    "BC_CONF": "benchmark"
                }, volumes={
                    self.remote_network_conf_file: {
                        "bind":"/caliper/packages/caliper-application/network/benchmark/benchmark/benchmark.json",
                        "mode":"rw"
                    }
                })
            self.logger.info("[%s]Zookeeper client deployed" % host)
        except docker.errors.APIError as error:
            self.logger.error("[%s]Error on docker creation of zoo client" % host)
            self.logger.error(error)
        except FileNotFoundError as error:
            self.logger.error(error)
    
    def _start_teardown(self):
        self._start_caliper_workload()
        self.logger.info("Caliper started. Fetching live log...")
        log_stream = self.local_connections["docker"]["containers"][self.docker_container_server_name].logs(follow=True, stream=True)
        for line in log_stream:
            print(line.decode("utf-8").strip("\n"))

    def _start_caliper_workload(self):
        # Adds to the workload conf all the host to monitor
        docker_rapi_hosts = []
        for host in self.hosts:
            docker_rapi_hosts.append("http://%s:2375/%s" % (host, self.manager_adapter.docker_node_name))
        with open(self.workload_file) as config_file:
            config_data = yaml.load(config_file)
        config_data["monitor"]["docker"]["name"] = docker_rapi_hosts
        with open(self.workload_file, "w") as config_file:
            yaml.dump(config_data, config_file, default_flow_style=False)
        self.logger.info("Updated workload configuration")

        self.logger.info("Starting caliper")
        local_docker = self.local_connections["docker"]["client"]
        self.local_connections["docker"]["containers"][self.docker_container_server_name] = local_docker.containers.run(
            self.dinr.resolve("caliper-server"),
            name=self.docker_container_server_name,
            detach=True,
            network=self.manager_adapter.manager.docker_network_name,
            environment={
                "BLOCKCHAIN": "benchmark",
                "BC_CONF": "benchmark",
                "BENCHMARK": "simple"
            }, volumes={
                HostManager.resolve_local_path(os.path.abspath(self.workload_file)): { # This must point to local host datadir
                    "bind": "/caliper/packages/caliper-application/benchmark/simple/config-benchmark.yaml",
                    "mode": "rw"
                },
                HostManager.resolve_local_path(self.manager_adapter.get_network_conf_file()): { # This must point to local host datadir
                    "bind": "/caliper/packages/caliper-application/network/benchmark/benchmark/benchmark.json",
                    "mode": "rw"
                },
                HostManager.resolve_local_path(self.reports_dir): {
                    "bind": "/caliper/packages/caliper-application/reports",
                    "mode": "rw"
                }
            })
    
    def _cleanup_setup(self):
        local_docker = self.local_connections["docker"]["client"]
        try:
            caliper_server = local_docker.containers.get(self.docker_container_server_name)
            caliper_server.remove(force=True)
            self.logger.info("Caliper container found and removed")
        except docker.errors.APIError as error:
            if error.status_code == 404:
                pass
            else:
                raise

    def _cleanup_loop(self, host):
        docker_client = self.hosts_connections[host]["docker"]["client"]
        try:
            zookeeper_client = docker_client.containers.get(self.docker_container_client_name)
            zookeeper_client.stop()
            zookeeper_client.remove()
            self.logger.info("[{0}]Zookeeper client found, stopped and removed".format(host))
        except docker.errors.APIError as error:
            if error.status_code == 404:
                pass
            else:
                raise
    
    def _cleanup_teardown(self):
        self.logger.info("Cleanup completed")
                

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    from caliper_ethereum import CaliperEthereum
    from host_manager import HostManager
    from geth_manager import GethManager
    import sys, time
    hosts_file_path = sys.argv[1]
    blockchain = sys.argv[2]
    host_manager = HostManager()
    host_manager.add_hosts_from_file(hosts_file_path)
    hosts = host_manager.get_hosts()
    
    if blockchain == "geth":
        manager = GethManager(hosts)
        manager.parse_conf(os.environ)
        manager.set_consensus_protocol(GethManager.CLIQUE)
    elif blockchain == "parity":
        manager = ParityManager(hosts)
    else:
        raise Exception("Only parity and geth blockchain are supported. Given %s" % blockchain)

    manager.init()
    manager.cleanup()
    manager.start()
    manager.cmd_events[manager.CMD_START].wait()
    manager_adapter = CaliperEthereum(manager)
    caliper_manager = CaliperManager(manager_adapter, "./caliper/config-ethereum.yaml")
    caliper_manager.parse_conf(os.environ)
    caliper_manager.init()
    caliper_manager.cleanup()
    caliper_manager.start()
    caliper_manager.cmd_events[manager.CMD_START].wait()
    caliper_manager.stop()
    caliper_manager.deinit()
    manager.stop()
    manager.deinit()