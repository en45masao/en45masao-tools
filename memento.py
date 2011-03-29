#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import re
import time
import urlparse
import htmlentitydefs
import StringIO
import zipfile
import logging
import wsgiref.handlers
from datetime import datetime, timedelta
from HTMLParser import HTMLParser, HTMLParseError
from google.appengine.api import urlfetch
from google.appengine.ext import db
from google.appengine.api import memcache
from google.appengine.api import taskqueue
from google.appengine.ext import webapp
from google.appengine.ext.webapp import template

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
			if re.match(self._base_url + r'journal/\d+/\d+/\d+/\d+[\w_-]*', url):
				self.journals.add(url)

		def add_resource_url(portion_url):
			url, frag = urlparse.urldefrag(urlparse.urljoin(self._base_url, portion_url))
			if re.match(r'http://.*smart\.fm/', url):
				self.resources.add(url)

		dic = dict(attrs)

		# Gets the range of the main body. (ad hoc...)
		if tag == 'div' and dic.get('class') == 'entry_body':
			self.body_range = (self.getpos()[0], False)
		if tag == 'div' and dic.get('class') == 'meta_div':
			self.body_range = (self.body_range[0], self.getpos()[0] - 2)

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

	def handle_data(self, data):
		if self._is_tag:
			self.tags.append(data)
			self._is_tag = False

class CommentParser(HTMLParser):
	def __init__(self, base_url):
		HTMLParser.__init__(self)
		self.comments = []
		self.resources = set()
		self._is_username = False
		self._is_comment = False
		self._is_in_p = False
		self._base_url = re.match(r'(http://.*/)', base_url).group(0)

	def handle_starttag(self, tag, attrs):
		def add_resource_url(portion_url):
			url, frag = urlparse.urldefrag(urlparse.urljoin(self._base_url, portion_url))
			if re.match(r'http://.*smart\.fm/', url):
				self.resources.add(url)

		def modify_url(tag, pair):
			ret = pair
			if (tag == 'a' and pair[0] == 'href') or (tag == 'img' and pair[0] == 'src'):
				if pair[1].find('http://') != 0:
					url = urlparse.urljoin(self._base_url, pair[1])
					url = modify_image_url(url)	# workaround for the bug in smart.fm
					ret = (pair[0], url)
			return ret

		dic = dict(attrs)

		if tag == 'div' and dic.get('class') == 'activity_content':
			self._is_comment = True

		if tag == 'h5':
			self._is_username = True

		if not self._is_in_p and tag == 'a' and re.search(r'/users/[^/]+$', dic.get('href')):
			username = re.search(r'/users/([^/]+$)', dic['href']).group(1)
			if self._is_username or (dic.get('class') == '' or dic.get('class') == 'staff'):
				self._append_info('username', username)
				self._is_username = False

		if tag == 'span' and 'data-time' in dic:
			st = time.strptime(dic['data-time'], '%a %b %d %H:%M:%S %Z %Y')
			self._append_info('date', datetime(*st[:6]))

		if self._is_comment and tag == 'p' and 'class' not in dic:
			self._is_in_p = True
		if self._is_comment and tag == 'blockquote' and dic.get('class') == 'deleted_comment':
			self._is_in_p = True

		if self._is_comment and tag == 'br':
			self.comments[-1]['comment'] = self.comments[-1].get('comment', '') + '\n'

		if self._is_comment and self._is_in_p and tag == 'img':
			self.comments[-1]['comment'] = self.comments[-1].get('comment', '') + '<%s%s>' % (tag, ''.join(map(lambda t: r' %s="%s"' % (modify_url(tag, t)), attrs)))

		if self._is_comment and tag == 'span' and 'id' in dic:
			if re.search(r'fulltext_', dic['id']):
				del(self.comments[-1]['comment'])

		# Gets resources' URLs.
		if (tag == 'img') and dic.get('src'):
			add_resource_url(dic['src'])

	def handle_endtag(self, tag):
		if (self._is_comment and tag == 'p') or (self._is_comment and self._is_in_p and tag == 'blockquote'):
			self.comments[-1]['comment'] = self.comments[-1].get('comment', '') + '\n'
			self._is_in_p = False

		if self._is_comment and tag == 'div':
			if len(self.comments) == 0:
				if 'username' in self.comments[-1] and 'date' in self.comments[-1] and 'comment' in self.comments[-1]:
					self._is_comment = False

	def handle_startendtag(self, tag, attrs):
		self.handle_starttag(tag, attrs)

	def handle_data(self, data):
		if self._is_comment and self._is_in_p:
			self.comments[-1]['comment'] = self.comments[-1].get('comment', '') + data

	def _append_info(self, key, value):
		if len(self.comments) == 0 or key in self.comments[-1]:
			self.comments.append({})
			self._is_username = False
			self._is_in_p = False

		self.comments[-1][key] = value

	def canonicalize_comments(self):
		if len(self.comments) > 0 and len(self.comments[-1]) < 3:	# '3' means comment, username, and date.
			del(self.comments[-1])

