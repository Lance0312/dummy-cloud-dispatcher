import requests

from datetime import datetime
from novaclient.client import Client
from novaclient import exceptions
from flask import Flask, render_template, request
from wtforms import Form, validators
from wtforms import TextField, PasswordField, DateTimeField, TextAreaField

app = Flask(__name__)


class DeployForm(Form):
    username = TextField('Username', [validators.InputRequired()])
    password = PasswordField('Password', [validators.InputRequired()])
    project = TextField('Project Name', [validators.InputRequired()])
    endpoint = TextField('Endpoint', [validators.InputRequired()])
    memo = TextAreaField('Memo')
    timestamp = DateTimeField('Timestamp', default=datetime.now())


@app.route("/", methods=['GET', 'POST'])
def deploy():
    form = DeployForm(request.form)
    status = ""
    msg = ""
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
        except requests.exceptions.ConnectionError as e:
            status = "error"
            msg = "Fail to connect to %s" % form.endpoint.data
        except IndexError as e:
            status = "error"
            msg = "No valid image or flavor"
        except Exception as e:
            status = "error"
            msg = "Something goes wrong, you might want to contact the admin."
        finally:
            return render_template("form.html", form=form,
                                   status=status, msg=msg)

    return render_template("form.html", form=form)

if __name__ == "__main__":
    app.run()
