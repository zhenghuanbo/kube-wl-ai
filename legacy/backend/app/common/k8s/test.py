from kubernetes import client, config
from kubernetes.config.config_exception import ConfigException

import os
print(os.path.abspath(r"D:\vscodeProject\A\kube-wl\backend\app\common\k8s\config\dev.yaml"))

config.load_kube_config(config_file=r"D:\vscodeProject\A\kube-wl\backend\app\common\k8s\config\dev.yaml")
# 如果在k8s的本机上，或者配置文件是~/.kube/config，则可以直接加载配置
# config.load_kube_config()

# 创建API对象
api_instance = client.CoreV1Api()

# 使用API对象执行操作，例如列出所有命名空间
namespace_list = api_instance.list_namespace()
for namespace in namespace_list.items:
    print(namespace.metadata.name)
