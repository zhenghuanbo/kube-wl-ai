from flask_appbuilder import Model
from sqlalchemy import Column, Integer, String, ForeignKey, Date
from sqlalchemy.orm import relationship

class Image(Model):
    """
    Model to represent the image.
    """
    __tablename__ = 'image'
    id = Column(Integer, primary_key=True)
    name = Column(String(150), unique=True, nullable=False)
    description = Column(String(564), default='Image description')
    created_on = Column(Date, nullable=False)


    def __repr__(self):
        return f"Image(name={self.name})"