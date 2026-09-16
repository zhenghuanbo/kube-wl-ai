from flask_appbuilder import Model
from sqlalchemy import Column, Integer, String, ForeignKey, Date
from sqlalchemy.orm import relationship


class ResourceSlot(Model):
    """
    Model to represent the resource slot.
    """
    __tablename__ = 'resource_slot'
    id = Column(Integer, primary_key=True)
    name = Column(String(150), unique=True, nullable=False)
    description = Column(String(564), default='Resource slot description')
    created_on = Column(Date, nullable=False)

    cpu_quota = Column(Integer, nullable=False, default=0)
    memory_quota = Column(Integer, nullable=False, default=0)
    GPU_type = Column(Integer, nullable=False, default=0)
    gpu_quota = Column(Integer, nullable=False, default=0)

    #resource_group = relationship("ResourceGroup", back_populates="resource_slots")

    def __repr__(self):
        return f"ResourceSlot(name={self.name})"