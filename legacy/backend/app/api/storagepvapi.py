from flask_appbuilder.models.sqla.interface import SQLAInterface
from flask_appbuilder.api import ModelRestApi
from ..model.model_storage_pv import Storage

class StorageApi(ModelRestApi):
    resource_name = 'storage'
    datamodel = SQLAInterface(Storage)
    allow_browser_login = True
