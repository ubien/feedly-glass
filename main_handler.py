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


PAGINATED_HTML = """
<article class='auto-paginate'>
<h2 class='blue text-large'>Did you know...?</h2>
<p>Cats are <em class='yellow'>solar-powered.</em> The time they spend
napping in direct sunlight is necessary to regenerate their internal
batteries. Cats that do not receive sufficient charge may exhibit the
following symptoms: lethargy, irritability, and disdainful glares. Cats
will reactivate on their own automatically after a complete charge
cycle; it is recommended that they be left undisturbed during this
process to maximize your enjoyment of your cat.</p><br/><p>
For more cat maintenance tips, tap to view the website!</p>
</article>
"""

FEEDLY_AUTH_URL = "http://sandbox.feedly.com/v3/auth/auth?response_type=code&client_id=sandbox&redirect_uri=http://localhost&scope=https%3A%2F%2Fcloud.feedly.com%2Fsubscriptions"
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


class MainHandler(webapp2.RequestHandler):
  """Request Handler for the main endpoint."""

  def _render_template(self, message=None):
    """Render the main page template."""
    template_values = {'userId': self.userid}
    if message:
      template_values['message'] = message
    # self.mirror_service is initialized in util.auth_required.
    try:
      template_values['contact'] = self.mirror_service.contacts().get(
        id='python-quick-start').execute()
    except errors.HttpError:
      logging.info('Unable to find Python Quick Start contact.')

    timeline_items = self.mirror_service.timeline().list(maxResults=3).execute()
    template_values['timelineItems'] = timeline_items.get('items', [])

    subscriptions = self.mirror_service.subscriptions().list().execute()
    for subscription in subscriptions.get('items', []):
      collection = subscription.get('collection')
      if collection == 'timeline':
        template_values['timelineSubscriptionExists'] = True
      elif collection == 'locations':
        template_values['locationSubscriptionExists'] = True

    template = jinja_environment.get_template('templates/index.html')
    self.response.out.write(template.render(template_values))

  @util.auth_required
  def get(self):
    """Render the main page."""
    # Get the flash message and delete it.
    if self.request.get("code"):
        self._handle_feedly_auth(self.request.get("code"))
    message = memcache.get(key=self.userid)
    memcache.delete(key=self.userid)
    self._render_template(message)

  def _handle_feedly_auth(self, code):
    print code
    fa = FeedlyAPI('sandbox', 'Z5ZSFRASVWCV3EFATRUY')
    resp = fa.getToken(code, 'http://localhost')
    user = db.GqlQuery("SELECT * FROM FeedlyUser WHERE id = :1", self.userid).get()
    print resp
    if not user:
        user = FeedlyUser(id=self.userid)
    user.feedly_access_token=resp['access_token']
    user.feedly_refresh_token=resp['refresh_token']
    print "insert "+self.userid
    user.put()

  @util.auth_required
  def post(self):
    """Execute the request and render the template."""
    operation = self.request.get('operation')
    # Dict of operations to easily map keys to methods.
    operations = {
        'insertSubscription': self._insert_subscription,
        'deleteSubscription': self._delete_subscription,
        'insertItem': self._insert_item,
        'insertPaginatedItem': self._insert_paginated_item,
        'insertItemWithAction': self._insert_item_with_action,
        'insertItemAllUsers': self._insert_item_all_users,
        'insertContact': self._insert_contact,
        'deleteContact': self._delete_contact,
        'deleteTimelineItem': self._delete_timeline_item
    }
    if operation in operations:
      message = operations[operation]()
    else:
      message = "I don't know how to " + operation
    # Store the flash message for 5 seconds.
    memcache.set(key=self.userid, value=message, time=5)
    self.redirect('/')

  def _insert_subscription(self):
    """Subscribe the app."""
    # self.userid is initialized in util.auth_required.
    body = {
        'collection': self.request.get('collection', 'timeline'),
        'userToken': self.userid,
        'callbackUrl': util.get_full_url(self, '/notify')
    }
    # self.mirror_service is initialized in util.auth_required.
    self.mirror_service.subscriptions().insert(body=body).execute()
    return 'Application is now subscribed to updates.'

  def _delete_subscription(self):
    """Unsubscribe from notifications."""
    collection = self.request.get('subscriptionId')
    self.mirror_service.subscriptions().delete(id=collection).execute()
    return 'Application has been unsubscribed.'

  def _insert_item(self):
    """Insert a timeline item."""
    logging.info('Inserting timeline item')
    body = {
        'notification': {'level': 'DEFAULT'}
    }
    if self.request.get('html') == 'on':
      body['html'] = [self.request.get('message')]
    else:
      body['text'] = self.request.get('message')

    media_link = self.request.get('imageUrl')
    if media_link:
      if media_link.startswith('/'):
        media_link = util.get_full_url(self, media_link)
      resp = urlfetch.fetch(media_link, deadline=20)
      media = MediaIoBaseUpload(
          io.BytesIO(resp.content), mimetype='image/jpeg', resumable=True)
    else:
      media = None

    # self.mirror_service is initialized in util.auth_required.
    self.mirror_service.timeline().insert(body=body, media_body=media).execute()
    return  'A timeline item has been inserted.'

  def _insert_paginated_item(self):
    """Insert a paginated timeline item."""
    logging.info('Inserting paginated timeline item')
    body = {
        'html': PAGINATED_HTML,
        'notification': {'level': 'DEFAULT'},
        'menuItems': [{
            'action': 'OPEN_URI',
            'payload': 'https://www.google.com/search?q=cat+maintenance+tips'
        }]
    }
    # self.mirror_service is initialized in util.auth_required.
    self.mirror_service.timeline().insert(body=body).execute()
    return  'A timeline item has been inserted.'

  def _insert_item_with_action(self):
    """Insert a timeline item user can reply to."""
    logging.info('Inserting timeline item')
    body = {
        'creator': {
            'displayName': 'Python Starter Project',
            'id': 'PYTHON_STARTER_PROJECT'
        },
        'text': 'Tell me what you had for lunch :)',
        'notification': {'level': 'DEFAULT'},
        'menuItems': [{'action': 'REPLY'}]
    }
    # self.mirror_service is initialized in util.auth_required.
    self.mirror_service.timeline().insert(body=body).execute()
    return 'A timeline item with action has been inserted.'

  def _insert_item_all_users(self):
    """Insert a timeline item to all authorized users."""
    logging.info('Inserting timeline item to all users')
    users = Credentials.all()
    total_users = users.count()

    if total_users > 10:
      return 'Total user count is %d. Aborting broadcast to save your quota' % (
          total_users)
    body = {
        'text': 'Hello Everyone!',
        'notification': {'level': 'DEFAULT'}
    }

    batch_responses = _BatchCallback()
    batch = BatchHttpRequest(callback=batch_responses.callback)
    for user in users:
      creds = StorageByKeyName(
          Credentials, user.key().name(), 'credentials').get()
      mirror_service = util.create_service('mirror', 'v1', creds)
      batch.add(
          mirror_service.timeline().insert(body=body),
          request_id=user.key().name())

    batch.execute(httplib2.Http())
    return 'Successfully sent cards to %d users (%d failed).' % (
        batch_responses.success, batch_responses.failure)

  def _insert_contact(self):
    """Insert a new Contact."""
    logging.info('Inserting contact')
    id = self.request.get('id')
    name = self.request.get('name')
    image_url = self.request.get('imageUrl')
    if not name or not image_url:
      return 'Must specify imageUrl and name to insert contact'
    else:
      if image_url.startswith('/'):
        image_url = util.get_full_url(self, image_url)
      body = {
          'id': id,
          'displayName': name,
          'imageUrls': [image_url],
          'acceptCommands': [{ 'type': 'TAKE_A_NOTE' }]
      }
      # self.mirror_service is initialized in util.auth_required.
      self.mirror_service.contacts().insert(body=body).execute()
      return 'Inserted contact: ' + name

  def _delete_contact(self):
    """Delete a Contact."""
    # self.mirror_service is initialized in util.auth_required.
    self.mirror_service.contacts().delete(
        id=self.request.get('id')).execute()
    return 'Contact has been deleted.'

  def _delete_timeline_item(self):
    """Delete a Timeline Item."""
    logging.info('Deleting timeline item')
    # self.mirror_service is initialized in util.auth_required.
    self.mirror_service.timeline().delete(id=self.request.get('itemId')).execute()
    return 'A timeline item has been deleted.'
	
