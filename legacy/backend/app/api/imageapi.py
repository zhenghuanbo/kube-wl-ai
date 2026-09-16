from flask_appbuilder.models.sqla.interface import SQLAInterface
from flask_appbuilder.api import ModelRestApi
from ..model.model_image import Image

class ImageApi(ModelRestApi):
    resource_name = 'image'
    datamodel = SQLAInterface(Image)
    allow_browser_login = True

