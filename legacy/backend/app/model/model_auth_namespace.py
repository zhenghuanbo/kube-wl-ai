from flask_appbuilder import Model
from sqlalchemy import Column, Integer, String, ForeignKey, Date
from sqlalchemy.orm import relationship
from flask_appbuilder.security.sqla.models import User, Group


class AuthNamespace(Model):
    """
    Model to represent the authentication namespace.
    """
    __tablename__ = 'auth_namespace'
    id = Column(Integer, primary_key=True)
    workspace_name = Column(String(150), unique=True, nullable=True)
    k8s_namespace = Column(String(150), unique=True, nullable=True)
    description = Column(String(564), default='Namespace for authentication')
    created_on = Column(Date, nullable=False)
    
    # Foreign key to link to User model
    created_by = Column(Integer, ForeignKey('ab_user.id'))
    user = relationship("User")
    
    # Foreign key to link to group model
    group_id = Column(Integer, ForeignKey('ab_group.id'))
    group = relationship("Group")

    
    #namespace_quotas = relationship("AuthNamespaceQuota", back_populates="auth_namespace")
    namespace_quotas = relationship("AuthNamespaceQuota", back_populates="auth_namespace")

    def __repr__(self):
        return f"AuthNamespace(name={self.name})"
