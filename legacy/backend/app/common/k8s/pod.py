from .k8sclient import K8sClient

class PodK8sClient(K8sClient):
    def list_pods(self, namespace):
        return self.core_v1.list_namespaced_pod(namespace=namespace).items

    def create_pod(self, namespace, pod_manifest):
        return self.core_v1.create_namespaced_pod(namespace=namespace, body=pod_manifest)

    def delete_pod(self, namespace, pod_name):
        return self.core_v1.delete_namespaced_pod(name=pod_name, namespace=namespace)

    def get_pod(self, namespace, pod_name):
        return self.core_v1.read_namespaced_pod(name=pod_name, namespace=namespace)
