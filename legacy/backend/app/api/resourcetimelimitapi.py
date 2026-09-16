from flask_appbuilder.models.sqla.interface import SQLAInterface
from flask_appbuilder.api import ModelRestApi
from ..model.model_resource_timelimit import ResourceTimeLimit

class ResourceTimeLimitApi(ModelRestApi):
    resource_name = 'resourcetimelimit'
    datamodel = SQLAInterface(ResourceTimeLimit)
    allow_browser_login = True