class JournalBodyFilter(HTMLParser):
	def __init__(self, base_url):
		HTMLParser.__init__(self)
		self.body = ''
		self._base_url = re.match(r'(http://.*/)', base_url).group(0)

	def handle_starttag(self, tag, attrs):
		def modify_url(tag, pair):
			ret = pair
			if (tag == 'a' and pair[0] == 'href') or (tag == 'img' and pair[0] == 'src') or (tag == 'script' and pair[0] == 'src'):
				if pair[1].find('http://') != 0:
					url = urlparse.urljoin(self._base_url, pair[1])
					url = modify_image_url(url)	# workaround for the bug in smart.fm
					ret = (pair[0], url)
			return ret

		self.body = self.body + '<%s%s>' % (tag, ''.join(map(lambda t: r' %s="%s"' % (modify_url(tag, t)), attrs)))

	def handle_endtag(self, tag):
		self.body += '</%s>' % (tag)

	def handle_startendtag(self, tag, attrs):
		self.handle_starttag(tag, attrs)

	def handle_data(self, data):
		self.body += data

	def handle_charref(self, ref):
		if re.match(r'x[0-9a-f]+', ref, re.IGNORECASE):
			self.body += unichr(int(ref[1:], 16))
		else:
			self.body += unichr(int(ref))

	def handle_entityref(self, name):
		if name in htmlentitydefs.name2codepoint.keys():
			self.body += unichr(htmlentitydefs.name2codepoint[name])

	def feed(self, data):
		lines = data.split('\n')
		if '-----' in lines or '--------' in lines:
			newdata = ''
			for line in lines:
				if line == '-----' or line == '--------':
					newdata += line + ' \n'
				else:
					newdata += line + '\n'
			HTMLParser.feed(self, newdata)
		else:
			HTMLParser.feed(self, data)

class MainPage(webapp.RequestHandler):
	def get(self):
		# Outputs a rendered HTML.
		template_values = {
			'trigger': True
		}
		self.response.out.write(template.render(os.path.join(os.path.dirname(__file__), 'memento.html'), template_values))

class TriggerPage(webapp.RequestHandler):
	def post(self):
		# Gets the parameters.
		user_id = self.request.get('user_id').encode('utf-8')
		folder = self.request.get('folder').encode('utf-8')
		extension = self.request.get('extension').encode('utf-8')
		tz_hour = self.request.get('tz_hour').encode('utf-8')
		tz_minute = self.request.get('tz_minute').encode('utf-8')

		# Checks whether the user exists or not.
		if not user_id or urlfetch.fetch('http://smart.fm/users/%s' % (user_id), method=urlfetch.HEAD, follow_redirects=False).status_code != 200:
			self.error(405)
			self.response.headers['Content-Type'] = 'text/plain; charset="UTF-8"'
			self.response.out.write('User not found.')
			return

		timestamp = datetime.now().strftime('%Y%m%d%H%M%S')

		self.request.str_POST['timestamp'] = timestamp	# ad hoc...

		# Stores the options.
		memcache.set(get_key(self, 'is_flat'), folder == 'flat')
		memcache.set(get_key(self, 'has_extension'), extension == 'extension')
		memcache.set(get_key(self, 'tz_hour'), tz_hour)
		memcache.set(get_key(self, 'tz_minute'), tz_minute)

		# Moves to the next phase.
		memcache.set(get_key(self, 'phase'), 1)
		taskqueue.add(url='/smartfm/memento/tasks/search', params={'user_id': user_id, 'timestamp': timestamp, 'page': 1})

		self.redirect('/smartfm/memento/progress?user_id=%s&timestamp=%s' % (user_id, timestamp))

