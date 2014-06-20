import requests
import uuid

from datetime import datetime
from novaclient.client import Client
from novaclient import exceptions
from flask import Flask, render_template, request
from wtforms import Form, validators
from wtforms import TextField, PasswordField, TextAreaField
from wtfrecaptcha.fields import RecaptchaField
from flask.ext.sqlalchemy import SQLAlchemy
from sqlalchemy.orm import validates
from celery import Celery, Task, chain
from celery.task import current
from flask_mail import Mail, Message
from werkzeug.routing import BaseConverter, ValidationError


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
    email_addr = TextField('Email Address (optional)', [validators.Optional(),
                                                        validators.Email()])
    captcha = RecaptchaField('Captcha',
                             public_key=app.config['RECAPTCHA_PUB_KEY'],
                             private_key=app.config['RECAPTCHA_PRIV_KEY'],
                             secure=True)


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

        if instance_status == "ACTIVE":
            status = "Instance Active"
        elif instance_status == "ERROR":
            status = "Instance Error"
        else:
            status = "ERROR"

        if instance_id:
            msg.body = """
Task ID: %s
Status: %s
Instance ID: %s
Memo:
%s
""" % (task_id, status, instance_id, memo)
        else:
            msg.body = """
Task ID: %s
Status: %s
Error Message: %s
Memo:
%s
""" % (task_id, status, errmsg, memo)

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
            if kwargs['email_addr']:
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
            if kwargs['email_addr']:
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
    form = DeployForm(request.form,
                      captcha={'ip_address': request.remote_addr})
    if request.method == 'POST' and form.validate():
        task = chain(deploy.s(version=2,
                              username=form.username.data,
                              password=form.password.data,
                              project=form.project.data,
                              endpoint=form.endpoint.data,
                              memo=form.memo.data,
                              email_addr=form.email_addr.data,
                              client_ip=request.remote_addr),
                     check_instance_status.s())()
        return render_template("form.html", form=form, result=True,
                               task_id=task.parent.id)

    return render_template("form.html", form=form)


# XXX: UUIDConverter is available in werkzeug 0.10
class UUIDConverter(BaseConverter):
    def __init__(self, url_map):
        super(UUIDConverter, self).__init__(url_map)

    def to_python(self, value):
        try:
            uuid.UUID(value)
            return value
        except ValueError:
            raise ValidationError()

    def to_url(self, value):
        try:
            uuid.UUID(value)
            return value
        except ValueError:
            raise ValidationError()

app.url_map.converters['uuid'] = UUIDConverter


@app.route("/status/<uuid:task_id>", methods=['GET'])
def status(task_id):
    task = celery.AsyncResult(task_id)
    record = Record.query.filter_by(task_id=task_id).first()
    return render_template("status.html",
                           task_id=task.id, task_status=task.status,
                           instance_id=record.instance_id,
                           instance_status=record.instance_status,
                           endpoint=record.endpoint,
                           memo=record.memo,
                           errmsg=record.msg)

if __name__ == "__main__":
    app.run()