class FeedlyHandler(webapp2.RequestHandler):
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

    @util.auth_required
    def get(self):
        if self.request.path == '/feeds':
            self._get_stream()

    def _get_stream(self):
        token = self._get_auth_token()
        if token:
            fa = FeedlyAPI('sandbox', 'Z5ZSFRASVWCV3EFATRUY')
            #self.response.out.write(json.dumps(fa.getSubscription(token=token)))
            categores = {
                'Uncategorized' : []
            }
            
            self._subscribeTimelineEvent()
            #self._clearTimeline()

            #self._insert_bundle_cover('Feedly', 1)
            categories = fa.getCategories(token=token)
            #subscriptions = fa.getSubscription(token=token)
            #for category in categories:
            profile = fa.getProfile(token)
            userId = profile['id']
            print userId
            feed_content = fa.getStreamContentUser(token, userId, count=1, unreadOnly='true')
            #feed_content = fa.getStreamContent(token, feed['id'], count=1, ranked="newest", unreadOnly=True, newerThan=None, continuation=None)
            print feed_content
            for item in feed_content['items']:
                print item
                image = None
                if 'thumbnail' in item:
                    image = item['thumbnail'][0]['url']
                elif 'visual' in item and 'url' in item['visual']:
                    image = item['visual']['url']
                self._insert_card(item['id'], item['title'], item['origin']['title'], image, item['alternate'][0]['href'], 1)

    def _insert_card(self, id, title, source, image, link, bundleId):
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
                {   'action' : 'CUSTOM',
                    'id': 'pocket',
                    'payload' : link,
                    'values' : [{'displayName' : 'Add To Pocket',
                              'iconUrl': 'http://3.bp.blogspot.com/-OTaixNGesIU/T45FQHvE8zI/AAAAAAAACUE/IB6Gd4y-MNQ/s1600/128.png'
                            }
                    ]
                },
                {
                    'action' : 'DELETE'
                }
            ],
            'html' : "<article><h1>"+title+"</h1><h2><i>"+source+"</i></h2>"
        }

        if image and image != 'none':
            body['html'] += '<img src="'+image+'" />'
            resp = urlfetch.fetch(image, deadline=20)
            media = MediaIoBaseUpload(
                io.BytesIO(resp.content), mimetype=self._get_mime_type(image), resumable=True)
        else:
          media = None
        body['html'] += "</article>"
        self.mirror_service.timeline().insert(body=body, media_body=media).execute()

    def _insert_bundle_cover(self, category, bundleId):
        body = {
            'bundleId' : bundleId,
            'isBundleCover' : True,
        }
        media_link = "http://glass-apps.org/wp-content/uploads/2013/03/feedly-logo1.png"
        body['html'] = "<article><h1>"+category+'</h1><img src="'+media_link+'" /></article>'
        resp = urlfetch.fetch(media_link, deadline=20)
        media = MediaIoBaseUpload(
            io.BytesIO(resp.content), mimetype=self._get_mime_type(media_link), resumable=True)
        self.mirror_service.timeline().insert(body=body, media_body=media).execute()

    def post(self):
        logging.info('SavePocket')
        data = json.loads(self.request.body)
        print "post"
        print data
        actions  = data.get('userActions', [])
        for action in actions:
            if 'payload' in action and action['payload'] == 'save':
                credentials = StorageByKeyName(Credentials, data['userToken'], 'credentials').get()
                if credentials:
                    mirror_service = util.create_service('mirror', 'v1', credentials)
                    timeline_item = mirror_service.timeline().get(id=data['itemId']).execute()
                    print 'save to feedly'
                    print timeline_item
                    token = self._get_auth_token(data['userToken'])
                    if token:
                        fa = FeedlyAPI('sandbox', 'Z5ZSFRASVWCV3EFATRUY')
                        fa.addTagSave(timeline_item['sourceItemId'], token)

        self.response.set_status(200)
        self.response.out.write("")

    def _subscribeTimelineEvent(self):
        callback_url = 'https://mirrornotifications.appspot.com/forward?url=http://ec2-23-20-178-62.compute-1.amazonaws.com:28000/subscriptions'
        #callback_url = 'https://feedly-glass.appspot.com/subscriptions'
        subscriptions = self.mirror_service.subscriptions().list().execute()
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
            self.mirror_service.subscriptions().insert(body=body).execute()

    def _clearTimeline(self):
        timeline_items = self.mirror_service.timeline().list(maxResults=20).execute()
        cards = timeline_items.get('items', [])
        for card in cards:
            self._clearTimelineItem(card['id'])

    def _clearTimelineItem(self, id):
        self.mirror_service.timeline().delete(id=id).execute()

MAIN_ROUTES = [
    ('/', MainHandler),
    ('/feeds', FeedlyHandler),
    ('/subscriptions', FeedlyHandler),

]