class ProgressPage(webapp.RequestHandler):
	def get(self):
		# Gets the parameters.
		user_id = self.request.get('user_id').encode('utf-8')
		timestamp = self.request.get('timestamp').encode('utf-8')

		phase = get_phase(self)

		# Checks whether the access is valid or not.
		if phase == 0:
			self.error(405)
			self.response.headers['Content-Type'] = 'text/plain; charset="UTF-8"'
			self.response.out.write('Invalid access.')
			return

		counter = get_counter(self)

		download_urls = []
		for i in xrange(int(memcache.get(get_key(self, 'download_url_num')) or 0)):
			download_urls.append(memcache.get(get_download_urls_key(self, i)))

		# Outputs a rendered HTML.
		template_values = {
			'progress': 0 < phase and phase < 6,
			'download': phase == 6,
			'user_id': user_id,
			'timestamp': timestamp,
			'meta_refresh': '' if phase == 6 else '<meta http-equiv="Refresh" content="5; URL=%s">' % (self.request.url),
			'download_urls': download_urls,
			'status_1': '' if phase < 2 else '%s' % ('Done' if phase > 2 else 'Page %d' % (counter)),
			'status_2': '' if phase < 3 else '%s' % ('Done' if phase > 3 else 'Journal %d of %d' % (counter, memcache.get(get_key(self, 'journal_num')) or 0)),
			'status_3': '' if phase < 4 else '%s' % ('Done' if phase > 4 else 'Image %d of %d' % (counter, memcache.get(get_key(self, 'resource_num')) or 0)),
			'status_4': '' if phase < 5 else '%s' % ('Done' if phase > 5 else 'Dividing...'),
			'journals_bytes': int(memcache.get(get_key(self, 'journals_bytes')) or 0),
			'resources_bytes': int(memcache.get(get_key(self, 'resources_bytes')) or 0)
		}
		self.response.out.write(template.render(os.path.join(os.path.dirname(__file__), 'memento.html'), template_values))

class DownloadPage(webapp.RequestHandler):
	def get(self):
		def get_filepath(url, is_flat, has_extension):
			assert url.find('http://') == 0
			filepath = re.sub(r'\?.*$', '', url)
			if is_flat:
				filepath = re.sub(r'http://smart\.fm/journals/(\d+)/comments', r'/\1_comments', url)
				filepath = re.sub(r'^.*/', '/', filepath)
			else:
				filepath = re.sub(r'http://', '/', filepath)
			if has_extension and re.search(r'/[^/]*\.[^/]*$', filepath) == None:
				filepath += '.html'
			return re.sub(r'^/', '', filepath)	# for Windows compressed folder

		# Gets the parameters.
		user_id = self.request.get('user_id').encode('utf-8')
		timestamp = self.request.get('timestamp').encode('utf-8')
		target = int(self.request.get('target').encode('utf-8') or 0)	# 0: all, 1: text, 2: image
		range_begin = int(self.request.get('begin').encode('utf-8') or 0)
		range_end = int(self.request.get('end').encode('utf-8') or 0)

		# Gets the statuses.
		journal_num = int(memcache.get(get_key(self, 'journal_num')) or 0)
		resource_num = int(memcache.get(get_key(self, 'resource_num')) or 0)

		# Gets the options.
		is_flat = memcache.get(get_key(self, 'is_flat'))
		has_extension = memcache.get(get_key(self, 'has_extension'))

		# Checks whether the access is valid or not.
		if get_phase(self) < 6:
			self.error(405)
			self.response.headers['Content-Type'] = 'text/plain; charset="UTF-8"'
			self.response.out.write('Invalid access.')
			return

		# Checks whether the parameters are valid or not.
		if (target < 0 or 2 < target) or (range_begin > range_end) or (target == 0 and (range_begin != 0 or range_end != 0)):
			self.error(405)
			self.response.headers['Content-Type'] = 'text/plain; charset="UTF-8"'
			self.response.out.write('Invalid parameters.')
			return

		zipdata = StringIO.StringIO()
		zipobj = zipfile.ZipFile(zipdata, 'w', zipfile.ZIP_DEFLATED)

		# [Journals, comments, and a Movable Type text]
		if target == 0 or target == 1:
			rng = xrange(journal_num) if range_begin == 0 and range_end == 0 else xrange(range_begin, range_end)

			# Gets all the journals as Movable Type format and stores them into a zip file.
			mt_text = ''
			for i in rng:
				journal_url = memcache.get(get_journals_key(self, i))
				content = get_content(str(get_journal_id(journal_url)))
				if content:
					mt_text += content
			zipobj.writestr('mt_log.txt'.encode('utf-8'), mt_text)

			# Gets all the journal text and stores them into a zip file.
			for i in rng:
				journal_url = memcache.get(get_journals_key(self, i))
				journal = get_content(journal_url)
				if journal:
					filepath = get_filepath(journal_url, is_flat, has_extension)
					if not filepath in zipobj.namelist():
						zipobj.writestr(filepath.encode('utf-8'), journal)

				comment_url = get_comment_url(get_journal_id(journal_url))
				comment = get_content(comment_url)
				if comment:
					filepath = get_filepath(comment_url, is_flat, has_extension)
					if not filepath in zipobj.namelist():
						zipobj.writestr(filepath.encode('utf-8'), comment)

		# [Resources]
		if target == 0 or target == 2:
			rng = xrange(resource_num) if range_begin == 0 and range_end == 0 else xrange(range_begin, range_end)

			# Gets all the resources and stores them into a zip file.
			for i in rng:
				resource_url = memcache.get(get_resources_key(self, i))
				resource = get_content(resource_url)
				if resource:
					filepath = get_filepath(resource_url, is_flat, has_extension)
					if not filepath in zipobj.namelist():
						zipobj.writestr(filepath.encode('utf-8'), resource)

		zipobj.close()

		# Makes a file name.
		filename = '%s_%s' % (user_id, timestamp)
		if target != 0:
			filename += '_%d' % (target)
			if target == 2:
				filename += '_%d-%d' % (range_begin, range_end)
		filename += '.zip'

		# Returns a generated zip file as a response.
		self.response.headers['Content-Type'] = 'application/octet-stream'
		self.response.headers['Content-Disposition'] = 'attachment; filename="%s"' % (filename)
		self.response.out.write(zipdata.getvalue())

