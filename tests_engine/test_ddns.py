import unittest
from unittest.mock import Mock, patch

from keasync.ddns import DdnsManager


class TestDdnsManagerAuth(unittest.TestCase):

    def test_01_auth_tuple_from_credentials(self):
        d = DdnsManager('http://nb', 'tok', 'http://d2:8001/',
                        username='syncer', password='s3cret')
        self.assertEqual(d.auth, ('syncer', 's3cret'))

    def test_02_no_auth_by_default(self):
        d = DdnsManager('http://nb', 'tok', 'http://d2:8001/')
        self.assertIsNone(d.auth)

    @patch('keasync.ddns.requests.post')
    def test_03_d2_command_sends_auth(self, post):
        resp = Mock()
        resp.raise_for_status = Mock()
        resp.json.return_value = [{'result': 0, 'text': 'ok'}]
        post.return_value = resp
        d = DdnsManager('http://nb', 'tok', 'http://d2:8001/',
                        username='syncer', password='s3cret')
        d._d2('config-get')
        self.assertEqual(post.call_args.kwargs['auth'], ('syncer', 's3cret'))
