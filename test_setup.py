import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import setup_bot


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.folder=Path(self.temp.name)
        self.location=patch.object(setup_bot,'__file__',str(self.folder/'setup_bot.py'))
        self.location.start()

    def tearDown(self):
        self.location.stop()
        self.temp.cleanup()

    def test_owner_and_token_are_saved_privately(self):
        with patch('builtins.input',return_value='123456789'), patch('getpass.getpass',return_value='123456:example-not-real-token'), patch('builtins.print'):
            setup_bot.main()
        path=self.folder/'bot-data/config.json'
        config=json.loads(path.read_text())
        self.assertEqual(config['allowed_user_id'],123456789)
        self.assertEqual(config['token'],'123456:example-not-real-token')
        self.assertEqual(path.stat().st_mode & 0o777,0o600)

    def test_invalid_owner_saves_nothing(self):
        for owner in ('','-1','someone','0','１２３'):
            with patch('builtins.input',return_value=owner), self.assertRaises(SystemExit):
                setup_bot.main()
            self.assertFalse((self.folder/'bot-data/config.json').exists())

    def test_invalid_token_saves_nothing(self):
        with patch('builtins.input',return_value='123456789'), patch('getpass.getpass',return_value='invalid'), self.assertRaises(SystemExit):
            setup_bot.main()
        self.assertFalse((self.folder/'bot-data/config.json').exists())

    def test_existing_configuration_is_preserved(self):
        path=self.folder/'bot-data/config.json'
        path.parent.mkdir()
        path.write_text('existing-config')
        with patch('builtins.input') as prompt, self.assertRaises(SystemExit):
            setup_bot.main()
        prompt.assert_not_called()
        self.assertEqual(path.read_text(),'existing-config')


if __name__=='__main__':
    unittest.main()
