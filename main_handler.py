# Copyright (C) 2013 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Request Handler for /main endpoint."""

__author__ = 'alainv@google.com (Alain Vongsouvanh)'


import io
import jinja2
import logging
import os
import webapp2

from google.appengine.api import memcache
from google.appengine.api import urlfetch

import httplib2
import json
from random import randint
from apiclient import errors
from apiclient.http import MediaIoBaseUpload
from apiclient.http import BatchHttpRequest
from oauth2client.appengine import StorageByKeyName
from lib.FeedlySDK.FeedlyApi import FeedlyAPI
from model import Credentials, FeedlyUser
from google.appengine.ext import db
import util


jinja_environment = jinja2.Environment(
    loader=jinja2.FileSystemLoader(os.path.dirname(__file__)))

FEEDLY_SECRET = None
class _BatchCallback(object):
  """Class used to track batch request responses."""

  def __init__(self):
    """Initialize a new _BatchCallback object."""
    self.success = 0
    self.failure = 0

  def callback(self, request_id, response, exception):
    """Method called on each HTTP Response from a batch request.

    For more information, see
      https://developers.google.com/api-client-library/python/guide/batch
    """
    if exception is None:
      self.success += 1
    else:
      self.failure += 1
      logging.error(
          'Failed to insert item for user %s: %s', request_id, exception)

class LandingPage(webapp2.RequestHandler):
    def get(self):
        if self.request.get("code"):
            self._handle_feedly_auth(self.request.get("code"))

        template_variables = {
            'google_auth' : util.auth_required(self),
            'feedly_auth' : False
        }

        if template_variables['google_auth']:
            template_variables['feedly_auth'] = self._check_feedly_auth()

            
        template = jinja_environment.get_template('templates/index.html')
        self.response.out.write(template.render(template_variables))

    @util.auth_required
    def _check_feedly_auth(self):
        user = db.GqlQuery("SELECT * FROM FeedlyUser WHERE id = :1", self.userid).get()
        if user and user.feedly_access_token != '':
            return True
        return False

    @util.auth_required
    def _handle_feedly_auth(self, code):
        print code
        fa = FeedlyAPI('sandbox', FEEDLY_SECRET)
        resp = fa.getToken(code, 'http://localhost')
        user = db.GqlQuery("SELECT * FROM FeedlyUser WHERE id = :1", self.userid).get()
        print resp
        if 'access_token' in resp:
            if not user:
                user = FeedlyUser(id=self.userid)
            user.feedly_access_token=resp['access_token']
            user.feedly_refresh_token=resp['refresh_token']
            print "insert "+self.userid
            user.put()



