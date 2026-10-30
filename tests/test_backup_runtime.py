import socket
import tempfile
import unittest
from pathlib import Path
from preservation import backup_tree, verify_backup


class BackupRuntimeTests(unittest.TestCase):
    def test_worker_notification_socket_is_excluded_but_persistent_files_verify(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'source';source.mkdir()
            (source/'book.txt').write_text('Recoverable synthetic chapter.')
            with socket.socket(socket.AF_UNIX) as notification:
                notification.bind(str(source/'.platform-wake.sock'))
                result=backup_tree(source,root/'backup',monitor=False,bytes_per_second=0)
            self.assertTrue(result['complete'])
            self.assertEqual(result['excluded_runtime'],[{'path':'.platform-wake.sock','kind':'unix_socket'}])
            self.assertEqual(verify_backup(root/'backup',monitor=False,bytes_per_second=0),[])

    def test_unknown_special_file_still_blocks_verified_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'source';source.mkdir()
            with socket.socket(socket.AF_UNIX) as notification:
                notification.bind(str(source/'unknown.sock'))
                result=backup_tree(source,root/'backup',monitor=False,bytes_per_second=0)
            self.assertFalse(result['complete'])
            self.assertEqual(len(result['errors']),1)
