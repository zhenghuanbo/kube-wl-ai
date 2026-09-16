from flask_appbuilder import Model
from sqlalchemy import Column, Integer, String, ForeignKey, Date
from sqlalchemy.orm import relationship


class AuthNamespaceQuota(Model):
    """
    Model to represent the authentication namespace quota.
    """
    __tablename__ = 'auth_namespace_quota'
    id = Column(Integer, primary_key=True)
    name = Column(String(150), unique=True, nullable=True)

    cpu_quota = Column(Integer, nullable=False, default=0)
    memory_quota = Column(Integer, nullable=False, default=0)
    storage_quota = Column(Integer, nullable=False, default=0)
    image_quota = Column(Integer, nullable=False, default=0)
    gpu_quota = Column(Integer, nullable=False, default=0)
    
    description = Column(String(564), default='Namespace for authentication quota')
    created_on = Column(Date, nullable=False)
    changed_on = Column(Date, nullable=False)
    
    # Foreign key to link to AuthNamespace model
    auth_namespace_id = Column(Integer, ForeignKey('auth_namespace.id'))
    auth_namespace = relationship("AuthNamespace", back_populates="namespace_quotas")

    def __repr__(self):
        return f"AuthNamespaceQuota(name={self.name})"