class CancelPage(webapp.RequestHandler):
	def get(self):
		# Gets the parameters.
		user_id = self.request.get('user_id').encode('utf-8')
		timestamp = self.request.get('timestamp').encode('utf-8')

		memcache.set(get_key(self, 'phase'), 0)

		self.redirect('/smartfm/memento.html')

class JournalSearcher(webapp.RequestHandler):
	def post(self):
		# Gets the parameters.
		user_id = self.request.get('user_id')
		timestamp = self.request.get('timestamp')
		page = int(self.request.get('page'))

		memcache.set(get_key(self, 'counter'), page)

		# Moves to the next phase.
		if get_phase(self) == 1:
			memcache.set(get_key(self, 'journal_num'), 0)
			memcache.incr(get_key(self, 'phase'))

		# Terminates the process if canceled.
		if get_phase(self) != 2:
			logging.info('This request was canceled.')
			return

		# Fetches a journal list page.
		list_url = 'http://smart.fm/users/%s/journal?page=%d' % (user_id, page)
		content = fetch_content(list_url, 0)

		# Parses an HTML to get a list of journals' URLs.
		parser = JournalParser(list_url)
		try:
			parser.feed(content)
		except HTMLParseError:
			logging.warning('Parse Error.')

		# Searches journals' URLs from an HTML.
		journal_urls = map(lambda rel_url: urlparse.urljoin(list_url, rel_url), re.findall(r'(/users/%s/journal/\d+/\d+/\d+/\d+[\w_-]*)' % (user_id), content))
		journal_urls += parser.journals

		if journal_urls:
			# Stores a list of journals.
			for url in journal_urls:
				journal_num = memcache.get(get_key(self, 'journal_num'))
				memcache.set(get_journals_key(self, journal_num), url)
				memcache.incr(get_key(self, 'journal_num'))

			if page < 100:
				taskqueue.add(url='/smartfm/memento/tasks/search', params={'user_id': user_id, 'timestamp': timestamp, 'page': page + 1})
				return

		# Eliminates redundant items.
		dict_urls = {}
		for i in xrange(memcache.get(get_key(self, 'journal_num')) or 0):
			url = memcache.get(get_journals_key(self, i))
			id = get_journal_id(url)
			if id not in dict_urls or len(url) > len(dict_urls[id]):
				dict_urls[get_journal_id(url)] = url
		memcache.set(get_key(self, 'journal_num'), len(dict_urls))

		# Stores an optimized list of journals.
		list_urls = sorted(dict_urls.values(), cmp=lambda a, b: cmp(get_journal_id(a), get_journal_id(b)), reverse=True)
		for i in xrange(len(list_urls)):
			memcache.set(get_journals_key(self, i), list_urls[i])

