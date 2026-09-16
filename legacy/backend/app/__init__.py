import logging

from flask import Flask
from flask_cors import CORS


from .api.applicationapi import *
from .api.instanceapi import *
from .api.taskapi import *
from .api.imageapi import *
from .api.resourcegroupapi import *
from .api.resourcenodeapi import *
from .api.resourceslotapi import *
from .api.resourcetimelimitapi import *
from .api.storagepvapi import *
from .api.authnamespaceapi import *
from .api.authnamespacequotaapi import *
from .api.authuserquotaapi import *
from .api.k8sapi import *
from .view.authview import *
from .api.mygroupapi import *
from .api.myuserapi import *

from .extensions import appbuilder, db

"""
 Logging configuration
"""

logging.basicConfig(format="%(asctime)s:%(levelname)s:%(name)s:%(message)s")
logging.getLogger().setLevel(logging.DEBUG)

def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object("config")
    with app.app_context():
        db.init_app(app)
        appbuilder.init_app(app, db.session)
        db.create_all()
        appbuilder.add_api(ApplicationApi)
        appbuilder.add_api(AuthNamespaceApi)
        appbuilder.add_api(AuthNamespaceQuotaApi)
        appbuilder.add_api(AuthUserQuotaApi)
        appbuilder.add_api(ImageApi)
        appbuilder.add_api(InstanceApi)
        appbuilder.add_api(K8sApi)
        appbuilder.add_api(ResourceGroupApi)
        appbuilder.add_api(ResourceNodeApi)
        appbuilder.add_api(ResourceSlotApi)
        appbuilder.add_api(ResourceTimeLimitApi)
        appbuilder.add_api(StorageApi)
        appbuilder.add_api(TaskApi)
        appbuilder.add_view_no_menu(AuthApi)
        appbuilder.add_api(MyGroupApi)
        appbuilder.add_api(MyUserApi)
        
    return app


# For backward compatibility
app = create_app()

# 允许跨域并携带 Cookie
CORS(app, supports_credentials=True)


"""
from sqlalchemy.engine import Engine
from sqlalchemy import event

#Only include this for SQLLite constraints
@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    # Will force sqllite contraint foreign keys
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()
"""







