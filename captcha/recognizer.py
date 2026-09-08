from __future__ import annotations

import base64
import binascii
import logging
import threading
from pathlib import Path
from typing import Any


MODEL_PATH = Path(__file__).resolve().parent / 'xserver_captcha.keras'


class CaptchaRecognitionError(RuntimeError):
    """Raised when local XServer captcha recognition cannot be completed."""


_model: Any = None
_model_lock = threading.Lock()
_inference_lock = threading.Lock()


def _tensorflow():
    try:
        import tensorflow as tf
    except ImportError as exc:
        raise CaptchaRecognitionError(
            'TensorFlow is not installed; install the project requirements before local captcha OCR.'
        ) from exc
    return tf


def get_model(model_path: Path | None = None):
    """Load the Keras model once and reuse it for subsequent recognitions."""
    global _model

    if _model is not None:
        return _model

    resolved_path = model_path or MODEL_PATH
    if not resolved_path.is_file():
        raise CaptchaRecognitionError(f'Local captcha model file does not exist: {resolved_path}')

    with _model_lock:
        if _model is not None:
            return _model
        try:
            _model = _tensorflow().keras.models.load_model(resolved_path, compile=False)
        except Exception as exc:
            raise CaptchaRecognitionError(
                f'Failed to load local captcha model from {resolved_path}: {exc}'
            ) from exc
        logging.info('Loaded local XServer captcha model: %s', resolved_path)
        return _model


def recognize_captcha(image_src: str) -> str:
    """Recognize an XServer captcha data URL using the bundled Keras model.

    Preprocessing and greedy CTC decoding intentionally match the reference
    captcha-cloudrun Python implementation.
    """
    if not isinstance(image_src, str) or not image_src.strip():
        raise CaptchaRecognitionError('Captcha image src is empty.')

    encoded_image = image_src.rsplit(',', 1)[-1].strip()
    if not encoded_image:
        raise CaptchaRecognitionError('Captcha image src contains no base64 payload.')

    try:
        base64.b64decode(encoded_image, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CaptchaRecognitionError(f'Captcha image base64 decode failed: {exc}') from exc

    tf = _tensorflow()
    try:
        # tf.io.decode_base64 expects the web-safe alphabet. This translation
        # is identical to the reference service implementation.
        websafe_image = encoded_image.translate(str.maketrans({'+': '-', '/': '_'}))
        image_bytes = tf.io.decode_base64(websafe_image)
    except Exception as exc:
        raise CaptchaRecognitionError(f'Captcha image base64 decode failed: {exc}') from exc

    try:
        image = tf.image.decode_png(image_bytes, channels=3)
    except Exception as exc:
        raise CaptchaRecognitionError(f'Captcha image decode failed: {exc}') from exc

    try:
        image = tf.image.resize(image, [60, 300]) / 255.0
        batch = tf.expand_dims(image, 0)
        model = get_model()
        with _inference_lock:
            predictions = model(batch)
        input_length = tf.fill([tf.shape(predictions)[0]], tf.shape(predictions)[1])
        decoded = tf.keras.backend.ctc_decode(
            predictions,
            input_length=input_length,
            greedy=True,
        )[0][0]
        code = ''.join(str(value) for value in decoded.numpy()[0] if value >= 0)
    except CaptchaRecognitionError:
        raise
    except Exception as exc:
        raise CaptchaRecognitionError(f'Local captcha inference failed: {exc}') from exc

    if not code:
        raise CaptchaRecognitionError('Local captcha decode returned an empty result.')
    if not code.isdigit():
        raise CaptchaRecognitionError(f'Local captcha decode returned a non-numeric result: {code!r}')

    return code