#		logging.info('journals:\n\t' + '\n\t'.join(list_urls))

		# Executes the next task to move to the next phase.
		taskqueue.add(url='/smartfm/memento/tasks/analyze', params={'user_id': user_id, 'timestamp': timestamp, 'journal_index': 0})

class JournalAnalyzer(webapp.RequestHandler):
	def post(self):
		# Gets the parameters.
		user_id = self.request.get('user_id')
		timestamp = self.request.get('timestamp')
		index = int(self.request.get('journal_index'))

		# Gets the options.
		tz_hour = int(memcache.get(get_key(self, 'tz_hour')) or 0)
		tz_minute = int(memcache.get(get_key(self, 'tz_minute')) or 0)

		memcache.set(get_key(self, 'counter'), index)

		# Moves to the next phase.
		if get_phase(self) == 2:
			memcache.set(get_key(self, 'resource_num'), 0)
			memcache.incr(get_key(self, 'phase'))

		# Terminates the process if canceled.
		if get_phase(self) != 3:
			logging.info('This request was canceled.')
			return

		if index < int(memcache.get(get_key(self, 'journal_num')) or 0):
			# Fetches an HTML file.
			journal_url = memcache.get(get_journals_key(self, index))
			journal = fetch_content(journal_url, 86400)

			# Fetches comments.
			comment = fetch_content(get_comment_url(get_journal_id(journal_url)), 21600)

			# Updates download size.
			bytes = int(memcache.get(get_key(self, 'journals_bytes')) or 0)
			bytes += len(journal)
			bytes += len(comment)
			memcache.set(get_key(self, 'journals_bytes'), bytes)

			# Parses an HTML to get a list of resources' URLs.
			journal_parser = JournalParser(journal_url)
			try:
				journal_parser.feed(journal.decode('utf-8'))
			except HTMLParseError:
				logging.warning('Parse Error.')

			# Parses comments.
			comment_parser = CommentParser(journal_url)
			try:
				comment_parser.feed(comment.decode('utf-8'))
			except HTMLParseError:
				logging.warning('Parse Error.')
				comment_parser.canonicalize_comments()

			# Stores a list of resources.
			for url in journal_parser.resources:
				url = modify_image_url(url)	# workaround for the bug in smart.fm
				resource_num = memcache.get(get_key(self, 'resource_num'))
				memcache.set(get_resources_key(self, resource_num), url)
				memcache.incr(get_key(self, 'resource_num'))

			# Converts an HTML body into a Movable Type format text.
			filter = JournalBodyFilter(journal_url)
			try:
				filter.feed('\n'.join(journal.decode('utf-8').split('\n')[journal_parser.body_range[0]:journal_parser.body_range[1]]))
			except HTMLParseError:
				logging.warning('Parse Error.')

			# Generates a meta data section.
			journal_parser.date += timedelta(hours=tz_hour, minutes=tz_minute)
			fh = StringIO.StringIO()
			fh.writelines([
				'TITLE: %s\n' % (journal_parser.title),
				'DATE: %s\n' % (journal_parser.date.strftime('%m/%d/%Y %H:%M:%S')),
				'STATUS: publish\n',
				'ALLOW COMMENTS: 1\n',
				'ALLOW PINGS: 1\n',
				'CONVERT BREAKS: 0\n'])
			fh.writelines(['CATEGORY: %s\n' % (tag) for tag in journal_parser.tags])
			fh.write('-----\n')

			# Generates a body field section.
			fh.write('BODY:\n')
			fh.write(filter.body)
			fh.write('\n-----\n')

			# Generates comments field section.
			for comment in comment_parser.comments:
				comment['date'] += timedelta(hours=tz_hour, minutes=tz_minute)
				fh.write('COMMENT:\n')
				fh.write('AUTHOR: %s\n' % (comment['username']))
				fh.write('DATE: %s\n' % (comment['date'].strftime('%m/%d/%Y %H:%M:%S')))
				fh.write(comment['comment'])
				fh.write('-----\n')

			fh.write('--------\n')

			# Stores a Movable Type format text.
			put_content(str(get_journal_id(journal_url)), fh.getvalue().encode('utf-8'))
			fh.close()

			taskqueue.add(url='/smartfm/memento/tasks/analyze', params={'user_id': user_id, 'timestamp': timestamp, 'journal_index': index + 1})

		else:
			# Eliminates redundant items.
			set_urls = set()
			for i in xrange(memcache.get(get_key(self, 'resource_num')) or 0):
				set_urls.add(memcache.get(get_resources_key(self, i)))
			for url in sorted(set_urls):
				if not re.match(r'.*/%s/.*' % (user_id), url):
					set_urls.remove(url)
			memcache.set(get_key(self, 'resource_num'), len(set_urls))

			# Stores an optimized list of resources.
			list_urls = sorted(set_urls)
			for i in xrange(len(list_urls)):
				memcache.set(get_resources_key(self, i), list_urls[i])

