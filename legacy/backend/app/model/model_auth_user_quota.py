from flask_appbuilder import Model
from sqlalchemy import Column, Integer, String, ForeignKey, Date
from sqlalchemy.orm import relationship
from flask_appbuilder.security.sqla.models import User, Group

class authUserQuota(Model):
    """
    Model to represent the authentication user quota.
    """
    __tablename__ = 'auth_user_quota'
    id = Column(Integer, primary_key=True)
    name = Column(String(150), unique=True, nullable=True)

    cpu_quota = Column(Integer, nullable=False, default=0)
    memory_quota = Column(Integer, nullable=False, default=0)
    storage_quota = Column(Integer, nullable=False, default=0)
    image_quota = Column(Integer, nullable=False, default=0)
    gpu_quota = Column(Integer, nullable=False, default=0)

    description = Column(String(564), default='User quota for authentication')
    created_on = Column(Date, nullable=False)
    changed_on = Column(Date, nullable=False)

    # Foreign key to link to User model
    user_id = Column(Integer, ForeignKey('ab_user.id'))
    user = relationship("User")

    def __repr__(self):
        return f"AuthUserQuota(name={self.name})"