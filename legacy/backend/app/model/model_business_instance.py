from flask_appbuilder import Model
from sqlalchemy import Column, Integer, String, ForeignKey, Date
from sqlalchemy.orm import relationship
from .model_auth_namespace import AuthNamespace

class Instance(Model):
    """
    Model to represent the business instance.
    """
    __tablename__ = 'business_instance'
    id = Column(Integer, primary_key=True)
    name = Column(String(150), unique=True, nullable=False)
    description = Column(String(564), default='Business instance description')
    created_on = Column(Date, nullable=False)
    changed_on = Column(Date)
    
    # Foreign key to link to ResourceNodeGroup model
    k8s_namespace = Column(String(150), ForeignKey('auth_namespace.k8s_namespace'), nullable=False)
    auth_namespace = relationship("AuthNamespace")

    images = Column(String(200))
    ide_type = Column(String(200))
    working_dir = Column(String(200))
    volume_mount = Column(String(400))
    node_selector = Column(String(200))
    env = Column(String(400))

    #resource_solt_id = Column(Integer, ForeignKey('resource_slot.id'), nullable=False)
    visibility_scope = Column(String(128), comment='可见范围')
    status = Column(String(64), comment='状态')
    shutdown_policy = Column(String(128), comment='关机策略')
    priority = Column(Integer, comment='优先级')
    queue = Column(String(128), comment='排队')
    resource_type = Column(String(128), comment='资源类型')
    system_disk = Column(String(128), comment='系统盘')

    service_duration = Column(Integer, comment='服务时长')
    auto_end_time = Column(Date, comment='自动结束时间')

    ssh_info = Column(String(256), comment='SSH信息')

    image_save_record = Column(String(256), comment='镜像保存记录')

    def __repr__(self):
        return f"Instance(name={self.name})"