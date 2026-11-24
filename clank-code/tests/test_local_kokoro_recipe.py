from src.openclank import local_model_executors as recipes
from src.openclank.local_executor import ExecutorOutput, LocalExecutorError


class _Pipeline:
    available = True

    def synthesize_raw(self, text, voice):
        assert text == "hello"
        assert voice == "af_heart"
        return b"RIFFwav"


def test_kokoro_recipe_uses_fixed_wav_default_without_loading_model(monkeypatch):
    monkeypatch.setitem(recipes._instances, "kokoro-82m", _Pipeline())
    output = recipes._kokoro("audio.synthesize", [], {"text": "hello"})
    assert isinstance(output, ExecutorOutput)
    assert output.media_type == "audio/wav"


def test_kokoro_recipe_rejects_unimplemented_speed_and_format(monkeypatch):
    monkeypatch.setitem(recipes._instances, "kokoro-82m", _Pipeline())
    for options, message in (
        ({"text": "hello", "speed": 1.25}, "speech speed"),
        ({"text": "hello", "responseFormat": "mp3"}, "WAV"),
    ):
        try:
            recipes._kokoro("audio.synthesize", [], options)
        except LocalExecutorError as error:
            assert message in str(error)
        else:
            raise AssertionError("unsupported Kokoro option was accepted")

