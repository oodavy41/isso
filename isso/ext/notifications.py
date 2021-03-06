# -*- encoding: utf-8 -*-

from __future__ import unicode_literals

import sys
import io
import os
import time
import json
import queue
import socket
import smtplib
import pdb

from email.utils import formatdate
from email.header import Header
from email.mime.text import MIMEText

from urllib.parse import unquote

import logging
logger = logging.getLogger("isso")

try:
    import uwsgi
except ImportError:
    uwsgi = None

from isso.compat import PY2K
from isso import local

if PY2K:
    from thread import start_new_thread
else:
    from _thread import start_new_thread


class SMTP(object):
    def __init__(self, isso):

        self.isso = isso
        self.conf = isso.conf.section("smtp")
        self.mailQ = queue.Queue()
        self.manageText = open(
            os.path.join(
                os.path.dirname(__file__), '..', 'templates', 'manage.html'),
            'r').read()

        self.notifyText = open(
            os.path.join(
                os.path.dirname(__file__), '..', 'templates', 'notify.html'),
            'r').read()
        # test SMTP connectivity
        try:
            with self:
                logger.info("connected to SMTP server")
        except (socket.error, smtplib.SMTPException):
            logger.exception("unable to connect to SMTP server")

        if uwsgi:

            def spooler(args):
                try:
                    self._sendmail(args[b"subject"].decode("utf-8"),
                                   args["body"].decode("utf-8"))
                except smtplib.SMTPConnectError:
                    return uwsgi.SPOOL_RETRY
                else:
                    return uwsgi.SPOOL_OK

            uwsgi.spooler = spooler

    def __enter__(self):

        klass = (smtplib.SMTP_SSL
                 if self.conf.get('security') == 'ssl' else smtplib.SMTP)
        self.client = klass(
            host=self.conf.get('host'),
            port=self.conf.getint('port'),
            timeout=self.conf.getint('timeout'))
        #self.client.set_debuglevel(1)
        #self.client.ehlo()

        if self.conf.get('security') == 'starttls':
            if sys.version_info >= (3, 4):
                import ssl
                self.client.starttls(context=ssl.create_default_context())
            else:
                self.client.starttls()

        username = self.conf.get('username')
        password = self.conf.get('password')
        if username and password:
            if PY2K:
                username = username.encode('ascii', errors='ignore')
                password = password.encode('ascii', errors='ignore')

            self.client.login(username, password)

        return self.client

    def __exit__(self, exc_type, exc_value, traceback):
        self.client.quit()

    def __iter__(self):
        yield "comments.new:after-save", self.notify

    def format(self, thread, comment, admin=False):
        class safesub(dict):
            def __missing__(self, key):
                return '{' + key + '}'
        # admin: url title comments author ipaddr delete
        # reply: url title comments author
        author = comment["author"] or "Anonymous"
        if comment["author"] and comment["website"]:
            author = "<a href=\"%s\">%s</a>" % (comment["website"],
                                                comment["author"])
        if comment["email"]:
            author += " <%s>" % comment["email"]

        comments = comment["text"]
        title = unquote(thread["uri"], encoding='utf-8', errors='replace')
        title=title.split('/')[-2]
        url = local("origin") + thread["uri"] + "#isso-%i" % comment["id"]
        ipaddr = comment["remote_addr"]
        delete = local(
            "host") + "/id/%i" % comment["id"] + "/delete/" + self.isso.sign(
                comment["id"])

        if (admin):
            return self.manageText.format_map(safesub(vars()))
        else:
            return self.notifyText.format_map(safesub(vars()))


    def notify(self, thread, comment):

        if "parent" in comment:
            comment_parent = self.isso.db.comments.get(comment["parent"])
            # Notify the author that a new comment is posted if requested
            if comment_parent and "email" in comment_parent and comment_parent["notification"]:
                body = self.format(thread, comment, admin=False)
                subject = "Re: New comment posted on %s" % thread["title"]
                self.sendmail(
                    subject, body, thread, comment, to=comment_parent["email"])
        body = self.format(thread, comment, admin=True)
        self.sendmail(thread["title"], body, thread, comment)

    def sendmail(self, subject, body, thread, comment, to=None):

        if uwsgi:
            uwsgi.spool({
                b"subject": subject.encode("utf-8"),
                b"body": body.encode("utf-8"),
                b"to": to
            })
        elif self.mailQ.empty():
            self.mailQ.put((subject, body, to))
            pdb.set_trace()    
            start_new_thread(self._retry,())
        else:
            self.mailQ.put((subject, body, to))

    def _sendmail(self, subject, body, to=None):

        from_addr = self.conf.get("from")
        to_addr = to or self.conf.get("to")

        msg = MIMEText(body, 'html', 'utf-8')
        msg['From'] = from_addr
        msg['To'] = to_addr
        msg['Date'] = formatdate(localtime=True)
        msg['Subject'] = Header(subject, 'utf-8')

        with self as con:
            con.sendmail(from_addr, to_addr, msg.as_string())

    def _retry(self):
        while (not self.mailQ.empty()):
            mail = self.mailQ.get()
            for x in range(5):
                try:
                    self._sendmail(mail[0], mail[1], mail[2])
                    time.sleep(1)
                except smtplib.SMTPConnectError:
                    time.sleep(60)
                else:
                    break


class Stdout(object):
    def __init__(self, conf):
        pass

    def __iter__(self):

        yield "comments.new:new-thread", self._new_thread
        yield "comments.new:finish", self._new_comment
        yield "comments.edit", self._edit_comment
        yield "comments.delete", self._delete_comment
        yield "comments.activate", self._activate_comment

    def _new_thread(self, thread):
        logger.info("new thread %(id)s: %(title)s" % thread)

    def _new_comment(self, thread, comment):
        logger.info("comment created: %s", json.dumps(comment))

    def _edit_comment(self, comment):
        logger.info('comment %i edited: %s', comment["id"],
                    json.dumps(comment))

    def _delete_comment(self, id):
        logger.info('comment %i deleted', id)

    def _activate_comment(self, id):
        logger.info("comment %s activated" % id)
