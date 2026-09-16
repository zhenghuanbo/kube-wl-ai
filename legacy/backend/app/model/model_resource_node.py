from flask_appbuilder import Model
from sqlalchemy import Column, Integer, String, ForeignKey, Date
from sqlalchemy.orm import relationship
from .model_resource_group import ResourceGroup

class ResourceNode(Model):
    """
    Model to represent the resource node.
    """
    __tablename__ = 'resource_node'
    id = Column(Integer, primary_key=True)
    hostname = Column(String(150), unique=True, nullable=False)
    description = Column(String(564), default='Resource node description')

    created_on = Column(Date, nullable=False)
    end_on = Column(Date, nullable=True)
    status = Column(String(50), nullable=False, default='active')

    cpu_cupacity = Column(Integer, nullable=False, default=0)
    memory_capacity = Column(Integer, nullable=False, default=0)
    gpu_capacity = Column(Integer, nullable=False, default=0)

    cpu_allocated = Column(Integer, nullable=False, default=0)
    memory_allocated = Column(Integer, nullable=False, default=0)
    gpu_allocated = Column(Integer, nullable=False, default=0)


    pod_count = Column(Integer, nullable=False, default=0)
    
    resource_group_id = Column(Integer, ForeignKey('resource_group.id'))
    resource_group = relationship("ResourceGroup", back_populates="resource_nodes")


    def __repr__(self):
        return f"ResourceNode(name={self.hostname})"