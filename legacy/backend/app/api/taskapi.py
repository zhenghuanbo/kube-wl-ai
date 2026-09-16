from flask_appbuilder.models.sqla.interface import SQLAInterface
from flask_appbuilder.api import ModelRestApi
from ..model.model_business_task import Task

class TaskApi(ModelRestApi):
    resource_name = 'task'
    datamodel = SQLAInterface(Task)
    allow_browser_login = True

