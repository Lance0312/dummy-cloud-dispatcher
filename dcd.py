import requests

from datetime import datetime
from novaclient.client import Client
from novaclient import exceptions
from flask import Flask, render_template, request
from wtforms import Form, validators
from wtforms import TextField, PasswordField, TextAreaField
from flask.ext.sqlalchemy import SQLAlchemy
from sqlalchemy.orm import validates
from celery import Celery, Task, chain
from celery.task import current
from flask_mail import Mail, Message


def make_celery(app):
    celery = Celery("dcd", broker=app.config['CELERY_BROKER_URL'],
                    backend=app.config['CELERY_RESULT_BACKEND'])
    celery.conf.update(app.config)
    TaskBase = celery.Task

    class ContextTask(TaskBase):
        abstract = True

        def __call__(self, *args, **kwargs):
            with app.app_context():
                return TaskBase.__call__(self, *args, **kwargs)
    celery.Task = ContextTask
    return celery


app = Flask(__name__)
app.config.from_envvar('DCD_SETTINGS')
db = SQLAlchemy(app)
celery = make_celery(app)
mail = Mail(app)


class DeployForm(Form):
    username = TextField('Username', [validators.InputRequired()])
    password = PasswordField('Password', [validators.InputRequired()])
    project = TextField('Project Name', [validators.InputRequired()])
    endpoint = TextField('Endpoint', [validators.InputRequired()])
    memo = TextAreaField('Memo')
    email_addr = TextField('Email Address', [validators.InputRequired(),
                                             validators.Email()])


class Record(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.String(100))
    endpoint = db.Column(db.String(4096))
    username = db.Column(db.String(4096))
    msg = db.Column(db.Text())
    memo = db.Column(db.Text())
    ip = db.Column(db.String(100))
    instance_id = db.Column(db.String(100))
    instance_status = db.Column(db.String(100))
    timestamp = db.Column(db.DateTime())

    def __init__(self, task_id, endpoint, username, memo, client_ip):
        self.task_id = task_id
        self.endpoint = endpoint
        self.username = username
        self.memo = memo
        self.ip = client_ip
        self.timestamp = datetime.now()

    def __repr__(self):
        return '<Record %r, %r, %r>' % (self.id, self.task_id, self.endpoint)

    @validates('endpoint')
    def validate_endpoint(self, key, endpoint):
        assert len(endpoint) <= 4096
        return endpoint

    @validates('username')
    def validate_username(self, key, username):
        assert len(username) <= 4096
        return username


def send_mail(recipient, task_id, instance_id=None, instance_status=None,
              errmsg=None, memo=None):
    with app.app_context():
        msg = Message("[Dummy Cloud Dispatcher] Instance Deploy Results",
                      recipients=[recipient, ])

        if instance_id:
            msg.body = """
Task ID: %s
Instance ID: %s
Instance Status: %s
Memo:
%s
""" % (task_id, instance_id, instance_status, memo)
        else:
            msg.body = """
Task ID: %s
Error Message: %s
Memo:
%s
""" % (task_id, errmsg, memo)

        mail.send(msg)


class DeployTask(Task):
    def on_success(self, retval, task_id, args, kwargs):
        record = Record.query.filter_by(task_id=task_id).first()
        record.instance_id = retval['instance_id']
        try:
            db.session.commit()
        except:
            db.session.rollback()
            raise

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        if isinstance(exc, exceptions.ClientException):
            msg = exc.message
        elif isinstance(exc, requests.exceptions.ConnectionError):
            msg = "Fail to connect to '%s'" % kwargs['endpoint']
        elif isinstance(exc, IndexError):
            msg = "No valid image or flavor"
        else:
            msg = "Something goes wrong, send '%s' to the administrator."\
                % task_id

        record = Record.query.filter_by(task_id=task_id).first()
        record.msg = msg
        try:
            db.session.commit()
            send_mail(kwargs['email_addr'],
                      task_id=record.task_id,
                      errmsg=record.msg,
                      memo=record.memo)
        except:
            db.session.rollback()
            raise


@celery.task()
def check_instance_status(kwargs):
    nova = Client(kwargs['version'], kwargs['username'], kwargs['password'],
                  kwargs['project'], kwargs['endpoint'])
    instance = nova.servers.get(kwargs['instance_id'])

    if instance.status == "BUILD":
        try:
            raise Exception("Still building")
        except Exception as e:
            interval = min(10 * (2 ** current.request.retries), 1800)
            raise current.retry(args=[kwargs], exc=e,
                                countdown=interval, max_retries=8)
    else:
        kwargs['instance_status'] = instance.status

        record = Record.query.\
            filter_by(instance_id=kwargs['instance_id']).first()
        record.instance_status = kwargs['instance_status']
        try:
            db.session.commit()
            send_mail(kwargs['email_addr'],
                      task_id=record.task_id,
                      instance_id=record.instance_id,
                      instance_status=record.instance_status,
                      memo=record.memo)
        except:
            db.session.rollback()
            raise

        return kwargs


@celery.task(base=DeployTask)
def deploy(**kwargs):
    record = Record(deploy.request.id, kwargs['endpoint'], kwargs['username'],
                    kwargs['memo'], kwargs['client_ip'])
    try:
        db.session.add(record)
        db.session.commit()
    except:
        db.session.rollback()
        raise

    nova = Client(kwargs['version'], kwargs['username'], kwargs['password'],
                  kwargs['project'], kwargs['endpoint'])
    image = nova.images.list()[0]
    flavor = nova.flavors.list()[0]
    instance = nova.servers.create(
        'dcd-test', image, flavor, min_count=1, max_count=1)
    kwargs['instance_id'] = instance.id

    return kwargs


@app.route("/", methods=['GET', 'POST'])
def dcd():
    form = DeployForm(request.form)
    if request.method == 'POST' and form.validate():
        chain(deploy.s(version=2,
                       username=form.username.data,
                       password=form.password.data,
                       project=form.project.data,
                       endpoint=form.endpoint.data,
                       memo=form.memo.data,
                       email_addr=form.email_addr.data,
                       client_ip=request.remote_addr),
              check_instance_status.s())()
        return render_template("form.html", form=form, result=True)

    return render_template("form.html", form=form)

if __name__ == "__main__":
    app.run()
