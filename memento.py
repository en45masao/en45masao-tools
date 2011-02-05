#!/usr/bin/env python
# -*- coding: utf-8 -*-

import re
import urllib2
import urlparse
import StringIO
import zipfile
import logging
import wsgiref.handlers
from HTMLParser import HTMLParser
from google.appengine.ext import webapp

class MyParser(HTMLParser) :
	def __init__(self, base_url):
		HTMLParser.__init__(self)
		self.base_url = re.match(r'(http://.*/)', base_url).group(0)
		self.entries = set()

	def handle_starttag(self, tag, attrs):
		dic = dict(attrs)
		if tag == 'a':
			url, frag = urlparse.urldefrag(urlparse.urljoin(self.base_url, dic['href']))
			if re.match(self.base_url + 'journal/[0-9]+/[0-9]+/[0-9]+/[0-9]+', url):
				self.entries.add(url)

class MainPage(webapp.RequestHandler):
	def get(self):
		# Gets the parameters.
		user_id = self.request.get('user_id').encode('utf8', 'replace')
		if not user_id:
			self.error(405)
			return

		# Gets all the journal URLs.
		entries = set()
		for page in range(1, 1000):
			url = 'http://smart.fm/users/' + user_id + '/journal?page=' + str(page)
#			logging.info('URL: ' + url)
			r = urllib2.urlopen(url)

			parser = MyParser(url)
			parser.feed(r.read())
			if not parser.entries:
				break

			entries |= parser.entries

		if not entries:
			self.response.headers['Content-Type'] = 'text/plain'
			self.response.out.write('Not found.\n')
			return

#		logging.info('URLs:\n' + '\n'.join(entries))

		# Gets all the journal HTMLs and stores them into a zip file.
		zipdata = StringIO.StringIO()
		zipobj = zipfile.ZipFile(zipdata, 'w', zipfile.ZIP_DEFLATED)
		for url in sorted(entries):
			r = urllib2.urlopen(url)
			filepath = re.sub(r'^http://', '/', re.match(r'(http://.*/[0-9]+)', url).group(0)) + '.html'
#			logging.info('File path: ' + filepath)
			if not filepath in zipobj.namelist():
				zipobj.writestr(filepath, r.read())
		zipobj.close()

		# Returns a generated zip file as a response.
		self.response.headers['Content-Type'] = 'application/octet-stream'
		self.response.headers['Content-Disposition'] = 'attachment; filename="' + user_id + '.zip"'
		self.response.out.write(zipdata.getvalue())

def main():
	application = webapp.WSGIApplication([
		('/memento', MainPage),
		], debug = True)
	wsgiref.handlers.CGIHandler().run(application)

if __name__ == "__main__":
	main()
