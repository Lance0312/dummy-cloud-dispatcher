import requests

from datetime import datetime
from novaclient.client import Client
from novaclient import exceptions
from flask import Flask, render_template, request
from wtforms import Form, validators
from wtforms import TextField, PasswordField, TextAreaField
from flask.ext.sqlalchemy import SQLAlchemy
from sqlalchemy.orm import validates

app = Flask(__name__)
app.config.from_envvar('DCD_SETTINGS')
db = SQLAlchemy(app)


class DeployForm(Form):
    username = TextField('Username', [validators.InputRequired()])
    password = PasswordField('Password', [validators.InputRequired()])
    project = TextField('Project Name', [validators.InputRequired()])
    endpoint = TextField('Endpoint', [validators.InputRequired()])
    memo = TextAreaField('Memo')


class Record(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    endpoint = db.Column(db.String(4096))
    username = db.Column(db.String(4096))
    msg = db.Column(db.Text())
    memo = db.Column(db.Text())
    ip = db.Column(db.String(100))
    timestamp = db.Column(db.DateTime())

    def __init__(self, endpoint, username, msg, memo):
        self.endpoint = endpoint
        self.username = username
        self.msg = msg
        self.memo = memo
        self.ip = request.remote_addr
        self.timestamp = datetime.now()

    def __repr__(self):
        return '<Record %r, %r>' % (self.ip, self.endpoint)

    @validates('endpoint')
    def validate_endpoint(self, key, endpoint):
        assert len(endpoint) <= 4096
        return endpoint

    @validates('username')
    def validate_username(self, key, username):
        assert len(username) <= 4096
        return username


@app.route("/", methods=['GET', 'POST'])
def deploy():
    form = DeployForm(request.form)
    status = ""
    msg = ""
    errmsg = ""
    if request.method == 'POST' and form.validate():
        try:
            nova = Client(2, form.username.data, form.password.data,
                          form.project.data, form.endpoint.data)
            image = nova.images.list()[0]
            flavor = nova.flavors.list()[0]
            nova.servers.create(
                'dcd-test', image, flavor, min_count=1, max_count=2)
            status = "success"
            msg = "Successfully deployed"
        except exceptions.ClientException as e:
            status = "error"
            msg = str(e)
            errmsg = str(e)
        except requests.exceptions.ConnectionError as e:
            status = "error"
            msg = "Fail to connect to %s" % form.endpoint.data
            errmsg = str(e)
        except IndexError as e:
            status = "error"
            msg = "No valid image or flavor"
            errmsg = str(e)
        except Exception as e:
            status = "error"
            msg = "Something goes wrong, you might want to contact the admin."
            errmsg = str(e)
        finally:
            try:
                record = Record(form.endpoint.data, form.username.data,
                                errmsg, form.memo.data)
                db.session.add(record)
                db.session.commit()
            except Exception as e:
                print str(e)
            return render_template("form.html", form=form,
                                   status=status, msg=msg)

    return render_template("form.html", form=form)

if __name__ == "__main__":
    app.run()
