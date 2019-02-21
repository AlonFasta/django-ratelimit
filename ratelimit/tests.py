from django.core.cache import cache, InvalidCacheBackendError
from django.core.exceptions import ImproperlyConfigured
from django.test import RequestFactory, TestCase
from django.test.utils import override_settings
from django.utils.decorators import method_decorator
from django.views.generic import View

from ratelimit.decorators import ratelimit
from ratelimit.exceptions import Ratelimited
from ratelimit.utils import is_ratelimited, _split_rate


rf = RequestFactory()


class MockUser(object):
    def __init__(self, authenticated=False):
        self.pk = 1
        self.is_authenticated = authenticated


class RateParsingTests(TestCase):
    def test_simple(self):
        tests = (
            ('100/s', (100, 1)),
            ('100/10s', (100, 10)),
            ('100/10', (100, 10)),
            ('100/m', (100, 60)),
            ('400/10m', (400, 600)),
            ('1000/h', (1000, 3600)),
            ('800/d', (800, 24 * 60 * 60)),
        )

        for i, o in tests:
            assert o == _split_rate(i)


def mykey(group, request):
    return request.META['REMOTE_ADDR'][::-1]


class RatelimitTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_no_key(self):
        @ratelimit(rate='1/m', block=True)
        def view(request):
            return True

        req = rf.get('/')
        with self.assertRaises(ImproperlyConfigured):
            view(req)

    def test_ip(self):
        @ratelimit(key='ip', rate='1/m')
        def view(request):
            return request.limited

        assert not view(rf.get('/')), 'First request works.'
        assert view(rf.get('/')), 'Second request is limited'

    def test_block(self):
        @ratelimit(key='ip', rate='1/m', block=True)
        def blocked(request):
            return request.limited

        assert not blocked(rf.get('/')), 'First request works.'
        with self.assertRaises(Ratelimited):
            blocked(rf.get('/')), 'Second request is blocked.'

    def test_method(self):
        @ratelimit(key='ip', method='POST', rate='1/m', group='a')
        def limit_post(request):
            return request.limited

        assert not limit_post(rf.post('/')), 'Do not limit first POST.'
        assert limit_post(rf.post('/')), 'Limit second POST.'
        assert not limit_post(rf.get('/')), 'Do not limit GET.'

    def test_unsafe_methods(self):
        @ratelimit(key='ip', method=ratelimit.UNSAFE, rate='0/m')
        def limit_unsafe(request):
            return request.limited

        assert not limit_unsafe(rf.get('/'))
        assert not limit_unsafe(rf.head('/'))
        assert not limit_unsafe(rf.options('/'))
        assert limit_unsafe(rf.delete('/'))
        assert limit_unsafe(rf.post('/'))
        assert limit_unsafe(rf.put('/'))
        assert limit_unsafe(rf.patch('/'))

    def test_key_get(self):
        @ratelimit(key='get:foo', rate='1/m', method='GET')
        def view(request):
            return request.limited

        assert not view(rf.get('/', {'foo': 'a'}))
        assert view(rf.get('/', {'foo': 'a'}))
        assert not view(rf.get('/', {'foo': 'b'}))
        assert view(rf.get('/', {'foo': 'b'}))

    def test_key_post(self):
        @ratelimit(key='post:foo', rate='1/m')
        def view(request):
            return request.limited

        assert not view(rf.post('/', {'foo': 'a'}))
        assert view(rf.post('/', {'foo': 'a'}))
        assert not view(rf.post('/', {'foo': 'b'}))
        assert view(rf.post('/', {'foo': 'b'}))

    def test_key_header(self):
        def _req():
            req = rf.post('/')
            req.META['HTTP_X_REAL_IP'] = '1.2.3.4'
            return req

        @ratelimit(key='header:x-real-ip', rate='1/m')
        @ratelimit(key='header:x-missing-header', rate='1/m')
        def view(request):
            return request.limited

        assert not view(_req())
        assert view(_req())

    def test_rate(self):
        @ratelimit(key='ip', rate='2/m')
        def twice(request):
            return request.limited

        assert not twice(rf.post('/')), 'First request is not limited.'
        assert not twice(rf.post('/')), 'Second request is not limited.'
        assert twice(rf.post('/')), 'Third request is limited.'

    def test_zero_rate(self):
        @ratelimit(key='ip', rate='0/m')
        def never(request):
            return request.limited

        assert never(rf.post('/'))

    def test_none_rate(self):
        @ratelimit(key='ip', rate=None)
        def always(request):
            return request.limited

        assert not always(rf.post('/'))
        assert not always(rf.post('/'))
        assert not always(rf.post('/'))
        assert not always(rf.post('/'))
        assert not always(rf.post('/'))
        assert not always(rf.post('/'))
        assert not always(rf.post('/'))

    def test_callable_rate(self):
        def _req(auth):
            req = rf.post('/')
            req.user = MockUser(authenticated=auth)
            return req

        def get_rate(group, request):
            if request.user.is_authenticated:
                return (2, 60)
            return (1, 60)

        @ratelimit(key='user_or_ip', rate=get_rate)
        def view(request):
            return request.limited

        assert not view(_req(auth=False))
        assert view(_req(auth=False))
        assert not view(_req(auth=True))
        assert not view(_req(auth=True))
        assert view(_req(auth=True))

    def test_callable_rate_none(self):
        def _req(never_limit=False):
            req = rf.post('/')
            req.never_limit = never_limit
            return req

        get_rate = lambda g, r: None if r.never_limit else '1/m'

        @ratelimit(key='ip', rate=get_rate)
        def view(request):
            return request.limited

        assert not view(_req())
        assert view(_req())
        assert not view(_req(never_limit=True))
        assert not view(_req(never_limit=True))

    def test_callable_rate_zero(self):
        def _req(auth):
            req = rf.post('/')
            req.user = MockUser(authenticated=auth)
            return req

        def get_rate(group, request):
            if request.user.is_authenticated:
                return '1/m'
            return '0/m'

        @ratelimit(key='ip', rate=get_rate)
        def view(request):
            return request.limited

        assert view(_req(auth=False))
        assert not view(_req(auth=True))
        assert view(_req(auth=True))

    @override_settings(RATELIMIT_USE_CACHE='fake-cache')
    def test_bad_cache(self):
        """The RATELIMIT_USE_CACHE setting works if the cache exists."""

        @ratelimit(key='ip', rate='1/m')
        def view(request):
            return request

        with self.assertRaises(InvalidCacheBackendError):
            view(rf.post('/'))

    @override_settings(RATELIMIT_USE_CACHE='connection-errors')
    def test_cache_connection_error(self):

        @ratelimit(key='ip', rate='1/m')
        def view(request):
            return request

        assert view(rf.post('/'))

    def test_user_or_ip(self):
        """Allow custom functions to set cache keys."""

        def _req(auth):
            req = rf.post('/')
            req.user = MockUser(authenticated=auth)
            return req

        @ratelimit(key='user_or_ip', rate='1/m', block=False)
        def view(request):
            return request.limited

        assert not view(_req(auth=False))
        assert view(_req(auth=False))

        auth = rf.post('/')
        auth.user = MockUser(authenticated=True)

        assert not view(_req(auth=True))
        assert view(_req(auth=True))

    def test_key_path(self):
        @ratelimit(key='ratelimit.tests.mykey', rate='1/m')
        def view(request):
            return request.limited

        assert not view(rf.post('/'))
        assert view(rf.post('/'))

    def test_callable_key(self):
        @ratelimit(key=mykey, rate='1/m')
        def view(request):
            return request.limited

        assert not view(rf.post('/'))
        assert view(rf.post('/'))

    def test_stacked_decorator(self):
        """Allow @ratelimit to be stacked."""
        # Put the shorter one first and make sure the second one doesn't
        # reset request.limited back to False.
        @ratelimit(rate='1/m', block=False, key=lambda x, y: 'min')
        @ratelimit(rate='10/d', block=False, key=lambda x, y: 'day')
        def view(request):
            return request.limited

        assert not view(rf.post('/'))
        assert view(rf.post('/'))

    def test_stacked_methods(self):
        """Different methods should result in different counts."""
        @ratelimit(rate='1/m', key='ip', method='GET')
        @ratelimit(rate='1/m', key='ip', method='POST')
        def view(request):
            return request.limited

        assert not view(rf.get('/'))
        assert not view(rf.post('/'))
        assert view(rf.get('/'))
        assert view(rf.post('/'))

    def test_sorted_methods(self):
        """Order of the methods shouldn't matter."""
        @ratelimit(rate='1/m', key='ip', method=['GET', 'POST'], group='a')
        def get_post(request):
            return request.limited

        @ratelimit(rate='1/m', key='ip', method=['POST', 'GET'], group='a')
        def post_get(request):
            return request.limited

        assert not get_post(rf.get('/'))
        assert post_get(rf.get('/'))

    def test_is_ratelimited(self):
        def get_key(group, request):
            return 'test_is_ratelimited_key'

        def not_increment(request):
            return is_ratelimited(request, increment=False,
                                  method=is_ratelimited.ALL, key=get_key,
                                  rate='1/m', group='a')

        def do_increment(request):
            return is_ratelimited(request, increment=True,
                                  method=is_ratelimited.ALL, key=get_key,
                                  rate='1/m', group='a')

        # Does not increment. Count still 0. Does not rate limit
        # because 0 < 1.
        assert not not_increment(rf.get('/'))

        # Increments. Does not rate limit because 0 < 1. Count now 1.
        assert not do_increment(rf.get('/'))

        # Does not increment. Count still 1. Not limited because 1 > 1
        # is false.
        assert not not_increment(rf.get('/'))

        # Count = 2, 2 > 1.
        assert do_increment(rf.get('/'))
        assert not_increment(rf.get('/'))

    @override_settings(RATELIMIT_USE_CACHE='connection-errors')
    def test_is_ratelimited_cache_connection_error_without_increment(self):
        def get_key(group, request):
            return 'test_is_ratelimited_key'

        def not_increment(request):
            return is_ratelimited(request, increment=False,
                                  method=is_ratelimited.ALL, key=get_key,
                                  rate='1/m', group='a')

        assert not not_increment(rf.get('/'))
        assert not not_increment(rf.get('/'))

    @override_settings(RATELIMIT_USE_CACHE='connection-errors')
    def test_is_ratelimited_cache_connection_error_with_increment(self):
        def get_key(group, request):
            return 'test_is_ratelimited_key'

        def do_increment(request):
            return is_ratelimited(request, increment=True,
                                  method=is_ratelimited.ALL, key=get_key,
                                  rate='1/m', group='a')

        assert not do_increment(rf.get('/'))
        assert not do_increment(rf.get('/'))

    @override_settings(RATELIMIT_USE_CACHE='connection-errors-redis')
    def test_is_ratelimited_cache_connection_error_with_increment_redis(self):
        def get_key(group, request):
            return 'test_is_ratelimited_key'

        def do_increment(request):
            return is_ratelimited(request, increment=True,
                                  method=is_ratelimited.ALL, key=get_key,
                                  rate='1/m', group='a')

        assert do_increment(rf.get('/'))
        assert do_increment(rf.get('/'))

    @override_settings(RATELIMIT_USE_CACHE='instant-expiration')
    def test_cache_timeout(self):
        @ratelimit(key='ip', rate='1/m', block=True)
        def view(request):
            return True

        assert view(rf.get('/'))
        assert view(rf.get('/'))


class RatelimitCBVTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_method_decorator(self):
        class TestView(View):
            @method_decorator(ratelimit(key='ip', rate='1/m', block=False))
            def post(self, request):
                return request.limited

        view = TestView.as_view()

        assert not view(rf.post('/'))
        assert view(rf.post('/'))

    def test_class_decorator(self):
        @method_decorator(ratelimit(key='ip', rate='1/m', block=False),
                          name='get')
        class TestView(View):
            def get(self, request):
                return request.limited

        view = TestView.as_view()

        assert not view(rf.get('/'))
        assert view(rf.get('/'))

    def test_wrap_view(self):
        class TestView(View):
            def get(self, request):
                return request.limited

        view = TestView.as_view()
        wrapped = ratelimit(key='ip', rate='1/m', block=False)(view)

        assert not wrapped(rf.get('/'))
        assert wrapped(rf.get('/'))

    def test_methods_counted_separately(self):
        class TestView(View):
            @method_decorator(ratelimit(key='ip', rate='1/m', method='GET'))
            def get(self, request):
                return request.limited

            @method_decorator(ratelimit(key='ip', rate='1/m', method='POST'))
            def post(self, request):
                return request.limited

        view = TestView.as_view()

        assert not view(rf.get('/'))
        assert view(rf.get('/'))
        assert not view(rf.post('/'))

    def test_views_counted_separately(self):
        class TestView(View):
            @method_decorator(ratelimit(key='ip', rate='1/m', method='GET'))
            def get(self, request):
                return request.limited

        class AnotherTestView(View):
            @method_decorator(ratelimit(key='ip', rate='1/m', method='GET'))
            def get(self, request):
                return request.limited

        test_view = TestView.as_view()
        another_view = AnotherTestView.as_view()

        assert not test_view(rf.get('/'))
        assert test_view(rf.get('/'))
        assert not another_view(rf.get('/'))
