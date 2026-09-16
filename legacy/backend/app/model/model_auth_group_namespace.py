from flask_appbuilder import Model
from sqlalchemy import Column, Integer, String, ForeignKey, Date

class AuthGroupNamespace(Model):
    """
    Model to represent the association between authentication groups and namespaces.
    """
    __tablename__ = 'auth_group_namespace'
    id = Column(Integer, primary_key=True)
    
    group_id = Column(Integer, ForeignKey('ab_group.id'))
    namespace_id = Column(Integer, ForeignKey('auth_namespace.id'))
    
    def __repr__(self):
        return f"AuthGroupNamespace(group_id={self.group_id}, namespace_id={self.namespace_id})"