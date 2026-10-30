"""No-network regressions for denied speech and safe public errors."""
import asyncio,sys,types,unittest
from unittest.mock import patch
from audio_providers import EdgeProvider,settings,speech_error_message
class Denied(Exception):
    status=403
class EdgeErrors(unittest.TestCase):
    def test_denied_handshake_does_not_leak_url(self):
        error=Denied("403, message='Invalid response status', url=wss://service.test/?token=secret")
        text=speech_error_message(error)
        self.assertIn('HTTP 403',text);self.assertNotIn('secret',text);self.assertNotIn('wss:',text)
        self.assertEqual(text,speech_error_message(str(error)))
    def test_other_status_and_url_errors_are_sanitized(self):
        self.assertIn('HTTP 429',speech_error_message("429, message='throttled', url=wss://host/?secret=token"))
        self.assertNotIn('token',speech_error_message('Connection lost at https://host/path?secret=token'))
    def test_provider_surfaces_rejection_without_succeeding(self):
        class Communicate:
            def __init__(self,*args,**kwargs):pass
            async def save(self,path,metadata=None):raise Denied("403, message='Invalid response status', url=wss://host/?token=secret")
        with patch.dict(sys.modules,{'edge_tts':types.SimpleNamespace(Communicate=Communicate)}), patch('audio_providers.configure_edge_endpoint'):
            with self.assertRaisesRegex(RuntimeError,'Microsoft rejected online speech') as caught:
                EdgeProvider().synthesize('New text',settings(),'unused.mp3')
            self.assertNotIn('secret',str(caught.exception))

    def test_resource_safety_interrupt_is_not_wrapped_as_service_failure(self):
        from resource_guard import ResourcePressure
        class Communicate:
            def __init__(self,*args,**kwargs):pass
            async def save(self,path,metadata=None):raise ResourcePressure('Unsafe I/O: stop')
        with patch.dict(sys.modules,{'edge_tts':types.SimpleNamespace(Communicate=Communicate)}), patch('audio_providers.configure_edge_endpoint'):
            with self.assertRaises(ResourcePressure):
                EdgeProvider().synthesize('New text',settings(),'unused.mp3')

    def test_endpoint_adapter_changes_host_and_parameter_idempotently(self):
        from edge_connection import compatible_url,LEGACY_ENDPOINT,EDGE_ENDPOINT
        from urllib.parse import urlsplit,parse_qs
        result=compatible_url(LEGACY_ENDPOINT+'?TrustedClientToken=test','test')
        self.assertEqual(urlsplit(result).netloc,'api.msedgeservices.com')
        self.assertEqual(parse_qs(urlsplit(result).query),{'Ocp-Apim-Subscription-Key':['test']})
        self.assertEqual(compatible_url(result,'test'),result)
        self.assertRaises(RuntimeError,compatible_url,'wss://untrusted.example/','test')
