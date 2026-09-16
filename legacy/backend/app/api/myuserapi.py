from flask_appbuilder.models.sqla.interface import SQLAInterface
from flask_appbuilder.api import ModelRestApi
from flask_appbuilder.security.sqla.models import User

class MyUserApi(ModelRestApi):
    resource_name = "user"
    datamodel = SQLAInterface(User)

    def pre_add(self, item):
        if len(item.groups) > 1:
            raise Exception("用户只能属于一个组")

    def pre_update(self, item):
        if len(item.groups) > 1:
            raise Exception("用户只能属于一个组")