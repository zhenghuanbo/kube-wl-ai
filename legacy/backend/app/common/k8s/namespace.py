from .k8sclient import K8sClient
from kubernetes import client

class NamespaceK8sClient(K8sClient):
    def list_namespaces(self):
        return self.core_v1.list_namespace().items

    def create_namespace(self, name,labels=None):
        body = client.V1Namespace(metadata=client.V1ObjectMeta(name=name,labels=labels))
        return self.core_v1.create_namespace(body=body)

    def delete_namespace(self, name):
        return self.core_v1.delete_namespace(name=name)
