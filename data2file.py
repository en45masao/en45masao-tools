#!/usr/bin/env python
# -*- coding: utf-8 -*-

import sys
import re
import base64
import urllib
import logging
import wsgiref.handlers
from google.appengine.ext import webapp
from google.appengine.api import images

class MainPage(webapp.RequestHandler):
	def get(self):
		self.error(403)

class DownloadHandler(webapp.RequestHandler):
	def post(self):
		m = re.match('data:([^ ;]+)[;]?((?:charset=)?[^ ;]*)[;]?((?:base64)?[^ ,]*),([^ ]+)', self.request.get('body').encode('utf8', 'replace'))
		if m == None:
			return

		if m.group(1) != '':
			mimetype = m.group(1);
		else:
			mimetype = 'text/plain'
		index = m.group(2).find('charset=')
		if index >= 0:
			charset = m.group(2)[index:];
		else:
			charset = 'US-ASCII'
		is_base64 = (m.group(2) == 'base64') | (m.group(3) == 'base64')
		if is_base64:
			data = base64.standard_b64decode(m.group(4))
		else:
			data = urllib.unquote(m.group(4)).encode('raw_unicode_escape').decode(charset)

		self.response.headers['Content-Type'] = mimetype
		self.response.headers['Content-Disposition'] = 'attachment; filename="' + urllib.quote(self.request.get('filename', default_value = 'data.bin').encode('utf-8')) + '"'
		self.response.out.write(data)

def main():
	application = webapp.WSGIApplication([
		('/data2file/', MainPage),
		('/data2file/download', DownloadHandler),
		], debug = True)
	wsgiref.handlers.CGIHandler().run(application)

if __name__ == "__main__":
	main()
