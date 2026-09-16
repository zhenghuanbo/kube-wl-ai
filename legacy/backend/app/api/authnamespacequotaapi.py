from flask_appbuilder.models.sqla.interface import SQLAInterface
from flask_appbuilder.api import ModelRestApi
from ..model.model_auth_namespace_quota import AuthNamespaceQuota

class AuthNamespaceQuotaApi(ModelRestApi):
    resource_name = 'authnamespacequota'
    datamodel = SQLAInterface(AuthNamespaceQuota)
    allow_browser_login = True
