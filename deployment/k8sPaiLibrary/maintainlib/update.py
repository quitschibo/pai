# Copyright (c) Microsoft Corporation
# All rights reserved.
#
# MIT License
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the "Software"), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and
# to permit persons to whom the Software is furnished to do so, subject to the following conditions:
# The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED *AS IS*, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING
# BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
# DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

import sys
import time
import yaml
import requests
import logging
import logging.config

from . import add as k8s_add
from . import clean as k8s_clean
from . import common as k8s_common
from . import remove as k8s_remove

from ...confStorage import conf_storage_util
from ...confStorage.download import download_configuration
from ...paiLibrary.common import directory_handler
from ...paiLibrary.common import kubernetes_handler
from ...paiLibrary.clusterObjectModel import objectModelFactory


class update:

    def __init__(self, **kwargs):
        self.logger = logging.getLogger(__name__)

        self.kube_config_path = None
        if "kube_config_path" in kwargs and kwargs[ "kube_config_path" ] != None:
            self.kube_config_path = kwargs[ "kube_config_path" ]

        if self.kube_config_path == None:
            self.logger.error("Unable to find KUBECONFIG. Please ensure that you have passed the correct path.")
            sys.exit(1)

        self.time = str(int(time.time()))
        self.tmp_path = "./tmp-machine-update-{0}".format(self.time)

        self.k8s_configuration = None
        self.node_list_from_k8s = None
        self.node_config_from_cluster_conf = None
        self.node_config_from_k8s = None



    def get_latest_configuration_from_pai(self):

        self.logger.info("Try to get latest configuration from kubernetes.")
        directory_handler.directory_create(self.tmp_path)

        config_get = download_configuration(config_output_path=self.tmp_path, kube_config_path=self.kube_config_path)
        config_get.run()
        objectModel = objectModelFactory.objectModelFactory(self.tmp_path)
        ret = objectModel.objectModelPipeLine()

        directory_handler.directory_delete(self.tmp_path)
        self.logger.info("Successfully get latest configuration from Kubernetes.")
        return ret["k8s"]



    def get_node_list_from_k8s_api(self):
        self.logger.info("Try to get node list from kubernetes api.")
        node_list = kubernetes_handler.list_all_nodes(PAI_KUBE_CONFIG_PATH=self.kube_config_path)
        self.logger.info("Successfully get latest configuration from kubernetes.")
        return node_list



    def get_node_config_from_cluster_configuration(self):

        self.logger.info("Try to get node confguration from cluster configuration.")
        cluster_config = self.k8s_configuration
        node_config_from_cluster_conf = dict()

        for role in cluster_config["remote_deployment"]:
            listname = cluster_config["remote_deployment"][role]["listname"]
            if listname not in cluster_config:
                continue

            for node_key in cluster_config[listname]:
                node_config = cluster_config[listname][node_key]
                node_config_from_cluster_conf[node_key] = node_config

        self.logger.info("Successfully get latest configuration from cluster configuration.")
        return node_config_from_cluster_conf



    """
    Get node configuration from kubernetes configmap. 
    """
    def get_node_config_from_k8s(self):
        self.logger.info("Try to get node configuration from kubernetes' configmap.")
        configmap_data = kubernetes_handler.get_configmap(self.kube_config_path, "pai-node-config", "kube-system")

        if configmap_data == None:
            return None

        pai_node_list = configmap_data["data"]["node-list"]
        self.logger.info("Successfully get node configuration from kubernetes' configmap.")
        return yaml.load(pai_node_list)



    """
    Update node configuration in kubernetes configmap based on cluster configuration  
    """
    def update_node_config(self):
        self.logger.info("Try to update node configuration to kubernetes' configmap.")
        yaml_data = yaml.dump(self.node_config_from_cluster_conf, default_flow_style=False)
        pai_node_list = {"node-list": yaml_data}
        kubernetes_handler.update_configmap(self.kube_config_path, "pai-node-config", pai_node_list, "kube-system")
        self.logger.info("Successfully update node configuration to kubernetes' configmap.")



    def check_node_healthz(self, address):
        try:
            r = requests.get("http://{0}:10248/healthz".format(address))
            if r.status_code == 200:
                return True
        except Exception as e:
            pass

        return False



    def remove(self, node_config, cluster_config):
        remove_worker = k8s_remove.remove(cluster_config, node_config, True)
        remove_worker.run()

        if node_config["k8s-role"] == "master":
            self.logger.info("master node is removed, sleep 60s for etcd cluster's updating")
            time.sleep(60)



    def install(self, node_config, cluster_config):
        add_worker = k8s_add.add(cluster_config, node_config, True)
        add_worker.run()

        if node_config["k8s-role"] == "master":
            self.logger.info("Master Node is added, sleep 60s to wait it ready.")
            time.sleep(60)



    def node_status_check(self, node_config, node_list):
        node_name = node_config["nodename"]
        if node_name not in node_list:
            return False

        for condition_instance in node_list[node_name]["condition"]:
            if condition_instance["type"] != "Ready":
                continue
            if condition_instance["status"] != "True":
                return False
            break

        if not self.check_node_healthz(node_config["hostip"]):
            return False

        return True



    """
    Check all machine in the k8s configuration. 
    With the url to check the k8s node is setup or not.
    
    URL: [ x.x.x.x:10248/healthz ]
    
    If ok, the node is setup. 
    Or paictl will first do a clean on the target node and then bootstrap corresponding service according to the role of the node.
    """
    def add_machine(self):

        node_list = self.node_list_from_k8s
        cluster_configuration = self.k8s_configuration

        for role in cluster_configuration["remote_deployment"]:
            listname = cluster_configuration["remote_deployment"][role]["listname"]
            if listname not in cluster_configuration:
                continue

            for node_key in cluster_configuration[listname]:
                node_config = cluster_configuration[listname][node_key]

                if not self.node_status_check(node_config, node_list):
                    self.logger.info("Begin to add new node into pai cluster.")
                    self.logger.info("Target node name: {0}".format(node_config["nodename"]))
                    self.logger.info("Target node address: {0}".format(node_config["hostip"]))

                    self.logger.info("[ 0/2 ] Cleaning the target node. ")
                    self.remove(node_config, cluster_configuration)
                    self.logger.info("[ 1/2 ] Cleaning Done!")

                    self.logger.info("[ 1/2 ] Install kubelet on the target node.")
                    self.install(node_config, cluster_configuration)
                    self.logger.info("[ 2/2 ] Install Done!")

                    self.logger.info("Node [{0}] is added into the cluster as a new kubernetes node.".format(node_config["nodename"]))



    """
    Check all machine in the node list from k8s.
    If the nodename not in the k8s configuration. 
    Paictl will clean the node.
    Or do nothing.
    """
    def remove_machine(self):
        for node in self.node_config_from_k8s:
            if node not in self.node_config_from_cluster_conf:

                node_config = self.node_config_from_k8s[node]

                self.logger.info("Begin to remove node from pai cluster.")
                self.logger.info("Target node name: {0}".format(node_config["nodename"]))
                self.logger.info("Target node address: {0}".format(node_config["hostip"]))

                self.logger.info(" [ 0/1 ] Cleanning the target node, remove all service.")
                self.remove(node_config, self.k8s_configuration)
                self.logger.info(" [ 1/1 ] Cleanning Done.")

                self.logger.info("Node [{0}] is removed from kubernetes.")



    def run(self):

        self.k8s_configuration = self.get_latest_configuration_from_pai()
        self.node_list_from_k8s = self.get_node_list_from_k8s_api()
        self.node_config_from_cluster_conf = self.get_node_config_from_cluster_configuration()
        self.node_config_from_k8s = self.get_node_config_from_k8s()
        if self.node_config_from_k8s == None:

            self.logger.warning("Suggestion: Due to the first time you execute this command.")
            self.logger.warning("Suggestion: Please roll back your configuration in the cluster first.")
            self.logger.warning("Suggestion: And then, with the old configuration which don't have any change on the machine list, execute the machine update command.")
            self.logger.warning("Suggestion: Then, update the latest configuration, re-execute the command")

            count_input = 0
            while True:
                user_input = raw_input("Do you want to continue the operation this time? (Y/N) ")
                if user_input == "N":
                    return
                elif user_input == "Y":
                    break
                else:
                    print(" Please type Y or N.")
                count_input = count_input + 1
                if count_input == 3:
                    self.logger.warning("3 Times.........  Sorry,  we will force stopping your operation.")
                    return

            self.node_config_from_k8s = self.node_config_from_cluster_conf

        self.add_machine()
        self.remove_machine()

        self.update_node_config()
        directory_handler.directory_delete(self.tmp_path)