class FeedlyHandler(webapp2.RequestHandler):

    @util.auth_required
    def get(self):
        self._refresh_stream(self.mirror_service)

    def post(self):
        data = json.loads(self.request.body)
        actions  = data.get('userActions', [])
        for action in actions:
            if 'payload' in action:
                credentials = StorageByKeyName(Credentials, data['userToken'], 'credentials').get()
                token = self._get_auth_token(data['userToken'])
                if credentials and token:
                    mirror_service = util.create_service('mirror', 'v1', credentials)
                    timeline_item = mirror_service.timeline().get(id=data['itemId']).execute()
                    if  action['payload'] == 'save':
                        print 'save to feedly'
                        fa = FeedlyAPI('sandbox', FEEDLY_SECRET)
                        id_parts = self._parse_source_id(timeline_item['sourceItemId'])
                        fa.addTagSave(id_parts['userId'], id_parts['entryId'], token)
                    elif action['payload'] == 'refresh':
                        print timeline_item['sourceItemId']
                        if memcache.get(key=timeline_item['sourceItemId']):
                            memcache.delete(timeline_item['sourceItemId'])
                            print 'refresh items'
                            print data
                            self._refresh_stream(mirror_service, token=token)
        self.response.set_status(200)
        self.response.out.write("")

    def _get_auth_token(self, userId=None):
        if not userId:
            userId = self.userid
        user = db.GqlQuery("SELECT * FROM FeedlyUser WHERE id = :1", userId).get()
        if user:
            return user.feedly_access_token
        else:
            return None

    def _get_mime_type(self, image_url):
        if '.png' in image_url:
            return "image/png"
        elif '.bmp' in image_url:
            return "image/bmp"
        else:
            return "image/jpeg"

    def _get_source_id(self, feedlyUserId, feedlyItemId):
        return feedlyUserId + "#*#" + feedlyItemId

    def _parse_source_id(self, id):
        pieces = id.split("#*#")
        return {
            'userId' : pieces[0],
            'entryId' : pieces[1]
        }

    def _get_refresh_id(self, feedlyUserId):
        return feedlyUserId + "#REFRESH#"+str(randint(1,1000))

    def _refresh_stream(self, mirror_service, token=None):
        if not token:
            token = self._get_auth_token()
        if token:
            fa = FeedlyAPI('sandbox', FEEDLY_SECRET)
            profile = fa.getProfile(token)
            userId = profile['id']
            self._subscribeTimelineEvent(mirror_service)
            self._clearTimeline(mirror_service)
            cardCover = self._create_bundle_cover(1)
            cardRefresh = self._create_refresh_card(self._get_refresh_id(userId), 1)
            batch = BatchHttpRequest()
            batch.add(
                mirror_service.timeline().insert(body=cardCover),
                request_id=str(userId)+'-cover')
            batch.add(
                mirror_service.timeline().insert(body=cardRefresh),
                request_id=str(userId)+'-refresh')
            feed_content = fa.getStreamContentUser(token, userId, count=5, unreadOnly='true')

            if feed_content['items']:
                markEntryIds = []
                for item in feed_content['items']:
                    print item['title']
                    image = None
                    if 'thumbnail' in item:
                        image = item['thumbnail'][0]['url']
                    elif 'visual' in item and 'url' in item['visual']:
                        image = item['visual']['url']
                    markEntryIds.append(item['id'])
                    source_id = self._get_source_id(userId,item['id'])
                    body = self._create_card(source_id, item['title'], item['origin']['title'], image, item['alternate'][0]['href'], 1)
                    batch.add(
                        mirror_service.timeline().insert(body=body),
                        request_id=item['id'])
                batch.execute(httplib2.Http())
                fa.markAsRead(token, markEntryIds)


    def _create_card(self, id, title, source, image, link, bundleId):
        html = "<article class=\"photo\">\n"
        if image:
            html += '<img src="'+image+'" width="100%" height="100%"><div class="photo-overlay"/>'
        html += '<section><div class="text-x-large"><p><strong class="blue">'+title+'</strong></div></section><footer><div><em class="yellow">'+source+'</em></p></div></footer></article>'

        body = {
            'bundleId' : bundleId,
            'sourceItemId' : id,
            'menuItems' : [{
                    'action': 'OPEN_URI',
                    'payload': link
                },
                {   'action' : 'CUSTOM',
                    'id': 'save',
                    'payload' : link,
                    'values' : [{'displayName' : 'Save For Later',
                              'iconUrl': 'http://files.softicons.com/download/system-icons/web0.2ama-icons-by-chrfb/png/128x128/Bookmark.png'
                            }
                    ]
                },
#                {   'action' : 'CUSTOM',
#                    'id': 'pocket',
#                    'payload' : link,
#                    'values' : [{'displayName' : 'Add To Pocket',
#                              'iconUrl': 'http://3.bp.blogspot.com/-OTaixNGesIU/T45FQHvE8zI/AAAAAAAACUE/IB6Gd4y-MNQ/s1600/128.png'
#                            }
#                    ]
#                },
                {
                    'action' : 'DELETE'
                }
            ],
            'html' : html
        }
        return body

    def _create_bundle_cover(self, bundleId):
        body = {
            'bundleId' : bundleId,
            'isBundleCover' : True,
            'notification': {'level': 'DEFAULT'},
            'html' : '<img src="http://glass-apps.org/wp-content/uploads/2013/03/feedly-logo1.png" width="100%" height="100%"><section><p class="text-auto-size white">Feedly</p></section>',

        }
        return body

    def _create_refresh_card(self, id, bundleId):
        print "refresh id:"+str(id)
        memcache.set(key=id, value=True)
        body = {
            'bundleId' : bundleId,
            'sourceItemId' : id,
            'html' : '<img src="http://blog.cachinko.com/blog/wp-content/uploads/2012/02/refresh.png" width="100%" height="100%"><div class="photo-overlay"/><section><p class="text-auto-size white">Refresh</p></section>',
            'menuItems' : [
                {   'action' : 'CUSTOM',
                    'id': 'refresh',
                    'values' : [{'displayName' : 'Clear current items',
                              'iconUrl': 'http://blog.cachinko.com/blog/wp-content/uploads/2012/02/refresh.png'
                            }
                    ]
                },
                {
                    'action' : 'DELETE'
                }]
        }
        return body

    def _subscribeTimelineEvent(self,mirror_service):
        callback_url = 'https://mirrornotifications.appspot.com/forward?url=http://ec2-23-20-178-62.compute-1.amazonaws.com:28000/subscriptions'
        #callback_url = 'https://feedly-glass.appspot.com/subscriptions'
        subscriptions = mirror_service.subscriptions().list().execute()
        should_set = True
        for subscription in subscriptions.get('items', []):
            if subscription.get('collection') == 'timeline':
                if subscription['callbackUrl'] == callback_url or subscription['userToken'] == self.userid:
                    should_set = False

        if should_set:
            body = {
                'collection': 'timeline',
                'userToken': self.userid,
                'callbackUrl': callback_url
            }
            mirror_service.subscriptions().insert(body=body).execute()

    def _clearTimeline(self, mirror_service):
        timeline_items = mirror_service.timeline().list(maxResults=20).execute()
        cards = timeline_items.get('items', [])

        if cards:
            batch_responses = _BatchCallback()
            batch = BatchHttpRequest(batch_responses.callback)
            for card in cards:
              batch.add(
                  mirror_service.timeline().delete(id=card['id']),
                  request_id=card['id'])
            batch.execute(httplib2.Http())

MAIN_ROUTES = [
    ('/', LandingPage),
    ('/feeds', FeedlyHandler)
]
