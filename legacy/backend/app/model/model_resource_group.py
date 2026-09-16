from flask_appbuilder import Model
from sqlalchemy import Column, Integer, String, ForeignKey, Date
from sqlalchemy.orm import relationship
from sqlalchemy import Table



resource_group_slot = Table(
    'resource_group_slot',
    Model.metadata,
    Column('resource_group_id', Integer, ForeignKey('resource_group.id')),
    Column('resource_slot_id', Integer, ForeignKey('resource_slot.id'))
)

resource_group_timelimit = Table(
    'resource_group_timelimit',
    Model.metadata,
    Column('resource_group_id', Integer, ForeignKey('resource_group.id')),
    Column('resource_timelimit_id', Integer, ForeignKey('resource_timelimit.id'))
)   

class ResourceGroup(Model):
    """
    Model to represent the resource node group.
    """
    __tablename__ = 'resource_group'
    id = Column(Integer, primary_key=True)
    name = Column(String(150), unique=True, nullable=False)
    description = Column(String(564), default='Resource node group description')
    created_on = Column(Date, nullable=False)
    range =Column(Integer, nullable=False, default=0)  # 资源组可见范围，0代表public，1代表私有

    group_k8s_label = Column(String(150), nullable=True)
    business_type = Column(String(150), nullable=True, default='default')

    #一对多
    resource_nodes = relationship("ResourceNode", back_populates="resource_group")

    #多对多
    resource_slots = relationship("ResourceSlot", 
                                  secondary=resource_group_slot, backref="resource_groups")

    resource_timelimits = relationship("ResourceTimeLimit", 
                                       secondary=resource_group_timelimit, 
                                       backref="resource_groups")


    def __repr__(self):
        return f"ResourceGroup(name={self.name})"