#			logging.info('resources:\n\t' + '\n\t'.join(list_urls))

			# Executes the next task to move to the next phase.
			taskqueue.add(url='/smartfm/memento/tasks/fetch', params={'user_id': user_id, 'timestamp': timestamp, 'resource_index': 0})

class ResourceFetcher(webapp.RequestHandler):
	def post(self):
		# Gets the parameters.
		user_id = self.request.get('user_id')
		timestamp = self.request.get('timestamp')
		index = int(self.request.get('resource_index'))

		memcache.set(get_key(self, 'counter'), index)

		# Moves to the next phase.
		if get_phase(self) == 3:
			memcache.set(get_key(self, 'counter'), 0)
			memcache.incr(get_key(self, 'phase'))

		# Terminates the process if canceled.
		if get_phase(self) != 4:
			logging.info('This request was canceled.')
			return

		if index < int(memcache.get(get_key(self, 'resource_num')) or 0):
			# Fetches a resource file.
			resource = fetch_content(memcache.get(get_resources_key(self, index)))

			# Updates download size.
			bytes = int(memcache.get(get_key(self, 'resources_bytes')) or 0)
			bytes += len(resource)
			memcache.set(get_key(self, 'resources_bytes'), bytes)

			taskqueue.add(url='/smartfm/memento/tasks/fetch', params={'user_id': user_id, 'timestamp': timestamp, 'resource_index': index + 1})

		else:
			# Executes the next task to move to the next phase.
			taskqueue.add(url='/smartfm/memento/tasks/divide', params={'user_id': user_id, 'timestamp': timestamp})

class ZipDivider(webapp.RequestHandler):
	MAX_FILESIZE = 10 * 1024 * 1024 - 65536

	def post(self):
		# Gets the parameters.
		user_id = self.request.get('user_id')
		timestamp = self.request.get('timestamp')

		# Gets the statuses.
		journal_num = int(memcache.get(get_key(self, 'journal_num')) or 0)
		resource_num = int(memcache.get(get_key(self, 'resource_num')) or 0)

		# Moves to the next phase.
		if get_phase(self) == 4:
			memcache.set(get_key(self, 'counter'), 0)
			memcache.incr(get_key(self, 'phase'))

		# Terminates the process if canceled.
		if get_phase(self) != 5:
			logging.info('This request was canceled.')
			return

		# Calculates total file size of texts.
		journals_bytes = 0
		for i in xrange(journal_num):
			journal_url = memcache.get(get_journals_key(self, i))
			journals_bytes += get_content_size(journal_url)

			comment_url = get_comment_url(get_journal_id(journal_url))
			journals_bytes += get_content_size(comment_url)

			journals_bytes += get_content_size(str(get_journal_id(journal_url)))

		# Divides the total files into 10MB chunks.
		current_bytes = journals_bytes
		resources_bytes = 0
		range_begin = 0
		url_index = 0
		for i in xrange(resource_num):
			resource_url = memcache.get(get_resources_key(self, i))
			bytes = get_content_size(resource_url)

			if current_bytes + bytes > self.MAX_FILESIZE and url_index == 0:
				memcache.set(get_download_urls_key(self, url_index), '/smartfm/memento/download?user_id=%s&timestamp=%s&target=1' % (user_id, timestamp))
				current_bytes = resources_bytes
				url_index += 1

			if current_bytes + bytes > self.MAX_FILESIZE and url_index > 0:
				memcache.set(get_download_urls_key(self, url_index), '/smartfm/memento/download?user_id=%s&timestamp=%s&target=2&begin=%d&end=%d' % (user_id, timestamp, range_begin, i))
				range_begin = i
				current_bytes = 0
				url_index += 1

			current_bytes += bytes
			resources_bytes += bytes

		if url_index == 0:
			assert journals_bytes + resources_bytes == current_bytes
			assert current_bytes <= self.MAX_FILESIZE
			memcache.set(get_download_urls_key(self, url_index), '/smartfm/memento/download?user_id=%s&timestamp=%s' % (user_id, timestamp))
		else:
			memcache.set(get_download_urls_key(self, url_index), '/smartfm/memento/download?user_id=%s&timestamp=%s&target=2&begin=%d&end=%d' % (user_id, timestamp, range_begin, resource_num))

		memcache.set(get_key(self, 'download_url_num'), url_index + 1)

		# Moves to the next phase.
		memcache.incr(get_key(self, 'phase'))

