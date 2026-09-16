from flask_appbuilder.models.sqla.interface import SQLAInterface
from flask_appbuilder.api import ModelRestApi
from ..model.model_auth_user_quota import authUserQuota

class AuthUserQuotaApi(ModelRestApi):
    resource_name = 'authuserquota'
    datamodel = SQLAInterface(authUserQuota)
    allow_browser_login = True

