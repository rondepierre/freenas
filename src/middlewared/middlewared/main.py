from collections import OrderedDict
from client.protocol import DDPProtocol
from daemon import DaemonContext
from daemon.pidfile import TimeoutPIDLockFile
from geventwebsocket import WebSocketServer, WebSocketApplication, Resource

import argparse
import imp
import inspect
import json
import logging
import logging.config
import os
import setproctitle
import subprocess
import sys
import traceback


class Application(WebSocketApplication):

    protocol_class = DDPProtocol

    def __init__(self, *args, **kwargs):
        super(Application, self).__init__(*args, **kwargs)
        self.authenticated = self._check_permission()

    def _send(self, data):
        self.ws.send(json.dumps(data))

    def send_error(self, message, error, stacktrace=None):
        self._send({
            'msg': 'result',
            'id': message['id'],
            'error': {
                'error': error,
                'stacktrace': stacktrace,
            },
        })

    def _check_permission(self):
        #if self.ws.environ['REMOTE_ADDR'] not in ('127.0.0.1', '::1'):
        #    return False

        remote = '{0}:{1}'.format(
            self.ws.environ['REMOTE_ADDR'], self.ws.environ['REMOTE_PORT']
        )

        proc = subprocess.Popen([
            '/usr/bin/sockstat', '-46c', '-p', self.ws.environ['REMOTE_PORT']
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for line in proc.communicate()[0].strip().splitlines()[1:]:
            cols = line.split()
            if cols[-1] == remote and cols[0] == 'root':
                return True
        return False

    def call_method(self, message):

        try:
            self._send({
                'id': message['id'],
                'msg': 'result',
                'result': self.middleware.call_method(
                    message['method'], message.get('params') or []
                ),
            })
        except Exception as e:
            self.send_error(message, str(e), ''.join(traceback.format_exception(sys.exc_type, sys.exc_value, sys.exc_traceback)))

    def on_open(self):
        pass

    def on_close(self, *args, **kwargs):
        pass

    def on_message(self, message):

        if not self.authenticated:
            self.send_error(message, 'Not authenticated')
            return

        if message['msg'] == 'method':
            self.call_method(message)


class Middleware(object):

    def __init__(self):
        self._services = {}
        self._plugins_load()

    def _plugins_load(self):
        from middlewared.service import Service
        plugins_dir = os.path.join(
            os.path.dirname(os.path.realpath(__file__)),
            'plugins',
        )
        if not os.path.exists(plugins_dir):
            return

        for f in os.listdir(plugins_dir):
            if not f.endswith('.py'):
                continue
            f = f[:-3]
            fp, pathname, description = imp.find_module(f, [plugins_dir])
            try:
                mod = imp.load_module(f, fp, pathname, description)
            finally:
                if fp:
                    fp.close()

            for attr in dir(mod):
                attr = getattr(mod, attr)
                if not inspect.isclass(attr):
                    continue
                if attr is Service:
                    continue
                if issubclass(attr, Service):
                    self.register_service(attr(self))

    def register_service(self, service):
        self._services[service._meta.namespace] = service

    def call_method(self, method, params):
        service, method = method.rsplit('.', 1)
        return getattr(self._services[service], method)(*params)

    def run(self):
        Application.middleware = self
        server = WebSocketServer(('', 8000), Resource(OrderedDict([
            ('/websocket', Application),
        ])))
        server.serve_forever()


def main():
    modpath = os.path.join(
        os.path.dirname(os.path.realpath(__file__)),
        '..',
    )
    if modpath not in sys.path:
        sys.path.insert(0, modpath)

    parser = argparse.ArgumentParser()
    parser.add_argument('restart', nargs='?')
    parser.add_argument('--foregound', '-f', action='store_true')
    args = parser.parse_args()

    pidpath = '/var/run/middlewared.pid'

    if args.restart:
        if os.path.exists(pidpath):
            with open(pidpath, 'r') as f:
                pid = int(f.read().strip())
            os.kill(pid, 15)

    try:
        if not args.foregound:
            daemonc = DaemonContext(
                pidfile=TimeoutPIDLockFile(pidpath),
                detach_process=True,
                stdout=sys.stdout,
                stdin=sys.stdin,
                stderr=sys.stderr,
            )
            daemonc.open()

        logging.config.dictConfig({
	        'version': 1,
            'handlers': {
                'console': {
                    'level': 'DEBUG',
                    'class': 'logging.StreamHandler',
                },
                'file': {
                    'level': 'DEBUG',
                    'class': 'logging.handlers.RotatingFileHandler',
                    'filename': '/var/log/middlewared.log',
                }
            },
            'loggers': {
                '': {
                    'handlers': ['console' if args.foregound else 'file'],
                    'level': 'DEBUG',
                    'propagate': True,
                },
            }
        })

        setproctitle.setproctitle('middlewared')

        Middleware().run()
    finally:
        if not args.foregound:
            daemonc.close()

if __name__ == '__main__':
    main()
