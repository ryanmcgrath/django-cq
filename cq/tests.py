from unittest.mock import patch
from unittest import skip
from datetime import datetime

from django.test import TestCase, override_settings
from django.utils import timezone
from django.conf import settings
try:
    from django.urls import reverse
except ImportError:
    from django.core.urlresolvers import reverse
from django.contrib.auth import get_user_model
from channels.tests import (
    TransactionChannelTestCase, ChannelTestCase
)
from channels.tests.base import ChannelTestCaseMixin
from channels import Channel
from channels.asgi import channel_layers
from redis import Redis
from croniter import croniter
from rest_framework.test import APITransactionTestCase, APIRequestFactory, force_authenticate

from .models import Task, RepeatingTask
from .decorators import task
from .consumers import run_task
from .views import TaskViewSet
from .backends import backend


User = get_user_model()


@task('a')
def task_a(task):
    return 'a'


@task
def task_b(task):
    raise Exception('b')
    return 'b'


@task
def task_c(task):
    sub = task.subtask(task_a)
    return sub


@task
def task_d(task):
    sub = task.subtask(task_a)
    return 'd'


@task
def task_e(task):
    sub = task.subtask(task_c)
    return sub


@task
def task_f(task):
    sub = task.subtask(task_b)
    return sub


@task
def task_g(task):
    sub = task.subtask(task_f)
    return sub


@task
def task_h(task):
    return task_i.delay().chain(task_j, (2,))


@task
def task_i(task):
    return 3


@task('j')
def task_j(task, a, b):
    return a + b


def errback(task, error):
    pass


@task('k')
def task_k(task, error=False):
    task.errorback(errback)
    if error:
        raise Exception


@override_settings(CQ_SERIAL=False)
class DecoratorTestCase(TransactionChannelTestCase):
    def test_adds_delay_function(self):
        self.assertTrue(hasattr(task_a, 'delay'))
        self.assertIsNot(task_a.delay, None)

    def test_task_is_still_a_function(self):
        self.assertEqual(task_a(), 'a')
        self.assertEqual(Task.objects.all().count(), 0)

    @patch('cq.models.Task.submit')
    def test_delay_creates_task(self, submit):
        before = timezone.now()
        task = task_a.delay()
        after = timezone.now()
        self.assertIsNot(task, None)
        self.assertGreater(len(str(task.id)), 10)
        self.assertGreater(task.submitted, before)
        self.assertLess(task.submitted, after)


@override_settings(CQ_SERIAL=False)
class TaskSuccessTestCase(TransactionChannelTestCase):
    def test_something(self):
        task = task_a.delay()
        run_task(self.get_next_message('cq-tasks', require=True))
        task.wait()
        self.assertEqual(task.status, task.STATUS_SUCCESS)


@override_settings(CQ_SERIAL=False)
class TaskFailureTestCase(TransactionChannelTestCase):
    def test_something(self):
        task = task_b.delay()
        run_task(self.get_next_message('cq-tasks', require=True))
        task.wait()
        self.assertEqual(task.status, task.STATUS_FAILURE)
        self.assertIsNot(task.error, None)

    def test_errorbacks(self):
        task = task_k.delay(error=True)
        run_task(self.get_next_message('cq-tasks', require=True))
        task.wait()


class DBLatencyTestCase(TestCase):
    pass


@override_settings(CQ_SERIAL=False)
class AsyncSubtaskTestCase(TransactionChannelTestCase):
    def test_returns_own_result(self):
        task = task_d.delay()
        run_task(self.get_next_message('cq-tasks', require=True))
        run_task(self.get_next_message('cq-tasks', require=True))
        task.wait()
        self.assertEqual(task.status, task.STATUS_SUCCESS)
        self.assertEqual(task.result, 'd')

    def test_returns_subtask_result(self):
        task = task_c.delay()
        run_task(self.get_next_message('cq-tasks', require=True))
        run_task(self.get_next_message('cq-tasks', require=True))
        task.wait()
        self.assertEqual(task.status, task.STATUS_SUCCESS)
        self.assertEqual(task.result, 'a')

    def test_returns_subsubtask_result(self):
        task = task_e.delay()
        run_task(self.get_next_message('cq-tasks', require=True))
        run_task(self.get_next_message('cq-tasks', require=True))
        run_task(self.get_next_message('cq-tasks', require=True))
        task.wait()
        self.assertEqual(task.status, task.STATUS_SUCCESS)
        self.assertEqual(task.result, 'a')

    def test_parent_tasks_enter_waiting_state(self):
        task = task_e.delay()
        run_task(self.get_next_message('cq-tasks', require=True))
        run_task(self.get_next_message('cq-tasks', require=True))
        task.wait(500)
        task.refresh_from_db()
        self.assertEqual(task.status, task.STATUS_WAITING)
        subtask = task.subtasks.first()
        self.assertEqual(subtask.status, Task.STATUS_WAITING)
        subsubtask = subtask.subtasks.first()
        self.assertEqual(subsubtask.status, Task.STATUS_QUEUED)

    def test_returns_subtask_error(self):
        task = task_f.delay()
        run_task(self.get_next_message('cq-tasks', require=True))
        run_task(self.get_next_message('cq-tasks', require=True))
        task.wait()
        self.assertEqual(task.status, task.STATUS_INCOMPLETE)
        self.assertEqual(task.result, None)
        self.assertIsNot(task.error, None)

    def test_returns_subsubtask_error(self):
        task = task_g.delay()
        run_task(self.get_next_message('cq-tasks', require=True))
        run_task(self.get_next_message('cq-tasks', require=True))
        run_task(self.get_next_message('cq-tasks', require=True))
        task.wait()
        self.assertEqual(task.status, task.STATUS_INCOMPLETE)
        self.assertEqual(task.result, None)
        self.assertIsNot(task.error, None)


