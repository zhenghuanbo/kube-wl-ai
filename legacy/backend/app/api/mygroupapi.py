from flask_appbuilder.models.sqla.interface import SQLAInterface
from flask_appbuilder.api import ModelRestApi
from ..model.mygroup import MyGroup

class MyGroupApi(ModelRestApi):
    resource_name = 'group'
    datamodel = SQLAInterface(MyGroup)
    allow_browser_login = True