import base64
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

from captcha import recognizer


FIXTURE_PATH = Path(__file__).resolve().parent / 'fixtures' / 'xserver_captcha_sample.png'


def fixture_data_url() -> str:
    encoded = base64.b64encode(FIXTURE_PATH.read_bytes()).decode('ascii')
    return f'data:image/png;base64,{encoded}'


class CaptchaRecognizerValidationTests(unittest.TestCase):
    def test_empty_src_has_clear_error(self):
        with self.assertRaisesRegex(recognizer.CaptchaRecognitionError, 'src is empty'):
            recognizer.recognize_captcha('')

    def test_malformed_base64_has_clear_error(self):
        with self.assertRaisesRegex(recognizer.CaptchaRecognitionError, 'base64 decode failed'):
            recognizer.recognize_captcha('data:image/png;base64,not-valid-***')

    def test_missing_model_has_clear_error(self):
        missing_model = Path(__file__).resolve().parent / 'fixtures' / 'missing.keras'
        with (
            patch.object(recognizer, '_model', None),
            self.assertRaisesRegex(recognizer.CaptchaRecognitionError, 'model file does not exist'),
        ):
            recognizer.get_model(missing_model)

    def test_model_is_loaded_only_once(self):
        model = object()

        class Models:
            def __init__(self):
                self.load_count = 0

            def load_model(self, path, compile=False):
                self.load_count += 1
                return model

        models = Models()
        fake_tf = type('FakeTensorFlow', (), {
            'keras': type('FakeKeras', (), {'models': models})(),
        })()
        with (
            patch.object(recognizer, '_model', None),
            patch.object(recognizer, '_tensorflow', return_value=fake_tf),
        ):
            self.assertIs(recognizer.get_model(), model)
            self.assertIs(recognizer.get_model(), model)

        self.assertEqual(models.load_count, 1)


@unittest.skipUnless(importlib.util.find_spec('tensorflow'), 'TensorFlow is not installed')
class CaptchaRecognizerIntegrationTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        recognizer._model = None

    def test_reference_sample_runs_full_pipeline_and_returns_digits(self):
        code = recognizer.recognize_captcha(fixture_data_url())

        self.assertIsInstance(code, str)
        self.assertTrue(code.isdigit())

    def test_repeated_recognition_loads_model_only_once(self):
        tf = recognizer._tensorflow()
        original_load_model = tf.keras.models.load_model
        load_count = 0

        def counting_load_model(*args, **kwargs):
            nonlocal load_count
            load_count += 1
            return original_load_model(*args, **kwargs)

        recognizer._model = None
        with patch.object(tf.keras.models, 'load_model', side_effect=counting_load_model):
            first_code = recognizer.recognize_captcha(fixture_data_url())
            second_code = recognizer.recognize_captcha(fixture_data_url())

        self.assertTrue(first_code.isdigit())
        self.assertTrue(second_code.isdigit())
        self.assertEqual(load_count, 1)

    def test_non_image_content_has_clear_error(self):
        payload = base64.b64encode(b'not an image').decode('ascii')
        with self.assertRaisesRegex(recognizer.CaptchaRecognitionError, 'image decode failed'):
            recognizer.recognize_captcha(f'data:image/png;base64,{payload}')


if __name__ == '__main__':
    unittest.main()
