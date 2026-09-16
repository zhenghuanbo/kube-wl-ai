from flask_appbuilder.models.sqla.interface import SQLAInterface
from flask_appbuilder.api import ModelRestApi
from ..model.model_business_application import Application

class ApplicationApi(ModelRestApi):
    resource_name = 'application'
    datamodel = SQLAInterface(Application)
    allow_browser_login = True

