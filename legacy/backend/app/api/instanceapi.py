from flask_appbuilder.models.sqla.interface import SQLAInterface
from flask_appbuilder.api import ModelRestApi
from ..model.model_business_instance import Instance

class InstanceApi(ModelRestApi):
    resource_name = 'instance'
    datamodel = SQLAInterface(Instance)
    allow_browser_login = True

