from flask_appbuilder.security.sqla.manager import SecurityManager
from .model.myuser import MyUser
from .model.mygroup import MyGroup
from .model.myrole import MyRole

class MySecurityManager(SecurityManager):
    user_model = MyUser
    role_model = MyRole
    group_model = MyGroup