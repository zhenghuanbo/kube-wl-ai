from flask_appbuilder.api import BaseApi, expose, safe
from flask import request
from ..common.k8s.pod import PodK8sClient
from ..common.k8s.namespace import NamespaceK8sClient

class K8sApi(BaseApi):
    resource_name = 'k8s'
    allow_browser_login = True

    @expose('/pods', methods=['GET'])
    @safe
    def list_pods(self):
        namespace = request.args.get('namespace', 'default')
        pods = PodK8sClient().list_pods(namespace)
        return self.response(200, [pod.metadata.name for pod in pods])

    @expose('/pods', methods=['POST'])
    @safe
    def create_pod(self):
        namespace = request.json.get('namespace', 'default')
        manifest = request.json.get('manifest')
        pod = PodK8sClient().create_pod(namespace, manifest)
        return self.response(200, {"name": pod.metadata.name})

    @expose('/pods/<pod_name>', methods=['DELETE'])
    @safe
    def delete_pod(self, pod_name):
        namespace = request.args.get('namespace', 'default')
        PodK8sClient().delete_pod(namespace, pod_name)
        return self.response(200, {"deleted": pod_name})

    @expose('/namespaces', methods=['GET'])
    @safe
    def list_namespaces(self):
        namespaces = NamespaceK8sClient().list_namespaces()
        return self.response(200, data=[ns.metadata.name for ns in namespaces])

    @expose('/namespaces', methods=['POST'])
    @safe
    def create_namespace(self):
        name = request.json.get('name')
        ns = NamespaceK8sClient().create_namespace(name)
        return self.response(200, {"name": ns.metadata.name})

    @expose('/namespaces/<name>', methods=['DELETE'])
    @safe
    def delete_namespace(self, name):
        NamespaceK8sClient().delete_namespace(name)
        return self.response(200, {"deleted": name})