class Data(db.Model):
	url = db.StringProperty(required=True)
	body = db.BlobProperty(required=True)
	size = db.IntegerProperty()

def fetch_content(url, time=21600):
	logging.info(url)

	content = None
	if time > 0:
		content = memcache.get(url)
	if content == None:
		try:
			headers = {} if re.match(r'http://smart\.fm/journals/\d+/\w', url) == None else {'X-Requested-With': 'XMLHttpRequest'}
			result = urlfetch.fetch(url, headers=headers)
			if result.status_code < 400:
				content = result.content
				if time > 0:
					memcache.add(url, content, time)
					data = Data(url=url, body=content, size=len(content))
					data.put()
		except (urlfetch.InvalidURLError, urlfetch.DownloadError):
			pass

	return content or ''

def get_content(url):
	content = memcache.get(url)
	if content == None:
		query = db.Query(Data).filter('url =', url)
		data = query.get()
		if data:
			content = data.body

	return content or ''

def get_content_size(url):
#	content = memcache.get(url)
#	if content != None:
#		return len(content)

	query = db.Query(Data).filter('url =', url)
	data = query.get()
	if data:
		return data.size

	return 0

def put_content(key, content):
	assert key != None and content != None
	data = Data(url=key, body=content, size=len(content))
	data.put()

def modify_image_url(url):
	return re.sub(r'^http://smart.fm/assets/user/', 'http://assets.smart.fm/assets/users/', url)	# workaround for the bug in smart.fm

def get_journal_id(journal_url):
	assert journal_url.find('http://') == 0
	m = re.match(r'.*/journal/\d+/\d+/\d+/(\d+)', journal_url)
	return int(m.group(1) or 0) if m else 0

def get_comment_url(journal_id):
	assert journal_id > 0
	return 'http://smart.fm/journals/%d/comments' % (journal_id)

def get_key(request_handler, keyname):
	user_id = request_handler.request.get('user_id')
	timestamp = request_handler.request.get('timestamp')
	return '%s_%s_%s' % (user_id, timestamp, keyname)

def get_phase(request_handler):
	phase = memcache.get(get_key(request_handler, 'phase'))
	return int(phase) if phase else 0

def get_counter(request_handler):
	counter = memcache.get(get_key(request_handler, 'counter'))
	return int(counter) if counter else 0

def get_journals_key(request_handler, index):
	user_id = request_handler.request.get('user_id')
	timestamp = request_handler.request.get('timestamp')
	return '%s_%s_journal_%05d' % (user_id, timestamp, index)

def get_resources_key(request_handler, index):
	user_id = request_handler.request.get('user_id')
	timestamp = request_handler.request.get('timestamp')
	return '%s_%s_resources_%05d' % (user_id, timestamp, index)

def get_download_urls_key(request_handler, index):
	user_id = request_handler.request.get('user_id')
	timestamp = request_handler.request.get('timestamp')
	return '%s_%s_download_urls_%d' % (user_id, timestamp, index)

def main():
	application = webapp.WSGIApplication([
		('/smartfm/memento.html', MainPage),
		('/smartfm/memento/trigger', TriggerPage),
		('/smartfm/memento/progress', ProgressPage),
		('/smartfm/memento/download', DownloadPage),
		('/smartfm/memento/cancel', CancelPage),
		('/smartfm/memento/tasks/search', JournalSearcher),
		('/smartfm/memento/tasks/analyze', JournalAnalyzer),
		('/smartfm/memento/tasks/fetch', ResourceFetcher),
		('/smartfm/memento/tasks/divide', ZipDivider),
		], debug=True)
	wsgiref.handlers.CGIHandler().run(application)

if __name__ == '__main__':
	main()
