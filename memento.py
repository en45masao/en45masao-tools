#!/usr/bin/env python
# -*- coding: utf-8 -*-

import re
import time
import urllib2
import urlparse
import StringIO
import zipfile
import logging
import wsgiref.handlers
from datetime import datetime
from HTMLParser import HTMLParser
from google.appengine.api import urlfetch
from google.appengine.api import memcache
from google.appengine.api import taskqueue
from google.appengine.ext import webapp

class JournalParser(HTMLParser):
	def __init__(self, base_url):
		HTMLParser.__init__(self)
		self.journals = set()
		self.resources = set()
		self.title = ''
		self.date = datetime.now()
		self.tags = []
		self.body_range = (False, False)
		self._base_url = re.match(r'(http://.*/)', base_url).group(0)
		self._is_tag = False

	def handle_starttag(self, tag, attrs):
		def add_journal_url(portion_url):
			url, frag = urlparse.urldefrag(urlparse.urljoin(self._base_url, portion_url))
			if re.match(self._base_url + r'journal/\d+/\d+/\d+/\d+', url):
				self.journals.add(url)

		def add_resource_url(portion_url):
			url, frag = urlparse.urldefrag(urlparse.urljoin(self._base_url, portion_url))
			if re.match(r'http://.*smart\.fm/', url):
				self.resources.add(url)

		dic = dict(attrs)

		# Gets the range of the main body. (ad hoc...)
		if tag == 'div' and dic.get('class') == 'entry_body':
			self.body_range = (self.getpos()[0] + 1, False)
		if tag == 'div' and dic.get('class') == 'meta_div':
			self.body_range = (self.body_range[0], self.getpos()[0] - 1)

		# Gets the title of the entry.
		if tag == 'meta' and dic.get('property') == 'og:title' and dic.get('content'):
			self.title = dic['content']

		# Gets the posted time.
		if tag == 'span' and dic.get('class') == 'blog_date' and dic.get('data-time'):
			st = time.strptime(dic['data-time'], '%a %b %d %H:%M:%S %Z %Y')
			self.date = datetime(*st[:6])

		# Treats the enclosed text as a tag.
		if tag == 'a' and dic.get('href') and re.match(r'.*/journal/tagged/.*', dic['href']):
			self._is_tag = True

		# Gets journal URLs.
		if tag == 'a' and dic.get('href'):
			add_journal_url(dic['href'])
		if tag == 'meta' and dic.get('property') == 'og:url' and dic.get('content'):
			add_journal_url(dic['content'])

		# Gets resources' URLs.
		if (tag == 'img' or tag == 'script') and dic.get('src'):
			add_resource_url(dic['src'])
		if tag == 'link' and dic.get('href'):
			add_resource_url(dic['href'])

	def handle_endtag(self, tag):
		pass

	def handle_data(self, data):
		if self._is_tag:
			self.tags.append(data)
			self._is_tag = False

class MainPage(webapp.RequestHandler):
	def post(self):
		# Gets the parameters.
		user_id = self.request.get('user_id').encode('utf8')

		# Checks whether the user exists or not.
		if not user_id or urlfetch.fetch('http://smart.fm/users/' + user_id, method=urlfetch.HEAD, follow_redirects=False).status_code != 200:
			self.error(405)
			self.response.headers['Content-Type'] = 'text/html; charset="UTF-8"'
			self.response.out.write('User not found.')
			return

		timestamp = datetime.now().strftime('%Y%m%d%H%M%S')

		self.request.str_POST['timestamp'] = timestamp  # ad hoc...

		# Moves to the next phase.
		memcache.set(get_counter_key(self), 0)
		memcache.set(get_journal_num_key(self), 0)
		memcache.set(get_phase_key(self), 1)
		taskqueue.add(url='/smartfm/memento/tasks/search', params={'user_id': user_id, 'timestamp': timestamp, 'page': 1})

		self.redirect('/smartfm/memento/download?user_id=%s&timestamp=%s' % (user_id, timestamp))

