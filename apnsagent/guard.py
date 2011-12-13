#!/usr/bin/env python
#encoding=utf-8

import time
import os
import sys

if os.getcwd() not in sys.path:
    sys.path.append(os.getcwd())

import threading
from optparse import OptionParser
from ConfigParser import ConfigParser


from constants import *
from notification import *
from logger import log,create_log
from web_daemon import *

import simplejson

from redis import *


class PushGuard(object):
    """推送服务的主程序，主要职责:
    - 从指定目录读取一批app的配置文件(证书和Key)，并为之创建相应的推送和
    Feedback线程。
    - 定时轮询目录，在运行时对推送线程进行增删改管理
    """

    def __init__(self, app_dir, server_info):
        """初始化推送主进程，需要提供APP_DIR和SERVER_INFO参数
        app_dir: 存放应用信息的目录
        server_info: 用于连接redis的信息
        """
        assert app_dir , '"app_dir" argument is reqiured!'
        self.app_dir = app_dir
        self.server_info = server_info

        
        self.rds = redis.Redis(**self.server_info)

        self.threads = {}
        self.notifiers = {}

    def run(self):
        """读取一个目录，遍历下面的app文件夹，每个app启动一到两条线程对此提供服
        务,一条用来发推送，一条用来收feedback
        """
        apps = [app for app in os.listdir(self.app_dir)
                if os.path.isdir(os.path.join(self.app_dir, app))]

        for app in apps:
            # 文件夹下面会有production,develop两个子目录，分别放不同的Key及Cert
            # 文件
            log.debug('getting ready for app : %s' % app)
            if os.path.exists(os.path.join(self.app_dir, app, DEVELOP_DIR)):
                self.make_worker_threads(app, develop=True)
            if os.path.exists(os.path.join(self.app_dir, app, PRODUCTION_DIR)):
                self.make_worker_threads(app)

                
        #TODO 启动一条监控的线程,允许动态增加推送应用及移除推送应用。使用
        #inotify或队列来实现监听变动。inotify或许不错哦。

        start_web_daemon(self)

        self.watch_app()
        
        log.debug('just wait here,there are %d threads ' % len(self.threads))

        while True:
            time.sleep(10)


    def make_worker_threads(self, app, develop=False):
        app_key = app
        path = os.path.join(self.app_dir, app, DEVELOP_DIR) \
               if develop else \
               os.path.join(self.app_dir, app, PRODUCTION_DIR)
        cer_file = os.path.join(path, CER_FILE)
        key_file = os.path.join(path, KEY_FILE)

        if not (os.path.exists(cer_file) and os.path.exists(key_file)):
            return 
        
        kwargs = {
            'develop': develop,
            'app_key': app_key,
            'cer_file': cer_file,
            'key_file': key_file,
            'server_info': self.server_info
        }

        push_job = threading.Thread(target=self.push, kwargs=kwargs)
        feedback_job = threading.Thread(target=self.feedback, kwargs=kwargs)

        push_job.setDaemon(True)
        feedback_job.setDaemon(True)
        
        push_job.start()
        feedback_job.start()

        self.threads[app_key + ":push"] = push_job
        self.threads[app_key + ":feeedback"] = feedback_job

    def stop_worker_thread(self, app):
        app_key = app

        self.notifiers[app_key + ":push"].alive = False
        self.notifiers[app_key + ":feeedback"].alive = False

        del self.threads[app_key + ":push"]
        del self.threads[app_key + ":feeedback"]

    def change_worker_thread(self, develop=False):
        self.stop_worker_thread(app)
        self.make_worker_threads(app,develop)


    def watch_app(self):
        self.watcher = threading.Thread(target=self.app_watcher)
        self.watcher.setDaemon(True)
        self.watcher.start()


    def app_watcher(self):
        ps = self.rds.pubsub()
        ps.subscribe("app_watcher")
        channel = ps.listen()
        for message in channel:
            m = simplejson.loads(message["data"])
            if(m["op"]=="stop"):
                self.stop_worker_thread(m["app_key"])

    def push(self, develop, app_key, cer_file, key_file, server_info):
        notifier = Notifier('push', develop, app_key,
                            cer_file, key_file, server_info)
        self.notifiers[app_key + ":push"] = notifier   # XXX
        notifier.run()



    def feedback(self, develop, app_key, cer_file, key_file, server_info):
        notifier = Notifier('feedback', develop, app_key,
                            cer_file, key_file, server_info)
        self.notifiers[app_key + ":feeedback"] = notifier   # XXX
        notifier.run()


def execute():
    parser = OptionParser(usage="%prog config [options]")
    parser.add_option("-f", "--folder", dest="app_dir",
                      help="folder where the certs and keys to stay")
    parser.add_option("-s", "--host", dest="host", default="127.0.0.1",
                      help="Redis host name or address")
    parser.add_option("-p", "--port", dest="port", default=6379, type="int",
                      help="Redis port")
    parser.add_option("-d", "--db", dest="db", default=0, type="int",
                      help="Redis database")
    parser.add_option("-a", "--password", dest="password", default="",
                      help="Redis password")
    parser.add_option("-l", "--log", dest="log",
                      help="log file")
    (options, args) = parser.parse_args(sys.argv)
    if options.log:
        create_log(options.log)
    else:
        create_log()

    if len(args) > 1:
        config = ConfigParser()
        config.read(args[1])
        guard = PushGuard(app_dir=config.get('app', 'app_dir'),
                          server_info={'host': config.get('redis', 'host'),
                                       'port': int(config.get('redis', 'port')),
                                       'db': int(config.get('redis', 'db')),
                                       'password': config.get('redis','password')})
    else:
        guard = PushGuard(app_dir=options.app_dir,
                          server_info={'host': options.host,
                                       'port': options.port,
                                       'db': options.db,
                                       'password': options.password})
    guard.run()

if __name__ == '__main__':
    execute()
