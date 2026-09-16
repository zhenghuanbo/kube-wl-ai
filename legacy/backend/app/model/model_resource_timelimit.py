from flask_appbuilder import Model
from sqlalchemy import Column, Integer, String, ForeignKey, Date
from sqlalchemy.orm import relationship


class ResourceTimeLimit(Model):
    """
    Model to represent the resource time limit.
    """
    __tablename__ = 'resource_timelimit'
    id = Column(Integer, primary_key=True)
    name = Column(String(150), unique=True, nullable=False)
    description = Column(String(564), default='Resource time limit description')
    created_on = Column(Date, nullable=False)

    time_limit = Column(Integer, nullable=False, default=0)  # Time limit in seconds
    # Foreign key to link to ResourceSlot model
    #resource_group = relationship("ResourceGroup", back_populates="resource_timelimits")

    def __repr__(self):
        return f"ResourceTimeLimit(name={self.name})"


