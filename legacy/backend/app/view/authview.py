from flask import g, jsonify
from flask_appbuilder import BaseView, expose
from flask import render_template


class AuthApi(BaseView):
    route_base = "/auth"

    @expose("/is_authenticated/")
    def userinfo(self):
        if g.user and g.user.is_authenticated:
            return jsonify(
                username=g.user.username,
                roles=[r.name for r in g.user.roles]
            )
        return jsonify(error="Unauthorized"), 401
    
    @expose("/jump/")
    def jump(self):
        return render_template('jump.html')


