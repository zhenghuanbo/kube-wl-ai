from flask_appbuilder.security.sqla.models import Group
from sqlalchemy import Column, Integer


class MyGroup(Group):
    company_type = Column(Integer, default=1, comment='公司类别: 1-内部, 2-关联, 3-外部')