class DownloadPage(webapp.RequestHandler):
	def get(self):
		# Gets the parameters.
		user_id = self.request.get('user_id').encode('utf8')
		timestamp = self.request.get('timestamp').encode('utf8')

		phase = get_phase(self)

		if phase == 0:
			self.error(405)
			self.response.headers['Content-Type'] = 'text/html; charset="UTF-8"'
			self.response.out.write('Invalid access.')
			return

		if phase < 4:
			counter = get_counter(self)

			self.response.headers['Content-Type'] = 'text/html; charset="UTF-8"'
			self.response.out.write('''
<!doctype html>
<html>
<head>
<title>Now Downloading...</title>
<meta http-equiv="Refresh" content="5; URL=%s">
<link type="text/css" rel="stylesheet" href="https://ajax.googleapis.com/ajax/libs/jqueryui/1.8.6/themes/cupertino/jquery-ui.css" media="screen">
<link type="text/css" rel="stylesheet" href="/common.css" media="screen">
</head>
<body>
<h1 class="ui-widget-header">Now Downloading...</h1>
''' % (self.request.url))

			self.response.out.write('<p>Phase: %d / 3 (%s)</p>' % (phase, ('Just started', 'Analyzing pages', 'Downloading journals', 'Downloading images', 'Done')[phase]))

			if phase >= 1:
				self.response.out.write('<p>Analyzing pages: %s</p>' % ('Done' if phase > 1 else '%d' % (counter)))
			if phase >= 2:
				self.response.out.write('<p>Downloading journals: %s</p>' % ('Done' if phase > 2 else '%d / %d' % (counter, memcache.get(get_journal_num_key(self)))))
			if phase >= 3:
				self.response.out.write('<p>Downloading images: %s</p>' % ('Done' if phase > 3 else '%d / %d' % (counter, memcache.get(get_resource_num_key(self)))))

			self.response.out.write('</body>\n</html>')
			return

		zipdata = StringIO.StringIO()
		zipobj = zipfile.ZipFile(zipdata, 'w', zipfile.ZIP_DEFLATED)

		# Gets all the journal resources and stores them into a zip file.
		for i in range(memcache.get(get_journal_num_key(self))):
			url = memcache.get(get_journals_key(self, i))
			content = fetch_content(url)

			filepath = re.sub(r'^http://', '/', url).encode("utf-8")
			logging.info(filepath)
			if not filepath in zipobj.namelist():
				zipobj.writestr(filepath, content)

		for i in range(memcache.get(get_resource_num_key(self))):
			url = memcache.get(get_resources_key(self, i))
			content = fetch_content(url)

			filepath = re.sub(r'^http://', '/', url).encode("utf-8")
			if not filepath in zipobj.namelist():
				zipobj.writestr(filepath, content)

		zipobj.close()

		# Returns a generated zip file as a response.
		self.response.headers['Content-Type'] = 'application/octet-stream'
		self.response.headers['Content-Disposition'] = 'attachment; filename="%s_%s.zip"' % (user_id, timestamp)
		self.response.out.write(zipdata.getvalue())

class JournalSearcher(webapp.RequestHandler):
	def post(self):
		# Gets the parameters.
		user_id = self.request.get('user_id')
		timestamp = self.request.get('timestamp')
		page = int(self.request.get('page'))

		memcache.set(get_counter_key(self), page)

		# Fetches a journal list page.
		journals_url = 'http://smart.fm/users/' + user_id + '/journal?page=' + str(page)
		content = fetch_content(journals_url)

		# Parses an HTML to get a list of journals' URLs.
		parser = JournalParser(journals_url)
		parser.feed(content)

		if parser.journals:
			# Stores a list of journals.
			for url in parser.journals:
				journal_num = memcache.get(get_journal_num_key(self))
				memcache.set(get_journals_key(self, journal_num), url)
				memcache.incr(get_journal_num_key(self))

			taskqueue.add(url='/smartfm/memento/tasks/search', params={'user_id': user_id, 'timestamp': timestamp, 'page': page + 1}, countdown=1)

		else:
			# Eliminates redundant items.
			set_urls = set()
			for i in range(memcache.get(get_journal_num_key(self))):
				set_urls.add(memcache.get(get_journals_key(self, i)))
			memcache.set(get_journal_num_key(self), len(set_urls))

			# Stores an optimized list of journals.
			list_urls = sorted(set_urls, cmp=lambda a, b: cmp(int(a), int(b)), key=lambda url: re.match(r'.*/journal/\d+/\d+/\d+/(\d+)', url).group(1), reverse=True)
			for i in range(len(list_urls)):
				memcache.set(get_journals_key(self, i), list_urls[i])

			logging.info('journals:\n\t' + '\n\t'.join(list_urls))

			# Moves to the next phase.
			memcache.set(get_resource_num_key(self), 0)
			memcache.incr(get_phase_key(self))
			taskqueue.add(url='/smartfm/memento/tasks/analyze', params={'user_id': user_id, 'timestamp': timestamp, 'journal_index': 0})

