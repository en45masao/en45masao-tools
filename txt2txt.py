#!/usr/bin/env python
# -*- coding: utf-8 -*-

import urllib
import logging
import wsgiref.handlers
from google.appengine.ext import webapp

class MainPage(webapp.RequestHandler):
	def get(self):
		self.error(403)

class DownloadHandler(webapp.RequestHandler):
	def post(self):
		self.response.headers['Content-Type'] = 'application/octet-stream'
		self.response.headers['Content-Disposition'] = 'attachment; filename="' + urllib.quote(self.request.get('filename', default_value = 'list.csv').encode('utf-8')) + '"'
		self.response.out.write(self.request.get('body').encode(self.request.get('destenc', default_value = 'cp932'), 'replace'))

def main():
	application = webapp.WSGIApplication([
		('/txt2txt/', MainPage),
		('/txt2txt/download', DownloadHandler),
		], debug = True)
	wsgiref.handlers.CGIHandler().run(application)

if __name__ == "__main__":
	main()
