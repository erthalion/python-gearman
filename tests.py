import unittest, time

from gearman import GearmanClient, GearmanWorker
from gearman.connection import GearmanConnection
from gearman.task import Task

job_servers = ["127.0.0.1"]

class FailedError(Exception):
    pass

def echo(job):
    return job.arg

def fail(job):
    raise FailedError()

def sleep(job):
    time.sleep(float(job.arg))
    return job.arg

class TestConnection(unittest.TestCase):
    def setUp(self):
        self.connection = GearmanConnection(job_servers[0])
        self.connection.connect()

    def testNoArgs(self):
        self.connection.send_command_blocking("echo_req")
        cmd = self.connection.recv_blocking()
        self.failUnlessEqual(cmd[0], "echo_res")

    def testWithArgs(self):
        self.connection.send_command_blocking("submit_job", dict(func="echo", uniq="", arg="tea"))
        cmd = self.connection.recv_blocking()
        self.failUnlessEqual(cmd[0], 'job_created')

class TestGearman(unittest.TestCase):
    def _on_exception(self, func, exc):
        self.last_exception = (func, exc)

    def setUp(self):
        self.last_exception = (None, None)
        self.worker = GearmanWorker(job_servers, on_exception=self._on_exception)
        self.worker.register_function("echo", echo)
        self.worker.register_function("fail", fail)
        self.worker.register_function("sleep", sleep, timeout=1)
        import thread
        thread.start_new_thread(self.worker.work, tuple()) # TODO: Shouldn't use threads.. but we do for now (also, the thread is never terminated)
        self.client = GearmanClient(job_servers)

    def tearDown(self):
        del self.worker
        del self.client

    def testComplete(self):
        self.failUnlessEqual(self.client.do_task(Task("echo", "bar")), 'bar')

    def testFail(self):
        self.failUnlessRaises(Exception, lambda:self.client.do_task(Task("fail", "bar")))
        # self.failUnlessEqual(self.last_exception[0], "fail")

    def testTimeout(self):
        self.failUnlessEqual(self.client.do_task(Task("sleep", "0.1")), '0.1')
        self.failUnlessRaises(Exception, lambda:self.client.do_task(Task("sleep", "1.5")))

if __name__ == '__main__':
    unittest.main()
