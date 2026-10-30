"""Timeouts do not require real DNS, TLS endpoints, or untrusted source access."""
import socket
import threading
import time
import unittest
from unittest.mock import patch
from safe_http import resolve_bounded
from chapter_updates import source_error


class SourceDeadlineTests(unittest.TestCase):
    def test_stalled_dns_returns_within_budget(self):
        release=threading.Event()
        def stalled(*args,**kwargs):
            release.wait(2)
            return [(socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.8.8',443))]
        start=time.monotonic()
        try:
            with patch('safe_http.socket.getaddrinfo',side_effect=stalled):
                with self.assertRaisesRegex(TimeoutError,'DNS lookup timed out'):
                    resolve_bounded('public.example',443,timeout=.02)
        finally:release.set()
        self.assertLess(time.monotonic()-start,.5)

    def test_errors_are_actionable_without_remote_details(self):
        for error,expected in [(TimeoutError('https://secret.example/token'),'timed out'),(ValueError('Could not resolve the remote hostname.'),'hostname'),(ValueError('Source returned HTTP 403 https://secret.example'),'blocked')]:
            message=source_error(error)
            self.assertIn(expected,message)
            self.assertNotIn('secret.example',message)
            self.assertIn('preserved',message)
