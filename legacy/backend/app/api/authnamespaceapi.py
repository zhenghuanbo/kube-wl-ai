from flask_appbuilder.models.sqla.interface import SQLAInterface
from flask_appbuilder.api import ModelRestApi
from ..model.model_auth_namespace import AuthNamespace
from flask_appbuilder.api import  expose, safe
from ..common.k8s.namespace import NamespaceK8sClient
from flask import g
from flask_appbuilder.security.sqla.models import Group 

class AuthNamespaceApi(ModelRestApi):
    resource_name = 'authnamespace'
    datamodel = SQLAInterface(AuthNamespace)
    allow_browser_login = True

    def pre_add(self, item):
        user = g.user
        #group_id = user.groups[0].id if user.groups else None
        group_id=item.group_id
        #判断课题组是否超过5个namespace，工作空间
        if group_id is not None:
            count = self.datamodel.session.query(AuthNamespace).filter(AuthNamespace.group_id == group_id).count()
            if count >= 5:
                return self.response(400, message="每个用户组最多只能创建5个namespace")
            # 查询 group name
            group = self.datamodel.session.query(Group).filter(Group.id == group_id).first()
            if not group:
                return self.response(400, message="无效的group_id")
            group_name = group.name
        else:
            return self.response(400, message="无法获取用户group_id")
        #todo: check item.name valid

        #更加workspace name 和 group name 拼接 namespace name
        workspace_name = getattr(item, 'workspace_name', None)
        k8s_namespace = f"wk--{group_name}-{workspace_name}"
        
        #更新设置参数
        item.k8s_namespace = k8s_namespace
        item.tmp_labels = f"group={group_name},workspace={workspace_name},created_by={user.username}"
        

    def post_add(self, item):
        # 限制每个用户组最多只能创建5个namespace
        
        # 拼接 namespace 名称
        k8s_namespace_name = getattr(item, 'k8s_namespace', None)
        if k8s_namespace_name:
            try:
                NamespaceK8sClient().create_namespace(k8s_namespace_name, getattr(item, 'tmp_labels', None))
            except Exception as e:
                return self.response(500, message=f"K8s创建namespace失败: {str(e)}")


    @expose('/get_namespaces_by_user/<int:user_id>/', methods=['post'])
    @safe
    def get_namespaces_by_user(self, user_id):
        """
        Custom method to get namespaces by user ID.
        """
        query = self.datamodel.session.query(AuthNamespace).filter(AuthNamespace.user_id == user_id)
        return [namespace.as_dict() for namespace in query.all()]
    
    @expose('/get_namespaces_by_group/<int:group_id>/', methods=['post'])
    @safe
    def get_namespaces_by_group(self, group_id):
        """
        Custom method to get namespaces by group ID.
        """
        query = self.datamodel.session.query(AuthNamespace).filter(AuthNamespace.group_id == group_id)
        return [namespace.as_dict() for namespace in query.all()]