class JournalAnalyzer(webapp.RequestHandler):
	def post(self):
		# Gets the parameters.
		user_id = self.request.get('user_id')
		timestamp = self.request.get('timestamp')
		index = int(self.request.get('journal_index'))

		memcache.set(get_counter_key(self), index)

		if index < int(memcache.get(get_journal_num_key(self))):
			# Fetches an HTML file.
			journal_url = memcache.get(get_journals_key(self, index))
			content = fetch_content(journal_url)

			# Parses an HTML to get a list of resources' URLs.
			parser = JournalParser(journal_url)
			parser.feed(content)

			# Stores a list of resources.
			for url in parser.resources:
				url = re.sub(r'^http://smart.fm/assets/user/', 'http://assets.smart.fm/assets/users/', url)	# workaround for the bug in smart.fm
				resource_num = memcache.get(get_resource_num_key(self))
				memcache.set(get_resources_key(self, resource_num), url)
				memcache.incr(get_resource_num_key(self))

			taskqueue.add(url='/smartfm/memento/tasks/analyze', params={'user_id': user_id, 'timestamp': timestamp, 'journal_index': index + 1}, countdown=1)

		else:
			# Eliminates redundant items.
			set_urls = set()
			for i in range(memcache.get(get_resource_num_key(self))):
				set_urls.add(memcache.get(get_resources_key(self, i)))
			for url in sorted(set_urls):
				if not re.match(r'.*/' + user_id +  r'/.*', url):
					set_urls.remove(url)
			memcache.set(get_resource_num_key(self), len(set_urls))

			# Stores an optimized list of resources.
			list_urls = sorted(set_urls)
			for i in range(len(list_urls)):
				memcache.set(get_resources_key(self, i), list_urls[i])

			logging.info('resources:\n\t' + '\n\t'.join(list_urls))

			# Moves to the next phase.
			memcache.incr(get_phase_key(self))
			taskqueue.add(url='/smartfm/memento/tasks/fetch', params={'user_id': user_id, 'timestamp': timestamp, 'resource_index': 0})

class ResourceFetcher(webapp.RequestHandler):
	def post(self):
		# Gets the parameters.
		user_id = self.request.get('user_id')
		timestamp = self.request.get('timestamp')
		index = int(self.request.get('resource_index'))

		memcache.set(get_counter_key(self), index)

		if index < int(memcache.get(get_resource_num_key(self))):
			# Fetches a resource file.
			fetch_content(memcache.get(get_resources_key(self, index)))
			taskqueue.add(url='/smartfm/memento/tasks/fetch', params={'user_id': user_id, 'timestamp': timestamp, 'resource_index': index + 1}, countdown=1)

		else:
			# Moves to the next phase.
			memcache.incr(get_phase_key(self))

def fetch_content(url, headers={}):
	logging.info(url)
	content = memcache.get(url)
	if content is None:
		content = urlfetch.fetch(url).content
		memcache.add(url, content, time=3600)
	return content

def get_phase(request_handler):
	phase = memcache.get(get_phase_key(request_handler))
	return int(phase) if phase else 0

def get_counter(request_handler):
	counter = memcache.get(get_counter_key(request_handler))
	return int(counter) if counter else 0

def get_phase_key(request_handler):
	user_id = request_handler.request.get('user_id')
	timestamp = request_handler.request.get('timestamp')
	return '%s_%s_phase' % (user_id, timestamp)

def get_counter_key(request_handler):
	user_id = request_handler.request.get('user_id')
	timestamp = request_handler.request.get('timestamp')
	return '%s_%s_counter' % (user_id, timestamp)

def get_journal_num_key(request_handler):
	user_id = request_handler.request.get('user_id')
	timestamp = request_handler.request.get('timestamp')
	return '%s_%s_journal_num' % (user_id, timestamp)

def get_resource_num_key(request_handler):
	user_id = request_handler.request.get('user_id')
	timestamp = request_handler.request.get('timestamp')
	return '%s_%s_resource_num' % (user_id, timestamp)

def get_journals_key(request_handler, index):
	user_id = request_handler.request.get('user_id')
	timestamp = request_handler.request.get('timestamp')
	return '%s_%s_journal_%05d' % (user_id, timestamp, index)

def get_resources_key(request_handler, index):
	user_id = request_handler.request.get('user_id')
	timestamp = request_handler.request.get('timestamp')
	return '%s_%s_resources_%05d' % (user_id, timestamp, index)

def main():
	application = webapp.WSGIApplication([
		('/smartfm/memento', MainPage),
		('/smartfm/memento/download', DownloadPage),
		('/smartfm/memento/tasks/search', JournalSearcher),
		('/smartfm/memento/tasks/analyze', JournalAnalyzer),
		('/smartfm/memento/tasks/fetch', ResourceFetcher),
		], debug=True)
	wsgiref.handlers.CGIHandler().run(application)

if __name__ == "__main__":
	main()
