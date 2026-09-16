from flask_appbuilder import Model
from sqlalchemy import Column, Integer, String, ForeignKey, Date
from sqlalchemy.orm import relationship
from flask_appbuilder.security.sqla.models import User, Group

class Storage(Model):
    """
    Model to represent the storage.
    """
    __tablename__ = 'storage_pv'
    id = Column(Integer, primary_key=True)
    name = Column(String(150), unique=True, nullable=False)
    description = Column(String(564), default='Storage description')
    created_on = Column(Date, nullable=False)

    storage_type = Column(String(50), nullable=False, default='default')  # Type of storage (e.g., SSD, HDD)
    capacity = Column(Integer, nullable=False, default=0)  # Storage capacity in GB

    pvc_name = Column(String(150), unique=True, nullable=True)  # Persistent Volume Claim name
    pvc_namespace = Column(String(150), nullable=True)  # Namespace for the PVC

    use_type = Column(String(50), nullable=False, default='default')  # Type of use (e.g., personal, shared)

    user_id = Column(Integer, ForeignKey('ab_user.id'), nullable=True)  # Foreign key to link to User model
    group_id = Column(Integer, ForeignKey('ab_group.id'), nullable=True)  # Foreign key to link to Group model

    changed_on = Column(Date, nullable=False)  #扩容时间
    changend_from= Column(String(150), nullable=True)  # Storage capacity in GB
    user = relationship("User")

    def __repr__(self):
        return f"Storage(name={self.name}, type={self.storage_type}, capacity={self.capacity}GB)"