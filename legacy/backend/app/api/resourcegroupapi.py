from flask_appbuilder.models.sqla.interface import SQLAInterface
from flask_appbuilder.api import ModelRestApi
from ..model.model_resource_group import ResourceGroup

class ResourceGroupApi(ModelRestApi):
    resource_name = 'resourcegroup'
    datamodel = SQLAInterface(ResourceGroup)
    allow_browser_login = True

