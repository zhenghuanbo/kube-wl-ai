from flask_appbuilder.models.sqla.interface import SQLAInterface
from flask_appbuilder.api import ModelRestApi
from ..model.model_resource_slot import ResourceSlot

class ResourceSlotApi(ModelRestApi):
    resource_name = 'resourceslot'
    datamodel = SQLAInterface(ResourceSlot)
    allow_browser_login = True