class SerialSubtaskTestCase(TransactionChannelTestCase):
    def test_returns_own_result(self):
        result = task_d()
        self.assertEqual(result, 'd')

    def test_returns_subtask_result(self):
        result = task_c()
        self.assertEqual(result, 'a')

    def test_returns_subsubtask_result(self):
        result = task_e()
        self.assertEqual(result, 'a')


@override_settings(CQ_SERIAL=False)
class AsyncChainedTaskTestCase(TransactionChannelTestCase):
    def test_all(self):
        task = task_h.delay()
        run_task(self.get_next_message('cq-tasks', require=True))
        run_task(self.get_next_message('cq-tasks', require=True))
        run_task(self.get_next_message('cq-tasks', require=True))
        task.wait()
        self.assertEqual(task.status, task.STATUS_SUCCESS)
        self.assertEqual(task.result, 5)


class GetQueuedTasksTestCase(TestCase):
    def test_returns_empty(self):
        task_ids = backend.get_queued_tasks()
        self.assertEqual(task_ids, {})

    @skip('Need worker disabled.')
    def test_queued(self):
        chan = Channel('cq-tasks')
        chan.send({'task_id': 'one'})
        chan.send({'task_id': 'two'})
        task_ids = get_queued_tasks()
        self.assertEqual(task_ids, {'one', 'two'})
        cl = chan.channel_layer
        while len(task_ids):
            msg = cl.receive_many(['cq-tasks'], block=True)
            task_ids.remove(msg[1]['task_id'])


# class GetRunningTasksTestCase(TestCase):
#     def test_empty_list(self):
#         task_ids = get_running_tasks()
#         self.assertEqual(task_ids, set())

#     def test_running(self):
#         conn = Redis.from_url(settings.REDIS_URL)
#         conn.lpush('cq-current', 'one')
#         conn.lpush('cq-current', 'two')
#         task_ids = get_running_tasks()
#         self.assertEqual(task_ids, {'one', 'two'})
#         task_ids = get_running_tasks()
#         self.assertEqual(task_ids, set())


class PublishCurrentTestCase(TestCase):
    def test_publish(self):
        backend.clear_current()
        backend.set_current_task('hello')
        backend.publish_current(max_its=2, sleep_time=0.1)
        backend.set_current_task('world')
        backend.publish_current(max_its=3, sleep_time=0.1)
        task_ids = backend.get_running_tasks()
        self.assertEqual(task_ids, {'hello', 'world'})
        task_ids = backend.get_running_tasks()
        self.assertEqual(task_ids, set())


class CreateRepeatingTaskTestCase(TestCase):
    def test_create(self):
        rt = RepeatingTask.objects.create(func_name='cq.tests.task_a')
        next = croniter(rt.crontab, timezone.now()).get_next(datetime)
        self.assertEqual(rt.next_run, next)


class RunRepeatingTaskTestCase(TransactionChannelTestCase):
    def test_run(self):
        rt = RepeatingTask.objects.create(func_name='cq.tests.task_a')
        task = rt.submit()
        self.assertLess(rt.last_run, timezone.now())
        self.assertGreater(rt.next_run, timezone.now())
        run_task(self.get_next_message('cq-tasks', require=True))
        task.wait()
        self.assertEqual(task.result, 'a')


class ViewTestCase(ChannelTestCaseMixin, APITransactionTestCase):
    def setUp(self):
        try:
            self.user = User.objects.create(
                username='a', email='a@a.org', password='a'
            )
        except:
            self.user = User.objects.create(
                email='a@a.org', password='a'
            )

    def test_create_and_get_task(self):

        # If the views aren't available, don't test.
        try:
            reverse('cqtask-list')
        except:
            return

        # Check task creation.
        data = {
            'task': 'j',
            'args': [2, 3]
        }
        self.client.force_authenticate(self.user)
        response = self.client.post(reverse('cqtask-list'), data, format='json')
        self.assertEqual(response.status_code, 201)
        self.assertNotEqual(response.json().get('id', None), None)

        # Then retreival.
        id = response.json()['id']
        response = self.client.get(reverse('cqtask-detail', kwargs={'pk': id}), data, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'Q')
        run_task(self.get_next_message('cq-tasks', require=True))
        response = self.client.get(reverse('cqtask-detail', kwargs={'pk': id}), data, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'S')
