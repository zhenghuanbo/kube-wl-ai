from flask_appbuilder.models.sqla.interface import SQLAInterface
from flask_appbuilder.api import ModelRestApi
from ..model.model_resource_node import ResourceNode

class ResourceNodeApi(ModelRestApi):
    resource_name = 'resourcenode'
    datamodel = SQLAInterface(ResourceNode)
    allow_browser_login = True
