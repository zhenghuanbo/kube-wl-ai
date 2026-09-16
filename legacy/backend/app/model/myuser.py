from flask_appbuilder.security.sqla.models import User
from sqlalchemy import Column, DateTime

class MyUser(User):
    expire_time = Column(DateTime, comment='账号到期时间')