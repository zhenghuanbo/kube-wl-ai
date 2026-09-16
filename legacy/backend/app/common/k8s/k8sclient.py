from kubernetes import client, config
from kubernetes.config.config_exception import ConfigException

class K8sClient:
    def __init__(self, kubeconfig_path="./config/dev.config"):
        try:
            config.load_incluster_config()
        except ConfigException:
            config.load_kube_config(config_file=r"D:\vscodeProject\A\kube-wl\backend\app\common\k8s\config\dev.yaml")
        self.core_v1 = client.CoreV1Api()
        self.apps_v1 = client.AppsV1Api()

    def list_namespaces(self):
        return [ns.metadata.name for ns in self.core_v1.list_namespace().items]

    def get_pod(self, namespace, pod_name):
        return self.core_v1.read_namespaced_pod(name=pod_name, namespace=namespace)

    # 可继续扩展其它常用操作


    # 可继续扩展其它常用操作
    
