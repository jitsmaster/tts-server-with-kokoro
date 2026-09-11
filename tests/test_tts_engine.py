import threading
import time
from unittest.mock import patch

from app import tts_engine


def _reset_pipelines():
    tts_engine._pipelines.clear()


def test_warm_up_constructs_pipeline_for_every_language():
    _reset_pipelines()
    with patch("app.tts_engine.KPipeline") as mock_pipeline:
        tts_engine.warm_up()
    assert set(tts_engine._pipelines.keys()) == set(tts_engine._LANG_CODES.keys())
    assert mock_pipeline.call_count == len(tts_engine._LANG_CODES)


def test_warm_up_is_idempotent():
    _reset_pipelines()
    with patch("app.tts_engine.KPipeline") as mock_pipeline:
        tts_engine.warm_up()
        tts_engine.warm_up()
    assert mock_pipeline.call_count == len(tts_engine._LANG_CODES)


def test_get_pipeline_is_thread_safe_for_same_language():
    _reset_pipelines()

    def slow_pipeline(*args, **kwargs):
        time.sleep(0.05)
        return object()

    with patch("app.tts_engine.KPipeline", side_effect=slow_pipeline) as mock_pipeline:
        threads = [
            threading.Thread(target=tts_engine._get_pipeline, args=("en",))
            for _ in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert mock_pipeline.call_count == 1
