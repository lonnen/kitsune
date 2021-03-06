from copy import copy
from datetime import datetime

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.sites.models import Site
from django.core import mail
from django.core.cache import cache

import celery.conf
import mock
from nose.tools import eq_
from test_utils import RequestFactory
import waffle

from sumo.tests import TestCase
from wiki.tasks import (send_reviewed_notification, rebuild_kb,
                        schedule_rebuild_kb, _rebuild_kb_chunk)
from wiki.tests import TestCaseBase, revision


REVIEWED_EMAIL_CONTENT = """Your revision has been reviewed.

admin has approved your revision to the document
%s.

Message from the reviewer:

%s

To view the history of this document, click the following
link, or paste it into your browser's location bar:

https://testserver/en-US/kb/%s/history
"""


class RebuildTestCase(TestCase):
    fixtures = ['users.json', 'wiki/documents.json']
    rf = RequestFactory()
    ALWAYS_EAGER = celery.conf.ALWAYS_EAGER

    def setUp(self):
        self.old_settings = copy(settings._wrapped.__dict__)
        celery.conf.ALWAYS_EAGER = True

    def tearDown(self):
        cache.delete(settings.WIKI_REBUILD_TOKEN)
        settings._wrapped.__dict__ = self.old_settings
        celery.conf.ALWAYS_EAGER = self.ALWAYS_EAGER

    @mock.patch.object(rebuild_kb, 'delay')
    @mock.patch.object(waffle, 'switch_is_active')
    def test_eager_queue(self, switch_is_active, delay):
        switch_is_active.return_value = True
        schedule_rebuild_kb()
        assert not cache.get(settings.WIKI_REBUILD_TOKEN)
        assert not delay.called

    @mock.patch.object(rebuild_kb, 'delay')
    @mock.patch.object(waffle, 'switch_is_active')
    def test_task_queue(self, switch_is_active, delay):
        switch_is_active.return_value = True
        celery.conf.ALWAYS_EAGER = False
        schedule_rebuild_kb()
        assert cache.get(settings.WIKI_REBUILD_TOKEN)
        assert delay.called

    @mock.patch.object(rebuild_kb, 'delay')
    @mock.patch.object(waffle, 'switch_is_active')
    def test_already_queued(self, switch_is_active, delay):
        switch_is_active.return_value = True
        cache.set(settings.WIKI_REBUILD_TOKEN, True)
        schedule_rebuild_kb()
        assert cache.get(settings.WIKI_REBUILD_TOKEN)
        assert not delay.called

    @mock.patch.object(rebuild_kb, 'delay')
    @mock.patch.object(cache, 'get')
    @mock.patch.object(waffle, 'switch_is_active')
    def test_dont_queue(self, switch_is_active, get, delay):
        switch_is_active.return_value = False
        schedule_rebuild_kb()
        assert not get.called
        assert not delay.called

    @mock.patch.object(_rebuild_kb_chunk, 'apply_async')
    def test_rebuild_chunk(self, apply_async):
        cache.set(settings.WIKI_REBUILD_TOKEN, True)
        rebuild_kb()
        assert not cache.get(settings.WIKI_REBUILD_TOKEN)
        data = set((1, 2, 4, 5))
        assert 'args' in apply_async.call_args[1]
        eq_(data, set(apply_async.call_args[1]['args'][0]))


class ReviewMailTestCase(TestCaseBase):
    """Test that the review mail gets sent."""
    fixtures = ['users.json']

    def _approve_and_send(self, revision, reviewer, message):
        revision.reviewer = reviewer
        revision.reviewed = datetime.now()
        revision.is_approved = True
        revision.save()
        send_reviewed_notification(revision, revision.document, message)

    @mock.patch.object(Site.objects, 'get_current')
    def test_reviewed_notification(self, get_current):
        get_current.return_value.domain = 'testserver'

        rev = revision()
        doc = rev.document
        msg = 'great work!'
        self._approve_and_send(rev, User.objects.get(username='admin'), msg)

        # Two emails will be sent, one each for the reviewer and the reviewed.
        eq_(2, len(mail.outbox))
        eq_('Your revision has been approved: %s' % doc.title,
            mail.outbox[0].subject)
        eq_([rev.creator.email], mail.outbox[0].to)
        eq_(REVIEWED_EMAIL_CONTENT % (doc.title, msg, doc.slug),
            mail.outbox[0].body)

    @mock.patch.object(Site.objects, 'get_current')
    def test_reviewed_by_creator_no_notification(self, get_current):
        get_current.return_value.domain = 'testserver'

        rev = revision()
        msg = "great work!"
        self._approve_and_send(rev, rev.creator, msg)

        # Verify no email was sent
        eq_(0, len(mail.outbox))

    @mock.patch.object(Site.objects, 'get_current')
    def test_unicode_notifications(self, get_current):
        get_current.return_value.domain = 'testserver'

        rev = revision()
        doc = rev.document
        doc.title = u'Foo \xe8 incode'
        msg = 'foo'
        self._approve_and_send(rev, User.objects.get(username='admin'), msg)

        # Two emails will be sent, one each for the reviewer and the reviewed.
        eq_(2, len(mail.outbox))
        eq_('Your revision has been approved: %s' % doc.title,
            mail.outbox[0].subject)

    @mock.patch.object(Site.objects, 'get_current')
    def test_escaping(self, get_current):
        get_current.return_value.domain = 'testserver'

        rev = revision()
        doc = rev.document
        doc.title = '"All about quotes"'
        msg = 'foo & "bar"'
        self._approve_and_send(rev, User.objects.get(username='admin'), msg)

        # Two emails will be sent, one each for the reviewer and the reviewed.
        eq_(2, len(mail.outbox))
        eq_('Your revision has been approved: %s' % doc.title,
            mail.outbox[0].subject)
        assert '&quot;' not in mail.outbox[0].body
        assert '"All about quotes"' in mail.outbox[0].body
        assert 'foo & "bar"' in mail.outbox[0].body
