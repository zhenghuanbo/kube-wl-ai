from flask_appbuilder import Model
from sqlalchemy import Column, Integer, String, ForeignKey, Date

class AuthGroupQuota(Model):
    """
    Model to represent the authentication group quota.
    """
    __tablename__ = 'auth_group_quota'
    id = Column(Integer, primary_key=True)
    name = Column(String(150), unique=True, nullable=True)

    cpu_quota = Column(Integer, nullable=False, default=0)
    memory_quota = Column(Integer, nullable=False, default=0)
    storage_quota = Column(Integer, nullable=False, default=0)
    image_quota = Column(Integer, nullable=False, default=0)
    gpu_quota = Column(Integer, nullable=False, default=0)

    description = Column(String(564), default='Group quota for authentication')
    created_on = Column(Date, nullable=False)
    changed_on = Column(Date, nullable=False)

    # Foreign key to link to Group model
    group_id = Column(Integer, ForeignKey('ab_group.id'))
    
    def __repr__(self):
        return f"AuthGroupQuota(name={self.name})"